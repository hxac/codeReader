# push_new 主循环：矩阵滚动与增量填表

## 1. 本讲目标

`push_new` 是 streaming_diff 的心脏：每收到一块新文本，它就把打分矩阵「滚动」一格，只为本块字符增量填表，最后在末列里选出新锚点并回溯出字符操作。学完本讲你应该能够：

1. 把 `push_new` 拆成「滚动 → 增量填表 → 锚定回溯」三段，并说出每一段对应的源码行。
2. 解释 `swap_columns(0, cols - 1)` 为什么能把上一轮的末列（锚点列）变成新一轮的初始化列，以及为什么这只是「交换」而不是「搬移」。
3. 读懂 `adjacent_columns_mut` 返回的 `(前一列只读, 当前列可写)` 如何支撑原地填表，以及第 0 行为什么用**绝对**列号 `j` 计分。
4. 跟踪 `previous_equal_runs` / `current_equal_runs` 这对双缓冲：何时清零、何时写入、何时换位、如何跨 `push_new` 调用保持状态。
5. 通过亲手加 `eprintln!` 打印矩阵，看到第二次 `push_new` 是如何复用上一次的矩阵缓冲的。

## 2. 前置知识

本讲站在前三讲的肩膀上，先把这些词复习一遍（细节见对应讲义）：

- **DP 状态 \( S(i, j) \)**：旧文本前 \( i \) 个字符与新文本前 \( j \) 个字符对齐后的最优分数。递推有三候选（u2-l2）：插入 \( c_{\text{ins}} = -1 \)、删除 \( c_{\text{del}} = -20 \)、相等 \( 1.8^{\min(\lfloor r/4 \rfloor,\ 16)} \)（\( r \) 是连续相等游程长度）。
- **列主序矩阵**（u2-l1）：`Matrix` 用一维 `Vec<f64>` 存 \( M+1 \) 行（\( M \) = 旧文本字符数）× 若干列，下标换算 `col * rows + row`。填表天然按列推进，一次只需要相邻两列。
- **边界列**（u3-l1）：`new()` 把矩阵初始化为 \( (M+1) \times 1 \)，第 0 列填 \( S(i,0) = -20i \)（删除前 \( i \) 个旧字符的代价）。此后每一轮 `push_new` 的起点，都是「上一轮结束时算好的最后一列」。
- **锚点 `(old_text_ix, new_text_ix)`**（u3-l1）：已经「结清」的区域边界。`push_new` 的全部工作可以概括成一句话：**从旧锚点出发，把本块新字符算完，再选一个新锚点，把两点之间的编辑脚本吐出来**。
- **双缓冲（double buffering）**：一块只读、一块可写，写完整体换位。图形学里用「前台帧/后台帧」避免边画边看；这里用 `previous_equal_runs` / `current_equal_runs` 两个等长数组避免逐元素复制游程状态。

如果这些词还有陌生的，建议先回看 u2-l1（Matrix 与 `swap_columns`）、u2-l2（打分常量与游程奖励）、u3-l1（构造函数与边界列），再继续本讲。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会聚焦其中几段：

| 代码位置 | 作用 | 与本讲的关系 |
|---|---|---|
| [streaming_diff.rs:L10-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L10-L104) | 私有 `Matrix`：存储、`resize`、`swap_columns`、`adjacent_columns_mut`、`Debug` 打印 | 本讲的两个「底层轮子」，u2-l1 已逐行讲过，本讲看它们的调用现场 |
| [streaming_diff.rs:L113-L122](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L113-L122) | `StreamingDiff` 的七个字段 | 重点看 `old_text_ix`/`new_text_ix` 锚点与两个 equal_runs 缓冲 |
| [streaming_diff.rs:L125-L147](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L125-L147) | 打分常量与 `new()` 构造函数 | u3-l1 已讲，本讲只引用其产出（边界列、初始锚点） |
| [streaming_diff.rs:L149-L199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149-L199) | **`push_new`——本讲主角** | 逐行精读 |
| [streaming_diff.rs:L201-L274](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L201-L274) | `backtrack` 回溯 | 本讲只当作黑盒调用，下一讲 u3-l3 专门拆 |
| [streaming_diff.rs:L963-L981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L963-L981) | 测试辅助 `random_streaming_diff`：随机分块喂给 `push_new` 的参考循环 | 综合实践里模仿它的分块方式 |
| [streaming_diff.rs:L1104-L1124](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) | 测试辅助 `apply_char_operations`：字符操作的参考解释器 | 综合实践里复用它做 round-trip 验证 |

（下文所有链接均省略前缀，只写 `streaming_diff.rs` 与行号；完整 base 为 `https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/`。）

## 4. 核心概念与源码讲解

### 4.1 push_new 全景：一块新文本的三段式旅程

#### 4.1.1 概念说明

`push_new(&mut self, text: &str) -> Vec<CharOperation>` 是流式差异的增量入口。调用者（比如 agent 的编辑会话）每从 LLM 那里收到一小段新文本，就调用一次 `push_new`，拿回「这一轮新结算出来」的字符操作序列。

它要解决的问题是：**标准 DP diff 需要拿到完整新文本才能算，而流式场景中新文本是一块一块到达的**。朴素的办法是每来一块就把整张 \( (M+1) \times (N+1) \) 表重算一遍——浪费；本实现的办法是利用「DP 递推只依赖前一列」这一性质，把上一轮算好的最后一列滚动为新一轮的第 0 列，只为**本块新增的字符**填列。

#### 4.1.2 核心流程

```text
push_new(text):                       # 设本块有 K 个字符，进入时锚点为 (old_text_ix, new_text_ix)
    ① 滚动
        new ← new + text 的字符        # new.len() 增加 K
        swap_columns(0, cols-1)        # 上一轮末列（= 锚点列）与第 0 列互换
        resize(M+1, new.len() - new_text_ix + 1)   # 新列数 = K + 1
    ② 增量填表（对每个新字符算一列）
        for j in (new_text_ix, new.len()]:
            current_equal_runs ← 全 0
            当前列[0] ← j × (-1)       # 第 0 行用绝对 j
            for i in 1..=M:
                当前列[i] ← max(插入, 删除, 相等分)
            swap(previous_equal_runs, current_equal_runs)
    ③ 锚定与回溯
        next_old_text_ix ← argmax{ 当前列[i] : i ∈ [old_text_ix, M] }
        hunks ← backtrack(next_old_text_ix, new.len())
        (old_text_ix, new_text_ix) ← (next_old_text_ix, new.len())
        return hunks
```

两句话概括：**①让旧答案就位，②只为新字符付算力，③在末列挑一个分数最高的收尾行，把从旧锚点到新锚点的路径翻译成操作序列。**

#### 4.1.3 源码精读

函数整体在 [streaming_diff.rs:L149-L199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149-L199)。三段的分界非常清晰：

- **① 滚动**只有三行（[L150-L153](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L150-L153)）：先把本块字符追加进 `self.new`，再交换第 0 列与最后一列，最后把矩阵列数调整为本块长度加一。4.2 节逐行拆解。
- **② 填表**是一个双层循环（[L155-L182](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L155-L182)）：外层每个新字符一列，内层每列从上到下填 \( M+1 \) 行。4.3、4.4 节逐行拆解。
- **③ 锚定回溯**（[L184-L198](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L198)）：从当前锚点行 `old_text_ix` 扫到 \( M \)，在末列里找分数最大的行（[L188-L192](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L188-L192) 用严格大于 `>` 比较并初始化 `max_score = NEG_INFINITY`，所以并列时取**最小**的行）；`next_new_text_ix` 无条件取 `new.len()`（[L186](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L186)）；然后从新锚点回溯到旧锚点拿到 hunks（[L195](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L195)），最后才把两个锚点字段更新为新值（[L196-L197](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L196-L197)）——**先回溯后更新**这个顺序不能颠倒，因为 `backtrack` 的循环终止条件就是「走回当前锚点」。

一个容易误读的地方：`push_new` 返回的 `Keep` 是**从旧锚点起算**的偏移。比如旧文本 `"hello"`，第一轮返回 `Keep { bytes: 2 }`（保住 `"he"`），第二轮返回 `Keep { bytes: 3 }` 指的是旧文本第 2..5 个字符 `"llo"`，而不是从头数的 `"hel"`。消费者必须把各轮返回值**按顺序拼接**使用（u1-l2 的守恒律按拼接后的整条序列成立）。

还有一个值得注意的不变量：锚点扫描从 `self.old_text_ix` 开始（[L187](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L187)），所以 `old_text_ix` 单调不减——已经结清的旧行不会再被选为终点，路径不会折回已吐出操作的区域。

#### 4.1.4 代码实践：给 push_new 装上「示波器」

本讲的主实践任务：在一份**复制的** `push_new` 里加打印，观察第二次调用如何复用矩阵。全程不改动 zed 仓库（那是读者自己的 clone，改了也无妨，但我们在外面搭练习更干净）。

**1. 实践目标**：亲眼看到「①滚动复用缓冲、②逐列增量填表、③锚点推进」三件事。

**2. 操作步骤**：

- 步骤一：在 zed 仓库**外面**新建一个练习 crate：

  ```
  streaming-trace/
  ├── Cargo.toml
  └── src/main.rs
  ```

  `Cargo.toml`（`rope` 用 path 依赖指向你本地的 zed 仓库；`ordered-float` 对齐 workspace 里的 `2.1.1`；空 `[workspace]` 表让它与 zed 的工作区隔离）：

  ```toml
  [package]
  name = "streaming-trace"
  version = "0.1.0"
  edition = "2021"

  [workspace]

  [dependencies]
  ordered-float = "2.1"
  rope = { path = "../zed/crates/rope" }   # 按你的实际相对路径调整
  ```

- 步骤二：把 [streaming_diff.rs 的第 1–522 行](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1-L522)（即 `#[cfg(test)]` 之前的全部实现）复制进 `src/main.rs`，并在文件末尾追加一个 `main` 函数。

- 步骤三：在 `push_new` 里插入三处打印（示例代码，非项目原有代码）：

  ```rust
  // ① 滚动之后：打印换进来的边界列（第 0 列）与新列数
  eprintln!("== push_new({text:?}) cols={} boundary_col={:?}",
      self.scores.cols,
      (0..=self.old.len()).map(|i| self.scores.get(i, 0)).collect::<Vec<_>>(),
  );
  ```

  ```rust
  // ② 内层 i 循环结束后（源码 L179 之后）：打印本列分数向量
  eprintln!("   j={j} char={new_char:?} col={current_scores:?}");
  ```

  ```rust
  // ③ 锚点扫描后（源码 L193 之后）：
  eprintln!("   anchor: ({next_old_text_ix}, {next_new_text_ix}) max_score={max_score}");
  ```

  （`Matrix` 自带 `Debug` 实现，见 [streaming_diff.rs:L93-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L93-L104)，想在填表前看整表也可以直接 `eprintln!("{:?}", self.scores)`。）

- 步骤四：`main` 里跑双块推送：

  ```rust
  fn main() {
      let mut diff = StreamingDiff::new("hello".to_string());
      println!("round 1: {:?}", diff.push_new("he"));
      println!("round 2: {:?}", diff.push_new("llo world"));
      println!("finish : {:?}", diff.finish());
  }
  ```

- 步骤五：`cargo run`，把完整输出存成一份 trace 记录。

**3. 需要观察的现象**：

- 第一次调用 `push_new("he")` 时矩阵只有 1 列，`swap_columns(0, 0)` 命中提前返回分支，什么都不交换；
- 第二次调用 `push_new("llo world")` 开头打印的 `boundary_col` 应该正好等于上一轮**最后一列**的分数向量（矩阵缓冲被复用，而不是重新分配）；
- 每列的 `col[0]` 随绝对 `j` 递减（-1、-2、-3……），第二轮从 -3 继续而不是从 -1 重新开始；
- 两轮的 `anchor` 打印：第一轮从 `(0,0)` 推进到某处，第二轮再推进到 `(5, 11)`。

**4. 预期结果**（下面是我按源码手算的期望值，供你核对；**待本地验证**）：

第一轮 `push_new("he")`（\( M=5 \)，进入时锚点 `(0,0)`、cols=1）：

| 列 | 新字符 | 分数向量（行 0..5） |
|---|---|---|
| 第 0 列（边界，来自 `new()`） | — | `[0, -20, -40, -60, -80, -100]` |
| j=1 | `'h'` | `[-1, 1, -19, -39, -59, -79]` |
| j=2 | `'e'` | `[-2, 0, 2, -18, -38, -58]` |

锚点扫描在 j=2 列取到最大值 2（行 2），anchor 推进到 `(2, 2)`，返回 `[Keep { bytes: 2 }]`。

第二轮 `push_new("llo world")`（进入时锚点 `(2,2)`、cols=3）：交换把 `[-2, 0, 2, -18, -38, -58]` 换到第 0 列，`resize` 扩到 10 列。前几列（相对列号）：

| 列 | 新字符 | 分数向量（行 0..5） |
|---|---|---|
| rel 1（j=3） | `'l'` | `[-3, -1, 1, 3, -17, -37]` |
| rel 2（j=4） | `'l'` | `[-4, -2, 0, 2, 4.8, -15.2]` |
| rel 3（j=5） | `'o'` | `[-5, -3, -1, 1, 3.8, 6.6]` |
| rel 9（j=11，末列） | `'d'` | `[-11, -9, -7, -5, -2.2, 0.6]` |

锚点扫描在行 2..=5 中取到最大值 0.6（行 5），anchor 推进到 `(5, 11)`，返回 `[Keep { bytes: 3 }, Insert { text: " world" }]`；随后 `finish()` 返回 `[]`（锚点已在终点）。

如果手头没有练习环境，退化为「源码阅读型实践」也可：对照上表逐格验证递推式（见 4.3.2），同样能完成观察。

#### 4.1.5 小练习与答案

**练习 1**：`push_new` 为什么每次都把 `next_new_text_ix` 定为 `new.len()`，而不是像 `old_text_ix` 那样在末列里挑？

**答案**：新文本是「输入」，本块字符全部到达、必须全部消费掉，所以新文本侧的锚点直接走到本块末尾；旧文本侧是「存量」，终点选在哪一行是算法的自由度（半全局对齐的自由终点），所以要扫描挑最大分。

**练习 2**：如果把 [L195-L197](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L195-L197) 中的 `backtrack` 调用移到两个锚点赋值**之后**，会发生什么？

**答案**：`backtrack` 内部的 `while (i, j) != (self.old_text_ix, self.new_text_ix)`（[L206](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L206)）会立刻满足——起点等于「新锚点」——循环体一次都不执行，返回空 `hunks`。也就是说每轮什么都吐不出来，所有差异被无限推迟。

**练习 3**：调用 `push_new("")`（空块）安全吗？推演一下会发生什么。

**答案**：安全。`new` 不变；若 cols>1，交换仍会把末列换到第 0 列，随后 `resize` 把列数收回到 1；填表循环范围 `new_text_ix+1..=new.len()` 为空，一次都不执行；锚点扫描在（换回来的同一列）边界列上重跑，同一组分数、从当前锚点行开始扫，最大值仍是原来的行，锚点不变；`backtrack` 起点等于锚点，返回 `[]`。结论：空块是无操作。（推演基于源码阅读，待本地验证。）

### 4.2 矩阵滚动：swap_columns + resize 如何复用上一轮的边界列

#### 4.2.1 概念说明

DP 按列填表时，算第 \( j \) 列只需要第 \( j-1 \) 列。所以理论上矩阵永远只需要「上一列 + 当前列」两列。但第 0 行的绝对计分和锚点回溯需要访问完整列历史吗？不需要——回溯只沿着已经算出的列走，而每一轮 `push_new` 之后我们只保留一列：**锚点列**。下一轮把这一列搬到第 0 列位置，就能当初始化列用。

这一节回答三个问题：为什么搬运的是「上一轮末列」？为什么用「交换」而不是「搬移」？`resize` 的列数公式是怎么来的？

#### 4.2.2 核心流程

```text
进入时：矩阵列 = [C0(旧边界), C1, ..., C_{K'-1}, C_{K'}(上一轮末列 = 当前锚点列)]
swap_columns(0, cols-1)
        ↓
矩阵列 = [C_{K'}(锚点列就位为边界), C1, ..., C_{K'-1}, C0(陈旧)]
resize(M+1, K+1)          # K = 本块字符数
        ↓
矩阵列 = [锚点列, 陈旧列或补零列 × max(0, ...) ..., 追加的零列]
        ↓  填表循环覆写第 1..=K 列的每一行
矩阵列 = [锚点列, 本块第 1 列, ..., 本块第 K 列(新锚点列)]
```

关键论证：**陈旧数据不会泄漏**。填表循环对第 \( k \) 列（\( k = 1..K \)）会写满全部 \( M+1 \) 行——第 0 行在 [L164](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L164)，第 1..\( M \) 行在 [L165-L179](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L165-L179) 的内层循环里无一遗漏。所以交换后被换到矩阵尾部/被 `resize` 截断的那些旧值，要么被覆写、要么被丢弃，永远不会被读到。

#### 4.2.3 源码精读

- [streaming_diff.rs:L150](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L150)：把本块字符追加到 `self.new`。注意 `self.new` 从不截断——它是新文本的完整历史，锚点只是标记「已结算到哪」。
- [streaming_diff.rs:L151](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L151)：`swap_columns(0, self.scores.cols - 1)`。上一轮结束时最后一列就是锚点列（上一轮锚点扫描读的正是它），把它换到第 0 列，本轮的「初始化列」就位。`swap_columns` 的实现（[L32-L53](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L32-L53)，u2-l1 逐行讲过）用 `as_mut_ptr` + `swap_nonoverlapping` 做**整列交换**：代价是 \( O(M) \) 次指针交换，与矩阵有多少列无关。如果改成「所有列左移一格」，列主序下要搬 \( O(M \times \text{cols}) \) 个 `f64`——块越多越亏。这就是「交换」胜过「搬移」的原因。
- [streaming_diff.rs:L152-L153](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L152-L153)：`resize(self.old.len() + 1, self.new.len() - self.new_text_ix + 1)`。行数恒为 \( M+1 \)（本 crate 里 `Matrix` 的行数从不改变，`resize` 在行数不变时等价于右侧补零或截断，见 u2-l1）；列数 \( = K + 1 \)，其中 \( K \) 是本块字符数——因为进入本 call 时 `new_text_ix` 恰好等于上一轮的 `new.len()`（[L197](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L197) 每轮都把它设为当时的 `new.len()`）。**矩阵大小只跟「单块长度」有关，跟新文本总长无关**——这是滚动复用在空间上的收益。
- 边界情形一（首次调用）：`new()` 之后 cols=1，`swap_columns(0, 0)` 命中 [L33-L35](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L33-L35) 的提前返回——边界列本来就在第 0 列，无需滚动。u2-l1 把这个早退分支称作安全防线，这里看到它第一次真正派上用场。
- 边界情形二（本块比上一块短）：`resize` 会截断右侧列；被截掉的正是换到尾部的陈旧列，无妨（反正要被覆写或已不需要）。

#### 4.2.4 代码实践

**1. 实践目标**：验证「上一轮末列 = 下一轮边界列」这一复用关系。

**2. 操作步骤**：沿用 4.1.4 的练习 crate。在 `push_new` 的 `swap_columns` 调用**前后**各加一行打印（示例代码）：

```rust
eprintln!("before swap cols={} last_col={:?}",
    self.scores.cols,
    (0..=self.old.len()).map(|i| self.scores.get(i, self.scores.cols - 1)).collect::<Vec<_>>(),
);
self.scores.swap_columns(0, self.scores.cols - 1);
eprintln!("after  swap col0 ={:?}",
    (0..=self.old.len()).map(|i| self.scores.get(i, 0)).collect::<Vec<_>>(),
);
```

对 `old="hello"` 先后 `push_new("he")`、`push_new("llo world")` 各一次。

**3. 需要观察的现象**：第二次调用里，`before swap` 打出的 `last_col` 应与 `after swap` 打出的 `col0` 完全一致，并且都等于第一次调用最后一个 `j=2` 列的分数向量。

**4. 预期结果**：三个值均为 `[-2, 0, 2, -18, -38, -58]`（手算值，**待本地验证**）。若不一致，说明你对滚动时机的理解有偏差，回头对照 4.2.2 的流程图。

#### 4.2.5 小练习与答案

**练习 1**：为什么交换的目标是 `cols - 1`（最后一列），而不是「上一轮锚点所在的列」？

**答案**：因为 `next_new_text_ix` 每轮都取 `new.len()`，锚点列**就是**上一轮的最后一列（上一轮锚点扫描读的正是 [L188](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L188) 的 `next_new_text_ix - self.new_text_ix` 列，即相对列号的最后一列）。两者是同一列，取 `cols - 1` 计算最简单。

**练习 2**：第一轮 `push_new("he")` 结束后矩阵有几列？`push_new("llo world")` 进行的 `swap_columns` 参数是多少？

**答案**：第一轮 `resize` 到 `2 - 0 + 1 = 3` 列；第二轮进入时 cols=3，`swap_columns(0, 2)`，随后 `resize` 到 `11 - 2 + 1 = 10` 列。

**练习 3**：如果删掉 `swap_columns` 那一行，直接 `resize` 后填表，第二轮算出的分数会基于哪个错误的初始化列？

**答案**：第 0 列仍是 `new()` 写入的删除初始化列 `[0, -20, -40, -60, -80, -100]`，即假装「前面一个新字符都没对齐过」。所有相对列的分数会整体错位，锚点选择与回溯随之失真——不过第 0 行仍会用绝对 `j` 计分（[L164](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L164) 不受交换影响），所以错误不会表现为崩溃，而是悄悄给出劣质 diff。

### 4.3 增量填表主循环：adjacent_columns_mut 与三候选打分

#### 4.3.1 概念说明

填表循环要做的事，教科书上写作：

\[ S(i, j) = \max \begin{cases} S(i, j-1) + c_{\text{ins}} & \text{（插入 } new[j\!-\!1]\text{，左格）} \\ S(i-1, j) + c_{\text{del}} & \text{（删除 } old[i\!-\!1]\text{，上格）} \\ S(i-1, j-1) + 1.8^{\min(\lfloor r/4 \rfloor,\ 16)} & \text{若 } old[i\!-\!1] = new[j\!-\!1] \\ -\infty & \text{（错配时禁止走对角）} \end{cases} \]

其中 \( c_{\text{ins}} = -1 \)、\( c_{\text{del}} = -20 \)（u2-l2），\( r \) 是结束于 \( (i,j) \) 的连续相等游程长度。边界 \( S(i,0) = -20i \) 在 `new()` 里一次算死，\( S(0,j) = -j \) 由本循环每列现算。

工程上的问题：算第 \( j \) 列需要**读上一列**（插入候选）同时**写当前列**（还要读当前列上一行做删除候选）。同一个 `Vec` 不能同时可变借用两段——除非用 `split_at_mut`。这正是 `adjacent_columns_mut` 存在的意义。

#### 4.3.2 核心流程

```text
for j in (new_text_ix, new.len()]:              # 每个新字符一列
    relative_j = j - new_text_ix                # 本轮矩阵内的列号（1..=K）
    current_equal_runs 全部清零                  # 4.4 节讲为什么必须清
    (previous_scores, current_scores) ← adjacent_columns_mut(relative_j)
        # previous_scores: 第 relative_j - 1 列，只读 —— 提供插入候选与游程前驱
        # current_scores:  第 relative_j 列，可写 —— 从上往下逐行填
    current_scores[0] ← j × (-1)                # 第 0 行：绝对 j，不能用 relative_j！
    for i in 1..=M:
        insertion = previous_scores[i] - 1      # 左格
        deletion  = current_scores[i-1] - 20    # 上格（当前列已填部分）
        equality  = 若字符相等: previous_scores[i-1] + 游程奖励; 否则 -∞
        current_scores[i] ← max(三者)
    swap(previous_equal_runs, current_equal_runs)  # 本列游程转正为「上一列」
```

注意两个下标体系并行：**绝对列号 `j`** 用于 `self.new[j-1]` 取字符和第 0 行计分；**相对列号 `relative_j`** 用于矩阵内寻址（因为矩阵每轮都被滚动重置过）。

#### 4.3.3 源码精读

- [streaming_diff.rs:L155-L158](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L155-L158)：外层循环变量 `j` 是**绝对**列号，`relative_j = j - self.new_text_ix` 把它折算成矩阵内列号；`new_char = self.new[j - 1]` 取本列对应的新字符。
- [streaming_diff.rs:L159-L162](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L159-L162)：先做四个局部绑定——`old`（只读）、`previous_equal_runs`（只读）、`current_equal_runs`（可写）、再调 `self.scores.adjacent_columns_mut(relative_j)` 拿到 `(previous_scores, current_scores)`。这是 Rust 借用检查器下的**字段级借拆分**：四个名字分别借 `self` 的四个不相交字段，互不冲突，于是内层循环可以毫无负担地同时用它们。`adjacent_columns_mut` 的实现（[L78-L90](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L78-L90)，u2-l1 讲过）用一次 `split_at_mut` 把 `cells` 劈成两段：返回元组 `(前一列的只读切片, 当前列的可变切片)`。类型签名把 DP 的数据流方向写死了：**当前列依赖前一列（只读）和当前列自己已填的上半段（可写）**，而第 0 列（边界列）被守卫条件 `current_col == 0` 挡在外面——边界列只许被 `swap_columns` 搬运，不许被填表覆写。
- [streaming_diff.rs:L164](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L164)：`current_scores[0] = j as f64 * Self::INSERTION_SCORE`。这里用**绝对** `j`：\( S(0, j) = -j \) 表示「新文本前 \( j \) 个字符全部靠插入」，代价与锚点无关、与分块无关。如果误用 `relative_j`，第二轮的行 0 会从 -1 重新起算，分数整体偏移，锚点比较就跨轮不可比了。
- [streaming_diff.rs:L165-L167](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L165-L167)：两个「走格子」候选。插入候选读 `previous_scores[i]`（左格，上一列同行）；删除候选读 `current_scores[i - 1]`（上格，本列上一行——这解释了为什么内层循环必须**从上到下**顺序填）。
- [streaming_diff.rs:L168-L176](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L168-L176)：相等候选。字符相等时，游程长度取「上一列对角格的游程 + 1」（[L169](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L169)），写入 `current_equal_runs[i]`（[L170](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L170)），再按 \( 1.8^{\min(\lfloor r/4 \rfloor,\ 16)} \) 计奖励（[L172-L173](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L172-L173)，调参理由见 u2-l2）；字符不等时取 `f64::NEG_INFINITY`（[L175](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L175)）——数学上的「禁止」，让 [L178](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L178) 的 `max` 永远选不到错配对角。
- [streaming_diff.rs:L178](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L178)：三个候选取最大写入当前格。注意 `insertion_score.max(deletion_score).max(equality_score)` 是 `f64::max`，与回溯阶段 `max_by_key(OrderedFloat(...))` 的并列平局规则不同——填表阶段并列只影响「分数值」（相同），走哪条路交给回溯阶段决定（u3-l3 的主题）。

用 4.1.4 的手算表核对一遍递推（第二轮 rel 1 列、j=3、`'l'`、边界列 `[-2, 0, 2, -18, -38, -58]`）：

- 行 0：`3 × (-1) = -3` ✓（绝对 j 的直接证据）
- 行 3（`old[2]='l'` 等于 `'l'`）：插入 = `previous[3] - 1 = -19`；删除 = `current[2] - 20 = 1 - 20 = -19`；相等：游程 = `previous_runs[2] + 1 = 2 + 1 = 3`（继承边界列的游程 2，见 4.4），指数 \( \min(3/4, 16) = 0 \) → 奖励 \( 1.8^0 = 1 \)，得分 = `previous[2] + 1 = 2 + 1 = 3`；取最大 **3** ✓
- 行 4（`old[3]='l'` 也等于 `'l'`）：相等候选的游程前驱是 `previous_runs[3]`（**不是**行 3 刚算出的 3！因为游程沿**对角线**继承），边界列该值为 0 → 游程 1 → 得分 = `-18 + 1 = -17`；删除 = `current[3] - 20 = 3 - 20 = -17`；并列取 -17 ✓

第二点值得停下来体会：两条对角线（`(3,j-1)→(4,j)` 与 `(2,j-1)→(3,j)`）上的匹配互不共享游程，因为它们对齐的是旧文本里**不同位置**的 `'l'`。

#### 4.3.4 代码实践

**1. 实践目标**：在 trace 里找到一个「插入候选击败相等候选」的具体格子，体会打分不是「有匹配就用匹配」。

**2. 操作步骤**：沿用 4.1.4 的练习环境。在相等分支（[L168-L176](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L168-L176) 对应处）加一行（示例代码）：

```rust
eprintln!("     i={i} ins={insertion_score} del={deletion_score} eq={equality_score} run={}", 
    current_equal_runs[i]);
```

重点看第二轮 j=8（新字符 `'o'`，即 `" world"` 里的 `'o'`）、i=5（旧文本的 `'o'`）这一格。

**3. 需要观察的现象**：该格三个候选分中，插入候选（沿上一列行 5 继续插入）应高于相等候选（对齐到旧文本末尾的 `'o'`）。

**4. 预期结果**：插入 ≈ 3.6、相等 = 2.8（前驱列行 4 的 1.8 加上游程奖励 1），最大值 3.6 记入当前格（手算值，**待本地验证**）。直觉解释：走到这里时 DP 认为「把 `'o'` 继续当作插入文本的一部分」比「回头对齐旧文本的 `'o'`」总分更高，因为对齐那条路的前驱分数太低。这正是 u2-l2 讲的「竞争裕度」在具体格子上的样子。

#### 4.3.5 小练习与答案

**练习 1**：内层循环为什么必须从 `i = 1` 递增到 `old.len()`，能不能倒序填？

**答案**：不能。删除候选读 `current_scores[i - 1]`（[L167](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L167)），即当前列的上一行；倒序填时上一行还是陈旧值/零，删除候选全部失真。列内自上而下、列间自左而右，是这套 DP 的两个固定方向。

**练习 2**：`adjacent_columns_mut` 为什么拒绝 `current_col == 0`？

**答案**：第 0 列是滚动进来的边界列（锚点列的历史分数），填表只许读它、不许写它；若允许传入 0，`split_at_mut` 的切点计算还会下溢（`previous_col_start = current_col_start - self.rows`，u2-l1 分析过）。守卫条件同时挡住了逻辑错误与算术错误。

**练习 3**：第 0 行 `current_scores[0] = j as f64 * Self::INSERTION_SCORE` 与内层循环的插入候选（[L166](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L166)）是什么关系？

**答案**：第 0 行是递推的边界种子的逐列重述：\( S(0,j) = S(0,j-1) + c_{\text{ins}} \) 反复展开就是 \( -j \)。它不能用 `previous_scores[0] - 1` 顺手算吗？可以，数值相同；但用绝对 `j` 直接种子化，让正确性不依赖「上一列第 0 行恰好等于 \(-(j-1)\)」这个跨列约定，也避免了相对/绝对列号混用的坑。

### 4.4 双缓冲 equal_runs：fill(0)、对角继承与 mem::swap

#### 4.4.1 概念说明

相等候选的奖励 \( 1.8^{\min(\lfloor r/4 \rfloor,\ 16)} \) 依赖连续相等游程 \( r \)：结束于 \( (i,j) \) 的对角线上连续匹配了多少个字符。递推定义很简单：

\[ r(i, j) = \begin{cases} r(i-1, j-1) + 1 & \text{若 } old[i\!-\!1] = new[j\!-\!1] \\ 0 & \text{否则} \end{cases} \]

如果不缓存，每个格子要沿对角线回溯数匹配，最坏 \( O(M) \) per 格。缓存方案：给每列配一条长度 \( M+1 \) 的游程数组，与分数列平行维护。于是又出现「读上一列、写当前列」的双借用需求——解法与分数矩阵同款：**两个数组 + 每列换位**，即双缓冲。

`StreamingDiff` 的两个字段（[L120-L121](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L120-L121)）：

```rust
previous_equal_runs: Vec<u32>,   // 上一列的游程（只读）
current_equal_runs: Vec<u32>,    // 当前列的游程（可写）
```

#### 4.4.2 核心流程

一列之内与一列之间的缓冲状态流转：

```text
处理第 j 列之前：
    previous_equal_runs = 第 j-1 列的游程（fresh）
    current_equal_runs  = 第 j-2 列的游程（陈旧！）

处理第 j 列：
    current_equal_runs.fill(0)          # ① 清零：抹掉陈旧值
    匹配格：current_equal_runs[i] = previous_equal_runs[i-1] + 1
    不匹配格：保持 0（① 保证了这一点）
    mem::swap(previous_equal_runs, current_equal_runs)   # ② 换位

处理第 j+1 列之前：
    previous_equal_runs = 第 j 列的游程（fresh）
    current_equal_runs  = 第 j-1 列的游程（陈旧，等下一轮 ① 清零）
```

跨 `push_new` 调用：每轮填表的最后一次 `mem::swap` 发生在**末列**（锚点列）之后，所以函数返回时 `previous_equal_runs` 恰好持有锚点列的游程——下一轮 `push_new` 的第一列直接从中继承，与分数边界列的滚动逻辑严丝合缝。**整个游程状态机与「把全表一次性重算」在数学上等价**，分块只是改变了计算的切分方式，没有改变递推本身。

#### 4.4.3 源码精读

- [streaming_diff.rs:L144-L145](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L144-L145)：`new()` 里两个缓冲都初始化为 `vec![0; old_len + 1]`——长度与矩阵列一致，初始全 0（还没有任何匹配）。
- [streaming_diff.rs:L156](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L156)：`self.current_equal_runs.fill(0)`。**这行是本节的题眼**。`current_equal_runs` 此刻装的是两列前的陈旧游程（见 4.4.2 流程）；填表循环只在匹配格写 `current_equal_runs[i]`，不匹配格一个字都不写——若不清零，下一列读 `previous_equal_runs[i-1]` 时就会读到两列前的旧值，把早已断掉的对角游程「复活」。清零之后，「没写」恰好等于「游程为 0」，语义闭合。
- [streaming_diff.rs:L169-L170](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L169-L170)：匹配格的读写对：读 `previous_equal_runs[i - 1]`（对角前驱），加一后写入 `current_equal_runs[i]`。注意读写发生在 `adjacent_columns_mut` 拿走 `self.scores` 可变借用**之后**仍能编译通过，靠的正是 4.3 讲的字段级借拆分。
- [streaming_diff.rs:L181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L181)：`std::mem::swap(&mut self.previous_equal_runs, &mut self.current_equal_runs)`——列处理完毕，刚写完的缓冲「转正」为下一列的 previous。交换的是两个 `Vec` 的内部指针，\( O(1) \)，不搬数据。

用 4.1.4 第二轮的手算数据核对游程流转（进入时 `previous_equal_runs` = 锚点列 j=2 的游程，即 `[0, 0, 2, 0, 0, 0]`）：

| 列 | 匹配格 | 写入的游程 | 奖励指数 |
|---|---|---|---|
| j=3 `'l'` | (i=3)（继承对角 runs[2]=2）、(i=4)（runs[3]=0） | runs[3]=3, runs[4]=1 | 都是 \( \lfloor r/4 \rfloor = 0 \) |
| j=4 `'l'` | (i=3)（runs[2]=0）、(i=4)（继承 runs[3]=3） | runs[3]=1, runs[4]=4 | 行 4 出现 \( \lfloor 4/4 \rfloor = 1 \)，首次拿到 1.8 奖励（该格得分 4.8 的由来） |
| j=5 `'o'` | (i=5)（继承 runs[4]=4） | runs[5]=5 | 指数仍为 1（\( \lfloor 5/4 \rfloor \)） |
| j=6 `' '` | 无匹配 | 全 0 | — |

特别注意 j=4 行 3：它**没有**继承 j=3 行 3 的游程 3（那是 `runs[3]`，本行读的是 `runs[2]`），重新从 1 开始——对角继承的又一实证。

#### 4.4.4 代码实践

**1. 实践目标**：观察 `fill(0)` 的必要性——构造一个「若不清零、结果会变」的反例。

**2. 操作步骤**：沿用练习 crate。

- 第一步：在 [L181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L181) 的 `mem::swap` 之后加打印（示例代码）：`eprintln!("   runs after j={j}: {:?}", self.previous_equal_runs);`，跑通原版，确认与 4.4.3 的表格一致。
- 第二步（进阶）：注释掉 [L156](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L156) 的 `fill(0)`，再跑，对比两版输出。

**3. 需要观察的现象**：反例并不好找——游程要残留到足够大（残留值 +1 后跨过 4 的倍数，指数档位才会跳变）才影响分数。如果随手试的例子里两版输出完全相同，不要惊讶，先分析为什么（提示：指数是 \( \lfloor r/4 \rfloor \)，小游程全在同一档）。

**4. 预期结果**：原版输出与 4.4.3 表格一致；删掉 `fill(0)` 后，多数简单例子输出不变（同档位），但可以构造出变差的例子（**待本地验证**）。这个实践的价值不在「必现差异」，而在让你亲手论证「清零把『未写』定义为 0」这一语义契约。

#### 4.4.5 小练习与答案

**练习 1**：`push_new` 返回的那一刻，`previous_equal_runs` 里装的是哪一列的游程？为什么下一轮调用可以直接用它？

**答案**：装的是本轮**末列**（也就是新锚点列）的游程——因为每列处理完都 `mem::swap`，末列也不例外。下一轮的第一列（锚点列 + 1）计算匹配格时按定义需要「锚点列的对角前驱游程 + 1」，正是它。分数边界列与游程缓冲在「锚点列」这一点上对齐，两者配套滚动。

**练习 2**：为什么不干脆用一个 `Vec<u32>`（单缓冲），每列先算好新数组再整体赋值回去？

**答案**：单缓冲逐元素赋值也能算对（新值基于上一列的 `runs[i-1]`，若从上往下原地更新会覆盖还未使用的 `runs[i-1]`——所以必须倒序或借助临时数组），但要么引入每列 \( O(M) \) 的临时分配，要么把更新顺序约束写得更隐蔽。双缓冲 + `mem::swap`（\( O(1) \) 指针交换）把「读旧写新」做成显式结构，正确性一目了然。这与分数矩阵的 `adjacent_columns_mut` 是同一个设计思想。

**练习 3**：`MAX_EQUALITY_EXPONENT = 16` 在哪个环节起作用？它和双缓冲有关系吗？

**答案**：在 [L172](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L172) 的 `cmp::min(equal_run as i32 / 4, Self::MAX_EQUALITY_EXPONENT)`：把指数封顶在 16（u2-l2 讲过这是防 `f64` 溢出的安全阀，\( 1.8^{16} \approx 1.2 \times 10^4 \)，再大就有溢出风险）。它与双缓冲没有直接关系——封顶发生在「读出游程、计算奖励」这一步；但注意 `current_equal_runs` 的类型是 `u32`，游程本身**不**封顶（只有指数封顶），所以缓冲里可能存很大的值，跨列继承时依旧正确。

## 5. 综合实践

把本讲的三段式、滚动复用、双缓冲串成一次完整的「示波器实验」：

1. **搭环境**：按 4.1.4 步骤一、二建好 `streaming-trace` 练习 crate（复制 [streaming_diff.rs:L1-L522](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1-L522)，追加 `main`）。
2. **加打印**：把 4.1.4、4.2.4、4.3.4 的三组 `eprintln!` 全部装上。
3. **跑基准场景**：`old = "hello"`，先 `push_new("he")` 再 `push_new("llo world")`，最后 `finish()`。逐条核对下面的检查单（答案都能在正文手算表里找到）：
   - 第一次调用 `swap_columns(0, 0)` 是否空操作？
   - 第二次调用的边界列是否等于第一次的末列 `[-2, 0, 2, -18, -38, -58]`？
   - 第二次调用第一列（j=3）的行 0 是否为 -3（绝对 j 证据）？行 3 是否为 3（游程跨调用继承证据：2 + 1 = 3）？
   - j=8 的 i=5 格是否插入候选（≈3.6）胜过相等候选（2.8）？
   - 两轮 anchor 是否依次推进到 `(2, 2)` 与 `(5, 11)`？
4. **round-trip 验证**：把测试模块里的参考解释器 [apply_char_operations（L1104-L1124）](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) 复制进练习 crate，拼接三轮返回的操作并断言：

   ```rust
   let mut diff = StreamingDiff::new("hello".to_string());
   let mut ops = diff.push_new("he");
   ops.extend(diff.push_new("llo world"));
   ops.extend(diff.finish());
   assert_eq!(apply_char_operations("hello", &ops), "hello world");
   ```

   预期断言通过，且 `ops` 为 `[Keep { bytes: 2 }, Keep { bytes: 3 }, Insert { text: " world" }]`（手算值，**待本地验证**）。
5. **加分项（分块无关性）**：模仿 [random_streaming_diff（L963-L981）](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L963-L981) 的分块循环，把 `"hello world"` 分别按每块 1、2、4、11 个字符推送（注意按字符边界切，参考它的 `is_char_boundary` 处理），断言每次 round-trip 都得到 `"hello world"`。你会在 trace 里看到：矩阵每次被滚动成不同大小，但拼接后的操作序列总能重建新文本——这正是 u1-l1 介绍的流式不变量，而本讲让你看到了它成立的具体机制。

## 6. 本讲小结

- `push_new` 是清晰的三段式：**滚动**（`swap_columns(0, cols-1)` 把上一轮末列换到第 0 列，`resize` 扩到本块长度 + 1）→ **增量填表**（每字符一列，三候选取 max）→ **锚定回溯**（末列选最大分行，回溯出操作，最后才更新锚点）。
- 滚动的本质是 DP 的列依赖性：只需要相邻两列，所以矩阵大小只跟「单块长度」有关（空间 \( O(M \times K_{\max}) \)），与新文本总长无关；整列交换 \( O(M) \)，优于整体搬移 \( O(M \times \text{cols}) \)。
- `adjacent_columns_mut` 用 `split_at_mut` 返回 `(前一列只读, 当前列可写)`，把数据流方向编码进类型；填表对每列写满全部 \( M+1 \) 行，所以交换/扩容残留的陈旧值永远读不到。
- 第 0 行用**绝对** `j` 计分（\( S(0,j) = -j \)），保证分数跨轮可比；填表内层必须自上而下（删除候选依赖当前列上一行）。
- `previous_equal_runs`/`current_equal_runs` 双缓冲：每列 `fill(0)` 把「未写」定义为游程 0，匹配格对角继承 `previous[i-1] + 1`，列尾 `mem::swap` 转正；函数返回时恰持有锚点列游程，下一轮无缝续算。
- 三候选打分不是「有匹配就走匹配」：j=8 的格子插入 3.6 > 相等 2.8，竞争由前驱分数决定——u2-l2 的打分模型在具体格子上兑现。

## 7. 下一步学习建议

本讲刻意把 `backtrack` 当黑盒：它如何从新锚点反向走 DP 决策、`OrderedFloat` 如何在浮点分数上做并列平局（练习中 j=4 行 4 的删除/相等并列正是伏笔）、`pending_insert` 如何把连续插入合并成一个 `Insert`——这些都是下一讲 **u3-l3「回溯与锚点：从最优终点还原编辑脚本」** 的内容。读完 u3-l3 后建议紧接着读 **u3-l4（finish 与流式语义）**，那里会用随机分块测试证明本讲观察到的「分块无关性」是可验证的不变量，而不是巧合。若想趁热动手，可以回到 4.4.4 的进阶实践：构造一个让 `fill(0)` 缺席时结果改变的用例，那会让你对双缓冲的理解从「记住流程」升级到「能推理边界」。
