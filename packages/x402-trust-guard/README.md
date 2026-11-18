# x402-trust-guard

Check an x402 seller before your agent pays it. One line on top of `@x402/fetch`.

```js
import { wrapFetchWithPayment, x402Client } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { withTrustGuard } from "x402-trust-guard";

const client = new x402Client().register("eip155:*", new ExactEvmScheme(account));
withTrustGuard(client);                       // ← the one line
const pay = wrapFetchWithPayment(fetch, client);

await pay("https://api.some-seller.com/v1/data"); // throws instead of paying if the seller looks unsafe
```

Before your client signs a payment, the guard asks the [x402 Trust Board](https://benmcalillycreative.github.io/x402-trust-board/)
about the endpoint and the address it wants paid, and **stops the payment** when the answer is `avoid`:

- the endpoint switched to a different wallet, or asks to be paid at a burn address
- the address isn't in the payout proof the seller published (a hijacked server can change where money goes; it can't change the seller's DNS)

Price problems (asking well above the listing), flaky uptime, low grades and thin history come back as `caution`,
which you can choose to block too (`onCaution: "block"`).

The board checks every listing in Coinbase's x402 Bazaar, Circle's Agent Marketplace and the MPP directory several times a day,
without paying, and counts real payments on Base and Solana.

## Cost

- **`mode: "paid"`** (default): asks the check API, $0.01 per check, paid by your own client over x402.
  Each endpoint is checked at most once a day (`cacheSeconds`), so an agent calling a seller 500 times a day pays one cent.
- **`mode: "free"`**: reads the board's public seller files and decides locally. Same verdicts, no payment.

## Options

```js
withTrustGuard(client, {
  mode: "paid",          // "paid" | "free"
  onCaution: "allow",    // "allow" | "block": works, but warnings / low grade / little history
  onUnknown: "allow",    // "allow" | "block": a seller the board has never seen
  onError: "allow",      // "allow" | "block": the check itself failed (network, board down)
  cacheSeconds: 86400,
  onResult: (r) => console.log(r.verdict, r.url, r.reasons),
});
```

`checkFree(url, payTo?)` checks one URL yourself, for free, and returns the full result.

## Also available

- **HTTP:** `GET https://x402-trust-check.ben-mcalilly.workers.dev/v1/check?url=…&payTo=…` ($0.01, x402)
- **MCP:** add `https://x402-trust-check.ben-mcalilly.workers.dev/mcp` as a remote MCP server; tool `check_payment`, 100 free checks a day
- **Sellers:** prove your payout address with a DNS record and a signature: [how](https://benmcalillycreative.github.io/x402-trust-board/verify.html)

MIT licensed.
