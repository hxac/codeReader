# 作用域线程 scope 快速上手

## 1. 本讲目标

本讲是整个学习路线里「第一次真正动手写并发代码」的入口。读完本讲，你应当能够：

- 用 `crossbeam_utils::thread::scope` 启动多个子线程，并让它们安全地借用主线程栈上的变量（例如 `&Vec`、`&mut [i32]`）。
- 读懂 `scope` / `spawn` / `ScopedJoinHandle` 的函数签名，特别是其中那个看起来神秘的 `'env` 生命周期参数代表什么。
- 理解「作用域结束」这一时刻发生了什么：所有未手动 join 的线程会被自动等待，并且任一线程 panic 都能被捕获、汇总返回。
- 对照真实源码讲清楚一件事：编译器凭什么相信「这些线程一定会在变量被销毁前结束」。

本讲只读一个文件 [crossbeam-utils/src/thread.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs)，它是 crossbeam 里最短小、最适合上手的一个并发原语。

## 2. 前置知识

### 2.1 为什么 `std::thread::spawn` 不能借用栈上变量

标准库的 `std::thread::spawn` 签名大致是：

```rust
pub fn spawn<F, T>(f: F) -> JoinHandle<T>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
```

注意 `F: ... + 'static`。这要求传进去的闭包以及它捕获的所有变量都必须是 `'static` 的——也就是说，要么拥有所有权，要么借用的是 `&'static` 全局数据。

为什么这么严格？因为 `spawn` 返回一个 `JoinHandle`，但**调用者不一定会调用 `.join()`**。线程可能在主函数返回、栈帧销毁之后还在跑。如果允许它借用主线程栈上的局部变量，等主线程一返回，那块栈内存就失效了，子线程再用就是悬垂引用（use-after-free）。Rust 编译器无法证明「你一定会 join」，干脆一律禁止。

### 2.2 作用域线程的核心思路

作用域线程（scoped thread）的思路是：用一个明确的代码块（作用域）向编译器**做出承诺**——

> 在这个作用域里 spawn 出去的所有线程，保证在作用域结束之前全部 join 完毕。

只要这个承诺成立，作用域里的线程借用作用域外（也就是更外层栈帧）的 `&T` 就是安全的：因为那些变量的生命周期一定比「作用域结束」长，而线程又一定在「作用域结束」之前结束。

crossbeam 用一个 RAII 的运行时机制来兑现这个承诺（作用域退出时自动 join）。Rust 1.63 起标准库也加入了等价的 [`std::thread::scope`](https://doc.rust-lang.org/std/thread/fn.scope.html)，本讲讲的 crossbeam 版本是其前辈，思路完全一致。源码注释里也明确说明这一点：

> **Note:** Since Rust 1.63, this function is soft-deprecated in favor of the more efficient `std::thread::scope`.
> —— [crossbeam-utils/src/thread.rs:131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L131)

> 提示：即使有了标准库版本，学习 crossbeam 的实现仍然有价值——它的 `Scope` 是 `Sync` 的，可以跨线程共享来 spawn，并且是后续学习 `WaitGroup`、`Parker` 等同步原语的热身。

## 3. 本讲源码地图

本讲只涉及一个文件，外加它的一个依赖：

| 文件 | 作用 |
| --- | --- |
| [crossbeam-utils/src/thread.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs) | `scope` / `Scope` / `spawn` / `ScopedJoinHandle` 的全部实现 |
| [crossbeam-utils/src/sync/wait_group.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs) | `WaitGroup`，被 `scope` 用来协调嵌套作用域的结束时机 |
| [crossbeam-utils/tests/thread.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs) | 行为测试，是本讲「代码实践」的素材来源 |

该模块在 `crossbeam-utils` 中通过 `pub mod thread;` 暴露（[lib.rs:100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L100)），又经主 crate 门面重导出为 `crossbeam::thread`（见前置讲义 u1-l3）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. `scope` 的签名与生命周期 `'env`
2. `spawn` 与 `ScopedJoinHandle::join`
3. 作用域结束时自动等待所有线程（含 panic 处理）

### 4.1 `scope` 的签名与生命周期 `'env`

#### 4.1.1 概念说明

`scope` 是整个机制的入口。它的签名（[thread.rs:146-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L146-L149)）：

```rust
pub fn scope<'env, F, R>(f: F) -> thread::Result<R>
where
    F: FnOnce(&Scope<'env>) -> R,
```

这里有两个生命周期/类型要点：

- **`'env`**：表示「作用域外那些被借用变量的生命周期」。闭包 `f` 接收一个 `&Scope<'env>`，后续在作用域里 spawn 的线程，就允许借用一切满足 `: 'env` 的数据。`'env` 是「环境 environment」的缩写。
- **返回值 `thread::Result<R>`**：也就是 `Result<R, Box<dyn Any + Send + 'static>>`。如果所有线程都正常结束，返回 `Ok(f 的返回值)`；如果有线程 panic，返回 `Err`，里面装着收集到的 panic 信息。

读者第一次看会觉得 `'env` 像是凭空出现的——它没有出现在 `f` 的参数类型之外的地方。它的真正作用是「把外层栈帧的生命周期信息注入到 `Scope` 类型里」，这样后续 `spawn` 的约束才能写出来（见 4.2）。

#### 4.1.2 核心流程

`scope` 的执行流程可以概括为：

```text
1. 新建一个 WaitGroup（初始计数=1）和一个 Scope 对象
2. 用 panic::catch_unwind 包裹执行 f(&scope)
3. 安装一个 AbortOnPanic 守卫（见 4.3）
4. drop(scope.wait_group) —— 让根作用域的 WaitGroup 引用先走
5. wg.wait()      —— 等待所有嵌套作用域（它们的 WaitGroup 克隆）都 drop
6. 遍历 handles 列表，join 所有尚未手动 join 的线程，收集 panic
7. 撤销 AbortOnPanic 守卫
8. 根据 f 是否 panic、子线程是否 panic 返回 Result
```

#### 4.1.3 源码精读

`Scope` 结构体的定义（[thread.rs:206-215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L206-L215)）：

```rust
pub struct Scope<'env> {
    /// The list of the thread join handles.
    handles: SharedVec<SharedOption<thread::JoinHandle<()>>>,

    /// Used to wait until all subscopes all dropped.
    wait_group: WaitGroup,

    /// Borrows data with invariant lifetime `'env`.
    _marker: PhantomData<&'env mut &'env ()>,
}
```

要点：

- `handles`：所有 spawn 出来的线程的 `JoinHandle` 都存这里。类型层层包装：`Arc<Mutex<Vec<Arc<Mutex<Option<JoinHandle>>>>>>`（`SharedVec`/`SharedOption` 在 [thread.rs:120-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L120-L121) 定义）。这种「共享可变」包装是因为 handle 既可能被作用域结束时自动 join，也可能被用户手动 `handle.join()` 取走，必须用 `Option` + `Mutex` 协调。
- `wait_group`：协调嵌套作用域的结束，详见 4.3。
- `_marker: PhantomData<&'env mut &'env ()>`：这行是 `'env` 的「定海神针」。它做了两件事——(a) 让 `Scope<'env>` 在不持有真正 `&'env` 引用的前提下仍然「携带」`'env`；(b) 用 `&'env mut &'env ()` 这种「双引用」形式把 `'env` 标记为**逆变/不变的（invariant）**，从而禁止编译器把较短的生命周期「收缩」进来。这是作用域线程安全模型里非常关键的一步，但本讲只需记住它的效果：**`'env` 一旦确定就不能被偷偷缩短**。

紧跟着的 [thread.rs:217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L217) 是 `unsafe impl Sync for Scope<'_> {}`——这是允许把 `&Scope` 从一个线程传到另一个线程（从而在线程内部继续 `spawn` 嵌套线程）的前提。这个 `unsafe` 的安全性正是靠上面的不变生命周期和「作用域结束自动 join」共同保证的。

`scope` 函数体的开头构造了这个对象（[thread.rs:159-164](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L159-L164)）：

```rust
let wg = WaitGroup::new();
let scope = Scope::<'env> {
    handles: SharedVec::default(),
    wait_group: wg.clone(),
    _marker: PhantomData,
};
```

注意 `WaitGroup::new()` 创建时内部计数为 `1`（见 [wait_group.rs:67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L67)），随后 `wg.clone()` 又 `+1` 到 `2`，一份给 `scope.wait_group`，一份留在本地 `wg`。这个 `1+1` 的安排是 4.3 节「等待嵌套作用域」机制的基础。

#### 4.1.4 代码实践

**实践目标**：用眼睛「跑」一遍 `scope` 的生命周期标注，体会 `'env` 如何把外层变量「带进」作用域。

**操作步骤**：

1. 在 `crossbeam-utils` 目录外新建一个临时 crate（或直接对照源码）阅读下面这段「示例代码」（非项目原有代码）：

```rust
// 示例代码：体会 'env
use crossbeam_utils::thread;

fn main() {
    let people = vec!["Alice".to_string(), "Bob".to_string()]; // 人们 lives 在 main 的栈帧

    thread::scope(|s| {          // s: &Scope<'env>，这里的 'env 绑定到 main 栈帧
        for person in &people {  // person: &'env String，能借给子线程
            s.spawn(move |_| {
                println!("Hello, {person}!");
            });
        }
    }).unwrap();
    // 作用域在这里返回，所有子线程已被 join，people 此时才被释放，安全。
}
```

2. 把鼠标停在 `s` 上看它的类型 `&Scope<'_>`，再停在闭包捕获的 `person` 上看生命周期。
3. 试着把 `thread::scope` 改成 `std::thread::scope`（标准库等价 API），观察行为是否一致。

**需要观察的现象**：`person` 是对 `people` 的借用，却能被 `move` 进子线程——这正是 `'env` 带来的能力。如果改成 `std::thread::spawn`，编译器会报 `E0597: people does not live long enough`（源码注释里贴了这个错误，见 [thread.rs:49-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L49-L62)）。

**预期结果**：crossbeam 版本与标准库版本都能编译并依次打印 `Hello, Alice!` / `Hello, Bob!`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `let people = vec![...]` 移到 `thread::scope(|s| {...})` 调用**之后**，会发生什么？

**参考答案**：编译失败。`people` 的生命周期会短于 `'env`（它定义在 scope 之后，作用域内的借用活得比它久），借用检查器会拒绝。`'env` 的不变性（4.1.3 的 `_marker`）正是防止这类「偷缩生命周期」的漏洞。

**练习 2**：`scope` 的返回类型是 `thread::Result<R>` 而不是 `R`，为什么？

**参考答案**：因为子线程可能 panic。`scope` 必须把这种情况汇报给调用者，所以用 `Result`：`Ok` 表示所有线程都成功，`Err` 表示至少一个线程（或 `f` 本身）panic 了。

---

### 4.2 `spawn` 与 `ScopedJoinHandle::join`

#### 4.2.1 概念说明

在作用域里，用 `s.spawn(...)` 启动一个子线程。它的关键签名（[thread.rs:258-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L258-L262)）：

```rust
pub fn spawn<'scope, F, T>(&'scope self, f: F) -> ScopedJoinHandle<'scope, T>
where
    F: FnOnce(&Scope<'env>) -> T,
    F: Send + 'env,
    T: Send + 'env,
```

这里出现了**第二个生命周期 `'scope`**，它和 `'env` 的分工是理解作用域线程的钥匙：

- `'env`：线程能借用的**外部数据**的生命周期（来自最外层 `scope` 调用）。
- `'scope`：`&self`（也就是 `Scope` 对象自身）的借用生命周期，决定了返回的 `ScopedJoinHandle<'scope, T>` 能活多久。

约束 `F: Send + 'env` 和 `T: Send + 'env` 含义直观：闭包及其返回值必须能跨线程传递（`Send`），并且不能含有比 `'env` 更短的生命周期。**注意没有 `'static`**——这正是相对于 `std::thread::spawn` 的核心放宽。

还有一个小细节：闭包 `f` 的参数是 `&Scope<'env>`，而不是 `()`。也就是说每个子线程都会收到一个「自己的 scope 引用」，可以用来继续 spawn 嵌套线程（见 4.2.4 的嵌套示例）。

`spawn` 返回 `ScopedJoinHandle<'scope, T>`，你可以选择：

- 调用 `handle.join()` 主动等待并取回返回值 `T`；
- 或者**什么都不做**，让作用域结束时自动 join。

#### 4.2.2 核心流程

`spawn` 本身只是 `builder().spawn(f).expect(...)` 的包装（[thread.rs:264-267](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L264-L267)）。真正干活的是 `ScopedThreadBuilder::spawn`（[thread.rs:424-480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L424-L480)），流程如下：

```text
1. 新建 result = Arc<Mutex<Option<T>>>，用来存闭包返回值
2. 克隆出一份新的 Scope（共享 handles 和 wait_group）准备移入子线程
3. 构造闭包 closure = move || { let scope = scope; let res = f(&scope); *result = Some(res); }
4. Box::new(closure) 得到 Box<dyn FnOnce() + Send + 'env>
5. unsafe { mem::transmute(...) } 把生命周期擦除成 'static  ★ 关键 unsafe
6. self.builder.spawn(closure) 真正交给 OS 起线程
7. 把 JoinHandle 包成 Arc<Mutex<Option<...>>> 推入 handles 列表
8. 返回 ScopedJoinHandle { handle, result, thread, _marker }
```

第 5 步是整个模块唯一真正「危险」的地方，也是它能把 `'env` 数据塞进只接受 `'static` 的 `std::thread::spawn` 的秘密。下面精读。

#### 4.2.3 源码精读

闭包的构造与生命周期擦除（[thread.rs:446-463](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L446-L463)）：

```rust
let closure = move || {
    // Make sure the scope is inside the closure with the proper `'env` lifetime.
    let scope: Scope<'env> = scope;

    // Run the closure.
    let res = f(&scope);

    // Store the result if the closure didn't panic.
    *result.lock().unwrap() = Some(res);
};

// Allocate `closure` on the heap and erase the `'env` bound.
let closure: Box<dyn FnOnce() + Send + 'env> = Box::new(closure);
let closure: Box<dyn FnOnce() + Send + 'static> =
    unsafe { mem::transmute(closure) };

// Finally, spawn the closure.
self.builder.spawn(closure)?
```

为什么这里的 `transmute` 是安全的？因为 `scope` 函数在返回前会 `join` 所有线程（4.3 节）。换句话说，虽然我们「骗」了类型系统说这个闭包是 `'static`，但**实际运行时**保证它一定在 `'env` 数据失效之前就执行完毕。这是「用运行时机制兑现编译期承诺」的典型范式——把安全责任从单点 `transmute` 转移到了 `scope` 的自动 join 逻辑上。

> 小贴士：这就是为什么本模块的 `unsafe` 写得很克制——只有一个 `transmute`（[thread.rs:459-460](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L459-L460)）和几个 `unsafe impl Send/Sync`（[thread.rs:217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L217)、[thread.rs:483-484](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L483-L484)），它们的安全性都挂在「作用域结束自动 join」这个钩子上。

`ScopedJoinHandle` 的结构（[thread.rs:490-502](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L490-L502)）：

```rust
pub struct ScopedJoinHandle<'scope, T> {
    handle: SharedOption<thread::JoinHandle<()>>,  // 真正的 OS 线程句柄
    result: SharedOption<T>,                        // 闭包返回值
    thread: thread::Thread,                         // 线程句柄（用于拿 id/name）
    _marker: PhantomData<&'scope ()>,               // 绑定 'scope
}
```

`join` 方法（[thread.rs:533-542](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L533-L542)）：

```rust
pub fn join(self) -> thread::Result<T> {
    let handle = self.handle.lock().unwrap().take().unwrap();
    handle
        .join()
        .map(|()| self.result.lock().unwrap().take().unwrap())
}
```

读法：`take()` 把 `Option<JoinHandle>` 取走（变成 `None`），这样作用域结束时再扫一遍 `handles` 列表，已经手动 join 过的就会被 `filter_map` 过滤掉，不会重复 join。`join` 成功后从共享的 `result` 里取出真正的返回值 `T`。如果子线程 panic，`handle.join()` 返回 `Err`，于是 `map` 不执行，直接把 panic 往上传。

#### 4.2.4 代码实践

**实践目标**：体验 `ScopedJoinHandle::join` 取回返回值，以及「把 `&Scope` 当参数」实现嵌套 spawn。

**操作步骤**：

1. 阅读项目自带的 doc-test（[thread.rs:246-256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L246-L256)），它展示了 spawn 返回 `42` 并被 join 取回。
2. 再看嵌套 spawn 的标准用法（[thread.rs:105-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L105-L111)）：外层闭包写 `|s|`（而不是 `|_|`），用传入的 `s` 再 spawn。
3. 运行项目的嵌套测试：

```bash
cargo test -p crossbeam-utils --test thread nesting
```

**需要观察的现象**：嵌套线程能正常打印/返回，且作用域返回时所有层级的线程都已结束。

**预期结果**：测试通过。注意 `nesting` 测试（[tests/thread.rs:136-166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L136-L166)）里递归 spawn 了 5 层，最终 `.unwrap()` 成功，说明无论嵌套多深，最外层 `scope` 都会等所有后代线程结束。

> 待本地验证：如果你不在 Linux/amd64 环境，线程创建行为可能略有差异，但测试逻辑不变。

#### 4.2.5 小练习与答案

**练习 1**：`spawn` 的签名里 `F: Send + 'env`，为什么是 `'env` 而不是 `'scope`？

**参考答案**：因为闭包捕获的外部数据来自 `'env`（最外层 scope 之外的栈帧），它必须能活到 `'env`。`'scope` 只约束 `ScopedJoinHandle` 本身能存活多久，与闭包捕获数据的生命周期是两回事。事实上 `'scope` 通常比 `'env` 短（handle 在作用域内就被 join/drop），而 `'env` 数据要一直活到作用域之后。

**练习 2**：如果同一个 handle 调用两次 `join()` 会怎样？

**参考答案**：第二次会 panic。因为 `join` 内部 `self.handle.lock().unwrap().take().unwrap()` 第一次就把 `Option` 取空了（[thread.rs:536](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L536)），第二次 `.unwrap()` 在 `None` 上触发 panic。同时 `join(self)` 按值消费了 handle，类型层面也不允许第二次调用。

---

### 4.3 作用域结束时自动等待所有线程

#### 4.3.1 概念说明

前两节反复说「作用域结束时会自动 join」，本节就看这到底是怎么兑现的，以及两个关键工程细节：

1. **嵌套作用域的等待**：子线程内部又 spawn 了孙线程，根 `scope` 怎么知道「连孙线程也结束」了？答案是用 `WaitGroup` 做引用计数。
2. **panic 不逃逸作用域**：如果某个子线程 panic，不能让 panic 沿调用栈一路向上「炸」到根 `scope` 之外——否则 4.2.3 那个 `transmute` 的安全前提（线程已被 join）就可能被破坏。crossbeam 用一个 `AbortOnPanic` 守卫把「作用域还没 join 完时的 unwind」升级为 `abort`。

#### 4.3.2 核心流程

`scope` 函数体后半段（[thread.rs:166-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L166-L202)）的等待与收尾：

```text
A. catch_unwind 执行 f(&scope)            —— f 的 panic 被捕获，存入 result
B. 安装 AbortOnPanic 守卫 guard           —— 之后若再 panic 就 abort 进程
C. drop(scope.wait_group)                 —— 根 scope 的那份 WaitGroup 计数 -1
D. wg.wait()                              —— 等到所有克隆的 WaitGroup 都 drop（嵌套 scope 结束）
E. 扫描 handles，join 所有未被手动 join 的线程，收集 panic 到 panics
F. mem::forget(guard)                     —— 安全了，撤销 abort 守卫
G. match result: f panic 则 resume_unwind；否则看 panics 是否为空决定 Ok/Err
```

关于 `WaitGroup` 的计数语义（[wait_group.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs)）：每 `clone` 一次 `count + 1`（[wait_group.rs:144-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L144-L151)），每 `drop` 一次 `count - 1`（[wait_group.rs:134-142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L134-L142)）。`wait(self)` 会消费自己那份引用（也 `-1`），然后阻塞直到 `count` 归零（[wait_group.rs:110-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L110-L131)）。

在 `scope` 里，初始 `count = 1`（`new`），构造 `Scope` 时又 `clone` 到 `2`。所以步骤 C `drop(scope.wait_group)` 让 `count` 从 `2` 减到 `1`，步骤 D `wg.wait()` 再减 `1` 到 `0` 之外的剩余部分要等谁？等所有「子 Scope」持有的 `wait_group.clone()`。每次 `ScopedThreadBuilder::spawn` 都会克隆一份新 `Scope`（[thread.rs:438-442](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L438-L442)），那份 `Scope` 的 `wait_group` 字段会在子线程的闭包结束、`Scope` 被 drop 时减少计数。**只有当所有层级（子、孙、……）的 `Scope` 都 drop 完毕，`count` 才归零，`wg.wait()` 才返回。**这就是「等待所有嵌套作用域」的实现。

> 说明：这里只关心「嵌套 Scope 全部 drop」，而单个线程是否结束由后面的 `handles.join()` 负责。两段式等待：先等 Scope 体系（WaitGroup），再等裸线程（JoinHandle）。

#### 4.3.3 源码精读

`AbortOnPanic` 守卫（[thread.rs:150-157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L150-L157)）：

```rust
struct AbortOnPanic;
impl Drop for AbortOnPanic {
    fn drop(&mut self) {
        if thread::panicking() {
            std::process::abort();
        }
    }
}
```

它在 [thread.rs:171](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L171) 创建（`let guard = AbortOnPanic;`），在 [thread.rs:188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L188) 用 `mem::forget(guard)` 撤销。它的作用窗口覆盖了步骤 C–E（等待与 join）。如果在这段窗口里发生 unwinding panic（比如某个 drop 实现里 panic），`guard` 的 `Drop` 会发现 `thread::panicking()` 为真，直接 `abort` 整个进程。

为什么要这么激进？因为如果在「还没 join 完所有线程」时让 unwind 继续，`scope` 函数会提前返回，那些仍在跑的子线程就会脱离控制——它们引用的栈数据随后可能失效。这是 4.2.3 `transmute` 安全前提被打破的唯一途径，所以宁可 abort 也不冒险。

最后的返回逻辑（[thread.rs:193-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L193-L202)）：

```rust
match result {
    Err(err) => panic::resume_unwind(err),   // f 自己 panic：恢复传播
    Ok(res) => {
        if panics.is_empty() {
            Ok(res)                          // 全部成功
        } else {
            Err(Box::new(panics))            // 有子线程 panic：打包返回
        }
    }
}
```

注意三种结局的区别：

| 情况 | 返回 | 说明 |
| --- | --- | --- |
| `f` 与所有子线程都成功 | `Ok(R)` | 正常路径 |
| `f` 本身 panic | （`resume_unwind` 继续 unwind） | 不吞掉 `f` 的 panic，原样向上传 |
| `f` 成功但有子线程 panic | `Err(Box<Vec<Box<dyn Any+Send>>>)` | 把所有子线程的 panic 收集成 `Vec` 打包 |

`panics` 是怎么收集的？见 [thread.rs:178-186](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L178-L186)：

```rust
let panics: Vec<_> = scope
    .handles
    .lock()
    .unwrap()
    .drain(..)
    .filter_map(|handle| handle.lock().unwrap().take())  // 跳过已被手动 join 的
    .filter_map(|handle| handle.join().err())             // 只留 panic 错误
    .collect();
```

#### 4.3.4 代码实践

**实践目标**：亲手制造一个子线程 panic，观察 `scope` 如何收集并返回错误，而不会让进程崩溃。

**操作步骤**：

1. 阅读项目测试 `panic_twice`（[tests/thread.rs:88-110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L88-L110)）。它故意让两个子线程都 panic，然后 `downcast` 检查收到的 `Vec` 长度为 2。
2. 运行该测试：

```bash
cargo test -p crossbeam-utils --test thread panic_twice
```

3. 想象一下（或自己写一段「示例代码」验证）：把 `scope` 调用包进 `assert!(result.is_err())`，体会「panic 被收集成普通返回值」的效果。

**需要观察的现象**：测试通过；进程**没有**因为子线程 panic 而崩溃，`scope` 把两个 panic 都收进了返回的 `Err` 里。

**预期结果**：`panic_twice` 测试断言 `vec.len() == 2` 成立（[tests/thread.rs:104](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L104)），并且能 `downcast_ref::<&str>()` 还原原始 panic 信息。

> 待本地验证：`panic_many` 测试（[tests/thread.rs:113-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L113-L134)）进一步验证 3 个 panic 全部被收集，可一并运行 `cargo test -p crossbeam-utils --test thread` 观察全绿。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AbortOnPanic` 守卫要在步骤 F 用 `mem::forget(guard)` 撤销，而不是让它自然 drop？

**参考答案**：到步骤 F 时，所有线程已经被 join 完毕，安全前提满足了。此时即使 `result` 处理过程中出现意料外的 panic，也不会再造成「线程逃逸作用域」的危险。如果不 `forget`，守卫在函数末尾自然 drop 时一旦检测到 panic 就会 abort，那样会把「本可以安全传播的 panic」变成进程崩溃，过于激进。

**练习 2**：如果一个子线程既没有被手动 `join`，也没有 panic，`scope` 结束时会怎样处理它的返回值 `T`？

**参考答案**：`T` 被丢弃（drop）。返回值存在 `result: Arc<Mutex<Option<T>>>` 里（[thread.rs:431](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L431)），作用域结束时这个 `Arc` 的最后一个引用被释放，`Option<T>` 随之 drop。所以「不 join 就拿不到返回值」——想用返回值必须显式 `handle.join()`。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「并发分段求和」任务。

**任务**：给定一段 `&mut [i32]`（或先 `&[i32]`），用 `scope` 启动 4 个线程，每个线程处理切片的一段，把局部和写到一个由主线程持有的数组里，最后在作用域**内**收集结果并求总和。

**示例代码**（非项目原有代码，需自行放入一个依赖 `crossbeam-utils` 的 crate 的 `main` 里运行）：

```rust
use crossbeam_utils::thread;

fn main() {
    let data: Vec<i32> = (1..=40).collect();       // 1..40，总和应为 820
    let chunk = data.len() / 4;
    let mut partials = [0i64; 4];                  // 栈上的局部结果数组

    thread::scope(|s| {
        for i in 0..4 {
            // 借用 data 的一段和 partials 的一个槽位 —— 体现「借用栈上数据」
            let span = &data[i * chunk..(i + 1) * chunk];
            let slot = &mut partials[i];           // &mut 借用，每个线程独占一个槽
            s.spawn(move |_| {
                *slot = span.iter().map(|&x| x as i64).sum();
            });
        }
        // 这里所有 handle 都没有手动 join，靠作用域结束自动 join
    })
    .unwrap();                                     // 任一线程 panic 都会变成 Err

    let total: i64 = partials.iter().sum();
    println!("partials = {partials:?}");
    println!("total = {total}");
    assert_eq!(total, (1..=40).map(|x| x as i64).sum());
}
```

**操作步骤**：

1. 新建一个二进制 crate：`cargo new scoped-sum --bin && cd scoped-sum`。
2. 在 `Cargo.toml` 加入 `crossbeam-utils = "0.8"`。
3. 把上面代码贴进 `src/main.rs`，`cargo run`。
4. 进阶：把 `data` 切片改成 `&mut [i32]`，让每个线程就地把自己那段清零，验证作用域线程也能持有 `&mut` 借用（注意每个线程只能拿到互不重叠的 `&mut` 段）。
5. 进阶：故意在某个线程里写 `panic!("boom")`，把 `.unwrap()` 改成 `match`，打印收到的 panic 列表，验证 4.3 的错误收集行为。

**需要观察的现象与预期结果**：

- 程序打印 `total = 820`（1 到 40 的和）。
- `partials` 是 4 个非零的局部和，加起来等于 820。
- 关键对比：把 `thread::scope` 换成 `std::thread::spawn` 会直接编译失败（`E0597` / `E0373`），因为 `partials` 和 `data` 不是 `'static`。这就直观体现了作用域线程「借用栈上数据」的价值。
- 进阶 panic 实验：进程不会崩溃，`scope` 返回 `Err`，里面是一个长度为 1 的 panic `Vec`。

## 6. 本讲小结

- `crossbeam_utils::thread::scope` 通过一个明确的作用域，向编译器承诺「所有 spawn 的线程在作用域结束前一定 join」，从而放宽了 `std::thread::spawn` 的 `'static` 限制，允许子线程借用 `'env` 生命周期的栈上数据。
- `Scope<'env>` 用 `PhantomData<&'env mut &'env ()>` 把 `'env` 固定为不变生命周期，是整个安全模型的地基；`spawn` 内部用一处 `unsafe { mem::transmute }` 把 `'env` 闭包擦成 `'static` 喂给标准库，其安全性完全依赖「作用域结束自动 join」。
- `spawn` 区分两个生命周期：`'env`（可借用的外部数据）和 `'scope`（`ScopedJoinHandle` 自身存活期）；`ScopedJoinHandle::join` 取回返回值 `T`，不手动 join 的线程会被作用域自动 join。
- 作用域结束时先 `WaitGroup::wait` 等待所有嵌套 `Scope` 体系 drop，再扫描 `handles` 列表 join 剩余线程；期间用 `AbortOnPanic` 守卫防止「线程未 join 完就 unwind」破坏 `transmute` 的安全前提。
- 子线程的 panic 不会被传播成进程崩溃，而是被收集成 `Vec<Box<dyn Any + Send>>` 通过 `scope` 的 `Err` 返回；`f` 自身的 panic 则通过 `resume_unwind` 原样继续传播。

## 7. 下一步学习建议

本讲是并发原语的「热身」，后续可以沿着两条线深入：

- **同步原语线**（本讲所在单元 u2）：`scope` 内部用到的 `WaitGroup` 是下一站的好材料，建议阅读 [crossbeam-utils/src/sync/wait_group.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs)，对应讲义 u2-l6；想理解 `Scope` 实现里更深的 `unsafe`/生命周期技巧，可读 u2-l7「作用域线程实现内幕」。
- **数据结构线**：有了「多个线程安全协作」的直觉后，第 3 单元将进入 crossbeam 最具规模的 `crossbeam-channel`，从 `unbounded`/`bounded` 通道开始（u3-l1），那里会再次用到本讲练到的「线程间共享数据」思维。

无论走哪条线，建议先把本讲的「综合实践」跑通，确保你能解释：为什么换掉 `scope` 就编译不过、为什么子线程 panic 不会让进程崩溃。
