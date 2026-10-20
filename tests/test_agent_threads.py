"""Isolated Agent Threads L0: authenticated actor, project ACL and durable receipts."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aios.agent_threads import NotEnabledError, load_project_acl
from aios.service import dispatch
from aios.store import ConflictError, NotFoundError, Store, ValidationError


GRANTS = {
    'mac': {'threads:create', 'threads:post', 'threads:read', 'threads:ack', 'threads:manage'},
    'windows': {'threads:post', 'threads:read', 'threads:ack'},
    'outsider': {'threads:post', 'threads:read'},
}
PROJECTS = {'mac': {'fixture'}, 'windows': {'fixture'}, 'outsider': {'other'}}


class AgentThreadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-threads-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'data'
        self.store = Store(self.root, thread_projects=PROJECTS, thread_principals=GRANTS)

    def call(self, operation, params, actor='mac', store=None, scopes=None):
        return dispatch(store or self.store, operation, params, actor=actor,
                        scopes=set(scopes if scopes is not None else GRANTS[actor]))

    def create(self, request_id='create-one'):
        return self.call('thread_create', {'project': 'fixture', 'purpose': 'Synthetic review',
                         'participants': ['windows'], 'request_id': request_id,
                         'max_messages': 12})

    def test_round_trip_receipts_acl_and_reopen(self):
        created = self.create()
        self.assertEqual(created, self.create())
        tid = created['result']['thread_id']
        self.assertEqual(self.call('thread_list', {'project': 'fixture'})['threads'][0]['thread_id'], tid)
        self.assertEqual(len(self.call('thread_list', {'project': 'fixture'}, actor='windows')['threads']), 1)
        with self.assertRaises(PermissionError):
            self.call('thread_list', {'project': 'fixture'}, actor='outsider')
        with self.assertRaises(NotFoundError):
            self.call('thread_read', {'thread_id': tid}, actor='outsider')
        with self.assertRaises(PermissionError):
            self.call('thread_create', {'project': 'fixture', 'purpose': 'Bad invitation',
                'participants': ['outsider'], 'request_id': 'bad-invite'})

        first_args = {'thread_id': tid, 'body': 'Please review fabricated behavior.',
                      'kind': 'question', 'request_id': 'post-one',
                      'target_participant_ids': ['windows']}
        first = self.call('thread_post', first_args)
        self.assertEqual(first, self.call('thread_post', first_args))
        with self.assertRaises(ConflictError):
            self.call('thread_post', dict(first_args, body='Changed request'))
        with self.assertRaises(PermissionError):
            self.call('thread_post', dict(first_args, request_id='bad-recipient',
                          target_participant_ids=['outsider']))
        visible = self.call('thread_read', {'thread_id': tid}, actor='windows')
        self.assertEqual(visible['messages'][0]['principal'], 'mac')
        self.assertEqual(visible['last_acknowledged_seq'], 0)
        fetched = self.call('thread_fetch', {'thread_id': tid, 'through_seq': 1,
                            'request_id': 'fetch-one'}, actor='windows')
        self.assertEqual(fetched['result']['last_fetched_seq'], 1)
        ack = self.call('thread_ack', {'thread_id': tid, 'through_seq': 1,
                                      'request_id': 'ack-one'}, actor='windows')
        self.assertEqual(ack['result']['last_acknowledged_seq'], 1)
        self.assertFalse(ack['result']['acted_on'])
        with self.assertRaises(ValidationError):
            self.call('thread_ack', {'thread_id': tid, 'through_seq': 4,
                                     'request_id': 'ack-future'}, actor='windows')

        reply = self.call('thread_post', {'thread_id': tid, 'body': 'Synthetic review passed.',
                          'kind': 'answer', 'request_id': 'post-two',
                          'parent_message_id': first['result']['message_id']}, actor='windows')
        self.assertEqual(reply['result']['seq'], 2)
        reopened = Store(self.root, thread_projects=PROJECTS, thread_principals=GRANTS)
        messages = self.call('thread_read', {'thread_id': tid}, store=reopened)['messages']
        self.assertEqual([item['seq'] for item in messages], [1, 2])
        self.assertEqual(messages[1]['parent_message_id'], first['result']['message_id'])
        paused = self.call('thread_close', {'thread_id': tid, 'action': 'pause',
                           'expected_revision': reply['result']['revision'],
                           'request_id': 'pause-one'})
        with self.assertRaises(ConflictError):
            self.call('thread_post', {'thread_id': tid, 'body': 'Blocked while paused',
                       'kind': 'status', 'request_id': 'post-paused'})
        with self.assertRaises(PermissionError):
            self.call('thread_close', {'thread_id': tid, 'action': 'resume',
                       'expected_revision': paused['result']['revision'],
                       'request_id': 'not-owner'}, actor='windows',
                       scopes=GRANTS['windows'] | {'threads:manage'})
        resumed = self.call('thread_close', {'thread_id': tid, 'action': 'resume',
                            'expected_revision': paused['result']['revision'],
                            'request_id': 'resume-one'})
        closed = self.call('thread_close', {'thread_id': tid, 'action': 'close',
                           'expected_revision': resumed['result']['revision'],
                           'request_id': 'close-one'})
        self.assertEqual(closed['result']['status'], 'closed')
        self.assertEqual(self.call('thread_read', {'thread_id': tid})['status'], 'closed')
        revoked = self.call('thread_revoke', {'thread_id': tid,
                            'participant': 'windows',
                            'expected_revision': closed['result']['revision'],
                            'request_id': 'revoke-one'})
        self.assertTrue(revoked['result']['revoked'])
        with self.assertRaises(NotFoundError):
            self.call('thread_read', {'thread_id': tid}, actor='windows')
        self.assertEqual(self.call('thread_list', {'project': 'fixture'},
                         actor='windows')['threads'], [])
        with self.assertRaises(NotFoundError):
            self.call('thread_ack', {'thread_id': tid, 'through_seq': 1,
                                     'request_id': 'revoked-ack'}, actor='windows')
        with self.assertRaises(NotFoundError):
            self.call('thread_ack', {'thread_id': tid, 'through_seq': 1,
                                     'request_id': 'ack-one'}, actor='windows')
        with self.assertRaises(NotFoundError):
            self.call('thread_fetch', {'thread_id': tid, 'through_seq': 1,
                                     'request_id': 'fetch-one'}, actor='windows')
        with self.assertRaises(NotFoundError):
            self.call('thread_post', {'thread_id': tid, 'body': 'Synthetic review passed.',
                       'kind': 'answer', 'request_id': 'post-two',
                       'parent_message_id': first['result']['message_id']}, actor='windows')

    def test_concurrent_posts_unique_sequence_and_cap(self):
        tid = self.create('create-concurrent')['result']['thread_id']
        def post(index):
            return self.call('thread_post', {'thread_id': tid, 'body': f'Fixture {index}',
                             'kind': 'status', 'request_id': f'parallel-{index}'},
                             actor='windows')['result']['seq']
        with ThreadPoolExecutor(max_workers=5) as pool:
            sequence = list(pool.map(post, range(10)))
        self.assertEqual(sorted(sequence), list(range(1, 11)))
        self.assertEqual(len(self.call('thread_read', {'thread_id': tid, 'limit': 20})['messages']), 10)
        post(10)
        post(11)
        with self.assertRaises(ConflictError):
            post(12)

    def test_fetched_cursor_is_durable_and_distinct_from_ack(self):
        tid = self.create('create-fetched')['result']['thread_id']
        empty = self.call('thread_read', {'thread_id': tid}, actor='windows')
        self.assertEqual(empty['last_fetched_seq'], 0)
        self.assertEqual(empty['last_acknowledged_seq'], 0)
        for seq in (1, 2):
            self.call('thread_post', {'thread_id': tid, 'body': f'Page {seq}',
                      'kind': 'status', 'request_id': f'fetched-post-{seq}'})
        first = self.call('thread_read', {'thread_id': tid, 'limit': 1}, actor='windows')
        self.assertEqual(first['messages'][-1]['seq'], 1)
        self.assertEqual(first['last_fetched_seq'], 0)
        self.assertEqual(first['last_acknowledged_seq'], 0)
        with self.assertRaises(PermissionError):
            self.call('thread_fetch', {'thread_id': tid, 'through_seq': 1,
                      'request_id': 'fetch-without-ack-scope'}, actor='windows',
                      scopes={'threads:read'})
        with self.assertRaises(PermissionError):
            self.call('thread_fetch', {'thread_id': tid, 'through_seq': 1,
                      'request_id': 'fetch-without-read-scope'}, actor='windows',
                      scopes={'threads:ack'})
        fetched_one = self.call('thread_fetch', {'thread_id': tid, 'through_seq': 1,
                                 'request_id': 'fetched-one'}, actor='windows')
        self.assertEqual(fetched_one, self.call('thread_fetch',
                         {'thread_id': tid, 'through_seq': 1,
                          'request_id': 'fetched-one'}, actor='windows'))
        self.assertEqual(fetched_one['result']['last_fetched_seq'], 1)
        self.assertEqual(fetched_one['result']['last_acknowledged_seq'], 0)
        self.assertNotIn('acted_on', fetched_one['result'])
        self.assertNotIn('replied_to', fetched_one['result'])
        no_page = self.call('thread_read', {'thread_id': tid, 'after_seq': 2}, actor='windows')
        self.assertEqual(no_page['last_fetched_seq'], 1)
        second = self.call('thread_read', {'thread_id': tid, 'after_seq': 1}, actor='windows')
        self.assertEqual(second['messages'][-1]['seq'], 2)
        self.assertEqual(second['last_fetched_seq'], 1)
        self.assertEqual(second['last_acknowledged_seq'], 0)
        with self.assertRaises(ValidationError):
            self.call('thread_fetch', {'thread_id': tid, 'through_seq': 3,
                      'request_id': 'fetched-uncommitted'}, actor='windows')
        self.call('thread_fetch', {'thread_id': tid, 'through_seq': 2,
                  'request_id': 'fetched-two'}, actor='windows')
        ack = self.call('thread_ack', {'thread_id': tid, 'through_seq': 1,
                        'request_id': 'fetched-ack'}, actor='windows')
        self.assertFalse(ack['result']['acted_on'])
        self.assertFalse(ack['result']['replied_to'])
        reopened = Store(self.root, thread_projects=PROJECTS, thread_principals=GRANTS)
        older = self.call('thread_read', {'thread_id': tid, 'limit': 1},
                          actor='windows', store=reopened)
        self.assertEqual(older['last_fetched_seq'], 2)
        self.assertEqual(older['last_acknowledged_seq'], 1)
        self.assertEqual(self.call('thread_fetch', {'thread_id': tid, 'through_seq': 1,
                         'request_id': 'fetched-older'}, actor='windows')['result']['last_fetched_seq'], 2)
        self.assertEqual(self.call('thread_list', {'project': 'fixture'},
                         actor='windows', store=reopened)['threads'][0]['unread'], 1)

    def test_existing_a6_participant_schema_migrates_without_inventing_fetches(self):
        root = Path(self.temp.name).resolve() / 'old-data'
        database = root / 'Database'
        database.mkdir(parents=True)
        with sqlite3.connect(database / 'state') as db:
            db.execute('''CREATE TABLE agent_thread_participants (
                thread_id TEXT NOT NULL, principal TEXT NOT NULL, role TEXT NOT NULL,
                last_ack INTEGER NOT NULL DEFAULT 0, revoked_at TEXT,
                PRIMARY KEY(thread_id, principal))''')
            db.execute('INSERT INTO agent_thread_participants VALUES(?,?,?,?,?)',
                       ('historical-thread', 'mac', 'owner', 2, None))
        Store(root, thread_projects=PROJECTS, thread_principals=GRANTS)
        with sqlite3.connect(database / 'state') as db:
            row = db.execute('''SELECT last_ack,last_fetched FROM agent_thread_participants
                                WHERE thread_id=? AND principal=?''',
                             ('historical-thread', 'mac')).fetchone()
        self.assertEqual(row, (2, 0))

    def test_feature_off_and_scope_checks(self):
        off = Store(Path(self.temp.name).resolve() / 'off')
        with self.assertRaises(NotEnabledError):
            self.call('thread_list', {'project': 'fixture'}, store=off)
        with self.assertRaises(PermissionError):
            self.call('thread_create', {'project': 'fixture', 'purpose': 'No scope',
                       'participants': [], 'request_id': 'not-scoped'},
                       scopes={'threads:read'})
        with self.assertRaises(PermissionError):
            self.call('thread_create', {'project': 'fixture', 'purpose': 'No read',
                       'participants': [], 'request_id': 'no-read'},
                       scopes={'threads:create'})

    def test_private_exact_acl(self):
        acl = Path(self.temp.name) / 'thread-acl.json'
        acl.write_text(json.dumps({'version': 1, 'projects': {'mac': ['fixture'],
                             'windows': ['fixture']}}))
        acl.chmod(0o600)
        result = load_project_acl(acl, GRANTS)
        self.assertEqual(result['mac'], frozenset({'fixture'}))
        acl.chmod(0o644)
        with self.assertRaises(ValidationError):
            load_project_acl(acl, GRANTS)


if __name__ == '__main__':
    unittest.main()
