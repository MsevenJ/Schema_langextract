"""Offline regression tests for the traffic-notice pipeline."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import langextract as lx

import traffic_notice_test as pipeline
from traffic_schema.coverage import audit_coverage
from traffic_schema.grounding import GROUNDING_WHITESPACE_NORMALIZED
from traffic_schema.ids import build_run_metadata, evidence_id, extraction_id, source_id
from traffic_schema.kg_export import KG_FILES, export_kg
from traffic_schema.review_export import REVIEW_FIELDNAMES, build_review_rows, write_review_queue
from traffic_schema.validation import control_measure_status
from traffic_schema.validation import extract_exception_text


PROJECT_DIR = Path(__file__).resolve().parents[1]
NOTICE_DIR = PROJECT_DIR / "test_notices"


def _notice(name: str) -> str:
    return (NOTICE_DIR / name).read_text(encoding="utf-8").strip()


def _extraction(
    extraction_class: str,
    text: str,
    source: str,
    *,
    attributes: dict | None = None,
    start: int | None = None,
    end: int | None = None,
) -> lx.data.Extraction:
    if start is None:
        start = source.index(text)
    if end is None:
        end = start + len(text)
    return lx.data.Extraction(
        extraction_class=extraction_class,
        extraction_text=text,
        char_interval=lx.data.CharInterval(start_pos=start, end_pos=end),
        attributes=attributes or {},
    )


def test_web_page_time_cannot_bind_to_notice04_first_rule() -> None:
    source = _notice("notice04.txt")
    rule_text = (
        "在广交会展馆（A、B、C、D区）实施交通管制，"
        "除持有广交会车证的车辆外，禁止其他车辆进入。"
    )
    rule = _extraction(
        "control_rule",
        rule_text,
        source,
        attributes={
            "road_names": ["广交会展馆"],
            "time_text": "2026-10-14 11:09",
        },
    )
    bound = pipeline.bind_control_rule_time(rule, source, [])
    assert bound.attributes["time_text"] == ""
    assert "2026-10-14 11:09" not in bound.attributes["time_text"]


def test_same_phase_heading_time_is_inherited() -> None:
    source = _notice("notice06.txt")
    rule_text = "滨江大道（南浦路-西湖云庄前）禁止中重型货车通行；"
    rule = _extraction(
        "control_rule",
        rule_text,
        source,
        attributes={"road_names": ["滨江大道"], "time_text": ""},
    )
    bound = pipeline.bind_control_rule_time(rule, source, [])
    assert bound.attributes["time_text"] == "16:00—19:30"


def test_whitespace_only_difference_is_safely_grounded() -> None:
    source = "同安路（甲路至乙路）禁止\n\t车辆通行。"
    model_text = "同安路（甲路至乙路）禁止 车辆通行。"
    extraction = lx.data.Extraction(
        extraction_class="control_rule",
        extraction_text=model_text,
        char_interval=lx.data.CharInterval(start_pos=0, end_pos=len(source)),
        attributes={
            "road_names": ["同安路"],
            "road_range": "甲路至乙路",
            "direction": "",
            "time_text": "",
            "measure": "禁止车辆通行",
            "vehicle_scope": "车辆",
            "condition": "",
            "exception": "",
        },
    )
    accepted, rejected, _ = pipeline.split_extractions([extraction], source)
    assert not rejected
    assert len(accepted) == 1
    assert accepted[0].extraction_text == source
    assert (
        getattr(accepted[0], "_grounding_status")
        == GROUNDING_WHITESPACE_NORMALIZED
    )
    assert accepted[0].char_interval.start_pos == 0
    assert accepted[0].char_interval.end_pos == len(source)


def test_non_whitespace_road_or_number_difference_is_rejected() -> None:
    source = "同安路禁止车辆通行。"
    extraction = lx.data.Extraction(
        extraction_class="control_rule",
        extraction_text="银川路禁止车辆通行。",
        char_interval=lx.data.CharInterval(start_pos=0, end_pos=len(source)),
        attributes={"road_names": ["银川路"], "time_text": ""},
    )
    accepted, rejected, _ = pipeline.split_extractions([extraction], source)
    assert not accepted
    assert any(
        "定位出的原文与 extraction_text 不一致" in reason
        for reason in rejected[0]["reasons"]
    )


def test_flow_control_and_diversion_is_mandatory_control() -> None:
    text = "在相关路口、节点视情采取临时交通控流和分流措施。"
    assert control_measure_status(text) == "mandatory"
    assert pipeline.is_strict_control_text(text)


def test_suffix_exception_is_preserved_and_not_vehicle_scope() -> None:
    source = _notice("notice07.txt")
    text = (
        "视情对会展辅路（国瑞路至会展中心南登录大厅南门路段）、"
        "海河南道南侧辅道（宁静东辅道至国瑞路段）由西向东禁止机动车通行"
        "（持农机展车辆通行证以及制式出租车除外）"
    )
    extraction = _extraction(
        "control_rule",
        text,
        source,
        attributes={
            "road_names": ["会展辅路", "海河南道南侧辅道"],
            "time_text": "2026年10月26日至10月28日，每日7时至18时",
            "vehicle_scope": "持农机展车辆通行证以及制式出租车除外",
            "exception": "",
        },
    )
    normalized = pipeline.normalize_extraction_attributes(extraction)
    assert normalized.attributes["exception"] == (
        "持农机展车辆通行证以及制式出租车除外"
    )
    assert normalized.attributes["vehicle_scope"] == ""
    assert extract_exception_text("除持证车辆外，禁止其他车辆进入") == "除持证车辆外"


def test_unextracted_notice06_tonghai_rule_is_coverage_unmatched() -> None:
    source = _notice("notice06.txt")
    stable_source_id = source_id(NOTICE_DIR / "notice06.txt", source)
    report = audit_coverage(source, [], stable_source_id)
    unmatched_texts = [item["text"] for item in report["unmatched_provisions"]]
    assert any("通海路（同心路-科园路）禁止车辆驶入" in text for text in unmatched_texts)
    assert report["needs_review"] is True


def test_notice05_missing_access_and_parking_items_are_audited() -> None:
    source = _notice("notice05.txt")
    stable_source_id = source_id(NOTICE_DIR / "notice05.txt", source)
    report = audit_coverage(source, [], stable_source_id)
    unmatched_texts = [item["text"] for item in report["unmatched_provisions"]]
    assert any("胶州湾大桥—华中路—火炬路" in text for text in unmatched_texts)
    assert any("市民健身中心内部停车场" in text for text in unmatched_texts)


def test_stable_ids_are_repeatable() -> None:
    source = "同安路禁止车辆通行。"
    stable_source_id_1 = source_id(Path("notice.txt"), source)
    stable_source_id_2 = source_id(Path("notice.txt"), source)
    assert stable_source_id_1 == stable_source_id_2
    assert extraction_id(stable_source_id_1, "control_rule", 0, 10) == extraction_id(
        stable_source_id_2, "control_rule", 0, 10
    )
    assert evidence_id(stable_source_id_1, 0, 10) == evidence_id(
        stable_source_id_2, 0, 10
    )


def test_review_csv_has_all_fields_and_utf8_bom() -> None:
    stable_source_id = "src_test"
    report = {
        "matched_provisions": [],
        "unmatched_provisions": [{
            "text": "6.通海路禁止车辆驶入；",
            "char_start": 10,
            "char_end": 24,
            "class_hint": "control_rule",
            "road_names": ["通海路"],
        }],
    }
    rows = build_review_rows(
        event_id_value="",
        stable_source_id=stable_source_id,
        file_name="notice06.txt",
        accepted=[],
        rejected=[],
        coverage_report=report,
    )
    with TemporaryDirectory(dir=PROJECT_DIR) as temporary_directory:
        path = write_review_queue(
            rows, Path(temporary_directory) / "review_queue.csv"
        )
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
        with path.open("r", encoding="utf-8-sig", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            assert tuple(reader.fieldnames or ()) == REVIEW_FIELDNAMES
            row = next(reader)
    assert row["review_status"] == "pending"
    assert row["is_correct"] == ""
    assert row["coverage_status"] == "unmatched"


def test_existing_four_outputs_are_still_saved() -> None:
    source = "同安路禁止车辆通行。"
    with TemporaryDirectory(dir=PROJECT_DIR) as temporary_directory:
        tmp_path = Path(temporary_directory)
        input_path = tmp_path / "sample.txt"
        input_path.write_text(source, encoding="utf-8")
        extraction = _extraction(
            "control_rule",
            source,
            source,
            attributes={
                "road_names": ["同安路"],
                "road_range": "",
                "direction": "",
                "time_text": "",
                "measure": "禁止车辆通行",
                "vehicle_scope": "车辆",
                "condition": "",
                "exception": "",
            },
        )
        setattr(extraction, "_grounding_status", "exact")
        setattr(extraction, "_validation_status", "accepted")
        metadata = build_run_metadata(
            schema_version=pipeline.SCHEMA_VERSION,
            prompt_version=pipeline.PROMPT_VERSION,
            model_name="qwen2.5:7b",
            ollama_url="http://127.0.0.1:11434",
            input_path=input_path,
            source_text=source,
            run_timestamp="2026-11-04T00:00:00+00:00",
        )
        pipeline.annotate_extraction_identity(
            extraction,
            source,
            metadata["source_id"],
            default_validation_status="accepted",
        )
        result = lx.data.AnnotatedDocument(
            document_id="random-langextract-id",
            text=source,
            extractions=[extraction],
        )
        paths = pipeline.save_results(
            result=result,
            source_text=source,
            all_extractions=[extraction],
            accepted=[extraction],
            rejected=[],
            input_path=input_path,
            output_dir=tmp_path,
            run_metadata=metadata,
        )
        assert set(paths) == {"raw", "accepted", "rejected", "visualization"}
        assert all(path.is_file() for path in paths.values())
        accepted_payload = json.loads(paths["accepted"].read_text(encoding="utf-8"))
        assert accepted_payload["source_id"] == metadata["source_id"]
        assert accepted_payload["document_id"] == metadata["source_id"]
        assert accepted_payload["extractions"][0]["extraction_id"].startswith("ext_")


def test_kg_export_marks_machine_facts_pending() -> None:
    source = "星光音乐会在海州体育馆举行。同安路禁止车辆通行。"
    event = _extraction("event_name", "星光音乐会", source)
    venue = _extraction("event_venue", "海州体育馆", source)
    rule = _extraction(
        "control_rule",
        "同安路禁止车辆通行。",
        source,
        attributes={
            "road_names": ["同安路"],
            "time_text": "",
            "measure": "禁止车辆通行",
        },
    )
    stable_source_id = source_id(Path("sample.txt"), source)
    for extraction in (event, venue, rule):
        pipeline.annotate_extraction_identity(
            extraction,
            source,
            stable_source_id,
            default_validation_status="accepted",
        )
    with TemporaryDirectory(dir=PROJECT_DIR) as temporary_directory:
        paths = export_kg(
            output_dir=Path(temporary_directory) / "kg_export",
            stable_source_id=stable_source_id,
            event_id_value="",
            accepted=[event, venue, rule],
        )
        assert set(paths) == set(KG_FILES)
        assert all(path.is_file() for path in paths.values())
        event_row = json.loads(paths["events.jsonl"].read_text(encoding="utf-8"))
        assert event_row["review_status"] == "pending"
        assert event_row["identity_status"] == "machine_candidate"
        relation_lines = paths["relations.jsonl"].read_text(encoding="utf-8").splitlines()
        relations = [json.loads(line)["relation"] for line in relation_lines]
        assert "held_at" in relations
        assert "has_control_rule" in relations
        assert "supported_by" in relations
