<div align="center">

<img src="assets/cover.jpg" alt="pickup — Find & read any local AI agent session" width="100%" />

# pickup

**让新的 AI Agent 秒速找到并读懂另一段本地会话。**

*跨项目、跨工具的本地会话发现与分级阅读工具 — 无需转换格式，无需后台常驻，零第三方依赖。*

<p>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10%2B-2563EB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/Dependencies-Zero%20(stdlib)-10B981?style=flat-square" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/Agents-7%20Supported-0EA5E9?style=flat-square" alt="7 Agents Supported">
  <img src="https://img.shields.io/badge/Mode-Local%20Read--Only-F59E0B?style=flat-square" alt="Local Read-Only">
  <img src="https://img.shields.io/badge/Tests-25%20Passing-16A34A?style=flat-square" alt="25 Tests Passing">
  <a href="https://github.com/tt-a1i/pickup/commits/main"><img src="https://img.shields.io/github/last-commit/tt-a1i/pickup?style=flat-square&color=64748B" alt="Last Commit"></a>
</p>

<p>
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

</div>

---

## 为什么做 `pickup`？

在 **Antigravity** 里聊了一半的方案，想让 **Codex** 接着改代码；在 **Kimi Code** 或 **Qoder** 里跑崩的任务，想换个工具接手——记录其实一直安静躺在本机磁盘上，新开窗口的 Agent 缺的只是一双能看懂各家格式的眼睛。

| 传统做法的痛点 | `pickup` 的解法 |
| :--- | :--- |
| 只能同目录或手动翻找散落的文件与 SQLite | **全机跨目录秒查**：混排 7 款本地 Agent，支持按原话、标题、话题词精确检索 |
| 动辄上千步的长会话直接喂给模型导致卡死或跑偏 | **分级阅读设计**：先给全篇用户提问大纲 + 尾部断点，再按 `--turns` / `--grep` 按需深读 |
| 需要常驻守护进程、2GB 索引副本或高耗 MCP | **零后台、零索引、只读**：仅用 Python 标准库直读原始记录，不修改源文件、不上传数据 |
| 接手后 Agent 擅自行动 | **克制边界**：只负责精准装载上下文，下一步改代码还是总结全听你当前指令 |

---

## 支持的工具

| 工具 | 默认本地记录位置 | 读取范围与细节 |
| :--- | :--- | :--- |
| **Antigravity** | `~/.gemini/antigravity/brain/` | 优先完整主会话 transcript 与近期后台任务日志 |
| **Codex** | `~/.codex/sessions/` | 主会话 JSONL（自动过滤注入的系统提示块） |
| **Claude Code** | `~/.claude/projects/` | 项目主会话 JSONL（兼容文本块数组形式的工具结果） |
| **Qoder** | `~/.qoder/projects/`、`~/.qoder-cn/projects/`、`~/.qoderwork/projects/` | 项目会话 JSONL（支持国际版、国内版与 QoderWork） |
| **Pi** | `~/.pi/agent/sessions/` | 按最后持久化节点的父链还原当前分支，保留该路径上的早期对话 |
| **Kimi Code** | `~/.kimi-code/sessions/` | 主 Agent 已提交消息，自动跳过重复回放事件 |
| **Devin CLI** | `~/.local/share/devin/cli/sessions.db` | 只读打开 SQLite，沿当前活跃消息分支还原 |

> 适配器基于 macOS 上实际观察到的存储格式实现。同一存储结构在其他类 Unix 系统上通常同样适用，但尚未全面实测；客户端升级后存储格式可能发生变化。云端专有会话、二进制附件与子 Agent 独立历史不在完整性保证范围内。

---

## 快速安装

需要 **Python 3.10+**，运行时零第三方依赖。

### 1. 克隆仓库并验证 CLI

```bash
git clone https://github.com/tt-a1i/pickup.git
cd pickup
python3 scripts/pickup.py --help
```

可选：在 `~/.zshrc` 或 `~/.bashrc` 中添加别名，随时随地直接敲 `pickup`：

```bash
alias pickup='python3 /absolute/path/to/pickup/scripts/pickup.py'
```

### 2. 作为 Agent Skill 安装（推荐）

将本仓库软链到对应 Agent 的技能目录，对话中即可直接用 `/pickup` 或自然语言触发：

```bash
# 例如安装到 Codex（Claude Code / Pi / OpenCode 等同理）
mkdir -p ~/.codex/skills
ln -s "$PWD" ~/.codex/skills/pickup
```

> 新开会话可能需要刷新技能列表；如果目标路径已有同名 skill，请先确认后再替换。

---

## 使用方式

以下示例在仓库根目录执行；配置别名后可将 `python3 scripts/pickup.py` 简写为 `pickup`。

### 第一步：查找候选（`find`）

```bash
# 不限项目目录，列出全机最近活跃的 6 段会话
python3 scripts/pickup.py

# 用一句原话、会话标题或话题词精准定位
python3 scripts/pickup.py "某个有辨识度的原话片段"

# 已知来源工具时缩小扫描范围
python3 scripts/pickup.py --agent kimi
python3 scripts/pickup.py --agent devin "登录问题"

# 自定义返回数量，或显式排除当前会话
python3 scripts/pickup.py --limit 10
python3 scripts/pickup.py --exclude '<完整会话ID或候选UID>'
```

- **只列候选，不盲目加载**：无线索时按文件修改时间排序；有线索时对解析后的标题、摘要、对话与保留的工具记录做文字匹配排序。
- 若运行环境提供受支持的当前会话 ID，脚本会自动排除自身；缺少时可用 `--exclude`。

### 第二步：查看概览与大纲（`show`）

选中候选列表里的 UID，先看「全篇脉络 + 尾部断点」：

```bash
python3 scripts/pickup.py show '<uid>'
python3 scripts/pickup.py show '<uid>' --tail 2
```

输出包含 **全部轮次的用户提问大纲（`Turn 1..N`）**、最近几轮完整对话、紧凑的工具调用轨迹与可用的错误/后台任务日志。

### 第三步：按需深读或全文通读（`read`）

```bash
# 从第 1 轮开始通读对话（剥离庞大原始 stdout，超 60,000 字符自动按轮次分页）
python3 scripts/pickup.py read '<uid>' --all

# 只展开大纲里关心的某几轮
python3 scripts/pickup.py read '<uid>' --turns 8-20

# 在该会话的对话与保留的工具输出中定向搜索关键词
python3 scripts/pickup.py read '<uid>' --grep 'build'

# 阅读指定轮次并附带原始工具输出片段
python3 scripts/pickup.py read '<uid>' --turns 8-20 --with-tools
```

### 在聊天窗口直接调用

装好 Skill 后，切到任意新窗口直接说：

- **`/pickup`** — 先列出全机最近会话供你点选。
- **`/pickup 帮我看下之前聊的登录超时方案`** — 按话题跨工具搜出对应会话并汇总给你。
- **贴一段原话或一张旧窗口截图 + `接着干`** — 接手的 Agent 自动从文字或图片提取线索定位原会话，核对磁盘上的真实代码与 Git 状态后继续完成任务。

---

## 范围与限制

- **本地只读**：SQLite 以 `?mode=ro` 只读打开，不启动常驻进程，不建本地副本库，不向外部发任何遥测或会话请求（Agent 将读出的文本送入模型时受该 Agent 自身隐私策略约束）。
- **搜索耗时**：带关键词的全历史检索会扫描更多本地文件，历史较多时可加 `--agent` 或更具体的原话提速。
- **Pi 分支还原**：按最后持久化节点的父链恢复当前分支；仅在内存中切换、尚未持久化后续节点的分支无法从磁盘还原。压缩摘要与仅摘要会话均会被保留并支持检索。
- **Devin CLI**：仅读取本地数据库，不支持调用 Devin 网页端云端 API 或获取账号凭据。

---

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
```

当前 **25 项回归测试**全部使用临时隔离样本运行（不含任何个人会话数据），覆盖候选排序、当前会话排除、ID 消歧、Pi 唯一 ID 与分支还原、仅摘要会话、Claude/Qoder 数组工具结果、转义中文检索、Antigravity 完整记录、Kimi 回放去重与 Devin 分支选择。

```text
pickup/
├── SKILL.md                  # Agent 技能指引
├── assets/cover.jpg          # README 封面图
├── scripts/
│   ├── pickup.py             # 核心 CLI：发现、概览、分页阅读与基础适配器
│   ├── pickup_extra.py       # Kimi Code / Devin CLI 适配器
│   └── pickup_pi.py          # Pi 持久化分支解析器
└── tests/                    # 25 项离线回归测试
```
