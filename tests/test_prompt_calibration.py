"""Offline regressions for traffic-notice Prompt 2.2.0 calibration."""

from __future__ import annotations

import traffic_notice_test as pipeline


def _extractions(examples, extraction_class):
    return [
        extraction
        for example in examples
        for extraction in example.extractions
        if extraction.extraction_class == extraction_class
    ]


def test_prompt_version_and_multistage_event_time_regression() -> None:
    assert pipeline.PROMPT_VERSION == "2.2.0"
    assert "第一期/第二期/第三期" in pipeline.PROMPT
    assert "每个阶段分别输出一条 event_time" in pipeline.PROMPT
    assert "每个阶段必须分别输出一条 event_time" in (
        pipeline.EVENT_METADATA_PROMPT
    )

    staged_times = {
        extraction.extraction_text
        for extraction in _extractions(
            pipeline.EVENT_METADATA_EXAMPLES, "event_time"
        )
    }
    assert {
        "第一期：10月15日至19日；",
        "第二期：10月23日至27日；",
        "第三期：10月31日至11月4日。",
    }.issubset(staged_times)


def test_control_guidance_boundary_and_attribute_regression() -> None:
    boundary_example = next(
        example
        for example in pipeline.EXAMPLES
        if "国展大道（辛柴路至国瑞路段）" in example.text
    )
    control = next(
        extraction
        for extraction in boundary_example.extractions
        if extraction.extraction_class == "control_rule"
    )
    guidance = next(
        extraction
        for extraction in boundary_example.extractions
        if extraction.extraction_class == "traffic_guidance"
    )

    assert control.extraction_text in boundary_example.text
    assert guidance.extraction_text in boundary_example.text
    assert guidance.extraction_text not in control.extraction_text
    assert control.extraction_text not in guidance.extraction_text
    assert boundary_example.text.index(control.extraction_text) < boundary_example.text.index(
        guidance.extraction_text
    )
    assert set(control.attributes or {}) == set(
        pipeline.CONTROL_RULE_ATTRIBUTE_KEYS
    )
    assert set(guidance.attributes or {}) == set(
        pipeline.TRAFFIC_GUIDANCE_ATTRIBUTE_KEYS
    )
    assert guidance.attributes["guidance_type"] == "detour"
    assert guidance.attributes["route_or_location"] == "辛柴路、国瑞路、海河南道"
    assert guidance.attributes["action"] == "绕行"

    assert "强制规定\n及其例外子句" in pipeline.CONTROL_RULE_PROMPT
    assert "只抽取后半部连续的引导子句" in pipeline.ROUTE_GUIDANCE_PROMPT
    assert "禁止停车" in pipeline.SERVICE_GUIDANCE_PROMPT
    assert "属于 control_rule" in pipeline.SERVICE_GUIDANCE_PROMPT


def test_publication_metadata_negative_example_regression() -> None:
    authority_negative = next(
        example
        for example in pipeline.NOTICE_METADATA_EXAMPLES
        if "特此公告" in example.text
    )
    assert [
        extraction.extraction_class for extraction in authority_negative.extractions
    ] == ["publication_date"]
    assert authority_negative.extractions[0].extraction_text == "2026年10月"

    date_negative = next(
        example
        for example in pipeline.NOTICE_METADATA_EXAMPLES
        if example.text == "10月16日至18日，重庆奥体中心将举行演唱会。"
    )
    assert date_negative.extractions == []
    assert "只接受原文末尾明确落款" in pipeline.NOTICE_METADATA_PROMPT
    assert "活动日期、管制日期和网页时间不是发布日期" in (
        pipeline.NOTICE_METADATA_PROMPT
    )


def test_prompt_deduplication_and_example_grounding_regression() -> None:
    prompts = (
        pipeline.PROMPT,
        pipeline.EVENT_METADATA_PROMPT,
        pipeline.NOTICE_METADATA_PROMPT,
        pipeline.CONTROL_RULE_PROMPT,
        pipeline.ROUTE_GUIDANCE_PROMPT,
        pipeline.SERVICE_GUIDANCE_PROMPT,
    )
    assert all("原文位置" in prompt and "去重" in prompt for prompt in prompts)
    assert "extraction_class + 原文出现区间 + extraction_text" in (
        pipeline.PROMPT
    )

    for example in pipeline.EXAMPLES:
        seen: set[tuple[str, str]] = set()
        for extraction in example.extractions:
            assert extraction.extraction_text in example.text
            key = (extraction.extraction_class, extraction.extraction_text)
            assert key not in seen
            seen.add(key)


def test_multidate_control_time_binding_regression() -> None:
    assert "多日期、进场/散场或不同分时段" in pipeline.PROMPT
    assert "每条规则只能绑定自己的原文时段" in (
        pipeline.CONTROL_RULE_PROMPT
    )
    assert "不得自行展开或推算日期" in pipeline.NOTICE_METADATA_PROMPT

    boundary_example = next(
        example
        for example in pipeline.EXAMPLES
        if "管制时间：2026年10月18日22时至10月29日22时" in example.text
    )
    expected_time = "2026年10月18日22时至10月29日22时"
    assert any(
        extraction.extraction_class == "control_time"
        and extraction.extraction_text == expected_time
        for extraction in boundary_example.extractions
    )
    assert all(
        (extraction.attributes or {}).get("time_text") == expected_time
        for extraction in boundary_example.extractions
        if extraction.extraction_class in {"control_rule", "traffic_guidance"}
    )
