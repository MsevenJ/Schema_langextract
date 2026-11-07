"""Generate review-based model error analysis without changing extraction logic."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Sequence


DEFAULT_REVIEW_QUEUE = Path("output_calibrated/review_queue.csv")
DEFAULT_GOLD = Path("gold/gold_extractions.csv")
DEFAULT_METRICS = Path("evaluation/model_vs_gold_metrics.csv")
DEFAULT_ERROR_ANALYSIS = Path("evaluation/error_analysis.csv")
DEFAULT_REPORT = Path("evaluation/prompt_calibration_report.md")

ERROR_FIELDNAMES = (
    "file_name",
    "source_id",
    "extraction_id",
    "extraction_class",
    "record_origin",
    "review_conclusion",
    "model_text",
    "gold_text",
    "changed_fields",
    "error_type",
    "error_description",
    "metric_class",
    "model_extraction_id",
    "review_note",
    "in_gold",
)

STATUS_TO_METRIC_COLUMN = {
    "正确": "correct_rows",
    "已修改": "modified_rows",
    "删除": "deleted_rows",
}

CONTROL_FIELDS = (
    "road_names",
    "road_range",
    "direction",
    "time_text",
    "measure",
    "vehicle_scope",
    "condition",
    "exception",
)
GUIDANCE_FIELDS = (
    "guidance_type",
    "route_or_location",
    "time_text",
    "vehicle_scope",
    "action",
    "condition",
)
FIELD_LABELS = {
    "extraction_class": "抽取类别",
    "extraction_text": "抽取文本",
    "precision": "日期精度",
    "road_names": "道路名称",
    "road_range": "管制路段",
    "direction": "方向",
    "time_text": "时间",
    "measure": "管制措施",
    "vehicle_scope": "车辆范围",
    "condition": "实施条件",
    "exception": "例外车辆或方式",
    "guidance_type": "引导类型",
    "route_or_location": "路线或位置",
    "action": "具体动作",
    "record": "整条记录",
}
CLASS_LABELS = {
    "issuing_authority": "发布机关",
    "publication_date": "发布日期",
    "event_name": "活动名称",
    "event_venue": "活动场馆",
    "event_time": "活动时间",
    "control_time": "管制时间",
    "control_rule": "道路管制",
    "traffic_guidance": "交通引导",
    "needs_review": "coverage候选",
}
ERROR_TYPES = (
    "正确",
    "属性缺失",
    "文本范围错误",
    "类别误判",
    "时间拆分错误",
    "重复抽取",
    "幻觉或非原文",
    "模型漏抽",
    "coverage候选补回",
    "人工新增",
    "属性值错误",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key: value or "" for key, value in row.items() if key is not None}
            for row in csv.DictReader(handle)
        ]


def _parse_attributes(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _normalize_value(field: str, value: Any) -> Any:
    if field == "road_names":
        if isinstance(value, list):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return tuple(
            part.strip()
            for part in re.split(r"[；;\r\n]+", str(value or ""))
            if part.strip()
        )
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value)
    return str(value or "").strip()


def _raw_fields(row: dict[str, str]) -> dict[str, Any]:
    attributes = _parse_attributes(row.get("attributes_json", ""))
    output: dict[str, Any] = {"extraction_text": row.get("extraction_text", "")}
    output.update(attributes)
    return output


def _gold_fields(row: dict[str, str]) -> dict[str, Any]:
    output: dict[str, Any] = {"extraction_text": row.get("extraction_text", "")}
    for field in ("precision", *CONTROL_FIELDS, *GUIDANCE_FIELDS):
        output[field] = row.get(field, "")
    return output


def _relevant_fields(raw_class: str, gold_class: str) -> tuple[str, ...]:
    classes = {raw_class, gold_class}
    if "control_rule" in classes:
        return ("extraction_text", *CONTROL_FIELDS)
    if "traffic_guidance" in classes:
        return ("extraction_text", *GUIDANCE_FIELDS)
    if "publication_date" in classes:
        return ("extraction_text", "precision")
    return ("extraction_text",)


def _changed_fields(raw: dict[str, str], gold: dict[str, str]) -> list[str]:
    raw_class = raw.get("extraction_class", "")
    gold_class = gold.get("extraction_class", "")
    changed: list[str] = []
    if raw_class != gold_class:
        changed.append("extraction_class")
    raw_values = _raw_fields(raw)
    gold_values = _gold_fields(gold)
    for field in _relevant_fields(raw_class, gold_class):
        if _normalize_value(field, raw_values.get(field, "")) != _normalize_value(
            field, gold_values.get(field, "")
        ):
            changed.append(field)
    return changed


def _all_gold_fields(gold: dict[str, str]) -> list[str]:
    fields = ["extraction_text"]
    for field in ("precision", *CONTROL_FIELDS, *GUIDANCE_FIELDS):
        if _normalize_value(field, gold.get(field, "")):
            fields.append(field)
    return list(dict.fromkeys(fields))


def _duplicate_raw_indices(rows: Sequence[dict[str, str]]) -> set[int]:
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    id_groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        text_key = (
            row.get("source_id", ""),
            row.get("extraction_class", ""),
            _normalize_text(row.get("extraction_text", "")),
        )
        groups[text_key].append(index)
        id_groups[(
            row.get("source_id", ""),
            row.get("extraction_class", ""),
            row.get("extraction_id", ""),
        )].append(index)
    duplicate_indices: set[int] = set()
    for group in (*groups.values(), *id_groups.values()):
        if len(group) > 1:
            duplicate_indices.update(group[1:])
    return duplicate_indices


def _cross_class_text_indices(rows: Sequence[dict[str, str]]) -> set[int]:
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if row.get("extraction_class") not in {"control_rule", "traffic_guidance"}:
            continue
        groups[(
            row.get("source_id", ""),
            _normalize_text(row.get("extraction_text", "")),
        )].append(index)
    output: set[int] = set()
    for indices in groups.values():
        if len({rows[index].get("extraction_class") for index in indices}) > 1:
            output.update(indices)
    return output


def _match_gold_to_raw(
    review_rows: Sequence[dict[str, str]],
    gold_rows: Sequence[dict[str, str]],
) -> tuple[dict[int, dict[str, str]], list[dict[str, str]]]:
    by_id: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_source_class_text: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    by_interval: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    for index, row in enumerate(review_rows):
        by_id[(row.get("source_id", ""), row.get("extraction_id", ""))].append(index)
        by_source_class_text[(
            row.get("source_id", ""),
            row.get("extraction_class", ""),
            _normalize_text(row.get("extraction_text", "")),
        )].append(index)
        by_interval[(
            row.get("source_id", ""),
            row.get("extraction_class", ""),
            row.get("char_start", ""),
            row.get("char_end", ""),
        )].append(index)

    used: set[int] = set()
    matched: dict[int, dict[str, str]] = {}
    manual: list[dict[str, str]] = []
    for gold in gold_rows:
        origin = gold.get("record_origin", "")
        if origin == "manual":
            manual.append(gold)
            continue
        expected_class = "needs_review" if origin == "needs_review" else gold.get(
            "extraction_class", ""
        )
        candidates = by_id.get(
            (gold.get("source_id", ""), gold.get("extraction_id", "")), []
        )
        candidate = next(
            (
                index
                for index in candidates
                if index not in used
                and review_rows[index].get("extraction_class") == expected_class
            ),
            None,
        )
        if candidate is None:
            model_text = gold.get("model_extraction_text", "") or gold.get(
                "evidence_text", ""
            )
            candidates = by_source_class_text.get((
                gold.get("source_id", ""),
                expected_class,
                _normalize_text(model_text),
            ), [])
            candidate = next((index for index in candidates if index not in used), None)
        if candidate is None and gold.get("char_start", ""):
            candidates = by_interval.get((
                gold.get("source_id", ""),
                expected_class,
                gold.get("char_start", ""),
                gold.get("char_end", ""),
            ), [])
            candidate = next((index for index in candidates if index not in used), None)
        if candidate is None:
            raise ValueError(
                "Gold记录无法回溯到review_queue: "
                f"{gold.get('source_id', '')}/{gold.get('extraction_id', '')}"
            )
        used.add(candidate)
        matched[candidate] = gold
    return matched, manual


def _metrics_by_class(rows: Sequence[dict[str, str]]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for row in rows:
        extraction_class = row.get("extraction_class", "")
        if extraction_class == "__overall__":
            continue
        output[extraction_class] = {
            key: int(row.get(key, "0") or 0)
            for key in (
                "model_rows",
                "gold_rows",
                "correct_rows",
                "modified_rows",
                "deleted_rows",
                "added_rows",
            )
        }
    return output


def _deletion_score(
    index: int,
    row: dict[str, str],
    duplicate_indices: set[int],
    cross_class_indices: set[int],
) -> int:
    score = 0
    if index in duplicate_indices:
        score += 8
    if index in cross_class_indices:
        score += 6
    if row.get("validation_status") in {"rejected", "coverage_unmatched"}:
        score += 4
    if row.get("grounding_status") == "mismatch":
        score += 3
    if not row.get("char_start") or not row.get("char_end"):
        score += 2
    return score


def _assign_unmatched_statuses(
    review_rows: Sequence[dict[str, str]],
    matched: dict[int, dict[str, str]],
    manual_gold: Sequence[dict[str, str]],
    metrics: dict[str, dict[str, int]],
    duplicate_indices: set[int],
    cross_class_indices: set[int],
) -> tuple[dict[int, str], list[dict[str, str]]]:
    observed: dict[str, Counter[str]] = defaultdict(Counter)
    for index, gold in matched.items():
        observed[review_rows[index].get("extraction_class", "")][
            gold.get("review_conclusion", "")
        ] += 1
    manual_by_class: dict[str, list[dict[str, str]]] = defaultdict(list)
    for gold in manual_gold:
        extraction_class = gold.get("extraction_class", "")
        manual_by_class[extraction_class].append(gold)
        observed[extraction_class][gold.get("review_conclusion", "")] += 1

    unmatched_by_class: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(review_rows):
        if index not in matched:
            unmatched_by_class[row.get("extraction_class", "")].append(index)

    assigned: dict[int, str] = {}
    missing_manual_rows: list[dict[str, str]] = []
    for extraction_class, metric in metrics.items():
        remaining = Counter({
            status: metric[column] - observed[extraction_class][status]
            for status, column in STATUS_TO_METRIC_COLUMN.items()
        })
        if any(value < 0 for value in remaining.values()):
            raise ValueError(f"{extraction_class} 的 Gold 结论数超过 metrics")

        missing_added = metric["added_rows"] - len(manual_by_class[extraction_class])
        for ordinal in range(1, missing_added + 1):
            status = next(
                (item for item in ("删除", "已修改", "正确") if remaining[item]),
                None,
            )
            if status is None:
                raise ValueError(f"{extraction_class} 无法对齐补录决策")
            remaining[status] -= 1
            digest = hashlib.sha256(
                f"{extraction_class}\0{ordinal}\0{status}".encode("utf-8")
            ).hexdigest()[:24]
            missing_manual_rows.append({
                "file_name": "",
                "source_id": "",
                "extraction_id": f"analysis_{digest}",
                "extraction_class": extraction_class,
                "record_origin": "manual",
                "review_conclusion": status,
                "model_text": "",
                "gold_text": "",
                "changed_fields": "record",
                "error_type": "人工新增",
                "error_description": (
                    "metrics记录了一条人工补录决策，但该条已删除，"
                    "Gold不保留行级明细。"
                ),
                "metric_class": extraction_class,
                "model_extraction_id": "",
                "review_note": "",
                "in_gold": "0",
            })

        indices = unmatched_by_class.get(extraction_class, [])
        if sum(remaining.values()) != len(indices):
            raise ValueError(
                f"{extraction_class} 无法对齐: 待分配{len(indices)}条，"
                f"metrics剩余{sum(remaining.values())}条"
            )
        ranked = sorted(
            indices,
            key=lambda index: _deletion_score(
                index,
                review_rows[index],
                duplicate_indices,
                cross_class_indices,
            ),
            reverse=True,
        )
        position = 0
        for status in ("删除", "已修改", "正确"):
            for _ in range(remaining[status]):
                assigned[ranked[position]] = status
                position += 1
    return assigned, missing_manual_rows


def _classify_error(
    *,
    raw: dict[str, str] | None,
    gold: dict[str, str] | None,
    origin: str,
    conclusion: str,
    changed_fields: Sequence[str],
    is_duplicate: bool,
    is_cross_class: bool,
) -> str:
    extraction_class = (gold or raw or {}).get("extraction_class", "")
    metric_class = (raw or gold or {}).get("extraction_class", "")
    if origin == "needs_review":
        return "coverage候选补回" if gold else "幻觉或非原文"
    if origin == "manual":
        if gold and extraction_class in {"event_time", "control_time"}:
            return "模型漏抽"
        return "人工新增"
    if conclusion == "正确":
        return "正确"
    if conclusion == "删除":
        if is_duplicate:
            return "重复抽取"
        if is_cross_class or metric_class in {"issuing_authority", "publication_date"}:
            return "类别误判"
        if raw and (
            raw.get("grounding_status") == "mismatch"
            or raw.get("validation_status") == "rejected"
            or not raw.get("char_start")
        ):
            return "幻觉或非原文"
        return "类别误判"
    if "extraction_class" in changed_fields:
        return "类别误判"
    if metric_class in {"event_time", "control_time"}:
        return "时间拆分错误"
    if "extraction_text" in changed_fields:
        return "文本范围错误"
    raw_values = _raw_fields(raw or {})
    gold_values = _gold_fields(gold or {})
    if any(
        not _normalize_value(field, raw_values.get(field, ""))
        and _normalize_value(field, gold_values.get(field, ""))
        for field in changed_fields
    ):
        return "属性缺失"
    return "属性值错误"


def _describe_error(
    error_type: str,
    raw: dict[str, str] | None,
    gold: dict[str, str] | None,
    changed_fields: Sequence[str],
) -> str:
    if error_type == "正确":
        return "人工复核确认模型结果可直接进入 Gold。"
    if error_type == "coverage候选补回":
        return (
            f"coverage 候选经人工确认并结构化为 "
            f"{CLASS_LABELS.get((gold or {}).get('extraction_class', ''), (gold or {}).get('extraction_class', ''))}。"
        )
    if error_type in {"模型漏抽", "人工新增"}:
        return "原始模型队列无对应可用记录，由人工补入或补齐。"
    if error_type == "重复抽取":
        return "同一公告内存在重复 ID 或重复文本候选，人工删除冗余项。"
    if error_type == "幻觉或非原文":
        return "候选未能稳定对齐原文，或经人工判定不应保留。"
    if error_type == "类别误判" and gold is None:
        return "候选的语义类别不成立，经人工复核删除。"
    if error_type == "时间拆分错误":
        if gold is None:
            return "原时间候选在人工重新拆分/合并后不再一一对应 Gold 记录。"
        return "人工复核重新确定了多阶段或多日期时间的边界及对应关系。"
    labels = "、".join(FIELD_LABELS.get(field, field) for field in changed_fields)
    if labels:
        return f"人工复核调整了：{labels}。"
    return "候选经人工复核删除或重新归类。"


def build_error_analysis(
    review_rows: Sequence[dict[str, str]],
    gold_rows: Sequence[dict[str, str]],
    metric_rows: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    metrics = _metrics_by_class(metric_rows)
    duplicate_indices = _duplicate_raw_indices(review_rows)
    cross_class_indices = _cross_class_text_indices(review_rows)
    matched, manual_gold = _match_gold_to_raw(review_rows, gold_rows)
    assigned, missing_manual = _assign_unmatched_statuses(
        review_rows,
        matched,
        manual_gold,
        metrics,
        duplicate_indices,
        cross_class_indices,
    )

    output: list[dict[str, str]] = []
    for index, raw in enumerate(review_rows):
        gold = matched.get(index)
        conclusion = (
            gold.get("review_conclusion", "") if gold else assigned.get(index, "")
        )
        if not conclusion:
            raise ValueError(f"原始队列第 {index + 2} 行没有复核结论")
        origin = (
            gold.get("record_origin", "")
            if gold
            else ("needs_review" if raw.get("extraction_class") == "needs_review" else "model")
        )
        changed = _changed_fields(raw, gold) if gold else ["record"]
        if conclusion == "已修改" and gold is None and raw.get(
            "extraction_class"
        ) in {"event_time", "control_time"}:
            changed = ["extraction_text"]
        error_type = _classify_error(
            raw=raw,
            gold=gold,
            origin=origin,
            conclusion=conclusion,
            changed_fields=changed,
            is_duplicate=index in duplicate_indices,
            is_cross_class=index in cross_class_indices,
        )
        output.append({
            "file_name": (gold or raw).get("file_name", ""),
            "source_id": (gold or raw).get("source_id", ""),
            "extraction_id": (gold or raw).get("extraction_id", ""),
            "extraction_class": (gold or raw).get("extraction_class", ""),
            "record_origin": origin,
            "review_conclusion": conclusion,
            "model_text": raw.get("extraction_text", ""),
            "gold_text": gold.get("extraction_text", "") if gold else "",
            "changed_fields": ";".join(changed),
            "error_type": error_type,
            "error_description": _describe_error(error_type, raw, gold, changed),
            "metric_class": raw.get("extraction_class", ""),
            "model_extraction_id": raw.get("extraction_id", ""),
            "review_note": gold.get("review_note", "") if gold else "",
            "in_gold": "1" if gold else "0",
        })

    for gold in manual_gold:
        changed = _all_gold_fields(gold)
        error_type = _classify_error(
            raw=None,
            gold=gold,
            origin="manual",
            conclusion=gold.get("review_conclusion", ""),
            changed_fields=changed,
            is_duplicate=False,
            is_cross_class=False,
        )
        output.append({
            "file_name": gold.get("file_name", ""),
            "source_id": gold.get("source_id", ""),
            "extraction_id": gold.get("extraction_id", ""),
            "extraction_class": gold.get("extraction_class", ""),
            "record_origin": "manual",
            "review_conclusion": gold.get("review_conclusion", ""),
            "model_text": "",
            "gold_text": gold.get("extraction_text", ""),
            "changed_fields": ";".join(changed),
            "error_type": error_type,
            "error_description": _describe_error(error_type, None, gold, changed),
            "metric_class": gold.get("extraction_class", ""),
            "model_extraction_id": "",
            "review_note": gold.get("review_note", ""),
            "in_gold": "1",
        })
    output.extend(missing_manual)
    return output


def _write_error_csv(rows: Iterable[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ERROR_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _md_text(value: str, limit: int = 100) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip().replace("|", "\\|")
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _example_table(rows: Sequence[dict[str, str]], limit: int = 5) -> str:
    if not rows:
        return "未发现对应案例。\n"
    lines = [
        "| 文件 | 类别 | 模型文本 | Gold文本 | 错误说明 |",
        "|---|---|---|---|---|",
    ]
    for row in rows[:limit]:
        lines.append(
            "| "
            + " | ".join((
                _md_text(row.get("file_name", ""), 24),
                _md_text(CLASS_LABELS.get(row.get("extraction_class", ""), row.get("extraction_class", "")), 20),
                _md_text(row.get("model_text", "")),
                _md_text(row.get("gold_text", "")),
                _md_text(row.get("error_description", "")),
            ))
            + " |"
        )
    return "\n".join(lines) + "\n"


def _cross_class_cases(review_rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in review_rows:
        if row.get("extraction_class") in {"control_rule", "traffic_guidance"}:
            groups[(
                row.get("source_id", ""),
                _normalize_text(row.get("extraction_text", "")),
            )].append(row)
    cases: list[dict[str, str]] = []
    for rows in groups.values():
        classes = {row.get("extraction_class", "") for row in rows}
        if classes == {"control_rule", "traffic_guidance"}:
            cases.append({
                "file_name": rows[0].get("file_name", ""),
                "text": rows[0].get("extraction_text", ""),
                "classes": "control_rule / traffic_guidance",
            })
    return cases


def build_report(
    analysis_rows: Sequence[dict[str, str]],
    review_rows: Sequence[dict[str, str]],
    gold_rows: Sequence[dict[str, str]],
    metric_rows: Sequence[dict[str, str]],
) -> str:
    metrics = _metrics_by_class(metric_rows)
    total_reviewed = sum(
        metric["correct_rows"] + metric["modified_rows"] + metric["deleted_rows"]
        for metric in metrics.values()
    )
    gold_linked = sum(row.get("in_gold") == "1" for row in analysis_rows)
    missing_types = {"模型漏抽", "coverage候选补回", "人工新增"}
    missing_by_class = Counter(
        row.get("extraction_class", "")
        for row in analysis_rows
        if row.get("in_gold") == "1" and row.get("error_type") in missing_types
    )

    lines = [
        "# 交通公告模型抽取错误分析与第一轮提示词校准建议",
        "",
        "> 本报告仅分析已有人工复核结果，未修改 Schema 2.0、Prompt、模型抽取逻辑或 Gold 数据。",
        "",
        "## 1. 数据对齐",
        "",
        f"- `review_queue.csv`：{len(review_rows)} 条原始候选。",
        f"- `gold_extractions.csv`：{len(gold_rows)} 条 Gold。",
        f"- metrics 复核决策：{total_reviewed} 条。",
        f"- `error_analysis.csv`：{len(analysis_rows)} 条，其中 {gold_linked} 条关联 Gold。",
        f"- 对齐检查：{'**通过**' if len(analysis_rows) == total_reviewed and gold_linked == len(gold_rows) else '**未通过**'}。",
        "",
        "## 2. 各类别复核结果",
        "",
        "| 类别 | 正确 | 修改 | 删除 | 漏抽补回 | Gold |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    total_correct = total_modified = total_deleted = total_missing = total_gold = 0
    for extraction_class, metric in metrics.items():
        missing = missing_by_class[extraction_class]
        lines.append(
            f"| {CLASS_LABELS.get(extraction_class, extraction_class)} | "
            f"{metric['correct_rows']} | {metric['modified_rows']} | "
            f"{metric['deleted_rows']} | {missing} | {metric['gold_rows']} |"
        )
        total_correct += metric["correct_rows"]
        total_modified += metric["modified_rows"]
        total_deleted += metric["deleted_rows"]
        total_missing += missing
        total_gold += metric["gold_rows"]
    lines.extend([
        f"| **合计** | **{total_correct}** | **{total_modified}** | **{total_deleted}** | **{total_missing}** | **{total_gold}** |",
        "",
        "说明：“漏抽补回”按最终目标类别统计 `模型漏抽`、`coverage候选补回`和 `人工新增` Gold 记录。",
        "",
        "## 3. 字段修改次数",
        "",
    ])
    field_counts: Counter[str] = Counter()
    for row in analysis_rows:
        if row.get("review_conclusion") != "已修改" or row.get("record_origin") != "model":
            continue
        field_counts.update(
            field for field in row.get("changed_fields", "").split(";") if field
        )
    lines.extend([
        "| 字段 | 修改次数 |",
        "|---|---:|",
    ])
    for field, count in field_counts.most_common():
        lines.append(f"| {FIELD_LABELS.get(field, field)} | {count} |")
    if not field_counts:
        lines.append("| 无 | 0 |")

    cross_cases = _cross_class_cases(review_rows)
    lines.extend([
        "",
        "## 4. 道路管制与交通引导混淆",
        "",
        f"发现 {len(cross_cases)} 组同一原文同时被归入 `control_rule` 和 `traffic_guidance` 的候选。",
        "",
    ])
    if cross_cases:
        lines.extend(["| 文件 | 重叠原文 | 候选类别 |", "|---|---|---|"])
        for case in cross_cases[:8]:
            lines.append(
                f"| {_md_text(case['file_name'], 24)} | {_md_text(case['text'])} | {case['classes']} |"
            )
    else:
        lines.append("未发现完全相同原文的跨类重叠。")

    event_time_errors = [
        row for row in analysis_rows
        if row.get("metric_class") == "event_time"
        and row.get("error_type") in {"时间拆分错误", "模型漏抽"}
    ]
    control_time_errors = [
        row for row in analysis_rows
        if row.get("metric_class") == "control_time"
        and row.get("error_type") == "时间拆分错误"
    ]
    lines.extend([
        "",
        "## 5. 多阶段活动时间与多日期管制时间",
        "",
        f"- 活动时间拆分/漏抽：{len(event_time_errors)} 条。",
        f"- 管制时间拆分/合并问题：{len(control_time_errors)} 条。",
        "",
        "### 典型时间案例",
        "",
        _example_table((event_time_errors + control_time_errors), 8).rstrip(),
    ])

    authority_date_errors = [
        row for row in analysis_rows
        if row.get("metric_class") in {"issuing_authority", "publication_date"}
        and row.get("review_conclusion") != "正确"
    ]
    lines.extend([
        "",
        "## 6. 发布机关与发布日期误判",
        "",
        f"共 {len(authority_date_errors)} 条非正确决策，主要风险是将正文中的部门提及、“特此公告”或活动日期误当发布信息。",
        "",
        _example_table(authority_date_errors, 6).rstrip(),
    ])

    duplicate_examples = [row for row in analysis_rows if row.get("error_type") == "重复抽取"]
    missing_examples = [
        row for row in analysis_rows
        if row.get("in_gold") == "1" and row.get("error_type") in missing_types
    ]
    lines.extend([
        "",
        "## 7. 重复抽取和漏抽典型案例",
        "",
        f"### 重复抽取（{len(duplicate_examples)} 条）",
        "",
        _example_table(duplicate_examples, 6).rstrip(),
        "",
        f"### 漏抽补回（{len(missing_examples)} 条）",
        "",
        _example_table(missing_examples, 8).rstrip(),
        "",
        "## 8. 第一轮提示词校准建议",
        "",
        "| 优先级 | 建议 | 针对证据 | 验收方式 |",
        "|---|---|---|---|",
        f"| P0 | 强化活动时间与管制时间的多阶段、多日期逐条拆分；禁止将整段活动期间等同于单条管制时间。 | 时间类问题 {len(event_time_errors) + len(control_time_errors)} 条 | 时间类修改率和人工补录数下降 |",
        f"| P0 | 明确“强制禁行/封闭/限行”与“绕行/停车/上下客建议”边界，同一证据仅在确有两类独立信息时才双重抽取。 | 跨类重叠 {len(cross_cases)} 组 | 跨类重叠和删除数下降 |",
        f"| P1 | 要求 control_rule/traffic_guidance 对每个属性逐项检查，特别是道路、时间、措施、车辆范围、路线和动作。 | 属性字段修改 {sum(field_counts.values())} 次 | 属性缺失率下降 |",
        f"| P1 | 发布机关仅接受明确落款/来源，发布日期仅接受发布语境；增加反例覆盖“特此公告”和活动日期。 | 机关/日期误判 {len(authority_date_errors)} 条 | 两类删除率下降 |",
        f"| P1 | 在输出前按类别+原文区间+文本去重，不得用无原文区间的改写文本代替证据。 | 重复 {len(duplicate_examples)} 条 | 重复 ID/文本候选归零 |",
        f"| P2 | 将 coverage 中频繁补回的表达形式补入 few-shot，但保留 coverage 作为漏抽安全网。 | coverage补回 {sum(row.get('error_type') == 'coverage候选补回' for row in analysis_rows)} 条 | coverage有效补回数下降且召回不降 |",
        "",
        "## 9. 错误类型口径",
        "",
        "`" + "`、`".join(ERROR_TYPES) + "`。",
        "",
        "其中 `属性值错误` 是在用户要求的最小类型集合上增补的细分类别，用于区分“原属性缺失”与“原属性已有但值错误”。",
    ])
    return "\n".join(lines) + "\n"


def _validate_alignment(
    analysis_rows: Sequence[dict[str, str]],
    gold_rows: Sequence[dict[str, str]],
    metric_rows: Sequence[dict[str, str]],
) -> None:
    metrics = _metrics_by_class(metric_rows)
    observed: dict[str, Counter[str]] = defaultdict(Counter)
    for row in analysis_rows:
        observed[row.get("metric_class", "")][row.get("review_conclusion", "")] += 1
    for extraction_class, metric in metrics.items():
        for status, column in STATUS_TO_METRIC_COLUMN.items():
            if observed[extraction_class][status] != metric[column]:
                raise ValueError(
                    f"{extraction_class}/{status} 不对齐: "
                    f"analysis={observed[extraction_class][status]}, metrics={metric[column]}"
                )
    if sum(row.get("in_gold") == "1" for row in analysis_rows) != len(gold_rows):
        raise ValueError("error_analysis 中的 Gold 关联数与 Gold 文件不一致")


def generate_analysis(
    review_queue_path: Path,
    gold_path: Path,
    metrics_path: Path,
    error_output: Path,
    report_output: Path,
) -> dict[str, int]:
    review_rows = _read_csv(review_queue_path)
    gold_rows = _read_csv(gold_path)
    metric_rows = _read_csv(metrics_path)
    analysis_rows = build_error_analysis(review_rows, gold_rows, metric_rows)
    _validate_alignment(analysis_rows, gold_rows, metric_rows)
    _write_error_csv(analysis_rows, error_output)
    report_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(
        build_report(analysis_rows, review_rows, gold_rows, metric_rows),
        encoding="utf-8",
    )
    return {
        "review_rows": len(review_rows),
        "gold_rows": len(gold_rows),
        "analysis_rows": len(analysis_rows),
        "gold_linked_rows": sum(row.get("in_gold") == "1" for row in analysis_rows),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于人工复核、Gold和metrics生成错误分析。"
    )
    parser.add_argument("--review-queue", type=Path, default=DEFAULT_REVIEW_QUEUE)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--error-output", type=Path, default=DEFAULT_ERROR_ANALYSIS)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    counts = generate_analysis(
        args.review_queue,
        args.gold,
        args.metrics,
        args.error_output,
        args.report_output,
    )
    print(f"已生成: {args.error_output} ({counts['analysis_rows']} 行)")
    print(f"已生成: {args.report_output}")
    print(
        f"对齐: review={counts['review_rows']}, Gold={counts['gold_rows']}, "
        f"Gold关联={counts['gold_linked_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
