"""Local BM25/alias/initials search. No network or model downloads."""
import json
import math
import re
from collections import Counter
from pathlib import Path

KB_ROOT=Path(__file__).parent


def tokens(value):
    value=str(value).lower()
    latin=re.findall(r'[a-z0-9_]+',value)
    chinese=re.findall(r'[\u4e00-\u9fff]+',value)
    return latin+[s[i:i+2] for s in chinese for i in range(max(1,len(s)-1))]


class SearchEngine:
    def __init__(self, root=KB_ROOT):
        self.root=Path(root)
        self.config=json.loads((self.root/'search_config.json').read_text(encoding='utf-8'))
        self.aliases=json.loads((self.root/'aliases.json').read_text(encoding='utf-8'))

    def documents(self, section=None, family=None):
        paths=[]
        if section in (None,'symbols'):
            paths += [('symbols',p) for p in (self.root/'symbols').glob('*.json')]
            from zhihui.cad.symbols import _generated_files
            if self.root==KB_ROOT: paths += [('symbols',p) for p in _generated_files()]
        if section in (None,'process_templates'):
            paths += [('process_templates',p) for p in (self.root/'process_templates').glob('*.json')]
        for name in ('codes','standards'):
            if section in (None,name): paths += [(name,p) for p in (self.root/name).glob('*.json')]
        out=[]
        for kind,path in paths:
            raw=json.loads(path.read_text(encoding='utf-8'))
            if family and raw.get('family')!=family: continue
            out.append({'id':raw.get('id',path.stem),'section':kind,'source':(str(path.relative_to(self.root)) if self.root in path.parents else 'generated/'+path.name), 'data':raw})
        return out

    def search(self, query, section=None, family=None, limit=3):
        normalized=str(query).lower().strip()
        for alias,canonical in self.aliases.items(): normalized=normalized.replace(alias,canonical)
        docs=self.documents(section,family)
        corpus=[Counter(tokens(json.dumps(d['data'],ensure_ascii=False))) for d in docs]
        query_tokens=set(tokens(normalized));n=len(docs);avg=sum(sum(c.values()) for c in corpus)/max(n,1)
        frequency={word:sum(word in c for c in corpus) for word in query_tokens}
        scored=[]
        for d,c in zip(docs,corpus):
            raw=d['data']; total=sum(c.values());score=0.
            for word in query_tokens:
                tf=c[word];df=frequency[word]
                score+=math.log(1+(n-df+.5)/(df+.5))*tf*2.2/(tf+1.2*(.25+.75*total/max(avg,1)))
            labels=[raw.get('name_cn',''),raw.get('name',''),raw.get('id',''),*raw.get('aliases',[]),*raw.get('keywords',[])]
            if normalized and any(normalized==str(s).lower() for s in labels): score+=15
            if normalized and any(len(str(s))>=2 and str(s).lower() in normalized for s in labels): score+=8
            if normalized in raw.get('initials',[]): score+=15
            if score>0: scored.append({**d,'score':round(1-math.exp(-score/8),4)})
        scored.sort(key=lambda x:(-x['score'],x['id']))
        top=scored[0]['score'] if scored else 0
        status='found' if top>=self.config['found_threshold'] else 'partial' if top>=self.config['partial_threshold'] else 'gap'
        return {'items':scored[:limit], 'top_score':top, 'status':status, 'source':'local_bm25_alias_initials'}


def kb_search(query, **filters):
    return SearchEngine().search(query,**filters)
