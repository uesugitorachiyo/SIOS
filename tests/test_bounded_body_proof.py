import threading
import unittest
from aios.bounded_body_proof import BodyProof
from tests.test_body_readiness import PATH,REV,TEXT,HIT,response

class Bounded(unittest.TestCase):
 def test_valid_and_failed_health(self):
  def call(op,p):
   if op=='health':return {'ok':True,'database':'ok'}
   if op=='memory_index_search':return response([HIT])
   if op=='read':return {'path':PATH,'revision':REV,'text':TEXT}
   raise AssertionError(op)
  self.assertTrue(BodyProof(call).check(PATH,REV,'fictionalbodytoken')['ready'])
  self.assertEqual(BodyProof(lambda o,p:{'ok':False}).check(PATH,REV,'fictionalbodytoken')['reason'],'health_unconfirmed')
 def test_timeout_retains_slot_and_does_not_issue_later_rpc(self):
  entered=threading.Event();release=threading.Event();calls=[]
  def call(op,p):
   calls.append(op)
   if op=='health': entered.set();release.wait(2);return {'ok':True,'database':'ok'}
   raise AssertionError('unexpected post-deadline RPC')
  proof=BodyProof(call,timeout=.05)
  try:
   result=proof.check(PATH,REV,'fictionalbodytoken')
   self.assertTrue(entered.is_set());self.assertEqual(result['reason'],'deadline_exceeded')
   self.assertEqual(proof.check(PATH,REV,'fictionalbodytoken')['reason'],'inflight')
  finally:release.set()
  self.assertEqual(calls,['health'])

if __name__=='__main__':unittest.main()
