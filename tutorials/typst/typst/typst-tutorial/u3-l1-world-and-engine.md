# World 抽象：编译器与环境的契约

> 前置讲义：u1-l4《CLI 入口：从命令行到一次编译的调用链》。
> 那一讲追踪到 `compile_once` 调用 `typst::compile::<T>` 时，我们刻意放过了它的参数 `world`——本讲就来还这笔债，并第一次站在**编译器作者**的视角看 Typst 最重要的一条架构边界：**编译器核心不读文件系统、不看系统时钟，它对外部世界的一切认知都来自一个 trait——`World`**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 逐条说出 `World` trait 七个方法的职责，以及每个方法对宿主（host，即嵌入编译器的程序）的「缓存责任」约定。
2. 读懂 `typst::compile::<T>` 的总装流程：求值只做一次、布局在内省循环里迭代到收敛；`Engine` 如何作为「编译服务包」被组装并传给各阶段。
3. 解释 `Routines` 函数指针表为什么被源码注释称为「本质上就是动态链接」，它如何落实 u1-l3 提出的「类型在 typst-library、行为拆到各阶段 crate」的架构决策。
4. 说出 `#[comemo::track]` 在本讲涉及源码中出现的全部位置，理解「宿主缓存输入、comemo 缓存计算」的两层分工。
5. 独立实现一个只含内联字符串源码的最小 `World`，用它编译 Hello World 并断言产物非空。

## 2. 前置知识

**trait 与 trait 对象（`dyn Trait`）。** trait 定义一组方法签名，类型通过 `impl Trait for Type` 提供实现；Rust 还允许把「任意实现了该 trait 的类型」擦除成 trait 对象，写作 `&dyn World`，调用方法时经虚表在运行时分发。编译器全程只握着 `&dyn World`，它不知道、也不关心背后是 CLI 的文件系统世界、测试套件的内存世界，还是文档站的定制世界。

**廉价克隆（clone ≈ 复制一个引用计数）。** `Source`、`Bytes`、`Font` 内部都是引用计数存储，克隆近乎零成本。这是 World 契约的地基：方法可以「大方地」按值返回，调用方反复索取也不会引发拷贝风暴。

**LazyHash：哈希只算一次。** `typst::utils::LazyHash<T>`（定义于 [crates/typst-utils/src/hash.rs:L72](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-utils/src/hash.rs#L72)）包装一个类型并缓存其哈希值。凡是被 comemo 反复追踪、被样式系统反复比较的大对象（标准库、字体簿）都包成 `LazyHash`，所以 `World::library` 与 `book` 返回的是 `&LazyHash<Library>`、`&LazyHash<FontBook>`。

**记忆化（memoization）。** 「同样的输入 → 直接返回上次的答案」。comemo 是 Typst 团队维护的 constraint-based memoization 库（u1-l2 在根 Cargo.toml 依赖表里见过它），是增量编译的基石，本讲第 4.3 节专门讲它在编译主流程中的用法。

**与前几讲的衔接：**

- u1-l4：CLI 的 `compile_and_export` 最终调用 `typst::compile::<PagedDocument>(world)`，现场在 [crates/typst-cli/src/compile.rs:L348-L352](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L348-L352)。本讲把这个调用的两侧（参数 `world` 与函数本体）都翻个底朝天。
- u1-l3：`Routines` 函数指针表是「类型与行为分离」决策的接线器——本讲读它的定义与装配现场。
- u2 系列：`Source` 持有源码文本、`FileId` 是文件的虚拟身份、`Span` 反查源码区间——`World` 的 `main/source/file` 方法正是在分发这几类身份。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `crates/typst-library/src/lib.rs` | 本讲主战场：`World` trait、`world_impl` 宏、`WorldExt`，以及携带 `routines` 字段的 `Library` |
| `crates/typst/src/lib.rs` | 总装 crate：`compile`/`compile_impl` 入口、内省循环、`LibraryExt`、`ROUTINES` 接线表 |
| `crates/typst-library/src/engine.rs` | `Engine` 编译上下文，以及 `Traced`、`Sink`、`Route` 三个伴随结构 |
| `crates/typst-library/src/routines.rs` | `Routines` 结构体的宏定义与函数指针清单 |
| `crates/typst-cli/src/world.rs` | 真实参考实现①：CLI 的 `SystemWorld` |
| `tests/src/world.rs` | 真实参考实现②：测试套件的 `TestWorld` |
| `crates/typst-kit/src/fonts.rs` | `FontStore`：可复用的字体存储，直接支撑 `book`/`font` 两个方法 |
| `crates/typst-syntax/src/path.rs` | `FileId`/`RootedPath`/`VirtualRoot`：`main()` 返回值的本体 |

## 4. 核心概念与源码讲解

### 4.1 World trait 契约：编译器眼中的「整个世界」

#### 4.1.1 概念说明

把编译器想象成一台纯函数机器：**输入**是「主文件 + 标准库 + 字体 + 当前时间」，**输出**是排好版的文档。只要输入不变，输出必然不变——这正是增量编译能缓存结果的前提。

问题在于：怎么获取输入？让编译器直接 `std::fs::read`？那它就永远只能是一个 CLI 程序，WebAssembly 版、语言服务器、在线预览、测试套件统统没法复用。Typst 的答案是把「获取输入」抽象成一个只有七个方法的 trait：

- 编译器核心（typst-eval、typst-layout 等）**只依赖** `World` 这个抽象；
- 宿主程序负责**实现** `World`：CLI 用文件系统实现，测试套件用内存字符串实现，文档站用「仓库目录 + 额外注入的函数」实现。

于是「编译器 ↔ 环境」之间有了一条清晰的所有权边界：**编译器不拥有任何输入，环境不参与任何编译决策**。这条契约还规定了缓存责任：宿主要自己缓存 `source/file/font` 的结果，编译器不做这份缓存——为什么，见 4.3.1。

#### 4.1.2 核心流程

一次编译中，编译器对 World 的调用时序大致是：

```text
typst::compile(world)
  │
  ├─ world.library()          // 取标准库（含 routines、样式、规则表）
  ├─ world.main()             // 取主文件的 FileId
  ├─ world.source(main_id)    // 取主源码 → 解析、求值
  │     └─ 求值中遇到 #import / #include / read()…
  │           ├─ world.source(other_id)   // 递归拉取依赖源码
  │           └─ world.file(other_id)     // 拉取任意文件（图片、数据…）
  │
  ├─ 排版中需要排文字
  │     ├─ world.book()       // 查字体元数据（FontBook）
  │     └─ world.font(i)      // 按索引取具体字体
  │
  └─ 文档里用了 datetime()
        └─ world.today(offset)  // 取当前日期
```

方法一览（七个，全是宿主必须实现的必需方法）：

| 方法 | 返回 | 职责 | 典型宿主缓存策略 |
|---|---|---|---|
| `library()` | `&LazyHash<Library>` | 标准库（作用域、样式、routines） | 进程内单例 |
| `book()` | `&LazyHash<FontBook>` | 全部字体的元数据索引 | 进程内单例（字体很少变） |
| `main()` | `FileId` | 主文件的身份 | 构造时确定 |
| `source(id)` | `FileResult<Source>` | 把文件当 Typst 源码取 | 按文件哈希缓存；每次编译后清空 |
| `file(id)` | `FileResult<Bytes>` | 把文件当原始字节取 | 同上 |
| `font(index)` | `Option<Font>` | 按 book 索引取字体本体 | 首次访问惰性加载后常驻 |
| `today(offset)` | `Option<Datetime>` | 当前日期（供 `datetime` 函数） | 一次编译内固定 |

#### 4.1.3 源码精读

**① trait 本体。** World 定义在 typst-library 的 crate 根：

[crates/typst-library/src/lib.rs:L62-L101](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62-L101)
这段代码用 `#[comemo::track]` 标注了整个 trait（细节在 4.3 讲），并声明 `World: Send + Sync`——`Send + Sync` 约束是因为排版阶段会用 rayon 并行处理子内容，多个线程要能同时通过 `&dyn World` 取输入。

逐个方法看关键约定：

- [L64-L67](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L64-L67)：`library()` 返回标准库，文档注明可通过 `Library::build()` 创建（即 4.2 要讲的 `LibraryExt`）。
- [L75-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L75-L76) 与 [L78-L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L78-L83)：`source` 与 `file` 分工——前者返回可解析的 `Source`，后者返回原始 `Bytes`。`file` 的文档要求「凡是 `source` 能成功的位置，`file` 也应成功」，并提示可以用 `Bytes::from_string` 从已有 `Source` 零拷贝地造出字节视图（因为 `Bytes::from_string` 接受任何 `AsRef<str>`，见 [crates/typst-library/src/foundations/bytes.rs:L72-L77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/bytes.rs#L72-L77)）。
- [L85-L91](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L85-L91)：`font(index)` 的索引**不保证**在 `book()` 范围内——增量编译校验时可能拿着旧 book 的索引来问新 world。这是「容错契约」的典型例子。
- [L93-L100](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L93-L100)：`today` 返回 `None` 是合法的，代价是脚本里的 `datetime` 函数报错。

**② trait 上方的缓存契约注释。** 这是全讲最重要的一段文档：

[crates/typst-library/src/lib.rs:L47-L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L47-L61)
注释直接回答了「为什么缓存归宿主」：编译器自己不了解「什么会变」——字体通常不变，可以跨多次编译缓存（`typst watch` 场景）；源文件每次编译都可能变，编译结束后就该清掉；语言服务器这类高级宿主甚至可以保留 `Source` 并用 `Source::edit` 原地增量编辑（正是 u2-l5 讲过的增量重解析入口）。**世界比编译器更懂自己的数据何时失效**，所以缓存责任放在世界一侧。

**③ 自动穿透包装的 blanket impl。**

[crates/typst-library/src/lib.rs:L103-L139](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L103-L139)
`world_impl!` 宏为 `Box<W>`、`Arc<W>`、`&W` 三种包装统一实现 `World`（L137-L139），全部方法解引用转发。这样无论宿主把世界装进什么容器递进来，编译器拿到的都直接是 `&dyn World`，不需要写任何适配代码。

**④ WorldExt：Span 反查的官方通道。**

[crates/typst-library/src/lib.rs:L141-L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L141-L159)
u2-l4 结尾提过的 `WorldExt::range` 就定义在这里：按 `DiagSpan` 的三种形态（Detached / Number / Range）分别处理，其中 Number 形态会调 `self.source(id)` 拿到 `Source` 再换算字节区间。**注意它是给所有 `World + ?Sized` 的 blanket 实现**——任何世界自动获得反查能力，这也是 IDE 功能（u5-l6）的基础设施。

**⑤ main() 返回的 FileId 是什么。** 顺带补齐 u2-l1 缺失的一块：`FileId` 是全局驻留（intern）的 16 位非零整数，指向一个 `RootedPath`：

- [crates/typst-syntax/src/path.rs:L17-L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/path.rs#L17-L35)：`RootedPath` = 虚拟根 + 虚拟路径；
- [crates/typst-syntax/src/path.rs:L73-L82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/path.rs#L73-L82)：虚拟根只有两种——项目根 `Project` 或某个包 `Package(PackageSpec)`。注意大纲里「FileId 由包与虚拟路径组成」的说法在当前代码中已细化为 `VirtualRoot` 枚举；
- [crates/typst-syntax/src/path.rs:L101-L129](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/path.rs#L101-L129)：`FileId::new` 查全局驻留表，重复路径复用同一编号，所以 `FileId` 是 `Copy` 的、比较和哈希都近乎免费。

#### 4.1.4 代码实践

**实践类型：源码阅读型（对照两个现成实现）。**

1. **实践目标**：确认「实现 World」在实践中到底是什么工作量，并识别每个实现背后的缓存取舍。
2. **操作步骤**：
   1. 打开 [crates/typst-cli/src/world.rs:L126-L154](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L126-L154)（`impl World for SystemWorld`），逐行核对七个方法分别把工作转发给了哪个字段（`library`、`fonts: LazyLock<FontStore>`、`files: FileStore`、`now: Time`）。
   2. 再看结构体定义 [L25-L38](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L25-L38) 与 [L112-L116](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L112-L116) 的 `reset()`：注意 `reset` 只清了 `files` 和 `now`，**没有**清 `fonts`——正对应 L47-L61 注释里「字体跨编译缓存、源文件每轮清空」的约定。
   3. 打开 [tests/src/world.rs:L53-L90](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L53-L90)（`impl World for TestWorld`）：`source/file` 只对主文件特判，其余依赖文件走共享的 `TestBase`；[L86-L89](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L86-L89) 的 `today` 返回**固定的 1970 年时间**——测试必须可复现，绝不能依赖真实时钟。
   4. 再看 [crates/typst-cli/src/eval.rs:L220-L252](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/eval.rs#L220-L252)：`ExpressionWorld` 是一个**包装器世界**，几乎全部方法转发给内部的 `SystemWorld`，只重写 `file` 来拦截表达式文件。这也是宿主侧扩展世界的惯用法。
3. **需要观察的现象**：三个实现的每个方法体几乎都是一两行转发；真正复杂的是世界背后的存储（`FileStore`、`FontStore`），而不是 trait 实现本身。
4. **预期结果**：你能画出「World 方法 → 宿主字段」的对照表，并说出每个实现各自牺牲了什么（SystemWorld：依赖真实文件系统与真实时钟；TestWorld：固定时间、依赖 tests 目录；ExpressionWorld：零新存储、纯委托）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `font(index)` 的文档特意声明索引可能越界，而 `source(id)` 没有类似声明？

**参考答案**：字体选择发生在排版阶段，增量编译下编译器可能缓存着用**旧字体簿**索引算出的中间结果，重放时拿旧索引问新世界，必须容错返回 `None`（或让宿主按需加载）。而 `source(id)` 的 `id` 来自当前这轮编译实际读到的文件标识，不存在新旧错配问题，失败直接以 `FileResult::Err` 报「文件不存在」即可。

**练习 2**：`world_impl!` 宏为 `Box<W>`/`Arc<W>`/`&W` 实现 `World` 有什么实际意义？删掉会怎样？

**参考答案**：编译器 API 统一收 `&dyn World`，有了这三个 blanket impl，宿主无论传 `&world`、`&Box<World>`（比如 CLI 里 `world` 被 `Box` 装箱后）还是 `&Arc<SystemWorld>` 都能自动协变到 trait 对象，无需手写适配。删掉后每个宿主都要为每种包装手写一份纯转发的 impl，属于样板代码灾难。

**练习 3**：如果要让 `TestWorld` 支持在测试里用 `datetime`，且结果可复现，应该改哪个方法？怎么改？

**参考答案**：改 `today`。现状 [tests/src/world.rs:L86-L89](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L86-L89) 已经是标准答案的形态：返回固定的 `Datetime::from_ymd_hms(1970, 1, 1, 12, 0, 0)`，再交给 `Time::fixed` 处理时区偏移。绝不能返回真实系统时间，否则测试输出会随运行日期漂移，视觉对照测试全部失效。

### 4.2 compile 入口与 Engine 交接

#### 4.2.1 概念说明

`World` 解决了「输入从哪来」，剩下的问题是：编译器内部各阶段（eval、realize、layout，彼此是独立 crate）在执行时需要**同时**拿到世界、标准库、内省器、警告收集器、循环检测器……如果把这些全塞进每个函数的参数表，签名会爆炸。

Typst 的做法是两件套：

1. **`Engine`**：一个打包了五样东西的「编译服务包」结构体，各阶段只接一个 `&mut Engine` 参数。
2. **`Routines`**：一张函数指针表。typst-library 里定义各阶段的**签名**，总装 crate `typst` 里一次性接线到 typst-eval/typst-realize/typst-layout 的**实现**。源码注释的原话是「This is essentially dynamic linking and done to allow for crate splitting」——本质上是动态链接，目的是允许 crate 拆分。这承接了 u1-l3 的结论：签名（类型）住在 typst-library，行为住在各阶段 crate，`Library.routines` 字段就是两者之间的那根线。

还有一个入口层概念：`compile` 是**泛型**的，`T: Output` 决定产出物类型——`PagedDocument`（分页目标，PDF/PNG/SVG 的共同上游）或 `HtmlDocument`。CLI 里 `typst::compile::<PagedDocument>(world)`（[crates/typst-cli/src/compile.rs:L350](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L350)）的 turbofish 写法正是在选目标。

#### 4.2.2 核心流程

`compile_impl` 的主干可以概括成下面的伪代码（省略警告与计时）：

```text
compile::<T>(world):
    sink = Sink::new()
    return compile_impl::<T>(world.track(), Traced::default().track(), sink)

compile_impl::<T>(world, traced, sink):
    library = world.library()
    按 T::target() 检查特性开关（html/bundle 需要显式 --features）
    content = typst_eval::eval(world, library, ..., main_source).content()
    // ↑ 求值只做一次

    loop:  // 最多 MAX_ITERS = 5 轮
        introspector = 上一轮文档的内省器（首轮为 EmptyIntrospector）
        constraint = comemo::Constraint::new()
        engine = Engine { library, world, introspector(带 constraint), traced, subsink, route }
        document = T::create(&mut engine, &content, styles)   // realize + layout
        if constraint.validate(document.introspector()):
            break    // 本轮排版实际用到的信息 == 本轮传入的信息 → 收敛
        history.push(document)

    若 sink 中还有延迟错误 → 返回 Err
    return document
```

两个关键设计：

- **求值一次，布局多轮**。目录、交叉引用这类「内省」信息要等排版完才知道，所以排版阶段拿着上一轮的内省器反复重排，直到「喂进去的」和「实际用到的」一致（收敛判定详见 4.3）。这是 u4-l7 的伏笔，本讲只需记住循环的位置。
- **延迟错误（delayed errors）**。前几轮布局里 show 规则可能因为内省器还没就绪而报错，这类错误先记进 sink，若最后一轮仍然存在才升级为致命错误。

#### 4.2.3 源码精读

**① compile 入口。**

[crates/typst/src/lib.rs:L64-L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L64-L83)
公开入口只做三件事：建一个 `Sink`（收警告）、把 `&dyn World` 转成 tracked 形式（`world.track()`，见 4.3）、调用内部 `compile_impl` 并把结果连同警告打包成 `Warned<SourceResult<T>>`。旁边的 [L87-L96](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L87-L96) 还有一个兄弟入口 `trace`：指定一个 `Span`，编译一遍，收集该位置求值过程中出现过的所有值——这是 IDE「跳转/求值预览」类功能的数据来源（u5-l6 会再遇到它）。

顺带注意 [L33-L40](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L33-L40)：总装 crate 通过 `pub use typst_library::*` 把 World/Engine/Library 全部再导出，并 re-export `comemo`/`syntax`/`utils`——所以下游（比如 CLI）只需要依赖 `typst` 一个 crate 就凑齐了全部类型。

**② 取主文件与求值。**

[crates/typst/src/lib.rs:L112-L132](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L112-L132)
这里能看到 World 的前三个方法被消费的现场：`world.library()` 取库、`world.main()` 拿主文件 id、`world.source(main)` 拿源码；读主文件失败时走 [L209-L245](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L209-L245) 的 `hint_invalid_main_file` 生成「你是不是想编译 file.pdf」这类提示——错误信息的友好度也从这一层开始做起。随后 `typst_eval::eval(...)` 把主源码变成模块，取其 `content()` 进入排版准备。注意 eval 只在循环**外**调用一次。

**③ 内省循环与 Engine 的组装现场。**

[crates/typst/src/lib.rs:L134-L186](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L134-L186)
循环体里每轮：从上一轮文档取内省器（首轮用 `EmptyIntrospector`，L115）；新建 `comemo::Constraint`（L145）；组装 `Engine`（L148-L155）；调 `T::create` 完成 realize + layout（L157）；用 [L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L159) 的 `constraint.validate(document.introspector())` 判断是否收敛。`history` 是容量 `MAX_ITERS - 1` 的 `ArrayVec`，`MAX_ITERS = 5` 定义在 [crates/typst-library/src/introspection/convergence.rs:L16-L17](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/introspection/convergence.rs#L16-L17)；装满仍未收敛就放弃并让 `analyze` 生成诊断（L164-L183）。循环结束后 [L189-L192](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L189-L192) 把残留的延迟错误升级为致命错误。

**④ Engine 结构体。**

[crates/typst-library/src/engine.rs:L17-L36](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L17-L36)
五个字段的职责：

| 字段 | 类型 | 职责 |
|---|---|---|
| `world` | `Tracked<dyn World>` | 环境输入（4.3 讲 `Tracked`） |
| `library` | `&LazyHash<Library>` | 标准库。注释（L21-L25）说明：本可经 `world.library()` 拿，但它被调用得太频繁，先取一次存起来，省去每次 tracked 调用的开销 |
| `introspector` | `Protected<Tracked<dyn Introspector>>` | 文档内省信息（目录、计数器、位置） |
| `traced` | `Tracked<Traced>` | 正被观察的 Span（配合 `trace` 入口） |
| `sink` | `TrackedMut<Sink>` | 警告/延迟错误/追踪值的只进收集器 |
| `route` | `Route` | 当前调用路径，检测循环导入与过深嵌套 |

Engine 还提供两个值得认识的方法：[L38-L50](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L38-L50) 的 `delay`（把错误压进 sink 而不是立刻中断，正是「延迟错误」的写入点）和 [L52-L102](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L52-L102) 的 `parallelize`（给每个子任务克隆一个带独立 subsink 的 Engine 并行跑，最后把各 subsink 合并回来——`World` 必须 `Send + Sync` 的原因就在这）。`Route` 的深度上限（show 规则 64 层、函数调用 80 层等）在 [L345-L399](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L345-L399)。

**⑤ Routines：签名与接线。**

- 签名侧：[crates/typst-library/src/routines.rs:L21-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L21-L49) 用宏生成 `Routines` 结构体——每个字段是一个 `fn` 指针，比如 [L93-L99](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L93-L99) 的 `layout_frame`。完整清单见 [L51-L112](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L51-L112)：`rules`（内置 show 规则表）、`eval_string`、`eval_closure`、`realize`、`layout_frame`，以及两个 HTML 专用例程。
- 接线侧：[crates/typst/src/lib.rs:L325-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342) 的静态 `ROUTINES` 表把每个槽位填上真实函数（`typst_eval::eval_string`、`typst_realize::realize`、`typst_layout::layout_frame`……）。`rules` 槽位更妙：它是个**闭包**，调用时才注册 typst-layout 与 typst-html 的规则。
- 注入路径：[crates/typst/src/lib.rs:L315-L322](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L315-L322) 的 `LibraryExt::builder` 把 `&ROUTINES` 塞进 `LibraryBuilder::from_routines`；宿主建世界时调 `Library::builder([...formats...]).build()`（真实例子见 [crates/typst-cli/src/world.rs:L56-L77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L56-L77)，CLI 在这里同时注入了 inputs、features 和五种导出格式），最终存进 [Library.routines 字段](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L167-L172)。此后 typst-library 里的内置元素想排版自己时，就经 `library.routines.layout_frame(...)` 间接调用 typst-layout 的实现——依赖箭头始终单向，无循环。

#### 4.2.4 代码实践

**实践类型：行为观察型（CLI 实验，无需改代码）。**

1. **实践目标**：亲眼看到 `compile_impl` 开头的特性门控（[crates/typst/src/lib.rs:L106-L110](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L106-L110)）与 `--features` 参数的完整链路。
2. **操作步骤**：
   1. 准备文件：`echo '#html.h1[Hi]' > /tmp/hello.typ`（用任意内容皆可）。
   2. 运行 `cargo run -p typst-cli -- compile /tmp/hello.typ --format html`。
   3. 再运行 `cargo run -p typst-cli -- compile /tmp/hello.typ --format html --features html`。
3. **需要观察的现象**：第 2 步应编译失败；第 3 步应能产出 HTML（或带警告成功）。
4. **预期结果**：第 2 步的错误消息是 `html export is only available when `--features html` is passed`，正对应 [crates/typst/src/lib.rs:L259-L265](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L259-L265) 的 `bail!`；第 3 步出现 `html export is under active development and incomplete` 警告，对应 [L250-L257](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L250-L257) 的 `sink.warn`。参数之所以能传到编译器，靠的是 CLI 的 `--features` 定义（[crates/typst-cli/src/args.rs:L519-L522](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L519-L522)）→ `SystemWorld::new` 里 `Library::builder(...).with_features(...)`（[crates/typst-cli/src/world.rs:L64-L77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L64-L77)）→ 编译时 `world.library().features` 检查。若本地输出与上述不符，以实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`compile::<T>` 里的 `T` 起什么作用？CLI 编译 PDF、PNG、SVG 时传的是同一个 `T` 吗？

**参考答案**：`T: Output` 决定编译产物形态与其后的分支逻辑（`T::target()` 决定 Paged/Html/Bundle 三条路，`T::create` 执行该目标的 realize+layout）。PDF/PNG/SVG 共享同一个 `T = PagedDocument`——它们只是同一份分页文档的不同导出方式（u5-l1/u5-l2 的主题），分叉发生在 `compile` 之后的导出阶段；只有 HTML（`HtmlDocument`）和 Bundle 才是不同的 `T`。

**练习 2**：为什么 `Engine.library` 要在 `compile_impl` 开头取一次存成字段，而不是每次用 `engine.world.library()`？

**参考答案**：见 [crates/typst-library/src/engine.rs:L21-L26](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L21-L26) 的注释：标准库在编译中被访问得极其频繁，而 `world.library()` 是 tracked 调用（有约束记录开销），取一次存 `&LazyHash<Library>` 引用即可让后续访问变成普通解引用。

**练习 3**：`Routines` 的 `Hash` 实现是空的（`fn hash(&self, _: &mut H) {}`），结合 `LazyHash<Library>` 想一想为什么这样可以接受？

**参考答案**：`Routines` 表内容在进程生命周期内恒定不变（静态 `ROUTINES` 由 `LazyLock` 初始化一次），所有实例等价，哈希成常量不影响正确性。把它算作零贡献可以让包含它的 `Library` 的哈希不必依赖函数指针地址（函数指针本身不可哈希），`LazyHash` 缓存的哈希也因此跨次编译稳定——这正是 comemo 判断「输入没变」所需要的性质。

### 4.3 comemo::track 的作用位置

#### 4.3.1 概念说明

现在补上贯穿全讲的最后一个概念。comemo 做的事情一句话：**给「带副作用的查询」建立可失效缓存**。

编译器里到处都是「问世界要数据、再基于数据做重计算」的函数。若世界变了（你改了一个字符），哪些重算、哪些复用？朴素的记忆化不够用，因为函数可能拿「还没定稿的输入」算出结果——比如第一轮布局用了「尚不完整的目录」，第二轮目录变了，旧结果就作废了。

comemo 的方案叫**约束式记忆化（constrained memoization）**：

- 被 `#[comemo::track]` 标注的类型，其方法调用会被包装：执行时顺带把「本次调用读了哪些输入、各输入的哈希」记录进一个 `Constraint` 对象；
- 下次重放同一段计算时，对每个 tracked 调用先算新环境下参数的哈希，与记录比对：一致就直接吐缓存结果，不一致才真正执行并从断点重算；
- 「输入哈希全部对上」可以写成：

\[ \text{缓存可复用} \iff \forall\, c \in \text{记录的调用}:\; h_c^{\text{旧}} = h_c^{\text{新}} \]

- 排版循环里那个 `constraint.validate(document.introspector())` 则是反向运用：本轮布局**宣称**用的是传入的内省器，把实际产出的内省器哈希与 constraint 里记录的比对，全部相等说明「布局没有偷看未来信息」，文档稳定。

这就解释了 4.1 的缓存分工：**宿主缓存「输入数据」（字体、文件），comemo 缓存「计算结果」（解析树、样式、帧）**。两层各管一段，增量编译才能既正确又快。

#### 4.3.2 核心流程

一次 `compile` 中 comemo 相关对象的协作：

```text
&dyn World
   │ world.track()                 // compile 入口：转为 Tracked<dyn World>
   ▼
Tracked<dyn World> ──进入──▶ compile_impl
   │
   │ 每轮循环：
   │   Constraint::new()                        // 空约束
   │   introspector.track_with(&constraint)     // 内省器调用记录进约束
   │   Engine { world, introspector, sink, ... }
   │   T::create(engine, ...)                   // 期间一切 tracked 调用被记录
   │   constraint.validate(新文档.introspector())
   │       ├─ 一致 → 收敛，break
   │       └─ 不一致 → 下一轮（comemo 会复用本轮中未受影响的子计算）
   ▼
Sink（TrackedMut）                // 警告/延迟错误：可变侧的追踪
```

`#[comemo::track]` 在本讲源码中的全部落点（共五处，属性语法上分两种：标在 trait 上会为整个 trait 生成 tracked 版方法；标在 `impl` 块上效果类似）：

| 位置 | 追踪对象 | 为什么需要追踪 |
|---|---|---|
| [typst-library/src/lib.rs:L62](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62)（标在 trait 上） | `World` 的读取 | 世界的每次读取都要能被记录/重放，否则「源文件改了」无法失效下游缓存 |
| [typst-library/src/engine.rs:L138](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L138) | `Traced::get` | `trace` 功能读它判断某 Span 是否在被观察文件里 |
| [typst-library/src/engine.rs:L209](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L209) | `Sink` 的写入 | 注释（L150-L156）说明：纯写入方法本可不验证，但该优化 comemo 尚未实现，先统一追踪 |
| [typst-library/src/engine.rs:L401](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L401) | `Route::contains`/`within` | 深度与循环检测的查询也要参与约束，保证缓存复用不破坏检测结果 |
| 消费点：[typst/src/lib.rs:L80](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L80) | `world.track()` | 把宿主递来的世界转成 tracked 形态，`compile_impl` 从此只认 `Tracked<dyn World>` |

（comemo 本体是外部 crate，不在本仓库内；上表是它在 typst 仓库内的全部「锚点」。）

#### 4.3.3 源码精读

**① 收敛判定那一行。**

[crates/typst/src/lib.rs:L145-L162](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L145-L162)
L145 建空约束；L151 用 `track_with(&constraint)` 把内省器「挂在」约束上——本轮布局对内省器的每次查询都被记录；L157 完成布局后，L159 拿**新文档自带**的内省器去 validate：它对 constraint 里记录的每个查询重新求哈希，与记录比对。全部相等 ⇒ 布局自洽 ⇒ break。这就是「两遍编译收敛」的判定核心，也是 u4-l7 的直接前置。

**② Sink 的可追踪写入。**

[crates/typst-library/src/engine.rs:L209-L259](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/engine.rs#L209-L259)
`#[comemo::track] impl Sink` 让 `warn`/`delay`/`value` 这些 `(&mut self, ..) -> ()` 方法也走 tracked 通道。L227-L233 的 `warn` 顺带展示了警告去重：用 `(span, message)` 的 128 位哈希做集合过滤，这就是你重复编译时警告不会刷屏的原因。

**③ World trait 上的那一行属性。**

[crates/typst-library/src/lib.rs:L62-L63](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L62-L63)
`#[comemo::track] pub trait World: Send + Sync`——proc-macro 会为 trait 的每个方法生成带约束记录的 tracked 版本；宿主的 `impl World for ...` 完全无感知，照常写普通方法即可。**这正是「宿主负责缓存数据、编译器负责追踪读取」在代码形态上的落点**：宿主写在 impl 里的缓存（如 `FontStore` 的 `OnceLock`、CLI 的 `LazyLock<FontStore>`）是数据层缓存；`#[comemo::track]` 生成的包装是计算层追踪。两层互不干扰。

#### 4.3.4 代码实践

**实践类型：源码阅读 + 行为观察型。**

1. **实践目标**：体会「宿主数据缓存」与「comemo 计算缓存」是两个独立层次，watch 场景下各自都在工作。
2. **操作步骤**：
   1. 运行 `cargo run -p typst-cli -- watch /tmp/hello.typ`（hello.typ 里放两三段文字即可）。
   2. 记录首次编译耗时（CLI 会打印）。
   3. 保持 watch 运行，改动一个字符保存，观察第二次编译耗时；再改动同一段文字保存，观察第三次。
   4. 对照源码：watch 复用同一个 `SystemWorld`，每轮编译前调 [`reset()`](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L112-L116)（只清文件与时间，字体不动）；编辑走 `Source::edit` 增量重解析（u2-l5）。
3. **需要观察的现象**：第二次及以后的编译显著快于首次；改动幅度越小，加速越明显。
4. **预期结果**：耗时下降来自三层缓存叠加——字体未重扫（宿主 `LazyLock`）、解析仅重做受影响区间（增量重解析）、求值/布局中未受影响的子计算被 comemo 复用。若你的机器上差异不明显（文档太小），可以换一份多页、含目录的文档再试。具体耗时数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`World` 的文档注释说「编译器自己不做缓存」。但 4.2 又说 comemo 会缓存计算。两者矛盾吗？

**参考答案**：不矛盾，它们是两层不同的缓存。comemo 缓存的是**计算结果**（某个函数在给定输入下的输出），而注释说的是**输入数据**（`source`/`file`/`font` 的返回值）——comemo 判断「输入没变」依赖对输入做哈希，如果每次 `source(id)` 都重新读磁盘、重新分配字符串，既慢又可能因为哈希粒度问题白白失效下游缓存。宿主缓存保证同一 `id` 反复索取时返回（逻辑上）同一份数据，comemo 才能放心复用。

**练习 2**：假设去掉 `#[comemo::track]`（trait 与 impl 上都去掉），编译还能得到正确结果吗？损失什么？

**参考答案**：能。track 不改变方法的语义，只生成带记录/重放能力的包装；直接调原方法结果相同。损失的是增量能力：`Tracked<dyn World>` 相关的约束记录不复存在，comemo 无法判断哪些缓存可复用，同时 `compile_impl` 里 `introspector.track_with(&constraint)` + `constraint.validate` 的收敛检测也随之退化——要么每轮全量重排直到结果碰巧稳定，要么需要另想收敛判据。正确性还在，性能与收敛机制崩了。

**练习 3**：`constraint.validate(document.introspector())` 为什么用「新文档的内省器」而不是「上一轮的内省器」来校验？

**参考答案**：要回答的问题是「本轮布局**实际依赖**的信息是否与**提供给它**的信息一致」。constraint 记录的是本轮布局读内省器时看到的查询结果；`document.introspector()` 是本轮产出的文档所隐含的「最新事实」。二者一致，说明布局没有引用「比它所用的更旧/更新」的错位信息，即本轮自洽——下一轮再跑也会得到同样结果，可以停了。若拿上一轮的内省器比，比的只是「两轮之间有没有变」，不能证明本轮内部自洽。

## 5. 综合实践

**综合任务：亲手实现一个 `MiniWorld`，把 Hello World 编译成 `PagedDocument`，并断言产物非空。** 这个任务会同时用到本讲全部三个最小模块：实现 World 契约（4.1）、以 `compile::<T>` 驱动总装（4.2）、并验证默认特性门控下的行为（4.3 的收敛循环在内部自动完成）。

**为什么选 `docs` 包做宿主**：它已经依赖 `typst`、`typst-kit`（启用 `embedded-fonts` 特性）和 `typst-layout`，集成测试目录 `docs/tests/` 会被 Cargo 自动发现，**无需改任何 manifest**。以下代码均为「示例代码」（本仓库中不存在，由你本地新建）。

1. **实践目标**：写出一个不含文件系统、时间固定为「无」的世界，跑通 `typst::compile`，用断言验证结果。

2. **操作步骤**：

   1. 新建文件 `docs/tests/mini_world.rs`，内容如下：

      ```rust
      //! 示例代码：最小 World 集成测试。
      //! 运行：cargo test -p typst-docs --test mini_world -- --nocapture

      use typst::diag::{FileError, FileResult};
      use typst::foundations::{Bytes, Datetime, Duration};
      use typst::syntax::{FileId, RootedPath, Source, VirtualPath, VirtualRoot};
      use typst::text::{Font, FontBook};
      use typst::utils::LazyHash;
      use typst::{Library, LibraryExt, Warned, World};
      use typst_kit::fonts::FontStore;
      use typst_layout::PagedDocument;

      struct MiniWorld {
          library: LazyHash<Library>,
          main: Source,
          fonts: FontStore,
      }

      impl MiniWorld {
          fn new(text: &str) -> Self {
              // 1) 造一个项目根下的虚拟文件身份
              let id = FileId::new(RootedPath::new(
                  VirtualRoot::Project,
                  VirtualPath::new("main.typ").unwrap(),
              ));
              // 2) 内嵌字体（typst-assets 提供），FontStore 负责缓存
              let mut fonts = FontStore::new();
              fonts.extend(typst_kit::fonts::embedded());
              Self {
                  library: LazyHash::new(Library::builder([]).build()),
                  main: Source::new(id, text.to_string()),
                  fonts,
              }
          }
      }

      impl World for MiniWorld {
          fn library(&self) -> &LazyHash<Library> { &self.library }
          fn book(&self) -> &LazyHash<FontBook> { self.fonts.book() }
          fn main(&self) -> FileId { self.main.id() }

          fn source(&self, id: FileId) -> FileResult<Source> {
              if id == self.main.id() {
                  Ok(self.main.clone())
              } else {
                  Err(FileError::NotFound(id.vpath().as_rooted_path().to_owned()))
              }
          }

          fn file(&self, id: FileId) -> FileResult<Bytes> {
              if id == self.main.id() {
                  // Source 实现了 AsRef<str>，可零拷贝转成字节视图
                  Ok(Bytes::from_string(self.main.clone()))
              } else {
                  Err(FileError::NotFound(id.vpath().as_rooted_path().to_owned()))
              }
          }

          fn font(&self, index: usize) -> Option<Font> { self.fonts.font(index) }
          // 我们的世界里没有时间；文档里用 datetime 会报错，这是契约允许的
          fn today(&self, _offset: Option<Duration>) -> Option<Datetime> { None }
      }

      #[test]
      fn mini_world_compiles_hello() {
          let world = MiniWorld::new("= Hello, World!\nMy first world.");
          let Warned { output, warnings } = typst::compile::<PagedDocument>(&world);
          let document = output.expect("compilation failed");
          for warning in &warnings {
              eprintln!("warning: {}", warning.message);
          }
          assert!(!document.pages().is_empty());
          println!("pages: {}", document.pages().len());
      }
      ```

      （`Warned` 经 `typst` 的 glob 再导出直接可用，`document.pages()` 来自 [crates/typst-layout/src/document.rs:L43-L46](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L43-L46)。）

   3. 运行：`cargo test -p typst-docs --test mini_world -- --nocapture`。
   4. 观察通过后做两个破坏性实验并恢复：
      - 实验一：把 `fonts.extend(typst_kit::fonts::embedded());` 注释掉（世界没有任何字体），重新运行，观察编译失败时的错误信息与字体的关系。
      - 实验二：把 `MiniWorld::new` 的文本改成 `#import "missing.typ"`，观察错误如何经过 `FileError::NotFound` 分支变成诊断信息。
   5. 完成后删除 `docs/tests/mini_world.rs`，恢复仓库原状。

3. **需要观察的现象**：测试通过；两个实验分别以「字体不可用」和「文件不存在」类错误失败。

4. **预期结果**：
   - 主测试：`pages: 1`，断言通过。Hello World 文本依赖 `FontStore` 里的内嵌字体（Libertinus Serif 等，见 [crates/typst-kit/src/fonts.rs:L129-L142](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-kit/src/fonts.rs#L129-L142)）完成排版，证明「字体来自世界、不来自编译器」。
   - 实验一：编译失败，错误与缺失字体家族相关（具体消息待本地验证）。
   - 实验二：编译失败，错误指向 `missing.typ` 不存在——你的 `source` 实现第一次真正被调用并返回了 `Err`。

## 6. 本讲小结

- `World` 是编译器与环境的唯一契约：七个方法（library/book/main/source/file/font/today）覆盖编译所需的全部外部输入；类型在 typst-library，实现散布在各宿主（CLI 的 `SystemWorld`、测试的 `TestWorld`、文档站的 `DocWorld` 等）。
- 缓存责任按层划分：**宿主缓存输入数据**（字体可跨编译、源文件每轮清、时间一次编译内固定），**comemo 缓存计算结果**——「世界更懂自己的数据何时变」，这是 trait 文档注释亲口给出的理由。
- `typst::compile::<T>` 的总装节奏是「求值一次 + 布局循环到收敛」：每轮组装一个 `Engine`（world/library/introspector/traced/sink/route 六件套），用 `constraint.validate` 判停，延迟错误最后晋升。
- `Engine` 是传给各阶段的「编译服务包」，`parallelize` 解释了 `World: Send + Sync` 的由来。
- `Routines` 函数指针表 = 源码注释里的「本质上的动态链接」：签名在 typst-library、实现在各阶段 crate、接线在总装 crate 的 `ROUTINES`，经 `Library::builder` 注入，落实了 u1-l3 的 crate 拆分决策。
- `#[comemo::track]` 在本讲范围内的落点：`World` trait、`Traced`/`Sink`/`Route` 的 impl，以及 `compile` 入口的 `world.track()`；约束式记忆化同时撑起增量编译与内省收敛判定。

## 7. 下一步学习建议

- **下一讲 u3-l2《标准库的诞生：Library、global 作用域与 prelude》**：本讲把 `Library` 当黑盒（只用了 `Library::builder([]).build()`），下一讲拆开它——`global()`/`prelude()` 如何注入全部内置定义，`with_inputs`/`with_features` 如何定制。可以先预习 [crates/typst-library/src/lib.rs:L369-L431](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L369-L431)。
- 若你更关心排版：带着本讲的 `Engine` 与收敛循环直接跳读 u4-l1（编译主流程总览）与 u4-l7（内省机制），那里解释 `EmptyIntrospector`、`ITER_NAMES` 与多轮重排的完整语义。
- 若你关心嵌入 Typst：拿本讲的 `MiniWorld` 起步，对照 `tests/src/world.rs` 与 `docs/src/world.rs` 扩展出你要的世界（文件加载、包解析可复用 typst-kit 的 `FileStore` 与 `SystemPackages`，u5-l7 展开）。
