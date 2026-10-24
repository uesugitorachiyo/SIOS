"""Existing synthetic owner-command regressions; fake transport only."""

import copy
import datetime as dt
import hashlib
import unittest

from aios.owner_completion import approve

class OwnerCommandTests(unittest.TestCase):
 def setUp(self):
  self.now=dt.datetime.now(dt.timezone.utc);self.content='Synthetic proposed completion\n';self.expiry=(self.now+dt.timedelta(minutes=2)).isoformat()
  self.first={'mission':'synthetic-owner-command','verification_id':'synthetic-verification','control_digest':'a'*64,'expected_revision':'b'*64,'current_revision':'b'*64,'desired_mission_sha256':hashlib.sha256(self.content.encode()).hexdigest(),'review_digest':'c'*64,'request_expires_at':(self.now+dt.timedelta(hours=1)).isoformat(),'review_expires_at':(self.now+dt.timedelta(minutes=5)).isoformat(),'owner_approval_required':True,'eligible':True,'independent':True,'qa':{'actor':'synthetic-qa','status':'PASS'},'security':{'actor':'synthetic-security','status':'PASS'},'diff':'-old\n+synthetic\n'}
  self.second=copy.deepcopy(self.first);self.calls=[];self.output=[]
  owner=self
  class Transport:
   def call(self,operation,params):
    owner.calls.append((operation,copy.deepcopy(params)))
    if operation=='completion_review':return copy.deepcopy(owner.first if len(owner.calls)==1 else owner.second)
    if operation=='completion_approve':return {'approved_scope':'development_completion','enforcement_active':False}
    raise AssertionError('Unexpected operation')
  self.transport=Transport()
 def run_command(self,confirm=None,interactive=True,clock=None):
  return approve(self.transport,'synthetic-verification',self.content,self.expiry,interactive=interactive,confirm=confirm or (lambda _: 'APPROVE '+self.first['mission']+' '+self.first['control_digest']),output=self.output.append,clock=clock or (lambda:self.now))
 def assert_no_approval(self):self.assertFalse(any(c[0]=='completion_approve' for c in self.calls))
 def test_exact_manual_confirmation_and_payload(self):
  self.assertEqual(self.run_command()['approved_scope'],'development_completion')
  self.assertEqual([c[0] for c in self.calls],['completion_review','completion_review','completion_approve'])
  self.assertEqual(self.calls[-1][1],{'verification_id':'synthetic-verification','control_digest':'a'*64,'expires_at':self.expiry})
  rendered='\n'.join(self.output)
  for value in ['synthetic-owner-command','a'*64,'b'*64,'synthetic-qa','synthetic-security','-old',self.expiry]:self.assertIn(value,rendered)
 def test_handoff_pins_reject_changed_review_before_approval(self):
  pins={'expected_mission':self.first['mission'],'expected_control_digest':self.first['control_digest'],
        'expected_revision':self.first['expected_revision']}
  self.assertEqual(approve(self.transport,'synthetic-verification',self.content,self.expiry,
       interactive=True,confirm=lambda _:'APPROVE '+self.first['mission']+' '+self.first['control_digest'],
       output=self.output.append,clock=lambda:self.now,**pins)['approved_scope'],'development_completion')
  self.calls=[];self.first['expected_revision']='d'*64
  with self.assertRaises(ValueError):approve(self.transport,'synthetic-verification',self.content,self.expiry,
       interactive=True,confirm=lambda _: 'APPROVE '+pins['expected_mission']+' '+pins['expected_control_digest'],
       output=self.output.append,clock=lambda:self.now,**pins)
  self.assert_no_approval()
 def test_changed_control_digest_and_revision_rejected(self):
  for key in ['control_digest','current_revision','review_digest']:
   with self.subTest(key=key):
    self.calls=[];self.second=copy.deepcopy(self.first);self.second[key]='d'*64
    with self.assertRaises(ValueError):self.run_command()
    self.assert_no_approval()
 def test_expired_review_and_requested_expiry(self):
  self.first['review_expires_at']=(self.now-dt.timedelta(seconds=1)).isoformat()
  with self.assertRaises(ValueError):self.run_command()
  self.assert_no_approval();self.calls=[];self.first['review_expires_at']=(self.now+dt.timedelta(minutes=5)).isoformat();self.expiry=(self.now-dt.timedelta(seconds=1)).isoformat()
  with self.assertRaises(ValueError):self.run_command()
  self.assert_no_approval()
 def test_missing_independent_reports(self):
  for key in ['qa','security']:
   with self.subTest(key=key):
    self.calls=[];r=copy.deepcopy(self.first);r[key]['status']='INCOMPLETE';self.first=r
    with self.assertRaises(ValueError):self.run_command()
    self.assert_no_approval();self.first[key]['status']='PASS'
  self.calls=[];self.first['independent']=False
  with self.assertRaises(ValueError):self.run_command()
  self.assert_no_approval()
 def test_cancel_and_noninteractive(self):
  with self.assertRaisesRegex(ValueError,'cancelled'):self.run_command(confirm=lambda _:'cancel')
  self.assertEqual(len(self.calls),1);self.assert_no_approval();self.calls=[]
  with self.assertRaises(ValueError):self.run_command(interactive=False)
  self.assertEqual(self.calls,[])
 def test_expiry_while_owner_reads(self):
  def confirm(_):self.now+=dt.timedelta(minutes=6);return 'APPROVE '+self.first['mission']+' '+self.first['control_digest']
  with self.assertRaises(ValueError):self.run_command(confirm=confirm)
  self.assert_no_approval()


if __name__ == "__main__":
    unittest.main()
