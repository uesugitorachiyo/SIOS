import hashlib
import unittest
from aios.evidence_search import SearchBudgetExceeded, search_evidence

class EvidenceSearchTests(unittest.TestCase):
    def setUp(self):
        self.reads=[]
        self.docs={
          'Brain/brain/goals/plan.md': {'path':'Brain/brain/goals/plan.md','revision':'b2','text':'---\nstatus: approved\nreview_due: 2000-01-01\n---\n# Plan\nOriginal decision.'},
          'Memory/notes/example.md': {'path':'Memory/notes/example.md','revision':'m1','text':'---\ntype: note\nrecorded_at: 2026-01-02\n---\n# History\nOriginal note.'},
          'Missions/example/mission.md': {'path':'Missions/example/mission.md','revision':'p1','text':'---\nstatus: in_progress\nlast_touched: 2026-01-02\n---\n# Project\nOriginal mission.'}}
    def call(self, op, params):
        if op=='search':
            if params['store']=='brain':return [{'path':'Brain/brain/goals/plan.md','revision':'0'*64,'excerpt':'not authoritative'}]
            if params['store']=='missions':return [{'path':'Missions/example/mission.md','revision':hashlib.sha256(self.docs['Missions/example/mission.md']['text'].encode()).hexdigest()}]
            if params['store']=='memory':return []
        if op=='memory_index_search':return {'results':[{'read_path':'Memory/notes/example.md','content':'not authoritative'}],'has_more':False}
        if op=='read':
            self.reads.append(params['relative']);data=dict(self.docs[params['relative']]);data['revision']=hashlib.sha256(data['text'].encode()).hexdigest();return data
        raise AssertionError(op)
    def test_full_reads_and_drift(self):
        out=search_evidence(self.call,'plan')
        self.assertEqual(len(out['results']),3)
        rows={r['store']:r for r in out['results']}
        self.assertIn('Original decision.',rows['brain']['text']);self.assertEqual(rows['brain']['revision'],hashlib.sha256(rows['brain']['text'].encode()).hexdigest())
        self.assertTrue(rows['brain']['changed_since_search']);self.assertTrue(rows['brain']['review_overdue'])
        self.assertIsNone(rows['memory']['changed_since_search']);self.assertEqual(rows['memory']['recorded_at'],'2026-01-02')
        self.assertEqual(set(self.reads),set(self.docs));self.assertFalse(out['errors'])
    def test_failure_not_empty_and_bad_paths_not_read(self):
        def bad(op,p):
            if op=='memory_index_search':raise RuntimeError('private server error')
            if op=='search' and p['store']=='brain':return [{'path':'Brain/brain/../../secret.md'}]
            if op=='search':return []
            raise AssertionError('unexpected read')
        out=search_evidence(bad,'q');self.assertEqual(out['results'],[])
        self.assertEqual({e['store'] for e in out['errors']},{'brain','memory'})
        self.assertNotIn('private server error',str(out))
    def test_unreadable_document_and_limit(self):
        def bad(op,p):
            if op=='read':raise FileNotFoundError('sensitive path')
            return self.call(op,p)
        out=search_evidence(bad,'q',limit=2)
        self.assertEqual(out['results'],[]);self.assertTrue(out['errors']);self.assertTrue(out['truncated'])
        self.assertNotIn('sensitive path',str(out))
    def test_invalid_query_and_limit(self):
        for query in ('','   ',None):
            with self.assertRaises(ValueError):search_evidence(self.call,query)
        for limit in (0,51,True):
            with self.assertRaises(ValueError):search_evidence(self.call,'q',limit=limit)
    def test_unknown_metadata_is_not_invented(self):
        self.docs['Memory/notes/example.md']['text']='# Untyped note\nUnconfirmed content.'
        row=next(r for r in search_evidence(self.call,'q')['results'] if r['store']=='memory')
        self.assertIsNone(row['declared_status']);self.assertIsNone(row['review_overdue'])

    def test_direct_memory_search_supplements_the_index(self):
        path='Memory/notes/direct-only.md'; text='# Found directly'
        def call(op,p):
            if op=='search' and p['store']=='memory':return [{'path':path,'revision':hashlib.sha256(text.encode()).hexdigest()}]
            if op=='search':return []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':return {'path':path,'revision':hashlib.sha256(text.encode()).hexdigest(),'text':text}
        out=search_evidence(call,'unique')
        self.assertEqual([x['path'] for x in out['results']],[path])
        self.assertNotIn({'store':'memory','reason':'index_may_lag'},out['warnings'])

    def test_index_lag_is_disclosed_when_direct_search_fails(self):
        def call(op,p):
            if op=='search' and p['store']=='memory':raise RuntimeError('offline')
            return self.call(op,p)
        out=search_evidence(call,'q')
        self.assertIn({'store':'memory','reason':'index_may_lag'},out['warnings'])
        self.assertIn({'store':'memory','reason':'direct_search_unavailable'},out['errors'])

    def test_direct_memory_survives_a_malformed_index_response(self):
        path='Memory/notes/direct-only.md'; text='matching record'
        digest=hashlib.sha256(text.encode()).hexdigest()
        def call(op,p):
            if op=='memory_index_search':return {'results':[]}
            if op=='search':return [{'path':path,'revision':digest}] if p['store']=='memory' else []
            if op=='read':return {'path':path,'revision':digest,'text':text}
        out=search_evidence(call,'matching')
        self.assertEqual([r['path'] for r in out['results']],[path])
        self.assertIn({'store':'memory','reason':'search_unavailable'},out['errors'])

    def test_memory_candidates_can_be_paged_without_repeating_or_omitting(self):
        text='matching record'; digest=hashlib.sha256(text.encode()).hexdigest()
        paths=[f'Memory/notes/{n:03}.md' for n in range(35)]
        def call(op,p):
            if op=='search' and p['store']=='memory':return [{'path':v,'revision':digest} for v in paths]
            if op=='search':return []
            if op=='memory_index_search':return {'results':[{'read_path':v} for v in paths[:10]],'has_more':True}
            if op=='read':return {'path':p['relative'],'revision':digest,'text':text}
        first=search_evidence(call,'matching',limit=10)
        self.assertEqual(first['next_offset'],10)
        seen=list(first['results']); offset=first['next_offset']
        while offset is not None:
            page=search_evidence(call,'matching',limit=10,offset=offset,fingerprint=first['fingerprint'])
            seen.extend(page['results']); offset=page['next_offset']
        self.assertEqual(len(seen),35);self.assertEqual({r['path'] for r in seen},set(paths))

    def test_changed_candidate_set_refuses_stale_cursor(self):
        rows=[{'path':'Memory/notes/a.md'}]
        def call(op,p):
            if op=='search':return list(rows) if p['store']=='memory' else []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':return {'path':p['relative'],'revision':hashlib.sha256(b'x').hexdigest(),'text':'x'}
        first=search_evidence(call,'x',limit=1)
        rows.append({'path':'Memory/notes/b.md'})
        page=search_evidence(call,'x',limit=1,offset=1,fingerprint=first['fingerprint'])
        self.assertEqual(page['results'],[])
        self.assertIn({'store':'all','reason':'result_set_changed'},page['errors'])

    def test_budget_exhaustion_retries_the_unread_source_on_next_page(self):
        paths=[f'Memory/notes/{n}.md' for n in range(3)]
        count=[0]; digest=hashlib.sha256(b'x').hexdigest()
        def call(op,p):
            if op=='search':return [{'path':v} for v in paths] if p['store']=='memory' else []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':
                count[0]+=1
                if count[0]==2:raise SearchBudgetExceeded()
                return {'path':p['relative'],'revision':digest,'text':'x'}
        first=search_evidence(call,'x',limit=3)
        self.assertEqual([r['path'] for r in first['results']],paths[:1])
        self.assertEqual(first['next_offset'],1)
        next_page=search_evidence(call,'x',limit=3,offset=first['next_offset'],fingerprint=first['fingerprint'])
        self.assertEqual([r['path'] for r in next_page['results']],paths[1:])

    def test_index_lag_is_never_claimed_complete(self):
        out=search_evidence(self.call,'q')
        self.assertNotIn({'store':'memory','reason':'index_may_lag'},out['warnings'])
    def test_embedded_json_example_is_not_document_metadata(self):
        self.docs['Memory/notes/example.md']['text'] = '# Example\n```json\n{"type":"decision","evidence_status":"verified"}\n```\n'
        row=next(r for r in search_evidence(self.call,'q')['results'] if r['store']=='memory')
        self.assertEqual(row['classification'],'unknown');self.assertIsNone(row['evidence_status'])
    def test_valid_selective_memory_metadata(self):
        name='selective-'+'a'*64;path='Memory/notes/'+name+'.md'
        self.docs[path]={'path':path,'revision':'x','text':'---\ntitle: '+name+'\ntype: note\npermalink: aios/notes/'+name+'\n---\n\n<!-- aios-selective-memory-v1 -->\n# Selective Memory: test\n\n```json\n{"schema":"aios-selective-memory-v1","adoption_status":"proposal","recorded_at":"2026-01-02T12:00:00Z"}\n```\n'}
        def call(op,p):
            if op=='memory_index_search':return {'results':[{'read_path':path}],'has_more':False}
            return self.call(op,p)
        row=next(r for r in search_evidence(call,'q')['results'] if r['store']=='memory')
        self.assertEqual(row['classification'],'proposal');self.assertEqual(row['recorded_at'],'2026-01-02T12:00:00Z')

    def test_nested_index_response(self):
        def call(op,p):
            if op=='memory_index_search':return {'result':{'results':[{'read_path':'Memory/notes/example.md'}], 'has_more':False}}
            return self.call(op,p)
        self.assertIn('memory',[r['store'] for r in search_evidence(call,'q')['results']])

    def test_limit_bounds_invalid_hits(self):
        def call(op,p):
            if op=='search' and p['store']=='brain':return [{'path':'../invalid.md'}]*25
            if op=='memory_index_search':return {'results':[],'has_more':False}
            return []
        out=search_evidence(call,'q',limit=1)
        self.assertEqual(len(out['errors']),1);self.assertTrue(out['truncated'])

    def test_invalid_review_date_is_unknown(self):
        self.docs['Brain/brain/goals/plan.md']['text']='---\nreview_due: 2099-01-01nonsense\n---\nExample'
        row=next(r for r in search_evidence(self.call,'q')['results'] if r['store']=='brain')
        self.assertIsNone(row['review_overdue'])

    def test_spoofed_metadata_is_not_trusted(self):
        self.docs['Memory/notes/example.md']['text']='# Quoted example\n<!-- aios-selective-memory-v1 -->\n# Selective Memory: example\n\n```json\n{"schema":"aios-selective-memory-v1","type":"decision","evidence_status":"verified"}\n```\n'
        row=next(r for r in search_evidence(self.call,'q')['results'] if r['store']=='memory')
        self.assertEqual(row['classification'],'unknown');self.assertIsNone(row['evidence_status'])

    def test_fair_cross_store_results(self):
        def call(op,p):
            if op=='search' and p['store']=='brain':return [{'path':f'Brain/brain/goals/{i}.md'} for i in range(10)]
            if op=='search' and p['store']=='missions':return [{'path':'Missions/example/mission.md'}]
            if op=='memory_index_search':return {'results':[{'read_path':'Memory/notes/example.md'}], 'has_more':False}
            if op=='read':return {'path':p['relative'],'revision':hashlib.sha256(b'# Original').hexdigest(),'text':'# Original'}
        result=search_evidence(call,'x',limit=3)
        self.assertEqual({x['store'] for x in result['results']},{'brain','memory','missions'})
        self.assertTrue(result['truncated'])

    def test_stale_after_and_review_due_are_independent(self):
        self.docs['Brain/brain/goals/plan.md']['text']='---\nreview_due: 2099-01-01\nstale_after: 2000-01-01\n---\nExample'
        row=next(r for r in search_evidence(self.call,'q')['results'] if r['store']=='brain')
        self.assertFalse(row['review_overdue']);self.assertTrue(row['stale'])

    def test_real_brain_decision_fields(self):
        self.docs['Brain/brain/goals/plan.md']['text']='---\ntype: Decision\ndecision_date: 2026-08-03\nverified:\n  - by: human:owner\n    at: 2026-08-03T13:59:09-07:00\nreview_due: 2000-01-01\n---\nDecision.'
        row=next(r for r in search_evidence(self.call,'q')['results'] if r['store']=='brain')
        self.assertEqual(row['classification'],'decision');self.assertEqual(row['decision_date'],'2026-08-03')
        self.assertEqual(row['verified_at'],'2026-08-03T13:59:09-07:00');self.assertTrue(row['review_overdue'])

    def test_mission_catalog_fallback_is_labeled(self):
        def call(op,p):
            if op=='search' and p['store']=='missions':raise RuntimeError('oversized history')
            if op=='catalog':return {'store':'missions','paths':['Missions/example/mission.md']}
            return self.call(op,p)
        result=search_evidence(call,'project')
        self.assertIn('missions',[r['store'] for r in result['results']])
        self.assertIn({'store':'missions','reason':'mission_catalog_partial'},result['warnings'])
        self.assertIn({'store':'missions','reason':'search_unavailable'},result['errors'])

    def test_crlf_metadata(self):
        self.docs['Brain/brain/goals/plan.md']['text']='---\r\ntype: Decision\r\ndecision_date: 2026-08-03\r\nreview_due: 2000-01-01\r\n---\r\nExample\r\n'
        row=next(r for r in search_evidence(self.call,'q')['results'] if r['store']=='brain')
        self.assertEqual(row['classification'],'decision');self.assertEqual(row['decision_date'],'2026-08-03');self.assertTrue(row['review_overdue'])
        self.assertIn('\r\n',row['text'])

    def test_hash_mismatch_is_refused(self):
        def call(op,p):
            value=self.call(op,p)
            if op=='read' and p['relative']=='Brain/brain/goals/plan.md':value['revision']='f'*64
            return value
        out=search_evidence(call,'q')
        self.assertNotIn('brain',[r['store'] for r in out['results']])
        self.assertIn({'store':'brain','reason':'source_integrity_mismatch','path':'Brain/brain/goals/plan.md'},out['errors'])

    def test_mixed_question_extracts_keywords(self):
        text='# Blueprint sales direction';path='Brain/brain/goals/plan.md';digest=hashlib.sha256(text.encode()).hexdigest()
        def call(op,p):
            if op=='search' and p['store']=='brain':return [{'path':path,'revision':digest}] if p['query']=='Blueprint' else []
            if op=='search':return []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':return {'path':path,'revision':digest,'text':text}
            raise AssertionError(op)
        q='Blueprint'+chr(0x306e)+chr(0x8ca9)+chr(0x58f2)+chr(0x65b9)+chr(0x91dd)+'?'
        out=search_evidence(call,q)
        self.assertEqual(len(out['results']),1);self.assertIn('Blueprint',out['terms'])

    def test_hyphenated_project_name(self):
        text='# mission-control decision';path='Brain/brain/goals/plan.md';digest=hashlib.sha256(text.encode()).hexdigest()
        def call(op,p):
            if op=='search' and p['store']=='brain':return [{'path':path,'revision':digest}] if p['query']=='Mission-Control' else []
            if op=='search':return []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':return {'path':path,'revision':digest,'text':text}
            raise AssertionError(op)
        out=search_evidence(call,'Mission Control '+chr(0x7406)+chr(0x7531))
        self.assertEqual(len(out['results']),1);self.assertIn('Mission-Control',out['terms'])

    def test_partial_variant_failure_keeps_results(self):
        text='# Blueprint direction';path='Brain/brain/goals/plan.md';digest=hashlib.sha256(text.encode()).hexdigest()
        q='Blueprint'+chr(0x306e)+chr(0x7406)+chr(0x7531)
        def call(op,p):
            if op=='search' and p['store']=='brain':
                if p['query']==q:return [{'path':path,'revision':digest}]
                raise RuntimeError('later variant failed')
            if op=='search':return []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':return {'path':path,'revision':digest,'text':text}
        out=search_evidence(call,q)
        self.assertEqual(len(out['results']),1);self.assertIn({'store':'brain','reason':'search_unavailable'},out['errors'])

    def test_selective_verified_date_is_not_document_type(self):
        name='selective-'+'b'*64;path='Memory/notes/'+name+'.md'
        text='---\ntitle: '+name+'\ntype: note\npermalink: aios/notes/'+name+'\n---\n\n<!-- aios-selective-memory-v1 -->\n# Selective Memory: test\n\n```json\n{"schema":"aios-selective-memory-v1","evidence_status":"verified","verified_at":"2026-01-02T12:00:00Z"}\n```\n'
        self.docs[path]={'path':path,'text':text}
        def call(op,p):
            if op=='memory_index_search':return {'results':[{'read_path':path}],'has_more':False}
            return self.call(op,p)
        row=next(r for r in search_evidence(call,'q')['results'] if r['store']=='memory')
        self.assertEqual(row['classification'],'unknown');self.assertEqual(row['evidence_status'],'verified');self.assertEqual(row['verified_at'],'2026-01-02T12:00:00Z')

    def test_english_question_keeps_final_subject(self):
        text='# Blueprint overview';path='Brain/brain/goals/plan.md';digest=hashlib.sha256(text.encode()).hexdigest()
        def call(op,p):
            if op=='search' and p['store']=='brain':return [{'path':path,'revision':digest}] if p['query']=='Blueprint' else []
            if op=='search':return []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':return {'path':path,'revision':digest,'text':text}
        out=search_evidence(call,'What is the sales strategy for Blueprint?')
        self.assertEqual(len(out['results']),1);self.assertIn('Blueprint',out['terms']);self.assertLessEqual(len(out['terms']),8)

    def test_english_question_keeps_middle_subject(self):
        text='# Blueprint sales strategy';path='Brain/brain/goals/plan.md';digest=hashlib.sha256(text.encode()).hexdigest()
        def call(op,p):
            if op=='search' and p['store']=='brain':return [{'path':path,'revision':digest}] if p['query']=='Blueprint' else []
            if op=='search':return []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            if op=='read':return {'path':path,'revision':digest,'text':text}
        out=search_evidence(call,'Can you explain the Blueprint sales strategy for me?')
        self.assertEqual(len(out['results']),1);self.assertIn('Blueprint',out['terms']);self.assertLessEqual(len(out['terms']),8)
