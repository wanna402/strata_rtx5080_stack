# -*- coding: utf-8 -*-
"""
===============================================================================
  🔌 工具平面常驻统一网关 · Tool Plane 8087 (v1.2)
  • 端口矩阵：8081 网关(路由平面) · 8083 主脑 · 8085 视觉
    · 8087 工具平面(MCP插件 + 本地技能统一注册表)
  • 工具平面只干工具平面的事：注册、发现、执行、健康检查。
    主网关脚本 (qwen_tool_proxy.py) 只做动态挂载与路由，零工具硬编码
    （解耦：本文件独立运行、独立调试，报错只看 8087 日志）。
  • 分类规划（category 即能力域，网关按域路由、每次只暴露相关域）：
      memory    记忆召回    memory-recall     (local-skill)
      billing   费用账本    billing-stats     (local-skill)
      time      时间        clock             (local-skill)
      workspace 工作区      workspace-read    (local-skill)
                            + anytxt          (mcp-stdio)
      network   联网搜索    web-search 系      (mcp-stdio，按需替换)
      text      文本压缩    history-compact   (网关内联，不占表)
  • 类型字段 type：mcp-stdio = 独立子进程（慢/可崩，隔离保主链）；
    local-skill = 本进程函数（毫秒级，崩不了）。
  • v1.2 新增标准 MCP 端点 POST /mcp（Streamable HTTP，无状态 JSON）：
    initialize / notifications / ping / tools/list / tools/call 全量支持；
    mcp-stdio 子进程桥已接入（按需拉起、tools/list 缓存、崩溃自动重拉一次），
    桥接工具名 = <server>__<tool>。旧私有端点 /health /tools/list /tools/call
    原样保留，互不影响。DSH MCP连接器 直连 http://127.0.0.1:8087/mcp。
  • 配置（全部外置，代码零写死）：
      mcp:   mcp_servers.local.json > mcp_servers.example.json
             > qidongqi/mcp-servers.json（旧版只读兼容）
      skill: skills_servers.local.json > skills_servers.example.json
  • 日志铁律：logs/8087_mcp_YYYYMMDD.log 单日单文件 append 累加，
    严禁拆分 out/err。
  • 安全：只绑 127.0.0.1；workspace-read 沙盒只读。
  • 独立调试（换机器直接跑，无需启动器）：
      python mcp_gateway_8087.py --selftest        # 只印注册表+健康，不绑端口
      python mcp_gateway_8087.py --port 8087       # 常驻
      curl http://127.0.0.1:8087/health
      curl http://127.0.0.1:8087/tools/list
      # MCP 标准端点自测（initialize）：
      # curl -X POST http://127.0.0.1:8087/mcp -H "Content-Type: application/json" ^
      #   -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}"
===============================================================================
"""

import atexit
import datetime
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
LOGS_DIR = os.path.join(ROOT_DIR, "log")
os.makedirs(LOGS_DIR, exist_ok=True)

PORT = 8087
HOST = "127.0.0.1"
SELFTEST = False
_i = 0
while _i < len(sys.argv):
    _a = sys.argv[_i]
    if _a == "--selftest":
        SELFTEST = True
    elif _a.startswith("--port="):
        try:
            PORT = int(_a.split("=", 1)[1])
        except Exception:
            pass
    elif _a == "--port" and _i + 1 < len(sys.argv):
        try:
            PORT = int(sys.argv[_i + 1])
        except Exception:
            pass
    _i += 1


def _expand(s, ctx):
    if not isinstance(s, str):
        return s
    for k, v in ctx.items():
        s = s.replace("${" + k + "}", v)
    return s


def _ctx():
    return {
        "PYTHON": sys.executable,
        "BASE_DIR": BASE_DIR.replace("\\", "/"),
        "MODELS_DIR": os.path.join(BASE_DIR, "models").replace("\\", "/"),
    }


def load_mcp_servers():
    """MCP 插件表：local 覆盖 example，同名按 name 合并；旧版 key 格式只读兼容。"""
    ctx = _ctx()
    merged, order = {}, []
    for fname in ("mcp_servers.example.json", "mcp_servers.local.json"):
        fpath = os.path.join(BASE_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for srv in json.load(f).get("servers", []):
                    name = srv.get("name", "")
                    if not name:
                        continue
                    item = dict(srv)
                    item["type"] = "mcp-stdio"
                    item["command"] = _expand(srv.get("command", ""), ctx)
                    item["args"] = [_expand(a, ctx) for a in (srv.get("args") or [])]
                    item["source"] = fname
                    if name not in merged:
                        order.append(name)
                    merged[name] = item
        except Exception as e:
            log_line(f"[WARN] 读取 {fname} 失败: {e}")
    if not merged:
        legacy = os.path.join(BASE_DIR, "qidongqi", "mcp-servers.json")
        if os.path.exists(legacy):
            try:
                with open(legacy, "r", encoding="utf-8") as f:
                    for name, srv in json.load(f).get("mcpServers", {}).items():
                        merged[name] = {
                            "name": name, "type": "mcp-stdio", "enabled": True,
                            "category": "legacy",
                            "command": _expand(srv.get("command", ""), ctx),
                            "args": [_expand(a, ctx) for a in (srv.get("args") or [])],
                            "env": srv.get("env", {}), "timeout": 30,
                            "desc": "旧版兼容导入", "source": "legacy",
                        }
                        order.append(name)
            except Exception as e:
                log_line(f"[WARN] 读取旧版 mcp-servers.json 失败: {e}")
    return [merged[n] for n in order]


def load_skills():
    """本地技能表：local 覆盖 example，同名按 name 合并。"""
    merged, order = {}, []
    for fname in ("skills_servers.example.json", "skills_servers.local.json"):
        fpath = os.path.join(BASE_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for sk in json.load(f).get("skills", []):
                    name = sk.get("name", "")
                    if not name:
                        continue
                    item = dict(sk)
                    item["type"] = "local-skill"
                    item["source"] = fname
                    if name not in merged:
                        order.append(name)
                    merged[name] = item
        except Exception as e:
            log_line(f"[WARN] 读取 {fname} 失败: {e}")
    return [merged[n] for n in order]


def load_registry():
    """统一注册表：MCP 插件 + 本地技能，同名 skill 优先（技能短路插件）。"""
    reg, seen = [], set()
    for sk in load_skills():
        reg.append(sk)
        seen.add(sk["name"])
    for srv in load_mcp_servers():
        if srv["name"] not in seen:
            reg.append(srv)
    return reg


def command_available(cmd):
    if not cmd:
        return False
    if os.path.isabs(cmd) or os.sep in cmd or "/" in cmd:
        return os.path.exists(cmd)
    return shutil.which(cmd) is not None


LOG_FP = None


def log_line(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        if LOG_FP:
            LOG_FP.write(line + "\n")
            LOG_FP.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# local-skill 执行器（本进程内，毫秒级；失败一律结构化返回，不抛异常）
# ---------------------------------------------------------------------------

def _skill_clock(_args):
    now = datetime.datetime.now().astimezone()
    return {"ok": True, "iso": now.isoformat(timespec="seconds"),
            "weekday": now.strftime("%A"), "tz": str(now.tzinfo)}


def _skill_billing_stats(_args):
    fpath = os.path.join(BASE_DIR, "token_billing_stats.json")
    if not os.path.exists(fpath):
        return {"ok": False, "error": "no_billing_file",
                "hint": "账本尚未生成（网关跑起来后自动落盘），非故障"}
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        keys = list(data.keys()) if isinstance(data, dict) else []
        return {"ok": True, "size_bytes": os.path.getsize(fpath),
                "top_keys": keys[:10], "has_data": True}
    except Exception as e:
        return {"ok": False, "error": "billing_read_failed", "reason": str(e)}


def _skill_workspace_read(args):
    rel = (args.get("path") or args.get("file") or "").strip()
    if not rel:
        return {"ok": False, "error": "missing_path", "hint": "传参 path（相对根目录）"}
    # 沙盒：禁绝对路径、禁 ..、禁隐藏段/.git/__pycache__/node_modules
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    banned = {".git", "__pycache__", "node_modules"}
    if (os.path.isabs(rel) or ".." in parts or any(p.startswith(".") or p in banned for p in parts)):
        return {"ok": False, "error": "path_denied", "hint": "沙盒只读：根目录内非隐藏文件"}
    fpath = os.path.join(BASE_DIR, *parts)
    if not os.path.isfile(fpath):
        return {"ok": False, "error": "not_found", "path": "/".join(parts)}
    if os.path.getsize(fpath) > 65536:
        return {"ok": False, "error": "too_large", "hint": "单文件≤64KB，超限请分段读"}
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            return {"ok": True, "path": "/".join(parts), "content": f.read()}
    except Exception as e:
        return {"ok": False, "error": "read_failed", "reason": str(e)}


def _skill_memory_recall(args):
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"ok": False, "error": "missing_query", "hint": "传参 query"}
    try:
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)
        import personal_memory as pm
        limit = int(args.get("limit", 3))
        hits = pm.recall_memory(query, limit=max(1, min(5, limit)))
        return {"ok": True, "query": query, "hits": hits}
    except Exception as e:
        return {"ok": False, "error": "recall_unavailable",
                "reason": f"{type(e).__name__}: {e}",
                "hint": "记忆检索依赖缺失（长期记忆写入端未实现），非工具平面故障"}


def _strip_tags(s):
    return html_mod.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _skill_web_search(args):
    """Bing 网页搜索（本地技能：无 Key、不用代理、实测 0.4s）。

    为什么不用 ddg-search：本机公网实测 DuckDuckGo / Google 直连都超时
    （16s 不可达），Bing 国内直连 0.4s 就出结果。ddg 仍留在
    mcp_servers.local.json，只是 enabled=false；哪天挂了代理再一键开回。
    失败也必须**快**：10s 封顶，并把"换方案、别重试"写进返回文本。
    """
    args = args if isinstance(args, dict) else {}
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"ok": False, "error": "missing_query", "hint": "传参 query"}
    try:
        limit = max(1, min(10, int(args.get("limit") or 5)))
    except Exception:
        limit = 5
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-CN"
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": "fetch_failed",
                "reason": f"{type(e).__name__}: {e}",
                "elapsed_ms": int((time.time() - t0) * 1000),
                "hint": "Bing 不可达；改用 harness 自带 web 搜索/抓取，不要重复同一个查询"}
    blocks = re.findall(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', page, re.S)
    results = []
    for b in blocks:
        if len(results) >= limit:
            break
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        p = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        results.append({
            "title": _strip_tags(m.group(2)),
            "url": html_mod.unescape(m.group(1)),
            "snippet": _strip_tags(p.group(1))[:240] if p else "",
        })
    out = {"ok": bool(results), "engine": "bing", "query": query,
           "elapsed_ms": int((time.time() - t0) * 1000),
           "count": len(results), "results": results}
    if not results:
        out["error"] = "no_results"
        out["hint"] = "Bing 页面结构可能变了/被拦截；改用 harness 自带 web 搜索"
    return out


SKILL_HANDLERS = {
    "clock": _skill_clock,
    "billing_stats": _skill_billing_stats,
    "workspace_read": _skill_workspace_read,
    "memory_recall": _skill_memory_recall,
    "web_search": _skill_web_search,
}


# ---------------------------------------------------------------------------
# mcp-stdio 子进程桥：每个插件一个常驻 stdio 子进程，按需拉起、崩溃自动重拉。
# MCP stdio 传输 = 换行分隔 JSON-RPC。tools/list 缓存；tools/call 直通。
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2025-03-26"   # 与 DSH MCP连接器 探测版本一致
MCP_SESSION_ID = uuid.uuid4().hex     # 无状态会话 id，仅原样回显给客户端


class McpTimeout(RuntimeError):
    """stdio 子进程在 timeout 内没回话。

    单独一种异常，是为了让 _with_retry 区分"网络卡死"和"进程崩了"：
    崩了值得重拉重试一次；卡死重试只会把等待翻倍（实测 19:42 两次 30s 就是这么来的）。
    """


class StdioBridge:
    def __init__(self, srv):
        self.srv = srv
        self.proc = None
        self.tools = None
        self.next_id = 0
        self.pending = {}
        self._pmx = threading.Lock()      # 保护 pending 表
        self._rpc = threading.Lock()      # 串行化整个 RPC 往返
        self._start_lock = threading.Lock()

    # -- 进程与读线程 ------------------------------------------------------
    def _env(self):
        ctx = _ctx()
        env = os.environ.copy()
        for k, v in (self.srv.get("env") or {}).items():
            env[str(k)] = _expand(str(v), ctx)
        return env

    def _pump(self):
        """stdout 逐行读取线程：响应按 id 投递；管道断开则唤醒全部等待者。"""
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is None or "method" in msg:
                    continue  # 通知/服务端请求：本桥不需要，忽略
                with self._pmx:
                    slot = self.pending.get(mid)
                    if slot:
                        slot[1] = msg
                if slot:
                    slot[0].set()
        except Exception as e:
            log_line(f"[WARN] stdio 读线程异常 {self.srv['name']}: {e}")
        with self._pmx:
            for slot in self.pending.values():
                slot[0].set()
            self.pending.clear()

    def _kill(self):
        p, self.proc, self.tools = self.proc, None, None
        if not p:
            return
        try:
            p.kill()
        except Exception:
            pass

    def _start(self):
        cmd = self.srv.get("command", "")
        if not command_available(cmd):
            raise RuntimeError(f"命令不存在: {cmd}")
        self.proc = subprocess.Popen(
            [cmd] + list(self.srv.get("args") or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=self._env(),
            cwd=self.srv.get("cwd") or BASE_DIR,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._pump, daemon=True).start()
        boot = max(int(self.srv.get("timeout") or 30), 120)  # uvx 首跑要下载
        self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "tool-plane-8087-bridge", "version": "1.2"},
        }, boot)
        self._notify("notifications/initialized")
        res = self._request("tools/list", {}, int(self.srv.get("timeout") or 30))
        self.tools = res.get("tools", [])
        log_line(f"[BRIDGE] {self.srv['name']} 子进程就绪 pid={self.proc.pid} "
                 f"tools={[t.get('name') for t in self.tools]}")

    def _ensure(self):
        with self._start_lock:
            if self.proc is not None and self.proc.poll() is None and self.tools is not None:
                return
            self._kill()
            self._start()

    # -- JSON-RPC 原语（stdio：换行分隔） ----------------------------------
    def _send(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _request(self, method, params, timeout=30):
        with self._rpc:
            self._ensure_started_inline()
            self.next_id += 1
            mid = self.next_id
            ev = threading.Event()
            with self._pmx:
                self.pending[mid] = [ev, None]
            try:
                self._send({"jsonrpc": "2.0", "id": mid,
                            "method": method, "params": params})
            except Exception as e:
                with self._pmx:
                    self.pending.pop(mid, None)
                self._kill()
                raise RuntimeError(f"stdio 写入失败: {e}")
            if not ev.wait(timeout):
                with self._pmx:
                    self.pending.pop(mid, None)
                self._kill()
                raise McpTimeout(
                    f"{self.srv.get('name')} 超时 {timeout}s（网络或代理没响应）。"
                    f"不要再重试同一个请求，改用其他搜索/抓取方式。")
            with self._pmx:
                slot = self.pending.pop(mid, None)
            resp = slot[1] if slot else None
            if resp is None:
                self._kill()
                raise RuntimeError(f"stdio 子进程断开: {method}")
            if "error" in resp:
                err = resp["error"]
                raise RuntimeError(f"{method} 出错: {err.get('message', err)}")
            return resp.get("result", {})

    def _ensure_started_inline(self):
        # _request 内部自启用：initialize/tools/list 走这里时不再递归 _ensure
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("stdio 子进程未启动")

    # -- 对外接口（崩溃才重拉重试；超时不重试，直接交回上层换方案） ---------
    def _with_retry(self, fn):
        self._ensure()
        try:
            return fn()
        except McpTimeout as e:
            # 超时基本都是网络/代理卡住，重试 = 把等待翻倍（30s 变 60s），
            # 而且同一个查询大概率还是超时。直接杀掉子进程（下次调用是干净进程），
            # 把可执行的建议原样抛给模型，让它换工具。
            log_line(f"[TIMEOUT] {self.srv['name']}：{e}；不重试，已重启子进程")
            with self._start_lock:
                self._kill()
            raise
        except Exception as e:
            log_line(f"[WARN] {self.srv['name']} 调用失败，重拉子进程重试: {e}")
            with self._start_lock:
                self._kill()
            self._ensure()
            return fn()

    def list_tools(self):
        def go():
            if self.tools is None:  # 握手在 _start 里已完成，这里只兜底
                self.tools = self._request(
                    "tools/list", {}, int(self.srv.get("timeout") or 30)
                ).get("tools", [])
            return self.tools
        return self._with_retry(go)

    def call_tool(self, tool, args):
        def go():
            return self._request("tools/call",
                                 {"name": tool, "arguments": args or {}},
                                 int(self.srv.get("timeout") or 30))
        return self._with_retry(go)


BRIDGES = {}
_BRIDGES_LOCK = threading.Lock()


def get_bridge(srv):
    with _BRIDGES_LOCK:
        br = BRIDGES.get(srv["name"])
        if br is None:
            br = StdioBridge(srv)
            BRIDGES[srv["name"]] = br
        return br


@atexit.register
def _kill_all_bridges():
    with _BRIDGES_LOCK:
        for br in BRIDGES.values():
            try:
                br._kill()
            except Exception:
                pass


def mcp_tool_list():
    """MCP tools/list：本地技能 + mcp-stdio 桥接工具（名 = <server>__<tool>）。
    桥接失败的插件只记日志跳过（诚实降级），不拖垮整张表。"""
    tools = []
    for s in load_registry():
        if not s.get("enabled"):
            continue
        if s.get("type") == "local-skill":
            if s.get("handler") not in SKILL_HANDLERS:
                continue
            tools.append({
                "name": s["name"],
                "description": s.get("desc", s["name"]),
                "inputSchema": s.get("tool_schema")
                or {"type": "object", "properties": {}},
            })
        else:
            try:
                for t in get_bridge(s).list_tools():
                    tools.append({
                        "name": f"{s['name']}__{t['name']}",
                        "description": (t.get("description")
                                        or t.get("title")
                                        or s.get("desc", ""))[:2048],
                        "inputSchema": t.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    })
            except Exception as e:
                log_line(f"[WARN] /mcp tools/list 桥接 {s['name']} 失败: {e}")
    return tools


def mcp_tool_call(name, args):
    """MCP tools/call 分发：返回 {content,isError}，失败一律结构化不抛。"""
    reg = {s["name"]: s for s in load_registry()}
    args = args if isinstance(args, dict) else {}
    if "__" in name:
        head, _, tail = name.partition("__")
        srv = reg.get(head)
        if srv and srv.get("type") == "mcp-stdio":
            if not srv.get("enabled"):
                return {"content": [{"type": "text",
                                     "text": f"server {head} 已禁用"}],
                        "isError": True}
            try:
                return get_bridge(srv).call_tool(tail, args)
            except Exception as e:
                return {"content": [{"type": "text",
                                     "text": f"桥接 {head} 失败: {e}"}],
                        "isError": True}
    srv = reg.get(name)
    if not srv or srv.get("type") != "local-skill":
        return {"content": [{"type": "text",
                             "text": f"未知工具: {name}"}], "isError": True}
    if not srv.get("enabled"):
        return {"content": [{"type": "text",
                             "text": f"工具 {name} 已禁用"}], "isError": True}
    fn = SKILL_HANDLERS.get(srv.get("handler", ""))
    if not fn:
        return {"content": [{"type": "text",
                             "text": f"handler 缺失: {name}"}], "isError": True}
    try:
        res = fn(args)
    except Exception as e:
        res = {"ok": False, "error": "handler_crashed", "reason": str(e)}
    return {"content": [{"type": "text",
                         "text": json.dumps(res, ensure_ascii=False)}],
            "isError": not res.get("ok")}


def _mcp_rpc_one(msg):
    """处理单条 JSON-RPC：None=通知已吞（204），dict=需回 JSON。"""
    if not isinstance(msg, dict) or "method" not in msg:
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    method = msg.get("method", "")
    mid = msg.get("id")
    is_notice = mid is None
    if method == "initialize":
        if is_notice:
            return None
        want = (msg.get("params") or {}).get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if isinstance(want, str) else MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tool-plane-8087", "version": "1.2"},
            "instructions": "工具平面 8087：本地技能直名调用；"
                            "MCP 插件工具以 <server>__<tool> 命名。"}}
    if method.startswith("notifications/"):
        return None
    if is_notice:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": mcp_tool_list()}}
    if method == "tools/call":
        p = msg.get("params") or {}
        res = mcp_tool_call(str(p.get("name", "")), p.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


class ToolPlaneHandler(BaseHTTPRequestHandler):
    server_version = "ToolPlane8087/1.2"

    def log_message(self, fmt, *args):
        log_line(f"[HTTP] {self.address_string()} {fmt % args}")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(body.__len__()))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/mcp":
            # 本服务器不提供 SSE 流：按 MCP Streamable HTTP 规范回 405
            self._send(405, {"error": "sse_not_supported",
                             "hint": "请用 POST /mcp 收 JSON 响应"})
            return
        if self.path in ("/health", "/healthz"):
            reg = load_registry()
            on = [s for s in reg if s.get("enabled")]
            cats = {}
            for s in on:
                cats[s.get("category", "misc")] = cats.get(s.get("category", "misc"), 0) + 1
            self._send(200, {"status": "ok", "port": PORT,
                             "servers_total": len(reg), "servers_enabled": len(on),
                             "by_category": cats,
                             "servers": [s["name"] for s in on]})
        elif self.path in ("/tools/list", "/tools", "/list"):
            reg = load_registry()
            out = []
            for s in reg:
                item = {"name": s["name"], "type": s.get("type", "?"),
                        "category": s.get("category", "misc"),
                        "enabled": bool(s.get("enabled")),
                        "desc": s.get("desc", ""), "source": s.get("source", "")}
                if s.get("type") == "mcp-stdio":
                    item["command_available"] = command_available(s.get("command", ""))
                else:
                    item["handler_ready"] = s.get("handler") in SKILL_HANDLERS
                out.append(item)
            self._send(200, {"servers": out})
        elif self.path in ("/", "/dashboard"):
            reg = load_registry()
            self._send(200, {"service": "tool-plane-8087", "status": "ok",
                             "endpoints": ["/health", "/tools/list", "/tools/call",
                                           "/mcp (MCP Streamable HTTP)"],
                             "servers": [s["name"] for s in reg if s.get("enabled")]})
        else:
            self._send(404, {"error": "not_found", "path": self.path})

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/mcp":
            self._handle_mcp()
            return
        if self.path in ("/tools/call", "/call"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                payload = {}
            name = payload.get("server") or payload.get("name") or payload.get("tool") or ""
            reg = {s["name"]: s for s in load_registry()}
            srv = reg.get(name)
            if not srv:
                self._send(404, {"error": "server_not_found", "server": name,
                                 "available": sorted(reg)})
                return
            if not srv.get("enabled"):
                self._send(403, {"error": "server_disabled", "server": name})
                return
            if srv.get("type") == "local-skill":
                fn = SKILL_HANDLERS.get(srv.get("handler", ""))
                if not fn:
                    self._send(501, {"error": "handler_missing", "server": name})
                    return
                try:
                    res = fn(payload.get("args", {}) if isinstance(payload.get("args"), dict) else payload)
                except Exception as e:
                    res = {"ok": False, "error": "handler_crashed", "reason": str(e)}
                self._send(200 if res.get("ok") else 503, {"server": name, "result": res})
            else:
                # v1.2：mcp-stdio 直通已接入（走同一个子进程桥，与 /mcp 共享）
                try:
                    res = get_bridge(srv).call_tool(
                        str(payload.get("tool_name") or ""),
                        payload.get("args", {}) if isinstance(payload.get("args"), dict) else {})
                except Exception as e:
                    res = {"ok": False, "error": "bridge_failed", "reason": str(e)}
                self._send(200 if not res.get("isError") else 502,
                           {"server": name, "result": res})
        else:
            self._send(404, {"error": "not_found", "path": self.path})

    # -- MCP Streamable HTTP（标准端点，无状态 JSON） ----------------------
    def _send_mcp(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Mcp-Session-Id", MCP_SESSION_ID)
        if obj is None:
            self.send_header("Content-Length", "0")
        else:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if body:
                self.wfile.write(body)
        except Exception:
            pass

    def _handle_mcp(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            raw = self.rfile.read(length) if length else b""
        except Exception:
            raw = b""
        try:
            parsed = json.loads(raw.decode("utf-8") or "null")
        except Exception:
            self._send_mcp(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
            return
        if parsed is None:
            self._send_mcp(202)
            return
        msgs = parsed if isinstance(parsed, list) else [parsed]
        replies = [r for r in (_mcp_rpc_one(m) for m in msgs) if r is not None]
        if not replies:
            self._send_mcp(202)
            return
        self._send_mcp(200, replies if isinstance(parsed, list) else replies[0])

    def do_DELETE(self):
        if self.path.split("?", 1)[0] == "/mcp":
            self._send_mcp(204)   # 会话终止：无状态，直接吞
        else:
            self._send(404, {"error": "not_found", "path": self.path})

    def do_OPTIONS(self):
        if self.path.split("?", 1)[0] == "/mcp":
            self.send_response(204)
            self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Accept, Mcp-Session-Id, "
                             "Mcp-Protocol-Version, Last-Event-ID")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(404, {"error": "not_found", "path": self.path})


def run_selftest():
    """独立调试入口：不绑端口，只印注册表 + 健康 + 本地技能实测，换机器排错先跑它。"""
    print("== Tool Plane 8087 selftest ==")
    print(f"BASE_DIR={BASE_DIR}")
    reg = load_registry()
    on = [s for s in reg if s.get("enabled")]
    print(f"registry: total={len(reg)} enabled={len(on)}")
    ok = True
    for s in reg:
        if s.get("type") == "mcp-stdio":
            avail = command_available(s.get("command", ""))
            flag = "OK " if (avail or not s.get("enabled")) else "MISS"
            if s.get("enabled") and not avail:
                ok = False
            print(f"  [{flag}] mcp-stdio  {s['name']:14s} cat={s.get('category','misc'):9s} "
                  f"en={int(bool(s.get('enabled')))} cmd={s.get('command','')[:60]} src={s.get('source','')}")
        else:
            ready = s.get("handler") in SKILL_HANDLERS
            flag = "OK " if (ready or not s.get("enabled")) else "MISS"
            if s.get("enabled") and not ready:
                ok = False
            print(f"  [{flag}] local-skill {s['name']:14s} cat={s.get('category','misc'):9s} "
                  f"en={int(bool(s.get('enabled')))} handler={s.get('handler','')} src={s.get('source','')}")
    # 本地技能实测（clock 必过；billing/memory 按环境明示 503 也算正常）
    for name, args in (("clock", {}), ("billing_stats", {}),):
        fn = SKILL_HANDLERS[{"clock": "clock", "billing_stats": "billing_stats"}[name]]
        try:
            res = fn(args)
            print(f"  [..] skill {name}: ok={res.get('ok')} "
                  f"{str(res)[:120]}")
        except Exception as e:
            ok = False
            print(f"  [FAIL] skill {name}: {e}")
    print("SELFTEST " + ("PASS" if ok else "FAIL(有 MISS，多为本机缺命令，按行处理)"))
    return 0 if ok else 1


def main():
    global LOG_FP
    if SELFTEST:
        sys.exit(run_selftest())
    today = time.strftime("%Y%m%d")
    log_file = os.path.join(LOGS_DIR, f"8087_mcp_{today}.log")
    try:
        LOG_FP = open(log_file, "a", encoding="utf-8", buffering=1)
        LOG_FP.write(f"\n--- [8087 ToolPlane Session at {time.strftime('%Y-%m-%d %H:%M:%S')}] ---\n")
        LOG_FP.flush()
    except Exception:
        LOG_FP = None
    reg = load_registry()
    log_line(f"🔌 工具平面 8087 启动：{len(reg)} 项（{[s['name'] for s in reg]}），日志 {os.path.basename(log_file)}")
    httpd = ThreadingHTTPServer((HOST, PORT), ToolPlaneHandler)
    log_line(f"✅ 监听 http://{HOST}:{PORT}（仅本机）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        if LOG_FP:
            try:
                LOG_FP.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
