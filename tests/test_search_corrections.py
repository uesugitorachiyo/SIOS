import hashlib
import json
import unittest
from aios.search_resolver import resolve_search_verified

A = 'selective-' + 'a' * 64
B = 'selective-' + 'b' * 64
C = 'selective-' + 'c' * 64

def path(stem): return 'Memory/notes/' + stem + '.md'
def source(stem, predecessor=None):
    data = {'schema':'aios-selective-memory-v1','meaning_id':'fiction',
            'claim':'fictional claim','conditions':[], 'evidence_status':'verified',
            'supersedes':predecessor}
    text = '---\ntitle: fiction\n---\n<!-- aios-selective-memory-v1 -->\n```json\n' + json.dumps(data) + '\n```\n'
    return {'path':path(stem), 'text':text, 'revision':hashlib.sha256(text.encode()).hexdigest()}

class CorrectionLinks(unittest.TestCase):
    def test_both_indexed_in_either_order(self):
        docs = {path(A):source(A),path(B):source(B,A),path(C):source(C,B)}
        for order in ([A,B,C],[C,B,A]):
            with self.subTest(order=order):
                result = resolve_search_verified('fiction', lambda q:{'results':[{'read_path':path(s)} for s in order]}, docs.__getitem__)
                self.assertEqual({(x['successor_path'],x['predecessor_path']) for x in result['declared_corrections']}, {(path(B),path(A)),(path(C),path(B))})
                self.assertEqual(result['current_state'],'unknown')
                self.assertEqual(result['index_coverage'],'unknown')
    def test_unverified_predecessor_no_relationship(self):
        docs={path(A):source(A),path(B):source(B,A)}
        docs[path(A)]['revision']='0'*64
        result=resolve_search_verified('fiction',lambda q:{'results':[{'read_path':path(A)},{'read_path':path(B)}]},docs.__getitem__)
        self.assertEqual(result['declared_corrections'],[])
    def test_snippet_and_self_reference_do_not_establish_relation(self):
        docs={path(A):source(A,A),path(B):source(B)}
        hits=[{'read_path':path(A),'content_snippet':B},{'read_path':path(B),'content_snippet':A}]
        result=resolve_search_verified('fiction',lambda q:{'results':hits},docs.__getitem__)
        self.assertEqual(result['declared_corrections'],[])
    def test_budget_does_not_infer_unread_relationship(self):
        docs={path(A):source(A),path(B):source(B,A)}
        result=resolve_search_verified('fiction',lambda q:{'results':[{'read_path':path(A)},{'read_path':path(B)}]},docs.__getitem__,max_reads=1)
        self.assertEqual(result['declared_corrections'],[])
        self.assertEqual(result['read_budget']['read_attempts'],1)
    def test_dereferenced_predecessor_and_duplicate_hits(self):
        docs={path(A):source(A),path(B):source(B,A)}
        for hits in ([B], [A,B,B]):
            with self.subTest(hits=hits):
                result=resolve_search_verified('fiction',lambda q:{'results':[{'read_path':path(s)} for s in hits]},docs.__getitem__)
                self.assertEqual([(x['successor_path'],x['predecessor_path']) for x in result['declared_corrections']],[(path(B),path(A))])

if __name__ == '__main__': unittest.main()
