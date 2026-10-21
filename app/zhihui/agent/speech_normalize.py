"""口述规范化：把语音识别出来的口语变成规整的指令文本，再交给命令通道或 Planner。

1. 同音/近音误识别纠正（词表在 speech_lexicon.json，改词表不用改代码）
2. 中文数字 → 阿拉伯数字：一百二十点五→120.5，负十→-10，一百五→150（口语省略单位），二零二四→2024
3. 符号：逗号→,  相对/艾特→@  "100 小于 45"→100<45  零零→0,0  fai三十→⌀30  正负零点一→±0.1  H七→H7  R二十→R20
规范化后的文本是 normalized_utterance；原文一起保留，所有改动记在 changes 里（前端日志会显示“识别：…”）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

LEXICON = Path(__file__).with_name("speech_lexicon.json")
DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_NUM = re.compile(r"负?[零〇一二两三四五六七八九十百千万]+(?:点[零〇一二三四五六七八九]+)?")


@dataclass
class NormResult:
    original: str
    text: str
    changes: list[dict] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.text != self.original


@lru_cache(maxsize=1)
def lexicon() -> dict:
    return json.loads(LEXICON.read_text(encoding="utf-8"))


def _cn_int(s: str) -> int:
    total = section = num = 0
    last_unit = 1
    for i, c in enumerate(s):
        if c in DIG:
            num = DIG[c]
            continue
        u = UNIT[c]
        if u == 10000:
            section += num
            total = (total + (section or 1)) * u
            section = num = 0
        else:
            if num == 0 and u == 10 and (i == 0 or s[i - 1] in UNIT):
                num = 1                                              # “十五”“一万十”
            section += num * u
            num = 0
        last_unit = u
    n = total + section + num
    if len(s) >= 3 and s[-1] in DIG and s[-2] in "百千万" and DIG[s[-1]] > 0:
        n = total + section + DIG[s[-1]] * (last_unit // 10)       # 一百五 = 150、一千二 = 1200（口语省略末位单位）
    return n


def cn_to_number(tok: str) -> float | int | None:
    """"一百二十点五"→120.5。无法解析返回 None。"""
    neg = tok.startswith("负")
    s = tok[1:] if neg else tok
    frac = ""
    if "点" in s:
        s, frac = s.split("点", 1)
        if not s:
            s = "零"
    if not s or any(c not in DIG and c not in UNIT for c in s):
        return None
    n = int("".join(str(DIG[c]) for c in s)) if all(c in DIG for c in s) else _cn_int(s)     # 纯数字串逐位拼接：二零二四
    val: float | int = n
    if frac:
        if any(c not in DIG for c in frac):
            return None
        val = float(f"{n}." + "".join(str(DIG[c]) for c in frac))
    return -val if neg else val


def _fmt(v: float | int) -> str:
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _fullwidth(text: str) -> str:
    out = []
    for ch in text:
        o = ord(ch)
        if 0xFF10 <= o <= 0xFF19 or 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            out.append(chr(o - 0xFEE0))
        elif ch in "＠":
            out.append("@")
        elif ch in "＜":
            out.append("<")
        elif ch == "．":
            out.append(".")
        else:
            out.append(ch)
    return "".join(out)


def _skip_numeral(text: str, m: re.Match) -> bool:
    """“一”不是数字的场合（统一、一般…）；“零件”的零；“十字”“十分”。"""
    tok = m.group(0)
    lex = lexicon()
    s, e = m.start(), m.end()
    window = text[max(0, s - 1):min(len(text), e + 2)]
    if any(w in window and (tok in w) for w in lex.get("keep_one", [])):
        return True
    nxt = text[e:e + 1]
    if tok == "零" and nxt and nxt in "件部散碎星售用":
        return True
    if tok.endswith("十") and nxt and nxt in "字分":
        return True
    if tok in ("万",) or (tok.endswith("万") and nxt == "一"):
        return True
    prev = text[s - 1:s]
    if prev and (prev.isdigit() or prev == ".") and all(ch in "十百千万" for ch in tok):
        return True                 # “600千帕”“15千瓦”“1千”：阿拉伯数字后的千/百/万是单位前缀或量级词，单独换成数字会错（以前 600千帕 → 6000帕）
    # 只有一个“十/百/千/万”之类的单位字、前面没有数字，也没有点（如“十字”）已在上面排除；其余按数字处理
    return False


def normalize(text: str) -> NormResult:
    original = text
    t = _fullwidth(text.strip())
    changes: list[dict] = []
    lex = lexicon()

    def log(a: str, b: str, rule: str) -> None:
        if a != b:
            changes.append({"from": a, "to": b, "rule": rule})

    for a, b in lex["replace"]:                                   # 1 误识别词
        if a in t:
            t = t.replace(a, b)
            log(a, b, "误识别纠正")
    t = re.sub(r"零零(?!\d)", lambda m: (log("零零", "0,0", "坐标原点"), "0,0")[1], t)
    for a, b in (("正负", "±"), ("加减", "±")):                       # 要在“负”被当作负号之前处理
        if a in t:
            t = t.replace(a, b)
            log(a, b, "公差符号")

    def num_sub(m: re.Match) -> str:                               # 2 中文数字
        if _skip_numeral(t, m):
            return m.group(0)
        v = cn_to_number(m.group(0))
        if v is None:
            return m.group(0)
        r = _fmt(v)
        log(m.group(0), r, "中文数字")
        return r

    t = _NUM.sub(num_sub, t)
    for rx in lex["regex"]:                                       # 3 符号与前缀
        if rx["pattern"].startswith("零零"):
            continue
        new = re.sub(rx["pattern"], rx["repl"], t)
        if new != t:
            log(rx.get("note", rx["pattern"]), "", rx.get("note", "符号"))
            changes[-1]["from"], changes[-1]["to"] = _diff(t, new)
            t = new
    # 尺寸前缀：H七 → H7、R二十 → R20 已在数字转换里完成（数字紧跟字母）
    t = re.sub(r"\s{2,}", " ", t).strip()
    return NormResult(original, t, changes)


def _diff(a: str, b: str) -> tuple[str, str]:
    """给改动日志一个简短的前后对照（取第一处不同的小片段）。"""
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    return a[max(0, i - 2):len(a) - j + 2 if j else len(a)][:24], b[max(0, i - 2):len(b) - j + 2 if j else len(b)][:24]
