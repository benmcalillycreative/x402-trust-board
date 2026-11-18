import { test } from "node:test";
import assert from "node:assert/strict";
import { analyze, findPayTo, parseListingInput } from "../src/listing.js";

const PAY = "0x" + "a1".repeat(20);
const T = (u) => parseListingInput(u).target;
const ok = (check) => ({ check, passed: true, detail: "ok", severity: "required" });
const bad = (check, detail) => ({ check, passed: false, detail, severity: "required" });
const reqs = { x402Version: 2, accepts: [{ scheme: "exact", network: "eip155:8453", amount: "1000", payTo: PAY }] };

test("input: https only, GET or POST", () => {
  assert.ok(parseListingInput("http://api.example.com/x").error);
  assert.ok(parseListingInput("https://api.example.com/x", "PUT").error);
  assert.equal(parseListingInput("https://api.example.com/x", "post").method, "POST");
});

test("finds payTo in v1 and v2 shapes", () => {
  assert.equal(findPayTo(reqs), PAY);
  assert.equal(findPayTo({ payTo: PAY }), PAY);
  assert.equal(findPayTo(null), null);
});

test("listed, healthy, above the 99% bar", () => {
  const out = analyze({
    target: T("https://api.example.com/x"),
    validate: { valid: true, preflight: [ok("reachable"), ok("returns_402"), ok("parse"), ok("has_bazaar_extension")],
      paymentRequirements: reqs, simulation: { outcome: "accepted" }, index: { active: true, lastCrawledAt: "2026-11-18T00:00:00Z" } },
    merchant: { resources: [{ resource: "https://api.example.com/x", description: "d", iconUrl: "i.png", serviceName: "S" }], pagination: { total: 1 } },
    seller: { page: "p", endpoints: [{ resource: "https://api.example.com/x", grade: "A", uptime: 1, uptime_30d: 0.995, probes_30d: 60, history_days: 30 }] },
  });
  assert.equal(out.listed, true);
  assert.equal(out.findings.filter((f) => f.status === "fail").length, 0);
  assert.ok(out.findings.some((f) => f.status === "pass" && /99.5%/.test(f.title)));
  assert.ok(out.findings.some((f) => /Agentic.Market/.test(f.title)), "explains how to fill the curated card");
});

test("400 before the middleware: not listed, with the fix", () => {
  const out = analyze({
    target: T("https://api.example.com/search?q=1"),
    validate: { valid: false, preflight: [ok("reachable"), bad("returns_402", "Endpoint returned 400")],
      paymentRequirements: null, simulation: { outcome: "rejected", rejectionReason: "no 402" }, index: null },
    merchant: null, seller: null,
  });
  assert.equal(out.listed, false);
  const f = out.findings.find((x) => x.title.startsWith("Answers an unpaid request"));
  assert.equal(f.status, "fail");
  assert.match(f.fix, /reach the middleware/);
  assert.ok(out.findings.some((x) => x.title === "Not listed in the Bazaar yet" && /settles through the CDP facilitator/.test(x.fix)));
  assert.ok(out.findings.some((x) => x.title === "Not on the x402 Trust Board yet"));
  assert.match(out.summary, /to fix first/);
});

test("passes everything but never settled: says so", () => {
  const out = analyze({
    target: T("https://api.example.com/x"),
    validate: { valid: true, preflight: [ok("returns_402")], paymentRequirements: reqs, simulation: { outcome: "accepted" }, index: null },
    merchant: { resources: [], pagination: { total: 0 } }, seller: undefined,
  });
  assert.match(out.summary, /needs a settled payment/);
  assert.ok(!out.findings.some((x) => x.title === "Not on the x402 Trust Board yet"), "board unreadable is not 'not on the board'");
});

test("long description, missing icon, below the bar", () => {
  const long = "x".repeat(600);
  const out = analyze({
    target: T("https://api.example.com/x"),
    validate: { valid: true, preflight: [], paymentRequirements: { ...reqs, description: long }, simulation: { outcome: "accepted" }, index: { active: true } },
    merchant: { resources: [{ resource: "https://api.example.com/x/", description: "d" }], pagination: { total: 1 } },
    seller: { endpoints: [{ resource: "https://api.example.com/x", uptime_30d: 0.97, probes_30d: 40, history_days: 12 }] },
  });
  assert.ok(out.findings.some((x) => x.title === "Description is too long" && x.status === "fail"));
  assert.ok(out.findings.some((x) => x.title === "Listing metadata is incomplete" && /iconUrl/.test(x.detail)));
  const av = out.findings.find((x) => x.title.startsWith("Availability"));
  assert.equal(av.status, "warn");
  assert.match(av.title, /12 days so far/);
});

test("validator down: no crash, no false verdict", () => {
  const out = analyze({ target: T("https://api.example.com/x"), validate: null, validateError: "timeout", merchant: null, seller: undefined });
  assert.equal(out.listed, false);
  assert.ok(out.findings.some((x) => x.title === "Coinbase's validator didn't answer"));
  assert.match(out.summary, /Try again in a minute/);
});

test("real validator output: a 404 groups into one fix, the rest are 'skipped'", () => {
  const skip = (check) => ({ check, passed: false, detail: "Skipped: endpoint did not return 402", severity: "required" });
  const out = analyze({
    target: T("https://example.com/nothing"),
    validate: { valid: false, statusCode: 404, paymentRequirements: null, simulation: { outcome: "rejected", rejectionReason: "not 402" }, index: null,
      preflight: [ok("url_valid"), ok("url_https"), ok("endpoint_reachable"),
        { check: "returns_402", passed: false, detail: "Endpoint returned HTTP 404 instead of 402", expected: "402", actual: "404", severity: "required" },
        skip("valid_json"), skip("payment_required_header"), skip("accepts[0].payTo"), skip("bazaar.info.input")] },
    merchant: null, seller: null,
  });
  const fails = out.findings.filter((f) => f.status === "fail" && f.title !== "Not listed in the Bazaar yet");
  assert.deepEqual(fails.map((f) => f.title), ["Answers an unpaid request with 402 Payment Required", "Coinbase's facilitator would reject it for indexing"]);
  assert.match(fails[0].detail, /got 404/);
  assert.ok(out.findings.some((f) => f.title === "Some checks were skipped" && /4 later checks/.test(f.detail)));
});

test("real validator output: 25 passes collapse into one line, advisory failures are warnings", () => {
  const names = ["url_valid", "url_https", "endpoint_reachable", "returns_402", "valid_json", "x402_version", "payment_required_header",
    "has_accepts", "accepts[0].scheme", "accepts[0].payTo", "has_resource", "has_bazaar_extension", "bazaar.info", "bazaar.info.input", "bazaar.schema", "parse"];
  const out = analyze({
    target: T("https://api.example.com/x"),
    validate: { valid: true, paymentRequirements: reqs, simulation: { outcome: "accepted" }, index: { active: true },
      preflight: [...names.map(ok), { check: "bazaar.info.output.example", passed: false, detail: "missing", severity: "advisory" }] },
    merchant: null, seller: undefined,
  });
  assert.equal(out.findings.filter((f) => f.status === "pass" && /Coinbase's indexing checks/.test(f.title)).length, 1);
  assert.ok(out.findings.some((f) => f.status === "warn" && f.title === "Bazaar metadata (extensions.bazaar) is complete"));
  assert.match(out.summary, /nothing blocking/);
});
