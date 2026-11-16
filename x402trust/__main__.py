"""Command line: python3 -m x402trust <command>

The cycle
  crawl     pull the x402 Bazaar catalog and the MPP service directory
  probe     check the endpoints that have gone longest without a check (never pays)
  score     compute trust scores from probe history
  report    write the leaderboard HTML + scores.json
  alerts    email watchers about anything that changed   (--dry-run to preview)
  payout    check sellers' published payout proofs (once a day per host)
  chain     count real USDC payments to sellers' Base wallets (incremental, time-capped)
            payout HOST   check one host right now and show what was found
  run       all of the above; crawls only once a day (what the schedule calls)
  status    quick summary of what's in the database

Owner notes (what sellers tell you about their own endpoints)
  note HOST_OR_URL retired|intended "what they said" [--when-http 410]
  notes                     list them
  unnote HOST_OR_URL        remove

Alerts
  watch EMAIL HOST_OR_URL [--name "Their name"]
  unwatch EMAIL [HOST_OR_URL]
  watchers                  list them
  test-email EMAIL          send one test message to check your email settings
"""

import argparse
import sys

from . import alerts, chain, config, crawl, crawl_mpp, db, notes, payout, probe, report, score

CYCLE = ["crawl", "probe", "payout", "chain", "score", "report", "alerts", "run", "status"]
MANAGE = ["note", "notes", "unnote", "watch", "unwatch", "watchers", "test-email"]


def status(conn):
    ep = conn.execute("SELECT COUNT(*), SUM(delisted=0) FROM endpoints").fetchone()
    pr = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM probes").fetchone()
    print(f"Database:  {config.DB_PATH}")
    print(f"Endpoints: {ep[0] or 0:,} known, {ep[1] or 0:,} currently listed")
    print(f"Probes:    {pr[0] or 0:,} recorded" + (f" ({pr[1]} → {pr[2]})" if pr[0] else ""))
    for row in conn.execute(
        "SELECT grade, COUNT(*) n FROM scores GROUP BY grade ORDER BY grade"
    ):
        print(f"  grade {row['grade']}: {row['n']:,}")
    w = alerts.load_watchers()
    print(f"Watchers:  {len(w)}" + ("" if alerts.smtp_ready() else " (email not set up; alerts go to outbox/)"))
    print(f"Owner notes: {len(notes.load())}")


def manage(args):
    a = args.args
    if args.command == "note":
        if len(a) < 3:
            print('Usage: note HOST_OR_URL retired|intended "what they said" [--when-http 410]')
            return 1
        codes = [int(c) for c in args.when_http.split(",")] if args.when_http else None
        n = notes.add(a[0], a[1], " ".join(a[2:]), codes)
        print(f"Noted: {n['match']} is {n['status']}"
              + (f" (when HTTP {', '.join(map(str, codes))})" if codes else "")
              + ". It takes effect at the next score.")
    elif args.command == "notes":
        for n in notes.load():
            when = f" [HTTP {', '.join(map(str, n['when_http']))}]" if n.get("when_http") else ""
            print(f"{n['added']}  {n['status']:<9} {n['match']}{when}  {n['note']}")
    elif args.command == "unnote":
        print(f"Removed {notes.remove(a[0]) if a else 0} note(s).")
    elif args.command == "watch":
        if len(a) != 2:
            print('Usage: watch EMAIL HOST_OR_URL [--name "Their name"]')
            return 1
        alerts.watch(a[0], a[1], args.name or "")
        print(f"Watching {a[1]} for {a[0]}. They'll get a 'monitoring is on' email after the next cycle.")
        if not alerts.smtp_ready():
            print("Note: email isn't set up yet, so alerts will be saved to outbox/ instead of sent.")
    elif args.command == "unwatch":
        if not a:
            print("Usage: unwatch EMAIL [HOST_OR_URL]")
            return 1
        print(f"Removed {alerts.unwatch(a[0], a[1] if len(a) > 1 else None)} watch(es).")
    elif args.command == "watchers":
        for w in alerts.load_watchers():
            print(f"{w['added']}  {w['email']:<32} {w['match']}  {w.get('name', '')}")
    elif args.command == "test-email":
        if not a:
            print("Usage: test-email EMAIL")
            return 1
        result = alerts.deliver(a[0], "x402 Trust Board: test email",
                                "If you can read this, alert emails are working.")
        print(f"Test email: {result}")
        return 0 if result == "sent" else 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="x402trust", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=CYCLE + MANAGE)
    parser.add_argument("args", nargs="*")
    parser.add_argument("--limit", type=int,
                        help="probe this many endpoints this time (default: X402T_PROBE_BATCH)")
    parser.add_argument("--when-http", help="for note: only endpoints whose latest check returned these codes, e.g. 410")
    parser.add_argument("--name", help="for watch: the name to greet them by")
    parser.add_argument("--dry-run", action="store_true", help="for alerts: print emails instead of sending")
    args = parser.parse_args(argv)

    if args.command in MANAGE:
        return manage(args)

    conn = db.connect()
    crawl_now = args.command == "crawl" or (args.command == "run" and crawl.due(conn))
    if args.command == "run" and not crawl_now:
        print(f"Catalog crawled within the last {config.CRAWL_EVERY_HOURS:g}h; skipping crawl.")
    if crawl_now:
        try:
            crawl.run(conn)
        except Exception as exc:
            print(f"Crawl failed: {exc}", file=sys.stderr)
            if args.command == "crawl":
                return 1
            print("Continuing with the endpoints already on file.", file=sys.stderr)
        if config.MPP_CATALOG_URL:
            try:
                crawl_mpp.run(conn)
            except Exception as exc:  # one catalog failing shouldn't stop the other
                print(f"MPP directory crawl failed: {exc}", file=sys.stderr)
    if args.command in ("probe", "run"):
        probe.run(conn, limit=args.limit)
    if args.command == "payout" and args.args:
        hosts = [h.lower().replace("https://", "").split("/")[0] for h in args.args]
        payout.run(conn, hosts=hosts, log=lambda *_: None)
        for h in hosts:
            print(payout.describe(conn, h))
        print("Run `score` and `report` (or wait for the next cycle) to update the board.")
        return 0
    if args.command in ("payout", "run"):
        try:
            payout.run(conn)
        except Exception as exc:  # never let this block scoring
            print(f"Payout check failed: {exc}", file=sys.stderr)
    if args.command == "chain" or (args.command == "run" and config.CHAIN_ENABLED):
        try:
            chain.run(conn)
        except Exception as exc:  # a slow or failing RPC shouldn't stop the board
            print(f"On-chain payment check failed: {exc}", file=sys.stderr)
    if args.command in ("score", "run"):
        score.run(conn)
    if args.command in ("report", "run"):
        report.run(conn)
    if args.command in ("alerts", "run"):
        alerts.run(conn, dry_run=args.dry_run)
    if args.command == "status":
        status(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
