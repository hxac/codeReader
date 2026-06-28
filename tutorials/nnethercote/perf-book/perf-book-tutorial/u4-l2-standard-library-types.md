# Standard Library Types——挖掘标准库的性能 API

## 1. 本讲目标

本讲精读 perf-book 第 14 章「Standard Library Types」。学完后你应该能够：

- 知道为什么「通读标准库常见类型的文档」本身就是一个性能优化手段；
- 掌握 `Vec` 上三个常被误用的方法：用 `vec![0; n]` 零填充、用 `swap_remove` 代替 `remove`、用 `retain` 批量删除；
- 理解 `Option::ok_or` 的「急切求值」陷阱，以及 `ok_or_else` 等「惰性求值」替代的收益；
- 理解 `Rc::make_mut` / `Arc::make_mut` 提供的「写时复制」语义；
- 学会评估是否把标准库 `Mutex` / `RwLock` 切换为 `parking_lot`，并用 Clippy 的 `disallowed_types` 防止混用。

贯穿全讲的统一思想是：**很多性能收益不在「换算法」里，而在「选对标准库方法」里**——这些方法签名相近、语义相似，却在分配次数、数据搬运量与求值时机上有本质差异。

## 2. 前置知识

本讲默认你已经掌握以下前置讲义建立的概念，下面只做最简提示，不展开：

- **u2-l2（Profiling）**：优化的前提是先定位「热点」。本讲提到的替换只在「这段代码被频繁执行」时才有意义；冷代码上换 API 是白费功夫。回溯技巧是先用 profiler（samply / perf + flamegraph）确认热点，再回来挑方法。
- **u3-l1（堆分配）**：`Vec` 的「三字表示（length / capacity / pointer）」、`clone` / `to_owned` 触发新分配，以及「分配率」这个独立性能维度。本讲讨论 `retain`、`swap_remove` 时，本质是在减少搬运与中间分配。
- **u3-l2（Vec 增长与复用）**：`Vec::with_capacity` 预分配、`workhorse` 集合 + `clear()` 复用容量。本讲的 `vec![0; n]` 与它们同属「减少不必要的分配与搬运」。
- **u4-l1（Hashing）** 与 **u2-l4（Linting）**：`clippy.toml` 里的 `disallowed_types` lint 能把「团队约定换掉某个标准库类型」固化为机器强制规则。本讲第 4 模块会再次用到它（这次是禁用标准库同步原语）。

两个需要先点明的术语：

- **急切求值（eager）**：参数在函数被调用之前就先算好、传进去，不管函数内部到底用不用得上。
- **惰性求值（lazy）**：把「如何计算」包进闭包，函数内部真正需要时才调用闭包去算。

## 3. 本讲源码地图

本讲几乎全部内容来自一个源码文件，另有一个跨章引用：

| 文件 | 作用 |
| --- | --- |
| `src/standard-library-types.md` | 本章全部内容：`Vec`、`Option`/`Result`、`Rc`/`Arc`、`Mutex` 等同步类型的高性能方法与替代品 |
| `src/linting.md` | 第 4 模块引用其中的「Disallowing Types」一节，说明如何用 Clippy 强制统一使用 `parking_lot` |

本章篇幅不长但信息密度高，几乎所有结论都是「用 A 代替 B」的可操作替换，适合当作一份「标准库方法速查表」反复查阅。

## 4. 核心概念与源码讲解

本章开篇就给出了贯穿全讲的建议（中文意译）：通读 `Vec`、`Option`、`Result`、`Rc`/`Arc` 这些常见类型的文档，去发现那些「有时能提升性能」的函数；同时也要了解 `Mutex`、`RwLock`、`Condvar`、`Once` 这些类型的高性能替代品。

参见本章开头的引言：

[src/standard-library-types.md:3-15](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L3-L15) —— 本章主张「通读标准库文档」这一行为本身就是优化手段，并列出了四类要重点关注的类型。

下面按四个最小模块逐一展开。

### 4.1 `vec![0;n]` 与 `swap_remove` / `retain`

#### 4.1.1 概念说明

`Vec` 是 Rust 程序里出现频率最高的集合（见 u3-l1 的「三字表示」）。正因为太常用，它的三类操作最容易被「用顺手但不够快」的写法拖慢：

1. **零填充**：想得到一个长度为 `n`、元素全为 0 的 `Vec`；
2. **按下标删除单个元素**：在某个位置删掉一个元素；
3. **批量删除多个元素**：按条件删掉若干元素。

perf-book 对这三类各给出一个「首选写法」。它们的共同点是：把 `O(n)` 的搬运或重复分配，换成 `O(1)` 或单趟扫描。

#### 4.1.2 核心流程

**(a) 零填充：用 `vec![0; n]`**

`vec![0; n]` 会创建一个长度为 `n`、元素全为 0 的 `Vec`。它的优势在于可以利用「操作系统协助」——例如让内核直接提供已经清零的内存页，而不必在用户态逐字节写入。因此它通常和 `resize`、`extend`、乃至手写 `unsafe` 一样快甚至更快，而且更简单。

**(b) 删除单个元素：`remove` vs `swap_remove`**

- `Vec::remove(i)`：删掉下标 `i` 的元素，然后把其后所有元素整体左移一位，是 `O(n)`。设 `Vec` 长度为 `n`，需要搬运的元素个数为：

  \[ \text{搬运元素数} = n - i - 1 \]

  删靠近末尾的元素便宜，删靠近开头的元素贵。它**保持剩余元素的相对顺序**。

- `Vec::swap_remove(i)`：把最后一个元素搬过来盖住下标 `i`，然后丢掉末尾，是 `O(1)`。它**不保持顺序**。

所以：**当你不在乎顺序时，删元素请用 `swap_remove` 而非 `remove`**，复杂度直接从 `O(n)` 降到 `O(1)`。

**(c) 批量删除：用 `retain`**

如果你要按条件删掉 `Vec` 里多个元素，最自然的写法可能是「循环里反复 `remove`」。但每次 `remove` 都是 `O(n)`，叠加起来非常昂贵。正确做法是用 `Vec::retain`：它对 `Vec` 做一趟原地扫描，保留谓词返回 `true` 的元素，丢掉其余的，整体是 `O(n)` 一次完成，不会反复搬运。`String`、`HashSet`、`HashMap` 等集合也有等价的 `retain` 方法。

#### 4.1.3 源码精读

本章「`Vec`」一节明确给出了这三条建议：

[src/standard-library-types.md:24-29](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L24-L29) —— 用 `vec![0; n]` 创建零填充 `Vec`，并指出它「大概与其它写法一样快或更快」，因为它可以利用 OS 协助。

[src/standard-library-types.md:31-34](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L31-L34) —— `remove` 是 `O(n)`（左移），`swap_remove` 不保序但 `O(1)`。

[src/standard-library-types.md:36-38](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L36-L38) —— `retain` 高效地批量删除，其它集合（`String`/`HashSet`/`HashMap`）也有等价方法。

#### 4.1.4 代码实践

下面的「示例代码」演示三种删除方式的差异。它不是 perf-book 的原有代码，但你可以放进一个 `cargo new` 的小项目里运行：

```rust
// 示例代码：对比 remove / swap_remove / retain
fn main() {
    // 1) 零填充：首选 vec![0; n]
    let zeros: Vec<u32> = vec![0; 1_000_000];
    assert_eq!(zeros.len(), 1_000_000);

    // 2) 删下标 0 的元素
    let mut v1: Vec<u32> = (0..100_000).collect();
    v1.remove(0);            // O(n)：搬 99999 个元素
    assert_eq!(v1[0], 1);

    let mut v2: Vec<u32> = (0..100_000).collect();
    v2.swap_remove(0);       // O(1)：把末尾元素搬过来
    assert_eq!(v2[0], 99_999); // 注意：顺序变了

    // 3) 批量删除偶数
    let mut v3: Vec<u32> = (0..100_000).collect();
    // ❌ 慢：循环里反复 remove
    // for i in (0..v3.len()).rev() { if v3[i] % 2 == 0 { v3.remove(i); } }
    // ✅ 快：retain 一趟搞定
    v3.retain(|&x| x % 2 != 0);
    assert!(v3.iter().all(|&x| x % 2 != 0));
}
```

**操作步骤**：把上面代码放进 `src/main.rs`，`cargo run --release` 跑通；然后把第 3 步注释掉的「循环 remove」写法打开（同时关掉 `retain` 那行），对比运行时间。

**需要观察的现象**：在小规模（几百元素）下两者都瞬时完成，看不出差异；但当数据量上到十万、百万级时，「循环 remove」会明显变慢，因为它每次删除都要整体左移。

**预期结果**：`retain` 版本远快于「循环 remove」版本；`swap_remove(0)` 远快于 `remove(0)`。具体倍数「待本地验证」，因为它取决于元素大小、缓存命中与数据量。

#### 4.1.5 小练习与答案

**练习 1**：一个长度为 `n` 的 `Vec`，用 `remove(0)` 连续删除前 `k` 个元素，总搬运量是多少？换成 `swap_remove(0)` 呢？

**答案**：`remove(0)` 每次删除都要把剩下元素整体左移，第 `i` 次删除搬运 `n - i - 1` 个元素，`k` 次累计搬运约为 \( \sum_{i=0}^{k-1}(n - i - 1) \)，是 `O(k·n)`。`swap_remove(0)` 每次只搬 1 个末尾元素，`k` 次共 `O(k)`。

**练习 2**：为什么 `vec![0; n]` 往往不比手写 `unsafe` 慢？

**答案**：因为它可以借助操作系统提供的清零内存（零页映射等），省掉用户态逐字节写入，又能保持安全；手写 `unsafe` 不见得更快，还多了出错风险。

---

### 4.2 `ok_or_else` 等惰性求值替代

#### 4.2.1 概念说明

`Option::ok_or` 把一个 `Option<T>` 转成 `Result<T, E>`：当是 `Some(v)` 时返回 `Ok(v)`，当是 `None` 时用你传入的 `err` 作为错误。关键陷阱在于——**`err` 是在调用 `ok_or` 之前就被急切求值的**，无论 `Option` 是 `Some` 还是 `None`。

如果构造 `err` 本身很贵（比如要格式化字符串、查表、分配），那么在 `Some` 的常见路径上，这笔开销就白白浪费了。`Option::ok_or_else` 用闭包把错误值的计算推迟到「真的需要」的那一刻（即 `None` 分支），从而实现惰性求值。

#### 4.2.2 核心流程

- `o.ok_or(expensive())`：`expensive()` **无条件**先执行，结果作为参数传入；即使 `o` 是 `Some`，这次昂贵计算也已发生。
- `o.ok_or_else(|| expensive())`：闭包 `|| expensive()` 只是「一个尚未执行的计算」；仅当 `o` 为 `None` 时才调用闭包求值。

两者在语义上等价（返回值类型相同），区别纯粹在求值时机。把 `ok_or` 改成 `ok_or_else` 是一次零风险的「单点替换」，前提是错误值构造确实有成本。

同样的「急切 vs 惰性」配对还有：`Option::map_or` / `Option::map_or_else`、`Option::unwrap_or` / `unwrap_or_else`，以及 `Result` 上的 `or` / `or_else`、`map_or` / `map_or_else`、`unwrap_or` / `unwrap_or_else`。规律是：**名字里带 `_else` 或 `_else` 的版本接受闭包、惰性求值**。

#### 4.2.3 源码精读

本章用一段对照代码精确说明了这个陷阱：

[src/standard-library-types.md:46-49](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L46-L49) —— `ok_or` 的 `err` 参数被急切求值；若构造昂贵，应改用 `ok_or_else` 通过闭包惰性求值。

[src/standard-library-types.md:51-61](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L51-L61) —— 项目原文的对照示例：`o.ok_or(expensive())` 总是执行 `expensive()`；`o.ok_or_else(|| expensive())` 只在需要时执行。

[src/standard-library-types.md:67-68](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L67-L68) —— 列出其它有惰性等价版本的方法：`Option::map_or`、`Option::unwrap_or`、`Result::or`、`Result::map_or`、`Result::unwrap_or`（均可配 `_else` 闭包版本）。

#### 4.2.4 代码实践

下面「示例代码」构造一个「错误值很贵」的场景，让 `ok_or` 与 `ok_or_else` 的差异可见：

```rust
// 示例代码：ok_or 的急切求值 vs ok_or_else 的惰性求值
use std::time::Instant;

fn expensive_err() -> String {
    // 模拟一次昂贵的错误构造（格式化 + 分配）
    std::thread::sleep(std::time::Duration::from_micros(50));
    format!("error: 缓存未命中，需回源构建 {}", "x".repeat(1000))
}

fn parse_eager(input: Option<u32>) -> Result<u32, String> {
    input.ok_or(expensive_err())          // ❌ 即使 Some 也先跑 expensive_err()
}

fn parse_lazy(input: Option<u32>) -> Result<u32, String> {
    input.ok_or_else(|| expensive_err())  // ✅ 仅 None 时才跑
}

fn main() {
    let input = Some(42); // 常见路径：命中 Some

    let t1 = Instant::now();
    let _ = parse_eager(input);
    let eager = t1.elapsed();

    let t2 = Instant::now();
    let _ = parse_lazy(input);
    let lazy = t2.elapsed();

    println!("eager: {eager:?}, lazy: {lazy:?}");
    println!("lazy 是否快很多？{}", lazy < eager);
}
```

**操作步骤**：放入 `src/main.rs`，`cargo run --release`，让 `input = Some(42)`（命中常见路径）。

**需要观察的现象**：`eager` 明显比 `lazy` 慢——因为 `parse_eager` 在 `Some` 路径上也执行了 `expensive_err()`（含 `sleep`）。

**预期结果**：`eager` 至少比 `lazy` 多出约 50 微秒（`sleep` 的开销），`lazy` 几乎为 0。把 `input` 改成 `None`，两者耗时接近（都执行了错误构造），这正印证了「差异只在 `Some` 路径上」。

#### 4.2.5 小练习与答案

**练习 1**：下面这段代码，`get_label` 在 `name` 是 `Some` 时是否仍被调用？如何改？

```rust
let id: Option<u32> = Some(10);
let r = id.ok_or(get_label());
```

**答案**：会调用。`ok_or` 急切求值，`get_label()` 在 `ok_or` 执行前就已运行。改为 `id.ok_or_else(|| get_label())` 即可惰性求值。

**练习 2**：`Option::unwrap_or(default())` 与 `Option::unwrap_or_else(|| default())` 的区别是什么？

**答案**：`unwrap_or(default())` 无条件先算 `default()`；`unwrap_or_else(|| default())` 仅在 `None` 时才算。当 `Some` 是常见路径且 `default()` 有成本时，用 `_else` 版本更省。

---

### 4.3 `Rc::make_mut` / `Arc::make_mut` 写时复制

#### 4.3.1 概念说明

`Rc`（单线程引用计数）和 `Arc`（原子引用计数，见 u3-l1）让多个所有者共享同一份堆数据。但共享意味着「只读」——要修改内部值时，你不能直接拿到 `&mut T`，否则会破坏别的所有者。

`Rc::make_mut` / `Arc::make_mut` 解决的就是「想在共享值上修改」的需求，采用**写时复制（clone-on-write）**语义：它在需要时才真正复制，能省则省。

#### 4.3.2 核心流程

`make_mut` 接受一个 `&mut Rc<T>`（或 `&mut Arc<T>`），返回 `&mut T`。内部按引用计数分支：

- **引用计数 == 1（唯一所有者）**：当前没有别人共享，直接返回指向原始值的可变引用，**零拷贝**。
- **引用计数 > 1（有多人共享）**：先 `clone` 一份内部值到新分配，把新分配交给这个 `Rc`/`Arc`，原值的引用计数减 1，再返回指向「这份私有副本」的可变引用。这样修改只影响自己，不影响其它所有者。

用伪代码表示：

```
fn make_mut(rc: &mut Rc<T>) -> &mut T {
    if Rc::strong_count(rc) == 1 {
        // 唯一所有者：原地可变
        return &mut *rc;              // 零拷贝
    } else {
        // 多人共享：先克隆出私有副本
        *rc = Rc::new((*rc).clone()); // 触发一次堆分配
        Rc::strong_count(rc) 旧值减 1
        return &mut *rc;              // 指向新副本
    }
}
```

perf-book 的评价是：`make_mut`「不常需要，但偶尔极其有用」。它最适合「大多数时间只读、偶尔要改」的共享数据——常见路径上引用计数为 1，几乎零开销；只有真正发生共享写入时才付拷贝代价。

#### 4.3.3 源码精读

[src/standard-library-types.md:78-82](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L78-L82) —— `Rc::make_mut` / `Arc::make_mut` 提供写时复制语义：引用计数大于 1 时克隆内部值以保证唯一所有权，否则直接改原值。

[src/standard-library-types.md:83-84](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L83-L84) —— 给出两个真实 Rust 仓库的提交示例（来自 rust-lang/rust 的 PR），可作为阅读真实用法的参考。

#### 4.3.4 代码实践

下面「示例代码」演示引用计数为 1 与大于 1 两种情形下 `make_mut` 的行为差异：

```rust
// 示例代码：观察 make_mut 在「唯一」与「共享」时的不同代价
use std::rc::Rc;

fn main() {
    // 情形 A：唯一所有者，make_mut 零拷贝
    let mut a: Rc<Vec<i32>> = Rc::new(vec![1, 2, 3]);
    {
        let _b = Rc::clone(&a);              // 强制制造共享（计数 = 2）
        let _ = Rc::make_mut(&mut a);        // 此时克隆：a 指向新副本
        assert_eq!(Rc::strong_count(&a), 1); // a 现在又是唯一所有者
        assert_eq!(*a, vec![1, 2, 3]);       // 内容仍是原值
    }
    // 此时 _b 已离开作用域，a 计数为 1

    // 情形 B：仍唯一，make_mut 不克隆
    let before = Rc::as_ptr(&a) as usize;
    Rc::make_mut(&mut a).push(4);            // 原地改，无克隆
    let after = Rc::as_ptr(&a) as usize;
    assert_eq!(before, after);               // 指针未变 → 没有重新分配
    assert_eq!(*a, vec![1, 2, 3, 4]);
}
```

**操作步骤**：放入 `src/main.rs`，`cargo run`（这里 `Rc` 是单线程的，不需要 `--release`）。

**需要观察的现象**：情形 A 在 `make_mut` 前制造了共享，所以 `make_mut` 触发克隆，之后 `a` 的强引用计数回到 1；情形 B 中 `a` 一直唯一，`make_mut` 不克隆，`as_ptr` 前后不变。

**预期结果**：所有 `assert!` 通过。这说明 `make_mut` 是「按需付费」——共享才付拷贝，独占则零成本。

> 说明：`as_ptr` 比较「指针是否改变」是一种推断「是否重新分配」的常用手段；在更严谨的场景下，应像 u3-l1 那样用 `dhat-rs` 统计真实分配次数（「待本地验证」具体分配数）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `make_mut` 在引用计数为 1 时可以直接返回 `&mut T`，而不需要克隆？

**答案**：引用计数为 1 表示当前 `Rc` 是唯一所有者，没有任何别的人持有这份数据，修改它不会影响其它所有者，因此可以安全地原地可变，零拷贝。

**练习 2**：如果一段共享数据「频繁被多个所有者同时修改」，`make_mut` 还合适吗？

**答案**：不太合适。频繁共享写入会让 `make_mut` 经常走进「计数 > 1」分支，反复克隆+分配，反而变慢且抬高分摊率（呼应 u3-l1 的「分配率」概念）。这种场景更适合直接用 `&mut`、或重新设计数据所有权，避免靠写时复制来「救场」。

---

### 4.4 `parking_lot` 同步原语的取舍

#### 4.4.1 概念说明

标准库的 `Mutex`、`RwLock`、`Condvar`、`Once` 是基本的同步原语。第三方 crate [`parking_lot`](https://crates.io/crates/parking_lot) 提供了这些类型的「替代实现」，API 与语义「相似但不完全相同」。

历史上 `parking_lot` 版本在体积、速度、灵活性上都**可靠地优于**标准库版本；但近年来标准库版本在某些平台上已经大幅改进。因此本章给出的关键结论是：**切换前必须先测量**——「`parking_lot` 一定更快」不再成立。

#### 4.4.2 核心流程

评估是否切换的决策流程：

1. **先用 profiler 确认锁竞争是热点**（承接 u2-l2）。如果锁根本不在热点路径上，换不换都无所谓。
2. **构造一个可重复的基准**（承接 u2-l1），覆盖你真实的锁使用模式（临界区长短、读写比例、并发线程数）。
3. **逐项替换并测量**：把标准库 `Mutex` 换成 `parking_lot::Mutex`，对比基准。只有在实测确实更快（且差异稳定、大于噪声）时才采纳。
4. **防止混用**：一旦决定「全项目用 `parking_lot`」，就要当心有人在新代码里不小心又写了 `std::sync::Mutex`。本章给出的对策是**用 Clippy 的 `disallowed_types` lint**——在 `clippy.toml` 里把标准库类型列为禁用，编译期就拦住误用（这正是 u2-l4 / u4-l1 用过的同一招）。

权衡要点：`parking_lot` 的 API 与标准库「相似但不完全相同」，例如某些错误处理、 poisoning 语义有差异，切换不是纯机械替换，需对照迁移。

#### 4.4.3 源码精读

[src/standard-library-types.md:91-99](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L91-L99) —— `parking_lot` 提供 `Mutex`/`RwLock`/`Condvar`/`Once` 的替代实现；过去「又小又快又灵活」，但标准库版本在某些平台已大幅改进，故切换前须测量。

[src/standard-library-types.md:103-105](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/standard-library-types.md#L103-L105) —— 全项目改用 `parking_lot` 后，容易不小心又用回标准库版本；可用 Clippy 避免此问题。

该链接指向 linting 章的「Disallowing Types」一节，那里给出了 `clippy.toml` 的写法：

[src/linting.md:46-52](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L46-L52) —— 用 `disallowed-types` lint 在 `clippy.toml` 中禁用某些标准库类型（书中以禁用 `HashMap`/`HashSet` 为例）。

把同一写法套到本模块，即可禁用标准库同步原语（示例代码，非书中原句）：

```toml
# 示例代码：clippy.toml，强制全项目用 parking_lot 而非标准库同步原语
disallowed-types = [
    "std::sync::Mutex",
    "std::sync::RwLock",
    "std::sync::Condvar",
    "std::sync::Once",
]
```

#### 4.4.4 代码实践

下面「示例代码」用 `parking_lot` 和标准库各写一个 `Mutex`，并在多线程下高频争抢，供你用基准测试对比（这是「源码阅读型 + 待本地验证」实践，因为实际加速比取决于平台与负载）：

```rust
// 示例代码：对比 std::sync::Mutex 与 parking_lot::Mutex
// Cargo.toml 需加：parking_lot = "0.12"
use std::sync::Arc;
use std::thread;

fn bench_std(threads: usize, iters: u64) -> u64 {
    let m = Arc::new(std::sync::Mutex::new(0u64));
    let handles: Vec<_> = (0..threads)
        .map(|_| {
            let m = Arc::clone(&m);
            thread::spawn(move || {
                for _ in 0..iters {
                    *m.lock().unwrap() += 1;
                }
            })
        })
        .collect();
    for h in handles { h.join().unwrap(); }
    *m.lock().unwrap()
}

fn bench_parking_lot(threads: usize, iters: u64) -> u64 {
    let m = Arc::new(parking_lot::Mutex::new(0u64));
    let handles: Vec<_> = (0..threads)
        .map(|_| {
            let m = Arc::clone(&m);
            thread::spawn(move || {
                for _ in 0..iters {
                    *m.lock() += 1; // 注意：parking_lot 的 lock() 直接返回守卫，无需 unwrap
                }
            })
        })
        .collect();
    for h in handles { h.join().unwrap(); }
    *m.lock()
}

fn main() {
    let (threads, iters) = (4, 1_000_000);
    println!("std sum = {}", bench_std(threads, iters));
    println!("pl  sum = {}", bench_parking_lot(threads, iters));
}
```

**操作步骤**：
1. `cargo add parking_lot`，放入上面代码，`cargo run --release` 确认两版结果一致（都是 `threads * iters`）。
2. 用 Hyperfine（见 u2-l1）分别计时 `bench_std` 版与 `bench_parking_lot` 版（可把两版各拆成一个独立的二进制入口，或写一个 `#[bench]`/Criterion 基准）。
3. 在 `clippy.toml` 加上面那段 `disallowed-types`，运行 `cargo clippy`，确认若有人写了 `std::sync::Mutex` 会被告警。

**需要观察的现象**：在你的机器/平台上，`parking_lot` 版可能更快、可能持平、理论上甚至可能更慢——这正是 perf-book 要求「测量后再决定」的原因。

**预期结果**：两版求和结果一致（正确性不变）；性能高低「待本地验证」，请以你机器上的实测为准，**不要假设 `parking_lot` 必胜**。

#### 4.4.5 小练习与答案

**练习 1**：既然 `parking_lot`「曾经又小又快」，为什么 perf-book 仍要求「切换前先测量」？

**答案**：因为标准库版本在某些平台上已经大幅改进，`parking_lot` 的优势不再「可靠地」成立；性能取决于平台、负载、临界区长度与并发度，只有基准测试能给可靠结论。

**练习 2**：全项目改用 `parking_lot::Mutex` 后，如何用最低成本防止有人误用回 `std::sync::Mutex`？

**答案**：在 `clippy.toml` 里用 `disallowed-types` 把 `std::sync::Mutex` 等列为禁用（写法见 4.4.3），`cargo clippy` 会在编译期拦住误用——这正是 u2-l4 / u4-l1 用过的同一机制，把团队约定固化为机器强制规则。

---

## 5. 综合实践

把本讲的四个模块串起来，做一次「标准库方法审查」综合任务。下面的「示例代码」同时含 `ok_or`、`Vec::remove`、`std::sync::Mutex` 三处「可优化点」，请你逐一改写并评估：

```rust
// 示例代码：待审查与改写的「不够快」版本
use std::sync::{Arc, Mutex};
use std::thread;

fn expensive_err() -> String {
    // 模拟昂贵的错误构造
    format!("error: {}", "x".repeat(1000))
}

// ❶ 用了 ok_or（急切求值 expensive_err，即使 Some 也跑）
fn find(values: &[u32], target: u32) -> Result<u32, String> {
    values.iter().copied().find(|&v| v == target).ok_or(expensive_err())
}

// ❷ 用了 Vec::remove（O(n)），且在循环里删除
fn drop_negatives(mut v: Vec<i32>) -> Vec<i32> {
    let mut i = 0;
    while i < v.len() {
        if v[i] < 0 { v.remove(i); } else { i += 1; }
    }
    v
}

// ❸ 用了 std::sync::Mutex（是否该换 parking_lot？）
fn counter(threads: usize, iters: u64) -> u64 {
    let m = Arc::new(Mutex::new(0u64));
    let hs: Vec<_> = (0..threads).map(|_| {
        let m = Arc::clone(&m);
        thread::spawn(move || { for _ in 0..iters { *m.lock().unwrap() += 1; } })
    }).collect();
    for h in hs { h.join().unwrap(); }
    *m.lock().unwrap()
}

fn main() {
    let _ = find(&[1, 2, 3], 2);
    let _ = drop_negatives(vec![1, -2, 3, -4]);
    let _ = counter(4, 100_000);
}
```

**任务清单**：

1. **改 ❶**：把 `ok_or(expensive_err())` 改成 `ok_or_else(|| expensive_err())`，说明为什么在「`Some` 是常见路径」时这一改更快（参考 4.2）。
2. **改 ❷**：把「循环里 `remove`」改成 `v.retain(|&x| x >= 0)`，说明为什么从 `O(n²)` 降到 `O(n)`（参考 4.1）。
3. **评估 ❸**：先用 profiler（samply / perf，参考 u2-l2）确认 `counter` 的锁是否真的在热点上；若是，再用基准测试（Hyperfine / Criterion，参考 u2-l1）对比换成 `parking_lot::Mutex` 后是否更快。**只有在实测更快时才切换**，并用 `clippy.toml` 的 `disallowed-types` 固化选择（参考 4.4）。

**预期结果**：❶❷ 是「无脑更快」的纯替换；❸ 是「测量后决定」的可选替换。完成后，你应该能清晰说出每处改动「为什么更快」以及「凭什么相信它更快（测量还是复杂度论证）」。

## 6. 本讲小结

- 本章的核心建议是：**通读标准库常见类型（`Vec`/`Option`/`Result`/`Rc`/`Arc`）的文档，本身就是性能优化**——很多收益藏在「选对方法」里。
- `Vec` 三连：用 `vec![0; n]` 零填充（可借 OS 协助）、用 `swap_remove` 代替 `remove`（`O(n)` → `O(1)`，代价是不保序）、用 `retain` 批量删除（一趟 `O(n)`，避免循环 `remove` 的 `O(n²)`）。
- `Option`/`Result` 求值时机：`ok_or` 急切求值错误值，`ok_or_else`（及 `map_or_else` / `unwrap_or_else` / `or_else` 等）用闭包惰性求值；错误值构造昂贵时务必换惰性版本。
- `Rc::make_mut` / `Arc::make_mut` 提供写时复制：引用计数为 1 时零拷贝原地改，大于 1 时才克隆出私有副本；适合「常读偶写」的共享数据。
- `parking_lot` 提供标准库同步原语的替代实现，但「一定更快」已不成立，**切换前必须测量**；全项目改用后用 `clippy.toml` 的 `disallowed_types` 防止误用回标准库版本。
- 贯穿全讲的纪律：纯替换（❶❷ 类）靠复杂度论证即可采纳；可选项（❸ 类）必须靠基准测试定去留。

## 7. 下一步学习建议

- 本讲的 `swap_remove`、`retain`、`vec![0; n]` 都是在「减少搬运与分配」，与 **u3-l1（堆分配）/ u3-l2（Vec 增长与复用）** 一脉相承。如果你还没把 `dhat-rs` 的堆用量回归测试用起来，建议回头补上，把「少分配」变成 CI 可拦截的硬约束。
- 下一站建议进入 **u4-l3（Iterators）**：那里会讲「避免不必要的 `collect`」「返回 `impl Iterator`」「`chain` / `filter_map` / `chunks_exact` 的取舍」，与本讲的 `retain`、惰性求值同属「减少中间分配与求值」的主题，正好衔接。
- 如果你对「锁的开销」这一节意犹未尽，可以跳到 **u6-l3（Parallelism）**，它从线程并行（rayon / crossbeam）与同步原语的角度系统讨论并发性能；而 **u5-l3（Wrapper Types 与日志/调试）** 则会进一步讲 `Mutex` 等包装类型「每次访问的开销」与「合并多个包装值」的技巧。
- 阅读源码建议：把 `src/standard-library-types.md` 当作速查表，配合官方标准库文档（书中文末附的 `doc.rust-lang.org` 链接）对照阅读每个方法的签名与复杂度说明，印象会更深。
