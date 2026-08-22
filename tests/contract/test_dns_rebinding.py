"""Redirects re-resolve DNS so a public host cannot hop onto a private address."""

from __future__ import annotations

import socket

import pytest

from obsalt.egress import EgressDenied, validate_redirect


def test_dns_rebinding_on_redirect_is_blocked(monkeypatch) -> None:
    answers = {
        "public.example.test": "203.0.113.10",
        "rebind.example.test": "169.254.169.254",
    }

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in answers:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(EgressDenied):
        validate_redirect("https://public.example.test/hooks", "https://rebind.example.test/meta")
