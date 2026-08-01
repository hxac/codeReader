# 项目概览、构建脚本与导出全景

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `typst-utils` 在 Typst 工作区里的**定位**：它是什么、为谁服务、依赖方向是怎样的。
- 看懂它的**目录结构**，知道每一类工具大致放在哪个文件。
- 读懂 `Cargo.toml`，理解 Cargo **工作区继承**（`{ workspace = true }`）这种依赖与版本声明方式。
- 理解 `build.rs` 如何通过 `cargo:rustc-env` 把版本号和 commit 注入到**编译期环境变量**，以及 `src/version.rs` 又如何读回它们。
- 在脑中建立 `src/lib.rs` 的「**模块声明 + `pub use` 重导出**」全景图，知道每个公开类型从哪里来。

本讲是整个 `typst-utils` 学习手册的**第一篇**，不默认你读过任何 Typst 源码。

## 2. 前置知识

- **Rust 基础**：模块（`mod`）、可见性（`pub`）、trait、宏（`macro_rules!`）的概念。
- **Cargo 基础**：`Cargo.toml`、workspace（工作区）、依赖声明。
- **什么是构建脚本（build.rs）**：Cargo 在编译你的 crate 之前，会先编译并运行 `build.rs`。它通过向标准输出打印特殊的 `cargo:` 指令来影响后续编译（例如设置编译期环境变量）。
- **编译期宏 `env!` / `option_env!`**：在**编译时**把环境变量的值「烧」进二进制，得到一个 `&'static str`，而不是运行时用 `std::env::var` 去读。

> 名词解释：**crate** 是 Rust 的编译单元，一个 `Cargo.toml` 对应一个 crate；**workspace** 把多个相关 crate 放在一起统一管理版本和依赖。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Cargo.toml` | crate 清单：名字、版本、依赖。大部分字段用 `{ workspace = true }` 从工作区根 `Cargo.toml` 继承 |
| `build.rs` | 构建脚本：在编译期注入 `TYPST_VERSION` 与 `TYPST_COMMIT_SHA` 两个环境变量 |
| `src/lib.rs` | crate 根：声明所有子模块、用 `pub use` 重导出公开 API，并内联定义了一批扩展 trait 与辅助函数 |
| `src/version.rs` | 读取 `build.rs` 注入的环境变量，解析出 `TypstVersion` |
| `src/macros.rs` | `singleton!` 等声明式宏（`version()` 内部用到了 `singleton!`） |
| `src/scalar.rs` | `Scalar` 类型（本讲实践任务会用到 `Scalar::new`） |

> 一个来自真实源码的事实：`typst-utils` 这个 crate **本身没有独立的 `README.md`**。它的 `Cargo.toml` 里写着 `readme = { workspace = true }`，指向的是**仓库根目录**的 `README.md`（那份文件介绍的是整个 Typst 项目，而不是 typst-utils）。这说明 `typst-utils` 是一个「内部工具 crate」，不单独面向终端用户宣传。手册后面的「综合实践」会带你验证这一点。

## 4. 核心概念与源码讲解

### 4.1 crate 定位与目录结构

#### 4.1.1 概念说明

`typst-utils` 是 Typst 工作区里的「**工具杂烩**」基础库。它不解决某一个具体业务问题，而是把整个 Typst 各 crate 都会用到的通用工具集中起来：确定性的浮点 `Scalar`、高精度舍入、时长格式化、各种小集合（`BitSet`/`ListSet`）、哈希体系、胖指针操作、字符串内化、后台并行等等。

它有两个鲜明特点：

- **它是「被依赖方」**：Typst 的几乎所有 crate（`typst-eval`、`typst-layout`、`typst-pdf`……）都可能依赖 `typst-utils`，但 `typst-utils` 自身**不依赖任何 `typst-*` crate**。它的依赖里只有 `once_cell`、`rayon`、`siphasher` 这类第三方基础库。这条规则保证了依赖图不会形成环。
- **它没有单一主流程**：不像编译器有一条「源码 → PDF」的主链路，`typst-utils` 是一组相对独立的工具。所以本手册的学习顺序是「先建全局认知，再逐个主题吃透」。

#### 4.1.2 核心流程

`typst-utils` 的目录结构非常扁平，所有源码都在 `src/` 下，**每个文件对应一个主题**：

```
typst-utils/
├── Cargo.toml      # 清单（依赖、版本）
├── build.rs        # 构建脚本（注入版本环境变量）
└── src/
    ├── lib.rs      # crate 根：模块声明 + pub use + 内联工具
    ├── macros.rs   # singleton! / sub_impl! / assign_impl! / display!
    ├── scalar.rs   # Scalar 确定性浮点
    ├── round.rs    # 高精度舍入
    ├── duration.rs # 时长格式化
    ├── bitset.rs   # BitSet / SmallBitSet
    ├── listset.rs  # ListSet
    ├── hash.rs     # hash128 / LazyHash / ManuallyHash / HashLock
    ├── pico.rs     # PicoStr 字符串内化
    ├── fat.rs      # 胖指针操作
    ├── protected.rs# Protected 访问守卫
    ├── deferred.rs # Deferred 后台并行
    └── version.rs  # 版本信息
```

记住这个「一文件一主题」的结构，后面每一篇讲义基本都只聚焦其中一个文件。

#### 4.1.3 源码精读

要确认「`typst-utils` 不依赖任何 `typst-*` crate」，直接看它的 `[dependencies]` 段即可——里面全是外部库：

- 引用 [Cargo.toml:15-25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L15-L25)：这里是 `[dependencies]` 段，列出了 10 个依赖，全部带 `{ workspace = true }`，**没有任何 `typst-` 开头的依赖**，这就坐实了它「最底层基础库」的定位。

#### 4.1.4 代码实践

**实践目标**：亲手确认目录结构，把上面的「文件→主题」对照表与现实对上。

**操作步骤**：

1. 在 `typst-utils` 目录下执行 `ls src/`（或只读命令 `git ls-files src/`）。
2. 数一数 `src/` 下共有多少个 `.rs` 文件。
3. 对照 4.1.2 的树形图，给每个文件找到它对应的主题。

**需要观察的现象**：`src/` 下应有 13 个 `.rs` 文件（含 `lib.rs`），与树形图一致。

**预期结果**：文件清单为 `lib.rs macros.rs scalar.rs round.rs duration.rs bitset.rs listset.rs hash.rs pico.rs fat.rs protected.rs deferred.rs version.rs`。（待本地验证：不同版本可能增删文件。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `typst-utils` 不能依赖 `typst-eval`？

> **参考答案**：因为 `typst-eval` 已经依赖了 `typst-utils`（业务 crate 用基础库）。如果 `typst-utils` 反过来再依赖 `typst-eval`，依赖图就会形成环，Cargo 会拒绝编译。依赖方向必须单向：从上层业务 crate 指向底层基础库。

**练习 2**：如果要在 `typst-utils` 里新增一个「字符串相似度计算」的工具，按现有约定应该新建哪个文件、在 `lib.rs` 里做什么？

> **参考答案**：按「一文件一主题」约定，新建 `src/levenshtein.rs`（或类似名），在 `lib.rs` 里用 `mod levenshtein;` 声明私有模块，再用 `pub use self::levenshtein::{...};` 把想公开的类型/函数重导出到 crate 根（参考 4.4 节的模式）。

### 4.2 Cargo.toml 依赖清单

#### 4.2.1 概念说明

Cargo **工作区（workspace）**允许多个 crate 共享元信息，避免每个 crate 重复写版本号。`typst-utils` 的 `Cargo.toml` 大量使用 `{ workspace = true }`，意思是「这个字段的值去工作区根 `Cargo.toml` 的 `[workspace.package]` / `[workspace.dependencies]` 里取」。

#### 4.2.2 核心流程

1. 工作区根 `Cargo.toml` 的 `[workspace.package]` 定义公共字段（如 `version`、`edition`、`rust-version`）。
2. `[workspace.dependencies]` 定义每个依赖的固定版本号，供各 crate 引用。
3. `typst-utils` 的 `Cargo.toml` 只写依赖**名字** + `{ workspace = true }`，不重复写版本号。

这样做的好处是：升级依赖时只需改工作区根的一处，所有 crate 同步生效。

#### 4.2.3 源码精读

- 引用 [Cargo.toml:1-13](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L1-L13)：`[package]` 段里 `version`、`edition`、`authors`、`license` 等全部是 `{ workspace = true }`，连 `description = "Utilities for Typst."` 这种短描述是少数写死的字段之一。
- 引用 [Cargo.toml:15-25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L15-L25)：`[dependencies]` 段，10 个依赖全部 `{ workspace = true }`。

下面这张表把每个依赖和它**实际被用到的位置**一一对应（这些用法都已在源码中核实）：

| 依赖 | 用途 | 被使用的位置 |
|---|---|---|
| `once_cell` | 惰性初始化容器；也被 crate 重新导出供下游使用 | `deferred.rs` 的 `OnceCell`；`lib.rs` 的 `pub use once_cell` |
| `portable-atomic` | 在更多平台上可用的原子类型 | `hash.rs` 的 `AtomicU128`（`HashLock`） |
| `rayon` | 数据并行 / 后台线程 | `deferred.rs` 的 `Deferred`（`rayon::spawn`） |
| `rustc-hash` | 快速哈希器 `FxHashMap` | `pico.rs` 的运行期 interner |
| `semver` | 解析语义化版本字符串 | `version.rs` 的 `Version::parse` |
| `siphasher` | 跨架构稳定的 SipHash | `hash.rs` 的 `hash128` |
| `smallvec` | 栈上小数组 `SmallVec` | `lib.rs` 的 `Rdedup` 实现、`listset.rs` |
| `thin-vec` | 把长度存在堆上的 `ThinVec`（省一个字） | `bitset.rs` 的 `BitSet` |
| `unicode-math-class` | 查询字符的 Unicode 数学分类 | `lib.rs` 的 `default_math_class` |
| `libm` | 跨平台确定性数学函数 | `round.rs` 的 `libm::exp10` |

> 为什么 `round.rs` 要用 `libm::exp10` 而不是直接 `10f64.powi(...)`？因为不同平台/编译器的浮点实现可能有细微差异，而 Typst 追求**跨平台确定性**（同一份文档在任何机器上编译结果一致）。`libm` 提供统一的实现。这个主题会在进阶篇「round.rs」讲义里展开。

#### 4.2.4 代码实践

**实践目标**：体会「工作区继承」如何避免版本号重复。

**操作步骤**：

1. 打开仓库根的 `Cargo.toml`，找到 `[workspace.package]` 段，记录 `version`、`edition`、`rust-version` 的值。
2. 打开 `crates/typst-utils/Cargo.toml`，确认它通过 `{ workspace = true }` 继承了这些值。
3. 在 `[workspace.dependencies]` 段找到 `libm` 的固定版本号，确认 `typst-utils` 不需要重复写。

**需要观察的现象**：`typst-utils/Cargo.toml` 里看不到任何具体的版本数字，全是 `{ workspace = true }`。

**预期结果**：工作区根 `Cargo.toml` 中 `version = "0.15.1"`、`edition = "2024"`、`rust-version = "1.92"`；`typst-utils` 全部继承。（待本地验证：值会随仓库版本变化。）

#### 4.2.5 小练习与答案

**练习 1**：如果把工作区根 `Cargo.toml` 里的 `edition` 从 `"2024"` 改成 `"2021"`，`typst-utils` 会受影响吗？

> **参考答案**：会。因为 `typst-utils/Cargo.toml` 写的是 `edition = { workspace = true }`，它的 edition 值直接来自工作区根。改一处，所有 `{ workspace = true }` 的 crate 全部跟着变。注意 `build.rs` 里用到了 `if ... && let Some(...) = ...` 这种 **let-chain** 语法（见 4.3.3），它依赖 2024 edition，改回 2021 可能导致编译失败。

**练习 2**：`typst-utils` 里有两处提到 `once_cell`（一处是依赖，一处是 `lib.rs` 的 `pub use once_cell`），它们是什么关系？

> **参考答案**：依赖声明（`Cargo.toml`）让 `once_cell` 可用；`lib.rs` 的 `pub use once_cell;` 则把它**重新导出**给依赖 `typst-utils` 的下游 crate，使下游能写 `typst_utils::once_cell::...` 而不必自己声明该依赖。这行导出带了 `#[doc(hidden)]`，表示「这是给内部/下游方便用的，不对外宣传成公开 API」。

### 4.3 build.rs 版本环境变量注入

#### 4.3.1 概念说明

这是本讲最有「机制感」的部分。

**问题**：Typst 想在运行时知道自己**编译时**的版本号和 git commit（比如 `typst --version` 要打印它们）。这两个值在编译时就定死了，运行时不会再变。

**Rust 的做法**：用「编译期环境变量」。`build.rs` 在编译前把值写入编译期环境，源码用 `env!` 宏在**编译时**把它们烧进二进制，变成 `&'static str`。

这里有一个关键设计：**外部构建工具（CI、打包脚本）可以主动设置这两个环境变量来覆盖默认值**；只有当外部没设置时，`build.rs` 才用包版本和 git commit 作为兜底。这正好支持了「打包到 crates.io 时用包版本」「从 git 源码构建时用真实 commit」等不同场景。

#### 4.3.2 核心流程

下面是完整的注入链路（文字版流程图）：

```
① cargo 决定编译 typst-utils
        │
        ▼
② 先编译并运行 build.rs
        │
        ├─ option_env!("TYPST_VERSION") 检查：外部已设置了吗？
        │     ├─ 没设置 → cargo:rustc-env=TYPST_VERSION=<CARGO_PKG_VERSION>（兜底）
        │     └─ 已设置 → 不做事（尊重外部值）
        │
        └─ option_env!("TYPST_COMMIT_SHA") 检查：外部已设置吗？
              ├─ 没设置 → 跑 `git rev-parse HEAD`，成功则 cargo:rustc-env=TYPST_COMMIT_SHA=<sha>
              └─ 已设置 / git 不可用 → 不做事
        │
        ▼
③ 编译 src/ 下源码
        │
        └─ version.rs 里：
              env!("TYPST_VERSION")         → 读到兜底或外部的值
              option_env!("TYPST_COMMIT_SHA") → 读到 commit 或 None
        │
        ▼
④ 运行时调用 version()
        └─ 这些值已是烧进去的 &'static str，singleton! 保证只解析一次
```

三个必须分清的概念：

- **`env!` / `option_env!` 是编译期宏**，不是运行时的 `std::env::var`。它在编译时就把值变成了字符串字面量。`env!` 在变量不存在时编译报错；`option_env!` 在不存在时返回 `None`。
- **`cargo:rustc-env=KEY=VAL`** 设置的环境变量，只对「本 crate 的 lib/bin 编译」可见，**不影响 `build.rs` 自己**。所以 `build.rs` 里用 `option_env!` 读到的是「外部真实环境」，用它来判断「要不要兜底」，逻辑自洽。
- **`cargo:rerun-if-env-changed=KEY`** 告诉 Cargo：只要 `KEY` 这个环境变量变了，就重新运行 `build.rs`。否则 Cargo 可能缓存 build 结果不重跑。

#### 4.3.3 源码精读

- 引用 [build.rs:4-5](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L4-L5)：声明「只要 `TYPST_VERSION` 或 `TYPST_COMMIT_SHA` 环境变量变化，就重新运行本构建脚本」。
- 引用 [build.rs:7-9](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L7-L9)：`option_env!("TYPST_VERSION").is_none()` 为真（即外部没设置）时，用 `cargo:rustc-env` 把 `TYPST_VERSION` 设为 `CARGO_PKG_VERSION`（Cargo 内置变量，等于包版本）。
- 引用 [build.rs:11-20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L11-L20)：同理处理 `TYPST_COMMIT_SHA`。若外部没设置，则调用 `git rev-parse HEAD` 取当前 commit；只有命令执行成功（`status.success()`）且输出是合法 UTF-8 时才注入。这里用到了 `if ... && let Some(sha) = ...` 这种 **let-chain** 语法，对应 `Cargo.toml` 继承来的 `edition = "2024"`。

> `git rev-parse HEAD` 失败的常见场景：源码来自 crates.io（打包时 `.git` 目录被剥离），或构建环境没装 git。此时 `TYPST_COMMIT_SHA` 不会被注入，下游 `option_env!` 得到 `None`，最终 `display_commit` 会显示 `"unknown commit"`（见下面 version.rs）。

读回这一侧在 `version.rs`：

- 引用 [version.rs:18-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L18-L37)：`version()` 函数。第 20 行 `env!("TYPST_VERSION")` 必然有值（`build.rs` 兜底保证了），第 21 行 `option_env!("TYPST_COMMIT_SHA")` 可能为 `None`。整个函数体被 `singleton!` 包裹——意味着全局只解析一次，之后直接返回缓存的 `&'static` 引用。
- 引用 [version.rs:22-31](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L22-L31)：用 `semver::Version::parse(raw)` 把版本字符串解析成 major/minor/patch，构造 `TypstVersion`。解析失败会 `panic!`（文档注释里也写了 `# Panics`）。
- 引用 [macros.rs:1-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L1-L8)：`singleton!` 宏的定义。它展开成一个 `std::sync::LazyLock` 静态变量，第一次访问时运行初始化闭包，之后返回同一个值的引用。这就是 `version()` 只解析一次的原理。
- 引用 [version.rs:93-99](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L93-L99)：`display_commit` 把可能很长的 commit hash 截断成前 8 个字符；若为 `None` 返回 `"unknown commit"`。

#### 4.3.4 代码实践

**实践目标**：亲手把一个自定义版本号「注入」到 `typst-utils`，体会 build.rs 的覆盖机制。

**操作步骤**（这是「源码阅读 + 思想实验」型实践，不需要改 typst 源码）：

1. 重新读 [build.rs:7-9](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L7-L9)，确认逻辑是「只有 `TYPST_VERSION` **未设置**时才兜底」。
2. 思考：如果在编译 typst-utils 前，先执行 `export TYPST_VERSION=9.9.9`，那么：
   - `build.rs` 里 `option_env!("TYPST_VERSION")` 得到 `Some("9.9.9")`，`is_none()` 为假，**不会**走兜底分支。
   - `version.rs` 里 `env!("TYPST_VERSION")` 读到的就是 `"9.9.9"`。
3. （可选，待本地验证）在 typst 仓库根执行 `TYPST_VERSION=9.9.9 cargo build -p typst-utils`，再写一个临时二进制调用 `typst_utils::version()` 打印，确认 `raw()` 返回 `"9.9.9"`、`major()` 返回 `9`。

**需要观察的现象**：外部设置的环境变量**优先于** build.rs 的兜底默认值。

**预期结果**：`version().raw()` 返回外部设置的值；若不设置，则返回包版本（`0.15.1`）。`display_commit(version().commit())` 在 git 仓库内构建时返回 commit 前 8 位，在 crates.io 源码构建时返回 `"unknown commit"`。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `version.rs` 里读 `TYPST_VERSION` 用 `env!`，而读 `TYPST_COMMIT_SHA` 用 `option_env!`？

> **参考答案**：`build.rs` 保证 `TYPST_VERSION` 一定有值（外部没设就用包版本兜底），所以可以用 `env!`（缺值即编译错误，但这种情况不会发生）。而 `TYPST_COMMIT_SHA` 在「git 不可用」或「外部未设置」时不会被注入，可能不存在，所以必须用 `option_env!` 得到 `Option<&'static str>`。

**练习 2**：从 crates.io 下载的 `typst-utils` 源码包，调用 `version().commit()` 最可能返回什么？为什么？

> **参考答案**：最可能返回 `None`（对应 `display_commit` 显示 `"unknown commit"`）。因为 crates.io 打包时会剥离 `.git` 目录，`build.rs` 执行 `git rev-parse HEAD` 会失败（或源码根本不在 git 仓库内），于是不会注入 `TYPST_COMMIT_SHA`。

### 4.4 lib.rs 模块与 pub use 导出

#### 4.4.1 概念说明

Rust 的 crate 根文件（`lib.rs`）负责两件事：**声明内部模块**、**决定对外暴露什么**。

`typst-utils` 用「**私有 `mod` + 选择性 `pub use`**」的模式控制公开 API 表面：

- 内部模块大多声明为**私有**（`mod foo;`），外界无法直接访问 `typst_utils::foo::...`。
- 只把想公开的类型/函数用 `pub use` **重导出**到 crate 根，这样用户写 `typst_utils::Scalar` 而不是 `typst_utils::scalar::Scalar`。

这样做的好处是：内部模块结构可以自由调整（重命名、拆分），只要 `pub use` 那一行不变，对外 API 就稳定。

#### 4.4.2 核心流程

`lib.rs` 顶部的组织顺序是：

1. `pub mod fat;` —— **唯一**的公开模块。
2. `#[macro_use] mod macros;` —— 宏模块，`#[macro_use]` 让其中的宏在**整个 crate 内**可用（无需手动 `use`）。
3. 一串**私有**模块：`bitset, deferred, duration, hash, listset, pico, protected, round, scalar, version_`。
4. 一串 **`pub use`**：把私有模块里的公开类型/函数提到 crate 根。

有一个值得注意的细节：版本模块写成 `#[path = "version.rs"] mod version_;`——文件叫 `version.rs`，但模块名故意叫 `version_`（带下划线），是为了**避免和导出的 `version` 函数重名**（`pub use self::version_::{..., version};`）。这是一个常用的小技巧。

#### 4.4.3 源码精读

- 引用 [lib.rs:3](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L3)：`pub mod fat;` —— 整个 crate 里唯一以「公开模块」形式暴露的模块（胖指针操作），其它模块都是私有 `mod` + `pub use`。
- 引用 [lib.rs:5-6](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L5-L6)：宏模块声明。`#[macro_use]` 表示其中的 `singleton!`、`sub_impl!` 等宏在后续模块里可直接使用（比如 `version.rs` 用到了 `singleton!`）。
- 引用 [lib.rs:7-17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L7-L17)：私有模块声明。注意第 16-17 行的 `#[path = "version.rs"] mod version_;`。
- 引用 [lib.rs:19-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L19-L28)：核心的 `pub use` 段——这就是 `typst-utils` 对外的**公开 API 清单**。
- 引用 [lib.rs:30-31](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L30-L31)：`#[doc(hidden)] pub use once_cell;` —— 重新导出 `once_cell` 给下游用，但 `#[doc(hidden)]` 表示它不出现在生成的文档里，属于「非正式公开 API」。

把 [lib.rs:19-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L19-L28) 整理成「公开 API 全景表」，这就是后面所有讲义会逐一讲解的对象：

| 公开名字 | 来自哪个私有模块 | 一句话作用 |
|---|---|---|
| `BitSet`, `SmallBitSet` | `bitset` | 位压缩集合 |
| `Deferred` | `deferred` | 后台并行预计算 |
| `format_duration` | `duration` | 把 `Duration` 格式化成可读字符串 |
| `hash128`, `LazyHash`, `ManuallyHash`, `HashLock` | `hash` | 哈希工具体系 |
| `ListSet` | `listset` | 小集合 |
| `PicoStr`, `ResolvedPicoStr` | `pico` | 字符串内化 |
| `Protected` | `protected` | 访问守卫类型 |
| `round_int_with_precision`, `round_with_precision` | `round` | 高精度舍入 |
| `Scalar` | `scalar` | 可哈希可排序的确定性浮点 |
| `TypstVersion`, `display_commit`, `version` | `version_` | 版本信息 |

> 注意：`lib.rs` 里还**内联**定义了一批扩展 trait 和辅助函数（如 `debug`、`display`、`SliceExt`、`Numeric`、`Static`、`Get`、`defer`、`DefSite` 等）。它们不在 `pub use` 列表里，因为它们就定义在 `lib.rs` 本身、天然就是公开的。这些内容是下一篇讲义（u1-l2「扩展 trait 与辅助函数」）的主题，本讲先建立「它们住在 `lib.rs`」的印象即可。

#### 4.4.4 代码实践

**实践目标**：把脑中的「公开 API 全景表」与源码逐行对上。

**操作步骤**：

1. 打开 [lib.rs:19-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L19-L28)。
2. 逐行读每一条 `pub use`，对照上面的全景表，确认「公开名字 ← 来自模块」。
3. 找到 `#[path = "version.rs"] mod version_;`（第 16-17 行），理解为什么模块名带下划线。

**需要观察的现象**：每一条 `pub use` 的形式都是 `pub use self::<模块>::{<公开名字>};`，其中 `self::` 指代本 crate 根。

**预期结果**：你能合上书，说出 `Scalar` 来自 `scalar` 模块、`PicoStr` 来自 `pico` 模块、`version` 函数来自 `version_` 模块。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `mod scalar;` 是私有的，但外部仍能用 `typst_utils::Scalar`？

> **参考答案**：因为 [lib.rs:27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L27) 有一行 `pub use self::scalar::Scalar;`，把 `Scalar` 重导出到了 crate 根。外部访问的是 crate 根上的 `Scalar`，而不是 `typst_utils::scalar::Scalar`（后者因模块私有而不可访问）。

**练习 2**：如果未来想把 `Scalar` 的内部实现拆到 `scalar/` 子目录里，需要改动对外 API 吗？

> **参考答案**：不需要。只要保持 `pub use self::scalar::Scalar;` 这一行不变，无论 `scalar` 模块内部是单文件还是子目录，对外暴露的 `typst_utils::Scalar` 都不变。这正是「私有 `mod` + 选择性 `pub use`」模式的核心收益——内部结构可自由重构，对外稳定。

## 5. 综合实践

**任务**：新建一个临时 Rust 项目，添加 `typst-utils` 依赖，验证「依赖可用 + 构建脚本注入的版本信息能读到 + `Scalar` 能正常构造打印」。这个任务把本讲的四个最小模块（定位、Cargo.toml、build.rs、lib.rs 导出）串起来。

**操作步骤**：

1. 在任意目录新建项目：

   ```sh
   cargo new try-typst-utils
   cd try-typst-utils
   ```

2. 在 `Cargo.toml` 的 `[dependencies]` 下添加依赖（`typst-utils` 已发布到 crates.io）：

   ```toml
   [dependencies]
   typst-utils = "0.15.1"
   ```

3. 在 `src/main.rs` 写：

   ```rust
   use typst_utils::{Scalar, display_commit, version};

   fn main() {
       // 读 build.rs 注入的版本信息
       let v = version();
       println!("version: {}.{}.{}", v.major(), v.minor(), v.patch());
       println!("raw: {}", v.raw());
       println!("commit: {}", display_commit(v.commit()));

       // 构造并打印一个标量
       let s = Scalar::new(1.0);
       println!("scalar: {s}");      // Scalar 实现了 Display
       println!("scalar get: {}", s.get()); // 取出内部 f64
   }
   ```

   > 示例代码（非 typst 原有代码）。`Scalar` 实现了 `Display`（见 [scalar.rs:87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L87)），所以可以直接用 `{s}` 打印。

4. 运行 `cargo run`。

**需要观察的现象**：

- `version` 行打印出 typst-utils 当前的版本号。
- `commit` 行：如果是从 git 仓库内构建会显示一串 hash 前 8 位；从 crates.io 安装则显示 `unknown commit`。
- `scalar` 行打印 `1`（或 `1.0`）。

**预期结果**：

- 若依赖来自 crates.io（默认）：`raw` 多半为 `0.15.1`，`commit` 为 `unknown commit`。
- 你可以再做一个对照实验：`TYPST_VERSION=9.9.9 cargo run`，观察 `version` 变成 `9.9.9`，验证 4.3 节讲的「外部环境变量覆盖 build.rs 兜底」。

（具体打印值待本地验证，取决于依赖来源与构建环境。）

## 6. 本讲小结

- `typst-utils` 是 Typst 工作区里**最底层的工具基础库**：被几乎所有其它 typst crate 依赖，但自身不依赖任何 typst crate。
- 目录结构是**一文件一主题**，13 个 `.rs` 文件分布在 `src/` 下，本手册后续每篇基本聚焦其中一个。
- `Cargo.toml` 大量使用 `{ workspace = true }` 从工作区根继承版本与依赖，升级只需改一处。
- 10 个依赖各有明确用途，例如 `libm` 提供跨平台确定性浮点、`siphasher` 提供稳定哈希、`rayon` 提供后台并行。
- `build.rs` 通过 `cargo:rustc-env` 把 `TYPST_VERSION`/`TYPST_COMMIT_SHA` 注入编译期环境变量，**外部设置优先、build.rs 兜底**；`version.rs` 用 `env!`/`option_env!` 在编译期读回，并用 `singleton!` 保证只解析一次。
- `lib.rs` 用「私有 `mod` + 选择性 `pub use`」控制公开 API 表面；其中 `#[path = "version.rs"] mod version_;` 的下划线命名是为了避开和 `version` 函数重名。

## 7. 下一步学习建议

- **下一篇讲义（u1-l2）**：深入 `lib.rs` 里内联定义的那一批**扩展 trait 与辅助函数**（`SliceExt`、`OptionExt`、`Static`、`debug`/`display`、`Numeric` 等），学会「用 trait 给外部类型加方法」的惯用法。
- **再下一篇（u1-l3）**：学习 `macros.rs` 里的声明式宏（`singleton!`、`assign_impl!` 等），你已经在本讲见过 `singleton!` 的用法，下一讲会讲它的「同伴」。
- **建议同步阅读**：在工作区根 `Cargo.toml` 浏览 `[workspace.dependencies]` 全貌，建立「整个 Typst 用了哪些基础库」的印象。
