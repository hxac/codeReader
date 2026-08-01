# 位压缩集合 BitSet 与 SmallBitSet

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「用一个机器字的每一位表示一个元素是否存在」这种位向量（bitset）思想，以及为什么它能极大节省内存。
- 读懂 `typst-utils` 里 `BitSet` 如何用 `ThinVec<usize>` 分块存位，以及 `insert`/`contains` 背后的 `chunk / within` 除法和位运算。
- 理解 `SmallBitSet` 的「小值 inline + 大值溢出」混合策略：为什么绝大多数情况零分配，以及为什么大值要减去 `BITS` 再存。
- 学会阅读两个结构 `Debug` 实现里「遍历所有可能值、按位检测」的还原写法。

本讲承接 [u1-l2](u1-l2-extension-traits-and-helpers.md)：那里我们学了如何用扩展 trait 给标准库类型加方法；这里我们看一个完整的、自包含的小数据结构是如何从零设计出来的。

## 2. 前置知识

在进入源码前，先用通俗语言把几个基础概念讲清楚。

**集合与位向量。** 一个「集合」关心的是「某个元素在不在里面」。如果元素都是非负整数 `0, 1, 2, …`，我们可以准备一串连续的「位（bit）」，第 `v` 位为 `1` 就表示 `v` 在集合里，为 `0` 就表示不在。这就是 **位向量（bit vector / bitset）**。它的好处是极度紧凑：8 个元素只需 1 字节。

**一个 `usize` 有多少位。** 在 64 位平台上一个 `usize` 是 64 位，在 32 位平台上是 32 位。Rust 用常量 `usize::BITS` 给出这个数。所以一个 `usize` 就能天然地当 64 位（或 32 位）的「位盘」用。

**三个位运算。** 本讲只需要三个操作：

| 运算 | 写法 | 含义 |
|---|---|---|
| 左移 | `1 << k` | 把 `1` 左移 `k` 位，得到「只有第 `k` 位是 1」的掩码 |
| 按位或赋值 | `word \|= mask` | 把某些位置 1（用来「插入」） |
| 按位与 | `word & mask` | 取出某些位（用来「查询」） |

把某个位置 1：`word |= 1 << k`。查询某位是否为 1：`(word & (1 << k)) != 0`。

**`ThinVec` 是什么。** `thin-vec` crate 提供的 `ThinVec<T>` 是一种「瘦向量」：空的 `ThinVec` **不会在堆上分配**，整个值大约只占一个指针的大小（它把长度/容量等元信息放到堆上的头部，空时连头部都没有）。这一点是本讲 `BitSet` 「小集合不浪费内存」的关键。这一点在 [u1-l1](u1-l1-project-overview-and-build.md) 的依赖表里也提到过。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它定义了两个互相配合的结构：

| 文件 | 作用 |
|---|---|
| [src/bitset.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs) | 定义 `BitSet`（按 `usize` 分块存位的集合）与 `SmallBitSet`（小值 inline、大值溢出到 `BitSet` 的集合），以及它们的 `Debug` 实现和单元测试 |

这两个类型在 [src/lib.rs:19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L19) 通过 `pub use self::bitset::{BitSet, SmallBitSet};` 对外导出，所以使用时直接写 `typst_utils::BitSet` / `typst_utils::SmallBitSet` 即可。

## 4. 核心概念与源码讲解

### 4.1 BitSet：分块位存储

#### 4.1.1 概念说明

`BitSet` 解决的问题是：**用最少的内存存一个「期望比较小」的非负整数集合**。

思路是把一长串位切成一段段「机器字」（`usize`），每段管 `BITS` 个位。要存值 `v`，先算出它落在第几个字（`chunk`）、字内的第几位（`within`），再把那一位置 1。

为什么用 `ThinVec<usize>` 而不是普通 `Vec<usize>`？因为 `BitSet` 经常是空的或很小，`ThinVec` 让空集合零分配，省掉一次堆内存申请。代价是：**插入一个很大的值会很贵**——因为要为中间所有空位补零。源码文档明确说明了这个权衡。

#### 4.1.2 核心流程

设每个字的位数为 \( B = \text{BITS} \)（64 位平台上 \( B = 64 \)）。对任意值 \( v \)：

\[
\text{chunk} = \left\lfloor \frac{v}{B} \right\rfloor, \qquad \text{within} = v \bmod B
\]

- 若集合当前只有 `n` 个字（`n = ThinVec 的长度`），而 `chunk >= n`，则需要把向量扩容到 `chunk + 1` 个字，新增位全填 0。
- 在第 `chunk` 个字里把第 `within` 位置 1：`word[chunk] |= 1 << within`。

要容纳的最大值为 \( v_{\max} \) 时，需要的字数为：

\[
\left\lfloor \frac{v_{\max}}{B} \right\rfloor + 1
\]

也就是说，**内存占用只取决于「插入过的最大值」，而与插入次数无关**。插 1 个值 `10_000_000` 也会分配一大片。

#### 4.1.3 源码精读

结构体定义与每块位数常量：

[bitset.rs:5-17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L5-L17) —— `BITS` 取 `usize::BITS`（64 位平台上为 64）；`BitSet` 内部就是一个 `ThinVec<usize>`，并派生了 `Clone/PartialEq/Hash`（注意没有 `remove`，这是「只增不减」的集合）。

文档注释也点明了设计取舍：

[bitset.rs:8-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L8-L15) —— 插入小值廉价、插入大值昂贵；并建议「除非自己管小数字，否则优先用 `SmallBitSet`」。

#### 4.1.4 代码实践

**目标**：直观感受 `BitSet` 的内存占用取决于「最大值」而非「元素个数」。

**步骤**（示例代码）：

```rust
// 示例代码
use typst_utils::BitSet;

fn main() {
    let mut a = BitSet::new();
    for v in 0..10 {
        a.insert(v); // 只用到第 0 个字
    }

    let mut b = BitSet::new();
    b.insert(10_000_000); // 一个超大值

    println!("a = {:?}", a);
    println!("b = {:?}", b);
}
```

**需要观察的现象**：`a` 的 Debug 输出是 `[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]`；`b` 虽然只插了一个值，但因为最大值极大，内部 `ThinVec` 会被扩容到非常长（约 `10_000_000 / 64 + 1 ≈ 156250` 个字）。

**预期结果**：通过 `{:?}` 能看到 `a` 完整列出 10 个小值；`b` 列出单个 `10000000`，但如果你在 `insert` 前后打印 `std::mem::size_of_val(&b)`，应看到 `BitSet` 本体只是一个指针大小（约 8 字节，64 位平台），真正的开销在堆上。具体字节数**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：在一个 64 位平台上，`BitSet::new()` 后先 `insert(63)` 再 `insert(64)`，内部 `ThinVec` 的长度分别是多少？

**答案**：`insert(63)` 时 `chunk = 63 / 64 = 0`，扩容到长度 1；`insert(64)` 时 `chunk = 64 / 64 = 1`，扩容到长度 2。所以最终长度为 2。

**练习 2**：为什么 `BitSet` 没有提供 `remove` 方法也能在很多场景下用？

**答案**：它的典型用途是「记录某些索引出现过/某些特性被启用」，这类用法通常是只增的；省掉 `remove` 可以保持实现极简、聚焦核心需求。

---

### 4.2 insert / contains 的位运算

#### 4.2.1 概念说明

上一节讲了存储布局，这一节看具体怎么「写入一位」和「读取一位」。核心就是 `chunk`/`within` 除法 + 三个位运算。`insert` 和 `contains` 共享完全相同的定位逻辑，区别只在最后一步是「置 1」还是「测试」。

#### 4.2.2 核心流程

伪代码（`B = BITS`）：

```
fn insert(v):
    chunk = v / B
    within = v % B
    若 chunk 超出当前长度 → 扩容并补 0
    self[chunk] |= 1 << within      # 置 1

fn contains(v):
    chunk = v / B
    within = v % B
    若 chunk 不存在 → 返回 false     # 越界即不在集合里
    return (self[chunk] & (1 << within)) != 0   # 测试该位
```

注意 `contains` 用 `self.0.get(chunk)` 做安全索引：当 `chunk` 超出已分配的字数时，直接判定为「不在集合里」，不会越界 panic。

#### 4.2.3 源码精读

`BitSet::insert`：

[bitset.rs:26-33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L26-L33) —— 先算 `chunk`/`within`；`resize(chunk + 1, 0)` 在需要时把向量补长并用 0 填充新字；最后 `self.0[chunk] |= 1 << within` 把目标位置 1。

`BitSet::contains`：

[bitset.rs:36-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L36-L41) —— 同样的定位；用 `let-else`（`self.0.get(chunk)` 返回 `None` 时直接 `return false`）处理未分配的字；最后 `(bits & (1 << within)) != 0` 判断该位。

#### 4.2.4 代码实践

**目标**：验证「字边界」两侧的值确实落在不同字里，从而确认 `chunk = v / B` 的正确性。

**步骤**（示例代码）：

```rust
// 示例代码
use typst_utils::BitSet;

fn main() {
    let mut set = BitSet::new();
    set.insert(0);
    set.insert(63); // 64 位平台上：chunk 0 的最高位
    set.insert(64); // 第一个跨字边界的值，进入 chunk 1

    assert!(set.contains(0));
    assert!(set.contains(63));
    assert!(set.contains(64));
    assert!(!set.contains(62));  // 没插过
    assert!(!set.contains(65));  // 没插过
    println!("{:?}", set); // 预期 [0, 63, 64]
}
```

**需要观察的现象**：`63` 和 `64` 虽然数值相邻，但因为 `63 / 64 == 0`、`64 / 64 == 1`，它们分属不同字；`contains(62)` 与 `contains(65)` 都为 `false`。

**预期结果**：Debug 输出为 `[0, 63, 64]`，所有断言通过。（在 32 位平台上 `BITS = 32`，请相应把 63/64 换成 31/32 观察。）

#### 4.2.5 小练习与答案

**练习 1**：`contains` 为什么用 `self.0.get(chunk)` 而不是 `self.0[chunk]`？

**答案**：`get` 返回 `Option`，当 `chunk` 超出已分配字数时返回 `None`，函数据此安全返回 `false`；若用 `self.0[chunk]`（直接索引）则会越界 panic。这是「未分配的字 = 该范围内的值都不在集合里」这一语义的安全实现。

**练习 2**：若连续 `insert(5)` 两次，第 5 位会被置成什么？

**答案**：仍是 1。`|= 1 << 5` 是幂等的：对已经是 1 的位再「或」一次结果不变，所以重复插入没有副作用，符合集合语义。

---

### 4.3 SmallBitSet：inline 与溢出的混合策略

#### 4.3.1 概念说明

`BitSet` 已经很省内存了，但即便只存 `{ 0 }`，它仍持有一个 `ThinVec`（虽然空时不分配，但毕竟是堆容器）。`SmallBitSet` 进一步优化：**把最常见的小值直接塞进一个 `usize` 字段 `low`，完全不走堆**；只有当值大到「一个字装不下」时，才落到内部的 `BitSet hi` 里。

这是一个经典的「fast path + slow path」设计：绝大多数场景命中 fast path（零分配），少数大值回退到通用方案。

#### 4.3.2 核心流程

设 \( B = \text{BITS} \)。`SmallBitSet` 有两个字段：

- `low: usize` —— 直接存值 \( < B \) 的位（内联，无分配）。
- `hi: BitSet` —— 存值 \( \ge B \) 的位，但**减去 \( B \) 后**再存。

为什么 `hi` 里要减去 `BITS`？因为 `low` 已经覆盖了 `0..B` 这个范围，如果 `hi` 再从原始值 `0` 开始存，它的第 0 个字就和 `low` 完全重复，白白浪费一个字。减去 \( B \) 后，`hi` 的第 0 个字对应原始范围 `B..2B`，与 `low` 无缝衔接又毫不重叠。

```
fn insert(v):
    if v < B:
        low |= 1 << v            # 内联
    else:
        hi.insert(v - B)         # 溢出，移位后委托给 BitSet

fn contains(v):
    if v < B:
        return (low & (1 << v)) != 0
    else:
        return hi.contains(v - B)
```

#### 4.3.3 源码精读

结构体与字段注释：

[bitset.rs:63-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L63-L72) —— `low` 存 `< BITS` 的值，`hi` 存 `> BITS` 的值；文档注释说明 `< 32/64`（随架构）的值内联存储，更大的值才分配。

`SmallBitSet::insert` / `contains`：

[bitset.rs:81-87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L81-L87) 与 [bitset.rs:90-96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L90-L96) —— 两个方法都用 `value < BITS` 分流；大值统一 `value - BITS` 委托给内部 `BitSet`，复用上一节的位运算逻辑。

#### 4.3.4 代码实践

这正是本讲指定的实践任务。

**目标**：用 `SmallBitSet` 收集一批跨度很大的索引，验证 inline 与溢出两条路径都正确，并断言 Debug 输出与 `contains` 行为。

**步骤**（示例代码，与源码自带测试 [bitset.rs:122-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L122-L146) 思路一致）：

```rust
// 示例代码
use typst_utils::SmallBitSet;

fn main() {
    let mut set = SmallBitSet::new();
    for v in [0, 1, 5, 64, 105, 208] {
        set.insert(v);
    }

    assert!(set.contains(0));
    assert!(set.contains(208));
    assert!(!set.contains(63)); // 关键：63 < 64，落在 low 里，但 low 没置这一位

    let s = format!("{set:?}");
    println!("{s}");
    assert_eq!(s, "[0, 1, 5, 64, 105, 208]");
}
```

**需要观察的现象**：

- `0, 1, 5` 都 `< 64`，走 `low` 内联路径，没有堆分配。
- `64, 105, 208` 都 `>= 64`，走 `hi` 路径，分别存成 `0, 41, 144`。其中 `208 - 64 = 144 = 2*64 + 16`，所以 `hi` 内部要扩容到 3 个字（chunk 0、1、2）。
- `contains(63)` 返回 `false`：`63 < 64`，检查的是 `low` 的第 63 位，而 `low` 只置了第 0、1、5 位。

**预期结果**：打印 `[0, 1, 5, 64, 105, 208]`，所有断言通过。这与源码测试 [bitset.rs:145](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L145) 的断言完全一致。

#### 4.3.5 小练习与答案

**练习 1**：若把 `SmallBitSet::insert` 里 `hi.insert(value - BITS)` 改成 `hi.insert(value)`（不减 `BITS`），会发生什么？

**答案**：功能上仍可工作（因为 `contains` 也得对应改），但 `hi` 的第 0 个字会完全重复 `low` 已经表示的 `0..B` 范围，浪费一个字；而且 `Debug` 输出会出错（同一个值会被 `low` 和 `hi` 各报告一次，或遍历范围算错）。减去 `BITS` 是为了消除这个重叠。

**练习 2**：在 64 位平台上，向 `SmallBitSet` 只 `insert(5)`，它有没有发生堆分配？

**答案**：没有。`5 < 64`，走 `low` 内联路径，只是把 `low` 的第 5 位置 1；`hi` 始终是空的 `BitSet`，而空 `BitSet` 的 `ThinVec` 不分配。这就是「小集合零分配」的体现。

---

### 4.4 Debug：遍历还原元素列表

#### 4.4.1 概念说明

位集合内部只有一串 0/1，没有「元素列表」这样的结构。那 `{:?}` 是怎么打印出 `[0, 1, 5, 64, …]` 的？答案是 **Debug 实现主动遍历所有「可能存在」的值，逐个调用 `contains` 检测**，把存在的收集进 `f.debug_list()`。

这是一种「以时间换空间 / 换实现简单度」的取舍：Debug 不维护额外索引，直接复用 `contains`，代码极简，代价是打印时要线性扫描整个可能范围。

#### 4.4.2 核心流程

关键问题是「要扫描到哪个值为止」。设已分配的字数为 `chunks`，那么可能存在的最大值上界是 `chunks * BITS`（不含）。遍历 `0 .. chunks * BITS`，对每个 `v` 调 `contains(v)`，为真则加入列表。

- 对 `BitSet`：`chunks = self.0.len()`（[bitset.rs:53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L53)）。
- 对 `SmallBitSet`：`chunks = 1 + self.hi.0.len()`（[bitset.rs:108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L108)）。这里的 `1` 就是 `low`（它等价于一个字），`self.hi.0.len()` 是 `hi` 的字数。

`SmallBitSet` 的 `1 + hi.0.len()` 正好呼应了上一节「`low` 占一个字范围、`hi` 接在其后」的布局：两者拼起来就是完整的可能值范围。

#### 4.4.3 源码精读

`BitSet` 的 Debug：

[bitset.rs:50-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L50-L61) —— `f.debug_list()` 开一个列表构建器；`for v in 0..chunks * BITS` 遍历；命中就 `list.entry(&v)`，最后 `list.finish()` 产出 `[...]` 形式。

`SmallBitSet` 的 Debug：

[bitset.rs:105-116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/bitset.rs#L105-L116) —— 结构与 `BitSet` 完全一致，只是 `chunks = 1 + self.hi.0.len()`，并且复用的是 `SmallBitSet::contains`（它自己会正确分流 `low`/`hi`）。

#### 4.4.4 代码实践

**目标**：通过「只插入一个大值」来观察 Debug 的遍历上界如何随之变化。

**步骤**（示例代码）：

```rust
// 示例代码
use typst_utils::SmallBitSet;

fn main() {
    let mut set = SmallBitSet::new();
    set.insert(200); // 200 >= 64，进入 hi：200-64=136 = 2*64+8 → hi 占 3 个字

    // 手算 Debug 遍历上界：chunks = 1 + hi字数 = 1 + 3 = 4，上界 = 4*64 = 256
    // 所以会扫 0..256，只有 200 命中
    println!("{:?}", set); // 预期 [200]
}
```

**需要观察的现象**：即便只插了一个值，Debug 也会扫描到 `256` 为止才停（因为 `hi` 占了 3 个字，加上 `low` 的 1 个字共 4 个字，上界 `4*64=256`）。输出仍是 `[200]`。

**预期结果**：打印 `[200]`。你可以把 `200` 改成 `70`（`70-64=6`，`hi` 占 1 个字，`chunks=2`，上界 `128`）观察输出仍是 `[70]`，但内部扫描范围变小了。

#### 4.4.5 小练习与答案

**练习 1**：`SmallBitSet` 的 Debug 里为什么是 `1 + self.hi.0.len()` 而不是 `self.hi.0.len()`？

**答案**：`low` 字段本身等价于一个 `usize` 字（管 `0..BITS`），必须把它算进总字数；`self.hi.0.len()` 只统计 `hi` 的字数。漏掉 `+1` 会导致 `0..BITS` 范围内的值永远不被扫描，Debug 漏掉所有内联值。

**练习 2**：如果集合里最大值是 `10`，但 `hi` 因某种原因仍有一个空字，Debug 会打印错误内容吗？

**答案**：不会打印「错误」内容，只是多扫描了一段全 0 的范围——那些值 `contains` 都返回 `false`，不会进入列表。代价仅是 Debug 稍慢，正确性不受影响。实际上 `SmallBitSet` 不会出现这种「无意义的空 `hi` 字」，因为 `hi` 只在插入大值时才扩容。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「已访问页面索引追踪器」的小示例，并用手算验证内部布局。

**任务**：

1. 用 `SmallBitSet` 表示「一批页面中哪些被访问过」。
2. 依次插入 `2, 7, 63, 64, 130`。
3. 用 `format!("{:?}", set)` 打印，并与手算结果对照。
4. 对其中两个值手算它们最终落在 `low` 还是 `hi` 的哪个字、哪一位，然后写一条断言验证 `contains`。

**参考分析**（64 位平台，`BITS = 64`）：

- `2, 7, 63` 都 `< 64`，进 `low`，分别置第 2、7、63 位。
- `64, 130` 都 `>= 64`，进 `hi`，存成 `0` 和 `66`。其中 `66 = 1*64 + 2`，所以 `hi` 占 2 个字（chunk 0、1）。
- Debug 遍历上界：`chunks = 1 + 2 = 3`，扫 `0..192`。命中 `2, 7, 63, 64, 130`。

**参考代码**（示例代码）：

```rust
use typst_utils::SmallBitSet;

fn main() {
    let mut visited = SmallBitSet::new();
    for page in [2, 7, 63, 64, 130] {
        visited.insert(page);
    }

    // 手算：130 落在 hi 的 chunk 1（(130-64)/64=1）、within 2（(130-64)%64=2）
    assert!(visited.contains(130));
    assert!(!visited.contains(131));

    let s = format!("{visited:?}");
    println!("{s}");
    assert_eq!(s, "[2, 7, 63, 64, 130]");
}
```

**预期结果**：打印 `[2, 7, 63, 64, 130]`，断言全部通过。这个练习同时覆盖了「分块存储（4.1）」「位运算定位（4.2）」「inline/溢出分流（4.3）」和「Debug 遍历还原（4.4）」。

## 6. 本讲小结

- `BitSet` 用 `ThinVec<usize>` 把一串位切成「每字 `BITS` 位」的块；存值 `v` 落在第 `v / BITS` 个字、字内第 `v % BITS` 位，内存占用取决于「最大值」而非「元素个数」。
- `insert` 用 `word |= 1 << within` 置位（必要时 `resize` 补零），`contains` 用 `get` 安全取字、`(word & (1 << within)) != 0` 测位；两者共享同一套定位逻辑。
- `SmallBitSet` 用 `low: usize` 内联存 `< BITS` 的值（零分配），`>= BITS` 的值减去 `BITS` 后委托给内部 `hi: BitSet`，消除 `low`/`hi` 范围重叠。
- `Debug` 不维护元素列表，而是遍历 `0 .. chunks * BITS` 逐个 `contains`，命中即输出；`SmallBitSet` 的总字数为 `1 + hi.0.len()`（`1` 代表 `low`）。
- 设计哲学：针对「期望很小」的集合做极致优化——fast path（内联）覆盖常见情况，slow path（`BitSet`）兜底大值，代码极简、只增不删。

## 7. 下一步学习建议

本讲我们看了一种「用位运算压缩存储」的集合。下一篇 [u2-l5 小集合 ListSet 与分组去重](u2-l5-listset-and-dedup.md) 会转向另一种小集合思路：`ListSet` 在「短列表线性查找」与「长列表排序后二分查找」之间按 `CUT_OFF` 自适应切换，并讲解 `lib.rs` 里的 `Rdedup`（保留后值的去重）与 `GroupByKey` 分组迭代器。学完后你会对 typst-utils 里「为小规模数据定制的数据结构」有更完整的认识。建议同时回头对比 `BitSet`（按值定位、O(1)）与 `ListSet`（按位置扫描、O(n) 或 O(log n)）各自的取舍。
