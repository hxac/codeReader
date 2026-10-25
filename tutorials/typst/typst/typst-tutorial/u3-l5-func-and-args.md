# 函数是一等公民：Func、Args 与类型转换 cast

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Func` 的五种内部形态（原生、元素、闭包、插件、with 绑定），以及 `Func::call_impl` 如何按形态派发调用。
2. 理解 `Args` 的「消费式」参数协议：`expect` / `eat` / `find` / `all` / `named` 如何从参数列表中取走并转换值，`finish` 如何兜底报告「多余参数」。
3. 掌握 cast 体系（`Reflect` / `IntoValue` / `FromValue` + `cast!` 宏）如何连接脚本世界的 `Value` 与 Rust 类型，以及 `#[func]` 宏如何为原生函数自动生成「解析参数 → 调用 → 统一结果」的包装代码。
4. 能在 Rust 测试中手工构造 `Args` 并直接调用一个内置函数（如 `upper`），观察参数消费成功与「多余参数」报错的完整过程。

本讲承接 u3-l3（`Value` 类型系统）与 u3-l4（`Content` 与元素机制）：`Value::Func` 里的那个 `Func` 到底长什么样、怎么被调用，正是本讲要拆开的内容。

## 2. 前置知识

- **一等公民（first-class citizen）**：在 Typst 里，函数和整数、字符串一样，是可以存进变量、当参数传递、当返回值的普通值。Rust 侧对应的就是 `Value::Func(Func)`。
- **位置参数与命名参数**：`#enum(start: 2)[A][B]` 中 `start: 2` 是命名参数（按名字识别），`[A]`、`[B]` 是位置参数（按出现顺序识别）。位置参数还可以写在括号外（trailing content block）。
- **参数槽（argument slot）**：Typst 不像 Rust 那样在编译期检查实参是否匹配形参。所有实参先被打包进一个 `Args` 列表，再由被调函数自己「按需取用」，最后检查有没有剩下的。这套协议是本讲核心之一。
- **trait 与派发**：Rust 的 trait 类似其他语言的接口。`Func` 内部是一个枚举，调用时按枚举变体做 `match` 派发——这是「运行时多态」的手写版本。
- **过程宏（proc-macro）**：`#[func]`、`#[elem]` 这类注解会在编译期生成额外的 Rust 代码（注册信息、参数解析样板）。本讲会看它到底生成了什么。前置阅读可回顾 u1-l3 提到的「类型在 typst-library、行为拆分到各阶段 crate」的架构决策，本讲的 `Routines` 间接调用是它的又一次现身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/foundations/func.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs) | `Func` 类型：五种内部形态、调用派发、`with`/`where`、`NativeFuncData` 元数据、`Closure` 数据 |
| [crates/typst-library/src/foundations/args.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs) | `Args`/`Arg`：参数的捕获、消费（`expect`/`named`/…）、`finish` 兜底、脚本侧 `arguments` 类型的方法 |
| [crates/typst-library/src/foundations/cast.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs) | `Reflect`/`IntoValue`/`FromValue` 三 trait、`CastInfo` 描述与错误生成、`Container`/`Never` 辅助类型 |
| [crates/typst-eval/src/call.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs) | 求值期的函数调用：`ast::FuncCall` 求值、`ast::Args` 求值（含 spread 展开）、`eval_closure` 执行用户闭包 |
| [crates/typst-macros/src/func.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs) | `#[func]` 过程宏的实现：生成 `NativeFuncData` 与「参数解析包装闭包」 |
| [crates/typst-library/src/text/case.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/case.rs) | `upper`/`lower` 内置函数与 `Caseable` 联合类型的 `cast!` 定义，本讲的解剖样本 |

## 4. 核心概念与源码讲解

### 4.1 Func：函数的统一表示与调用派发

#### 4.1.1 概念说明

脚本里的「函数值」在 Rust 侧只有一个类型 `Func`，但它背后可能是五种完全不同的东西：

1. **原生函数**（`Native`）：Rust 写的内置函数，如 `upper`、`min`，由 `#[func]` 宏注册。
2. **元素函数**（`Element`）：`heading`、`table` 这类「调用即造元素」的函数（承接 u3-l4 的元素机制），还能用于 set/show 规则和选择器。
3. **用户闭包**（`Closure`）：`#let f(x) = ...` 或 `(x) => ...` 定义的函数，携带语法树节点、参数默认值与捕获的外层变量。
4. **插件函数**（`Plugin`）：WASM 插件导出的函数（深入探讨在 u5-l9）。
5. **with 绑定**（`With`）：`f.with(args)` 产生的「预应用了一部分参数」的新函数。

`Func` 还携带一个 `span`，用于在调用出错时把错误定位到「这个函数值是在哪里创建的」。

#### 4.1.2 核心流程

一次 `#f(a, b: 1)` 调用在求值期的路径：

```text
ast::FuncCall::eval (call.rs)
  ├─ 先求值 callee，再求值 args（顺序有讲究）
  ├─ ast::Args::eval：把每个实参表达式求值成 Arg { name?, Spanned<Value> }
  │    └─ spread(..x) 按 x 的类型展开：array→多个位置参数 / dict→多个命名参数
  └─ call_func → Func::call::<T>
       └─ Func::call_traced → Func::call_impl
            ├─ Native:  (函数指针)(engine, context, &mut args) → args.finish()
            ├─ Element: func.construct(engine, &mut args) → args.finish()
            ├─ Closure: routines.eval_closure(...)（经 Routines 表间接调用）
            ├─ Plugin:  args.all::<Bytes>() → WASM 调用
            └─ With:    预应用参数排在前面，再递归 call_impl
```

关键点：**`Func` 是数据，调用是解释**。脚本层永远只搬运 `Func` 值，真正执行时才按内部形态派发；用户闭包甚至不经过 typst-library 直接执行，而是通过 `Routines` 函数指针表跳到 typst-eval 的 `eval_closure`——这就是 u1-l3 说的「签名住在 library、实现在总装 crate 接线」的模式。

#### 4.1.3 源码精读

`Func` 本体只是「内部表示 + 报错用 span」的组合：

- [crates/typst-library/src/foundations/func.rs:141-148](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L141-L148) —— `Func` 结构体：`inner: FuncInner` 加一个调用出错时使用的 `span`；`#[ty(scope, cast, name = "function")]` 把它注册为脚本里的 `function` 类型并挂上关联作用域。
- [crates/typst-library/src/foundations/func.rs:150-163](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L150-L163) —— `FuncInner` 五变体枚举：`Native(Static<NativeFuncData>)`、`Element(Element)`、`Closure(Arc<LazyHash<Closure>>)`、`Plugin(Arc<PluginFunc>)`、`With(Arc<With>)`。整个枚举 `derive(PartialEq, Hash)`，所以函数值可以比较、可以参与 comemo 缓存键的计算。

`With` 的结构值得单独看：

- [crates/typst-library/src/foundations/func.rs:178-191](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L178-L191) —— `With { func, args, params_span }`：内层函数加预应用参数。注释特意说明把 `params_span` 缓存在这一层，避免每次调用都递归下钻取 span。

调用入口三层递进：

- [crates/typst-library/src/foundations/func.rs:364-375](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L364-L375) —— `Func::call<T: FromValue>`：泛型入口，先拿到 `Tracepoint::Call`（错误追踪点），再对返回值做 `.cast()` 把 `Value` 转成调用方想要的 Rust 类型。
- [crates/typst-library/src/foundations/func.rs:379-389](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L379-L389) —— `call_traced`：把 `impl IntoArgs` 统一转成 `Args`，执行后用 `.trace(...)` 给错误加上「发生在调用 xxx 时」的追踪信息。
- [crates/typst-library/src/foundations/func.rs:391-433](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L391-L433) —— `call_impl`：真正的派发 `match`。逐分支看：
  - 原生分支（L400-404）：`(native.function.0)(engine, context, &mut args)?` 直接调函数指针，然后 `args.finish()?` 兜底检查多余参数；
  - 元素分支（L405-409）：走 `func.construct`（u3-l4 讲过的元素构造），产出 `Value::Content`；
  - 闭包分支（L410-421）：`(engine.library.routines.eval_closure)(...)`——注意这不是直接调用，而是查 `Routines` 函数指针表（在总装 crate 的 `static ROUTINES` 里接线），把 world、library、introspector、sink、route、context 一股脑传给 typst-eval；
  - 插件分支（L422-427）：把所有剩余位置参数 `args.all::<Bytes>()` 收集后交给 WASM；
  - with 分支（L428-431）：**预应用参数排在前面**（`with.args.items.iter().cloned().chain(args.items)`），再递归调用内层函数。

求值侧的入口在 typst-eval：

- [crates/typst-eval/src/call.rs:24-82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L24-L82) —— `impl Eval for ast::FuncCall`：先 `route.check_call_depth()` 防无限递归；callee 若是字段访问（`a.b(...)`）走方法解析（L65-67 处把接收者插为第一个参数——这正是 u3-l3 说的「类型作用域查表 → 接收者插队」）；普通 callee 则先求值再 `cast::<Func>()`，注释明确「先求值 callee，再求值参数」。
- [crates/typst-eval/src/call.rs:166-177](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L166-L177) —— `call_func`：给函数补上调用点 span 后调 `Func::call`；非 WASM 平台用 `stacker::maybe_grow` 动态扩栈，让深递归不至于爆栈。

用户闭包如何被执行（概览，细节留给 u3-l6）：

- [crates/typst-eval/src/call.rs:639-653](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L639-L653) —— `eval_closure` 带 `#[comemo::memoize]`：同样的 `(func, closure, args)` 组合不会重复执行，这就是「Typst 函数纯函数性」的缓存落点之一。
- [crates/typst-eval/src/call.rs:692-737](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L692-L737) —— 参数绑定循环：位置参数用 `args.expect::<Value>` 取、命名参数用 `args.named` 取（取不到用默认值）、sink 参数用 `args.consume` 收走多余位置参数，最后 `args.finish()?`（L737）确保没有未消费参数——和原生函数的消费协议一模一样。

最后看「函数也是模块」的实现——关联作用域：

- [crates/typst-library/src/foundations/func.rs:322-338](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L322-L338) —— `Func::field`：从函数自己的 scope 里取子定义（`assert.eq`、`list.item` 就是这么来的），闭包没有 scope 会得到 "cannot access fields on user-defined functions"。
- [crates/typst-library/src/foundations/func.rs:458-478](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L458-L478) —— `Func::with`（脚本侧 `f.with(..)`）：用 `args.take()` 把调用时的实参整个打包成 `FuncInner::With`。
- [crates/typst-library/src/foundations/func.rs:489-519](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L489-L519) —— `Func::where_`（脚本侧 `heading.where(level: 2)`）：只对元素函数开放，把命名参数转成字段过滤条件，产出 `Selector`（承接 u3-l4）。

#### 4.1.4 代码实践：验证 `with` 的参数顺序语义

**实践目标**：亲眼看到「with 预应用的参数排在调用参数之前」，从而理解 `call_impl` 的 with 分支。

**操作步骤**（使用已构建好的 CLI，见 u1-l2）：

1. 新建 `with-demo.typ`（示例代码，非仓库文件）：

   ```typst
   #let shout = upper.with("typst")
   // 预应用的 "typst" 占掉了唯一的 text 参数
   #shout()      // 预期输出 TYPST
   #shout("x")   // 预期报错：unexpected argument
   ```

2. 只保留第一行调用，运行：

   ```bash
   cargo run -p typst-cli -- compile with-demo.typ out.pdf
   ```

3. 取消第二行 `#shout("x")` 的注释再编译一次，观察报错。

**需要观察的现象**：第一次编译成功，PDF 中出现 `TYPST`；第二次编译失败，错误信息形如 `unexpected argument`（来自 `Args::finish`），因为 `"typst"` 已被 `expect("text")` 消费，多出来的 `"x"` 留在参数列表里被 `finish` 兜底报出。

**预期结果**：错误消息与位置可在本地确认（待本地验证具体文案）；根据 [args.rs:261-271](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L261-L271) 的逻辑，多余位置参数报 `unexpected argument`（不带名字）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Func` 要 `derive(Hash)`？

**答案**：`Func` 会作为 `Value` 的一部分参与 comemo 的记忆化缓存（例如 `eval_closure` 的 `#[comemo::memoize]` 以参数哈希为缓存键），也用于 `Content`/`Args` 等可哈希结构；哈希还要求 `PartialEq` 语义一致（[func.rs:541-545](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L541-L545) 把相等性委托给 `FuncInner`）。

**练习 2**：`#assert.eq(1, 1)` 中的 `.eq` 是怎么找到的？

**答案**：`assert` 求值成 `Value::Func`，字段访问走 [func.rs:322-338](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L322-L338) 的 `Func::field`，在原生函数自带的 `scope`（`NativeFuncData.scope`，由 `#[scope]` 块填充）里查到 `eq` 绑定。

**练习 3**：闭包分支为什么不直接 `typst_eval::eval_closure(...)` 而要走 `routines` 表？

**答案**：typst-library 是底层 crate，不能依赖 typst-eval（否则循环依赖）；于是把函数指针存进 `Routines`，由总装 crate 在 [crates/typst/src/lib.rs:329-342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L329-L342) 的 `static ROUTINES` 里接线，等效于编译期完成的动态链接。

### 4.2 Args：位置/命名参数的消费协议

#### 4.2.1 概念说明

`Args` 是「一次调用捕获的全部实参」：一个 `Arg` 列表，每个 `Arg` 带可选名字（`None` 即位置参数）、值和 span。它同时是脚本里的 `arguments` 类型（argument sink 收到的就是它）。

Typst 没有「函数签名编译期检查」，取而代之的是一套**消费式协议**：

- 被调函数按需从列表里**取走**（remove）参数：按位置取、按名字取、按类型挑、或全收；
- 取参数时同时完成 `Value` → Rust 类型的转换（cast）；
- 全部取完后由 `finish()` 检查：列表若还剩东西，就是「多余参数」错误。

「取走即删除」保证了同一个参数不会被消费两次，也让 `finish` 能精确指出是哪个参数多余。

#### 4.2.2 核心流程

消费原语一览（T 均需实现 `FromValue<Spanned<Value>>`）：

| 方法 | 语义 | 常见用途 |
| --- | --- | --- |
| `expect::<T>(what)` | 取走第一个位置参数并 cast；没有则报 `missing argument: {what}` | 必选位置参数 |
| `eat::<T>()` | 同上但缺失返回 `None` 不报错 | 带默认值的位置参数 |
| `find::<T>()` | 跳过不能 cast 的位置参数，取走第一个可 cast 的 | 类型可区分的可选参数 |
| `all::<T>()` | 取走**所有**可 cast 的位置参数 | variadic 参数 |
| `named::<T>(name)` | 取走所有同名命名参数，用最后一个 | 命名参数 |
| `named_or_find::<T>(name)` | 先按名取，取不到退化为 `find` | 可位置可命名的参数 |
| `consume(n)` | 原样取走 n 个位置参数（不 cast） | 闭包 sink 参数 |
| `finish()` | 若还有剩余参数则报 `unexpected argument` | 兜底 |

求值期把语法树里的实参表达式变成 `Args` 的过程在 `ast::Args::eval`，其中 spread `..x` 按 `x` 的运行时类型展开。

#### 4.2.3 源码精读

数据结构：

- [crates/typst-library/src/foundations/args.rs:56-64](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L56-L64) —— `Args { span, items: EcoVec<Arg> }`：`span` 是**整个调用点**的 span（不是参数列表的）。
- [crates/typst-library/src/foundations/args.rs:542-551](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L542-L551) —— `Arg { span, name: Option<Str>, value: Spanned<Value> }`：位置参数 `name` 为 `None`；每个值自带 span，报错能精确到那个实参表达式。

核心消费方法：

- [crates/typst-library/src/foundations/args.rs:114-127](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L114-L127) —— `eat`：线性找到第一个 `name.is_none()` 的槽，`remove` 掉并 `T::from_value(value).at(span)`——cast 失败时用**值自己的 span** 报错。
- [crates/typst-library/src/foundations/args.rs:149-161](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L149-L161) —— `expect`：`eat` 的缺省报错版。
- [crates/typst-library/src/foundations/args.rs:163-178](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L163-L178) —— `missing_argument`：一个贴心的特判——如果用户写了 `text: "hi"`（把本该位置的参数写成了命名），报 ``the argument `text` is positional`` 并提示删掉 `text:`。
- [crates/typst-library/src/foundations/args.rs:180-193](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L180-L193) —— `find`：用 `T::castable(&slot.value.v)` 先试类型再取，这就是「`box(x, y)` 与 `box(width: 2pt)` 靠类型区分参数」的实现基础。
- [crates/typst-library/src/foundations/args.rs:195-218](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L195-L218) —— `all`：`retain` 一遍，能 cast 的收集、不能的累计错误后统一返回（`eco_vec!` 攒错误再一次性报）。
- [crates/typst-library/src/foundations/args.rs:220-240](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L220-L240) —— `named`：注释点明「不提前退出」——把所有同名参数都删掉、用最后一个，重复命名不报错而是后者覆盖前者。
- [crates/typst-library/src/foundations/args.rs:261-271](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L261-L271) —— `finish`：只看剩余列表的第一项，命名参数报 `unexpected argument: {name}`，位置参数报 `unexpected argument`。

脚本侧 `arguments` 类型的能力（`#[scope]` 块注册）：

- [crates/typst-library/src/foundations/args.rs:324-347](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L324-L347) —— `arguments(..args)` 构造函数就是 `args.take()`：把自己收到的参数原样打包成新的 `Args` 值。
- [crates/typst-library/src/foundations/args.rs:404-424](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L404-L424) —— `pos()` / `named()`：把参数列表视图化为数组/字典。
- [crates/typst-library/src/foundations/args.rs:377-397](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L377-L397) —— `at(key, default:)`：整数按下标、字符串按名取；配套的 `get`（L294-312）支持负下标（倒数第 n 个）。
- [crates/typst-library/src/foundations/args.rs:500-513](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L500-L513) —— `Add` 实现：`a + b` 会先删掉 a 中被 b 同名覆盖的命名参数再拼接——参数合并的语义也定义在这里。

求值侧：实参如何变成 `Args`：

- [crates/typst-eval/src/call.rs:408-458](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L408-L458) —— `impl Eval for ast::Args`：Pos/Named 直接求值入列；Spread 分支（L432-450）按运行时类型展开——`none` 忽略、`array` 展开为多个位置参数、`dict` 展开为多个命名参数、`arguments` 值原样并入，其他类型报 `cannot spread {ty}`。末尾注释强调 span 故意用 detached，让上层 `FuncCall::eval` 用整个调用点的 span 覆盖（`spanned(span)`）。
- [crates/typst-library/src/foundations/args.rs:581-602](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L581-L602) —— `IntoArgs` trait：让 `Func::call` 既接受现成的 `Args`，也接受任意 `IntoValue` 迭代器（自动视为位置参数）——Rust 侧调用脚本函数因此非常省事。

#### 4.2.4 代码实践：观察 spread 展开与 sink 收集

**实践目标**：验证 `ast::Args::eval` 的 spread 规则与 `arguments` 类型的内容。

**操作步骤**：

1. 新建 `args-demo.typ`（示例代码）：

   ```typst
   #let collect(..sink) = sink
   // 混合：位置、命名、数组展开、字典展开
   #let a = (2, 3)
   #let d = (fill: blue)
   #repr(collect(1, x: 2, ..a, ..d))
   ```

2. 编译并查看输出：

   ```bash
   cargo run -p typst-cli -- compile args-demo.typ out.pdf
   ```

3. 对照 [call.rs:432-450](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L432-L450) 逐项核对。

**需要观察的现象**：PDF 中出现 `arguments(1, 2, 3, x: 2, fill: blue)` 形态的 repr（顺序以本地输出为准，待本地验证）。数组 `(2, 3)` 被拆成两个位置参数，字典 `(fill: blue)` 被拆成命名参数。

**预期结果**：sink 收到的 `arguments` 值包含全部 5 个参数；`pos()` 应得到 `(1, 2, 3)`，`named()` 应得到 `(x: 2, fill: blue)`。可在文档里追加 `#repr(collect(1, x: 2, ..a, ..d).pos())` 验证。

#### 4.2.5 小练习与答案

**练习 1**：`#f(a: 1, a: 2)` 里 `args.named::<i64>("a")` 返回什么？列表里还剩几个 `a`？

**答案**：返回 `Some(2)`（用最后一个），且两个 `a` 都被删除——见 [args.rs:226-239](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L226-L239) 的循环不提前退出。

**练习 2**：为什么 `finish` 只检查 `items.first()` 而不列出所有剩余参数？

**答案**：消费协议保证正常路径下参数会被逐个取走；走到 `finish` 时说明签名不匹配，报第一个剩余参数已足以定位问题，实现也最简单（[args.rs:263-269](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L263-L269)）。

**练习 3**：`Args::new` 与 `Args::push` 构造的参数 span 用的是哪个？

**答案**：`Arg.value` 的 span 用传入的具体 span（每个值可独立定位），而 `Arg.span`（参数槽本身的 span）用 `self.span` 即整个调用点 span——见 [args.rs:67-78](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L67-L78) 与 [args.rs:106-112](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L106-L112)。

### 4.3 cast：Value 与 Rust 类型的双向转换

#### 4.3.1 概念说明

脚本世界只有 `Value`（u3-l3 的 30 个变体），Rust 世界有具体类型（`Str`、`Caseable`、`Vec<Value>`…）。cast 体系就是两者之间的海关，由三个 trait 分工：

- **`Reflect`**：描述「T 能接受什么样的 Value」——不实际转换，只用于文档、自动补全和快速判断（`castable`）；
- **`IntoValue`**：`T → Value`，永不失败（Rust 侧构造的值一定合法）；
- **`FromValue`**：`Value → T`，可能失败（用户传什么都有可能），失败时生成「expected X, found Y」错误。

为什么不用标准库的 `TryFrom<Value>`？源码注释直接回答：会与现有 impl 冲突（conflicting impls），而且 `From<T> for Value` 的方向会让代码里到处是难读的 `.into()`。

此外，`cast!` 宏让「联合类型」（一个参数接受多种类型，如 `upper` 接受字符串**或**内容）的声明非常简洁。

#### 4.3.2 核心流程

一个原生函数参数从脚本到 Rust 的完整旅程：

```text
脚本实参表达式
  → 求值为 Value                        (typst-eval)
  → 装入 Arg { name?, Spanned<Value> }  (ast::Args::eval)
  → args.expect::<Caseable>("text")     (消费 + cast)
       → <Caseable as FromValue>::from_value(v)
            匹配 cast! 声明: Str → Caseable::Str / Content → Caseable::Content
            都不匹配 → <Caseable as Reflect>::error(&v)
                 → CastInfo::error: "expected string or content, found ..."
       失败时 .at(值的span) 附加定位
  → Rust 函数拿到具体类型执行
  → 返回值经 IntoValue/IntoResult 装回 Value
```

而这一切的接线由 `#[func]` 宏完成：它为每个参数生成一行消费代码，规则是——variadic 用 `all`、named 用 `named`、带默认值用 `eat`、其余用 `expect`。

#### 4.3.3 源码精读

三个 trait 的契约：

- [crates/typst-library/src/foundations/cast.rs:20-32](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L20-L32) —— 模块级注释写明三 trait 分工与「为何不用 TryFrom」。
- [crates/typst-library/src/foundations/cast.rs:33-58](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L33-L58) —— `Reflect`：`input()`/`output()` 返回 `CastInfo`，`castable()` 做廉价判断（注释解释：走 `CastInfo` 动态判断要堆分配，太贵），`error()` 生成类型不符的消息。
- [crates/typst-library/src/foundations/cast.rs:172-178](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L172-L178) —— `IntoValue`：单方法 trait。
- [crates/typst-library/src/foundations/cast.rs:249-293](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L249-L293) —— `FromValue` 及其 `Spanned` 相关 impl：注意 L280-284——`T: FromValue<Spanned<Value>>` 直接丢弃 span；L286-293——请求 `Spanned<T>` 则把值自带的 span 保留下来，供需要定位的参数使用。

类型描述与错误生成：

- [crates/typst-library/src/foundations/cast.rs:295-306](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L295-L306) —— `CastInfo` 四变体：`Any`（任何值都行）、`Value`（特定字面值，如 `"auto"`）、`Type`（某类型的任意值）、`Union`（多选一）。
- [crates/typst-library/src/foundations/cast.rs:308-367](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L308-L367) —— `CastInfo::error`：`walk` 展平 Union 后拼出 `expected A, B or C, found {实际类型}`；随后是一组实用提示——整数配 `length` 时提示「a length needs a unit - did you mean {i}pt?」（L341-344）、字符串配 `label` 时提示 `<name>` 写法（L345-355）。你在 Typst 里见过的这些报错提示，源头就在这里。
- [crates/typst-library/src/foundations/cast.rs:421-437](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L421-L437) —— `Container` trait：让 `Option<T>`/`Vec<T>`/`SmallVec` 暴露内部类型 `Inner`。`#[func]` 宏对 variadic 参数或无默认值的 named 参数，取 `Container::Inner` 的 `CastInfo` 作为元素类型描述（如 `Vec<Value>` 的参数在文档里显示为接受任意值序列）。
- [crates/typst-library/src/foundations/cast.rs:439-467](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L439-L467) —— `Never`：不可 inhabited 的类型，`castable` 恒为 false，用来表达「此参数不接受任何值」的空 Union。

`cast!` 宏的实战样本——`upper` 的参数类型：

- [crates/typst-library/src/text/case.rs:28-34](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/case.rs#L28-L34) —— `pub fn upper(text: Caseable) -> Caseable`：签名里没有 Value，转换全部由 cast 体系完成。
- [crates/typst-library/src/text/case.rs:44-58](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/case.rs#L44-L58) —— `Caseable` 枚举 + `cast!` 声明：`self =>` 分支描述 IntoValue 方向（两个变体各自转回 Value）；`v: Str =>` / `v: Content =>` 分支描述 FromValue 方向。传整数进来时，`Reflect::error` 会根据这个 Union 生成「expected string or content, found integer」（具体措辞待本地验证）。
- [crates/typst-library/src/text/mod.rs:69-71](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/mod.rs#L69-L71) —— `global.define_func::<upper>()`：把宏生成的（doc-hidden 的）`upper` 零大小枚举注册进全局作用域——承接 u3-l2 的 Library 装配流程。

`#[func]` 宏到底生成了什么（在 typst-macros 里看模板）：

- [crates/typst-macros/src/func.rs:370-419](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs#L370-L419) —— `create_wrapper_closure`：生成的闭包形如 `|engine, context, args| { 解析各参数; args.take().finish()?; 调用原函数; IntoResult::into_result(output, args.span) }`——这就是 `NativeFuncPtr` 指向的东西。
- [crates/typst-macros/src/func.rs:458-477](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs#L458-L477) —— `create_param_parser`：参数属性到消费方法的映射表——`variadic → args.all()`、`named → args.named()`、有默认值 → `args.eat()` 加 `unwrap_or_else(默认值)`、否则 `args.expect(name)`。
- [crates/typst-macros/src/func.rs:421-456](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs#L421-L456) —— `create_param_info`：同时生成 `NativeParamInfo`（名字、文档、`input: <T as Reflect>::input()`、默认值闭包、positional/named/variadic/required 标志），供文档站和 IDE 补全使用。
- [crates/typst-macros/src/func.rs:251-290](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs#L251-L290) —— `create`：对自由函数额外生成一个与函数同名的**零变体枚举**（Rust 的类型与函数在不同命名空间，可以同名共存）并 `impl NativeFunc`——这就是 `define_func::<upper>()` 里那个 `upper` 类型的来历（[func.rs:689-699](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L689-L699) 定义了 `NativeFunc` trait）。

返回值的统一出口：

- [crates/typst-library/src/foundations/cast.rs:210-241](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L210-L241) —— `IntoResult`：原生函数可以直接返回 `T`、`StrResult<T>`、`HintedStrResult<T>` 或 `SourceResult<T>`，包装闭包统一走 `into_result(output, args.span)`——`StrResult` 的错误在这里被 `.at(span)` 附上调用点定位。所以 Rust 侧写内置函数时 `bail!("...")` 的字符串错误会自动出现在正确的源码位置。

#### 4.3.4 代码实践：触发并解读 cast 错误

**实践目标**：把 4.3.2 的流程图在真实报错里走一遍。

**操作步骤**：

1. 新建 `cast-demo.typ`（示例代码）：

   ```typst
   #upper(42)
   ```

   编译：

   ```bash
   cargo run -p typst-cli -- compile cast-demo.typ out.pdf
   ```

2. 改成缺少参数再编译：

   ```typst
   #upper()
   ```

3. 再改成「把位置参数写成命名参数」：

   ```typst
   #upper(text: "hi")
   ```

**需要观察的现象**：

- 第 1 种：类型错误，消息形如 `expected string or content, found integer`——由 `Caseable` 的 `cast!` Union 经 `CastInfo::error` 生成；
- 第 2 种：`missing argument: text`——由 `Args::expect` → `missing_argument` 生成；
- 第 3 种：``the argument `text` is positional`` 加提示 `try removing text:`——由 [args.rs:163-178](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L163-L178) 的特判生成。

**预期结果**：三种报错分别命中 cast 层、Args 消费层、Args 错误提示层——正好覆盖本讲三个最小模块的报错路径。具体文案与错误中显示的调用追踪（`error occurred in this call of function upper` 之类）以本地输出为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`Reflect` 与 `FromValue` 都能判断「值能不能转」，为什么要两个？

**答案**：`castable` 只是 `Reflect` 上的廉价布尔判断（供 `Args::find`/`all` 快速筛选）；真正转换与生成详细错误（`from_value` 失败时走 `Reflect::error`）是 `FromValue` 的职责。分离让「筛选」可以零成本内联（[cast.rs:40-45](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L40-L45) 注释）。

**练习 2**：给 `#[func]` 函数的参数标 `#[variadic]` 后，宏生成的消费代码与 `CastInfo` 有什么变化？

**答案**：消费代码变成 `args.all()?`（一次收走所有可 cast 的位置参数，[macros/func.rs:462-464](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs#L462-L464)）；`NativeParamInfo.input` 取的是 `Container::Inner`（元素类型）的 `CastInfo`（[macros/func.rs:429-433](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs#L429-L433)）。

**练习 3**：为什么 `IntoValue` 不返回 `Result`？

**答案**：Rust 侧类型系统保证构造出的值一定是合法的 `Value` 变体；只有「用户输入的任意 Value 转 Rust 类型」方向才可能失败，所以失败可能性只存在于 `FromValue`（[cast.rs:24-28](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs#L24-L28) 的方向注释）。

## 5. 综合实践：在 Rust 测试里直接调用 `upper`

这是本讲的主实践：不经过解析与求值，**手工构造 `Args` 并以 Rust 调用内置函数 `upper`**，完整观察「期望参数消费成功 → 多余参数报错」的全过程。构造 `Engine` 的方式照搬仓库内先例：[convergence.rs:217-235](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/introspection/convergence.rs#L217-L235)（ad-hoc Engine）与 [typst-ide/src/tests.rs:76-113](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-ide/src/tests.rs#L76-L113)（最小 TestWorld）。

**实践目标**：

1. 成功路径：`Args = ["hello"]` → `upper` 消费 `text` 参数，返回 `"HELLO"`；
2. 多余参数：`Args = ["hello", foo: 1, 42]` → `text` 消费成功，`finish` 报 `unexpected argument: foo`；
3. 命名误用：`Args = [text: "hi"]` → 报 ``the argument `text` is positional`` 并带提示。

**操作步骤**：

1. 新建文件 `crates/typst/tests/u3l5_args_practice.rs`（示例代码；Cargo 自动发现 `tests/` 下的集成测试，**无需修改任何 Cargo.toml**；总装 crate `typst` 再导出了 typst-library 全部模块及 comemo，依赖齐全）：

   ```rust
   use typst::comemo::{Track, TrackedMut};
   use typst::diag::{FileError, FileResult};
   use typst::engine::{Engine, Route, Sink, Traced};
   use typst::foundations::{
       Arg, Args, Bytes, Context, Datetime, Duration, NativeFunc, Value,
   };
   use typst::introspection::EmptyIntrospector;
   use typst::syntax::{FileId, RootedPath, Span, Spanned, VirtualPath, VirtualRoot};
   use typst::text::{Font, FontBook};
   use typst::utils::{LazyHash, Protected};
   use typst::{Library, LibraryExt, World};

   /// 最小 World：只有 library 与空的字体簿会被用到。
   struct MiniWorld {
       library: LazyHash<Library>,
       book: LazyHash<FontBook>,
       main: FileId,
   }

   impl World for MiniWorld {
       fn library(&self) -> &LazyHash<Library> {
           &self.library
       }
       fn book(&self) -> &LazyHash<FontBook> {
           &self.book
       }
       fn main(&self) -> FileId {
           self.main
       }
       fn source(&self, id: FileId) -> FileResult<typst::syntax::Source> {
           Err(FileError::NotFound(id.vpath().get_without_slash().into()))
       }
       fn file(&self, id: FileId) -> FileResult<Bytes> {
           Err(FileError::NotFound(id.vpath().get_without_slash().into()))
       }
       fn font(&self, _: usize) -> Option<Font> {
           None
       }
       fn today(&self, _: Option<Duration>) -> Option<Datetime> {
           None
       }
   }

   #[test]
   fn call_upper_by_hand() {
       let world = MiniWorld {
           library: LazyHash::new(Library::new([])),
           book: LazyHash::new(FontBook::new()),
           main: RootedPath::new(VirtualRoot::Project, VirtualPath::new("main.typ").unwrap())
               .intern(),
       };

       // 仿照 convergence.rs 的 compute() 构造 ad-hoc Engine。
       let introspector = EmptyIntrospector;
       let traced = Traced::default();
       let mut sink = Sink::new();
       let world_dyn: &dyn World = &world;
       let mut engine = Engine {
           library: world.library(),
           world: world_dyn.track(),
           introspector: Protected::new(introspector.track()),
           traced: traced.track(),
           sink: sink.track_mut(),
           route: Route::default(),
       };

       // `#[func]` 宏为 upper 生成了同名零变体枚举，实现 NativeFunc。
       let upper = typst::text::upper::func();
       let span = Span::detached();
       let context = Context::none();

       // 场景 1：只有期望的位置参数 → 消费成功。
       let args = Args::new(span, [Value::Str("hello".into())]);
       let ok = upper.call::<Value>(&mut engine, context.track(), args, span);
       match ok {
           Ok(Value::Str(s)) => assert_eq!(s.as_str(), "HELLO"),
           other => panic!("expected Ok(HELLO), got {other:?}"),
       }

       // 场景 2：期望参数 + 一个命名多余参数 + 一个位置多余参数。
       let mut args = Args::new(span, [Value::Str("hello".into())]);
       args.items.push(Arg {
           span,
           name: Some("foo".into()),
           value: Spanned::new(Value::Int(1), span),
       });
       args.items.push(Arg {
           span,
           name: None,
           value: Spanned::new(Value::Int(42), span),
       });
       let err = upper
           .call::<Value>(&mut engine, context.track(), args, span)
           .unwrap_err();
       assert!(
           err[0].message.contains("unexpected argument: foo"),
           "actual: {}",
           err[0].message
       );

       // 场景 3：把本该位置的参数写成了命名参数。
       let mut args = Args::new(span, Vec::<Value>::new());
       args.items.push(Arg {
           span,
           name: Some("text".into()),
           value: Spanned::new(Value::Str("hi".into()), span),
       });
       let err = upper
           .call::<Value>(&mut engine, context.track(), args, span)
           .unwrap_err();
       assert!(
           err[0].message.contains("the argument `text` is positional"),
           "actual: {}",
           err[0].message
       );
   }
   ```

2. 运行（首次需先完成 u1-l2 的构建；加 `-- --nocapture` 便于配合 `dbg!` 观察）：

   ```bash
   cargo test -p typst --test u3l5_args_practice -- --nocapture
   ```

3. 实践完成后删除该临时文件，仓库即恢复原状。

**需要观察的现象**：

- 场景 1 通过：`expect("text")` 取走 `"hello"`，`Caseable::Str` 经 `to_uppercase` 返回 `HELLO`，`finish` 时列表已空；
- 场景 2 失败于 `unexpected argument: foo`：宏生成的包装闭包先 `expect("text")`（消费成功），再 `args.take().finish()?` 时剩余列表的第一项是命名参数 `foo`（[macros/func.rs:386-387](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs#L386-L387)、[args.rs:261-271](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L261-L271)）；
- 场景 3 失败于 ``the argument `text` is positional``（[args.rs:163-178](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/args.rs#L163-L178)）。

**预期结果**：三个断言全部通过即证明你对调用链的理解正确。断言中的消息文案直接取自上述源码行，但整个测试尚未在本环境运行过——**待本地验证**；若断言失败，请把实际消息与源码对照，那本身就是最好的练习。

## 6. 本讲小结

- `Func` 是脚本函数值的统一 Rust 表示，内部五形态（Native/Element/Closure/Plugin/With）在 `call_impl` 中按 `match` 派发；闭包经 `Routines` 函数指针表间接进入 typst-eval 的 `eval_closure`，这是 crate 拆分架构的落地。
- `Args` 是「消费式」参数协议：`expect`/`eat`/`find`/`all`/`named` 边取边 cast 边删除，`finish` 兜底报多余参数；重复命名参数取最后一个；`missing_argument` 对「位置参数写成命名」给出专门提示。
- cast 三 trait 分工明确：`Reflect` 描述与快速判断、`IntoValue` 无损上行、`FromValue` 可失败下行；`CastInfo::error` 生成你平时见到的 `expected X, found Y` 及其提示。
- `#[func]` 宏按参数属性生成消费代码（variadic→all、named→named、默认→eat、必选→expect）、`NativeFuncData` 元数据与同名零变体枚举；返回值经 `IntoResult` 统一附带调用点 span。
- 求值侧 `ast::Args::eval` 负责 spread 展开（array→位置、dict→命名、arguments→原样并入），方法调用把接收者插为第一个参数。
- 在 Rust 里调用内置函数只需要：最小 World + ad-hoc Engine + 手工 `Args` + `Func::call`——综合实践已给出完整可粘贴的模板。

## 7. 下一步学习建议

- **下一讲 u3-l6（解释器主循环）**：本讲的 `ast::FuncCall::eval` 只是 typst-eval 的一角，下一讲把 `code.rs`/`markup.rs`/`binding.rs`/`import.rs`/`flow.rs` 串成完整的求值地图，其中 `eval_closure` 的参数绑定循环（[call.rs:692-737](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L692-L737)）会再次出现。
- **继续阅读**：[crates/typst-library/src/foundations/func.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs) 的 `ParamInfo`/`NativeParamInfo`（IDE 与文档站的数据源）；[crates/typst-macros/src/func.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/func.rs) 可配合 `cargo expand -p typst-library`（需自行安装 cargo-expand，见 u5-l5）看真实展开产物。
- 若对 `Value::Dyn` 与第三方类型的 cast 感兴趣，可预习 [cast.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/cast.rs) 中 `Dynamic` 相关的 `FromValue` 实现（在 value.rs 中，u3-l3 已铺垫）。
