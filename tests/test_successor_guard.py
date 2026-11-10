import hashlib
import json
import unittest
from aios.successor_guard import inspect


def path(char):
    return 'Memory/notes/selective-' + char * 64 + '.md'


def note(p, supersedes=None, extra=''):
    text = ('<!-- aios-selective-memory-v1 -->\n```json\n' +
            json.dumps({'supersedes':supersedes, 'fixture':extra}) + '\n```\n')
    return {'path':p,'text':text,'revision':hashlib.sha256(text.encode()).hexdigest()}


def page(*paths, **changes):
    data = {'results':[{'read_path':p} for p in paths], 'total':len(paths),
            'current_page':1,'page_size':10,'total_is_exact':True,'has_more':False}
    data.update(changes)
    return data


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_edge_read_before_search_and_no_body_in_output(self):
        old, new = path('1'),path('2')
        docs = {old:note(old,extra='fictional-secret-body'),
                new:note(new,'selective-' + '1'*64,extra='fictional-secret-body')}
        calls = []
        async def read(p):
            calls.append(('read',p))
            return docs[p]
        async def search(q):
            calls.append(('search',q))
            return page(old,new)
        out = await inspect(old,read=read,search=search)
        self.assertEqual(out['status'],'unknown')
        self.assertEqual(out['reason'],'index_coverage_unproven')
        self.assertEqual(out['current_state'],'unknown')
        self.assertEqual(out['index_coverage'],'unknown')
        self.assertEqual(out['successors'],[{'path':new,'revision':docs[new]['revision']}])
        self.assertEqual(calls, [('read',old),('search','selective-'+'1'*64),('read',new),('read',old)])
        self.assertNotIn('fictional-secret-body',str(out))

    async def test_missing_old_never_searches_and_hides_errors(self):
        async def read(_):
            raise RuntimeError('fictional-private-token')
        async def search(_):
            self.fail('must read predecessor before search')
        out = await inspect(path('1'),read=read,search=search)
        self.assertEqual(out['reason'],'predecessor_unavailable')
        self.assertNotIn('fictional-private-token',str(out))

    async def test_paging_and_approximate_count_are_unknown_even_with_edge(self):
        old,new=path('1'),path('2')
        docs={old:note(old),new:note(new,'selective-'+'1'*64)}
        async def read(p): return docs[p]
        for flags in ({'has_more':True,'total':11}, {'total_is_exact':False}, {'total':3}):
            async def search(_,f=flags): return page(old,new,**f)
            out=await inspect(old,read=read,search=search)
            self.assertEqual((out['status'],out['reason']),('unknown','incomplete_search'))
            self.assertEqual(out['successors'][0]['path'],new)
            self.assertEqual(out['current_state'],'unknown')

    async def test_multiple_edges_are_ambiguous(self):
        old,a,b=path('1'),path('2'),path('3')
        docs={old:note(old),a:note(a,'selective-'+'1'*64),b:note(b,'selective-'+'1'*64)}
        async def read(p): return docs[p]
        async def search(_): return page(old,a,b)
        out=await inspect(old,read=read,search=search)
        self.assertEqual((out['status'],out['reason']),('unknown','multiple_successors'))
        self.assertEqual(len(out['successors']),2)

    async def test_corrupt_candidate_and_malicious_path_fail_closed(self):
        old,new=path('1'),path('2')
        docs={old:note(old),new:dict(note(new,'selective-'+'1'*64),revision='0'*64)}
        async def read(p): return docs[p]
        async def search(_): return page(old,new,'Memory/notes/../../secret.md')
        out=await inspect(old,read=read,search=search)
        self.assertEqual((out['status'],out['reason']),('unknown','incomplete_search'))
        self.assertEqual(out['successors'],[])

    async def test_duplicate_json_fields_rejected(self):
        old,new=path('1'),path('2')
        text=('<!-- aios-selective-memory-v1 -->\n```json\n'
              '{"supersedes":null,"supersedes":"selective-'+'1'*64+'"}\n```\n')
        bad={'path':new,'text':text,'revision':hashlib.sha256(text.encode()).hexdigest()}
        async def read(p): return note(old) if p==old else bad
        async def search(_): return page(old,new)
        out=await inspect(old,read=read,search=search)
        self.assertEqual((out['status'],out['reason']),('unknown','incomplete_search'))

    async def test_changed_predecessor_after_search_is_unknown(self):
        old=path('1')
        versions=[note(old,extra='first'),note(old,extra='second')]
        async def read(_): return versions.pop(0)
        async def search(_): return page(old)
        out=await inspect(old,read=read,search=search)
        self.assertEqual((out['status'],out['reason']),('unknown','predecessor_changed'))

    async def test_invalid_source_shape_and_search_exception_are_sanitized(self):
        old=path('1')
        async def wrong(_): return note(path('2'))
        async def never(_): self.fail('wrong source cannot be searched')
        self.assertEqual((await inspect(old,read=wrong,search=never))['reason'],'predecessor_unavailable')
        async def read(_): return note(old)
        async def bad(_): raise RuntimeError('fixture-private-token')
        out=await inspect(old,read=read,search=bad)
        self.assertEqual(out['reason'],'search_or_read_unavailable')
        self.assertNotIn('fixture-private-token',str(out))


if __name__ == '__main__':
    unittest.main(verbosity=2)
