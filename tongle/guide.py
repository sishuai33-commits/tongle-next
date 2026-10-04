import os
import sys
import getpass
from pathlib import Path

from . import guide_actions as A
from .portability import lock_file, unlock_file


def _out(output):
    if output is None:
        def emit(text):
            print(text)
        return emit
    if callable(output):
        return output
    def emit(text):
        output.write(text + "\n")
    return emit


def _ask(input_fn, prompt):
    return input_fn(prompt)


def _validate_goal(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    if len(raw) > 1000:
        return None
    return raw


def _validate_pref(raw):
    raw = (raw or "").strip()
    if len(raw) > 2000:
        return None
    return raw


def _ask_nonempty(input_fn, prompt):
    while True:
        raw = _ask(input_fn, prompt)
        if raw is None or raw == "":
            raise EOFError
        val = raw.strip()
        if val:
            return val


def _ask_goal(input_fn, outfn):
    while True:
        raw = _ask(input_fn, "你现在想完成什么？")
        if raw is None:
            raise EOFError
        g = _validate_goal(raw)
        if g is not None:
            return g
        outfn("目标请控制在1到1000字，且不能为空，请重新输入。")


def _ask_files(input_fn, outfn):
    while True:
        raw = _ask(input_fn, "请把要整理的资料拖到这里，或粘贴路径（可留空跳过）：")
        if raw is None:
            raise EOFError
        try:
            paths = A.parse_files(raw)
        except Exception as exc:
            outfn(A.message(exc) + " 请重新输入，或留空跳过。")
            continue
        return paths


def _ask_choice(input_fn, prompt, valid, outfn):
    while True:
        raw = _ask(input_fn, prompt)
        if raw is None:
            raise EOFError
        val = raw.strip()
        if val in valid:
            return val
        outfn("请输入有效选项。")


def _ask_secret(secret_fn, prompt):
    if secret_fn is None:
        secret_fn = getpass.getpass
    raw = secret_fn(prompt)
    if raw is None:
        return ""
    return raw.strip()


def _fresh(home, vault, input_fn, secret_fn, outfn):
    outfn("1/3 " + "你现在想完成什么？")
    goal = _ask_goal(input_fn, outfn)
    outfn("你也可以写下输出偏好（可留空）。")
    raw = _ask(input_fn, "输出偏好（可留空）：")
    if raw is None:
        raise EOFError
    pref = _validate_pref(raw)
    while pref is None:
        outfn("偏好请控制在2000字以内，请重新输入，或留空。")
        raw = _ask(input_fn, "输出偏好（可留空）：")
        if raw is None:
            raise EOFError
        pref = _validate_pref(raw)

    outfn("2/3 选择资料")
    paths = _ask_files(input_fn, outfn)
    if not paths:
        outfn("已跳过资料选择，之后可在菜单里添加。")
    else:
        outfn("已选资料：")
        for p in paths:
            outfn(str(p))

    outfn("3/3 连接模型")
    outfn("留空使用默认 DeepSeek；1 默认 DeepSeek；2 兼容服务；0 稍后离线。")
    choice = _ask_choice(input_fn, "请选择：", {"", "1", "2", "0"}, outfn)

    provider = None
    endpoint = None
    model = None
    if choice in ("", "1"):
        provider = "deepseek"
        endpoint = "https://api.deepseek.com/chat/completions"
        model = "deepseek-flash"
    elif choice == "2":
        outfn("请输入兼容服务的接口地址与模型名。")
        endpoint = _ask_nonempty(input_fn, "接口地址：")
        model = _ask_nonempty(input_fn, "模型名：")
        provider = "openai"
        try:
            from . import product
            product._check_endpoint(endpoint)
        except Exception as exc:
            outfn(A.message(exc) + " 已改为离线，稍后可重新连接。")
            provider = None
            endpoint = None
            model = None

    if provider is not None:
        host = endpoint.split("/")[2] if "//" in endpoint else endpoint
        outfn("服务地址：" + host)
        outfn("选中的资料、目标与偏好会发给该服务，API可能收费。")
        key = _ask_secret(secret_fn, "请输入API密钥（留空则离线）：")
        if not key:
            outfn("未输入密钥，将离线保存。")
            provider = None
            endpoint = None
            model = None
        else:
            os.environ["TONGLE_GUIDE_API_KEY"] = key
            outfn("下次重新打开时可能需要再次输入密钥。")

    outfn("目标：" + goal)
    if paths:
        outfn("资料：" + "，".join(str(p) for p in paths))
    else:
        outfn("资料：无")
    outfn("知识库默认位置：" + str(vault))

    while True:
        raw = _ask(input_fn, "开始整理并启用自动维护？[Y/n]")
        if raw is None:
            raise EOFError
        ans = raw.strip().lower()
        if ans in ("", "y", "yes"):
            break
        if ans in ("n", "no"):
            outfn("已取消，未创建任何文件。")
            return False
        outfn("请输入 y 或 n。")

    state = A.create(home, vault, goal, pref, paths)
    new_model = None
    if provider is not None:
        new_model = (provider, endpoint, model)
    return {"state": state, "new_model": new_model}


def _confirm_send(input_fn, outfn, model_info):
    endpoint = model_info.get("endpoint") or ""
    host = endpoint.split("/")[2] if "//" in endpoint else endpoint
    outfn("服务地址：" + host)
    outfn("选中的资料、目标与偏好会发给该服务，API可能收费。")
    raw = _ask(input_fn, "确认连接？[Y/n]")
    if raw is None:
        raise EOFError
    return raw.strip().lower() in ("", "y", "yes")


def _ensure_connect(home, input_fn, secret_fn, outfn, allow_new, force_key=False):
    info = A.model_info(home)
    if info.get("configured") and (not info.get("key_present") or force_key):
        key_env = info.get("key_env")
        if not key_env:
            outfn("当前模型由外部元数据配置，无法安全替换密钥，请手动配置环境变量。")
            return False
        outfn("需要该模型服务的密钥才能继续。")
        outfn("服务配置保持不变，只补充当前会话密钥。")
        if not _confirm_send(input_fn, outfn, info):
            return False
        key = _ask_secret(secret_fn, "请输入API密钥（留空则取消）：")
        if not key:
            return False
        os.environ[key_env] = key
        return True
    if info.get("configured"):
        return True

    outfn("请选择模型服务：1 默认 DeepSeek；2 兼容服务。")
    choice = _ask_choice(input_fn, "请选择：", {"1", "2"}, outfn)
    if choice == "1":
        provider = "deepseek"
        endpoint = "https://api.deepseek.com/chat/completions"
        model = "deepseek-flash"
    else:
        endpoint = _ask_nonempty(input_fn, "接口地址：")
        model = _ask_nonempty(input_fn, "模型名：")
        provider = "openai"
        if "//" not in endpoint or not endpoint.startswith("https://"):
            outfn("接口地址需为HTTPS，请重试。")
            return False
        try:
            from . import product
            product._check_endpoint(endpoint)
        except Exception as exc:
            outfn(A.message(exc))
            return False
    if not _confirm_send(input_fn, outfn, {"endpoint": endpoint}):
        return False
    key = _ask_secret(secret_fn, "请输入API密钥（留空则取消）：")
    if not key:
        return False
    os.environ["TONGLE_GUIDE_API_KEY"] = key
    A.configure(home, provider, endpoint, model, "TONGLE_GUIDE_API_KEY")
    return True


def _take_lock(home):
    lockpath = Path(home) / "guide.lock"
    A.safe(lockpath)
    fh = open(lockpath, "a+")
    try:
        lock_file(fh, blocking=False)
    except (BlockingIOError, OSError):
        fh.close()
        return None
    return fh


def _release_lock(fh):
    if fh is None:
        return
    try:
        unlock_file(fh)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _needs_init(home):
    p = Path(home)
    if not p.exists():
        return True
    try:
        items = list(p.iterdir())
    except Exception:
        return False
    return len(items) == 0


def _first_flow(home, state, input_fn, secret_fn, outfn):
    info = A.model_info(home)
    if not info.get("configured") or not info.get("key_present"):
        outfn("模型尚未配置或缺少密钥，无法整理。")
        return state
    try:
        res = A.maintain(home)
    except Exception as exc:
        outfn(A.message(exc))
        return state
    if not res.get("ok"):
        outfn("维护失败：" + str(res.get("reason", "未知原因")))
        return state

    if not state.get("completed"):
        outfn("正在根据已有知识准备草稿……")
        try:
            draft = A.draft(home, state.get("goal"))
        except Exception as exc:
            outfn(A.message(exc))
            outfn("尚未生成草稿，稍后可重试。")
            return A.load(home)
        outfn(draft.get("text") or "")
        if draft.get("path"):
            outfn("草稿文件：" + str(draft["path"]))
        if draft.get("cards"):
            outfn("本次提供的卡片：" + str(draft["cards"]))
        _feedback(home, draft, input_fn, outfn)

    outfn("正在整理所选资料，请稍候……")
    try:
        ok = A.start(home)
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception as exc:
        outfn(A.message(exc))
        return A.load(home)
    if ok:
        outfn("自动整理已启动。退出窗口后仍会继续；菜单5可停止。")
    return A.load(home)


def _feedback(home, draft, input_fn, outfn):
    while True:
        raw = _ask(input_fn, "这份草稿有帮助吗？1 有帮助 2 需要改 0 稍后")
        if raw is None:
            raise EOFError
        val = raw.strip()
        if val == "1":
            A.feedback(home, draft.get("session"), "1")
            return
        if val == "2":
            corr = _ask(input_fn, "请写下修改建议：")
            if corr is None:
                raise EOFError
            A.feedback(home, draft.get("session"), "2", corr)
            return
        if val == "0":
            return
        outfn("请输入 0、1 或 2。")


def _menu(home, state, input_fn, secret_fn, outfn):
    while True:
        running = False
        try:
            st = A.status(home)
            running = bool(st.get("maintenance", {}).get("running"))
        except Exception:
            running = False
        outfn("菜单：1 做一件事｜2 添加资料｜3 查看知识与状态｜4 连接模型/继续整理｜5 停止自动整理｜0 退出")
        raw = _ask(input_fn, "请选择：")
        if raw is None:
            raise EOFError
        choice = raw.strip()

        if choice == "1":
            try:
                if not _ensure_connect(home, input_fn, secret_fn, outfn, True):
                    continue
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                outfn(A.message(exc))
                continue
            goal = _ask_nonempty(input_fn, "这次想完成什么？")
            outfn("正在整理所选资料，请稍候……")
            try:
                res = A.maintain(home)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                outfn(A.message(exc))
                continue
            if not res.get("ok"):
                outfn("维护失败：" + str(res.get("reason", "未知原因")))
                continue
            outfn("正在根据已有知识准备草稿……")
            try:
                draft = A.draft(home, goal)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                outfn(A.message(exc))
                continue
            outfn(draft.get("text") or "")
            if draft.get("path"):
                outfn("草稿文件：" + str(draft["path"]))
            _feedback(home, draft, input_fn, outfn)
            state = A.load(home)

        elif choice == "2":
            paths = _ask_files(input_fn, outfn)
            if not paths:
                continue
            info = A.model_info(home)
            if info.get("configured"):
                outfn("将把这些资料发往模型服务整理。")
                endpoint = info.get("endpoint") or ""
                host = endpoint.split("/")[2] if "//" in endpoint else endpoint
                if host:
                    outfn("服务地址：" + host)
            if running:
                outfn("自动整理正在运行，新资料会进入后续整理范围。")
            outfn("即将添加：" + "，".join(str(p) for p in paths))
            raw = _ask(input_fn, "确认添加？[Y/n]")
            if raw is None:
                raise EOFError
            if raw.strip().lower() not in ("", "y", "yes"):
                continue
            try:
                n = A.add(home, paths)
            except Exception as exc:
                outfn(A.message(exc))
                continue
            outfn("已添加资料：" + str(n))
            if info.get("configured") and info.get("key_present"):
                try:
                    state = _first_flow(home, state, input_fn, secret_fn, outfn)
                except (EOFError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    outfn(A.message(exc))
                    continue
            else:
                outfn("资料已保存，尚未生成卡片。下次可连接模型继续。")

        elif choice == "3":
            try:
                st = A.status(home)
            except Exception as exc:
                outfn(A.message(exc))
                continue
            vpath = state.get("vault") or str(Path.home() / "Tongle知识")
            outfn("知识库：" + str(vpath))
            outfn("首页：" + str(Path(vpath) / "首页.md"))
            m = st.get("maintenance", {})
            outfn("自动整理运行中：" + ("是" if m.get("running") else "否"))
            outfn("失败批次：" + str(m.get("failed_batches", 0)))
            work = st.get("work") or {}
            pending = work.get("pending")
            retry = work.get("retry")
            budget_wait = work.get("budget_wait")
            def _n(v):
                return "未知" if v is None else str(v)
            outfn("待处理：" + _n(pending) + "｜重试：" + _n(retry) + "｜额度等待：" + _n(budget_wait))
            if m.get("needs_attention"):
                outfn("有需要关注的事项。")

        elif choice == "4":
            try:
                if not _ensure_connect(home, input_fn, secret_fn, outfn, True, force_key=True):
                    continue
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                outfn(A.message(exc))
                continue
            try:
                state = _first_flow(home, state, input_fn, secret_fn, outfn)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                outfn(A.message(exc))
                continue

        elif choice == "5":
            try:
                A.stop(home)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                outfn(A.message(exc))
                continue
            outfn("已请求停止，当前批次会自然结束，不会强制中断。")

        elif choice == "0" or choice == "":
            try:
                st = A.status(home)
                still = bool(st.get("maintenance", {}).get("running"))
            except Exception:
                still = False
            if still:
                outfn("自动整理仍在后台继续，可稍后在菜单5停止。")
            else:
                outfn("已退出，进度已保存。")
            return state

        else:
            outfn("请输入有效选项。")


def run(home=None, vault=None, input_fn=None, secret_fn=None, output=None):
    outfn = _out(output)
    saved_env = dict(os.environ)

    if input_fn is None:
        if not sys.stdin.isatty():
            if os.name == 'nt':
                outfn("请双击“开始使用.cmd”，或在终端运行 .\\tongle.exe guide。")
            else:
                outfn("请双击“开始使用.command”，或在终端运行 ./tongle guide。")
            return
        input_fn = input

    lock = None
    created = False
    try:
        from . import product as _product
        homep = Path(_product.resolve_home(str(home) if home is not None else None))
        if vault is None:
            vaultp = Path.home() / "Tongle知识"
        else:
            vaultp = _product.resolve_path(str(vault), 'vault')
        A.safe(homep)

        if _needs_init(homep):
            result = _fresh(homep, vaultp, input_fn, secret_fn, outfn)
            if not result:
                return
            created = True
            lock = _take_lock(homep)
            if lock is None:
                outfn("无法获取锁，请稍后重试。")
                return
            new_model = result.get("new_model")
            if new_model is not None:
                provider, endpoint, model = new_model
                A.configure(homep, provider, endpoint, model, "TONGLE_GUIDE_API_KEY")
            state = A.load(homep)
        else:
            try:
                state = A.load(homep)
            except Exception as exc:
                outfn("无法读取已有配置，请检查后再试：" + A.message(exc))
                return
            lock = _take_lock(homep)
            if lock is None:
                outfn("无法获取锁，请稍后重试。")
                return

        outfn("目标：" + str(state.get("goal", "")))
        outfn("知识库：" + str(state.get("vault", "")))

        info = A.model_info(homep)
        if created:
            if info.get("configured") and info.get("key_present"):
                state = _first_flow(homep, state, input_fn, secret_fn, outfn)
            else:
                outfn("资料已保存，尚未生成卡片。下次可连接模型继续。")
        elif info.get("configured") and not info.get("key_present"):
            outfn("当前模型服务缺少密钥，可重新输入。")

        _menu(homep, state, input_fn, secret_fn, outfn)

    except (EOFError, KeyboardInterrupt):
        if created or "state" in locals():
            outfn("已保存进度。")
        else:
            outfn("已取消，未创建资料库。")
    except ValueError as exc:
        outfn("操作无法完成：" + A.message(exc))
    except Exception as exc:
        outfn("出错了：" + A.message(exc))
    finally:
        _release_lock(lock)
        changed = {"TONGLE_GUIDE_API_KEY"}
        try:
            info = A.model_info(Path(_product_resolve_home()))
            if info.get("key_env"):
                changed.add(info["key_env"])
        except Exception:
            pass
        for name in changed:
            if name in saved_env:
                os.environ[name] = saved_env[name]
            else:
                os.environ.pop(name, None)


def _product_resolve_home():
    from . import product as _product
    return _product.resolve_home(None)