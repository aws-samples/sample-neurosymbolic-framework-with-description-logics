# CDK deployment (split architecture, external AWS, behind auth)

Scaffolds the AWS deployment described in [`../docs/deployment-cdk.md`](../docs/deployment-cdk.md).

**Status: deployed and live, behind Cognito auth.** The stacks are deployed to
the target account (us-east-1). Both the web UI and the MCP endpoint are gated
by **app-level Cognito JWT auth** (see [Authentication](#authentication) below);
the browser SPA logs in through the Cognito hosted UI.

## Stacks

| Stack | Contents |
|---|---|
| `NeurosymbolicDL-Network` | VPC (public + private-egress subnets), security groups, S3 gateway endpoint |
| `NeurosymbolicDL-Storage` | S3 buckets (frontend/ontologies/artifacts), EFS filesystem + access point |
| `NeurosymbolicDL-Compute` | ECS Fargate cluster, web service behind an ALB, on-demand builder task def, IAM (Bedrock/S3/RunTask) |
| `NeurosymbolicDL-Auth` | Cognito user pool, confidential app client, **public SPA app client (PKCE)**, hosted-UI domain |
| `NeurosymbolicDL-Edge` | CloudFront (S3 SPA + `/api/*` → ALB) |

## First-time setup

```bash
cd cdk
npm install
npx cdk bootstrap            # once per account/region
npx cdk synth                # verify it compiles/synthesizes
```

## Deploy

```bash
# Region must have Bedrock Opus available (default us-east-1).
npx cdk deploy --all -c region=us-east-1
```

### Frontend (SPA) deploy

The frontend `dist/` is **not** uploaded by CDK (no `BucketDeployment`); build
it and sync to the Edge frontend bucket, then invalidate CloudFront. The build
needs the Cognito config so the SPA can run the hosted-UI login (values come
from the `NeurosymbolicDL-Auth` outputs and the CloudFront domain):

```bash
cd frontend
VITE_COGNITO_DOMAIN="https://neurosymbolic-dl-<ACCOUNT>.auth.us-east-1.amazoncognito.com" \
VITE_COGNITO_CLIENT_ID="<SpaClientId output>" \
VITE_COGNITO_REDIRECT="https://<DistributionDomain>/" \
  npm run build
aws s3 sync dist/ "s3://<FrontendBucketName output>/" --delete
aws cloudfront create-invalidation --distribution-id <DistId> --paths '/*'
```

When the `VITE_COGNITO_*` vars are omitted the SPA builds with auth **disabled**
(for local `npm run dev` against an open backend).

## Authentication

Both surfaces are gated at the **application layer** (not the ALB), so no ACM
certificate is required — CloudFront terminates TLS at the edge and the ALB
stays plain HTTP behind it.

- **Backend** (`ontology_qa_cli/auth.py`): a FastAPI middleware verifies a
  Cognito-issued JWT (RS256 against the pool JWKS; checks `exp`/`nbf`, `iss`,
  `token_use`, and audience) on every request. Enabled by the
  `COGNITO_USER_POOL_ID` env var, which `compute-stack.ts` injects into the web
  and MCP containers. `COGNITO_CLIENT_ID` is a comma-separated **list** of
  accepted app-client ids (the confidential client *and* the public SPA client).
  The ALB health-check paths (`/api/ontologies`, `/mcp/health`) stay public.
  Browser SSE (EventSource can't set headers) passes the token as an
  `?access_token=` query param on its GET streams.
- **Browser SPA** (`frontend/src/auth.ts`): OAuth authorization-code grant with
  **PKCE** against the Cognito hosted UI, using the **public** SPA app client
  (no secret). On load the SPA redirects to the hosted UI, exchanges the
  returned `?code=` for tokens, and attaches the id token as
  `Authorization: Bearer` on API calls.
- **MCP / programmatic** clients send `Authorization: Bearer <jwt>` directly.
  The hosted-UI browser flow does not mint machine tokens; for headless access
  add a client-credentials (M2M) app client and use its access token.

**The user pool is invite-only** (`selfSignUpEnabled: false`). Create a user with
`aws cognito-idp admin-create-user` + `admin-set-user-password --permanent`
before anyone can log in.

Two app clients exist on the pool:

| Client | Secret | Purpose |
|---|---|---|
| `AppClient` (confidential) | yes | ALB `authenticate-cognito` action / future M2M |
| `SpaClient` (public, PKCE) | no | the browser SPA hosted-UI login |

## Known gaps / TODO (tracked in docs/deployment-cdk.md)

- **`Dockerfile.builder` needs a real `kiro-cli` install** — currently a
  placeholder. Biggest risk (Task 0).
- **Task 6**: `webapp.py` still spawns the builder as a local subprocess. It
  must be changed to call `ecs.run_task` against the builder task def
  (`BuilderTaskDefArn` output) and poll `describe_tasks`. IAM for this is
  already granted in `compute-stack.ts`.
- **Optional ALB-level `authenticate-cognito`**: app-level auth is the active
  mechanism. The ALB HTTPS listener path still exists behind
  `-c albCertificateArn=...` for anyone who wants edge auth with a real cert.
- **HTTPS on the ALB** and locking ALB ingress to the CloudFront prefix list
  (Task 8).
- **Scope `bedrock:InvokeModel`** to the specific model ARN (Task 8).
- Bedrock region/model are still hardcoded in the Python (Task 2) — the stack
  passes `BEDROCK_REGION` but the app must read it.
