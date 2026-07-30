# 解释器入口：eval 与 eval_string

## 1. 本讲目标

前两讲我们已经建立了两件大事：typst-eval 是 Typst 的「代码解释器」，负责把语法树（AST）求值成运行时值（[u1-l1](u1-l1-overview.md)）；它的源码按 AST 节点类别被切成 12 个子模块，所有求值共享一台 `Vm` 虚拟机，统一的抽象是 `Eval` trait（[u1-l2](u1-l2-module-map.md)）。

但有一个问题我们一直「悬而未决」：**这一切是从哪里启动的？** 给定一个 `.typ` 源文件，第一台 `Vm` 是谁造出来的？求值完成后产出的 `Module` 又是怎么拼起来的？本讲就来回答这个问题。

本讲聚焦在 `src/lib.rs` 顶部的两个**入口函数**：

- `eval`：把一整个 `Source`（源文件）求值成一个 `Module`（模块）。
- `eval_string`：按指定的语法模式（Code / Markup / Math）求值一段字符串，返回单个 `Value`。

学完本讲，你应该能够：

- 说出 `eval()` 从被调用到返回 `Module` 的 **6 个主要步骤**，并能逐行对应到源码；
- 解释 `#[comemo::memoize]` 缓存对求值入口的意义，以及 `route.contains()` 如何防止**循环求值**；
- 看懂 `Engine`、`Route`、`Sink`、`Traced`、`Scopes`、`Context` 这些「求值环境」是如何在入口里被准备出来的；
- 说明 `eval_string()` 与 `eval()` 在**解析时机、错误处理、返回类型**上的关键差异；
- 回答一个设计性问题：**为什么在有语法错误、但处于 inspect（IDE 追踪）模式时，`eval` 仍然会继续求值？**

本讲是第 1 单元的「钻进源码」环节——从鸟瞰（u1-l1）、地图（u1-l2）进入到了**逐行精读**。

## 2. 前置知识

本讲假定你已经读过 [u1-l1](u1-l1-overview.md) 和 [u1-l2](u1-l2-module-map.md)。下面补充几个本讲会反复出现、但前两讲还没展开的概念。

### 2.1 comemo 与 `Tracked` / `TrackedMut`

`comemo` 是 Typst 使用的**记忆化（memoization）缓存库**。它的核心思想是：给一个纯函数打上 `#[comemo::memoize]`，它就会**记住「输入 → 输出」的映射**；下次用「相等」的输入再调用时，直接返回缓存结果，不再重新计算。

但 Rust 的引用/借用不能直接当缓存键。所以 comemo 提供了 `Tracked<T>` 和 `TrackedMut<T>` 这两种**「可追踪句柄」**——它们是对内部数据的只读 / 可写包装，可以被哈希、比较，从而参与缓存键的相等性判定。你在 `eval` 的签名里看到的 `Tracked<dyn World>`、`Tracked<Route>`、`TrackedMut<Sink>` 都是这种句柄。

> 一句话记忆：**`#[comemo::memoize]` 负责缓存，`Tracked`/`TrackedMut` 负责「让参数能参与缓存判等」**。细节我们在 4.2 展开。

### 2.2 `SourceResult<T>` 与 `?` 运算符

求值可能失败（比如代码里写了 `#panic` 之类）。typst-library 定义了：

- `SourceResult<T>`：要么是成功的 `Ok(T)`，要么是 `Err(Vec<SourceDiagnostic>)`——一组带**源码定位（span）**的诊断信息。
- 函数末尾的 `?`：遇到 `Err` 就**提前返回**，把错误向上抛。

本讲里你会看到大量 `?`，比如 `markup.eval(&mut vm)?`——它表示「求值 markup，如果出错就把错误返回给调用者」。

### 2.3 `Value` 与 `Content`（回顾）

- **`Value`**：代码模式（Code）的运行时值（整数、字符串、数组、函数……）。
- **`Content`**：排版内容的抽象（一段文本、一个标题、一个列表项）。

`eval` 求值整个文件，产出的是一段排版内容（`Content`），再连同作用域一起打包成 `Module`；`eval_string` 则**按模式**决定产出 `Value` 还是 `Content`。

### 2.4 模块（Module）是什么

一个 `.typ` 文件求值后会得到两样东西：

1. **一段排版内容**（文件里写的文本/标题/图形）；
2. **一个作用域**（文件里 `#let`、`import` 定义出的名字）。

把这两者打包在一起，就是 `Module`。后续别的文件 `import` 它时，既能取走其中的名字，也能 `include` 它的内容。`Module` 的内部结构我们会在 4.4 精读。

## 3. 本讲源码地图

本讲的主战场是 `lib.rs` 的两个入口函数，但要把它们讲透，必须顺带看几个「配角」类型。

| 文件 | 角色 | 本讲用来做什么 |
| --- | --- | --- |
| `crates/typst-eval/src/lib.rs` | 入口函数 `eval` / `eval_string`、`Eval` trait | **主战场**，逐行精读 |
| `crates/typst-eval/src/vm.rs` | `Vm` 虚拟机、`Vm::new` | 看 `Vm` 是怎么在入口里被构造的 |
| `crates/typst-eval/src/flow.rs` | `FlowEvent` 与 `forbidden()` | 看顶层出现 break/return 时如何报错 |
| `crates/typst-library/src/engine.rs` | `Engine` / `Route` / `Sink` / `Traced` | 看求值环境的类型定义 |
| `crates/typst-library/src/foundations/module.rs` | `Module` 结构与构造器 | 看 `Module` 如何被装配 |
| `crates/typst-library/src/routines.rs` | `SpanMode` 枚举 | 看 `eval_string` 的 span 模式 |

> 本讲的永久链接统一指向提交 `146a58329a30f6cd38978c22c6bf0e430d8962a1`。`typst-eval` 下的文件用 `crates/typst-eval/src/...`，`typst-library` 下的文件用 `crates/typst-library/src/...`，都在同一个仓库、同一个提交里。

---

## 4. 核心概念与源码讲解

### 4.1 两个入口：定位、签名与六步流程

#### 4.1.1 概念说明

typst-eval 对外暴露的「求值」能力其实有**两个层面**：

- **「求值一整个文件」** → `eval()`：这是编译流水线的主入口。CLI（typst-cli）每编译一个文档，最终都会调到这里；别的文件 `import`/`include` 一个 `.typ` 时，也会递归调到这里。它吃进一个 `Source`，吐出一个 `Module`。
- **「求值一小段字符串」** → `eval_string()`：这是一个**更灵活的工具函数**，常被测试、REPL、IDE 或「把 Typst 当库用」的场景调用。它不绑定具体文件，而是按调用者指定的语法模式（Code/Markup/Math）求值一段字符串，返回单个 `Value`。

打个比方：`eval` 像是「编译一个 `.typ` 工程」，而 `eval_string` 像是「在程序里临时跑一句 Typst 代码」。

#### 4.1.2 核心流程：`eval()` 的六步

`eval()` 的函数体虽然不长，但逻辑层次很清晰，可以拆成 **6 步**：

```text
eval(world, library, traced, sink, route, source)
  │
  ├─ 步骤①  循环防护：route.contains(id)？是 → panic（内部错误）
  │
  ├─ 步骤②  准备 Engine：组装 library / world / introspector / traced / sink / route
  │
  ├─ 步骤③  准备 Vm：Context::none() + Scopes::new + Vm::new
  │
  ├─ 步骤④  收集语法错误/警告：warnings 进 sink；有 errors 且非 inspect → 提前返回 Err
  │
  ├─ 步骤⑤  求值 Markup：root.cast::<Markup>() 后 markup.eval(&mut vm)
  │
  └─ 步骤⑥  处理 flow + 装配 Module：有残留 flow → bail；否则 Module::new(...).with_content(...).with_file_id(...)
```

这 6 步构成了「从源文件到模块」的完整骨架。后面的 4.2～4.4 会逐步把每一步掰开讲。

#### 4.1.3 源码精读：`eval` 的完整签名与函数体

先看 `eval` 的整体（属性 + 签名 + 六步）：

> [src/lib.rs:37-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L37-L97) —— 两个 `#[...]` 属性 + `pub fn eval`，这是「求值整个源文件得到 Module」的入口。

我们逐段看。

**属性与签名**（[src/lib.rs:38-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L38-L47)）：

```rust
#[comemo::memoize]
#[typst_macros::time(name = "eval", span = source.root().span())]
pub fn eval(
    world: Tracked<dyn World + '_>,
    library: &LazyHash<Library>,
    traced: Tracked<Traced>,
    sink: TrackedMut<Sink>,
    route: Tracked<Route>,
    source: &Source,
) -> SourceResult<Module> {
```

两个属性的作用：

- `#[comemo::memoize]`：开启**记忆化缓存**（4.2 详解）。同一个 `source`（且其它 `Tracked` 参数相等时）只求值一次。
- `#[typst_macros::time(...)]`：插桩**计时**（由 typst-timing crate 提供），用于性能剖析，对结果无影响。

6 个参数可以分成 3 组：

| 参数 | 含义 | 分组 |
| --- | --- | --- |
| `world` | 对「外部世界」的句柄（读文件、字体、日期……） | 环境 |
| `library` | 标准库（内置函数、类型、作用域） | 环境 |
| `traced` | 「正在追踪哪个 span」（IDE hover 用） | 环境 |
| `sink` | 收集警告/延迟错误/追踪值的「下水道」 | 环境 |
| `route` | 当前的「调用路径」，用于防循环与限深 | 环境 |
| `source` | 要被求值的源文件本身 | 输入 |

返回类型 `SourceResult<Module>`：成功返回 `Module`，失败返回一组诊断。

**步骤① 循环防护**（[src/lib.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L48-L52)）：

```rust
// Prevent cyclic evaluation.
let id = source.id();
if route.contains(id) {
    panic!("Tried to cyclicly evaluate {:?}", id.vpath());
}
```

注意这里用的是 `panic!` 而不是返回 `Err`。这是一个**有意为之的设计**：循环求值（A 求 B、B 又求 A）属于「编译器内部逻辑不应出现的状态」，而不是「用户代码写错了」。因此用 panic 表示「这是 bug 级别的问题」。`route.contains(id)` 沿着调用链向上查找，看这个文件 id 是否已经在当前路径里（详见 4.3）。

**步骤② 准备 Engine**（[src/lib.rs:54-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L54-L63)）：

```rust
let introspector = EmptyIntrospector;
let engine = Engine {
    library,
    world,
    introspector: Protected::new(introspector.track()),
    traced,
    sink,
    route: Route::extend(route).with_id(id),
};
```

`Engine` 是「求值引擎」，把上面 5 个环境参数 + 一个空的 introspector（**内省器**，排版期才知道页面布局，求值阶段先给空的）打包成一个结构体。`Route::extend(route).with_id(id)` 表示「在父路径上延长一段，并记下当前文件 id」——这正是循环检测要查的数据。

**步骤③ 准备 Vm**（[src/lib.rs:65-69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L65-L69)）：

```rust
let context = Context::none();
let scopes = Scopes::new(Some(library));
let root = source.root();
let mut vm = Vm::new(engine, context.track(), scopes, root.span());
```

- `Context::none()`：求值阶段的「上下文」先设为空（排版阶段才会注入真实上下文）。
- `Scopes::new(Some(library))`：建一个**作用域栈**，栈底放标准库（这样内置名字如 `calc`、`str` 才找得到）。
- `Vm::new(...)`：把 engine、context、scopes 装配成一台虚拟机。注意第 4 个参数是 `root.span()`——它会被用来判断「这台 Vm 是否正处于 inspect 模式」（4.4 详解）。

**步骤④ 收集语法错误/警告**（[src/lib.rs:71-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L71-L82)）：

```rust
let (errors, warnings) = root.errors_and_warnings();
for warning in warnings {
    vm.engine.sink.warn(warning.into());
}
if !errors.is_empty() && vm.inspected.is_none() {
    return Err(errors.into_iter().map(Into::into).collect());
}
```

关键在 `&& vm.inspected.is_none()` 这个条件——**只有在「没有正在追踪的 span」时，才因语法错误提前返回**。这就是本讲标题里那个设计性问题的答案所在（见 4.4.4）。注释也点明了：警告统一走 sink，不跟错误混在一起返回。

**步骤⑤ 求值 Markup**（[src/lib.rs:84-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L84-L86)）：

```rust
let markup = root.cast::<ast::Markup>().unwrap();
let output = markup.eval(&mut vm)?;
```

一个 `.typ` 文件的根节点一定是 `ast::Markup`（标记模式）。`cast::<ast::Markup>()` 把语法根节点「转换视图」成 Markup，然后调用 `markup.eval(&mut vm)`——这就是 `Eval` trait 的入口！从这里开始，整棵 AST 的求值就递归展开了（后续讲义 u2 会深入）。`output` 的类型是 `Content`。

**步骤⑥ 处理 flow 并装配 Module**（[src/lib.rs:88-96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L88-L96)）：

```rust
if let Some(flow) = vm.flow {
    bail!(flow.forbidden());
}

let name = id.vpath().file_stem().unwrap_or_default();
Ok(Module::new(name, vm.scopes.top).with_content(output).with_file_id(id))
```

- 求值完整个文件后，如果 `vm.flow` 里还残留着控制流事件（比如文件顶层写了一个裸 `return`/`break`），说明它在「不该出现的地方」出现了——调用 `flow.forbidden()` 转成错误并 `bail!`。
- 否则，用 `Module::new` 把「文件名（取自路径的 file_stem）+ 最外层作用域 `vm.scopes.top`」组装成模块，再用 builder 链 `.with_content(output)`（塞进排版内容）和 `.with_file_id(id)`（记下来源文件）补全。

至此 `eval` 返回一个完整的 `Module`。

#### 4.1.4 代码实践：六步标注

**实践目标**：把抽象的「六步流程」牢牢绑定到真实代码行上。

**操作步骤**：

1. 打开 [src/lib.rs:37-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L37-L97)。
2. 在源码旁边（或一张纸上）画出上面的 6 步流程图。
3. 给每一步标注它对应的源码行区间，例如：步骤① = `L48-L52`，步骤⑥ = `L88-L96`。

**需要观察的现象**：注意步骤②（准备 Engine）和步骤③（准备 Vm）是**求值开始之前**的纯装配；真正「跑」代码只有步骤⑤那一行 `markup.eval(&mut vm)?`。

**预期结果**：你能不看讲义，指着源码说出「这一步在干什么、它产生什么变量、交给下一步用」。

#### 4.1.5 小练习与答案

**练习 1**：`eval` 的返回类型是 `SourceResult<Module>`。如果求值过程中触发了 `bail!`（比如步骤⑥发现残留 flow），这个 `Module` 还会返回吗？

> **答案**：不会。`bail!` 等价于 `return Err(...)`，它会立即以错误诊断数组结束函数，跳过后面的 `Ok(Module::new(...))`。

**练习 2**：`eval` 的第 5 个参数 `source` 是 `&Source`，而前几个环境参数是 `Tracked<...>`。为什么 `source` 不需要包成 `Tracked`？

> **答案**：`Source` 本身是按值/引用即可参与 `Hash`/比较的普通类型，comemo 可以直接用它做缓存键；而 `World`/`Route`/`Sink`/`Traced` 这些是跨调用的「可追踪句柄」，必须用 `Tracked` 包装才能正确参与缓存判等。

---

### 4.2 comemo::memoize：缓存的意义与循环防护

#### 4.2.1 概念说明

为什么 `eval` 要打 `#[comemo::memoize]`？因为 Typst 的编译是**增量、多趟**的：

- 文档里 `import` 的模块可能被多处引用；
- 排版阶段可能因为内省（introspection）触发**重新求值**；
- IDE 每次敲一个字符都可能触发重新编译。

如果每次都从零求值，性能不可接受。comemo 的做法是：**「相同输入 → 直接返回缓存的输出」**。对 `eval` 来说，「输入相同」意味着 `world`、`library`、`traced`、`sink`、`route`、`source` 这些参数**都相等**（按 comemo 的判等规则）。一旦命中缓存，就完全跳过步骤①~⑥。

这也解释了为什么所有「环境」参数都要用 `Tracked`/`TrackedMut`：它们是 comemo 用来**判定输入是否相等**的句柄。

#### 4.2.2 核心流程：缓存命中 vs. 缓存未命中

```text
调用 eval(source, ...)
  │
  ├─ comemo 用所有参数算出「缓存键」
  │
  ├─ 命中？  ── 是 ──→ 直接返回缓存的 SourceResult<Module>（连副作用都重放）
  │
  └─ 未命中  ──→ 执行步骤①~⑥，把结果（连同 sink 里记录的副作用）写入缓存
```

关于副作用有一个精妙之处：`Sink` 是 `TrackedMut`（可写），求值过程中往里塞的警告、追踪值、延迟错误**都是副作用**。comemo 在缓存未命中时执行函数、记录这些副作用；缓存命中时，它会把**当初记录的副作用再「重放」一遍**，从而对外表现得好像重新执行了一次。这就是注释里「`(&mut self, ..) -> ()` 的 tracked 方法不需要验证」的底气——comemo 保证重放的一致性。

#### 4.2.3 源码精读：memoize 属性与循环防护

回到函数上方的属性：

> [src/lib.rs:38-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L38-L39) —— `#[comemo::memoize]` 开启记忆化；`#[typst_macros::time(...)]` 仅做计时插桩。

再看循环防护：

> [src/lib.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L48-L52) —— `route.contains(id)` 为真就 `panic!`。

`route` 的类型定义在 typst-library：

> [crates/typst-library/src/engine.rs:255-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L255-L281) —— `Route` 是一个「链表式」结构：每段有可选的 `outer`（父段）、可选的 `id`（文件 id）、以及一个 `len`（这段贡献的嵌套深度）。

`contains` 就是沿链表查 id：

> [crates/typst-library/src/engine.rs:400-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L400-L402) —— 当前段 `id` 匹配，或递归问 `outer` 是否包含。

而 `Route::extend` 和 `with_id` 是在步骤②里用的：

> [crates/typst-library/src/engine.rs:295-307](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L295-L307) —— `extend` 接上父段并令 `len = 1`；`with_id` 给当前段打上文件 id。

`Route` 同时承担两件事：**循环防护**（`contains`，靠 `id`）和**深度限制**（靠 `len` 的累加）。整条路径的总深度是各段 `len` 之和：

\[
\text{depth}(\text{route}) \;=\; \sum_{\text{seg} \in \text{route}} \text{seg.len}
\]

当这个深度超过 `MAX_CALL_DEPTH`（80）、`MAX_SHOW_RULE_DEPTH`（64）等阈值时，`Route` 的 `check_call_depth` / `check_show_depth` 会报错（见 u6-l3）。注意这是**返回错误**，不是 panic——深度过深是用户代码的问题（比如无限递归），而循环求值是编译器内部问题。

> [crates/typst-library/src/engine.rs:340-393](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L340-L393) —— 各类 `MAX_*_DEPTH` 常量与 `check_call_depth` 的实现。

#### 4.2.4 代码实践：读 comemo 文档并预测缓存行为

**实践目标**：理解「哪些参数变化会让 `eval` 缓存失效」。

**操作步骤**：

1. 阅读 comemo 的文档（在 `Cargo.toml` 的依赖里能看到 `comemo = { workspace = true }`，可在 crates.io 或 docs.rs 查 `comemo` crate）。
2. 对照 `eval` 的 6 个参数，回答下面的问题。

**需要观察/思考的问题**：

- 如果只改了 `sink`（比如换了一个新的空 Sink），`eval` 会重新求值吗？
- 如果 `source` 的内容没变，但 `route` 变深了一层（嵌套调用），会命中缓存吗？

**预期结果**：

- `sink` 是 `TrackedMut`，改变它通常会让缓存失效（因为它是可变输入）。
- `route` 也是缓存键的一部分。但 comemo 对 `Route` 有特殊优化——它用 `upper`（父链长度的上界）而不是精确长度参与判等，这样「同一个 source 在不同但都未超限的深度下求值」仍可能复用缓存（见 engine.rs 里 `upper: AtomicUsize` 的注释，L275-L280）。
- 「待本地验证」：comemo 的精确失效规则建议你写一个小测试，改一个参数观察是否触发重新求值（例如在 `markup.eval` 前后打印日志）。

#### 4.2.5 小练习与答案

**练习 1**：`eval` 用 `panic!` 处理循环求值，但用 `Err`（`check_call_depth`）处理递归过深。为什么这两种「坏情况」处理方式不同？

> **答案**：循环求值（A→B→A）意味着 comemo 的缓存机制本身被绕过了、属于编译器内部的非法状态，因此用 panic；而递归过深是用户写出了「太深的合法递归」（如 `let f = () => f()`），属于用户错误，应当返回可被捕获的 `Err` 并显示给用户。

**练习 2**：为什么 `Sink` 的 tracked 方法（如 `warn`、`value`）签名都是 `(&mut self, ..) -> ()`，注释却说「不需要验证」？

> **答案**：因为 comemo 在缓存命中时会**重放**当初记录的副作用，保证可观察行为与重新执行一致；这些方法没有有意义的返回值需要校验。

---

### 4.3 Engine、Route、Scopes、Context：求值环境的准备

#### 4.3.1 概念说明

步骤②③ 是「搭台子」——在真正求值（步骤⑤）之前，把所有「求值环境」准备好。这些环境分成两类：

- **静态、贯穿全程的** → 打包进 `Engine`：`library`、`world`、`introspector`、`traced`、`sink`、`route`。
- **本次求值专属的、可变的** → 放进 `Vm`：`scopes`（作用域栈）、`flow`（控制流事件）、`inspected`（追踪 span）、`context`。

`Engine` 更像「全局配置 + 外部世界」，`Vm` 更像「当前这次求值的临时状态」。一台 `Vm` 只服务一次模块求值或一次函数调用（这和 [u1-l2](u1-l2-module-map.md) 里说的「每求值一个模块、每调用一次函数都新建一个 Vm」一致）。

#### 4.3.2 核心流程：从 5 个参数到一台 Vm

```text
world + library + traced + sink + route     source
        │                                      │
        ▼                                      ▼
   组装 Engine (introspector 先给空)        root = source.root()
        │                                      │
        │  Route::extend(route).with_id(id)    │
        │                                      ▼
        │                            Context::none() + Scopes::new(library)
        │                                      │
        └──────────────► Vm::new(engine, context, scopes, root.span())
                                               │
                                               ▼
                                            一台待命 Vm
```

#### 4.3.3 源码精读

**Engine 的装配**已在 4.1.3 的步骤②引用过（[src/lib.rs:54-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L54-L63)）。`Engine` 结构体本身定义在 typst-library（字段就是那 6 个），这里不展开。

**Route 的延长**（`Route::extend(route).with_id(id)`）已引用 [engine.rs:295-307](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L295-L307)。

**Vm 的结构**（[src/vm.rs:12-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L12-L28)）：

```rust
pub struct Vm<'a> {
    pub engine: Engine<'a>,        // 求值引擎（全局环境）
    pub flow: Option<FlowEvent>,   // 当前正在发生的控制流事件
    pub scopes: Scopes<'a>,        // 作用域栈
    pub inspected: Option<Span>,   // 正在追踪的 span（IDE 用）
    pub context: Tracked<'a, Context<'a>>, // 隐式上下文
}
```

**Vm::new**（[src/vm.rs:31-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L31-L40)）：

```rust
pub fn new(engine, context, scopes, target: Span) -> Self {
    let inspected = target.id().and_then(|id| engine.traced.get(id));
    Self { engine, context, flow: None, scopes, inspected }
}
```

这一行是本讲的关键细节之一：

```rust
let inspected = target.id().and_then(|id| engine.traced.get(id));
```

它的含义是——「拿 `target`（这里是文件根 span）的文件 id，去问 `traced`：『你要追踪的 span 在不在本文件里？』」。`Traced::get` 的实现：

> [crates/typst-library/src/engine.rs:140-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L140-L142) —— 只有当被追踪的 span 属于 `id` 这个文件时才返回 `Some(span)`，否则返回 `None`。

这个「按文件过滤」的设计很重要：它保证 IDE 追踪某个文件的某个 span 时，**只有那个文件**的求值会进入 inspect 模式，其它文件的缓存不会因此失效（见 4.2）。

`flow` 初始化为 `None`——一开始没有任何控制流事件。`engine.traced.get` 也可能返回 `None`，此时 `inspected = None`，即「不在追踪模式」。

> 补充：`Vm` 还提供 `define` / `bind` / `trace` / `trace_at` 等方法（[src/vm.rs:47-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L47-L91)），它们负责绑定变量、记录追踪值。这些会在 [u1-l4](u1-l1-eval-trait-vm.md) 深入，本讲只需知道「`Vm` 持有这些能力」即可。

#### 4.3.4 代码实践：追踪 `inspected` 的来源

**实践目标**：搞清楚「这台 Vm 到底在不在线上追踪」由谁决定。

**操作步骤**：

1. 读 [src/vm.rs:38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L38) 这一行。
2. 追问：`engine.traced` 从哪来？答案在 [src/lib.rs:56-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L56-L63)——它就是 `eval` 的第 3 个参数 `traced: Tracked<Traced>`。
3. 再追问：调用者怎么把「想追踪的 span」传进来？答案是构造一个 `Traced::new(span)` 并 track 后传入。

**需要观察的现象**：`inspected` 是否为 `Some`，完全取决于「`traced` 里记录的 span 是否落在 `source.id()` 这个文件里」。

**预期结果**：你能用自己的话解释「为什么追踪 A 文件的 span，不会让 B 文件的 `eval` 进入 inspect 模式」（因为 `Traced::get` 按文件 id 过滤）。

#### 4.3.5 小练习与答案

**练习 1**：`eval` 里 `Context::none()`，这意味着求值阶段能拿到真实的「当前样式/位置」上下文吗？

> **答案**：不能。求值阶段上下文为空（`Context::none()`），真实的上下文（如当前样式、所在页面）要到排版阶段才注入。这也是为什么 `eval` 里 `introspector` 先给 `EmptyIntrospector`。

**练习 2**：`Route::extend(route).with_id(id)` 做了两件事，分别对应 `Route` 的哪两个职责？

> **答案**：`with_id(id)` 把当前文件 id 记进路径，供 `contains` 做**循环防护**；`extend` 令 `len = 1`，参与**深度累加**（供 `check_call_depth` 限深）。

---

### 4.4 求值、错误处理与 Module 装配

#### 4.4.1 概念说明

步骤④⑤⑥ 是「真正干活」的部分：

- **步骤④** 先把语法层（typst-syntax）已经发现的问题（errors/warnings）处理掉；
- **步骤⑤** 才调用 `Eval` trait 跑 AST；
- **步骤⑥** 把结果打包成 `Module`，并处理「顶层不应出现的控制流」。

这里有一个微妙但重要的设计：**「语法错误」和「求值」并不是完全互斥的**。在有语法错误时，如果正处于 IDE 追踪（inspect）模式，`eval` 仍然会继续求值。

#### 4.4.2 核心流程

```text
root.errors_and_warnings()
   │
   ├─ warnings → 全部丢进 sink（无论是否 inspect）
   │
   └─ errors 非空？
         ├─ 是 且 inspected.is_none()  → return Err(errors)   ← 正常编译，有错就停
         └─ 是 且 inspected.is_some()  → 继续！              ← IDE 追踪，容忍语法错
                  │
                  ▼
   markup.eval(&mut vm)?          ← 求值整棵 Markup（产出 Content）
                  │
                  ▼
   vm.flow 残留？
         ├─ 是 → bail!(flow.forbidden())   ← 顶层裸 return/break 报错
         └─ 否 → Module::new(name, scopes.top)
                    .with_content(output)
                    .with_file_id(id)       ← 装配并返回
```

#### 4.4.3 源码精读

**错误/警告处理**已在 4.1.3 步骤④引用（[src/lib.rs:71-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L71-L82)）。要点是：

- **警告永远进 sink**（`vm.engine.sink.warn(...)`），不随 `Err` 返回——这是为了「警告统一走 sink」的一致性。
- **错误**只在 `!errors.is_empty() && vm.inspected.is_none()` 时提前返回。

**Markup 求值**（[src/lib.rs:84-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L84-L86)）调用 `markup.eval(&mut vm)`，这是 `Eval` trait（[src/lib.rs:177-184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L177-L184)）的实例：

```rust
pub trait Eval {
    type Output;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>;
}
```

对 `ast::Markup`，`Output = Content`。从这里开始进入 u2 的领地。

**flow 处理**（[src/lib.rs:88-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L88-L91)）：

```rust
if let Some(flow) = vm.flow {
    bail!(flow.forbidden());
}
```

`FlowEvent` 定义在 flow.rs：

> [src/flow.rs:12-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L12-L22) —— `Break` / `Continue` / `Return(Span, Option<Value>, bool)` 三种控制流事件。

`forbidden()` 把它转成「不允许出现在这里」的错误：

> [src/flow.rs:24-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L24-L39) —— break 报「cannot break outside of loop」，return 报「cannot return outside of function」。

也就是说：`break`/`continue`/`return` 只能出现在循环或函数体内（在那里它们会被消费掉，见 u3-l2）；如果求值完整个文件后 `vm.flow` 仍非空，说明它出现在了模块顶层——这是非法的。

**Module 装配**（[src/lib.rs:93-96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L93-L96)）：

```rust
let name = id.vpath().file_stem().unwrap_or_default();
Ok(Module::new(name, vm.scopes.top).with_content(output).with_file_id(id))
```

`Module` 的结构与构造器在 typst-library：

> [crates/typst-library/src/foundations/module.rs:48-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L48-L65) —— `Module` 内部有 `scope`（顶层绑定）、`content`（排版内容）、`file_id`（来源文件）三个核心字段。

三个构造器分别填这三个字段：

> - [`Module::new(name, scope)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L69-L78) —— 用文件名和**最外层作用域** `vm.scopes.top` 建模块（content 先为空）。
> - [`with_content(content)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L105-L108) —— 塞入步骤⑤求值出的 `output`。
> - [`with_file_id(id)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L111-L114) —— 记下来源文件，供 `include`/反射使用。

注意传给 `Module::new` 的是 `vm.scopes.top`——**最外层作用域**（不是整个栈）。求值过程中 `#let`、`import` 等定义最终都落在最外层（模块级），函数/块内部的作用域在求值完后就丢弃了。

#### 4.4.4 代码实践：回答本讲的核心设计问题

**实践目标**：解释「为什么在发现语法错误、但处于 inspect 模式时，`eval` 仍然继续求值」。

**操作步骤**：

1. 重读 [src/lib.rs:78-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L78-L82) 的那个 `if` 条件。
2. 结合 4.3 学到的「`inspected` 由 `traced` 决定」，思考 IDE 场景：用户在编辑器里把鼠标悬停（hover）在某个表达式上，想看它的值。

**需要观察的现象 / 预期解释**：

- IDE 触发追踪时，会调用者把 `traced` 设成「想看的那段 span」，于是这台 Vm 的 `inspected = Some(span)`。
- 但用户正在编辑的文件**很可能有尚未修好的语法错误**（写到一半）。如果 `eval` 一遇到语法错误就返回 `Err`，IDE 的 hover 就永远拿不到值——哪怕错误在文件别处、与悬停的表达式无关。
- 所以条件写成 `!errors.is_empty() && vm.inspected.is_none()`：**只有「不在追踪模式」时才因错误停下；追踪模式下容忍语法错误，继续把 markup 求值下去**，这样 `trace_at`/`trace` 才有机会把悬停表达式的值写进 sink，供 IDE 读取。
- 代价：追踪模式下可能求值出一堆「半成品」结果，但这没关系——IDE 只关心被追踪那个 span 的值，且这些结果不会进正式编译产物（追踪是单独的调用路径）。

> 这是一个很典型的「**为工具链体验让步**」的设计权衡。源码注释（[src/lib.rs:71-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L71-L73)）也直接点明了这一点。

#### 4.4.5 小练习与答案

**练习 1**：假如用户在文件顶层写了一行 `#return 5`（函数外 return），`eval` 会怎样结束？

> **答案**：求值 `Markup` 时，`FuncReturn` 会往 `vm.flow` 写入一个 `FlowEvent::Return`。因为顶层没有函数去消费它，求值结束后 `vm.flow` 仍非空，步骤⑥的 `if let Some(flow) = vm.flow` 成立，`bail!(flow.forbidden())` 报「cannot return outside of function」。

**练习 2**：为什么警告要写进 `sink`，而不是和 errors 一起从 `Err` 返回？

> **答案**：源码注释解释道——为了**一致性**，警告统一由 sink 收集（`vm.engine.sink.warn(...)`）。这样无论求值是成功还是有错误，警告的处理路径都只有一条；调用者总是从 sink 取警告，而不必同时处理「返回值里的警告」和「sink 里的警告」两处。

**练习 3**：`Module::new` 接收 `vm.scopes.top`。如果求值过程中函数体内 `#let` 了一个局部变量，它会出现在最终 `Module` 的 scope 里吗？

> **答案**：不会。函数体有自己的作用域（通过 `Scopes::enter` 压栈，见 u2-l3），求值完就弹出。只有落在 `vm.scopes.top`（模块最外层）的绑定才会进 `Module`。

---

### 4.5 eval_string：三种语法模式与 Value 返回

#### 4.5.1 概念说明

`eval_string` 是「轻量、灵活」的第二个入口。它不绑定一个真实文件，而是：

- 接收一段字符串 `string`；
- 按调用者指定的 `mode: SyntaxMode`（Code / Markup / Math）去解析；
- 求值后返回**单个 `Value`**（不是 `Module`）。

典型用途：单元测试里跑一句 Typst 代码、IDE 内部求值片段、把 Typst 当库嵌入别的程序。

它和 `eval` 的关键差异（**重点对比**）：

| 维度 | `eval` | `eval_string` |
| --- | --- | --- |
| 输入 | `&Source`（完整源文件） | `&str`（一段字符串） |
| 解析 | 调用者已解析好（`source.root()`） | **自己**按 `mode` 调 `parse`/`parse_code`/`parse_math` |
| 返回 | `Module` | `Value` |
| 循环防护 | `route.contains(id)` + `panic!` | **无**（用 `Route::default()`，不挂 id） |
| 语法错误处理 | inspect 模式下容忍错误继续求值 | **有错就返回 `Err`**（无 inspect 特例） |
| 追踪 | 支持（`traced` 参数） | 不支持（内部用 `Traced::default()`） |
| 调用路径 | 编译主链路、`import`/`include` 递归 | 工具型，独立调用 |
| span 来源 | 真实文件 span | 由 `SpanMode` 指定 |

#### 4.5.2 核心流程

```text
eval_string(world, library, sink, introspector, context, string, spans, mode, scope)
   │
   ├─ 按 mode 解析：Code→parse_code / Markup→parse / Math→parse_math
   │
   ├─ 按 spans 给节点打 span：Uniform(detached 跳过) / Uniform(synthesize) / Mapped
   │
   ├─ 收集 errors/warnings：有 errors → 直接 return Err（无 inspect 特例）
   │
   ├─ 准备 Engine（traced 用默认空值；route 用 Route::default()）
   │
   ├─ 准备 Vm，并把调用者传入的 scope 压入作用域栈
   │
   ├─ 按 mode 求值：
   │     Code    → Code.eval(&mut vm)            → Value
   │     Markup  → Markup.eval(&mut vm)          → Content → 包成 Value::Content
   │     Math    → Math.eval → EquationElem.pack → Value::Content
   │
   ├─ flow 残留？→ bail!(forbidden)
   │
   └─ Ok(output: Value)
```

#### 4.5.3 源码精读

**完整函数**：

> [src/lib.rs:99-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L99-L175) —— `eval_string`，按 `mode` 求值字符串返回 `Value`。

**① 按 mode 解析**（[src/lib.rs:114-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L114-L118)）：

```rust
let mut root = match mode {
    SyntaxMode::Code => parse_code(string),
    SyntaxMode::Markup => parse(string),
    SyntaxMode::Math => parse_math(string),
};
```

注意 `eval` 不需要这一步——它的 `source` 已经是被解析好的 `Source`。`eval_string` 多承担了「解析」这一步。

**② 按 SpanMode 打 span**（[src/lib.rs:120-126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L120-L126)）。`SpanMode` 决定这段字符串里的节点该带什么 span（影响诊断落点）：

> [crates/typst-library/src/routines.rs:127-151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L127-L151) —— `SpanMode::Uniform(Span)`（统一一个 span）或 `Mapped { id, mapper, .. }`（把字符串区间映射到真实文件区间）。

- `Uniform(detached)`：span 是「游离」的，啥也不做（错误会落在调用 `eval` 的位置）。
- `Uniform(span)`：调用 `root.synthesize(span)` 给所有节点统一打上这个 span。
- `Mapped`：调用 `root.synthesize_mapped(...)` 做区间映射；映射本身可能出错，用 `.at(mapper_error_span)?` 把错误定位到指定 span。

**③ 错误/警告处理**（[src/lib.rs:128-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L128-L137)）——和 `eval` 类似，但**没有 `&& vm.inspected.is_none()` 这个特例**，有 errors 就直接返回：

```rust
if !errors.is_empty() {
    return Err(errors.into_iter().map(Into::into).collect());
}
```

为什么 `eval_string` 不需要 inspect 特例？因为它内部用 `Traced::default()`（[src/lib.rs:140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L140)），永远不处于追踪模式——它本来就是工具型入口，不服务 IDE hover。

**④ 准备 Engine**（[src/lib.rs:139-148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L139-L148)）——注意三点与 `eval` 不同：

- `traced` 用 `Traced::default()`（不追踪）；
- `route` 用 `Route::default()`（一个全新的空路径，**不接父路径**）；
- `introspector` 由调用者传入（而不是固定 `EmptyIntrospector`）。

因为 `route` 不挂任何文件 id，也没有 `route.contains` 检查——`eval_string` 不参与「文件级循环求值」的防护。

**⑤ 准备 Vm + 压入调用者 scope**（[src/lib.rs:150-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L150-L153)）：

```rust
let scopes = Scopes::new(Some(library));
let mut vm = Vm::new(engine, context, scopes, root.span());
vm.scopes.scopes.push(scope);
```

最后一行 `vm.scopes.scopes.push(scope)` 是 `eval_string` 独有的能力：**把调用者提供的 `scope`（一组成员）压到作用域栈上**。这样字符串里的代码就能访问调用者注入的名字——这对「把 Typst 当库用」很重要。

**⑥ 按 mode 求值**（[src/lib.rs:155-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L155-L167)）：

```rust
let output = match mode {
    SyntaxMode::Code => root.cast::<ast::Code>().unwrap().eval(&mut vm)?,
    SyntaxMode::Markup => {
        Value::Content(root.cast::<ast::Markup>().unwrap().eval(&mut vm)?)
    }
    SyntaxMode::Math => Value::Content(
        EquationElem::new(root.cast::<ast::Math>().unwrap().eval(&mut vm)?)
            .with_block(false).pack().spanned(root.span()),
    ),
};
```

返回值差异一目了然：

- **Code 模式**：根节点是 `ast::Code`，求值产出 `Value`（如 `1 + 2` → `Value::Int(3)`）。
- **Markup 模式**：根节点是 `ast::Markup`，产出 `Content`，再包一层 `Value::Content(...)`。
- **Math 模式**：根节点是 `ast::Math`，产出数学内容后包成 `EquationElem`（非块级 `with_block(false)`），再 `.pack()` 成 `Content`，最后包成 `Value::Content`。

**⑦ flow 处理 + 返回**（[src/lib.rs:169-174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L169-L174)）——和 `eval` 的步骤⑥同理，残留 flow 报错；否则 `Ok(output)`。

#### 4.5.4 代码实践：预测三种模式的返回值

**实践目标**：建立对「同一字符串、不同 mode → 不同返回」的直觉。

**操作步骤**：

假设有字符串 `"1 + 2.5"`，分别用三种模式调用 `eval_string`（**待本地验证**：可在 typst 仓库的测试里找到 `eval_string` 的真实调用示例，或写一个最小的 dev 测试）。

**需要观察/预测的现象**：

| 字符串 | mode | 预期返回 `Value` |
| --- | --- | --- |
| `"1 + 2"` | Code | `Value::Int(3)` |
| `"*hi*"` | Markup | `Value::Content(...)`（一段含强调的排版内容） |
| `"a/b"` | Math | `Value::Content(...)`（一个分数方程元素） |

**预期结果**：你能解释为什么 Code 模式直接返回 `Value`，而 Markup/Math 都要包一层 `Value::Content`——因为 Code 的求值结果本身就是 `Value`，而 Markup/Math 的结果是 `Content`，需要统一成 `Value` 才能作为函数返回值。

> **提示**：如果你想在源码里找 `eval_string` 的真实用法，可以用 `Grep` 在整个 typst 仓库搜索 `eval_string`，典型调用点出现在测试辅助代码和 typst-cli 的部分场景中。

#### 4.5.5 小练习与答案

**练习 1**：`eval_string` 为什么不做 `route.contains` 循环防护？

> **答案**：它用 `Route::default()`，不把任何文件 id 挂到路径上，也没有 `source.id()` 可用（输入只是一段字符串）。它通常是「一次性工具调用」，不参与文件间的递归 `import`，因此不需要文件级循环检测。

**练习 2**：`eval_string` 里 `vm.scopes.scopes.push(scope)` 这一行的作用是什么？

> **答案**：把调用者传入的 `scope`（一组预定义名字）作为一层作用域压入栈顶，使得被求值的字符串代码能访问这些注入的名字（类似「带预设变量的求值」）。

**练习 3**：为什么 `eval_string` 不需要（也没有）inspect 特例？

> **答案**：因为它内部固定使用 `Traced::default()`，`Vm::new` 里 `engine.traced.get(id)` 永远返回 `None`，即 `inspected` 恒为 `None`，所以「`!errors.is_empty() && vm.inspected.is_none()`」中的后半段恒真——条件退化成「有错就返回」，不需要单独写特例。

---

## 5. 综合实践

把本讲的全部知识串起来，完成下面这个**「调用追踪」**任务。

**任务**：假设 typst-cli 开始编译一个 `main.typ`，最终会调用 `eval(...)`。请你写一份「调用笔记」，按下面的要求把整条入口链路讲清楚。

1. **画出 `eval` 的六步流程图**（手绘或文字版均可），并在每一步旁边标注它对应的 [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L37-L97) 行号区间。
2. **追踪 `route` 的生命周期**：`eval` 收到的 `route` 参数 → `Route::extend(route)` → `with_id(id)` → 存进 `Engine` → 传给 `Vm`。说明 `route.contains(id)` 在「A import B、B 又 import A」时会怎样触发 panic。
3. **对比 `eval_string`**：列出至少 3 处它与 `eval` 的关键差异，并解释「为什么 `eval_string` 不需要循环防护、不需要 inspect 特例」。
4. **解释设计权衡**：用你自己的话写一段话，回答本讲的核心问题——**为什么在发现语法错误但处于 inspect 模式时 `eval` 仍然继续求值？**（提示：从「IDE hover 体验」和「错误可能在文件别处」两个角度回答。）
5. **（选做，源码阅读型）**：用 `Grep` 在仓库里搜索 `eval_string(` 的调用点，找一个真实用法，说明调用者传了什么样的 `mode`、`SpanMode` 和 `scope`，并解释为什么这么传。

**验收标准**：

- 你的六步流程图行号标注与本讲 4.1.3 一致；
- 你能正确说出 `Module::new` 接收的是 `vm.scopes.top`（最外层作用域）；
- 你能解释 `panic!`（循环）与 `Err`（深度/语法错）处理方式的不同及其原因。

## 6. 本讲小结

- typst-eval 对外有两个 `#[comemo::memoize]` 入口：`eval`（`Source` → `Module`）和 `eval_string`（`&str` + `mode` → `Value`）。
- `eval` 的函数体可拆成 **6 步**：循环防护 → 准备 Engine → 准备 Vm → 收集错误/警告 → 求值 Markup → 处理 flow 并装配 Module。
- `#[comemo::memoize]` 让「相同输入」只求值一次，并重放 sink 副作用；所有环境参数用 `Tracked`/`TrackedMut` 参与缓存判等。
- `Route` 身兼两职：`contains(id)` 做**循环求值防护**（命中则 `panic!`），`len` 累加做**深度限制**（超限返回 `Err`）。
- `Vm::new` 通过 `engine.traced.get(id)` 决定是否进入 inspect 模式——「按文件过滤」保证追踪只影响目标文件。
- `eval` 在 inspect 模式下**容忍语法错误继续求值**，是为了让 IDE hover 能在被编辑（可能有错）的文件里仍拿到表达式的值。
- `eval_string` 自己解析字符串、用空 `Route`/`Traced`、有错即返回、按 mode 返回 `Value`/`Content`，且能把调用者的 `scope` 注入作用域栈。

## 7. 下一步学习建议

本讲把「入口」讲透了，但有一大块我们只是「路过」——**`Eval` trait 的真正实现**和 **`Vm` 的 `define`/`bind`/`trace` 方法**。下一讲 [u1-l4：Eval trait 与 Vm 虚拟机](u1-l1-eval-trait-vm.md) 会把它们作为主角深入讲解：

- `Eval` trait 的 `self` 消费 + `&mut Vm` + `SourceResult<Output>` 设计，以及各 AST 节点 `impl Eval` 的分发模式；
- `Vm::define` / `bind` / `trace` / `trace_at` 的作用，尤其是 `bind` 在插入绑定时附加的两件「副作用」（`is` 标识符警告、`trace_at` 调用）；
- `hint_if_shadowed_std` 如何在「用户变量遮蔽了标准库函数」时给出修复提示。

如果你想提前热身，可以：

- 跳到 [src/vm.rs:47-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L47-L91) 通读 `Vm` 的方法，猜猜 `trace_at` 为什么「只在被追踪 span 上才真正记录」；
- 浏览 [src/lib.rs:177-184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L177-L184) 的 `Eval` trait 定义，留意关联类型 `type Output`。

完成 u1-l4 后，第 1 单元就结束，你将带着「入口 + Eval + Vm」三件套进入第 2 单元（各类表达式的具体求值）。
