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

1. Collect proofs for `H` (sections 3.1 and 3.2). Cache for at most 24 hours.
2. If no proofs exist: proceed under the client's normal policy. Absence proves nothing.
3. If `P` is in the proofs: proceed. Clients MAY show the level.
4. If proofs exist and `P` is **not** among them: this is a **payout mismatch**. Clients SHOULD NOT pay
   without explicit confirmation. Catalogs and monitors SHOULD flag the listing.

Step 4 is the point of this spec: once a seller publishes a proof, a hijacked endpoint can no longer
silently redirect payments.

Exceptions verifiers should handle:

- **Per-payment addresses.** Some sellers quote a fresh address every time (deposit-address or payment
  processor flows). A mismatch is meaningless for them; verifiers SHOULD only flag a mismatch when the
  quoted address is stable across checks. A future version may define a proof for an address
  *factory* or processor.
- **Weaker hints.** Addresses mentioned in other files (e.g. a general service manifest at
  `/.well-known/x402`) MAY confirm a match but MUST NOT, on their own, cause a mismatch.

## 7. Rotation and revocation

- To rotate: publish the new address (new record or entry), switch `payTo`, then remove the old one.
- To revoke: remove the record or entry. Verifiers SHOULD re-check at least daily.
- There is no expiry field: a stale proof is removed, not timed out.

## 8. Security considerations

- **Server or deployment compromise:** defeated by DNS proofs; the well-known file is only as strong as
  the web server.
- **DNS compromise:** defeated by requiring a signature (`dns_signed`), since the attacker would also
  need the payout wallet's key, at which point they already control the funds.
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
