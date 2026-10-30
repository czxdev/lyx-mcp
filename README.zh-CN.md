# LyX MCP Server

[English README](README.md)

LyX MCP Server 让 AI 助手编辑 LyX 论文，保留修订记录，并支持文本、部分文档结构的修改和 PDF 导出。它帮助你审阅每处修改，并在持续修订时保证论文可正常导出 PDF。

## 前置条件

- **Linux**，需提供 `/proc` 和 `prctl(PR_SET_PDEATHSIG)`。进程清理与集成测试依赖 Linux 功能。安装的 LyX/Qt 必须提供 offscreen 平台插件；当前 server 仅支持该后端。
- **本机安装 LyX 2.4.x**。将 `[lyx].binary` 设置为可执行文件的绝对路径。其他 LyX 版本需要独立的 `profile_seed_dir`，并单独完成集成测试。
- **Python 3.11 或更新版本**，以及用于安装 MCP server 和依赖的 `venv` 与 `pip`。
- **可用的 LaTeX 工具链**，用于 `pdf2` 导出，包括 `pdflatex`、论文使用的 `bibtex` 等文献工具，以及论文所需的 TeX 宏包。显示修订需要 `xcolor.sty` 和 `ulem.sty`。使用 SVG 图像的文档可能还需要 Inkscape 等 SVG 转换器。
- **可写路径：** `[lyx].runtime_root` 必须可写，且允许创建 FIFO 和启动子进程。每篇论文及持久导出目标必须位于配置的 `[security].allowed_roots` 目录内。运行 server 的用户需要文献库和图像资源的读取权限，以及论文目录的写入权限。还应确保 `PWD` 环境变量指向存在且可写的路径。

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

MCP 接口已合并为四个工具：

| 工具 | 用途 |
| --- | --- |
| `lyx_read` | 一次读取版本状态、目录、指定章节/段落、搜索结果和 inset 信息；按需读取构建产物或 PDF 批注 |
| `lyx_apply_edits` | 带版本前置条件的混合编辑事务：统一预检、保护指定范围、验证、编译和最终 PDF 交付 |
| `lyx_compare` | 两方或三方文本比较、小范围修改片段、上下文和冲突；支持选择性重新应用 |
| `lyx_job` | 查询状态、有限等待、取消；终态说明是否提交、回滚及能否重试 |

先通过 `lyx_read` 获取需要的章节与 `state.sha256`，再将它作为 `expected_sha256` 提交。节点 ID 绑定文档版本；一个批次中的全部操作针对同一基线解析，并使用调用方提供的唯一 `id` 返回结果。默认读取直接解析已保存源文件，不启动 LyX，也不会将隐藏 Note 中的相同文字误作正文锚点。用 `selector`、`search`、`representations` 和返回长度限制减少无关内容。

混合批次支持正文、文献引用、段落拆分/合并、段落样式、段落移动、公式、交叉引用、表格单元格、标题、已审阅 Section 修订导入和修订归并。跨 inset 替换必须在新文本中保留对应 token，并在 `preserve_insets` 中列出相同顺序的 ID。`protect` 与 `assertions` 可保护已完成章节和指定 inset。结构修改会明确报告 LyX 是否能将其显示为修订。

事务在临时副本上统一修改，由隔离 LyX 保存一次、验证一次、编译一次；全部通过后才原子替换目标文件。`output_path` 可以直接保存最终带修订 PDF。失败或取消后先查看终态，再决定是否重新读取并重试。

编译返回可复用的 `build_id`。文档与依赖指纹不变时，验证和导出复用同一构建。使用 `lyx_read(build_id=...,artifact="latex"/"log"/"aux"/"bbl"/"pdf"/"manifest")` 按需读取结果；manifest 包含全部文件、诊断和文献/交叉引用映射。普通写响应不会返回全文或完整日志。超时涵盖排队与 IPC，长操作通过 job ID 查询和取消。

旧的 Python service 方法仍保留兼容性，旧 MCP 便捷工具名称已合并。升级后应重启 MCP 连接并同步[编辑 skill](skill/lyx-paper-editing/SKILL.md)。完整参数与边界见 [README](README.md#agent-interface) 和[接口设计](docs/interface.md)。当前原生 LaTeX 导出仍针对全文；结构/inset 比较冲突需要明确操作和人工审阅。PDF 批注提取与渲染是可选功能，需要安装 `pdf` extra。

## 进程隔离与清理

每个 server 启动时，在权限为 `0700` 的私有目录池 `<runtime_root>/lyx-mcp-<uid>/` 中取得一个槽位。持久的 `<slot>.lock` 文件通过非阻塞的独占 `flock` 锁定整个会话。并发实例各自取得独立槽位，后续会话复用首个空闲槽位，并先清理其中的旧内容。锁文件位于槽位的 runtime 目录之外，清理时保留。LyX 继承锁的文件描述符，因此槽位会一直保留到 MCP server 和 LyX 子进程均释放该描述符。

每个槽位包含私有 runtime 和 userdir，设置独立的 `\serverpipe "$$UserDir/run/lyxserver"`，并以 `lyx --no-remote -userdir ...` 和 `QT_QPA_PLATFORM=offscreen` 启动 LyX。所有工具共享一个 LyXServer client ID 和一把异步锁。LyX 内部重启沿用同一槽位，重建 profile、管道、日志、缓存和编译临时文件，并保留导出文件与回滚快照直至会话结束。正常退出在释放锁前删除 runtime 内容；异常退出的残留在该槽位下次被取得时清理。槽位数量随并发使用峰值增长。

如果 `/tmp` 使用 tmpfs，也可将 `runtime_root` 指向磁盘上的可写目录。旧版本创建的随机名称 `lyx-mcp-*` 目录，应在对应实例停止后单独清理。

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
