# x402 Payout Proofs (draft)

**Status:** draft for discussion · **Version:** `x402payto1` · **Reference verifier:**
[`x402trust/payout.py`](../x402trust/payout.py) · **Live data:** [payouts.json](https://settlewatch.com/payouts.json)

## 1. Problem

An x402 `402 Payment Required` response tells the client which address to pay (`payTo`). Nothing in
the protocol ties that address to whoever runs the endpoint. If the server, its hosting, a proxy in
front of it, or a catalog listing is compromised or simply wrong, clients pay a stranger, and the
payment is final.

This draft defines a small, optional, free way for a seller to publish **which addresses are theirs**,
in places an attacker who only controls the HTTP response can't change, plus a signature that shows
the wallet's holder agrees. Clients and facilitators can check it before paying.

It needs no change to x402 itself, no registry, and no transaction.

## 2. Terms

- **Seller domain**: the DNS name the paid resource is served from (`api.example.com`), or a parent of
  it that the seller controls (`example.com`).
- **Payout address**: an address that appears in `accepts[].payTo`.
- **Proof**: a statement, in one of the locations below, that a payout address belongs to the seller domain.
- **Verifier**: anything that checks proofs (a client before paying, a facilitator, a catalog, a monitor).

The key words MUST, SHOULD and MAY are used as in RFC 2119.

## 3. Where proofs live

A seller MAY publish proofs in either or both places.

### 3.1 DNS (stronger)

One TXT record per address at `_x402-payto.<domain>`:

```
_x402-payto.example.com.  TXT  "v=x402payto1; a=0xAbC…; d=2026-11-04; s=0x…"
```

| Field | Required | Meaning |
|---|---|---|
| `v` | yes | Always `x402payto1`. Records with any other value MUST be ignored. |
| `a` | yes | The payout address, as it appears in `payTo`. |
| `d` | with `s` | Issue date, `YYYY-MM-DD` (UTC), used in the signed message. |
| `s` | no | Signature over the message in section 4, hex with `0x`. |

- Fields are separated by `;`, keys are case-insensitive, whitespace around keys and values is ignored.
- Verifiers MUST join a TXT record's character strings before parsing (signatures exceed 255 bytes).
- A domain MAY have several records, one per address (e.g. one per network, or old and new during a
  rotation).
- A record on `example.com` covers `example.com` and all its subdomains. Verifiers SHOULD look up the
  resource's host and each parent up to, but not including, the registrable suffix.
- Verifiers MUST NOT accept a record on a shared-hosting parent (anything in the private section of
  the Public Suffix List, e.g. `onrender.com`, `workers.dev`, `vercel.app`), because one customer's
  record there says nothing about another's subdomain. Sellers on shared hosting use 3.2.

DNS is preferred because someone who takes over a web server or a deployment usually can't change the
domain's DNS.

### 3.2 Well-known file

`https://<host>/.well-known/x402-payto.json`, served from the exact host of the paid resource:

```json
{
  "version": 1,
  "domain": "api.example.com",
  "payTo": [
    {"address": "0xAbC…", "issued": "2026-11-04", "signature": "0x…"},
    {"address": "7xKX…", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"}
  ]
}
```

- `domain` MUST equal the host it is served from. A file covers only that host.
- Entries MAY be bare strings (`"0xAbC…"`), meaning unsigned.
- `network` is optional (CAIP-2) and informational.
- Verifiers MUST NOT follow redirects, SHOULD cap the response (64 KB is plenty), and SHOULD read at
  most 20 entries.

## 4. The signed message

A signature shows that whoever holds the payout wallet agrees it serves this domain. The message is
UTF-8, lines separated by `\n`, no trailing newline:

```
x402 payout verification

Domain: <domain, lowercase>
Pay-to: <address, lowercase for 0x addresses>
Issued: <YYYY-MM-DD>
```

`<domain>` is the name the proof is published on: the DNS name the TXT record sits under (without
`_x402-payto.`), or the file's `domain`.

### 4.1 EVM addresses

- Sign with EIP-191 `personal_sign` (`"\x19Ethereum Signed Message:\n" + len + message`).
- **Externally owned accounts:** valid if `ecrecover` returns the address.
- **Smart-contract wallets:** valid if `isValidSignature(hash, signature)` (ERC-1271) on the payout
  network returns `0x1626ba7e`.
- **Undeployed smart wallets** (ERC-6492 wrapped signatures) are out of scope for v1: verifiers MAY
  report them as unverifiable rather than invalid. Sellers should deploy the wallet or sign with an EOA.

### 4.2 Solana addresses (proposed)

Ed25519 signature over the same UTF-8 message (as produced by wallet `signMessage`), base58-encoded,
verified against the address's public key. Not yet checked by the reference verifier.

### 4.3 Date

`Issued` MUST NOT be more than one day in the future. It is not an expiry; it records when the
seller made the statement, so verifiers can show its age and sellers can re-issue after a rotation.

## 5. Verification levels

From strongest to weakest:

| Level | Meaning |
|---|---|
| `dns_signed` | DNS record + valid signature |
| `site_signed` | Well-known file + valid signature |
| `dns` | DNS record, no or invalid signature |
| `site` | Well-known file, no or invalid signature |

A signature that fails to verify MUST NOT raise the level; verifiers SHOULD report it so the seller can
fix it.

## 6. Using proofs before paying

Given a 402 response with `payTo = P` for a resource on host `H`:

1. Collect proofs for `H` (sections 3.1 and 3.2). Cache for at most 24 hours. **DNS decides when it exists:**
   if the domain publishes any DNS proof, only addresses in DNS count, and a well-known file entry for any
   other address is ignored. Whoever takes over the web server can rewrite that file and sign it with a
   wallet of their own; they can't change the DNS. The file is for sellers who can't publish DNS (shared
   hosting), and verifiers should label it as the weaker level.
2. If no proofs exist: proceed under the client's normal policy. Absence proves nothing.
3. If `P` is in the proofs: proceed. Clients MAY show the level.
4. If proofs exist and `P` is **not** among them: this is a **payout mismatch**. Clients SHOULD NOT pay
   without explicit confirmation. Catalogs and monitors SHOULD flag the listing.

Step 4 is the point of this spec: once a seller publishes a proof, a hijacked endpoint can no longer
silently redirect payments.

### 6.1 Caching

A live DNS lookup should not sit in front of every payment. Clients SHOULD:

1. Cache a domain's proof set for **min(record TTL, 24 hours)**. When no record exists, cache that answer
   for the negative TTL (the SOA minimum), capped the same way.
2. When the quoted `payTo` **is** in the cached set, pay. No lookup.
3. When it **isn't**, look up DNS live (bypassing the cache) before deciding. A legitimate rotation
   passes this re-check; an address swapped on a compromised server fails it, because the attacker
   can't change the domain's DNS. This is the only path that pays for a round trip.
4. Rate-limit those re-checks per domain (for example, at most one a minute), so a hostile server
   quoting random addresses can't make the client hammer DNS.
5. Treat a failed lookup as no evidence either way: never block on DNS being unreachable alone.

Revocation is the one case the cache bounds: after a seller removes an address, a client may still
match it until its cached copy expires (at most 24 hours, or the record's TTL if shorter). Sellers who
may need to revoke under pressure should keep the TTL short.

The reference implementation is `proofs.js` in `x402-trust-guard`, shared with the Settle Watch check
API.

### 6.2 Outcomes

A check reports exactly one of these. Most checks today end in `absent`, so the distinction matters most
there.

| Outcome | When | What the client does |
|---|---|---|
| `verified` | A proof exists for the domain and lists `P` (level per section 5) | Pay. May show the level and the proof's `Issued` date |
| `mismatch` | Proofs exist and `P` isn't in any of them, after the live re-check (6.1, step 3) | Don't pay without confirmation (step 4) |
| `malformed` | A `_x402-payto` record or file exists but can't be parsed or names another domain | Report it to the seller; decide as if `absent`. It never blocks on its own |
| `absent` | No proof published | Normal policy. Not a verdict on the seller |
| `unchecked` | The check didn't run or couldn't finish (offline, policy, DNS timeout) | Normal policy. Never reported as `absent` or `mismatch` |

`absent` must never be treated as `mismatch`: proofs are opt-in, and blocking the sellers who haven't
published one would stop anyone from adopting them. `unchecked` must never be reported as `verified`.

**Signature status is reported alongside the outcome, not as one.** A record that parses and lists
addresses binds them to the domain whatever its signature says, because the binding comes from whoever
controls the DNS (or the site). So `verified` and `mismatch` are decided on the address list, and each
proof carries its own signature status:

| Signature | Meaning | Effect |
|---|---|---|
| `valid` | Recovers to the listed address (or passes ERC-1271 for a contract wallet) | Raises the level to `*_signed` |
| `invalid` | Checked, and doesn't verify for the listed address | Level stays unsigned; report it to the seller |
| `unverifiable` | Couldn't be checked here (a contract wallet and no chain access) | Level stays unsigned; not a failure |
| `none` | No signature | Level stays unsigned |

An `invalid` signature is worth surfacing, and a client MAY treat it more strictly than an unsigned proof.
It is rarely an attack, though: someone who controls the domain can sign with a wallet of their own and
pass. In practice it means the seller signed with the wrong wallet or the wrong message. Clients without
chain access MUST report a contract wallet's signature as `unverifiable`, never `invalid`.

The age of a proof is information, not an outcome. A verifier SHOULD report `Issued` with a `verified`
result and MAY let the client set a maximum age, but age MUST NOT soften a `mismatch`: a record that is
still published and doesn't list `P` looks the same whether the seller rotated carelessly or the endpoint
was hijacked, and the second is what the proof exists to catch. Sellers avoid the false alarm by adding
the new address before switching (section 7).

Exceptions verifiers should handle:

- **Per-payment addresses.** Some sellers quote a fresh address every time (deposit-address or payment
  processor flows). A mismatch is meaningless for them; verifiers SHOULD only flag a mismatch when the
  quoted address is stable across checks. A future version may define a proof for an address
  *factory* or processor.
- **Weaker hints.** Addresses mentioned in other files (e.g. a general service manifest at
  `/.well-known/x402`) MAY confirm a match but MUST NOT, on their own, cause a mismatch.

## 7. Rotation and revocation

- To rotate: publish the new address (new record or entry), **wait one TTL** so resolvers serve it,
  switch `payTo`, then remove the old one.
- TTL: 3600 seconds is a sensible default. Lower it (to 300, say) a day before a planned change and
  raise it again after.
- To revoke: remove the record or entry. Verifiers SHOULD re-check at least daily.
- Verifiers that keep history SHOULD remember the addresses a domain has proven (trust on first use, like
  SSH host keys) and hold a newly listed address for a while before it counts (Settle Watch: 72 hours),
  trusting the earlier ones meanwhile and alerting anyone watching the seller. This applies to DNS and the
  file alike. A planned rotation passes untouched: list the new address next to the old one, wait out the
  hold, switch `payTo`, then remove the old one.
- A domain's first proof has no earlier proof to compare against, but a monitor may still have the
  addresses the endpoint quoted before it. If the first proof names an address the endpoint wasn't already
  asking for, Settle Watch holds it the same way (a hijacker could swap the address and publish a "first"
  proof in one move). A first proof for the address already in use, or from a host with no history, counts
  at once. A proof binds an address to whoever runs the domain; it says nothing about whether that party is
  honest, which is what the rest of the board's record is for.
- There is no expiry field: a stale proof is removed, not timed out.

## 8. Security considerations

- **Server or deployment compromise:** defeated by DNS proofs; the well-known file is only as strong as
  the web server.
- **DNS or registrar compromise:** someone who takes over the domain can publish their own record and sign
  it with their own wallet, so a signature alone doesn't stop them; it proves the listed wallet agreed, not
  that it's the same wallet as last week. What catches it is continuity: a verifier that remembers the
  domain's earlier addresses sees a new one appear and holds it (section 7), and watchers are alerted while
  the board still trusts the old address. Clients without history (a fresh process) can't tell, which is
  one reason to check against a monitor that keeps it. Registrar lock and 2FA on the registrar account
  remain the seller's first defense.
- **Server compromise plus a self-signed file:** an attacker who controls the web server can publish their
  own address in the well-known file, signed by their own wallet. That's why DNS takes precedence (section 6,
  step 1): with a DNS proof in place, a file-only address is a mismatch, not a match. A seller with no DNS
  proof has no such anchor, so the best a verifier can do is notice the change: hold the new file address
  (section 7) and alert the people watching that seller. Publishing a DNS record is the real fix, and it
  takes effect for that seller on the next check, with no migration step.
- **Where the signing key lives:** the signature comes from the payout wallet itself, not from a key on the
  API server. Receiving x402 payments never needs that key (the facilitator settles to the address), so it
  belongs off the request path entirely: cold storage, a hardware wallet or a multisig. Signing a proof is a
  one-time, offline step, repeated only when rotating.
- **Replay across domains:** the signed message names the domain; a signature for `a.com` does not
  verify for `b.com`.
- **Lookalike domains:** proofs bind an address to a domain, not to a brand. Clients still need to call
  the domain they meant to call.
- **Privacy:** proofs publish an association between a domain and an address. That association is
  already public in every 402 response.
- **Resolver trust:** verifiers SHOULD use DNSSEC-validating resolvers or DNS-over-HTTPS to a resolver
  they trust.

## 9. Making a proof (seller how-to)

1. Sign the message in section 4 with the payout wallet (any wallet that supports `personal_sign`).
2. Add the DNS record at your DNS provider, or publish the file.
3. Check it with a verifier, for example Settle Watch's
   [verify page](https://settlewatch.com/verify.html).

### 9.1 Implementations

Verifiers:

- [x402-trust-guard](https://www.npmjs.com/package/x402-trust-guard) and the Settle Watch check API
  (reference implementation; sections 6, 6.1 and 6.2).
- [teppi-check](https://www.npmjs.com/package/teppi-check) 0.6.0 and later (`--payout-proof`): reports the
  section 6.2 outcome per payTo, beside its own checks. Contract-wallet signatures read as `unverifiable`
  for now.
- The Settle Watch board, which also keeps the history behind section 7's hold.

Sellers publishing a DNS proof: `api.settlewatch.com` (signed), `api.teppi.xyz`.

## 10. Relation to the Offer and Receipt extension

The [Offer and Receipt extension](https://github.com/x402-foundation/x402/blob/main/specs/extensions/extension-offer-and-receipt.md)
binds a server's **signing key** to its domain (`_controllers.<domain>` TXT, or `did:web`) so offers and receipts
can be verified. That proves the terms came from the server's key, not that the **payout address** in them
belongs to the domain's owner: a server whose key is in use by an attacker can still sign an offer naming the
attacker's address. Payout proofs bind the address itself, with the wallet's own signature. The two compose: a
verifier can require both. If preferred, the DNS form here could be expressed in the same `_controllers` syntax
(`controller=did:pkh:eip155:8453:<payTo>`) with a marker for payout use.

## 11. Open questions

- Should `PaymentRequirements.extra` carry a hint (e.g. `"payToProof": "dns"`) so clients know to look?
- Should facilitators check proofs at `/verify` and refuse a mismatch?
- A proof form for per-payment addresses (processor or factory signing on behalf of the seller).
- Registering `_x402-payto` in the IANA underscored DNS node names registry (RFC 8552).
