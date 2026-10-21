"""Web API：提交 → 轮询 → 签名下载；在结果上继续改；反问后续接。"""

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ZHIHUI_LLM", "mock")
    from zhihui.api.app import app

    with TestClient(app) as c:
        yield c


def wait(client, tid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = client.get(f"/api/tasks/{tid}").json()
        if t["status"] in ("READY", "FAILED", "WAITING_CONFIRMATION", "CANCELLED"):
            return t
        time.sleep(0.2)
    raise TimeoutError(tid)


def test_generate_modify_and_download(client):
    tid = client.post("/api/tasks", data={"prompt": "画垃圾焚烧烟气流程：垃圾储坑 V-101 → 焚烧炉 F-101 → 余热锅炉 B-101 → 布袋除尘器 D-101 → 引风机 F-201 → 烟囱 S-101"}).json()["id"]
    t = wait(client, tid)
    assert t["status"] == "READY", t
    names = [e["name"] for e in t["events"] if e["kind"] == "tool_call"]
    assert names[0] == "create_drawing" and names[-1] == "export_artifacts"
    dxf = next(a for a in t["result"]["artifacts"] if a["format"] == "dxf")
    assert client.get(dxf["url"]).status_code == 200
    assert client.get(dxf["url"].replace("sig=", "sig=0")).status_code == 403

    tid2 = client.post("/api/tasks", data={"prompt": "把 F-201 右移 800 mm", "parent_id": tid}).json()["id"]
    t2 = wait(client, tid2)
    assert t2["status"] == "READY" and t2["result"]["diff"]["moved"][0]["tag"] == "F-201"

    tid3 = client.post("/api/tasks", data={"prompt": "挪一下风机", "parent_id": tid}).json()["id"]
    t3 = wait(client, tid3)
    assert t3["status"] == "WAITING_CONFIRMATION" and t3["result"]["question"]
    assert client.post(f"/api/tasks/{tid3}/reply", json={"answer": "把 F-201 下移 500 mm"}).status_code == 200
    assert wait(client, tid3)["status"] == "READY"


def test_rejects_bad_upload(client):
    r = client.post("/api/tasks", data={"prompt": "改图"}, files={"file": ("a.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400
