# gpui_util 是什么：定位、依赖与构建

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `gpui_util` 在 zed 工作区中的角色：它是为整个 GPUI 生态提供**公共基础工具**的 crate，自身**不依赖 GPUI**。
2. 读懂它的 `Cargo.toml`，理解「极简依赖 + 平台门控」策略：`log`、`anyhow` 是全平台依赖，`which` 只在 Windows 目标下引入。
3. 理解 workspace（工作区）继承机制：`publish.workspace`、`edition.workspace`、`[lints] workspace = true` 分别从仓库根 `Cargo.toml` 继承什么。
4. 认识 `src/lib.rs` 的门面（顶部注释、导入、`pub mod arc_cow`），并第一次浏览这个 crate 导出的全部公开项。

本讲是整个手册的起点，不要求你已经读过任何 Zed 代码。

## 2. 前置知识

阅读本讲前，用通俗语言先建立这几个概念：

- **crate 与 workspace（工作区）**：Rust 中一个可编译单元叫一个 crate；一个仓库可以通过根目录的 `Cargo.toml` 把几十个 crate 组织成一个 workspace，共享依赖版本和编译配置。zed 仓库就是一个巨大的 workspace，`gpui_util` 是其中位于 `crates/gpui_util` 的一个成员。
- **依赖（dependency）**：crate A 在自己的 `Cargo.toml` 里声明依赖 crate B，才能 `use B::...`。依赖关系是有方向的，如果 `gpui_util` 反过来依赖 `gpui`，而 `gpui` 又依赖 `gpui_util`，就会形成**循环依赖**，Rust 不允许。
- **条件编译（`#[cfg(...)]`）**：Rust 可以按编译目标（target）选择性地编译某段代码。例如 `#[cfg(target_os = "windows")]` 标注的项只在编译 Windows 版本时存在，其他平台上这段代码**根本不会被编译**，因此 Windows 专用依赖也不会被拉进 Linux/macOS 的构建。
- **GPUI**：Zed 自研的 UI 框架 crate（位于 `crates/gpui`），同时提供状态管理与并发原语。GPUI 很重、牵扯平台窗口系统；而像模糊搜索（`fuzzy`）、语言服务器协议（`lsp`）这样的 crate 并不需要一个 UI 框架。
- **`log` 与 `anyhow`**：`log` 是 Rust 生态的标准日志门面 crate（提供 `log::error!` 等宏，不关心日志写到哪里）；`anyhow` 提供动态类型的错误类型 `anyhow::Error`，常用于应用层错误传递。

## 3. 本讲源码地图

本讲涉及的关键文件很少，这个 crate 本身也极小——两个源码文件、合计约 744 行：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `Cargo.toml` | 16 | 声明 crate 名称、依赖（含 Windows 专用依赖）与 workspace 继承 |
| `src/lib.rs` | 603 | crate 根：全部公开工具几乎都定义在这一个文件里（平台工具、宏、`ResultExt`、日志基础设施、Future 适配器、`defer`、`TypeId` 哈希器、部分排序） |
| `src/arc_cow.rs` | 141 | 唯一的子模块：`ArcCow` 智能指针（借用或 `Arc` 所有的二选一类型） |

本讲精读的范围只限于 `Cargo.toml` 全文、`src/lib.rs` 的开头部分（第 1~32 行）以及 `src/arc_cow.rs` 的枚举定义；`lib.rs` 中间的各个区段会在后续讲义中逐个深入。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **gpui_util 的定位**：它在依赖图中处于什么位置、谁在用它。
2. **Cargo.toml 依赖声明**：极简依赖与平台门控、workspace 继承。
3. **lib.rs 顶部注释与导入、arc_cow 模块声明**：crate 的「门面」长什么样。

### 4.1 模块一：gpui_util 的定位——GPUI 生态的公共地基

#### 4.1.1 概念说明

`gpui_util` 解决的问题是：**GPUI 生态里的很多 crate 需要同一批通用小工具（错误日志、延迟执行、智能指针……），但这些工具不应该绑定在 GPUI 这个 UI 框架上。**

如果把这些工具放进 `gpui`，那么任何想用它们的 crate 都必须连带依赖整个 GPUI（以及它背后的窗口系统、渲染器）。对 `fuzzy`（模糊匹配）、`lsp`（语言服务器协议）这类纯逻辑 crate 来说，这是无法接受的重量。于是 Zed 把它们下沉到一个**位于依赖图最底层**的工具 crate：`gpui_util`。

用依赖图表达（箭头表示「依赖」）：

```text
        gpui (UI 框架)   fuzzy (模糊搜索)   lsp   collections   util ...
              │               │              │        │           │
              └───────────────┴──────┬───────┴─────────┴───────────┘
                                     ▼
                               gpui_util            ← 本讲主角，零 GPUI 依赖
                                     │
                          ┌──────────┴──────────┐
                          ▼                     ▼
                        log、anyhow        which（仅 Windows 目标）
```

这个设计带来两个直接好处：

- **无循环依赖**：`gpui` 自己也依赖 `gpui_util`（见下方源码），如果反过来 `gpui_util` 依赖 `gpui` 就会成环。
- **轻量复用**：不需要 UI 的 crate（`fuzzy`、`lsp`、`collections`）也能用上同一批工具，且不必拉入 GPUI。

#### 4.1.2 核心流程

理解定位的「流程」就是理解依赖声明的传播：

1. zed 根 `Cargo.toml` 在 `[workspace.dependencies]` 中登记 `gpui_util = { path = "crates/gpui_util" }`，统一版本入口。
2. 各下游 crate 在自己的 `Cargo.toml` 中写 `gpui_util.workspace = true`，即成为依赖者。
3. 编译某个 crate 时，Cargo 沿依赖图向下收集，`gpui_util`（及其 `log`、`anyhow`）被一并编译。
4. 若编译目标是 Windows，`gpui_util` 中 `#[cfg(target_os = "windows")]` 的代码与 `which` 依赖才会参与编译。

#### 4.1.3 源码精读

**1）workspace 根对它的登记。** 仓库根 `Cargo.toml`（第 271 行起是 `[workspace.dependencies]` 区段）登记了：

[Cargo.toml:L365](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/Cargo.toml#L365)
—— 以相对路径 `crates/gpui_util` 把本 crate 纳入 workspace 共享依赖表，所有下游统一用 `gpui_util.workspace = true` 引用。

**2）下游使用者（以 `gpui` 为例）。** 在 `crates/gpui/Cargo.toml`、`crates/fuzzy/Cargo.toml`、`crates/lsp/Cargo.toml` 等文件中都能 grep 到 `gpui_util`。截至当前 HEAD，经实际检索：

- 工作区内有 **16 个 crate** 依赖它，包括 `gpui`、`gpui_linux`、`gpui_macos`、`gpui_windows`、`fuzzy`、`fuzzy_nucleo`、`lsp`、`collections`、`util`、`ui`、`reqwest_client`、`language` 系等；
- 有 **59 个 `.rs` 源文件**中出现了 `gpui_util::` 调用。

**3）它不依赖 gpui 的证据。** 本 crate 的 `Cargo.toml` 全文只有 16 行（下一模块精读），依赖区里没有任何 GPUI 相关条目——这是「零 GPUI 依赖」的直接证明。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `gpui_util` 在依赖图中的位置——它被谁用、自己用了谁。

**操作步骤**：

1. 进入 zed 仓库根目录。
2. 列出依赖它的 crate：

   ```bash
   grep -rl 'gpui_util' crates/*/Cargo.toml | sort
   ```

3. 数一数有多少个 `.rs` 文件在使用它：

   ```bash
   grep -rl 'gpui_util::' crates --include='*.rs' | wc -l
   ```

4. 打印它的依赖树（默认目标）：

   ```bash
   cargo tree -p gpui_util
   ```

**需要观察的现象**：

- 第 2 步输出约 16 个 `crates/*/Cargo.toml`（外加根 `Cargo.toml`），其中既有 `gpui` 这样的重框架，也有 `fuzzy`、`collections` 这样的轻量 crate。
- 第 4 步的依赖树非常浅：只有 `log` 与 `anyhow` 两棵子树。

**预期结果**：依赖树大致形如 `gpui_util → log, anyhow`，树中没有 `gpui`。具体输出**待本地验证**（`cargo tree` 的完整子树取决于 lockfile 版本）。

#### 4.1.5 小练习与答案

**练习 1**：如果 Zed 开发者把 `ResultExt` 从 `gpui_util` 移到 `gpui` 里，`lsp` crate 想继续用它会有什么后果？

**参考答案**：`lsp` 必须新增对 `gpui` 的依赖，从而连带编译整个 UI 框架及其平台后端，编译时间、二进制体积、测试环境负担都会显著增加；对 `lsp` 这种纯协议库来说完全不值得。这正是工具被下沉到 `gpui_util` 的原因。

**练习 2**：为什么 `gpui_util` 不能反过来依赖 `gpui`？

**参考答案**：因为 `gpui` 已经依赖 `gpui_util`，再反向依赖会构成循环依赖，Cargo 会直接拒绝。更重要的是语义上 `gpui_util` 的定位就是「比 GPUI 更底层」。

**练习 3**：用一句话向同事介绍 `gpui_util`。

**参考答案**：「Zed 工作区里最底层的基础工具箱，给 GPUI 生态所有 crate 共享，但自己完全不依赖 GPUI。」

### 4.2 模块二：Cargo.toml——极简依赖与平台门控

#### 4.2.1 概念说明

`gpui_util` 的 `Cargo.toml` 只有 16 行，却完整展示了两个重要实践：

- **极简依赖**：全平台依赖只有 `log` 和 `anyhow` 两个。基础库依赖越少，下游的编译负担与版本冲突风险越小。
- **按目标平台的依赖声明**：`which`（一个在 `PATH` 中查找可执行文件的 crate）只写在 `[target.'cfg(target_os = "windows")'.dependencies]` 区段下。这意味着 Linux/macOS 构建根本不会编译 `which`——它唯一的用途是 Windows 上探测 PowerShell（见 4.3.3 提到的 `get_windows_system_shell`）。

此外它展示了 workspace 继承的三种写法：`publish.workspace`、`edition.workspace` 与 `[lints] workspace = true`——本 crate 不自己写版本号/版本特性/告警规则，而是从仓库根统一继承。

#### 4.2.2 核心流程

这份清单的读取顺序与语义：

```text
[package]        name = "gpui_util", version = "0.1.0"
     publish.workspace / edition.workspace      ← 从根 Cargo.toml 的 [workspace.package] 继承
[dependencies]
     log.workspace      →  根 Cargo.toml 第 671 行: log = "0.4.16"（带 serde 特性）
     anyhow.workspace   →  根 Cargo.toml 第 521 行: anyhow = "1.0.86"
[target.'cfg(target_os = "windows")'.dependencies]
     which.workspace    →  根 Cargo.toml 第 893 行: which = "6.0.0"
[lints]
     workspace = true   →  继承根 [workspace.lints.rust] / [workspace.lints.clippy]
```

版本号集中在根 `Cargo.toml`，全工作区共享同一份依赖版本——这就是「`.workspace = true`」语法的意义：**本 crate 不指定版本，引用 workspace 级别的定义**。

#### 4.2.3 源码精读

`Cargo.toml` 全文如下：

[Cargo.toml:L1-L16](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml#L1-L16)
—— crate 完整声明：包名 `gpui_util`、版本 `0.1.0`，`publish` 与 `edition` 从 workspace 继承。

逐段说明：

- [Cargo.toml:L7-L9](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml#L7-L9) —— 全平台依赖区：`log`（日志门面）与 `anyhow`（动态错误类型），均以 `.workspace = true` 引用根定义。
- [Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml#L11-L12) —— Windows 专用依赖区：`which` 只在 `cfg(target_os = "windows")` 成立时生效。它是 `src/lib.rs` 中 `which::which_global("pwsh.exe")` 探测调用的支撑。
- [Cargo.toml:L14-L15](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml#L14-L15) —— lint 配置继承自 workspace。

对应的根定义可以在仓库根看到：

- [Cargo.toml:L521](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/Cargo.toml#L521) —— `anyhow = "1.0.86"` 的 workspace 版本定义。
- [Cargo.toml:L671](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/Cargo.toml#L671) —— `log = { version = "0.4.16", features = [...] }`。
- [Cargo.toml:L893](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/Cargo.toml#L893) —— `which = "6.0.0"`。
- [Cargo.toml:L1072-L1079](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/Cargo.toml#L1072-L1079) —— `[workspace.lints]` 区段，例如 clippy 的 `dbg_macro = "deny"`、`todo = "deny"`；本 crate 通过 `[lints] workspace = true` 全盘继承这些规则。

#### 4.2.4 代码实践

**实践目标**：直观看到「平台门控依赖」的效果——同一 crate 在两个编译目标下依赖树不同。

**操作步骤**：

1. 在 zed 仓库根目录执行（默认目标，例如 Linux/macOS 主机）：

   ```bash
   cargo tree -p gpui_util
   ```

2. 再指定 Windows 目标执行（无需安装 Windows 工具链，`cargo tree` 只解析依赖图，不编译）：

   ```bash
   cargo tree -p gpui_util --target x86_64-pc-windows-msvc
   ```

3. 对比两棵树里是否出现 `which`。

**需要观察的现象**：默认目标的依赖树里**没有** `which`；指定 Windows 目标后依赖树里**多出** `which` 及其子依赖。

**预期结果**：两次输出差异即为 `[target.'cfg(target_os = "windows")'.dependencies]` 生效的证据。具体树形输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `which` 不直接放进 `[dependencies]`？

**参考答案**：`which` 只被 Windows 分支的 `get_windows_system_shell` 使用。放进全平台依赖会让 Linux/macOS 构建也无谓地编译它。放进 target 专属区段后，非 Windows 目标的依赖图中它根本不存在，构建更干净、更快。

**练习 2**：`log.workspace = true` 与直接写 `log = "0.4"` 有什么区别？

**参考答案**：前者表示「版本与特性由仓库根 `Cargo.toml` 的 `[workspace.dependencies]` 统一决定」，本 crate 无权（也无需）指定；后者是本 crate 自己锁定版本。zed 仓库选择前者，保证几十个 crate 用的是同一个 `log` 版本与特性集，避免重复编译不同版本。

**练习 3**：`[lints] workspace = true` 会带来什么效果？

**参考答案**：本 crate 自动继承根 `Cargo.toml` 中 `[workspace.lints.rust]` 与 `[workspace.lints.clippy]` 声明的所有 lint 等级（例如 `clippy::dbg_macro` 为 deny），因此即使这么小的 crate 也必须遵守全仓库统一的代码规范。

### 4.3 模块三：lib.rs 门面——顶部注释、导入与 arc_cow 模块声明

#### 4.3.1 概念说明

Rust crate 的「门面」是它的库根文件（本 crate 是 `src/lib.rs`）：外界能看见什么，完全由这个文件（及其声明出的子模块）中以 `pub` 标注的项决定。

`gpui_util` 的门面有三个值得注意的细节：

1. **第 1~2 行是一段「历史痕迹」注释**——它不在任何 `//!` 文档注释语法里，而是两行普通注释，记录了曾经可能从这里导出的内容。这类注释是了解 crate 演化历史的线索（第三单元会专门讨论）。
2. **导入区只使用 `std`**——`use std::{env, ffi::OsStr, ...}` 全部指向标准库，第三方能力（`log`、`anyhow`）在代码体内以 `log::`、`anyhow::` 路径直接引用。这本身就再次印证了依赖极简。
3. **唯一的子模块声明 `pub mod arc_cow;`**——`lib.rs` 其余所有工具都直接定义在库根，只有 `ArcCow` 智能指针独立成文件。这也是本 crate 刻意保持「大单文件 + 一个子模块」的组织方式。

#### 4.3.2 核心流程

浏览这个 crate 的方法（也是本讲实践任务的基础）：

```text
打开 src/lib.rs
  ├─ L1~L2     历史注释（FluentBuilder / 一行旧 pub use）
  ├─ L4~L13    std 导入
  ├─ L15       pub mod arc_cow;          ← 唯一子模块入口
  ├─ L17~L146  #[cfg(target_os = "windows")] 平台工具区
  ├─ L148~L209 通用小函数与宏（post_inc / measure / debug_panic! / maybe!）
  ├─ L210~L336 ResultExt trait 与日志内部实现
  ├─ L338~L503 Future 适配器（TryFutureExt / LogErrorFuture / UnwrapFuture）
  ├─ L505~L527 defer 与 Deferred
  ├─ L529~L564 TypeId 哈希器
  ├─ L566~L581 内嵌单元测试 type_id_hasher
  └─ L583~L603 truncate_to_bottom_n_sorted_by 部分排序工具
```

「查找全部公开项」即扫描文件中的 `pub fn`、`pub struct`、`pub trait`、`pub mod`、`pub enum` 与 `#[macro_export]` 宏。

#### 4.3.3 源码精读

**1）顶部的历史注释。**

[src/lib.rs:L1-L2](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L1-L2)
—— 两行普通注释：第一行写着 `FluentBuilder`，第二行是一行被注释掉的旧导出 `pub use gpui_util::{FutureExt, Timeout, arc_cow::ArcCow};`。合读可知 `FluentBuilder`、`FutureExt`、`Timeout` 这些名字曾与本 crate 相关（后被迁出），`ArcCow` 则仍在（只是改为经 `pub mod arc_cow` 子模块访问）。这是源码留给读者的「搬家记录」。

**2）std-only 导入区。**

[src/lib.rs:L4-L13](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L4-L13)
—— 从标准库导入 `env`、`OsStr`、`AddAssign`、`panic::Location`、`Pin`、`OnceLock`、`Context/Poll`、`Instant` 等，分别服务于后文的 `measure`、`new_std_command`、`post_inc`、`#[track_caller]` 日志、手写 Future 等工具。没有任何第三方 `use`。

**3）子模块声明。**

[src/lib.rs:L15](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L15)
—— `pub mod arc_cow;` 把 `src/arc_cow.rs` 挂载为公开子模块，外界以 `gpui_util::arc_cow::ArcCow` 路径访问。

**4）arc_cow.rs 的核心定义。**

[src/arc_cow.rs:L9-L12](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L9-L12)
—— `pub enum ArcCow<'a, T: ?Sized>` 只有两个变体：`Borrowed(&'a T)`（借用一份已有数据）与 `Owned(Arc<T>)`（引用计数拥有）。它像 `std::borrow::Cow` 一样灵活，但克隆 `Owned` 变体只是递增引用计数而非复制数据。本讲只需认识它的形状，第 2 单元第 6 讲会逐个 trait 精读。

**5）门面之后的第一个公开项——平台门控的函数。**

[src/lib.rs:L17-L32](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L17-L32)
—— `new_std_command` 用一对互补的 `cfg` 提供同名函数的两个实现：Windows 版创建子进程时设置 `CREATE_NO_WINDOW`（`0x0800_0000`）标志以避免弹出控制台窗口（注意第 18 行的 `const CREATE_NO_WINDOW` **没有 `pub`**，是私有常量）；非 Windows 版直接转发 `std::process::Command::new`。调用方在任何平台都写 `gpui_util::new_std_command(...)`，由编译器挑选实现。

#### 4.3.4 代码实践

**实践目标**：用文档工具生成 crate 的公开项清单，并学会对照源码验证。

**操作步骤**：

1. 在 zed 仓库根目录生成文档（不生成依赖的文档，速度快）：

   ```bash
   cargo doc -p gpui_util --no-deps
   ```

2. 打开 `target/doc/gpui_util/index.html`（或用 `cargo doc -p gpui_util --no-deps --open`），浏览左侧的模块/结构体/trait/函数/宏列表。
3. 在源码侧统计公开项：

   ```bash
   grep -n -E '^pub (fn|struct|enum|trait|mod)|^macro_rules|^#\[macro_export\]' crates/gpui_util/src/lib.rs
   ```

4. 对照第 5 节「综合实践」中的清单表核对数量。

**需要观察的现象**：文档页面上，Functions 一栏里**不会**出现 `get_windows_system_shell`（因为在非 Windows 主机上生成文档时，该 `cfg` 分支未编译）；Functions 一栏**会**出现 `new_std_command`（双定义在文档上合并为一个条目）。

**预期结果**：你在非 Windows 主机上看到的公开项就是下文综合实践清单中标注「全平台」的那些。Windows 专属条目需交叉编译目标下才能见到，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`src/lib.rs` 第 1~2 行的注释为什么不是 `//!` 文档注释？

**参考答案**：`//!` 是会进入 rustdoc 的 crate 级文档注释；这两行用的是普通 `//`，说明作者并不打算把它作为对外文档展示，它更像是给维护者看的备忘/历史记录（记录哪些导出已从这里迁走）。

**练习 2**：外界如何使用 `ArcCow`？写出完整路径。

**参考答案**：由于 `pub mod arc_cow;` 且类型在 `src/arc_cow.rs` 中以 `pub enum ArcCow` 定义，完整路径是 `gpui_util::arc_cow::ArcCow`。注意顶部注释里那行被注释掉的 `pub use ... arc_cow::ArcCow` 若恢复，则可以短路径 `gpui_util::ArcCow` 使用——这正是注释记录的历史差异。

**练习 3**：`new_std_command` 在 Windows 与非 Windows 上有两个同名定义，为什么不算重复定义错误？

**参考答案**：两个定义分别被 `#[cfg(target_os = "windows")]` 与 `#[cfg(not(target_os = "windows"))]` 门控，任何一次编译只有一个分支被编译进去，因此不会冲突；调用方代码则完全一致。

## 5. 综合实践

**任务**：为 `gpui_util` 制作一份「公开项清单表」，并标注平台可见性。这是本讲最重要的产出，后续每一讲都会从中取材。

**操作步骤**：

1. 在 zed 仓库根目录运行编译检查与文档生成：

   ```bash
   cargo check -p gpui_util
   cargo doc -p gpui_util --no-deps
   ```

2. 用 4.3.4 的 grep 命令扫描 `src/lib.rs`，再打开 `src/arc_cow.rs` 找出其中的 `pub enum`。
3. 把所有公开项按下表分类整理（函数 / 宏 / trait / 结构体 / 枚举 / 模块），标注定义行号与平台。

**预期结果**（依据当前 HEAD 源码整理，可作为核对答案）：

| 类别 | 名称 | 定义位置 | 平台 |
| --- | --- | --- | --- |
| 模块 | `arc_cow` | lib.rs L15 | 全平台 |
| 函数 | `new_std_command` | lib.rs L21（win）/ L30（非 win） | 全平台（双实现） |
| 函数 | `get_windows_system_shell` | lib.rs L35 | 仅 Windows |
| 函数 | `post_inc` | lib.rs L148 | 全平台 |
| 函数 | `measure` | lib.rs L154 | 全平台 |
| 宏 | `debug_panic!` | lib.rs L174（`#[macro_export]` 于 L173） | 全平台 |
| 函数 | `some_or_debug_panic` | lib.rs L186 | 全平台 |
| 宏 | `maybe!` | lib.rs L199（`#[macro_export]` 于 L198） | 全平台 |
| trait | `ResultExt` | lib.rs L210 | 全平台 |
| 函数 | `log_err`（自由函数） | lib.rs L324 | 全平台 |
| trait | `TryFutureExt` | lib.rs L338 | 全平台 |
| trait | `TryFutureExtBacktrace` | lib.rs L357 | 全平台 |
| 结构体 | `LogErrorFuture` | lib.rs L434 | 全平台 |
| 结构体 | `LogErrorWithBacktraceFuture` | lib.rs L461 | 全平台 |
| 结构体 | `UnwrapFuture` | lib.rs L487 | 全平台 |
| 结构体 | `Deferred` | lib.rs L505 | 全平台 |
| 函数 | `defer` | lib.rs L524 | 全平台 |
| 结构体 | `TypeIdHashBuilder` | lib.rs L529 | 全平台 |
| 结构体 | `TypeIdHasher` | lib.rs L540 | 全平台 |
| 函数 | `truncate_to_bottom_n_sorted_by` | lib.rs L583 | 全平台 |
| 枚举 | `ArcCow` | arc_cow.rs L9 | 全平台 |

两点额外观察（重要）：

- 表中「仅 Windows」的公开项只有 `get_windows_system_shell` 一个。
- `CREATE_NO_WINDOW`（lib.rs L18）虽然出现在 cfg 区段里，但它**没有 `pub`**，是私有常量，不应出现在清单中——这是练习「grep 到的项≠公开项」的好例子。grep 扫描结果与 `cargo doc` 页面的差异，请以本地实际输出为准（**待本地验证**）。

## 6. 本讲小结

- `gpui_util` 是 zed 工作区最底层的基础工具 crate：为 GPUI 生态（含 `gpui` 本身以及不需要 UI 的 `fuzzy`、`lsp`、`collections` 等 16 个 crate、约 59 个源文件）提供公共工具，自身零 GPUI 依赖，从而避免循环依赖并让轻量 crate 免于背负 UI 框架。
- 它的 `Cargo.toml` 只有 16 行：全平台依赖仅 `log` 与 `anyhow`，`which` 通过 `[target.'cfg(target_os = "windows")'.dependencies]` 仅在 Windows 目标引入。
- `publish.workspace`、`edition.workspace`、`[lints] workspace = true` 体现了 workspace 统一管理：版本、发布策略与 lint 规则都继承自仓库根 `Cargo.toml`。
- `src/lib.rs` 是一个「大单文件」门面：顶部两行历史注释记录了 `FluentBuilder`/`FutureExt`/`Timeout` 的迁出痕迹，唯一的子模块是 `pub mod arc_cow`（内含 `ArcCow` 枚举）。
- 公开项里只有一个 Windows 专属函数 `get_windows_system_shell`；`new_std_command` 用一对互补 `cfg` 在所有平台提供统一签名。
- 制作「公开项清单表」（第 5 节）是后续所有讲义的地图：第 2 单元的每一讲对应表中的一项或几项。

## 7. 下一步学习建议

下一讲（`u1-l2-source-map-and-cfg.md`）将把本讲只浏览过的 `src/lib.rs` 展开成完整的**分区地图**：平台工具、宏、`ResultExt`、日志基础设施、Future 适配器、`defer`、哈希器、部分排序工具各占哪个区段，并深入讲解 `#[cfg(...)]` 条件编译的更多细节（如何在非 Windows 平台上阅读、验证 Windows 分支）。

在进入下一讲之前，建议你先自己动手做一遍第 5 节的清单表——之后阅读任何一讲，都可以回到这张表定位「现在读到的项在 crate 全貌中的位置」。
