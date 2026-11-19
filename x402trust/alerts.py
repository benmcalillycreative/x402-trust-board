"""Email alerts for sellers who asked to be told when their endpoints change.

Watch list: ~/x402-trust-data/watchers.json (managed with `python3 -m x402trust watch`).
Watched endpoints are checked every cycle. Changes are bundled so nobody gets buried:

  - one email per person per cycle at most, covering every service they watch;
  - urgent changes go out with the next cycle: an endpoint stopped answering (two failed checks
    in a row, so one blip doesn't page anyone) or answers again, asks to be paid at a different
    wallet (skipped for services that use a fresh address on every request), or was removed
    from the Bazaar catalog;
  - everything else waits for a daily digest (at most one a day): price changes, new listings,
    back in the catalog, and availability over 30 days falling below or back above the 99%
    Coinbase wants for featured listings. Anything waiting rides along with an urgent email;
  - changes that undo themselves before they're sent (a price that moves and moves back, a dip
    below 99% that recovers) are dropped, and long lists are grouped and capped.

The first cycle after someone starts watching a service sends a short "monitoring is on" email
with the current status, so they know it works (one per service, even for people who already
watch another one).

Sending uses SMTP (for Gmail: an app password; see the README). If SMTP isn't set
up, emails are written to ~/x402-trust-data/outbox/ instead and nothing is sent.
"""

import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import urlsplit

URGENT = ("down", "up", "wallet", "delisted")   # sent with the next cycle
DIGEST_HOURS = 20                               # everything else: at most one digest a day
LIST_MAX = 10                                   # lines per group before "and N more"

from . import config, db
from . import notes as owner_notes
from .parse import norm_addr
from .score import UP, NOT_COUNTED, outcome_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state (
    email     TEXT NOT NULL,
    resource  TEXT NOT NULL,
    state     TEXT NOT NULL,
    updated   TEXT NOT NULL,
    PRIMARY KEY (email, resource)
);
CREATE TABLE IF NOT EXISTS alert_pending (
    email     TEXT NOT NULL,
    resource  TEXT NOT NULL,
    kind      TEXT NOT NULL,     -- down | up | wallet | delisted | price | new | relisted | bar | bar_ok
    match     TEXT NOT NULL,     -- the watched service it belongs to
    data      TEXT NOT NULL,     -- what to say (JSON); for price, the first "was" and the latest "now"
    first_ts  TEXT NOT NULL,
    ts        TEXT NOT NULL,
    PRIMARY KEY (email, resource, kind)
);
CREATE TABLE IF NOT EXISTS alert_log (
    ts        TEXT NOT NULL,
    email     TEXT NOT NULL,
    subject   TEXT NOT NULL,
    events    INTEGER,
    delivery  TEXT NOT NULL      -- sent | outbox | failed: <reason>
);
"""


# ---------- watch list ----------

def watchers_path():
    return config.DATA_DIR / "watchers.json"


def load_watchers():
    try:
        data = json.loads(watchers_path().read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_watchers(items):
    watchers_path().parent.mkdir(parents=True, exist_ok=True)
    watchers_path().write_text(json.dumps(items, indent=1))


def watch(email, match, name=""):
    items = [w for w in load_watchers() if not (w["email"] == email and w["match"] == match)]
    items.append({"email": email, "match": match, "name": name,
                  "added": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    save_watchers(items)


def unwatch(email, match=None):
    items = load_watchers()
    kept = [w for w in items if not (w["email"].lower() == email.lower() and (match is None or w["match"] == match))]
    save_watchers(kept)
    return len(items) - len(kept)


def watched_resources(conn):
    """Every currently listed endpoint someone is watching (probed every cycle)."""
    out = []
    for w in load_watchers():
        out += [r["resource"] for r in _endpoints_for(conn, w["match"])]
    return sorted(set(out))


def _endpoints_for(conn, match):
    if "://" in match:
        rows = conn.execute("SELECT * FROM endpoints WHERE resource LIKE ? ESCAPE '\\'",
                            (match.replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM endpoints WHERE host = ?", (match.lower(),)).fetchall()
    return rows


# ---------- state ----------

def pct(x):
    if x is None:
        return "an unknown share"
    return f"{round(100 * x, 1):g}%"


def money(v):
    if v is None:
        return "an unknown amount"
    if v >= 1:
        return f"${v:,.2f}"
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "$" + (s + "0" if len(s.split(".")[-1]) == 1 else s)


def current_state(conn, ep, prev, notes):
    probes = conn.execute(
        "SELECT * FROM probes WHERE resource=? ORDER BY ts DESC LIMIT 6", (ep["resource"],)
    ).fetchall()
    note = owner_notes.find(notes, ep["resource"], list(reversed(probes)))
    counted = [p for p in probes if outcome_of(p) not in NOT_COUNTED]
    if note and note["status"] == "retired":
        status = "retired"
    elif counted and outcome_of(counted[0]) in UP:
        status = "up"
    elif len(counted) >= 2 and all(outcome_of(p) not in UP for p in counted[:2]):
        status = "down"
    else:
        status = (prev or {}).get("status", "unknown")
    healthy = [p for p in probes if p["outcome"] == "healthy"]
    last = healthy[0] if healthy else None
    sc = conn.execute("SELECT uptime_30d, probes_30d, history_days FROM scores WHERE resource=?",
                      (ep["resource"],)).fetchone()
    # Only judge the bar on enough history: a week and ten checks, so a new endpoint's first blip doesn't alert.
    below = None
    if sc and sc["uptime_30d"] is not None and (sc["probes_30d"] or 0) >= 10 and (sc["history_days"] or 0) >= 7:
        below = sc["uptime_30d"] < config.CURATION_BAR
    quotes = [norm_addr(p["quoted_pay_to"]) for p in healthy[:3] if p["quoted_pay_to"]]
    return {
        "status": status,
        "amount": last["quoted_amount"] if last else (prev or {}).get("amount"),
        "usd": last["quoted_usd"] if last else (prev or {}).get("usd"),
        "pay_to": norm_addr(last["quoted_pay_to"]) if last and last["quoted_pay_to"] else (prev or {}).get("pay_to"),
        "rotating": len(quotes) >= 3 and len(set(quotes)) == len(quotes),
        "listed_usd": ep["listed_usd"],
        "delisted": bool(ep["delisted"]),
        "last_code": counted[0]["status_code"] if counted else None,
        "last_check": probes[0]["ts"] if probes else None,
        "below_bar": below,
        "uptime_30d": sc["uptime_30d"] if sc else None,
    }


def event_records(resource, prev, cur):
    """What changed for one endpoint, as (kind, data) pairs. Empty if nothing did."""
    ev = []
    if prev is None:
        if cur["status"] != "retired":
            ev.append(("new", {}))
        return ev
    if prev["status"] == "up" and cur["status"] == "down":
        ev.append(("down", {"code": cur.get("last_code")}))
    elif prev["status"] == "down" and cur["status"] == "up":
        ev.append(("up", {}))
    if prev.get("amount") and cur.get("amount") and prev["amount"] != cur["amount"]:
        ev.append(("price", {"was_amount": prev["amount"], "was_usd": prev.get("usd"), "amount": cur["amount"],
                             "usd": cur.get("usd"), "listed_usd": cur.get("listed_usd")}))
    if (prev.get("pay_to") and cur.get("pay_to") and prev["pay_to"] != cur["pay_to"]
            and not cur.get("rotating") and not prev.get("rotating")):
        ev.append(("wallet", {"was": prev["pay_to"], "now": cur["pay_to"]}))
    if prev.get("below_bar") is False and cur.get("below_bar") is True:
        ev.append(("bar", {"uptime_30d": cur.get("uptime_30d")}))
    elif prev.get("below_bar") is True and cur.get("below_bar") is False:
        ev.append(("bar_ok", {"uptime_30d": cur.get("uptime_30d")}))
    if not prev.get("delisted") and cur.get("delisted"):
        ev.append(("delisted", {}))
    elif prev.get("delisted") and not cur.get("delisted"):
        ev.append(("relisted", {}))
    return ev


def describe(kind, resource, d):
    """One plain-English line for an event."""
    if kind == "new":
        return f"Newly listed: {resource}"
    if kind == "down":
        code = f" (HTTP {d['code']})" if d.get("code") else ""
        return f"Stopped answering{code} on the last two checks: {resource}"
    if kind == "up":
        return f"Answering again: {resource}"
    if kind == "price":
        listed = f" Its listing says {money(d['listed_usd'])}." if d.get("listed_usd") is not None else ""
        return f"Now asks {money(d.get('usd'))} (was {money(d.get('was_usd'))}): {resource}.{listed}"
    if kind == "wallet":
        return f"Now asks for payment to {d['now']} (was {d['was']}): {resource}"
    if kind == "bar":
        return (f"Availability over 30 days fell to {pct(d.get('uptime_30d'))}, below the 99% Coinbase "
                f"wants for featured Bazaar listings: {resource}")
    if kind == "bar_ok":
        return f"Back above 99% availability over 30 days ({pct(d.get('uptime_30d'))}): {resource}"
    if kind == "delisted":
        return f"Removed from the Bazaar catalog: {resource}"
    if kind == "relisted":
        return f"Back in the Bazaar catalog: {resource}"
    return f"{kind}: {resource}"


def events_for(resource, prev, cur):
    """Plain-English lines describing what changed. Empty list if nothing did."""
    return [(k, describe(k, resource, d)) for k, d in event_records(resource, prev, cur)]


# ---------- email ----------

def smtp_ready():
    return bool(config.SMTP_USER and config.SMTP_PASS)


def deliver(to, subject, body, log=print):
    msg = EmailMessage()
    sender = config.ALERT_FROM or config.SMTP_USER or "alerts@localhost"
    msg["From"] = formataddr((config.ALERT_FROM_NAME, sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else "localhost")
    msg.set_content(body, cte="8bit")
    if not smtp_ready():
        outbox = config.DATA_DIR / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{to.replace('@', '_at_')}.eml"
        (outbox / name).write_bytes(bytes(msg))
        log(f"  (SMTP not set up) saved to outbox/{name}")
        return "outbox"
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
        return "sent"
    except Exception as exc:  # keep the cycle going; the log says what failed
        log(f"  Email to {to} failed: {exc}")
        return f"failed: {exc}"[:200]


FOOTER = ("\n\n--\nYou're getting this because you asked for Settle Watch alerts. "
          "Just reply \"stop\" and I'll take you off the list.")


def welcome_body(w, states):
    total = len(states)
    up = sum(1 for s in states.values() if s["status"] == "up")
    down = sorted(r for r, s in states.items() if s["status"] == "down")
    retired = sum(1 for s in states.values() if s["status"] == "retired")
    lines = [
        f"Hi{(' ' + w['name']) if w.get('name') else ''},",
        "",
        f"Monitoring is on for {w['match']}. I'm watching {total} listed endpoint{'s' if total != 1 else ''}, "
        "checked every six hours without paying.",
        "",
        f"Right now: {up} answering"
        + (f", {len(down)} not answering" if down else "")
        + (f", {retired} marked retired at your request" if retired else "")
        + (f", {total - up - len(down) - retired} not checked yet" if total - up - len(down) - retired else "")
        + ".",
    ]
    if down:
        lines += ["", "Not answering:"] + [f"  {r}" for r in down[:20]]
        if len(down) > 20:
            lines.append(f"  …and {len(down) - 20} more")
    lines += ["", "From here on: if an endpoint stops answering or comes back, asks to be paid at a different "
              "wallet, or drops out of the Bazaar, you'll hear after the next check. Smaller changes (prices, new "
              "listings, 99% availability over 30 days) come together in one email a day at most. No change, no email.",
              "", "Ben"]
    return "\n".join(lines) + footer(w)


def footer(w):
    from . import signups
    link = signups.stop_link(w.get("email", ""), w.get("match", ""))
    return FOOTER + (f"\nOr unsubscribe in one click: {link}" if link else "")


GROUPS = [  # (kind, heading), in the order they appear in an email
    ("down", "Stopped answering on the last two checks"),
    ("wallet", "Asks to be paid at a different wallet"),
    ("delisted", "Removed from the Bazaar catalog"),
    ("up", "Answering again"),
    ("price", "Price changes"),
    ("bar", "Fell below 99% availability over 30 days (Coinbase's bar for featured listings)"),
    ("bar_ok", "Back above 99% availability over 30 days"),
    ("relisted", "Back in the Bazaar catalog"),
    ("new", "Newly listed"),
]


def _short(resource, match):
    """The endpoint's path when it's on the watched host, so long lists stay readable."""
    host = match.split("://")[-1].split("/")[0].lower()
    u = urlsplit(resource)
    if u.netloc.lower() == host:
        return (u.path or "/") + (f"?{u.query}" if u.query else "")
    return resource


def _item(kind, res, d):
    if kind == "down":
        return res + (f" (HTTP {d['code']})" if d.get("code") else "")
    if kind == "price":
        listed = f" (listing says {money(d['listed_usd'])})" if d.get("listed_usd") is not None else ""
        return f"{res}: {money(d.get('was_usd'))} -> {money(d.get('usd'))}{listed}"
    if kind == "wallet":
        return f"{res}: now {d['now']} (was {d['was']})"
    if kind in ("bar", "bar_ok"):
        return f"{res}: {pct(d.get('uptime_30d'))}"
    return res


def net_out(items):
    """Drop changes that undid themselves while waiting: a price back where it was, a dip below 99% that
    recovered. items: [(match, resource, kind, data)]"""
    keys = {(r, k) for _, r, k, _ in items}
    out = []
    for m, r, k, d in items:
        if k == "price" and d.get("was_amount") == d.get("amount"):
            continue
        if k in ("bar", "bar_ok") and (r, "bar" if k == "bar_ok" else "bar_ok") in keys:
            continue
        out.append((m, r, k, d))
    return out


def summary(items):
    counts = {}
    for _, _, k, _ in items:
        counts[k] = counts.get(k, 0) + 1
    words = {"down": ("endpoint stopped answering", "endpoints stopped answering"),
             "up": ("endpoint answering again", "endpoints answering again"),
             "wallet": ("wallet change", "wallet changes"), "delisted": ("removed from the Bazaar", "removed from the Bazaar"),
             "price": ("price change", "price changes"), "new": ("new listing", "new listings"),
             "bar": ("below 99%", "below 99%"), "bar_ok": ("back above 99%", "back above 99%"),
             "relisted": ("back in the Bazaar", "back in the Bazaar")}
    parts = [f"{counts[k]} {words[k][counts[k] != 1]}" for k, _ in GROUPS if counts.get(k)]
    return ", ".join(parts)


def change_body(w, items, urgent):
    """One email for one person: every service they watch, grouped by service and kind."""
    services = []
    for m, _, _, _ in items:
        if m not in services:
            services.append(m)
    head = ("Something needs a look" if urgent else "Here's what changed") + (
        f" on {services[0]}" if len(services) == 1 else f" on {len(services)} of the services you watch")
    lines = [f"Hi{(' ' + w['name']) if w.get('name') else ''},", "", head + ":"]
    for m in services:
        mine = [(r, k, d) for mm, r, k, d in items if mm == m]
        if len(services) > 1:
            lines += ["", m, "-" * min(len(m), 60)]
        for kind, heading in GROUPS:
            group = sorted((_item(kind, _short(r, m), d) for r, k, d in mine if k == kind))
            if not group:
                continue
            lines += ["", f"{heading} ({len(group)}):"]
            lines += [f"  {g}" for g in group[:LIST_MAX]]
            if len(group) > LIST_MAX:
                lines.append(f"  and {len(group) - LIST_MAX} more")
        host = m.split("://")[-1].split("/")[0]
        lines += ["", f"Live status: https://settlewatch.com/seller/{host}.html"]
    if not urgent:
        lines += ["", "Changes like these come together once a day at most. You'll hear sooner if an endpoint stops "
                  "answering or its wallet changes."]
    lines += ["", "Ben"]
    return "\n".join(lines) + footer(w)


# ---------- the alert step ----------

def queue(conn, email, match, resource, kind, data, now):
    row = conn.execute("SELECT data FROM alert_pending WHERE email=? AND resource=? AND kind=?",
                       (email, resource, kind)).fetchone()
    if row and kind == "price":  # keep the first "was" so a change that undoes itself nets out
        old = json.loads(row["data"])
        data = {**data, "was_amount": old.get("was_amount"), "was_usd": old.get("was_usd")}
    if row:
        conn.execute("UPDATE alert_pending SET data=?, match=?, ts=? WHERE email=? AND resource=? AND kind=?",
                     (json.dumps(data), match, now, email, resource, kind))
    else:
        conn.execute("INSERT INTO alert_pending VALUES (?,?,?,?,?,?,?)",
                     (email, resource, kind, match, json.dumps(data), now, now))


def _last_sent(conn, email):
    row = conn.execute("SELECT MAX(ts) FROM alert_log WHERE email=? AND delivery IN ('sent', 'outbox')",
                       (email,)).fetchone()
    return row[0] if row else None


def _hours_since(ts, now):
    try:
        then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (now - then).total_seconds() / 3600


def run(conn=None, dry_run=False, log=print, now=None):
    conn = conn or db.connect()
    conn.executescript(SCHEMA)
    watchers = load_watchers()
    if not watchers:
        return 0
    now_dt = now or datetime.now(timezone.utc)
    notes = owner_notes.load()
    sent = 0
    people = {}
    for w in watchers:
        people.setdefault(w["email"], []).append(w)
    for email, ws in people.items():
        prev_rows = {r["resource"]: json.loads(r["state"]) for r in conn.execute(
            "SELECT resource, state FROM alert_state WHERE email=?", (email,))}
        keep = {}  # resource -> state to save
        stamp = db.now()
        for w in ws:
            eps = [ep for ep in _endpoints_for(conn, w["match"])
                   if not (ep["delisted"] and (prev_rows.get(ep["resource"]) is None
                                               or prev_rows[ep["resource"]].get("delisted")))]
            # First cycle for this service (even if this person already watches another one): say hello.
            first_time = not prev_rows or (bool(eps) and not any(ep["resource"] in prev_rows for ep in eps))
            states = {}
            for ep in eps:
                prev = prev_rows.get(ep["resource"])
                cur = current_state(conn, ep, prev, notes)
                states[ep["resource"]] = cur
                if not first_time and not dry_run:
                    for kind, data in event_records(ep["resource"], prev, cur):
                        queue(conn, email, w["match"], ep["resource"], kind, data, stamp)
            if first_time:
                subject = f"Settle Watch: monitoring is on for {w['match']}"
                body = welcome_body(w, states)
                if dry_run:
                    log(f"[dry run] To {email}: {subject}\n{body}\n")
                    continue
                delivery = deliver(email, subject, body, log)
                conn.execute("INSERT INTO alert_log VALUES (?,?,?,?,?)", (db.now(), email, subject, 0, delivery))
                log(f"Alert to {email}: {subject} ({delivery})")
                if delivery == "sent":
                    sent += 1
                elif delivery.startswith("failed"):
                    continue  # don't record state, so the welcome is tried again next cycle
            keep.update(states)
        if dry_run:
            continue
        # Everything waiting for this person, across every service they still watch.
        watched = [w["match"] for w in ws]
        conn.execute(f"DELETE FROM alert_pending WHERE email=? AND match NOT IN ({','.join('?' * len(watched))})",
                     (email, *watched))
        rows = conn.execute("SELECT match, resource, kind, data, first_ts FROM alert_pending WHERE email=?",
                            (email,)).fetchall()
        items = net_out([(r["match"], r["resource"], r["kind"], json.loads(r["data"])) for r in rows])
        if rows and not items:
            conn.execute("DELETE FROM alert_pending WHERE email=?", (email,))  # it all undid itself
        urgent = any(k in URGENT for _, _, k, _ in items)
        since = _hours_since(_last_sent(conn, email), now_dt)
        if items and (urgent or since is None or since >= DIGEST_HOURS):
            w = ws[0]
            what = summary(items)
            services = sorted({m for m, _, _, _ in items})
            where = services[0] if len(services) == 1 else f"{len(services)} services"
            subject = (f"Settle Watch alert: {what} on {where}" if urgent else f"Settle Watch daily: {what} on {where}")
            if len(subject) > 140:
                subject = subject[:137] + "..."
            body = change_body(w, items, urgent)
            delivery = deliver(email, subject, body, log)
            conn.execute("INSERT INTO alert_log VALUES (?,?,?,?,?)", (db.now(), email, subject, len(items), delivery))
            log(f"Alert to {email}: {subject} ({delivery})")
            if delivery == "sent":
                sent += 1
            if not delivery.startswith("failed"):
                conn.execute("DELETE FROM alert_pending WHERE email=?", (email,))
        for resource, cur in keep.items():
            conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?,?,?,?)",
                         (email, resource, json.dumps(cur), stamp))
        conn.commit()
    return sent
