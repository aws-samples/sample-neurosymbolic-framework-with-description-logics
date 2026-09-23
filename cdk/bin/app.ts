#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkStack } from '../lib/network-stack';
import { StorageStack } from '../lib/storage-stack';
import { ComputeStack } from '../lib/compute-stack';
import { AuthStack } from '../lib/auth-stack';
import { EdgeStack } from '../lib/edge-stack';

const app = new cdk.App();

// External AWS account: account/region come from the CLI environment
// (CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION) or app context. Bedrock Opus must
// be available in this region — default us-east-1; override via `-c region=...`.
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: app.node.tryGetContext('region') ?? process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

const prefix = 'NeurosymbolicDL';

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
  certificateArn: app.node.tryGetContext('albCertificateArn'),
});

new EdgeStack(app, `${prefix}-Edge`, {
  env,
  loadBalancer: compute.loadBalancer,
  userPool: auth.userPool,
});

app.synth();
