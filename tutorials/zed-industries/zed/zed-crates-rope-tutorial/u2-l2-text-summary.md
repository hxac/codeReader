# TextSummary：一段文本的全部统计信息

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐一说出 `TextSummary` 九个字段（`len` / `chars` / `len_utf16` / `lines` / `first_line_chars` / `last_line_chars` / `last_line_len_utf16` / `longest_row` / `longest_row_chars`）的含义，以及它们分别支撑了哪些 O(1) 查询。
2. 手工推导 `From<&str>` 如何逐字符构造出一份摘要，包括 `longest_row` 的「平局取最早行」规则。
3. 独立推导 `AddAssign` 的合并代数：两段文本拼接时，首行/末行字段如何合并、`longest_row` 如何平移。
4. 解释「块摘要 → 树节点摘要 → 整根绳子摘要」这条逐层累加链路，理解为什么 `Rope::summary()` 是 O(1)。
5. 读懂 `newline()` / `add_newline()` 这两个预制换行摘要，并能识别其中的疑点。

## 2. 前置知识

本讲建立在 u2-l1（坐标系统）之上，先回顾三个关键认知：

- **字节偏移（`usize`）是基准坐标**。`Point` 的 `column` 字段按 **UTF-8 字节数**计，而不是字符数。
- **三种「长度」口径不同**：一个 emoji（如 😀，U+1F600）占 4 个 UTF-8 字节、算 1 个字符（char）、占 2 个 UTF-16 码元。LSP 等外部协议用 UTF-16 口径，所以摘要必须同时维护三套计数。
- **`Point` 的加法是「位置 + 位移」语义且不可交换**：`Point(0, 2) + Point(1, 0) = Point(1, 0)`——位移跨行时会丢弃位置里的列。这正是文本拼接时「末行合并」在坐标层面的体现，本讲的 `AddAssign` 会反复用到它。

另外需要一点抽象代数的直觉：**monoid（幺半群）**。一个集合带上一个满足结合律的二元运算和一个单位元，就构成 monoid。本讲的核心思想是：**`TextSummary` 配上 `+=` 构成一个 monoid，因此可以挂在前缀和树（SumTree）上任意分块、任意顺序地折叠，而不损失信息**。不理解这个名词也不影响阅读，记住「可拼、可结合」即可。

为什么需要摘要？如果每次想知道「这个文件有多少行」都要全文扫描一遍，编辑器在百万字节的文件上会卡死。`TextSummary` 的思路是：**预先把统计信息算好并缓存在树的节点里，任何一次编辑只顺带更新 O(log n) 个节点的缓存**。缓存能这么做的前提，是统计量本身满足「两段各自的摘要能够 O(1) 拼出合并后的摘要」——这就是 `AddAssign` 存在的意义。

## 3. 本讲源码地图

| 文件 | 本讲关注的部分 | 作用 |
|---|---|---|
| `crates/rope/src/rope.rs` | L1280–L1304 | `TextSummary` 结构体定义（九个字段） |
| `crates/rope/src/rope.rs` | L1306–L1335 | `lines_utf16()`、`newline()`、`add_newline()` |
| `crates/rope/src/rope.rs` | L1337–L1383 | `From<&str>`：逐字符构造摘要 |
| `crates/rope/src/rope.rs` | L1385–L1439 | `ContextLessSummary`、`Add`、`AddAssign` 合并代数 |
| `crates/rope/src/rope.rs` | L1255–L1278 | `Item for Chunk` 与 `ChunkSummary`：块接入 SumTree 的挂点 |
| `crates/rope/src/rope.rs` | L312–L330 | `Rope::summary/len/max_point`：O(1) 查询入口 |
| `crates/rope/src/rope.rs` | L745–L775 | `Cursor::summary`：任意区间的摘要计算 |
| `crates/rope/src/rope.rs` | L2161–L2200 | 随机测试中对摘要与 `longest_row` 的断言 |
| `crates/rope/src/chunk.rs` | L316–L405 | `ChunkSlice::text_summary` 等位图版 O(1) 统计 |
| `crates/rope/src/offset_utf16.rs` | L45–L49 | `OffsetUtf16` 的 `AddAssign`（`add_newline` 疑点的钥匙） |
| `crates/text/src/tests.rs` | L289–L330 | 上层 crate 手写 `TextSummary` 字面量做断言的实例 |

## 4. 核心概念与源码讲解

### 4.1 TextSummary 的九个字段与 From<&str> 逐字符构造

#### 4.1.1 概念说明

`TextSummary` 是「一段文本的体检报告」：给定任意一段文本，它用一个固定大小的结构体回答九个问题。它 Copy、字段全公开、可直接 `assert_eq!` 比较——这些性质让上层代码（如 `crates/text` 的测试）能像写普通数字一样手写期望值。

九个字段可以分为三组：

| 组 | 字段 | 类型 | 含义 | 支撑的 O(1) 查询 |
|---|---|---|---|---|
| 数量 | `len` | `usize` | UTF-8 字节数 | `Rope::len()` |
| 数量 | `chars` | `usize` | 字符（Unicode 标量值）个数 | `rope.summary().chars` |
| 数量 | `len_utf16` | `OffsetUtf16` | UTF-16 码元数 | UTF-16 坐标换算的边界判断 |
| 位置 | `lines` | `Point` | 行数 + 末行字节数，即 EOF 的行列位置 | `Rope::max_point()`、`offset_to_point` 的越界短路 |
| 首末行 | `first_line_chars` | `u32` | 首行字符数 | **仅为合并服务**（见 4.2） |
| 首末行 | `last_line_chars` | `u32` | 末行字符数 | `Rope::line_len(最后一行)`、合并 |
| 首末行 | `last_line_len_utf16` | `u32` | 末行 UTF-16 码元数 | `lines_utf16()` 组装 UTF-16 口径的 EOF |
| 最长行 | `longest_row` | `u32` | 字符数最多的行的行号 | 最长行定位 |
| 最长行 | `longest_row_chars` | `u32` | 最长行的字符数 | 横向滚动范围估算（如 `lsp_store` 中取 `longest_row_chars`） |

两个容易困惑的点先澄清：

- **为什么需要 `first_line_chars` / `last_line_chars` 这些「首末行」字段？** 它们对单段文本来说似乎是冗余的（首行字符数扫描一遍就能知道）。它们存在的唯一理由是**让合并成为 O(1)**：两段文本拼接时，唯一「说不清」的行是拼接处——它由前段的末行和后段的首行连成。只要每段摘要都记着自己的首行和末行，拼接行的长度就能直接算出来。这是「为可拼性付出的字段开销」。
- **`longest_row` 为什么是行号而不是内容？** 摘要只存统计量。行号 + 长度足以支撑「找最长行」类查询，内容再按行号去取。

#### 4.1.2 核心流程

`From<&str>` 是摘要的「朴素定义」：对整段文本做一次 O(n) 的线性扫描。伪代码如下（对照源码 L1337–L1383）：

```text
len_utf16, lines, first_line_chars, last_line_chars,
last_line_len_utf16, longest_row, longest_row_chars, chars = 0

for c in text.chars():              # 按 Unicode 标量值迭代
    chars += 1
    len_utf16 += c.len_utf16()      # BMP 外字符计 2
    if c == '\n':
        lines += Point(1, 0)        # 跨行位移：丢弃当前列（u2-l1 的语义）
        last_line_chars = 0
        last_line_len_utf16 = 0
    else:
        lines.column += c.len_utf8() as u32   # Point 列按字节累加
        last_line_len_utf16 += c.len_utf16() as u32
        last_line_chars += 1
    if lines.row == 0:
        first_line_chars = last_line_chars    # 还在第一行，持续刷新
    if last_line_chars > longest_row_chars:   # 严格大于 → 平局保留更早的行
        longest_row = lines.row
        longest_row_chars = last_line_chars

len = text.len()                    # 字节数最后一次性取
```

注意三个细节：

1. `first_line_chars` 的刷新条件是 `lines.row == 0`——一旦遇到第一个换行符，`lines.row` 变成 1，之后就不再更新，于是它「冻结」在第一行的长度。
2. `longest_row` 的比较是**严格大于**（`>`）。若两行字符数相同，先出现者胜。这就是 tie-break 规则。
3. 换行符本身不计入任何行的 `last_line_chars`（它属于行分隔符），但计入 `chars`、`len`、`len_utf16` 和 `lines.row`。

#### 4.1.3 源码精读

结构体定义（每个字段的文档注释都值得读一遍）：

[crates/rope/src/rope.rs:L1280-L1304](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1280-L1304)——定义 `TextSummary` 的九个字段。特别注意 `lines` 的注释：它标记的是「最后一个字节之后的位置」（假如 EOF 是一个字符，它就在这里），这个视角让 `lines` 天然等于 `max_point`。

`From<&str>` 的逐字符循环：

[crates/rope/src/rope.rs:L1347-L1369](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1347-L1369)——循环体分四个动作：累加三种长度计数（L1348–L1349）、按是否换行更新 `lines` 与末行字段（L1351–L1359）、在第一行内刷新 `first_line_chars`（L1361–L1363）、用严格大于更新最长行（L1365–L1368）。

`lines_utf16()` 辅助方法：

[crates/rope/src/rope.rs:L1307-L1312](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1307-L1312)——把 `lines.row`（行数）与 `last_line_len_utf16`（末行 UTF-16 长度）拼成一个 `PointUtf16`，即 UTF-16 口径下的 EOF 位置。行号在两种口径下一致，只有列需要换算，所以这里不用扫描全文。

上层 crate 的真实用法（证明这份「报告」确实被手写比较）：

[crates/text/src/tests.rs:L289-L302](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L289-L302)——`crates/text` 的测试对 `"ab\nefg\nhklm\nnopqrs\ntuvwxyz"` 的区间 `0..2`（即 `"ab"`）手写了完整的 `TextSummary` 字面量做断言。同文件 L303–L316 断言区间 `1..3`（即 `"b\n"`）：注意 `lines` 为 `Point::new(1, 0)`、`last_line_chars` 为 0——**以换行符结尾的文本，其「末行」是换行符后面的空行**。

#### 4.1.4 代码实践

**实践目标**：用手算 + 程序输出双重验证 `From<&str>`，并观察三种长度口径的分岔与 `longest_row` 的 tie-break。

**操作步骤**：

1. 在你自己的 Zed 克隆里打开 `crates/rope/src/rope.rs`，滚动到文件尾部的 `mod tests`，在最后一个测试函数后面加入下面的测试（这是读者练习；本讲义本身不修改源码）。
2. 在仓库根目录运行 `cargo test -p rope text_summary_fields_lab`。

```rust
// 示例代码：读者练习，加在 crates/rope/src/rope.rs 的 mod tests 内
#[test]
fn text_summary_fields_lab() {
    // 用例 1：三种长度口径分岔（1 字节 + 4 字节 emoji + 换行 + 1 字节）
    let s = TextSummary::from("a\u{1F600}\nb");
    assert_eq!(s.len, 7);            // 1 + 4 + 1 + 1 个字节
    assert_eq!(s.chars, 4);          // a, 😀, \n, b
    assert_eq!(s.len_utf16, OffsetUtf16(5)); // 1 + 2 + 1 + 1 个码元
    assert_eq!(s.lines, Point::new(1, 1));   // 末行只有 'b'，1 个字节
    assert_eq!(s.first_line_chars, 2);       // 'a' 和 😀
    assert_eq!(s.last_line_chars, 1);
    assert_eq!(s.last_line_len_utf16, 1);
    assert_eq!((s.longest_row, s.longest_row_chars), (0, 2)); // 首行更长

    // 用例 2：tie-break——两行都是 2 个字符，取更早的 row 0
    let s = TextSummary::from("ab\ncd");
    assert_eq!((s.longest_row, s.longest_row_chars), (0, 2));
    assert_eq!(s.lines, Point::new(1, 2));
    assert_eq!(s.first_line_chars, 2);

    // 用例 3：以换行符结尾（对照 crates/text/src/tests.rs 的 1..3 用例）
    let s = TextSummary::from("b\n");
    assert_eq!(s.lines, Point::new(1, 0));
    assert_eq!(s.last_line_chars, 0);   // 换行后的空行
    assert_eq!((s.longest_row, s.longest_row_chars), (0, 1));
}
```

**需要观察的现象**：

- 用例 1 中 `len`、`chars`、`len_utf16` 是三个不同的数字（7 / 4 / 5）——这是理解后续 UTF-16 坐标换算的地基。
- 用例 2 中 `longest_row` 是 0 而不是 1，尽管两行长度相等。

**预期结果**：三个用例全部通过。若把用例 2 的断言改成 `(1, 2)` 会失败——平局时先出现的行获胜。

**待本地验证**：请实际运行确认（本讲义作者未替你执行）。

#### 4.1.5 小练习与答案

**练习 1**：`TextSummary::from("")` 的九个字段各是多少？

**答案**：全零——`len = 0, chars = 0, len_utf16 = OffsetUtf16(0), lines = Point(0, 0), first_line_chars = 0, last_line_chars = 0, last_line_len_utf16 = 0, longest_row = 0, longest_row_chars = 0`。循环一次都不执行，所有累加器保持初值。这也说明**空摘要（`Default`）就是 `+=` 运算的单位元**，是 monoid 的「幺」。

**练习 2**：`TextSummary::from("ab\ncdef\n")` 的 `lines` 与 `last_line_chars` 是多少？

**答案**：`lines = Point::new(2, 0)`，`last_line_chars = 0`。文本有两个换行符，行数推进到 2；最后一个字符是换行符，把末行字段清零，`lines.column` 被 `Point(1, 0)` 位移清零。`first_line_chars = 2`，最长行是 row 1（4 个字符）。

**练习 3**：为什么 `first_line_chars` 用 `u32` 而 `len` 用 `usize`？

**答案**：从源码注释与用法看（待确认——源码未显式说明动机）：`len` 必须能表示整段文本的字节数，受 `usize` 寻址范围约束；而首末行、最长行是「单行」尺度上的统计，`u32`（约 42 亿）对单行字符数绰绰有余，用更窄的类型可以让整个 `TextSummary` 结构体更小、`Copy` 更便宜——树上每个节点都要缓存一份，字段大小直接影响缓存命中率。

### 4.2 ops::AddAssign：两段摘要的合并代数

#### 4.2.1 概念说明

`AddAssign` 回答的问题是：**已知前段文本 A 和后段文本 B 各自的摘要，不重新扫描，能否 O(1) 得到 `A · B`（A 接在 B 前面）的摘要？**

答案是肯定的，但九个字段里只有四个（`chars`、`len`、`len_utf16`、`lines`）是「直接相加」的，其余五个都要特判。核心难点只有一个：**拼接处那一行**。设

\[ S = \text{summary}(A), \quad O = \text{summary}(B) \]

拼接行的字符数为

\[ j = S.\text{last\_line\_chars} + O.\text{first\_line\_chars} \]

它位于行号 \( S.\text{lines}.row \)（A 的最后一行，从 0 数起）。这个新行会参与「最长行」的竞争。

#### 4.2.2 核心流程

`AddAssign` 的五个步骤（对照源码 L1404–L1433）：

```text
fn add_assign(self = S, other = O):
    # 1. 拼接行参与最长行竞争（严格 >，平局保留 S 原有的）
    j = S.last_line_chars + O.first_line_chars
    if j > S.longest_row_chars:
        S.longest_row     = S.lines.row        # 拼接发生在 A 的末行
        S.longest_row_chars = j
    # 2. O 内部的最长行若仍更大，行号要加上 A 的行数平移
    if O.longest_row_chars > S.longest_row_chars:
        S.longest_row       = S.lines.row + O.longest_row   # 注意用合并前的 S.lines.row
        S.longest_row_chars = O.longest_row_chars
    # 3. 首行：只有 A 整体只有一行时，A 的首行才会被拼接延长
    if S.lines.row == 0:
        S.first_line_chars += O.first_line_chars
    # 4. 末行：只有 B 整体只有一行时，拼接行才是新的末行
    if O.lines.row == 0:
        S.last_line_chars      += O.first_line_chars        # 单行文本 first == last
        S.last_line_len_utf16 += O.last_line_len_utf16
    else:
        S.last_line_chars      = O.last_line_chars
        S.last_line_len_utf16 = O.last_line_len_utf16
    # 5. 数量与位置：直接相加（Point 加法即「位置+位移」）
    S.chars     += O.chars
    S.len       += O.len
    S.len_utf16 += O.len_utf16
    S.lines     += O.lines
```

末行字段的合并规则写成数学形式：

\[
\text{last}(A \cdot B) =
\begin{cases}
\text{last}(A) + \text{first}(B) & \text{rows}(B) = 0 \text{（B 只有一行，拼接行即末行）} \\
\text{last}(B) & \text{rows}(B) > 0 \text{（B 自带换行，末行在 B 内部）}
\end{cases}
\]

三个语义细节：

1. **顺序敏感**：步骤 2 中 `S.longest_row = S.lines.row + O.longest_row` 用的是**还没执行步骤 5** 的 `S.lines.row`。行号平移量是 A 的行数，若先做了 `S.lines += O.lines` 再平移就会翻倍错。
2. **平局语义一致**：两处比较都是严格 `>`，与 `From<&str>` 的 tie-break 一致——多个行等长时，**最早出现的行**最终获胜。注意 crate 自己的随机测试（L2170–L2200）对此更宽容：它收集所有等长的最长行，只断言 `longest_row` 是其中**之一**，不锁定具体是哪个。这是测试对实现细节的防御性放松。
3. **结合律**：由于 `merge(S, O)` 恒等于 `From(A 的文本拼上 B 的文本)`，而字符串拼接满足结合律，所以 `(TextSummary, +=)` 满足结合律、空摘要是单位元——完整的 monoid。这正是 SumTree 能按任意树形折叠摘要的数学保证。

#### 4.2.3 源码精读

[crates/rope/src/rope.rs:L1404-L1433](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1404-L1433)——`AddAssign<&TextSummary>` 的完整实现。L1406–L1414 处理最长行（拼接行竞争 + O 内部最长行平移），L1416–L1418 处理首行，L1420–L1426 处理末行（`if other.lines.row == 0` 分支里累加的是 `other.first_line_chars`——因为单行文本的 first 与 last 相等，两者等价），L1428–L1431 做四个「可加」字段的直加。

[crates/rope/src/rope.rs:L1395-L1402](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1395-L1402)——`Add<Self>` 只是「取值版」包装：克隆左值后委托给 `AddAssign`。保持两个 trait 共用一份逻辑，避免漂移。

[crates/rope/src/rope.rs:L1385-L1393](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1385-L1393)——`ContextLessSummary for TextSummary` 把 `+=` 接入 sum_tree 的世界：`add_summary` 直接委托给 `*self += summary`。SumTree 内部维护子树摘要时调用的就是这里。

crate 自己的对拍测试（本讲实践的思想源头）：

[crates/rope/src/rope.rs:L2161-L2167](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L2161-L2167)——随机测试对随机区间断言 `cursor(start).summary::<TextSummary>(end) == TextSummary::from(&expected[start..end])`：左边走的是「块摘要合并」路径，右边走的是「朴素扫描」路径，两者必须处处一致。这就是合并代数的正确性契约。

#### 4.2.4 代码实践

**实践目标**：手算两段文本的摘要，按合并规则推出拼接结果，再用测试对照；覆盖 tie-break 用例。

**操作步骤**：

1. **手算**（先不要运行代码）。分别对 `"ab\ncd"` 和 `"ef"` 填写下表，然后按 4.2.2 的五个步骤推出 `merged = S("ab\ncd") += O("ef")`：

| 字段 | S = from("ab\ncd") | O = from("ef") | merged（手算） |
|---|---|---|---|
| `len` | 5 | 2 | 7 |
| `chars` | 5 | 2 | 7 |
| `len_utf16` | 5 | 5 | 7 |
| `lines` | (1, 2) | (0, 2) | (1, 4) |
| `first_line_chars` | 2 | 2 | 2 |
| `last_line_chars` | 2 | 2 | 4 |
| `last_line_len_utf16` | 2 | 2 | 4 |
| `longest_row` | 0 | 0 | 1 |
| `longest_row_chars` | 2 | 2 | 4 |

   推导要点：拼接行 \( j = 2 + 2 = 4 > 2 \)，于是 `longest_row = S.lines.row = 1`、`longest_row_chars = 4`；`O.lines.row == 0`，末行 = `2 + 2 = 4`；`S.lines.row != 0`，首行保持 2。

2. **写测试**（示例代码，加在 `mod tests` 内）：

```rust
#[test]
fn text_summary_merge_lab() {
    // 基本合并：合并结果必须等于对拼接文本的直接扫描
    let mut merged = TextSummary::from("ab\ncd");
    merged += &TextSummary::from("ef");
    assert_eq!(merged, TextSummary::from("ab\ncdef"));

    // tie-break：拼接行与 S 原有的最长行等长（4 == 4），保留 S 的 row 0
    let mut tied = TextSummary::from("abcd\nxy"); // 最长行 row 0，4 字符
    tied += &TextSummary::from("AB");             // 拼接行 row 1 也是 4 字符
    assert_eq!(tied.longest_row, 0);              // 严格 > ：不更新
    assert_eq!(tied.longest_row_chars, 4);
    assert_eq!(tied, TextSummary::from("abcd\nxyAB"));

    // 后段自带换行：末行直接采用后段的末行
    let mut tail = TextSummary::from("ab");
    tail += &TextSummary::from("cd\nef");
    assert_eq!(tail.last_line_chars, 2);          // 'e','f'
    assert_eq!(tail.lines, Point::new(1, 2));
}
```

3. 运行 `cargo test -p rope text_summary_merge_lab`。

**需要观察的现象**：三组断言全部通过；特别是第二组，`longest_row` 停留在 0 而不是变成 1。

**预期结果**：通过。若手算表格与断言值有任何一处不一致，回到 4.2.2 的五个步骤定位是哪个字段的规则理解偏了。

**待本地验证**：请实际运行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AddAssign` 中 `other.lines.row == 0` 分支累加的是 `other.first_line_chars` 而不是 `other.last_line_chars`？两者何时不同？

**答案**：当 `other.lines.row == 0`（后段不含换行符）时，后段只有一行，首行即末行，`first_line_chars == last_line_chars`，两个写法等价。两者只在后段含换行符时不同，而那种情况走的是 `else` 分支（直接覆盖）。所以这是一个等价的写法选择（源码未注释说明原因，待确认），语义上无差别。

**练习 2**：`AddAssign` 里如果交换步骤 2 与步骤 5 的顺序（先 `self.lines += other.lines` 再平移 `longest_row`），会出什么错？

**答案**：`self.longest_row = self.lines.row + other.longest_row` 中的 `self.lines.row` 必须是**合并前**（即 A 自己）的行数。若先执行了 `self.lines += other.lines`，`self.lines.row` 已经变成两段总行数，平移结果会多加一次 `other.lines.row`，`longest_row` 越界偏大。

**练习 3**：`chars`、`len`、`len_utf16` 为什么可以「无脑相加」而不需要首末行那样的特判？

**答案**：因为它们是**两段无关的可加量**：拼接不改变 A 部分的计数，也不改变 B 部分的计数。首末行字段本质上统计的是「与边界有关的量」，拼接会把边界粘起来，所以必须特判。这也提示了一个一般性原则：**想往 `TextSummary` 里加新字段，先问它「拼接时能否由两段的对应字段 O(1) 推出」——不能的话就必须附加辅助字段**。

### 4.3 newline() 与 add_newline()：预构造的换行摘要

#### 4.3.1 概念说明

编辑场景里最频繁的单字符编辑之一就是敲回车。为此 rope 提供了两个与换行符相关的便利 API：

- `TextSummary::newline()`：返回字符串 `"\n"` 的预制摘要，一个关联函数，可以直接与其他摘要做 `+=`。
- `TextSummary::add_newline(&mut self)`：原地方法，语义上是「给这段文本追加一个换行符」，即等价于 `*self += TextSummary::newline()` 的意图。

这一小模块的价值在于：它是**最小、最完整的合并代数应用题**——单字符摘要与任意摘要的合并。同时它也藏着两个值得警惕的疑点（见 4.3.3），是绝佳的批判性源码阅读材料。

#### 4.3.2 核心流程

`newline()` 的字段值可以完全由 `From<&str>("\n")` 推出：

```text
newline() = TextSummary {
    len: 1,                        // '\n' 1 个字节
    chars: 1,                      // 1 个字符
    len_utf16: OffsetUtf16(1),     // 1 个码元
    first_line_chars: 0,           // 换行符不属于任何行的内容
    last_line_chars: 0,
    last_line_len_utf16: 0,
    lines: Point::new(1, 0),       // 行数 +1，末行是空行
    longest_row: 0, longest_row_chars: 0,   // 空文本的最长行为 row 0、0 字符
}
```

`add_newline` 的**意图**（按方法名与字段选择推断）是等价变换：

\[ \texttt{add\_newline}(S) \stackrel{?}{=} S + \text{newline()} \]

对照 `AddAssign` 的规则，`S + newline()` 应该做的动作是：`len += 1`、`chars += 1`、`len_utf16 += 1`、末行字段清零、`lines += Point(1, 0)`、最长行不变（换行不延长任何行，且清零后的末行长度 0 不会赢得竞争）。

#### 4.3.3 源码精读

[crates/rope/src/rope.rs:L1314-L1326](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1314-L1326)——`newline()` 返回上面推导的字面量。可用 `assert_eq!(TextSummary::newline(), TextSummary::from("\n"))` 验证两者一致。

[crates/rope/src/rope.rs:L1328-L1335](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1328-L1335)——`add_newline` 的实现。逐行对照「意图」：

- L1329 `self.len += 1`：正确。
- L1330 `self.len_utf16 += OffsetUtf16(self.len_utf16.0 + 1)`：**疑点一**。查 [crates/rope/src/offset_utf16.rs:L45-L49](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs#L45-L49)，`OffsetUtf16` 的 `AddAssign` 执行 `self.0 += other.0`，所以这一行的实际效果是

  \[ \text{new} = \text{old} + (\text{old} + 1) = 2 \cdot \text{old} + 1 \]

  只有当 `old == 0` 时碰巧得到正确的 1；当 `old > 0` 时 `len_utf16` 会翻倍加一，与「加一个换行符」不符。疑似本意是 `self.len_utf16 += OffsetUtf16(1)`。
- L1331–L1332 末行字段清零：正确。
- L1333 `self.lines += Point::new(1, 0)`：正确（跨行位移清零列）。
- **疑点二**：没有 `self.chars += 1`，而 `newline()` 里 `chars` 是 1。用 `add_newline` 之后的 `chars` 会比 `From` 口径少 1。

**佐证与定性**：在 `crates/rope/src` 全目录搜索 `add_newline`，只有 L1328 这一处定义、没有任何调用点；`newline()` 同样没有 crate 内调用者。也就是说这两个疑点当前不影响 crate 自身行为，更像是预留（或有待修正）的公共 API。是否为上游笔误，**待确认**——读者可到 zed 仓库提 issue 求证。这也是读源码的重要一课：**pub API 不等于被验证过的 API，没被测试覆盖的代码要带着怀疑读**。

#### 4.3.4 代码实践

**实践目标**：用测试实证 `newline()` 与 `From<&str>` 的一致性，以及 `add_newline` 的实际行为与意图的偏差。

**操作步骤**：

1. 在 `mod tests` 中加入（示例代码）：

```rust
#[test]
fn text_summary_newline_lab() {
    // newline() 与朴素扫描一致
    assert_eq!(TextSummary::newline(), TextSummary::from("\n"));

    // 单字符摘要是合并代数的好单位：S + newline() 应等于扫描 "ab\n"
    let mut s = TextSummary::from("ab");
    s += &TextSummary::newline();
    assert_eq!(s, TextSummary::from("ab\n"));

    // add_newline 的实际行为：从 Default 出发
    let mut t = TextSummary::default();
    t.add_newline();
    // 意图是 from("\n")；观察 chars 与 len_utf16 两个字段
    println!("add_newline from default: {:?}", t);

    // 从非零 len_utf16 出发，放大疑点一
    let mut u = TextSummary::from("ab"); // len_utf16 == 2
    u.add_newline();
    println!("add_newline after 'ab': {:?}", u);
    // 按 From 口径应等于 from("ab\n")：
    // len == 3, chars == 3, len_utf16 == OffsetUtf16(3), lines == (1, 0)
}
```

2. 把 `println!` 的输出与 `TextSummary::from("ab\n")` 的期望值（`len=3, chars=3, len_utf16=3, lines=(1,0), last_line_chars=0`）逐字段对比。
3. 用 `cargo test -p rope text_summary_newline_lab -- --nocapture` 运行以看到打印。

**需要观察的现象**：`s += &newline()` 与 `from("ab\n")` 相等（合并代数自洽）；而 `u` 的 `len_utf16` 按 \( 2 \times 2 + 1 = 5 \) 变化（不是 3）、`chars` 停留在 2（不是 3）——与「追加一个换行符」的意图不符。

**预期结果**：前两组断言通过；`add_newline` 部分的打印值与手推的 \( 2 \cdot \text{old} + 1 \) 规律吻合。此为源码可推导的确定行为，但请以实际运行输出为准（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`TextSummary::newline()` 与 `TextSummary::default()` 相比，哪些字段不同？

**答案**：`len`（0→1）、`chars`（0→1）、`len_utf16`（0→1）、`lines`（(0,0)→(1,0)）四个字段不同；首末行与最长行字段在两者中都是 0。

**练习 2**：为什么 `add_newline` 里不需要更新 `longest_row` / `longest_row_chars`？

**答案**：追加换行符不会延长任何已有行；它把（清零后的）末行变成空行。长度 0 的空行只有在整段文本所有行都是空行时才可能并列最长，而那种情况下 `longest_row_chars` 本来就是 0、`longest_row` 本来就是 0（由 `From` 的严格 `>` 与初值保证），不更新也是对的。

**练习 3**：如果让你修复 `add_newline`，最小改动是什么？

**答案**：把 L1330 改为 `self.len_utf16 += OffsetUtf16(1);`，并补一行 `self.chars += 1;`。改完可以用 `assert_eq!` 断言 `add_newline` 后的摘要等于 `From` 口径下追加了 `"\n"` 的结果（即练习中 `u` 应等于 `TextSummary::from("ab\n")`）。当然，是否真的要改由上游维护者决定——先提 issue 确认意图（**待确认**）。

### 4.4 从块摘要到整根绳子：SumTree 的逐层累加

#### 4.4.1 概念说明

前面三个模块解决了「单块文本的摘要怎么算、两块摘要怎么拼」。本模块把它们串成 rope 真正使用的链路：

```text
Chunk（≤128 字节的块）
  │  Chunk::summary()            —— Item trait 的挂点
  ▼
ChunkSummary { text: TextSummary }   —— 单块摘要（由位图 O(1) 算出）
  │  ChunkSummary::add_summary()  —— 委托给 TextSummary::add_assign
  ▼
SumTree 内部节点摘要            —— 子树所有块摘要的前缀和（逐层向上累加）
  │  树根摘要
  ▼
Rope::summary()                 —— O(1) 读根缓存
```

关键认知有三点：

1. **块摘要不是扫出来的，是位图数出来的**。`ChunkSlice::text_summary()` 用 popcount（数位图中 1 的个数）直接得到 `chars`、`len_utf16`、`lines` 等字段，比逐字符扫描快得多（位图细节在 u2-l4 展开）。
2. **树的中间节点缓存子树摘要的和**。`push` / `append` 等写操作在更新树结构时，只需把路径上 O(log n) 个节点的缓存重新 `add_summary` 一遍——这就是「编辑后统计仍然 O(log n)」的来源。
3. **全文摘要从不重算**。`Rope::summary()` 只是读根节点缓存；`Rope::len()` 与 `Rope::max_point()` 是同一份缓存向 `usize` / `Point` 维度的投影（extent）。由于 `(TextSummary, +=)` 是 monoid，树怎么平衡、块怎么切，折出来的结果都一样——4.2 的结合律在这里兑现。

#### 4.4.2 核心流程

以 `Rope::from("一段 500 字节的文本")` 为例：

```text
1. push 把文本切成若干 ≤ MAX_BASE(128) 字节的 Chunk；
2. 每个 Chunk 挂上 SumTree 时，Item::summary() 计算它的 ChunkSummary
   （内部走 ChunkSlice::text_summary() 的位图统计）；
3. SumTree 在插入/追加时自底向上维护每个内部节点的摘要
   （对子节点摘要依次调用 add_summary，即 TextSummary 的 +=）；
4. 查询：
   - rope.summary()  → 根节点缓存的 TextSummary
   - rope.len()      → 同一缓存向 usize 维度的投影（extent）
   - rope.max_point()→ 向 Point 维度的投影，值恒等于 summary().lines
   - rope.cursor(a).summary::<TextSummary>(b)
                   → 区间 [a, b) 的摘要 = 起点块内切片摘要
                     + 中间整块摘要（树上一段连续子树的和）
                     + 终点块内切片摘要
```

第 4 步的区间摘要尤其体现「前缀和」思想：区间和 = 两个前缀和之差的推广，在树上表现为「定位起止块 + 累加中间子树 + 两端块内切片」，无需触碰区间外的任何块。

#### 4.4.3 源码精读

块接入 SumTree 的挂点：

[crates/rope/src/rope.rs:L1255-L1263](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1255-L1263)——`impl sum_tree::Item for Chunk`：每个块的摘要就是一个 `ChunkSummary`，内容来自 `as_slice().text_summary()`。

[crates/rope/src/rope.rs:L1265-L1278](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1265-L1278)——`ChunkSummary` 只包了一个 `text: TextSummary`；它的 `add_summary` 委托给 `self.text += &summary.text`。**树的累加语义完全由 4.2 的合并代数决定**，中间没有任何额外魔法。

位图版摘要（块内 O(1) 统计）：

[crates/rope/src/chunk.rs:L316-L331](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L316-L331)——`ChunkSlice::text_summary()` 组装九个字段：`chars` 与 `longest_row` 由 `longest_row(&mut chars)` 一次循环顺带算出，其余字段各自由下面的 popcount 方法给出。

[crates/rope/src/chunk.rs:L353-L369](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L353-L369)——`first_line_chars` / `last_line_chars` / `last_line_len_utf16`：分别用 `newlines` 位图的第一个/最后一个置位位做掩码，再与 `chars`（或 `chars_utf16`）位图按位与后数 1。「首个换行符之前的字符」和「最后一个换行符之后的字符」被翻译成了两次位运算。

[crates/rope/src/chunk.rs:L371-L405](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L371-L405)——`longest_row(&mut total_chars)`：在块内逐行消费 `newlines` 位图（每次剥掉最低位的换行符及其之前的部分），统计每行字符数并维护最长行，顺带累计全块字符数。注意 L385 的比较同样是严格 `>`，与 `From<&str>` 的 tie-break 一致——**两套实现必须保持同一套语义**，否则块路径与扫描路径会对同一文本给出不同摘要。

O(1) 查询入口：

[crates/rope/src/rope.rs:L312-L318](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L312-L318)——`Rope::summary()` 返回根缓存的 `text`；`Rope::len()` 是 extent 投影。二者都不触发任何扫描。

[crates/rope/src/rope.rs:L324-L330](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L324-L330)——`max_point()` / `max_point_utf16()` 同为 extent 投影；`summary().lines` 就是 EOF 的行列位置。

[crates/rope/src/rope.rs:L397-L400](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L397-L400)——`offset_to_point` 的越界短路直接返回 `self.summary().lines`：摘要字段充当「无穷远处的坐标」，一处实际的下游消费。

区间摘要的三段式：

[crates/rope/src/rope.rs:L745-L775](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L745-L775)——`Cursor::summary::<D>(end_offset)`：L757–L762 累加起点块的内切片摘要（`D::from_chunk(start_chunk.slice(..))`），L764–L770 累加中间整块（`self.chunks.summary(&end_offset, Bias::Right)`，走树的前缀和）与终点块的内切片。泛型 `D: TextDimension` 允许调用方只要某一个维度（如 `usize`）而不必搬运整份摘要。

#### 4.4.4 代码实践

**实践目标**：验证「块 → 树 → 全文」链路与「直接扫描」给出完全相同的摘要，并体会区间摘要的三段式结构。

**操作步骤**：

1. 在 `mod tests` 加入（示例代码）：

```rust
#[test]
fn text_summary_tree_lab() {
    let text = "aaaa\nbbbb\ncccc\ndddd\neeee\nffff"; // 30 字节
    // 测试配置下 Bitmap = u16，MAX_BASE = 16，此文本必然跨多个块
    let rope = Rope::from(text);

    // 整根绳子的摘要 == 对全文的朴素扫描
    assert_eq!(rope.summary(), TextSummary::from(text));
    assert_eq!(rope.max_point(), rope.summary().lines);
    assert_eq!(rope.max_point(), Point::new(5, 4));

    // 任意区间的摘要 == 对切片的朴素扫描
    for (start, end) in [(0, 5), (3, 17), (5, 5), (0, text.len())] {
        assert_eq!(
            rope.cursor(start).summary::<TextSummary>(end),
            TextSummary::from(&text[start..end]),
            "range {start}..{end}"
        );
    }
}
```

2. 运行 `cargo test -p rope text_summary_tree_lab`。
3. 把文本换成 `"中文混排\n😀 emoji\n第三行"` 再跑一次，观察多字节内容下结论是否依旧成立。

**需要观察的现象**：所有断言通过；`cursor(3).summary(17)` 横跨块边界（区间 `3..17` 在测试配置下必然跨块），其结果仍与扫描 `&text[3..17]` 一致——中间整块的贡献全部来自树上缓存的摘要，没有任何逐字符计算。

**预期结果**：全部通过。第 5 章的综合实践会把这件事推广到随机文本。

**待本地验证**：请实际运行确认。

#### 4.4.5 小练习与答案

**练习 1**：`Rope::len()` 为什么可以不经过 `TextSummary` 直接返回 `chunks.extent(())`？

**答案**：`extent` 是 SumTree 摘要向某个维度的投影。`usize` 实现了 `Dimension for ChunkSummary`（从 `summary.text.len` 累加），所以树同样为 `usize` 维护了前缀和，`len()` 读的是这个更窄的缓存，避免复制整份九字段摘要。

**练习 2**：`rope.summary().lines` 与 `rope.max_point()` 是什么关系？

**答案**：恒等。`lines` 字段的语义就是「EOF 的行列位置」，`max_point()` 是同一份根摘要向 `Point` 维度的投影。`offset_to_point` 在 `offset >= len` 时直接返回 `summary().lines` 也印证了这一点。

**练习 3**：如果把每个 Chunk 的 `MAX_BASE` 从 128 改成 512，`Rope::summary()` 的结果会变吗？

**答案**：不会。`(TextSummary, +=)` 满足结合律，块切多大、树怎么折叠只影响性能与内存布局，不影响折出来的和。这正是 4.2 练习 3 提到的一般性原则的宏观体现——4.4.4 的测试在任何块大小下都应当通过。

## 5. 综合实践

**任务：实现一个「摘要合并模拟器」，亲手把「块 → 折叠 → 全文」的链路走一遍，并用随机化的文本对拍。**

要求：

1. 把任意文本按固定 7 字节（在字符边界处回退）切块，**模拟 rope 的分块**；
2. 逐块用 `TextSummary::from` 求摘要，再用 `+=` 折叠成整体；
3. 断言折叠结果与 `TextSummary::from(全文)` 完全相等；
4. 断言 `Rope::from(text).summary()` 也与之相等——真实 rope（树形折叠）与你的线性折叠给出同一个答案；
5. 覆盖多组刁钻文本：空串、纯换行、多字节混排、以换行结尾等。

参考实现（示例代码，加在 `crates/rope/src/rope.rs` 的 `mod tests` 内）：

```rust
#[test]
fn text_summary_fold_lab() {
    let texts = [
        "",
        "ab\ncd",
        "ef",
        "\n\n\n",
        "a\u{1F600}\nb",
        "ab\nefg\nhklm\nnopqrs\ntuvwxyz",
        "中文混排\n与 emoji \u{1F600} 共存\n第三行\n",
    ];

    for text in texts {
        let whole = TextSummary::from(text);

        // 1) 两段式：在每个字符边界处切一刀，验证任意切点的合并
        let mut boundaries = vec![0];
        boundaries.extend(text.char_indices().map(|(i, _)| i));
        boundaries.push(text.len());
        for (idx, &mid) in boundaries.iter().enumerate() {
            for &end in boundaries.iter().skip(idx + 1) {
                let mut folded = TextSummary::from(&text[..mid]);
                folded += &TextSummary::from(&text[mid..end]);
                folded += &TextSummary::from(&text[end..]);
                assert_eq!(folded, whole, "split at {mid}/{end} of {text:?}");
            }
        }

        // 2) 块式折叠：每 7 字节在字符边界切块后逐块相加
        let mut folded = TextSummary::default(); // 单位元起步
        let mut rest = text;
        while !rest.is_empty() {
            let mut cut = rest.len().min(7);
            while !rest.is_char_boundary(cut) {
                cut -= 1;
            }
            folded += &TextSummary::from(&rest[..cut]);
            rest = &rest[cut..];
        }
        assert_eq!(folded, whole, "chunked fold of {text:?}");

        // 3) 真实 rope 的树形折叠与线性折叠一致
        assert_eq!(Rope::from(text).summary(), whole, "rope of {text:?}");
    }
}
```

运行：`cargo test -p rope text_summary_fold_lab`。

**检查点**：

- 三段式合并覆盖了「空块」「换行落在切点」「多字节字符被切开边界回退」等所有坑，若 `AddAssign` 的任何一个字段规则理解有误，这里会立刻炸出不相等；
- 第 2 步从 `TextSummary::default()`（单位元）开始折叠，验证了 monoid 的「幺」；
- 第 3 步把你的手工折叠与 SumTree 的树形折叠对拍——**折叠顺序不同、结果相同**，这就是结合律的工程价值。

预期全部通过（**待本地验证**）。若你想进一步强化，可仿照 `crates/rope/src/rope.rs` 测试区的做法引入 `StdRng` 生成随机文本做上百轮对拍——这正好是 u3-l2（测试策略）的预告。

## 6. 本讲小结

- `TextSummary` 用九个字段固化一段文本的全部统计：数量（`len`/`chars`/`len_utf16`）、EOF 位置（`lines`）、首末行（`first_line_chars`/`last_line_chars`/`last_line_len_utf16`）、最长行（`longest_row`/`longest_row_chars`）。
- `From<&str>` 是摘要的朴素定义：O(n) 逐字符扫描；`longest_row` 采用严格 `>` 比较，平局时最早的行获胜。
- `AddAssign` 是合并代数：拼接行 \( S.last\_line\_chars + O.first\_line\_chars \) 参与最长行竞争；首行字段只在 `S` 单行时累加；末行字段在 `O` 单行时累加、否则被 `O` 覆盖；行号平移必须用合并前的 `S.lines.row`。
- `(TextSummary, +=)` 构成 monoid（结合律 + 空摘要单位元），这是 SumTree 能按任意树形折叠摘要、且 `Rope::summary()`/`len()`/`max_point()` 全部 O(1) 的数学根基。
- 块内摘要走 `ChunkSlice` 的位图 popcount 路径，与扫描路径共用同一套 tie-break 语义；区间摘要是「起点块切片 + 中间整块 + 终点块切片」的三段式。
- `newline()` 是 `"\n"` 的预制摘要；`add_newline` 存在两处与意图不符的疑点（`chars` 未自增、`len_utf16` 翻倍加一），且二者在 crate 内均无调用者——读 pub API 要保持怀疑。

## 7. 下一步学习建议

本讲搞定了「摘要是什么、怎么合并」。下一讲 **u2-l3（SumTree 集成）** 将钻进树本身：`sum_tree::Item` / `Summary` / `Dimension` 三个 trait 的分工、`find::<usize, _>((), &offset, Bias::Left)` 这类调用如何借助本讲的摘要做 O(log n) 定位，以及 `Dimensions` 元组如何让一次查找同时得到两种坐标。建议带着这个问题去读：**树节点除了缓存摘要，还要缓存什么才能让「按坐标找块」不用回溯？** 另外，`ChunkSlice` 的四张位图在 4.4.3 只露了一角，完整机制在 u2-l4 展开；若你对 `add_newline` 的疑点感兴趣，不妨先去 zed 仓库搜一下相关 issue，或直接提一个带复现测试的 issue——那就是 u3-l1（边界防御与错误策略）和 u3-l2（测试策略）的实战预演。
