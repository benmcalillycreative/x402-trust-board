// x402 Trust Board: check-before-you-pay API.
//
//   GET /v1/check?url=<the URL you're about to pay>[&payTo=<the address its 402 asks for>]
//
// Costs PRICE (wrangler.toml) in USDC on Base, paid with x402 (settled through Coinbase's CDP facilitator).
// Returns ok / caution / avoid / unknown with the reasons, from the board's latest checks.
// Free: GET / (how to use it) and GET /v1/health.

import { Hono } from "hono";
import { paymentMiddleware, x402ResourceServer } from "@x402/hono";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { HTTPFacilitatorClient } from "@x402/core/server";
import { createFacilitatorConfig } from "@coinbase/x402";
import { declareDiscoveryExtension, bazaarResourceServerExtension } from "@x402/extensions/bazaar";
import { parseTarget, verdict } from "./check.js";
import { signups } from "./signups.js";
import { mcp } from "./mcp.js";

const BASE = "eip155:8453";
const DEFAULT_PRICE = "$0.01";
const priceOf = (env) => (/^\$\d+(\.\d+)?$/.test(env.PRICE || "") ? env.PRICE : DEFAULT_PRICE);

const EXAMPLE = {
  url: "https://api.example.com/v1/quote",
  host: "api.example.com",
  verdict: "caution",
  reasons: ["Graded C: answers, but less reliably than most."],
  endpoint: { grade: "C", score: 61, uptime: 0.82, p50_latency_ms: 640, listed_usd: 0.01, flags: [] },
  seller: { grade: "B", payout: { verified: true } },
  pay_to: { address: "0x…", proven_by_seller: "dns_signed", matches_listing: true, contradicts_seller_proof: false },
};

function routes(env) {
  return {
    "GET /v1/check": {
      accepts: { scheme: "exact", price: priceOf(env), network: BASE, payTo: env.PAY_TO, maxTimeoutSeconds: 60 },
      description:
        "Check an x402 or MPP endpoint before paying it: uptime, payment-term red flags (switched or " +
        "burn wallets, overcharges) and whether the payout address is proven by the seller. Returns " +
        "ok, caution, avoid or unknown with reasons.",
      mimeType: "application/json",
      serviceName: "x402 Trust Board",
      tags: ["trust", "security", "x402", "due-diligence", "payments"],
      extensions: declareDiscoveryExtension({
        input: { url: "https://api.example.com/v1/quote", payTo: "0x0000000000000000000000000000000000000001" },
        inputSchema: {
          type: "object",
          properties: {
            url: { type: "string", description: "The paid URL you're about to call" },
            payTo: { type: "string", description: "Optional: the address its 402 response asks you to pay" },
          },
          required: ["url"],
        },
        output: { example: EXAMPLE },
      }),
      unpaidResponseBody: () => ({
        contentType: "application/json",
        body: { error: `Payment required: ${priceOf(env)} USDC on Base via x402.`, docs: "/" },
      }),
    },
  };
}

let payment; // built once per isolate: env doesn't change between requests

function paywall(env) {
  if (!payment) {
    const fac = env.FACILITATOR_URL
      ? new HTTPFacilitatorClient({ url: env.FACILITATOR_URL }) // local tests only
      : new HTTPFacilitatorClient(createFacilitatorConfig(env.CDP_API_KEY_ID, env.CDP_API_KEY_SECRET));
    const server = new x402ResourceServer(fac).register(BASE, new ExactEvmScheme());
    server.registerExtension(bazaarResourceServerExtension);
    payment = paymentMiddleware(routes(env), server);
  }
  return payment;
}

async function loadSeller(env, host) {
  const res = await fetch(`${env.BOARD_URL}seller/${host}.json`, {
    cf: { cacheTtl: 600, cacheEverything: true },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`board returned HTTP ${res.status}`);
  return res.json();
}

const app = new Hono();

app.get("/", (c) =>
  c.json({
    name: "x402 Trust Board check API",
    what: "Ask before you pay: is this x402/MPP endpoint up, are its payment terms what it listed, and is the address it wants paid one the seller has proven?",
    call: "GET /v1/check?url=<paid URL>&payTo=<address from its 402, optional>",
    price: `${priceOf(c.env)} USDC on Base (x402 v2, exact scheme)`,
    verdicts: { ok: "fine to pay", caution: "works but has warnings or little history", avoid: "money would go somewhere it shouldn't", unknown: "never seen" },
    method: `${c.env.BOARD_URL}method.html`,
    board: c.env.BOARD_URL,
    data: "Free bulk data: scores.json, payouts.json and seller/<host>.json on the board",
    mcp: "Remote MCP server at /mcp (tool: check_payment), free up to 100 checks a day",
  })
);

app.get("/v1/health", (c) => c.json({ ok: true, payTo: c.env.PAY_TO || null }));

app.route("/v1/alerts", signups);

// Free MCP tool (capped per IP per day) for agents in Claude, Cursor and other MCP clients.
app.all("/mcp", (c) => mcp(c, loadSeller));

// Reject bad input before the paywall, so nobody pays for a typo.
app.use("/v1/check", async (c, next) => {
  if (!c.env.PAY_TO) return c.json({ error: "Not configured yet: PAY_TO is missing." }, 503);
  // No url at all: let the paywall answer 402 with the price, so directories and scanners that probe
  // the bare endpoint can discover it. A url that's there but malformed is refused before the paywall.
  const raw = c.req.query("url");
  if (!raw) return next();
  const target = parseTarget(raw);
  if (!target) return c.json({ error: "Pass the full https:// URL you're about to pay as ?url=" }, 400);
  const payTo = c.req.query("payTo");
  if (payTo && !/^0x[0-9a-fA-F]{40}$/.test(payTo) && !/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(payTo)) {
    return c.json({ error: "payTo should be an 0x… or Solana address" }, 400);
  }
  c.set("target", target);
  return next();
});

app.use("/v1/check", (c, next) => paywall(c.env)(c, next));

app.get("/v1/check", async (c) => {
  const target = c.get("target");
  // Paid without a url (a scanner, say): 4xx responses aren't settled, so nothing is charged.
  if (!target) return c.json({ error: "Pass the full https:// URL you're about to pay as ?url=" }, 400);
  let seller;
  try {
    seller = await loadSeller(c.env, target.host);
  } catch (e) {
    // A 5xx means the middleware won't settle, so the caller isn't charged.
    return c.json({ error: "Couldn't read the board just now; you weren't charged. Try again shortly." }, 503);
  }
  return c.json(verdict(target, seller, c.req.query("payTo"), c.env.BOARD_URL));
});

export default app;
