"""OTLP tenant assertions corroborate an authenticated org. They never choose one."""

from __future__ import annotations

from collections.abc import Sequence

from obsalt.plugin.types import ReadableSpan


def tenant_assertions(spans: Sequence[ReadableSpan]) -> set[str]:
    """Collect ``obsalt.org`` values. ``service.namespace`` corroborates when paired."""

    orgs: set[str] = set()
    namespaces: set[str] = set()
    for span in spans:
        for bag in (span.resource or {}, span.attributes or {}):
            org = bag.get("obsalt.org")
            if org not in (None, ""):
                orgs.add(str(org))
            ns = bag.get("service.namespace")
            if ns not in (None, ""):
                namespaces.add(str(ns))
    if orgs and namespaces and orgs != namespaces:
        return orgs | namespaces
    return orgs


def reject_tenant_assertions(spans: Sequence[ReadableSpan], authenticated_org: str) -> str | None:
    found = tenant_assertions(spans)
    if not found:
        return None
    if len(found) > 1:
        return "mixed-org assertions in one batch"
    only = next(iter(found))
    if only != authenticated_org:
        return "resource attribute cannot choose an organization"
    return None
