# World 抽象：编译器与环境的契约

> 前置讲义：u1-l4（CLI 入口：从命令行到一次编译的调用链）、u2-l1（源码与文件身份：Source、FileId 与包路径）。
> 本讲进入第三单元「类型与求值」，第一次站在**编译器作者**视角看 Typst：它如何与外界环境解耦。

## 1. 本讲目标

学完本讲，你应当能够：

1. 逐条说出 `World` trait 七个方法（`library` / `book` / `main` / `source` / `file` / `font` / `today`）的职责与缓存约定。
2. 跟踪 `typst::compile` 入口：`&dyn World` 如何被 `track()` 成 `Tracked<dyn World>`，如何装进 `Engine`，再分发给求值与排版阶段。
3. 说出 `Engine` 六个字段（world、library、introspector、traced、sink、route）各自的用途。
4. 解释 `#[comemo::track]` 出现在源码的哪些位置、为什么「World 由宿主负责缓存，编译器只负责追踪读取」。
5. 亲手实现一个只含内联字符串源码的最小 `World`，并用它编译出第一份文档。

## 2. 前置知识

用到的概念都是 Rust 日常语法加一点架构直觉，不熟悉的术语在这里补齐：

- **trait 与 `dyn Trait`（动态派发）**：trait 定义一组方法签名，`&dyn World` 是「任意实现了 World 的类型的引用」，调用时按实际类型派发。编译器核心只依赖 `&dyn World`，不关心背后是文件系统、内存还是网络。
- **宿主与依赖注入**：把「读文件、找字体、取日期」这些环境操作抽象成接口，由调用方（宿主）注入实现。好处：编译器变成可独立测试、可嵌入（CLI、WebAssembly、语言服务器、测试运行器）的纯逻辑组件。
- **Source 与 FileId（回顾 u2-l1）**：`Source` 是一份已解析的源文件（文本 + 行信息 + 语法树，引用计数、克隆廉价）；`FileId` 是全局驻留（intern）的文件身份，由「虚拟根（项目/包）+ 虚拟路径」构成。
- **记忆化（memoization）**：函数记住「上次输入 → 上次输出」，下次同样输入直接复用。难点在于判断「输入是否真的没变」，这正是 comemo 要解决的问题，本讲 4.3 展开。
- **`LazyHash<T>`**：typst-utils 提供的「首次访问时才计算哈希」的包装，让 `Library`、`FontBook` 这类大对象参与缓存键时不必反复哈希。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/lib.rs` | `World` trait 定义与文档、`WorldExt`、`Library` / `LibraryBuilder`（标准库容器） |
| `crates/typst/src/lib.rs` | 总装 crate：`compile` / `trace` 入口、`compile_impl` 主流程、`LibraryExt`、`ROUTINES` 接线表 |
| `crates/typst-library/src/engine.rs` | `Engine`（编译上下文）、`Traced`、`Sink`、`Route` |
| `crates/typst-library/src/routines.rs` | `Routines` 函数指针表（跨 crate 行为接线的契约） |
| `crates/typst-cli/src/world.rs` | `SystemWorld`：生产宿主（文件系统 + 系统字体 + 时钟）的实现 |
| `tests/src/world.rs` | `TestWorld`：测试运行器宿主的实现，本讲综合实践的范本 |
| `crates/typst-kit/src/files.rs` | `FileStore`：宿主侧文件/源码缓存（可直接实现 `World::source/file`） |
| `crates/typst-kit/src/fonts.rs` | `FontStore`：宿主侧字体缓存（可直接实现 `World::book/font`） |
| `crates/typst-syntax/src/source.rs`、`crates/typst-syntax/src/path.rs` | `Source::new/detached`、`RootedPath::intern`（实践环节用到，回顾 u2-l1/u2-l4） |

依赖方向（承接 u1-l3 的地图）：`typst-eval` / `typst-realize` / `typst-layout` 等阶段 crate 只认识 `typst-library` 里的 `World` 与 `Engine`；`typst` crate 负责总装；宿主（CLI、测试）在最外层实现 `World`。
