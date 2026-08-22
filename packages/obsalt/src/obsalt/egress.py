"""HTTPS-only egress with SSRF protections shared by backfill, judges, OTLP dests, webhooks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


class EgressDenied(Exception):
    pass


def validate_destination(url: str, *, allow_http_localhost: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise EgressDenied(f"scheme {parsed.scheme!r} is not allowed")
    if parsed.scheme == "http" and not allow_http_localhost:
        raise EgressDenied("HTTP destinations are not allowed")
    host = parsed.hostname or ""
    if not host:
        raise EgressDenied("missing host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressDenied(f"DNS resolution failed for {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _BLOCKED) and not allow_http_localhost:
            raise EgressDenied(f"destination resolves to blocked address {ip}")
