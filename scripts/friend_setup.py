#!/usr/bin/env python3
"""First-run guide and basic local use for the portable Tongle distribution."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "default"
DEMO_PROJECT = "tongle-demo"
DEMO_ID = "tongle-demo-stargazing"
DEMO_QUERY = "观星活动集合"
DEMO_TEXT = "# 周末观星活动\n\n观星活动集合时间为周六 19:30，地点为北门。\n"


def default_data_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Tongle"
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        if local.is_absolute():
            return local / "Tongle"
    return Path.home() / "Tongle"


def parse_path(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if not raw:
        raise ValueError("请输入文件或目录路径。")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("请使用完整路径。")
    return path


def choose_data_dir(raw, bundle, default):
    data = parse_path(raw) if raw.strip() else default
    if not data.is_absolute():
        raise ValueError("请使用完整路径。")
    data = data.resolve()
    bundle = bundle.resolve()
    if data == Path(data.anchor) or data == bundle or data in bundle.parents or bundle in data.parents:
        raise ValueError("请选择安装包目录之外的专用目录，且不能使用磁盘根目录。")
    if any(path.exists() and not path.is_dir() for path in (data, *data.parents)):
        raise ValueError("所选路径已被文件占用，请选一个目录。")
    return data


def terminal_text(text):
    return "".join(c for c in str(text) if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) < 160)


def runtime_environment():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "TONGLE_NEXT_"))}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def run_json(args, payload=None):
    result = subprocess.run([sys.executable, "-X", "utf8", *args], cwd=ROOT, env=runtime_environment(),
                            input=json.dumps(payload, ensure_ascii=False) if payload is not None else "",
                            encoding="utf-8", capture_output=True, timeout=120)
    if result.returncode:
        try:
            detail = json.loads(result.stderr).get("error")
        except (ValueError, AttributeError):
            detail = None
        if not detail:
            try:
                failed = [c["name"] for c in json.loads(result.stdout).get("checks", []) if not c["ok"]]
                detail = ", ".join(failed)
            except (ValueError, AttributeError, TypeError, KeyError):
                pass
        raise ValueError("Tongle 操作未完成。" + (" " + terminal_text(detail)[:400] if detail else ""))
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("Tongle 返回了无法识别的结果。")
    return value


class LocalInstance:
    def __init__(self, data):
        self.data = data
        self.home = data / "instance"
        self.vault = data / "vault"

    def lifecycle(self, command, *args):
        return run_json([str(ROOT / "scripts" / "tongle.py"), command,
                         "--home", str(self.home), *args])

    def verify(self, create=False):
        if any(path.is_symlink() or path.resolve() != path for path in (self.home, self.vault)):
            raise ValueError("数据子目录指向了其他位置，请选择一个新的专用目录。")
        launcher = self.home / "launcher.json"
        if launcher.is_symlink():
            raise ValueError("启动配置指向其他位置，请检查现有目录。")
        created = False
        if not launcher.is_file():
            if not create:
                raise ValueError("此目录尚未初始化，请先运行“开始使用”。")
            if self.data.exists() and any(self.data.iterdir()):
                raise ValueError("所选目录已有文件，请选择一个新的空目录。")
            self.lifecycle("setup", "--vault", str(self.vault), "--project", PROJECT, "--write")
            created = True
        binding = json.loads(launcher.read_text(encoding="utf-8"))
        if (Path(binding.get("root", "")).resolve() != ROOT
                or Path(binding.get("python", "")).resolve() != Path(sys.executable).resolve()
                or binding.get("project") != PROJECT):
            raise ValueError("所选数据目录由其他版本或项目管理，请从原来的 Tongle 入口打开。")
        for name in ("instance.json", "config.json", "state.sqlite3"):
            path = self.home / name
            if path.is_symlink() or path.resolve() != path or not path.is_file():
                raise ValueError("该目录的 Tongle 实例不完整，请检查原安装。")
        preflight = self.lifecycle("preflight")
        vault_check = next((item for item in preflight["checks"] if item["name"] == "vault"), {})
        if Path(vault_check.get("path") or "").resolve() != self.vault:
            raise ValueError("实例的知识库不在所选目录，请从原来的 Tongle 入口打开。")
        return {"ok": True, "created": created, "data_dir": str(self.data),
                "home": str(self.home), "vault": str(self.vault), "project": PROJECT,
                "enabled": preflight["instance"]["enabled"], "host_connection": "unverified"}

    def memory(self, action, project, payload):
        return run_json(["-m", "tongle", "--home", str(self.home), "memory",
                         "--project", project, "--session", "tongle-first-run", "--action", action],
                        payload=payload)

    def register(self, path, project, source_id):
        if path.is_symlink():
            raise ValueError("请选择原文件，不能使用指向其他位置的链接。")
        result = self.lifecycle("register", "--project", project, "--path", str(path),
                                "--source-id", source_id, "--write")
        receipt = result["ingest"]
        if receipt.get("source_id") != source_id or type(receipt.get("version")) is not int:
            raise ValueError("没有收到有效的导入回执。")
        return receipt

    def import_file(self, raw):
        path = parse_path(raw)
        key = os.path.normcase(str(path.resolve()))
        source_id = "default-file-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.register(path, PROJECT, source_id)

    def search(self, query, project=PROJECT):
        query = query.strip()
        if not query or len(query) > 1000:
            raise ValueError("请输入不超过 1000 字的查找问题。")
        return self.memory("search", project, {"query": query})["sources"]

    def read(self, source, project=PROJECT):
        return self.memory("source", project, {"id": source["source_id"], "version": source["version"]})

    def demo(self):
        path = self.data / "tongle-demo.md"
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("示例路径已被链接占用，请保留原文件并改用另一数据目录。")
        try:
            with path.open("x", encoding="utf-8", newline="\n") as output:
                output.write(DEMO_TEXT)
        except FileExistsError:
            if not path.is_file() or path.stat().st_size > 4096 or path.read_text(encoding="utf-8") != DEMO_TEXT:
                raise ValueError("同名示例文件已有其他内容。请保留该文件，并使用另一数据目录运行示例。")
        receipt = self.register(path, DEMO_PROJECT, DEMO_ID)
        match = next((s for s in self.search(DEMO_QUERY, DEMO_PROJECT)
                      if s["source_id"] == DEMO_ID and s["version"] == receipt["version"]), None)
        if match is None:
            raise ValueError("示例已导入，但尚未从检索结果中找到。")
        read = self.read(match, DEMO_PROJECT)
        if not read.get("complete") or read.get("text") != DEMO_TEXT:
            raise ValueError("示例已检索到，但读回内容与原文不一致。")
        return {**receipt, "match_verified": True, "text": read["text"]}


def command_text(args):
    if os.name == "nt":
        return "& " + " ".join("'" + str(item).replace("'", "''") + "'" for item in args)
    return shlex.join(map(str, args))


def connect_host(instance):
    pi_command = [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "tongle.py"),
                  "pi", "--home", str(instance.home)]
    if shutil.which("pi"):
        if input("现在打开已安装的 Pi？打开后可使用你的 Pi 账户 [y/N] ").strip().lower() == "y":
            result = subprocess.run(pi_command, cwd=instance.data, env=runtime_environment())
            if result.returncode:
                print("Pi 启动或运行未完成。请先检查 Pi 能否独立对话。")
            return
    else:
        print("未找到 Pi。装好并确认能正常对话后，可以回来打开。")
    print("也可在终端复制以下命令。")
    print(terminal_text(command_text(pi_command)))
    if os.name != "nt":
        print("已有 Codex 的接入步骤见安装包内 docs/friend-quickstart.md。")
    print("接入后请开一段新对话，用自己选的一条资料验证能找到原文。")


def show_source(read):
    print(terminal_text(read["text"]).strip())
    if read.get("next_offset") is not None:
        print("这里只显示原文开头。")


def interactive(instance):
    print("知识库已准备好。数据保存在", terminal_text(instance.data))
    while True:
        print("\n1 体验本地示例  2 添加资料  3 查找资料  4 接入已有 AI 工具  0 退出")
        action = input("请选择 [0] ").strip() or "0"
        try:
            if action == "0":
                return
            if action == "1":
                show_source(instance.demo())
                print("示例已通过导入、检索和原文核对。")
            elif action == "2":
                receipt = instance.import_file(input("请粘贴 Markdown、文本或 DOCX 文件的完整路径。\n> "))
                print("资料已导入，可用第 3 项查找。版本", receipt["version"])
            elif action == "3":
                sources = instance.search(input("想找什么？\n> "))
                if sources:
                    show_source(instance.read(sources[0]))
                else:
                    print("没有找到相关原文。可以用资料中的关键词再查一次。")
            elif action == "4":
                connect_host(instance)
            else:
                print("请输入 0 到 4 中的一项。")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(terminal_text(exc), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--data-dir", help="absolute directory for instance and vault")
    parser.add_argument("--json", action="store_true")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--demo", action="store_true")
    operations.add_argument("--status", action="store_true")
    operations.add_argument("--import", dest="file")
    operations.add_argument("--search")
    args = parser.parse_args()
    noninteractive = (args.data_dir is not None or args.json or args.demo or args.status
                      or args.file is not None or args.search is not None)
    if noninteractive and not args.data_dir:
        parser.error("non-interactive operation requires --data-dir")
    default = default_data_dir()
    try:
        raw = args.data_dir if noninteractive else input(
            f"欢迎使用 Tongle。\n直接回车使用默认目录 {terminal_text(default)}。\n"
            "也可以粘贴一个新的空目录路径。再次打开时请选择同一个目录。\n> ")
        instance = LocalInstance(choose_data_dir(raw, ROOT, default))
        result = instance.verify(create=not (args.status or args.file is not None or args.search is not None))
        if not noninteractive:
            interactive(instance)
            return 0
        if args.demo:
            result["demo"] = instance.demo()
        if args.file is not None:
            result["imported"] = instance.import_file(args.file)
        if args.search is not None:
            result["sources"] = instance.search(args.search)
            if result["sources"]:
                result["first_source"] = instance.read(result["sources"][0])
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("知识库已准备好。数据保存在", terminal_text(instance.data))
            if args.demo:
                show_source(result["demo"])
                print("示例已通过导入、检索和原文核对。")
            elif args.file is not None:
                print("资料已导入。版本", result["imported"]["version"])
            elif args.search is not None:
                if "first_source" in result:
                    show_source(result["first_source"])
                else:
                    print("没有找到相关原文。")
        return 0
    except (EOFError, KeyboardInterrupt):
        return 1
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        message = terminal_text(exc)
        if args.json:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
