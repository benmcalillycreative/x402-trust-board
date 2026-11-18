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
  "--var", `BOARD_URL:${F}/`, "--var", `FACILITATOR_URL:${F}/fac`, "--var", `PAY_TO:${PAY_TO}`],
  { env: { ...process.env, NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" }, stdio: ["ignore", "pipe", "pipe"] });
let log = "";
dev.stdout.on("data", (d) => (log += d));
dev.stderr.on("data", (d) => (log += d));
const done = (code) => { dev.kill("SIGTERM"); fake.close(); process.exit(code); };

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
  assert.equal(pr.accepts[0].amount, "1000");
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
  console.log("e2e passed");
  done(0);
} catch (e) {
  console.error(e);
  console.error(log.slice(-3000));
  done(1);
}
