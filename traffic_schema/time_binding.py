"""Conservative control-rule time inheritance."""

from __future__ import annotations

from dataclasses import dataclass
import re


WEB_METADATA_MARKERS = (
    "发布时间",
    "来源",
    "浏览次数",
    "字体",
    "分享",
)

CONTROL_CONTEXT_MARKERS = (
    "管制",
    "管控",
    "禁行",
    "禁止通行",
    "进场",
    "散场",
    "离场",
    "封闭",
    "限行",
)

_TIME_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:展会期间|演出期间|活动期间)?(?:每天|每日)"
        r"\d{1,2}(?::\d{2}|时(?:\d{1,2}分)?)[-—至]"
        r"\d{1,2}(?::\d{2}|时(?:\d{1,2}分)?)",
        r"(?:\d{4}年)?\d{1,2}月\d{1,2}日[^，。；：\n]{0,35}"
        r"(?:至|—|-)[^，。；：\n]{1,30}",
        r"(?:每天|每日)?\d{1,2}时(?:\d{1,2}分)?(?:至|—|-)"
        r"\d{1,2}时(?:\d{1,2}分)?",
        r"(?:进场|散场|离场|演唱会结束|展会|活动)期间"
        r"(?:（[^）]{1,30}）)?",
        r"(?:每日|每天)?(?:进场|散场|进散场)高峰时段",
        r"\d{1,2}:\d{2}[-—至]\d{1,2}:\d{2}",
    )
)

_LEVEL_ONE_RE = re.compile(r"^\s*[一二三四五六七八九十]+[、.]\s*")
_LEVEL_TWO_RE = re.compile(r"^\s*[（(][一二三四五六七八九十0-9]+[）)]\s*")
_LEVEL_THREE_RE = re.compile(r"^\s*(?:\d+[.、]|[①②③④⑤⑥⑦⑧⑨⑩])\s*")


@dataclass(frozen=True)
class SourceLine:
    text: str
    start: int
    end: int
    level: int


def is_web_metadata_line(line: str) -> bool:
    stripped = line.strip()
    if any(marker in stripped for marker in WEB_METADATA_MARKERS):
        return True
    return bool(
        re.match(r"^时间[：:]\s*\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?", stripped)
    )


def is_web_metadata_time(source_text: str, time_text: str) -> bool:
    """True when every literal occurrence is confined to web metadata lines."""
    if not time_text:
        return False
    found = False
    search_start = 0
    while True:
        position = source_text.find(time_text, search_start)
        if position < 0:
            break
        found = True
        line_start = source_text.rfind("\n", 0, position) + 1
        line_end = source_text.find("\n", position)
        if line_end < 0:
            line_end = len(source_text)
        if not is_web_metadata_line(source_text[line_start:line_end]):
            return False
        search_start = position + max(1, len(time_text))
    return found


def heading_level(line: str) -> int:
    if _LEVEL_ONE_RE.match(line):
        return 1
    if _LEVEL_TWO_RE.match(line):
        return 2
    if _LEVEL_THREE_RE.match(line):
        return 3
    return 0


def source_lines(source_text: str) -> list[SourceLine]:
    lines: list[SourceLine] = []
    offset = 0
    for raw_line in source_text.splitlines(keepends=True):
        text = raw_line.rstrip("\r\n")
        lines.append(SourceLine(text, offset, offset + len(text), heading_level(text)))
        offset += len(raw_line)
    if not lines or offset < len(source_text):
        text = source_text[offset:]
        lines.append(SourceLine(text, offset, len(source_text), heading_level(text)))
    return lines


def extract_literal_time(text: str) -> str:
    if is_web_metadata_line(text):
        return ""
    for group in re.findall(r"[（(]([^（）()]{1,50})[）)]", text):
        if any(marker in group for marker in ("时", ":", "期间", "时段")):
            return group.strip()
    for pattern in _TIME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    if re.match(r"^\s*时间[：:]", text):
        value = re.split(r"时间[：:]", text, maxsplit=1)[1].strip(" \t；;。")
        return value
    return ""


def _line_index_at(lines: list[SourceLine], position: int) -> int:
    for index, line in enumerate(lines):
        if line.start <= position <= line.end:
            return index
    return max(0, len(lines) - 1)


def find_inheritable_time(source_text: str, rule_start: int) -> str:
    """Find a time only inside the immediately containing numbered section."""
    lines = source_lines(source_text)
    if not lines:
        return ""
    current_index = _line_index_at(lines, rule_start)
    current_level = lines[current_index].level or 4

    for index in range(current_index - 1, -1, -1):
        line = lines[index]
        stripped = line.text.strip()
        if not stripped or is_web_metadata_line(line.text):
            continue

        time_text = extract_literal_time(line.text)
        has_control_context = any(
            marker in line.text for marker in CONTROL_CONTEXT_MARKERS
        )
        is_explicit_time_line = bool(re.match(r"^\s*时间[：:]", line.text))
        if time_text and (has_control_context or is_explicit_time_line):
            return time_text

        if line.level:
            # A sibling or a new higher-level section is a hard boundary.
            if line.level >= current_level:
                return ""
            # A direct parent may itself carry a phase time; otherwise do not
            # cross it into a previous section.
            if time_text and has_control_context:
                return time_text
            return ""

    return ""
