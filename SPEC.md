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

---

# v1.2 delta — a real topology drawing for each use case

Feedback from the product owner, verbatim in substance: the use-case card explains the network
in prose — CIDRs, "no peering, no transit gateway", bullet lists of addresses. Nobody can hold
that in their head. It has to be a **network drawing**: graphical, clickable, driven by live
data from the cloud, so an architect understands the lab in seconds. The v1.1 description text
is the wrong medium and is being removed from the card.

## Principle

**Structure comes from the cloud; meaning comes from the manifest.** Every box and containment
edge is derived from the live inventory (VPCs, subnets, route tables, gateways, instances,
addresses, security-group rules). AWS cannot tell you *why* traffic flows, so the handful of
application flows — "the client dials the PSE on 443 via the NAT and the internet" — are
declared in the manifest and drawn as flow edges, visibly distinct from structure. Nothing in
the drawing is hardcoded to this lab; a second use case gets a drawing for free.

## Backend — `GET /api/usecases/{id}/topology`

Built from the provider's cached inventory (refresh with `?refresh=1`), filtered to resources
carrying the use case's `tags` (plus resources reachable only through them: the subnets of a
tagged VPC, the IGW attached to it, EIPs associated to tagged instances/NATs). Provider-neutral
node/edge model:

```
{"generated_at":…, "usecase":"zpa-private-service-edge", "provider":"aws", "region":"eu-central-1",
 "nodes":[
   {"id":"internet","kind":"internet","label":"Internet"},
   {"id":"vpc-…","kind":"vpc","label":"VPC A","cidr":"10.91.0.0/16","parent":null,
    "detail":{…the inventory record…}},
   {"id":"subnet-…","kind":"subnet","label":"zpa-lab-public","cidr":"10.91.10.0/24",
    "parent":"vpc-…","exposure":"public"|"private"|"isolated","az":"eu-central-1a"},
   {"id":"i-…","kind":"instance","label":"zpa-lab-pse","parent":"subnet-…","role":"pse",
    "type":"m5.large","state":"running","private_ip":…,"public_ip":…,"detail":{…}},
   {"id":"nat-…","kind":"nat","label":"NAT","parent":"vpc-…","public_ip":…},
   {"id":"igw-…","kind":"igw","label":"IGW","parent":"vpc-…"},
   {"id":"eipalloc-…","kind":"eip","label":"3.70.113.155","attached_to":"i-…"|"nat-…"|null}
 ],
 "edges":[
   {"kind":"route","from":"subnet-…","to":"igw-…","label":"0.0.0.0/0"},          # default routes
   {"kind":"route","from":"subnet-…","to":"nat-…","label":"0.0.0.0/0"},
   {"kind":"uplink","from":"igw-…","to":"internet"},
   {"kind":"uplink","from":"nat-…","to":"igw-…"},
   {"kind":"allow","from":"<cidr or sg-id>","to":"i-…","label":"tcp/443"},        # SG ingress
   {"kind":"flow","from":"i-…","to":"i-…","via":["nat-…","internet"],"label":"dials :443",
    "declared":true}                                                              # from manifest
 ],
 "enrolment":{"i-…":{"authenticated":true,"label":"Private Service Edge"}, …},  # from status probe
 "unknown":[…resources tagged but not placeable…]}
```
- `role` on an instance comes from the manifest's `topology.roles` map (by Name tag); absent →
  null. `exposure` for a subnet: `public` if its default route is an IGW, `private` if a NAT,
  `isolated` if none.
- Manifest additions (validated, all optional):
  ```yaml
  topology:
    roles: {zpa-lab-pse: pse, zpa-lab-connector: connector, zpa-lab-priv-connector: connector,
            zpa-lab-server: app, zpa-lab-mcu-client: client}
    flows:
      - {from: zpa-lab-mcu-client,    to: zpa-lab-pse, label: "dials :443", via: [nat, internet]}
      - {from: zpa-lab-priv-connector,to: zpa-lab-pse, label: "dials :443", via: [nat, internet]}
      - {from: zpa-lab-connector,     to: zpa-lab-pse, label: "dials :443 (local)"}
      - {from: zpa-lab-priv-connector,to: zpa-lab-server, label: ":8080"}
      - {from: zpa-lab-pse,           to: internet,    label: "control plane :443"}
    blocked:
      - {from: zpa-lab-mcu-client, to: zpa-lab-server, label: "no route"}
  ```
  Names resolve to instance ids at build time; a flow whose endpoint is missing goes to
  `unknown` with a reason rather than failing. `nat`/`internet` in `via` resolve to the NAT of
  the source's VPC and the internet node.
- Cached 60 s; 200 with `nodes: []` and a `reason` when the provider is disconnected or the use
  case is off (the drawing then shows the declared skeleton only — see frontend).

## Frontend — the drawing

Replaces the prose topology in the expanded card (the description keeps only *what it is*,
*what ON/OFF do*, *cost*, *sharing* — cut everything that reads like a network spec).

- **Inline SVG rendered from the graph**, deterministic auto-layout, no library:
  internet node top-centre; each VPC a rounded container in a row beneath it, labelled with name
  and CIDR; subnets as nested containers stacked inside their VPC, labelled with name, CIDR and
  an exposure badge; instances as cards inside their subnet (role glyph, name, type, private IP,
  enrolment lamp from `enrolment`); NAT and IGW as nodes on the VPC's top edge; EIPs as chips on
  the node they're attached to, idle EIPs as chips outside any VPC. Orthogonal edges: routes as
  thin solid lines, uplinks to the internet as solid, **declared flows as dashed amber**, blocked
  pairs as a red struck line. Edge labels on hover and in a legend. The whole thing fits the card
  width at 1280 and grows at 1920; pan/zoom not required, but wide graphs may scroll horizontally.
- **Everything is clickable.** Instance / subnet / VPC / NAT / EIP click → the existing region
  drawer opens deep-linked to that resource (`#/clouds/aws/<region>?inst=` etc.; extend the
  drawer's deep-link params to `?subnet=`, `?vpc=`, `?nat=`, `?eip=` and scroll+flash the item).
  Hover highlights the node, its edges and the nodes at their other ends; a small inspector
  panel beside the SVG shows the hovered/selected node's key facts without leaving the card.
- **Off state:** when the use case is off, draw the declared skeleton (VPCs/subnets/instances
  from the manifest's last known topology are not available — so render the flows/roles as a
  greyed schematic with "not running" and the last-run timestamp) rather than nothing.
- Legend, keyboard: nodes are focusable in DOM order, Enter opens the drawer.
- Mock: a fixture graph for the PSE lab that exercises every node kind, an idle EIP, all five
  flows, the blocked pair, and the off-state schematic; screenshots at 1280 and 1920, dark and
  light.

## Definition of done
The PSE card shows a drawing an architect can read in seconds: two VPC boxes, three subnets,
five instances with lamps, NAT and IGWs, the internet on top, the three dashed flows converging
on the PSE, the red blocked MCU→PRIV pair. Clicking the PSE opens the drawer on it. The drawing
regenerates from AWS on refresh — after an OFF/ON cycle it shows the new ids and addresses with
no manifest change.

---

# v1.3 delta — provider selector on the Use cases page

Use cases will be separated by cloud: AWS today, Google Cloud and Microsoft Azure next.
The Use cases page therefore opens with a **provider selector** and shows only the
selected provider's use cases beneath it. Backend is unchanged: `GET /api/usecases`
already tags each summary with `provider`, and `GET /api/providers` already carries each
provider's connection state. This is a frontend workstream.

## The selector

- A **provider rail** at the top of the Use cases page: one large selectable tile per
  registered provider, in registry order (AWS, Google Cloud, Microsoft Azure), always all
  three even when a provider has no use cases yet. Graphically deliberate, not a tab strip:
  each tile carries the provider's mark (drawn inline as SVG in the v1 design language —
  no logos fetched, no emoji), its name, the connection lamp and state
  (`connected · since …` / `unplugged`), the use-case count, how many are **on**, and the
  running monthly cost of that provider's *on* use cases (sum of the summaries'
  `cost_monthly`-type field if present, else omitted). Selected tile: amber outline,
  raised; others recede. Hover lifts. Transition on selection (fast, ≤150 ms; respect
  `prefers-reduced-motion`).
- Selecting a tile filters the cards below; the card list re-renders with a short fade.
  A provider with zero use cases shows a designed **empty state**: the provider's mark,
  "No use cases for Google Cloud yet", and — if it is unplugged — a link to the Clouds page
  to plug it in; if connected — "Use cases for this cloud arrive as manifests under
  `usecases/`". Never an empty white area.
- **Deep link and memory:** the route becomes `#/usecases/<provider>`; `#/usecases` alone
  resolves to the remembered choice (localStorage `sb.usecases.provider`), else the first
  provider that has at least one use case, else `aws`. An unknown provider id in the URL
  falls back the same way. Existing deep links used by the drawer's return path
  (`#/usecases`) keep working. The drawer's `returnTo` should carry the selected provider.
- **Keyboard:** the rail is a `radiogroup`; tiles are `radio` with roving tabindex,
  Left/Right/Home/End move selection, the count and state are in the accessible name.
- The page header's copy stays; the rail sits between the header and the cards. The
  Refresh button refreshes both the rail's provider states and the cards.
- 1280: three tiles in a row, each ≥ 360 px. Narrower: tiles wrap 2+1, never scroll.

## Mock

`?mock=1` already carries GCP and Azure use cases; keep them and make the rail honest
against the mock provider states (AWS connected, GCP unplugged, Azure connected but with a
use case that is off). Add `&provider=<id>` to preselect.

## Definition of done (v1.3)

- Rail renders all three providers with live connection state and counts; selection
  filters; empty states designed; deep link + localStorage memory; keyboard radiogroup.
- Screenshots at 1280 and 1920, dark and light: AWS selected, GCP empty/unplugged, Azure
  selected, and the keyboard focus ring, into `scratchpad/ui-shots/v13/`.
- No change outside `app/static/`. No external requests. All existing behaviour — card
  expand, topology, outline, drawer return path — unchanged.

---

# v1.4 delta — the off state draws what ON would deploy

Product owner, on the v1.2 off state (a "roles" strip under an Internet node): *"It doesn't
show what would be deployed if someone hits the ON button. The experience between both
should be the same, although clearly to understand what is deployed and what not."*

## Principle

One drawing, two registers. When the use case is on, the graph comes from the cloud
(**deployed**). When it is off, the *same* graph — VPCs, subnets, instances, gateways,
addresses, routes, flows — comes from a real `tofu plan` of the use case (**planned**),
because the plan is the truthful answer to "what happens if I hit ON". Layout, glyphs,
hover, legend and inspector are identical; only the register changes. The v1.2 "declared
topology" roles schematic is retired; it remains only as the last fallback when no plan can
be made (provider disconnected).

## Backend — `GET /api/usecases/{id}/topology` when the use case is off

- **One plan, two consumers.** The ON outline already runs `tofu plan -json`. Unify: a
  single `_plan(manifest, "on")` runs `tofu plan -json -out=<planfile>` (never apply; the
  plan file lives in the checkout dir and is deleted after use), parses the JSON stream for
  the outline as today, and runs `tofu show -json <planfile>` for the graph. One cache
  (60 s, `?refresh=1` re-plans, invalidated when a job ends) serves both endpoints, so the
  outline's resource count and the drawing's are the same number from the same plan.
- **Graph from the plan.** Build the same node/edge vocabulary as v1.2 from
  `planned_values.root_module.resources` (types, names, known attributes: CIDRs, tags,
  instance types, protocols/ports) plus `configuration.root_module.resources[].expressions`
  references for structure (subnet→vpc, instance→subnet, instance→security groups,
  NAT→subnet and →EIP, EIP→instance, route tables→routes→gateway, associations→subnet,
  rule→group and →source). Node `id` is the resource address (`aws_instance.pse`); `label`
  is `tags.Name` else the resource name. Attributes unknown until apply (private/public IPs,
  allocation ids) are `null` — never invented. Subnet `exposure` from the associated route
  table's default route target, as v1.2. Flows/blocked from the manifest resolved by Name
  as today; `enrolment` is `{}`.
- **Type mapping is a table.** `app/usecases/plan_graph.py` is provider-neutral; an AWS
  table maps resource types to kinds (`aws_vpc→vpc`, `aws_subnet→subnet`,
  `aws_instance→instance`, `aws_nat_gateway→nat`, `aws_internet_gateway→igw`,
  `aws_eip→eip`, and the linking types: route tables, routes, associations, security
  groups and rules). Nothing AWS-specific escapes the table and `detail`.
- **Every node carries `source: {path, line}`** — the file (relative to the use-case repo
  root, the same path form `GET /usecases/{id}/code` accepts) and the line where
  `resource "<type>" "<name>"` is declared in `terraform_dir`. Live (deployed) nodes get
  the same field when the address can be matched from state (`tofu state list` ↔ tags), else
  omit it.
- **Response additions:** `register: "deployed" | "planned"`; when planned also
  `plan: {generated_at, resources: <count of create changes>, error?}`. A failed plan
  returns `nodes: []`, `reason`, `register: "planned"`, and the error scrubbed. When the
  provider is disconnected, `nodes: []`, `reason: "Connect <provider> to plan …"`, and
  `declared` (v1.2) for the fallback.
- **Tests:** a hand-built `tofu show -json` fixture modelled on the PSE lab's terraform
  (2 VPCs, 4 subnets with route tables and associations, 5 instances, NAT, 2 IGWs, 3 EIPs,
  security groups with the 443 rules) → same counts and exposures as the live fixture, ids
  are addresses, IPs null, flows resolved through the NAT, blocked pair, `source` lines
  resolved against a temp checkout containing the resource blocks, outline and topology
  share one plan run (assert the runner is invoked once for both). No cloud, no tofu binary
  in tests (monkeypatch the runner).

## Frontend — one drawing, two registers

- **Planned register:** the same `topoLayout`/`topoSvg` path with the planned graph. VPCs,
  subnets and cards keep their exact shapes and positions; the register is expressed by
  dashed container strokes, a subtle diagonal hatch on subnet fills, hollow enrolment lamps,
  and value slots reading `assigned at ON` (mono, muted) where IPs and addresses are null.
  A banner above the drawing replaces "NOT RUNNING": `PLANNED · this is what ON deploys ·
  45 resources · plan 2 min ago` with the last-run note kept at the right. When on, the
  banner reads `DEPLOYED · eu-central-1 · generated 1 min ago` — same bar, two registers.
- **Legend** gains the register pair: `deployed` (solid) / `planned` (dashed, hatched).
- **Inspector** works identically, header `PLANNED · not deployed` or `DEPLOYED`; a
  `source` row `main.tf:112` on every node that has one.
- **Click:** deployed → region drawer (v1.2). Planned → nothing exists in the cloud, so
  click/Enter opens the card's **code view** at `source.path`, scrolled to `source.line`
  with the resource block flashed. The inspector button reads `Open source` in that case.
- **Fallbacks:** plan failed → the error verbatim with Retry; provider disconnected → the
  v1.2 declared schematic, labelled as such.
- **Mock:** `&topo=planned` renders the planned PSE graph (ids as addresses, null IPs,
  `source` lines); `&topo=planfail`; `&topo=disconnected`.

## Definition of done (v1.4)

- Off state shows the full lab as it will be deployed, from a real plan; on state
  unchanged; the two are visibly the same drawing in two registers.
- Outline and topology agree on the resource count from one plan run.
- Tests green, no cloud in tests; screenshots at 1280/1920, dark/light: planned, deployed,
  hover in each, click-through to source, plan-failed, disconnected — `ui-shots/v14/`.

---

# v1.5 delta — a second use case, and routes that end at an appliance

The second use case, **Cloud Connector — AWS workload zero trust** (`zcc-aws-workload`), arrives
as a manifest. Almost everything it needs already exists: the drawing, the two registers, the
outline, the code view and the region drawer are use-case-neutral by construction. Three things
do not exist yet, and they are what this delta adds.

The lab: one workload in a cloud VPC whose **default route points at a Cloud Connector's service
interface**, and a private application in a second, unconnected VPC reached through an App
Connector. Repo `nilsujma-dev/zs-zcc-aws-workload-lab`, tag `Project=zcc-workload-lab`,
`eu-central-1`. The build contract for the lab repo is its own `DECISIONS.md`; this section is
only what Switchboard owes it.

## A. Two new roles

`topology.roles` already accepts any string; the frontend has to know these two.

| role | word | glyph |
|---|---|---|
| `cloud-connector` | Cloud Connector | a shield with an arrow through it — traffic passing through something that inspects it |
| `workload` | Workload | a two-tier server stack |

`ROLE_RANK` gains them (`pse · cloud-connector · connector · app · client · workload`) so
instances still sort deterministically inside a subnet. Nothing else in the drawing changes.

## B. `inspected` — a default route that ends at an appliance, not a gateway

A subnet whose `0.0.0.0/0` points at a **network interface** is not `private` and it is certainly
not `isolated`: its traffic is steered into an appliance that inspects it. That is the whole
point of the use case, so the drawing has to say it.

- **New subnet exposure `inspected`**, alongside `public` / `private` / `isolated`. Badge: amber
  outline, label `INSPECTED`, in the subnet header and in the legend. Ranked between `public` and
  `private` when subnets are ordered inside a VPC.
- **The route resolves to the instance, not the interface.** The subnet node carries
  `exposure: "inspected"` and `default_route: <instance>` (id in the deployed register, resource
  address in the planned one), and a `route` edge runs subnet → instance with
  `inspected: true` and `eni: {id, name, private_ip}` naming the interface it goes through.
  Drawing an ENI as its own node was rejected: it is a port on a box that is already drawn.
- **Neither builder recognises an id format.** `topology.py` resolves the target through the
  inventory's own ENI records; `plan_graph.py` resolves it through the plan's references. An
  interface whose owner is not part of the use case leaves the subnet `inspected` with **no**
  edge and an `unknown` entry saying which instance is missing — never an invented line.

### Live register — `app/providers/aws.py`, `app/usecases/topology.py`
- Inventory gains `regions[].network_interfaces[]`:
  `{id, name, description, vpc, subnet, az, private_ip, instance, device_index, status,
  interface_type, source_dest_check, tags}` from `describe_network_interfaces`. Only the ENI
  knows which instance owns it, and a route target only ever gives the interface's id.
  ENIs are not drawn and do not count towards `region.resource_count`.
- `topology.py` maps `default_route → network_interfaces[].instance` (and accepts a route that
  already names an instance), sets the exposure, and emits the route edge once the instance node
  exists.

### Planned register — `app/usecases/plan_graph.py`
- Two new linking kinds in the AWS table: `aws_network_interface` (`network_interface`) and
  `aws_network_interface_attachment` (`attachment`).
- `network_interface_id` joins `gateway_id` / `nat_gateway_id` as a route target on
  `aws_route_table`, `aws_default_route_table` and `aws_route`, in `TARGET_KINDS`, and therefore
  in the pooled-reference fallback (the AWS provider models `route` as a set attribute, so the
  target that is unknown until apply is identified by the key the planned item *omits*).
- An interface's owner comes from either shape: an `aws_network_interface_attachment` joining
  the two, or `aws_instance`'s inline `network_interface` blocks (`TypeSpec.attach` names the
  reference key and the ordering key).
- **An appliance takes no `subnet_id`.** When an instance references no subnet, it is placed in
  the subnet of its lowest-device-index interface, and its security groups are collected from
  its interfaces as well as from itself — otherwise the Cloud Connector would land in `unknown`
  and its rules would draw no `allow` edges.

## C. Secrets Manager in the cost rollup

The lab keeps the Cloud Connector's deployment credentials in one Secrets Manager secret, which
survives OFF. It is small and it is real, so it is in the estimate.

- Inventory gains `regions[].secrets[]` (`{arn, id, name, description, created, last_changed,
  rotation_enabled, monthly_usd, tags}`); deleted secrets are skipped. Listing them needs its own
  permission, so a failure yields an empty list and never breaks a region scan.
- `pricing.py` gains `SECRET_MONTHLY_USD = 0.40` — a flat per-secret rate, identical in every
  commercial region, so a constant rather than a Pricing API lookup. API calls are usage, not a
  standing rate, and are excluded like NAT data processing (a note says so).
- Cost line `Secrets Manager secret`, unit `secret-mo`, attributed by the secret's own tags — so
  a secret the lab does not tag lands in `untagged`, and the lab repo is expected to tag it
  `Project=zcc-workload-lab`.

## D. The manifest

`usecases/zcc-aws-workload/usecase.yaml`, sibling in structure and tone to the PSE lab's:
source `https://github.com/nilsujma-dev/zs-zcc-aws-workload-lab.git` @ `main`, terraform dir
`terraform`, state key `usecases/zcc-aws-workload/terraform.tfstate`, `tags: {Project:
zcc-workload-lab}`, `secrets: [zscaler_oneapi]`, status probe `python3 scripts/status.py --json`
every 60 s, twelve `on` steps (baseline · preflight · ZPA objects · CC admin, templates and
secret · SSM key · apply · wait for registration · forward to ZPA · ZPA access rule · ZIA URL and
DLP · verify the tenant is unchanged · wait for evidence), one `off` step, an `effects` block,
and a `topology` block with four roles, four flows and one blocked pair. The description keeps
the PSE lab's four headings — what it is, what ON and OFF do, cost (≈ $192/month on at list),
sharing — and no network prose: the drawing carries that.

Two steps run in one shell step (`ztw_create.py && put_cc_secret.py`) because the tenant admin
and the secret that holds its credentials are one idea and the contract names the step once.

## Definition of done (v1.5)

- `GET /api/usecases` lists both AWS use cases; the CC card expands to a drawing with two VPCs,
  five subnets, four instances, the NAT, both IGWs, the workload subnet badged `INSPECTED` and a
  route line from it to the Cloud Connector, the three dashed flows and the red blocked pair.
- Off and on are the same drawing in two registers, and the `inspected` route survives both.
- The cost estimate carries the Secrets Manager line; the region total still equals the sum of
  its lines.
- Tests green with no cloud calls; the CC fixtures cover the inline and standalone route shapes
  and the inline and attachment-resource interface shapes. `?mock=1` carries the CC lab as a
  second AWS use case in both registers, and `&labs=real` trims the mock to the two use cases
  that actually ship (the others exist only to give the v1.3 rail a running and a failed card);
  screenshots at 1280 dark and light in `scratchpad/ui-shots/v15/`.

---

# v1.6 delta — stale entries are pruned, and the card says so

The two labs enrol into a **shared** Zscaler tenant, and every rebuild left a disconnected entry
behind. The lab repos gain a `prune.py` that deletes them; the architecture note
(`prune-architecture.md`, §1 ownership, §2 hooks, §5 safety) is the contract. Switchboard owes it
three things: the steps in the manifests, the counts in the status probe, and card text that no
longer says pruning is manual. **No API and no schema change** — steps are `name` + `run`, and the
status probe's JSON already passes through untouched.

## A. The status probe — `stale` and `keys`

`status.py --json` gains two optional keys. Everything else in the probe is unchanged, and a probe
without them renders exactly as before.

```json
"stale": {"count": 3, "connectors": 1, "service_edges": 0, "cc_vms": 1, "cc_groups": 0,
          "locations": 1, "last_prune": "2026-09-06T18:02:11Z", "last_prune_deleted": 4},
"keys": [{"type": "connector", "name": "AWS-Lab ZCC CONNECTOR_GRP key v1", "usage": "2/200", "current": true}]
```

- `stale` is **flat counts**, never nested per-object detail: the per-entry lines live in the job
  log (`prune.py --json`), which is where a decision about one entry belongs. `count` is the total;
  a kind the lab does not have is `0` (the PSE lab has no `cc_*` or `locations`). `last_prune` /
  `last_prune_deleted` describe the last run that actually deleted something.
- `keys` is a **list of records** (`type`, `name`, `usage` as `used/max`, `current`), so
  `renderProbe`'s existing "array of records → table" rule already has the right shape; the current
  key of each type is flagged rather than ordered first, because the list is short and the order is
  the tenant's.
- `summary` gains `, N stale entries` when `N > 0`. `healthy` is **not** affected by a stale count:
  a stale entry is bookkeeping, not a broken lab.

## B. Frontend — one line and one table

In the status-probe block, above the key/value grid:

- **The stale line.** `stale entries: 3 connectors · 1 service edge · 1 CC group`, kinds in a fixed
  order (connectors, service edges, CC VMs, CC groups, locations), singular and plural spelled out,
  **zero kinds omitted**, and `none` when everything is zero. A **lamp: amber when the total is
  > 0**, unlit when it is zero — the same lamp vocabulary the rest of the panel uses, so "there is
  something left over" reads at a glance without turning the card red. `last prune <time> · N
  deleted` follows in dim text when the probe carries it.
- **The keys table.** `type · name · usage`, small and mono, the current key marked with a
  `current` chip. It is rendered by the same table code as any other array of records, given its
  own column order and the chip; `keys` and `stale` are taken out of the generic walk so they
  cannot also appear as flattened `stale.count` items.

The topology inspector is **unchanged**: `stale` is use-case bookkeeping, not a property of a node,
and a component whose probe carries it maps to its instance exactly as before (`_parse_components`
ignores keys it does not know).

## C. Manifests

Steps (`name` ≤ 40 chars, `name` + `run` only), placed where the architecture note puts the hooks —
OFF after the destroy, ON after the create scripts and before the SSM seed and `tofu apply`, and
for the CC lab once more after `ZIA URL and DLP policy`, because the superseded ZIA location is
only unreferenced after the rules have been re-scoped:

```yaml
# zpa-private-service-edge          # zcc-aws-workload
"on":                               "on":
  … Create PRIV connector group       … Create CC admin, templates and secret
  - name: Prune stale entries         - name: Prune stale entries
    run: python3 scripts/prune.py --phase on-pre --apply
  … Seed provisioning keys into SSM   … ZIA URL and DLP policy
                                      - name: Prune superseded CC group and location
                                        run: python3 scripts/prune.py --phase on-post --apply
"off":                              "off":
  - name: Destroy infrastructure      - name: Destroy infrastructure
  - name: Prune stale entries         - name: Prune stale entries
    run: python3 scripts/prune.py --phase off --apply
```

`--apply` is explicit in the manifest because `prune.py` is dry-run by default; the phase names the
hook, so one script serves all three placements and the log line says which one ran.

## D. Card text

The declared `effects` are what the confirm dialog reads out, so they carry the change:

- ON `creates` gains "Nothing accumulates: stale entries from earlier rebuilds are pruned before
  apply" (the CC lab's line also names the superseded group and location after the re-scope).
- OFF `destroys` gains "stale disconnected entries in the lab's own groups are deleted" — and for
  the CC lab "one ZIA location survives until the next ON".
- OFF `retains` no longer says entries "accumulate one per rebuild" or that pruning is "deliberately
  manual". What it retains now is what the prune deliberately keeps: an entry it could not prove was
  the lab's own, and the CC lab's current ZIA location with its empty group.
- The descriptions lose "leaves a stale, disconnected entry" and gain the prune step in the numbered
  ON list.

## Definition of done (v1.6)

- Both manifests load and `GET /api/usecases/{id}` lists the new steps in the contracted order:
  six ON / two OFF for the PSE lab, fourteen ON / two OFF for the CC lab.
- A probe carrying `stale` and `keys` renders the stale line (amber lamp when > 0, `none` when all
  zero) and the keys table, and still maps its components onto instances in the drawing.
- Tests green with no cloud calls, and no deploy from this change; screenshots of both cards'
  probe blocks at 1280 dark in `scratchpad/ui-shots/v16/`.
