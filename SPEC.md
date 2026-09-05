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

---

# v1.1 delta — region drawer, procedure outline, credentials for every cloud

Three changes requested after v1 went live. Everything in v1 stands unless amended here.

## A. Region drawer (Clouds page)

Clicking a region tile that has resources opens a **drawer from the right edge** (not the
full-width table below the grid, which is removed). The drawer is the detailed inventory for
that region: every resource, with as much detail as the API gives us. Regions with nothing are
not clickable.

### Backend — richer inventory
`GET /api/providers/aws/inventory` grows. Per region, in addition to v1 fields:

```
instances[]: + ami, ami_name, az, platform, architecture, iam_instance_profile,
             key_name, security_groups:[{id,name}], subnet, vpc, root_device,
             monitoring, ebs_optimized, volumes:[volume_id...], launched, uptime_h,
             monthly_usd (this instance's compute line), user_data_present: bool
volumes[]:   + az, iops, throughput, encrypted, attached_to (instance id|null), device,
             created, monthly_usd
vpcs[]:      + subnets:[{id,name,cidr,az,public:bool,route_table}], igw, nat_gateways:[ids],
             route_tables:[{id,name,routes:[{dest,target}],subnets:[ids]}], dns_hostnames
nat_gateways[]: + subnet, private_ip, connectivity_type, created, monthly_usd
eips[]:      + allocation_id, association:{instance|nat|eni}|null, monthly_usd
security_groups[]: NEW — {id,name,vpc,description,ingress:[{proto,from,to,source}],
                          egress:[...], attached_to:[instance ids]}
region.monthly_usd, region.resource_count
```
Every monetary field derives from the same Pricing lookups as `cost.lines`; the region total
must equal the sum of its lines. Extra describe calls run inside the existing per-region thread.
Keep the 10-minute cache. Do not add new API endpoints for this — it is the same inventory.

### Frontend — the drawer
- Slides in from the right, ~640px at 1280 / ~760px at 1920, backdrop dims the page, Esc and
  the backdrop close it, focus is trapped inside while open, the trigger tile regains focus on
  close. URL hash carries `#/clouds/aws/eu-central-1` so a drawer is deep-linkable and survives
  refresh.
- Header: region, monthly cost for the region, resource count, `generated_at`.
- Body: collapsible sections **Instances · VPCs & subnets · NAT gateways · Elastic IPs ·
  Volumes · Security groups**, each with a count in the heading, expanded by default only when
  non-empty. Instances render as cards (name, id, type, state lamp, AZ, private/public IP,
  launched + uptime, monthly cost) that expand to the full attribute set as a definition list,
  with attached volumes and security groups linked to their sections. VPCs show subnets as a
  table with their route table's default route (`0.0.0.0/0 → igw|nat|none`) so public vs
  private is visible at a glance. Security groups show rules as a table.
- Group by `Project` tag is preserved as a filter chip row at the top of the drawer.
- A copy control on every id. Tags shown as chips. Idle EIPs and unattached volumes flagged.
- Empty section states designed, not omitted.

## B. Procedure outline (Use cases page)

Before anything runs, the operator sees exactly what ON or OFF will do — from a real plan, plus
what the manifest declares happens outside OpenTofu.

### Manifest — declared effects
```yaml
effects:
  "on":
    creates:                      # outside terraform, in prose; the tofu plan supplies the rest
      - "ZPA Service Edge Group, App Connector Groups and provisioning keys — reused by name if present"
      - "Three SSM SecureString parameters under /zpa-lab/ holding the provisioning keys"
    retains: []
  "off":
    destroys:
      - "Everything OpenTofu manages in this use case (see plan)"
    retains:
      - "ZPA groups and provisioning keys — deleting them is deliberately manual"
      - "SSM parameters under /zpa-lab/"
      - "The S3 state object (versioned) and the remote lock"
      - "Enrolled Service Edge / App Connector entries in ZPA, which show as disconnected"
```
`manifest.py` validates this block; both keys optional, lists of strings.

### Backend — plan endpoint
```
GET /api/usecases/{id}/outline?action=on|off        (cached 60 s per action; 409 if a job runs)
  -> {"action":"on",
      "plan": {"ok":true, "generated_at":"…",
               "create":[{"address":"aws_instance.pse","type":"aws_instance","name":"pse"}],
               "update":[…], "destroy":[…], "unchanged":[…],
               "summary":{"create":N,"update":N,"destroy":N,"unchanged":N}}
              | {"ok":false,"error":"…"},
      "declared": {…the manifest effects block for this action…},
      "steps": [{"name":…,"run":…}],
      "retained_state": {"backend":"s3","bucket":…,"key":…}}
```
`on` runs `tofu plan -json -input=false`; `off` runs `tofu plan -destroy -json -input=false`.
Parse the JSON stream's `planned_change` records (`change.action`) and the final
`change_summary`. Resources are grouped by `type` in the response order they arrive. A plan
against a use case that is already **on** with no drift yields `create:[]` and
`unchanged:[all]` — that is the correct answer, not an error. Run it in the checkout with the
same env as a step. Never `apply`.

### Frontend
- The expanded card gets an **Outline** section with two columns, ON and OFF, each loading its
  plan on expand (spinner, then content; a failed plan shows the error verbatim and still
  shows the declared effects and steps).
- Each column: **Steps** (numbered, as now) · **Generated / Destroyed** — the plan's resources
  grouped by type with counts, e.g. `aws_instance × 5`, expandable to the addresses · **Already
  present / Unchanged** count · **Outside OpenTofu** — the declared `creates`/`destroys` · **Kept**
  — the declared `retains` plus the remote state line. For ON when the use case is on and the
  plan is empty, the column reads *"Nothing to generate — 49 resources already present."*
- The **confirmation modal** shows the same outline for the chosen action, not just the step
  list, with a summary sentence at the top: *"OFF will destroy 49 AWS resources across 2 VPCs and
  keep 4 things outside OpenTofu."* The confirm button is disabled until the plan has loaded.

## C. Credentials for every cloud, rotatable

- **AWS:** a connected jack shows **Rotate credentials** (as well as Disconnect). It opens the
  same connect form; submitting runs the full checklist and, on success, replaces the stored
  credentials atomically — inventory and use cases keep working throughout. On failure the old
  credentials remain. `POST /api/providers/aws/connect` already does this; the UI just needs to
  offer it while connected. Show "credentials updated <time>" on the jack.
- **GCP and Azure become real providers** at the *connect* level: the jack's **Plug in** opens
  a credential form, the backend validates and stores, the jack shows connected identity.
  Inventory and cost for them return `{"supported": false, "reason": "…"}` and the UI shows a
  designed "inventory not built for this provider yet" state — connected, honest, not disabled.
  - `providers/gcp.py`: form = service-account JSON (pasted or uploaded) + optional project id.
    Checks: JSON parses and is a service account; token obtainable; project resolvable
    (`cloudresourcemanager.projects.get`); Compute API enabled (`compute.regions.list` on the
    project, or a clear "API not enabled" detail). Identity: `client_email`, `project_id`.
    Deps: `google-auth`, `google-api-python-client`.
  - `providers/azure.py`: form = tenant id, client id, client secret, optional subscription id.
    Checks: token from `ClientSecretCredential`; subscriptions listable; the chosen (or only)
    subscription readable; Resource Manager reachable. Identity: tenant, subscription name +
    id, client id. Deps: `azure-identity`, `azure-mgmt-resource`.
  - Registry: `{"aws","gcp","azure"}`. `GET /api/providers` returns all three with
    `capabilities: {"inventory": bool, "usecases": bool}` so the UI can be honest per provider.
  - The provider form shape is described by the backend: `GET /api/providers/{id}/form`
    → `{"fields":[{"name","label","type":"text|password|textarea|file","required","help"}]}`.
    The UI renders forms from this; no provider-specific form code in the frontend.
- Use cases whose `provider` has `capabilities.usecases == false` show why the switch is
  unavailable, as now.

## Definition of done (v1.1)
- Clicking `eu-central-1` opens the drawer showing all five instances with full attributes,
  both VPCs with subnets and default routes, the NAT, the EIPs with association, volumes, and
  the security groups with rules; region cost equals the sum of its lines.
- The PSE card's Outline shows ON = *nothing to generate, 49 present* and OFF = *destroy 49*,
  grouped by type, with the four declared retentions; the OFF modal's summary sentence is
  correct. No apply was run to produce this.
- AWS: Rotate credentials with the same keys succeeds and nothing is interrupted; with a bad
  secret it fails and the old credentials still work.
- GCP and Azure: Plug in opens a rendered form; submitting empty fields is rejected client-side
  with the field's help text; a fake credential fails the checklist with a clear detail.
- Tests updated; `?mock=1` covers the drawer, the outline (both actions, plus a failed plan), and
  all three provider forms.
