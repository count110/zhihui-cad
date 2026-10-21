"""设备图：重新生成的差量、脱离参数的图元、版本与撤销、函数层的错误路径、IR 往返（任务书 10.1 “重新生成”“IR”“函数”几条）。"""
import copy
import json

import pytest

from gen_helpers import Env
from zhihui.agent.general.prompts import EQUIP_FEW_SHOTS
from zhihui.cad.equipment import check as CHK
from zhihui.cad.equipment import sheet as SH
from zhihui.ir import Drawing
from zhihui.ir.vessel import VesselPatchIn

V_SPEC = EQUIP_FEW_SHOTS[0][1]["workflow_steps"][0]["payload"]["args"]["spec"]
H_SPEC = EQUIP_FEW_SHOTS[5][1]["workflow_steps"][0]["payload"]["args"]["spec"]


def make(tmp_path, spec=None):
    e = Env(tmp_path)
    did = e.new("hg_equipment_a1")
    e.call("vessel_define", did, op="create", spec=copy.deepcopy(spec or V_SPEC))
    return e, did


def gen(d):
    return {x.id: (x.kind, json.dumps(x.data, sort_keys=True), x.space) for x in d.entities if "equip:auto" in x.tags}


# ------------------------------------------------------------------ 差量
def test_regenerate_same_params_changes_nothing(tmp_path):
    e, did = make(tmp_path)
    d = e.d(did)
    before = gen(d)
    res = SH.apply(d, SH.compose(d.vessel))
    assert res["created"] == [] and res["modified"] == [] and res["deleted"] == []
    assert gen(d) == before
    assert all(i.startswith("EQ-") for i in before)


def test_rotating_one_nozzle_only_touches_related_entities(tmp_path):
    e, did = make(tmp_path)
    before = gen(e.d(did))
    e.call("vessel_define", did, op="patch", set={"nozzles[purpose=进料口].orientation_deg": 90})
    after = gen(e.d(did))
    changed = {i for i in set(before) | set(after) if before.get(i) != after.get(i)}
    mark = next(n.mark for n in e.d(did).vessel.nozzles if n.purpose == "进料口")
    # 相关 = 该管口自己的图元（主视图/方位图/尺寸）、件号和明细栏、管口表、技术要求（默认项说明可能变）
    related = (f"-NZ-{mark}-", f"DIM-NZ-{mark}-", "-BAL-", "-TBL-", "-TECH-")
    unrelated = sorted(i for i in changed if not any(k in i for k in related))
    assert changed, "改了方位，至少该管口的图元要变"
    assert not unrelated, f"与进料口无关的图元也变了：{unrelated[:8]}"
    others = [n.mark for n in e.d(did).vessel.nozzles if n.mark != mark]
    for m in others:
        assert all(before.get(i) == after.get(i) for i in before if f"-NZ-{m}-" in i)


def test_user_entities_survive_regeneration(tmp_path):
    e, did = make(tmp_path)
    e.call("draw_linear_primitives", did, primitive_type="line", points=["50000,0", "50100,0"])
    lid = e.ids(did, "line")[-1]
    assert not any(t.startswith("equip") for t in e.ent(did, lid).tags)
    e.call("vessel_define", did, op="patch", add={"shell.length": 500})
    assert e.ent(did, lid).data["p2"][0] == 50100


def test_editing_generated_entity_detaches_it(tmp_path):
    e, did = make(tmp_path)
    gid = next(x.id for x in e.d(did).entities if x.kind == "line" and "equip:auto" in x.tags and x.space == "Model")
    r = e.call("transform_entities", did, operation="move", entity_ids=[gid], displacement=[10, 0])
    assert any("脱离参数" in w for w in r.get("warnings", []))
    ent = e.ent(did, gid)
    assert "equip:detached" in ent.tags and "equip:auto" not in ent.tags
    moved = json.dumps(ent.data, sort_keys=True)
    e.call("vessel_define", did, op="patch", set={"design.medium": "水"})
    assert json.dumps(e.ent(did, gid).data, sort_keys=True) == moved          # 重新生成不覆盖它


def test_deleting_generated_entity_is_remembered(tmp_path):
    e, did = make(tmp_path)
    gid = next(x.id for x in e.d(did).entities if x.kind == "line" and "equip:auto" in x.tags and x.space == "Model")
    e.call("erase_entities", did, entity_ids=[gid])
    assert gid in e.d(did).vessel.drawing["detached"]
    e.call("vessel_define", did, op="patch", set={"design.medium": "水"})
    assert e.d(did).get_entity(gid) is None                                    # 删掉的生成图元不再画回来


def test_copies_of_generated_entities_become_user_entities(tmp_path):
    e, did = make(tmp_path)
    gid = next(x.id for x in e.d(did).entities if x.kind == "line" and "equip:auto" in x.tags and x.space == "Model")
    r = e.call("replicate_entities", did, operation="copy", entity_ids=[gid], base_point="0,0", target_points=["0,50000"])
    new = r["created"][0]
    assert not any(t.startswith("equip") for t in e.ent(did, new).tags)
    e.call("vessel_define", did, op="patch", set={"design.medium": "水"})
    assert e.d(did).get_entity(new) is not None


def test_generated_entities_not_in_recent(tmp_path):
    e, did = make(tmp_path)
    assert all(not r["entity_id"].startswith("EQ-") for r in e.d(did).session.recent_entities)


# ------------------------------------------------------------------ 版本与撤销
def test_one_version_per_patch_and_none_on_incomplete(tmp_path):
    e, did = make(tmp_path)
    r0 = e.rev(did)
    e.call("vessel_define", did, op="patch", add={"shell.length": 500})
    assert e.rev(did) == r0 + 1
    r = e.err("vessel_define", did, op="patch", upsert_nozzles=[{"purpose_text": "放空口", "location": "top_head"}])
    assert r["error_code"] == "SPEC_INCOMPLETE" and e.rev(did) == r0 + 1
    assert any(p["path"].endswith(".dn") for p in r["pending"])


def test_undo_redo_restores_params_and_drawing(tmp_path):
    e, did = make(tmp_path)
    before = gen(e.d(did))
    e.call("vessel_define", did, op="patch", add={"shell.length": 500})
    assert e.d(did).vessel.shell.length_mm == 2900
    e.call("history_undo_redo", did, action="undo", steps=1)
    d = e.d(did)
    assert d.vessel.shell.length_mm == 2400 and gen(d) == before
    e.call("history_undo_redo", did, action="redo", steps=1)
    assert e.d(did).vessel.shell.length_mm == 2900


# ------------------------------------------------------------------ 函数层错误路径
def test_create_twice_or_on_user_drawing_needs_confirm(tmp_path):
    e, did = make(tmp_path)
    assert e.err("vessel_define", did, op="create", spec=copy.deepcopy(V_SPEC))["error_code"] == "CONFIRM_REQUIRED"
    e2 = Env(tmp_path / "b")
    d2 = e2.new("gb_a3_mech")
    e2.call("draw_linear_primitives", d2, primitive_type="line", points=["0,0", "10,0"])
    assert e2.err("vessel_define", d2, op="create", spec=copy.deepcopy(V_SPEC))["error_code"] == "CONFIRM_REQUIRED"
    assert e2.call("vessel_define", d2, op="create", spec=copy.deepcopy(V_SPEC), confirmed=True)["ok"]


def test_patch_and_delete_paths(tmp_path):
    e, did = make(tmp_path)
    assert e.err("vessel_define", did, op="patch")["error_code"] == "MISSING_ARG"
    assert e.err("vessel_define", did, op="delete")["error_code"] == "CONFIRM_REQUIRED"
    e.call("vessel_define", did, op="delete", confirmed=True)
    d = e.d(did)
    assert d.vessel is None and not [x for x in d.entities if "equip:auto" in x.tags] and not [l for l in d.layouts if l.get("equip")]
    e3 = Env(tmp_path / "c")
    d3 = e3.new("hg_equipment_a1")
    assert e3.err("vessel_define", d3, op="patch", set={"design.medium": "水"})["error_code"] == "NO_VESSEL"


def test_query_lookup_check(tmp_path):
    e, did = make(tmp_path)
    for what in ("summary", "nozzles", "supports", "pending", "sources", "parts", "dims"):
        assert e.call("vessel_query", did, what=what)["ok"]
    noz = e.call("vessel_query", did, what="nozzles", filter="purpose=液位计口")["data"]["nozzles"]
    assert len(noz) == 2 and all(n["dn"] == 25 for n in noz)
    r = e.call("vessel_lookup_standard", None, table_id="pipe_dims", keys={"series": "B", "dn": 80})
    assert r["data"]["row"]["od"] == 89 and r["data"]["verified"] is False
    assert e.err("vessel_lookup_standard", None, table_id="pipe_dims", keys={"series": "B", "dn": 85})["error_code"] == "STD_SIZE_NOT_FOUND"
    c = e.call("vessel_check", did)["data"]
    assert c["errors"] == []


def test_unverified_tables_flagged_on_drawing_and_reply(tmp_path):
    e = Env(tmp_path)
    did = e.new("hg_equipment_a1")
    r = e.call("vessel_define", did, op="create", spec=copy.deepcopy(V_SPEC))
    d = e.d(did)
    assert "pipe_dims" in d.vessel.drawing["unverified_tables"]
    assert any(w.startswith("W_UNVERIFIED_TABLE") for w in r["warnings"])
    texts = " ".join(x.data.get("text", "") for x in d.entities if x.kind == "text" and x.space == SH.LAYOUT)
    assert "未经核对" in texts and "不得直接用于制造" in texts and "（方案图）" in texts


@pytest.mark.parametrize("spec", [
    {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 600, "length_mm": 1200, "thickness_mm": 6}, "nozzles": [{"dn": 50, "purpose_text": "进料口"}, {"dn": 50, "purpose_text": "出料口"}], "supports": [{"kind": "leg", "count": 3, "height_mm": 600}]},
    {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 3000, "length_mm": 6000, "thickness_mm": 16}, "heads": [{"kind": "THA"}], "nozzles": [{"dn": 600, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 150, "purpose_text": "进料口"}, {"dn": 150, "purpose_text": "出料口"}], "supports": [{"kind": "lug", "count": 4}], "design": {"design_pressure_mpa": 1.6, "design_temp_c": 150}},
    {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1200, "length_mm": 2000, "thickness_mm": 10}, "heads": [{"kind": "FLAT", "thickness_mm": 30}], "nozzles": [{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}], "supports": [{"kind": "lug", "count": 4}], "design": {"design_pressure_mpa": 0.3}},
    H_SPEC,
])
def test_boundary_specs_generate_without_check_errors(tmp_path, spec):
    e, did = make(tmp_path, spec)
    d = e.d(did)
    assert CHK.run(d.vessel, d)["errors"] == []
    assert e.svc.validate_drawing(did)["passed"]


# ------------------------------------------------------------------ IR
def test_vessel_roundtrip_and_old_drawings_unaffected(tmp_path):
    e, did = make(tmp_path)
    d = e.d(did)
    again = Drawing.model_validate_json(d.to_json())
    assert again.vessel == d.vessel
    old = Drawing(id="dwg_x")
    assert "vessel" not in json.loads(old.compat_json())
    assert Drawing.model_validate_json(old.to_json()).vessel is None
    with pytest.raises(Exception):
        VesselPatchIn.model_validate({"kind": "vertical_vessel", "shell": {"inner_diameter": 1600}})       # 嵌套字段写错名字要报错，不能静默丢弃
