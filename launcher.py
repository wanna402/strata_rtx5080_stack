import os
import sys
import json
import shutil
import subprocess

# Force UTF-8 on stdout/stderr
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CUSTOM_DIR = r"H:\Strata-custom"
CONFIGS_DIR = os.path.join(CUSTOM_DIR, "configs")
MAIN_DIR = r"H:\Strata-main"
ACTIVE_CONFIG_PATH = os.path.join(MAIN_DIR, "strata-abliterated-iq3_s.json")
NOVISION_CONFLICT_PATH = os.path.join(MAIN_DIR, "strata-abliterated-iq3_s-novision.json")

SCHEME_A_SRC = os.path.join(CONFIGS_DIR, "strata-scheme-A-vision.json")
SCHEME_B_SRC = os.path.join(CONFIGS_DIR, "strata-scheme-B-novision.json")

# =============================================================================
# 【网关可调开关 · 以后复查看这里】            记录日期：2026-10-19
# -----------------------------------------------------------------------------
# gateway/gateway_8081.py 只在启动时读一次环境变量；本文件用 subprocess.Popen
# 拉起网关(:8081) 和托盘，子进程原样继承这里的环境 —— 在下面赋值即可生效。
# 生效方式：托盘"保存退出" → 重新运行 launcher.py（改完要重启，热改不生效）。
# 注意：如果绕过本文件、直接双击 stack_tray.py 启动，这里的赋值不生效，
#       那种情况需要把变量写进系统环境变量。
#
#   STRATA_EFFORT_STICKY=0        关掉"会话内思考档位粘住"（默认开）
#   STRATA_STRIP_THINKING=1       从历史消息里摘掉思考块（默认关，理由见下）
#   STRATA_EFFORT_TTL=3600        档位记忆保留多少秒（默认 3600）
#   STRATA_LOOP_WARN=3            同一工具调用连续 N 次 → 只告警（默认 3）
#   STRATA_LOOP_BLOCK=0           设 >0：连续 N 次直接 409 拦截（默认 0=不拦）
#   STRATA_CACHE_WARN=0.5         复用率掉到上次的 50% → 记"前缀断崖"（默认 0.5）
#   STRATA_CACHE_WARN_MIN=20000   prompt 小于此值不判断崖（默认 20000）
#   STRATA_CONT=0                 关掉"空回复重试 / 截断续写"补刀（默认开）
#   STRATA_CONT_MAX=2             一次请求最多补几刀（默认 2）
#   STRATA_WORKSTYLE=0            关掉 system 末尾的"开工方式"提示（默认开）
#
# ---- 为什么 STRATA_STRIP_THINKING 保持关闭（结论：不开）-------------------
#   2026-10-19 本机实测：
#     · 12,000 字符历史思考 = 3,664 tok（约 0.305 tok/字符）
#     · 真实长视图 THINK:18988 ≈ 5,800 tok，只占 70,519 prompt 的 8%
#     · 折合预填约 1.8 秒；而长视图单次 30–60 秒，瓶颈在解码不在预填
#     · 上下文长期 54%（70K/131K），剪枝阈值 120,586 一次都没触发
#   另注：引擎不认 chat_template_kwargs.preserve_thinking（传了 prompt_tokens
#        一模一样），真要省只能在网关侧删 reasoning_content —— 已实现，留开关。
#   取舍：省 8% 换"模型看不到前几轮被否掉的方案"，对 agentic 编码不划算。
#
# ---- 什么时候回来复查 -------------------------------------------------------
#   满足任一条再考虑打开（建议改成"只保留最近 3 条思考"，比全删更稳）：
#     1) prompt 长期超过 105K（80%），或日志出现 [GUARD] 剪枝；
#     2) 模型反复推导同一结论、或忘记前面已经排除的方案。
#   打开方式：取消下面这一行的注释，然后重启整栈。
# os.environ["STRATA_STRIP_THINKING"] = "1"
#
# ---- 同日落地的五项网关优化（复查时按函数名找）-----------------------------
#   1 档位粘住     _sticky_effort() / _conv_id()        日志标记 STICKY
#   2 缓存 KPI     _cache_note()                        日志 cache=命中/总计
#   3 思考摘除     apply_thinking_policy()              默认关（见上）
#   4 剪枝 v2      guard_or_trim() / _compress_old_tools() / _drop_oldest_turns()
#   5 模板结论     官方模板探针 5/5 通过（含 JSON 字符串工具参数），froggeric 不换
#   另有：虚拟档位 strata-fast/code/deep/nothink（/v1/models 可见）、
#         断连 [ABORT]、结构化 413、工具死循环检测、/v1/responses 超限兜底
# =============================================================================
#
# =============================================================================
# 【2026-10-20 · DSH"做一点点就停 / 开头就断"的两个根因与修法】
# -----------------------------------------------------------------------------
# 现象：agentic 长任务里，模型要么回一条空消息（只有思考、没有正文），
#       要么正文刚开个头就断（353 字 / 629 字），DSH 以为这一轮说完了。
#
# ---- 根因 A：分类器把 harness 注入块当成了"人类提问" ------------------------
#   detect_dynamic_difficulty() 原来会扫描整段最后一条消息，hindsight 记忆块
#   里的 "architecture"、运行上下文里的"深度分析"等词把一句"继续"判成
#   deep_reasoning/high/6144。后果有两层：
#     · reasoning_effort 被模板写进 system 头 → **整条前缀缓存作废**
#       （实测缓存归零、重算 15.4s）；
#     · 档位粘住（只升不降）把 high 带进最后一轮，而 6144 的预算正好等于
#       引擎的块边界 → 思考独占整块。
#   修法：新增 _INJECTED_PREFIXES / _is_injected_block()，扫描"人类真实提问"
#   时跳过 harness/插件注入块；短续句（继续/接着/然后/ok/go on）优先判
#   quick_reply/low/1024。回归脚本：%TEMP%\opencode\cls_test.py（7 用例）。
#
# ---- 根因 B：引擎分块 + 空回复 -----------------------------------------------
#   引擎把一次生成按 min(reasoning_budget_tokens, 0.75*max_tokens) 切块，实测：
#       预算 256  / max 4096  → 256 + 3092   (total 3366)
#       预算 6144 / max 8192  → 6145 + 2030  (total 8192)
#       19:46 真实请求         → 6145 + 182   (total 6344，正文只有 353 字)
#   块边界正好落在"思考刚结束、正文刚要展开"时，续写块经常写几十字就 EOS；
#   另一种是纯空回复：只有 reasoning、没有 content、也没有 tool_calls。
#   修法：网关侧补刀（gateway_8081.py / _needs_cont + _cont_body）：
#     · 空回复 → 追加一句"继续"再请求一次；
#     · 截断   → 撞满 max_tokens，或 >=62% max 且正文未收尾
#                → 把已写正文当 assistant 回传 + "接着写"，只把新正文接上去；
#                _stitch() 去掉重复尾巴，_looks_done() 判断是否已经收尾。
#     · 最多 STRATA_CONT_MAX 刀（默认 2），补刀期间每 5s 发空 delta 心跳，
#       防止客户端读超时；补刀只对 /v1/chat/completions，Anthropic/Responses
#       原样透传。
#   补刀请求强制关思考：chat_template_kwargs.enable_thinking=false
#     （实测顶层 enable_thinking 引擎**不认**，照样思考；kwargs 才真生效）
#     再配 reasoning_budget_tokens=512 兜底。
#   ★ 血泪坑：引擎把 finish_reason 和 usage 放在**同一个 chunk** 里，解析必须
#     先看 choices 再看 usage —— 之前先判 usage 就 continue，finish_reason
#     恒为 None，补刀逻辑写好了却一次都没触发。回归：%TEMP%\opencode\e2e_cont.py
#     （假后端两种形状都测：分开的、挤在同一块的）。
#
# ---- 日志里怎么看 ------------------------------------------------------------
#   8081_main : 行尾 CONT:1 表示这一请求补过刀（out 里已含补刀生成量）
#   8081_gateway : [CONT] <key> conv=xxxx empty|cut→第N次：+N 字 / N 个工具调用
# =============================================================================
#
# =============================================================================
# 【2026-10-20 · 工具卡死 + "先找能力再动手"】
# -----------------------------------------------------------------------------
# ---- A 工具卡死（ddg-search 19:42 连续两次超时，白等 60 秒）----------------
#   · gateway/mcp_servers.local.json：ddg-search 的 timeout 60s → 20s。
#     搜索 20 秒不回，基本就是网络/代理死了，等 60 秒纯浪费。
#   · gateway/mcp_gateway_8087.py::StdioBridge._with_retry：
#     改成"崩了才重拉重试，超时不重试"。以前超时也重试 = 等待翻倍；
#     现在超时立刻杀掉子进程（下次调用是干净进程），并把
#     "不要再重试同一个请求，改用其他搜索/抓取方式"写进错误文本，
#     模型在 tool 结果里看得到，会自己换方案。
#   · 新日志标记：8087 日志里的 [TIMEOUT] <server> ... 不重试，已重启子进程
#   · 21:00 现场：本机直连 DuckDuckGo / Google 都是 16s 超时，Bing 0.4s。
#     为避免模型白等，先加了 web-search（Bing，本地技能，0.4s），ddg 暂关。
#   · 21:10 复查：Clash 内核（verge-mihomo.exe）起来后，DDG 恢复可达
#     （0.5-1.6s，Google 也通了）——**ddg 没坏，是当时 Clash 没开**。
#     于是 ddg 重新 enabled=true，timeout 压到 12s。
#   · 结论：两个搜索都留着，分工明确 ——
#       web-search = Bing，免代理、永远能用，默认选它；
#       ddg-search = DDG，需要 Clash/TUN 开着，结果质量通常更好；
#       ddg 超时（12s）基本就是 Clash 没开，模型按"开工方式"第 2 条改用 web-search。
#   · 实测：ddg 冷启动 6.9s（uvx 拉进程）、热调用 1.3s；web-search 0.43s。
#
# ---- B 开工方式（每个新任务先选工具，别硬写、别死磕）-----------------------
#   gateway_8081.py::inject_workstyle_openai() 在 system **末尾**贴一段固定指令：
#     1) 先挑现成的（:8087 六个工具 + harness 自带 web/文件/shell/子代理 +
#        技能与插件清单），选一个、一行说明理由，不罗列清单；
#     2) 工具失败或超时：同一工具最多重试 1 次，再失败换备选，
#        连续 2 次失败就停下说明原因（禁止死循环）；
#     3) 3 步以上长任务先用一句话说清"分几步、每步用什么"。
#   为什么放末尾：harness 的 system 很长（身份+工具+技能清单），
#     末尾离生成最近、照做率最高。
#   为什么不打穿缓存：这段文字每轮**完全一样**，前缀缓存照常命中。
#   幂等：已贴过不再贴；补刀请求会主动摘掉这段（补刀是"接着写"，不是新任务）。
#   关掉：STRATA_WORKSTYLE=0。
#   验证：让模型复述【开工方式】，能原样吐出 = 注入生效（2026-10-20 已验）。
# =============================================================================

def get_system_ram_gb():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.total / (1024**3), 1), round(vm.available / (1024**3), 1)
    except Exception:
        return 64.0, 50.0

def get_engine_version():
    build_json = os.path.join(MAIN_DIR, "engine", "BUILD.json")
    if os.path.exists(build_json):
        try:
            with open(build_json, "r", encoding="utf-8") as f:
                d = json.load(f)
                ver = d.get("version", "0.1.47")
                cuda = d.get("cuda", "13.0")
                return f"{ver} (CUDA {cuda})"
        except Exception:
            pass
    return "0.1.47 (CUDA 13.0)"

def check_vision_files():
    vision_exe = os.path.join(MAIN_DIR, "engine", "strata-vision.exe")
    mmproj = r"H:\models\mmproj-Qwen3.8-Flash-Next-BF16.gguf"
    return os.path.exists(vision_exe) and os.path.exists(mmproj)

def main():
    os.chdir(MAIN_DIR)
    
    # 1. Gather dynamic self-check info
    engine_ver = get_engine_version()
    total_ram, avail_ram = get_system_ram_gb()
    vision_ok = check_vision_files()
    vision_status = "就绪 (strata-vision + mmproj)" if vision_ok else "缺失/未就绪"
    
    # 2. Display Banner & Self-Check Panel
    print("=" * 68)
    print("       Strata 智能栈一键启动器 (定制优化层: H:\\Strata-custom)")
    print("=" * 68)
    print(f" [系统自检] 硬件平台 : RTX 5080 (16GB) | 系统RAM: {total_ram}GB (可用 {avail_ram}GB)")
    print(f" [系统自检] 引擎版本 : {engine_ver}")
    print(f" [系统自检] 目标模型 : qwen3.8-flash-next-abliterated-iq3_s")
    print(f" [系统自检] 最大上下文: 131072 (128K 超长上下文)")
    print(f" [系统自检] 视觉组件 : {vision_status}")
    print(f" [服务端口] Web聊天界面: http://127.0.0.1:8083  |  API网关: http://127.0.0.1:8081/v1")
    print("=" * 68)
    print(" 请选择启动方案 [直接按 Enter 默认启动方案 A]:")
    print("   [1] 方案 A: 全能视觉甜点版 (推荐)")
    print("       - 128K长上下文 + 视觉多模态(识别图片/图表) + 32K KV驻留 + 异步换页")
    print("   [2] 方案 B: 纯文本极速冲刺版")
    print("       - 卸载视觉释放1.5G显存给专家缓存，纯文本/代码极速冲刺 (65-75+ tok/s)")
    print("-" * 68)

    # 3. Interactive Choice
    try:
        choice = input(" 请选择 [1/2] (默认 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消启动。")
        return

    if choice == "2" or choice.lower() == "b":
        chosen_scheme = "B"
        chosen_src = SCHEME_B_SRC
        chosen_title = "方案 B (纯文本极速冲刺版)"
    else:
        chosen_scheme = "A"
        chosen_src = SCHEME_A_SRC
        chosen_title = "方案 A (全能视觉甜点版)"

    print("-" * 68)
    print(f" >>> 已选择: 【{chosen_title}】")
    
    # 4. Prepare active configuration
    if not os.path.exists(chosen_src):
        print(f" [错误] 方案配置文件未找到: {chosen_src}")
        input("按任意键退出...")
        return
        
    # 引擎日志按天落地到统一目录：不再把所有运行都写进写死的 8083_engine_20261007.log
    import time as _time
    _log_dir = os.path.join(CUSTOM_DIR, "log")
    os.makedirs(_log_dir, exist_ok=True)
    with open(chosen_src, "r", encoding="utf-8-sig") as _f:
        _cfg = json.load(_f)
    _cfg["log"] = os.path.join(_log_dir, "8083_main_%s.log" % _time.strftime("%Y%m%d"))
    with open(ACTIVE_CONFIG_PATH, "w", encoding="utf-8") as _f:
        json.dump(_cfg, _f, ensure_ascii=False, indent=1)
    
    # Clean up any conflicting novision file in Strata-main so setup.py never asks "Which one?"
    if os.path.exists(NOVISION_CONFLICT_PATH):
        try:
            os.remove(NOVISION_CONFLICT_PATH)
        except Exception:
            pass

    # 5. Start tray / gateway if available
    try:
        import time
        import urllib.request
        
        trigger_path = os.path.join(CUSTOM_DIR, "log", "autostart.trigger")
        os.makedirs(os.path.dirname(trigger_path), exist_ok=True)
        with open(trigger_path, "w") as f:
            f.write("autostart\n")
        tray_script = os.path.join(CUSTOM_DIR, "stack_tray.py")
        if os.path.exists(tray_script):
            subprocess.Popen(["C:\\Python312\\pythonw.exe", tray_script, "--autostart"])
        
        # Ensure gateway :8081 is active for DSH and API callers
        # 铁律：8081 先到先得。TCP 端口被占（无论是本网关在启动中，还是 llama 栈的
        # qwen_tool_proxy 先占了）一律复用，绝不重复 spawn 抢端口。
        def _port_open(p):
            import socket
            s = socket.socket()
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", p))
                return True
            except Exception:
                return False
            finally:
                s.close()
        gw_up = False
        for _ in range(5):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8081/health", timeout=0.5) as r:
                    if r.status == 200:
                        gw_up = True
                        break
            except Exception:
                time.sleep(0.2)
        if not gw_up and _port_open(8081):
            print(" [服务复用] :8081 已被占用（llama 栈代理或网关启动中），后来者不抢占，直接复用")
            gw_up = True
        if not gw_up:
            gw_script = os.path.join(CUSTOM_DIR, "gateway", "gateway_8081.py")
            if os.path.exists(gw_script):
                subprocess.Popen(["C:\\Python312\\python.exe", gw_script], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for _ in range(10):
                    time.sleep(0.3)
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:8081/health", timeout=0.5) as r:
                            if r.status == 200:
                                gw_up = True
                                break
                    except Exception:
                        pass
        if gw_up:
            print(" [服务就绪] API 统一网关 :8081 已就绪 (DSH / API 入口在线)")
    except Exception:
        pass

    # 6. Launch the model via Strata's setup.py
    python_exe = os.path.join(MAIN_DIR, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python_exe):
        python_exe = "C:\\Python312\\python.exe"

    print(" >>> 正在加载并启动主脑引擎，首次装载 50GB 权重约需 30-45 秒...")
    print(" >>> [提示] 网关已具备智能握手缓冲机制：即使现在向 DSH 发送任务，网关也会自动排队等待，就绪后秒级直通！")
    print("=" * 68)
    
    # Pass directly into setup.py with --yes to prevent any extra questions
    cmd = [python_exe, "setup.py", "--port", "8083", "--yes"]
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\n[提示] 收到中断信号，服务停止。")

if __name__ == "__main__":
    main()
