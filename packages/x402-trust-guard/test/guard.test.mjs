// End to end with a real x402 client (throwaway key), a fake seller, a fake board and a fake
// check API. Nothing touches a chain: the fakes accept any signature.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { wrapFetchWithPayment, x402Client } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { withTrustGuard, checkFree } from "../index.js";
import { ProofCache, dnsNames, joinTxt, reconcile } from "../proofs.js";

const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const GOOD = "0x" + "a1".repeat(20);
const BAD = "0x" + "dd".repeat(20);
const SWAP = "0x" + "5a".repeat(20);  // a swapped address the fake API doesn't know about
const NEW = "0x" + "ee".repeat(20);   // a legitimate rotation the board hasn't seen yet
let dns = [], dnsQueries = 0;         // the seller's live _x402-payto records
const HOST = "seller.example";
let server, base, paid = [], checks = 0;

const SELLER = {
  host: HOST, name: "Seller", grade: "A", generated_at: new Date().toISOString(),
  payout: { verified: true, proofs: [{ address: GOOD, level: "dns_signed" }] },
  endpoints: [
    { resource: "https://seller.example/good", grade: "A", score: 95, listed_pay_to: GOOD, flags: [] },
    { resource: "https://seller.example/flagged", grade: "B", score: 80, listed_pay_to: GOOD,
      flags: [{ code: "overcharge_10x", severity: "red", text: "Asks 10x its listed price" }] },
  ],
};

function required(url, payTo) {
  const pr = { x402Version: 2, resource: { url }, accepts: [{ scheme: "exact", network: "eip155:8453", amount: "1000",
    asset: USDC, payTo, maxTimeoutSeconds: 60, extra: { name: "USD Coin", version: "2" } }] };
  return Buffer.from(JSON.stringify(pr)).toString("base64");
}

before(async () => {
  server = http.createServer((req, res) => {
    const url = new URL(req.url, base);
    const signed = req.headers["payment-signature"] || req.headers["x-payment"];
    if (url.pathname === "/dns") {
      dnsQueries++;
      const body = dns.length
        ? { Status: 0, Answer: dns.map((a) => ({ name: url.searchParams.get("name"), type: 16, TTL: 300, data: `"v=x402payto1; a=${a}"` })) }
        : { Status: 3, Authority: [{ TTL: 900 }] };
      res.writeHead(200, { "content-type": "application/dns-json" }); return res.end(JSON.stringify(body));
    }
    if (url.pathname === `/board/seller/${HOST}.json`) { res.writeHead(200); return res.end(JSON.stringify(SELLER)); }
    if (url.pathname.startsWith("/board/seller/")) { res.writeHead(404); return res.end("{}"); }
    if (url.pathname === "/api/v1/check") {
      if (!signed) { res.writeHead(402, { "payment-required": required(`${base}/api/v1/check`, "0x" + "c3".repeat(20)) }); return res.end("{}"); }
      checks++;
      const target = url.searchParams.get("url");
      const v = target.endsWith("/flagged") || url.searchParams.get("payTo")?.toLowerCase() === BAD ? "avoid" : "ok";
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ url: target, host: HOST, verdict: v, reasons: [v === "avoid" ? "red flag" : "fine"] }));
    }
    // The fake seller: /good and /flagged ask GOOD, /hijacked asks BAD. The URL the client sees is
    // https://seller.example/..., routed here by the fetch shim below.
    const payTo = { "/hijacked": BAD, "/swapped": SWAP, "/rotated": NEW }[url.pathname] || GOOD;
    if (!signed) { res.writeHead(402, { "payment-required": required(`https://${HOST}${url.pathname}`, payTo) }); return res.end("{}"); }
    paid.push(url.pathname);
    res.writeHead(200); res.end("paid content");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

// Route https://seller.example/* to the local fake seller.
const shim = (input, init) => {
  const req = input instanceof Request ? input : new Request(input, init);
  const u = new URL(req.url);
  if (u.hostname === HOST) return fetch(new Request(`${base}${u.pathname}${u.search}`, req));
  return fetch(req);
};

function client(opts) {
  const c = new x402Client().register("eip155:*", new ExactEvmScheme(privateKeyToAccount(generatePrivateKey())));
  withTrustGuard(c, { board: `${base}/board/`, api: `${base}/api`, doh: `${base}/dns`, fetch: shim, ...opts });
  return wrapFetchWithPayment(shim, c);
}

test("free mode: pays a good endpoint, refuses a flagged one and a hijacked address", async () => {
  paid = [];
  const pay = client({ mode: "free" });
  assert.equal((await pay(`https://${HOST}/good`)).status, 200);
  await assert.rejects(pay(`https://${HOST}/flagged`), /blocked a payment/);
  await assert.rejects(pay(`https://${HOST}/hijacked`), /isn't in the payout proof/);
  assert.deepEqual(paid, ["/good"]);
});

test("paid mode: one check per endpoint, cached, and the check itself isn't checked", async () => {
  paid = []; checks = 0;
  const pay = client({ mode: "paid" });
  assert.equal((await pay(`https://${HOST}/good`)).status, 200);
  assert.equal((await pay(`https://${HOST}/good`)).status, 200);
  assert.equal(checks, 1, "second payment to the same endpoint uses the cached verdict");
  await assert.rejects(pay(`https://${HOST}/flagged`), /blocked a payment/);
  assert.deepEqual(paid, ["/good", "/good"]);
});

function hookOf(opts) {
  const c = new x402Client();
  let hook;
  c.onBeforePaymentCreation = (h) => { hook = h; return c; };
  withTrustGuard(c, { mode: "free", board: `${base}/board/`, doh: `${base}/dns`, fetch: shim, ...opts });
  return hook;
}

test("unknown sellers pass by default and can be blocked", async () => {
  const res = await checkFree("https://nobody.example/x", null, { board: `${base}/board/`, fetch: shim });
  assert.equal(res.verdict, "unknown");
  const args = { paymentRequired: { resource: { url: "https://nobody.example/x" } }, selectedRequirements: { payTo: GOOD } };
  assert.equal(await hookOf({})(args), undefined);
  assert.equal((await hookOf({ onUnknown: "block" })(args)).abort, true);
});

test("a failed check allows by default and blocks when asked", async () => {
  const mk = (onError) => hookOf({ onError, board: "http://127.0.0.1:1/" });
  const args = { paymentRequired: { resource: { url: `https://${HOST}/good` } }, selectedRequirements: { payTo: GOOD } };
  assert.equal(await mk("allow")(args), undefined);
  assert.equal((await mk("block")(args)).abort, true);
});

test("live DNS proof: a swapped payTo is blocked before any check is bought; the normal path costs no lookups", async () => {
  dns = [GOOD]; dnsQueries = 0; paid = []; checks = 0;
  const pay = client({ mode: "paid" });
  assert.equal((await pay(`https://${HOST}/good`)).status, 200);
  assert.equal((await pay(`https://${HOST}/good`)).status, 200);
  assert.equal(dnsQueries, 1, "one lookup, then cached by TTL");
  await assert.rejects(pay(`https://${HOST}/swapped`), /isn't in the payout proof/);
  assert.equal(dnsQueries, 1, "the re-check is rate-limited right after a lookup");
  assert.equal(checks, 1, "no paid check was bought for the swapped address");
  assert.deepEqual(paid, ["/good", "/good"]);
  dns = [];
});

test("live DNS proof: a rotation the board hasn't seen yet is let through as caution", async () => {
  dns = [GOOD, NEW]; paid = [];
  const seen = [];
  const pay = client({ mode: "free", onResult: (r) => seen.push(r) });
  assert.equal((await pay(`https://${HOST}/rotated`)).status, 200);
  assert.deepEqual(paid, ["/rotated"]);
  const r = seen.at(-1);
  assert.equal(r.verdict, "caution");
  assert.equal(r.pay_to.contradicts_seller_proof, false);
  assert.match(r.reasons.join(" "), /changed after the board's last check/);
  dns = [];
});

test("proof cache: TTL, a live re-check only on a miss, rate-limited, shared hosting skipped", async () => {
  let t = 0, n = 0, records = [GOOD];
  const f = async () => { n++; return new Response(JSON.stringify({ Status: 0, Answer: records.map((a) => ({ type: 16, TTL: 600, data: `"v=x402payto1; " "a=${a}"` })) })); };
  const pc = new ProofCache({ fetch: f, doh: "https://doh.test/q", now: () => t });
  assert.equal((await pc.check("api.seller.example", GOOD)).status, "match");
  assert.equal(n, 2, "host and parent looked up once");
  assert.equal((await pc.check("api.seller.example", GOOD)).status, "match");
  assert.equal(n, 2, "cached");
  t = 30e3;
  assert.equal((await pc.check("api.seller.example", NEW)).status, "mismatch");
  assert.equal(n, 2, "no re-check within a minute of the last lookup");
  t = 90e3; records = [GOOD, NEW];
  assert.equal((await pc.check("api.seller.example", NEW)).status, "match", "after a minute a miss re-checks live and finds the rotation");
  assert.equal(n, 4);
  t = 90e3 + 601e3;
  await pc.check("api.seller.example", GOOD);
  assert.equal(n, 6, "expired after the record's TTL");
  assert.equal((await pc.check("x.onrender.com", GOOD)).status, "none");
  assert.deepEqual(dnsNames("a.b.co.uk"), ["a.b.co.uk", "b.co.uk"]);
  assert.equal(joinTxt('"v=x402payto1; a=0x1" "; s=0xab"'), "v=x402payto1; a=0x1; s=0xab");
  const failing = new ProofCache({ fetch: async () => { throw new Error("offline"); } });
  assert.equal((await failing.check("api.seller.example", GOOD)).status, "error", "DNS down never blocks by itself");
  assert.equal(reconcile({ verdict: "ok", reasons: [], pay_to: { address: SWAP } }, { status: "error" }).verdict, "ok");
});

test("proof cache reports the spec's outcomes and the signature status (spec 6.2)", async () => {
  let txt = [];
  const f = async () => new Response(JSON.stringify({ Status: 0, Answer: txt.map((d) => ({ type: 16, TTL: 600, data: `"${d}"` })) }));
  const run = async (records, verify, payTo = GOOD) => {
    txt = records;
    return new ProofCache({ fetch: f, doh: "https://doh.test/q", verify, now: () => 0 }).check("seller.example", payTo);
  };
  assert.equal((await run([], null)).outcome, "absent");
  assert.equal((await run(["v=spf1 -all"], null)).outcome, "absent", "other TXT records aren't proofs");
  assert.equal((await run(["v=x402payto1; d=2026-11-19"], null)).outcome, "malformed", "no address");
  assert.equal((await run(["v=x402payto1; a=not-an-address"], null)).outcome, "malformed");
  let r = await run([`v=x402payto1; a=${GOOD}; d=2026-11-19; s=0xabc`], async () => true);
  assert.deepEqual([r.outcome, r.level, r.signature, r.issued], ["verified", "dns_signed", "valid", "2026-11-19"]);
  r = await run([`v=x402payto1; a=${GOOD}; d=2026-11-19; s=0xabc`], async () => null);
  assert.deepEqual([r.outcome, r.level, r.signature], ["verified", "dns", "unverifiable"], "a contract wallet isn't invalid");
  r = await run([`v=x402payto1; a=${GOOD}; d=2026-11-19; s=0xabc`], async () => false);
  assert.deepEqual([r.outcome, r.level, r.signature], ["verified", "dns", "invalid"], "a bad signature still binds the address");
  r = await run([`v=x402payto1; a=${GOOD}`], null);
  assert.deepEqual([r.outcome, r.signature], ["verified", "none"]);
  assert.equal((await run([`v=x402payto1; a=${GOOD}; s=0xabc`], async () => true, NEW)).outcome, "mismatch",
    "a mismatch whatever the signature says");
  const failing = new ProofCache({ fetch: async () => { throw new Error("offline"); } });
  assert.equal((await failing.check("seller.example", GOOD)).outcome, "unchecked");
  const out = reconcile({ verdict: "ok", reasons: [], pay_to: { address: GOOD } },
    { status: "match", outcome: "verified", level: "dns", signature: "invalid", issued: "2026-11-19" });
  assert.deepEqual([out.verdict, out.dns_outcome, out.dns_signature, out.dns_issued], ["ok", "verified", "invalid", "2026-11-19"]);
});

test("proofs.js is the same file in the API and the guard", () => {
  const here = fs.readFileSync(new URL("../proofs.js", import.meta.url), "utf8");
  const api = fs.readFileSync(new URL("../../../worker/src/proofs.js", import.meta.url), "utf8");
  assert.equal(here, api, "packages/x402-trust-guard/proofs.js must stay a copy of worker/src/proofs.js");
});

test("verdict logic matches the API's copy", () => {
  const here = fs.readFileSync(new URL("../verdict.js", import.meta.url), "utf8");
  const api = fs.readFileSync(new URL("../../../worker/src/check.js", import.meta.url), "utf8");
  assert.equal(here, api, "packages/x402-trust-guard/verdict.js must stay a copy of worker/src/check.js");
});
