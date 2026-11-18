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

const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const GOOD = "0x" + "a1".repeat(20);
const BAD = "0x" + "dd".repeat(20);
const HOST = "seller.example";
let server, base, paid = [], checks = 0;

const SELLER = {
  host: HOST, name: "Seller", grade: "A", generated_at: "2026-11-18T00:00:00Z",
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
    const payTo = url.pathname === "/hijacked" ? BAD : GOOD;
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
  withTrustGuard(c, { board: `${base}/board/`, api: `${base}/api`, fetch: shim, ...opts });
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
  withTrustGuard(c, { mode: "free", board: `${base}/board/`, fetch: shim, ...opts });
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

test("verdict logic matches the API's copy", () => {
  const here = fs.readFileSync(new URL("../verdict.js", import.meta.url), "utf8");
  const api = fs.readFileSync(new URL("../../../worker/src/check.js", import.meta.url), "utf8");
  assert.equal(here, api, "packages/x402-trust-guard/verdict.js must stay a copy of worker/src/check.js");
});
