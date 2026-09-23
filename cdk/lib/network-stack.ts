import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';

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
      description: 'ALB ingress (from CloudFront) / egress to the service',
      allowAllOutbound: true,
    });
    // ALB accepts HTTP from CloudFront. Lock this to the CloudFront managed
    // prefix list in Task 8 so the ALB is not open to the world directly.
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'HTTP from CloudFront (tighten to the CloudFront prefix list in Task 8)',
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
