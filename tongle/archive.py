"""Portable validated backups of SQLite originals/status and editable managed notes."""
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from typing import Any
from .engine import Engine, digest
from .presentation import folder
from .knowledge_files import KnowledgeFiles
from .evolution import enabled

# The 2026-10 memory cutover restore drill measured 96 MiB of authoritative
# data after dropping only derived indexes. Keep a finite import/export bound
# with growth room; do not delete source/audit history to fit the former 100 MiB.
MAX_ARCHIVE = 256 * 1024 * 1024

_MANAGED_NOTE = re.compile(r'[0-9a-f]{32}\.md')
_SAFE_REL = re.compile(r'[^/\\]')


def _too_large(total):
    """Actionable size error: the measured size, not just the bare limit.

    A bare "exceeds 100MB limit" reads as a mystery; the measured total says
    immediately whether the store needs reclaiming or the bound needs a
    deliberate decision.
    """
    return ValueError('backup exceeds the %dMB archive limit: contents would be %.1fMB'
                      % (MAX_ARCHIVE // 1048576, total / 1048576.0))


def _is_safe_rel(rel):
    if not isinstance(rel,str) or not rel or '\\' in rel or '\x00' in rel:return False
    p=PurePosixPath(rel)
    return not p.is_absolute() and str(p)==rel and all(part not in ('.','..') and not part.startswith('.') for part in p.parts)


def _no_symlinks(path):
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('symlink in backup path')


def _collect_vault_files(engine):
    root=Path(engine.db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()[0]);_no_symlinks(root)
    out={}
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root).as_posix()
        if any(part.startswith('.') for part in PurePosixPath(rel).parts):continue
        _no_symlinks(path)
        if path.is_dir():continue
        if not stat.S_ISREG(path.stat().st_mode) or not _is_safe_rel(rel):raise ValueError('unsupported visible vault file')
        out['assets/'+rel]=path
    return out


def _database_snapshot(engine, database):
    """Return a portable copy without the rebuildable raw-source index.

    Only the temporary copy is changed. The explicit list deliberately excludes
    corrections, notes, source versions, processing disposition and audit data.
    Dropping the FTS virtual table also drops its SQLite-owned shadow tables.
    """
    backup = sqlite3.connect(database)
    try:
        engine.db.backup(backup)
        derived = DERIVED_TABLES
        trigger_sql=derived_triggers()
        for name,sql in backup.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall():
            if name not in trigger_sql:continue
            if _normalize_sql(sql)!=_normalize_sql(trigger_sql[name]):
                raise ValueError('unrecognized derived trigger definition: '+name)
            backup.execute('DROP TRIGGER "'+name+'"')
        existing = {row[0] for row in backup.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if existing.intersection(derived):
            for table in derived:
                backup.execute('DROP TABLE IF EXISTS ' + table)
            backup.commit()
            backup.execute('VACUUM')
    finally:
        # The copied database may use WAL. Closing checkpoints the compacted
        # copy before read_bytes; reading just the main file earlier is unsafe.
        backup.close()
    return database.read_bytes()


DERIVED_TABLES = ('source_memory_fts','source_memory_chunks','source_memory_indexed','source_memory_dirty',
                  'knowledge_search_fts','knowledge_search','knowledge_search_dirty','knowledge_vectors',
                  'knowledge_graph_nodes','knowledge_graph_links','knowledge_graph_targets',
                  'knowledge_graph_dirty','knowledge_graph_truncated')
OBSERVATION_TABLES = ('runtime_observations','runtime_daily','runtime_alerts','memory_context_receipts',
                      'runtime_observation_metrics','runtime_reason_daily')


def derived_triggers():
    from .knowledge_index import DERIVED_TRIGGERS as knowledge
    from . import source_memory
    from .graph_retrieval import DERIVED_TRIGGERS as graph
    return {**knowledge,**getattr(source_memory,'DERIVED_TRIGGERS',{}),**graph}


def _normalize_sql(sql):
    return ' '.join(sql.split()).rstrip(';')


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _strip_semicolon(sql: str) -> str:
    sql = _norm(sql)
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def approved_trigger_names(rows: Any, expected: Any) -> list[str]:
    if not isinstance(rows, list) or not isinstance(expected, dict):
        raise ValueError("malformed input")
    if len(rows) > 64 or len(expected) > 32:
        raise ValueError("input too large")

    trusted: dict[str, tuple[str, str]] = {}
    for name, spec in expected.items():
        if not isinstance(name, str) or not isinstance(spec, tuple) or len(spec) != 2:
            raise ValueError("malformed expected")
        table_name, sql = spec
        if not isinstance(table_name, str) or not isinstance(sql, str):
            raise ValueError("malformed expected")
        trusted[name] = (table_name, _strip_semicolon(sql))

    found: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 4:
            raise ValueError("malformed row")
        typ, name, table_name, sql = row
        if not all(isinstance(x, str) for x in (typ, name, table_name, sql)):
            raise ValueError("malformed field")
        if typ != "trigger":
            raise ValueError("unknown executable schema object: %s" % typ)
        if name in found:
            raise ValueError("duplicate trigger: %s" % name)
        found[name] = (table_name, _strip_semicolon(sql))

    for name, spec in found.items():
        if name not in trusted or trusted[name] != spec:
            raise ValueError("unapproved trigger: %s" % name)

    return sorted(found)


def backup_fingerprint_exclusions():
    # FTS shadow tables are exact SQLite-owned derivatives, not an open-ended
    # name prefix that could hide a future authoritative table.
    shadow={name+suffix for name in ('source_memory_fts','knowledge_search_fts')
            for suffix in ('_data','_idx','_content','_docsize','_config')}
    return set(DERIVED_TABLES)|set(OBSERVATION_TABLES)|shadow


def _collect_managed_files(engine):
    """Only the Vault files this engine owns, not every visible Vault file.

    ``knowledge_files`` is the engine's own record of what it published, so an
    archive built from it restores the engine without dragging in ordinary
    notes the user never handed over. That keeps a large personal Vault from
    inflating every daily payload; the trade-off is that a restore no longer
    reproduces unrelated Vault files, which is why the full copy stays the
    default (``knowledge_backup_include_vault``).
    """
    root=Path(engine.db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()[0]);_no_symlinks(root)
    out={}
    for row in engine.db.execute("SELECT path FROM knowledge_files WHERE state='ok' ORDER BY path"):
        rel=row['path']
        if not _is_safe_rel(rel):raise ValueError('unsupported visible vault file')
        path=root/rel
        _no_symlinks(path)
        if not path.is_file():raise ValueError('tracked file missing: '+rel)
        out['assets/'+rel]=path
    return out


def _export_format2(engine, target):
    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('export destination exists')
    KnowledgeFiles(engine).sync()
    cfgpath=engine.home/'config.json';cfg=json.loads(cfgpath.read_text()) if cfgpath.is_file() else {}
    # Default is the full visible Vault: a backup that cannot rebuild the notes
    # is weaker disaster recovery, so opting out must be explicit.
    include_vault = cfg.get('knowledge_backup_include_vault', True) is not False
    with tempfile.TemporaryDirectory(prefix='tongle-export-') as temp:
        database = Path(temp) / 'state.sqlite3'
        with engine.locked():
            contents = {'state.sqlite3': _database_snapshot(engine, database)}
            _add_registry(engine,contents)
            file_map = _collect_vault_files(engine) if include_vault else _collect_managed_files(engine)
            for name, path in file_map.items():
                contents[name] = path.read_bytes()
            for name,path in file_map.items():
                if path.read_bytes()!=contents[name]:raise ValueError('vault changed during backup')
            for row in engine.db.execute('SELECT path,file_hash,state FROM knowledge_files'):
                name='assets/'+row['path']
                if row['state']=='ok' and (name not in contents or digest(contents[name])!=row['file_hash']):raise ValueError('tracked file changed during backup')
        total = sum(map(len, contents.values()))
        if total > MAX_ARCHIVE:
            raise _too_large(total)
        from .lifecycle import portable_settings
        settings=portable_settings(cfg)
        settings.setdefault('knowledge_mode',cfg.get('knowledge_mode','evolving'))
        settings.setdefault('library_excluded_projects',[])
        settings.setdefault('knowledge_project_topics',{})
        settings.setdefault('knowledge_project_names',{})
        from .memory_policy import uses_knowledge_store
        uses_knowledge_store(cfg)
        if 'knowledge_memory_projects' in cfg:settings['knowledge_memory_projects']=cfg['knowledge_memory_projects']
        if not isinstance(settings['library_excluded_projects'],list) or not all(isinstance(x,str) for x in settings['library_excluded_projects']):raise ValueError('invalid scope exclusions')
        manifest = {'format': 2, 'schema': 1,'settings':settings,
                    'local_models_require_rebind':bool(cfg.get('retrieval')),
                    'files': {name: digest(data) for name, data in contents.items()}}
        target.parent.mkdir(parents=True, exist_ok=True)
        tempzip = Path(temp) / 'backup.zip'
        with zipfile.ZipFile(tempzip, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in contents.items():
                z.writestr(name, data)
            z.writestr('manifest.json', json.dumps(manifest))
        with target.open('xb') as f:
            f.write(tempzip.read_bytes())
            f.flush()
            os.fsync(f.fileno())
    return {'exported': str(target), 'files': len(contents),
            'sha256': digest(target.read_bytes()),
            # The UNCOMPRESSED payload size: this is what MAX_ARCHIVE bounds
            # and what a size trend must watch. The written archive is a
            # deflated zip, roughly a third of it, so reporting the file
            # size here would make any percentage-of-limit check fire long
            # after the export had already started failing.
            'content_bytes': total}


def export_archive(engine, target):
    if enabled(engine):
        return _export_format2(engine, target)
    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('export destination exists')
    engine.sync()
    with tempfile.TemporaryDirectory(prefix='tongle-export-') as temp:
        database = Path(temp) / 'state.sqlite3'
        contents = {'state.sqlite3': _database_snapshot(engine, database)}
        _add_registry(engine,contents)
        root = engine.vault()
        for path in root.rglob('*.md'):
            if path.is_symlink():
                raise ValueError('managed note symlink cannot be exported')
            contents['notes/' + path.relative_to(root).as_posix()] = path.read_bytes()
        total = sum(map(len, contents.values()))
        if total > MAX_ARCHIVE:
            raise _too_large(total)
        manifest = {'format': 1, 'schema': 1,
                    'files': {name: digest(data) for name, data in contents.items()}}
        from .memory_policy import read_home_config,uses_knowledge_store
        cfg=read_home_config(engine.home);uses_knowledge_store(cfg)
        from .lifecycle import portable_settings
        manifest['settings']=portable_settings(cfg)
        manifest['settings']['knowledge_mode']='legacy'
        manifest['local_models_require_rebind']=bool(cfg.get('retrieval'))
        target.parent.mkdir(parents=True, exist_ok=True)
        tempzip = Path(temp) / 'backup.zip'
        with zipfile.ZipFile(tempzip, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in contents.items():
                z.writestr(name, data)
            z.writestr('manifest.json', json.dumps(manifest))
        # Exclusive creation, fsync; failure doesn't overwrite any existing backup.
        with target.open('xb') as f:
            f.write(tempzip.read_bytes())
            f.flush()
            os.fsync(f.fileno())
    return {'exported': str(target), 'files': len(contents),
            'sha256': digest(target.read_bytes()),
            # The UNCOMPRESSED payload size: this is what MAX_ARCHIVE bounds
            # and what a size trend must watch. The written archive is a
            # deflated zip, roughly a third of it, so reporting the file
            # size here would make any percentage-of-limit check fire long
            # after the export had already started failing.
            'content_bytes': total}


def _norm_key(s):
    return unicodedata.normalize('NFC', s).casefold()


def _validate_format2_db(db, allowed):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cards={}
    for table in ('knowledge_cards','knowledge_files','knowledge_publications'):
        if table not in tables:continue
        seen={};idcol='id' if table=='knowledge_cards' else 'card_id'
        for rid,path in db.execute('SELECT '+idcol+',path FROM '+table):
            if not isinstance(rid,str) or not re.fullmatch('[0-9a-f]{32}',rid):raise ValueError('invalid knowledge identity')
            if table=='knowledge_cards':cards[rid]=path
            elif rid not in cards:raise ValueError('orphan knowledge binding')
            if path is None and table=='knowledge_cards':continue
            if not _is_safe_rel(path) or not path.endswith('.md'):raise ValueError('unsafe knowledge path')
            key=_norm_key(path)
            if key in seen and seen[key]!=rid:raise ValueError('knowledge path collision')
            seen[key]=rid
    if 'knowledge_files' in tables:
        for path,state in db.execute('SELECT path,state FROM knowledge_files'):
            if state=='ok' and path not in allowed:raise ValueError('published file missing from archive')


def _add_registry(engine, contents):
    from .lifecycle import registered_sources,validate_registry
    if (engine.home/'maintenance/sources.json').exists():
        contents['metadata/sources.json']=json.dumps(validate_registry(registered_sources(engine.home)),ensure_ascii=False).encode()


def _detach_document_paths(database):
    """Turn restored documents into immutable archived originals.

    Called only on the already-validated temporary database. Old locations are
    retained as hints, never used as filesystem bindings on the restored host.
    """
    db=sqlite3.connect(database)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('CREATE TABLE IF NOT EXISTS restored_source_locations('
                   'source_id TEXT PRIMARY KEY,project TEXT NOT NULL,original_path TEXT NOT NULL)')
        rows=db.execute("SELECT id,project,original_path FROM sources WHERE kind='document' AND original_path IS NOT NULL").fetchall()
        if any(not all(isinstance(x,str) for x in row) for row in rows):raise ValueError('invalid document location hint')
        db.executemany('INSERT OR REPLACE INTO restored_source_locations VALUES(?,?,?)',rows)
        db.execute("UPDATE sources SET original_path=NULL WHERE kind='document'")
        db.commit()
        return [{'source_id':sid,'project':project,'original_path':path}
                for sid,project,path in db.execute('SELECT source_id,project,original_path FROM restored_source_locations ORDER BY source_id')]
    finally:db.close()


def _restore_format2(home, archive, vault, content, files, managed,settings,registry=None,local_models=False,source_locations=None):
    _no_symlinks(home);_no_symlinks(vault)
    home.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    try:
        (home / 'state.sqlite3').write_bytes(content['state.sqlite3'])
        for name, data in content.items():
            if not name.startswith('assets/'):
                continue
            rel = name[len('assets/'):]
            path = vault / Path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as f:
                f.write(data)
        (home / 'config.json').write_text(json.dumps(settings))
        engine = Engine(home)
        with engine.transaction() as db:
            db.execute("UPDATE meta SET value=? WHERE key='vault'", (str(vault.resolve()),))
            db.execute("UPDATE jobs SET state='pending', lease_until=0, lease_token=NULL WHERE state='running' AND tongle_source_process(source_id,version)")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_work'").fetchone():
                db.execute("UPDATE knowledge_work SET state='pending', lease_until=0, lease_token=NULL WHERE state='running' AND tongle_work_process(project,inputs)")
        KnowledgeFiles(engine).sync()
        engine.store.close()
    except BaseException:
        raise
    from .lifecycle import restore_metadata
    rebind=restore_metadata(home,registry,local_models,source_locations)
    return {'restored': True, 'home': str(home.resolve()),
            'vault': str(vault.resolve()), 'model_disabled': True,'rebind':rebind}


def restore_archive(home, archive, vault):
    home = Path(home).absolute()
    vault = Path(vault).absolute()
    archive = Path(archive)
    managed = vault / 'tongle'
    if home.is_symlink() or vault.is_symlink() or managed.is_symlink():
        raise ValueError('restore destination symlink')
    if home.exists() and any(home.iterdir()):
        raise ValueError('restore requires clean home')
    if vault.exists() and any(vault.iterdir()):
        raise ValueError('restore requires clean vault')
    if home.resolve() == vault.resolve() or home.resolve() in vault.resolve().parents or vault.resolve() in home.resolve().parents:
        raise ValueError('restore home and vault must be separate directories')
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError('archive exceeds bound')
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError('duplicate/excess archive members')
        if sum(i.file_size for i in infos) > MAX_ARCHIVE:
            raise ValueError('expanded archive exceeds bound')
        for item in infos:
            name = item.filename
            parts = PurePosixPath(name).parts
            if name.startswith('/') or '\\' in name or '..' in parts or not parts or name != str(PurePosixPath(name)):
                raise ValueError('unsafe archive path')
            if stat.S_ISLNK(item.external_attr >> 16) or item.is_dir():
                raise ValueError('unsupported archive entry')
        manifest = json.loads(z.read('manifest.json'))
        if manifest.get('schema') != 1:
            raise ValueError('unsupported backup format')
        fmt = manifest.get('format')
        files = manifest.get('files')
        if not isinstance(files, dict) or set(files) != set(names) - {'manifest.json'} or 'state.sqlite3' not in files:
            raise ValueError('manifest members mismatch')
        if fmt == 1:
            for name in files:
                if name not in ('state.sqlite3','metadata/sources.json') and not (name.startswith('notes/') and name.endswith('.md')):
                    raise ValueError('unknown archive member')
        elif fmt == 2:
            for name in files:
                if name not in ('state.sqlite3','metadata/sources.json') and not (name.startswith('assets/') and _is_safe_rel(name[len('assets/'):])):
                    raise ValueError('unknown archive member')
        else:
            raise ValueError('unsupported backup format')
        if fmt==2 and len({_norm_key(name) for name in files})!=len(files):raise ValueError('archive case collision')
        settings=manifest.get('settings',{'knowledge_mode':'evolving'} if fmt==2 else {})
        from .lifecycle import validate_portable_settings,validate_registry
        validate_portable_settings(settings)
        if fmt==1 and settings:
            from .memory_policy import uses_knowledge_store
            if uses_knowledge_store(settings):
                raise ValueError('invalid legacy backup settings')
        if fmt==2:
            from .memory_policy import uses_knowledge_store
            if not uses_knowledge_store(settings):raise ValueError('invalid restored mode')
            labels=settings.get('knowledge_project_names',{})
            if not isinstance(labels,dict) or len(labels)>1000 or not all(isinstance(k,str) and len(k)<=200 and isinstance(v,str) and len(v)<=200 for k,v in labels.items()):raise ValueError('invalid restored project names')
            topics=settings.get('knowledge_project_topics',{})
            if not isinstance(topics,dict) or len(topics)>1000 or not all(isinstance(k,str) and len(k)<=200 and isinstance(v,list) and len(v)<=50 and all(isinstance(x,str) and len(x)<=200 for x in v) for k,v in topics.items()):raise ValueError('invalid restored topics')
            excluded=settings.get('library_excluded_projects',[])
            if not isinstance(excluded,list) or len(excluded)>1000 or not all(isinstance(x,str) and len(x)<=200 for x in excluded):raise ValueError('invalid restored exclusions')
        content = {name: z.read(name) for name in files}
        if any(digest(data) != files[name] for name, data in content.items()):
            raise ValueError('backup checksum mismatch')
        registry=validate_registry(json.loads(content['metadata/sources.json'])) if 'metadata/sources.json' in content else None
        local_models=manifest.get('local_models_require_rebind',False)
        if type(local_models) is not bool:raise ValueError('invalid local model rebind state')
    # Validate SQLite before creating final directories or copying any note.
    with tempfile.TemporaryDirectory(prefix='tongle-restore-check-') as temp:
        path = Path(temp) / 'state.sqlite3'
        path.write_bytes(content['state.sqlite3'])
        db = sqlite3.connect(path)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('backup database integrity failure')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'meta', 'sources', 'versions', 'events', 'jobs', 'records', 'tombstones'}.issubset(tables):
                raise ValueError('backup schema incomplete')
            from .graph_retrieval import DERIVED_TRIGGERS as graph_triggers, DERIVED_TABLES as graph_tables
            objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('trigger','view')").fetchall()
            approved = approved_trigger_names(objects, {name: ('knowledge_cards', sql) for name, sql in graph_triggers.items()})
            for name in approved:
                db.execute('DROP TRIGGER "'+name+'"')
            # Rebuild the entire graph projection from canonical cards; stale or
            # injected derived rows must never survive an imported archive.
            for name in graph_tables:
                db.execute('DROP TABLE IF EXISTS '+name)
            db.commit()
            name = db.execute("SELECT value FROM meta WHERE key='managed_dir'").fetchone()
            managed = vault / folder(name[0] if name else 'tongle')
            if fmt == 1:
                for rid, note_path in db.execute('SELECT id,note_path FROM records'):
                    if not isinstance(rid, str) or not re.fullmatch(r'[0-9a-f]{32}', rid):
                        raise ValueError('invalid record identity in backup')
                    if note_path and (PurePosixPath(note_path).is_absolute() or '..' in PurePosixPath(note_path).parts or '\\' in note_path):
                        raise ValueError('unsafe note binding in backup')
            else:
                allowed = {n[len('assets/'):] for n in files if n.startswith('assets/')}
                _validate_format2_db(db, allowed)
        finally:
            db.close()
        source_locations=_detach_document_paths(path)
        content['state.sqlite3']=path.read_bytes()
    if fmt == 2:
        return _restore_format2(home, archive, vault, content, files, managed,settings,registry,local_models,source_locations)
    home.mkdir(parents=True, exist_ok=True)
    managed.mkdir(parents=True, exist_ok=True)
    try:
        (home / 'state.sqlite3').write_bytes(content['state.sqlite3'])
        for name, data in content.items():
            if not name.startswith('notes/'):
                continue
            path = managed / Path(name.removeprefix('notes/'))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as f:
                f.write(data)
        if settings:(home/'config.json').write_text(json.dumps(settings))
        engine = Engine(home)
        with engine.transaction():
            engine.db.execute("UPDATE meta SET value=? WHERE key='vault'", (str(vault.resolve()),))
            engine.db.execute("UPDATE jobs SET state='pending',lease_until=0,lease_token=NULL WHERE state='running' AND tongle_source_process(source_id,version)")
        engine.sync()
        engine.store.close()
    except BaseException:
        # Only newly populated clean destinations; preserve contents for diagnosis,
        # never remove arbitrary directories on a partial failure.
        raise
    from .lifecycle import restore_metadata
    return {'restored': True, 'home': str(home.resolve()), 'vault': str(vault.resolve()), 'model_disabled': True,
            'rebind':restore_metadata(home,registry,local_models,source_locations)}
