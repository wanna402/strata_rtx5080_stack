# AGENTS.md — H:\Strata-custom 的操作规则

给在这台机器上干活的 AI（包括以后的我自己）。先读这里，再动手。

## 一、最重要的一条：不要替用户启动 Strata

**用户自己双击 `H:\Strata-custom\Strata-启动.bat` 启动模型栈。**

- 没有用户明确要求，**不要**拉起这个栈（不要跑 launcher.py、不要跑 setup.py 启动模型、不要开网关）。
- 如果为了做验证/实验**临时**拉起来了，**做完必须关掉**，并向用户说明已经关掉了：
  - 关掉 `H:\Strata-custom\Strata-启动.bat` 那个 cmd 窗口（= 停模型），或
  - 终止 `launcher.py` / `stack_tray.py` / `gateway_8081.py` / `mcp_gateway_8087.py` / `serve\server.py` / `engine\strata.exe` / `engine\strata-vision.exe`，
  - 确认 8081 / 8083 / 8087 三个端口都空了。
- 用户要的"命令窗口 + 8083 主模型实时日志"是**他们自己那个窗口**。不要用后台/最小化/隐藏的方式另开一个替身窗口。

## 二、这台机器的现状（2026-10-22 更新后）

| 东西 | 位置 | 说明 |
|---|---|---|
| 源码安装 | `H:\Strata-main` | Strata **0.2.2**（引擎 0.1.47，官方引擎，未改动） |
| 定制层 | `H:\Strata-custom` | launcher.py / stack_tray.py / gateway(8081,8087) / configs / log |
| 启动入口 | `H:\Strata-custom\Strata-启动.bat` | 方案 A=1 / 方案 B=2；**关窗口 = 停模型** |
| 模型权重 | `H:\models\...abliterated-IQ3_S-0000X-of-00002.gguf` | 脚本栈直接用这里的**原名**文件 |
| 专家包 / MTP | `H:\Strata-data\packs\abliterated-iq3_s`、`H:\Strata-data\mtp\rt` | `mtp\tensors`（5GB 原始张量）已删，引擎不需要 |
| 全局设置 | `H:\Strata-settings\settings.json` | `C:\Users\...\AppData\Roaming\Strata` 是指向它的 0 字节目录联接 |
| 官方桌面版 | **已卸载** | 试过，不适合这套定制栈 |

端口：8083 = Strata 服务 / 8081 = 统一网关（OpenAI+Anthropic+Responses 兼容，带补刀·档位粘住·剪枝）/ 8087 = MCP 网关。

## 三、别重复踩的坑

- **方案切换只有一条路**：`Strata-启动.bat` 启动时的 `1`（方案 A）/ `2`（方案 B）选择。
  不要新增"切换方案-*.bat"、也不要给 launcher 加 `--scheme` 之类的参数 —— 旧的切换脚本功能与启动器重复、
  写的配置会被启动器覆盖（等于无效），且是 GBK 内容配 `chcp 65001` 导致中文乱码，已于 2026-10-22 删除。

- **别改 `H:\models` 里的模型文件名**：引擎按同名主干找兄弟分片（`gguf_split_paths`），只改一个分片就起不来；脚本栈配置模板里写死了这两个路径。
- **别动 `H:\Strata-custom\configs\*.json` 里的路径**：launcher 每次启动都用模板重写 `H:\Strata-main\strata-abliterated-iq3_s.json`。
- **桌面版曾经用过的"官方名硬链接 + `.done` 标记"目录已删**（`H:\Strata-data\models\`）。要再让官方工具认这个模型，才需要重建（硬链接 0 额外占用）。
- **硬链接不是拷贝**：`fsutil hardlink list` 能看到同一份数据的多个名字；删一个名字不释放空间。统计磁盘占用别用简单求和。
- 引擎版本回滚（`setup.py --rollback-engine`）依赖 `engine\.previous`，**已按用户要求删除**，现在没有引擎级回滚了。
- 显存：16G 卡在 131072 上下文下只剩约 0.6G，引擎会告警（0.1.45 就有，非升级引入）；用户选择保持 `--vram-reserve-mib 1024` 不动。
- C 盘紧张（~33G 空闲），H 盘宽松（~199G 空闲）。清理优先动 C 盘的缓存，别删 H 盘用户资产。

## 四、报告口径

- 用了哪些端口、起了/停了哪些进程、磁盘增删多少，都要**明说**。
- 验证请求要给出真实 HTTP 状态码和耗时，别只说"正常"。
