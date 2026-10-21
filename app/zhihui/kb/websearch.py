"""Web search for equipment the local knowledge base does not know (DeepSeek Search API).

Only the equipment *name* is sent as the query - never the user's full requirement, tags or numbers.
Results are treated as untrusted data: they only ever become evidence text for the symbol designer role,
which cannot call tools. Fetch is deliberately not used (cost, SSRF surface).

Env:
  ZHIHUI_SYMBOL_SEARCH   auto (default: on when a DeepSeek key exists and the LLM is not mock) / on / off
  DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL (default https://api.deepseek.com)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Hit:
    title: str
    url: str
    summary: str
    score: float = 0.0
    authority: int = -1


@dataclass
class Evidence:
    hits: list[Hit] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    calls: int = 0
    error: str = ''


def enabled() -> bool:
    mode = os.environ.get('ZHIHUI_SYMBOL_SEARCH', 'auto').lower()
    if mode == 'off':
        return False
    if not os.environ.get('DEEPSEEK_API_KEY'):
        return False
    if mode == 'on':
        return True
    return os.environ.get('ZHIHUI_LLM', 'real') != 'mock'


def _post(path: str, body: dict, timeout: float) -> dict:
    base = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
    req = urllib.request.Request(base + path, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 headers={'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def search(query: str, count: int = 8, timeout: float = 12.0) -> list[Hit]:
    """One Search API call. Raises on transport/HTTP errors; the caller decides to degrade."""
    data = _post('/v1/tools/search', {'query': query[:128], 'count': max(1, min(int(count), 50)), 'need_content': False}, timeout)
    return [Hit(title=str(x.get('title', ''))[:120], url=str(x.get('url', '')), summary=str(x.get('summary') or x.get('snippet') or '')[:700],
                score=float(x.get('rerank_score') or 0), authority=int(x.get('authority') if x.get('authority') is not None else -1))
            for x in data.get('results', []) if str(x.get('url', '')).startswith(('http://', 'https://'))]


def gather_equipment_evidence(name: str, max_hits: int = 5) -> Evidence:
    """Two short queries about the equipment's symbol and structure; keep the best-ranked, de-duplicated hits."""
    ev = Evidence()
    name = name.strip()[:30]
    seen: set[str] = set()
    pool: list[Hit] = []
    for q in (f'{name} 工艺流程图 设备图例 符号画法', f'{name} 设备结构 组成 进出口 工作原理'):
        ev.queries.append(q)
        try:
            ev.calls += 1
            for h in search(q):
                if h.url not in seen:
                    seen.add(h.url)
                    pool.append(h)
        except urllib.error.HTTPError as e:
            ev.error = f'检索失败 HTTP {e.code}'
            if e.code in (401, 402, 403):
                break
        except Exception as e:  # network, timeout, bad JSON: degrade, never fail the drawing
            ev.error = '检索失败：' + type(e).__name__
            break
    good = [h for h in pool if h.score >= 1.0 or h.authority >= 3] or pool[:3]
    good.sort(key=lambda h: (-(h.score), -h.authority))
    ev.hits = good[:max_hits]
    return ev
