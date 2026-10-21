import pytest


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """每个测试独立的数据目录，互不干扰。"""
    monkeypatch.setenv("ZHIHUI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ZHIHUI_MCP", "inproc")
    return tmp_path
