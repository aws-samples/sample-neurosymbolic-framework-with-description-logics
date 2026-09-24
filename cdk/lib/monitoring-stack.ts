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
}

/**
 * Cost guardrails (Task 8). Bedrock token spend is the real cost driver, so the
 * primary alarms watch AWS/Bedrock token metrics; a billing alarm is a coarse
 * dollar backstop. All alarms notify one SNS topic. Optionally (killSwitch) a
 * Lambda on that topic scales the web service to 0 when the token cap is hit,
 * turning the alarm into a real spend stop.
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

    new cdk.CfnOutput(this, 'AlertTopicArn', {
      value: this.alertTopic.topicArn,
      description: 'Subscribe additional endpoints (Slack, PagerDuty) here.',
    });
    new cdk.CfnOutput(this, 'BedrockTokenAlarmName', { value: tokenAlarm.alarmName });
  }
}
