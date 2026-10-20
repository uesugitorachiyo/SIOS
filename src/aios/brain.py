"""Reviewed single-document Brain changes using the Store intent journal.

Candidates never change canonical content. An adapter must supply an authenticated
owner identity and ``owner_authorized=True`` to apply a reviewed content hash.
The canonical bytes remain exactly the reviewed candidate; human confirmation is
recorded in a separate immutable approval audit, not invented in old verification
metadata. This validates a generic concept document, not a complete OKF bundle,
its index relationships, linked sources, or the truth of free-form prose. Such
semantic and bundle checks remain the owner's responsibility before approval.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import yaml

from .identifiers import safe_relative
from .store import Store, ValidationError, ConflictError, NotFoundError, _text, _json, _hash

BASES = {'unknown', 'inferred', 'stated', 'observed'}
STATUSES = {'draft', 'stable', 'deprecated'}
_PROTECTED = re.compile(r'verif|confirm|approv|reviewed', re.I)
_ASSERTION = re.compile(r'\b(?:human[-_ ](?:verified|confirmed|approved)|verified[-_ ]by|confirmed[-_ ]by|approved[-_ ]by)\b', re.I)


class _Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result or key == '<<':
                raise ValidationError('Frontmatter keys must be unique strings without merges')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _document(content):
    _text(content)
    match = re.match(r'\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z', content, re.S)
    if not match:
        raise ValidationError('Brain content requires YAML frontmatter and Markdown body')
    if len(match[1].encode('utf-8')) > 64_000:
        raise ValidationError('Brain frontmatter exceeds 64 KB')
    try:
        depth = 0
        for event in yaml.parse(match[1]):
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 20:
                    raise ValidationError('Brain frontmatter nesting exceeds 20 levels')
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if isinstance(event, yaml.AliasEvent):
                raise ValidationError('YAML aliases are not accepted in Brain frontmatter')
        document = yaml.load(match[1], Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ValidationError('Invalid Brain YAML frontmatter') from exc
    if not isinstance(document, dict):
        raise ValidationError('Brain frontmatter must be a mapping')
    for key in ('title', 'type'):
        _text(document.get(key), f'Brain {key}', 300)
    if not isinstance(document.get('status'), str) or document['status'] not in STATUSES:
        raise ValidationError('Brain status must be draft, stable, or deprecated')
    if not match[2].strip():
        raise ValidationError('Brain Markdown body must be nonempty')
    return document, match[2]


def _protected(document, path=()):
    result = {}
    if isinstance(document, dict):
        for key, value in document.items():
            child = path + (key,)
            if _PROTECTED.search(key):
                result[child] = value
            else:
                result.update(_protected(value, child))
    elif isinstance(document, list):
        for number, value in enumerate(document):
            result.update(_protected(value, path + (number,)))
    elif isinstance(document, str) and (_ASSERTION.search(document) or document.lower() in {'verified', 'confirmed', 'approved'}):
        result[path] = document
    return result


def _verification_unchanged(content, old):
    current, body = _document(content)
    previous, previous_body = _document(old) if old is not None else ({}, '')
    if _protected(current) != _protected(previous):
        raise ValidationError('Proposals must preserve existing verification metadata; approval is recorded separately')
    old_assertions = [line for line in previous_body.splitlines() if _ASSERTION.search(line)]
    new_assertions = [line for line in body.splitlines() if _ASSERTION.search(line)]
    if old_assertions != new_assertions:
        raise ValidationError('Proposals cannot introduce or change explicit human-verification assertions')


def _target(value):
    try:
        result = safe_relative(value).as_posix()
    except (TypeError, ValueError) as exc:
        raise ValidationError('Unsafe canonical Brain target') from exc
    if not result.startswith('Brain/brain/') or not result.endswith('.md'):
        raise ValidationError('Target must be a Markdown document under Brain/brain')
    return result


def _proposal_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValidationError('Invalid Brain proposal ID')
    return value


def _sources(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValidationError('Sources require 1–100 resource/title mappings')
    result = []
    for source in value:
        if not isinstance(source, dict) or set(source) != {'resource', 'title'}:
            raise ValidationError('Each source requires resource and title')
        result.append({'resource': _text(source['resource'], 'source resource', 4000),
                       'title': _text(source['title'], 'source title', 500)})
    return result


class BrainWorkflow:
    def __init__(self, store: Store):
        self.store = store

    def propose(self, target, content, request_id, expected_revision=None, sources=None,
                basis='unknown', actor='local'):
        target = _target(target)
        _text(content)
        _text(request_id, 'request_id', 200)
        sources = _sources(sources)
        if not isinstance(basis, str) or basis not in BASES:
            raise ValidationError('Basis must be unknown, inferred, observed, or stated')
        if expected_revision is not None and not re.fullmatch(r'[0-9a-f]{64}', str(expected_revision)):
            raise ValidationError('Expected revision must be SHA-256 or null for creation')
        document, _ = _document(content)
        if 'basis' in document and document['basis'] != basis:
            raise ValidationError('Frontmatter basis must match proposal basis')
        for source_key in ('sources', 'source'):
            if source_key in document:
                frontmatter_sources = document[source_key]
                if (not isinstance(frontmatter_sources, list) or
                    any(not isinstance(source, dict) or not {'resource', 'title'} <= set(source) or
                        set(source) - {'id', 'resource', 'title', 'author'} for source in frontmatter_sources)):
                    raise ValidationError('Frontmatter sources require resource/title with optional id/author')
                projection = [{'resource': source['resource'], 'title': source['title']} for source in frontmatter_sources]
                if projection != sources:
                    raise ValidationError('Frontmatter source resource/title must match proposal sources')
        proposal_id = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        revision = _hash(content.encode('utf-8'))
        metadata_path = f'Runs/brain-proposals/{proposal_id}.json'
        candidate_path = f'Brain/working/candidates/{proposal_id}.md'
        def build():
            if self.store._bytes(metadata_path) is not None or self.store._bytes(candidate_path) is not None:
                raise ConflictError('Proposal already exists')
            old = self.store._bytes(target)
            if _hash(old) != expected_revision:
                raise ConflictError('Canonical Brain revision changed; reread before proposing')
            _verification_unchanged(content, old.decode('utf-8') if old is not None else None)
            metadata = {'schema_version': 1, 'proposal_id': proposal_id, 'request_id': request_id, 'target': target,
                        'candidate': candidate_path, 'proposal_revision': revision,
                        'base_revision': expected_revision, 'base_content': old.decode('utf-8') if old is not None else None,
                        'sources': sources, 'basis': basis, 'proposed_by': actor,
                        'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            result = {'proposal_id': proposal_id, 'proposal_revision': revision, 'path': candidate_path,
                      'revision': revision, 'target': target, 'base_revision': expected_revision}
            return [(candidate_path, content), (metadata_path, _json(metadata)+'\n')], result
        return self.store._operation(request_id, actor, {'operation': 'brain_propose', 'target': target,
                                    'content': content, 'sources': sources, 'basis': basis,
                                    'expected_revision': expected_revision}, build)

    def _load(self, proposal_id):
        proposal_id = _proposal_id(proposal_id)
        raw = self.store._bytes(f'Runs/brain-proposals/{proposal_id}.json')
        if raw is None:
            raise NotFoundError(proposal_id)
        try:
            metadata = json.loads(raw)
            if hashlib.sha256(metadata['request_id'].encode('utf-8')).hexdigest() != proposal_id:
                raise ValueError()
            # Bind immutable provenance and candidate hash to the durable original intent.
            with contextlib.closing(sqlite3.connect(self.store._path('Database/state'))) as reader:
                original = reader.execute('SELECT intent,complete FROM operations WHERE request_id=?', (metadata['request_id'],)).fetchone()
            if original is None:
                raise ValueError()
            matching = [item for item in json.loads(original[0]) if item['path'] == f'Runs/brain-proposals/{proposal_id}.json']
            if len(matching) != 1 or matching[0]['text'].encode('utf-8') != raw:
                raise ValueError()
            if metadata['schema_version'] != 1 or metadata['proposal_id'] != proposal_id:
                raise ValueError()
            if metadata['candidate'] != f'Brain/working/candidates/{proposal_id}.md':
                raise ValueError()
            _target(metadata['target'])
            _sources(metadata['sources'])
            if metadata['basis'] not in BASES:
                raise ValueError()
            base = metadata['base_content']
            if base is not None and not isinstance(base, str):
                raise ValueError()
            if _hash(base.encode('utf-8') if base is not None else None) != metadata['base_revision']:
                raise ValueError()
            candidate = self.store._bytes(metadata['candidate'])
            if candidate is None or _hash(candidate) != metadata['proposal_revision']:
                raise ValueError()
            content = candidate.decode('utf-8')
            _verification_unchanged(content, base)
        except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
            raise ConflictError('Proposal metadata or candidate changed; preserve the original proposal') from exc
        return metadata, content

    def review(self, proposal_id):
        with self.store._connection() as db:
            self.store._recover(db)
            metadata, content = self._load(proposal_id)
            current = self.store._bytes(metadata['target'])
            return {'proposal_id': proposal_id, 'target': metadata['target'], 'content': content,
                    'proposal_revision': metadata['proposal_revision'], 'base_revision': metadata['base_revision'],
                    'sources': metadata['sources'], 'basis': metadata['basis'],
                    'proposed_by': metadata['proposed_by'], 'stale': _hash(current) != metadata['base_revision'],
                    'warnings': ['Source claims and index/link consistency require owner review.'] +
                                ([f"This proposal has {metadata['basis']} basis; approval will not convert it to stated."] if metadata['basis'] in {'unknown', 'inferred'} else []),
                    'approval_effect': 'Canonical bytes will equal this exact content. A separate immutable audit records the authenticated owner approval; existing verification metadata is unchanged.',
                    'diff': ''.join(difflib.unified_diff((metadata['base_content'] or '').splitlines(keepends=True),
                                                        content.splitlines(keepends=True), fromfile=metadata['target'], tofile=metadata['candidate']))}

    def apply(self, proposal_id, proposal_revision, request_id, actor, owner_authorized: bool=False):
        if owner_authorized is not True:
            raise PermissionError('Canonical Brain changes require authenticated owner approval')
        _text(actor, 'authenticated owner identity', 200)
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', actor):
            raise ValidationError('Authenticated owner identity must be a lowercase device slug')
        proposal_id = _proposal_id(proposal_id)
        if not isinstance(proposal_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', proposal_revision):
            raise ValidationError('Approval requires the exact reviewed proposal SHA-256')
        audit_path = f'Runs/brain-approvals/{proposal_id}.json'
        def build():
            metadata, content = self._load(proposal_id)
            if metadata['proposal_revision'] != proposal_revision:
                raise ConflictError('Approval hash differs from the reviewed proposal')
            if self.store._bytes(audit_path) is not None:
                raise ConflictError('Proposal was already applied; replay its original request ID')
            current = self.store._bytes(metadata['target'])
            if _hash(current) != metadata['base_revision']:
                raise ConflictError('Canonical Brain changed after proposal; create a new proposal')
            _verification_unchanged(content, current.decode('utf-8') if current is not None else None)
            confirmed_at = dt.datetime.now(dt.timezone.utc).isoformat()
            audit = {'schema_version': 1, 'proposal_id': proposal_id, 'proposal_revision': proposal_revision,
                     'target': metadata['target'], 'base_revision': metadata['base_revision'],
                     'sources': metadata['sources'], 'basis': metadata['basis'],
                     'event': 'human-confirmed', 'actor': f'human:{actor}',
                     'confirmed_at': confirmed_at, 'request_id': request_id,
                     'verified': {'by': f'human:{actor}', 'at': confirmed_at},
                     'old_revision': metadata['base_revision'], 'new_revision': proposal_revision,
                     'provenance': {'sources': metadata['sources'], 'basis': metadata['basis'], 'proposed_by': metadata['proposed_by']},
                     'scope': 'Approval of this exact canonical content; no retroactive verification claims.'}
            result = {'path': metadata['target'], 'revision': proposal_revision, 'proposal_id': proposal_id,
                      'audit_path': audit_path, 'approval': audit}
            return [(metadata['target'], content), (audit_path, _json(audit)+'\n')], result
        return self.store._operation(request_id, actor, {'operation': 'brain_apply', 'proposal_id': proposal_id,
                                    'proposal_revision': proposal_revision}, build)
