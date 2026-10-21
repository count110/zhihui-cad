"""网页“设置”：填写 / 更换大模型 API Key、模型名、接口地址。

- 只接受白名单里的变量；密钥只写不读（GET 只返回“已设置 + 末 4 位”），留空表示不改。
- 改动写回 app/.env（保留注释和其他行）并立刻写入当前进程环境变量：每个任务都会重新读取，无需重启。
- 只允许本机访问；服务若开放到局域网，需在 .env 里设 ZHIHUI_ADMIN_TOKEN，并在请求头 X-Zhihui-Admin 里带上。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from zhihui.env import ENV_FILE

PROVIDERS = {
    "deepseek": {"label": "DeepSeek", "key": "DEEPSEEK_API_KEY", "model": "DEEPSEEK_MODEL", "base": "DEEPSEEK_BASE_URL"},
    "qwen": {"label": "通义千问（阿里云百炼）", "key": "DASHSCOPE_API_KEY", "model": "QWEN_MODEL", "base": "QWEN_BASE_URL"},
    "glm": {"label": "智谱 GLM", "key": "ZHIPUAI_API_KEY", "model": "GLM_MODEL", "base": "GLM_BASE_URL"},
    "openai": {"label": "OpenAI 兼容接口", "key": "OPENAI_API_KEY", "model": "OPENAI_MODEL", "base": "OPENAI_BASE_URL"},
}
CHOICES = {"ZHIHUI_LLM": ("real", "mock"), "ZHIHUI_LLM_PRIMARY": tuple(PROVIDERS), "ZHIHUI_LLM_BACKUP": ("", *PROVIDERS),
           "ZHIHUI_SYMBOL_SEARCH": ("auto", "on", "off")}
SECRETS = {p["key"] for p in PROVIDERS.values()}
PLAIN = {p["model"] for p in PROVIDERS.values()} | {p["base"] for p in PROVIDERS.values()}
ALLOWED = SECRETS | PLAIN | set(CHOICES)
LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


class SettingsError(ValueError):
    pass


def allowed_client(host: str | None, token: str | None) -> bool:
    admin = os.environ.get("ZHIHUI_ADMIN_TOKEN", "")
    if admin:
        return token == admin
    return (host or "") in LOCAL


def mask(value: str) -> str:
    return ("…" + value[-4:]) if len(value) >= 8 else "已设置"


def current() -> dict:
    out = {"providers": [], "choices": {k: list(v) for k, v in CHOICES.items()}}
    for k in CHOICES:
        out[k] = os.environ.get(k, {"ZHIHUI_LLM": "real", "ZHIHUI_LLM_PRIMARY": "deepseek", "ZHIHUI_SYMBOL_SEARCH": "auto"}.get(k, ""))
    from zhihui.agent.llm import PROVIDERS as DEFAULTS
    for pid, p in PROVIDERS.items():
        key = os.environ.get(p["key"], "")
        out["providers"].append({"id": pid, "label": p["label"], "key_set": bool(key), "key_hint": mask(key) if key else "",
                                 "model": os.environ.get(p["model"], ""), "model_default": DEFAULTS[pid]["model"],
                                 "base_url": os.environ.get(p["base"], ""), "base_default": DEFAULTS[pid]["base_url"]})
    return out


def _check(name: str, value: str) -> str:
    value = value.strip()
    if name in CHOICES:
        if value not in CHOICES[name]:
            raise SettingsError(f"{name} 只能是 {'/'.join(x or '（不用）' for x in CHOICES[name])}")
    elif name in SECRETS:
        if not re.fullmatch(r"[\x21-\x7e]{8,300}", value):
            raise SettingsError("API Key 格式不对：应为不含空格的英文字符，至少 8 位")
    elif name.endswith("_BASE_URL"):
        if value and not re.fullmatch(r"https?://[^\s\"'<>]{3,200}", value):
            raise SettingsError("接口地址应以 http:// 或 https:// 开头")
    elif name.endswith("_MODEL"):
        if value and not re.fullmatch(r"[\w.\-:/]{1,80}", value):
            raise SettingsError("模型名只能包含字母、数字和 . - _ : /")
    return value


def update(changes: dict, clear: list[str] | None = None, path: Path | None = None) -> list[str]:
    """changes: {变量名: 值}；密钥值为空表示不改。clear：要删除的密钥。返回实际改动的变量名（不含值）。"""
    todo: dict[str, str | None] = {}
    for name, value in (changes or {}).items():
        if name not in ALLOWED:
            raise SettingsError(f"不允许修改 {name}")
        value = "" if value is None else str(value)
        if name in SECRETS and not value.strip():
            continue
        value = _check(name, value)
        if value == os.environ.get(name, "") and name not in SECRETS:
            continue      # unchanged: don't rewrite the line
        todo[name] = value
    for name in clear or []:
        if name not in SECRETS:
            raise SettingsError(f"只能清除 API Key，不能清除 {name}")
        todo[name] = None
    if not todo:
        return []
    path = Path(path or ENV_FILE)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else ["# API Key 仅保存在本机 .env"]
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in todo:
            seen.add(key)
            lines[i] = f"{key}={todo[key]}" if todo[key] is not None else f"{key}="
    lines += [f"{k}={v}" for k, v in todo.items() if k not in seen and v is not None]
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    for k, v in todo.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return sorted(todo)


async def test_provider(provider: str) -> dict:
    """用当前配置真的连一次：列模型（不产生对话费用）；列模型不支持时发一个极短对话。"""
    if provider not in PROVIDERS:
        raise SettingsError("未知的模型提供方")
    from zhihui.agent.llm import OpenAICompatLLM
    try:
        llm = OpenAICompatLLM(provider, timeout=15)
    except RuntimeError:
        return {"ok": False, "message": "还没有填写这个提供方的 API Key"}
    try:
        try:
            models = [m.id async for m in llm.client.models.list()]
            found = llm.model in models
            return {"ok": True, "model": llm.model, "model_listed": found,
                    "message": f"连接成功，模型 {llm.model}" + ("可用" if found else "不在该账号的模型列表里，请核对模型名")}
        except Exception:
            r = await llm.client.chat.completions.create(model=llm.model, messages=[{"role": "user", "content": "回复OK"}], max_tokens=2)
            return {"ok": True, "model": llm.model, "model_listed": True, "message": f"连接成功，模型 {r.model or llm.model} 可用"}
    except Exception as ex:
        text = str(ex)
        code = getattr(ex, "status_code", None)
        hint = {401: "API Key 无效", 402: "账户余额不足", 403: "无权访问", 404: "模型名或接口地址不对", 429: "请求太频繁，稍后再试"}.get(code, "")
        return {"ok": False, "message": (hint or "连接失败") + f"（{type(ex).__name__}{' ' + str(code) if code else ''}）", "detail": text[:200].replace(os.environ.get(PROVIDERS[provider]['key'], '@@'), '***')}
    finally:
        await llm.client.close()
