"""Manual owner confirmation over authenticated, server-retained review state."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import re


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('Timezone-aware expiry required')
    return parsed


def approve(transport, verification_id, content, expires_at, *, interactive,
            confirm=input, output=print, clock=lambda: datetime.now(timezone.utc),
            expected_mission=None, expected_control_digest=None, expected_revision=None):
    if interactive is not True:
        raise ValueError('Owner approval requires interactive terminal review')
    desired = hashlib.sha256(content.encode('utf-8')).hexdigest()
    pins = {'mission': expected_mission, 'control_digest': expected_control_digest,
            'expected_revision': expected_revision}
    if any(value is not None for value in pins.values()):
        if not all(isinstance(value, str) and value for value in pins.values()):
            raise ValueError('Complete owner handoff pins required')
        for value in (expected_control_digest, expected_revision):
            if not re.fullmatch('[0-9a-f]{64}', value):
                raise ValueError('Invalid owner handoff digest')
    request = {'verification_id': verification_id, 'content': content}

    def checked(review):
        if any(value is not None and review.get(key) != value for key, value in pins.items()):
            raise ValueError('Authenticated review differs from owner handoff')
        for key in ('control_digest', 'expected_revision', 'current_revision',
                    'desired_mission_sha256', 'review_digest'):
            if not isinstance(review.get(key), str) or not re.fullmatch('[0-9a-f]{64}', review[key]):
                raise ValueError('Incomplete authenticated review')
        if (review.get('verification_id') != verification_id
                or review['desired_mission_sha256'] != desired
                or review['current_revision'] != review['expected_revision']):
            raise ValueError('Review target or revision changed')
        if (review.get('eligible') is not True or review.get('independent') is not True
                or review.get('qa', {}).get('status') != 'PASS'
                or review.get('security', {}).get('status') != 'PASS'
                or review.get('owner_approval_required') is not True):
            raise ValueError('Independent review/evidence is incomplete')
        instant = clock()
        if (not instant < _time(review['review_expires_at'])
                or not instant < _time(expires_at) <= _time(review['request_expires_at'])):
            raise ValueError('Review or requested approval expired')
        return review

    first = checked(transport.call('completion_review', request))
    # JSON escapes terminal control characters in untrusted Mission/evidence text.
    output(json.dumps(first, ensure_ascii=False, indent=2))
    output('Requested approval expiry: ' + expires_at)
    expected = 'APPROVE ' + first['mission'] + ' ' + first['control_digest']
    if confirm('Type ' + expected + ' to approve, or cancel: ') != expected:
        raise ValueError('Owner approval cancelled')
    checked(first)  # The owner may have spent time reviewing the display.
    second = checked(transport.call('completion_review', request))
    pinned = ('mission', 'verification_id', 'control_digest', 'expected_revision',
              'current_revision', 'desired_mission_sha256', 'review_digest',
              'request_expires_at', 'diff', 'qa', 'security', 'independent')
    if any(first.get(k) != second.get(k) for k in pinned):
        raise ValueError('Review changed; restart owner review')
    return transport.call('completion_approve', {
        'verification_id': verification_id,
        'control_digest': first['control_digest'], 'expires_at': expires_at})
