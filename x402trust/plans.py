"""The seller plan's bookkeeping on the board's computer. No invoices: the plan is paid in USDC on Base
through x402 (worker/src/plan.js), and this module keeps up with it.

Each cycle:
  - every settled payment the Worker queued is recorded in plans.json (once per transaction), the
    payer gets a receipt with the transaction link, the domain is added to their alerts, and the owner
    gets a note so the listing review can start;
  - five days before a plan runs out, the seller gets one email with pay links (1, 3 or 12 months);
  - when it has run out, one last email says so. Free alerts carry on; nothing else happens.

USDC can't be pulled from a wallet, so nothing is ever charged without the seller paying a link.
"""

import json
from datetime import datetime, timedelta, timezone

from . import alerts, config

PRICE_MONTHLY = 11.99
MONTHS = (1, 3, 12)
REMIND_DAYS = 5
BASESCAN = "https://basescan.org/tx/"


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
            "  - Within a couple of days you'll get a listing review: the listing check run across all your routes,",
            "    what to fix, and what to send Coinbase for your Agentic.Market card.",
            "  - Each month, an availability report per endpoint against Coinbase's 99% bar.",
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
        "" if renewal else "To do: the listing review (listing check across every route, what to fix, what to send Coinbase).",
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
