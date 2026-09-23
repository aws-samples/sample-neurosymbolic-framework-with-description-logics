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
 * - artifactBucket: optional durable copy of runs/ artifacts.
 * - fileSystem + accessPoint: shared runs/ + *.sqlite3 cache, mounted by BOTH
 *   the web service (reads the best program) and the builder task (writes it).
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

    const commonBucketProps: s3.BucketProps = {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // RETAIN in prod; DESTROY+autoDelete is convenient for a first iteration.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    };

    this.ontologyBucket = new s3.Bucket(this, 'OntologyBucket', commonBucketProps);
    this.artifactBucket = new s3.Bucket(this, 'ArtifactBucket', {
      ...commonBucketProps,
      versioned: true,
    });

    this.fileSystem = new efs.FileSystem(this, 'RunsFs', {
      vpc: props.vpc,
      securityGroup: props.efsSecurityGroup,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      encrypted: true,
      lifecyclePolicy: efs.LifecyclePolicy.AFTER_30_DAYS,
      performanceMode: efs.PerformanceMode.GENERAL_PURPOSE,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // RETAIN in prod
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
