#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkStack } from '../lib/network-stack';
import { StorageStack } from '../lib/storage-stack';
import { ComputeStack } from '../lib/compute-stack';
import { AuthStack } from '../lib/auth-stack';
import { EdgeStack } from '../lib/edge-stack';
import { MonitoringStack } from '../lib/monitoring-stack';

const app = new cdk.App();

// External AWS account: account/region come from the CLI environment
// (CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION) or app context. Bedrock Opus must
// be available in this region — default us-east-1; override via `-c region=...`.
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: app.node.tryGetContext('region') ?? process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

const prefix = 'NeurosymbolicDL';

// Bedrock model id, shared by the compute env wiring and the cost-guardrail
// alarms. Must match llm_config.py's DEFAULT_BEDROCK_MODEL (Task 2) so the
// CloudWatch ModelId dimension tracks the model the app actually invokes.
const bedrockModel =
  app.node.tryGetContext('bedrockModel') ?? 'us.anthropic.claude-opus-4-6-v1';

const network = new NetworkStack(app, `${prefix}-Network`, { env });

const storage = new StorageStack(app, `${prefix}-Storage`, {
  env,
  vpc: network.vpc,
  efsSecurityGroup: network.efsSecurityGroup,
});

// Identity for the "behind auth" requirement. `appDomain` (the CloudFront /
// custom domain the app is served from) is chicken-and-egg with the
// distribution, so it is context-driven: set `-c appDomain=https://<domain>`
// once the CloudFront domain is known, so the app client gets its OAuth
// callback URLs. Until then the client is created without callbacks.
const auth = new AuthStack(app, `${prefix}-Auth`, {
  env,
  appDomain: app.node.tryGetContext('appDomain'),
});

const compute = new ComputeStack(app, `${prefix}-Compute`, {
  env,
  vpc: network.vpc,
  albSecurityGroup: network.albSecurityGroup,
  serviceSecurityGroup: network.serviceSecurityGroup,
  fileSystem: storage.fileSystem,
  accessPoint: storage.accessPoint,
  ontologyBucket: storage.ontologyBucket,
  artifactBucket: storage.artifactBucket,
  // Ontologies exposed as MCP answer_question_with_<NAME> tools (each also
  // gets a per-deploy builder evolve task). All configured ontologies are
  // served; the list mirrors ontology_config.yaml.
  mcpOntologies: ['FMA', 'GO', 'CTO', 'CL', 'HPO', 'NCIt', 'OBI', 'OGMS', 'BFO', 'Mondo', 'LifeSci'],
  // Image tags/repos are supplied by CI (Task 7). Placeholder context keys:
  webImageAsset: app.node.tryGetContext('webImage'),
  builderImageAsset: app.node.tryGetContext('builderImage'),
  bedrockRegion: app.node.tryGetContext('bedrockRegion') ?? 'us-east-1',
  // Default matches ontology_qa_cli/llm_config.py's DEFAULT_BEDROCK_MODEL.
  bedrockModel:
    app.node.tryGetContext('bedrockModel') ?? 'us.anthropic.claude-opus-4-6-v1',
  // HTTPS Cognito listener: enabled when an ACM cert ARN (same region as the
  // ALB) is supplied via `-c albCertificateArn=arn:aws:acm:...`. Without it the
  // ALB keeps the plain HTTP listener and CloudFront terminates TLS at the edge.
  userPool: auth.userPool,
  userPoolClient: auth.userPoolClient,
  userPoolDomain: auth.userPoolDomain,
  spaClient: auth.spaClient,
  m2mClient: auth.m2mClient,
  certificateArn: app.node.tryGetContext('albCertificateArn'),
  // Web autoscaling bounds — override via `-c webMinCapacity=` / `-c webMaxCapacity=`.
  webMinCapacity: numContext(app, 'webMinCapacity'),
  webMaxCapacity: numContext(app, 'webMaxCapacity'),
});

new EdgeStack(app, `${prefix}-Edge`, {
  env,
  loadBalancer: compute.loadBalancer,
  userPool: auth.userPool,
});

// Cost guardrails (Task 8). Pinned to us-east-1 because the AWS/Billing
// EstimatedCharges metric is only published there; the Bedrock token alarms
// work in any region but us-east-1 is also the app's default Bedrock region.
// Configure via context: -c alertEmail=you@amazon.com -c dailyTokenCap=... -c monthlyUsdCap=...
//
// Optional kill-switch (-c killSwitch=true): a Lambda scales the web ECS
// service to 0 when the token cap fires. It needs the cluster + service names,
// which the Compute stack prints as outputs (ClusterName; the WebService's
// generated name) — pass them once known: -c killSwitchCluster=... -c
// killSwitchService=.... Region defaults to the app region (where Compute lives).
new MonitoringStack(app, `${prefix}-Monitoring`, {
  env: { account: env.account, region: 'us-east-1' },
  bedrockModelId: bedrockModel,
  alertEmail: app.node.tryGetContext('alertEmail'),
  dailyTokenCap: numContext(app, 'dailyTokenCap'),
  monthlyUsdCap: numContext(app, 'monthlyUsdCap'),
  killSwitch: app.node.tryGetContext('killSwitch') === 'true' ||
    app.node.tryGetContext('killSwitch') === true,
  killSwitchCluster: app.node.tryGetContext('killSwitchCluster'),
  killSwitchService: app.node.tryGetContext('killSwitchService'),
  killSwitchRegion: app.node.tryGetContext('killSwitchRegion') ?? env.region,

  // Ops alarms (Task 8). ALB + EFS come straight from the live constructs (same
  // app, cross-region metric read is fine). ECS cluster/service reuse the
  // killSwitch context keys — the Compute stack doesn't expose the service as a
  // public field, and these are the same names the kill-switch already needs.
  opsResourceRegion: env.region,
  albFullName: compute.loadBalancer.loadBalancerFullName,
  alb5xxThreshold: numContext(app, 'alb5xxThreshold'),
  ecsClusterName: app.node.tryGetContext('killSwitchCluster'),
  ecsServiceName: app.node.tryGetContext('killSwitchService'),
  efsFileSystemId: storage.fileSystem.fileSystemId,
});

app.synth();

/** Read a numeric context value, or undefined so the stack default applies. */
function numContext(app: cdk.App, key: string): number | undefined {
  const raw = app.node.tryGetContext(key);
  if (raw === undefined || raw === null || String(raw).trim() === '') return undefined;
  const n = Number(raw);
  if (Number.isNaN(n)) throw new Error(`Context '${key}' must be a number, got: ${raw}`);
  return n;
}
