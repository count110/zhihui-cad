import json
import shutil
import subprocess
import sys
from zhihui.kb.search import SearchEngine,KB_ROOT
from zhihui.kb.gaps import GapStore
from zhihui.kb.capability import check_capability


def test_search_alias_initials_and_gap():
 engine=SearchEngine()
 assert engine.search('菜市场污水',section='process_templates')['items'][0]['id']=='market'
 assert engine.search('jlt',section='process_templates')['items'][0]['id']=='distillation'
 assert engine.search('not_existing_qzyx_8849')['status']=='gap'
 assert engine.search('冷',section='symbols')['status'] in {'partial','gap'}
 assert len(engine.documents('process_templates'))==8


def test_capability_mapping_and_rotation():
 assert check_capability('设计单位写学院')['tool']=='edit_title_block'
 assert check_capability('把图纸整体旋转90度')['supported'] is False


def test_gap_close_loop_updates_alias_and_creates_original_prompt_regression(tmp_path):
 kb=tmp_path/'kb';shutil.copytree(KB_ROOT,kb)
 store=GapStore(tmp_path/'db.sqlite');gid=store.record('task','隔油设备',{'prompt':'画隔油设备处理餐饮污水'},'ask_user')
 case=store.resolve(gid,'隔油设备','农贸市场',kb_root=kb,case_dir=tmp_path/'cases')
 assert store.list()[0]['status']=='resolved'
 assert json.loads(case.read_text(encoding='utf-8'))['prompt']=='画隔油设备处理餐饮污水'
 assert SearchEngine(kb).search('隔油设备',section='process_templates')['items'][0]['id']=='market'
 result=subprocess.run([sys.executable,'-m','zhihui.kb.gaps','--db',str(store.path),'list'],capture_output=True,text=True,encoding='utf-8')
 assert result.returncode==0 and gid in result.stdout
