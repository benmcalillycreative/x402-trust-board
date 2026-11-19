// "Why isn't my endpoint in the Bazaar?" A free check for sellers.
//
//   GET /v1/listing?url=<your paid URL>[&method=GET|POST]
//
// It asks Coinbase's own public validator (the same checks the Bazaar indexer runs), looks the
// payout address up in Coinbase's merchant discovery list, adds what the board has measured
// (30-day availability against Coinbase's 99% bar for featured listings), and turns all of it
// into plain findings with a fix for each one. Free, capped per IP per day.
//
// The analysis below is pure (no Workers APIs) so it can be tested with plain Node.

import { parseTarget, findEndpoint } from "./check.js";

export const LISTING_FREE_PER_DAY = 30;
export const CURATION_BAR = 0.99; // Coinbase: at least 99% availability over 30 days for featured listings
export const DESCRIPTION_MAX = 500; // Coinbase rejects longer descriptions on verify and settle

const strip = (s) => (s || "").split("?")[0].replace(/\/+$/, "").toLowerCase();

/** The first payTo found anywhere in the decoded payment requirements (V1 or V2 shape). */
export function findPayTo(obj, depth = 0) {
  if (!obj || typeof obj !== "object" || depth > 6) return null;
  if (typeof obj.payTo === "string" && obj.payTo) return obj.payTo;
  for (const v of Array.isArray(obj) ? obj : Object.values(obj)) {
    const hit = findPayTo(v, depth + 1);
    if (hit) return hit;
  }
  return null;
}

function findString(obj, key, depth = 0) {
  if (!obj || typeof obj !== "object" || depth > 6) return null;
  if (typeof obj[key] === "string") return obj[key];
  for (const v of Array.isArray(obj) ? obj : Object.values(obj)) {
    const hit = findString(v, key, depth + 1);
    if (hit) return hit;
  }
  return null;
}

const pct = (x) => `${(Math.round(x * 1000) / 10).toFixed(1)}%`;

// Coinbase's validator runs ~25 named checks (url_https, returns_402, payment_required_header, accepts[0].payTo,
// bazaar.info.input, ...). Group them into the few things a seller actually has to fix, each with one fix.
const GROUPS = [
  { key: "url", match: /^url_/, title: "A valid public https:// URL",
    fix: "Use the full https:// URL of the deployed route, on a public host name." },
  { key: "reachable", match: /^(endpoint_)?reachable$/, title: "Reachable over public HTTPS",
    fix: "Deploy the route to a public https:// URL that answers without a login, VPN or IP allow-list." },
  { key: "returns_402", match: /^returns_402$/, title: "Answers an unpaid request with 402 Payment Required",
    fix:
      "Make the route answer 402 when no payment is attached. If you validate input before the x402 " +
      "middleware, let a request with no input (or your example input) reach the middleware first; a 400 " +
      "for missing input stops the indexer. Check the method too: a POST route checked with GET (or the reverse) fails here." },
  { key: "terms", match: /^(valid_json|x402_version|payment_required_header|has_accepts|accepts\[|has_resource|parse)/,
    title: "Payment terms are complete and readable",
    fix:
      "For x402 v2, send the terms base64-encoded in the PAYMENT-REQUIRED response header (the indexer reads the " +
      "header, not the body), with x402Version 2, a resource, and an accepts entry naming scheme, network, asset, " +
      "amount, payTo and maxTimeoutSeconds." },
  { key: "bazaar", match: /^(has_bazaar_extension|bazaar\.)/, title: "Bazaar metadata (extensions.bazaar) is complete",
    fix:
      "Add an extensions.bazaar block to the route: info.input (an example input whose method matches the route " +
      "and that still gets a 402), info.output with an example, and schema.properties.input that the example " +
      "validates against. With the x402 SDKs this is declareDiscoveryExtension({ input, inputSchema, output })." },
];
const groupOf = (check) => GROUPS.find((g) => g.match.test(check || "")) ||
  { key: check, title: `Coinbase check: ${check}`, fix: null };

/**
 * Turn the validator's answer, the merchant list and the board's record into findings.
 * Each finding: { status: "pass" | "fail" | "warn" | "info", title, detail, fix? }.
 */
export function analyze({ target, validate, merchant, seller, validateError, merchantError }) {
  const findings = [];
  const add = (status, title, detail, fix) => findings.push(fix ? { status, title, detail, fix } : { status, title, detail });

  // 1. Coinbase's validator: what the indexer sees.
  if (validateError || !validate) {
    add("info", "Coinbase's validator didn't answer", validateError || "No response.",
      "Try again in a minute. You can also run it yourself: POST https://api.cdp.coinbase.com/platform/v2/x402/validate.");
  } else {
    const pre = validate.preflight || [];
    const failed = pre.filter((c) => !c.passed);
    const skipped = failed.filter((c) => /^skipped/i.test(c.detail || ""));
    const real = failed.filter((c) => !/^skipped/i.test(c.detail || ""));
    const byGroup = new Map();
    for (const c of real) {
      const g = groupOf(c.check);
      if (!byGroup.has(g.key)) byGroup.set(g.key, { g, items: [] });
      byGroup.get(g.key).items.push(c);
    }
    for (const { g, items } of byGroup.values()) {
      const required = items.some((c) => c.severity !== "advisory");
      const detail = items.slice(0, 4).map((c) =>
        [`${c.check}: ${c.detail || "failed"}`, c.expected ? `(expected ${c.expected}` + (c.actual ? `, got ${c.actual})` : ")") : ""]
          .filter(Boolean).join(" ")).join(" · ") + (items.length > 4 ? ` · and ${items.length - 4} more` : "");
      add(required ? "fail" : "warn", g.title, detail, g.fix || undefined);
    }
    if (pre.length && !real.some((c) => c.severity !== "advisory")) {
      add("pass", "Passes Coinbase's indexing checks", `${pre.length - failed.length} of ${pre.length} preflight checks passed` +
        (real.length ? `; ${real.length} advisory note${real.length === 1 ? "" : "s"} above.` : "."));
    } else if (skipped.length) {
      add("info", "Some checks were skipped", `${skipped.length} later check${skipped.length === 1 ? " was" : "s were"} skipped because of the failures above; fix those and run this again.`);
    }
    const sim = validate.simulation || {};
    if (sim.outcome === "accepted") {
      add("pass", "Coinbase's facilitator would accept it for indexing", "The validator's simulated decision is: accepted.");
    } else if (sim.outcome === "rejected") {
      add("fail", "Coinbase's facilitator would reject it for indexing", sim.rejectionReason || "No reason given.",
        "Fix the failed checks above first; the reason here usually points at the one that matters.");
    }
    // v2 keeps it at resource.description, v1 on the requirement itself. (Not from the Bazaar block: its JSON
    // schema has "description" keys of its own.)
    const description = findString(validate.paymentRequirements, "description");
    if (description && description.length > DESCRIPTION_MAX) {
      add("fail", "Description is too long", `${description.length} characters; Coinbase rejects more than ${DESCRIPTION_MAX} on verify and settle.`,
        `Shorten the route's description to ${DESCRIPTION_MAX} characters or fewer.`);
    }
  }

  // 2. Is it actually in the index?
  const index = validate?.index;
  const payTo = findPayTo(validate?.paymentRequirements);
  const entries = merchant?.resources || [];
  const entry = entries.find((r) => strip(r.resource) === strip(target.url)) || null;
  if (index && index.active) {
    const seen = index.lastCrawledAt ? ` Last crawled ${index.lastCrawledAt}.` : "";
    add("pass", "Listed in the Bazaar", `Coinbase's index has this resource as active.${seen}`);
  } else if (validate) {
    const why = entry
      ? "Coinbase's merchant list has a resource at this URL for your payout address, but the validator reports it as not active."
      : "Coinbase's index doesn't have this resource.";
    add("fail", "Not listed in the Bazaar yet", why,
      "Listing starts from a paid call that settles through the CDP facilitator, with paymentPayload.resource and " +
      "paymentPayload.extensions.bazaar set on the settle. Indexing can take up to 15 minutes after that. " +
      "\"processing\" in the EXTENSION-RESPONSES header is not an error and doesn't guarantee a listing; " +
      "\"rejected\" comes with a reason. A resource with no settled payment for 30 days drops out again.");
  }
  if (payTo && merchant) {
    const total = merchant.pagination?.total ?? entries.length;
    add("info", "Your payout address in the Bazaar",
      `${payTo} has ${total} active resource${total === 1 ? "" : "s"} in Coinbase's merchant list` +
        (entry ? ", including this one." : (total ? ", but not this URL. A listing is tied to the exact resource URL your settle reports." : ".")));
  } else if (merchantError) {
    add("info", "Couldn't read Coinbase's merchant list", merchantError);
  }

  // 3. Agentic.Market card: what the per-route metadata can and can't fill in.
  if (entry) {
    const missing = [];
    if (!entry.description) missing.push("description");
    if (!entry.iconUrl) missing.push("icon (resource.iconUrl)");
    if (!entry.serviceName) missing.push("serviceName");
    if (missing.length) {
      add("warn", "Listing metadata is incomplete", `The Bazaar entry is missing: ${missing.join(", ")}.`,
        "Set them on the route: description (500 characters or fewer, saying when an agent should use it), serviceName, " +
          "and resource.iconUrl (PNG, JPG, WebP or GIF; not SVG). They are picked up on the next settled payment.");
    }
    add("info", "Agentic.Market card (name, category, description)",
      "The service-level card on Agentic.Market is curated by Coinbase, so an empty card isn't something the route can fix by itself.",
      "Ask for it with an issue on github.com/coinbase/cdp-sdk (that's where Coinbase routes Agentic.Market curation requests): " +
        "give the service id, name, category, a one-line description and your website.");
  }

  // 4. What the board measured: availability against Coinbase's bar for featured listings.
  const ep = seller ? findEndpoint(seller, target) : null;
  let board = null;
  if (ep) {
    const u30 = typeof ep.uptime_30d === "number" ? ep.uptime_30d : null;
    const days = ep.history_days || null;
    board = {
      grade: ep.grade, uptime_7d: ep.uptime, uptime_30d: u30, probes_30d: ep.probes_30d ?? null,
      history_days: days, page: seller.page || null,
    };
    if (u30 !== null) {
      const span = days && days < 30 ? `${days} days so far` : "30 days";
      const meets = u30 >= CURATION_BAR;
      add(meets ? "pass" : "warn", `Availability ${pct(u30)} over ${span} (board's checks)`,
        `${ep.probes_30d ?? "Several"} unpaid checks by Settle Watch. Coinbase features listings at 99% or better over 30 days ` +
          "(above 99.5% gets priority), measured by its own probe; this is a second opinion, not Coinbase's number.",
        meets ? undefined : "Find the failed checks on your seller page and fix whatever made them fail (timeouts, 5xx, wrong status).");
    }
  } else if (seller === null) {
    add("info", "Not on Settle Watch yet",
      "The board hasn't seen this URL in the Bazaar, Circle's marketplace or the MPP directory, so it has no availability history for it.");
  }

  // Being unlisted is the outcome, not one of the causes: count only the things that block a listing.
  const failed = findings.filter((f) => f.status === "fail" && f.title !== "Not listed in the Bazaar yet").length;
  const listed = Boolean(index && index.active);
  const summary = !validate
    ? "Couldn't run Coinbase's checks just now (its validator didn't answer). Try again in a minute."
    : listed
    ? (failed ? "Listed, with problems to fix." : "Listed in the Bazaar, and nothing blocking it.")
    : (failed ? `Not listed: ${failed} thing${failed === 1 ? "" : "s"} to fix first.` : "Not listed yet: it passes the checks, so it needs a settled payment through the CDP facilitator.");
  return { listed, payTo: payTo || null, summary, findings, board };
}

/** Normalise and sanity-check the input. Returns { target, method } or { error }. */
export function parseListingInput(rawUrl, rawMethod) {
  const target = parseTarget(rawUrl);
  if (!target || !target.url.startsWith("https://")) return { error: "Pass your full https:// endpoint URL as ?url=" };
  const method = (rawMethod || "GET").toUpperCase();
  if (method !== "GET" && method !== "POST") return { error: "method must be GET or POST (what Coinbase's validator supports)" };
  return { target, method };
}
