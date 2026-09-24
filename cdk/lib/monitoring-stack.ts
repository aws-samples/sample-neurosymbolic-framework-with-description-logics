import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';

export interface MonitoringStackProps extends cdk.StackProps {
  /**
   * Bedrock model id whose token usage is alarmed on. This is the CloudWatch
   * `ModelId` dimension value — must match the model the app actually invokes
   * (see llm_config.py / Task 2), otherwise the alarm watches an empty metric.
   */
  bedrockModelId: string;
  /**
   * Email address subscribed to the alerts SNS topic. Optional: if omitted, the
   * topic is still created (and its ARN output) so subscriptions can be added
   * later — CDK never puts an email in a form that would leak it in the template.
   */
  alertEmail?: string;
  /**
   * Soft per-day token cap. When the day's summed input+output token count
   * crosses this, the alarm fires. Bedrock (Opus) is the dominant variable
   * cost, and it is billed per token, so this is the earliest cost signal.
   * Default 5,000,000 tokens/day — tune to your budget.
   */
  dailyTokenCap?: number;
  /**
   * Hard total-dollar backstop. AWS Billing publishes estimated charges as a
   * running monthly total in us-east-1; this alarm fires when the whole
   * account's estimated bill crosses the threshold, catching any cost the
   * token alarm can't see (Fargate, egress, a misconfigured ModelId, etc.).
   * Default $200. Set to 0 to skip the billing alarm.
   */
  monthlyUsdCap?: number;
  /**
   * Automated kill-switch. When true, a Lambda subscribed to the alert topic
   * scales the web ECS service to desiredCount=0 the moment the daily
   * token-cap alarm enters ALARM — a real spend STOP, not just a notification.
   * OFF by default: an alarm that silently takes the service down is
   * surprising, so it must be opted into explicitly (`-c killSwitch=true`).
   *
   * Requires `killSwitchCluster`, `killSwitchService`, and `killSwitchRegion`
   * so the Lambda knows exactly which service to scale (the compute stack lives
   * in the app region, which may differ from this stack's us-east-1 pin).
   */
  killSwitch?: boolean;
  /** ECS cluster name of the web service to scale down. Required if killSwitch. */
  killSwitchCluster?: string;
  /** ECS service name to scale to 0. Required if killSwitch. */
  killSwitchService?: string;
  /** Region the ECS cluster/service live in. Required if killSwitch. */
  killSwitchRegion?: string;

  // ---- Ops alarms (Task 8): ALB 5xx, ECS task crash loops, EFS burst credit.
  // All optional and config-driven by name/id so this us-east-1-pinned stack can
  // alarm on resources in the app region without cross-region construct refs.
  // Each group is created only when its identifier(s) are supplied.
  /**
   * Region the ALB / ECS / EFS resources live in (the app region). Defaults to
   * `killSwitchRegion` when set, else this stack's region. Ops-alarm metrics are
   * built with this explicit region so they read the right account/region data.
   */
  opsResourceRegion?: string;
  /**
   * ALB metric dimension value, i.e. `loadBalancer.loadBalancerFullName`
   * (`app/<name>/<id>`). When set, an ALB 5xx alarm is created.
   */
  albFullName?: string;
  /** ALB 5xx count over 5 min that trips the alarm. Default 10. */
  alb5xxThreshold?: number;
  /**
   * ECS cluster + service names to watch for a crash loop (sustained low/zero
   * RunningTaskCount). When both set, a crash-loop alarm is created. Defaults to
   * the killSwitch cluster/service when those are provided.
   */
  ecsClusterName?: string;
  ecsServiceName?: string;
  /**
   * EFS filesystem id (`fs-...`). When set, a burst-credit-exhaustion alarm is
   * created (BurstCreditBalance approaching zero). Note: only meaningful in
   * EFS bursting-throughput mode.
   */
  efsFileSystemId?: string;
}

/**
 * Cost guardrails + ops alarms (Task 8). Bedrock token spend is the real cost
 * driver, so the primary alarms watch AWS/Bedrock token metrics; a billing alarm
 * is a coarse dollar backstop. Ops alarms (ALB 5xx, ECS crash loop, EFS
 * burst-credit) watch health. All alarms notify one SNS topic. Optionally
 * (killSwitch) a Lambda on that topic scales the web service to 0 when the token
 * cap is hit, turning the alarm into a real spend stop.
 *
 * NOTE on region: the AWS/Billing EstimatedCharges metric is ONLY published in
 * us-east-1. Deploy this stack in us-east-1 (the app's default Bedrock region)
 * so the billing alarm has data. If you deploy elsewhere, pass monthlyUsdCap: 0
 * to skip it and rely on the token alarms + a separate us-east-1 billing alarm.
 */
export class MonitoringStack extends cdk.Stack {
  public readonly alertTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: MonitoringStackProps) {
    super(scope, id, props);

    const dailyTokenCap = props.dailyTokenCap ?? 5_000_000;
    const monthlyUsdCap = props.monthlyUsdCap ?? 200;

    // ---- alerts topic -----------------------------------------------------
    this.alertTopic = new sns.Topic(this, 'CostAlerts', {
      displayName: 'Neurosymbolic-DL cost & ops alerts',
    });
    if (props.alertEmail) {
      this.alertTopic.addSubscription(
        new subscriptions.EmailSubscription(props.alertEmail),
      );
    }
    const alarmAction = new cwActions.SnsAction(this.alertTopic);

    // ---- Bedrock token spend (the real cost driver) -----------------------
    // AWS/Bedrock publishes InputTokenCount / OutputTokenCount per ModelId.
    // We alarm on the DAILY SUM of input+output tokens crossing the cap.
    const period = cdk.Duration.days(1);
    const dimensions = { ModelId: props.bedrockModelId };

    const inputTokens = new cloudwatch.Metric({
      namespace: 'AWS/Bedrock',
      metricName: 'InputTokenCount',
      dimensionsMap: dimensions,
      statistic: cloudwatch.Stats.SUM,
      period,
    });
    const outputTokens = new cloudwatch.Metric({
      namespace: 'AWS/Bedrock',
      metricName: 'OutputTokenCount',
      dimensionsMap: dimensions,
      statistic: cloudwatch.Stats.SUM,
      period,
    });

    // Total tokens = input + output. Output tokens cost several times more than
    // input on Opus, but a single token-count cap is a deliberately simple
    // early-warning signal; the billing alarm is the dollar-accurate backstop.
    const totalTokens = new cloudwatch.MathExpression({
      expression: 'inTok + outTok',
      usingMetrics: { inTok: inputTokens, outTok: outputTokens },
      label: 'Bedrock total tokens/day',
      period,
    });

    const tokenAlarm = new cloudwatch.Alarm(this, 'BedrockDailyTokenCap', {
      alarmName: `${this.stackName}-bedrock-daily-token-cap`,
      alarmDescription:
        `Bedrock input+output tokens for ${props.bedrockModelId} exceeded ` +
        `${dailyTokenCap.toLocaleString()} in a day. Bedrock is the dominant ` +
        `variable cost — investigate usage or pause the service.`,
      metric: totalTokens,
      threshold: dailyTokenCap,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      // If the metric is missing (no Bedrock calls that day) that is NOT a
      // cost problem — treat missing data as not-breaching.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    tokenAlarm.addAlarmAction(alarmAction);
    tokenAlarm.addOkAction(alarmAction);

    // ---- billing backstop (coarse total-dollar cap) -----------------------
    // AWS/Billing EstimatedCharges is a running monthly total, us-east-1 only.
    if (monthlyUsdCap > 0) {
      const estimatedCharges = new cloudwatch.Metric({
        namespace: 'AWS/Billing',
        metricName: 'EstimatedCharges',
        dimensionsMap: { Currency: 'USD' },
        statistic: cloudwatch.Stats.MAXIMUM,
        // Billing metrics update a few times a day; a 6h period is plenty.
        period: cdk.Duration.hours(6),
      });

      const billingAlarm = new cloudwatch.Alarm(this, 'MonthlyEstimatedCharges', {
        alarmName: `${this.stackName}-monthly-estimated-charges`,
        alarmDescription:
          `Account estimated charges exceeded $${monthlyUsdCap} this month. ` +
          `Hard dollar backstop covering all cost (Bedrock, Fargate, egress).`,
        metric: estimatedCharges,
        threshold: monthlyUsdCap,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      billingAlarm.addAlarmAction(alarmAction);

      new cdk.CfnOutput(this, 'BillingAlarmName', { value: billingAlarm.alarmName });
    }

    // ---- kill-switch (opt-in): scale the web service to 0 on ALARM --------
    // A CloudWatch alarm only NOTIFIES. When props.killSwitch is set, this
    // Lambda — subscribed to the same alert topic — turns the token-cap alarm
    // into a real spend STOP by scaling the web ECS service to desiredCount=0.
    // It ignores every other alarm on the topic (billing, OK transitions) and
    // acts only when the daily token-cap alarm enters state ALARM.
    if (props.killSwitch) {
      const missing: string[] = [];
      if (!props.killSwitchCluster) missing.push('killSwitchCluster');
      if (!props.killSwitchService) missing.push('killSwitchService');
      if (!props.killSwitchRegion) missing.push('killSwitchRegion');
      if (missing.length) {
        throw new Error(
          `killSwitch=true requires ${missing.join(', ')} so the Lambda knows ` +
          `which ECS service to scale down (compute lives in the app region, ` +
          `which may differ from this stack's us-east-1).`,
        );
      }

      const killFn = new lambda.Function(this, 'CostKillSwitch', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.handler',
        timeout: cdk.Duration.seconds(30),
        description:
          'Scales the web ECS service to 0 when the Bedrock token-cap alarm fires.',
        environment: {
          CLUSTER_NAME: props.killSwitchCluster!,
          SERVICE_NAME: props.killSwitchService!,
          SERVICE_REGION: props.killSwitchRegion!,
          // Only this alarm triggers the kill; other topic messages are ignored.
          TRIGGER_ALARM_NAME: tokenAlarm.alarmName,
        },
        code: lambda.Code.fromInline([
          'import json, os, boto3',
          '',
          'ecs = boto3.client("ecs", region_name=os.environ["SERVICE_REGION"])',
          'CLUSTER = os.environ["CLUSTER_NAME"]',
          'SERVICE = os.environ["SERVICE_NAME"]',
          'TRIGGER = os.environ["TRIGGER_ALARM_NAME"]',
          '',
          'def handler(event, _ctx):',
          '    for rec in event.get("Records", []):',
          '        msg = json.loads(rec["Sns"]["Message"])',
          '        name = msg.get("AlarmName")',
          '        state = msg.get("NewStateValue")',
          '        # Act only on OUR alarm entering ALARM. Ignore billing alarm,',
          '        # OK/INSUFFICIENT_DATA transitions, and any manual test message.',
          '        if name != TRIGGER or state != "ALARM":',
          '            print(f"ignoring alarm={name!r} state={state!r}")',
          '            continue',
          '        print(f"KILL: scaling {SERVICE} on {CLUSTER} to desiredCount=0")',
          '        ecs.update_service(cluster=CLUSTER, service=SERVICE, desiredCount=0)',
          '        print("scaled to 0")',
          '    return {"ok": True}',
        ].join('\n')),
      });

      // Least privilege: only stop THIS service, and read it back.
      const serviceArn = cdk.Stack.of(this).formatArn({
        service: 'ecs',
        region: props.killSwitchRegion!,
        resource: 'service',
        resourceName: `${props.killSwitchCluster!}/${props.killSwitchService!}`,
      });
      killFn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['ecs:UpdateService', 'ecs:DescribeServices'],
        resources: [serviceArn],
      }));

      // Wire the Lambda to the alert topic (fan-out alongside email).
      this.alertTopic.addSubscription(new subscriptions.LambdaSubscription(killFn));

      new cdk.CfnOutput(this, 'KillSwitchFunctionName', {
        value: killFn.functionName,
        description:
          'Scales the web service to 0 when the token-cap alarm fires. ' +
          'Restore service with: aws ecs update-service --desired-count 1.',
      });
    }

    // ---- Ops alarms (Task 8) ---------------------------------------------
    // ALB 5xx, ECS crash loop, EFS burst-credit exhaustion. Built from
    // names/ids with an explicit region so this us-east-1 stack can watch
    // app-region resources. Each is created only when its id(s) are supplied.
    const opsRegion = props.opsResourceRegion ?? props.killSwitchRegion ?? this.region;

    // ALB 5xx: elevated server-side errors from the load balancer itself.
    if (props.albFullName) {
      const alb5xx = new cloudwatch.Metric({
        namespace: 'AWS/ApplicationELB',
        metricName: 'HTTPCode_ELB_5XX_Count',
        dimensionsMap: { LoadBalancer: props.albFullName },
        statistic: cloudwatch.Stats.SUM,
        period: cdk.Duration.minutes(5),
        region: opsRegion,
      });
      const alb5xxAlarm = new cloudwatch.Alarm(this, 'Alb5xx', {
        alarmName: `${this.stackName}-alb-5xx`,
        alarmDescription:
          `ALB returned more than ${props.alb5xxThreshold ?? 10} 5xx responses ` +
          `in 5 min — the load balancer could not reach a healthy target.`,
        metric: alb5xx,
        threshold: props.alb5xxThreshold ?? 10,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      alb5xxAlarm.addAlarmAction(alarmAction);
      alb5xxAlarm.addOkAction(alarmAction);
    }

    // ECS crash loop: the web service running fewer tasks than desired for a
    // sustained window (tasks starting then dying). RunningTaskCount < 1
    // averaged over 15 min (3 x 5-min periods) catches a task that can't stay up.
    if (props.ecsClusterName && props.ecsServiceName) {
      const runningTasks = new cloudwatch.Metric({
        namespace: 'ECS/ContainerInsights',
        metricName: 'RunningTaskCount',
        dimensionsMap: {
          ClusterName: props.ecsClusterName,
          ServiceName: props.ecsServiceName,
        },
        statistic: cloudwatch.Stats.AVERAGE,
        period: cdk.Duration.minutes(5),
        region: opsRegion,
      });
      const crashLoopAlarm = new cloudwatch.Alarm(this, 'EcsCrashLoop', {
        alarmName: `${this.stackName}-ecs-crash-loop`,
        alarmDescription:
          `Web service ${props.ecsServiceName} had <1 running task on average ` +
          `for 15 min — likely a crash loop (tasks starting then exiting). ` +
          `Requires Container Insights (enabled on the cluster).`,
        metric: runningTasks,
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 3,
        datapointsToAlarm: 3,
        // No data here would mean the service/insights is gone — treat as
        // breaching so a vanished service is noticed rather than silently OK.
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      });
      crashLoopAlarm.addAlarmAction(alarmAction);
      crashLoopAlarm.addOkAction(alarmAction);
    }

    // EFS burst-credit exhaustion: in bursting-throughput mode, a depleted
    // BurstCreditBalance throttles the shared runs/ + sqlite cache, stalling
    // both web and builder. Alarm well before zero so there's time to react.
    if (props.efsFileSystemId) {
      const burstCredits = new cloudwatch.Metric({
        namespace: 'AWS/EFS',
        metricName: 'BurstCreditBalance',
        dimensionsMap: { FileSystemId: props.efsFileSystemId },
        statistic: cloudwatch.Stats.AVERAGE,
        period: cdk.Duration.minutes(15),
        region: opsRegion,
      });
      const burstAlarm = new cloudwatch.Alarm(this, 'EfsBurstCredit', {
        alarmName: `${this.stackName}-efs-burst-credit`,
        alarmDescription:
          `EFS ${props.efsFileSystemId} burst-credit balance is low — sustained ` +
          `throughput will throttle the shared runs/ + sqlite cache. Only ` +
          `meaningful in bursting-throughput mode; ignore if using Elastic/` +
          `Provisioned throughput.`,
        metric: burstCredits,
        // ~1 TiB-minute: comfortably above zero, giving lead time to act.
        threshold: 1_000_000_000_000,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      burstAlarm.addAlarmAction(alarmAction);
      burstAlarm.addOkAction(alarmAction);
    }

    new cdk.CfnOutput(this, 'AlertTopicArn', {
      value: this.alertTopic.topicArn,
      description: 'Subscribe additional endpoints (Slack, PagerDuty) here.',
    });
    new cdk.CfnOutput(this, 'BedrockTokenAlarmName', { value: tokenAlarm.alarmName });
  }
}
