# LyX MCP Server

[English README](README.md)

LyX MCP Server 让 AI 助手编辑 LyX 论文，保留修订记录，并支持文本、部分文档结构的修改和 PDF 导出。它帮助你审阅每处修改，并在持续修订时保证论文可正常导出 PDF。

## 安装与启动

启动前必须具备：Linux（含 `/proc` 与 `prctl`，当前仅支持 Qt offscreen 后端）、本机 LyX 2.4.x、Python 3.11+ 及 `venv`/`pip`、可用的 LaTeX 工具链。显示修订的 PDF 需要 `pdflatex`、`xcolor.sty` 和 `ulem.sty`；使用参考文献的论文还需要对应的 `bibtex` 等文献工具，使用 SVG 的论文可能需要 Inkscape 等转换器。`runtime_root` 必须可写且支持 FIFO；论文及持久导出路径必须在可写的 `allowed_roots` 中，图像和文献库文件也必须可读。

先检查环境：

```bash
python3 --version
lyx -version
command -v pdflatex
command -v bibtex
kpsewhich xcolor.sty
kpsewhich ulem.sty
```

配置文件中的 `binary` 应为 LyX 的绝对路径，`allowed_roots` 应替换为真实论文目录的绝对路径。默认配置只允许访问启动目录下的 `.lyx` 文件；示例配置中的占位路径不能直接使用。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml
# 修改 config.toml 中的 allowed_roots 为论文目录的绝对路径
```

使用 Codex 时，再安装仓库附带的 [LyX 论文编辑 skill](skill/lyx-paper-editing/SKILL.md)，让它按论文编辑流程调用 MCP：

```bash
mkdir -p ~/.codex/skills
cp -a skill/lyx-paper-editing ~/.codex/skills/
```

如果自定义了 Codex 的 skills 目录，请复制到对应位置；其他 MCP 客户端可以不安装 skill。

在 MCP 客户端中，将 command 设为 `.venv/bin/lyx-mcp` 的绝对路径，并设置环境变量 `LYX_MCP_CONFIG` 指向配置文件的绝对路径。客户端会按需启动 stdio server，无需另开一个常驻 server。server 只通过 stdout 传输 MCP 协议；LyX 的 stdout/stderr 写入独立 runtime 的日志。

需要手动启动时，可运行 `LYX_MCP_CONFIG="$PWD/config.toml" .venv/bin/lyx-mcp`；进程会等待 MCP 客户端通过 stdin 发送请求。

## 工具

| 工具 | 用途 |
| --- | --- |
| `lyx_get_document_state` | 查询路径、SHA256、Track Changes 和 autosave 状态 |
| `lyx_read` | 由 LyX 导出纯文本或 LaTeX |
| `lyx_apply_edits` | 批量 replace、delete、insert_before、insert_after；默认编译 PDF |
| `lyx_replace_text`、`lyx_delete_text`、`lyx_insert_text` | 单项编辑的便捷接口 |
| `lyx_export` | 导出 text、latex 或 pdf2；可复制到允许目录中的新文件 |
| `lyx_validate_revision` | 检查 Track Changes 并编译当前文档或 master |
| `lyx_import_revision_range` | 将另一份 `.lyx` 中已追踪的连续 Section 区间导入目标，并编译验证 |
| `lyx_normalize_revisions` | 合并指定作者及时间戳的碎片化文字修订，接受不安全的 `resizebox` ERT 替换，并编译验证 |

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

默认要求每个目标在 LyX 导出的纯文本中精确且唯一，并能映射到 LyX 源文件中连续的普通文字。多个目标可用 `context_before`、`context_after` 消歧，再用 `occurrence` 指定匹配项。替换操作自动收缩相同的前后文字，同时保持整词边界；句子整体重写仍作为一个连续修订。跨公式或其他 inset 的目标会在修改前被拒绝，避免 LyX 搜索超时；带公式、引用和结构的既有修订可用 `lyx_import_revision_range` 导入。插入文本中的 `\n` 表示段落分隔，且只支持在段落边界处插入。

导入工具需要源、目标各自的 SHA256，以及相同且唯一的起止 Section 标题。它复制起始 Section 到结束 Section 之前的 LyX 标记，补齐修订作者，保留目标区间外的内容，并在独立 LyX 缓冲区重新打开、保存和编译。源区间必须已有 Track Changes 标记，目标区间不能已有修订标记。该工具会替换目标区间内的内容，不做三方合并；使用前应检查两份文档在此区间的差异。

所有成功的编辑与导入均会设置 `\tracking_changes true` 和 `\output_changes true`。编译不仅检查 TeX 致命错误，也检查最终日志中的未定义 citation。`lyx_normalize_revisions` 适用于已导入的逐词修订：它把短间隔内同一作者和时间戳的修订合成可读的短语或句子，同时保留接受和拒绝修订后的正文。无法安全显示的 `resizebox` ERT 开口替换会被接受为结构性格式变动；返回值明确报告数量。随附的 [LyX 论文编辑 skill](skill/lyx-paper-editing/SKILL.md) 说明选择修订范围的方法及当前测试覆盖的 LyX 语法。

`checker_profile` 只能引用配置中的固定命令。`master_path` 可用于编辑 child 文档后编译 master；目前尚未用真实多文件论文完成集成验证。`lyx_export` 的默认产物位于临时 runtime，server 退出后会删除；需要保留时传入 `output_path`，目标必须在 `allowed_roots` 中且尚不存在。

## 隔离与冲突边界

每个 server 启动时创建权限为 `0700` 的 runtime 和 userdir，设置独立的 `\serverpipe "$$UserDir/run/lyxserver"`，并以 `lyx --no-remote -userdir ...` 启动 `QT_QPA_PLATFORM=offscreen` 进程。server 内只维持一个 LyXServer client ID，所有工具共享一把异步锁。正常退出时先请求 `lyx-quit`，再对自身独立进程组发送 SIGTERM；若三秒后仍存活则发送 SIGKILL。Linux 启动器还会在 MCP 父进程意外死亡时自动杀死 LyX 子进程，防止遗留孤儿进程。旧版仅在正常关闭路径清理，因此 MCP 进程异常退出可能留下 LyX。上述信号不会发给其他桌面 LyX 实例。

桌面 LyX 可以同时运行。两个实例若同时编辑**同一文件**，桌面 LyX 中尚未保存、尚未 autosave 的内容无法被此 server 检测。请先保存并关闭该文件的桌面编辑会话。server 会拒绝更新的 `#file.lyx#` autosave、已知磁盘 SHA 变化以及事务内的外部改动。

默认 profile 仅为本机 LyX 2.4.x 验证。其他版本需显式设置独立的 `profile_seed_dir`，并重新运行真实 LyX 集成测试。当前版本只实现 offscreen backend；若系统需要 Xvfb，应先补上并验证该启动路径。

## 测试

```bash
PYTHONPATH=src .venv/bin/mypy src
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/pytest -q
```

默认测试会启动两个独立的本机 LyX 实例，使用临时 `.lyx` 副本验证 Track Changes、PDF、文本编辑与失败回滚。stdio MCP 端到端测试需允许本地 IPC：

```bash
LYX_MCP_RUN_STDIO_TEST=1 PYTHONPATH=src .venv/bin/pytest -q tests/test_stdio.py tests/test_lyx_syntax.py
```
