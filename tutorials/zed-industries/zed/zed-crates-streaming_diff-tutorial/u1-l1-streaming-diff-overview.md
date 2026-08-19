# 第 1 讲：streaming_diff 是什么：流式差异计算的定位与运行方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 `streaming_diff` 这个 crate 解决什么问题：**旧文本固定、新文本分块流式到达时，边接收边计算字符级差异，并能把字符级差异折叠成行级差异**。
2. 说出它在 Zed 工作区中的位置、它依赖哪些外部 crate（`rope`、`ordered-float`），以及哪些真实功能（agent 的编辑会话、agent_ui 的代码生成）在使用它。
3. 独立完成三件事：
   - `cargo test -p streaming_diff`（并知道预期有多少个测试）；
   - `cargo bench -p streaming_diff --no-run`（确认基准可编译）；
   - `cargo tree -p streaming_diff`（观察依赖树）。
4. 对照源码说出这个 crate 的库根文件在哪里、整个 crate 为什么只有一个源文件、公开 API（对外暴露的类型）有哪些。

本讲是整个学习手册的第一篇，不要求你预先了解 diff 算法细节——那些是后面几讲的内容。本讲只解决"它是什么、放在哪、怎么跑起来"。

## 2. 前置知识

### 2.1 什么是 diff（差异）

给定一份**旧文本**（old）和一份**新文本**（new），diff 算法输出一个**操作序列**，把这个操作序列应用到旧文本上就能得到新文本。最常见的三种操作是：

- **Insert（插入）**：在新位置写入一段新文本；
- **Delete（删除）**：跳过旧文本中的一段，不把它带进结果；
- **Keep（保留）**：把旧文本的一段原样复制到结果里。

例如 `"Hello, world!"` 变成 `"Hello, Rust!"`，可以表达为：保留前 7 字节（`Hello, `）、删除 5 字节（`world`）、插入 `Rust`、保留 1 字节（`!`）。本讲末尾会看到这个例子正是 crate 自带的测试之一。

### 2.2 什么是"流式"（streaming）

传统 diff（比如 `git diff`）要求**同时拿到完整的旧文本和新文本**才开始计算。但在一个 AI 编辑场景里，新文本是模型一个 token 一个 token 吐出来的：

```
旧文本（一次性已知）:  "aaaa\nbbbb"
新文本（分块到达）:    "aaa" → "a\nBB" → "BB"
```

如果等全部到达再 diff，用户就要干等；如果每来一小块就对"旧 vs 目前已到的新文本"整体重算一遍，计算量又是平方级的。`streaming_diff` 的做法是**增量**计算：每来一块，只在新旧文本的"未结算区间"上推进动态规划（Dynamic Programming，DP）矩阵，并立刻返回这段区间内已经确定的差异操作。它的核心不变量是：**无论新文本被切成什么样的小块，把每次返回的操作按顺序拼起来、应用到旧文本上，最终一定等于完整的新文本**（这个性质的细节在第 10 讲 `finish` 与流式语义中展开）。

### 2.3 Rust workspace 与 crate

Zed 是一个由几百个 crate 组成的 **Cargo workspace**。每个 crate 是一个独立编译单元，有自己的 `Cargo.toml`。工作区根目录的 `Cargo.toml` 用 `crates/xxx = { path = "crates/xxx" }` 这样的条目把成员登记进来，成员之间就可以按名字互相依赖。

### 2.4 rope 是什么

`rope` 是一种为编辑器设计的字符串数据结构：把文本组织成树状的块，使得"在第 100 万个字符处插入"这样的操作不必移动整段内存。Zed 自己实现了 `rope` crate，`streaming_diff` 的行级部分（`LineDiff`）借用它的 `Point`（行/列坐标）类型来做字节偏移与行列的换算。本讲只需知道"rope = Zed 的文本表示，Point = 行列坐标"即可。

## 3. 本讲源码地图

本讲涉及的关键文件（均为实际存在的文件）：

| 文件 | 作用 |
| --- | --- |
| `crates/streaming_diff/Cargo.toml` | crate 清单：声明库名、库根路径、运行依赖、开发依赖和 criterion 基准 |
| `crates/streaming_diff/src/streaming_diff.rs` | **库根文件**，整个 crate 的全部实现和 16 个单元测试都在这一个文件里（共 1125 行） |
| `crates/streaming_diff/benches/streaming_diff.rs` | criterion 基准：用四种"编辑形态"的夹具度量 `push_new` 和 `finish` 的吞吐 |
| `crates/streaming_diff/LICENSE-GPL` | GPL-3.0-or-later 许可证文本 |
| `Cargo.toml`（仓库根） | 工作区清单，登记了 `streaming_diff` 及其依赖 `rope`、`ordered-float` 的来源 |
| `crates/agent/src/tools/edit_session.rs` | 真实调用方之一：agent 编辑工具在流式输出新文本时用 `StreamingDiff` |
| `crates/agent_ui/src/buffer_codegen.rs` | 真实调用方之二：同时使用 `StreamingDiff` 与 `LineDiff` 维护行级差异 |

一个诚实的说明：`crates/streaming_diff/` 目录下**没有**自己的 `README.md`；仓库根目录的 `README.md` 介绍的是 Zed 编辑器整体，与本 crate 无直接关系。因此本讲以源码和 `Cargo.toml` 为唯一事实来源。

## 4. 核心概念与源码讲解

本讲覆盖两个最小模块：**Cargo.toml** 与 **crate 目录结构**，外加一个"它在 Zed 中被谁使用"的定位小节。

### 4.1 模块一：Cargo.toml——这个 crate 由什么构成

#### 4.1.1 概念说明

`Cargo.toml` 是 crate 的"身份证 + 采购单"。它回答四个问题：

1. 这个 crate 叫什么、版本号多少、用什么许可证；
2. 库的入口文件在哪里（`[lib] path`）；
3. 正式发布时依赖谁（`[dependencies]`）；
4. 只在测试/基准里依赖谁（`[dev-dependencies]`）以及有哪些基准（`[[bench]]`）。

区分"运行依赖"和"开发依赖"很重要：`streaming_diff` 编译成库交付给 `agent`、`agent_ui` 时，只需要 `rope` 和 `ordered-float` 两个依赖，非常轻；而 `criterion`（基准框架）、`rand`（随机数）、`util`（测试工具）只在跑测试时才参与编译，不会传递给下游。

#### 4.1.2 核心流程

阅读任何 crate 的 `Cargo.toml`，建议按这个顺序问自己：

```text
1. [package]   → 名字、版本、许可证
2. [lib] path  → 库根文件在哪（决定我从哪个文件开始读源码）
3. [dependencies] → 运行时依赖（越少越好，说明 crate 越独立）
4. [dev-dependencies] → 只为测试存在的依赖
5. [[bench]] / [[test]] / [[example]] → 还有哪些附属目标要编译
```

对应到 `streaming_diff`，这条链是：名字 `streaming_diff` → 库根 `src/streaming_diff.rs` → 运行依赖 `ordered-float` + `rope` → 开发依赖 `criterion` + `rand` + `util` → 一个基准目标 `streaming_diff`。

#### 4.1.3 源码精读

先看整个文件（很短，值得整体读一遍）：

[Cargo.toml:L1-L26](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L1-L26) —— 这是 `streaming_diff` crate 的完整清单，一共只有 26 行。

逐段拆开：

[Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L11-L12) —— 声明库根文件是 `src/streaming_diff.rs`。这符合 Zed 仓库的编码规范：不用默认的 `lib.rs`，也不用 `mod.rs`，而是让库根文件与 crate 同名，"见文件名知 crate"。

[Cargo.toml:L14-L16](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L14-L16) —— 运行依赖只有两个：`ordered-float`（给浮点数提供全序比较，后面回溯时用 `OrderedFloat` 包住分数才能取 `max_by_key`）和 `rope`（Zed 的文本结构，提供 `Point`/`Rope`/`TextSummary`）。两者都写成 `workspace = true`，即版本由工作区根 `Cargo.toml` 统一管理。

[Cargo.toml:L18-L21](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L18-L21) —— 开发依赖：`criterion`（基准框架）、`rand`（随机化测试）、以及带 `test-support` feature 的 `util`（提供 `RandomCharIter`，用来生成随机 Unicode 文本，测试对多字节字符的健壮性）。

[Cargo.toml:L23-L25](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L23-L25) —— 声明一个名为 `streaming_diff` 的基准目标，源文件在 `benches/streaming_diff.rs`；`harness = false` 表示不使用内置测试框架，由 criterion 自己的 `criterion_main!` 接管 main 函数。

依赖在工作区根的登记处（供交叉核对）：

- [Cargo.toml:L445](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/Cargo.toml#L445)（仓库根）—— `rope = { path = "crates/rope" }`，本地 path 依赖；
- [Cargo.toml:L464](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/Cargo.toml#L464)（仓库根）—— `streaming_diff = { path = "crates/streaming_diff" }`，这就是 `agent`、`agent_ui` 能写 `use streaming_diff::...` 的原因；
- [Cargo.toml:L730](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/Cargo.toml#L730)（仓库根）—— `ordered-float = "2.1.1"`，唯一的第三方运行依赖，来自 crates.io。

#### 4.1.4 代码实践：用 cargo tree 观察依赖

1. **实践目标**：亲眼确认"运行依赖只有 `rope` + `ordered-float`"，并区分开测试期的依赖。
2. **操作步骤**：在 Zed 仓库根目录执行：
   ```bash
   cargo tree -p streaming_diff
   ```
3. **需要观察的现象**：树的第一层应只有 `ordered-float` 和 `rope` 两个分支；`rope` 下面会带出 `arrayvec`、`sum_tree`、`text` 等 Zed 内部 crate；`criterion`、`rand`、`util` 不应出现在这棵树里（它们是 dev-dependencies，只有 `cargo tree -p streaming_diff --edges dev` 之类带 dev 的视图才会显示）。
4. **预期结果**：得到一棵以 `streaming_diff v0.1.0` 为根、以 `ordered-float` 和 `rope` 为两大分支的依赖树。具体输出形状随工作区版本变化，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `benches/streaming_diff.rs` 需要的 `criterion` 从 `[dev-dependencies]` 挪到 `[dependencies]`，会发生什么坏事？

**答案**：功能上基准仍能编译，但所有依赖 `streaming_diff` 的下游 crate（如 `agent`）也会被迫编译并链接 `criterion` 及其依赖树，拖慢整个工作区的构建，还会无谓地增大产物。dev-dependencies 的意义就是把"只有测试/基准需要的东西"隔离在正式构建之外。

**练习 2**：`Cargo.toml` 里没有任何 `[[test]]` 段，为什么 `cargo test -p streaming_diff` 仍然能找到测试？

**答案**：Cargo 对测试目标有默认约定：库 crate 中带 `#[cfg(test)] mod tests` 的代码会被内置测试框架（libtest）收集，无需在 `Cargo.toml` 里显式声明。`[[bench]]` 之所以要声明，是因为它设置了 `harness = false`，需要显式告诉 Cargo 这个目标的源文件与接管方式。

**练习 3**：`edition.workspace = true` 与 `ordered-float.workspace = true` 各自"继承"的是什么？

**答案**：前者继承工作区统一的 Rust edition（语义由根 `Cargo.toml` 的 `[workspace.package]` 决定），后者继承 `[workspace.dependencies]` 中登记的版本与来源（`ordered-float = "2.1.1"`）。两种写法都为了避免几十个 crate 各写一份版本号、随后失同步。

### 4.2 模块二：crate 目录结构与库根文件

#### 4.2.1 概念说明

打开 `crates/streaming_diff/` 目录，结构是：

```text
crates/streaming_diff/
├── Cargo.toml              # 4.1 已精读
├── LICENSE-GPL             # GPL-3.0-or-later 许可证文本
├── benches/
│   └── streaming_diff.rs   # criterion 基准（322 行）
└── src/
    └── streaming_diff.rs   # 库根文件：实现 + 测试（1125 行）
```

这个 crate 的显著特点是**单文件**：没有 `lib.rs`，没有子模块目录，全部实现都在 `src/streaming_diff.rs` 里。对一个定位清晰、体量小的算法 crate 来说，这是合理选择——所有相关类型（打分矩阵、两种操作枚举、两个 diff 结构）在一个文件里互相配合，读源码时不需要跳来跳去。

文件内部的"分区"大致是：

| 行区间（当前 HEAD） | 内容 | 可见性 |
| --- | --- | --- |
| L1–L8 | `use` 导入：`OrderedFloat`、rope 的 `Point/Rope/TextSummary`、`BTreeSet` 等 | 私有 |
| L10–L104 | `Matrix`：列主序打分矩阵及其 `resize/swap_columns/get/set`、`Debug` 实现 | **私有**（不对外暴露） |
| L106–L111 | `CharOperation`：字符级操作枚举 | **pub** |
| L113–L279 | `StreamingDiff`：核心算法（`new/push_new/backtrack/finish` 与打分常量） | **pub**（`backtrack` 私有） |
| L281–L286 | `LineOperation`：行级操作枚举 | **pub** |
| L288–L522 | `LineDiff`：把字符操作折叠成行操作的状态机，以及 `is_line_start/is_line_end` 辅助函数 | **pub** |
| L524–L1125 | `#[cfg(test)] mod tests`：16 个单元测试 + 随机化测试工具函数 | 仅测试期编译 |

也就是说，这个 crate 对外暴露的 API 面只有四个名字：`CharOperation`、`StreamingDiff`、`LineOperation`、`LineDiff`。这是很小、很稳定的接口。

#### 4.2.2 核心流程

库根文件顶部的导入可以直接告诉我们 crate 的"材料清单"：

```text
ordered_float::OrderedFloat   → 浮点分数的全序比较（回溯取最优时用）
rope::{Point, Rope, TextSummary} → 行列坐标、文本结构、文本摘要（LineDiff 用）
std::collections::BTreeSet    → 有序集合，存"被删的行号/被插的行号"
std::{cmp, fmt::Debug, ops::Range} → 比较、打印、表示插入区间的 Range
```

由此可以画出 crate 内部的两段式结构：

```text
                    ┌──────────────────────────────────────────┐
  old: String ──▶   │ StreamingDiff（字符级，纯 Vec<char>/f64） │ ──▶ Vec<CharOperation>
  new: &str 分块 ─▶  │   依赖私有 Matrix + OrderedFloat          │        （逐块返回）
                    └──────────────────────────────────────────┘
                                        │ 每块的 CharOperation
                                        ▼
                    ┌──────────────────────────────────────────┐
  old_text: &Rope ─▶│ LineDiff（行级，依赖 rope 的 Point/Rope）   │ ──▶ Vec<LineOperation>
                    │   把字符操作折叠成行级 Keep/Insert/Delete  │
                    └──────────────────────────────────────────┘
```

两段是**松耦合**的：`StreamingDiff` 完全不依赖 rope；`LineDiff` 也不依赖 `StreamingDiff`，它只消费 `CharOperation` 序列。你可以只用前一半，也可以把两者串起来。

#### 4.2.3 源码精读

[streaming_diff.rs:L1-L8](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1-L8) —— 库根文件的全部导入。只出现 `ordered_float` 和 `rope` 两个外部名字，印证 4.1 的依赖清单。

[streaming_diff.rs:L106-L111](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L106-L111) —— 公开类型之一 `CharOperation`：`Insert` 携带被插入的文本 `String`，`Delete`/`Keep` 只带一个字节数 `usize`。为什么 Insert 带内容而 Delete/Keep 只带长度？因为删除和保留的内容本来就来自旧文本，只需要指明"跳过/复制多少字节"；而插入的内容在旧文本里不存在，必须随操作携带。这个设计的详细语义是下一讲（u1-l2）的主题。

[streaming_diff.rs:L113-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L113-L128) —— 核心结构 `StreamingDiff` 的字段与四个打分常量。注意 `old`/`new` 都是 `Vec<char>`（按字符而非字节计数），`scores` 是私有 `Matrix`；常量 `INSERTION_SCORE = -1.`、`DELETION_SCORE = -20.` 等只在第二单元展开，本讲只需知道"插入便宜、删除昂贵"这个取向是为了配合"流式生成新内容"的场景。

[streaming_diff.rs:L149-L199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149-L199) —— `pub fn push_new(&mut self, text: &str) -> Vec<CharOperation>`：流式使用的入口。每次调用喂进一小块新文本，返回这一块"结算"出的差异操作。这是本 crate 最重要的公开方法。

[streaming_diff.rs:L276-L278](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L276-L278) —— `pub fn finish(self) -> Vec<CharOperation>`：流结束时调用，消费自身并返回最后一段操作。

[streaming_diff.rs:L281-L302](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L281-L302) —— 另两个公开类型：`LineOperation`（行级 Keep/Insert/Delete，只带行数）与 `LineDiff` 的状态字段（两个 `Point` 游标、两个 `BTreeSet<u32>` 行号集合、两个缓冲区）。

[streaming_diff.rs:L524-L528](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L524-L528) —— `#[cfg(test)] mod tests` 的开始。从 L524 到文件末尾 L1125 全部是测试与测试工具，正式构建不会编译它们。

[streaming_diff.rs:L1037-L1050](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1037-L1050) —— `test_apply_char_operations`：2.1 节那个 `"Hello, world!" → "Hello, Rust!"` 的例子就来自这里，它用辅助函数 `apply_char_operations` 把操作序列应用回旧文本来验证。

[benches/streaming_diff.rs:L7-L8](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L7-L8) —— 基准的固定随机种子 `SEED` 与分块大小 `CHUNK_SIZE = 512`（字节）：模拟"每 512 字节来一块"的流。

[benches/streaming_diff.rs:L78-L81](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L78-L81) —— 注释解释了为什么夹具体积要保持克制：`StreamingDiff` 在几十 KB 的替换文本上会被刻意压满、变得非常慢；这些尺寸代表真实的 `edit_file` 新旧文本块，又足以跨越"一帧预算"量级的 CPU 工作。

[benches/streaming_diff.rs:L320-L321](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L320-L321) —— `criterion_group!` / `criterion_main!` 收尾，注册 `streaming_diff_push_new` 与 `streaming_diff_finish` 两组基准，对应 `Cargo.toml` 里 `harness = false` 的自接管 main。

#### 4.2.4 代码实践：数一数公开 API 与测试

这是一个纯源码阅读型实践，不需要编译：

1. **实践目标**：亲手确认"crate 对外只暴露 4 个名字、自带 16 个测试"，建立对库边界的直觉。
2. **操作步骤**：
   - 在 `crates/streaming_diff/src/streaming_diff.rs` 中搜索 `^pub `（行首的 `pub`），把命中的项分类：`pub enum` / `pub struct` / `pub fn`；
   - 再搜索 `#[test]`，数一数个数；
   - 对照本讲 4.2.1 的表格，核对行区间。
3. **需要观察的现象**：`pub` 项应恰好落在 `CharOperation`、`StreamingDiff`（及其 `new/push_new/finish`）、`LineOperation`、`LineDiff`（及其 `push_char_operations/push_char_operation/finish/line_operations`）这几个名字上；`Matrix` 及其所有方法都没有 `pub`。
4. **预期结果**：`#[test]` 共 **16** 个（我用静态统计确认过：`test_random_diffs`、`test_apply_char_operations` 等 16 个函数）。因此 `cargo test -p streaming_diff` 应报告 `16 passed`（具体输出**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`Matrix` 为什么不做成 `pub`？如果暴露出去，调用方要付出什么代价？

**答案**：`Matrix` 是实现细节（列主序布局、`unsafe` 的列交换）。一旦 `pub`，它的内存布局和行为就成了公共契约，以后想换存储方式（例如改成行主序、或换成环形缓冲）都会构成破坏性变更，调用方还可能绕过 `StreamingDiff` 直接改分数破坏不变量。Rust 的默认私有性让库作者可以自由重构内部。

**练习 2**：库根文件叫 `src/streaming_diff.rs` 而不是默认的 `src/lib.rs`，这靠什么机制生效？这个约定来自哪里？

**答案**：靠 `Cargo.toml` 的 `[lib] path = "src/streaming_diff.rs"`（见 4.1.3 引用的 L11-L12）生效；它是 Zed 仓库 CLAUDE.md 中的编码规范——新 crate 应在 `Cargo.toml` 指定与 crate 同名的库根文件，保持命名一致、可读性更好。

**练习 3**：`StreamingDiff`（字符级）和 `LineDiff`（行级）谁是"生产者"、谁是"消费者"？依据是什么？

**答案**：`StreamingDiff` 是生产者，产出 `Vec<CharOperation>`；`LineDiff` 是消费者，其 `push_char_operation`/`push_char_operations`（L305-L313、L315-L353）接收 `CharOperation` 与旧文本 `&Rope`，把字符操作折叠为行级状态，最终经 `line_operations()` 产出 `Vec<LineOperation>`。依据是数据流向：`LineDiff` 的输入类型恰好是 `StreamingDiff` 的输出类型，而反过来不成立。

### 4.3 定位：谁在 Zed 里使用 streaming_diff

#### 4.3.1 概念说明

一个 crate 的"定位"最好由它的调用方来定义。工作区里有两处真实使用：

1. **`agent` crate 的 `edit_session`（编辑会话）**：Zed 的 agent（AI 助手）执行 `edit_file` 之类工具时，模型流式输出替换文本。`edit_session` 为每次流式编辑建立一个 `StreamingDiff`，随着文本到达不断拿到 `CharOperation`，把"已确定"的差异写回缓冲区，用户在编辑器里看到的是逐渐成型的编辑，而不是等待最后的整段替换。
2. **`agent_ui` crate 的 `buffer_codegen`（缓冲区代码生成）**：在生成/重放代码的场景中同时使用 `StreamingDiff`（字符级）与 `LineDiff`（行级）——前者给出精确的字符补丁，后者把补丁折叠成"哪几行被删、哪几行被插"，便于按行渲染 diff 高亮。

为什么两处都要"字符级 + 行级"两层？因为编辑器写回最小单位是编辑操作（字符级最精确），而 UI 上给人看的 diff 高亮以行为单位。`streaming_diff` 恰好把这两层都提供了。

#### 4.3.2 核心流程

典型的调用方生命周期（也是第 13 讲综合示例的骨架）：

```text
1. StreamingDiff::new(old_text)            # 锁定旧文本
2. 循环：每来一小块新文本 s：
       let ops = diff.push_new(s)          # 返回这块结算出的字符级操作
       应用 ops 到缓冲区 / 喂给 LineDiff
3. diff.finish()                           # 流结束，取回最后的操作
4. LineDiff::line_operations()             # 随时可取当前的行级差异
```

#### 4.3.3 源码精读

[edit_session.rs:L25](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs#L25) —— `agent` 的编辑会话只导入 `CharOperation` 与 `StreamingDiff` 两个名字：这个调用方只要字符级差异。

[edit_session.rs:L391-L395](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs#L391-L395) —— 编辑流水线的一个状态 `StreamingNewText`，字段里直接持有 `streaming_diff: StreamingDiff`，另带 `edit_cursor`、`reindenter`、`original_snapshot` 等，说明 diff 是"随流式输出演进的状态机"的一部分，而非一次性计算。

[edit_session.rs:L579-L580](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs#L579-L580) —— 进入该状态时用 `StreamingDiff::new(old_text_in_buffer)` 初始化：旧文本取自当前缓冲区快照，这就是"旧文本一次性固定"的来源。

[buffer_codegen.rs:L44](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent_ui/src/buffer_codegen.rs#L44) —— `agent_ui` 的导入列表更全：`CharOperation, LineDiff, LineOperation, StreamingDiff` 四个名字全都用上，说明它同时消费字符级与行级两层。

[buffer_codegen.rs:L739-L740](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent_ui/src/buffer_codegen.rs#L739-L740) —— 在代码生成流开始处同时创建 `StreamingDiff::new(selected_text.to_string())` 与 `LineDiff::default()`，正是 4.2.2 那张两段式结构图的现实版。

#### 4.3.4 代码实践：跟踪一条调用链（源码阅读型）

1. **实践目标**：沿着"工具输出 → StreamingDiff → 缓冲区"的方向走一遍调用链，感受流式 diff 在产品中的位置。
2. **操作步骤**：
   - 打开 [edit_session.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs)，定位 L579 附近 `StreamingNewText` 状态的建立；
   - 在同一文件内搜索 `push_new`，观察流式文本到达时如何调用并消费返回的 `CharOperation`；
   - 再打开 [buffer_codegen.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent_ui/src/buffer_codegen.rs) L739 附近，搜索 `push_char_operations` 与 `line_operations`，观察行级差异的读取时机。
3. **需要观察的现象**：两个调用方都遵循 4.3.2 的骨架：先 `new` 锁定旧文本，再在流式循环里 `push_new`，最后 `finish`；`buffer_codegen` 额外维护一个 `LineDiff`。
4. **预期结果**：能画出一条从"模型输出的一小块文本"到"编辑器缓冲区更新 + 行级 diff 高亮"的数据流草图。具体行号可能随代码演进漂移，若与本文不符，以仓库当前源码为准（**行号以本讲 HEAD `4c72447` 为准**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `edit_session` 只导入 `StreamingDiff`，而 `buffer_codegen` 还要导入 `LineDiff`？

**答案**：`edit_session` 关心的是"把新文本正确地写进缓冲区"，字符级操作已足够；`buffer_codegen` 要把差异呈现给用户（按行高亮的 diff UI），因此还要把字符操作折叠成行操作，需要 `LineDiff`/`LineOperation`。

**练习 2**：`StreamingDiff::new` 的参数是 `old: String`（按值），而 `LineDiff::push_char_operation` 每次都借用 `old_text: &Rope`。为什么会有这种差别？

**答案**：`StreamingDiff` 需要长期持有旧文本（存成 `Vec<char>` 反复比对，跨越多次 `push_new`），所以一次性拿走所有权；`LineDiff` 不保存旧文本，只在处理每个操作时需要临时查询旧文本的行列信息（如行尾判定、字节偏移换算），借用 `&Rope` 即可，把文本的存活管理留给调用方。

**练习 3**：假设你要给自己的工具接入这个 crate，最少需要 import 哪些名字？

**答案**：最少两个：`StreamingDiff`（构造与推进）和 `CharOperation`（消费返回值，做模式匹配把操作应用到你的文本上）。若还想要行级差异，再加 `LineDiff` 和 `LineOperation`。

## 5. 综合实践

把本讲的三件事串成一次动手记录。在 **Zed 仓库根目录**执行（不要在 `crates/streaming_diff` 里执行，`-p` 参数需要工作区上下文）：

```bash
# ① 跑单元测试
cargo test -p streaming_diff

# ② 只编译、不运行基准，确认 benches 目标可用
cargo bench -p streaming_diff --no-run

# ③ 观察依赖树
cargo tree -p streaming_diff
```

**要记录的内容**（建议做成一张小表）：

| 项目 | 预期 | 你的实测 |
| --- | --- | --- |
| ① 测试数量 | 16 个（源码中 `#[test]` 的静态计数） | 待本地验证 |
| ① 测试结果 | 全部通过（`test result: ok`） | 待本地验证 |
| ② 基准编译 | 无错误结束，产物包含 `streaming_diff` bench 目标 | 待本地验证 |
| ③ 运行依赖 | 第一层仅 `ordered-float` 与 `rope` | 待本地验证 |

**加分项**：随机化测试 `test_random_diffs` 支持环境变量调参，试试：

```bash
ITERATIONS=1000 SEED=7 OLD_TEXT_LEN=40 cargo test -p streaming_diff test_random_diffs -- --nocapture
```

它会用 `seed + i` 作每轮的随机种子生成随机旧文本、随机编辑、随机分块流式推送，然后验证"操作序列能重建新文本"这一不变量（实现在 [streaming_diff.rs:L926-L951](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L926-L951)，环境变量的读取在 [streaming_diff.rs:L983-L1005](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L983-L1005)）。参数细节是第 12 讲的主题，这里先感受一下"用随机化轰炸不变量"的测试风格。

注意：我**没有**替你运行过这些命令，所有"预期"都来自对源码的静态阅读；请以你的本地输出为准。

## 6. 本讲小结

- `streaming_diff` 解决的问题是：旧文本固定、新文本分块流式到达时**增量**计算字符级 diff，并可进一步折叠成行级 diff；核心保证是任意分块下操作序列都能重建新文本。
- 它是一个**单文件 crate**：`src/streaming_diff.rs`（1125 行）包含全部实现与 16 个单元测试；`benches/streaming_diff.rs` 是唯一的基准目标。
- 运行依赖只有 `rope`（Zed 的文本结构，供 `LineDiff` 用）和 `ordered-float`（浮点分数全序比较）两个；`criterion`/`rand`/`util` 只在开发期参与编译。
- 公开 API 只有 4 个名字：`CharOperation`、`StreamingDiff`、`LineOperation`、`LineDiff`；打分用的 `Matrix` 是私有实现细节。
- 两层结构松耦合：`StreamingDiff` 生产 `CharOperation`，`LineDiff` 消费 `CharOperation`、生产 `LineOperation`。
- 真实调用方有两处：`agent/src/tools/edit_session.rs`（只用字符级，写回缓冲区）与 `agent_ui/src/buffer_codegen.rs`（字符级 + 行级，用于按行 diff 呈现）。

## 7. 下一步学习建议

下一讲 **u1-l2《差异的语言：CharOperation 与 LineOperation 数据模型》** 将深入本讲一笔带过的两个枚举：`Insert` 为什么带 `String`、`Delete`/`Keep` 为什么只带字节数、`CharOperation` 与 `LineOperation` 的单位差异，并带你用测试里的 `apply_char_operations`/`apply_line_operations` 亲手把操作序列应用回旧文本。

在进入下一讲之前，建议你先做两件小事：

1. 通读 [streaming_diff.rs:L106-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L106-L128) 和 [L281-L302](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L281-L302)，对四个公开类型的字段建立印象；
2. 跳读测试模块里任意两三个用例（如 [test_replace_line](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L622-L648)），体会"给定手写的字符操作 → 断言行操作 → 断言往返一致"这条贯穿全 crate 的验证链——它也是后续每一讲反复使用的验证手段。
