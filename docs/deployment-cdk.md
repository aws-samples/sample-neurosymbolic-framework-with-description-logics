# AWS Deployment Architecture (CDK)

Deploying the neurosymbolic-DL app to AWS. Goal was serverless; the honest
verdict is **mostly-serverless hybrid** — the interactive server and the build
job do not fit Lambda, and forcing them to would be more work and worse
operationally. This doc explains why, gives the recommended architecture, and
breaks the work into ordered tasks.

## Decisions locked (2026-09-23)

- **Split architecture** — web service (Fargate) + separate on-demand build
  task (Fargate RunTask). Task 6 is therefore **mandatory**, not optional.
- **External AWS account** → CDK library is plain **`aws-cdk-lib`** (not
  `@amzn/pipelines`); CI via CDK Pipelines / CodePipeline or GitLab CI.
- **Behind auth** — the app is not public. Cognito (CloudFront/ALB OIDC) moves
  into the **core build**, not Task 8 hardening.
- **Resolved (2026-09-23):** `kiro-cli` CAN be containerized. It ships a Linux
  build (`curl -fsSL https://cli.kiro.dev/install | bash`) and a headless mode
  that authenticates from a `KIRO_API_KEY` env var — no browser, no expiring SSO
  session — so it fits a Fargate task cleanly. `Dockerfile.builder` installs it;
  the ECS task def injects `KIRO_API_KEY` from Secrets Manager at runtime.

## What the app actually is (drives everything below)

Two workloads with opposite shapes:

| | **Chat / serve** (`ontology_qa_cli.webapp`) | **Builder** (`ontology-qa-build`) |
|---|---|---|
| Trigger | User HTTP requests | On-demand ("build this ontology") |
| Duration | Long-lived; SSE streams per turn | Minutes → tens of minutes (≤20 iterations) |
| State | **In-memory** `OntologyAnswerer` + `ChatSession` cache per process | Writes `runs/` artifacts |
| Heavy step | Loads a large OWL into an owlready2 **SQLite backend once**, reused across turns | Spawns query programs in parallel subprocesses |
| External deps | **Bedrock** (`claude-opus` in `us-east-1`) for follow-up rewrite + evolved program | Bedrock (judge) **and `kiro-cli`** (a local CLI binary invoked via subprocess in `fixer.py`) |

### Why not pure serverless (Lambda)

- **Chat server is stateful.** It caches loaded answerers and conversations in
  process memory (`webapp.py`: `answerers`, `sessions` dicts). Lambda's
  per-invocation model would reload the multi-second ontology on every cold
  start and cannot hold a conversation across turns without an external store.
- **SSE streaming.** `/api/chat/stream` and `/api/stream/{job_id}` hold an open
  event stream. Lambda response streaming exists but is awkward for
  long/interactive SSE; a container behind an ALB is the natural fit.
- **The builder needs `kiro-cli`.** `fixer.py` shells out to the `kiro-cli`
  binary. That is not present in a Lambda runtime and is not something to
  reproduce there. It also runs far longer than practical for Lambda and wants
  a real filesystem + parallel subprocesses.
- **SQLite on-disk cache.** The answerer opens `*.sqlite3` next to the OWL file
  and needs a writable, low-latency local filesystem — an EFS/ephemeral volume
  on a container, not a Lambda `/tmp` you rebuild each cold start.

### What IS serverless-friendly (and stays that way)

- The **React frontend** (`frontend/dist`) → S3 + CloudFront (fully serverless).
- **Ontology source + build artifacts** → S3 (the code already supports
  `s3://` OWL URIs via `s3_cache.py`).
- The **build job** → Fargate **task** launched on demand (serverless compute,
  no idle cost) — not Lambda, but no servers to manage.

## Recommended architecture

```
                       ┌──────────────────────────────────────────┐
   Browser ──HTTPS──▶  │ CloudFront                                 │
                       │  ├─ /            → S3 (React static build) │
                       │  └─ /api/*       → ALB → ECS Fargate (svc) │
                       └──────────────────────────────────────────┘
                                              │
                    ┌─────────────────────────┴───────────────────────┐
                    │ ECS Fargate service: "web"                        │
                    │  - FastAPI (uvicorn) in a container               │
                    │  - holds OntologyAnswerer/ChatSession in memory   │
                    │  - EFS mount for runs/ + *.sqlite3 cache          │
                    │  - IAM: bedrock:InvokeModel, s3:GetObject         │
                    └───────────────┬───────────────────────────────────┘
                                    │ RunTask (on "build" request)
                                    ▼
                    ┌───────────────────────────────────────────────────┐
                    │ ECS Fargate task: "builder" (on-demand, exits)     │
                    │  - ontology-qa-build <name> --plain                │
                    │  - kiro-cli baked into the image                   │
                    │  - writes runs/ to EFS (shared) or S3              │
                    │  - IAM: bedrock:InvokeModel, s3:*Object            │
                    └───────────────────────────────────────────────────┘

   Shared: S3 (frontend, ontologies, optional run artifacts) · EFS (runs/ + sqlite cache)
           · CloudWatch Logs · ECR (2 images) · Secrets/SSM for config
```

### Key decisions

| Decision | Why |
|---|---|
| ECS **Fargate** for the web server, not Lambda | Stateful in-memory caches + long-lived SSE + reused SQLite backend |
| Fargate **RunTask** for the builder, not Lambda | Long runtime, parallel subprocesses, needs `kiro-cli` binary |
| **EFS** shared volume | `runs/` and `*.sqlite3` must be visible to both the web service (reads best program) and the builder (writes it), and survive task restarts |
| **S3 + CloudFront** for the SPA | Cheapest, fully serverless static hosting; `/api/*` behavior routed to the ALB |
| **Bedrock** stays in `us-east-1` | Code hardcodes region/model; keep it or parameterize (see Task 2) |
| Rework the web→build handoff | Today `webapp.py` does `subprocess.Popen([... builder ...])` **in the same container**. In the split model the web service must instead call ECS `RunTask` and poll task status. This is the one non-trivial code change. |

### Alternative if you want it simpler / cheaper first

- **Single Fargate service, no split**: run the current `webapp.py` unchanged
  (it spawns the builder as a subprocess inside its own container). One image,
  one service, EFS for state. Downside: a heavy build competes with chat for
  the container's CPU/memory, and scaling the web tier scales the builder with
  it. Good enough for a demo / low traffic; Task 6 is then optional.
- **App Runner instead of ALB+ECS**: even less to manage for the web tier, but
  no EFS support and less control — you'd need S3-backed state instead of EFS,
  which is a bigger `webapp.py` change. Not recommended given the SQLite cache.

## Cost sketch (low traffic, us-east-1, order-of-magnitude)

- Fargate web service, 1 task 0.5 vCPU/1 GB, always-on: ~$15–20/mo.
- Fargate build tasks: pay per run only (a 10-min build ≈ a few cents of compute).
- ALB: ~$16/mo base. CloudFront + S3: ~$1–5/mo at low volume. EFS: a few $/mo.
- **Bedrock (Opus) dominates variable cost** — it's per-token on every chat turn
  and every build iteration. This is the number to watch, not the infra.
- Scale-to-zero option: put the web service behind a schedule or use App Runner
  if idle cost matters more than the SQLite/EFS convenience.

---

## Tasks

Ordered so each builds on the last. Tasks 1–5 get a working single-service
deploy; 6 adds the web/builder split; 7–8 harden it.

### Task 0 — Scope decided ✅ (2026-09-23)
- [x] Target: **split** (web service + separate build task).
- [x] Account: **external AWS**, **app behind auth** (Cognito).
- [ ] Confirm region for Bedrock Opus availability (default us-east-1) + account owner.
- [x] **`kiro-cli` confirmed containerizable** (2026-09-23) — Linux install via
      the official script + headless `KIRO_API_KEY` auth. Installed in
      `Dockerfile.builder`; the ECS task def supplies `KIRO_API_KEY` from a
      Secrets Manager secret.

### Task 1 — Containerize the app
- [ ] `Dockerfile.web`: Python 3.12 base, `uv sync --extra api`, copy source,
      run `uvicorn ontology_qa_cli.webapp:app`. Bind `0.0.0.0`, honor `$PORT`.
- [x] `Dockerfile.builder`: same base + **`kiro-cli` installed** (official Linux
      installer), headless auth via `KIRO_API_KEY` (injected at runtime, not
      baked in), entrypoint `python -m ontology_qa_cli.builder`. (Skip if
      single-service v1 — the web image already spawns the builder.)
- [ ] Build the React app in a multi-stage layer (or in CI) → `frontend/dist`.
- [ ] Local smoke test: `docker run` the web image, hit `/api/ontologies`.

### Task 2 — Make runtime config environment-driven
- [x] Parameterize the Bedrock **region/model** — done. `ontology_qa_cli/llm_config.py`
      reads `ONTOLOGY_QA_BEDROCK_REGION` and `ONTOLOGY_QA_BEDROCK_MODEL`, defaulting
      to the previously-hardcoded `us-east-1` / `us.anthropic.claude-opus-4-6-v1`
      (no-op when unset). Wired into `chat.py`, `builder.py`, and `judge.py`. The
      CDK compute stack sets these exact env vars (`ONTOLOGY_QA_BEDROCK_REGION` /
      `ONTOLOGY_QA_BEDROCK_MODEL`) on both the web and builder task defs, sourced
      from the `bedrockRegion` / `bedrockModel` app-context keys (same defaults).
- [ ] Ensure ontology paths use `s3://` URIs (already supported by
      `s3_cache.py`) and point `ONTOLOGY_QA_CACHE_DIR` at the EFS mount.
- [ ] Confirm `ontology_config.yaml` is baked into the image or read from S3.

### Task 3 — Scaffold the CDK app
- [ ] `cdk/` app (TypeScript recommended; Python also fine given the team).
      Internal Amazon builders: use `@amzn/pipelines` CDK constructs; external
      AWS: plain `aws-cdk-lib`.
- [ ] Stacks: `NetworkStack` (VPC, subnets, SGs), `StorageStack` (S3 buckets,
      EFS), `ComputeStack` (ECS cluster, task defs, service, ALB),
      `EdgeStack` (CloudFront + S3 SPA origin + `/api/*` → ALB behavior).
      NOTE: the frontend SPA bucket lives in `EdgeStack`, not `StorageStack` —
      CloudFront's OAC mutates the bucket policy to reference the distribution,
      so co-locating the bucket and distribution avoids a `StorageStack`↔
      `EdgeStack` dependency cycle that otherwise blocks `cdk synth`.
- [ ] Wire ECR repositories for the image(s); `cdk deploy` should reference them.

### Task 4 — Storage + networking
- [ ] S3 buckets: `frontend` (SPA), `ontologies` (OWL source), optional
      `artifacts` (runs/). Block public access; CloudFront OAC for the SPA bucket.
- [ ] EFS filesystem + access point; mount target in the VPC private subnets.
- [ ] Security groups: ALB→service on the app port; service/task→EFS 2049;
      egress to Bedrock + S3 (Gateway/Interface VPC endpoints to avoid NAT cost).

### Task 5 — Web service + edge
- [ ] ECS Fargate service running `Dockerfile.web`, EFS mounted at the cache /
      runs path, IAM task role with `bedrock:InvokeModel` + `s3:GetObject`.
- [ ] ALB with health check on `/api/ontologies` (cheap, no Bedrock call).
- [ ] CloudFront: default origin S3 (SPA), `/api/*` behavior → ALB, SSE-friendly
      (disable response buffering / caching on `/api/*`).
- [ ] Deploy SPA build to the frontend bucket (CDK `BucketDeployment` or CI step).
- [ ] End-to-end test: load the site, select an ontology, get an answer.

### Task 6 — Web → builder split ✅ (2026-09-23)
Implemented as a **pluggable build backend** (`ontology_qa_cli/build_backend.py`)
selected by the `BUILD_BACKEND` env var — `subprocess` (default, local dev /
single container) or `ecs` (Fargate RunTask). `webapp.py` is now backend-agnostic
(`backend.start/poll/log_tail/stream_lines`); the original subprocess behavior is
unchanged and all existing tests stay green. New tests in `tests/test_build_backend.py`.
- [x] `subprocess.Popen(...)` extracted to `SubprocessBuildBackend`.
- [x] `EcsBuildBackend`: `ecs.run_task` (ontology as container command override),
      `ecs.describe_tasks` for status, CloudWatch `get_log_events` for the SSE log tail.
- [x] IAM already granted in `compute-stack.ts` (`ecs:RunTask`, `DescribeTasks`, `PassRole`).
- [ ] Set the ECS backend env vars on the web task definition (below) and flip
      `BUILD_BACKEND=ecs`. Wire `BUILD_ECS_*` from CDK outputs in `compute-stack.ts`.

**ECS backend env vars** (set on the web Fargate task def):
```
BUILD_BACKEND=ecs
BUILD_ECS_CLUSTER=<cluster name/arn>
BUILD_ECS_TASK_DEFINITION=<builder task def family:revision or arn>
BUILD_ECS_CONTAINER_NAME=builder
BUILD_ECS_SUBNETS=<private subnet ids, comma-separated>
BUILD_ECS_SECURITY_GROUPS=<sg ids, comma-separated>
BUILD_ECS_LOG_GROUP=<builder awslogs group>
BUILD_ECS_LOG_STREAM_PREFIX=builder
ONTOLOGY_RUNS_DIR=/mnt/runs      # shared EFS mount
```

### Task 7 — CI/CD pipeline
- [ ] Pipeline: on merge to `main`, build both images → push to ECR, build SPA,
      `cdk deploy`. Internal: Amazon Pipelines; external: CDK Pipelines /
      CodePipeline or GitLab CI (repo is on `gitlab.aws.dev`).
- [ ] Gate on the test suite (now ~1.6s — the `conftest.py` fix makes CI cheap).

### Task 8 — Hardening & ops
- [x] **ALB ingress locked to CloudFront.** `NetworkStack` no longer opens the
      ALB security group to `0.0.0.0/0`. It resolves the AWS-managed
      `com.amazonaws.global.cloudfront.origin-facing` prefix list at deploy time
      (via a `DescribeManagedPrefixLists` custom resource, since the id is
      region-specific) and scopes ALB ingress to it on ports 80 and 443 — so the
      internet-facing ALB is reachable only from CloudFront edge, not directly.
- [x] Bedrock **cost guardrails** ✅ (2026-09-23): new `MonitoringStack`
      (`cdk/lib/monitoring-stack.ts`, pinned to `us-east-1`) with an SNS alert
      topic and two alarms — (1) a daily Bedrock **token cap** on
      `AWS/Bedrock` `InputTokenCount`+`OutputTokenCount` for the app's `ModelId`
      (the real cost driver), and (2) a coarse `AWS/Billing` `EstimatedCharges`
      dollar backstop. Configure via context: `-c alertEmail=you@amazon.com`
      `-c dailyTokenCap=<tokens>` `-c monthlyUsdCap=<usd>` (`monthlyUsdCap=0`
      skips the billing alarm). `ModelId` tracks the same `bedrockModel`
      default as Task 2's `llm_config.py`.
- [x] Authentication in front of the app (Cognito). Identity now lives in a
      dedicated **`AuthStack`** (user pool + app client + hosted-UI domain),
      consumed by an **ALB HTTPS (443) listener** in `ComputeStack` that runs an
      `authenticate-cognito` action before forwarding to the web service —
      unauthenticated `/api/*` is bounced to the Cognito hosted UI. Enabled by
      passing an ACM cert ARN (`-c albCertificateArn=...`, same region as the
      ALB) + `-c appDomain=https://<cloudfront-domain>` for the OAuth callback;
      without the cert the ALB keeps a plain HTTP listener and CloudFront
      terminates TLS at the edge. `webapp.py` warns it is "not for hostile
      multi-tenant use," so the Cognito gate is the front door.
- [x] Bedrock **cost kill-switch** ✅ (2026-09-23): opt-in Lambda (also in
      `MonitoringStack`) subscribed to the alert topic. When the daily
      token-cap alarm enters `ALARM` it scales the web ECS service to
      `desiredCount=0` — a real spend STOP, not just a notification. It ignores
      every other topic message (billing alarm, OK transitions). OFF by default;
      enable with `-c killSwitch=true` and supply the target service:
      `-c killSwitchCluster=<name> -c killSwitchService=<name>`
      (`killSwitchRegion` defaults to the app region where Compute lives —
      the Compute stack prints `ClusterName` and the web service's generated
      name as outputs). IAM is least-privilege: `ecs:UpdateService` +
      `ecs:DescribeServices` on that one service ARN only. Restore service after
      a kill with `aws ecs update-service --desired-count 1`.
- [~] Autoscaling policy for the web service **(done)**; concurrency limit on
      build tasks **(still open)**. Web service target-tracks on CPU (60%) and
      ALB request-count-per-target (200/target), bounds 1–4 tasks
      (`-c webMinCapacity=` / `-c webMaxCapacity=`). Scale-in cooldown is a
      deliberate 5 min (vs 1 min out) because web tasks hold in-memory
      ChatSession state and serve long-lived SSE streams — a fast scale-in would
      drop active sessions. Build-task concurrency capping is separate (Task 6
      run_task path) and not yet done.
- [x] Backups/lifecycle for EFS `runs/` and S3 artifacts; log retention.
      **EFS:** `enableAutomaticBackups` (AWS Backup daily plan, 35-day retention
      on the default vault). **S3:** all buckets abort incomplete multipart
      uploads after 7d; `artifactBucket` (versioned) transitions noncurrent
      versions to IA after 30d and expires them after 90d. **Log retention:**
      already set — ECS awslogs use `ONE_MONTH` in `compute-stack.ts`. Source OWL
      (`ontologyBucket`) and the SPA (`frontendBucket`) keep current objects.
- [x] Alarms: ALB 5xx, task crash loops, EFS burst-credit exhaustion. Added to
      `MonitoringStack` (same alert topic as the cost alarms): **ALB 5xx**
      (`HTTPCode_ELB_5XX_Count` > 10/5min), **ECS crash loop**
      (`RunningTaskCount` < 1 avg for 15min, needs Container Insights — already
      on), **EFS burst credit** (`BurstCreditBalance` low; bursting mode only).
      Config-driven by name/id with an explicit `opsResourceRegion` (app region)
      so the us-east-1-pinned stack reads app-region metrics; ALB name + EFS id
      wired from live constructs, ECS cluster/service reuse the killSwitch keys.

## Open questions for you
1. **Split or single-service for v1?** (Task 0) — single is faster to ship; split
   is the right long-term shape.
2. ~~Is `kiro-cli` installable in a container image?~~ **RESOLVED (2026-09-23):
   yes** — Linux installer + headless `KIRO_API_KEY` auth; installed in
   `Dockerfile.builder`, key injected from Secrets Manager at runtime.
3. Should the deployed app be **public** or behind auth? Changes Task 8 materially.
4. Internal Amazon account (Pipelines/Isengard) or external AWS account? Changes
   the CDK construct library and CI in Tasks 3 and 7.
