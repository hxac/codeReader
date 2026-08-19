# 列主序打分矩阵：Matrix 的存储与操作

## 1. 本讲目标

第一讲里我们提到过，`StreamingDiff` 内部有一块"私有打分矩阵 `Matrix`"，它是整个流式 diff 算法的地基。本讲就把这块地基挖开看清楚。学完本讲，你应该能够：

1. 解释 `Matrix` 为什么用一维 `Vec<f64>` 存二维表格，以及 `cells[col * rows + row]` 这个列主序下标换算是怎么来的。
2. 说明 `resize` 扩容时"旧值原位保留、新列自动补零"的语义成立的前提，以及 `get`/`set` 如何用 panic 防护越界访问。
3. 读懂 `swap_columns` 为什么必须借助 `unsafe` 的 `as_mut_ptr` + `swap_nonoverlapping` 来交换两列，并能论证这段 unsafe 代码为什么是安全的。
4. 读懂 `adjacent_columns_mut` 如何用一次 `split_at_mut` 同时借出"上一列（只读）+ 当前列（可写）"，以及它为什么是 Rust 借用检查器友好写法。

本讲只讲数据结构本身，不涉及分数怎么算——打分常量是下一讲（u2-l2）的内容，填表主循环是单元三（u3-l2）的内容。

## 2. 前置知识

本讲是 beginner 级别，只需具备以下概念（用大白话解释）：

- **动态规划打分矩阵（直觉版）**：上一讲我们说 diff 的结果是 `CharOperation` 序列。要找出"最好"的序列，经典做法是填一张二维表：行对应旧文本的前缀，列对应新文本的前缀，每个格子存"走到这里的最佳累计分"。本讲的 `Matrix` 就是这张表。你暂时不需要懂格子里的分数含义，只需要把它当成"一张二维表格"。
- **`Vec<T>`**：Rust 中最常用的可增长数组，元素在堆上连续排列。`Vec::resize(new_len, fill)` 的语义是：长度变大时在**尾部**追加 `fill` 副本（容量够则不重新分配），长度变小时从**尾部**截断。
- **行主序 vs 列主序**：把二维表压平成一维数组有两种常见方式。行主序（C 语言二维数组）把同一**行**的元素放在一起，下标换算是 `row * cols + col`；列主序（Fortran、NumPy 的 order='F'）把同一**列**的元素放在一起，下标换算是 `col * rows + row`。`Matrix` 采用列主序。
- **Rust 借用规则**：同一时刻，一块数据要么有任意多个只读引用（`&T`），要么只有一个可读写引用（`&mut T`），不能混用。这个规则让编译器在编译期就排除数据竞争，但也让"同时读 A 段、写 B 段（A、B 同属一个 Vec）"这类需求需要专门的工具。
- **`split_at_mut`**：标准库提供的"安全切一刀"工具，把一个可变切片按某个下标分成前后两段**互不重叠**的可变切片，返回 `(&mut [T], &mut [T])`。这是绕开"不能同时拿两个 `&mut`"的官方姿势。
- **`unsafe` 与裸指针**：`unsafe` 块内可以执行编译器无法自动验证安全性的操作（如裸指针算术）。本讲会遇到 `as_mut_ptr()`（拿到 Vec 缓冲区的裸指针）和 `std::ptr::swap_nonoverlapping`（逐元素交换两段**互不重叠**的内存）。unsafe 并不意味着"一定出错"，而是"安全性由程序员向编译器担保"；担保条件会写在文档里，我们要学会逐条核对。
- **f64**：双精度浮点数。本 crate 用浮点数给编辑路径打分（有奖励有惩罚，方便做指数衰减/增长），这就是 `cells` 元素类型是 `f64` 的原因。

## 3. 本讲源码地图

`streaming_diff` 是单文件 crate，库根就是下面这一个文件。本讲关注的代码段全部位于文件开头的 L10–L104，外加 `StreamingDiff` 中对它的三处使用。

| 代码段 | 位置 | 作用 |
| --- | --- | --- |
| `Matrix` 结构体 | [src/streaming_diff.rs:L10-L15](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L10-L15) | 一维 `Vec<f64>` + 行列数的紧凑二维表 |
| `Matrix::new` | [src/streaming_diff.rs:L17-L24](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L17-L24) | 构造 0×0 空矩阵 |
| `Matrix::resize` | [src/streaming_diff.rs:L26-L30](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L26-L30) | 调整尺寸；扩容时尾部补零 |
| `Matrix::swap_columns` | [src/streaming_diff.rs:L32-L53](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L32-L53) | 用裸指针整列交换（全 crate 唯一的 unsafe 块） |
| `Matrix::get` / `Matrix::set` | [src/streaming_diff.rs:L55-L76](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L55-L76) | 带越界 panic 防护的读写 |
| `Matrix::adjacent_columns_mut` | [src/streaming_diff.rs:L78-L90](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L78-L90) | 一次 `split_at_mut` 借出相邻两列 |
| `Debug for Matrix` | [src/streaming_diff.rs:L93-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L93-L104) | 按行列印矩阵，便于调试观察 |
| 使用处 1：构造初始化 | [src/streaming_diff.rs:L130-L147](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L130-L147) | `StreamingDiff::new` 里 resize + 逐行 set 第 0 列 |
| 使用处 2：滚动扩容 | [src/streaming_diff.rs:L149-L162](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149-L162) | `push_new` 里 swap + resize + `adjacent_columns_mut` |
| 使用处 3：读终点分数 | [src/streaming_diff.rs:L184-L199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L199) | 用 `get` 扫最后一列找最优终点 |

（crate 的其余部分——`CharOperation`/`LineOperation`/`LineDiff` 与测试模块——已在 u1-l2 讲过或留待单元四。）

## 4. 核心概念与源码讲解

### 4.1 Matrix：一维数组扮演的二维表

#### 4.1.1 概念说明

`Matrix` 是一个私有的辅助结构体，解决的问题是：**Rust 没有内置的"可增长二维数组"**，而流式 diff 需要一张"列数会不断增长"的打分表。与其用 `Vec<Vec<f64>>`（每列单独分配、内存不连续、扩容时要逐列搬移），不如用一个一维 `Vec<f64>` 加两个尺寸字段自己算下标。

它选择的是**列主序**布局：第 `col` 列的 `rows` 个元素在内存中连续存放。下标换算公式为：

\[
\text{index}(row, col) = col \times rows + row
\]

为什么选列主序而不是更常见的行主序？因为这个动态规划是**按列填表**的——新文本每多到一个字符，就多填一列（详见 u3-l2）。列主序让"一列"恰好是一段连续内存，带来三个直接好处：

1. **缓存友好**：填表时反复顺序扫描一整列。
2. **`resize` 语义天然对齐**：在尾部追加元素 = 在右侧新增整列（4.2 节）。
3. **整列操作可以用指针算术完成**：交换两列、切出相邻两列都变成"对一段连续内存的操作"（4.3、4.4 节）。

在 `StreamingDiff` 的语境里，行下标 `i` 表示"旧文本的前 `i` 个字符"，列下标 `j` 表示"本轮新文本块的前 `j` 个字符"，`scores[i][j]` 是该子问题的最优累计分。本讲只把它当二维表看即可。

#### 4.1.2 核心流程

以 `rows = 3, cols = 2` 为例，平铺后的 `cells` 长度为 \(3 \times 2 = 6\)，布局如下：

```text
逻辑视图                    内存布局（cells 下标 → 元素）
  col=0  col=1             0:(0,0)  1:(1,0)  2:(2,0)  3:(0,1)  4:(1,1)  5:(2,1)
row=0  ┌─────┬─────┐       └── 第 0 列连续 ──┘└── 第 1 列连续 ──┘
row=1  │     │     │
row=2  └─────┴─────┘
```

读写流程（`get` / `set`）：

1. 检查 `row < rows`，否则 panic（`"row out of bounds"`）。
2. 检查 `col < cols`，否则 panic（`"column out of bounds"`）。
3. 按公式定位元素：读返回 `cells[col * rows + row]`，写给它赋值。

两道边界检查把"越界索引"这种未定义行为变成了带明确消息的程序崩溃——这是 Rust 里防御式编程的常见取舍（注意：直接对 `Vec` 做越界索引本身也会 panic，但消息不含行列语义；这里的检查给出了更可诊断的信息）。

#### 4.1.3 源码精读

结构体定义只有三个字段（[src/streaming_diff.rs:L10-L15](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L10-L15)）：数据 `cells`、行数 `rows`、列数 `cols`。`#[derive(Default)]` 让全零值可用（空 Vec + 两个 0），`new()` 则显式写出同样的初始状态（[src/streaming_diff.rs:L17-L24](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L17-L24)）。

`get` 的关键一行（[src/streaming_diff.rs:L55-L64](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L55-L64)）：

```rust
self.cells[col * self.rows + row]
```

这就是列主序换算的本体：先乘列号得到该列的起点，再加行号偏移。`set` 与之完全对称，只是把读取换成赋值（[src/streaming_diff.rs:L66-L76](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L66-L76)）。

`Debug` 实现（[src/streaming_diff.rs:L93-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L93-L104)）外层循环遍历行、内层循环遍历列、每个元素调用 `get`，用 `{:5}` 定宽打印——这样 `println!("{:?}", matrix)` 输出的才是人类习惯的"一行一行"的矩阵。它在本讲的综合实践里会作为观察工具出场。

最后看一个真实使用处：`StreamingDiff::new` 把矩阵初始化为 `(old_len+1) × 1`，并用循环把第 0 列填成 \(i \times \text{DELETION\_SCORE}\)（[src/streaming_diff.rs:L133-L137](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L133-L137)）——"第 0 列"对应"新文本还没有任何字符"，此时只能删旧字符，所以整列都是删除代价。分数含义留到 u2-l2，这里只需看到"`resize` 定形 + `set` 逐格填值"的用法。

#### 4.1.4 代码实践

> 学习手册原则：不修改仓库源码。所以本讲所有实践都在一个**独立练习 crate** 里进行，把 `Matrix` 复制过去做实验。这也是阅读小型算法库的通用方法：复制、改造、用断言逼自己说出每一步的预期。

**实践目标**：亲手验证列主序下标换算与越界防护。

**操作步骤**：

1. 在任意目录执行 `cargo new matrix_lab --lib && cd matrix_lab`。
2. 把 [src/streaming_diff.rs:L10-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L10-L104)（`Matrix` 结构体、`impl Matrix` 与 `Debug` 实现）复制进 `src/lib.rs`。
3. 追加下面的测试（示例代码）：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matrix_layout_is_column_major() {
        let mut m = Matrix::new();
        m.resize(3, 2); // rows=3, cols=2
        // 用 row*10+col 作为可区分的值
        for row in 0..3 {
            for col in 0..2 {
                m.set(row, col, (row * 10 + col) as f64);
            }
        }
        // 换算验证：index(row, col) = col * rows + row
        assert_eq!(m.get(1, 1), 5.0); // 1*3+1 = 4 -> cells[4] = 5
        assert_eq!(m.get(2, 1), 6.0); // 1*3+2 = 5 -> cells[5] = 6
        assert_eq!(m.get(2, 0), 3.0); // 0*3+2 = 2 -> cells[2] = 3
        // 对角可交换性不成立：get(0,1)=1 而 get(1,0)=10，说明行列不可混
        assert_eq!(m.get(0, 1), 1.0);
        assert_eq!(m.get(1, 0), 10.0);
    }

    #[test]
    #[should_panic(expected = "row out of bounds")]
    fn matrix_get_rejects_out_of_range_row() {
        let mut m = Matrix::new();
        m.resize(3, 2);
        let _ = m.get(3, 0);
    }
}
```

4. 运行 `cargo test`。

**需要观察的现象**：两个测试都通过；特别是 `get(0,1)` 与 `get(1,0)` 返回不同值，证明 (row, col) 顺序不能写反。

**预期结果**：按手算，`cells` 的线性顺序应为 `[0, 10, 20, 1, 11, 21]`（先第 0 列的 3 个值，再第 1 列的 3 个值）。若断言失败，先检查你是不是把换算写成了 `row * cols + col`（行主序）。以上为手工推演，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`rows = 4, cols = 3` 时 `cells.len()` 是多少？`get(2, 2)` 对应 `cells` 的第几个元素？

**答案**：长度为 \(4 \times 3 = 12\)；下标为 \(2 \times 4 + 2 = 10\)（第 11 个元素，0 起数第 10 个）。

**练习 2**：`get` 先检查 `row` 再检查 `col`，调换检查顺序有区别吗？这两道检查的意义是什么？

**答案**：功能上等价，任何越界都会 panic，只是 panic 消息不同（先查谁就先报谁）。意义在于：不检查就直接做 `cells[col * rows + row]`，虽然 Rust 的切片索引也会 panic，但消息只有下标数字；显式检查提供了带语义（行/列）的错误信息，把"未定义行为风险"提前变成"可诊断的崩溃"。另外注意 `col * self.rows + row` 本身可能溢出吗？在本 crate 的使用规模下不会，但显式检查发生在乘法之前，也顺带规避了"用越界列号做大数乘法"的路径。

**练习 3**：`Debug` 实现里如果外层循环换成遍历列、内层遍历行，打印结果会变成什么样？

**答案**：输出会变成"竖着切"的转置样式——每行打印的是逻辑上的一列。因为内存本来就是列连续的，外层循环决定的是"打印的第 i 行对应哪个逻辑维度"。外层走行才能得到人类习惯的矩阵视图。

### 4.2 Matrix::resize：尾部补零恰好等于"右侧新增零列"

#### 4.2.1 概念说明

`resize(rows, cols)` 负责改变矩阵尺寸，实现只有一行核心逻辑：`self.cells.resize(rows * cols, 0.)`。它依赖 `Vec::resize` 的两个语义：

- **扩大**：在**尾部**追加 `0.` 直到达到新长度，已有元素原位不动；若 `Vec` 容量足够则不重新分配。
- **缩小**：从**尾部**截断多余元素。

关键洞察是：**在列主序布局下，`cells` 的尾部恰好是最右边的那几列**。所以只要 `rows`（也就是列的步长/宽度）不变，"扩容"就精确等价于"在矩阵右侧新增若干全 0 的列"，旧值的 `(row, col)` 位置全部保持不变。用公式说，设旧尺寸 \((r, c)\)、新尺寸 \((r, c')\)，\(c' > c\)：

\[
L' = r \times c' \ge L = r \times c, \quad \text{尾部追加 } L' - L \text{ 个 } 0
\]

\[
\forall j < c:\ \text{get}'(i, j) = \text{get}(i, j); \qquad \forall j \ge c:\ \text{get}'(i, j) = 0
\]

但如果 **`rows` 改变**，这个等价关系就破了：平铺数据的"切分宽度"变了，同一批字节会被重新解释成不同的 (row, col) 位置，数据会"串列"。这正是本 crate 的一个隐含约定：`rows` 一旦定下就不再改变——构造时 `resize(old_len + 1, 1)`（[src/streaming_diff.rs:L134](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L134)），之后 `push_new` 里扩容仍然是 `self.old.len() + 1` 行（[src/streaming_diff.rs:L152-L153](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L152-L153)）——旧文本在流式过程中是固定的，所以行数天然不变，只有列数随新文本增长。

这个"复用容量、只在尾部动手"的设计，让每来一个新字符块的矩阵扩容代价非常小：不搬旧数据、通常不重新分配，只是把右边界往外推。

#### 4.2.2 核心流程

```text
resize(rows, cols):
    cells.resize(rows * cols, 0.0)   # 尾部补零（扩大）或截断（缩小）
    self.rows = rows                 # 先完成可能涉及分配/搬移的操作，
    self.cols = cols                 # 再提交元数据
```

`push_new` 中的使用顺序值得注意（细节在 u3-l2 展开）：

1. `swap_columns(0, cols - 1)`：把上一轮的"边界列"（最后一列）换到第 0 列的位置。
2. `resize(old_len + 1, new_chunk_len + 1)`：向右扩出本轮待填的新列（全 0）。

也就是说：**先交换、后扩容**。交换发生在旧尺寸上（边界检查按旧列数进行），扩容补出的零列正好是接下来填表循环要写入的目标。

#### 4.2.3 源码精读

实现本体（[src/streaming_diff.rs:L26-L30](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L26-L30)）：

```rust
fn resize(&mut self, rows: usize, cols: usize) {
    self.cells.resize(rows * cols, 0.);
    self.rows = rows;
    self.cols = cols;
}
```

构造时的首次定形——行数为旧文本长度加一（多出的第 0 行表示"一个旧字符都不对齐"），列数为 1（[src/streaming_diff.rs:L133-L134](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L133-L134)）：

```rust
let mut scores = Matrix::new();
scores.resize(old_len + 1, 1);
```

流式扩容处（[src/streaming_diff.rs:L151-L153](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L151-L153)）：注意 `resize` 的第二个参数是 `self.new.len() - self.new_text_ix + 1`——列数是"**本轮**尚未处理的新字符数 + 1"，说明矩阵每轮都只覆盖当前块的相对列号，而不是整个新文本的绝对列号。这个"相对列"设计与 4.4 节的 `adjacent_columns_mut`、u3-l2 的主循环是一套的。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：验证"rows 不变时，扩容 = 旧值原位保留 + 右侧新列补零"，并亲眼看到 rows 改变时数据串位。

**操作步骤**：

1. 继续使用 4.1.4 的 `matrix_lab` crate。
2. 追加测试（示例代码）：

```rust
#[test]
fn matrix_resize_extends_with_zeroed_columns() {
    let mut m = Matrix::new();
    m.resize(2, 3); // rows=2, cols=3 -> cells 长 6
    for col in 0..3 {
        for row in 0..2 {
            m.set(row, col, (row * 10 + col) as f64);
        }
    }
    // 此刻 cells = [0, 10, 1, 11, 2, 12]

    m.resize(2, 5); // 扩到 5 列 -> cells 长 10，尾部补 4 个 0

    // 旧值原位保留
    assert_eq!(m.get(0, 0), 0.0);
    assert_eq!(m.get(1, 1), 11.0);
    assert_eq!(m.get(1, 2), 12.0);
    // 新列全为 0
    assert_eq!(m.get(0, 3), 0.0);
    assert_eq!(m.get(1, 3), 0.0);
    assert_eq!(m.get(0, 4), 0.0);
    assert_eq!(m.get(1, 4), 0.0);
}

#[test]
fn matrix_resize_with_different_rows_scrambles_data() {
    // 反面教材：改变 rows 会让平铺数据被重新切分
    let mut m = Matrix::new();
    m.resize(2, 3);
    for col in 0..3 {
        for row in 0..2 {
            m.set(row, col, (row * 10 + col) as f64);
        }
    }
    m.resize(3, 3); // rows 2 -> 3，步长变了
    // 手工推演：cells 仍以 [0,10,1,11,2,12] 开头，尾部补 3 个 0，
    // 但现在按每列 3 个元素切分：col0 = [0,10,1]!
    println!("{:?}", m);
    assert_eq!(m.get(2, 0), 1.0); // 旧 (0,1) 的值跑到了 (2,0)
}
```

3. 运行 `cargo test resize -- --nocapture`，观察第二个测试打印出的矩阵形状。

**需要观察的现象**：第一个测试通过——扩容后旧列原封不动、新列全 0；第二个测试也通过，但它的断言揭示旧值 `1.0`（原本在 `(0, 1)` 位置）出现在了 `(2, 0)`——数据串位了。

**预期结果**：与上面注释里的手工推演一致（`cells = [0,10,1,11,2,12]`，扩容后 `[0,10,1,11,2,12,0,0,0,0]`）。这解释了为什么 `streaming_diff` 里 `rows` 恒等于 `old_len + 1` 从不改变。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`resize(5, 4)` 后再 `resize(5, 2)`，`cells` 的长度和内容分别是什么？

**答案**：长度 \(5 \times 2 = 10\)。缩小从尾部截断，等价于砍掉最右边的 2 列，保留原第 0、1 列共 10 个元素，值与位置都不变。

**练习 2**：把 `resize` 的实现改成先赋值 `self.rows`/`self.cols` 再调用 `self.cells.resize(...)`，功能上有区别吗？哪种写法更稳妥？

**答案**：功能上等价（`cells.resize` 不读 `self.rows`/`self.cols`）。更稳妥的是现在的顺序：`cells.resize` 是唯一可能涉及内存分配、因而可能失败的操作，先做可能失败的事、再提交元数据，失败路径上结构体不会处于"字段已更新而数据没跟上"的半更新状态。

**练习 3**：为什么"尾部补零恰好等于右侧新增零列"这个性质对本 crate 特别重要？

**答案**：流式场景下列数随新文本块不断增长，`push_new` 每次都要扩容。有这个性质，扩容就只是"把右边界推出去"，不需要把任何旧列的数据搬家或重拷；配合 4.3 节的整列交换，矩阵可以无限"滚动"复用同一块缓冲区。

### 4.3 Matrix::swap_columns：全 crate 唯一的 unsafe 块

#### 4.3.1 概念说明

`swap_columns(col1, col2)` 把两**整列**（各 `rows` 个连续元素）原地交换。它的存在动机在 `push_new` 的第一行（[src/streaming_diff.rs:L151](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L151)）：

```rust
self.scores.swap_columns(0, self.scores.cols - 1);
```

流式计算中，**上一轮算完的最后一列**（边界列）在下一轮要充当**第 0 列**（初始列）。与其新建矩阵并拷贝，不如把最后一列和第 0 列交换一下，再在右侧补零扩容（4.2 节），整个矩阵就"滚动"了一格。这是流式算法不重新分配大内存的关键技巧。

为什么需要 unsafe？因为**安全 Rust 无法在一次操作里同时可变借用同一个 `Vec` 的两段不相交区间**——写 `(&mut cells[a..b], &mut cells[c..d])` 会被借用检查器直接拒绝（两个可变借用）。`split_at_mut` 只能切**一刀**，适合"相邻两段"（这正是 4.4 节的场景），但对任意两列需要切两刀。当然，逐元素 `cells.swap(a, b)` 循环也能安全实现，但 `std::ptr::swap_nonoverlapping` 可以让编译器生成更整体的移动（类似 memcpy 的块交换），对大列更快。这是典型的"用 unsafe 换性能 + 用前置检查换安全"的取舍。

#### 4.3.2 核心流程

```text
swap_columns(col1, col2):
    if col1 == col2: return          # 防线：同一列交换会违反 nonoverlapping 契约
    if col1 >= cols: panic           # 越界防护
    if col2 >= cols: panic
    unsafe:
        ptr = cells.as_mut_ptr()
        swap_nonoverlapping(ptr + col1*rows, ptr + col2*rows, rows)
```

`std::ptr::swap_nonoverlapping` 的安全契约（safety requirements）要求：两段内存各自在分配区内（in bounds），且**互不重叠**。逐条核对：

1. **in bounds**：已检查 `col < cols`，因此 \(col \times rows + rows \le cols \times rows = \text{cells.len()}\)，两段都完整落在缓冲区内。
2. **不重叠**：已检查 `col1 != col2`（开头早退），两个区间 \([col_1 \times rows,\ (col_1+1) \times rows)\) 与 \([col_2 \times rows,\ (col_2+1) \times rows)\) 长度各为 `rows` 且起点不同，必然不相交。

特别注意第 2 条：如果 `col1 == col2`，两个区间完全重合，调用 `swap_nonoverlapping` 就是**未定义行为**。所以开头的 `if col1 == col2 { return; }` 不只是省事的优化，而是维护 unsafe 前置条件的**安全防线**，绝对不能删。此外每段长度恰好是 `rows`，正因为列主序布局下列是连续的，"整列交换"才能用一次 `swap_nonverlapping` 完成。

#### 4.3.3 源码精读

完整实现（[src/streaming_diff.rs:L32-L53](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L32-L53)）：

```rust
fn swap_columns(&mut self, col1: usize, col2: usize) {
    if col1 == col2 {
        return;
    }
    if col1 >= self.cols {
        panic!("column out of bounds");
    }
    if col2 >= self.cols {
        panic!("column out of bounds");
    }
    unsafe {
        let ptr = self.cells.as_mut_ptr();
        std::ptr::swap_nonoverlapping(
            ptr.add(col1 * self.rows),
            ptr.add(col2 * self.rows),
            self.rows,
        );
    }
}
```

要点：`as_mut_ptr()` 拿到缓冲区首地址；`ptr.add(col * self.rows)` 做指针偏移定位到列首（`add` 要求偏移后在同一分配区内，越界检查已保证）；`swap_nonoverlapping` 交换 `self.rows` 个 `f64`。整个函数是 `src/streaming_diff.rs` 中**唯一**的 `unsafe` 块（可用 `grep -n unsafe src/streaming_diff.rs` 验证，只命中第 45 行）。

#### 4.3.4 代码实践

**实践目标**：验证整列交换的语义与越界防护，并练习"逐条核对 unsafe 契约"的方法。

**操作步骤**：

1. 在 `matrix_lab` 中追加测试（示例代码）：

```rust
#[test]
fn matrix_swap_columns_swaps_whole_columns() {
    let mut m = Matrix::new();
    m.resize(2, 3);
    for col in 0..3 {
        for row in 0..2 {
            m.set(row, col, (row * 10 + col) as f64);
        }
    }
    // cells = [0,10 | 1,11 | 2,12]
    m.swap_columns(0, 2);
    // 期望 cells = [2,12 | 1,11 | 0,10]
    assert_eq!(m.get(0, 0), 2.0);
    assert_eq!(m.get(1, 0), 12.0);
    assert_eq!(m.get(0, 2), 0.0);
    assert_eq!(m.get(1, 2), 10.0);
    // 中间列不受影响
    assert_eq!(m.get(0, 1), 1.0);
    assert_eq!(m.get(1, 1), 11.0);
}

#[test]
fn matrix_swap_same_column_is_noop() {
    let mut m = Matrix::new();
    m.resize(2, 2);
    m.set(0, 1, 7.0);
    m.swap_columns(1, 1); // 早退分支
    assert_eq!(m.get(0, 1), 7.0);
}

#[test]
#[should_panic(expected = "column out of bounds")]
fn matrix_swap_rejects_out_of_range_column() {
    let mut m = Matrix::new();
    m.resize(2, 2);
    m.swap_columns(0, 2);
}
```

2. 运行 `cargo test swap`。

**需要观察的现象**：三个测试都通过；交换后两列的值整体互换，中间列原样；相同列号是无操作；越界列号触发 panic。

**预期结果**：与注释中的手推 `cells` 变化一致。**待本地验证**。

**源码阅读型附加任务**：打开标准库文档中 `std::ptr::swap_nonoverlapping` 的 Safety 一节，把它的每条要求抄下来，在 `swap_columns` 里逐条找到对应的保证（本讲 4.3.2 已给出核对示范）。这个"契约核对"习惯是阅读一切 unsafe 代码的基本功。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `if col1 == col2 { return; }`，`swap_columns(1, 1)` 的结果还对吗？

**答案**：数值结果看起来"不变"（自己和自己交换），但这是**未定义行为**——两段内存完全重叠，违反 `swap_nonoverlapping` 的"不得重叠"契约。UB 意味着编译器可以做出任何假设，今天"碰巧对"不代表明天还对。所以这个早退是安全防线，不能删。

**练习 2**：能否用纯安全代码实现同样的整列交换？代价是什么？

**答案**：可以，例如 `for i in 0..self.rows { self.cells.swap(col1 * self.rows + i, col2 * self.rows + i); }`（`slice::swap` 是安全 API），或者用两次 `split_at_mut` 嵌套切出两段。语义完全等价；`swap_nonoverlapping` 的版本让编译器有机会把逐元素交换优化成块移动，在列很长时更快。本 crate 选择了 unsafe + 前置检查的组合。

**练习 3**：`rows == 0` 时调用 `swap_columns(0, 1)`（假设 `cols >= 2`）会发生什么？

**答案**：交换 0 个元素，等价于无操作——两个列首指针都偏移 0 个元素，`swap_nonoverlapping` 不访问任何元素。实际上在本 crate 中 `rows` 恒为 `old_len + 1 >= 1`，这个退化情形不会被触发。

### 4.4 Matrix::adjacent_columns_mut：一刀切出"读上一列、写当前列"

#### 4.4.1 概念说明

填表循环（u3-l2 精读）的每一步是：**读第 `k-1` 列、写第 `k` 列**。按借用规则，"同时读 A 段、写 B 段（A、B 同属一个 Vec）"不能直接写出来；而幸运的是，这里 A 和 B **相邻**——`split_at_mut` 恰好只切一刀就能把"某点之前"和"某点之后"分成两个独立的可变切片。于是：

- 在 `current_col * rows` 处切一刀：前半段的**最后 `rows` 个元素**就是上一列，后半段的**前 `rows` 个元素**就是当前列。
- 返回类型是 `(&[f64], &mut [f64])`：上一列**只读**、当前列**可写**。这个类型签名把 DP 填表的数据流（信息只从左列流向右列，右列写完不再回头改左列）直接编码进了 API——调用方想误写上一列都过不了编译。

为什么 `current_col == 0` 要 panic？因为第 0 列没有"左邻居"，"上一列"根本不存在；`current_col >= cols` 则是常规越界。

#### 4.4.2 核心流程

```text
adjacent_columns_mut(current_col):     # 要求 1 <= current_col < cols
    start = current_col * rows
    (before, after) = cells.split_at_mut(start)   # 安全地切成两段
    返回 ( &before[start - rows .. start],        # 上一列（只读，长度 rows）
           &mut after[..rows] )                   # 当前列（可写，长度 rows）
```

内存示意图（`rows = 3`，`current_col = 2`）：

```text
cells:  [ col0: 3 个 | col1: 3 个 | col2: 3 个 | ... ]
                        └── before ──┘└──── after ─────┘
                        切点在 col2 起点 = 2 * 3 = 6
previous = before 的最后 3 个   = col1（只读）
current  = after  的最前 3 个   = col2（可写）
```

无下溢保证：`current_col >= 1` 意味着 \(start = current\_col \times rows \ge rows\)，所以 `start - rows` 这个 `usize` 减法不会回绕。切片范围合法性：`before` 长度为 `start`，取其尾部 `rows` 个需要 \(start \ge rows\)（成立）；`after` 长度为 \((cols - current\_col) \times rows \ge rows\)（因 `current_col < cols`），取其头部 `rows` 个也合法。

#### 4.4.3 源码精读

实现（[src/streaming_diff.rs:L78-L90](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L78-L90)）：

```rust
fn adjacent_columns_mut(&mut self, current_col: usize) -> (&[f64], &mut [f64]) {
    if current_col == 0 || current_col >= self.cols {
        panic!("column out of bounds");
    }

    let current_col_start = current_col * self.rows;
    let previous_col_start = current_col_start - self.rows;
    let (before_current, current_and_after) = self.cells.split_at_mut(current_col_start);
    (
        &before_current[previous_col_start..current_col_start],
        &mut current_and_after[..self.rows],
    )
}
```

真实调用处（[src/streaming_diff.rs:L155-L179](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L155-L179)）：填表循环对每个新字符的相对列号 `relative_j` 调用一次（[src/streaming_diff.rs:L162](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L162)）：

```rust
let (previous_scores, current_scores) = self.scores.adjacent_columns_mut(relative_j);
```

随后 `current_scores[0]` 在 L164 写入、`current_scores[i]` 在 L178 写入；`previous_scores[i]`（L166）与 `previous_scores[i - 1]`（L173）只读——与方法签名承诺的数据流完全一致。另外注意 L157 的 `relative_j = j - self.new_text_ix`：列号是相对本轮锚点的偏移，这与 4.2.3 观察到的 `resize` 列数口径（`new.len() - new_text_ix + 1`）配套，矩阵每轮都"从头开始"编号。

#### 4.4.4 代码实践

**实践目标**：验证 `adjacent_columns_mut` 返回的切片身份（上一列只读、当前列可写）与长度。

**操作步骤**：

1. 在 `matrix_lab` 中追加测试（示例代码）：

```rust
#[test]
fn matrix_adjacent_columns_returns_previous_and_current() {
    let mut m = Matrix::new();
    m.resize(3, 3);
    for col in 0..3 {
        for row in 0..3 {
            m.set(row, col, (row * 10 + col) as f64);
        }
    }
    // cells = [0,10,20 | 1,11,21 | 2,12,22]

    {
        let (previous, current) = m.adjacent_columns_mut(2);
        assert_eq!(previous.len(), 3); // 长度等于 rows
        assert_eq!(current.len(), 3);
        // previous 是第 1 列（只读）
        assert_eq!(previous[0], 1.0);
        assert_eq!(previous[2], 21.0);
        // current 是第 2 列，写入立即可见
        current[0] = 99.0;
    } // 可变借用在这里结束
    assert_eq!(m.get(0, 2), 99.0);
    // 第 0、1 列不受影响
    assert_eq!(m.get(1, 1), 11.0);
}

#[test]
#[should_panic(expected = "column out of bounds")]
fn matrix_adjacent_columns_rejects_first_column() {
    let mut m = Matrix::new();
    m.resize(2, 2);
    let _ = m.adjacent_columns_mut(0);
}
```

2. 运行 `cargo test adjacent`。

**需要观察的现象**：第一个测试通过——`previous` 恰是第 1 列的值、写 `current` 反映到 `get(·, 2)`；第二个测试确认 `current_col = 0` 被 panic 拒绝。

**预期结果**：与手推一致。**待本地验证**。附加观察：如果你在第一个测试里尝试写 `previous[0] = 1.0;`，会得到编译错误（`&[f64]` 不可写）——签名层面的防护是可以亲自触发的。

#### 4.4.5 小练习与答案

**练习 1**：为什么返回类型是 `(&[f64], &mut [f64])` 而不是 `(&mut [f64], &mut [f64])`？

**答案**：填表只需要读上一列。给上一列只读引用，(a) 从类型上杜绝调用方误改已结算的列（改了会破坏 DP 的正确性），(b) 也符合"信息单向从左列流向右列"的数据流直觉。技术上 `split_at_mut` 返回的两段都是 `&mut`，取其不可变再借用（`&before_current[..]`）即可降级为只读——这是实现者的主动选择。

**练习 2**：`current_col = 1` 时 `previous` 是哪一列？为什么这个方法不能用来填第 0 列？

**答案**：`previous` 是第 0 列。第 0 列是"边界列"（初始条件），在 `StreamingDiff::new` 里由构造函数用删除代价直接初始化（4.1.3），不参与"由左列推出"的递推，所以 API 从设计上就把 `current_col = 0` 排除掉了。

**练习 3**：如果把实现改成 `(&cells[a..b], &mut cells[c..d])` 两个独立切片表达式，会发生什么？

**答案**：无法通过借用检查——同一时刻对 `cells` 既存在共享借用又存在可变借用（或两个可变借用），编译器直接报错。这正是需要 `split_at_mut` 的原因：它用"切一刀"证明两段互不重叠，从而安全地返回两个独立切片。

## 5. 综合实践

**任务**：把本讲四个模块串成一个"迷你滚动矩阵"demo，预热单元三的 `push_new` 主循环。

**背景**：`push_new` 每收到一个新文本块，骨架就是三步——交换（滚动）→ 扩容 → 逐列填表（[src/streaming_diff.rs:L151-L182](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L151-L182)）。我们用简化分数（"每列比左列 +1"）复刻这个骨架。

**操作步骤**（在 `matrix_lab` 中追加，示例代码）：

```rust
#[test]
fn rolling_matrix_demo() {
    let rows = 4; // 模拟 old_len + 1
    let mut m = Matrix::new();

    // 第 1 步：构造初始化——第 0 列填入 i * (-20.)（模拟删除代价初始化，L135-L137）
    m.resize(rows, 2);
    for i in 0..rows {
        m.set(i, 0, i as f64 * -20.);
    }
    let boundary: Vec<f64> = (0..rows).map(|i| m.get(i, 1)).collect(); // 记录滚动前的最后一列

    // 第 2 步：滚动——把最后一列换到第 0 列，再向右扩出一列零
    m.swap_columns(0, 1);
    m.resize(rows, 3);

    // 断言：上一轮的边界列现在就是第 0 列
    for i in 0..rows {
        assert_eq!(m.get(i, 0), boundary[i]);
    }

    // 第 3 步：逐列填表——第 k 列 = 第 k-1 列每行 +1（模拟“由左列推右列”）
    for k in 1..=2 {
        let (previous, current) = m.adjacent_columns_mut(k);
        for i in 0..rows {
            current[i] = previous[i] + 1.;
        }
    }

    // 断言：第 1、2 列依次比边界列大 1、大 2
    for i in 0..rows {
        assert_eq!(m.get(i, 1), boundary[i] + 1.);
        assert_eq!(m.get(i, 2), boundary[i] + 2.);
    }

    println!("{:?}", m); // 用 Debug 实现观察最终矩阵形态
}
```

**需要观察的现象**：

1. 滚动后第 0 列的值等于滚动前的最后一列（本例中扩容前最后一列全 0，所以第 0 列为 0——重点在于"位置对上了"这一机制）。
2. 填表后每一列整体比左列大 1，`println!` 打印出阶梯状的矩阵。
3. 全程没有重新分配整个矩阵（可在 `resize` 前后打印 `cells.capacity()` 对比观察，通常扩容时容量已够或成倍增长）。

**预期结果**：按手推，最终矩阵每行为 `[boundary[i], boundary[i]+1, boundary[i]+2]`。**待本地验证**。

**思考题**（选做）：如果第 3 步的填表顺序改成从右往左（先填第 2 列、再填第 1 列），结果会不同吗？为什么 `push_new` 的真实实现必须从左往右填？（提示：第 k 列依赖第 k-1 列的**新值**；从右往左会读到还没更新的旧数据。）

## 6. 本讲小结

- `Matrix` 用一个一维 `Vec<f64>` 加 `rows`/`cols` 两个字段实现可增长的二维打分表，采用**列主序**布局，下标换算为 \(\text{index}(row, col) = col \times rows + row\)；`get`/`set` 都带行列双重越界检查，把错误变成带语义的 panic。
- `resize` 委托给 `Vec::resize` 的"尾部补零/截断"语义；因为列主序下尾部恰好是最右侧的列，且本 crate 中 `rows` 恒为 `old_len + 1` 不变，扩容就等价于"右侧新增全 0 的列、旧值原位不动"——这是流式场景下低成本扩容的关键。
- `swap_columns` 是全 crate 唯一的 unsafe 块：用 `as_mut_ptr` + `std::ptr::swap_nonoverlapping` 一次性交换两段各长 `rows` 的连续内存。安全性由三道前置保证支撑：`col1 != col2`（不重叠）、两个列号越界检查（in bounds）、列主序（列连续）。
- `adjacent_columns_mut` 用一次 `split_at_mut` 同时借出"上一列（`&[f64]` 只读）+ 当前列（`&mut [f64]` 可写）"，把 DP 填表的单向数据流编码进类型签名，是借用检查器友好的标准技巧。
- 这四个操作合起来构成 `push_new` 的矩阵骨架：**交换滚动（L151）→ 扩容补零（L152-L153）→ 相邻两列填表（L162）**，使矩阵可以无限复用同一块缓冲区。

## 7. 下一步学习建议

- **下一讲（u2-l2）**：矩阵的"格子"里到底存什么分数？我们将解读 `INSERTION_SCORE = -1`、`DELETION_SCORE = -20`、`EQUALITY_BASE = 1.8`、`MAX_EQUALITY_EXPONENT = 16` 这组打分常量（[src/streaming_diff.rs:L124-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L124-L128)），理解为什么这个打分模型天然偏向"保留旧文本 + 插入新内容"。
- **预先阅读**：带着本讲的结论去读 [src/streaming_diff.rs:L149-L199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149-L199)（`push_new`），重点看 L151-L153 的滚动扩容和 L155-L182 的填表循环如何把 `swap_columns`、`resize`、`adjacent_columns_mut` 串起来——综合实践的第 2、3 步就是它们的简化版。
- **延伸阅读**（标准库文档）：`Vec::resize`、`slice::split_at_mut`、`std::ptr::swap_nonoverlapping` 的文档与 Safety 章节，对照本讲的核对示范再读一遍。
- **背景知识**：如果对"列主序 vs 行主序"的缓存影响感兴趣，可以搜索"cache-friendly loop order / row-major vs column-major"相关材料；这与本 crate 按列填表的访问模式直接相关。
