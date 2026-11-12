"""Verify a packaged Tongle using native Pi and an isolated loopback provider."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import platform
import sqlite3
import stat
import subprocess
import tempfile
import threading
import zipfile


PROJECT = "portable-pi-demo"
SOURCE_ID = "portable-pi-lighthouse"
QUERY = "lighthouse handover"
VALUE = "azure-willow-73"
DOCUMENT = f"# Lighthouse handover\n\nThe lighthouse handover code is {VALUE}.\n"
INTRO = "This is an isolated Tongle acceptance session. Please reply READY."
QUESTION = "Find the lighthouse handover code in Tongle. Search and read the source."
INTRO_ANSWER = "READY"
FINAL_ANSWER = f"The lighthouse handover code is {VALUE}."


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if item.get("type") == "text")
    return ""


class Conversation:
    """A deterministic provider that requires actual tool results before answering."""

    def __init__(self):
        self.phases = []
        self.errors = []

    def respond(self, messages):
        user = [text_content(m.get("content")) for m in messages if m.get("role") == "user"]
        tools = [json.loads(text_content(m.get("content"))) for m in messages if m.get("role") == "tool"]
        if not user:
            raise ValueError("Pi did not send a user message")
        if INTRO in user and not tools and not self.phases:
            self.phases.append("initial_answer")
            return {"content": INTRO_ANSWER}, "stop"
        # Pi can serialize the adapter's separate context message as another
        # user-role entry. Require the original prompt independently.
        if user.count(QUESTION) != 1 or INTRO in user:
            raise ValueError("Pi did not preserve the follow-up question")
        if not tools and self.phases == ["initial_answer"]:
            self.phases.append("search")
            return self.tool("search", {"query": QUERY})
        if len(tools) == 1 and self.phases == ["initial_answer", "search"]:
            source = next(
                (s for s in tools[0].get("sources", []) if s.get("source_id") == SOURCE_ID),
                None,
            )
            if source is None or source.get("version") != 1:
                raise ValueError("Native search did not find the imported source and version")
            self.phases.append("read_source")
            return self.tool("source", {"id": SOURCE_ID, "version": source["version"]})
        if len(tools) == 2 and self.phases == ["initial_answer", "search", "read_source"]:
            source = tools[-1]
            if source.get("source_id") != SOURCE_ID or source.get("version") != 1:
                raise ValueError("Native source tool did not return the expected source identity")
            if source.get("text") != DOCUMENT:
                raise ValueError("Native source tool did not return the exact imported Markdown")
            self.phases.append("followup_answer")
            return {"content": FINAL_ANSWER}, "stop"
        raise ValueError("Unexpected native Pi request sequence")

    @staticmethod
    def tool(action, payload):
        call = {
            "index": 0,
            "id": f"call_tongle_{action}",
            "type": "function",
            "function": {
                "name": "tongle_memory",
                "arguments": json.dumps({"action": action, "payload": payload}),
            },
        }
        return {"tool_calls": [call]}, "tool_calls"


@contextmanager
def loopback(conversation):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                if self.path != "/v1/chat/completions":
                    raise ValueError("Unexpected loopback request path")
                size = int(self.headers["Content-Length"])
                if not 0 < size <= 512_000:
                    raise ValueError("Unexpected loopback request length")
                request = json.loads(self.rfile.read(size))
                delta, reason = conversation.respond(request["messages"])
            except (ValueError, KeyError, TypeError) as exc:
                conversation.errors.append(str(exc))
                self.send_error(400, "Synthetic provider assertion failed")
                return
            packets = [
                {"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": reason},
            ]
            data = b"".join(
                ("data: " + json.dumps({
                    "id": "chatcmpl-tongle-acceptance",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "tongle-fixture",
                    "choices": [packet],
                }) + "\n\n").encode("utf-8")
                for packet in packets
            ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            server.shutdown()
            thread.join()


def extract(archive, target):
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            path = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if (not path.parts or path.parts[0] != "Tongle" or ".." in path.parts
                    or "\\" in member.filename or ":" in member.filename or stat.S_ISLNK(mode)):
                raise ValueError("Unexpected member in Tongle ZIP")
            installed = Path(package.extract(member, target))
            if os.name != "nt" and mode & stat.S_IXUSR:
                installed.chmod(mode & 0o777)
    return target / "Tongle"


def isolated_env(base, pi):
    keys = ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "SystemDrive", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    # The supplied launcher and its Node runtime come from the test machine.
    # Keep credentials, Pi settings and Python imports outside the test profile.
    path = [str(pi.parent), os.environ.get("PATH", os.defpath)]
    profile = base / "profile"
    profile.mkdir()
    env.update({
        "PATH": os.pathsep.join(path),
        "HOME": str(profile),
        "USERPROFILE": str(profile),
        "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "PI_CODING_AGENT_DIR": str(profile / "pi"),
        "PI_OFFLINE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def run(arguments, cwd, env, *, timeout=45, check=True):
    result = subprocess.run(
        [str(item) for item in arguments], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(f"Process exit {result.returncode}: {result.stderr[-1600:]}")
    return result


def native_answers(stdout):
    answers = []
    for line in stdout.splitlines():
        event = json.loads(line)
        message = event.get("message", {})
        if (event.get("type") == "message_end" and message.get("role") == "assistant"
                and message.get("stopReason") == "stop"):
            answers.append(text_content(message.get("content")))
    return answers


def verify(package, pi, base):
    work = base / "独立项目"
    work.mkdir()
    env = isolated_env(base, pi)
    profile = Path(env["PI_CODING_AGENT_DIR"])
    profile.mkdir()
    sessions = base / "sessions"
    instance = base / "本地数据" / "instance"
    vault = instance.parent / "vault"
    python = package / ("runtime/python.exe" if os.name == "nt" else "runtime/bin/python3")
    core = [python, "-s", "-B", "-X", "utf8", package / "scripts" / "tongle.py"]
    run([*core, "setup", "--home", instance, "--vault", vault,
         "--project", PROJECT, "--write"], work, env)
    document = work / "lighthouse.md"
    document.write_bytes(DOCUMENT.encode("utf-8"))
    run([*core, "register", "--home", instance, "--project", PROJECT, "--source-id",
         SOURCE_ID, "--path", document, "--write"], work, env)

    conversation = Conversation()
    try:
        with loopback(conversation) as endpoint:
            models = {"providers": {"loopback": {
                "baseUrl": endpoint,
                "api": "openai-completions",
                "apiKey": "tongle-acceptance-fixture",
                "models": [{"id": "tongle-fixture", "contextWindow": 32768, "maxTokens": 4096}],
            }}}
            (profile / "models.json").write_text(json.dumps(models), encoding="utf-8")
            for prompt, answer in ((INTRO, INTRO_ANSWER), (QUESTION, FINAL_ANSWER)):
                command = [
                    *core, "pi", "--home", instance, "--provider", "loopback",
                    "--model", "tongle-fixture", "--", "--thinking", "off",
                    "--no-builtin-tools", "--tools", "tongle_memory", "--no-skills",
                    "--no-context-files", "--no-prompt-templates", "--no-themes",
                    "--offline", "--session-dir", sessions, "--mode", "json", "-p", prompt,
                ]
                result = run(command, work, env)
                if conversation.errors:
                    raise ValueError(conversation.errors[0])
                if native_answers(result.stdout) != [answer]:
                    raise ValueError("Native Pi output did not include the expected final answer")
    finally:
        run([*core, "stop", "--home", instance], work, env)

    database = (instance / "state.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database, uri=True)) as db:
        saved = db.execute(
            "SELECT s.session,s.role,v.text FROM sources s JOIN versions v "
            "ON v.source_id=s.id AND v.version=s.current_version "
            "WHERE s.project=? AND s.role IN ('user','assistant')",
            (PROJECT,),
        ).fetchall()
    pairs = {}
    for session, role, text in saved:
        pairs.setdefault(session, []).append((role, text))
    expected = [sorted([("user", prompt), ("assistant", answer)])
                for prompt, answer in ((INTRO, INTRO_ANSWER), (QUESTION, FINAL_ANSWER))]
    if not all(pairs) or sorted(sorted(turn) for turn in pairs.values()) != sorted(expected):
        raise ValueError("Tongle did not preserve the exact user and assistant turns in two sessions")
    return {
        "passed": True,
        "system": platform.system(),
        "architecture": platform.machine(),
        "isolated_pi_profile": True,
        "model_transport": "synthetic OpenAI-compatible HTTP on 127.0.0.1",
        "native_pi_requests": conversation.phases,
        "native_sessions": len(pairs),
        "exact_saved_messages": len(saved),
        "imported_source_read_back": True,
        "maintenance_stop_requested": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--pi", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not args.pi.is_file():
        parser.error("--pi must point to an installed Pi launcher")
    with tempfile.TemporaryDirectory(prefix="tongle-pi-check-") as temp:
        base = Path(temp).resolve()
        package = extract(args.zip.resolve(), base / "干净 安装")
        result = verify(package, args.pi.absolute(), base)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
