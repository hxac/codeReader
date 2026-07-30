# #\[time\] 计时宏：最简单的代码注入

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `#[time]` 这个属性宏从「用户写下的源码」到「生成出的 TokenStream」之间发生了什么。
- 解释 `time::Meta` 如何借助 `util.rs` 里的 `parse_string` / `parse_key_value` 解析 `#[time(..)]` 括号中的 `name` 与 `span` 两个可选项。
- 理解 `time::create` 是如何**原地修改** `syn::ItemFn`，在函数体最前面 `insert(0, ..)` 注入一条 `let __scope = .. TimingScope .. ;` 语句的。
- 区分 `TimingScope::with_span(#name, Some(#span.into_raw()))` 与 `TimingScope::new(#name)` 两种构造方式的触发条件。
- 结合 `lib.rs` 的注释，说明为什么 `#[time]` 在 `wasm32` 目标下「被省略」，以及这个省略其实并不发生在本宏里。

本讲是整个 `typst-macros` 学习路线里第一个「真正生成代码」的宏。它只有 45 行，却完整展示了后续所有宏共享的工作流，是理解 `#[func]`、`#[elem]` 等复杂宏的最佳入口。

## 2. 前置知识

在进入源码前，先建立三个直觉。如果你已经学完 u1-l1、u1-l2、u1-l3，可以快速扫过本节。

### 2.1 属性宏（attribute macro）的签名

属性宏接收**两个**参数：`stream`（属性自身的参数，即 `#[time(这里的全部内容)]`）和 `item`（被装饰的代码项，即整个函数）。这一点与函数式宏 `cast!`（只收 `stream`）、派生宏 `#[derive(Cast)]`（只收 `item`）不同。详见 u1-l2。

### 2.2 RAII：用「作用域变量」自动收尾

`TimingScope` 是一个 RAII（Resource Acquisition Is Initialization）守卫：它在**构造时**记录「开始」事件，在**离开作用域被 drop 时**记录「结束」事件。所以只要在函数体开头创建一个绑定到局部变量 `__scope` 的守卫，让它活到函数返回，就能自动测出整个函数的耗时——不需要手动写「开始计时 / 结束计时」两条语句。本宏要做的，就是把这一行守卫「塞」进函数体最前面。

### 2.3 syn 的 AST 是可变的

`syn::ItemFn` 不是只读的语法树，它是一个普通的 Rust 结构体，字段可以被修改。`time::create` 正是利用这一点：拿到函数的 AST，往它的 `block.stmts`（函数体的语句列表）里插入一条新语句，再把整棵 AST 重新转回 TokenStream。这种「改 AST 再吐回去」的模式，是所有代码注入型宏的本质。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| `src/time.rs` | 本讲主角，仅 45 行。包含 `time::time`（入口）、`time::Meta`（参数解析）、`time::create`（代码注入）三部分。 |
| `src/util.rs` | 提供 `parse_string`、`parse_key_value`、`eat_comma` 与 `kw`（自定义关键字 `name` / `span`）。这些是上一讲 u1-l3 讲过的「键 × 值类型」正交矩阵。 |
| `src/lib.rs` | 提供 `#[time]` 的 `#[proc_macro_attribute]` 入口，以及解释 `wasm32` 省略行为的文档注释。 |

> 提醒：永久链接里的 `146a58329a30f6cd38978c22c6bf0e430d8962a1` 是当前 HEAD。本讲还会跨 crate 引用一个 `typst-timing` 的链接，用来展示「省略」机制真正落在哪里。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **模块 A**：`time::time` + `time::Meta` —— 解析 `#[time(..)]` 的参数（依赖 `parse_string` / `parse_key_value`）。
2. **模块 B**：`time::create` —— 把一条 `TimingScope` 语句注入函数体开头。
3. **模块 C**：`TimingScope` 注入语句的运行时归宿 —— `wasm32` 下为何「被省略」。

### 4.1 模块 A：`time::time` 与 `Meta` —— 解析括号里的参数

#### 4.1.1 概念说明

`#[time]` 可以单独使用，也可以带参数：

```ignore
#[time]
fn fibonacci(n: u64) -> u64 { .. }

#[time(span = span)]
fn fibonacci_spanned(n: u64, span: Span) -> u64 { .. }
```

括号里的内容被传进 `stream`。我们需要把它解析成一个中间结构 `Meta`，它只关心两个可选项：

- `name`：这条计时记录的名字（字符串字面量）。不写时默认取函数名。
- `span`：这次调用对应的源码 span（任意表达式）。不写时为 `None`。

为什么要先解析成 `Meta` 这种「中间结构」，而不是直接边读边生成代码？因为后续 `create` 需要**先决定**用哪种构造方式（带 span 还是不带），再据此生成不同的 TokenStream。把「解析」和「生成」拆开，正是 typst-macros 全部七个宏共用的 `parse → create` 流水线的雏形（u4-l5 会总览这条流水线）。

#### 4.1.2 核心流程

```
#[time(name = "fib", span = s)]  fn fib() { .. }
        └────── stream ──────┘     └ item ┘┘ fn body ┘
              │
              ▼  syn::parse2::<Meta>(stream)
        Meta { name: Some("fib"), span: Some(s) }
              │
              ▼  time::create(meta, item)
        （见模块 B）
```

`Meta` 的 `Parse` 实现按固定顺序消费 stream：

1. 先尝试解析 `name = "字符串"`（用 `parse_string::<kw::name>`）。
2. 再尝试解析 `span = 表达式`（用 `parse_key_value::<kw::span, syn::Expr>`）。

两者都用「先 peek 再决定要不要消费」的策略，因此**缺省任何一项都合法**。但顺序是写死的：如果两项都给，`name` 必须在 `span` 之前，否则 `syn::parse2` 会因为 stream 没被完整消费而报「unexpected token」错误（详见 4.1.4 实践）。

#### 4.1.3 源码精读

先看入口函数 `time::time`，它只做「解析 + 转交」两件事：

[src/time.rs:9-12](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/time.rs#L9-L12) —— 把 `stream` 解析成 `Meta`，再交给 `create`；返回 `syn::Result`，出错由入口的 `to_compile_error` 回传（见 u1-l2）。

注意第 10 行用的是 `syn::parse2(stream)`（而不是 `parse_macro_input!`）。因为 `parse_macro_input!` 只能用在 `#[proc_macro]` 入口函数体内（lib.rs 那一层）；这里已经身处子模块，拿到的是 `proc_macro2::TokenStream`，所以用 `parse2`。

接着看 `Meta` 本身与它的 `Parse`：

[src/time.rs:15-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/time.rs#L15-L27) —— `Meta` 只有两个 `Option` 字段；`Parse::parse` 依次调用 `parse_string::<kw::name>` 和 `parse_key_value::<kw::span, syn::Expr>`。

这两个 helper 都来自 `util.rs`，是 u1-l3 讲过的「键 × 值类型」正交矩阵的成员：

[src/util.rs:136-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L136-L140) —— `parse_string<K>` 其实就是 `parse_key_value::<K, syn::LitStr>` 再 `.map(|s| s.value())`，所以 `name` 必须是字符串字面量。

[src/util.rs:114-126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L114-L126) —— `parse_key_value<K, V>` 先用 `peek` 探测关键字 `K` 是否出现：没出现就立刻返回 `Ok(None)` 且**不消费任何 token**；出现了才依次吃掉 `K`、`=`、值 `V`，最后 `eat_comma` 吃掉一个可选逗号。

`peek` 不消费这一行为非常关键——它让 `name` 缺省时 stream 原地不动，`span` 的解析才能正确从同一个位置继续。`eat_comma` 负责吃掉两项之间那个可选的逗号：

[src/util.rs:163-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L163-L167) —— 有逗号就吃，没有就放过。

最后是关键字本身。`kw::name` 与 `kw::span` 不是 Rust 内置关键字，而是用 `syn::custom_keyword!` 造出来的「自定义关键字」，这样 `peek(|_| K::default())` 才能识别 `name` / `span` 这两个字面标识符：

[src/util.rs:270-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L270-L282) —— `kw` 模块用 `syn::custom_keyword!` 集中声明了所有自定义关键字，本讲只用 `name`（271 行）和 `span`（272 行）。

> 小结一张「正交矩阵」表（承接 u1-l3）：

| helper | 关键字 K | 值类型 V | 用于 |
| --- | --- | --- | --- |
| `parse_string::<kw::name>` | `name` | `syn::LitStr` | `#[time(name = "fib")]` |
| `parse_key_value::<kw::span, syn::Expr>` | `span` | `syn::Expr` | `#[time(span = some_expr)]` |

#### 4.1.4 代码实践

这是一个**源码阅读 / 预测型**实践，无需运行任何命令。

1. **实践目标**：凭 `Meta::parse` 的逻辑，预测若干 `#[time(..)]` 写法解析出的 `Meta`，并找出会报错的写法。
2. **操作步骤**：对下面每一种写法，写出 `Meta { name, span }` 两个字段的值。
   - (a) `#[time]`
   - (b) `#[time(name = "fib")]`
   - (c) `#[time(span = span)]`
   - (d) `#[time(name = "fib", span = span)]`
   - (e) `#[time(span = span, name = "fib")]`
3. **需要观察的现象**：对 (e)，注意 `parse_string::<kw::name>` 首先在 `span` 处 `peek` 失败、返回 `None` 且不消费；随后 `parse_key_value::<kw::span, ..>` 消费掉 `span = span` 与其后逗号，留下 `name = "fib"` 未被消费。
4. **预期结果**：
   - (a) `name=None, span=None`
   - (b) `name=Some("fib"), span=None`
   - (c) `name=None, span=Some(span)`
   - (d) `name=Some("fib"), span=Some(span)`
   - (e) **编译错误**。`syn::parse2` 要求整段 stream 被完整消费，残留的 `name = "fib"` 会触发类似 `unexpected token` 的错误。
5. 结论：**两个字段都给时，`name` 必须写在 `span` 前面**。这个顺序约束并非来自注释，而是 `Parse::parse` 里两行代码的书写顺序决定的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Meta::parse` 里两行的顺序对调（先解析 `span` 再解析 `name`），用户代码需要怎样相应调整？

**答案**：对调后，两个字段都给时必须写成 `#[time(span = s, name = "fib")]`，即 `span` 在前。报错写法会反过来变成 `#[time(name = "fib", span = s)]`。

**练习 2**：为什么 `name` 用 `parse_string`（必须是字符串字面量），而 `span` 用 `parse_key_value::<_, syn::Expr>`（可以是任意表达式）？

**答案**：`name` 最终会成为 `TimingScope` 内部 `&'static str` 的计时标签，用字符串字面量最自然；而 `span` 是运行时传入的源码位置（一个变量名，如函数参数 `span: Span`），它本身就是一个表达式，所以允许任意 `syn::Expr`。

---

### 4.2 模块 B：`time::create` —— 在函数体开头注入 `TimingScope`

#### 4.2.1 概念说明

拿到 `Meta` 和 `item: syn::ItemFn` 后，`create` 的工作只有三步：决定计时名、决定构造方式、把一条语句塞进函数体最前面。它**不生成新的类型**，也不实现任何 trait——只是「往用户函数里加一行」。正因如此，`#[time]` 是七个宏里最纯粹、最易读的「代码注入」范例。

「注入到函数体最前面」而不是「包裹整个函数」，是一个有意识的选择：注入一行 `let __scope = ..;` 后，函数原本的语句原封不动地跟在后面，对调试器和读代码的人都几乎透明。

#### 4.2.2 核心流程

```
create(meta, item):
  1. name = meta.name.unwrap_or_else(|| 函数名.to_string())
  2. 构造表达式 construct：
        若 meta.span 是 Some(span)  → quote!{ with_span(#name, Some(#span.into_raw())) }
        若 meta.span 是 None        → quote!{ new(#name) }
  3. stmt = parse_quote!{ let __scope = ::typst_timing::TimingScope::#construct; }
  4. item.block.stmts.insert(0, stmt)   // 塞到函数体第一条
  5. item.into_token_stream()           // 把改过的整棵函数吐回去
```

这里有两层 `quote!` 嵌套：先用 `quote!` 拼出 `construct`（一个 `TokenStream` 片段），再把这个片段用 `#construct` 嵌进 `parse_quote!` 的整条语句。`#name` 是一个 `String`，`quote!` 会把它当作字符串字面量输出（注意它实现的是 `ToTokens`，输出带引号的字面量，而不是裸标识符）。

#### 4.2.3 源码精读

整段 `create` 只有 16 行：

[src/time.rs:29-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/time.rs#L29-L44) —— `create` 的全部逻辑：取名、选构造方式、`insert(0, ..)`、回吐整棵 AST。

逐段看几个关键点：

- 第 30 行 `meta.name.unwrap_or_else(|| item.sig.ident.to_string())`：用户没给 `name` 时，默认用函数名（`item.sig.ident`）的字符串形式。这就是为什么 `#[time] fn fibonacci(..)` 即使不带参数，计时记录里也能出现 `"fibonacci"`。

- 第 31–34 行的 `match` 决定 `construct`：

  [src/time.rs:31-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/time.rs#L31-L34) —— 这是本讲的核心分支：有 `span` 走 `with_span`，没有就走 `new`。

  - `with_span(#name, Some(#span.into_raw()))`：把用户的 span 表达式先 `.into_raw()` 转成原始数值。原因见 `typst-timing` 的注释——`typst-timing` 不能依赖 `typst-syntax`（否则会形成循环依赖），所以只接受一个原始数字 `Option<NonZeroU64>`。我们会在 4.3.3 给出该注释的链接。
  - `new(#name)`：不带 span 的轻量构造。

- 第 36–41 行执行注入：

  [src/time.rs:36-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/time.rs#L36-L41) —— `item.block.stmts.insert(0, parse_quote!{ .. })`。注意 `item` 被声明为 `mut`，这里在原地修改它的函数体。

  `parse_quote!` 是 `quote!` 的「解析版」：它把 `quote!` 风格的 token 模板**直接解析成某个 syn 类型**（这里由上下文推断为 `syn::Stmt`，因为 `Vec::<syn::Stmt>::insert` 期望一个 `syn::Stmt`）。所以这一行等价于「手写一条 `let __scope = ..;` 语句再塞进语句列表」。

  `__scope` 这个名字不是随便取的——它和 `typst-timing` 声明式宏里用的绑定名一致，且双下划线前缀能尽量避开与用户局部变量撞名。这条语句产生的 `__scope` 会活到函数体末尾，离开作用域时 drop，从而自动记录「结束」事件（RAII）。

- 第 43 行 `item.into_token_stream()`：把改过的整棵函数 AST 转回 TokenStream 返回。函数原有的所有语句都还在，只是最前面多了一行。

#### 4.2.4 代码实践

1. **实践目标**：用 `cargo expand` 实际观察 `#[time]` 注入后的代码；若不便构建整个 typst，则改为手工推演。
2. **操作步骤（可选，需本地验证）**：在 typst 仓库里挑一个标了 `#[time]` 的函数（例如 `crates/typst-library` 中的某些函数），在对应 crate 目录运行 `cargo expand`（需先 `cargo install cargo-expand`），在展开结果里搜索 `TimingScope`。
3. **需要观察的现象**：展开后的函数体第一条语句应形如 `let __scope = ::typst_timing::TimingScope::new("xxx");` 或 `..::with_span("xxx", Some(span.into_raw()));`，其后紧跟原始函数体。
4. **预期结果**：能直接看到「注入的那一行 + 原函数体」的组合。如果你无法运行 `cargo expand`（构建 typst 较重），这一步可标记为「待本地验证」，改为下面的手工推演。
5. **手工推演（不依赖运行）**：对

   ```ignore
   #[time]
   fn double(x: i64) -> i64 { 2 * x }
   ```

   写出 `create` 生成的函数体第一条语句。答案：`let __scope = ::typst_timing::TimingScope::new("double");`（`name` 缺省取函数名 `"double"`，`span` 为 `None` 走 `new` 分支）。

#### 4.2.5 小练习与答案

**练习 1**：`create` 为什么把 `item` 声明成 `mut item: syn::ItemFn`？如果不加 `mut` 会怎样？

**答案**：因为第 36 行要调用 `item.block.stmts.insert(..)` 原地修改 `item`。Rust 要求被修改的绑定必须声明为 `mut`；不加 `mut` 会在该行编译失败（`cannot mutate immutable variable`）。

**练习 2**：`parse_quote!{ .. #construct; }` 里的 `#construct` 是一个 `proc_macro2::TokenStream`。为什么 `quote!` / `parse_quote!` 能直接接受一个 `TokenStream` 作为插值变量？

**答案**：`proc_macro2::TokenStream` 实现了 `quote::ToTokens`，而 `quote!` 对所有 `T: ToTokens` 都支持 `#var` 插值——它会把这些 token 原样拼进输出。所以「先用 `quote!` 造片段，再用 `#片段` 嵌进更大的模板」是惯用写法。

**练习 3**：假如某函数已有局部变量也叫 `__scope`，会发生什么？

**答案**：注入的 `let __scope = ..;` 在函数体最前面，会与用户后续声明的同名变量产生「shadowing（遮蔽）」；通常仍能编译，但语义上用户的 `__scope` 会被遮蔽。这正是宏约定使用双下划线前缀来「尽量」避免撞名的原因。

---

### 4.3 模块 C：`TimingScope` 注入语句的运行时归宿 —— `wasm32` 下为何「被省略」

#### 4.3.1 概念说明

`lib.rs` 给 `#[time]` 写了一段重要注释：

> By default, all tracing is omitted using the `wasm32` target flag. This is done to avoid bloating the web app, which doesn't need tracing.

这句话容易让人误以为 `time.rs` 里有 `#[cfg(target_arch = "wasm32")]`。但读完 4.2 你会发现——**`time::create` 永远无条件地注入那条 `let __scope = ..;` 语句**，宏里没有任何 cfg 判断。那么「省略」到底发生在哪？

答案：省略发生在本宏生成的代码所**调用**的运行时 crate `typst-timing` 里。宏生成的 `TimingScope::new(..)` / `with_span(..)` 会返回一个 `Option<TimingScope>`：当计时未启用时返回 `None`，于是 `__scope` 绑定的是 `None`，drop 一个 `None` 没有任何开销。同时 `typst-timing` 用 `#[cfg(target_arch = "wasm32")]` 选用更轻量的时间戳实现，避免把 `SystemTime`、事件序列化等「重」代码编进 `.wasm`，从而控制 web 端体积。

这是一个很重要的设计取舍：**宏保持极简、永远注入；平台相关的开关下沉到运行时 crate。** 这样宏的体积小、逻辑直白，而「要不要真的计时」是运行期可开关的。

#### 4.3.2 核心流程

```
宏生成：let __scope = ::typst_timing::TimingScope::new("fib");   // 总是注入
                                │
                                ▼  运行时（typst-timing）
        with_span(name, span) → if is_enabled() { Some(真正记录) } else { None }
                                │
        ┌──────────────────────┴───────────────────────┐
     Some(守卫)                                      None
     进入时记录 Start                               什么都不做
     函数返回 drop 时记录 End                       drop 也无开销
```

所以即使每个 `#[time]` 函数都多了一行，在 web 端（未启用追踪时）的代价接近「一次 `is_enabled()` 判断 + 一个 `None`」。

#### 4.3.3 源码精读

先看 `lib.rs` 里那段决定性的注释与入口：

[src/lib.rs:330-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L330-L341) —— 注释明确：默认在 `wasm32` 下省略所有 tracing，以免拖大 web app 体积；并说明 `span` 会被用作 `EventKey`。

[src/lib.rs:362-368](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L362-L368) —— `#[time]` 的 `#[proc_macro_attribute]` 入口，沿用全员一致的 `unwrap_or_else(to_compile_error)` 错误回传模式（u1-l2）。

注意入口把 `stream` 和 `item` 都 `.into()` 转成 `proc_macro2` 版本后，再交给 `time::time`，这与 u1-l2 讲的 `BoundaryStream` 类型边界一致。

再看「省略」真正落地的运行时 crate（跨 crate 引用，同一 HEAD）：

[crates/typst-timing/src/lib.rs:159-177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-timing/src/lib.rs#L159-L177) —— `new` / `with_span` 都返回 `Option<Self>`，内部用 `is_enabled()` 把关：未启用就返回 `None`。这正是「注入了但等于没做事」的来源。

这段代码紧上面的注释（162–177 行附近）还解释了为什么 `with_span` 收的是原始数字 `Option<NonZeroU64>` 而不是 `Span`——因为 `typst-timing` 不能依赖 `typst-syntax`（否则 `typst-syntax` 就没法反过来依赖 `typst-timing`，形成循环）。这正好对应 4.2.3 里 `#span.into_raw()` 的由来。

[crates/typst-timing/src/lib.rs:194-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-timing/src/lib.rs#L194-L205) —— `Drop` 实现在守卫被销毁时记录「End」事件。若 `__scope` 是 `None`，这里根本不会执行。

最后看时间戳如何按平台条件编译：

[crates/typst-timing/src/lib.rs:228-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-timing/src/lib.rs#L228-L235) —— `Timestamp` 在非 wasm32 用 `std::time::SystemTime`，在 wasm32 用 `f64`（更轻）。仓库内还能看到更多 `#[cfg(target_arch = "wasm32")]` 分支选用不同的时间源，目的是把 web 端体积压到最小。

把这些拼起来就回答了 `lib.rs` 那句注释：**「omitted using the wasm32 target flag」指的是运行时层面（轻量时间戳 + `is_enabled()` 返回 None）的省略，而不是宏层面的 cfg。宏本身永远注入那一行。**

#### 4.3.4 代码实践

这是一个**源码阅读型**实践。

1. **实践目标**：用源码证据回答「为什么 `#[time]` 在 wasm32 下『被省略』」。
2. **操作步骤**：
   - 重读 [src/lib.rs:330-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L330-L341) 的注释。
   - 打开 `crates/typst-timing/src/lib.rs`，确认 `time.rs` 生成的 `TimingScope::new` / `with_span` 返回的是 `Option`，且受 `is_enabled()` 控制。
   - 用 `grep` 在 `crates/typst-timing/src/lib.rs` 中统计 `#[cfg(target_arch = "wasm32")]` 出现的次数与位置（例如 `Grep` 模式 `target_arch = "wasm32"`）。
3. **需要观察的现象**：宏侧（`src/time.rs`）**没有任何** `wasm32` 的 cfg；所有平台分支都集中在 `typst-timing` 运行时里。
4. **预期结果**：得出结论——「省略」是运行时行为，宏永远注入；web 端未启用追踪时 `__scope` 为 `None`，几乎零开销，且时间戳实现更轻，从而不拖大 `.wasm` 体积。
5. 若你想亲眼看到 `is_enabled()` 的实现，可继续在 `typst-timing` 内查找其定义（「待本地确认」其确切行号，但本讲引用的 159–177 行已足以支撑结论）。

#### 4.3.5 小练习与答案

**练习 1**：有人说「`#[time]` 在 wasm32 下被省略，所以 `time.rs` 里一定有 `#[cfg(target_arch = "wasm32")]`」。这句话对吗？为什么？

**答案**：不对。`time.rs` 里没有任何 cfg。宏永远注入 `let __scope = ..;`；省略发生在运行时 crate `typst-timing`：`new`/`with_span` 返回 `Option`，未启用时为 `None`，外加时间戳按 `wasm32` 条件编译选用更轻实现。

**练习 2**：为什么把「是否计时」做成运行时开关（`is_enabled()`），而不是编译期 cfg 直接抹掉宏调用？

**答案**：运行时开关让同一份代码既能在 CLI（开启追踪、生成火焰图）又能 in web（关闭追踪、保持体积）下复用，而无需为两个目标维护两套函数体。宏保持极简、永远注入，把决策下沉到运行时，是「简单宏 + 智能运行时」的典型取舍。

## 5. 综合实践

把本讲三个模块串起来：仿照 `time::create`，亲手写一个最小的属性宏 `#[log_entry]`，它在函数体开头注入一条 `println!`，并据此回答 wasm32 的省略问题。

### 5.1 实践目标

- 复现 `parse → 改 AST → 回吐` 的代码注入流水线。
- 体会 `item.block.stmts.insert(0, ..)` 的作用。
- 用自己的话解释 `#[time]` 在 wasm32 下被省略的原因。

### 5.2 操作步骤

在一个独立的练习用 proc-macro crate 里（例如 `cargo new --lib log_entry_macro`，并在其 `Cargo.toml` 设 `[lib] proc-macro = true`），编写如下宏。这是**示例代码**，结构与 `time::create` 一一对应：

```ignore
// 示例代码：一个最小 attribute 宏，仿照 typst-macros 的 time::create
use proc_macro::TokenStream;
use syn::{parse_macro_input, parse_quote};

#[proc_macro_attribute]
pub fn log_entry(_stream: TokenStream, item: TokenStream) -> TokenStream {
    // 1. 解析被装饰的函数（对应 time.rs 里的 parse_macro_input!）
    let mut item = parse_macro_input!(item as syn::ItemFn);

    // 2. 取函数名作为日志标签（对应 create 里 meta.name 的缺省取值）
    let name = item.sig.ident.to_string();

    // 3. 在函数体最前面注入一条语句（对应 block.stmts.insert(0, parse_quote!{ .. })）
    item.block.stmts.insert(
        0,
        parse_quote! {
            println!("[log_entry] entered `{}`", #name);
        },
    );

    // 4. 把改过的整棵函数吐回去
    item.into_token_stream().into()
}
```

在另一个普通 crate 里引用它：

```ignore
// 示例代码：使用方
use log_entry_macro::log_entry;

#[log_entry]
fn greet(name: &str) {
    println!("hello, {name}");
}

fn main() { greet("typst"); }
```

### 5.3 需要观察的现象

- 运行后应先打印 `[log_entry] entered \`greet\``，再打印 `hello, typst`，证明注入的语句确实在函数体最前面先执行。
- 用 `cargo expand` 展开后，应看到 `greet` 的函数体首行是那条 `println!`。

### 5.4 预期结果

若 `log_entry` 行为正确，说明你已经掌握了 `#[time]` 的核心机制：**解析（可省）→ 取名 → `insert(0, ..)` 注入 → 回吐 AST**。`#[time]` 与它的唯一实质区别，是注入的语句从 `println!` 换成了 `let __scope = ::typst_timing::TimingScope::..;`，并多了一个可选的 `name` / `span` 参数解析。

> 实际能否在本机构建取决于你的工具链与练习 crate 配置；若暂未搭建，可将运行结果标记为「待本地验证」，并改为手工推演 `greet` 展开后的函数体。

### 5.5 回答 wasm32 省略问题

在练习的 README 或注释里，用一句话回答：**`#[time]` 默认在 wasm32 下被省略，是为了避免拖大不需要追踪的 web 端体积**（依据 [src/lib.rs:340-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L340-L341) 的注释）。而省略并不发生在 `time.rs`：宏永远注入那一行；真正「等于没计时」的是运行时 `typst-timing`——`TimingScope::new/with_span` 返回 `Option`，未启用时为 `None`，且时间戳实现按 `#[cfg(target_arch = "wasm32")]` 选用更轻量版本。

## 6. 本讲小结

- `#[time]` 是属性宏，收 `stream`（括号参数）与 `item`（被装饰函数）；入口在 [src/lib.rs:362-368](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L362-L368)，沿用全员的 `to_compile_error` 错误回传。
- `Meta` 只有两个 `Option` 字段 `name` / `span`，分别用 `parse_string::<kw::name>`（必须字符串字面量）和 `parse_key_value::<kw::span, syn::Expr>`（任意表达式）解析；两者都缺省合法，但都给时 `name` 必须在前。
- `time::create` 的全部精髓是 `item.block.stmts.insert(0, parse_quote!{ let __scope = ..; })`——原地改 AST，把一条 RAII 守卫塞进函数体最前面，函数返回时自动记录结束事件。
- 有 `span` 走 `TimingScope::with_span(#name, Some(#span.into_raw()))`，无 `span` 走 `TimingScope::new(#name)`；`.into_raw()` 是为了避免 `typst-timing` 反向依赖 `typst-syntax`。
- 「wasm32 下省略」是运行时行为：宏永远注入，`typst-timing` 的 `new`/`with_span` 返回 `Option` 且用 `is_enabled()` 把关，未启用即为 `None`；时间戳还按 cfg 选用更轻实现，以控制 web 体积。
- 这套「解析中间结构 `Meta` → `create` 生成」就是 typst-macros 七个宏共享流水线的最简版本，是后续 `#[func]`、`#[elem]` 的骨架。

## 7. 下一步学习建议

- 下一讲 **u2-l2 `#[ty]` 类型宏**：从「只注入一行」升级到「生成 `NativeType` / `NativeTypeData` 的完整 impl」，你会看到 `BareType` 和 `LazyLock` 延迟初始化首次登场。
- 若想加深对 `util.rs` helper 的理解，建议回头重读 u1-l3 的「键 × 值类型正交矩阵」，本讲用到的 `parse_string` / `parse_key_value` 只是其中两格。
- 对「运行时省略」感兴趣的话，可以浏览 `crates/typst-timing/src/lib.rs` 的 `TimingScope`、`Drop`、`Timestamp` 三段，对照本讲 4.3.3 的链接阅读，体会「简单宏 + 智能运行时」的分工。
