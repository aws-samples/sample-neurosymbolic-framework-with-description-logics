import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as efs from 'aws-cdk-lib/aws-efs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

export interface ComputeStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  albSecurityGroup: ec2.ISecurityGroup;
  serviceSecurityGroup: ec2.ISecurityGroup;
  fileSystem: efs.IFileSystem;
  accessPoint: efs.IAccessPoint;
  ontologyBucket: s3.IBucket;
  artifactBucket: s3.IBucket;
  /** Path to the web image build context, or undefined to use a repo asset. */
  webImageAsset?: string;
  builderImageAsset?: string;
  bedrockRegion: string;
}

const APP_PORT = 8000;
const EFS_MOUNT = '/mnt/runs'; // ONTOLOGY_QA_CACHE_DIR + runs/ live here
const VOLUME_NAME = 'runs';

/**
 * ECS Fargate cluster with:
 *  - a long-lived WEB service behind an ALB (stateful chat/serve), and
 *  - a BUILDER task definition launched on demand via ecs.run_task (Task 6).
 * Both mount the shared EFS access point at EFS_MOUNT.
 */
export class ComputeStack extends cdk.Stack {
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly builderTaskDefinition: ecs.FargateTaskDefinition;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const cluster = new ecs.Cluster(this, 'Cluster', {
      vpc: props.vpc,
      containerInsights: true,
    });

    // Images: repo-root Dockerfiles. In CI (Task 7) these become ECR image
    // references; here we point at the build context with the right Dockerfile.
    const webImage = ecs.ContainerImage.fromAsset(props.webImageAsset ?? '..', {
      file: 'Dockerfile.web',
    });
    const builderImage = ecs.ContainerImage.fromAsset(props.builderImageAsset ?? '..', {
      file: 'Dockerfile.builder',
    });

    const efsVolume: ecs.Volume = {
      name: VOLUME_NAME,
      efsVolumeConfiguration: {
        fileSystemId: props.fileSystem.fileSystemId,
        transitEncryption: 'ENABLED',
        authorizationConfig: { accessPointId: props.accessPoint.accessPointId, iam: 'ENABLED' },
      },
    };

    const commonEnv: Record<string, string> = {
      ONTOLOGY_QA_CACHE_DIR: EFS_MOUNT,
      BEDROCK_REGION: props.bedrockRegion,
      // BEDROCK_MODEL_ID: '...'  // wire once Task 2 parameterizes it
    };

    // ---- shared IAM: Bedrock invoke (scope to the model ARN in Task 8) -----
    const bedrockInvoke = new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: ['*'], // TODO Task 8: scope to the specific foundation-model ARN
    });

    // ======================= WEB SERVICE ===================================
    const webTaskDef = new ecs.FargateTaskDefinition(this, 'WebTaskDef', {
      cpu: 512,
      memoryLimitMiB: 1024,
      volumes: [efsVolume],
    });
    webTaskDef.addToTaskRolePolicy(bedrockInvoke);
    props.ontologyBucket.grantRead(webTaskDef.taskRole);
    props.artifactBucket.grantReadWrite(webTaskDef.taskRole);
    props.fileSystem.grantReadWrite(webTaskDef.taskRole);

    const webContainer = webTaskDef.addContainer('web', {
      image: webImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'web',
        logRetention: logs.RetentionDays.ONE_MONTH,
      }),
      environment: { ...commonEnv, PORT: String(APP_PORT) },
      portMappings: [{ containerPort: APP_PORT }],
    });
    webContainer.addMountPoints({
      containerPath: EFS_MOUNT,
      sourceVolume: VOLUME_NAME,
      readOnly: false,
    });

    const service = new ecs.FargateService(this, 'WebService', {
      cluster,
      taskDefinition: webTaskDef,
      desiredCount: 1,
      securityGroups: [props.serviceSecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      // SSE streams are long-lived; give in-flight requests time to drain.
      minHealthyPercent: 100,
    });

    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc: props.vpc,
      internetFacing: true,
      securityGroup: props.albSecurityGroup,
    });
    const listener = this.loadBalancer.addListener('Http', {
      port: 80,
      open: false, // ingress is controlled by the ALB security group
    });
    listener.addTargets('WebTargets', {
      port: APP_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      // Cheap health check — /api/ontologies does no Bedrock call.
      healthCheck: {
        path: '/api/ontologies',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(30),
      },
      // SSE: don't let the target group idle-timeout mid-stream.
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // ======================= BUILDER TASK ==================================
    // Launched on demand by the web service via ecs.run_task (Task 6), NOT a
    // standing service. Same EFS mount so the built program is visible to web.
    this.builderTaskDefinition = new ecs.FargateTaskDefinition(this, 'BuilderTaskDef', {
      cpu: 1024,
      memoryLimitMiB: 2048,
      volumes: [efsVolume],
    });
    this.builderTaskDefinition.addToTaskRolePolicy(bedrockInvoke);
    props.ontologyBucket.grantRead(this.builderTaskDefinition.taskRole);
    props.artifactBucket.grantReadWrite(this.builderTaskDefinition.taskRole);
    props.fileSystem.grantReadWrite(this.builderTaskDefinition.taskRole);

    // kiro-cli headless auth. The API key lives in Secrets Manager (create the
    // empty secret with CDK, then set its value out-of-band — never in code) and
    // is injected into the builder container as KIRO_API_KEY at task start, so
    // the value never appears in the task definition or image.
    const kiroApiKeySecret = new secretsmanager.Secret(this, 'KiroApiKey', {
      description: 'kiro-cli headless API key for the ontology builder task',
    });

    const builderContainer = this.builderTaskDefinition.addContainer('builder', {
      image: builderImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'builder',
        logRetention: logs.RetentionDays.ONE_MONTH,
      }),
      environment: commonEnv,
      secrets: {
        // kiro-cli reads KIRO_API_KEY for headless auth (ontology_qa_cli/fixer.py).
        KIRO_API_KEY: ecs.Secret.fromSecretsManager(kiroApiKeySecret),
      },
      // Command (ontology name + build args) is supplied as a container
      // override by the web service's run_task call in Task 6, e.g.:
      //   overrides.containerOverrides[0].command = [
      //     "ontology_qa_cli.builder", "<name>", "--plain",
      //     "--ontology-config", "ontology_config.yaml", "--output-dir", EFS_MOUNT]
    });
    builderContainer.addMountPoints({
      containerPath: EFS_MOUNT,
      sourceVolume: VOLUME_NAME,
      readOnly: false,
    });

    // ---- Task 6 wiring: let the WEB task launch the BUILDER task ----------
    // The web service calls ecs.run_task; grant exactly that, scoped to the
    // builder task definition, plus PassRole for the builder's task+exec roles.
    webTaskDef.addToTaskRolePolicy(new iam.PolicyStatement({
      actions: ['ecs:RunTask'],
      resources: [this.builderTaskDefinition.taskDefinitionArn],
      conditions: { ArnEquals: { 'ecs:cluster': cluster.clusterArn } },
    }));
    webTaskDef.addToTaskRolePolicy(new iam.PolicyStatement({
      actions: ['ecs:DescribeTasks', 'ecs:ListTasks'],
      resources: ['*'],
      conditions: { ArnEquals: { 'ecs:cluster': cluster.clusterArn } },
    }));
    webTaskDef.addToTaskRolePolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [
        this.builderTaskDefinition.taskRole.roleArn,
        this.builderTaskDefinition.obtainExecutionRole().roleArn,
      ],
    }));

    new cdk.CfnOutput(this, 'ClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'KiroApiKeySecretArn', {
      value: kiroApiKeySecret.secretArn,
      description: 'Set the kiro-cli API key value here (Secrets Manager) before running a build.',
    });
    new cdk.CfnOutput(this, 'BuilderTaskDefArn', {
      value: this.builderTaskDefinition.taskDefinitionArn,
    });
    new cdk.CfnOutput(this, 'AlbDnsName', { value: this.loadBalancer.loadBalancerDnsName });
  }
}
