"""Shared egress policy for backfill, judges, embedders, OTLP, recordings, webhooks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("169.254.169.254/32"),
)


class EgressDenied(ValueError):
    pass


def validate_destination(url: str, *, allow_http: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ({"https"} if not allow_http else {"https", "http"}):
        raise EgressDenied("only https destinations are allowed")
    if parsed.port and parsed.port not in {80, 443, 4317, 4318}:
        if parsed.scheme == "https" and parsed.port == 443:
            pass
        elif parsed.port not in {4317, 4318}:
            raise EgressDenied(f"port {parsed.port} is not allowed")
    host = parsed.hostname
    if not host:
        raise EgressDenied("destination host is required")
    if host in {"localhost", "metadata.google.internal"}:
        raise EgressDenied("blocked host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443)
    except socket.gaierror as exc:
        raise EgressDenied(f"dns resolution failed: {exc}") from exc
    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        for network in _BLOCKED:
            if ip in network:
                raise EgressDenied(f"blocked address {addr}")
    return url
