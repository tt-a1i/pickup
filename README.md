# pickup

**让新的 AI Agent 找到并读懂另一段本地会话。**

跨项目、跨工具查找会话，支持按原话、标题、话题词检索，以及概览、分轮阅读和全文阅读。适合切换 Agent、接续中断的工作，或回看另一段讨论。

Local session discovery and reading for AI agents. No session conversion, background service, index database, or third-party Python dependencies.

## 为什么做这个

在 Antigravity 里聊过的方案，想让 Codex 接着看；在 Kimi Code 里做了一半的任务，想换一个工具继续。记录已经在本机，新的 Agent 需要的是找到正确的会话，并按需要读取它。

pickup 将各工具的本地格式转换成可读文本。它不迁移原会话，也不替用户决定接下来要执行什么。

## 支持的工具

| 工具 | 默认本地记录位置 | 读取范围 |
| --- | --- | --- |
| Antigravity | `~/.gemini/antigravity/brain/` | 优先使用完整主会话 transcript、可用的后台任务日志 |
| Codex | `~/.codex/sessions/` | 主会话 JSONL |
| Claude Code | `~/.claude/projects/` | 项目中的主会话 JSONL |
| Qoder | `~/.qoder/projects/`、`~/.qoder-cn/projects/`、`~/.qoderwork/projects/` | 项目会话 JSONL |
| Pi | `~/.pi/agent/sessions/` | 会话 JSONL |
| Kimi Code | `~/.kimi-code/sessions/` | 主 Agent 的已提交消息，跳过重复的回放事件 |
| Devin CLI | `~/.local/share/devin/cli/sessions.db` | 沿当前消息分支还原对话，排除其他分支 |

这些适配器基于 macOS 上实际观察到的记录格式。相同路径和格式可能适用于其他系统，但尚未验证。客户端升级后，存储格式可能变化。云端专有会话、附件内容和子 Agent 历史不在完整性保证范围内。

## 安装

需要 **Python 3.10+**，运行时仅使用标准库。

```bash
git clone https://github.com/tt-a1i/pickup.git
cd pickup
python3 scripts/pickup.py --help
```

可选：在 shell 配置中设置别名，路径改为实际克隆位置：

```bash
alias pickup='python3 /absolute/path/to/pickup/scripts/pickup.py'
```

### 作为 Agent Skill 安装

将本仓库放进对应 Agent 的技能目录，或从该目录建立指向本仓库的符号链接。例如，在已克隆的仓库目录中执行：

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD" ~/.codex/skills/pickup
```

如果目标位置已有同名 skill，先检查已有版本，再决定替换。其他客户端按各自的技能安装方式加载根目录的 `SKILL.md`。新会话可能需要重新加载技能列表。

## 使用

以下示例在仓库根目录执行；设置别名后可以把 `python3 scripts/pickup.py` 替换为 `pickup`。

### 1. 查找候选

```bash
# 不限制当前项目目录，列出最近 6 段会话
python3 scripts/pickup.py

# 按原话、标题或话题词查找
python3 scripts/pickup.py "某个有辨识度的原话片段"

# 已知来源时缩小范围
python3 scripts/pickup.py --agent kimi
python3 scripts/pickup.py --agent devin "登录问题"

# 自定义数量，或手动排除某个会话
python3 scripts/pickup.py --limit 10
python3 scripts/pickup.py --exclude '<完整会话ID或候选UID>'
```

**查找只返回候选，不自动读取第一名。** 无线索时按记录文件的修改时间排序；有线索时，对解析后的标题、对话和保留的工具记录做文字匹配并排序，再考虑时间。排序不代表已经确定用户意图，也不是语义搜索。

当运行环境提供受支持的当前会话 ID 时，脚本自动排除它；缺少该信息时，可用 `--exclude`。脚本不会根据一句“pickup”就判定某条记录是当前会话。

### 2. 选定后读取概览

把输出中的 UID 原样填入命令：

```bash
python3 scripts/pickup.py show '<uid>'
python3 scripts/pickup.py show '<uid>' --tail 2
```

概览包含全部用户提问的轮次目录，以及最近几轮对话、工具调用摘要和可用的错误信息。目录和尾部文字会缩短，完整内容通过 `read` 读取。

### 3. 深入阅读

```bash
# 从头阅读，超过默认 60,000 字符时给出后续轮次范围
python3 scripts/pickup.py read '<uid>' --all

# 阅读指定轮次
python3 scripts/pickup.py read '<uid>' --turns 8-20

# 搜索对话与保留的工具记录
python3 scripts/pickup.py read '<uid>' --grep 'build'

# 附带工具输出片段
python3 scripts/pickup.py read '<uid>' --turns 8-20 --with-tools
```

`--all` 保留用户和助手对话，省略庞大的原始工具输出；工具轨迹使用简短摘要。分页在轮次之间切分，因此单独一轮很长时仍可能超过字符预算。按输出提示继续读取剩余轮次；如果调用工具自身截断了输出，改用更小的轮次范围。

### 在聊天里用

安装 skill 后，可以直接说：

- `pickup`：先列候选，选择后再加载。
- `pickup，读一下之前讨论过的登录问题`。
- 粘贴另一段会话的原话或截图，补一句 `pickup，了解这里的上下文`。
- `pickup，找到那段会话后接着完成任务`。

截图由接手的 Agent 提取文字线索，脚本本身不做 OCR。读取历史以后，是总结、回答问题还是继续实现，由当前用户指令决定。继续代码工作时，还需要核对磁盘上的实际文件和 Git 状态。

## 范围与限制

- 读取本机原始记录，SQLite 以只读模式打开；不上传历史，不调用模型，不启动常驻进程，也不创建索引数据库。
- Agent 将读出的历史送入模型时，仍受该 Agent 的数据处理方式约束。命令输出也可能包含会话中的私密信息，分享前请检查。
- 全历史文本搜索没有此前的最近记录数量限制，因此历史很多时会较慢；使用更具体的原话或 `--agent` 可以缩小查找范围。
- 原始记录缺失的内容无法还原。长工具调用参数及输出片段有截断，检索结果不等同于完整审计日志。
- 不支持 Devin 网页端的云端会话 API，不尝试获取账号凭据。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖候选列表行为、当前会话排除、旧记录搜索、ID 歧义、秒/毫秒时间戳、转义中文搜索、无关字段过滤、Antigravity 完整记录、关键词摘录、Pi 工具输出、Kimi 回放去重和 Devin 分支选择。测试使用临时样本，不包含个人会话数据。

```text
SKILL.md                  Agent 的使用说明
scripts/pickup.py         检索、概览、阅读与原有格式适配
scripts/pickup_extra.py   Kimi Code / Devin CLI 读取器
tests/                    回归测试
```
