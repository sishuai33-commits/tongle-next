"""Explicit, bounded reading receipts; no fetching, inference or directory scan.

Receipts describe what the caller read, not proof of authentic/full coverage.
The byte version and the acquisition identity deliberately have separate lives.
"""
import hashlib
import json
import os
from datetime import datetime
from urllib.parse import urlsplit


def _text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ValueError(name + ' must be bounded nonempty text')
    return value


def validate_manifest(value):
    required = {'acquisition_id', 'captured_at', 'method', 'complete', 'coverage'}
    optional = {'origin_url', 'original_ref', 'representation', 'limitations'}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError('invalid acquisition manifest fields')
    result = dict(value)
    for key in ('acquisition_id', 'captured_at', 'method'):
        _text(result[key], key, 200)
    _text(result['coverage'], 'coverage', 2000)
    if type(result['complete']) is not bool:
        raise ValueError('complete must be boolean')
    try:
        at = datetime.fromisoformat(result['captured_at'].replace('Z', '+00:00'))
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError()
    except ValueError:
        raise ValueError('captured_at must be an ISO timestamp with timezone') from None
    for key in ('original_ref', 'representation'):
        if key in result:
            _text(result[key], key, 2000)
    if 'origin_url' in result:
        _text(result['origin_url'], 'origin_url', 4096)
        url = urlsplit(result['origin_url'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise ValueError('origin_url must be an absolute HTTP(S) URL without credentials')
    if 'limitations' in result:
        limits = result['limitations']
        if not isinstance(limits, list) or len(limits) > 20:
            raise ValueError('limitations must be a list of at most 20 entries')
        for limit in limits:
            _text(limit, 'limitation', 500)
    if len(json.dumps(result, ensure_ascii=False)) > 16000:
        raise ValueError('acquisition manifest exceeds bound')
    return result


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS source_acquisitions (
        project TEXT NOT NULL, acquisition_id TEXT NOT NULL,
        source_id TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
        input_path TEXT NOT NULL, manifest TEXT NOT NULL, fingerprint TEXT NOT NULL,
        complete INTEGER NOT NULL, created_version INTEGER NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY(project, acquisition_id),
        FOREIGN KEY(source_id,version) REFERENCES versions(source_id,version))''')
    db.execute('CREATE INDEX IF NOT EXISTS source_acquisitions_source ON source_acquisitions(source_id,version)')


def prepare(db, project, source_id, digest, manifest, input_path, current):
    """Inside the same write transaction as ingest. Return a prior receipt or None."""
    _schema(db)
    payload = json.dumps([project, source_id, digest, manifest, input_path], ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    previous = db.execute('SELECT * FROM source_acquisitions WHERE project=? AND acquisition_id=?',
                          (project, manifest['acquisition_id'])).fetchone()
    if previous:
        if previous['fingerprint'] != fingerprint:
            raise ValueError('acquisition identity reused with different content or metadata')
        return {'source_id': source_id, 'version': previous['version'],
                'acquisition_id': manifest['acquisition_id'], 'replayed': True}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_status'").fetchone():
        state = db.execute('SELECT state FROM source_status WHERE source_id=?', (source_id,)).fetchone()
        if state and state[0] == 'revoked':
            raise ValueError('revoked source cannot be acquired again')
    if current and current['hash'] != digest and not manifest['complete']:
        receipts = db.execute('SELECT complete,created_version,manifest FROM source_acquisitions '
                              'WHERE source_id=? AND version=?', (source_id, current['current_version'])).fetchall()
        origins = [r for r in receipts if r['created_version']]
        if not origins or any(r['complete'] for r in receipts):
            raise ValueError('partial acquisition cannot replace complete or unknown coverage; use a separate scoped source-id')
        if any(json.loads(r['manifest'])['coverage'] != manifest['coverage'] for r in origins):
            raise ValueError('partial coverage changed; use a separate scoped source-id')
    return None


def record(db, project, source_id, digest, manifest, input_path, result, created_version, now):
    payload = json.dumps([project, source_id, digest, manifest, input_path], ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
    db.execute('INSERT INTO source_acquisitions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (project, manifest['acquisition_id'], source_id, result['version'], digest,
                input_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                hashlib.sha256(payload.encode()).hexdigest(), int(manifest['complete']),
                int(created_version), now))
    return dict(result, acquisition_id=manifest['acquisition_id'], replayed=False)


def acquisition_provenance(db, project, source_id, version):
    """Read-only, version-bound declarations for source/search consumers.

    The latest receipt describes a particular reading; it cannot downgrade an
    unchanged byte version that an earlier receipt declared complete. Neither
    declaration is an independent coverage/semantic verification.
    """
    if type(version) is not int or version < 1:
        raise ValueError('version must be a positive integer')
    absent = {'state': 'not_recorded', 'coverage_assessment': 'unknown'}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_acquisitions'").fetchone():
        return absent
    # No schema initialization, row_factory assumption or cross-project lookup.
    row = db.execute('SELECT a.manifest FROM source_acquisitions a JOIN sources s ON s.id=a.source_id '
                     'WHERE a.project=? AND s.project=? AND a.source_id=? AND a.version=? '
                     'ORDER BY a.rowid DESC LIMIT 1', (project, project, source_id, version)).fetchone()
    if not row:
        return absent
    aggregate = db.execute('SELECT max(complete),max(created_version) FROM source_acquisitions '
                           'WHERE project=? AND source_id=? AND version=?', (project, source_id, version)).fetchone()
    coverage = 'complete_declared' if aggregate[0] else ('partial_declared' if aggregate[1] else 'unknown')
    return {'state': 'recorded', 'coverage_assessment': 'caller_declared',
            'version_coverage': coverage, 'latest_reading': json.loads(row[0])}


def acquire(home, project, path, source_id, manifest, write=False):
    """Freeze a caller-supplied reading result and ingest it with its receipt.

    This intentionally does not register a mutable input file for maintenance.
    Future acquisitions explicitly supply their own complete/partial declaration.
    """
    from .lifecycle import safe_path, locked, registered_sources, validate_registry
    from .store import Store, MAX_ORIGINAL_BYTES, TEXT_EXTENSIONS, _decode_text, _docx_text
    home, path = safe_path(home), safe_path(path)
    _text(project, 'project', 200)
    _text(source_id, 'source_id', 200)
    manifest = validate_manifest(manifest)
    if not path.is_file() or path.stat().st_size > MAX_ORIGINAL_BYTES:
        raise ValueError('acquisition requires an existing regular file within 5MB')
    if path.suffix.lower() not in TEXT_EXTENSIONS | {'.docx'}:
        raise ValueError('unsupported acquisition representation; extract text first')
    with path.open('rb') as stream:
        original = stream.read(MAX_ORIGINAL_BYTES + 1)
    if len(original) > MAX_ORIGINAL_BYTES:
        raise ValueError('acquisition file grew beyond 5MB')
    # Reject malformed input before creating a snapshot or touching the database.
    if path.suffix.lower() == '.docx':
        _docx_text(original)
    else:
        _decode_text(original)
    digest = hashlib.sha256(original).hexdigest()
    target = safe_path(home / 'acquisition-payloads' / (digest + path.suffix.lower()))
    plan = {'source_id': source_id, 'content_hash': digest, 'input_path': str(path),
            'snapshot_path': str(target), 'acquisition': manifest, 'dry_run': not write,
            'coverage_assessment': 'caller_declared', 'mutable_file_registration': False}
    if not write:
        return plan
    if not (home / 'state.sqlite3').is_file():
        raise ValueError('initialize the instance before acquiring a source')
    with locked(home):
        registry = validate_registry(registered_sources(home))['sources']
        if any(r['project'] == project and r['source_id'] == source_id for r in registry):
            raise ValueError('source is registered for mutable file tracking; acquire under a separate explicit source-id')
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if target.stat().st_size != len(original) or target.read_bytes() != original:
                raise ValueError('immutable acquisition snapshot changed')
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        store = Store(home)
        try:
            receipt = store.ingest(project, target, source_id, acquisition=manifest, acquisition_path=str(path), expected_hash=digest)
        finally:
            store.close()
        return dict(plan, ingest=receipt)
