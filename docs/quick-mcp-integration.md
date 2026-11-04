# Connecting Amazon Quick Suite to the Neurosymbolic MCP server

The MCP server speaks MCP over the official SDK's **Streamable HTTP** transport
(stateless JSON mode) at `POST /mcp`, behind CloudFront → ALB → Fargate, gated
by application-level Cognito JWT auth. This is the shape Amazon Quick Suite's
MCP client prefers (it uses Streamable HTTP and does not require the SSE
stream).

There are two ways Quick authenticates to it. Both use the **existing** Cognito
M2M (client-credentials) app client — no new identity infrastructure.

- **Automatic (OAuth discovery).** The server publishes OAuth 2.0 Protected
  Resource Metadata (RFC 9728) and advertises it in its `401` challenge, so
  Quick can discover the Cognito authorization server on its own.
- **Manual (Service authentication / 2LO).** You paste three values —
  **Client ID, Client Secret, Token URL** — into Quick's connector form.

The manual path always works and is the one to use first; discovery is a
convenience on top of it.

---

## 1. Values you need

| Quick field | Value | Where it comes from |
|---|---|---|
| **MCP server endpoint** | `https://<cloudfront-domain>/mcp` | `DistributionDomain` output of the **Edge** stack |
| **Client ID** | the M2M app client id | `M2mClientId` output of the **Auth** stack |
| **Client Secret** | the M2M client's generated secret | Cognito (see below) |
| **Token URL** | `https://<hosted-ui-domain>.auth.<region>.amazoncognito.com/oauth2/token` | `HostedUiDomain` output of the **Auth** stack |
| **Scope** | `neurosymbolic-mcp/mcp.invoke` | `M2mScope` output of the **Auth** stack |

Values for your deployment (from the Auth stack outputs):

| Field | Value |
|---|---|
| MCP server endpoint | `https://<cloudfront-domain>/mcp` |
| Client ID | `<M2M_CLIENT_ID>` |
| Token URL | `https://<domain-prefix>.auth.<region>.amazoncognito.com/oauth2/token` |
| Scope | `neurosymbolic-mcp/mcp.invoke` |
| User pool id | `<USER_POOL_ID>` |

The hosted-UI domain prefix is `neurosymbolic-dl-<ACCOUNT_ID>` (see
`cdk/lib/auth-stack.ts`), so the token URL is
`https://neurosymbolic-dl-<ACCOUNT_ID>.auth.<region>.amazoncognito.com/oauth2/token`.

### Fetch the live values (admin profile — the service role has no `cognito-idp`)

```bash
# MCP endpoint (CloudFront domain) — Edge stack
aws cloudformation describe-stacks --stack-name NeurosymbolicDL-Edge \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionDomain'].OutputValue" \
  --output text --profile <admin-profile>

# M2M client id, hosted-UI domain, and scope — Auth stack
aws cloudformation describe-stacks --stack-name NeurosymbolicDL-Auth \
  --query "Stacks[0].Outputs[?OutputKey=='M2mClientId'].OutputValue" \
  --output text --profile <admin-profile>
aws cloudformation describe-stacks --stack-name NeurosymbolicDL-Auth \
  --query "Stacks[0].Outputs[?OutputKey=='HostedUiDomain'].OutputValue" \
  --output text --profile <admin-profile>

# the M2M client secret (user pool id is the UserPoolId Auth-stack output)
aws cognito-idp describe-user-pool-client \
  --user-pool-id <UserPoolId> \
  --client-id <M2mClientId> \
  --query 'UserPoolClient.ClientSecret' --output text --profile <admin-profile>
```

> The M2M client id + secret are also mirrored in Secrets Manager under
> `neurosymbolic-mcp/m2m-client` (the MCP task role can read it), if you prefer
> to source them from there.

---

## 2. Manual setup in the Quick console (Service authentication / 2LO)

1. In the Amazon Quick console, open **Connectors**.
2. Choose **Create for your team**, then **Model Context Protocol (MCP)**.
3. Fill in:
   - **Name** — e.g. `Neurosymbolic Ontology QA`.
   - **MCP server endpoint** — `https://<cloudfront-domain>/mcp`.
   - **Connection type** — **Public network** (the endpoint is public via
     CloudFront).
   - **Authentication** — **Service authentication (Service-to-Service)**:
     - **Client ID** — the M2M client id.
     - **Client Secret** — the M2M client secret.
     - **Token URL** — the Cognito `/oauth2/token` URL above.
4. Save. Quick runs the client-credentials grant against the Token URL, gets a
   Cognito **access** token, and sends it as `Authorization: Bearer` on every
   `POST /mcp`. The server's `auth.py` verifier already accepts that token (its
   `client_id` claim is in the container's accepted-audience list).
5. **Enable the tools.** After the connector saves, Quick runs `tools/list` and
   should show one tool per configured ontology:
   `answer_question_with_FMA / GO / CTO / CL / HPO / NCIt / OBI / OGMS / BFO`.
   Enable them for the relevant Quick agent/space.
6. **Smoke-test from the agent.** Ask a question that routes to a tool, e.g.
   *"What is apoptosis?"* → the agent calls `answer_question_with_GO` and
   returns a grounded prose answer with `answered: true`.

> Quick console menu labels vary by release; the **field values** above are
> exact and verified against the live endpoint. The authenticated `tools/list`
> (instant, no reasoning) is the best first check that a token is accepted —
> Quick performs it during connector setup, so a working `tools/list` curl
> (section 2 verify step) predicts a successful handshake.

### Why there is no "scope" field to fill in

Cognito's client-credentials grant normally requires a `scope`, but Quick's
Service-to-Service form does not expose one. That is fine here: when a
client-credentials request **omits** `scope`, Cognito issues a token carrying
**all** custom scopes the app client is configured for. The M2M client is
configured for exactly one scope (`neurosymbolic-mcp/mcp.invoke`), so the
scope-less request Quick sends yields a token with that scope. No shim needed.

### Verify the token path by hand (optional)

```bash
TOKEN=$(curl -s -u '<M2mClientId>:<secret>' \
  -d 'grant_type=client_credentials' \
  https://<domain-prefix>.auth.<region>.amazoncognito.com/oauth2/token \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# MCP initialize through CloudFront (note the required Accept header)
curl -s -X POST https://<cloudfront-domain>/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

A `result` with `serverInfo.name == "ontology-subsumption-mcp"` means auth +
transport are good; `tools/list` then returns one `answer_question_with_<ONTO>`
tool per configured ontology.

---

## 3. Automatic discovery (RFC 9728)

When Cognito auth is configured, the server:

- serves **`GET /.well-known/oauth-protected-resource`** (public), a Protected
  Resource Metadata document whose `authorization_servers` points at the
  Cognito user pool issuer
  (`https://cognito-idp.<region>.amazonaws.com/<poolId>`), and
- sets the `401` response's `WWW-Authenticate` header to
  `Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource"`.

Quick first sends an unauthenticated request; on the `401` it reads
`resource_metadata`, fetches the document, then fetches the Cognito pool's
`/.well-known/openid-configuration` to learn the token endpoint. If the
authorization server advertised Dynamic Client Registration (DCR), Quick could
register itself automatically — Cognito user pools do **not** expose a DCR
`registration_endpoint`, so for Cognito you still provide the M2M client id +
secret manually (section 2). Discovery saves Quick from being told the token
URL, but the client credentials are still supplied by hand.

---

## 4. Notes

- **Stateless transport, no sticky sessions.** The server runs the SDK in
  `stateless_http=True, json_response=True` mode, so every `POST /mcp` is a
  self-contained request answered with a single JSON body — any Fargate task
  can serve any request, which is why no ALB stickiness is required.
- **Host/Origin check.** The SDK's DNS-rebinding protection is disabled on this
  server (the Host header behind CloudFront/ALB is the public domain, not an
  allowlistable fixed value, and the Cognito JWT is the real gate). Do not
  re-enable it without also allowlisting the CloudFront/ALB hosts.
- **Health check stays public.** `GET /mcp/health` and the discovery document
  are the only unauthenticated paths; everything else requires a valid token.
- **Cold-start 504.** The first `tools/call` to a not-yet-loaded ontology can
  exceed CloudFront's 30s origin-response timeout and return `504`; the warm
  call then succeeds (~11s for GO). If Quick's tool-call timeout is short or it
  does not retry, warm each ontology after deploy (one `tools/call` per tool),
  or raise the `/mcp` CloudFront/ALB origin timeout. A cold-start `504` is not
  an auth or grounding failure — `tools/list` (no reasoning) still returns
  instantly.
- **Treat the M2M secret as durable.** It is a non-expiring app-client
  credential. Do not paste it into chat/email; prefer minting a short-lived
  (1h) access token for ad-hoc testers, or create a separate per-tester M2M
  client you can revoke. Rotate with
  `aws cognito-idp update-user-pool-client ...` (invalidates the old secret, so
  reconfigure Quick with the new value).
