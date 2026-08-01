# Span 紧凑编码

## 1. 本讲目标

本讲精读 `src/span.rs` 的前半部分，集中讲清楚一件事：**Typst 如何用区区 8 个字节，给语法树里每一个节点一个稳定、可比较、可还原成字节范围的「身份证号」。**

学完本讲你应当能够：

- 解释 `Span` 为什么用「编号」而不是「字节范围」来定位源码，以及这种设计在增量编辑下的好处。
- 画出 `Span` 的 64 位内部布局：高 16 位 `FileId`、低 48 位 `number`，以及 `number` 字段内部四类取值的分区。
- 区分 `SpanKind` 的三个变体 `Detached` / `Number` / `Range`，并说明外部文件范围（external）为什么不出现在 `SpanKind` 里。
- 读懂 `detached()` / `from_number()` / `from_range()` / `pack()` / `get()` / `id()` / `number()` 这一整套「打包—还原」API，并理解其背后的位运算。
- 理解 `NonZeroU64` 带来的 null 优化：为什么 `Option<Span>` 也只要 8 字节。

## 2. 前置知识

本讲依赖你已经建立的几个认知（来自前序讲义）：

- **CST 与 `SyntaxNode`**（u5-l1、u5-l2）：Typst 的具体语法树是无损的，每个节点都需要一个身份标识，供下游（求值、诊断、IDE 跳转）引用。
- **`numberize` 的两条不变量**（u1-l4 已经预告，u6-l2 会详讲）：父节点编号小于任意子节点，兄弟节点编号从左到右递增。本讲只关心「编号本身怎么编码进 8 字节」，编号怎么分配留到 u6-l2。
- **`FileId`**：来自 `src/path.rs`，是一个包装了 `NonZeroU16` 的 16 位文件身份号（[src/path.rs:98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L98)）。一个 `Span` 必须能说出自己属于哪个文件，所以 `FileId` 是 `Span` 的一部分。

如果你还不熟悉位运算（移位 `<<`、按位或 `|`、按位与 `&`、掩码 mask），建议先快速复习：本讲的位布局就是用这几样基本操作拼出来的。

## 3. 本讲源码地图

本讲几乎只读一个文件：

| 文件 | 本讲关注的内容 |
| --- | --- |
| [src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs) | `Span` 结构体、`SpanKind` 枚举、`SpanNumber`、`detached`/`from_number`/`from_range`/`pack`/`get`/`id`/`number`、以及 `FULL` / `FILE_ID_SHIFT` 等编码常量 |

辅助参考：

| 文件 | 关注点 |
| --- | --- |
| [src/path.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs) | `FileId(NonZeroU16)` 的定义、`into_raw` / `from_raw` |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs) | 第 34–36 行把 `Span`、`SpanKind`、`SpanNumber` 等通过 `pub use` 挂牌到 crate 根 |

> 说明：`DiagSpan` / `DiagSpanKind` / `SubRange` / `RangeMapper` 也在 `span.rs` 里，但它们涉及「诊断范围」「外部文件」「子区间映射」，属于 u6-l3 的主题，本讲只在必要时点到，不展开。

## 4. 核心概念与源码讲解

### 4.1 Span：用「编号」而非「字节范围」定位源码

#### 4.1.1 概念说明

「Span」直译是「跨度」，在很多编译器里它就是一段字节范围，比如「第 12 字节到第 18 字节」。但 Typst 的 `Span` 不是这样——它给 CST 里的**每个节点一个独一无二的整数编号**，这个编号就是节点的身份。

为什么不用字节范围？`span.rs` 顶部的文档注释把动机说得很直白：字节范围在你打字时会**频繁漂移**。想象你在文档开头插了一个字符，那么后面所有节点的字节起点都要 +1，缓存立刻大面积失效。而编号不会：编号是在 `numberize` 时按树结构分配的，只要编辑是局部的，多数节点的编号都能保持不变，连插入点之后的节点也可能不动。

[src/span.rs:42-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L42-L50)：这段注释说明了「编号稳定」对带记忆化（memoization）的增量编译的意义——编号稳定，缓存命中率才高。

#### 4.1.2 核心流程

一个 `Span` 的生命周期是「打包 → 携带 → 还原」三步：

1. **打包（pack）**：`numberize` 给某节点分配好编号 `num` 后，调用 `Span::from_number(id, num)`，把「文件号 + 编号」压缩进一个 64 位整数。
2. **携带**：这个 `Span` 被 `SyntaxNode` 顶层字段携带（u5-l1 讲过 `SyntaxNode` 有一个 `span: Span` 字段），随节点一起被复制、哈希、比较——因为 `Span` 是 `Copy + Eq + Hash`。
3. **还原（get / id / number）**：下游需要时，用 `span.id()` 取出文件、用 `span.get()` 还原成可读的 `SpanKind`、或交给 `Source::range` 反查字节范围。

关键直觉：**Span 本身只存「谁（文件）+ 第几号（编号）」，不存字节位置。** 字节位置是临时算出来的（由 `Source` 结合 `Lines` 和编号单调性反查），这样才能在编辑后保持稳定。

为了让「按编号快速找节点」成为可能，编号必须满足两条单调不变量（[src/span.rs:52-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L52-L61)）：

- 父节点编号 < 任意子节点编号；
- 兄弟节点编号从左到右递增。

合起来就是：对兄弟序列 `[A, B, C]`，`A` 及其全部后代的编号都小于 `B`，`B` 及其全部后代的编号都小于 `C`。这让 `find_number` 可以用「下一个兄弟当上界」做二分式剪枝（u5-l3 已用，u6-l2 详讲分配）。

#### 4.1.3 源码精读

`Span` 的本体极其朴素——一个字段，包了 `NonZeroU64`：

[src/span.rs:62-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L62-L63)：定义 `Span(NonZeroU64)`，并派生 `Debug, Copy, Clone, Eq, PartialEq, Hash`。整个 `Span` 就是一个 64 位非零整数，所有魔法都在「这 64 位怎么解释」。

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
pub struct Span(NonZeroU64);
```

与之配套的 `SpanNumber` 是编号的类型别名（[src/span.rs:65-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L65-L72)），它是个 `pub(crate)` 的元组结构体 `SpanNumber(pub(crate) u64)`，外部主要拿它当 `Source::range` 的入参用。注意它的字段是 `pub(crate)`，crate 外无法直接构造，只能从 `Span::get()` 里拿到。

`SpanKind` 则是 `Span` 的「展开视图」，把那个 64 位整数翻译成人能读的三种情况：

[src/span.rs:74-83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L74-L83)：`SpanKind` 枚举有 `Detached`（不指向任何文件）、`Number { id, num }`（编号 span，最常见）、`Range { id, range }`（原始字节范围 span）。

```rust
pub enum SpanKind {
    Detached,
    Number { id: FileId, num: SpanNumber },
    Range { id: FileId, range: Range<usize> },
}
```

> 注意只有三个变体。第四类「外部文件范围（external）」是 `DiagSpan` 的事，不出现在 `SpanKind`——这是 4.3 节要重点澄清的「四类取值 vs 三个变体」之分。

#### 4.1.4 代码实践

**实践目标**：理解「编号稳定」这件事不是抽象口号，而是有测试在守护。

**操作步骤**：

1. 打开 `src/span.rs` 末尾的 `#[cfg(test)] mod tests`（[src/span.rs:553](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L553)）。
2. 阅读 `test_span_number_encoding`（[src/span.rs:564-570](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L564-L570)），它造一个 `id=5, num=10` 的 span，断言 `id()` 与 `number()` 能原样取回。

**需要观察的现象**：构造与还原是**无损往返（roundtrip）**——`from_number(id, num)` 进去，`id()` / `number()` 出来，值完全一致。这说明编号本身被无损地「装进」又「取出」了 8 字节。

**预期结果**：测试通过；`span.id() == Some(id)` 且 `span.number() == 10`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Span` 改成存 `Range<usize>`（字节范围），在「文档开头插入一个字符」后，整个 CST 里大概有多少节点的 span 会失效？这会带来什么后果？

> **答案**：插入点之后的**所有**节点 span 都会失效（起点、终点都要 +1）。后果是：以 span 为键的记忆化缓存几乎全部 miss，增量编译退化成接近全量重算。这正是 Typst 选择「编号」而非「字节范围」的核心动机。

**练习 2**：`Span` 派生了 `Hash`。为什么对编译器来说「Span 可哈希」很重要？

> **答案**：下游的 memoization 缓存（如求值结果）常以 `Span` 或包含 `Span` 的结构作为哈希键。编号稳定 + 可哈希，意味着「同一节点」在编辑前后落到同一个桶里，缓存才能复用。

---

### 4.2 8 字节位布局与 null 优化

#### 4.2.1 概念说明

`Span` 要同时承载「属于哪个文件」和「第几号」两份信息，却只能用 8 字节。Typst 的做法是把这 64 位**切成两段**：高 16 位放文件号 `FileId`，低 48 位放编号 `number`。文档注释把布局写成了一行：

[src/span.rs:92-94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L92-L94)：

```
| 16 bits file id | 48 bits number |
```

「null 优化」则是 Rust 的一个语言特性：因为 `Span` 内部是 `NonZeroU64`（永不为 0），编译器知道 `0` 这个值是「空闲」的，于是可以用它来表示 `Option<Span>::None`。结果是 `Option<Span>` 也只占 8 字节，而不是 16 字节。文档注释明确提到了这一点：

[src/span.rs:21-23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L21-L23)：`Span` 紧凑存于 8 字节、可拷贝、null 优化（`Option<Span>` 同样 8 字节）。

#### 4.2.2 核心流程

整个 64 位布局如下（bit 63 在左，bit 0 在右）：

```
 bit:  63              48 47                                       0
      +------------------+-------------------------------------------+
      |   file id (16)   |              number (48)                  |
      +------------------+-------------------------------------------+
                            ↑
                  FILE_ID_SHIFT = 48（id 左移 48 位后 OR 进来）
```

编码（`pack`）与解码（`id` / `number`）是互逆的位运算：

- **编码**：`bits = (id << 48) | number`。因为 `id` 是 `NonZeroU16`，高 16 位必非零，整体也必非零，所以 `NonZeroU64::new(bits).unwrap()` 永远成功。
- **解码 `id`**：`bits >> 48` 取高 16 位；若结果为 0（即 detached 的情况），返回 `None`。
- **解码 `number`**：`bits & ((1<<48)-1)` 取低 48 位（用掩码 `NUMBER_MASK` 抹掉文件号）。

用数学语言写，设 `Span` 内部 64 位整数为 \( b \)，文件号为 \( f \)，编号为 \( n \)，则：

\[
b = (f \ll 48) \;\vee\; n,\qquad f = b \gg 48,\qquad n = b \;\&\; (2^{48}-1)
\]

其中 \( \vee \) 为按位或、\( \& \) 为按位与、\( \ll/\gg \) 为左右移位。

#### 4.2.3 源码精读

编码常量集中定义在 `impl Span` 顶部（[src/span.rs:102-110](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L102-L110)），关键是这几个：

```rust
const NUMBER_BITS: usize = 48;
const FILE_ID_SHIFT: usize = Self::NUMBER_BITS;        // = 48
const NUMBER_MASK: u64 = (1 << Self::NUMBER_BITS) - 1; // 低 48 位全 1
```

打包函数 `pack`（[src/span.rs:145-150](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L145-L150)）正是上面公式的直接翻译：

```rust
const fn pack(id: FileId, low: u64) -> Self {
    let bits = ((id.into_raw().get() as u64) << Self::FILE_ID_SHIFT) | low;
    // The file ID is non-zero.
    Self(NonZeroU64::new(bits).unwrap())
}
```

`id.into_raw().get()` 把 `NonZeroU16` 取成 `u16` 再提升为 `u64`，左移 48 位后与低 48 位的 `low`（即 number）按位或。注释「The file ID is non-zero」解释了为什么 `unwrap` 不会 panic：合法 `Span` 的文件号非零 → 高位非零 → 整体非零。

解码 `id`（[src/span.rs:160-167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L160-L167)）：

```rust
pub const fn id(self) -> Option<FileId> {
    // Detached span has only zero high bits, so it will trigger the `None` case.
    match NonZeroU16::new((self.0.get() >> Self::FILE_ID_SHIFT) as u16) {
        Some(v) => Some(FileId::from_raw(v)),
        None => None,
    }
}
```

注意它的精妙之处：detached span 的高 16 位是 0（文件号为 0），所以 `NonZeroU16::new(0)` 返回 `None`，`id()` 自然返回 `None`。**「是不是 detached」和「文件号是不是 0」被合二为一**，不用单独留一个标志位。

解码 `number`（[src/span.rs:170-172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L170-L172)）用掩码取低 48 位：

```rust
pub(crate) const fn number(self) -> u64 {
    self.0.get() & Self::NUMBER_MASK
}
```

#### 4.2.4 代码实践

**实践目标**：亲手画出 64 位布局，把抽象的「16 + 48」落到比特位上。

**操作步骤**：

1. 阅读顶部文档注释第 92–110 行（[src/span.rs:92-110](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L92-L110)）。
2. 在纸上（或注释里）画一根 64 格的横线，左 16 格标注「file id (bits 48..63)」，右 48 格标注「number (bits 0..47)」。
3. 标出 `FILE_ID_SHIFT = 48`、`NUMBER_MASK = 2^48 - 1` 在图上对应的位置。

**需要观察的现象**：file id 占据**最高** 16 位，number 占据**最低** 48 位；两者不重叠，所以 `pack` 用按位或、`id`/`number` 用移位与掩码即可无干扰地拆装。

**预期结果**：一张清晰的「高位文件号、低位编号」分区图，且能指出 `id()` 利用「文件号为 0 ⇒ detached」来省掉独立标志位。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Option<Span>` 和 `Span` 一样大（都是 8 字节）？如果 `Span` 内部是普通 `u64` 而不是 `NonZeroU64`，`Option<Span>` 会是多大？

> **答案**：`NonZeroU64` 向编译器保证「内部值永不为 0」，于是 `0` 这个位模式可被用来编码 `None`（Rust 的 niche optimization）。若改用普通 `u64`，编译器无法排除 0 是合法值的可能，必须额外加一个判别字节，`Option<Span>` 会变大（通常 16 字节，含对齐填充）。在动辄存放海量 span 的编译器里，这个差别很可观。

**练习 2**：`pack` 里的 `NonZeroU64::new(bits).unwrap()` 会不会 panic？为什么？

> **答案**：不会。合法 `Span` 的文件号是 `NonZeroU16`（非零），左移到高 16 位后整体必非零，所以 `NonZeroU64::new` 一定返回 `Some`。注释「The file ID is non-zero」正是这个保证。唯一高 16 位为 0 的 detached 是用 `Self::DETACHED = NonZeroU64::new(1)` 直接构造的，不走 `pack`。

---

### 4.3 四类编号值与构造/还原 API

#### 4.3.1 概念说明

上一节把低 48 位笼统称作「number」。但实际上这 48 位被进一步**划分成四个区段**，分别编码四种不同的值（[src/span.rs:25-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L25-L41)）：

1. **Detached**：不指向任何文件（编译器自己产生的、无处安放的 span）。
2. **Numbered**：Typst 源文件 CST 节点的编号，最常见。
3. **Range**：原始字节范围（两个 23 位整数），用于「把一段文本当 Typst 语法解析」的场景。
4. **External**：外部文件（如 JSON）里的字节起点，仅供 `DiagSpan` 使用。

这里有个容易踩的认知陷阱：文档列了**四类取值**，但 `SpanKind` 枚举只有**三个变体**（`Detached` / `Number` / `Range`）。原因正是第 4 类「external」只能存在于 16 字节的 `DiagSpan` 里（它需要额外的 `extra` 字段存结束位置），一个普通 8 字节 `Span` 装不下。所以：

- `Span::get()` 只可能返回 `Detached` / `Number` / `Range` 三者之一；
- external 区段是 `DiagSpan::get()` 的专利（u6-l3 详讲）。

#### 4.3.2 核心流程

低 48 位 `number` 字段的四段分区如下（按取值从小到大，正好把 \( [1, 2^{48}) \) 切成不重叠的四块）：

```
number 取值区间                    含义                        字段位宽
─────────────────────────────────────────────────────────────────────
 1                                detached 哨兵（且 file id=0）    —
 [2, 2^47)                        numbered（CST 节点编号）        一个 47 位整数
 [2^47, 2^47 + 2^46)              external 文件起点（仅 DiagSpan） 一个 46 位整数
 [2^47 + 2^46, 2^48)              range（两个字节下标）           两个 23 位整数
```

对应的几个边界常量（[src/span.rs:86-110](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L86-L110)）：

- `FULL = 2 .. (1 << 47)`：numbered span 的合法编号范围（[src/span.rs:87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L87)）。注意起点是 2，因为 1 留给了 detached。
- `EXTERNAL_BASE = FULL.end = 2^47`：external 区段起点。
- `EXTERNAL_VALUE_MAX = 2^46 - 1`：external 单值的最大值。
- `RANGE_BASE = EXTERNAL_BASE + 2^RANGE_BITS = 2^47 + 2^46`：range 区段起点。
- `RANGE_VALUE_BITS = 23`、`RANGE_VALUE_MAX = 2^23 - 1`：range 两个半各自 23 位。

range 的打包特别巧妙：把起点和终点**两个 23 位整数**拼进低 46 位——`number = RANGE_BASE + (start << 23) | end`，其中 start 占 bit 23..46、end 占 bit 0..23（[src/span.rs:128-133](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L128-L133)）。于是 range 的起点/终点各自最大 \( 2^{23}-1 = 8388607 \)，超出会被饱和（`saturate`）截断。

> 设计取舍：Typst 把「最常见、需要长期稳定」的 numbered span 分配了最大的 \( 2^{47}-2 \) 个编号槽位（够 CST 节点用了）；把较罕见的 range 塞进最高四分之一区间，每个 range 只用 46 位就能装下两个下标。

#### 4.3.3 源码精读

**detached**（[src/span.rs:89-90](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L89-L90)、[src/span.rs:113-115](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L113-L115)）：detached 就是整数 1，文件号（高 16 位）为 0。

```rust
const DETACHED: Self = Self(NonZeroU64::new(1).unwrap());
pub const fn detached() -> Self { Self::DETACHED }
```

**from_number**（[src/span.rs:118-122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L118-L122)）：先 `debug_assert` 编号落在 `FULL` 内，再 `pack`。它是 `pub(crate)`——只有 `numberize` 会调用，外部无法直接造编号 span。

```rust
pub(crate) const fn from_number(id: FileId, SpanNumber(number): SpanNumber) -> Self {
    debug_assert!(Self::FULL.start <= number);
    debug_assert!(number < Self::FULL.end);
    Self::pack(id, number)
}
```

**from_range**（[src/span.rs:128-133](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L128-L133)）：把两个字节下标饱和到 23 位、拼成 `number`、加上 `RANGE_BASE`、再 `pack`。

```rust
pub(crate) const fn from_range(id: FileId, range: Range<usize>) -> Self {
    let start = saturate(range.start, Self::RANGE_VALUE_MAX);
    let end = saturate(range.end, Self::RANGE_VALUE_MAX);
    let number = (start << Self::RANGE_VALUE_BITS) | end;
    Self::pack(id, Self::RANGE_BASE + number)
}
```

> `saturate`（[src/span.rs:329-331](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L329-L331)）是手写的 `min`，注释说因为 `usize::min` 在 `const fn` 里还不稳定，所以自己写。

**get**（[src/span.rs:177-187](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L177-L187)）：还原的总入口，三步判断：

```rust
pub const fn get(self) -> SpanKind {
    let Some(id) = self.id() else { return SpanKind::Detached };
    let num = self.number();
    if let Some(packed_range) = num.checked_sub(Self::RANGE_BASE) {
        let start = (packed_range >> Self::RANGE_VALUE_BITS) as usize;
        let end = (packed_range & Self::RANGE_VALUE_MAX) as usize;
        SpanKind::Range { id, range: start..end }
    } else {
        SpanKind::Number { id, num: SpanNumber(num) }
    }
}
```

判别逻辑：先看 `id()` 是否为 `None`（⇒ Detached）；再看 `number` 减 `RANGE_BASE` 是否「够减」（`checked_sub` 返回 `Some` ⇒ 处于 range 区段 ⇒ Range）；否则就是 Number。

注意一个细节：对普通 `Span`，`from_number` 保证编号 \(< 2^{47} < \) `EXTERNAL_BASE`，所以 `get()` 永远不会把一个普通 span 误判进 external 区段——external 是 `DiagSpan::get()` 单独处理的（它对 `Number` 分支再做一次 `checked_sub(EXTERNAL_BASE)`，见 [src/span.rs:287-318](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L287-L318)，u6-l3 详讲）。

#### 4.3.4 代码实践

**实践目标**：通过阅读 range 的往返测试，验证「两个字节下标」被无损地塞进又取出 8 字节。

**操作步骤**：

1. 阅读 `test_span_range_encoding`（[src/span.rs:572-588](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L572-L588)）。它用 `Span::from_range(file_id, range)` 造 span，再用 `span.get()` 还原，断言 `Range { id, range }` 里的 `range` 与输入一致。
2. 关注几个用例：`0..0`、`177..233`、`0..0x7F_FFFF`（即 \( 0..2^{23}-1 \)，正好是 23 位上限）、`0x7F_FFFE..0x7F_FFFF`（接近上限的边界）。
3. 运行本 crate 的测试，观察结果（**待本地验证**）：

   ```bash
   cargo test -p typst-syntax test_span_range_encoding
   ```

**需要观察的现象**：`from_range` 造的 span，经 `get()` 还原后 `range` 字段与原始输入**逐字节相等**，包括边界值 \( 2^{23}-1 \)。这证明「两个 23 位整数拼进 46 位」是无损的。

**预期结果**：测试通过；最大可表达的 range 端点是 \( 2^{23}-1 = 8388607 \)，超过会被 `saturate` 截到这个值（测试里没有直接验「截断」，但注释 [src/span.rs:126-127](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L126-L127) 说明了饱和行为）。

> 提示：`from_number` / `from_range` 都是 `pub(crate)`，crate 外无法直接调用。本实践定位为「阅读测试理解行为」：你通过测试用例和断言来确认编码的无损性，而不是自己手写调用。

#### 4.3.5 小练习与答案

**练习 1**：文档说有「四类取值」，但 `SpanKind` 只有三个变体。多出来的那一类是什么？为什么它不在 `SpanKind` 里？

> **答案**：多出来的是「external 文件起点」（用于在 JSON 等非 Typst 文件里定位诊断）。它需要同时存「起点」和「终点」两个最多 46 位的数，8 字节的 `Span` 装不下（只能装起点，终点要另存），所以它只出现在 16 字节的 `DiagSpan`（带一个 `extra: u64` 字段）里，由 `DiagSpanKind` 表达。

**练习 2**：`from_number` 里两条 `debug_assert` 检查的是哪两条性质？如果编号等于 1 会怎样？

> **答案**：检查编号落在 `FULL = 2 .. 2^47` 内，即「≥ 2 且 < \( 2^{47} \)」。编号等于 1 会触发 `debug_assert!(Self::FULL.start <= number)` 失败（因为 `FULL.start == 2`），在 debug 构建里 panic——1 是留给 detached 的哨兵，不能当普通编号用。

**练习 3**：range 的两个字节下标各自最多能表示到多大？为什么 Typst 觉得这个上限够用？

> **答案**：各自最多 \( 2^{23}-1 \approx 8.39 \times 10^6 \)。range span 用于「把一段文本当 Typst 解析」这类相对局部的场景，单段文本很少超过 800 万字节；真超了也只是被饱和截断（诊断范围略不准），不会崩溃。把更大的编号空间（\( 2^{47} \)）留给更常见、更需要稳定的 numbered span，是合理的取舍。

---

## 5. 综合实践

**任务**：把本讲的三块知识——「编号而非范围」「位布局」「四类取值」——串成一张完整的 64 位「Span 身份证」说明书。

请完成以下 deliverable：

1. **画一张完整的 64 位布局图**，要求标注：
   - 高 16 位：`file id`（并注明 detached 时为 0）。
   - 低 48 位：`number`，并在这 48 位内部再细分出四个区段（detached 哨兵、numbered、external、range），写出每个区段的取值范围（用 \( 2^k \) 表达）与位宽。
2. **写一份「打包—还原」对照表**：左列是操作（`pack` / `id` / `number` / `get`），右列是对应的位运算公式（移位、掩码、`checked_sub`）。
3. **回答一个综合问题**：给定一个 `Span`，仅凭它的内部 64 位整数，你能否判断它属于 `Detached` / `Number` / `Range` 中的哪一类？请用 `id()` 和 `number()` 与各 `BASE` 常量之间的关系说明判断流程（这正是 `get()` 在做的事）。

**参考答案要点**：

- 布局图：高 16 位 file id；低 48 位中，`1` = detached（高 16 位为 0），`[2, 2^47)` = numbered（47 位），`[2^47, 2^47+2^46)` = external（46 位，仅 DiagSpan），`[2^47+2^46, 2^48)` = range（两个 23 位）。
- 判断流程：`id() == None` ⇒ Detached；否则取 `num = number()`，若 `num.checked_sub(RANGE_BASE).is_some()` ⇒ Range（再拆两个 23 位）；否则 ⇒ Number。普通 `Span` 的 `num` 永远 `< 2^47`，故不会落进 external 区段。

> 如果你想跑代码验证，可以在本仓库执行 `cargo test -p typst-syntax -- span`，把 `test_span_detached` / `test_span_number_encoding` / `test_span_range_encoding` 三个测试的通过情况作为你的理解的「执行证据」（**待本地验证**）。

## 6. 本讲小结

- `Span` 是 Typst 给 CST 节点的「身份证号」，**用整数编号而非字节范围**定位源码，目的是让 span 在增量编辑下保持稳定，从而保住 memoization 缓存命中率。
- `Span` 本体是 `NonZeroU64`，**8 字节**；高 16 位是 `FileId`，低 48 位是 `number`，由 `pack`（左移+或）、`id`（右移）、`number`（掩码与）这一组互逆位运算拆装。
- 因为内部是 `NonZeroU64`，`Option<Span>` 也只要 8 字节（null 优化）；detached 还顺便利用「文件号为 0」复用为标志位，省掉独立字段。
- 低 48 位 `number` 内部再分四段：detached 哨兵（1）、numbered（`[2, 2^47)`，最常见）、external（`[2^47, 2^47+2^46)`，仅 `DiagSpan`）、range（`[2^47+2^46, 2^48)`，两个 23 位下标）。
- 「四类取值」≠「三个变体」：普通 `Span::get()` 只返回 `Detached`/`Number`/`Range`；external 是 16 字节 `DiagSpan` 的专利，留到 u6-l3。
- 关键 API：`detached()`（取哨兵）、`from_number`/`from_range`（`pub(crate)` 打包）、`get()`（按 `id` ⇒ `checked_sub(RANGE_BASE)` 的顺序还原）、`id()`/`number()`（字段解码）。

## 7. 下一步学习建议

本讲只讲了「编号怎么编码进 8 字节」，还没讲「编号怎么分配」。建议按顺序继续：

- **u6-l2 编号 Span 与 numberize**：精读 `src/node.rs` 的 `numberize`，看它如何用中序遍历、取中点、挤左半区留余量的策略给每个节点分配满足「父<子、兄弟递增」两条不变量的编号，并理解 `Span::FULL` 区间与 `Unnumberable` 错误的由来。
- **u6-l3 DiagSpan、SubRange 与外部范围**：把本讲刻意留白的 `DiagSpan` / `DiagSpanKind` / `SubRange` / `RangeMapper` 补齐，看 external 文件范围如何在 16 字节里编码、`SubRange` 如何在节点内指向子区间、`Source::range` 如何把编号反查成字节范围。
- 想立刻看到 span 的实际使用，可以跳到 **u5-l3** 的 `find_number`（依赖本讲的两条编号不变量）或 **u8-l1** 的 `Source::find`（把 span 映射回 `LinkedNode`）。
