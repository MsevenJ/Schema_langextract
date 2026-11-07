"""Basic reconciliation tests for review-based error analysis."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from pathlib import Path

from generate_error_analysis import (
    ERROR_FIELDNAMES,
    ERROR_TYPES,
    generate_analysis,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_error_analysis_aligns_with_gold_and_metrics(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    review_path = project_root / "output_calibrated" / "review_queue.csv"
    gold_path = project_root / "gold" / "gold_extractions.csv"
    metrics_path = project_root / "evaluation" / "model_vs_gold_metrics.csv"
    error_path = tmp_path / "error_analysis.csv"
    report_path = tmp_path / "prompt_calibration_report.md"

    result = generate_analysis(
        review_path,
        gold_path,
        metrics_path,
        error_path,
        report_path,
    )
    analysis = _read_csv(error_path)
    gold = _read_csv(gold_path)
    metrics = _read_csv(metrics_path)
    overall = next(row for row in metrics if row["extraction_class"] == "__overall__")

    assert list(analysis[0]) == list(ERROR_FIELDNAMES)
    assert result["analysis_rows"] == int(overall["reviewed_rows"])
    assert len(analysis) == int(overall["reviewed_rows"])
    assert sum(row["in_gold"] == "1" for row in analysis) == len(gold)
    assert result["gold_linked_rows"] == len(gold)

    observed: dict[str, Counter[str]] = defaultdict(Counter)
    for row in analysis:
        observed[row["metric_class"]][row["review_conclusion"]] += 1
    for row in metrics:
        extraction_class = row["extraction_class"]
        if extraction_class == "__overall__":
            continue
        assert observed[extraction_class]["正确"] == int(row["correct_rows"])
        assert observed[extraction_class]["已修改"] == int(row["modified_rows"])
        assert observed[extraction_class]["删除"] == int(row["deleted_rows"])

    required_error_types = {
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
    }
    assert required_error_types.issubset(set(ERROR_TYPES))

    report = report_path.read_text(encoding="utf-8")
    for heading in (
        "各类别复核结果",
        "字段修改次数",
        "道路管制与交通引导混淆",
        "多阶段活动时间与多日期管制时间",
        "发布机关与发布日期误判",
        "重复抽取和漏抽典型案例",
        "第一轮提示词校准建议",
    ):
        assert heading in report
