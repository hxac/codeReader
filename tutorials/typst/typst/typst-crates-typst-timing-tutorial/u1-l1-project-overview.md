# 项目定位与整体结构

## 1. 本讲目标

本讲是 typst-timing 学习手册的第一篇，目标是让你在「不读任何实现细节」的前提下，先建立两个认识：

1. **typst-timing 是什么**：它在 Typst 这个大型 workspace 里扮演什么角色、解决什么问题。
2. **它长什么样**：它的目录结构如何、依赖了哪些库、对外提供了哪些 API、wasm 支持又是怎么挂上去的。

读完本讲，你应该能够：

- 用一句话说清 typst-timing 的职责；
- 读懂它的 `Cargo.toml`，理解「workspace 依赖」与「可选 wasm feature」这两套机制；
- 拿着 `src/lib.rs` 这一个文件的「源码地图」，知道每一段大概在做什么，并为后续讲义（事件模型、线程模型、时间戳、导出等）做好准备。

本讲**只做总览**，不深入任何一个内部机制的实现细节——那是 u2、u3 各篇讲义的任务。

## 2. 前置知识

在开始之前，请确认你了解下面这些基础概念。不熟悉也没关系，我们会用通俗的方式再带一遍。

- **Rust crate 与 workspace**：一个 crate 是一个独立的编译单元（有自己的 `Cargo.toml`）；一个 workspace 可以把多个 crate 放在一起统一管理版本、依赖和 lint。Typst 就是一个包含几十个 crate 的 workspace。
- **Cargo 的 `workspace = true`**：当某个字段（如 `version`、`serde`）写成 `{ workspace = true }` 时，表示「这个值不要在这里写死，去根目录的 `Cargo.toml` 的 `[workspace.package]` / `[workspace.dependencies]` 里取」。它的好处是：全 workspace 的版本统一改一处即可。
- **Chrome Trace / Perfetto**：Chrome 浏览器自带的性能分析工具（地址栏输入 `chrome://tracing`），后来独立成 [Perfetto](https://ui.perfetto.dev/)。它读取一种特定的 JSON 格式（一组带 `B`/`E` 相位的事件），把程序运行过程画成时间轴。typst-timing 导出的就是这种格式。
- **条件编译 `cfg`**：Rust 可以用 `#[cfg(...)]` / `cfg!(...)` 让某段代码只在特定目标平台（如 `target_arch = "wasm32"`）或启用了某个 feature 时才编译。
- **性能计时（profiling / timing）的直觉**：在一段代码开始时记一个时间戳、结束时再记一个时间戳，两条记录配成一对，就能知道这段代码花了多久。typst-timing 做的就是「帮 Typst 把成千上万对这样的时间戳收集起来，再导出成可视化文件」。

> 术语提示：后续会反复出现「事件（Event）」「作用域（Scope）」「时间戳（Timestamp）」「线程（Thread）」等词。本讲你只要知道「typst-timing 会收集一堆带时间戳的事件」即可，细节后面再讲。

## 3. 本讲源码地图

typst-timing 是一个**极小的 crate**：整个 crate 的源码只有两个文件。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-timing/Cargo.toml` | crate 的「身份证 + 依赖清单」。声明名字、版本、依赖的第三方库，以及 wasm 可选 feature。 |
| `crates/typst-timing/src/lib.rs` | crate 的**全部实现**。约 320 行，单文件，包含宏、全局状态、计时作用域、事件结构、时间戳抽象、wasm 计时器等所有逻辑。 |

> 小提示：在大型项目里看到「一个 crate 只有单个 `.rs` 文件」并不奇怪。当一个模块职责足够单一、代码量足够小，强行拆成多文件反而增加阅读负担。typst-timing 就是这种「小而美」的设计。

本讲我们主要站在「俯瞰」视角看这两个文件；后续讲义才会逐块钻进去。

## 4. 核心概念与源码讲解

### 4.1 crate 定位：typst-timing 在 Typst 中扮演什么角色

#### 4.1.1 概念说明

Typst 是一个用 Rust 写的现代化排版引擎。一次完整的排版（把一份 `.typ` 文档编译成 PDF）要经过**解析（syntax）、求值（eval）、实现（realize）、布局（layout）、渲染/导出（render/svg/pdf）** 等多个阶段，每个阶段又由成千上万个小子任务组成。

当一份文档编译得很慢时，开发者需要回答一个问题：**时间到底花在哪一步了？**

为了回答这个问题，Typst 在各个关键位置埋了「计时点」，把「某段代码从开始到结束」记录成一对事件。typst-timing 就是负责**收集和导出这些计时事件**的那个 crate。它本身不做排版，也不参与编译流程，它只提供三样东西：

1. 一个**全局开关**（默认关闭）；
2. 一套**记录事件**的简便写法（`timed!` 宏和 `TimingScope`）；
3. 一个**导出**函数，把收集到的事件写成 Chrome Trace 能读的 JSON。

正因为 typst-timing 这么「底层、这么通用」，它几乎是整个 workspace 最被广泛依赖的 crate 之一：解析、求值、布局、渲染、PDF、SVG、CLI……几乎每个核心 crate 都依赖它，以便在自己的关键路径上插入计时点。

#### 4.1.2 核心流程

从「使用者」的角度，typst-timing 的工作流程可以概括成一条直线：

```
默认关闭（开销≈0）
   │  调用 enable()
   ▼
计时开启：代码里用 timed!(...) 包裹的片段
   │  每个片段开始/结束 各 push 一条事件
   ▼
事件被收集到全局缓冲区
   │  调用 export_json(...)
   ▼
写出 Chrome Trace JSON（同时清空缓冲区）
   │  用 chrome://tracing 或 Perfetto 打开
   ▼
可视化时间轴，定位性能热点
```

关键设计点（本讲只点到为止，后续细讲）：

- **默认关闭**：计时本身有开销，所以生产环境默认不开。开关是一个原子布尔（详见 u2）。
- **零成本门控**：关闭时，`timed!` 几乎不产生任何代码开销（详见 u3）。
- **导出格式**：选择 Chrome Trace JSON，是因为它有现成的、免费的可视化工具，不用自己造轮子。

#### 4.1.3 源码精读

crate 级别的文档注释只有一句话，直接点明了定位——「为 Typst 做性能计时」：

> [`src/lib.rs:1`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L1-L1)：crate 的唯一文档注释，写明本 crate 的职责。

`Cargo.toml` 顶部的 `description` 字段也是同一句话，会显示在 crates.io 上：

> [`Cargo.toml:1-13`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/Cargo.toml#L1-L13)：包元数据（名字、描述等），所有字段都用 `{ workspace = true }` 从 workspace 根继承。

「几乎所有核心 crate 都依赖它」这件事，可以通过搜索 workspace 里各 crate 的 `Cargo.toml` 得到印证——`typst-syntax`、`typst-eval`、`typst-realize`、`typst-layout`、`typst-render`、`typst-svg`、`typst-pdf`、`typst-library`、`typst-kit`、`typst-cli` 等十几个 crate 的依赖清单里都有：

```toml
typst-timing = { workspace = true }
```

而 workspace 根 `Cargo.toml` 把它注册成了一个 workspace 级依赖（路径 + 版本），供大家共享：

> 仓库**根目录** `Cargo.toml` 的 `[workspace.dependencies]` 段（第 34 行）：`typst-timing = { path = "crates/typst-timing", version = "0.15.1" }`，于是各 crate 只要写 `typst-timing = { workspace = true }` 即可复用。
>
> 说明：本讲 permalink base 只覆盖 `crates/typst-timing/` 目录，因此根 `Cargo.toml` 不便给出本讲格式的永久链接；请自行打开仓库根 `Cargo.toml` 第 34 行对照。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是建立「它在 Typst 里无处不在」的直觉。

1. **实践目标**：数一数有多少个 crate 依赖了 typst-timing，感受它的「基础设施」地位。
2. **操作步骤**：在仓库根目录执行（只读命令）：
   ```bash
   grep -rl 'typst-timing' --include 'Cargo.toml' crates/
   ```
3. **需要观察的现象**：会列出一长串 `crates/*/Cargo.toml` 文件路径。
4. **预期结果**：应能看到 typst-syntax、typst-eval、typst-layout、typst-render、typst-svg、typst-pdf、typst-kit、typst-cli 等十余个核心 crate 都命中。
5. 如果你无法运行该命令，可对照本讲列出的清单理解，结论一致。

#### 4.1.5 小练习与答案

**练习 1**：typst-timing 自己参与排版（解析/布局/渲染）吗？
**答案**：不参与。它只提供「记录 + 导出计时事件」的能力，是一个被各排版 crate 依赖的基础设施型 crate。

**练习 2**：为什么 typst-timing 要默认关闭？
**答案**：因为计时本身有写缓冲、加锁、取时间戳等开销。Typst 在生产编译时不需要计时，默认关闭可以让这些开销接近于零；只有开发者想分析性能时才手动 `enable()`。

---

### 4.2 Cargo.toml 与 workspace 依赖关系

#### 4.2.1 概念说明

`Cargo.toml` 是一个 Rust crate 的「身份证 + 依赖清单」。typst-timing 的 `Cargo.toml` 是学习 Typst workspace 依赖管理的极佳样本，因为它同时演示了三种机制：

1. **包元数据继承**：`version`、`edition`、`license` 等全部 `{ workspace = true }`，统一由根 `Cargo.toml` 的 `[workspace.package]` 决定。
2. **第三方库依赖**：依赖了 `parking_lot`、`serde`、`serde_json` 三个库，也都走 `{ workspace = true }`，版本号在根 `Cargo.toml` 的 `[workspace.dependencies]` 里集中管理。
3. **平台相关的可选依赖**：`web-sys` 只在编译到 `wasm32` 时才需要，而且进一步用 `optional = true` + feature 门控，做到「不用就不编译进来」。

#### 4.2.2 核心流程

依赖关系可以这样理解：

```
typst-timing
   ├──（普通依赖，所有平台都需要）
   │     parking_lot   —— 提供更快、更简单的 Mutex
   │     serde         —— 序列化框架（derive 宏 + Serialize trait）
   │     serde_json    —— 把结构序列化成 JSON 字节流
   │
   └──（仅在 wasm32 目标 + 开启 wasm feature 时才编译）
         web-sys       —— Rust 对浏览器 JS API（performance）的绑定
```

为什么是这三个普通依赖？

- **serde + serde_json**：导出函数要把事件写成 Chrome Trace JSON，最省事、最稳的做法就是用 serde 的派生宏自动序列化。
- **parking_lot**：用它提供的 `Mutex` 来保护全局事件缓冲区（比标准库的 `Mutex` 更轻量、API 更顺）。

> 这些依赖的具体用途会在后续讲义（u2 讲全局状态、u2-l4 讲导出）中逐一展开，这里你只要记住「它们各自管一摊事」即可。

#### 4.2.3 源码精读

**普通依赖区**（所有平台都需要）：

> [`Cargo.toml:15-18`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/Cargo.toml#L15-L18)：声明 `parking_lot`、`serde`、`serde_json` 三个依赖，全部 `{ workspace = true }`，版本由 workspace 根统一管理。

在仓库根 `Cargo.toml` 的 `[workspace.dependencies]` 里，能查到它们被锁定的版本与 feature：

- `parking_lot = "0.12.1"`（根 `Cargo.toml` 第 95 行）
- `serde = { version = "1.0.184", features = ["derive"] }`（根 `Cargo.toml` 第 116 行，注意开了 `derive`，所以本 crate 里能用 `#[derive(Serialize)]`）
- `serde_json = "1"`（根 `Cargo.toml` 第 117 行）

而本 crate 自身的版本（`0.15.1`）、Rust 最低版本（`1.92`）、`edition`（`2024`）、`license`（`Apache-2.0`）也都来自根 `Cargo.toml` 的 `[workspace.package]`（根 `Cargo.toml` 第 6-16 行）。这就是为什么本 crate 的 `Cargo.toml` 顶部 13 行几乎全是 `{ workspace = true }` 而看不到具体值。

**lint 区**：

> [`Cargo.toml:26-27`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/Cargo.toml#L26-L27)：`[lints] workspace = true`，让本 crate 沿用 workspace 统一的一套 clippy 严格规则（定义在根 `Cargo.toml` 的 `[workspace.lints.clippy]`）。

#### 4.2.4 代码实践

这是一个**配置阅读 + 对照型实践**，帮助你看懂「workspace 继承」到底继承了什么。

1. **实践目标**：亲手把本 crate `Cargo.toml` 里每个 `{ workspace = true }` 的字段，对应到根 `Cargo.toml` 里的真实值。
2. **操作步骤**：打开本 crate 的 `Cargo.toml` 第 4-13 行（包元数据）和第 15-18 行（依赖），再打开仓库根 `Cargo.toml` 的 `[workspace.package]`（第 6-16 行）与 `[workspace.dependencies]`（第 18 行起）。
3. **需要观察的现象**：本 crate 写的字段名，都能在根 `Cargo.toml` 找到同名的键。
4. **预期结果**：你能填出这样一张对照表——
   | 本 crate 字段 | 根 Cargo.toml 里的真实值 |
   | --- | --- |
   | `version` | `0.15.1` |
   | `rust-version` | `1.92` |
   | `edition` | `2024` |
   | `license` | `Apache-2.0` |
   | `parking_lot` | `0.12.1` |
   | `serde` | `1.0.184`（带 `derive`） |
   | `serde_json` | `1` |
5. 如果某项你一时找不到对应值，标注「待确认」即可，不要猜测。

#### 4.2.5 小练习与答案

**练习 1**：为什么 typst-timing 的 `Cargo.toml` 里不直接写 `serde = "1.0.184"`，而要写 `{ workspace = true }`？
**答案**：为了让整个 workspace 的依赖版本统一。某天要升级 serde，只需在根 `Cargo.toml` 改一处，所有 crate 同步生效，避免版本碎片化导致的重复编译或不一致。

**练习 2**：本 crate 的三个普通依赖（parking_lot / serde / serde_json）分别对应它的哪项职责？
**答案**：serde + serde_json 用于把计时事件序列化成 Chrome Trace JSON；parking_lot 提供保护全局事件缓冲区所用的 `Mutex`。

---

### 4.3 src/lib.rs 单文件组织与对外 API 分布

#### 4.3.1 概念说明

typst-timing 的全部实现都在 `src/lib.rs` 一个文件里（约 320 行）。本讲我们**不展开实现**，而是先给它画一张「地图」：把文件按行号分成几段，看清楚每段管什么。有了这张地图，后续讲义你再钻进任何一段都不会迷路。

这个文件可以粗略地分成 **5 大块**：

1. **对外 API（宏 + 函数 + 公开类型）**：使用者直接接触的部分。
2. **全局状态**：开关、事件缓冲区、每线程数据。
3. **计时作用域 `TimingScope`**：记录事件的「探针」本体。
4. **事件数据模型**：`Event`、`EventKind`。
5. **跨平台时间抽象**：`Timestamp`、`ThreadData`、（wasm 时的）`WasmTimer`。

#### 4.3.2 核心流程

把 `src/lib.rs` 按行号铺开，大致是下面这张表（**本讲只看「在哪、是什么」，不看「怎么做」**）：

| 行号区间 | 内容 | 归属 | 本讲角色 |
| --- | --- | --- | --- |
| L1 | `//! Performance timing for Typst.` | 文档 | crate 定位 |
| L3–9 | `use` 导入 | — | 依赖了哪些标准库/第三方 |
| L11–44 | `timed!` 宏（`#[macro_export]`） | 对外 API | 使用者最常用的写法 |
| L46–58 | `THREAD_DATA`（thread_local） | 全局状态 | 每线程数据（u2 细讲） |
| L60–61 | `ENABLED`（AtomicBool） | 全局状态 | 全局开关 |
| L63–64 | `EVENTS`（Mutex\<Vec\<Event\>\>） | 全局状态 | 事件缓冲区 |
| L66–92 | `enable / disable / is_enabled / clear` | 对外 API | 开关与清空 |
| L94–150 | `export_json` | 对外 API | 导出 Chrome Trace JSON |
| L152–157 | `TimingScope` 结构体（pub） | 对外类型 | 计时探针 |
| L159–205 | `TimingScope::new/with_span/new_impl` + `Drop` | 对外 API | 创建与自动收尾 |
| L207–226 | `Event` / `EventKind`（私有） | 数据模型 | 事件长什么样（u2-l1 细讲） |
| L228–270 | `Timestamp` | 内部抽象 | 跨平台时间（u2-l3 细讲） |
| L272–283 | `ThreadData` | 内部抽象 | 每线程数据载体 |
| L285–320 | `WasmTimer`（仅 wasm） | 内部抽象 | wasm 计时（u3-l3 细讲） |

「对外 API」是本讲你要记住的重点，因为这是后续所有实践都要调用的东西：

- `timed!` 宏：包裹一段表达式，给它命名、可选地附一个 span。
- `enable()` / `disable()` / `is_enabled()`：全局开关。
- `clear()`：清空已收集的事件。
- `export_json(writer, source)`：把事件导出为 Chrome Trace JSON。
- `TimingScope`（含 `new` / `with_span`）：更底层的、手动管理作用域的入口。

#### 4.3.3 源码精读

**入口导入**——这几行暴露了本 crate 用到的「积木」，正好对应 `Cargo.toml` 的依赖：

> [`src/lib.rs:3-9`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L3-L9)：导入标准库的 `Write`、`NonZeroU64`、原子类型，以及第三方的 `parking_lot::Mutex` 和 `serde` 序列化工具。可以看出本 crate 的「积木」与依赖清单完全对应。

**对外开关 API**——这是本讲综合实践要用到的：

> [`src/lib.rs:66-72`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L66-L72)：`enable()` 把全局原子布尔 `ENABLED` 置为 `true`。
>
> [`src/lib.rs:82-86`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L82-L86)：`is_enabled()` 读取该布尔，返回当前是否处于计时开启状态。
>
> [`src/lib.rs:74-80`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L74-L80)：`disable()` 把它置回 `false`。

这三个函数都带 `#[inline]` 并使用 `Ordering::Relaxed`——本讲你只要记住「它们非常轻量」即可，背后的内存序与零成本门控会在 u2/u3 详解。

#### 4.3.4 代码实践

这是一个**源码阅读 + 标注型实践**，帮你把「地图」刻进脑子。

1. **实践目标**：在不读实现的前提下，能给 `src/lib.rs` 的每一段标出「属于哪一大块」。
2. **操作步骤**：打开 `src/lib.rs`，从上到下滚动，对照本讲 4.3.2 的表格，在每个行号区间的开头写一行注释（脑内标注即可），例如 `// === 对外 API：开关 ===`。
3. **需要观察的现象**：你会发现文件的排列顺序是「先放对外 API，再放内部实现」，这是很常见且友好的组织方式。
4. **预期结果**：你能合上讲义，凭记忆说出「对外 API 主要分布在文件的 L11–44（宏）、L66–92（开关/清空）、L94–150（导出）、L152–205（TimingScope）这几段」。
5. 本步骤不修改源码，无需运行；如想加深印象，可把这张表抄一遍。

#### 4.3.5 小练习与答案

**练习 1**：typst-timing 对外公开的类型里，哪一个代表「一段被计时的代码区间」？
**答案**：`TimingScope`（`src/lib.rs` 第 153 行）。它在创建时记录「开始」事件，在 `Drop` 时记录「结束」事件。

**练习 2**：`Event` 和 `EventKind` 是公开的还是 crate 内部的？
**答案**：它们没有 `pub`，是私有的（`struct Event` / `enum EventKind`），仅供本 crate 内部使用。使用者并不直接接触事件结构，而是通过 `timed!` / `TimingScope` / `export_json` 间接交互。

**练习 3**：本文件为什么不拆成多个 `.rs` 文件？
**答案**：因为整体逻辑紧密相关、体量不大（约 320 行），单文件反而更利于一次性建立全局认知，也减少模块跳转。这是「职责单一的小 crate」常见且合理的取舍。

---

### 4.4 wasm feature 与可选依赖 web-sys 的条件引入

#### 4.4.1 概念说明

Typst 既能编译成原生程序（跑在 Windows/macOS/Linux 上），也能编译成 WebAssembly（wasm）跑在浏览器里（比如 [typst.app](https://typst.app) 或各种网页端编辑器）。两种环境取「当前时间」的方式完全不同：

- **原生**：用标准库的 `std::time::SystemTime`。
- **浏览器/wasm**：要用浏览器 JS 提供的 `performance.now()`，而访问它需要 `web-sys` 这个 crate。

问题来了：大多数用户只在原生平台用 typst-timing，没必要把他们机器上根本用不到的 `web-sys` 编译进去。于是 typst-timing 用了 **Cargo 的可选依赖 + feature + 条件编译** 三件套，把 wasm 相关代码「按需」引进来。

#### 4.4.2 核心流程

这扇「门」由**两道闸**共同把守：

```
闸一：Cargo 层面（决定要不要把 web-sys 编译进来）
   [target.'cfg(target_arch = "wasm32")'.dependencies]
   web-sys = { ..., optional = true }     ← 可选依赖，且仅 wasm32 目标可见

闸二：feature 层面（决定是否真的启用）
   [features]
   wasm = ["dep:web-sys"]                  ← 只有开启 wasm feature，才拉入 web-sys

─── 只有「目标是 wasm32」 且 「用户开启了 wasm feature」，web-sys 才会被编译 ───

闸三：源码层面（决定每段代码是否参与编译）
   #[cfg(all(target_arch = "wasm32", feature = "wasm"))]
   struct WasmTimer { ... }                ← 代码同样双重门控
```

这种「平台 cfg + feature + 代码 cfg」三重一致的设计，确保了：

- 原生平台：完全不碰 `web-sys`，编译产物干净、体积小。
- wasm 平台、但没开 `wasm` feature：依赖不引入，代码退化（时间戳恒为 `0.0`，不报错但不计时）。
- wasm 平台、且开了 `wasm` feature：`web-sys` 引入，`WasmTimer` 生效，能拿到真实的高精度时间。

#### 4.4.3 源码精读

**闸一 + 闸二（Cargo.toml）**：

> [`Cargo.toml:20-21`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/Cargo.toml#L20-L21)：声明 `web-sys` 只在 `cfg(target_arch = "wasm32")` 目标下可见、且 `optional = true`；它还只开启了 `Window`、`WorkerGlobalScope`、`Performance` 三个 feature——正好是「获取 performance 句柄」所需的最小集。

> [`Cargo.toml:23-24`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/Cargo.toml#L23-L24)：`[features]` 段定义了 `wasm = ["dep:web-sys"]`。`dep:` 前缀是 Cargo 的现代写法，明确表示「这里拉入的是名为 `web-sys` 的**可选依赖**」，而不是某个同名的隐式 feature。

**闸三（源码里的双重 cfg）**——本讲只看「门控」本身，不看 `WasmTimer` 的实现：

> [`src/lib.rs:55-56`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L55-L56)：`thread_local` 里的 `timer` 字段被 `#[cfg(all(target_arch = "wasm32", feature = "wasm"))]` 门控——非 wasm 或未开 feature 时，这个字段根本不存在。
>
> [`src/lib.rs:286-292`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L286-L292)：整个 `WasmTimer` 结构体同样被双重 cfg 门控。
>
> [`src/lib.rs:248-252`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L248-L252)：取时间戳 `now_with` 里，wasm 分了两个分支——开了 feature 用 `data.timer.now()`，没开 feature 则返回 `0.0`（即「不计时但不崩溃」的退化行为）。

#### 4.4.4 代码实践

这是一个**配置解读型实践**，理解「两道闸」如何叠加。

1. **实践目标**：说清楚在三种组合下，`web-sys` 是否会被编译、`WasmTimer` 是否生效。
2. **操作步骤**：阅读 `Cargo.toml` 第 20-24 行，再阅读 `src/lib.rs` 第 55-56、248-252、286-292 行；用下面的表格逐格推断。
3. **需要观察的现象**：把「目标平台」和「是否开启 wasm feature」两个维度交叉。
4. **预期结果**（填表）：
   | 目标平台 | 开启 `wasm` feature？ | `web-sys` 被编译？ | `WasmTimer` 生效？ | 时间戳来源 |
   | --- | --- | --- | --- | --- |
   | 原生（非 wasm32） | 否（一般不开） | 否 | 否（结构体不编译） | `SystemTime` |
   | wasm32 | 否 | 否 | 否（走 `0.0` 退化分支） | 恒为 `0.0` |
   | wasm32 | 是 | 是 | 是 | `performance.now()` |
5. 表中「wasm32 + 否」一行的退化行为（返回 `0.0`）来自 `src/lib.rs` 第 251-252 行；如果你不确定，回到该处对照确认。

#### 4.4.5 小练习与答案

**练习 1**：`web-sys = { ..., optional = true }` 里的 `optional = true` 起什么作用？
**答案**：把 `web-sys` 标记为「可选依赖」——默认不会编译它，只有当某个 feature 通过 `dep:web-sys` 显式拉入时（本 crate 里就是 `wasm` feature），它才会进入编译。

**练习 2**：为什么源码里的 cfg 要写成 `all(target_arch = "wasm32", feature = "wasm")` 两个条件，而不是只写一个？
**答案**：因为存在「目标是 wasm32，但用户没开 `wasm` feature」的情况（此时 `web-sys` 没被引入）。如果只用平台条件，源码会去引用一个没编译进来的 `web-sys` 类型而报错；加上 `feature = "wasm"` 这个第二道闸，就能让这段代码在「未启用」时彻底不编译，并走 `0.0` 的退化分支。

**练习 3**：`wasm = ["dep:web-sys"]` 里的 `dep:` 前缀能否省略？
**答案**：在现代 Cargo（resolver v2）里，`dep:` 前缀用来明确「拉入可选依赖本身」，避免与「隐式 feature」混淆，推荐写上。本 crate 写法是规范做法（注意仓库根用了 `resolver = "2"`）。

---

## 5. 综合实践

本讲的综合实践是一个**完整可运行**的小项目：新建一个独立的 Rust 二进制，依赖 typst-timing，验证它的开关 API 真能正常编译和运行。这一步把你前面学到的「依赖怎么加、API 在哪、wasm feature 怎么回事」全部串起来。

> 提示：typst-timing 已作为 `0.15.x` 发布到 crates.io（参见 workspace 根 `Cargo.toml` 的 `version = "0.15.1"`）。下面给出两种引用方式，任选其一。如果你在 Typst workspace 内部做实验，推荐方式 B（path 引用）；如果你在一个全新的独立目录，用方式 A。

### 实践目标

确认 typst-timing 的依赖能被正确引入，且 `enable()` / `is_enabled()` / `disable()` 三个开关 API 行为符合预期。

### 操作步骤

1. 在任意目录新建一个二进制项目（示例命令）：
   ```bash
   cargo new timing-demo
   cd timing-demo
   ```
2. 编辑 `timing-demo/Cargo.toml`，在 `[dependencies]` 下加入 typst-timing。**方式 A（从 crates.io）**：
   ```toml
   [dependencies]
   typst-timing = "0.15"
   ```
   **方式 B（在 Typst 仓库内部，用相对路径引用）**：
   ```toml
   [dependencies]
   typst-timing = { path = "../crates/typst-timing" }
   ```
   > 版本号与可用性以你本地的 crates.io 状态为准；若 `0.15` 拉取失败，可改用方式 B 的 path 引用，标注「待本地验证」。
3. 把 `src/main.rs` 改成下面的内容（**示例代码**，由本讲提供）：
   ```rust
   use typst_timing::{disable, enable, is_enabled};

   fn main() {
       // 默认应该是关闭的（注：进程刚启动时 ENABLED 初值为 false）
       println!("启动时 is_enabled = {}", is_enabled());

       enable();
       println!("enable() 后 is_enabled = {}", is_enabled());

       disable();
       println!("disable() 后 is_enabled = {}", is_enabled());
   }
   ```
4. 运行：
   ```bash
   cargo run
   ```

### 需要观察的现象

程序依次打印三行 `is_enabled` 的值。

### 预期结果

```
启动时 is_enabled = false
enable() 后 is_enabled = true
disable() 后 is_enabled = false
```

如果看到这三行，说明你已成功把 typst-timing 作为依赖引入，并且它的全局开关 API 工作正常——这正是后续所有计时功能的地基。

> 如果运行结果与预期不符（例如版本拉取失败），请按上面的提示改用 path 引用，并把该步骤标注「待本地验证」，不要假装已经跑通。

### 进阶小挑战（可选）

把 `enable()` 保留，再尝试调用 `export_json` 把（目前还几乎为空的）事件导出成一个 JSON 文件。如果你能成功写出文件，哪怕内容很短，也说明你已经摸到了「记录 → 导出」这条主线。具体导出函数的细节会在 **u2-l4** 详细讲解，本挑战只要求你「先跑起来」。

## 6. 本讲小结

- typst-timing 是 Typst workspace 里的**基础设施型 crate**，职责单一：为 Typst 收集性能计时事件，并导出成 Chrome Trace 可视化的 JSON。
- 它是**最被广泛依赖的 crate 之一**：syntax、eval、layout、render、svg、pdf、kit、cli 等十几个核心 crate 都依赖它。
- 整个 crate 只有 `Cargo.toml` + `src/lib.rs` 两个源文件；`src/lib.rs` 约 320 行，组织成「对外 API → 全局状态 → 计时作用域 → 事件模型 → 时间抽象」五大块。
- `Cargo.toml` 演示了 workspace 的统一依赖管理：包元数据和 `parking_lot`/`serde`/`serde_json` 全部 `{ workspace = true }`，版本集中在根 `Cargo.toml`。
- wasm 支持用 **三重门控** 实现：`web-sys` 是「仅 wasm32 目标 + optional」的可选依赖，`wasm` feature 用 `dep:web-sys` 拉入它，源码再用 `cfg(all(target_arch = "wasm32", feature = "wasm"))` 确保不启用时完全不编译。
- 对外 API 主要有：`timed!` 宏、`enable/disable/is_enabled`、`clear`、`export_json`，以及 `TimingScope` 类型——后续讲义会逐个深入。

## 7. 下一步学习建议

本讲建立了「它是什么、长什么样」的全局认识，但**刻意没有展开任何内部机制**。下一讲 **u1-l2《快速上手：启用计时与 timed! 宏》** 会让你第一次真正「用起来」：调用 `enable()`，用 `timed!` 宏和 `TimingScope` 包裹代码块，并导出第一份 Chrome Trace JSON。

建议的后续阅读顺序：

1. **u1-l2**：动手用 `timed!` 记录第一对事件（最快的正反馈）。
2. 然后进入 **u2（核心机制）**：u2-l1 事件数据模型 → u2-l2 全局状态与线程模型 → u2-l3 跨平台时间戳 → u2-l4 导出 JSON。
3. 最后是 **u3（设计取舍与集成）**：零成本门控、宏生态、wasm 计时器、与 typst-kit Timer 的端到端集成。

在进入 u1-l2 之前，建议你回头把本讲的「`src/lib.rs` 源码地图（4.3.2）」记熟——它会是后续所有讲义的导航底图。
