#!/usr/bin/env bash
# deploy.sh — push Switchboard to the lab host and (re)start it.
#
#   ./deploy.sh            rsync the repo, generate .env on first run, build, start, wait for health
#   ./deploy.sh --logs     follow the container logs
#   ./deploy.sh --status   compose ps + health probe
#   ./deploy.sh --down     stop and remove the container (the data volume is kept)
#
# Run from the operator's Mac. The host is reachable only through the ZPA tunnel.
# Override the target with SWITCHBOARD_HOST=user@host.
set -euo pipefail

HOST="${SWITCHBOARD_HOST:-nils@10.1.200.10}"
REMOTE_DIR="switchboard"          # relative to the remote user's home
PORT=8080
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

ssh_host() {
  ssh -o ConnectTimeout=10 "$HOST" "$@"
}

host_only() {
  # user@host -> host, for the URL we print at the end
  printf '%s' "${HOST##*@}"
}

sync_repo() {
  log "Syncing repo to ${HOST}:~/${REMOTE_DIR}"
  rsync -az --delete \
    --exclude '.git' \
    --exclude 'tests' \
    --exclude 'data' \
    --exclude '__pycache__' \
    --exclude '*.tfstate*' \
    --exclude '.env' \
    --exclude '.DS_Store' \
    "${HERE}/" "${HOST}:${REMOTE_DIR}/"
}

# Everything below runs ON THE HOST. Single-quoted heredoc: nothing expands locally.
remote_deploy() {
  log "Preparing host and starting Switchboard"
  ssh_host bash -s -- "$REMOTE_DIR" "$PORT" <<'REMOTE'
set -euo pipefail
remote_dir="$1"
port="$2"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

cd "${HOME}/${remote_dir}" || die "remote dir ~/${remote_dir} missing (rsync failed?)"

# --- prerequisites ------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker is not installed on $(hostname)"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is not available on $(hostname)"
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon as $(id -un) — is this user in the docker group?"

api_key="${HOME}/.zscaler_api_key"
oneapi_env="${HOME}/.config/zscaler/oneapi.env"

[[ -f "$api_key" ]] || die "missing ${api_key} (OneAPI client secret). Create it with mode 0600 and rerun."
mode="$(stat -c '%a' "$api_key")"
[[ "$mode" == "600" ]] || die "${api_key} is mode ${mode}; it must be 0600 (chmod 600 ${api_key})"
[[ -s "$api_key" ]] || die "${api_key} is empty"

[[ -f "$oneapi_env" ]] || die "missing ${oneapi_env} (needs ZS_ISSUER, ZS_CLIENT_ID, ZPA_CUSTOMER_ID, ZS_GATEWAY)"
for var in ZS_ISSUER ZS_CLIENT_ID ZPA_CUSTOMER_ID ZS_GATEWAY; do
  grep -Eq "^${var}=." "$oneapi_env" || die "${oneapi_env} does not set ${var}"
done

if [[ "$(id -u)" != "1000" ]]; then
  printf '\033[1;33mwarning:\033[0m %s\n' \
    "you are uid $(id -u); the container runs as uid 1000 and will not be able to read ${api_key}." >&2
fi

# --- .env: generate once, keep forever ------------------------------------------
if [[ ! -f .env ]]; then
  log "No .env on host — generating from .env.example"
  [[ -f .env.example ]] || die ".env.example missing from the synced repo"

  # Fernet key: 32 random bytes, urlsafe base64.
  secret_key="$(openssl rand -base64 32 | tr '+/' '-_')"
  # 24-char alphanumeric operator password.
  # cut consumes all of its input; a head(1) here would SIGPIPE tr and, under
  # pipefail + errexit, abort the whole remote script without a message.
  password="$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | cut -c1-24)"
  [[ ${#password} -eq 24 ]] || die "password generation failed"

  umask 077
  sed -e "s|^SWITCHBOARD_PASSWORD=.*|SWITCHBOARD_PASSWORD=${password}|" \
      -e "s|^SWITCHBOARD_SECRET_KEY=.*|SWITCHBOARD_SECRET_KEY=${secret_key}|" \
      .env.example > .env
  chmod 600 .env

  printf '\n'
  printf '\033[1;33m========================================================================\033[0m\n'
  printf '\033[1;33m  SAVE THIS — the Switchboard password is shown once and never again:\033[0m\n'
  printf '\n'
  printf '      %s\n' "$password"
  printf '\n'
  printf '  It lives only in ~/%s/.env on this host (mode 0600).\n' "$remote_dir"
  printf '\033[1;33m========================================================================\033[0m\n'
  printf '\n'
else
  chmod 600 .env
  grep -Eq '^SWITCHBOARD_PASSWORD=.'   .env || die ".env exists but SWITCHBOARD_PASSWORD is empty"
  grep -Eq '^SWITCHBOARD_SECRET_KEY=.' .env || die ".env exists but SWITCHBOARD_SECRET_KEY is empty"
fi

# --- build and start ------------------------------------------------------------
log "docker compose up -d --build"
docker compose up -d --build

# --- wait for health (90 s) -----------------------------------------------------
log "Waiting for http://localhost:${port}/api/health"
healthy=0
for _ in $(seq 1 45); do
  if curl -fsS -m 3 "http://localhost:${port}/api/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done

if [[ "$healthy" != "1" ]]; then
  printf '\033[1;31merror:\033[0m health check did not pass within 90 s\n' >&2
  docker compose ps >&2 || true
  docker compose logs --tail=60 >&2 || true
  exit 1
fi
log "Healthy"
REMOTE
}

cmd_deploy() {
  sync_repo
  remote_deploy
  log "Switchboard is up: http://$(host_only):${PORT}"
}

cmd_logs() {
  ssh -t -o ConnectTimeout=10 "$HOST" "cd '${REMOTE_DIR}' && docker compose logs -f --tail=200"
}

cmd_status() {
  ssh_host bash -s -- "$REMOTE_DIR" "$PORT" <<'REMOTE'
set -euo pipefail
cd "${HOME}/$1" 2>/dev/null || { echo "not deployed: ~/$1 missing"; exit 1; }
docker compose ps
printf 'health: '
if curl -fsS -m 3 "http://localhost:$2/api/health"; then printf '\n'; else printf 'unreachable\n'; exit 1; fi
REMOTE
}

cmd_down() {
  log "Stopping Switchboard on ${HOST} (the data volume is kept)"
  ssh_host bash -s -- "$REMOTE_DIR" <<'REMOTE'
set -euo pipefail
cd "${HOME}/$1" 2>/dev/null || { echo "not deployed: ~/$1 missing"; exit 0; }
docker compose down
REMOTE
}

case "${1:-deploy}" in
  deploy)            cmd_deploy ;;
  --logs|logs)       cmd_logs ;;
  --status|status)   cmd_status ;;
  --down|down)       cmd_down ;;
  -h|--help|help)    usage ;;
  *)                 usage >&2; die "unknown argument: $1" ;;
esac
