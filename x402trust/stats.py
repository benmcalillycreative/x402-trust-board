"""stats.json on the board: the numbers that say whether Settle Watch is getting used.

Paid checks and seller-plan payments come from the Worker's /v1/stats (aggregate, payers shortened);
weekly and monthly npm downloads of x402-trust-guard come from npm; active plans from plans.json.
A weekly summary reads the published file, so it never needs access to this computer.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config, plans

GUARD = "x402-trust-guard"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def collect(log=print):
    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        out["api"] = _get(f"{config.PUBLIC_API}/v1/stats")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["api"] = None
        log(f"Stats: couldn't read {config.PUBLIC_API}/v1/stats: {exc}")
    guard = {}
    for period in ("last-week", "last-month"):
        try:
            guard[period] = _get(f"https://api.npmjs.org/downloads/point/{period}/{GUARD}").get("downloads")
        except (urllib.error.URLError, OSError, ValueError):
            guard[period] = None
    out["guard_downloads"] = guard
    out["seller_plans_active"] = len(plans.active())
    return out


def write(log=print):
    data = collect(log=log)
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORT_DIR / "stats.json").write_text(json.dumps(data, indent=1))
    api = data.get("api") or {}
    w = api.get("last_7_days") or {}
    log(f"Stats: {w.get('checks', '?')} paid check(s) in 7 days, guard {data['guard_downloads'].get('last-week')} "
        f"download(s) last week, {data['seller_plans_active']} active plan(s)")
    return data
