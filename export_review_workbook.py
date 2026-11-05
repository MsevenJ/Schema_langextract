"""Export the traffic-notice review queue as a reviewer-friendly workbook.

This is a presentation-only transformation.  It does not change Schema 2.0,
the extraction prompt, validation, or the source review_queue.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import OrderedDict
from pathlib import Path
import tempfile
from typing import Any, Iterable, Sequence

try:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
except ImportError as exc:  # pragma: no cover - depends on the local environment.
    raise SystemExit(
        "缺少 openpyxl，请先执行: python -m pip install 'openpyxl>=3.1.0'"
    ) from exc


DEFAULT_INPUT = Path("output_calibrated/review_queue.csv")
DEFAULT_OUTPUT = Path("output_calibrated/traffic_notice_review.xlsx")

REVIEW_OPTIONS = ("待复核", "正确", "已修改", "删除")

ANNOUNCEMENT_SHEET = "公告信息复核"
CONTROL_SHEET = "道路管制复核"
GUIDANCE_SHEET = "交通引导复核"
RAW_SHEET = "原始结果"

ANNOUNCEMENT_HEADERS = (
    "文件名",
    "活动名称",
    "活动场馆",
    "活动时间",
    "管制时间",
    "发布机关",
    "发布日期",
    "复核结论",
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

EVENT_COLUMN_BY_CLASS = OrderedDict((
    ("event_name", "活动名称"),
    ("event_venue", "活动场馆"),
    ("event_time", "活动时间"),
    ("control_time", "管制时间"),
    ("issuing_authority", "发布机关"),
    ("publication_date", "发布日期"),
))

REQUIRED_INPUT_FIELDS = {
    "source_id",
    "file_name",
    "extraction_id",
    "extraction_class",
    "extraction_text",
    "attributes_json",
}

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)
GREEN_FILL = PatternFill("solid", fgColor="C6EFCE")
YELLOW_FILL = PatternFill("solid", fgColor="FFF2CC")
RED_FILL = PatternFill("solid", fgColor="FFC7CE")


def _parse_attributes(value: str, *, row_number: int) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"review_queue.csv 第 {row_number} 行 attributes_json 不是合法 JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"review_queue.csv 第 {row_number} 行 attributes_json 必须是 JSON 对象"
        )
    return parsed


def _read_review_queue(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到复核队列: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        missing = sorted(REQUIRED_INPUT_FIELDS.difference(fieldnames))
        if missing:
            raise ValueError(f"review_queue.csv 缺少字段: {', '.join(missing)}")
        rows = [
            {name: value or "" for name, value in row.items() if name is not None}
            for row in reader
        ]
    return fieldnames, rows


def _display_list(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _rows_by_source(rows: Iterable[dict[str, str]]) -> OrderedDict[str, list[dict[str, str]]]:
    grouped: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    for row in rows:
        source_id = row["source_id"].strip()
        if not source_id:
            raise ValueError("review_queue.csv 存在空 source_id")
        grouped.setdefault(source_id, []).append(row)
    return grouped


def _event_values(source_rows: Sequence[dict[str, str]], extraction_class: str) -> str:
    # Keep every candidate, including rejected candidates, so the reviewer can
    # decide what belongs in gold.  Newlines make multiple values editable.
    return "\n".join(
        row["extraction_text"]
        for row in source_rows
        if row["extraction_class"] == extraction_class and row["extraction_text"]
    )


def _build_announcement_rows(
    grouped: OrderedDict[str, list[dict[str, str]]],
) -> list[list[str]]:
    output: list[list[str]] = []
    for source_id, source_rows in grouped.items():
        first = source_rows[0]
        values = {
            column: _event_values(source_rows, extraction_class)
            for extraction_class, column in EVENT_COLUMN_BY_CLASS.items()
        }
        output.append([
            first["file_name"],
            values["活动名称"],
            values["活动场馆"],
            values["活动时间"],
            values["管制时间"],
            values["发布机关"],
            values["发布日期"],
            "待复核",
            "",
            source_id,
        ])
    return output


def _event_name_by_source(
    grouped: OrderedDict[str, list[dict[str, str]]],
) -> dict[str, str]:
    return {
        source_id: _event_values(source_rows, "event_name")
        for source_id, source_rows in grouped.items()
    }


def _build_control_rows(
    rows: Sequence[dict[str, str]], event_names: dict[str, str]
) -> list[list[str]]:
    output: list[list[str]] = []
    for row_number, row in enumerate(rows, start=2):
        if row["extraction_class"] != "control_rule":
            continue
        attributes = _parse_attributes(row["attributes_json"], row_number=row_number)
        output.append([
            row["file_name"],
            event_names.get(row["source_id"], ""),
            _display_list(attributes.get("road_names")),
            _display_list(attributes.get("road_range")),
            _display_list(attributes.get("direction")),
            _display_list(attributes.get("time_text")),
            _display_list(attributes.get("measure")),
            _display_list(attributes.get("vehicle_scope")),
            _display_list(attributes.get("condition")),
            _display_list(attributes.get("exception")),
            row["extraction_text"],
            "待复核",
            "",
            row["extraction_id"],
            row["source_id"],
        ])
    return output


def _build_guidance_rows(
    rows: Sequence[dict[str, str]], event_names: dict[str, str]
) -> list[list[str]]:
    output: list[list[str]] = []
    for row_number, row in enumerate(rows, start=2):
        if row["extraction_class"] != "traffic_guidance":
            continue
        attributes = _parse_attributes(row["attributes_json"], row_number=row_number)
        route_or_location = attributes.get("route_or_location", "")
        # Coverage candidates carry only road_names.  Surface those values in
        # the closest ordinary review column instead of hiding them in JSON.
        if not route_or_location:
            route_or_location = attributes.get("road_names", "")
        output.append([
            row["file_name"],
            event_names.get(row["source_id"], ""),
            _display_list(attributes.get("guidance_type")),
            _display_list(route_or_location),
            _display_list(attributes.get("time_text")),
            _display_list(attributes.get("vehicle_scope")),
            _display_list(attributes.get("action")),
            _display_list(attributes.get("condition")),
            row["extraction_text"],
            "待复核",
            "",
            row["extraction_id"],
            row["source_id"],
        ])
    return output


def _append_table(worksheet: Any, headers: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    worksheet.append(list(headers))
    for row in rows:
        worksheet.append(list(row))
    worksheet.freeze_panes = "A2"
    worksheet.sheet_view.showGridLines = False
    worksheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(headers))}{max(worksheet.max_row, 1)}"
    )
    for cell in worksheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    worksheet.row_dimensions[1].height = 30
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.number_format = "@"


def _add_review_validation(worksheet: Any, headers: Sequence[str]) -> None:
    conclusion_index = headers.index("复核结论") + 1
    conclusion_letter = get_column_letter(conclusion_index)
    final_row = max(worksheet.max_row, 2)
    validation = DataValidation(
        type="list",
        formula1='"' + ",".join(REVIEW_OPTIONS) + '"',
        allow_blank=False,
    )
    validation.errorTitle = "复核结论无效"
    validation.error = "请从下拉列表中选择待复核、正确、已修改或删除。"
    validation.promptTitle = "复核结论"
    validation.prompt = "正确=直接接受；已修改=按当前单元格接受；删除=不进入金标准。"
    validation.showInputMessage = True
    validation.showErrorMessage = True
    worksheet.add_data_validation(validation)
    validation.add(f"{conclusion_letter}2:{conclusion_letter}{final_row}")

    table_range = f"A2:{get_column_letter(len(headers))}{final_row}"
    for value, fill in (("正确", GREEN_FILL), ("已修改", YELLOW_FILL), ("删除", RED_FILL)):
        worksheet.conditional_formatting.add(
            table_range,
            FormulaRule(
                formula=[f'${conclusion_letter}2="{value}"'],
                fill=fill,
            ),
        )


def _set_widths(worksheet: Any, headers: Sequence[str]) -> None:
    preferred = {
        "文件名": 18,
        "活动名称": 26,
        "活动场馆": 24,
        "活动时间": 26,
        "管制时间": 28,
        "发布机关": 28,
        "发布日期": 18,
        "道路名称": 28,
        "管制路段": 38,
        "方向": 15,
        "管制措施": 32,
        "车辆范围": 25,
        "实施条件": 25,
        "例外车辆或方式": 42,
        "引导类型": 18,
        "路线或位置": 45,
        "适用时间": 25,
        "具体动作": 35,
        "原文证据": 70,
        "复核结论": 14,
        "复核备注": 35,
        "extraction_id": 31,
        "source_id": 31,
        "attributes_json": 65,
        "extraction_text": 65,
    }
    for index, header in enumerate(headers, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = preferred.get(
            header, min(max(len(header) + 4, 14), 28)
        )


def _style_review_sheet(worksheet: Any, headers: Sequence[str], row_height: float) -> None:
    _add_review_validation(worksheet, headers)
    _set_widths(worksheet, headers)
    for index in range(2, worksheet.max_row + 1):
        worksheet.row_dimensions[index].height = row_height


def export_workbook(input_path: Path, output_path: Path) -> dict[str, int]:
    fieldnames, rows = _read_review_queue(input_path)
    grouped = _rows_by_source(rows)
    event_names = _event_name_by_source(grouped)

    announcement_rows = _build_announcement_rows(grouped)
    control_rows = _build_control_rows(rows, event_names)
    guidance_rows = _build_guidance_rows(rows, event_names)
    raw_rows = [[row.get(field, "") for field in fieldnames] for row in rows]

    workbook = Workbook()
    announcement = workbook.active
    announcement.title = ANNOUNCEMENT_SHEET
    control = workbook.create_sheet(CONTROL_SHEET)
    guidance = workbook.create_sheet(GUIDANCE_SHEET)
    raw = workbook.create_sheet(RAW_SHEET)

    _append_table(announcement, ANNOUNCEMENT_HEADERS, announcement_rows)
    _append_table(control, CONTROL_HEADERS, control_rows)
    _append_table(guidance, GUIDANCE_HEADERS, guidance_rows)
    _append_table(raw, fieldnames, raw_rows)

    _style_review_sheet(announcement, ANNOUNCEMENT_HEADERS, 54)
    _style_review_sheet(control, CONTROL_HEADERS, 66)
    _style_review_sheet(guidance, GUIDANCE_HEADERS, 66)
    _set_widths(raw, fieldnames)

    announcement.sheet_properties.tabColor = "5B9BD5"
    control.sheet_properties.tabColor = "ED7D31"
    guidance.sheet_properties.tabColor = "70AD47"
    raw.sheet_properties.tabColor = "A5A5A5"
    raw.protection.sheet = True

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}.",
            suffix=".xlsx",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        workbook.save(temporary_name)
        Path(temporary_name).replace(output_path)
    finally:
        workbook.close()
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)

    return {
        ANNOUNCEMENT_SHEET: len(announcement_rows),
        CONTROL_SHEET: len(control_rows),
        GUIDANCE_SHEET: len(guidance_rows),
        RAW_SHEET: len(raw_rows),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 review_queue.csv 导出为分表人工复核 Excel。"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    counts = export_workbook(args.input, args.output)
    print(f"已生成: {args.output}")
    for sheet_name, count in counts.items():
        print(f"  {sheet_name}: {count} 行（不含表头）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
