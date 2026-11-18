"""Email alerts for sellers who asked to be told when their endpoints change.

Watch list: ~/x402-trust-data/watchers.json (managed with `python3 -m x402trust watch`).
Watched endpoints are checked every cycle. After each cycle, each watcher gets at
most one email listing what changed since the last cycle:

  - stopped answering (two failed checks in a row, so one blip doesn't page anyone)
  - answering again
  - asks a different price than before
  - asks for payment to a different wallet than before (skipped for services that
    use a fresh address on every request)
  - removed from, or back in, the Bazaar catalog
  - newly listed under the watched name

The first cycle after someone is added sends a short "monitoring is on" email with
the current status, so they know it works.

Sending uses SMTP (for Gmail: an app password; see the README). If SMTP isn't set
up, emails are written to ~/x402-trust-data/outbox/ instead and nothing is sent.
"""

import json
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import urlsplit

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
    }


def events_for(resource, prev, cur):
    """Plain-English lines describing what changed. Empty list if nothing did."""
    ev = []
    if prev is None:
        if cur["status"] != "retired":
            ev.append(("new", f"Newly listed: {resource}"))
        return ev
    if prev["status"] == "up" and cur["status"] == "down":
        code = f" (HTTP {cur['last_code']})" if cur.get("last_code") else ""
        ev.append(("down", f"Stopped answering{code} on the last two checks: {resource}"))
    elif prev["status"] == "down" and cur["status"] == "up":
        ev.append(("up", f"Answering again: {resource}"))
    if prev.get("amount") and cur.get("amount") and prev["amount"] != cur["amount"]:
        listed = f" Its listing says {money(cur['listed_usd'])}." if cur.get("listed_usd") is not None else ""
        ev.append(("price", f"Now asks {money(cur['usd'])} (was {money(prev.get('usd'))}): {resource}.{listed}"))
    if (prev.get("pay_to") and cur.get("pay_to") and prev["pay_to"] != cur["pay_to"]
            and not cur.get("rotating") and not prev.get("rotating")):
        ev.append(("wallet", f"Now asks for payment to {cur['pay_to']} (was {prev['pay_to']}): {resource}"))
    if not prev.get("delisted") and cur.get("delisted"):
        ev.append(("delisted", f"Removed from the Bazaar catalog: {resource}"))
    elif prev.get("delisted") and not cur.get("delisted"):
        ev.append(("relisted", f"Back in the Bazaar catalog: {resource}"))
    return ev


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


FOOTER = ("\n\n--\nYou're getting this because you asked for x402 Trust Board alerts. "
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
    lines += ["", "From here on you'll get one email per check when something changes: an endpoint stops "
              "answering or comes back, or starts asking a different price or wallet. No change, no email.",
              "", "Ben"]
    return "\n".join(lines) + footer(w)


def footer(w):
    from . import signups
    link = signups.stop_link(w.get("email", ""), w.get("match", ""))
    return FOOTER + (f"\nOr unsubscribe in one click: {link}" if link else "")


def change_body(w, events):
    order = {"down": 0, "wallet": 1, "price": 2, "delisted": 3, "up": 4, "relisted": 5, "new": 6}
    events = sorted(events, key=lambda e: order.get(e[0], 9))
    lines = [f"Hi{(' ' + w['name']) if w.get('name') else ''},", "",
             f"Here's what changed on {w['match']} since the last check:", ""]
    lines += [f"- {text}" for _, text in events[:50]]
    if len(events) > 50:
        lines.append(f"- …and {len(events) - 50} more")
    lines += ["", "Ben"]
    return "\n".join(lines) + footer(w)


# ---------- the alert step ----------

def run(conn=None, dry_run=False, log=print):
    conn = conn or db.connect()
    conn.executescript(SCHEMA)
    watchers = load_watchers()
    if not watchers:
        return 0
    notes = owner_notes.load()
    sent = 0
    for w in watchers:
        email = w["email"]
        prev_rows = {r["resource"]: json.loads(r["state"]) for r in conn.execute(
            "SELECT resource, state FROM alert_state WHERE email=?", (email,))}
        first_time = not prev_rows
        states, events = {}, []
        for ep in _endpoints_for(conn, w["match"]):
            prev = prev_rows.get(ep["resource"])
            if ep["delisted"] and (prev is None or prev.get("delisted")):
                continue
            cur = current_state(conn, ep, prev, notes)
            states[ep["resource"]] = cur
            if not first_time:
                events += events_for(ep["resource"], prev, cur)
        if first_time:
            subject = f"x402 Trust Board: monitoring is on for {w['match']}"
            body = welcome_body(w, states)
        elif events:
            n = len(events)
            subject = f"x402 alert: {n} change{'s' if n != 1 else ''} on {w['match']}"
            body = change_body(w, events)
        else:
            subject = body = None

        if body:
            if dry_run:
                log(f"[dry run] To {email}: {subject}\n{body}\n")
                continue
            delivery = deliver(email, subject, body, log)
            conn.execute("INSERT INTO alert_log VALUES (?,?,?,?,?)",
                         (db.now(), email, subject, len(events), delivery))
            log(f"Alert to {email}: {subject} ({delivery})")
            if delivery == "sent":
                sent += 1
            elif delivery.startswith("failed"):
                continue  # keep old state so the same changes go out next cycle
        if not dry_run:
            now = db.now()
            for resource, cur in states.items():
                conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?,?,?,?)",
                             (email, resource, json.dumps(cur), now))
            conn.commit()
    return sent
