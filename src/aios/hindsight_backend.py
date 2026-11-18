"""Experimental single-trust-domain Hindsight gateway backend.

Project tags filter recall; they do not authorize access. Every gateway device
must be allowed to read every record in this bank. Deployment and service
registration are intentionally outside this module.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from itertools import zip_longest
from .hindsight_journal import PrimaryJournal, PrimaryJournalReader
from .hindsight_writer import reconcile, write_primary

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_PATH = re.compile(r'Memory/hindsight/(primary-[0-9a-f]{64})\.md\Z')
_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z')


class UncertainWrite(Exception):
    pass


def classify(row):
    meta = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
    ident = row.get('document_id') if isinstance(row.get('document_id'), str) else None
    body = row.get('text') if isinstance(row.get('text'), str) else ''
    result = {'document_id': ident, 'excerpt': body[:600], 'excerpt_truncated': len(body) > 600,
              'tags': row.get('tags') if isinstance(row.get('tags'), list) else [], 'authoritative': False}
    if meta.get('source_kind') == 'sios-hindsight-primary' and ident and ident.startswith('primary-') and _HASH.fullmatch(ident[8:]):
        rev = meta.get('content_sha256')
        result.update(kind='hindsight_primary', source_path=f'Memory/hindsight/{ident}.md',
                      source_revision=rev if isinstance(rev, str) and _HASH.fullmatch(rev) else None)
    elif isinstance(meta.get('source_path'), str) and isinstance(meta.get('source_revision'), str) and _HASH.fullmatch(meta['source_revision']) and ident == 'sios-' + hashlib.sha256(meta['source_path'].encode()).hexdigest():
        result.update(kind='basic_source_copy', source_path=meta['source_path'], source_revision=meta['source_revision'])
    else:
        result.update(kind='unknown_origin', source_path=None, source_revision=None)
    return result


class HindsightBackend:
    def __init__(self, client, journal_root, target_lock, allowed_project_tags):
        self.client = client
        self.root = Path(journal_root)
        self.lock = Path(target_lock)
        self.tags = frozenset(allowed_project_tags)
        if not self.tags or any(not isinstance(t, str) or not re.fullmatch(r'project:[a-z0-9]+(?:-[a-z0-9]+)*', t) for t in self.tags):
            raise ValueError('invalid_project_tags')

    def health(self):
        return {'ok': True, 'backend': 'hindsight'}

    def map_error(self, exc):
        if isinstance(exc, UncertainWrite):
            return 503, 'unknown_no_retry'
        if isinstance(exc, BlockingIOError):
            return 503, 'writer_busy'
        if isinstance(exc, (ValueError, PermissionError)):
            value = str(exc)
            if re.fullmatch(r'(?:invalid_[a-z_]+|request_conflict|reconcile_actor_mismatch|journal_[a-z_]+)', value):
                return 400, value
        return 500, 'internal_error'

    def _tag(self, args):
        tag = args.get('project_tag')
        if tag not in self.tags:
            raise ValueError('invalid_project_tag')
        return tag

    def _recall(self, args):
        tag = self._tag(args)
        query, limit = args.get('query'), args.get('limit', 10)
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError('invalid_query')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('invalid_limit')
        def search(tags):
            data = self.client._request('/memories/recall', {'query': query, 'budget': 'low', 'max_tokens': 2048,
                                                              'tags': tags, 'tags_match': 'all_strict'})
            rows = data.get('results')
            if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
                raise ValueError('invalid_results')
            return [classify(row) for row in rows]
        main = search([tag])
        try:
            lane, lane_status = search([tag, 'origin:primary']), 'ok'
        except Exception:
            lane, lane_status = [], 'unavailable'
        seen, merged = set(), []
        for pair in zip_longest(lane, main):
            for row in pair:
                if row is None or (row['document_id'] is not None and row['document_id'] in seen):
                    continue
                if row['document_id'] is not None:
                    seen.add(row['document_id'])
                merged.append(row)
        return {'results': merged[:limit], 'count': min(len(merged), limit), 'total_returned': len(merged),
                'truncated': len(merged) > limit, 'lane_status': lane_status, 'coverage': 'unknown'}

    def _read(self, path):
        match = _PATH.fullmatch(path) if isinstance(path, str) else None
        if match is None:
            raise ValueError('invalid_path')
        ident = match.group(1)
        with PrimaryJournalReader(self.root) as journal:
            if journal is None:
                raise ValueError('journal_unavailable')
            row = journal._db.execute('SELECT metadata, state FROM writes WHERE document_id=?', (ident,)).fetchone()
        if row is None or row[1] != 'committed':
            raise ValueError('invalid_path')
        metadata = json.loads(row[0])
        doc = self.client.get(ident)
        if not isinstance(doc, dict) or doc.get('metadata') != metadata or not isinstance(doc.get('text'), str):
            raise ValueError('invalid_source')
        revision = hashlib.sha256(doc['text'].encode()).hexdigest()
        if revision != metadata.get('content_sha256'):
            raise ValueError('invalid_source')
        return {'path': path, 'source_revision': revision, 'text': doc['text'], 'actor_status': 'unattested'}

    def call(self, op, actor, args):
        if op == 'memory_recall':
            return self._recall(args)
        if op == 'memory_read_primary':
            return self._read(args.get('path'))
        request_id = args.get('request_id')
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise ValueError('invalid_request_id')
        with PrimaryJournal(self.root, target_lock=self.lock) as journal:
            if op == 'memory_store_reconcile':
                result = reconcile(self.client, journal, request_id=request_id, actor=actor)
                if result.get('status') == 'unknown_no_retry':
                    raise UncertainWrite()
                if result.get('status') in ('reconciled_committed', 'already_committed'):
                    result['verification_scope'] = 'text_and_metadata_only'
                return result
            if op == 'memory_store':
                tag = self._tag(args)
                supersedes = args.get('supersedes')
                if supersedes is not None:
                    if not isinstance(supersedes, str) or not re.fullmatch(r'primary-[0-9a-f]{64}', supersedes):
                        raise ValueError('invalid_supersedes')
                    row = journal._db.execute('SELECT metadata, state FROM writes WHERE document_id=?', (supersedes,)).fetchone()
                    if row is None or row[1] != 'committed':
                        raise ValueError('invalid_supersedes')
                    previous = json.loads(row[0])
                    prior_doc = self.client.get(supersedes)
                    current_ident = 'primary-' + hashlib.sha256(request_id.encode()).hexdigest()
                    competing = [item for item in journal.primary_pending_or_committed_successors(supersedes)
                                 if item.get('document_id') != current_ident]
                    if (previous.get('project_tag', 'project:sios') != tag or not isinstance(prior_doc, dict)
                            or prior_doc.get('metadata') != previous or not isinstance(prior_doc.get('text'), str)
                            or hashlib.sha256(prior_doc['text'].encode()).hexdigest() != previous.get('content_sha256')
                            or competing):
                        raise ValueError('invalid_supersedes')
                result = write_primary(self.client, journal, request_id=request_id, content=args.get('content'),
                                       actor=actor, project_tag=tag, supersedes=supersedes, entities=args.get('entities'))
                if result['status'] == 'unknown_no_retry':
                    raise UncertainWrite()
                if result['status'] in ('already_committed', 'committed_exact_readback'):
                    result.update(source_path=f"Memory/hindsight/{result['document_id']}.md", source_revision=result['revision'],
                                  verification_scope='text_and_metadata_only')
                return result
        raise ValueError('invalid_operation')
