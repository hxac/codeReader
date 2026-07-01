# select! 宏

## 1. 本讲目标

在上一讲（u3-l9）里，我们剖析了 `Select` 的**动态**多路选择算法——它在运行期构建任意长度的操作列表，再走「try_select → register → wait_until → unregister → accept」五阶段。本讲把视角上移一层：**用户写的 `select! { ... }` 宏，是怎么翻译成那套运行时调用的？**

读完本讲，你应当能够：

1. 说清 `select!` 宏内部的**两个阶段**（解析 / 代码生成）以及各自的子规则（`@list` / `@list_error` / `@case` / `@init` / `@count` / `@add` / `@complete`）。
2. 理解声明式宏（`macro_rules!`）如何用「内部 helper 标记 + 递归重写」实现状态机式的分阶段解析，并在编译期给出**精准的语法诊断**。
3. 掌握 `@init` 的三条**编译期短路优化**——常见单 `recv` 场景会被直接改写成 `recv()` / `try_recv()` / `recv_timeout()`，根本不走 `Select` 机制。
4. 能用 `cargo expand` 看一段 `select!` 展开后的真实代码，并标注它对应 `select.rs` 的 `select` / `try_select` / `select_timeout` 与五阶段算法的哪几个阶段。
5. 认识 `default` 分支的三种形态、case 数量上限（32），以及 `SelectedOperation` 必须「选中即完成」的强约束。

---

## 2. 前置知识

本讲假设你已经掌握以下内容（均在前面讲义建立）：

- **声明式宏 `macro_rules!` 的基础**：宏通过「模式匹配 token 树 → 重写」来工作；一条规则形如 `(模式) => { 展开体 }`；宏可以递归调用自身，每次重写消耗一部分 token，直到无法再匹配为止。`$x:tt` 匹配单个 token 树，`$($x:tt)*` 匹配重复的 token 序列，`$e:expr` 匹配一个表达式。
- **内部 helper 标记**：为了让宏分阶段处理，常见手法是在调用自己时在最前面塞一个以 `@` 开头的「阶段标记」，例如 `crossbeam_channel_internal!(@list ...)`，用不同的模式区分「现在处于哪个阶段」。这种写法可以藏在 `#[doc(hidden)]` 后面，对用户不可见。
- **crossbeam-channel 的 select 运行时**（u3-l9）：`Select<'a>` 在运行期持有 `&dyn SelectHandle` 操作列表，`run_select` 是核心算法，`SelectedOperation` 是「已经开始、必须完成」的半成品操作，`Selected` 是用 `AtomicUsize` 编码的 `Waiting/Aborted/Disconnected/Operation` 四态状态机。
- **flavor 与 SelectHandle 契约**（u3-l3）：每个 `Sender`/`Receiver` 都实现 `SelectHandle` trait，提供 `try_select/register/accept/...` 等方法；宏生成的代码最终都落到这些方法上。
- **`Token` 与两阶段协议**（u3-l3 / u3-l9）：选中阶段只「占位」并把现场写进 `Token`，完成阶段才真正读写消息；这是 `SelectedOperation::recv/send` 存在的原因。

如果对其中某点生疏，建议先回看对应讲义再继续——本讲会直接复用这些结论，不再重复推导。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪些部分 |
|------|------|------------------|
| `crossbeam-channel/src/select_macro.rs` | `select!` 与 `select_biased!` 宏的全部定义，以及内部 helper 宏 `crossbeam_channel_internal!` 的所有阶段规则 | 全文（1168 行）几乎都涉及，是本讲主角 |
| `crossbeam-channel/src/select.rs` | select 运行时：`Token`/`Operation`/`Selected`、`run_select` 五阶段算法、`select`/`try_select`/`select_timeout` 三个入口、`SelectedOperation` | `try_select`/`select`/`select_timeout`（宏展开后的调用目标）、`SelectedOperation::recv/send/index/Drop`、`sender_addr`/`receiver_addr` |
| `crossbeam-channel/src/lib.rs` | `internal` 模块把运行时函数以 `#[doc(hidden)]` 暴露给宏 | `internal` 模块的重导出（宏里写 `$crate::internal::select` 的落点） |

> 提示：宏的源码读起来像「状态机翻译表」——不要试图从上到下顺序读完，而是按「阶段」分组阅读。下面每个模块都会聚焦某一组规则。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应宏的两个阶段加一座「连接运行时的桥」：

- **4.1 解析阶段**：`@list` / `@list_error` / `@case`，把用户的自由语法规范化为统一的 case 列表，并给出编译期诊断。
- **4.2 代码生成阶段**：`@init`（含三条编译期优化）→ `@count` → `@add` → `@complete`，把 case 列表翻译成可执行代码。
- **4.3 select! 与底层 Select 的对应**：宏生成的 `_sel` 数组、`internal::select*` 三个入口、`SelectedOperation` 的强约束，如何对接 u3-l9 的运行时五阶段。

### 4.1 解析阶段：从用户语法到规范化 case 列表

#### 4.1.1 概念说明

用户写 `select!` 时，语法相当灵活：

- 每个分支以 `recv` / `send` / `default` 开头；
- `recv`/`send` 后**必须**用 `-> 模式` 绑定结果，而 `default` 用 `=>`（因为它没有结果）；
- 分支体既可以是 `=> { 块 }`，也可以是 `=> 表达式`；
- 分支之间用逗号分隔，但如果体是 `{ 块 }`，逗号可以省略；
- `default` 可以不带括号（`default =>`），也可以带超时（`default(100ms) =>`）。

这种「多种写法都合法」的灵活性，对用户友好，但对宏来说是负担。因此解析阶段的核心目标是：**把所有合法变体都重写成同一种内部规范形态**，再交给后续的代码生成阶段统一处理。

crossbeam 的做法是经典的「**两遍解析**」：

1. **第一遍 `@list`**：宽松地切分出一个个 case（容忍逗号省略、表达式/块两种体），同时把明显错误（如 `recv =>` 漏了 `->`）拦截下来；产出「待校验的 case 序列」。
2. **第二遍 `@case`**：对每个 case 的参数列表做**严格**校验（`recv` 恰好一个参数、`send` 恰好两个参数、`default` 零或一个参数），通过则收入正式 cases，否则报错。

> 关键设计：`@list` 只做「切分」，`@case` 才做「校验」。两者职责分离，让诊断信息更精准。

#### 4.1.2 核心流程

解析阶段是一个**递归下降**的状态机，`@list` 不断吃掉 token、把已处理的 case 累积到第二个参数 `($($head)*)` 里，直到输入为空，再交给 `@case`。

```text
select! { recv(r) -> msg => body, default => fallback }
   │
   ▼  入口规则把整体喂给 @list
@list (recv(r) -> msg => body, default => fallback) ()
   │  @list 逐个吃 case，累积进 $head
   ▼  ……（每吃一个就递归 @list 一次，输入越来越短）
@list () (recv(...) -> ... => {...}, default() => {...},)
   │  输入空 → 切换到 @case 做严格校验
   ▼
@case (recv(...) -> ... => {...}, default() => {...},) () ()
   │  @case 逐个校验参数个数，收入正式 cases
   ▼
@case () <cases> <default>
   │  全部校验通过 → 进入代码生成阶段 @init
   ▼
@init <cases> <default>
```

任何一步若匹配不到「成功」规则，就落到「错误」规则，调用 `compile_error!` 在编译期报错。

`default` 的容错处理值得一提：用户写 `default => ...` 时没有参数列表，`@list` 会**主动补一个空列表** `default() => ...`，让后续阶段可以统一按「`default(参数)`」处理。这是 `@list` 的第一组规则在做的事。

#### 4.1.3 源码精读

**文件头注释**已经把两阶段的总览讲清楚了——这是阅读本宏最好的入口：

> 这段注释明确指出宏分「解析」与「代码生成」两大阶段，并列出各自的子规则名，与本讲的模块划分完全一致。

- [select_macro.rs:1-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1-L21) — 宏的阶段总览注释。

**`@list` 的「补 default 空参数」规则**——它排在最前面，先于严格切分，把裸 `default =>` 改写成 `default() =>`：

```rust
// If necessary, insert an empty argument list after `default`.
(@list
    (default => $($tail:tt)*)
    ($($head:tt)*)
) => {
    $crate::crossbeam_channel_internal!(
        @list
        (default() => $($tail)*)
        ($($head)*)
    )
};
```

- [select_macro.rs:38-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L38-L47) — 裸 `default` 自动补 `()`。
- [select_macro.rs:49-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L49-L65) — 拦截 `default ->` / `default(...) ->`，报「`default` 后应为 `=>`」。
- [select_macro.rs:67-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L67-L83) — 拦截 `recv(...) =>` / `send(...) =>`（漏了 `->`），报「应为 `->`」。

注意规则顺序在 `macro_rules!` 里至关重要：**Rust 按从上到下的顺序尝试匹配，第一个匹配成功的规则胜出**。所以「错误拦截」规则必须排在「通用切分」规则之前，否则 `recv =>` 会被通用规则错误地当成合法 case 吞掉。

**`@list` 的「通用切分」规则**——这是正常路径，把一个合法 case 从输入搬到累积区 `$head`，并支持「块后可省逗号」「表达式后必须逗号」两种体：

- [select_macro.rs:100-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L100-L131) — 逗号分隔的块体 / 表达式体切分，以及「块体可省逗号」规则。
- [select_macro.rs:132-142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L132-L142) — 最后一个 case（可选尾逗号）。
- [select_macro.rs:25-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L25-L36) — 输入空时切换到 `@case`。

**`@list_error1/2/3/4` 的分级诊断**——当 `@list` 的所有「成功」规则都匹配失败时，落到 `@list_error1`，它再分四级逐步定位错误，给出比「invalid syntax」更具体的提示：

- [select_macro.rs:143-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L143-L149) — `@list` 失败时进入 `@list_error1`。
- [select_macro.rs:150-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L150-L168) — 第一级：检查 case 类型必须是 `recv`/`send`/`default`。
- [select_macro.rs:172-193](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L172-L193) — 第二级：检查是否漏了参数列表。
- [select_macro.rs:195-354](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L195-L354) — 第三级：检查 `=>` 及其后的体，覆盖「漏 `=>`」「`}` 后误用分号」「函数调用后漏逗号」等大量典型笔误。
- [select_macro.rs:355-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L355-L358) — 第四级兜底：`invalid syntax`。

**`@case` 的严格参数校验**——逐个检查每个 case 的参数个数，例如 `recv` 必须恰好一个表达式参数：

```rust
// Check the format of a recv case.
(@case
    (recv($r:expr $(,)?) -> $res:pat => $body:tt, $($tail:tt)*)
    ($($cases:tt)*)
    $default:tt
) => {
    $crate::crossbeam_channel_internal!(
        @case
        ($($tail)*)
        ($($cases)* recv($r) -> $res => $body,)
        $default
    )
};
```

- [select_macro.rs:374-385](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L374-L385) — `recv` 的成功匹配（恰好一个 `$r:expr`），收入正式 cases。
- [select_macro.rs:416-427](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L416-L427) — `send` 的成功匹配（恰好两个参数 `$s:expr, $m:expr`）。
- [select_macro.rs:457-482](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L457-L482) — `default` 的两种合法形态：`default()` 与 `default($timeout:expr)`。注意 `default` 被收进**单独的**第三个参数 `$default`，且 [select_macro.rs:484-492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L484-L492) 检测重复 `default`（一个 `select!` 只能有一个 `default`）。
- [select_macro.rs:360-371](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L360-L371) — 全部 case 校验通过，切换到代码生成阶段 `@init`。

#### 4.1.4 代码实践

**实践目标**：体会解析阶段的「分级语法诊断」——故意触发几类典型笔误，观察宏在编译期给出的精准错误。

**操作步骤**：

1. 新建一个临时 crate 或在已有的 crossbeam 依赖项目里，写下面这段**故意带错**的 `select!`：

```rust
// 示例代码：用于触发宏的语法诊断，逐个取消注释观察编译错误
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s, r) = unbounded::<i32>();

    select! {
        // 错误 A：recv 后漏了 `->`，应报 "expected `->` after `recv` case, found `=>`"
        // recv(r) => msg => {},

        // 错误 B：default 后误用 `->`
        // default -> {},

        // 错误 C：send 只传了一个参数
        // send(s) -> res => {},

        // 错误 D：两个 default
        // default => {}, default => {},

        // 正确：取消下面这行注释以保证块可编译
        recv(r) -> msg => { let _ = msg; },
    }
    let _ = s;
}
```

2. 每次只取消**一个**错误分支的注释，运行 `cargo build`，记录编译器报出的 `compile_error!` 消息。
3. 对照 [select_macro.rs:67-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L67-L83)、[select_macro.rs:387-399](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L387-L399)、[select_macro.rs:429-441](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L429-L441)、[select_macro.rs:484-492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L484-L492)，确认每条消息分别由哪条规则产生。

**需要观察的现象**：错误信息是「宏作者手写的中文友好提示」（如「expected `->` after `recv` case, found `=>`」），而不是编译器默认的 token 流噪声。

**预期结果**：每个笔误都对应一条精确诊断——这正是 `@list_error` 四级分类与 `@case` 严格校验的产物。

> 若本地无 nightly 工具链，本实践只需 `cargo build`（stable 即可），不涉及 `cargo expand`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `@list` 里「拦截 `recv(...) =>` 漏 `->`」的规则（第 67-74 行）必须排在「通用切分」规则（第 100-110 行）之前？

**答案**：`macro_rules!` 按规则书写顺序自上而下匹配，第一个能匹配的规则胜出。`recv(...) => ...` 这串 token 也能被通用切分规则里的 `$case:ident $args:tt $(-> $res:pat)* => ...` 匹配（因为 `$(-> $res:pat)*` 允许零次出现），于是漏 `->` 的笔误会被当成合法 case 静默吞下，错误被推迟到很后面、信息变模糊。把错误规则前置，才能在第一时间拦截。

**练习 2**：`default` 在 `@case` 阶段被收进**第三个参数** `$default` 而非和 `recv`/`send` 一起进 `$cases`，这样做有什么好处？

**答案**：`default` 语义上与 `recv`/`send` 不同——它不是「通道操作」而是「兜底分支」，且全局只能有一个。把它单独存放，既方便 [select_macro.rs:484-492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L484-L492) 检测「重复 default」，也方便代码生成阶段（`@init`/`@add`）用「第二个参数是否为 `()`」一句话区分「有无 default」并据此选择 `try_recv`/`recv_timeout`/阻塞等不同代码路径。

---

### 4.2 代码生成阶段：从 case 列表到可执行代码

#### 4.2.1 概念说明

解析阶段产出一个规范化的 case 列表（外加可选的 `default`）。代码生成阶段的任务，是把这些**编译期**的 token 翻译成一段**运行期** Rust 代码，其核心动作是：构建一个操作数组、调用运行时入口、完成被选中的操作。

但在进入「通用生成」之前，宏先做了一件很聪明的事——**编译期短路优化**。许多 `select!` 其实只有一个 `recv` 分支：

```rust
select! { recv(r) -> msg => body }                       // 阻塞收
select! { recv(r) -> msg => body, default => fallback }  // 非阻塞收
select! { recv(r) -> msg => body, default(t) => fallback } // 带超时收
```

这些场景**根本不需要** `Select` 那套五阶段算法——直接调 `Receiver::recv()` / `try_recv()` / `recv_timeout()` 即可，开销小得多。`@init` 在编译期识别出这三种形态，直接展开成对应的普通方法调用，跳过整个 `Select` 机制。

只有当优化不命中（例如多个 `recv`、或含 `send`），`@init` 才走「通用路径」：声明一个定长数组 `_sel`，把每个操作填进去，再交给 `@add` 启动选择、`@complete` 完成操作。

#### 4.2.2 核心流程

代码生成阶段是一个清晰的流水线：

```text
@init  ← 先尝试三条「单 recv」短路优化；命中则直接展开为 recv()/try_recv()/recv_timeout()
  │
  └─ 优化不命中 → 通用 @init：
        1. @count 编译期数出 case 数量 N（const _LEN）
        2. 声明 _sel: [(&dyn SelectHandle, usize, usize); N]
        3. 附带一份「最多 32 个标签」的清单 (_oper0.._oper31)
        ↓
@add   ← 递归地把每个 recv/send 填进 _sel[i]，并把 case 重写成带 [i] 下标的形态
  │     最后根据 default 形态选择运行时入口：
  │       无 default      → internal::select(...)           阻塞
  │       default()       → internal::try_select(...)        非阻塞
  │       default(timeout)→ internal::select_timeout(...)    带超时
  ↓
@complete ← 拿到 SelectedOperation 后，按 oper.index() 匹配 [i]，调用 oper.recv()/oper.send() 完成操作
```

四个子规则的接力关系：`@count` 是 `@init` 内部的一个**常量计数**调用；`@init` 通用分支末尾调用 `@add`；`@add` 把数组填满后调用 `@complete`。

#### 4.2.3 源码精读

**`@init` 的三条短路优化**——这是最值得一看的部分。例如「单 recv + 非阻塞 default」被直接改写成 `try_recv()`：

```rust
// Optimize `select!` into `try_recv()`.
(@init
    (recv($r:expr) -> $res:pat => $recv_body:tt,)
    (default() => $default_body:tt,)
) => {{
    match $r {
        ref _r => {
            let _r: &$crate::Receiver<_> = _r;
            match _r.try_recv() {
                ::std::result::Result::Err($crate::TryRecvError::Empty) => {
                    $default_body
                }
                _res => {
                    let _res = _res.map_err(|_| $crate::RecvError);
                    let $res = _res;
                    $recv_body
                }
            }
        }
    }
}};
```

可以看到，展开后的代码里**完全没有** `Select`、没有 `_sel` 数组、没有 `internal::select`——它就是一个普通的 `try_recv()` 匹配。三种优化分别对应三种常见单 recv 场景：

- [select_macro.rs:538-557](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L538-L557) — 单 recv + `default()` → `try_recv()`。
- [select_macro.rs:559-571](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L559-L571) — 单 recv 无 default → `recv()`。
- [select_macro.rs:573-592](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L573-L592) — 单 recv + `default(timeout)` → `recv_timeout(timeout)`。

> 小知识：源码里 [select_macro.rs:594-690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L594-L690) 还留着几段**被注释掉的优化**（双 recv、单 send），标注 `TODO(stjepang): Implement this optimization.`——说明作者本想覆盖更多场景，但目前只有单 recv 三条已实现。

**通用 `@init`：声明数组 + 附带 32 标签**——优化不命中时走这里。它先用 `@count` 算出 `const _LEN`，再声明 `_sel` 数组，并把一份「标签清单」透传给 `@add`：

- [select_macro.rs:693-744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L693-L744) — 通用 `@init`。

注意其中两个关键细节：

1. `const _LEN: usize = $crate::crossbeam_channel_internal!(@count (...));`（第 697 行）——在编译期把 case 数量算成一个常量，用来给数组定长。
2. 那份标签清单 `(0usize _oper0) (1usize _oper1) ... (31usize _oper31)`（第 708-741 行）——每个标签是「下标 + 变量名」对。`@add` 每填一个操作就**消耗一个标签**，用其中的 `$var:ident` 作为承载该通道引用的局部变量名、`$i:tt` 作为数组下标。**清单只有 32 项，这正是 case 数量上限的来源**——一旦 case 超过 32，标签耗尽，[select_macro.rs:837-845](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L837-L845) 会报 `too many operations in a 'select!' block`。

**`@count`：编译期递归计数**——模式匹配地「数」case 个数：

```rust
(@count ()) => { 0 };
(@count ($oper:ident $args:tt -> $res:pat => $body:tt, $($cases:tt)*)) => {
    1 + $crate::crossbeam_channel_internal!(@count ($($cases)*))
};
```

- [select_macro.rs:747-752](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L747-L752) — 递归地 `1 + @count(剩余)`，编译期归约为一个常量。

**`@add`：填数组 + 选运行时入口**——每条 `@add` 规则吃一个 case，把它写入 `_sel[$i]`，并把 case 重写成带下标 `[i]` 的形态塞进 `$cases`，递归直到输入空，再按 `default` 形态选择入口：

- [select_macro.rs:847-877](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L847-L877) — 处理 `recv`：把 `(handle, i, receiver_addr)` 写进 `_sel[$i]`，并用一处 `unsafe` 的 `unbind` 把 `Receiver` 引用的生命周期擦除（详见 4.3）。
- [select_macro.rs:879-909](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L879-L909) — 处理 `send`，对称地写入 `(handle, i, sender_addr)`。
- [select_macro.rs:755-775](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L755-L775) — 输入空且**无 default**：调 `internal::select(&_mut sel, _IS_BIASED)`（阻塞）。
- [select_macro.rs:777-805](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L777-L805) — 输入空且 `default()`：调 `internal::try_select(...)`（非阻塞），返回 `None` 走 default 体。
- [select_macro.rs:807-835](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L807-L835) — 输入空且 `default(timeout)`：调 `internal::select_timeout(...)`。

三处都有一句 `unsafe { ::std::mem::transmute(_oper) }`——它把 `SelectedOperation` 的生命周期擦掉，目的是「让 `_sel` 能在不必等 `SelectedOperation` 离开作用域时就提前释放」。注释明说这是为了**在不支持 NLL（非词法生命周期）的旧编译器上**也能让 `sel` 早 drop，从而避免 `sel` 数组里那些 `&dyn SelectHandle` 借用不必要地延长。

**`@complete`：按下标完成操作**——拿到 `SelectedOperation` 后，按 `oper.index() == $i` 逐个匹配，命中者调 `oper.recv()/oper.send()` 真正完成：

- [select_macro.rs:912-931](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L912-L931) — 完成一个 `recv`：`let _res = $oper.recv($r);` 后执行用户体。
- [select_macro.rs:933-952](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L933-L952) — 完成一个 `send`，对称。
- [select_macro.rs:954-962](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L954-L962) — 理论上不可达的兜底 `unreachable!`（下标必然命中某个 case）。

注意 `{ $sel };`（第 919、939 行）这一句——它显式地「使用一下 `_sel`」强制它在完成操作后立即 drop，释放借用。

#### 4.2.4 代码实践

**实践目标**：用 `cargo expand`（或手动推理）亲眼看到 `@init` 的三条短路优化，体会「单 recv 的 select! 根本不走 Select」。

**操作步骤**：

1. 安装 cargo-expand（需要 nightly）：`rustup component add rustc-codegen-cranelift` 不需要，直接 `cargo install cargo-expand`（或对单次使用：`cargo +nightly expand`）。
2. 写一段含三个独立 `select!` 的示例：

```rust
// 示例代码：用于观察 @init 短路优化
use std::time::Duration;
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s, r) = unbounded::<i32>();
    let _ = &s;

    // 形态 1：单 recv 无 default → 应展开为 r.recv()
    select! { recv(r) -> msg => { let _ = msg; } }

    // 形态 2：单 recv + default() → 应展开为 r.try_recv() 的 match
    select! {
        recv(r) -> msg => { let _ = msg; },
        default => {},
    }

    // 形态 3：单 recv + default(timeout) → 应展开为 r.recv_timeout(timeout) 的 match
    select! {
        recv(r) -> msg => { let _ = msg; },
        default(Duration::from_millis(50)) => {},
    }
}
```

3. 运行 `cargo +nightly expand`（在临时 crate 内）查看展开结果。
4. 在展开输出里**搜索** `try_recv` / `recv_timeout` / `::recv`，确认三段分别命中三条优化规则。

**需要观察的现象**：展开后的代码里**找不到** `Select`、`_sel`、`internal::select` 字样——它们被普通的 `try_recv`/`recv`/`recv_timeout` 取代。

**预期结果**：三个 `select!` 分别展开成 [select_macro.rs:538-592](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L538-L592) 三条规则的展开体。

> 待本地验证：若没有 nightly，可改为「手动推理」——对照三条 `@init` 规则，把示例代码逐一替换成其展开体，得出同样的结论。

#### 4.2.5 小练习与答案

**练习 1**：`select!` 的 case 数量上限是多少？这个上限是由哪段代码决定的？能否通过修改一处把上限提高到 64？

**答案**：上限是 **32**。它由通用 `@init` 里附带的标签清单决定——[select_macro.rs:708-741](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L708-L741) 只列了 `(0usize _oper0) ... (31usize _oper31)` 共 32 个标签。`@add` 每处理一个 case 就消耗一个标签，标签耗尽即触发 [select_macro.rs:837-845](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L837-L845) 的 `too many operations` 错误。理论上可以往清单里续写 `(32usize _oper32) ... (63usize _oper63)` 把上限提到 64（这是纯编译期、token 级的修改），但需同步确认 `_sel` 数组与下游能承受更大定长。

**练习 2**：为什么「单 recv」的三条优化值得做，而「单 send」的优化（源码里被注释掉）至今未实现？

**答案**：`recv` 是 `select!` 最高频的用法（同时监听多个接收端、或给单个接收端加超时/非阻塞语义），优化收益大、模式简单（`recv`/`try_recv`/`recv_timeout` 三个现成方法一一对应）。而「单 send」语义上较少见、且 `send` 还要携带消息值、错误类型映射更繁琐，作者用 `TODO` 标注后一直未补完，故仍走通用路径。这是一种典型的「按收益排优先级」的工程取舍。

---

### 4.3 select! 与底层 Select 运行时的对应

#### 4.3.1 概念说明

优化不命中的通用路径里，宏生成的代码最终会调用三个**运行时入口**：`internal::select`、`internal::try_select`、`internal::select_timeout`，并和一个叫 `SelectedOperation` 的类型打交道。本模块就把这座「宏 ↔ 运行时」的桥架起来，回答两个问题：

1. 宏里写的 `$crate::internal::select` 到底指哪个函数？`internal` 模块是什么？
2. 宏生成的 `_sel` 数组、`@add`/`@complete` 的接力，如何对应 u3-l9 讲过的 `run_select` 五阶段算法？

此外，宏里出现了几处 `unsafe { transmute }`，本模块也讲清它们为何是必要的、又在保证什么不变量。

#### 4.3.2 核心流程

先看宏与运行时的接线总览：

```text
select! 宏生成的代码                 运行时 (select.rs / lib.rs)
─────────────────────               ──────────────────────────
_sel: [(&dyn SelectHandle,usize,usize); N]
        │  每个元素是 (channel 的 SelectHandle, 下标, channel 地址)
        ▼
internal::select(&_sel, _IS_BIASED)  ──→  pub fn select() ──→ run_select(Timeout::Never)
internal::try_select(...)            ──→  pub fn try_select() ──→ run_select(Timeout::Now)
internal::select_timeout(...,t)      ──→  pub fn select_timeout() ──→ run_select(Timeout::At)
        │                                           │
        ▼                                           ▼
   返回 SelectedOperation                    run_select 五阶段（u3-l9）：
        │                                    ① try_select 全部 ② register 进 Waker
        ▼                                    ③ wait_until 阻塞 ④ unregister ⑤ accept
   @complete 按 oper.index() 匹配 [i]
        │
        ▼
   oper.recv(r) / oper.send(s, m)   ──→  SelectedOperation::recv/send → channel::read/write
```

三个入口函数的差别，全在一个 `Timeout` 枚举上：`Now`（不阻塞）、`Never`（永久阻塞）、`At(Instant)`（阻塞到时刻）。`run_select` 据此决定是否进入睡眠。

`SelectedOperation` 是一座「半成品」桥——它代表「某个操作已被选中、占位信息已写进 `Token`，但消息还没真正读写」。因此它有一个**强约束**：必须被 `recv()`/`send()` 完整消费，否则在 drop 时 panic。

#### 4.3.3 源码精读

**`internal` 模块：宏的运行时入口表**——宏里写 `$crate::internal::select`，落点就在 lib.rs 这个 `#[doc(hidden)]` 模块。它把 select.rs 里若干函数重导出给宏用，但对普通用户隐藏（`#[doc(hidden)]`）：

```rust
/// Crate internals used by the `select!` macro.
#[doc(hidden)]
#[cfg(feature = "std")]
pub mod internal {
    pub use crate::select::{
        SelectHandle, receiver_addr, select, select_timeout, sender_addr, try_select,
    };
}
```

- [lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375) — `internal` 模块重导出 `select`/`try_select`/`select_timeout`/`SelectHandle`/`sender_addr`/`receiver_addr`。

**三个运行时入口：差别只在 `Timeout`**——它们的签名几乎一样，都接收 `&mut [(&dyn SelectHandle, usize, usize)]`，区别仅是传给 `run_select` 的 `Timeout` 值：

```rust
pub fn try_select<'a>(
    handles: &mut [(&'a dyn SelectHandle, usize, usize)],
    is_biased: bool,
) -> Result<SelectedOperation<'a>, TrySelectError> {
    match run_select(handles, Timeout::Now, is_biased) { ... }
}

pub fn select<'a>(
    handles: &mut [(&'a dyn SelectHandle, usize, usize)],
    is_biased: bool,
) -> SelectedOperation<'a> {
    if handles.is_empty() { panic!("no operations have been added to `Select`"); }
    let (token, index, addr) = run_select(handles, Timeout::Never, is_biased).unwrap();
    SelectedOperation { token, index, addr, _marker: PhantomData }
}
```

- [select.rs:456-469](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L456-L469) — `try_select`（`Timeout::Now`）。
- [select.rs:474-489](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L474-L489) — `select`（`Timeout::Never`，空操作集会 panic）。
- [select.rs:494-503](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L494-L503) — `select_timeout`（`Timeout::At`）。
- [select.rs:161-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L161-L170) — `Timeout` 枚举三态。

注意 `is_biased` 参数：它来自 `select!` 宏里 `const _IS_BIASED: bool = false;`（`select!`）或 `true`（`select_biased!`），用来决定 `run_select` 是否在开头 `shuffle` 操作顺序以实现公平性——

- [select_macro.rs:1135-1146](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1135-L1146) — `select!` 宏设 `_IS_BIASED = false`。
- [select_macro.rs:1156-1167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1156-L1167) — `select_biased!` 设 `_IS_BIASED = true`。
- [select.rs:196-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) — `run_select` 开头：非偏置时 `shuffle` 以保证公平。

这就是 `select!` 与 `select_biased!` 的唯一差别——其余完全共用 `crossbeam_channel_internal!`。`select!` 在多个操作同时就绪时**随机**选一个；`select_biased!` 则**总是选最靠前**的那个。

**`run_select` 与五阶段算法**——u3-l9 已详述，这里只做接线回顾。宏生成的「填数组 → 调入口」最终都进入 `run_select`：

- [select.rs:176-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L176-L211) — `run_select` 开篇：先创建空 `Token`，第一轮 `try_select` 乐观尝试所有操作（对应五阶段的「① try_select 全部」），命中即返回 `(token, index, addr)`；否则进入 `loop` 走「register → wait_until → unregister」的阻塞循环。

> 五阶段细节（`try_select` → `register` → `wait_until` → `unregister` → `accept`）见 u3-l9，本讲不重复。宏与运行时的对接点就是：宏负责「构造 `handles` 数组 + 选入口 + 完成」，运行时负责「在这批 handles 上跑五阶段」。

**`SelectedOperation`：必须完成的半成品**——它持有 `token`（占位现场）、`index`（被选中操作的下标）、`addr`（被选中 channel 的地址）。它的 `recv`/`send` 方法在完成时**校验地址**，确保用户传入的 channel 正是被选中的那个：

```rust
pub fn recv<T>(mut self, r: &Receiver<T>) -> Result<T, RecvError> {
    assert!(r.addr() == self.addr, "passed a receiver that wasn't selected");
    let res = unsafe { channel::read(r, &mut self.token) };
    mem::forget(self);   // 完成后阻止 Drop 运行
    res.map_err(|_| RecvError)
}
```

- [select.rs:1209-1221](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1209-L1221) — `SelectedOperation` 结构（`token`/`index`/`addr`/`_marker`）。
- [select.rs:1248-1250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1248-L1250) — `index()` 返回被选中操作下标（宏的 `@complete` 据此匹配 `[i]`）。
- [select.rs:1276-1284](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1276-L1284) — `send`：地址校验 + `channel::write` + `mem::forget(self)`。
- [select.rs:1310-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1310-L1318) — `recv`：地址校验 + `channel::read` + `mem::forget(self)`。
- [select.rs:1327-1331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1327-L1331) — `Drop for SelectedOperation`：**直接 panic**「dropped without completing」。

`mem::forget(self)` 与 `Drop` 的 panic 是一对组合拳：`SelectedOperation` 代表「已经开始但未完成」的操作，底层 flavor 可能已经为它预占了槽位（例如 array flavor 的 start 阶段占了一个 stamp）。如果半途丢弃而不完成，会留下「幽灵占位」破坏通道状态——所以类型层面用「drop 即 panic」逼你一定要调 `recv`/`send`，而这两个方法内部用 `mem::forget` 主动逃避 panic。宏的 `@complete` 正是据此设计成「选中即立即完成」。

**关于宏里那几处 `unsafe { transmute }`**——它们有两类用途：

1. [select_macro.rs:860-863](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L860-L863) 的 `unbind`：把 `&Receiver`/`&Sender` 的生命周期擦成 `'a`，让它们能塞进 `_sel` 数组并被提前 drop。安全性由「`_sel` 与 `SelectedOperation` 都不会逃出 `select!` 块、调用方在块内始终持有原始 channel 引用」这一结构不变量保证。
2. [select_macro.rs:766](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L766) 对 `_oper` 的 `transmute`：擦掉 `SelectedOperation` 的生命周期，同样为了让 `_sel` 能早 drop（如注释所述，兼容非 NLL 编译器）。

这两处都是「为了借用精确释放而手动放宽生命周期」，属于宏层面精心控制的 `unsafe`，与通道内部的 `unsafe`（flavor 算法）正交。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `SelectedOperation` 的 `Drop` 与地址校验，理解为何 `select!` 宏的 `@complete` 必须「选中即立即完成」、且只对被选中的那个 channel 调用 `recv`/`send`。

**操作步骤**：

1. 阅读 [select.rs:1327-1331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1327-L1331) 的 `Drop`，确认它一定会 panic。
2. 阅读 [select.rs:1310-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1310-L1318) 的 `recv`，注意 `mem::forget(self)`——它阻止 `Drop` 触发。
3. 回到宏的 `@complete`（[select_macro.rs:912-931](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L912-L931)），观察它的结构：`if $oper.index() == $i { $oper.recv($r); ... } else { 递归 @complete }`——它用下标精确匹配，**只对命中的那个 case** 调用 `recv`/`send`，从而保证 `SelectedOperation` 必然在分支内被消费。
4. 思考：如果用户在 `select!` 的 case 体里写了 `return`/`?`/`break`，`SelectedOperation` 还会被消费吗？（提示：`@complete` 是 `if/else` 表达式，`$oper.recv()` 在执行用户体 `$body` **之前**就已调用并 `forget`，所以用户体内的任何控制流都不会跳过完成步骤。）

**需要观察的现象**：完成操作（`oper.recv/oper.send`）与用户体（`$body`）的先后关系——先完成、后执行用户代码。

**预期结果**：理解 `SelectedOperation` 的「必须完成」约束如何与宏的 `@complete` 结构互相咬合，从而**保证无论用户体如何书写，通道状态都不会留下幽灵占位**。

> 待本地验证：本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`select!` 和 `select_biased!` 的源码差别有多大？这个差别最终影响了运行时的哪段逻辑？

**答案**：源码差别极小——仅 `_IS_BIASED` 一个常量（`false` vs `true`），[select_macro.rs:1135-1167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1135-L1167)，其余完全共用 `crossbeam_channel_internal!`。该布尔值经 `internal::select*` 传入 `run_select` 的 `is_biased` 参数，影响 [select.rs:196-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199)：`false` 时先 `shuffle` 操作顺序（多操作同时就绪则随机选一个，公平），`true` 时保持原序（总选最靠前，可预测）。

**练习 2**：为什么 `SelectedOperation` 的 `Drop` 要直接 panic，而不是「自动取消操作」？

**答案**：一旦 `run_select` 返回 `SelectedOperation`，底层 flavor 通常已经为该操作**预占了资源**（如 array flavor 在 start 阶段改了某个槽的 stamp、zero flavor 准备了交接 packet）。此时「自动取消」需要为每个 flavor 实现一套回滚协议，复杂且易错。crossbeam 选择了更强 but更简单的契约：**选中即必须完成**。用 `Drop → panic` 把契约提升为类型层面的硬约束，再用 `recv/send` 内的 `mem::forget` 为「正常完成」开一道安全出口。宏的 `@complete` 结构正是为了从语法上保证这道出口必然被走到。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**主线任务**（即规格里要求的代码实践）：

> 写一段 `select!` 同时 `recv` 两个通道并带 `default` 超时分支，用 `cargo expand`（或手动推理）查看展开结果，标注展开代码对应 `select.rs` 的哪几个阶段。

**操作步骤**：

1. 准备示例代码（这是「不会被短路优化」的通用路径——因为有**两个** `recv`）：

```rust
// 示例代码：综合实践 —— 两路 recv + 超时
use std::time::Duration;
use std::thread;
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s1, r1) = unbounded::<i32>();
    let (s2, r2) = unbounded::<i32>();

    thread::spawn(move || { thread::sleep(Duration::from_millis(80)); let _ = s1.send(1); });
    thread::spawn(move || { thread::sleep(Duration::from_millis(80)); let _ = s2.send(2); });

    select! {
        recv(r1) -> msg => println!("r1: {:?}", msg),
        recv(r2) -> msg => println!("r2: {:?}", msg),
        default(Duration::from_millis(50)) => println!("timed out"),
    }
}
```

2. 运行 `cargo +nightly expand`（或对照源码手动展开）。
3. 在展开结果中找出并**标注**以下片段，填入对应 `select.rs` 阶段：

| 展开后的代码片段 | 对应宏规则 | 对应 select.rs 阶段/函数 |
|------------------|-----------|--------------------------|
| `const _LEN: usize = ...`（值为 2） | `@count` | ——（编译期计数） |
| `let mut _sel = [...];`（含两个被填入的 `(&dyn SelectHandle, i, addr)`） | `@init` 通用 + `@add` | `run_select` 的输入 `handles` |
| `internal::select_timeout(&mut _sel, Duration::from_millis(50), _IS_BIASED)` | `@add`（default(timeout)） | `select_timeout` → `run_select(Timeout::At)` |
| `if _oper.index() == 0 { _oper.recv(_r1); ... } else if ...` | `@complete` | `SelectedOperation::recv` → `channel::read` |
| `shuffle`（因 `_IS_BIASED=false`） | `select!` 设 `_IS_BIASED=false` | `run_select` 开头 [select.rs:196-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) |

4. 把展开代码与 u3-l9 的五阶段算法对照：标注「try_select 全部」对应展开里的哪一句、「register/wait_until」藏在 `internal::select_timeout` 调用内部、「accept」即 `_oper.recv(...)`。

**需要观察的现象**：一个看似简单的 `select!`，展开后是一段约 30~40 行、包含 `unsafe` 块、定长数组、`internal::select_timeout` 调用与 `@complete` 分发的代码。

**预期结果**：你能指着展开代码的每一行，说出它来自宏的哪个规则、对应运行时的哪个阶段。至此，宏与运行时的对应关系完全打通。

> 待本地验证：若没有 nightly 工具链，可改为「手动推理」——按本讲 4.2 与 4.3 给出的规则与模板，手工把示例 `select!` 翻译成展开体，再完成同样的标注。

---

## 6. 本讲小结

- `select!` 宏是「**解析** + **代码生成**」两阶段状态机，由内部 helper 宏 `crossbeam_channel_internal!` 用 `@阶段名` 标记分阶段递归重写实现；对用户隐藏（`#[doc(hidden)]`）。
- **解析阶段** = `@list`（宽松切分 + 早期拦截）→ `@list_error1..4`（四级诊断）→ `@case`（严格校验参数个数）。规则顺序很关键，错误规则必须前置；`default` 被单独存放以便查重与选路径。
- **代码生成阶段** = `@init`（先试三条单 recv 短路优化）→ `@count`（编译期计数）→ `@add`（填 `_sel` 数组 + 选运行时入口）→ `@complete`（按下标完成）。单 recv 场景会被直接改写成 `recv`/`try_recv`/`recv_timeout`，根本不走 `Select`。
- **case 数量上限 32**，由通用 `@init` 附带的标签清单 `_oper0.._oper31` 决定；超出报 `too many operations`。
- 宏与运行时通过 `internal` 模块（[lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375)）对接，三个入口 `select`/`try_select`/`select_timeout` 的差别仅在传给 `run_select` 的 `Timeout`（`Never`/`Now`/`At`）。
- `select!` 与 `select_biased!` 唯一差别是 `_IS_BIASED` 常量，它决定 `run_select` 是否 `shuffle`（公平 vs 偏置）。
- `SelectedOperation` 是「必须完成」的半成品，`Drop → panic` + `recv/send` 内 `mem::forget` 是一对组合拳，宏的 `@complete` 结构从语法上保证它必然被消费。

---

## 7. 下一步学习建议

本讲是 crossbeam-channel 单元的收官篇。建议：

1. **横向对比**：把本讲的 `select!`（编译期、定长、可短路优化）与 u3-l9 的 `Select`（运行期、动态、可变长）并排复习，写一张表对比二者在「分支数何时确定」「能否优化」「公平性」「典型场景」上的差异，巩固「何时用哪个」的判断。
2. **动手扩展**：尝试仿照 `@init` 已实现的三条优化，为「单 send」补一条短路优化（参考 [select_macro.rs:655-690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L655-L690) 的 TODO），用 `try_send`/`send`/`send_timeout` 实现，体会宏作者当时未完成的工作量。
3. **进入下一单元**：crossbeam-channel 的并发正确性依赖大量 `unsafe` 与精细的内存序。下一单元（u4）将转向 crossbeam-queue，那里的 `ArrayQueue`/`SegQueue` 同样是无锁设计，会复用本单元建立的对 ABA、stamp 编码、`CachePadded` 的理解，是向 u5（crossbeam-epoch 内存回收）过渡的良好台阶。
4. **延伸阅读**：若对宏的「分阶段状态机」写法感兴趣，可对比标准库 `vec!`、`println!` 的内部 helper 宏实现，体会这一 Rust 宏惯用法（TT munching）的通用模式。
