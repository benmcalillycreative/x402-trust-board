"""The seller plan's bookkeeping on the board's computer. No invoices: the plan is paid in USDC on Base
through x402 (worker/src/plan.js), and this module keeps up with it.

Each cycle:
  - every settled payment the Worker queued is recorded in plans.json (once per transaction), the
    payer gets a receipt with the transaction link, the domain is added to their alerts, and the owner
    gets a note so the listing review can start;
  - a new plan gets its listing review: Coinbase's validator and merchant list run across every route
    on the domain (through the Worker's /v1/listing), grouped into what to fix, plus a ready-to-file
    request for the Agentic.Market card. The owner gets a copy;
  - on the first run of each month, every active plan gets a 30-day availability report per endpoint
    against Coinbase's bar for featured listings, with every failed check and what it returned;
  - five days before a plan runs out, the seller gets one email with pay links (1, 3 or 12 months);
  - when it has run out, one last email says so. Free alerts carry on; nothing else happens.

USDC can't be pulled from a wallet, so nothing is ever charged without the seller paying a link.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import alerts, config, score

PRICE_MONTHLY = 11.99
MONTHS = (1, 3, 12)
REMIND_DAYS = 5
BASESCAN = "https://basescan.org/tx/"
BOARD = "https://settlewatch.com/"
BAR, PRIORITY = 0.99, 0.995      # Coinbase: featured at 99% over 30 days, priority above 99.5%
REVIEW_MAX = 40                  # routes per listing review (each one is a call to Coinbase's validator)
REVIEW_PAUSE = 1.5               # seconds between them, to stay polite
MIN_CHECKS = 10                  # fewer counted checks than this is "too little history to judge"


def path():
    return config.DATA_DIR / "plans.json"


def load():
    try:
        data = json.loads(path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(plans):
    path().parent.mkdir(parents=True, exist_ok=True)
    tmp = path().with_suffix(".tmp")
    tmp.write_text(json.dumps(plans, indent=1, sort_keys=True))
    tmp.replace(path())


def _dt(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _day(iso):
    d = _dt(iso)
    return d.strftime("%B %-d, %Y") if d else "?"


def price(months):
    return f"${PRICE_MONTHLY * months:.2f}"


def pay_link(plan_id, months=1):
    return f"{config.PUBLIC_API}/v1/plan/pay?p={plan_id}&months={months}"


def pay_links(plan_id):
    return "\n".join(f"  {m} month{'s' if m > 1 else ''} ({price(m)}): {pay_link(plan_id, m)}" for m in MONTHS)


def active(now=None):
    now = now or datetime.now(timezone.utc)
    return {i: p for i, p in load().items() if (_dt(p.get("paid_until")) or now) > now}


def record(plans, it):
    """Add one queued payment to plans (in place). Returns (entry, is_new_tx)."""
    pid = it.get("id")
    entry = plans.setdefault(pid, {"id": pid, "host": it.get("host"), "email": (it.get("email") or "").lower(),
                                   "since": it.get("ts"), "payments": [], "paid_until": None,
                                   "mailed": [], "notified": [], "reminded": None, "ended": None})
    ref = it.get("tx") or it.get("key")
    if any((p.get("tx") or p.get("key")) == ref for p in entry["payments"]):
        return entry, False
    entry["payments"].append({k: it.get(k) for k in ("tx", "key", "payer", "network", "amount", "months", "ts")})
    cur, new = _dt(entry.get("paid_until")), _dt(it.get("paid_until"))
    if new and (not cur or new > cur):
        entry["paid_until"] = it.get("paid_until")
    return entry, True


def receipt_body(entry, pay):
    first = len(entry["payments"]) == 1
    tx = pay.get("tx")
    lines = [
        "Hi,",
        "",
        f"Thanks for paying for the Settle Watch seller plan for {entry['host']}.",
        "",
        f"  Paid: {pay.get('amount')} USDC on Base, {pay.get('months')} month{'s' if (pay.get('months') or 1) > 1 else ''}",
        f"  From: {pay.get('payer') or '-'}",
        f"  Transaction: {BASESCAN + tx if tx else '-'}",
        f"  Plan runs until: {_day(entry['paid_until'])}",
        "",
    ]
    if first:
        lines += [
            "What happens now:",
            f"  - Every endpoint on {entry['host']} is checked every cycle, and alerts about it go to this address.",
            "  - Within a few hours you'll get a listing review: Coinbase's own checks run across all your routes,",
            "    what to fix, and what to send Coinbase for your Agentic.Market card.",
            "  - On the 1st of each month, an availability report per endpoint against Coinbase's 99% bar.",
            "  - Reply to this email to add teammates to the alerts or to get help with a signed payout proof.",
            "",
        ]
    lines += [
        f"Nothing renews by itself. {REMIND_DAYS} days before the end date you'll get one email with a link to pay",
        "for more time. Paying early adds on top, so you never lose days. If you don't pay, the plan simply ends",
        "and your free alerts carry on.",
        "",
        "Grades are never for sale: the plan changes how closely you're watched, not how you're graded.",
        "",
        "Ben",
        "Settle Watch",
    ]
    return "\n".join(lines)


def owner_body(entry, pay, renewal):
    tx = pay.get("tx")
    return "\n".join([
        f"{'Renewal' if renewal else 'New seller plan'}: {entry['host']}",
        "",
        f"Email: {entry['email']}",
        f"Paid: {pay.get('amount')} for {pay.get('months')} month(s), from {pay.get('payer') or '-'}",
        f"Transaction: {BASESCAN + tx if tx else '-'}",
        f"Runs until: {_day(entry['paid_until'])}",
        "",
        "" if renewal else "The listing review goes out on its own with the next run; you'll get a copy.",
    ]).rstrip() + "\n"


def handle(plans, it, watching, log=print):
    """Process one queued payment. Returns True once the receipt is out, so the queue item can be acked."""
    entry, _ = record(plans, it)
    ref = it.get("tx") or it.get("key")
    pay = next(p for p in entry["payments"] if (p.get("tx") or p.get("key")) == ref)
    if (entry["email"], entry["host"]) not in watching:
        alerts.watch(entry["email"], entry["host"], "")
        watching.add((entry["email"], entry["host"]))
    # Reminder flags are keyed by the end date, so a payment that moves it re-arms them by itself.
    owner = config.ALERT_FROM or config.SMTP_USER
    if owner and ref not in entry["notified"]:
        renewal = len(entry["payments"]) > 1
        subject = f"{'Seller plan renewed' if renewal else 'New seller plan'}: {entry['host']} ({pay.get('amount')})"
        if alerts.deliver(owner, subject, owner_body(entry, pay, renewal), log=log) == "sent":
            entry["notified"].append(ref)
    if ref not in entry["mailed"]:
        subject = f"Receipt: Settle Watch seller plan for {entry['host']}, until {_day(entry['paid_until'])}"
        if alerts.deliver(entry["email"], subject, receipt_body(entry, pay), log=log) == "sent":
            entry["mailed"].append(ref)
    return ref in entry["mailed"]


def reminder_body(entry, ended):
    if ended:
        head = [f"The Settle Watch seller plan for {entry['host']} ended on {_day(entry['paid_until'])}.",
                "",
                "Your free alerts carry on. To pick the plan back up, pay any of these links from any wallet",
                "with USDC on Base (it starts again from the day you pay):"]
    else:
        head = [f"The Settle Watch seller plan for {entry['host']} runs until {_day(entry['paid_until'])}.",
                "",
                "Nothing renews by itself. To keep it going, pay any of these links from any wallet with USDC on",
                "Base. The time is added on top of what's left:"]
    return "\n".join(["Hi,", "", *head, "", pay_links(entry["id"]), "",
                      "If you'd rather let it go, there's nothing to do." if not ended else
                      "Thanks for having been on the plan.", "", "Ben", "Settle Watch"])


def remind(now=None, log=print):
    """Send the one 'running out' and the one 'ended' email per paid period."""
    now = now or datetime.now(timezone.utc)
    plans = load()
    sent = 0
    for entry in plans.values():
        until = _dt(entry.get("paid_until"))
        if not until:
            continue
        key = entry["paid_until"]
        if until <= now and entry.get("ended") != key:
            subject = f"Your Settle Watch seller plan for {entry['host']} has ended"
            if alerts.deliver(entry["email"], subject, reminder_body(entry, True), log=log) == "sent":
                entry["ended"] = key
                sent += 1
        elif now < until <= now + timedelta(days=REMIND_DAYS) and entry.get("reminded") != key:
            subject = f"Your Settle Watch seller plan for {entry['host']} ends {_day(key)}"
            if alerts.deliver(entry["email"], subject, reminder_body(entry, False), log=log) == "sent":
                entry["reminded"] = key
                sent += 1
    if sent:
        save(plans)
    live = sum(1 for p in plans.values() if (_dt(p.get("paid_until")) or now) > now)
    log(f"Seller plans: {live} active, {sent} reminder(s) sent")
    return sent


# ---- Listing review (once, when a plan starts) ---------------------------------------------------------

def host_endpoints(conn, host):
    rows = conn.execute("SELECT resource, method, service_name, description FROM endpoints "
                        "WHERE host=? AND COALESCE(delisted,0)=0 ORDER BY resource", (host,)).fetchall()
    return [dict(r) for r in rows]


def listing(url, method):
    """The Worker's listing check, authenticated so reviews aren't held to the public daily limit."""
    q = urllib.parse.urlencode({"url": url, "method": method})
    req = urllib.request.Request(f"{config.API_URL}/v1/listing?{q}", headers={
        "Authorization": f"Bearer {config.ALERTS_TOKEN}", "User-Agent": config.USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _pct(x):
    return "-" if x is None else f"{x * 100:.1f}%".replace(".0%", "%")


def review_body(entry, eps, results, total, uptime=None):
    host = entry["host"]
    checked = [(ep, r) for ep, r in results if "error" not in r]
    listed = sum(1 for _, r in checked if r.get("listed"))
    lines = ["Hi,", "",
             f"Here's the listing review for {host}. It runs Coinbase's own validator (the checks its Bazaar indexer "
             "makes) and its merchant list on "
             + (f"the first {len(results)} of your {total} routes." if total > len(results) else f"all {total} of your routes."),
             "", f"In the Bazaar now: {listed} of {len(checked)} checked."]
    groups = {}
    for ep, r in checked:
        for f in r.get("findings") or []:
            if f.get("status") not in ("fail", "warn"):
                continue
            g = groups.setdefault(f["title"], {"status": f["status"], "fix": f.get("fix"), "detail": f.get("detail"), "urls": []})
            g["urls"].append(ep["resource"])
    lines += ["", "WHAT TO FIX", ""]
    if not groups:
        lines.append("Nothing blocking: every route checked passes Coinbase's checks.")
    for title, g in sorted(groups.items(), key=lambda kv: (kv[1]["status"] != "fail", -len(kv[1]["urls"]), kv[0])):
        n = len(g["urls"])
        lines.append(f"{'[Fix]' if g['status'] == 'fail' else '[Check]'} {title} ({n} route{'s' if n != 1 else ''})")
        if g.get("detail"):
            lines.append(f"  What we saw: {g['detail'][:300]}")
        if g.get("fix"):
            lines.append(f"  Fix: {g['fix']}")
        for u in g["urls"][:5]:
            lines.append(f"    {u}")
        if n > 5:
            lines.append(f"    and {n - 5} more")
        lines.append("")
    lines += ["ROUTE BY ROUTE", ""]
    for ep, r in results:
        if "error" in r:
            lines.append(f"  Couldn't check   {ep['resource']} ({r['error']})")
        else:
            lines.append(f"  {'Listed    ' if r.get('listed') else 'Not listed'}  {ep['method']} {ep['resource']}")
            lines.append(f"              {r.get('summary', '')}")
    name = next((e["service_name"] for e in eps if e.get("service_name")), None) or host
    desc = next((e["description"] for e in eps if e.get("description")), None) or "(one line on what it does and who it's for)"
    lines += ["", "YOUR AGENTIC.MARKET CARD", "",
              "The service card on Agentic.Market (name, category, description) is curated by Coinbase, so no route",
              "setting fills it in. Ask for it with an issue at https://github.com/coinbase/cdp-sdk/issues/new. Here's a",
              "draft; fix anything that's off:", "",
              f"  Title: Agentic.Market card for {name} ({host})",
              f"  Service: {name}",
              f"  Domain: {host}",
              "  Category: (pick the closest one on Agentic.Market)",
              f"  Description: {desc[:300]}",
              "  Website: (your site)",
              f"  Bazaar listings: {listed} route{'s' if listed != 1 else ''} on {host}"]
    if uptime is not None:
        lines.append(f"  Availability: {_pct(uptime)} over 30 days by Settle Watch's checks, {BOARD}seller/{host}.html")
    lines += ["", "Once the fixes are in, Coinbase picks them up on the next settled payment through its facilitator.",
              "Reply to this email with any questions. Your availability report comes on the 1st.", "", "Ben", "Settle Watch"]
    return "\n".join(lines)


def review(conn, entry, log=print):
    eps = host_endpoints(conn, entry["host"])
    if not eps:
        log(f"Seller plans: no endpoints on file for {entry['host']} yet; review waits")
        return False
    for ep in eps:
        ep["method"] = "POST" if (ep.get("method") or "").upper() == "POST" else "GET"
    results = []
    for i, ep in enumerate(eps[:REVIEW_MAX]):
        if i:
            time.sleep(REVIEW_PAUSE)
        try:
            results.append((ep, listing(ep["resource"], ep["method"])))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            results.append((ep, {"error": str(exc)[:120]}))
    if not any("error" not in r for _, r in results):
        log(f"Seller plans: listing checks failed for {entry['host']}; review retries next run")
        return False
    av = score.availability(conn)
    counted = [av[e["resource"]] for e in eps if e["resource"] in av]
    up, n = sum(a[0] for a in counted), sum(a[1] for a in counted)
    body = review_body(entry, eps, results, len(eps), up / n if n else None)
    subject = f"Listing review for {entry['host']}"
    if alerts.deliver(entry["email"], subject, body, log=log) != "sent":
        return False
    owner = config.ALERT_FROM or config.SMTP_USER
    if owner and owner.lower() != entry["email"]:
        alerts.deliver(owner, f"[copy] {subject} (sent to {entry['email']})", body, log=log)
    return True


# ---- Monthly availability report ----------------------------------------------------------------------

def failed_checks(conn, resources, since):
    """Counted checks that didn't answer, newest first: {resource: [row, ...]}."""
    out = {}
    if not resources:
        return out
    marks = ",".join("?" * len(resources))
    for r in conn.execute(f"SELECT resource, ts, outcome, status_code, error FROM probes WHERE ts >= ? "
                          f"AND resource IN ({marks}) ORDER BY ts DESC", (since, *resources)):
        o = score.outcome_of(r)
        if o in score.UP or o in score.NOT_COUNTED:
            continue
        out.setdefault(r["resource"], []).append((r["ts"], o, r["status_code"], r["error"]))
    return out


def report_body(conn, entry, now):
    host = entry["host"]
    since_dt = now - timedelta(days=config.AVAILABILITY_DAYS)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    eps = host_endpoints(conn, host)
    av = score.availability(conn, now=now)
    rows = []
    for e in eps:
        up, n, hist = av.get(e["resource"], (0, 0, None))
        rows.append((e["resource"], up / n if n else None, n, hist))
    fails = failed_checks(conn, [r[0] for r in rows], since)

    def status(u, n):
        if n < MIN_CHECKS or u is None:
            return "too few checks"
        return "priority" if u > PRIORITY else "featured" if u >= BAR else "BELOW 99%"
    tally = {}
    for _, u, n, _ in rows:
        tally[status(u, n)] = tally.get(status(u, n), 0) + 1
    up_all, n_all = sum(av.get(e["resource"], (0, 0, 0))[0] for e in eps), sum(r[2] for r in rows)
    lines = ["Hi,", "",
             f"Your availability report for {host}: {since_dt.strftime('%B %-d')} to {now.strftime('%B %-d, %Y')} "
             "(the 30 days Coinbase measures for featured listings).", "",
             f"Overall: {_pct(up_all / n_all if n_all else None)} of {n_all} checks answered, across {len(rows)} endpoints.",
             f"  Above 99.5% (priority placement): {tally.get('priority', 0)}",
             f"  99% to 99.5% (meets the featured bar): {tally.get('featured', 0)}",
             f"  Below 99%: {tally.get('BELOW 99%', 0)}",
             f"  Too few checks to judge (under {MIN_CHECKS}): {tally.get('too few checks', 0)}",
             "", "PER ENDPOINT (worst first)", ""]
    order = sorted(rows, key=lambda r: (r[1] is None, r[1] if r[1] is not None else 2, r[0]))
    for res, u, n, hist in order:
        span = f", {hist} day{'s' if hist != 1 else ''} of history" if hist and hist < config.AVAILABILITY_DAYS else ""
        lines.append(f"  {_pct(u):>6}  {status(u, n):<15} {res}  ({n} check{'s' if n != 1 else ''}{span})")
    lines += ["", "FAILED CHECKS (what each one returned)", ""]
    if not fails:
        lines.append("None. Every counted check answered.")
    for res, u, n, hist in order:
        fs = fails.get(res)
        if not fs:
            continue
        lines.append(f"{res}: {len(fs)} failed")
        for ts, o, code, err in fs[:10]:
            what = f"HTTP {code}" if code else o.replace("_", " ")
            lines.append(f"  {ts[:16].replace('T', ' ')} UTC  {what}{f'  ({err[:100]})' if err else ''}")
        if len(fs) > 10:
            lines.append(f"  and {len(fs) - 10} more")
        lines.append("")
    lines += ["", "These are Settle Watch's own unpaid checks, a second opinion next to Coinbase's probe. A check counts",
              "as answered when the endpoint replies with its payment terms (or serves it free); checks that failed",
              "because of our side of the connection aren't counted against you.", "",
              f"Live numbers any time: {BOARD}seller/{host}.html", "",
              f"Your plan runs until {_day(entry['paid_until'])}.", "", "Ben", "Settle Watch"]
    return "\n".join(lines)


def monthly_reports(conn, plans, now, log=print):
    month = now.strftime("%Y-%m")
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sent = 0
    for entry in plans.values():
        until, since = _dt(entry.get("paid_until")), _dt(entry.get("since"))
        if entry.get("reported") == month or not until or until <= now or not since or since >= start:
            continue
        body = report_body(conn, entry, now)
        subject = f"Availability report for {entry['host']}, {(start - timedelta(days=1)).strftime('%B %Y')}"
        if alerts.deliver(entry["email"], subject, body, log=log) == "sent":
            entry["reported"] = month
            sent += 1
    return sent


def cycle(conn, now=None, log=print):
    """Everything time-driven for the plans: reviews for new plans, the monthly report, reminders."""
    now = now or datetime.now(timezone.utc)
    plans = load()
    reviewed = reports = 0
    if plans and config.API_URL and config.ALERTS_TOKEN:
        for entry in plans.values():
            if not entry.get("reviewed") and (_dt(entry.get("paid_until")) or now) > now:
                if review(conn, entry, log=log):
                    entry["reviewed"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                    reviewed += 1
                    save(plans)
    if plans:
        reports = monthly_reports(conn, plans, now, log=log)
        if reports:
            save(plans)
    if reviewed or reports:
        log(f"Seller plans: {reviewed} listing review(s), {reports} monthly report(s) sent")
    return remind(now=now, log=log)
