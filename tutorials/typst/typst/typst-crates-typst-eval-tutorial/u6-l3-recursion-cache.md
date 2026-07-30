# 递归安全、栈增长与缓存

## 1. 本讲目标

Typst 是一门图灵完备的语言：用户可以写函数、可以递归调用、可以让模块互相 `import`、也可以让 `show` 规则反复套娃。只要允许「递归」，就必须防范三种会让编译器崩溃或卡死的灾难——

1. **无限循环**：一个模块求值时又求值自己（循环 eval / 循环 import），永不退出。
2. **过深递归**：函数调用层层嵌套，逻辑上可能终止，但层数深到不健康。
3. **物理栈溢出**：递归虽然层数有限，但每一层占用的 C/Rust 栈帧之和超过了操作系统分配的栈空间，进程直接 `segfault` 崩溃。

本讲把 typst-eval 的「运行时安全」三条防线一次性讲透，并补上贯穿全局的缓存机制。读完本讲，你应当能够：

- 说清 `Route` 如何同时承担「循环防护」与「调用深度检查」两件截然不同的事，以及为什么前者用 `panic!`、后者用 `Err`。
- 解释 `stacker::maybe_grow` 在 32 KB / 2 MB 两个阈值下如何动态地把调用挪到新栈上，以及为什么 wasm32 要绕开它。
- 理解 `#[comemo::memoize]` 对 `eval` 与 `eval_closure` 的缓存命中机制，并能说出 `Tracked<World>`、`TrackedMut<Sink>` 等参数如何参与缓存键的相等性判定、副作用如何被重放。

## 2. 前置知识

本讲是整本手册（u1–u6）的收尾，默认你已读过 u1-l3（`eval`/`eval_string` 入口）、u4-l3（`eval_closure`）、u5-l1（`import`/`include`）。下面对几个关键术语做最小回顾，确保后面读到时不卡壳：

- **求值（eval）**：把 AST 节点转成运行时 `Value` / `Content` 的过程。typst-eval 是 tree-walking interpreter（遍历式解释器），调用栈 = Rust 的函数调用栈。
- **`Vm` 虚拟机**：携带求值状态（作用域、控制流、追踪）的容器，每求值一个模块、每调用一次函数都新建一个。
- **`Route`（路线）**：记录「当前这次编译是从哪里一步步走过来」的链式结构，是本讲的核心数据结构，定义在 typst-library 的 `engine.rs`。
- **comemo**：Typst 自研的记忆化（memoization）库，用 `#[comemo::memoize]` 标注的函数，相同输入会直接返回缓存的输出。
- **`Tracked` / `TrackedMut`**：comemo 提供的「可追踪句柄」。把一个值包装成 `Tracked` 后，它对它的每一次方法调用都会被 comemo 记账，从而既能做缓存判等，也能重放副作用。`TrackedMut` 是可变版本。

> 一句话直觉：**前两节讲「怎么别让程序把解释器拖垮」，第三节讲「怎么让解释器别重复做无用功」。**

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| `src/lib.rs` | `eval` 与 `eval_string` 两个入口：循环防护的第一现场、comemo 缓存的标注点。 |
| `src/call.rs` | `FuncCall::eval` 里调用 `check_call_depth`；`call_func` 里调用 `stacker::maybe_grow`；`eval_closure` 是第二个被 comemo 缓存的函数。 |
| `src/import.rs` | `import_file` 里做循环导入防护，并递归调用 `eval`。 |
| `crates/typst-library/src/engine.rs`（跨 crate 支持） | `Route` 结构体的定义、`contains` / `check_call_depth` / `within` / `extend` / `with_id` 的实现，以及 `MAX_CALL_DEPTH` 常量。 |

> 注意：`Route` 并不在 typst-eval 里，而在 typst-library。typst-eval 只是它的「使用者」。我们把它作为支持类型一起精读，因为三道防线里有两道都建在它之上。

## 4. 核心概念与源码讲解

### 4.1 Route：一条链，两道防线（循环防护 + 调用深度）

#### 4.1.1 概念说明

`Route` 是一个「调用链快照」。每当解释器要进入一个**新的求值上下文**（求值一个新模块、或调用一个函数），它都会把当前的 `Route` 作为「外层（outer）」，再套一层新的「段（segment）」上去。于是整条链就记录了「我是从哪个模块、经过哪些调用，一路走到这里的」。

这条链同时服务两个**完全不同**的目的，理解它们的区别是本讲的关键：

| 用途 | 关心的字段 | 检查方法 | 命中时的处理 | 性质 |
| --- | --- | --- | --- | --- |
| **循环防护** | `id`（文件 id） | `contains(id)` | `panic!`（eval）/ `bail!`（import） | 链上是否**出现过同一个文件** |
| **调用深度** | `len`（嵌套长度） | `check_call_depth()` | 返回 `Err`（用户错误） | 链的**总长度**是否超限 |

为什么循环防护要 `panic!`？因为「一个文件在求值途中又出现在自己的求值链里」属于**编译器内部不变量被破坏**——typst-eval 在进入一个模块求值前，理应保证它不在链上；如果链上已经有它，说明缓存（comemo）或调用编排出了 bug，这不该是用户能直接触发的事，所以用 panic 暴露出来。而「循环 import」（A 导入 B、B 又导入 A）是用户代码的常见错误，必须给出友好报错，所以走 `bail!` 返回 `Err`。

#### 4.1.2 核心流程

先看 `Route` 的数据结构（定义在 typst-library）：

[engine.rs:258-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L258-L281) 展示了 `Route<'a>` 的四个字段：

- `outer`：指向父段的可追踪句柄（链表的「下一个」）。
- `id`：如果这一段是因为「开始求值某个模块」而产生的，就记录该模块的 `FileId`；否则为 `None`。
- `len`：这一段的「长度」。进入函数调用、嵌套布局、套用 show 规则时会让 `len` 累加。整条链的长度 = 各段 `len` 之和。
- `upper`：父链长度的一个**上界**（原子变量），是一个缓存优化，后面 4.1.4 会讲。

**循环防护**靠 `id` 字段。`contains(id)` 沿 `outer` 链递归地问「有没有哪一段的 `id` 等于目标文件」：

```text
contains(target_id):
  若 self.id == Some(target_id) → true（命中循环）
  否则若存在 outer → 递归问 outer.contains(target_id)
  否则 → false
```

**调用深度**靠 `len` 字段累加，`within(MAX_CALL_DEPTH)` 判断「整条链长度是否 ≤ 上限」。每进入一次函数调用，解释器并不会真的改写 `Route`（它是不可变的快照），而是通过 comemo 的 tracked 句柄让「深度」这一信息在调用间传递；`check_call_depth()` 读当前深度，超限就报错。

#### 4.1.3 源码精读

**① 循环 eval 防护（panic）**：`eval()` 一进来就检查。

[src/lib.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L48-L52) —— 求值一个 `Source` 前先 `route.contains(id)`，命中即 `panic!`，注释明说这是防止「cyclic evaluation」。

注意紧接其后的 [src/lib.rs:62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L54-L63)：构造新的 `Engine` 时用 `Route::extend(route).with_id(id)`，把当前文件的 id **挂到新的一段上**——这正是让后续深层 `eval` 能通过 `contains` 发现「我又绕回自己」的关键。

**② 循环 import 防护（用户友好报错）**：`import_file` 里同样的 `contains` 检查，但用 `bail!`。

[src/import.rs:227-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L227-L245) —— 先向 World 请求 `Source`，再 `engine.route.contains(source.id())` 命中就 `bail!(span, "cyclic import")`，最后才递归调用 `eval`。因为 `eval` 内部那次 `contains` 检查（①）依赖 `with_id` 把 id 挂上，而递归 `eval` 复用的正是 `import_file` 已经延伸过的 `engine.route`，两层防护配合严密。

> 为什么需要两层？`import_file` 的 `bail!` 面向用户、带 span、可被 `trace` 叠加「while importing ...」回溯；`eval` 的 `panic!` 是兜底的不变量。正常情况下用户只会撞到外层友好的 `cyclic import`。

**③ 调用深度检查（用户报错）**：每次函数调用前都查。

[src/call.rs:31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L24-L32) —— `FuncCall::eval` 第一件事就是 `vm.engine.route.check_call_depth().at(span)?`。数学模式下的函数调用 `eval_math_call` 也一样，见 [src/call.rs:98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L98)。

`check_call_depth` 与 `MAX_CALL_DEPTH` 的定义在 typst-library：

[engine.rs:388-393](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L351-L393) —— `MAX_CALL_DEPTH: usize = 80`，`check_call_depth` 在 `!self.within(80)` 时返回 `bail!("maximum function call depth exceeded")`。注意它与 show 规则深度（64）、布局深度（72）、HTML 深度（72）是**不同**的常量，刻意拉开差距，好让不同类型的「过深」报出各自更贴切的错误。

**④ `Route::extend` / `with_id` / `track`**：

[engine.rs:294-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L294-L323) —— `extend(outer)` 套一段默认 `len=1`、`id=None` 的新段；`with_id(id)` 给段贴上文件 id；`track()` 在跟踪前做一个优化：如果这一段既没 id 又 `len=0`（对缓存无贡献），就直接跳过它，复用 outer，避免无谓地让缓存键变化。

在 `eval_closure` 里也能看到同样的延伸模式：[src/call.rs:672](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L665-L673) 用 `Route::extend(route)` 给函数调用建立新的求值上下文（注意这里**不**带 `with_id`，因为函数调用不引入新文件）。

#### 4.1.4 关于 `within` 的上界优化（选读）

`contains` 是朴素的链表递归。但 `within(depth)` 不能也朴素地每次都走到底——否则深度检查本身就变成 O(深度) 的开销，而且会破坏 comemo 缓存复用（同样的计算在不同深度会被当成不同的缓存键）。

[engine.rs:405-428](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L404-L428) 用 `upper: AtomicUsize` 缓存「父链长度的一个上界」。核心逻辑可概括为：

\[ \text{命中缓存: } \text{upper} + \text{self.len} \le \text{depth} \Rightarrow \text{within} = \text{true} \]

即如果「父链上界 + 本段长度」已经 ≤ 上限，就不必再往上游走，直接判定「在限度内」。只有需要真正递归时才往下问，并在确认在限度内时用 `compare_exchange` 把 `upper` 收紧（只降不升）。这让深度检查在大多数情况下是 O(1)，同时不牺牲缓存的复用性——注释里那句「knowing the exact length would defeat the whole purpose because it would prevent cache reuse」正是在解释这一点。

#### 4.1.5 代码实践

**实践目标**：亲手触发循环 import，观察「用户友好报错」；并对比想象一下循环 eval（panic）的差别。

1. 准备两个文件，互相 `include`：
   - `a.typ`：`#include "b.typ"`
   - `b.typ`：`#include "a.typ"`
2. 用 typst CLI 编译 `a.typ`：`typst compile a.typ`。
3. 观察输出：应当得到一条带 span 的 `cyclic import` 错误（来自 `import_file` 的 `bail!`），而不是 Rust 的 panic。
4. （源码阅读型）回到 [src/import.rs:231-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L227-L245)，跟踪 `route.contains` 是如何沿 `outer` 链发现 `a.typ` 的 id 已经在链上：`a` 求值 → 延伸 route（带 a 的 id）→ `include b` → `b` 求值（`eval` 内 `with_id(b)`）→ `b` 里 `include a` → `route.contains(a)` 命中 → `bail!`。

**预期结果**：用户看到清晰的 `error: cyclic import`，定位到 `b.typ` 中的 include 语句。

> 待本地验证：不同 typst 版本对循环 import 的报错措辞与 span 定位可能略有差异，以你本地的 CLI 输出为准。

#### 4.1.6 小练习与答案

**练习 1**：`eval` 的循环防护用 `panic!`，`import_file` 用 `bail!`，为什么不统一？

> **答案**：`panic!` 表示编译器内部不变量被破坏（按设计，一个文件进入求值前就该确保不在链上，命中说明缓存/编排出 bug，不应让用户能直接触发）；`bail!` 返回 `Err` 是面向用户的正常错误，能带 span、能被 `trace` 叠加「while importing」。用户写的循环 import 属于后者，故走 `bail!`。

**练习 2**：`MAX_CALL_DEPTH = 80`，而 `MAX_SHOW_RULE_DEPTH = 64`。为什么故意让它们不一样？

> **答案**：让不同类型的嵌套报出各自专属的错误。若两者相等，当 show 规则与函数调用交错嵌套时，就分不清该报「show 套娃」还是「调用过深」。把 show 的上限设得更低，保证一旦是 show 规则的问题，会优先命中 show 那条更贴切的错误信息（见 engine.rs 第 336-340 行的注释）。

---

### 4.2 stacker::maybe_grow：动态栈增长，防爆栈

#### 4.2.1 概念说明

调用深度检查（`check_call_depth`）是一道**逻辑**防线：它在层数到 80 时就拦住。但 80 层真的安全吗？不一定。typst-eval 是 tree-walking interpreter，求值一个 Typst 表达式可能在 Rust 侧消耗**多层**栈帧（`Expr::eval` → `eval_code` → 又一个 `Expr::eval` → `call_func` → `eval_closure` → `body.eval` → ……）。而且 Typst 的很多内置元素（布局、show 规则）也会回调进 eval。所以「逻辑层数」与「实际 Rust 栈深度」并不一一对应。

更糟的是：即便逻辑层数有限，**总栈字节数**仍可能超出操作系统给线程的栈（典型 1–8 MB），导致进程直接段错误，连报错都来不及。

`stacker` 解决的就是后者。它的思路是：当检测到当前栈「快用完了」（剩余 < 红线），就在**堆**上分配一块新的大栈，把接下来的闭包挪到新栈上执行，从而突破单块物理栈的限制。`maybe_grow(red_zone, stack_size, f)` 的两个阈值含义：

- `red_zone`（红线）：剩余栈低于此值时才触发换栈；高于则直接在当前栈执行，零额外开销。
- `stack_size`：换栈时新分配的栈大小。

\[ \text{if 剩余栈} < \text{red\_zone} \Rightarrow \text{在大小为 stack\_size 的新栈上运行 } f \]

#### 4.2.2 核心流程

`call_func` 把真正的函数执行包成一个闭包 `f`，然后交给 `stacker::maybe_grow`：

```text
call_func(vm, func, args, span):
  构造闭包 f = || func.call(&mut engine, context, args).trace(...)
  若 wasm32 → 直接 return f()        // stacker 在 wasm 上有缺陷，禁用
  否则 → stacker::maybe_grow(32KB, 2MB, f)
```

为什么把换栈点放在 `call_func`？因为**函数调用**正是递归/嵌套加深的地方。在每一层调用前留一个「栈检查 + 可能换栈」的关口，就为整条求值链提供了动态的栈伸缩能力。

#### 4.2.3 源码精读

[src/call.rs:166-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L166-L181) —— `call_func` 的全部实现。要点：

- 第 170-173 行：`f` 闭包封装了 `func.call(...)` 以及 `.trace(...)`（给深层错误叠加「while calling …」回溯帧）。
- 第 175-177 行：`#[cfg(target_arch = "wasm32")] return f();`，注释「Stacker is broken on WASM」。
- 第 179-180 行：非 wasm 走 `stacker::maybe_grow(32 * 1024, 2 * 1024 * 1024, f)`，即红线 32 KB、新栈 2 MB。

依赖声明也印证了这一点：[Cargo.toml:28-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/Cargo.toml#L28-L29) 把 `stacker` 放在 `cfg(not(target_arch = "wasm32"))` 的 target-specific 依赖下——wasm 平台根本不引入这个 crate，所以代码里必须用 `#[cfg]` 分两条返回路径，否则 wasm 编译会因找不到 `stacker` 而失败。

> **为什么 wasm 要例外？** stacker 依赖读取栈指针并做栈切换（stack switching），这需要平台相关的汇编或 `makecontext` 之类机制，在 wasm 的线性内存模型下无法可靠实现（注释直言「broken」）。wasm 通常运行在浏览器/Node 的 worker 里，有自己的内存上限与栈管理，故直接 `f()` 不做换栈。

#### 4.2.4 代码实践

**实践目标**：理解「逻辑深度」与「物理栈」两道关卡的分工。

1. 阅读型实践：对照 [src/call.rs:31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L24-L32)（`check_call_depth`）与 [src/call.rs:179-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L166-L181)（`stacker`），回答：这两行代码谁先执行？分别防的是什么？
2. 思考题：假设把 `MAX_CALL_DEPTH` 调大到 80000，同时移除 `stacker::maybe_grow`，递归求值一个深递归 Typst 函数会发生什么？
   - **预期分析**：`check_call_depth` 在 80000 层才拦，但 Rust 栈早在几百到几千层时（取决于每层帧大小）就已溢出，没有 stacker 兜底就会**段错误**崩溃，连 `maximum function call depth exceeded` 都报不出来。这正是两道关卡缺一不可的原因：`check_call_depth` 防止「层数不健康」，`stacker` 防止「字节数超限」。

> 待本地验证：用一段故意深递归的 Typst（如 `#let f(n) = { if n == 0 { none } else { f(n - 1) } }; #f(1000)`）编译，观察是先撞到 `maximum function call depth exceeded`（逻辑关卡）还是更深的异常。注意不要把 `n` 调到真的会爆栈的量级。

#### 4.2.5 小练习与答案

**练习**：`stacker::maybe_grow` 的两个参数 `32 * 1024` 和 `2 * 1024 * 1024` 分别是什么？为什么不把红线设得很大（比如 1 MB）？

> **答案**：第一个是红线 `red_zone`（剩余栈 < 32 KB 时触发换栈），第二个是新栈大小 `stack_size`（2 MB）。红线设太大，会让绝大多数调用都白白触发换栈（堆上分配 + 栈切换有成本），抹平了「平时零开销、临危才换栈」的设计意图；设太小，又可能在两次检查之间就被递归吃光栈而段错误。32 KB 是兼顾「留给单层调用的安全余量」与「不频繁触发」的经验值。

---

### 4.3 comemo::memoize：求值结果的记忆化缓存

#### 4.3.1 概念说明

前两节是「安全」，这一节是「性能」。Typst 的增量编译会反复求值同一批模块/函数：一个文档里 `include` 了十次的子模块、被 show 规则反复触发的函数……如果每次都从头求值，编译会慢得不可接受。

comemo 的 `#[comemo::memoize]` 给函数套上一层**按输入指纹缓存输出**的机制：第一次用某组参数调用时正常执行并记下 `(参数指纹 → 输出)`；之后只要参数指纹相同，就直接返回缓存输出，跳过整个函数体。

typst-eval 里有两个被 memoize 的关键函数：

- `eval`：求值一个 `Source` 得到 `Module`（u1-l3 详述）。
- `eval_closure`：执行一个用户定义函数得到 `Value`（u4-l3 详述）。

缓存它们意味着：**同一个文件、在同样的 world/library/route/traced 上下文下，只求值一次；同一个闭包、在同样的参数与上下文下，也只执行一次。**

#### 4.3.2 核心流程

comemo 的判等不是「指针相等」，而是**指纹（fingerprint）相等**：

```text
memoize 函数被调用(args...):
  fp = 把每个参数的指纹拼起来
  若缓存里有 fp → 取出缓存的 output，并重放其中记录的 TrackedMut 副作用 → 返回
  否则 → 真正执行函数体，期间 comemo 记录所有对 TrackedMut 的写入
        把 (fp → output, 副作用记录) 存进缓存 → 返回
```

关键点有两个：

1. **每个参数都贡献指纹**：普通引用类型（如 `&LazyHash<Library>`、`&Func`、`Args`）按内容/哈希贡献；`Tracked<T>` / `TrackedMut<T>` 类型则按其底层被追踪值的内容贡献。
2. **可变句柄的副作用被重放**：`TrackedMut<Sink>` 比较特殊——函数体里对 sink 写入的警告、追踪值等，在缓存命中时会被**重放**到当前的 sink 上，而不是简单地丢弃。这样缓存既省了重复计算，又不丢副作用。

#### 4.3.3 源码精读

**① `eval` 的 memoize**：[src/lib.rs:37-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L37-L47)。注意参数表里全是可追踪/可哈希的句柄：`world: Tracked<dyn World>`、`library: &LazyHash<Library>`、`traced: Tracked<Traced>`、`sink: TrackedMut<Sink>`、`route: Tracked<Route>`、`source: &Source`。这些**共同**构成缓存键。同一个 `source`，只要 `world`/`library`/`route`/`traced` 任一变化（指纹不同），就触发重算；全相同则命中缓存。

**② `eval_closure` 的 memoize**：[src/call.rs:634-647](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L633-L647)。参数更长：`func`、`closure`、`world`、`library`、`introspector`、`traced`、`sink`、`route`、`context`、`args`。同一个闭包 + 同样的 `args` + 完全相同的上下文，第二次调用直接拿缓存结果。这就是为什么 typst-ide（u6-l2）即便为了 hover 反复求值整个文件，开销也可控——大部分子调用都命中了缓存。

**③ `TrackedMut<Sink>` 的副作用重放**：Sink 的设计印证了 comemo 的重放机制。看 [engine.rs:145-159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L145-L159) 的注释——Sink 的所有 tracked 方法都是 `(&mut self, ..) -> ()` 形式（写入型），comemo 正是靠记录这些写入并在命中时重放，来保证「缓存命中也不丢警告/追踪值」。并行执行时把每个子任务的 sink 收集起来再 `extend` 回外层 sink（[engine.rs:90-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L90-L99)），也是同一套「subsink 回放」思想。

**④ `route` 作为缓存键的意义**：注意 `eval_closure` 的参数里有 `route: Tracked<Route>`。这看起来矛盾——route 不是会随调用加深而变化吗，岂不是每次都缓存失效？

这正是 `Route::within` 的 `upper` 上界优化（4.1.4）和 `track()` 跳过空段（4.1.3 ④）的用武之地：它们刻意让「同一段求值在不同但都未超限的深度下」尽量产生**相同**的指纹，从而**最大化缓存复用**。换句话说，route 参与缓存键，是为了在「上下文真的变了」（比如 world 变了、模块变了）时正确失效；而 route 内部的优化，是为了在「只是深度数字不同、语义等价」时不白白失效。这是 comemo 缓存与 Route 设计之间最精妙的配合。

#### 4.3.4 代码实践

**实践目标**：把「缓存键 = 所有参数指纹」和「TrackedMut 副作用重放」这两件事用自己的话讲清楚。

1. 阅读型实践：列出 `eval_closure`（[src/call.rs:636-647](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L633-L647)）的全部参数，按下表分类：

   | 参数 | 类型 | 指纹来源 | 在缓存中的角色 |
   | --- | --- | --- | --- |
   | `func` | `&Func` | 函数身份 | 区分「是哪个函数」 |
   | `closure` | `&LazyHash<Closure>` | LazyHash 哈希 | 区分闭包定义 |
   | `args` | `Args` | 参数内容 | 区分「用什么参数调用」 |
   | `world`/`library`/`introspector`/`traced`/`context` | `Tracked<…>` / `&LazyHash<…>` | 被追踪值内容 | 上下文变化则失效 |
   | `route` | `Tracked<Route>` | route 链（经 `upper` 优化） | 上下文变化则失效，但等价深度尽量复用 |
   | `sink` | `TrackedMut<Sink>` | 被追踪值内容 | 既参与判等，**副作用在命中时被重放** |

2. 思辨题：假设 typst-eval 没有给 `eval_closure` 加 `#[comemo::memoize]`，在一次编译里，某个被 show 规则触发了 100 次的纯函数会被求值多少次？加上 memoize 之后呢？
   - **预期分析**：无缓存则 100 次（每次 show 命中都重跑函数体）；有缓存且每次 `args`/上下文相同则只 1 次，其余 99 次命中缓存并重放 sink 副作用。

> 待本地验证：comemo 的精确指纹算法在其独立 crate 内，本讲只讲「按参数内容判等 + 重放可变副作用」这一合约层面的行为；若要核对指纹的具体计算，需阅读 comemo 源码。

#### 4.3.5 小练习与答案

**练习 1**：缓存 `eval_closure` 时，`Tracked<World>` 如何参与缓存键的相等性判定？

> **答案**：comemo 给 `Tracked<T>` 计算的指纹来自其底层被追踪值的内容（World 通过 `#[comemo::track]` 暴露的方法所产生的可观测行为）。两个 `Tracked<World>` 只有在「对同样的查询返回同样的结果」时指纹才相同；一旦底层 World 改变（例如用户编辑了文件、依赖变了），指纹随之改变，`eval_closure` 缓存对该 World 失效，触发重算。这正是增量编译「只重算受影响部分」的基础。

**练习 2**：为什么 `TrackedMut<Sink>` 比其他 `Tracked` 参数更特殊？

> **答案**：其他 `Tracked` 参数只读，只参与缓存键判等；而 sink 是写入目标，函数体执行时会对它 `warn`/记录追踪值。若缓存命中就跳过函数体，这些副作用就会丢失。所以 comemo 对 `TrackedMut` 额外做「副作用记录 + 命中时重放」：首次执行时记下所有对 sink 的写入，命中缓存时把这些写入重新应用到当前的 sink 上，保证警告与追踪值不丢。

## 5. 综合实践

把三道防线 + 缓存串起来，完成下面这张「运行时安全与性能」总表，并用一段话说明它们如何协同：

| 机制 | 位置 | 防止 / 解决的问题 | 触发后的处理 |
| --- | --- | --- | --- |
| `route.contains`（eval） | [src/lib.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L48-L52) | （你填） | （你填） |
| `route.contains`（import） | [src/import.rs:231-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L227-L245) | （你填） | （你填） |
| `route.check_call_depth` | [src/call.rs:31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L24-L32) | （你填） | （你填） |
| `stacker::maybe_grow` | [src/call.rs:179-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L166-L181) | （你填） | （你填） |
| `comemo::memoize` | [src/lib.rs:38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L37-L47) / [src/call.rs:634](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L633-L647) | （你填） | （你填） |

**参考答案要点**：

- `route.contains`（eval）→ 防止循环求值同一文件（内部不变量）→ `panic!`。
- `route.contains`（import）→ 防止循环 import（用户错误）→ `bail!("cyclic import")`，带 span、可叠加 trace。
- `check_call_depth` → 防止函数调用嵌套过深（逻辑层数 > 80）→ 返回 `Err("maximum function call depth exceeded")`。
- `stacker::maybe_grow` → 防止物理栈溢出（字节数超限，wasm 例外）→ 把后续调用挪到 2 MB 新栈上继续执行。
- `comemo::memoize` → 防止重复求值（性能）→ 相同指纹直接返回缓存，`TrackedMut<Sink>` 副作用重放。

**协同关系**：循环防护挡住「永不退出」的图灵灾难；`check_call_depth` 在逻辑层把嵌套压在 80 以内；`stacker` 在物理层兜底，保证即便单层帧很大也不会段错误；三者共同确保求值「一定会停下来且不崩」。而 `comemo::memoize` 在「一定会停下来」的前提下，让那些会被反复触发的求值只算一次——它依赖 `route` 等参数的指纹做判等，又靠 `Route::within`/`track` 的优化避免误失效，安全防线与缓存机制由此咬合成一个整体。

## 6. 本讲小结

- `Route` 是一条记录「怎么走到这里」的链，同时服务两个目的：用 `id` 字段做**循环防护**（`contains`），用 `len` 字段做**调用深度检查**（`check_call_depth`，上限 80）。
- 循环 eval 命中走 `panic!`（编译器内部不变量），循环 import 命中走 `bail!`（用户友好报错），两者都建立在 `Route::contains` 沿 `outer` 链递归查找之上；`eval` 用 `Route::extend(route).with_id(id)` 把当前文件挂进链。
- `stacker::maybe_grow(32KB, 2MB, f)` 在物理栈快用完时把函数执行挪到堆上的新栈，防段错误；wasm32 因 stacker 有缺陷而绕开（直接 `f()`），依赖声明也用 target-specific cfg 隔离。
- 逻辑深度（`check_call_depth`）防「层数不健康」，物理栈（`stacker`）防「字节数超限」，两者不可互相替代。
- `eval` 与 `eval_closure` 都带 `#[comemo::memoize]`：按**全部参数的指纹**做缓存键，相同指纹直接返回缓存输出；`TrackedMut<Sink>` 的写入副作用在命中时被重放，不丢警告与追踪值。
- `Route::within` 的 `upper` 上界优化与 `track()` 的跳空段优化，刻意让「等价但深度数字不同」的调用产生相同指纹，从而在 route 参与缓存键的同时最大化复用——这是安全机制与缓存机制最精妙的结合点。

## 7. 下一步学习建议

本讲是 typst-eval 学习手册的收官篇。到这里，你已经从 u1 的鸟瞰、u2 的表达式/语法模式、u3 的控制流、u4 的函数与捕获、u5 的模块与样式，一路读到了 u6 的诊断、追踪与运行时安全。建议的后续方向：

- **横向打通「求值 → 排版」**：typst-eval 产出的 `Module`/`Content` 会被交给 typst-library 的排版引擎（layout）和 introspection。`Route` 上还有 `MAX_LAYOUT_DEPTH`、`check_layout_depth` 等同族防线，可以去 `crates/typst-library/src/engine.rs` 对照本讲，看排版侧的递归安全如何复用同一套 Route 思想。
- **深入 comemo**：本讲只在「合约层」讲了指纹判等与副作用重放。若想真正理解增量编译为何高效，建议阅读 comemo crate 源码，看 `#[comemo::track]`/`#[comemo::memoize]` 宏如何生成指纹计算与缓存查改的代码。
- **动手做一个求值追踪实验**：结合 u6-l2 的 tracing 机制，给本讲的 `call_func` 加一条日志（仅本地实验，勿提交），观察一次真实文档编译中「缓存命中 vs 实际求值」的比例，直观感受 memoize 的威力。
- **回顾全册**：用本讲的「三道防线 + 缓存」视角，回头重新审视 u1-l3 的 `eval` 六步流程，你会发现那条主链上的每一处设计（循环防护、route 延伸、memoize、sink 重放）现在都已了然于胸。
