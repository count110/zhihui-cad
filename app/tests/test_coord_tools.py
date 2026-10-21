import asyncio
from mcp import Client
from zhihui.tools.core import ToolService
from zhihui.agent.extended_guards import ExtendedGuard


def drawing():
 s=ToolService();did=s.create_drawing()['drawing_id'];s.place_equipment(did,[{'symbol_id':'tank','tag':'V-101'},{'symbol_id':'pump','tag':'P-101'}]);s.connect_ports(did,[{'from_port':'V-101.out','to_port':'P-101.in'}]);s.auto_layout(did);return s,did


def test_title_block_user_facts_and_find():
 s,did=drawing()
 assert s.edit_title_block(did,{'unit_name':'广东石油化工学院'})['error_code']=='E_UNSOURCED_VALUE'
 assert s.edit_title_block(did,{'unit_name':'广东石油化工学院'},{'text':'设计单位写广东石油化工学院'})['ok']
 assert s.find_entities(did,kind='title_block_field',query='广东')['candidates'][0]['ref']=='unit_name'


def test_named_region_tracks_equipment_and_lists_errors():
 s,did=drawing();r=s.add_hatch(did,target='V-101',region='fill');assert r['ok'],r
 assert not s.store.load(did).hatches[0].disassociated
 r=s.add_hatch(did,target='V-101',region='absent');assert r['error_code']=='E_REGION_UNKNOWN' and 'fill' in r['available_regions']


def test_annotation_edit_delete_confirmation():
 s,did=drawing();ref=s.add_annotation(did,'V-101','原料')['annotation_ref']
 assert s.edit_annotation(did,ref,'产品',{'text':'产品'})['ok']
 assert s.delete_annotation(did,ref)['error_code']=='E_TARGET_UNCONFIRMED'
 assert ExtendedGuard().check('delete_annotation',{'annotation_ref':ref,'confirmed':True},[{'role':'user','content':'删标注'}])['error_code']=='E_TARGET_UNCONFIRMED'
 assert s.delete_annotation(did,ref,True)['ok']


def test_extended_mcp_preserves_legacy_catalog():
 from zhihui.mcp_server.server import mcp,extended_mcp,ALL_TOOL_NAMES
 async def check():
  async with Client(mcp) as c: assert len((await c.list_tools()).tools)==20
  async with Client(extended_mcp) as c: assert {t.name for t in (await c.list_tools()).tools}==set(ALL_TOOL_NAMES)
 asyncio.run(check())
