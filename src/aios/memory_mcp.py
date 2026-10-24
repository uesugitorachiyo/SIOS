"""Bounded private Memory and read-only SIOS context client."""
from pathlib import Path
import argparse
import re
import unicodedata
from typing import Any

from .client import Client, RemoteError
from .config import Config
from .identifiers import portable_component
from .selective_memory import parse_capture_json, _prepare_capture
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

TOOLS = frozenset({'health', 'memory_read', 'memory_capture',
                   'memory_index_search', 'memory_index_status', 'memory_index_refresh',
                   'read', 'read_revision', 'brain_review'})

_BRAIN_GUIDANCE = frozenset({
    'Brain/AGENTS.md',
    'Brain/workflows/review-candidates.md',
    'Brain/workflows/promote-memory.md',
    'Policies/brain-template-decision.md',
})
_EXACT_CONTEXT = _BRAIN_GUIDANCE | {'Guidance/index.json', 'AGENTS.md'}
_SHARED_SKILLS = frozenset({
    'aios-knowledge', 'aios-mission', 'aios-weekly-review',
    'codebase-design', 'grill-me', 'improve-codebase-architecture',
    'taste-skill', 'to-questionnaire', 'writing-for-agents',
})


def memory_path(value):
    if (not isinstance(value, str) or not value.startswith('Memory/')
            or '\\' in value or ':' in value or '%' in value
            or any(ord(c) < 32 for c in value)
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        raise ToolError('memory_path_required')
    return value


def context_path(value, *, revision=False, owner_named_noncanonical=frozenset()):
    """Bound a shared read without granting server-wide read scope to this MCP surface."""
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or '%' in value or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in value)):
        raise ToolError('context_path_required')
    parts = value.split('/')
    if any(part in {'', '.', '..'} or part.endswith(('.', ' ')) or (part.startswith('.') and
            (index != 0 or part != '.agents')) for index, part in enumerate(parts)):
        raise ToolError('context_path_required')

    mission = re.fullmatch(r'Missions/([a-z0-9][a-z0-9_-]*)/(?:mission|log)\.md', value)
    # Evidence filenames retain their source spelling, including ordinary
    # Unicode, spaces and interior dots. The common component check above and
    # the central Store still reject traversal, hidden nodes and symlinks.
    evidence = (len(parts) >= 4 and parts[0] == 'Missions'
                and re.fullmatch(r'[a-z0-9][a-z0-9_-]*', parts[1])
                and parts[2] == 'evidence' and parts[-1].endswith('.md'))
    mission_markdown = bool(mission or evidence)
    if revision:
        if not mission_markdown:
            raise ToolError('mission_markdown_required')
        return value
    if mission_markdown or value in _EXACT_CONTEXT:
        return value
    if value.startswith('Memory/') and value.endswith('.md'):
        return value
    if re.fullmatch(r'Brain/brain/(?:[a-z0-9][a-z0-9_-]*/)*[a-z0-9][a-z0-9_-]*\.md', value):
        return value
    # Exact owner naming, rather than a case-folded pattern, is the authority
    # boundary here. Historical candidate names include uppercase letters.
    if (value.endswith('.md') and
            ((value.startswith('Brain/inbox/') and len(parts) >= 3) or
             (value.startswith('Brain/working/candidates/') and len(parts) >= 4))):
        if value in owner_named_noncanonical:
            return value
        raise ToolError('owner_named_item_required')
    skill = re.fullmatch(r'\.agents/skills/([a-z0-9-]+)/SKILL\.md', value)
    if skill and skill.group(1) in _SHARED_SKILLS:
        return value
    raise ToolError('context_path_required')


def owner_named_paths(values):
    """Validate exact noncanonical Brain paths supplied by the local launcher."""
    paths = set()
    for value in values:
        if (not isinstance(value, str) or not value.startswith(
                ('Brain/inbox/', 'Brain/working/candidates/')) or value in paths):
            raise ValueError('invalid_owner_named_noncanonical')
        try:
            context_path(value, owner_named_noncanonical={value})
        except ToolError:
            raise ValueError('invalid_owner_named_noncanonical') from None
        paths.add(value)
    return frozenset(paths)


class MemoryGateway:
    def __init__(self, client, *, owner_named_noncanonical=()):
        self.client = client
        self.owner_named_noncanonical = frozenset(owner_named_noncanonical)

    def invoke(self, tool, params):
        if tool not in TOOLS:
            raise ToolError('tool_outside_memory_scope')
        params = dict(params)
        operation = tool
        if tool == 'memory_read':
            params['relative'] = memory_path(params['relative'])
            operation = 'read'
        if tool in {'read', 'read_revision'}:
            params['relative'] = context_path(params['relative'], revision=tool == 'read_revision',
                owner_named_noncanonical=self.owner_named_noncanonical)
        if tool == 'brain_review':
            proposal_id = params.get('proposal_id')
            if not isinstance(proposal_id, str) or not re.fullmatch(r'[0-9a-f]{64}', proposal_id):
                raise ToolError('invalid_request')
        if tool == 'memory_index_refresh':
            try:
                portable_component(params['identifier'])
            except (ValueError, TypeError):
                raise ToolError('invalid_request') from None
        if tool == 'memory_index_refresh':
            marker = params.get('query')
            if not isinstance(marker, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{3,99}', marker):
                raise ToolError('invalid_index_marker: use 4-100 lowercase letters/digits/underscores/hyphens; no spaces; marker must already occur in note')
        if tool == 'memory_capture':
            try:
                capture = parse_capture_json(params['request_json'])
                _prepare_capture(capture)
                # Central-relative evidence must stay in Memory for this surface.
                ref = capture.get('source_reference')
                if ref and not ref.startswith(('https://', 'codex://threads/')):
                    memory_path(ref)
                if capture.get('mission_reference'):
                    raise ToolError('memory_path_required')
            except (ValueError, TypeError, UnicodeError):
                raise ToolError('invalid_request') from None
        try:
            result = self.client.call(operation, params)
        except RemoteError as exc:
            code = exc.code if isinstance(exc.code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', exc.code) else 'remote_error'
            raise ToolError(code) from None
        except Exception:
            raise ToolError('client_call_failed') from None
        if not isinstance(result, dict):
            raise ToolError('invalid_response')
        if tool == 'health':
            return {k: result[k] for k in ('ok', 'database', 'timezone', 'pending_operations') if k in result}
        if tool in {'memory_read', 'read', 'read_revision'}:
            if result.get('path') != params['relative']:
                raise ToolError('invalid_response')
        if tool == 'read':
            if params['relative'].startswith('Brain/brain/') and result.get('authority_status') != 'canonical':
                raise ToolError('invalid_response')
            if params['relative'].startswith(('Brain/inbox/', 'Brain/working/candidates/')) and result.get('authority_status') != 'pending_or_source_not_canonical':
                raise ToolError('invalid_response')
        if tool == 'read_revision' and result.get('content_returned') is not False:
            raise ToolError('invalid_response')
        if tool == 'brain_review' and result.get('proposal_id') != params['proposal_id']:
            raise ToolError('invalid_response')
        return result


def build(client, *, owner_named_noncanonical=()):
    gateway = MemoryGateway(client, owner_named_noncanonical=owner_named_noncanonical)
    server = MCPServer('SIOS-Memory', instructions=(
        'Private shared SIOS Memory and bounded read-only Guidance, Brain and Mission context. '
        'Use read(relative) for full indexed Memory hits and approved context; read_revision is Mission Markdown metadata only. '
        'For an oversized Mission log, compare its read_revision with the source SHA in a versioned Mission evidence snapshot index, then read its Markdown parts only while those revisions match. '
        'Brain/working and Brain/inbox are noncanonical and require an owner-named exact item configured in this gateway; the default allowlist is empty. No Brain proposal or approval, Mission write/completion, thread or routine publication tools. '
        'Writes require explicit task authorization and stable request IDs; preserve exact payload after uncertainty. '
        'No automatic conversation collection. Durable records are English with truthful original provenance. '
        'Antigravity and Claude must not execute, schedule or publish Morning Brief or afternoon Radar; Codex only.'
    ))
    read_ann = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write_ann = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read_ann)
    def health() -> Any:
        """Check central health; never returns server filesystem root."""
        return gateway.invoke('health', {})

    @server.tool(annotations=read_ann)
    def memory_read(relative: str) -> Any:
        """Read full text/revision. Pass relative='Memory/notes/example.md', including Memory/. The parameter is relative, not path. Other roots rejected."""
        return gateway.invoke('memory_read', {'relative': relative})

    @server.tool(annotations=read_ann)
    def read(relative: str) -> Any:
        """Read full text and revision for Memory hit.read_path or approved Guidance, skill, Brain and Mission Markdown. Exact Brain inbox/working items need a configured owner-named allowlist; none are enabled by default. Morning Brief/Radar skills, arbitrary Policies, Runs and non-Markdown evidence are denied."""
        return gateway.invoke('read', {'relative': relative})

    @server.tool(annotations=read_ann)
    def read_revision(relative: str) -> Any:
        """Read SHA-256 and byte count, without the body, for a Mission mission.md, log.md or evidence Markdown. For oversized logs, match this revision to a versioned Mission evidence snapshot index before reading its parts."""
        return gateway.invoke('read_revision', {'relative': relative})

    @server.tool(annotations=read_ann)
    def brain_review(proposal_id: str) -> Any:
        """Read an existing Brain proposal's exact diff and stale-base status. This does not propose, approve or promote Brain content."""
        return gateway.invoke('brain_review', {'proposal_id': proposal_id})

    @server.tool(annotations=write_ann)
    def memory_capture(request_json: str) -> Any:
        """Capture one explicitly selected reusable delta via existing strict JSON contract; no transcripts/secrets. No auto trigger."""
        return gateway.invoke('memory_capture', {'request_json': request_json})

    @server.tool(annotations=read_ann)
    def memory_index_search(query: str) -> Any:
        """Search Memory with ordinary query text, including spaces and hyphens. This is not the stricter refresh verification marker. Read each relevant hit's full read_path."""
        return gateway.invoke('memory_index_search', {'query': query})

    @server.tool(annotations=read_ann)
    def memory_index_status() -> Any:
        """Show central Memory index status; this grants no other store access."""
        return gateway.invoke('memory_index_status', {})

    @server.tool(annotations=write_ann)
    def memory_index_refresh(identifier: str, expected_revision: str,
                             query: str, timeout: int = 30) -> Any:
        """Verify committed note indexing. identifier is filename stem, no path/.md. query is an existing unique lowercase marker matching [a-z0-9][a-z0-9_-]{3,99}, not general search text. Never rewrite a note merely to add a marker. Timeout does not undo commit."""
        return gateway.invoke('memory_index_refresh', dict(identifier=identifier,
            expected_revision=expected_revision, query=query, timeout=timeout))

    return server


def main():
    parser = argparse.ArgumentParser(prog='sios-memory')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--ca-file', type=Path, required=True)
    parser.add_argument('--owner-named-noncanonical', action='append', default=[],
                        metavar='EXACT_BRAIN_PATH')
    parser.add_argument('command', choices=['mcp'])
    args = parser.parse_args()
    cfg = Config.load(args.config)
    if cfg.mode != 'client':
        raise RuntimeError('authenticated_client_required')
    build(Client(cfg, ca_file=args.ca_file),
          owner_named_noncanonical=owner_named_paths(args.owner_named_noncanonical)).run()


if __name__ == '__main__':
    main()
