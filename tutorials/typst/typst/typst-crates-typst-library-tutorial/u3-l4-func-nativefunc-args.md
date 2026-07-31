# func 宏、NativeFunc 与 Args

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清一个普通的 Rust `fn` 是如何通过 `#[func]` 宏变成 Typst 标准库里可调用的「原生函数」的，以及宏在编译期生成了哪几块代码。
- 读懂 `Func` / `FuncInner` / `NativeFunc` / `NativeFuncData` 这一组运行时表示，理解 `Func::call` 如何把一次函数调用分派到不同种类的函数。
- 掌握 `Args` 的内部结构，并能解释位置参数、命名参数、可变参数（variadic）分别由哪几个方法按什么顺序消费。
- 理解 `#[scope]` 宏与 `#[func]` 宏如何协作，从而让一个函数既能被调用（如 `assert(...)`），又能挂载子函数（如 `assert.eq(...)`）。

本讲是「Content 与元素系统」单元里关于「函数」的收口篇。元素（`#[elem]`）在上一讲（u3-l3）已经讲过，本讲专讲与之并列的另一类原生定义——原生函数。

## 2. 前置知识

本讲默认你已经学过：

- **u2-l3 类型转换系统**：`Reflect` / `IntoValue` / `FromValue` / `CastInfo` 三段式转换模型。本讲里函数参数的「解析」就是反复调用 `FromValue`。
- **u3-l1 Content 与 RawContent**：`Content` 是所有标记与函数调用的产物。元素函数（`#[elem]`）被调用后产出 `Content`。
- **u3-l3 elem 宏、字段系统与 Packed**：`#[elem]` 宏与 `#[func]` 宏是姐妹宏，生成结构高度相似（都生成「影子类型 + 静态数据描述 + 包装闭包」）。理解了 `#[elem]`，本讲的 `#[func]` 会非常自然。

几个本讲要用到的术语先做一个最简解释：

- **原生函数（native function）**：用 Rust 写好、经 `#[func]` 宏注册、对 Typst 用户可见的函数，例如 `panic`、`assert`、`eval`、`rect`。
- **闭包（closure）**：用户在 Typst 代码里用 `let f(x) = ...` 或 `(x) => ...` 定义的函数。它和原生函数是 `Func` 的两种不同内部表示。
- **元素函数（element function）**：和某个元素绑定的函数，调用后产生该元素的 `Content`，还能用于 `set`/`show`/`where`。它和「普通原生函数」是 `Func` 内部并列的两类。

## 3. 本讲源码地图

本讲涉及的关键文件如下（前三个是本讲的核心，后四个是支撑）：

| 文件 | 作用 |
| --- | --- |
| `src/foundations/func.rs` | `Func` / `FuncInner` / `NativeFunc` / `NativeFuncData` / `Func::call` 的定义，以及 `#[scope] impl Func` 上的 `with` / `where_`。 |
| `src/foundations/args.rs` | `Args` / `Arg` 的结构，以及消费位置/命名/可变参数的一组方法，还有 `Args` 自身作为类型的方法（`pos` / `named` / `at` 等）。 |
| `src/foundations/mod.rs` | `foundations::define` 的注册入口，以及 `panic` / `assert` / `eval` 三个原生函数的真实实现，是本讲最主要的示例来源。 |
| `src/foundations/scope.rs` | `define_func` / `define_func_with_data` / `NativeScope` trait，说明函数如何被装进作用域。 |
| `src/foundations/cast.rs` | `IntoResult`（把函数返回值统一成 `SourceResult<Value>`）、`Container`（取 `Option`/`Vec` 的内部类型）、`Never`（不可达返回类型）。 |
| `crates/typst-macros/src/func.rs`（另一 crate） | `#[func]` 过程宏的实现。 |
| `crates/typst-macros/src/scope.rs`（另一 crate） | `#[scope]` 过程宏的实现。 |

> 提示：`#[func]` 与 `#[scope]` 两个宏定义在 `typst-macros` crate 里，在 `typst-library` 中被 reexport——`func` 在 [func.rs:2](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L2) reexport，`scope`/`ty` 在 [mod.rs:71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L71) reexport（`func` 不在这里）。本讲会同时引用两个 crate 的源码。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**func 宏**、**NativeFunc 与 NativeFuncData**、**Args**、**scope 宏**。

### 4.1 func 宏：从 Rust 函数到标准库函数

#### 4.1.1 概念说明

在 Typst 标准库里，`panic`、`assert`、`eval` 这些函数其实就是一个个普通的 Rust `fn`。但 Typst 运行时不能直接调用任意 Rust 函数——它需要一个统一的调用签名（接收 `Engine`、`Context`、`Args`，返回 `SourceResult<Value>`），还需要一份描述这个函数的元数据（名字、参数、文档、版本）。

`#[func]` 就是在编译期自动补齐这些东西的过程宏。你只写业务逻辑，宏帮你生成：

1. **一个「影子类型」**：一个与函数同名的零变体枚举（如 `enum panic {}`）。它纯粹用来在类型层面「指代」这个函数，便于用 `define_func::<panic>()` 注册。
2. **一份静态元数据 `NativeFuncData`**：包含函数指针、名字、文档、参数表等。
3. **一个包装闭包**：负责把运行时收到的 `Args` 按参数顺序解析出来，再转交给你的真实函数。
4. **`impl NativeFunc`**：把上面三者粘起来。

这与上一讲 `#[elem]` 宏生成的「影子类型 + 静态 `ContentVtable` + `Construct`/`Set`」结构几乎是同构的，可以对照理解。

#### 4.1.2 核心流程

`#[func]` 宏的处理流程（对应 [func.rs:14-17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L14-L17) 的入口 `func()` → `parse()` → `create()`）：

```text
#[func(...)] pub fn f(engine, a: T, #[named] b: U, #[variadic] rest: Vec<V>) -> R
        │
        ▼  parse()
   读取 #[func(...)] 元信息（name/title/since/scope/keywords/...）
   扫描每个参数，分出：
     · 特殊参数  : self / engine / context / args / span  （运行时注入，不暴露给 Typst）
     · 用户参数  : 普通位置 / #[named] / #[variadic] / #[external] / #[default]
        │
        ▼  create()
   ① rewrite_fn_item   去掉参数上的属性，得到干净的 fn
   ② create_func_ty    生成影子类型 enum f {}（仅当没有 parent 时）
   ③ create_func_data  生成 static NativeFuncData { function, name, params, scope, ... }
        · 其中 function = 指向 create_wrapper_closure 生成的闭包
   ④ impl NativeFunc for f { fn data() -> &'static NativeFuncData { &DATA } }
```

其中最关键的是 `create_wrapper_closure`（[func.rs:375-423](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L375-L423)）：它为每个用户参数生成一段「解析代码」（handler），按声明顺序逐个从 `Args` 里取值，取完后再调用你的真实函数。不同标注对应不同的取值方式，规则集中在 `create_param_parser`（[func.rs:463-481](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L463-L481)）：

| 参数标注 | 生成的取值代码 | 含义 |
| --- | --- | --- |
| `#[variadic]` | `args.all()?` | 取走全部剩余位置参数 |
| `#[named]` | `args.named(name)?` | 按名字取（命名参数） |
| 有 `#[default]` | `args.eat()?` 然后 `unwrap_or_else(default)` | 可选位置参数，没有就用默认值 |
| 都没有（必填位置） | `args.expect(name)?` | 取下一个位置参数，缺失则报错 |

参数解析完成后，如果你的函数没有声明 `args` 特殊参数，宏还会自动插入 `args.take().finish()?`，用来对「多余参数」报错（unexpected argument）。

#### 4.1.3 源码精读

**入口与元信息解析。** `#[func(...)]` 括号里的内容被解析成 `Meta`（[func.rs:102-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L102-L136)），支持 `scope` / `contextual` / `name` / `title` / `constructor` / `since` / `keywords` / `parent` 等键。这些直接对应后面 `NativeFuncData` 的各个字段。

**生成影子类型。** 当函数没有 `parent`（即不是某个作用域里的子函数）时，宏会生成一个零变体枚举（[func.rs:361-372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L361-L372)）：

```rust
#[doc(hidden)]
pub enum panic {}   // 影子类型，与 fn panic 同名，处在「类型命名空间」
```

这里有个 Rust 小知识：函数名 `fn panic` 处于「值命名空间」，枚举 `enum panic` 处于「类型命名空间」，二者同名但互不冲突。所以后面写 `define_func::<panic>()` 时，`panic` 解析到的是枚举类型。

**生成静态数据。** `create_func_data` 把所有元信息填进 `NativeFuncData`（[func.rs:293-358](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L293-L358)）。注意其中三项用了惰性初始化：`scope`、`params`、`returns` 都包在 `LazyLock` 里，避免程序启动时就把所有函数的描述都算出来。

**真实示例：`panic`。** 看 [mod.rs:125-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L125-L155) 的 `panic` 函数（精简后）：

```rust
#[func(since = "forever", keywords = ["error"])]
pub fn panic(
    #[variadic]
    values: Vec<Value>,
) -> StrResult<Never> {
    // ... 拼接 "panicked with: ..." 字符串
    Err(msg)
}
```

对照 `create_param_parser` 的规则，宏为它生成的包装闭包大致等价于：

```rust
// 示例代码：#[func] 为 panic 生成的包装闭包（手写还原，非源码原文）
|engine, context, args| {
    let __typst_func = panic;            // 真实函数
    let mut values: Vec<Value> = args.all()?;   // values 是 #[variadic]
    args.take().finish()?;               // panic 没有声明 args 特殊参数，故自动 finish
    let output = __typst_func(values);   // 调用真实函数
    IntoResult::into_result(output, args.span) // 把 StrResult<Never> 统一成 SourceResult<Value>
}
```

其中 `StrResult<Never>` 的统一化由 `IntoResult` 完成（见 4.2.3）。`Never` 是一个不可达类型（[cast.rs:437-439](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L437-L439) `pub enum Never {}`），用来表达「这个函数只可能出错、不可能正常返回」。

#### 4.1.4 代码实践

**实践目标**：用一个「逆向还原」练习，验证你理解了 `#[func]` 宏的取值规则。

**操作步骤**：

1. 打开 [mod.rs:256-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L256-L322) 的 `eval` 函数，记下它的参数：`engine`（特殊）、`source: Spanned<String>`（必填位置）、`#[named] #[default(SyntaxMode::Code)] mode`、`#[named] #[default] scope: Dict`。
2. 对照 [func.rs:463-481](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L463-L481) 的取值规则，在纸上写出宏为 `eval` 生成的 handler 顺序。
3. 检查：`engine` 是特殊参数，不生成 handler；`source` 是必填位置 → `args.expect("source")`；`mode` 有 `#[named]` 且有默认 → `args.named("mode")?` 再 `unwrap_or_else(...)`；`scope` 同理。

**需要观察的现象 / 预期结果**：你写出的顺序应当是

```text
let mut source: Spanned<String> = args.expect("source")?;
let mut mode: SyntaxMode = args.named("mode")?.unwrap_or_else(|| SyntaxMode::Code);
let mut scope: Dict = args.named("scope")?.unwrap_or_else(|| Default::default());
args.take().finish()?;
let output = __typst_func(engine, source, mode, scope);
```

> 待本地验证：如果本地装了 `cargo-expand`，可以尝试 `cargo expand -p typst-library foundations::mod 2>/dev/null | grep -A30 'fn eval'`（展开结果依赖工具版本，可能不精确；以源码规则为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么需要「影子类型」`enum panic {}`？直接用函数指针不行吗？

**参考答案**：注册时需要一个稳定的「类型层面的名字」作为泛型参数（`define_func::<panic>()`），而函数处在值命名空间、不能直接当类型用。影子枚举提供了这个类型层面的占位符，同时也作为 `impl NativeFunc` 的 `Self`，把「函数指针 + 元数据」绑成一个可静态寻址的整体。

**练习 2**：如果一个参数同时标了 `#[variadic]` 和 `#[named]`，会怎样？

**参考答案**：看 `create_param_parser`（[func.rs:466-468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L466-L468)），判断顺序是「先 variadic，再 named」。所以 `variadic` 优先，会生成 `args.all()?`。实际代码里不会这么标注（语义冲突），这里只是用来理解判断优先级。

---

### 4.2 NativeFunc 与 NativeFuncData：函数的运行时表示

#### 4.2.1 概念说明

`#[func]` 宏在编译期生成了元数据；运行时，所有的函数值都统一用 `Func` 表示。`Func` 是一个外壳，里面装着 `FuncInner`——一个枚举，区分五种不同的函数来源。理解这个枚举，就理解了「Typst 眼中的函数到底有哪几种」。

`NativeFunc` / `NativeFuncData` 则专门描述「原生函数」这一种：一份静态的描述表 + 一个统一签名的函数指针。

#### 4.2.2 核心流程

一次函数调用 `f(args)` 在运行时的路径：

```text
Func::call(engine, context, args)
        │  把 args 转成 Args（IntoArgs）
        ▼
Func::call_impl   ← match self.inner
        │
        ├── Native(data)  → (data.function.0)(engine, context, &mut args)?  再 args.finish()?
        ├── Element(elem) → elem.construct(engine, &mut args)?              再 args.finish()?  返回 Content
        ├── Closure(c)    → (routines.eval_closure)(...)   ← 行为在 typst-eval，经 Routines 回调
        ├── Plugin(p)     → args.all::<Bytes>()?  p.call(inputs)?            返回 Bytes
        └── With(with)    → 把预绑参数拼到 args 前面，再 with.0.call(...)
```

关键点：**只有原生函数和元素函数的调用逻辑在 `typst-library` 本地**；闭包的求值行为在 `typst-eval` crate，通过 `engine.library.routines.eval_closure` 这个函数指针回调（这是 u1-l1 讲过的「crate 分离 + Routines 动态链接」机制）。所以 `Func::call_impl` 是一个收口的分派器。

#### 4.2.3 源码精读

**`Func` 外壳与五种内部表示。** 见 [func.rs:138-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L138-L160)：

```rust
pub struct Func {
    inner: FuncInner,           // 真正的函数表示
    span: Span,                 // 报错时归属的 span
}

enum FuncInner {
    Native(Static<NativeFuncData>),  // 原生函数（本讲重点）
    Element(Element),                // 元素函数（如 heading、rect）
    Closure(Arc<LazyHash<Closure>>), // 用户闭包
    Plugin(Arc<PluginFunc>),         // WASM 插件函数
    With(Arc<(Func, Args)>),         // 预绑了部分参数的函数（func.with(..)）
}
```

`Static<T>` 表示一个 `'static` 的指向（这里是进程内唯一的 `NativeFuncData`），所以两个原生函数相等只需比较指针即可（见 [func.rs:477-484](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L477-L484)）。

**`Func::call` 与分派。** [func.rs:323-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L323-L375)：

```rust
pub fn call<A: IntoArgs>(&self, engine: &mut Engine, context: Tracked<Context>, args: A)
    -> SourceResult<Value>
{
    self.call_impl(engine, context, args.into_args(self.span))
}
```

`call_impl` 对 `Native` 分支的处理是：

```rust
FuncInner::Native(native) => {
    let value = (native.function.0)(engine, context, &mut args)?;  // 调包装闭包
    args.finish()?;                                                 // 报告多余参数
    Ok(value)
}
```

注意 `args.finish()?` 这里是**第二道**「多余参数」检查：第一道在包装闭包内部（`args.take().finish()?`，仅当函数没有声明 `args` 特殊参数时插入），第二道在 `call_impl` 里。这是双重保险。

**`NativeFunc` trait 与 `NativeFuncData`。** `NativeFunc`（[func.rs:619-629](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L619-L629)）就是把「影子类型」和它的数据描述连起来：

```rust
pub trait NativeFunc {
    fn func() -> Func { Func::from(Self::data()) }
    fn data() -> &'static NativeFuncData;
}
```

`NativeFuncData`（[func.rs:631-656](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L631-L656）是那张描述表，字段含义如下：

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| `function: NativeFuncPtr` | 包装闭包的函数指针 | `create_wrapper_closure` |
| `name` / `title` | 用户可见名 / 文档标题名 | `#[func(name=.., title=..)]` |
| `since` | 引入版本 | `#[func(since=..)]` |
| `docs` | 文档字符串 | 函数上方的 `///` 注释 |
| `keywords` | 搜索关键词 | `#[func(keywords=[..])]` |
| `contextual` | 是否依赖 context | `#[func(contextual)]` |
| `scope` | 子定义作用域 | `#[func(scope)]` + `#[scope] impl`（见 4.4） |
| `params` | 参数表 | 由各参数标注生成 |
| `returns` | 返回值描述 | `<R as Reflect>::output()` |

`function` 字段的类型 `NativeFuncPtr`（[func.rs:663-668](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L663-L668)）就是统一签名：

```rust
type NativeFuncSignature =
    dyn Fn(&mut Engine, Tracked<Context>, &mut Args) -> SourceResult<Value> + Send + Sync;
```

**返回值如何统一。** 业务函数返回 `Value`、`StrResult<T>`、`SourceResult<T>` 等各种类型，统一交给 `IntoResult`（[cast.rs:214-241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L214-L241)）：普通值直接 `into_value`；`StrResult` / `HintedStrResult` 把字符串错误用 `.at(span)` 附上 span 变成 `SourceDiagnostic`；`SourceResult` 直接透传。这就是为什么 `panic` 能写 `-> StrResult<Never>` 而 `eval` 写 `-> SourceResult<Value>`。

#### 4.2.4 代码实践

**实践目标**：追踪一次 `assert(1 < 2)` 的运行时分派路径。

**操作步骤**：

1. `assert` 在 [mod.rs:169-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L169-L185) 定义，经 [mod.rs:116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L116) 的 `global.define_func::<assert>()` 注册，注册时 [scope.rs:137-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L137-L140) 调 `T::data()` 拿到 `NativeFuncData`，包成 `Func::from(data)`，用 `data.name`（即 `"assert"`）作键放进作用域。
2. 当 Typst 求值 `assert(1 < 2)`：查作用域拿到这个 `Func`，调用 [func.rs:323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L323) 的 `Func::call`。
3. `call_impl` 命中 `Native` 分支（[func.rs:342-345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L342-L345)），执行包装闭包。
4. 闭包按 `assert` 的参数（`condition: bool` 必填位置、`#[named] message: Option<EcoString>`）解析 `args`，再调用真实 `assert`。

**预期结果**：你能画出从「用户写 `assert(1<2)`」到「真实 `assert` 函数体被执行」的完整调用链，并指出每一步落在哪个文件。

#### 4.2.5 小练习与答案

**练习 1**：`Func::call_impl` 里，`Element` 分支返回 `Value::Content(value)`，而 `Native` 分支直接返回 `value`。为什么元素函数要包一层 `Content`？

**参考答案**：元素函数（如 `rect`）的产物就是一个元素实例，即 `Content`；统一成 `Value::Content` 后才能进入通用的 `Value` 流通。普通原生函数可能返回任意 `Value`（数字、数组、`none` 等），所以直接返回 `value`。

**练习 2**：`FuncInner::With` 是干什么用的？举例说明。

**参考答案**：`With` 表示「预绑了部分参数的函数」，由 `Func::with` 产生（[func.rs:393-408](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L393-L408)）。调用时它把预绑参数拼到新参数前面再委托原函数（[func.rs:370-373](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L370-L373)）。典型例子是用户写 `let f = alert.with(fill: blue)`，之后 `f[...]` 等价于 `alert(fill: blue)[...]`。

---

### 4.3 Args：位置 / 命名 / 可变参数的容器与消费

#### 4.3.1 概念说明

`Args` 是「一次函数调用收到的全部参数」的运行时容器。Typst 的参数有三种形态：

- **位置参数**：按出现顺序识别，如 `list([A], [B])` 里的 `[A]`、`[B]`。
- **命名参数**：`name: value` 形式，如 `enum(start: 2)` 里的 `start: 2`。
- **可变参数（sink / variadic）**：用 `..sink` 收集剩余参数，如 `let f(..rest)`。

`Args` 把这三种形态统一存成一张扁平的列表，每项要么是位置、要么是带名字的命名。函数体（实际上是宏生成的包装闭包）通过一组「消费方法」按需把它们取走。

#### 4.3.2 核心流程

`Args` 的内部结构很简单（[args.rs:53-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L53-L61)）：

```rust
pub struct Args {
    pub span: Span,          // 整个函数调用的 span
    pub items: EcoVec<Arg>,  // 参数列表
}

pub struct Arg {
    pub span: Span,
    pub name: Option<Str>,   // None = 位置参数；Some = 命名参数的名字
    pub value: Spanned<Value>,
}
```

「位置 vs 命名」的区分就落在 `name` 是否为 `None`。所有消费方法都围绕「扫描 `items`，按规则取出并删除」展开：

| 方法 | 行为 | 典型用途 |
| --- | --- | --- |
| `expect::<T>(what)` | 取第一个位置参数并转型，缺失报错 | 必填位置参数 |
| `eat::<T>()` | 取第一个位置参数并转型，没有返回 `None` | 可选位置参数 |
| `find::<T>()` | 取第一个**可转型**的位置参数 | 类型可区分的可选位置 |
| `all::<T>()` | 取走**全部**位置参数并逐个转型 | `#[variadic]` |
| `named::<T>(name)` | 按名字取（同名全删，用最后一个） | `#[named]` |
| `named_or_find::<T>(name)` | 先按名字取，没有再 `find` | 既可命名又可位置的参数 |
| `finish()` | 若还有剩余参数则报「unexpected argument」 | 收尾检查 |

**取用顺序的核心规则**（这是本讲练习的重点）：

1. 宏按**参数声明顺序**生成 handler，逐个消费。
2. 位置类方法（`expect`/`eat`/`find`/`all`）都只看 `name.is_none()` 的项；它们会在列表里跳过命名参数。
3. `named` 只看名字匹配的项，**与位置无关**——所以命名参数写在哪个位置都能被正确取到。
4. 可变参数 `all()` 取走的是「前面位置参数被吃掉之后，剩下的全部位置参数」。
5. 全部消费后，`finish()` 兜底报告任何没被认领的参数。

举个例子，假设有函数 `fn f(a, b, #[named] c, #[variadic] rest)`，宏生成的消费顺序是：

```text
a = expect("a")   →  取第 1 个位置
b = expect("b")   →  取第 2 个位置
c = named("c")    →  删除所有名为 c 的项
rest = all()      →  取走剩余全部位置
finish()          →  此时若有剩余必为非法命名参数
```

对调用 `f(1, 2, 3, 4, c: 9, 5)`：`a=1, b=2`，`c` 把 `c:9` 删掉，`all()` 取走剩余位置 `[3,4,5]`，`finish()` 通过。

#### 4.3.3 源码精读

**`eat` 与 `expect`。** [args.rs:112-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L112-L124) 的 `eat` 遍历 `items`，找到第一个 `name.is_none()` 的项，`remove` 取出并转型；找不到返回 `None`。`expect`（[args.rs:150-158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L150-L158)）就是 `eat` 外加「没有就报错」，而且报错信息很贴心——`missing_argument`（[args.rs:161-174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L161-L174)）会检查用户是不是把本该写位置的参数误写成了同名命名参数，给出 `the argument 'x' is positional; hint: try removing 'x:'` 这样的提示。

**`all`（可变参数）。** [args.rs:192-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L192-L214) 用 `retain` 遍历：保留命名参数，把所有位置参数取走并逐个 `from_value` 转型，转型失败的错误收集起来统一返回。

**`named`。** [args.rs:218-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L218-L236) 关键细节在注释里：它**不会**在第一次匹配后就停，而是删掉所有同名项、用最后一个。这保证 `f(x: 1, x: 2)` 最终取到 `x: 2`，符合「同名命名参数后者覆盖前者」的语义。

**`finish`。** [args.rs:259-267](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L259-L267) 取剩余第一项报「unexpected argument」，对命名/位置分别给出不同措辞。

**`Args` 自身也是一个 Typst 类型。** 它有自己的 `#[scope] impl Args`（[args.rs:320-449](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L320-L449)），暴露给用户的方法有 `pos`（[args.rs:374-381](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L374-L381)，返回位置参数数组）、`named`（[args.rs:384-390](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L384-L390)，返回命名参数字典）、`at`、`len`、`filter`、`map`，以及构造器 `construct`（[args.rs:330-339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L330-L339)）。用户写的 `let f(..sink) = { sink.pos() }` 里，`sink` 就是 `Args`，`.pos()` 就是这里的 `to_pos`。

注意区分两组同名方法：**消费型** `Args::named`（`&mut self`，从 `args` 里删除）供函数实现内部使用；**查询型** `Args::to_named`（`&self`，不修改，标了 `#[func(name="named")]`）供用户在 Typst 里调用。

#### 4.3.4 代码实践

**实践目标**：用一个带三类参数的真实函数，验证取用顺序。

**操作步骤**：

1. 看 [mod.rs:135-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L135-L155) 的 `panic`：它只有一个 `#[variadic] values: Vec<Value>`。所以调用 `panic("a", "b", x: 1)` 时，`all()` 会取走位置参数 `["a","b"]`，但 `x: 1` 留在 `items` 里。
2. 接着 `args.take().finish()?`（包装闭包自动插入）会检测到剩余的 `x: 1`，报 `unexpected argument: x`。
3. 想看一个同时有三类参数的例子，参考 `eval`（[mod.rs:266-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L266-L322)）：必填位置 `source`、命名带默认 `mode`、命名带默认 `scope`。

**需要观察的现象 / 预期结果**：

- `panic("a", "b")` → 正常报错 `panicked with: a, b`。
- `panic("a", x: 1)` → 报 `unexpected argument: x`（因为 `all()` 不吃命名参数，`finish()` 兜底）。
- `eval("1+1")` → `source="1+1"`，`mode`/`scope` 用默认值。
- `eval("= 标题", mode: "markup")` → `mode` 被命名取走，`source` 仍是位置参数。

> 待本地验证：上述 Typst 调用行为可用 `typst compile` 或 REPL 验证；以源码 `create_param_parser` 规则为权威依据。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `named` 要删掉所有同名项而不是匹配第一个就停？

**参考答案**：因为 Typst 规定「同名命名参数后者覆盖前者」。如果只删第一个，剩余的同名项会被 `finish()` 当成「unexpected argument」误报。删掉全部、保留最后一个值，既实现了覆盖语义，又避免了误报。

**练习 2**：`eat` 和 `find` 都能取「可选位置参数」，区别是什么？

**参考答案**：`eat`（[args.rs:112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L112)）取第一个位置参数并尝试转型，转型失败会报错；`find`（[args.rs:177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L177)）只取第一个**转型能成功**（`T::castable`）的位置参数，跳过不能转型的。`find` 适用于「多个不同类型的位置参数混在一起、按类型区分」的场景。

---

### 4.4 scope 宏与函数作用域：把子函数挂到函数上

#### 4.4.1 概念说明

有些函数自带一组「附属定义」，比如 `assert` 下面有 `assert.eq` / `assert.ne`，`list` 下面有 `list.item`，`counter` 对象上有 `.step()` / `.get()` 等方法。Typst 把这种「函数/类型上的子命名空间」叫做**函数作用域（function scope）**。

`#[scope]` 宏就是用来收集一个 `impl` 块里的成员（常量、函数、类型/元素），把它们装进一个 `Scope` 并实现 `NativeScope` trait。它和 `#[func]` 宏协作：`#[func]` 标注 `scope` 标志位的函数会去调用 `NativeScope::scope()` 来获取自己的子作用域。

#### 4.4.2 核心流程

以 `assert` 为例，协作关系是：

```text
① 顶层函数加 scope 标志：
      #[func(scope, ...)] pub fn assert(...) {...}
   → create_func_data 里：scope = <assert as NativeScope>::scope()

② 用 #[scope] 提供该作用域的实现：
      #[scope] impl assert {
          #[func] pub fn eq(...) {...}     // 子函数
          #[func] pub fn ne(...) {...}
      }
   → scope 宏把每个 #[func] 的 parent 改写成 assert，
     生成 impl NativeScope for assert {
         fn scope() -> Scope {
             let mut scope = Scope::deduplicating();
             scope.define_func_with_data(assert::eq_data());   // 注册 assert.eq
             scope.define_func_with_data(assert::ne_data());   // 注册 assert.ne
             scope
         }
     }

③ 注册主函数：
      global.define_func::<assert>();
   → 作用域里放入 Func(assert)，其 .scope() 即上面收集到的 { eq, ne }
```

用户写 `assert.eq(10, 10)` 时，求值器先取 `assert`（一个 `Func`），再对它做字段访问 `assert.eq`——这会走 [func.rs:291-305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L291-L305) 的 `Func::field`，到 `Func` 的 `scope` 里查出 `eq` 这个绑定并返回。

#### 4.4.3 源码精读

**`#[func]` 侧如何声明 scope。** `create_func_data` 里（[func.rs:317-321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L317-L321)）：

```rust
let scope = if *scope {
    quote! { <#ident as #foundations::NativeScope>::scope() }
} else {
    quote! { #foundations::Scope::new() }
};
```

也就是说，`#[func(scope)]` 让函数的数据里 `scope` 字段指向「同名类型的 `NativeScope::scope()`」；不加 `scope` 就是空作用域。`NativeScope` trait 定义在 [scope.rs:240-246](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L240-L246)，有两个方法：`constructor()`（类型的构造函数，可选）和 `scope()`。

**`#[scope]` 侧如何收集成员。** 入口 [scope.rs:11-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L11-L116) 遍历 `impl` 块的每个成员：

- `const` → `scope.define(name, Self::CONST)`（[scope.rs:132-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L132-L136)）；
- 带 `#[elem]` 的类型 → `scope.define_elem::<T>()`，否则 → `scope.define_type::<T>()`（[scope.rs:139-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L139-L147)）；
- `#[func]` 函数 → `scope.define_func_with_data(Self::fn_data())`（[scope.rs:151-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L151-L175)）。

最后生成（[scope.rs:100-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L100-L116)）：

```rust
impl NativeScope for assert {
    fn constructor() -> Option<&'static NativeFuncData> { None } // 或 Some(..) 若有 #[func(constructor)]
    fn scope() -> Scope {
        let mut scope = Scope::deduplicating();
        scope.define_func_with_data(assert::eq_data());
        scope.define_func_with_data(assert::ne_data());
        scope
    }
}
```

**子函数的影子类型去哪了？** 注意：作用域里的子函数（如 `eq`）**不会**生成 `enum eq {}` 影子类型。因为 `handle_fn` 会给它的 `#[func]` 加上 `parent = #self_ty`（[scope.rs:159-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L159-L167)），而 `create_func_ty` 在「有 parent」时直接返回 `None`（[func.rs:361-364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L361-L364)），改而生成一个 `eq_data()` 函数（[func.rs:269-278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L269-L278)）来返回数据。这就是为什么 `#[scope]` 里写的是 `assert::eq_data()` 而不是 `define_func::<eq>()`。

**真实示例：`assert` 家族。** 见 [mod.rs:157-254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L157-L254)：顶层 `assert` 标了 `#[func(scope, ...)]`，下面 `#[scope] impl assert` 里有两个 `#[func]` 子函数 `eq` / `ne`。三者各自有自己的参数（`assert` 有 `condition` + `#[named] message`；`eq`/`ne` 有 `left`/`right` + `#[named] message`），都返回 `StrResult<NoneValue>`。

**构造器特殊情形。** 如果 `#[scope] impl` 里有 `#[func(constructor)]`，它不会被注册为普通子函数，而是成为该**类型**的构造函数，挂到 `NativeScope::constructor()` 上（[scope.rs:167-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L167-L169)）。例子见 `Args::construct`（[args.rs:330-339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L330-L339)），它是 `arguments(...)` 类型的构造函数。

#### 4.4.4 代码实践

**实践目标**：把 `#[func]` 与 `#[scope]` 的协作完整说清楚。

**操作步骤**：

1. 打开 [mod.rs:157-254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L157-L254)，确认 `assert` 的三件套：顶层 `#[func(scope)] pub fn assert`、`#[scope] impl assert`、里面的 `#[func] eq` / `#[func] ne`。
2. 回答三个问题（见下方预期结果）。

**预期结果**：

- **`assert` 本身怎么被调用？** 由 [mod.rs:116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L116) `define_func::<assert>()` 注册，用户 `assert(cond)` 走 `Func::call` → `Native` 分支 → 包装闭包。
- **`assert.eq` 怎么被解析？** `assert` 的 `Func` 的 `scope()`（由 `#[scope]` 生成）里装着 `eq`，用户 `assert.eq(...)` 先字段访问 `assert.eq` 取出子 `Func`，再调用它。
- **`eq` 为什么没有影子类型？** 因为它有 `parent = assert`，宏只生成 `assert::eq_data()` 函数，不生成 `enum eq {}`。

> 这是个纯源码阅读型实践，无需运行；结论可直接对照 `typst-macros/src/func.rs` 与 `scope.rs` 验证。

#### 4.4.5 小练习与答案

**练习 1**：`#[scope]` 既能写在 `impl Func` 上（给 `Func` 这个类型挂方法，如 `func.with`），也能写在 `impl assert` 上（给某个函数挂子函数）。这两者的共同点是什么？

**参考答案**：两者都生成了 `impl NativeScope for <Self类型>`，把 `impl` 块里的成员收进一个 `Scope`。差别只在「Self 类型」是什么：对 `Func` 是标准库的函数类型本身（所以 `func.with` 是所有函数都有的方法），对 `assert` 是那个函数的影子枚举类型（所以 `assert.eq` 只有 `assert` 才有）。

**练习 2**：如果把 `#[func(constructor)]` 加到 `#[scope] impl` 里的某个函数上，它和普通 `#[func]` 子函数的注册方式有何不同？

**参考答案**：普通子函数通过 `scope.define_func_with_data(..)` 注册进作用域，用户用 `parent.fn` 访问；构造函数不进作用域，而是挂到 `NativeScope::constructor()`，用于「以类型名当函数调用」的构造（如 `arguments(...)` 调用 `Args::construct`）。

---

## 5. 综合实践

**任务**：以 `foundations/mod.rs` 里的 `panic` / `assert` / `eval` 为样本，整理出一张「从 Typst 调用到 Rust 执行」的全景表，并在 fork 中仿写一个最小的原生函数。

**步骤**：

1. **填表**（对照本讲源码完成）：

   | 函数 | `#[func]` 关键标注 | 参数种类（按声明顺序） | 是否有 `#[scope]` | 返回类型 | 生成影子类型？ |
   | --- | --- | --- | --- | --- | --- |
   | `panic` | `since`, `keywords` | variadic `values` | 否 | `StrResult<Never>` | 是 |
   | `assert` | `scope`, `since` | 必填位置 `condition`，`#[named] message` | 是 | `StrResult<NoneValue>` | 是 |
   | `assert.eq` | `title`, `since`（`parent=assert`） | 必填位置 `left`/`right`，`#[named] message` | — | `StrResult<NoneValue>` | 否（只生成 `eq_data()`） |
   | `eval` | `title`, `since` | 特殊 `engine`，必填位置 `source`，`#[named]#[default] mode`，`#[named]#[default] scope` | 否 | `SourceResult<Value>` | 是 |

2. **画调用链**：任选 `assert.eq(10, 10)`，画出从「求值器取 `assert.eq`」到「`eq` 函数体执行」的完整路径，标出每一步所在文件与行号（提示：会经过 `Func::field` → 取子 `Func` → `Func::call` → `call_impl` 的 `Native` 分支 → 包装闭包 → `eq`）。

3. **仿写（可选，需 fork）**：在 `foundations` 目录仿照 `panic` 新增一个原生函数，例如：

   ```rust
   // 示例代码：仿写一个原生函数（需自行注册到 define）
   #[func(since = "forever")]
   pub fn shout(
       /// 要大喊的内容
       text: Str,
   ) -> Str {
       text.to_uppercase()
   }
   ```
   然后在 [mod.rs:114-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L114-L119) 的 `define` 里加一行 `global.define_func::<shout>();`，重新编译后即可在 Typst 里调用 `#shout("hi")`。

   > 待本地验证：编译改动需要完整构建 typst（`cargo build`），属较重操作；本步骤主要用来检验你是否掌握了「写函数 → 标 `#[func]` → `define_func` 注册」三步法。

**验收标准**：表格无遗漏、调用链每一步都能指出文件与行号、仿写函数能正确推断出宏生成的取值代码（`args.expect("text")` → `args.take().finish()`）。

## 6. 本讲小结

- `#[func]` 在编译期为普通 Rust `fn` 生成四样东西：清洗后的 `fn`、影子类型（如 `enum panic {}`）、静态 `NativeFuncData`、以及一个把 `Args` 解析成具体参数再调用真实函数的包装闭包。
- 参数取值规则集中在 `create_param_parser`：`#[variadic]`→`all()`、`#[named]`→`named()`、有 `#[default]`→`eat()`+默认、必填位置→`expect()`；取完若函数没声明 `args` 特殊参数则自动 `finish()` 报多余参数。
- 运行时所有函数统一为 `Func`，其 `FuncInner` 区分原生 / 元素 / 闭包 / 插件 / 预绑五类；`Func::call_impl` 是分派器，闭包求值经 `routines` 函数指针回调到 `typst-eval`。
- `Args` 是一张扁平的 `Arg` 列表，靠 `name` 是否为 `None` 区分位置/命名；同名命名参数全删取最后一个、位置参数按声明顺序消费、可变参数取走剩余全部位置。
- `#[scope]` 收集 `impl` 块成员实现 `NativeScope`，配合 `#[func(scope)]` 标志让函数拥有子作用域（如 `assert.eq`）；子函数因带 `parent` 不生成影子类型，只生成 `fn_data()`。
- 消费型方法（供实现内部用，`&mut self`）与查询型方法（供用户用，标 `#[func]`）同名但职责不同，注意区分。

## 7. 下一步学习建议

- **进入样式系统（u4）**：`#[func]` 解决了「函数与参数」，下一讲 `Styles / StyleChain / fold / resolve` 解决「`set` 规则如何变成样式并折叠」。元素函数的 `#[elem]` 字段标注（`#[fold]`、`#[default]`、ghost 等）与样式链紧密耦合，建议把 u3-l3 和本讲一起复习后再读 u4。
- **读更多原生函数实现**：挑 `src/foundations/calc.rs` 或 `src/loading/*.rs` 里的函数练手，对照本讲的取值规则预测它们的包装闭包，再用 `cargo expand` 验证。
- **对比 `#[elem]` 与 `#[func]`**：回看 `typst-macros/src/elem.rs` 与 `func.rs`，体会二者「影子类型 + 静态描述表 + 包装/构造」的同构设计，这对你在 u12-l3 动手扩展标准库是关键基础。
