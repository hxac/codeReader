# World 抽象：编译器与环境的契约

> 前置讲义：u1-l4《CLI 入口：从命令行到一次编译的调用链》。
> 那一讲读到 `compile_once` 时，我们刻意放过了 `SystemWorld`——本讲就来还这笔债，并第一次站在**编译器作者**的视角看 Typst 最重要的一条架构边界：**编译器核心不读文件系统、不看系统时钟，它对外部世界的一切认知都来自一个 trait——`World`**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 逐条说出 `World` trait 七个方法的职责，以及每个方法对宿主「缓存责任」的约定。
2. 读懂 `typst::compile::<T>` 的总装流程：求值只做一次、布局迭代到内省收敛；`Engine` 如何作为「编译服务包」被组装并传递给各阶段。
3. 解释 `Routines` 函数指针表为什么被源码注释称为「本质上就是动态链接」，它如何落实「类型在 typst-library、行为拆到各阶段 crate」的架构决策（承接 u1-l3）。
4. 说出 `#[comemo::track]` 出现的位置与作用，理解「宿主缓存输入、comemo 缓存计算」的两层分工。
5. 独立实现一个只含内联字符串源码的最小 `World`，用它编译 Hello World 并断言产物非空。

## 2. 前置知识

**trait 与 trait 对象（`dyn Trait`）。** trait 定义一组方法签名，类型通过 `impl Trait for Type` 提供实现；Rust 还允许把「任意实现了该 trait 的类型」擦除成 trait 对象，写作 `&dyn World`，调用方法时经虚表在运行时分发。编译器全程只握着 `&dyn World`，它不知道、也不关心背后是 CLI 的文件系统世界，还是测试的内存世界。

**廉价克隆（clone ≈ 复制一个指针）。** `Source`、`Bytes`、`Font` 内部都是引用计数存储，克隆近乎零成本。这是 World 契约的地基：方法可以「大方地」按值返回，调用方反复索取也不会造成拷贝风暴。

**LazyHash：哈希只算一次。** `typst::utils::LazyHash<T>` 包装一个类型并缓存它的哈希值。凡是被 comemo 反复追踪、被样式系统反复比较的大对象（标准库、字体簿、样式集）都包成 `LazyHash`，所以 `World::library` 与 `book` 返回的是 `&LazyHash<Library>`、`&LazyHash<FontBook>`。

**记忆化（memoization）。** 「同样的输入 → 直接给上次的答案」。comemo 是 Typst 团队维护的 constraint-based memoization 库（根 Cargo.toml 依赖表中的 `comemo`，u1-l2 见过），它是增量编译的基石，第 4.3 节专门讲。

**与前几讲的衔接：**

- u1-l4：CLI 的 `compile_and_export` 最终调用 `typst::compile::<T>`，现场在 [crates/typst-cli/src/compile.rs:L350](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L350)。本讲把这个调用翻个底朝天。
- u2-l1 的主题：`FileId` 由「包身份 + 虚拟路径」组成，`Source` 持有源码文本。`World` 的 `main/source/file` 方法就是在分发这两类身份。
- u2-l4：Span 反查源码区间推荐用 `WorldExt::range`——本讲终于读到它的定义处。
- u1-l3：「类型定义在 typst-library、行为拆分到各阶段 crate」由 `Routines` 函数指针表接线——本讲读它的本体与装配现场。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `crates/typst-library/src/lib.rs` | 本讲主战场：`World` trait、`world_impl` 宏、`WorldExt`、`Library`/`LibraryBuilder` |
| `crates/typst/src/lib.rs` | 总装 crate：`compile`/`compile_impl` 入口、`LibraryExt`、`ROUTINES` 接线表 |
| `crates/typst-library/src/engine.rs` | `Engine` 编译上下文（另含 `Sink`、`Traced` 等） |
| `crates/typst-library/src/routines.rs` | `Routines` 结构体的宏定义 |
| `crates/typst-cli/src/world.rs` | `SystemWorld`：CLI 生产宿主的 World 实现 |
| `tests/src/world.rs` | `TestWorld`：集成测试套件的 World 实现，本讲综合实践的范本 |
| `crates/typst-kit/src/fonts.rs` | `FontStore` 字体仓库，直接支撑 `World::book/font` |
| `crates/typst-layout/src/document.rs` | `PagedDocument`：`compile` 的产物类型之一 |

数据流位置（承接 u1-l3 的地图）：编译是「字符串 → SyntaxNode → Content → Frame → 输出字节」的流水线；`World` 不属于其中任何一段，它站在整条流水线的**供给侧**——任何阶段需要外部输入时，都回头问它要。

## 4. 核心概念与源码讲解

### 4.1 World trait 契约

#### 4.1.1 概念说明

编译一份文档，编译器需要哪些「外部输入」？列一张清单：

- 一份标准库（全局作用域里有哪些函数、默认样式长什么样）；
- 一堆字体（文本要整形，就得有字形数据）；
- 任意多个文件（主文件、`#import` 的模块、图片、数据文件）；
- 一个当前日期（脚本里的 `datetime` 函数要用）。

Typst 把这张清单直接写成了一个 trait（[crates/typst-library/src/lib.rs:L62-L101](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62-L101)）。**「契约」是双向的**：

- 编译器承诺：除这七个方法外，绝不以任何方式接触外界（不 `std::fs::read`、不问系统时钟）；
- 宿主承诺：实现这七个方法，并管好各自的缓存。

这样设计值不值？看仓库里现成的七个实现（`grep -rn "impl World for" --include="*.rs" .` 可随时重查）：

| 实现 | 位置 | 宿主场景 |
|---|---|---|
| `SystemWorld` | [crates/typst-cli/src/world.rs:L126](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L126) | CLI：真实文件系统 + 系统字体 + 时钟 |
| `TestWorld` | [tests/src/world.rs:L53](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L53) | 集成测试：内存源码 + 固定时间 |
| `TestWorld` | crates/typst-ide/src/tests.rs:L76 | IDE 单元测试 |
| `DocWorld` | docs/src/world.rs:L89 | 文档站文档编译 |
| `ExampleWorld` | docs/src/example.rs:L257 | 文档站在线示例 |
| `FuzzWorld` | tests/fuzz/src/lib.rs:L39 | 模糊测试 |
| `ExpressionWorld` | crates/typst-cli/src/eval.rs:L220 | `typst query`/求值子命令 |

同一个编译器核心，从命令行工具、测试运行器到在线 playground、fuzzer 全部复用——这就是把「环境」抽象成 trait 的回报。

#### 4.1.2 核心流程

一次编译中，World 各方法被调用的「时刻表」大致如下：

```text
compile 开始
 ├─ main() + source(main)     ← 主文件：身份 + 源码，只取一次
 ├─ library()                 ← 贯穿全程：作用域、默认样式、内置规则
 ├─ 求值阶段（typst-eval）
 │   └─ source(id)            ← #import / #include 的模块
 ├─ 排版阶段（typst-realize / typst-layout）
 │   ├─ book()                ← 给文本挑字体（查字体元数据）
 │   └─ font(index)           ← 加载选中字体的字形数据
 ├─ 需要外部数据时（任何阶段）
 │   └─ file(id)              ← #image、#read、csv() 等读文件
 └─ 脚本调用 datetime 时
     └─ today(offset)
```

七个方法的职责与约定一览：

| 方法 | 返回类型 | 职责 | 缓存约定（源自 trait 文档） |
|---|---|---|---|
| `library()` | `&LazyHash<Library>` | 标准库 | 构造一次，长期复用 |
| `book()` | `&LazyHash<FontBook>` | 全部字体的元数据索引 | 字体很少变，可跨编译缓存 |
| `main()` | `FileId` | 主文件身份 | — |
| `source(id)` | `FileResult<Source>` | 取已解析源码 | 源文件常变，编译后应清理；LSP 可原地 `Source::edit` |
| `file(id)` | `FileResult<Bytes>` | 取任意文件字节 | 同上 |
| `font(index)` | `Option<Font>` | 按索引取字体本体 | 惰性加载，加载后缓存 |
| `today(offset)` | `Option<Datetime>` | 当前日期 | 同一次编译内应恒定 |

#### 4.1.3 源码精读

**先读 trait 上方的文档注释——它是整份契约的灵魂**（[crates/typst-library/src/lib.rs:L47-L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L47-L61)）：

```rust
/// All loading functions (`main`, `source`, `file`, `font`) should perform
/// internal caching so that they are relatively cheap on repeated invocations
/// with the same argument. [`Source`], [`Bytes`], and [`Font`] are
/// all reference-counted and thus cheap to clone.
///
/// The compiler doesn't do the caching itself because the world has much more
/// information on when something can change. For example, fonts typically don't
/// change and can thus even be cached across multiple compilations (for
/// long-running applications like `typst watch`). Source files on the other
/// hand can change and should thus be cleared after each compilation.
```

翻译成三句话：**① 加载类方法要做内部缓存；② 编译器不代劳，因为只有宿主才知道「什么会变」；③ 字体可跨编译缓存，源文件每次编译后应清理，LSP 类客户端可保留源文件原地编辑以换取增量性能。**这是本讲最重要的一段设计陈述，第 4.3 节会看到它与 comemo 的配合。

**trait 本体**（[crates/typst-library/src/lib.rs:L62-L101](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62-L101)；`#[comemo::track]` 注解先记住，4.3 专门讲）：

```rust
#[comemo::track]
pub trait World: Send + Sync {
    /// The standard library.
    fn library(&self) -> &LazyHash<Library>;

    /// Metadata about all known fonts.
    fn book(&self) -> &LazyHash<FontBook>;

    /// Get the file id of the main source file.
    fn main(&self) -> FileId;

    /// Try to access the specified file location as a source file.
    fn source(&self, id: FileId) -> FileResult<Source>;

    /// Try to access the specified file.
    fn file(&self, id: FileId) -> FileResult<Bytes>;

    /// Try to access the font with the given index in the font book.
    fn font(&self, index: usize) -> Option<Font>;

    /// Get the current date.
    fn today(&self, offset: Option<Duration>) -> Option<Datetime>;
}
```

四个值得注意的细节：

1. `Send + Sync`：排版阶段会用 rayon 并行处理（见 4.2.3 的 `Engine::parallelize`），World 必须能安全跨线程共享。
2. 错误显式进类型：`FileResult<T>` 即 `Result<T, FileError>`（[crates/typst-library/src/diag.rs:L771](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/diag.rs#L771)）——文件读不到是正常业务，不是 panic。`FileError` 的变体（`NotFound`、`AccessDenied`、`InvalidUtf8`……）定义在 [crates/typst-library/src/diag.rs:L775-L795](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/diag.rs#L775-L795)。
3. `font` 的文档特意警告：**索引不保证落在 `book()` 返回的范围内**——增量编译校验时，编译器可能拿着旧字体簿的索引来问新世界，因此必须防御性地返回 `Option`（[crates/typst-library/src/lib.rs:L85-L91](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L85-L91)）。
4. `today` 返回 `None` 完全合法，代价只是脚本里的 `datetime` 函数报错——所以最小 World 可以理直气壮地返回 `None`。

**`world_impl!` 宏：给包装类型自动实现 World**（[crates/typst-library/src/lib.rs:L103-L139](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L103-L139)）：

```rust
macro_rules! world_impl {
    ($W:ident for $ptr:ty) => {
        impl<$W: World> World for $ptr {
            fn library(&self) -> &LazyHash<Library> {
                self.deref().library()
            }
            // ……其余六个方法同样经 Deref 转发……
        }
    };
}

world_impl!(W for std::boxed::Box<W>);
world_impl!(W for std::sync::Arc<W>);
world_impl!(W for &W);
```

于是 `Box<dyn World>`、`Arc<MyWorld>`、`&&world` 都自动「是」一个 World，宿主与调用方都不必手工转发。

**`WorldExt::range`：Span 反查字节区间**（[crates/typst-library/src/lib.rs:L141-L160](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L141-L160)）。u2-l4 里推荐过它，现在看实现——三种 `DiagSpan` 形态各归其位：

```rust
impl<T: World + ?Sized> WorldExt for T {
    fn range(&self, span: impl Into<DiagSpan>) -> Option<Range<usize>> {
        match span.into().get() {
            DiagSpanKind::Detached => None,
            DiagSpanKind::Number { id, num, sub_range } => {
                self.source(id).ok()?.range(num, sub_range)
            }
            DiagSpanKind::Range { id: _, range } => Some(range),
        }
    }
}
```

编号型 Span 要回 `World` 取 `Source` 再换算区间——**这正是 World 契约的又一次兑现**：连诊断系统反查源码位置，也只能走 World。

#### 4.1.4 代码实践：两个真实 World 的对照阅读

1. **实践目标**：建立「实现一个 World ≈ 组装几个现成零件」的体感。
2. **操作步骤**：
   1. 打开 [crates/typst-cli/src/world.rs:L126-L154](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L126-L154)（`impl World for SystemWorld`）与 [tests/src/world.rs:L53-L90](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L53-L90)（`impl World for TestWorld`）。
   2. 对照源码填写下表「委托给谁」（已给出一半作为示范）：

   | 方法 | SystemWorld 委托给 | TestWorld 委托给 |
   |---|---|---|
   | `library` | ？ | `self.library`（`Arc<LazyHash<Library>>`） |
   | `book` / `font` | ？（提示：同一个组件） | `base.book` / `base.fonts.get(index)` |
   | `main` / `source` / `file` | `self.files`（`FileStore`） | 主文件特判，其余交 `base.files` |
   | `today` | `self.now`（`typst_kit::datetime::Time`） | ？（提示：返回固定值） |

   3. 再读 [crates/typst-kit/src/fonts.rs:L55-L71](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-kit/src/fonts.rs#L55-L71)：`FontStore::book()` 与 `font()` 的文档注释直说「Can directly be used to implement `World::book/font`」。
3. **需要观察的现象**：两个实现没有任何一个方法里有「重活」——全部是一行委托；`SystemWorld` 的 `book`/`font` 甚至委托给同一个 `FontStore`。
4. **预期结果**：参考答案——SystemWorld：`library → self.library`，`book/font → self.fonts（FontStore）`，`main → self.files.loader().main`，`today → self.now`；TestWorld 的 `today → Time::fixed(Datetime::from_ymd_hms(1970, 1, 1, 12, 0, 0))`（固定时间保证测试可复现，见 [tests/src/world.rs:L86-L89](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L86-L89)）。
5. 本实践为纯阅读型，无需运行验证。

#### 4.1.5 小练习与答案

**练习 1**：既然 `Source`/`Bytes`/`Font` 都是引用计数类型、克隆廉价，为什么 trait 文档还要求宿主做「内部缓存」？

**答案**：引用计数只解决「克隆便宜」，不解决「重复加载」。没有缓存时，每次 `source(id)` 都要重新读盘、重新解析；缓存后，comemo 增量编译期间对同一输入的高频重复询问，只付出一次哈希查找加一次引用计数的成本。

**练习 2**：为什么 `typst watch` 场景下字体可以跨编译缓存，源文件却建议「每次编译后清理」？

**答案**：trait 文档（[crates/typst-library/src/lib.rs:L54-L58](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L54-L58)）的原话：字体通常不变，长驻进程可以一直留着；源文件随时会被用户编辑，而「会不会变」的信息只有宿主掌握，所以缓存生命周期必须由宿主决定——编译器代劳只会帮倒忙。

**练习 3**：`WorldExt::range` 的 `DiagSpanKind::Number` 分支为什么用 `self.source(id).ok()?`，而不是直接 unwrap？

**答案**：编号型 Span 指向的文件可能已不在当前 World 中（诊断可能来自别的文件、或文件已被删除），`source` 返回 `Err` 时只能以 `None` 表示「查不到」。这也解释了方法签名为什么是 `Option<Range<usize>>`。

### 4.2 compile 入口与 Engine 交接

#### 4.2.1 概念说明

u1-l4 读 CLI 时，`compile_and_export` 里的 `typst::compile::<T>` 是个黑盒，现在打开它。先认识三个角色：

- **`compile<T>`**：总装 crate `typst` 的唯一编译入口。泛型参数 `T` 是「想要的产物类型」，必须实现 `Output` trait——例如 `PagedDocument` 在 [crates/typst-layout/src/document.rs:L78](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L78) 处 `impl Output for PagedDocument`（提供 `target()` 与 `create()` 两个入口）。CLI 传什么 `T`，由 `--format` 决定（u1-l4 讲过）。
- **`Engine`**：编译期上下文。求值与排版函数的签名里都有 `&mut Engine`——它是各阶段共享的「服务包」：世界、标准库、内省器、警告池、调用路由，全在这一个结构体里传递。
- **`Routines`**：函数指针表。u1-l3 说过「类型在 typst-library、行为拆到各阶段 crate」——`Routines` 就是那排接线端子，源码注释称之为「本质上就是动态链接」（essentially dynamic linking）。

#### 4.2.2 核心流程

`typst::compile::<T>(&world)` 的主干（伪代码，源码在 4.2.3 逐段精读）：

```text
compile::<T>(world: &dyn World)
 └─ compile_impl::<T>(world.track(), …)          ← 4.3 的主角：.track()
     ├─ library = world.library()
     ├─ 按 T::target() 校验目标（Paged / Html / Bundle）与特性开关
     ├─ styles ← library.styles + 目标信息
     ├─ main = world.source(world.main())         ← 主文件只取一次
     ├─ content = typst_eval::eval(…)?.content()  ← 求值也只做一次
     └─ loop（内省收敛循环，注释原话：五次内不收敛就放弃）
         ├─ introspector ← 上一轮的文档（首轮为空）
         ├─ constraint = comemo::Constraint::new()
         ├─ engine = Engine { world, library, introspector, traced, sink, route }
         ├─ document = T::create(&mut engine, &content, styles)  ← realize + layout
         └─ constraint.validate(document.introspector())？
             ├─ 成立 → 收敛，break
             └─ 不成立 → history.push(document)，再排一轮
```

收敛条件可以写成：

\[ \text{introspector}(D_n) = \text{introspector}(D_{n-1}) \implies \text{第 } n \text{ 轮即最终文档} \]

直觉：`counter`、`query`、交叉引用这类**内省**（introspection）既依赖布局结果、又反过来影响布局（页码、目录长度），所以必须迭代到自洽。普通文档第一轮就自洽。这是 u4-l7 的主题，本讲只需知道循环长在哪。

#### 4.2.3 源码精读

**入口 `compile`**（[crates/typst/src/lib.rs:L75-L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L75-L83)）：

```rust
pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>>
where
    T: Output,
{
    let mut sink = Sink::new();
    let output = compile_impl::<T>(world.track(), Traced::default().track(), &mut sink)
        .map_err(deduplicate);
    Warned { output, warnings: sink.warnings() }
}
```

只做三件事：建一个 `Sink`（警告、延迟错误、追踪值的收集池）；把 `&dyn World` 变成 `Tracked` 交给内部实现；错误去重后与警告一起打包成 `Warned`。注意 `world.track()`——下一节的主角在这里登场。

**`compile_impl` 前半：取库、取主文件、求值**（[crates/typst/src/lib.rs:L100-L132](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L100-L132)）：

```rust
fn compile_impl<T: Output>(
    world: Tracked<dyn World + '_>,
    traced: Tracked<Traced>,
    sink: &mut Sink,
) -> SourceResult<T> {
    let library = world.library();
    // ……目标校验（Paged/Html/Bundle）、样式链组装……
    // Fetch the main source file once.
    let main = world.main();
    let main = world
        .source(main)
        .map_err(|err| hint_invalid_main_file(world, err, main))?;

    // First evaluate the main source file into a module.
    let content = typst_eval::eval(
        world, library, traced, sink.track_mut(), Route::default().track(), &main,
    )?
    .content();
    // ……
```

主文件读取失败时走 `hint_invalid_main_file`（[crates/typst/src/lib.rs:L207-L225](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L207-L225)）补充提示——例如把 `.pdf` 误当 `.typ` 打开时会怀疑你写错了扩展名。这是 u1-l4 说「Typst 报错友好」在源码层的落点之一。

**后半：收敛循环**（[crates/typst/src/lib.rs:L134-L195](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L134-L195)，核心节选）：

```rust
    let mut history: ArrayVec<T, { MAX_ITERS - 1 }> = ArrayVec::new();
    let mut document: T;

    // Relayout until all introspections stabilize.
    // If that doesn't happen within five attempts, we give up.
    loop {
        let introspector = history
            .last()
            .map(|doc| doc.introspector())
            .unwrap_or(&empty_introspector);
        let constraint = comemo::Constraint::new();

        let mut subsink = Sink::new();
        let mut engine = Engine {
            library,
            world,
            introspector: Protected::new(introspector.track_with(&constraint)),
            traced,
            sink: subsink.track_mut(),
            route: Route::default(),
        };

        document = T::create(&mut engine, &content, styles)?;

        if timed!("check stabilized", constraint.validate(document.introspector())) {
            sink.extend_from_sink(subsink);
            break;
        }
        // ……history 满则分析内省历史、产出警告后 break；否则 push 继续……
    }

    // Promote delayed errors.
    let delayed = sink.delayed();
    if !delayed.is_empty() {
        return Err(delayed);
    }

    Ok(document)
}
```

三个细节：

1. 每轮新建一个 `subsink`，只有退出那轮（收敛或放弃）的警告才并回主 `sink`——中间轮次基于过期内省信息产生的警告不会泄漏给用户。
2. `Engine` 在**循环体内**每轮重建，因为 `introspector` 每轮都换。
3. 「延迟错误」机制（`Engine::delay`，[crates/typst-library/src/engine.rs:L38-L50](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L38-L50)）：布局期错误先记账不中断，收敛后若仍存在才升级为致命——一页的排版问题不会让你拿不到其他页的诊断。

**`Engine` 结构体**（[crates/typst-library/src/engine.rs:L17-L36](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L17-L36)）：

```rust
/// Holds all data needed during compilation.
pub struct Engine<'a> {
    /// The compilation environment.
    pub world: Tracked<'a, dyn World + 'a>,
    /// Definition of Typst's standard library.
    ///
    /// Can be accessed via `world.library()`, but we fetch it once upfront
    /// because it's accessed so frequently and we want to avoid the overhead of
    /// the tracked call.
    pub library: &'a LazyHash<Library>,
    /// Provides access to information about the document.
    pub introspector: Protected<Tracked<'a, dyn Introspector + 'a>>,
    /// May hold a span that is currently under inspection.
    pub traced: Tracked<'a, Traced>,
    /// A pure sink for warnings, delayed errors, and spans under inspection.
    pub sink: TrackedMut<'a, Sink>,
    /// The route the engine took during compilation. This is used to detect
    /// cyclic imports and excessive nesting.
    pub route: Route<'a>,
}
```

六个字段就是编译器的「全部家当」：环境、标准库、内省器、追踪器、警告池、调用路由（防循环导入与过深嵌套）。`library` 字段的注释值得单独回味：**库本可经 `world.library()` 随取随用，但它被访问得太频繁，宁可编译开始时取一份快照存成普通引用，也要省掉每次 tracked 调用的记账开销**——tracked 不是免费的，这是 4.3 的伏笔。

**`Routines`：动态链接表的定义与装配**。结构体由宏生成（[crates/typst-library/src/routines.rs:L21-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L21-L49)）：

```rust
macro_rules! routines {
    ($(
        fn $name:ident $(&<$($time:lifetime),*>)? ($($args:tt)*) -> $ret:ty
    )*) => {
        /// Defines implementation of various Typst compiler routines as a table
        /// of function pointers.
        ///
        /// This is essentially dynamic linking and done to allow for crate
        /// splitting.
        pub struct Routines {
            $(
                pub $name: $(for<$($time:lifetime),*>)? fn ($($args)*) -> $ret
            ),*
        }

        impl Hash for Routines {
            fn hash<H: Hasher>(&self, _: &mut H) {}
        }
    };
}
```

装配现场在总装 crate（[crates/typst/src/lib.rs:L325-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342)）：

```rust
static ROUTINES: LazyLock<Routines> = LazyLock::new(|| Routines {
    rules: || {
        let mut rules = NativeRuleMap::new();
        typst_layout::register(&mut rules);
        typst_html::register(&mut rules);
        rules
    },
    eval_string: typst_eval::eval_string,
    eval_closure: typst_eval::eval_closure,
    realize: typst_realize::realize,
    layout_frame: typst_layout::layout_frame,
    html_mathml_body: typst_html::html_mathml_body,
    html_span_filled: typst_html::html_span_filled,
});
```

`typst-library` 用 `routines!` 宏定义签名，`Library` 持有 `pub routines: &'static Routines` 字段（[crates/typst-library/src/lib.rs:L169-L172](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L169-L172)），`typst` 总装 crate 填入 `typst-eval/realize/layout/html` 的真实函数——**一次在编译期完成的「动态链接」**，让「类型」与「行为」得以分家。空 `Hash` 实现同样别有深意：函数指针的地址值不该参与任何内容哈希，这张表一旦建好就当作常量看待，否则会无谓搅动 comemo 的缓存键。

宿主怎么拿到装好线的库？`LibraryExt` 提供入口（[crates/typst/src/lib.rs:L289-L323](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L289-L323)）：

```rust
impl LibraryExt for Library {
    fn builder(formats: impl IntoIterator<Item = Format>) -> LibraryBuilder {
        LibraryBuilder::from_routines(&ROUTINES, formats)
    }
}
```

`Library::builder([...])` 一调用，`&ROUTINES` 就被缝进标准库。`Library` 的其余字段（`global/math/styles/rules/std/features/formats`）见 [crates/typst-library/src/lib.rs:L161-L188](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L161-L188)，装配细节留给下一讲 u3-l2。

#### 4.2.4 代码实践：数一数布局迭代了几轮

1. **实践目标**：亲眼确认「求值一次、布局可能多轮」。
2. **操作步骤**：
   1. 在本地分支打开 `crates/typst/src/lib.rs`，在 `compile_impl` 的 `loop` 内、`TimingScope::new` 那行（约 L140）之后临时插入一行：`eprintln!("[layout iter {}]", history.len() + 1);`
   2. 准备两个文件：`plain.typ` 只写 `Hello, world!`；`intro.typ` 写一句 `#query(heading, none).len()`（或放一个 `#outline()`）。
   3. 分别运行 `cargo run -p typst-cli -- compile plain.typ` 与 `cargo run -p typst-cli -- compile intro.typ`，观察 stderr。
   4. 验证完还原：`git checkout -- crates/typst/src/lib.rs`。
3. **需要观察的现象**：`plain.typ` 打印一行 `[layout iter 1]`；`intro.typ` 打印两行或更多。
4. **预期结果**：无内省的文档第一轮即收敛；用到 `query/outline/counter` 的文档需要第二轮才能拿到第一轮的内省结果。本实验作者未在本地运行，输出以「待本地验证」为准。

#### 4.2.5 小练习与答案

**练习 1**：`compile::<T>` 的 `T` 可以取哪些值？由哪个 trait 约束？

**答案**：任何实现 `Output` 的类型，目前有 `PagedDocument`（[crates/typst-layout/src/document.rs:L78](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L78)）与 `HtmlDocument`。`Output` 提供 `target()`（选择 Paged/Html/Bundle 目标）与 `create()`（布局入口），`compile_impl` 据此分叉。

**练习 2**：为什么求值在循环外、布局在循环内？

**答案**：求值产物 `Content` 不依赖布局结果，算一次就够；内省信息（页码、位置）依赖布局又影响布局，只能迭代。把 `eval` 挪进循环不会改变结果，只会白费功夫——这也呼应 u1-l3 的结论「compile 只求值一次，循环的是排版与内省」。

**练习 3**：`Routines` 为什么要手写一个空的 `Hash` 实现？

**答案**：默认派生的 `Hash` 会哈希所有字段——包括函数指针的地址。地址值在语义上是「常量表」的噪声：不同构建、不同加载方式下并不可靠，掺进哈希只会无谓搅动 comemo 缓存键。空实现让 Routines 对哈希「隐身」，符合「表内容恒定」的事实。

### 4.3 comemo::track 的作用位置

#### 4.3.1 概念说明

comemo（**co**nstraint-based **memo**ization，Typst 团队的独立 crate）解决的问题：**长驻进程反复编译时，如何只重算真正受影响的部分**。两个核心概念：

- **`#[comemo::track]`**：标注在 trait 定义上（全仓库只有一处——`World` 的定义处）。它让 trait 的每个方法获得「被追踪版」：调用会被登记为**约束**，并按输入记忆化。
- **`Tracked<T>`**：进入追踪世界的门票。`&dyn World` 要先 `world.track()` 变成 `Tracked<dyn World>`，之后的方法调用才走记忆化路径。

于是 Typst 的缓存分成泾渭分明的两层：

| 层 | 谁负责 | 缓存什么 | 失效依据 |
|---|---|---|---|
| 输入缓存 | 宿主（World 实现） | `Source`/`Bytes`/`Font` 的加载结果 | 宿主自己判断（文件会不会变） |
| 计算缓存 | comemo（编译器侧） | `eval`/`realize`/`layout` 等函数结果 | tracked 输入的约束比对 |

4.1.3 引用的那句「编译器不代劳缓存，因为宿主才知道什么会变」，说的就是这条分界线。

#### 4.3.2 核心流程

一次 tracked 调用的生命周期：

```text
engine 持有 Tracked<dyn World>
 └─ 调 world.source(id)
     ├─ comemo 登记约束：「source(id) 被调用过，参数是 id」
     ├─ 结果按「世界内容哈希 + 参数 id」缓存
     └─ 下次同参数调用：
         ├─ 世界没变 → 直接命中缓存
         └─ 世界变了 → 重新执行，并使旧约束失效
```

约束如何被利用：`compile_impl` 每轮布局前 `comemo::Constraint::new()`，把它绑给 introspector（`introspector.track_with(&constraint)`，[crates/typst/src/lib.rs:L145-L151](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L145-L151)）；布局期间对世界的一切读取都登记进这张约束表；轮末 `constraint.validate(新introspector)` 检查「这轮读过的所有东西，在新结果下是否仍然成立」——成立即收敛（[crates/typst/src/lib.rs:L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L159)）。

增量收益可以粗略写成（只对输入已变的阶段付费）：

\[ T_{\text{重编译}} \;\approx\; \sum_{p \,\in\, \text{受影响阶段}} C_p \quad\ll\quad \sum_{p \,\in\, \text{全部阶段}} C_p \]

这也是 `typst watch` 能秒级重编译的来源（u5-l8 会做实验验证）。

#### 4.3.3 源码精读

**注解位置——全仓库唯一**（[crates/typst-library/src/lib.rs:L62-L63](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62-L63)）：

```rust
#[comemo::track]
pub trait World: Send + Sync {
```

`#[comemo::track]` 是过程宏：它为 `World` 生成一套追踪版方法，把「谁在什么约束下问了什么」记账。**实现方完全无感**——4.1.1 表里那七个实现没有一行代码与 comemo 有关。

**入口转换**（[crates/typst/src/lib.rs:L79-L81](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L79-L81)）：

```rust
    let mut sink = Sink::new();
    let output = compile_impl::<T>(world.track(), Traced::default().track(), &mut sink)
        .map_err(deduplicate);
```

`&dyn World` 在此一分为二：交给 `compile_impl` 的是 tracked 视图；求值、排版函数经 `Engine.world` 字段拿到的也是 `Tracked<'a, dyn World + 'a>`（[crates/typst-library/src/engine.rs:L19-L20](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L19-L20)）——**整条编译流水线里，再没有人拿得到裸的 `&dyn World`**。

**约束的创建与校验**（[crates/typst/src/lib.rs:L145-L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L145-L159)）：

```rust
        let constraint = comemo::Constraint::new();

        let mut engine = Engine {
            // ……
            introspector: Protected::new(introspector.track_with(&constraint)),
            // ……
        };

        document = T::create(&mut engine, &content, styles)?;

        if timed!("check stabilized", constraint.validate(document.introspector())) {
            sink.extend_from_sink(subsink);
            break;
        }
```

**顺手的证据**：总装 crate 把 comemo 原样再导出（[crates/typst/src/lib.rs:L33-L34](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L33-L34) 的 `pub extern crate comemo;`），下游嵌入 Typst 时无需单独依赖 comemo。

#### 4.3.4 代码实践：把 track 的足迹找出来

1. **实践目标**：建立「tracked 类型在编译器里如何流动」的地图感。
2. **操作步骤**：在仓库根目录运行：
   ```bash
   grep -rn 'track()\|Tracked<' crates/typst/src/lib.rs crates/typst-library/src/engine.rs | head -20
   grep -rn 'track_mut()\|track_with(' crates/typst-eval/src crates/typst-layout/src | head -10
   ```
3. **需要观察的现象**：第一组是「入口转换」清单（`world.track()`、`Traced::default().track()`、`Route::default().track()` 等）；第二组是 tracked 调用在阶段 crate 里的延续（`sink.track_mut()` 等）。
4. **预期结果**：得到一张清单，印证 `Engine` 的 `world/introspector/traced/sink` 四个字段都是 tracked 类型，唯独 `library` 不是（见 4.2.3 引用的注释）。命令可运行，结果以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：`#[comemo::track]` 标在 trait 定义上还是实现上？实现 `World` 的代码需要配合做什么？

**答案**：标在定义上（[crates/typst-library/src/lib.rs:L62](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62)）。实现方什么都不用做——追踪发生在调用侧（`Tracked` 包装的方法分发），宏改写的是 trait 的调用协议，不是实现。

**练习 2**：`Engine.library` 为什么是普通引用而不是 `Tracked`？

**答案**：engine.rs L22-L25 的注释：库可经 `world.library()` 拿到，但它被访问得太频繁，而 tracked 调用有记账开销，于是编译开始时取一次快照存成普通引用。这是「记忆化并非零成本、热点要绕开」的实例。

**练习 3**：宿主在两次编译之间修改了某个被 `#import` 的文件，缓存会怎样？

**答案**：宿主应让 `World::source(id)` 返回新内容（最理想是原地 `Source::edit`，保住未变局部的 Span 稳定性——u2-l5 讲过的增量重解析）。comemo 发现该输入内容哈希变了，依赖它的 `eval` 缓存失效重算；不依赖它的部分（其他文件、布局中无关子树）继续命中缓存。

## 5. 综合实践：三十行代码造一个世界

目标：不借助文件系统，实现一个**只含内联字符串源码**的最小 World，编译 Hello World，断言产物非空，并顺手验证 comemo 的二次编译加速。范本是 [tests/src/world.rs:L53-L90](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L53-L90) 的 `TestWorld` 与 [tests/src/world.rs:L100-L114](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L100-L114) 的 `TestBase`（字体来自 `typst_assets::fonts()` 与 `typst_dev_assets::fonts()`）。

**步骤 1**：新建 `crates/typst-cli/examples/mini_world.rs`。typst-cli 已依赖 typst、typst-kit 且默认启用 `embedded-fonts` 特性（见 [crates/typst-cli/Cargo.toml:L91-L95](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/Cargo.toml#L91-L95)），`examples/` 目录会被 Cargo 自动发现。以下为**本讲义编写的示例代码**（非仓库原有文件）：

```rust
//! 最小 World：内联源码 + 内嵌字体，不碰文件系统。
//! 运行：cargo run -p typst-cli --example mini_world

use typst::diag::{FileError, FileResult};
use typst::foundations::{Bytes, Datetime, Duration};
use typst::syntax::{FileId, Source};
use typst::text::{Font, FontBook};
use typst::utils::LazyHash;
use typst::{Library, LibraryExt, World};
use typst_kit::fonts::{FontStore, embedded};
use typst_layout::PagedDocument;

struct MiniWorld {
    library: LazyHash<Library>,
    main: Source,
    fonts: FontStore,
}

impl World for MiniWorld {
    fn library(&self) -> &LazyHash<Library> {
        &self.library
    }

    fn book(&self) -> &LazyHash<FontBook> {
        self.fonts.book() // FontStore 文档明说：可直接用于实现 World::book
    }

    fn main(&self) -> FileId {
        self.main.id()
    }

    fn source(&self, id: FileId) -> FileResult<Source> {
        if id == self.main.id() {
            Ok(self.main.clone()) // Source 引用计数，克隆廉价
        } else {
            Err(FileError::NotFound(id.vpath().get_without_slash().into()))
        }
    }

    fn file(&self, id: FileId) -> FileResult<Bytes> {
        if id == self.main.id() {
            Ok(Bytes::from_string(self.main.clone()))
        } else {
            Err(FileError::NotFound(id.vpath().get_without_slash().into()))
        }
    }

    fn font(&self, index: usize) -> Option<Font> {
        self.fonts.font(index)
    }

    fn today(&self, offset: Option<Duration>) -> Option<Datetime> {
        None // 我们的世界里没有时间；代价是 datetime 函数报错
    }
}
```

接上，同一个文件里加上 `main`：

```rust
fn main() {
    // 组装世界：标准库（空格式清单，仅排版够用）+ 内联源码 + 内嵌字体。
    let world = MiniWorld {
        library: LazyHash::new(Library::new([])),
        main: Source::detached("Hello, *World*!"),
        fonts: {
            let mut store = FontStore::new();
            store.extend(embedded()); // Libertinus Serif、NCM、DejaVu Sans Mono
            store
        },
    };

    // 第一次编译。
    let start = std::time::Instant::now();
    let document = typst::compile::<PagedDocument>(&world);
    let first = start.elapsed();
    let document = document.output.expect("compilation failed");
    assert!(!document.pages().is_empty(), "文档不能没有页面");

    // 同一个世界再编译一次：观察 comemo 记忆化。
    let start = std::time::Instant::now();
    let again = typst::compile::<PagedDocument>(&world);
    let second = start.elapsed();
    assert!(again.output.is_ok());

    println!("pages = {}", document.pages().len());
    println!("first  compile: {first:?}");
    println!("second compile: {second:?}  ← 预期明显更快");
}
```

代码里每个 API 都有出处：`Source::detached`（[crates/typst-syntax/src/source.rs:L45](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs#L45)，给内联文本发一个独立 FileId）、`FontStore::extend/embedded`（[crates/typst-kit/src/fonts.rs:L129-L142](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-kit/src/fonts.rs#L129-L142)）、`LibraryExt::new`（空格式清单可行，见 [crates/typst/src/lib.rs:L348-L356](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L348-L356) 的测试）、`typst::compile::<PagedDocument>`（与 CLI 同款调用，[crates/typst-cli/src/compile.rs:L350](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L350)）、`Bytes::from_string` 接受任何 `AsRef<str>`（[crates/typst-library/src/foundations/bytes.rs:L72-L75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/bytes.rs#L72-L75)，`Source` 实现了它）。

**步骤 2**：运行 `cargo run -p typst-cli --example mini_world`。

**步骤 3**：观察三点：

1. 断言通过，`pages = 1`（默认 A4 页面下 Hello World 就是一页）。
2. 第二次编译显著快于第一次——eval/layout 的缓存全部命中，这是 comemo 生效的直观证据。
3. `Library::new([])` 传了空格式清单：本例只排版、不导出，够用；CLI 生产版注册全部五种格式，对照 [crates/typst-cli/src/world.rs:L67-L73](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L67-L73)。

**步骤 4（进阶）**：把 `main` 换成新字符串、重建 `MiniWorld` 再编译第三次——预期耗时回升（新输入让旧缓存失效）。这正是 4.3 说的两层缓存分界：**换不换世界的决定权，始终在宿主手里**。

**预期结果与免责**：以上代码依据仓库现有 API 编写，作者未在本地执行，运行输出「待本地验证」；若编译报错，优先检查 example 文件是否位于 `crates/typst-cli/examples/`（Cargo 自动发现）以及是否误开了 `--no-default-features`（`embedded()` 需要 `embedded-fonts` 特性）。

