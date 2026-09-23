# 设计核对与落地边界

本仓库本机 LyX 为 2.4.4；`lyx/` 是对应源代码。引用对话中的总体方向成立，但实现前需要以下修正。

1. `lyx/src/LyX.cpp` 中长期运行的 `Server` 在 GUI event loop 路径创建。`-batch` 执行命令后退出，不能作为 FIFO 后端。本机已验证 `QT_QPA_PLATFORM=offscreen`、`--no-remote`、独立 `-userdir` 下的 hello 和 LFUN 往返。
2. `lyx/src/LyXRC.cpp` 会把 `\serverpipe` 中的 `$$UserDir` 展开为该实例的 userdir。每个进程使用独立 runtime/userdir/pipe；不读取桌面 LyX 配置。默认最小 profile 使用本机 2.4.x 的 Preferences Format 38，其他版本只能从显式 seed 启动。
3. `lyx/src/Server.cpp` 的协议按换行分帧，参数不能含实际换行；hello 会注册 client，client 数量有限。一个进程使用一个长期有效的 ID，而不是每个请求换 ID。所有 LFUN 序列受全局锁保护。
4. `lyx/src/BufferView.cpp` 中 `word-find-forward` 的参数是大小写不敏感、非整词；旧 LFUN 文字说明不应作为唯一依据。先在 LyX 导出的文本中定位并消歧，再执行搜索。最终导出文本必须等于编辑计划的预期文本，否则回滚。
5. `lyx/src/LyXAction.cpp` 中 `changes-track` 是 toggle。编辑前读取文档 header，仅在 false 时切换，保存后再检查 true。快照发生在首次切换之前，以便失败时恢复原始字节；`output_changes` 不可改变。
6. `lyx/src/frontends/qt/GuiView.cpp` 的 GUI 导出路径可能异步运行。收到 `INFO` 之后还需等待新产物稳定。本机测试证明：TeX 报 `Undefined control sequence` 时，LyX 仍可能留下一个带 `%%EOF` 的 PDF。因此 PDF 除检查文件头尾，还必须找到本次生成的 TeX `.log`，排除 `!` 错误并确认 `Output written on`。普通编辑只能经 LFUN；直接写 `.lyx` 仅用于快照恢复。
7. 独立进程不代表可以安全地同时编辑同一文档。已知 SHA、较新的 autosave 和事务内 SHA 能检测一部分冲突。读取前若磁盘 SHA 已变，应强制 LyX buffer 重新载入，避免把过期 buffer 当作新内容。桌面实例未保存的内存修改仍不可见。

当前落地的顺序是：独立 runtime → FIFO 协议和生命周期 → 路径与 header 检查 → 普通文本编辑和事务 → PDF/checker gate → MCP v2 stdio 接口 → 本机集成测试。后续应分别验证多文件 master/child、Xvfb fallback、复杂 inset 与跨段编辑，不能把这些能力直接归入 V1 保证范围。

参考：[LyXServer 协议](https://wiki.lyx.org/LyX/LyXServer)、[MCP Python SDK v2](https://github.com/modelcontextprotocol/python-sdk)、[MCPServer 运行方式](https://py.sdk.modelcontextprotocol.io/run/)。
