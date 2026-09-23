import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';

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
}

/**
 * Cost guardrails (Task 8). Bedrock token spend is the real cost driver, so the
 * primary alarms watch AWS/Bedrock token metrics; a billing alarm is a coarse
 * dollar backstop. All alarms notify one SNS topic.
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

    new cdk.CfnOutput(this, 'AlertTopicArn', {
      value: this.alertTopic.topicArn,
      description: 'Subscribe additional endpoints (Slack, PagerDuty) here.',
    });
    new cdk.CfnOutput(this, 'BedrockTokenAlarmName', { value: tokenAlarm.alarmName });
  }
}
