import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';

export interface EdgeStackProps extends cdk.StackProps {
  loadBalancer: elbv2.IApplicationLoadBalancer;
  /** Cognito user pool from AuthStack (identity is owned there, not here). */
  userPool: cognito.IUserPool;
}

/**
 * Edge:
 *  - Frontend SPA S3 bucket (owned here so CloudFront's OAC policy mutation
 *    stays in-stack; see StorageStack note on the avoided Storage <-> Edge
 *    dependency cycle).
 *  - CloudFront: default origin = frontend S3 bucket (OAC, private); /api/* ->
 *    the ALB with caching disabled and headers forwarded (SSE-friendly).
 *
 * AUTH WIRING NOTE: identity (the Cognito user pool/client/domain) now lives in
 * AuthStack. With an INTERNET-FACING ALB the auth enforcement is an ALB HTTPS
 * listener `authenticate-cognito` action in front of the target group (added in
 * ComputeStack, which consumes AuthStack's user pool). This stack only fronts
 * the app with CloudFront and echoes the user-pool id as an output for
 * convenience.
 */
export class EdgeStack extends cdk.Stack {
  public readonly distribution: cloudfront.Distribution;
  public readonly frontendBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);

    // `-c prod=true` retains the SPA bucket on teardown; dev/test keeps
    // DESTROY + autoDelete (mutually exclusive with RETAIN).
    const isProd = this.node.tryGetContext('prod') === true
      || this.node.tryGetContext('prod') === 'true';

    // ---- Frontend SPA bucket (owned here so CloudFront's OAC policy mutation
    // stays in-stack; see StorageStack note on the avoided dependency cycle) --
    this.frontendBucket = new s3.Bucket(this, 'FrontendBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !isProd,
    });

    // ---- CloudFront ------------------------------------------------------
    const albOrigin = new origins.LoadBalancerV2Origin(props.loadBalancer, {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY, // ALB is HTTP; front it with HTTPS at CloudFront
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      // Default behavior: the SPA from S3 (OAC configured automatically by S3BucketOrigin).
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.frontendBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      additionalBehaviors: {
        // API + SSE: no caching, forward everything, allow all methods.
        'api/*': {
          origin: albOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
          // SSE streams must not be buffered by CloudFront.
          responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.CORS_ALLOW_ALL_ORIGINS,
        },
      },
      // SPA client-side routing: serve index.html for unknown paths.
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
    });

    new cdk.CfnOutput(this, 'DistributionDomain', {
      value: this.distribution.distributionDomainName,
    });
    new cdk.CfnOutput(this, 'FrontendBucketName', { value: this.frontendBucket.bucketName });
    // Identity lives in AuthStack (which outputs the client id + hosted-UI
    // domain); echoed here for convenience alongside the distribution domain.
    new cdk.CfnOutput(this, 'UserPoolId', { value: props.userPool.userPoolId });
  }
}
