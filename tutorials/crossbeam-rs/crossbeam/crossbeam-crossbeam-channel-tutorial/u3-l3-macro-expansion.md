# select! 宏展开机制 select_macro.rs

## 1. 本讲目标

本讲是「select 内核」系列的第三篇。你已经在前两讲里分别学会了 `select!` 的**用法**（u2-l9）和它背后**调度算法** `run_select`（u3-l1）。这一讲要回答的是中间那块拼图：

> 当你在源码里写下一段 `select! { ... }`，编译器在真正编译它之前，到底把它「改写」成了什么样的 Rust 代码？

学完本讲你应该能够：

1. 说清 `select!` 与 `select_biased!` 两个宏的**唯一区别**在哪里、为什么只需一个布尔常量就能切换公平性。
2. 看懂内部宏 `crossbeam_channel_internal!` 的**两阶段架构**（解析阶段 `@list`/`@case` + 代码生成阶段 `@init`/`@count`/`@add`/`@complete`）。
3. 解释**单分支优化**：为什么只有「单个 `recv` + 可选 `default`」会退化成 `recv()` / `try_recv()` / `recv_timeout()`，而带 `send` 的不会。
4. 读懂生成的 **handle 数组**、**按 `index` 分发**、以及**完成操作**调用链，并把它们一一对应到 `internal` 模块与 `Select` 动态 API。

---

## 2. 前置知识

本讲假设你已经掌握下列概念（若不熟请先回看对应讲义）：

- **声明宏（`macro_rules!`）基础**：`$name:tt` / `$e:expr` / `$($t:tt)*` 等片段分类器（fragment specifier）、重复语法、以及「模式匹配 + 递归展开」的工作方式。`macro_rules!` 没有「函数调用」，它是靠**一条条 arms 递归地把 token 流改写**，直到无可改写。
- **`select!` 的语法**（u2-l9）：分支三段式 `recv(r) -> msg => body` / `send(s, v) -> res => body`，以及 `default` / `default(timeout)`。
- **`Select` 动态 API 与 `SelectedOperation`**（u2-l10）：操作被拆成「抢占 → 完成」两步，`SelectedOperation` 是必须完成的「占座凭证」。
- **`run_select` 与 `SelectHandle` trait**（u3-l1）：`select!` 宏最终就是去调用 `internal::select` / `try_select` / `select_timeout`，它们内部都走 `run_select`。

两个贯穿全讲的关键词先交代清楚：

- **handle（句柄）三元组**：宏为每个分支在栈上建一个数组，每个元素是 `(&dyn SelectHandle, usize index, usize addr)`——一个「参与选择的操作」的描述。这和 `Select` 动态 API 里 `handles: Vec<...>` 的元素是同一种结构（u2-l10 已讲过）。
- **`internal` 模块**：`src/lib.rs` 里一个 `#[doc(hidden)]` 的 `pub mod`，把 `select` / `try_select` / `select_timeout` / `sender_addr` / `receiver_addr` / `SelectHandle` 暴露出来，**专供宏展开后调用**。用户文档里看不到它，但宏离不开它。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/select_macro.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs) | 本讲主角。定义内部宏 `crossbeam_channel_internal!`（两阶段改写）和两个用户入口 `select!` / `select_biased!`。 |
| [src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | 宏展开后调用的目标：`select` / `try_select` / `select_timeout` 三个自由函数、`SelectedOperation`、`sender_addr` / `receiver_addr`。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs) | 末尾的 `pub mod internal` 把上述函数重新导出给宏用；`pub use` 把 `Select` / `SelectedOperation` 等导出给用户。 |
| [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | `Sender::addr` / `Receiver::addr`（被 `sender_addr` / `receiver_addr` 转发）与 `unsafe fn write` / `read`（被 `SelectedOperation::send` / `recv` 调用完成操作）。 |

---

## 4. 核心概念与源码讲解

### 4.1 入口：select! 与 select_biased! 的唯一差异

#### 4.1.1 概念说明

用户能直接调用的宏有两个：`select!`（无偏/随机）和 `select_biased!`（有偏/靠前优先）。它们**几乎完全相同**，连展开逻辑都不各自实现一份，而是都委托给同一个内部宏 `crossbeam_channel_internal!`。

两者的唯一区别，是在展开结果里植入一个布尔常量 `_IS_BIASED`：`select!` 植入 `false`，`select_biased!` 植入 `true`。这个常量随后被生成的代码透传给 `internal::select(..., _IS_BIASED)`，最终在 `run_select` 里决定「是否在仲裁前 `shuffle` 打乱操作顺序」（u3-l1 已讲：`shuffle` 带来公平性，跳过 `shuffle` 带来「靠前优先」）。

#### 4.1.2 核心流程

```
select! { ... }                          select_biased! { ... }
        |                                          |
        | 注入 const _IS_BIASED = false;           | 注入 const _IS_BIASED = true;
        v                                          v
  crossbeam_channel_internal!( ...同样的 token 流... )
```

伪代码（两个入口的骨架）：

```
macro_rules! select {
    ($($tokens:tt)*) => {{
        const _IS_BIASED: bool = false;        // ← 唯一差异点
        $crate::crossbeam_channel_internal!($($tokens)*)
    }};
}
// select_biased! 完全一样，只是 true
```

#### 4.1.3 源码精读

`select!` 入口，植入 `false`：

[src/select_macro.rs:1135-1146](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1135-L1146) — 用户入口 `select!`：把 token 原样包进一个块，在块作用域里定义 `const _IS_BIASED: bool = false;`，再交给内部宏。

`select_biased!` 入口，植入 `true`：

[src/select_macro.rs:1156-1167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1156-L1167) — 用户入口 `select_biased!`：与上面逐字相同，唯一差别是 `const _IS_BIASED: bool = true;`。

`_IS_BIASED` 在哪里被消费？在代码生成阶段 `@add` 的终止分支里，它作为参数透传：

[src/select_macro.rs:754-775](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L754-L775) — 阻塞式终止分支调用 `$crate::internal::select(&mut $sel, _IS_BIASED)`。`_IS_BIASED` 就是入口植入的那个常量，按作用域可见。

`run_select` 如何使用它（这是 u3-l1 的内容，此处只做衔接）：

[src/select.rs:196-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) — `if !is_biased { utils::shuffle(handles); }`。`is_biased` 正是宏透传进来的 `_IS_BIASED`。

> 结论：两个宏共享 99% 的实现，公平 vs 有偏只由一个布尔常量在编译期决定。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「两个宏生成代码的唯一差别是那个布尔常量」。

**操作步骤**：

1. 在 `crossbeam-channel` 目录下写一个临时示例 `examples/_tmp_bias.rs`（仅供观察，用完可删）：

```rust
use std::time::Duration;
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s, r) = unbounded::<i32>();
    let _ = s;
    select! {
        recv(r) -> _msg => {},
        default(Duration::from_millis(10)) => {},
    }
}
```

2. 安装展开工具：`cargo install cargo-expand`（需要 nightly：`rustup default nightly` 或 `rustup run nightly cargo expand`）。
3. 分别展开两种宏（手动把 `select!` 改成 `select_biased!` 再展开一次）：

```bash
cargo expand --example _tmp_bias
```

**需要观察的现象**：两次展开的代码**几乎完全一致**，只在最外层块里出现 `const _IS_BIASED: bool = false;`（`select!`）与 `... = true;`（`select_biased!`），并且都能在后续 `internal::select_timeout(..., _IS_BIASED)` 调用里看到这个名字。

**预期结果**：证实「公平性 = 编译期常量开关」，运行时无额外开销。若环境装不上 `cargo-expand`，可跳到 4.5 综合实践用「人工推导」方式对照。

> 注：临时示例文件不在本仓库版本控制里；观察完即可删除，不要提交。

#### 4.1.5 小练习与答案

**练习 1**：如果有人想新增一个「总是偏向最后一个分支」的 `select_last!` 宏，最小改动是什么？

**参考答案**：复制 `select!` 的定义，依然调用 `crossbeam_channel_internal!`——但仅靠 `_IS_BIASED` 不够，因为现有算法只支持「shuffle」或「不 shuffle（靠前优先）」两种。要支持「靠后优先」需要在 `run_select` 里增加新的排序逻辑（或对 handle 数组做一次 reverse）。这说明：入口宏只负责「喂常量 + 喂 token」，真正的选择策略在 `run_select`。

---

### 4.2 内部宏的两阶段架构与解析阶段 `@list` / `@case`

#### 4.2.1 概念说明

`crossbeam_channel_internal!` 是一个**带「内部标签」的递归宏**。它的每个 arm 都以一个 `@名字` 开头（如 `@list`、`@case`、`@init`），这些标签不是给用户写的——用户永远不会写出 `crossbeam_channel_internal!(@list ...)`——它们是宏**自己调用自己时**用来区分「现在进行到哪一步」的状态标记。

文件顶部的注释把整个流程划成两大阶段：

1. **解析阶段（Parsing）**：`@list` 把一串 token 切成一个个「case」，`@case` 逐个校验每个 case 的参数列表是否合法。中途任何语法错误都用 `compile_error!` 报出来。
2. **代码生成阶段（Codegen）**：`@init` 决定能否走快路径优化、并初始化 handle 数组；`@count` 数 case 个数；`@add` 把每个 recv/send 注册进数组并最终发起选择；`@complete` 根据 `index` 完成被选中的那个操作。

引入两个术语：

- **归一化（normalization）**：用户写的分支形态多样（带/不带 `-> 结果`、带/不带尾逗号、块体或表达式体），`@list` 把它们都改写成同一种内部形态 `case(args) -> res => { body },`，方便后续阶段处理。
- **`compile_error!` 诊断**：宏用一系列 `@list_errorN` arm 来「猜」用户写错了什么，给出尽可能友好的报错（例如「did you mean to put a comma…」）。

#### 4.2.2 核心流程

```
入口 arm（最外层）
   └─> @list : 逐个 case 切分 + 归一化（处理逗号/分号/箭头/缺省 default() 等）
         ├─ 语法错误？ ─> @list_error1..4 ─> compile_error!
         └─ 全部切完   ─> @case
                ├─ 校验 recv/send/default 参数 ─> 错则 compile_error!
                └─ 全部合法   ─> @init（进入代码生成）
```

每个 case 被归一化成内部统一形态：

```
recv($r:expr)        -> $res:pat => $body:tt,
send($s:expr,$m:expr)-> $res:pat => $body:tt,
default()            =>          $body:tt,
default($timeout)    =>          $body:tt,
```

#### 4.2.3 源码精读

顶部注释给出的两阶段划分：

[src/select_macro.rs:3-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L3-L21) — 文档注释，列出解析阶段（`@list` / `@list_errorN` / `@case`）与代码生成阶段（`@init` / `@count` / `@add` / `@complete`）。

内部宏本体与 `#[doc(hidden)]`：

[src/select_macro.rs:22-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L22-L24) — `#[doc(hidden)] #[macro_export] macro_rules! crossbeam_channel_internal`。`#[doc(hidden)]` 让它不出现在文档里，`#[macro_export]` 又让它能被 `$crate::crossbeam_channel_internal!` 跨路径调用。

`@list` 的几条代表性 arm：

[src/select_macro.rs:37-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L37-L47) — 把裸 `default => ...` 补上空参数列表变成 `default() => ...`，完成形态归一化。

[src/select_macro.rs:66-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L66-L74) — 检测到 `recv(...) =>`（漏写 `-> 结果`）时报 `compile_error!("expected '->' after 'recv' case, found '=>'")`。

[src/select_macro.rs:100-110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L100-L110) — 匹配到一个合法 case（带逗号分隔），把它**归一化**后追加到 `head` 列表，然后递归 `@list` 处理 `tail`。

`@case` 校验三类分支的参数列表（以 recv 为例）：

[src/select_macro.rs:373-385](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L373-L385) — 合法 recv 形态 `recv($r:expr $(,)?)`，把它原样收集进 `$cases`，继续 `@case` 处理下一条。

[src/select_macro.rs:386-399](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L386-L399) — recv 参数列表不合法时的 `compile_error!`（例如参数不是单个表达式）。

[src/select_macro.rs:483-492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L483-L492) — `default` 出现两次时报错「there can be only one `default` case」。

全部 case 校验完毕，进入代码生成阶段：

[src/select_macro.rs:360-371](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L360-L371) — `@case` 收到空待处理列表时，把收集到的 `$cases` 和 `$default` 交给 `@init`。

入口 arm（把用户 token 喂进 `@list`）：

[src/select_macro.rs:974-991](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L974-L991) — 三条入口 arm：空块报错（L974）、不带 `->` 结果的简单形态（L978，把 `body` 包成 `{ $body }` 并补逗号）、以及兜底的 catch-all（L985，把原始 token 整体交给 `@list`，让带 `-> res` 的分支也能被处理）。

#### 4.2.4 代码实践

**实践目标**：感受 `compile_error!` 诊断是如何在「展开期」而非「运行期」捕获错误的。

**操作步骤**：

1. 在一个临时 `.rs` 文件里**故意写错**语法，逐个观察编译器报错：

```rust
use crossbeam_channel::{select, unbounded};
fn main() {
    let (_s, r) = unbounded::<i32>();
    select! {
        recv(r) => println!("漏写 -> 结果"),   // 故意漏写 ->
    }
}
```

2. `cargo build`，阅读报错信息——它应当来自宏里的 `compile_error!("expected '->' after 'recv' case, found '=>'")`。
3. 再分别试：把 `recv(r)` 写成 `recv r`（漏括号）、在 `}` 后加分号、写两个 `default`。逐一对照 [src/select_macro.rs:143-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L143-L358) 里的 `@list_error1..4`，确认报错文案能对上。

**需要观察的现象**：每种写错方式都给出一条**有针对性的、人话的**错误提示，而不是「macro expansion failed」。

**预期结果**：理解宏为何要写这么多 arm——绝大多数 arm 都是为「在编译期给出友好错误」服务的。

#### 4.2.5 小练习与答案

**练习 1**：`@list` 为什么要先把 `default => ...` 改写成 `default() => ...`？

**参考答案**：为了让后续阶段（`@case` 与 `@init`）能用**同一套**模式匹配处理「无参 default」和「有参 default(timeout)」——统一带上空参数列表 `default()` 后，`@case` 只要匹配 `default($($args:tt)*)` 一种形态即可。

**练习 2**：用户写 `recv(r) -> msg => body`，这条分支会从哪条入口 arm 进入？

**参考答案**：因为带 `-> msg`，它不匹配 L978 那条「`=> $body:expr`」的简单 arm（`->` 卡在中间），会落到 L985 的 catch-all `($($tokens:tt)*)`，把整串 token 原样交给 `@list`，由 `@list` 负责识别 `-> $res:pat`。

---

### 4.3 单分支优化：`@init` 的三条快路径

#### 4.3.1 概念说明

`run_select` 是一套相当重的调度算法（注册、park、唤醒、accept，见 u3-l1）。如果一个 `select!` 块**只有一个 recv 分支**，那它根本不需要 select 调度——直接调单条收发 API 即可，开销低得多。

`@init` 阶段就是干这件事的：它**先尝试把 `select!` 退化成等价的单条 API 调用**。退化规则对应「单个 recv 分支 + 可选 default」的三种组合：

| select! 形态 | 退化成 | 等价的单条 API |
| --- | --- | --- |
| 仅 `recv(r) -> msg => body` | 阻塞接收 | `r.recv()` |
| `recv(r)` + `default => body` | 非阻塞接收 | `r.try_recv()` |
| `recv(r)` + `default(timeout) => body` | 限时接收 | `r.recv_timeout(timeout)` |

**重要限制**：这三条优化 arm **只匹配 recv**。源码里其实也写了 send 的优化 arm，但全部被注释掉了（标注 `TODO(stjepang): Implement this optimization.`），所以带 `send` 的单分支 select **不会**走快路径，而是走通用代码生成。这是一个容易踩坑的细节。

#### 4.3.2 核心流程

```
@init 收到 (cases, default)
   ├─ cases 恰好是单个 recv(...)？
   │     ├─ default 空        ─> 生成 r.recv()        （阻塞）
   │     ├─ default()         ─> 生成 r.try_recv()    （非阻塞）
   │     └─ default(timeout)  ─> 生成 r.recv_timeout()（限时）
   └─ 其他（多个分支 / 含 send / 形态不符）─> 走通用 @init（见 4.4）
```

退化后生成的代码大致长这样（以 try_recv 退化为例）：

```rust
// select! { recv(r) -> msg => body, default => fallback }
// 退化成：
match _r.try_recv() {
    Err(TryRecvError::Empty) => fallback,      // default 分支
    _res => {
        let _res = _res.map_err(|_| RecvError); // 把 TryRecvError 归一化成 RecvError
        let msg = _res;
        body
    }
}
```

注意错误类型的归一化：`select!` 的 recv 分支约定绑定 `Result<T, RecvError>`，而 `try_recv` 返回的是 `Result<T, TryRecvError>`，所以要用 `.map_err(|_| RecvError)` 把 `Empty`/`Disconnected` 都压成 `RecvError`（与 u2-l3 错误体系一致）。

#### 4.3.3 源码精读

退化为 `try_recv()`：

[src/select_macro.rs:537-557](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L537-L557) — 匹配「单个 recv + `default()`」，生成 `_r.try_recv()`，`Empty` 走 default body，其余 `.map_err(|_| RecvError)` 后绑定到 `$res` 执行 recv body。

退化为 `recv()`：

[src/select_macro.rs:558-571](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L558-L571) — 匹配「单个 recv + 无 default」，直接生成 `_r.recv()`（阻塞），结果绑定到 `$res` 后执行 body。

退化为 `recv_timeout()`：

[src/select_macro.rs:572-592](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L572-L592) — 匹配「单个 recv + `default($timeout)`」，生成 `_r.recv_timeout($timeout)`，`Timeout` 走 default body，其余归一化为 `RecvError` 后执行 recv body。

被注释掉的 send 优化（说明 send 暂不支持快路径）：

[src/select_macro.rs:655-690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L655-L690) — 三条 send 优化 arm 全部被注释、只留 `TODO`，证实「单 send 分支目前不走快路径」。

#### 4.3.4 代码实践

**实践目标**：验证「单 recv 分支会退化、单 send 分支不会」。

**操作步骤**：

1. 写两个对照示例。

```rust
use std::time::Duration;
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s, r) = unbounded::<i32>();
    // 例 A：单 recv + default(timeout) —— 应退化为 recv_timeout
    select! {
        recv(r) -> _msg => {},
        default(Duration::from_millis(10)) => {},
    }
    // 例 B：单 send + default —— 不退化，走通用路径
    select! {
        send(s, 1) -> _res => {},
        default => {},
    }
}
```

2. `cargo expand`（或人工对照 4.3.3 的源码）查看展开结果。

**需要观察的现象**：

- 例 A 的展开里**没有** `internal::select_timeout`、没有 `_sel` 数组，而是直接出现 `r.recv_timeout(Duration::from_millis(10))`。
- 例 B 的展开里**有** `_sel` 数组、`internal::try_select(...)` 和 `@complete` 分发——即走了 4.4 的通用路径。

**预期结果**：单 recv 退化、单 send 不退化。若无法运行 `cargo expand`，标注「待本地验证」，但可根据 4.3.3 的 arm 匹配规则**推断**例 A 命中 L572 的 recv_timeout 优化、例 B 因 send 优化被注释而落入通用 `@init`。

#### 4.3.5 小练习与答案

**练习 1**：`select! { recv(r) -> msg => body }`（单个 recv、无 default）退化后，`msg` 的类型是什么？为什么不会有 `Timeout` 这种可能？

**参考答案**：退化成 `r.recv()`，返回 `Result<T, RecvError>`，故 `msg: Result<T, RecvError>`。因为走的是阻塞 `recv()`，它要么收到消息要么返回 `Disconnected`（`RecvError`），没有「超时」概念，所以不需要、也不会出现 `Timeout` 变体。

**练习 2**：为什么 send 的单分支优化被注释掉了仍然能正确工作？

**参考答案**：因为它只是「没走快路径」，并不是「不支持」。任何带 send 的 select 都会落入通用 `@init`，经由 `internal::select*` + `SelectedOperation::send` 完成等价语义，只是开销略高。

---

### 4.4 通用代码生成：`@init` / `@count` / `@add` / `@complete`

#### 4.4.1 概念说明

当 `@init` 的三条快路径都不命中时（多分支、含 send 等），宏走**通用代码生成**：在栈上构造一个 handle 数组，把每个 recv/send 注册进去，调用 `internal::select*` 选出一个操作，再根据它的 `index` 分发到对应分支完成它。

这套生成的代码，本质上是把 `Select` 动态 API（u2-l10）的用法「编译期写死」：

- `Select::new()` + 多次 `sel.recv/sel.send`  ──对应──>  宏生成的 `_sel` 数组 + `@add` 逐个填充。
- `sel.select()` / `try_select()` / `select_timeout()`  ──对应──>  `internal::select` / `try_select` / `select_timeout`。
- `op.index()` + `op.recv/op.send`  ──对应──>  `@complete` 的 `if $oper.index() == $i` 分发与完成调用。

三个生成阶段的职责：

- **`@init`（通用分支）**：用 `@count` 算出分支数 `_LEN`，声明固定大小的 `_sel` 数组，预置一份「标签池」`((0usize _oper0) (1usize _oper1) ...)`，交给 `@add`。
- **`@add`**：每消费一条 case，从标签池取出一个 `(下标, 变量名)`，把对应 `&Receiver`/`&Sender` 连同 `index`、`addr` 写入 `_sel[$i]`，并把「带下标的 case」累积进一个新列表；case 全部消费完后，按 `default` 形态选择 `internal::select` / `try_select` / `select_timeout`。
- **`@complete`**：拿到 `SelectedOperation` 后，用 `if $oper.index() == $i` 逐个比对，命中则调 `$oper.recv($r)` / `$oper.send($s, $m)` 完成操作并执行用户 body。

两个工程要点：

- **标签池上限 32**：源码里标签只列到 `_oper31`，因此一个 `select!` 块**最多 32 个操作**；超出会 `compile_error!("too many operations in a 'select!' block")`。
- **生命周期擦除（lifetime erasure）**：生成的代码用 `unsafe { mem::transmute(...) }` 和一个内部 `unbind` 函数把引用的生命周期擦掉，目的是「让 `_sel` 能在没有 NLL（non-lexical lifetimes）的老编译器上也能提前 drop」。这是宏特意为兼容性留的转换。

#### 4.4.2 核心流程

通用 `@init` 生成的骨架（伪代码）：

```
{
    const _LEN: usize = <@count 算出的分支数>;
    let _handle: &dyn SelectHandle = &never::<()>();      // 占位 handle
    let mut _sel = [(_handle, 0, 0); _LEN];               // 定长数组，先用占位填满

    // @add 逐个填充：
    //   分支0 (recv r): _sel[0] = (&r, 0, receiver_addr(&r));
    //   分支1 (send s,v): _sel[1] = (&s, 1, sender_addr(&s));
    // 全部填完后按 default 形态发起选择：
    let _oper = internal::select(&mut _sel, _IS_BIASED);  // 或 try_select / select_timeout

    // @complete 按 index 分发：
    if _oper.index() == 0 { let res = _oper.recv(&r);  { _sel }; <recv body> }
    else if _oper.index() == 1 { let res = _oper.send(&s, v); { _sel }; <send body> }
    else { unreachable!(...) }
}
```

`@complete` 用 `index` 做线性分发（if-else 链），而不是 `match`，是因为 `index` 是运行时的 `usize`，而每个分支的 `$i` 是编译期常量——一条条 `if $oper.index() == $i` 恰好能把它们串起来。

#### 4.4.3 源码精读

通用 `@init`：算 `_LEN`、建占位数组、预置标签池：

[src/select_macro.rs:692-744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L692-L744) — 关键三处：`const _LEN = @count(...)`（L697）；`let _handle: &dyn SelectHandle = &never::<()>()`（L698，用零大小、永不就绪的 `never()` 当占位，仅用于把数组类型钉死为 `&dyn SelectHandle`）；`let mut _sel = [(_handle, 0, 0); _LEN]`（L701）；以及标签池 `((0usize _oper0) ... (31usize _oper31))`（L708-L741）。

`@count` 递归数分支：

[src/select_macro.rs:746-752](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L746-L752) — 每条 case 贡献 `1 +`，空列表为 `0`，递归求和。

`@add` 的三条**终止**分支（case 已全部注册，发起选择）：

[src/select_macro.rs:754-775](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L754-L775) — 无 default：调 `internal::select`，返回 `SelectedOperation`（直接进 `@complete`）。

[src/select_macro.rs:776-805](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L776-L805) — `default()`：调 `internal::try_select`，返回 `Option<SelectedOperation>`，`None` 时执行 default body。

[src/select_macro.rs:806-835](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L806-L835) — `default($timeout)`：调 `internal::select_timeout`，返回 `Option<SelectedOperation>`，`None` 时执行 default body。

标签耗尽（超过 32 个操作）：

[src/select_macro.rs:836-845](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L836-L845) — `@add` 还有剩余 case 但标签池 `()` 已空，报 `compile_error!("too many operations in a 'select!' block")`。

`@add` 消费一条 recv / send（注册进数组）：

[src/select_macro.rs:846-877](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L846-L877) — recv：取出标签 `(0usize _oper0)`，用 `unbind` 擦除生命周期得到 `$var: &Receiver<_>`，写入 `_sel[$i] = ($var, $i, internal::receiver_addr($var))`，并把带下标的 case `[$i] recv($var) -> $res => $body,` 累积进列表，递归 `@add`。

[src/select_macro.rs:878-909](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L878-L909) — send：同结构，写入 `_sel[$i] = ($var, $i, internal::sender_addr($var))`，累积 `[$i] send($var, $m) -> $res => $body,`。

`@complete` 按 index 分发并完成：

[src/select_macro.rs:911-931](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L911-L931) — recv：`if $oper.index() == $i` 命中则 `$oper.recv($r)` 完成、`{ $sel }` 显式 drop 数组、绑定结果到 `$res` 执行 body；否则递归 `@complete` 比下一个。

[src/select_macro.rs:932-952](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L932-L952) — send：命中则 `$oper.send($s, $m)` 完成。

[src/select_macro.rs:953-962](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L953-L962) — 兜底：所有 index 都没命中（理论上不可能），`unreachable!("internal error ...")`。

宏展开后调用的目标函数（`internal` 模块经 `lib.rs` 重新导出）：

[src/select.rs:473-489](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L473-L489) — `pub fn select(...)`：阻塞式，内部 `run_select(handles, Timeout::Never, is_biased)`。

[src/select.rs:455-469](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L455-L469) — `pub fn try_select(...)`：非阻塞式，`Timeout::Now`。

[src/select.rs:493-503](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L493-L503) — `pub fn select_timeout(...)`：限时式，`Duration` 溢出时退化为阻塞 `select`。

`addr` 辅助函数（完成操作时做端身份校验）：

[src/select.rs:523-530](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L523-L530) — `sender_addr` / `receiver_addr`，分别转发 `Sender::addr` / `Receiver::addr`，把端指针地址存进 handle 三元组，供 `SelectedOperation::send/recv` 校验「传进来的端就是被选中的那个」。

`internal` 模块的导出：

[src/lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375) — `#[doc(hidden)] pub mod internal`，重新导出 `SelectHandle` / `select` / `try_select` / `select_timeout` / `sender_addr` / `receiver_addr` 给宏用。

完成操作最终调到的 `channel::write` / `channel::read`（在 `SelectedOperation::send` / `recv` 内部，u3-l2 已讲）：

[src/channel.rs:1539-1560](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1539-L1560) — `unsafe fn write` 与 `unsafe fn read`，用抢占阶段拿到的 `Token` 真正搬运消息。

#### 4.4.4 代码实践

**实践目标**：手工推导一个含 `recv` + `send` + `default(timeout)` 的 `select!` 展开结果，并把每一段对应到 4.4.3 的源码。

**操作步骤**：

1. 阅读下面这段「示例代码」（用户视角）：

```rust
// 示例代码：用户写的 select!
select! {
    recv(r) -> msg => { println!("recv: {:?}", msg); },
    send(s, 20) -> res => { println!("send: {:?}", res); },
    default(Duration::from_millis(100)) => { println!("timed out"); },
}
```

2. 因为含 `send`，**不会**命中单分支优化，走通用路径。下面是它展开后的**示意代码**（标注为「示例代码」，是对 `cargo expand` 输出的精简转述，变量名与宏源码一致）：

```rust
// 示例代码：展开结果（精简转述）
{
    const _IS_BIASED: bool = false;                       // 来自 select! 入口
    {
        const _LEN: usize = 2;                            // @count：recv + send = 2
        let _handle: &dyn ::crossbeam_channel::internal::SelectHandle =
            &::crossbeam_channel::never::<()>();
        let mut _sel = [(_handle, 0, 0); 2];              // 占位数组

        // —— @add：注册 recv —— 标签 (0usize _oper0)
        let _oper0: &::crossbeam_channel::Receiver<_> = /* &r，擦除生命周期 */;
        _sel[0] = (_oper0, 0, ::crossbeam_channel::internal::receiver_addr(_oper0));

        // —— @add：注册 send —— 标签 (1usize _oper1)
        let _oper1: &::crossbeam_channel::Sender<_> = /* &s，擦除生命周期 */;
        _sel[1] = (_oper1, 1, ::crossbeam_channel::internal::sender_addr(_oper1));

        // —— @add 终止（default(timeout)）——> select_timeout
        let _oper: ::std::option::Option<::crossbeam_channel::SelectedOperation<'_>> = {
            let _oper = ::crossbeam_channel::internal::select_timeout(
                &mut _sel, Duration::from_millis(100), _IS_BIASED);
            unsafe { ::std::mem::transmute(_oper) }       // 擦除生命周期
        };

        match _oper {
            None => { { _sel }; println!("timed out"); }, // default 分支
            Some(_oper) => {
                // —— @complete：按 index 分发
                if _oper.index() == 0 {
                    let _res = _oper.recv(_oper0);         // 完成接收
                    { _sel };
                    let msg = _res;
                    { println!("recv: {:?}", msg); }
                } else if _oper.index() == 1 {
                    let _res = _oper.send(_oper1, 20);     // 完成发送
                    { _sel };
                    let res = _res;
                    { println!("send: {:?}", res); }
                } else {
                    unreachable!("internal error in crossbeam-channel: invalid case");
                }
            }
        }
    }
}
```

3. 在展开示意里**圈出三个关键结构**并标注对应源码行：
   - **handle 数组** `_sel`：对应 [src/select_macro.rs:701](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L701)。
   - **index 分发** `if _oper.index() == 0 / 1`：对应 [src/select_macro.rs:917](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L917) 与 [src/select_macro.rs:938](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L938)。
   - **完成调用** `_oper.recv(_oper0)` / `_oper.send(_oper1, 20)`：对应 [src/select_macro.rs:918](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L918) 与 [src/select_macro.rs:939](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L939)。

**需要观察的现象**：展开代码里出现了 `_sel` 数组、`internal::select_timeout`、`transmute` 擦除生命周期、`match _oper { None / Some }`、以及 `if _oper.index() == $i` 链。

**预期结果**：你能把展开代码里的每一块都指回 `select_macro.rs` 的某条 arm，以及 `select.rs` 的某个函数。若用 `cargo expand` 实跑，输出会更啰嗦（含更多 `::crossbeam_channel::` 全路径），但结构与此一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `@complete` 用 `if $oper.index() == $i` 的 if-else 链，而不是 `match $oper.index() { 0 => ..., 1 => ... }`？

**参考答案**：两者语义等价，但宏生成 if-else 链更自然——每条 `@complete` arm 只「负责一个 index」，命中就执行 body、不命中就**递归展开下一条 `@complete`**（见 L924-L929 的 `$crate::crossbeam_channel_internal! { @complete ... ($($tail)*) }`）。这种「逐条递归」的写法天然形成 if-else 链，比一次性拼出一个 `match` 更简单。

**练习 2**：占位 handle 用的是 `never::<()>()`。如果把它换成 `unbounded::<()>()` 的接收端，程序还能正确工作吗？为什么？

**参考答案**：功能上仍能工作（占位 handle 只用于撑起数组类型，随后会被 `@add` 全部覆盖），但 `never()` 是零大小、永不就绪的「占位」通道，构造代价几乎为零；换成 `unbounded()` 会无谓分配堆内存，且 `never()` 在语义上更准确地表达了「这个槽位目前不代表任何真实操作」。

**练习 3**：把上面的示例再加 32 个分支会怎样？

**参考答案**：标签池只列到 `_oper31`（共 32 个），第 33 个分支会让 `@add` 在标签池已空 `()` 时命中 [src/select_macro.rs:836-845](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L836-L845) 的 arm，报 `compile_error!("too many operations in a 'select!' block")`。分支多到这种程度本就建议改用 `Select` 动态 API（u2-l10）。

---

## 5. 综合实践

把本讲三件事（入口差异、单分支优化、通用代码生成）串起来做一个「展开对照」小任务。

**任务**：写一个程序，里面有三段功能等价的代码，分别用 `select!`、`select_biased!`、`Select` 动态 API 实现「从 `r` 接收，带 100ms 超时」。

```rust
use std::time::Duration;
use crossbeam_channel::{select, select_biased, Select, unbounded};

fn main() {
    let (_s, r) = unbounded::<i32>();

    // (1) select! —— 单 recv 分支，会退化为 recv_timeout
    select! { recv(r) -> _msg => {}, default(Duration::from_millis(100)) => {} }

    // (2) select_biased! —— 同上，但 _IS_BIASED = true
    select_biased! { recv(r) -> _msg => {}, default(Duration::from_millis(100)) => {} }

    // (3) Select 动态 API —— 手写等价逻辑
    let mut sel = Select::new();
    sel.recv(&r);
    match sel.select_timeout(Duration::from_millis(100)) {
        Ok(op) => { let _ = op.recv(&r); },
        Err(_) => {},
    }
}
```

**要做的分析**：

1. 用 `cargo expand` 展开 (1) 和 (2)，确认它们唯一的差别是 `const _IS_BIASED: bool` 的取值，且都退化成 `r.recv_timeout(...)`（命中 [src/select_macro.rs:572-592](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L572-L592) 的快路径）。
2. 把 (1) 改成「recv + send」两分支（参照 4.4.4 的示例），再次展开，确认这次**没有**退化，而是出现 `_sel` 数组与 `internal::select_timeout` 调用。
3. 对照 (3)：手写的 `Select` 用法与 (1)/(2) 展开后的结构应当**几乎同构**——`sel.recv(&r)` 对应 `_sel[0] = (...)`，`sel.select_timeout(...)` 对应 `internal::select_timeout(...)`，`op.recv(&r)` 对应 `@complete` 里的 `$oper.recv($r)`。

**预期收获**：你会直观地看到「`select!` 宏 = 编译期把 `Select` 动态 API 的样板代码写死」，并理解单分支优化是宏额外做的「编译期化简」。若本机没有 nightly / `cargo-expand`，可改为「人工推导」并标注「待本地验证」。

---

## 6. 本讲小结

- `select!` 与 `select_biased!` **共享同一套内部宏** `crossbeam_channel_internal!`，唯一差别是入口植入的 `const _IS_BIASED: bool`，它最终在 `run_select` 里决定是否 `shuffle`。
- `crossbeam_channel_internal!` 是**带 `@标签` 的递归宏**，分两大阶段：解析（`@list`/`@case`/`@list_errorN`）与代码生成（`@init`/`@count`/`@add`/`@complete`）。
- 解析阶段把形态各异的分支**归一化**为 `case(args) -> res => { body },`，并用大量 arm 在编译期给出**友好的 `compile_error!`**。
- **单分支优化**只有「单个 recv + 可选 default」会命中，退化成 `recv()`/`try_recv()`/`recv_timeout()`；send 的优化 arm 被注释掉，所以带 send 的分支走通用路径。
- 通用代码生成在栈上建 `_sel` handle 数组（上限 32），`@add` 逐个注册并按 default 形态调 `internal::select`/`try_select`/`select_timeout`，`@complete` 用 `if $oper.index() == $i` 分发并完成操作。
- 生成的代码与 `Select` 动态 API **几乎同构**；`transmute`/`unbind` 用于擦除引用生命周期，让 `_sel` 能提前 drop。

---

## 7. 下一步学习建议

- **往下读正确性**：本讲出现的 `transmute`、`unbind`、`SelectedOperation::send/recv` 里的 `unsafe { channel::write/read }` 都涉及 unsafe 边界，建议接着读 **u3-l4 内存序与 unsafe 的正确性**，搞清 `Token` 在抢占与搬运之间传递时的不变量。
- **往回印证 flavor 对接**：`@complete` 调的 `$oper.recv/recv` 最终走到各 flavor 的 `read`/`write`，可结合 **u3-l2 SelectHandle trait 与 flavor 对接** 看清「抢占—Token—搬运」三段式在每种 flavor 里如何落地。
- **看真实用例**：`examples/fibonacci.rs`、`examples/matching.rs`、`examples/stopwatch.rs`（u3-l9 会集中讲）展示了 `select!` 在流水线、会合、定时场景里的实战写法，可以把它们的展开与本章的推导互相印证。
- **想扩展宏**：若你想加新的分支类型或新的选择策略，入手点是 `@case`（参数校验）+ `@add`（注册）+ `run_select`（策略），并注意保持 `internal` 模块导出与宏调用同步。
