import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';

export interface AuthStackProps extends cdk.StackProps {
  /**
   * Public origin the app is served from (the CloudFront domain, or a custom
   * domain in front of it), used to build the OAuth callback/logout URLs.
   *
   * The ALB `authenticate-cognito` action redirects unauthenticated users to
   * the Cognito hosted UI and Cognito redirects back to
   * `<appDomain>/oauth2/idpresponse` — the fixed path the ALB serves for the
   * auth handshake. Pass e.g. `https://d123.cloudfront.net`. When omitted the
   * client is created without callback URLs (add them once the domain exists).
   */
  appDomain?: string;
}

/**
 * Identity for the app: a Cognito user pool + app client + hosted-UI domain,
 * broken out of EdgeStack so BOTH the ALB HTTPS listener (ComputeStack, which
 * runs the `authenticate-cognito` action) and EdgeStack (outputs) can depend on
 * it without creating an Edge <-> Compute dependency cycle.
 *
 * Invite-only by default (`selfSignUpEnabled: false`); flip on for public
 * sign-up. The app client carries a secret because the ALB auth action uses the
 * confidential authorization-code grant.
 */
export class AuthStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly userPoolDomain: cognito.UserPoolDomain;

  constructor(scope: Construct, id: string, props: AuthStackProps = {}) {
    super(scope, id, props);

    // `-c prod=true` retains the user pool on teardown so real user accounts
    // survive a stack deletion; dev/test keeps DESTROY for a clean teardown.
    const isProd = this.node.tryGetContext('prod') === true
      || this.node.tryGetContext('prod') === 'true';

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: false, // invite-only; flip on if public sign-up is wanted
      signInAliases: { email: true },
      removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    // The ALB authenticate-cognito action needs the confidential
    // authorization-code grant, so the client must have a secret and must list
    // the ALB's fixed auth callback path as a valid callback URL.
    const appDomain = props.appDomain;
    this.userPoolClient = this.userPool.addClient('AppClient', {
      generateSecret: true,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
        // The ALB serves the OAuth handshake at /oauth2/idpresponse on the
        // public origin. Only set callbacks when the domain is known; Cognito
        // rejects an empty/invalid callback list.
        ...(appDomain
          ? {
              callbackUrls: [`${appDomain}/oauth2/idpresponse`],
              logoutUrls: [appDomain],
            }
          : {}),
      },
    });

    this.userPoolDomain = this.userPool.addDomain('HostedUiDomain', {
      cognitoDomain: { domainPrefix: `neurosymbolic-dl-${this.account}` },
    });

    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, 'HostedUiDomain', {
      value: this.userPoolDomain.domainName,
    });
  }
}
