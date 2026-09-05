"""Switchboard FastAPI app: all routes from SPEC.md, plus /api/health and the static UI."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


class NoCacheStaticFiles(StaticFiles):
    """Static files that browsers must revalidate on every load.

    Without an explicit Cache-Control, browsers heuristically cache app.js and
    app.css from Last-Modified, so a deploy is invisible until a hard refresh.
    ETag stays, so an unchanged file is still a cheap 304.
    """

    async def get_response(self, path, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException  # base class: catches FastAPI's subclass and router 404s

from app.auth import Auth
from app.jobs import JobRunner
from app.providers import build_registry
from app.providers.base import FormError, Provider
from app.store import Store, utcnow_iso
from app.usecases.engine import Engine, EngineError, iso_age_seconds

log = logging.getLogger("switchboard")
logging.basicConfig(level=os.environ.get("SWITCHBOARD_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
INVENTORY_CACHE_S = 10 * 60
PLACEHOLDER_HTML = "<!doctype html><title>Switchboard</title><p>Switchboard backend is up; the UI has not been built yet.</p>"


def _version() -> str:
    if os.environ.get("SWITCHBOARD_VERSION"):
        return os.environ["SWITCHBOARD_VERSION"]
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "dev"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, "code": code})


# ---------------------------------------------------------------------- request models
class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


# ---------------------------------------------------------------------- app state
class State:
    """Singletons built at startup; attached as app.state.sb."""

    def __init__(self) -> None:
        self.store = Store()
        self.auth = Auth()
        self.providers: dict[str, Provider] = build_registry(self.store.pricing_cache_path)
        self.jobs = JobRunner(self.store)
        usecases_root = Path(os.environ.get("SWITCHBOARD_USECASES") or (REPO_DIR / "usecases"))
        self.engine = Engine(self.store, self.providers, self.jobs, usecases_root)
        self.version = _version()
        self.inventory_lock = self.engine.inventory_lock


def build_app(*, background: bool | None = None) -> FastAPI:
    run_background = background if background is not None else os.environ.get("SWITCHBOARD_BACKGROUND", "1") != "0"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sb = State()
        app.state.sb = sb
        recovered = sb.jobs.recover()
        if recovered:
            log.warning("marked %d interrupted job(s) as failed", recovered)
        if not sb.auth.enabled:
            log.error("SWITCHBOARD_PASSWORD is not set: nobody can log in")
        if run_background:
            sb.engine.start_background()
        log.info("Switchboard %s ready; data at %s", sb.version, sb.store.root)
        try:
            yield
        finally:
            sb.engine.stop_background()

    app = FastAPI(title="Switchboard", version=_version(), lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # ------------------------------------------------------------------ errors
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(EngineError)
    async def _engine_error(_request: Request, exc: EngineError) -> JSONResponse:
        return error_response(exc.status, exc.code, str(exc))

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            return error_response(exc.status_code, str(detail.get("code", "error")), str(detail["error"]))
        code = {401: "unauthenticated", 404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return error_response(exc.status_code, code, str(detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo `input` back: the connect body carries credentials.
        fields = sorted({".".join(str(p) for p in e.get("loc", ()) if p != "body") or "body" for e in exc.errors()})
        return error_response(422, "validation_error", f"Invalid request: check {', '.join(fields)}")

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", type(exc).__name__)
        return error_response(500, "internal_error", "Internal error; see the server log")

    # ------------------------------------------------------------------ helpers
    def sb(request: Request) -> State:
        return request.app.state.sb

    def require_auth(request: Request) -> None:
        sb(request).auth.require(request)

    # ------------------------------------------------------------------ open routes
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, str]:
        return {"status": "ok", "version": sb(request).version}

    @app.post("/api/auth/login", status_code=204)
    async def login(body: LoginRequest, request: Request) -> Response:
        auth = sb(request).auth
        if not auth.check_password(body.password):
            raise ApiError(401, "bad_password", "Wrong password")
        response = Response(status_code=204)
        auth.issue_cookie(response)
        return response

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        response = Response(status_code=204)
        sb(request).auth.clear_cookie(response)
        return response

    # ------------------------------------------------------------------ guarded routes
    api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

    @api.get("/auth/me")
    async def me() -> dict[str, bool]:
        return {"authenticated": True}

    def _provider_view(provider: Provider, record: dict[str, Any] | None) -> dict[str, Any]:
        connected = bool(record) and record.get("status") == "connected"
        identity = (record or {}).get("identity") if connected else None
        return {
            "id": provider.id,
            "name": provider.name,
            "status": "connected" if connected else "disconnected",
            "identity": identity,
            "identity_label": provider.identity_label(identity) if connected else None,
            "regions": (record or {}).get("regions", []) if connected else [],
            "connected_at": (record or {}).get("connected_at") if connected else None,
            "credentials_updated_at": (record or {}).get("credentials_updated_at") if connected else None,
            "capabilities": dict(provider.capabilities),
        }

    def _provider(request: Request, provider_id: str) -> Provider:
        provider = sb(request).providers.get(provider_id)
        if provider is None:
            raise ApiError(404, "unknown_provider", f"Unknown provider '{provider_id}'")
        return provider

    @api.get("/providers")
    async def list_providers(request: Request) -> list[dict[str, Any]]:
        s = sb(request)
        records = s.store.get_providers()
        return [_provider_view(p, records.get(pid)) for pid, p in s.providers.items()]

    @api.get("/providers/{provider_id}/form")
    async def provider_form(provider_id: str, request: Request) -> dict[str, Any]:
        provider = _provider(request, provider_id)
        return {"fields": [f.to_api() for f in provider.form_fields()]}

    @api.post("/providers/{provider_id}/connect")
    async def connect_provider(provider_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Runs the provider checklist. Stores (or atomically replaces) credentials only when every
        required check passes; a failed reconnect leaves the previous credentials untouched."""
        s = sb(request)
        provider = _provider(request, provider_id)
        try:
            credentials = provider.parse_form(body)
        except FormError as exc:
            raise ApiError(422, "validation_error", str(exc)) from None
        regions = body.get("regions")
        if regions is not None and (not isinstance(regions, list) or not all(isinstance(r, str) and r.strip() for r in regions)):
            raise ApiError(422, "validation_error", "Invalid request: check regions")
        regions = sorted({r.strip() for r in regions}) if regions else None
        result = await run_in_threadpool(provider.connect, credentials, regions)
        if result.credentials is not None and result.report.identity is not None:
            now = utcnow_iso()
            previous = s.store.get_provider(provider_id) or {}
            was_connected = previous.get("status") == "connected"
            s.store.save_provider(
                provider_id,
                {
                    "status": "connected",
                    "identity": result.report.identity.to_api(),
                    "regions": result.regions,
                    "credentials": s.store.encrypt(result.credentials),
                    "connected_at": previous.get("connected_at") if was_connected and previous.get("connected_at") else now,
                    "credentials_updated_at": now,
                },
            )
            for uc_id in s.engine.manifests()[0]:
                s.engine.invalidate(uc_id)
            if run_background:
                threading.Thread(target=s.engine.warm_up, name="engine-warmup", daemon=True).start()
        return result.report.to_api()

    @api.delete("/providers/{provider_id}", status_code=204)
    async def disconnect_provider(provider_id: str, request: Request) -> Response:
        s = sb(request)
        _provider(request, provider_id)
        s.store.delete_provider(provider_id)
        for uc_id in s.engine.manifests()[0]:
            s.engine.invalidate(uc_id)
        return Response(status_code=204)

    @api.get("/providers/{provider_id}/inventory")
    async def provider_inventory(provider_id: str, request: Request, refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
        s = sb(request)
        provider = _provider(request, provider_id)
        if not provider.capabilities.get("inventory"):
            return {**provider.unsupported_inventory(), "generated_at": None, "stale": False}
        record = s.store.get_provider(provider_id)
        if not record or record.get("status") != "connected":
            raise ApiError(409, "provider_not_connected", f"Provider '{provider_id}' is not connected")
        cached = s.store.get_inventory(provider_id)
        age = iso_age_seconds(cached.get("generated_at")) if cached else None
        if cached and not refresh and age is not None and age < INVENTORY_CACHE_S:
            return {**cached, "stale": False}

        def scan() -> dict[str, Any]:
            with s.inventory_lock:
                latest = s.store.get_inventory(provider_id)
                latest_age = iso_age_seconds(latest.get("generated_at")) if latest else None
                if latest and not refresh and latest_age is not None and latest_age < INVENTORY_CACHE_S:
                    return {**latest, "stale": False}
                creds = s.store.provider_credentials(provider_id)
                if creds is None:
                    raise ApiError(409, "provider_not_connected", f"Provider '{provider_id}' is not connected")
                inventory = provider.inventory(creds, record.get("regions") or [])
                s.store.save_inventory(provider_id, inventory)
                return inventory

        try:
            return await run_in_threadpool(scan)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - serve the stale copy rather than nothing
            log.exception("inventory scan failed for %s", provider_id)
            if cached:
                return {**cached, "stale": True, "error": f"Refresh failed: {type(exc).__name__}"}
            raise ApiError(502, "inventory_failed", f"Inventory scan failed: {type(exc).__name__}") from None

    # ------------------------------------------------------------------ use cases
    @api.get("/usecases")
    async def list_usecases(request: Request) -> list[dict[str, Any]]:
        engine = sb(request).engine
        manifests, errors = engine.manifests()
        for uc_id, err in errors.items():
            log.error("manifest %s ignored: %s", uc_id, err)
        return await run_in_threadpool(lambda: [engine.summary(m) for m in manifests.values()])

    @api.get("/usecases/{usecase_id}")
    async def get_usecase(usecase_id: str, request: Request) -> dict[str, Any]:
        engine = sb(request).engine
        manifest = engine.manifest(usecase_id)
        return await run_in_threadpool(engine.detail, manifest)

    @api.post("/usecases/{usecase_id}/on", status_code=202)
    async def usecase_on(usecase_id: str, request: Request) -> dict[str, str]:
        engine = sb(request).engine
        return {"job_id": engine.start_job(engine.manifest(usecase_id), "on")}

    @api.post("/usecases/{usecase_id}/off", status_code=202)
    async def usecase_off(usecase_id: str, request: Request) -> dict[str, str]:
        engine = sb(request).engine
        return {"job_id": engine.start_job(engine.manifest(usecase_id), "off")}

    @api.post("/usecases/{usecase_id}/refresh")
    async def usecase_refresh(usecase_id: str, request: Request) -> dict[str, Any]:
        engine = sb(request).engine
        manifest = engine.manifest(usecase_id)

        def refresh() -> dict[str, Any]:
            engine.invalidate(manifest.id)
            engine.state(manifest, force=True)
            engine.probe_status(manifest)
            return engine.detail(manifest)

        return await run_in_threadpool(refresh)

    @api.get("/usecases/{usecase_id}/outline")
    async def usecase_outline(usecase_id: str, request: Request, action: str = Query(...)) -> dict[str, Any]:
        engine = sb(request).engine
        manifest = engine.manifest(usecase_id)
        if action not in ("on", "off"):
            raise ApiError(400, "bad_action", "action must be 'on' or 'off'")
        return await run_in_threadpool(engine.outline, manifest, action)

    @api.get("/usecases/{usecase_id}/topology")
    async def usecase_topology(usecase_id: str, request: Request, refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
        """Live network drawing data (v1.2). Always 200: `nodes: []` + `reason` when nothing can be drawn."""
        engine = sb(request).engine
        manifest = engine.manifest(usecase_id)
        return await run_in_threadpool(lambda: engine.topology(manifest, refresh=bool(refresh)))

    @api.get("/usecases/{usecase_id}/code")
    async def usecase_code(usecase_id: str, request: Request, path: str | None = None) -> dict[str, Any]:
        engine = sb(request).engine
        manifest = engine.manifest(usecase_id)
        if path:
            return await run_in_threadpool(engine.code_file, manifest, path)
        return await run_in_threadpool(engine.code_tree, manifest)

    # ------------------------------------------------------------------ jobs
    @api.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> dict[str, Any]:
        job = sb(request).jobs.get(job_id)
        if job is None:
            raise ApiError(404, "not_found", f"Unknown job '{job_id}'")
        return job

    @api.get("/jobs/{job_id}/log")
    async def get_job_log(job_id: str, request: Request, since: int = Query(0, ge=0)) -> dict[str, Any]:
        result = sb(request).jobs.read_log(job_id, since)
        if result is None:
            raise ApiError(404, "not_found", f"Unknown job '{job_id}'")
        lines, next_offset = result
        return {"lines": lines, "next": next_offset}

    app.include_router(api)

    # ------------------------------------------------------------------ static UI
    if (STATIC_DIR / "index.html").is_file():
        app.mount("/", NoCacheStaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:
        log.warning("%s missing; serving a placeholder page", STATIC_DIR / "index.html")

        @app.get("/", include_in_schema=False)
        async def placeholder() -> HTMLResponse:
            return HTMLResponse(PLACEHOLDER_HTML)

    return app


app = build_app()
