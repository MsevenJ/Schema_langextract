"""Deterministic, evidence-backed JSONL export for downstream GraphRAG."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .ids import evidence_id, extraction_id


KG_FILES = (
    "events.jsonl",
    "venues.jsonl",
    "roads.jsonl",
    "control_rules.jsonl",
    "guidance.jsonl",
    "evidence.jsonl",
    "relations.jsonl",
)


def _node_id(prefix: str, value: str) -> str:
    import hashlib

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _load_other_sources(path: Path, stable_source_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("source_id") != stable_source_id:
            rows.append(payload)
    return rows


def export_kg(
    *,
    output_dir: Path,
    stable_source_id: str,
    event_id_value: str,
    accepted: Iterable[Any],
) -> dict[str, Path]:
    """Export machine-pending facts; never mark them human-confirmed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in KG_FILES}
    accepted_list = list(accepted)
    event_nodes: list[str] = []
    venue_nodes: list[str] = []

    for extraction in accepted_list:
        interval = getattr(extraction, "char_interval", None)
        start = getattr(interval, "start_pos", None) if interval else None
        end = getattr(interval, "end_pos", None) if interval else None
        ext_id = getattr(
            extraction,
            "_extraction_id",
            extraction_id(
                stable_source_id,
                extraction.extraction_class,
                start,
                end,
            ),
        )
        evd_id = evidence_id(stable_source_id, start, end)
        common = {
            "review_status": "pending",
            "source_id": stable_source_id,
            "evidence_id": evd_id,
            "extraction_id": ext_id,
        }
        rows["evidence.jsonl"].append({
            "evidence_id": evd_id,
            "source_id": stable_source_id,
            "char_start": start,
            "char_end": end,
            "text": extraction.extraction_text,
            "grounding_status": getattr(extraction, "_grounding_status", "exact"),
            "review_status": "pending",
        })

        entity_id = ""
        if extraction.extraction_class == "event_name":
            entity_id = event_id_value or ext_id
            event_nodes.append(entity_id)
            rows["events.jsonl"].append({
                "event_id": entity_id,
                "name": extraction.extraction_text,
                "identity_status": (
                    "manifest" if event_id_value else "machine_candidate"
                ),
                **common,
            })
        elif extraction.extraction_class == "event_venue":
            entity_id = _node_id("venue", extraction.extraction_text)
            venue_nodes.append(entity_id)
            rows["venues.jsonl"].append({
                "venue_id": entity_id,
                "name": extraction.extraction_text,
                **common,
            })
        elif extraction.extraction_class == "control_rule":
            entity_id = ext_id
            rows["control_rules.jsonl"].append({
                "control_rule_id": entity_id,
                "text": extraction.extraction_text,
                "attributes": extraction.attributes or {},
                **common,
            })
            for road_name in (extraction.attributes or {}).get("road_names", []):
                road_id = _node_id("road", road_name)
                rows["roads.jsonl"].append({
                    "road_id": road_id,
                    "name": road_name,
                    "review_status": "pending",
                    "source_id": stable_source_id,
                    "evidence_id": evd_id,
                })
                rows["relations.jsonl"].append({
                    "from_id": entity_id,
                    "relation": "affects",
                    "to_id": road_id,
                    **common,
                })
            time_text = (extraction.attributes or {}).get("time_text", "")
            if time_text:
                rows["relations.jsonl"].append({
                    "from_id": entity_id,
                    "relation": "active_during",
                    "to_id": _node_id("time", time_text),
                    "time_text": time_text,
                    **common,
                })
        elif extraction.extraction_class == "traffic_guidance":
            entity_id = ext_id
            guidance_type = (extraction.attributes or {}).get(
                "guidance_type", "general_advice"
            )
            rows["guidance.jsonl"].append({
                "guidance_id": entity_id,
                "text": extraction.extraction_text,
                "attributes": extraction.attributes or {},
                **common,
            })
            concern = {
                "parking": "parking",
                "public_transport": "public_transport",
                "drop_off": "ride_hailing",
            }.get(guidance_type, guidance_type)
            rows["relations.jsonl"].append({
                "from_id": entity_id,
                "relation": "concerns",
                "to_id": concern,
                **common,
            })

        if entity_id:
            rows["relations.jsonl"].append({
                "from_id": entity_id,
                "relation": "supported_by",
                "to_id": evd_id,
                **common,
            })

    for event_node in event_nodes:
        for venue_node in venue_nodes:
            rows["relations.jsonl"].append({
                "from_id": event_node,
                "relation": "held_at",
                "to_id": venue_node,
                "review_status": "pending",
                "source_id": stable_source_id,
                "evidence_id": "",
            })
        for rule in rows["control_rules.jsonl"]:
            rows["relations.jsonl"].append({
                "from_id": event_node,
                "relation": "has_control_rule",
                "to_id": rule["control_rule_id"],
                "review_status": "pending",
                "source_id": stable_source_id,
                "evidence_id": rule["evidence_id"],
            })

    paths: dict[str, Path] = {}
    for name, file_rows in rows.items():
        path = output_dir / name
        merged_rows = _load_other_sources(path, stable_source_id) + file_rows
        # Replace this source's prior export and de-duplicate deterministically.
        unique = {
            json.dumps(row, ensure_ascii=False, sort_keys=True): row
            for row in merged_rows
        }
        _write_jsonl(path, unique.values())
        paths[name] = path
    return paths
