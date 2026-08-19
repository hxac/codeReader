# 第 2 讲：差异的语言：CharOperation 与 LineOperation 数据模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 `CharOperation` 三个变体（`Insert`/`Delete`/`Keep`）的语义，并解释为什么 `Insert` 携带 `String` 而 `Delete`/`Keep` 只带一个 `usize` 字节数。
2. 准确说出 `LineOperation` 三个变体的语义，解释它与 `CharOperation` "同构但单位不同" 体现在哪里（字节 vs 行、带内容 vs 只带数量、`usize` vs `u32`）。
3. **手工**把一串 `CharOperation` 应用到旧文本上得到新文本——不借助于运行代码，只用一张纸一支笔；对 `LineOperation` 也能做同样的事。
4. 读懂并会复用测试模块里的两个"官方解释器" `apply_char_operations` 与 `apply_line_operations`，理解贯穿整个 crate 的验证链：操作序列必须能重建新文本。
5. 独立实现一个 `count_old_bytes(ops) -> usize` 辅助函数，并用 crate 自带的 `"Hello, world!"` 例子（预期 13 字节）断言它的正确性。

上一讲（u1-l1）我们回答了"这个 crate 是什么、怎么跑"。本讲进入它的**数据模型**——两种操作枚举是整个 crate 的"词汇表"，后面所有算法（打分矩阵、动态规划、行级折叠）说到底都是在生产这两类对象。先把词汇表吃透，后面的算法课才不会边读边猜。

## 2. 前置知识

### 2.1 把 diff 看成一门小语言：操作序列 + 解释器

一个 diff 算法的输出不是"一张对照表"，而是一串**指令**（操作序列，也叫编辑脚本 edit script）。把指令按顺序"执行"在旧文本上，就得到新文本。执行指令的程序就是**解释器**。

本 crate 里，`CharOperation` 和 `LineOperation` 是两门小语言的"语句"，而测试模块里的 `apply_char_operations` / `apply_line_operations` 就是这两门语言的参考解释器。**理解一门语言最快的方式就是读懂它的解释器**——这是本讲的方法论，也是实践任务的出发点。

### 2.2 字节、字符与 UTF-8：为什么"字符级"操作用字节计数

Rust 的 `String` 是 UTF-8 字节序列，UTF-8 是**变长编码**：ASCII 字符占 1 字节，中文、emoji 等占 2～4 字节。于是：

- `"Hello".len()` 是 5（字节），`"你好".len()` 是 **6**（字节），而 `"你好".chars().count()` 是 2（字符）；
- 对字符串做切片 `&s[a..b]` 时，`a`、`b` 是**字节下标**，且必须恰好落在字符边界上，否则直接 panic。

所以"字符级差异"里计量的 `bytes: usize` 指的是 **UTF-8 字节数**。后面会看到，算法内部按**字符**比较、输出时按**字节**计量，两套单位在 `len_utf8()` 处完成换算。

### 2.3 行的定义：以 `\n` 切分

本 crate 对"行"的定义非常朴素：**用 `\n` 分隔的文本片段**（与测试中 `str::split('\n')` 的语义一致）：

- `"aaaa\nbbbb"` 按行切分是 `["aaaa", "bbbb"]`，共 **2 行**；
- `"aaaa\nbbbb\n"` 切分是 `["aaaa", "bbbb", ""]`，共 **3 行**——末尾的换行符会产生一个**空尾行**。

这个细节看似琐碎，却是理解 `LineDiff` 某些"怪异"输出（比如删掉第二行却多出一个 `Insert`）的钥匙，4.2 节会专门用到。

### 2.4 承接上一讲：rope 与 Point

u1-l1 说过：`LineDiff` 借用 `rope` crate 的 `Point`（行列坐标，`row`/`column` 都是 `u32`）做字节偏移与行列的换算。本讲只需要记住一个推论：`LineOperation` 里的行数用 `u32`，正是因为它要和 `Point` 的行号类型（以及 `LineDiff` 内部 `BTreeSet<u32>` 行号集合）对齐；而 `CharOperation` 用 `usize`，是因为它直接参与 `String` 的字节切片，`usize` 是 Rust 字符串长度的天然类型。

## 3. 本讲源码地图

本讲涉及的关键源码（全部在同一个文件里，行号以本讲 HEAD `4c72447` 为准）：

| 位置（`crates/streaming_diff/src/streaming_diff.rs`） | 内容 | 在本讲中的角色 |
| --- | --- | --- |
| [L106-L111](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L106-L111) | `pub enum CharOperation` | 模块一主角：字符级操作的词汇表 |
| [L281-L286](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L281-L286) | `pub enum LineOperation` | 模块二主角：行级操作的词汇表 |
| [L1104-L1124](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) | `tests::apply_char_operations` | CharOperation 语言的"参考解释器" |
| [L1007-L1035](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1007-L1035) | `tests::apply_line_operations` | LineOperation 语言的"参考解释器" |
| [L1037-L1050](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1037-L1050) | `test_apply_char_operations` | `"Hello, world!"` 例子，实践任务的依据 |
| [L530-L923](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L530-L923) | 14 个手写行为测试 | `LineDiff` 的"行为规格说明书"，本讲取用其中几个 |
| [L953-L961](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L953-L961) | `tests::char_ops_to_line_ops` | 两种语言之间的"翻译器" |
| [L925-L951](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L925-L951) | `test_random_diffs` | 随机化双重验证，模块三的核心证据 |

说明：`apply_char_operations`、`apply_line_operations`、`char_ops_to_line_ops` 都定义在 `#[cfg(test)] mod tests` 内部，**不是公开 API**。它们是 crate 作者自己用来验证正确性的"试金石"，对我们读者则是理解语义的最佳教材。

## 4. 核心概念与源码讲解

本讲覆盖三个模块：**CharOperation**、**LineOperation**、以及把两者串起来的**验证链（apply_char_operations / apply_line_operations）**。

### 4.1 模块一：CharOperation——字符级差异的三个动词

#### 4.1.1 概念说明

先看定义，一共只有 6 行：

```rust
#[derive(Debug, Clone)]
pub enum CharOperation {
    Insert { text: String },
    Delete { bytes: usize },
    Keep { bytes: usize },
}
```

三个变体就是三个动词，描述"如何从旧文本走到新文本"：

| 变体 | 语义 | 载荷 | 载荷为什么是这个 |
| --- | --- | --- | --- |
| `Insert { text }` | 在当前位置**写入**一段全新内容 | 被插入的文本本身（`String`） | 插入的内容**在旧文本里不存在**，不随操作携带就无处可取 |
| `Delete { bytes }` | 从旧文本**跳过** `bytes` 个字节，不带入结果 | 只有长度 | 被删的内容就在旧文本里，顺序消费时游标已经指向它，携带是纯冗余 |
| `Keep { bytes }` | 把旧文本接下来 `bytes` 个字节**原样复制**到结果 | 只有长度 | 同上，保留的内容来自旧文本本身 |

"Insert 带内容、Delete/Keep 只带长度"还有一层**流式场景**的原因：`CharOperation` 诞生于 `push_new` 的执行过程中（见 4.1.3），那时**新文本还没有完整到达**——插入的内容此刻只存在于这条操作里，必须随操作一起"逃逸"出去；而删除/保留引用的是早已固定的旧文本，一个字节数就够了。

两个容易忽略的细节：

1. **"字符级"的准确含义是"按字符对齐、按字节计量"。** 算法内部用 `Vec<char>` 逐字符比较；产出操作时用 `len_utf8()` 把字符数换算成字节数（见 4.1.3 的 `backtrack` 引用）。这样消费者可以直接用字节切片 `&old[a..b]` 取内容，不用再做字符换算。
2. **派生只有 `Debug, Clone`，没有 `PartialEq`。** 因为 crate 从不直接比较两个字符操作序列是否相等——同一对文本可以有很多个同样正确的编辑脚本，正确的判据是"应用后能否重建新文本"（行为等价），而不是"序列逐项相等"（结构等价）。对比之下 `LineOperation` 派生了 `PartialEq`，因为测试里确实直接 `assert_eq!` 行操作序列（见 4.2.1）。

#### 4.1.2 核心流程

`apply_char_operations` 的执行逻辑用伪代码表达（这就是"解释器"的全部）：

```text
输入: old_text, ops
状态: result = ""（输出缓冲）, old_ix = 0（旧文本读游标）
for op in ops:
    Keep{bytes}   → result += old_text[old_ix .. old_ix+bytes]; old_ix += bytes
    Delete{bytes} → old_ix += bytes          # 只动游标，不产出
    Insert{text}  → result += text           # 只产出，不动游标
返回 result
```

注意游标的分工：`Keep`/`Delete` 消耗**旧文本**（推进 `old_ix`），`Insert` 产出**新内容**（不碰旧文本）。由此立刻得到两条不变量——只要操作序列来自一次完整的 diff（所有 `push_new` 返回值按序拼上 `finish` 的返回值）：

\[ \sum_{\text{op} \in \text{ops}} \mathrm{old\_bytes}(\text{op}) \;=\; |\mathrm{old}| \]

其中 \(\mathrm{old\_bytes}(\text{Keep}\{n\}) = n\)，\(\mathrm{old\_bytes}(\text{Delete}\{n\}) = n\)，\(\mathrm{old\_bytes}(\text{Insert}\{\cdot\}) = 0\)。也就是说，**Keep 与 Delete 的字节数加起来恰好把旧文本从头到尾走一遍**，不多不少。以及：

\[ \mathrm{apply}(\mathrm{old}, \mathrm{ops}) = \mathrm{new} \]

这正是 u1-l1 提到的"核心不变量"在本讲的落地形式，也是实践任务里 `count_old_bytes` 函数要度量的东西。

#### 4.1.3 源码精读

**枚举定义：**

[streaming_diff.rs:L106-L111](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L106-L111) —— `CharOperation` 的全部定义。三个变体、两个字段名（`text` / `bytes`）、两个 derive，读完后你对这门"语言"的语法已经全部掌握。

**参考解释器：**

[streaming_diff.rs:L1104-L1124](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) —— `apply_char_operations`：与 4.1.2 的伪代码逐行对应。三个 match 分支各只有一到三行。注意 `Keep` 分支里的字节切片 `&old_text[old_ix..old_ix + bytes]`——若 `bytes` 不落在字符边界上，这一行会 panic；因此**操作序列的良构性（字节数总和、边界对齐）是生产者的责任**，解释器只管执行。

**配套测试（本讲实践的依据）：**

[streaming_diff.rs:L1037-L1050](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1037-L1050) —— `test_apply_char_operations`：旧文本 `"Hello, world!"`，操作序列 `[Keep{7}, Delete{5}, Insert{"Rust"}, Keep{1}]`，断言结果是 `"Hello, Rust!"`。手工执行一遍（请在纸上照着这张表走一遍，感受游标运动）：

| 步骤 | 操作 | `old_ix`（前） | 效果 | `result` 累积为 |
| --- | --- | --- | --- | --- |
| 1 | `Keep { bytes: 7 }` | 0 | 复制 `old[0..7]` = `"Hello, "` | `"Hello, "` |
| 2 | `Delete { bytes: 5 }` | 7 | 跳过 `old[7..12]` = `"world"` | `"Hello, "` |
| 3 | `Insert { text: "Rust" }` | 12 | 追加 `"Rust"` | `"Hello, Rust"` |
| 4 | `Keep { bytes: 1 }` | 12 | 复制 `old[12..13]` = `"!"` | `"Hello, Rust!"` |

验证不变量：\(\mathrm{old\_bytes}\) 总和 \(= 7 + 5 + 1 = 13 =\) `"Hello, world!".len()` ✓。

**生产者在哪里（为下一单元铺垫，本讲只认门牌）：**

[streaming_diff.rs:L149](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149) —— `pub fn push_new(&mut self, text: &str) -> Vec<CharOperation>`：每喂进一块新文本，返回**这一块结算出**的字符操作。`Insert` 的 `text` 就是从这里的新块里切出来的。

[streaming_diff.rs:L248](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L248) —— `let char_len = self.old[i - 1].len_utf8();`：这是"按字符比较、按字节计量"的换算点。回溯每走过一个旧字符，就把它换算成 UTF-8 字节数累加进 `Delete`/`Keep`。

[streaming_diff.rs:L272](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L272) —— `hunks.reverse();`：回溯是从终点**倒着**走向起点的，最后反转一次，保证消费者拿到的操作序列是**从左到右的文本顺序**——否则 `apply_char_operations` 这类解释器就没法顺序执行了。

#### 4.1.4 代码实践：实现 count_old_bytes

本讲规定的实践任务（可直接照做）：

1. **实践目标**：亲手实现并验证"Keep+Delete 字节数 = 旧文本长度"这条不变量的度量函数 `count_old_bytes`，加深对三个变体载荷的理解。

2. **操作步骤**：
   - 在本地仓库新建目录与文件 `crates/streaming_diff/tests/u1_l2_practice.rs`（这是 crate 的**集成测试**目录；不修改任何现有源码文件，练习完删掉即可，请勿提交）。`CharOperation` 是公开类型，集成测试可以直接 `use streaming_diff::CharOperation;`。
   - 写入以下内容（`count_old_bytes` 是**本讲示例代码**，仿照 [apply_char_operations](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) 的 match 结构写的）：

     ```rust
     use streaming_diff::CharOperation;

     fn count_old_bytes(ops: &[CharOperation]) -> usize {
         ops.iter()
             .map(|op| match op {
                 CharOperation::Keep { bytes } | CharOperation::Delete { bytes } => *bytes,
                 CharOperation::Insert { .. } => 0,
             })
             .sum()
     }

     #[test]
     fn test_count_old_bytes_hello_world() {
         // 操作序列取自 crate 自带的 test_apply_char_operations
         let char_ops = vec![
             CharOperation::Keep { bytes: 7 },
             CharOperation::Delete { bytes: 5 },
             CharOperation::Insert { text: "Rust".to_string() },
             CharOperation::Keep { bytes: 1 },
         ];
         assert_eq!(count_old_bytes(&char_ops), 13);
         // 同时断言它恰好等于旧文本的字节数
         assert_eq!(count_old_bytes(&char_ops), "Hello, world!".len());
     }
     ```
   - 在 **Zed 仓库根目录**执行：

     ```bash
     cargo test -p streaming_diff --test u1_l2_practice
     ```

3. **需要观察的现象**：测试通过；`Insert` 分支返回 0，不参与旧文本计数。另注意：从此 `cargo test -p streaming_diff` 会多跑这一个集成测试（原先只有 16 个单元测试）。

4. **预期结果**：`test_count_old_bytes_hello_world ... ok`，总计 1 passed。我未在本地运行过，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：手工构造一组 `CharOperation`，把 `"foo bar"` 变成 `"foo baz"`（要求包含至少一个 `Keep`）。

**答案**：两串文本的公共前缀是 `"foo ba"`（6 字节），差异只在最后一个字符。取 `[Keep { bytes: 6 }, Delete { bytes: 1 }, Insert { text: "z".into() }]`。验证：\(\mathrm{old\_bytes} = 6 + 1 = 7 =\) `"foo bar".len()` ✓；应用结果 `foo ba` + `z` = `"foo baz"` ✓。

**练习 2**：`Delete` 为什么不携带被删的文本？携带了会有什么坏处？

**答案**：被删的内容就在旧文本里，且解释器顺序执行时游标已经指向它，只需一个长度即可定位；携带文本是冗余信息，白白增大每条操作的内存占用——流式场景下操作数量很大。更根本地，`Insert` 携带内容是**被迫的**（新文本当时不完整，内容无处可取），`Delete`/`Keep` 则**没必要**。

**练习 3**：`"你好,世界"` 的 `len()` 是多少？若一条 `Keep` 恰好保留其中的 `"你"` 一个字符，`bytes` 应该是多少？

**答案**：`"你"`、`"好"`、`"世"`、`"界"` 各 3 字节，逗号是 ASCII 1 字节，共 \(3+3+1+3+3 = 13\) 字节（`len()` 返回 13，`chars().count()` 返回 5）。保留单个 `"你"` 的 `Keep` 其 `bytes` 为 **3**。这也解释了为什么 `random_streaming_diff` 在切块时要专门把块边界对齐到字符边界（[L969-L972](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L969-L972)）——否则切出一半的多字节字符会破坏 `&str` 的合法性。

### 4.2 模块二：LineOperation——行级差异的同构动词

#### 4.2.1 概念说明

```rust
#[derive(Debug, Clone, PartialEq)]
pub enum LineOperation {
    Insert { lines: u32 },
    Delete { lines: u32 },
    Keep { lines: u32 },
}
```

它与 `CharOperation` **同构**：同样的三个动词，同样的"数量"载荷风格。差异全在"单位"与"信息含量"上：

| 对比维度 | `CharOperation` | `LineOperation` |
| --- | --- | --- |
| 计量单位 | UTF-8 字节（`usize`） | 行数（`u32`） |
| `Insert` 载荷 | `text: String`（带内容） | `lines: u32`（**不带内容**） |
| 载荷类型来源 | `usize` 配合 `String` 字节切片 | `u32` 配合 `rope::Point` 的行号 |
| 生产者 | `StreamingDiff::push_new` / `finish` | `LineDiff::line_operations` |
| 典型消费者 | 调用方把补丁写回编辑器缓冲区 | UI 按行渲染 diff 高亮 |
| derive | `Debug, Clone` | `Debug, Clone, PartialEq` |

两个关键问题：

**为什么 `LineOperation::Insert` 不带文本？** 因为行级操作的消费者（例如 `agent_ui` 的 diff 呈现层）手里**同时拥有完整的旧文本与新文本**——行级差异是在事实尘埃落定之后折叠出来的。它的解释器签名因此多了一个参数：`apply_line_operations(old_text, new_text, line_ops)`，插入的行内容按行号去 `new_text` 里取即可。对比 `CharOperation::Insert`：它诞生于流式过程中，当时新文本不完整，内容必须随操作携带。**两种 Insert 的载荷差异，本质是"产出时新文本是否完整"的差异。**

**为什么 derive 多了 `PartialEq`？** 因为手写测试直接把 `LineDiff` 的输出与期望的行操作序列做 `assert_eq!`（例如 [L537-L548](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L537-L548)），编译期就要求相等比较；而字符操作从不做结构化比较（4.1.1 已解释原因）。

还要建立一个直觉：**行是粗粒度的"全有或全无"单位**。只要一行内任何一个字符变了，这一行在行级语言里就只能表达为"删一行 + 插一行"，不存在"改半行"。

#### 4.2.2 核心流程

`apply_line_operations` 的解释器伪代码——注意它维护**两个**游标（旧行游标 `old_start`、新行游标 `new_start`），而字符级解释器只有一个旧文本游标：

```text
输入: old_text, new_text, line_ops
预处理: old_lines = old_text 按 \n 切分; new_lines = new_text 按 \n 切分
状态: result = []; old_start = 0; new_start = 0
for op in line_ops:
    Keep{lines}   → result += old_lines[old_start .. old_start+lines]
                    old_start += lines; new_start += lines
    Delete{lines} → old_start += lines
    Insert{lines} → result += new_lines[new_start .. new_start+lines]
                    new_start += lines
返回 result 用 \n 重新拼接
```

于是行数版守恒律成立（\(K\)、\(D\)、\(I\) 分别是序列中 Keep/Delete/Insert 的行数总和）：

\[ K + D = L_{\mathrm{old}}, \qquad K + I = L_{\mathrm{new}} \]

即 Keep+Delete 恰好覆盖旧文本所有行，Keep+Insert 恰好覆盖新文本所有行。

#### 4.2.3 源码精读

**枚举定义：**

[streaming_diff.rs:L281-L286](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L281-L286) —— `LineOperation` 的全部定义，与 4.1.3 的 `CharOperation` 并排对照阅读，"同构"一词不言自明。

**参考解释器：**

[streaming_diff.rs:L1007-L1035](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1007-L1035) —— `apply_line_operations`。关键片段：

```rust
let old_lines: Vec<&str> = old_text.split('\n').collect();
let new_lines: Vec<&str> = new_text.split('\n').collect();
...
LineOperation::Keep { lines } => {
    let old_end = old_start + *lines as usize;
    result.extend(&old_lines[old_start..old_end]);   // 内容取自旧文本
    old_start = old_end;
    new_start += *lines as usize;                     // 双游标同步前进
}
LineOperation::Insert { lines } => {
    let new_end = new_start + *lines as usize;
    result.extend(&new_lines[new_start..new_end]);   // 内容取自新文本
    new_start = new_end;
}
```

`Delete` 分支只有一行 `old_start += *lines as usize;`（只跳过、不产出）。最后 `result.join("\n")` 把行重新拼回文本——`split`/`join` 互为逆操作，拼接是无损的。

**行为规格：整行替换的标准形状。**

[streaming_diff.rs:L622-L648](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L622-L648) —— `test_replace_line`：旧文本 `"aaaa\nbbbb\ncccc"`（3 行），字符操作 `[Keep{5}, Delete{4}, Insert{"BBBB"}, Keep{5}]`（第二行整行被换），断言行操作恰为：

```rust
vec![
    LineOperation::Keep { lines: 1 },    // "aaaa" 原样保留
    LineOperation::Delete { lines: 1 },  // "bbbb" 被删
    LineOperation::Insert { lines: 1 },  // 插入新行 "BBBB"
    LineOperation::Keep { lines: 1 },    // "cccc" 原样保留
]
```

验证守恒律：\(K+D = 2+1 = 3 = L_{\mathrm{old}}\) ✓；\(K+I = 2+1 = 3 = L_{\mathrm{new}}\) ✓。

**行为规格：行内一字之差也要"整行换"。**

[streaming_diff.rs:L680-L702](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L680-L702) —— `test_edit_at_end_of_line`：旧文本仍是 `"aaaa\nbbbb\ncccc"`，字符操作只是在第一行末尾插一个字符：`[Keep{4}, Insert{"A"}, Keep{10}]`（没碰任何换行符）。行操作却是 `[Delete{1}, Insert{1}, Keep{2}]`——第一行整行删除、插入修改后的 `"aaaaA"`。这就是 4.2.1 说的"行是全有或全无的单位"。

**行为规格：空尾行的微妙之处。**

[streaming_diff.rs:L551-L572](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L551-L572) —— `test_delete_second_of_two_lines`：旧文本 `"aaaa\nbbbb"`，字符操作 `[Keep{5}, Delete{4}]`，新文本是 `"aaaa\n"`（注意末尾换行还在）。直觉上行操作似乎是 `[Keep{1}, Delete{1}]`，实际断言却是：

```rust
vec![
    LineOperation::Keep { lines: 1 },
    LineOperation::Delete { lines: 1 },
    LineOperation::Insert { lines: 1 }   // 多出来的！
]
```

原因：`"aaaa\n"` 按 `\n` 切分是 `["aaaa", ""]` 共 **2 行**（2.3 节的空尾行），而 `Delete` 只跳过旧行、**不能凭空造出新行**。要重建那个空尾行，只能 `Insert{1}` 从新文本取出空串。守恒律：\(K+D = 1+1 = 2 = L_{\mathrm{old}}\) ✓，\(K+I = 1+1 = 2 = L_{\mathrm{new}}\) ✓——多出来的 `Insert` 正是守恒律要求的。

**生产者门牌（细节留待第四单元）：**

[streaming_diff.rs:L462-L513](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L462-L513) —— `LineDiff::line_operations`：用两个 `peekable` 迭代器归并 `deleted_rows` 与 `inserted_rows` 两个有序行号集合，产出 `Vec<LineOperation>`。本讲只需知道它是行级语言唯一的"说话人"。

#### 4.2.4 代码实践：手工执行行级解释器

1. **实践目标**：不运行任何代码，用纸笔把一串行操作"应用"到文本上，直到能脱口说出每条操作对两个游标的影响。

2. **操作步骤**：
   - 取 `test_edit_at_end_of_line` 的素材：`old = "aaaa\nbbbb\ncccc"`，`new = "aaaaA\nbbbb\ncccc"`，`line_ops = [Delete{1}, Insert{1}, Keep{2}]`。
   - 先写出 `old_lines` 与 `new_lines`（各 3 个元素），画一张追踪表，逐行记录 `old_start`、`new_start`、`result` 的变化。

3. **需要观察的现象**：`Delete` 只推进旧行游标；`Insert` 只推进新行游标且内容来自 `new_lines`；`Keep` 让两个游标同步前进。

4. **预期结果**：你的表格应收敛到如下轨迹（`result` 用行列表达）：

   | 步骤 | 操作 | old_start | new_start | result |
   | --- | --- | --- | --- | --- |
   | 初始 | — | 0 | 0 | `[]` |
   | 1 | `Delete { 1 }` | 1 | 0 | `[]`（跳过旧行 `"aaaa"`） |
   | 2 | `Insert { 1 }` | 1 | 1 | `["aaaaA"]`（取新行 0） |
   | 3 | `Keep { 2 }` | 3 | 3 | `["aaaaA", "bbbb", "cccc"]`（取旧行 1、2） |

   拼接得 `"aaaaA\nbbbb\ncccc"` = `new` ✓，与 [L697-L701](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L697-L701) 的断言一致。若中途卡住，对照 [apply_line_operations](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1007-L1035) 的对应分支检查。

#### 4.2.5 小练习与答案

**练习 1**：旧文本 `"aaaa\nbbbb"`、新文本 `"aaaa"`（删掉了第二行**连同它前面的换行符**）。行操作应该是 `[Keep{1}, Delete{1}]` 吗？

**答案**：不是。`"aaaa"` 切分后只有 1 行，若取 `[Keep{1}, Delete{1}]`，则 \(K+I = 1 \ne L_{\mathrm{new}} = 1\)……恰好相等？注意守恒律本身不排除 `[Keep{1}, Delete{1}]`（\(K+D=2\)、\(K+I=1\) 都成立），且应用结果 `["aaaa"]` 也确实等于新文本——所以这组操作其实是**合法解**。但它要求删掉的 5 个字节（`\nbbbb`）是从旧文本第 1 行行尾跨到第 2 行的，`LineDiff` 的实际折叠结果以 [L551-L572](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L551-L572) 的镜像情形为准（那边因为空尾行多出一个 `Insert`）。这道题的教训是：**同一对文本可以有多组合法的行操作**，守恒律 + 可重建性才是判据，具体输出形状由 `LineDiff` 的折叠规则决定。

**练习 2**：为什么行内改动一个字符，会导致整行 `Delete` + `Insert`？

**答案**：`LineOperation` 的最小单位是整行，语言里没有"修改半行"的动词。要把"行内不同"表达出来，只能组合"删掉旧版本的这一行"和"插入新版本的这一行"，见 [test_edit_at_end_of_line](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L680-L702)。这也是所有行级 diff（如 `git diff` 的 `-$/+` 行）的共同取舍。

**练习 3**：`LineOperation` 的行数字段为什么是 `u32` 而不是像 `CharOperation` 那样用 `usize`？

**答案**：行级世界与 `rope::Point` 对齐——`Point` 的 `row` 字段是 `u32`，`LineDiff` 内部的 `deleted_rows`/`inserted_rows` 也是 `BTreeSet<u32>`（[L296-L298](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L296-L298)），输出自然沿用同一类型。而字节级世界与 `String`/`str` 的长度与切片类型 `usize` 对齐。类型选择反映的是各自要互操作的坐标系。

### 4.3 模块三：验证链——apply 双解释器如何证明"差异可重建"

#### 4.3.1 概念说明

u1-l1 画过那张两段式结构图：`StreamingDiff` 生产 `CharOperation`，`LineDiff` 消费 `CharOperation`、生产 `LineOperation`。本讲把这条流水线补上最后一环——**验收**：

```text
old ──▶ StreamingDiff ──▶ char_ops ──▶ LineDiff ──▶ line_ops
              │                              │
              ▼                              ▼
   apply_char_operations(old, char_ops)   apply_line_operations(old, new, line_ops)
              │                              │
              └──────────┬───────────────────┘
                         ▼
                  两者都必须 == new
```

这条**双重 round-trip（往返）验证链**是整个 crate 测试体系的骨架：

- 第一重验证字符级语言的正确性：解释器只拿 `old` 就能重建 `new`（`Insert` 自带内容，所以不需要 `new`）；
- 第二重验证行级折叠的正确性：解释器拿 `old` 与 `new` 能重建 `new`——看似"作弊"（用了 `new`），其实只从 `new` 里取**被插入的行**，结构信息（哪些行删、哪些行插、哪些行留）完全来自 `line_ops` 本身，因此仍然是非平凡的验证。

#### 4.3.2 核心流程

把字符操作翻译成行操作的流程（`char_ops_to_line_ops`）：

```text
1. old_text 转 Rope（LineDiff 的行坐标查询都走 rope）
2. LineDiff::default() 建立空状态
3. 逐条 push_char_operation(op, &old_rope)：喂字符操作，内部折叠行状态
4. diff.finish(&old_rope)：清空缓冲、推进游标到文本末尾
5. diff.line_operations()：从行号集合归并出行操作
```

随机化测试则把验证链自动化：

```text
随机生成 old → 随机编辑得 new → 随机分块流式推送（StreamingDiff）
    → 断言① apply_char_operations(old, char_ops) == new
    → 断言②③ char_ops_to_line_ops 得 line_ops 后
            apply_line_operations(old, new, line_ops) == new
```

#### 4.3.3 源码精读

**翻译器：**

[streaming_diff.rs:L953-L961](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L953-L961) —— `char_ops_to_line_ops` 的全部 8 行：`Rope::from` → 循环 `push_char_operation` → `finish` → `line_operations`。它同时演示了 `LineDiff` 公开 API 的标准用法。

[streaming_diff.rs:L305-L313](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L305-L313) —— `pub fn push_char_operations`：上面循环的公开封装版，接受任何产出 `&CharOperation` 的迭代器——这就是"字符级语言与行级语言松耦合"的接口证据。

**随机化验证：**

[streaming_diff.rs:L925-L951](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L925-L951) —— `test_random_diffs` 的主体：三段 `println!` 分别打印 old、new、char operations（配合 `-- --nocapture` 可见），随后正是 4.3.1 图中的两重断言（[L942-L943](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L942-L943) 与 [L946-L949](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L946-L949)）。

[streaming_diff.rs:L963-L981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L963-L981) —— `random_streaming_diff`：随机切块流式推送的实现。每次 `push_new` 返回的操作直接 `extend` 进总序列，最后再拼上 `diff.finish()` 的返回值——**总序列 = 各块返回值按序拼接**，这就是"任意分块都能重建"不变量的实验形态。切块时把边界对齐到字符边界（[L969-L972](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L969-L972)），呼应练习 4.1-3。

#### 4.3.4 代码实践：亲眼看看随机测试打印的操作序列

1. **实践目标**：从真实运行输出里"摸到"两种操作语言——看一眼算法真正吐出来的 `CharOperation` 与 `LineOperation` 长什么样，而不只是手写例子。

2. **操作步骤**：在 Zed 仓库根目录执行（参数调小让输出可读）：

   ```bash
   ITERATIONS=3 SEED=0 OLD_TEXT_LEN=12 \
     cargo test -p streaming_diff test_random_diffs -- --nocapture
   ```

   从 stdout 里挑一个 iteration，抄下它的 `old text`、`new text`、`char operations` 三行。

3. **需要观察的现象**：
   - `char operations` 是一长串 `Keep`/`Delete`/`Insert` 交替的序列，顺序与文本方向一致（4.1.3 讲过的 `hunks.reverse()` 的功劳）；
   - 随机文本含多字节字符时，`Keep`/`Delete` 的字节数会明显大于字符数（例如一个中文字符对应 `bytes: 3`）；
   - `line operations` 通常远短于 `char operations`——行级是"摘要"，字符级是"全文"。

4. **预期结果**：3 个 iteration 全部通过（`test result: ok`）。随后用你抄下的数据手工核对断言①：把 char operations 应用到 old 上，看是否得到 new。随机输出依赖种子与运行环境，具体序列**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`char_ops_to_line_ops` 里如果漏掉 `diff.finish(&old_rope)` 这一行，会出什么问题？

**答案**：`finish` 负责（a）把 `buffered_insert`/`buffered_delete` 两个缓冲清空结算、（b）把 `old_end` 游标推进到旧文本末尾（[L453-L460](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L453-L460)）。漏掉它，尾部还悬在缓冲里的插入/删除不会进入 `inserted_rows`/`deleted_rows`，`line_operations` 归并出的行操作会缺失，重建就会偏离 `new`。

**练习 2**：`test_random_diffs` 一共做了几层断言？各验证什么？

**答案**：两层三件事：① `apply_char_operations(old, char_ops) == new` 验证字符级操作可重建新文本；② 把 char_ops 经 `char_ops_to_line_ops` 折叠为 line_ops；③ `apply_line_operations(old, new, line_ops) == new` 验证行级折叠没有丢失结构信息。①是算法正确性，③是折叠正确性，互不涵盖。

**练习 3**：`apply_line_operations` 的参数里就有 `new_text`，用它重建出 `new_text`，算不算"循环论证"？

**答案**：不算。`new_text` 只被用来**取被插入行的内容**；"哪些行删、哪些行插、哪些行留、各多少行"这些结构决策全部来自 `line_ops`。如果折叠过程弄丢了哪怕一个空尾行（回想 4.2.3 的 `test_delete_second_of_two_lines`），游标就会错位、取错行或行数越界，断言立刻失败。所以它检验的是**结构信息是否无损**，与"用答案抄答案"有本质区别。

## 5. 综合实践

把本讲三个模块串成一个动手任务：**为一段真实的小代码编辑，手写字符操作 → 自证不变量 → 交给 crate 验证**。

设定：旧文本与目标新文本分别是

```text
old = "fn main() {\n    println!(\"Hello\");\n}"
new = "fn main() {\n    println!(\"Bonjour\");\n}"
```

**第一步：手写 `char_ops`。** 公共前缀是从开头到 `"Hello"` 之前的 `"`,`"` 之间的位置——逐段数字节：`fn main() {` 11 字节 + `\n` 1 + `    println!("` 14 = **26** 字节；被删的 `"Hello"` 5 字节；尾部 `");\n}` 5 字节。于是：

```rust
let ops = vec![
    CharOperation::Keep { bytes: 26 },
    CharOperation::Delete { bytes: 5 },
    CharOperation::Insert { text: "Bonjour".into() },
    CharOperation::Keep { bytes: 5 },
];
```

**第二步：纸面自证两条不变量。** \(\mathrm{old\_bytes} = 26+5+5 = 36 = |old|\) ✓；按 4.1.2 的表格手工 apply，应得到 `new` ✓（请真的画一遍表）。

**第三步：写进 4.1.4 创建的 `crates/streaming_diff/tests/u1_l2_practice.rs`（示例代码，仿写测试模块的两个解释器）：**

```rust
use rope::Rope;
use streaming_diff::{CharOperation, LineDiff};

// 仿写 tests::apply_char_operations（L1104-L1124），行为保持一致
fn apply_char_operations(old_text: &str, char_ops: &[CharOperation]) -> String {
    let mut result = String::new();
    let mut old_ix = 0;
    for operation in char_ops {
        match operation {
            CharOperation::Keep { bytes } => {
                result.push_str(&old_text[old_ix..old_ix + bytes]);
                old_ix += bytes;
            }
            CharOperation::Delete { bytes } => old_ix += bytes,
            CharOperation::Insert { text } => result.push_str(text),
        }
    }
    result
}

#[test]
fn test_handwritten_edit() {
    let old = "fn main() {\n    println!(\"Hello\");\n}";
    let new = "fn main() {\n    println!(\"Bonjour\");\n}";
    let ops = vec![ /* 第一步的四个操作 */ ];

    assert_eq!(count_old_bytes(&ops), old.len());          // 不变量一
    assert_eq!(apply_char_operations(old, &ops), new);     // 不变量二
}

#[test]
fn test_line_folding_roundtrip() {                          // 选做：接入真实 LineDiff
    let old = "fn main() {\n    println!(\"Hello\");\n}";
    let new = "fn main() {\n    println!(\"Bonjour\");\n}";
    let ops = vec![ /* 同上 */ ];

    let old_rope = Rope::from(old);
    let mut line_diff = LineDiff::default();
    line_diff.push_char_operations(&ops, &old_rope);
    line_diff.finish(&old_rope);
    let line_ops = line_diff.line_operations();

    // 行级 round-trip：行数守恒 K+D = 3（旧文本 3 行），K+I = 3（新文本 3 行）
    let kept_or_deleted: u32 = line_ops.iter().map(|op| match op {
        streaming_diff::LineOperation::Keep { lines }
        | streaming_diff::LineOperation::Delete { lines } => *lines,
        _ => 0,
    }).sum();
    assert_eq!(kept_or_deleted, 3);
}
```

说明：`rope` 在 `streaming_diff` 的 `[dependencies]` 里（[Cargo.toml:L14-L16](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L14-L16)），集成测试可以直接 `use rope::Rope;`。运行：

```bash
cargo test -p streaming_diff --test u1_l2_practice
```

**预期结果**：两个测试全部通过；`line_ops` 的具体形状按 4.2.3 `test_replace_line` 的同构情形推断应为 `[Keep{1}, Delete{1}, Insert{1}, Keep{1}]`（第 2 行整行替换），这一步**待本地验证**——把它也写成断言，若与推断不符，恭喜你发现了值得深挖的行為差异（那正是第四单元 `LineDiff` 状态机要解释的内容）。完成后删除练习文件，保持仓库干净。我未替你运行过以上命令，所有"预期"来自对源码的静态阅读。

## 6. 本讲小结

- `CharOperation` 是字符级差异语言的三个动词：`Insert` 携带 `String`（流式产出时新文本不完整，内容必须随操作逃逸），`Delete`/`Keep` 只带 `usize` 字节数（内容就在旧文本里，游标已定位）。
- "字符级"的准确含义是**按字符对齐比较、按字节计量输出**，换算点在 `backtrack` 的 `len_utf8()`；字节数必须落在 UTF-8 字符边界上，这是生产者的责任。
- 两条核心不变量：Keep+Delete 字节数之和 \(= |\mathrm{old}|\)；`apply_char_operations(old, ops) = new`。后者在任意分块的流式场景下都成立（各块返回值按序拼接 + `finish`）。
- `LineOperation` 与前者同构但单位是行（`u32`，对齐 `rope::Point`）；`Insert` 不带文本，因为消费者手里已有完整的新旧文本；行数守恒律为 \(K+D = L_{old}\)、\(K+I = L_{new}\)。
- 行是"全有或全无"的单位：行内一字之差也会折叠成整行 Delete+Insert；空尾行（`"a\nb\n"` 是 3 行）解释了若干看似多余的 `Insert`。
- `apply_char_operations` / `apply_line_operations` 是两门语言的参考解释器，与 `char_ops_to_line_ops`、`test_random_diffs` 共同构成贯穿全 crate 的双重 round-trip 验证链。

## 7. 下一步学习建议

本讲搞定了"词汇表"——算法**输出**什么。下一单元进入"语法机"——算法**如何决定**输出：

- 下一讲 **u2-l1《列主序打分矩阵：Matrix 的存储与操作》**：拆解私有结构 `Matrix`——为什么用一维 `Vec<f64>` 做**列主序**存储、`resize` 如何在流式过程中复用容量、`swap_columns` 为什么要用 `unsafe` 的指针交换。它是动态规划的"草稿纸"。
- 再下一讲 **u2-l2《打分模型：为什么删除比插入贵 20 倍》**：解读 `INSERTION_SCORE = -1`、`DELETION_SCORE = -20`、`EQUALITY_BASE = 1.8` 这组常量如何让 diff 偏向"保留旧文本 + 插入新内容"，从而匹配 LLM 流式输出的场景。

在进入下一讲之前，建议做两个热身：

1. 重读 [streaming_diff.rs:L110-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L110-L128)，注意 `StreamingDiff` 的字段里 `old`/`new` 是 `Vec<char>`（按字符），而操作输出按字节——想一想这两套单位各自服务于谁；
2. 把 5 节综合实践里手写的 `ops` 喂给 `StreamingDiff`（用 `new` + 分块 `push_new` + `finish` 跑一遍，对比它的输出与你手写的序列）：多半不完全相同——同一切换有多组合法编辑脚本，而算法通过打分从中挑一个。**为什么打分会偏好某一个**，正是第二单元的主题。
