# 文本坐标系统：Point、OffsetUtf16、PointUtf16 与 Unclipped

## 1. 本讲目标

学完本讲，你应该能够：

- 区分 rope 中四类坐标表达：字节偏移（`usize`）、`Point`、`OffsetUtf16`、`PointUtf16`，说清它们各自的计量单位和典型来源。
- 熟练推导 `Point` / `PointUtf16` 的加法与减法结果，理解「位置 + 位移」这种非对称语义，以及它为什么恰好是文本拼接所需要的。
- 理解 `Ord` 实现中把 `(row, column)` 打包成一个整数比较的技巧。
- 说清 `Unclipped<T>` 这个零开销包装器存在的原因：用类型系统区分「已经过验证的坐标」和「来自外部、可能越界的坐标」。

本讲是单元二的第一讲。后续的 `TextSummary`（u2-l2）与 SumTree 集成（u2-l3）都建立在本讲的坐标类型之上。

## 2. 前置知识

### 2.1 回顾：字节偏移是 rope 的基准坐标

u1-l3 已经确立：`Rope` 的所有区间操作以 UTF-8 **字节偏移**（`usize`）为基准，端点必须落在字符边界。本讲要回答的问题是：字节偏移之外，为什么 rope 还需要另外三种坐标类型？

### 2.2 UTF-8 与 UTF-16：两种计量文本的方式

- **UTF-8**：Rust `String` 的内部编码。一个字符占 1～4 字节：`'a'` 占 1 字节，`'中'` 占 3 字节，emoji `'🧘'` 占 4 字节。
- **UTF-16**：用 16 位代码单元（code unit）计量字符。绝大多数常用字符（包括全部 CJK）占 1 个代码单元；增补平面字符（大部分 emoji）占 2 个代码单元，即所谓「代理对」（surrogate pair）。

同一个字符在两种编码下的「宽度」不同：

| 字符 | UTF-8 字节 | UTF-16 代码单元 |
|------|-----------|----------------|
| `'a'` | 1 | 1 |
| `'中'` | 3 | 1 |
| `'🧘'` | 4 | 2 |

**为什么 rope 要关心 UTF-16？** 因为 LSP（Language Server Protocol）等外部协议用 UTF-16 代码单元来表示位置。编辑器内部的字节坐标和协议坐标是两套度量衡，必须在边界处显式换算——这正是 `OffsetUtf16` / `PointUtf16` 存在的理由。

### 2.3 位置与位移：数轴直觉

把文本想成一条数轴。数轴上有两类量：

- **位置**（点）：绝对坐标，如「第 2 行第 5 列」。
- **位移**（向量）：相对跨度，如「往下 2 行、再向右 5 列」。

Rust 标准库没有内建的「点 + 向量」区分，rope 的 `Point` 用一套精心设计的加法语义同时扮演两者。理解「左操作数是位置、右操作数是位移」是读懂本讲源码的钥匙。

### 2.4 newtype 模式

`OffsetUtf16(pub usize)` 这种「只有一个字段的新结构体」叫 newtype。它在编译期把「裸整数」和「语义明确的坐标」区分开，运行期零开销（`Copy` 派生）。`Unclipped<T>` 也是同一思想的运用。

## 3. 本讲源码地图

| 文件 | 行数级别 | 职责 | 本讲关注点 |
|------|---------|------|-----------|
| [src/point.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs) | 约 150 行 | UTF-8 口径的行列坐标 `Point` | `Add`/`Sub`/`AddAssign`/`Ord`、`parse_str` |
| [src/point_utf16.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs) | 约 120 行 | UTF-16 口径的行列坐标 `PointUtf16` | 与 `Point` 的同构关系 |
| [src/offset_utf16.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs) | 约 50 行 | 一维 UTF-16 偏移 `OffsetUtf16` | 纯数值运算的 newtype |
| [src/unclipped.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs) | 约 52 行 | 「未裁剪坐标」标签 `Unclipped<T>` | 类型即契约 |
| [src/rope.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs) | 约 2200 行（不含测试） | crate 根：`Rope` 及换算 API | 坐标换算函数族、`clip` 家族 |

这四个坐标文件都是只依赖 `std`（`unclipped.rs` 额外引用 crate 内的 `ChunkSummary` 与 `sum_tree`）的「叶子模块」，可以独立阅读。`rope.rs` 顶部的再导出把它们暴露给外部：

[rope.rs:17-21](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L17-L21) 逐行 `pub use` 了 `Chunk`、`OffsetUtf16`、`Point`、`PointUtf16`、`Unclipped` ——这就是外部 crate（如 `text`、`editor`）引用这些类型的入口。

## 4. 核心概念与源码讲解

### 4.1 坐标体系总览：同一个位置，四种表达

#### 4.1.1 概念说明

rope 里「文本中某个位置」有四种表达，分属三套计量体系：

| 类型 | 维度 | row/column 或偏移的单位 | 典型来源 |
|------|------|------------------------|---------|
| `usize`（裸字节偏移） | 一维 | UTF-8 字节 | rope 内部一切区间操作 |
| `Point` | 二维 (row, column) | row = 换行符个数；column = 行内 UTF-8 字节数 | 编辑器界面（光标、选区）、内部摘要 |
| `OffsetUtf16` | 一维 | UTF-16 代码单元 | LSP 等外部协议的一维位置 |
| `PointUtf16` | 二维 (row, column) | row = 换行符个数；column = 行内 UTF-16 代码单元数 | LSP 的 `Position{line, character}` |

三套体系两两正交：维度（一维/二维）× 单位（UTF-8 字节/UTF-16 代码单元）。四个类型正好覆盖了除「一维 UTF-8」需要单独命名外的全部组合——一维 UTF-8 就是裸 `usize`，不需要 newtype。

关键认知：**这些类型之间没有隐式转换**。`Point` 和 `PointUtf16` 字段完全相同（都是 `u32` + `u32`），但语义不同，混用必须经过 rope 的显式换算函数。Rust 的类型系统在这里充当「度量衡检查器」。

#### 4.1.2 核心流程

`rope.rs` 提供了一族换算函数，把任意一种坐标转成另一种。它们共享同一个套路：

```
换算(输入坐标):
    1. 若输入已越过整根绳子的末尾 → 直接返回 summary 里缓存的末尾坐标
    2. 在 SumTree 上用输入坐标定位所在的 Chunk，
       同时取回「该 Chunk 之前所有文本」的前缀坐标 start（两种维度成对返回）
    3. overshoot = 输入坐标 - start    ← 用到本讲的 Sub 语义
    4. 返回 start 的目标维度 + 块内换算(overshoot)   ← 用到本讲的 Add 语义
```

步骤 3、4 正是 `Point`/`PointUtf16` 的 `Sub` 与 `Add` 的真实调用现场——这就是为什么 rope 要为坐标类型精心设计运算符：**换算函数的主体就是一次减法和一次加法**。

#### 4.1.3 源码精读

以 `offset_to_point`（字节偏移 → 行列）为例：

[rope.rs:397-409](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L397-L409) 是上述四步的完整落地：越界时返回 `summary().lines`（即整根绳子的末尾 `Point`，见 u2-l2）；否则 `find::<Dimensions<usize, Point>, _>` 一次查找同时拿到「字节前缀 `start.0`」和「`Point` 前缀 `start.1`」；`overshoot = offset - start.0` 算出块内字节偏移；最后 `start.1 + chunk.as_slice().offset_to_point(overshoot)` 把「块之前的行列位置」加上「块内位移」得到答案。

同族的函数还有（行号为当前 HEAD 实测）：

- [rope.rs:369](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L369) `offset_to_offset_utf16`
- [rope.rs:383-395](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L383-L395) `offset_utf16_to_offset`
- [rope.rs:411-423](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L411-L423) `offset_to_point_utf16`
- [rope.rs:425-437](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L425-L437) `point_to_point_utf16`
- [rope.rs:439-452](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L439-L452) `point_utf16_to_point`
- [rope.rs:454-464](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L454-L464) `point_to_offset`
- [rope.rs:466-477](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L466-L477) `point_to_offset_utf16`
- [rope.rs:479-485](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L479-L485) `point_utf16_to_offset` / `point_utf16_to_offset_utf16`

一共 9 个函数，覆盖四种坐标间的全部（有意义的）转换方向。本讲只需记住套路；`find`、`Dimensions` 与 SumTree 的细节留给 u2-l3。

#### 4.1.4 代码实践

**实践目标**：直观感受「同一个位置，四种坐标」的差异。

**操作步骤**（示例代码，可加进 `crates/rope/src/rope.rs` 末尾 `mod tests` 中运行，测试模块头部已有 `use super::*;`，见 [rope.rs:1727-1733](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1727-L1733)；练习后请还原源码）：

```rust
// 示例代码：观察同一位置在四套坐标下的表示
#[test]
fn test_coordinate_representations() {
    let rope = Rope::from("a🧘b");
    let offset = 5; // 'b' 的起始字节（也是合法字符边界）
    println!("len = {}", rope.len());
    println!("offset_to_point        = {:?}", rope.offset_to_point(offset));
    println!("offset_to_point_utf16  = {:?}", rope.offset_to_point_utf16(offset));
    println!("offset_to_offset_utf16 = {:?}", rope.offset_to_offset_utf16(offset));
}
```

运行：`cargo test -p rope test_coordinate_representations -- --nocapture`（在 `crates/rope` 目录下也可直接 `cargo test test_coordinate_representations -- --nocapture`）。

**需要观察的现象**：`len = 6`（1 + 4 + 1 字节），`offset_to_point` 为 `Point(0:5)`，`offset_to_point_utf16` 为列 3（`'a'` 与 `'🧘'` 共 1 + 2 个代码单元），`offset_to_offset_utf16` 为 `OffsetUtf16(3)`。

**预期结果**：UTF-8 口径的列是 5，UTF-16 口径的列是 3——同一个位置，两把尺子量出两个数。若输出与此不符，请先检查文本是否被编辑器替换成了别的 emoji。

#### 4.1.5 小练习与答案

**练习 1**：`Point` 和 `PointUtf16` 字段完全相同，为什么不直接用 `Point` 加一个布尔标志区分单位？

**参考答案**：布尔标志在编译期不可区分，容易在参数传递时搞错；而不同类型可以让编译器强制检查——把 `Point` 传给期望 `PointUtf16` 的函数会直接编译失败。这是 newtype 的核心价值：让非法状态不可表示（或至少难以表示）。

**练习 2**：为什么一维 UTF-8 偏移不需要 newtype，而一维 UTF-16 偏移需要？

**参考答案**：`usize` 是 rope 内部一切操作的基准，函数签名里出现 `usize` 默认就是字节偏移，不会与其他裸整数语义混淆（`chars` 计数走 `TextSummary` 的具名字段）。而 UTF-16 偏移与字节偏移同为 `usize` 量级、同为「偏移」，若都用裸 `usize`，调用方极易传错；`OffsetUtf16` 用类型把这个语义钉死。

### 4.2 Point：UTF-8 口径的行列坐标与非对称加法

#### 4.2.1 概念说明

`Point` 是「第几行、行内第几个字节」的二元组：

- `row`：从 0 计数的行号，等于该位置之前的换行符个数。
- `column`：从 0 计数的行内位置，单位是 **UTF-8 字节**（不是字符数！「中」会让 column 前进 3）。

`Point` 最重要的设计是它的加法：**左操作数是位置，右操作数是位移**。这不是普通向量的分量相加，而是一条专为「文本拼接」定制的规则。

#### 4.2.2 核心流程

设位置 \( p = (r_p, c_p) \)，位移 \( d = (r_d, c_d) \)：

\[ p + d = \begin{cases} (r_p,\; c_p + c_d) & \text{若 } r_d = 0 \text{（位移不跨行：列直接累加）} \\[4pt] (r_p + r_d,\; c_d) & \text{若 } r_d > 0 \text{（位移跨行：原列作废，列由位移决定）} \end{cases} \]

直觉：把 \( p \) 想成「前一段文本 A 的末尾位置」，\( d \) 想成「后一段文本 B 的跨度」。若 B 不含换行，A 的末行被接长，列相加；若 B 含换行，A 的末行后面接的是 B 的中间内容，拼接后的末尾位置落在 B 的最后一行上，列与 A 无关。

减法是加法的逆：\( p - q \) 给出「从 \( q \) 走到 \( p \)」的位移：

\[ p - q = \begin{cases} (0,\; c_p - c_q) & \text{若 } r_p = r_q \\ (r_p - r_q,\; c_p) & \text{若 } r_p > r_q \end{cases} \]

于是恒等式 \( q + (p - q) = p \) 成立（要求 \( q \le p \)，`Sub` 里有 `debug_assert!` 把关）。比较运算则按字典序：先比 row，再比 column。

#### 4.2.3 源码精读

[point.rs:7-12](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L7-L12) 定义结构体：`Point { row: u32, column: u32 }`，派生了 `Clone, Copy, Default, Eq, PartialEq, Hash`——可以像整数一样自由复制。

[point.rs:74-84](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L74-L84) 实现加法，把上面公式逐字翻译成 `if other.row == 0` 的分支。注意：跨行分支里 `self.column` 被**直接丢弃**。

[point.rs:94-106](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L94-L106) 实现减法：先 `debug_assert!(other <= self)`（减出一个「负位移」几乎必然是上游逻辑 bug），再按同行/跨行分支。同行时返回 `(0, Δcolumn)`——结果是个纯位移；跨行时返回 `(Δrow, self.column)`。

[point.rs:114-123](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L114-L123) `AddAssign` 与 `Add` 完全同构，只是原地修改。它在 rope 的摘要构造里被高频调用——例如 [rope.rs:1351-1358](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1351-L1358)：遇换行 `lines += Point::new(1, 0)`，否则 `lines.column += c.len_utf8()`，逐字符累积出文本的末尾 `Point`。

[point.rs:131-146](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L131-L146) 实现全序比较。64 位平台上把两个 `u32` 打包进一个 `usize`：

\[ \mathrm{key}(r, c) = r \times 2^{32} + c \]

因为 \( 0 \le c < 2^{32} \)，这种「进制拼接」与字典序严格等价，单次整数比较取代两次比较加分支，对 SumTree 中海量的维度比较更友好。32 位平台放不下，退回普通的先 row 后 column 比较（[point.rs:139-145](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L139-L145)）。

调用现场回顾 4.1.3 的 `offset_to_point`：[rope.rs:405-408](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L405-L408) 的 `start.1 + chunk.as_slice().offset_to_point(overshoot)` —— 位置加位移，学完本节再看那行代码应当豁然开朗。

另有两个实用方法：[point.rs:57-63](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L57-L63) `saturating_sub`（小于就返回零点，避免下溢）；[point.rs:44-51](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L44-L51) `parse_str` 按 `\n` 切分文本、取最后一行的 `(行号, 字节长度)`，即「文本末尾的 `Point`」（综合实践会用到它）。

#### 4.2.4 代码实践

**实践目标**：用测试钉死 `Point` 加法的非对称规律，并验证减法是加法的逆。

**操作步骤**（示例代码，同样放进 `mod tests`）：

```rust
// 示例代码：Point 加法的非对称性
#[test]
fn test_point_add_asymmetry() {
    // 不含坐标换算，纯运算符语义：
    let a = Point::new(0, 3);
    let b = Point::new(2, 5);

    // b 作为位移：跨行，a 的列被丢弃
    assert_eq!(a + b, Point::new(2, 5));
    // a 作为位移：不跨行，列累加
    assert_eq!(b + a, Point::new(2, 8));

    // 减法是加法的逆：q + (p - q) == p
    let p = Point::new(2, 8);
    let q = Point::new(0, 3);
    assert_eq!(q + (p - q), p);
}
```

运行 `cargo test -p rope test_point_add_asymmetry`。

**需要观察的现象**：`a + b == (2,5)` 而 `b + a == (2,8)`，加法不可交换。

**预期结果**：测试通过。规律总结——右操作数的 `row` 决定语义：`row > 0` 时结果列等于右操作数列；`row == 0` 时列相加。

#### 4.2.5 小练习与答案

**练习 1**：手算：`TextSummary::from("ab\ncd").lines + TextSummary::from("ef\ngh").lines` 应等于什么？用它验证拼接语义。

**参考答案**：`"ab\ncd"` 的末尾 `Point` 是 `(1,2)`，`"ef\ngh"` 的跨度是 `(1,2)`。按加法规则（位移跨行）：`(1+1, 2) = (2,2)`。而拼接文本 `"ab\ncdef\ngh"` 的末尾位置正是第 2 行 `"gh"` 的第 2 个字节，即 `(2,2)`。相等——这就是「A 的末尾 + B 的跨度 = A+B 的末尾」。

**练习 2**：`Point::MAX`（[point.rs:21-24](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L21-L24)）定义为 `(u32::MAX, u32::MAX)`。按 `Ord` 的打包公式，它比所有正常 `Point` 都大吗？

**参考答案**：是。任何合法文本位置满足 \( r < 2^{32}, c < 2^{32} \)，打包值 \( r \cdot 2^{32} + c \le (2^{32}-1) \cdot 2^{32} + (2^{32}-1) = 2^{64} - 1 \)，恰是 `Point::MAX` 的打包值；其他点的打包值严格更小。所以 `Point::MAX` 可安全用作「比一切实位置大」的哨兵。

**练习 3**：为什么 `Sub` 的跨行分支返回 `(r_p - r_q, c_p)` 而不是 `(r_p - r_q, 0)`？

**参考答案**：因为结果是「位移」，而 `(0, c_p)` 形式的位移加上起点 \( q \) 才能还原 \( p \)：验证 \( (r_q, c_q) + (r_p - r_q, c_p) \)，位移 row > 0，得 \( (r_p, c_p) \) ✓。若返回 `(Δr, 0)`，逆运算会得到 \( (r_p, 0) \)，丢失列信息。

### 4.3 PointUtf16：同样的壳，不同的计量单位

#### 4.3.1 概念说明

`PointUtf16` 与 `Point` 的结构、运算符实现**逐行同构**，唯一区别是 `column` 的单位从 UTF-8 字节换成 UTF-16 代码单元。它对应 LSP 的 `Position { line, character }`：`line` 是 0 起行号，`character` 是该行内从行首起的 UTF-16 代码单元数。

分开定义两个类型（而不是泛型 `Point<N>`）是刻意的取舍：两者实现都很短，复制一份换来独立的 `Ord`/运算符 impl 与更直白的错误信息，也避免了泛型参数污染所有调用点。

#### 4.3.2 核心流程

`PointUtf16` 的加、减、比较流程与 4.2.2 的公式完全相同，只是代入的列单位不同。两套行列坐标之间的桥梁是 rope 的换算函数（4.1.3 的 `point_to_point_utf16` / `point_utf16_to_point`）以及 `TextSummary` 的辅助方法 [lines_utf16()](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1307-L1312)——它把摘要里按 UTF-8 口径存的 `lines` 与 `last_line_len_utf16` 组装成 UTF-16 口径的末尾点。

#### 4.3.3 源码精读

[point_utf16.rs:6-10](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs#L6-L10) 定义：字段与 `Point` 相同的 `row: u32, column: u32`。

[point_utf16.rs:47-57](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs#L47-L57) 加法实现，与 [point.rs:74-84](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L74-L84) 逐行对照，逻辑一致。

[point_utf16.rs:67-79](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs#L67-L79) 减法实现，同样带 `debug_assert!(other <= self)`。

[point_utf16.rs:104-119](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs#L104-L119) `Ord`：与 `Point` 相同的位打包技巧。

rope 层的真实使用见 [rope.rs:2126-2132](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L2126-L2132)（`test_random_rope` 里的参照模型）：遇换行 `point_utf16 += PointUtf16::new(1, 0)`，否则 `point_utf16.column += ch.len_utf16()`——注意这里累加的是 `len_utf16()` 而非 `len_utf8()`，这正是两把尺子的分界线。

#### 4.3.4 代码实践

**实践目标**：在同一个文本上对比 `Point` 与 `PointUtf16` 的列读数。

**操作步骤**（示例代码）：

```rust
// 示例代码：同一位置的两把尺子
#[test]
fn test_point_vs_point_utf16() {
    let rope = Rope::from("🧘中");
    let end = rope.len(); // 4 + 3 = 7 字节
    let utf8_point = rope.offset_to_point(end);
    let utf16_point = rope.offset_to_point_utf16(end);
    println!("utf8  column = {}", utf8_point.column);
    println!("utf16 column = {}", utf16_point.column);
    assert_eq!(utf8_point.column, 7);
    assert_eq!(utf16_point.column, 3); // 2 + 1 个代码单元
}
```

运行 `cargo test -p rope test_point_vs_point_utf16 -- --nocapture`。

**需要观察的现象**：同一 offset，`Point` 列为 7、`PointUtf16` 列为 3。

**预期结果**：测试通过；`'🧘'` 贡献 4 字节 / 2 代码单元，`'中'` 贡献 3 字节 / 1 代码单元。

#### 4.3.5 小练习与答案

**练习 1**：如果 LSP 服务器返回 `character = 3`，而该行内容是 `"🧘中"`，这个位置落在哪？

**参考答案**：UTF-16 列 3 表示 3 个代码单元处：`'🧘'` 占代码单元 0～1，`'中'` 占 2，列 3 恰好是行尾（合法边界）。但若返回 `character = 1`，则落在 `'🧘'` 的代理对**中间**——不是合法边界，必须先 clip（见 4.5）。

**练习 2**：`PointUtf16` 没有自定义 `Debug`（用派生实现），而 `Point` 有 `"Point(row:column)"` 格式（[point.rs:14-18](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L14-L18)）。这会带来什么实际差别？

**参考答案**：`Point` 打印为 `Point(2:5)`，`PointUtf16` 打印为 `PointUtf16 { row: 2, column: 5 }`。排查日志时，前者更紧凑；更重要的是两者的输出不会混淆——即使格式相近，类型名也会提示你当前口径。

### 4.4 OffsetUtf16：一维 UTF-16 偏移

#### 4.4.1 概念说明

`OffsetUtf16` 是「从文本开头数起的 UTF-16 代码单元总数」，一个纯粹的 `usize` newtype。它服务于需要一维 UTF-16 坐标的场景（例如按协议偏移切分文本、与外部系统交换区间端点），也作为 SumTree 查找的维度之一（u2-l3 展开）。

#### 4.4.2 核心流程

因为是一维数值，`Add`/`Sub` 就是普通整数加减：

\[ \mathrm{OffsetUtf16}(a) + \mathrm{OffsetUtf16}(b) = \mathrm{OffsetUtf16}(a + b) \]

没有行列分支，没有跨行语义——对比 `Point`，可以清楚看到「二维坐标的复杂性全部来自换行」。

#### 4.4.3 源码精读

[offset_utf16.rs:3-4](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs#L3-L4) 一行定义：`pub struct OffsetUtf16(pub usize);`，派生 `Copy, Clone, Debug, Default, Eq, PartialEq, Ord, PartialOrd`——序直接用整数序。

[offset_utf16.rs:14-20](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs#L14-L20) 加法：包一层构造。同时为 `&Self` 实现了引用版（[offset_utf16.rs:6-12](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs#L6-L12)），这是 sum_tree 维度累加的常用形态（`&self += &summary` 风格）。

[offset_utf16.rs:22-37](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs#L22-L37) 减法：注意一个不对称细节——**引用版** `Sub<&Self>` 带 `debug_assert!(*other <= self)`，**值版** `Sub` 不带。两个 `usize` 相减若下溢，debug 构建会 panic、release 构建会按 Rust 整数默认规则回绕（wrap），得到一个天文数字般的「负偏移」。写代码时应当像引用版那样先验证大小关系。

#### 4.4.4 代码实践

**实践目标**：验证 offset ↔ OffsetUtf16 换算在混合文本上的往返一致性。

**操作步骤**（示例代码）：

```rust
// 示例代码：UTF-8 偏移与 UTF-16 偏移的往返
#[test]
fn test_offset_utf16_roundtrip() {
    let text = "a中🧘b\n中";
    let rope = Rope::from(text);
    for offset in 0..=rope.len() {
        if !rope.is_char_boundary(offset) {
            continue; // 只在字符边界上换算
        }
        let utf16 = rope.offset_to_offset_utf16(offset);
        let back = rope.offset_utf16_to_offset(utf16);
        assert_eq!(back, offset, "offset {offset} 往返失败");
    }
}
```

运行 `cargo test -p rope test_offset_utf16_roundtrip`。

**需要观察的现象**：对每个字符边界偏移，先转 UTF-16 再转回来，值不变。

**预期结果**：测试通过。另外可打印观察：`"a中🧘b\n中"` 总长 13 字节（1+3+4+1+1+3）、7 个代码单元（1+1+2+1+1+1）。

#### 4.4.5 小练习与答案

**练习 1**：不运行代码，推算 `Rope::from("中🧘")` 的 `len`、`summary().len_utf16` 各是多少。

**参考答案**：`len = 3 + 4 = 7`（UTF-8 字节）；`len_utf16 = 1 + 2 = 3`（UTF-16 代码单元，`OffsetUtf16(3)`）。

**练习 2**：值版 `Sub`（[offset_utf16.rs:31-37](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs#L31-L37)）没有 `debug_assert!`，这是缺陷还是合理设计？谈谈你的看法。

**参考答案**：开放题。可认为是不一致（同样的前置条件理应同样检查）；也可认为值版多用于已验证过大小关系的内层热路径，省一次断言。工程上的启示：**依赖 `debug_assert` 之外，调用方仍应自己保证 `other <= self`**，因为 release 构建里断言不生效，下溢的回绕值会顺着调用链污染后续计算（rope 在 `saturating_sub` 里就提供了防下溢的替代品，见 [point.rs:57-63](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L57-L63)）。

### 4.5 Unclipped<T>：用类型系统标注「未裁剪」坐标

#### 4.5.1 概念说明

外部送来的 UTF-16 坐标天然不可信：可能落在代理对中间、越过行尾、甚至超出整个文档。rope 对这类输入提供两条路：

- **普通路径**：函数收 `PointUtf16`，调用方自行保证坐标合法（越界时内部用 `debug_panic!` 报告契约违反）。
- **宽容路径**：函数收 `Unclipped<PointUtf16>`，明确告诉实现「这个坐标未经验证，请自行收敛到最近合法位置」。

`Unclipped<T>` 是一个零开销的标签包装器。它不改变任何运行时行为（内部字段就是 `pub T`），价值全在类型层面：**调用点必须显式写 `Unclipped(...)` 或 `.into()`，把「我知道此坐标可能非法」变成代码里看得见的一步**，而不是默默把脏数据传进去。

#### 4.5.2 核心流程

宽容路径的统一流程（以 `clip_point_utf16` 为例）：

```
clip_point_utf16(Unclipped(p), bias):
    1. 在 SumTree 上定位 p 所在的 Chunk（Bias::Right，越界时落到最后一个块）
    2. overshoot = Unclipped(p - 块起点)
    3. 在块内按 bias 把列收敛到合法的 UTF-16 / 字素边界
    4. 若 p 越过整根绳子末尾，直接返回 summary 的末尾点
```

与 4.1.2 的换算套路完全同构，差别只在第 3 步用「裁剪」替代「换算」。

#### 4.5.3 源码精读

[unclipped.rs:4-11](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs#L4-L11) 定义：`pub struct Unclipped<T>(pub T);`，派生全套常用 trait，外加 `From<T>` 让 `point.into()` 也能完成包装。

[unclipped.rs:13-23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs#L13-L23) 是点睛之笔：为 `Unclipped<T>` 转发 `sum_tree::Dimension` 实现。有了它，`Unclipped<PointUtf16>` 也能作为 SumTree 查找的维度——rope 层换算才得以把 `overshoot` 包成 `Unclipped` 一路传进块方法（[rope.rs:529](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L529)）。

[unclipped.rs:25-51](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs#L25-L51) 泛型转发 `Add`/`Sub`/`AddAssign`/`SubAssign`——包装不改变运算，只是让标签跟着值走。

rope 层的三个宽容入口：

- [rope.rs:563-571](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L563-L571) `clip_point_utf16(&self, point: Unclipped<PointUtf16>, bias)`：注意签名——**必须**传 `Unclipped`。
- [rope.rs:487-489](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L487-L489) `unclipped_point_utf16_to_offset`：把未裁剪点转成字节偏移（越界收敛到末尾）。
- [rope.rs:522-534](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L522-L534) `unclipped_point_utf16_to_point`：未裁剪 UTF-16 点 → UTF-8 点。

块内的宽容语义由一个 `clip: bool` 参数控制。[chunk.rs:517-562](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L517-L562) `point_utf16_to_offset(&self, point, clip)`：行越界、列越过行尾、落在字符中间三种情况下，`clip == false` 走 `debug_panic!`（debug 构建 panic、release 构建 `log::error!` 并带回栈，见 [gpui_util/src/lib.rs:174-183](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/gpui_util/src/lib.rs#L174-L183) 的宏定义），`clip == true` 则静默收敛到行尾/块尾。[rope.rs:479-489](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L479-L489) 显示：普通入口传 `false`，`Unclipped` 入口传 `true`——同一个块方法，两种契约，由外层 API 的**类型签名**决定。

#### 4.5.4 代码实践

**实践目标**：复现 rope 自带的 `test_clip`，理解 Unclipped 与 Bias 的组合行为。

**操作步骤**：先阅读 [rope.rs:1748-1794](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1748-L1794) 的 `test_clip`，再运行它：`cargo test -p rope test_clip`。然后尝试（示例代码，加进 `mod tests`）：

```rust
// 示例代码：Unclipped 坐标的收敛
#[test]
fn test_unclipped_behavior() {
    let rope = Rope::from("🧘"); // 4 字节 / 2 个 UTF-16 代码单元

    // 列 1 落在代理对中间：Left 收敛到 0，Right 收敛到 2
    assert_eq!(
        rope.clip_point_utf16(Unclipped(PointUtf16::new(0, 1)), Bias::Left),
        PointUtf16::new(0, 0)
    );
    assert_eq!(
        rope.clip_point_utf16(Unclipped(PointUtf16::new(0, 1)), Bias::Right),
        PointUtf16::new(0, 2)
    );
    // 列 3 越过行尾：Right 也只收敛到行尾 2
    assert_eq!(
        rope.clip_point_utf16(Unclipped(PointUtf16::new(0, 3)), Bias::Right),
        PointUtf16::new(0, 2)
    );
    // 对照：字节口径的 clip_point，列 1 同样在字符中间
    assert_eq!(rope.clip_point(Point::new(0, 1), Bias::Right), Point::new(0, 4));
}
```

运行 `cargo test -p rope test_unclipped_behavior`。

**需要观察的现象**：UTF-16 列 1 向右裁到 2（代理对末尾），字节列 1 向右裁到 4（4 字节字符末尾）——两种口径各自收敛到自己单位下的下一个合法边界。

**预期结果**：测试通过（断言即 [rope.rs:1769-1780](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1769-L1780) 与 [rope.rs:1760-1763](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1760-L1763) 的原断言）。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `clip_point_utf16` 的签名从 `Unclipped<PointUtf16>` 改成 `PointUtf16`，会损失什么？

**参考答案**：损失的是**调用点的自觉性**。现在调用方必须写 `Unclipped(p)`，reviewer 一眼看出「这里有个未验证坐标」；改回裸类型后，任何拿着 `PointUtf16` 的代码都能顺手传进来，越界与代理对中间态的问题会在远离源头的裁剪函数里才爆发。运行时行为完全一样，损失的纯是类型层的可读性与可审查性。

**练习 2**：`debug_panic!` 在 release 构建下的行为是什么？这与「不要静默丢弃错误」的规范（仓库 CLAUDE.md）是否冲突？

**参考答案**：release 下它调用 `log::error!` 输出错误与回栈但不中断程序（[gpui_util/src/lib.rs:174-183](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/gpui_util/src/lib.rs#L174-L183)）。不冲突——错误被**记录**而非吞掉，且函数接着返回收敛后的安全值，编辑器不会因一个坏坐标崩溃。这是「记录 + 降级」而非「静默丢弃」。

**练习 3**：`Unclipped<T>` 的 `Dimension` 转发实现（[unclipped.rs:13-23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs#L13-L23)）里，`zero` 与 `add_summary` 分别做了什么？

**参考答案**：`zero` 委托内层 `T::zero(())` 再包一层 `Unclipped`；`add_summary` 把 `&ChunkSummary` 交给内层 `T` 累加。即「维度语义完全由 T 决定，Unclipped 只是透传」，所以任何已实现 `Dimension` 的坐标类型都能免费获得未裁剪版本。

## 5. 综合实践

把本讲内容串成一个完整的测试模块。**实践目标**：同时验证 (a) `Point` 加法的非对称规律与减法互逆性在真实 Rope 上成立；(b) 你自己实现的 `point_display` 与 `Point::parse_str` 语义一致。

**操作步骤**：

1. 打开 `crates/rope/src/rope.rs`，滚动到文件末尾的 `mod tests`（[rope.rs:1727-1733](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1727-L1733)），把下面的代码粘进去（该模块已 `use super::*;`）。**练习结束后请删除这些改动，保持源码干净。**

```rust
// 示例代码：综合实践——坐标运算规律 + parse_str 对拍
fn point_display(p: Point) -> String {
    format!("{}:{}", p.row, p.column)
}

#[test]
fn test_coordinate_systems_integration() {
    // —— 第一部分：Point 运算规律（含 emoji 与 CJK 的真实 Rope）——
    let rope = Rope::from("中🧘\nabc\n中");
    let summary = rope.summary();

    // max_point 即整根绳子的末尾 Point（"中" 行，列 = 3 字节）
    let max = rope.max_point();
    assert_eq!(max, Point::new(2, 3));
    assert_eq!(max, summary.lines);

    // 拼接律：A 末尾 + B 跨度 == A+B 末尾（真实 Rope 上验证）
    let a = Rope::from("ab\ncd");
    let b = Rope::from("ef\ngh");
    let mut joined = a.clone();
    joined.append(b.clone());
    assert_eq!(
        a.max_point() + b.max_point(),       // 位置 + 位移
        joined.max_point()                    // 拼接后的末尾
    );

    // 减法互逆：p == q + (p - q)
    let p = rope.max_point();
    let q = Point::new(1, 2);
    assert!(q <= p);
    assert_eq!(q + (p - q), p);

    // —— 第二部分：point_display 与 Point::parse_str 对拍 ——
    for text in ["", "ab", "ab\ncd", "ab\n", "中\n🧘x", "a\nb\nc"] {
        let expected = point_display(Point::parse_str(text));
        let actual = point_display(Rope::from(text).max_point());
        assert_eq!(expected, actual, "text = {text:?}");
    }
}
```

2. 在 `crates/rope` 目录运行：`cargo test test_coordinate_systems_integration`。

**需要观察的现象**：

- `max_point()` 与 `summary().lines` 相等——`Point` 既是「位置」也是整根绳子的「跨度」。
- 拼接律成立：`(1,2) + (1,2) == (2,2)`。
- `parse_str` 对每个文本都返回该文本的末尾 `Point`，且列是**字节**口径（`"中\n🧘x"` 得 `(1,5)`：4 + 1 字节）。

**预期结果**：测试全部通过。若 `parse_str` 对拍失败，优先检查 `point_display` 是否误用了字符计数（`text.chars().count()`）而非字节长度——这是最常见的单位错误。

**待本地验证**：以上断言基于对源码的推演（公式与 [rope.rs:1351-1358](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1351-L1358) 的逐字符累积规则），请以本地 `cargo test` 的实际输出为准。

## 6. 本讲小结

- rope 用四类坐标表达同一位置：`usize`（UTF-8 字节，内部基准）、`Point`（UTF-8 行列）、`OffsetUtf16`（UTF-16 一维）、`PointUtf16`（UTF-16 行列），相互转换只有显式 API 一条路。
- `Point`/`PointUtf16` 的加法是「位置 + 位移」语义：位移跨行时位置的列被丢弃；减法是它的逆，满足 \( q + (p - q) = p \)。这正好是文本拼接末尾坐标的合并规则。
- `Ord` 用 \( r \times 2^{32} + c \) 的位打包把字典序变成一次整数比较。
- `PointUtf16` 与 `Point` 逐行同构、仅单位不同；类型隔离换来了编译期的度量衡检查。
- `Unclipped<T>` 是零开销标签，把「未验证的外部坐标」变成调用点上显式可见的一步；块内用 `clip: bool` + `debug_panic!` 实现普通/宽容两种契约。

## 7. 下一步学习建议

- **下一讲（u2-l2）**：`TextSummary`。本讲的 `Point` 运算是 `TextSummary` 合并代数的基石——两段摘要相加时 `lines` 字段正是用 `Point` 加法拼起来的，去读 [rope.rs:1395](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1395) 起的 `AddAssign` 会发现本讲公式的身影。
- 继续阅读 [point.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs) 与 [point_utf16.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs) 全文，两个文件都不足 150 行，适合通读。
- 提前浏览 [rope.rs:1441-1447](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1441-L1447) 的 `TextDimension` trait——`Point`、`PointUtf16`、`OffsetUtf16`、`usize` 都实现了它，u2-l3 会解释它如何让 SumTree 一次查找同时服务多种坐标。
