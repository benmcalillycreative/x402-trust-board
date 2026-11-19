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
//
// It also reads the seller's payout proof from DNS itself (_x402-payto TXT records, see
// docs/payout-proof-spec.md), so a payTo swapped on a compromised server is caught even between the
// board's refreshes. That lookup is cached by the record's TTL (at most 24 hours) and only repeated
// live when the quoted address isn't in the cached set: no DNS round trip on the normal path.
import { parseTarget, verdict } from "./verdict.js";
import { ProofCache, reconcile, withProof } from "./proofs.js";

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
 * @param {boolean} [opts.dnsProofs=true]  check the seller's DNS payout proof live (cached by its TTL)
 * @param {"block"|"allow"} [opts.onProofMismatch="block"]  the seller's DNS proof doesn't list the address
 *        (use "allow" only for sellers that quote a fresh address for every payment)
 * @param {string} [opts.doh]  DNS-over-HTTPS JSON endpoint (default Cloudflare)
 * @param {(result: object) => void} [opts.onResult]  called with every verdict (for logging)
 * @param {typeof fetch} [opts.fetch]
 * @param {string} [opts.api]
 * @param {string} [opts.board]
 */
export function withTrustGuard(client, opts = {}) {
  const o = {
    mode: "paid", onCaution: "allow", onUnknown: "allow", onError: "allow", cacheSeconds: 86400,
    dnsProofs: true, onProofMismatch: "block",
    api: DEFAULT_API, board: DEFAULT_BOARD, fetch: globalThis.fetch, ...opts,
  };
  const cache = new Map();
  const proofs = new ProofCache({ fetch: o.fetch, doh: o.doh, verify: verifySig });
  let paidFetch = null;

  async function lookup(target, payTo) {
    if (o.mode === "free") {
      const res = await o.fetch(`${o.board}seller/${target.host}.json`);
      if (res.status === 404) return verdict(target, null, payTo, o.board);
      if (!res.ok) throw new Error(`board returned HTTP ${res.status}`);
      const seller = await res.json();
      const result = verdict(target, seller, payTo, o.board);
      // The board's file says the address contradicts the seller's proof: ask DNS live, the seller may have rotated.
      if (o.dnsProofs && result.pay_to?.contradicts_seller_proof) {
        const live = await proofs.check(target.host, payTo, { fresh: true });
        return reconcile(result, live, (p) => verdict(target, withProof(seller, p), payTo, o.board));
      }
      return result;
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
    const payTo = selectedRequirements?.payTo;
    const target = parseTarget(url);
    // The seller's own DNS proof, read live (cached by TTL). A mismatch blocks without asking anyone else.
    const live = o.dnsProofs && target ? await proofs.check(target.host, payTo) : null;
    if (live?.status === "mismatch") {
      const result = reconcile({ url, host: target.host, verdict: "avoid", reasons: [], pay_to: { address: payTo } }, live);
      o.onResult?.(result);
      if (o.onProofMismatch === "block") return { abort: true, reason: new PaymentBlocked(result).message };
    }
    let result;
    try {
      result = await check(url, payTo);
    } catch (e) {
      o.onResult?.({ url, verdict: "error", reasons: [String(e.message || e)] });
      return o.onError === "block" ? { abort: true, reason: `trust check failed: ${e.message || e}` } : undefined;
    }
    if (!result) return;
    if (live && live.status !== "mismatch") result = reconcile(result, live);
    o.onResult?.(result);
    const block =
      result.verdict === "avoid" ||
      (result.verdict === "caution" && o.onCaution === "block") ||
      (result.verdict === "unknown" && o.onUnknown === "block");
    if (block) return { abort: true, reason: new PaymentBlocked(result).message };
  });
  return client;
}

// Signature check for DNS proofs, when viem is installed (it comes with @x402/evm). A smart-contract
// wallet's signature doesn't recover to its address without a chain call: that counts as unknown, not invalid.
let viem;
async function verifySig(address, message, signature) {
  if (!address.startsWith("0x")) return null;
  if (viem === undefined) viem = await import("viem").catch(() => null);
  if (!viem) return null;
  const who = await viem.recoverMessageAddress({ message, signature });
  return who.toLowerCase() === address.toLowerCase() ? true : null;
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
