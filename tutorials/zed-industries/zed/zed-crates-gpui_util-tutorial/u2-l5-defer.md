# defer 与 Deferred：基于 Drop 的 RAII 延迟执行

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 RAII（资源获取即初始化）模式：把「清理动作」绑定到「值的生命周期」，让退出作用域这件事自动触发清理。
2. 精读 `Deferred<F>` 结构体：为什么内部是 `Option<F>`，`abort()` 如何靠「取走闭包」让 `drop` 不再执行。
3. 精读 `Drop` 实现：为什么必须用 `Option::take()` 而不能直接调用闭包（答案是 `FnOnce` 会消费闭包）。
4. 精读 `defer` 构造函数：`#[must_use]` 如何在编译期拦住 `defer(f);` 这种「闭包立刻执行」的误用。
5. 能在 zed 仓库中识别 `let _guard = util::defer(...)` 这种真实用法，并解释 `_guard`（下划线前缀）与 `let _ =`（裸下划线）的天壤之别。

## 2. 前置知识

### 2.1 RAII：用生命周期管理资源

RAII（Resource Acquisition Is Initialization，资源获取即初始化）是 Rust 最核心的资源管理思想：**资源在构造时获取、在 drop 时释放**。你其实每天都在用它：

- `std::fs::File`：drop 时自动关闭文件。
- `std::sync::MutexGuard`：drop 时自动解锁。
- `Vec`：drop 时自动释放堆内存。

RAII 的好处是**确定性**：drop 发生在值离开作用域的那一刻，不依赖垃圾回收器什么时候想起来回收。而且无论你是正常走到作用域结尾、用 `?` 提前返回、还是 panic 展开栈，`Drop` 都会被执行——这一点对「清理动作」至关重要，也是 `defer` 存在的意义。

### 2.2 Drop trait

```rust
// 示例代码：Drop trait 的签名
trait Drop {
    fn drop(&mut self);
}
```

当值离开作用域时，Rust 自动调用 `drop`。注意签名只给 `&mut self`——这决定了后面 `Deferred` 必须用 `Option` 包装闭包（见 4.2）。

### 2.3 FnOnce 与闭包的所有权

`FnOnce` 是「只能调用一次」的闭包 trait：调用它会**消费闭包本身**（拿走它捕获的所有变量）。所以通过 `&mut self` 调用一个 `FnOnce` 闭包是不行的——必须先把它按值取出来。

### 2.4 Option::take

```rust
// 示例代码：Option::take 的签名
impl<T> Option<T> {
    fn take(&mut self) -> Option<T>; // 取走值，原位置留下 None
}
```

这是 Rust 里「从 `&mut` 中按值取出东西」的标准手法，本讲的主角正是靠它工作的。

### 2.5 与 Go 的 defer 对比

| | Go 的 `defer` | gpui_util 的 `defer` |
|---|---|---|
| 触发时机 | 函数返回时 | 守卫值被 drop 时 |
| 能否提前取消 | 不能 | 能（`abort()`） |
| 多个 defer 的顺序 | 后注册先执行（LIFO） | 后构造先 drop（LIFO，Rust 的 drop 顺序） |
| 典型用途 | 函数级收尾 | 任意作用域级清理，包括循环体、闭包内 |

## 3. 本讲源码地图

本讲只涉及一个源码文件中的一个区段（u1-l2 源码地图中的第 7 区段）：

| 文件 | 行号 | 作用 |
|---|---|---|
| `crates/gpui_util/src/lib.rs` | L505–L526 | `Deferred` 结构体、`abort`、`Drop` 实现与 `defer` 函数 |

另有两个真实使用点，用于观察它如何被下游 crate 使用：

| 文件 | 行号 | 用法 |
|---|---|---|
| `crates/lsp/src/lsp.rs` | L660–L665 | 作用域结束時清理响应处理器（守卫模式） |
| `crates/git/src/repository.rs` | L3688–L3698, L3715 | 兜底删除临时文件，成功路径上 `abort()` 取消（取消模式） |

## 4. 核心概念与源码讲解

### 4.1 Deferred 结构体与 abort 方法

#### 4.1.1 概念说明

`Deferred<F>` 是一个「延时炸弹」：构造时把一个闭包塞进去，之后谁也不碰它；当它被 drop 时，闭包被引爆（执行）。它解决的问题很实际——

```rust
// 示例代码：没有 defer 时的痛点
fn work() -> Result<()> {
    let resource = acquire()?;        // 获取资源
    let result = do_something(resource)?; // ← 这里 ? 提前返回，忘了清理！
    release(resource);
    Ok(result)
}
```

手动清理在有 `?` 提前返回的函数里极容易漏。把清理动作挂到 `Drop` 上之后，**所有退出路径（正常返回、`?`、panic 展开）都会统一触发清理**，一行都不会漏。

`abort()` 则是「剪断引线」：销毁守卫但不执行闭包。典型场景是「我先安排了兜底清理，后来发现正常路径已经清理过了，不需要再来一次」——4.1.3 的 git 真实用法正是如此。

#### 4.1.2 核心流程

`Deferred` 的全部状态只有两种，生命周期如下：

```
构造 (defer 或直接构造)
        │
        ▼
 ┌──────────────┐   drop()    ┌──────────────┐
 │ Alive        │ ──────────► │ 引爆：执行 f  │ → 值消失
 │ Some(f)      │             └──────────────┘
 └──────────────┘
        │
        │ abort()
        ▼
 ┌──────────────┐   drop()    ┌─────────────┐
 │ Empty        │ ──────────► │ 什么都不做    │ → 值消失
 │ None         │             └─────────────┘
 └──────────────┘
```

关键点：

1. `abort(mut self)` **按值消费 self**——调用之后这个守卫就没了，不可能再误触发，类型系统保证了「取消」是一次性的、不可逆的。
2. `abort` 内部只是 `self.0.take()`：把闭包取走扔掉（准确说取走后没有调用就被丢弃了），self 随后带着 `None` 被 drop，`Drop::drop` 看到 `None` 就直接跳过。
3. 一次性的语义由 `Option` 表达：`Some(f)` 表示「还没引爆」，`None` 表示「已引爆或已拆除」。

#### 4.1.3 源码精读

结构体定义与 `abort` 方法：

[crates/gpui_util/src/lib.rs:505-L512](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L505-L512) —— `Deferred` 被定义为元组结构体，唯一的字段是私有的 `Option<F>`（外部代码无法绕过 `defer` 函数直接构造它）；`abort` 按值拿走 `self` 并用 `take()` 清空闭包。

```rust
pub struct Deferred<F: FnOnce()>(Option<F>);

impl<F: FnOnce()> Deferred<F> {
    /// Drop without running the deferred function.
    pub fn abort(mut self) {
        self.0.take();
    }
}
```

三个值得逐字品味的细节：

- `pub struct Deferred<F: FnOnce()>(Option<F>)`：字段 `0` 没有 `pub`，所以外部 crate **只能**通过 `defer()` 构造——保证了「守卫必须来自 `defer`」这个不变量。
- `pub fn abort(mut self)`：参数是 `mut self`（按值），因为 `take()` 需要 `&mut self.0`；按值消费意味着「取消守卫」这个动作在类型层面就是终态。
- `self.0.take();`：这句是分号表达式，取出的 `Option<F>` 没有被绑定，直接被丢弃——闭包在这条语句里被销毁，从未调用。

再看一个真实的取消场景。[crates/git/src/repository.rs:3688-L3698](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/git/src/repository.rs#L3688-L3698)：git crate 生成一个临时的 excludes 文件，用 `defer` 安排「函数退出时删除它」作为兜底：

```rust
let delete_excludes_file = util::defer({
    let excludes_file_path = excludes_file_path.clone();
    let executor = git.executor.clone();
    move || {
        executor
            .spawn(async move {
                smol::fs::remove_file(excludes_file_path).await.log_err();
            })
            .detach();
    }
});
```

而 [crates/git/src/repository.rs:3714-L3715](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/git/src/repository.rs#L3714-L3715) 是成功路径：文件已经手动删掉了，于是调用 `abort()` 拆除兜底守卫，避免重复删除：

```rust
smol::fs::remove_file(&excludes_file_path).await.ok();
delete_excludes_file.abort();
```

如果这里没有 `abort()`，闭包会在函数返回时再删一次已经不存在的文件——虽然 `remove_file` 的错误被 `.log_err()` 记录而不是 panic（u2-l1 讲过的模式），但会产生一条无意义的错误日志。`abort` 让兜底逻辑可以被精确撤销。

#### 4.1.4 代码实践

**实践目标**：验证 `abort()` 之后闭包不再执行，且 `abort` 消费守卫（不能二次使用）。

**操作步骤**（源码阅读 + 本地小实验）：

1. 创建一个独立的练习 crate（不修改 zed 源码）：

   ```bash
   cargo new defer_lab --lib
   cd defer_lab
   ```

2. 在 `defer_lab/Cargo.toml` 中用路径依赖指向本地 zed 检出（gpui_util 只依赖 `log` 和 `anyhow`，很轻）：

   ```toml
   [dependencies]
   gpui_util = { path = "../../zed/crates/gpui_util" }  # 按你的实际相对路径调整
   ```

3. 在 `src/lib.rs` 写一个测试（示例代码）：

   ```rust
   use std::rc::Rc;
   use std::cell::Cell;
   use gpui_util::defer;

   #[test]
   fn abort_prevents_deferred_call() {
       let fired = Rc::new(Cell::new(false));
       let fired_in_closure = fired.clone();

       let guard = defer(move || fired_in_closure.set(true));
       assert_eq!(fired.get(), false); // 还没 drop，闭包未执行
       guard.abort();                  // 取消
       assert_eq!(fired.get(), false); // drop 后仍未执行
   }

   #[test]
   fn drop_runs_deferred_call() {
       let fired = Rc::new(Cell::new(false));
       let fired_in_closure = fired.clone();
       {
           let _guard = defer(move || fired_in_closure.set(true));
           assert_eq!(fired.get(), false); // 守卫存活期间不执行
       } // _guard 在这里离开作用域被 drop
       assert_eq!(fired.get(), true);      // drop 触发了闭包
   }
   ```

4. 运行 `cargo test`。

**需要观察的现象**：两个测试都通过——`abort` 路径上标志位始终为 `false`，普通 drop 路径上作用域结束后标志位变为 `true`。

**预期结果**：`test result: ok. 2 passed`。另外你可以试着在 `guard.abort();` 之后再写一行 `guard.abort();`，编译器会报 `use of moved value: guard`——这就是「abort 按值消费」的类型保证。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `abort` 的签名是 `pub fn abort(mut self)` 而不是 `pub fn abort(&mut self)`？

**答案**：如果接受 `&mut self`，调用后守卫仍然存活，后续 drop 时还得再决定一次是否执行闭包，调用方很容易忘记它已被取消，语义上也可能出现「取消后又想恢复」的混乱状态。按值消费让「取消」成为终态：守卫在 `abort` 返回时就被 drop 了，类型系统直接禁止后续任何使用（包括二次 `abort`）。`mut` 只是因为函数体里要调用需要 `&mut self.0` 的 `take()`。

**练习 2**：`Deferred` 的字段为什么是私有的？如果字段是 `pub`，会破坏什么保证？

**答案**：字段私有使外部代码只能通过 `defer()` 函数构造 `Deferred`，构造即「装入一个闭包」，不存在「空守卫」或「半初始化守卫」。若字段是 `pub`，外部可以构造 `Deferred(None)` 绕过 `defer`，也可以拿到 `Deferred(Some(f))` 后直接读出闭包另行调用，守卫「drop 时恰好执行一次」的语义就被打破了。（严格地说，同 crate 内的代码仍可访问私有字段，但 zed 的约定是都走 `defer`。）

**练习 3**：在 4.1.3 的 git 例子中，如果把 L3715 的 `delete_excludes_file.abort();` 删掉，会发生什么？

**答案**：函数正常返回时守卫被 drop，闭包再次尝试删除 `excludes_file_path`。由于文件在 L3714 已被删除，第二次 `remove_file` 会返回 `Err`，被 `.log_err()` 记录成一条错误日志（不会 panic），产生噪音。功能上无害，但 `abort()` 的存在让「成功路径手动清理、失败路径兜底清理」的意图表达得一清二楚。

### 4.2 Drop 实现：Option::take 模式

#### 4.2.1 概念说明

`Drop` 实现是整个机制的引擎：值被丢弃时检查内部是否还有闭包，有就执行。这里最有教学价值的问题是——**为什么不能直接写 `self.0()`？**

因为 `F: FnOnce` 的调用语法 `(f)()` 会消费 `f`（按值拿走闭包及其捕获的变量），而 `Drop::drop` 只有 `&mut self`。从 `&mut Option<F>` 里按值取出 `F` 的唯一安全办法就是 `take()`：把值搬出来，原位置留下 `None`。这个「用 `Option` 包装以便在 `drop` 中移动出来」的手法在 Rust 里俯拾皆是，通常被称作 take 模式或「drop 引信」。

#### 4.2.2 核心流程

`Drop::drop` 的执行步骤：

1. `self.0.take()`：把 `Option<F>` 按值取出（原位置变 `None`）。
2. `if let Some(f) = ...`：判断是否有闭包。
   - `Some(f)`：调用 `f()`——此时 `f` 已经是拥有的值，`FnOnce` 的调用合法。
   - `None`：说明闭包已被 `abort` 取走过（或已被构造为空），什么都不做。

配合 4.1 的状态图：`Alive → drop → 引爆`，`Empty → drop → 静默`。`take()` 同时承担了「取出」和「清空标记」两个职责——即使 `f()` 内部 panic 导致 drop 过程重入，`self.0` 也已经是 `None`，不会二次执行闭包。

另外注意 Rust 的 drop 顺序是**逆序的（LIFO）**：同一作用域内后构造的守卫先 drop。多个 `defer` 嵌套时，后注册的清理先执行，与 Go 的 `defer` 语义一致。

#### 4.2.3 源码精读

[crates/gpui_util/src/lib.rs:514-L520](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L514-L520) —— `Drop` 实现：取出闭包，有则执行。

```rust
impl<F: FnOnce()> Drop for Deferred<F> {
    fn drop(&mut self) {
        if let Some(f) = self.0.take() {
            f()
        }
    }
}
```

对照一个真实使用点。[crates/lsp/src/lsp.rs:660-L665](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/lsp/src/lsp.rs#L660-L665)：LSP stdout 处理循环开始前注册「作用域结束时清空 response_handlers」的守卫——无论循环是正常结束、`?` 提前返回还是 panic，处理器表都会被清理：

```rust
let _clear_response_handlers = gpui_util::defer({
    let response_handlers = response_handlers.clone();
    move || {
        response_handlers.lock().take();
    }
});
```

注意绑定名是 `_clear_response_handlers`（下划线**前缀**）而不是裸 `_`——这里的区别在 4.3 详解。zed 仓库里这类用法非常多（`rpc/src/peer.rs`、`context_server/src/client.rs`、`gpui_linux` 的 X11 剪贴板等），搜 `util::defer(` 能看到几十处。

#### 4.2.4 代码实践

**实践目标**：验证 drop 的触发时机覆盖所有退出路径，且多个守卫按 LIFO 顺序触发。

**操作步骤**：

1. 在 4.1.4 的 `defer_lab` 中追加测试（示例代码）：

   ```rust
   #[test]
   fn early_return_still_triggers_drop() {
       let log = Rc::new(RefCell::new(Vec::new()));

       fn inner(log: Rc<RefCell<Vec<&'static str>>>) -> Option<()> {
           let _first = {
               let log = log.clone();
               defer(move || log.borrow_mut().push("first"))
           };
           let _second = {
               let log = log.clone();
               defer(move || log.borrow_mut().push("second"))
           };
           log.borrow_mut().push("body"); // 两个守卫仍存活
           None? // 提前返回：? 在这里触发两个守卫的 drop
       }

       inner(log.clone()).unwrap_none();
       assert_eq!(*log.borrow(), vec!["body", "second", "first"]); // LIFO
   }
   ```

   （需要在文件头补充 `use std::cell::RefCell;`。）

2. `cargo test early_return_still_triggers_drop`。

**需要观察的现象**：即使函数通过 `None?` 提前返回，两个闭包仍然执行，且顺序是 `second` 在前、`first` 在后。

**预期结果**：测试通过，断言 `vec!["second", "first"]` 成立——这就是「`?` 提前返回也会触发 Drop」与「LIFO drop 顺序」的直接证据。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：把 `Drop::drop` 改成下面这样为什么编译不过？

```rust
fn drop(&mut self) {
    if self.0.is_some() {
        (self.0.unwrap())(); // 想当然的写法
    }
}
```

**答案**：`unwrap()` 会尝试把 `F` 从 `&mut self.0`（实际上是 `self.0` 这个地方的所有权属于 `self`，而 `self` 是借来的）中按值移出，这等于从借用的位置移动出值，违反 Rust 的借用规则（错误信息形如 `cannot move out of borrowed content` / `cannot move out of `self.0``）。`FnOnce` 的调用需要闭包的所有权，所以必须先用 `take()` 把值完整地「搬」出来。

**练习 2**：`f()` 执行期间如果发生了 panic，`Deferred` 会处于什么状态？会不会二次执行闭包？

**答案**：`take()` 在调用 `f()` 之前已经把 `self.0` 置为 `None`，所以即使 `f()` panic、panic 展开过程中这个值继续被 drop，`Drop::drop` 里看到的也是 `None`，闭包不会执行第二次。这正是「先 take 再调用」写法的健壮性所在。

**练习 3**：`Deferred` 在哪些情况下**不会**触发闭包执行？

**答案**：调用了 `abort()`；被 `std::mem::forget` 显式遗忘（泄漏）；进程通过 `std::process::abort` 等不展开栈的方式退出；程序在 drop 之前就结束。`Drop` 只覆盖「值正常离开作用域」的情形，这也是文档把语义限定为 "when the returned value is dropped" 的原因。

### 4.3 defer 构造函数与 #[must_use]

#### 4.3.1 概念说明

`defer` 是唯一的公开构造入口：把闭包装进 `Deferred` 并返回。它只有三行，但 `#[must_use]` 属性承载了本讲最重要的安全设计。

危险在于：`defer(f)` 返回的守卫**就是**清理动作的载体。如果你写

```rust
// 示例代码：危险写法
defer(f); // 语句末尾，临时守卫立即被 drop → f 立刻执行！
```

临时值在语句结束时立即 drop，闭包当场执行——这不是延迟，是马上调用，还比直接调 `f()` 多了一层包装。这几乎永远是 bug。`#[must_use]` 让编译器对「返回值被丢弃」发出 `unused_must_use` 警告，把这类错误拦在编译期。

#### 4.3.2 核心流程

正确与错误用法对照：

```
defer(f);            ✗ 守卫立即 drop，f 立即执行；#[must_use] 发出警告
let _ = defer(f);    ✗ 裸 `_` 不绑定，守卫同样立即 drop；且 let _ 会抑制警告（更隐蔽！）
let _g = defer(f);   ✓ 下划线前缀的名字仍然绑定，守卫存活到作用域末尾
let g = defer(f);    ✓ 最直白；若后面没用到 g 本身会有 unused_variables 警告
let g = defer(f);
g.abort();           ✓ 需要取消时按值交出守卫
```

关键辨析（也是 zed 代码评审的高频知识点）：

- `_g`（下划线**开头**的标识符）：是正常的变量绑定，只是告诉编译器「我知道它没被读，别警告」，守卫一直活到作用域结束。**这是守卫的标准命名**，如 lsp.rs 的 `_clear_response_handlers`、rpc.rs 的 `_end_connection`。
- `_`（裸下划线模式）：**不绑定**，右侧表达式产生的值在 `let` 语句结束时就 drop，守卫立即引爆清理；而且 `let _ =` 是「显式丢弃」，`#[must_use]` 的警告会被抑制。写 `let _ = defer(f);` 比 `defer(f);` 更糟——前者连警告都没有。

#### 4.3.3 源码精读

[crates/gpui_util/src/lib.rs:522-L526](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L522-L526) —— `defer` 函数：doc 注释一句话说清语义（"Run the given function when the returned value is dropped (unless it's cancelled)"），`#[must_use]` 防止返回值被当作语句丢弃，函数体只是把闭包装进 `Some`：

```rust
/// Run the given function when the returned value is dropped (unless it's cancelled).
#[must_use]
pub fn defer<F: FnOnce()>(f: F) -> Deferred<F> {
    Deferred(Some(f))
}
```

逐项说明：

- **doc 注释**把契约讲完整了：触发时机是 "when the returned value is dropped"，而非「函数返回时」；括号里的 "unless it's cancelled" 对应 `abort()`。
- **`#[must_use]`**：这是属性宏，作用于函数声明。返回值被忽略（作为表达式语句丢弃）时，编译器发出 `unused_must_use` lint——默认是 `warn` 级别，不是 error。也就是说 `defer(f);` 仍能编译通过（带警告）并立即执行闭包，`#[must_use]` 是提醒不是强制。
- **泛型约束 `F: FnOnce()`**：清理闭包只需要调用一次、不接收参数、不返回值。捕获环境通过 `move` 闭包按值带入（参考 git 例子中先 `clone` 再 `move` 的写法）。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 `#[must_use]` 的警告，以及两个「立即执行」陷阱各自的表现。

**操作步骤**：

1. 在 `defer_lab/src/lib.rs` 中写（示例代码）：

   ```rust
   pub fn bad_style() {
       defer(|| println!("bad_style: 立即执行了吗？"));
       println!("bad_style: 闭包之后");
   }
   ```

2. `cargo build`，观察编译器输出。

**需要观察的现象**：

- 编译器发出类似 `unused return value of defer that must be used` 的警告（`#[must_use]` 生效）。
- 若忽略警告运行，输出顺序是「bad_style: 立即执行了吗？」在「bad_style: 闭包之后」**之前**——守卫作为临时值在语句末尾就被 drop，闭包当场执行。

3. 再把那行改成 `let _ = defer(|| println!(...));` 重新 `cargo build`。

**预期结果**：这次**没有** `unused_must_use` 警告（`let _ =` 抑制了它），但闭包依然立即执行——这就是 4.3.2 说的「更隐蔽的陷阱」。正确写法 `let _g = defer(...)` 则既无警告、闭包也延迟到作用域结束。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：`#[must_use]` 能不能彻底阻止 `defer(f);` 这种错误？

**答案**：不能。它是 lint 而非类型检查或运行时机制，默认 `warn` 级别；项目可以在 `Cargo.toml` 里把该 lint 提升为 `deny`（zed 的 workspace lints 有类似做法，具体级别以根 `Cargo.toml` 为准，待确认），但默认情况下带警告也能编译，闭包照样立即执行。它大幅提高了错误被发现的概率，但最终防线仍是代码评审与测试。

**练习 2**：为什么 `let _ = defer(f);` 连警告都没有，却和 `defer(f);` 行为一样糟糕？

**答案**：`let _ = expr;` 中 `_` 是不绑定的模式，右侧值在语句结束即被 drop（守卫立即引爆）；同时 Rust 把 `let _ =` 视为「显式表明要丢弃」，`unused_must_use` 对此不再报警。行为同样错误、提示却消失了，所以它比裸语句更危险。正确写法是 `let _name = defer(f);`——下划线开头的**标识符**会真正绑定并持有守卫。

**练习 3**：如果想让清理闭包接收一个「作用域结果」并在成功时跳过清理（类似 scopeguard 的 `defer_on_unwind!`），用现有的 `Deferred` 能做到吗？

**答案**：可以近似做到——在闭包里判断一个由外部控制的标志，例如闭包捕获 `Rc<Cell<bool>>`，正常路径先 `flag.set(true)` 再让守卫自然 drop，闭包内检查标志决定是否清理；或者干脆像 git 例子那样在成功路径显式调用 `abort()`。`Deferred` 本身没有提供「按 unwind 与否分叉」的内置能力，它刻意保持最小；需要更复杂语义时，惯用做法是组合 `abort()` 与共享状态标志。

## 5. 综合实践

把本讲三个模块串起来，完成一个「作用域计时器」小工具加验证套件。全程在 `defer_lab` 练习 crate 中进行，不修改 zed 源码。

**任务**：

1. **作用域计时器**：写一个函数 `timed_scope<T>(label: &'static str, body: impl FnOnce() -> T) -> T`，进入时记录 `std::time::Instant::now()`，用 `defer` 注册「drop 时 `eprintln!` 打印 label 与耗时」，然后执行 body 并返回结果。要求：body 内部用 `?` 或提前 return 退出时，耗时照样打印（示例代码框架）：

   ```rust
   use gpui_util::defer;
   use std::time::Instant;

   pub fn timed_scope<T>(label: &'static str, body: impl FnOnce() -> T) -> T {
       let start = Instant::now();
       let _timer = defer(move || {
           eprintln!("[{label}] elapsed: {:?}", start.elapsed());
       });
       body()
   }
   ```

2. **abort 验证**：写测试证明 `abort()` 后闭包不执行（复用 4.1.4 的 `Rc<Cell<bool>>` 方案）。

3. **陷阱验证**：依次尝试 `defer(f);` 与 `let _ = defer(f);` 两种写法，记录 `cargo build` 的警告差异与运行时的执行时机差异，写一段 3–5 句的结论，说明团队规范里应该如何书写守卫绑定（提示：`let _描述性名称 = ...`）。

**验收标准**：

- `timed_scope("demo", || std::thread::sleep(std::time::Duration::from_millis(50)))` 输出的耗时约 50ms（`[demo] elapsed: 50.x ms`）。（待本地验证）
- abort 测试与 drop 测试全部通过。
- 能口头回答：「为什么 `#[must_use]` 拦不住 `let _ =`？」

## 6. 本讲小结

- `defer(f)` 返回一个守卫 `Deferred<F>`，闭包在守卫被 drop 时执行——把清理绑定到生命周期，正常返回、`?` 提前返回、panic 展开全部覆盖，这是 RAII 的确定性优势。
- `Deferred` 内部是私有的 `Option<F>`：`Some` 表示「待引爆」，`None` 表示「已引爆或已拆除」；字段私有保证外部只能经 `defer()` 构造。
- `Drop` 实现必须用 `Option::take()`：`FnOnce` 调用需要闭包所有权，而 `drop` 只有 `&mut self`；先 take 再调用也让闭包不可能执行两次。
- `abort(mut self)` 按值消费守卫并取走闭包，取消是一次性终态；真实用例见 git crate「兜底删临时文件、成功后 abort 撤销」。
- `defer(f);` 会让守卫作为临时值立即 drop、闭包当场执行；`#[must_use]` 对此发出警告，但 `let _ = defer(f);` 会抑制警告且行为同样错误——守卫的标准写法是 `let _描述性名称 = defer(...)`。
- 多个守卫按构造的逆序（LIFO）触发，与 Go 的 defer 语义一致。

## 7. 下一步学习建议

- 下一讲（u2-l6）转向 `src/arc_cow.rs`：`ArcCow<'a, T>` 在 `Borrowed(&T)` 与 `Owned(Arc<T>)` 之间二选一，同样是「一枚举两变体、为一批 trait 做对称委托」的设计，可与本讲 `Deferred` 的极简枚举风格对照着读。
- 在 zed 仓库中执行 `grep -rn "util::defer(" crates/ | head -30`，挑两个真实调用点（建议 `crates/rpc/src/peer.rs:156` 与 `crates/agent/src/tools/edit_session.rs:745`），说出每个守卫清理的是什么资源、存活到哪个作用域末尾。
- 对照阅读标准库的 `MutexGuard`（`lock` 返回的 RAII 守卫）与 scopeguard crate，体会「drop 引信」这一模式的通用形态。
- 想深入 drop 顺序与 `let _` 语义的规范依据，可读 Rust Reference 的 *Drop scopes* 与 *Underscore patterns* 两节，验证本讲的 LIFO 与「裸 `_` 不绑定」结论。
