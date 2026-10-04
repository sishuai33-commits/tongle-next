"""Product entry: setup/start/stop/status/once/add/context/backup/restore/connect.

Thin bounded wrapper over the existing core (python -m tongle) and the existing
scripts/maintain.py. No engine/evolution/schema/adapter rewrites.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from tongle import guide
from tongle.portability import lock_file, unlock_file

ROOT = Path(__file__).resolve().parents[1]
ENV_NAME = re.compile(r'^[A-Z_][A-Z0-9_]*$')
CONTROLS = re.compile(r'[\x00-\x1f\x7f]')
STYLES = ('product', 'consulting', 'research')
PROVIDERS = ('deepseek', 'openai')
DEFAULT_HOME = '~/.local/share/tongle'
MODEL_TIMEOUT_SECONDS = 200
CORE_TIMEOUT = 200
MAINTAIN_TIMEOUT = 200
MAX_SOURCE_BYTES = 5 * 1024 * 1024

STYLE_SECTIONS = {
    'product': ['结论', '依据', '适用范围', '验收'],
    'consulting': ['背景', '证据', '备选', '风险', '建议'],
    'research': ['问题', '方法', '证据强度', '不确定性', '反例'],
}

STYLE_GUIDES = {
    'product': {
        '结论': '用一句话说明面向哪类用户做出什么选择；不写口号，不写无依据的承诺。',
        '依据': '只引用已登记来源中的事实、数据或需求；标出来源，不把偏好当证据。',
        '适用范围': '写明适用场景、版本与前提；明确不适用的场景和已知边界。',
        '验收': '给出可检验的验收条件与判定方法；每条都能被他人复现。',
    },
    'consulting': {
        '背景': '描述客户处境、目标与约束；区分已知事实与尚未验证的假设。',
        '证据': '列出支撑判断的可核查材料；注明来源可靠性与口径，不夸大。',
        '备选': '给出可行备选方案及各自取舍；说明为何纳入或排除。',
        '风险': '列出主要风险、触发条件与影响；不回避不确定性。',
        '建议': '给出推荐动作、责任方与时间点；与证据和风险保持一致。',
    },
    'research': {
        '问题': '把研究问题写成可检验的形式；界定变量、范围与假设。',
        '方法': '说明方法、数据与操作步骤；让读者能复现关键环节。',
        '证据强度': '评估证据质量与来源等级；指出结论可支撑到什么程度。',
        '不确定性': '显式给出未知、误差与替代解释；不做超出证据的推断。',
        '反例': '给出现有反例或不一致结果；说明它们对结论的影响。',
    },
}

DEFAULT_CONFIG = {
    'knowledge_mode': 'evolving',
    'knowledge_quiet_seconds': 300,
    'knowledge_daily_call_budget': 40,
    'knowledge_daily_token_budget': 300000,
    'knowledge_max_output_tokens': 4096,
    'model_timeout_seconds': MODEL_TIMEOUT_SECONDS,
    'model_command': [],
    'model_name': 'offline',
}


class ProductError(Exception):
    """Bounded, already-sanitized failure message for CLI output."""


def resolve_home(value):
    if value is None:
        value = DEFAULT_HOME
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    _reject_if_symlink(raw, 'home', ancestors=True)
    try:
        return raw.resolve()
    except (OSError, RuntimeError):
        raise ProductError('home path could not be resolved')


def resolve_path(value, label='path'):
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    _reject_if_symlink(raw, label, ancestors=True)
    try:
        return raw.resolve()
    except (OSError, RuntimeError):
        raise ProductError(label + ' path could not be resolved')


def _reject_if_symlink(path, label, ancestors=False):
    if path.is_symlink():
        raise ProductError('refusing symlink ' + label)
    if ancestors:
        probe = path.parent
        seen = 0
        while probe != probe.parent and seen < 64:
            if probe.is_symlink():
                raise ProductError('refusing symlink in ' + label + ' ancestors')
            probe = probe.parent
            seen += 1


def _check_endpoint(endpoint):
    if not isinstance(endpoint, str) or not endpoint:
        raise ProductError('endpoint is required')
    if CONTROLS.search(endpoint) or re.search(r'\s', endpoint):
        raise ProductError('endpoint contains whitespace or control characters')
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ProductError('endpoint is not a valid URL')
    if parts.scheme != 'https':
        raise ProductError('endpoint must be an https URL')
    if parts.username or parts.password or '@' in (parts.netloc or ''):
        raise ProductError('endpoint must not carry userinfo')
    if parts.query or parts.fragment:
        raise ProductError('endpoint must not carry query or fragment')
    if port is not None and not (0 < port < 65536):
        raise ProductError('endpoint port is out of range')
    host = (parts.hostname or '').lower()
    if not host:
        raise ProductError('endpoint must carry a host')
    if host in ('localhost', '127.0.0.1', '::1') or host.endswith('.localhost'):
        raise ProductError('local endpoint is not allowed for the product entry')
    if not parts.path or not parts.path.endswith('/chat/completions'):
        raise ProductError('endpoint path must end with /chat/completions')
    return endpoint


def _clean_text(value, label, required=False, allow_slash=False):
    if value is None:
        if required:
            raise ProductError(label + ' is required')
        return None
    if not isinstance(value, str) or CONTROLS.search(value):
        raise ProductError(label + ' must be a plain text value')
    text = value.strip()
    if required and not text:
        raise ProductError(label + ' must be nonempty')
    return text


def _atomic_json(path, value):
    if path.is_symlink():
        raise ProductError('refusing to write through symlink ' + path.name)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with tmp.open('x', encoding='utf8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        os.chmod(tmp, path.stat().st_mode)
    os.replace(tmp, path)


def _read_json(path, default=None, strict=False):
    if path.is_symlink():
        raise ProductError('refusing symlink json: ' + path.name)
    if not path.exists():
        return default
    if not path.is_file():
        if strict:
            raise ProductError('expected json file: ' + path.name)
        return default
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        if strict:
            raise ProductError('corrupt json: ' + path.name)
        return default


def _read_strict_dict(path, label):
    """Read a json dict strictly; corrupt/non-dict raises instead of being hidden."""
    if path.is_symlink():
        raise ProductError('refusing symlink ' + label)
    if not path.exists():
        return None
    if not path.is_file():
        raise ProductError(label + ' must be a regular file')
    try:
        data = json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise ProductError(label + ' cannot be read')
    if not isinstance(data, dict):
        raise ProductError(label + ' must be a json object')
    return data


def _require_initialized(home):
    """Refuse to run on an uninitialized or corrupted home."""
    _assert_no_symlink(home, 'home')
    if not home.is_dir():
        raise ProductError('home is not initialized')
    state = home / 'state.sqlite3'
    if state.is_symlink():
        raise ProductError('state.sqlite3 symlink rejected')
    if not state.is_file():
        raise ProductError('home is not initialized')
    _read_strict_dict(home / 'config.json', 'config.json')
    _read_strict_dict(home / 'product.json', 'product.json')
    return home


def _core(home, command):
    _assert_no_symlink(home, 'home')
    argv = [sys.executable, '-m', 'tongle', '--home', str(home)] + list(command)
    proc = _run(argv, CORE_TIMEOUT)
    if proc.returncode:
        raise ProductError('core ' + command[0] + ' failed')
    data = _last_json(proc.stdout)
    if not isinstance(data, dict) or 'error' in data:
        raise ProductError('core ' + command[0] + ' failed')
    return data


def _maintain(home, command):
    script = ROOT / 'scripts' / 'maintain.py'
    argv = [sys.executable, str(script), '--home', str(home),
            '--root', str(ROOT), '--python', sys.executable, command]
    timeout = None if command == 'once' else MAINTAIN_TIMEOUT
    proc = _run(argv, timeout)
    payload = _last_json(proc.stdout)
    if proc.returncode:
        raise ProductError('maintenance ' + command + ' failed')
    if not isinstance(payload, dict):
        raise ProductError('maintenance ' + command + ' failed')
    if 'error' in payload and payload.get('error'):
        raise ProductError('maintenance ' + command + ' failed')
    return payload


def _run(argv, timeout):
    from tongle.portability import process_options, terminate_tree
    env = dict(os.environ)
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        child = subprocess.Popen(argv, cwd=str(ROOT), shell=False, env=env,
                                 text=True, encoding='utf-8',
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 **process_options(detached=True))
    except OSError:
        raise ProductError('command could not be launched')
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # subprocess.run would only kill the direct child and then block on
        # the pipes. Own the tree instead: terminate_tree ends the process
        # group, then drain the pipes with a bounded second wait. Never call an
        # unbounded communicate() here: a stuck grandchild would block forever.
        terminate_tree(child)
        try:
            out, err = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_tree(child, force=True)
            try:
                out, err = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                for stream in (child.stdin, child.stdout, child.stderr):
                    try:
                        stream.close()
                    except Exception:
                        pass
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                raise ProductError('command timed out') from None
        raise ProductError('command timed out')
    return subprocess.CompletedProcess(argv, child.returncode, out, err)


def _last_json(text):
    """The core/maintenance CLIs emit exactly one JSON document on stdout."""
    if not text or not text.strip():
        return None
    candidate = text.strip()
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def _assert_no_symlink(path, label):
    if path.is_symlink():
        raise ProductError('refusing symlink ' + label)
    probe = path
    seen = 0
    while probe != probe.parent and seen < 64:
        if probe.is_symlink():
            raise ProductError('refusing symlink ' + label)
        probe = probe.parent
        seen += 1


def _is_nested(a, b):
    a = Path(a)
    b = Path(b)
    return b == a or a in b.parents or b in a.parents


def _require_distinct_dirs(home, vault):
    if _is_nested(home, vault):
        raise ProductError('home and vault must be distinct nonnested directories')


def _validate_setup_paths(home, vault):
    """Validate all path safety BEFORE any mutation or directory creation."""
    _reject_if_symlink(home, 'home', ancestors=True)
    _reject_if_symlink(vault, 'vault', ancestors=True)
    if home.exists() and not home.is_dir():
        raise ProductError('home exists and is not a directory')
    if vault.exists() and not vault.is_dir():
        raise ProductError('vault exists and is not a directory')
    _require_distinct_dirs(home, vault)
    knowledge = vault / '知识'
    if knowledge.is_symlink():
        raise ProductError('vault/知识 symlink rejected')
    if knowledge.exists() and not knowledge.is_dir():
        raise ProductError('vault/知识 exists and is not a directory')
    if home.exists():
        _existing_state(home, vault)
        try:
            extra = list(home.iterdir())
        except OSError:
            raise ProductError('cannot inspect home')
        if extra:
            raise ProductError('refusing setup: home is not empty')


def _existing_state(home, vault):
    for name in ('state.sqlite3', 'config.json', 'product.json', 'preferences.md'):
        target = home / name
        if target.is_symlink():
            raise ProductError(name + ' symlink rejected')
        if target.exists():
            raise ProductError('refusing setup: ' + name + ' exists (not a fresh home)')
    maint = home / 'maintenance'
    for name in ('sources.json', 'sources.lock', 'state.json'):
        target = maint / name
        if target.is_symlink():
            raise ProductError('maintenance ' + name + ' symlink rejected')
        if target.exists():
            raise ProductError('refusing setup: maintenance/' + name + ' exists')
    if vault.is_symlink():
        raise ProductError('vault symlink rejected')


def _provider_request(args):
    supplied = [args.endpoint, args.model, args.key_env]
    if args.provider is None:
        if any(supplied):
            raise ProductError('provider flags require --provider')
        return {'enabled': False}
    if not all(supplied):
        raise ProductError('--provider requires --endpoint --model --key-env')
    endpoint = _check_endpoint(args.endpoint)
    model = _clean_text(args.model, 'model', required=True, allow_slash=True)
    if not ENV_NAME.fullmatch(args.key_env or ''):
        raise ProductError('key env name must match [A-Z_][A-Z0-9_]*')
    if not os.environ.get(args.key_env):
        raise ProductError('environment variable ' + args.key_env + ' is not set or empty')
    return {'enabled': True, 'provider': args.provider, 'endpoint': endpoint,
            'model': model, 'key_env': args.key_env}


def _config_for(model):
    config = dict(DEFAULT_CONFIG)
    if model.get('enabled'):
        runner = ROOT / 'scripts' / 'http_model.py'
        config['model_command'] = [sys.executable, str(runner),
                                   '--provider', model['provider'],
                                   '--endpoint', model['endpoint'],
                                   '--model', model['model'],
                                   '--key-env', model['key_env'],
                                   '--timeout', '180',
                                   '--max-output', '4096']
        config['model_name'] = model['model']
    else:
        config['model_command'] = []
        config['model_name'] = 'offline'
    return config


def _preferences(project, style):
    guide = STYLE_GUIDES[style]
    lines = [
        '# Tongle 偏好（用户可编辑）',
        '',
        '适用范围：项目 ' + project + '，风格 ' + style + '。',
        '这些是用户在本实例中选择的写作偏好，用于指导预期组织方式，',
        '不会被当作事实证据；它们不覆盖来源证据、引用或不确定性，',
        '也不改变现有五个文件夹与卡片结构，只影响生成内容的组织方式。',
        'setup 阶段 style_effect=unverified，只有在后续消费（生成）证据出现后',
        '才能确认风格是否真正生效。',
        '',
        '## 预期组织方式与写作要求',
        '',
    ]
    for section in STYLE_SECTIONS[style]:
        lines += ['### ' + section, '', guide[section], '']
    lines += [
        '## 来源与维护',
        '',
        '- 只使用 sources.json 中登记的来源；不隐式发现其他文件。',
        '- 偏好只影响组织与表达，不改变证据、引用与不确定性标注。',
        '- 风格是否生效在 setup 阶段记为 unverified，需以消费证据为准。',
        '',
    ]
    return '\n'.join(lines)


def _sources_path(home):
    return home / 'maintenance' / 'sources.json'


def _stable_source_id(project, path):
    digest = hashlib.sha256((project + '\0' + str(path)).encode('utf8')).hexdigest()[:16]
    return 'src-' + digest


def _validate_registry(data):
    if not isinstance(data, dict):
        raise ProductError('registry is corrupt')
    sources = data.get('sources')
    if not isinstance(sources, list):
        raise ProductError('registry is corrupt')
    for row in sources:
        if not isinstance(row, dict):
            raise ProductError('registry is corrupt')
        for field in ('project', 'path', 'source_id'):
            if not isinstance(row.get(field), str) or not row.get(field):
                raise ProductError('registry is corrupt')
    return sources


def _open_registry_locked(home):
    """Strict registry access under a separate flock; reject unsafe paths."""
    directory = home / 'maintenance'
    if directory.is_symlink():
        raise ProductError('maintenance directory cannot be symlink')
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ProductError('maintenance directory cannot be symlink')
    lock_path = directory / 'sources.lock'
    registry_path = _sources_path(home)
    if lock_path.is_symlink():
        raise ProductError('lock file cannot be symlink')
    if registry_path.is_symlink():
        raise ProductError('registry cannot be symlink')
    lock = lock_path.open('a')
    lock_file(lock)
    return lock, registry_path


def _read_registry_locked(registry_path):
    if registry_path.is_symlink():
        raise ProductError('registry cannot be symlink')
    data = _read_json(registry_path, {'sources': []}, strict=True)
    if data is None:
        data = {'sources': []}
    entries = _validate_registry(data)
    return data, entries


def _upsert_entry(data, project, path, source_id, kind):
    kept = [r for r in data.get('sources', []) if r.get('source_id') != source_id]
    entry = {'project': project, 'source_id': source_id,
             'path': str(path), 'kind': kind}
    for row in data.get('sources', []):
        if row.get('source_id') == source_id:
            for key, value in row.items():
                if key not in entry:
                    entry[key] = value
            break
    kept.append(entry)
    data['sources'] = kept
    return data


def _ingest_and_register_locked(home, project, path, source_id, kind='preferences'):
    lock, registry_path = _open_registry_locked(home)
    try:
        data, entries = _read_registry_locked(registry_path)
        duplicate = any(r.get('source_id') == source_id and
                        r.get('project') == project for r in entries)
        receipt = _core(home, ['ingest', '--project', project,
                               '--path', str(path), '--source-id', source_id])
        if not duplicate:
            data = _upsert_entry(data, project, path, source_id, kind)
            _atomic_json(registry_path, data)
    finally:
        unlock_file(lock)
        lock.close()
    return receipt, duplicate


def _check_source(path):
    if path.is_symlink():
        raise ProductError('source symlink rejected')
    if not path.is_file():
        raise ProductError('source file not found')
    if path.suffix.lower() not in ('.md', '.txt', '.docx'):
        raise ProductError('only .md/.txt/.docx are supported')
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ProductError('source file exceeds 5MiB')
    return path


def _model_status(home, product=None, config=None):
    if product is None:
        product = _read_strict_dict(home / 'product.json', 'product.json') or {}
    if config is None:
        config = _read_strict_dict(home / 'config.json', 'config.json') or {}
    command = config.get('model_command')
    if command is None or command == [] or command == '':
        configured = False
    elif isinstance(command, list):
        if command and all(isinstance(x, str) and x for x in command):
            configured = True
        else:
            raise ProductError('invalid model command')
    elif isinstance(command, str):
        if command:
            configured = True
        else:
            raise ProductError('invalid model command')
    else:
        raise ProductError('invalid model command')
    meta_enabled = product.get('model_enabled') is True
    meta_key_env = product.get('key_env')
    if meta_enabled and isinstance(meta_key_env, str) and ENV_NAME.fullmatch(meta_key_env):
        key_env = meta_key_env
        key_present = bool(os.environ.get(key_env))
        key_readiness = 'known'
    else:
        key_env = None
        key_present = False
        key_readiness = 'unknown'
    return {'configured': configured, 'key_env': key_env,
            'key_present': key_present, 'key_readiness': key_readiness}


def _require_model_ready(home):
    status = _model_status(home)
    if status['configured']:
        if status['key_env'] is None:
            raise ProductError('model is configured externally and lacks product key'
                               ' metadata; re-run setup or disable the model')
        if not status['key_present']:
            raise ProductError('model enabled but ' + status['key_env'] +
                               ' is not set in the environment; export it or disable the model')
    return status


def _known_project(home):
    product = _read_strict_dict(home / 'product.json', 'product.json') or {}
    project = product.get('project')
    if isinstance(project, str) and project and not CONTROLS.search(project):
        return project
    return None


def _require_project(home, explicit):
    if explicit is not None:
        return _clean_text(explicit, 'project', required=True)
    known = _known_project(home)
    if known:
        return known
    raise ProductError('project unknown; pass --project explicitly')


def cmd_setup(args):
    home = args.home
    vault = args.vault
    project = _clean_text(args.project, 'project', required=True)
    model = _provider_request(args)
    _validate_setup_paths(home, vault)
    config = _config_for(model)
    product = {
        'project': project,
        'style': args.style,
        'provider': model.get('provider'),
        'model': model.get('model'),
        'endpoint': model.get('endpoint'),
        'key_env': model.get('key_env'),
        'model_enabled': bool(model.get('enabled')),
        'style_effect': 'unverified',
        'style_applied': False,
    }
    if not args.write:
        return {'ok': True, 'home': str(home), 'vault': str(vault),
                'written': False, 'project': project, 'style': args.style,
                'style_effect': 'unverified', 'model_enabled': product['model_enabled'],
                'model_configured': bool(config['model_command'])}
    home.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    _core(home, ['init', '--vault', str(vault), '--evolving'])
    existing = _read_strict_dict(home / 'config.json', 'config.json')
    merged = dict(existing) if existing else {}
    merged.update(config)
    _atomic_json(home / 'config.json', merged)
    _atomic_json(home / 'product.json', product)
    pref = home / 'preferences.md'
    if pref.is_symlink():
        raise ProductError('preferences symlink rejected')
    with pref.open('x', encoding='utf8') as handle:
        handle.write(_preferences(project, args.style))
    resolved = resolve_path(str(pref), 'preferences')
    source_id = _stable_source_id(project, resolved)
    receipt, _duplicate = _ingest_and_register_locked(home, project, resolved,
                                                     source_id, 'preferences')
    version = receipt.get('version')
    _core(home, ['sync'])
    return {'ok': True, 'home': str(home), 'written': True, 'project': project,
            'style': args.style, 'style_effect': 'unverified',
            'source_id': source_id, 'version': version,
            'model_enabled': product['model_enabled'],
            'next': 'tongle.product start --home ' + str(home)}


def cmd_start(args):
    _require_initialized(args.home)
    _require_model_ready(args.home)
    return _maintain(args.home, 'start')


def cmd_stop(args):
    _require_initialized(args.home)
    return _maintain(args.home, 'stop')


def cmd_once(args):
    _require_initialized(args.home)
    _require_model_ready(args.home)
    return _maintain(args.home, 'once')


def cmd_status(args):
    _require_initialized(args.home)
    model = _model_status(args.home)
    core = _core(args.home, ['status'])
    maintenance = _maintain(args.home, 'status')
    return {'ok': True, 'home': str(args.home), 'maintenance': maintenance,
            'core': core, 'model': model}


def cmd_add(args):
    _require_initialized(args.home)
    path = resolve_path(args.path, 'source')
    _check_source(path)
    project = _require_project(args.home, args.project)
    source_id = _stable_source_id(project, path)
    receipt, duplicate = _ingest_and_register_locked(args.home, project, path,
                                                     source_id, 'source')
    version = receipt.get('version')
    return {'ok': True, 'source_id': source_id, 'project': project,
            'path': str(path), 'version': version, 'duplicate': duplicate}


def cmd_context(args):
    _require_initialized(args.home)
    project = _require_project(args.home, args.project)
    return _core(args.home, ['context', '--project', project, '--text', args.text,
                             '--session', args.session])


def cmd_backup(args):
    _require_initialized(args.home)
    target = resolve_path(args.to, 'backup target')
    return _core(args.home, ['export', '--to', str(target)])


def cmd_restore(args):
    vault = resolve_path(args.vault, 'vault')
    source = resolve_path(args.source, 'archive')
    argv = [sys.executable, '-m', 'tongle', '--home', str(args.home),
            'restore', '--from', str(source), '--vault', str(vault)]
    proc = _run(argv, CORE_TIMEOUT)
    if proc.returncode:
        raise ProductError('core restore failed')
    data = _last_json(proc.stdout)
    if not isinstance(data, dict) or 'error' in data:
        raise ProductError('core restore failed')
    product = _read_strict_dict(args.home / 'product.json', 'product.json')
    metadata_missing = product is None
    model = _model_status(args.home, product=product or {})
    return {'ok': True, 'restore': data, 'model': model,
            'metadata_missing': metadata_missing}


def cmd_guide(args):
    return guide.run(args.home, vault=args.vault)


def cmd_connect(args):
    env = {
        'TONGLE_NEXT_HOME': str(args.home),
        'TONGLE_NEXT_ROOT': str(ROOT),
        'TONGLE_NEXT_PYTHON': sys.executable,
    }
    explicit = _clean_text(args.project, 'project', required=True) if args.project is not None else None
    project = explicit if explicit is not None else _known_project(args.home)
    if project:
        env['TONGLE_NEXT_PROJECT'] = project
    env['TONGLE_NEXT_HOST'] = args.host
    if args.host == 'pi':
        command = ['pi', '--no-extensions', '-e', str(ROOT / 'adapters' / 'pi.ts')]
        return {'host': 'pi', 'env': env, 'command': command,
                'coverage': 'unknown until real host evidence'}
    argv = [sys.executable, str(ROOT / 'adapters' / 'hook.py'),
            '--adapter-id=tongle-next', '--host=codex']
    return {'host': 'codex', 'env': env, 'argv': argv,
            'coverage': 'unknown until real host evidence'}


class _ProductParser(argparse.ArgumentParser):
    def error(self, message):
        raise ProductError('invalid command arguments')


def parser():
    p = _ProductParser(prog='tongle.product')
    sub = p.add_subparsers(dest='command', required=True)
    sub.parser_class = _ProductParser

    def with_home(name):
        s = sub.add_parser(name)
        s.add_argument('--home', default=None)
        return s

    s = with_home('setup')
    s.add_argument('--vault', required=True)
    s.add_argument('--project', required=True)
    s.add_argument('--style', choices=STYLES, default='product')
    s.add_argument('--provider', choices=PROVIDERS, default=None)
    s.add_argument('--endpoint', default=None)
    s.add_argument('--model', default=None)
    s.add_argument('--key-env', dest='key_env', default=None)
    s.add_argument('--write', action='store_true')
    for name in ('start', 'stop', 'once', 'status'):
        with_home(name)
    a = with_home('add')
    a.add_argument('--path', required=True)
    a.add_argument('--project', default=None)
    c = with_home('context')
    c.add_argument('--project', default=None)
    c.add_argument('--text', required=True)
    c.add_argument('--session', default='')
    b = with_home('backup')
    b.add_argument('--to', required=True)
    r = with_home('restore')
    r.add_argument('--from', dest='source', required=True)
    r.add_argument('--vault', required=True)
    n = with_home('connect')
    n.add_argument('--host', choices=['pi', 'codex'], required=True)
    n.add_argument('--project', default=None)
    g = with_home('guide')
    g.add_argument('--vault', default=None)
    return p


COMMANDS = {'setup': cmd_setup, 'start': cmd_start, 'stop': cmd_stop, 'once': cmd_once,
            'status': cmd_status, 'add': cmd_add, 'context': cmd_context,
            'backup': cmd_backup, 'restore': cmd_restore, 'connect': cmd_connect,
            'guide': cmd_guide}


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ['guide']
    args = parser().parse_args(argv)
    if args.command == 'guide':
        return cmd_guide(args)
    args.home = resolve_home(args.home)
    if args.command == 'setup':
        args.vault = resolve_path(args.vault, 'vault')
    return COMMANDS[args.command](args)


if __name__ == '__main__':
    from tongle.portability import configure_stdio
    configure_stdio()
    try:
        result = main()
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
    except ProductError as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        print(json.dumps({'error': 'bounded failure'}, ensure_ascii=False))
        sys.exit(1)
