// Live payout-proof checks (docs/payout-proof-spec.md), shared by the check API and x402-trust-guard.
// packages/x402-trust-guard/proofs.js must stay a copy of this file (a test enforces it).
//
// Cache policy (spec section 6.1): keep a domain's proof set for min(record TTL, 24h). A quoted payTo
// that's in the set costs nothing. Only an address that isn't in it triggers a fresh DNS lookup before
// deciding, and those re-checks are rate-limited per domain so a hostile server can't make the client
// hammer DNS. A legitimate rotation passes the re-check; a swapped address on a compromised server
// fails it, because the attacker can't change the domain's DNS.

export const DOH = "https://cloudflare-dns.com/dns-query";
export const MAX_SECONDS = 86400;
export const MIN_SECONDS = 60;
export const RECHECK_SECONDS = 60;

// Hosting platforms where many unrelated people share one parent domain: a record there proves nothing
// about a customer's subdomain (spec 3.1). Same list as the board's x402trust/payout.py.
export const SHARED_SUFFIXES = [
  "workers.dev", "pages.dev", "onrender.com", "vercel.app", "netlify.app", "fly.dev",
  "up.railway.app", "railway.app", "herokuapp.com", "github.io", "hf.space", "sslip.io",
  "nip.io", "run.app", "appspot.com", "amazonaws.com", "cloudfront.net", "azurewebsites.net",
  "duckdns.org", "ngrok.app", "ngrok-free.app", "replit.app", "repl.co", "deno.dev",
  "supabase.co", "firebaseapp.com", "web.app", "elestio.app", "base44.app",
];
const TWO_PART = ["co.uk", "org.uk", "com.au", "net.au", "co.jp", "com.br", "co.nz", "co.in", "com.mx"];

const norm = (a) => String(a || "").trim().toLowerCase();

/** The host and its parents a DNS proof could sit on; empty for IPs and shared hosting. */
export function dnsNames(host) {
  const h = norm(host).replace(/\.$/, "");
  if (!h.includes(".") || /^[0-9.]+$/.test(h) || h.includes(":")) return [];
  if (SHARED_SUFFIXES.some((s) => h === s || h.endsWith("." + s))) return [];
  const labels = h.split(".");
  const out = [];
  for (let i = 0; i < labels.length - 1; i++) {
    const name = labels.slice(i).join(".");
    if (TWO_PART.includes(name)) break;
    out.push(name);
  }
  return out;
}

/** The signed message (spec section 4). */
export const message = (domain, address, issued) =>
  `x402 payout verification\n\nDomain: ${norm(domain)}\nPay-to: ${address.startsWith("0x") ? norm(address) : address}\nIssued: ${issued}`;

/** One DoH TXT answer's data -> the record text, joining its character strings (signatures exceed 255 bytes). */
export function joinTxt(data) {
  const parts = [...String(data).matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1].replace(/\\(.)/g, "$1"));
  return parts.length ? parts.join("") : String(data);
}

export function parseRecord(text, domain) {
  const f = {};
  for (const part of text.split(";")) {
    const i = part.indexOf("=");
    if (i > 0) f[part.slice(0, i).trim().toLowerCase()] = part.slice(i + 1).trim();
  }
  if (f.v !== "x402payto1" || !f.a) return null;
  return { domain, address: f.a, issued: f.d || null, signature: f.s || null };
}

/**
 * Per-process cache of live DNS proofs.
 *   fetch   fetch implementation
 *   doh     DNS-over-HTTPS JSON endpoint (Cloudflare and Google style ?name=&type=TXT)
 *   verify  optional async (address, message, signature) => true | false | null. true makes the level
 *           "dns_signed"; anything else leaves it "dns" (a smart-contract wallet can't be checked
 *           without a chain call, so a signature that doesn't recover is never treated as a mismatch).
 */
export class ProofCache {
  constructor({ fetch: f = globalThis.fetch, doh = DOH, verify = null, now = () => Date.now() } = {}) {
    this.fetch = (...args) => f(...args); // unbound: Workers reject fetch called as a method
    this.doh = doh; this.verify = verify; this.now = now;
    this.entries = new Map(); // host -> { proofs: Map(address -> level), until, fetchedAt }
  }

  async query(name) {
    const res = await this.fetch(`${this.doh}?name=${encodeURIComponent(`_x402-payto.${name}`)}&type=TXT`,
      { headers: { accept: "application/dns-json" } });
    if (!res.ok) throw new Error(`DNS-over-HTTPS returned HTTP ${res.status}`);
    const j = await res.json();
    if (j.Status !== 0 && j.Status !== 3) throw new Error(`DNS lookup failed (status ${j.Status})`);
    const answers = (j.Answer || []).filter((a) => a.type === 16);
    const ttl = Math.min(...answers.map((a) => a.TTL), ...(j.Authority || []).map((a) => a.TTL), MAX_SECONDS);
    return { records: answers.map((a) => parseRecord(joinTxt(a.data), name)).filter(Boolean), ttl };
  }

  async load(host) {
    const proofs = new Map();
    let ttl = MAX_SECONDS;
    for (const name of dnsNames(host)) {
      const r = await this.query(name);
      ttl = Math.min(ttl, r.ttl);
      for (const rec of r.records) {
        let level = "dns";
        if (rec.signature && rec.issued && this.verify) {
          try {
            if ((await this.verify(rec.address, message(rec.domain, rec.address, rec.issued), rec.signature)) === true) level = "dns_signed";
          } catch { /* unverifiable here; still a DNS proof */ }
        }
        const key = norm(rec.address);
        if (proofs.get(key) !== "dns_signed") proofs.set(key, level);
      }
    }
    const now = this.now();
    const entry = { proofs, fetchedAt: now, until: now + Math.max(MIN_SECONDS, Math.min(ttl, MAX_SECONDS)) * 1000 };
    this.entries.set(norm(host), entry);
    return entry;
  }

  /**
   * Is payTo in the seller's live DNS proofs?
   *   { status: "none" }                         no DNS proof (absence proves nothing), or shared hosting
   *   { status: "match", level }                 it's proven
   *   { status: "mismatch", proven, rechecked }  proofs exist and this address isn't in them
   *   { status: "error", error }                 DNS couldn't be read; decide on other evidence
   * fresh: true skips the cache (still rate-limited per domain).
   */
  async check(host, payTo, { fresh = false } = {}) {
    if (!payTo || !dnsNames(host).length) return { status: "none" };
    const key = norm(host);
    try {
      let entry = this.entries.get(key);
      const canRefetch = !entry || this.now() - entry.fetchedAt >= RECHECK_SECONDS * 1000;
      let rechecked = false;
      if (!entry || this.now() >= entry.until || (fresh && canRefetch)) {
        entry = await this.load(host);
        rechecked = true;
      }
      if (!entry.proofs.size) return { status: "none" };
      if (entry.proofs.has(norm(payTo))) return { status: "match", level: entry.proofs.get(norm(payTo)) };
      if (!rechecked && canRefetch) {
        entry = await this.load(host); // the only path that pays for a live lookup
        rechecked = true;
        if (entry.proofs.has(norm(payTo))) return { status: "match", level: entry.proofs.get(norm(payTo)) };
        if (!entry.proofs.size) return { status: "none" };
      }
      return { status: "mismatch", proven: [...entry.proofs.keys()], rechecked };
    } catch (e) {
      return { status: "error", error: String(e.message || e) };
    }
  }
}

const PROOF_REASON = "The address you're about to pay isn't in the payout proof this seller published.";
const ROTATED = "The seller's DNS payout proof lists this address now (it changed after the board's last check).";

/** The board's seller data with one more proof, for rebuilding a verdict after a live match. */
export const withProof = (seller, proof) =>
  seller ? { ...seller, payout: { ...(seller.payout || {}), proofs: [...(seller.payout?.proofs || []), proof] } } : seller;

/**
 * Combine a verdict built from the board's data (refreshed every few hours) with a live DNS check.
 *   - The board saw a contradiction, but the address is in the seller's DNS proof now (a rotation since
 *     the board's last look): rebuild the verdict with that proof (rerun), and answer no better than
 *     "caution". Without rerun the board's answer stands.
 *   - The board saw nothing wrong, but the live DNS proof doesn't list the address: "avoid".
 */
export function reconcile(result, live, rerun = null) {
  if (!result || !result.pay_to || !live) return result;
  if (live.status === "match" && result.pay_to.contradicts_seller_proof) {
    if (!rerun) return { ...result, dns_live: live.status };
    const out = rerun({ address: result.pay_to.address, level: live.level });
    return { ...out, verdict: out.verdict === "ok" ? "caution" : out.verdict, reasons: [...(out.reasons || []), ROTATED],
      dns_live: live.status };
  }
  const out = { ...result, reasons: [...(result.reasons || [])], pay_to: { ...result.pay_to }, dns_live: live.status };
  if (live.status === "match" && !out.pay_to.proven_by_seller) out.pay_to.proven_by_seller = live.level;
  if (live.status === "mismatch") {
    out.pay_to.contradicts_seller_proof = true;
    if (!out.reasons.includes(PROOF_REASON)) out.reasons.push(PROOF_REASON);
    out.verdict = "avoid";
  }
  return out;
}
