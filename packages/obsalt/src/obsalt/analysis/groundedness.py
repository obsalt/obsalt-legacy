"""Optional local encoder. Torch is imported inside functions, never at module load."""

from __future__ import annotations

import logging
import multiprocessing
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from obsalt.analysis.hallucination import grounding_corpus
from obsalt.domain.enums import AnalysisState
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.util import sha256_text

log = logging.getLogger("obsalt.analysis.groundedness")

ANALYZER_ID = "groundedness"
ANALYZER_VERSION = "1"
DEFAULT_MODEL_ID = "KRLabsOrg/lettucedect-v2-mmbert-base"
# Published pair packing (context, answer). v2 mmBERT-base RAGTruth example-F1 74.3.
# Hub commit for KRLabsOrg/lettucedect-v2-mmbert-base (fetched 2026-08-30).
MODEL_REVISION = "8831ce063dff760b01efe4519b4188f5ee2456f2"
ALLOWLIST_PREFIX = "KRLabsOrg/lettucedect-"

PredictTurn = Callable[[str, str], list[dict[str, Any]]]

_LOGGED_STATUS = False


def groundedness_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def model_allowed(model_id: str) -> bool:
    raw = str(model_id or "").strip()
    if not raw:
        return False
    if raw.startswith(ALLOWLIST_PREFIX):
        return True
    if raw.startswith("/") or raw.startswith("./") or raw.startswith("../"):
        return True
    return False


def maybe_run_groundedness(revision: CallRevision, state: Any) -> AnalysisResult | None:
    """Sampled encoder row, or None when disabled / not drawn."""
    from obsalt.analysis.runners import org_groundedness_enabled, org_groundedness_sample_rate

    settings = getattr(state, "settings", None)
    _log_status(settings)
    if not org_groundedness_enabled(state, revision.org_id):
        return None
    rate = org_groundedness_sample_rate(state, revision.org_id)
    if not _sampled(revision.call_id, rate):
        return None
    return run_groundedness(revision, settings=settings)


def run_groundedness(
    call: CallRevision,
    *,
    settings: Any | None = None,
    predict_turn: PredictTurn | None = None,
) -> AnalysisResult:
    """Never raises into promote. Missing extra/weights → not_judged."""
    model_id = str(getattr(settings, "groundedness_model", None) or DEFAULT_MODEL_ID)
    timeout = float(getattr(settings, "groundedness_turn_timeout_seconds", 8.0) or 8.0)
    cache_dir = getattr(settings, "groundedness_cache_dir", None)
    if not model_allowed(model_id):
        return _result(
            call, status="not_judged", error="model id is not allowlisted", model=model_id
        )
    if predict_turn is None and not groundedness_available():
        return _result(call, status="not_judged", error="extra not installed", model=model_id)
    context = "\n".join(grounding_corpus(call))
    spans: list[dict[str, Any]] = []
    predict = predict_turn or (
        lambda ctx, answer: _predict_turn_killable(
            ctx, answer, model_id=model_id, timeout=timeout, cache_dir=cache_dir
        )
    )
    try:
        for turn in call.agent_turns():
            if not turn.text:
                continue
            started = time.perf_counter()
            found = predict(context, turn.text)
            _observe_seconds(time.perf_counter() - started)
            for item in found:
                start = int(item.get("start") or 0)
                end = int(item.get("end") or 0)
                if start < 0 or end > len(turn.text) or end <= start:
                    continue
                spans.append(
                    {
                        "turn_index": turn.index,
                        "start": start,
                        "end": end,
                        "text": turn.text[start:end],
                        "confidence": float(item.get("confidence") or item.get("score") or 0.0),
                    }
                )
        _observe_run("completed")
        return _result(call, status="completed", spans=spans, model=model_id)
    except TimeoutError as exc:
        _observe_run("timeout")
        return _result(call, status="not_judged", error=str(exc) or "timeout", model=model_id)
    except ImportError as exc:
        _observe_run("import_error")
        return _result(call, status="not_judged", error=str(exc), model=model_id)
    except Exception as exc:  # noqa: BLE001 — never fail promote
        log.exception("groundedness failed for %s", call.call_id)
        _observe_run("not_judged")
        return _result(call, status="not_judged", error=str(exc), model=model_id)


def _sampled(call_id: str, rate: float) -> bool:
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = int(sha256_text(call_id)[:8], 16) / 0xFFFFFFFF
    return digest < rate


def _predict_turn_killable(
    context: str,
    answer: str,
    *,
    model_id: str,
    timeout: float,
    cache_dir: str | None,
) -> list[dict[str, Any]]:
    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[tuple[str, Any]] = ctx.Queue()
    proc = ctx.Process(
        target=_child_entry,
        args=(
            {"context": context, "answer": answer, "model_id": model_id, "cache_dir": cache_dir},
            queue,
        ),
    )
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(1)
        raise TimeoutError(f"groundedness turn exceeded {timeout}s")
    if queue.empty():
        raise TimeoutError("groundedness child exited without a result")
    status, value = queue.get()
    if status != "ok":
        raise RuntimeError(str(value))
    return list(value)


def _child_entry(payload: dict[str, Any], queue: Any) -> None:
    try:
        queue.put(("ok", _predict_turn_local(**payload)))
    except Exception as exc:  # noqa: BLE001
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def _predict_turn_local(
    context: str,
    answer: str,
    model_id: str,
    cache_dir: str | None = None,
) -> list[dict[str, Any]]:
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    kwargs: dict[str, Any] = {"trust_remote_code": False, "revision": MODEL_REVISION}
    if cache_dir:
        kwargs["cache_dir"] = cache_dir
    if model_id.startswith("/") or model_id.startswith("./"):
        kwargs["local_files_only"] = True
    tokenizer = AutoTokenizer.from_pretrained(model_id, **kwargs)
    model = AutoModelForTokenClassification.from_pretrained(model_id, **kwargs)
    model.eval()
    encoded = tokenizer(
        context,
        answer,
        return_tensors="pt",
        return_offsets_mapping=True,
        return_token_type_ids=True,
        truncation=True,
        max_length=min(int(getattr(tokenizer, "model_max_length", 512) or 512), 4096),
    )
    offsets = encoded.pop("offset_mapping")[0].tolist()
    token_type = encoded.get("token_type_ids")
    type_ids = token_type[0].tolist() if token_type is not None else [1] * len(offsets)
    with torch.no_grad():
        logits = model(**encoded).logits[0]
    probs = torch.softmax(logits, dim=-1)
    pred = torch.argmax(logits, dim=-1).tolist()
    hallu: list[tuple[int, int, float]] = []
    for index, label in enumerate(pred):
        if index >= len(offsets) or index >= len(type_ids):
            break
        if type_ids[index] != 1 or label == 0:
            continue
        start, end = offsets[index]
        if end <= start:
            continue
        score = float(probs[index][label])
        hallu.append((int(start), int(end), score))
    return _merge_spans(hallu)


def _merge_spans(items: Sequence[tuple[int, int, float]]) -> list[dict[str, Any]]:
    if not items:
        return []
    ordered = sorted(items)
    start, end, score = ordered[0]
    n = 1
    out: list[dict[str, Any]] = []
    for next_start, next_end, next_score in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
            score += next_score
            n += 1
            continue
        out.append({"start": start, "end": end, "confidence": score / n})
        start, end, score, n = next_start, next_end, next_score, 1
    out.append({"start": start, "end": end, "confidence": score / n})
    return out


def _result(
    call: CallRevision,
    *,
    status: str,
    spans: Sequence[Mapping[str, Any]] | None = None,
    error: str = "",
    model: str = DEFAULT_MODEL_ID,
) -> AnalysisResult:
    payload: dict[str, Any] = {
        "backend": "lettucedetect",
        "model": model,
        "status": status,
        "spans": [dict(item) for item in spans or []],
        "selection": "local_encoder",
    }
    execution = AnalysisExecution(
        call_id=call.call_id,
        revision=call.revision,
        analyzer_id=ANALYZER_ID,
        analyzer_version=ANALYZER_VERSION,
        state=AnalysisState.COMPLETED,
        content_hash=sha256_text(call.revision + ANALYZER_ID),
        error=error or None,
    )
    return AnalysisResult(execution=execution, payload=payload)


def _log_status(settings: Any | None) -> None:
    global _LOGGED_STATUS
    if _LOGGED_STATUS:
        return
    _LOGGED_STATUS = True
    extra = "yes" if groundedness_available() else "no"
    enabled = bool(getattr(settings, "groundedness_enabled", False))
    rate = getattr(settings, "groundedness_sample_rate", 0)
    model = getattr(settings, "groundedness_model", DEFAULT_MODEL_ID)
    log.info(
        "groundedness extra=%s enabled=%s sample_rate=%s model=%s", extra, enabled, rate, model
    )


def _observe_run(status: str) -> None:
    try:
        from obsalt.metrics import groundedness_runs_total

        groundedness_runs_total.labels(status=status).inc()
    except Exception:
        return


def _observe_seconds(value: float) -> None:
    if value <= 0:
        return
    try:
        from obsalt.metrics import groundedness_seconds

        groundedness_seconds.observe(value)
    except Exception:
        return
