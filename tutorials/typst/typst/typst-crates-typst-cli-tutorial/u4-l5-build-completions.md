# 构建期产物与 completions 命令

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `typst` 的命令行定义（`src/args.rs`）是如何被**运行时二进制**和**构建脚本（build script）**同时复用的，以及这条「共享」约束背后的依赖限制。
- 读懂 `build.rs`：它如何通过 `GEN_ARTIFACTS` 环境变量这一开关，在 `cargo build` 时生成 man 手册页（`typst.1` 等）和多 shell 补全脚本。
- 理解 `build.rs` 与 Cargo 之间的两条契约：`cargo:rustc-env=TARGET=...`（把目标三元组注入为编译期常量）与 `cargo:rerun-if-env-changed=GEN_ARTIFACTS`（精准的增量重跑触发）。
- 读懂运行时 `typst completions <shell>` 子命令，并理解它与构建期生成这两条路径的**同与不同**。

本讲是专家层（u4）的一篇，承接 u1-l3「命令行参数模型」对 `args.rs` 的讲解——本讲不再解释 clap 派生宏本身，而是聚焦「同一份参数定义如何服务于两个截然不同的消费方」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：什么是 build script（构建脚本）。**
Rust 的 Cargo 允许在 crate 根目录放一个 `build.rs`（也叫「build script」）。它在**编译这个 crate 之前**被编译并执行，通常用来：探测系统环境、生成代码、向 Cargo 打印指令（以 `cargo:` 开头的特殊 `println!`）。typst-cli 的 `build.rs` 属于第三类用途：生成 man 页和补全脚本，并通过 `cargo:` 指令把环境信息传达给后续编译。

**直觉二：什么是 man 手册页和 shell 补全。**
- **man 手册页**是 Unix 系统的命令手册，用 `man typst` 查看，文件名约定为 `typst.1`（`.1` 表示「用户命令」这一章节）。`typst` 还会为每个子命令生成独立的手册，如 `typst-compile.1`，可用 `man typst-compile` 或 `man 1 typst-compile` 查看。
- **shell 补全脚本**让你在 bash/zsh/fish/PowerShell/elvish 里输入 `typst co` 后按 Tab，自动补全成 `typst compile`，并提示该子命令的选项。不同 shell 需要不同格式的脚本。

这两类产物本质上都是从「命令行参数定义」**派生**出来的：知道有哪些子命令、有哪些选项，就能机械地生成手册和补全。所以 Typst 让它们由同一份 `args.rs` 自动生成，而不是手写维护。

**直觉三：什么是「编译期常量注入」。**
`build.rs` 里 `println!("cargo:rustc-env=TARGET=...")` 的作用是：把一个环境变量的值，在编译期「焊」进二进制。之后运行时代码可以用宏 `env!("TARGET")` 读取这个值——它会被替换成一个字符串字面量，不依赖运行时环境。本讲的 `build.rs` 正是用这种方式把构建目标平台告诉了二进制。

> 术语对照：本讲会频繁出现「运行时（runtime）」与「构建期（build time）」两个词，分别指「最终用户执行 `typst` 命令时」和「开发者执行 `cargo build` 编译 typst 时」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `src/args.rs` | 用 clap 派生宏定义全部子命令与参数 | **共享根基**：被运行时和构建脚本同时导入，是两条产物路径的共同数据源 |
| `build.rs` | 构建脚本，生成 man 页与补全脚本 | **构建期产物生成器**：本讲的主战场之一 |
| `src/completions.rs` | 运行时 `completions` 子命令的实现 | **运行时补全生成器**：13 行的薄壳 |
| `src/main.rs` | 入口与命令分发 | 把 `Completions` 子命令接到 `completions::completions` |
| `Cargo.toml` | 依赖与 feature 声明 | 解释「为什么 args.rs 的导入受到严格限制」的关键证据 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「被依赖者在先」的顺序展开：先讲共享根基（4.1），再讲构建期如何消费它生成产物（4.2），接着讲 build.rs 与 Cargo 的环境变量契约（4.3），最后讲运行时如何消费它（4.4）。

### 4.1 共享根基：args.rs 与「双重导入」约束

#### 4.1.1 概念说明

`src/args.rs` 里定义了 `CliArguments`、`Command` 枚举、各子命令结构体等（详见 u1-l3）。本模块关心的是它的一个特殊身份：**它同时被两个独立的编译单元导入**。

- **运行时侧**：主二进制通过 `src/main.rs` 里的 `mod args;`（常规模块声明）把 `src/args.rs` 纳入编译。
- **构建脚本侧**：`build.rs` 通过 `#[path = "src/args.rs"] mod args;` 把**同一个文件**再编译进 build script。

`#[path = ...]` 属性显式指定模块的源文件路径，使 build script 能「跨目录」引用 `src/` 下的文件。物理上是一份文件，逻辑上被编译了两次——一次编进最终的 `typst` 二进制，一次编进 `build.rs` 这个一次性的辅助程序。这就是「同一份参数定义服务于两个消费方」的实现机制。

这条双重导入带来一个**硬性约束**：`args.rs` 里只能 `use` 那些**既是运行时依赖、又是构建依赖**的 crate。原因很简单——build script 是一个独立程序，它编译时只能看到 `[build-dependencies]`；而运行时只能看到 `[dependencies]`。`args.rs` 既然两头都用，它引用的每一个外部 crate 都必须同时出现在两个依赖列表里，否则其中一侧会报「找不到 crate」。

#### 4.1.2 核心流程

用一个对照表说明约束如何落地：

```
                 args.rs 引用的 crate        是否在 [dependencies]？   是否在 [build-dependencies]？   结论
                 ----------------------      ----------------------    ----------------------------    ----
                 clap                        ✔                        ✔                              可用
                 clap_complete               ✔                        ✔                              可用
                 semver                      ✔                        ✔                              可用
                 serde                       ✔                        ✔                              可用
                 typst-utils                 ✔                        ✔                              可用
                 clap_mangen                 ✘（只在 build-deps）       ✔                              ✘ args.rs 不能用
                 typst / typst-eval / ...    ✔                        ✘                              ✘ args.rs 不能用
```

可以看到，`args.rs` 顶部的导入恰好落在「两边都有」的交集里。而 `clap_mangen`（man 页生成库）只在 `[build-dependencies]` 中——所以它由 `build.rs` 自己直接 `use`，绝不能出现在 `args.rs` 里。运行时核心库（`typst` 等）则反向地只在 `[dependencies]` 中，同样不能进 `args.rs`。

#### 4.1.3 源码精读

args.rs 顶部开宗明义地写明了这条约束（文件第 1–4 行的注释）：

[文件路径:L1-L4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L1-L4) — 注释明确指出：本模块既被 typst-cli 本体导入，也被它的 build script 导入；只能引用「同时是运行时依赖和构建依赖」的 crate，否则会得到一条令人困惑的「crate 缺失」错误。

紧随其后的导入全部落在「交集」里（注意 `clap_mangen` 不在其中）：

[文件路径:L13-L19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L13-L19) — `clap`、`clap_complete::Shell`、`semver::Version`、`serde::Serialize`、`typst_utils::display_possible_values` 都来自「双重依赖」的 crate。

证据在 Cargo.toml。先看运行时依赖（节选）：

[文件路径:L33-L34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L33-L34) — `clap` 与 `clap_complete` 同时是运行时依赖。

再看构建依赖：

[文件路径:L78-L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L78-L86) — `clap`、`clap_complete` 再次出现（与运行时构成交集），而 `clap_mangen` **只**出现在这里（第 82 行），印证了它只能由 build script 使用、不能进入 args.rs。

补全命令本身的参数定义也非常短，这是本模块要交接给 4.2/4.4 的数据点：

[文件路径:L267-L273](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L267-L273) — `CompletionsCommand` 只有一个字段 `shell: Shell`，`Shell` 正是 `clap_complete::Shell` 枚举（bash/zsh/fish/PowerShell/elvish），用 `#[arg(value_enum)]` 让用户在命令行传 `typst completions bash`。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手验证「双重导入」约束。

1. **实践目标**：确认 `args.rs` 的每个外部 import 都同时存在于 `[dependencies]` 和 `[build-dependencies]`。
2. **操作步骤**：
   - 打开 `src/args.rs`，列出顶部所有 `use` 的外部 crate（如 `clap`、`clap_complete`、`semver`、`serde`、`typst_utils`，以及宏 `color_print::cstr!`）。
   - 打开 `Cargo.toml`，分别在 `[dependencies]`（约第 20–56 行）和 `[build-dependencies]`（第 78–86 行）两段里逐个核对这些 crate 是否都出现。
   - 单独留意 `clap_mangen`：确认它只出现在 `[build-dependencies]`，而不在 `args.rs` 的 import 里（它在 `build.rs` 里被 import）。
3. **需要观察的现象**：`args.rs` 的所有外部依赖都能在两个列表里同时找到；`clap_mangen` 是唯一的「只属于 build script」的例外。
4. **预期结果**：与 4.1.2 的对照表一致。若你在本地用某个「只在运行时」的 crate（比如尝试在 `args.rs` 里 `use typst::World;`），`cargo build` 时 build script 阶段会立刻报「找不到 `typst` crate」——这条「令人困惑的错误」正是第 1–4 行注释警告的内容。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clap_mangen` 不能放进 `args.rs`，而必须由 `build.rs` 自己 `use`？

> **参考答案**：因为 `clap_mangen` 只声明在 `[build-dependencies]`，不在运行时 `[dependencies]` 中。`args.rs` 被运行时二进制导入，运行时编译阶段看不到 `clap_mangen`，会报 crate 缺失。而 `build.rs` 只在构建期运行，能看到 `[build-dependencies]`，所以可以安全使用。

**练习 2**：如果新增一个命令行选项需要用到某个「只在运行时」的类型（例如 `typst::diag::SourceResult`），它应该放在 `args.rs` 还是别处？为什么？

> **参考答案**：不能放进 `args.rs`，否则 build script 编译 `args.rs` 时会失败。应把对该运行时类型的依赖放在「只被运行时使用」的模块里（如 `src/compile.rs`），让 `args.rs` 仅保留「两边都能编译」的纯参数定义。

---

### 4.2 构建期产物生成：build.rs 与 GEN_ARTIFACTS 开关

#### 4.2.1 概念说明

`build.rs` 的核心职责是**可选地**生成 man 手册页和 shell 补全脚本。注意「可选」二字——它不是每次 `cargo build` 都生成，而是受环境变量 `GEN_ARTIFACTS` 控制：只有当打包者（例如 Linux 发行版维护者、Homebrew formula 作者）设置了 `GEN_ARTIFACTS=<目录>` 时，才把产物写到该目录。普通开发者构建时这个变量为空，`build.rs` 几乎是个 no-op。

这是一个面向下游打包者的设计：发行版需要把 `typst.1` 装到 `/usr/share/man/man1/`、把补全脚本装到 `/usr/share/bash-completion/completions/`，于是 typst 提供这个开关让他们在打包流程里一次性拿到这些产物。仓库自身的 `docs/content/changelog/0.2.0.typ` 也记录了这一特性：「Shell completions and man pages can now be generated by setting the `GEN_ARTIFACTS` environment variable to a target directory and then building Typst」。

生成两类产物用到两个不同的库：
- **man 页**用 `clap_mangen`：把 clap 的 `Command` 结构渲染成 roff 格式的 `.1` 文件。
- **补全脚本**用 `clap_complete`：遍历 `Shell::value_variants()`，为每种 shell 调用 `generate_to`。

两者都以 `args::CliArguments::command()`（clap 的 `CommandFactory` trait 提供的方法，把派生宏定义转成运行时的 `Command` 树）作为统一的数据源。

#### 4.2.2 核心流程

`build.rs` 的 `main()` 可以画成下面这条流水线：

```
main()
  │
  ├─ 1. println!("cargo:rustc-env=TARGET=…")         # 注入编译期常量（见 4.3）
  ├─ 2. println!("cargo:rerun-if-env-changed=GEN_ARTIFACTS")  # 增量触发声明（见 4.3）
  │
  └─ 3. if 存在 GEN_ARTIFACTS 环境变量（指向目录 out）:
         │
         ├─ create_dir_all(out)
         ├─ cmd = CliArguments::command()             # 复用 args.rs，构造 clap Command 树
         │
         ├─ 【man 页 - 主命令】
         │     Man::new(cmd.clone()).render() → out/typst.1
         │
         ├─ 【man 页 - 各子命令】for subcmd in cmd.get_subcommands():
         │     name = "typst-" + subcmd 名称
         │     Man::new(subcmd).render() → out/typst-<子命令>.1
         │
         └─ 【补全脚本】for shell in Shell::value_variants():
               generate_to(shell, cmd, "typst", out)
               → out/ 下写出对应 shell 的补全文件
```

关键点：步骤 1、2 **无条件**执行（无论是否生成产物都需要），步骤 3 整体被 `GEN_ARTIFACTS` 门控。

#### 4.2.3 源码精读

首先是「双重导入」在 build script 侧的落地——通过 `#[path]` 引入同一份 `args.rs`，并用 `#[expect(dead_code)]` 抑制未使用告警：

[文件路径:L9-L11](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/build.rs#L9-L11) — `#[path = "src/args.rs"]` 让 build script 编译同一份参数定义；`#[expect(dead_code)]` 是因为 build script 只用到 `CliArguments`，而 `args.rs` 里还有大量 build script 用不到的项（如 `CompileArgs`、`WorldArgs`），不加这个属性会触发一堆 dead_code 警告。

接着是 GEN_ARTIFACTS 门控与产物生成的主体：

[文件路径:L18-L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/build.rs#L18-L37) — `env::var_os("GEN_ARTIFACTS")` 用 `var_os`（而非 `var`）以正确处理非 UTF-8 路径；拿到目录后 `create_dir_all` 确保它存在，再用 `args::CliArguments::command()` 构造 clap 的 `Command` 树（第 21 行），后续 man 页与补全都基于它。

man 页主命令的生成（第 23–25 行）：

[文件路径:L23-L25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/build.rs#L23-L25) — `Man::new(cmd.clone())` 把整棵 `Command` 包成 man 页对象，`render` 直接写进 `out/typst.1`。注意 `cmd.clone()`——因为同一个 `cmd` 稍后还要用于遍历子命令和生成补全，这里克隆避免后续的可变借用冲突。

各子命令 man 页的生成（第 27–32 行）：

[文件路径:L27-L32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/build.rs#L27-L32) — 遍历 `cmd.get_subcommands()`，为每个子命令拼出 `typst-<name>` 的名字（如 `typst-compile`），再单独渲染成 `typst-compile.1`。这解释了为什么 `man typst-compile` 能用。

> 小知识：workspace 的 `Cargo.toml` 里 `clap_mangen` 声明了 `features = ["env"]`（见 [workspace Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L48)）。这个 feature 让生成的 man 页**包含环境变量信息**——例如 `--cert` 旁注的 `TYPST_CERT`、`--package-path` 旁注的 `TYPST_PACKAGE_PATH` 都会被写进手册，方便用户在 man 里查阅。

补全脚本的生成（第 34–36 行）：

[文件路径:L34-L36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/build.rs#L34-L36) — `Shell::value_variants()` 返回 `clap_complete::Shell` 的全部变体（bash、elvish、fish、powershell、zsh），`generate_to` 为每种 shell 各写一个文件到 `out`。第三个参数 `"typst"` 是补全脚本里的二进制名（补全脚本据此知道它在为哪个命令做补全）。

#### 4.2.4 代码实践

这是一个**可在本地构建验证的实践**。

1. **实践目标**：用 `GEN_ARTIFACTS` 触发产物生成，亲眼看到 man 页和各 shell 补全脚本。
2. **操作步骤**：
   - 在 `crates/typst-cli` 目录下（或仓库根目录）执行：
     ```bash
     GEN_ARTIFACTS=./gen-out cargo build
     ```
   - 构建完成后查看 `./gen-out` 目录内容。
3. **需要观察的现象**：`gen-out` 下应出现：
   - 一个主 man 页 `typst.1`，以及 9 个子命令 man 页 `typst-compile.1`、`typst-watch.1`、`typst-init.1`、`typst-query.1`、`typst-eval.1`、`typst-fonts.1`、`typst-update.1`、`typst-completions.1`、`typst-info.1`。
   - 各 shell 的补全脚本：bash 生成 `typst.bash`，zsh 与 elvish 生成 `_typst`，fish 生成 `_typst.fish`，PowerShell 生成 `_typst.ps1`。
4. **预期结果**：用 `man ./gen-out/typst.1` 能正常阅读手册（若系统装了 `man`）；用 `cat ./gen-out/typst.bash` 能看到 bash 补全函数。文件名与 4.2.3 描述一致。若未设置 `GEN_ARTIFACTS` 直接 `cargo build`，则 `gen-out` 不会出现任何文件，印证了门控逻辑。
5. **注意**：子命令 man 页数量取决于当前编译启用的 feature。例如未启用 `self-update` 时 `update` 子命令虽仍存在（被 `hide`），`get_subcommands()` 仍会返回它，故 `typst-update.1` 仍会生成——具体以本地实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 21 行用 `args::CliArguments::command()`，而不是在 build.rs 里重新手写一份子命令列表？

> **参考答案**：因为命令行定义是产物内容的唯一真相源。复用 `args.rs` 保证 man 页、补全脚本与实际 `typst` 接受的命令**永远一致**——在 `args.rs` 加一个新选项，下次构建生成的手册和补全会自动包含它，无需同步维护两份定义。

**练习 2**：第 24 行 `.render()` 后面跟了 `.unwrap()`。如果生成 man 页时发生错误（例如目标目录不可写），build script 会怎样？

> **参考答案**：`unwrap()` 会让 `main()` panic，Cargo 会把 build script 的非零退出视为构建失败，整个 `cargo build` 报错中止。这与本模块所有 `unwrap` 的处理方式一致（第 20、24、31、35 行）——build script 对这类「不该发生」的环境错误采用直接 panic 的简单策略。

---

### 4.3 build.rs 与 Cargo 的环境变量契约

#### 4.3.1 概念说明

`build.rs` 除了生成文件，还通过两条 `println!("cargo:...")` 指令与 Cargo 立下契约。这两条指令虽短，却各自承担关键职责，且**无论是否生成产物都会执行**。

1. **`cargo:rustc-env=TARGET=...`**：把构建目标三元组（如 `x86_64-unknown-linux-gnu`）注入为一个编译期环境变量。之后运行时代码用 `env!("TARGET")` 读取它，会被替换成字面量字符串。
2. **`cargo:rerun-if-env-changed=GEN_ARTIFACTS`**：告诉 Cargo「只有当 `GEN_ARTIFACTS` 这个环境变量的值发生变化时，才需要重新运行 build script」。

这两条共同体现了 build script 的一个重要性质：**Cargo 默认不知道 build script 依赖什么，因此默认只在 build script 源码本身变化时才重跑它**。`rerun-if-env-changed` 是 build script 主动声明额外依赖的手段。

#### 4.3.2 核心流程

```
build script 契约的两条指令
  │
  ├─ rustc-env=TARGET=$TARGET
  │     ↳ 把「构建目标」焊进二进制
  │     ↳ 运行时 env!("TARGET") 直接读到（无需运行时环境变量）
  │     ↳ 被谁用？见 src/update.rs 的 determine_asset! 宏（u4-l4 自更新）
  │
  └─ rerun-if-env-changed=GEN_ARTIFACTS
        ↳ Cargo 据此决定是否重跑 build.rs
        ↳ 未设置 → 设置了 → 值变了：这三种情况都会触发重跑
        ↳ 否则即使改了别的文件，build.rs 也不重跑（保持产物稳定）
```

关于 `TARGET` 的来源：`env::var("TARGET")` 读的是 Cargo 在调用 build script 时**自动注入**的环境变量（Cargo 会给 build script 一组以 `CARGO_` 和构建参数开头的变量，`TARGET` 就是其中之一，表示 `--target` 的值或 host 三元组）。

#### 4.3.3 源码精读

这两条契约紧挨在 `main()` 开头（第 15–16 行）：

[文件路径:L15-L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/build.rs#L15-L16) — 第 15 行把 `TARGET` 注入为编译期常量，注释链接的 Stack Overflow 答案解释了这条技巧：通过 `cargo:rustc-env` 让运行时代码能用 `env!` 读到构建期的目标信息。第 16 行声明 `GEN_ARTIFACTS` 为重跑触发条件。

`TARGET` 注入的运行时消费方在自更新模块（u4-l4 详讲），这里只做定位佐证：

[文件路径:L35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L35) — `determine_asset!` 宏里 `match env!("TARGET")` 读取的就是 build.rs 第 15 行注入的值。它据此判断当前二进制对应哪个 GitHub release 资产名。这是本讲与 u4-l4「自更新机制」的一个交叉点：构建期信息经 build script 流向运行时。

#### 4.3.4 代码实践

这是一个**源码阅读 + 推理型实践**。

1. **实践目标**：验证 `TARGET` 注入与 `rerun-if-env-changed` 的实际效果。
2. **操作步骤**：
   - 先 `cargo build` 一次（成功）。
   - 再连续执行 `cargo build`（不改动任何文件），观察是否真的「nothing to do」、build script 不重跑。
   - 然后执行 `GEN_ARTIFACTS=./gen-out cargo build`（首次设置该变量），观察 build script 是否被重新执行、产物是否生成。
   - 用 `rustc -vV` 查看本机 host 三元组，与 `env!("TARGET")` 在 `update.rs` 中匹配的资产名做对照。
3. **需要观察的现象**：
   - 连续两次相同 `cargo build`：第二次 Cargo 报告「Finished」且不重跑 build script（因为 `GEN_ARTIFACTS` 没变、build.rs 源码没变）。
   - 首次给 `GEN_ARTIFACTS` 赋值后：build script 重跑，`gen-out` 出现产物。
4. **预期结果**：与 `rerun-if-env-changed=GEN_ARTIFACTS` 的语义一致。`TARGET` 的值与 `rustc -vV | grep host` 给出的三元组一致（在你未显式传 `--target` 的情况下）。
5. **若无法确认运行结果**：build script 是否重跑，可借助 `cargo build -vv`（very verbose）查看其输出是否再次出现，明确写「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉第 16 行的 `rerun-if-env-changed=GEN_ARTIFACTS`，会出什么问题？

> **参考答案**：Cargo 会退回到默认策略——只在 `build.rs`（及其 `rerun-if-changed` 声明的文件）变化时才重跑。结果是：第一次没设置 `GEN_ARTIFACTS` 构建、之后设置了再构建，Cargo 可能**不会重跑** build script，从而不生成产物。这条声明保证了「环境变量变化」能可靠触发重跑。

**练习 2**：第 15 行注入的 `TARGET`，为什么用 build script 注入，而不是让运行时代码用 `std::env::var("TARGET")` 直接读？

> **参考答案**：因为 `TARGET` 是**构建期**才知道的信息（取决于 `cargo build --target` 的参数），最终用户的运行环境里并没有这个变量。用 `cargo:rustc-env` 把它焊进二进制，运行时 `env!("TARGET")` 读到的是编译期字面量，与用户机器环境无关，自更新据此才能选对资产。

---

### 4.4 运行时 completions 命令

#### 4.4.1 概念说明

`typst completions <shell>` 是给**最终用户**用的子命令：它把补全脚本**打印到标准输出**，让用户自行重定向到自己的 shell 配置目录。这与 4.2 的构建期生成是「同源不同路径」——两者都调用 `clap_complete`，数据源都是 `CliArguments::command()`，区别在于：

| 维度 | 构建期（build.rs） | 运行时（completions 命令） |
| --- | --- | --- |
| 触发者 | 打包者，设 `GEN_ARTIFACTS` | 最终用户，跑 `typst completions bash` |
| 时机 | `cargo build` 时 | 任意时刻 |
| 产物 | man 页 + 全部 shell 补全，写到磁盘目录 | 单一 shell 补全，写到 stdout |
| 用到的库 | `clap_mangen` + `clap_complete::generate_to` | 仅 `clap_complete::generate` |
| 目的 | 给发行版打包 | 给个人用户安装补全 |

一个关键差异：构建期用 `generate_to`（**to** = 写到文件），运行时用 `generate`（写到任意 `Write` 目标，这里接 `stdout`）。

#### 4.4.2 核心流程

`completions.rs` 只有 13 行，流程极简：

```
completions(command)               # command: &CompletionsCommand，含一个 shell 字段
  │
  ├─ cmd = CliArguments::command()       # 再次复用 args.rs，构造 Command 树
  ├─ bin_name = cmd.get_name()           # 取出 "typst"（即 #[clap(name="typst")]）
  └─ generate(command.shell, &mut cmd, bin_name, &mut stdout())
                                         # 把 <shell> 的补全脚本写到 stdout
```

分发链路的上游在 `main.rs`：`Completions` 是少数几个**不带 `?`** 的子命令之一（与 `fonts` 一样），因为 `completions()` 返回 `()` 而非 `Result`——它不会失败，自然无需冒泡错误。

#### 4.4.3 源码精读

completions.rs 全文（仅 13 行）：

[文件路径:L9-L13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/completions.rs#L9-L13) — `CliArguments::command()`（来自 `clap::CommandFactory`，第 3 行导入）复用 args.rs 构造 `Command` 树；`cmd.get_name()` 取出二进制名（即 args.rs 第 52 行 `name = "typst"` 设定的值）；`generate` 把指定 shell 的补全写到 `stdout`。注意它 `use crate::args::{CliArguments, CompletionsCommand}`（第 6 行）——运行时侧通过正常的 `crate::args` 路径访问同一份定义，与 build.rs 的 `#[path]` 导入殊途同归。

分发的上游连接点：

[文件路径:L78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L78) — `dispatch()` 把 `Command::Completions(command)` 直接交给 `crate::completions::completions(command)`，注意这一行末尾**没有 `?`**（对比第 71–75、77、79 行那些带 `?` 的子命令）。`completions` 与 `fonts`（第 76 行）一样是无错误返回的「纯输出」命令。

#### 4.4.4 代码实践

这是一个**可直接运行的实践**（假设你已按 u1-l1 构建出 `typst` 二进制）。

1. **实践目标**：用运行时命令生成补全，并对比与构建期产物的差异。
2. **操作步骤**：
   ```bash
   ./target/debug/typst completions bash > my-typst.bash
   ./target/debug/typst completions zsh  > my-typst.zsh
   ./target/debug/typst completions fish > my-typst.fish
   ```
   再对照 4.2.4 里 `GEN_ARTIFACTS` 生成的 `typst.bash`：
   ```bash
   diff my-typst.bash gen-out/typst.bash
   ```
3. **需要观察的现象**：
   - `typst completions bash` 的输出直接打到 stdout（不写文件），需自行 `>` 重定向。
   - 与构建期生成的 `typst.bash` 内容**应当一致**（同库同数据源）。
   - 运行时命令只能一次输出**一种** shell，而构建期一次性产出全部 shell。
4. **预期结果**：`diff` 无输出（或仅极小差异）。运行时命令不生成 man 页（man 页只能由 build script 用 `clap_mangen` 生成，运行时二进制未依赖 `clap_mangen`）。
5. **安装到本机 bash 体验**（可选）：`source my-typst.bash` 后输入 `typst co<Tab>` 应补全为 `compile`——具体补全行为依赖你的 shell 配置，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `typst completions bash` 不能像构建期那样顺便生成 man 页？

> **参考答案**：因为 `clap_mangen` 只在 `[build-dependencies]` 里，运行时二进制没有这个依赖（见 4.1）。man 页是面向打包者的构建期产物，运行时命令只负责个人用户的补全，故只引入了 `clap_complete`（`generate`）。

**练习 2**：第 11 行特意 `cmd.get_name().to_string()` 取出 `bin_name` 再传给 `generate`。如果直接硬编码字符串 `"typst"` 会有什么隐患？

> **参考答案**：`get_name()` 取的是 args.rs 里 `#[clap(name = "typst")]` 的权威值，保持单一真相源。若硬编码 `"typst"`，将来万一改了 `name`，补全脚本里还会写旧名字，导致补全与实际命令名不一致。`to_string()` 则把 `&str` 转成拥有所有权的 `String`，以满足 `generate` 的参数类型要求。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「修改命令行定义 → 观察两类产物同步变化」的闭环。这是一个**源码阅读 + 本地构建**综合任务。

**任务背景**：假设你要给 `typst` 加一个新的顶层标志（例如 `--goodbye`），验证 man 页与补全会如何自动跟上。

**步骤**：

1. **修改单一真相源**。在 `src/args.rs` 的 `CliArguments` 结构体里（参考 [L64-L76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L64-L76)），仿照 `color` 字段加一个字段，例如：
   ```rust
   /// A demo flag for the tutorial.
   #[clap(long)]
   pub goodbye: bool,
   ```
   > 注意：这是仅在本地副本上的学习性修改，不提交、不改丢原始仓库状态；遵守「不修改源码交付」的原则，练习后请用 `git checkout -- src/args.rs` 还原。

2. **构建期验证**。执行：
   ```bash
   GEN_ARTIFACTS=./gen-out cargo build
   grep -i goodbye gen-out/typst.1          # man 页里应出现新标志
   grep -i goodbye gen-out/typst.bash       # bash 补全里应出现 --goodbye
   ```

3. **运行时验证**。执行：
   ```bash
   ./target/debug/typst completions bash | grep -i goodbye
   ./target/debug/typst --help             # 帮助里应列出 --goodbye
   ```

4. **对照观察**：
   - 构建期的 `typst.1`、`typst.bash`、运行时的 `completions bash` 输出、`--help` 输出——四处都自动包含了新标志，你却**只改了一处**（`args.rs`）。这正是「单一真相源 + 复用 `CliArguments::command()`」的威力。
   - 还原修改：`git checkout -- src/args.rs`，再次构建确认产物恢复。

**需要观察的现象与预期结果**：四处输出都同步出现 `--goodbye`，证明 man 页、补全、帮助文本共享同一份 `args.rs` 定义；还原后再次构建，产物中不再出现该标志。

> 若本地不便修改源码，可退化为**纯阅读型综合实践**：沿着 `args.rs::CliArguments` → `build.rs::args::CliArguments::command()` → `Man::render` / `generate_to`，以及 `args.rs::CliArguments` → `completions.rs::CliArguments::command()` → `generate` 这两条链路，画出一张「一份定义、两个消费方、四类产物」的数据流图。

## 6. 本讲小结

- `src/args.rs` 是被**运行时二进制**和 **build script** 双重导入的共享根基：`build.rs` 用 `#[path = "src/args.rs"] mod args;` 引入同一份文件，物理一份、编译两次。
- 这条双重导入带来硬约束：`args.rs` 只能 `use` 那些**同时**在 `[dependencies]` 和 `[build-dependencies]` 里的 crate；`clap_mangen`（仅构建依赖）和 `typst`（仅运行时依赖）都不能进 `args.rs`，证据就在 `Cargo.toml` 的两段依赖列表里。
- `build.rs` 受 `GEN_ARTIFACTS` 环境变量门控：设置后才用 `clap_mangen` 生成主命令及各子命令的 man 页（`typst.1`、`typst-compile.1` 等）、用 `clap_complete::generate_to` 生成全部 shell 补全脚本；这是面向发行版打包者的可选开关。
- `build.rs` 还与 Cargo 立两条契约：`cargo:rustc-env=TARGET=...` 把目标三元组焊进二进制（供 `update.rs` 的 `env!("TARGET")` 读取），`cargo:rerun-if-env-changed=GEN_ARTIFACTS` 保证环境变量变化时重跑 build script。
- 运行时 `typst completions <shell>` 是同一份数据源的「另一条路径」：用 `clap_complete::generate` 把单一 shell 的补全打到 stdout，面向个人用户；它与构建期产物同源但不生成 man 页。
- 两类产物之所以能保持与实际命令行一致，根本原因是它们都复用 `CliArguments::command()`——改一处 `args.rs`，man 页、补全、`--help` 自动同步。

## 7. 下一步学习建议

- **横向对照 u4-l4（自更新机制）**：本讲提到 `build.rs` 注入的 `TARGET` 被 `src/update.rs` 的 `determine_asset!` 宏消费。建议阅读 [update.rs:L23-L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L23-L37)，看构建期常量如何决定运行时下载哪个 release 资产，把「build script → 编译期常量 → 运行时」这条链路走完整。
- **深入 `args.rs`**：若尚未通读，回到 u1-l3 把 `CliArguments`、`Command` 枚举与各参数组看完；本讲的「双重导入」约束正是建立在 u1-l3 的派生宏模型之上。
- **扩展实践**：尝试在本地克隆里把生成的 `typst.1` 用 `man` 实际查看（`man -l gen-out/typst.1`），并把 `typst.bash` source 进当前 shell 体验 Tab 补全，直观感受这些「派生产物」的用户价值。
- **后续讲义**：u4-l6（info 命令与环境内省）会展示 typst 如何把构建特性、版本等信息对外汇报，与本讲的「构建期信息流向外部」主题互为补充，可作为下一篇阅读。
