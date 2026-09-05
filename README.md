# zs-lab-cloud-dashboard

**Switchboard** — plug in a cloud, flip a use case on. A small control plane for the
lab's cloud footprint: connect a provider, see what is running and what it costs,
and turn whole lab environments on and off from one page.

## Status

| | |
|---|---|
| **Owner** | Nils Ujma |
| **Context** | Zscaler — demo lab. Standalone project; the only thing it shares with other work is the ZPA tenant |
| **Stability** | experimental |
| **Runtime** | Docker Compose on Ubuntu x86_64; image: Python 3.12 (FastAPI, boto3), OpenTofu 1.12, AWS CLI v2, git |

## Why

Lab environments in the cloud get built once, left running, and forgotten, and each
one has its own README, its own scripts and its own way of being torn down. The
[ZPA Private Service Edge lab](https://github.com/nilsujma-dev/zs-zpa-private-service-edge-lab)
costs about $285 a month while it is up; the only way to know that, or to switch
it off, was to remember where the repo was and run the right commands with the
right credentials exported.

Switchboard replaces that with two pages:

- **Clouds** — a patch panel with one jack per provider. Plug in AWS with
  credentials, watch the connection checklist fill in (STS identity, regions,
  EC2, Pricing API, state bucket), then see a live inventory of every region
  — instances, VPCs, NAT gateways, elastic IPs, volumes — grouped by `Project`
  tag with a monthly cost estimate from on-demand list prices. GCP and Azure are
  present on the panel but honestly labelled as not wired yet; nothing in the UI
  or the engine is AWS-shaped.
- **Use cases** — a card per lab environment with a physical-feeling on/off
  switch, a state lamp, the turn-on and turn-off procedure shown step by step with
  a live log, and a code browser for the repo it is built from. A use case is a
  short YAML manifest pointing at a git repo; the engine clones it, runs
  `tofu init` against a shared S3 state backend, and executes the manifest's
  shell steps in order. First use case: the PSE lab, already running in AWS.

It exists for a demo lab, so breaking it costs a rebuild, not a customer.

### Security model, in plain terms

This thing holds cloud credentials and can destroy infrastructure, so:

- **One password**, set once by `deploy.sh`, printed once, kept only on the host.
  Every route needs it except login, the health probe and static files.
- **Credentials are encrypted at rest** with a Fernet key that lives only in the
  host's `.env`, never in the repo, the image or the data volume. Rotate the key
  and every stored credential is gone — by design.
- **Credentials never leave the server.** No API response contains a secret; the
  Clouds page shows identity and status, not keys. Job logs are scrubbed before
  they are written.
- **The Zscaler OneAPI secret is a read-only file mount**, read at use time.
- **Use-case steps run with a minimal environment** as an unprivileged user; the
  decrypted credentials exist only inside that subprocess for its lifetime.
- **The host is reachable only through ZPA.** Plain HTTP on `:8080`; TLS
  termination is a v2 item, see the runbook.

## Quick start

From the operator's Mac, with the ZPA tunnel up and Docker on the host:

```sh
git clone git@github.com:nilsujma-dev/zs-lab-cloud-dashboard.git
cd zs-lab-cloud-dashboard
./deploy.sh
```

The first run generates the host's `.env`, prints the operator password once,
builds the image, starts the container and waits for `/api/health`. Then open
`http://10.1.200.10:8080`, log in, plug in AWS with fresh SSO credentials, and the
PSE use case shows **on** with its enrolment status.

```sh
./deploy.sh --status   # compose ps + health
./deploy.sh --logs     # follow logs
./deploy.sh --down     # stop; the data volume is kept
```

Local development runs the same image:

```sh
docker build -t switchboard:dev .
docker run --rm -p 8080:8080 -e SWITCHBOARD_PASSWORD=dev \
  -e SWITCHBOARD_SECRET_KEY="$(openssl rand -base64 32 | tr '+/' '-_')" switchboard:dev
```

## Configuration

`.env` on the host, generated from `.env.example` by `deploy.sh`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SWITCHBOARD_PASSWORD` | yes | generated | Single operator password for UI and API |
| `SWITCHBOARD_SECRET_KEY` | yes | generated | Fernet key for credentials at rest and the session cookie |
| `SWITCHBOARD_DATA` | no | `/data` | Data directory inside the container (the `data` named volume) |
| `ZSCALER_API_KEY_FILE` | no | `/run/secrets/zscaler_api_key` | Path of the mounted OneAPI secret |
| `TZ` | no | `Europe/Amsterdam` | Timezone for logs and timestamps |

Two host files are required and are checked by `deploy.sh`:

| File | Description |
|---|---|
| `~/.config/zscaler/oneapi.env` | `ZS_ISSUER`, `ZS_CLIENT_ID`, `ZPA_CUSTOMER_ID`, `ZS_GATEWAY`; loaded as a second `env_file` |
| `~/.zscaler_api_key` | OneAPI client secret, mode 0600; bind-mounted read-only |

Secrets never live in this repo. Cloud credentials are entered in the UI and
stored encrypted; OpenTofu state lives in S3 (`zs-lab-tfstate-<account-id>`,
`use_lockfile = true`), never on disk.

## Lab / network notes

| | |
|---|---|
| Host | `10.1.200.10` (`nils@`), Ubuntu x86_64, Docker + compose plugin |
| App | `http://10.1.200.10:8080`, bound on `0.0.0.0:8080` on the host |
| Reachability | Through the **ZPA tunnel only**. No public IP, no LAN path; `deploy.sh`, `ssh` and the browser all need the tunnel |
| Repo on host | `~/switchboard` (rsynced by `deploy.sh`) |
| Data | Docker named volume `switchboard_data` at `/data` in the container |
| State bucket | `zs-lab-tfstate-<aws-account-id>` in `eu-central-1`, created on provider connect |
| Shared with other work | The ZPA tenant only. No VPCs, subnets, segments or policies |

## Runbook

See [docs/runbook.md](docs/runbook.md) for deploy and redeploy, rotating the
password and the secret key, what lives in `/data` and how to back it up, reading
job logs on the host, adding a provider module, adding a use case, and the TLS
plan.

## Repo checklist

- [x] Name follows `zs-<area>-<thing>` (see the account conventions on the profile)
- [ ] Topics set: `zscaler`, `zpa`, `aws`, `opentofu`, `fastapi`, `lab`
- [ ] Description filled in on GitHub
- [x] `.gitignore` covers local config and state
- [ ] Secrets confirmed absent from history — see `SECURITY.md`

## Licence

MIT — see [LICENSE](LICENSE).
