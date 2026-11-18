// The verdict logic, kept free of Workers APIs so it can be tested with plain Node.

export const RED = "red";
export const STALE_HOURS = 24; // the board refreshes every 6 hours; a day without one means something is wrong

/** Parse and normalise the URL an agent is about to pay. Returns null if it isn't one. */
export function parseTarget(raw) {
  if (!raw || raw.length > 2048) return null;
  let u;
  try {
    u = new URL(raw);
  } catch {
    return null;
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") return null;
  const host = u.hostname.toLowerCase();
  if (!/^[a-z0-9.-]+$/.test(host) || !host.includes(".")) return null;
  return { url: u.toString(), host, noQuery: u.origin + u.pathname };
}

const strip = (s) => (s || "").replace(/\/+$/, "");

/** Find the endpoint record that matches the URL: exact, then without the query, then trailing slash. */
export function findEndpoint(seller, target) {
  const eps = seller?.endpoints || [];
  return (
    eps.find((e) => e.resource === target.url) ||
    eps.find((e) => e.resource === target.noQuery) ||
    eps.find((e) => strip(e.resource.split("?")[0]) === strip(target.noQuery)) ||
    null
  );
}

const norm = (a) => (a || "").trim().toLowerCase();

/** Is the address the agent is about to pay one the seller has proven is theirs? */
export function payToCheck(seller, endpoint, payTo) {
  if (!payTo) return null;
  const proofs = seller?.payout?.proofs || [];
  const proof = proofs.find((p) => norm(p.address) === norm(payTo));
  const strong = proofs.some((p) => p.level !== "manifest");
  return {
    address: payTo,
    proven_by_seller: proof ? proof.level : null,
    matches_listing: endpoint?.listed_pay_to ? norm(endpoint.listed_pay_to) === norm(payTo) : null,
    // The seller published a payout proof and this address isn't in it.
    contradicts_seller_proof: !proof && strong,
  };
}

/**
 * Build the answer. verdict is one of:
 *   ok       graded B or better, no warnings, nothing contradicts the payout address, data under a day old
 *   caution  warnings (including big price gaps), a low grade, little history, or stale data
 *   avoid    money would go somewhere it shouldn't: a switched or burn wallet, a non-public address, or an
 *            address the seller's own payout proof contradicts
 *   unknown  the board has never seen this seller
 */
export function verdict(target, seller, payTo, board, now = Date.now()) {
  const base = { url: target.url, host: target.host, board };
  if (!seller) {
    return {
      ...base,
      verdict: "unknown",
      reasons: ["This seller isn't in the x402 Bazaar, Circle's marketplace or the MPP directory, so the board has no history for it."],
      endpoint: null,
      seller: null,
      pay_to: payTo ? { address: payTo, proven_by_seller: null, matches_listing: null, contradicts_seller_proof: false } : null,
    };
  }
  const ep = findEndpoint(seller, target);
  const pay = payToCheck(seller, ep, payTo);
  const flags = ep?.flags || [];
  const reasons = [];
  let v = "ok";
  const red = flags.filter((f) => f.severity === RED);
  const warn = flags.filter((f) => f.severity === "warn");
  if (red.length) {
    v = "avoid";
    reasons.push(...red.map((f) => f.text));
  }
  if (pay?.contradicts_seller_proof) {
    v = "avoid";
    reasons.push("The address you're about to pay isn't in the payout proof this seller published.");
  }
  if (v !== "avoid") {
    if (warn.length) {
      v = "caution";
      reasons.push(...warn.map((f) => f.text));
    }
    const grade = ep ? ep.grade : seller.grade;
    if (!grade) {
      v = "caution";
      reasons.push(ep ? "Not enough checks yet to grade this endpoint." : "This exact URL isn't graded; the seller's other endpoints are summarised instead.");
    } else if (grade === "D" || grade === "F") {
      v = "caution";
      reasons.push(`Graded ${grade}: it often fails to answer or is slow.`);
    } else if (grade === "C" && v === "ok") {
      v = "caution";
      reasons.push("Graded C: answers, but less reliably than most.");
    }
  }
  const asOf = Date.parse(seller.generated_at || "");
  const ageHours = Number.isFinite(asOf) ? Math.max(0, Math.round((now - asOf) / 36e5)) : null;
  if (ageHours === null || ageHours > STALE_HOURS) {
    // Old data can't vouch for anything: never answer "ok" on it.
    if (v === "ok") v = "caution";
    reasons.push(ageHours === null
      ? "The board's data on this seller has no date, so it may be out of date."
      : `The board's data on this seller is ${ageHours} hours old (it normally refreshes every 6 hours).`);
  }
  if (v === "ok") reasons.push("Answers reliably and its payment terms match its listing.");
  if (seller.payout?.verified) reasons.push("The seller has published a signed proof of its payout address.");
  return {
    ...base,
    verdict: v,
    reasons,
    endpoint: ep && {
      resource: ep.resource,
      grade: ep.grade,
      score: ep.score,
      uptime: ep.uptime,
      p50_latency_ms: ep.p50_latency_ms,
      listed_usd: ep.listed_usd,
      network: ep.network,
      listed_pay_to: ep.listed_pay_to,
      protocol: ep.protocol,
      listed_on: ep.listed_on,
      flags,
    },
    seller: {
      name: seller.name,
      grade: seller.grade,
      average_score: seller.average_score,
      endpoints_total: seller.endpoints_total,
      red_flag_endpoints: seller.red_flag_endpoints,
      payout: seller.payout,
      onchain: seller.onchain,
      page: seller.page,
    },
    pay_to: pay,
    data_as_of: seller.generated_at,
    data_age_hours: ageHours,
  };
}
