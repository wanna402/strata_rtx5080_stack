# -*- coding: utf-8 -*-
"""
Strata 智能栈托盘管家 (stack_tray.py)
=====================================
常驻右下角托盘，管理三个服务：
  :8083 主脑 Strata（黑窗口由桌面 bat 启动，本程序只看状态/可停/可开页）
  :8087 工具平面 mcp_gateway_8087.py（本程序拉起，隐藏窗口）
  :8081 网关 gateway_8081.py（本程序拉起，隐藏窗口）

  说明：:8086 向量 BGE-M3 已停用（ENABLE_VEC=False）。代码保留在常量区，
        恢复方式见 ENABLE_VEC 上方注释；默认不会再拉起。

用法：
  pythonw stack_tray.py            # 常驻托盘（无窗口）
  python  stack_tray.py --selftest # 自检：起 8087，健康检查通过后全停退出
  日志：logs/stack_tray_YYYYMMDD.log（单日单文件，append 永续）

网关可调开关（档位粘住 / 历史思考摘除 / 循环检测 / 缓存告警）见 launcher.py 顶部注释。
"""
import os
import sys
import time
import json
import socket
import threading
import subprocess
import urllib.request
import webbrowser
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "log")  # 统一日志目录：8081/8083/8086/8087/托盘 全在这里
os.makedirs(LOGS_DIR, exist_ok=True)

# 向量 embedding 引擎：Strata 自家引擎没有 embedding 模式，这里借用一个外部
# 运行时二进制（和 python.exe 一个性质，只跑 BGE-M3，不读 llamacpp 目录任何配置）
LLAMA_SERVER = r"H:\llama-bin-win-cuda-13.3-x64\llama-server.exe"
BGE_MODEL = r"H:\models\bge-m3-q8_0.gguf"

# 8086 向量服务（BGE-M3）默认不再随栈启动：
#   它常驻占 584 MB 显存，而 personal_memory.db 至今 0 行（没有任何写入端）。
#   真要做长期记忆时改回 True；更推荐按 CPU 跑（llama-server -ngl 0），显存零占用。
ENABLE_VEC = False
TOOL_PLANE = os.path.join(BASE_DIR, "gateway", "mcp_gateway_8087.py")
GATEWAY_SCRIPT = os.path.join(BASE_DIR, "gateway", "gateway_8081.py")

PORTS = {"main": 8083, "vec": 8086, "tools": 8087, "gw": 8081}
TRAY_MUTEX_PORT = 18081  # 托盘单实例互斥（bind 占用即已有实例）
AUTOSTART_TRIGGER = os.path.join(LOGS_DIR, "autostart.trigger")  # bat 每次都写，活着的托盘捡到即全启

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------- 日志（单日单文件 append） ----------------
_tray_log_fp = None
_tray_log_day = ""


def tray_log(msg):
    global _tray_log_fp, _tray_log_day
    day = time.strftime("%Y%m%d")
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        if day != _tray_log_day and _tray_log_fp:
            try:
                _tray_log_fp.close()
            except Exception:
                pass
            _tray_log_fp = None
        if _tray_log_fp is None:
            _tray_log_fp = open(os.path.join(LOGS_DIR, f"stack_tray_{day}.log"),
                                "a", encoding="utf-8", buffering=1)
            _tray_log_day = day
        _tray_log_fp.write(line + "\n")
        _tray_log_fp.flush()
    except Exception:
        pass


def port_open(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def http_json(url, timeout=3):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    return None


def check_main():
    """主脑状态：/health 优先，端口通即算半活。"""
    h = http_json(f"http://127.0.0.1:{PORTS['main']}/health", timeout=3)
    if isinstance(h, dict) and h.get("status") == "ok":
        return "ok"
    if port_open(PORTS["main"]):
        return "starting"
    return "down"


def check_vec():
    if not ENABLE_VEC:
        return "off"
    h = http_json(f"http://127.0.0.1:{PORTS['vec']}/health", timeout=3)
    if isinstance(h, dict) and h.get("status") in ("ok", "loading model", "error"):
        return "ok"
    return "down" if not port_open(PORTS["vec"]) else "starting"


def check_tools():
    h = http_json(f"http://127.0.0.1:{PORTS['tools']}/health", timeout=3)
    if isinstance(h, dict) and h.get("status") == "ok":
        return "ok"
    return "down" if not port_open(PORTS["tools"]) else "starting"


def check_gw():
    if not os.path.exists(GATEWAY_SCRIPT):
        return "todo"
    h = http_json(f"http://127.0.0.1:{PORTS['gw']}/health", timeout=3)
    if isinstance(h, dict) and h.get("status") == "ok":
        return "ok"
    return "down" if not port_open(PORTS["gw"]) else "starting"


# ---------------- 服务拉起 / 停止 ----------------
_owned = {}  # name -> Popen（本托盘拉起的进程）
_last_autostart = 0.0  # autostart 节流：60 秒内重复触发直接忽略（防双击 bat 起两份）


def _owned_alive(name):
    p = _owned.get(name)
    return p is not None and p.poll() is None
_main_was_up = False       # 主脑曾经起来过（用于识别“黑窗被叉掉”）
_tray_stop_ts = 0.0        # 托盘自己停主脑的时间戳（15 秒内主脑消失不算被叉）


def _tray_initiated_recent(sec=15):
    return (time.time() - _tray_stop_ts) < sec


_spawn_logs = []  # 子进程日志句柄，必须持有，否则会被 GC 提前关掉


def _spawn_hidden(cmd, cwd=None, log_path=None):
    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    out = subprocess.DEVNULL
    if log_path:
        try:
            out = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            _spawn_logs.append(out)
        except Exception:
            out = subprocess.DEVNULL
    return subprocess.Popen(cmd, cwd=cwd, creationflags=creationflags,
                            startupinfo=startupinfo,
                            stdout=out, stderr=subprocess.STDOUT)


def start_vec():
    if not ENABLE_VEC:
        return "disabled"
    if _owned_alive("vec"):
        return "already"
    if check_vec() != "down":
        return "already"
    if not os.path.exists(LLAMA_SERVER) or not os.path.exists(BGE_MODEL):
        return "missing"
    _owned["vec"] = _spawn_hidden(
        [LLAMA_SERVER, "-m", BGE_MODEL, "--embedding",
         "--port", str(PORTS["vec"]), "--ctx-size", "8192"], cwd=BASE_DIR,
        log_path=os.path.join(LOGS_DIR, "8086_vec_%s.log" % time.strftime("%Y%m%d")))
    tray_log(f"向量 :{PORTS['vec']} 拉起中 (pid={_owned['vec'].pid})")
    return "started"


def start_tools():
    if _owned_alive("tools"):
        return "already"
    if check_tools() != "down":
        return "already"
    if not os.path.exists(TOOL_PLANE):
        return "missing"
    _owned["tools"] = _spawn_hidden(
        [sys.executable, TOOL_PLANE, "--port", str(PORTS["tools"])], cwd=BASE_DIR)
    tray_log(f"工具平面 :{PORTS['tools']} 拉起中 (pid={_owned['tools'].pid})")
    return "started"


def start_gw():
    if _owned_alive("gw"):
        return "already"
    if port_open(PORTS["gw"]):
        # 先到先得铁律：TCP 端口被占（llama 栈代理先占了，或自家网关正在启动中）一律复用
        tray_log(f"网关 :{PORTS['gw']} 端口已被占用（外来代理或自家启动中），不抢占")
        return "already"
    if check_gw() in ("ok", "starting"):
        return "already"
    if not os.path.exists(GATEWAY_SCRIPT):
        return "missing"
    _owned["gw"] = _spawn_hidden(
        [sys.executable, GATEWAY_SCRIPT, "--port", str(PORTS["gw"])], cwd=BASE_DIR)
    tray_log(f"网关 :{PORTS['gw']} 拉起中 (pid={_owned['gw'].pid})")
    return "started"


def stop_owned(name):
    p = _owned.pop(name, None)
    if p is None:
        return "not-owned"
    try:
        p.terminate()
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    tray_log(f"{name} 已停止")
    return "stopped"


def _ps_pids_by_cmdline(substr):
    """按命令行关键字找 python.exe 的 PID：psutil 直查，失败才回退 PowerShell。"""
    try:
        import psutil
        mine = os.getpid()
        out = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (p.info["name"] or "").lower() != "python.exe":
                    continue
                if p.info["pid"] == mine:
                    continue
                cmd = " ".join(p.info["cmdline"] or [])
                if substr.lower() in cmd.lower():
                    out.append(str(p.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return out
    except Exception as e:
        tray_log(f"psutil 查询失败，回退 PowerShell：{type(e).__name__}")
    import subprocess as sp
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object {$_.CommandLine -like '*" + substr + "*'} | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        cflags = 0x08000000 if sys.platform == "win32" else 0
        sinfo = None
        if sys.platform == "win32":
            sinfo = sp.STARTUPINFO()
            sinfo.dwFlags |= sp.STARTF_USESHOWWINDOW
            sinfo.wShowWindow = 0
        out = sp.run(["powershell", "-NoProfile", "-Command", ps],
                     capture_output=True, text=True, timeout=30,
                     creationflags=cflags, startupinfo=sinfo).stdout
    except Exception as e:
        tray_log(f"进程查询失败：{type(e).__name__}")
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip().isdigit()]


def _kill_by_cmdline(substr):
    """按命令行关键字精确杀进程（只动 Strata 自家的 python，不碰别人的）。"""
    import subprocess as sp
    pids = _ps_pids_by_cmdline(substr)
    if not pids:
        tray_log(f"未找到命令行含 {substr} 的进程（无需杀）")
    cflags = 0x08000000 if sys.platform == "win32" else 0
    sinfo = None
    if sys.platform == "win32":
        sinfo = sp.STARTUPINFO()
        sinfo.dwFlags |= sp.STARTF_USESHOWWINDOW
        sinfo.wShowWindow = 0
    for pid in pids:
        try:
            sp.run(["taskkill", "/PID", pid, "/T", "/F"],
                   capture_output=True, timeout=15,
                   creationflags=cflags, startupinfo=sinfo)
            tray_log(f"已终止 python pid={pid} ({substr})")
        except Exception as e:
            tray_log(f"终止 pid={pid} 失败：{type(e).__name__}")
    return pids


def _strata_pids():
    """活着的引擎 PID（strata.exe / strata-vision.exe）。"""
    pids = []
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info["name"] or "").lower() in ("strata.exe", "strata-vision.exe"):
                    pids.append(p.pid)
            except Exception:
                pass
    except Exception:
        pass
    return pids


def _graceful_stop_strata(wait_sec=30):
    """先请引擎自己退：CTRL_BREAK -> 它自己的收尾流程（--expert-profile-save 会落盘），
    最多等 wait_sec 秒；还活着的返回给调用方补刀。

    直接 taskkill 会跳过收尾，这一轮自适应层学到的东西就全丢了。
    实测：CTRL_C_EVENT 送不进别的控制台（AttachConsole 也不行），CTRL_BREAK_EVENT 可以。
    """
    pids = _strata_pids()
    if not pids:
        return []
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.AttachConsole.argtypes = [wintypes.DWORD]
    k32.AttachConsole.restype = wintypes.BOOL
    k32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    sent = []
    for pid in pids:
        k32.FreeConsole()
        if k32.AttachConsole(pid):
            try:
                k32.SetConsoleCtrlHandler(None, True)   # 别让这个信号反过来带走托盘自己
                if k32.GenerateConsoleCtrlEvent(1, 0):  # 1 = CTRL_BREAK_EVENT
                    sent.append(pid)
            finally:
                k32.FreeConsole()
    tray_log(f"引擎收尾：向 {len(sent)}/{len(pids)} 个进程发 CTRL_BREAK，最多等 {wait_sec}s 让它落盘")
    t0 = time.time()
    while time.time() - t0 < wait_sec:
        if not _strata_pids():
            tray_log(f"引擎已自行退出（{time.time() - t0:.1f}s），状态已落盘")
            return []
        time.sleep(0.5)
    left = _strata_pids()
    tray_log(f"引擎 {wait_sec}s 内未退净，剩 {len(left)} 个，改强杀")
    return left


def stop_all():
    global _tray_stop_ts
    _tray_stop_ts = time.time()
    for name in ("gw", "tools", "vec"):
        stop_owned(name)
    _kill_by_cmdline("Strata-main")
    # 补刀：launcher.py 抢先拉起的 gateway_8081.py（命令行含 Strata-custom）不是托盘
    # 亲儿子，光靠 stop_owned 杀不到；只精确匹配 python.exe，llama 栈的代理
    # （命令行含 llama-bin）与托盘自身（pythonw.exe）均不受影响。
    _kill_by_cmdline("Strata-custom")
    # 最后处理引擎：先发 CTRL_BREAK 让它走自己的收尾（把 expert profile 落盘），
    # 等不到才强杀。放在 launcher 之后，免得 launcher 又把它拉起来。
    survivors = _graceful_stop_strata(30)
    cflags = 0x08000000 if sys.platform == "win32" else 0
    sinfo = None
    if sys.platform == "win32":
        sinfo = subprocess.STARTUPINFO()
        sinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        sinfo.wShowWindow = 0
    if survivors:
        for img in ("strata.exe", "strata-vision.exe"):
            try:
                subprocess.run(["taskkill", "/IM", img, "/F"],
                               capture_output=True, timeout=15,
                               creationflags=cflags, startupinfo=sinfo)
            except Exception:
                pass
    tray_log("全部服务已停止")
    return "stopped"


def _strata_console_alive():
    """检测黑窗、启动器或底层推理引擎是否存活（psutil直查，杜绝误报）。"""
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (p.info["name"] or "").lower()
                if name in ("strata.exe", "strata-vision.exe"):
                    return True
                if name == "python.exe":
                    cmd = " ".join(p.info["cmdline"] or []).lower()
                    if any(k in cmd for k in ("setup.py", "launcher.py", "server.py", "strata-main")):
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False
    except Exception:
        return True  # 查不到就当还在，宁可不自杀


def open_page(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


# ---------------- 托盘 ----------------
_state = {"main": "?", "vec": "?", "tools": "?", "gw": "?"}
_icon = None
DOT = {"ok": "●", "starting": "◐", "down": "○", "todo": "–", "?": "…", "off": "–"}


def autostart_all(source=""):
    global _last_autostart
    if time.time() - _last_autostart < 60:
        tray_log("autostart 距上次不足 60 秒，忽略重复触发")
        return "throttled"
    _last_autostart = time.time()
    tray_log(f"autostart{('（' + source + '）') if source else ''}：向量→工具→网关")
    if ENABLE_VEC:
        start_vec()
    start_tools()
    start_gw()


SHIP_STATE = os.path.join(LOGS_DIR, ".shipstate.json")
SHIP_GLOB = r"H:\Strata-main\log\8083_engine_*.log"


def ship_engine_log():
    """主脑引擎日志搬运：把 log 目录下 8083_engine_*.log（0.1.41 新位置）新增的字节，
    按日累加进 gateway/logs/8083_main_YYYYMMDD.log。断点续传，不丢不重。"""
    try:
        import glob as _glob
        cands = sorted(_glob.glob(SHIP_GLOB), key=lambda p: os.path.getmtime(p))
        if not cands:
            return
        src = cands[-1]
        size = os.path.getsize(src)
        try:
            st = json.load(open(SHIP_STATE, encoding="utf-8"))
        except Exception:
            st = {}
        if st.get("path") != src:
            # 新文件：1MB 以内全搬，更大的只搬末尾 200KB 当上下文
            off = 0 if size < 1024 * 1024 else max(0, size - 200 * 1024)
        elif size < st.get("offset", 0):
            off = 0  # 文件被截断/重建：从头搬
        else:
            off = st["offset"]
        if size > off:
            with open(src, "rb") as f:
                f.seek(off)
                chunk = f.read()
            if chunk:
                day = time.strftime("%Y%m%d")
                with open(os.path.join(LOGS_DIR, f"8083_main_{day}.log"),
                          "ab") as f:
                    f.write(chunk if chunk.endswith(b"\n") else chunk + b"\n")
            off = size
        json.dump({"path": src, "offset": off},
                  open(SHIP_STATE, "w", encoding="utf-8"))
    except Exception:
        pass


_main_down_streak = 0


def poll_loop():
    global _main_was_up, _main_down_streak
    while True:
        try:
            ship_engine_log()
            if os.path.exists(AUTOSTART_TRIGGER):
                try:
                    os.remove(AUTOSTART_TRIGGER)
                except Exception:
                    pass
                autostart_all("触发文件")
            _state["main"] = check_main()
            _state["vec"] = check_vec()
            _state["tools"] = check_tools()
            _state["gw"] = check_gw()
            if _state["main"] in ("ok", "starting"):
                _main_was_up = True
                _main_down_streak = 0
            elif _main_was_up and _state["main"] == "down":
                _main_down_streak += 1
                # 必须连续 4 次检测（20秒）且确认底层任何主脑进程均已彻底退出，才执行退出停机
                if (_main_down_streak >= 4
                        and not _tray_initiated_recent(sec=30)
                        and not _strata_console_alive()):
                    tray_log("黑窗与主脑引擎确认彻底退出：连带停止全部服务并退出托盘")
                    try:
                        stop_all()
                    finally:
                        if _icon:
                            _icon.stop()
                    return
            if _icon:
                parts = [f"主脑{DOT[_state['main']]}", f"向量{DOT[_state['vec']]}",
                         f"工具{DOT[_state['tools']]}", f"网关{DOT[_state['gw']]}"]
                try:
                    _icon.title = "Strata智能栈 | " + " ".join(parts)
                    _icon.update_menu()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(5)


def _open_logs():
    # 统一日志目录：H:\Strata-custom\log（8081/8083/8086/8087/托盘 全在这里）
    try:
        os.startfile(LOGS_DIR)
    except Exception:
        pass


def build_menu():
    from pystray import Menu, MenuItem
    return Menu(
        MenuItem("打开聊天 :8083", lambda: open_page(f"http://127.0.0.1:{PORTS['main']}/")),
        MenuItem("打开看板 :8081", lambda: open_page(f"http://127.0.0.1:{PORTS['gw']}/dashboard")),
        MenuItem("查看工具 :8087", lambda: open_page(f"http://127.0.0.1:{PORTS['tools']}/tools/list")),
        MenuItem("查看日志", lambda: _open_logs()),
        Menu.SEPARATOR,
        MenuItem("退出（保存后全停）", lambda i: threading.Thread(target=_quit, daemon=True).start()),
    )


def _quit():
    try:
        stop_all()
    finally:
        if _icon:
            try:
                _icon.stop()
            except Exception as e:
                tray_log(f"托盘退出异常：{e}")


def make_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), (17, 24, 39))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, 58, 58], radius=12, fill=(37, 99, 235))
    d.ellipse([24, 24, 40, 40], fill=(34, 197, 94))
    return img


def run_tray():
    global _icon
    try:
        lock = socket.socket()
        lock.bind(("127.0.0.1", TRAY_MUTEX_PORT))
    except Exception:
        return  # 已有托盘实例，直接退出
    from pystray import Icon
    tray_log("托盘管家启动")
    threading.Thread(target=poll_loop, daemon=True).start()
    if "--autostart" in sys.argv:
        def _auto():
            time.sleep(3)
            autostart_all("启动参数")
        threading.Thread(target=_auto, daemon=True).start()
    _icon = Icon("strata-stack", make_icon(), "Strata智能栈 | 探测中…", build_menu())
    _icon.run()


def run_selftest():
    """自检：起 8086+8087，健康通过后全停。不碰主脑（50GB 太重）。"""
    print("== stack_tray selftest ==")
    ok = True
    r1, r2 = start_vec(), start_tools()
    print(f"start_vec={r1} start_tools={r2}")
    for i in range(30):
        time.sleep(2)
        v, t = check_vec(), check_tools()
        print(f"  t+{(i + 1) * 2}s vec={v} tools={t}")
        if v == "ok" and t == "ok":
            break
    else:
        ok = False
    stop_owned("tools")
    stop_owned("vec")
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else (run_tray() or 0))
