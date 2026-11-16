"""Refuse to probe addresses on private networks.

Listings are written by strangers. Without this, a listing pointing at
http://192.168.1.1 would make your machine poke at your own router.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from . import config


class Blocked(Exception):
    pass


def check_url(url: str) -> str:
    """Raise Blocked if the URL is not a public http(s) address. Returns the host."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Blocked(f"scheme {parts.scheme!r} not allowed")
    host = parts.hostname
    if not host:
        raise Blocked("no host")
    if config.ALLOW_PRIVATE:
        return host
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise Blocked(f"dns: {exc}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise Blocked(f"{host} resolves to non-public address {ip}")
    return host
