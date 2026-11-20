"""Independent whole-result specification for the B3 fixed-data core profile.

Derived from the core subset of the public reference oracle. No pager, cursor,
continuation, pruning or retained-coverage implementation is imported here.
"""

import json
from pathlib import Path


MAX_FIXTURE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 10000
MAX_STAGES = 32
MAX_SORT_KEY = 2**53 - 1


def _name(value):
    return type(value) is str and 1 <= len(value) <= 128 and all(32 <= ord(c) <= 126 for c in value)


def _fields(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError(f"{label} must contain exactly {', '.join(expected)}")


def validate_fixture(fixture):
    """Validate without changing the input; return that same fixture object."""
    _fields(fixture, ('schema_version', 'name', 'records', 'stages'), 'Fixture')
    if type(fixture['schema_version']) is not int or fixture['schema_version'] != 1:
        raise ValueError('Unsupported fixture schema_version')
    if not _name(fixture['name']):
        raise ValueError('Fixture name must be a nonempty printable ASCII string of at most 128 characters')
    records, stages = fixture['records'], fixture['stages']
    if type(records) is not list or len(records) > MAX_RECORDS:
        raise ValueError(f'Fixture records must be a list of at most {MAX_RECORDS} records')
    if type(stages) is not list or len(stages) > MAX_STAGES:
        raise ValueError(f'Fixture stages must be a list of at most {MAX_STAGES} stages')
    identities = set()
    for record in records:
        _fields(record, ('id', 'sort_key'), 'Record')
        identity, key = record['id'], record['sort_key']
        if not _name(identity) or identity in identities:
            raise ValueError('Record IDs must be distinct printable ASCII strings of 1..128 characters')
        if type(key) is not int or not -MAX_SORT_KEY <= key <= MAX_SORT_KEY:
            raise ValueError('sort_key must be an integer in the exact JSON numeric profile')
        identities.add(identity)
    names = set()
    for stage in stages:
        _fields(stage, ('name', 'ids', 'quota'), 'Stage')
        name, members, quota = stage['name'], stage['ids'], stage['quota']
        if not _name(name) or name in names:
            raise ValueError('Stage names must be distinct printable ASCII strings of 1..128 characters')
        names.add(name)
        if type(members) is not list or len(members) > MAX_RECORDS:
            raise ValueError('Stage ids must be a bounded list')
        if any(type(identity) is not str or identity not in identities for identity in members):
            raise ValueError('Every stage membership must name a record ID')
        if len(set(members)) != len(members):
            raise ValueError('A stage must not contain the same ID twice')
        if quota is not None and (type(quota) is not int or not 0 <= quota <= MAX_RECORDS):
            raise ValueError('Stage quota must be null or an integer from 0 through 10000')
    return fixture


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'Duplicate JSON key: {key}')
        value[key] = item
    return value


def _nonfinite(value):
    raise ValueError(f'Non-finite JSON number: {value}')


def load_fixture(path):
    """Read bounded UTF-8 JSON, rejecting duplicate fields and invalid profiles."""
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_FIXTURE_BYTES + 1)
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ValueError(f'Fixture exceeds {MAX_FIXTURE_BYTES} bytes')
    try:
        fixture = json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_nonfinite)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Fixture must be bounded UTF-8 JSON') from exc
    return validate_fixture(fixture)


def unpaged_oracle(fixture):
    """Compute the complete staged selection directly, without paged execution."""
    validate_fixture(fixture)
    positions = {record['id']: (record['sort_key'], record['id']) for record in fixture['records']}
    selected = set()
    result = []
    for stage in fixture['stages']:
        available = sorted((identity for identity in stage['ids'] if identity not in selected),
                           key=positions.__getitem__, reverse=True)
        chosen = available if stage['quota'] is None else available[:stage['quota']]
        result.extend({'stage': stage['name'], 'id': identity} for identity in chosen)
        selected.update(chosen)
    return result
