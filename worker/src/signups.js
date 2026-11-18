// Self-serve alert sign-ups with double opt-in.
//
// The Worker only holds requests. The board's own computer (which already sends the alert
// emails) pulls them, sends the confirmation email, and adds confirmed watchers. So the only
// mail ever sent comes from the same address as the alerts themselves.
//
//   POST /v1/alerts/subscribe   form: email, host           (public)
//   POST /v1/alerts/pro         form: email, host, note     (public: interest in the seller plan)
//   GET  /v1/alerts/confirm?t=  link in the confirmation     (public)
//   GET  /v1/alerts/stop?e=&m=&s=  one-click unsubscribe     (public, HMAC-signed by the board)
//   GET  /v1/alerts/queue       pending work for the board   (Bearer ALERTS_TOKEN)
//   POST /v1/alerts/ack         {key, state}                 (Bearer ALERTS_TOKEN)

import { Hono } from "hono";

const DAY = 86400;
const EMAIL = /^[^\s@<>()",;:]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,}$/i;
const HOST = /^[a-z0-9.-]{3,253}$/;

const page = (title, msg, board) =>
  `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
  `<title>${title}</title><style>body{font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:12vh auto;padding:0 16px;` +
  `color:#1d1d1b;background:#fafaf7}@media(prefers-color-scheme:dark){body{color:#eee;background:#161614}a{color:#9cf}}</style>` +
  `<h1>${title}</h1><p>${msg}</p><p><a href="${board}">Back to the x402 Trust Board</a></p>`;

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

async function sha(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function hmac(secret, msg) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

async function bump(kv, key, ttl, max) {
  const n = parseInt((await kv.get(key)) || "0", 10) + 1;
  await kv.put(key, String(n), { expirationTtl: ttl });
  return n <= max;
}

const authed = (c) => {
  const want = c.env.ALERTS_TOKEN;
  return want && c.req.header("authorization") === `Bearer ${want}`;
};

export const signups = new Hono();

signups.use("*", async (c, next) => {
  if (!c.env.SIGNUPS) return c.json({ error: "Sign-ups aren't set up on this deployment." }, 503);
  return next();
});

signups.post("/subscribe", async (c) => {
  const form = await c.req.parseBody().catch(() => ({}));
  const email = String(form.email || "").trim().toLowerCase();
  const host = String(form.host || "").trim().toLowerCase();
  const board = c.env.BOARD_URL;
  const reply = (code, title, msg) => c.html(page(title, msg, board), code);
  if (form.website) return reply(200, "Thanks", "Check your inbox."); // honeypot: bots fill every field
  if (!EMAIL.test(email) || email.length > 254) return reply(400, "That email doesn't look right", "Go back and check it.");
  if (!HOST.test(host)) return reply(400, "Unknown seller", "Pick a seller from the board.");
  const ip = c.req.header("cf-connecting-ip") || "unknown";
  const hour = Math.floor(Date.now() / 3600e3);
  if (!(await bump(c.env.SIGNUPS, `rl:ip:${ip}:${hour}`, 3600, 5)) ||
      !(await bump(c.env.SIGNUPS, `rl:em:${await sha(email)}:${Math.floor(hour / 24)}`, DAY, 3))) {
    return reply(429, "Too many requests", "Try again later.");
  }
  const seller = await fetch(`${board}seller/${host}.json`, { cf: { cacheTtl: 600, cacheEverything: true } });
  if (seller.status !== 200) return reply(404, "Unknown seller", "The board doesn't list that seller.");
  const token = crypto.randomUUID().replace(/-/g, "");
  await c.env.SIGNUPS.put(`p:${token}`, JSON.stringify({ email, host, ts: new Date().toISOString(), state: "new" }), {
    expirationTtl: 7 * DAY,
  });
  return reply(200, "Almost done",
    `We'll email <b>${esc(email)}</b> a confirmation link for <b>${esc(host)}</b> within a few hours ` +
    `(the board runs every six). Click it and monitoring starts. Nothing happens if you don't.`);
});

signups.post("/pro", async (c) => {
  const form = await c.req.parseBody().catch(() => ({}));
  const email = String(form.email || "").trim().toLowerCase();
  const host = String(form.host || "").trim().toLowerCase().replace(/^https?:\/\//, "").split("/")[0];
  const note = String(form.note || "").trim().slice(0, 1000);
  const board = c.env.BOARD_URL;
  const reply = (code, title, msg) => c.html(page(title, msg, board), code);
  if (form.website) return reply(200, "Thanks", "We'll be in touch.");
  if (!EMAIL.test(email) || email.length > 254) return reply(400, "That email doesn't look right", "Go back and check it.");
  if (host && !HOST.test(host)) return reply(400, "That service address doesn't look right", "Use its domain, like api.example.com.");
  const ip = c.req.header("cf-connecting-ip") || "unknown";
  if (!(await bump(c.env.SIGNUPS, `rl:pro:${ip}:${Math.floor(Date.now() / 3600e3)}`, 3600, 5))) {
    return reply(429, "Too many requests", "Try again later.");
  }
  const token = crypto.randomUUID().replace(/-/g, "");
  await c.env.SIGNUPS.put(`i:${token}`, JSON.stringify({ email, host, note, ts: new Date().toISOString(), state: "new" }), {
    expirationTtl: 30 * DAY,
  });
  return reply(200, "Thanks", `We'll email <b>${esc(email)}</b> about early access${host ? ` for <b>${esc(host)}</b>` : ""}.`);
});

signups.get("/confirm", async (c) => {
  const t = (c.req.query("t") || "").replace(/[^a-f0-9]/g, "");
  const board = c.env.BOARD_URL;
  const raw = t && (await c.env.SIGNUPS.get(`p:${t}`));
  if (!raw) return c.html(page("Link expired", "That confirmation link has expired or was already used. Sign up again from the board.", board), 404);
  const rec = JSON.parse(raw);
  if (rec.state !== "confirmed") {
    rec.state = "confirmed";
    rec.confirmed = new Date().toISOString();
    await c.env.SIGNUPS.put(`p:${t}`, JSON.stringify(rec), { expirationTtl: 7 * DAY });
  }
  return c.html(page("Confirmed", `Alerts for <b>${esc(rec.host)}</b> start with the board's next run. ` +
    `You'll get a "monitoring is on" email first, then one email only when something changes.`, board));
});

signups.get("/stop", async (c) => {
  const e = (c.req.query("e") || "").toLowerCase(), m = c.req.query("m") || "", s = c.req.query("s") || "";
  const board = c.env.BOARD_URL;
  if (!c.env.ALERTS_TOKEN || !e || s !== (await hmac(c.env.ALERTS_TOKEN, `${e}|${m}`))) {
    return c.html(page("Link not valid", "Reply \"stop\" to any alert email instead and you'll be taken off.", board), 400);
  }
  await c.env.SIGNUPS.put(`s:${await sha(`${e}|${m}`)}`, JSON.stringify({ email: e, match: m, ts: new Date().toISOString() }), {
    expirationTtl: 30 * DAY,
  });
  return c.html(page("Unsubscribed", `No more alerts for <b>${esc(m || "anything")}</b> will go to <b>${esc(e)}</b> after the board's next run.`, board));
});

signups.get("/queue", async (c) => {
  if (!authed(c)) return c.json({ error: "unauthorized" }, 401);
  const out = [];
  for (const prefix of ["p:", "s:", "i:"]) {
    let cursor;
    do {
      const page_ = await c.env.SIGNUPS.list({ prefix, cursor });
      for (const k of page_.keys) {
        const v = await c.env.SIGNUPS.get(k.name);
        if (v) out.push({ key: k.name, ...JSON.parse(v), kind: { "p:": "signup", "s:": "stop", "i:": "interest" }[prefix] });
      }
      cursor = page_.list_complete ? null : page_.cursor;
    } while (cursor);
  }
  return c.json({ items: out });
});

signups.post("/ack", async (c) => {
  if (!authed(c)) return c.json({ error: "unauthorized" }, 401);
  const { key, state } = await c.req.json().catch(() => ({}));
  if (!/^[psi]:[a-f0-9]{16,64}$/.test(key || "")) return c.json({ error: "bad key" }, 400);
  if (state === "done") {
    await c.env.SIGNUPS.delete(key);
  } else if (state === "mailed") {
    const raw = await c.env.SIGNUPS.get(key);
    if (raw) {
      const rec = JSON.parse(raw);
      if (rec.state === "new") rec.state = "mailed";
      await c.env.SIGNUPS.put(key, JSON.stringify(rec), { expirationTtl: 7 * DAY });
    }
  } else {
    return c.json({ error: "state must be mailed or done" }, 400);
  }
  return c.json({ ok: true });
});
