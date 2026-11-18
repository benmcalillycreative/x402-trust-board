# Check-before-you-pay API

A Cloudflare Worker that answers one question for an agent about to pay an x402 or MPP endpoint:
**should I?** It reads the board's latest checks (`seller/<host>.json`, published with the board)
and charges $0.001 USDC on Base per answer, paid with x402 and settled through Coinbase's CDP
facilitator. Bad input is refused before the paywall, and nothing is charged if the board can't be read.

```
GET /v1/check?url=<the URL you're about to pay>&payTo=<the address its 402 asks for, optional>
```

```json
{
  "verdict": "ok | caution | avoid | unknown",
  "reasons": ["..."],
  "endpoint": {"grade": "A", "score": 91, "uptime": 0.99, "p50_latency_ms": 210, "flags": []},
  "seller": {"grade": "B", "payout": {"verified": true, "proofs": [...]}, "page": "..."},
  "pay_to": {"proven_by_seller": "dns_signed", "matches_listing": true, "contradicts_seller_proof": false}
}
```

- **avoid**: a red flag (wallet switched, burn address, payout mismatch, 10x overcharge) or the
  address you're about to pay contradicts the payout proof the seller published.
- **caution**: works, but has warnings, a C/D/F grade, or not enough history.
- **ok**: graded A or B with matching terms.
- **unknown**: the seller isn't in any catalog the board reads.

Free: `GET /` (description) and `GET /v1/health`. The same data is free in bulk on the board
(`scores.json`, `payouts.json`, `seller/<host>.json`); the API is for agents that want one answer,
pay-per-call, without downloading anything.

## Deploy (from a computer with Node 20+)

```bash
cd worker
npm ci
npx wrangler login                        # once
npx wrangler secret put PAY_TO            # your Base address that receives payments
npx wrangler secret put CDP_API_KEY_ID    # portal.cdp.coinbase.com > API keys
npx wrangler secret put CDP_API_KEY_SECRET
npx wrangler deploy
```

The first 1,000 settlements a month through the CDP facilitator are free. After the first paid call
settles, the facilitator lists the endpoint in the x402 Bazaar using the discovery metadata the Worker
declares.

## Test

```bash
npm test        # verdict logic
npm run e2e     # wrangler dev + fake board + fake facilitator + a real x402 client with a throwaway key
```
