# 目录结构与模块地图：十个源文件各自负责什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 对 `crates/text/src` 下 10 个源文件中的任何一个，说出一句准确的职责摘要。
2. 区分三种模块可见性策略：`pub mod`（locator、operation_queue、subscription）、私有 `mod` + 再导出（anchor、patch、selection）、私有 `mod` + 私有 `use`（undo_map），以及 `network`、`tests` 这两个带 `#[cfg]` 门控、只在测试构建中编译的模块。
3. 根据真实的 `use` 引用关系画出模块依赖图，并理解一个关键事实：**这个 crate 的文件拆分是「物理拆分」而非「架构分层」**——子模块通过 `use crate::...` 回指根模块，形成中心辐射加回边的网状结构。

## 2. 前置知识

本讲不需要读懂算法细节，但需要你理解以下几个 Rust 模块系统的基本概念：

- **`mod x;` 声明**：告诉编译器「crate 里有一个子模块，内容在 `x.rs` 文件中」。声明本身写在库根文件里，但文件必须真实存在于 `src/` 下。
- **`pub mod x;` 与 `mod x;` 的区别**：`pub mod` 允许 crate 外部通过 `text::x::某类型` 的完整路径访问模块内容；私有 `mod` 则把模块路径封闭在 crate 内部。
- **`use` 与 `pub use`**：`use` 是「引入给自己用」，外部看不见；`pub use` 是「再导出」，把名字提升到当前模块的公共命名空间。例如 `pub use patch::Patch;` 之后，外部可以写 `text::Patch` 而不必写 `text::patch::Patch`。
- **`#[cfg(any(test, feature = "test-support"))]`**：条件编译门控。被它标注的模块只在 `cargo test`（`test` cfg 为真）或依赖方开启 `test-support` feature 时才参与编译，正常发布构建中这个文件根本不存在于编译产物里。
- **门面模式（facade）**：对外只暴露一个统一入口，内部结构隐藏。text crate 的 `src/text.rs` 就是门面：所有公共名字最终都汇聚到 `text::` 这一个命名空间下。

一个容易混淆的点先说清楚：**「模块可见」和「类型可见」是两回事**。私有模块里的 `pub struct`，外部无法通过路径写出它的名字，但如果它出现在公开函数签名里，外部仍然能使用该类型的值。理解这一点，才能读懂本讲的可见性清单。

## 3. 本讲源码地图

先给出全景表。行数取自当前 HEAD 的真实统计（共 6593 行，与上一讲「约 6600 行」的说法一致）：

| 文件 | 行数 | 声明方式 | 一句话职责 |
|------|------|----------|-----------|
| `src/text.rs` | 3845 | 库根（`Cargo.toml` 中 `[lib] path`） | 门面 + 主逻辑：Buffer、BufferSnapshot、Fragment、编辑与合并算法全在这 |
| `src/anchor.rs` | 249 | `mod anchor` + `pub use anchor::*` | Anchor 锚点：用「插入操作时间戳 + 操作内偏移」表示编辑无关的位置 |
| `src/locator.rs` | 177 | `pub mod locator` | Locator：类分数的可插入有序标识，做 Fragment 的 id |
| `src/network.rs` | 94 | `#[cfg(any(test, feature = "test-support"))] pub mod network` | 模拟不可靠网络（重复、乱序、部分投递），仅供测试 |
| `src/operation_queue.rs` | 165 | `pub mod operation_queue` | 以 Lamport 时间戳为 key 的操作队列，暂存乱序到达的远程操作 |
| `src/patch.rs` | 655 | `mod patch` + `pub use patch::Patch` | Patch：一组不相交 Edit 的集合，支持 compose / invert 等补丁代数 |
| `src/selection.rs` | 169 | `mod selection` + `pub use selection::*` | Selection：start/end 恒有序 + reversed 标志的选区表示 |
| `src/subscription.rs` | 67 | `pub mod subscription` + `pub use subscription::*` | Topic / Subscription：基于弱引用的编辑变更发布订阅 |
| `src/tests.rs` | 1057 | `#[cfg(test)] mod tests` | 全部单元测试与随机化协同测试 |
| `src/undo_map.rs` | 115 | `mod undo_map` + `use undo_map::UndoMap`（私有） | UndoMap：记录每个编辑被撤销的次数，奇偶性决定可见性 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：声明区清单、位置标识双雄（anchor + locator）、变更记录三件套（patch + subscription + undo_map）、选区与队列（selection + operation_queue）、测试设施（network + tests）。

### 4.1 库根 text.rs 的模块声明区：一张可见性清单

#### 4.1.1 概念说明

`src/text.rs` 身兼二职：它既是 crate 的**库根**（`Cargo.toml` 里用 `[lib] path = "src/text.rs"` 指定，这是 Zed 仓库的命名约定，不用默认的 `lib.rs`），又是**主逻辑文件**——Buffer、BufferSnapshot、Fragment、编辑合并算法这 3800 多行核心代码都在这里。

库根文件的开头负责「点名」：声明这个 crate 由哪些模块组成、哪些名字对外可见。读懂这 11 行声明，等于拿到了整个 crate 的访问权限地图。

#### 4.1.2 核心流程

声明区可以按三个维度分类：

```text
按可见性分：
  pub mod  ──→ locator, operation_queue, subscription, (network 仅测试构建)
  mod      ──→ anchor, patch, selection, undo_map, (tests 仅测试构建)

按再导出方式分（针对私有 mod）：
  pub use 模块::*     ──→ anchor, selection, subscription（全部提升）
  pub use 模块::某类型 ──→ patch（只提升 Patch 一个名字）
  use 模块::某类型     ──→ undo_map（纯内部使用，不进公共 API）

按编译条件分：
  无门控              ──→ 其余 7 个模块始终编译
  any(test, test-support) ──→ network
  test               ──→ tests
```

#### 4.1.3 源码精读

先看模块声明本体，[src/text.rs:1-11](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1-L11)——这 11 行声明了全部 9 个子模块，并标注了各自的可见性与编译条件：

- 第 1 行 `mod anchor;`：私有模块，Anchor 的实现细节藏在 `anchor.rs`。
- 第 2 行 `pub mod locator;`：公开模块，外部可走 `text::locator::Locator` 完整路径。
- 第 3-4 行 `#[cfg(any(test, feature = "test-support"))] pub mod network;`：只在测试或开启 `test-support` feature 时编译。
- 第 5 行 `pub mod operation_queue;`、第 8 行 `pub mod subscription;`：另外两个公开模块。
- 第 6、7、11 行 `mod patch; mod selection; mod undo_map;`：三个私有模块。
- 第 9-10 行 `#[cfg(test)] mod tests;`：测试模块只在 `cargo test` 时编译。

接着看再导出区，[src/text.rs:13-43](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L13-L43)，这里集中体现了三种策略：

- [src/text.rs:13](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L13) `pub use anchor::*;`——把私有模块 anchor 的全部公开项（`Anchor`、`OffsetRangeExt`、`AnchorRangeExt`）提升为 `text::Anchor` 等顶层名字。
- [src/text.rs:18-19](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L18-L19) `use locator::Locator;` 与 `use operation_queue::OperationQueue;`——注意这是**私有** `use`：模块本身是 `pub mod`，外部能走完整路径访问，但 crate 内部只把它引进来自己用，不提升到根命名空间。
- [src/text.rs:20](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L20) `pub use patch::Patch;`——只精确提升 `Patch` 一个名字。经查证，`patch.rs` 中顶层公开项**只有** `pub struct Patch` 这一个（[src/patch.rs:8](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/patch.rs#L8)），所以这条再导出就是 patch 模块公共 API 的全部。
- [src/text.rs:24-25](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L24-L25) 与 [src/text.rs:39](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L39)——`pub use rope::*;` 让 text 成为文本类型（Rope、Point、TextSummary 等）的唯一入口；`pub use selection::*;`、`pub use subscription::*;` 则把这两个模块的内容整体提升。
- [src/text.rs:42](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L42) `use undo_map::UndoMap;`——私有引入。`undo_map` 模块本身私有，且没有 `pub use`，所以 `UndoMap` 是**纯内部实现**：外部代码写不出 `text::UndoMap` 这个路径，它只通过 `BufferSnapshot` 的行为间接暴露。

`subscription` 是唯一「双通道」的模块：既是 `pub mod`（第 8 行）又有 `pub use subscription::*`（第 39 行），所以 `text::Subscription` 和 `text::subscription::Subscription` 两条路径都通。

为什么 `operation_queue` 要做成 `pub mod`？这不是摆设。仓库里有真实的外部消费者——language crate 通过完整路径复用了它：[crates/language/src/buffer.rs:62](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/buffer.rs#L62) 写着 `use text::operation_queue::OperationQueue;`，用它管理 language 层自己的延迟操作。相比之下，`locator` 虽然也是 `pub mod`，但目前在仓库内只被 text crate 自己引用（`text.rs:18` 和 `anchor.rs:3`），它的公开性更多是为外部复用预留的能力。

最后看 feature 定义，[Cargo.toml:15-16](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L15-L16)：`test-support = ["rand", "util/test-support"]`。配合 [Cargo.toml:25](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L25) 的 `rand = { workspace = true, optional = true }` 可以看出：随机数库 `rand` 是可选依赖，只在 `test-support` feature 下进入发布依赖；而 [Cargo.toml:32-38](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L32-L38) 的 dev-dependencies 里也有一份 `rand`，供 `#[cfg(test)]` 的测试代码使用。这解释了为什么 `network.rs`（需要 `rand::Rng`）可以挂在 `any(test, feature = "test-support")` 条件下编译。

#### 4.1.4 代码实践

**实践目标**：用 grep 验证「声明区的每个模块，确实在 text.rs 主逻辑中被真实引用」，并统计引用密度。

**操作步骤**（在 `crates/text` 目录下执行，均为只读命令）：

1. 统计 `Locator`（来自 locator 模块）的引用位置：

   ```bash
   grep -n 'Locator' src/text.rs | head -20
   ```

2. 统计三个「只引入不提升」的模块类型的引用：

   ```bash
   grep -nE 'OperationQueue|Topic|UndoMap' src/text.rs
   ```

3. 统计 `Patch` 与 `Anchor` 的引用规模：

   ```bash
   grep -c 'Patch' src/text.rs
   grep -c 'Anchor' src/text.rs
   ```

**需要观察的现象**（笔者撰写本讲时的实际运行结果，供对照）：

- `Locator` 在 text.rs 中出现约 20+ 处，密集区在 770-790 行（构建初始 fragments）、1040-1100 行（本地编辑时生成新 fragment id）、1880-2020 行（远程编辑路径）、2560-2580 行（锚点解析）。
- `OperationQueue|Topic|UndoMap` 恰好命中 5 处关键字段：`deferred_ops: OperationQueue<Operation>`、`subscriptions: Topic<usize>`（均在 Buffer 结构体内）、`undo_map: UndoMap`（在 BufferSnapshot 结构体内）以及两处 `use` 引入。
- `Anchor` 的引用数（约 40+ 处）远超 `Locator`，集中在 2400-2650 行的锚点解析区。

**预期结果**：你会直观看到「text.rs 是所有子模块的唯一消费者」这一星型结构——所有 `use` 边都从 text.rs 指向子模块（或反向从子模块指回），子模块之间几乎不直接相连（唯一的例外是 anchor → locator 和 subscription → patch，见 4.2 和 4.3）。

#### 4.1.5 小练习与答案

**练习 1**：`patch` 模块是私有的，为什么外部代码还能使用 `Patch` 类型？

答案：因为 [src/text.rs:20](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L20) 有 `pub use patch::Patch;` 再导出。私有的是**模块路径**（`text::patch` 这个路径外部走不通），但 `Patch` 这个名字被提升到了 `text::` 根命名空间，所以外部写 `text::Patch` 完全合法。

**练习 2**：`UndoMap` 类型在 [src/undo_map.rs:48](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/undo_map.rs#L48) 标着 `pub struct`，外部代码能写出 `text::UndoMap` 吗？

答案：不能。`undo_map` 是私有 `mod`（[src/text.rs:11](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L11)），且 text.rs 对它只有私有 `use undo_map::UndoMap;`（第 42 行），没有任何 `pub use`。私有模块挡住了路径，所以这个 `pub` 的实际效果只到 crate 边界为止。`UndoMap` 是纯内部实现，外部只能通过 BufferSnapshot 的方法间接受益。

**练习 3**：`network` 模块在哪些构建配置下参与编译？

答案：两种——`cargo test -p text`（此时 `test` cfg 为真）或某个依赖方开启 text 的 `test-support` feature 时（例如 Zed 的测试基础设施）。平时 `cargo build` 发布构建完全不编译 `network.rs`，它的 `rand` 依赖也不会被拉进发布产物。

### 4.2 位置标识双雄：anchor.rs 与 locator.rs

#### 4.2.1 概念说明

这两个文件解决同一个问题的两个层面：**「如何在文本中指定一个位置，使其在后续编辑下仍然有意义」**。

- `locator.rs` 解决**存储层**：Fragment（文本片段）需要 id，如果用整数下标，在中间插入就要重编号。`Locator` 像分数一样，能在任意两个已有标识之间生成新的标识。
- `anchor.rs` 解决**应用层**：编辑器和协作方需要「书签」——`Anchor` 记录「我是某次插入操作中的第 N 个字节」，这个描述不随文本变化而失效。

直觉上可以把 `Locator` 想象成分数：在 \(\frac{1}{2}\) 和 \(\frac{2}{3}\) 之间永远能插入 \(\frac{3}{5}\)（分数的中间数），不需要改动任何既有分数。`Locator::between(lhs, rhs)` 做的就是这件事，只是用整数序列而非真分数实现。

#### 4.2.2 核心流程

`Locator::between` 的核心是一行位运算：

\[ \text{mid} = \text{lhs} + ((\text{rhs} - \text{lhs}) \gg 48) \]

即不取中点，而是取「靠近 lhs 的 1/65536 处」。这样做的效果：从左往右顺序追加时（打字的典型模式），每一步的 mid 都严格大于 lhs 且只推进一个 u64 分量，标识深度（SmallVec 长度）保持在 1，不会膨胀。

`Anchor` 的解析流程（本讲只讲结构，细节留待第 6 单元）：

```text
Anchor{timestamp, offset}
  → 用 timestamp 在 insertions 树中定位插入操作
  → 找到该操作产生的 fragment 的 Locator id
  → 在 fragments 树中累加 summary 得到 offset / Point 等坐标
```

#### 4.2.3 源码精读

- [src/anchor.rs:1-6](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L1-L6)——注意这个文件的第一行 `use crate::{BufferId, BufferSnapshot, Point, ...}`：**子模块回指根模块**引入类型。这是本讲依赖图的关键回边。
- [src/anchor.rs:8-28](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L8-L28)——`Anchor` 的四个要素：时间戳（拆成 `timestamp_replica_id` + `timestamp_value` 两个字段内联存储）、`offset`（操作内字节偏移）、`bias`（贴住前一个还是后一个字符）、`buffer_id`。第 15-17 行的注释明说了拆分原因：让 replica_id、value、bias 共享对齐空隙，**省下 8 字节**。Anchor 是编辑器里创建最频繁的对象之一，字节级优化值得。
- [src/anchor.rs:91-103](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L91-L103)——`Anchor::cmp` 需要借 `&BufferSnapshot` 才能比较两个锚点的顺序：因为要先经 `fragment_id_for_anchor` 把时间戳翻译成 Locator，再比较。这是「锚点不能脱离 buffer 独立解释」的体现。
- [src/locator.rs:4-12](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/locator.rs#L4-L12)——`Locator(SmallVec<[u64; 2]>)`：默认内联 2 个 u64，绝大多数标识只需 1 个，堆分配为零。文档注释说明了用法约定：集合的初始元素应取 `between(min, max)`，给两端留插入余地。
- [src/locator.rs:52-65](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/locator.rs#L52-L65)——`between` 的实现：lhs 侧用 0 填充、rhs 侧用 u64::MAX 填充后逐位配对，第 57 行注释强调「这个移位至关重要！它优化了顺序打字的常见情形」。
- [src/locator.rs:82-106](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/locator.rs#L82-L106)——`Locator` 为 sum_tree 实现了 `Item` / `KeyedItem` / `ContextLessSummary`，使它可以作为 SumTree 的 key。这解释了它为什么放在独立文件里：它是一个自足的通用数据结构。
- [src/text.rs:568-577](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L568-L577)——`Fragment` 结构的第一个字段 `id: Locator`。Locator 的最终用途就在这里：每个文本片段用 Locator 作身份。
- [src/anchor.rs:2-4](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L2-L4)——anchor.rs 的 import 里出现 `locator::Locator`：这是**子模块之间的少数直连边**（anchor → locator）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「Locator 是 Fragment 的 id」以及「Anchor 的 Debug 输出能还原四要素」。

**操作步骤**：

1. 打开 [src/text.rs:568-577](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L568-L577)，记下 `Fragment` 的 7 个字段名。
2. 打开 [src/text.rs:776-790](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L776-L790) 附近（`Buffer::new` 构建初始 fragments 的循环），观察 `Locator::between(&prev_locator, &Locator::max())` 如何为初始文本的每一段分配递增 id。
3. 阅读 [src/anchor.rs:30-46](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L30-L46) 的 Debug 实现：min/max 锚点有专用打印格式，普通锚点打印 timestamp / offset / bias / buffer_id 四个字段。
4. 运行 locator.rs 自带的深度测试（只读验证，不修改代码）：

   ```bash
   cargo test -p text locator
   ```

**需要观察的现象**：步骤 4 会跑三个测试：`test_locators`（随机性质测试，100 次迭代）、`test_sequential_forward_append_stays_at_depth_1`（10 万次顺序追加深度保持 1）、`test_typing_at_cursor_stays_at_depth_2`（分裂后连续输入深度保持 2）。

**预期结果**：三个测试全部通过。若想加深理解，可对照 [src/locator.rs:146-176](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/locator.rs#L146-L176) 的注释阅读这两个深度测试——它们模拟的正是「构建初始 buffer」和「在光标处连续打字」两种真实模式。（具体测试输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`Anchor` 为什么把一个 `clock::Lamport` 时间戳拆成 `timestamp_replica_id` 和 `timestamp_value` 两个独立字段存？

答案：为了内存布局。见 [src/anchor.rs:15-17](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L15-L17) 的注释：拆开后 replica_id（u16）、value（u32）、bias 可以塞进其他字段留下的对齐空隙（padding），整个结构体省 8 字节。Anchor 在编辑器中被海量创建（每个光标、选区、诊断位置都是锚点），这个优化有实际价值。

**练习 2**：`Fragment.id` 为什么用 `Locator` 而不是简单的 `usize` 序号？

答案：因为协同编辑中多个副本会**并发**在文本中间插入新片段。整数序号在中间插入需要移动后续所有编号，两个副本各自移动会产生不可调和的冲突；而 `Locator::between` 能在前驱和后继之间原地生成新标识（[src/locator.rs:52-65](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/locator.rs#L52-L65)），既不打扰既有片段，又保证全序可比较。

**练习 3**：`Anchor::cmp`（[src/anchor.rs:91](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L91)）为什么不像整数那样直接比较字段，而要传入 `&BufferSnapshot`？

答案：因为两个锚点可能属于**不同的插入操作**，它们的时间戳（Lamport）顺序不等于文本顺序。必须借助 buffer 内部的 `insertions` 树把时间戳翻译成 fragment 的 Locator id（[src/anchor.rs:95-98](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L95-L98)），才能得到文本中的真实先后。这也说明锚点是「相对于某份 buffer 快照才有意义」的句柄。

### 4.3 变更记录三件套：patch.rs、subscription.rs 与 undo_map.rs

#### 4.3.1 概念说明

这三个文件回答「**变更如何被表达、传播和撤销**」：

- `patch.rs`：`Patch<T>` 是一组互不相交的 `Edit<T>`（旧区间 → 新区间映射）的集合，提供 compose（复合）、invert（反转）等「补丁代数」运算。它是增量更新的数学语言。
- `subscription.rs`：`Topic` / `Subscription` 是发布订阅对——buffer 每次编辑后把 Patch 发布出去，订阅方（如语法高亮、渲染层）消费增量。用 `Weak` 引用防止订阅方忘记退订导致内存泄漏。
- `undo_map.rs`：`UndoMap` 记录「每个编辑（edit_id）被哪些撤销操作（undo_id）撤销过几次」，用计数的**奇偶性**判断一个片段当前是否处于被撤销状态。

三者有依赖关系：subscription.rs 的 import 是 `use crate::{Edit, Patch}`——它同时回指根模块（Edit 定义在 text.rs）并直连 patch 模块（Patch）。

#### 4.3.2 核心流程

发布订阅的核心循环：

```text
Buffer::edit 发生
  → Topic::publish(edits)
      → 遍历订阅者列表（Vec<Weak<Mutex<Patch<T>>>>）
      → Weak::upgrade：
          成功 → 该订阅者的 Patch 与新 edits compose，累积
          失败 → 订阅方已被 drop，从列表中移除（retain 返回 false）
  → 订阅方稍后 Subscription::consume() 取走累积的 Patch
```

撤销的奇偶判定：

\[ \text{is\_undone}(edit\_id) \iff \text{undo\_count}(edit\_id) \bmod 2 = 1 \]

撤销一次计数加一（奇数 → 不可见），重做再加一（偶数 → 恢复可见）。

#### 4.3.3 源码精读

- [src/patch.rs:7-31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/patch.rs#L7-L31)——`Patch<T>(Vec<Edit<T>>)`。注意 `new` 里 `#[cfg(debug_assertions)]` 块的两条断言：edits 必须按 old 和 new 两套坐标都严格递增且不重叠——这是补丁代数成立的前提。
- [src/patch.rs:87-118](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/patch.rs#L87-L118)——`compose` 主循环的开头：用两个 peekable 迭代器对齐旧编辑与新编辑，维护 `old_start` / `new_start` 双坐标累计。本讲只需认识这个形状，逐行精读留到 u5-l2。
- [src/subscription.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/subscription.rs#L1)——`use crate::{Edit, Patch};`：回边 + 直连边的活例子。
- [src/subscription.rs:8-11](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/subscription.rs#L8-L11)——两个核心类型：`Topic<T>(Mutex<Vec<Weak<Mutex<Patch<T>>>>>)` 和 `Subscription<T>(Arc<Mutex<Patch<T>>>)`。注意整个文件只有 67 行——这是 crate 里最小的模块，职责极其单一。
- [src/subscription.rs:58-66](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/subscription.rs#L58-L66)——`retain` + `upgrade` 的防泄漏设计：升级失败的弱引用（订阅方已 drop）直接从列表剔除。
- [src/undo_map.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/undo_map.rs#L1)——`use crate::UndoOperation;`：回指根模块定义的操作枚举变体。
- [src/undo_map.rs:47-66](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/undo_map.rs#L47-L66)——`UndoMap(SumTree<UndoMapEntry>)` 与 `insert`：把一次 UndoOperation 展开成若干 `(edit_id, undo_id) → undo_count` 条目插入 SumTree。
- [src/undo_map.rs:68-70](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/undo_map.rs#L68-L70)——奇偶判定本体，一行 `self.undo_count(edit_id) % 2 == 1`。
- 挂载点：[src/text.rs:65](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L65) `subscriptions: Topic<usize>`（Buffer 的字段，usize 表示以字节偏移为坐标的补丁）、[src/text.rs:119](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L119) `undo_map: UndoMap`（BufferSnapshot 的字段，说明**快照也要知道撤销状态**才能回答历史可见性）。

#### 4.3.4 代码实践

**实践目标**：从 Buffer 的字段出发，找到这三个模块在主逻辑中的「插座」。

**操作步骤**：

1. 读 [src/text.rs:59-68](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L59-L68) 的 Buffer 结构体，圈出 `subscriptions: Topic<usize>`。
2. 在 text.rs 中搜索 `subscribe` 的定义与 `edits_patch` 的使用（提示：[src/text.rs:969](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L969) 附近有 `let mut edits_patch = Patch::default();`）。
3. 追一条链：`Buffer::subscribe`（[src/text.rs:1548](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1548)）→ 返回 `Subscription<usize>` → 想象编辑器层调用它后每轮渲染 `consume()` 一次。

**需要观察的现象**：`Topic<usize>` 中的类型参数 `usize` 会一路传导到 `Patch<usize>`、`Edit<usize>`——即订阅者拿到的补丁以「可见文本字节偏移」为坐标系。

**预期结果**：你能在不动一行代码的情况下，画出「edit → edits_patch → subscriptions.publish → 订阅方 consume」这条通知链的草图。（链路中各函数的完整行为留待 u5-l3 精读。）

#### 4.3.5 小练习与答案

**练习 1**：`Topic` 为什么存 `Weak<Mutex<Patch<T>>>` 而不是 `Arc`？

答案：如果存 `Arc`，订阅方即使 drop 了 `Subscription`，Topic 仍持有强引用，其内部 `Patch` 永远不会被释放，造成内存泄漏。存 `Weak` 后，[src/subscription.rs:58-66](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/subscription.rs#L58-L66) 在 publish 时用 `upgrade` 探活：升级失败说明订阅方已销毁，`retain` 返回 false 把它从列表清除。

**练习 2**：为什么「一个编辑被撤销偶数次」等价于「未被撤销」？

答案：撤销和重做是交替发生的：第一次撤销使计数变 1（文本消失），重做变 2（文本恢复），再撤销变 3……每次操作恰好翻转一次可见性，所以 [src/undo_map.rs:68-70](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/undo_map.rs#L68-L70) 用 `undo_count % 2 == 1` 判定。用计数而非布尔值的好处是支持「撤销的撤销」链，且能把计数作为数据广播给其他副本，让各副本独立得出一致的可见性结论。

**练习 3**：`UndoMap` 为什么是 `BufferSnapshot` 的字段而不是只属于 `Buffer`？

答案：因为「某片段在某历史版本是否可见」这类查询（`was_visible`）是针对**特定版本**的快照语义，需要 undo 信息与版本向量联合判断（[src/undo_map.rs:71-93](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/undo_map.rs#L71-L93) 的 `was_undone` 要检查 `version.observed(undo_id)`）。快照必须自持这份信息才能独立回答时间旅行式的问题。

### 4.4 选区与延迟队列：selection.rs 与 operation_queue.rs

#### 4.4.1 概念说明

- `selection.rs`：编辑器的选区（光标拖过的范围）。设计巧点在于不存 head/tail，而是存**恒有序的 start/end 加一个 reversed 标志**——区间运算永远用有序端点，方向信息单独保留。
- `operation_queue.rs`：网络会乱序投递操作，收到「暂时应用不了」的操作（它依赖的前置编辑还没到）时要先排队。`OperationQueue` 以 Lamport 时间戳为 key 用 SumTree 实现这个队列，天然有序、天然去重。

这两个模块有个共同点：**它们不依赖 crate 内任何其他模块**（只依赖 clock、sum_tree 等外部 crate），是可以整体搬走的自足组件——这也解释了 `operation_queue` 为什么是 `pub mod`：language crate 确实在复用它。

#### 4.4.2 核心流程

`OperationQueue::insert` 的三步：

```text
1. ops.sort_unstable_by_key(lamport_timestamp)   → 按 Lamport 排序
2. ops.dedup_by_key(lamport_timestamp)           → 相同时间戳的操作只留一个
3. SumTree::edit(Insert(...))                    → 批量插入摘要树
```

排序 + 去重后插入有序树，所以队列 invariant「键严格递增」始终成立（[src/operation_queue.rs:80](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/operation_queue.rs#L80) 的 `add_summary` 里 `assert!(self.key < other.key)` 就是在防守这条不变量）。

`Selection` 的方向维护：`set_head` 时若新 head 越过了 tail，翻转 `reversed` 并收缩另一端（[src/selection.rs:71-86](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L71-L86)），保证任何时刻 `start <= end` 恒成立。

#### 4.4.3 源码精读

- [src/selection.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L1)——`use crate::{Anchor, BufferSnapshot, TextDimension};`：回指根模块，还用到了 anchor 模块提升出来的 `Anchor`（经由 `pub use anchor::*`，路径上是根命名空间的名字）。
- [src/selection.rs:5-15](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L5-L15)——`SelectionGoal`：记录垂直移动光标时想保持的水平位置（f64 像素列或折行位置），解决「下一行比当前行短时光标该停在哪」的编辑器经典问题。
- [src/selection.rs:17-24](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L17-L24)——`Selection<T>` 五字段：`id`、`start`、`end`、`reversed`、`goal`。泛型 `T` 可以是 `Anchor`，于是选区获得了编辑稳定性。
- [src/selection.rs:156-169](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L156-L169)——`impl Selection<Anchor>` 的 `resolve` 方法把锚点选区解析为任意维度 `D: TextDimension` 的坐标选区，是「Anchor 世界」与「坐标世界」的桥梁。
- [src/operation_queue.rs:5-7](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/operation_queue.rs#L5-L7)——`pub trait Operation`：只要能报出自己的 Lamport 时间戳，就能进这个队列。这是一个刻意做小的抽象接口。
- [src/operation_queue.rs:13](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/operation_queue.rs#L13)——`OperationQueue<T: Operation>(SumTree<OperationItem<T>>)`：一行结构体，全部能力来自 SumTree。
- [src/operation_queue.rs:49-58](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/operation_queue.rs#L49-L58)——insert 的排序去重两步 + 批量树编辑。
- [src/text.rs:62](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L62)——Buffer 的 `deferred_ops: OperationQueue<Operation>` 字段：队列的用武之地。
- [src/text.rs:3386-3393](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L3386-L3393)——`impl operation_queue::Operation for Operation`：text 自己的操作枚举（Edit/Undo 变体）实现那个小 trait，报出各自的 timestamp。注意这里用的是**完整路径** `operation_queue::Operation`，因为模块虽 pub 但名字没有被提升，`Operation` 这个名字在根命名空间已被 text 自己的枚举占用。
- 外部复用证据：[crates/language/src/buffer.rs:62](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/buffer.rs#L62) `use text::operation_queue::OperationQueue;`——`pub mod` 的存在理由。

#### 4.4.4 代码实践

**实践目标**：确认 `OperationQueue` 是自足组件，并观察它的单元测试不依赖 Buffer。

**操作步骤**：

1. 阅读 [src/operation_queue.rs:128-165](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/operation_queue.rs#L128-L165) 的内嵌测试：它自定义了一个 `TestOperation(clock::Lamport)` 来满足 trait，完全没有用到 Buffer——自足性的直接证据。
2. 运行这个测试：

   ```bash
   cargo test -p text operation_queue
   ```

3. 在仓库根目录执行下面的只读搜索，亲自找出所有外部消费者：

   ```bash
   grep -rn 'text::operation_queue' crates/ --include='*.rs'
   ```

**需要观察的现象**：步骤 2 的 `test_len` 会用 `clock.tick()` 生成时间戳、验证插入 / drain 后的计数；步骤 3 的输出目前只有一条：language crate 的 buffer.rs。

**预期结果**：测试通过；grep 只命中 `crates/language/src/buffer.rs:62` 一处。（测试输出待本地验证；grep 结果是撰写本讲时的仓库快照。）

#### 4.4.5 小练习与答案

**练习 1**：`OperationQueue::insert` 为什么能「免费」去重？

答案：见 [src/operation_queue.rs:50-51](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/operation_queue.rs#L50-L51)：先按 `lamport_timestamp` 排序，再 `dedup_by_key` 相同时间戳。因为 Lamport 时间戳在全系统内唯一标识一个操作（副本 id + 序号），网络重复投递同一操作时时间戳相同，排序后相邻即被去掉。

**练习 2**：`Selection` 为什么存 `start/end + reversed` 而不是直接存 `head/tail`？

答案：head/tail 是无序的（head 可以在 tail 左边或右边），做交集、包含、比较等区间运算前每次都要先排序；而 `start <= end` 恒成立（[src/selection.rs:71-103](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L71-L103) 的 set_head/set_tail 在翻转时维护这一点），区间运算直接可用，只在需要方向语义时通过 `head()`/`tail()` 访问器（[src/selection.rs:27-43](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L27-L43)）换算一次。

**练习 3**：为什么 text.rs 里写 `impl operation_queue::Operation for Operation` 而不是给这个 trait 起个别的名字提升到根？

答案：命名冲突：根命名空间里的 `Operation` 已经是 text 自己定义的操作枚举（[src/text.rs:619-622](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L619-L622)）。trait 与类型同名在 Rust 里允许（不同命名空间），但 `use` 引入时会冲突，所以走模块路径消歧。

### 4.5 只随测试编译的模块：network.rs 与 tests.rs

#### 4.5.1 概念说明

协同算法的正确性无法靠几个手写用例证明——需要「对抗性」的随机测试。这两个文件构成测试基础设施：

- `network.rs`：一个**故意不可靠**的消息传递网络。它会把每条消息随机复制 1-3 份、插到收件箱的随机位置（模拟乱序）、投递时只随机取走一部分（模拟部分投递），还支持断线与重连。协同实现必须在这种虐待下仍然收敛。
- `tests.rs`：一千多行测试，从 [src/tests.rs:17-31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L31) 的朴素 `test_edit` 到多副本随机协同测试。

这两个模块都不出现在发布构建里——它们是「测试专用脚手架」，却与生产代码同仓库同目录，靠 `#[cfg]` 门控隔离。

#### 4.5.2 核心流程

`Network::broadcast` 的「虐待三连」：

```text
for 每个非发送者且未断线的收件箱:
    for 收到的每条消息:
        随机生成 1..4 份副本           ← 模拟重复
        每份插入收件箱的随机下标       ← 模拟乱序
receive 时:
    随机决定取走 0..=len 条           ← 模拟部分投递
```

#### 4.5.3 源码精读

- [src/text.rs:3-4](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L3-L4)——network 的门控声明 `#[cfg(any(test, feature = "test-support"))] pub mod network;`：`any` 表示两条通路任一满足即可。
- [src/network.rs:6-10](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/network.rs#L6-L10)——`Network<T: Clone, R: rand::Rng>`：泛型于消息类型与随机源，泛型 `R` 让测试可以注入可复现的种子。
- [src/network.rs:57-80](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/network.rs#L57-L80)——broadcast 全文：第 66-67 行的注释明说「插入一份或多份副本，可能插在这位同伴之前发的消息**前面**，以模拟乱序投递」；第 59-61 行先丢弃断线同伴的消息。
- [src/network.rs:86-93](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/network.rs#L86-L93)——receive：`random_range(0..inbox.len() + 1)` 取走随机前缀。
- [src/network.rs:30-38](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/network.rs#L30-L38)——`disconnect_peer` 清空收件箱（丢弃在途消息），`reconnect_peer` 从指定同伴**复制**一份收件箱作为重连后的初始状态——这模拟的是「重连时做一次状态同步」。
- [src/tests.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L1)——`use super::{network::Network, *};`：tests 模块的两条依赖边（→ text.rs 的全部内容，→ network）都在这一行。
- [src/tests.rs:17-31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L31)——`test_edit`：上一讲实践中你运行过的测试，五次 edit 覆盖了插入、删除、替换三种形态。
- [src/tests.rs:51-52](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L51-L52)——`#[gpui::test(iterations = 100)]`：同一测试用 100 个不同随机种子跑 100 遍，是随机化测试的入口标志（细节留待 u8-l3）。

#### 4.5.4 代码实践

**实践目标**：验证 network 模块是「隔离的测试设施」，不泄漏进正常构建。

**操作步骤**：

1. 只读搜索 network 的全部引用：

   ```bash
   grep -rn 'network' src/ --include='*.rs'
   ```

2. 对比两次检查的编译范围（在 `crates/text` 下）：

   ```bash
   cargo check -p text 2>&1 | tail -5
   cargo check -p text --tests 2>&1 | tail -5
   ```

3. 查看 [Cargo.toml:15-16](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L15-L16) 与 [Cargo.toml:25](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L25)，理解 `rand` 的双重身份（optional 正式依赖 + dev-dependency）。

**需要观察的现象**：步骤 1 应只命中两处文件：`src/text.rs` 的声明（第 3-4 行）与 `src/tests.rs` 的使用（第 1 行）——主逻辑零引用。步骤 2 中，不带 `--tests` 的检查不编译 `network.rs` 与 `tests.rs`，带 `--tests` 的检查会多编译这两个文件（编译产物列表更长）。

**预期结果**：network 与 tests 是纯粹的被 `#[cfg]` 圈养的模块；正式发布的 text crate 二进制里没有它们的任何代码，`rand` 也不会进入正式依赖闭包。（两次 cargo check 的具体输出差异待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：`Network::broadcast` 用哪两处随机性分别模拟「重复」和「乱序」？

答案：重复来自数量随机——[src/network.rs:68](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/network.rs#L68) `for _ in 0..self.rng.random_range(1..4)` 每条消息投 1-3 份；乱序来自位置随机——[src/network.rs:69](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/network.rs#L69) `random_range(0..inbox.len() + 1)` 把副本插到收件箱任意下标，包括已有消息之前。

**练习 2**：`reconnect_peer` 为什么要从另一个同伴复制收件箱，而不是保留断线前自己的？

答案：断线期间该副本错过了大量消息，自己的旧收件箱已过时。`replicate`（[src/network.rs:48-51](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/network.rs#L48-L51)）模拟的是真实协作系统的重连语义：重连方从在线同伴那里同步一份最新状态，对应到 buffer 层就是拿对方的快照做状态转移，而不是回放全部历史。

**练习 3**：如果把 `#[cfg(any(test, feature = "test-support"))]` 改成 `#[cfg(test)]`，会破坏什么？

答案：所有**依赖 text 的 crate** 在它们自己的测试里将无法使用 `Network`——因为依赖方测试编译时，text 是作为普通依赖构建的，`test` cfg 不会传染，只有 `test-support` feature 能由依赖方主动开启。Zed 仓库里其他 crate 的协作测试正是靠开启这个 feature 才能借用 text 的模拟网络。（这是 `any(test, feature)` 双条件的设计动机。）

## 5. 综合实践

**任务：绘制并验证 text crate 的完整模块依赖图。**

这是本讲的毕业练习，把 5 个最小模块的观察串成一张图。

**步骤一：先凭阅读画出草图。** 只看每个文件顶部的 `use` 语句，画出模块间的有向边（A → B 表示 A 引用了 B 的内容）。子模块对根模块的引用形如 `use crate::{...}`。

**步骤二：用 grep 逐边验证。** 对每条边执行一次搜索，例如验证「anchor → text 根」这条边：

```bash
head -10 src/anchor.rs
```

应看到 `use crate::{BufferId, BufferSnapshot, Point, ...}`。

**步骤三：与参考答案对照。** 以下是笔者根据当前 HEAD 逐文件核实出的完整边集（→ 表示「引用」）：

```text
              ┌────────────────────────────┐
   外部 crate  │          text.rs           │  库根 + 3800 行主逻辑
  rope/clock/  └─┬────┬────┬────┬────┬────┬─┘
  sum_tree/       │    │    │    │    │    │   (use / pub use)
  collections     ▼    ▼    ▼    ▼    ▼    ▼
              anchor locator patch selec- subs- opera- undo_map
                        │      │    tion   cription  tion_queue
                        │      │            │         │
                        │      │            │         │
                        ▼      ▼            ▼         ▼
                   （anchor→locator）（sub→patch）（各模块 → text.rs 回边）
                        │
   测试侧：tests.rs ──→ text.rs（use super::*）
           tests.rs ──→ network.rs（use super::network::Network）
           network.rs ──→（无 crate 内依赖，只有外部 crate）
```

文字版边清单：

| 边 | 依据 |
|----|------|
| text.rs → anchor / locator / patch / selection / subscription / operation_queue / undo_map | [src/text.rs:13-42](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L13-L42) 的 `pub use` 与 `use` |
| anchor.rs → text.rs（回边） | [src/anchor.rs:1-6](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L1-L6) `use crate::{BufferId, BufferSnapshot, ...}` |
| anchor.rs → locator.rs | [src/anchor.rs:2-4](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/anchor.rs#L2-L4) 引入 `locator::Locator` |
| selection.rs → text.rs（回边） | [src/selection.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/selection.rs#L1) |
| patch.rs → text.rs（回边） | [src/patch.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/patch.rs#L1) `use crate::Edit`（Edit 定义在 text.rs） |
| subscription.rs → text.rs + patch.rs | [src/subscription.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/subscription.rs#L1) `use crate::{Edit, Patch}` |
| undo_map.rs → text.rs（回边） | [src/undo_map.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/undo_map.rs#L1) `use crate::UndoOperation` |
| tests.rs → text.rs + network.rs | [src/tests.rs:1](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L1) |
| locator.rs / operation_queue.rs / network.rs → （crate 内无边） | 三者只 import 外部 crate |

**步骤四：写下你从图中得出的三条结论**，然后对照：

1. **这不是分层树，是中心辐射网**：text.rs 是唯一消费者兼类型定义中心，六个子模块全部有 `use crate::...` 回边。文件拆分提高的是**可读性**，不是架构隔离——`Edit`、`BufferSnapshot`、`Operation` 这些核心类型定义在 text.rs，被各子模块反向引用，Rust 的模块系统允许这种网状引用。
2. **自足的叶子模块有三个**：locator、operation_queue、network 不依赖 crate 内任何东西。前两个被刻意做成 `pub mod`（operation_queue 已被 language crate 复用），network 因 `#[cfg]` 门控只在测试构建存在。
3. **可见性梯度精细到名字**：从 `pub use anchor::*`（全提升）到 `pub use patch::Patch`（单名提升）再到 `use undo_map::UndoMap`（零提升，纯内部），同一个 crate 里存在四种对外暴露粒度，每种都对应真实的消费需求。

## 6. 本讲小结

- text crate 共 10 个源文件、6593 行：text.rs（3845 行）是库根兼主逻辑，其余 9 个文件按「一个内聚概念一个文件」拆分，从 67 行的 subscription 到 1057 行的 tests。
- 模块可见性分四档：`pub mod`（locator、operation_queue、subscription，保留完整路径访问，operation_queue 已被 language crate 复用）；私有 `mod` + `pub use`（anchor、selection 全提升，patch 只提升 Patch 一个名字）；私有 `mod` + 私有 `use`（undo_map，纯内部实现）；`#[cfg]` 门控（network、tests，不进发布构建）。
- anchor 与 locator 分别解决「位置标识」的应用层与存储层：Anchor 用「插入时间戳 + 操作内偏移」免疫编辑，Locator 用类分数的 `between` 让并发插入无需重编号，二者在 `Fragment.id` 处汇合。
- patch / subscription / undo_map 构成变更记录三件套：Patch 是补丁代数的载体，Topic/Subscription 用 Weak 引用实现防泄漏的发布订阅，UndoMap 用撤销计数的奇偶性判定可见性。
- 依赖图的真实形状是「中心辐射 + 回边」：六个子模块都通过 `use crate::...` 回指 text.rs 定义的核心类型；只有 locator、operation_queue、network 三个叶子模块完全自足。
- network 模拟重复、乱序、部分投递、断线重连的不可靠网络，是协同正确性的随机化测试基础设施，靠 `any(test, feature = "test-support")` 双通路门控。

## 7. 下一步学习建议

本讲你拿到了完整的「房间平面图」，但还没有走进任何一个房间。下一讲 **u1-l4「Buffer 与 BufferSnapshot：可变拥有者与不可变快照」**将打开最大的一扇门：逐字段剖析 [src/text.rs:59-68](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L59-L68) 的 Buffer 与 [src/text.rs:112-124](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L112-L124) 的 BufferSnapshot，理解为什么「读走快照、改走 Buffer」。

在进入下一讲之前，建议你先做两件热身阅读：

1. 通读 [src/text.rs:1-140](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1-L140)——声明区加上 Buffer / BufferSnapshot / History 三个结构体的字段定义，这是后续所有讲的「字典页」。
2. 跳读 [src/tests.rs:17-31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L31) 的 `test_edit`，注意它的断言风格：不用辅助框架，直接对 `buffer.text()` 的字符串结果做 `assert_eq!`——你在后续各讲的实践中也会照这个风格写测试。

之后，第 2 单元将带你深入坐标系统（offset、Point、PointUtf16），第 3 单元补齐协同编辑的理论地基（Lamport 时钟、Fragment 墓碑模型、Locator 的深入剖析——本讲 4.2 节是你已经迈出的第一步）。
