// Aggregate usage counters in KV (st:<YYYY-MM-DD>), so the board can publish how often Settle Watch is
// actually paid for. Payer addresses are public on-chain anyway; they're shortened in the output.

const DAY = 86400;
const short = (a) => (a && a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a || "?");
const usd = (price) => parseFloat(String(price || "0").replace("$", "")) || 0;

export async function countPaid(env, kind, settled, price) {
  if (!env.SIGNUPS) return;
  const key = `st:${new Date().toISOString().slice(0, 10)}`;
  const rec = JSON.parse((await env.SIGNUPS.get(key)) || "{}");
  rec[kind] = (rec[kind] || 0) + 1;
  rec[`${kind}_usd`] = Math.round(((rec[`${kind}_usd`] || 0) + usd(price)) * 1e6) / 1e6;
  const payer = String(settled?.payer || "").toLowerCase();
  if (payer) {
    rec.payers = rec.payers || {};
    rec.payers[payer] = (rec.payers[payer] || 0) + 1;
  }
  await env.SIGNUPS.put(key, JSON.stringify(rec), { expirationTtl: 400 * DAY });
}

export async function stats(env) {
  const days = [];
  if (env.SIGNUPS) {
    let cursor;
    do {
      const page = await env.SIGNUPS.list({ prefix: "st:", cursor });
      for (const k of page.keys) {
        const rec = JSON.parse((await env.SIGNUPS.get(k.name)) || "{}");
        const payers = Object.fromEntries(Object.entries(rec.payers || {}).map(([p, n]) => [short(p), n]));
        days.push({ day: k.name.slice(3), checks: rec.checks || 0, checks_usd: rec.checks_usd || 0,
          plans: rec.plans || 0, plans_usd: rec.plans_usd || 0, payers });
      }
      cursor = page.list_complete ? null : page.cursor;
    } while (cursor);
  }
  days.sort((a, b) => (a.day < b.day ? 1 : -1));
  const since = (n) => new Date(Date.now() - n * DAY * 1000).toISOString().slice(0, 10);
  const total = (n) => {
    const d = days.filter((x) => x.day > since(n));
    const payers = new Set(d.flatMap((x) => Object.keys(x.payers)));
    return { checks: d.reduce((s, x) => s + x.checks, 0), plans: d.reduce((s, x) => s + x.plans, 0),
      usd: Math.round(d.reduce((s, x) => s + x.checks_usd + x.plans_usd, 0) * 100) / 100, payers: payers.size };
  };
  return { generated_at: new Date().toISOString(), last_7_days: total(7), last_30_days: total(30), days: days.slice(0, 90),
    note: "Settled payments only (paid checks at /v1/check and seller-plan payments). Payers are shortened wallet addresses." };
}
