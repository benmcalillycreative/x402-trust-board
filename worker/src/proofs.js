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

const ADDRESS = /^(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$/; // EVM, or Solana (base58)

/** One TXT record -> a proof, { malformed: true } for an x402payto1 record that can't be used, or null. */
export function parseRecord(text, domain) {
  const f = {};
  for (const part of text.split(";")) {
    const i = part.indexOf("=");
    if (i > 0) f[part.slice(0, i).trim().toLowerCase()] = part.slice(i + 1).trim();
  }
  if (f.v !== "x402payto1") return null; // some other TXT record at that name
  if (!f.a || !ADDRESS.test(f.a)) return { malformed: true };
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
    const parsed = answers.map((a) => parseRecord(joinTxt(a.data), name)).filter(Boolean);
    return { records: parsed.filter((r) => !r.malformed), malformed: parsed.some((r) => r.malformed), ttl };
  }

  async load(host) {
    const proofs = new Map(); // address -> level
    const details = new Map(); // address -> { signature, issued } (spec 6.2: signature status rides with the proof)
    let ttl = MAX_SECONDS;
    let malformed = false;
    for (const name of dnsNames(host)) {
      const r = await this.query(name);
      ttl = Math.min(ttl, r.ttl);
      malformed = malformed || r.malformed;
      for (const rec of r.records) {
        let signature = rec.signature ? "unverifiable" : "none";
        if (rec.signature && rec.issued && this.verify) {
          try {
            const ok = await this.verify(rec.address, message(rec.domain, rec.address, rec.issued), rec.signature);
            // false only when the verifier is sure (it can check contract wallets); null means "can't tell here".
            if (ok === true) signature = "valid";
            else if (ok === false) signature = "invalid";
          } catch { /* unverifiable here; still a DNS proof */ }
        }
        const key = norm(rec.address);
        if (proofs.get(key) === "dns_signed") continue;
        proofs.set(key, signature === "valid" ? "dns_signed" : "dns");
        details.set(key, { signature, issued: rec.issued });
      }
    }
    const now = this.now();
    const entry = { proofs, details, malformed, fetchedAt: now,
      until: now + Math.max(MIN_SECONDS, Math.min(ttl, MAX_SECONDS)) * 1000 };
    this.entries.set(norm(host), entry);
    return entry;
  }

  /**
   * Is payTo in the seller's live DNS proofs? `outcome` uses the spec's names (section 6.2); `status` is the
   * older field, kept for compatibility.
   *   outcome "absent"     status "none"      no DNS proof (absence proves nothing), or shared hosting
   *   outcome "malformed"  status "none"      an x402payto1 record exists but can't be used; decide as if absent
   *   outcome "verified"   status "match"     it's proven: { level, signature, issued }
   *   outcome "mismatch"   status "mismatch"  proofs exist and this address isn't in them: { proven, rechecked }
   *   outcome "unchecked"  status "error"     DNS couldn't be read; decide on other evidence
   * signature: "valid" | "invalid" | "unverifiable" | "none" (a contract wallet without a chain check is
   * "unverifiable", never "invalid").
   * fresh: true skips the cache (still rate-limited per domain).
   */
  async check(host, payTo, { fresh = false } = {}) {
    if (!payTo || !dnsNames(host).length) return { status: "none", outcome: "absent" };
    const none = (e) => ({ status: "none", outcome: e.malformed ? "malformed" : "absent" });
    const match = (e) => ({ status: "match", outcome: "verified", level: e.proofs.get(norm(payTo)),
      ...(e.details?.get(norm(payTo)) || {}) });
    const key = norm(host);
    try {
      let entry = this.entries.get(key);
      const canRefetch = !entry || this.now() - entry.fetchedAt >= RECHECK_SECONDS * 1000;
      let rechecked = false;
      if (!entry || this.now() >= entry.until || (fresh && canRefetch)) {
        entry = await this.load(host);
        rechecked = true;
      }
      if (!entry.proofs.size) return none(entry);
      if (entry.proofs.has(norm(payTo))) return match(entry);
      if (!rechecked && canRefetch) {
        entry = await this.load(host); // the only path that pays for a live lookup
        rechecked = true;
        if (entry.proofs.has(norm(payTo))) return match(entry);
        if (!entry.proofs.size) return none(entry);
      }
      return { status: "mismatch", outcome: "mismatch", proven: [...entry.proofs.keys()], rechecked };
    } catch (e) {
      return { status: "error", outcome: "unchecked", error: String(e.message || e) };
    }
  }
}

const PROOF_REASON = "The address you're about to pay isn't in the payout proof this seller published.";
const ROTATED = "The seller's DNS payout proof lists this address now (it changed after the board's last check).";

/** The board's seller data with one more proof, for rebuilding a verdict after a live match. */
export const withProof = (seller, proof) =>
  seller ? { ...seller, payout: { ...(seller.payout || {}), proofs: [...(seller.payout?.proofs || []), proof] } } : seller;

/** The spec-named fields of a live check, for the result (spec 6.2). */
const liveFields = (live) => ({
  dns_outcome: live.outcome || null,
  ...(live.signature ? { dns_signature: live.signature } : {}),
  ...(live.issued ? { dns_issued: live.issued } : {}),
});

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
    if (!rerun) return { ...result, dns_live: live.status, ...liveFields(live) };
    const out = rerun({ address: result.pay_to.address, level: live.level });
    return { ...out, verdict: out.verdict === "ok" ? "caution" : out.verdict, reasons: [...(out.reasons || []), ROTATED],
      dns_live: live.status, ...liveFields(live) };
  }
  const out = { ...result, reasons: [...(result.reasons || [])], pay_to: { ...result.pay_to }, dns_live: live.status,
    ...liveFields(live) };
  if (live.status === "match" && !out.pay_to.proven_by_seller) out.pay_to.proven_by_seller = live.level;
  if (live.status === "mismatch") {
    out.pay_to.contradicts_seller_proof = true;
    if (!out.reasons.includes(PROOF_REASON)) out.reasons.push(PROOF_REASON);
    out.verdict = "avoid";
  }
  return out;
}
