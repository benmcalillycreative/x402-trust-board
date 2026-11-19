// x402-trust-guard: check a seller before your agent pays it.
//
//   import { withTrustGuard } from "x402-trust-guard";
//   withTrustGuard(client);                       // client = your x402Client
//   const pay = wrapFetchWithPayment(fetch, client);
//
// Before every payment the client is about to sign, the guard asks Settle Watch whether
// the endpoint and the address it wants paid look safe, and stops the payment on "avoid"
// (a switched or burn wallet, a 10x overcharge, an address that contradicts the seller's own
// payout proof). Answers are cached, so each endpoint is checked at most once a day.
import { parseTarget, verdict } from "./verdict.js";

export const DEFAULT_API = "https://api.settlewatch.com";
export const DEFAULT_BOARD = "https://settlewatch.com/";

export class PaymentBlocked extends Error {
  constructor(result) {
    super(`x402-trust-guard blocked a payment to ${result.url}: ${result.reasons.join(" ")}`);
    this.name = "PaymentBlocked";
    this.result = result;
  }
}

/**
 * Add the trust check to an x402Client. Returns the same client.
 *
 * @param {object} client  an x402Client (from @x402/core or @x402/fetch)
 * @param {object} [opts]
 * @param {"paid"|"free"} [opts.mode="paid"]  paid: ask the check API (a cent a check, paid with this
 *        same client); free: read the board's public seller files and decide locally
 * @param {"allow"|"block"} [opts.onCaution="allow"]  what to do when the verdict is "caution"
 * @param {"allow"|"block"} [opts.onUnknown="allow"]  sellers the board has never seen
 * @param {"allow"|"block"} [opts.onError="allow"]    the check itself failed (network, board down)
 * @param {number} [opts.cacheSeconds=86400]
 * @param {(result: object) => void} [opts.onResult]  called with every verdict (for logging)
 * @param {typeof fetch} [opts.fetch]
 * @param {string} [opts.api]
 * @param {string} [opts.board]
 */
export function withTrustGuard(client, opts = {}) {
  const o = {
    mode: "paid", onCaution: "allow", onUnknown: "allow", onError: "allow", cacheSeconds: 86400,
    api: DEFAULT_API, board: DEFAULT_BOARD, fetch: globalThis.fetch, ...opts,
  };
  const cache = new Map();
  let paidFetch = null;

  async function lookup(target, payTo) {
    if (o.mode === "free") {
      const res = await o.fetch(`${o.board}seller/${target.host}.json`);
      if (res.status === 404) return verdict(target, null, payTo, o.board);
      if (!res.ok) throw new Error(`board returned HTTP ${res.status}`);
      return verdict(target, await res.json(), payTo, o.board);
    }
    if (!paidFetch) {
      const { wrapFetchWithPayment } = await import("@x402/fetch");
      paidFetch = wrapFetchWithPayment(o.fetch, client);
    }
    const q = new URLSearchParams({ url: target.url });
    if (payTo) q.set("payTo", payTo);
    const res = await paidFetch(`${o.api}/v1/check?${q}`);
    if (!res.ok) throw new Error(`check API returned HTTP ${res.status}`);
    return res.json();
  }

  async function check(url, payTo) {
    const target = parseTarget(url);
    if (!target) return null;
    const key = `${target.noQuery}|${(payTo || "").toLowerCase()}`;
    const hit = cache.get(key);
    if (hit && hit.until > Date.now()) return hit.result;
    const result = await lookup(target, payTo);
    cache.set(key, { result, until: Date.now() + o.cacheSeconds * 1000 });
    return result;
  }

  client.onBeforePaymentCreation(async ({ paymentRequired, selectedRequirements }) => {
    const url = paymentRequired?.resource?.url;
    if (!url || url.startsWith(o.api)) return; // never check the checker
    let result;
    try {
      result = await check(url, selectedRequirements?.payTo);
    } catch (e) {
      o.onResult?.({ url, verdict: "error", reasons: [String(e.message || e)] });
      return o.onError === "block" ? { abort: true, reason: `trust check failed: ${e.message || e}` } : undefined;
    }
    if (!result) return;
    o.onResult?.(result);
    const block =
      result.verdict === "avoid" ||
      (result.verdict === "caution" && o.onCaution === "block") ||
      (result.verdict === "unknown" && o.onUnknown === "block");
    if (block) return { abort: true, reason: new PaymentBlocked(result).message };
  });
  return client;
}

/** Check one URL yourself (free mode, no payment). */
export async function checkFree(url, payTo, { board = DEFAULT_BOARD, fetch: f = globalThis.fetch } = {}) {
  const target = parseTarget(url);
  if (!target) throw new Error("not a URL");
  const res = await f(`${board}seller/${target.host}.json`);
  if (res.status === 404) return verdict(target, null, payTo, board);
  if (!res.ok) throw new Error(`board returned HTTP ${res.status}`);
  return verdict(target, await res.json(), payTo, board);
}
