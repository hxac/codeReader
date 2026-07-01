# 无锁跳表结构

## 1. 本讲目标

本讲精读 `crossbeam-skiplist/src/base.rs` 中无锁跳表的**节点结构层**，也就是 `SkipMap` / `SkipSet` 能够做到「无锁、并发安全」的地基。

读完本讲，你应当能够：

- 画出 `Node` 的四字段内存布局，并解释为何 `tower` 必须放在最后；
- 说清 `refs_and_height` 如何用**一个机器字同时存储「塔高」与「引用计数」**，以及这样做省了什么；
- 理解 xorshift 随机数如何生成符合跳表概率分布的塔高，以及 `max_height` 提示如何加速查找；
- 跟着 `try_increment` / `decrement` / `finalize` 走一遍引用计数回收链路，并指出它与 `crossbeam-epoch` 的衔接点；
- 解释 `mark_tower` 为何要从**最高层向第 0 层逐层打删除标记**。

本讲只看**结构与节点级机制**，不展开 `insert` / `get` / `remove` 的完整并发协调（那是下一讲 u7-l2 的内容）。

## 2. 前置知识

本讲依赖 u5（crossbeam-epoch）已建立的认知，尤其是下面三点：

1. **epoch 延迟回收**：无锁结构里，删除方想 `free` 一个节点时，读取方可能还攥着它的指针，立即释放会导致 use-after-free。解法是把释放动作 `defer` 掉，等全局 epoch 推进两代之后再真正执行（详见 u5-l5 的「过期判据 \( \text{global\_epoch} - \text{bag\_epoch} \geq 2 \)」）。
2. **带标签的原子指针 `Atomic<T>`**：crossbeam-epoch 的 `Atomic<T>` 利用对齐指针低位恒为 0 的特性，把若干**标签位（tag）**塞进指针的同一个机器字里，从而能用一条 CAS 同时改指针与标签（详见 u5-l2）。本讲的删除标记就藏在 tag 的最低位。
3. **`Guard` 与 `'g` 生命周期**：`pin()` 返回 RAII 凭证 `Guard`；从 `Atomic` 读出的 `Shared<'g, T>` 活不过这次 pin，从而在类型层保证读出的引用不会被回收（详见 u5-l3）。

另外，本讲大量使用 `AtomicUsize` 的 `fetch_sub` / `compare_exchange`，这和标准库 `Arc` 的引用计数回收是同一套模式（Release 减计数 → 最后一个回收者做 Acquire fence）。

补充一个数据结构常识：**跳表（skip list）** 用多层链表模拟平衡树。最底层（level 0）是完整链表，每个节点以一定概率 \( p \) 向上「长」一层。查找时从最高层往右走、走不动就下降一层，期望查找代价 \( O(\log n) \)。本讲的 `tower` 就是节点的多层指针集合。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crossbeam-skiplist/src/base.rs` | 无锁跳表的核心实现：节点 `Node`、塔 `Tower`/`Head`、引用计数、高度生成、`mark_tower`、以及 `insert`/`remove`/`search` 等所有算法。本讲只读它的**节点结构层**部分。 |
| `crossbeam-skiplist/src/lib.rs` | crate 文档与门面，说明跳表是 lock-free、用 epoch 做 GC，并把 `base::SkipList` 重导出。 |

要点：`base.rs` 是一个近 2400 行的大文件，但**节点结构层**集中在文件前 400 行（`Node`、`Tower`、`Head`、`HotData`、引用计数、`mark_tower`），后面才是 `SkipList` 的各类操作。

## 4. 核心概念与源码讲解

### 4.1 Node 节点布局与 Tower 多层指针

#### 4.1.1 概念说明

跳表的每个元素是一个节点 `Node<K, V>`。一个节点要同时承载四样东西：

1. **键 `key`** 与 **值 `value`**（用户数据）；
2. **引用计数**：记录「有多少个 `Entry` 句柄 + 它被装进了多少层」指向自己，决定它何时能被释放；
3. **塔高**：这个节点有多少层指针；
4. **塔 `tower`**：一个长度等于塔高的原子指针数组，`tower[i]` 指向同层右邻居。

关键设计是：**塔是变长的**。不同节点的塔高不同（1 到 32 层），如果给每个节点都开满 32 层指针会浪费大量内存。所以 `tower` 被设计成「紧跟在节点固定字段之后的、长度可变的数组」，整个 `Node` 一次性按需分配。

#### 4.1.2 核心流程

节点在内存中按 `#[repr(C)]` 的字段顺序紧凑排列：

```text
+--------+-----+------------------+-----------------+
| value  | key | refs_and_height  | tower[0..height]|
+--------+-----+------------------+-----------------+
         固定字段                     变长尾随数组
```

- `#[repr(C)]` 强制字段顺序，保证 `tower` 永远是**最后一个字段**，这样它后面的变长数组才能用指针算术访问。
- 注释明确说：把 key、引用计数、高度放在离 tower 近的地方，是为了**遍历时的缓存局部性**——跳表查找最频繁的操作就是顺着 tower 读指针。
- `Tower` 本身是个零大小类型（ZST）占位 `pointers: [Atomic<Node<K,V>>; 0]`，真正的大小由分配时的 `height` 决定。

#### 4.1.3 源码精读

节点结构定义在 [base.rs:129-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L129-L145)：注意 `tower: Tower<K, V>` 是最后一个字段，注释解释了为何要紧挨着放。

`Tower` 占位类型在 [base.rs:36-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L36-L39)：`[Atomic<Node<K,V>>; 0]` 是个长度为 0 的数组，编译期不占空间，运行期靠 `get_layout` 接上变长部分。

跳表还有一个特殊的「头节点」`Head`，它**固定满高**（`MAX_HEIGHT` 层），位于 `SkipList` 结构体内，充当所有层的起点，见 [base.rs:90-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L90-L93)。

变长布局的关键在 `get_layout`，它把「固定字段」与「height 个原子指针的数组」拼成一个 `Layout`，见 [base.rs:198-206](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L198-L206)：

```rust
fn get_layout(height: usize) -> Layout {
    assert!((1..=MAX_HEIGHT).contains(&height));
    Layout::new::<Self>()
        .extend(Layout::array::<Atomic<Self>>(height).unwrap())
        .unwrap().0
        .pad_to_align()
}
```

`Layout::new::<Self>().extend(array)` 正是「固定头部 + 变长尾部」的标准写法。

`alloc` 在分配后立刻把 `refs_and_height` 初始化为 `(height - 1) | (ref_count << HEIGHT_BITS)`，并把 tower 区清零（全 null 指针），见 [base.rs:169-184](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L169-L184)。

#### 4.1.4 代码实践

**实践目标**：理解变长分配，亲眼看到一个节点占多少字节。

**操作步骤**：

1. 打开 [base.rs:198-206](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L198-L206) 的 `get_layout`。
2. 在本地写一段示例代码（非项目原代码，仅作演示）：

   ```rust
   // 示例代码：手算一个 Node<&str, u64> 在 height=3 时的布局
   use core::alloc::Layout;
   // 假设固定头部大小为 H、对齐为 A（用 std::mem::size_of / align_of 实测）
   // tower 部分是 3 个 Atomic<Self>，即 3 个原子指针（通常 8 字节）
   ```

3. 用 `core::mem::{size_of, align_of}` 在真实类型上测量 `Node` 固定字段部分大小，再套用 `get_layout` 的算法推算 `height=1` 与 `height=32` 时整个分配的字节数差。

**需要观察的现象**：塔高从 1 涨到 32 时，单个节点多用了大约 \( 31 \times 8 = 248 \) 字节（64 位下一个 `Atomic` 指针 8 字节）。

**预期结果**：能说清「为什么不能给所有节点都开满 32 层」——内存浪费会随节点数线性放大。

**待本地验证**：具体字节数取决于 `K`/`V` 的大小与对齐，请在本机实测。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tower` 必须是 `#[repr(C)]` 结构体的最后一个字段？

> **答**：因为 tower 是变长的。只有把它放在末尾，紧随其后的变长数组才能用「节点基地址 + 固定头偏移 + 索引 × 元素大小」的指针算术安全访问；若它不在末尾，其后的字段地址会随 tower 长度漂移，无法确定布局。

**练习 2**：`Head` 和普通 `Node` 的 tower 有什么本质区别？

> **答**：`Head` 的 tower 固定是 `MAX_HEIGHT`（32）层，作为整个跳表所有层的统一起点；普通 `Node` 的 tower 长度等于各自的随机塔高（1 到 32），按需分配。

---

### 4.2 refs_and_height 位打包：高度与引用计数共用一个机器字

#### 4.2.1 概念说明

每个节点既要存「塔高」又要存「引用计数」，并且两者都要被**并发原子地修改**。最朴素的写法是两个 `AtomicUsize` 字段。但跳表对缓存极其敏感（查找时每个节点都要碰这两个字段），多一个字段就意味着一次额外的缓存行加载。

crossbeam 的做法是**位打包（bit packing）**：把高度和引用计数塞进**同一个 `AtomicUsize`**，用一条原子指令同时操作两者。

布局约定（低位放高度，高位放引用计数）：

- 最低 5 位（`HEIGHT_MASK`）存 `height - 1`，取值范围 \( 0 \ldots 31 \)，对应塔高 \( 1 \ldots 32 \)；
- 其余高位存引用计数 `ref_count`。

这样既省了一个字段、又让「检查/修改引用计数」与「读取高度」共用同一次原子加载，缓存更友好。

#### 4.2.2 核心流程

三个常量定调，见 [base.rs:23-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L23-L30)：

\[
\text{HEIGHT\_BITS} = 5,\quad \text{MAX\_HEIGHT} = 2^5 = 32,\quad \text{HEIGHT\_MASK} = 2^5 - 1 = 31
\]

打包公式（`alloc` 中写入）：

\[
\text{refs\_and\_height} = (\text{height} - 1)\ \|\ (\text{ref\_count} \ll \text{HEIGHT\_BITS})
\]

拆包公式：

\[
\text{height} = (\text{word}\ \&\ \text{HEIGHT\_MASK}) + 1
\]

\[
\text{ref\_count} = \text{word} \gg \text{HEIGHT\_BITS}
\]

引用计数的含义（节点结构注释里写明）：它等于「指向本节点的 `Entry` 句柄数」加上「本节点被装入跳表的层数」。所以新插入一个塔高为 \( h \)、且要返回一个 `Entry` 的节点时，初始引用计数通常是 \( h + 1 \) 的相关计数（`insert` 里实际用初值 2，见 4.4）。

#### 4.2.3 源码精读

字段声明与含义注释在 [base.rs:137-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L137-L141)，明确说引用计数 = Entry 数 + 被装入的层数。

打包写入在 `alloc` 里，见 [base.rs:177-178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L177-L178)：

```rust
ptr::addr_of_mut!((*ptr).refs_and_height)
    .write(AtomicUsize::new((height - 1) | (ref_count << HEIGHT_BITS)));
```

读取塔高 `height()` 用掩码取低 5 位再加 1，见 [base.rs:209-212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L209-L212)：

```rust
fn height(&self) -> usize {
    (self.refs_and_height.load(Ordering::Relaxed) & HEIGHT_MASK) + 1
}
```

#### 4.2.4 代码实践

**实践目标**：亲手演算打包与拆包。

**操作步骤**：

1. 假设塔高 `height = 4`、引用计数 `ref_count = 3`。
2. 套公式算 `refs_and_height`：\( (4-1)\ |\ (3 \ll 5) = 3\ |\ 96 = 99 \)。
3. 反向拆包：\( 99\ \&\ 31 = 3 \)，加 1 得塔高 4；\( 99 \gg 5 = 3 \)，得引用计数 3。

**需要观察的现象**：低位和高位互不干扰，加减引用计数（`fetch_sub(1 << HEIGHT_BITS)`）不会污染高度位。

**预期结果**：能解释「为什么引用计数要 `fetch_sub(1 << HEIGHT_BITS)` 而不是 `fetch_sub(1)`」——因为每份引用计数占 5 位之上的一个步进，必须按 `1 << HEIGHT_BITS` 加减。

#### 4.2.5 小练习与答案

**练习 1**：把高度放在低位、引用计数放在高位，反过来行不行？

> **答**：理论上可以，但当前设计让「读取高度」只需一次掩码 `& HEIGHT_MASK`（不用移位），而高度是查找时几乎每节点都要读的热数据，省一次移位有意义。引用计数用 `>> HEIGHT_BITS` 读，调用频率低得多。

**练习 2**：塔高上限为什么是 32？

> **答**：因为只留了 5 位存高度（`HEIGHT_BITS = 5`，\( 2^5 = 32 \)）。5 位足够覆盖任何现实规模的跳表（32 层对应期望 \( 2^{32} \) 量级节点），又把剩下的高位都留给了引用计数。

---

### 4.3 xorshift 高度生成与 max_height 提示

#### 4.3.1 概念说明

跳表要保证期望 \( O(\log n) \) 的查找，关键在于**塔高服从几何分布**：节点高度为 \( k \) 的概率约为 \( p^{k-1}(1-p) \)。crossbeam 取 \( p = \tfrac12 \)，即每往上一层概率减半。

为此需要一个**快、可并发、无锁**的伪随机源。标准库的 `rand` 不 `no_std`、且非并发友好，于是 base.rs 自己实现了一个极简的 **xorshift** 生成器，并把种子放在跳表共享的 `HotData` 里。

此外，查找要从「当前实际最高层」开始往下走才有意义。但「实际最高层」会随插入变化，且精确维护它需要额外同步。crossbeam 的折中是维护一个**只增不减的 `max_height` 提示**：它可能偏大，但配合一个「跳过空层」的快速循环就能修正，避免了「维护精确最高层」的同步开销。

#### 4.3.2 核心流程

`random_height` 三步走（见 [base.rs:707-751](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L707-L751)）：

1. **xorshift 推进种子**：读取共享 `seed`，做三次异或移位（`<<13`、`>>17`、`<<5`），写回。这是 Marsaglia 的 32 位 xorshift。
2. **把随机数映射成高度**：取 `trailing_zeros(num) + 1`。一个均匀随机数的末尾连续 0 位数服从几何分布——恰好给出「每层概率减半」的塔高分布，再 `min` 上限 `MAX_HEIGHT`。

   \[
   \Pr(\text{trailing\_zeros} \geq k) = 2^{-k} \;\Rightarrow\; \Pr(\text{height} \geq k+1) \approx 2^{-k}
   \]

3. **收缩过高的塔**：如果算出的高度远超当前跳表里已有的最高塔（通过检查 head 在各层的指针是否为空判断），就把它往下调，避免出现「孤零零的超高塔」。
4. **更新 `max_height` 提示**：用 CAS 把提示抬到新高度（只增不减）。

查找时（`search_bound` / `search_position`）从 `max_height` 开始，先用一个快速循环跳过 head 为空的层，再正式逐层下降，见 [base.rs:846-857](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L846-L857)。

#### 4.3.3 源码精读

xorshift 主体在 [base.rs:713-719](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L713-L719)：

```rust
let mut num = self.hot_data.seed.load(Ordering::Relaxed);
num ^= num << 13;
num ^= num >> 17;
num ^= num << 5;
self.hot_data.seed.store(num, Ordering::Relaxed);
let mut height = cmp::min(MAX_HEIGHT, num.trailing_zeros() as usize + 1);
```

> 注意：这里对 `seed` 用 `load` 后 `store`，**不是原子的 RMW**。两个线程并发调用时可能读到同一个 `num`、各自算出相同高度。这不影响正确性——跳表不要求高度唯一或完美服从分布，只要「大体上是几何分布」即可；用 `Relaxed` 是因为这里没有任何同步语义依赖。

收缩过高度的逻辑在 [base.rs:726-735](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L726-L735)：当 `height >= 4` 且 head 在 `height-2` 层的指针为空时，就把高度减一，循环往复。

`max_height` 提示的 CAS 抬升在 [base.rs:738-749](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L738-L749)（只增不减），字段本身定义在 `HotData`，见 [base.rs:450-452](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L450-L452)，注释明说「never decreases」。

#### 4.3.4 代码实践

**实践目标**：验证 xorshift + `trailing_zeros` 确实产生几何分布的塔高。

**操作步骤**：

1. 把 [base.rs:713-719](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L713-L719) 的四行 xorshift 抄进一个示例程序（示例代码），从 `seed = 1` 开始迭代 10000 次。
2. 对每次产生的 `num`，统计 `min(32, trailing_zeros(num)+1)` 的分布（高度 1、2、3…各出现多少次）。

**需要观察的现象**：高度 1 的约占一半，高度 2 约占四分之一，高度 3 约占八分之一……大致符合 \( \Pr(\text{height}=k) \approx 2^{-k} \)。

**预期结果**：用一张表或柱状图展示「高度 vs 出现次数」，确认是几何衰减。**待本地验证**具体数值。

#### 4.3.5 小练习与答案

**练习 1**：`max_height` 为什么设计成「只增不减」？

> **答**：精确维护「当前实际最高层」需要在删除最后一个高层节点时原子地回退计数，引入额外争用与复杂的状态机。只增不减虽可能让提示偏大，但查找开头那个「跳过空层」的快速循环只需几次空指针检查就能修正，代价远低于精确同步。

**练习 2**：两个线程并发 `random_height` 时读到相同 `num`，会出问题吗？

> **答**：不会。两个节点塔高相同完全合法，跳表算法不依赖高度互异或精确分布，只需高度「大致服从几何分布」即可保证期望复杂度。

---

### 4.4 引用计数 try_increment / decrement / finalize

#### 4.4.1 概念说明

无锁跳表里，一个节点可能同时被「跳表结构本身（若干层）」「若干个用户持有的 `Entry` 句柄」「正在遍历的线程（经 `Guard` 保护）」共同引用。何时释放节点，必须靠**引用计数**精确裁决：计数归零，才真正销毁。

这套机制和标准库 `Arc` 同源，但有两个关键不同：

1. **计数与 epoch 联动**：`decrement` 发现计数归零后，不是立刻 `free`，而是把 `finalize` 闭包 `defer_unchecked` 到 epoch 宽限期之后——因为此刻可能还有别的线程持着裸指针正在读它（详见 u5-l5）。
2. **「计数为 0 即已入队回收」的不变量**：一旦计数掉到 0，节点就已被排进回收队列，**绝不能再被加回去**，否则会 double-free。所以 `try_increment` 必须先检查计数非 0。

#### 4.4.2 核心流程

引用计数的三条路径：

- **`try_increment`（加引用）**：CAS 循环。先读 `refs_and_height`；若高位（引用计数部分）为 0，说明节点已入队待删，返回 `false` 拒绝；否则尝试 `checked_add(1 << HEIGHT_BITS)` 并 CAS，成功返回 `true`，溢出则 panic。
- **`decrement`（减引用）**：`fetch_sub(1 << HEIGHT_BITS, Release)`。若旧值的引用计数部分为 1（即这次减完归零），做一次 `fence(Acquire)`，再 `guard.defer_unchecked(finalize)`。
- **`finalize`（真正销毁）**：`drop_in_place` 掉 key 和 value（运行析构），再 `dealloc` 释放内存。

这是教科书式的「Release 计数 + 最后一个回收者 Acquire fence」模式：Release 让本线程对节点数据的写入对其他线程可见；最后一个减到 0 的线程做 Acquire fence，确保它看到所有先前持有者对节点数据的全部写入，之后才安全销毁。

#### 4.4.3 源码精读

`try_increment` 的「计数为 0 即拒绝」逻辑在 [base.rs:222-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249)，核心两步：

```rust
// 计数为 0 → 已入队待删，拒绝（避免 double-free）
if refs_and_height & !HEIGHT_MASK == 0 {
    return false;
}
// 溢出保护 + CAS 加 1<<HEIGHT_BITS
let new_refs_and_height = refs_and_height
    .checked_add(1 << HEIGHT_BITS)
    .expect("SkipList reference count overflow");
```

`decrement` 在 [base.rs:293-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L293-L304)：

```rust
unsafe fn decrement(self, guard: &Guard) {
    if self.refs_and_height
        .fetch_sub(1 << HEIGHT_BITS, Ordering::Release)
        >> HEIGHT_BITS  // 取旧值的引用计数部分
        == 1            // 旧值是 1 → 减完归零
    {
        fence(Ordering::Acquire);
        unsafe { guard.defer_unchecked(move || Node::finalize(self.ptr.as_ptr())) }
    }
}
```

`finalize` 在 [base.rs:252-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L252-L262)：先 `drop_in_place` key/value，再 `dealloc`。

`insert` 在创建新节点时把初始引用计数设为 **2**（一个给将返回的 `Entry`，一个给 level 0 的链入），见 [base.rs:1051-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1051-L1065)，注释解释了「2」的来源。后续每往更高层链入一次，会再 `increment`。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `remove` 的引用计数变化，看清回收链路。

**操作步骤**：

1. 阅读 `remove` 在 [base.rs:1283-1322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1283-L1322)：它先 `RefEntry::try_acquire`（`try_increment` +1）拿走节点作为返回的 `Entry`，再 `mark_tower` 逻辑删除，然后逐层 `compare_exchange` 物理摘除，每摘掉一层 `n.decrement`（−1）。
2. 假设某节点塔高 2、且此刻没有外部 `Entry`：被摘除前引用计数 = 2（两层链入）。`remove` 先 `try_acquire` 变 3，再 `mark_tower`，然后逐层摘除各 `decrement`（3→2→1），最后用户 drop 掉返回的 `Entry` 时再 `decrement`（1→0）触发 `defer finalize`。

**需要观察的现象**：引用计数在「摘除各层」与「Entry 释放」两个维度上独立递减，谁先谁后都安全，只有归零那一次才安排销毁。

**预期结果**：能说清「为什么 `decrement` 不能直接 `free`，而要 `defer_unchecked`」——因为 epoch 宽限期内别的线程可能还攥着这个节点的裸指针在读。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `try_increment` 在计数为 0 时必须返回 `false`，而不是把它从 0 加回 1？

> **答**：计数掉到 0 的那一刻，节点已被排进 epoch 回收队列（`defer finalize` 已发出）。若再加回 1，等 epoch 推进两代后 `finalize` 照常执行 free，而新引用者却还持着指针，就会 use-after-free / double-free。所以「0 即终态」是不可逆的。

**练习 2**：`decrement` 里 `fetch_sub(..., Release)` 之后为何还要 `fence(Acquire)`？

> **答**：Release 只保证「本线程之前的写」在减计数前对其他线程可见；但「最后一个回收者」必须看见**所有**先前持有者写过的数据才能安全析构。Acquire fence 让这最后一个线程与之前所有 Release 形成 happens-before，确保看到完整数据。这与 `Arc::drop` 完全同构。

---

### 4.5 mark_tower 自顶向下的逻辑删除标记

#### 4.5.1 概念说明

无锁结构里「删除一个节点」分两步：先**逻辑删除**（标记「它已经不在了」），再**物理删除**（真正把它的指针从链表里摘掉）。为什么不能一步到位？因为可能有多个线程同时在操作相邻节点，直接物理改指针会和它们冲突；而打一个「标记位」可以用单条原子指令完成，且让所有后续读者一致地看到「这个节点已删」。

crossbeam 把删除标记藏在**塔指针的 tag 最低位**（这正是 u5-l2 讲的 epoch 带标签指针）：某个节点的 level-0 指针 tag=1，就表示该节点已被逻辑删除。

`mark_tower` 的任务是把这个节点**每一层**指针都打上标记。关键是顺序——**从最高层向第 0 层（自顶向下）逐层标记**。

#### 4.5.2 核心流程

`mark_tower` 的循环（见 [base.rs:327-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L327-L348)）：

```text
对 level 从 height-1 递减到 0：
    tag = tower[level].fetch_or(1, SeqCst) 的旧 tag
    如果 level == 0 且 旧 tag == 1：说明别人已经标记过第 0 层 → return false
返回 true（我成功标记了第 0 层，即我完成了删除）
```

两个要点：

1. **用 `fetch_or(1)` 打标**：把 tag 最低位置 1，同时拿回旧 tag。旧 tag=1 表示已被别人标记过。
2. **以 level 0 为仲裁点**：谁成功把 level 0 从 0 翻成 1，谁就是「执行删除的那一个」，返回 `true`；若发现 level 0 已是 1，说明别人先删了，自己返回 `false`。

为什么自顶向下？核心是**让并发读者能安全地协助清理**。读者在任意一层看到一个已标记的指针，就知道这个节点「正在被删」，可以绕过它或帮忙摘除（`help_unlink`）。自顶向下保证了：当 level 0 被标记时，上面所有层都已标记完毕——此时节点已从所有层的「有效链接」中退出，读者绝不会把一个「半标记」节点误当成有效节点继续往下走。若反过来自底向上，会出现「level 0 已标但上层未标」的窗口，上层读者可能把该节点当成有效前置节点记录下来，破坏查找的正确性。

配合查找时的 `help_unlink`：读者在 [base.rs:882-893](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L882-L893) 发现 `succ.tag() == 1` 时，会尝试 CAS 把已删节点从当前层摘掉（见 [base.rs:758-781](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L758-L781)），实现「删除是协作式的」。

#### 4.5.3 源码精读

`mark_tower` 全文在 [base.rs:327-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L327-L348)。注意它对 tag 的读取用了 `epoch::unprotected()`（假守卫），因为这里只关心 tag 位、不解引用指针，所以不需要 pin 保护，注释 [base.rs:331-336](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L331-L336) 解释了这一点。

判定节点是否已删的 `is_removed` 只看 level 0 的 tag，见 [base.rs:351-361](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L351-L361)——再次印证「level 0 是删除与否的唯一权威判据」。

调用点：`insert` 替换旧节点时在 [base.rs:1089](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1089) 调 `r.mark_tower()`；`remove` 在 [base.rs:1297](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1297) 调；`RefEntry::remove` 在 [base.rs:1698](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1698) 调。

#### 4.5.4 代码实践

**实践目标**：把本讲的两个核心机制（tower 多层指针 + mark_tower 顺序）画成图，建立直觉。

**操作步骤**：

1. 画一个 **3 层（MAX_HEIGHT 之外、实际最高层 level 2）、4 个节点**的跳表示意图，键值依次为 `10, 20, 30, 40`。标注：
   - 节点 10：塔高 3，`tower[0]→20, tower[1]→20, tower[2]→20`
   - 节点 20：塔高 1，`tower[0]→30`
   - 节点 30：塔高 2，`tower[0]→40, tower[1]→40`
   - 节点 40：塔高 1，`tower[0]→null`
   - `Head` 满高 3 层，分别指向节点 10。
2. 假设要删除节点 30。在图上模拟 `mark_tower`：从 level 1（最高）开始 `fetch_or(1)` 给 `tower[1]` 打标，再到 level 0 给 `tower[0]` 打标。
3. 对照 [base.rs:327-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L327-L348) 回答：如果改成自底向上（先标 level 0 再标 level 1），在「标完 level 0、还没标 level 1」的瞬间，一个正在 level 1 遍历的读者会看到什么？

**需要观察的现象**：自底向上时，level 1 的读者仍会把节点 30 当成有效节点（因为它的 level 1 指针未标记），可能把它记录为前驱，随后下降到 level 0 才发现它已删——这会破坏 `Position` 里 `left`/`right` 的一致性，需要重启搜索。

**预期结果**：用自己的话总结「自顶向下保证了 level 0 被标记时，整塔都已退出有效链接，读者不会把半标记节点当成有效前驱」。

#### 4.5.5 小练习与答案

**练习 1**：`mark_tower` 为什么以「level 0 是否被我翻成 1」作为「我是否完成了删除」的判据，而不是看最高层？

> **答**：因为 level 0 是唯一包含所有节点的完整链表，也是删除的「最终落点」。多个线程可能并发 `mark_tower` 同一个节点，高层可能被不同线程各自 `fetch_or` 一次（幂等），但 level 0 的 0→1 翻转只能被一个线程「亲眼见证旧值为 0」——用旧 tag 是否为 1 仲裁出唯一的删除者，避免重复扣 `len`、重复回收。

**练习 2**：`mark_tower` 里读 tag 为何能用 `epoch::unprotected()` 而不需要 `Guard`？

> **答**：因为它只 `fetch_or` 指针的 tag 位、读回旧 tag，**不解引用**指针指向的对象。不解引用就不会触发 use-after-free，所以不需要 pin 保护。这是 epoch API 里常见的「只碰 tag」优化。

---

## 5. 综合实践

把本讲的四个机制串起来，做一个**源码阅读 + 图示**的综合任务。

**任务**：以「插入一个塔高为 3 的新节点」为主线，把结构层知识串成一条因果链。

**步骤**：

1. **算高度**：阅读 [base.rs:707-751](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L707-L751) 的 `random_height`，假设本次 xorshift 算出 `height = 3`。
2. **分配节点**：阅读 [base.rs:169-184](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L169-L184) 的 `alloc`，写出新节点的 `refs_and_height`：初始 `ref_count = 2`（见 [base.rs:1051-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1051-L1065)），塔高 3，故 `refs_and_height = (3-1) | (2 << 5) = 66`。tower 区被清成 3 个 null 指针。
3. **画出此时的节点**：标注四字段 `value/key/refs_and_height=66/tower[0..3]`，并画出 `tower` 的三个指针格子。
4. **链入与计数**：跟踪 `insert_internal` 在 level 0 链入成功后（[base.rs:1076-1093](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1076-L1093)），再往 level 1、level 2 链入（[base.rs:1136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1136) 起）。理解「初始 ref_count=2 = 1 个 Entry + level 0 的 1 次链入；更高层链入会另行 increment」。
5. **删除它**：假设随后 `remove` 该节点，对照 [base.rs:1297-1322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1297-L1322) 画出 `mark_tower` 自顶向下打标（level 2 → level 1 → level 0），再逐层 `compare_exchange` 物理摘除并 `decrement`。

**交付物**：两张图（插入后的跳表片段、删除过程中 level 0 被标记的瞬间）+ 一段文字，说清「`refs_and_height` 的位打包如何让高度与引用计数共用一次原子操作」「`mark_tower` 自顶向下如何避免读者把半标记节点当有效前驱」「`decrement` 归零后为何要走 epoch `defer` 而非直接 free」。

**待本地验证**：若想看到真实运行行为，可在 `try_increment` / `decrement` / `mark_tower` 各加一行 `eprintln!`（仅作学习，勿提交），用 `crossbeam_utils::thread::scope` 起多线程并发 `insert`/`remove` 同一批键，观察引用计数与标记的时序。注意加日志会改变时序，结论以源码逻辑为准。

## 6. 本讲小结

- `Node<K,V>` 用 `#[repr(C)]` 把 `value/key/refs_and_height` 固定字段放前面、变长 `tower` 放最后，靠 `Layout::extend` 一次性按塔高分配，兼顾缓存局部性与零浪费。
- **位打包**：`refs_and_height` 一个机器字里，低 5 位存 `height-1`、高位存引用计数，加减引用计数用 `1 << HEIGHT_BITS` 步进，读写高度和计数共用一次原子操作。
- **xorshift + `trailing_zeros`** 生成符合几何分布的塔高（每层概率减半），配合「收缩过高塔」与「只增不减的 `max_height` 提示 + 跳过空层」让查找从合适的层起步。
- **引用计数**采用 `Arc` 式「Release 减 + 最后回收者 Acquire fence」模式，但归零后不直接 free，而是 `defer_unchecked(finalize)` 交 epoch 宽限期后再销毁；`try_increment` 在计数为 0 时拒绝，守住「0 即终态」防 double-free。
- **`mark_tower`** 把删除标记藏在塔指针 tag 最低位，**自顶向下**逐层 `fetch_or(1)`，以 level 0 的 0→1 翻转仲裁唯一删除者，保证半标记节点不会被读者误当有效前驱。

## 7. 下一步学习建议

本讲只搭好了**节点结构层**的地基。下一讲 **u7-l2 跳表操作与 epoch 集成** 将在这套结构上展开完整的并发算法：

- `search_bound` 如何逐层下降并**协助摘除**（`help_unlink`）已标记节点；
- `insert` / `get` / `remove` 如何在多个线程并发修改下用 CAS 协调、处理失败重试；
- 节点释放如何全面依赖 crossbeam-epoch 的 `Guard`/`defer_unchecked`，以及 `SkipMap`/`SkipSet` 如何把 `base::SkipList` 包装成 `BTreeMap` 式的易用接口。

建议带着本讲的两张图（节点布局、mark_tower 时序）进入下一讲，并随时回看 `refs_and_height` 的位打包与引用计数不变量——它们是理解所有并发操作的钥匙。如果想验证整体正确性，可在读完 u7-l2 后进入 **u7-l3 测试、loom 与并发正确性**，用 miri/loom 实跑这些无锁代码。
