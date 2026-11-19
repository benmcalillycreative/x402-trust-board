// Settle Watch: check-before-you-pay API.
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
import { createFacilitatorConfig, createAuthHeader } from "@coinbase/x402";
import { declareDiscoveryExtension, bazaarResourceServerExtension } from "@x402/extensions/bazaar";
import { parseTarget, verdict } from "./check.js";
import { signups } from "./signups.js";
import { mcp } from "./mcp.js";
import { openapi, wellKnown } from "./openapi.js";
import { ICON_PNG_BASE64 } from "./icon.js";
import { registerPlan, planRoute, decodeSettlement } from "./plan.js";
import { countPaid, stats } from "./stats.js";
import { ProofCache, reconcile, withProof } from "./proofs.js";
import { recoverMessageAddress } from "viem";
import { evmPaywall } from "@x402/paywall/evm";
import { analyze, findPayTo, parseListingInput, LISTING_FREE_PER_DAY } from "./listing.js";

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
      serviceName: "Settle Watch",
      iconUrl: "https://api.settlewatch.com/icon.png",
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
    "GET /v1/plan/pay": planRoute(env, BASE),
  };
}

// People who open a paid link in a browser get a wallet page instead of a bare 402. Only the EVM
// handler is bundled (the package's root export carries every chain and is several MB).
const PAYWALL_CONFIG = { appName: "Settle Watch", appLogo: "https://api.settlewatch.com/icon.png" };
const walletPage = {
  generateHtml(paymentRequired, config) {
    const req = (paymentRequired.accepts || []).find((a) => evmPaywall.supports(a)) || paymentRequired.accepts?.[0];
    return evmPaywall.generateHtml(req, paymentRequired, { ...PAYWALL_CONFIG, ...config });
  },
};

let payment; // built once per isolate: env doesn't change between requests

function paywall(env) {
  if (!payment) {
    const fac = env.FACILITATOR_URL
      ? new HTTPFacilitatorClient({ url: env.FACILITATOR_URL }) // local tests only
      : new HTTPFacilitatorClient(createFacilitatorConfig(env.CDP_API_KEY_ID, env.CDP_API_KEY_SECRET));
    const server = new x402ResourceServer(fac).register(BASE, new ExactEvmScheme());
    server.registerExtension(bazaarResourceServerExtension);
    payment = paymentMiddleware(routes(env), server, PAYWALL_CONFIG, walletPage);
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

function cors(c, res) {
  res.headers.set("Access-Control-Allow-Origin", "*");
  res.headers.set("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.headers.set("Access-Control-Max-Age", "86400");
  return res;
}

const CDP = (env) => (env.CDP_PUBLIC_API || "https://api.cdp.coinbase.com/platform").replace(/\/+$/, "");

async function fetchJson(url, init, ms = 15000) {
  let res = await fetch(url, { ...init, signal: AbortSignal.timeout(ms) });
  if (res.status === 429) { // Coinbase rate-limits now and then: one polite retry
    await new Promise((r) => setTimeout(r, 2000));
    res = await fetch(url, { ...init, signal: AbortSignal.timeout(ms) });
  }
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch {}
  if (!res.ok) throw new Error(`HTTP ${res.status}${body?.errorMessage ? `: ${body.errorMessage}` : ""}`);
  return body;
}

async function underLimit(env, ip, kind, perDay) {
  if (!env.SIGNUPS) return true;
  const key = `rl:${kind}:${ip}:${Math.floor(Date.now() / 86400e3)}`;
  const n = parseInt((await env.SIGNUPS.get(key)) || "0", 10) + 1;
  await env.SIGNUPS.put(key, String(n), { expirationTtl: 86400 });
  return n <= perDay;
}

// Coinbase rate-limits anonymous calls from shared Cloudflare addresses (429), so sign them with the CDP key the
// Worker already holds for settling. If signing isn't accepted, fall back to an anonymous call.
async function cdpCall(env, method, path, body, ms) {
  const url = `${CDP(env)}${path}`;
  const base = { "user-agent": "settlewatch/1.0 (+listing check)", ...(body ? { "content-type": "application/json" } : {}) };
  const init = (headers) => ({ method, headers, ...(body ? { body: JSON.stringify(body) } : {}) });
  if (env.CDP_API_KEY_ID && env.CDP_API_KEY_SECRET && !env.CDP_PUBLIC_API) {
    try {
      const u = new URL(url);
      const auth = await createAuthHeader(env.CDP_API_KEY_ID, env.CDP_API_KEY_SECRET, method, u.host, u.pathname);
      return await fetchJson(url, init({ ...base, Authorization: auth }), ms);
    } catch (e) {
      if (!/HTTP (401|403)/.test(String(e?.message))) throw e;
    }
  }
  return fetchJson(url, init(base), ms);
}

async function runListing(env, target, method) {
  let validate = null, validateError = null, merchant = null, merchantError = null, seller;
  try {
    validate = await cdpCall(env, "POST", "/v2/x402/validate", { resource: target.url, method }, 25000);
  } catch (e) {
    validateError = `Coinbase's validator: ${e.message || e}`;
  }
  const payTo = findPayTo(validate?.paymentRequirements);
  if (payTo && /^(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$/.test(payTo)) {
    try {
      merchant = await cdpCall(env, "GET", `/v2/x402/discovery/merchant?payTo=${encodeURIComponent(payTo)}&limit=100&offset=0`, null, 15000);
    } catch (e) {
      merchantError = `${e.message || e}`;
    }
  }
  try {
    seller = await loadSeller(env, target.host);
  } catch {
    seller = undefined; // the board couldn't be read: say nothing about it rather than "not on the board"
  }
  const out = analyze({ target, validate, merchant, seller, validateError, merchantError });
  return {
    url: target.url,
    method,
    checked_at: new Date().toISOString(),
    ...out,
    coinbase: validate && {
      valid: validate.valid, statusCode: validate.statusCode, x402Version: validate.x402Version,
      simulation: validate.simulation, index: validate.index,
    },
    note: "Uses Coinbase's public validator and merchant discovery list plus the board's own unpaid checks. Nothing is paid.",
  };
}

async function listingCheck(c) {
  const parsed = parseListingInput(c.req.query("url"), c.req.query("method"));
  if (parsed.error) return c.json({ error: parsed.error }, 400);
  const { target, method } = parsed;
  const cacheKey = `ls:${method}:${target.url}`;
  if (c.env.SIGNUPS) {
    const hit = await c.env.SIGNUPS.get(cacheKey);
    if (hit) return c.json({ ...JSON.parse(hit), cached: true });
  }
  const ip = c.req.header("cf-connecting-ip") || "unknown";
  // The board's own computer (seller-plan listing reviews) authenticates and isn't held to the public limit.
  const board = c.env.ALERTS_TOKEN && c.req.header("authorization") === `Bearer ${c.env.ALERTS_TOKEN}`;
  const perDay = parseInt(c.env.LISTING_PER_DAY, 10) || LISTING_FREE_PER_DAY;
  if (!board && !(await underLimit(c.env, ip, "listing", perDay))) {
    return c.json({ error: `Free limit reached (${perDay} listing checks a day). Try again tomorrow.` }, 429);
  }
  const result = await runListing(c.env, target, method);
  if (c.env.SIGNUPS && result.coinbase) {
    await c.env.SIGNUPS.put(cacheKey, JSON.stringify(result), { expirationTtl: 600 });
  }
  return c.json(result);
}

const app = new Hono();

app.get("/", (c) =>
  c.json({
    name: "Settle Watch check API",
    what: "Ask before you pay: is this x402/MPP endpoint up, are its payment terms what it listed, and is the address it wants paid one the seller has proven?",
    call: "GET /v1/check?url=<paid URL>&payTo=<address from its 402, optional>",
    price: `${priceOf(c.env)} USDC on Base (x402 v2, exact scheme)`,
    verdicts: { ok: "fine to pay", caution: "works but has warnings or little history", avoid: "money would go somewhere it shouldn't", unknown: "never seen" },
    method: `${c.env.BOARD_URL}method.html`,
    board: c.env.BOARD_URL,
    data: "Free bulk data: scores.json, payouts.json and seller/<host>.json on the board",
    mcp: "Remote MCP server at /mcp (tools: check_payment, check_listing), free up to 100 checks a day",
    listing: "For sellers, free: GET /v1/listing?url=<your endpoint>[&method=POST] explains why it is or isn't in the Bazaar",
    seller_plan: `${c.env.BOARD_URL}pro.html (paid in USDC on Base via x402, prepaid by the month)`,
  })
);

const ICON = Uint8Array.from(atob(ICON_PNG_BASE64), (ch) => ch.charCodeAt(0));
app.get("/icon.png", (c) => c.body(ICON, 200, { "content-type": "image/png", "cache-control": "public, max-age=86400" }));

app.get("/v1/health", (c) => c.json({ ok: true, payTo: c.env.PAY_TO || null }));

app.route("/v1/alerts", signups);

// Public usage numbers: paid checks and plan payments per day, no emails, payers shortened.
app.get("/v1/stats", async (c) => c.json(await stats(c.env), 200, { "cache-control": "public, max-age=300" }));

// The seller plan, paid in USDC through the same x402 middleware (see plan.js).
registerPlan(app, paywall);

// Discovery documents for directories such as x402scan.
app.get("/openapi.json", (c) => c.json(openapi(c.env, new URL(c.req.url).origin, priceOf(c.env))));
app.get("/.well-known/x402", (c) => c.json(wellKnown(new URL(c.req.url).origin)));

// Free MCP tool (capped per IP per day) for agents in Claude, Cursor and other MCP clients.
app.all("/mcp", (c) => mcp(c, loadSeller, runListing));

// Free for sellers: "why isn't my endpoint in the Bazaar?" Readable from the board's own pages.
app.options("/v1/listing", (c) => cors(c, c.body(null, 204)));
app.get("/v1/listing", async (c) => cors(c, await listingCheck(c)));

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

// Count settled checks (aggregate only) so the board can publish how often the check is really paid for.
app.use("/v1/check", async (c, next) => {
  await next();
  const settled = c.res && c.res.status === 200 ? decodeSettlement(c.res.headers.get("PAYMENT-RESPONSE")) : null;
  if (settled) await countPaid(c.env, "checks", settled, priceOf(c.env)).catch(() => {});
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
  const payTo = c.req.query("payTo");
  let result = verdict(target, seller, payTo, c.env.BOARD_URL);
  // The board reads payout proofs a few times a day. When its data says the address contradicts the
  // seller's proof, look at DNS live before answering: the seller may have rotated since.
  if (seller && result.pay_to?.contradicts_seller_proof) {
    const live = await proofCache(c.env).check(target.host, payTo, { fresh: true });
    result = reconcile(result, live, (p) => verdict(target, withProof(seller, p), payTo, c.env.BOARD_URL));
  }
  return c.json(result);
});

// Live DNS proofs, cached per isolate by the spec's policy (proofs.js).
let proofs = null;
const verifySig = async (address, message, signature) => {
  if (!address.startsWith("0x")) return null;
  const who = await recoverMessageAddress({ message, signature });
  return who.toLowerCase() === address.toLowerCase() ? true : null; // a contract wallet doesn't recover: unknown, not false
};
function proofCache(env) {
  if (!proofs) proofs = new ProofCache({ doh: env.DOH_URL || undefined, verify: verifySig });
  return proofs;
}

export default app;
