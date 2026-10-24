# World 抽象：编译器与环境的契约

> 本讲是第三单元「类型与求值」的第一讲，承接 [u1-l4](u1-l4-cli-entry-and-first-compile.md)（CLI 调用链中出现过 `SystemWorld`）与 [u2-l1](u2-l1-source-and-fileid.md)（`Source` 与 `FileId`）。

## 1. 本讲目标

学完本讲，你应该能够：

- 逐条说出 `World` trait 七个方法的职责、返回类型与缓存约定，说清楚「谁负责缓存」的边界。
- 跟踪 `typst::compile` 入口：从 `&dyn World` 到 `Engine` 的构造，再到「求值一次、排版-内省循环」的完整交接过程。
- 解释 `#[comemo::track]` 出现在源码的哪三个位置、`world.track()` 做了什么，以及 `Routines` 函数指针表如何支撑「类型在 typst-library、行为在各阶段 crate」的架构。
- 动手实现一个只含内联字符串源码的最小 `World`，用它编译出第一份 `PagedDocument`。

## 2. 前置知识

### 2.1 trait 与 trait 对象：依赖倒置

Rust 的 trait 定义「一组能力的契约」，`dyn World` 是「任意实现了该契约的类型」的动态分发形态。编译器核心（`typst` crate）不直接读写文件系统，而是通过 `&dyn World` 索取输入——这就是依赖倒置：高层策略（编译算法）不依赖低层细节（文件从磁盘、内存还是网络来）。CLI、语言服务器、测试套件各自实现 `World`，编译器代码一行都不用改。

### 2.2 记忆化与增量编译：comemo 的直觉

comemo 是 Typst 团队维护的外部 crate（各编译器 crate 以 `comemo = { workspace = true }` 的方式引用它）。普通记忆化（memoization）是：

\[ f_{\text{memo}}(x) = \text{cache}[x] \quad (\text{当} x \in \text{cache})，\text{否则重算} f(x) \text{并写回缓存} \]

comemo 的改进在于：缓存键不是「输入的值」本身，而是「这次计算**实际读取了**输入的哪些部分、读到了什么」。一个没有被读取的部分发生变化，缓存仍然命中——这正是增量编译需要的性质。本讲只需要这个直觉：`World` 被 track 之后，重复编译时没被修改影响的部分会直接复用（机制细节在 u5-l8 展开）。

### 2.3 已学过的两块拼图

- **u2-l1**：`Source` 持有源码文本并提供行列/字节区间换算；`FileId` 标识一个虚拟文件。本 HEAD 中 `FileId` 由 `RootedPath`（= `VirtualRoot` + `VirtualPath`）经全局 interner 产生，是一个可 `Copy` 的 16 位编号（见 [crates/typst-syntax/src/path.rs:L75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/path.rs#L75) 的 `VirtualRoot` 枚举）。
- **u1-l4**：CLI 在 `compile.rs` 中组装 `SystemWorld`，然后调用 `typst::compile::<T>`。本讲回答：`SystemWorld` 到底实现了什么契约，`compile` 拿到它之后做了什么。

### 2.4 术语表

| 术语 | 一句话解释 |
| --- | --- |
| `World` | 编译器向宿主环境索取输入的 7 方法 trait |
| `Library` | 标准库：全局作用域、样式默认值、内置规则表等 |
| `Engine` | 编译期上下文容器，打包 world/library/sink 等，全阶段共享 |
| `Routines` | 函数指针表，让 library 能调用各行为 crate 的实现 |
| `Tracked<T>` | comemo 的追踪句柄，记录「哪些方法被调用过」 |
| `Sink` | 编译副产品的纯收集器：警告、延迟错误、被追踪的值 |

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `crates/typst-library/src/lib.rs` | 核心类型定义 | `World`/`WorldExt`/`Library`/`LibraryBuilder` |
| `crates/typst-library/src/engine.rs` | 编译上下文 | `Engine`/`Sink`/`Traced`/`Route` |
| `crates/typst-library/src/routines.rs` | 例程表 | `routines!` 宏与 `Routines` 结构 |
| `crates/typst/src/lib.rs` | 总装 crate | `compile`/`compile_impl`/`ROUTINES`/`LibraryExt` |
| `crates/typst-cli/src/world.rs` | 宿主实现范例 | `SystemWorld` |
| `tests/src/world.rs` | 最小实现范例 | `TestWorld`/`TestBase` |
| `crates/typst-kit/src/fonts.rs` | 字体工具 | `FontStore` 与 `embedded()` 提供器 |

阅读建议：本讲以 `typst-library/src/lib.rs` 和 `typst/src/lib.rs` 为主线，其余文件作为「同一契约的不同实现」对照着读。

## 4. 核心概念与源码讲解

### 4.1 World trait 契约

#### 4.1.1 概念说明

**它是什么**：`World` 是一个只有 7 个方法的 trait，是编译器与宿主环境之间的全部接口。

**解决什么问题**：编译器需要源码、字体、标准库、日期等输入，但输入来源千差万别——CLI 读磁盘、语言服务器持有内存缓冲、测试套件用内联字符串。如果编译器直接 `std::fs::read`，就无法被嵌入任何环境。`World` 把「输入从哪来」这个问题推给宿主。

**为什么还要约定缓存**：一次编译中 `world.source(id)` 可能被调用成千上万次（span 反查、每次 import、诊断定位）。如果每次都真的去读磁盘，性能会崩塌。所以 trait 文档明确要求：所有加载类方法必须做内部缓存，让重复调用变得便宜。

#### 4.1.2 核心流程

一次编译中，`World` 的方法在什么时候被谁调用：

```text
typst::compile(world)
  |- world.library()             取标准库（compile_impl 一开始）
  |- world.main()                主文件身份（FileId）
  |- world.source(main_id)       主源码（Source）
  |- world.source(id) x N        eval 阶段每次 #import
  |- world.file(id) x N          #data()/#read() 读取任意文件
  |- world.book()/world.font(i)  文本整形时查字体
  |- world.today(offset)         文档里使用 datetime 时
```

七个方法一览：

| 方法 | 返回类型 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `library` | `&LazyHash<Library>` | 全阶段 | 标准库；宿主一般用 `Library::builder(...)` 构建 |
| `book` | `&LazyHash<FontBook>` | 文本排版 | 字体元数据册（不含字体数据本体） |
| `main` | `FileId` | `compile_impl` | 主文件身份，编译入口 |
| `source` | `FileResult<Source>` | eval/诊断 | 按 id 取已解析源文件 |
| `file` | `FileResult<Bytes>` | `data()`/`read()` | 按 id 取任意文件字节 |
| `font` | `Option<Font>` | 文本整形 | 按 `book` 索引取字体本体（可惰性加载） |
| `today` | `Option<Datetime>` | `datetime` 函数 | 当前日期；返回 `None` 则该函数报错 |

**缓存责任的划分**（本讲最重要的设计决策）：

- **宿主负责缓存加载**：`World` 文档注释原话——加载函数应做内部缓存；`Source`/`Bytes`/`Font` 都是引用计数类型，克隆很便宜。
- **编译器负责追踪结果**：编译器自己不缓存加载，因为「World 比编译器更清楚什么会变」——字体几乎不变，可以跨多次编译缓存（对 `typst watch` 这类长驻进程友好）；源文件会变，每轮编译后应清理；高级客户端（语言服务器）可以保留源文件并用 `Source::edit` 原地编辑，获得更好的增量性能。

#### 4.1.3 源码精读

**（1）trait 定义与缓存契约原文**

[crates/typst-library/src/lib.rs:L47-L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L47-L61) 是 `World` 的文档注释，逐条写出上面「缓存责任的划分」的约定——读源码时请先读这段注释，它是契约的正式文本。

[crates/typst-library/src/lib.rs:L62-L101](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62-L101) 是 trait 本体：L62 的 `#[comemo::track]` 先记住（4.3 节展开），L63 起 `pub trait World: Send + Sync`，七个方法依次是：

- [L67](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L67) `fn library(&self) -> &LazyHash<Library>`——标准库入口；
- [L70](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L70) `fn book(&self) -> &LazyHash<FontBook>`——字体元数据；
- [L73](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L73) `fn main(&self) -> FileId`——主文件 id；
- [L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L76) `fn source(&self, id: FileId) -> FileResult<Source>`——按 id 取源文件；
- [L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L83) `fn file(&self, id: FileId) -> FileResult<Bytes>`——按 id 取字节，文档要求「`source` 成功的 id 这里也必须成功」，且可用 `Bytes::from_string` 从现有 `Source` 造一个廉价视图；
- [L91](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L91) `fn font(&self, index: usize) -> Option<Font>`——注意 L84-L90 的文档：索引**不保证**落在 `book()` 范围内，因为增量编译验证时可能拿着旧 book 的索引来询问，必须容忍越界并返回 `None`；
- [L100](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L100) `fn today(&self, offset: Option<Duration>) -> Option<Datetime>`——无 offset 取本地日期，有 offset 取对应 UTC 偏移的日期。

**（2）自动转发与扩展方法**

[crates/typst-library/src/lib.rs:L137-L139](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L137-L139) 用 `world_impl!` 宏为 `Box<W>`、`Arc<W>`、`&W` 生成了转发实现——宿主可以直接把 `Arc<MyWorld>` 当 `World` 用，不必手动实现。

[crates/typst-library/src/lib.rs:L142-L147](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L142-L147) 定义 `WorldExt`，唯一的 `range` 方法把 `Span` 反查成字节区间（u2-l4 讲过 span 结构）。它的 blanket 实现 [L149-L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L149-L159) 对任意 `World` 自动生效：按 `DiagSpanKind` 的三种形态（`Detached`/`Number`/`Range`）分别处理，其中 `Number` 形态会调用 `self.source(id)`——这就是 `source()` 被高频调用的原因之一。

**（3）两个现成实现对照**

CLI 的生产实现：[crates/typst-cli/src/world.rs:L126-L154](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L126-L154) 的 `impl World for SystemWorld`，每个方法都是一行委托——`book()` 转给 `self.fonts.book()`，`source()` 转给 `self.files.source(id)`，`today()` 转给 `self.now.today(offset)`。结构体定义在 [L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L25)，装配逻辑在 [L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L42) 的 `SystemWorld::new`（构建 `Library`、惰性发现字体、建立文件存储）。**实现 `World` 是「薄委托」而不是「重逻辑」**——缓存都藏在被委托的字段里（如 `LazyLock<FontStore>` 保证字体只扫描一次）。

测试套件的最小实现：[tests/src/world.rs:L53-L84](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L53-L84) 的 `impl World for TestWorld`，`source`/`file` 对主文件做特判（内联字符串就在手边），其余文件走共享的 `TestBase`；`today` 固定返回 1970-01-01 12:00，保证测试输出可复现。结构体在 [L34](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L34)，构造在 [L46-L51](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L46-L51)——`TestBase`（[L93](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L93) 起）把字体、文件、`Library` 做成全局单例，因此为几千个测试各建一个 `TestWorld` 都很便宜。

#### 4.1.4 代码实践

**实践目标**：找出仓库里所有 `World` 的实现者，并按宿主类型分类。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   grep -rn 'impl World for' crates/ tests/ --include='*.rs'
   ```

2. 对每个命中位置，打开文件看它的结构体字段，回答「这个宿主从哪里拿输入」。

**需要观察的现象 / 预期结果**（已验证，共 5 处）：

| 位置 | 宿主 | 输入来源 |
| --- | --- | --- |
| `crates/typst-cli/src/world.rs:126` | `SystemWorld` | 操作系统（磁盘、系统字体、系统时钟） |
| `crates/typst-cli/src/eval.rs:220` | `ExpressionWorld` | 命令行传入的表达式字符串（`typst eval` 子命令） |
| `crates/typst-ide/src/tests.rs:76` | `TestWorld`（IDE 测试） | 内存字符串 |
| `tests/src/world.rs:53` | `TestWorld`（集成测试） | 测试用例内联字符串 + 套件文件 |
| `tests/fuzz/src/lib.rs:39` | `FuzzWorld` | fuzzer 生成的随机输入 |

注意 `typst-library` 自己**没有**任何 `World` 实现——库 crate 只定义契约，实现属于宿主。这正是 4.1.1 依赖倒置的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `file()` 的文档要求「`source` 成功的 id，`file` 也必须成功」？

> **答案**：同一个 `FileId` 会走两条路径——`#import` 走 `source()`，`#data()`/`#read()` 走 `file()`。如果两条路径对同一文件的可见性不一致，同一份文档在不同代码路径下时而能编译时而不能，增量缓存与诊断都会错乱。

**练习 2**：`font(index)` 为什么可能收到越界索引？

> **答案**：见 [L84-L90](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L84-L90) 的文档——增量编译验证时，comemo 可能拿着**旧的**（或另一个 World 的）`FontBook` 索引来询问当前 World，宿主必须返回 `None` 而不是 panic。

**练习 3**：`TestWorld` 的 `today` 为什么固定为 1970-01-01 12:00？

> **答案**：测试输出必须可复现。若依赖系统时钟，含 `datetime` 的文档其引用输出会随运行日期漂移，`cargo testit` 的视觉对照（u1-l5）就失效了。

### 4.2 compile 入口与 Engine 交接

#### 4.2.1 概念说明

**`compile` 是什么**：总装 crate `typst` 对外的唯一编译入口。它是一个泛型函数 `compile<T: Output>`，泛型参数 `T` 决定产物类型——`PagedDocument`（分页排版）或 `HtmlDocument`（网页）。`Output` 是定义在 `typst_library::foundations` 中的 trait，统一了「创建文档」这个动作：`target()` 声明目标、`create()` 真正干活。

**Engine 是什么**：编译期上下文容器。求值、realize、排版的每一层函数都拿 `&mut Engine`，它打包了 world、library、introspector、traced、sink、route 六样东西——避免每个函数签名都拖一长串参数，也把「编译副作用」集中管理。

**为什么还有个 `trace` 函数**：语言服务器需要在某个 span 处「观察表达式求值出了什么值」。它和 `compile` 走完全相同的管线，唯一区别是传入的 `Traced` 带上了目标 span、结束后从 `Sink` 里取回观察值。所以两者共用内部实现 `compile_impl`。

#### 4.2.2 核心流程

`compile` 到 `Engine` 的完整交接（行号见 4.2.3）：

```text
typst::compile::<T>(&dyn World)
  |- Sink::new()                          新建警告/追踪值收集器
  |- world.track()                        &dyn World 转成 Tracked<dyn World>
  |- compile_impl::<T>(tracked, traced, &mut sink)
       |- world.library()                 取标准库
       |- match T::target()               Paged 放行；Html/Bundle 检查 feature 开关
       |- 组装 StyleChain（library.styles + target 样式）
       |- world.main() + world.source()   取主源码（失败则报「主文件无效」提示）
       |- typst_eval::eval(...)           求值一次 -> Module -> .content()
       '- loop（最多 MAX_ITERS 轮）        内省迭代（源码注释：五次尝试后放弃）
            |- introspector               上轮文档的内省器，或空的 EmptyIntrospector
            |- Constraint::new()          建立本轮约束
            |- Engine { library, world, introspector, traced, sink, route }
            |- T::create(&mut engine, &content, styles)   realize + layout
            '- constraint.validate(新文档的内省器)          稳定则 break
  '- Warned { output, warnings: sink.warnings() }
```

两个关键结论（呼应 u1-l3）：

1. **求值只发生一次**，循环的是「排版 + 内省」——因为 `query`/`counter` 这类功能要看排版结果（第 3 页有几个图）才能决定第 1 页怎么排，必须迭代到收敛（u4-l7 详解）。
2. 中间没有任何文件 IO——编译器需要的一切都经 `World`。

#### 4.2.3 源码精读

**（1）入口三件套：compile、trace、compile_impl**

[crates/typst/src/lib.rs:L64-L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L64-L83) 是 `compile` 的文档与本体：签名 `pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>>`（[L75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L75)），主体只有三步——建 `Sink`、调 `compile_impl`（[L80](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L80)，注意 `world.track()` 就发生在这行的实参里）、把 sink 里的警告连同产物包成 `Warned`（[L82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L82)）。L74 的 `#[typst_macros::time]` 为它加上计时 span（typst-timing，u4-l1 会用来观测各阶段耗时）。

[crates/typst/src/lib.rs:L88-L96](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L88-L96) 是 `trace`：同一个 `compile_impl`，传入 `Traced::new(span)`，最后 `sink.values()` 取回在目标 span 处观察到的值序列。

[crates/typst/src/lib.rs:L100-L104](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L100-L104) 是 `compile_impl` 的签名，注意第一个参数类型 `Tracked<dyn World + '_>`——**从这一刻起，World 以被追踪的形态在编译器内流转**，后面所有阶段拿到的都是它。

**（2）主流程四个锚点**

- [L116-L119](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L116-L119)：`world.main()` 拿 id，`world.source(id)` 取主源码，失败时 `hint_invalid_main_file` 给出「是不是写错了文件名」之类的提示——`World` 契约在错误路径上的体现。
- [L124-L132](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L124-L132)：调用 `typst_eval::eval`，六个实参（world/library/traced/sink/route/main）就是「求值阶段的全部环境」，返回 `Module` 后立刻 `.content()` 取出文档内容树（`Content` 的结构在 u3-l4 讲）。
- [L148-L155](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L148-L155)：循环体内构造 `Engine` 的字面量，六个字段一次装配。introspector 被包成 `Protected::new(introspector.track_with(&constraint))`——`Protected` 表示「引擎视角下只读」，`track_with(&constraint)` 把「排版实际读了哪些内省信息」记录进约束。
- [L157-L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L157-L159)：`T::create(&mut engine, &content, styles)` 触发 realize + layout；随后 `constraint.validate(document.introspector())` 判断「实际用到的内省信息」与「产出的内省信息」是否一致，一致则本轮结果稳定、`break`。

**（3）Engine 的六个字段**

[crates/typst-library/src/engine.rs:L18-L36](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L18-L36) 定义 `Engine`：

| 字段 | 行号 | 类型 | 说明 |
| --- | --- | --- | --- |
| `world` | [L20](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L20) | `Tracked<dyn World>` | 环境输入（被追踪） |
| `library` | [L26](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L26) | `&LazyHash<Library>` | 标准库；注释说明为避开 tracked 调用开销而提前取出 |
| `introspector` | [L28](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L28) | `Protected<Tracked<dyn Introspector>>` | 文档内省信息（上一轮的结果） |
| `traced` | [L30](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L30) | `Tracked<Traced>` | 语言服务器要观察的 span（普通编译为空） |
| `sink` | [L32](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L32) | `TrackedMut<Sink>` | 警告/延迟错误/追踪值的汇集处 |
| `route` | [L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L35) | `Route` | 编译路径：防循环 import、防递归过深 |

配套组件（同一文件）：`Sink`（[L157](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L157)）是纯收集器——[L194](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L194) 的 `warnings()`、[L199](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L199) 的 `values()`、[L204](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L204) 的 `extend_from_sink()`（内省循环每轮建子 sink，稳定后并回主 sink）；`Route`（[L263](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L263)）的 [L405](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L405) `contains` 判断循环 import，`check_show_depth`/`check_layout_depth`/`check_call_depth`（L359-L393 一带）防止递归失控；`Engine::delay`（[L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L42)）实现「延迟错误」——内省循环里先记账不立刻中断，若循环结束仍未消除才升级为致命错误。

**（4）Output：泛型入口如何路由到排版 crate**

[crates/typst-layout/src/document.rs:L78-L92](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L78-L92) 是 `impl Output for PagedDocument`：`target()` 返回 `Target::Paged`，`create()` 一行转调 `crate::layout_document(engine, content, styles)`。也就是说，`compile_impl` 里的 `T::create` 这一个调用点，就是「总装 crate 走进排版 crate」的接缝。`PagedDocument` 本体（字段 `pages`/`info`/`options`/`introspector`，[L18](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L18) 起）通过访问器 [L44](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L44) `pages()` 对外提供页面列表——字段本身是私有的，导出 crate 都走方法。

#### 4.2.4 代码实践

**实践目标**：不看 4.2.2 的伪代码，自己从 `compile` 出发重画调用图，验证「无文件 IO」。

**操作步骤**：

1. 打开 [crates/typst/src/lib.rs:L75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L75) 读 `compile` 主体。
2. 跳到 [L100](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L100) 的 `compile_impl`，依次定位：取 library、`match T::target()`、取 main、`typst_eval::eval`、`Engine` 构造、`T::create`。
3. 对 `T = PagedDocument` 打开 [crates/typst-layout/src/document.rs:L78](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L78)，确认 `create` 转调 `layout_document`。
4. 在这两个文件里搜索 `std::fs` 或 `File::open`——一个都找不到。

**需要观察的现象 / 预期结果**：画出一张 10 个节点以内的调用图，与 4.2.2 的伪代码互相印证；确认所有输入都来自 `world.xxx()` 调用。

#### 4.2.5 小练习与答案

**练习 1**：`compile` 为什么不直接返回 `T`，而是 `Warned<SourceResult<T>>`？

> **答案**：`SourceResult<T> = Result<T, EcoVec<SourceDiagnostic>>` 表达「致命错误集合」；`Warned` 再包一层非致命警告。这是排版系统的错误策略：能继续就继续，把问题攒起来最后一起报告——一个拼写错误不应该毁掉整份文档的编译。

**练习 2**：`Route` 字段解决什么问题？

> **答案**：两类失控——循环 import（`contains` 检查 FileId 是否已在当前路径上）与递归过深（show 规则互相触发、布局互相嵌套、函数互相调用，各有深度上限检查）。

**练习 3**：为什么 `Engine.library` 是普通引用，而 `world` 保持 `Tracked`？

> **答案**：见 [engine.rs:L21-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L21-L25) 的注释——`library` 访问极其频繁，编译一开始取出一次后不再变，用普通引用避免每次 tracked 调用的开销；`world` 的方法调用需要参与 comemo 依赖追踪，所以必须保持 `Tracked`。

### 4.3 comemo::track 的作用位置与 Routines 接线

#### 4.3.1 概念说明

**`#[comemo::track]` 做什么**：这个属性宏贴在 `World` trait 上，为它生成配套的追踪机制。`world.track()` 把 `&dyn World` 变成 `Tracked<dyn World>`——一个会记录「哪些方法被调用、返回了什么」的代理句柄。下游以 `Tracked` 值为参数的函数因此能在 comemo 的缓存里登记自己的依赖；下一次编译时，没受影响的函数直接命中缓存。**track 在输入侧记录依赖，`Constraint::validate` 在输出侧验证一致性**，两者配合让增量编译既复用又正确。

**Routines 解决什么问题**：u1-l3 给出过架构结论「类型住 typst-library、行为住 typst-eval/typst-layout」。但这有个循环依赖难题：library 的元素在编译时需要调用 layout 的函数，而 layout 又依赖 library 的类型。解法：library 定义一个函数指针表类型 `Routines`，总装 crate `typst` 在静态表 `ROUTINES` 里填入各行为 crate 的函数——源码注释称其为「本质上是动态链接（This is essentially dynamic linking and done to allow for crate splitting）」。

#### 4.3.2 核心流程

`track` 在本讲范围内的三个位置：

1. **定义处**：`#[comemo::track]` 挂在 `pub trait World` 之上（[typst-library/src/lib.rs:L62](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62)）。
2. **转换处**：`compile` 里 `world.track()`（[typst/src/lib.rs:L80](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L80)）。
3. **流转处**：`compile_impl` 形参（[L101](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L101)）与 `Engine.world` 字段（[engine.rs:L20](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L20)）。

`Routines` 的装配链：

```text
LibraryExt::builder(formats)                              使用者入口
  -> LibraryBuilder::from_routines(&ROUTINES, formats)    把静态表塞进 builder
     -> LibraryBuilder::build()
        |- (self.routines.rules)()               调函数指针拿内置规则表
        |- format.register_rules(&mut rules)     每个格式再注册自己的规则
        '- Library { routines, global, math, styles, rules, std, features, formats }
```

`Library.routines` 字段存住 `&'static Routines`，编译期各阶段通过它回调行为 crate 的函数。

#### 4.3.3 源码精读

**（1）Routines 的类型与条目**

[crates/typst-library/src/routines.rs:L32](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L32) 是宏生成的 `pub struct Routines`——每个字段都是一个 `fn` 指针类型（生成它的 `routines!` 宏就在同文件上方，其中对 `Routines` 的 `Hash` 实现是空的：函数指针地址不稳定，且它是全局单例，无需参与哈希）。条目清单在 [L51](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L51) 起的 `routines!` 调用块里：

| 条目 | 行号 | 提供行为的 crate |
| --- | --- | --- |
| `rules()` -> `NativeRuleMap` | [L53](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L53) | typst-layout / typst-html（在 `ROUTINES` 的闭包里填入） |
| `eval_string` | [L56](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L56) | typst-eval |
| `eval_closure` | [L69](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L69) | typst-eval |
| `realize` | [L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L83) | typst-realize |
| `layout_frame` | [L93](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L93) | typst-layout |
| `html_mathml_body` | [L102](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L102) | typst-html |
| `html_span_filled` | [L111](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L111) | typst-html |

**（2）总装 crate 的接线**

[crates/typst/src/lib.rs:L329-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L329-L342) 是 `static ROUTINES: LazyLock<Routines>`：`rules` 闭包里调 `typst_layout::register(&mut rules)` 和 `typst_html::register(&mut rules)`（[L330-L335](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L330-L335)），其余字段直接指向各 crate 的自由函数（[L336-L341](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L336-L341)）。`LazyLock` 保证整张表只初始化一次。注意各导出 crate（typst-pdf 等）只是 `typst` 的 dev-dependency（u1-l3 的结论），真正被 `ROUTINES` 引用的只有 layout/html/eval/realize。

**（3）从 builder 到 Library**

[crates/typst/src/lib.rs:L290-L322](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L290-L322) 的 `LibraryExt` 给 `Library` 提供两个构造入口 `new`/`builder`，实现里都在 [L321](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L321) 调 `LibraryBuilder::from_routines(&ROUTINES, formats)`。`from_routines` 本体在 [typst-library/src/lib.rs:L204-L214](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L204-L214)：把 `&'static Routines` 和格式列表存进 builder（builder 全貌 [L194-L199](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L194-L199)，定制项有 `with_inputs` [L217](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L217) 与 `with_features` [L225](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L225)）。

`build` 在 [typst-library/src/lib.rs:L231-L240](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L231-L240)：装配 math 模块与 global 作用域（`global()` 在 [L369](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L369)、`prelude()` 在 [L389](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L389)，u3-l2 的主题），通过函数指针 `(self.routines.rules)()` 拿内置规则表，再让每个 `Format` 注册自己的规则，最后拼出 `Library`。`Library` 的八个字段见 [L169-L188](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L169-L188)：`routines`（[L172](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L172)）、`global`/`math` 模块、`styles` 默认样式、`rules` 规则表、`std` 绑定、`features` 实验开关、`formats` 已注册格式。

**顺带一个「以源码为准」的实例**：[L160-L165](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L160-L165) 的文档注释建议「用 `Library::default()` 获得标准配置」，但整个 `crates/` 里并不存在 `impl Default for Library`（已 grep 验证）——文档滞后于代码。构造请用 `LibraryExt::new(...)` 或 `Library::builder(...)`。这也是 u1-l3 结论的又一次印证：注释与架构文档都可能滞后，最终依据是代码本身。

#### 4.3.4 代码实践

**实践目标**：看清 `ROUTINES` 单例与 `Library` 的装配顺序，能默写装配序列。

**操作步骤**：

1. 读 [crates/typst/src/lib.rs:L329-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L329-L342)，抄下 7 个例程各自来自哪个 crate。
2. 读 [tests/src/world.rs:L210](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L210) 起的 `library()`：测试库注册了哪 5 种 Format、往 global 作用域注入了什么（`check` 模块与 `test`/`test_repr`/`print` 函数——这就是 u1-l5 里 `#test` 断言的来源）。
3. 回答：新增一个导出格式需要在几处登记？

**需要观察的现象 / 预期结果**：能画出「`LibraryExt::builder` -> `from_routines(&ROUTINES)` -> `build()` -> rules 注册 -> `Library`」的装配序列图；第 3 步答案：在调用方把格式加进 `builder([...])` 列表，并在该格式的 crate 里实现 `register_rules`（u5-l3 展开）。

#### 4.3.5 小练习与答案

**练习 1**：`compile_impl` 里 `constraint.validate(...)` 验证的是什么？它和 `track` 是什么关系？

> **答案**：验证「本轮排版**实际读取**的内省信息」与「本轮**产出**的内省信息」是否一致——不一致说明文档还没收敛，需要再排一轮。`track` 在输入侧记录依赖（哪些 World 方法被读），`Constraint` 在输出侧记录消费（哪些内省信息被用），二者是 comemo 增量机制的两半。

**练习 2**：为什么 `Routines` 的 `Hash` 实现是空的？

> **答案**：`Routines` 全是函数指针，地址在不同构建间不稳定，参与哈希会破坏缓存一致性；而它本身是进程内单例，语义上恒等，无需区分。

**练习 3**：`Engine` 为什么不直接持有 `Routines`，而是持有 `library: &LazyHash<Library>`？

> **答案**：`Library` 已经打包了 `routines` 字段（`&'static Routines`）以及作用域、样式等；`Engine` 频繁访问 `library`，取一次引用即可连带拿到例程表（[engine.rs:L21-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L21-L25) 的注释解释了这种「提前取出」的取舍）。
