# Routines 与 crate 分离机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Routines` 这张「函数指针表」是什么、为什么 Typst 需要它。
- 解释为什么 `typst-library` 的 `Cargo.toml` 里**没有** `typst-eval`、`typst-realize`、`typst-layout`、`typst-html` 这些行为 crate，却仍能在编译期调用它们。
- 看懂 `routines!` 宏如何把一串 `fn` 声明展开成 `Routines` 结构体、`Hash` 与 `Debug` 实现。
- 识别 `SpanMode`、`RealizationKind`、`FragmentKind`、`Arenas`、`Pair` 这些「附属类型」在例程签名里扮演的角色。
- 自己追踪一条 `(engine.library.routines.X)(...)` 调用链，并说明它如何打破循环依赖。

## 2. 前置知识

本讲是编译环境单元（u5）的第四篇，承接 u5-l1（`World` trait）建立的「`Library` 持有 `routines` 字段」印象。开始前，请确保你大致了解：

- **Rust 的 crate 依赖是有向无环图（DAG）**：如果 `A` 依赖 `B`，`B` 就不能再依赖 `A`，否则编译器报「circular dependency」。这是本讲要解决的核心矛盾。
- **函数指针（`fn` pointer）**：与闭包（`Fn` trait）不同，裸函数指针 `fn(Args) -> Ret` 是 `Copy`、`'static`、无状态的，可以被存进结构体字段、被哈希、被像普通值一样传来传去。
- **依赖注入 / 动态链接的直觉**：一个模块只「声明」自己需要哪些能力（接口），由上层在运行期把「具体实现」塞进去。Typst 把这套思路用在编译器内部，源码原话是 *「This is essentially dynamic linking」*（这本质上是动态链接）。
- u5-l1 里 `Library` 的七个字段：其中第一个 `routines: &'static Routines` 就是本讲主角。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/routines.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs) | 定义 `routines!` 宏、`Routines` 结构体，以及只为支撑例程签名而存在的附属类型（`SpanMode`、`RealizationKind`、`FragmentKind`、`Arenas`、`Pair`）。 |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) | `Library.routines` 字段、`LibraryBuilder::from_routines`、`build()` 与 `global()` 中对例程的调用。 |
| [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/Cargo.toml) | 依赖清单——证据所在：它**不**列出任何行为 crate。 |
| `crates/typst/src/lib.rs`（**本 crate 之外**） | 行为 crate 的真正实现被装配进 `static ROUTINES`，由顶层 `typst` crate 提供。 |

> 提示：最后那个文件不在 `typst-library` 里。本讲会跨出 crate 边界看一眼「实现侧」，但这只是为了讲清机制；可引用的「接口侧」全部在 `typst-library` 内。

## 4. 核心概念与源码讲解

### 4.1 Routines：一张函数指针表

#### 4.1.1 概念说明

回看 u1-l1 的核心结论：`typst-library` 身兼两职——既是标准库，又集中了编译器的**核心类型定义**（`Value`/`Content`/`Func`/`Module`……）。而编译器的**行为**（求值 `eval`、收敛 `realize`、排版 `layout`、导出 HTML）被刻意拆到了独立的 crate（`typst-eval`、`typst-realize`、`typst-layout`、`typst-html`）。

这里有一个天然矛盾：

- 行为 crate（如 `typst-eval`）需要**用到** `Value`、`Content` 这些类型，所以它们要**依赖** `typst-library`。
- 但标准库里的某些函数（如用户写的 `#eval("...")`、`measure()`）在执行时又需要**回调**行为 crate 的实现（去真正求值、去真正排版）。

如果 `typst-library` 直接 `use typst_eval;`，那么：

```
typst-library ──依赖──▶ typst-eval ──依赖──▶ typst-library   ❌ 循环依赖
```

Rust 编译器会直接拒绝。

解决办法就是 **`Routines`**：一张只装「函数指针」的表。`typst-library` 只定义这张表的**形状**（每个槽位的函数签名），**不**提供实现；真正的实现由最顶层的 `typst` crate（它同时依赖 `typst-library` 和所有行为 crate）在程序启动时填进去，并以 `&'static Routines` 的形式长期挂在 `Library` 上。

```
            ┌─────────── 顶层 typst crate（装配者）───────────┐
            │  static ROUTINES = Routines {                   │
            │      eval_string: typst_eval::eval_string,      │
            │      realize:      typst_realize::realize,      │
            │      layout_frame: typst_layout::layout_frame,  │
            │      ...                                         │
            │  }                                               │
            └        │ 依赖            │ 依赖            ─────┘
        ┌────────────▼───      ┌───────▼─────────┐
        │ typst-library  │      │ typst-eval 等   │
        │ （定义 Routines │◀─依赖│ （提供实现）     │
        │   的形状/接口） │      │                 │
        └────────────────┘      └─────────────────┘
```

依赖方向因此变成单向无环：`typst-eval` 等行为 crate → `typst-library`；顶层 `typst` → 两者。循环被打破。

> 类比：这和操作系统的**动态链接**、面向对象里的**依赖注入**、Rust 的 **trait object（`dyn Trait`）虚函数表**是同一类思想——把「调用者」和「实现者」在编译期解耦，在运行期接线。区别在于 `Routines` 用的是最轻量的**裸函数指针**，没有 vtable、没有泛型单态化膨胀，整张表 `Copy` 且 `'static`。

#### 4.1.2 核心流程

一个例程从「定义」到「被调用」的生命周期：

1. **声明形状**：在 `routines.rs` 里用 `routines!` 宏写一行 `fn eval_string(...) -> SourceResult<Value>`，宏展开成 `Routines` 结构体的一个 `pub` 字段，类型是 `fn(...) -> ...`。
2. **挂载到 Library**：`Library` 的第一个字段是 `routines: &'static Routines`（见 lib.rs:169）。`LibraryBuilder::from_routines(&ROUTINES)` 接收外部传入的表。
3. **装配实现**：顶层 `typst` crate 持有 `static ROUTINES: LazyLock<Routines>`，把每个槽位指向真实的行为 crate 函数。
4. **运行期分发**：标准库里的某处（例如 `eval()` 函数体）写 `(engine.library.routines.eval_string)(参数...)`——取出函数指针，像普通函数一样调用它。此时控制权就跨进了 `typst-eval`。
5. **返回**：行为 crate 算完，按签名约定返回 `SourceResult<Value>` 等类型，控制权回到 `typst-library`。

伪代码：

```text
# 用户在 Typst 里写 #eval("1 + 2")
foundations::eval() {                         # typst-library 内
    ...
    (engine.library.routines.eval_string)(    # 取函数指针
        engine.world, engine.library, sink,
        &text, SpanMode::Uniform(span), mode, scope,
    )
    # ↑ 这一跳，实际执行的是 typst_eval::eval_string
}
```

#### 4.1.3 源码精读

**① Library 持有例程表。** `Routines` 是 `Library` 的第一个字段，类型是 `&'static Routines`（进程级常量，永不改变）：

[src/lib.rs:166-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L166-L170) — `Library` 结构体，`routines` 字段注释明说它就是「一张函数指针表，用于 crate 分离」。

**② 装配阶段：build() 只是把外部传来的表存起来，并调一次 `rules()`。**

[src/lib.rs:221-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L221-L234) — `build()` 内 `rules: (self.routines.rules)()`：立刻调用 `rules` 这个函数指针拿到内置 show 规则表 `NativeRuleMap`。注意 `html` 模块也是按需经例程装配的（`global()` 里 `(routines.html_module)()`，见 lib.rs:349）。

**③ 装配者在外部。** 真正把行为 crate 的实现塞进表里的代码**不在** `typst-library`，而在顶层 `crates/typst/src/lib.rs`：

[crates/typst/src/lib.rs:311-325](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L311-L325) — `static ROUTINES: LazyLock<Routines>` 把每个槽位指向 `typst_eval::eval_string`、`typst_realize::realize`、`typst_layout::layout_frame`、`typst_html::module` 等。这是「实现侧」的唯一入口，正因为放在能同时 `use` 两边的顶层 crate，循环依赖才得以避免。

**④ 运行期分发的典型调用点。** 标准 `eval()` 函数体里直接「取指针再调用」：

[src/foundations/mod.rs:305-321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L305-L321) — `(engine.library.routines.eval_string)(engine.world, engine.library, ..., &text, SpanMode::Uniform(span), mode, scope)`。这一行的执行就跨进了 `typst-eval`。

类似地，闭包调用走 `eval_closure`：

[src/foundations/func.rs:352-363](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L352-L363) — `FuncInner::Closure(closure)` 分支调用 `(engine.library.routines.eval_closure)(...)`。

排版相关的能力也走同一套机制，分布在多个文件：

- [src/layout/measure.rs:96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L96) — `measure()` 通过 `routines.layout_frame` 真正排版一帧来度量尺寸。
- [src/visualize/tiling.rs:287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/tiling.rs#L287) — 平铺图案在内部也要排版内容。
- [src/model/outline.rs:788](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L788) — 目录条目也需要排一帧。
- [src/math/ir/resolve.rs:132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/resolve.rs#L132) — 数学 IR 解析时调用 `routines.realize` 把 content 展平。

> 观察：这些调用点都长得一模一样——`(engine.library.routines.<名字>)(参数...)`。`engine.library` 是 `&LazyHash<Library>`，`.routines` 是 `&'static Routines`，`.eval_string` 等是裸 `fn` 字段。整条链路零虚派发开销、零分配。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`typst-library` 不依赖任何行为 crate」这一核心论断，并定位实现侧的装配代码。

**操作步骤**：

1. 打开 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/Cargo.toml) 的 `[dependencies]` 段（第 15–75 行），逐行找 `typst-` 开头的依赖。
2. 在仓库根目录运行（只读检查，不编译）：

   ```bash
   grep -E '^typst-' crates/typst-library/Cargo.toml
   ```

3. 对比打开 `crates/typst/Cargo.toml`（顶层 crate），看它是否列出了 `typst-eval`、`typst-realize`、`typst-layout`、`typst-html`。
4. 用只读 git 查看实现侧装配：

   ```bash
   git show HEAD:crates/typst/src/lib.rs | sed -n '305,326p'
   ```

**需要观察的现象**：

- 步骤 2 的输出**只**有：`typst-assets`、`typst-macros`、`typst-syntax`、`typst-timing`、`typst-utils`——**没有** `typst-eval/realize/layout/html`。
- 顶层 `typst` crate 则**确实**依赖这些行为 crate。

**预期结果**：你亲眼确认了「行为实现不在 `typst-library` 的依赖图里」，而调用它们的能力完全来自运行期的 `Routines` 函数指针表。这就是 crate 分离的物证。

> 若环境无法运行 `grep`/`git`，本结论可通过直接阅读上述两个 `Cargo.toml` 得到，标注「待本地验证」的仅是命令本身的输出格式。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Routines` 改成普通 trait（`trait Routines { fn eval_string(...); ... }`）并用 `dyn Routines`，功能上是否可行？Typst 为什么选了函数指针表？

**参考答案**：功能上可行，但有几个劣势：(a) trait object 带 vtable 与动态派发；(b) 每个 `&self` 方法要经过一次虚调用；(c) trait 方法的签名约束更死（不能像函数指针字段那样自由组合高阶生命周期）。裸函数指针表是 `Copy`、`'static`、可直接存进字段、可被 `Hash`/`Debug`，是最轻量的接线方式。Typst 选它正是为了把「间接调用」的成本压到最低——因为这条路径在每次编译中被频繁触发。

**练习 2**：`Routines` 的字段类型 `fn(...) -> Ret`，和闭包 trait `Fn` 有什么本质区别？为什么这里必须用前者？

**参考答案**：`fn` 是**函数指针**，`Copy`、`'static`、无捕获环境；`Fn` 是 trait，其实现（闭包）可能捕获变量、有生命周期、不可 `Copy`。`Routines` 是 `static`、要被存进结构体字段、要参与 `Hash`，必须用无状态、可拷贝、无生命周期的 `fn` 指针。所有目标函数（`typst_eval::eval_string` 等）恰好都是顶层自由函数，正好满足 `fn` 指针的要求。

**练习 3**：为什么 `Routines` 在 `Library` 里以 `&'static` 的形式存在，而不是拥有所有权？

**参考答案**：整张表在进程启动时由顶层 crate 用 `LazyLock` 装配一次，进程生命期内永不改变。`&'static` 既免拷贝、又让 `Library` 的 `Clone`（用于每次收敛迭代）几乎免费——所有副本共享同一张表。

---

### 4.2 routines! 宏：用声明式宏生成结构

#### 4.2.1 概念说明

`Routines` 有 8 个例程字段，每个都要写一遍签名，而且还要配套生成 `Hash`、`Debug`。手写既冗长又易错（签名改一处要改三处）。`routines!` 是一个声明式宏（`macro_rules!`），输入一串形如 `fn 名字(参数) -> 返回类型` 的声明，输出：

1. `Routines` 结构体——每个声明变成一个 `pub fn(...)` 字段；
2. 一个**手写的、什么都不做**的 `Hash` 实现；
3. 一个打印 `Routines(..)` 的 `Debug` 实现。

这样增删一个例程只需改宏调用处的一行，结构体与两个 trait 实现自动同步。

#### 4.2.2 核心流程

宏的展开逻辑（伪代码）：

```text
routines! {
    fn rules() -> NativeRuleMap
    fn eval_string(...) -> SourceResult<Value>
    fn realize<'a>(...) -> SourceResult<Vec<Pair<'a>>>
}
        │ 展开
        ▼
pub struct Routines {
    pub rules:       fn() -> NativeRuleMap,
    pub eval_string: fn(...) -> SourceResult<Value>,
    pub realize:     for<'a> fn(...) -> SourceResult<Vec<Pair<'a>>>,   // 注意 HRTB
}
impl Hash for Routines { fn hash<H>(&self, _: &mut H) {} }   // 刻意空实现
impl Debug for Routines { fn fmt(..) { f.pad("Routines(..)") } }
```

两个关键细节：

- **高阶生命周期（HRTB）**：`realize<'a>` 的返回类型 `Vec<Pair<'a>>` 与参数 `&'a Content` 的生命周期绑定，展开后字段类型是 `for<'a> fn(...) -> SourceResult<Vec<Pair<'a>>>`。宏里的 `$(for<$($time),*>)?` 片段专门处理这一情况（routines.rs:34）。
- **空的 `Hash`**：`fn hash<H: Hasher>(&self, _: &mut H) {}`——第二个参数直接丢弃，什么也不哈希。

为什么 `Hash` 要刻意留空？因为 `Library` 派生了 `Hash`（用于 comemo 增量记忆化，见 u9-l3/u12-l2），而 `Routines` 是它的字段。但例程表是 `&'static`、进程级常量——它在整个进程里永远是同一组函数指针，**永远不会变化**。comemo 用哈希来探测「输入是否变了」，一个永远不变的量理应对哈希毫无贡献。留空既正确（不会引起错误的缓存命中/失效）又更快（跳过 8 个指针的哈希）。

> 一个小考点：裸函数指针 `fn(...)` 本身**可以**实现 `Hash`（按地址），所以作者并非被迫留空，而是**主动选择**留空以表达「这张表对增量编译透明」的语义。

#### 4.2.3 源码精读

**① 宏定义。** 顶部注释再次点明设计意图。

[src/routines.rs:20-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L20-L48) — `macro_rules! routines`。注意第 31–36 行生成结构体字段时用了 `pub $name: $(for<$($time),*>)? fn ($($args)*) -> $ret`，`for<...>` 片段处理 `realize<'a>` 这种带生命周期的例程。第 38–40 行是空的 `Hash`，第 42–46 行是 `Debug`。

**② 宏调用：列出全部 8 个例程的形状。**

[src/routines.rs:50-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L50-L114) — `routines! { ... }` 展开就是 `Routines` 的全部字段。逐一对应：

| 例程字段 | 签名要点 | 实现所在 crate |
|---------|---------|---------------|
| `rules` ([51-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L51-L52)) | `fn() -> NativeRuleMap` | `typst-layout` + `typst-html`（在顶层 `rules` 闭包里 `register`） |
| `eval_string` ([54-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L54-L65)) | 求值字符串为 `Value` | `typst-eval` |
| `eval_closure` ([67-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L67-L79)) | 调用闭包 | `typst-eval` |
| `realize<'a>` ([81-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L81-L89)) | 把 content 展平为带样式条目列表（带生命周期，HRTB） | `typst-realize` |
| `layout_frame` ([91-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L91-L98)) | 排版成单帧 `Frame` | `typst-layout` |
| `html_module` ([100-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L100-L101)) | 构造 `html` 模块 | `typst-html` |
| `html_mathml_body<'a>` ([103-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L103-L107)) | 取 MathML 元素体 | `typst-html` |
| `html_span_filled` ([109-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L109-L113)) | 给内容包一层带颜色的 span（HTML 导出临时方案） | `typst-html` |

这正是「Routines 暴露的全部例程」清单——8 项。

#### 4.2.4 代码实践

**实践目标**：观察宏展开后的真实代码，确认「一行声明 → 一个字段 + 自动 trait 实现」。

**操作步骤**：

1. 在 `typst-library` 目录运行宏展开（只读、不编译）：

   ```bash
   cargo expand -p typst-library routines 2>/dev/null \
     | grep -A40 'pub struct Routines'
   ```

   > 若未安装 `cargo-expand`，可跳到步骤 2 的纯阅读法。

2. 阅读 [src/routines.rs:50-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L50-L114)，对照上表，把每个 `fn 名字(...)` 在脑中展开成一个 `pub 名字: fn(...) -> ...` 字段。

3. **关键观察**：在展开结果里找到 `impl Hash for Routines`，确认其函数体是空的（`{}`），并对照宏定义第 38–40 行解释原因。

**需要观察的现象**：

- 展开后 `Routines` 有且仅有 8 个字段，与宏调用里的 8 个 `fn` 声明一一对应。
- `realize` 字段类型带 `for<'a>`，其余不带。
- `Hash::hash` 体为空。

**预期结果**：你验证了「增删一个例程 = 改宏一处」的机制，并理解了空 `Hash` 的语义。

> 若 `cargo expand` 不可用：本结论完全可通过阅读 routines.rs:20-48（宏）与 50-114（调用）推断，标注「待本地验证」的仅是 `cargo expand` 命令的可用性。

#### 4.2.5 小练习与答案

**练习 1**：如果要新增一个例程 `fn render_png(...) -> Bytes`（导出 PNG），需要改哪些地方？

**参考答案**：(1) 在 `routines.rs` 的 `routines! { ... }` 里加一行 `fn render_png(...) -> Bytes`；(2) 在顶层 `crates/typst/src/lib.rs` 的 `static ROUTINES` 里加 `render_png: typst_<某 crate>::render_png`；(3) 在需要调用的标准库函数里写 `(engine.library.routines.render_png)(...)`。`Routines` 结构体、`Hash`、`Debug` **无需**手改——宏自动生成。

**练习 2**：宏里 `$(for<$($time),*>)?` 这个片段为什么是必须的？去掉会怎样？

**参考答案**：它生成 `for<'a>` 形式的高阶 trait bound（HRTB），让 `realize<'a>` 这种「返回值/参数的生命周期由调用点决定」的函数指针能被正确表达。去掉后，`realize` 字段会变成普通 `fn(...)`，编译器无法把签名里的 `'a` 与参数 `'a Content`、返回 `Vec<Pair<'a>>` 绑定，导致生命周期不匹配的编译错误。

**练习 3**：`Debug` 实现为什么打印 `Routines(..)` 而不是逐字段打印？

**参考答案**：函数指针的 `Debug` 输出是无意义的地址，逐字段打印对人类毫无可读性。`Routines(..)` 既表明「这是 Routines」，又用 `..` 暗示「省略了内部细节」，是惯例写法（与「未实现的 Debug 用占位符」一致）。

---

### 4.3 SpanMode、RealizationKind 与其他附属类型

#### 4.3.1 概念说明

`routines.rs` 文件后半部分（116 行之后）定义了 `SpanMode`、`RealizationKind`、`FragmentKind`、`Arenas`、`Pair` 五个类型。源码注释直接点明了它们的来历：

> *「The types below only live here to enable the routines to be defined here. Conceptually, they belong with the modules where the functions they are used with are defined in.」*
> （下列类型只待在这里，是为了让例程能在此定义。从概念上讲，它们本应住在各自被使用的模块里。）

为什么「本应住在别处却放在 routines.rs」？因为它们是**例程签名的组成部分**（参数类型或返回类型），而例程的**形状**定义在 `typst-library`。如果 `SpanMode` 住在 `typst-eval`，那么 `routines.rs` 里写 `fn eval_string(..., spans: SpanMode, ...)` 就会让 `typst-library` 依赖 `typst-eval`——循环依赖立刻回来。

于是 Typst 采取的分工是：**所有「接口词汇类型」都收在 `typst-library`**（因为它是所有 crate 的公共依赖），**行为实现留在行为 crate**。`SpanMode`、`RealizationKind` 就是这种「跨 crate 共享的接口词汇」。

#### 4.3.2 核心流程

这五个类型分别服务两类例程：

- **服务 `eval_string` 的 `SpanMode<'a>`**：描述「求值文本里的语法节点该挂哪个 span」。分两种模式：
  - `Uniform(Span)`：所有节点共用一个 span（简单、用于就地 `eval`）。
  - `Mapped { id, mapper, mapper_error_span }`：文本不在 Typst 文件里时，用 `RangeMapper` 把文本区间精确映射进真实文件，诊断信息能落到正确位置。

- **服务 `realize` 的 `RealizationKind<'a>` / `FragmentKind` / `Arenas` / `Pair<'a>`**：描述「这次收敛是哪种性质的、产出什么、临时内存从哪来」。
  - `RealizationKind`：`Bundle` / `Document { info }` / `Fragment { kind }` / `Par` / `Math`——区分顶层文档、容器片段、段落、数学等不同收敛场景。
  - `FragmentKind`：`Inline` / `Block`——收敛产物是「纯行内」还是「被强制成块」。
  - `Arenas`：收敛期延长对象生命周期的临时内存场（content / styles / bump）。
  - `Pair<'a> = (&'a Content, StyleChain<'a>)`：`realize` 的返回元素——「一段内容 + 作用于它的样式链」。

#### 4.3.3 源码精读

**① 文件作者的自白。** 这段注释是理解整个后半部分的钥匙。

[src/routines.rs:116-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L116-L118) — 明说这些类型「只为了支撑例程定义而存在于此」。

**② `SpanMode`：影响诊断落点与 content 的 span。**

[src/routines.rs:120-151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L120-L151) — 两种变体的文档说明：`Uniform` 让所有错误与 content 共用一个 span；`Mapped` 允许把外部文本（不在 `.typ` 文件里的）精确映射回真实文件，从而得到精准诊断（而非笼统地报到 `eval` 调用处）。它在 `eval_string` 调用点以 `SpanMode::Uniform(span)` 形式被构造（见 [foundations/mod.rs:318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L318)）。

**③ `RealizationKind`：收敛的「种类」开关。**

[src/routines.rs:153-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L153-L169) — 五个变体，`Document { info: &'a mut DocumentInfo }` 和 `Fragment { kind: &'a mut FragmentKind }` 通过可变引用把收敛结果「回写」给调用方（一种借用返回的技巧）。

**④ `FragmentKind` / `Arenas` / `Pair`。**

- [src/routines.rs:171-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L171-L180) — `FragmentKind::Inline / Block`。
- [src/routines.rs:182-193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L182-L193) — `Arenas`，三个临时内存场（`typed_arena`、`bumpalo`），注释强调「返回的 content 仍在使用期间必须保活」。
- [src/routines.rs:195-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L195-L196) — `Pair<'a>`，`realize` 的输出元素类型别名。

#### 4.3.4 代码实践

**实践目标**：理解「接口词汇类型为何必须住在 typst-library」这一架构原则。

**操作步骤**：

1. 假设把 `SpanMode` 移到 `typst-eval`（仅作思维实验）。
2. 问自己：`routines.rs` 第 55–65 行的 `eval_string` 签名（含 `spans: SpanMode`）还能在 `typst-library` 编译通过吗？
3. 用 git 确认 `SpanMode` 在本 crate 的导出位置：

   ```bash
   git show HEAD:crates/typst-library/src/routines.rs | sed -n '120,151p'
   ```

4. 在行为 crate 侧反查它如何被引用（只读）：

   ```bash
   grep -rn "SpanMode" crates/typst-eval/src | head
   ```

**需要观察的现象**：

- 步骤 2：不能——`typst-library` 不依赖 `typst-eval`，签名里出现 `typst_eval::SpanMode` 会直接编译失败。这正是这些类型必须留在本 crate 的原因。
- 步骤 4：`typst-eval` 里 `use typst_library::routines::SpanMode;` 之类——即**行为 crate 反过来 import 本 crate 的类型**，依赖方向依旧是「行为 crate → typst-library」，单向无环。

**预期结果**：你验证了「接口词汇在 typst-library、行为实现在行为 crate」的依赖方向一致性，理解了附属类型存在的必要性。

> 若无法运行 grep：阅读 routines.rs 与行为 crate 源码即可得出同一结论；命令仅为辅助。

#### 4.3.5 小练习与答案

**练习 1**：`SpanMode::Uniform` 与 `SpanMode::Mapped` 分别适合什么场景？

**参考答案**：`Uniform(Span)` 适合「就地求值一小段代码」，所有错误报到调用处即可（如用户写的 `#eval("1+")`，错误报到 `eval` 调用点）。`Mapped` 适合「求值的文本来自外部、且希望诊断精确落在外部文件里」（如语言服务器把文档中的某段标记文本求值，希望报错指向原文档的精确位置），它用 `RangeMapper` 把求值文本区间映射回真实文件。

**练习 2**：`RealizationKind::Document { info: &'a mut DocumentInfo }` 为什么用可变引用而不是返回值？

**参考答案**：收敛是一个把 content「展平」的过程，`set document` 规则产生的文档元信息需要在展平过程中被「就地填入」`DocumentInfo`。用 `&'a mut` 让 `realize` 的实现（在 `typst-realize`）能直接写入调用方提供的 `DocumentInfo`，避免额外的返回值包装与所有权转移，签名也更贴近「副作用式填充」的语义。

**练习 3**：`Pair<'a>` 为什么是「内容 + 样式链」的元组，而不是只返回 content？

**参考答案**：收敛要把一棵 content 树展平成「扁平的、带样式的条目列表」（见例程注释 *「realizes content into a flat list of well-known, styled items」*）。展平后每段内容需要明确「它现在受哪条样式链约束」，否则下游排版无法正确取值。`(&Content, StyleChain)` 正是把这段关联打包传递（参见 u4-l1 的 `StyleChain`）。

## 5. 综合实践

**任务**：以本讲为线索，绘制一张「`eval("1 + 2")` 从用户输入到求值完成」的**跨 crate 调用序列图」，并标注每一跳发生在哪个 crate。

**操作步骤**：

1. **入口**：用户在 Typst 源码里写 `#eval("1 + 2")`。找到它映射到的标准库函数——[src/foundations/mod.rs:305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L305) 的 `eval` 函数（`#[func]` 标注，见 u3-l4）。
2. **第一跳（本 crate 内）**：`eval` 函数体构造 `SpanMode::Uniform(span)`、组装 `Scope`，然后写下 `(engine.library.routines.eval_string)(...)`。这一行**还在** `typst-library`。
3. **关键跨 crate 跳**：取出的函数指针实际指向 `typst_eval::eval_string`（由顶层 [crates/typst/src/lib.rs:318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L318) 装配）。控制权进入 `typst-eval` crate。
4. **求值**：`typst-eval` 解析 `"1 + 2"`、求值、返回 `SourceResult<Value>`（`Value::Int(3)`）。注意它使用的 `Value`、`Scope`、`SpanMode` 等类型都来自 `typst-library`。
5. **回到本 crate**：`eval` 把 `Value` 返回给 Typst 用户。

**产出**：画出上述 5 步的序列图，每步标注所属 crate，并在「第 3 步」处用箭头说明「这里是通过函数指针完成的跨 crate 调用，编译期无依赖、运行期接线」。

**自我检查**：

- 你的图里，`typst-library → typst-eval` 的箭头是**实线（函数指针调用）**还是**虚线（编译期依赖）**？正确答案：实线（运行期），编译期 `typst-library` 根本不认识 `typst-eval`。
- 反方向 `typst-eval → typst-library`（用 `Value`/`SpanMode`）是什么？正确答案：编译期依赖（`typst-eval` 的 `Cargo.toml` 列了 `typst-library`）。

完成本任务后，你应该能向别人讲清：「Typst 用一张 `Routines` 函数指针表，把『类型定义』和『行为实现』在 crate 层面彻底分开，又在运行期把它们缝起来——这就是 crate 分离机制。」

## 6. 本讲小结

- `Routines` 是一张 8 字段的**函数指针表**，源码原话称之为「本质上的动态链接」，专门用于实现 **crate 分离**。
- 矛盾在于：行为 crate（`typst-eval` 等）依赖 `typst-library` 的类型，而标准库又要回调行为实现——直接依赖会形成循环。`Routines` 把「调谁」推迟到运行期，让编译期依赖保持单向无环。
- `Routines` 的**形状**定义在 `typst-library`（`routines.rs`），**实现**装配在顶层 `typst` crate 的 `static ROUTINES`，二者在程序启动时接线。
- 调用统一写作 `(engine.library.routines.<名字>)(参数...)`，零虚派发、零分配；调用点遍布 `foundations`、`func`、`layout`、`model`、`math`、`visualize` 等模块。
- `routines!` 宏把「一行声明」展开成「一个字段 + 空 `Hash` + `Debug`」；空 `Hash` 表达「例程表对 comemo 增量编译透明」的语义。
- `SpanMode`、`RealizationKind`、`FragmentKind`、`Arenas`、`Pair` 是「接口词汇类型」，因签名需要而留在 `typst-library`，体现「类型在库内、行为在库外」的分工。

## 7. 下一步学习建议

- **想看例程的真正实现**：进入行为 crate 阅读 `typst-eval::eval_string`、`typst-realize::realize`、`typst-layout::layout_frame` 的源码，对照本讲的签名理解「接口侧 ↔ 实现侧」的契约。
- **想理解为何 `Library` 要 `Hash`**：结合 u9-l3（`Introspector` 与收敛循环）和 u12-l2（comemo/rayon/LazyHash），看 `Routines` 的空 `Hash` 如何融入增量记忆化大局。
- **想看特性开关如何与例程协作**：进入 u12-l1（特性开关与 PDF/HTML 输出模块），看 `html_module` 例程如何被 `Feature::Html` 条件装配进 `global()`。
- **想做扩展实践**：进入 u12-l3，亲手新增一个 `#[func]`/`#[elem]`，理解「新增标准库定义」与「新增例程」两种扩展点的区别。
