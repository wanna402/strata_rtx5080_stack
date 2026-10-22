import json
import os
import sys

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

# 1. Dynamic Engine Version
engine_ver = "0.1.44.1"
build_json = r"H:\Strata-main\engine\BUILD.json"
if os.path.exists(build_json):
    try:
        with open(build_json, "r", encoding="utf-8") as f:
            data = json.load(f)
            engine_ver = data.get("version", engine_ver)
    except Exception:
        pass

# 2. Dynamic Model Config
model_name = "qwen3.8-flash-next-abliterated-iq3_s"
port = 8083
context = "131072"
has_vision = True

cfg_json = r"H:\Strata-main\strata-abliterated-iq3_s.json"
if os.path.exists(cfg_json):
    try:
        with open(cfg_json, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
            model_name = cfg.get("model_name", model_name)
            port = cfg.get("port", port)
            args = cfg.get("args", [])
            if "--max-context" in args:
                idx = args.index("--max-context")
                if idx + 1 < len(args):
                    context = str(args[idx + 1])
            has_vision = ("vision" in cfg) or ("--vision" in args)
    except Exception:
        pass

scheme_info = "方案 A (全能视觉版: 128K长上下文 + 视觉模态 + 32K KV驻留)" if has_vision else "方案 B (极速纯文本: 卸载视觉模态 释放1.5G显存 极速冲刺)"

print("===============================================")
print(" Strata 智能栈一键启动（定制层：H:\\Strata-custom）")
print(f" 引擎版本 : {engine_ver}")
print(f" 启动模型 : {model_name}")
print(f" 端口     : {port}")
print(f" 上下文   : {context}")
print(f" 当前方案 : {scheme_info}")
print(f" 网页聊天 : http://127.0.0.1:{port}")
print(" 前端统一入口: http://127.0.0.1:8081/v1")
print("===============================================")
