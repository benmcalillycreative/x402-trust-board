// Discovery documents for directories (x402scan reads /openapi.json first, then /.well-known/x402).
export function openapi(env, origin, price) {
  const amount = price.replace("$", "");
  const verdict = {
    type: "object",
    properties: {
      verdict: { type: "string", enum: ["ok", "caution", "avoid", "unknown"] },
      reasons: { type: "array", items: { type: "string" } },
      endpoint: { type: ["object", "null"] },
      seller: { type: ["object", "null"] },
      pay_to: { type: ["object", "null"] },
      data_as_of: { type: "string" },
      data_age_hours: { type: ["number", "null"] },
    },
    required: ["verdict", "reasons"],
  };
  return {
    openapi: "3.1.0",
    info: {
      title: "x402 Trust Board: check before you pay",
      version: "1.0.0",
      description:
        "Independent pre-payment check for x402 and MPP endpoints: does it answer, do its payment terms match its " +
        "listing, and is the address it wants paid one the seller has proven is theirs.",
      "x-guidance":
        "Before paying any x402 or MPP endpoint, call GET /v1/check?url=<the URL you are about to pay>&payTo=<the " +
        "address its 402 asks for>. Don't pay when verdict is 'avoid'. 'caution' means look at the reasons first.",
      contact: { url: env.BOARD_URL },
    },
    servers: [{ url: origin }],
    paths: {
      "/v1/check": {
        get: {
          operationId: "check",
          summary: "Check an endpoint before paying it",
          tags: ["Trust"],
          "x-payment-info": {
            price: { mode: "fixed", currency: "USD", amount },
            protocols: [{ x402: {} }],
          },
          parameters: [
            { name: "url", in: "query", required: true, description: "The paid URL you're about to call",
              schema: { type: "string", format: "uri" } },
            { name: "payTo", in: "query", required: false, description: "The address its 402 response asks you to pay",
              schema: { type: "string" } },
          ],
          responses: {
            200: { description: "Verdict", content: { "application/json": { schema: verdict } } },
            400: { description: "The url is missing or malformed (not charged)" },
            402: { description: "Payment Required" },
          },
        },
      },
      "/v1/health": { get: { operationId: "health", summary: "Liveness and payout address", security: [],
        responses: { 200: { description: "OK" } } } },
      "/mcp": { post: { operationId: "mcp", summary: "Free remote MCP server (tool: check_payment), 100 checks a day",
        security: [], responses: { 200: { description: "JSON-RPC response" } } } },
    },
  };
}

export function wellKnown(origin) {
  return { version: 1, resources: [`${origin}/v1/check`] };
}
