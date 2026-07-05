# Scope、ScopedThreadBuilder 与 ScopedJoinHandle

## 1. 本讲目标

上一讲（u4-l1）我们剖析了 `thread::scope` 的**入口函数**：它用 `WaitGroup` 做栅栏、用 `AbortOnPanic` 守卫封堵线程逃逸窗口、最后自动 join 残留线程。但那时我们刻意回避了三个最关键的问题：

1. 用户调用 `s.spawn(...)` 时，**线程到底是怎么被造出来的**？
2. 闭包明明借用了 `'env` 的栈上数据，凭什么能塞进要求 `'static` 的 `std::thread::spawn`？
3. `ScopedJoinHandle::join()` 是怎么把子线程的**返回值**取回来的？

本讲就回答这三个问题。学完后你应当能够：

- 说清 `Scope::spawn` 与 `Scope::builder` 的分工，以及它们最终都汇聚到 `ScopedThreadBuilder::spawn`。
- 解释 `Box<dyn FnOnce() + Send>` + `mem::transmute` 这套「生命周期擦除」技巧为什么是安全的。
- 解释 `ScopedJoinHandle` 为何要把 `JoinHandle` 与结果 `T` 分别存进两个 `Arc<Mutex<Option<_>>>`，以及 `join()` 如何取回返回值。
- 写出「在子线程内再 spawn 子线程」的嵌套用法，并理解为什么要给闭包传入新的 `Scope` 引用。

## 2. 前置知识

本讲默认你已经掌握 u4-l1 的全部结论。为了行文自洽，这里用一句话回顾相关概念：

- **作用域线程（scoped thread）**：通过「scope 返回前一定 join 所有子线程」的承诺，让借用检查器允许子线程借用 `'env` 生命周期（非 `'static`）的栈上数据。
- **`'env` 不变生命周期**：`Scope<'env>` 用 `PhantomData<&'env mut &'env ()>` 把 `'env` 标记为**不变（invariant）**，防止编译器把生命周期悄悄缩短或延长，从而保证共享状态的类型严格匹配。
- **`WaitGroup`**：引用计数式同步原语，clone 增计数、drop/wait 减计数，归零后唤醒等待者。`scope()` 用它作为「所有子 Scope 已销毁」的栅栏。
- **`Arc<Mutex<T>>` 共享可变状态**：本讲会大量出现它的类型别名 `SharedVec<T>` 与 `SharedOption<T>`。

如果你对上面任何一项感到陌生，建议先回到 u4-l1、u3-l2 复习。

另外需要一点 Rust 基础：

- **`Box<dyn FnOnce() + Send>`**：把闭包装箱成**trait 对象**，从而擦除闭包的具体类型，只保留「可调用一次、可跨线程移动」的契约。
- **`mem::transmute`**：在两个**大小相同**的类型间做位级重解释；它是 `unsafe` 的，因为编译器不再替你检查语义正确性。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它是整章的核心：

| 文件 | 作用 |
| --- | --- |
| `src/thread.rs` | 作用域线程的全部实现：`scope()` 入口（u4-l1 已讲）、`Scope`、`ScopedThreadBuilder`、`ScopedJoinHandle`，以及 unix/windows 平台扩展。 |

文件顶部有两个贯穿全讲的类型别名，先记住它们能省掉一半阅读负担：

```rust
type SharedVec<T> = Arc<Mutex<Vec<T>>>;
type SharedOption<T> = Arc<Mutex<Option<T>>>;
```

[src/thread.rs:L120-L121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L120-L121)：定义两个共享容器的别名。`SharedVec` 用来收集所有线程的 join 句柄；`SharedOption` 既用来存句柄（允许「scope 自动 join」与「用户手动 join」二选一地取走），也用来存线程返回值。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进，对应三个类型：

1. **`Scope` 与 `spawn`/`builder`**：作用域对象本身，提供两种 spawn 入口。
2. **`ScopedThreadBuilder`**：真正造线程的地方，包含危险的 `transmute`。
3. **`ScopedJoinHandle`**：取回返回值与线程句柄的句柄。

### 4.1 Scope 与 spawn / builder

#### 4.1.1 概念说明

`Scope<'env>` 是用户在 `thread::scope(|s| { ... })` 里拿到的那个 `s`。它对外只暴露两件事：

- `s.spawn(f)`：用一个默认配置的 builder 起 thread，**失败则 panic**。
- `s.builder()`：返回一个可配置（name、stack_size）的 builder，**失败返回 `io::Result`**，适合需要从「OS 拒绝创建线程」中恢复的场景。

二者的关系是：`spawn` 只是 `builder().spawn(f).expect(...)` 的一行糖。所有真正的活都在 `ScopedThreadBuilder::spawn` 里干。

`Scope` 自身只持有三块状态，全部是「可被多个线程共享」的句柄：

```rust
pub struct Scope<'env> {
    handles: SharedVec<SharedOption<thread::JoinHandle<()>>>,
    wait_group: WaitGroup,
    _marker: PhantomData<&'env mut &'env ()>,
}
```

[src/thread.rs:L206-L215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L206-L215)：`Scope` 结构定义。`handles` 是所有（子）线程句柄的共享列表；`wait_group` 用来追踪「还有多少个子 Scope 存活」；`_marker` 是把 `'env` 钉成不变生命周期的幻影。

> 注意 `handles` 里装的是 `JoinHandle<()>`——线程返回**单元值** `()`，不是用户期望的 `T`。`T` 被另存他处（见 4.3）。这是理解整条 join 路径的钥匙。

`_marker: PhantomData<&'env mut &'env ()>` 是 Rust 社区公认的「让参数 `'env` 不变」的写法。`&'env mut &'env ()` 同时出现协变（外层 `&'a T`）与抗变（`&'a mut T`）位置，二者相互抵消，结果是**不变**。不变意味着编译器不能把 `Scope<'long>` 当 `Scope<'short>` 用、也不能反过来——这正是我们要的：共享状态里的句柄与结果都严格绑死在 `'env` 上，谁也别想偷偷改长改短。

#### 4.1.2 核心流程

`spawn` 的执行流程（极简）：

```
s.spawn(f)
   └─ self.builder()              // 得到一个 ScopedThreadBuilder（默认配置）
        └─ .spawn(f)              // 真正造线程（见 4.2）
             └─ .expect("...")    // 把 io::Result 转成「成功值或 panic」
```

`builder()` 几乎什么都没做，只是把 `self` 与一个崭新的 `thread::Builder::new()` 捆在一起。

#### 4.1.3 源码精读

`spawn` 方法本身只有三行有效逻辑：

```rust
pub fn spawn<'scope, F, T>(&'scope self, f: F) -> ScopedJoinHandle<'scope, T>
where
    F: FnOnce(&Scope<'env>) -> T,
    F: Send + 'env,
    T: Send + 'env,
{
    self.builder()
        .spawn(f)
        .expect("failed to spawn scoped thread")
}
```

[src/thread.rs:L258-L267](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L258-L267)：`Scope::spawn` 把构造委托给 builder，并用 `expect` 把 OS 层失败升级为 panic。

留意签名里的两个生命周期与两个 trait bound，它们是整章的语义契约：

- `'scope` 借自 `&'scope self`，因此返回的 `ScopedJoinHandle<'scope, T>` 的生命周期**不超过当前这次对 Scope 的借用**——这保证了句柄不会跑到 scope 结束之后还被人握着。
- 闭包 `F` 与返回值 `T` 都要求 `Send + 'env`：`Send` 才能跨线程移动；`'env` 表示它们可以**借用 scope 外的栈上数据**，这正是作用域线程的全部意义。

`builder()` 同样简短：

```rust
pub fn builder<'scope>(&'scope self) -> ScopedThreadBuilder<'scope, 'env> {
    ScopedThreadBuilder {
        scope: self,
        builder: thread::Builder::new(),
    }
}
```

[src/thread.rs:L282-L287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L282-L287)：`builder` 把 `Scope` 的引用与一个默认的 `std::thread::Builder` 组合成配置器。

`builder()` 之所以也带 `'scope` 借用，是为了让后续 `ScopedThreadBuilder::spawn` 返回的句柄同样受 `'scope` 约束——配置器本身只是个中转，最终句柄的生命周期上限仍是「对 Scope 的这次借用」。

#### 4.1.4 代码实践

**实践目标**：体会 `spawn` 与 `builder().spawn()` 在错误处理上的差别。

**操作步骤**：

1. 新建一个依赖 `crossbeam-utils` 的 binary crate。
2. 写下面两段对照代码（**示例代码**，非项目原有）：

```rust
use crossbeam_utils::thread;

fn main() {
    // (1) spawn：失败直接 panic
    thread::scope(|s| {
        let h = s.spawn(|_| 42);
        assert_eq!(h.join().unwrap(), 42);
    }).unwrap();

    // (2) builder().spawn()：拿到 io::Result，可自行处理
    thread::scope(|s| {
        let h = s.builder()
            .name("worker-1".to_string())
            .spawn(|_| {
                assert_eq!(std::thread::current().name(), Some("worker-1"));
                7
            })
            .unwrap(); // 这里仍可 unwrap，但你也可以 match
        assert_eq!(h.join().unwrap(), 7);
    }).unwrap();
}
```

**需要观察的现象**：两段都能通过；第二段里子线程读到的 `current().name()` 正是 `"worker-1"`。
**预期结果**：程序正常退出，无 panic。若你的环境无法创建线程（极少见），第 (1) 段会 panic，第 (2) 段可在 `match` 中自行降级。

#### 4.1.5 小练习与答案

**练习 1**：`Scope` 为什么要 `unsafe impl Sync for Scope<'_> {}`？它自己实现了哪些 trait？

> **答案**：`Scope` 内部全是 `Arc<Mutex<..>>` 与 `WaitGroup`（这些都是 `Sync`），外加一个零大小的 `PhantomData`。但 `PhantomData<&'env mut &'env ()>` 让编译器认为 `Scope` 含一个 `&'env mut ...` 引用，而 `&'env mut T` 默认 `!Sync`（可变共享不安全）。事实上 `Scope` 的可变状态全在 `Mutex` 保护下，共享是安全的，所以手动 `unsafe impl Sync`。注意它**只** impl 了 `Sync`，没有手动 impl `Send`——`Scope` 通过引用传递，不需要 `Send`。

**练习 2**：把 `s.spawn(|_| 42)` 改成 `s.spawn(|| 42)`（去掉闭包参数），能编译吗？为什么？

> **答案**：不能。签名要求 `F: FnOnce(&Scope<'env>) -> T`，闭包必须接受一个 `&Scope` 参数。这个参数正是给「嵌套 spawn」用的（见 4.3.5）。即使你不打算嵌套，也必须写出 `|_|`。

---

### 4.2 ScopedThreadBuilder：配置与 spawn 装箱

#### 4.2.1 概念说明

`ScopedThreadBuilder` 是一个**建造者（builder）**：先用 `name` / `stack_size` 链式配置，最后调用 `spawn` 真正起线程。它的结构很朴素：

```rust
pub struct ScopedThreadBuilder<'scope, 'env> {
    scope: &'scope Scope<'env>,
    builder: thread::Builder,
}
```

[src/thread.rs:L330-L333](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L330-L333)：builder 只是把「要往哪个 scope 里塞」与「标准库的 thread::Builder」放在一起。

`name` 与 `stack_size` 都是对 `self.builder` 的薄封装，返回 `self` 以支持链式调用。真正有意思的是 `spawn` 方法——它要解决一个看似无解的矛盾：

> 用户闭包 `f` 借用了 `'env` 的栈数据，但 `std::thread::Builder::spawn` 要求闭包是 `'static`。

#### 4.2.2 核心流程

`ScopedThreadBuilder::spawn` 的流程可以拆成 5 步：

```
1. 准备一个 SharedOption<T> result（用来存返回值）
2. 克隆一个 Scope（共享同一个 handles 列表与 wait_group），把它 move 进新线程
3. 把用户闭包 f 包成「返回 () 的新闭包 closure」：
     - 内层调用 f(&scope)
     - 把返回值塞进 result
4. 把 closure 装箱成 Box<dyn FnOnce() + Send + 'env>
   再 unsafe transmute 成 Box<dyn FnOnce() + Send + 'static>
5. 交给 self.builder.spawn(closure) 真正起 OS 线程
   把句柄包成 Arc<Mutex<Option<..>>> 推进 scope.handles
   返回 ScopedJoinHandle
```

第 3 步把「返回 `T` 的闭包」改造成「返回 `()` 的闭包」——这是为了让 `JoinHandle` 的类型固定为 `JoinHandle<()>`，从而句柄可以统一存进 `SharedVec`。返回值 `T` 走 `result` 这条独立通道。

第 4 步是全篇唯一的真正危险动作，下一节详细论证其安全性。

#### 4.2.3 源码精读

先看配置方法，它们都很短：

```rust
pub fn name(mut self, name: String) -> Self {
    self.builder = self.builder.name(name);
    self
}

pub fn stack_size(mut self, size: usize) -> Self {
    self.builder = self.builder.stack_size(size);
    self
}
```

[src/thread.rs:L357-L360](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L357-L360) 与 [src/thread.rs:L382-L385](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L382-L385)：`name` 与 `stack_size` 直接转发给标准库的 `thread::Builder`，链式返回 `Self`。

接下来是核心的 `spawn`。我们分段读。**先看「克隆 Scope + 包装闭包」**：

```rust
let result = SharedOption::default();                       // ① 存返回值

let (handle, thread) = {
    let result = Arc::clone(&result);                       // 闭包要捕获 result 的副本

    let scope = Scope::<'env> {                             // ② 克隆一个 Scope
        handles: Arc::clone(&self.scope.handles),           //    共享同一份句柄列表
        wait_group: self.scope.wait_group.clone(),          //    clone 让 WaitGroup 计数 +1
        _marker: PhantomData,
    };

    let handle = {
        let closure = move || {
            let scope: Scope<'env> = scope;                 // ③ 把 scope 移入闭包
            let res = f(&scope);                            //    调用用户闭包
            *result.lock().unwrap() = Some(res);            //    存返回值
        };
        // ... 见下方「装箱」
```

[src/thread.rs:L430-L455](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L430-L455)：创建结果槽、克隆 Scope、把用户闭包 `f` 包成返回 `()` 的 `closure`。

注意第 ② 步：克隆出的 `scope` **复用了父 scope 的 `handles` 列表**（`Arc::clone`）和**同一个 `WaitGroup`**（`clone` 让计数 +1）。这个克隆的 scope 会被 move 进新线程，并通过 `f(&scope)` 交给用户的闭包——这就是「在子线程内还能继续 `s.spawn(...)`」的实现基础（见 4.3.5 嵌套）。

**最关键的「装箱 + transmute」**：

```rust
// Allocate `closure` on the heap and erase the `'env` bound.
let closure: Box<dyn FnOnce() + Send + 'env> = Box::new(closure);
let closure: Box<dyn FnOnce() + Send + 'static> =
    unsafe { mem::transmute(closure) };

// Finally, spawn the closure.
self.builder.spawn(closure)?
```

[src/thread.rs:L457-L463](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L457-L463)：先把闭包装箱成 trait 对象擦除具体类型，再用 `transmute` 把 trait 对象的生命周期参数从 `'env` 改写成 `'static`，最后交给标准库起线程。

这里有两层「擦除」，不要混淆：

| 步骤 | 擦除的东西 | 是否 unsafe | 为什么安全 |
| --- | --- | --- | --- |
| `Box::new(closure)` 变成 `Box<dyn FnOnce() + Send + 'env>` | 闭包的**具体类型** | 否（标准 trait 对象转型） | 闭包本就 `Send + 'env`，转型只是丢掉静态类型信息 |
| `transmute` 成 `Box<dyn FnOnce() + Send + 'static>` | trait 对象的**生命周期参数** `'env → 'static` | **是** | 见下方安全性论证 |

**`transmute` 的安全性论证**：`std::thread::Builder::spawn` 要求 `F: 'static`，是因为标准库无法保证子线程何时结束、会不会活到引用数据被释放之后。但在本 crate 里，`scope()` 入口（u4-l1）做出了**强承诺**：

1. 在 `scope` 返回前，`drop(scope.wait_group); wg.wait();` 一定会等到所有子 Scope 销毁（WaitGroup 计数归零）。
2. 紧接着 `scope.handles.drain(..)` 会 join 所有未被手动 join 的线程。

也就是说——**只要被借用 `'env` 的数据还活着，线程就一定已经被 join 完毕**。把闭包当成 `'static` 是「骗」编译器，但运行时的 join 承诺兜底，使得它实际上比 `'static` 更严格。这就是这处 `unsafe` 成立的全部依据。

> 为什么不直接让 `std::thread::spawn` 接受非 `'static`？因为标准库 API 无法表达「我保证会 join」这种跨函数的、依赖运行时行为的承诺。作用域线程的全部价值就在于用 `scope()` 的结构把这个承诺**编码进类型系统**。

**收尾：句柄入表、返回**：

```rust
let thread = handle.thread().clone();
let handle = Arc::new(Mutex::new(Some(handle)));
(handle, thread)
};

// Add the handle to the shared list of join handles.
self.scope.handles.lock().unwrap().push(Arc::clone(&handle));

Ok(ScopedJoinHandle {
    handle,
    result,
    thread,
    _marker: PhantomData,
})
```

[src/thread.rs:L466-L480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L466-L480)：把句柄包成 `Arc<Mutex<Option<JoinHandle<()>>>>`，**同时**推进共享列表与返回给用户的 `ScopedJoinHandle`——两者持有同一个 `Arc`，所以「scope 自动 join」和「用户手动 join」只能有一个真正取走句柄（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：通过修改 `stack_size` 直观感受 builder 的配置效果。

**操作步骤**：

1. 在 scope 内用一个**故意递归很深**的闭包消耗栈空间（**示例代码**）：

```rust
use crossbeam_utils::thread;

fn recurse(n: u64) -> u64 {
    if n == 0 { 1 } else { n * recurse(n - 1) }
}

fn main() {
    thread::scope(|s| {
        // 默认栈大小通常 2MiB 或 8MiB，递归会很深才溢出
        let h = s.builder()
            .stack_size(64 * 1024)   // 故意只给 64KiB
            .spawn(|_| recurse(100_000))
            .unwrap();
        match h.join() {
            Ok(v) => println!("结果 {v}"),
            Err(_) => println!("子线程溢出栈 / panic"),
        }
    }).unwrap();
}
```

**需要观察的现象**：把 `stack_size` 从 `64 * 1024` 调大到 `8 * 1024 * 1024`，子线程从「栈溢出 panic」变为「正常返回」。
**预期结果**：小栈时大概率打印 `子线程溢出栈 / panic`；大栈时打印 `结果 ...`。具体阈值取决于平台，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 ③ 步的 `let scope: Scope<'env> = scope;` 删掉、直接在闭包里用 `f`，会出现什么问题？

> **答案**：删掉这行后，用户闭包 `f` 就拿不到 `&Scope` 参数了——`f` 的签名是 `FnOnce(&Scope<'env>) -> T`，必须传入一个 scope。这行赋值只是把外层构造好的 `scope`（捕获进闭包的那个）显式标注上 `'env` 生命周期再交给 `f`。没有它，嵌套 spawn 就无从谈起。

**练习 2**：`transmute` 为什么写在「装箱之后」而不是「装箱之前」？

> **答案**：闭包的具体类型在编译期由其捕获的变量决定，每个 `f` 的类型都不同，无法统一写成一个 `transmute` 目标类型。装箱成 `Box<dyn FnOnce() + Send + 'env>` 后，类型被擦除成统一的 trait 对象（fat pointer），此时 `transmute` 到 `Box<dyn FnOnce() + Send + 'static>` 才是「两个同样大小的 fat pointer 之间的重解释」，符合 `transmute` 的等长约束。

**练习 3**：`name` 文档里说「The name must not contain null bytes (`\0`)」。这与 builder 的哪段实现有关？

> **答案**：与 `ScopedThreadBuilder` 本身无关——它只是把 `name` 透传给 `std::thread::Builder::name`。null 字节的检查发生在标准库内部（构造 OS 线程名时），`ScopedThreadBuilder::spawn` 的文档「Panics if a thread name was set and it contained null bytes」就是承接自 `std::thread::Builder::spawn` 的行为。

---

### 4.3 ScopedJoinHandle 与 SharedOption 结果存储

#### 4.3.1 概念说明

`ScopedJoinHandle<'scope, T>` 是 `spawn` 返回给用户的句柄，提供三件事：

- `join(self)`：等待线程结束，取回返回值 `T`（失败返回 panic 信息）。
- `thread(&self)`：拿到底层 `std::thread::Thread` 的引用（可用于 `park`/`unpark` 等）。
- 通过 `unsafe impl Send/Sync`，句柄本身可在多线程间传递。

它的字段定义揭示了「结果存储」的设计：

```rust
pub struct ScopedJoinHandle<'scope, T> {
    handle: SharedOption<thread::JoinHandle<()>>,   // OS 线程句柄
    result: SharedOption<T>,                         // 用户返回值
    thread: thread::Thread,                          // 线程引用
    _marker: PhantomData<&'scope ()>,
}
```

[src/thread.rs:L490-L502](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L490-L502)：句柄与返回值**分两路**存储。`handle` 与 4.2 中推进共享列表的是**同一个 `Arc`**；`result` 与闭包内写入的是**同一个 `Arc`**。

为什么要「分两路」？因为：

1. 句柄要被**两方共享**——scope 的自动 join（u4-l1 里 `scope.handles.drain(..)`）和用户的手动 `join()` 都可能取走它。所以它必须是 `Arc<Mutex<Option<_>>>`，谁先 `take()` 谁负责 join。
2. 线程返回的是 `()`（见 4.2 的闭包包装），`T` 只能走独立的 `result` 通道，且同样要 `Arc<Mutex<Option<T>>>`，因为写在线程里、读在 `join()` 里。

#### 4.3.2 核心流程

`join()` 的流程：

```
join(self)
  ├─ handle.lock().take().unwrap()      // 取出 OS 句柄（一定还在，见论证）
  ├─ handle.join()                       // 阻塞等待 OS 线程结束，返回 Result<(), panic>
  └─ .map(|()| result.lock().take().unwrap())
                                         // 线程成功 → 取出用户返回值 T
                                         // 线程 panic  → 透传 Err(panic)
```

关键点：**只有线程正常返回时，`result` 里才一定有 `Some(T)`**。因为闭包的写入语句 `*result.lock().unwrap() = Some(res);` 在 `f(&scope)` 之后执行；若 `f` panic，写入不会发生，`result` 保持 `None`。此时 `handle.join()` 已经返回 `Err`，`.map` 短路，永远不会去碰 `result.lock().take().unwrap()`，所以「线程 panic 时 `result` 为 `None`」不会引发二次 panic。

#### 4.3.3 源码精读

`join` 的实现非常紧凑：

```rust
pub fn join(self) -> thread::Result<T> {
    // Take out the handle. The handle will surely be available because the root scope waits
    // for nested scopes before joining remaining threads.
    let handle = self.handle.lock().unwrap().take().unwrap();

    // Join the thread and then take the result out of its inner closure.
    handle
        .join()
        .map(|()| self.result.lock().unwrap().take().unwrap())
}
```

[src/thread.rs:L533-L542](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L533-L542)：`join` 取出共享句柄、等待 OS 线程、再从结果槽取出 `T`。

逐字解读那行注释「The handle will surely be available because the root scope waits for nested scopes before joining remaining threads」：

- 在 u4-l1 的 `scope()` 里，自动 join 发生在 `drop(scope.wait_group); wg.wait();` **之后**。
- `wg.wait()` 要等所有子 Scope 的 `WaitGroup` 计数归零——而子 Scope 的 drop 会减少计数。
- 也就是说：**只要还有任何子线程/子 Scope 没结束，根 scope 就不会进入自动 join 阶段去 `take()` 句柄**。
- 因此用户在 `f` 内部调用 `h.join()` 时，根 scope 还没动过句柄表，`take().unwrap()` 必然成功。

这是「作用域线程的 join 顺序」与「WaitGroup 栅栏」精密配合的结果。如果你跳过 `wg.wait()` 直接 join，就可能出现「scope 和用户同时取句柄」的竞争，`take()` 会拿到 `None` 而 panic。

`thread()` 方法只是个访问器：

```rust
pub fn thread(&self) -> &thread::Thread {
    &self.thread
}
```

[src/thread.rs:L556-L558](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L556-L558)：返回底层 `Thread` 的引用，可用于线程命名、`park`/`unpark` 等。

最后注意句柄的 trait 实现：

```rust
unsafe impl<T> Send for ScopedJoinHandle<'_, T> {}
unsafe impl<T> Sync for ScopedJoinHandle<'_, T> {}
```

[src/thread.rs:L483-L484](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L483-L484)：手动声明 `Send + Sync`，使得句柄可在不同线程间传递与共享。安全性来自：内部状态全在 `Mutex` 保护下，且 `'scope` 生命周期保证句柄不会逃出 scope。

#### 4.3.4 代码实践

**实践目标**：验证 `join()` 能取回返回值，并观察 panic 子线程的 `join` 行为。

**操作步骤**（**示例代码**）：

```rust
use crossbeam_utils::thread;

fn main() {
    let outcome = thread::scope(|s| {
        let h1 = s.spawn(|_| {
            println!("正常线程");
            42
        });
        let h2 = s.spawn(|_| {
            panic!("子线程故意 panic");
        });

        assert_eq!(h1.join().unwrap(), 42);     // 取回返回值
        assert!(h2.join().is_err());            // panic → Err
        "scope 内逻辑完成"
    });
    assert_eq!(outcome.unwrap(), "scope 内逻辑完成");
}
```

**需要观察的现象**：`h1.join()` 返回 `Ok(42)`；`h2.join()` 返回 `Err(...)`，但**不会**让主线程跟着 panic（panic 被封装成 `Err`）。`scope()` 整体返回 `Ok("scope 内逻辑完成")`，因为所有**手动 join 过的**线程不会再次进入 u4-l1 的 panic 收集。
**预期结果**：终端先打印 `正常线程` 与一条来自 `h2` 的 panic 栈，最后 `outcome` 是 `Ok(...)`，断言全部通过。**待本地验证** panic 的具体打印格式。

> 思考点：把 `let h2 = ...; assert!(h2.join().is_err());` 这两行注释掉，让 `h2` 不被手动 join。此时 `scope()` 会自动 join 它并把 panic 收集进 `Err`——`outcome` 将变成 `Err(Vec<Box<dyn Any>>)`。这就是 u4-l1 讲过的「自动 join + panic 汇总」与本讲「手动 join」的边界。

#### 4.3.5 嵌套 spawn：为什么要把 Scope 当参数传

`thread.rs` 顶部的文档专门用一节讲「Nesting scoped threads」。先看会**编译失败**的写法（文件里就是 `compile_fail` 示例）：

```rust
thread::scope(|s| {
    s.spawn(|_| {
        s.spawn(|_| println!("nested thread"));   // ❌ 借用了外层 s
    });
});
```

[src/thread.rs:L87-L97](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L87-L97)：失败示例。`s` 是 `thread::scope` 的局部变量，它**活在自己的 scope 调用里**；子线程闭包去借用 `s`，但 `s` 的生命周期短于子线程可能存活的时间，借用检查器拒绝。

正确写法是利用「每个子线程的闭包都会收到一个 `&Scope` 参数」：

```rust
thread::scope(|s| {
    s.spawn(|s| {                                  // 👈 注意参数 |s|
        s.spawn(|_| println!("nested thread"));    // ✅ 用收到的 s，不是外层 s
    });
}).unwrap();
```

[src/thread.rs:L102-L112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L102-L112)：正确示例。内层闭包用一个**新的参数 `s`**（由 4.2 的克隆 Scope 提供）来继续 spawn。

回看 4.2.3 的第 ②③ 步：每次 spawn 都会克隆一个 `Scope` move 进新线程，并通过 `f(&scope)` 把它作为参数交给用户闭包。这个参数 `s` 的生命周期与**当前线程**绑定，而不是与外层 `scope()` 调用绑定，所以借用它能通过检查。同时它共享父 scope 的 `handles` 与 `wait_group`，因此嵌套线程也会被根 scope 正确 join——这正是 u4-l1 里 `WaitGroup` 栅栏需要追踪「所有子 Scope」的原因。

#### 4.3.6 小练习与答案

**练习 1**：`ScopedJoinHandle` 为什么不直接存 `JoinHandle<T>`，而要拆成 `JoinHandle<()>` + `SharedOption<T>`？

> **答案**：因为同一个 `JoinHandle` 要被「scope 自动 join」和「用户手动 join」**两方**共享（`Arc<Mutex<Option<_>>>`，先 take 者负责 join）。如果存 `JoinHandle<T>`，那么自动 join 路径（u4-l1 里 `handles.drain(..)`）也会消费掉 `T`，但自动 join 时用户根本拿不到这个 `T`，会被丢掉。把线程返回值改成 `()`、`T` 走独立的 `SharedOption<T>` 通道后，`JoinHandle<()>` 可以自由地被任一方 take/join，而 `T` 只在用户调用 `ScopedJoinHandle::join()` 时才被取出；若用户没手动 join，`T` 随 `Arc` drop 而正常释放，不会泄漏也不会被错误消费。

**练习 2**：`join()` 里 `self.result.lock().unwrap().take().unwrap()` 的第二个 `unwrap` 在什么情况下可能 panic？

> **答案**：仅在「`handle.join()` 返回 `Ok(())` 但 `result` 却是 `None`」时才会 panic。而闭包实现保证：只有 `f(&scope)` 不 panic、正常返回 `res` 后才会执行 `*result.lock() = Some(res)`。所以只要 `handle.join()` 成功，`result` 一定是 `Some`。换言之在当前实现下这第二个 `unwrap` 不会 panic——它是一个由「闭包写入顺序」保证的不变量。

**练习 3**：把 4.3.4 里的 `h2` 改成在闭包里 `loop {}` 死循环，然后**不**手动 join 它，直接让 `scope()` 返回。会发生什么？

> **答案**：`scope()` 在 `wg.wait()` 之后会执行 `scope.handles.drain(..)` 自动 join 残留线程，于是 `scope()` 会**永久阻塞**在那个 `h2` 的 join 上。这印证了「scope 返回前一定 join 所有子线程」是硬承诺——子线程不结束，scope 就回不来。设计上这是正确行为（保证借用安全），但提醒使用者：作用域线程里不要放无法终止的任务。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「双层并发求和」任务：

**需求**：给定一个栈上 `Vec<i32>`，把它切成 2 份，每份交给一个外层线程；每个外层线程内部再 spawn 2 个内层线程各算半份的局部和，外层线程汇总自己的两个内层结果后返回；主线程在 scope 返回后把两个外层结果加起来。

**参考实现**（**示例代码**）：

```rust
use crossbeam_utils::thread;

fn main() {
    let data: Vec<i32> = (1..=100).collect();
    let mid = data.len() / 2;
    let half = mid / 2;

    let total = thread::scope(|s| {
        // 两个外层线程，各处理 data 的一半
        let handles: Vec<_> = [0, mid].iter().map(|&start| {
            // 注意闭包参数 |s|：用于在内层继续 spawn
            s.builder()
                .name(format!("outer-{start}"))
                .spawn(move |s| -> i32 {
                    let chunk = &data[start..start + mid];
                    // 内层再切两半，各起一个线程
                    let inner: Vec<_> = [0, half].iter().map(|&off| {
                        s.spawn(move |_| -> i32 {
                            chunk[off..off + half].iter().sum::<i32>()
                        })
                    }).collect();

                    inner.into_iter().map(|h| h.join().unwrap()).sum()
                })
                .unwrap()
        }).collect();

        handles.into_iter().map(|h| h.join().unwrap()).sum::<i32>()
    }).unwrap();

    assert_eq!(total, (1..=100).sum::<i32>());
    println!("total = {total}");
}
```

**这道综合题考察了本讲的全部要点**：

- **4.1**：`s.builder().name(...).spawn(...)` 链式配置，返回 `ScopedJoinHandle`。
- **4.2**：外层闭包 `move |s|` 借用栈上 `data`（非 `'static`），靠 `transmute` 擦除 `'env` 才能传入 `std::thread`。
- **4.3**：内层 `s.spawn(...)` 用的是**作为参数传入的 `s`**（嵌套 spawn），并通过 `h.join().unwrap()` 取回 `i32` 返回值。
- **与 u4-l1 的衔接**：scope 返回时，所有未手动 join 的线程（本例全部手动 join 了）会被自动 join，`AbortOnPanic` 守卫封堵逃逸窗口。

**需要观察的现象**：程序打印 `total = 5050`；线程名 `outer-0` / `outer-50` 可在调试器或 `current().name()` 中看到。
**预期结果**：断言通过，正常退出。**待本地验证**：若想观察交错，可在内层闭包里加 `println!`。

## 6. 本讲小结

- `Scope` 持有三样东西：共享句柄表 `SharedVec<SharedOption<JoinHandle<()>>>`、`WaitGroup`、以及把 `'env` 钉成**不变**生命周期的 `PhantomData<&'env mut &'env ()>`。它只提供 `spawn`（失败 panic）与 `builder`（失败可恢复）两个入口，且 `spawn` 只是 `builder().spawn().expect(...)` 的糖。
- `ScopedThreadBuilder` 是真正造线程的地方。它先把「返回 `T` 的用户闭包」包成「返回 `()`、把 `T` 写入 `SharedOption<T>` 的内层闭包」，再装箱成 `Box<dyn FnOnce() + Send + 'env>`，最后 **`unsafe transmute` 成 `'static`** 交给 `std::thread::Builder::spawn`。这处 `unsafe` 的安全性由 `scope()` 的 join 承诺兜底。
- 句柄被包成 `Arc<Mutex<Option<JoinHandle<()>>>>` **同时**推进共享列表与返回值——所以「scope 自动 join」和「用户手动 join」共享同一把锁，先 `take()` 者负责 join；`WaitGroup` 栅栏保证二者不会竞争取空句柄。
- `ScopedJoinHandle` 把「OS 句柄」与「返回值 `T`」**分两路**存进两个 `SharedOption`，因为线程返回 `()`。`join()` 先 take+join 句柄，成功后再 take 结果；线程 panic 时 `result` 保持 `None`，但 `.map` 短路不会触发二次 panic。
- **嵌套 spawn** 必须使用「作为参数传入的 `&Scope`」，而不是借用外层 `scope()` 的 `s`——后者生命周期不够长。这个参数就是 `ScopedThreadBuilder::spawn` 克隆并 move 进线程的那个 `Scope`，它共享父 scope 的句柄表与 WaitGroup。
- 三个类型把 u4-l1 的「join 承诺」落到了具体的字段与 `unsafe` 上：`'env` 不变性 + `transmute` + `SharedOption` 共同构成了「借用栈数据但能安全 join」的类型层证据。

## 7. 下一步学习建议

到这里，`src/thread.rs` 的核心实现（`scope()` + `Scope` + `ScopedThreadBuilder` + `ScopedJoinHandle`）已经全部讲完。建议你接下来：

1. **阅读 `tests/thread.rs`**（在 u5-l4 会专门讲测试策略），看官方测试如何为「panic 汇总」「嵌套 spawn」「句柄 join 顺序」写不变量断言，对照本讲的结论验证你的理解。
2. **对比 `std::thread::scope`**（Rust 1.63 起稳定）。文档里明确写了 crossbeam 的 `scope` 是 **soft-deprecated**（见 [src/thread.rs:L131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L131)）。试着把综合实践改成标准库版本，体会有哪些 API 差异（例如标准库没有 `ScopedThreadBuilder` 的等价物，而是直接 `Builder::spawn_scoped`）。
3. **进入第五单元（advanced）**：u5-l1 会集中剖析 `atomic_cell.rs` 的 `unsafe` 安全性论证，与本讲的 `transmute` 论证方法是同一类「写出安全性前提清单」的训练；u5-l3 会讲到 `loom` 抽象层——届时你会理解为什么 `thread.rs` 在 `#[cfg(not(crossbeam_loom))]` 下才编译（loom 无法建模 OS 线程）。
4. **动手扩展**：尝试给本讲的综合实践加上「错误注入」——让某个内层线程 panic，观察 `handle.join().is_err()` 与 `scope()` 返回 `Err(Vec<...>)` 的边界，巩固「手动 join vs 自动 join」对 panic 收集的影响。
