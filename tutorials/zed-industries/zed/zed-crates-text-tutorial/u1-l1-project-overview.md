# 项目定位：text crate 是什么、在 Zed 中处于什么位置

> 本讲是 `zed-crates-text-tutorial` 学习手册的第一讲（u1-l1），无前置依赖。
> 阅读本讲不需要你事先了解 Zed，也不需要你读过任何一行 Zed 源码。

## 1. 本讲目标

学完本讲，你应该能够：

1. **说出 text crate 的定位**：它是 Zed 编辑器的「协同文本缓冲区」实现，是编辑器里所有文本数据的家；它被 `language`、`editor`、`project` 等 29 个上层 crate 依赖，自己只依赖少数几个更基础的基础设施 crate。
2. **列出 text crate 的核心依赖**：`rope`（文本存储）、`clock`（Lamport 时钟与版本向量）、`sum_tree`（带摘要的有序树）、`collections`（哈希容器封装），并说出它们各自承担的职责。
3. **知道库根是 `src/text.rs` 而不是 `lib.rs`**：理解 `Cargo.toml` 中 `[lib] path = "src/text.rs"` 配置的含义，以及 `test-support` feature 为什么存在。
4. 在自己的机器上**跑通 text crate 的测试**，并亲手添加一个属于自己的测试函数。

## 2. 前置知识

本讲会用到下面几个概念，用通俗的语言先解释一遍：

### 2.1 什么是「缓冲区（Buffer）」

打开任何一个文本编辑器，你在屏幕上看到、正在敲击键盘修改的那份文本，在程序内部通常被称为一个 **buffer（缓冲区）**。它不只是「一个字符串」：

- 它要支持**高效地在中间插入和删除**（字符串的中间插入是 O(n) 的，太慢）；
- 它要记录**撤销/重做历史**（Ctrl+Z / Ctrl+Shift+Z）；
- 在 Zed 这样的协作编辑器里，它还要支持**多个副本并发编辑后自动收敛一致**——两个人同时改同一个文件，各自的改动合并后不会互相覆盖、也不会把文档改乱。

text crate 就是 Zed 中承担这一切的组件。第三点（并发编辑合并）是它区别于普通文本容器的地方，其思路接近 **CRDT**（Conflict-free Replicated Data Type，无冲突复制数据类型）——你不需要现在就懂 CRDT，只需要知道：**删除不真的删除，而是打上「墓碑」标记；每次插入都有一个全局唯一的身份证**。这些会在后续单元展开。

### 2.2 什么是 Rust 的 crate 与 workspace

- 一个 **crate** 是 Rust 的最小编译单元，可以理解为一个「库」或「包」。
- 一个 **workspace** 是多个 crate 的集合，共享一套依赖版本和构建缓存。Zed 整个仓库就是一个 workspace，`crates/` 目录下有上百个 crate。
- crate 的元信息（名字、依赖、feature 开关）写在 `Cargo.toml` 里。**读任何一个 Rust 项目的第一步，往往是读它的 `Cargo.toml`**——本讲我们就从这里入手。

### 2.3 什么是 Lamport 时钟（只需直觉，不必深究）

为了让「多个副本的编辑」能排出先后顺序，每次编辑都会领一个**时间戳**。它不是挂钟时间，而是一个「序号 + 副本编号」的组合：

- 本副本每做一次编辑，就把自己的序号加一：\( \text{value}_{new} = \text{value}_{old} + 1 \)
- 收到别人的时间戳时，把自己的序号抬高到不小于对方：\( \text{value}_{new} = \max(\text{value}_{old},\ \text{value}_{observed}) \)

这样得到的时间戳能保证「因果在前的事件，时间戳一定更小」。这套东西由 Zed 的 `clock` crate 提供，text crate 是它的消费者。本讲只需要这个直觉，细节在单元 3 展开。

### 2.4 你需要会的命令行操作

会打开终端、进入仓库根目录、运行 `cargo` 命令即可。本讲所有命令都是只读或只影响构建缓存的，不会修改源码（唯一的例外是最后一个实践，它会往测试文件里加几行，我们会教你如何还原）。

## 3. 本讲源码地图

本讲涉及的关键文件（均相对 Zed 仓库根目录）：

| 文件 | 行数 | 在本讲中的作用 |
| --- | --- | --- |
| `crates/text/Cargo.toml` | 39 | crate 的「身份证」：库根路径、feature、依赖清单 |
| `crates/text/src/text.rs` | 3845 | 库根文件：模块声明区 + `Buffer`/`BufferSnapshot` 主逻辑 |
| `crates/text/src/tests.rs` | 1057 | 全部单元测试，含我们要运行的 `test_edit` |
| `crates/language/Cargo.toml` | — | 上层消费者的例子：`language` 依赖 `text` |
| `crates/editor/Cargo.toml` | — | 上层消费者的例子：`editor` 依赖 `text` |
| `crates/clock/src/clock.rs` | — | 顺带确认 `clock` crate 提供什么（`ReplicaId`/`Lamport`/`Global`） |

text crate 全部 10 个源文件合计约 6600 行（精确数字是 6593 行，来自 `wc -l`），规模适中，非常适合完整精读。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** 从 Cargo.toml 认识 text crate：库根、依赖与被依赖
2. **4.2** `src/text.rs` 的 mod 声明区：十个源文件的分工
3. **4.3** Buffer 总体印象：一个会记账的文本容器

### 4.1 从 Cargo.toml 认识 text crate：库根、依赖与被依赖

#### 4.1.1 概念说明

`Cargo.toml` 是一个 Rust crate 的「身份证」。它回答三个问题：

- 这个 crate **叫什么**、以哪个文件为编译入口（**库根，library root**）；
- 它**依赖谁**（它站在哪些肩膀上）；
- 它**被谁依赖**（它为谁服务）。

text crate 有一个容易让初学者迷惑的地方：它的库根**不是**默认的 `src/lib.rs`，而是 `src/text.rs`。这是 Zed 仓库的刻意约定——CLAUDE.md 中明确要求创建新 crate 时用 `[lib] path = "...rs"` 指定一个与 crate 同名、更有描述性的文件名。所以你在这套教程里会看到所有源码引用都以 `src/text.rs` 开头。

#### 4.1.2 核心流程

认识一个 crate 的推荐顺序（本讲实际执行的顺序）：

1. 读 `[package]` 段：确认 crate 名字与版本。
2. 读 `[lib]` 段：找到库根文件。
3. 读 `[features]` 段：弄清有哪些编译期开关。
4. 读 `[dependencies]` 段：画出「它依赖谁」。
5. 用文本搜索反查其他 crate 的 `[dependencies]`：画出「谁依赖它」。
6. 把两张图拼起来，得到这个 crate 在整个项目里的**层次位置**。

#### 4.1.3 源码精读

**库根配置**——注意 `path = "src/text.rs"`，以及 `doctest = false`（关闭文档示例测试，因为这是一个内部 crate，文档示例不作为测试运行）：

[crates/text/Cargo.toml:L1-L13](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L1-L13)

```toml
[package]
name = "text"
version = "0.1.0"
edition.workspace = true
publish.workspace = true
license = "GPL-3.0-or-later"

[lints]
workspace = true

[lib]
path = "src/text.rs"
doctest = false
```

这段声明了：crate 名为 `text`，库根是 `src/text.rs`。

**feature 开关**——`test-support` 是一个只在测试场景启用的可选功能集：

[crates/text/Cargo.toml:L15-L16](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L15-L16)

```toml
[features]
test-support = ["rand", "util/test-support"]
```

它做两件事：启用可选依赖 `rand`，并把下游 `util` crate 的 `test-support` feature 也打开。有了它，`src/network.rs`（模拟丢包乱序的测试网络）、`randomly_edit` 等测试工具才能编译——这些代码**不会出现在正式发布的二进制里**。其他 crate（如 `language`）在自己的 `[dev-dependencies]` 里以 `features = ["test-support"]` 的方式引用 text，就能在自己的测试里使用这些工具。

**依赖清单**——text crate 站在哪些肩膀上：

[crates/text/Cargo.toml:L18-L30](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/Cargo.toml#L18-L30)

```toml
[dependencies]
anyhow.workspace = true
clock.workspace = true
collections.workspace = true
log.workspace = true
parking_lot.workspace = true
postage.workspace = true
rand = { workspace = true, optional = true }
regex.workspace = true
rope.workspace = true
smallvec.workspace = true
sum_tree.workspace = true
util.workspace = true
```

其中与「协同文本缓冲区」这个本职最相关的四个：

| 依赖 crate | 职责 | 关键类型（可在源码中印证） |
| --- | --- | --- |
| `rope` | 文本的实际存储：绳结构，支持 O(log n) 的中间插入/删除与持久化共享 | `Rope`、`Point`、`TextSummary`、`LineEnding`（text crate 通过 `pub use rope::*` 整体转手） |
| `clock` | 协同排序的「时间系统」：副本编号、Lamport 时间戳、版本向量 | `ReplicaId`（[crates/clock/src/clock.rs:L12-L39](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/clock/src/clock.rs#L12-L39)）、`Lamport`（[crates/clock/src/clock.rs:L57-L66](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/clock/src/clock.rs#L57-L66)）、`Global` 版本向量（[crates/clock/src/clock.rs:L68-L74](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/clock/src/clock.rs#L68-L74)） |
| `sum_tree` | 带摘要的自平衡有序树：按前缀和高效定位、可按维度游标导航 | `SumTree`、`Item`/`Summary` trait（[crates/sum_tree/src/sum_tree.rs:L31-L55](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L31-L55)） |
| `collections` | Zed 内部统一的哈希容器类型别名（基于 fx hash，迭代更快） | `HashMap`/`HashSet`（[crates/collections/src/collections.rs:L1-L2](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/collections/src/collections.rs#L1-L2)） |

**被谁依赖**——反查上层消费者。以 `language` crate 为例，它把 `text` 列为正式依赖：

[crates/language/Cargo.toml:L66](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/Cargo.toml#L66)

```toml
text.workspace = true
```

同时在自己的 `[dev-dependencies]` 里再次引入并打开测试支持：

[crates/language/Cargo.toml:L94](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/Cargo.toml#L94)

```toml
text = { workspace = true, features = ["test-support"] }
```

`editor` crate 的写法完全一样，分别在 [crates/editor/Cargo.toml:L84](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/Cargo.toml#L84) 与 [crates/editor/Cargo.toml:L125](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/Cargo.toml#L125)。

在当前 HEAD，`crates/` 下共有 **29 个 crate** 的 Cargo.toml 声明了对 `text` 的依赖，包括 `project`、`editor`、`language`、`multi_buffer`、`vim`、`search`、`git`、`collab`、`agent` 等——几乎是整个编辑器功能面的地基。于是层次图大致是：

```
vim / search / collab / agent ...        ← 功能层
        │
   project / multi_buffer               ← 文件与多缓冲区组织层
        │
      editor ←→ language                 ← 编辑器 UI / 语法语言层
        │                    │
        └────────┬───────────┘
              text                        ← 本教程的主角：协同文本缓冲区
        ┌────┬────┬────┬────┐
      rope  clock sum_tree collections    ← 基础设施层
```

#### 4.1.4 代码实践

**实践目标**：亲手验证上面画的依赖图，而不是相信课本。

**操作步骤**：

1. 进入 Zed 仓库根目录（即包含顶层 `Cargo.toml` 的目录，不是 `crates/text`）。
2. 运行下面这条搜索，统计哪些 crate 依赖 `text`：

   ```bash
   grep -lE '^text\.workspace = true|^text = \{ workspace' crates/*/Cargo.toml
   ```

3. 把输出与本讲给出的 29 个 crate 列表对照（我的搜索结果包括：`acp_thread`、`action_log`、`agent`、`agent_ui`、`benchmarks`、`buffer_diff`、`channel`、`client`、`codestral`、`collab`、`debugger_ui`、`diagnostics`、`edit_prediction`、`edit_prediction_context`、`edit_prediction_types`、`edit_prediction_ui`、`editor`、`fs`、`git`、`go_to_line`、`language`、`lsp_locations`、`multi_buffer`、`project`、`prompt_store`、`search`、`tabular_data_preview`、`vim`、`worktree`）。
4. 再反向看 text 自己依赖谁：

   ```bash
   grep -E '^[a-z_-]+\.workspace = true' crates/text/Cargo.toml
   ```

**需要观察的现象**：

- 第 2 步输出的列表不包含 `rope`、`clock`、`sum_tree`——它们是 text 的**下游**（更底层），不是消费者。
- 第 4 步的输出里能看到 `rope`、`clock`、`sum_tree`、`collections`，验证了「基础设施层」的说法。

**预期结果**：依赖方向单向、无环；`text` 处在「基础设施工具」与「编辑器业务」之间的夹层。精确的列表条数可能随仓库演进略有出入，以你的搜索结果为准（本讲数据基于当前 HEAD 统计）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rand` 在 `[dependencies]` 里写成 `optional = true`，而 `regex` 不用？

**参考答案**：因为随机数只在测试/仿真场景需要（随机编辑、模拟网络），正式构建不需要它；配合 `test-support = ["rand", ...]`，只有启用该 feature 时 `rand` 才参与编译，减小发布产物的依赖面。`regex` 用于换行符检测（`LINE_SEPARATORS_REGEX`），是运行时必需，所以不是可选的。

**练习 2**：如果不存在 `[lib] path = "src/text.rs"` 这一行，Cargo 会去哪里找库根？

**参考答案**：会找默认的 `src/lib.rs`。而 text crate 没有这个文件，构建会直接失败。这是 Zed 仓库的约定：用与 crate 同名、语义更明确的文件名做库根（如 `text.rs`），CLAUDE.md 中对新建 crate 有同样要求。

**练习 3**：`language` 的 Cargo.toml 中 `text` 出现了两次（L66 与 L94），为什么不算重复？

**参考答案**：两处作用域不同。L66 在 `[dependencies]`，是正式依赖（不开 test-support）；L94 在 `[dev-dependencies]`，仅测试时生效，并且额外打开了 `features = ["test-support"]`，让 language 的测试可以用 text 的 `Network` 模拟网络等测试工具。同一 crate 在两个段中声明、feature 集不同，是 Rust 常见做法。

### 4.2 `src/text.rs` 的 mod 声明区：十个源文件的分工

#### 4.2.1 概念说明

库根文件 `src/text.rs` 有 3845 行，但它的**开头 11 行**是整份地图的目录页——Rust 用 `mod` 声明把代码拆到多个文件。读懂这个声明区，你就知道 text crate 的全部零件叫什么、哪些对外公开（`pub`）、哪些只在内部使用、哪些只在测试时存在。

理解三个修饰关键词：

- `mod x;`：私有模块，外部不可见；
- `pub mod x;`：公开模块，外部可以用 `text::locator::...` 这样的路径访问；
- `#[cfg(test)]` / `#[cfg(any(test, feature = "test-support"))]`：条件编译，只在测试（或下游启用了 test-support）时才编译进产物。

#### 4.2.2 核心流程

模块声明区 → 再导出（`pub use`）→ 其余代码。绘制模块地图的流程：

1. 列出 9 条 `mod` 声明，标注可见性与条件编译。
2. 找出 `pub use` 再导出，确定 crate 对外的「门面」。
3. 对照每个文件的行数与角色，形成一张分工表。

#### 4.2.3 源码精读

**模块声明区**——整个 crate 的目录页：

[crates/text/src/text.rs:L1-L11](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1-L11)

```rust
mod anchor;
pub mod locator;
#[cfg(any(test, feature = "test-support"))]
pub mod network;
pub mod operation_queue;
mod patch;
mod selection;
pub mod subscription;
#[cfg(test)]
mod tests;
mod undo_map;
```

逐条解读：

| 声明 | 文件 | 行数 | 角色 | 可见性 |
| --- | --- | --- | --- | --- |
| `mod anchor;` | `src/anchor.rs` | 249 | `Anchor` 锚点：在并发编辑下稳定的位置句柄 | 私有，但通过 `pub use anchor::*` 转公开 |
| `pub mod locator;` | `src/locator.rs` | 177 | `Locator`：可在任意两标识之间插入的有序 ID | 公开 |
| `pub mod network;`（带 cfg） | `src/network.rs` | 94 | 模拟丢包/乱序/重复的网络，测试专用 | 仅测试与 test-support |
| `pub mod operation_queue;` | `src/operation_queue.rs` | 165 | `OperationQueue`：暂存乱序到达的远程操作 | 公开 |
| `mod patch;` | `src/patch.rs` | 655 | `Patch`：一组编辑的代数（compose/invert） | 私有，`pub use patch::Patch` 转公开 |
| `mod selection;` | `src/selection.rs` | 169 | `Selection`：选区与方向 | 私有，`pub use selection::*` 转公开 |
| `pub mod subscription;` | `src/subscription.rs` | 67 | 变更订阅：`Topic`/`Subscription` | 公开 |
| `mod tests;`（带 cfg） | `src/tests.rs` | 1057 | 全部单元测试 | 仅 `cargo test` |
| `mod undo_map;` | `src/undo_map.rs` | 115 | `UndoMap`：记录各编辑被撤销的次数 | 私有 |

**再导出区**——决定 crate 的「门面」长什么样：

[crates/text/src/text.rs:L13-L25](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L13-L25)

```rust
pub use anchor::*;
use anyhow::{Context as _, Result};
use clock::Lamport;
pub use clock::ReplicaId;
use collections::{HashMap, HashSet};
use locator::Locator;
use operation_queue::OperationQueue;
pub use patch::Patch;
use postage::{oneshot, prelude::*};

use regex::Regex;
pub use rope::*;
pub use selection::*;
```

三个值得注意的设计：

1. `pub use rope::*;`（L24）把 rope crate 的 `Rope`、`Point`、`TextSummary`、`LineEnding` 等**整体转手**。上层 crate 只需 `use text::Point`，不必直接依赖 rope——text 成为了文本类型的「唯一入口」，减少上层依赖面。
2. `pub use clock::ReplicaId;`（L16）同理，副本编号也从这里走。
3. `anchor`、`patch`、`selection` 是「模块私有 + 选择性再导出」：内部路径短，外部门面干净。

还有一个有趣的细节——测试与正式构建行为不同：

[crates/text/src/text.rs:L51-L55](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L51-L55)

```rust
/// The maximum length of a single insertion operation.
/// Fragments larger than this will be split into multiple smaller
/// fragments. This allows us to use relative `u32` offsets instead of `usize`,
/// reducing memory usage.
const MAX_INSERTION_LEN: usize = if cfg!(test) { 16 } else { u32::MAX as usize };
```

正式构建里单个插入片段最大可达 u32::MAX；而在测试里被压到 **16 字节**，强迫每次测试都触发「大插入切分成多片」的代码路径。这是让单元测试用小样本覆盖边界逻辑的常用技巧，也解释了为什么你以后读测试代码时会看到大量碎片。

#### 4.2.4 代码实践

**实践目标**：不看本讲的表格，独立画出 text crate 的模块地图。

**操作步骤**：

1. 打开 [crates/text/src/text.rs:L1-L43](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1-L43)，把 9 条 `mod` 声明抄到笔记里。
2. 用行数统计核对每个文件的体量：

   ```bash
   wc -l crates/text/src/*.rs
   ```

3. 打开每个文件的开头 30 行，为它写一句「角色摘要」（例如 `src/undo_map.rs` 的结构体名和文档注释会告诉你它记录什么）。
4. 检查再导出：在 `src/text.rs` 里搜索 `pub use`，标出哪些私有模块其实通过再导出对外可见。

**需要观察的现象**：

- `src/tests.rs` 只在 `#[cfg(test)]` 下存在，所以 `grep -rn "mod tests" crates/text/src/` 只会命中 text.rs 一处。
- `src/network.rs` 的条件是 `any(test, feature = "test-support")`，比 `tests.rs` 宽——因为它还要服务**其他 crate 的测试**。

**预期结果**：你得到一张 10 行的表（9 个子模块 + 库根自身），与 4.2.3 的表格内容一致。若某文件的角色你暂时说不清（比如 `patch.rs`），标记出来——那正是后续单元要学的内容，本讲不必深究。

#### 4.2.5 小练习与答案

**练习 1**：`mod patch;` 是私有模块，但外部明明可以用 `text::Patch`，为什么？

**参考答案**：因为库根里有 `pub use patch::Patch;`（[crates/text/src/text.rs:L20](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L20)）。私有模块限制的是路径 `text::patch::Patch` 的可达性，而再导出把类型本身挂到了 crate 根上。这是「内部路径简洁 + 外部门面可控」的惯用法。

**练习 2**：`#[cfg(test)] mod tests;` 与 `#[cfg(any(test, feature = "test-support"))] pub mod network;` 的适用场景有何不同？

**参考答案**：`tests` 只服务本 crate 自己的测试（`cargo test -p text`）；`network` 还要在**别的 crate**（如 language、editor）的测试里用作模拟网络，所以条件多了 `feature = "test-support"`，并且必须是 `pub`——这正是 4.1 中「下游在 dev-dependencies 里开 test-support」能生效的前提。

**练习 3**：为什么 `MAX_INSERTION_LEN` 在测试里要改成 16？

**参考答案**：让小体量的测试输入也能触发「长插入被切分为多个 fragment」的分支，从而用廉价的测试覆盖到正式构建中只有大文件才会走到的逻辑。若不这样做，这些分支在测试中几乎永远不会被执行。

### 4.3 Buffer 总体印象：一个会记账的文本容器

#### 4.3.1 概念说明

`Buffer` 是 text crate 对外的核心类型：一个**会记账的文本容器**。「记账」体现在三方面：

- **历史账**：每次编辑都生成一个 `Operation` 对象，可以被序列化后发送给其他副本（协作的「信件」），也进撤销栈；
- **版本账**：内部维护一个版本向量，随时能回答「我现在已知每个副本编辑到第几号」；
- **订阅账**：任何关心这份文本的人（比如编辑器 UI）都可以订阅变更，收到增量补丁而不是全文。

与之配套的 `BufferSnapshot` 是**不可变快照**：某一时刻文本状态的纯数据视图，可廉价克隆，供后台线程做语法高亮、搜索等只读计算，不必锁住 Buffer。两者的分工是「可变拥有者 vs 只读快照」——本讲只需建立印象，细节在 u1-l4 展开。

#### 4.3.2 核心流程

`Buffer::edit` 的一次本地编辑，内部依次做五件事：

```
Buffer::edit(edits)
  ├─ 1. start_transaction()        开启一个事务（支持嵌套与撤销分组）
  ├─ 2. lamport_clock.tick()       领取新的 Lamport 时间戳
  ├─ 3. apply_local_edit(...)      真正改动 rope/fragment，产出
  │      ├─ EditOperation          → 要广播给其他副本的操作
  │      └─ Patch                  → 要推送给订阅者的增量
  ├─ 4. history.push(...) / push_undo(...)   记入历史与撤销栈
  │      version.observe(...)      版本向量吸收本时间戳
  └─ 5. end_transaction()          关闭事务，必要时自动分组
```

注意返回值：`edit` 返回一个 `Operation`。调用方（协作层）把它发给别的副本；对方收到后调 `Buffer::apply_ops` 重放。也就是说，**本地编辑与远程合并走的是同一套数据结构**，这是协同一致性的根基（u4 单元精读）。

#### 4.3.3 源码精读

**Buffer 的字段**——「账本」的具象化：

[crates/text/src/text.rs:L59-L68](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L59-L68)

```rust
pub struct Buffer {
    snapshot: BufferSnapshot,
    history: History,
    deferred_ops: OperationQueue<Operation>,
    deferred_replicas: HashSet<ReplicaId>,
    pub lamport_clock: clock::Lamport,
    subscriptions: Topic<usize>,
    edit_id_resolvers: HashMap<clock::Lamport, Vec<oneshot::Sender<()>>>,
    wait_for_version_txs: Vec<(clock::Global, oneshot::Sender<()>)>,
}
```

逐字段一句话：`snapshot` 是当前文本状态；`history` 是撤销/重做与事务历史；`deferred_ops` 暂存「依赖未到齐、暂时没法应用」的远程操作；`deferred_replicas` 记录哪些副本还有操作被挂着；`lamport_clock` 是发号器；`subscriptions` 是变更订阅主题；最后两个 `oneshot` 相关字段支撑「等待某个编辑/版本到达」的异步 API。

对照**不可变快照**的字段，能清楚看到「谁多带了账本」：

[crates/text/src/text.rs:L112-L124](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L112-L124)

```rust
#[derive(Clone)]
pub struct BufferSnapshot {
    visible_text: Rope,
    deleted_text: Rope,
    fragments: SumTree<Fragment>,
    insertions: SumTree<InsertionFragment>,
    insertion_slices: TreeSet<InsertionSlice>,
    undo_map: UndoMap,
    pub version: clock::Global,
    remote_id: BufferId,
    replica_id: ReplicaId,
    line_ending: LineEnding,
}
```

两个值得现在就埋下伏笔的细节：

- 有**两根绳**：`visible_text`（可见文本）与 `deleted_text`（被删除文本的墓碑）。删除不是擦除，而是把文本挪进墓碑绳——这是 undo 与协同合并能成立的关键（u3-l2 专讲）。
- `BufferSnapshot` 标了 `#[derive(Clone)]` 且全是数据，克隆它是 O(log n) 级别的共享而非深拷贝。

**创建一个 Buffer**：

[crates/text/src/text.rs:L748-L753](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L748-L753)

```rust
pub fn new(replica_id: ReplicaId, remote_id: BufferId, base_text: impl Into<String>) -> Buffer {
    let mut base_text = base_text.into();
    let line_ending = LineEnding::detect(&base_text);
    LineEnding::normalize(&mut base_text);
    Self::new_normalized(replica_id, remote_id, line_ending, Rope::from(&*base_text))
}
```

三个参数：`replica_id`（我是谁，用于时间戳发号）、`remote_id`（这个缓冲区在全编辑器里的唯一编号，`BufferId` 是非零 u64 的包装，见 [crates/text/src/text.rs:L86-L91](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L86-L91) 的 `BufferId::new`——0 是非法值）、`base_text`（初始文本）。注意入口处做了换行规范化：检测原始风格（CRLF/CR）记在 `line_ending` 里，内部一律存 `\n`，读出时再按需还原。

真正的初始化在 `new_normalized`（[crates/text/src/text.rs:L755-L827](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L755-L827)）：它把初始文本切成若干 `Fragment` 压入 `fragments` 树，每段领一个 `Locator` 作为身份证，并让时钟观察到这次「初始插入」。

**执行一次编辑**：

[crates/text/src/text.rs:L870-L890](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L870-L890)

```rust
pub fn edit<R, I, S, T>(&mut self, edits: R) -> Operation
where
    R: IntoIterator<IntoIter = I>,
    I: ExactSizeIterator<Item = (Range<S>, T)>,
    S: ToOffset,
    T: Into<Arc<str>>,
{
    let edits = edits
        .into_iter()
        .map(|(range, new_text)| (range, new_text.into()));

    self.start_transaction();
    let timestamp = self.lamport_clock.tick();
    let operation = Operation::Edit(self.apply_local_edit(edits, timestamp));

    self.history.push(operation.clone());
    self.history.push_undo(operation.timestamp());
    self.snapshot.version.observe(operation.timestamp());
    self.end_transaction();
    operation
}
```

签名值得细品：`edits` 是「区间 → 新文本」的迭代器，一次可传多组编辑；`S: ToOffset` 表示区间端点可以是字节偏移、行列坐标等多种形态（u2-l2 专讲）；返回值就是那个可广播的 `Operation`。函数体正是 4.3.2 流程图的五步。

而接收方一侧的入口是：

[crates/text/src/text.rs:L909-L922](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L909-L922)

```rust
pub fn apply_ops<I: IntoIterator<Item = Operation>>(&mut self, ops: I) {
    let mut deferred_ops = Vec::new();
    for op in ops {
        self.history.push(op.clone());
        if self.can_apply_op(&op) {
            self.apply_op(op);
        } else {
            self.deferred_replicas.insert(op.replica_id());
            deferred_ops.push(op);
        }
    }
    self.deferred_ops.insert(deferred_ops);
    self.flush_deferred_ops();
}
```

注意 `can_apply_op` 判断与 `deferred_ops` 暂存——网络乱序到达的操作会先排队，等依赖补齐后由 `flush_deferred_ops` 重放。这个机制在 u4-l4 展开，本讲只留印象。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：确认 text crate 在你的机器上可构建、测试可运行，并写下你的第一个测试。

**操作步骤**：

1. 进入 Zed 仓库根目录，先只编译不运行，确认 crate 可构建（首次编译依赖较久，可能需要数分钟）：

   ```bash
   cargo test -p text --no-run
   ```

2. 按名字过滤，只跑 `test_edit` 这一个测试：

   ```bash
   cargo test -p text test_edit
   ```

   它对应 [crates/text/src/tests.rs:L17-L31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L31)，内容正是 4.3.3 讲的 `Buffer::new` + `Buffer::edit` 最小用法：

   ```rust
   #[test]
   fn test_edit() {
       let mut buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), "abc");
       assert_eq!(buffer.text(), "abc");
       buffer.edit([(3..3, "def")]);
       assert_eq!(buffer.text(), "abcdef");
       buffer.edit([(0..0, "ghi")]);
       assert_eq!(buffer.text(), "ghiabcdef");
       buffer.edit([(5..5, "jkl")]);
       assert_eq!(buffer.text(), "ghiabjklcdef");
       buffer.edit([(6..7, "")]);
       assert_eq!(buffer.text(), "ghiabjlcdef");
       buffer.edit([(4..9, "mno")]);
       assert_eq!(buffer.text(), "ghiamnoef");
   }
   ```

3. 在 `crates/text/src/tests.rs` 的**末尾**模仿上面的写法，新增一个属于你的空测试（该文件顶部已有 `use super::{network::Network, *};`，所以 `Buffer` 等类型直接可用，不需要额外 use）：

   ```rust
   #[test]
   fn test_my_first_buffer() {
   }
   ```

4. 运行它：

   ```bash
   cargo test -p text test_my_first_buffer
   ```

5. （可选进阶）把测试体改成真正做事，体会一遍最小链路：

   ```rust
   #[test]
   fn test_my_first_buffer() {
       let mut buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), "hello");
       buffer.edit([(5..5, ", world")]);
       assert_eq!(buffer.text(), "hello, world");
   }
   ```

6. 实践结束后还原源码，保持仓库干净：

   ```bash
   git restore crates/text/src/tests.rs
   ```

**需要观察的现象**：

- 第 2 步：输出中出现 `test_edit ... ok`，且 `test result: ok. 1 passed`（`cargo test` 的名字过滤是子串匹配，只会有一个测试命中）。
- 第 4 步：空测试同样编译通过并显示 `1 passed`——Rust 的测试就是普通函数，空函数也是合法测试。
- 第 5 步（若做了）：断言通过；若把 `"hello, world"` 故意写错，会看到 panic 信息里打印出 `assert_eq` 左右两边的实际值。

**预期结果**：三条命令全部通过即达成实践目标。具体的输出格式与耗时随环境不同，**待本地验证**（本讲不预设你已运行成功；若第 1 步失败，常见原因是 Rust 工具链版本过旧，可先运行 `rustup update`）。

> 提醒：第 3 步会修改 `crates/text/src/tests.rs`。这是学习用的临时改动，做完记得用第 6 步还原，不要提交到版本库。

#### 4.3.5 小练习与答案

**练习 1**：`Buffer` 和 `BufferSnapshot` 都能拿到文本，为什么不直接用一个类型？

**参考答案**：`Buffer` 是可变拥有者，独占编辑权，额外带着历史、撤销栈、延迟操作队列、订阅者等「账本」；`BufferSnapshot` 是纯数据快照，`#[derive(Clone)]` 且克隆代价低，可以安全地交给后台线程做只读计算（高亮、搜索）。把「会变的」和「定格的」分开，既避免了到处加锁，也让并发计算成为可能。细节在 u1-l4。

**练习 2**：`BufferId::new(0)` 会发生什么？为什么 0 要被禁止？

**参考答案**：返回 `Err`（[crates/text/src/text.rs:L86-L91](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L86-L91) 用 `NonZeroU64` 承载，`new` 里 `NonZeroU64::new(id).context("Buffer id cannot be 0.")?`）。用 `NonZeroU64` 一方面把「非零」这个不变量编码进类型系统，另一方面让编译器有机会做「零值不占用合法取值」的内存优化（如 `Option<BufferId>` 与 `BufferId` 同大小）。0 被保留为「无/默认」语义。

**练习 3**：`Buffer::edit` 的参数为什么是 `(Range<S>, T)` 的迭代器，而不是「单个位置 + 单个字符串」？

**参考答案**：因为真实编辑经常是**一次一组**——例如同时多光标编辑、格式化重排、协作合并重放。批量提交让这些编辑共享同一个事务（一次撤销单元）、同一个 Lamport 时间戳、同一次 rope 重建，既保证语义原子性，也显著减少开销。`test_edit` 里 `(4..9, "mno")` 这种「区间替换」形态也说明删除与插入统一为「替换」一种原语。

## 5. 综合实践

**任务：产出一份属于你自己的《text crate 侦察报告》。** 把本讲三个模块的动手环节串起来，完成后你对这个 crate 的「骨架」就有了一份亲手验证过的记录。

报告需包含四部分：

1. **身份页**：抄录 `crates/text/Cargo.toml` 的 `[lib]` 段，回答「这个 crate 的库根是什么文件、为什么不是 lib.rs」。
2. **依赖图**：贴出你用 `grep -lE '^text\.workspace = true|^text = \{ workspace' crates/*/Cargo.toml` 得到的消费者列表，挑选 `language` 与 `editor` 两个 crate，分别指出它们在哪一行声明正式依赖、在哪一行声明带 `test-support` 的开发依赖（对照本讲 4.1.3 的四个链接）。
3. **模块表**：10 个源文件的「文件名 / 行数 / 一句话角色 / 可见性」四列表格（可用 `wc -l crates/text/src/*.rs` 取行数），并标注哪两个文件只在测试相关条件下编译。
4. **运行记录**：记录你执行 `cargo test -p text --no-run`、`cargo test -p text test_edit`、新增 `test_my_first_buffer` 三步的实际结果（成功/失败与报错信息），以及最后 `git restore` 后 `git status` 恢复干净的事实。

完成标准：第 4 部分必须有真实输出，不允许凭空填写；如果某步失败，把失败原因和排查过程写进报告，比「全绿」更有学习价值。

## 6. 本讲小结

- **定位**：text crate 是 Zed 的协同文本缓冲区，约 6600 行、10 个源文件；上承 `language`/`editor`/`project` 等 29 个 crate，下靠 `rope`（存储）、`clock`（Lamport 时钟与版本向量）、`sum_tree`（摘要树）、`collections`（哈希容器）四块基础设施。
- **库根**：`[lib] path = "src/text.rs"`，不是默认的 `src/lib.rs`；`doctest = false`；`test-support` feature 专门为测试工具（如模拟网络）开启编译开关。
- **地图**：`src/text.rs` 开头 11 行的 `mod` 声明区就是全 crate 目录页；`anchor`/`patch`/`selection` 等私有模块通过 `pub use` 组成对外门面，`pub use rope::*` 让 text 成为文本类型的唯一入口。
- **主角**：`Buffer` 是「会记账的文本容器」——历史、撤销、订阅、延迟队列；`BufferSnapshot` 是可廉价克隆的只读快照，内部有 `visible_text` 与 `deleted_text` **两根绳**（删除不擦除，进墓碑）。
- **链路**：`Buffer::edit` 五步（开事务 → 领时间戳 → 应用 → 记账 → 关事务）返回可广播的 `Operation`；对端用 `Buffer::apply_ops` 重放，乱序操作进 `deferred_ops` 排队。
- **环境**：你已经能构建 text crate、运行单个测试、添加自己的测试并还原改动。

## 7. 下一步学习建议

下一讲（u1-l2《第一个 Buffer：创建、编辑与读取文本》）将把本讲的「总体印象」变成肌肉记忆：动手用 `Buffer::new` / `Buffer::edit` / `BufferSnapshot::text` / `Buffer::version` 完成插入、替换、删除，并观察版本号如何随编辑变化。

在进入下一讲之前，建议你先做两件轻量阅读：

1. 通读 [crates/text/src/tests.rs:L17-L49](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L49) 的前几个测试——它们是全 crate 最直白的用法示例。
2. 浏览 [crates/clock/src/clock.rs:L57-L74](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/clock/src/clock.rs#L57-L74)，记住三个名字：`Lamport`（单点时间戳）、`Global`（版本向量）、`ReplicaId`（副本编号）——它们会出现在之后每一讲里。

之后的路线：单元 1 打基础（Buffer 与快照的分工），单元 2 建立坐标系统，单元 3 进入协同编辑的理论地基（时钟、Fragment 墓碑、Locator），再往后才是编辑主链路、订阅、锚点、撤销与性能专题。
