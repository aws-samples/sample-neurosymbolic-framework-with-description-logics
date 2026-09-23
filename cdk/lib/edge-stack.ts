import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';

export interface EdgeStackProps extends cdk.StackProps {
  loadBalancer: elbv2.IApplicationLoadBalancer;
}

/**
 * Edge + auth:
 *  - CloudFront: default origin = frontend S3 bucket (OAC, private); /api/* ->
 *    the ALB with caching disabled and headers forwarded (SSE-friendly).
 *  - Cognito user pool + app client for the "behind auth" requirement.
 *
 * AUTH WIRING NOTE: with an INTERNET-FACING ALB the robust pattern is an ALB
 * listener `authenticate-cognito` action in front of the target group (so
 * unauthenticated /api/* is bounced to the Cognito hosted UI). That action
 * lives on the listener in ComputeStack; it needs an HTTPS listener + ACM cert
 * + the user pool below. This stack provisions the Cognito resources and the
 * CloudFront distribution; the listener auth action is added once a domain +
 * cert exist (see the TODO). For a quick gate you can instead use a
 * CloudFront + Lambda@Edge / signed-cookie check — heavier, not recommended.
 */
export class EdgeStack extends cdk.Stack {
  public readonly distribution: cloudfront.Distribution;
  public readonly userPool: cognito.UserPool;
  public readonly frontendBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);

    // ---- Frontend SPA bucket (owned here so CloudFront's OAC policy mutation
    // stays in-stack; see StorageStack note on the avoided dependency cycle) --
    this.frontendBucket = new s3.Bucket(this, 'FrontendBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // RETAIN in prod
      autoDeleteObjects: true,
    });

    // ---- Cognito (identity for the app) ----------------------------------
    this.userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: false, // invite-only; flip on if public sign-up is wanted
      signInAliases: { email: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY, // RETAIN in prod
    });
    const userPoolClient = this.userPool.addClient('AppClient', {
      generateSecret: true,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
        // callbackUrls: [`https://<distribution-domain>/oauth2/idpresponse`], // set after the domain exists
      },
    });
    this.userPool.addDomain('HostedUiDomain', {
      cognitoDomain: { domainPrefix: `neurosymbolic-dl-${this.account}` },
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
    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: userPoolClient.userPoolClientId });
  }
}
