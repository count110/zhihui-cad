"""Knowledge-gap storage and administrator close-loop CLI."""
import argparse
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .search import KB_ROOT

SCHEMA='''CREATE TABLE IF NOT EXISTS knowledge_gaps (
id TEXT PRIMARY KEY,task_id TEXT NOT NULL,query TEXT NOT NULL,context_json TEXT NOT NULL,
fallback_used TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL)'''


class GapStore:
    def __init__(self, path=None):
        if path is None:
            from zhihui.tools.store import data_dir
            path=data_dir()/'zhihui.db'
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as c: c.execute(SCHEMA)

    def record(self,task_id,query,context,fallback):
        gid='gap_'+uuid.uuid4().hex[:12]
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO knowledge_gaps VALUES(?,?,?,?,?,?,?)',(gid,task_id,query,json.dumps(context,ensure_ascii=False),fallback,'open',time.time()))
        return gid

    def list(self):
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM knowledge_gaps ORDER BY created_at DESC')]

    def resolve(self,gap_id,alias=None,canonical=None,template=None,kb_root=KB_ROOT,case_dir=None):
        import re
        root=Path(kb_root)
        gap=next((r for r in self.list() if r['id']==gap_id),None)
        if gap is None: raise KeyError(gap_id)
        if not template and not (alias and canonical): raise ValueError('必须提供 --alias/--canonical 或 --template，不能只把记录标为解决')
        if template:
            raw=json.loads(Path(template).read_text(encoding='utf-8'))
            if not re.fullmatch(r'[a-z0-9_]+',raw['id']) or not raw.get('equipment') or not raw.get('source'):
                raise ValueError('模板缺少安全 id、equipment 或 source')
            dest=root/'process_templates'/(raw['id']+'.json');dest.write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            file=root/'aliases.json';aliases=json.loads(file.read_text(encoding='utf-8'));aliases[alias]=canonical
            file.write_text(json.dumps(aliases,ensure_ascii=False,indent=2),encoding='utf-8')
        context=json.loads(gap['context_json'])
        prompt=context.get('prompt',gap['query'])
        with sqlite3.connect(self.path) as c:
            if c.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone():
                row=c.execute('SELECT prompt FROM tasks WHERE id=?',(gap['task_id'],)).fetchone()
                if row: prompt=row[0]
        folder=Path(case_dir) if case_dir else Path(__file__).resolve().parents[2]/'06_测试与基准用例'/'cases'
        folder.mkdir(parents=True,exist_ok=True)
        number=max([10,*[int(p.stem[1:]) for p in folder.glob('S*.json') if re.fullmatch(r'S\d+',p.stem)]])+1
        case={'id':f'S{number:03d}','category':'S','prompt':prompt,'source':{'type':'resolved_knowledge_gap','gap_id':gap_id,'task_id':gap['task_id']},'regression':{'query':gap['query'],'canonical':canonical},'expected':{'outcome':['READY','READY_WITH_WARNINGS','WAITING_CONFIRMATION'],'max_tool_calls':40,'max_seconds':180}}
        case_path=folder/(case['id']+'.json');case_path.write_text(json.dumps(case,ensure_ascii=False,indent=2),encoding='utf-8')
        with sqlite3.connect(self.path) as c: c.execute("UPDATE knowledge_gaps SET status='resolved' WHERE id=?",(gap_id,))
        return case_path


def main():
    import sys
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--db',type=Path)
    sub=parser.add_subparsers(dest='command',required=True);sub.add_parser('list')
    resolve=sub.add_parser('resolve');resolve.add_argument('id');resolve.add_argument('--alias');resolve.add_argument('--canonical');resolve.add_argument('--template',type=Path)
    args=parser.parse_args();store=GapStore(args.db)
    if args.command=='list': print(json.dumps(store.list(),ensure_ascii=False,indent=2))
    else: print(store.resolve(args.id,args.alias,args.canonical,args.template))


if __name__=='__main__': main()
