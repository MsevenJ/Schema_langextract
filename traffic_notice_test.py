"""Extract structured facts from a Chinese traffic-control notice with Ollama.

Quick start (PowerShell):
    python traffic_notice_test.py --input traffic_notice.txt

The input file must be UTF-8 plain text. This script uses the local
``qwen2.5:7b`` model through Ollama and does not call a cloud API.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


try:
    from dotenv import load_dotenv
    import langextract as lx
    from langextract.core import data as core_data
    from langextract.core import exceptions as core_exceptions
    from langextract.core import format_handler as core_format_handler
except ModuleNotFoundError:
    venv_python = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file() and Path(sys.executable).resolve() != venv_python:
        completed = subprocess.run(
            [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
            check=False,
        )
        raise SystemExit(completed.returncode)
    raise


EXTRACTION_CLASSES = (
    "event_name",
    "event_venue",
    "control_time",
    "control_rule",
    "dynamic_adjustment",
    "travel_advice",
    "issuing_authority",
    "publication_date",
)


PROMPT = """
你负责从中文道路交通管制通告中抽取可回溯到原文的结构化信息。

只能使用以下 extraction_class：
- event_name：原文明确出现的活动、赛事或演唱会名称。
- event_venue：原文明确出现的活动场馆。
- control_time：交通管制开始、结束或持续时间的完整表述。
- control_rule：针对一条道路或一个路段的完整交通管制规定。
- dynamic_adjustment：针对整体管制措施、范围或时间的动态调整规定。
- travel_advice：面向市民或驾驶人的出行、绕行或服从现场指挥建议。
- issuing_authority：通告的发布机关，仅在原文明确写出发布机关或落款时抽取。
- publication_date：通告的发布日期，仅在原文明确写出发布日期时抽取。

必须遵守以下规则：
1. 只能抽取原文明确出现的信息，不得依靠常识推测或补全。
2. extraction_text 必须是原文中真实、连续、逐字完全一致的片段；不得改写、
   拼接不连续片段或生成原文没有的文字。按信息在原文中的出现顺序输出。
3. 不得猜测发布机关、发布日期和活动时间。提到交通警察、工作人员或某部门将
   采取措施，不等于该部门是发布机关。“特此通告”只是结束语，绝对不是发布
   机关。原文没有发布机关落款或明确发布主体时，不输出 issuing_authority。
4. event_time 与 control_time 不是一回事。本任务不抽取 event_time，只抽取原文
   明确表述的交通管制时间。不得把活动结束时间单独当作活动时间；如果它构成
   管制结束条件，应保留在完整的 control_time 中。
5. 每个 control_rule 必须覆盖针对同一道路的一条完整规定。若道路管制主句后面
   紧接着例外车辆、允许通行方式或禁止其他车辆的补充句，应把这些连续句子合并
   为同一个 control_rule 的 extraction_text，不得拆成 controlled_road、
   control_measure 或 affected_vehicle。
6. control_rule 的 attributes 使用以下键：road_name、road_range、direction、
   measure、affected_vehicle、exception_vehicle、exception_movement、condition。
   属性值必须由原文支持；原文没有的属性填写空字符串。条件封闭写入 condition，
   例外车辆写入 exception_vehicle，允许的通行方式写入 exception_movement。
   属性也要保留原文中的关键限定词；例如原文是“可借用……通行”，
   exception_movement 必须包含“可”，不得弱化为“借用……通行”。
7. “根据现场情况封闭”一类条件属于对应 control_rule，并在 condition 中保留。
   只有独立描述整体管制范围、时间或措施可能调整的句子才抽取为
   dynamic_adjustment；不要把同一句条件道路规定再重复抽成 dynamic_adjustment。
8. “尽量绕行管制区域”属于 travel_advice，不是具体的 detour_route。出行建议应
   保留为原文中的完整建议句。相邻的两条建议必须分别抽取，不得跨越序号、换行
   或分隔符拼接。只有原文明示具体道路时，travel_advice 才能使用 route 属性；
   “管制区域”不是具体路线。纯礼貌性致歉不抽取。
9. 必须逐字保留 extraction_text 末尾在原文中出现的句号或分号，不得把分号改成
   句号，也不得增删标点。
10. 不要输出本清单之外的类别，不要重复抽取同一信息。
11. 最终答案必须且只能是一个合法 JSON 对象，顶层只能有一个名为 extractions
    的键，其值必须是数组。不要在 JSON 前后输出解释、标题或 Markdown 代码围栏。
12. extractions 数组中的每个元素只表示一条抽取，格式必须是：
    {"类别名": "与原文逐字一致的连续片段", "类别名_attributes": {属性}}
    例如 control_rule 必须写成：
    {"control_rule": "原文片段", "control_rule_attributes": {"road_name": "道路名"}}
    同一类别有多条结果时，应在 extractions 数组中放入多个独立元素，绝对不要把
    control_rule 或其他类别改成嵌套数组。
13. 不得使用 event、event_date、event_time、alternative_routes、
    additional_information 等自创字段。原文没有某一类别时，直接省略该类别，
    不要填写“未提供”、空内容或根据上下文猜测。
""".strip()


EXAMPLES = [
    lx.data.ExampleData(
        text=(
            "为保障2026星海音乐节顺利举行，海滨体育中心周边道路将实施临时交通管制。"
            "2026年9月18日18时起至演出散场后，滨海大道（文体路至星光路段）"
            "由东向西方向封闭。除持证工作车辆可在现场民警指挥下借用对向车道通行外，"
            "禁止其他机动车通行。请市民提前规划路线，尽量绕行上述路段。\n\n"
            "滨江市公安局交通警察支队\n"
            "2026年9月16日"
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="event_name",
                extraction_text="2026星海音乐节",
                attributes={"event_type": "音乐节"},
            ),
            lx.data.Extraction(
                extraction_class="event_venue",
                extraction_text="海滨体育中心",
                attributes={"venue_type": "体育场馆"},
            ),
            lx.data.Extraction(
                extraction_class="control_time",
                extraction_text="2026年9月18日18时起至演出散场后",
                attributes={
                    "start_text": "2026年9月18日18时",
                    "end_text": "演出散场后",
                },
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text=(
                    "滨海大道（文体路至星光路段）由东向西方向封闭。"
                    "除持证工作车辆可在现场民警指挥下借用对向车道通行外，"
                    "禁止其他机动车通行。"
                ),
                attributes={
                    "road_name": "滨海大道",
                    "road_range": "文体路至星光路段",
                    "direction": "东向西",
                    "measure": "封闭；禁止其他机动车通行",
                    "affected_vehicle": "其他机动车",
                    "exception_vehicle": "持证工作车辆",
                    "exception_movement": "可在现场民警指挥下借用对向车道通行",
                    "condition": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="travel_advice",
                extraction_text="请市民提前规划路线，尽量绕行上述路段。",
                attributes={"advice_type": "提前规划并绕行"},
            ),
            lx.data.Extraction(
                extraction_class="issuing_authority",
                extraction_text="滨江市公安局交通警察支队",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="publication_date",
                extraction_text="2026年9月16日",
                attributes={},
            ),
        ],
    ),
    lx.data.ExampleData(
        text=(
            "为保障夏季城市展览会举办期间市会展中心周边交通安全，现通告如下："
            "2026年7月5日7时30分至12时，人民路（建设街至和平街段）"
            "南向北方向禁止机动车通行。环城路（东门桥至解放路段）届时根据现场"
            "交通流量实施临时封闭。根据人流、车流变化，公安交管部门将适时调整"
            "管制范围和时间；请过往车辆选择复兴路通行；驾驶人应服从现场交通警察"
            "指挥。"
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="event_name",
                extraction_text="夏季城市展览会",
                attributes={"event_type": "展览会"},
            ),
            lx.data.Extraction(
                extraction_class="event_venue",
                extraction_text="市会展中心",
                attributes={"venue_type": "会展场馆"},
            ),
            lx.data.Extraction(
                extraction_class="control_time",
                extraction_text="2026年7月5日7时30分至12时",
                attributes={
                    "start_text": "2026年7月5日7时30分",
                    "end_text": "12时",
                },
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text=(
                    "人民路（建设街至和平街段）南向北方向禁止机动车通行。"
                ),
                attributes={
                    "road_name": "人民路",
                    "road_range": "建设街至和平街段",
                    "direction": "南向北",
                    "measure": "禁止机动车通行",
                    "affected_vehicle": "机动车",
                    "exception_vehicle": "",
                    "exception_movement": "",
                    "condition": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text=(
                    "环城路（东门桥至解放路段）届时根据现场交通流量实施临时封闭。"
                ),
                attributes={
                    "road_name": "环城路",
                    "road_range": "东门桥至解放路段",
                    "direction": "",
                    "measure": "临时封闭",
                    "affected_vehicle": "",
                    "exception_vehicle": "",
                    "exception_movement": "",
                    "condition": "届时根据现场交通流量",
                },
            ),
            lx.data.Extraction(
                extraction_class="dynamic_adjustment",
                extraction_text=(
                    "根据人流、车流变化，公安交管部门将适时调整管制范围和时间；"
                ),
                attributes={
                    "condition": "根据人流、车流变化",
                    "adjustment": "适时调整管制范围和时间",
                },
            ),
            lx.data.Extraction(
                extraction_class="travel_advice",
                extraction_text="请过往车辆选择复兴路通行；",
                attributes={
                    "advice_type": "绕行建议",
                    "route": "复兴路",
                },
            ),
            lx.data.Extraction(
                extraction_class="travel_advice",
                extraction_text="驾驶人应服从现场交通警察指挥。",
                attributes={"advice_type": "服从现场指挥"},
            ),
        ],
    ),
]


class TrafficNoticeFormatHandler(core_format_handler.FormatHandler):
    """Parse normal LangExtract JSON and repair Qwen's category-grouped JSON."""

    def __init__(self) -> None:
        super().__init__(
            format_type=core_data.FormatType.JSON,
            use_wrapper=True,
            wrapper_key=core_data.EXTRACTIONS_KEY,
            use_fences=False,
        )
        self.normalized_response_count = 0
        self.raw_responses: list[str] = []

    def parse_output(
        self,
        text: str,
        *,
        strict: bool | None = None,
    ) -> list[dict[str, Any]]:
        self.raw_responses.append(text)
        try:
            return list(super().parse_output(text, strict=strict))
        except core_exceptions.FormatError as original_error:
            try:
                parsed = json.loads(self._extract_content(text))
            except (json.JSONDecodeError, TypeError, ValueError):
                raise original_error

            normalized = self._normalize_category_object(parsed)
            if not normalized:
                raise original_error

            self.normalized_response_count += 1
            repaired = json.dumps(
                {core_data.EXTRACTIONS_KEY: normalized},
                ensure_ascii=False,
            )
            return list(super().parse_output(repaired, strict=strict))

    @staticmethod
    def _normalize_category_object(parsed: Any) -> list[dict[str, Any]]:
        if not isinstance(parsed, dict):
            return []

        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(category: str, extraction_text: Any, attributes: Any) -> None:
            if (
                category not in EXTRACTION_CLASSES
                or not isinstance(extraction_text, str)
                or not extraction_text
            ):
                return
            signature = (category, extraction_text)
            if signature in seen:
                return
            seen.add(signature)
            if not isinstance(attributes, dict):
                attributes = {}
            normalized.append(
                {
                    category: extraction_text,
                    f"{category}_attributes": attributes,
                }
            )

        def add_category_value(
            category: str,
            value: Any,
            top_level_attributes: Any,
        ) -> None:
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if isinstance(entry, str):
                    extraction_text = entry
                    attributes = top_level_attributes
                elif isinstance(entry, dict):
                    extraction_text = entry.get(category)
                    attributes = entry.get(
                        f"{category}_attributes",
                        entry.get("attributes", top_level_attributes),
                    )
                else:
                    continue

                add(category, extraction_text, attributes)

        def walk_standard(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk_standard(item)
                return
            if not isinstance(value, dict):
                return

            extraction_class = value.get("extraction_class")
            if extraction_class in EXTRACTION_CLASSES:
                add(
                    extraction_class,
                    value.get("extraction_text"),
                    value.get("attributes", {}),
                )

            for category in EXTRACTION_CLASSES:
                if category in value:
                    add_category_value(
                        category,
                        value[category],
                        value.get(f"{category}_attributes", {}),
                    )

            for nested_value in value.values():
                if isinstance(nested_value, (dict, list)):
                    walk_standard(nested_value)

        walk_standard(parsed)

        # Qwen occasionally invents a category-oriented response even after
        # seeing valid LangExtract examples. Only map aliases whose semantics
        # match the requested classes; all items still undergo source alignment.
        add("event_venue", parsed.get("location"), {})
        add("issuing_authority", parsed.get("organizer"), {})

        legacy_rules = parsed.get("traffic_control_measures")
        if legacy_rules is None:
            legacy_rules = parsed.get("control_measures")
        if isinstance(legacy_rules, list):
            for entry in legacy_rules:
                if not isinstance(entry, dict):
                    continue
                rule_text = entry.get("control_rule", entry.get("control_measure"))
                raw_attributes = entry.get(
                    "control_rule_attributes",
                    entry.get("control_measure_attributes", {}),
                )
                attributes = TrafficNoticeFormatHandler._legacy_rule_attributes(
                    rule_text,
                    raw_attributes,
                )
                add("control_rule", rule_text, attributes)

        legacy_advice = parsed.get("advice")
        if isinstance(legacy_advice, list):
            for entry in legacy_advice:
                if isinstance(entry, str):
                    add("travel_advice", entry, {})
                elif isinstance(entry, dict):
                    advice_text = entry.get("travel_advice", entry.get("advice"))
                    attributes = {}
                    advice_type = entry.get("advice_type")
                    if isinstance(advice_type, str) and advice_type:
                        attributes["advice_type"] = advice_type
                    add("travel_advice", advice_text, attributes)

        return normalized

    @staticmethod
    def _legacy_rule_attributes(rule_text: Any, raw_attributes: Any) -> dict[str, str]:
        if not isinstance(rule_text, str):
            return {}
        if not isinstance(raw_attributes, dict):
            raw_attributes = {}

        road_description = raw_attributes.get("affected_road", "")
        if not isinstance(road_description, str):
            road_description = ""

        road_name = ""
        for suffix in ("大道", "公路", "大街", "街", "路", "桥", "隧道"):
            suffix_end = road_description.find(suffix)
            if suffix_end >= 0:
                road_name = road_description[: suffix_end + len(suffix)]
                break

        road_range = ""
        left = road_description.find("（")
        right = road_description.find("）", left + 1)
        if left >= 0 and right > left:
            road_range = road_description[left + 1 : right]

        direction = ""
        for candidate in (
            "东向西",
            "西向东",
            "南向北",
            "北向南",
            "双向",
        ):
            if candidate in rule_text:
                direction = candidate
                break

        measure = raw_attributes.get("restriction_type", "")
        if not isinstance(measure, str) or measure not in rule_text:
            measure = ""
            for candidate in (
                "禁止中重型货车通行",
                "禁止社会车辆驶入",
                "禁止车辆驶入",
                "禁止机动车通行",
                "禁止通行",
                "单向交通管制",
                "临时封闭",
                "封闭",
            ):
                if candidate in rule_text:
                    measure = candidate
                    break

        affected_vehicle = raw_attributes.get("affected_vehicle_type", "")
        if (
            not isinstance(affected_vehicle, str)
            or affected_vehicle not in rule_text
        ):
            affected_vehicle = ""

        condition = raw_attributes.get("adjustment", "")
        if not isinstance(condition, str) or condition not in rule_text:
            condition = ""

        return {
            "road_name": road_name,
            "road_range": road_range,
            "direction": direction,
            "measure": measure,
            "affected_vehicle": affected_vehicle,
            "exception_vehicle": "",
            "exception_movement": "",
            "condition": condition,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从中文交通管制通告中抽取结构化信息。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("traffic_notice.txt"),
        help="包含一份交通管制通告的 UTF-8 文本文件。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="原始结果、过滤结果和可视化文件的输出目录。",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        help="Ollama 模型名称，默认为 qwen2.5:7b。",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        help="Ollama 服务地址，默认为 http://127.0.0.1:11434。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("OLLAMA_TIMEOUT", "300")),
        help="单次 Ollama 请求超时秒数，默认为 300。",
    )
    return parser.parse_args()


def read_notice(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{path}")

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"输入文件为空：{path}")
    return text


def validate_extraction(
    extraction: lx.data.Extraction,
    source_text: str,
) -> tuple[list[str], str | None]:
    """Return rejection reasons and the located source text, if available."""
    reasons: list[str] = []
    extraction_text = extraction.extraction_text
    if not isinstance(extraction_text, str) or not extraction_text:
        reasons.append("extraction_text 为空")

    if (
        extraction.extraction_class == "issuing_authority"
        and isinstance(extraction_text, str)
        and extraction_text.strip(" \t\r\n。；：:")
        in {"特此通告", "现通告如下", "通告如下"}
    ):
        reasons.append("结束语不是发布机关")

    interval = extraction.char_interval
    located_text: str | None = None
    if interval is None:
        reasons.append("char_interval 不存在")
        return reasons, located_text

    start = interval.start_pos
    end = interval.end_pos
    if not isinstance(start, int) or not isinstance(end, int):
        reasons.append("char_interval 起止位置不是整数")
        return reasons, located_text

    if end <= start:
        reasons.append("定位区间长度不大于 0")

    in_bounds = start >= 0 and end <= len(source_text)
    if not in_bounds:
        reasons.append("定位区间超出原文范围")
    else:
        located_text = source_text[start:end]
        if located_text != extraction_text:
            reasons.append("定位出的原文与 extraction_text 不一致")

    return reasons, located_text


def serialize_extraction(extraction: lx.data.Extraction) -> dict[str, Any]:
    interval = extraction.char_interval
    alignment_status = extraction.alignment_status
    return {
        "extraction_class": extraction.extraction_class,
        "extraction_text": extraction.extraction_text,
        "attributes": extraction.attributes or {},
        "char_interval": (
            {
                "start_pos": interval.start_pos,
                "end_pos": interval.end_pos,
            }
            if interval is not None
            else None
        ),
        "alignment_status": (
            alignment_status.value
            if hasattr(alignment_status, "value")
            else alignment_status
        ),
        "extraction_index": extraction.extraction_index,
        "group_index": extraction.group_index,
        "description": extraction.description,
    }


def prepare_accepted_extraction(
    extraction: lx.data.Extraction,
) -> lx.data.Extraction:
    """Copy an accepted item and restore source-supported attribute wording."""
    accepted = copy.deepcopy(extraction)
    attributes = dict(accepted.attributes or {})

    if accepted.extraction_class == "control_rule":
        movement = attributes.get("exception_movement")
        if isinstance(movement, str) and movement:
            position = accepted.extraction_text.find(movement)
            if position >= 0:
                for prefix in ("允许", "可"):
                    prefix_start = position - len(prefix)
                    if (
                        prefix_start >= 0
                        and accepted.extraction_text[prefix_start:position]
                        == prefix
                        and not movement.startswith(prefix)
                    ):
                        attributes["exception_movement"] = prefix + movement
                        break

    if (
        accepted.extraction_class == "travel_advice"
        and attributes.get("route") in {"管制区域", "管控区域", "上述区域"}
    ):
        attributes.pop("route")

    accepted.attributes = attributes
    return accepted


def repair_grounding_from_source(
    extraction: lx.data.Extraction,
    source_text: str,
) -> lx.data.Extraction | None:
    """Repair only unambiguous alignment or terminal-punctuation differences."""
    extraction_text = extraction.extraction_text
    interval = extraction.char_interval
    if (
        not isinstance(extraction_text, str)
        or not extraction_text
        or interval is None
        or not isinstance(interval.start_pos, int)
        or not isinstance(interval.end_pos, int)
    ):
        return None

    # If the exact text occurs only once, correcting the interval is safe.
    exact_start = source_text.find(extraction_text)
    if exact_start >= 0 and source_text.find(extraction_text, exact_start + 1) < 0:
        repaired = copy.deepcopy(extraction)
        repaired.char_interval = lx.data.CharInterval(
            start_pos=exact_start,
            end_pos=exact_start + len(extraction_text),
        )
        return repaired

    start = interval.start_pos
    end = interval.end_pos
    if not (0 <= start < end <= len(source_text)):
        return None

    located_text = source_text[start:end]
    terminal_punctuation = "。；;！？!?"
    if (
        len(extraction_text) == len(located_text) + 1
        and extraction_text[:-1] == located_text
        and extraction_text[-1] in terminal_punctuation
        and end < len(source_text)
        and source_text[end] in terminal_punctuation
    ):
        repaired = copy.deepcopy(extraction)
        repaired.extraction_text = source_text[start : end + 1]
        repaired.char_interval = lx.data.CharInterval(
            start_pos=start,
            end_pos=end + 1,
        )
        return repaired

    return None


def complete_control_rule_from_source(
    extraction: lx.data.Extraction,
    source_text: str,
) -> lx.data.Extraction | None:
    """Expand a road-rule fragment to its complete numbered source line."""
    if extraction.extraction_class != "control_rule":
        return None
    interval = extraction.char_interval
    if interval is None or not isinstance(interval.start_pos, int):
        return None
    if not (0 <= interval.start_pos < len(source_text)):
        return None

    attributes = extraction.attributes or {}
    road_name = attributes.get("road_name")
    if not isinstance(road_name, str) or not road_name:
        return None

    line_start = source_text.rfind("\n", 0, interval.start_pos) + 1
    line_end = source_text.find("\n", interval.start_pos)
    if line_end < 0:
        line_end = len(source_text)
    source_line = source_text[line_start:line_end]

    prefix = re.match(
        r"^\s*(?:[（(][一二三四五六七八九十0-9]+[）)]|[0-9]+[.、])\s*",
        source_line,
    )
    content_offset = prefix.end() if prefix else len(source_line) - len(source_line.lstrip())
    content = source_line[content_offset:].rstrip()
    if not content or road_name not in content:
        return None
    if not any(
        marker in content
        for marker in ("禁止", "封闭", "管制", "通行", "停放", "绕行")
    ):
        return None
    if content == extraction.extraction_text:
        return None

    repaired = copy.deepcopy(extraction)
    repaired.extraction_text = content
    repaired.char_interval = lx.data.CharInterval(
        start_pos=line_start + content_offset,
        end_pos=line_start + content_offset + len(content),
    )

    repaired_attributes = dict(attributes)
    for condition in (
        "视现场实际情况调整",
        "根据现场交通流量情况",
        "视情况",
        "届时根据现场情况",
    ):
        if condition in content:
            repaired_attributes["condition"] = condition
            break
    repaired.attributes = repaired_attributes
    return repaired


def split_extractions(
    extractions: list[lx.data.Extraction],
    source_text: str,
) -> tuple[list[lx.data.Extraction], list[dict[str, Any]], int]:
    accepted: list[lx.data.Extraction] = []
    rejected: list[dict[str, Any]] = []
    repaired_count = 0

    for extraction in extractions:
        candidate = extraction
        completed_rule = complete_control_rule_from_source(
            extraction,
            source_text,
        )
        if completed_rule is not None:
            candidate = completed_rule
            repaired_count += 1

        reasons, located_text = validate_extraction(candidate, source_text)
        if reasons:
            repaired = repair_grounding_from_source(candidate, source_text)
            if repaired is not None:
                repaired_reasons, repaired_located_text = validate_extraction(
                    repaired,
                    source_text,
                )
                if not repaired_reasons:
                    candidate = repaired
                    reasons = repaired_reasons
                    located_text = repaired_located_text
                    repaired_count += 1

        if reasons:
            rejected.append(
                {
                    "reasons": reasons,
                    "located_text": located_text,
                    "extraction": serialize_extraction(extraction),
                }
            )
        else:
            accepted.append(prepare_accepted_extraction(candidate))

    return accepted, rejected, repaired_count


def make_document(
    result: lx.data.AnnotatedDocument,
    source_text: str,
    extractions: list[lx.data.Extraction],
) -> lx.data.AnnotatedDocument:
    return lx.data.AnnotatedDocument(
        document_id=result.document_id,
        text=source_text,
        extractions=extractions,
    )


def save_model_responses(
    format_handler: TrafficNoticeFormatHandler,
    input_path: Path,
    output_dir: Path,
) -> Path | None:
    if not format_handler.raw_responses:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{input_path.stem}_model_responses.json"
    payload = {
        "input_file": str(input_path.resolve()),
        "response_count": len(format_handler.raw_responses),
        "responses": [
            {"index": index, "content": content}
            for index, content in enumerate(
                format_handler.raw_responses,
                start=1,
            )
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def save_results(
    result: lx.data.AnnotatedDocument,
    source_text: str,
    all_extractions: list[lx.data.Extraction],
    accepted: list[lx.data.Extraction],
    rejected: list[dict[str, Any]],
    input_path: Path,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = input_path.stem
    paths = {
        "raw": output_dir / f"{prefix}_raw.jsonl",
        "accepted": output_dir / f"{prefix}_accepted.jsonl",
        "rejected": output_dir / f"{prefix}_rejected.json",
        "visualization": output_dir / f"{prefix}_visualization.html",
    }

    raw_document = make_document(result, source_text, all_extractions)
    accepted_document = make_document(result, source_text, accepted)
    lx.io.save_annotated_documents(
        [raw_document],
        output_name=paths["raw"].name,
        output_dir=output_dir,
        show_progress=False,
    )
    lx.io.save_annotated_documents(
        [accepted_document],
        output_name=paths["accepted"].name,
        output_dir=output_dir,
        show_progress=False,
    )

    rejected_payload = {
        "input_file": str(input_path.resolve()),
        "document_id": result.document_id,
        "source_length": len(source_text),
        "raw_count": len(all_extractions),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "rejections": rejected,
    }
    paths["rejected"].write_text(
        json.dumps(rejected_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    html_content = lx.visualize(accepted_document)
    if hasattr(html_content, "data"):
        html_content = html_content.data
    paths["visualization"].write_text(str(html_content), encoding="utf-8")
    return paths


def print_accepted_results(accepted: list[lx.data.Extraction]) -> None:
    grouped: dict[str, list[lx.data.Extraction]] = {}
    for extraction in accepted:
        grouped.setdefault(extraction.extraction_class, []).append(extraction)

    ordered_categories = list(EXTRACTION_CLASSES)
    ordered_categories.extend(
        sorted(category for category in grouped if category not in EXTRACTION_CLASSES)
    )
    for category in ordered_categories:
        items = grouped.get(category, [])
        if not items:
            continue
        print(f"\n=== {category}（{len(items)}）===")
        for index, extraction in enumerate(items, start=1):
            interval = extraction.char_interval
            print(f"[{index}]")
            print(f"  类型：{extraction.extraction_class}")
            print(f"  原文：{extraction.extraction_text}")
            print(
                "  属性："
                + json.dumps(
                    extraction.attributes or {},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            print(
                f"  原文起止位置：[{interval.start_pos}, {interval.end_pos})"
            )


def print_summary(
    all_extractions: list[lx.data.Extraction],
    accepted: list[lx.data.Extraction],
    rejected: list[dict[str, Any]],
    repaired_count: int,
) -> None:
    counts = Counter(item.extraction_class for item in accepted)
    print("\n=== 统计 ===")
    print(f"原始数量：{len(all_extractions)}")
    print(f"保留数量：{len(accepted)}")
    print(f"拒绝数量：{len(rejected)}")
    print(f"原文定位修复数量：{repaired_count}")
    print("各类别数量：")
    for category in EXTRACTION_CLASSES:
        print(f"  {category}: {counts.get(category, 0)}")
    for category in sorted(set(counts) - set(EXTRACTION_CLASSES)):
        print(f"  {category}: {counts[category]}")


def main() -> int:
    load_dotenv()
    args = parse_args()
    response_format_handler: TrafficNoticeFormatHandler | None = None

    try:
        notice_text = read_notice(args.input)

        # Keep provider selection explicit so no cloud provider can be chosen.
        model_config = lx.factory.ModelConfig(
            model_id=args.model,
            provider="OllamaLanguageModel",
            provider_kwargs={
                "model_url": args.ollama_url,
                "temperature": 0.0,
                "timeout": args.timeout,
            },
        )
        response_format_handler = TrafficNoticeFormatHandler()

        result = lx.extract(
            text_or_documents=notice_text,
            prompt_description=PROMPT,
            examples=EXAMPLES,
            config=model_config,
            extraction_passes=1,
            max_char_buffer=1500,
            max_workers=1,
            resolver_params={
                "format_handler": response_format_handler,
                "suppress_parse_errors": False,
            },
            tokenizer=lx.tokenizer.UnicodeTokenizer(),
        )
        model_response_path = save_model_responses(
            response_format_handler,
            args.input,
            args.output_dir,
        )

        all_extractions = list(result.extractions or [])
        if not all_extractions:
            raise RuntimeError("模型没有返回任何可解析的原始抽取结果")
        accepted, rejected, repaired_count = split_extractions(
            all_extractions,
            notice_text,
        )

        print(f"模型：{args.model}")
        print(f"Ollama：{args.ollama_url}")
        if response_format_handler.normalized_response_count:
            print(
                "输出格式修复："
                f"{response_format_handler.normalized_response_count} 个模型回复"
                "已转换为 LangExtract 格式"
            )
        print_accepted_results(accepted)
        print_summary(all_extractions, accepted, rejected, repaired_count)

        paths = save_results(
            result=result,
            source_text=notice_text,
            all_extractions=all_extractions,
            accepted=accepted,
            rejected=rejected,
            input_path=args.input,
            output_dir=args.output_dir,
        )
        print("\n输出文件：")
        print(f"  原始结果：{paths['raw'].resolve()}")
        print(f"  保留结果：{paths['accepted'].resolve()}")
        print(f"  拒绝结果：{paths['rejected'].resolve()}")
        print(f"  可视化：{paths['visualization'].resolve()}")
        if model_response_path is not None:
            print(f"  模型原始回复：{model_response_path.resolve()}")
        return 0

    except Exception as exc:
        model_response_path = (
            save_model_responses(
                response_format_handler,
                args.input,
                args.output_dir,
            )
            if response_format_handler is not None
            else None
        )
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        if model_response_path is not None:
            print(
                f"模型原始回复：{model_response_path.resolve()}",
                file=sys.stderr,
            )
        print(
            "排查提示：请确认 Ollama 正在运行，并已安装模型 "
            f"{args.model}。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
