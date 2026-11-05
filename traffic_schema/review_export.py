"""Human review queue generation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .ids import evidence_id, extraction_id


REVIEW_FIELDNAMES = (
    "event_id",
    "source_id",
    "file_name",
    "extraction_id",
    "extraction_class",
    "extraction_text",
    "attributes_json",
    "char_start",
    "char_end",
    "grounding_status",
    "validation_status",
    "rejection_reasons",
    "coverage_status",
    "review_status",
    "is_correct",
    "corrected_class",
    "corrected_text",
    "corrected_attributes",
    "review_note",
)


def _base_row(
    *,
    event_id_value: str,
    stable_source_id: str,
    file_name: str,
    extraction_identifier: str,
    extraction_class: str,
    extraction_text: str,
    attributes: dict[str, Any] | None,
    char_start: int | None,
    char_end: int | None,
    grounding_status: str,
    validation_status: str,
    rejection_reasons: str,
    coverage_status: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id_value,
        "source_id": stable_source_id,
        "file_name": file_name,
        "extraction_id": extraction_identifier,
        "extraction_class": extraction_class,
        "extraction_text": extraction_text,
        "attributes_json": json.dumps(
            attributes or {}, ensure_ascii=False, sort_keys=True
        ),
        "char_start": "" if char_start is None else char_start,
        "char_end": "" if char_end is None else char_end,
        "grounding_status": grounding_status,
        "validation_status": validation_status,
        "rejection_reasons": rejection_reasons,
        "coverage_status": coverage_status,
        "review_status": "pending",
        "is_correct": "",
        "corrected_class": "",
        "corrected_text": "",
        "corrected_attributes": "",
        "review_note": "",
    }


def build_review_rows(
    *,
    event_id_value: str,
    stable_source_id: str,
    file_name: str,
    accepted: Iterable[Any],
    rejected: Iterable[dict[str, Any]],
    coverage_report: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    matched_candidate_ids = {
        extraction_identifier
        for candidate in coverage_report.get("matched_provisions", [])
        for extraction_identifier in candidate.get("matched_extraction_ids", [])
    }

    for extraction in accepted:
        interval = getattr(extraction, "char_interval", None)
        start = getattr(interval, "start_pos", None) if interval else None
        end = getattr(interval, "end_pos", None) if interval else None
        identifier = getattr(
            extraction,
            "_extraction_id",
            extraction_id(
                stable_source_id,
                extraction.extraction_class,
                start,
                end,
            ),
        )
        rows.append(_base_row(
            event_id_value=event_id_value,
            stable_source_id=stable_source_id,
            file_name=file_name,
            extraction_identifier=identifier,
            extraction_class=extraction.extraction_class,
            extraction_text=extraction.extraction_text,
            attributes=extraction.attributes,
            char_start=start,
            char_end=end,
            grounding_status=getattr(extraction, "_grounding_status", "exact"),
            validation_status=getattr(extraction, "_validation_status", "accepted"),
            rejection_reasons="",
            coverage_status=(
                "matched" if identifier in matched_candidate_ids else "not_applicable"
            ),
        ))

    for item in rejected:
        serialized = item.get("extraction", {})
        interval = serialized.get("char_interval") or {}
        start = interval.get("start_pos")
        end = interval.get("end_pos")
        extraction_class = serialized.get("extraction_class", "")
        identifier = extraction_id(
            stable_source_id,
            extraction_class,
            start,
            end,
        )
        rows.append(_base_row(
            event_id_value=event_id_value,
            stable_source_id=stable_source_id,
            file_name=file_name,
            extraction_identifier=identifier,
            extraction_class=extraction_class,
            extraction_text=serialized.get("extraction_text", ""),
            attributes=serialized.get("attributes", {}),
            char_start=start,
            char_end=end,
            grounding_status=serialized.get("grounding_status", "mismatch"),
            validation_status="rejected",
            rejection_reasons="；".join(item.get("reasons", [])),
            coverage_status="not_applicable",
        ))

    for candidate in coverage_report.get("unmatched_provisions", []):
        start = candidate.get("char_start")
        end = candidate.get("char_end")
        candidate_identifier = "cov_" + evidence_id(
            stable_source_id, start, end
        ).removeprefix("evd_")
        rows.append(_base_row(
            event_id_value=event_id_value,
            stable_source_id=stable_source_id,
            file_name=file_name,
            extraction_identifier=candidate_identifier,
            extraction_class=candidate.get("class_hint", "needs_review"),
            extraction_text=candidate.get("text", ""),
            attributes={"road_names": candidate.get("road_names", [])},
            char_start=start,
            char_end=end,
            grounding_status="exact",
            validation_status="coverage_unmatched",
            rejection_reasons="模型结果未覆盖候选条款",
            coverage_status="unmatched",
        ))
    return rows


def write_review_queue(rows: Iterable[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_handle:
        writer = csv.DictWriter(
            file_handle,
            fieldnames=REVIEW_FIELDNAMES,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
    return path
