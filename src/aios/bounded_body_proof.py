"""Deadline-bounded client wrapper; outstanding RPC retains one worker slot."""
from __future__ import annotations
import threading
import time
from .body_readiness import observe

class BodyProof:
 def __init__(self,call,timeout=10):
  if not callable(call) or isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not 0<timeout<=15:
   raise ValueError('invalid_configuration')
  self.call=call;self.timeout=timeout;self.gate=threading.Lock()
 def check(self,path,revision,marker):
  base={'ready':False,'index_coverage':'unknown','atomic_snapshot':False,
        'scope':'one_body_literal_search_and_full_source_read'}
  if not self.gate.acquire(blocking=False):return {**base,'reason':'inflight'}
  deadline=time.monotonic()+self.timeout;done=threading.Event();state={}
  def request(op,params):
   if time.monotonic()>=deadline:raise TimeoutError()
   result=self.call(op,params)
   if time.monotonic()>=deadline:raise TimeoutError()
   return result
  def worker():
   try:
    health=request('health',{})
    if not isinstance(health,dict) or health.get('ok') is not True or health.get('database')!='ok':
     state['result']={**base,'reason':'health_unconfirmed'}
    else:
     state['result']=observe(path,revision,marker,
      search=lambda q:request('memory_index_search',{'query':q}),
      read=lambda p:request('read',{'relative':p}))
   except Exception:state['result']={**base,'reason':'observation_unavailable'}
   finally:self.gate.release();done.set()
  try:threading.Thread(target=worker,daemon=True,name='sios-body-proof').start()
  except Exception:
   self.gate.release();return {**base,'reason':'worker_unavailable'}
  if not done.wait(max(0,deadline-time.monotonic())):return {**base,'reason':'deadline_exceeded'}
  if time.monotonic()>=deadline:return {**base,'reason':'deadline_exceeded'}
  return state.get('result',{**base,'reason':'observation_unavailable'})
