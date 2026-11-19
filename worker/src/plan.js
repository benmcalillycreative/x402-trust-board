// The seller plan, paid in USDC on Base through x402. No invoices, no card, no subscription to cancel.
//
// USDC can't be pulled from someone's wallet each month, so the plan is prepaid: one x402 payment buys
// 1, 3 or 12 months for one domain. Each settled payment is recorded here at once (paid_until moves
// forward from today, or from the current end date if there is time left) and queued for the board's
// computer, which sends the receipt, starts the extra monitoring, and emails a pay link a few days
// before the time runs out. Stop paying and the plan simply ends; free alerts carry on.
//
//   POST /v1/plan/start           form: email, host, months   (public) -> redirects to the pay link
//   GET  /v1/plan/pay?p=&months=  x402: $11.99 a month         (public; browsers get a wallet page)
//   GET  /v1/plan/status?p=       {host, active, paid_until}   (public; no email in it)
//
// KV (the SIGNUPS namespace):
//   plan:<id>  {id, host, email, created, paid_until, payments[]}   id = first 24 hex of sha256(host|email)
//   b:<hex>    one settled payment, waiting for the board's computer (kind "payment" in /v1/alerts/queue)

const DAY = 86400;
export const PLAN_MONTHS = [1, 3, 12];
export const PLAN_MONTHLY_USD = 11.99;
const EMAIL = /^[^\s@<>()",;:]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,}$/i;
const HOST = /^[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$/;

export const monthly = (env) => {
  const n = parseFloat(String(env.PLAN_PRICE || "").replace("$", ""));
  return n > 0 && n < 1000 ? n : PLAN_MONTHLY_USD;
};
export const monthsOf = (raw) => (PLAN_MONTHS.includes(Number(raw)) ? Number(raw) : null);
export const planPrice = (env, months) => `$${(Math.round(monthly(env) * months * 100) / 100).toFixed(2)}`;

export async function planId(host, email) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${host}|${email.toLowerCase()}`));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 24);
}

export function normalizeHost(raw) {
  const host = String(raw || "").trim().toLowerCase().replace(/^https?:\/\//, "").split(/[/?#:]/)[0];
  return HOST.test(host) && host.length <= 253 ? host : null;
}

// Calendar months, clamped (Jan 31 + 1 month = Feb 28), counted from whichever is later: now or the
// current end date, so paying early never loses days.
export function extend(paidUntil, months, now = new Date()) {
  const cur = paidUntil ? new Date(paidUntil) : null;
  const from = cur && cur > now ? cur : now;
  const d = new Date(from.getTime());
  const day = d.getUTCDate();
  d.setUTCDate(1);
  d.setUTCMonth(d.getUTCMonth() + months);
  const last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
  d.setUTCDate(Math.min(day, last));
  return d.toISOString();
}

export function decodeSettlement(header) {
  if (!header) return null;
  try {
    const s = JSON.parse(atob(header));
    return s && s.success !== false ? s : null;
  } catch {
    return null;
  }
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const day = (iso) => new Date(iso).toISOString().slice(0, 10);

function html(title, inner, board) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
    `<title>${esc(title)}</title><style>body{font:16px/1.55 system-ui,sans-serif;max-width:580px;margin:10vh auto;padding:0 16px;` +
    `color:#1d1d1b;background:#fafaf7}code{font-size:13px;word-break:break-all}.muted{color:#6b6b66}` +
    `@media(prefers-color-scheme:dark){body{color:#eee;background:#161614}a{color:#9cf}.muted{color:#a3a39c}}</style></head>` +
    `<body><h1>${esc(title)}</h1>${inner}<p><a href="${esc(board)}pro.html">Back to Settle Watch</a></p></body></html>`;
}

// Route config for the x402 middleware. Not declared for the Bazaar: this is a plan, not an API to list.
export function planRoute(env, base) {
  return {
    accepts: {
      scheme: "exact",
      network: base,
      payTo: env.PAY_TO,
      maxTimeoutSeconds: 300,
      price: (ctx) => planPrice(env, monthsOf(ctx.adapter.getQueryParam?.("months")) || 1),
    },
    description: `Settle Watch seller plan for one domain: $${monthly(env).toFixed(2)} a month in USDC, prepaid. Nothing renews by itself.`,
    mimeType: "text/html",
    serviceName: "Settle Watch",
    iconUrl: "https://api.settlewatch.com/icon.png",
    unpaidResponseBody: (ctx) => {
      const m = monthsOf(ctx.adapter.getQueryParam?.("months")) || 1;
      return {
        contentType: "application/json",
        body: { error: `Payment required: ${planPrice(env, m)} USDC on Base via x402 for ${m} month${m > 1 ? "s" : ""} of the seller plan.` },
      };
    },
  };
}

async function readPlan(env, id) {
  const raw = id && /^[a-f0-9]{24}$/.test(id) ? await env.SIGNUPS.get(`plan:${id}`) : null;
  return raw ? JSON.parse(raw) : null;
}

async function bump(kv, key, ttl, max) {
  const n = parseInt((await kv.get(key)) || "0", 10) + 1;
  await kv.put(key, String(n), { expirationTtl: ttl });
  return n <= max;
}

export function registerPlan(app, paywall) {
  app.use("/v1/plan/*", async (c, next) => {
    if (!c.env.SIGNUPS) return c.json({ error: "The seller plan isn't set up on this deployment." }, 503);
    return next();
  });

  app.post("/v1/plan/start", async (c) => {
    const board = c.env.BOARD_URL;
    const form = await c.req.parseBody().catch(() => ({}));
    const reply = (code, title, msg) => c.html(html(title, `<p>${msg}</p>`, board), code);
    if (form.website) return reply(200, "Thanks", "Check your inbox.");
    const email = String(form.email || "").trim().toLowerCase();
    const host = normalizeHost(form.host);
    const months = monthsOf(form.months) || 1;
    if (!EMAIL.test(email) || email.length > 254) return reply(400, "That email doesn't look right", "Go back and check it.");
    if (!host) return reply(400, "That domain doesn't look right", "Use your service's domain, like api.example.com.");
    if (!c.env.PAY_TO) return reply(503, "Not open yet", "Payments aren't set up on this deployment.");
    const ip = c.req.header("cf-connecting-ip") || "unknown";
    if (!(await bump(c.env.SIGNUPS, `rl:plan:${ip}:${Math.floor(Date.now() / 3600e3)}`, 3600, 10))) {
      return reply(429, "Too many requests", "Try again later.");
    }
    const seller = await fetch(`${board}seller/${host}.json`, { cf: { cacheTtl: 600, cacheEverything: true } }).catch(() => null);
    if (!seller || seller.status === 404) {
      return reply(404, "Settle Watch doesn't track that domain yet",
        `It picks services up from the Bazaar and other x402 directories, so <b>${esc(host)}</b> isn't on the board yet ` +
        `and there'd be nothing to watch. Run the free <a href="${esc(board)}listing.html">listing check</a> to see why, ` +
        `and come back once it's listed. Nothing was charged.`);
    }
    if (!seller.ok) return reply(503, "Couldn't reach the board", "Try again in a minute. Nothing was charged.");
    const id = await planId(host, email);
    const existing = await readPlan(c.env, id);
    if (!existing) {
      const rec = { id, host, email, created: new Date().toISOString(), paid_until: null, payments: [] };
      await c.env.SIGNUPS.put(`plan:${id}`, JSON.stringify(rec), { expirationTtl: 7 * DAY }); // kept once paid
    }
    return c.redirect(`/v1/plan/pay?p=${id}&months=${months}`, 303);
  });

  app.get("/v1/plan/status", async (c) => {
    const rec = await readPlan(c.env, c.req.query("p"));
    if (!rec) return c.json({ error: "No such plan" }, 404);
    const active = !!rec.paid_until && new Date(rec.paid_until) > new Date();
    return c.json({ host: rec.host, active, paid_until: rec.paid_until });
  });

  // 1) Check the link before the paywall, so nobody pays for a plan that doesn't exist.
  app.use("/v1/plan/pay", async (c, next) => {
    const board = c.env.BOARD_URL;
    if (!c.env.PAY_TO) return c.json({ error: "Not configured yet: PAY_TO is missing." }, 503);
    const rec = await readPlan(c.env, c.req.query("p"));
    if (!rec) {
      return c.html(html("Start from the seller page",
        `<p>This pay link has expired or isn't complete. Pick your domain on the <a href="${esc(board)}pro.html">seller page</a> ` +
        `and you'll get a fresh one. Nothing was charged.</p>`, board), 404);
    }
    const months = monthsOf(c.req.query("months"));
    if (!months) return c.redirect(`/v1/plan/pay?p=${rec.id}&months=1`, 302);
    c.set("plan", rec);
    c.set("months", months);
    return next();
  });

  // 2) After the paywall has settled (it runs after the handler), record the payment. A response
  //    without a settlement receipt means nothing was charged, so nothing is recorded.
  app.use("/v1/plan/pay", async (c, next) => {
    await next();
    const next_ = c.get("planNext");
    const settled = c.res && c.res.status === 200 ? decodeSettlement(c.res.headers.get("PAYMENT-RESPONSE")) : null;
    if (!next_ || !settled) return;
    const { rec, months, previous } = next_;
    const pay = { tx: settled.transaction || null, payer: settled.payer || null, network: settled.network || null,
      amount: planPrice(c.env, months), months, ts: new Date().toISOString() };
    rec.payments = [...(rec.payments || []), pay].slice(-36);
    await c.env.SIGNUPS.put(`plan:${rec.id}`, JSON.stringify(rec)); // no expiry once paid
    const key = `b:${crypto.randomUUID().replace(/-/g, "")}`;
    await c.env.SIGNUPS.put(key, JSON.stringify({ id: rec.id, host: rec.host, email: rec.email, ...pay,
      paid_until: rec.paid_until, previous_until: previous, state: "new" }), { expirationTtl: 90 * DAY });
  });

  // 3) The x402 paywall: agents get a 402 with the terms, people in a browser get a wallet page.
  app.use("/v1/plan/pay", (c, next) => paywall(c.env)(c, next));

  // 4) Only reached with a verified payment. The receipt shows the new end date; it's committed in (2)
  //    once the payment has actually settled.
  app.get("/v1/plan/pay", async (c) => {
    const rec = { ...c.get("plan") };
    const months = c.get("months");
    const previous = rec.paid_until;
    rec.paid_until = extend(previous, months);
    c.set("planNext", { rec, months, previous });
    const board = c.env.BOARD_URL;
    if ((c.req.header("accept") || "").includes("application/json") && !(c.req.header("accept") || "").includes("text/html")) {
      return c.json({ ok: true, host: rec.host, months, amount: planPrice(c.env, months), paid_until: rec.paid_until });
    }
    return c.html(html("Paid, thank you",
      `<p>The seller plan for <b>${esc(rec.host)}</b> now runs until <b>${day(rec.paid_until)}</b> ` +
      `(${months} month${months > 1 ? "s" : ""}, ${esc(planPrice(c.env, months))} USDC on Base).</p>` +
      `<p>A receipt with the transaction link goes to <b>${esc(rec.email)}</b> with the board's next run, along with ` +
      `what happens next. Nothing renews by itself: a few days before the end date you'll get an email with a link ` +
      `like this one to pay for more time, and if you don't use it the plan simply ends. Free alerts carry on either way.</p>` +
      `<p class="muted">Paying again early adds the time on top, so you never lose days.</p>`, board));
  });
}
