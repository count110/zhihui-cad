"""可选：用 ODA File Converter 把 DXF 转成 DWG（AutoCAD 2018 格式）。

ODA File Converter 是免费工具，需用户自行安装（https://www.opendesign.com/guestfiles/oda_file_converter）；
程序按环境变量 ZHIHUI_ODA → PATH → 默认安装目录的顺序找 ODAFileConverter，找不到就报 E_NO_ODA，不影响 DXF / PDF 导出。
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def find_oda() -> str | None:
    if os.environ.get("ZHIHUI_ODA") and Path(os.environ["ZHIHUI_ODA"]).is_file():
        return os.environ["ZHIHUI_ODA"]
    exe = shutil.which("ODAFileConverter")
    if exe:
        return exe
    pats = ["C:/Program Files/ODA/*/ODAFileConverter.exe", "/usr/bin/ODAFileConverter", "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"]
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def to_dwg(dxf_path: str | Path, out_path: str | Path, version: str = "ACAD2018") -> Path:
    """转一个文件；ODA 只能按目录转，所以先拷到临时目录。"""
    exe = find_oda()
    if exe is None:
        raise FileNotFoundError("没有找到 ODA File Converter")
    dxf_path, out_path = Path(dxf_path), Path(out_path)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        shutil.copy(dxf_path, Path(src) / dxf_path.name)
        subprocess.run([exe, src, dst, version, "DWG", "0", "1", dxf_path.name], check=True, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        produced = Path(dst) / (dxf_path.stem + ".dwg")
        if not produced.exists():
            raise RuntimeError("ODA File Converter 没有输出 DWG")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(produced, out_path)
    return out_path
