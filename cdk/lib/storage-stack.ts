import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as efs from 'aws-cdk-lib/aws-efs';

export interface StorageStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  efsSecurityGroup: ec2.ISecurityGroup;
}

/**
 * S3 buckets + EFS filesystem.
 *
 * - ontologyBucket: source OWL files (the app already speaks s3:// via
 *   ontology_qa_cli/s3_cache.py).
 * - artifactBucket: optional durable copy of runs/ artifacts (versioned, with
 *   lifecycle: old versions -> IA after 30d, expire after 90d).
 * - fileSystem + accessPoint: shared runs/ + *.sqlite3 cache, mounted by BOTH
 *   the web service (reads the best program) and the builder task (writes it).
 *   AWS Backup automatic daily backups enabled.
 *
 * All buckets abort incomplete multipart uploads after 7 days.
 *
 * NOTE: the frontend SPA bucket lives in EdgeStack, not here. CloudFront's OAC
 * mutates the bucket policy to reference the distribution; keeping the bucket
 * and distribution in one stack avoids a Storage<->Edge dependency cycle.
 */
export class StorageStack extends cdk.Stack {
  public readonly ontologyBucket: s3.Bucket;
  public readonly artifactBucket: s3.Bucket;
  public readonly fileSystem: efs.FileSystem;
  public readonly accessPoint: efs.AccessPoint;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    // `-c prod=true` retains data on stack deletion (buckets, EFS) so a teardown
    // can't wipe prod state. Default (dev/test) keeps DESTROY + autoDelete for a
    // clean, no-orphan teardown. RETAIN and autoDeleteObjects are mutually
    // exclusive, so autoDelete is disabled whenever we retain.
    const isProd = this.node.tryGetContext('prod') === true
      || this.node.tryGetContext('prod') === 'true';
    const dataRemovalPolicy = isProd
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    const commonBucketProps: s3.BucketProps = {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // RETAIN under `-c prod=true`; DESTROY+autoDelete is convenient for a
      // first iteration. autoDelete is off when retaining (they conflict).
      removalPolicy: dataRemovalPolicy,
      autoDeleteObjects: !isProd,
      // Reclaim storage from failed/abandoned multipart uploads everywhere.
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
      ],
    };

    this.ontologyBucket = new s3.Bucket(this, 'OntologyBucket', commonBucketProps);
    this.artifactBucket = new s3.Bucket(this, 'ArtifactBucket', {
      ...commonBucketProps,
      versioned: true,
      // Durable copy of runs/ artifacts: keep current objects, but cap the cost
      // of old versions and cool down infrequently-accessed artifacts.
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
        {
          // Old versions are recovery points, not hot data — expire after 90d,
          // move to cheaper storage well before that.
          noncurrentVersionExpiration: cdk.Duration.days(90),
          noncurrentVersionTransitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
        },
      ],
    });

    this.fileSystem = new efs.FileSystem(this, 'RunsFs', {
      vpc: props.vpc,
      securityGroup: props.efsSecurityGroup,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      encrypted: true,
      lifecyclePolicy: efs.LifecyclePolicy.AFTER_30_DAYS,
      performanceMode: efs.PerformanceMode.GENERAL_PURPOSE,
      // AWS Backup daily plan (35-day retention) on the default vault, so the
      // shared runs/ + sqlite cache is recoverable. This is the managed plan;
      // swap for a custom BackupPlan if a different cadence/retention is needed.
      enableAutomaticBackups: true,
      removalPolicy: dataRemovalPolicy, // RETAIN under `-c prod=true`
    });

    // POSIX identity + root the containers see. Both tasks mount this AP so
    // they share one runs/ + sqlite cache tree.
    this.accessPoint = this.fileSystem.addAccessPoint('RunsAp', {
      path: '/runs',
      createAcl: { ownerGid: '1000', ownerUid: '1000', permissions: '750' },
      posixUser: { gid: '1000', uid: '1000' },
    });
  }
}
