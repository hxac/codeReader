# buffer_diff 是什么：Zed 里的行级差异引擎

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 `buffer_diff` 在 Zed 中的职责边界：它为一个 buffer 与其「diff 基准文本」计算行级差异，并把差异块（hunk）以可查询的形式提供给编辑器、git 面板等消费者。
2. 逐条列出 `buffer_diff` 的依赖（`imara-diff`、`sum_tree`、`gpui`、`language`、`rope`、`text` 等），并说出每个依赖大致提供什么能力。
3. 通过 grep 找出 Zed 仓库中所有依赖 `buffer_diff` 的下游 crate，知道 `editor`、`git_ui`、`project` 等各自用它做什么。

本讲是整本手册的第一讲，不要求你写过 Zed 的代码，只要求你对「编辑器」和「git」有日常使用经验。

## 2. 前置知识

在进入源码之前，先把几个名词用通俗语言讲清楚。它们会贯穿整本手册。

### 2.1 buffer 是什么

在编辑器内部，一个打开的文件并不是一个普通字符串，而是一个称为 **buffer（缓冲区）** 的对象。它除了文件文本本身，还维护着语法高亮信息、撤销历史、协作状态等。在 Zed 中：

- `text` crate 提供最底层的 `Buffer`（纯文本 + 编辑历史）；
- `language` crate 在其上扩展出带语法分析的 `Buffer`；
- 文本内容用 `rope` crate 的 `Rope`（绳索）结构存储，而不是 `String`，这样在大文件中间插入一行不需要拷贝整个字符串。

### 2.2 diff 与 hunk 是什么

**diff（差异）** 是把两份文本按行对比后的结果。**hunk（差异块）** 是 diff 中的一小块连续区域：一段「基准文本里有、当前文本里没有（删除）」或「当前文本里有、基准文本里没有（新增）」或两者兼有（修改）的内容。例如 git 命令行输出里以 `@@ -1,3 +1,4 @@` 开头的每一段就是一个 hunk。

`buffer_diff` 做的事情可以概括为：

> 拿一个 buffer 的当前内容，和一份「基准文本」（diff base，最典型的就是 git HEAD 版本的文件内容）做行级对比，产出一组 hunk，并回答诸如「第 10 行有没有被改过」「这个 hunk 在基准文本里对应哪几行」这类查询。

### 2.3 diff base：HEAD、index 是什么

如果你用过 `git status`，会看到「已暂存」和「未暂存」两类改动。git 内部其实维护着三个版本的文件内容：

| 名称 | 含义 |
| --- | --- |
| HEAD | 最近一次提交里的内容 |
| index（暂存区） | `git add` 之后、提交之前的内容 |
| 工作区 | 磁盘上（编辑器里）正在编辑的内容 |

`buffer_diff` 把「和谁比」抽象成 `DiffBaseKind`：可以和 HEAD 比、和 index 比、和一个任意 git 对象（Oid）比、或者和调用方临时给定的一段文本比（Custom，比如 agent 修改前的原文、剪贴板内容）。这在源码注释里有明确说明（本讲 4.2 节会看到原文）。

### 2.4 crate 与 workspace

Zed 是一个庞大的 Rust 工程，根目录的 `Cargo.toml` 定义了一个 **workspace**，`crates/` 目录下每个子目录是一个 **crate**（可独立编译的库或程序）。你在子 crate 的 `Cargo.toml` 里看到的 `foo.workspace = true`，意思是「依赖 foo，版本号等工作区根统一管理」。

### 2.5 gpui 的 Entity 是什么

Zed 自研了 UI 框架 gpui。gpui 里的 **`Entity<T>`** 是「一块被框架管理的状态」的句柄：状态 `T` 存在框架里，别的代码拿着句柄去读取（`read`）或更新（`update`）它；状态变化时可以发出事件（`EventEmitter` 机制），订阅者会收到通知。`buffer_diff` 里的 `BufferDiff` 正是一个被 gpui 管理的实体，编辑器 UI 通过订阅它来刷新界面上的差异标记。

## 3. 本讲源码地图

本讲涉及的文件都在 Zed 仓库的 `crates/` 下：

| 文件 | 作用 |
| --- | --- |
| `crates/buffer_diff/Cargo.toml` | 本 crate 的清单：名字、依赖、feature。全文仅 44 行，是了解其「外部关系」的最佳入口。 |
| `crates/buffer_diff/src/buffer_diff.rs` | 全部实现所在的**单一源码文件**，共 4362 行。文件开头 18 行的 `use` 导入就是一张「架构依赖地图」。 |
| `crates/git_ui/Cargo.toml` | git 面板 UI crate 的清单，第 24 行声明依赖 `buffer_diff`。 |
| `crates/editor/Cargo.toml` | 编辑器 crate 的清单，第 45 行声明依赖 `buffer_diff`。 |

值得先记住的一个事实：**`buffer_diff` 没有目录式的模块结构**，`Cargo.toml` 里 `[lib] path = "src/buffer_diff.rs"` 指明整个库就是一个文件。这符合 Zed 的编码规范（优先在现有文件中实现功能），对学习者反而是好事——所有逻辑都在一处，配合本手册逐段精读即可。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. Cargo 依赖清单——buffer_diff 依赖了什么、为什么；
2. 文件头部 use 导入——从导入列表读懂内部架构；
3. 下游消费者——谁在用 buffer_diff、各自用来做什么。

### 4.1 Cargo 依赖清单：buffer_diff 依赖了什么

#### 4.1.1 概念说明

一个 crate 的 `Cargo.toml` 依赖清单是最可靠的「架构说明书」：它白纸黑字地列出了这个 crate 允许自己使用哪些外部能力。读懂依赖清单，就能在进入任何实现细节之前，先对 crate 的能力边界形成正确预期。

对 `buffer_diff` 来说，依赖可以分成四类：

1. **diff 算法**：`imara-diff`——真正的差异比对算法不在这里实现，而是复用这个为 git 打造的 Rust diff 库。
2. **Zed 内部基础库**：`gpui`（实体与事件）、`language`（带语法的 buffer 与词级 diff）、`rope`（文本结构）、`text`（纯文本 buffer 与坐标/锚点）、`sum_tree`（区间树，用来存 hunk）、`clock`/`util`/`ztracing`（工具类）。
3. **日志与测试辅助**：`log`、`tracing`、`pretty_assertions`。
4. **可选依赖**：`settings`，只在本 crate 自己的 `test-support` feature 下启用——把「读用户设置」的能力隔离在测试之外，生产构建不引入。

#### 4.1.2 核心流程

从依赖清单推断 crate 的工作流程：

```text
外部（如 project crate）提供基准文本
        │
        ▼
rope/text/language: 两侧文本各就各位（buffer 快照 + 基准文本 buffer）
        │
        ▼
imara-diff: 按行计算差异 → 得到行号区间形式的 hunk
        │
        ▼
sum_tree: 把 hunk 存进可按区间查询的树（BufferDiffSnapshot）
        │
        ▼
gpui: 状态变化 → 发出事件 → editor / git_ui 等订阅者刷新 UI
```

后续单元会逐一展开这条链路；本讲只需建立起这个「俯瞰图」。

#### 4.1.3 源码精读

先看包定义与库路径（注意它不是默认的 `src/lib.rs`）：

- [Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/Cargo.toml#L11-L12) —— `[lib] path = "src/buffer_diff.rs"`：整个库的唯一源码文件，文件名与 crate 同名，这是 Zed 的命名规范。

再看 feature 定义：

- [Cargo.toml:L14-L15](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/Cargo.toml#L14-L15) —— 定义 `test-support` feature，它唯一的作用是拉起可选依赖 `settings`。也就是说，只有在测试场景下，本 crate 才会去读 `LanguageSettings`（词级 diff 开关等用户设置）；正常构建不依赖设置系统。

核心依赖清单：

- [Cargo.toml:L17-L30](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/Cargo.toml#L17-L30) —— `[dependencies]` 全表。逐项说明：

| 依赖 | 提供的能力 | 在本 crate 中的角色 |
| --- | --- | --- |
| `imara-diff` | 高性能 diff 算法（git 同款 histogram 算法） | 行级差异计算引擎 |
| `gpui` | Entity、事件、异步任务 | `BufferDiff` 实体与后台计算调度 |
| `language` | 带语法分析的 buffer、`word_diff_ranges` | 基准文本 buffer、hunk 内的词级差异 |
| `rope` | Rope 文本结构 | 高效表示/拼接基准文本 |
| `text` | Buffer、Anchor、Point、Patch | buffer 快照、坐标与锚点、编辑补丁 |
| `sum_tree` | 支持聚合摘要的平衡树 | hunk 与 pending hunk 的有序存储 |
| `clock` | 逻辑时钟 | 追踪 buffer 编辑版本以判定 pending 是否过期 |
| `util` / `log` / `tracing` / `ztracing` | 工具与日志 | 辅助设施 |

- [Cargo.toml:L32-L40](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/Cargo.toml#L32-L40) —— `[dev-dependencies]`：只在测试时编译，包括 `rand`（随机化测试）、`unindent`（书写多行测试文本）、开启 `test-support` feature 的 `gpui` 与 `text` 等。第 3 讲运行测试时会用到它们。

- [Cargo.toml:L42-L43](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/Cargo.toml#L42-L43) —— 告诉 `cargo-machete`（检查无用依赖的工具）忽略 `tracing`，因为它通过宏间接使用。

#### 4.1.4 代码实践

**实践目标**：亲手确认 crate 可独立构建，并观察它的直接依赖图。

**操作步骤**：

1. 在 Zed 仓库根目录执行 `cargo build -p buffer_diff`，确认编译通过。
2. 执行 `cargo tree -p buffer_diff --depth 1`，打印本 crate 的直接依赖。
3. 对照上方的依赖角色表，把输出里的每个依赖标注上「它提供了什么」。

**需要观察的现象**：`cargo tree` 输出的顶层依赖列表应当与 `Cargo.toml` 的 `[dependencies]` 一致；注意 `settings` 不应出现（它只在 `test-support` feature 下启用）。

**预期结果**：构建成功；依赖列表与源码清单吻合。本讲义的作者没有替你运行过这两条命令，输出细节「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `settings` 是可选依赖，而 `language` 不是？

**参考答案**：`language` 参与核心流程——基准文本本身就是 `language::Buffer`，词级差异也依赖 `language::word_diff_ranges`，没有它 crate 无法工作；而 `settings` 只在测试中读取用户设置（如 `word_diff_enabled`）时需要，生产路径上不需要，因此挂到 `test-support` feature 后面，避免让核心库依赖 UI 配置系统。

**练习 2**：如果让你把「按行比对两段文本」这一步从本 crate 中抽出去，依赖清单里哪一项可以去掉？

**参考答案**：`imara-diff`。它是唯一提供 diff 算法的依赖；本 crate 对它做的是「喂数据、收行号区间」的封装（第 3 单元第 1 讲会精读这层封装）。

### 4.2 文件头部 use 导入：一张架构地图

#### 4.2.1 概念说明

读单个大文件的 Rust crate 时，头部 `use` 导入是最有效的「目录页」：每个导入项都暗示文件中存在对应的代码路径。`buffer_diff.rs` 的前 18 行导入恰好按依赖分组排列，与本 crate 的分层一一对应。

#### 4.2.2 核心流程

把导入逐组翻译成职责：

```text
gpui 导入        → 实体生命周期与事件（BufferDiff 是什么）
imara_diff 导入 → 差异算法（怎么算出 hunk）
language 导入   → 基准文本 buffer 与词级差异（和谁比、比多细）
rope 导入       → 文本表示（内容怎么存）
sum_tree 导入   → hunk 存储（结果怎么组织）
text 导入       → 坐标、锚点、补丁（位置怎么表达）
util 导入       → 错误处理辅助
```

#### 4.2.3 源码精读

- [buffer_diff.rs:L1](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1) —— 从 `gpui` 导入 `App`、`Context`、`Entity`、`EventEmitter`、`Task`：`BufferDiff` 是 gpui 实体、会发事件、更新在后台异步进行，这些能力全部来自这一行。

- [buffer_diff.rs:L2](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2) —— 从 `imara_diff` 导入 `Algorithm`、`Diff`、`InternedInput` 与 `sources::lines`：按行切分文本、执行 diff、选择算法（histogram）。这是第 3 单元第 1 讲的主角。

- [buffer_diff.rs:L3-L6](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3-L6) —— 从 `language` 导入 `Capability`（把基准 buffer 设为只读）、`DiffOptions`、`Language`/`LanguageRegistry`（语法信息）、`LanguageSettings`（测试中读取词级 diff 开关）、`word_diff_ranges`（词级差异计算）。

- [buffer_diff.rs:L7](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L7) 与 [buffer_diff.rs:L14](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L14) —— `rope::Rope` 与 `sum_tree::SumTree`：前者是文本的物理表示，后者是 hunk 的逻辑容器。

- [buffer_diff.rs:L15-L17](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L15-L17) —— 从 `text` 导入 `Anchor`（随编辑自动移动的位置锚点）、`Bias`、`BufferId`、`Edit`、`OffsetRangeExt`、`Patch`、`Point` 与坐标转换 trait：整份「坐标系工具箱」。

- [buffer_diff.rs:L18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L18) —— `util::{ResultExt, debug_panic}`：日志化地吞掉可忽略错误、只在调试构建报警，符合 Zed「不静默丢弃错误」的规范。

导入之后紧接着就是本 crate 的门面。先看两个最具代表性的定义：

- [buffer_diff.rs:L20](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L20) —— 常量 `MAX_WORD_DIFF_LINE_COUNT = 5`：一个 hunk 超过 5 行就不做词级高亮，防止大改动时卡顿。

- [buffer_diff.rs:L22-L29](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L22-L29) —— `BufferDiff` 结构体：持有 buffer id、只读的基准文本 buffer、diff 快照、可选的 secondary diff（git stage 场景，第 4 单元展开）、buffer 快照与基准类型。

- [buffer_diff.rs:L31-L45](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L31-L45) —— `DiffBaseKind` 枚举，注释写得很清楚：只有基准为 HEAD 的 diff 才支持 stage/restore hunk，对其他基准（例如与另一分支的 merge base）做 stage 会改写已提交的内容。四种变体分别是 `Head`（已提交内容）、`Index`（暂存区内容）、`Oid`（任意 git 对象）、`Custom`（调用方给定文本，例如 agent 的原始文本、剪贴板、另一个文件）。

- [buffer_diff.rs:L47-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L47-L55) —— `BufferDiffSnapshot`：不可变快照，内部是两棵 `SumTree`（hunks 与 pending hunks）加上两侧文本快照。UI 每一帧读取的就是它。

- [buffer_diff.rs:L1552-L1566](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1552-L1566) —— `DiffChanged`（携带精确到锚点的变更范围）与 `BufferDiffEvent`（`BaseTextChanged` / `DiffChanged` 两种事件），以及 `impl EventEmitter<BufferDiffEvent> for BufferDiff`——这是与下游 UI 的契约。

#### 4.2.4 代码实践

**实践目标**：验证「导入项 = 代码路径」这个判断方法，建立对文件布局的手感。

**操作步骤**：

1. 在仓库根目录执行 `rg -n "InternedInput" crates/buffer_diff/src/buffer_diff.rs`，记下所有命中行。
2. 再分别执行 `rg -n "SumTree<" crates/buffer_diff/src/buffer_diff.rs` 与 `rg -n "word_diff_ranges" crates/buffer_diff/src/buffer_diff.rs`。
3. 打开命中行附近的代码，各读 20 行左右，写下你猜测每个导入项参与的函数名。

**需要观察的现象**：`InternedInput` 的命中应该集中在 diff 计算相关函数（如 `compute_hunks` 一带）；`SumTree<` 出现的行号应该正好对应 `BufferDiffSnapshot` 等结构体的字段声明；`word_diff_ranges` 的调用点应该在 hunk 构建逻辑里。

**预期结果**：三个导入项的命中位置分别落在「计算」「存储」「词级细化」三个不同代码区段，验证了 4.2.2 的分层推断。具体行号「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：导入列表里为什么同时需要 `text::Anchor` 和 `text::Point` 两种位置类型？

**参考答案**：`Point` 是静态的（行、列坐标），buffer 一旦编辑就可能失效；`Anchor` 是钉在文本里的锚点，随编辑自动漂移。hunk 存储用 `Anchor`（diff 结果不能因为后续打字而错位），对外呈现给 UI 时再换算成 `Point`/行号。这是第 2 单元的核心话题之一。

**练习 2**：不看正文中途返回，凭注释说明 `DiffBaseKind::Oid` 与 `DiffBaseKind::Custom` 的区别。

**参考答案**：`Oid` 是仓库里某个 git 对象的版本（如与另一分支的 merge base），来源仍是 git；`Custom` 是调用方临时提供的任意文本（agent 的原始文本、剪贴板、另一个文件），与 git 对象无关。

### 4.3 下游消费者：谁在用 buffer_diff

#### 4.3.1 概念说明

理解一个 crate 的职责，反面证据同样重要：**谁依赖它、在哪里调用它**。`buffer_diff` 自己不渲染任何 UI，也不直接读 git 仓库——它是纯粹的「差异计算与查询」中间层。上游（`project`）把 git 数据喂给它，下游（`editor`、`git_ui` 等）消费它产出的 hunk。

#### 4.3.2 核心流程

一次典型的端到端数据流：

```text
project crate：从 git 仓库读取 HEAD/index 文本
        │  创建并持有 BufferDiff 实体，写入基准文本
        ▼
buffer_diff：计算 hunk，发出 DiffChanged 事件
        │
        ├── editor：订阅事件 → 在编辑器 gutter 里画增/删/改标记，按行高亮
        ├── git_ui / git_ui_core：渲染 git 面板中的文件 diff 视图
        └── agent_ui 等：展示 agent 对文件修改的审查 diff
```

#### 4.3.3 源码精读

声明依赖的两份清单（本讲规格中列出的关键源码）：

- [git_ui/Cargo.toml:L24](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/git_ui/Cargo.toml#L24) —— git 面板 crate 声明 `buffer_diff.workspace = true`。
- [editor/Cargo.toml:L45](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/Cargo.toml#L45) —— 编辑器 crate 声明同一依赖。

用 `rg -l '^buffer_diff\.workspace\s*=\s*true' -g Cargo.toml` 在整个仓库扫描，可以确认共 **12 个 crate** 依赖本 crate。结合对各 crate 源码中 `buffer_diff::` 调用点的抽查，整理成下表：

| 下游 crate | 调用点（抽查到的文件） | 用途概述 |
| --- | --- | --- |
| `project` | `src/project.rs`、`src/git_store.rs`、`src/git_store/diff_buffer_list.rs` | 中枢：为每个受 git 管理的 buffer 创建/缓存 `BufferDiff` 实体，把 git HEAD/index 文本灌入基准 |
| `editor` | `src/git.rs`、`src/element.rs`、`src/items.rs`、`src/split.rs` 等 | 在编辑器里渲染 hunk：gutter 标记、变更行高亮、行内 diff |
| `git_ui` / `git_ui_core` | `git_ui_core/src/file_diff_view.rs` | git 面板里的单文件 diff 视图 |
| `multi_buffer` | `src/multi_buffer.rs` | 多文件/多选区聚合编辑时透传每个 buffer 的 diff 状态 |
| `agent_ui` | `src/agent_diff.rs`、`src/conversation_view.rs` 等 | 展示 agent 对文件的修改（对应 `DiffBaseKind::Custom` 场景） |
| `edit_prediction` / `edit_prediction_ui` | —— | 内联补全建议与 buffer 差异的协调（依赖已声明，调用点未逐一抽查） |
| `collab` | —— | 协作场景下的变更展示 |
| `action_log` / `markdown_preview` / `acp_thread` | —— | 各自的辅助性消费（依赖已声明，调用点未逐一抽查） |

表中前五行的调用点经过逐一 grep 验证；后几行仅验证了 Cargo.toml 中的依赖声明，具体用法留作读者的综合实践。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手复现上面的消费者扫描，形成自己的观察笔记。

**操作步骤**：

1. 在 Zed 仓库根目录执行：

   ```bash
   rg -l '^buffer_diff\.workspace\s*=\s*true' -g Cargo.toml
   ```

2. 对输出中的每个 crate，执行一次调用点抽查（以 `project` 为例）：

   ```bash
   rg -l 'buffer_diff::' crates/project/src
   ```

3. 挑两个调用文件，各打开一处调用上下文读几行，回答：「它拿 `BufferDiff` 的什么——实体、快照，还是事件？」
4. 整理一份**不超过 10 行**的观察笔记：每个消费者一行，格式如「`editor`：在 `git.rs` 中读取 diff 快照，用于 gutter 渲染」。

**需要观察的现象**：扫描结果应为 12 个 crate（含 `buffer_diff` 之外的所有路径）；不同 crate 的调用密度差异很大——`editor` 与 `project` 命中文件最多，说明它们是重度消费者。

**预期结果**：笔记与 4.3.3 的表格前几行基本一致。数量与名单以你本地 `rg` 的实际输出为准（本讲义作者扫描时得到 12 个）；若与仓库当前状态不一致，以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `buffer_diff` 不直接依赖 `git` 相关 crate，而是让 `project` 在中间倒一手？

**参考答案**：职责分离。`buffer_diff` 只关心「两段文本怎么比、结果怎么查」，基准文本从哪里来（git HEAD、index、任意对象、剪贴板）与它无关。这样它才能同时服务 git 面板（HEAD/index 基准）和 agent 审查（Custom 基准）两类互不相干的场景，也避免了 UI 层 crate 直接耦合 git 实现细节。

**练习 2**：`editor` crate 里同时存在 `src/git.rs` 与 `src/element.rs` 两处 `buffer_diff::` 调用，推测它们各自负责什么。

**参考答案**：`git.rs` 负责编辑器与 git 集成的状态层——持有 diff 实体、订阅 `BufferDiffEvent`、维护每个 hunk 的渲染状态；`element.rs` 是 gpui 渲染层——每帧读取 `BufferDiffSnapshot`，把 hunk 画成 gutter 标记和行高亮。前者管数据流，后者管像素。

**练习 3**：如果要给「与另一分支比较」的功能做 UI，应该用 `DiffBaseKind` 的哪个变体？为什么不能对它做 stage 操作？

**参考答案**：用 `Oid`（例如指向 merge base 的提交对象）。不能 stage，因为基准不是 HEAD——把工作区内容写进 index 相当于用「另一个分支的状态」覆盖暂存区，会改写已提交工作对应的差异语义。源码在 `DiffBaseKind` 的文档注释（[buffer_diff.rs:L31-L33](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L31-L33)）里写明了这条规则，对应的判定函数 `is_stageable` 在 [buffer_diff.rs:L1687](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1687)。

## 5. 综合实践

把本讲三个模块串成一个任务：**为 `buffer_diff` 写一页「架构速览」笔记**，要求包含四部分，全部基于你本地验证过的命令输出：

1. **定位**：用不超过 3 句话说明 buffer_diff 的职责（参考 4.1.2 的流程图，用自己的话重写）。
2. **依赖表**：跑 `cargo tree -p buffer_diff --depth 1`，为每个直接依赖标注角色（可对照 4.1.3 的表，但必须与你看到的输出一致）。
3. **导入地图**：抄录 [buffer_diff.rs:L1-L18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1-L18) 的导入，按 gpui / imara-diff / language / 文本基础库 / 工具 分组，每组写一句「它支撑了 crate 的哪种能力」。
4. **消费者清单**：附上 4.3.4 的扫描命令与你的 10 行观察笔记。

验收标准：笔记中不出现任何未经你本地命令验证的结论；对没把握的条目直接写「待确认」。这份笔记在后续单元精读源码时会反复用到。

## 6. 本讲小结

- `buffer_diff` 是 Zed 的行级差异引擎：为一个 buffer 与其 diff 基准文本（git HEAD、index、任意对象或自定义文本）计算 hunk，并以快照 + 事件的形式对外提供查询。
- 整个 crate 是单文件实现（`src/buffer_diff.rs`，4362 行），依赖核心是 `imara-diff`（算法）、`text`/`rope`/`language`（文本与语法）、`sum_tree`（hunk 存储）、`gpui`(实体与事件)。
- `settings` 是仅测试启用的可选依赖：生产路径不读用户设置，测试路径才读 `word_diff_enabled` 等开关。
- 头部 `use` 导入是一张分层地图：gpui→生命周期、imara-diff→计算、language→基准与词级差异、text→坐标锚点、sum_tree→存储。
- 全仓库共有 12 个 crate 依赖它；重度消费者是 `project`（创建并灌注 diff 实体的中枢）、`editor`（gutter 与行高亮渲染）、`git_ui`/`git_ui_core`（面板 diff 视图）与 `agent_ui`（agent 修改审查）。
- `DiffBaseKind` 四种变体决定了「和谁比」，且只有 HEAD 基准允许 stage/unstage——这是第 4 单元的伏笔。

## 7. 下一步学习建议

下一讲（`u1-l2-core-concepts.md`）将深入本讲只是路过的三个核心数据结构：`BufferDiff` 实体、`BufferDiffSnapshot` 快照与 `DiffHunk` 差异块，逐字段讲解它们如何用锚点描述「一块差异」。

在进入下一讲前，建议你先做两件小事：

1. 通读 [buffer_diff.rs:L20-L180](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L20-L180)，本讲提到的所有类型定义都在这段里，混个眼熟即可，不必逐行理解。
2. 翻一眼 `sum_tree` crate 的公开文档（`crates/sum_tree/src/sum_tree.rs` 头部注释），了解「Item/Summary/SeekTarget」三个词的含义——第 2 单元第 2 讲会正式拆解它们在 hunk 存储中的用法。
