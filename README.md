# Strata RTX 5080 Stack（Strata 本地推理定制栈 · 5080/64GB 专精调优）

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Strata](https://img.shields.io/badge/Engine-Strata%200.1.47-green.svg)](https://github.com/Niko1221/Strata)
[![CUDA](https://img.shields.io/badge/CUDA-13.0-76B900.svg)](https://developer.nvidia.com/cuda-toolkit)
[![OpenAI Compatible](https://img.shields.io/badge/API-OpenAI%20%2F%20Anthropic%20%2F%20Responses-orange.svg)](http://127.0.0.1:8081/v1)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

在 **RTX 5080 (16GB) + 64GB 内存 + Windows 11** 上跑 [Strata](https://github.com/Niko1221/Strata) 的本地大模型推理栈：
官方引擎原样不改，上面加一层自建网关与针对本机硬件的调优。

---

## 它是什么

```
① 官方层（不在本仓库）  H:\Strata-main    Strata 0.2.2 源码 + 官方引擎 0.1.47（签名 feed 取得）
② 定制层（本仓库）      H:\Strata-custom  启动器 / 统一网关 / MCP 网关 / 两个方案的运行配置 / 调优档案
③ 资产层（不在本仓库）  H:\models  H:\Strata-data   GGUF 权重、专家包、MTP、tokenizer
```

本仓库**只收这台机器专属的设置**，与机器无关的通用层和大文件资产全部解耦在外。

---

## 硬件前提（本仓库的调优对象）

| 部件 | 规格 |
| :--- | :--- |
| GPU | NVIDIA GeForce RTX 5080，16 GB（compute 12.0 / sm_120） |
| 驱动 / CUDA | 617.42 / CUDA 13.0（引擎最低要求 580.65） |
| 内存 | 64 GB（专家常驻约 46.84 GiB） |
| 磁盘 | H: 放模型与数据（宽裕）；C: 系统盘（紧张，只放缓存） |
| 模型 | `qwen3.8-flash-next-abliterated-iq3_s`（IQ3_S，2 分片） |

---

## 文件清单

| 文件 | 类型 | 说明 |
| :--- | :--- | :--- |
| **`machine-profile.json`** | 档案 | **单一事实源**：硬件、模型路径、全部调优参数、端口、已知取舍、调优流程 |
| **`configs/strata-scheme-A-vision.json`** | 配置 | 方案 A：128K 长上下文 + 视觉（CPU 读图）+ 32K KV 驻留 + 异步换页 |
| **`configs/strata-scheme-B-novision.json`** | 配置 | 方案 B：纯文本极速版，卸掉视觉把显存让给专家缓存 |
| **`launcher.py`** | 脚本 | 选方案 → 写运行配置 → 拉网关与托盘 → 启动引擎 |
| **`stack_tray.py`** | 脚本 | 系统托盘：状态、快捷开关、安全退出 |
| **`show_banner.py`** | 脚本 | 启动横幅渲染 |
| **`gateway/gateway_8081.py`** | 脚本 | **统一网关 8081**：OpenAI / Anthropic / Responses 三协议原生；空回复补刀续写、思考档位粘住、剪枝 v2、开工方式注入、`strata-fast/code/deep/nothink` 虚拟档位 |
| **`gateway/mcp_gateway_8087.py`** | 脚本 | MCP 网关 8087（工具/计费/配额） |
| **`gateway/personal_memory.py`** | 脚本 | 个人记忆（本地持久化） |
| **`gateway/*.example.json`** | 模板 | MCP / skills 服务器配置模板；本机覆盖层 `*.local.json` 不入库 |
| **`Strata-启动.bat`** 等 | 脚本 | 启动入口与方案切换、内存压缩开关 |
| **`AGENTS.md`** | 规则 | 给在这台机器上干活的 AI 的操作规则 |
| **`VERSION.txt`** | 文档 | 当前版本与目录分布 |

---

## 启动

双击 `Strata-启动.bat` → 选 `1`（方案 A）或 `2`（方案 B）。

命令窗口会打印实时日志（这就是 8083 主模型的运行输出）：

```
[strata] experts loaded: 46.84 GiB at 2.96 GiB/s (35 s so far)
[strata] filling the GPU's expert cache (3794 experts, 7.25 GiB of VRAM) ...
ready: http://127.0.0.1:8083/v1 (OpenAI: /v1/chat/completions, Anthropic: /v1/messages, context 131072 tokens, images on)
[strata] reading the prompt: 291 of 296 tokens, 1 s so far
[strata] done: 21 tokens in 2 s (39.6 tok/s) (stop, cancel=False), expert cache 63.3% hit
```

**关掉这个窗口 = 停掉模型。**

更详细的引擎内部日志在 `H:\Strata-custom\log\8083_main_YYYYMMDD.log`。

---

## 端口

| 端口 | 服务 |
| :--- | :--- |
| 8083 | Strata 模型服务（OpenAI + Anthropic + Responses 原生） |
| 8081 | 统一网关（带补刀 / 档位粘住 / 剪枝等增强），工具接这里 |
| 8087 | MCP 网关 |

---

## 调优参数与理由

| 参数 | 值 | 为什么 |
| :--- | :--- | :--- |
| `--max-context` | 131072 | 本机内存撑得住 128K；配合 int8 KV |
| `--kv` / `--kv-resident` | int8 / 32768 | KV 量化 + 32K 常驻显存，命中率与速度的平衡点 |
| `--expert-cache` / `--prefill` | auto / auto | 由引擎按实测显存与速度自选，跨版本更稳 |
| `--spec` / `--spec-min-p` | 2 / 0.5 | 投机解码开 2 档；再开收益变小、显存更紧 |
| `--adapt-async` | 1 | 异步换页，和 KV 驻留配合 |
| `--host-core` | last | 宿主线程放最后一个物理核，把前面的核让给专家池 |
| `--prompt-cache-spread` / `--tail` | 开 | 前缀缓存分散与尾部缓存，长会话命中率更稳 |
| `--expert-profile-save-every` | 1 | 把学到的专家路由画像存回 `profile-coding.bin`，换机/换卡可迁移 |
| `--vram-reserve-mib` | 1024 | 给显卡留 1G 给别的程序用（**刻意不再调高**，见下） |
| 采样 | temp 1.0 / top_p 0.95 / top_k 20 | 与官方默认一致 |

### 已知取舍：16GB 显存的边界

在 131072 上下文下，专家缓存会占到 7.25~7.53 GiB，**只剩约 0.55~0.62 GiB**，引擎会告警：

> only 615 MiB of VRAM free ... when the card is this full the driver can page GPU memory to system RAM,
> decode falls to a few tok/s and a window may never finish

这不是升级引入的（0.1.45 起就有），是 128K 上下文在这张卡上的物理边界。
想更稳就把 `--vram-reserve-mib` 提到 1536（专家缓存变小、命中率略降）；
本机目前**保持 1024 不动**，实测速度优先。

方案 B（去掉视觉）能把约 1.5G 显存还给专家缓存，实测纯文本可达 65-75+ tok/s。

---

## 更新上游 Strata（不影响本仓库）

```bat
cd /d H:\Strata-main
UPDATE.bat          :: 非 git 克隆会提示，忽略即可；它会从签名 feed 拉新引擎
```

上游换的是 `H:\Strata-main`（源码 + 引擎），**本仓库一行都不用动**。
换完引擎后本仓库的 `machine-profile.json` 里 `install.engine_version` 需要跟着更新。

---

## 不在本仓库的东西（解耦）

| 位置 | 内容 |
| :--- | :--- |
| `H:\Strata-main` | 上游 Strata 0.2.2 源码 + 官方引擎 0.1.47 |
| `H:\models` | GGUF 原始权重（134 GB） |
| `H:\Strata-data` | 准备好的专家包 `packs\`、MTP `mtp\rt`、tokenizer |
| `H:\Strata-custom\log` | 引擎日志 |
| `H:\Strata-custom\bin` | uv/uvx 二进制（第三方） |
| `gateway/mcp_servers.local.json` | 本机覆盖层（换网络时改 example 再拷成 local） |
| `gateway/miracle_api_keys.json`、`token_billing_stats.json` | 网关运行时状态 |
| `profile-coding.bin` | 引擎每次运行重写的专家画像 |

---

## 版本轨迹

| 日期 | 内容 |
| :--- | :--- |
| 2026-10-14 | 初始定制版 |
| 2026-10-22 | 上游升级到 Strata 0.2.2 / 引擎 0.1.47（签名 feed，ed25519 验签 + SHA-256 校验）；统一网关补刀/档位粘住/剪枝 v2/开工方式注入落地；官方桌面版试用后卸载；清理 5GB 未用 MTP 张量源 |

---

## License

MIT，见 [LICENSE](LICENSE)。上游 Strata 同样是 MIT。
模型权重的许可归各自作者（Qwen3.8-Flash-Next 系于 Qwen；abliterated 版本为社区微调）。
