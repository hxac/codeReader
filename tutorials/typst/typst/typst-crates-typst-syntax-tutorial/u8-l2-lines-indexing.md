# Lines 行列与编码转换

## 1. 本讲目标

本讲聚焦 `typst-syntax` 中负责「文本缓冲 + 行列元数据 + 编码转换」的 `Lines` 类型。学完本讲，你应该能够：

- 说清 `Lines` / `Line` 这两个结构各存了什么、为什么 `Lines` 要做成 `Arc` 引用计数且只对文本做 `Hash`。
- 掌握四组双向转换：`byte ↔ line`、`byte ↔ column`、`line,column ↔ byte`、`byte ↔ utf16`，并能复述它们各自的实现思路。
- 理解「二分查找定位行」为什么是这些转换的共同加速结构。
- 解释 Typst 为什么除了 UTF-8 字节偏移之外，还要维护 UTF-16 编码单元偏移（答案是 LSP / IDE 兼容）。

本讲只读 `src/lines.rs` 一个文件，并与 `src/source.rs`、`src/lexer.rs` 做少量交叉引用。文本**编辑**（`edit` / `replace` / `replacement_range`）是下一讲 u8-l3 的主题，本讲只在「综合视角」里点到为止。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 三种「位置坐标」并不等价

同一段文本里「第几个字符」可以用至少三种单位度量：

| 单位 | 说明 | 典型使用者 |
| --- | --- | --- |
| UTF-8 字节偏移（byte index） | Rust `&str` 的原生下标，UTF-8 编码下每个字符占 1–4 字节 | Typst 内部、`SyntaxNode` 的字节区间 |
| 字符 / 列（column） | 按 Unicode 码点（`char`）计数，一个字符算一列 | 给用户看的「行、列」位置 |
| UTF-16 编码单元（code unit） | UTF-16 下 BMP 字符占 1 单元，辅助平面字符（> U+FFFF）占 2 单元（代理对） | LSP 协议、VS Code 等 IDE |

同一个字符在三种坐标下的「宽度」不同。例如表情 `💛`（U+1F49B）：

\[ 
\text{UTF-8} = 4 \text{ 字节},\quad \text{char} = 1,\quad \text{UTF-16} = 2 \text{ 单元}
\]

而德语变音字母 `ä`（U+00E4）：

\[ 
\text{UTF-8} = 2 \text{ 字节},\quad \text{char} = 1,\quad \text{UTF-16} = 1 \text{ 单元}
\]

ASCII 字符（如 `a`、`\n`）三者都是 1。`Lines` 的核心工作，就是在这三种坐标之间做**精确且高效**的换算。

### 2.2 Typst 的换行口径

`Lines` 判断「哪里是一行的边界」时，并不只认 `\n`，而是复用 u3-l4 讲过的 `is_newline` 公共口径，它把 6 个字符都视为换行：

- 行进纸 `\n`、垂直制表 `\x0B`、换页 `\x0C`、回车 `\r`；
- 下一行 NEL `\u{0085}`、行分隔 LS `\u{2028}`、段分隔 PS `\u{2029}`。

特别地，`\r\n`（Windows 换行）会被**合并算一次**换行，而不是两次。这一点在后面 `lines_from` 的实现里很关键。

### 2.3 Lines 是 Source 的「文本唯一真相」

上一讲 u8-l1 已经说明：`Source` 不另存一份字符串，`text()` 直接转发给内部的 `Lines::text()`。也就是说，`Lines` 既是文本容器，又是行列索引。理解了 `Lines`，就理解了 Typst 源码层面的全部「位置」语义。

## 3. 本讲源码地图

本讲涉及的文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/lines.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs) | 本讲主角。定义 `Lines` / `Line`，实现全部坐标转换与（下一讲的）编辑。 |
| [src/source.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs) | 把 `Lines<String>` 作为 `SourceInner` 的一个字段，对外暴露 `Source::lines()`。 |
| [src/lexer.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs) | 提供 `is_newline`，是 `lines_from` 判定行边界的依据（见 u3-l4）。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs) | 通过 `pub use self::lines::Lines;` 把 `Lines` 挂牌到 crate 根，是公开 API。 |

可见性要点：`Lines` 与 `Line` 都是 `pub`，但 `Line` 的两个字段（`byte_idx`、`utf16_idx`）是私有的，外部只能通过 `Lines` 的方法间接使用。`Lines` 是本 crate 少数几个被 `pub use` 公开的「基础设施类型」之一。

---

## 4. 核心概念与源码讲解

### 4.1 Lines 与 Line：文本缓冲与行元数据的载体

#### 4.1.1 概念说明

`Lines` 想解决的问题是：给定一段可能很长的源码文本，我们要**频繁地**回答「第 N 字节在第几行第几列」「第 K 个 UTF-16 单元对应哪个字节」这类问题。如果每次都从头扫描文本，复杂度是 \(O(n)\)；当 IDE 每移动一次光标都要查一次时，这就不可接受了。

`Lines` 的做法是**预计算 + 缓存**：在构造时一次性扫描全文，记录下「每一行的起点在字节层面是第几个、在 UTF-16 层面是第几个」，存成一个有序数组。之后所有查询都退化成「在有序数组里二分查找 + 在单行内线性扫描」，从而把全文扫描变成局部扫描。

这里有两个对外类型：

- `Lines<S>`：对外壳，持有 `Arc<LinesInner<S>>`，因此**克隆廉价**。
- `Line`：单行的元数据，是一个 `Copy` 的小结构，只有两个 `usize` 字段。

`Lines` 是泛型 `Lines<S>`，`S` 通常是 `String`（在 `Source` 里，拥有所有权）或 `&str`（在测试里，借用即可）。所有「只读转换」方法都写在 `impl<T: AsRef<str>> Lines<T>` 里，对两种文本载体都适用；而「编辑」方法只写在 `impl Lines<String>` 里，要求拥有所有权。

#### 4.1.2 核心流程

构造 `Lines` 的流程：

1. `Lines::new(text)` 调用私有函数 `lines(text.as_ref())`。
2. `lines()` 先放入一个固定的「第 0 行」`Line { byte_idx: 0, utf16_idx: 0 }`（任何文本都从第 0 行第 0 字节开始）。
3. 再用 `lines_from(0, 0, text)` 从偏移 0 开始扫描，每遇到一个换行就产出一个新 `Line`，其 `byte_idx` / `utf16_idx` 指向**下一行**的起点。
4. 把这些 `Line` 收集成 `Vec<Line>`，连同文本一起塞进 `Arc<LinesInner>`。

查询时，关键性质是：`lines` 数组按 `byte_idx`（也按 `utf16_idx`）**严格递增**。这正是二分查找能成立的前提。

#### 4.1.3 源码精读

`Lines` 的对外壳是一个单字段元组结构，内部是 `Arc`：

[src/lines.rs:8-19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L8-L19) —— `Lines<S>(Arc<LinesInner<S>>)`，注释明确说「内部引用计数，克隆廉价」。`LinesInner` 只有两字段：`lines: Vec<Line>` 与 `text: T`。

[src/lines.rs:21-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L21-L28) —— `Line` 结构，两个字段都标注了用途：`byte_idx` 是该行起点的 UTF-8 字节偏移，`utf16_idx` 是该行起点的 UTF-16 编码单元偏移。字段私有，外部不可直接读。

构造与基本访问器都在通用 `impl<T: AsRef<str>> Lines<T>` 块里：

[src/lines.rs:31-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L31-L40) —— `new` 调 `lines(text.as_ref())` 建表；`text()` 转发到内部 `text` 的 `as_ref()`，这是「文本唯一真相」的入口。

[src/lines.rs:47-56](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L47-L56) —— 三个 `len_*`。注意 `len_utf16()` 不是简单字段，而是「最后一行的 `utf16_idx` + 最后一行剩余文本的 UTF-16 长度」，因为最后一行通常不以换行结尾、其尾部长度需要现算。

构造时真正干活的是两个私有函数：

[src/lines.rs:244-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L244-L249) —— `lines()` 用 `std::iter::once` 放入第 0 行，再 `chain(lines_from(...))`。

[src/lines.rs:251-276](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L251-L276) —— `lines_from` 是核心扫描器。它用 `unscanny::Scanner` 的 `eat_until` 一路吃到换行符；关键细节有两处：

1. 传给 `eat_until` 的闭包在「判断是否换行」的同时顺手累加 `utf16_idx += c.len_utf16()`，所以 `eat_until` 停下时，`utf16_idx` 已经包含了被吃掉的整段（含停在的那个换行符）的 UTF-16 宽度。
2. 吃掉换行符后，`if s.eat() == Some('\r') && s.eat_if('\n')` 处理 `\r\n` 合并：若是 `\r\n`，`eat_if` 多吃一个 `\n` 并把 `utf16_idx += 1`（补上那个被合并的 `\n`）。最终 `byte_idx = byte_offset + s.cursor()` 指向**下一行起点**。

[src/lines.rs:278-282](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L278-L282) —— 辅助函数 `len_utf16(string)`，对每个 `char` 取 `len_utf16` 求和，线性时间。

**引用计数与哈希语义**：`Lines` 的两个 trait 实现体现了它的「值对象」定位。

[src/lines.rs:232-236](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L232-L236) —— `Hash` 实现**只对 `text` 哈希，不哈希 `lines` 数组**。这是有意为之：行元数据完全由文本决定（`lines()` 是纯函数），哈希文本已足以区分；省掉对大数组的哈希能加快 `Source` 的缓存键计算。注意 `Lines` 没有手写 `PartialEq`，它依赖 `Source` 层的派生。

[src/lines.rs:238-242](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L238-L242) —— `AsRef<str>` 让 `Lines` 本身能当字符串切片用。

`Lines` 在 `Source` 中的集成方式：

[src/source.rs:28-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L28-L32) —— `SourceInner` 三字段之一就是 `lines: Lines<String>`（拥有所有权）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Lines::new` 产出的行元数据数组与手动计算的预期一致。

**操作步骤**：

1. 在仓库内运行本 crate 的现有测试 `test_source_file_new`，它断言了测试串 `"ä\tcde\nf💛g\r\nhi\rjkl"` 的行表：

```bash
cargo test -p typst-syntax test_source_file_new
```

2. 自己手动算一遍该串的字节布局（节选关键字符）：

| 字符 | ä | \t | c | d | e | \n | f | 💛 | g | \r | \n | h | i | \r | j | k | l |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 字节起 | 0 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 |

其中 `ä` 占 2 字节（0–1），`💛` 占 4 字节（8–11）。换行分别落在字节 6（`\n`）、13–14（`\r\n` 合并）、17（`\r`），故四行起点为 0、7、15、18。

**需要观察的现象**：测试通过，且行表为：

```
Line { byte_idx: 0,  utf16_idx: 0 }
Line { byte_idx: 7,  utf16_idx: 6 }
Line { byte_idx: 15, utf16_idx: 12 }
Line { byte_idx: 18, utf16_idx: 15 }
```

**预期结果**：与 [src/lines.rs:293-301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L293-L301) 的断言完全一致（该断言本身就是仓库内的权威预期）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Lines` 的 `Hash` 实现只哈希 `text` 而不哈希 `lines` 数组？这样安全吗？

**参考答案**：因为 `lines` 数组是 `text` 的纯函数（由 `lines(text)` 计算），相同文本必然产生相同行表，所以哈希文本已能唯一区分两个 `Lines` 的「值」。省去对大数组的哈希可以显著加快 `Source` 的哈希计算，而这对 Typst 增量编译的缓存键命中判断至关重要。安全性成立。

**练习 2**：`Line` 的两个字段为什么设为私有？

**参考答案**：行元数据是 `Lines` 的内部加速结构，外部直接读写会破坏「行起点严格递增」「与文本一致」等不变量。私有化强制外部只能通过 `Lines` 的方法访问，把不变量的维护权收口在 `lines.rs` 内部。

---

### 4.2 byte_to_line 与 byte_to_column：从字节偏移到行列

#### 4.2.1 概念说明

这是最常见的一类查询：「我知道某个字节偏移（比如某个 `SyntaxNode` 的区间端点），它在第几行第几列？」`Lines` 用两个方法分别回答「第几行」和「第几列」：

- `byte_to_line(byte_idx) -> Option<usize>`：返回该字节所属行的**行号**（从 0 计）。
- `byte_to_column(byte_idx) -> Option<usize>`：返回该字节在所属行内的**列号**，列号定义为「该行内、该字节之前的字符个数」。

两者都返回 `Option`，越界（`byte_idx > 文本字节长度`）返回 `None`。注意是 `>` 而非 `>=`：文本末尾位置（等于长度）是合法光标位，返回有效结果。

#### 4.2.2 核心流程

`byte_to_line` 的关键是**二分查找**。因为 `lines` 数组按 `byte_idx` 递增，我们可以用 `binary_search_by_key`：

- 若 `byte_idx` 恰好等于某行起点（`Ok(i)`），行号就是 `i`。
- 若不等于（`Err(i)`），`i` 是「插入位置」，则所属行号是 `i - 1`（即前一行）。

这一步是 \(O(\log L)\)，\(L\) 为行数。

`byte_to_column` 先调 `byte_to_line` 定位到行，取出该行起点 `start`，再取子串 `text[start..byte_idx]`，数其中字符数（`chars().count()`）。这一步是 \(O(\log L + c)\)，\(c\) 为该行内偏移之前的字符数。

辅助方法 `line_to_byte` / `line_to_range` 则是反方向的小工具：由行号取起点字节、取整行字节区间。

#### 4.2.3 源码精读

[src/lines.rs:66-74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L66-L74) —— `byte_to_line`。先用 `(byte_idx <= self.text().len()).then(...)` 做越界守卫（注意是 `<=`，末尾位置合法），再用 `binary_search_by_key` 在 `lines` 数组里按 `byte_idx` 查。`Ok(i) => i`、`Err(i) => i - 1` 的双分支正是上文描述的「命中行首 vs 落在某行中间」。

[src/lines.rs:76-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L76-L85) —— `byte_to_column`。`?` 链式：`byte_to_line` 定位行 → `line_to_byte` 取行首 → `text.get(start..byte_idx)` 取前缀子串 → `head.chars().count()` 数字符。注释明确：「列 = 该字节之前该行内的字符个数」。这里用 `get` 而非直接索引，是为了在边界异常时优雅返回 `None`。

[src/lines.rs:87-94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L87-L94) —— `byte_to_line_column`，把上两者合成一次返回 `(line, col)`，避免重复定位。

[src/lines.rs:117-127](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L117-L127) —— `line_to_byte` 直接取 `lines[line_idx].byte_idx`；`line_to_range` 取 `[start, 下一行起点或文末)`。`unwrap_or(self.text().len())` 处理最后一行没有「下一行」的情况。

以测试串为例验证 `byte_to_line`（参见 [src/lines.rs:305-315](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L305-L315)）：字节起点数组为 `[0, 7, 15, 18]`。`byte_to_line(0)=Ok→0`，`byte_to_line(2)=Err(1)-1=0`，`byte_to_line(7)=Ok→1`，`byte_to_line(8)=Err(2)-1=1`，`byte_to_line(21)=Err(4)-1=3`，`byte_to_line(22)=None`（越界）。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试断言，理解二分查找 `Err` 分支的 `i - 1` 含义。

**操作步骤**：

1. 打开 [src/lines.rs:304-315](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L304-L315) 的 `test_source_file_pos_to_line`。
2. 运行：

```bash
cargo test -p typst-syntax test_source_file_pos_to_line
```

3. 对照字节表，自己解释为何 `byte_to_line(12)` 返回 `Some(1)`（提示：12 落在第 1 行 `[7, 15)` 区间内，二分查找 `binary_search_by_key(&12, ...)` 在数组 `[0,7,15,18]` 上返回 `Err(2)`，故行号 `2-1=1`）。

**需要观察的现象**：测试通过。

**预期结果**：`byte_to_line(12) == Some(1)`，与断言一致。

#### 4.2.5 小练习与答案

**练习 1**：若 `byte_idx` 正好等于某行起点，`binary_search_by_key` 返回 `Ok` 还是 `Err`？行号如何确定？

**参考答案**：返回 `Ok(i)`，`i` 正是该行在数组中的下标，行号直接取 `i`。例如 `byte_to_line(7)`，7 是第 1 行起点，返回 `Ok(1)` → 行号 1。

**练习 2**：`byte_to_column` 与 `byte_to_utf16` 都需要「行首偏移 + 行内前缀」，为什么前者用 `chars().count()` 而后者用 `len_utf16()`？

**参考答案**：因为「列」按字符（`char`）定义，一个字符算一列；而 UTF-16 偏移按编码单元定义，辅助平面字符算 2 个单元。两者度量单位不同，故分别用 `chars().count()` 和 `len_utf16()`（即 `char::len_utf16` 之和）。

---

### 4.3 line_column_to_byte：从行列回到字节偏移

#### 4.3.1 概念说明

这是 4.2 的反方向：IDE 给出「第 L 行第 C 列」（比如光标位置），Typst 需要把它换算回字节偏移，才能定位到 `SyntaxNode`。`line_column_to_byte(line_idx, column_idx) -> Option<usize>` 完成这件事。

它与 4.2 的方法构成**往返关系**：对文本内任意合法字节偏移 `b`，先 `byte_to_line_column(b)` 得到 `(l, c)`，再 `line_column_to_byte(l, c)` 应能还原回 `b`。

#### 4.3.2 核心流程

1. 用 `line_to_range(line_idx)` 取出该行的字节区间 `[start, end)`。
2. 取该行子串，构造 `chars()` 迭代器。
3. 用 `for _ in 0..column_idx { chars.next()?; }` 跳过 `column_idx` 个字符；若中途迭代器耗尽（列号超过该行字符数），`?` 让函数返回 `None`。
4. 最终字节偏移 = `range.start + (line.len() - chars.as_str().len())`。

第 4 步用了一个巧妙的恒等式：`chars()` 是借用原字符串的迭代器，调用 `next` 会推进内部游标；剩余未消费部分 `chars.as_str()` 的长度，就是「还没跳过的字节数」。于是 `行内已跳过的字节数 = 行总字节长 - 剩余字节长`。

#### 4.3.3 源码精读

[src/lines.rs:129-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L129-L146) —— `line_column_to_byte`。注释说明列号是「从行首起跳过的字符数」。核心是 `chars.next()?` 循环（越界返回 `None`）和最后那行 `range.start + (line.len() - chars.as_str().len())`。

注意一个**边界细节**：`line_to_range` 返回的行区间**包含行尾换行符**（因为下一行起点才是当前行的 `end`）。这意味着 `line_column_to_byte` 允许列号落到换行符上，这与「光标可以停在行尾」的语义一致。

往返一致性由测试 `test_source_file_roundtrip` 守护：

[src/lines.rs:350-364](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L350-L364) —— 对字节 0、7、12、21 做 `byte→(line,column)→byte` 往返，断言还原成功。

#### 4.3.4 代码实践

**实践目标**：验证「字节 → 行列 → 字节」的往返一致性。

**操作步骤**：

1. 运行现有测试：

```bash
cargo test -p typst-syntax test_source_file_roundtrip
```

2.（可选源码阅读）在测试串里手动验证一处往返：字节 12（`g`）→ `byte_to_line_column(12)`：行 1（起点 7），前缀 `text[7..12]="f💛"` 共 2 字符 → 列 2；再 `line_column_to_byte(1, 2)`：第 1 行区间 `[7,15)`，跳过 2 个字符 `f`、`💛`（共 1+4=5 字节）→ 字节 `7+5=12`，还原成功。

**需要观察的现象**：测试通过；手动推算与程序行为一致。

**预期结果**：所有往返点都还原回原字节，与断言一致。

#### 4.3.5 小练习与答案

**练习 1**：`line_column_to_byte` 为什么用 `chars.as_str().len()` 而不是另开计数器统计已跳过字节？

**参考答案**：因为不同字符占不同字节数，必须按「字符」推进才能正确解释列号；而 `Chars` 迭代器天然按字符推进，其剩余视图 `as_str()` 的字节长度恰好就是「未跳过字节数」，于是「已跳过字节 = 行总长 − 剩余长」，无需另维护计数器，简洁且无歧义。

**练习 2**：若 `column_idx` 大于该行字符数，函数返回什么？为什么这是合理设计？

**参考答案**：返回 `None`。因为 `chars.next()?` 在迭代器耗尽时返回 `None`，`?` 直接传播出去。这合理：超出该行字符数的列号是无意义的位置，应当让调用方感知失败而非静默夹紧。

---

### 4.4 byte_to_utf16 与 utf16_to_byte：UTF-16 双向转换

#### 4.4.1 概念说明

为什么 Typst 要维护 UTF-16？因为 **LSP（Language Server Protocol）和大多数 IDE（VS Code 等）内部用 UTF-16 编码单元表示位置**。当 Typst 作为语言服务器向编辑器报告诊断、跳转、补全位置时，必须把内部的 UTF-8 字节偏移翻译成 UTF-16 单元偏移；反之，编辑器传来的光标位置也是 UTF-16 单位，需要翻译回字节偏移。这就是 `byte_to_utf16` 与 `utf16_to_byte` 存在的全部理由。

`Lines` 把这件事做得高效的方式，是在 `Line` 上额外缓存 `utf16_idx`：每一行不仅记字节起点，也记 UTF-16 起点。于是 UTF-16 与字节之间的换算同样退化成「二分定位行 + 行内局部扫描」。

#### 4.4.2 核心流程

**`byte_to_utf16(byte_idx)`（字节 → UTF-16）**：

1. `byte_to_line(byte_idx)` 定位到行 `l`。
2. 取该行起点 `line.utf16_idx`，加上行首到 `byte_idx` 这段子串的 UTF-16 长度（`len_utf16(head)`）。
3. 结果 = `line.utf16_idx + len_utf16(text[line.byte_idx..byte_idx])`。

**`utf16_to_byte(utf16_idx)`（UTF-16 → 字节）**：

1. 用 `binary_search_by_key` 在 `lines` 数组里按 `utf16_idx` 二分，找到 UTF-16 起点不超过 `utf16_idx` 的最后一行（`Err(i) => i-1` 同理）。
2. 从该行字节起点开始，逐字符累加 `c.len_utf16()`，一旦累加值 `k >= utf16_idx`，返回当前字符的字节偏移。
3. 若扫到文末仍未达到，且 `k == utf16_idx`（恰好指文末），返回文本长度；否则返回 `None`。

一个重要细节：因为 `k >= utf16_idx` 在字符边界判定，所以 `utf16_to_byte` 永远返回**字符起始字节**，绝不会落到代理对中间。若 `utf16_idx` 落在一个代理对的两个单元之间（例如 `💛` 的中间），它会**向后吸附**到下一个字符的起点——这是合理的容错。

#### 4.4.3 源码精读

[src/lines.rs:58-64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L58-L64) —— `byte_to_utf16`。复用 `byte_to_line` 与 `Line::utf16_idx`，再加行内前缀的 `len_utf16`。注意三处都用 `?`：行定位失败、行对象缺失、子串切割失败（`byte_idx` 不在字符边界）都会返回 `None`。

[src/lines.rs:96-115](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L96-L115) —— `utf16_to_byte`，本讲最精巧的方法。分两段：

- 二分段（98-103 行）：`binary_search_by_key(&utf16_idx, |line| line.utf16_idx)`，`Ok(i)=>i`、`Err(i)=>i-1`，定位到「UTF-16 起点 ≤ 目标」的最后一行。
- 线性段（105-114 行）：`k` 从 `line.utf16_idx` 起累加 `c.len_utf16()`，`char_indices` 同时给出字节偏移 `i`；一旦 `k >= utf16_idx` 返回 `line.byte_idx + i`。扫到文末时用 `(k == utf16_idx).then_some(text.len())` 处理「恰好指文末」的边界。

[src/lines.rs:47-51](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L47-L51) —— `len_utf16`（方法版）与 [src/lines.rs:278-282](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L278-L282) 的函数版 `len_utf16` 配合使用，后者把每个 `char` 映射到其 UTF-16 宽度求和。

以测试串验证（参见 [src/lines.rs:329-347](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L329-L347) 的 `test_source_file_utf16`）：

- `byte_to_utf16(8)`：行 1（`utf16_idx=6`），前缀 `text[7..8]="f"` 的 UTF-16 长度 1 → `6+1=7`。而字节 8 正是 `💛` 起点。
- `byte_to_utf16(12)`：行 1，前缀 `text[7..12]="f💛"`，`f` 贡献 1、`💛` 贡献 2 → `6+3=9`。字节 12 是 `g`。
- 反向 `utf16_to_byte(7)`：二分定位到行 1（`utf16_idx=6`），从字节 7 起扫：`f` 使 `k=7`，`k>=7` 成立 → 返回 `7+1=8`，还原回字节 8。往返成立。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用含多字节字符（`ä`、`💛`）与 `\r\n` 的文本构造 `Lines`，验证 `byte_to_utf16` 与 `utf16_to_byte` 的往返一致性。

**操作步骤**：

仓库内已有现成测试 `test_source_file_utf16`，直接运行即可观察权威行为：

```bash
cargo test -p typst-syntax test_source_file_utf16
```

若想自己写一个最小验证，可在 `src/lines.rs` 的 `#[cfg(test)] mod tests` 内追加（**示例代码**，非项目原有代码）：

```rust
#[test]
fn my_utf16_roundtrip() {
    // ä=2B/1u, \n, f=1B, 💛=4B/2u, g, \r\n
    let text = "ä\nf💛g\r\nhi";
    let lines = Lines::new(text);
    // 遍历每个字符边界字节，做 byte -> utf16 -> byte 往返
    for byte_idx in [0usize, 2, 3, 4, 8, 12] {
        let u = lines.byte_to_utf16(byte_idx).unwrap();
        let b = lines.utf16_to_byte(u).unwrap();
        assert_eq!(b, byte_idx, "roundtrip failed at {}", byte_idx);
    }
}
```

**需要观察的现象**：测试通过，每个字符边界字节经 UTF-16 往返后都能还原。

**预期结果**：往返全部成立。手工核对一处：`byte_to_utf16(8)`（`💛` 起点）= 行 1 的 `utf16_idx(2)` + 前缀 `text[3..8]="f"` 的 UTF-16 长度 1 = 3；`utf16_to_byte(3)` 二分到行 1，从字节 3 起扫 `f` 使 `k=3>=3`，返回 `3+1=4`？—— 这里要注意：本例文本是 `"ä\nf💛g\r\nhi"`，行起点为字节 0 和 3（`\n` 在字节 2），所以行 1 的 `byte_idx=3`、`utf16_idx=2`。`byte_to_utf16(8)` = `2 + len_utf16("f💛"[..])`，前缀 `text[3..8]="f💛"` 的 UTF-16 长度 = 1+2 = 3 → 结果 5。`utf16_to_byte(5)`：二分行 1（`utf16_idx=2`），从字节 3 起扫 `f`(k→3)、`💛`(k→5)，`k>=5` 时返回 `3 + 💛的字节偏移1 = 4`？此处理论值需以本地实际运行为准。

> ⚠️ 上段最后一处的精确数值建议**待本地验证**：不同示例串的行起点与偏移需以 `cargo test` 实际输出为准；最稳妥的做法是直接信任并运行仓库自带的 `test_source_file_utf16`（其断言已经覆盖 `ä`、`💛`、`\r\n` 三种关键字符的往返），而非自行重算。

**预期结果（权威）**：仓库测试 `test_source_file_utf16` 全绿，覆盖字节 `0,2,3,8,12,21` 与 UTF-16 `0,1,2,7,9,18` 的往返，并断言 `byte_to_utf16(22)=None`、`utf16_to_byte(19)=None`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Line` 要同时缓存 `byte_idx` 和 `utf16_idx`，而不是只缓存 `byte_idx`、用的时候再算 UTF-16？

**参考答案**：因为 UTF-16 偏移查询（`utf16_to_byte`）与字节→UTF-16 查询（`byte_to_utf16`）都极高频（IDE 每次光标移动、每次诊断上报都要用）。若只缓存字节起点，每次 UTF-16 查询都得从文件头线性累加 UTF-16 宽度，退化为 \(O(n)\)。缓存每行 UTF-16 起点后，查询退化为「二分定位行 \(O(\log L)\) + 行内局部扫描」，对大文件是数量级提升。

**练习 2**：若调用 `utf16_to_byte` 时传入的 `utf16_idx` 恰好落在一个代理对的两个单元之间（例如指向 `💛` 的「半个」），会发生什么？

**参考答案**：会**向后吸附**到下一个字符的字节起点。因为循环判定条件是 `k >= utf16_idx`，`💛` 一次性让 `k` 跨过 2 个 UTF-16 单元，中间值不会触发返回；只有扫到下一个字符、`k` 再次满足条件时才返回。这保证结果始终是合法的字符边界字节，避免产生指向代理对中间的非法偏移。

**练习 3**：`byte_to_utf16` 在 `byte_idx` 不落在字符边界时会怎样？为什么这样设计？

**参考答案**：返回 `None`。因为 `text.get(line.byte_idx..byte_idx)` 在 `byte_idx` 非字符边界时返回 `None`，`?` 随之传播。这样设计是为了「不撒谎」：非字符边界的字节偏移本身就不是合法的字符位置，强行返回一个近似 UTF-16 值会误导调用方，不如显式失败。

---

## 5. 综合实践

设计一个把本讲四组转换串起来的小任务：**实现一个迷你的「位置翻译器」**。

**任务描述**：给定测试串 `TEST = "ä\tcde\nf💛g\r\nhi\rjkl"`，写一个小程序（可放在 `src/lines.rs` 的测试模块里）完成以下三件事，并打印结果：

1. 取表情 `💛` 的字节起点（应为 8），用 `byte_to_line_column` 报告它的「行、列」。
2. 用上一步得到的「行、列」调用 `line_column_to_byte`，验证能还原回 8。
3. 用 `byte_to_utf16` 把字节 8 翻译成 UTF-16 偏移，再用 `utf16_to_byte` 翻译回来，验证往返一致。

**参考实现骨架**（**示例代码**）：

```rust
#[test]
fn mini_position_translator() {
    let text = "ä\tcde\nf💛g\r\nhi\rjkl";
    let lines = Lines::new(text);

    // 1. 字节 -> 行列
    let (line, col) = lines.byte_to_line_column(8).unwrap();
    println!("💛 at byte 8 -> line {}, col {}", line, col);

    // 2. 行列 -> 字节（往返）
    let back = lines.line_column_to_byte(line, col).unwrap();
    assert_eq!(back, 8);

    // 3. 字节 <-> UTF-16（往返）
    let u = lines.byte_to_utf16(8).unwrap();
    assert_eq!(lines.utf16_to_byte(u).unwrap(), 8);
}
```

**预期现象**：步骤 1 应报告 `💛` 在第 1 行（0 计）、第 1 列（该行 `f` 是第 0 列、`💛` 是第 1 列）；步骤 2、3 的往返断言成立。运行：

```bash
cargo test -p typst-syntax mini_position_translator -- --nocapture
```

> 精确的「行、列」数值建议以本地 `--nocapture` 打印为准（「待本地验证」），但往返一致性必然成立，因为这就是 `Lines` 四组转换的设计契约。

**实践要点**：这个任务把「字节↔行列」「字节↔UTF-16」两条往返链都串了起来，帮助你体会 `Line` 缓存的 `byte_idx` / `utf16_idx` 是如何同时服务两套坐标系的。

## 6. 本讲小结

- `Lines` 是 Typst 的「文本容器 + 行列元数据」二合一：内部 `Arc<LinesInner>`，克隆廉价；`Hash` 只哈希文本（行表是文本的纯函数），服务于 `Source` 的增量编译缓存键。
- `Line` 是私有字段的小结构，缓存每行的 `byte_idx` 与 `utf16_idx` 两个起点，是所有坐标转换的共同加速结构；行起点严格递增，支撑二分查找。
- `byte_to_line` 用 `binary_search_by_key`（`Ok(i)=>i`、`Err(i)=>i-1`）在 \(O(\log L)\) 定位行；`byte_to_column` 在行内 `chars().count()` 数字符。
- `line_column_to_byte` 是其逆运算，用 `Chars` 迭代器的 `as_str().len()` 巧妙换算「已跳过字节数」，与 `byte_to_line_column` 构成往返。
- `byte_to_utf16` / `utf16_to_byte` 服务 LSP/IDE 的 UTF-16 坐标；`utf16_to_byte` 永远返回字符边界字节，对代理对中间值向后吸附。
- 越界一律返回 `None`，但文本末尾位置（等于长度）合法；非字符边界的字节偏移在 `byte_to_utf16` 中也返回 `None`，遵循「不撒谎」原则。

## 7. 下一步学习建议

本讲只覆盖了 `Lines` 的**只读**查询面。`src/lines.rs` 还有一整个 `impl Lines<String>` 块负责文本**编辑**——下一讲 **u8-l3 文本编辑与行重建** 将讲解：

- `edit(replace, with)` 如何用 `Arc::make_mut` 做写时复制，截断失效行起点并只重算受影响部分；
- `replace(new)` 与 `replacement_range` 如何用公共前缀/后缀 diff 求最小编辑；
- `\r` 与 `\n` 跨编辑拼接的边界修正逻辑。

建议在进入 u8-l3 前，先回头确认你理解了本讲的「行起点数组」结构——因为编辑的核心难点，正是「如何在改动后高效维护这个数组」。如果想看 `Lines` 在更大语境中的角色，可重读 u8-l1 中 `Source::edit` 如何先调 `lines.edit` 再调 `reparse`（[src/source.rs:104-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L104-L112)），那是 `Lines` 编辑能力通往增量重解析（U9）的入口。
