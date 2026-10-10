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
  /**
   * Public (no-secret) app client for the browser SPA. Uses the
   * authorization-code grant with PKCE against the hosted UI; the SPA attaches
   * the resulting id token as `Authorization: Bearer` on its API/MCP calls,
   * which the app-level Cognito middleware verifies. Separate from
   * `userPoolClient` (confidential, for the ALB auth action / M2M) because a
   * browser client must not hold a client secret.
   */
  public readonly spaClient: cognito.UserPoolClient;
  /**
   * Machine-to-machine (M2M) confidential app client for headless callers (the
   * MCP server's programmatic clients). Uses the OAuth **client-credentials**
   * grant: the caller POSTs its client id + secret to the hosted-UI token
   * endpoint and gets back an *access* token (no user, no browser). That token
   * carries this client's id in its `client_id` claim, which the app-level
   * Cognito middleware accepts the same way it accepts the SPA/ALB clients.
   *
   * Client-credentials requires a custom OAuth scope on a resource server --
   * Cognito forbids the standard `openid`/`email` scopes with that grant -- so
   * this client is scoped to {@link resourceServer}'s `mcp.invoke` scope.
   */
  public readonly m2mClient: cognito.UserPoolClient;
  /** Resource server that owns the custom M2M scope (`<identifier>/mcp.invoke`). */
  public readonly resourceServer: cognito.UserPoolResourceServer;
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

    // Public SPA client (no secret) for the browser login flow. The SPA runs
    // the authorization-code + PKCE grant against the hosted UI and sends the
    // id token to the API. Callback/logout URLs point at the app origin's SPA
    // root (not the ALB's /oauth2/idpresponse path, which is only for the ALB
    // authenticate-cognito action). Only wired when the domain is known.
    this.spaClient = this.userPool.addClient('SpaClient', {
      generateSecret: false,
      authFlows: {}, // hosted-UI OAuth only; no direct USER_PASSWORD/SRP flows
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
        ...(appDomain
          ? {
              // The SPA handles the redirect at its own root; it reads the
              // `?code=` param there and exchanges it for tokens via PKCE.
              callbackUrls: [appDomain, `${appDomain}/`],
              logoutUrls: [appDomain, `${appDomain}/`],
            }
          : {}),
      },
      preventUserExistenceErrors: true,
    });

    // ---- Machine-to-machine (headless MCP) client ------------------------
    // The client-credentials grant cannot use the standard openid/email
    // scopes, so define a resource server that owns a custom scope and grant
    // it to a confidential M2M client. A headless caller then does:
    //   curl -u <m2mClientId>:<secret> \
    //     -d 'grant_type=client_credentials&scope=<identifier>/mcp.invoke' \
    //     https://<hostedUiDomain>/oauth2/token
    // and sends the returned access token as `Authorization: Bearer` to /mcp.
    const mcpScopeName = 'mcp.invoke';
    this.resourceServer = this.userPool.addResourceServer('McpResourceServer', {
      identifier: 'neurosymbolic-mcp',
      scopes: [
        new cognito.ResourceServerScope({
          scopeName: mcpScopeName,
          scopeDescription: 'Invoke the Neurosymbolic MCP API headlessly',
        }),
      ],
    });

    this.m2mClient = this.userPool.addClient('M2mClient', {
      generateSecret: true, // confidential: the caller holds id + secret
      authFlows: {}, // no user-facing flows; client-credentials only
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [
          cognito.OAuthScope.resourceServer(this.resourceServer, {
            scopeName: mcpScopeName,
            scopeDescription: 'Invoke the Neurosymbolic MCP API headlessly',
          }),
        ],
      },
    });

    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, 'SpaClientId', {
      value: this.spaClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, 'M2mClientId', {
      value: this.m2mClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, 'M2mScope', {
      value: `${this.resourceServer.userPoolResourceServerId}/${mcpScopeName}`,
      description:
        'OAuth scope a headless client requests in the client-credentials grant',
    });
    new cdk.CfnOutput(this, 'HostedUiDomain', {
      value: this.userPoolDomain.domainName,
    });
  }
}
