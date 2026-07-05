# Node 与 Tower 的内存布局

## 1. 本讲目标

本讲是进入 `base.rs` 无锁算法源码的**第一站**。在阅读任何并发算法（搜索、插入、删除）之前，必须先看懂一个最基础的问题：**一个跳表节点在内存里到底长什么样？**

学完本讲，你应当能够：

- 说出 `HEIGHT_BITS`、`MAX_HEIGHT`、`HEIGHT_MASK` 三个常量的含义，并能手算「高度」与「引用计数」是如何被压缩进同一个 `AtomicUsize` 的。
- 解释 `Node` 为什么用 `repr(C)`、字段为什么是这个顺序、以及为什么「塔（tower）」必须是最后一个字段。
- 描述 `Node::alloc` / `get_layout` / `dealloc` / `finalize` 如何为一个**变长塔**的节点分配、初始化与回收内存。
- 说明 `Tower`、`TowerRef`、`Head`、`NodeRef` 各自的角色，并能解释为什么它们要用 `NonNull`（裸指针）而不是普通的 `&T` 引用。

本讲**只讲数据结构本身的内存布局**，不展开搜索/插入/删除算法（那是第三单元的内容），也不展开 epoch 回收的完整协议（那是 u2-l6 的内容）——但会讲到二者与本讲布局的衔接点。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 跳表的「塔」是什么

跳表的每个节点不只有一个「下一个」指针，而是有**一摞指针**，层数从 1 到 `MAX_HEIGHT` 不等。这一摞指针在源码里被称为 **tower（塔）**。塔越高，节点在跳表里「站得越高」，查找时越可能被高层指针直接跳过/选中。每个节点的塔高是插入时**随机决定**的（详见 u3-l10），因此整个跳表的期望查找复杂度是 \(O(\log n)\)。

关键点：**不同节点的塔高不一样**，所以每个节点占用的内存大小也不一样。这就是本讲要解决的核心难题——如何在一个固定结构体里塞进一个「长度可变的指针数组」。

### 2.2 C 语言的「柔性数组成员」技巧

在 C 里，要在结构体末尾放一个长度可变的数组，常用「柔性数组成员（flexible array member）」：

```c
struct Node {
    Value value;
    Key   key;
    size_t refs_and_height;
    Atomic* tower[];   // 长度在运行时决定
};
```

`crossbeam-skiplist` 用 unsafe Rust 复刻了这一技巧：把 `tower` 字段定义成**长度为 0 的数组**（一个零大小占位），然后在分配内存时**额外多分配** `height` 个指针的空间，紧接着结构体头部存放。访问时用裸指针算术去索引这片「藏在末尾」的动态数组。

### 2.3 引用计数与高度为什么要挤在一起

无锁数据结构里，一个节点是否「还活着」要用**引用计数**判断（被 `Entry` 句柄或被某一层链接持有时计数大于 0）。同时节点还需要记住自己的塔高（用于遍历和回收时计算内存大小）。

这两个值都**很小**（高度最多 32，引用计数最多几十亿），却都要被原子地访问。如果用两个原子变量，既要多占内存，又可能在并发下让「读高度」和「读计数」看到不一致的中间态。作者的解法是：**把二者打包进同一个 `AtomicUsize`**，用一次原子读写同时拿到二者。这个打包字段就叫 `refs_and_height`。

> 提示：如果你对 `AtomicUsize`、`Ordering`、`NonNull` 这些 Rust 并发/指针原语还不熟，本讲会用到的只有「原子整数」和「裸指针」两个概念，先建立直觉即可，内存序的细节留到 u5-l17。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `src/base.rs` | 底层无锁跳表的全部实现 | `Node` / `Tower` / `Head` / `TowerRef` / `NodeRef` 的定义与 `Node::alloc` 等方法 |
| `src/alloc_helper.rs` | 自实现的 `Global` 分配器 | `Node::alloc` 内部调用的 `Global::allocate` / `deallocate` |

回顾 u1-l3 的分层：`base.rs` 是最底层的无锁原语，`map.rs` / `set.rs` 都在它之上做封装。本讲我们直接站在最底层，看一块「裸」节点长什么样。

## 4. 核心概念与源码讲解

### 4.1 高度编码：把高度与引用计数压进一个原子字

#### 4.1.1 概念说明

`refs_and_height` 是一个 `AtomicUsize`（平台相关的无锁原子整数，64 位平台上是 64 位）。作者把这一个字拆成两段：

- **低位 5 比特**：存放 `height - 1`（注意是减 1 后的值）。
- **高位其余比特**：存放 `ref_count`（引用计数），其存储形式是 `ref_count << HEIGHT_BITS`。

这样设计的好处：高度和引用计数**永远是一致地一起被读到**（一次原子加载），而且只占一个字的内存。

#### 4.1.2 核心流程

设 `H = HEIGHT_BITS = 5`，则三个常量关系为：

\[ \text{MAX\_HEIGHT} = 1 \ll \text{HEIGHT\_BITS} = 2^5 = 32 \]

\[ \text{HEIGHT\_MASK} = (1 \ll \text{HEIGHT\_BITS}) - 1 = 2^5 - 1 = 31 = 0b11111 \]

编码（写入 `refs_and_height`，见 `Node::alloc`）：

\[ \text{refs\_and\_height} = (\text{height} - 1) \;\big|\; (\text{ref\_count} \ll \text{HEIGHT\_BITS}) \]

解码高度（见 `Node::height`）：

\[ \text{height} = (\text{refs\_and\_height} \;\&\; \text{HEIGHT\_MASK}) + 1 \]

解码引用计数（见 `NodeRef::decrement` / `try_increment`，用右移实现）：

\[ \text{ref\_count} = \text{refs\_and\_height} \gg \text{HEIGHT\_BITS} \]

由于 `usize` 是平台相关的，引用计数能用的位数也随平台变化：

- 64 位平台：\(64 - 5 = 59\) 位，最大引用计数 \(2^{59}-1\)。
- 32 位平台：\(32 - 5 = 27\) 位，最大引用计数 \(2^{27}-1 = 134\,217\,727\)。

#### 4.1.3 源码精读

三个常量集中定义在文件顶部：

[base.rs:23-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L23-L30) — 定义 `HEIGHT_BITS=5`、`MAX_HEIGHT=1<<5=32`、`HEIGHT_MASK=(1<<5)-1=31`。

`Node::height` 用掩码取低位再加 1，把「存储值」还原成「真实高度」：

[base.rs:208-212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L208-L212) — `height()` 用 `Relaxed` 载入后 `& HEIGHT_MASK + 1`。

`Node::alloc` 在分配时一次性写入打包值，公式正是 \((\text{height}-1)\;|\;(\text{ref\_count}\ll 5)\)：

[base.rs:177-178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L177-L178) — 写入 `(height - 1) | (ref_count << HEIGHT_BITS)`。

引用计数的增减则以 `1 << HEIGHT_BITS`（即 32）为步长，对 `refs_and_height` 做原子加减，从而**只动高位、不碰低位的高度**：

[base.rs:222-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249) — `try_increment` 先用 `& !HEIGHT_MASK` 判断高位（引用计数）是否为 0，再用 `checked_add(1 << HEIGHT_BITS)` 原子地加 1。

#### 4.1.4 代码实践

**实践目标**：亲手算一遍位编码，确认你理解了「高度占几位、引用计数占几位」。

**操作步骤**（纯手算，无需运行）：

1. 取 `MAX_HEIGHT=32`、`HEIGHT_BITS=5`。
2. 回答：`refs_and_height` 中**高度**占几位？**引用计数**占几位（分 64 位与 32 位平台）？最大引用计数是多少？
3. 取一个具体例子：`height=3`、`ref_count=2`，计算 `refs_and_height` 的数值，并用 `height()` 的解码公式验证能还原出 3。

**预期结果**：

- 高度占低位 **5 比特**（存的是 `height-1`，范围 0..=31，对应真实高度 1..=32）。
- 引用计数占高位：64 位平台上 **59 比特**，最大 \(2^{59}-1 = 576\,460\,752\,303\,423\,487\)；32 位平台上 **27 比特**，最大 \(2^{27}-1 = 134\,217\,727\)。
- `height=3, ref_count=2`：\((3-1)\;|\;(2\ll 5) = 2\;|\;64 = 66\)。解码高度：\(66\;\&\;31 = 2\)，\(+1 = 3\) ✓。

#### 4.1.5 小练习与答案

**练习 1**：为什么存的是 `height - 1` 而不是 `height`？

**答案**：因为高度最小是 1（至少要有第 0 层指针）。如果直接存 `height`，5 比特能表示 0..=31，高度 32 就溢出了。存 `height - 1` 后范围变成 0..=31，正好 5 比特装下整个 1..=32。

**练习 2**：`try_increment` 里判断「引用计数是否为 0」用的是 `refs_and_height & !HEIGHT_MASK == 0`，为什么不是 `refs_and_height == 0`？

**答案**：因为低位 5 比特存的是高度（几乎总是非 0），所以 `refs_and_height` 本身基本不会等于 0。必须用 `& !HEIGHT_MASK` 屏蔽掉低位的高度，只看高位是否全 0，才能正确判断「引用计数归零」。

---

### 4.2 Node 结构体与 repr(C) 变长内存布局

#### 4.2.1 概念说明

`Node<K, V>` 是跳表里存放一个键值对的「真身」。它有三个**定长**字段（`value`、`key`、`refs_and_height`），外加一个**变长**的 `tower` 字段（塔，长度随节点高度变化）。

Rust 的普通结构体不支持「末尾变长数组」，所以作者用了一个组合技：

1. 用 `#[repr(C)]` **锁定字段顺序**，保证 `tower` 永远在最后。
2. 把 `tower` 的类型 `Tower` 定义成**零大小占位**（长度为 0 的数组）。
3. 分配内存时，**在结构体头部之后多分配** `height` 个指针，作为真正的塔。
4. 用裸指针算术去访问这片动态数组（见 4.4）。

#### 4.2.2 核心流程

一个 `Node` 在内存中的逻辑布局（以 `K = u64, V = u64`、64 位平台、`height = 3` 为例）：

```
偏移    字段                   大小       说明
 0      value: V               8 字节     值（放在最前）
 8      key: K                 8 字节     键
16      refs_and_height        8 字节     AtomicUsize（低5位=height-1，高位=ref_count）
        ── 以上是 Layout::new::<Node>() 的固定头部 ──
        ── Tower 是 ZST，占 0 字节，仅作占位 ──
24      tower[0] (level 0)     8 字节     Atomic<Node*>：第0层后继指针
32      tower[1] (level 1)     8 字节     Atomic<Node*>：第1层后继指针
40      tower[2] (level 2)     8 字节     Atomic<Node*>：第2层后继指针
48      对齐填充 (pad_to_align)
```

字段顺序的设计有两个考量：

- **硬约束**：变长塔必须在**最后**，否则后面字段的位置会随塔高漂移，无法用固定偏移访问。
- **缓存友好**：源码注释指出，把 `key`、`refs_and_height` 和 `tower` 放在一起（靠近末尾），是因为**遍历跳表时频繁访问的是「比较键 + 跟随指针」**，把它们聚在一起能让一次缓存行读取就拿到这些「热」数据；而 `value` 只在真正命中节点时才访问，属于相对「冷」的数据，因此放在最前。

#### 4.2.3 源码精读

`Node` 的定义与设计注释：

[base.rs:123-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L123-L145) — `#[repr(C)] struct Node`，字段顺序为 `value / key / refs_and_height / tower`，注释解释塔必须在末尾且字段聚簇是为了缓存局部性。

`refs_and_height` 字段的文档说清了引用计数到底在数什么：

[base.rs:137-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L137-L141) — 引用计数 = 指向该节点的 `Entry` 数量 + 该节点被安装到的层数。

`Tower` 是零大小占位（`[Atomic<Node>; 0]`）：

[base.rs:32-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L32-L39) — `#[repr(C)] struct Tower`，`pointers` 是长度为 0 的数组，实际大小随节点高度变化。

> 注意：`Node`、`Tower` 等结构体在源码中都是**私有**的（没有 `pub`），外部用户只能通过 `SkipMap` / `SkipSet` 间接使用。这是有意的封装——这些裸布局只能配合 `unsafe` 访问。

#### 4.2.4 代码实践

**实践目标**：把上面的抽象布局落到一张具体的图上。

**操作步骤**：

1. 假设 `K = u64`、`V = u64`、64 位平台、`height = 3`。
2. 仿照上面的偏移表，**亲手画一张内存布局示意图**，标出每个字段的偏移、大小，以及 3 个 `Atomic<Node*>` 塔指针（分别指向「第 0/1/2 层的后继节点」）。
3. 思考：如果把 `tower` 字段移到 `value` 和 `key` 之间，会发生什么问题？

**预期结果**：

- 固定头部 = `value(8) + key(8) + refs_and_height(8) = 24` 字节（`Tower` ZST 占 0 字节）。
- 动态塔 = `3 × 8 = 24` 字节。
- 若把塔放到中间，则 `key` 和 `refs_and_height` 的偏移会随塔高变化，无法用固定公式定位——这就是塔必须在最后的根本原因。

（如需在真实机器上核验指针大小，可写一个临时小程序打印 `core::mem::size_of::<Atomic<u64>>()` 与 `size_of::<AtomicUsize>()`，但 `Node` 本身是私有类型、无法直接度量，故布局核验以读源码 + 手算为准。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `value` 放在 `key` 前面，而不是按「直觉」的 `key / value` 顺序？

**答案**：这是缓存局部性的取舍。遍历时频繁比较的是 `key`、操作的是 `refs_and_height` 和 `tower` 指针，因此把它们三个聚在末尾；`value` 相对冷，放在最前面（离塔最远），避免在遍历比较时把不需要的 value 拉进缓存行。

**练习 2**：`Tower` 里的 `pointers: [Atomic<Node>; 0]` 是「长度为 0 的数组」，它真的占 0 字节吗？

**答案**：是的，`[T; 0]` 是零大小类型（ZST），`Tower` 因而也是 ZST，在 `Layout::new::<Node>()` 里不贡献任何字节。真正的塔空间是 `Node::alloc` 时额外分配的（见 4.3）。

---

### 4.3 Node 的分配、初始化与回收：alloc / get_layout / dealloc / finalize

#### 4.3.1 概念说明

因为每个节点的塔高不同，`Node` 不能用普通的 `Box::new` 分配。`base.rs` 提供了四个配套方法：

- `get_layout(height)`：根据高度算出**这块节点内存的 `Layout`**（大小 + 对齐）。
- `alloc(height, ref_count)`：按布局分配内存，**初始化 `refs_and_height` 和塔指针**，但**故意不初始化 `key` / `value`**（因此是 `unsafe`）。
- `dealloc(ptr)`：按节点自身记录的高度算出布局，归还内存（**不运行析构**）。
- `finalize(ptr)`：先 `drop` 掉 `key` 和 `value`，再 `dealloc`——这是「完整销毁」。

#### 4.3.2 核心流程

`get_layout` 用 `Layout::extend` 把「固定头部」和「动态塔数组」拼起来：

```
Layout::new::<Node>()                        # 固定头部（含 ZST tower，占 0 字节）
    .extend(Layout::array::<Atomic<Node>>(height))   # 追加 height 个指针
    .pad_to_align()                          # 对齐填充
```

`alloc` 的步骤：

1. 用 `get_layout(height)` 算出布局。
2. 调 `Global.allocate(layout)` 拿到一块裸内存。
3. 写入 `refs_and_height = (height-1) | (ref_count << HEIGHT_BITS)`。
4. 用 `write_bytes(0, height)` 把塔的 `height` 个指针**清零**（即初始化为空指针）。
5. **不写 `key` / `value`**——留给调用方（`insert`）去写。

为什么初始引用计数常常是 2？在 `insert` 里能看到原因：

> 引用计数初始为 2，分别对应：(1) 将要返回给调用者的那个 `Entry`；(2) 即将安装在塔第 0 层的那条链接。

#### 4.3.3 源码精读

`get_layout`：断言高度合法后，用 `extend` 拼接布局并 `pad_to_align`：

[base.rs:197-206](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L197-L206) — `assert!((1..=MAX_HEIGHT).contains(&height))`，然后 `Layout::new::<Self>().extend(Layout::array::<Atomic<Self>>(height))`。

`alloc`：分配、写 `refs_and_height`、清零塔指针，但不碰 `key`/`value`：

[base.rs:169-184](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L169-L184) — 调 `Global.allocate(layout)`，用 `addr_of_mut!` 写 `refs_and_height`，再用 `write_bytes(0, height)` 把塔指针清零。

`dealloc`：从节点自身读出高度，反推布局后归还内存：

[base.rs:189-195](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L189-L195) — 读 `(*ptr).height()`，重算 `get_layout`，再 `Global.deallocate`。

`finalize`：先 `drop_in_place` 析构 `key` 和 `value`，再 `dealloc`：

[base.rs:251-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L251-L262) — 标注 `#[cold]`（表示这是冷路径，引导编译器优化），依次 drop key、value，最后 dealloc。

`insert` 中的真实调用，展示了「先 alloc 再补写 key/value」的协议，以及初始 `ref_count = 2` 的由来：

[base.rs:1049-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1049-L1065) — `Node::<K, V>::alloc(height, 2)`，随后用 `addr_of_mut!((*n).key).write(key)` 与 `...value).write(value)` 补写键值。

底层的 `Global` 分配器（自实现，替代未稳定的 `alloc::alloc::Global`）：

[alloc_helper.rs:7-9](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L7-L9) — `pub(crate) struct Global;`，注释说明它基于不稳定的 `alloc::alloc::Global`。

[alloc_helper.rs:38-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L38-L40) — `allocate` 转发到 `alloc::alloc::alloc`。

#### 4.3.4 代码实践

**实践目标**：通过跟踪一次 `insert`，理解「alloc 只建壳、key/value 随后补写」的两步协议。

**操作步骤**（源码阅读型实践）：

1. 打开 [base.rs:1049-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1049-L1065)。
2. 注意第 1047 行注释：「先创建 value 再创建 node，这样即使 `value()` 闭包 panic 也不会产生一个未初始化的节点」。
3. 跟踪三步：`alloc(height, 2)` → `write(key)` → `write(value)`。
4. 回答：如果 `alloc` 里顺便把 `key`/`value` 也清零（而不是留给调用方），会有什么问题？

**预期结果**：

- `alloc` 之后、`write(key)` 之前，这块内存的 `key`/`value` 是**未初始化**的，绝不能被读取——这正是 `alloc` 标记为 `unsafe` 的原因。
- 若 `alloc` 提前清零 key/value，对带有「不安全位模式」的类型（例如某些枚举）会构成未定义行为；并且也无法填入真正的键值，徒劳无益。

**待本地验证**：可选地，用 `cargo +nightly miri test` 跑 `tests/base.rs` 的插入相关用例，Miri 会检查这类「未初始化内存读取」是否被违反。

#### 4.3.5 小练习与答案

**练习 1**：`dealloc` 里为什么要重新读一遍 `(*ptr).height()` 来算布局，而不是让调用方把 height 传进来？

**答案**：为了让 `dealloc` 的签名尽可能简单（只需一个指针），并保证「回收时用到的布局」与「分配时用到的布局」严格一致——二者都由同一个 `height` 经 `get_layout` 推导，避免调用方传错高度导致 `dealloc` 用错误的 layout 归还内存（这是未定义行为）。

**练习 2**：`finalize` 为什么标 `#[cold]`？

**答案**：`finalize` 只在节点引用计数归零、被延迟回收时才执行，是罕见路径。`#[cold]` 告诉编译器「这个函数很少调用」，从而把热点路径（如搜索、引用计数增减）的代码布局优化得更好，不把冷代码内联进热路径。

---

### 4.4 Tower / TowerRef / Head 与 NodeRef：动态塔的访问抽象

#### 4.4.1 概念说明

4.2 里我们看到塔是一个「藏在节点末尾的动态数组」。问题是：**怎么安全地访问它？** 不能用普通的 `&tower.pointers[i]`，因为 `Tower` 是 ZST，普通引用在 Rust 的别名模型（Stacked Borrows）下**只对那 0 个字节有访问权**，无权碰后面的动态数组。

作者的解法是定义四个「引用包装类型」：

- **`Tower<K,V>`**：ZST 占位，表示「这里有一摞指针」。
- **`TowerRef<'a, K, V>`**：一个**带生命周期的裸指针**（`NonNull<Tower>` + `PhantomData`），专门用来索引动态塔。
- **`Head<K,V>`**：跳表的「头」，内含一个**满高**（`MAX_HEIGHT` 个）的指针数组，存放在 `SkipList` 结构体内部。
- **`NodeRef<'a, K, V>`**：一个**带生命周期的节点裸指针**，既能访问节点的定长字段，也能经 `as_tower()` 拿到 `TowerRef` 去访问动态塔。

核心思想：**把 `Head` 和普通 `Node` 统一成同一种「塔视图」**，这样搜索/遍历代码可以无差别地从头节点或普通节点出发。

#### 4.4.2 核心流程

`TowerRef::get_level(index)` 用裸指针算术访问第 `index` 层指针：

```
&Tower  ──cast──>  *const Atomic<Node>  ──.add(index)──>  第 index 层指针
```

`NodeRef::as_tower()` 取节点末尾 `tower` 字段的地址，包装成 `TowerRef`；`NodeRef::get_level(index)` 就是 `self.as_tower().get_level(index)` 的简写。

`Head::as_tower()` 把 `&Head` 强转为 `TowerRef`，于是**头节点也能像普通节点一样被 `get_level` 索引**——这是统一抽象的关键。

`NodeRef::new` 是构造入口（`unsafe`），要求调用方保证：指针不仅指向一个合法 `Node`，而且对其末尾动态塔的实际大小也有访问权。

#### 4.4.3 源码精读

`TowerRef` 的定义与设计注释——直接点明「为何不能用普通引用」：

[base.rs:41-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L41-L50) — 注释解释：普通 `&Tower` 在 Stacked Borrows 下无权访问动态塔的字节，故用 `NonNull<Tower>` 保留 provenance（出处权）。

`TowerRef::get_level` 用 `.add(index)` 做指针算术：

[base.rs:80-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L80-L83) — `&*(self.ptr.as_ptr() as *const Atomic<Node<K,V>>).add(index)`。

`Head` 是满高数组（`MAX_HEIGHT` 个指针），并提供 `as_tower` 把自己伪装成 `TowerRef`：

[base.rs:86-110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L86-L110) — `Head` 含 `pointers: [Atomic<Node>; MAX_HEIGHT]`；`as_tower` 用 `NonNull::from(self).cast::<Tower>()` 把 `&Head` 当成 `Tower` 看。

`NodeRef` 的定义与注释——同样强调「保留对末尾动态塔的 provenance」：

[base.rs:147-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L147-L154) — `NodeRef` 是 `NonNull<Node>` + `PhantomData<&'a Node>`，注释提到「在少数地方还依赖它保留写权限」。

`NodeRef::new`（构造）、`as_tower`（取塔视图）、`as_ref`（取定长字段引用）、`get_level`（索引塔层）：

[base.rs:265-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L265-L278) — `NodeRef::new`，`unsafe`，要求指针对末尾动态塔同样有效。

[base.rs:363-372](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L363-L372) — `as_tower`，取 `addr_of!((*ptr).tower)` 包装成 `TowerRef`。

[base.rs:374-391](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L374-L391) — `as_ref`（拿普通 `&Node`，用于不需访问塔的场景）与 `get_level`（转发到 `as_tower().get_level`）。

#### 4.4.4 代码实践

**实践目标**：理解「统一塔视图」如何让头节点和普通节点共用同一套遍历代码。

**操作步骤**（源码阅读型实践）：

1. 对比 [base.rs:86-110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L86-L110)（`Head`）与 [base.rs:363-372](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L363-L372)（`NodeRef::as_tower`）：二者最终都产出 `TowerRef`。
2. 在 `base.rs` 中搜索 `.get_level(`，观察搜索函数是否**同时**对头节点和普通节点调用 `get_level`。
3. 回答：如果没有 `TowerRef` 这个中间抽象，搜索代码会变成什么样？

**预期结果**：

- 搜索算法只需面向 `TowerRef` 编程：从头节点的 `as_tower()` 出发，逐层 `get_level(level)` 取后继指针，无论是头还是普通节点，调用方式完全一致。
- 若没有 `TowerRef`，就必须为「头节点（定长满高数组）」和「普通节点（变长数组）」写两套索引逻辑，或在每个调用点区分类型，代码会冗余且易错。

**待本地验证**：可用 `grep -n "get_level" src/base.rs` 自行统计调用点，确认统一抽象的覆盖范围。

#### 4.4.5 小练习与答案

**练习 1**：`TowerRef` 和 `NodeRef` 都用了 `NonNull` + `PhantomData<&'a T>`，为什么不直接用 `&'a T`？

**答案**：第一，`T`（`Tower`）是 ZST，`&Tower` 在 Stacked Borrows 下只对 0 字节有访问权，不能用来索引后面的动态数组；用 `NonNull`（裸指针）能保留对整块分配（含动态尾部）的 provenance。第二，`NonNull` 还允许在内部做更灵活的指针运算与别名操作（`NodeRef` 注释提到「少数情况依赖它保留写权限」），这是普通共享引用 `&T` 不允许的。`PhantomData<&'a T>` 仅用来把生命周期 `'a` 绑定上去，由借用检查器约束使用范围。

**练习 2**：`Head` 的指针数组是定长 `[Atomic<Node>; MAX_HEIGHT]`，而普通节点的塔是变长的。为什么头节点要「满高」？

**答案**：跳表查找总是从**最高层**开始向右、向下走。当前实际最高层（`HotData.max_height`）只增不减，但理论上限是 `MAX_HEIGHT`。头节点作为查找起点，必须能在任意一层提供出发指针，因此预分配满高数组。而普通节点的高度由随机数决定（通常很低），用变长分配节省内存。

---

## 5. 综合实践

把本讲的知识串起来，做一个**可手算验证**的生命周期推演。

**场景**：一个 `height = 3`、初始 `ref_count = 2` 的节点刚被 `insert` 创建。

**任务**：

1. **写出初始 `refs_and_height` 的数值**（提示：用 4.1.4 的公式）。
2. **画出该节点的内存布局示意图**（含 `value / key / refs_and_height` 与 3 个塔指针，标注偏移与大小）。
3. **模拟两次引用计数递减**（对应 `NodeRef::decrement`：每次 `fetch_sub(1 << 5, Release)`，然后 `>> 5` 判断是否等于 1）：
   - 第 1 次：drop 掉返回的那个 `Entry`。
   - 第 2 次：把节点从第 0 层链接中摘除。
   - 在每一步写出 `refs_and_height` 的旧值、新值，以及是否触发 `finalize`。

**参考解答**：

1. 初始值：\((3-1)\;|\;(2\ll 5) = 2\;|\;64 = 66\)。
2. 布局图见 4.2.2（固定头部 24 字节 + 3 个指针 24 字节）。
3. 递减过程（每次减 `1 << 5 = 32`）：

| 步骤 | 旧 `refs_and_height` | 新 `refs_and_height` | 旧值 `>> 5` | 是否触发 `finalize` |
|------|----------------------|----------------------|-------------|--------------------|
| 第 1 次 decrement | 66 | 34 | 66 >> 5 = **2** | 否（≠ 1） |
| 第 2 次 decrement | 34 | 2  | 34 >> 5 = **1** | **是**（== 1） |

   即：引用计数从 2 → 1 → 0。只有当**旧值右移后等于 1**（意味着这次递减让它从 1 降到 0）时，才经 `Acquire` 栅栏后用 `guard.defer_unchecked(Node::finalize)` 安排回收。这正对应 [base.rs:292-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L292-L304) 的逻辑。

4. **反思**：为什么用「旧值 `>> 5 == 1`」而不是「新值 `>> 5 == 0`」来判断？因为多个线程可能并发递减，用「我这次递减恰好把它从 1 降到 0」来判定，能保证**恰好一个线程**赢得回收权，避免重复 `finalize`（double-free）。

> 这个递减与延迟回收的完整协议（包括 `Release`/`Acquire` 栅栏、epoch `Guard` 的作用）会在 **u2-l6（epoch 内存回收与引用计数）** 中展开。本讲只需记住：`refs_and_height` 的高位归零，是触发节点销毁的信号。

## 6. 本讲小结

- `refs_and_height` 把**高度**（低 5 位，存 `height-1`）和**引用计数**（高位）压进一个 `AtomicUsize`，保证二者被原子地一致读取；`HEIGHT_BITS=5`、`MAX_HEIGHT=32`、`HEIGHT_MASK=31`。
- `Node` 用 `#[repr(C)]` 锁定字段顺序 `value / key / refs_and_height / tower`；塔必须在末尾（变长约束），前三者聚簇是为了遍历时的缓存局部性。
- 变长塔通过「ZST 占位 + 分配时多申请 `height` 个指针」实现，`get_layout` 用 `Layout::extend` 拼接，`alloc` 初始化引用计数与塔指针但**故意不初始化 key/value**，`finalize` 完整析构后再 `dealloc`。
- `Tower`（ZST 占位）、`TowerRef`、`Head`（满高头）、`NodeRef` 共同构成「动态塔访问抽象」；`TowerRef`/`NodeRef` 用 `NonNull` + `PhantomData` 保留对动态尾部的 provenance，使头节点和普通节点都能用 `get_level` 统一索引。
- `Head` 预分配满高数组作为查找起点，普通节点按随机高度变长分配以节省内存——这是「统一塔视图」下的两种具体实现。

## 7. 下一步学习建议

本讲只看了节点的**静态布局**，还没有涉及并发。建议接下来：

1. **u2-l6（epoch 内存回收与引用计数）**：精读 `NodeRef::decrement` / `decrement_with_pin` / `try_increment` / `finalize` 与 crossbeam-epoch 的 `Guard`、`defer_unchecked` 如何协作，理解「引用计数 + epoch」如何避免 use-after-free，以及为何需要 `K: 'static + V: 'static`。
2. **u3-l8（搜索算法）**：本讲的 `TowerRef::get_level` / `Position` 是搜索的基础——届时你会看到 `Position` 里 `left: [TowerRef; MAX_HEIGHT]` 和 `right: [Shared<Node>; MAX_HEIGHT]` 如何记录每层的邻接节点。
3. 课外延伸：阅读 [crossbeam-epoch](https://github.com/crossbeam-rs/crossbeam/tree/master/crossbeam-epoch) 的 `Atomic` / `Shared` / `Guard` 文档，理解本讲反复出现的 `epoch::unprotected()` 与指针 tag（`fetch_or(1)`）的底层含义（tag 与删除标记将在 u3-l9 详述）。
