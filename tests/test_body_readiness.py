import hashlib
import importlib.util
from pathlib import Path
import unittest
from aios.body_readiness import observe

STEM='selective-'+'a'*64
PATH='Memory/notes/'+STEM+'.md'
TEXT='---\ntitle: '+STEM+'\n---\n<!-- aios-selective-memory-v1 -->\nfictionalbodytoken is here\n'
REV=hashlib.sha256(TEXT.encode()).hexdigest()
HIT={'read_path':PATH,'file_path':'notes/'+STEM+'.md','title':STEM,'type':'entity'}
def response(hits):
 return {'results':hits,'total':len(hits),'total_is_exact':True,'has_more':False,'current_page':1,'page_size':10}
def run(*,marker='fictionalbodytoken',hits=None,source=None):
 return observe(PATH,REV,marker,search=lambda q:response([HIT] if hits is None else hits),read=lambda p:{'path':p,'revision':REV,'text':TEXT} if source is None else source)

class Readiness(unittest.TestCase):
 def test_stem_readiness_does_not_prove_body_query(self):
  module=Path(__file__).resolve().parents[1]/'src/aios/client_ready_proof.py'
  spec=importlib.util.spec_from_file_location('ready_proof_fixture',module)
  ready_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(ready_module)
  def rpc(op,params):
   if op=='health':return {'ok':True,'database':'ok'}
   if op=='memory_index_status':return {'total_files':1,'observed_files':[{'path':'notes/'+STEM+'.md','checksum':REV}]}
   if op=='memory_index_search':
    hits=[HIT] if params['query']==STEM else []
    return {'results':hits,'total':len(hits),'total_is_exact':True,'has_more':False,'current_page':1,'page_size':10}
   if op=='read':return {'path':PATH,'revision':REV,'text':TEXT}
   raise AssertionError(op)
  result=ready_module.CaptureReadiness(rpc).observe(PATH,REV)
  self.assertTrue(result['ready'])
  self.assertFalse(observe(PATH,REV,'fictionalbodytoken',search=lambda q:rpc('memory_index_search',{'query':q}),read=lambda p:rpc('read',{'relative':p}))['ready'])
 def test_found_in_source_and_search(self): self.assertTrue(run()['ready'])
 def test_missing_fts_or_marker(self):
  self.assertFalse(run(hits=[])['ready'])
  self.assertFalse(run(marker='absentbodytoken')['ready'])
 def test_stale_or_tampered_source(self):
  self.assertFalse(run(source={'path':PATH,'revision':REV,'text':'tampered'})['ready'])
  self.assertFalse(run(source={'path':PATH,'revision':'0'*64,'text':TEXT})['ready'])
 def test_wrong_or_duplicate_search_hits(self):
  self.assertFalse(run(hits=[{**HIT,'file_path':'notes/other.md'}])['ready'])
  self.assertFalse(run(hits=[HIT,HIT])['ready'])
 def test_marker_not_unique_to_stem_or_invalid(self):
  self.assertFalse(run(marker='aaaa')['ready'])
  self.assertFalse(run(marker='bad text')['ready'])
 def test_japanese_marker_requires_search_and_verified_source(self):
  text=TEXT+'架空検索目印\n'
  rev=hashlib.sha256(text.encode()).hexdigest()
  observed=[]
  def search(query):
   observed.append(query)
   return response([HIT])
  source=lambda p:{'path':p,'revision':rev,'text':text}
  self.assertTrue(observe(PATH,rev,'架空検索目印',search=search,read=source)['ready'])
  self.assertEqual(observed,['架空検索目印'])
  self.assertEqual(observe(PATH,rev,'架空検索目印',search=lambda q:response([]),read=source)['reason'],'search_unconfirmed')
  self.assertEqual(observe(PATH,rev,'存在しない',search=search,read=source)['reason'],'marker_not_in_source')
 def test_marker_validation_bounds_and_syntax(self):
  for marker in ('x','abc','架','架 空','架空"検索','架空\n検索','_架空','架空' * 51):
   with self.subTest(marker=marker):
    result=observe(PATH,REV,marker,search=lambda q:self.fail('invalid query executed'),read=lambda p:None)
    self.assertEqual(result['reason'],'invalid_marker')
 def test_backend_failure_never_ready(self):
  def fail(q):raise RuntimeError('secret should not leak')
  r=observe(PATH,REV,'fictionalbodytoken',search=fail,read=lambda _:None)
  self.assertEqual(r['reason'],'observation_unavailable')
  self.assertNotIn('secret',str(r))
 def test_malformed_pagination_never_ready(self):
  for changes in ({'total':0},{'has_more':True},{'total_is_exact':False},{'current_page':2},{'page_size':0}):
   with self.subTest(changes=changes):
    value=response([HIT]);value.update(changes)
    self.assertFalse(observe(PATH,REV,'fictionalbodytoken',search=lambda q:value,read=lambda p:{'path':p,'revision':REV,'text':TEXT})['ready'])

if __name__=='__main__':unittest.main()
