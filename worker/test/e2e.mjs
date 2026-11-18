// End to end, locally: wrangler dev + a fake board + a fake facilitator + a real x402 client
// signing with a throwaway key. Nothing touches a chain. Run: npm run e2e
import http from "node:http";
import { spawn } from "node:child_process";
import assert from "node:assert/strict";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { wrapFetchWithPayment, x402Client } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm/exact/client";

const PAY_TO = "0x" + "c3".repeat(20);
const A = "0x" + "a1".repeat(20);
const calls = [];
const SELLER = {
  host: "api.example.com", name: "Example", grade: "B", average_score: 78, generated_at: "2026-11-18T00:00:00Z",
  endpoints_total: 1, red_flag_endpoints: 0, payout: { verified: true, best_level: "dns_signed", proofs: [{ address: A, level: "dns_signed" }] },
  endpoints: [{ resource: "https://api.example.com/x", grade: "A", score: 91, listed_pay_to: A, flags: [] }],
};

const fake = http.createServer((req, res) => {
  let body = "";
  req.on("data", (d) => (body += d));
  req.on("end", () => {
    calls.push(req.url);
    const json = (code, obj) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(obj)); };
    if (req.url === "/seller/api.example.com.json") return json(200, SELLER);
    if (req.url.startsWith("/seller/")) return json(404, {});
    if (req.url === "/fac/supported") return json(200, { kinds: [{ x402Version: 2, scheme: "exact", network: "eip155:8453" }], extensions: ["bazaar"], signers: {} });
    const p = body ? JSON.parse(body) : {};
    const payer = p.paymentPayload?.payload?.authorization?.from;
    if (req.url === "/fac/verify") return json(200, { isValid: true, payer });
    if (req.url === "/fac/settle") return json(200, { success: true, transaction: "0x" + "ee".repeat(32), network: "eip155:8453", payer });
    json(404, {});
  });
});
await new Promise((r) => fake.listen(0, "127.0.0.1", r));
const F = `http://127.0.0.1:${fake.address().port}`;
const PORT = 8799;
const W = `http://127.0.0.1:${PORT}`;

const dev = spawn("npx", ["wrangler", "dev", "--port", String(PORT), "--ip", "127.0.0.1",
  "--var", `ALERTS_TOKEN:t0ken`, "--var", "PRICE:$0.01", "--var", `BOARD_URL:${F}/`, "--var", `FACILITATOR_URL:${F}/fac`, "--var", `PAY_TO:${PAY_TO}`],
  { env: { ...process.env, NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" }, stdio: ["ignore", "pipe", "pipe"], detached: true });
let log = "";
dev.stdout.on("data", (d) => (log += d));
dev.stderr.on("data", (d) => (log += d));
const done = (code) => {
  try { process.kill(-dev.pid, "SIGTERM"); } catch {} // the whole group: npx, wrangler and workerd
  fake.close();
  process.exit(code);
};

try {
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(`${W}/v1/health`)).ok) break; } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  assert.equal((await (await fetch(`${W}/v1/health`)).json()).payTo, PAY_TO);
  assert.equal((await fetch(`${W}/v1/check?url=nope`)).status, 400, "bad input is refused before the paywall");

  const unpaid = await fetch(`${W}/v1/check?url=${encodeURIComponent("https://api.example.com/x")}`);
  assert.equal(unpaid.status, 402);
  const pr = JSON.parse(Buffer.from(unpaid.headers.get("payment-required"), "base64").toString());
  assert.equal(pr.x402Version, 2);
  assert.equal(pr.accepts[0].amount, "10000", "PRICE var sets the price");
  assert.equal(pr.accepts[0].payTo.toLowerCase(), PAY_TO);
  assert.equal(pr.accepts[0].network, "eip155:8453");
  assert.ok(pr.extensions?.bazaar, "bazaar discovery metadata is declared");
  console.log("402 OK:", JSON.stringify(pr.accepts[0]));

  const account = privateKeyToAccount(generatePrivateKey());
  const client = new x402Client().register("eip155:*", new ExactEvmScheme(account));
  const pay = wrapFetchWithPayment(fetch, client);
  const ok = await pay(`${W}/v1/check?url=${encodeURIComponent("https://api.example.com/x")}&payTo=${A}`);
  const out = await ok.json();
  assert.equal(ok.status, 200, JSON.stringify(out));
  assert.equal(out.verdict, "ok");
  assert.equal(out.pay_to.proven_by_seller, "dns_signed");
  assert.ok(ok.headers.get("payment-response"), "settlement receipt header");
  assert.ok(calls.includes("/fac/verify") && calls.includes("/fac/settle"));
  console.log("paid OK:", out.verdict, out.reasons);

  const bad = await pay(`${W}/v1/check?url=${encodeURIComponent("https://api.example.com/x")}&payTo=0x${"dd".repeat(20)}`);
  assert.equal((await bad.json()).verdict, "avoid");
  const unk = await pay(`${W}/v1/check?url=${encodeURIComponent("https://never.example/x")}`);
  assert.equal((await unk.json()).verdict, "unknown");

  // Alert sign-ups: subscribe -> queue -> mailed -> confirm -> done, and a signed stop link.
  const sub = await fetch(`${W}/v1/alerts/subscribe`, { method: "POST", body: new URLSearchParams({ email: "Pat@Example.com", host: "api.example.com" }) });
  assert.equal(sub.status, 200, await sub.text());
  assert.equal((await fetch(`${W}/v1/alerts/subscribe`, { method: "POST", body: new URLSearchParams({ email: "x@y.co", host: "never.example" }) })).status, 404);
  assert.equal((await fetch(`${W}/v1/alerts/queue`)).status, 401);
  const H = { authorization: "Bearer t0ken", "content-type": "application/json" };
  let q = (await (await fetch(`${W}/v1/alerts/queue`, { headers: H })).json()).items;
  assert.equal(q.length, 1);
  assert.equal(q[0].email, "pat@example.com");
  assert.equal(q[0].state, "new");
  await fetch(`${W}/v1/alerts/ack`, { method: "POST", headers: H, body: JSON.stringify({ key: q[0].key, state: "mailed" }) });
  assert.equal((await fetch(`${W}/v1/alerts/confirm?t=${q[0].key.slice(2)}`)).status, 200);
  q = (await (await fetch(`${W}/v1/alerts/queue`, { headers: H })).json()).items;
  assert.equal(q[0].state, "confirmed");
  await fetch(`${W}/v1/alerts/ack`, { method: "POST", headers: H, body: JSON.stringify({ key: q[0].key, state: "done" }) });
  const { createHmac } = await import("node:crypto");
  const sig = createHmac("sha256", "t0ken").update("pat@example.com|api.example.com").digest("hex").slice(0, 32);
  assert.equal((await fetch(`${W}/v1/alerts/stop?e=pat%40example.com&m=api.example.com&s=bad`)).status, 400);
  assert.equal((await fetch(`${W}/v1/alerts/stop?e=pat%40example.com&m=api.example.com&s=${sig}`)).status, 200);
  q = (await (await fetch(`${W}/v1/alerts/queue`, { headers: H })).json()).items;
  assert.deepEqual(q.map((i) => [i.kind, i.match]), [["stop", "api.example.com"]]);
  console.log("sign-ups OK");

  // MCP: initialize, list, call (free, no payment).
  const call = (body) => fetch(`${W}/mcp`, { method: "POST", headers: { "content-type": "application/json", accept: "application/json, text/event-stream" }, body: JSON.stringify(body) }).then((r) => r.json());
  const init = await call({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "1" } } });
  assert.equal(init.result.protocolVersion, "2025-06-18");
  assert.equal((await fetch(`${W}/mcp`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) })).status, 202);
  const tools = await call({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  assert.equal(tools.result.tools[0].name, "check_payment");
  const res = await call({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "check_payment", arguments: { url: "https://api.example.com/x", payTo: A } } });
  assert.equal(res.result.structuredContent.verdict, "ok");
  assert.match(res.result.content[0].text, /^OK:/);
  console.log("mcp OK");
  console.log("e2e passed");
  done(0);
} catch (e) {
  console.error(e);
  console.error(log.slice(-3000));
  done(1);
}
