"""图纸存储：每次工具调用成功就落一个新版本（JSON），失败则什么都不写——这就是工具的事务性。

目录结构（DATA_DIR 默认为项目下的 data/，可用环境变量 ZHIHUI_DATA_DIR 指定）：
  drawings/<drawing_id>/current.json      当前版本
  drawings/<drawing_id>/revs/0003.json    历史版本
  drawings/<drawing_id>/exports/r3/...    导出产物
  uploads/<task_id>/<name>.dxf            用户上传的原件（只读，永不覆写）
"""

from __future__ import annotations

import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from zhihui.ir import Drawing


def data_dir() -> Path:
    p = Path(os.environ.get("ZHIHUI_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


class DrawingStore:
    def __init__(self, root: Path | None = None):
        self._root = Path(root) if root else None
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @property
    def root(self) -> Path:
        """未指定时每次按环境变量解析，便于测试和评测切换数据目录。"""
        return self._root or data_dir()

    def _dir(self, drawing_id: str) -> Path:
        if not drawing_id.startswith("dwg_") or not drawing_id[4:].isalnum():
            raise KeyError(drawing_id)
        return self.root / "drawings" / drawing_id

    def new_id(self) -> str:
        return "dwg_" + uuid.uuid4().hex[:10]

    def exists(self, drawing_id: str) -> bool:
        try:
            return (self._dir(drawing_id) / "current.json").exists()
        except KeyError:
            return False

    def load(self, drawing_id: str, revision: int | None = None) -> Drawing:
        base = self._dir(drawing_id)
        f = base / "current.json" if revision is None else base / "revs" / f"{revision:04d}.json"
        if not f.exists():
            raise KeyError(drawing_id)
        return Drawing.model_validate_json(f.read_text(encoding="utf-8"))

    def save(self, d: Drawing) -> Drawing:
        """版本号 +1 后写入；先写临时文件再替换，避免半截文件。"""
        d.revision += 1
        base = self._dir(d.id)
        (base / "revs").mkdir(parents=True, exist_ok=True)
        text = d.to_json()
        rev = base / "revs" / f"{d.revision:04d}.json"
        rev.write_text(text, encoding="utf-8")
        tmp = base / "current.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, base / "current.json")
        return d

    def export_dir(self, d: Drawing) -> Path:
        p = self._dir(d.id) / "exports" / f"r{d.revision}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def restore(self, drawing_id: str, revision: int) -> Drawing:
        """Restore historical contents as a NEW revision; retain the full audit trail.

        The caller holds lock(drawing_id), just like any other mutation. A rollback
        must never reset the revision counter or overwrite a historical snapshot.
        """
        current = self.load(drawing_id)
        restored = self.load(drawing_id, revision)
        restored.revision = current.revision
        return self.save(restored)

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    def resolve_ref(self, ref: str) -> Path:
        """把 file_ref（相对 DATA_DIR 的路径）解析成真实路径，拒绝越界访问。"""
        p = (self.root / ref).resolve()
        if self.root.resolve() not in p.parents:
            raise PermissionError(ref)
        return p

    @contextmanager
    def lock(self, drawing_id: str):
        with self._guard:
            lk = self._locks.setdefault(drawing_id, threading.Lock())
        with lk:
            yield
