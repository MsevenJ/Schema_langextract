"""Apply a completed traffic-notice review workbook to gold outputs."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Sequence

try:
    from openpyxl import load_workbook
except ImportError as exc:  # pragma: no cover - depends on the local environment.
    raise SystemExit(
        "缺少 openpyxl，请先执行: python -m pip install 'openpyxl>=3.1.0'"
    ) from exc


DEFAULT_INPUT = Path("output_calibrated/traffic_notice_review.xlsx")
DEFAULT_JSONL_OUTPUT = Path("gold/gold_extractions.jsonl")
DEFAULT_CSV_OUTPUT = Path("gold/gold_extractions.csv")
DEFAULT_UNREVIEWED_OUTPUT = Path("gold/unreviewed_rows.csv")
DEFAULT_METRICS_OUTPUT = Path("evaluation/model_vs_gold_metrics.csv")

ANNOUNCEMENT_SHEET = "公告信息复核"
CONTROL_SHEET = "道路管制复核"
GUIDANCE_SHEET = "交通引导复核"
MISSING_SHEET = "漏抽候选复核"
RAW_SHEET = "原始结果"

ANNOUNCEMENT_HEADERS = (
    "文件名",
    "活动名称",
    "活动名称复核结论",
    "活动场馆",
    "活动场馆复核结论",
    "活动时间",
    "活动时间复核结论",
    "管制时间",
    "管制时间复核结论",
    "发布机关",
    "发布机关复核结论",
    "发布日期",
    "发布日期复核结论",
    "复核备注",
    "source_id",
)

CONTROL_HEADERS = (
    "文件名",
    "活动名称",
    "道路名称",
    "管制路段",
    "方向",
    "管制时间",
    "管制措施",
    "车辆范围",
    "实施条件",
    "例外车辆或方式",
    "原文证据",
    "复核结论",
    "复核备注",
    "extraction_id",
    "source_id",
)

GUIDANCE_HEADERS = (
    "文件名",
    "活动名称",
    "引导类型",
    "路线或位置",
    "适用时间",
    "车辆范围",
    "具体动作",
    "实施条件",
    "原文证据",
    "复核结论",
    "复核备注",
    "extraction_id",
    "source_id",
)

MISSING_HEADERS = (
    "文件名",
    "候选原文",
    "道路名称",
    "目标类型",
    "对应结构化字段",
    "管制路段",
    "方向",
    "时间",
    "管制措施",
    "车辆范围",
    "引导类型",
    "路线或位置",
    "具体动作",
    "实施条件",
    "例外车辆或方式",
    "复核结论",
    "复核备注",
    "extraction_id",
    "source_id",
)

EVENT_CLASS_BY_COLUMN = {
    "活动名称": "event_name",
    "活动场馆": "event_venue",
    "活动时间": "event_time",
    "管制时间": "control_time",
    "发布机关": "issuing_authority",
    "发布日期": "publication_date",
}

GUIDANCE_TYPE_BY_LABEL = {
    "绕行路线": "detour",
    "入场路线": "access",
    "散场/离场路线": "egress",
    "停车": "parking",
    "上下客": "drop_off",
    "公共交通": "public_transport",
    "一般出行建议": "general_advice",
}

TARGET_CLASS_BY_LABEL = {
    "道路管制": "control_rule",
    "交通引导": "traffic_guidance",
    "非目标": "non_target",
}

EVENT_CLASSES = frozenset(EVENT_CLASS_BY_COLUMN.values())
ALLOWED_CONCLUSIONS = frozenset({"待复核", "正确", "已修改", "删除"})
CONFIRMED_CONCLUSIONS = frozenset({"正确", "已修改"})
GUIDANCE_TYPES = frozenset({
    "detour",
    "access",
    "egress",
    "parking",
    "drop_off",
    "public_transport",
    "general_advice",
})

RAW_REQUIRED_FIELDS = {
    "source_id",
    "file_name",
    "extraction_id",
    "extraction_class",
    "extraction_text",
    "attributes_json",
}

CSV_FIELDNAMES = (
    "schema_version",
    "file_name",
    "source_id",
    "extraction_id",
    "extraction_class",
    "extraction_text",
    "evidence_text",
    "model_extraction_text",
    "review_status",
    "review_conclusion",
    "review_note",
    "record_origin",
    "event_name",
    "precision",
    "road_names",
    "road_range",
    "direction",
    "time_text",
    "measure",
    "vehicle_scope",
    "condition",
    "exception",
    "guidance_type",
    "route_or_location",
    "action",
    "char_start",
    "char_end",
    "grounding_status",
    "validation_status",
    "coverage_status",
)

METRICS_FIELDNAMES = (
    "extraction_class",
    "model_rows",
    "gold_rows",
    "reviewed_rows",
    "pending_rows",
    "correct_rows",
    "modified_rows",
    "deleted_rows",
    "added_rows",
    "review_completion_rate",
    "exact_acceptance_rate",
    "gold_retention_rate",
)

CLASS_ORDER = (
    "issuing_authority",
    "publication_date",
    "event_name",
    "event_venue",
    "event_time",
    "control_time",
    "control_rule",
    "traffic_guidance",
    "needs_review",
)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _sheet_rows(
    workbook: Any,
    sheet_name: str,
    expected_headers: Sequence[str] | None = None,
) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    if sheet_name not in workbook.sheetnames:
        raise ValueError(f"工作簿缺少工作表: {sheet_name}")
    worksheet = workbook[sheet_name]
    headers = [_cell_text(cell.value).strip() for cell in worksheet[1]]
    if expected_headers is not None and headers != list(expected_headers):
        raise ValueError(
            f"工作表“{sheet_name}”表头已改动。\n"
            f"期望: {list(expected_headers)}\n实际: {headers}"
        )
    output: list[tuple[int, dict[str, str]]] = []
    for row_number, cells in enumerate(
        worksheet.iter_rows(min_row=2, max_col=len(headers)), start=2
    ):
        values = [_cell_text(cell.value) for cell in cells]
        if not any(value.strip() for value in values):
            continue
        output.append((row_number, dict(zip(headers, values))))
    return headers, output


def _normalize_conclusion(value: str, *, sheet_name: str, row_number: int) -> str:
    conclusion = value.strip() or "待复核"
    if conclusion not in ALLOWED_CONCLUSIONS:
        raise ValueError(
            f"工作表“{sheet_name}”第 {row_number} 行复核结论无效: {conclusion}"
        )
    return conclusion


def _parse_json_object(value: str, *, context: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{context} attributes_json 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{context} attributes_json 必须是 JSON 对象")
    return parsed


def _split_lines(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\r\n", "\n").split("\n") if part.strip()]


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;\r\n]+", value) if part.strip()]


def _display_list(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value).strip()


def _synthetic_extraction_id(
    source_id: str, extraction_class: str, value: str, ordinal: int
) -> str:
    payload = f"{source_id}\0{extraction_class}\0{ordinal}\0{value}".encode("utf-8")
    return "ext_" + hashlib.sha256(payload).hexdigest()[:24]


def _infer_publication_precision(value: str, fallback: str = "") -> str:
    value = value.strip()
    if re.search(r"(?:\d{1,2}日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})", value):
        return "day"
    if re.search(r"(?:\d{1,2}月|\d{4}[-/.]\d{1,2})", value):
        return "month"
    return fallback


def _guidance_type_code(value: str) -> str:
    value = value.strip()
    if value in GUIDANCE_TYPE_BY_LABEL:
        return GUIDANCE_TYPE_BY_LABEL[value]
    if value in GUIDANCE_TYPES:
        return value
    return ""


def _make_record(
    *,
    original: dict[str, str] | None,
    file_name: str,
    source_id: str,
    extraction_id: str,
    extraction_class: str,
    extraction_text: str,
    evidence_text: str,
    attributes: dict[str, Any],
    conclusion: str,
    review_note: str,
    event_name: str = "",
    record_origin: str | None = None,
) -> dict[str, Any]:
    original = original or {}
    if record_origin is None:
        record_origin = "model" if original else "manual"
    road_names = attributes.get("road_names", [])
    record: dict[str, Any] = {
        "schema_version": "2.0",
        "file_name": file_name or original.get("file_name", ""),
        "source_id": source_id,
        "extraction_id": extraction_id,
        "extraction_class": extraction_class,
        "extraction_text": extraction_text,
        "evidence_text": evidence_text,
        "model_extraction_text": original.get("extraction_text", ""),
        "review_status": "pending" if conclusion == "待复核" else "confirmed",
        "review_conclusion": conclusion,
        "review_note": review_note,
        "record_origin": record_origin,
        "event_name": event_name,
        "attributes": attributes,
        "precision": _display_list(attributes.get("precision")),
        "road_names": _display_list(road_names),
        "road_range": _display_list(attributes.get("road_range")),
        "direction": _display_list(attributes.get("direction")),
        "time_text": _display_list(attributes.get("time_text")),
        "measure": _display_list(attributes.get("measure")),
        "vehicle_scope": _display_list(attributes.get("vehicle_scope")),
        "condition": _display_list(attributes.get("condition")),
        "exception": _display_list(attributes.get("exception")),
        "guidance_type": _display_list(attributes.get("guidance_type")),
        "route_or_location": _display_list(attributes.get("route_or_location")),
        "action": _display_list(attributes.get("action")),
        "char_start": original.get("char_start", ""),
        "char_end": original.get("char_end", ""),
        "grounding_status": original.get("grounding_status", ""),
        "validation_status": original.get("validation_status", ""),
        "coverage_status": original.get("coverage_status", ""),
    }
    return record


def _event_record(
    *,
    value: str,
    ordinal: int,
    extraction_class: str,
    original: dict[str, str] | None,
    row: dict[str, str],
    conclusion: str,
) -> dict[str, Any]:
    original_attributes = _parse_json_object(
        (original or {}).get("attributes_json", ""),
        context=f"{RAW_SHEET}/{row.get('source_id', '')}/{extraction_class}",
    )
    attributes: dict[str, Any] = {}
    if extraction_class == "publication_date":
        precision = _infer_publication_precision(
            value, str(original_attributes.get("precision", ""))
        )
        if precision:
            attributes["precision"] = precision
    source_id = row["source_id"].strip()
    extraction_id = (original or {}).get("extraction_id", "") or _synthetic_extraction_id(
        source_id, extraction_class, value, ordinal
    )
    return _make_record(
        original=original,
        file_name=row["文件名"],
        source_id=source_id,
        extraction_id=extraction_id,
        extraction_class=extraction_class,
        extraction_text=value,
        evidence_text=value,
        attributes=attributes,
        conclusion=conclusion,
        review_note=row["复核备注"],
        event_name=row["活动名称"],
    )


def _control_record(
    *,
    row: dict[str, str],
    original: dict[str, str] | None,
    conclusion: str,
    ordinal: int,
    record_origin: str | None = None,
) -> dict[str, Any]:
    attributes = {
        "road_names": _split_list(row["道路名称"]),
        "road_range": row["管制路段"].strip(),
        "direction": row["方向"].strip(),
        "time_text": row["管制时间"].strip(),
        "measure": row["管制措施"].strip(),
        "vehicle_scope": row["车辆范围"].strip(),
        "condition": row["实施条件"].strip(),
        "exception": row["例外车辆或方式"].strip(),
    }
    source_id = row["source_id"].strip()
    evidence = row["原文证据"].strip()
    extraction_id = row["extraction_id"].strip() or _synthetic_extraction_id(
        source_id, "control_rule", evidence, ordinal
    )
    return _make_record(
        original=original,
        file_name=row["文件名"],
        source_id=source_id,
        extraction_id=extraction_id,
        extraction_class="control_rule",
        extraction_text=evidence,
        evidence_text=evidence,
        attributes=attributes,
        conclusion=conclusion,
        review_note=row["复核备注"],
        event_name=row["活动名称"],
        record_origin=record_origin,
    )


def _guidance_record(
    *,
    row: dict[str, str],
    original: dict[str, str] | None,
    conclusion: str,
    ordinal: int,
    row_number: int,
    record_origin: str | None = None,
    sheet_name: str = GUIDANCE_SHEET,
) -> dict[str, Any]:
    guidance_label = row["引导类型"].strip()
    guidance_type = _guidance_type_code(guidance_label)
    if conclusion in CONFIRMED_CONCLUSIONS and not guidance_type:
        allowed = "、".join(GUIDANCE_TYPE_BY_LABEL)
        raise ValueError(
            f"工作表“{sheet_name}”第 {row_number} 行引导类型必须是: {allowed}"
        )
    attributes = {
        "guidance_type": guidance_type,
        "route_or_location": row["路线或位置"].strip(),
        "time_text": row["适用时间"].strip(),
        "vehicle_scope": row["车辆范围"].strip(),
        "action": row["具体动作"].strip(),
        "condition": row["实施条件"].strip(),
    }
    source_id = row["source_id"].strip()
    evidence = row["原文证据"].strip()
    extraction_id = row["extraction_id"].strip() or _synthetic_extraction_id(
        source_id, "traffic_guidance", evidence, ordinal
    )
    return _make_record(
        original=original,
        file_name=row["文件名"],
        source_id=source_id,
        extraction_id=extraction_id,
        extraction_class="traffic_guidance",
        extraction_text=evidence,
        evidence_text=evidence,
        attributes=attributes,
        conclusion=conclusion,
        review_note=row["复核备注"],
        event_name=row["活动名称"],
        record_origin=record_origin,
    )


def _resolve_identity(
    row: dict[str, str],
    *,
    file_to_sources: dict[str, set[str]],
    source_to_file: dict[str, str],
    sheet_name: str,
    row_number: int,
) -> None:
    file_name = row.get("文件名", "").strip()
    source_id = row.get("source_id", "").strip()
    if not source_id and file_name:
        candidates = file_to_sources.get(file_name, set())
        if len(candidates) == 1:
            source_id = next(iter(candidates))
            row["source_id"] = source_id
    if not file_name and source_id in source_to_file:
        row["文件名"] = source_to_file[source_id]
        file_name = row["文件名"]
    if not source_id:
        raise ValueError(
            f"工作表“{sheet_name}”第 {row_number} 行无法确定 source_id；"
            "请填写 source_id 或已知文件名。"
        )


def _missing_candidate_record(
    *,
    row: dict[str, str],
    original: dict[str, str] | None,
    conclusion: str,
    ordinal: int,
    row_number: int,
) -> dict[str, Any] | None:
    if conclusion == "删除":
        return None
    target_label = row["目标类型"].strip()
    target_class = TARGET_CLASS_BY_LABEL.get(target_label, "")
    if conclusion in CONFIRMED_CONCLUSIONS and not target_class:
        raise ValueError(
            f"工作表“{MISSING_SHEET}”第 {row_number} 行已完成复核，"
            "但未选择目标类型。"
        )
    if target_class in ("", "non_target"):
        return None

    common = {
        "文件名": row["文件名"],
        "活动名称": "",
        "道路名称": row["道路名称"],
        "车辆范围": row["车辆范围"],
        "实施条件": row["实施条件"],
        "原文证据": row["候选原文"],
        "复核备注": row["复核备注"],
        "extraction_id": row["extraction_id"],
        "source_id": row["source_id"],
    }
    if target_class == "control_rule":
        control_row = {
            **common,
            "管制路段": row["管制路段"],
            "方向": row["方向"],
            "管制时间": row["时间"],
            "管制措施": row["管制措施"],
            "例外车辆或方式": row["例外车辆或方式"],
        }
        return _control_record(
            row=control_row,
            original=original,
            conclusion=conclusion,
            ordinal=ordinal,
            record_origin="needs_review",
        )

    guidance_row = {
        **common,
        "引导类型": row["引导类型"],
        "路线或位置": row["路线或位置"],
        "适用时间": row["时间"],
        "具体动作": row["具体动作"],
    }
    return _guidance_record(
        row=guidance_row,
        original=original,
        conclusion=conclusion,
        ordinal=ordinal,
        row_number=row_number,
        record_origin="needs_review",
        sheet_name=MISSING_SHEET,
    )


def _original_record(row: dict[str, str]) -> dict[str, Any]:
    attributes = _parse_json_object(
        row.get("attributes_json", ""),
        context=f"{RAW_SHEET}/{row.get('source_id', '')}/{row.get('extraction_id', '')}",
    )
    return _make_record(
        original=row,
        file_name=row.get("file_name", ""),
        source_id=row.get("source_id", ""),
        extraction_id=row.get("extraction_id", ""),
        extraction_class=row.get("extraction_class", ""),
        extraction_text=row.get("extraction_text", ""),
        evidence_text=row.get("extraction_text", ""),
        attributes=attributes,
        conclusion="待复核",
        review_note=row.get("review_note", ""),
    )


def _find_original_index(
    *,
    indices_by_key: dict[tuple[str, str, str], list[int]],
    raw_rows: Sequence[dict[str, str]],
    used_indices: set[int],
    source_id: str,
    extraction_class: str,
    extraction_id: str,
    evidence: str,
) -> int | None:
    candidates = indices_by_key.get((source_id, extraction_class, extraction_id), [])
    for index in candidates:
        if index not in used_indices and raw_rows[index].get("extraction_text", "") == evidence:
            return index
    for index in candidates:
        if index not in used_indices:
            return index
    return None


def _record_decision(
    *,
    conclusion: str,
    record: dict[str, Any],
    gold_records: list[dict[str, Any]],
    unreviewed_records: list[dict[str, Any]],
) -> None:
    if conclusion in CONFIRMED_CONCLUSIONS:
        gold_records.append(record)
    elif conclusion == "待复核":
        unreviewed_records.append(record)


def _atomic_text_write(path: Path, writer: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig" if path.suffix.lower() == ".csv" else "utf-8",
            newline="",
            prefix=f".{path.stem}.",
            suffix=path.suffix,
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            writer(handle)
        Path(temporary_name).replace(path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _write_jsonl(records: Iterable[dict[str, Any]], path: Path) -> None:
    def write(handle: Any) -> None:
        for record in records:
            json.dump(record, handle, ensure_ascii=False, sort_keys=False)
            handle.write("\n")

    _atomic_text_write(path, write)


def _write_csv(records: Iterable[dict[str, Any]], path: Path) -> None:
    def write(handle: Any) -> None:
        csv_writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        csv_writer.writeheader()
        csv_writer.writerows(records)

    _atomic_text_write(path, write)


def _ensure_unique_gold_extraction_ids(records: Sequence[dict[str, Any]]) -> None:
    """Make Gold extraction IDs unique while preserving the first occurrence."""
    seen_ids: set[str] = set()
    occurrence_by_original_id: Counter[str] = Counter()
    for record in records:
        original_id = str(record.get("extraction_id", "")).strip()
        occurrence_by_original_id[original_id] += 1
        if original_id and original_id not in seen_ids:
            seen_ids.add(original_id)
            continue

        ordinal = occurrence_by_original_id[original_id]
        if not original_id:
            ordinal = max(ordinal, 1)
        while True:
            replacement = _synthetic_extraction_id(
                str(record.get("source_id", "")),
                str(record.get("extraction_class", "")),
                str(record.get("extraction_text", "")),
                ordinal,
            )
            if replacement not in seen_ids:
                break
            ordinal += 1
        record["extraction_id"] = replacement
        seen_ids.add(replacement)


def _rate(numerator: int, denominator: int) -> str:
    return "" if denominator == 0 else f"{numerator / denominator:.6f}"


def _build_metrics(
    raw_rows: Sequence[dict[str, str]],
    decisions: Sequence[str],
    extra_decisions: Sequence[tuple[str, str]],
    gold_records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    model_counts = Counter(row.get("extraction_class", "") for row in raw_rows)
    gold_counts = Counter(record["extraction_class"] for record in gold_records)
    status_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row, decision in zip(raw_rows, decisions):
        status_counts[row.get("extraction_class", "")][decision] += 1
    added_counts = Counter(extraction_class for extraction_class, _ in extra_decisions)
    for extraction_class, decision in extra_decisions:
        status_counts[extraction_class][decision] += 1

    seen = set(model_counts) | set(gold_counts) | set(added_counts)
    ordered_classes = [item for item in CLASS_ORDER if item in seen]
    ordered_classes.extend(sorted(seen.difference(ordered_classes)))

    metrics: list[dict[str, Any]] = []
    for extraction_class in ordered_classes:
        counts = status_counts[extraction_class]
        model_rows = model_counts[extraction_class]
        correct = counts["正确"]
        modified = counts["已修改"]
        deleted = counts["删除"]
        pending = counts["待复核"]
        reviewed = correct + modified + deleted
        total_review_rows = model_rows + added_counts[extraction_class]
        metrics.append({
            "extraction_class": extraction_class,
            "model_rows": model_rows,
            "gold_rows": gold_counts[extraction_class],
            "reviewed_rows": reviewed,
            "pending_rows": pending,
            "correct_rows": correct,
            "modified_rows": modified,
            "deleted_rows": deleted,
            "added_rows": added_counts[extraction_class],
            "review_completion_rate": _rate(reviewed, total_review_rows),
            "exact_acceptance_rate": _rate(correct, reviewed),
            "gold_retention_rate": _rate(
                gold_counts[extraction_class], total_review_rows
            ),
        })

    overall = {
        key: sum(int(row[key]) for row in metrics)
        for key in (
            "model_rows",
            "gold_rows",
            "reviewed_rows",
            "pending_rows",
            "correct_rows",
            "modified_rows",
            "deleted_rows",
            "added_rows",
        )
    }
    metrics.append({
        "extraction_class": "__overall__",
        **overall,
        "review_completion_rate": _rate(
            overall["reviewed_rows"],
            overall["model_rows"] + overall["added_rows"],
        ),
        "exact_acceptance_rate": _rate(overall["correct_rows"], overall["reviewed_rows"]),
        "gold_retention_rate": _rate(
            overall["gold_rows"],
            overall["model_rows"] + overall["added_rows"],
        ),
    })
    return metrics


def _write_metrics(metrics: Iterable[dict[str, Any]], path: Path) -> None:
    def write(handle: Any) -> None:
        csv_writer = csv.DictWriter(handle, fieldnames=METRICS_FIELDNAMES)
        csv_writer.writeheader()
        csv_writer.writerows(metrics)

    _atomic_text_write(path, write)


def apply_workbook(
    input_path: Path,
    jsonl_output: Path,
    csv_output: Path,
    unreviewed_output: Path,
    metrics_output: Path,
) -> dict[str, int]:
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到复核工作簿: {input_path}")

    workbook = load_workbook(input_path, data_only=True, read_only=False)
    try:
        _, announcement_rows = _sheet_rows(
            workbook, ANNOUNCEMENT_SHEET, ANNOUNCEMENT_HEADERS
        )
        _, control_rows = _sheet_rows(workbook, CONTROL_SHEET, CONTROL_HEADERS)
        _, guidance_rows = _sheet_rows(workbook, GUIDANCE_SHEET, GUIDANCE_HEADERS)
        _, missing_rows = _sheet_rows(workbook, MISSING_SHEET, MISSING_HEADERS)
        raw_headers, numbered_raw_rows = _sheet_rows(workbook, RAW_SHEET)
    finally:
        workbook.close()

    missing_raw_fields = sorted(RAW_REQUIRED_FIELDS.difference(raw_headers))
    if missing_raw_fields:
        raise ValueError(
            f"工作表“{RAW_SHEET}”缺少字段: {', '.join(missing_raw_fields)}"
        )
    raw_rows = [row for _, row in numbered_raw_rows]
    decisions = ["待复核"] * len(raw_rows)
    used_indices: set[int] = set()
    represented_indices: set[int] = set()
    extra_decisions: list[tuple[str, str]] = []
    gold_records: list[dict[str, Any]] = []
    unreviewed_records: list[dict[str, Any]] = []

    indices_by_source_class: dict[tuple[str, str], list[int]] = defaultdict(list)
    indices_by_key: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    file_to_sources: dict[str, set[str]] = defaultdict(set)
    source_to_file: dict[str, str] = {}
    for index, raw_row in enumerate(raw_rows):
        source_id = raw_row.get("source_id", "")
        extraction_class = raw_row.get("extraction_class", "")
        extraction_id = raw_row.get("extraction_id", "")
        indices_by_source_class[(source_id, extraction_class)].append(index)
        indices_by_key[(source_id, extraction_class, extraction_id)].append(index)
        file_name = raw_row.get("file_name", "")
        if file_name and source_id:
            file_to_sources[file_name].add(source_id)
            source_to_file.setdefault(source_id, file_name)

    for row_number, row in announcement_rows:
        source_id = row["source_id"].strip()
        if not source_id:
            raise ValueError(f"工作表“{ANNOUNCEMENT_SHEET}”第 {row_number} 行 source_id 为空")
        for column, extraction_class in EVENT_CLASS_BY_COLUMN.items():
            conclusion_header = f"{column}复核结论"
            conclusion = _normalize_conclusion(
                row[conclusion_header],
                sheet_name=ANNOUNCEMENT_SHEET,
                row_number=row_number,
            )
            original_indices = indices_by_source_class.get((source_id, extraction_class), [])
            represented_indices.update(original_indices)
            for index in original_indices:
                decisions[index] = conclusion

            values = _split_lines(row[column])
            for ordinal, value in enumerate(values):
                original_index = (
                    original_indices[ordinal] if ordinal < len(original_indices) else None
                )
                original = raw_rows[original_index] if original_index is not None else None
                if original_index is not None:
                    used_indices.add(original_index)
                else:
                    extra_decisions.append((extraction_class, conclusion))
                record = _event_record(
                    value=value,
                    ordinal=ordinal,
                    extraction_class=extraction_class,
                    original=original,
                    row=row,
                    conclusion=conclusion,
                )
                _record_decision(
                    conclusion=conclusion,
                    record=record,
                    gold_records=gold_records,
                    unreviewed_records=unreviewed_records,
                )

            # If a pending cell was accidentally shortened or cleared, retain
            # every unmatched original candidate in the unreviewed output.
            if conclusion == "待复核":
                for original_index in original_indices[len(values):]:
                    used_indices.add(original_index)
                    unreviewed_records.append(_original_record(raw_rows[original_index]))

    for ordinal, (row_number, row) in enumerate(control_rows):
        conclusion = _normalize_conclusion(
            row["复核结论"], sheet_name=CONTROL_SHEET, row_number=row_number
        )
        _resolve_identity(
            row,
            file_to_sources=file_to_sources,
            source_to_file=source_to_file,
            sheet_name=CONTROL_SHEET,
            row_number=row_number,
        )
        source_id = row["source_id"].strip()
        extraction_id = row["extraction_id"].strip()
        original_index = _find_original_index(
            indices_by_key=indices_by_key,
            raw_rows=raw_rows,
            used_indices=used_indices,
            source_id=source_id,
            extraction_class="control_rule",
            extraction_id=extraction_id,
            evidence=row["原文证据"].strip(),
        )
        original = raw_rows[original_index] if original_index is not None else None
        if original_index is None:
            extra_decisions.append(("control_rule", conclusion))
        else:
            used_indices.add(original_index)
            represented_indices.add(original_index)
            decisions[original_index] = conclusion
        record = _control_record(
            row=row,
            original=original,
            conclusion=conclusion,
            ordinal=ordinal,
            record_origin="model" if original is not None else "manual",
        )
        _record_decision(
            conclusion=conclusion,
            record=record,
            gold_records=gold_records,
            unreviewed_records=unreviewed_records,
        )

    for ordinal, (row_number, row) in enumerate(guidance_rows):
        conclusion = _normalize_conclusion(
            row["复核结论"], sheet_name=GUIDANCE_SHEET, row_number=row_number
        )
        _resolve_identity(
            row,
            file_to_sources=file_to_sources,
            source_to_file=source_to_file,
            sheet_name=GUIDANCE_SHEET,
            row_number=row_number,
        )
        source_id = row["source_id"].strip()
        extraction_id = row["extraction_id"].strip()
        original_index = _find_original_index(
            indices_by_key=indices_by_key,
            raw_rows=raw_rows,
            used_indices=used_indices,
            source_id=source_id,
            extraction_class="traffic_guidance",
            extraction_id=extraction_id,
            evidence=row["原文证据"].strip(),
        )
        original = raw_rows[original_index] if original_index is not None else None
        if original_index is None:
            extra_decisions.append(("traffic_guidance", conclusion))
        else:
            used_indices.add(original_index)
            represented_indices.add(original_index)
            decisions[original_index] = conclusion
        record = _guidance_record(
            row=row,
            original=original,
            conclusion=conclusion,
            ordinal=ordinal,
            row_number=row_number,
            record_origin="model" if original is not None else "manual",
        )
        _record_decision(
            conclusion=conclusion,
            record=record,
            gold_records=gold_records,
            unreviewed_records=unreviewed_records,
        )

    for ordinal, (row_number, row) in enumerate(missing_rows):
        conclusion = _normalize_conclusion(
            row["复核结论"], sheet_name=MISSING_SHEET, row_number=row_number
        )
        _resolve_identity(
            row,
            file_to_sources=file_to_sources,
            source_to_file=source_to_file,
            sheet_name=MISSING_SHEET,
            row_number=row_number,
        )
        source_id = row["source_id"].strip()
        extraction_id = row["extraction_id"].strip()
        original_index = _find_original_index(
            indices_by_key=indices_by_key,
            raw_rows=raw_rows,
            used_indices=used_indices,
            source_id=source_id,
            extraction_class="needs_review",
            extraction_id=extraction_id,
            evidence=row["候选原文"].strip(),
        )
        original = raw_rows[original_index] if original_index is not None else None
        if original_index is None:
            extra_decisions.append(("needs_review", conclusion))
        else:
            used_indices.add(original_index)
            represented_indices.add(original_index)
            decisions[original_index] = conclusion

        if conclusion == "待复核":
            if original is not None:
                unreviewed_records.append(_original_record(original))
            else:
                unreviewed_records.append(_make_record(
                    original=None,
                    file_name=row["文件名"],
                    source_id=source_id,
                    extraction_id=extraction_id or _synthetic_extraction_id(
                        source_id, "needs_review", row["候选原文"], ordinal
                    ),
                    extraction_class="needs_review",
                    extraction_text=row["候选原文"],
                    evidence_text=row["候选原文"],
                    attributes={"road_names": _split_list(row["道路名称"])},
                    conclusion=conclusion,
                    review_note=row["复核备注"],
                    record_origin="manual",
                ))
            continue

        record = _missing_candidate_record(
            row=row,
            original=original,
            conclusion=conclusion,
            ordinal=ordinal,
            row_number=row_number,
        )
        if record is not None:
            gold_records.append(record)

    # Any raw source row missing from all review sheets remains explicitly
    # pending, so a workbook edit cannot silently discard a model candidate.
    for index, raw_row in enumerate(raw_rows):
        if index not in represented_indices:
            unreviewed_records.append(_original_record(raw_row))

    _ensure_unique_gold_extraction_ids(gold_records)
    metrics = _build_metrics(raw_rows, decisions, extra_decisions, gold_records)
    _write_jsonl(gold_records, jsonl_output)
    _write_csv(gold_records, csv_output)
    _write_csv(unreviewed_records, unreviewed_output)
    _write_metrics(metrics, metrics_output)

    return {
        "gold": len(gold_records),
        "unreviewed": len(unreviewed_records),
        "deleted": (
            sum(1 for decision in decisions if decision == "删除")
            + sum(1 for _, decision in extra_decisions if decision == "删除")
        ),
        "source_rows": len(raw_rows),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="读取人工复核 Excel，生成金标准、未复核队列和评估指标。"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--jsonl-output", type=Path, default=DEFAULT_JSONL_OUTPUT)
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT)
    parser.add_argument(
        "--unreviewed-output", type=Path, default=DEFAULT_UNREVIEWED_OUTPUT
    )
    parser.add_argument("--metrics-output", type=Path, default=DEFAULT_METRICS_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    counts = apply_workbook(
        args.input,
        args.jsonl_output,
        args.csv_output,
        args.unreviewed_output,
        args.metrics_output,
    )
    print(f"已读取: {args.input}")
    print(f"已确认: {counts['gold']} 条")
    print(f"待复核: {counts['unreviewed']} 条")
    print(f"已删除: {counts['deleted']} 条")
    print(f"金标准 JSONL: {args.jsonl_output}")
    print(f"金标准 CSV: {args.csv_output}")
    print(f"未复核 CSV: {args.unreviewed_output}")
    print(f"评估指标: {args.metrics_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
