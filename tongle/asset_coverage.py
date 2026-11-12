"""Read-only coverage of an explicit, bounded path list. Never scans a Vault."""
import hashlib
from pathlib import Path
import sqlite3
from contextlib import closing


def inspect_assets(home, project, paths):
    from .lifecycle import safe_path, registered_sources, validate_registry
    from .source_acquisition import _text
    from .store import MAX_ORIGINAL_BYTES, TEXT_EXTENSIONS
    _text(project, 'project', 200)
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 64:
        raise ValueError('coverage requires between 1 and 64 explicit paths')
    home = safe_path(home)
    targets = [safe_path(p) for p in paths]
    result = {'schema_version': 1, 'project': project, 'scope': 'explicit_paths',
              'read_only': True, 'assets': [], 'warnings': [], 'database_state': 'unknown'}
    try:
        registry = validate_registry(registered_sources(home))['sources']
    except (OSError, ValueError) as exc:
        registry = []
        result['warnings'].append('registry_unavailable:' + type(exc).__name__)
    dbpath = safe_path(home / 'state.sqlite3')
    if not dbpath.is_file():
        result['database_state'] = 'unavailable'
        return _assets(None, registry, project, targets, result, MAX_ORIGINAL_BYTES, TEXT_EXTENSIONS)
    try:
        # immutable avoids SQLite side files when the database has no WAL. A
        # live WAL must be read normally; ignoring it could report stale state.
        uri = dbpath.as_uri() + '?mode=ro'
        if not dbpath.with_name(dbpath.name + '-wal').exists():
            uri += '&immutable=1'
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            result['database_state'] = 'available'
            return _assets(db, registry, project, targets, result, MAX_ORIGINAL_BYTES, TEXT_EXTENSIONS)
    except sqlite3.Error as exc:
        result['database_state'] = 'error'
        result['warnings'].append('database_unavailable:' + type(exc).__name__)
        result['assets'] = []
        return _assets(None, registry, project, targets, result, MAX_ORIGINAL_BYTES, TEXT_EXTENSIONS)


def _assets(db, registry, project, targets, result, max_bytes, extensions):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} if db else set()
    vault = None
    if 'meta' in tables:
        row = db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()
        vault = Path(row[0]) if row else None
    for path in targets:
        entry = {'path': str(path), 'file_state': 'missing', 'source_state': 'unknown',
                 'card_state': 'unknown', 'sources': [], 'cards': [],
                 'format_supported': path.suffix.lower() in extensions | {'.docx'}}
        digest = None
        content = None
        try:
            if path.is_file():
                entry['file_state'] = 'present'
                entry['bytes'] = path.stat().st_size
                if entry['bytes'] <= max_bytes:
                    with path.open('rb') as stream:
                        content = stream.read(max_bytes + 1)
                    if len(content) <= max_bytes:
                        digest = hashlib.sha256(content).hexdigest()
                    else:
                        entry['file_state'] = 'changed_during_read'
                        content = None
                else:
                    entry['file_state'] = 'over_read_bound'
            elif path.exists():
                entry['file_state'] = 'not_regular_file'
        except OSError:
            entry['file_state'] = 'unreadable'
        registration = [r for r in registry if r['project'] == project and r['path'] == str(path)]
        entry['registered_for_maintenance'] = bool(registration)
        if {'sources', 'versions'} <= tables:
            rows = db.execute('SELECT s.id,s.current_version,s.original_path,v.hash FROM sources s '
                              'JOIN versions v ON v.source_id=s.id AND v.version=s.current_version '
                              'WHERE s.project=? AND s.original_path=?', (project, str(path))).fetchall()
            by_id = {r['id']: dict(r) for r in rows}
            if 'source_acquisitions' in tables:
                for row in db.execute('SELECT DISTINCT s.id,s.current_version,s.original_path,v.hash '
                                      'FROM source_acquisitions a JOIN sources s ON s.id=a.source_id '
                                      'JOIN versions v ON v.source_id=s.id AND v.version=s.current_version '
                                      'WHERE a.project=? AND a.input_path=? AND s.project=?', (project, str(path), project)):
                    by_id[row['id']] = dict(row)
            entry['source_state'] = 'registered' if by_id else ('registered_not_ingested' if registration else 'not_registered_in_project')
            for row in by_id.values():
                sid, version = row['id'], row['current_version']
                snapshot = row['original_path'] != str(path)
                status = 'unknown'
                if 'source_memory_indexed' in tables:
                    indexed = db.execute('SELECT text_hash FROM source_memory_indexed WHERE source_id=? AND version=?', (sid, version)).fetchone()
                    status = 'ready' if indexed and indexed[0] == row['hash'] else 'pending'
                    if 'source_memory_dirty' in tables and db.execute('SELECT 1 FROM source_memory_dirty WHERE source_id=? AND version=?', (sid, version)).fetchone():
                        status = 'pending'
                availability = 'unknown'
                if 'source_status' in tables:
                    state = db.execute('SELECT state FROM source_status WHERE source_id=?', (sid,)).fetchone()
                    availability = state[0] if state else 'not_recorded'
                entry['sources'].append({'id': sid, 'version': version, 'index_state': status,
                                         'availability': availability,
                                         'storage': 'acquisition_snapshot' if snapshot else 'registered_file',
                                         'input_matches_version': digest == row['hash'] if digest else None})
        if 'knowledge_cards' in tables:
            entry['card_state'] = 'not_managed_in_project'
            candidates = [str(path)]
            if vault:
                try:
                    candidates.append(path.relative_to(vault).as_posix())
                except ValueError:
                    pass
            rows = db.execute('SELECT id,version,state,path FROM knowledge_cards WHERE project=? AND path IN (' + ','.join('?' for _ in candidates) + ')', [project, *candidates]).fetchall()
            if not rows and content is not None and path.suffix.lower() == '.md':
                from .knowledge_files import _parse_wrapper
                marker = _parse_wrapper(content.decode('utf-8', errors='replace'))
                if marker:
                    rows = db.execute('SELECT id,version,state,path FROM knowledge_cards WHERE project=? AND id=?', (project, marker[0])).fetchall()
            for row in rows:
                location_matches = row['path'] in candidates
                idx = 'unknown'
                if 'knowledge_search' in tables:
                    found = db.execute('SELECT version FROM knowledge_search WHERE card_id=?', (row['id'],)).fetchone()
                    idx = 'ready' if found and found[0] == row['version'] else 'pending'
                    if 'knowledge_search_dirty' in tables and db.execute('SELECT 1 FROM knowledge_search_dirty WHERE card_id=?', (row['id'],)).fetchone():
                        idx = 'pending'
                file_state = 'unknown'
                if 'knowledge_files' in tables:
                    tracked = db.execute('SELECT file_hash,version FROM knowledge_files WHERE card_id=?', (row['id'],)).fetchone()
                    if tracked and digest:
                        file_state = 'unchanged' if tracked['file_hash'] == digest and tracked['version'] == row['version'] else 'needs_verification'
                entry['cards'].append({'id': row['id'], 'version': row['version'], 'state': row['state'],
                                       'index_state': idx, 'file_state': file_state,
                                       'location_state': 'known' if location_matches else 'pending_location_verification'})
            if rows:
                entry['card_state'] = 'managed' if all(r['path'] in candidates for r in rows) else 'identity_marker_needs_verification'
        result['assets'].append(entry)
    return result
