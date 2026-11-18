"""State of agent payments: a monthly snapshot of the whole market, from the board's own data.

    python3 -m x402trust monthly            # last 30 days, labelled with this month
    python3 -m x402trust monthly 2026-11    # label explicitly

Writes reports/state/<YYYY-MM>.json (numbers), .md (a draft to edit and post) and .html (published
with the board). Each issue compares itself with the previous one when there is one.
"""

import html
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db, report
from .score import FLAG_TEXT, RED_FLAGS, severity

BOARD = "https://benmcalillycreative.github.io/x402-trust-board/"


def _pct(n, d):
    return round(100 * n / d, 1) if d else 0.0


def collect(conn, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = report.load(conn)
    listed = [r for r in rows if not r["delisted"]]
    eps = conn.execute(
        "SELECT host, COALESCE(protocol,'x402') proto, COALESCE(source,'bazaar') src, first_seen, "
        "l30_calls, l30_payers FROM endpoints WHERE delisted=0").fetchall()
    by_catalog = Counter("mpp" if e["proto"] == "mpp" else e["src"] for e in eps)
    hosts = Counter(e["host"] for e in eps)
    first_seen_host = {}
    for h, fs in conn.execute("SELECT host, MIN(first_seen) FROM endpoints GROUP BY host"):
        first_seen_host[h] = fs
    new_hosts = sorted(h for h, fs in first_seen_host.items() if fs and fs >= since and h in hosts)
    calls = Counter()
    for e in eps:
        calls[e["host"]] += e["l30_calls"] or 0
    total_calls = sum(calls.values())

    pr = conn.execute(
        "SELECT outcome, latency_ms FROM probes WHERE ts >= ?", (since,)).fetchall()
    answered = [p for p in pr if p["outcome"] in ("healthy", "free")]
    lat = [p["latency_ms"] for p in pr if p["outcome"] == "healthy" and p["latency_ms"]]

    graded = [r for r in listed if r["grade"] not in (None, "—")]
    grade_hosts = defaultdict(list)
    for r in graded:
        grade_hosts[r["host"]].append(r["score"])
    flags = Counter(f for r in listed for f in r["flags"])
    red_hosts = {r["host"] for r in listed if set(r["flags"]) & RED_FLAGS}

    levels = Counter()
    try:
        from . import payout
        for _host, m in payout.load(conn).items():
            best = max(m.values(), key=lambda lv: payout.LEVELS.get(lv, -1))
            levels[best] += 1
    except Exception:
        pass

    try:
        from . import chain
        chain_hosts = report.onchain_by_host(conn)
        sums = chain.summaries(conn)
    except Exception:
        chain_hosts, sums = [], {}
    # Several hosts can share one wallet (a custom domain and its platform URL, say). Count each
    # address once for the totals, and show hosts with the same wallets as one row.
    addrs = {a for h in chain_hosts for a in h.get("addresses", [])}
    usd = sum(sums[a]["usd"] for a in addrs if a in sums)
    payments = sum(sums[a]["payments"] for a in addrs if a in sums)
    self_paid = sum(sums[a]["payments"] * sums[a]["own_share"] for a in addrs if a in sums)
    groups = {}
    for h in chain_hosts:
        key = tuple(h.get("addresses") or [h["host"]])
        g = groups.setdefault(key, {"hosts": [], "usd": h["usd"], "payments": h["payments"],
                                    "payers": h["payers"], "catalog_calls": 0})
        g["hosts"].append(h["host"])
        g["catalog_calls"] += h["catalog_calls"]
    services = sorted(groups.values(), key=lambda g: -g["usd"])
    cat_calls_counted = sum(g["catalog_calls"] for g in services)

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"from": since, "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": 30},
        "catalog": {
            "endpoints": len(eps),
            "by_catalog": dict(by_catalog),
            "hosts": len(hosts),
            "new_hosts": len(new_hosts),
            "new_host_names": new_hosts[:50],
            "top_host_share_pct": _pct(hosts.most_common(1)[0][1], len(eps)) if hosts else 0,
            "top_host": hosts.most_common(1)[0][0] if hosts else None,
        },
        "usage_reported": {
            "calls_30d": total_calls,
            "top10_share_pct": _pct(sum(c for _, c in calls.most_common(10)), total_calls),
            "hosts_with_calls": sum(1 for c in calls.values() if c),
        },
        "health": {
            "checks": len(pr),
            "answered_pct": _pct(len(answered), len(pr)),
            "p50_latency_ms": int(statistics.median(lat)) if lat else None,
        },
        "grades": {
            "endpoints_graded": len(graded),
            "endpoint_grades": dict(Counter(r["grade"] for r in graded)),
            "hosts_graded": len(grade_hosts),
            "hosts_a_or_b": sum(1 for s in grade_hosts.values() if sum(s) / len(s) >= 70),
        },
        "flags": {
            "red_endpoints": sum(1 for r in listed if set(r["flags"]) & RED_FLAGS),
            "red_hosts": len(red_hosts),
            "top": [{"flag": f, "text": FLAG_TEXT.get(f, f), "endpoints": n}
                    for f, n in flags.most_common() if severity(f) in ("red", "warn")][:12],
        },
        "payout_proofs": {"hosts": sum(levels.values()), "by_level": dict(levels)},
        "onchain": {
            "hosts_counted": len(chain_hosts),
            "services_counted": len(services),
            "usdc": round(usd, 2),
            "payments": payments,
            "self_paid_pct": _pct(self_paid, payments),
            "catalog_calls_for_same_hosts": cat_calls_counted,
            "top": [{"host": " + ".join(sorted(g["hosts"])[:3]) + (" + more" if len(g["hosts"]) > 3 else ""),
                     "usd": g["usd"], "payments": g["payments"], "payers": g["payers"]} for g in services[:10]],
        },
    }


def _delta(cur, prev, path, unit=""):
    def get(d):
        for k in path:
            d = (d or {}).get(k)
        return d
    a, b = get(cur), get(prev)
    if prev is None or a is None or b is None or not isinstance(a, (int, float)):
        return ""
    diff = a - b
    if not diff:
        return " (no change)"
    sign = "+" if diff > 0 else "−"
    val = f"{abs(diff):,.1f}" if isinstance(diff, float) else f"{abs(diff):,}"
    return f" ({sign}{val}{unit} vs last month)"


def markdown(s, label, prev=None):
    c, u, h, g, f, p, o = (s["catalog"], s["usage_reported"], s["health"], s["grades"], s["flags"],
                           s["payout_proofs"], s["onchain"])
    cat = ", ".join(f"{n:,} {name}" for key, name in (("bazaar", "in Coinbase's x402 Bazaar"),
                                                        ("circle", "only in Circle's marketplace"),
                                                        ("mpp", "in the MPP directory"))
                    for n in [c["by_catalog"].get(key, 0)] if n)
    lines = [
        f"# State of agent payments: {label}",
        "",
        f"*x402 Trust Board, data for the 30 days to {s['window']['to'][:10]}. Every listed endpoint is "
        f"checked several times a day without paying; payments are counted on Base.*",
        "",
        "## The market in numbers",
        "",
        f"- **{c['endpoints']:,} paid endpoints listed** ({cat}){_delta(s, prev, ['catalog', 'endpoints'])}, "
        f"from **{c['hosts']:,} services**{_delta(s, prev, ['catalog', 'hosts'])}. "
        f"{c['new_hosts']:,} services are new this month.",
        f"- **{c['top_host_share_pct']}% of all listings come from one service** ({c['top_host']}).",
        f"- Coinbase reports **{u['calls_30d']:,} paid calls** in 30 days across {u['hosts_with_calls']:,} services; "
        f"**{u['top10_share_pct']}% of them went to 10 services**.",
        f"- **{h['answered_pct']}% of {h['checks']:,} checks got an answer**{_delta(s, prev, ['health', 'answered_pct'], ' pts')}"
        + (f"; the median paid endpoint answered in {h['p50_latency_ms']:,} ms." if h["p50_latency_ms"] else "."),
        "",
        "## Can an agent trust what it pays?",
        "",
        f"- {g['endpoints_graded']:,} endpoints have enough history for a grade. Of {g['hosts_graded']:,} graded "
        f"services, **{g['hosts_a_or_b']:,} average A or B**.",
        f"- **{f['red_endpoints']:,} endpoints at {f['red_hosts']:,} services carry a red flag**"
        f"{_delta(s, prev, ['flags', 'red_endpoints'])}: a switched wallet, a burn address, a 10× overcharge, "
        "or an address that contradicts the seller's own payout proof.",
        f"- **{p['hosts']:,} services have published a payout proof**{_delta(s, prev, ['payout_proofs', 'hosts'])}"
        + (f" ({p['by_level'].get('dns_signed', 0)} DNS + signature, {p['by_level'].get('site_signed', 0)} file + signature)"
           if p["by_level"] else "") + ".",
        "",
        "Most common problems:",
        "",
    ]
    lines += [f"- {t['text']}: {t['endpoints']:,} endpoint{'s' if t['endpoints'] != 1 else ''}" for t in f["top"][:8]]
    lines += ["", "## Real money on Base", ""]
    if o["hosts_counted"]:
        lines += [
            f"For the {o.get('services_counted', o['hosts_counted']):,} services whose payout wallets the board has fully counted: "
            f"**${o['usdc']:,.2f} USDC in {o['payments']:,} payments**{_delta(s, prev, ['onchain', 'usdc'])}. "
            f"**{o['self_paid_pct']}% of those payments came from the seller's own wallets.** "
            f"For the same services the catalog reports {o['catalog_calls_for_same_hosts']:,} paid calls.",
            "",
            "| Service | USDC | Payments | Payers |",
            "|---|---:|---:|---:|",
        ]
        lines += [f"| {t['host']} | ${t['usd']:,.2f} | {t['payments']:,} | {t['payers']:,} |" for t in o["top"]]
    else:
        lines += ["The on-chain count is still catching up; numbers will appear next month."]
    lines += [
        "",
        "## How this is measured",
        "",
        f"Listings come from Coinbase's x402 Bazaar, Circle's Agent Marketplace and the MPP directory. Each "
        "endpoint is asked for its price without paying, and the answer is compared with its listing. Grades "
        "weigh uptime, speed, whether the terms match, and paying customers. On-chain figures are USDC "
        "transfers on Base to the addresses endpoints ask to be paid, for services whose every address has been "
        f"scanned. Full method and data: {BOARD}",
        "",
    ]
    return "\n".join(lines)


def _html(md_text, label):
    """A tiny Markdown subset (headings, bullets, bold, italics, tables, links) to a styled page."""
    import re
    out, table = [], []

    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"\*(.+?)\*", r"<i>\1</i>", t)
        return re.sub(r"(https://[^\s<]+)", r'<a href="\1">\1</a>', t)

    def flush():
        if table:
            head, *body = [r for r in table if not set(r.replace("|", "").strip()) <= set("-:")]
            cells = lambda r: [x.strip() for x in r.strip("|").split("|")]
            out.append("<div class=wrap><table><thead><tr>" + "".join(f"<th>{inline(x)}</th>" for x in cells(head))
                       + "</tr></thead><tbody>" + "".join(
                           "<tr>" + "".join(f"<td>{inline(x)}</td>" for x in cells(r)) + "</tr>" for r in body)
                       + "</tbody></table></div>")
            table.clear()

    in_list = False
    for line in md_text.splitlines():
        if line.startswith("|"):
            table.append(line)
            continue
        flush()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    flush()
    if in_list:
        out.append("</ul>")
    from .pages import page
    body = f'<p class="muted"><a href="../index.html">x402 Trust Board</a> / State of agent payments</p>' + "".join(out)
    return page(f"State of agent payments: {label}", body,
                f"Monthly snapshot of the agent-payments market (x402 and MPP): {label}.")


def run(conn=None, label=None, log=print):
    conn = conn or db.connect()
    now = datetime.now(timezone.utc)
    label_key = label or now.strftime("%Y-%m")
    pretty = datetime.strptime(label_key, "%Y-%m").strftime("%B %Y")
    outdir = config.REPORT_DIR / "state"
    outdir.mkdir(parents=True, exist_ok=True)
    stats = collect(conn, now)
    prev = None
    earlier = sorted(p for p in outdir.glob("*.json") if p.stem < label_key)
    if earlier:
        try:
            prev = json.loads(earlier[-1].read_text())
        except ValueError:
            prev = None
    md = markdown(stats, pretty, prev)
    (outdir / f"{label_key}.json").write_text(json.dumps(stats, indent=1))
    (outdir / f"{label_key}.md").write_text(md)
    (outdir / f"{label_key}.html").write_text(_html(md, pretty))
    issues = sorted(p.stem for p in outdir.glob("*.json"))
    links = "".join(f'<li><a href="{k}.html">{datetime.strptime(k, "%Y-%m").strftime("%B %Y")}</a></li>'
                    for k in reversed(issues))
    from .pages import page
    (outdir / "index.html").write_text(page(
        "State of agent payments: x402 Trust Board",
        f'<p class="muted"><a href="../index.html">x402 Trust Board</a></p><h1>State of agent payments</h1>'
        f'<p class="sub">A monthly snapshot of the agent-payments market.</p><div class="card"><ul>{links}</ul></div>',
        "Monthly snapshots of the agent-payments market."))
    log(f"State of agent payments ({pretty}) written to {outdir}/{label_key}.md")
    return outdir / f"{label_key}.md"


def auto(conn, log=print):
    """In the first days of a month, write last month's issue once."""
    now = datetime.now(timezone.utc)
    if now.day > 3:
        return None
    last = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if (config.REPORT_DIR / "state" / f"{last}.json").exists():
        return None
    return run(conn, label=last, log=log)
