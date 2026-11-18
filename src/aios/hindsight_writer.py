"""Uninstalled direct-Hindsight SIOS Memory writer candidate.

No credentials, listener, scheduler, production registration, or SIOS source
Store are opened here. Caller must enforce authenticated actor/scopes and
privacy selection before supplying the client and content. A durable, private,
exclusively locked journal must be provided by the eventual runtime. The fake
in-memory journal is for tests only. Unknown writes are never replayed.
"""
from __future__ import annotations
import hashlib
import re
import json
from datetime import datetime, timezone

_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z')
_DOCUMENT = re.compile(r'(?:primary|sios|legacy)-[0-9a-f]{64}\Z')


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def reconcile(client, journal, *, request_id, actor):
    """Resolve a journaled primary write by exact GET, without resending it.

    A missing or mismatched target is not evidence of failure: leave unknown.
    Caller authenticates and holds the same journal/target locks as a writer.
    """
    if not isinstance(request_id,str) or not _REQUEST.fullmatch(request_id):raise ValueError('invalid_request_id')
    if not isinstance(actor,str) or not actor.strip():raise PermissionError('authenticated_actor_required')
    row=journal.get(request_id)
    if row is None:return {'status':'not_found'}
    ident='primary-'+_hash(request_id);metadata=row.get('metadata');state=row.get('state')
    if (row.get('document_id')!=ident or not isinstance(metadata,dict)
            or metadata.get('request_id')!=request_id or metadata.get('source_kind')!='sios-hindsight-primary'
            or not isinstance(metadata.get('content_sha256'),str)
            or not re.fullmatch(r'[0-9a-f]{64}',metadata['content_sha256'])
            or state not in ('unknown','committed')):
        raise ValueError('journal_row_invalid')
    if metadata.get('source_actor')!=actor:raise PermissionError('reconcile_actor_mismatch')
    try:target=client.get(ident)
    except Exception:return {'status':'unknown_no_retry','document_id':ident}
    if (not isinstance(target,dict) or target.get('metadata')!=metadata
            or not isinstance(target.get('text'),str) or _hash(target['text'])!=metadata['content_sha256']):
        return {'status':'unknown_no_retry','document_id':ident}
    if state=='unknown':
        try:journal.mark_committed(request_id)
        except Exception:return {'status':'unknown_no_retry','document_id':ident}
    return {'status':'reconciled_committed' if state=='unknown' else 'already_committed',
            'document_id':ident,'revision':metadata['content_sha256'],'search_visibility':'unverified'}



ORIGIN_TAG = "origin:primary"

def write_primary(client, journal, *, request_id, content, actor, supersedes, project_tag, entities):
    """primary_writer_candidate.write with ISO-now timestamp, origin tag and entities.

    Same journal rules: record before send, one send, exact readback, unknown is
    never resent. The retain timestamp and tags are not part of the journaled
    identity, so replays compare only content, actor, project, supersedes and
    entities (order-insensitive). Entities are recorded in the metadata only when
    present, so documents written without them keep the historical metadata shape.
    """
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise ValueError('invalid_request_id')
    if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 256 * 1024:
        raise ValueError('invalid_content')
    if not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', actor):
        raise ValueError('invalid_actor')
    if not isinstance(project_tag, str) or not re.fullmatch(r'project:[a-z0-9]+(?:-[a-z0-9]+)*', project_tag):
        raise ValueError('invalid_project_tag')
    if supersedes is not None and (not isinstance(supersedes, str) or not re.fullmatch(r'(?:primary|sios)-[0-9a-f]{64}', supersedes)):
        raise ValueError('invalid_supersedes')
    if entities is not None and (not isinstance(entities, list) or len(entities) > 8 or
                                 any(not isinstance(e, str) or not e.strip() or len(e) > 80 for e in entities)):
        raise ValueError('invalid_entities')
    ident = 'primary-' + _hash(request_id)
    metadata = {'source_kind': 'sios-hindsight-primary', 'content_sha256': _hash(content),
                'request_id': request_id, 'source_actor': actor}
    if project_tag != 'project:sios':
        metadata['project_tag'] = project_tag
    if supersedes is not None:
        metadata['supersedes_document_id'] = supersedes
    if entities:
        metadata['entities'] = json.dumps(sorted(e.casefold() for e in entities), ensure_ascii=False,
                                          separators=(',', ':'))

    def same(value):
        return isinstance(value, dict) and value.get('text') == content and value.get('metadata') == metadata

    target = client.get(ident)
    row = journal.get(request_id)
    if row is not None:
        if row.get('document_id') != ident or row.get('metadata') != metadata:
            raise ValueError('request_conflict')
        if row.get('state') not in ('unknown', 'committed'):
            raise ValueError('journal_state_invalid')
        if not same(target):
            return {'status': 'unknown_no_retry', 'document_id': ident}
        if row['state'] == 'unknown':
            try:
                journal.mark_committed(request_id)
            except Exception:
                return {'status': 'unknown_no_retry', 'document_id': ident}
        return {'status': 'already_committed', 'document_id': ident, 'revision': metadata['content_sha256'],
                'search_visibility': 'unverified'}
    if target is not None:
        return {'status': 'target_conflict_no_write', 'document_id': ident}
    try:
        configuration = client._request('/config')
    except Exception:
        return {'status': 'held', 'reason': 'retain_strategy_unavailable'}
    strategies = configuration.get('config', {}).get('retain_strategies') if isinstance(configuration, dict) else None
    if not isinstance(strategies, dict) or strategies.get('sios_primary_chunks') != {'retain_extraction_mode': 'chunks'}:
        return {'status': 'held', 'reason': 'retain_strategy_unconfigured'}
    journal.insert_unknown({'request_id': request_id, 'document_id': ident, 'metadata': metadata, 'state': 'unknown'})
    item = {'content': content, 'document_id': ident,
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'metadata': metadata, 'tags': [project_tag, ORIGIN_TAG], 'strategy': 'sios_primary_chunks'}
    if entities:
        item['entities'] = [{'text': e} for e in entities]
    try:
        client._request('/memories', {'items': [item], 'async': False})
    except Exception:
        return {'status': 'unknown_no_retry', 'document_id': ident}
    try:
        saved = client.get(ident)
    except Exception:
        return {'status': 'unknown_no_retry', 'document_id': ident}
    if not same(saved):
        return {'status': 'unknown_no_retry', 'document_id': ident}
    try:
        journal.mark_committed(request_id)
    except Exception:
        return {'status': 'unknown_no_retry', 'document_id': ident}
    return {'status': 'committed_exact_readback', 'document_id': ident, 'revision': metadata['content_sha256'],
            'search_visibility': 'unverified'}
