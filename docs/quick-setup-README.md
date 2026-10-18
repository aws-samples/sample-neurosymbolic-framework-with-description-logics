# Quick Setup — Neurosymbolic Ontology QA MCP server

Hand-off card for configuring **Amazon Quick** to call the Neurosymbolic MCP
tools. Everything server-side is already deployed and verified; you only fill in
a connector. For the full background (how auth works, discovery, transport
internals) see [`quick-mcp-integration.md`](./quick-mcp-integration.md).

---

## Paste these into the Quick MCP connector

| Quick field | Value |
|---|---|
| **MCP server endpoint / URL** | `https://d15j8kb5manvuj.cloudfront.net/mcp` |
| **Transport** | Streamable HTTP (remote MCP) — *not* stdio |
| **Connection type** | Public network |
| **Authentication** | OAuth 2.0 client credentials (Service authentication / Service-to-Service / 2LO) |
| **Token URL** | `https://neurosymbolic-dl-347181052905.auth.us-east-1.amazoncognito.com/oauth2/token` |
| **Client ID** | `1gdhtn870npje732s65t50f62o` |
| **Client Secret** | *(the M2M client secret — see "Getting the secret" below; do not commit it)* |
| **Scope** | `neurosymbolic-mcp/mcp.invoke` *(leave blank if Quick has no scope field — Cognito returns the single configured scope anyway)* |

Account: `347181052905` · Region: `us-east-1` · User pool: `us-east-1_d9KfAPrid`

---

## Steps in the Quick console

1. Open **Connectors** → **Create for your team** → **Model Context Protocol (MCP)**.
2. **Name** it, e.g. `Neurosymbolic Ontology QA`.
3. **MCP server endpoint** → the URL above. **Connection type** → Public network.
4. **Authentication** → **Service authentication** (client credentials), then
   paste **Token URL**, **Client ID**, **Client Secret** (and **Scope** if the
   field exists).
5. **Save.** Quick mints a token and runs `tools/list`; you should see nine tools:
   `answer_question_with_FMA / GO / CTO / CL / HPO / NCIt / OBI / OGMS / BFO`.
6. Enable the tools for the agent/space, then ask e.g. *"What is apoptosis?"* —
   the agent calls `answer_question_with_GO` and returns a grounded answer.

> Menu labels vary by Quick release; the **values** above are exact and verified
> against the live endpoint.

---

## Getting the secret (admin only)

The deploy/service role cannot read it. From an **ndl admin** profile:

```bash
aws cognito-idp describe-user-pool-client --region us-east-1 \
  --user-pool-id us-east-1_d9KfAPrid \
  --client-id 1gdhtn870npje732s65t50f62o \
  --query 'UserPoolClient.ClientSecret' --output text --profile <ndl-admin>
```

It is also mirrored in Secrets Manager under `neurosymbolic-mcp/m2m-client`.

**It is a durable (non-expiring) credential** — don't paste it into chat/email.
For ad-hoc human testers, mint a 1-hour token instead of sharing the secret, or
create a separate revocable per-tester M2M client. If the secret is ever
exposed, rotate it with `aws cognito-idp update-user-pool-client ...` and
reconfigure Quick with the new value.

---

## Smoke-test before pointing Quick at it (optional)

```bash
TOKEN=$(curl -s -u '1gdhtn870npje732s65t50f62o:<SECRET>' \
  -d 'grant_type=client_credentials&scope=neurosymbolic-mcp/mcp.invoke' \
  https://neurosymbolic-dl-347181052905.auth.us-east-1.amazoncognito.com/oauth2/token \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# instant, no reasoning — confirms the token is accepted
curl -s -X POST https://d15j8kb5manvuj.cloudfront.net/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A `result.tools` list = auth + transport are good, and Quick's handshake will
work too.

---

## Known gotcha: cold-start 504

The **first** `tools/call` to a not-yet-loaded ontology can exceed CloudFront's
30-second origin timeout and return **504**. The warm retry succeeds (~11s for
GO). This is **not** an auth or grounding failure. If Quick doesn't retry, warm
each ontology once after a deploy, or raise the `/mcp` origin timeout.
`tools/list` is unaffected (no reasoning).
