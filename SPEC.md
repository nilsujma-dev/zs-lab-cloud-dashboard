# Switchboard — engineering spec (v1)

Repo: `nilsujma-dev/zs-lab-cloud-dashboard` (public). Product name in the UI: **Switchboard**.
Runs as one Docker Compose service on `10.1.200.10`, reachable only inside the ZPA-protected
lab network. Owner: Nils Ujma.

Two pages. One idea: **plug in a cloud, flip a use case on.**

1. **Clouds** — connect a provider with credentials, see it validated with a checklist, then a
   live inventory of what is running and where, with a monthly cost estimate. AWS first; GCP and
   Azure are additional provider modules later — nothing in the UI or engine may be AWS-shaped.
2. **Use cases** — a card per use case with a description, a physical-feeling on/off switch, the
   turn-on / turn-off procedure shown step by step with a live log, and a code browser. First use
   case: the ZPA Private Service Edge lab, already running in AWS.

This is a control plane that holds cloud credentials and can create and destroy infrastructure.
Treat it accordingly. See *Security* below — those rules are not optional.

---

## Stack (decided)

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12, **FastAPI** + uvicorn | Routing, JSON, background jobs, small |
| AWS | **boto3** | Provider modules use each cloud's official SDK |
| Frontend | **Vanilla HTML/CSS/JS**, no build step, served static by FastAPI | Zero toolchain, matches the house style |
| Config/data | JSON + YAML in one named volume `/data` | Nothing else to operate |
| Secrets at rest | `cryptography` Fernet, key from env | Credentials never stored in clear |
| Use-case execution | Subprocess: **OpenTofu 1.12**, AWS CLI v2, git, python3 in the image | Manifests are shell steps, so any repo works |
| Remote state | OpenTofu **S3 backend, `use_lockfile = true`** (no DynamoDB) | One source of truth per use case; lab and dashboard share it |

Departure from the `zs-ot-ebc-dashboard` stdlib-only rule is deliberate: that constraint exists
for a host without pip. Inside a container it buys nothing, and boto3 + FastAPI remove a large
amount of hand-rolled code (a stdlib SigV4 signer already exists in the lab repo and is exactly
the kind of thing we should not maintain twice).

## Layout and ownership

```
zs-lab-cloud-dashboard/
  SPEC.md  README.md  LICENSE  SECURITY.md  .gitignore
  Dockerfile  compose.yaml  .env.example  deploy.sh        <- PACKAGING owns
  requirements.txt                                          <- BACKEND owns
  app/
    main.py            FastAPI app, all routes               <- BACKEND
    auth.py            password + session cookie             <- BACKEND
    store.py           /data layout, Fernet, JSON persistence <- BACKEND
    jobs.py            background job runner + log tail       <- BACKEND
    providers/
      __init__.py      registry: {"aws": AwsProvider}         <- BACKEND
      base.py          Provider interface + dataclasses       <- BACKEND
      aws.py           AWS implementation                     <- BACKEND
      pricing.py       AWS Pricing API lookups, cached        <- BACKEND
    usecases/
      manifest.py      schema + loader + validation           <- BACKEND
      engine.py        checkout, tofu init, step runner, status <- BACKEND
    static/
      index.html  app.css  app.js  (+ assets)                 <- FRONTEND owns
  usecases/
    zpa-private-service-edge/usecase.yaml                    <- PACKAGING
  docs/runbook.md                                            <- PACKAGING (skeleton), all extend
  tests/                                                     <- BACKEND
```

Anyone may read anything. Only the owner edits a path. Cross-cutting needs go through SPEC.md.

## Data layout (`/data`, one Docker named volume)

```
/data/
  secret.key                 NOT here -- key comes from env SWITCHBOARD_SECRET_KEY
  providers.json             {"aws": {"status":..., "identity":{...}, "regions":[...],
                                       "credentials": "<fernet blob>", "connected_at": ...}}
  inventory/aws.json         last inventory + cost, with generated_at
  usecases/<id>/
    checkout/                git clone of manifest.source
    runs/<job_id>.json       job record
    runs/<job_id>.log        step-by-step log, append-only
    status.json              last status probe
  pricing-cache.json         Pricing API results, 24h TTL
```

The tofu working directory is `checkout/<manifest.terraform.dir>`. State is **never** local —
every `tofu init` passes `-backend-config` pointing at the S3 bucket
`zs-lab-tfstate-<aws-account-id>` (created idempotently by the AWS provider on connect,
versioning on, public access blocked, SSE-S3), key = `manifest.terraform.state_key`,
region `eu-central-1`, `use_lockfile=true`.

## Security (non-negotiable)

- **Auth on every route** except `/api/auth/login` and static assets. Single password from env
  `SWITCHBOARD_PASSWORD`; signed session cookie (HttpOnly, SameSite=Lax); 12h expiry.
- Cloud credentials are Fernet-encrypted at rest with `SWITCHBOARD_SECRET_KEY` (env only,
  generated by `deploy.sh` into the host's `.env`, never committed).
- **Credentials never leave the server.** No API response includes a secret, a session token,
  or a provisioning key. `GET /api/providers` returns identity and status only.
- Zscaler OneAPI secret is a **file mount** at `/run/secrets/zscaler_api_key` (from the host's
  `~/.zscaler_api_key`), read at use time, never copied into `/data`.
- Job logs are scrubbed before write: any value matching a stored secret, `AKIA|ASIA[A-Z0-9]{16}`,
  or a 300+ char base64 run is replaced with `<redacted>`.
- Use-case steps run with a minimal env: the provider's credentials, the manifest's declared
  `env`, `ZS_*` from the host env, `PATH`, `HOME=/data/usecases/<id>`. Nothing else leaks in.
- Bind to `0.0.0.0:8080` on the host; the host is reachable only through ZPA. Document this.
  TLS termination is a v2 item; say so in the README rather than pretend.

## API contract

All JSON. Errors: `{"error": "<human sentence>", "code": "<slug>"}` with 4xx/5xx.

### Auth
```
POST /api/auth/login    {password}            -> 204, sets cookie   | 401
POST /api/auth/logout                          -> 204
GET  /api/auth/me                              -> {"authenticated": true}
```

### Providers
```
GET  /api/providers
  -> [{"id":"aws","name":"Amazon Web Services","status":"connected"|"disconnected",
       "identity":{"account":"2573…","arn":"…","alias":null}|null,
       "regions":["eu-central-1",…], "connected_at":"<iso>"|null}]

POST /api/providers/aws/connect
  {"access_key_id":…, "secret_access_key":…, "session_token":…|null, "regions":[…]|null}
  -> ConnectionReport (200 even if a check fails; "ok" tells you)
     {"ok": true,
      "identity": {"account":…, "arn":…},
      "checks": [
        {"name":"Credentials valid (STS)",            "ok":true,  "detail":"assumed-role/…"},
        {"name":"Can list regions",                   "ok":true,  "detail":"17 enabled"},
        {"name":"Can describe EC2 in eu-central-1",   "ok":true,  "detail":""},
        {"name":"Pricing API reachable",              "ok":true,  "detail":"us-east-1"},
        {"name":"State bucket ready",                 "ok":true,  "detail":"zs-lab-tfstate-2573…"},
        {"name":"Session token expiry",               "ok":true,  "detail":"temporary credentials — expires when the SSO session does"}
      ]}
  Credentials are stored ONLY if every check marked required passes (all but pricing).

DELETE /api/providers/aws                         -> 204   (forgets credentials + inventory)

GET  /api/providers/aws/inventory?refresh=0|1
  -> {"generated_at":"<iso>", "stale": false,
      "regions": [
        {"region":"eu-central-1",
         "instances":[{"id":"i-…","name":"zpa-lab-pse","type":"m5.large","state":"running",
                       "private_ip":"10.91.10.5","public_ip":"63.188.16.52",
                       "launched":"<iso>","tags":{"Project":"zpa-pse-lab",…}}],
         "vpcs":[{"id":"vpc-…","name":…,"cidr":"10.91.0.0/16","default":false}],
         "nat_gateways":[{"id":…,"vpc":…,"state":"available","public_ip":…}],
         "eips":[{"ip":…,"attached":true|false,"instance":…|null}],
         "volumes":[{"id":…,"size_gb":80,"type":"gp3","attached":true}]
        }],
      "totals":{"instances":5,"running":5,"vpcs":2,"nat_gateways":1,"eips":3,"volumes_gb":298},
      "groups":[{"key":"Project=zpa-pse-lab","instances":5,"monthly_usd":284.89}],
      "cost":{"monthly_usd":296.19,"currency":"USD","method":"on-demand list price × 730h",
              "lines":[{"item":"m5.large Linux","region":"eu-central-1","qty":730,"unit":"hr",
                        "unit_usd":0.115,"monthly_usd":83.95}, …],
              "notes":["Unattached elastic IPs are billed", "NAT data processing not included"]}}
  Scan all enabled regions in parallel (thread pool). Default VPCs are listed but flagged.
  refresh=1 bypasses the cached inventory; otherwise serve cache if < 10 min old.
```

### Use cases
```
GET  /api/usecases
  -> [{"id":"zpa-private-service-edge","name":…,"provider":"aws","summary":…,
       "state":"on"|"off"|"turning_on"|"turning_off"|"error"|"unknown",
       "resources":5, "last_run":{"job_id":…,"action":"on","state":"succeeded","ended":"<iso>"}|null,
       "provider_connected":true}]

GET  /api/usecases/{id}
  -> {…everything above…, "description":"<markdown>",
      "procedure":{"on":[{"name":…,"run":…}], "off":[…]},
      "source":{"git":…,"ref":…,"commit":"<sha>"|null},
      "status":{…output of the manifest status probe…}|null,
      "runs":[{"job_id":…,"action":…,"state":…,"started":…,"ended":…}]}   (last 20)

POST /api/usecases/{id}/on        -> 202 {"job_id":…}   | 409 if a job is already running
POST /api/usecases/{id}/off       -> 202 {"job_id":…}   | 409
POST /api/usecases/{id}/refresh   -> 200 {…state + status…}   (re-runs the status probe now)

GET  /api/usecases/{id}/code                 -> {"commit":…,"files":[{"path":…,"size":…}]}
GET  /api/usecases/{id}/code?path=terraform/main.tf
                                             -> {"path":…,"language":"hcl","content":"…"}
   Only files inside the checkout. Reject `..`. Skip .git and binaries.
```

### Jobs
```
GET  /api/jobs/{job_id}
  -> {"id":…,"usecase":…,"action":"on"|"off","state":"running"|"succeeded"|"failed",
      "steps":[{"name":…,"state":"pending"|"running"|"succeeded"|"failed"|"skipped",
                "started":…,"ended":…,"exit_code":…}],
      "started":…,"ended":…}
GET  /api/jobs/{job_id}/log?since=0     -> {"lines":[…],"next":<int>}   (poll every 1–2 s)
```

## Use-case manifest (`usecases/<id>/usecase.yaml`)

```yaml
id: zpa-private-service-edge            # [a-z0-9-]+, matches the directory name
name: ZPA Private Service Edge lab
provider: aws                           # must be a registered provider id
summary: A Private Service Edge in an isolated VPC, plus a segmented client/server VPC.
description: |                          # markdown; the card shows it
  …
source:
  git: https://github.com/nilsujma-dev/zs-zpa-private-service-edge-lab.git
  ref: main
terraform:
  dir: terraform                        # relative to checkout
  state_key: usecases/zpa-private-service-edge/terraform.tfstate
env:                                    # non-secret, applied to every step
  AWS_DEFAULT_REGION: eu-central-1
secrets:                                # host-provided; engine maps these in
  - zscaler_oneapi                      # -> ZS_ISSUER, ZS_CLIENT_ID, ZPA_CUSTOMER_ID env
                                        #    + ~/.zscaler_api_key symlink to the mounted secret
on:                                     # ordered; stop on first failure
  - name: Create ZPA groups and keys
    run: python3 scripts/zpa_create.py
  - name: Create PRIV connector group
    run: python3 scripts/zpa_create_priv.py
  - name: Seed provisioning keys into SSM
    run: python3 scripts/put_keys_ssm.py
  - name: Apply infrastructure
    run: tofu -chdir=terraform apply -auto-approve -input=false
  - name: Wait for enrolment
    run: python3 scripts/wait_enrolled.py --timeout 900
off:
  - name: Destroy infrastructure
    run: tofu -chdir=terraform destroy -auto-approve -input=false
status:                                 # optional; must print JSON on stdout
  run: python3 scripts/status.py --json
  interval_s: 60
tags:                                   # inventory grouping + cost attribution
  Project: zpa-pse-lab
```

Engine rules:
- Before any `on`/`off` job: ensure checkout exists and is at `source.ref` (clone or
  fetch+reset), then `tofu -chdir=<dir> init -input=false -backend-config=…` (idempotent).
- `state` is derived: job running → `turning_*`; else `tofu state list` non-empty → `on`;
  empty → `off`; tofu error → `unknown`; last job failed → `error` (until a later success).
- The status probe runs on demand and every `interval_s` while the use case is `on`; its JSON is
  passed through to the UI untouched (the PSE lab probe reports enrolment per component).
- Steps inherit the provider's decrypted credentials via env for the life of the subprocess only.

## Frontend brief (Switchboard)

An operator's switchboard: **lines you plug in, switches you flip.** Dark-first, instrument-panel,
committed — not a generic admin template. One accent, spent on live state (a connected line, a
running use case); everything else quiet. Data in a monospace face; numbers tabular. Must remain
legible in a light theme via `prefers-color-scheme`, but the dark design leads.

**Clouds page.** A patch panel: one jack per provider. AWS is live; GCP and Azure are present but
"not wired yet" (disabled, honest). Plugging in opens a connect form; submit shows the
ConnectionReport as a checklist that fills in one line at a time — that *is* the "does a few
checks and confirms" moment, make it satisfying. Once connected: a region grid, each region a
tile showing instances/VPCs/NAT/EIPs, tiles empty-but-present for regions with nothing, and a
cost panel with the monthly total and the line items. Group by tag (`Project=…`) so a use case's
footprint is visible as a unit. Refresh button, `generated_at` shown, stale state flagged.

**Use cases page.** One card per use case: name, provider chip, summary, a large physical toggle
with a state lamp (off / turning on / on / error), resource count, last run. Opening a card
reveals: the description (rendered markdown), the **procedure** as two ordered lists (on / off)
with each step's live state while a job runs, a terminal-style log that tails, and a **code
drawer** — file tree on the left, file on the right with syntax colouring (a small highlighter
for HCL, Python, YAML, shell is enough; no CDN dependency). Flipping the switch asks for
confirmation with the procedure it is about to run. A running job disables the switch.

Empty states, error states and loading states are designed, not defaulted. No emoji. No
gradients-for-the-sake-of-it. Keyboard focus visible. Works at 1280 and at 1920.

## Definition of done (v1)

- `docker compose up` on the host serves Switchboard on `:8080` behind the password.
- AWS connects with the fresh SSO credentials and shows the five running lab instances, both
  VPCs, the NAT, three EIPs, ~$285/mo — matching `tools/cost.py` in the lab repo within 5%.
- The PSE use case shows **on** immediately (state migrated to S3 before deploy), with the
  status probe reporting three components `ZPN_STATUS_AUTHENTICATED`.
- Flipping it off runs the destroy procedure with a live log; flipping it on rebuilds and waits
  for enrolment. Both verified once, end to end.
- Nothing in `/data`, logs, or any API response contains a credential. Verified by grep.
- `README.md` in the house format; repo indexed on the profile README; every commit pushed.
