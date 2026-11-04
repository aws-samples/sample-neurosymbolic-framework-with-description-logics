import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as path from 'path';

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
      // CloudFront's default origin response timeout is 30s. A grounded MCP/API
      // query (POST /mcp, /api/select) runs the neurosymbolic program plus
      // Bedrock LLM reasoning, which routinely exceeds 30s on a cold or large
      // ontology (e.g. GO, Mondo) — CloudFront then returns a 504 Gateway
      // Timeout while the backend is still working. Raise the read timeout to
      // 60s, the maximum allowed without an AWS quota increase. The ALB idle
      // timeout defaults to 60s, so this is consistent up the chain.
      readTimeout: cdk.Duration.seconds(60),
      keepaliveTimeout: cdk.Duration.seconds(60),
    });

    // Force the SPA shell (index.html) to be revalidated on every load so a
    // browser or CloudFront edge can never keep serving a stale pre-auth
    // bundle after a frontend redeploy. The content-hashed /assets/* keep the
    // long-lived CACHING_OPTIMIZED default (their names change on rebuild, so
    // they are safe to cache forever). This overrides whatever Cache-Control
    // the S3 object carries, so it holds regardless of how dist/ was uploaded.
    const noStoreHeaders = new cloudfront.ResponseHeadersPolicy(this, 'IndexNoStore', {
      customHeadersBehavior: {
        customHeaders: [
          {
            header: 'Cache-Control',
            value: 'no-cache, no-store, must-revalidate',
            override: true,
          },
        ],
      },
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      // Default behavior: the SPA shell from S3 (OAC configured automatically
      // by S3BucketOrigin). This serves `/` (defaultRootObject -> index.html),
      // every client-side route, AND the 403/404 error-page index.html below,
      // so NO-STORE lives here: the shell is always revalidated and a stale
      // pre-auth bundle can never be served after a frontend redeploy. The
      // content-hashed /assets/* get the long-lived cache via their own
      // behavior below (their names change on rebuild, so caching them forever
      // is safe). no-store here overrides whatever Cache-Control the S3 object
      // carries, so it holds regardless of how dist/ was uploaded.
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.frontendBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        responseHeadersPolicy: noStoreHeaders,
      },
      additionalBehaviors: {
        // Content-hashed, immutable build assets: cache aggressively at the
        // edge and in the browser. Safe because every rebuild emits new hashed
        // filenames, so a changed bundle is a new URL, never a stale hit.
        'assets/*': {
          origin: origins.S3BucketOrigin.withOriginAccessControl(this.frontendBucket),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        },
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
        // Remote MCP server (Streamable HTTP, POST /mcp + GET /mcp/health):
        // route to the ALB origin exactly like the API, otherwise /mcp* falls
        // through to the S3 default behavior and gets served the SPA index.html.
        // NB: 'mcp/*' matches /mcp/health but NOT the bare POST /mcp, so an
        // exact 'mcp' behavior is required alongside it.
        'mcp': {
          origin: albOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
          responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.CORS_ALLOW_ALL_ORIGINS,
        },
        'mcp/*': {
          origin: albOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
          responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.CORS_ALLOW_ALL_ORIGINS,
        },
        // RFC 9728 OAuth Protected Resource Metadata, served by the MCP service
        // (GET /.well-known/oauth-protected-resource). Route it to the ALB
        // origin; otherwise it falls through to the S3 default behavior and is
        // served the SPA index.html, so an RFC-9728-aware client (Amazon Quick
        // Suite) following the 401 `resource_metadata` hint gets HTML, not the
        // JSON document. Exact-path behavior: the discovery doc lives at this
        // single path, so no wildcard is needed.
        '.well-known/oauth-protected-resource': {
          origin: albOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
          responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.CORS_ALLOW_ALL_ORIGINS,
        },
      },
      // SPA client-side routing: serve index.html for unknown paths.
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
    });

    // ---- Frontend SPA deployment ----------------------------------------
    // Ship the PRE-BUILT ../frontend/dist into the SPA bucket as part of
    // `cdk deploy`, and invalidate the distribution so the new bundle is live
    // immediately. Previously this was a manual `aws s3 sync` + CloudFront
    // invalidation (see cdk/README.md); folding it in here means a single
    // `cdk deploy` ships the container AND the SPA, so the browser can never be
    // left on a stale pre-build.
    //
    // NB: the SPA must be built with the Cognito VITE_* vars baked in BEFORE
    // `cdk deploy` (the CloudFront redirect URL is build-time config). See
    // cdk/README.md "Frontend (SPA) deploy".
    //
    // One cache-control is applied to every uploaded object here, but the REAL
    // caching is enforced at the edge by the distribution's behaviors above:
    // index.html is forced `no-store` by the default behavior's
    // ResponseHeadersPolicy, while content-hashed `assets/*` get
    // CACHING_OPTIMIZED. Both override the S3 object metadata, so the
    // `immutable` value below is correct for the hashed assets and harmless for
    // index.html (the edge policy wins).
    const distDir = path.join(__dirname, '..', '..', 'frontend', 'dist');
    new s3deploy.BucketDeployment(this, 'FrontendDeployment', {
      sources: [s3deploy.Source.asset(distDir)],
      destinationBucket: this.frontendBucket,
      distribution: this.distribution,
      distributionPaths: ['/*'],
      prune: true,
      cacheControl: [
        s3deploy.CacheControl.setPublic(),
        s3deploy.CacheControl.maxAge(cdk.Duration.days(365)),
        s3deploy.CacheControl.immutable(),
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
