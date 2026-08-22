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

_LOOPBACK = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)

_METADATA = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


class EgressDenied(Exception):
    pass


def _canonical_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_loopback(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = _canonical_ip(ip)
    return ip.is_loopback or any(ip in net for net in _LOOPBACK)


def _is_metadata(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = _canonical_ip(ip)
    return any(ip in net for net in _METADATA)


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = _canonical_ip(ip)
    return any(ip in net for net in _BLOCKED)


def validate_destination(url: str, *, allow_http_localhost: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise EgressDenied(f"scheme {parsed.scheme!r} is not allowed")
    host = parsed.hostname or ""
    if not host:
        raise EgressDenied("missing host")
    try:
        infos = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise EgressDenied(f"DNS resolution failed for {host}") from exc
    ips = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not ips:
        raise EgressDenied(f"DNS resolution failed for {host}")
    for ip in ips:
        if _is_metadata(ip):
            raise EgressDenied(f"destination resolves to blocked address {ip}")
        if _is_blocked(ip) and not (allow_http_localhost and _is_loopback(ip)):
            raise EgressDenied(f"destination resolves to blocked address {ip}")
    if parsed.scheme == "http":
        if not allow_http_localhost:
            raise EgressDenied("HTTP destinations are not allowed")
        if not all(_is_loopback(ip) for ip in ips):
            raise EgressDenied("HTTP is only allowed for localhost")
