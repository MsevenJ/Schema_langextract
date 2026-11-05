"""End-to-end tests for the traffic-notice human-review workbook."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from openpyxl import load_workbook

from apply_review_workbook import (
    ANNOUNCEMENT_HEADERS,
    ANNOUNCEMENT_SHEET,
    CONTROL_HEADERS,
    CONTROL_SHEET,
    CSV_FIELDNAMES,
    GUIDANCE_HEADERS,
    GUIDANCE_SHEET,
    GUIDANCE_TYPE_BY_LABEL,
    MISSING_HEADERS,
    MISSING_SHEET,
    RAW_SHEET,
    apply_workbook,
)
from export_review_workbook import export_workbook


RAW_HEADERS = (
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

SOURCE_ID = "src_review_test"
FILE_NAME = "notice_test.txt"


def _raw_row(
    ordinal: int,
    extraction_class: str,
    text: str,
    attributes: dict[str, object] | None = None,
) -> dict[str, str]:
    return {
        "event_id": "",
        "source_id": SOURCE_ID,
        "file_name": FILE_NAME,
        "extraction_id": f"ext_test_{ordinal:03d}",
        "extraction_class": extraction_class,
        "extraction_text": text,
        "attributes_json": json.dumps(attributes or {}, ensure_ascii=False),
        "char_start": str(ordinal * 10),
        "char_end": str(ordinal * 10 + len(text)),
        "grounding_status": "exact",
        "validation_status": "accepted",
        "rejection_reasons": "",
        "coverage_status": "matched",
        "review_status": "pending",
        "is_correct": "",
        "corrected_class": "",
        "corrected_text": "",
        "corrected_attributes": "",
        "review_note": "",
    }


def _fixture_rows() -> list[dict[str, str]]:
    rows = [
        _raw_row(1, "event_name", "测试演唱会"),
        _raw_row(2, "event_venue", "测试体育馆"),
        _raw_row(3, "event_time", "2026年11月8日19时"),
        _raw_row(4, "control_time", "2026年11月8日17时至23时"),
        _raw_row(5, "issuing_authority", "测试市公安局交警支队"),
        _raw_row(6, "publication_date", "2026年11月5日", {"precision": "day"}),
        _raw_row(
            7,
            "control_rule",
            "测试路禁止车辆通行。",
            {
                "road_names": ["测试路"],
                "road_range": "全线",
                "direction": "双向",
                "time_text": "17时至23时",
                "measure": "禁止车辆通行",
                "vehicle_scope": "车辆",
                "condition": "",
                "exception": "",
            },
        ),
    ]
    # Reproduce the five guidance rows whose model guidance_type is empty.
    for index in range(5):
        rows.append(_raw_row(
            8 + index,
            "traffic_guidance",
            f"第{index + 1}条交通引导候选。",
            {
                "guidance_type": "",
                "route_or_location": f"位置{index + 1}",
                "time_text": "",
                "vehicle_scope": "社会车辆",
                "action": "听从引导",
                "condition": "",
            },
        ))
    for index in range(13):
        rows.append(_raw_row(
            13 + index,
            "needs_review",
            f"第{index + 1}条漏抽候选原文。",
            {"road_names": [f"候选路{index + 1}"]},
        ))
    return rows


def _write_queue(path: Path) -> list[dict[str, str]]:
    rows = _fixture_rows()
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _append_dict_row(worksheet, headers, values):
    worksheet.append([values.get(header, "") for header in headers])


def test_export_has_independent_decisions_missing_sheet_and_long_validations(tmp_path):
    queue_path = tmp_path / "review_queue.csv"
    workbook_path = tmp_path / "review.xlsx"
    source_rows = _write_queue(queue_path)

    counts = export_workbook(queue_path, workbook_path)
    assert counts == {
        ANNOUNCEMENT_SHEET: 1,
        CONTROL_SHEET: 1,
        GUIDANCE_SHEET: 5,
        MISSING_SHEET: 13,
        RAW_SHEET: 25,
    }

    workbook = load_workbook(workbook_path)
    try:
        assert workbook.sheetnames == [
            ANNOUNCEMENT_SHEET,
            CONTROL_SHEET,
            GUIDANCE_SHEET,
            MISSING_SHEET,
            RAW_SHEET,
        ]
        announcement = workbook[ANNOUNCEMENT_SHEET]
        assert [cell.value for cell in announcement[1]] == list(ANNOUNCEMENT_HEADERS)
        for field in ("活动名称", "活动场馆", "活动时间", "管制时间", "发布机关", "发布日期"):
            conclusion_column = ANNOUNCEMENT_HEADERS.index(f"{field}复核结论") + 1
            assert announcement.cell(2, conclusion_column).value == "待复核"
        assert len(announcement.data_validations.dataValidation) == 6

        guidance = workbook[GUIDANCE_SHEET]
        type_column = GUIDANCE_HEADERS.index("引导类型") + 1
        assert all(guidance.cell(row, type_column).value is None for row in range(2, 7))
        guidance_formulas = [
            validation.formula1
            for validation in guidance.data_validations.dataValidation
        ]
        assert any("绕行路线" in formula and "一般出行建议" in formula for formula in guidance_formulas)
        assert any("5000" in str(validation.sqref) for validation in guidance.data_validations.dataValidation)
        assert guidance.tables
        assert guidance.cell(1, GUIDANCE_HEADERS.index("extraction_id") + 1).comment

        missing = workbook[MISSING_SHEET]
        assert [cell.value for cell in missing[1]] == list(MISSING_HEADERS)
        target_formulas = [
            validation.formula1
            for validation in missing.data_validations.dataValidation
        ]
        assert any("道路管制" in formula and "交通引导" in formula and "非目标" in formula for formula in target_formulas)

        raw = workbook[RAW_SHEET]
        raw_headers = [cell.value for cell in raw[1]]
        raw_values = [
            dict(zip(raw_headers, ["" if cell.value is None else str(cell.value) for cell in row]))
            for row in raw.iter_rows(min_row=2, max_col=len(raw_headers))
        ]
        assert raw_values == source_rows
    finally:
        workbook.close()


def test_apply_full_review_supports_all_resolutions_and_manual_rows(tmp_path):
    queue_path = tmp_path / "review_queue.csv"
    workbook_path = tmp_path / "review.xlsx"
    _write_queue(queue_path)
    export_workbook(queue_path, workbook_path)

    labels = list(GUIDANCE_TYPE_BY_LABEL)
    workbook = load_workbook(workbook_path)
    announcement = workbook[ANNOUNCEMENT_SHEET]
    for field in ("活动名称", "活动场馆", "活动时间", "管制时间", "发布机关", "发布日期"):
        announcement.cell(2, ANNOUNCEMENT_HEADERS.index(f"{field}复核结论") + 1).value = "正确"
    # Prove that one field can be deleted without affecting other notice fields.
    announcement.cell(2, ANNOUNCEMENT_HEADERS.index("活动场馆复核结论") + 1).value = "删除"
    announcement.cell(2, ANNOUNCEMENT_HEADERS.index("活动时间") + 1).value = "2026年11月8日20时"
    announcement.cell(2, ANNOUNCEMENT_HEADERS.index("活动时间复核结论") + 1).value = "已修改"

    control = workbook[CONTROL_SHEET]
    control.cell(2, CONTROL_HEADERS.index("复核结论") + 1).value = "正确"

    guidance = workbook[GUIDANCE_SHEET]
    for offset, row_number in enumerate(range(2, 7)):
        guidance.cell(row_number, GUIDANCE_HEADERS.index("引导类型") + 1).value = labels[offset]
        guidance.cell(row_number, GUIDANCE_HEADERS.index("复核结论") + 1).value = (
            "已修改" if row_number == 2 else "正确"
        )

    missing = workbook[MISSING_SHEET]
    for offset, row_number in enumerate(range(2, 15)):
        if offset < 5:
            missing.cell(row_number, MISSING_HEADERS.index("目标类型") + 1).value = "道路管制"
            missing.cell(row_number, MISSING_HEADERS.index("管制措施") + 1).value = "临时交通管制"
            missing.cell(row_number, MISSING_HEADERS.index("复核结论") + 1).value = "已修改"
        elif offset < 10:
            missing.cell(row_number, MISSING_HEADERS.index("目标类型") + 1).value = "交通引导"
            missing.cell(row_number, MISSING_HEADERS.index("引导类型") + 1).value = labels[offset % len(labels)]
            missing.cell(row_number, MISSING_HEADERS.index("具体动作") + 1).value = "按指定路线通行"
            missing.cell(row_number, MISSING_HEADERS.index("复核结论") + 1).value = "正确"
        else:
            missing.cell(row_number, MISSING_HEADERS.index("目标类型") + 1).value = "非目标"
            missing.cell(row_number, MISSING_HEADERS.index("复核结论") + 1).value = "正确"

    # New rows intentionally leave extraction_id and source_id empty.  The
    # file name identifies the source and apply_workbook generates stable IDs.
    _append_dict_row(control, CONTROL_HEADERS, {
        "文件名": FILE_NAME,
        "活动名称": "测试演唱会",
        "道路名称": "人工补录路",
        "管制时间": "20时至21时",
        "管制措施": "禁止通行",
        "原文证据": "人工发现的道路管制原文。",
        "复核结论": "已修改",
        "复核备注": "人工补录",
    })
    _append_dict_row(guidance, GUIDANCE_HEADERS, {
        "文件名": FILE_NAME,
        "活动名称": "测试演唱会",
        "引导类型": labels[-1],
        "路线或位置": "人工补录位置",
        "具体动作": "提前绕行",
        "原文证据": "人工发现的交通引导原文。",
        "复核结论": "已修改",
        "复核备注": "人工补录",
    })
    workbook.save(workbook_path)
    workbook.close()

    jsonl_output = tmp_path / "gold.jsonl"
    csv_output = tmp_path / "gold.csv"
    unreviewed_output = tmp_path / "unreviewed.csv"
    metrics_output = tmp_path / "metrics.csv"
    counts = apply_workbook(
        workbook_path,
        jsonl_output,
        csv_output,
        unreviewed_output,
        metrics_output,
    )

    assert counts == {"gold": 23, "unreviewed": 0, "deleted": 1, "source_rows": 25}
    gold = _read_csv(csv_output)
    assert list(gold[0]) == list(CSV_FIELDNAMES)
    assert _read_csv(unreviewed_output) == []
    assert len([json.loads(line) for line in jsonl_output.read_text(encoding="utf-8").splitlines()]) == 23

    classes_by_id = {row["extraction_id"]: row["extraction_class"] for row in gold}
    for ordinal in range(13, 18):
        assert classes_by_id[f"ext_test_{ordinal:03d}"] == "control_rule"
    for ordinal in range(18, 23):
        assert classes_by_id[f"ext_test_{ordinal:03d}"] == "traffic_guidance"
    for ordinal in range(23, 26):
        assert f"ext_test_{ordinal:03d}" not in classes_by_id

    event_classes = {row["extraction_class"] for row in gold}
    assert "event_name" in event_classes
    assert "event_venue" not in event_classes
    assert any(
        row["extraction_class"] == "event_time"
        and row["extraction_text"] == "2026年11月8日20时"
        for row in gold
    )

    manual = [row for row in gold if row["record_origin"] == "manual"]
    assert len(manual) == 2
    assert all(
        row["extraction_id"].startswith("ext_")
        and row["source_id"] == SOURCE_ID
        and row["evidence_text"]
        for row in manual
    )
    converted = [row for row in gold if row["record_origin"] == "needs_review"]
    assert len(converted) == 10
    guidance_codes = {
        row["guidance_type"]
        for row in gold
        if row["extraction_class"] == "traffic_guidance"
    }
    assert guidance_codes == set(GUIDANCE_TYPE_BY_LABEL.values())

    metrics = _read_csv(metrics_output)
    overall = next(row for row in metrics if row["extraction_class"] == "__overall__")
    assert overall["model_rows"] == "25"
    assert overall["added_rows"] == "2"
    assert overall["reviewed_rows"] == "27"
    assert overall["pending_rows"] == "0"
    assert overall["review_completion_rate"] == "1.000000"


def test_current_notice04_to_notice08_can_complete_the_full_review_loop(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    queue_path = project_root / "output_calibrated" / "review_queue.csv"
    workbook_path = tmp_path / "current_review.xlsx"
    counts = export_workbook(queue_path, workbook_path)
    assert counts == {
        ANNOUNCEMENT_SHEET: 5,
        CONTROL_SHEET: 38,
        GUIDANCE_SHEET: 28,
        MISSING_SHEET: 13,
        RAW_SHEET: 126,
    }

    workbook = load_workbook(workbook_path)
    announcement = workbook[ANNOUNCEMENT_SHEET]
    for row_number in range(2, announcement.max_row + 1):
        for field in ("活动名称", "活动场馆", "活动时间", "管制时间", "发布机关", "发布日期"):
            announcement.cell(
                row_number, ANNOUNCEMENT_HEADERS.index(f"{field}复核结论") + 1
            ).value = "正确"

    control = workbook[CONTROL_SHEET]
    conclusion_column = CONTROL_HEADERS.index("复核结论") + 1
    for row_number in range(2, control.max_row + 1):
        control.cell(row_number, conclusion_column).value = "正确"
    control.cell(2, CONTROL_HEADERS.index("管制措施") + 1).value = "人工校准的管制措施"
    control.cell(2, conclusion_column).value = "已修改"
    control.cell(3, conclusion_column).value = "删除"

    guidance = workbook[GUIDANCE_SHEET]
    type_column = GUIDANCE_HEADERS.index("引导类型") + 1
    conclusion_column = GUIDANCE_HEADERS.index("复核结论") + 1
    blank_guidance_rows = [
        row_number
        for row_number in range(2, guidance.max_row + 1)
        if not guidance.cell(row_number, type_column).value
    ]
    assert len(blank_guidance_rows) == 5
    for row_number in range(2, guidance.max_row + 1):
        if not guidance.cell(row_number, type_column).value:
            guidance.cell(row_number, type_column).value = "一般出行建议"
        guidance.cell(row_number, conclusion_column).value = "正确"
    guidance.cell(2, conclusion_column).value = "已修改"

    missing = workbook[MISSING_SHEET]
    for offset, row_number in enumerate(range(2, missing.max_row + 1)):
        if offset < 5:
            missing.cell(row_number, MISSING_HEADERS.index("目标类型") + 1).value = "道路管制"
            missing.cell(row_number, MISSING_HEADERS.index("管制措施") + 1).value = "临时交通管制"
            missing.cell(row_number, MISSING_HEADERS.index("复核结论") + 1).value = "已修改"
        elif offset < 10:
            missing.cell(row_number, MISSING_HEADERS.index("目标类型") + 1).value = "交通引导"
            missing.cell(row_number, MISSING_HEADERS.index("引导类型") + 1).value = "一般出行建议"
            missing.cell(row_number, MISSING_HEADERS.index("具体动作") + 1).value = "按现场指引通行"
            missing.cell(row_number, MISSING_HEADERS.index("复核结论") + 1).value = "正确"
        else:
            missing.cell(row_number, MISSING_HEADERS.index("目标类型") + 1).value = "非目标"
            missing.cell(row_number, MISSING_HEADERS.index("复核结论") + 1).value = "正确"

    first_file = control.cell(2, CONTROL_HEADERS.index("文件名") + 1).value
    _append_dict_row(control, CONTROL_HEADERS, {
        "文件名": first_file,
        "道路名称": "人工补录道路",
        "管制措施": "禁止通行",
        "原文证据": "人工补录的道路管制证据。",
        "复核结论": "已修改",
    })
    _append_dict_row(guidance, GUIDANCE_HEADERS, {
        "文件名": first_file,
        "引导类型": "一般出行建议",
        "具体动作": "提前规划路线",
        "原文证据": "人工补录的交通引导证据。",
        "复核结论": "已修改",
    })
    workbook.save(workbook_path)
    workbook.close()

    jsonl_output = tmp_path / "current_gold.jsonl"
    csv_output = tmp_path / "current_gold.csv"
    unreviewed_output = tmp_path / "current_unreviewed.csv"
    metrics_output = tmp_path / "current_metrics.csv"
    result = apply_workbook(
        workbook_path,
        jsonl_output,
        csv_output,
        unreviewed_output,
        metrics_output,
    )
    assert result == {
        "gold": 124,
        "unreviewed": 0,
        "deleted": 1,
        "source_rows": 126,
    }
    assert _read_csv(unreviewed_output) == []
    gold = _read_csv(csv_output)
    assert len([row for row in gold if row["record_origin"] == "needs_review"]) == 10
    manual = [row for row in gold if row["record_origin"] == "manual"]
    assert len(manual) == 2
    assert all(row["extraction_id"] for row in manual)

    overall = next(
        row
        for row in _read_csv(metrics_output)
        if row["extraction_class"] == "__overall__"
    )
    assert overall["model_rows"] == "126"
    assert overall["added_rows"] == "2"
    assert overall["reviewed_rows"] == "128"
    assert overall["pending_rows"] == "0"
    assert overall["review_completion_rate"] == "1.000000"
