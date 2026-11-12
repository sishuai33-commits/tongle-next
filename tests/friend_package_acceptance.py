#!/usr/bin/env python3
"""Run the actual download bundle with a fresh profile and no Python on PATH.

The test uses synthetic Markdown, an isolated local instance, the bundled
Python, and the shipped entry point. It does not log in to an AI host or
simulate evidence of AI host activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


WINDOWS = os.name == 'nt'
LAUNCHER = 'Start Tongle.cmd' if WINDOWS else 'Start Tongle.command'
QUERY = '白鹭路线交接'
FACT = '白鹭路线交接定在周四下午三点，集合点是梧桐门。'


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unpack(archive, target):
    with zipfile.ZipFile(archive) as zipped:
        names = set()
        for entry in zipped.infolist():
            path = PurePosixPath(entry.filename)
            key = path.as_posix().casefold()
            check(not path.is_absolute() and '..' not in path.parts
                  and '\\' not in entry.filename and ':' not in entry.filename
                  and key not in names, 'unsafe or duplicate member in download')
            names.add(key)
        if WINDOWS:
            zipped.extractall(target)
    if not WINDOWS:
        # macOS Archive Utility and ditto retain Unix launcher modes and
        # bundled Python symlinks, which zipfile.extractall does not retain.
        subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(target)],
                       check=True, timeout=120)


def fresh_environment(profile):
    profile.mkdir()
    env = {
        'HOME': str(profile),
        'USERPROFILE': str(profile),
        'TMPDIR': str(profile),
        'TEMP': str(profile),
        'TMP': str(profile),
        'PYTHONUTF8': '1',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    if WINDOWS:
        system = Path(os.environ['SystemRoot'])
        for key in ('APPDATA', 'LOCALAPPDATA'):
            directory = profile/key
            directory.mkdir()
            env[key] = str(directory)
        env.update(SystemRoot=str(system), WINDIR=str(system),
                   ComSpec=str(system/'System32'/'cmd.exe'),
                   PATH=str(system/'System32'), PATHEXT='.EXE;.COM;.BAT;.CMD')
    else:
        env['PATH'] = '/bin:/sbin:/usr/sbin'
    check(not any(shutil.which(name, path=env['PATH']) for name in ('python', 'python3', 'pi')),
          'acceptance PATH exposes a preinstalled Python or Pi')
    return env


def launch_command(launcher, args, env):
    if not WINDOWS:
        return [str(launcher), *args]
    # Invoke the cmd file exactly as a normal command-shell user would.
    # /S strips the outer pair while retaining the quotes on the path.
    shell = subprocess.list2cmdline([env['ComSpec'], '/D', '/S', '/C'])
    inner = subprocess.list2cmdline([str(launcher), *args])
    return shell + ' "' + inner + '"'


def invoke(argv, *, env, cwd, label, input_text=None, json_output=True):
    result = subprocess.run(argv, cwd=cwd, env=env, input=input_text,
                            capture_output=True, encoding='utf-8', timeout=90)
    check(result.returncode == 0,
          f'{label} exited {result.returncode}: {(result.stderr or result.stdout)[-2500:]}')
    if not json_output:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f'{label} returned invalid JSON: {result.stdout[-1500:]}') from exc


def assert_fact(result, source_id):
    check(result.get('ok') and result.get('first_source', {}).get('source_id') == source_id
          and FACT in result['first_source'].get('text', ''), 'search did not read the original source')


def test_bundle(archive, evidence):
    with tempfile.TemporaryDirectory(prefix='tongle-clean-') as temp:
        root = Path(temp).resolve()
        extract = root/'download'
        unpack(archive, extract)
        entries = list(extract.rglob(LAUNCHER))
        check(len(entries) == 1, 'download must contain exactly one startup launcher')
        relocated = root/'移动后 Tongle'
        entries[0].parent.rename(relocated)
        launcher = relocated/LAUNCHER
        runtime = relocated/('runtime/python.exe' if WINDOWS else 'runtime/bin/python3')
        check(runtime.is_file(), 'bundled Python executable is missing')
        env = fresh_environment(root/'干净账户')
        outside = root/'启动位置'
        outside.mkdir()
        data = (Path(env['LOCALAPPDATA'])/'Tongle' if WINDOWS
                else Path(env['HOME'])/'Library'/'Application Support'/'Tongle')
        home, vault = data/'instance', data/'vault'

        def guide(label, *args, directory=data):
            command = launch_command(launcher, ['--data-dir', str(directory), *args, '--json'], env)
            return invoke(command, env=env, cwd=outside, label=label)

        def core(label, command, *args, instance=home):
            return invoke([str(runtime), '-m', 'tongle', '--home', str(instance), command, *args],
                          env=env, cwd=relocated, label=label)

        def maintain(command):
            return invoke([str(runtime), str(relocated/'scripts/maintain.py'), '--home', str(home), command],
                          env=env, cwd=outside, label='maintenance '+command)

        runtime_info = invoke(
            [str(runtime), '-c',
             'import json,platform,sqlite3,sys; print(json.dumps(dict(executable=sys.executable,'
             'python=platform.python_version(),sqlite=sqlite3.sqlite_version)))'],
            env=env, cwd=outside, label='bundled Python')
        check(Path(runtime_info['executable']).resolve() == runtime.resolve(),
              'starter did not use the bundled runtime')
        evidence['runtime'] = {k: runtime_info[k] for k in ('python', 'sqlite')}

        source = outside/'白鹭 交接.md'
        source.write_text(f'# {QUERY}\n\n{FACT}\n', encoding='utf-8')
        check(not data.exists(), 'default data directory is not fresh')
        transcript = invoke(
            launch_command(launcher, [], env), env=env, cwd=outside,
            label='interactive first launch with the default data directory',
            input_text=f'\n1\n2\n{source}\n3\n{QUERY}\n0\n', json_output=False)
        check('示例已通过导入、检索和原文核对。' in transcript
              and '资料已导入' in transcript and FACT in transcript,
              'the interactive guide did not complete the sample, import and original read')
        evidence['checks'].append('interactive_default_directory_sample_import_search')

        first = guide('read sample from existing installation', '--demo')
        check(first.get('ok') and Path(first['home']) == home and Path(first['vault']) == vault
              and first.get('created') is False and first.get('demo', {}).get('match_verified'),
              'first-run guided sample did not persist')
        launch_binding = json.loads((home/'launcher.json').read_text(encoding='utf-8'))
        check(Path(launch_binding['root']).resolve() == relocated
              and Path(launch_binding['python']).resolve() == runtime.resolve(),
              'created instance is not bound to relocated bundle')
        evidence['checks'].append('relocated_launcher_first_run_demo')

        loaded = guide('reimport identical user material', '--import', str(source))
        source_id = loaded.get('imported', {}).get('source_id')
        check(loaded.get('ok') and source_id and loaded['imported']['version'] == 1,
              'import did not persist a versioned source')
        project = first['project']
        check(project != 'tongle-demo', 'daily project and starter demo share a scope')
        assert_fact(guide('search after restart', '--search', QUERY), source_id)
        status = guide('read existing installation status', '--status')
        check(status.get('ok') and status.get('enabled') and status.get('created') is False,
              'restart did not reuse the installed data')
        evidence['checks'].extend(['user_markdown_import', 'new_process_search_and_original_read',
                                   'restart_preserves_instance'])

        cycle = maintain('once')
        check(cycle.get('ok') and not cycle.get('errors'), 'maintenance cycle reported errors')
        backup = json.loads((home/'maintenance/backup.json').read_text(encoding='utf-8'))
        check(backup.get('restore_verification', {}).get('method') == 'isolated_restore_v1'
              and Path(backup['path']).is_file(), 'maintenance did not verify a restorable backup')
        evidence['checks'].append('maintenance_cycle_and_backup_restore_gate')

        exported = root/'白鹭 backup.zip'
        check(core('export', 'export', '--to', str(exported)).get('exported') and exported.is_file(),
              'user backup export failed')
        restored = root/'恢复到新目录'
        restore_home, restore_vault = restored/'instance', restored/'vault'
        result = core('restore', 'restore', '--from', str(exported), '--vault', str(restore_vault),
                      instance=restore_home)
        check(result.get('restored'), 'isolated restore failed')
        original = invoke(
            [str(runtime), '-m', 'tongle', '--home', str(restore_home), 'memory',
             '--project', project, '--session', 'release-acceptance', '--action', 'source'],
            env=env, cwd=relocated, label='read restored original',
            input_text=json.dumps({'id': source_id, 'version': 1}))
        check(original.get('source_id') == source_id and FACT in original.get('text', ''),
              'restored source does not retain its exact content and identity')
        check(guide('original installation remains enabled', '--status').get('enabled'),
              'restore changed the original installation')
        evidence['checks'].append('export_restore_preserves_source_and_original_instance')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    options = parser.parse_args()
    archive = options.zip.resolve()
    evidence = {
        'ok': False,
        'platform': platform.platform(),
        'machine': platform.machine(),
        'archive_sha256': sha256(archive),
        'scope': 'fresh profile, no external runtime on PATH, native bundled CLI, synthetic Markdown',
        'native_ai_host': 'not_exercised',
        'checks': [],
    }
    try:
        test_bundle(archive, evidence)
        evidence['ok'] = True
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        evidence['error'] = str(exc)
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False))
    return 0 if evidence['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
