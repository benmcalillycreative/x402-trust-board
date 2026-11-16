"""Verified payout addresses: proof that a seller's pay-to address is really theirs.

A listing can name any address. Nothing in x402 or the Bazaar proves that the
wallet getting paid belongs to whoever runs the endpoint. This module checks two
proofs a seller can publish, both optional and free:

1. Their own site lists the address, in a small file at
       https://<host>/.well-known/x402-payto.json
   or their DNS does, in a TXT record at
       _x402-payto.<domain>
   That shows whoever controls the site (or the domain) chose that address.

2. The wallet itself signs a short message naming the domain:

       x402 payout verification

       Domain: example.com
       Pay-to: 0xabc…   (lowercase)
       Issued: 2026-11-04

   That shows whoever holds the wallet agrees. Ordinary wallets are checked by
   recovering the signer from the signature; smart-contract wallets (for example
   Coinbase Smart Wallet) through an ERC-1271 call on Base.

Both together mean the site and the wallet belong to the same party. DNS is the
stronger of the two, because someone who breaks into a web server usually can't
change the domain's DNS.

File format (one file per host):
    {"version": 1, "domain": "api.example.com",
     "payTo": [{"address": "0x…", "issued": "2026-11-04", "signature": "0x…"}]}

DNS format (one TXT record per address; covers the domain and its subdomains):
    _x402-payto.example.com  TXT  "v=x402payto1; a=0x…; d=2026-11-04; s=0x…"

The signature is optional. Without it the address counts as "declared by the
site" but not verified. Each host is checked at most once a day.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import config, db, netguard
from .ethsig import hexbytes, keccak256, personal_hash, recover_address
from .parse import norm_addr
from .probe import NoRedirect

WELL_KNOWN = "/.well-known/x402-payto.json"
DNS_LABEL = "_x402-payto."
MESSAGE = "x402 payout verification\n\nDomain: {domain}\nPay-to: {address}\nIssued: {issued}"
ERC6492_SUFFIX = bytes.fromhex("64926492" * 8)

# Hosting platforms where many unrelated people share one parent domain. A DNS
# record on a parent like onrender.com proves nothing about a customer's subdomain.
SHARED_SUFFIXES = {
    "workers.dev", "pages.dev", "onrender.com", "vercel.app", "netlify.app", "fly.dev",
    "up.railway.app", "railway.app", "herokuapp.com", "github.io", "hf.space", "sslip.io",
    "nip.io", "run.app", "appspot.com", "amazonaws.com", "cloudfront.net", "azurewebsites.net",
    "duckdns.org", "ngrok.app", "ngrok-free.app", "replit.app", "repl.co", "deno.dev",
    "supabase.co", "firebaseapp.com", "web.app", "elestio.app", "base44.app",
}
# Country-style suffixes: stop before them when walking up to parent domains.
TWO_PART_SUFFIXES = {"co.uk", "org.uk", "com.au", "net.au", "co.jp", "com.br", "co.nz", "co.in", "com.mx"}

LEVELS = {"dns_signed": 4, "site_signed": 3, "dns": 2, "site": 1, "manifest": 0}
MANIFEST = "/.well-known/x402"  # the service manifest many x402 sellers already publish

SCHEMA = """
CREATE TABLE IF NOT EXISTS payout_checks (
    host        TEXT PRIMARY KEY,
    checked_at  TEXT NOT NULL,
    summary     TEXT                -- what was found, for `payout HOST` and debugging
);
CREATE TABLE IF NOT EXISTS payout_proofs (
    host        TEXT NOT NULL,      -- the endpoint host this proof applies to
    source      TEXT NOT NULL,      -- site | dns
    domain      TEXT NOT NULL,      -- the domain named in the proof
    address     TEXT NOT NULL,      -- normalized (EVM lowercase)
    issued      TEXT,
    signer      TEXT NOT NULL,      -- wallet | contract | unsigned | invalid | unverifiable
    detail      TEXT,
    PRIMARY KEY (host, source, address)
);
"""


# ---------- proof checking ----------

def message(domain, address, issued):
    return MESSAGE.format(domain=domain.lower(), address=address.lower(), issued=issued)


def _valid_date(issued):
    try:
        d = datetime.strptime(issued, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return d <= datetime.now(timezone.utc) + timedelta(days=1)


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(config.BASE_RPC_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=config.PROBE_TIMEOUT) as resp:
        out = json.loads(resp.read(65536))
    if "error" in out:
        raise RuntimeError(out["error"])
    return out.get("result")


def _erc1271_valid(address, msg_hash, signature):
    """Ask a smart-contract wallet on Base whether it signed this hash."""
    if _rpc("eth_getCode", [address, "latest"]) in (None, "0x", "0x0"):
        return False
    sig = signature
    padded = sig + b"\x00" * (-len(sig) % 32)
    data = ("0x1626ba7e" + msg_hash.hex() + (64).to_bytes(32, "big").hex()
            + len(sig).to_bytes(32, "big").hex() + padded.hex())
    result = _rpc("eth_call", [{"to": address, "data": data}, "latest"]) or ""
    return result.lower().startswith("0x1626ba7e")


def check_signature(domain, address, issued, signature):
    """(signer, detail): wallet / contract when the signature checks out."""
    if not signature:
        return "unsigned", ""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address or ""):
        return "unverifiable", "only Ethereum-style (0x…) addresses can be signature-checked so far"
    if not _valid_date(issued):
        return "invalid", "issued date missing, malformed, or in the future"
    sig = hexbytes(signature)
    if not sig:
        return "invalid", "signature is not hex"
    msg_hash = personal_hash(message(domain, address, issued))
    if len(sig) == 65 and recover_address(msg_hash, sig) == address.lower():
        return "wallet", ""
    if sig.endswith(ERC6492_SUFFIX):
        return "unverifiable", "smart wallet not deployed yet (ERC-6492); deploy it or sign with a regular wallet"
    try:
        if _erc1271_valid(address, msg_hash, sig):
            return "contract", ""
    except Exception as exc:  # RPC trouble: try again tomorrow rather than call it invalid
        return "unverifiable", f"couldn't reach Base to check a smart-wallet signature: {exc}"[:200]
    return "invalid", "signature doesn't match this address and message"


# ---------- reading the proofs ----------

def parse_file(text, host):
    """Entries from a .well-known file. Returns (entries, problem)."""
    try:
        data = json.loads(text)
    except ValueError:
        return [], "file isn't valid JSON"
    if not isinstance(data, dict) or not isinstance(data.get("payTo"), list):
        return [], 'file needs a "payTo" list'
    domain = str(data.get("domain") or host).lower()
    if domain != host:
        return [], f'file says domain "{domain}" but was served from {host}'
    out = []
    for item in data["payTo"][:20]:
        if isinstance(item, str):
            item = {"address": item}
        if isinstance(item, dict) and item.get("address"):
            out.append((domain, str(item["address"]), item.get("issued"), item.get("signature")))
    return out, "" if out else "no addresses listed"


def parse_manifest(text):
    """Pay-to addresses named in a site's own x402 service manifest (/.well-known/x402).

    Weaker than the payout file: it's unsigned and not made for this purpose, so it can
    confirm an address the endpoint asks for, but never on its own make a mismatch."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    found = set()

    def walk(node, depth=0):
        if depth > 6 or len(found) > 20:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("payTo", "pay_to") and isinstance(v, str):
                    found.add(v)
                elif k == "payToByNetwork" and isinstance(v, dict):
                    found.update(x for x in v.values() if isinstance(x, str))
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:200]:
                walk(v, depth + 1)

    walk(data)
    return sorted(a for a in found if re.fullmatch(r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}", a))


def parse_txt(records, domain):
    out = []
    for rec in records:
        fields = {}
        for part in rec.split(";"):
            k, _, v = part.strip().partition("=")
            fields[k.strip().lower()] = v.strip()
        if fields.get("v") == "x402payto1" and fields.get("a"):
            out.append((domain, fields["a"], fields.get("d"), fields.get("s")))
    return out


def dns_names(host):
    """The host and its parents that a DNS proof could sit on.

    Empty for shared hosting (a DNS record on onrender.com says nothing about one
    customer's subdomain); those sellers use the .well-known file instead."""
    h = host.lower().rstrip(".")
    if re.fullmatch(r"[0-9.]+|\[?[0-9a-f:]+\]?", h) or "." not in h:
        return []  # an IP address or bare name has no DNS of its own to check
    if any(h == s or h.endswith("." + s) for s in SHARED_SUFFIXES):
        return []
    labels = h.split(".")
    names = []
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        if name in TWO_PART_SUFFIXES:
            break
        names.append(name)
    return names


_opener = urllib.request.build_opener(NoRedirect)


def fetch_file(host, path=WELL_KNOWN):
    url = f"https://{host}{path}"
    try:
        netguard.check_url(url)
    except netguard.Blocked as exc:
        return None, str(exc)
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with _opener.open(req, timeout=config.PROBE_TIMEOUT) as resp:
            return resp.read(65536).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:
        return None, str(exc)[:120]


def fetch_txt(name):
    """TXT records for a name, via DNS-over-HTTPS (the standard library has no DNS client)."""
    url = config.DOH_URL + "?" + urllib.parse.urlencode({"name": name, "type": "TXT"})
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json",
                                               "User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=config.PROBE_TIMEOUT) as resp:
        data = json.loads(resp.read(65536))
    out = []
    for ans in data.get("Answer") or []:
        if ans.get("type") == 16:
            out.append("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', ans.get("data", ""))) or ans.get("data", ""))
    return out


def check_host(host):
    """Look for both kinds of proof for one host. Returns (proofs, summary)."""
    proofs, notes = [], []
    text, problem = fetch_file(host)
    if text is not None:
        entries, problem = parse_file(text, host)
        for e in entries:
            proofs.append(("site",) + e)
    notes.append(f"site file: {problem or 'found'}")
    text, problem = fetch_file(host, MANIFEST)
    named = parse_manifest(text) if text else []
    if named:
        notes.append(f"x402 manifest: {len(named)} address(es)")
        proofs += [("manifest", host, a, None, None) for a in named]
    for name in dns_names(host):
        try:
            records = fetch_txt(DNS_LABEL + name)
        except Exception as exc:
            notes.append(f"dns {name}: lookup failed ({str(exc)[:60]})")
            continue
        entries = parse_txt(records, name)
        if entries:
            notes.append(f"dns {name}: {len(entries)} record(s)")
            proofs += [("dns",) + e for e in entries]
    checked = []
    for source, domain, address, issued, signature in proofs:
        signer, detail = check_signature(domain, address, issued, signature)
        checked.append({"source": source, "domain": domain, "address": norm_addr(address),
                        "issued": issued, "signer": signer, "detail": detail})
    return checked, "; ".join(notes)


# ---------- the cycle step ----------

def due_hosts(conn):
    """Hosts that quoted a pay-to address recently and haven't been checked today."""
    since = (datetime.now(timezone.utc) - timedelta(days=config.SCORE_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT DISTINCT e.host FROM probes p JOIN endpoints e USING (resource)
           WHERE p.ts >= ? AND p.quoted_pay_to IS NOT NULL AND e.host IS NOT NULL
             AND e.host NOT IN (SELECT host FROM payout_checks WHERE checked_at >= ?)""",
        (since, cutoff)).fetchall()
    return [r[0].split(":")[0] for r in rows]


def save(conn, host, proofs, summary):
    conn.execute("DELETE FROM payout_proofs WHERE host=?", (host,))
    for p in proofs:
        conn.execute("INSERT OR REPLACE INTO payout_proofs VALUES (?,?,?,?,?,?,?)",
                     (host, p["source"], p["domain"], p["address"], p["issued"], p["signer"], p["detail"]))
    conn.execute("INSERT OR REPLACE INTO payout_checks VALUES (?,?,?)", (host, db.now(), summary))


def run(conn=None, hosts=None, log=print):
    conn = conn or db.connect()
    conn.executescript(SCHEMA)
    hosts = hosts or due_hosts(conn)
    if not hosts:
        return 0
    log(f"Checking payout proofs for {len(hosts):,} hosts")
    found = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for host, (proofs, summary) in zip(hosts, pool.map(check_host, hosts)):
            save(conn, host, proofs, summary)
            found += bool(proofs)
    conn.commit()
    log(f"Payout proofs: {found:,} hosts publish one")
    return found


def load(conn):
    """{host: {address: level}}, keeping the strongest proof per address."""
    conn.executescript(SCHEMA)
    out = {}
    for r in conn.execute("SELECT host, source, address, signer FROM payout_proofs"):
        if r[3] == "invalid":
            continue  # a broken signature proves nothing either way; don't count the claim
        signed = r[3] in ("wallet", "contract")
        if r[1] == "manifest":
            level = "manifest"
        else:
            level = ("dns" if r[1] == "dns" else "site") + ("_signed" if signed else "")
        cur = out.setdefault(r[0], {}).get(r[2])
        if cur is None or LEVELS[level] > LEVELS[cur]:
            out[r[0]][r[2]] = level
    return out


def describe(conn, host):
    """Human-readable result for one host (for the `payout HOST` command)."""
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT checked_at, summary FROM payout_checks WHERE host=?", (host,)).fetchone()
    lines = [f"{host}: " + (f"checked {row[0]} ({row[1]})" if row else "not checked yet")]
    for r in conn.execute("SELECT source, domain, address, issued, signer, detail FROM payout_proofs WHERE host=?",
                          (host,)):
        verdict = {"wallet": "VERIFIED (wallet signature)", "contract": "VERIFIED (smart-wallet signature)",
                   "unsigned": "declared, not signed", "invalid": "signature INVALID",
                   "unverifiable": "can't check signature"}[r[4]]
        lines.append(f"  {r[0]:<8} {r[2]}  {verdict}" + (f": {r[5]}" if r[5] else ""))
    return "\n".join(lines)
