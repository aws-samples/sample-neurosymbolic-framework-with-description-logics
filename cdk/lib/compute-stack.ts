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
import * as cr from 'aws-cdk-lib/custom-resources';

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
  /** Path to the MCP image build context, or undefined to use a repo asset. */
  mcpImageAsset?: string;
  /** Which configured ontologies the MCP server serves (default ["GO","CTO"]). */
  mcpOntologies?: string[];
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
const MCP_PORT = 8080; // the MCP Streamable-HTTP server (Dockerfile.mcp)
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
    const mcpImage = ecs.ContainerImage.fromAsset(props.mcpImageAsset ?? '..', {
      file: 'Dockerfile.mcp',
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

    // Captured when the authenticated HTTPS listener exists, so the MCP service
    // below can attach its own path-based (`/mcp*`) authenticated rule to the
    // same listener (same Cognito identity, same ALB).
    let httpsListener: elbv2.ApplicationListener | undefined;
    // Captured in the no-cert fallback so the MCP service can expose `/mcp*` on
    // the plain HTTP listener for testing.
    let plainHttpListener: elbv2.ApplicationListener | undefined;

    if (cognitoReady) {
      // The ALB SG (NetworkStack) allows :80 today; open :443 for the HTTPS
      // listener. Kept as anyIpv4 to match the current :80 rule — the
      // `alb-hardening` change tightens both to the CloudFront prefix list.
      props.albSecurityGroup.addIngressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(443),
        'HTTPS from CloudFront (tighten to the CloudFront prefix list in Task 8)',
      );

      httpsListener = this.loadBalancer.addListener('Https', {
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
      plainHttpListener = this.loadBalancer.addListener('Http', {
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

    // ======================= MCP SERVICE ===================================
    // A long-lived Fargate service running the remote MCP server (Dockerfile.mcp)
    // which exposes one free-form question-answering tool PER configured ontology
    // over MCP Streamable HTTP at POST /mcp. Loads each ontology (MCP_ONTOLOGIES,
    // e.g. "GO,CTO") lazily on first call from the shared S3 ontology bucket.
    // Reaches the ALB through a path-based (`/mcp*`) rule on the SAME
    // Cognito-authenticated HTTPS listener as the web service, so every MCP
    // request is authenticated before it hits the service.
    const mcpOntologies = props.mcpOntologies ?? ['GO', 'CTO'];
    const mcpOntologiesCsv = mcpOntologies.join(',');

    const mcpTaskDef = new ecs.FargateTaskDefinition(this, 'McpTaskDef', {
      cpu: 512,
      memoryLimitMiB: 1024,
      volumes: [efsVolume],
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    // S3 read for the ontology OWL; EFS for the owlready2 sqlite cache.
    // Bedrock InvokeModel is required at RUNTIME: the two reasoner tools are
    // pure DL (no model), but answer_question_with_GO runs the evolved query
    // program, which calls Bedrock per question. Without this grant that tool
    // degrades to "could not determine an answer" on an AccessDenied.
    props.ontologyBucket.grantRead(mcpTaskDef.taskRole);
    props.fileSystem.grantReadWrite(mcpTaskDef.taskRole);
    mcpTaskDef.addToTaskRolePolicy(bedrockInvoke);

    const mcpContainer = mcpTaskDef.addContainer('mcp', {
      image: mcpImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'mcp',
        logRetention: logs.RetentionDays.ONE_MONTH,
      }),
      environment: {
        ONTOLOGY_QA_CACHE_DIR: EFS_MOUNT,
        ONTOLOGY_CONFIG: 'ontology_config.yaml',
        MCP_ONTOLOGIES: mcpOntologiesCsv,
        // Each answer_question_with_<NAME> tool loads the best evolved query
        // program for its ontology from the shared runs/ mount (where the
        // builder Fargate task writes it). Without this the server defaults to
        // container-local ./runs and never finds it.
        ONTOLOGY_RUNS_DIR: EFS_MOUNT,
        PORT: String(MCP_PORT),
      },
      portMappings: [{ containerPort: MCP_PORT }],
    });
    mcpContainer.addMountPoints({
      containerPath: EFS_MOUNT,
      sourceVolume: VOLUME_NAME,
      readOnly: false,
    });

    const mcpService = new ecs.FargateService(this, 'McpService', {
      cluster,
      taskDefinition: mcpTaskDef,
      desiredCount: 1,
      securityGroups: [props.serviceSecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      minHealthyPercent: 100,
    });

    const mcpTargetGroup = new elbv2.ApplicationTargetGroup(this, 'McpTargets', {
      vpc: props.vpc,
      port: MCP_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [mcpService],
      // GET /mcp/health is a cheap liveness probe (no ontology load, no auth).
      healthCheck: {
        path: '/mcp/health',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(30),
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    if (httpsListener) {
      // Authenticated path rule: `/mcp*` -> Cognito auth -> MCP target group.
      // Higher-priority (lower number) than the listener default so it wins for
      // MCP paths; everything else still falls through to the web service.
      httpsListener.addAction('McpRule', {
        priority: 10,
        conditions: [elbv2.ListenerCondition.pathPatterns(['/mcp', '/mcp/*'])],
        action: new actions.AuthenticateCognitoAction({
          userPool: props.userPool!,
          userPoolClient: props.userPoolClient!,
          userPoolDomain: props.userPoolDomain!,
          next: elbv2.ListenerAction.forward([mcpTargetGroup]),
        }),
      });
    } else {
      // No authenticated HTTPS listener yet (no cert/identity): expose `/mcp*`
      // on the plain HTTP listener so the service is still reachable for
      // testing. NOTE: unauthenticated — the authenticated rule above is the
      // intended production path once a cert + Cognito identity are supplied.
      plainHttpListener?.addAction('McpRule', {
        priority: 10,
        conditions: [elbv2.ListenerCondition.pathPatterns(['/mcp', '/mcp/*'])],
        action: elbv2.ListenerAction.forward([mcpTargetGroup]),
      });
    }

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

    // ============== DEPLOY-TIME ONE-SHOT EVOLVE (GO) =======================
    // Fire the builder ONCE per `cdk deploy` to evolve a fresh query program
    // for the MCP server's ontology, written to the shared EFS runs/ mount that
    // the MCP service loads from. This is the "evolve a query program when the
    // MCP server is deployed" behaviour.
    //
    // Design — deliberately NON-BLOCKING on the build outcome:
    //   * The custom resource only LAUNCHES the build (ecs:RunTask) and returns
    //     as soon as the task is accepted. It does NOT wait for the (10-30 min,
    //     LLM-driven, costly) evolve to finish — CloudFormation custom resources
    //     cannot block a deploy that long, and we explicitly want the stack to
    //     ship regardless of build outcome.
    //   * ignoreErrorCodesMatching swallows RunTask API failures too (capacity,
    //     throttling), so even a failed LAUNCH never fails the deploy. Worst
    //     case: no fresh program is produced and answer_question_with_GO
    //     degrades to its honest "no query program built yet" tool error until
    //     a later build lands.
    //   * A timestamp physicalResourceId forces a re-run on EVERY deploy, so a
    //     fresh evolve is kicked off each time — matching "when deployed".
    //
    // Prerequisite: the builder reads KIRO_API_KEY (headless auth) from the
    // Secrets Manager secret below. If that secret is empty the launched build
    // will start and then fail — harmless here (deploy still succeeds, tool
    // degrades), but the program won't refresh until the key is set.
    const evolveSubnets = props.vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    });

    // One best-effort evolve launch PER served ontology. Each deploy fires a
    // fresh builder Fargate task for every ontology the MCP server serves, so
    // every answer_question_with_<NAME> tool gets a freshly-evolved program on
    // the EFS runs/ mount. Separate custom resources keep the launches
    // independent: one ontology's RunTask failure never blocks another's.
    for (const onto of mcpOntologies) {
      const runEvolveTask: cr.AwsSdkCall = {
        service: 'ECS',
        action: 'runTask',
        parameters: {
          cluster: cluster.clusterArn,
          taskDefinition: this.builderTaskDefinition.taskDefinitionArn,
          launchType: 'FARGATE',
          count: 1,
          networkConfiguration: {
            awsvpcConfiguration: {
              subnets: evolveSubnets.subnetIds,
              securityGroups: [props.serviceSecurityGroup.securityGroupId],
              assignPublicIp: 'DISABLED',
            },
          },
          overrides: {
            containerOverrides: [
              {
                name: 'builder',
                // The builder image ENTRYPOINT is ["uv","run","python","-m"], so
                // the override is just the module + args (do NOT repeat the
                // entrypoint). Mirrors the Task 6 run_task contract.
                command: [
                  'ontology_qa_cli.builder',
                  onto,
                  '--plain',
                  '--ontology-config', 'ontology_config.yaml',
                  '--output-dir', EFS_MOUNT,
                ],
              },
            ],
          },
        },
        // New id each deploy -> the custom resource re-runs RunTask every time.
        physicalResourceId: cr.PhysicalResourceId.of(`evolve-${onto}-${Date.now()}`),
        // Any RunTask failure (capacity, throttling, etc.) is swallowed so the
        // deploy still succeeds — the evolve is strictly best-effort.
        ignoreErrorCodesMatching: '.*',
      };

      const evolveOnDeploy = new cr.AwsCustomResource(this, `EvolveQueryProgramOnDeploy${onto}`, {
        onCreate: runEvolveTask,
        onUpdate: runEvolveTask,
        // No onDelete: tearing down the stack should not launch a build.
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['ecs:RunTask'],
            resources: [this.builderTaskDefinition.taskDefinitionArn],
            conditions: { ArnEquals: { 'ecs:cluster': cluster.clusterArn } },
          }),
          // RunTask with a task role requires PassRole for the builder's roles.
          new iam.PolicyStatement({
            actions: ['iam:PassRole'],
            resources: [
              this.builderTaskDefinition.taskRole.roleArn,
              this.builderTaskDefinition.obtainExecutionRole().roleArn,
            ],
          }),
        ]),
        // The build runs long after this returns; the CR itself is quick.
        timeout: cdk.Duration.minutes(2),
      });
      // Ensure the builder task def (and its roles) exist before we launch it.
      evolveOnDeploy.node.addDependency(this.builderTaskDefinition);
    }

    new cdk.CfnOutput(this, 'ClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'KiroApiKeySecretArn', {
      value: kiroApiKeySecret.secretArn,
      description: 'Set the kiro-cli API key value here (Secrets Manager) before running a build.',
    });
    new cdk.CfnOutput(this, 'BuilderTaskDefArn', {
      value: this.builderTaskDefinition.taskDefinitionArn,
    });
    new cdk.CfnOutput(this, 'AlbDnsName', { value: this.loadBalancer.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'McpEndpointPath', {
      value: '/mcp',
      description:
        'MCP Streamable-HTTP endpoint (POST /mcp), behind Cognito auth on the ALB HTTPS listener. ' +
        `Serving ontologies: ${mcpOntologiesCsv} (one answer_question_with_<NAME> tool each).`,
    });
    new cdk.CfnOutput(this, 'EvolveOnDeploy', {
      value: `${mcpOntologiesCsv} (best-effort, non-blocking)`,
      description:
        'Each deploy launches a one-shot builder Fargate task PER served ontology ' +
        'to evolve a fresh query program into the EFS runs/ mount. It is ' +
        'best-effort: a failed launch or build never fails the deploy, and the ' +
        'affected answer_question_with_<NAME> tool degrades to "no program built ' +
        'yet" until one lands. Requires the KiroApiKey secret to be set for the build to succeed.',
    });
  }
}
