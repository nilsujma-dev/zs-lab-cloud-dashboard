# Switchboard runbook

Operating notes for the control plane on `10.1.200.10`. The host is reachable only
through the ZPA tunnel; nothing here is exposed to the internet.

Contents

- [Deploy and redeploy](#deploy-and-redeploy)
- [Rotating the password](#rotating-the-password)
- [Rotating the secret key](#rotating-the-secret-key)
- [What lives in /data and how to back it up](#what-lives-in-data-and-how-to-back-it-up)
- [Reading job logs on the host](#reading-job-logs-on-the-host)
- [Adding a provider module](#adding-a-provider-module)
- [Adding a use case](#adding-a-use-case)
- [Network reachability](#network-reachability)
- [TLS termination (v2)](#tls-termination-v2)

## Deploy and redeploy

Everything goes through `deploy.sh`, run from the operator's Mac with the ZPA
tunnel up. It is idempotent: run it as often as you like.

```sh
./deploy.sh              # sync, build, start, wait for /api/health
./deploy.sh --status     # compose ps + health probe
./deploy.sh --logs       # follow container logs
./deploy.sh --down       # stop the container; the data volume is kept
```

What a deploy does, in order:

1. `rsync -az --delete` the repo to `~/switchboard` on the host, excluding `.git`,
   `tests`, `data`, `__pycache__`, `*.tfstate*` and `.env`. The host's `.env` is
   protected from `--delete` by that exclude; it is never overwritten.
2. Preflight on the host: docker and the compose plugin exist, `~/.zscaler_api_key`
   exists and is mode `0600`, `~/.config/zscaler/oneapi.env` exists and sets
   `ZS_ISSUER`, `ZS_CLIENT_ID`, `ZPA_CUSTOMER_ID`, `ZS_GATEWAY`. Any miss fails
   loudly before anything is built.
3. First run only: `.env` is generated from `.env.example` with a random Fernet
   key and a random 24-character password. **The password is printed once.** It is
   not stored anywhere but `~/switchboard/.env` (mode 0600) on the host.
4. `docker compose up -d --build`, then poll `http://localhost:8080/api/health`
   for up to 90 seconds. On timeout it prints `compose ps` and the last 60 log
   lines and exits non-zero.

A redeploy is the same command. The container is rebuilt only if the image inputs
changed; the named volume `data` survives across redeploys, `--down`, and image
rebuilds. It is only removed by an explicit `docker compose down -v`, which
`deploy.sh` never runs.

Target another host with `SWITCHBOARD_HOST=user@host ./deploy.sh`. The container
runs as uid 1000; if the deploying user on the host is not uid 1000 the mounted
`~/.zscaler_api_key` (0600) will be unreadable inside the container and
`deploy.sh` warns about it.

### Manual operations on the host

```sh
ssh nils@10.1.200.10
cd ~/switchboard
docker compose ps
docker compose logs -f --tail=200
docker compose restart
docker compose exec switchboard tofu version
docker compose exec switchboard aws --version
```

### Lost the password?

It is in `~/switchboard/.env` on the host:

```sh
ssh nils@10.1.200.10 'grep ^SWITCHBOARD_PASSWORD= ~/switchboard/.env'
```

## Rotating the password

The password is only an environment variable; nothing at rest depends on it.

```sh
ssh nils@10.1.200.10
cd ~/switchboard
new="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
sed -i "s|^SWITCHBOARD_PASSWORD=.*|SWITCHBOARD_PASSWORD=${new}|" .env
echo "$new"          # save it
docker compose up -d # recreates the container with the new env
```

Existing sessions remain valid until they expire (12 h) because the session
cookie is signed with the secret key, not the password. Rotate the key too if you
want every session invalidated now.

## Rotating the secret key

`SWITCHBOARD_SECRET_KEY` is the Fernet key that encrypts cloud credentials in
`/data/providers.json` and signs the session cookie. **Changing it makes every
stored credential undecryptable and logs everyone out.** That is the intended
failure mode: there is no key-escrow and no re-encryption path in v1.

Procedure:

1. In the UI, disconnect every provider (Clouds page) — or accept that they will
   show as broken after the rotation and must be forgotten with
   `DELETE /api/providers/<id>` and reconnected.
2. On the host:

   ```sh
   cd ~/switchboard
   key="$(openssl rand -base64 32 | tr '+/' '-_')"
   sed -i "s|^SWITCHBOARD_SECRET_KEY=.*|SWITCHBOARD_SECRET_KEY=${key}|" .env
   docker compose up -d
   ```

3. Log in again and reconnect each provider with fresh credentials.

Inventory, job history, use-case checkouts and OpenTofu state are unaffected:
none of them are encrypted with the key, and state lives in S3, not in `/data`.

## What lives in /data and how to back it up

`/data` is the Docker named volume `switchboard_data`. It is the only persistent
state the container has.

| Path | What | Contains secrets? |
|---|---|---|
| `providers.json` | Per-provider status, identity, regions, and the **Fernet-encrypted** credential blob | Encrypted only |
| `inventory/<provider>.json` | Last inventory scan and cost estimate, with `generated_at` | No |
| `pricing-cache.json` | AWS Pricing API results, 24 h TTL | No |
| `usecases/<id>/checkout/` | `git clone` of the manifest's `source.git` at `source.ref`; also the `HOME` for that use case's steps | No (the checkout's `.terraform/` holds provider binaries, not state) |
| `usecases/<id>/runs/<job_id>.json` | Job record: action, steps, exit codes, timestamps | No |
| `usecases/<id>/runs/<job_id>.log` | Step-by-step log, append-only, scrubbed before write | Scrubbed |
| `usecases/<id>/status.json` | Last status-probe output | No |

What is deliberately **not** in `/data`:

- The Fernet key (env only, in `~/switchboard/.env` on the host).
- The Zscaler OneAPI secret (bind-mounted read-only at `/run/secrets/zscaler_api_key`).
- OpenTofu state. Every `tofu init` points at the S3 backend
  `zs-lab-tfstate-<aws-account-id>` (versioned, public access blocked, SSE-S3,
  `use_lockfile = true`). The lab repo and Switchboard share that state; there is
  one source of truth per use case.

### Backup

Almost everything in `/data` is a cache that regenerates itself. The only thing
worth keeping is `providers.json` (saves reconnecting) and `usecases/*/runs/`
(audit history). A backup is only useful together with the `.env` that holds the
key that can decrypt it.

```sh
ssh nils@10.1.200.10
docker run --rm -v switchboard_data:/data:ro -v "$HOME":/backup alpine \
  tar czf /backup/switchboard-data-$(date +%F).tgz -C /data .
cp ~/switchboard/.env ~/switchboard-env-$(date +%F)   # keep next to the tarball, mode 0600
```

Restore into a fresh volume:

```sh
docker compose down
docker run --rm -v switchboard_data:/data -v "$HOME":/backup alpine \
  sh -c 'cd /data && tar xzf /backup/switchboard-data-YYYY-MM-DD.tgz && chown -R 1000:1000 /data'
docker compose up -d
```

Treat the tarball plus `.env` as a credential: together they are the cloud
credentials in clear.

### Starting over

`docker compose down -v` deletes the volume. Nothing in AWS or ZPA is touched;
use-case state is in S3 and reappears as soon as the provider is reconnected and
the engine runs `tofu init` again.

## Reading job logs on the host

The UI tails the same file the engine writes. To read it directly:

```sh
ssh nils@10.1.200.10
cd ~/switchboard
docker compose exec switchboard sh -c 'ls -t /data/usecases/zpa-private-service-edge/runs/'
docker compose exec switchboard sh -c 'tail -f /data/usecases/zpa-private-service-edge/runs/<job_id>.log'
docker compose exec switchboard sh -c 'cat /data/usecases/zpa-private-service-edge/runs/<job_id>.json'
```

Or straight from the volume without entering the container:

```sh
sudo tail -f /var/lib/docker/volumes/switchboard_data/_data/usecases/zpa-private-service-edge/runs/<job_id>.log
```

Logs are scrubbed before write (stored secret values, `AKIA…`/`ASIA…` key ids,
long base64 runs become `<redacted>`). If you see a credential in a log, that is a
bug — report it, do not paste it anywhere.

Application (uvicorn) logs go to the container's stdout: `./deploy.sh --logs`.

## Adding a provider module

Providers live in `app/providers/`. Nothing outside that package may be shaped
around a specific cloud: the UI renders whatever the provider returns.

1. Create `app/providers/<id>.py` implementing the `Provider` interface from
   `app/providers/base.py`: `connect()` returning a `ConnectionReport` (a list of
   named checks, each required or not), `inventory()` returning regions, resources,
   totals, tag groups and a cost block, `forget()`, and whatever the engine needs to
   inject credentials into a step's environment.
2. Register it in `app/providers/__init__.py` (`{"aws": AwsProvider, "<id>": …}`).
3. Persist only what the interface says: credentials go through the store's Fernet
   helpers; never write a secret in clear to `/data`.
4. If the provider needs a CLI in the image (GCP: `gcloud`; Azure: `az`), add a
   pinned install layer to the `Dockerfile` next to the AWS CLI one, and bump the
   `Runtime` row in `README.md`.
5. Add a use case manifest with `provider: <id>` to prove it end to end; the
   Clouds page will show the new jack as soon as the registry knows about it.

## Adding a use case

A use case is a directory under `usecases/` with a `usecase.yaml` (the manifest)
and a public or reachable git repo that holds the code. The manifest is the whole
integration; Switchboard never needs to know what the repo does.

Walkthrough, using the PSE lab as the worked example
(`usecases/zpa-private-service-edge/usecase.yaml`):

| Key | Meaning |
|---|---|
| `id` | `[a-z0-9-]+`, must equal the directory name. Also the path under `/data/usecases/`. |
| `name`, `summary` | Card title and one-line summary. |
| `provider` | A registered provider id. The card is disabled until that provider is connected. |
| `description` | Markdown, rendered on the open card. Say what it builds, what on/off does, what it costs. |
| `source.git`, `source.ref` | Cloned into `checkout/` before every job; `fetch` + `reset --hard` to `ref` on later runs. |
| `terraform.dir` | Working directory for `tofu`, relative to the checkout. |
| `terraform.state_key` | Key in the S3 state bucket. Unique per use case. |
| `env` | Non-secret variables applied to every step. |
| `secrets` | Host-provided secret bundles the engine maps in. `zscaler_oneapi` gives `ZS_*` env and a `~/.zscaler_api_key` symlink to the mounted secret. |
| `on`, `off` | Ordered shell steps; the job stops on the first non-zero exit. |
| `status` | Optional probe. Must print JSON on stdout; re-run on demand and every `interval_s` while the use case is on. |
| `tags` | Tag filter used by the inventory to group resources and attribute cost to this use case. |

Rules the engine enforces:

- Steps run as `switchboard` inside the container with a minimal environment:
  provider credentials, the manifest `env`, mapped secrets, `PATH`, and
  `HOME=/data/usecases/<id>`. Anything else the repo expects must be declared.
- `tofu init` is run by the engine with the S3 `-backend-config`; the repo's
  backend block should declare `backend "s3" {}` and leave the values empty.
- State is derived, not stored: a running job is `turning_*`; otherwise a non-empty
  `tofu state list` is `on`, empty is `off`, and a tofu error is `unknown`.
- Step output is scrubbed before it reaches the log.

To add one: copy the PSE manifest, change every value, run
`docker compose exec switchboard python3 -c "from app.usecases.manifest import load_all; print(load_all())"`
(or the equivalent the backend exposes) to validate, then `./deploy.sh`. The
manifest directory is baked into the image (`COPY usecases/ usecases/`), so a new
manifest is a redeploy, not a hot reload.

Repo prerequisites for a use case that should work first time:

- The `on` steps are safe to re-run (creating ZPA objects that already exist must
  not fail).
- The `off` step leaves nothing billing.
- The status probe exits 0 and prints JSON even when nothing is deployed.

## Network reachability

Switchboard binds `0.0.0.0:8080` on `10.1.200.10`. That host sits inside the lab
network and is reachable **only through ZPA**: there is no public IP, no port
forward, no LAN path from an operator laptop. Log in to the ZPA tunnel first;
`deploy.sh`, the browser and `ssh` all go through it.

Because the network boundary is the tunnel, the single password is the second
factor, not the first. Do not expose port 8080 beyond the host.

Landmarks:

| | |
|---|---|
| Host | `10.1.200.10` (`nils@`), Ubuntu x86_64, Docker + compose plugin |
| App | `http://10.1.200.10:8080` |
| Repo on host | `~/switchboard` |
| Host secrets | `~/.zscaler_api_key` (0600), `~/.config/zscaler/oneapi.env` |
| State bucket | `zs-lab-tfstate-<aws-account-id>`, `eu-central-1` |

## TLS termination (v2)

v1 serves plain HTTP on `:8080` and relies on the ZPA tunnel for transport
security. The session cookie is `HttpOnly` and `SameSite=Lax` but not `Secure`,
because there is no TLS to set it on. This is a known v1 limitation and is stated
in the README rather than hidden.

v2 plan: put a reverse proxy in front (Caddy with an internal CA, or nginx with a
lab-issued certificate), bind Switchboard to `127.0.0.1:8080` only, set the cookie
`Secure`, and have `deploy.sh` manage the proxy as a second compose service. Until
then, do not put anything but the ZPA tunnel between a browser and this host.
