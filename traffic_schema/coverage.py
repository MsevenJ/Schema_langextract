"""Offline source-provision coverage audit; never creates extractions."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Iterable

from .ids import evidence_id
from .time_binding import heading_level, source_lines


SECTION_KEYWORDS = (
    "管制",
    "管控",
    "禁行",
    "入场路线",
    "散场路线",
    "离场路线",
    "参展",
    "离场",
    "注意事项",
    "绕行路线",
    "停车场",
    "公共交通",
    "公交",
    "地铁",
    "上下客",
    "出行提示",
)

PROVISION_KEYWORDS = (
    "管制",
    "封闭",
    "禁行",
    "禁止",
    "限行",
    "通行",
    "绕行",
    "驶入",
    "驶出",
    "停车",
    "停放",
    "公交",
    "地铁",
    "轨道",
    "上下客",
    "即停即走",
    "提前",
    "服从",
)

_ROAD_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9·]{1,24}(?:大道|公路|大街|辅道|快速路|街|路|桥|隧道)"
)
_NUMBERED_RE = re.compile(
    r"^\s*(?:[一二三四五六七八九十]+[、.]|"
    r"[（(][一二三四五六七八九十0-9]+[）)]|"
    r"\d+[.、]|[①②③④⑤⑥⑦⑧⑨⑩])"
)


def _class_hint(text: str) -> str:
    if any(word in text for word in ("绕行", "入场路线", "散场路线", "离场路线")):
        return "traffic_guidance"
    if any(word in text for word in ("停车场", "公交", "地铁", "上下客", "即停即走")):
        return "traffic_guidance"
    if any(word in text for word in ("管制", "封闭", "禁止", "限行", "通行")):
        return "control_rule"
    return "needs_review"


def _interval(extraction: Any) -> tuple[int | None, int | None]:
    interval = getattr(extraction, "char_interval", None)
    if interval is None:
        return None, None
    return getattr(interval, "start_pos", None), getattr(interval, "end_pos", None)


def _overlaps(
    left_start: int,
    left_end: int,
    right_start: int | None,
    right_end: int | None,
) -> bool:
    if right_start is None or right_end is None:
        return False
    return max(left_start, right_start) < min(left_end, right_end)


def audit_coverage(
    source_text: str,
    accepted_extractions: Iterable[Any],
    stable_source_id: str,
) -> dict[str, Any]:
    """Compare line-level candidate provisions with accepted char intervals."""
    accepted = list(accepted_extractions)
    lines = source_lines(source_text)
    source_sections: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    active_section = ""

    for line in lines:
        stripped = line.text.strip()
        if not stripped:
            continue
        level = heading_level(line.text)
        heading_like = bool(
            (
                level in {1, 2}
                and len(stripped) <= 40
                and not stripped.endswith(("。", "；", ";", "！", "？"))
            )
            or stripped.endswith(("通告", "公告"))
        )
        if heading_like:
            active_section = ""
        is_section = bool(
            heading_like and any(word in stripped for word in SECTION_KEYWORDS)
        )
        if is_section:
            active_section = stripped
            source_sections.append({
                "text": stripped,
                "char_start": line.start,
                "char_end": line.end,
                "level": level,
            })

        numbered = bool(_NUMBERED_RE.match(line.text))
        has_keyword = any(word in stripped for word in PROVISION_KEYWORDS)
        road_names = list(dict.fromkeys(_ROAD_RE.findall(stripped)))
        heading_only = heading_like
        in_relevant_section = any(
            word in active_section for word in SECTION_KEYWORDS
        )
        if heading_only or not (
            has_keyword or road_names or (numbered and in_relevant_section)
        ):
            continue

        matched_ids: list[str] = []
        for extraction in accepted:
            extraction_start, extraction_end = _interval(extraction)
            if _overlaps(line.start, line.end, extraction_start, extraction_end):
                extraction_identifier = getattr(extraction, "_extraction_id", "")
                if extraction_identifier:
                    matched_ids.append(extraction_identifier)

        candidate = {
            "candidate_id": evidence_id(stable_source_id, line.start, line.end),
            "section": active_section,
            "text": line.text,
            "char_start": line.start,
            "char_end": line.end,
            "road_names": road_names,
            "class_hint": _class_hint(stripped),
            "matched_extraction_ids": matched_ids,
            "coverage_status": "matched" if matched_ids else "unmatched",
        }
        candidates.append(candidate)

    matched = [item for item in candidates if item["coverage_status"] == "matched"]
    unmatched = [item for item in candidates if item["coverage_status"] == "unmatched"]
    coverage_rate = len(matched) / len(candidates) if candidates else 1.0
    return {
        "source_id": stable_source_id,
        "source_sections": source_sections,
        "candidate_provisions": candidates,
        "matched_provisions": matched,
        "unmatched_provisions": unmatched,
        "coverage_rate": round(coverage_rate, 6),
        "needs_review": bool(unmatched),
    }


def save_coverage_report(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
