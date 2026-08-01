# Deferred 后台并行与 defer RAII

## 1. 本讲目标

学完本讲后，你应当能够：

- 读懂 `Deferred<T>` 如何借助 rayon 线程池把一个耗时任务"扔到后台"，再通过 `wait` 安全取回结果。
- 解释 `wait` 为什么用 `rayon::yield_now` 做"协作式等待"，并理解它为何能让代码在单线程 / WASM 平台上也不死锁。
- 用 `defer()` 函数把"临时修改某个值、作用域结束自动还原"写成 RAII 模式，并理解其返回的 `DeferHandle` 在 `Drop` 时如何可靠地触发一次性回调。
- 理解 `Drop` 里用 `Option<F>` + `std::mem::take` 调用 `FnOnce` 闭包这一经典 Rust 写法。

本讲是专家层的第三篇，承接 u1-l3（`singleton!` 与 `LazyLock` 的惰性初始化），也和上一篇 u3-l2 的 `Protected`（用 newtype 在类型系统层面强制访问纪律）形成对照：本讲的 `defer()` 同样是"用值的生命周期来表达纪律"，只不过纪律从"访问需说明理由"变成了"离开作用域必须还原状态"。

## 2. 前置知识

在进入源码前，先建立四个直觉概念。

**惰性初始化（lazy init）。** 一个值在被真正需要之前不计算，第一次访问时才计算并缓存。u1-l3 讲过 `LazyLock` 实现"全局唯一、惰性初始化的 `&'static T`"。本讲的 `Deferred` 也做惰性初始化，但计算发生在**另一个线程**上。

**`OnceCell<T>`。** `once_cell::sync::OnceCell<T>` 是一个"最多被赋值一次"的容器：初始为空，可以写入一次，之后永远只读。它的关键方法有三个——

| 方法 | 行为 |
|------|------|
| `get()` | 立即返回 `Option<&T>`，已赋值则 `Some`，否则 `None`，**绝不阻塞** |
| `get_or_init(f)` | 若空则用 `f` 填充，返回 `&T`；已有值则原样返回（幂等） |
| `wait()` | 阻塞当前线程，直到容器**被其他线程**填上值 |

记住这三者的区别，是理解本讲 `wait` 设计的关键。

**rayon 线程池。** [rayon](https://docs.rs/rayon) 是 Rust 的数据并行库，维护一个工作线程池。`rayon::spawn(closure)` 把一个任务投递到池子里"尽快执行"，立即返回、不等待。`rayon::yield_now()` 则是"让出当前工作线程"：调度器可能借此去执行池子里排队的其他任务，包括被 `spawn` 投递的任务。

**RAII（Resource Acquisition Is Initialization）。** Rust 的核心范式：资源的生命周期绑定到一个值的生命周期，值被销毁（`Drop`）时自动释放资源。本讲的 `defer()` 把"还原状态"这个动作绑定到一个临时值的 `Drop` 上，从而保证函数无论从哪条路径返回（包括提前 `return`、出错）都一定会还原。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/deferred.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/deferred.rs) | `Deferred<T>` 类型：把闭包扔到 rayon 后台线程惰性执行，可 `wait` 取回。仅 54 行，是本讲主角之一。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | 内联定义 `defer()` 函数与私有 `DeferHandle` 结构（函数体内嵌定义），是本讲另一主角。`lib.rs` 第 8 行 `mod deferred;` 与第 20 行 `pub use self::deferred::Deferred;` 完成"私有 mod + 选择性 pub use"的导出。 |

依赖层面，[Cargo.toml](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml) 引入了 `once_cell`（提供 `OnceCell`）与 `rayon`（提供线程池与 `yield_now`），二者是本讲的两根支柱。

---

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：4.1 讲 `Deferred` 怎么把任务送进后台；4.2 讲怎么取回结果且兼容单线程平台；4.3 讲 `defer()` 的 RAII 设计意图与 API 形状；4.4 讲 `DeferHandle` 在 `Drop` 时调用 `FnOnce` 的底层技巧。

### 4.1 Deferred：把闭包送进后台线程惰性求值

#### 4.1.1 概念说明

设想这样一个场景：编译 Typst 文档时，某个耗时结果（比如加载并解析一个字体文件）迟早要用，但不必阻塞当前调用。我们希望"尽早开始算、晚点再来取"——也就是**预计算（precomputation）**。`Deferred<T>` 正是为此设计：它把一个 `FnOnce() -> T` 闭包扔到 rayon 的后台线程池里立即开始执行，同时给你一个可以廉价克隆、稍后 `wait` 取结果的句柄。

它和 u1-l3 的 `singleton!` 都做惰性初始化，区别在于：`singleton!` 在**第一次被访问的线程**上同步计算；`Deferred` 在**另一个线程**上异步计算，调用方继续干别的活，实现真正的并行。

#### 4.1.2 核心流程

`Deferred::new(f)` 的执行流程：

1. 创建一个共享的"结果容器" `Arc<OnceCell<T>>`。
2. 克隆一份 `Arc`，连同闭包 `f` 一起 `move` 进一个 `rayon::spawn` 任务。
3. rayon 线程池接管该任务，在某个后台工作线程上调用 `OnceCell::get_or_init(f)` 算出 `T` 并写入容器。
4. `new` 立即返回 `Deferred`，此时容器可能仍是空的（后台还没跑完）。
5. 调用方拿到 `Deferred` 后，任意时刻可调用 `wait()` 取回 `&T`。

伪代码：

```text
fn new(f):
    cell = Arc::new(OnceCell::空)
    后台任务(cell.clone(), f)       # rayon::spawn
    return Deferred(cell)           # 立即返回，不等后台
```

由于容器是 `Arc` 共享的，`Deferred` 可以被 `Clone`（仅 `clone` 了 `Arc`），多个副本 `wait` 到的是同一个值。

#### 4.1.3 源码精读

`Deferred` 的结构体定义极其简洁，整个类型就是一个被 `Arc` 包裹的 `OnceCell`：

[src/deferred.rs:L5-L8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/deferred.rs#L5-L8) —— 文档注释说明"在另一个线程上惰性执行、可被等待"，结构体字段 `Arc<OnceCell<T>>` 是唯一的存储。

构造函数 `new`：

[src/deferred.rs:L15-L27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/deferred.rs#L15-L27) —— 关键三步：第 19 行建 `Arc<OnceCell>`；第 20 行克隆 `Arc`；第 21-25 行 `rayon::spawn` 把克隆和闭包一起送进后台，调用 `get_or_init(f)`。

注意第 24 行用的是 `get_or_init` 而非 `set`/`init`。源码注释（第 22-23 行）解释：这是为了"避免在外部已经设过值时 panic"——`get_or_init` 是幂等的（已有值就原样返回，不调用 `f`）。这是防御性写法：即便将来有人通过某种途径提前写入了容器，后台任务也不会 panic。

类型约束 `T: Send + Sync + 'static` 与 `F: FnOnce() -> T + Send + Sync + 'static`（第 10、17 行）保证闭包和结果都能安全地跨越线程边界、且存活足够久（后台线程的生命周期不依附于当前栈帧）。

`Clone` 实现只是 `clone` 了 `Arc`：

[src/deferred.rs:L49-L53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/deferred.rs#L49-L53) —— 因此 `Deferred` 廉价可克隆，所有副本共享同一个结果容器，`wait` 拿到的是同一个 `&T`。

#### 4.1.4 代码实践

**实践目标：** 验证 `Deferred::new` 真的把计算送到了后台，且 `wait` 能取回结果。

**操作步骤：** 在一个依赖 `typst-utils` 的临时项目中写下如下示例代码，并运行（`cargo run`）。

```rust
// 示例代码（非项目原有代码）
use typst_utils::Deferred;

fn main() {
    let start = std::time::Instant::now();
    let deferred = Deferred::new(|| {
        std::thread::sleep(std::time::Duration::from_millis(300));
        21 * 2
    });
    // 此处后台线程正在算 300ms；主线程可在此期间做别的事
    println!("构造完成，已耗时 {:?}", start.elapsed());
    let result = deferred.wait();
    println!("wait 取回 {}，总耗时 {:?}", result, start.elapsed());
}
```

**需要观察的现象：** "构造完成"那一行的耗时应当远小于 300ms（因为后台已经开始算，构造立即返回）；"wait 取回"的总耗时接近 300ms（等待后台算完）。

**预期结果：** 打印形如 `构造完成，已耗时 1.xx ms` 与 `wait 取回 42，总耗时 300.xx ms`。

**待本地验证：** 具体毫秒数取决于机器与 rayon 线程池调度，请以本地实测为准。

#### 4.1.5 小练习与答案

**练习 1：** 如果把第 4.1.4 示例里闭包的返回类型从 `i32` 改成 `Rc<i32>`，能编译通过吗？为什么？

**答案：** 不能。`Rc` 不是 `Send + Sync`，而 `new` 要求 `T: Send + Sync + 'static`，编译器会直接拒绝。要跨线程共享所有权应改用 `Arc`。

**练习 2：** 同一个 `Deferred` 被 `clone` 出两份，对两份分别 `wait`，得到的是同一个值还是两份拷贝？

**答案：** 同一个值。`Clone` 只复制了内部 `Arc`，底层 `OnceCell` 是共享的，`wait` 返回的 `&T` 指向同一处内存。

---

### 4.2 wait：协作式等待与单线程 / WASM 兼容

#### 4.2.1 概念说明

`wait` 看似简单——"等后台算完返回结果"——但它藏着一个关键工程问题：**在单线程平台（尤其是 WASM）上如何避免死锁？**

在多线程平台上，直接调用 `OnceCell::wait()` 阻塞当前线程即可，因为后台任务跑在另一个 CPU 线程上，迟早会写满容器唤醒我们。但在 WASM 这类**单线程**目标上，rayon 退化为"所有任务在同一个线程上轮流执行"（协作式调度）。如果此时还粗暴阻塞 `wait`，后台任务根本没机会运行——它和我们在同一条船上，船被我们占着——于是死锁。

`Deferred::wait` 的解法是"先协作让出，再阻塞兜底"：趁等待的机会，主动 `yield` 让 rayon 调度器去把后台任务跑完，绝大多数情况下 `yield` 回来时结果已经就绪；只有当让出也没有任务可执行时，才退化到阻塞 `wait`。

#### 4.2.2 核心流程

`wait(&self) -> &T` 的流程：

1. **快速路径**：`OnceCell::get()` 立即试探，若已有值直接返回，**不 yield**，零开销。
2. **协作等待循环**：反复调用 `rayon::yield_now()`。每次让出期间若 rayon 执行了别的任务（包括我们 `spawn` 的后台任务），就继续让出；直到某次让出"没有执行到任何任务"（空闲），说明后台任务大概率已完成。
3. **阻塞兜底**：调用 `OnceCell::wait()`，确保即便协作等待没等到也最终能拿到值。

伪代码：

```text
fn wait():
    if 容器已有值: return 该值          # 快速路径
    while yield_now 执行了别的任务:    # 协作让出，让后台有机会跑
        continue
    return 容器.wait()                  # 阻塞兜底
```

为什么循环条件是"执行了别的任务才继续"？因为只要每次让出都实际推动了其他任务（很可能就是我们的后台计算），就值得继续让出；一旦让出却没有任何任务可跑（空闲），说明能做的都做完了，此时要么后台已完成（进入 `wait` 立刻返回），要么真的无事可做（`wait` 真正阻塞）。

#### 4.2.3 源码精读

快速路径是显式的 `get`，不触碰 rayon：

[src/deferred.rs:L34-L39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/deferred.rs#L34-L39) —— 第 37 行 `self.0.get()` 是 `OnceCell` 的非阻塞查询；注释（第 35-36 行）明确"已有值时不该 yield"，所以快速路径绝不进入调度器。

协作等待循环：

[src/deferred.rs:L43-L45](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/deferred.rs#L43-L45) —— 第 43 行 `while let Some(rayon::Yield::Executed) = rayon::yield_now() {}`：`rayon::yield_now()` 让出当前工作线程，返回值反映这次让出是否执行了任务；`Some(Yield::Executed)` 表示执行了，则继续循环。注释（第 41-42 行）点明了动机——为单线程平台（WASM）兼容而 yield，给后台值"一个计算的机会"。

第 45 行 `self.0.wait()` 是 once_cell 的阻塞等待，作为最后兜底。

这条 `yield_now` 设计在 Typst 仓库里有真实影响：测试代码注释曾指出，正是 `Deferred` 的 `yield` 行为，使得在并行测试里用 `par_bridge`（而非 `par_iter`）来避免 PDF 导出阶段的栈溢出——可见 `wait` 的"协作让出"会在调用栈上叠加 rayon 任务，是个值得注意的副作用（见仓库 `tests/src/tests.rs:154-156` 的相关注释）。

#### 4.2.4 代码实践

**实践目标：** 体会"快速路径"的存在——后台任务很快时 `wait` 几乎不进入 yield 循环。

**操作步骤：** 修改 4.1.4 的示例，在 `Deferred::new` 之后**先睡 500ms 再 `wait`**：

```rust
// 示例代码
let deferred = Deferred::new(|| { std::thread::sleep(Duration::from_millis(50)); 7 });
std::thread::sleep(Duration::from_millis(500));   // 故意等久一点
println!("{}", deferred.wait());                   // 此刻后台早已完成
```

**需要观察的现象：** `wait` 调用几乎瞬间返回，因为进入 `wait` 时容器已被后台填满，命中第 37 行的快速路径，不会 yield。

**预期结果：** `wait` 本身的耗时为微秒级。

**待本地验证：** 可在 `wait` 前后各打印一次 `Instant::now()` 来测耗时。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `wait` 不直接写 `self.0.wait()` 一行，而要先 `yield`？

**答案：** 为了单线程平台（WASM）兼容。在这些平台上 `spawn` 的任务排队在同一线程，直接阻塞 `wait` 会死锁；先 `yield` 让调度器有机会把后台任务跑完。

**练习 2：** 假如 rayon 线程池里任务排得很满，`wait` 的协作循环会不会一直空转不返回？

**答案：** 不会。循环每次 `yield_now` 若执行了任务就继续——这正是我们想要的（让后台跑）；当池子空闲（返回非 `Executed`）时退出循环，进入第 45 行的阻塞 `wait` 兜底，最终一定返回。多线程平台上后台任务跑在独立线程，`wait` 也一定能等到它写满容器。

---

### 4.3 defer()：用 RAII 实现"临时修改-自动还原"

#### 4.3.1 概念说明

另一个常见场景：你需要**临时**改变某个值的状态，做完一件事后**必须**把它还原。典型例子是图形栈的 push/pop——绘制前 push 一个状态，绘制完 pop 回去。

朴素的写法是在每条返回路径上都手写一行还原代码，极易遗漏（尤其涉及 `?`、`return`、提前退出时）。`defer()` 用 RAII 把"还原"绑定到一个临时值的生命周期：你拿到一个表现得像 `&mut T` 的句柄随意修改，一旦这个句柄离开作用域被 `Drop`，预设的还原闭包就自动执行。

这一点和上一篇 u3-l2 的 `Protected` 异曲同工：`Protected` 用 newtype 强制"访问要写理由"，`defer()` 用值的生命周期强制"离开必还原"——两者都是**用 API 形状把纪律编码进类型系统**。

#### 4.3.2 核心流程

`defer(thing: &mut T, deferred: F)` 的使用流程：

1. 调用方提供一个 `&mut T`（要被临时操作的目标）和一个 `F: FnOnce(&mut T)`（还原动作）。
2. `defer` 借用这个 `&mut T`，构造一个私有句柄 `DeferHandle` 返回。
3. 调用方通过 `DeferHandle`（实现了 `DerefMut`）像使用 `&mut T` 一样读写目标值。
4. 句柄离开作用域 → `Drop` 触发 → 还原闭包以目标值为参数执行一次。

典型用法（图形栈 push/pop）：

```text
fn draw(surface):
    let mut s = defer(surface, |s| s.pop())   # 进入：假定 surface 已 push
    s.set_fill(...)                            # 期间：随意修改 s
    s.draw(...)                                # 离开作用域：自动 s.pop()
```

#### 4.3.3 源码精读

`defer` 的签名与函数体（句柄在函数内部定义）：

[src/lib.rs:L426-L457](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L426-L457) —— 第 427-430 行是函数签名，返回 `impl DerefMut<Target = T>`；第 431-434 行在函数体内定义私有结构 `DeferHandle<'a, T, F>`，持有 `&'a mut T` 和 `Option<F>`；第 456 行构造并返回它。

`DeferHandle` 通过 `Deref` / `DerefMut` 透传对内部 `&mut T` 的访问：

[src/lib.rs:L442-L454](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L442-L454) —— 这两个 trait 实现让 `DeferHandle` 在读写上与 `&mut T` 完全等价，因此拿到句柄后可以无缝调用 `T` 的方法。

这条设计在 Typst 真实代码里被大量使用。一个典型例证在 SVG 路径绘制中：

[src/path.rs:L84-L84](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L84-L84)（位于 `typst-svg` crate）—— `let mut builder = defer(self, |b| b.last_point = pos);`：绘制各种线段前用 `defer` 记下进入时的 `last_point`，绘制过程中 `last_point` 会被反复改写，而一旦 `builder` 离开作用域就自动恢复成进入时的 `pos`。

另一个 push/pop 例子在 PDF 文本导出：

[src/text.rs:L47-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-pdf/src/text.rs#L47-L50)（位于 `typst-pdf` crate）—— `let mut surface = defer(surface, |s| s.pop());`：在 `surface` 上设置填充、描边、画字形，作用域结束时自动 `pop()` 恢复绘图状态栈。这两个真实用法印证了 `defer` 的设计意图。

#### 4.3.4 代码实践

**实践目标：** 用 `defer()` 临时修改一个可变变量，验证作用域结束后它自动还原。

**操作步骤：** 在依赖 `typst-utils` 的项目中写下示例代码并运行。

```rust
// 示例代码（非项目原有代码）
use typst_utils::defer;

fn main() {
    let mut count = 10i32;
    println!("进入作用域前 count = {count}");

    {
        // 临时把 count 当成可改写的句柄，并登记"离开时把 count 还原成 10"
        let mut handle = defer(&mut count, |c| *c = 10);
        *handle += 5;        // 期间随意修改
        println!("作用域内 count = {count}");
    }   // handle 在此被 Drop，回调执行：*count = 10

    println!("离开作用域后 count = {count}");
}
```

**需要观察的现象：** 作用域内 `count` 变成 15；离开作用域后 `count` 自动变回 10。

**预期结果：** 三行输出依次为 `10`、`15`、`10`。

#### 4.3.5 小练习与答案

**练习 1：** `defer` 的返回类型是 `impl DerefMut<Target = T>` 而非直接 `DeferHandle`，为什么要这样写？

**答案：** 因为 `DeferHandle` 是定义在 `defer` 函数体**内部**的私有结构体，外部无法命名它的类型。用 `impl DerefMut<Target = T>` 暴露的是它的能力（可当作 `&mut T` 用），而非具体类型，既隐藏了实现，也让调用方无需关心句柄的确切形态。

**练习 2：** 如果在 `defer` 拿到句柄后，函数因某种原因 `return` 提前退出（而非自然走到作用域末尾），还原还会发生吗？

**答案：** 会。`Drop` 在值离开作用域的**任何**路径上都会触发，包括提前 `return`、`?` 传播错误、`break` 等。这正是 RAII 的核心价值——还原与控制流解耦，不会因漏写而丢失。

---

### 4.4 Drop 回调触发：用 Option<F> + mem::take 调用 FnOnce

#### 4.4.1 概念说明

`DeferHandle` 的 `Drop` 实现里藏着一个 Rust 初学者常踩的坑：**如何在一个结构体里存放一个 `FnOnce` 闭包，并在析构时调用它一次？**

难点在于 `FnOnce` 只能被"消费（move）"着调用一次，而 `Drop::drop(&mut self)` 只有 `&mut self`，**不能**从 `&mut` 引用里 move 出字段。直接写 `self.deferred(self.thing)` 会编译失败——你无法从一个 `&mut` 后面的字段里把闭包搬走。

标准解法是：把闭包存进 `Option<F>`。`Option` 提供了 `take()`——它把内部的值 move 出来、原地留下 `None`，这正是"`&mut self` 下安全 move 出一个字段"的官方手段。

#### 4.4.2 核心流程

`DeferHandle::drop` 的执行流程：

1. `std::mem::take(&mut self.deferred)`：把 `Option<F>` 里的闭包 move 出来（字段变为 `None`），得到原 `Option<F>`（值是 `Some(f)`）。
2. `.expect("deferred function")`：解包 `Option` 得到 `f`（`F`）。
3. `(self.thing)`：以 `&mut T` 为参数调用 `f` 一次。

伪代码：

```text
fn drop(&mut self):
    f = mem::take(&mut self.deferred).expect(...)   # 把闭包从 Option 里搬出
    f(self.thing)                                    # 调用一次：还原状态
```

`mem::take` 能用于 `Option<F>` 是因为 `Option` 实现了 `Default`（默认值是 `None`），所以 `take` 把内部值取出后能原地补一个 `None`。

#### 4.4.3 源码精读

`Drop` 实现集中在一行：

[src/lib.rs:L436-L440](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L436-L440) —— 第 438 行 `std::mem::take(&mut self.deferred).expect("deferred function")(self.thing)` 一气呵成完成"取出 → 解包 → 调用"。`self.deferred` 的类型是 `Option<F>`（见第 433 行字段声明），`expect` 的信息 `"deferred function"` 是一条不会触发的内部不变量断言——`DeferHandle` 构造时 `deferred` 必为 `Some`（第 456 行），且只会被 `drop` 消费一次，所以这里 `take` 出来一定是 `Some`。

对比第 456 行的构造：

[src/lib.rs:L456-L456](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L456-L456) —— `deferred: Some(deferred)` 显式包成 `Some`，与 `Drop` 里的 `expect` 形成闭环，保证不变量"构造为 Some、析构时 take 仍为 Some"成立。

这套"`Option<F>` + `mem::take` + `expect`"的写法是 Rust 中"存储并最终一次性触发 `FnOnce`"的惯用法，值得记下。

#### 4.4.4 代码实践

**实践目标：** 不用 `typst_utils::defer`，自己手写一个最小版 `defer`，体会 `Option<F>` + `mem::take` 的必要性。

**操作步骤：** 写下示例代码并编译运行。

```rust
// 示例代码（非项目原有代码）
struct MyDefer<'a, T, F: FnOnce(&mut T)> {
    thing: &'a mut T,
    deferred: Option<F>,
}

impl<T, F: FnOnce(&mut T)> Drop for MyDefer<'_, T, F> {
    fn drop(&mut self) {
        // 关键：从 &mut self 中把 FnOnce 闭包搬出来，只能靠 Option::take
        std::mem::take(&mut self.deferred).expect("deferred")(self.thing);
    }
}

fn my_defer<'a, T, F: FnOnce(&mut T)>(thing: &'a mut T, f: F) -> MyDefer<'a, T, F> {
    MyDefer { thing, deferred: Some(f) }
}

fn main() {
    let mut x = String::from("hello");
    {
        let mut d = my_defer(&mut x, |s| s.push_str("!"));
        d.thing.push_str(" world"); // 期间修改
    } // drop 触发：push_str("!")
    println!("{x}");
}
```

**需要观察的现象：** 程序能编译通过并运行；若尝试把 `mem::take` 换成直接 `self.deferred.unwrap()(self.thing)`（绕过 take），编译器会报"cannot move out of `self.deferred`"之类的错误。

**预期结果：** 输出 `hello world!`。尝试改写后会得到编译错误——这正好验证了为什么必须用 `Option` + `take`。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 `DeferHandle.deferred` 字段类型是 `Option<F>` 而非直接 `F`？

**答案：** 因为 `Drop::drop` 只有 `&mut self`，无法从 `&mut` 引用直接 move 出 `F`（`FnOnce` 调用要求消费 `F`）。`Option<F>` 配合 `mem::take`（依赖 `Option: Default`）能在 `&mut self` 下安全地把 `F` 搬出来调用。

**练习 2：** `Drop` 里 `.expect("deferred function")` 在什么情况下会 panic？正常使用 `defer()` 时会发生吗？

**答案：** 仅当 `self.deferred` 为 `None` 时 panic。正常使用不会发生：构造时（[src/lib.rs:456](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L456-L456)）置为 `Some`，而 `DeferHandle` 只会被 `Drop` 一次，`take` 只执行一次，故 `expect` 总能拿到 `Some`。它是一条防御性内部不变量断言。

---

## 5. 综合实践

把本讲两个工具串起来：用一个 `Deferred` 在后台预算一个"还原目标值"，主线程同时用 `defer()` 临时改写某个状态，最后核对状态被还原到了 `Deferred` 算出的值。

**任务：** 在一个依赖 `typst-utils` 的项目中实现如下逻辑（示例代码）。

```rust
use std::time::Duration;
use typst_utils::{defer, Deferred};

fn main() {
    // 后台预算：恢复目标 = 进入时的原始值
    let original: i32 = 42;
    let target = Deferred::new(move || {
        std::thread::sleep(Duration::from_millis(100)); // 模拟耗时
        original // 捕获 original 作为恢复值
    });

    let mut state = original;
    {
        // 临时改写 state，并登记"离开时把它还原成后台算出的 target"
        let restore_to = target.wait().clone(); // wait 取回后台结果（这里即 42）
        let mut handle = defer(&mut state, move |s| *s = restore_to);
        *handle += 100;          // 期间改写
        println!("作用域内 state = {state}");      // 142
    } // drop：state 被还原成 restore_to（42）

    println!("作用域外 state = {state}");          // 42
}
```

**操作步骤：**

1. 理解为何 `Deferred` 的闭包要 `move` 捕获 `original`（满足 `F: 'static`）。
2. 观察后台计算（100ms）与 `defer` 还原是否都按预期发生。
3. 把 `*handle += 100` 改成会在中间 `return` 的逻辑（包成函数），验证 `defer` 仍能还原——体会 RAII 与控制流解耦。
4. 思考：若把 `target.wait()` 放到 `defer` 的闭包里（即"还原时再去 wait"）会有什么不同？哪一种更好？把结论写在注释里。

**预期结果：** 输出 `作用域内 state = 142` 与 `作用域外 state = 42`。`Deferred` 把"恢复值"的计算并行化，`defer` 保证还原一定执行。

**待本地验证：** 具体数值以本地编译运行为准；若在单线程目标上编译，留意 `Deferred` 仍能靠 `yield_now` 正常工作。

---

## 6. 本讲小结

- `Deferred<T>` 是 `Arc<OnceCell<T>>` 的薄包装：`new` 把闭包用 `rayon::spawn` 送进后台线程惰性求值，`Clone` 只复制 `Arc`，所有副本 `wait` 到同一个值。
- `wait` 走"快速路径（`get`）→ 协作让出（`rayon::yield_now` 循环）→ 阻塞兜底（`OnceCell::wait`）"三段式；`yield` 是为了让单线程 / WASM 平台上 `spawn` 的任务有机会执行、避免死锁。
- `defer(&mut T, F)` 返回一个 `DerefMut` 句柄 `DeferHandle`，让调用方像用 `&mut T` 一样临时修改目标，并在句柄 `Drop` 时自动执行还原闭包 `F`，是 RAII 式的"临时修改-自动还原"。
- `DeferHandle::drop` 用 `Option<F>` + `std::mem::take` + `expect` 的惯用法，解决"`&mut self` 下无法 move 出 `FnOnce`"的难题。
- 真实驱动场景：`typst-svg` 用 `defer` 还原路径绘制的 `last_point`，`typst-pdf` 用 `defer` 实现绘图状态栈的 `pop`。
- 设计哲学呼应 u3-l2：`Protected` 与 `defer()` 都在"用值 / 类型的形状把使用纪律编码进 API"，前者约束访问，后者约束还原。

## 7. 下一步学习建议

- 下一篇 **u3-l4 版本信息与定义位点 DefSite** 将讲解 `version.rs` 如何读取 build.rs 注入的环境变量，以及 `DefSite` 如何描述宏展开后稳定的定义位置——它同样借助 u1-l3 的 `singleton!` 做惰性解析，可对照阅读。
- 若想加深对 rayon 协作调度的理解，建议阅读 rayon 文档中关于 `yield_now`、线程池与 WASM 单线程模式（`wasm-bindgen-rayon`）的章节，并回顾本仓库 `tests/src/tests.rs` 中关于 `Deferred` yielding 与 `par_bridge` 的注释。
- 想看更多 `defer` 的真实用法，可用 `git grep -n "typst_utils::defer"` 在 `crates/typst-pdf`、`crates/typst-svg` 下检索 push/pop、状态保存恢复等模式。
