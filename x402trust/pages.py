"""One page per seller (host) plus an embeddable badge.

seller/<host>.html   the host's grade, every listed endpoint, payout proofs, owner notes
badge/<host>.svg     a small badge sellers can put on their own site, linking to that page
sellers.html         an index of every seller page

Static files, written next to the board and published with it.
"""

import html
import json
import re
from collections import Counter, defaultdict

from . import config, db, notes, payout
from .score import FLAG_TEXT, grade_for, severity

BOARD = "https://settlewatch.com/"
GRADE_COLOR = {"A": "#1f7a4d", "B": "#4f8a2b", "C": "#a07a12", "D": "#c05a17", "F": "#b3261e"}
LEVEL_TEXT = {
    "dns_signed": "Verified: domain DNS + wallet signature",
    "site_signed": "Verified: file on the seller's site + wallet signature",
    "dns": "Declared in DNS (not signed)",
    "site": "Declared on the seller's site (not signed)",
    "manifest": "Listed in the seller's x402 manifest",
}
SAFE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,252}$")


def esc(v):
    return html.escape(str(v)) if v is not None else ""


def slug(host):
    h = (host or "").lower()
    return h if SAFE.match(h) else None


def note_host(match):
    m = (match or "").lower()
    if "://" in m:
        m = m.split("://", 1)[1]
    return m.split("/", 1)[0]


def host_grade(rows):
    """Average score of the host's graded endpoints, as a letter; None when nothing is graded."""
    scores = [r["score"] for r in rows if r["score"] is not None and r["grade"] not in (None, "—")]
    if not scores:
        return None, None
    avg = sum(scores) / len(scores)
    return grade_for(avg), round(avg)


def best_level(levels):
    order = ["dns_signed", "site_signed", "dns", "site", "manifest"]
    for lv in order:
        if lv in levels:
            return lv
    return None


def badge_svg(grade, verified):
    left = "x402 Trust"
    right = (grade or "—") + (" · verified" if verified else "")
    color = GRADE_COLOR.get(grade, "#8a8a85")
    lw, rw = 72, 18 + 7 * len(right)
    w = lw + rw
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" '
            f'aria-label="{esc(left)}: {esc(right)}"><title>{esc(left)}: {esc(right)}</title>'
            f'<rect width="{lw}" height="20" fill="#1b1b18" rx="3"/>'
            f'<rect x="{lw - 3}" width="{rw + 3}" height="20" fill="{color}" rx="3"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
            f'<text x="8" y="14">{esc(left)}</text><text x="{lw + 8}" y="14">{esc(right)}</text></g></svg>')


PAGE_CSS = """
:root{--bg:#f7f6f2;--panel:#fff;--ink:#1b1b18;--muted:#6b6a63;--line:#e4e2da;--accent:#1f5f4a;
--red:#b3261e;--redbg:#fbe9e7;--warn:#8a5a00;--warnbg:#fbf0d9;--good:#1f6b45;--goodbg:#e3f3ea;--chip:#efede6}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#141412;--panel:#1c1c19;--ink:#ecebe5;
--muted:#9d9b92;--line:#2e2d29;--accent:#6cc2a1;--red:#ff8a80;--redbg:#3a1c1a;--warn:#f0c46a;--warnbg:#3a2f17;
--good:#7fd6a6;--goodbg:#16301f;--chip:#262622}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 60px}a{color:var(--accent)}
h1{font-size:24px;margin:6px 0 2px;word-break:break-all}.sub{color:var(--muted);margin:0 0 18px}
.top{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
.grade{font-size:40px;font-weight:700;width:72px;height:72px;border-radius:14px;display:flex;align-items:center;
justify-content:center;color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 14px}
.card h2{font-size:15px;margin:0 0 8px}.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:600px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top;font-size:14px}th{font-size:12px;text-transform:uppercase;color:var(--muted)}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;margin:2px 4px 2px 0;background:var(--chip)}
.chip.red{background:var(--redbg);color:var(--red)}.chip.warn{background:var(--warnbg);color:var(--warn)}
.chip.good{background:var(--goodbg);color:var(--good)}code{font-size:12px;word-break:break-all}
.g{font-weight:700}.muted{color:var(--muted)}textarea{width:100%;font:12px ui-monospace,monospace;padding:8px;
border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
form.alert{display:flex;gap:8px;flex-wrap:wrap}form.alert input[type=email],form.alert input[type=text],form.alert textarea{flex:1 1 220px;padding:8px 10px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
form.alert button{padding:8px 14px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}
.hp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
"""


def page(title, body, description=""):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><meta name="description" content="{esc(description)}">'
            f'<style>{PAGE_CSS}</style></head><body><main>{body}</main></body></html>')


def pct(x):
    """0.9934 -> '99.3%'; 1.0 -> '100%'."""
    if x is None:
        return "—"
    v = round(100 * x, 1)
    return f"{v:g}%"


def availability_30d(rows):
    """Host-level 30-day availability: the endpoint average, how many fall short of Coinbase's 99% bar for featured
    listings, and how many days of history it rests on. None when nothing has been checked in the window."""
    measured = [r for r in rows if r.get("uptime_30d") is not None and (r.get("probes_30d") or 0) > 0]
    if not measured:
        return None
    avg = sum(r["uptime_30d"] for r in measured) / len(measured)
    return {
        "average": round(avg, 4),
        "endpoints_measured": len(measured),
        "endpoints_below_bar": sum(1 for r in measured if r["uptime_30d"] < config.CURATION_BAR),
        "history_days": max((r.get("history_days") or 0) for r in measured) or None,
        "bar": config.CURATION_BAR,
    }


def listing_link(resource, prefix=""):
    from urllib.parse import quote
    return f'{prefix}listing.html?url={quote(resource, safe="")}'


def chips(flags):
    out = []
    for f in flags:
        if f in ("delisted",):
            continue
        sev = severity(f)
        cls = {"red": "red", "warn": "warn", "good": "good"}.get(sev, "")
        out.append(f'<span class="chip {cls}">{esc(FLAG_TEXT.get(f, f))}</span>')
    return "".join(out)


def seller_page(host, rows, levels, proofs, host_notes, site, onchain):
    grade, avg = host_grade(rows)
    level = best_level(levels)
    verified = level in ("dns_signed", "site_signed")
    color = GRADE_COLOR.get(grade, "#8a8a85")
    names = Counter(r["service_name"] for r in rows if r.get("service_name"))
    name = names.most_common(1)[0][0] if names else host
    grades = Counter(r["grade"] for r in rows)
    red = sum(1 for r in rows if any(severity(f) == "red" for f in r["flags"]))
    body = [f'<p class="muted"><a href="../index.html">Settle Watch</a> / <a href="../sellers.html">Sellers</a></p>',
            f'<div class="top"><div class="grade" style="background:{color}">{esc(grade or "—")}</div>'
            f'<div><h1>{esc(name)}</h1><p class="sub"><code>{esc(host)}</code>'
            + (f' · <a href="https://{esc(site)}" rel="nofollow noopener">{esc(site)} ↗</a>' if site else "")
            + "</p></div></div>"]
    summary = [f"{len(rows)} listed endpoint{'s' if len(rows) != 1 else ''}"]
    if avg is not None:
        summary.append(f"average score {avg}/100")
    summary.append(", ".join(f"{n}×{g}" for g, n in sorted(grades.items(), key=lambda kv: "ABCDF—".find(kv[0] or "—"))))
    if red:
        summary.append(f"{red} with a red flag")
    from .report import listed_on
    cats = {c for r in rows for c in listed_on(r)}
    names_ = [n for k, n in (("bazaar", "Coinbase x402 Bazaar"), ("circle", "Circle Agent Marketplace"),
                             ("mpp", "MPP directory")) if k in cats]
    if names_:
        summary.append("listed on " + " and ".join(names_))
    body.append(f'<div class="card"><h2>Summary</h2><p>{esc(" · ".join(summary))}</p>'
                f'<p class="muted">Every endpoint is checked a few times a day without paying: does it answer, what '
                f'does it ask for, and does that match its listing. <a href="../index.html#about">How grading works</a>.</p></div>')
    av = availability_30d(rows)
    if av:
        span = (f"the last {av['history_days']} days" if av["history_days"] and av["history_days"] < config.AVAILABILITY_DAYS
                else f"the last {config.AVAILABILITY_DAYS} days")
        below = av["endpoints_below_bar"]
        verdict = ("Every endpoint is at or above 99%." if not below else
                   f"{below} of {av['endpoints_measured']} endpoint{'s' if av['endpoints_measured'] != 1 else ''} "
                   f"below 99%.")
        body.append(f'<div class="card"><h2>Availability</h2><p><b>{esc(pct(av["average"]))}</b> of checks answered over '
                    f'{esc(span)}. {esc(verdict)}</p><p class="muted">Coinbase features Bazaar listings that are available '
                    f'at least 99% of the time over 30 days (above 99.5% gets priority), measured by its own probe. These '
                    f'are the board\'s unpaid checks: a second opinion, not Coinbase\'s number. Not showing up in the '
                    f'Bazaar? <a href="../listing.html">Run the free listing check</a>.</p></div>')
    if proofs:
        items = "".join(
            f'<li><code>{esc(p["address"])}</code>: {esc(LEVEL_TEXT.get(p["level"], p["level"]))}</li>' for p in proofs)
        body.append(f'<div class="card"><h2>Payout address</h2><ul>{items}</ul>'
                    f'<p class="muted">Proofs are published by the seller and re-checked daily. '
                    f'<a href="../verify.html">How payout verification works</a>.</p></div>')
    else:
        body.append('<div class="card"><h2>Payout address</h2><p>Not verified. Nothing yet proves that the wallet these '
                    'endpoints ask to be paid belongs to this seller.</p><p class="muted">Run this service? '
                    '<a href="../verify.html">Verify your payout address</a>: free, about five minutes, no transaction.</p></div>')
    if host_notes:
        items = "".join(f'<li>{esc(n.get("note") or "")} <span class="muted">({esc(n.get("added") or "")})</span></li>'
                        for n in host_notes)
        body.append(f'<div class="card"><h2>Confirmed by the seller</h2><ul>{items}</ul></div>')
    if onchain:
        body.append(f'<div class="card"><h2>Real payments on Base and Solana (30 days)</h2><p>{onchain["payments"]:,} payments from '
                    f'{onchain["payers"]:,} wallets, ${onchain["usd"]:,.2f} in USDC'
                    + (f', {round(100 * onchain["own_share"])}% from the seller\'s own wallets' if onchain["own_share"] else "")
                    + '.</p><p class="muted">All USDC received by the addresses these endpoints ask to be paid to.</p></div>')
    trs = []
    for r in sorted(rows, key=lambda r: (-(r["score"] or -1), r["resource"])):
        path = r["resource"].split(host, 1)[-1] or "/"
        g = r["grade"] or "—"
        u30 = r.get("uptime_30d")
        low = ' style="color:var(--warn)"' if u30 is not None and u30 < config.CURATION_BAR else ""
        check = (f' <a class="muted" href="{esc(listing_link(r["resource"], "../"))}">listing check</a>'
                 if (r.get("protocol") or "x402") == "x402" else "")
        trs.append(f'<tr><td><code>{esc(path)}</code>{check}<br>{chips(r["flags"])}</td>'
                   f'<td class="g" style="color:{GRADE_COLOR.get(g, "inherit")}">{esc(g)}</td>'
                   f'<td>{esc("—" if r["uptime"] is None else str(round(100 * r["uptime"])) + "%")}</td>'
                   f'<td{low}>{esc(pct(u30))}</td>'
                   f'<td>{esc("—" if r["listed_usd"] is None else "$" + format(r["listed_usd"], ".4f").rstrip("0").rstrip("."))}</td>'
                   f'<td>{esc((r.get("protocol") or "x402").upper())}</td></tr>')
    body.append('<div class="card"><h2>Endpoints</h2><div class="wrap"><table><thead><tr><th>Endpoint</th><th>Grade</th>'
                '<th>Answered (7 days)</th><th>30 days</th><th>Listed price</th><th>Protocol</th></tr></thead><tbody>'
                + "".join(trs) + "</tbody></table></div></div>")
    if config.PUBLIC_API:
        body.append(
            f'<div class="card"><h2>Get alerts</h2><p>One email when an endpoint here stops answering, comes back, '
            f'drops out of the Bazaar, falls below 99% availability over 30 days, or starts asking a different price '
            f'or wallet. Nothing otherwise.</p>'
            f'<form class="alert" method="post" action="{esc(config.PUBLIC_API)}/v1/alerts/subscribe">'
            f'<input type="hidden" name="host" value="{esc(host)}">'
            f'<input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">'
            f'<input type="email" name="email" required placeholder="you@example.com" aria-label="Email">'
            f'<button type="submit">Watch {esc(host)}</button></form>'
            f'<p class="muted">You\'ll get a confirmation link first. Unsubscribe in one click from any alert.</p></div>')
    url = f"{BOARD}seller/{host}.html"
    snippet = (f'<a href="{url}"><img src="{BOARD}badge/{host}.svg" '
               f'alt="Settle Watch: {grade or "not graded"}{", payout verified" if verified else ""}"></a>')
    body.append('<p class="muted">Run this service? <a href="../pro.html">See what sellers get</a>, '
                'including checks on every endpoint.</p>')
    body.append(f'<div class="card"><h2>Badge for your site</h2><p><img src="../badge/{esc(host)}.svg" alt=""></p>'
                f'<p class="muted">It updates with every board refresh. Paste this where you want it:</p>'
                f'<textarea rows="3" readonly onclick="this.select()">{esc(snippet)}</textarea></div>')
    body.append(f'<p class="muted">Updated {esc(db.now())}. Data: <a href="../scores.json">scores.json</a> · '
                f'<a href="../payouts.json">payouts.json</a> · <a href="../method.html">Method and limits</a>. '
                f'Questions or corrections: reply to any board email.</p>')
    desc = f"{name} ({host}) on Settle Watch: grade {grade or 'not graded'}, {len(rows)} endpoints" + \
           (", payout address verified" if verified else "")
    return page(f"{name}: Settle Watch", "".join(body), desc), badge_svg(grade, verified), grade, verified, name


JSON_ENDPOINT_CAP = 1000  # per host; listing farms have tens of thousands


def seller_json(host, rows, proofs, name, onchain=None):
    """Machine-readable seller record: what the check-before-you-pay API reads."""
    from .report import listed_on
    grade, avg = host_grade(rows)
    ranked = sorted(rows, key=lambda r: (r["grade"] in (None, "—"), -(r["probes"] or 0)))
    eps = []
    for r in ranked[:JSON_ENDPOINT_CAP]:
        eps.append({
            "resource": r["resource"],
            "grade": r["grade"] if r["grade"] not in (None, "—") else None,
            "score": r["score"] if r["grade"] not in (None, "—") else None,
            "uptime": r["uptime"],
            "uptime_30d": r.get("uptime_30d"),
            "probes_30d": r.get("probes_30d"),
            "history_days": r.get("history_days"),
            "p50_latency_ms": r["p50_latency_ms"],
            "probes": r["probes"],
            "listed_usd": r["listed_usd"],
            "network": r.get("network"),
            "listed_pay_to": r.get("pay_to"),
            "protocol": r.get("protocol") or "x402",
            "listed_on": listed_on(r),
            "flags": [{"code": f, "severity": severity(f), "text": FLAG_TEXT.get(f, f)} for f in r["flags"]],
        })
    level = best_level({p["level"] for p in proofs})
    return {
        "host": host,
        "name": name,
        "generated_at": db.now(),
        "grade": grade,
        "average_score": avg,
        "endpoints_total": len(rows),
        "endpoints_graded": sum(1 for r in rows if r["grade"] not in (None, "—")),
        "red_flag_endpoints": sum(1 for r in rows if any(severity(f) == "red" for f in r["flags"])),
        "availability_30d": availability_30d(rows),
        "payout": {
            "verified": level in ("dns_signed", "site_signed"),
            "best_level": level,
            "proofs": [{"address": p["address"], "level": p["level"]} for p in proofs],
        },
        "onchain": ({k: onchain.get(k) for k in ("usd", "payments", "payers", "own_share") if k in onchain}
                    if onchain else None),
        "page": f"{BOARD}seller/{slug(host)}.html",
        "endpoints": eps,
    }


def method_page():
    """What the verdicts mean, what each flag is based on, and what none of it can promise."""
    order = {"red": 0, "warn": 1, "info": 2, "good": 3}
    label = {"red": "Red flag", "warn": "Warning", "info": "Information", "good": "Good"}
    rows = sorted(FLAG_TEXT.items(), key=lambda kv: (order.get(severity(kv[0]), 9), kv[0]))
    flags = "".join(f'<tr><td><code>{esc(code)}</code></td><td>{esc(text)}</td><td>{label.get(severity(code), "")}</td></tr>'
                    for code, text in rows)
    body = f"""<p class="muted"><a href="index.html">Settle Watch</a> / Method and limits</p>
<h1>Method and limits</h1>
<p class="sub">What the board checks, what its answers mean, and what they can't promise.</p>
<div class="card"><h2>What is checked</h2>
<p>Every endpoint listed in Coinbase's x402 Bazaar, Circle's Agent Marketplace and the MPP directory is asked for its
price several times a day <b>without paying</b>. The answer (network, amount, the address to pay) is compared with the
listing and with earlier answers. USDC payments to each service's payout addresses are counted on Base and Solana.
Sellers can publish a signed <a href="verify.html">payout proof</a>.</p></div>
<div class="card"><h2>Verdicts (check API, MCP tool and x402-trust-guard)</h2><ul>
<li><b>avoid</b>: money would go somewhere it shouldn't. The endpoint switched to a wallet nothing it listed names, asks
to be paid at a burn address or a non-public address, or asks for an address that contradicts the payout proof the
seller published. Each is a direct observation of the endpoint's own 402 answer.</li>
<li><b>caution</b>: it works, but something needs a look: a price well above its listing (often a stale listing), a
wallet its listing doesn't name, flaky or slow answers, a C/D/F grade, too few checks to grade, or data more than a day old.</li>
<li><b>ok</b>: graded A or B on recent data, with payment terms that match its listing and nothing contradicting its
payout address.</li>
<li><b>unknown</b>: the seller isn't in any catalog the board reads.</li></ul>
<p>Grades (A–F) rank reliability and listing accuracy; they are not a safety verdict on their own.</p></div>
<div class="card"><h2>What it can't tell you</h2><ul>
<li><b>Whether you'll get what you pay for.</b> Checks never pay, so delivery after payment isn't tested.</li>
<li><b>Whether a seller is honest or solvent.</b> A payout proof shows that whoever controls the domain also controls the
wallet. Nothing more.</li>
<li><b>What happened since the last check.</b> Data refreshes about every six hours; answers say how old they are, and
anything over a day old is never "ok".</li>
<li><b>Payments elsewhere.</b> Only USDC on Base and Solana is counted. Services that hand out a new address for every
payment, or whose addresses aren't all counted yet, show no payment numbers rather than partial ones.</li></ul></div>
<div class="card"><h2>If the check can't be reached</h2>
<p>x402-trust-guard lets the payer choose: <code>onError: "allow"</code> (default, the payment goes ahead) or
<code>"block"</code>. Unknown sellers and caution verdicts are allowed by default and can be blocked the same way.</p></div>
<div class="card"><h2>How the numbers are counted</h2><ul>
<li>Headline counts cover endpoints listed now. Delisted ones stay in <a href="scores.json">scores.json</a>.</li>
<li>A service is a distinct host. Hosts paid at exactly the same addresses are shown as one service in payment tables,
and every payment total counts each address once.</li>
<li>Paid-call counts come from Coinbase's catalog; payment counts come from the chain. They differ, and both are shown.</li></ul></div>
<div class="card"><h2>Independence and corrections</h2>
<p>Grades and findings are never for sale: the seller plan changes how often and how closely a service is watched,
never how it is graded. A seller who thinks a finding is wrong can reply to any board email; confirmed intended terms
(an example offer, a price that depends on the request) are recorded as owner notes and shown as such. The code is
<a href="https://github.com/benmcalillycreative/x402-trust-board">public</a>.</p></div>
<div class="card wrap"><h2>Every flag</h2><table><thead><tr><th>Code</th><th>Meaning</th><th>Level</th></tr></thead>
<tbody>{flags}</tbody></table></div>"""
    return page("Method and limits: Settle Watch", body,
                "How Settle Watch checks agent-payable APIs, what its verdicts mean, and what they can't promise.")


def pro_page():
    """The seller plan: what sellers get, and a form to ask for early access."""
    form = (
        f'<form class="alert" method="post" action="{esc(config.PUBLIC_API)}/v1/alerts/pro" style="flex-direction:column;align-items:stretch">'
        f'<input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">'
        f'<input type="email" name="email" required placeholder="you@example.com" aria-label="Email">'
        f'<input type="text" name="host" placeholder="Your service, e.g. api.example.com" aria-label="Your service" '
        f'inputmode="url" pattern="[A-Za-z0-9.:/-]+">'
        f'<textarea name="note" rows="3" placeholder="Anything we should know (optional)" aria-label="Note"></textarea>'
        f'<button type="submit">Ask for early access</button></form>'
    ) if config.PUBLIC_API else '<p>Reply to any board email, or open an issue on the board\'s GitHub, and say which service you run.</p>'
    body = (
        '<p class="muted"><a href="index.html">Settle Watch</a> / For sellers</p>'
        '<h1>For sellers</h1>'
        '<p class="sub">Get listed, stay listed, and know the moment something breaks. Agents check the board before '
        'they pay, and Coinbase features only listings that stay up.</p>'
        '<div class="card"><h2>Free, for everyone</h2><ul>'
        '<li><a href="listing.html">Listing check</a>: is your endpoint in the Bazaar, and if not, exactly why. It runs '
        'Coinbase\'s own validator, looks up your payout address in Coinbase\'s merchant list, and gives a fix for '
        'each problem</li>'
        '<li>Your grade, endpoints, 30-day availability and payout proofs on your own <a href="sellers.html">seller '
        'page</a>, with a badge for your site</li>'
        '<li><a href="verify.html">Payout verification</a>: a DNS record and one signature, and agents can tell your '
        'wallet from a hijacker\'s</li>'
        '<li>Alerts when an endpoint stops answering, drops out of the Bazaar, falls below 99% over 30 days, or '
        'changes price or wallet</li></ul></div>'
        '<div class="card"><h2>Seller plan (early access)</h2><ul>'
        '<li>Every one of your endpoints checked every cycle, not on the rotating sample, so your availability record '
        'covers all of them</li>'
        '<li>A monthly availability report per endpoint against Coinbase\'s bar for featured listings (99% over 30 days, '
        '99.5% for priority), with every failed check and what it returned</li>'
        '<li>A listing review: we run the listing check across all your routes, then tell you what to fix and what to '
        'send Coinbase for your Agentic.Market card</li>'
        '<li>Alerts to everyone on your team, and hands-on help with a signed payout proof</li>'
        '</ul><p class="muted">Pricing is being set with the first sellers. <b>Grades are never for sale:</b> the plan '
        'doesn\'t change how you are graded, only how often and how closely you are watched. The availability numbers '
        'are the board\'s unpaid checks, a second opinion next to Coinbase\'s own probe.</p>'
        f'{form}</div>'
    )
    return page("For sellers: Settle Watch", body,
                "What sellers of agent-payable APIs get from Settle Watch, free and Pro.")


LISTING_JS = """
(function(){
var API=document.getElementById('lc').dataset.api;
var f=document.getElementById('lc'),out=document.getElementById('out'),btn=f.querySelector('button');
function el(t,c,txt){var e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e;}
var LABEL={pass:'OK',fail:'Fix',warn:'Check',info:'Note'};
var CLS={pass:'good',fail:'red',warn:'warn',info:''};
function show(d){
  out.textContent='';
  var card=el('div','card');
  card.appendChild(el('h2',null,d.listed?'Listed in the Bazaar':'Not listed in the Bazaar'));
  card.appendChild(el('p',null,d.summary));
  var meta=el('p','muted','Checked '+d.checked_at+(d.cached?' (cached for 10 minutes)':'')+'. '+(d.note||''));
  card.appendChild(meta);out.appendChild(card);
  var order={fail:0,warn:1,info:2,pass:3};
  (d.findings||[]).slice().sort(function(a,b){return order[a.status]-order[b.status];}).forEach(function(x){
    var c=el('div','card');var h=el('h2');
    h.appendChild(el('span','chip '+CLS[x.status],LABEL[x.status]));h.appendChild(document.createTextNode(' '+x.title));
    c.appendChild(h);if(x.detail)c.appendChild(el('p',null,x.detail));
    if(x.fix){var p=el('p');p.appendChild(el('b',null,'How to fix: '));p.appendChild(document.createTextNode(x.fix));c.appendChild(p);}
    out.appendChild(c);
  });
  if(d.board&&d.board.page){var p=el('p','muted');p.appendChild(document.createTextNode('Full history on your '));
    var a=el('a',null,'seller page');a.href=d.board.page;p.appendChild(a);p.appendChild(document.createTextNode('.'));out.appendChild(p);}
}
function run(url,method){
  out.textContent='';out.appendChild(el('p','muted','Checking with Coinbase\\u2019s validator\\u2026 this takes a few seconds.'));
  btn.disabled=true;
  fetch(API+'/v1/listing?url='+encodeURIComponent(url)+'&method='+method)
   .then(function(r){return r.json().then(function(j){return [r.status,j];});})
   .then(function(rj){btn.disabled=false;if(rj[0]!==200){out.textContent='';out.appendChild(el('p',null,rj[1].error||('Error '+rj[0])));return;}show(rj[1]);})
   .catch(function(){btn.disabled=false;out.textContent='';out.appendChild(el('p',null,'Couldn\\u2019t reach the check service. Try again in a minute.'));});
}
f.addEventListener('submit',function(e){e.preventDefault();var u=f.url.value.trim();var m=f.method.value;
  try{history.replaceState(null,'','?url='+encodeURIComponent(u)+(m==='POST'?'&method=POST':''));}catch(_){}
  run(u,m);});
var q=new URLSearchParams(location.search);
if(q.get('url')){f.url.value=q.get('url');if(q.get('method')==='POST')f.method.value='POST';run(f.url.value,f.method.value);}
})();
"""


def listing_page():
    """'Why isn't my endpoint in the Bazaar?' A free check for sellers, run by the check API."""
    body = (
        '<p class="muted"><a href="index.html">Settle Watch</a> / Listing check</p>'
        '<h1>Why isn\'t my endpoint in the Bazaar?</h1>'
        '<p class="sub">Paste your paid endpoint. This runs Coinbase\'s own validator (the checks the Bazaar indexer '
        'runs), looks your payout address up in Coinbase\'s merchant list, and adds the board\'s 30-day availability '
        'against Coinbase\'s 99% bar for featured listings. Every problem comes with a fix. Free, nothing is paid.</p>'
        f'<div class="card"><form class="alert" id="lc" data-api="{esc(config.PUBLIC_API)}">'
        '<input type="text" name="url" required placeholder="https://api.example.com/v1/your-route" aria-label="Endpoint URL" '
        'inputmode="url" style="flex:3 1 320px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;'
        'background:var(--bg);color:var(--ink);font:inherit">'
        '<select name="method" aria-label="HTTP method" style="padding:8px;border:1px solid var(--line);border-radius:8px;'
        'background:var(--bg);color:var(--ink);font:inherit"><option>GET</option><option>POST</option></select>'
        '<button type="submit">Check listing</button></form>'
        '<p class="muted">Same check from an agent: the <code>check_listing</code> tool on the MCP server '
        f'<code>{esc(config.PUBLIC_API)}/mcp</code>, or <code>GET {esc(config.PUBLIC_API)}/v1/listing?url=…</code>. '
        '30 checks a day per address.</p></div>'
        '<div id="out" aria-live="polite"></div>'
        '<div class="card"><h2>How a route gets into the Bazaar</h2><ol>'
        '<li>It answers an unpaid request with <b>402</b>, with the terms in the <code>PAYMENT-REQUIRED</code> header '
        '(x402 v2). Input validation that returns 400 before the payment middleware stops the indexer.</li>'
        '<li>It advertises an <code>extensions.bazaar</code> block: an example input that still gets a 402, and the '
        'schema that example validates against.</li>'
        '<li>A paid call settles through the CDP facilitator with <code>paymentPayload.resource</code> and '
        '<code>paymentPayload.extensions.bazaar</code> set. Indexing can take up to 15 minutes; '
        '<code>processing</code> isn\'t an error, and a resource with no settled payment for 30 days drops out.</li>'
        '<li>The Agentic.Market card (name, category, description) is curated by Coinbase: ask for it with an issue on '
        '<a href="https://github.com/coinbase/cdp-sdk/issues" rel="noopener">coinbase/cdp-sdk</a>.</li>'
        '<li>Featured placement needs 99% availability over 30 days on Coinbase\'s probe, complete metadata and live '
        'mainnet payments.</li></ol>'
        '<p class="muted">Source: Coinbase\'s <a href="https://docs.cdp.coinbase.com/x402/seller/get-discovered" '
        'rel="noopener">Get discovered</a> docs. Want this watched for you? <a href="pro.html">See the seller plan</a>.</p></div>'
        f'<script>{LISTING_JS}</script>'
    )
    return page("Listing check: Settle Watch", body,
                "Why isn't my x402 endpoint in Coinbase's Bazaar? A free check with a fix for each problem.")


def build(conn, rows, outdir, onchain_hosts=None, log=print):
    """Write seller/, badge/ and sellers.html under outdir. Returns the number of pages."""
    from .report import website
    by_host = defaultdict(list)
    for r in rows:
        if r.get("delisted") or "delisted" in r["flags"]:
            continue
        by_host[r["host"]].append(r)
    levels = payout.load(conn)
    conn.executescript(payout.SCHEMA)
    proofs = defaultdict(list)
    for host, address, lv in ((h, a, l) for h, m in levels.items() for a, l in m.items()):
        proofs[host].append({"address": address, "level": lv})
    all_notes = notes.load()
    chain = {h["host"]: h for h in (onchain_hosts or [])}
    (outdir / "seller").mkdir(parents=True, exist_ok=True)
    (outdir / "badge").mkdir(parents=True, exist_ok=True)
    index = []
    for host, hrows in by_host.items():
        s = slug(host)
        if not s:
            continue
        hn = [n for n in all_notes if note_host(n["match"]) == host.lower()]
        html_text, svg, grade, verified, name = seller_page(
            host, hrows, set(levels.get(host, {}).values()), proofs.get(host, []), hn, website(host), chain.get(host))
        (outdir / "seller" / f"{s}.html").write_text(html_text)
        (outdir / "badge" / f"{s}.svg").write_text(svg)
        (outdir / "seller" / f"{s}.json").write_text(json.dumps(
            seller_json(host, hrows, proofs.get(host, []), name, chain.get(host)), separators=(",", ":")))
        index.append((name, s, grade, verified, len(hrows)))
    index.sort(key=lambda t: (t[1]))
    lis = "".join(
        f'<tr><td><a href="seller/{esc(s)}.html">{esc(s)}</a><br><span class="muted">{esc(n if n != s else "")}</span></td>'
        f'<td class="g" style="color:{GRADE_COLOR.get(g, "inherit")}">{esc(g or "—")}</td><td>{c}</td>'
        f'<td>{"✓" if v else ""}</td></tr>' for n, s, g, v, c in index)
    body = (f'<p class="muted"><a href="index.html">Settle Watch</a></p><h1>Sellers</h1>'
            f'<p class="sub">Run one of these? <a href="listing.html">Check why a route isn\'t in the Bazaar</a> · <a href="pro.html">What sellers get</a>. {len(index):,} sellers with endpoints listed in the x402 Bazaar, Circle\'s Agent Marketplace or the MPP directory.</p>'
            f'<div class="card wrap"><table><thead><tr><th>Seller</th><th>Grade</th><th>Endpoints</th>'
            f'<th>Payout verified</th></tr></thead><tbody>{lis}</tbody></table></div>')
    (outdir / "pro.html").write_text(pro_page())
    (outdir / "method.html").write_text(method_page())
    (outdir / "listing.html").write_text(listing_page())
    (outdir / "sellers.html").write_text(page("Sellers: Settle Watch", body,
                                              "Every seller on Settle Watch, with grades and payout verification."))
    log(f"  Seller pages: {len(index):,}")
    return len(index)
