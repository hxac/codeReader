# ROUTINES 表与 crate 切分

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `Routines` 这张「函数指针表」到底是为了解决什么问题而存在的——也就是 **crate 切分（crate splitting）** 与它背后打破循环依赖的「依赖反转」思路。
- 读懂 `typst/src/lib.rs` 顶层那个 `ROUTINES` 静态量，看懂它如何把 `typst-layout` / `typst-html` / `typst-realize` / `typst-eval` 里的真实实现「接线（wire）」进函数指针表。
- 看懂 `routines!` 宏如何用同一份签名清单，同时声明 `Routines` 结构体的所有字段类型。
- 解释为什么 `Routines` 的 `Hash` 实现是空的、`Debug` 只打印 `Routines(..)`，以及这和 `comemo` 增量缓存有什么关系。
- 能够独立追查任意一个 routine 字段（如 `realize`、`layout_frame`）的「接口声明 → 真实实现 → 装配接线」三处源码位置。

本讲是专家层内容，承接 u1-l1（门面与流水线地图）和 u2-l1（`compile_impl` 主流程）。你将看到 typst crate 作为「顶层装配者」所做的一件最关键的架构工作。

## 2. 前置知识

阅读本讲前，请先确认你理解以下概念（前面讲义已建立）：

- **门面（facade）**：`typst` crate 源码极小，主要靠 `pub use typst_library::*` 再导出子 crate 的能力（见 u1-l1）。
- **四步流水线**：解析（typst-syntax）→ 求值（typst-eval）→ 布局（typst-realize / typst-layout / typst-html）→ 导出（u1-l1）。
- **`Engine` 上下文**：贯穿求值与布局的中央对象，持有 `world`、`library`、`introspector`、`sink` 等字段（见 u2-l4）。本讲会反复用到 `engine.library.routines.X` 这种调用形式。
- **`Library`**：Typst 标准库对象，是编译器运行时的「全局环境」，由 `World` 注入（见 u1-l2、u2-l1）。
- **`comemo` 增量缓存**：Typst 用它把纯函数的调用结果缓存起来，缓存键依赖于参数的哈希值。这一点是理解「为什么 `Hash` 要为空」的关键。

如果你对 Rust 的以下特性不熟，这里先给一句话解释：

- **函数指针（`fn` pointer）**：`fn(Args) -> Ret` 是一种类型，表示「接受 `Args` 返回 `Ret` 的函数」。和闭包 `Fn` trait 不同，`fn` 指针是「裸」的、不捕获环境、大小固定、可哈希困难的纯地址。`Routines` 的每个字段就是一个 `fn` 指针。
- **`LazyLock`**：标准库提供的「第一次访问时才初始化」的全局静态量，线程安全。`ROUTINES` 用它实现「按需初始化一次、全程序复用」。
- **高阶生命周期 bound（higher-ranked bound, `for<'a>`）**：当函数指针的返回类型里带有生命周期参数时（如 `realize<'a>` 返回 `Vec<Pair<'a>>`），字段类型要写成 `for<'a> fn(...) -> ...`，表示「对任意生命周期 `'a` 都成立」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`crates/typst/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | typst crate 的全部源码。本讲重点是末尾的 `ROUTINES` 静态量与 `LibraryExt` 装配。 |
| [`crates/typst-library/src/routines.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs) | 定义 `Routines` 结构体、`routines!` 宏，以及它空的 `Hash` / `Debug` 实现。本讲的核心文件。 |
| [`crates/typst-library/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) | `Library` 结构体（含 `routines` 字段）、`LibraryBuilder::from_routines`，以及 `global()` 里对 `rules` / `html_module` 的调用。 |
| [`crates/typst/Cargo.toml`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/Cargo.toml) | typst crate 的依赖清单，能看到它依赖所有「实现侧」子 crate，是「顶层装配者」身份的直接证据。 |
| `crates/typst-library/src/foundations/func.rs` 等 | 「接口侧」调用点：通过 `engine.library.routines.X(...)` 间接调用真实实现。 |
| `crates/typst-realize/src/lib.rs`、`crates/typst-layout/src/...`、`crates/typst-html/src/...` | 「实现侧」：`realize`、`layout_frame`、`register`、`module` 等真实函数所在。 |

> 记住一条贯穿全讲的链路：**接口声明在 typst-library，真实实现在各子 crate，装配接线在 typst crate。** 三者缺一不可。

## 4. 核心概念与源码讲解

### 4.1 为什么需要 Routines——crate 切分与依赖反转

#### 4.1.1 概念说明

先看一个看似简单的问题：**求值（eval）模块要调用布局（layout）函数，布局模块又要调用求值函数——它们之间怎么组织依赖？**

在 Typst 的架构里：

- `typst-library` 是「核心定义层」，定义了 `Engine`、`Content`、`StyleChain`、`Library`、`World`、各种元素（element）等几乎所有「公共语言对象」。
- `typst-eval`、`typst-realize`、`typst-layout`、`typst-html` 是「算法实现层」，它们都要用到核心层定义的对象，因此**都依赖 `typst-library`**。

但这带来一个麻烦：`typst-library` 内部有时**也需要反过来调用**这些算法实现。比如：

- 在 `typst-library` 里实现 `measure()`（测量一段内容尺寸）时，需要真正去**布局**一次 → 要调 `typst-layout::layout_frame`。
- 在 `typst-library` 里实现数学公式归约时，需要**realize**一次 → 要调 `typst_realize::realize`。
- 在 `typst-library` 里调用一个 Typst 闭包时，需要真正**求值** → 要调 `typst_eval::eval_closure`。

如果 `typst-library` 直接 `use typst_layout::layout_frame`，就会形成 **`typst-library` ↔ 子 crate 的循环依赖**，Rust 编译器会直接拒绝。

解决这个问题的经典手法叫**依赖反转（dependency inversion）/ 依赖注入**：

1. 「需要被调用」的函数，在核心层 `typst-library` 里**只声明签名**（以函数指针字段的形式），不引用任何子 crate。
2. 核心层代码通过这些指针**间接调用**：`engine.library.routines.layout_frame(...)`。
3. 真正「填入指针」的工作，交给**依赖所有人的顶层 crate**——也就是 `typst` crate。它依赖 `typst-library` 和所有子 crate，所以在它那里把指针指向真实函数，不存在循环。

源码注释把这件事说得很直白：

> `/// This is essentially dynamic linking and done to allow for crate splitting.`
> （这本质上就是动态链接，目的是实现 crate 切分。）

> **术语解释 —— crate 切分**：把原本可能揉在一个巨型 crate 里的代码，按编译/职责拆成多个独立 crate。好处是编译并行度更高、依赖更清晰、公共边界更小。`Routines` 就是支撑这种切分的关键「接缝」。

#### 4.1.2 核心流程

没有 `Routines` 时，依赖会循环：

```
typst-library ──想调──> typst-layout ──依赖──> typst-library   ✗ 循环！
```

引入 `Routines` 后，箭头方向被「反转」：

```
                    接口声明（函数指针字段）
   typst-library ────────────────────────────> （只定义签名）

   typst-library ──通过指针间接调用──> [函数指针] <──填入实现── typst-layout
                                                                    │
   typst crate（依赖所有人）──────────装配──────────────────────────┘
```

最终的依赖图变成严格的「单向」：

```
typst ──依赖──> { typst-layout, typst-html, typst-realize, typst-eval, typst-library }
typst-layout ──依赖──> typst-library
typst-realize ──依赖──> typst-library
typst-eval    ──依赖──> typst-library
typst-html    ──依赖──> typst-library
typst-library ──依赖──> （不依赖任何实现层 crate，零循环）
```

装配时机是**程序启动时**：`typst` crate 里的 `ROUTINES` 静态量在第一次被访问时（`LazyLock`）把真实函数地址填入；之后这个表被存进 `Library.routines`，伴随整个编译过程。

#### 4.1.3 源码精读

`typst` crate 的 `Cargo.toml` 是「顶层装配者」身份的铁证——它依赖了**所有**实现层子 crate：

[crates/typst/Cargo.toml:L15-L24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/Cargo.toml#L15-L24) —— typst crate 同时依赖 `typst-eval`、`typst-html`、`typst-layout`、`typst-library`、`typst-realize`，因此它是唯一「能同时看到接口与实现」的 crate，装配工作只能在这里完成。

而 `ROUTINES` 上方的文档注释，用一句话点明了整张表的存在意义：

[crates/typst/src/lib.rs:L307-L311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L307-L311) ——「Defines implementation of various Typst compiler routines as a table of function pointers. This is essentially dynamic linking and done to allow for crate splitting.」

> 关键结论：`Routines` 不是为了性能、不是为了灵活配置，**它的唯一目的是让 typst 仓库能把代码切成多个独立 crate 而不产生循环依赖**。理解了这一点，本讲其余内容都顺理成章。

#### 4.1.4 代码实践

**实践目标**：亲手验证「依赖单向、无循环」这条结论。

**操作步骤**：

1. 打开 `crates/typst/Cargo.toml`，列出 typst 的全部依赖。
2. 打开 `crates/typst-layout/Cargo.toml`、`crates/typst-realize/Cargo.toml`、`crates/typst-html/Cargo.toml`、`crates/typst-eval/Cargo.toml`，确认它们各自依赖 `typst-library`。
3. 打开 `crates/typst-library/Cargo.toml`，确认它**没有**依赖 `typst-layout` / `typst-realize` / `typst-html` / `typst-eval`。

**需要观察的现象**：依赖箭头严格「向下」收敛到 `typst-library`，没有任何一条从 `typst-library` 指回实现层。

**预期结果**：你会得到一张和本讲 4.1.2 节一致的 DAG（有向无环图）。如果 `typst-library` 出现了对实现层 crate 的依赖，那就是循环依赖 bug。

> 如果无法在本地运行 `cargo tree`，可改为纯文件阅读：上面三步只读 `Cargo.toml` 即可得出结论，无需编译。

#### 4.1.5 小练习与答案

**练习 1**：假设团队决定把 `typst-layout` 并回 `typst-library`（不再切分），`Routines` 是否还有必要保留 `layout_frame` 字段？

**参考答案**：若不再切分，`typst-library` 可以直接 `use typst_layout::layout_frame` 调用，`layout_frame` 这个函数指针字段就不再必要。这正是「字段存在 ⟺ 该实现被切分到了别的 crate」的体现。

**练习 2**：为什么「装配」工作必须放在 `typst` crate，而不能放在 `typst-library` 自己里面？

**参考答案**：因为 `typst-library` 不能依赖实现层 crate（会循环）。只有 `typst` crate 同时依赖 `typst-library` 和所有实现层 crate，才具备「把指针指向真实函数」的资格。

---

### 4.2 `routines!` 宏与 `Routines` 结构体

#### 4.2.1 概念说明

`Routines` 是一个**全部由函数指针字段组成的结构体**。它有 9 个字段：`rules`、`eval_string`、`eval_closure`、`realize`、`layout_frame`、`html_module`、`html_mathml_body`、`html_span_filled`（外加若干）。每个字段长这样：

```rust
pub realize: for<'a> fn(...) -> SourceResult<Vec<Pair<'a>>>,
```

如果手写，每加一个 routine，就要同时维护「字段名 + 参数列表 + 返回类型 + 文档注释」，容易写错、容易和真实函数签名对不上。于是 typst 用一个宏 `routines!`，**用一份「类函数签名」清单同时生成字段定义**，让声明更紧凑、更不容易和真实实现脱节。

> **直觉**：把 `Routines` 想成一张「函数槽位表」，宏帮你一次性把所有槽位的形状（参数类型、返回类型）刻好。

#### 4.2.2 核心流程

宏的输入是一组形如 `fn name(args) -> Ret` 的条目，宏对每一条生成：

1. 一个 `pub` 字段，类型为 `fn(args) -> Ret`；
2. 若条目带生命周期（如 `realize<'a>`），字段类型前加上 `for<'a>`（高阶 bound），写成 `for<'a> fn(...) -> ...`。

伪代码展开：

```
输入： fn layout_frame(engine: &mut Engine, ...) -> SourceResult<Frame>
       fn realize<'a>(...) -> SourceResult<Vec<Pair<'a>>>

展开： pub layout_frame: fn(&mut Engine, ...) -> SourceResult<Frame>,
       pub realize:      for<'a> fn(...) -> SourceResult<Vec<Pair<'a>>>,
```

宏同时还生成了两个 impl：空的 `Hash` 与简化的 `Debug`（见 4.5 节）。

#### 4.2.3 源码精读

宏定义在 `routines.rs` 顶部。它的匹配模式 `fn $name:ident $(<$($time:lifetime),*>)? ($($args:tt)*) -> $ret:ty` 同时捕获了「可选的生命周期参数」「参数 token 树」「返回类型」，再用 `$(for<$($time),*>)?` 在字段类型前按需插入 `for<...>`：

[crates/typst-library/src/routines.rs:L20-L48](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L20-L48) —— `routines!` 宏定义。注意第 34 行 `pub $name: $(for<$($time),*>)? fn ($($args)*) -> $ret` 正是「字段 = 函数指针类型」的生成处；第 38–40 行的空 `Hash`、第 42–46 行的 `Routines(..)` `Debug` 也由同一个宏生成。

紧接着宏对自身做了一次调用，列出全部 routine 条目，这就是 `Routines` 的全部字段：

[crates/typst-library/src/routines.rs:L50-L114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L50-L114) —— `routines! { ... }` 调用，声明了 `rules` / `eval_string` / `eval_closure` / `realize` / `layout_frame` / `html_module` / `html_mathml_body` / `html_span_filled` 等全部槽位。注意 `realize<'a>`（L82）和 `html_mathml_body<'a>`（L104）带生命周期，会生成 `for<'a> fn(...)` 字段。

> 一个细节：`rules` 字段类型是 `fn() -> NativeRuleMap`（L52），它本身不带参数——因为「创建内置 show 规则表」是一次性自包含操作，所需依赖（`typst_layout::register` / `typst_html::register`）以闭包捕获的形式包在里面（见 4.3 节）。

#### 4.2.4 代码实践

**实践目标**：亲手把一条宏条目「展开」成字段，验证宏的正确性。

**操作步骤**：

1. 看 `routines!` 调用里的 `fn layout_frame(engine: &mut Engine, content: &Content, locator: Locator, styles: StyleChain, region: Region) -> SourceResult<Frame>`（L92-L98）。
2. 按照 4.2.2 的规则，手写出它对应的字段类型。
3. 对照 4.3 节里 `ROUTINES` 中 `layout_frame: typst_layout::layout_frame` 的赋值，确认 `typst_layout::layout_frame` 的真实签名和这个字段类型完全一致。

**需要观察的现象**：手写出的字段 `pub layout_frame: fn(&mut Engine, &Content, Locator, StyleChain, Region) -> SourceResult<Frame>` 与真实函数签名逐字匹配，编译器才能把函数名隐式转换成 `fn` 指针赋值。

**预期结果**：两边签名完全一致。若不一致，`ROUTINES` 那一行会编译失败——这也是宏「用一份签名同时约束声明与实现」的价值所在。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `realize<'a>` 生成的字段类型前面要加 `for<'a>`，而 `layout_frame` 不用？

**参考答案**：`realize` 的返回类型 `Vec<Pair<'a>>` 里带有生命周期 `'a`，这个 `'a` 与参数中的 `&'a Content` / `StyleChain<'a>` 绑定，是「函数指针类型本身」的一部分，必须用高阶 bound `for<'a>` 表达「对任意 `'a` 成立」。`layout_frame` 的签名里没有这样的「跨参数与返回值」的生命周期变量，所以是普通 `fn(...)`。

**练习 2**：如果新增一个 routine，需要在哪些地方改动？

**参考答案**：(a) 在 `routines.rs` 的 `routines! { ... }` 里加一条签名；(b) 在 `typst/src/lib.rs` 的 `ROUTINES` 静态量里加一行 `name: 真实函数,`；(c) 写好真实实现（通常在某个子 crate）。宏会自动生成字段，无需手改结构体。

---

### 4.3 ROUTINES 静态量与接线装配

#### 4.3.1 概念说明

接口（`Routines` 结构体）声明在 `typst-library`，但它的「实例」——真正填好指针的表——只能由 `typst` crate 创建。这个唯一实例就是全局静态量 `ROUTINES`。

`ROUTINES` 的每个字段都指向一个**真实函数**：

- `rules` → 一个闭包，内部调用 `typst_layout::register` 和 `typst_html::register`，把两个 crate 的内置 show 规则塞进同一张 `NativeRuleMap`。
- `eval_string` → `typst_eval::eval_string`
- `eval_closure` → `typst_eval::eval_closure`
- `realize` → `typst_realize::realize`
- `layout_frame` → `typst_layout::layout_frame`
- `html_module` → `typst_html::module`
- `html_mathml_body` → `typst_html::html_mathml_body`
- `html_span_filled` → `typst_html::html_span_filled`

这张表随后被存入 `Library.routines`（一个 `&'static Routines` 引用），随 `Library` 一路传到 `Engine.library`，最终让任何一段 `typst-library` 代码都能通过 `engine.library.routines.X(...)` 触达真实实现。

> **直觉**：`ROUTINES` 是一张「电话簿」——`typst-library` 里只记了「分机号（字段名）」，`typst` crate 在启动时把每个分机号接上「真实的人（函数）」。

#### 4.3.2 核心流程

装配的数据流分四步：

1. **构造**：程序首次访问 `ROUTINES` 时，`LazyLock` 调用初始化闭包，逐字段填入真实函数地址。
2. **注入 Builder**：`Library::builder()`（即 `LibraryExt::builder`）调用 `LibraryBuilder::from_routines(&ROUTINES)`，把这张表的引用交给 builder。
3. **存入 Library**：`LibraryBuilder::build()` 产出 `Library { routines: self.routines, ... }`，于是 `Library.routines` 就是 `&'static ROUTINES`。
4. **使用**：编译期间，`Engine.library.routines.X(...)` 即是对真实函数的间接调用。

由于 `ROUTINES` 是 `&'static`，`Library` 持有的也只是这个引用，整个程序生命周期内只有这一份实例，零分配、零拷贝。

#### 4.3.3 源码精读

`ROUTINES` 静态量本身——注意 `rules` 字段用的是闭包 `|| { ... }` 而非裸函数名，因为它要把两个 crate 的 `register` 组合起来：

[crates/typst/src/lib.rs:L311-L325](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L311-L325) —— `ROUTINES` 的全部字段赋值。`rules`（L312-L317）是一个闭包，先建空 `NativeRuleMap`，再分别调 `typst_layout::register` 和 `typst_html::register` 把两边的内置规则都注册进去；其余字段（L318-L324）则是「字段名 = 子 crate 函数名」的直接映射。

注入入口——`LibraryExt::builder` 把 `ROUTINES` 交给 builder：

[crates/typst/src/lib.rs:L297-L305](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L297-L305) —— `LibraryExt for Library` 的 `builder()` 调用 `LibraryBuilder::from_routines(&ROUTINES)`，这是 typst crate 与 typst-library 之间的唯一「交接点」。

builder 侧保存这个引用：

[crates/typst-library/src/lib.rs:L195-L204](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L195-L204) —— `LibraryBuilder::from_routines` 把 `&'static Routines` 存进 builder，`build()` 时（L230 附近）原样写入 `Library.routines`。

`Library` 结构体持有 routines 字段：

[crates/typst-library/src/lib.rs:L166-L183](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L166-L183) —— `Library` 第一个字段就是 `pub routines: &'static Routines`（L169）。它和 `global` / `math` / `styles` / `rules` / `std` / `features` 并列，是标准库的「根配置」之一。

> 注意区分两个 `rules`：`Library.rules`（L178，`NativeRuleMap` 实例）是 `ROUTINES.rules`（L312，`fn() -> NativeRuleMap`）**调用的产物**。前者是「已建好的规则表」，后者是「知道如何建规则表的函数」。在 `build()` 里能看到这一步：`rules: (self.routines.rules)()`（L230）。

#### 4.3.4 代码实践

**实践目标**：跟踪 `rules` 这个 routine 的完整装配链路。

**操作步骤**：

1. 看 [`crates/typst/src/lib.rs:L312-L317`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L312-L317)：`ROUTINES.rules` 闭包内调用了 `typst_layout::register` 与 `typst_html::register`。
2. 跳到实现：[`crates/typst-layout/src/rules.rs:L39`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/rules.rs#L39) 与 [`crates/typst-html/src/rules.rs:L39`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/rules.rs#L39)，确认两个 `register` 都把各自 crate 的内置 show 规则写入传入的 `&mut NativeRuleMap`。
3. 回到 [`crates/typst-library/src/lib.rs:L230`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L230)：`build()` 里 `rules: (self.routines.rules)()` 触发闭包，把 layout + html 的规则合并成一张表存入 `Library.rules`。

**需要观察的现象**：一张 `NativeRuleMap` 同时承载了 paged 目标（来自 `typst_layout::register`）和 html 目标（来自 `typst_html::register`）的内置规则。

**预期结果**：你能画出 `register(layout) + register(html) → 闭包 → ROUTINES.rules → LibraryBuilder.build() → Library.rules` 这条「实现 → 装配 → 使用」链路图。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rules` 用闭包 `|| { ... }`，而其它字段用裸函数名 `typst_realize::realize`？

**参考答案**：`rules` 需要**组合**两个 crate 的 `register` 调用（先 layout 后 html）并构造中间变量 `NativeRuleMap`，单个函数名无法表达这种「多步逻辑」。其余 routine 是「一对一」地指向某个已存在的函数，函数名本身就是合法的 `fn` 指针，无需包装。注意：闭包要能赋给 `fn() -> NativeRuleMap` 字段，它必须**不捕获任何环境**（捕获了就会变成 `Fn` 而非 `fn`，无法转换）——这里闭包只用到外部 crate 的函数，确实零捕获。

**练习 2**：`Library.routines` 是 `&'static Routines`。这个 `'static` 生命周期对编译器/缓存有什么好处？

**参考答案**：`ROUTINES` 由 `LazyLock` 持有，存活整个程序，所以引用是 `'static`。这意味着 `Library` 只需复制一个指针大小，无需拥有/克隆函数表，也无需生命周期参数污染 `Library` 的泛型签名。

---

### 4.4 接口的另一端——求值/布局中的指针调用与完整链路

#### 4.4.1 概念说明

到目前为止我们看了「接口声明」和「装配接线」，还差最后一环：**`typst-library` 内部的代码究竟是怎么通过这张表调用真实实现的？**

答案是统一的句式：

```rust
(engine.library.routines.<字段名>)( 实参... )
```

也就是说，先从 `Engine` 拿到 `library`，再取它的 `routines` 表，再取出某个函数指针字段，最后像调用普通函数一样调用它。对编译器而言，这是一次**间接调用（indirect call）**——通过指针跳转到真实函数。虽然在 typst 的语境里这点开销可以忽略（这些 routine 都是重活，函数指针本身的开销远小于内部工作量），但它确实是「动态链接」在语义上的体现。

这种写法让 `typst-library` 在**不依赖任何实现层 crate** 的前提下，依然能驱动完整的求值与布局。

#### 4.4.2 核心流程

一个典型调用（以「在 `measure()` 里布局一次」为例）的执行流：

```
typst-library: measure.rs
   engine.library.routines.layout_frame(engine, &content, locator, styles, pod)
        │  (函数指针间接调用)
        ▼
   ROUTINES.layout_frame  ==  typst_layout::layout_frame   （typst crate 装配）
        │
        ▼
typst-layout: flow/mod.rs::layout_frame   （真实实现）
```

其它 routine 的调用形态完全一致，只是字段名和实参不同。

#### 4.4.3 源码精读

四个典型调用点，覆盖了 `eval_string` / `eval_closure` / `realize` / `layout_frame`：

`measure()` 通过 `layout_frame` 真正布局一次以测量尺寸：

[crates/typst-library/src/layout/measure.rs:L96-L100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/measure.rs#L96-L100) —— `(engine.library.routines.layout_frame)(engine, &content, locator, styles.chain(&style), pod)`。这里 `typst-library` 完全不知道 `typst_layout` 的存在，却能调用到它的 `layout_frame`。

调用一个 Typst 闭包时，走 `eval_closure`：

[crates/typst-library/src/foundations/func.rs:L352-L356](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/func.rs#L352-L356) —— `FuncInner::Closure(closure) => (engine.library.routines.eval_closure)(self, closure, engine.world, ...)`。原生函数（`FuncInner::Native`）走另一分支，只有用户定义的闭包才需要「真正求值」，因此借指针跳到 `typst_eval::eval_closure`。

把字符串当 Typst 代码求值（`eval`、`repr` 等场景）走 `eval_string`：

[crates/typst-library/src/foundations/mod.rs:L305-L309](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L305-L309) —— `(engine.library.routines.eval_string)(engine.world, engine.library, ..., string, ...)`。

数学公式归约时走 `realize`：

[crates/typst-library/src/math/ir/resolve.rs:L132-L136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L132-L136) —— `(self.engine.library.routines.realize)(RealizationKind::Math, self.engine, ...)`。

真实实现侧（节选两个），证明指针最终落到了子 crate：

[crates/typst-layout/src/flow/mod.rs:L42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/flow/mod.rs#L42) —— `pub fn layout_frame(engine: &mut Engine, ...)`，即 `ROUTINES.layout_frame` 指向的真实函数。

[crates/typst-realize/src/lib.rs:L43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L43) —— `pub fn realize<'a>(kind: RealizationKind, ...)`，即 `ROUTINES.realize` 指向的真实函数。

> 这就是「接口—实现—装配」三处的完整证据：声明在 `routines.rs`、实现分别在 `typst-layout` / `typst-realize`、装配在 `ROUTINES`。

#### 4.4.4 代码实践（本讲的综合代码实践之一）

**实践目标**：选 `layout_frame` 字段，亲手画出它的「接口—实现—装配」三处源码位置。

**操作步骤**：

1. **接口侧**：打开 [`crates/typst-library/src/routines.rs:L92-L98`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L92-L98)，记录 `layout_frame` 的声明签名。
2. **调用侧**：打开 [`crates/typst-library/src/layout/measure.rs:L96`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/measure.rs#L96)（以及 [`model/outline.rs:L788`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/outline.rs#L788)、[`visualize/tiling.rs:L287`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/tiling.rs#L287)），看 `typst-library` 如何通过 `engine.library.routines.layout_frame(...)` 间接调用。
3. **实现侧**：打开 [`crates/typst-layout/src/flow/mod.rs:L42`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/flow/mod.rs#L42)，确认真实函数 `layout_frame` 存在且签名与接口一致。
4. **装配侧**：打开 [`crates/typst/src/lib.rs:L321`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L321)，看到 `layout_frame: typst_layout::layout_frame` 这行接线。

**需要观察的现象**：四处源码分属三个不同 crate（`typst-library` × 2、`typst-layout`、`typst`），但通过函数指针表被串成一条完整调用链。

**预期结果**：得到一张包含四个节点、三条边的链路图：

```
声明(routines.rs) ──装配(ROUTINES)──> 实现(typst-layout/flow) <──调用(measure.rs 等)
```

> 待本地验证：若你本地能编译，可在 `layout_frame` 实现入口加一行 `eprintln!("layout_frame called")`，运行任意含 `measure` 的 Typst 文档，观察这条间接调用是否真的被触发。

#### 4.4.5 小练习与答案

**练习 1**：`engine.library.routines.layout_frame(...)` 是直接调用还是间接调用？对性能影响大吗？

**参考答案**：是间接调用（通过函数指针跳转）。在 typst 场景里影响可忽略——`layout_frame` 内部要做大量排版工作，单次指针跳转的开销相对于排版工作量是纳秒对毫秒级别。typst 选择这种结构是为了**架构解耦（crate 切分）**，而非为了性能。

**练习 2**：`func.rs` 里 `FuncInner::Native` 分支为何不需要走 `routines`，而 `FuncInner::Closure` 需要？

**参考答案**：原生函数的实现就在 `typst-library` 内部（由 `#[func]` 宏生成的 Rust 函数），可以直接调用，无需跨 crate。闭包的「真正求值」逻辑在 `typst-eval` crate，`typst-library` 不能直接依赖它，所以必须借 `routines.eval_closure` 间接跳转。

---

### 4.5 Hash 为空与 Debug 的细节——为 comemo 缓存让路

#### 4.5.1 概念说明

`Routines` 还有两个看似古怪的实现：

- `impl Hash for Routines { fn hash<H>(&self, _: &mut H) {} }` —— **空的哈希**，任何 `Routines` 都哈希出同样的值。
- `impl Debug for Routines { fn fmt(&self, f) { f.pad("Routines(..)") } }` —— **永远只打印 `Routines(..)`**。

要理解它们，必须回到 comemo 缓存。`Library` 在编译期被包成 `LazyHash<Library>`（参见 `eval_string` 签名里的 `library: &LazyHash<Library>`），并作为 comemo 缓存的键的一部分。`LazyHash` 要求 `Library: Hash`，而 `Library` 含一个 `routines: &'static Routines` 字段，因此 **`Routines` 必须实现 `Hash`**。

但「哈希一个函数指针表」既无意义也有风险：

- 函数指针的地址在不同编译产物里不稳定（同一份代码重编译地址可能变），拿它当缓存键会破坏可复现性。
- `ROUTINES` 是**全程序唯一的 `'static` 单例**，它的「身份」永远不变——既然不变，把它哈希成常数是最正确的选择：它永远不会成为「缓存失效」的来源。

`Debug` 同理：函数指针没有有意义的调试输出，打印一串地址只是噪声，不如统一显示 `Routines(..)`。

> **关键结论**：「`Hash` 为空」不是偷懒，而是经过深思的：**`Routines` 是进程级单例，它的存在不参与缓存失效判定**。真正决定缓存失效的是 `Library` 的其它字段（`global` / `styles` / `rules` / `features`）。

#### 4.5.2 核心流程

comemo 缓存键的构成（概念性描述）：

```
缓存键 = hash( 函数参数..., library, ... )
         其中 library = LazyHash<Library>
              Library 含 routines 字段 → 需 hash Routines
              但 hash(Routines) == 常数（空实现）
```

因此当且仅当 `Library` 的**实质内容**（规则、样式、特性开关）变化时，`LazyHash<Library>` 的哈希才变化，缓存才失效；`routines` 字段永远贡献 0 字节的影响。

#### 4.5.3 源码精读

空的 `Hash` 与 `Routines(..)` 的 `Debug`，都由 `routines!` 宏统一生成：

[crates/typst-library/src/routines.rs:L38-L46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L38-L46) —— `fn hash<H: Hasher>(&self, _: &mut H) {}` 完全不写入任何字节（L39）；`f.pad("Routines(..)")` 让 Debug 输出恒为 `Routines(..)`（L44）。这两个 impl 与 `Routines` 结构体定义（L31-L36）在同一个宏展开里，保证只要用了 `routines!`，就一定带这两个实现，不会漏。

可对照 `Library` 里 routines 字段的声明，理解它如何成为 `Hash` 要求的一部分：

[crates/typst-library/src/lib.rs:L167-L169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L167-L169) —— 注释再次强调「table of function pointers」，字段类型 `&'static Routines` 必须满足 `Library` 派生的 `Hash` 约束。

#### 4.5.4 代码实践

**实践目标**：理解「`Routines` 不参与缓存失效」这一设计的后果。

**操作步骤**：

1. 假设你给 `Routines` 新增了一个字段 `foo`，并在 `ROUTINES` 里把 `foo` 指向函数 A。
2. 思考：现在你把 `foo` 改指向函数 B（同一进程内重编译 `typst` crate 后重新运行），对一次全新编译的结果有没有影响？

**需要观察的现象**：因为是「全新进程」，comemo 缓存本来就是空的，所以指向 A 还是 B 当然会影响结果。但**在同一个长生命周期进程里**（比如语言服务器 LSP），`ROUTINES` 在进程启动时就固定了，运行期间无法更换——这正好和「`Hash` 为空 → 它不触发缓存失效」自洽：既然运行期间不变，就无需靠哈希变化来失效缓存。

**预期结果**：体会到「单例 + 空 Hash」是一对自洽的设计：**对象恒定不变 ⟹ 哈希成常数 ⟹ 不污染缓存键**。

> 待本地验证：如果你尝试在运行期「换掉」`ROUTINES.foo` 的指向，会发现做不到——它是不可变 `LazyLock`。这从机制上保证了空 Hash 的安全性。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `Routines` 的 `Hash` 改成「正常派生」（`#[derive(Hash)]`，逐字段哈希函数指针地址），会出什么问题？

**参考答案**：函数指针地址在不同编译/构建里不稳定，会导致同样的 `Library` 内容在不同构建里哈希不同，破坏 comemo 缓存的可复现性；而且同一进程内 `ROUTINES` 本就不变，逐字段哈希只是徒增开销、不会带来任何缓存失效信号。所以空 Hash 是更优解。

**练习 2**：`Debug` 打印 `Routines(..)` 而不是展开字段，主要为了什么？

**参考答案**：函数指针的 Debug 输出是裸地址，对人没有意义且会随构建变化，刷在日志/错误信息里只会造成困扰。统一打印 `Routines(..)` 既表明「这里是一个 Routines」，又不泄漏无意义的地址噪声。

---

## 5. 综合实践

把本讲全部知识串起来，完成下面这个**全链路追踪任务**。

**任务**：选择 `realize` 这个 routine，写出它从「被 typst-library 调用」到「在 typst-realize 中真正执行」的完整四段证据，并解释为什么这条链路必须经过 `typst` crate 装配而不能直连。

**要求产出**：

1. **接口声明**：在 [`crates/typst-library/src/routines.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs) 中找到 `realize` 的签名（注意它带 `'a` 生命周期），抄下它的字段类型，说明为什么会生成 `for<'a> fn(...)`。
2. **调用点**：在 `typst-library` 中找到至少一处通过 `engine.library.routines.realize(...)` 调用它的代码（提示：数学归约 [`math/ir/resolve.rs:L132`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L132)）。
3. **真实实现**：在 [`crates/typst-realize/src/lib.rs:L43`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L43) 确认 `realize` 函数存在且签名与接口一致。
4. **装配接线**：在 [`crates/typst/src/lib.rs:L320`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L320) 找到 `realize: typst_realize::realize` 这行。
5. **架构解释**：用一段话说明——如果让 `typst-library` 直接 `use typst_realize::realize`，依赖图会怎样？为什么必须用 `Routines` + `ROUTINES` 这套机制来「反转」依赖？

**进阶（可选）**：把 `realize` 的链路画成一张含四个节点（声明 / 调用 / 实现 / 装配）的有向图，标注每个节点所在的 crate，直观呈现「接口在 library、实现在子 crate、装配在 typst」的三明治结构。

完成本任务后，你应该能对 typst 仓库里**任意一个** routine（不只是 `realize`）独立完成同样的追踪——这正是本讲想赋予你的能力。

## 6. 本讲小结

- `Routines` 是一张**函数指针表**，它存在的唯一目的是 **crate 切分**：让 `typst-library` 能在不依赖任何实现层 crate 的前提下，间接调用 `typst-eval` / `typst-realize` / `typst-layout` / `typst-html` 的实现，从而打破循环依赖。
- 依赖方向被「反转」：实现层 crate 依赖 `typst-library`；`typst-library` 只持有指针；由同时依赖所有人的 `typst` crate 在启动时通过 `ROUTINES` 静态量完成装配。
- `routines!` 宏用一份「类函数签名」清单同时生成 `Routines` 的所有字段类型，带生命周期的条目（如 `realize<'a>`）会生成 `for<'a> fn(...)` 字段。
- `ROUTINES` 是 `LazyLock` 全程序单例，经 `LibraryExt::builder` → `LibraryBuilder::from_routines` → `Library.routines: &'static Routines` 注入，最终在 `engine.library.routines.X(...)` 处被间接调用。
- 调用句式统一为 `(engine.library.routines.<字段>)(实参...)`，覆盖 `eval_string`（字符串求值）、`eval_closure`（闭包调用）、`realize`（归约）、`layout_frame`（布局）等场景。
- `Hash` 实现为空、`Debug` 打印 `Routines(..)`，是因为 `Routines` 是进程级 `'static` 单例，恒定不变，不应参与 comemo 缓存的失效判定——「单例 + 空 Hash」是一对自洽设计。

## 7. 下一步学习建议

- **下一讲 u3-l3（Library 构建与特性开关）**：本讲看到 `LibraryBuilder::from_routines` 是 builder 的起点，下一讲会完整展开 `LibraryBuilder` 的 `with_inputs` / `with_features` / `build`，以及 `Features` / `Feature` 开关如何条件注册 `html` 模块（正是 `ROUTINES.html_module` 被使用的地方，见 [`lib.rs:L349`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L349)）。
- **下一讲 u3-l4（诊断处理）**：理解 `compile` 出口处的 `deduplicate` 与延迟错误提升。
- **延伸阅读**：
  - 想看「实现侧」如何使用 `Engine`，可读 `typst-realize/src/lib.rs` 与 `typst-layout/src/flow/mod.rs` 的函数体。
  - 想理解 `rules` 注册的产物 `NativeRuleMap` 如何被消费，可结合 u3-l1（`TargetElem` 与按目标分支的 show 规则）一起读。
  - 对 comemo 缓存键感兴趣，可回到 u2-l4 复习 `Engine` 字段的 `Tracked` / `Protected` 包装与缓存失效机制。
