"""护栏：拦下猜目标、编数值；按用户原话查到的唯一匹配放行。"""

from zhihui.agent.guards import Guard


def msgs(text: str) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


def test_blocks_guessed_target_and_allows_mentioned_tag():
    g = Guard()
    m = msgs("把泵右移 300 mm")
    assert g.check("move_equipment", {"equipment_ref": "P-101", "dx": 300}, m)["error_code"] == "E_TARGET_UNCONFIRMED"
    assert g.check("move_equipment", {"equipment_ref": "P-101", "dx": 300}, msgs("把 P-101 右移 300 mm")) is None


def test_unique_find_result_from_user_words_confirms_target():
    g = Guard()
    m = msgs("把引风机右移 300 mm")
    g.observe("find_entities", {"query": "引风机"}, {"ok": True, "candidates": [{"ref": "F-201", "id": "E6", "score": 0.85}]}, m)
    assert g.check("move_equipment", {"equipment_ref": "F-201", "dx": 300}, m) is None


def test_ambiguous_or_self_invented_search_does_not_confirm():
    g = Guard()
    m = msgs("把泵右移 300 mm")
    two = {"ok": True, "ambiguous": "x", "candidates": [{"ref": "P-101", "score": 0.6}, {"ref": "P-102", "score": 0.6}]}
    g.observe("find_entities", {"query": "泵"}, two, m)
    # 模型自己猜一个位号再去查，不算用户确认
    g.observe("find_entities", {"tag": "P-101"}, {"ok": True, "candidates": [{"ref": "P-101", "score": 1.0}]}, m)
    assert g.check("move_equipment", {"equipment_ref": "P-101"}, m)["error_code"] == "E_TARGET_UNCONFIRMED"


def test_user_answer_confirms_target():
    g = Guard()
    m = msgs("把泵右移 300 mm") + [{"role": "tool", "tool_call_id": "c1", "content": '{"user_answer": "P-102"}'}]
    assert g.check("move_equipment", {"equipment_ref": "P-102"}, m) is None


def test_blocks_invented_numbers_but_allows_given_ones():
    g = Guard()
    m = msgs("画一个储罐 V-101，并标注它的直径和容积")
    r = g.check("add_annotation", {"anchor_ref": "V-101", "text": "直径：2.5m"}, m)
    assert r["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("add_annotation", {"anchor_ref": "V-101", "text": "直径：2.5m"}, msgs("储罐 V-101 直径 2.5 m")) is None
    # 位号里的数字不算数值
    assert g.check("add_annotation", {"anchor_ref": "V-101", "text": "见 V-101"}, m) is None
    bad = {"items": [{"symbol_id": "tank", "tag": "V-101", "params": {"容积": "50 m3"}}]}
    assert g.check("place_equipment", bad, m)["error_code"] == "E_UNSOURCED_VALUE"


def test_blocks_placeholder_annotation():
    g = Guard()
    m = msgs("画一个储罐 V-101，并标注它的直径和容积")
    assert g.check("add_annotation", {"anchor_ref": "V-101", "text": "直径：？"}, m)["error_code"] == "E_PLACEHOLDER_VALUE"
    assert g.check("add_annotation", {"anchor_ref": "V-101", "text": "原料储罐"}, m) is None


def test_full_relayout_needs_user_consent():
    g = Guard()
    args = {"drawing_id": "d", "mode": "full", "allow_full_relayout": True}
    assert g.check("auto_layout", args, msgs("在 P-101 后面加一个阀门"))["error_code"] == "E_RELAYOUT_UNCONFIRMED"
    assert g.check("auto_layout", args, msgs("加个阀门，可以整图重排")) is None
