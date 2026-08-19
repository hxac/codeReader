# HunkSink：从行号到字节区间与锚点

## 1. 本讲目标

上一讲（u3-l1）我们看到 `compute_hunks` 调用 imara-diff 算出行级差异，但 imara-diff 的输出只是「行号区间对」——它既不知道字节偏移，也不知道锚点。本讲专门讲解两者之间的翻译官 `HunkSink`，学完后你应该能：

1. 准确说出 imara-diff hunk 的 `before`/`after` 行区间语义（半开区间、纯插入时 `before` 为空、纯删除时 `after` 为空）。
2. 手工推演 `compute_line_offsets` 构建的行偏移前缀和表，并用它把 base 侧行号换算成 `diff_base_byte_range`。
3. 讲清楚 `process_change` 的完整换算链路：行号 → 字节区间（base 侧）/ 锚点区间（buffer 侧）/ Point 区间（`diff_base_point_range`）。
4. 解释 `diff_base_point_range` 这个「冗余」字段存在的理由：它为 SumTree 摘要里的 `removed_rows` 预先算好 base 侧行数。

## 2. 前置知识

本讲只依赖前面几讲已经建立的概念，这里做一次快速回顾和少量补充：

- **行号（row）、字节偏移（offset）、Point、Anchor**：`Point { row, column }` 是 buffer 里的二维坐标；字节偏移是文本从头数起的字节数；`Anchor` 是跨编辑稳定的「位置身份」（u1-l1、u2-l1）。
- **半开区间 `[start, end)`**：本讲所有行区间都是半开的——`before = 2..3` 表示「只涉及第 2 行」这一行（行号从 0 数起）。
- **前缀和（prefix sum）**：给定数列 \( a_0, a_1, \dots, a_{n-1} \)，前缀和数组满足 \( S_0 = 0,\ S_{i+1} = S_i + a_i \)。于是任意区间的和可以用两次查表相减得到：\( a_i + \dots + a_{j-1} = S_j - S_i \)。`compute_line_offsets` 就是一张「每行字节长度」的前缀和表。
- **imara-diff 的三步用法**（u3-l1）：`InternedInput::new(lines(base), lines(buffer))` 把两侧文本按行切成 token 装载，`Diff::compute(Algorithm::Histogram, &input)` 计算，`diff.hunks()` 逐个吐出 `DiffHunk { before, after, .. }`。`lines` 来自 `imara_diff::sources`（[buffer_diff.rs:L2](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2)），**每个 token 都含行尾符**，所以 token 长度之和等于文本总字节数——这是本讲偏移表能够成立的基石（下面会用测试反证这一点）。
- **base 侧没有锚点**：diff 的基准文本在计算阶段只是一份 `Arc<str>` 加一份 `Rope`，不是一个带锚点系统的 buffer。所以 buffer 侧用锚点、base 侧用字节偏移，是「一边有身份系统、一边没有」的自然结果。

## 3. 本讲源码地图

本讲全部源码都在同一个文件里：

| 位置 | 作用 |
| --- | --- |
| [buffer_diff.rs:L1184-L1242](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1184-L1242) | `compute_hunks`：diff 计算总入口，创建并驱动 HunkSink（上一讲主题，本讲只看它与 HunkSink 的接口） |
| [buffer_diff.rs:L1243-L1249](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1243-L1249) | `struct HunkSink`：四个字段的结构体定义 |
| [buffer_diff.rs:L1251-L1266](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1251-L1266) | `HunkSink::new`：预计算 base 文本的行偏移表 |
| [buffer_diff.rs:L1268-L1276](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1268-L1276) | `compute_line_offsets`：前缀和表的构建 |
| [buffer_diff.rs:L1279-L1344](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1279-L1344) | `process_change`：单个 hunk 的完整换算（本讲主角） |
| [buffer_diff.rs:L131-L139](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L139) | `struct InternalDiffHunk`：换算的产物（SumTree 里真正存的东西） |
| [buffer_diff.rs:L183-L200](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L183-L200) | `DiffHunkSummary` 的 `summary()`：`diff_base_point_range` 的唯一消费点 |
| [buffer_diff.rs:L263-L273](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L263-L273) | `usize` 的 `SeekTarget` 实现：解释为什么 base 侧选择字节偏移做坐标 |
| [buffer_diff.rs:L2387-L2423](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2387-L2423) | `assert_hunks`：测试断言工具，直接用字节区间切 base 文本 |
| [buffer_diff.rs:L2442-L2496](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2442-L2496) | `test_buffer_diff_simple`：本讲实践环节的参照模板 |

## 4. 核心概念与源码讲解

### 4.1 HunkSink：diff 算法与存储层之间的翻译官

#### 4.1.1 概念说明

imara-diff 是一个纯粹的差异算法库：它输入两串「token 序列」，输出若干「token 区间对」。它对 token 的具体含义一无所知——在我们这里 token 恰好是「带行尾符的一行文本」，所以输出可以被解读为行号区间。

但 SumTree 里存的 `InternalDiffHunk` 需要的是三套坐标：

- base 侧的**字节区间** `diff_base_byte_range`（供切片 base 文本、供 `usize` SeekTarget 剪枝）；
- buffer 侧的**锚点区间** `buffer_range`（供跨编辑稳定定位）；
- base 侧的 **Point 区间** `diff_base_point_range`（供摘要求 `removed_rows`，见 4.4）。

`HunkSink` 就是站在中间做这套换算的临时对象。它是一个私有结构体，生命周期极短：在 `compute_hunks` 里被创建、被喂入每个 imara-diff hunk、最后一次性交出全部成品。

#### 4.1.2 核心流程

```text
compute_hunks(diff_base, buffer, diff_options)
  ├─ InternedInput::new(lines(base), lines(buffer_text))   # 按 token（行）装载
  ├─ Diff::compute(Algorithm::Histogram, &input)            # 算差异
  ├─ diff.postprocess_lines(&input)                          # 规范化歧义摆放（u3-l1）
  ├─ sink = HunkSink::new(...)                               # ← 预计算行偏移表
  ├─ for hunk in diff.hunks():
  │     sink.process_change(hunk.before, hunk.after)         # ← 逐个换算
  └─ for hunk in sink.finish():
        tree.push(hunk, buffer)                              # 入 SumTree（u2-l2）
```

一句话概括：**new 阶段建表，process_change 阶段查表换算，finish 阶段收尾**。

#### 4.1.3 源码精读

先看驱动方 `compute_hunks` 中与 HunkSink 的接口（完整讲解见 u3-l1）：

[buffer_diff.rs:L1221-L1227](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1221-L1227) 这段代码创建 sink、把 imara-diff 的每个 hunk 喂给它、最后把成品逐个压入 SumTree。注意喂进去的只有 `hunk.before` 和 `hunk.after` 两个行号区间——imara-diff 的全部输出就这么多。

再看结构体本身：

[buffer_diff.rs:L1243-L1249](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1243-L1249) `HunkSink` 持有四个东西：base 文本的 Rope（用于字节→Point 换算）、计算发起时的 buffer 快照（用于创建锚点）、可选的 diff 选项（word diff 用，下一讲）、以及预计算好的 `old_line_offsets` 行偏移表。

注意它的生命周期被 `'a` 参数钉死在这几份输入的引用上——它是个纯翻译器，不拥有任何数据，算完即弃。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 imara-diff 输出的「原始行号区间对」，建立对 `before`/`after` 的直觉，再对照 HunkSink 换算后的成品。

**操作步骤**：在 `mod tests` 里新增一个普通测试（不需要 gpui 上下文，因为 imara-diff 是纯 CPU 计算）。`InternedInput`、`Diff`、`Algorithm`、`lines` 都已在文件顶部导入，`use super::*`（[buffer_diff.rs:L2429](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2429)）会把它们带进测试模块。

```rust
// 示例代码：非项目原有，建议加在 mod tests 内
#[test]
fn test_print_imara_raw_hunks() {
    let base = "alpha\nbravo\ncharlie\ndelta\necho\n";
    let text = "alpha\nbravo\nCHARLIE\ndelta\nxray\necho\n";
    let input = InternedInput::new(lines(base), lines(text));
    let mut diff = Diff::compute(Algorithm::Histogram, &input);
    diff.postprocess_lines(&input);
    for hunk in diff.hunks() {
        println!("before {:?} after {:?}", hunk.before, hunk.after);
    }
}
```

运行方式（在 zed 仓库根目录）：

```bash
cargo test -p buffer_diff test_print_imara_raw_hunks -- --nocapture
```

**需要观察的现象**：打印出的行号区间对长什么样，`before` 和 `after` 分别落在哪一行。

**预期结果**（待本地验证，两处改动的行号可以按下面的推演预先算好）：

- 修改行（`charlie` → `CHARLIE`）：`before 2..3 after 2..3`——两个区间都非空；
- 插入行（`delta` 后新增 `xray`）：`before 4..4 after 4..5`——`before` 是空区间，锚在插入点。

这个测试的价值在于把「算法的原始输出」和「我们最终存进树里的 `InternalDiffHunk`」摆在同一张桌上看：后者的所有字段都能从前者加上一张偏移表推导出来。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HunkSink` 不需要为 buffer 侧也建一张行偏移表？

**答案**：因为 buffer 侧行号可以直接通过当时持有的 `buffer` 快照换成锚点——`buffer.anchor_before(Point::new(row, 0))` 一步到位。base 侧则只是一份裸字符串（`&str`）加 Rope，没有任何「按行号查位置」的接口，所以必须自己建表把行号翻译成字节偏移。

**练习 2**：`HunkSink::finish` 做了什么复杂工作吗？

**答案**：没有。[buffer_diff.rs:L1346-L1348](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1346-L1348) 只是 `self.hunks` 的简单移动返回。所有工作都在 `process_change` 里完成了；`finish` 的存在只是把 `Vec<InternalDiffHunk>` 从 sink 中取出并消耗掉 sink 的语法糖。

### 4.2 compute_line_offsets：base 文本的行偏移前缀和

#### 4.2.1 概念说明

`compute_line_offsets` 解决的问题是：**「base 文本第 i 行从第几个字节开始？」**

它的做法是教科书式的前缀和。设 base 文本被 `lines()` 切成 \( n \) 个 token，第 \( i \) 个 token 长度为 \( |L_i| \)（含行尾符），则：

\[
S_0 = 0, \qquad S_{i+1} = S_i + |L_i|
\]

于是「第 i 行到第 j-1 行（含行尾符）占用的字节区间」一次查表相减即得：

\[
\text{bytes}(i..j) = \bigl[\,S_i,\; S_j\,\bigr)
\]

这正是数学上「区间和 = 两个前缀和之差」的直接应用，把每次换算从「重新数一遍字节」降为 O(1) 查表。

**为什么 token 必须含行尾符？** 因为只有每个字节都恰好被一个 token 认领、且不重不漏，前缀和的终点才会落在真实的行边界上。这一点可以用项目自己的测试反证：`test_buffer_diff_simple` 断言被删除的文本是 `"two\n"`（连换行符一起，[buffer_diff.rs:L2467](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2467)），而这段文本来自 `&diff_base[hunk.diff_base_byte_range]`（[buffer_diff.rs:L2401](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2401)）。若 token 不含行尾符，前缀和就会错位，切出来的将是乱码片段，测试根本不可能通过。

还有两个边角性质值得记住：

- 表长是「行数 + 1」：`offsets.len() == n + 1`，最后一项 \( S_n \) 等于文本总字节数（无论最后一行是否带换行符，token 都不重不漏地覆盖全文）；
- 因此 `old_line_offsets[old_end]` 在 `old_end == 行数` 时依然有效，越界访问不可能发生（imara-diff 的行号上界就是行数）。

#### 4.2.2 核心流程

```text
compute_line_offsets(text):
    offsets = [0]                      # S_0 = 0
    offset = 0
    for line in lines(text):           # 每个 token 含行尾符
        offset += line.len()           # 滚动累加
        offsets.push(offset)           # 记录每个 S_{i+1}
    return offsets                     # 长度 = 行数 + 1
```

一次 O(总字节数) 的线性扫描，只在 `HunkSink::new` 时执行一次，之后所有 hunk 共享。

#### 4.2.3 源码精读

[buffer_diff.rs:L1251-L1266](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1251-L1266) 构造函数 `HunkSink::new`：先调 `compute_line_offsets(diff_base)` 建表，再原样保存四个输入。注意建表用的是 `diff_base: &str`（已按 u3-l1 所述做过 LF 归一化的字符串），而字节→Point 换算用的是 `diff_base_rope`——两者内容一致（`update_diff` 里有 `debug_assert` 交叉校验，[buffer_diff.rs:L1941-L1944](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1941-L1944)），只是两份表示。

[buffer_diff.rs:L1268-L1276](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1268-L1276) `compute_line_offsets` 本体，与上面的伪代码逐行对应：从 `[0]` 起步，逐 token 累加字节数并压栈。

#### 4.2.4 代码实践

**实践目标**：为一个手工写定的 5 行 base 文本手算 `old_line_offsets`，得到一张可以离线查的表。

**操作步骤**：

1. 写定 base 文本（5 行，故意让各行长度不同）：

   ```text
   alpha
   bravo
   charlie
   delta
   echo
   ```

   即字符串 `"alpha\nbravo\ncharlie\ndelta\necho\n"`。

2. 逐行累加**含行尾符**的长度：`alpha\n` 6 字节、`bravo\n` 6、`charlie\n` 8、`delta\n` 6、`echo\n` 5，总共 31 字节。

3. 写出前缀和表：

   | 行号 i | 0 | 1 | 2 | 3 | 4 | 5 |
   | --- | --- | --- | --- | --- | --- | --- |
   | `offsets[i]` | 0 | 6 | 12 | 20 | 26 | 31 |

   表长 6 = 行数 5 + 1；`offsets[5] = 31` 恰为文本总长。

4. 用表做两次查表减法练习：行 `2..3` 的字节区间是 `12..20`（正是 `"charlie\n"`）；行 `4..4` 的字节区间是 `26..26`（空区间，位于 `"echo\n"` 行首之前）。

**需要观察的现象**：纯手工推演，无运行步骤；重点检查「行区间→字节区间」的换算是否已经成为机械操作。

**预期结果**：上表与两个区间答案。5.3 节的综合实践会回到这张表，用程序输出核对。

#### 4.2.5 小练习与答案

**练习 1**：若 base 文本为 `"a\nb"`（末行无换行符），`compute_line_offsets` 返回什么？

**答案**：`[0, 2, 3]`。token 是 `"a\n"`（2 字节）和 `"b"`（1 字节），前缀和为 0、2、3；`offsets[2] == 3` 仍是文本总长。可见「token 含行尾符」与「末行可以没有行尾符」并不矛盾——每个 token 认领自己那行的全部字节，包括可能缺席的最后一个换行符。

**练习 2**：把 `compute_line_offsets` 里的 `offset += line.len()` 改成 `offset += line.trim_end().len()` 会发生什么？

**答案**：每行会漏掉 1 个换行符字节，前缀和整体偏小且误差逐行累积（第 i 行起偏移少 i 个字节）。`diff_base_byte_range` 将指向错误的字节位置，`&diff_base[range]` 切出的删除文本错位（`assert_hunks` 立刻失败）。这是一个很好的思想实验：换算层的「一个小字符」错误会以测试失败的形式在断言层显形。（不要真的改源码，本练习只需推演。）

### 4.3 process_change：一次换算的完整链路

#### 4.3.1 概念说明

`process_change(before, after)` 是本讲的主角。先明确输入语义：

- `before: Range<u32>`——**base 侧**被改动（删除或修改掉）的行区间，半开；
- `after: Range<u32>`——**buffer 侧**新出现的行区间，半开。

三类基本改动对应三种区间形态，与 u2-l1 学过的 `status()` 判定严丝合缝：

| 改动类型 | `before` | `after` | `diff_base_byte_range` | `buffer_range` | `status()` |
| --- | --- | --- | --- | --- | --- |
| 修改 | 非空 | 非空 | 非空 | 非空 | Modified |
| 纯插入 | 空 | 非空 | 空（锚在插入点） | 非空 | Added |
| 纯删除 | 非空 | 空 | 非空 | 空 | Deleted |

例如在 `delta` 与 `echo` 之间插入一行：`before = 4..4`（base 里没有任何行被改动），查表得字节区间 `26..26`——一个空区间，位置精确落在两行之间的边界上。「空区间也有位置」正是字节坐标（和锚点坐标）的表达能力，行号坐标做不到这一点。

buffer 侧则把 `after` 直接当作行号区间，通过当时的 buffer 快照铸成锚点。这里有一个承接 u2-l1 的关键设计：**锚点在 diff 计算的瞬间铸造，之后无论 buffer 怎么编辑都保持身份**。diff 在后台线程算完回来时 buffer 可能已经又变了，行号早已过期，而锚点仍然指向「原来那块文本」。

#### 4.3.2 核心流程

```text
process_change(before, after):
    1. old_start, old_end, new_start, new_end = 四个行号转 usize
    2. diff_base_byte_range = offsets[old_start] .. offsets[old_end]     # 查表：base 字节
    3. buffer_range = anchor_before(Point(new_start,0))
                    .. anchor_before(Point(new_end,0))                   # 铸锚：buffer 位置
    4. 算 base_line_count / buffer_line_count（word diff 的门槛，下一讲）
    5. （可选）两侧文本做词级 diff，得 base_word_diffs / buffer_word_diffs
    6. diff_base_point_range = rope.offset_to_point(byte_start)
                             .. rope.offset_to_point(byte_end)            # base 行数（见 4.4）
    7. push 进 self.hunks
```

三套坐标各走一条独立通路：**查表**得字节、**铸锚**得 buffer 位置、**Rope 换算**得 base 的 Point。

#### 4.3.3 源码精读

[buffer_diff.rs:L1281-L1292](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1281-L1292) 换算主体：先把四个行号从 u32 转成 usize（便于做索引），第 1286 行**一次查表相减得到 base 字节区间**，第 1288-1292 行把 `after` 行号区间经 `Point::new(row, 0)` 铸成锚点区间。两端统一用 `anchor_before`，于是纯插入/纯删除产生的空区间两端是同一个锚点——空锚点区间恒空（u2-l1），保证 Added/Deleted 判定在后续编辑下持久成立。另一个细节：`after` 的右端可以等于 buffer 的总行数，此时 `Point::new(row, 0)` 对应 buffer 末尾位置，仍然合法——「在文件末尾追加行」这类 hunk 不会因此出错。

[buffer_diff.rs:L1294-L1330](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1294-L1330) word diff 分支：先算出 `base_line_count` 与 `buffer_line_count`，满足「buffer 侧非空、两侧行数相等、不超过 `max_word_diff_line_count`」三个条件才计算词级差异。注意它复用了刚算出的 `diff_base_byte_range`（用 `chunks_in_range` 从 base Rope 取文本）和 `buffer_range`（用 `text_for_range` 取文本）。本讲只需认识到「换算产物立刻被下游消费」即可，词级差异本身是 u3-l3 的主题。

[buffer_diff.rs:L1332-L1343](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1332-L1343) 产物组装：构造 `InternalDiffHunk`。第三套坐标 `diff_base_point_range` 在这里用 `self.diff_base_rope.offset_to_point(...)` 对字节区间两端各换算一次得到——注意它**只在这里、拿着 base Rope 的时候**算，理由见 4.4。

对照 [buffer_diff.rs:L131-L139](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L139) 的结构体定义可以确认：`process_change` 恰好填满 `InternalDiffHunk` 的全部五个字段，一个不多一个不少。

最后补一句 base 字节坐标的「下游回报」：SumTree 为 `usize` 实现了 `SeekTarget`，直接拿 `diff_base_byte_range` 做子树剪枝（[buffer_diff.rs:L263-L273](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L263-L273)，u2-l2 已学）——这就是 base 侧选择字节偏移（而非行号或 Point）作存储坐标的实际收益之一：与 `hunks_intersecting_base_text_range` 等查询（u2-l3）天然对齐。

#### 4.3.4 代码实践

**实践目标**：用 4.2 手算的偏移表**先预测**一个修改 + 一个插入产生的 hunk 字段，再运行真实 diff 打印核对。

**操作步骤**：

1. 沿用 4.2 的 base：`"alpha\nbravo\ncharlie\ndelta\necho\n"`；构造 buffer：`"alpha\nbravo\nCHARLIE\ndelta\nxray\necho\n"`（改 `charlie`，插 `xray`）。
2. 手工预测（用 4.2 的表）：
   - 修改 hunk：`before 2..3, after 2..3` → 字节区间 `12..20`、buffer 行 `2..3`、状态 Modified；
   - 插入 hunk：`before 4..4, after 4..5` → 字节区间 `26..26`、buffer 行 `4..5`、状态 Added。
3. 在 `mod tests` 里新增（示例代码，非项目原有）：

   ```rust
   #[gpui::test]
   async fn test_hunk_sink_offsets_practice(cx: &mut gpui::TestAppContext) {
       let diff_base = "alpha\nbravo\ncharlie\ndelta\necho\n";
       let buffer_text = "alpha\nbravo\nCHARLIE\ndelta\nxray\necho\n";
       let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
       let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.to_string(), cx);
       for hunk in diff.hunks_intersecting_range(
           Anchor::min_max_range_for_buffer(buffer.remote_id()),
           &buffer,
       ) {
           println!(
               "rows {:?}, base bytes {:?} -> {:?}, status {:?}",
               hunk.range,
               hunk.diff_base_byte_range,
               &diff_base[hunk.diff_base_byte_range.clone()],
               hunk.status(),
           );
       }
   }
   ```

4. 运行：

   ```bash
   cargo test -p buffer_diff test_hunk_sink_offsets_practice -- --nocapture
   ```

**需要观察的现象**：打印出的每个 hunk 的行区间、字节区间、按字节区间切出的 base 文本、状态。

**预期结果**（待本地验证）：两行输出依次为

```text
rows 2..3 行区间, base bytes 12..20 -> "charlie\n", status modified
rows 4..5 行区间, base bytes 26..26 -> "", status added
```

（`status` 的实际打印形态是 `DiffHunkStatus` 的 Debug 输出。）若与手算一致，说明你已独立复现了 `process_change` 的换算逻辑。

#### 4.3.5 小练习与答案

**练习 1**：删除 base 的第 1 行 `bravo`（buffer 为 `"alpha\ncharlie\ndelta\necho\n"`），预测 hunk 的全部公开字段。

**答案**：`before = 1..2`、`after = 1..1`；`diff_base_byte_range = offsets[1]..offsets[2] = 6..12`，切出 `"bravo\n"`；buffer 行区间 `1..1` 为空；`status()` 为 Deleted（buffer 侧区间空）。可用与 4.3.4 相同的测试骨架验证。

**练习 2**：`buffer_range` 为什么不用 `anchor_after` 铸造结束端？

**答案**：两端统一用 `anchor_before` 使空区间的两端是**同一个**锚点对象语义（同位置 before 锚），从而「区间为空」这一事实被锚点系统忠实保留——后续任何编辑后它仍然是空区间，Deleted/Added 的判定不会翻转。如果一端 before、一端 after，空区间的两端会分属两个不同方向的锚，在边界处发生插入时可能不再重合。这承接 u2-l1「空锚点区间恒空」的设计动机。

### 4.4 diff_base_point_range：为 SumTree 摘要预存的 base 行数

#### 4.4.1 概念说明

`InternalDiffHunk` 里存了 base 侧的两套坐标：字节区间和 Point 区间。后者看起来是冗余的——有 Rope 在手，随时能把字节换成 Point。那为什么要多存一份？

答案藏在 u2-l2 讲过的 SumTree 约束里：`Item::summary` 的上下文类型 `Summary::Context` **只携带主 buffer 的快照**（[buffer_diff.rs:L215-L216](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L215-L216)），因为树是跟着 buffer 建的、树的导航都按 buffer 坐标进行。摘要要聚合的 `removed_rows`（base 侧删了多少行）却需要 base 侧行数——在 `summary()` 里把字节换算成 Point 需要 base Rope，而那里拿不到。

于是设计变成：**在还拿着 base Rope 的时刻（即 `process_change` 里）预先算好 base 侧行数区间，存进 hunk**。摘要层只做一次减法：

\[
\text{removed\_rows} = \text{point\_range.end.row} - \text{point\_range.start.row}
\]

这是「换算时机」的一个典型权衡：晚换算（查询时才算）拿不到上下文，早换算（创建时就算）要多存一个字段。`HunkSink` 选择了早换算。

#### 4.4.2 核心流程

```text
创建时（process_change，手握 base Rope）:
    diff_base_point_range = rope.offset_to_point(byte_start) .. rope.offset_to_point(byte_end)

聚合时（Item::summary，只有主 buffer 快照）:
    removed_rows = diff_base_point_range.end.row - diff_base_point_range.start.row   # 饱和减法

查询时（BufferDiffSnapshot::changed_row_counts）:
    (added_rows, removed_rows) = 树根摘要的两个计数                      # O(1)
```

一条数据，三个时刻，各取所需。

#### 4.4.3 源码精读

[buffer_diff.rs:L1335-L1340](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1335-L1340) 创建时的换算：对 `diff_base_byte_range` 两端各调一次 `offset_to_point`。注意纯插入 hunk 的字节区间两端相同，因此 Point 区间也退化为 `P..P`，对 `removed_rows` 贡献 0——与「插入不删除任何行」的直觉一致。

[buffer_diff.rs:L186-L199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L186-L199) 消费点：`summary()` 里 `added_rows` 由 buffer 锚点区间解析成 Point 后做行差（这里拿得到主 buffer 快照，没问题），而 `removed_rows`（第 193-197 行）直接用预存的 `diff_base_point_range` 做饱和减法——这是该字段在全 crate 的唯一读取处。

[buffer_diff.rs:L298-L301](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L298-L301) 最终出口：`changed_row_counts()` 读一次树根摘要，O(1) 返回 `(added_rows, removed_rows)`——编辑器状态栏的「+N −M」就是这么来的。

还有一个容易被忽略的副作用：`InternalDiffHunk` 派生了 `PartialEq`（[buffer_diff.rs:L131-L132](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L132)），`diff_base_point_range` 参与相等比较。u3-l5 将讲的 `compare_hunks` 用 `new_hunk != old_hunk` 判断 hunk 是否变化，因此 base 侧行数区间一变（哪怕字节区间不变），也会被视为「hunk 变了」并触发更精确的变更通知。另需注意：公开的 `DiffHunk` **不**暴露这个字段，所以测试里只能间接验证它（见下面的实践）。

#### 4.4.4 代码实践

**实践目标**：通过 `changed_row_counts()` 间接验证 `diff_base_point_range` 被正确预计算。

**操作步骤**：

1. 在 4.3.4 的测试末尾追加一行断言（示例代码）：

   ```rust
   assert_eq!(diff.changed_row_counts(), (1, 1));
   ```

2. 再复制一份测试，把 buffer 改成只删除一行的 `"alpha\ncharlie\ndelta\necho\n"`，断言 `changed_row_counts() == (0, 1)`。

3. 运行 `cargo test -p buffer_diff test_hunk_sink_offsets_practice`。

**需要观察的现象**：两组计数与「新增几行、删除几行」的手算是否一致。

**预期结果**（待本地验证）：修改 + 插入场景为 `(1, 1)`（`CHARLIE` 计 1 行新增、`charlie` 计 1 行删除；`xray` 是纯插入只计入新增）；纯删除场景为 `(0, 1)`。由于 `added_rows`/`removed_rows` 全部经由 `diff_base_point_range`（或其 buffer 侧对应物）聚合而来，计数正确即说明预计算正确。

#### 4.4.5 小练习与答案

**练习 1**：既然 `Summary::Context` 只有主 buffer 快照，为什么不让它也带上 base Rope？

**答案**：SumTree 的上下文类型是整棵树共用的（`PendingHunk` 与 `InternalDiffHunk` 共享 `DiffHunkSummary`，u2-l2），带上 base Rope 意味着每次树的构建、合并、查询都要额外穿透一份基准文本的引用，且 base 文本在 diff 生命周期里会整体替换（`set_base_text`），树的摘要却要跨替换保持有效。预存 Point 区间把依赖限制在「创建那一刻的 base」，摘要层从此自洽，不用关心 base 后来变成什么。

**练习 2**：一个纯插入 hunk 的 `diff_base_point_range` 是什么？对 `removed_rows` 贡献多少？

**答案**：字节区间两端相同（如 `26..26`），`offset_to_point` 换算两次得到同一点（`Point(4,0)..Point(4,0)`），行差为 0，即对 `removed_rows` 贡献 0。这保证 `changed_row_counts()` 里减号一侧不会被插入操作污染。

**练习 3**：`removed_rows` 的减法为什么用 `saturating_sub` 而不是普通减法？

**答案**：防御性编程。正常数据下 `end.row >= start.row` 恒成立（区间非反向），但摘要数据会在树中反复合并、且 hunk 来自异步计算结果，用饱和减法保证万一出现异常数据也只会得到 0 而不是 panic——这与项目「避免可能 panic 的运算」的编码准则一致。

## 5. 综合实践

把三个模块串成一个「手算 vs 程序」的对照实验。base 仍用 4.2 的 5 行文本，buffer 一次性做三种改动（**相邻的删除 + 修改**，再加一个**纯插入**）：

```text
base:   alpha / bravo / charlie / delta / echo
buffer: alpha / CHARLIE / delta / xray / echo
```

**第一步，手算**（用 4.2 的偏移表 `[0, 6, 12, 20, 26, 31]`）：

1. 删除 `bravo`（行 1）与修改 `charlie`（行 2）**相邻**，diff 算法会合并为一个 hunk：`before 1..3, after 1..2`，字节区间 `6..20`（切出 `"bravo\ncharlie\n"`），buffer 行 `1..2`，状态 Modified，`diff_base_point_range` 为 `Point(1,0)..Point(3,0)`；
2. 插入 `xray`：`before 4..4, after 3..4`，字节区间 `26..26`，buffer 行 `3..4`，状态 Added；
3. `changed_row_counts()`：`added_rows = (2-1) + (4-3) = 2`，`removed_rows = (3-1) + 0 = 2`，即 `(2, 2)`。

**第二步，写测试核对**（示例代码，加进 `mod tests`）：

```rust
#[gpui::test]
async fn test_hunk_sink_capstone(cx: &mut gpui::TestAppContext) {
    let diff_base = "alpha\nbravo\ncharlie\ndelta\necho\n";
    let buffer_text = "alpha\nCHARLIE\ndelta\nxray\necho\n";
    let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.to_string(), cx);

    let mut observed = Vec::new();
    for hunk in diff.hunks_intersecting_range(
        Anchor::min_max_range_for_buffer(buffer.remote_id()),
        &buffer,
    ) {
        observed.push((
            hunk.range.clone(),
            hunk.diff_base_byte_range.clone(),
            diff_base[hunk.diff_base_byte_range.clone()].to_string(),
            hunk.status(),
        ));
    }
    println!("{:?}", observed);

    assert_hunks(
        diff.hunks_intersecting_range(
            Anchor::min_max_range_for_buffer(buffer.remote_id()),
            &buffer,
        ),
        &buffer,
        diff_base,
        &[
            (1..2, "bravo\ncharlie\n", "CHARLIE\n", DiffHunkStatus::modified_none()),
            (3..4, "", "xray\n", DiffHunkStatus::added_none()),
        ],
    );
    assert_eq!(diff.changed_row_counts(), (2, 2));
}
```

**第三步，运行并回答两个问题**（预期结果待本地验证）：

1. 打印的字节区间 `6..20` 与 `26..26` 是否与你查表的结果一致？
2. 为什么相邻的「删一行 + 改一行」是一个 hunk 而不是两个？（提示：diff 算法输出的是**连续的**变更区段，中间没有未变行作分隔时不可能拆开；这个现象在 u5-l1 的相邻改动测试里还会正式出现。）

若三处断言全部通过且打印与手算吻合，说明你已能独立走通「imara-diff 行号 → 偏移表 → 字节/锚点/Point 三套坐标 → SumTree 摘要」的整条链路。

## 6. 本讲小结

- `HunkSink` 是 imara-diff 与 SumTree 存储层之间的翻译官：`new` 建表、`process_change` 逐 hunk 换算、`finish` 交出成品，生命周期只覆盖一次 diff 计算。
- imara-diff 的 hunk 输出是 `before`（base 侧行区间）与 `after`（buffer 侧行区间）两个半开区间；纯插入时 `before` 为空、纯删除时 `after` 为空，与 `status()` 的三态判定一一对应。
- `compute_line_offsets` 是一张前缀和表：`offsets[i]` 为第 i 行起始字节偏移，任意行区间的字节范围一次查表相减即得；其正确性前提是 `lines()` 的 token 含行尾符（可用 `assert_hunks` 切出 `"two\n"` 的既有测试反证）。
- `process_change` 走三条独立通路：查表得 `diff_base_byte_range`、经 buffer 快照铸锚得 `buffer_range`（空区间两端同锚，保证 Added/Deleted 判定持久）、经 base Rope 的 `offset_to_point` 得 `diff_base_point_range`。
- `diff_base_point_range` 是为 SumTree 摘要预存的 base 侧行数：`Item::summary` 的上下文只有主 buffer 快照、拿不到 base Rope，所以必须在创建时就算好；它同时参与 `InternalDiffHunk` 的相等比较，影响 u3-l5 的增量变更通知。
- base 侧选字节偏移做存储坐标有直接回报：`usize` 的 `SeekTarget` 实现直接拿它做子树剪枝，支撑按 base 范围的 hunk 查询。

## 7. 下一步学习建议

下一讲 u3-l3《词级差异：word_diff_ranges 与设置开关》将钻进本讲刻意绕开的 word diff 分支：`build_diff_options` 如何读取 `word_diff_enabled` 设置、`MAX_WORD_DIFF_LINE_COUNT = 5` 与「两侧行数相等」的触发条件、以及 `base_word_diffs` 存相对偏移而 `buffer_word_diffs` 存锚点的对称设计。建议在继续之前：

1. 重读 [buffer_diff.rs:L1294-L1330](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1294-L1330)，找出它消费了 `process_change` 前半段的哪两个产物；
2. 想一个问题：为什么词级差异的 buffer 侧结果要加上 `buffer_start_offset` 再铸锚（第 1317-1325 行），而 base 侧保持相对偏移不动？——答案会在下一讲展开。
