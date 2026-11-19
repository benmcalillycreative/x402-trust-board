import { test } from "node:test";
import assert from "node:assert/strict";
import { parseTarget, verdict } from "../src/check.js";

const A = "0x" + "a1".repeat(20);
const B = "0x" + "b2".repeat(20);
const seller = (eps, proofs = []) => ({
  host: "api.example.com", name: "Example", grade: "B", generated_at: new Date().toISOString(),
  payout: { verified: proofs.some((p) => p.level === "dns_signed"), proofs },
  endpoints: eps,
});
const ep = (resource, grade, flags = [], pay = A) => ({ resource, grade, score: 80, listed_pay_to: pay, flags });
const T = (u) => parseTarget(u);

test("rejects junk", () => {
  assert.equal(parseTarget("not a url"), null);
  assert.equal(parseTarget("ftp://x.com/a"), null);
  assert.equal(parseTarget("http://localhost/a"), null);
  assert.equal(parseTarget("https://api.example.com/a").host, "api.example.com");
});

test("unknown seller", () => {
  assert.equal(verdict(T("https://nope.example/x"), null, null, "b").verdict, "unknown");
});

test("good endpoint is ok, matched without query string", () => {
  const v = verdict(T("https://api.example.com/x?q=1"), seller([ep("https://api.example.com/x", "A")]), A, "b");
  assert.equal(v.verdict, "ok");
  assert.equal(v.endpoint.grade, "A");
  assert.equal(v.pay_to.matches_listing, true);
});

test("red flag means avoid", () => {
  const f = [{ code: "wallet_switched", severity: "red", text: "Wallet switched" }];
  assert.equal(verdict(T("https://api.example.com/x"), seller([ep("https://api.example.com/x", "B", f)]), null, "b").verdict, "avoid");
});

test("address contradicting the seller's proof means avoid", () => {
  const s = seller([ep("https://api.example.com/x", "A")], [{ address: A, level: "dns_signed" }]);
  assert.equal(verdict(T("https://api.example.com/x"), s, B, "b").verdict, "avoid");
  const ok = verdict(T("https://api.example.com/x"), s, A.toUpperCase().replace("0X", "0x"), "b");
  assert.equal(ok.verdict, "ok");
  assert.equal(ok.pay_to.proven_by_seller, "dns_signed");
});

test("manifest-only proof doesn't contradict", () => {
  const s = seller([ep("https://api.example.com/x", "A")], [{ address: A, level: "manifest" }]);
  assert.equal(verdict(T("https://api.example.com/x"), s, B, "b").verdict, "ok");
});

test("warnings, low grades and ungraded mean caution", () => {
  const w = [{ code: "slow", severity: "warn", text: "Slow" }];
  assert.equal(verdict(T("https://api.example.com/x"), seller([ep("https://api.example.com/x", "A", w)]), null, "b").verdict, "caution");
  assert.equal(verdict(T("https://api.example.com/x"), seller([ep("https://api.example.com/x", "F")]), null, "b").verdict, "caution");
  assert.equal(verdict(T("https://api.example.com/x"), seller([ep("https://api.example.com/x", null)]), null, "b").verdict, "caution");
  // URL not on file: falls back to the seller's grade but says so
  const v = verdict(T("https://api.example.com/other"), seller([ep("https://api.example.com/x", "A")]), null, "b");
  assert.equal(v.endpoint, null);
});

test("stale data never says ok", () => {
  const s = seller([ep("https://api.example.com/x", "A")]);
  s.generated_at = new Date(Date.now() - 30 * 36e5).toISOString();
  const v = verdict(T("https://api.example.com/x"), s, null, "b");
  assert.equal(v.verdict, "caution");
  assert.equal(v.data_age_hours, 30);
  assert.match(v.reasons.join(" "), /30 hours old/);
});

test("DNS decides: a file-only address doesn't clear the check when a DNS proof exists", () => {
  const t = parseTarget("https://api.example.com/x");
  const A = "0x" + "a1".repeat(20), X = "0x" + "e6".repeat(20);
  const seller = { generated_at: new Date().toISOString(), grade: "A",
    payout: { proofs: [{ address: A, level: "dns_signed" }, { address: X, level: "site_signed" }] },
    endpoints: [{ resource: t.url, grade: "A", flags: [] }] };
  const v = verdict(t, seller, X, "https://settlewatch.com/");
  assert.equal(v.verdict, "avoid");
  assert.equal(v.pay_to.contradicts_seller_proof, true);
  const fileOnly = { ...seller, payout: { proofs: [{ address: X, level: "site_signed" }] } };
  assert.equal(verdict(t, fileOnly, X, "https://settlewatch.com/").pay_to.proven_by_seller, "site_signed");
});
