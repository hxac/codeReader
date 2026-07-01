# 无锁跳表结构

## 1. 本讲目标

本讲深入 `crossbeam-skiplist` 的实现内核 `base.rs`，剖析**无锁跳表（lock-free skip list）**最底层的「积木」——节点（`Node`）。读完本讲，你应当能够：

1. 画出 `Node` 在内存中的字段布局，并说清为什么 `tower` 必须是「变长尾数组」。
2. 解释 `refs_and_height` 这一个机器字如何同时打包「引用计数」与「塔高度」，以及它带来的算术技巧。
3. 理解 xorshift 随机高度生成的几何分布原理，以及 `max_height` 提示为何只增不减。
4. 掌握引用计数 `try_increment` / `decrement` / `finalize` 三件套如何与 epoch 延迟回收协作。
5. 说清楚 `mark_tower` 为什么必须**从最高层向第 0 层**逐层标记删除。

本讲只看「结构定义」与「节点级原语」，**不**展开并发 `insert` / `get` / `remove` 的完整搜索流程（那是下一讲 u7-l2 的内容）。后者建立在本讲打下的地基之上。

## 2. 前置知识

本讲是专家层内容，需要你已具备以下认知（前序讲义已建立）：

- **跳表是什么**：一种有序的、概率性的数据结构，用多层链表实现 \(O(\log n)\) 的查找。最底层是一条完整的有序链表，每往上一层都跳着保留一部分节点，形似「快车道」。查找时从最高层起步，沿「快车道」快速逼近目标后再逐层下降。详见维基百科 [skip list](https://en.wikipedia.org/wiki/Skip_list)。
- **epoch 内存回收（u5-l1 ~ u5-l5）**：无锁结构里删除一个节点时，可能有别的线程正持着它的指针在读，不能立刻释放。crossbeam 用 epoch 机制把释放动作「延迟两个代次」后才真正执行。本讲里 `finalize` 的调用全部经 `guard.defer_unchecked(...)` 走这条延迟通道。
- **Atomic 指针与低位标签（u5-l2）**：`crossbeam_epoch::Atomic<T>` 是一个 `AtomicPtr`，因为指针按对齐，**低若干位恒为 0**，可塞一个「标签（tag）」。`Shared::tag()` 读标签，`fetch_or(1, ...)` 把最低位置 1。本讲里「删除标记」就存在每个塔指针的 tag 位里。
- **CachePadded 与伪共享（u2-l2）**：热点字段单独占一条缓存行，避免多核乒乓。本讲里 `SkipList` 的 `HotData` 就用 `CachePadded` 包裹。

一句话回顾上承结论（u5-l5）：被删节点的 `finalize` 不是在引用计数归零时立刻执行，而是塞进 epoch 垃圾袋，等到「全局代次 − 入袋代次 ≥ 2」才由 `collect` 真正销毁。本讲会把这条结论落到跳表节点上。

## 3. 本讲源码地图

本讲涉及两个文件，主战场是 `base.rs`：

| 文件 | 作用 | 本讲用到 |
|------|------|----------|
| [base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) | 无锁跳表的**全部实现内核**：节点定义、引用计数、塔操作、搜索、插入、删除、迭代器都在这里。 | `Node`/`Tower`/`Head` 结构、`refs_and_height` 打包、`random_height`、`try_increment`/`decrement`/`finalize`、`mark_tower` |
| [lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs) | crate 文档与模块声明。把 `base::SkipList` 重导出，并在 `std` 特性下提供 `SkipMap`/`SkipSet` 包装。 | 文档级背景：跳表的并发语义、epoch GC、与 BTreeMap 的取舍 |

> 提示：`base.rs` 有 2300+ 行，但本讲只读它最前面的 ~400 行（结构定义与节点原语）和 `random_height` 一段。后续 `search_bound`/`insert_internal`/`remove` 留给 u7-l2。

## 4. 核心概念与源码讲解

### 4.1 Node 节点与 refs_and_height 打包布局

#### 4.1.1 概念说明

跳表里每一个键值对住在一个 `Node` 里。无锁并发对 `Node` 的设计提出两个苛刻要求：

1. **变长塔**：不同节点的高度（也就是塔的层数）不同，由概率生成。我们不希望为每个节点都按最大高度分配 32 个指针槽（浪费内存），而希望「高度几，塔就恰好有几层」。这等价于 C 里的「柔性数组成员（flexible array member）」。
2. **单字打包计数与高度**：每个节点的「当前被多少处引用」和「塔有多高」都是高频读写的元信息。把它们塞进**同一个 `AtomicUsize`**，既能用一条原子指令同时操作，又能让遍历时这俩信息与塔指针落在同一条缓存行上——这是 `#[repr(C)]` 字段顺序的精心安排。

#### 4.1.2 核心流程：内存布局

先看三个布局常量，它们定义了「高度」占多少位：

[base.rs:24-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L24-L30) —— 定义 `HEIGHT_BITS=5`，因此 `MAX_HEIGHT=32`，塔最高 32 层；低 5 位存高度，高位存引用计数。

再看 `Node` 本体（注意 `#[repr(C)]` 固定字段顺序）：

[base.rs:123-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L123-L145) —— `Node` 字段顺序为 `value`、`key`、`refs_and_height`、`tower`。注释明确说明 `tower` 必须是最后一个字段（因为它是变长的），而 `key`/计数/高度紧贴 `tower` 以提升遍历时的缓存命中率。

它的内存形象如下（以高度 3 的节点为例）：

```
Node<K,V> 分配（高度 = 3）:
┌────────────────────┐  ← 固定头部（Layout::new::<Self>()）
│ value: V           │
│ key:   K           │
│ refs_and_height    │  ← 一个机器字，同时编码 [引用计数 | 高度]
├────────────────────┤  ← 紧随其后的变长尾数组
│ tower[0] (level 0) │  Atomic<Node>
│ tower[1] (level 1) │  Atomic<Node>
│ tower[2] (level 2) │  Atomic<Node>
└────────────────────┘  （没有 tower[3]，因为高度只到 2）
```

#### 4.1.3 源码精读：refs_and_height 的编码与解码

打包规则：低 `HEIGHT_BITS=5` 位存 `(height - 1)`，其余高位存 `ref_count`。也就是

\[
\text{refs\_and\_height} = (\text{ref\_count} \ll 5) \;|\; (\text{height} - 1)
\]

解码高度（`height()`）：

[base.rs:208-212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L208-L212) —— 用 `& HEIGHT_MASK` 取出低 5 位，再 `+1` 还原成真实高度（1..=32）。

分配时直接写好这个字：

[base.rs:169-184](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L169-L184) —— `Node::alloc(height, ref_count)`：按 `get_layout(height)` 申请一块内存，把 `refs_and_height` 写成 `(height - 1) | (ref_count << HEIGHT_BITS)`，并把塔指针区 `write_bytes(0, height)` 清零（即全部初始化为 null）。key/value 此时**故意不写**，故函数标 `unsafe`。

对应的布局计算（变长的关键）：

[base.rs:197-206](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L197-L206) —— `get_layout` 用 `Layout::new::<Self>().extend(Layout::array::<Atomic<Self>>(height))` 把「固定头部」和「height 个原子指针数组」拼成一块连续、按对齐填充的内存。这就是 Rust 版的「柔性数组」实现。

> **算术小结**：因为高度占低 5 位，给引用计数加 1 就是给整个字加 \(1 \ll 5 = 32\)；要单独读引用计数，就把整个字逻辑右移 5 位（`>> HEIGHT_BITS`）。这套「加 32 / 减 32 / 右移 5」的算术贯穿后面所有引用计数操作。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是把「打包/拆包」内化成肌肉记忆：

1. **目标**：手算一个具体节点的 `refs_and_height` 值。
2. **步骤**：
   - 假设某节点高度 `height = 3`，初始引用计数 `ref_count = 2`（这正是 `insert_internal` 里 `Node::alloc(height, 2)` 的初值，下一节会解释为何是 2）。
   - 套用公式 \((\text{ref\_count} \ll 5) \;|\; (\text{height}-1)\) 算出这个字的十进制值。
3. **预期**：\((2 \ll 5) \;|\; 2 = 64 \;|\; 2 = 66\)。
4. **反向验证**：用 `66 & 0b11111 = 2`，`+1 = 3` 得到高度；`66 >> 5 = 2` 得到引用计数。把你的手算结果与 `height()` [base.rs:208-212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L208-L212) 的逻辑对一遍。

#### 4.1.5 小练习与答案

**练习 1**：为什么把高度存成 `height - 1` 而不是 `height`？
> **答案**：高度取值范围是 1..=32（共 32 个值），而 5 位二进制能表示 0..=31。存 `height - 1` 正好把 1..=32 映射到 0..=31，5 位刚好够用、不浪费。

**练习 2**：若把 `HEIGHT_BITS` 从 5 调成 4，会出现什么后果？
> **答案**：`MAX_HEIGHT` 降到 16，跳表最高 16 层；同时引用计数占的高位变多、单节点最大引用计数变大。这是「高度精度」与「引用计数容量」之间的位预算权衡。

### 4.2 Tower 多层指针与变长塔

#### 4.2.1 概念说明

`Tower` 是节点里那组「指向下一节点的多层原子指针」。难点在于：Rust 的结构体字段大小必须在编译期确定，而我们要的是「每个节点的塔长度不同」。crossbeam 的解法分两步：

1. 把 `Tower` 定义成一个**零大小类型（ZST）占位符** `[Atomic<Node>; 0]`，仅作「尾部锚点」；
2. 真正的指针数组在 `Node::alloc` 时按 `height` 动态追加在节点尾部（见 4.1.3 的 `get_layout`）。

此外，跳表还有一个特殊的「头节点（Head）」：它不是真实键值对，只是各层链表的公共起点，因此它内嵌一个**固定满高**（`MAX_HEIGHT` 个槽）的塔。

#### 4.2.2 核心流程：三种「塔视角」

```
Head (内嵌于 SkipList)            普通节点 Node
┌────────────────────┐            ┌────────────────────┐
│ pointers[0..32]    │ ——满高 32  │ value/key/refs_and_│
│ (静态数组)         │            │ height             │
└────────────────────┘            │ tower[0..height)   │ ← 变长尾数组
                                  └────────────────────┘
        ↓ as_tower() 把 Head 也当作 Tower 看待，统一访问接口
```

`TowerRef` / `NodeRef` 是带 provenance（指针来源）的「瘦引用」——因为 `&Tower` 这种普通引用对一个 ZST 没有访问尾数组的权限（stacked borrows 下会失效），必须自己持 `NonNull` 并 `unsafe` 地按下标取槽。

#### 4.2.3 源码精读

`Tower` 与 `Head` 的定义：

[base.rs:32-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L32-L39) —— `Tower` 是 `#[repr(C)]` 的 ZST，仅含 `[Atomic<Node<K, V>>; 0]` 占位。

[base.rs:86-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L86-L93) —— `Head` 持一个静态的满高数组 `[Atomic<Node<K, V>>; MAX_HEIGHT]`。

`TowerRef::get_level` —— 按下标取某一层的原子指针，靠裸指针 `add(index)` 跨过 `index` 个 `Atomic<Node>` 步长：

[base.rs:74-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L74-L84) —— `get_level(index)` 把塔基址当 `*const Atomic<Node>` 做 `add(index)`，取第 `index` 层。这就是「变长数组按下标寻址」的本质。

`Head::as_tower` —— 把 `&Head` 指针 cast 成 `&Tower`，从而 Head 与普通节点能共用同一套 `get_level` 接口：

[base.rs:105-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L105-L109) —— `as_tower` 用 `NonNull::from(self).cast::<Tower<K,V>>()` 把 Head「伪装」成 Tower。因为 `Head.pointers` 与 `Tower.pointers` 同为 `#[repr(C)]` 的首字段，二者指针数值相等，cast 合法。

#### 4.2.4 代码实践

1. **目标**：验证「Head 与 Tower 的指针数值相等」这一 cast 安全性前提。
2. **步骤**：在 [base.rs:105-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L105-L109) 的 `as_tower` 处阅读，确认 `Head` 与 `Tower` 都是 `#[repr(C)]` 且**首字段都是 `pointers`**（一个是满高数组、一个是零长数组）。这是「指针数值相等」的物理保证。
3. **观察**：在 `Head::get_level` [base.rs:116-120](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L116-L120) 与 `TowerRef::get_level` [base.rs:79-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L79-L83) 之间，注意二者取槽方式不同（前者用 `pointers.get_unchecked`，后者用裸指针 `add`），但都依赖「首字段即塔基址」。
4. **预期**：理解为什么 crossbeam 不直接给 `Tower` 一个泛型高度参数——因为 Rust 泛型不能表达「每实例不同长度」，只能用 ZST + 尾数组布局绕开。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Tower` 要做成 ZST 而不是直接把 `[Atomic<Node>; MAX_HEIGHT]` 放进 `Node`？
> **答案**：那样每个节点无论实际多高都会背上 32 个指针槽（32 × 8 = 256 字节），海量节点时浪费惊人。ZST 占位 + 变长尾数组让节点只占「真实需要」的空间。

**练习 2**：`TowerRef` / `NodeRef` 为什么要 `unsafe fn new`，而不直接用普通引用？
> **答案**：普通 `&Tower` 对 ZST 没有访问其尾部字节的权限（stacked borrows 模型下读取变长数组会 UB）。`TowerRef` 自己持 `NonNull` 并在文档里要求调用者保证「该指针对其真实塔大小可访问」，把安全责任上移到调用点。

### 4.3 xorshift 高度生成与 max_height 提示

#### 4.3.1 概念说明

跳表的性能完全依赖一个概率假设：**节点高度服从几何分布**。若每个节点向上「再长一层」的概率都是 \(p\)，则高度恰为 \(h\) 的概率

\[
P(\text{height} = h) = (1-p)\, p^{\,h-1},
\]

期望最大高度约为 \(\log_{1/p}(n)\)，查找代价期望 \(O(\log n)\)。crossbeam 取 \(p = 1/2\)。

为此需要一个**快**而**足够随机**的随机数发生器。crossbeam 没用标准库的 `rand`（重、且非 `no_std` 友好），而是用一个轻量到极致的 **xorshift** 算法，再用「尾零个数（trailing zeros）」一行把随机数映射成几何分布的高度。

#### 4.3.2 核心流程：random_height 的三步

[base.rs:707-751](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L707-L751) —— `random_height` 全函数。

它做三件事：

1. **xorshift 推进种子**（713-717 行）：从 `hot_data.seed` 读旧种子，做 `^= num << 13; ^= num >> 17; ^= num << 5;` 三步（Marsaglia 的 32 位 xorshift 常数），写回。种子是跳表共享的一个 `AtomicUsize`，多线程并发推进——这里用 `Relaxed`，因为高度的「绝对随机性」不重要、「够分散」即可，偶发种子相同只是生成同样的高度，不影响正确性。
2. **映射成几何分布高度**（719 行）：`height = min(MAX_HEIGHT, num.trailing_zeros() + 1)`。一个均匀随机数的最低连续 0 位数 \(k\) 满足 \(P(k \ge t) = 2^{-t}\)，因此 \(P(\text{height} \ge h) = 2^{-(h-1)}\)，正好是 \(p=1/2\) 的几何分布。
3. **就地裁剪 + 更新提示**（720-749 行）：见下一小节。

#### 4.3.3 源码精读：裁剪与 max_height 提示

裁剪（避免无谓的超高节点）：

[base.rs:720-735](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L720-L735) —— 当 `height >= 4` 且 Head 的第 `height-2` 层指针为 `null` 时，把 `height` 减 1。语义是「如果当前最高塔都还没到这个高度，就别造一个鹤立鸡群的超高节点」。这能把节点高度钳制在「现实存在的最高塔 +1」附近，省内存、也减少空层遍历。

更新 `max_height` 提示（查找起点）：

[base.rs:737-749](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L737-L749) —— 用 CAS 把 `hot_data.max_height` 单调推到 `height`（只增不减）。

而 `max_height` 的定义在 `HotData`：

[base.rs:442-453](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L442-L453) —— `HotData` 把「种子」「元素数 `len`」「`max_height`」三个高频读写字段打包，整体再用 `CachePadded`（见 u2-l2）单独占一条缓存行，避免与跳表其它字段的写互相伪共享。注释点明 `max_height` 是「查找起点提示，且永不减小」。

> **为什么 max_height 只增不减？** 减小需要遍历确认「确实没有更高的塔了」，这在并发下既贵又无意义——把它当乐观提示即可，查多了空层至多多花几次 `Relaxed` 的指针读。`SkipList` 初始 `max_height = 1`（[base.rs:494-505](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L494-L505)）。

#### 4.3.4 代码实践

1. **目标**：亲手验证 `trailing_zeros` 的几何分布。
2. **步骤**：写一个独立的小程序（这是**示例代码**，不在仓库中），对 xorshift 三步生成的若干个 `u32` 统计 `trailing_zeros`：
   ```rust
   // 示例代码：统计 trailing_zeros 分布
   let mut num: u32 = 1;
   let mut hist = [0usize; 33];
   for _ in 0..1_000_000 {
       num ^= num << 13; num ^= num >> 17; num ^= num << 5;
       hist[(num.trailing_zeros() as usize).min(32)] += 1;
   }
   println!("{:?}", hist);
   ```
3. **观察**：`hist[0]`（即 height=1）约占 50%，`hist[1]`（height=2）约占 25%，`hist[k]` 约占 \(1/2^{k+1}\)。
4. **预期**：分布近似 \(P(k)=2^{-(k+1)}\)，验证了 \(p=1/2\) 的几何高度假设。若运行环境受限，标注「待本地验证」即可，分布结论可由数学直接推出。

#### 4.3.5 小练习与答案

**练习 1**：xorshift 的种子是所有线程共享的 `AtomicUsize`，会不会有线程安全/正确性问题？
> **答案**：不会有**正确性**问题。`load`/`store` 用 `Relaxed`，并发推进可能让两个线程拿到同一个旧种子、生成同一个高度，但跳表只要求高度「统计上」服从几何分布即可，偶发重复无害。这体现了「能 Relaxed 就不 Acquire」的无锁性能纪律。

**练习 2**：`max_height` 永不减小，会不会让查找越来越慢？
> **答案**：不会显著变慢。即使提示偏大，多出的也只是几次 `Relaxed` 的 null 指针读（很快下降到真实高度）。换来的好处是更新 `max_height` 无需昂贵的全局扫描，是无锁可维护的。

### 4.4 引用计数 try_increment / decrement / finalize

#### 4.4.1 概念说明

跳表节点没有「所有者」——它被多个结构同时引用：塔的每一层链接算一份、每个对外暴露的 `Entry`/`RefEntry` 句柄算一份。所以节点用**引用计数**管理生命周期，而且这个计数就藏在 4.1 讲的 `refs_and_height` 高位里。

引用计数的语义是：

\[
\text{ref\_count} = (\text{节点被安装在塔中的层数}) + (\text{指向它的 Entry 句柄数})
\]

当计数归零，意味着「它在表里彻底断链、也没人持有句柄」，可以销毁 key/value 并释放内存。但——**不能立刻 free**，因为可能有别的线程正持着从 `Shared` 读出的指针在比较 key（见 u5-l5）。所以「归零」触发的 `finalize` 要走 epoch 延迟回收。

#### 4.4.2 核心流程：三条原语

- **`try_increment`**：尝试给引用计数 +1（实际 `+ 1<<5`）。**只在计数非零时成功**：若已是 0，说明节点已在销毁队列里，再 +1 会复活一个即将被 free 的对象 → 双重释放。返回 `true`/`false`。
- **`decrement`**：给引用计数 −1（实际 `- 1<<5`）。若旧值的高位 == 1（即这次减完恰好归零），插一道 `Acquire` 栅栏，然后用 `guard.defer_unchecked(|| Node::finalize(...))` 把销毁动作交给 epoch。
- **`finalize`**：drop key、drop value、dealloc 内存。`#[cold]` 标注因为它只在节点寿终时调用一次。

```
            ref_count 变化（高度3节点，初值=2：1层link + 1个Entry）
insert:     alloc(ref_count=2)  ──► 安装 level0 成功
            每装上一层 +1      ──► 2 → 3 → 4 ...
get/remove: try_increment +1    ──► 拿到 Entry
drop Entry: decrement -1
unlink 每层: decrement -1       ──► 归零时 defer finalize → epoch 两代后真正 free
```

#### 4.4.3 源码精读

`try_increment`（CAS 自增，零值拒绝）：

[base.rs:214-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L214-L249) —— 先 `load`，若 `refs_and_height & !HEIGHT_MASK == 0`（引用计数位为零）直接返回 `false`；否则 `checked_add(1 << HEIGHT_BITS)` 防溢出（溢出直接 panic），用 `compare_exchange_weak` 重试自增。

`decrement`（减到 0 则延迟 finalize）：

[base.rs:292-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L292-L304) —— `fetch_sub(1 << HEIGHT_BITS, Release)` 返回**旧值**；把旧值 `>> HEIGHT_BITS` 取出旧引用计数，若它 == 1（减完即 0），先 `fence(Acquire)` 建立 Happens-before，再 `guard.defer_unchecked(move || Node::finalize(...))`。这一处就是与 u5-l5 衔接的关键调用点。

`finalize`：

[base.rs:251-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L251-L262) —— `drop_in_place` 分别析构 key、value，再 `Node::dealloc` 释放内存。

> **为什么 `decrement` 用 Release、归零处用 Acquire？** 这是经典的「引用计数 + 延迟释放」内存序模式：每次 `fetch_sub(Release)` 保证「此前对该节点数据的写」对最终的销毁者可见；归零时的 `fence(Acquire)` 与之配对，确保销毁线程能看到该节点**全部**历史写，避免在仍有未同步写时 `drop` 掉数据。这是 u5 系列里 epoch/内存序讨论在跳表上的具体落地。

#### 4.4.4 代码实践

1. **目标**：跟踪一次 `insert` 里引用计数的完整生命周期。
2. **步骤**：阅读 `insert_internal` [base.rs:1013-1234](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1013-L1234)，重点在三处：
   - [base.rs:1050-1055](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1050-L1055)：`Node::alloc(height, 2)`，注释解释初值 2 = 「一个给返回的 RefEntry，一个给 level-0 链接」。
   - [base.rs:1192-1193](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1192-L1193)：每成功装上一层更高层，`fetch_add(1 << HEIGHT_BITS)`。
   - [base.rs:1207-1208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1207-L1208)：装某层失败回退时，`fetch_sub(1 << HEIGHT_BITS)` 抵消。
3. **观察**：把 4.4.2 里的「计数 = 层数 + Entry 数」公式逐行对上：level-0 link（+1，含在初值 2 里）、每个 RefEntry（+1，含在初值 2 里）、每个更高层 link（+1）。
4. **预期**：能口述「一个高度 3、成功装满 3 层、返回 1 个 RefEntry 的节点，最终 ref_count = 3(层) + 1(Entry) = 4」。当该 Entry 被 drop 且节点被从 3 层全部 unlink，ref_count 依次 4→3→2→1→0，最后一次 `decrement` 触发 `defer finalize`。

#### 4.4.5 小练习与答案

**练习 1**：`try_increment` 为什么不能对引用计数为 0 的节点成功？
> **答案**：引用计数为 0 意味着节点已被某次 `decrement` 判定寿终、`finalize` 已塞进 epoch 垃圾袋（虽尚未真正 free）。此时若再 +1 让它「复活」，等 epoch 两代后 `finalize` 照常执行 drop+dealloc，就会释放仍在被使用的内存 → use-after-free / 双重释放。

**练习 2**：为什么 `finalize` 不直接在 `decrement` 里同步调用，而要 `defer_unchecked` 交给 epoch？
> **答案**：因为 `decrement` 把 ref_count 减到 0 只代表「没有任何结构再持有它」，但**可能有并发搜索线程**在 pin 窗口内仍持着 `Shared` 指针在读它的 key。立刻 free 会撞上这些读者。交给 epoch 延迟两个代次，可保证所有在途读者都已 unpin，此时 free 才安全（详见 u5-l5 的「宽限期」）。

### 4.5 mark_tower 自顶向下的逻辑删除标记

#### 4.5.1 概念说明

删除一个节点分两步（Harris-Michael 经典思路）：

1. **逻辑删除（标记）**：把节点每一层的「出向指针」打上删除标记（存在 tag 位里），宣布「这个节点已退出服务」。这是删除操作的**线性化点**——标记一旦打上，该节点对所有线程都视为已删。
2. **物理摘除（unlink）**：真正把前驱的指针绕过它、接到后继，并 `decrement` 引用计数。这一步可由删除者自己做，也可由任何撞见它的搜索线程「顺便帮忙（helping）」。

关键难点：**标记必须按从最高层到第 0 层的顺序打**。本节就讲清为什么。

#### 4.5.2 核心流程：mark_tower 的遍历顺序与胜负判定

`mark_tower` 从 `level = height-1` 一路降到 `level = 0`，每层用 `fetch_or(1, SeqCst)` 把该层出向指针的 tag 最低位置 1：

```
mark_tower (height=3):
  level 2:  fetch_or(1)  →  打标记（最高层先打）
  level 1:  fetch_or(1)  →  打标记
  level 0:  fetch_or(1)  →  打标记  ← 线性化点
            若返回的旧 tag 已经是 1 ⇒ 有人抢先删了 ⇒ 返回 false（我输了）
```

只有 **level 0 的标记**是裁决胜负的依据：谁把 level-0 从 0 翻成 1，谁就是「唯一赢家」，返回 `true`；若 `fetch_or` 在 level 0 发现已经是 1，说明别人已赢了，返回 `false`。

#### 4.5.3 源码精读

[base.rs:326-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L326-L348) —— `mark_tower`：`for level in (0..height).rev()` 即从高到低；每层 `fetch_or(1, SeqCst, ...)`，`if level == 0 && tag == 1 { return false; }` 判负。

判定「是否已删」的快查 `is_removed`：

[base.rs:350-361](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L350-L361) —— 只看 level 0 的 tag 是否为 1。因为按「自顶向下」约定，level 0 被标记 ⇒ 所有更高层必然已被标记 ⇒ 节点彻底封死。

调用点（删除主路径）：`remove` 先 `try_increment` 抢到 Entry，再 `mark_tower`：

[base.rs:1296-1334](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1296-L1334) —— `if n.mark_tower() { ... }`：赢了就 `len` 减 1，逐层 CAS 把前驱绕过本节点，每成功一层 `n.decrement(guard)`；输了（已被别人删）则 `n.decrement(guard)` 退回刚才 `try_increment` 借的那一引用，返回 `None`。

#### 4.5.4 为什么必须自顶向下？（本讲核心论证）

这是本节最重要的理解点。假设反过来，**自底向上**打标记（先 level 0，后高层），会出什么问题？

考虑删除者 D 正在删节点 N，与一个并发插入者 I 竞争：

- 若 D 先把 **level 0** 打了标记（N 已逻辑删除），但 **level 1 还没打**；
- 此时 I 在 level 1 上做插入，读到 N 的 level-1 出向指针（**未标记**），于是它 CAS 把一个新节点 X 接到「N 之后、N 的 level-1 后继之前」；
- 这步 CAS 会**成功**——因为 level 1 没打标记，I 看不到 N 正在被删；
- 结果：N 已经在 level 0 逻辑删除，却在 level 1 上新挂了一个后继 X。X 在 level 1 可达，但它的「level-0 前驱链」从一开始就是断的，成为一个**悬挂、难以正确 unlink** 的节点，破坏跳表不变量。

**自顶向下**正好堵死这个窗口：等 D 把标记打到 level 0（线性化点）时，**所有更高层早就打满了标记**。任何插入者 I 无论在哪个层触及 N，都会先看到一个**已标记**的出向指针，于是不会（也不允许）往 N 后面挂新节点（`insert_internal` 在 [base.rs:1149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1149) 正是靠 `next.tag() == 1` 判定并中止建塔）。因此：

> **不变量**：一旦 level 0 被标记，N 在**所有层**都已封死，不可能再被新插入挂接。删除因此是可线性化的。

换一种说法：标记的「传播方向」与查找的「下降方向」一致——查找自顶向下，那么「先封住高层、最后封住 level 0」就能保证任何并发的查找/插入要么完全在删除前看到 N（合法），要么在某层看到已标记指针而绕开/协助清理，**不会**看到「上层未标记、下层已标记」这种半删的撕裂视图。

#### 4.5.5 代码实践

1. **目标**：在源码里确认「插入者会因标记指针而中止」，闭环验证 4.5.4 的论证。
2. **步骤**：
   - 在 `insert_internal` 建塔循环里读 [base.rs:1146-1151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1146-L1151)：`if next.tag() == 1 { break 'build; }`——发现自己刚插的节点正被别人删，就停止建高层。
   - 再看 `mark_tower` [base.rs:330-344](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L330-L344) 的 `(0..height).rev()` 顺序。
3. **观察**：把两处对上——删除者从高到低打标记，插入者从低到高建塔并在每层检查标记。两者交错时，插入者最多把新节点接上 level 0，但只要删除者的标记波浪（自顶向下）追上，插入者就会在更高层撞见标记而 `break 'build`。
4. **预期**：能复述「自顶向下标记 + 插入者每层查标记」二者共同保证了删除的线性化与无悬挂节点。

#### 4.5.6 小练习与答案

**练习 1**：`mark_tower` 为什么用 `fetch_or` 而不是 `compare_exchange` 来打标记？
> **答案**：打标记是「无条件把 tag 最低位置 1」，不关心指针原本指向谁；`fetch_or(1)` 一条指令即可，且返回**旧值**供我们读取旧 tag 判胜负。`compare_exchange` 会要求「预期指针 == 某值」，但后继指针随时可能被别人改（比如被 unlink 改向），用 CAS 反而要处理更多失败重试。`fetch_or` 对「只动 tag、不动指针」最贴合。

**练习 2**：如果两个线程同时对同一节点调用 `mark_tower`，会发生什么？
> **答案**：每层 `fetch_or` 幂等，两线程都安全执行；但只有**先把 level 0 从 0 翻成 1** 的那个线程拿到 `tag==0`（返回 `true`，赢家，负责 `len -= 1` 与 unlink）；另一线程在 level 0 看到的是 `tag==1`，返回 `false`（输家），仅 `decrement` 退回自己 `try_increment` 借的引用。胜负由 level 0 唯一裁决，绝无重复删除。

## 5. 综合实践

把本讲四个最小模块（`Node` 布局、`Tower`、引用计数、`mark_tower`）串起来，完成下面这个「画图 + 推演」任务。

**任务背景**：构造一个极小的跳表，仅含 4 个键值对，键依次为 `10, 20, 30, 40`，假设随机生成的高度分别是 `height(10)=1`、`height(20)=2`、`height(30)=1`、`height(40)=3`，外加 Head（满高 32，图里画到第 3 层即可）。

**步骤 1：画示意图**。在纸上画出 3 层（level 2/1/0）的跳表，Head 在最左，按上述高度排布 4 个节点；用箭头标出每个节点的 `tower[level]` 指针，并补出每个节点的 `(ref_count, height)`（提示：按「层数 + Entry 数」算 ref_count）。

参考答案（tower 指针连接关系，假设所有插入返回的 RefEntry 都已 drop，故 Entry 数 = 0）：

```
level 2: Head ─────────────────────────────► 40 ──► null
level 1: Head ─────────► 20 ────────────────► 40 ──► null
level 0: Head ─► 10 ───► 20 ─► 30 ──────────► 40 ──► null
            (1,1)      (2,2) (1,1)           (3,3)
```

各节点 `(ref_count, height)`：`ref_count = 层数 + Entry 数`。Entry 已 drop 时 Entry 数 = 0，故 `10→(1,1)`、`20→(2,2)`、`30→(1,1)`、`40→(3,3)`。若你假设某些 RefEntry 仍被持有，请把对应 ref_count 加上持有的句柄数并写明。

**步骤 2：推演删除 `20`**。模拟 `remove(&20)`：
1. 搜索定位到 20，`try_increment` 把 ref_count 从 2 加到 3（借一份给返回的 RefEntry）。
2. `mark_tower` 自顶向下：level 1 `fetch_or`（翻 0→1，胜），level 0 `fetch_or`（翻 0→1，胜）→ 返回 `true`。
3. 逐层 unlink：level 1 把 Head→20 改成 Head→40（CAS 成功，`decrement`，ref_count 3→2）；level 0 把 10→20 改成 10→30（CAS 成功，`decrement`，ref_count 2→1）。
4. 返回 RefEntry（ref_count 仍为 1）。当调用者 drop 这个 RefEntry 时，最后一次 `decrement` 让 ref_count 1→0，触发 `guard.defer_unchecked(Node::finalize)`，经 epoch 两代后真正析构 key/value 并 dealloc。

**步骤 3：回答核心问题**（本讲的点睛之笔）：假如 `mark_tower` 改成自底向上（先 level 0 后 level 1），在上面的场景里，删除 `20` 与「并发插入一个 height=2 的新键 25」交错时会出现什么危险？请用 4.5.4 的论证写一段话（提示：插入 25 可能在 level 1 成功接到 20 之后，而 20 的 level 0 已标记 → 25 悬挂）。

> 若你无法直接运行跳表（`base.rs` 是底层内核，没有独立可执行入口），上面的实践属于**源码阅读 + 纸上推演型**，符合「无法确定运行结果时标注」的要求。可选用的高层验证方式见下一讲 u7-l2（用 `SkipMap` 多线程并发操作来观察最终一致性）。

## 6. 本讲小结

- **`Node` 用 `#[repr(C)]` + 变长尾数组**实现「高度几、塔就几层」：固定头部是 `value/key/refs_and_height`，尾部是 `height` 个 `Atomic<Node>` 指针（[base.rs:129-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L129-L145)）。
- **`refs_and_height` 单字打包**：低 5 位存 `(height-1)`，高位存引用计数；自增引用即 `+1<<5`，读引用即 `>>5`（[base.rs:24-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L24-L30)）。
- **xorshift + trailing_zeros** 生成 \(p=1/2\) 的几何分布高度；`max_height` 是「只增不减」的查找起点提示，三者与 `len`/`seed` 同住一个 `CachePadded<HotData>` 缓存行（[base.rs:707-751](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L707-L751)、[base.rs:442-453](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L442-L453)）。
- **引用计数 = 层数 + Entry 数**：`try_increment` 拒绝零计数节点（防复活），`decrement` 归零时经 `Release/Acquire` 配对后 `defer_unchecked(finalize)`，把销毁交给 epoch 延迟两代回收（[base.rs:214-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L214-L262)、[base.rs:292-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L292-L304)）。
- **`mark_tower` 自顶向下**：每层 `fetch_or(1)`，以 level 0 的胜负裁决唯一赢家；这个顺序保证 level 0 标记时所有高层已封死，杜绝并发插入挂接到「半删」节点上，实现删除的线性化（[base.rs:326-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L326-L348)）。

## 7. 下一步学习建议

本讲只搭好了「节点积木」与「节点级原语」。下一讲 **u7-l2 跳表操作与 epoch 集成** 将把这些积木拼成完整的并发操作：

- 精读 `search_bound` 的逐层下降与撞见已删节点时的 `help_unlink` 协作；
- 完整走通并发 `insert` / `get` / `remove` 的无锁协调，并把本讲的引用计数与 `mark_tower` 放回它们的真实调用上下文；
- 看 `map.rs` / `set.rs` 如何把裸 `SkipList` 包装成对用户友好的 `SkipMap` / `SkipSet`。

建议在进入 u7-l2 前，回头确认两件事：一是 u5-l5 的「epoch 两代延迟回收」结论（本讲 `decrement` 末尾的 `defer_unchecked` 就靠它兜底）；二是 u5-l2 的「Atomic 指针低位 tag」机制（本讲 `mark_tower` 的 `fetch_or(1)` 与 `tag()` 全在用它）。随后用 **u7-l3** 的 loom/miri/tsan 工具链来验证这些无锁不变量在所有交错下都成立，为整个 crossbeam-skiplist 单元收尾。
