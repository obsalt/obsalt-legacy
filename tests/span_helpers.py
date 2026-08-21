from __future__ import annotations

from collections import defaultdict

from opentelemetry.sdk.trace.export import ReadableSpan


def span_forest(spans: list[ReadableSpan]) -> dict[str, list[str]]:
    """Map parent span name -> child names (stable for assertions)."""
    by_id = {span.context.span_id: span for span in spans if span.context}
    children: dict[int, list[ReadableSpan]] = defaultdict(list)
    roots: list[ReadableSpan] = []
    for span in spans:
        parent_id = span.parent.span_id if span.parent else None
        if parent_id and parent_id in by_id:
            children[parent_id].append(span)
        else:
            roots.append(span)
    tree: dict[str, list[str]] = {}
    for span in spans:
        tree[span.name] = [child.name for child in children.get(span.context.span_id, [])]
    tree["__roots__"] = [r.name for r in roots]
    return tree


def attrs(span: ReadableSpan) -> dict:
    return dict(span.attributes or {})
