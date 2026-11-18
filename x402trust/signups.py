"""Self-serve alert sign-ups (double opt-in), handled together with the Worker in worker/.

Someone fills in the form on a seller page; the Worker stores the request. Each cycle this
module pulls the queue and:
  - emails a confirmation link for new requests (from the same address as the alerts),
  - adds a watcher once the link has been clicked,
  - removes a watcher when someone used the one-click unsubscribe link.
Nothing is watched until the person who owns the address confirms it.
"""

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

from . import alerts, config


def ready():
    return bool(config.API_URL and config.ALERTS_TOKEN)


def sign(email, match):
    msg = f"{email.lower()}|{match}".encode()
    return hmac.new(config.ALERTS_TOKEN.encode(), msg, hashlib.sha256).hexdigest()[:32]


def stop_link(email, match):
    if not ready() or not email:
        return None
    q = urllib.parse.urlencode({"e": email.lower(), "m": match, "s": sign(email, match)})
    return f"{config.API_URL}/v1/alerts/stop?{q}"


def _call(path, payload=None):
    req = urllib.request.Request(
        f"{config.API_URL}/v1/alerts/{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {config.ALERTS_TOKEN}", "User-Agent": config.USER_AGENT,
                 "Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def confirm_body(email, host, link):
    return "\n".join([
        "Hi,",
        "",
        f"Someone (hopefully you) asked for x402 Trust Board alerts about {host} to go to {email}.",
        "",
        "To start them, open this link:",
        link,
        "",
        "You'll get one email when monitoring starts, then one only when something changes: an endpoint "
        "stops answering or comes back, or starts asking a different price or wallet.",
        "",
        "If this wasn't you, ignore this email and nothing happens.",
        "",
        "Ben",
    ])


def run(log=print):
    if not ready():
        return None
    try:
        items = _call("queue").get("items") or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"Sign-ups: couldn't reach {config.API_URL}: {exc}")
        return None
    mailed = added = stopped = leads = 0
    watching = {(w["email"].lower(), w["match"]) for w in alerts.load_watchers()}
    for it in items:
        key, kind, state = it.get("key"), it.get("kind"), it.get("state")
        email = (it.get("email") or "").strip().lower()
        try:
            if kind == "interest":
                owner = config.ALERT_FROM or config.SMTP_USER
                if owner:
                    body = (f"Someone asked about the seller plan.\n\nEmail: {email}\nService: {it.get('host') or '-'}\n"
                            f"When: {it.get('ts')}\n\n{it.get('note') or '(no note)'}\n")
                    if alerts.deliver(owner, f"Seller plan interest: {it.get('host') or email}", body, log=log) == "sent":
                        _call("ack", {"key": key, "state": "done"})
                        leads += 1
            elif kind == "stop":
                alerts.unwatch(email, it.get("match") or None)
                _call("ack", {"key": key, "state": "done"})
                stopped += 1
            elif kind == "signup" and state == "new":
                if (email, it.get("host")) in watching:
                    _call("ack", {"key": key, "state": "done"})
                    continue
                link = f"{config.API_URL}/v1/alerts/confirm?t={key.split(':', 1)[1]}"
                result = alerts.deliver(email, f"Confirm x402 Trust Board alerts for {it.get('host')}",
                                        confirm_body(email, it.get("host"), link), log=log)
                if result == "sent":
                    _call("ack", {"key": key, "state": "mailed"})
                    mailed += 1
            elif kind == "signup" and state == "confirmed":
                if (email, it.get("host")) not in watching:
                    alerts.watch(email, it.get("host"), "")
                    watching.add((email, it.get("host")))
                    added += 1
                _call("ack", {"key": key, "state": "done"})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"Sign-ups: {key}: {exc}")
    log(f"Sign-ups: {mailed} confirmation(s) sent, {added} watcher(s) added, {stopped} unsubscribe(s), "
        f"{leads} seller-plan lead(s) forwarded")
    return {"mailed": mailed, "added": added, "stopped": stopped, "leads": leads}
