import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as efs from 'aws-cdk-lib/aws-efs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as actions from 'aws-cdk-lib/aws-elasticloadbalancingv2-actions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
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
  bedrockModel: string;
  /**
   * Cognito identity (from AuthStack) used by the ALB HTTPS listener's
   * `authenticate-cognito` action. Required together with `certificateArn` to
   * enable the authenticated HTTPS listener; omit both to keep the plain HTTP
   * listener only.
   */
  userPool?: cognito.IUserPool;
  userPoolClient?: cognito.IUserPoolClient;
  userPoolDomain?: cognito.IUserPoolDomain;
  /**
   * ARN of an ACM certificate for the ALB HTTPS (443) listener. Must be in the
   * SAME region as the ALB (unlike the CloudFront cert, which lives in
   * us-east-1). When set, the ALB serves an HTTPS listener that authenticates
   * via Cognito before forwarding to the web service.
   */
  certificateArn?: string;
  /** Web-service autoscaling bounds (target tracking on CPU + request count). */
  webMinCapacity?: number;
  webMaxCapacity?: number;
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
      // Names MUST match what ontology_qa_cli/llm_config.py reads at runtime
      // (ONTOLOGY_QA_BEDROCK_REGION / ONTOLOGY_QA_BEDROCK_MODEL); otherwise the
      // app silently falls back to its hardcoded defaults, ignoring these.
      ONTOLOGY_QA_BEDROCK_REGION: props.bedrockRegion,
      ONTOLOGY_QA_BEDROCK_MODEL: props.bedrockModel,
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
      // Run on Graviton/arm64 so the natively-built (Apple Silicon) image runs
      // without emulation. Both task defs must match the image architecture.
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
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

    // Shared target group for the web service — referenced by whichever
    // listener(s) exist below.
    const webTargetGroup = new elbv2.ApplicationTargetGroup(this, 'WebTargets', {
      vpc: props.vpc,
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

    // Enable the authenticated HTTPS listener only when BOTH an ACM cert and
    // the Cognito identity are supplied. `authenticate-cognito` requires HTTPS
    // (the ALB will not run it on a plain HTTP listener), so without a cert we
    // fall back to the plain HTTP listener and leave auth to be added once a
    // cert + domain exist.
    const cognitoReady =
      !!props.certificateArn &&
      !!props.userPool &&
      !!props.userPoolClient &&
      !!props.userPoolDomain;

    if (cognitoReady) {
      // The ALB SG (NetworkStack) allows :80 today; open :443 for the HTTPS
      // listener. Kept as anyIpv4 to match the current :80 rule — the
      // `alb-hardening` change tightens both to the CloudFront prefix list.
      props.albSecurityGroup.addIngressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(443),
        'HTTPS from CloudFront (tighten to the CloudFront prefix list in Task 8)',
      );

      const httpsListener = this.loadBalancer.addListener('Https', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [elbv2.ListenerCertificate.fromArn(props.certificateArn!)],
        open: false, // ingress is controlled by the ALB security group
        // Unauthenticated requests are bounced to the Cognito hosted UI; once
        // authenticated the request is forwarded to the web target group.
        defaultAction: new actions.AuthenticateCognitoAction({
          userPool: props.userPool!,
          userPoolClient: props.userPoolClient!,
          userPoolDomain: props.userPoolDomain!,
          next: elbv2.ListenerAction.forward([webTargetGroup]),
        }),
      });
      // Referenced so the construct is not flagged as unused; the action above
      // already wires the target group in.
      void httpsListener;

      // Plain HTTP now only redirects to HTTPS so the auth handshake always
      // happens over TLS.
      this.loadBalancer.addListener('Http', {
        port: 80,
        open: false,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS',
          port: '443',
          permanent: true,
        }),
      });
    } else {
      // No cert/identity yet: single HTTP listener forwarding to the service.
      // (CloudFront still fronts this with HTTPS at the edge.)
      this.loadBalancer.addListener('Http', {
        port: 80,
        open: false, // ingress is controlled by the ALB security group
        defaultAction: elbv2.ListenerAction.forward([webTargetGroup]),
      });
    }

    // ---- Autoscaling for the web service ----------------------------------
    // The web tasks hold in-memory ChatSession/OntologyAnswerer state and serve
    // long-lived SSE streams, so scale OUT readily but scale IN cautiously — a
    // premature scale-in would drop active sessions/streams. Bounds are modest
    // (a small interactive service, not a fleet); raise maxCapacity via context
    // if load warrants.
    const scaling = service.autoScaleTaskCount({
      minCapacity: props.webMinCapacity ?? 1,
      maxCapacity: props.webMaxCapacity ?? 4,
    });
    // CPU target tracking — the primary signal for the Bedrock-bound workload.
    scaling.scaleOnCpuUtilization('CpuScaling', {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.minutes(5), // slow to give SSE streams time to drain
      scaleOutCooldown: cdk.Duration.minutes(1), // quick to absorb bursts
    });
    // Request-count-per-target — catches connection load CPU may not reflect
    // (many idle-ish SSE clients). Tied to the ALB target group above.
    scaling.scaleOnRequestCount('RequestScaling', {
      requestsPerTarget: 200,
      targetGroup: webTargetGroup,
      scaleInCooldown: cdk.Duration.minutes(5),
      scaleOutCooldown: cdk.Duration.minutes(1),
    });

    // ======================= BUILDER TASK ==================================
    // Launched on demand by the web service via ecs.run_task (Task 6), NOT a
    // standing service. Same EFS mount so the built program is visible to web.
    this.builderTaskDefinition = new ecs.FargateTaskDefinition(this, 'BuilderTaskDef', {
      cpu: 1024,
      memoryLimitMiB: 2048,
      volumes: [efsVolume],
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
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

    const builderLogGroup = new logs.LogGroup(this, 'BuilderLogGroup', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const builderContainer = this.builderTaskDefinition.addContainer('builder', {
      image: builderImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'builder',
        logGroup: builderLogGroup,
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

    // ---- Tell the WEB app to use the ECS build backend -------------------
    // Without BUILD_BACKEND=ecs the web app defaults to the subprocess backend
    // and runs the (long, costly) build INSIDE the web container instead of
    // launching the builder Fargate task. These vars are read by
    // ontology_qa_cli/build_backend.py:EcsBuildBackend. The private subnets +
    // service SG match where the web service itself runs (egress to Bedrock/S3,
    // reachable by EFS). The log group is the builder's, tailed for the live
    // build stream; grant the web role read on it.
    const builderSubnets = props.vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    });
    webContainer.addEnvironment('BUILD_BACKEND', 'ecs');
    webContainer.addEnvironment('BUILD_ECS_CLUSTER', cluster.clusterName);
    webContainer.addEnvironment(
      'BUILD_ECS_TASK_DEFINITION',
      this.builderTaskDefinition.taskDefinitionArn,
    );
    webContainer.addEnvironment('BUILD_ECS_CONTAINER_NAME', 'builder');
    webContainer.addEnvironment('BUILD_ECS_SUBNETS', builderSubnets.subnetIds.join(','));
    webContainer.addEnvironment(
      'BUILD_ECS_SECURITY_GROUPS',
      props.serviceSecurityGroup.securityGroupId,
    );
    webContainer.addEnvironment('BUILD_ECS_LOG_GROUP', builderLogGroup.logGroupName);
    webContainer.addEnvironment('BUILD_ECS_LOG_STREAM_PREFIX', 'builder');
    webContainer.addEnvironment('ONTOLOGY_CONFIG', 'ontology_config.yaml');
    webContainer.addEnvironment('ONTOLOGY_RUNS_DIR', EFS_MOUNT);
    // The web app tails the builder's CloudWatch logs for the live build stream.
    builderLogGroup.grantRead(webTaskDef.taskRole);

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
