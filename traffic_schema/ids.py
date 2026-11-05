"""Deterministic identifiers and run metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any


def _stable_hash(*parts: object, length: int = 24) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def source_text_sha256(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def source_id(input_path: Path | str, source_text: str) -> str:
    """Remain stable for the same named, unchanged source notice."""
    name = Path(input_path).name.casefold()
    return "src_" + _stable_hash(name, source_text_sha256(source_text))


def extraction_id(
    stable_source_id: str,
    extraction_class: str,
    char_start: int | None,
    char_end: int | None,
) -> str:
    return "ext_" + _stable_hash(
        stable_source_id,
        extraction_class,
        char_start if char_start is not None else "",
        char_end if char_end is not None else "",
    )


def evidence_id(
    stable_source_id: str,
    char_start: int | None,
    char_end: int | None,
) -> str:
    return "evd_" + _stable_hash(
        stable_source_id,
        char_start if char_start is not None else "",
        char_end if char_end is not None else "",
    )


def build_run_metadata(
    *,
    schema_version: str,
    prompt_version: str,
    model_name: str,
    ollama_url: str,
    input_path: Path,
    source_text: str,
    run_timestamp: str | None = None,
) -> dict[str, Any]:
    stable_source_id = source_id(input_path, source_text)
    return {
        "schema_version": schema_version,
        "prompt_version": prompt_version,
        "model_name": model_name,
        "ollama_url": ollama_url,
        "run_timestamp": run_timestamp
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_text_sha256": source_text_sha256(source_text),
        "source_id": stable_source_id,
    }
