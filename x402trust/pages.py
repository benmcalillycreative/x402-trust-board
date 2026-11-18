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

BOARD = "https://benmcalillycreative.github.io/x402-trust-board/"
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
    body = [f'<p class="muted"><a href="../index.html">x402 Trust Board</a> / <a href="../sellers.html">Sellers</a></p>',
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
        trs.append(f'<tr><td><code>{esc(path)}</code><br>{chips(r["flags"])}</td>'
                   f'<td class="g" style="color:{GRADE_COLOR.get(g, "inherit")}">{esc(g)}</td>'
                   f'<td>{esc("—" if r["uptime"] is None else str(round(100 * r["uptime"])) + "%")}</td>'
                   f'<td>{esc("—" if r["listed_usd"] is None else "$" + format(r["listed_usd"], ".4f").rstrip("0").rstrip("."))}</td>'
                   f'<td>{esc((r.get("protocol") or "x402").upper())}</td></tr>')
    body.append('<div class="card"><h2>Endpoints</h2><div class="wrap"><table><thead><tr><th>Endpoint</th><th>Grade</th>'
                '<th>Answered</th><th>Listed price</th><th>Protocol</th></tr></thead><tbody>'
                + "".join(trs) + "</tbody></table></div></div>")
    if config.API_URL:
        body.append(
            f'<div class="card"><h2>Get alerts</h2><p>One email when an endpoint here stops answering, comes back, '
            f'or starts asking a different price or wallet. Nothing otherwise.</p>'
            f'<form class="alert" method="post" action="{esc(config.API_URL)}/v1/alerts/subscribe">'
            f'<input type="hidden" name="host" value="{esc(host)}">'
            f'<input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">'
            f'<input type="email" name="email" required placeholder="you@example.com" aria-label="Email">'
            f'<button type="submit">Watch {esc(host)}</button></form>'
            f'<p class="muted">You\'ll get a confirmation link first. Unsubscribe in one click from any alert.</p></div>')
    url = f"{BOARD}seller/{host}.html"
    snippet = (f'<a href="{url}"><img src="{BOARD}badge/{host}.svg" '
               f'alt="x402 Trust Board: {grade or "not graded"}{", payout verified" if verified else ""}"></a>')
    body.append('<p class="muted">Run this service? <a href="../pro.html">See what sellers get</a>, '
                'including checks on every endpoint.</p>')
    body.append(f'<div class="card"><h2>Badge for your site</h2><p><img src="../badge/{esc(host)}.svg" alt=""></p>'
                f'<p class="muted">It updates with every board refresh. Paste this where you want it:</p>'
                f'<textarea rows="3" readonly onclick="this.select()">{esc(snippet)}</textarea></div>')
    body.append(f'<p class="muted">Updated {esc(db.now())}. Data: <a href="../scores.json">scores.json</a> · '
                f'<a href="../payouts.json">payouts.json</a>. Questions or corrections: reply to any board email.</p>')
    desc = f"{name} ({host}) on the x402 Trust Board: grade {grade or 'not graded'}, {len(rows)} endpoints" + \
           (", payout address verified" if verified else "")
    return page(f"{name}: x402 Trust Board", "".join(body), desc), badge_svg(grade, verified), grade, verified, name


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


def pro_page():
    """The seller plan: what sellers get, and a form to ask for early access."""
    form = (
        f'<form class="alert" method="post" action="{esc(config.API_URL)}/v1/alerts/pro" style="flex-direction:column;align-items:stretch">'
        f'<input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">'
        f'<input type="email" name="email" required placeholder="you@example.com" aria-label="Email">'
        f'<input type="text" name="host" placeholder="Your service, e.g. api.example.com" aria-label="Your service" '
        f'inputmode="url" pattern="[A-Za-z0-9.:/-]+">'
        f'<textarea name="note" rows="3" placeholder="Anything we should know (optional)" aria-label="Note"></textarea>'
        f'<button type="submit">Ask for early access</button></form>'
    ) if config.API_URL else '<p>Reply to any board email, or open an issue on the board\'s GitHub, and say which service you run.</p>'
    body = (
        '<p class="muted"><a href="index.html">x402 Trust Board</a> / For sellers</p>'
        '<h1>For sellers</h1>'
        '<p class="sub">Agents increasingly check the board before they pay. Make sure what they see about you is right, '
        'and find out the moment something breaks.</p>'
        '<div class="card"><h2>Free, for everyone</h2><ul>'
        '<li>Your grade, endpoints and payout proofs on your own <a href="sellers.html">seller page</a>, with a badge for your site</li>'
        '<li><a href="verify.html">Payout verification</a>: a DNS record and one signature, and agents can tell your wallet from a hijacker\'s</li>'
        '<li>Alerts when your endpoints stop answering or change price or wallet</li></ul></div>'
        '<div class="card"><h2>Pro (early access)</h2><ul>'
        '<li>Every one of your endpoints checked every cycle, not on the rotating sample, so your grade reflects all of them</li>'
        '<li>Alerts on all your endpoints to everyone on your team</li>'
        '<li>Hands-on help setting up a signed payout proof</li>'
        '<li>A monthly report on your service: uptime, failed checks, what agents saw, and the payments counted on chain</li>'
        '</ul><p class="muted">Pricing is being set with the first sellers. <b>Grades are never for sale:</b> Pro doesn\'t '
        'change how you are graded, only how often and how closely you are watched.</p>'
        f'{form}</div>'
    )
    return page("For sellers: x402 Trust Board", body,
                "What sellers of agent-payable APIs get from the x402 Trust Board, free and Pro.")


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
    body = (f'<p class="muted"><a href="index.html">x402 Trust Board</a></p><h1>Sellers</h1>'
            f'<p class="sub">Run one of these? <a href="pro.html">What sellers get</a>. {len(index):,} sellers with endpoints listed in the x402 Bazaar, Circle\'s Agent Marketplace or the MPP directory.</p>'
            f'<div class="card wrap"><table><thead><tr><th>Seller</th><th>Grade</th><th>Endpoints</th>'
            f'<th>Payout verified</th></tr></thead><tbody>{lis}</tbody></table></div>')
    (outdir / "pro.html").write_text(pro_page())
    (outdir / "sellers.html").write_text(page("Sellers: x402 Trust Board", body,
                                              "Every seller on the x402 Trust Board, with grades and payout verification."))
    log(f"  Seller pages: {len(index):,}")
    return len(index)
