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
| `NeurosymbolicDL-Auth` | Cognito user pool, confidential app client, **public SPA app client (PKCE)**, **M2M client-credentials client**, hosted-UI domain |
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
  accepted app-client ids (the confidential client, the public SPA client, and
  the M2M client-credentials client).
  The ALB health-check paths (`/api/ontologies`, `/mcp/health`) stay public.
  Browser SSE (EventSource can't set headers) passes the token as an
  `?access_token=` query param on its GET streams.
- **Browser SPA** (`frontend/src/auth.ts`): OAuth authorization-code grant with
  **PKCE** against the Cognito hosted UI, using the **public** SPA app client
  (no secret). On load the SPA redirects to the hosted UI, exchanges the
  returned `?code=` for tokens, and attaches the id token as
  `Authorization: Bearer` on API calls.
- **MCP / programmatic (headless)** clients use the **M2M client-credentials
  grant**: POST the M2M client id + secret to the hosted-UI token endpoint to
  get an *access* token (no user, no browser), then send it as
  `Authorization: Bearer <token>` to `/mcp`. The grant requires a custom OAuth
  scope (Cognito forbids `openid`/`email` with client-credentials), so a
  resource server `neurosymbolic-mcp` owns the `mcp.invoke` scope the M2M client
  is granted. The `NeurosymbolicDL-Auth` stack prints the client id
  (`M2mClientId`) and the scope (`M2mScope`). The client id + secret are stored
  in Secrets Manager at **`neurosymbolic-mcp/m2m-client`** (JSON: `client_id`,
  `client_secret`, `token_url`, `scope`, `user_pool_id`); the Compute stack
  imports that secret and grants the **MCP task role** read access to it. Fetch
  the credentials from the secret:

  ```bash
  # read the stored M2M credentials
  CREDS=$(aws secretsmanager get-secret-value --region us-east-1 \
    --secret-id neurosymbolic-mcp/m2m-client --query SecretString --output text)
  CID=$(echo "$CREDS"    | python -c 'import sys,json;print(json.load(sys.stdin)["client_id"])')
  SECRET=$(echo "$CREDS" | python -c 'import sys,json;print(json.load(sys.stdin)["client_secret"])')

  # get an access token (valid ~1h), then call the MCP endpoint
  TOKEN=$(curl -s -u "$CID:$SECRET" \
    -d 'grant_type=client_credentials&scope=neurosymbolic-mcp/mcp.invoke' \
    https://neurosymbolic-dl-347181052905.auth.us-east-1.amazoncognito.com/oauth2/token \
    | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

  curl -H "Authorization: Bearer $TOKEN" https://d15j8kb5manvuj.cloudfront.net/mcp ...
  ```

  > The secret VALUE is a Cognito-generated client secret, so CDK cannot
  > populate it — the AuthStack mints the client, the value is written to the
  > secret out-of-band, and the Compute stack `fromSecretNameV2`-imports it to
  > attach the task-role grant. If you rotate the client secret in Cognito,
  > re-write the Secrets Manager value (`put-secret-value`).

**The user pool is invite-only** (`selfSignUpEnabled: false`). Create a user with
`aws cognito-idp admin-create-user` + `admin-set-user-password --permanent`
before anyone can log in.

Three app clients exist on the pool:

| Client | Secret | Purpose |
|---|---|---|
| `AppClient` (confidential) | yes | ALB `authenticate-cognito` action |
| `SpaClient` (public, PKCE) | no | the browser SPA hosted-UI login |
| `M2mClient` (confidential) | yes | headless MCP access (client-credentials, `mcp.invoke` scope) |

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
