"""Owner notes: what a seller has told you about their own endpoints.

Stored in ~/x402-trust-data/owner-notes.json so upgrades never overwrite them. Notes can
also ship with the code in managed/owner-notes.json (kept in the repo, updated along with
it); a local note for the same match wins.

  retired   the owner took these endpoints down on purpose. They stop counting as
            dead or flaky, show as "Retired by owner", and get no grade.
  intended  the owner confirmed the terms are deliberate (a price that depends on
            the request, a separate payment wallet). Price and wallet warnings are
            dropped and the board shows "Owner confirmed".

A note matches a host (api.example.com), or any URL that starts with a given prefix
(https://api.example.com/v1/). With --when-http, it only applies to endpoints whose
latest check returned one of those status codes (e.g. 410 for retired routes).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config

STATUSES = ("retired", "intended")


def path():
    return config.DATA_DIR / "owner-notes.json"


def managed_path():
    return Path(__file__).resolve().parent.parent / "managed" / "owner-notes.json"


def _read(p):
    try:
        data = json.loads(p.read_text())
        return [n for n in data if isinstance(n, dict) and n.get("match") and n.get("status") in STATUSES] \
            if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def load_local():
    return _read(path())


def load():
    """Local notes first (they win), then the ones that ship with the code."""
    local = load_local()
    seen = {(n["match"], tuple(n.get("when_http") or ())) for n in local}
    managed = [n for n in _read(managed_path())
               if (n["match"], tuple(n.get("when_http") or ())) not in seen]
    return local + managed


def save(notes):
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(notes, indent=1))


def add(match, status, text, when_http=None):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    notes = [n for n in load_local() if not (n["match"] == match and n.get("when_http") == when_http)]
    notes.append({
        "match": match, "status": status, "note": text,
        "when_http": sorted(when_http) if when_http else None,
        "added": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    save(notes)
    return notes[-1]


def remove(match):
    notes = load_local()
    kept = [n for n in notes if n["match"] != match]
    save(kept)
    return len(notes) - len(kept)


def matches(match, resource):
    if "://" in match:
        return resource.startswith(match)
    return (urlsplit(resource).hostname or "").lower() == match.lower()


def find(notes, resource, probes=()):
    """The note that applies to this endpoint, or None.

    `probes` are this endpoint's checks in time order; only needed for --when-http notes.
    """
    for n in notes:
        if not matches(n["match"], resource):
            continue
        codes = n.get("when_http")
        if codes:
            last = next((p["status_code"] for p in reversed(probes) if p["status_code"]), None)
            if last not in codes:
                continue
        return n
    return None
