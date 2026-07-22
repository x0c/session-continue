# pickup 性能知识库

## 什么时候读

改、评审或排查启动、会话扫描、对话预览、内嵌终端渲染、缓存、原生扩展、安装包或发布流水线时先读本文；各助手历史语义仍以 `SESSION_SCANNING_KNOWLEDGE_BASE.md` 为准，终端交互语义仍以 `EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md` 为准。

## 性能架构

pickup 的热路径分为四层：

1. 轻量入口只处理版本、缓存维护、只读 Agent 命令和更新命令；只有进入交互界面时才加载 Textual 与完整界面模块。
2. Claude、Codex、Kimi、Cursor 的历史元数据按源文件精确签名保存为本地派生缓存；OpenCode 继续使用自身 SQLite 查询与注册表内存签名。所有运行时仍并行扫描，缓存写入在一次扫描结束后批量提交。
3. 完整对话先查进程内缓存，再查本地派生缓存；只有源文件签名变化才重新解析。TUI 与 Agent 深度查询共用这一份结果。
4. JSON 解码与 ANSI 屏幕解析优先进入 Rust 原生扩展。屏幕解析在释放 Python 全局锁后完成，并直接返回合并后的行文本、样式区间和指纹，避免为每个终端格创建 Python 对象。扩展不可用或显式关闭时自动走语义相同的 Python 参考实现。

静态对话预览还缓存完整布局结果；滚动只切可见窗口，不再对每一可见行重复排版整篇对话。实时终端继续按行指纹比较，只重建和刷新变化行。

## 派生缓存边界

- 默认位置：`~/.cache/pickup/performance-cache.sqlite3`；遵循 `XDG_CACHE_HOME`，也可用 `PICKUP_CACHE_DIR` 改目录。
- 默认上限 256 MiB；可用 `PICKUP_CACHE_MAX_MB` 调整，最小 16 MiB。超过上限时优先淘汰完整对话，元数据保留以保障启动速度。
- 文件签名包含设备、inode、字节数和纳秒修改时间；Codex 额外包含标题索引签名，Cursor 额外包含提示历史和正文数据库签名。任一输入变化都视为未命中。
- 缓存目录权限为当前用户独占，数据库为当前用户读写。内容只来自用户本来可读的本机会话历史，不上传、不进入项目日志。
- 数据库损坏、锁竞争、只读文件系统或原生扩展缺失都必须降级为未命中，不能阻断原始历史读取。
- `PICKUP_CACHE=0` 可完全关闭；`pickup cache status` 查看状态，`pickup cache clear --dry-run` 预览，`pickup cache clear` 幂等清空。

## 原生扩展与分发

- 原生扩展使用稳定的 Python 3.10 ABI，一个平台产物覆盖该平台的 Python 3.10 及以上版本。
- 正式发布必须构建 macOS 通用轮子，以及 glibc/musl 的 Linux x86_64、aarch64 轮子，并附源代码包和校验和。
- 一键安装脚本按操作系统、CPU 架构和 Linux libc 直接选择预编译轮子；找不到匹配产物时才退回源码安装。项目支持范围仍是 macOS 与 Linux，不声明 Windows 支持。
- Homebrew 源码配方构建时必须声明 Maturin 与 Rust 构建依赖，并在隔离环境中生成轮子，不能继续调用旧的纯 Python 安装入口。
- `PICKUP_NATIVE=0` 可强制走 Python 回退，用于差分测试和故障隔离；正常用户不需要设置。

## 测量与验收

仓库的 `scripts/benchmark.py` 只输出计时与数量，不输出真实会话正文。性能改动至少记录：

```bash
python3 scripts/benchmark.py
PICKUP_NATIVE=0 python3 scripts/benchmark.py
python3 -c "import time; from pickup.runtime import default_registry; r=default_registry(); t=time.perf_counter(); r.scan_all(50); print(f'{(time.perf_counter()-t)*1000:.0f}ms')"
```

还必须完成完整单测、`selftest.sh`、至少 5 条真实会话抽样和 TUI 截图验收。原生 ANSI 解析必须与 Python 参考实现差分一致，覆盖索引色、真彩、宽字符、emoji、组合字符和非 SGR 转义序列。

基准应同时保留冷缓存与暖缓存数据；不把共享机器瞬时负载造成的单次抖动写成稳定结论。回归判断优先看多次中位数，并保留原生关闭时的对照组。
