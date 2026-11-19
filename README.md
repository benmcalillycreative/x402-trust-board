# Settle Watch

An independent referee for agent-payable endpoints: everything in Coinbase's x402 Bazaar, Circle's
Agent Marketplace and the MPP (Machine Payments Protocol) service directory. Every 6 hours it:

1. **Crawls** the x402 Bazaar catalog (Coinbase's public discovery API) and records every listed endpoint. This happens once a day; the catalog is 66K+ listings and takes about 10 minutes to pull.
2. **Probes** the 15,000 endpoints that have gone longest without a check (at most 100 per host), without paying. It asks for the resource, expects an HTTP 402 with payment terms, and records whether it answered, how fast, and whether the terms match the listing. At four cycles a day, real services get about two checks a day.
3. **Scores** each endpoint 0–100 with a letter grade and plain-English flags.
4. **Writes** a leaderboard page (the top 500 plus every red flag) and a `scores.json` export with everything.

No wallet, no keys, no dependencies. It never pays anyone.

The live board: https://settlewatch.com/

**Source available, all rights reserved.** This code is public so that anyone, sellers included,
can see exactly how the board grades endpoints. It is not open source: no license is granted to
copy, modify or redistribute it. © 2026 Ben McAlilly.

## Get it running (5 minutes)

You need Python 3.9 or newer. On a Mac, `python3 --version` will tell you. If it offers to install the developer tools, say yes.

```bash
# 1. Put the folder in your home directory, then:
cd ~/x402-trust

# 2. Add your contact email (or a web page) to settings.env, so endpoint owners know who's checking them.
open -e settings.env

# 3. Test run on a small sample
python3 -m x402trust crawl
python3 -m x402trust probe --limit 50
python3 -m x402trust score
python3 -m x402trust report
open ~/x402-trust-data/reports/leaderboard.html

# 4. Happy? Schedule it: runs now, then every 6 hours
chmod +x install-schedule.sh && ./install-schedule.sh
```

To check on it at any time: `python3 -m x402trust status`. Logs live in `~/x402-trust-data/logs/`.
To stop the schedule: `./install-schedule.sh --remove`.

Run it on a machine that stays on. Uptime numbers are only as good as how often you check.

## How scoring works

| Weight | Signal | What it measures |
|---|---|---|
| 50% | Uptime | Share of checks where the server answered: a 402 with terms, a free response, or a 400 asking for input first. Failures caused by our own Python's TLS don't count against anyone |
| 15% | Speed | Median response time. Half a second or less gets full marks; 5 seconds or more gets zero |
| 20% | Consistency | Whether it asks to be paid where, and roughly what, its listing says |
| 15% | Adoption | Unique paying customers in the last 30 days, from the catalog |

What the first week of real data taught, now built in:

- **Fresh payment addresses are normal.** Many honest services (Browserbase, Tavily and others) generate a new address for every payment, or pay into another wallet the same service lists. Neither is penalized.
- **Prices often depend on the request.** Cheaper than listed, or up to 2×, is information only. **2–10×** and **over 10×** are warnings (a stale listing explains a big gap as often as a bad seller does, and the payer sees the real price before signing). Red flags are kept for money going somewhere it shouldn't.
- **The real hijack signature** is a wallet that *used to* match the listing and then switched to an address no listing anywhere mentions. That's an automatic F, capped at 30.

Caps, because good uptime shouldn't hide terms an agent would regret paying:

| Problem | Effect |
|---|---|
| Wallet switched to an unknown address | Capped at 30, always F |
| Asks over 10× its listed price | Capped at 45 |
| Asks 2–10× listed, pays a wallet not in its listing, or listed as paid but free | Capped at 69 |
| Points at a private network address | 0 |

Chips on the leaderboard are red (would regret paying), amber (warning), or grey (information only).
An endpoint needs 3 checks before it gets a grade.

**Big hosts are sampled.** Each cycle checks at most 100 endpoints from any one host, so one host with 66,000 templated listings can't crowd out everyone else. Real services get about two checks a day; most of a giant host's listings stay ungraded.

## Owner notes

When a seller tells you something about their own endpoints, record it so the board reflects it:

```bash
# Routes they took down on purpose (only the ones answering HTTP 410):
python3 -m x402trust note api.example.com retired "Retired on purpose (owner, Oct 29)" --when-http 410
# Terms they confirmed are deliberate, e.g. a price that depends on the request:
python3 -m x402trust note https://api.example.com/v1/search intended "Price depends on query size (owner)"
python3 -m x402trust notes          # list
python3 -m x402trust unnote api.example.com
```

**Retired** endpoints stop counting as dead or flaky and show as "Retired by owner." **Intended** drops the price and wallet warnings and shows "Owner confirmed." Notes live in `~/x402-trust-data/owner-notes.json`, so upgrades never touch them.

Sellers don't always need a note. If a bare URL can only return an illustration (it needs order details
before it can price anything), the seller can mark that offer with `"extra": {"example": true}` in the
402 requirement. A zero-address quote marked that way shows as "example offer" (information) instead of
the red burn-address flag.

## MPP (Machine Payments Protocol)

Once a day, alongside the Bazaar crawl, the board reads the MPP service directory
(`X402T_MPP_CATALOG_URL`, default `https://mpp.dev/api/services`; set it to empty to turn MPP off).
Every paid endpoint there is probed the same way: MPP answers an unpaid request with HTTP 402 and one
`WWW-Authenticate: Payment` challenge per payment method (stablecoin on Tempo, card through Stripe, ...).
The board records the option that names a recipient address, so the wallet can be tracked.

Differences from x402, built into the scoring:

- **No wallet in the listing.** The MPP directory doesn't say who gets paid, so there's nothing to compare
  against. Instead the board compares an endpoint with its own history: if it asked for the same address
  several times and then switches, it shows "Started asking for payment to a new address" (a warning).
  A payout proof settles it either way.
- **No usage numbers.** The directory doesn't publish paid calls or payers, so adoption is left out of
  MPP scores rather than counted as zero.
- **Card-only endpoints** (Stripe) have no address at all; they're checked for uptime and price only.

MPP endpoints carry an "MPP" label on the board, and the table can be filtered by protocol.

## Circle Agent Marketplace

Once a day, after the Bazaar and MPP crawls, the board reads Circle's public x402 discovery API
(`X402T_CIRCLE_CATALOG_URL`, default `https://api.circle.com/v2/x402/discovery/resources`; set it to
empty to turn it off). Listings are ordinary x402, so they're probed and graded the same way.

- A resource the Bazaar also lists stays a Bazaar row (with Bazaar usage numbers); the board only notes
  that Circle lists it too (`listed_on` in scores.json).
- A resource only Circle lists gets a "Circle" label. Circle doesn't publish usage, so, as with MPP, the
  customers part of the score is left out rather than counted as zero.
- Only the Circle crawl can delist a Circle-only row, after the same 36-hour grace period.

## Verified payout addresses

A listing can name any wallet, and nothing in x402 proves the wallet getting paid belongs to whoever runs the
endpoint. Sellers can prove it, free, with two things published together:

1. Their site or DNS names the address: a file at `https://<host>/.well-known/x402-payto.json`, or a TXT record at
   `_x402-payto.<domain>`.
2. The wallet signs a short message naming the domain (an ordinary wallet signature, no transaction).

The board checks every host that quoted a pay-to address, once a day, and shows:

| Chip | Meaning |
|---|---|
| Payout address verified (domain DNS + wallet signature) | strongest: DNS is hard to change even if the web server is breached |
| Payout address verified (its site + wallet signature) | the site and the wallet belong to the same party |
| Payout address listed by its own site (not signed) | information only |
| Asks for payment to an address its own site doesn't list | red flag, capped at 30, always F |

Sellers make their proof on `verify.html` (published next to the board): it builds the message, signs it with their
browser wallet, and gives them the file and the DNS record. The exact format is at the bottom of that page.

When a seller tells you they've published one, check it right away instead of waiting a day:

```bash
python3 -m x402trust payout api.example.com
python3 -m x402trust score && python3 -m x402trust report && ./publish.sh
```

Signatures are checked with the standard library (`x402trust/ethsig.py`); smart-contract wallets go through an
ERC-1271 call to Base (`X402T_BASE_RPC_URL`, default `https://mainnet.base.org`). DNS is looked up over HTTPS
(`X402T_DOH_URL`, default Cloudflare).

## Payout proofs for buyers' clients (payouts.json)

Next to `scores.json`, the board publishes `payouts.json`: every payout proof it has found, when
it last re-read the proofs (`proofs_checked_at`) and when an endpoint on that host last asked to be
paid to each proven address (`last_seen_asking`). A client can use those timestamps to enforce its
own staleness limit. Better still, it can verify at call time without trusting the board: read
the seller's `_x402-payto.<domain>` TXT record (or `/.well-known/x402-payto.json`), check the
signature, and compare the address with the `payTo` in the live 402. The DNS method doesn't depend
on the API server, so a compromised server alone can't forge it.

## Real payments on Base

The catalog's call and payer counts come from the seller side, and some of them are padded by sellers paying
themselves. So the board also counts payments straight from the blockchain: every USDC transfer on Base to an address
an endpoint asks to be paid to, over the last 30 days.

For each host the board shows payments, distinct paying wallets, USDC received, and how much of it came from the
service's own wallets (the receiving address itself, or any other address the same host asks to be paid to). Two
flags come out of it:

| Flag | When |
|---|---|
| Most payments come from the seller's own wallets (warning) | 20+ payments and at least half from its own wallets |
| Almost all payments come from one wallet (info) | one outside wallet sent 80%+ of payments |

Sellers buying from other sellers is normal (agents pay agents), so that is never counted against anyone. The numbers
are also written to `onchain.json` next to `scores.json`.

```bash
python3 -m x402trust chain    # also runs inside each cycle
```

The first run backfills 30 days for every address. Each run is capped at 15 minutes (`X402T_CHAIN_MAX_SECONDS`) and
picks up where it stopped, so on free public RPCs the backfill can take a few cycles. Free RPCs rate-limit hard,
so requests are paced and spread across several (`mainnet.base.org`, PublicNode, dRPC and 1RPC by default; set your
own list with `X402T_BASE_RPC_URLS`). When all of them are refusing, the run stops cleanly and the next cycle carries
on from the same block. Addresses still backfilling
are left off the board until they're caught up, so partial counts are never shown. After that, each run only reads
new blocks and takes seconds. A free Alchemy or QuickNode Base URL in `X402T_BASE_RPC_URL` makes the backfill much
faster. Set `X402T_CHAIN=0` to turn it off.

Solana USDC is counted too (`chain_sol.py`): for each Solana pay-to address, its USDC token accounts'
transactions are read back 30 days through public Solana RPCs (`X402T_SOLANA_RPC_URLS`), at most
`X402T_SOLANA_MAX_SECONDS` per run. Accounts with more than `X402T_SOLANA_MAX_TX` transactions in the window
are left out rather than shown partly counted. `X402T_SOLANA=0` turns it off. Tempo (MPP) and other tokens
show no on-chain numbers yet.

## Check-before-you-pay API (worker/)

`seller/<host>.json` on the board holds each seller's grade, payout proofs and up to 1,000 endpoint
records with their flags. The Cloudflare Worker in `worker/` turns that into a paid x402 endpoint,
`GET /v1/check?url=...&payTo=...` at $0.01, that answers ok / caution / avoid / unknown with reasons.
See `worker/README.md` to deploy it. The same Worker serves a free remote MCP server at `/mcp`
(tools `check_payment` and `check_listing`).

**For sellers, free:** `GET /v1/listing?url=...` (and `listing.html` on the board) answers "why isn't my endpoint
in the Bazaar?". It runs Coinbase's public validator (`POST /platform/v2/x402/validate`, the indexer's own checks),
looks the payout address up in Coinbase's merchant list (`/platform/v2/x402/discovery/merchant`), adds the board's
30-day availability, and returns findings with a fix for each. 30 checks a day per IP, cached 10 minutes per URL.

**Seller plan, paid in USDC:** `pro.html` posts to `POST /v1/plan/start`, which sends the seller to
`GET /v1/plan/pay?p=<plan id>&months=1|3|12`, an x402 route at $11.99 a month paid to the same `PAY_TO` (browsers
get a wallet page). USDC can't be pulled from a wallet, so the plan is prepaid: each settled payment moves the plan's
end date forward and is queued for the board's computer, where `x402trust/plans.py` records it in `plans.json`, sends
the receipt with the transaction link, adds the domain to the payer's alerts, tells the owner, emails one pay link
five days before the end date and one note when it has ended. No invoices; nothing charges by itself.
A new plan also gets its listing review on the next run (the Worker's listing check across every route on the
domain, authenticated with `ALERTS_TOKEN` so it isn't held to the public limit, grouped into what to fix, plus a
drafted Agentic.Market card request), and every active plan gets a 30-day availability report per endpoint, with
each failed check, on the first run of the month. The owner gets a copy of each review.

**30-day availability:** every score run also computes, per endpoint, the share of counted checks that answered over
the last 30 days (`uptime_30d`, `probes_30d`, `history_days`). Seller pages and `seller/<host>.json` show it against
Coinbase's bar for featured listings (99% over 30 days), and watchers get an alert when an endpoint crosses that bar.

`packages/x402-trust-guard/` is a one-line add-on for x402 clients (`withTrustGuard(client)`) that runs that
check before every payment and refuses red flags. It is MIT-licensed (unlike the rest of this repo) so anyone
can ship it; publish it with `cd packages/x402-trust-guard && npm publish`.

## State of agent payments (monthly)

`python3 -m x402trust monthly [YYYY-MM]` writes a market snapshot to `reports/state/`: numbers
(`.json`), a draft to edit and post (`.md`) and a page published with the board (`.html`). Each issue
compares itself with the previous one. `run` writes last month's issue by itself in the first three
days of each month.

## Automatic updates

When this folder is a git checkout of the `main` branch, `run.sh` checks GitHub for new code
before every cycle (`update.sh`). New code is kept only if the full test suite passes on this
machine; otherwise the previous code is restored and the cycle runs on it. Every update and
rollback is written to the day's log. Your `settings.env` and data folder are never touched.
Set `X402T_AUTO_UPDATE=0` in `settings.env` to turn it off.

Owner notes can ship with the code in `managed/owner-notes.json`. A note you add locally
(`python3 -m x402trust note ...`) for the same endpoint takes precedence.

## Alerts

Sellers can ask to be told when their endpoints change. Watched endpoints are checked every cycle, and after each cycle a watcher gets at most one email covering what changed: an endpoint stopped answering (two failed checks in a row) or came back, started asking a different price or wallet, or left or rejoined the catalog. Their first email says "monitoring is on" and gives the current status. No change, no email.

```bash
python3 -m x402trust watch owner@example.com api.example.com --name "Example"
python3 -m x402trust watchers
python3 -m x402trust unwatch owner@example.com
python3 -m x402trust alerts --dry-run   # preview what would go out, without sending
```

**Email setup (Gmail):** turn on 2-Step Verification for the account, create an app password at https://myaccount.google.com/apppasswords, then add these to `settings.env`:

```
X402T_SMTP_USER=you@gmail.com
X402T_SMTP_PASS=your-16-character-app-password
X402T_ALERT_FROM_NAME=Your Name
```

Run `chmod 600 settings.env` so only your account can read it, and `python3 -m x402trust test-email you@gmail.com` to check it works. Until email is set up, alerts are saved to `~/x402-trust-data/outbox/` and nothing is sent.

**Self-serve sign-ups (optional).** With the Worker in `worker/` deployed, every seller page gets a
"Get alerts" form. Sign-ups are double opt-in: the Worker only stores the request; this computer sends
the confirmation email (from the same address as the alerts) and adds the watcher once the link is
clicked. Every alert then carries a one-click unsubscribe link. Add to `settings.env`:

```
X402T_API_URL=https://x402-trust-check.<your-subdomain>.workers.dev
X402T_ALERTS_TOKEN=<the same long random string you gave the Worker as ALERTS_TOKEN>
```

`python3 -m x402trust signups` processes the queue on demand; `run` does it every cycle.

## Publishing the board (optional)

The board can publish itself to GitHub Pages after every cycle. It's free, and there's no server to run.

1. On GitHub, create a new **public** repository, for example `x402-trust-board`, with no files.
2. On the mini: `./setup-publish.sh your-github-name/x402-trust-board`. This makes a key that can only push to that one repo and prints it.
3. Add that key in the repo under **Settings → Deploy keys → Add deploy key**, and tick **Allow write access**.
4. Run `./publish.sh` once. Then go to **Settings → Pages**, choose **Deploy from a branch**, select `gh-pages` and `/ (root)`, and click **Save**.

From then on, every cycle republishes the board automatically. Each publish replaces the branch with one fresh commit, so the repo never grows.
To use your own domain, set `X402T_PUBLISH_DOMAIN=trust.example.com` in `settings.env` and add a CNAME record pointing to `your-github-name.github.io`.
To stop publishing, remove the `X402T_PUBLISH_REPO` line from `settings.env`.

**Before going public,** set `X402T_CONTACT` to an address you're happy to show the world. The board shows it under "Is this your endpoint?"

## Safety

- **It never pays.** There's no wallet in the code.
- **It won't touch your home network.** Listings are written by strangers, so it refuses any URL that resolves to a private address (your router, your other machines) and never follows redirects.
- **It's polite.** At most 2 simultaneous requests to any one host. It identifies itself honestly in every request. If a host fails 5 times in a row, it's marked down for the rest of that cycle instead of being hammered.
- **Cycles never overlap.** If one is still running when the next is due, the next one skips.

## Files

```
x402trust/crawl.py    catalog crawler (pagination, retries, delisting)
x402trust/probe.py    unpaid probes (v1 body and v2 PAYMENT-REQUIRED header)
x402trust/parse.py    reading payment terms, USDC → dollars, network names
x402trust/netguard.py private-address blocking
x402trust/score.py    trust scores and flags (tune the weights here)
x402trust/report.py   leaderboard HTML + scores.json
x402trust/crawl_mpp.py MPP directory crawler
x402trust/crawl_circle.py Circle Agent Marketplace crawler
x402trust/payout.py   verified payout addresses (DNS, site file, wallet signature)
x402trust/chain.py    real USDC payments on Base, counted from the chain
update.sh             self-update from GitHub (kept only if tests pass)
managed/              owner notes that ship with the code
tests/test_e2e.py     full test against a fake Bazaar: python3 -m unittest discover -s tests
```

Everything is stored in one SQLite file, `~/x402-trust-data/trust.db`. Raw catalog entries and every probe are kept, so you can re-score history later with new rules.

## What's next

- **Paid test calls** on a small rotating sample, with a capped budget. These check that the endpoint delivers what it promises after you pay. That's the signal nobody else has.
