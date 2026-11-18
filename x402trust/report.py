"""Write the leaderboard (HTML) and a machine-readable export (JSON)."""

import html
import json
from pathlib import Path
from collections import Counter, defaultdict

from . import config, db
from .score import FLAG_TEXT, RED_FLAGS, WARN_FLAGS, severity

TERMS_FLAGS = {"wallet_switched", "burn_address", "overcharge_10x", "overcharge_2x", "unlisted_wallet",
               "shared_wallet", "free_but_listed", "broken_terms", "private_address"}
PER_HOST_ROWS = 3


def load(conn):
    rows = conn.execute(
        """
        SELECT e.resource, e.host, e.service_name, e.description, e.listed_usd, e.network,
               e.pay_to, e.l30_payers, e.l30_calls, e.curated, e.first_seen, e.delisted, e.protocol,
               e.source, e.circle_seen,
               s.probes, s.uptime, s.p50_latency_ms, s.score, s.grade, s.flags, s.computed_at
        FROM scores s JOIN endpoints e USING (resource)
        ORDER BY s.score DESC, e.l30_payers DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = json.loads(d["flags"] or "[]")
        out.append(d)
    return out


def export_json(rows, path):
    payload = {
        "generated_at": db.now(),
        "window_days": config.SCORE_WINDOW_DAYS,
        "endpoints": [
            {
                "resource": r["resource"],
                "host": r["host"],
                "service": r["service_name"],
                "score": r["score"],
                "grade": r["grade"],
                "uptime": r["uptime"],
                "p50_latency_ms": r["p50_latency_ms"],
                "listed_usd": r["listed_usd"],
                "flags": r["flags"],
                "probes": r["probes"],
                "protocol": r["protocol"] or "x402",
                "listed_on": listed_on(r),
            }
            for r in rows
        ],
    }
    path.write_text(json.dumps(payload, separators=(",", ":")))


def listed_on(r):
    """Which catalogs list this endpoint: bazaar, circle, mpp."""
    if (r.get("protocol") or "x402") == "mpp":
        return ["mpp"]
    out = [] if r.get("source") == "circle" else ["bazaar"]
    if r.get("circle_seen"):
        out.append("circle")
    return out


def esc(v):
    return html.escape(str(v)) if v is not None else ""


def price(v):
    if v is None:
        return "—"
    return f"${v:,.4f}".rstrip("0").rstrip(".") if v < 1 else f"${v:,.2f}"


def host_summary(rows):
    by_host = defaultdict(lambda: {"listings": 0, "checked": 0, "up_sum": 0.0, "calls": 0})
    for r in rows:
        h = by_host[r["host"]]
        h["listings"] += 1
        h["calls"] += r["l30_calls"] or 0
        if r["probes"]:
            h["checked"] += 1
            h["up_sum"] += r["uptime"] or 0
    return by_host


ROTATING_LIMIT = 5  # more distinct pay-to addresses than this in the window = per-request addresses


SOURCE_URL = "https://github.com/benmcalillycreative/x402-trust-board/tree/main"


def engine_version():
    """Package version plus the git commit the board was built from, when there is one."""
    import subprocess
    from . import __version__
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        sha = ""
    return f"v{__version__}" + (f" ({sha})" if sha else "")


def website(host):
    """The seller's own site: the registrable domain of the endpoint's host
    (api.cleave.fi -> cleave.fi). None on shared hosting such as workers.dev or
    onrender.com, where the domain belongs to the platform, not the seller."""
    from .payout import dns_names
    names = dns_names(host or "")
    return names[-1] if names else None


def site_link(host):
    site = website(host)
    if not site:
        return ""
    return (f' <a class="site" href="https://{esc(site)}" target="_blank" rel="noopener nofollow" '
            f'title="The seller\'s website">{esc(site)} \u2197</a>')


def all_summaries(conn):
    """Fully counted payout addresses on Base (lowercase 0x) and Solana (base58, case kept)."""
    from . import chain, chain_sol
    sums = dict(chain.summaries(conn))
    if config.SOL_ENABLED:
        try:
            sums.update(chain_sol.summaries(conn))
        except Exception:
            pass
    return sums


def onchain_by_host(conn):
    """Real USDC received on Base and Solana per service, next to Coinbase's own call counts."""
    from datetime import datetime, timedelta, timezone
    from . import chain, chain_sol
    sums = all_summaries(conn)
    if not sums:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=config.SCORE_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    addrs = defaultdict(set)
    for host, a, net in conn.execute(
            "SELECT e.host, p.quoted_pay_to, p.quoted_network FROM probes p JOIN endpoints e USING (resource) "
            "WHERE p.ts >= ? AND p.quoted_network IN (?, ?) AND p.quoted_pay_to IS NOT NULL",
            (since, chain.CHAIN, chain_sol.CHAIN)):
        addrs[host].add(a.lower() if net == chain.CHAIN else a.strip())
    calls = {h: c or 0 for h, c in conn.execute(
        "SELECT host, SUM(l30_calls) FROM endpoints WHERE delisted = 0 GROUP BY host")}
    out = []
    for host, aset in addrs.items():
        # A new address for every request (per-quote deposit addresses) can't be counted this way:
        # each one is paid at most once, so the totals would say "no customers". Skip those.
        if len(aset) > ROTATING_LIMIT:
            continue
        # Show a service only when every address it asks to be paid to is fully counted;
        # otherwise the numbers would cover part of its money and look like an accusation.
        if not all(a in sums for a in aset):
            continue
        got = [sums[a] for a in aset]
        payments = sum(g["payments"] for g in got)
        own = sum(g["payments"] * g["own_share"] for g in got)
        out.append({"host": host, "payments": payments, "payers": sum(g["payers"] for g in got),
                    "usd": round(sum(g["usd"] for g in got), 2), "own_share": own / payments if payments else 0,
                    "catalog_calls": calls.get(host, 0), "addresses": sorted(aset)})
    return sorted(out, key=lambda r: -r["usd"])


def onchain_section(hosts, total_usd, own_usd_share):
    if not hosts:
        return ""
    def row(h):
        own = f"{round(100 * h['own_share'])}%" if h["payments"] else "—"
        return (f'<tr><td>{esc(h["host"])}</td><td class="num">{h["payments"]:,}</td>'
                f'<td class="num">{h["payers"]:,}</td><td class="num">${h["usd"]:,.2f}</td>'
                f'<td class="num">{own}</td><td class="num">{h["catalog_calls"]:,}</td></tr>')
    return f"""<h2 id="onchain">Real payments on Base and Solana</h2>
<p class="lede">USDC that actually reached each service's payment address in the last {config.CHAIN_WINDOW_DAYS} days,
read from the blockchain, next to the call count Coinbase's catalog reports. "From own wallets" is money sent from
addresses the same service asks to be paid to: the seller paying itself. ${total_usd:,.0f} in total
across all tracked addresses, {round(100 * own_usd_share)}% of it from sellers' own wallets.</p>
<div class="wrap"><table><thead><tr><th>Host</th><th class="num">Payments</th><th class="num">Paying wallets</th>
<th class="num">USDC received</th><th class="num">From own wallets</th><th class="num">Catalog calls (30d)</th></tr></thead>
<tbody>{"".join(row(h) for h in hosts[:15])}</tbody></table></div>"""


def build_html(rows, crawl, onchain=None):
    total = len(rows)
    hosts = host_summary(rows)
    n_hosts = len(hosts)
    biggest = sorted(hosts.items(), key=lambda kv: -kv[1]["listings"])
    big_host, big = biggest[0] if biggest else ("", {"listings": 0})
    big_share = round(100 * big["listings"] / total) if total else 0
    checked = [r for r in rows if r["probes"] and "retired_by_owner" not in r["flags"]]
    answered_pct = round(100 * sum(1 for r in checked if r["uptime"] > 0) / len(checked)) if checked else 0
    graded = [r for r in rows if r["grade"] != "—"]
    grades = Counter(r["grade"] for r in graded)
    red = [r for r in rows if set(r["flags"]) & RED_FLAGS]
    n_mpp = sum(1 for r in rows if r["protocol"] == "mpp")
    state_link = (' Monthly: <a href="state/index.html">State of agent payments</a>.'
                  if (config.REPORT_DIR / "state" / "index.html").exists() else "")
    n_circle = sum(1 for r in rows if r.get("source") == "circle")
    circle_stat = (f'<div class="stat"><b>{n_circle:,}</b><span>only on Circle\'s marketplace</span></div>'
                   if n_circle else "")
    onchain = onchain or {"hosts": [], "total_usd": 0, "own_share": 0}
    onchain_html = onchain_section(onchain["hosts"], onchain["total_usd"], onchain["own_share"])
    n_verified = len({r["host"] for r in rows if set(r["flags"]) & {"payout_verified", "payout_verified_dns"}})
    terms = [r for r in rows if set(r["flags"]) & TERMS_FLAGS]
    total_calls = sum(h["calls"] for h in hosts.values()) or 1
    by_calls = sorted(hosts.items(), key=lambda kv: -kv[1]["calls"])
    top10_calls = round(100 * sum(h["calls"] for _, h in by_calls[:10]) / total_calls)

    # The page can't hold 90K+ rows. Show graded endpoints, at most PER_HOST_ROWS per
    # host so one big host can't fill the board, plus everything with a problem in its
    # payment terms. scores.json carries everything.
    cap = config.REPORT_MAX_ROWS
    per_host = Counter()
    top = []
    for r in graded:
        if per_host[r["host"]] < PER_HOST_ROWS and not (set(r["flags"]) & TERMS_FLAGS):
            per_host[r["host"]] += 1
            top.append(r)
            if len(top) >= cap:
                break
    shown_ids = {r["resource"] for r in top}
    # Worst problems first, so the cap only ever trims the mildest ones. Red flags are
    # never trimmed: the headline counts them, so the table must show every one.
    def badness(r):
        f = set(r["flags"])
        return (0 if f & RED_FLAGS else 1 if f & WARN_FLAGS else 2, r["score"] or 0)
    flagged = sorted((r for r in terms if r["resource"] not in shown_ids), key=badness)
    n_red = sum(1 for r in flagged if set(r["flags"]) & RED_FLAGS)
    flagged = flagged[:max(cap, n_red)]
    shown = top + flagged
    shown.sort(key=lambda r: (-(r["score"] or 0), -(r["l30_payers"] or 0)))

    contact = config.CONTACT.strip()
    if contact:
        link = contact if contact.startswith("http") else f"mailto:{contact}"
        contact_html = (f'<p><b>Is this your endpoint?</b> If something here is wrong, or you want an '
                        f'alert the moment your endpoint breaks or drifts from its listing, '
                        f'<a href="{esc(link)}">get in touch</a>. Corrections are welcome and fixed fast.</p>')
    else:
        contact_html = ""

    def host_rows(items, kind):
        out = []
        for host, h in items:
            up = f"{round(100 * h['up_sum'] / h['checked'])}%" if h["checked"] else "—"
            if kind == "listings":
                share = f"{100 * h['listings'] / total:.1f}%"
            else:
                share = f"{100 * h['calls'] / total_calls:.1f}%"
            out.append(f"<tr><td class=ep><div class=url>{esc(host)}</div></td>"
                       f"<td class=num>{h['listings']:,}</td><td class=num>{share}</td>"
                       f"<td class=num>{h['calls']:,}</td><td class=num>{up}</td></tr>")
        return "".join(out)

    def row_html(r):
        flags = "".join(
            f'<span class="flag {severity(f)}" title="{esc(FLAG_TEXT.get(f, f))}">'
            f"{esc(FLAG_TEXT.get(f, f))}</span>"
            for f in r["flags"]
        )
        name = r["service_name"] or r["host"] or ""
        uptime = "—" if not r["probes"] else f"{round(100 * r['uptime'])}%"
        latency = "—" if r["p50_latency_ms"] is None else f"{r['p50_latency_ms']:,} ms"
        grade_cls = "g" + ("x" if r["grade"] == "—" else r["grade"])
        return (
            f'<tr data-proto="{esc(r["protocol"] or "x402")}" '
            f'data-search="{esc((name + " " + r["resource"] + " " + (r["description"] or "")).lower())}">'
            f'<td><span class="grade {grade_cls}">{esc(r["grade"])}</span></td>'
            f'<td class="num" data-v="{r["score"]}">{r["score"]}</td>'
            f'<td class="ep"><div class="name"><a class="seller" href="seller/{esc(r["host"])}.html">{esc(name)}</a>{site_link(r["host"])}{" <span class=cur>curated</span>" if r["curated"] else ""}{" <span class=proto>MPP</span>" if r["protocol"] == "mpp" else ""}{" <span class=proto>Circle</span>" if r.get("source") == "circle" else ""}</div>'
            f'<div class="url">{esc(r["resource"])}</div>'
            f'<div class="flags">{flags}</div></td>'
            f'<td class="num" data-v="{r["uptime"] or 0}">{uptime}</td>'
            f'<td class="num" data-v="{r["p50_latency_ms"] or 999999}">{latency}</td>'
            f'<td class="num" data-v="{r["listed_usd"] if r["listed_usd"] is not None else -1}">{price(r["listed_usd"])}</td>'
            f'<td class="num" data-v="{r["l30_payers"] or 0}">{r["l30_payers"] if r["l30_payers"] is not None else "—"}</td>'
            "</tr>"
        )

    red_section = ""
    if red:
        red_rows = sorted(red, key=lambda r: (r["score"] or 0, -(r["l30_payers"] or 0)))
        red_section = (
            f'<h2 id="red">Red flags</h2>\n'
            f'<p class="lede">{len(red):,} endpoint{"s" if len(red) != 1 else ""} an agent would regret '
            f'paying: asking far more than the listed price, payment to an address nobody can spend from, '
            f'or a wallet that changed to one no listing mentions.</p>\n'
            f'<div class="wrap"><table id="rt">\n'
            f'<thead><tr><th>Grade</th><th class="num">Score</th><th>Endpoint</th><th class="num">Uptime</th>\n'
            f'<th class="num">Speed</th><th class="num">Price</th><th class="num">Payers (30d)</th></tr></thead>\n'
            f'<tbody>{"".join(row_html(r) for r in red_rows)}</tbody></table></div>'
        )

    crawl_line = ""
    if crawl:
        crawl_line = (
            f"Last crawl {esc(crawl['ts'])} · {crawl['fetched']:,} seen in it · "
            f"{crawl['new_endpoints']:,} new · {crawl['delisted']:,} dropped"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>x402 Trust Board</title>
<style>
:root {{
  --bg:#f7f6f2; --panel:#ffffff; --ink:#1b1b18; --muted:#6b6a63; --line:#e4e2da;
  --accent:#1f5f4a; --red:#b3261e; --redbg:#fbe9e7; --chip:#efede6; --warn:#8a5a00; --warnbg:#fbf0d9; --good:#1f6b45; --goodbg:#e3f3ea;
  --gA:#1f7a4d; --gB:#4f8a2b; --gC:#a07a12; --gD:#c05a17; --gF:#b3261e; --gx:#8a8a85;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#141412; --panel:#1c1c19; --ink:#ecebe5; --muted:#9d9b92; --line:#2e2d29;
  --accent:#6cc2a1; --red:#ff8a80; --redbg:#3a1c1a; --chip:#262622; --warn:#f0c46a; --warnbg:#3a2f17; --good:#7fd6a6; --goodbg:#16301f;
}} }}
:root[data-theme="dark"] {{
  --bg:#141412; --panel:#1c1c19; --ink:#ecebe5; --muted:#9d9b92; --line:#2e2d29;
  --accent:#6cc2a1; --red:#ff8a80; --redbg:#3a1c1a; --chip:#262622; --warn:#f0c46a; --warnbg:#3a2f17; --good:#7fd6a6; --goodbg:#16301f;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:28px 16px 60px; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.01em; }}
.sub {{ color:var(--muted); margin:0 0 22px; font-size:14px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:22px; }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
.stat b {{ display:block; font-size:24px; font-variant-numeric:tabular-nums; }}
.stat span {{ color:var(--muted); font-size:13px; }}
.stat.red b {{ color:var(--red); }}
a.stat {{ color:inherit; text-decoration:none; }}
#rt th {{ cursor:default; }}
.tools {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px; }}
input[type=search] {{ flex:1; min-width:200px; padding:9px 12px; border-radius:8px;
  border:1px solid var(--line); background:var(--panel); color:var(--ink); font:inherit; }}
label.chk {{ display:flex; align-items:center; gap:6px; color:var(--muted); font-size:14px; }}
.wrap {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; min-width:720px; }}
th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; text-align:left; }}
th {{ font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
  cursor:pointer; user-select:none; position:sticky; top:0; background:var(--panel); }}
th.num, td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
tr:last-child td {{ border-bottom:0; }}
.grade {{ display:inline-grid; place-items:center; width:30px; height:30px; border-radius:7px;
  color:#fff; font-weight:700; }}
.gA{{background:var(--gA)}} .gB{{background:var(--gB)}} .gC{{background:var(--gC)}}
.gD{{background:var(--gD)}} .gF{{background:var(--gF)}} .gx{{background:var(--gx)}}
.ep .name {{ font-weight:600; }}
.ep a.seller {{ color:inherit; text-decoration:none; }}
.ep a.seller:hover {{ text-decoration:underline; }}
.ep a.site {{ font-size:12px; font-weight:400; color:var(--muted); text-decoration:none; margin-left:6px; }}
.ep a.site:hover {{ text-decoration:underline; }}
.ep .url {{ color:var(--muted); font-size:12.5px; word-break:break-all; }}
.proto {{ font-size:11px; font-weight:600; color:var(--muted); border:1px solid var(--line);
  border-radius:4px; padding:0 4px; margin-left:4px; }}
select {{ padding:9px 10px; border-radius:8px; border:1px solid var(--line); background:var(--panel);
  color:var(--ink); font:inherit; }}
.cur {{ font-size:11px; font-weight:500; color:var(--accent); border:1px solid var(--accent);
  border-radius:4px; padding:0 4px; margin-left:4px; }}
.flags {{ margin-top:4px; display:flex; flex-wrap:wrap; gap:4px; }}
.flag {{ font-size:11.5px; background:var(--chip); color:var(--muted); border-radius:4px; padding:1px 6px; }}
.flag.red {{ background:var(--redbg); color:var(--red); }}
.flag.warn {{ background:var(--warnbg); color:var(--warn); }}
.flag.good {{ background:var(--goodbg); color:var(--good); }}
h2 {{ font-size:17px; margin:30px 0 4px; }}
.lede {{ color:var(--muted); font-size:14px; margin:0 0 10px; }}
.twocol {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:14px; }}
.twocol table {{ min-width:500px; }}
.twocol .url {{ word-break:normal; white-space:nowrap; }}
.twocol th {{ position:static; cursor:default; }}
.note {{ color:var(--muted); font-size:13px; margin-top:16px; }}
.about {{ margin-top:34px; padding-top:6px; border-top:1px solid var(--line); color:var(--ink); font-size:14.5px; max-width:760px; }}
.about p {{ margin:0 0 12px; }}
.about code {{ font-size:13px; background:var(--chip); padding:1px 4px; border-radius:4px; }}
.about a {{ color:var(--accent); }}
</style></head>
<body><main>
<h1>x402 Trust Board</h1>
<p class="sub">Independent checks of the x402 Bazaar, Circle's Agent Marketplace and the MPP service directory. Probed without paying. {crawl_line}</p>
<div class="stats">
  <div class="stat"><b>{total:,}</b><span>endpoints listed</span></div>
  <div class="stat"><b>{n_hosts:,}</b><span>distinct services (hosts)</span></div>
  <div class="stat"><b>{n_mpp:,}</b><span>from the MPP directory</span></div>
  {circle_stat}
  <div class="stat"><b>{big_share}%</b><span>of listings from one host</span></div>
  <div class="stat"><b>{answered_pct}%</b><span>of checked listings answered</span></div>
  <div class="stat"><b>{top10_calls}%</b><span>of paid calls go to 10 hosts</span></div>
  <a class="stat red" href="#red"><b>{len(red):,}</b><span>red flags</span></a>
  <div class="stat"><b>{len(terms):,}</b><span>with payment-term problems</span></div>
  <div class="stat"><b>{len(graded):,}</b><span>graded ({grades.get('A', 0):,} A)</span></div>
  <a class="stat" href="verify.html"><b>{n_verified:,}</b><span>with a verified payout address</span></a>
</div>
<div class="twocol">
<div><h2>Who's really in the catalog</h2>
<p class="lede">Hosts with the most listings. Uptime is from sampled checks.</p>
<div class="wrap"><table><thead><tr><th>Host</th><th class="num">Listings</th><th class="num">Share</th>
<th class="num">Calls (30d)</th><th class="num">Uptime</th></tr></thead>
<tbody>{host_rows(biggest[:10], "listings")}</tbody></table></div></div>
<div><h2>Where the paid traffic goes</h2>
<p class="lede">Hosts by calls in the last 30 days (Coinbase's own counts).</p>
<div class="wrap"><table><thead><tr><th>Host</th><th class="num">Listings</th><th class="num">Share</th>
<th class="num">Calls (30d)</th><th class="num">Uptime</th></tr></thead>
<tbody>{host_rows(by_calls[:10], "calls")}</tbody></table></div></div>
</div>
{onchain_html}
{red_section}
<h2>Endpoints</h2>
<p class="lede">Graded endpoints (at most {PER_HOST_ROWS} per host) and every endpoint whose payment terms don't match its listing.</p>
<div class="tools">
  <input type="search" id="q" placeholder="Filter by name, URL or description">
  <select id="proto" aria-label="Protocol"><option value="">x402 + MPP</option><option value="x402">x402 only</option><option value="mpp">MPP only</option></select>
  <label class="chk"><input type="checkbox" id="redonly"> Problems only</label>
</div>
<div class="wrap"><table id="t">
<thead><tr><th>Grade</th><th class="num">Score</th><th>Endpoint</th><th class="num">Uptime</th>
<th class="num">Speed</th><th class="num">Price</th><th class="num">Payers (30d)</th></tr></thead>
<tbody>
{''.join(row_html(r) for r in shown)}
</tbody></table></div>
<p class="note">Showing {len(shown):,} of {total:,}: {len(top):,} graded endpoints plus
{len(flagged):,} with payment-term problems. Every endpoint is in scores.json.
Scores cover the last {config.SCORE_WINDOW_DAYS} days; an endpoint needs {config.MIN_PROBES_FOR_GRADE}
checks before it gets a grade. Big hosts are sampled, so most of their listings stay ungraded.
Red chips are problems an agent would regret paying into; amber chips are warnings; grey chips
are information only. Price shown only for dollar-denominated listings. Click a column header to sort.</p>
<section class="about">
<h2>How this works</h2>
<p>Every listing in Coinbase's public x402 Bazaar catalog, Circle's Agent Marketplace and the MPP
(Machine Payments Protocol) service directory is checked on a rotating schedule,
about twice a day for most services. Each check asks for the resource <b>without paying</b> and
records whether the server answered, how fast, and whether the payment terms it quotes match the
listing: the same network, a wallet the listing (or the same service) names, and a price within
2× of what's listed. No wallet is involved and nothing is ever paid.</p>
<p>Scores weigh uptime (50%), speed (15%), whether the payment terms match the listing (20%), and
paying customers in the last 30 days (15%, from the catalog; the MPP directory and Circle's marketplace don't
publish usage, so for their listings that part is left out rather than counted as zero). Grades need at least
{config.MIN_PROBES_FOR_GRADE} checks in the last {config.SCORE_WINDOW_DAYS} days. Hosts with
thousands of listings are sampled, so most of their listings stay ungraded. Paid-call counts and
payer counts are Coinbase's own figures from the catalog.</p>
<p>What doesn't count against anyone: fresh payment addresses per request, paying into another
wallet the same service lists, prices that vary by request (up to 2×), asking for input before
quoting a price, route-pattern listings like <code>/price/:address</code>, rate limits, and TLS
problems on our side.</p>
<p><b>Run an endpoint?</b> Prove your payout address is yours, free: publish it on your site or DNS and
sign it with the wallet. Verified endpoints get a green mark, and if anything ever asks to be paid
somewhere else, the board flags it. <a href="verify.html">Verify your payout address</a>.</p>
<p>Every seller has its own page with all of its endpoints, its payout proofs and a badge for its own site:
<a href="sellers.html">browse sellers</a>.{state_link}</p>
{contact_html}
<p class="engine">Built by engine {engine_version()}. <a href="{SOURCE_URL}">Read the code</a> that grades every endpoint.</p>
</section>
</main>
<script>
const q = document.getElementById('q'), red = document.getElementById('redonly'), proto = document.getElementById('proto');
const rows = [...document.querySelectorAll('#t tbody tr')];
function filter() {{
  const term = q.value.trim().toLowerCase();
  rows.forEach(r => {{
    const okTerm = !term || r.dataset.search.includes(term);
    const okRed = !red.checked || r.querySelector('.flag.red, .flag.warn');
    const okProto = !proto.value || r.dataset.proto === proto.value;
    r.style.display = okTerm && okRed && okProto ? '' : 'none';
  }});
}}
q.addEventListener('input', filter); red.addEventListener('change', filter); proto.addEventListener('change', filter);
document.querySelectorAll('#t th').forEach((th, i) => {{
  let asc = false;
  th.addEventListener('click', () => {{
    asc = !asc;
    const tb = document.querySelector('#t tbody');
    const val = r => {{
      const c = r.children[i];
      return c.dataset.v !== undefined ? parseFloat(c.dataset.v) : c.textContent.trim().toLowerCase();
    }};
    rows.sort((a, b) => {{ const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1); }});
    rows.forEach(r => tb.appendChild(r));
  }});
}});
</script>
</body></html>"""


def run(conn=None, log=print):
    conn = conn or db.connect()
    rows = load(conn)
    crawl = conn.execute("SELECT * FROM crawls ORDER BY ts DESC LIMIT 1").fetchone()
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = config.REPORT_DIR / "leaderboard.html"
    json_path = config.REPORT_DIR / "scores.json"
    try:
        hosts = onchain_by_host(conn)
        sums = all_summaries(conn)
        total = sum(v["usd"] for v in sums.values())
        own_usd = sum(v["usd"] * v["own_share"] for v in sums.values())  # approximation by payment share
        onchain = {"hosts": hosts, "total_usd": total, "own_share": own_usd / total if total else 0}
    except Exception as exc:
        log(f"  (on-chain section skipped: {exc})")
        onchain = None
    html_path.write_text(build_html(rows, dict(crawl) if crawl else None, onchain))
    export_json(rows, json_path)
    if onchain:
        (config.REPORT_DIR / "onchain.json").write_text(json.dumps(
            {"generated_at": db.now(), "window_days": config.CHAIN_WINDOW_DAYS, "chain": "eip155:8453",
             "chains": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"],
             "asset": "USDC", "total_usd": round(onchain["total_usd"], 2), "hosts": onchain["hosts"]},
            separators=(",", ":")))
    try:
        from . import pages
        pages.build(conn, rows, config.REPORT_DIR, onchain["hosts"] if onchain else None, log=log)
    except Exception as exc:
        log(f"  (seller pages skipped: {exc})")
    try:
        (config.REPORT_DIR / "payouts.json").write_text(json.dumps(payouts_export(conn), separators=(",", ":")))
    except Exception as exc:
        log(f"  (payouts.json skipped: {exc})")
    log(f"Report written: {html_path}")
    return html_path


def payouts_export(conn):
    """Every payout proof the board has found, with when it was last checked and when an
    endpoint on that host last asked to be paid to each proven address. Meant for buyers'
    clients: check freshness yourself, or better, verify the proof at call time (see how_to_verify)."""
    from . import payout
    conn.executescript(payout.SCHEMA)
    checked = {h: t for h, t in conn.execute("SELECT host, checked_at FROM payout_checks")}
    last_asked, original = {}, {}
    for host, addr, ts, raw in conn.execute(
            "SELECT e.host, lower(p.quoted_pay_to), MAX(p.ts), p.quoted_pay_to FROM probes p JOIN endpoints e USING (resource) "
            "WHERE p.quoted_pay_to IS NOT NULL GROUP BY e.host, lower(p.quoted_pay_to)"):
        last_asked[(host, addr)] = ts
        original[addr] = raw
    # Proofs are stored lowercased for comparison. That's harmless for 0x addresses but breaks
    # base58 (Solana) ones, so give those back in the case the seller's own 402 uses.
    fix_case = lambda a: a if a.startswith("0x") else original.get(a.lower(), a)
    hosts = {}
    for host, source, domain, address, issued, signer in conn.execute(
            "SELECT host, source, domain, address, issued, signer FROM payout_proofs ORDER BY host"):
        h = hosts.setdefault(host, {"host": host, "proofs_checked_at": checked.get(host), "proofs": []})
        h["proofs"].append({
            "source": source, "domain": domain, "address": fix_case(address), "issued": issued,
            "signature": signer,  # wallet | contract | unsigned | invalid | unverifiable
            "last_seen_asking": last_asked.get((host, address.lower())),
        })
    return {
        "generated_at": db.now(),
        "how_to_verify": {
            "dns": f"TXT record at {payout.DNS_LABEL}<domain>",
            "site": f"https://<domain>{payout.WELL_KNOWN}",
            "message": payout.MESSAGE,
            "note": "Each proof is an EIP-191 personal_sign (or ERC-1271 for smart wallets) of the message above. "
                    "A client can check the payTo in a live 402 against the seller's own DNS record at call "
                    "time, without trusting this board. The DNS method doesn't depend on the API server, so a "
                    "compromised server alone can't forge it.",
            "checks": "The board re-reads proofs about once a day (DNS over HTTPS, so a changed record can lag by "
                      "its TTL) and checks each endpoint's 402 about twice a day.",
        },
        "hosts": list(hosts.values()),
    }
