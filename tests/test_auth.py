"""Auth guard: 401 without cookie, 200 with; error shape; no secret echo."""

from __future__ import annotations

import pytest

from app.auth import COOKIE_NAME, Auth
from tests.conftest import TEST_PASSWORD


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/auth/me"),
        ("get", "/api/providers"),
        ("get", "/api/providers/aws/inventory"),
        ("post", "/api/providers/aws/connect"),
        ("delete", "/api/providers/aws"),
        ("get", "/api/usecases"),
        ("get", "/api/usecases/x"),
        ("post", "/api/usecases/x/on"),
        ("post", "/api/usecases/x/off"),
        ("post", "/api/usecases/x/refresh"),
        ("get", "/api/usecases/x/code"),
        ("get", "/api/jobs/j"),
        ("get", "/api/jobs/j/log"),
    ],
)
def test_routes_require_cookie(client, method: str, path: str) -> None:
    r = getattr(client, method)(path)
    assert r.status_code == 401
    assert r.json() == {"error": "Not authenticated", "code": "unauthenticated"}


def test_health_is_open(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and isinstance(body["version"], str) and body["version"]


def test_login_sets_httponly_cookie_and_unlocks(client) -> None:
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 204
    set_cookie = r.headers["set-cookie"].lower()
    assert COOKIE_NAME in set_cookie and "httponly" in set_cookie and "samesite=lax" in set_cookie
    assert "max-age=43200" in set_cookie
    assert client.get("/api/auth/me").json() == {"authenticated": True}
    assert client.get("/api/providers").status_code == 200
    assert client.get("/api/usecases").status_code == 200


def test_wrong_password(client) -> None:
    r = client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    assert r.json()["code"] == "bad_password"
    assert client.get("/api/auth/me").status_code == 401


def test_logout_clears_session(logged_in) -> None:
    assert logged_in.get("/api/auth/me").status_code == 200
    assert logged_in.post("/api/auth/logout").status_code == 204
    assert logged_in.get("/api/auth/me").status_code == 401


def test_forged_cookie_rejected(client) -> None:
    client.cookies.set(COOKIE_NAME, "forged.value.here")
    assert client.get("/api/auth/me").status_code == 401


def test_expired_cookie_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.auth as auth_mod

    a = Auth(password="x", secret_key="k" * 32)

    class _Resp:
        cookie: str = ""

        def set_cookie(self, name: str, value: str, **_kw: object) -> None:
            self.cookie = value

    resp = _Resp()
    a.issue_cookie(resp)  # type: ignore[arg-type]

    class _Req:
        cookies = {COOKIE_NAME: resp.cookie}

    assert a.is_authenticated(_Req())  # type: ignore[arg-type]
    monkeypatch.setattr(auth_mod, "SESSION_TTL_S", -1)
    assert not a.is_authenticated(_Req())  # type: ignore[arg-type]


def test_unset_password_locks_everyone_out() -> None:
    a = Auth(password="", secret_key="k" * 32)
    assert not a.enabled
    assert not a.check_password("")


def test_validation_errors_never_echo_input(logged_in) -> None:
    r = logged_in.post("/api/providers/aws/connect", json={"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": 12345})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text and "12345" not in r.text


def test_provider_listing_has_no_credentials(logged_in, data_dir) -> None:
    from app.store import Store

    store = Store(data_dir)
    store.save_provider(
        "aws",
        {
            "status": "connected",
            "identity": {"account": "257300000000", "arn": "arn:aws:iam::257300000000:user/x", "alias": None},
            "regions": ["eu-central-1"],
            "credentials": store.encrypt({"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "TOPSECRET", "session_token": None}),
            "connected_at": "2026-09-05T10:00:00+00:00",
        },
    )
    r = logged_in.get("/api/providers")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["status"] == "connected" and body[0]["identity"]["account"] == "257300000000"
    assert "credentials" not in body[0]
    assert "TOPSECRET" not in r.text and "AKIA" not in r.text
    raw = (data_dir / "providers.json").read_text()
    assert "TOPSECRET" not in raw and "AKIAIOSFODNN7EXAMPLE" not in raw


def test_unknown_route_uses_error_shape(logged_in) -> None:
    r = logged_in.get("/api/nope")
    assert r.status_code == 404
    assert set(r.json()) == {"error", "code"}
