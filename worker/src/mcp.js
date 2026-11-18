// A remote MCP server (Streamable HTTP, stateless) so agents in Claude, Cursor and other MCP
// clients can ask "is this safe to pay?" with no code and no wallet. Free, capped per IP per day.
//
//   POST /mcp   JSON-RPC: initialize, tools/list, tools/call (check_payment), ping
import { parseTarget, verdict } from "./check.js";
import { parseListingInput } from "./listing.js";

const VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
export const FREE_PER_DAY = 100;

const TOOL = {
  name: "check_payment",
  title: "Check before paying an x402 / MPP endpoint",
  description:
    "Before paying an agent-payable API (x402 or MPP), check whether it is safe: does it answer reliably, " +
    "do its payment terms match its listing (no switched or burn wallets, no overcharges), and is the address " +
    "it asks you to pay one the seller has proven is theirs. Returns verdict ok | caution | avoid | unknown with reasons. " +
    "Pass the payTo address from the 402 response when you have it.",
  inputSchema: {
    type: "object",
    properties: {
      url: { type: "string", description: "The full https:// URL you are about to pay" },
      payTo: { type: "string", description: "Optional: the address its 402 response asks you to pay" },
    },
    required: ["url"],
  },
  annotations: { readOnlyHint: true, openWorldHint: true },
};

const LISTING_TOOL = {
  name: "check_listing",
  title: "Why isn't my x402 endpoint in the Bazaar?",
  description:
    "For sellers: check whether an x402 endpoint is listed in Coinbase's Bazaar and, if not, why. Runs Coinbase's public " +
    "validator (the indexer's own checks), looks the payout address up in Coinbase's merchant list, and adds the " +
    "x402 Trust Board's 30-day availability against Coinbase's 99% bar for featured listings. Returns findings with a fix for each.",
  inputSchema: {
    type: "object",
    properties: {
      url: { type: "string", description: "The full https:// URL of your paid endpoint" },
      method: { type: "string", enum: ["GET", "POST"], description: "HTTP method of the route (default GET)" },
    },
    required: ["url"],
  },
  annotations: { readOnlyHint: true, openWorldHint: true },
};

const rpc = (id, result) => ({ jsonrpc: "2.0", id, result });
const rpcErr = (id, code, message) => ({ jsonrpc: "2.0", id, error: { code, message } });

async function allowed(env, ip) {
  if (!env.SIGNUPS) return true;
  const key = `rl:mcp:${ip}:${Math.floor(Date.now() / 86400e3)}`;
  const n = parseInt((await env.SIGNUPS.get(key)) || "0", 10) + 1;
  await env.SIGNUPS.put(key, String(n), { expirationTtl: 86400 });
  return n <= FREE_PER_DAY;
}

async function callListing(env, args, ip, runListing) {
  const parsed = parseListingInput(args?.url, args?.method);
  if (parsed.error) return { isError: true, content: [{ type: "text", text: parsed.error }] };
  if (!(await allowed(env, ip))) {
    return { isError: true, content: [{ type: "text", text: `Free limit reached (${FREE_PER_DAY} checks a day). Try again tomorrow.` }] };
  }
  const out = await runListing(env, parsed.target, parsed.method);
  const lines = out.findings.map((f) => `- [${f.status}] ${f.title}: ${f.detail}${f.fix ? ` Fix: ${f.fix}` : ""}`);
  return { content: [{ type: "text", text: `${out.summary}\n\n${lines.join("\n")}` }], structuredContent: out };
}

async function callTool(env, args, ip, loadSeller) {
  const target = parseTarget(args?.url);
  if (!target) return { isError: true, content: [{ type: "text", text: "Pass the full https:// URL you're about to pay as `url`." }] };
  if (!(await allowed(env, ip))) {
    return { isError: true, content: [{ type: "text", text:
      `Free limit reached (${FREE_PER_DAY} checks a day). Unlimited checks: GET /v1/check?url=... paid per call with x402.` }] };
  }
  const seller = await loadSeller(env, target.host);
  const out = verdict(target, seller, args?.payTo, env.BOARD_URL);
  const line = `${out.verdict.toUpperCase()}: ${out.reasons.join(" ")}`;
  return { content: [{ type: "text", text: `${line}\n\n${JSON.stringify(out, null, 2)}` }], structuredContent: out };
}

async function handle(env, msg, ip, loadSeller, runListing) {
  const { id, method, params } = msg || {};
  if (id === undefined || id === null) return null; // a notification: nothing to send back
  if (method === "initialize") {
    const asked = params?.protocolVersion;
    return rpc(id, {
      protocolVersion: VERSIONS.includes(asked) ? asked : VERSIONS[0],
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "x402-trust-board", title: "x402 Trust Board", version: "1.0.0" },
      instructions: "Call check_payment before paying any x402 or MPP endpoint. Don't pay when the verdict is avoid. " +
        "Sellers: call check_listing to find out why an endpoint isn't in Coinbase's Bazaar.",
    });
  }
  if (method === "ping") return rpc(id, {});
  if (method === "tools/list") return rpc(id, { tools: runListing ? [TOOL, LISTING_TOOL] : [TOOL] });
  if (method === "tools/call") {
    const isListing = runListing && params?.name === LISTING_TOOL.name;
    if (params?.name !== TOOL.name && !isListing) return rpcErr(id, -32602, `Unknown tool: ${params?.name}`);
    try {
      if (isListing) return rpc(id, await callListing(env, params.arguments || {}, ip, runListing));
      return rpc(id, await callTool(env, params.arguments || {}, ip, loadSeller));
    } catch (e) {
      return rpc(id, { isError: true, content: [{ type: "text", text: "Couldn't read the board just now. Try again shortly." }] });
    }
  }
  return rpcErr(id, -32601, `Method not found: ${method}`);
}

export async function mcp(c, loadSeller, runListing) {
  if (c.req.method !== "POST") return c.json(rpcErr(null, -32000, "Use POST (Streamable HTTP, stateless)."), 405);
  let body;
  try {
    body = await c.req.json();
  } catch {
    return c.json(rpcErr(null, -32700, "Parse error"), 400);
  }
  const ip = c.req.header("cf-connecting-ip") || "unknown";
  const msgs = Array.isArray(body) ? body : [body];
  const out = (await Promise.all(msgs.map((m) => handle(c.env, m, ip, loadSeller, runListing)))).filter(Boolean);
  if (!out.length) return c.body(null, 202);
  return c.json(Array.isArray(body) ? out : out[0]);
}
