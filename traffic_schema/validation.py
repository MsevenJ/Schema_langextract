"""Central semantic rules for traffic-control extractions."""

from __future__ import annotations

import re


CONTROL_MEASURE_MARKERS = (
    "禁止通行",
    "禁止驶入",
    "禁止进入",
    "封闭",
    "临时封闭",
    "限行",
    "交通管制",
    "临时交通管理",
    "控流",
    "分流",
    "单向通行",
    "单向交通",
    "只出不进",
    "停止通行",
    "清理车辆",
    "车辆进行清理",
    "禁止停车",
    "严禁停车",
    "严禁上下客",
    "预约通行",
    "预约轮候",
    "分批放行",
    "分批、错峰进入",
    "临时交通组织",
    "禁止车辆",
    "禁止机动车",
    "禁止一切车辆",
)

_EXCEPTION_PATTERNS = (
    re.compile(r"除[^。；\n]{1,100}?外"),
    re.compile(
        r"(?:持[^，。；（）()\n]{0,80}?(?:通行证|车证)|"
        r"[^，。；（）()\n]{0,60}?(?:车辆|机动车|出租车))"
        r"[^，。；（）()\n]{0,60}?除外"
    ),
)


def control_measure_status(text: str) -> str:
    """Return mandatory or needs_review without discarding uncertain rules."""
    if any(marker in text for marker in CONTROL_MEASURE_MARKERS):
        return "mandatory"
    return "needs_review"


def is_mandatory_control(text: str) -> bool:
    return control_measure_status(text) == "mandatory"


def extract_exception_text(text: str) -> str:
    """Extract one literal exception phrase, supporting both Chinese orders."""
    matches: list[tuple[int, str]] = []
    for pattern in _EXCEPTION_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), match.group(0)))
    if not matches:
        return ""
    matches.sort(key=lambda item: item[0])
    return "；".join(dict.fromkeys(value for _, value in matches))


def separate_vehicle_scope_and_exception(
    vehicle_scope: str,
    exception: str,
) -> tuple[str, str]:
    """Prevent an exception-only phrase from becoming the restricted scope."""
    scope = vehicle_scope.strip()
    exception = exception.strip()
    if not exception:
        return scope, exception
    if scope == exception or scope.endswith("除外"):
        return "", exception
    if exception in scope:
        scope = scope.replace(f"（{exception}）", "")
        scope = scope.replace(f"({exception})", "")
        scope = scope.replace(exception, "").strip(" ，,；;（）()")
    return scope, exception
