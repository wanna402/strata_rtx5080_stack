# -*- coding: utf-8 -*-
"""
Strata 智能栈前端网关 :8081 (gateway_8081.py, Phase 1)
=====================================================
唯一入口。所有前端（OpenCode / Claude Code / DeepSeek Harness / WebUI）只填：
  http://127.0.0.1:8081/v1
后端：主脑 Strata :8083（OpenAI + Anthropic + Responses 原生三协议，纯透传）

功能（Phase 1）：
  模型别名映射  云端别名/随便填 -> :8083 真实活跃模型（/v1/models 探测）
  Key 鉴权      miracle_api_keys.json（识别不拦截，和老网关一致）
  虚拟计费      token_billing_stats.json（和 :8087 billing_stats 技能同文件）
  防爆剪枝      转发前 count_tokens 估算，超 92% 上下文则掐中间、实在塞不下 413
  记忆注入      doctrines 常驻 + personal_memory 召回 top-2 拼进 system（只读）
  日志铁律      logs/8081_gateway_YYYYMMDD.log（准入/鉴权/异常）
                logs/8081_main_YYYYMMDD.log（每请求：key/模型/token/耗时/tps）
                logs/8081_sub_YYYYMMDD.log（Helper 预留，HELPER_URL 为空即不用）
  看板          /dashboard（四端口状态、今日分 Key 统计、最近请求、GPU 尽力而为）

Phase 2（缓存友好 · 可观测 · 2026-10-19）：
  档位粘住      同一会话沿用思考档位（只升不降）。reasoning_effort 会被模板写进
                system 消息头，中途翻转 = 整条前缀缓存作废；粘住后一次会话最多断一次。
  缓存 KPI      每请求记 cache=命中/总计 与复用率；复用骤降在 gateway 日志打 [CACHE] 断崖。
  虚拟档位      model=strata-fast / strata-code / strata-deep / strata-nothink，
                纯参数预设（/v1/models 里可见），最终都落到同一个后端模型。
  剪枝 v2       先压旧工具输出（不改条数/顺序，配对与 KV 前缀都保住），
                再按 user 轮边界整轮丢弃；仍超限才 413。
  死循环检测    同一工具调用连续 N 次 → 告警（默认）；STRATA_LOOP_BLOCK>0 时直接 409。
  断连日志      客户端中断 → [ABORT]，跳出循环关闭上游，停止空转生成。
  计费修正      命中缓存按 in_hit 计价；Anthropic 的 input_tokens/output_tokens 也认。
  环境变量      STRATA_EFFORT_STICKY=0 关粘住；STRATA_STRIP_THINKING=1 从历史消息摘掉思考块
                （实测引擎不认 preserve_thinking，只能在网关侧删）；
                STRATA_LOOP_WARN/BLOCK 调循环阈值；STRATA_CACHE_WARN/MIN 调断崖判定。

用法：
  python gateway_8081.py [--port 8081] [--backend http://127.0.0.1:8083]
  python gateway_8081.py --selftest   # 起服务自检 /health 后退出（不需主脑在线）
"""
import os
import sys
import re
import copy
import json
import time
import socket
import threading
import datetime
import html as _html
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
LOGS_DIR = os.path.join(ROOT_DIR, "log")
os.makedirs(LOGS_DIR, exist_ok=True)

PORT = 8081
BACKEND = os.environ.get("STRATA_URL", "http://127.0.0.1:8083")
HELPER_URL = os.environ.get("HELPER_URL", "")  # 副脑预留，为空即不用
SELFTEST = False
_i = 0
while _i < len(sys.argv):
    _a = sys.argv[_i]
    if _a == "--selftest":
        SELFTEST = True
    elif _a == "--port" and _i + 1 < len(sys.argv):
        try:
            PORT = int(sys.argv[_i + 1])
        except Exception:
            pass
        _i += 1
    elif _a == "--backend" and _i + 1 < len(sys.argv):
        BACKEND = sys.argv[_i + 1].rstrip("/")
        _i += 1
    _i += 1

# 虚拟计费（沿用老网关费率）
PRICING = {"in_hit_m": 0.05, "in_miss_m": 1.50, "out_m": 4.50, "image_each": 0.0015}
BILL_FILE = os.path.join(BASE_DIR, "token_billing_stats.json")
KEYS_FILE = os.path.join(BASE_DIR, "miracle_api_keys.json")
GUARD_RATIO = 0.92   # 估算超 max_ctx*92% 即剪枝
TARGET_RATIO = 0.80  # 剪枝目标水位
MAX_BODY = 50 * 1024 * 1024

# ---------------- 缓存友好 / 诊断开关（全部可用环境变量覆盖；默认不改变质量） ----------------
# reasoning_effort 会被模板渲染进 system 消息头，会话中途翻转 = 整个前缀缓存作废。
EFFORT_STICKY = os.environ.get("STRATA_EFFORT_STICKY", "1") != "0"
EFFORT_TTL_S = int(os.environ.get("STRATA_EFFORT_TTL", "3600"))
# 1 = 让模板丢掉历史轮的思考块（当前轮保留）；默认关，先量再说
STRIP_THINKING = os.environ.get("STRATA_STRIP_THINKING", "0") == "1"
LOOP_WARN = int(os.environ.get("STRATA_LOOP_WARN", "3"))    # 连续同一工具调用 N 次 → 告警
LOOP_BLOCK = int(os.environ.get("STRATA_LOOP_BLOCK", "0"))  # >0 时到 N 次直接 409
CACHE_WARN_RATIO = float(os.environ.get("STRATA_CACHE_WARN", "0.5"))   # 复用掉到上次的 50% 视为断崖
CACHE_WARN_MIN = int(os.environ.get("STRATA_CACHE_WARN_MIN", "20000")) # prompt 小于它不判断崖

# ---- 空回复 / 截断自动补刀（2026-10-20）------------------------------------
# 实测两次真实事故后加的兜底（决策记录见 launcher.py 顶部注释）：
#  1) 空回复：有 reasoning、没有 content、也没有 tool_calls，finish=stop。
#     DSH 会把这种回复当成"这一轮说完了"，直接把 turn 结束掉。
#  2) 截断：引擎把一次生成按 min(reasoning_budget_tokens, 0.75*max_tokens)
#     切块（实测 256→256+3092、6144→6144+2030、8192→6144+182），块边界正好
#     落在"思考刚结束、正文刚开始"时，续写块常常只写几十字就 EOS，
#     表现就是"出了个开头就断了"。
# 两种都在网关这里补一刀：拿原文再请求一次，只把新正文接上去，用户无感。
# 关掉：STRATA_CONT=0；最多补几刀：STRATA_CONT_MAX（默认 2）。
CONT_ENABLE = os.environ.get("STRATA_CONT", "1").lower() not in ("0", "false", "off", "no")
CONT_MAX_ROUNDS = max(0, int(os.environ.get("STRATA_CONT_MAX", "2")))
CONT_EMPTY_NUDGE = "继续。直接给出结果；需要调用工具就调用，否则直接输出正文。"
CONT_CUT_NUDGE = ("从上面断掉的位置继续写，紧接最后一个字符往下写；"
                  "不要重复已经写过的内容，不要重新开头，不要再解释，直接续写正文。")

# ---- 开工方式提示（2026-10-20）------------------------------------------------
# 用户诉求：每个新任务先看有哪些现成工具/技能/插件，挑最合适的，而不是上来就硬写
# 或者在同一棵树上吊死。DSH 本来就把工具清单、技能清单塞进 system，但它不主动"选"。
# 这里在 system **末尾**补一段固定指令（放末尾是因为离生成最近、照做率最高；
# 而且文字每次完全一样，前缀缓存照样命中，不会像动态召回那样把缓存打穿）。
# 关掉：STRATA_WORKSTYLE=0。
WORKSTYLE_ENABLE = os.environ.get("STRATA_WORKSTYLE", "1").lower() not in ("0", "false", "off", "no")
WORKSTYLE_MARK = "【开工方式】"
WORKSTYLE_TEXT = (
    "【开工方式】每次开始新任务时照做，不要再复述本段：\n"
    "1. 先挑工具再动手，优先用现成的（以你当前工具清单为准）：工具平面 :8087 有 clock(本机时间)、"
    "billing-stats(费用账本)、memory-recall(记忆召回)、workspace-read(只读读文件)、"
    "web-search(Bing 搜索，免代理)、ddg-search(DDG 搜索，需 Clash)；harness 自带 web 搜索/抓取、"
    "文件读写、shell、子代理，以及技能与插件清单。选最合适的一个，用一行说明理由即可，"
    "不要罗列清单、不要重复造轮子。\n"
    "2. 工具失败或超时：同一个工具最多重试 1 次；仍失败立即换备选方案"
    "（例如 ddg-search 超时 = Clash 没开，改用 web-search；web-search 没结果就改用 harness 自带搜索或直接抓网页）；"
    "连续 2 次失败就停下说明原因，禁止死循环。\n"
    "3. 需要 3 步以上的长任务：先用一句话说明“分几步、每步用什么工具”，再开始执行。"
)

# 虚拟模型档位：名字只是"参数预设"，最终都落到同一个后端模型（零运行时成本）
MODEL_PROFILES = {
    "strata-fast":    {"reasoning_effort": "low",  "reasoning_budget_tokens": 1024,
                       "temperature": 0.7, "top_p": 0.80, "top_k": 20, "presence_penalty": 0.0},
    "strata-code":    {"reasoning_effort": "low",  "reasoning_budget_tokens": 2560,
                       "temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0},
    "strata-deep":    {"reasoning_effort": "high", "reasoning_budget_tokens": 8192,
                       "temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0},
    "strata-nothink": {"enable_thinking": False,   "temperature": 0.7, "top_p": 0.80,
                       "top_k": 20, "presence_penalty": 1.5},
}

# ---------------- 自适应动态难度与思考采样引擎 v7.0（Flash-Next on Strata） ----------------
# 客户端显式指定的参数永远胜出；未指定时网关按上下文难度自适应匹配最佳思考深度与预算
TASK_MATRIX = {
    "coding":  {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 0.0, "reasoning_effort": "low", "reasoning_budget_tokens": 2560, "max_tokens": 8192},
    "math":    {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 0.0, "reasoning_effort": "high", "reasoning_budget_tokens": 6144, "max_tokens": 16384},
    "tools":   {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 0.0, "reasoning_effort": "low", "reasoning_budget_tokens": 1536, "max_tokens": 4096},
    "rag":     {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 0.0, "reasoning_effort": "low", "reasoning_budget_tokens": 2048, "max_tokens": 4096},
    "writing": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 0.0, "reasoning_effort": "high", "reasoning_budget_tokens": 6144, "max_tokens": 16384},
    "chat":    {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 1.5, "reasoning_effort": "low", "reasoning_budget_tokens": 1024, "max_tokens": 2048},
}
TASK_ALIAS = {"coding": ("code", "dev", "program", "编程", "开发", "代码"),
              "math": ("math", "数学", "逻辑", "logic", "reason"),
              "tools": ("tool", "json", "工具", "function", "agent"),
              "rag": ("rag", "知识", "检索", "search", "kb"),
              "writing": ("writ", "写作", "创作", "文学", "小说"),
              "chat": ("chat", "闲聊", "闲", "general", "default", "qa", "问答")}


def resolve_task(header_value):
    h = (header_value or "").strip().lower()
    if not h:
        return None
    for task, keys in TASK_ALIAS.items():
        if h == task or any(k in h for k in keys):
            return task
    return None


def _extract_msg_text(c):
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return str(c)


# ---- 注入块识别（2026-10-20 修）----------------------------------------
# DSH / 插件每轮都会在 user 消息末尾追加若干个"看不见的"块：运行上下文、技能
# 清单、hindsight 记忆提醒、时间快照、压缩检查点…… 它们也是 role=user，
# 所以旧的"取最后一条 user 消息"会把它们当成人话去匹配关键词。
# 实测事故：用户只打了一句"继续"，但后面跟的 hindsight 块里有
#   "ground yourself in this repo's architecture, conventions, ..."
# -> 命中 deep_keywords 里的 "architecture" -> 判定 deep_reasoning/high/6144
# -> 系统头里的档位翻转，整条 44.6K 前缀缓存作废（15 秒重算），
#    并且思考预算从 2048 抬到 6144，最终答案正好卡在 6144 的分块边界上被截断。
_INJECTED_PREFIXES = (
    "<system-reminder",                  # 技能目录 / 运行时提醒
    "<hindsight",                        # hindsight_knowledge / _refresh
    "<untrusted-memory",                 # 记忆原文包裹
    "<compacted-summary",                # 压缩检查点
    "<available_skills>",                # 技能清单正文
    "current runtime context",           # DSH 运行上下文快照
    "time sampled",                      # 时间快照
    "the approval policy changed",       # 权限变更通知
    "this is an automatically generated checkpoint",
)


def _is_injected_block(text):
    """是不是 harness/插件注入的块（而不是用户手打的字）。"""
    t = (text or "").lstrip().lower()
    if not t:
        return True
    return any(t.startswith(p) for p in _INJECTED_PREFIXES)


def detect_dynamic_difficulty(messages, tools=None):
    """
    智能上下文难度与任务阶段评估器 (Dynamic Difficulty Evaluator):
    根据当前请求的会话深度、上下文状态与问题复杂度，自适应匹配思考深度与思考预算：
    1. agent_tool: Agent 中间工具流转态 -> low, budget=1536 (快速决策下步工具，不冗余沉思)
    2. quick_reply: 短句确认、闲聊、简单问答、继续 -> low, budget=1024 (干脆利落秒级直达)
    3. deep_reasoning: 复杂数学证明、算法建模、底层架构设计、深度死锁审计 -> high, budget=6144 (硬核攻坚深度求解)
    4. coding_balanced: 常规编程、Excel/Word 表格自动化、脚本、bug修复 -> low, budget=2560 (甜点平衡：精炼思考+高速产出)
    5. general_balanced: 一般任务兜底 -> low, budget=2048
    """
    if not isinstance(messages, list) or not messages:
        return "general", "low", 2048

    last_msg = messages[-1] if messages else {}
    last_role = last_msg.get("role", "")
    last_content = _extract_msg_text(last_msg.get("content", "")).strip()

    # 1. 状态一：Agent 工具流转阶段 (Tool Loop)
    if last_role in ("tool", "function"):
        return "agent_tool", "low", 1536

    tool_markers = ("tool call", "tool result", "command output", "exit code",
                    "stdout:", "stderr:", "[tool_result]", "已执行", "输出结果")
    if last_role == "user" and any(last_content.lower().startswith(m) for m in tool_markers):
        return "agent_tool", "low", 1536

    if len(messages) >= 2:
        prev_msg = messages[-2]
        if prev_msg.get("role") == "assistant" and (prev_msg.get("tool_calls") or "```" in last_content):
            if len(last_content) > 800:
                return "agent_tool", "low", 1536

    # 查找最近的"人类真实提问"：跳过 harness / 插件注入的块。
    # 注入块也是 role=user，但里面的字不是用户打的，不能拿去匹配关键词。
    user_query = ""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        txt = _extract_msg_text(msg.get("content", "")).strip()
        if not txt:
            continue
        if _is_injected_block(txt):
            continue
        if any(txt.lower().startswith(tm) for tm in tool_markers):
            continue
        user_query = txt
        break

    if not user_query:
        return "general", "low", 2048

    query_len = len(user_query)
    q_low = user_query.lower()

    # 2. 状态二：短句确认、极简轻量任务
    quick_confirmations = ("继续", "好的", "行", "没问题", "确认", "下一步", "开始", "执行",
                           "可以", "按方案执行", "你好", "在吗", "hi", "hello", "ok", "yes", "proceed")
    if query_len <= 15 and any(q_low == c or q_low.startswith(c) for c in quick_confirmations):
        return "quick_reply", "low", 1024

    lightweight_keywords = ("翻译", "润色", "改错字", "转成json", "提炼摘要", "一句话总结", "提取字段", "列出目录", "重命名")
    if query_len < 100 and any(k in user_query for k in lightweight_keywords):
        return "lightweight", "low", 1024

    # 3. 状态三：复杂深度逻辑、高难度算法与数学证明
    deep_keywords = ("数学证明", "微分", "微积分", "动态规划", "算法复杂度", "推导",
                     "架构设计", "底层协议", "并发模型", "内存泄漏深度分析", "安全漏洞",
                     "死锁分析", "数学建模", "证明题", "proof", "architecture", "algorithm optimization")
    if any(k in user_query for k in deep_keywords):
        return "deep_reasoning", "high", 6144

    math_symbols = ("\\int", "\\sum", "\\prod", "O(N log N)", "O(N^2)", "\\frac")
    if any(s in user_query for s in math_symbols):
        return "math_logic", "high", 6144

    # 4. 状态四：常规开发与自动化办公表格任务 (甜点平衡: low + 2560 budget)
    coding_keywords = ("python", "code", "def ", "class ", "excel", "docx", "xlsx", "表格", "写代码", "修复", "bug", "脚本", "函数")
    if any(k in q_low for k in coding_keywords):
        return "coding_balanced", "low", 2560

    return "general_balanced", "low", 2048


# ---------------- 会话级思考档位粘住（保护前缀缓存） ----------------
_EFFORT_RANK = {"none": 0, "off": 0, "low": 1, "minimal": 1,
                "medium": 2, "high": 3, "xhigh": 3, "max": 3, "extreme": 3}
_conv_effort = {}
_conv_effort_lock = threading.Lock()


def _remember_effort(conv, effort, budget=0):
    """记住某会话的思考档位（客户端显式指定时用）。"""
    if not conv or conv == "-":
        return
    with _conv_effort_lock:
        _conv_effort[conv] = (str(effort), int(budget or 0), time.time())


def _sticky_effort(conv, effort, budget):
    """同一会话沿用上次档位；只允许升级、不允许降级 → 一次会话最多断一次前缀。

    reasoning_effort 会被模板渲染进 system 消息头（xhigh / low 各一句），
    所以档位在会话中途来回翻转，等于把 35K 的前缀缓存反复作废。
    """
    if not EFFORT_STICKY or not conv or conv == "-":
        return effort, budget, False
    now = time.time()
    with _conv_effort_lock:
        dead = [k for k, v in _conv_effort.items() if now - v[2] > EFFORT_TTL_S]
        for k in dead:
            _conv_effort.pop(k, None)
        prev = _conv_effort.get(conv)
        if not prev:
            _conv_effort[conv] = (effort, budget, now)
            return effort, budget, False
        if _EFFORT_RANK.get(str(effort), 0) > _EFFORT_RANK.get(prev[0], 0):
            _conv_effort[conv] = (effort, budget, now)
            return effort, budget, False
        _conv_effort[conv] = (prev[0], prev[1], now)
        return prev[0], prev[1], True


def apply_profile(body):
    """虚拟模型名 → 参数预设。返回档位名（未命中返回 None）。"""
    if not isinstance(body, dict):
        return None
    name = body.get("model")
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    prof = MODEL_PROFILES.get(key)
    if not prof:
        return None
    for k, v in prof.items():
        if k == "enable_thinking":
            ctk = body.get("chat_template_kwargs")
            if not isinstance(ctk, dict):
                ctk = {}
            ctk.setdefault("enable_thinking", v)
            body["chat_template_kwargs"] = ctk
        elif body.get(k) is None:
            body[k] = v
    return key


def _thinking_chars(msgs):
    """诊断用：数一下请求里带回来的历史思考有多少字符（只读，不改请求）。"""
    n = 0
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        rc = m.get("reasoning_content")
        if isinstance(rc, str):
            n += len(rc)
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") in ("thinking", "reasoning"):
                    t = p.get("thinking") or p.get("text") or ""
                    if isinstance(t, str):
                        n += len(t)
    return n


def apply_thinking_policy(body):
    """STRATA_STRIP_THINKING=1：从历史消息里真正摘掉思考块。

    实测：引擎不转发 chat_template_kwargs.preserve_thinking（传了 prompt_tokens 不变），
    但会照常渲染 reasoning_content / Anthropic thinking 块 → 只能在网关侧删。
    保留最后一条 assistant 的思考（模型继续推理时还要看），其余历史清空。
    返回摘掉的字符数。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return 0
    removed, keep = 0, True
    for m in reversed(msgs):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        if keep:
            keep = False          # 最新那一条留着
            continue
        rc = m.get("reasoning_content")
        if isinstance(rc, str) and rc:
            removed += len(rc)
            m.pop("reasoning_content", None)
        c = m.get("content")
        if isinstance(c, list):
            drop = [p for p in c if isinstance(p, dict)
                    and p.get("type") in ("thinking", "reasoning")]
            if drop:
                removed += sum(len(str(p.get("thinking") or p.get("text") or ""))
                               for p in drop)
                m["content"] = [p for p in c if p not in drop]
    return removed


def _tool_sig(m):
    tcs = m.get("tool_calls")
    if not isinstance(tcs, list) or not tcs:
        return None
    out = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        out.append([fn.get("name"), fn.get("arguments")])
    if not out:
        return None
    try:
        return json.dumps(out, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(out)


def detect_tool_loop(msgs):
    """看最近几轮 assistant 是否在重复完全相同的工具调用（同名字 + 同参数）。"""
    if not isinstance(msgs, list) or not msgs:
        return None, 0
    sigs = []
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("tool", "function"):
            continue
        if role == "assistant":
            s = _tool_sig(m)
            if s:
                sigs.append(s)
            else:
                break
        elif role == "user":
            break
        if len(sigs) >= 8:
            break
    if not sigs:
        return None, 0
    run = 1
    for s in sigs[1:]:
        if s == sigs[0]:
            run += 1
        else:
            break
    return sigs[0], run


def apply_task_type(body, task_header, path):
    """根据 X-Task-Type 或动态难度自适应注入思考深度与采样参数。

    返回 (task, filled, raised, meta)。meta 里带会话指纹与"档位是否粘住"，
    用来解释前缀缓存为什么复用/没复用。
    """
    if not isinstance(body, dict):
        return None, [], False, {}
    task = resolve_task(task_header)
    row = TASK_MATRIX.get(task) if task else None
    filled, raised = [], False
    meta = {"conv": "-", "sid": "-", "sticky": False, "effort": None, "explicit": False}

    if path in ("/v1/chat/completions", "/v1/messages"):
        ctk = body.get("chat_template_kwargs")
        thinking_disabled = isinstance(ctk, dict) and ctk.get("enable_thinking") is False
        
        msgs = body.get("messages") or []
        meta["conv"] = _conv_fp(msgs)
        meta["sid"] = _conv_id(msgs)
        explicit_effort = body.get("reasoning_effort")
        meta["explicit"] = explicit_effort is not None
        if explicit_effort is not None and not thinking_disabled:
            _remember_effort(meta["sid"], explicit_effort,
                             body.get("reasoning_budget_tokens") or 0)
            meta["effort"] = explicit_effort
        if row:
            fields = dict(row)
        else:
            # 动态任务难度评估器 (Dynamic Difficulty Evaluator)
            tools = body.get("tools")
            dyn_task, dyn_effort, dyn_budget = detect_dynamic_difficulty(msgs, tools)
            if explicit_effort is None and not thinking_disabled:
                dyn_effort, dyn_budget, stuck = _sticky_effort(
                    meta["sid"], dyn_effort, dyn_budget)
                meta["sticky"] = stuck
            if meta["effort"] is None and not thinking_disabled:
                meta["effort"] = dyn_effort
            task = f"auto:{dyn_task}"
            fields = {
                "temperature": 0.7 if "tool" in dyn_task else 1.0,
                "top_p": 0.8 if "tool" in dyn_task else 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "reasoning_effort": dyn_effort,
                "reasoning_budget_tokens": dyn_budget,
                "max_tokens": 8192
            }

        if meta["effort"] is None:
            meta["effort"] = body.get("reasoning_effort") or fields.get("reasoning_effort")

        for k, v in fields.items():
            if k == "reasoning_effort":
                if body.get("reasoning_effort") is None and not thinking_disabled:
                    body["reasoning_effort"] = v
                    filled.append(f"{k}={v}")
            elif k == "reasoning_budget_tokens":
                if body.get("reasoning_budget_tokens") is None and not thinking_disabled:
                    body["reasoning_budget_tokens"] = v
                    filled.append(f"budget={v}")
            elif k == "max_tokens":
                cur = body.get("max_tokens")
                if cur is None:
                    body["max_tokens"] = v
                    filled.append(f"{k}={v}")
                elif isinstance(cur, (int, float)) and cur < v:
                    body["max_tokens"] = v
                    raised = True
                    filled.append(f"{k}={cur}->{v}")
            elif body.get(k) is None:
                body[k] = v
                filled.append(f"{k}={v}")

    return task, filled, raised, meta

# ---------------- 三路日志（单日单文件 append） ----------------
_log_fps = {}
_log_lock = threading.Lock()


def _log_fp(role):
    day = time.strftime("%Y%m%d")
    key = (role, day)
    with _log_lock:
        fp = _log_fps.get(key)
        if fp is None:
            for (r, d), old in list(_log_fps.items()):
                if d != day:
                    try:
                        old.close()
                    except Exception:
                        pass
                    _log_fps.pop((r, d), None)
            fp = open(os.path.join(LOGS_DIR, f"8081_{role}_{day}.log"),
                      "a", encoding="utf-8", buffering=1)
            _log_fps[key] = fp
        return fp


def log_line(role, msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        fp = _log_fp(role)
        fp.write(line + "\n")
        fp.flush()
    except Exception:
        pass


# ---------------- 后端探测（动态防抖：在线5秒，离线1秒快速感知） ----------------
_backend_cache = {"t": 0.0, "model": None, "n_ctx": 131072, "up": False}
_backend_lock = threading.Lock()


def backend_info(force=False):
    now = time.time()
    with _backend_lock:
        ttl = 5.0 if _backend_cache["up"] else 1.0
        if not force and (now - _backend_cache["t"] < ttl):
            return dict(_backend_cache)
    up, model, n_ctx = False, None, 131072
    try:
        req = urllib.request.Request(BACKEND + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as r:
            h = json.loads(r.read().decode("utf-8"))
            if h.get("status") == "ok":
                up = True
                model = h.get("model") or "qwen3.8-flash-next-abliterated-iq3_s"
                n_ctx = int(h.get("max_context") or 131072)
    except Exception:
        try:
            req = urllib.request.Request(BACKEND + "/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as r:
                data = json.loads(r.read().decode("utf-8"))
                items = data.get("data") or []
                if items:
                    up, model = True, items[0].get("id")
                    n_ctx = int(((items[0].get("meta") or {}).get("n_ctx")) or 131072)
        except Exception:
            pass
    with _backend_lock:
        _backend_cache.update({"t": now, "model": model, "n_ctx": n_ctx, "up": up})
        return dict(_backend_cache)


def wait_for_backend(timeout=90):
    """当主脑正在启动或热身时，网关优雅排队等待握手，避免前端出现连接失败或503。"""
    t_end = time.time() + timeout
    while time.time() < t_end:
        info = backend_info(force=True)
        if info["up"]:
            return info
        time.sleep(1.0)
    return backend_info(force=True)


_metrics_cache = {"t": 0.0, "data": {}}
_metrics_lock = threading.Lock()


def strata_metrics():
    """获取 8083 (/metrics) 引擎底座指标（15秒缓存防抖，低频静默采样，杜绝性能损耗）"""
    now = time.time()
    with _metrics_lock:
        if now - _metrics_cache["t"] < 15.0:
            return dict(_metrics_cache["data"])
    data = {}
    try:
        req = urllib.request.Request(BACKEND + "/metrics", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    with _metrics_lock:
        _metrics_cache.update({"t": now, "data": data})
        return dict(_metrics_cache["data"])


CLOUD_PREFIX = ("claude-", "gpt-", "o1", "o3", "deepseek-", "gemini-", "grok-",
                "qwen", "strata", "default", "auto", "local", "main", "vision",
                "image", "ocr", "chat", "coder", "instruct")
# 老 27B 栈的别名（DSH 旧会话/旧配置里可能还留着）：一律指向当前主脑，Strata 本来也忽略 model 名
LEGACY_ALIASES = {"27b-a", "27b-gsq-m", "27b-gsq", "27b-a-work", "27b-nv-h", "27b-nv-m",
                  "27b-n-h", "mid-high", "qwen3-coder", "qwen3-coder-30b", "c30b", "30b",
                  "nex-n2.5-mini", "nex-35b", "35b", "ornith", "ornith-35b",
                  "qwen3.8-flash-next-iq3_s"}


def resolve_alias(requested):
    """云端别名/随便填 -> 后端真实模型；未知名字原样透传（Strata 本来也忽略 model 字段）。"""
    info = backend_info()
    live = info["model"] or "qwen3.8-flash-next-iq3_s"
    if not requested or not isinstance(requested, str):
        return live, requested
    rl = requested.strip().lower()
    if rl == live.lower() or rl.startswith(CLOUD_PREFIX) or rl in LEGACY_ALIASES:
        return live, requested
    # 未知名（客户端随便填的 id，如 "llamacpp"）：后端本来也忽略 model 字段，
    # 但日志/看板要显示真实模型，所以一律归一到 live 模型，原名记进 alias。
    return live, requested


def _with_profiles(payload):
    """把虚拟档位名挂进 /v1/models，客户端才能选到。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return payload
    have = {d.get("id") for d in data if isinstance(d, dict)}
    for name in MODEL_PROFILES:
        if name not in have:
            data.append({"id": name, "object": "model", "owned_by": "strata-gateway",
                         "meta": {"profile": True, "note": "网关参数预设，落到同一后端模型"}})
    payload["data"] = data
    return payload


# ---------------- Key 鉴权（识别不拦截） ----------------
_keys = {}


def load_keys():
    global _keys
    default = {"admin": {"name": "Admin 主控机"}, "llamacpp": {"name": "Llamacpp"},
               "v100-32G": {"name": "v100-32G 工作机"}}
    try:
        if os.path.exists(KEYS_FILE):
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                _keys = json.load(f)
                return
    except Exception:
        pass
    _keys = default


def identify(auth_header, x_api_key=""):
    load_keys()
    token = (x_api_key or "").strip()
    if not token and auth_header:
        parts = auth_header.strip().split()
        token = (parts[-1] if len(parts) >= 2 else parts[0]).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return "admin", "Admin 主控机"
    if token in _keys:
        return token, _keys[token].get("name", token)
    low = token.lower()
    if "v100" in low:
        return "v100-32G", "v100-32G 工作机"
    if "llama" in low:
        return "llamacpp", "Llamacpp"
    return token, f"设备 [{token}]"


# ---------------- 计费 ----------------
_bill_lock = threading.Lock()


def bill_record(key_id, prompt_tok, completion_tok, images=0, cached_tok=0):
    # 命中缓存的输入按 in_hit 计价，未命中按 in_miss；虚拟计费因此能反映前缀复用
    cached_tok = max(0, min(int(cached_tok or 0), int(prompt_tok or 0)))
    miss_tok = max(0, int(prompt_tok or 0) - cached_tok)
    cost = (miss_tok / 1e6) * PRICING["in_miss_m"] \
        + (cached_tok / 1e6) * PRICING["in_hit_m"] \
        + (completion_tok / 1e6) * PRICING["out_m"] + images * PRICING["image_each"]
    day = time.strftime("%Y-%m-%d")
    with _bill_lock:
        try:
            data = {}
            if os.path.exists(BILL_FILE):
                with open(BILL_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            data = {}
        d = data.setdefault(day, {})
        k = d.setdefault(key_id, {"calls": 0, "prompt_tokens": 0,
                                  "completion_tokens": 0, "cost_cny": 0.0})
        k["calls"] += 1
        k["prompt_tokens"] += prompt_tok
        k["completion_tokens"] += completion_tok
        k["cost_cny"] = round(k["cost_cny"] + cost, 4)
        try:
            with open(BILL_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return round(cost, 4)


# ---------------- token 估算 + 防爆剪枝 ----------------
def rough_tokens(obj):
    """启发式估算（中英混合约 1.6 字符/token），count_tokens 不可用时的 fallback。"""
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return max(1, int(len(s) / 1.6))


def count_tokens_anthropic(system, messages):
    """借 Strata /v1/messages/count_tokens 精确计数；失败返回 None。"""
    try:
        body = json.dumps({"model": "x", "system": system or "",
                           "messages": messages}).encode("utf-8")
        req = urllib.request.Request(BACKEND + "/v1/messages/count_tokens", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return int(json.loads(r.read().decode("utf-8")).get("input_tokens", 0))
    except Exception:
        return None


def _text_of(c):
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return ""


def _to_anthropic_messages(oai_messages):
    out = []
    for m in oai_messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        if role in ("system", "developer"):
            continue
        out.append({"role": "assistant" if role == "assistant" else "user",
                    "content": _text_of(m.get("content"))})
    return out


def _system_of(oai_messages):
    return "\n".join(_text_of(m.get("content")) for m in (oai_messages or [])
                     if isinstance(m, dict) and m.get("role") in ("system", "developer"))


def _recount(msgs, tools_tok):
    """重新精确计数（engine 的 count_tokens 不可用时退回启发式）。"""
    est = count_tokens_anthropic(_system_of(msgs), _to_anthropic_messages(msgs))
    if est is None:
        est = rough_tokens({"messages": msgs})
    return int(est) + tools_tok


def _compress_old_tools(msgs, keep_last=3, min_chars=600):
    """阶段 1：把已消费的旧工具输出压成一行占位符。

    不改消息条数、不改顺序 → tool_call/tool_result 配对和 KV 前缀都保住。
    最后 keep_last 条工具输出保留原文（模型可能还要用）。
    """
    idx = [i for i, m in enumerate(msgs)
           if isinstance(m, dict) and m.get("role") == "tool"]
    hit = 0
    for i in (idx[:-keep_last] if len(idx) > keep_last else []):
        t = _text_of(msgs[i].get("content"))
        if len(t) > min_chars:
            msgs[i]["content"] = "[tool output omitted by gateway: %d chars]" % len(t)
            hit += 1
    return hit


def _drop_oldest_turns(msgs, keep_tail=6):
    """阶段 2：按 user 边界整轮丢弃，保 system + 首条 user + 末尾若干轮。

    只在 user 边界切，避免留下孤立的 tool 结果（会破坏配对）。
    """
    sys_msgs = [m for m in msgs if m.get("role") in ("system", "developer")]
    conv = [m for m in msgs if m.get("role") not in ("system", "developer")]
    uid = [i for i, m in enumerate(conv) if m.get("role") == "user"]
    if len(uid) <= keep_tail or uid[-keep_tail] <= uid[0]:
        kept = conv[-2:] if len(conv) > 2 else conv
        return sys_msgs + kept, len(conv) - len(kept)
    head = conv[uid[0]]
    tail = conv[uid[-keep_tail]:]
    return sys_msgs + [head] + tail, len(conv) - 1 - len(tail)


def guard_or_trim(body, n_ctx):
    """返回 (body, trimmed, est)。超限时先压旧工具输出，再整轮丢弃，塞不下就诚实 413。"""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return body, False, 0
    try:
        tools_tok = rough_tokens(body.get("tools")) if body.get("tools") else 0
    except Exception:
        tools_tok = 0
    orig_n = len(msgs)
    est = _recount(msgs, tools_tok)
    if est <= n_ctx * GUARD_RATIO:
        return body, False, est

    limit = n_ctx * TARGET_RATIO
    phases, dropped = [], 0
    # 阶段 1：压缩旧工具输出（不改条数/顺序，前缀与配对都不动）
    for _ in range(4):
        if not _compress_old_tools(msgs):
            break
        phases.append("tools")
        est = _recount(msgs, tools_tok)
        if est <= limit:
            break
    # 阶段 2：整轮丢弃（兜底，逐级收紧保留轮数）
    if est > limit:
        dropped = 0
        for keep in (6, 4, 2):
            new_msgs, d = _drop_oldest_turns(msgs, keep_tail=keep)
            if d <= 0:
                break
            msgs[:] = new_msgs
            dropped += d
            est = _recount(msgs, tools_tok)
            if est <= limit:
                break
        phases.append("turns")
    if est > n_ctx * GUARD_RATIO:
        return None, True, est
    log_line("gateway", f"[GUARD] 剪枝：{orig_n} -> {len(msgs)} 条"
                        f"（{'/'.join(phases) or 'none'}"
                        f"{'，丢 ' + str(dropped) + ' 条' if dropped else ''}），~{est} tok")
    return body, True, est


def _responses_tokens(body):
    """Responses API 的超限兜底估算（不裁剪，避免破坏 input 结构）。"""
    try:
        msgs = []
        instr = body.get("instructions")
        if isinstance(instr, str) and instr:
            msgs.append({"role": "system", "content": instr})
        inp = body.get("input")
        if isinstance(inp, str):
            msgs.append({"role": "user", "content": inp})
        elif isinstance(inp, list):
            for item in inp:
                if not isinstance(item, dict):
                    continue
                role = item.get("role") or "user"
                c = item.get("content")
                if isinstance(c, str):
                    msgs.append({"role": role, "content": c})
                elif isinstance(c, list):
                    msgs.append({"role": role, "content": "".join(
                        p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") in ("input_text", "text", "output_text"))})
        if not msgs:
            return 0
        est = count_tokens_anthropic(_system_of(msgs), _to_anthropic_messages(msgs))
        return int(est) if est is not None else rough_tokens(body)
    except Exception:
        return 0


# ---------------- 记忆注入（只读；失败静默） ----------------
_pm = None


def memory_context(query, max_chars=2000):
    global _pm
    if _pm is None:
        try:
            sys.path.insert(0, BASE_DIR)
            import personal_memory as pm
            _pm = pm
        except Exception:
            _pm = False
    if not _pm or not query:
        return ""
    try:
        if os.environ.get("GATEWAY_DYNAMIC_MEMORY") == "1":
            ctx = _pm.get_relevant_context(query, max_items=2)
        else:
            # 静态 doctrines：保 KV 前缀缓存；动态召回按需走 :8087 memory-recall 工具
            ctx = _pm.get_doctrines_text()
        return ctx[:max_chars] if ctx else ""
    except Exception:
        return ""


def inject_memory_openai(body):
    try:
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return
        last_user = next((m for m in reversed(msgs)
                          if m.get("role") == "user"), None)
        mem = memory_context(_text_of((last_user or {}).get("content"))[-2000:])
        if not mem:
            return
        for m in msgs:
            if m.get("role") == "system":
                m["content"] = f"{mem}\n\n{_text_of(m.get('content'))}"
                return
        msgs.insert(0, {"role": "system", "content": mem})
    except Exception:
        pass


def inject_workstyle_openai(body):
    """把"开工方式"贴到 system 末尾。

    · 放末尾：harness 的 system 很长（身份 + 工具清单 + 技能清单），
      末尾的指令离生成最近，模型照做率最高；
    · 固定文本：同一会话每轮完全一致 → 前缀缓存照常命中；
    · 幂等：已经贴过（客户端重放同一请求）就不再贴第二遍。
    """
    if not WORKSTYLE_ENABLE:
        return
    try:
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return
        for m in msgs:
            if m.get("role") == "system" and WORKSTYLE_MARK in _text_of(m.get("content")):
                return
        for m in msgs:
            if m.get("role") == "system":
                m["content"] = f"{_text_of(m.get('content'))}\n\n{WORKSTYLE_TEXT}"
                return
        msgs.insert(0, {"role": "system", "content": WORKSTYLE_TEXT})
    except Exception:
        pass


def _conv_fp(msgs):
    """诊断用：给“这是哪一条会话”打一个短指纹（首条非 system 消息 + 消息数）。

    注意：带消息数，所以同一会话每轮都会变 —— 只能用来区分“视图”（短视图/长视图）。
    """
    try:
        if not isinstance(msgs, list) or not msgs:
            return "-"
        for m in msgs:
            if isinstance(m, dict) and m.get("role") not in ("system", "developer"):
                t = _text_of(m.get("content")) or ""
                h = abs(hash(str(m.get("role", "")) + "\x00" + t[:500])) & 0xFFFFFFFF
                return "%08x/%d" % (h, len(msgs))
    except Exception:
        pass
    return "-"


def _conv_id(msgs):
    """稳定会话 id：system 头 + 首条 user 的哈希（**不含消息数**）。

    档位粘住和缓存断崖必须用它：`_conv_fp` 带消息数，每轮都变，
    拿它做 key 会导致 `_last_cache` 永远命中不了、粘住永远不生效。
    """
    try:
        if not isinstance(msgs, list) or not msgs:
            return "-"
        sys_txt = "\n".join(_text_of(m.get("content")) for m in msgs
                            if isinstance(m, dict) and m.get("role") in ("system", "developer"))
        first_user = ""
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "user":
                first_user = _text_of(m.get("content")) or ""
                break
        h = abs(hash(sys_txt[:4000] + "\x00" + first_user[:400])) & 0xFFFFFFFF
        return "%08x" % h
    except Exception:
        return "-"


# ---------------- 看板数据 ----------------
_recent = []
_recent_lock = threading.Lock()
_task_counts = {}
_task_lock = threading.Lock()
_peak_tps = {"v": 0.0}
_last_decision = {}
_active = {"n": 0}

# 前缀缓存记账：prompt/cached 累计、断崖次数、档位粘住次数
_cache_stat = {"prompt": 0, "cached": 0, "calls": 0, "cliff": 0, "sticky": 0}
_cache_lock = threading.Lock()
_last_cache = {}


def _cache_note(conv, prompt_t, cached):
    """前缀复用记账 + 断崖检测。返回 (是否断崖, 上次复用 token)。"""
    cliff, prev_cached = False, 0
    now = time.time()
    with _cache_lock:
        _cache_stat["prompt"] += prompt_t
        _cache_stat["cached"] += cached
        _cache_stat["calls"] += 1
        prev = _last_cache.get(conv)
        if prev:
            prev_cached = prev[0]
            if (prompt_t >= CACHE_WARN_MIN and prev[0] >= 10000
                    and cached < prev[0] * CACHE_WARN_RATIO):
                cliff = True
                _cache_stat["cliff"] += 1
        _last_cache[conv] = (cached, prompt_t, now)
        if len(_last_cache) > 200:
            old = sorted(_last_cache.items(), key=lambda kv: kv[1][2])[:100]
            for k, _v in old:
                _last_cache.pop(k, None)
    return cliff, prev_cached


def recent_add(rec):
    with _recent_lock:
        _recent.append(rec)
        del _recent[:-50]
    if rec.get("task"):
        with _task_lock:
            _task_counts[rec["task"]] = _task_counts.get(rec["task"], 0) + 1
            _last_decision.update({"t": rec.get("t", ""), "task": rec.get("task", ""),
                                   "filled": rec.get("task_filled", []),
                                   "model": rec.get("model", ""), "key": rec.get("key", "")})
    try:
        if float(rec.get("tps") or 0) > _peak_tps["v"]:
            _peak_tps["v"] = float(rec["tps"])
    except Exception:
        pass


def backend_status():
    """主脑忙/闲（对等老网关的槽位状态：单 FIFO，只有忙和闲两种槽位）。"""
    try:
        req = urllib.request.Request(BACKEND + "/status", method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            s = json.loads(r.read().decode("utf-8"))
            return {"busy": bool(s.get("busy")), "elapsed_s": s.get("elapsed_s", 0),
                    "tokens_per_s": s.get("tokens_per_s", 0)}
    except Exception:
        return {"busy": False, "elapsed_s": 0, "tokens_per_s": 0}


TASK_CN = {
    "coding": "💻 编程开发", "math": "🧮 数学逻辑", "tools": "🛠️ 工具/JSON",
    "rag": "📚 知识RAG", "writing": "🎨 文学创作", "chat": "💬 通用闲聊",
    "auto:agent_tool": "⚡ 工具流转", "auto:quick_reply": "⚡ 极速短答",
    "auto:lightweight": "⚡ 轻量问答", "auto:coding_balanced": "🎯 编程甜点",
    "auto:deep_reasoning": "🧠 深度攻坚", "auto:math_logic": "🧮 算法推导",
    "auto:general": "⚖️ 平衡思考"
}


def heat_cells():
    """本月每日调用热力日历（深度提取 token 吞吐、请求数与价值，GitHub 极客暗夜霓虹风格）。"""
    try:
        if os.path.exists(BILL_FILE):
            with open(BILL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
    except Exception:
        data = {}
    now = datetime.date.today()
    import calendar as calmod
    first_wd, ndays = calmod.monthrange(now.year, now.month)
    dh = data.get("daily_history") or {}

    cells = ['<div class="cal-square empty"></div>'] * ((first_wd + 1) % 7)
    today_str = now.strftime("%Y-%m-%d")

    for d in range(1, ndays + 1):
        key = f"{now.year}-{now.month:02d}-{d:02d}"
        
        # 兼容读取模式：1. 顶级日期字典（8081网关记录） 2. daily_history 历史归档
        calls = 0
        p_tok = 0
        c_tok = 0
        cost = 0.0
        
        if key in data and isinstance(data[key], dict):
            for v in data[key].values():
                calls += v.get("calls", 0)
                p_tok += v.get("prompt_tokens", 0)
                c_tok += v.get("completion_tokens", 0)
                cost += v.get("cost_cny", 0.0)
        elif key in dh and isinstance(dh[key], dict):
            h = dh[key]
            calls = h.get("requests", 0)
            p_tok = h.get("prompt_tokens", 0)
            c_tok = h.get("completion_tokens", 0)
            cost = h.get("cost_cny", 0.0)

        total_tok = p_tok + c_tok

        # 热力等级划分
        if total_tok == 0 and calls == 0:
            lv = "level-0"
        elif total_tok < 10000 and calls < 5:
            lv = "level-1"
        elif total_tok < 100000 and calls < 20:
            lv = "level-2"
        elif total_tok < 500000:
            lv = "level-3"
        else:
            lv = "level-4"

        # Token 简写显示 (如 1.2M / 45k)
        tok_badge = ""
        if total_tok > 0:
            if total_tok >= 1_000_000:
                tok_badge = f"{total_tok / 1_000_000:.1f}M"
            elif total_tok >= 1_000:
                tok_badge = f"{total_tok / 1_000:.0f}k"
            else:
                tok_badge = str(total_tok)
        elif calls > 0:
            tok_badge = f"{calls}req"

        is_today = " is-today" if key == today_str else ""
        sub_html = f'<div class="cal-square-tokens">{tok_badge}</div>' if tok_badge else ''
        
        cells.append(
            f'<div class="cal-square {lv}{is_today}" '
            f'data-date="{key}" data-reqs="{calls}" data-prompt="{p_tok}" '
            f'data-comp="{c_tok}" data-total="{total_tok}" data-cost="{cost:.4f}" '
            f'onmouseenter="showHeatTooltip(event, this)" '
            f'onmousemove="moveHeatTooltip(event)" '
            f'onmouseleave="hideHeatTooltip()">'
            f'<div>{d}</div>{sub_html}'
            f'</div>'
        )

    grid_items = "".join(cells)
    weekdays = "".join(f"<div>{w}</div>" for w in ["日", "一", "二", "三", "四", "五", "六"])
    return f'''
    <div class="calendar-container">
      <div class="calendar-weekdays">{weekdays}</div>
      <div class="calendar-grid">{grid_items}</div>
    </div>
    '''


def get_strata_engine_meta(sm_eng, info, uptime_min=0):
    """
    动态检测并解析 Strata 引擎版本号、底层编译架构与运行期功能特性矩阵。
    不硬编码任何版本号或特性文案，未来解压覆盖新版 zip 或修改模型参数后自动自适应呈现。
    """
    candidates = [
        os.path.abspath(os.path.join(ROOT_DIR, "..", "Strata-main")),
        r"H:\Strata-main",
        ROOT_DIR
    ]
    strata_root = next((p for p in candidates if os.path.exists(os.path.join(p, "engine", "BUILD.json")) or os.path.exists(os.path.join(p, "CMakeLists.txt"))), None) or r"H:\Strata-main"

    build_info = {}
    build_path = os.path.join(strata_root, "engine", "BUILD.json")
    if os.path.exists(build_path):
        try:
            with open(build_path, "r", encoding="utf-8") as f:
                build_info = json.load(f)
        except Exception:
            pass

    cmake_ver = None
    cmake_path = os.path.join(strata_root, "CMakeLists.txt")
    if os.path.exists(cmake_path):
        try:
            with open(cmake_path, "r", encoding="utf-8") as f:
                m = re.search(r"project\(strata\s+VERSION\s+([\d.]+)", f.read())
                if m:
                    cmake_ver = m.group(1)
        except Exception:
            pass

    # 读取模型与参数配置文件（寻找 active model json）
    cfg_info = {}
    active_model_name = sm_eng.get("model") or info.get("model") or ""
    cfg_paths = [
        os.path.join(ROOT_DIR, "strata-abliterated-iq3_s.json"),
        os.path.join(strata_root, "strata-abliterated-iq3_s.json")
    ]
    for d in (ROOT_DIR, strata_root):
        if os.path.isdir(d):
            try:
                for fn in os.listdir(d):
                    if fn.startswith("strata-") and fn.endswith(".json"):
                        full_p = os.path.join(d, fn)
                        if full_p not in cfg_paths:
                            cfg_paths.append(full_p)
            except Exception:
                pass

    for cfg_candidate in cfg_paths:
        if os.path.exists(cfg_candidate):
            try:
                with open(cfg_candidate, "r", encoding="utf-8") as f:
                    c = json.load(f)
                    if active_model_name and active_model_name in c.get("model_name", ""):
                        cfg_info = c
                        break
                    elif not cfg_info:
                        cfg_info = c
            except Exception:
                pass

    raw_args = cfg_info.get("args") or []
    parsed_args = {}
    i = 0
    while i < len(raw_args):
        arg = str(raw_args[i])
        if arg.startswith("--"):
            key = arg[2:]
            if i + 1 < len(raw_args) and not str(raw_args[i + 1]).startswith("--"):
                parsed_args[key] = str(raw_args[i + 1])
                i += 1
            else:
                parsed_args[key] = True
        i += 1

    # 1. 动态版本号解析 (BUILD.json > sm_eng metrics > CMakeLists.txt)
    live_ver = sm_eng.get("version")
    file_ver = build_info.get("version") or cmake_ver
    if file_ver and live_ver:
        ver = file_ver if len(file_ver) >= len(live_ver) else live_ver
    else:
        ver = file_ver or live_ver or "0.1.44.1"

    # 2. 编译架构与硬件加速标签
    cuda_ver = build_info.get("cuda") or "13.0"
    archs = build_info.get("archs") or []
    if 120 in archs:
        arch_label = "RTX 5080/SM120 原生优化"
    elif archs:
        arch_label = "SM" + "/".join(str(a) for a in archs) + " 原生指令集"
    else:
        arch_label = "GPU 原生加速"
    tech_pill = f"CUDA {cuda_ver} · {arch_label}"

    # 3. 运行状态徽章
    source_tag = (build_info.get("source") or "Release").capitalize()
    is_up = bool(info.get("up"))
    if is_up:
        badge_html = f'<span class="badge ok" style="font-size:11px"><span class="dot ok"></span>{source_tag} 正式版 ({uptime_min}m)</span>'
    else:
        badge_html = f'<span class="badge" style="font-size:11px;background:rgba(148,163,184,0.12);color:#94a3b8;border:1px solid rgba(148,163,184,0.25)"><span class="dot" style="background:#94a3b8"></span>{source_tag} 待命就绪</span>'

    # 4. 动态特性说明矩阵条目
    # (a) 投机解码 (MTP)
    spec_val = sm_eng.get("spec") or parsed_args.get("spec") or "2"
    min_p_val = parsed_args.get("spec-min-p") or "0.5"
    if spec_val and str(spec_val) != "0":
        spec_text = f"• <b>多重投机解码 (MTP)</b>：{spec_val} 步预测 · 投机窗口加速 (Min-p {min_p_val})"
    else:
        spec_text = "• <b>解码管线</b>：标准自回归逐字生成模式"

    # (b) 上下文长度与 KV Cache
    max_ctx = parsed_args.get("max-context") or info.get("n_ctx") or 131072
    try:
        max_ctx_int = int(max_ctx)
        max_ctx_str = f"{max_ctx_int:,}"
    except Exception:
        max_ctx_str = str(max_ctx)
    kv_type = (sm_eng.get("kv") or parsed_args.get("kv") or "int8").upper()
    ctx_text = f"• <b>长上下文与量化</b>：原生 {max_ctx_str} 上下文 · {kv_type} KV Cache"

    # (c) MoE 专家分级与常驻
    slots = sm_eng.get("expert_slots", 2066)
    cache_mib = sm_eng.get("expert_cache_mib", 4064)
    moe_text = f"• <b>MoE 智能专家分级</b>：按访问频率预热驻留显存 (常驻 {slots} 槽位)"

    # (d) 视觉模态感知
    has_vision = ("vision" in parsed_args) or ("vision" in cfg_info) or (build_info.get("vision") == "gpu")
    if has_vision:
        vision_backend = "GPU" if build_info.get("vision") == "gpu" else "硬件"
        vision_text = f"• <b>多模态视觉直通</b>：搭载 mmproj 视觉编码器，{vision_backend} 直通全模态感知"
    else:
        res_mib = parsed_args.get("vram-reserve-mib", "700")
        vision_text = f"• <b>显存保护管线</b>：预留 {res_mib} MiB VRAM 弹性缓冲"

    return {
        "version": ver,
        "tech_pill": tech_pill,
        "badge_html": badge_html,
        "bullets": [spec_text, ctx_text, moe_text, vision_text],
        "slots": slots,
        "cache_mib": cache_mib,
        "kv": kv_type.lower(),
        "spec": spec_val
    }


def dashboard():
    info = backend_info()
    st = backend_status()
    sm = strata_metrics()
    today = time.strftime("%Y-%m-%d")
    billing = {}
    try:
        if os.path.exists(BILL_FILE):
            with open(BILL_FILE, "r", encoding="utf-8") as f:
                billing = (json.load(f).get(today)) or {}
    except Exception:
        pass
    with _recent_lock:
        recent = list(reversed(_recent[-20:]))
    with _task_lock:
        counts = dict(_task_counts)
        dec = dict(_last_decision)

    # 8083 核心引擎纯净指标提取（100% 取自 Strata 引擎本身，不掺杂外部系统负载）
    sm_eng = sm.get("engine") or {}
    sm_tot = sm.get("totals") or {}
    sm_live = sm.get("live") or {}
    sm_reqs = sm.get("requests") or []

    drafts_offered = sm_tot.get("drafts_offered", 0)
    drafts_accepted = sm_tot.get("drafts_accepted", 0)
    draft_rate_pct = f"{(drafts_accepted / drafts_offered * 100):.1f}%" if drafts_offered > 0 else "0.0%"

    sm_prompt_tot = sm_tot.get("prompt_tokens", 0)
    sm_reused_tot = sm_tot.get("reused", 0)
    kv_hit_rate_pct = f"{(sm_reused_tot / sm_prompt_tot * 100):.1f}%" if sm_prompt_tot > 0 else "0.0%"
    # 网关侧口径（按请求记账，能定位"前缀被谁改写"）
    with _cache_lock:
        _cs_prompt, _cs_cached = _cache_stat["prompt"], _cache_stat["cached"]
        _cs_calls, _cs_sticky, _cs_cliff = (_cache_stat["calls"], _cache_stat["sticky"],
                                            _cache_stat["cliff"])
    gw_reuse_pct = f"{(_cs_cached / _cs_prompt * 100):.1f}%" if _cs_prompt else "--"
    gw_sticky_pct = f"{(_cs_sticky / _cs_calls * 100):.0f}%" if _cs_calls else "--"
    gw_cliff = _cs_cliff
    engine_uptime_min = int((sm.get("uptime_s", 0) or 0) / 60)

    # 动态检测 Strata 引擎版本、底层编译信息与功能特性矩阵（非硬编码，更新版本自适应）
    engine_meta = get_strata_engine_meta(sm_eng, info, engine_uptime_min)
    eng_ver = engine_meta["version"]
    eng_model = sm_eng.get("model", info.get("model") or "qwen3.8-flash-next")
    eng_slots = sm_eng.get("expert_slots", engine_meta["slots"])
    eng_cache_mib = sm_eng.get("expert_cache_mib", engine_meta["cache_mib"])
    eng_kv = sm_eng.get("kv", engine_meta["kv"])
    eng_arena_gib = (sm_eng.get("arena_mib", 0) or 0) / 1024
    eng_vram_free_mib = sm_eng.get("vram_free_mib", 325)
    eng_spec = sm_eng.get("spec", engine_meta["spec"])
    eng_workers = sm_eng.get("pool_workers", 5)

    meta_tech_pill = engine_meta["tech_pill"]
    meta_badge_html = engine_meta["badge_html"]
    feature_bullets_html = "\n".join(f"            <div>{b}</div>" for b in engine_meta["bullets"])

    # 模型三级存储分布 (Qwen3.8-Flash-Next 78.1 GB 总量)
    model_total_gb = 78.10
    vram_expert_gb = eng_cache_mib / 1024.0  # 约 3.97 GB
    vram_other_gb = 11.23  # 密集层、MTP头、注意力、Vision投影
    vram_total_gb = vram_expert_gb + vram_other_gb  # 约 15.20 GB
    
    ram_arena_gb = eng_arena_gib if eng_arena_gib > 0 else 46.84  # 内存 Arena 工作区常驻专家 46.84 GB
    ssd_cold_gb = max(0.0, model_total_gb - vram_total_gb - ram_arena_gb)  # 剩余动态磁盘按需映射
    
    pct_gpu = (vram_total_gb / model_total_gb) * 100.0  # ~19.5%
    pct_ram = (ram_arena_gb / model_total_gb) * 100.0   # ~60.0%
    pct_ssd = (ssd_cold_gb / model_total_gb) * 100.0    # ~20.5%

    last_sm_r = sm_reqs[0] if sm_reqs else {}
    vram_hit_rate_pct = f"{(last_sm_r.get('hit_rate', 0.70) * 100):.1f}%" if last_sm_r.get('hit_rate') is not None else "--"
    pcie_share_pct = f"{(last_sm_r.get('pcie_share', 0.20) * 100):.1f}%" if last_sm_r.get('pcie_share') is not None else "--"
    engine_tok_s = f"{last_sm_r.get('decode_tok_s', 0):.1f}" if last_sm_r.get('decode_tok_s') else "--"
    engine_uptime_min = int((sm.get("uptime_s", 0) or 0) / 60)

    # 4 端口感知
    ports = [
        {"port": 8081, "name": "网关", "ok": True, "desc": "智能协同统一中枢"},
        {"port": 8083, "name": "主脑", "ok": bool(info["up"]), "desc": "Strata MoE 推理底座"},
        {"port": 8087, "name": "工具", "ok": False, "desc": "MCP 工具执行平面"}
    ]
    for p in ports:
        if p["port"] == 8087:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{p['port']}/health")
                with urllib.request.urlopen(req, timeout=0.2) as resp:
                    p["ok"] = (resp.status == 200)
            except Exception:
                p["ok"] = False

    port_chips = "".join(
        f'<div class="port-chip"><span class="dot {"ok" if p["ok"] else "down"}"></span>'
        f'<strong>:{p["port"]}</strong><span>{p["name"]}</span>'
        f'<span style="font-size:11px;color:{"#00ff9d" if p["ok"] else "#ff3366"}">{"在位" if p["ok"] else "未就绪"}</span></div>'
        for p in ports
    )

    # 上下文剪枝守护指标（记忆/画像卡已随 8086 一起下线，不再展示空面板）
    trim_total = sum(1 for r in recent if r.get("trimmed"))

    rows = "".join(
        f"<tr><td>{r.get('t','')}</td>"
        f"<td><span class='key-pill'>{r.get('key','')}</span></td>"
        f"<td><span style='color:#38bdf8'>{r.get('model','')}</span></td>"
        f"<td><span class='task-pill'>{r.get('task','') or '-'}</span></td>"
        f"<td>{r.get('prompt',0)} / {r.get('completion',0)}</td>"
        f"<td>" + (
            f"<span class='badge' style='background:rgba(239,68,68,0.15);color:#f87171;border:1px solid rgba(239,68,68,0.3)'>✂️ 已修剪</span>"
            if r.get('trimmed') else
            f"<span style='color:var(--text-muted);font-size:11px'>完整注入</span>"
        ) + f"</td>"
        f"<td><span style='color:#facc15'>{r.get('ttft','-')}{'ms' if r.get('ttft') not in ('-', None) else ''}</span></td>"
        f"<td><span style='color:#00ff9d;font-weight:700'>{r.get('tps','')}</span></td>"
        f"<td>{r.get('ms',0)}ms</td></tr>" for r in recent)

    brows = "".join(
        f"<tr><td><span class='key-pill'>{k}</span></td>"
        f"<td><b>{v.get('calls',0)}</b></td>"
        f"<td>{v.get('prompt_tokens',0):,} / {v.get('completion_tokens',0):,}</td>"
        f"<td style='color:#00ff9d;font-weight:700'>¥{v.get('cost_cny',0):.4f}</td></tr>" for k, v in billing.items())

    mrows = "".join(
        f"<tr><td><b>{TASK_CN.get(t, t)}</b></td>"
        f"<td>{TASK_MATRIX[t]['temperature']}</td>"
        f"<td>{TASK_MATRIX[t]['top_p']}</td>"
        f"<td>{TASK_MATRIX[t]['presence_penalty']}</td>"
        f"<td><span class='badge' style='background:rgba(176,38,255,0.15);color:#b026ff;border:1px solid rgba(176,38,255,0.3)'>{TASK_MATRIX[t]['reasoning_effort']}</span></td>"
        f"<td>{TASK_MATRIX[t]['max_tokens']}</td>"
        f"<td style='color:#00f0ff;font-weight:800'>{counts.get(t, 0)}</td></tr>" for t in TASK_MATRIX)

    dec_line = ("无历史决策" if not dec else
                f"{dec.get('t','')} [{dec.get('key','')}] {TASK_CN.get(dec.get('task',''), dec.get('task',''))} "
                f"→ {dec.get('model','')}（自适应填充: {','.join(dec.get('filled', []))}）")

    busy_text = ("空闲待命" if info["up"] and not st["busy"] else ("推理中" if st["busy"] else "离线未启动"))
    busy_cls = "ok" if info["up"] and not st["busy"] else ("busy" if st["busy"] else "down")

    heat_html = heat_cells()

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Strata 智能协同网关 6.0 · 全模态 MoE 看板</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<meta http-equiv="refresh" content="15">
<style>
  :root {{
    --bg: #060911;
    --card-bg: rgba(13, 19, 33, 0.75);
    --border: rgba(56, 189, 248, 0.16);
    --border-glow: rgba(56, 189, 248, 0.45);
    --accent: #00f0ff;
    --accent-blue: #38bdf8;
    --accent-green: #00ff9d;
    --accent-purple: #b026ff;
    --accent-yellow: #facc15;
    --accent-red: #ff3366;
    --text: #f1f5f9;
    --text-muted: #8493a8;
    --mono: 'JetBrains Mono', monospace;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background-color: var(--bg);
    color: var(--text);
    font-family: 'Inter', -apple-system, sans-serif;
    padding: 24px;
    min-height: 100vh;
    background-image: 
      radial-gradient(circle at 12px 12px, rgba(56, 189, 248, 0.05) 1px, transparent 1px),
      radial-gradient(circle at 50% 0%, rgba(0, 240, 255, 0.08) 0%, transparent 60%),
      radial-gradient(circle at 100% 100%, rgba(176, 38, 255, 0.08) 0%, transparent 60%);
    background-size: 24px 24px, 100% 100%, 100% 100%;
  }}
  .header {{
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; margin-bottom: 24px;
  }}
  .title-group h1 {{
    font-size: 22px; font-weight: 800; background: linear-gradient(135deg, #00f0ff, #38bdf8, #b026ff);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    display: flex; align-items: center; gap: 10px;
  }}
  .title-group p {{ font-size: 13px; color: var(--text-muted); margin-top: 4px; font-family: var(--mono); }}
  .nav-links {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .btn-nav {{
    padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; text-decoration: none;
    background: rgba(15, 23, 42, 0.8); border: 1px solid var(--border); color: #e2e8f0;
    display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s;
  }}
  .btn-nav:hover {{
    background: rgba(56, 189, 248, 0.15); border-color: var(--accent); color: var(--accent);
    box-shadow: 0 0 15px rgba(0, 240, 255, 0.25);
  }}
  .btn-nav.primary {{
    background: linear-gradient(135deg, #0284c7, #0369a1); border-color: rgba(0, 240, 255, 0.4); color: #fff;
  }}

  /* 整体响应式居中容器 */
  .container {{ max-width: 1680px; margin: 0 auto; width: 100%; }}

  /* 顶部 4 KPI 核心指标条 */
  .grid-kpi {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }}
  @media (max-width: 1200px) {{ .grid-kpi {{ grid-template-columns: repeat(2, 1fr); }} }}
  @media (max-width: 640px) {{ .grid-kpi {{ grid-template-columns: 1fr; }} }}

  /* 核心主体左右双栏均衡网格 (58% / 42%) */
  .main-layout {{ display: grid; grid-template-columns: minmax(0, 1.18fr) minmax(0, 0.82fr); gap: 20px; align-items: start; }}
  @media (max-width: 1200px) {{ .main-layout {{ grid-template-columns: 1fr; }} }}

  .card {{
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4); transition: transform 0.2s, border-color 0.2s; margin-bottom: 20px;
  }}
  .card:hover {{ border-color: var(--border-glow); }}
  .card-header {{
    font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted);
    display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
  }}
  .card-val {{ font-size: 24px; font-weight: 800; font-family: var(--mono); color: #fff; }}
  .card-sub {{ font-size: 13px; color: var(--text-muted); margin-top: 6px; font-family: var(--mono); }}
  .badge {{ display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 700; font-family: var(--mono); }}
  .badge.ok {{ background: rgba(0, 255, 157, 0.12); color: var(--accent-green); border: 1px solid rgba(0, 255, 157, 0.3); }}
  .badge.busy {{ background: rgba(250, 204, 21, 0.12); color: var(--accent-yellow); border: 1px solid rgba(250, 204, 21, 0.3); }}
  .badge.down {{ background: rgba(255, 51, 102, 0.12); color: var(--accent-red); border: 1px solid rgba(255, 51, 102, 0.3); }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .dot.ok {{ background: var(--accent-green); box-shadow: 0 0 8px var(--accent-green); }}
  .dot.busy {{ background: var(--accent-yellow); box-shadow: 0 0 8px var(--accent-yellow); }}
  .dot.down {{ background: var(--accent-red); box-shadow: 0 0 8px var(--accent-red); }}
  .progress-bar {{ background: rgba(255, 255, 255, 0.06); border-radius: 6px; height: 8px; overflow: hidden; margin: 8px 0 4px; }}
  .progress-fill {{ height: 100%; border-radius: 6px; transition: width 0.3s ease; }}

  /* 模型三级存储分布条 (GPU / RAM / SSD) */
  .dist-bar {{
    display: flex; height: 26px; border-radius: 8px; overflow: hidden;
    background: rgba(15, 23, 42, 0.9); border: 1px solid rgba(56, 189, 248, 0.25);
    margin: 12px 0 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
  }}
  .dist-segment {{
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; font-family: var(--mono); color: #fff;
    transition: width 0.4s ease; text-shadow: 0 1px 3px rgba(0,0,0,0.8);
    position: relative; overflow: hidden;
  }}
  .dist-segment.gpu {{ background: linear-gradient(90deg, #0284c7, #00f0ff); }}
  .dist-segment.ram {{ background: linear-gradient(90deg, #9333ea, #c084fc); }}
  .dist-segment.ssd {{ background: linear-gradient(90deg, #334155, #64748b); }}

  .dist-legend {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
    font-size: 12px; margin-top: 8px;
  }}
  @media (max-width: 600px) {{ .dist-legend {{ grid-template-columns: 1fr; }} }}
  .dist-legend-item {{
    background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 6px; padding: 8px 10px;
  }}
  .dist-legend-title {{ display: flex; align-items: center; gap: 6px; font-weight: 600; margin-bottom: 3px; font-size: 11px; }}
  .dist-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .dist-dot.gpu {{ background: #00f0ff; box-shadow: 0 0 6px #00f0ff; }}
  .dist-dot.ram {{ background: #c084fc; box-shadow: 0 0 6px #c084fc; }}
  .dist-dot.ssd {{ background: #94a3b8; box-shadow: 0 0 6px #94a3b8; }}

  table {{ width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; }}
  th {{ background: rgba(15, 23, 42, 0.6); color: var(--text-muted); font-weight: 600; text-align: left; padding: 10px 14px; font-size: 12px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 10px 14px; border-bottom: 1px solid rgba(56, 189, 248, 0.08); font-family: var(--mono); color: #cbd5e1; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(56, 189, 248, 0.04); }}
  .table-scroll {{ overflow-x: auto; max-height: 440px; overflow-y: auto; border-radius: 8px; border: 1px solid rgba(56, 189, 248, 0.1); }}
  .table-scroll th {{ position: sticky; top: 0; background: #0c121e; z-index: 2; }}
  .port-chip {{ display: inline-flex; align-items: center; gap: 8px; background: rgba(15, 23, 42, 0.7); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px; font-size: 12px; margin-right: 8px; margin-bottom: 6px; }}
  .port-chip strong {{ color: var(--accent-blue); font-family: var(--mono); }}
  .key-pill {{ background: rgba(56, 189, 248, 0.1); color: var(--accent-blue); padding: 2px 8px; border-radius: 6px; border: 1px solid rgba(56, 189, 248, 0.2); font-size: 11px; }}
  .task-pill {{ background: rgba(176, 38, 255, 0.1); color: #d8b4fe; padding: 2px 8px; border-radius: 6px; border: 1px solid rgba(176, 38, 255, 0.2); font-size: 11px; }}

  /* 8083 底座监控精粹仪表盘网格 */
  .mon-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 10px; }}
  @media (max-width: 600px) {{ .mon-grid {{ grid-template-columns: 1fr; }} }}
  .mon-tile {{ background: rgba(15, 23, 42, 0.65); border: 1px solid rgba(56, 189, 248, 0.12); border-radius: 8px; padding: 12px 14px; }}
  .mon-title {{ font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 700; letter-spacing: 0.05em; display: flex; justify-content: space-between; margin-bottom: 6px; }}
  .mon-val {{ font-size: 16px; font-weight: 700; font-family: var(--mono); color: #fff; }}
  .mon-sub {{ font-size: 11px; color: var(--text-muted); font-family: var(--mono); margin-top: 4px; }}

  /* 极客暗夜热力日历组件 */
  .calendar-container {{ width: 100%; margin-top: 4px; }}
  .calendar-weekdays {{ display: grid; grid-template-columns: repeat(7, 1fr); text-align: center; font-size: 11px; color: var(--text-muted); margin-bottom: 6px; font-weight: 600; font-family: var(--mono); }}
  .calendar-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }}
  .cal-square {{
    height: 42px; border-radius: 6px; display: flex; flex-direction: column; align-items: center; justify-content: center;
    font-family: var(--mono); font-size: 12px; font-weight: 600; cursor: pointer;
    border: 1px solid rgba(255,255,255,0.06); position: relative; transition: all 0.2s ease;
  }}
  .cal-square:hover {{ transform: scale(1.08); z-index: 10; border-color: var(--accent); box-shadow: 0 4px 16px rgba(0, 240, 255, 0.4); }}
  .cal-square.empty {{ background: transparent; border: none; cursor: default; }}
  .cal-square.empty:hover {{ transform: none; box-shadow: none; }}
  .cal-square.is-today {{ outline: 2px solid var(--accent); outline-offset: 1px; }}

  .level-0 {{ background: #0c121e; color: #475569; }}
  .level-1 {{ background: rgba(56, 189, 248, 0.22); color: #bae6fd; border-color: rgba(56, 189, 248, 0.4); }}
  .level-2 {{ background: rgba(56, 189, 248, 0.50); color: #fff; border-color: rgba(56, 189, 248, 0.7); }}
  .level-3 {{ background: rgba(0, 255, 157, 0.65); color: #000; font-weight: 700; border-color: rgba(0, 255, 157, 0.85); }}
  .level-4 {{ background: #00ff9d; color: #000; font-weight: 800; border-color: #86efac; box-shadow: 0 0 12px rgba(0, 255, 157, 0.5); }}
  .cal-square-tokens {{ font-size: 9px; font-weight: normal; opacity: 0.9; margin-top: 1px; line-height: 1; }}

  /* 悬停浮窗 Tooltip */
  #heat-tooltip {{
    position: fixed; display: none; z-index: 1000; pointer-events: none;
    background: rgba(10, 15, 26, 0.96); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--accent); border-radius: 10px;
    padding: 12px 16px; font-size: 12px; line-height: 1.6; color: #f8fafc;
    box-shadow: 0 10px 30px rgba(0,0,0,0.8), 0 0 15px rgba(0, 240, 255, 0.25); min-width: 230px;
  }}
  .tt-title {{ font-weight: 700; color: var(--accent); border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 4px; margin-bottom: 6px; font-family: var(--mono); }}
  .tt-row {{ display: flex; justify-content: space-between; gap: 16px; }}
  .tt-val {{ font-family: var(--mono); font-weight: 600; color: #fff; }}
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <div class="title-group">
      <h1>🚀 Strata 智能协同网关 6.0</h1>
      <p>Qwen3.8-Flash-Next MoE 深度优化架构 · 前端统一入口 :8081/v1</p>
    </div>
    <div class="nav-links">
      <a href="http://127.0.0.1:8083/" target="_blank" class="btn-nav primary">💬 Strata Web 对话</a>
      <a href="http://127.0.0.1:8083/#monitor" target="_blank" class="btn-nav">📈 8083 性能底座</a>
      <a href="http://127.0.0.1:8087/tools/list" target="_blank" class="btn-nav">🛠️ MCP 工具清单</a>
      <a href="javascript:location.reload()" class="btn-nav">🔄 刷新</a>
    </div>
  </div>

  <!-- 顶部 4 KPI 核心指标条 (通栏居中平衡) -->
  <div class="grid-kpi">
    <!-- KPI 1: 主脑底座 -->
    <div class="card" style="margin-bottom:0">
      <div class="card-header">
        <span>🧠 主脑引擎底座 (:8083)</span>
        <span class="badge {busy_cls}"><span class="dot {busy_cls}"></span>{busy_text}</span>
      </div>
      <div class="card-val" style="font-size:18px;color:var(--accent)">{info["model"] or "Flash-Next abliterated IQ3_S"}</div>
      <div class="card-sub">上下文窗口: {info["n_ctx"]:,} tokens | 投机解码: 4步加速</div>
    </div>

    <!-- KPI 2: ⚡ 投机加速与解码吞吐 -->
    <div class="card" style="margin-bottom:0">
      <div class="card-header">
        <span>⚡ 投机预测加速 (Speculative)</span>
        <span style="color:var(--accent-green);font-family:var(--mono)">{draft_rate_pct} 命中</span>
      </div>
      <div class="card-val">{drafts_accepted:,} <span style="font-size:13px;color:var(--text-muted)">/ {drafts_offered:,} 步接受</span></div>
      <div class="card-sub">峰值吞吐: {_peak_tps["v"]} tok/s | 今日调度: {sum(counts.values())} 次</div>
    </div>

    <!-- KPI 3: 💾 KV 缓存与前缀复用 -->
    <div class="card" style="margin-bottom:0">
      <div class="card-header">
        <span>💾 KV 缓存与前缀复用 (Prompt Cache)</span>
        <span style="color:var(--accent);font-family:var(--mono)">{kv_hit_rate_pct} 命中</span>
      </div>
      <div class="card-val">{sm_reused_tot:,} <span style="font-size:13px;color:var(--text-muted)">/ {sm_prompt_tot:,} tokens</span></div>
      <div class="card-sub">网关侧复用 {gw_reuse_pct} · 档位粘住 {gw_sticky_pct} · 断崖 <span style="color:#f87171">{gw_cliff} 次</span></div>
      <div class="card-sub">专家显存常驻: {vram_hit_rate_pct} | PCIe 传输占比: {pcie_share_pct}</div>
    </div>

    <!-- KPI 4: 🛡️ 上下文防爆剪枝 -->
    <div class="card" style="margin-bottom:0">
      <div class="card-header">
        <span>🛡️ 上下文防爆剪枝</span>
        <span class="badge" style="font-size:10px;padding:2px 8px;background:rgba(56,189,248,0.12);color:#38bdf8;border:1px solid rgba(56,189,248,0.3)">阈值 92%</span>
      </div>
      <div class="card-val" style="font-size:22px;color:var(--accent-purple)">{trim_total}<span style="font-size:14px;color:var(--text-muted)"> 次修剪</span></div>
      <div class="card-sub">超阈值先压旧工具输出、再按轮丢弃；仍超限返回结构化 413</div>
    </div>
  </div>

  <!-- 核心主体左右双栏均衡排版 (左 58% : 右 42%) -->
  <div class="main-layout">
    <!-- 左栏: 请求流水 + 采样矩阵 + 计费设备统计 -->
    <div class="main-col-left">
      <!-- 最近请求协同流水 -->
      <div class="card">
        <div class="card-header">
          <span>📊 最近请求协同流水 (Top 20)</span>
          <span style="color:var(--accent-blue);font-family:var(--mono)">剪枝守护: {trim_total}次激活 · 实时滚动</span>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr><th>时间</th><th>设备</th><th>模型</th><th>任务意图</th><th>Tokens (In/Out)</th><th>上下文剪枝状态</th><th>首字延迟</th><th>吞吐</th><th>耗时</th></tr>
            </thead>
            <tbody>
              {rows or '<tr><td colspan="9" style="text-align:center;color:var(--text-muted);padding:24px">暂无请求历史</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>

      <!-- 任务自适应矩阵 -->
      <div class="card">
        <div class="card-header">
          <span>🎯 任务自适应动态采样矩阵</span>
          <span style="font-size:12px;color:var(--accent-blue)">今日调度 {sum(counts.values())} 次 · X-Task-Type</span>
        </div>
        <div style="overflow-x:auto">
          <table>
            <thead>
              <tr><th>意图领域</th><th>Temperature</th><th>Top-P</th><th>Presence</th><th>思考档位</th><th>保底 Max Tokens</th><th>今日命中</th></tr>
            </thead>
            <tbody>
              {mrows}
            </tbody>
          </table>
        </div>
        <div style="margin-top:14px;font-size:12px;color:var(--text-muted);font-family:var(--mono);padding:8px 12px;background:rgba(0,0,0,0.25);border-radius:6px">
          <span>最近决策快照：</span><span style="color:#e2e8f0">{dec_line}</span>
        </div>
      </div>

      <!-- 分 Key 统计 -->
      <div class="card">
        <div class="card-header">
          <span>💰 今日分设备 / Key 消耗统计（{today}）</span>
          <span style="color:var(--accent-green);font-family:var(--mono)">计费审计</span>
        </div>
        <div style="overflow-x:auto">
          <table>
            <thead>
              <tr><th>设备标识</th><th>请求数</th><th>总吞吐 (In/Out)</th><th>虚拟价值</th></tr>
            </thead>
            <tbody>
              {brows or '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);padding:18px">今日暂无消耗</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 右栏: 8083 底座监控精粹 + 本月调用热力日历 -->
    <div class="main-col-right">
      <!-- 📦 Strata 8083 版本特性与三级模型存储分布 (原生架构核心图解) -->
      <div class="card">
        <div class="card-header">
          <span>📦 Strata 底座架构实况 · v{eng_ver}</span>
          {meta_badge_html}
        </div>

        <!-- 当前版本特性说明（自适应动态矩阵） -->
        <div style="background:rgba(15,23,42,0.6);border:1px solid rgba(56,189,248,0.15);border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:12px;line-height:1.6">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <span style="color:var(--accent);font-weight:700">🚀 当前版本能力矩阵 (v{eng_ver})：</span>
            <span class="key-pill" style="font-size:10px">{meta_tech_pill}</span>
          </div>
          <div style="color:#cbd5e1;display:grid;grid-template-columns:1fr 1fr;gap:6px">
{feature_bullets_html}
          </div>
        </div>

        <!-- 💾 Qwen3.8-Flash-Next 内存/GPU/SSD 存储分布图 (重点可视化) -->
        <div style="background:rgba(10,15,26,0.85);border:1px solid rgba(56,189,248,0.2);border-radius:8px;padding:12px 14px;margin-bottom:14px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px">
            <span style="font-weight:700;color:#fff">💾 模型文件存储分布画像 ({model_total_gb:.1f} GB 权重全量)</span>
            <span style="font-family:var(--mono);color:var(--accent-green);font-size:11px">动态三级分层调度</span>
          </div>

          <!-- 彩色三级分布条图 (GPU / RAM / SSD) -->
          <div class="dist-bar">
            <div class="dist-segment gpu" style="width:{pct_gpu:.1f}%" title="GPU 显存: {vram_total_gb:.1f} GB ({pct_gpu:.1f}%)">
              GPU 显存 {pct_gpu:.0f}%
            </div>
            <div class="dist-segment ram" style="width:{pct_ram:.1f}%" title="系统内存: {ram_arena_gb:.1f} GB ({pct_ram:.1f}%)">
              系统 RAM {pct_ram:.0f}%
            </div>
            <div class="dist-segment ssd" style="width:{pct_ssd:.1f}%" title="SSD 磁盘映射: {ssd_cold_gb:.1f} GB ({pct_ssd:.1f}%)">
              SSD 冷数据 {pct_ssd:.0f}%
            </div>
          </div>

          <!-- 分布图例与详细说明 -->
          <div class="dist-legend">
            <div class="dist-legend-item">
              <div class="dist-legend-title" style="color:var(--accent)"><span class="dist-dot gpu"></span>GPU 显存常驻</div>
              <div style="font-family:var(--mono);font-size:13px;font-weight:700;color:#fff">{vram_total_gb:.2f} GB <span style="font-size:10px;color:var(--text-muted)">({pct_gpu:.1f}%)</span></div>
              <div style="font-size:10px;color:var(--text-muted);margin-top:2px">{eng_slots}个热点MoE专家+MTP+Vision</div>
            </div>
            <div class="dist-legend-item">
              <div class="dist-legend-title" style="color:var(--accent-purple)"><span class="dist-dot ram"></span>系统内存锁定 (Arena)</div>
              <div style="font-family:var(--mono);font-size:13px;font-weight:700;color:#fff">{ram_arena_gb:.2f} GB <span style="font-size:10px;color:var(--text-muted)">({pct_ram:.1f}%)</span></div>
              <div style="font-size:10px;color:var(--text-muted);margin-top:2px">常驻计算专家池，超高速零拷贝注入</div>
            </div>
            <div class="dist-legend-item">
              <div class="dist-legend-title" style="color:#94a3b8"><span class="dist-dot ssd"></span>SSD 高速按需映射</div>
              <div style="font-family:var(--mono);font-size:13px;font-weight:700;color:#fff">{ssd_cold_gb:.2f} GB <span style="font-size:10px;color:var(--text-muted)">({pct_ssd:.1f}%)</span></div>
              <div style="font-size:10px;color:var(--text-muted);margin-top:2px">稀疏冷门专家，NVMe 并发秒级载入</div>
            </div>
          </div>
        </div>
        
        <!-- 引擎纯净指标仪表盘 (4小格) -->
        <div class="mon-grid">
          <!-- MoE 专家常驻显存槽位 -->
          <div class="mon-tile">
            <div class="mon-title"><span>MoE 显存专家槽位</span><span style="color:var(--accent)">{eng_slots} Slots</span></div>
            <div class="mon-val">{eng_cache_mib} <span style="font-size:12px;color:var(--text-muted)">MiB (常驻显存)</span></div>
            <div class="progress-bar"><div class="progress-fill" style="width:100%;background:linear-gradient(90deg, #38bdf8, #00f0ff)"></div></div>
            <div class="mon-sub">GPU 显存安全余量: {eng_vram_free_mib} MiB</div>
          </div>

          <!-- 系统内存推理工作区 (Arena) -->
          <div class="mon-tile">
            <div class="mon-title"><span>系统内存计算区</span><span style="color:var(--accent-purple)">Arena 锁定</span></div>
            <div class="mon-val">{eng_arena_gib:.1f} <span style="font-size:12px;color:var(--text-muted)">GiB (RAM)</span></div>
            <div class="progress-bar"><div class="progress-fill" style="width:78%;background:linear-gradient(90deg, #b026ff, #d8b4fe)"></div></div>
            <div class="mon-sub">并发计算线程: {eng_workers} Pool Workers</div>
          </div>

          <!-- 投机解码加速矩阵 -->
          <div class="mon-tile">
            <div class="mon-title"><span>投机预测加速 (Spec)</span><span style="color:var(--accent-yellow)">{eng_spec} 步窗口</span></div>
            <div class="mon-val">{draft_rate_pct} <span style="font-size:12px;color:var(--text-muted)">命中接受率</span></div>
            <div class="progress-bar"><div class="progress-fill" style="width:{min(100, int(drafts_accepted / (drafts_offered or 1) * 100))}%;background:linear-gradient(90deg, #00ff9d, #facc15)"></div></div>
            <div class="mon-sub">累计命中: {drafts_accepted:,} / {drafts_offered:,} 步</div>
          </div>

          <!-- KV 缓存与前缀复用 -->
          <div class="mon-tile">
            <div class="mon-title"><span>KV 前缀复用量 (KV Cache)</span><span style="color:var(--accent-green)">量化: {eng_kv.upper()}</span></div>
            <div class="mon-val">{kv_hit_rate_pct} <span style="font-size:12px;color:var(--text-muted)">复用率</span></div>
            <div class="progress-bar"><div class="progress-fill" style="width:{min(100, int(sm_reused_tot / (sm_prompt_tot or 1) * 100))}%;background:linear-gradient(90deg, #0284c7, #38bdf8)"></div></div>
            <div class="mon-sub">复用: {sm_reused_tot:,} / {sm_prompt_tot:,} tok · 解码: {engine_tok_s} t/s</div>
          </div>
        </div>

        <!-- 4 端口感知联动条 -->
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid rgba(56,189,248,0.12)">
          <div style="font-size:11px;color:var(--text-muted);font-weight:700;margin-bottom:6px">全栈服务协同端口平面：</div>
          <div>{port_chips}</div>
        </div>
      </div>

      <!-- 📅 本月调用热力日历 (Token & 算力监控) -->
      <div class="card">
        <div class="card-header">
          <span>📅 本月调用热力日历 (Token & 算力监控)</span>
          <span style="color:var(--text-muted);font-family:var(--mono);font-size:11px">鼠标悬浮查看详细明细</span>
        </div>
        <div style="margin-top:4px">
          {heat_html}
        </div>
        <div style="display:flex;align-items:center;justify-content:flex-end;gap:6px;margin-top:12px;font-size:11px;color:var(--text-muted);font-family:var(--mono)">
          <span>少</span>
          <span class="cal-square level-0" style="width:14px;height:14px;display:inline-block;border-radius:2px"></span>
          <span class="cal-square level-1" style="width:14px;height:14px;display:inline-block;border-radius:2px"></span>
          <span class="cal-square level-2" style="width:14px;height:14px;display:inline-block;border-radius:2px"></span>
          <span class="cal-square level-3" style="width:14px;height:14px;display:inline-block;border-radius:2px"></span>
          <span class="cal-square level-4" style="width:14px;height:14px;display:inline-block;border-radius:2px"></span>
          <span>多</span>
        </div>
      </div>
    </div>
  </div>
</div>

<div id="heat-tooltip"></div>

<script>
  const heatTooltip = document.getElementById('heat-tooltip');
  const weekdayNames = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function showHeatTooltip(e, el) {{
    const date = el.getAttribute('data-date');
    const reqs = parseInt(el.getAttribute('data-reqs') || '0', 10);
    const prompt = parseInt(el.getAttribute('data-prompt') || '0', 10);
    const comp = parseInt(el.getAttribute('data-comp') || '0', 10);
    const total = parseInt(el.getAttribute('data-total') || '0', 10);
    const cost = el.getAttribute('data-cost') || '0.0000';

    const d = new Date(date);
    const weekday = isNaN(d.getDay()) ? '' : weekdayNames[d.getDay()];

    heatTooltip.innerHTML = `
      <div class="tt-title">📅 ${{date}} (${{weekday}})</div>
      <div class="tt-row"><span>🪙 当日算力价值：</span><span class="tt-val" style="color:var(--accent-green);font-weight:700;">¥${{cost}}</span></div>
      <div class="tt-row"><span>🔢 调用请求次数：</span><span class="tt-val">${{reqs}} 次</span></div>
      <div class="tt-row"><span>📥 输入预填 (Prompt)：</span><span class="tt-val" style="color:#facc15;">${{prompt.toLocaleString()}}</span></div>
      <div class="tt-row"><span>📤 输出生成 (Output)：</span><span class="tt-val" style="color:#b026ff;">${{comp.toLocaleString()}}</span></div>
      <div class="tt-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:6px; padding-top:6px;">
        <span>📊 吞吐总计 (Tokens)：</span><span class="tt-val" style="color:var(--accent);font-weight:700;">${{total.toLocaleString()}}</span>
      </div>
    `;
    heatTooltip.style.display = 'block';
    moveHeatTooltip(e);
  }}

  function moveHeatTooltip(e) {{
    const x = e.clientX + 16;
    const y = e.clientY + 16;
    heatTooltip.style.left = `${{Math.min(window.innerWidth - 260, x)}}px`;
    heatTooltip.style.top = `${{Math.min(window.innerHeight - 200, y)}}px`;
  }}

  function hideHeatTooltip() {{
    heatTooltip.style.display = 'none';
  }}
</script>

</body>
</html>"""


# ---------------- 空回复 / 截断补刀（2026-10-20） ----------------
def _needs_cont(finish, text, has_tools, compl_t, max_tok):
    """
    判断这次回复要不要补一刀：
      ""      不用
      "empty" 空回复（没有正文、也没有工具调用）
      "cut"   正文被截断（token 快撞块边界，但正文只有几百字）
    """
    if not CONT_ENABLE:
        return ""
    if has_tools:
        return ""
    if finish not in ("stop", "length"):
        return ""
    t = (text or "").strip()
    if not t:
        return "empty"               # 只有 reasoning，正文一个字都没有
    near = bool(max_tok and compl_t and compl_t >= max_tok * 0.62)
    hard = bool(max_tok and compl_t and compl_t >= max_tok - 2)
    if hard and len(t) < 20000:
        return "cut"                 # 撞满 max_tokens，必然是截断
    if near and len(t) < 20000 and not _looks_done(t):
        return "cut"                 # 生成了几千 token，正文却停在半截
    if len(t) < 40 and compl_t >= 300 and not _looks_done(t):
        return "empty"               # 想了半天只吐几个字，等于没答
    return ""


def _looks_done(text):
    """正文是否已经收尾（用来决定还要不要继续补）。"""
    t = (text or "").rstrip()
    if not t:
        return False
    if t.endswith("```"):
        return True
    return t[-1] in "。！？!?…：:；;|）)]}】》\"'”’`"


def _stitch(a, b):
    """把续写块接到原文后面：去掉块首多余换行，并消掉重复的尾巴。"""
    a = a or ""
    b = b or ""
    if not a:
        return b
    last = a[-1:]
    if last.isalnum() or ("\u4e00" <= last <= "\u9fff"):
        bb = b.lstrip("\r\n")
        if bb:
            b = bb
    for k in range(min(60, len(a), len(b)), 3, -1):
        if a.endswith(b[:k]):
            return a + b[k:]
    return a + b


def _cont_body(body, text, mode):
    """构造补刀请求：原消息 + 断点正文 + 一句"接着写"。"""
    b = copy.deepcopy(body)
    b.pop("stream", None)
    b.pop("stream_options", None)
    b["stream"] = False
    # 续写不要再沉思一遍（否则 6000 token 思考又吃掉一个块）。
    # 实测：顶层 enable_thinking 引擎**不认**（照样思考）；真正生效的是
    # chat_template_kwargs.enable_thinking=false（实测 reasoning 长度归零）。
    # 两个都带上兼容老引擎，再留一个 512 的小预算兜底。
    b["chat_template_kwargs"] = {"enable_thinking": False}
    b["enable_thinking"] = False
    b["reasoning_budget_tokens"] = 512
    msgs = list(b.get("messages") or [])
    # 补刀不是"新任务"：把开工提示摘掉，免得模型又开始"先说明计划"而不是接着写。
    for _m in msgs:
        if isinstance(_m, dict) and _m.get("role") == "system":
            _c = _text_of(_m.get("content"))
            if WORKSTYLE_MARK in _c:
                _m["content"] = (_c.replace("\n\n" + WORKSTYLE_TEXT, "")
                                   .replace(WORKSTYLE_TEXT, "").strip())
    if mode == "cut" and (text or "").strip():
        msgs = msgs + [{"role": "assistant", "content": text},
                       {"role": "user", "content": CONT_CUT_NUDGE}]
        b.pop("tools", None)        # 续写只要正文，别再起工具调用
        b.pop("tool_choice", None)
    else:
        msgs = msgs + [{"role": "user", "content": CONT_EMPTY_NUDGE}]
    b["messages"] = msgs
    return b


def _cont_result(j):
    ch = (j.get("choices") or [{}])[0] or {}
    msg = ch.get("message") or {}
    return (msg.get("content") or "", msg.get("tool_calls") or [],
            ch.get("finish_reason"), j.get("usage") or {})


def _post_backend(payload, timeout=1800):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(BACKEND + "/v1/chat/completions", data=data,
                                         method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 503 and attempt < 3:
                time.sleep(2.0)
                continue
            break
        except Exception as e:
            last = e
            if attempt < 3:
                time.sleep(1.0)
    raise last if isinstance(last, Exception) else RuntimeError("continuation failed")


def _post_backend_bg(payload):
    """后台跑补刀请求，前台好继续给客户端发心跳（DSH 的读超时不会等到死）。"""
    box, ev = {}, threading.Event()

    def _run():
        try:
            box["j"] = _post_backend(payload)
        except Exception as e:
            box["e"] = f"{type(e).__name__}: {e}"
        finally:
            ev.set()

    threading.Thread(target=_run, daemon=True).start()
    return box, ev


def _sse_chunk(cid, model, created, delta, finish):
    return b"data: " + json.dumps({
        "id": cid or "chatcmpl-gw", "object": "chat.completion.chunk",
        "created": created or int(time.time()), "model": model or "",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _tc_deltas(tools):
    out = []
    for i, tc in enumerate(tools or []):
        fn = tc.get("function") or {}
        out.append({"index": i, "id": tc.get("id") or ("call_gw%d" % i), "type": "function",
                    "function": {"name": fn.get("name") or "",
                                 "arguments": fn.get("arguments") or ""}})
    return out


def _finish_nonstream(j, body, conv_fp, key_name):
    """非流式补刀：原地改 j（content / tool_calls / usage），返回 (轮数, 补刀 prompt 增量)。"""
    if not CONT_ENABLE:
        return 0, 0
    ch = (j.get("choices") or [{}])[0] or {}
    msg = ch.get("message") or {}
    text = msg.get("content") or ""
    tools = list(msg.get("tool_calls") or [])
    usage = j.get("usage") or {}
    mt = int(body.get("max_tokens") or 0)
    mode = _needs_cont(ch.get("finish_reason"), text, tools,
                       int(usage.get("completion_tokens") or 0), mt)
    rounds, prompt_add = 0, 0
    while mode and rounds < CONT_MAX_ROUNDS:
        rounds += 1
        try:
            cj = _post_backend(_cont_body(body, text, mode))
        except Exception as e:
            log_line("gateway", f"[CONT] {key_name} conv={conv_fp} 第{rounds}次补刀失败：{type(e).__name__}: {e}")
            break
        ctext, ctools, cfin, cusage = _cont_result(cj)
        if ctext:
            text = _stitch(text, ctext)
            msg["content"] = text
        if ctools:
            tools = tools + list(ctools)
            msg["tool_calls"] = tools
            ch["finish_reason"] = "tool_calls"
        cu = j.get("usage") or {}
        cu["completion_tokens"] = int(cu.get("completion_tokens") or 0) + int(cusage.get("completion_tokens") or 0)
        cu["total_tokens"] = int(cu.get("prompt_tokens") or 0) + int(cu.get("completion_tokens") or 0)
        j["usage"] = cu
        prompt_add += int(cusage.get("prompt_tokens") or 0)
        log_line("gateway", f"[CONT] {key_name} conv={conv_fp} {mode}→第{rounds}次："
                            f"+{len(ctext)} 字 / {len(ctools)} 个工具调用")
        if ctools or not ctext:
            break
        c_compl = int(cusage.get("completion_tokens") or 0)
        ch["finish_reason"] = "length" if (mt and c_compl >= mt - 2) else "stop"
        if _looks_done(text) and not (mt and c_compl >= mt - 2):
            break
        mode = "cut"
    return rounds, prompt_add


# ---------------- HTTP ----------------
class GWHandler(BaseHTTPRequestHandler):
    server_version = "StrataGW/1.0"

    def log_message(self, fmt, *args):
        log_line("gateway", f"[HTTP] {self.address_string()} {fmt % args}")

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _sse(self, payload):
        """往流里写一块（bytes），失败返回 False。"""
        try:
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except Exception:
            return False

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/health", "/api/health"):
            info = backend_info()
            self._send(200, {"status": "ok", "service": "strata-gateway",
                             "backend": BACKEND, "backend_up": info["up"],
                             "model": info["model"], "helper": bool(HELPER_URL)})
        elif path == "/dashboard":
            body = dashboard().encode("utf-8")
            self._send(200, raw=body, ctype="text/html")
        elif path in ("/v1/models", "/models"):
            info = backend_info()
            if not info["up"]:
                info = wait_for_backend(timeout=5)
            if not info["up"]:
                fallback_models = _with_profiles({
                    "object": "list",
                    "data": [{
                        "id": "qwen3.8-flash-next-abliterated-iq3_s",
                        "object": "model",
                        "status": {"value": "loading"},
                        "meta": {"n_ctx": 131072},
                        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}
                    }]
                })
                self._send(200, fallback_models)
                return
            try:
                req = urllib.request.Request(BACKEND + "/v1/models", method="GET")
                with urllib.request.urlopen(req, timeout=10) as r:
                    raw_models = r.read()
                try:
                    parsed = _with_profiles(json.loads(raw_models.decode("utf-8")))
                    raw_models = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
                except Exception:
                    pass
                self._send(200, raw=raw_models)
            except Exception as e:
                self._send(502, {"error": {"message": f"后端异常：{e}", "code": "backend_error"}})
        elif path in ("/v1/billing", "/billing", "/stats", "/v1/stats"):
            today = time.strftime("%Y-%m-%d")
            bill = {}
            try:
                if os.path.exists(BILL_FILE):
                    with open(BILL_FILE, "r", encoding="utf-8") as f:
                        bill = json.load(f)
            except Exception:
                pass
            with _recent_lock:
                rec_list = list(reversed(_recent[-20:]))
            self._send(200, {
                "status": "ok",
                "today": today,
                "billing_today": bill.get(today, {}),
                "billing_all": bill,
                "peak_tps": _peak_tps["v"],
                "active_queue": _active["n"],
                "recent_requests": rec_list
            })
        elif path in ("/api/ports", "/v1/api/ports"):
            info = backend_info()
            ports_info = [
                {"port": 8081, "name": "网关", "ok": True, "desc": "智能协同统一中枢"},
                {"port": 8083, "name": "主脑", "ok": bool(info["up"]), "desc": "Strata MoE 推理底座"},
                {"port": 8087, "name": "工具", "ok": False, "desc": "MCP 工具执行平面"}
            ]
            for p in ports_info:
                if p["port"] == 8087:
                    try:
                        req = urllib.request.Request(f"http://127.0.0.1:{p['port']}/health")
                        with urllib.request.urlopen(req, timeout=0.2) as resp:
                            p["ok"] = (resp.status == 200)
                    except Exception:
                        p["ok"] = False
            self._send(200, {"status": "ok", "ports": ports_info})
        else:
            self._send(404, {"error": {"message": "not found", "code": "not_found"}})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/v1/messages", "/v1/responses",
                        "/v1/messages/count_tokens"):
            self._send(404, {"error": {"message": "not found", "code": "not_found"}})
            return
        body = self._read_body()
        if body is None:
            self._send(413, {"error": {"message": "请求体超 50MB 上限", "code": "too_large"}})
            return
        key_id, key_name = identify(self.headers.get("Authorization"),
                                    self.headers.get("x-api-key", ""))
        info = backend_info()
        if not info["up"]:
            log_line("gateway", f"[WAIT] {key_name} {path}：主脑引擎尚未就绪，网关缓冲排队等待握手(最长90s)...")
            info = wait_for_backend(timeout=90)
            if not info["up"]:
                log_line("gateway", f"[REJECT] {key_name} {path}：主脑握手超时（90秒内未就绪）")
                self._send(503, {"error": {"message": "主脑启动超时（:8083 未就绪），请检查黑窗口输出",
                                           "code": "backend_down"}})
                return
            log_line("gateway", f"[HANDSHAKE] {key_name} {path}：主脑已完成握手就绪！开始直通转发")
        # 0) 虚拟模型档位：名字只是参数预设，必须在自适应判定之前生效
        profile = apply_profile(body) if isinstance(body, dict) else None
        conv_fp = _conv_fp(body.get("messages")) if isinstance(body, dict) else "-"
        conv_sid = _conv_id(body.get("messages")) if isinstance(body, dict) else "-"
        think_chars = _thinking_chars(body.get("messages")) if isinstance(body, dict) else 0
        strip_chars = 0
        if STRIP_THINKING and isinstance(body, dict):
            strip_chars = apply_thinking_policy(body)
        # 1) 任务自适应采样（X-Task-Type）：只补客户端没写的，max_tokens 按地板顶
        task, filled, raised, meta = apply_task_type(
            body, self.headers.get("X-Task-Type", ""), path)
        if meta.get("sticky"):
            with _cache_lock:
                _cache_stat["sticky"] += 1
        # 2) 档位 / 别名 → 真实模型
        if isinstance(body, dict) and isinstance(body.get("model"), str):
            if profile:
                orig = profile
                body["model"] = backend_info().get("model") or body["model"]
            else:
                live, orig = resolve_alias(body["model"])
                body["model"] = live
        else:
            orig = None
        # 3) 工具死循环检测（默认只告警；STRATA_LOOP_BLOCK>0 才拦截）
        loop_sig, loop_n = detect_tool_loop(
            body.get("messages") if isinstance(body, dict) else None)
        if loop_sig and loop_n >= LOOP_WARN:
            log_line("gateway", f"[LOOP] {key_name} conv={conv_fp}：同一工具调用连续 {loop_n} 次 {str(loop_sig)[:90]}")
            if LOOP_BLOCK and loop_n >= LOOP_BLOCK:
                self._send(409, {"error": {
                    "message": f"工具调用死循环：连续 {loop_n} 次完全相同的调用，已拦截",
                    "code": "tool_loop", "type": "invalid_request_error",
                    "repeats": loop_n, "signature": str(loop_sig)[:300]}})
                return
        # 4) 记忆注入（只对 chat；messages 的 system 网关不改，原样透传）
        if path == "/v1/chat/completions":
            inject_memory_openai(body)
            inject_workstyle_openai(body)
            guarded = guard_or_trim(body, info["n_ctx"])
            if guarded[0] is None:
                est_over = guarded[2]
                log_line("gateway", f"[REJECT] {key_name}：超上下文上限 ~{est_over} tok")
                self._send(413, {"error": {
                    "message": (f"输入约 {est_over} tok，超过主脑上下文 {info['n_ctx']} tok"
                                f"（目标水位 {int(info['n_ctx'] * TARGET_RATIO)} tok）"),
                    "code": "context_overflow",
                    "type": "invalid_request_error",
                    "prompt_tokens": est_over,
                    "n_ctx": info["n_ctx"],
                    "target_tokens": int(info["n_ctx"] * TARGET_RATIO),
                    "overflow_tokens": max(0, est_over - info["n_ctx"]),
                    "hint": "网关已压缩旧工具结果并尝试丢弃最早轮次；仍超限请缩短工具输出或减少历史"}})
                return
            body, trimmed, est = guarded
        elif path == "/v1/responses":
            # Responses 走原生透传，只做超限兜底
            n_est = _responses_tokens(body)
            if n_est and n_est > info["n_ctx"] * GUARD_RATIO:
                log_line("gateway", f"[REJECT] {key_name}：Responses 请求 ~{n_est} tok 超限")
                self._send(413, {"error": {
                    "message": f"输入约 {n_est} tok，超过主脑上下文 {info['n_ctx']} tok",
                    "code": "context_overflow", "type": "invalid_request_error",
                    "prompt_tokens": n_est, "n_ctx": info["n_ctx"],
                    "hint": "请缩短 input 或减少历史条目"}})
                return
            trimmed, est = False, n_est
        else:
            trimmed, est = False, 0
        stream = bool(isinstance(body, dict) and body.get("stream"))
        t0 = time.time()
        first_t, out_text, usage = 0.0, [], {}
        cont_rounds, cont_prompt = 0, 0
        _active["n"] += 1
        headers_sent = False
        aborted = False
        try:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            r = None
            # 主脑启动期间的 503 EngineStarting 或端口刚通时的连接抖动自动重试
            for attempt in range(45):
                try:
                    req = urllib.request.Request(BACKEND + path, data=data, method="POST",
                                                 headers={"Content-Type": "application/json"})
                    r = urllib.request.urlopen(req, timeout=1800)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 503 and attempt < 44:
                        try:
                            raw_err = e.read()
                        except Exception:
                            raw_err = b""
                        low_err = raw_err.lower()
                        if b"starting" in low_err or b"not running" in low_err or b"busy" in low_err:
                            if attempt % 5 == 0:
                                log_line("gateway", f"[WAIT] 主脑启动中(503)：排队重试第 {attempt+1} 次...")
                            time.sleep(2.0)
                            continue
                    raise
                except (urllib.error.URLError, ConnectionResetError, ConnectionRefusedError) as e:
                    if attempt < 44:
                        if attempt % 5 == 0:
                            log_line("gateway", f"[WAIT] 主脑连接重试中({type(e).__name__})：排队第 {attempt+1} 次...")
                        time.sleep(2.0)
                        continue
                    raise

            with r:
                if not stream:
                    raw = r.read()
                    try:
                        _j = json.loads(raw)
                        if path == "/v1/messages/count_tokens":
                            est = int(_j.get("input_tokens", 0))
                        elif path == "/v1/chat/completions":
                            cont_rounds, cont_prompt = _finish_nonstream(
                                _j, body, conv_fp, key_name)
                            if cont_rounds:
                                raw = json.dumps(_j, ensure_ascii=False).encode("utf-8")
                        usage = (_j.get("usage")) or {}
                    except Exception as e:
                        log_line("gateway", f"[CONT] 非流式收尾异常：{type(e).__name__}: {e}")
                    self._send(200, raw=raw)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    headers_sent = True
                    elt = (path == "/v1/chat/completions")
                    held_finish = held_usage = held_done = None
                    finish_reason = None
                    has_tools = False
                    chunk_id, chunk_model, chunk_created = "", "", 0
                    for line in r:
                        if not first_t:
                            first_t = time.time()
                        stripped = line.strip()
                        if elt and stripped.startswith(b"data:"):
                            payload = stripped[5:].strip()
                            if payload == b"[DONE]":
                                held_done = line      # 先扣下，补完刀再发
                                continue
                            j = None
                            if payload:
                                try:
                                    j = json.loads(payload)
                                except Exception:
                                    j = None
                            if isinstance(j, dict):
                                # 注意：引擎会把 finish_reason 和 usage 塞进**同一个** chunk
                                # （实测最后一块长这样：
                                #  {"choices":[{"delta":{},"finish_reason":"stop"}],
                                #   "usage":{...}}），所以必须先看 choices 再看 usage，
                                # 否则 finish_reason 永远读不到，补刀永远不触发。
                                u = j.get("usage")
                                has_usage = isinstance(u, dict)
                                if has_usage:
                                    usage = {**usage, **u}
                                chs = j.get("choices") or []
                                handled = False
                                if chs:
                                    if not chunk_id:
                                        chunk_id = j.get("id") or ""
                                        chunk_model = j.get("model") or ""
                                        chunk_created = j.get("created") or 0
                                    ch0 = chs[0] or {}
                                    d = ch0.get("delta") or {}
                                    if d.get("tool_calls"):
                                        has_tools = True
                                    c = d.get("content")
                                    if isinstance(c, str) and c:
                                        out_text.append(c)
                                    if ch0.get("finish_reason"):
                                        finish_reason = ch0["finish_reason"]
                                        if isinstance(c, str) and c:
                                            # 正文和收尾挤在同一块：正文先补发，收尾块扣下
                                            if not self._sse(_sse_chunk(
                                                    chunk_id, chunk_model, chunk_created,
                                                    {"content": c}, None)):
                                                aborted = True
                                                break
                                            # 扣下的块里不能再带一遍正文，否则客户端会收到重复内容
                                            d["content"] = ""
                                            held_finish = (
                                                b"data: " + json.dumps(j, ensure_ascii=False).encode("utf-8") + b"\n\n", j)
                                        else:
                                            held_finish = (line, j)
                                        handled = True
                                if has_usage:
                                    # 带 usage 的块整块扣下（可能同时带 finish_reason），
                                    # 等补刀完再改写 usage / finish_reason 一起补发
                                    held_usage = (line, j)
                                    if handled:
                                        held_finish = None
                                    continue
                                if handled:
                                    continue
                        if not self._sse(line):
                            aborted = True   # 客户端断了：跳出后关闭上游，让引擎停止生成
                            break
                        if (not elt) and stripped.startswith(b"data:"):
                            payload = stripped[5:].strip()
                            if payload and payload != b"[DONE]":
                                try:
                                    j = json.loads(payload)
                                    # OpenAI: usage 在顶层；Anthropic: message_start 里在 message.usage
                                    u = j.get("usage")
                                    if not isinstance(u, dict):
                                        _m = j.get("message")
                                        u = _m.get("usage") if isinstance(_m, dict) else None
                                    if isinstance(u, dict):
                                        usage = {**usage, **u}
                                    # Anthropic 的文本增量在 delta.text（不是 choices[].delta.content）
                                    d = j.get("delta")
                                    if isinstance(d, dict) and isinstance(d.get("text"), str):
                                        out_text.append(d["text"])
                                except Exception:
                                    pass
                    # ---- 空回复 / 截断：补刀（只对 OpenAI 流式；Anthropic/Responses 原样透传） ----
                    if elt and not aborted and not has_tools:
                        mt = int(body.get("max_tokens") or 0)
                        mode = _needs_cont(finish_reason, "".join(out_text), has_tools,
                                           int(usage.get("completion_tokens") or 0), mt)
                        while mode and cont_rounds < CONT_MAX_ROUNDS and not aborted:
                            cont_rounds += 1
                            box, ev = _post_backend_bg(_cont_body(body, "".join(out_text), mode))
                            while not ev.wait(5.0):
                                # 空 delta 心跳：别让客户端读超时把还在生成的连接掐掉
                                if not self._sse(_sse_chunk(chunk_id, chunk_model,
                                                            chunk_created, {}, None)):
                                    aborted = True
                                    break
                            if aborted:
                                break
                            if "e" in box:
                                log_line("gateway", f"[CONT] {key_name} conv={conv_fp} "
                                                    f"第{cont_rounds}次补刀失败：{box['e']}")
                                break
                            ctext, ctools, cfin, cusage = _cont_result(box.get("j") or {})
                            if ctext:
                                out_text.append(ctext)
                                for _i in range(0, len(ctext), 240):
                                    if not self._sse(_sse_chunk(
                                            chunk_id, chunk_model, chunk_created,
                                            {"content": ctext[_i:_i + 240]}, None)):
                                        aborted = True
                                        break
                            if ctools:
                                if not self._sse(_sse_chunk(chunk_id, chunk_model, chunk_created,
                                                            {"tool_calls": _tc_deltas(ctools)}, None)):
                                    aborted = True
                                has_tools = True
                                finish_reason = "tool_calls"
                            cu = usage if isinstance(usage, dict) else {}
                            cu["completion_tokens"] = (int(cu.get("completion_tokens") or 0)
                                                       + int(cusage.get("completion_tokens") or 0))
                            cu["total_tokens"] = (int(cu.get("prompt_tokens") or 0)
                                                  + int(cu.get("completion_tokens") or 0))
                            usage = cu
                            cont_prompt += int(cusage.get("prompt_tokens") or 0)
                            log_line("gateway", f"[CONT] {key_name} conv={conv_fp} {mode}→"
                                                f"第{cont_rounds}次：+{len(ctext)} 字 / {len(ctools)} 个工具调用")
                            if aborted or ctools or not ctext:
                                break
                            c_compl = int(cusage.get("completion_tokens") or 0)
                            # 补刀成功后收尾原因要改：原来的 length/stop 已经不代表最终状态
                            finish_reason = "length" if (mt and c_compl >= mt - 2) else "stop"
                            if _looks_done("".join(out_text)) and not (mt and c_compl >= mt - 2):
                                break
                            mode = "cut"
                    # ---- 把扣下的 usage / 收尾 / [DONE] 补发 ----
                    if not aborted and elt:
                        if held_usage:
                            _ln, _ju = held_usage
                            _ju["usage"] = usage
                            # 有些引擎把 finish_reason 和 usage 放同一块；
                            # 补刀后收尾原因可能从 stop 变成 tool_calls，要一起改写
                            if (finish_reason and _ju.get("choices")
                                    and isinstance(_ju["choices"][0], dict)
                                    and _ju["choices"][0].get("finish_reason")):
                                _ju["choices"][0]["finish_reason"] = finish_reason
                            if not self._sse(b"data: " + json.dumps(
                                    _ju, ensure_ascii=False).encode("utf-8") + b"\n\n"):
                                aborted = True
                        if not aborted:
                            if held_finish:
                                _ln, _jf = held_finish
                                if finish_reason and _jf.get("choices"):
                                    _jf["choices"][0]["finish_reason"] = finish_reason
                                    _ln = b"data: " + json.dumps(
                                        _jf, ensure_ascii=False).encode("utf-8") + b"\n\n"
                                if not self._sse(_ln):
                                    aborted = True
                            elif not held_usage:
                                if not self._sse(_sse_chunk(chunk_id, chunk_model, chunk_created,
                                                            {}, finish_reason or "stop")):
                                    aborted = True
                        if not aborted:
                            if held_done:
                                if not self._sse(held_done):
                                    aborted = True
                            else:
                                if not self._sse(b"data: [DONE]\n\n"):
                                    aborted = True
            ms = int((time.time() - t0) * 1000)
            # OpenAI 用 prompt_tokens/completion_tokens；Anthropic 用 input_tokens/output_tokens
            prompt_t = int(usage.get("prompt_tokens") or usage.get("input_tokens") or est or 0)
            compl_t = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            if stream and not compl_t and out_text:
                compl_t = rough_tokens("".join(out_text))
            tps = round(compl_t / max(0.1, (time.time() - (first_t or t0))), 1) if compl_t else 0.0
            # TTFT 只对流式真实（首块到达）；非流式没有首字概念，记 "-" 不造数
            ttft_ms = int((first_t - t0) * 1000) if (stream and first_t) else None
            if aborted:
                log_line("gateway", f"[ABORT] {key_name} {path} conv={conv_fp}：客户端断开，"
                                    f"已关闭上游（收 {len(out_text)} 段，{int((time.time() - t0) * 1000)}ms）")
            if path == "/v1/messages/count_tokens":
                log_line("gateway", f"{key_id} {path} ~{est} tok（纯计数，不计费）")
                return
            # 前缀缓存复用记账（引擎在 usage.prompt_tokens_details.cached_tokens 里给）
            ptd = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
            cached_t = int((ptd or {}).get("cached_tokens")
                           or (usage.get("cache_read_input_tokens") if isinstance(usage, dict) else 0)
                           or 0)
            cached_t = max(0, min(cached_t, prompt_t))
            cliff, prev_cached = _cache_note(conv_sid, prompt_t, cached_t)
            if cliff:
                log_line("gateway", f"[CACHE] {key_name} sid={conv_sid} conv={conv_fp} 前缀断崖："
                                    f"上次复用 {prev_cached} tok，本次只剩 {cached_t} tok"
                                    f"（prompt {prompt_t}）——查 system 头/思考档位/记忆注入是否变了")
            # 补刀的 prompt 几乎全命中缓存，按命中价计；生成 token 已经并进 compl_t
            bill_prompt = prompt_t + cont_prompt
            bill_cached = min(bill_prompt, cached_t + cont_prompt)
            cost = bill_record(key_id, bill_prompt, compl_t, cached_tok=bill_cached)
            rec = {"t": time.strftime("%H:%M:%S"), "key": key_id,
                   "model": body.get("model", ""), "path": path,
                   "prompt": prompt_t, "completion": compl_t, "tps": tps,
                   "ttft": ttft_ms if ttft_ms is not None else "-",
                   "ms": ms, "trimmed": trimmed,
                   "cache": cached_t,
                   "reuse": round(cached_t / prompt_t * 100, 1) if prompt_t else 0.0,
                   "think": think_chars, "profile": profile or "",
                   "sticky": bool(meta.get("sticky")), "aborted": aborted,
                   "task": task or "", "task_filled": filled,
                   "alias": orig if orig and orig != body.get("model") else "",
                   "cont": cont_rounds}
            recent_add(rec)
            log_line("main", f"{key_id} {path} model={rec['model']} "
                             f"in/out={prompt_t}/{compl_t} cache={cached_t}/{prompt_t} conv={conv_fp} {ms}ms "
                             f"{('ttft=' + str(ttft_ms) + 'ms ') if ttft_ms is not None else ''}"
                             f"{tps}t/s "
                             f"¥{cost}{' TRIM' if trimmed else ''}"
                             f"{' ABORT' if aborted else ''}"
                             f"{' PROFILE:' + profile if profile else ''}"
                             f"{' STICKY' if meta.get('sticky') else ''}"
                             f"{' THINK:' + str(think_chars) if think_chars else ''}"
                             f"{' STRIP:' + str(strip_chars) if strip_chars else ''}"
                             f"{' TASK:' + task + '[' + ','.join(filled) + ']' if task else ''}"
                             f"{' MAXTOK-RAISED' if raised else ''}"
                             f"{' CONT:' + str(cont_rounds) if cont_rounds else ''}"
                             f"{' alias:' + str(orig) if rec['alias'] else ''}")
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            log_line("gateway", f"[UPSTREAM {e.code}] {key_name} {path}：{raw[:200]}")
            if not headers_sent:
                self._send(e.code if e.code else 502, raw=raw or b"{}")
            else:
                try:
                    self.wfile.write(b"\ndata: {\"error\": {\"message\": \"upstream error\"}}\n\ndata: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
        except Exception as e:
            log_line("gateway", f"[ERROR] {key_name} {path}：{type(e).__name__}: {e}")
            if not headers_sent:
                try:
                    self._send(502, {"error": {"message": f"网关转发异常：{e}", "code": "gateway_error"}})
                except Exception:
                    pass
            else:
                try:
                    self.wfile.write(f"\ndata: {{\"error\": {{\"message\": \"{e}\"}}}}\n\ndata: [DONE]\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass
        finally:
            _active["n"] = max(0, _active["n"] - 1)


def run_selftest():
    import threading as th
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), GWHandler)
    srv.daemon_threads = True
    th.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(1.5)
    ok = True
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
            h = json.loads(r.read().decode("utf-8"))
            print("health:", h)
            ok = h.get("status") == "ok"
    except Exception as e:
        print("health FAIL:", e)
        ok = False
    srv.shutdown()
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    log_line("gateway", f"网关启动 :{PORT} → {BACKEND}（前端统一入口 /v1）")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), GWHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(run_selftest() if SELFTEST else (main() or 0))
