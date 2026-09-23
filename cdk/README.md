# CDK deployment (split architecture, external AWS, behind auth)

Scaffolds the AWS deployment described in [`../docs/deployment-cdk.md`](../docs/deployment-cdk.md).

**Status: scaffold only.** These files have not been synth'd or deployed. Node
deps are not installed and no AWS resources exist yet. Review before deploying.

## Stacks

| Stack | Contents |
|---|---|
| `NeurosymbolicDL-Network` | VPC (public + private-egress subnets), security groups, S3 gateway endpoint |
| `NeurosymbolicDL-Storage` | S3 buckets (frontend/ontologies/artifacts), EFS filesystem + access point |
| `NeurosymbolicDL-Compute` | ECS Fargate cluster, web service behind an ALB, on-demand builder task def, IAM (Bedrock/S3/RunTask) |
| `NeurosymbolicDL-Edge` | CloudFront (S3 SPA + `/api/*` → ALB), Cognito user pool + app client |

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

## Known gaps / TODO (tracked in docs/deployment-cdk.md)

- **`Dockerfile.builder` needs a real `kiro-cli` install** — currently a
  placeholder. Biggest risk (Task 0).
- **Task 6**: `webapp.py` still spawns the builder as a local subprocess. It
  must be changed to call `ecs.run_task` against the builder task def
  (`BuilderTaskDefArn` output) and poll `describe_tasks`. IAM for this is
  already granted in `compute-stack.ts`.
- **Auth wiring**: Cognito resources exist; the ALB listener
  `authenticate-cognito` action needs an HTTPS listener + ACM cert + domain
  before it can be attached (see the note in `edge-stack.ts`).
- **HTTPS on the ALB** and locking ALB ingress to the CloudFront prefix list
  (Task 8).
- **Scope `bedrock:InvokeModel`** to the specific model ARN (Task 8).
- Bedrock region/model are still hardcoded in the Python (Task 2) — the stack
  passes `BEDROCK_REGION` but the app must read it.
