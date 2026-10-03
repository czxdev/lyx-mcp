# LyX MCP Server

[English README](README.md)

LyX MCP Server 让 AI 助手编辑 LyX 论文，保留修订记录，并支持文本、部分文档结构的修改和 PDF 导出。它帮助你审阅每处修改，并在持续修订时保证论文可正常导出 PDF。

## 前置条件

- **Linux**，需提供 `/proc` 和 `prctl(PR_SET_PDEATHSIG)`。进程清理与集成测试依赖 Linux 功能。安装的 LyX/Qt 必须提供 offscreen 平台插件；当前 server 仅支持该后端。
- **本机安装 LyX 2.4.x**。将 `[lyx].binary` 设置为可执行文件的绝对路径。其他 LyX 版本需要独立的 `profile_seed_dir`，并单独完成集成测试。
- **Python 3.11 或更新版本**，以及用于安装 MCP server 和依赖的 `venv` 与 `pip`。
- **可用的 LaTeX 工具链**，用于 `pdf2` 导出，包括 `pdflatex`、论文使用的 `bibtex` 等文献工具，以及论文所需的 TeX 宏包。显示修订需要 `xcolor.sty` 和 `ulem.sty`。使用 SVG 图像的文档可能还需要 Inkscape 等 SVG 转换器。
- **可写路径：** `[lyx].runtime_root` 必须可写，且允许创建 FIFO 和启动子进程。每篇论文及持久导出目标必须位于配置的 `[security].allowed_roots` 目录内。运行 server 的用户需要文献库和图像资源的读取权限，以及论文目录的写入权限。还应确保 `PWD` 环境变量指向可写路径（应与 `[lyx].runtime_root` 相同）。

配置 MCP 客户端前，先检查基础环境：

```bash
python3 --version
lyx -version
command -v pdflatex
command -v bibtex
kpsewhich xcolor.sty
kpsewhich ulem.sty
```

如果某条命令没有输出路径，说明依赖该功能的文档所需的组件尚未安装。使用 SVG 的论文还应检查 `command -v inkscape` 或配置的转换器。

## 安装与连接

在本仓库目录中运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml
```

使用 Codex 时，再安装仓库附带的 [LyX 论文编辑 skill](skill/lyx-paper-editing/SKILL.md)，让它按论文编辑流程调用 MCP：

```bash
mkdir -p ~/.codex/skills
cp -a skill/lyx-paper-editing ~/.codex/skills/
```

如果自定义了 Codex 的 skills 目录，请复制到对应位置；其他 MCP 客户端可以不安装 skill。

修改 `config.toml`：将 `binary` 设为 LyX 的绝对路径，`runtime_root` 设为可写目录（默认为 `/tmp`），`allowed_roots` 设为包含论文和持久导出文件的**目录绝对路径**。必须替换占位路径 `/absolute/path/to/papers`。这些目录之外的论文会被拒绝访问。`config.toml` 不纳入 Git 版本管理。

配置 MCP 客户端通过 **stdio** 启动 server。将 `command` 设为本仓库 `.venv/bin/lyx-mcp` 的绝对路径，并在环境中将 `LYX_MCP_CONFIG` 设为 `config.toml` 的绝对路径。客户端会按需启动该命令，无需单独运行常驻 server。手动启动时可运行以下命令；进程会等待 stdin 上的 MCP 请求，可按 Ctrl+C 停止：

```bash
LYX_MCP_CONFIG="$PWD/config.toml" .venv/bin/lyx-mcp
```

server 只通过 stdout 传输 MCP 协议。LyX 的 stdout 和 stderr 写入独立 runtime 目录中的文件。

## 工具

| 工具 | 用途 |
| --- | --- |
| `lyx_get_document_state` | 查询路径、SHA256、Track Changes 和 autosave 状态 |
| `lyx_read` | 由 LyX 导出接受修订后的纯文本或 LaTeX |
| `lyx_apply_edits` | 批量执行 `replace`、`delete`、`insert_before` 和 `insert_after`；默认编译 |
| `lyx_replace_text`、`lyx_delete_text`、`lyx_insert_text` | 单项编辑的便捷接口 |
| `lyx_insert_citation` | 在普通正文锚点前后插入引用；与已有引用相邻时允许 LyX 合并，默认编译 PDF |
| `lyx_export` | 导出 text、latex 或 pdf2；可复制到允许目录中的新文件 |
| `lyx_validate_revision` | 检查 Track Changes 并编译当前文档或 master |
| `lyx_import_revision_range` | 将另一份 `.lyx` 中已追踪的连续 Section 区间导入目标，并编译验证 |
| `lyx_normalize_revisions` | 合并指定作者及时间戳的碎片化修订，处理不安全的 `resizebox` ERT 替换，并编译验证 |

批量编辑示例：

```json
{
  "path": "/absolute/path/to/paper.lyx",
  "edits": [
    {
      "op": "replace",
      "old_text": "The method achieve better results.",
      "new_text": "The method achieves better results."
    },
    {
      "op": "insert_after",
      "anchor": "This is the final paragraph.",
      "text": "\nA new paragraph."
    }
  ],
  "compile": true
}
```

通常要求每个目标在 LyX 导出的接受修订后纯文本中精确且唯一，并能映射到 `.lyx` 源文件中连续的普通文字。重复文本可用 `context_before` 和 `context_after` 消歧，再用 `occurrence` 指定匹配项。替换操作自动收缩相同的前后文字，同时保留合理的整词或数值范围边界。句子整体重写仍作为一个连续修订。跨公式或其他 inset 的目标会在 LyX 搜索前被拒绝。按文献 key 新增引用时使用 `lyx_insert_citation`；导入已审阅的结构性修订（包括已有的引用 inset）时使用 `lyx_import_revision_range`。插入文本中的 `\n` 表示段落分隔，且只支持在段落边界处插入。

`lyx_insert_citation` 接收 `path`、`anchor` 和 `keys`，默认在锚点后插入；多个 key 用逗号分隔，`position="before"` 可改为锚点前。独立插入的引用 inset 带修订记录；LyX 将 key 合并到紧邻的已有引用时也视为成功，并返回 `merged_into_existing=true`。默认编译 PDF，文献库中不存在的 key 会导致回滚；需要空格时另作普通文本编辑。

导入工具需要源、目标各自的 SHA256，以及相同且唯一的起止 Section 标题。它复制起始 Section 到结束 Section 之前的 LyX 标记，补齐修订作者，然后在独立 LyX 中重新打开、保存和编译结果。源区间必须已有 Track Changes 标记，目标区间不能已有修订标记。整个目标区间会被替换，不做三方合并，因此使用前应比对完整区间。

所有成功的编辑与导入均会设置 `\tracking_changes true` 和 `\output_changes true`。编译会检查 TeX 致命错误和最终日志中的未定义引用。`lyx_normalize_revisions` 将同一作者和时间戳的相邻修订合成可读的短语或句子，同时保留接受和拒绝修订后的正文。它会将不安全的 `resizebox` ERT 开口替换接受为结构性格式变动，并报告数量。随附的 [LyX 论文编辑 skill](skill/lyx-paper-editing/SKILL.md) 说明选择修订范围的方法及已测试的 LyX 语法。在普通 LyX 正文和图表标题中，数值范围应使用 `10-50`；`10--50` 是原始 TeX 输入语法，应避免复制到 LyX 正文中。

`checker_profile` 只能引用配置中的固定命令。`master_path` 可用于编辑 child 文档后编译 master；目前尚未用真实多文件论文完成集成验证。`lyx_export` 的默认产物位于临时 runtime，server 退出后会删除；需要保留时传入 `output_path`，目标必须在 `allowed_roots` 中且尚不存在。

## 进程隔离与清理

每个 server 启动时创建权限为 `0700` 的私有 runtime 和 userdir，设置独立的 `\serverpipe "$$UserDir/run/lyxserver"`，并以 `lyx --no-remote -userdir ...` 和 `QT_QPA_PLATFORM=offscreen` 启动 LyX。所有工具共享一个 LyXServer client ID 和一把异步锁。桌面 LyX 实例不会被复用或发送信号。

正常退出时，server 先请求 LyX 退出，再向**自身**进程组发送 SIGTERM；如果三秒宽限期后该进程组仍存活，则发送 SIGKILL。在 Linux 上，LyX 启动器还会在执行 LyX 前设置父进程死亡信号，确保 MCP server 被突然终止后，其 LyX 子进程也随之退出。旧版仅尝试在正常关闭路径中清理，如果 MCP 进程先退出，可能遗留孤儿进程。因此，占用大量 CPU 且忽略 SIGTERM 的 LyX 进程会触发 SIGKILL 后备清理；诊断底层忙循环原因需要该进程的日志或堆栈跟踪。server 始终只向自身进程组发送信号。

两个实例可以同时编辑不同文件。如果桌面实例编辑**同一文件**，其中尚未保存、尚未 autosave 的改动无法被此 server 检测；应先保存并关闭该桌面缓冲区。server 会拒绝更新的 `#file.lyx#` autosave、已知磁盘 SHA 变化以及事务内的外部改动。

默认 profile 仅为本机 LyX 2.4.x 验证。其他版本需显式设置独立的 `profile_seed_dir`，并重新运行真实 LyX 集成测试。当前版本只实现 offscreen backend；若系统需要 Xvfb，应先补上并验证该启动路径。

## 测试

```bash
PYTHONPATH=src .venv/bin/mypy src
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/pytest -q
```

默认测试会启动独立的本机 LyX 进程，使用临时 `.lyx` 副本验证修订、PDF 导出、文本编辑、回滚和进程清理。stdio MCP 和 LyX 语法测试需要本地 IPC 访问权限：

```bash
LYX_MCP_RUN_STDIO_TEST=1 PYTHONPATH=src .venv/bin/pytest -q tests/test_stdio.py tests/test_lyx_syntax.py
```
