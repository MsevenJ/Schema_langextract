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
import csv
from dataclasses import dataclass
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


SCHEMA_VERSION = "2.0"

EXTRACTION_CLASSES = (
    "issuing_authority",
    "publication_date",
    "event_name",
    "event_venue",
    "event_time",
    "control_time",
    "control_rule",
    "traffic_guidance",
)

GUIDANCE_TYPES = frozenset({
    "detour",
    "access",
    "egress",
    "parking",
    "drop_off",
    "public_transport",
    "general_advice",
})

CONTROL_RULE_ATTRIBUTE_KEYS = (
    "road_names",
    "road_range",
    "direction",
    "time_text",
    "measure",
    "vehicle_scope",
    "condition",
    "exception",
)

TRAFFIC_GUIDANCE_ATTRIBUTE_KEYS = (
    "guidance_type",
    "route_or_location",
    "time_text",
    "vehicle_scope",
    "action",
    "condition",
)


PROMPT = """
你负责从中文活动交通通告中抽取可回溯到原文的结构化信息。

只能使用以下 extraction_class：
- issuing_authority：通告的发布机关，仅在原文明确写出发布机关或落款时抽取。
- publication_date：通告的发布日期，可精确到日，也可只精确到月。
- event_name：原文明确出现的活动、演出、展会或比赛名称，包括正文中的名称。
- event_venue：原文明确出现的活动场馆或举办地点。
- event_time：活动、演出、展会或比赛的举办时间。
- control_time：道路交通管制的总体时段或进场、散场等分阶段管制时段。
- control_rule：封闭、禁行、单向通行、车辆限行、禁止停车、临时交通组织等
  具有强制性的道路交通管理规定。
- traffic_guidance：绕行、入场、疏散、停车场、上下客、公交调整和一般出行
  建议等非道路强制管制类交通组织信息。

必须遵守以下规则：
1. 只能抽取原文明确出现的信息，不得依靠常识推测、补全、改写或计算时间。
2. extraction_text 必须是原文中真实、连续、逐字完全一致的片段；不得拼接
   不连续片段。按信息在原文中的出现顺序输出，逐字保留原文标点。
3. event_time 与 control_time 必须严格区分。活动“举行、举办、开演、比赛”的
   时间属于 event_time；道路“管制、封闭、禁行、限行”的时间属于 control_time。
   不得仅因两个时间接近就把活动时间当作管制时间。
4. publication_date 只有完整日期时使用 {"precision":"day"}；原文只有
   “2027年6月”这类月份时使用 {"precision":"month"}。原文没有发布日期时
   不输出。不得把活动日期当发布日期。
5. 不得猜测发布机关。提到公安交管部门将采取措施，不等于它是发布机关。
   “特此通告”“来源”“发布时间”“浏览次数”“分享”等网页文字不是发布机关。
6. 每个 control_rule 只覆盖一条完整规定。若主句后紧接例外车辆、允许通行方式
   或同一规则的补充句，应保留这些连续句子；不要合并不同编号的独立规则。
7. control_rule 的 attributes 必须且只能使用：road_names、road_range、direction、
   time_text、measure、vehicle_scope、condition、exception。road_names 必须是字符串
   数组，其余均为字符串；缺失值用空数组或空字符串。
8. 每条 control_rule 的 time_text 必须填写其对应的原文管制时段。时间写在规则
   句内时逐字复制；写在本条上方的小标题、段首或章节标题时继承该原文时间。
   不得转换成 ISO 时间，不得推算。原文确无明确时间才填写空字符串。
9. “根据现场情况”“视情”“适时启用”等写入 control_rule.condition；特殊车辆、
   持证车辆、允许通行方式等写入 control_rule.exception。
10. traffic_guidance 的 attributes 必须且只能使用：guidance_type、
    route_or_location、time_text、vehicle_scope、action、condition，缺失值为空字符串。
    guidance_type 只能是 detour、access、egress、parking、drop_off、
    public_transport、general_advice 之一。
11. 绕行路线用 detour，入场路线用 access，散场或离场路线用 egress，停车场和
    允许停车信息用 parking，上下客点用 drop_off，公交地铁调整用
    public_transport，一般提示用 general_advice。严格禁行、封闭、限行和禁止停车
    始终属于 control_rule，不能归为 traffic_guidance。
12. 不再使用 dynamic_adjustment 或 travel_advice。动态条件并入对应
    control_rule.condition，一般提示并入 traffic_guidance。
13. 不要输出本清单之外的类别，不要重复抽取同一信息，不要输出“未提供”。
14. 最终答案必须且只能是一个合法 JSON 对象，顶层只能有一个名为 extractions
    的键，其值必须是数组。不要在 JSON 前后输出解释、标题或 Markdown 代码围栏。
15. extractions 数组中的每个元素只表示一条抽取，格式必须是：
    {"类别名": "与原文逐字一致的连续片段", "类别名_attributes": {属性}}
    同一类别有多条时放入多个独立元素，不能把类别改成嵌套数组。
""".strip()


EXAMPLES = [
    lx.data.ExampleData(
        text=(
            "海州市公安局交通警察支队关于音乐会期间交通管制的通告\n\n"
            "“银河回声”世界巡回音乐会海州站将于2027年5月2日19时30分在海州体育馆举行。"
            "交通管制时间为5月2日16时至演出结束。\n\n"
            "（一）进场管制（16时至19时）\n"
            "1.湖滨大道（迎宾路至体育路段）东向西封闭，除持证保障车辆外，禁止其他车辆通行。\n"
            "2.育才路（新城街至湖滨大道段）禁止机动车驶入。\n\n"
            "（二）散场管制（21时30分至散场结束）\n"
            "体育路视现场车流情况由南向北实行单向通行。\n\n"
            "社会车辆可绕行迎宾路、新城街；驾驶人请服从现场交警指挥。\n\n"
            "海州市公安局交通警察支队\n"
            "2027年4月28日"
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="issuing_authority",
                extraction_text="海州市公安局交通警察支队",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="publication_date",
                extraction_text="2027年4月28日",
                attributes={"precision": "day"},
            ),
            lx.data.Extraction(
                extraction_class="event_name",
                extraction_text="“银河回声”世界巡回音乐会海州站",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="event_venue",
                extraction_text="海州体育馆",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="event_time",
                extraction_text="2027年5月2日19时30分",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="control_time",
                extraction_text="5月2日16时至演出结束",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="control_time",
                extraction_text="16时至19时",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="control_time",
                extraction_text="21时30分至散场结束",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text=(
                    "湖滨大道（迎宾路至体育路段）东向西封闭，除持证保障车辆外，"
                    "禁止其他车辆通行。"
                ),
                attributes={
                    "road_names": ["湖滨大道"],
                    "road_range": "迎宾路至体育路段",
                    "direction": "东向西",
                    "time_text": "16时至19时",
                    "measure": "封闭；禁止其他车辆通行",
                    "vehicle_scope": "其他车辆",
                    "condition": "",
                    "exception": "持证保障车辆",
                },
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text="育才路（新城街至湖滨大道段）禁止机动车驶入。",
                attributes={
                    "road_names": ["育才路"],
                    "road_range": "新城街至湖滨大道段",
                    "direction": "",
                    "time_text": "16时至19时",
                    "measure": "禁止机动车驶入",
                    "vehicle_scope": "机动车",
                    "condition": "",
                    "exception": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text="体育路视现场车流情况由南向北实行单向通行。",
                attributes={
                    "road_names": ["体育路"],
                    "road_range": "",
                    "direction": "南向北",
                    "time_text": "21时30分至散场结束",
                    "measure": "单向通行",
                    "vehicle_scope": "",
                    "condition": "视现场车流情况",
                    "exception": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="traffic_guidance",
                extraction_text="社会车辆可绕行迎宾路、新城街；",
                attributes={
                    "guidance_type": "detour",
                    "route_or_location": "迎宾路、新城街",
                    "time_text": "",
                    "vehicle_scope": "社会车辆",
                    "action": "可绕行",
                    "condition": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="traffic_guidance",
                extraction_text="驾驶人请服从现场交警指挥。",
                attributes={
                    "guidance_type": "general_advice",
                    "route_or_location": "",
                    "time_text": "",
                    "vehicle_scope": "驾驶人",
                    "action": "服从现场交警指挥",
                    "condition": "",
                },
            ),
        ],
    ),
    lx.data.ExampleData(
        text=(
            "第十二届云帆装备博览会交通组织通告\n\n"
            "第十二届云帆装备博览会将于2027年6月10日至12日每日9时至17时在云港国际会展中心举办。\n\n"
            "一、进场管制（每日7时30分至10时）\n"
            "港城大道（海川路至会展路段）双向禁止中重型货车通行。"
            "会展南路全线禁止车辆停放。\n\n"
            "二、散场管制（每日16时30分至19时）\n"
            "滨河路适时启用东向西单向通行。\n\n"
            "观众车辆可经海川路、港城大道进入P1停车场；离场车辆从会展东路驶往环湾快速路。"
            "P1、P2停车场供社会车辆停放。出租车、网约车在会展北门即停即走。"
            "观众可乘地铁二号线至会展站。\n\n"
            "云港市公安局交通管理支队\n"
            "2027年6月"
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="issuing_authority",
                extraction_text="云港市公安局交通管理支队",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="publication_date",
                extraction_text="2027年6月",
                attributes={"precision": "month"},
            ),
            lx.data.Extraction(
                extraction_class="event_name",
                extraction_text="第十二届云帆装备博览会",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="event_venue",
                extraction_text="云港国际会展中心",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="event_time",
                extraction_text="2027年6月10日至12日每日9时至17时",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="control_time",
                extraction_text="每日7时30分至10时",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="control_time",
                extraction_text="每日16时30分至19时",
                attributes={},
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text="港城大道（海川路至会展路段）双向禁止中重型货车通行。",
                attributes={
                    "road_names": ["港城大道"],
                    "road_range": "海川路至会展路段",
                    "direction": "双向",
                    "time_text": "每日7时30分至10时",
                    "measure": "禁止中重型货车通行",
                    "vehicle_scope": "中重型货车",
                    "condition": "",
                    "exception": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text="会展南路全线禁止车辆停放。",
                attributes={
                    "road_names": ["会展南路"],
                    "road_range": "全线",
                    "direction": "",
                    "time_text": "每日7时30分至10时",
                    "measure": "禁止车辆停放",
                    "vehicle_scope": "车辆",
                    "condition": "",
                    "exception": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="control_rule",
                extraction_text="滨河路适时启用东向西单向通行。",
                attributes={
                    "road_names": ["滨河路"],
                    "road_range": "",
                    "direction": "东向西",
                    "time_text": "每日16时30分至19时",
                    "measure": "单向通行",
                    "vehicle_scope": "",
                    "condition": "适时启用",
                    "exception": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="traffic_guidance",
                extraction_text="观众车辆可经海川路、港城大道进入P1停车场；",
                attributes={
                    "guidance_type": "access",
                    "route_or_location": "海川路、港城大道、P1停车场",
                    "time_text": "",
                    "vehicle_scope": "观众车辆",
                    "action": "进入",
                    "condition": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="traffic_guidance",
                extraction_text="离场车辆从会展东路驶往环湾快速路。",
                attributes={
                    "guidance_type": "egress",
                    "route_or_location": "会展东路、环湾快速路",
                    "time_text": "",
                    "vehicle_scope": "离场车辆",
                    "action": "驶往",
                    "condition": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="traffic_guidance",
                extraction_text="P1、P2停车场供社会车辆停放。",
                attributes={
                    "guidance_type": "parking",
                    "route_or_location": "P1、P2停车场",
                    "time_text": "",
                    "vehicle_scope": "社会车辆",
                    "action": "停放",
                    "condition": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="traffic_guidance",
                extraction_text="出租车、网约车在会展北门即停即走。",
                attributes={
                    "guidance_type": "drop_off",
                    "route_or_location": "会展北门",
                    "time_text": "",
                    "vehicle_scope": "出租车、网约车",
                    "action": "即停即走",
                    "condition": "",
                },
            ),
            lx.data.Extraction(
                extraction_class="traffic_guidance",
                extraction_text="观众可乘地铁二号线至会展站。",
                attributes={
                    "guidance_type": "public_transport",
                    "route_or_location": "地铁二号线、会展站",
                    "time_text": "",
                    "vehicle_scope": "观众",
                    "action": "乘地铁二号线至会展站",
                    "condition": "",
                },
            ),
        ],
    ),
]


EVENT_METADATA_CLASSES = frozenset({
    "event_name",
    "event_venue",
    "event_time",
})

NOTICE_METADATA_CLASSES = frozenset({
    "issuing_authority",
    "publication_date",
    "control_time",
})

EVENT_METADATA_PROMPT = """
从中文活动交通通告中只抽取 event_name、event_venue、event_time。

逐句检查包含“举行”“举办”“开演”的句子，把明确出现的活动或演唱会名称、
活动场馆、活动举办时间分别输出为独立 extraction。活动名称可能在正文中，应
保留“巡回演唱会”“博览会”等完整文字。event_time 仅表示活动举办时间，绝不能
把道路封闭、禁行、管制时段当作 event_time。

每个 JSON 数组元素只放一个类别，类别名必须严格使用 event_name、event_venue、
event_time，不得改成 event、venue、event_date、date 或 time。

每个 extraction_text 必须是原文中连续、逐字完全一致的片段，不得改写、拼接、
推算或输出“未提供”。原文没有的类别不要输出。
最终只能输出 {"extractions":[...]} JSON 对象，不得输出其他字段或说明。
""".strip()

EVENT_METADATA_EXAMPLES = [
    lx.data.ExampleData(
        text=example.text,
        extractions=[
            extraction
            for extraction in example.extractions
            if extraction.extraction_class in EVENT_METADATA_CLASSES
        ],
    )
    for example in EXAMPLES
]

NOTICE_METADATA_PROMPT = """
从中文活动交通通告中只抽取 issuing_authority、publication_date、control_time。

issuing_authority 仅限原文明确出现的发布机关。“来源”“发布时间”“浏览次数”、
网站名称和文章标题不是发布机关。publication_date 仅限正式发布日期：完整日期
添加 precision=day，只有“某年某月”时添加 precision=month；网页元数据里的时间
不得当作正式发布日期。control_time 是道路封闭、禁行、限行或其他交通管制的
总体或分阶段时段，不得把活动举办时间当作 control_time。原文没有的类别不要输出。

每个 JSON 数组元素只放一个类别，类别名必须严格使用 issuing_authority、
publication_date、control_time。extraction_text 必须是原文中连续、逐字完全一致
的片段，不得改写、拼接、推算或输出“未提供”。
最终只能输出 {"extractions":[...]} JSON 对象，不得输出其他字段或说明。
""".strip()

NOTICE_METADATA_EXAMPLES = [
    lx.data.ExampleData(
        text=example.text,
        extractions=[
            extraction
            for extraction in example.extractions
            if extraction.extraction_class in NOTICE_METADATA_CLASSES
        ],
    )
    for example in EXAMPLES
]


CONTROL_RULE_PROMPT = """
从中文活动交通通告中只抽取 control_rule。control_rule 是具有强制性的道路交通
管理规定，包括封闭、禁行、车辆限行、禁止停车、单向通行、只出不进、临时交通
组织及条件管制。绕行、入场、离场、停车场位置、上下客、公交调整和一般建议不是
control_rule，本次不要输出它们。

每条 extraction 必须覆盖一条完整且独立的规定，不要合并不同编号。属性必须包含
road_names、road_range、direction、time_text、measure、vehicle_scope、condition、
exception。章节标题、分组标题或本条上方明确给出的管制时间必须逐字继承到
time_text；没有明确时间时填空字符串。视情、根据现场情况、适时启用写入
condition，特殊车辆及允许通行情形写入 exception。只能抽取原文明示信息。

extraction_text 必须是原文中连续、逐字完全一致的文字，不得改写、拼接或补造。
最终只能输出 {"extractions":[...]} JSON 对象，不得输出其他字段或说明。
""".strip()

CONTROL_RULE_EXAMPLES = [
    lx.data.ExampleData(
        text=example.text,
        extractions=[
            extraction
            for extraction in example.extractions
            if extraction.extraction_class == "control_rule"
        ],
    )
    for example in EXAMPLES
]


ROUTE_GUIDANCE_TYPES = frozenset({"detour", "access", "egress"})
SERVICE_GUIDANCE_TYPES = frozenset({
    "parking",
    "drop_off",
    "public_transport",
    "general_advice",
})

ROUTE_GUIDANCE_PROMPT = """
从中文活动交通通告中只抽取绕行、入场、散场或离场路线，统一使用
traffic_guidance，guidance_type 只能是 detour、access、egress。停车、上下客、
公交地铁和一般提示本次不要输出。封闭、禁行、限行、禁止停车、单向通行、
只出不进等强制道路规定也不要输出。

属性必须包含 guidance_type、route_or_location、time_text、vehicle_scope、action、
condition。时间必须逐字复制原文，没有明确时间时填空字符串。每条 extraction
只覆盖一条完整路线，只能抽取原文明示信息。

extraction_text 必须是原文中连续、逐字完全一致的文字，不得改写、拼接或补造。
最终只能输出 {"extractions":[...]} JSON 对象，不得输出其他字段或说明。
""".strip()

ROUTE_GUIDANCE_EXAMPLES = [
    lx.data.ExampleData(
        text=example.text,
        extractions=[
            extraction
            for extraction in example.extractions
            if (
                extraction.extraction_class == "traffic_guidance"
                and (extraction.attributes or {}).get("guidance_type")
                in ROUTE_GUIDANCE_TYPES
            )
        ],
    )
    for example in EXAMPLES
]

SERVICE_GUIDANCE_PROMPT = """
从中文活动交通通告中只抽取停车场及停车管理、出租车网约车等车辆上下客、公交
地铁或临时站点调整，以及提前出行、服从指挥等一般建议，统一使用
traffic_guidance。guidance_type 只能是 parking、drop_off、public_transport、
general_advice。绕行、入场、散场或离场路线本次不要输出。封闭、禁行、限行、
禁止停车、单向通行、只出不进等强制道路规定也不要输出。

属性必须包含 guidance_type、route_or_location、time_text、vehicle_scope、action、
condition。时间必须逐字复制原文，没有明确时间时填空字符串。每条 extraction
只覆盖一条完整组织信息或建议，只能抽取原文明示信息。

extraction_text 必须是原文中连续、逐字完全一致的文字，不得改写、拼接或补造。
最终只能输出 {"extractions":[...]} JSON 对象，不得输出其他字段或说明。
""".strip()

SERVICE_GUIDANCE_EXAMPLES = [
    lx.data.ExampleData(
        text=example.text,
        extractions=[
            extraction
            for extraction in example.extractions
            if (
                extraction.extraction_class == "traffic_guidance"
                and (extraction.attributes or {}).get("guidance_type")
                in SERVICE_GUIDANCE_TYPES
            )
        ],
    )
    for example in EXAMPLES
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
            parsed_items = list(super().parse_output(text, strict=strict))
            normalized = self._normalize_category_object(
                {core_data.EXTRACTIONS_KEY: parsed_items}
            )
            if normalized:
                if normalized != parsed_items:
                    self.normalized_response_count += 1
                return normalized
            return parsed_items
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
            if category == "control_rule" and isinstance(extraction_text, str):
                strict_markers = (
                    "禁止",
                    "封闭",
                    "管制",
                    "限行",
                    "单向通行",
                    "只出不进",
                    "严禁",
                )
                guidance_markers = (
                    "绕行",
                    "驶出",
                    "入场",
                    "离场",
                    "停车场",
                    "允许机动车有序单排停放",
                    "上下客",
                    "即停即走",
                    "公交",
                    "地铁",
                )
                if (
                    not any(marker in extraction_text for marker in strict_markers)
                    and any(marker in extraction_text for marker in guidance_markers)
                ):
                    category = "traffic_guidance"
                    _, attributes = TrafficNoticeFormatHandler._legacy_guidance(
                        {
                            "traffic_guidance": extraction_text,
                            "attributes": attributes,
                        },
                        "general_advice",
                    )
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

                if category == "control_rule":
                    attributes = TrafficNoticeFormatHandler._legacy_rule_attributes(
                        extraction_text,
                        attributes,
                    )
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
                attributes = value.get("attributes", {})
                if extraction_class == "control_rule":
                    attributes = TrafficNoticeFormatHandler._legacy_rule_attributes(
                        value.get("extraction_text"),
                        attributes,
                    )
                add(
                    extraction_class,
                    value.get("extraction_text"),
                    attributes,
                )

            for category in EXTRACTION_CLASSES:
                if category in value:
                    add_category_value(
                        category,
                        value[category],
                        value.get(f"{category}_attributes", {}),
                    )

            nested_aliases = {
                "event": "event_name",
                "activity_name": "event_name",
                "concert_name": "event_name",
                "venue": "event_venue",
                "event_location": "event_venue",
                "event_date": "event_time",
                "activity_time": "event_time",
                "performance_time": "event_time",
                "traffic_control_time": "control_time",
                "control_period": "control_time",
                "organizer": "issuing_authority",
                "publisher": "issuing_authority",
                "issuing_body": "issuing_authority",
                "publish_date": "publication_date",
                "release_date": "publication_date",
            }
            for alias, category in nested_aliases.items():
                if alias in value:
                    add(category, value.get(alias), {})

            if any(
                alias in value
                for alias in ("event", "activity_name", "concert_name")
            ):
                for alias in ("location", "date", "time"):
                    if alias not in value:
                        continue
                    category = "event_venue" if alias == "location" else "event_time"
                    add(category, value.get(alias), {})

            for nested_value in value.values():
                if isinstance(nested_value, (dict, list)):
                    walk_standard(nested_value)

        walk_standard(parsed)

        # Qwen occasionally invents a category-oriented response even after
        # seeing valid LangExtract examples. Only map aliases whose semantics
        # match the requested classes; all items still undergo source alignment.
        for alias in ("event", "activity_name", "concert_name"):
            add("event_name", parsed.get(alias), {})
        for alias in ("venue", "event_location"):
            add("event_venue", parsed.get(alias), {})
        for alias in ("event_date", "activity_time", "performance_time"):
            add("event_time", parsed.get(alias), {})
        for alias in ("traffic_control_time", "control_period"):
            add("control_time", parsed.get(alias), {})
        for alias in ("organizer", "publisher", "issuing_body"):
            add("issuing_authority", parsed.get(alias), {})
        for alias in ("publish_date", "release_date"):
            add("publication_date", parsed.get(alias), {})

        rule_collection_keys = {
            "traffic_control",
            "traffic_control_measures",
            "control_measures",
            "control_rules",
            "traffic_rules",
        }
        guidance_collections = {
            "advice": "general_advice",
            "travel_advice": "general_advice",
            "general_advice": "general_advice",
            "alternative_routes": "detour",
            "detour_routes": "detour",
            "entry_routes": "access",
            "access_routes": "access",
            "exit_routes": "egress",
            "egress_routes": "egress",
            "parking_lots": "parking",
            "parking_information": "parking",
            "drop_off_points": "drop_off",
            "public_transport": "public_transport",
            "transit_information": "public_transport",
        }

        def walk_aliases(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk_aliases(item)
                return
            if not isinstance(value, dict):
                return

            for key, collection in value.items():
                if key in rule_collection_keys:
                    entries = collection if isinstance(collection, list) else [collection]
                    for entry in entries:
                        if isinstance(entry, str):
                            add(
                                "control_rule",
                                entry,
                                TrafficNoticeFormatHandler._legacy_rule_attributes(
                                    entry,
                                    {},
                                ),
                            )
                        elif isinstance(entry, dict):
                            rule_text = entry.get(
                                "control_rule",
                                entry.get(
                                    "control_measure",
                                    entry.get(
                                        "traffic_control",
                                        entry.get("rule", entry.get("text")),
                                    ),
                                ),
                            )
                            raw_attributes = entry.get(
                                "control_rule_attributes",
                                entry.get(
                                    "control_measure_attributes",
                                    entry.get(
                                        "traffic_control_attributes",
                                        entry.get("attributes", {}),
                                    ),
                                ),
                            )
                            if (
                                isinstance(raw_attributes, dict)
                                and raw_attributes.get("control_type") == "绕行"
                            ):
                                guidance_text, guidance_attributes = (
                                    TrafficNoticeFormatHandler._legacy_guidance(
                                        {
                                            "text": rule_text,
                                            "attributes": raw_attributes,
                                        },
                                        "detour",
                                    )
                                )
                                add(
                                    "traffic_guidance",
                                    guidance_text,
                                    guidance_attributes,
                                )
                                continue
                            add(
                                "control_rule",
                                rule_text,
                                TrafficNoticeFormatHandler._legacy_rule_attributes(
                                    rule_text,
                                    raw_attributes,
                                ),
                            )

                if key in guidance_collections:
                    entries = collection if isinstance(collection, list) else [collection]
                    for entry in entries:
                        if isinstance(entry, str):
                            sibling_attributes = value.get(f"{key}_attributes", {})
                            if isinstance(sibling_attributes, dict):
                                entry = {
                                    "text": entry,
                                    "attributes": sibling_attributes,
                                }
                        text_value, attributes = (
                            TrafficNoticeFormatHandler._legacy_guidance(
                                entry,
                                guidance_collections[key],
                            )
                        )
                        add("traffic_guidance", text_value, attributes)

                if (
                    key not in rule_collection_keys
                    and key not in guidance_collections
                    and isinstance(collection, (dict, list))
                ):
                    walk_aliases(collection)

        walk_aliases(parsed)

        return normalized

    @staticmethod
    def _legacy_rule_attributes(rule_text: Any, raw_attributes: Any) -> dict[str, Any]:
        if not isinstance(rule_text, str):
            return {}
        if not isinstance(raw_attributes, dict):
            raw_attributes = {}

        road_description = raw_attributes.get(
            "affected_road",
            raw_attributes.get(
                "road_name",
                raw_attributes.get(
                    "route_or_location",
                    raw_attributes.get("affected_areas", ""),
                ),
            ),
        )
        if not isinstance(road_description, str):
            road_description = ""

        road_names = raw_attributes.get("road_names", [])
        if isinstance(road_names, str):
            road_names = [road_names]
        elif not isinstance(road_names, list):
            road_names = []
        road_names = [item for item in road_names if isinstance(item, str) and item]
        if not road_names:
            road_name = raw_attributes.get("road_name", "")
            if isinstance(road_name, str) and road_name:
                road_names = [road_name]
        if not road_names:
            for suffix in ("大道", "公路", "大街", "街", "路", "桥", "隧道"):
                suffix_end = road_description.find(suffix)
                if suffix_end >= 0:
                    road_names = [road_description[: suffix_end + len(suffix)]]
                    break

        road_range = raw_attributes.get("road_range", "")
        if not isinstance(road_range, str):
            road_range = ""
        left = road_description.find("（")
        right = road_description.find("）", left + 1)
        if not road_range and left >= 0 and right > left:
            road_range = road_description[left + 1 : right]

        direction = raw_attributes.get("direction", "")
        if not isinstance(direction, str) or not direction:
            direction = ""
            for candidate in ("东向西", "西向东", "南向北", "北向南", "双向"):
                if candidate in rule_text:
                    direction = candidate
                    break

        measure = raw_attributes.get(
            "measure",
            raw_attributes.get(
                "restriction_type",
                raw_attributes.get("action", raw_attributes.get("control_type", "")),
            ),
        )
        if not isinstance(measure, str):
            measure = ""
        if not measure:
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

        vehicle_scope = raw_attributes.get(
            "vehicle_scope",
            raw_attributes.get(
                "affected_vehicle",
                raw_attributes.get("affected_vehicle_type", ""),
            ),
        )
        if not isinstance(vehicle_scope, str):
            vehicle_scope = ""

        condition = raw_attributes.get(
            "condition",
            raw_attributes.get("adjustment", ""),
        )
        if not isinstance(condition, str):
            condition = ""

        time_text = raw_attributes.get(
            "time_text",
            raw_attributes.get("time_period", ""),
        )
        if not isinstance(time_text, str):
            time_text = ""

        exception_parts = []
        for key in ("exception", "exception_vehicle", "exception_movement"):
            value = raw_attributes.get(key, "")
            if isinstance(value, str) and value and value not in exception_parts:
                exception_parts.append(value)

        return {
            "road_names": road_names,
            "road_range": road_range,
            "direction": direction,
            "time_text": time_text,
            "measure": measure,
            "vehicle_scope": vehicle_scope,
            "condition": condition,
            "exception": "；".join(exception_parts),
        }

    @staticmethod
    def _legacy_guidance(
        entry: Any,
        default_type: str,
    ) -> tuple[str | None, dict[str, str]]:
        if isinstance(entry, str):
            text_value = entry
            raw_attributes: dict[str, Any] = {}
        elif isinstance(entry, dict):
            text_value = None
            for key in (
                "traffic_guidance",
                "travel_advice",
                "advice",
                "route",
                "parking_lot",
                "details",
                "text",
                "description",
            ):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    text_value = value
                    break
            raw_attributes = entry.get(
                "traffic_guidance_attributes",
                entry.get(
                    "travel_advice_attributes",
                    entry.get("advice_attributes", entry.get("attributes", entry)),
                ),
            )
            if not isinstance(raw_attributes, dict):
                raw_attributes = {}
        else:
            return None, {}

        if not isinstance(text_value, str) or not text_value:
            return None, {}
        if ("禁止停车" in text_value or "严禁停车" in text_value) and default_type == "parking":
            return None, {}

        guidance_type = raw_attributes.get("guidance_type", default_type)
        if guidance_type not in GUIDANCE_TYPES:
            guidance_type = default_type
        if guidance_type == "general_advice":
            if "绕行" in text_value:
                guidance_type = "detour"
            elif any(marker in text_value for marker in ("入场", "进入")):
                guidance_type = "access"
            elif any(marker in text_value for marker in ("散场", "离场", "驶出")):
                guidance_type = "egress"
            elif any(marker in text_value for marker in ("停车场", "停放")):
                guidance_type = "parking"
            elif any(marker in text_value for marker in ("上下客", "即停即走")):
                guidance_type = "drop_off"
            elif any(marker in text_value for marker in ("公交", "地铁", "轨道")):
                guidance_type = "public_transport"

        route_or_location = raw_attributes.get(
            "route_or_location",
            raw_attributes.get(
                "route",
                raw_attributes.get("parking_lot", raw_attributes.get("location", "")),
            ),
        )
        if not isinstance(route_or_location, str):
            route_or_location = ""

        def text_attribute(*keys: str) -> str:
            for key in keys:
                value = raw_attributes.get(key)
                if isinstance(value, str):
                    return value
            return ""

        return text_value, {
            "guidance_type": guidance_type,
            "route_or_location": route_or_location,
            "time_text": text_attribute("time_text", "time_period"),
            "vehicle_scope": text_attribute(
                "vehicle_scope",
                "affected_vehicle",
                "affected_vehicle_type",
            ),
            "action": text_attribute("action", "measure"),
            "condition": text_attribute("condition", "adjustment"),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从中文交通管制通告中抽取结构化信息。"
    )
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--input",
        type=Path,
        help="包含一份交通管制通告的 UTF-8 文本文件，默认 traffic_notice.txt。",
    )
    input_group.add_argument(
        "--input-dir",
        type=Path,
        help="批量处理该目录第一层的全部 .txt 文件，按文件名顺序串行执行。",
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
        default=int(os.getenv("OLLAMA_TIMEOUT", "600")),
        help="单次 Ollama 请求超时秒数，默认为 600。",
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

    if extraction.extraction_class not in EXTRACTION_CLASSES:
        reasons.append("extraction_class 不属于 Schema 2.0")

    if isinstance(extraction_text, str):
        stripped_text = extraction_text.strip(" \t\r\n。；：:")
        web_noise = ("来源", "发布时间", "浏览次数", "分享", "字体")
        if extraction.extraction_class == "issuing_authority":
            if stripped_text in {
                "特此通告",
                "现通告如下",
                "通告如下",
                "特此公告",
                "本网",
                "本网站",
            }:
                reasons.append("该文本不是发布机关")
            if any(marker in extraction_text for marker in web_noise):
                reasons.append("网页字段不是发布机关")
        if extraction.extraction_class == "publication_date":
            if any(marker in extraction_text for marker in web_noise):
                reasons.append("发布日期不能包含网页字段文字")
            if not re.fullmatch(r"\d{4}年\d{1,2}月(?:\d{1,2}日)?", stripped_text):
                reasons.append("发布日期必须是原文中的年月或年月日")

    attributes = extraction.attributes or {}
    if extraction.extraction_class == "control_rule":
        road_names = attributes.get("road_names")
        if not isinstance(road_names, list) or any(
            not isinstance(item, str) for item in road_names
        ):
            reasons.append("control_rule.road_names 必须是字符串数组")
        time_text = attributes.get("time_text")
        if not isinstance(time_text, str):
            reasons.append("control_rule.time_text 必须是字符串")
        elif time_text and time_text not in source_text:
            reasons.append("control_rule.time_text 未在原文出现")
        if not any(
            marker in (extraction_text or "")
            for marker in (
                "禁止",
                "封闭",
                "管制",
                "限行",
                "单向通行",
                "只出不进",
                "严禁",
            )
        ):
            reasons.append("control_rule 不包含强制性交通管理措施")
    if extraction.extraction_class == "traffic_guidance":
        guidance_type = attributes.get("guidance_type")
        if guidance_type not in GUIDANCE_TYPES:
            reasons.append("traffic_guidance.guidance_type 不在允许列表中")
        if any(
            marker in (extraction_text or "")
            for marker in ("禁止通行", "禁止驶入", "封闭", "严禁停车", "车辆限行")
        ):
            reasons.append("强制性道路管制不能归入 traffic_guidance")
        time_text = attributes.get("time_text")
        if not isinstance(time_text, str):
            reasons.append("traffic_guidance.time_text 必须是字符串")
        elif time_text and time_text not in source_text:
            reasons.append("traffic_guidance.time_text 未在原文出现")

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
        context_prefix = source_text[max(0, start - 16) : start]
        if extraction.extraction_class == "issuing_authority" and any(
            marker in context_prefix for marker in ("来源：", "来源:", "信息来源")
        ):
            reasons.append("网页来源字段不是发布机关")
        if extraction.extraction_class == "publication_date" and any(
            marker in context_prefix
            for marker in ("发布时间：", "发布时间:", "浏览时间：", "浏览时间:")
        ):
            reasons.append("网页发布时间不是正式发布日期")

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


def infer_guidance_type(extraction_text: str) -> str:
    if "绕行" in extraction_text:
        return "detour"
    if any(marker in extraction_text for marker in ("入场", "进入", "前往")):
        return "access"
    if any(marker in extraction_text for marker in ("散场", "离场", "驶出")):
        return "egress"
    if any(marker in extraction_text for marker in ("停车场", "停放", "停车位")):
        return "parking"
    if any(marker in extraction_text for marker in ("上下客", "即停即走", "下客")):
        return "drop_off"
    if any(marker in extraction_text for marker in ("公交", "地铁", "轨道", "临时站")):
        return "public_transport"
    return "general_advice"


def is_strict_control_text(extraction_text: str) -> bool:
    """Distinguish mandatory road controls from advisory traffic guidance."""
    return any(
        marker in extraction_text
        for marker in (
            "禁止",
            "不得进入",
            "封闭",
            "限行",
            "单向通行",
            "单向交通",
            "只出不进",
            "严禁停车",
            "实施交通管制",
            "实施临时交通管制",
            "临时交通管理措施",
            "临时交通控流",
        )
    )


def normalize_extraction_attributes(
    extraction: lx.data.Extraction,
) -> lx.data.Extraction:
    """Copy an extraction and normalize attributes to Schema 2.0."""
    normalized = copy.deepcopy(extraction)
    attributes = dict(normalized.attributes or {})

    if (
        normalized.extraction_class == "traffic_guidance"
        and is_strict_control_text(normalized.extraction_text)
    ):
        # Qwen sometimes puts mandatory controls under its guidance bucket.
        # Reclassify only the model-extracted, source-grounded span; never
        # synthesize a new span from attributes.
        normalized.extraction_class = "control_rule"

    if normalized.extraction_class == "control_rule":
        attributes = TrafficNoticeFormatHandler._legacy_rule_attributes(
            normalized.extraction_text,
            attributes,
        )
        road_names: list[str] = []
        for item in attributes.get("road_names", []):
            if not isinstance(item, str):
                continue
            for road_name in item.split("、"):
                road_name = road_name.strip()
                if (
                    road_name
                    and road_name in normalized.extraction_text
                    and road_name not in road_names
                ):
                    road_names.append(road_name)
        attributes["road_names"] = road_names

        road_range = attributes.get("road_range", "")
        if not isinstance(road_range, str) or road_range not in normalized.extraction_text:
            road_range = ""
            range_match = re.search(r"[（(]([^（）()]+)[）)]", normalized.extraction_text)
            if range_match:
                road_range = range_match.group(1)
        attributes["road_range"] = road_range

        for key in CONTROL_RULE_ATTRIBUTE_KEYS:
            if key == "road_names":
                continue
            value = attributes.get(key, "")
            attributes[key] = value if isinstance(value, str) else ""

        for key in (
            "direction",
            "measure",
            "vehicle_scope",
            "condition",
            "exception",
        ):
            if attributes[key] and attributes[key] not in normalized.extraction_text:
                attributes[key] = ""

        if attributes["vehicle_scope"] and not any(
            marker in attributes["vehicle_scope"]
            for marker in ("车", "机动车", "货车", "非机动车", "行人", "人员")
        ):
            attributes["vehicle_scope"] = ""
        if attributes["condition"] and not any(
            marker in attributes["condition"]
            for marker in ("根据", "视", "适时", "届时", "当", "遇", "高峰", "期间")
        ):
            attributes["condition"] = ""

        if not attributes["condition"]:
            for pattern in (
                r"(?:届时将)?根据[^，。；]{0,20}情况",
                r"视[^，。；]{0,20}情况",
                r"视情",
                r"适时启用",
            ):
                match = re.search(pattern, normalized.extraction_text)
                if match:
                    attributes["condition"] = match.group(0)
                    break
        if not attributes["exception"]:
            exception_match = re.search(r"除[^。；]+?外", normalized.extraction_text)
            if exception_match:
                attributes["exception"] = exception_match.group(0)
        attributes = {
            key: attributes[key]
            for key in CONTROL_RULE_ATTRIBUTE_KEYS
        }

    elif normalized.extraction_class == "traffic_guidance":
        guidance_type = attributes.get("guidance_type")
        if guidance_type not in GUIDANCE_TYPES:
            guidance_type = infer_guidance_type(normalized.extraction_text)
        attributes["guidance_type"] = guidance_type
        for key in TRAFFIC_GUIDANCE_ATTRIBUTE_KEYS:
            value = attributes.get(key, "")
            attributes[key] = value if isinstance(value, str) else ""
        if attributes["route_or_location"] in {
            "管制区域",
            "管控区域",
            "上述区域",
        }:
            attributes["route_or_location"] = ""
        for key in (
            "route_or_location",
            "vehicle_scope",
            "action",
            "condition",
        ):
            if attributes[key] and attributes[key] not in normalized.extraction_text:
                attributes[key] = ""
        attributes = {
            key: attributes[key]
            for key in TRAFFIC_GUIDANCE_ATTRIBUTE_KEYS
        }

    elif normalized.extraction_class == "publication_date":
        date_text = normalized.extraction_text.strip(" \t\r\n。；：:")
        attributes = {
            "precision": "day" if date_text.endswith("日") else "month"
        }

    normalized.attributes = attributes
    return normalized


def prepare_accepted_extraction(
    extraction: lx.data.Extraction,
) -> lx.data.Extraction:
    return normalize_extraction_attributes(extraction)


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
    """Expand a road-rule fragment to one complete source provision."""
    if extraction.extraction_class != "control_rule":
        return None
    interval = extraction.char_interval
    if interval is None or not isinstance(interval.start_pos, int):
        return None
    if not (0 <= interval.start_pos < len(source_text)):
        return None

    attributes = extraction.attributes or {}
    road_names = attributes.get("road_names", [])
    if not isinstance(road_names, list):
        road_names = []
    road_names = [item for item in road_names if isinstance(item, str) and item]
    if not road_names:
        return None

    if (
        interval.end_pos <= len(source_text)
        and source_text[interval.start_pos : interval.end_pos]
        == extraction.extraction_text
        and extraction.extraction_text.endswith(("。", "；", ";", "！", "？"))
    ):
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
    content_start = line_start + content_offset
    anchor_start = max(interval.start_pos, content_start)
    anchor_end = min(max(interval.end_pos, anchor_start + 1), line_end)

    provision_start = content_start
    for marker in ("。", "；", ";"):
        previous = source_text.rfind(marker, content_start, anchor_start)
        if previous >= provision_start:
            provision_start = previous + 1
    while provision_start < line_end and source_text[provision_start].isspace():
        provision_start += 1

    provision_end = line_end
    terminal_positions = [
        position
        for marker in ("。", "；", ";", "！", "？")
        if (position := source_text.find(marker, anchor_end, line_end)) >= 0
    ]
    if terminal_positions:
        provision_end = min(terminal_positions) + 1

    next_start = provision_end
    while next_start < line_end and source_text[next_start].isspace():
        next_start += 1
    if source_text.startswith(("除", "其中", "但", "允许", "持证"), next_start):
        next_terminal_positions = [
            position
            for marker in ("。", "；", ";", "！", "？")
            if (position := source_text.find(marker, next_start, line_end)) >= 0
        ]
        if next_terminal_positions:
            provision_end = min(next_terminal_positions) + 1

    content = source_text[provision_start:provision_end].strip()
    left_trim = len(source_text[provision_start:provision_end]) - len(
        source_text[provision_start:provision_end].lstrip()
    )
    provision_start += left_trim
    if not content or not any(road_name in content for road_name in road_names):
        return None
    if not any(
        marker in content
        for marker in (
            "禁止",
            "封闭",
            "管制",
            "限行",
            "单向",
            "只出不进",
            "严禁",
        )
    ):
        return None
    if content == extraction.extraction_text:
        return None

    repaired = copy.deepcopy(extraction)
    repaired.extraction_text = content
    repaired.char_interval = lx.data.CharInterval(
        start_pos=provision_start,
        end_pos=provision_start + len(content),
    )

    repaired_attributes = dict(attributes)
    for condition in (
        "视现场实际情况调整",
        "根据现场交通流量情况",
        "根据现场车流、人流情况",
        "根据现场情况",
        "视情",
        "适时启用",
        "视情况",
        "届时根据现场情况",
    ):
        if condition in content:
            repaired_attributes["condition"] = condition
            break
    repaired.attributes = repaired_attributes
    return repaired


INLINE_TIME_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:展会期间|演出期间|活动期间)?(?:每天|每日)\d{1,2}(?::\d{2}|时(?:\d{1,2}分)?)[-—至]\d{1,2}(?::\d{2}|时(?:\d{1,2}分)?)",
        r"(?:\d{4}年)?\d{1,2}月\d{1,2}日[^，。；：\n]{0,35}(?:至|—|-)[^，。；：\n]{1,30}",
        r"(?:每天|每日)?\d{1,2}时(?:\d{1,2}分)?(?:至|—|-)\d{1,2}时(?:\d{1,2}分)?",
        r"(?:进场|散场|离场|演唱会结束|展会|活动)期间(?:（[^）]{1,30}）)?",
        r"(?:每日|每天)?(?:进场|散场|进散场)高峰时段",
    )
)


def extract_time_text_from_context(text: str) -> str:
    """Return one literal time phrase from source context, without conversion."""
    for group in re.findall(r"[（(]([^（）()]{1,50})[）)]", text):
        if any(marker in group for marker in ("时", ":", "期间", "时段")):
            return group.strip()

    if "时间：" in text:
        value = text.split("时间：", 1)[1].strip(" \t；;。")
        if value:
            return value

    for pattern in INLINE_TIME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)

    prefix = re.split(r"[，,；;。]", text, maxsplit=1)[0].strip()
    if len(prefix) <= 70 and any(
        marker in prefix
        for marker in ("年", "月", "日", "时", "期间", "时段", "活动结束")
    ):
        if any(character.isdigit() for character in prefix) or "期间" in prefix:
            return prefix
    return ""


def find_heading_time_text(
    source_text: str,
    rule_start: int,
) -> str:
    context_start = max(0, rule_start - 500)
    preceding = source_text[context_start:rule_start]
    lines = [line.strip() for line in preceding.splitlines() if line.strip()]
    for reverse_index, line in enumerate(reversed(lines[-8:])):
        if any(
            marker in line
            for marker in ("进场", "散场", "离场", "管制", "禁行", "时间：")
        ):
            time_text = extract_time_text_from_context(line)
            if time_text:
                return time_text
        if reverse_index + 1 < len(lines[-8:]):
            prior_line = lines[-8:][-(reverse_index + 2)]
            if any(
                marker in prior_line
                for marker in ("管控时段", "管制时间", "禁行时间", "禁止通行时间")
            ):
                time_text = extract_time_text_from_context(line)
                if time_text:
                    return time_text
    return ""


def bind_control_rule_time(
    extraction: lx.data.Extraction,
    source_text: str,
    control_times: list[lx.data.Extraction],
) -> lx.data.Extraction:
    bound = normalize_extraction_attributes(extraction)
    if bound.extraction_class != "control_rule":
        return bound

    attributes = dict(bound.attributes or {})
    current = attributes.get("time_text", "")
    if isinstance(current, str) and current and current in source_text:
        return bound

    time_text = extract_time_text_from_context(bound.extraction_text)
    interval = bound.char_interval
    if not time_text and interval is not None:
        time_text = find_heading_time_text(source_text, interval.start_pos)

    attributes["time_text"] = time_text if time_text in source_text else ""
    bound.attributes = attributes
    return bound


def normalize_time_class_from_context(
    extraction: lx.data.Extraction,
    source_text: str,
) -> lx.data.Extraction:
    normalized = copy.deepcopy(extraction)
    if normalized.extraction_class not in {"event_time", "control_time"}:
        return normalized

    interval = normalized.char_interval
    if interval is None:
        return normalized
    sentence_start = max(
        source_text.rfind("。", 0, interval.start_pos),
        source_text.rfind("\n", 0, interval.start_pos),
    ) + 1
    sentence_end_candidates = [
        position
        for marker in ("。", "\n")
        if (position := source_text.find(marker, interval.end_pos)) >= 0
    ]
    sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(source_text)
    context = source_text[sentence_start:sentence_end]
    control_markers = (
        "管制",
        "管控",
        "封闭",
        "禁行",
        "禁止通行",
        "道路恢复通行秩序",
        "只出不进",
    )
    event_markers = ("举行", "举办", "开演", "比赛", "演出时间")

    if normalized.extraction_class == "event_time" and any(
        marker in context for marker in control_markers
    ):
        normalized.extraction_class = "control_time"
    elif (
        normalized.extraction_class == "control_time"
        and any(marker in context for marker in event_markers)
        and not any(marker in context for marker in control_markers)
    ):
        normalized.extraction_class = "event_time"
    return normalized


def split_extractions(
    extractions: list[lx.data.Extraction],
    source_text: str,
) -> tuple[list[lx.data.Extraction], list[dict[str, Any]], int]:
    accepted: list[lx.data.Extraction] = []
    rejected: list[dict[str, Any]] = []
    repaired_count = 0
    normalized_extractions = [
        normalize_time_class_from_context(
            normalize_extraction_attributes(extraction),
            source_text,
        )
        for extraction in extractions
    ]
    control_times = [
        extraction
        for extraction in normalized_extractions
        if extraction.extraction_class == "control_time"
    ]
    accepted_signatures: set[tuple[str, str]] = set()

    for extraction, normalized in zip(extractions, normalized_extractions):
        candidate = normalized
        completed_rule = complete_control_rule_from_source(
            candidate,
            source_text,
        )
        if completed_rule is not None:
            candidate = normalize_extraction_attributes(completed_rule)
            repaired_count += 1

        if candidate.extraction_class == "control_rule":
            candidate = bind_control_rule_time(
                candidate,
                source_text,
                control_times,
            )

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

        signature = (candidate.extraction_class, candidate.extraction_text)
        if not reasons:
            duplicate = any(
                existing.extraction_class == candidate.extraction_class
                and (
                    candidate.extraction_text in existing.extraction_text
                    or existing.extraction_text == candidate.extraction_text
                )
                for existing in accepted
            )
            if duplicate:
                reasons = ["与已保留结果重复或被更完整结果覆盖"]

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
            accepted_signatures.add(signature)

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
    pass_errors: list[str] | None = None,
) -> Path | None:
    if not format_handler.raw_responses and not pass_errors:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{input_path.stem}_model_responses.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "input_file": str(input_path.resolve()),
        "response_count": len(format_handler.raw_responses),
        "pass_errors": pass_errors or [],
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


def add_schema_version_to_jsonl(path: Path) -> None:
    versioned_lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        payload = {"schema_version": SCHEMA_VERSION, **payload}
        versioned_lines.append(json.dumps(payload, ensure_ascii=False))
    path.write_text("\n".join(versioned_lines) + "\n", encoding="utf-8")


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
    add_schema_version_to_jsonl(paths["raw"])
    add_schema_version_to_jsonl(paths["accepted"])

    rejected_payload = {
        "schema_version": SCHEMA_VERSION,
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


@dataclass
class NoticeRunStats:
    input_path: Path
    raw_count: int
    accepted_count: int
    rejected_count: int
    repaired_count: int
    category_counts: Counter[str]
    status: str = "success"
    error: str = ""


def process_notice(
    input_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
    *,
    print_details: bool,
) -> NoticeRunStats:
    notice_text = read_notice(input_path)
    response_format_handler = TrafficNoticeFormatHandler()

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

    all_extractions: list[lx.data.Extraction] = []
    successful_results: list[lx.data.AnnotatedDocument] = []
    pass_errors: list[str] = []
    recovery_passes: list[str] = []

    def run_extract_pass(
        label: str,
        prompt: str,
        examples: list[lx.data.ExampleData],
    ) -> list[lx.data.Extraction]:
        try:
            pass_result = lx.extract(
                text_or_documents=notice_text,
                prompt_description=prompt,
                examples=examples,
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
        except Exception as exc:
            pass_errors.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"  [警告] {label}失败，继续执行其余抽取：{type(exc).__name__}: {exc}")
            return []
        successful_results.append(pass_result)
        return list(pass_result.extractions or [])

    is_long_notice = len(notice_text) > 1000
    if not is_long_notice:
        all_extractions.extend(
            run_extract_pass(
                "主抽取",
                PROMPT,
                EXAMPLES,
            )
        )
    else:
        recovery_passes.append("长文本分类型处理")

    preliminary_accepted, _, _ = split_extractions(all_extractions, notice_text)
    accepted_classes = {
        extraction.extraction_class for extraction in preliminary_accepted
    }
    if EVENT_METADATA_CLASSES - accepted_classes:
        recovery_passes.append("活动元数据")
        all_extractions.extend(
            run_extract_pass(
                "活动元数据补抽",
                EVENT_METADATA_PROMPT,
                EVENT_METADATA_EXAMPLES,
            )
        )

    preliminary_accepted, _, _ = split_extractions(all_extractions, notice_text)
    accepted_classes = {
        extraction.extraction_class for extraction in preliminary_accepted
    }
    if NOTICE_METADATA_CLASSES - accepted_classes:
        recovery_passes.append("通告元数据")
        all_extractions.extend(
            run_extract_pass(
                "通告元数据补抽",
                NOTICE_METADATA_PROMPT,
                NOTICE_METADATA_EXAMPLES,
            )
        )

    preliminary_accepted, _, _ = split_extractions(all_extractions, notice_text)
    if not any(
        extraction.extraction_class == "control_rule"
        for extraction in preliminary_accepted
    ):
        recovery_passes.append("道路管制规则")
        all_extractions.extend(
            run_extract_pass(
                "道路管制规则补抽",
                CONTROL_RULE_PROMPT,
                CONTROL_RULE_EXAMPLES,
            )
        )

    preliminary_accepted, _, _ = split_extractions(
        all_extractions,
        notice_text,
    )
    accepted_guidance_types = {
        (extraction.attributes or {}).get("guidance_type")
        for extraction in preliminary_accepted
        if extraction.extraction_class == "traffic_guidance"
    }
    route_cues = ("绕行", "入场路线", "散场路线", "离场路线", "驶出")
    service_cues = ("停车", "上下客", "即停即走", "公交", "地铁", "出行提示", "服从")
    needs_route_guidance = is_long_notice or (
        any(cue in notice_text for cue in route_cues)
        and not accepted_guidance_types.intersection(ROUTE_GUIDANCE_TYPES)
    )
    needs_service_guidance = is_long_notice or (
        any(cue in notice_text for cue in service_cues)
        and not accepted_guidance_types.intersection(SERVICE_GUIDANCE_TYPES)
    )
    if needs_route_guidance:
        recovery_passes.append("路线引导")
        all_extractions.extend(
            run_extract_pass(
                "路线引导补抽",
                ROUTE_GUIDANCE_PROMPT,
                ROUTE_GUIDANCE_EXAMPLES,
            )
        )
    if needs_service_guidance:
        recovery_passes.append("停车公交与一般提示")
        all_extractions.extend(
            run_extract_pass(
                "停车公交与一般提示补抽",
                SERVICE_GUIDANCE_PROMPT,
                SERVICE_GUIDANCE_EXAMPLES,
            )
        )

    model_response_path = save_model_responses(
        response_format_handler,
        input_path,
        output_dir,
        pass_errors,
    )
    if not successful_results:
        detail = "；".join(pass_errors) or "未知错误"
        raise RuntimeError(f"所有 Ollama 抽取 pass 均失败：{detail}")
    result = successful_results[0]
    if not all_extractions:
        detail = "；".join(pass_errors)
        suffix = f"；失败详情：{detail}" if detail else ""
        raise RuntimeError(f"模型没有返回任何可解析的原始抽取结果{suffix}")

    accepted, rejected, repaired_count = split_extractions(
        all_extractions,
        notice_text,
    )
    paths = save_results(
        result=result,
        source_text=notice_text,
        all_extractions=all_extractions,
        accepted=accepted,
        rejected=rejected,
        input_path=input_path,
        output_dir=output_dir,
    )

    if print_details:
        print(f"模型：{args.model}")
        print(f"Ollama：{args.ollama_url}")
        if response_format_handler.normalized_response_count:
            print(
                "输出格式修复："
                f"{response_format_handler.normalized_response_count} 个模型回复"
                "已转换为 LangExtract 格式"
            )
        if recovery_passes:
            print("聚焦补抽：" + "、".join(recovery_passes))
        if pass_errors:
            print("已由其他 pass 恢复的失败：")
            for error in pass_errors:
                print(f"  - {error}")
        print_accepted_results(accepted)
        print_summary(all_extractions, accepted, rejected, repaired_count)
        print("\n输出文件：")
        print(f"  原始结果：{paths['raw'].resolve()}")
        print(f"  保留结果：{paths['accepted'].resolve()}")
        print(f"  拒绝结果：{paths['rejected'].resolve()}")
        print(f"  可视化：{paths['visualization'].resolve()}")
        if model_response_path is not None:
            print(f"  模型原始回复：{model_response_path.resolve()}")

    counts = Counter(item.extraction_class for item in accepted)
    return NoticeRunStats(
        input_path=input_path,
        raw_count=len(all_extractions),
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        repaired_count=repaired_count,
        category_counts=counts,
    )


def collect_inputs(args: argparse.Namespace) -> tuple[list[Path], bool]:
    if args.input_dir is None:
        return [(args.input or Path("traffic_notice.txt")).resolve()], False

    input_dir = args.input_dir.resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"找不到输入目录：{input_dir}")
    paths = sorted(
        (path.resolve() for path in input_dir.glob("*.txt") if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    if not paths:
        raise ValueError(f"输入目录中没有 .txt 文件：{input_dir}")
    duplicate_stems = [
        stem
        for stem, count in Counter(path.stem.casefold() for path in paths).items()
        if count > 1
    ]
    if duplicate_stems:
        raise ValueError("批量输入存在同名文件，输出前缀会冲突")
    return paths, True


def save_evaluation_summary(
    results: list[NoticeRunStats],
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "evaluation_summary.csv"
    fieldnames = [
        "schema_version",
        "file_name",
        "status",
        "error",
        "raw_count",
        "accepted_count",
        "rejected_count",
        *EXTRACTION_CLASSES,
    ]
    with summary_path.open("w", encoding="utf-8-sig", newline="") as file_handle:
        writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow({
                "schema_version": SCHEMA_VERSION,
                "file_name": result.input_path.name,
                "status": result.status,
                "error": result.error,
                "raw_count": result.raw_count,
                "accepted_count": result.accepted_count,
                "rejected_count": result.rejected_count,
                **{
                    category: result.category_counts.get(category, 0)
                    for category in EXTRACTION_CLASSES
                },
            })
    return summary_path


def main() -> int:
    load_dotenv()
    args = parse_args()

    try:
        input_paths, batch_mode = collect_inputs(args)
        output_dir = args.output_dir.resolve()
    except Exception as exc:
        print(f"任务未启动：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not batch_mode:
        try:
            process_notice(
                input_paths[0],
                output_dir,
                args,
                print_details=True,
            )
            return 0
        except Exception as exc:
            print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "排查提示：请查看模型原始回复，并确认 Ollama 正在运行且已安装模型 "
                f"{args.model}。",
                file=sys.stderr,
            )
            return 1

    print("批量执行计划：")
    print(f"  文件数量：{len(input_paths)}")
    print("  处理方式：串行（每次一篇）")
    print(f"  输出目录：{output_dir}")
    results: list[NoticeRunStats] = []
    for index, input_path in enumerate(input_paths, start=1):
        print(f"\n[{index}/{len(input_paths)}] 正在处理 {input_path.name}……")
        try:
            stats = process_notice(
                input_path,
                output_dir,
                args,
                print_details=False,
            )
            results.append(stats)
            print(
                f"[成功] {input_path.name}: 原始 {stats.raw_count}，"
                f"保留 {stats.accepted_count}，拒绝 {stats.rejected_count}"
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            results.append(
                NoticeRunStats(
                    input_path=input_path,
                    raw_count=0,
                    accepted_count=0,
                    rejected_count=0,
                    repaired_count=0,
                    category_counts=Counter(),
                    status="failed",
                    error=error,
                )
            )
            print(f"[失败] {input_path.name}: {error}", file=sys.stderr)

    summary_path = save_evaluation_summary(results, output_dir)
    success_count = sum(result.status == "success" for result in results)
    failure_count = len(results) - success_count
    print("\n批量处理统计：")
    print(f"  总数：{len(results)}")
    print(f"  成功：{success_count}")
    print(f"  失败：{failure_count}")
    print(f"  汇总文件：{summary_path.resolve()}")
    return 0 if failure_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
