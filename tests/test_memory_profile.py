"""Contract checks for the bounded SIOS Memory and context MCP surface."""

import asyncio
import json
import unittest

from aios.memory_mcp import MemoryGateway, TOOLS, build, owner_named_paths
from mcp.server.mcpserver.exceptions import ToolError


EXPECTED_TOOLS = {
    'health', 'memory_read', 'memory_capture',
    'memory_index_search', 'memory_index_status', 'memory_index_refresh',
    'read', 'read_revision', 'brain_review',
}


class Fake:
    def __init__(self):
        self.calls = []
        self.wrong_path = False
        self.wrong_authority = False
        self.revision_has_body = False

    def call(self, operation, params):
        self.calls.append((operation, params))
        if operation == 'health':
            return {'ok': True, 'database': 'ok', 'data_root': 'not-for-clients'}
        if operation == 'read':
            path = 'Memory/notes/wrong.md' if self.wrong_path else params['relative']
            result = {'path': path, 'text': 'synthetic', 'revision': 'a' * 64}
            if path.startswith('Brain/brain/'):
                result['authority_status'] = 'pending_or_source_not_canonical' if self.wrong_authority else 'canonical'
            elif path.startswith(('Brain/inbox/', 'Brain/working/candidates/')):
                result['authority_status'] = 'canonical' if self.wrong_authority else 'pending_or_source_not_canonical'
            return result
        if operation == 'read_revision':
            return {'path': 'Missions/wrong/log.md' if self.wrong_path else params['relative'],
                    'revision': 'a' * 64, 'bytes': 42,
                    'content_returned': self.revision_has_body}
        if operation == 'brain_review':
            return {'proposal_id': 'b' * 64 if self.wrong_path else params['proposal_id'],
                    'diff': 'synthetic', 'stale': False}
        return {'status': 'synthetic'}


class Tests(unittest.TestCase):
    def setUp(self):
        self.client = Fake()
        self.gateway = MemoryGateway(self.client, owner_named_noncanonical={
            'Brain/inbox/owner-named.md',
            'Brain/working/candidates/owner-named.md',
        })

    def test_literal_discovery_contract(self):
        self.assertEqual(TOOLS, EXPECTED_TOOLS)
        listed = asyncio.run(build(self.client).list_tools())
        self.assertEqual(len(listed), 9)
        self.assertEqual({tool.name for tool in listed}, EXPECTED_TOOLS)
        by_name = {tool.name: tool for tool in listed}
        for name in ('read', 'read_revision', 'brain_review'):
            # Check the wire alias clients see; the SDK attribute name varies by version.
            self.assertIs(by_name[name].annotations.model_dump(by_alias=True)['readOnlyHint'], True)
        self.assertIn('snapshot', by_name['read_revision'].description)

    def test_existing_memory_read_retains_legacy_name_support(self):
        self.gateway.invoke('memory_read', {'relative': 'Memory/notes/Unicode â€” source.md'})
        self.assertEqual(self.client.calls[0][0], 'read')

    def test_shared_read_covers_memory_guidance_brain_and_missions(self):
        paths = [
            'Memory/notes/Unicode â€” source.md', 'Guidance/index.json', 'AGENTS.md',
            '.agents/skills/aios-knowledge/SKILL.md', '.agents/skills/aios-mission/SKILL.md',
            '.agents/skills/aios-weekly-review/SKILL.md', 'Brain/AGENTS.md',
            'Brain/workflows/review-candidates.md', 'Brain/workflows/promote-memory.md',
            'Policies/brain-template-decision.md', 'Brain/brain/index.md',
            'Brain/brain/goals/example.md', 'Brain/inbox/owner-named.md',
            'Brain/working/candidates/owner-named.md', 'Missions/example/mission.md',
            'Missions/example/log.md', 'Missions/example/evidence/log-read-snapshot-abc/index.md',
            'Missions/example/evidence/nested/report.md',
            'Missions/example/evidence/Review V2.md',
            'Missions/example/evidence/日本語/review.v2.md',
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.gateway.invoke('read', {'relative': path})['path'], path)
        self.assertEqual(len(self.client.calls), len(paths))
        self.assertTrue(all(operation == 'read' for operation, _ in self.client.calls))

    def test_context_denials_happen_before_transport(self):
        rejected = [
            'Policies/routine-focus.md', 'Policies/selective-memory.md',
            'Brain/templates/decision.md', 'Brain/prompts/quick-brain-dump.md',
            'Brain/sources/example.md', 'Brain/working/other.md',
            'Brain/inbox/other.md', 'Brain/working/candidates/other.md',
            'Brain/brain/.hidden.md', 'Brain/brain/index.json',
            '.agents/skills/aios-morning-brief/SKILL.md',
            '.agents/skills/aios-opportunity-radar/SKILL.md',
            '.agents/skills/unregistered/SKILL.md',
            'Missions/example/MISSION.md', 'Missions/Example/mission.md',
            'Missions/example/evidence/result.json', 'Missions/example/evidence/.hidden.md',
            'Missions/example/Evidence/report.md', 'Missions/example/evidence/../log.md',
            'Missions/example//log.md', 'Missions\\example\\log.md',
            'Missions/example/%2e%2e/log.md', 'Missions/example/C:/log.md',
            'Runs/example.md', 'Database/state.md', 'Logs/example.md',
            '../Missions/example/log.md', '/Missions/example/log.md',
            'Missions/example/log.md\x00', '', None, 4,
        ]
        for path in rejected:
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.gateway.invoke('read', {'relative': path})
        self.assertEqual(self.client.calls, [])

    def test_noncanonical_default_is_fail_closed(self):
        gateway = MemoryGateway(self.client)
        for path in ['Brain/inbox/owner-named.md',
                     'Brain/working/candidates/owner-named.md']:
            with self.subTest(path=path), self.assertRaisesRegex(ToolError, 'owner_named_item_required'):
                gateway.invoke('read', {'relative': path})
        self.assertEqual(self.client.calls, [])

    def test_launcher_owner_named_paths_are_exact_and_noncanonical_only(self):
        named = owner_named_paths([
            'Brain/inbox/owner-named.md',
            'Brain/working/candidates/owner-named.md',
            'Brain/working/candidates/REVIEW-2026-09-30.md',
        ])
        gateway = MemoryGateway(self.client, owner_named_noncanonical=named)
        for path in named:
            self.assertEqual(gateway.invoke('read', {'relative': path})['path'], path)
        self.client.calls.clear()
        for path in ['Brain/inbox/other.md',
                     'Brain/working/candidates/other.md']:
            with self.subTest(path=path), self.assertRaisesRegex(ToolError, 'owner_named_item_required'):
                gateway.invoke('read', {'relative': path})
        for path in [
            'Brain/brain/index.md', 'Brain/AGENTS.md', 'Brain/inbox/../brain/index.md',
            'Brain/inbox/example.json', 'Brain/inbox/.hidden.md',
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                owner_named_paths([path])
        with self.assertRaises(ValueError):
            owner_named_paths(['Brain/inbox/owner-named.md',
                               'Brain/inbox/owner-named.md'])
        self.assertEqual(self.client.calls, [])

    def test_revision_only_allows_mission_markdown(self):
        for path in ['Missions/example/mission.md', 'Missions/example/log.md',
                     'Missions/example/evidence/log-read-snapshot-abc/part-001.md',
                     'Missions/example/evidence/Review V2.md',
                     'Missions/example/evidence/日本語/review.v2.md']:
            with self.subTest(path=path):
                self.assertFalse(self.gateway.invoke('read_revision', {'relative': path})['content_returned'])
        self.client.calls.clear()
        for path in ['Memory/notes/example.md', 'Brain/brain/index.md',
                     'Missions/example/evidence/receipt.json', 'Missions/example/LOG.md',
                     'Missions/example/evidence/.secret.md']:
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.gateway.invoke('read_revision', {'relative': path})
        self.assertEqual(self.client.calls, [])

    def test_wrong_response_path_and_authority_are_rejected(self):
        self.client.wrong_path = True
        for tool, path in [('memory_read', 'Memory/notes/example.md'),
                           ('read', 'Missions/example/mission.md'),
                           ('read_revision', 'Missions/example/log.md')]:
            with self.subTest(tool=tool), self.assertRaisesRegex(ToolError, 'invalid_response'):
                self.gateway.invoke(tool, {'relative': path})
        self.client.wrong_path = False
        self.client.wrong_authority = True
        for path in ['Brain/brain/index.md', 'Brain/inbox/owner-named.md']:
            with self.subTest(path=path), self.assertRaisesRegex(ToolError, 'invalid_response'):
                self.gateway.invoke('read', {'relative': path})
        self.client.wrong_authority = False
        self.client.revision_has_body = True
        with self.assertRaisesRegex(ToolError, 'invalid_response'):
            self.gateway.invoke('read_revision', {'relative': 'Missions/example/log.md'})

    def test_brain_review_is_read_only_and_writes_are_unexposed(self):
        proposal_id = 'a' * 64
        self.gateway.invoke('brain_review', {'proposal_id': proposal_id})
        self.assertEqual(self.client.calls, [('brain_review', {'proposal_id': proposal_id})])
        self.client.calls.clear()
        for invalid in ['../abc', 'ABC123', None, 'abc123']:
            with self.subTest(proposal_id=invalid), self.assertRaises(ToolError):
                self.gateway.invoke('brain_review', {'proposal_id': invalid})
        for tool in ['context_read', 'memory_write', 'brain_propose', 'brain_apply', 'mission_write',
                     'mission_complete', 'publish', 'thread_post', 'catalog', 'history']:
            with self.subTest(tool=tool), self.assertRaises(ToolError):
                self.gateway.invoke(tool, {})
        self.assertEqual(self.client.calls, [])

    def test_wrong_brain_review_response_is_rejected(self):
        self.client.wrong_path = True
        with self.assertRaisesRegex(ToolError, 'invalid_response'):
            self.gateway.invoke('brain_review', {'proposal_id': 'a' * 64})

    def test_existing_memory_only_guard_remains(self):
        for path in ['Brain/brain/index.md', 'Missions/example/mission.md',
                     'Memory/../Brain/index.md', 'Memory\\notes\\a.md',
                     'Memory//a.md', '/Memory/a.md', 'Memory/%2e%2e/a.md',
                     'Memory/C:/a.md', 'Memory/./a.md']:
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.gateway.invoke('memory_read', {'relative': path})
        self.assertEqual(self.client.calls, [])

    def test_refresh_identifier_cannot_escape(self):
        for identifier in ['../x', 'a/b', 'Brain/a']:
            with self.subTest(identifier=identifier), self.assertRaises(ToolError):
                self.gateway.invoke('memory_index_refresh', {
                    'identifier': identifier, 'expected_revision': 'a' * 64,
                    'query': 'valid-marker',
                })
        self.assertEqual(self.client.calls, [])

    def test_fixed_health_fields(self):
        self.assertNotIn('data_root', self.gateway.invoke('health', {}))

    def test_payload_free_nosave_passes_without_new_selection(self):
        self.gateway.invoke('memory_capture', {'request_json': '{"retain":false}'})
        self.assertEqual(self.client.calls, [('memory_capture', {'request_json': '{"retain":false}'})])

    def test_capture_root_override_rejected(self):
        with self.assertRaises(ToolError):
            self.gateway.invoke('memory_capture', {'request_json': '{"retain":false,"root":"Brain"}'})
        self.assertEqual(self.client.calls, [])

    def test_capture_mission_reference_rejected_before_transport(self):
        capture = {
            'retain': True, 'kind': 'delta', 'request_id': 'example-request',
            'meaning_id': 'example-meaning', 'claim': 'Synthetic claim.',
            'source_type': 'direct_user', 'source_actor': 'owner',
            'source_reference': 'codex://threads/00000000-0000-4000-8000-000000000000',
            'project': 'example', 'conditions': [], 'outcome': 'synthetic',
            'evidence_status': 'stated', 'recorded_at': '2026-10-22T00:00:00+00:00',
            'retrieval_cue': 'synthetic',
        }
        # Control: the same payload without mission_reference is otherwise valid.
        self.gateway.invoke('memory_capture', {'request_json': json.dumps(capture)})
        self.assertEqual(len(self.client.calls), 1)
        self.client.calls.clear()
        capture['mission_reference'] = 'Missions/example/mission.md'
        with self.assertRaisesRegex(ToolError, 'memory_path_required'):
            self.gateway.invoke('memory_capture', {'request_json': json.dumps(capture)})
        self.assertEqual(self.client.calls, [])

    def test_refresh_hyphen_marker_forwarded(self):
        self.gateway.invoke('memory_index_refresh', {'identifier': 'example',
                            'expected_revision': 'a' * 64, 'query': 'example-marker'})
        self.assertEqual(self.client.calls[0][1]['query'], 'example-marker')

    def test_refresh_invalid_marker_rejected_before_transport(self):
        for query in ['two words', 'UPPERCASE', 'abc', 'x' * 101, None]:
            with self.subTest(query=query), self.assertRaisesRegex(ToolError, 'invalid_index_marker'):
                self.gateway.invoke('memory_index_refresh', {'identifier': 'example', 'query': query})
        self.assertEqual(self.client.calls, [])

    def test_general_search_not_restricted_to_marker(self):
        for query in ['two words', 'UPPERCASE', 'example-marker']:
            self.gateway.invoke('memory_index_search', {'query': query})
        self.assertEqual(len(self.client.calls), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
