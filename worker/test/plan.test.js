import { test } from "node:test";
import assert from "node:assert/strict";
import { extend, monthsOf, planPrice, planId, normalizeHost, decodeSettlement } from "../src/plan.js";

test("price: $11.99 a month, straight multiples, overridable", () => {
  assert.equal(planPrice({}, 1), "$11.99");
  assert.equal(planPrice({}, 3), "$35.97");
  assert.equal(planPrice({}, 12), "$143.88");
  assert.equal(planPrice({ PLAN_PRICE: "$9.50" }, 2), "$19.00");
  assert.equal(planPrice({ PLAN_PRICE: "nonsense" }, 1), "$11.99");
});

test("only 1, 3 or 12 months", () => {
  assert.equal(monthsOf("3"), 3);
  assert.equal(monthsOf("2"), null);
  assert.equal(monthsOf(undefined), null);
});

test("time is added from today, or from the end date when there is time left", () => {
  const now = new Date("2026-11-18T12:00:00Z");
  assert.equal(extend(null, 1, now), "2026-12-18T12:00:00.000Z");
  assert.equal(extend("2026-11-01T00:00:00Z", 1, now), "2026-12-18T12:00:00.000Z", "lapsed: counts from now");
  assert.equal(extend("2026-11-25T00:00:00Z", 1, now), "2026-12-25T00:00:00.000Z", "early renewal keeps the days left");
  assert.equal(extend(null, 1, new Date("2027-01-31T00:00:00Z")), "2027-02-28T00:00:00.000Z", "clamped to the month's end");
  assert.equal(extend(null, 12, now), "2027-11-18T12:00:00.000Z");
});

test("plan id: stable, case-insensitive email, no email in it", async () => {
  const a = await planId("api.example.com", "Pat@Example.com");
  assert.equal(a, await planId("api.example.com", "pat@example.com"));
  assert.match(a, /^[a-f0-9]{24}$/);
  assert.notEqual(a, await planId("api.other.com", "pat@example.com"));
});

test("domain from whatever was typed", () => {
  assert.equal(normalizeHost("https://API.Example.com/v1/x?y=1"), "api.example.com");
  assert.equal(normalizeHost("api.example.com:443"), "api.example.com");
  assert.equal(normalizeHost("localhost"), null);
  assert.equal(normalizeHost("<script>.com"), null);
});

test("settlement header: only a successful one counts", () => {
  const enc = (o) => btoa(JSON.stringify(o));
  assert.equal(decodeSettlement(enc({ success: true, transaction: "0xab" })).transaction, "0xab");
  assert.equal(decodeSettlement(enc({ success: false })), null);
  assert.equal(decodeSettlement("garbage"), null);
  assert.equal(decodeSettlement(null), null);
});
