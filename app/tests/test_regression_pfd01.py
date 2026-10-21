"""回归：网页任务「生活垃圾焚烧烟气处理工艺流程图（PFD-01）」工具调用超过 40 次失败。

原因有三：
1. 「余热回收后烟气」在锅炉和急冷塔之间放不下，引出线候选只往下找 15 mm，最后压在急冷塔上 → E_STD_TEXT_LINE；
   错误里只给了文字、没给对象，模型只好把设备上下来回挪，直到调用次数用完。
2. 管道来源（pipe_terminal 接 out 口）的位号写在箭头右边，正好挡住 out 口的引出段，助燃空气、急冷水两条辅助管永远布不通。
3. 介质名超过 12 字被硬截断，「生活垃圾（抓斗/料斗给料，非烟气管线）」截成半个括号。
"""

from zhihui.agent.llm import DEMO_PLAN
from zhihui.tools.core import ToolService, short_medium

MEDIA = ["生活垃圾（抓斗/料斗给料，非烟气管线）", "高温烟气", "余热回收后烟气", "急冷后烟气", "除尘后烟气", "净化后烟气"]
AUX = [{"symbol_id": "pipe_terminal", "tag": "X-101", "name": "助燃空气"}, {"symbol_id": "pipe_terminal", "tag": "X-102", "name": "蒸汽"},
       {"symbol_id": "pipe_terminal", "tag": "X-103", "name": "急冷水"}, {"symbol_id": "pipe_terminal", "tag": "X-104", "name": "飞灰"}]
LINKS = [{"from_port": "X-101.out", "line_type": "auxiliary", "medium": "助燃空气", "to_port": "F-101.air_in"},
         {"from_port": "B-101.out", "line_type": "auxiliary", "medium": "蒸汽", "to_port": "X-102.in"},
         {"from_port": "X-103.out", "line_type": "auxiliary", "medium": "急冷水", "to_port": "T-101.spray_in"},
         {"from_port": "D-101.dust_out", "line_type": "auxiliary", "medium": "飞灰", "to_port": "X-104.in"}]


def test_pfd01_task_validates_and_exports():
    svc = ToolService()
    did = svc.create_drawing(title="生活垃圾焚烧烟气处理工艺流程图", paper_size="A3",
                             title_block={"drawing_name": "生活垃圾焚烧烟气处理工艺流程图", "drawing_no": "PFD-01", "scale_text": "不按比例"})["drawing_id"]
    assert svc.place_equipment(did, DEMO_PLAN["equipment"])["ok"]
    r = svc.connect_ports(did, [dict(c, medium=m) for c, m in zip(DEMO_PLAN["connections"], MEDIA)])
    assert r["ok"] and any("图上写作「生活垃圾」" in n for n in r["notes"])  # 超长介质名去掉括号里的说明，并告诉模型
    assert svc.auto_layout(did)["ok"]
    assert svc.validate_drawing(did)["passed"]
    assert svc.place_equipment(did, AUX)["ok"]
    assert svc.connect_ports(did, LINKS)["ok"]
    lay = svc.auto_layout(did)
    assert lay["ok"] and lay["mode"] == "incremental" and not lay["route_failed"]  # 管道来源也能布线
    v = svc.validate_drawing(did)
    assert v["passed"], v["errors"]
    assert svc.export_artifacts(did, ["dxf"])["ok"]


def test_short_medium():
    assert short_medium("余热回收后烟气") == "余热回收后烟气"
    assert short_medium("生活垃圾（抓斗/料斗给料，非烟气管线）") == "生活垃圾"
    out = short_medium("abc(defghijklmnopq")
    assert len(out) <= 12 and out.count("(") == out.count(")")
