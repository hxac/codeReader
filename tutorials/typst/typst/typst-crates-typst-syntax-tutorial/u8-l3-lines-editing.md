# 文本编辑与行重建

## 1. 本讲目标

本讲承接 u8-l2（`Lines` 的行列与编码转换），解决一个新问题：**当文本被就地编辑后，`Lines` 如何高效地更新它的行起点表？**

学完后你应当能够：

- 说清 `Source::edit` 与 `Lines::edit` 的分工：前者管「文本+行表」与「语法树」两件事，后者只管前一件。
- 解释 `replacement_range` 如何用「公共前缀 + 公共后缀」求出最小单次编辑，并理解为何要做 UTF-8 字符边界对齐。
- 复述 `Lines::edit` 的增量策略：保留编辑起点所在行及其以上的行起点，丢弃之后的，再从起点重算。
- 理解 `Arc::make_mut` 在这里的写时复制（copy-on-write）作用。
- 说清 `\r` 与 `\n` 跨编辑边界拼接成 `\r\n` 时，为什么要额外 `pop` 掉一个行起点。

---

## 2. 前置知识

在进入编辑逻辑前，先用三段话复习必要背景。

**什么是行起点表？** `Lines` 内部维护一个 `Vec<Line>`，每个 `Line` 缓存某一行的 UTF-8 字节起点 `byte_idx` 与 UTF-16 起点 `utf16_idx`（见 u8-l2）。所有 `byte_to_line` / `byte_to_column` / `utf16_to_byte` 等查询都依赖这张表，并用二分查找定位行。所以「编辑后」的核心任务就是把这张表改对。

**写时复制（copy-on-write）。** `Lines` 内部是 `Arc<LinesInner>`（[lines.rs:8-12](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L8-L12)），克隆只增加引用计数、几乎免费。但当我们要改文本时，必须先拿到独占的可变引用。`Arc::make_mut(&mut self.0)` 的契约是：若引用计数为 1 则原地返回可变引用（零成本）；否则先深拷一份再返回。这正是 `Source` 增量编译里「旧版本仍被缓存引用、新版本要改就只能复制」的典型场景。

**Typst 的换行口径。** 本 crate 用统一的 `is_newline` 判定换行（[lines.rs:6](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L6)），它把 `\n`、`\r`、`\u{0085}`、`\u{2028}`、`\u{2029}` 等都视为换行，并把 `\r\n` 合并算作**一次**换行。这一点在编辑时尤为关键，是本讲第 4 个模块的焦点。

---

## 3. 本讲源码地图

本讲只涉及一个源文件，但它与 `source.rs` 的编辑入口紧密相连：

| 文件 | 作用 |
| --- | --- |
| [src/lines.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs) | 文本容器 + 行列表，本讲主角：`edit` / `replace` / `replacement_range` / `lines_from` 全在此 |
| [src/source.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs) | `Source::edit` / `Source::replace`，先调 `Lines::edit` 改文本与行表，再调 `reparse` 改语法树 |

注意一个可见性细节：`edit` / `replace` / `replacement_range` 只定义在 `impl Lines<String>` 上（[lines.rs:149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L149)），因为修改文本要求持有所有权；而只读查询定义在 `impl Lines<T: AsRef<str>>` 上，对 `&str` 也能用（测试里常见）。

---

## 4. 核心概念与源码讲解

### 4.1 编辑全景：从 Source::edit 到 Lines::edit

#### 4.1.1 概念说明

Typst 的增量编译以「一次小编辑」为单位：用户在编辑器里改了几个字，外部世界告诉 `Source`「把 `replace` 这个字节范围换成 `with`」。`Source::edit` 做两件相互独立的事：

1. **改文本与行表**——交给 `Lines::edit`；
2. **改语法树**——交给 `reparse`（增量重解析，见 U9）。

本讲只关心第 1 件。把它单独抽到 `Lines` 里，是因为「文本+行表」是一个自洽的子系统：只要给定 `(replace, with)`，行表就能在不碰语法树的情况下被正确更新。

#### 4.1.2 核心流程

```
Source::edit(replace, with)
  ├─ inner.lines.edit(replace, with)      // 本讲范围：改文本 + 改行表
  └─ reparse(&mut inner.root, text, replace, with.len())  // U9：改树
```

`Lines::edit` 内部遵循一条朴素的增量原则：**编辑点之前的行起点一定没变，编辑点之后的行起点需要重算。** 具体步骤见 4.3，这里先看 `Source` 层的对接代码。

#### 4.1.3 源码精读

`Source::edit` 把两件事串起来（[source.rs:104-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L104-L112)）：

```rust
pub fn edit(&mut self, replace: Range<usize>, with: &str) -> Range<usize> {
    let inner = &mut **Arc::make_mut(&mut self.0);
    // 改文本与行表。
    inner.lines.edit(replace.clone(), with);
    // 增量重解析被替换的范围。
    reparse(&mut inner.root, inner.lines.text(), replace, with.len())
}
```

注意 `Source` 自己也用了一次 `Arc::make_mut` 来拿可变的 `SourceInner`；随后 `Lines::edit` 内部还会再来一次（对更内层的 `Arc<LinesInner>`）。两次写时复制互不干扰，因为它们作用在不同层级。

`Source::replace` 则是「给一整段新文本」的便捷入口：它先用 `replacement_range` 算出最小编辑，再转成一次 `edit`（[source.rs:85-96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L85-L96)）。

#### 4.1.4 代码实践

**实践目标**：观察一次 `edit` 之后，`Lines` 的行数与各行起点的变化。

操作步骤（用公开 API，无需改源码）：

1. 在仓库外建一个小 crate，依赖 `typst-syntax`（或直接在 `lines.rs` 的 `#[cfg(test)]` 模块里临时加测试）。
2. 构造 `Lines::new("ab\ncd".to_string())`，打印 `len_lines()` 与每行的 `line_to_byte(i)`。
3. 调用 `edit(2..3, "X")`（把第一个 `\n` 之前的 `b` 换成 `X`，注意这只是改字符不动换行）。
4. 再次打印行数与行起点。

预期结果：编辑只动了字符、没动换行，行数不变，行起点也不变；文本变为 `"aX\ncd"`。若你把 `edit` 改成 `edit(1..2, "\n")`（插入一个换行），则行数应 +1，并在新换行处多出一个行起点。

> 待本地验证：具体打印值请自行运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `edit` 只定义在 `Lines<String>` 上，而不在 `Lines<T: AsRef<str>>` 上？

**参考答案**：`edit` 要就地改写文本（`inner.text.replace_range(...)`），这要求文本类型本身可变，即 `String`。只读查询只需要 `as_ref()` 拿到 `&str`，所以放在更宽的 `AsRef<str>` 上。

**练习 2**：`Source::edit` 里出现了两次 `Arc::make_mut` 吗？分别在哪一层？

**参考答案**：一次在 `Source::edit`（对 `Arc<SourceInner>`，见 [source.rs:105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L105)），一次在 `Lines::edit`（对更内层的 `Arc<LinesInner>`，见 [lines.rs:210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L210)）。前者让 `SourceInner` 可变，后者让 `LinesInner` 可变。

---

### 4.2 replacement_range：最小编辑与字符边界对齐

#### 4.2.1 概念说明

`Source::replace(new)` 接收的是「一整段新文本」，但内部只想做一次最小范围的 `edit`。比如把 `"hello world"` 改成 `"hello worlds"`，其实只是在末尾插入一个 `s`，没必要重写整段。

`replacement_range` 就是这个「最小化」算法：它找出旧文本与新文本的**公共前缀**与**公共后缀**，把中间不同的那一截当作唯一要替换的区域。设

\[ \text{old} = P\, C_{\text{old}}\, S, \qquad \text{new} = P\, C_{\text{new}}\, S \]

其中 \(P\) 是公共前缀、\(S\) 是公共后缀，那么把 `old` 变成 `new` 等价于单次替换：

\[ \text{old}\big[\,|P|\,..\,|\text{old}|-|S|\,\big] \;\longrightarrow\; \text{new}\big[\,|P|\,..\,|\text{new}|-|S|\,\big] \]

#### 4.2.2 核心流程

1. **逐字节**数公共前缀长度 `prefix`。
2. 若两串完全相等，返回 `None`（`replace` 据此判定「无变化」）。
3. **把 `prefix` 回退到字符边界**：字节级 diff 可能停在某个多字节 UTF-8 字符的中间，必须回退到一个完整的字符边界。
4. 在去掉前缀的剩余部分，从末尾**逐字节**数公共后缀 `suffix`。
5. **把 `suffix` 推进到字符边界**（理由同上，但方向相反）。
6. 返回 `Some((prefix, suffix))`。

第 3、5 步是这段代码的精髓，下一节展开。

#### 4.2.3 源码精读

完整函数在 [lines.rs:172-197](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L172-L197)。关键片段：

前缀 diff 与「相等则无变化」判定（[lines.rs:175-180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L175-L180)）：

```rust
let mut prefix =
    zip(old.bytes(), new.bytes()).take_while(|(x, y)| x == y).count();
if prefix == old.len() && prefix == new.len() {
    return None;
}
```

前缀回退到字符边界（[lines.rs:182-184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L182-L184)）：

```rust
while !old.is_char_boundary(prefix) || !new.is_char_boundary(prefix) {
    prefix -= 1;
}
```

后缀 diff 与后缀推进到字符边界（[lines.rs:186-194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L186-L194)）：

```rust
let mut suffix = zip(old[prefix..].bytes().rev(), new[prefix..].bytes().rev())
    .take_while(|(x, y)| x == y)
    .count();
while !old.is_char_boundary(old.len() - suffix)
    || !new.is_char_boundary(new.len() - suffix)
{
    suffix += 1;
}
```

**为什么要做字符边界对齐？** 举个具体例子：旧文本里有 `ä`（UTF-8 两字节 `0xC3 0xA4`），新文本同一位置是 `å`（`0xC3 0xA5`）。逐字节比较时，第一字节 `0xC3` 相等，于是 `prefix` 停在 1——但这恰好是 `ä` / `å` 的中间字节，不是字符边界。若不修正，替换范围就会从一个字符的半截开始，产生非法的 UTF-8 切片。

修正方向有讲究：

- **前缀**用 `prefix -= 1` **回退**：把整个被劈开的字符**纳入**替换区（前缀变短，替换区向左扩）。因为前缀里那些字节虽然相等，但把一个完整字符划进替换区只是「多替换一点相同内容」，结果正确。
- **后缀**用 `suffix += 1` **前进**：同样把被劈开的字符**纳入**替换区（后缀变长，替换区从右侧收缩）。注意后缀的边界判据是 `old.len() - suffix`，增大 `suffix` 让这个下标左移到字符边界。

两者方向不同但目标一致：**宁可让替换区稍微大一点，也绝不在字符中间切开。**

#### 4.2.4 代码实践

**实践目标**：亲手验证 `replacement_range` 在多字节字符上的字符边界对齐。

操作步骤：

1. 构造 `Lines::new("äa".to_string())`（文本为 `ä` + `a`，4 字节）。
2. 对新串 `"åa"`（把 `ä` 换成 `å`）调用 `replacement_range`，打印 `(prefix, suffix)`。
3. 思考：`prefix` 是否为 0？为什么不是 1？

预期结果：`prefix == 0`、`suffix == 1`（公共后缀是末尾的 `a`）。原始字节级 prefix 是 1（首字节 `0xC3` 相等），但因字节 1 不是字符边界，回退到 0，把整个 `ä/å` 纳入替换区。

> 待本地验证：具体数值请运行确认。

#### 4.2.5 小练习与答案

**练习 1**：若 `old == new`，`replacement_range` 返回什么？`Source::replace` 据此会怎么做？

**参考答案**：返回 `None`。`Source::replace`（[source.rs:88-90](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L88-L90)）匹配到 `None` 直接返回 `0..0`，不做任何编辑与重解析。

**练习 2**：为什么前缀回退用 `-= 1`，而后缀推进用 `+= 1`？

**参考答案**：两者都在把「被字节级 diff 劈开的多字节字符」整纳入替换区。前缀 `prefix` 是替换区**左端**，回退让它左扩；后缀 `suffix` 是从右端算起的公共长度，`old.len() - suffix` 才是替换区右端，增大 `suffix` 让右端左缩——方向不同，但都把劈开的字符圈进替换区，从而保证两端都落在字符边界上。

---

### 4.3 edit 主流程：截断 + 重算 + 写时复制

#### 4.3.1 概念说明

拿到一个明确的 `(replace, with)` 后，`Lines::edit` 要同时更新两样东西：文本本身、以及行起点表。朴素做法是「整段重算行表」（像 `Lines::new` 那样扫一遍全文），但 Typst 选择更省的策略——**只重算编辑点之后的行起点**。

依据很简单：编辑点 `replace.start` 之前的文本一字未动，那么所有起点 `byte_idx < replace.start` 的行都仍然有效。编辑点之后的行起点则可能整体平移（插入/删除改变了字节偏移），与其逐个调整，不如丢弃后重算。

#### 4.3.2 核心流程

```
edit(replace, with):
  start_byte  = replace.start
  start_utf16 = byte_to_utf16(start_byte)        // 编辑点的 UTF-16 偏移
  line        = byte_to_line(start_byte)          // 编辑点所在行号

  inner = Arc::make_mut(&mut self.0)              // 写时复制
  inner.text.replace_range(replace, with)         // 1. 改文本

  inner.lines.truncate(line + 1)                  // 2. 丢弃「编辑点所在行」之后的行起点
                                                  //    （保留 0..=line，因为它们的起点 < start_byte 仍有效）
  if text[..start_byte] 以 '\r' 结尾 且 with 以 '\n' 开头:
      inner.lines.pop()                           // 3. \r\n 跨边界拼接修正（见 4.4）

  inner.lines.extend(lines_from(start_byte, start_utf16, &text[start_byte..]))
                                                  // 4. 从编辑点重算后续行起点
```

第 2 步保留的是 `0..=line`：注意它**保留了编辑点所在的那一行**（索引 `line`）。这一行的起点 `≤ start_byte`，确实未被编辑破坏。第 4 步再从 `start_byte` 开始往后扫描，补回所有更靠后的行起点。

#### 4.3.3 源码精读

完整 `edit` 在 [lines.rs:205-229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L205-L229)。核心几行：

写时复制 + 改文本（[lines.rs:210-213](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L210-L213)）：

```rust
let inner = Arc::make_mut(&mut self.0);
// 改文本本身。
inner.text.replace_range(replace.clone(), with);
```

截断失效行起点（[lines.rs:216](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L216)）：

```rust
// 移除失效的行起点。
inner.lines.truncate(line + 1);
```

从编辑点重算（[lines.rs:224-228](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L224-L228)）：

```rust
// 重算编辑点之后的行起点。
inner.lines.extend(lines_from(
    start_byte,
    start_utf16,
    &inner.text[start_byte..],
));
```

注意 `lines_from` 的起点偏移传的是 `start_byte` / `start_utf16`，扫描的子串是 `&text[start_byte..]`。这样它产出的每个 `Line` 的 `byte_idx` / `utf16_idx` 都已加上起点的基准偏移，可直接 `extend` 拼到表尾。

`lines_from` 本身是 4.4 节（注：指下一个模块 4.4 在本讲内部对应「lines_from」语义，见下）的主角，此处先把它当成「给定起点，扫描子串产出后续行起点」的黑盒。

> 说明：本讲的小节编号里，`lines_from` 的细节单独成节于 4.4，`\r\n` 拼接修正则在 4.5。

#### 4.3.4 代码实践

**实践目标**：用 `test_source_file_edit` 同款思路，验证 `edit` 后行起点与「直接对结果文本新建 `Lines`」完全一致。

参考 crate 内已有的 [test_source_file_edit（lines.rs:367-405）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L367-L405)，它对每组 `(prev, range, with, after)` 同时跑 `edit` 与 `replace` 两条路径，再比对内部 `lines` 字段。由于 `Line` 字段是私有的，外部读者改用**公开 API** 写等价检查（在自己的小 crate 或本地测试里）：

```rust
// 示例代码（非项目原有代码）
use typst_syntax::Lines;

fn assert_lines_match(prev: &str, range: std::ops::Range<usize>, with: &str, after: &str) {
    let mut edited = Lines::new(prev.to_string());
    edited.edit(range, with);

    let reference = Lines::new(after); // 直接对结果文本建表，当作 ground truth

    assert_eq!(edited.text(), reference.text());
    assert_eq!(edited.len_lines(), reference.len_lines());
    for i in 0..reference.len_lines() {
        assert_eq!(edited.line_to_byte(i), reference.line_to_byte(i),
            "line {i} byte start mismatch");
    }
}
```

然后喂入几组用例（取自项目测试，[lines.rs:390-404](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L390-L404)）：

```rust
// 示例代码
assert_lines_match("abc\n",     0..0,  "hi\n",   "hi\nabc\n");      // 行首插入
assert_lines_match("abc\ndef",  7..7,  "hi",     "abc\ndefhi");     // 行尾追加
assert_lines_match("abc\ndef\r", 8..8, "\nghi",  "abc\ndef\r\nghi"); // \r\n 拼接（见 4.5）
```

预期结果：每组用例的两条断言全部通过，说明 `edit` 重建出的行起点表与「从零建表」完全一致。

> 待本地验证：如未配置依赖，可直接在 `lines.rs` 的 `#[cfg(test)] mod tests` 里临时加这条函数后运行 `cargo test -p typst-syntax test_source_file_edit`（在本地克隆上操作）。

#### 4.3.5 小练习与答案

**练习 1**：`truncate(line + 1)` 为什么是 `line + 1` 而不是 `line`？

**参考答案**：编辑点 `start_byte` 落在第 `line` 行**之内**（`byte_to_line(start_byte) == line`），而这一行的起点 `byte_idx ≤ start_byte`，并未被编辑破坏，必须保留。`Vec::truncate(n)` 保留索引 `0..n`，所以要传 `line + 1` 才能保住第 `line` 行。

**练习 2**：若一次编辑只在某行内部增删字符、完全不触碰换行符，`lines_from` 会产出几个新 `Line`？

**参考答案**：零个。编辑点之后的子串里没有任何换行符，`lines_from` 扫不到换行就提前返回 `None`（见 4.4）。于是 `extend` 不追加任何行，行表除被截断部分外不变——又因为这次编辑没动换行，被截断的也本就不存在，整体行表不变。

---

### 4.4 lines_from：从偏移量增量重建行起点

#### 4.4.1 概念说明

`lines_from` 是行表重建的原子积木。给它一个起点偏移（`byte_offset`、`utf16_offset`）和一段子串，它返回一个迭代器，**每遇到一个换行就产出一个 `Line`**，其 `byte_idx` / `utf16_idx` 是换行之后下一行的起点（已加上起点的基准偏移）。

它和「从零建表」用的 `lines` 函数是同一个零件：`lines` 只是在前面补一个 `{byte_idx:0, utf16_idx:0}`，然后接上 `lines_from(0, 0, text)`（[lines.rs:245-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L245-L249)）。换句话说，`Lines::new` 是「全量重算」，`Lines::edit` 是「从 `start_byte` 增量重算」，二者共用 `lines_from`。

#### 4.4.2 核心流程

`lines_from` 用一个 `unscanny::Scanner` 在子串上推进，靠 `std::iter::from_fn` 产出一个惰性迭代器：

```
loop:
  s.eat_until(每个字符 c: 累加 c 的 utf16 长度，且 c 是换行则停)   // 走到下一个换行
  if s.done(): return None                                        // 没有更多换行，结束
  if s.eat()=='\r' 且 s.eat_if('\n'):  utf16_idx += 1             // \r\n 算一次换行，多吞一个 \n
  emit Line { byte_idx: byte_offset + s.cursor(), utf16_idx }     // 记录换行之后的下一行起点
```

两个微妙点：

1. **utf16 计数靠谓词副作用**。`eat_until` 的谓词对**每个**经过的字符（包括换行符本身）都 `utf16_idx += c.len_utf16()`。所以走到换行时，`utf16_idx` 已经把换行符算进去了。
2. **`\r\n` 合并**。`is_newline` 把 `\r` 和 `\n` 都判为换行，但 Typst 要把它们连在一起时算**一次**。于是代码先 `eat()` 掉 `\r`，若紧跟 `\n` 再 `eat_if('\n')` 吞掉，并给 `utf16_idx` 补 `+1`（因为谓词只对 `\r` 加过 1，`\n` 这一字节要补上）。

#### 4.4.3 源码精读

`lines_from` 在 [lines.rs:252-276](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L252-L276)：

```rust
fn lines_from(
    byte_offset: usize,
    utf16_offset: usize,
    text: &str,
) -> impl Iterator<Item = Line> + '_ {
    let mut s = unscanny::Scanner::new(text);
    let mut utf16_idx = utf16_offset;

    std::iter::from_fn(move || {
        s.eat_until(|c: char| {
            utf16_idx += c.len_utf16();
            is_newline(c)
        });

        if s.done() { return None; }

        if s.eat() == Some('\r') && s.eat_if('\n') {
            utf16_idx += 1;
        }

        Some(Line { byte_idx: byte_offset + s.cursor(), utf16_idx })
    })
}
```

谓词副作用在 [lines.rs:261-264](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L261-L264)；`\r\n` 合并在 [lines.rs:270-272](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L270-L272)。

`lines()` 复用它来全量建表（[lines.rs:245-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L245-L249)）：

```rust
fn lines(text: &str) -> Vec<Line> {
    std::iter::once(Line { byte_idx: 0, utf16_idx: 0 })
        .chain(lines_from(0, 0, text))
        .collect()
}
```

#### 4.4.4 代码实践

**实践目标**：手工执行 `lines_from`，理解它如何为测试常量 `TEST` 产出行起点。

操作步骤：取 `TEST = "ä\tcde\nf💛g\r\nhi\rjkl"`（[lines.rs:288](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L288)），项目测试已给出 ground truth（[lines.rs:294-301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L294-L301)）：

```
[
    Line { byte_idx:  0, utf16_idx:  0 },
    Line { byte_idx:  7, utf16_idx:  6 },   // 在 '\n' 之后
    Line { byte_idx: 15, utf16_idx: 12 },   // 在 "\r\n" 之后（合并算一次）
    Line { byte_idx: 18, utf16_idx: 15 },   // 在 '\r' 之后
]
```

请你按上面「核心流程」的步骤，在纸上从左到右扫一遍 `TEST`，逐个换行验证 `byte_idx` 与 `utf16_idx` 的来历。重点确认两件事：

1. `f💛g\r\nhi` 里的 `\r\n` 只产生**一个**行起点（`byte_idx:15`），而不是两个。
2. 末尾的 `\r`（lone `\r`）也产生一个行起点（`byte_idx:18`），因为 `is_newline('\r')` 为真。

预期结果：你手算的四个行起点与上面 ground truth 完全一致。

> 待本地验证：若不放心字符字节宽度，可在本地用 `Lines::new(TEST).len_lines()` 确认行数为 4。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `eat_until` 的谓词里要在判断 `is_newline(c)` **之前**先 `utf16_idx += c.len_utf16()`？

**参考答案**：因为换行符本身也占 UTF-16 编码单元（`\n`、`\r` 各占 1），而 `Line.utf16_idx` 记录的是**换行之后**下一行的起点。如果不把换行符算进去，下一行的 `utf16_idx` 就会少 1，导致 `byte_to_utf16` / `utf16_to_byte` 全部错位。

**练习 2**：`lines_from` 在什么情况下产出空迭代器？

**参考答案**：当传入的 `text` 子串里**不含任何换行符**时。`eat_until` 一路吃到末尾，`s.done()` 为真，第一次调用就返回 `None`。`Lines::edit` 正是依赖这一点：纯字符编辑（不动换行）时 `extend` 不追加任何行。

---

### 4.5 \r 与 \n 跨编辑边界拼接的修正

#### 4.5.1 概念说明

这是 `edit` 里最微妙的一行。考虑这个场景：

- 旧文本末尾是 `"abc\ndef\r"`（最后一个字符是孤立的 `\r`，它本身是一个换行）。
- 编辑在末尾（`replace = 8..8`）插入 `"\nghi"`。

编辑**之前**，行表里记录了「孤立 `\r` 之后有一个行起点」（在 byte 8）。编辑**之后**，文本变成 `"abc\ndef\r\nghi"`：原来的 `\r` 与新插入的 `\n` 拼成了 `\r\n`，而 `\r\n` 在 Typst 里是**一次**换行。于是那个原本指向 byte 8 的行起点就**错了**——byte 8 现在压在 `\n` 上，而行起点必须落在换行**之后**（应为 byte 9）。

问题根源：编辑点所在行的起点 `byte_idx == start_byte`，是 4.3 节 `truncate(line + 1)` 保留下来的「恰好压在边界上」的行起点。在拼接出 `\r\n` 时它失效了。

#### 4.5.2 核心流程

`edit` 用一个条件分支处理这个边角：

```
if text[..start_byte] 以 '\r' 结尾  且  with 以 '\n' 开头:
    inner.lines.pop()    // 丢掉那个压在 start_byte 上的行起点
// 随后 lines_from(start_byte, ...) 会重新扫描，把 \r\n 当一次换行，正确产出 start_byte+1 的起点
```

为什么这里能安全地 `pop`？因为只要 `text[..start_byte]` 以 `\r` 结尾，`start_byte` 就一定是个换行的边界，`byte_to_line(start_byte)` 返回的那一行**恰好在 `start_byte` 开始**——`pop` 掉的就是它。随后 `lines_from` 从 `start_byte` 重扫，把新拼出的 `\r\n` 正确识别为一次换行，补回一个位于 `start_byte+1` 的行起点。

#### 4.5.3 源码精读

拼接修正分支在 [lines.rs:219-221](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L219-L221)：

```rust
// 处理 \r 与 \n 的拼接。
if inner.text[..start_byte].ends_with('\r') && with.starts_with('\n') {
    inner.lines.pop();
}
```

它夹在 `truncate` 与 `extend` 之间，正好修正「保留行」中可能压在边界上的那一项。

项目测试明确覆盖了这个用例（[lines.rs:401](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L401)）：

```rust
// 测试拼接出的 \r 与 \n。
test("abc\ndef\r", 8..8, "\nghi", "abc\ndef\r\nghi");
```

反方向也存在一个用例（在行首插入 `hi\r`，与原有的开头 `\n` 拼成 `\r\n`，[lines.rs:391](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L391)）：

```rust
test("\nabc", 0..0, "hi\r", "hi\r\nabc");
```

注意第二个用例里 `start_byte == 0`，`text[..0]` 是空串、并不以 `\r` 结尾，所以**不会**触发 `pop`；它的正确性靠的是 `lines_from` 从 byte 0 重扫时把 `hi\r\n` 里的 `\r\n` 算成一次换行。两个用例从两个方向共同约束了 `\r\n` 的处理。

#### 4.5.4 代码实践

**实践目标**：复现 `\r\n` 拼接用例，并观察「没有 `pop` 会怎样」。

操作步骤：

1. 用 4.3.4 的 `assert_lines_match` 跑这一组：`assert_lines_match("abc\ndef\r", 8..8, "\nghi", "abc\ndef\r\nghi")`。
2. 确认它通过：`edited` 的行起点表与 `reference = Lines::new("abc\ndef\r\nghi")` 完全一致，即行起点为 `[0, 4, 9]`（0 是 `abc`，4 是 `def`，9 是 `ghi`，其中 `\r\n` 合并产生一个起点 9）。
3. **思考实验**：假如把 [lines.rs:219-221](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L219-L221) 那三行注释掉（不要真改源码，只在脑中推演），`truncate(3)` 会保留 `[0, 4, 8]`，其中 `8` 压在新拼出的 `\n` 上，行表就会多出一个错误的行起点。

预期结果：第 2 步通过；第 3 步说明若缺少 `pop`，行起点表会多出一个落在 `\n` 上的非法项。

> 待本地验证：若想实际看到「缺少 `pop`」的后果，可在本地克隆上**临时**注释那三行后运行 `cargo test -p typst-syntax test_source_file_edit`，观察失败信息，然后还原。

#### 4.5.5 小练习与答案

**练习 1**：触发 `pop` 的两个条件是什么？为什么满足这两个条件时，被 `pop` 的行起点一定位于 `start_byte`？

**参考答案**：条件是 `text[..start_byte]` 以 `\r` 结尾、且 `with` 以 `\n` 开头。因为 `\r` 是换行，所以 `start_byte` 一定是某行的起点；`byte_to_line(start_byte)` 返回的那一行（即 `truncate` 保留的最后一行）的 `byte_idx` 恰为 `start_byte`。`pop` 丢的就是它。

**练习 2**：用例 `test("\nabc", 0..0, "hi\r", "hi\r\nabc")` 为何不触发 `pop` 也能得到正确结果？

**参考答案**：这里 `start_byte == 0`，`text[..0]` 是空串、不以 `\r` 结尾，条件不成立，不 `pop`。但 `truncate(1)` 只保留了 `[0]`，随后 `lines_from(0, 0, "hi\r\nabc")` 从头重扫，把 `hi\r\n` 里的 `\r\n` 正确算成一次换行，产出位于 byte 4 的行起点，最终行表 `[0, 4]` 正确。换言之，拼接出 `\r\n` 的两侧无论哪边由编辑「贡献」，最终都靠 `lines_from` 的 `\r\n` 合并逻辑兜底。

---

## 5. 综合实践

把本讲四件事（最小编辑、写时复制、截断+重算、`\r\n` 拼接）串起来，完成下面这个端到端的小任务。

**任务**：实现一个函数 `replace_and_check(prev: &str, new: &str)`，它：

1. 用 `Lines::new(prev.to_string())` 建表；
2. 调用 `replacement_range(new)` 得到 `(prefix, suffix)`（若为 `None` 直接返回「无变化」）；
3. 据 `(prefix, suffix)` 算出 `replace` 与 `with`，调用 `edit`；
4. 同时用 `Lines::new(new)` 直接建一张「标准答案」表；
5. 逐行比对 `line_to_byte(i)`，打印是否一致。

然后至少喂入以下三组用例并解释结果：

- `prev = "aäc"`, `new = "aåc"`（中间多字节字符被换，验证字符边界对齐：替换区应覆盖整个 `ä/å`，而非从字节 2 开始）。
- `prev = "x\ny\nz"`, `new = "x\ny"`（删除末尾两段，验证截断+重算后行数从 3 变 2）。
- `prev = "m\r"`, `new = "m\r\nn"`（验证 `\r\n` 跨边界拼接修正：注意 `replace` 走的是 `replacement_range` 路径，确认它最终也调用 `edit` 并触发 `pop` 分支）。

**验收标准**：每组用例，你函数里 `edit` 之后的行表都与「标准答案」逐行一致；并且你能用本讲的概念解释每组为何如此（尤其第 1 组的字符边界、第 3 组的 `pop`）。

> 提示：第 3 组里 `replacement_range("m\r\nn")`（`prev = "m\r"`）会发现公共前缀 `"m\r"`（2 字节）、公共后缀为空，于是 `replace = 2..2`（在字节 2 处的一次**插入**）、`with = "\nn"`。`edit` 时 `text[..2] = "m\r"` 以 `\r` 结尾、`with` 以 `\n` 开头，正好触发 4.5 的 `pop` 分支；最终行表应为 `[{0,0},{3,3}]`。具体范围请你在实践中打印确认。这是把 `replacement_range` 与 `\r\n` 拼接两件事压在一起的典型例子。

---

## 6. 本讲小结

- `Source::edit` 把「改文本+行表」与「增量重解析」分成两步：前者完全由 `Lines::edit` 负责，本讲只讲前者。
- `replacement_range` 用「公共前缀 + 公共后缀」求最小单次编辑，并用 `is_char_boundary` 做字符边界对齐：前缀回退（`-1`）、后缀前进（`+1`），宁可替换区稍大也绝不在 UTF-8 字符中间切开。
- `Lines::edit` 的增量策略是「保留编辑点所在行及以上（`truncate(line+1)`），丢弃之后的，再用 `lines_from` 从 `start_byte` 重算」，避免整段重扫。
- `Arc::make_mut` 在 `Source` 与 `Lines` 两层各做一次写时复制，让「旧版本被缓存、新版本要改」得以安全进行。
- `lines_from` 是全量建表与增量重建共用的原子：每遇换行产出一个行起点，`\r\n` 合并算一次换行，UTF-16 计数靠 `eat_until` 谓词副作用。
- 当编辑点左侧是 `\r`、插入内容以 `\n` 开头时，二者拼成 `\r\n`，需要 `pop` 掉压在 `start_byte` 上的失效行起点，再由 `lines_from` 重算。

---

## 7. 下一步学习建议

本讲把「文本与行表」的编辑讲完了，但 `Source::edit` 的另一半——`reparse`——尚未展开。建议接下来进入 **U9 增量重解析**：

- **u9-l1 增量编译与 reparse 入口**：看 `Source::edit` 调完 `Lines::edit` 后如何调用 `reparse`，以及失败时如何回退到全量 `parse + numberize`。
- **u9-l2 try_reparse 核心算法**：理解它如何找到完全包住编辑范围的最内层节点并局部重解析。

此外，如果想从更高层看 `Lines` 如何被 `Source` 总成，可回顾 **u8-l1 Source 文件抽象**；若想再深入「行起点编号如何支撑 span 反查」，可回顾 **u6-l2 编号 Span 与 numberize**。
