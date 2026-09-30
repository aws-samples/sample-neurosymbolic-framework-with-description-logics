import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as cr from 'aws-cdk-lib/custom-resources';

/**
 * VPC + security groups shared by the storage and compute stacks.
 *
 * - Public subnets for the ALB, private-with-egress for the Fargate tasks.
 * - Gateway VPC endpoint for S3 (free, avoids NAT for S3 traffic). Bedrock is
 *   reached over the internet via NAT here; add an Interface endpoint for
 *   bedrock-runtime to cut NAT cost (see the commented block).
 */
export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly serviceSecurityGroup: ec2.SecurityGroup;
  public readonly efsSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1, // one NAT for egress (Bedrock). Set 0 if you add interface endpoints for everything.
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
    });

    // S3 gateway endpoint — ontology + artifact traffic stays off NAT.
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    // Optional: keep Bedrock traffic private + off NAT.
    // this.vpc.addInterfaceEndpoint('BedrockRuntimeEndpoint', {
    //   service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
    // });

    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc: this.vpc,
      description: 'ALB ingress (from CloudFront only) / egress to the service',
      allowAllOutbound: true,
    });

    // The ALB is internet-facing so CloudFront can reach it, but it must NOT be
    // open to the world directly — that would let anyone bypass CloudFront (and
    // any WAF / auth / caching in front of it). AWS publishes a managed prefix
    // list, `com.amazonaws.global.cloudfront.origin-facing`, holding exactly the
    // CloudFront edge origin-facing CIDRs. Its ID is region-specific and is not
    // a CloudFormation pseudo-parameter, so we resolve it at deploy time with a
    // tiny DescribeManagedPrefixLists custom resource and scope ALB ingress to
    // it. Result: only CloudFront can hit the ALB on 80/443.
    const cloudFrontPrefixList = new cr.AwsCustomResource(this, 'CloudFrontOriginPrefixList', {
      onUpdate: {
        service: 'EC2',
        action: 'describeManagedPrefixLists',
        parameters: {
          Filters: [
            { Name: 'prefix-list-name', Values: ['com.amazonaws.global.cloudfront.origin-facing'] },
          ],
        },
        physicalResourceId: cr.PhysicalResourceId.of('cloudfront-origin-facing-prefix-list'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE,
      }),
      installLatestAwsSdk: false,
    });
    const cloudFrontPrefixListId = cloudFrontPrefixList.getResponseField(
      'PrefixLists.0.PrefixListId',
    );

    // ALB accepts traffic only from the CloudFront origin-facing prefix list.
    // Scoped to 443 ONLY: the origin-facing prefix list has ~55 entries, and
    // each entry consumes a security-group rule allowance. Adding it on both
    // 80 and 443 (~110 rules) exceeds the default 60-rules-per-SG limit
    // (ServiceLimitExceeded). CloudFront reaches the origin over one protocol,
    // and HTTP is redirected to HTTPS at the edge, so the :80 origin rule is
    // unnecessary. Restore a :80 rule only after raising the SG rule quota.
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.prefixList(cloudFrontPrefixListId),
      ec2.Port.tcp(443),
      'HTTPS from CloudFront edge only',
    );

    this.serviceSecurityGroup = new ec2.SecurityGroup(this, 'ServiceSg', {
      vpc: this.vpc,
      description: 'Fargate web/builder tasks',
      allowAllOutbound: true,
    });
    // Only the ALB may reach the app port on the service.
    this.serviceSecurityGroup.addIngressRule(
      this.albSecurityGroup,
      ec2.Port.tcp(8000),
      'App traffic from the ALB only',
    );

    this.efsSecurityGroup = new ec2.SecurityGroup(this, 'EfsSg', {
      vpc: this.vpc,
      description: 'EFS mount targets',
      allowAllOutbound: true,
    });
    // NFS from the Fargate tasks (web service + builder) only.
    this.efsSecurityGroup.addIngressRule(
      this.serviceSecurityGroup,
      ec2.Port.tcp(2049),
      'NFS from Fargate tasks',
    );
  }
}
