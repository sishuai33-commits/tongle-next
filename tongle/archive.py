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
from .engine import Engine, digest
from .presentation import folder
from .knowledge_files import KnowledgeFiles
from .evolution import enabled

MAX_ARCHIVE = 100 * 1024 * 1024

_MANAGED_NOTE = re.compile(r'[0-9a-f]{32}\.md')
_SAFE_REL = re.compile(r'[^/\\]')


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


def _export_format2(engine, target):
    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('export destination exists')
    KnowledgeFiles(engine).sync()
    with tempfile.TemporaryDirectory(prefix='tongle-export-') as temp:
        database = Path(temp) / 'state.sqlite3'
        with engine.locked():
            backup = sqlite3.connect(database)
            engine.db.backup(backup)
            backup.close()
            contents = {'state.sqlite3': database.read_bytes()}
            file_map = _collect_vault_files(engine)
            for name, path in file_map.items():
                contents[name] = path.read_bytes()
            for name,path in file_map.items():
                if path.read_bytes()!=contents[name]:raise ValueError('vault changed during backup')
            for row in engine.db.execute('SELECT path,file_hash,state FROM knowledge_files'):
                name='assets/'+row['path']
                if row['state']=='ok' and (name not in contents or digest(contents[name])!=row['file_hash']):raise ValueError('tracked file changed during backup')
        if sum(map(len, contents.values())) > MAX_ARCHIVE:
            raise ValueError('backup exceeds first-version 100MB limit')
        cfgpath=engine.home/'config.json';cfg=json.loads(cfgpath.read_text(encoding='utf-8')) if cfgpath.is_file() else {}
        settings={'knowledge_mode':'evolving','library_excluded_projects':cfg.get('library_excluded_projects',[]),'knowledge_project_topics':cfg.get('knowledge_project_topics',{}),'knowledge_project_names':cfg.get('knowledge_project_names',{})}
        if not isinstance(settings['library_excluded_projects'],list) or not all(isinstance(x,str) for x in settings['library_excluded_projects']):raise ValueError('invalid scope exclusions')
        manifest = {'format': 2, 'schema': 1,'settings':settings,
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
    return {'exported': str(target), 'files': len(contents), 'sha256': digest(target.read_bytes())}


def export_archive(engine, target):
    if enabled(engine):
        return _export_format2(engine, target)
    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('export destination exists')
    engine.sync()
    with tempfile.TemporaryDirectory(prefix='tongle-export-') as temp:
        database = Path(temp) / 'state.sqlite3'
        backup = sqlite3.connect(database)
        engine.db.backup(backup)
        backup.close()
        contents = {'state.sqlite3': database.read_bytes()}
        root = engine.vault()
        for path in root.rglob('*.md'):
            if path.is_symlink():
                raise ValueError('managed note symlink cannot be exported')
            contents['notes/' + str(path.relative_to(root))] = path.read_bytes()
        if sum(map(len, contents.values())) > MAX_ARCHIVE:
            raise ValueError('backup exceeds first-version 100MB limit')
        manifest = {'format': 1, 'schema': 1,
                    'files': {name: digest(data) for name, data in contents.items()}}
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
    return {'exported': str(target), 'files': len(contents), 'sha256': digest(target.read_bytes())}


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


def _restore_format2(home, archive, vault, content, files, managed,settings):
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
        (home / 'config.json').write_text(json.dumps(settings),encoding='utf-8')
        engine = Engine(home)
        with engine.transaction() as db:
            db.execute("UPDATE meta SET value=? WHERE key='vault'", (str(vault.resolve()),))
            db.execute("UPDATE jobs SET state='pending', lease_until=0, lease_token=NULL WHERE state='running'")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_work'").fetchone():
                db.execute("UPDATE knowledge_work SET state='pending', lease_until=0, lease_token=NULL WHERE state='running'")
        KnowledgeFiles(engine).sync()
        engine.store.close()
    except BaseException:
        raise
    return {'restored': True, 'home': str(home.resolve()),
            'vault': str(vault.resolve()), 'model_disabled': True}


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
                if name != 'state.sqlite3' and not (name.startswith('notes/') and name.endswith('.md')):
                    raise ValueError('unknown archive member')
        elif fmt == 2:
            for name in files:
                if name != 'state.sqlite3' and not (name.startswith('assets/') and _is_safe_rel(name[len('assets/'):])):
                    raise ValueError('unknown archive member')
        else:
            raise ValueError('unsupported backup format')
        if fmt==2 and len({_norm_key(name) for name in files})!=len(files):raise ValueError('archive case collision')
        settings=manifest.get('settings',{'knowledge_mode':'evolving'})
        if fmt==2:
            if not isinstance(settings,dict) or set(settings)-{'knowledge_mode','library_excluded_projects','knowledge_project_topics','knowledge_project_names'}:raise ValueError('unsafe backup settings')
            if settings.get('knowledge_mode')!='evolving':raise ValueError('invalid restored mode')
            labels=settings.get('knowledge_project_names',{})
            if not isinstance(labels,dict) or len(labels)>1000 or not all(isinstance(k,str) and len(k)<=200 and isinstance(v,str) and len(v)<=200 for k,v in labels.items()):raise ValueError('invalid restored project names')
            topics=settings.get('knowledge_project_topics',{})
            if not isinstance(topics,dict) or len(topics)>1000 or not all(isinstance(k,str) and len(k)<=200 and isinstance(v,list) and len(v)<=50 and all(isinstance(x,str) and len(x)<=200 for x in v) for k,v in topics.items()):raise ValueError('invalid restored topics')
            excluded=settings.get('library_excluded_projects',[])
            if not isinstance(excluded,list) or len(excluded)>1000 or not all(isinstance(x,str) and len(x)<=200 for x in excluded):raise ValueError('invalid restored exclusions')
        content = {name: z.read(name) for name in files}
        if any(digest(data) != files[name] for name, data in content.items()):
            raise ValueError('backup checksum mismatch')
    # Validate SQLite before creating final directories or copying any note.
    with tempfile.TemporaryDirectory(prefix='tongle-restore-check-') as temp:
        path = Path(temp) / 'state.sqlite3'
        path.write_bytes(content['state.sqlite3'])
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('backup database integrity failure')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'meta', 'sources', 'versions', 'events', 'jobs', 'records', 'tombstones'}.issubset(tables):
                raise ValueError('backup schema incomplete')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
                raise ValueError('executable schema objects rejected')
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
    if fmt == 2:
        return _restore_format2(home, archive, vault, content, files, managed,settings)
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
        engine = Engine(home)
        with engine.transaction():
            engine.db.execute("UPDATE meta SET value=? WHERE key='vault'", (str(vault.resolve()),))
            engine.db.execute("UPDATE jobs SET state='pending',lease_until=0,lease_token=NULL WHERE state='running'")
        engine.sync()
        engine.store.close()
    except BaseException:
        # Only newly populated clean destinations; preserve contents for diagnosis,
        # never remove arbitrary directories on a partial failure.
        raise
    return {'restored': True, 'home': str(home.resolve()), 'vault': str(vault.resolve()), 'model_disabled': True}
