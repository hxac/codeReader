# 工具链与 cargo-oxide 驱动

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 cuda-oxide **为什么必须用一个被钉死的 nightly Rust 工具链**，以及它需要哪些外部依赖（CUDA Toolkit、`llc`、clang/libclang）。
- 说出 `cargo oxide` 这个「自定义 cargo 子命令」是如何被路由到 `cargo-oxide` 二进制的，并能列举它的主要子命令（`run` / `build` / `pipeline` / `doctor` / `new` / `setup` 等）各自做什么。
- 复述 `cargo oxide doctor` 的 9 项检查，并知道哪些是「致命检查」、哪些只是「信息性提示」。
- 画出 codegen 后端（`librustc_codegen_cuda.so`）的 **5 步发现链**（discovery chain），并理解三条缓存失效信号之间的优先级。
- 列举最重要的 `CUDA_OXIDE_*` 环境变量（`CUDA_OXIDE_BACKEND`、`CUDA_OXIDE_TARGET`、`CUDA_OXIDE_DEVICE_ARCH`、`CUDA_OXIDE_LLC` 等）的作用与优先级。

本讲承接 [u1-l1](u1-l1-project-overview.md)（项目定位）与 [u1-l2](u1-l2-workspace-and-crate-map.md)（crate 地图）：你已经知道 cuda-oxide 是一个自定义 rustc 后端，且最关键的 `rustc-codegen-cuda` 编外于 workspace。本讲回答的下一个问题是——**这套需要特殊编译的编译器，日常到底怎么被「驱动」起来？**

## 2. 前置知识

在开始前，先建立几个直觉。如果你已经熟悉 Rust / CUDA 的常规工具链，可以快速浏览本节。

- **nightly Rust 与 `rust-toolchain.toml`**：Rust 有 stable / beta / nightly 三个发布通道。`rust-toolchain.toml` 是一个放在仓库根目录的文件，rustup 看到它会自动为该目录安装并切换到指定的工具链。cuda-oxide 大量使用 `#![feature(...)]` 这类 nightly 才有的不稳定特性，所以**必须**钉死在一个具体的 nightly 版本上。
- **rustc codegen backend（代码生成后端）**：rustc 支持可插拔后端——你可以编译出一个 `librustc_codegen_xxx.so`，让 rustc 在生成机器码时改走你的逻辑。cuda-oxide 的后端就是这样一个 `.so`，被 rustc 在运行时 `dlopen` 加载。详见 [u1-l1](u1-l1-project-overview.md)。
- **cargo 子命令（custom cargo subcommand）**：cargo 约定，只要 `PATH` 里有一个名为 `cargo-xxx` 的可执行文件，`cargo xxx ...` 就会被转发给它。这就是 `cargo oxide` 能成立的原因。
- **cargo 别名（alias）**：`.cargo/config.toml` 里的 `[alias]` 表可以把一个短命令展开成一长串。cuda-oxide 用它让仓库内的 `cargo oxide` 直接指向 workspace 内的 `cargo-oxide` 包。
- **PTX / NVVM IR / llc**：PTX 是 NVIDIA 的并行线程汇编指令集；NVVM IR 是基于 LLVM 的、喂给 libNVVM 的中间表示；`llc` 是 LLVM 的静态编译器，cuda-oxide 用它把 LLVM IR 翻译成 PTX。这部分在 [u1-l1](u1-l1-project-overview.md) 的流水线鸟瞰里已经提过。
- **环境变量优先级**：当同一个设置（比如目标架构）既可以由命令行参数、又可以由环境变量、还可以由配置文件指定时，必须有一套明确的「谁覆盖谁」的规则，否则行为不可预测。本讲会反复遇到这种优先级问题。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [rust-toolchain.toml](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/rust-toolchain.toml) | 钉死的 nightly 工具链与组件清单。 |
| [.cargo/config.toml](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/.cargo/config.toml) | 定义 `cargo oxide` 别名与全局构建环境变量。 |
| [crates/cargo-oxide/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs) | CLI 定义（基于 clap）与子命令分发。 |
| [crates/cargo-oxide/README.md](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/README.md) | 子命令、标志、后端发现链的权威用户文档。 |
| [crates/cargo-oxide/src/backend.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs) | 后端发现与构建逻辑（5 步发现链、缓存失效）。 |
| [crates/cargo-oxide/src/commands.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs) | 所有子命令的实现，含 `doctor`、`setup`、上下文解析、rustflags 组装。 |

> 提示：本讲引用的行号都基于当前 HEAD `52e7078`。`cargo-oxide` 是 workspace 内一个**故意不依赖 CUDA Toolkit** 的纯 cargo 子命令（见其 `Cargo.toml` 的注释），所以它能在「裸机」上编译运行——这一点对 `doctor` 命令至关重要，后面会反复用到。

---

## 4. 核心概念与源码讲解

### 4.1 固定的 nightly 工具链与外部依赖

#### 4.1.1 概念说明

cuda-oxide 不是普通的 Rust 库，它是一个**编译器**。一个编译器对自己的构建环境比普通应用挑剔得多：

1. 它要调用 rustc 的内部 API（`#![feature(rustc_private)]`），这些 API 只在 nightly 上存在，且**每个 nightly 版本都可能改名或删除**。所以必须钉死一个具体日期的 nightly。
2. 它需要 rustc 的源码（`rust-src`）和开发组件（`rustc-dev`）才能链接内部 crate；还需要 `llvm-tools` 组件（其中带了带 NVPTX 后端的 `llc`）。
3. 它最终要把 LLVM IR 翻译成 PTX，因此需要 LLVM 21+ 的 `llc`。
4. 宿主侧的 `cuda-bindings` crate 用 `bindgen` 解析 CUDA 的 C 头文件 `cuda.h`，而 bindgen 运行时需要 libclang 及其资源目录下的 `stddef.h`。
5. 设备侧若调用 `sin/cos/exp` 这类数学函数，运行时还需要 libNVVM、nvJitLink 与 `libdevice.10.bc`。

这些依赖分布在三个层面：**Rust 工具链**（rustup 管）、**系统包**（apt / nix 管）、**CUDA Toolkit**（NVIDIA 管）。`rust-toolchain.toml` 只负责第一层，后两层要靠 `cargo oxide doctor` 来诊断。

#### 4.1.2 核心流程

依赖就绪后，一次 `cargo oxide run vecadd` 在「工具链」这一层经历的流程是：

```text
rustup 读取 rust-toolchain.toml
   └─ 自动安装/切换到 nightly-2026-04-03 + 5 个组件
cargo oxide（别名）被解析
   └─ 运行 cargo-oxide 二进制
      └─ cargo-oxide 组装 CARGO_ENCODED_RUSTFLAGS
         ├─ -Zcodegen-backend=<librustc_codegen_cuda.so 的绝对路径>
         ├─ -Zmir-enable-passes=-JumpThreading   ← 正确性硬约束
         └─ ...其它优化/符号修饰标志
      └─ 调用 cargo run --release
         └─ rustc 加载自定义后端 .so，走 cuda-oxide 流水线
```

关键点：工具链是被 `rust-toolchain.toml` 隐式钉死的，用户**不需要**手动 `rustup default`；但 `llc`、clang、CUDA Toolkit 这些「系统层」依赖 `rust-toolchain.toml` 管不到，必须另行安装。

#### 4.1.3 源码精读

**钉死的工具链**。整个文件只有三行，但每一行都很重要：

[rust-toolchain.toml:1-3](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/rust-toolchain.toml#L1-L3) —— `channel` 钉死具体日期（而非笼统的 `nightly`），`components` 列出五个必需组件：

```toml
[toolchain]
channel = "nightly-2026-04-03"
components = ["rust-src", "rustc-dev", "rust-analyzer", "clippy", "llvm-tools"]
```

- `rust-src` / `rustc-dev`：让 `rustc-codegen-cuda` 能 `extern crate` rustc 的内部 crate。
- `llvm-tools`：附带一个**带 NVPTX 后端**的 `llc`，这是 `doctor` 与流水线首选的 `llc`（后面 4.3 会看到）。
- `rust-analyzer` / `clippy`：开发体验组件。

**外部依赖的权威清单**写在 README：

[README.md:118-122](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/README.md#L118-L122) —— 列出 cargo-oxide、Rust nightly（含 `rust-src`/`rustc-dev`/`llvm-tools`）、CUDA Toolkit 12.x+、clang+libclang 开发头、Linux（Ubuntu 24.04 测过）。

**`llc` 的 21+ 版本约束**很关键，README 解释了原因：

[README.md:186-187](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/README.md#L186-L187) —— cuda-oxide 会发射 TMA / tcgen05 / WGMMA 等新 PTX intrinsic，LLVM 20 及更早的 `llc` 不认识这些签名，所以 Hopper / Blackwell 相关内核必须 LLVM 21+。

**正确性硬约束标志**。下面这段是 cuda-oxide 给 rustc 强制注入的「不可被覆盖」的标志，`-Zmir-enable-passes=-JumpThreading` 尤其要记住：

[crates/cargo-oxide/src/commands.rs:2079-2085](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L2079-L2085) —— 注入 `-Zcodegen-backend=<so>` 指向后端、`-Copt-level=3`、`-Cdebug-assertions=off`、`-Zmir-enable-passes=-JumpThreading`、`-Csymbol-mangling-version=v0`。

> 关于 `-Zmir-enable-passes=-JumpThreading`：JumpThreading 这条 MIR 优化会**复制同步屏障**，对 GPU SIMT 模型来说是致命的（会导致屏障数量不匹配、进而死锁）。这正是 [u1-l1](u1-l1-project-overview.md) 提到的「编译必须关掉 JumpThreading」的落地位置。注意它被放在标志列表的**最后**，而 rustc 对重复的 `-C/-Z` 选项是「后者覆盖前者」，所以这套正确性约束不会被用户继承下来的 `RUSTFLAGS` 推翻（详见 [crates/cargo-oxide/src/commands.rs:2061-2064](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L2061-L2064) 的注释）。

#### 4.1.4 代码实践

**实践目标**：确认本机的 nightly 工具链与组件，并理解 `rust-toolchain.toml` 的作用。

**操作步骤（源码阅读型 + 可选运行）**：

1. 打开 [rust-toolchain.toml](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/rust-toolchain.toml)，记下钉死的日期与五个组件。
2. 在仓库根目录运行 `rustc --version` 与 `rustup show`（若本机已装 rustup）。
3. 运行 `rustup component list --toolchain nightly-2026-04-03 | grep -E 'rust-src|rustc-dev|llvm-tools'`，确认这三个组件处于 `installed` 状态。

**需要观察的现象**：

- `rustc --version` 输出里应包含 `nightly` 与日期 `2026-04-03`。
- 在仓库目录外（比如 `/tmp`）运行 `rustc --version`，对比其版本是否不同——这能直观体现 `rust-toolchain.toml` 的「目录级覆盖」效果。

**预期结果**：仓库内自动切到 `nightly-2026-04-03`；若缺失组件，rustup 会提示安装。

**待本地验证**：上述命令的精确输出取决于本机是否已联网安装过该工具链；未安装时 `rustup show` 会显示 `(default)` 之外的工具链并自动触发下载。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `channel` 写成 `"nightly-2026-04-03"` 而不是 `"nightly"`？

> **参考答案**：笼统的 `"nightly"` 会随时间漂移到不同的日期，而 cuda-oxide 依赖的 rustc 内部 API 在不同 nightly 间可能不兼容；钉死具体日期保证「今天能编译，明天还能编译」。这与 `rustc-codegen-cuda` 必须手动同步 pliron 的 git rev 是同一类「版本钉死」哲学（见 [u1-l2](u1-l2-workspace-and-crate-map.md)）。

**练习 2**：`-Zmir-enable-passes=-JumpThreading` 这个标志的「减号」前缀是什么意思？为什么它必须放在 rustflags 列表的最后？

> **参考答案**：前置减号表示「禁用」该 pass（即关掉 JumpThreading）。放在最后是因为 rustc 对重复 `-C/-Z` 选项采用「last-one-wins」语义，放在最后能保证用户继承下来的同名设置无法重新打开这个会破坏 GPU 同步语义的优化。

---

### 4.2 cargo-oxide：一条命令驱动整条流水线

#### 4.2.1 概念说明

cuda-oxide 的构建链很长（Rust → MIR → dialect-mir → LLVM dialect → LLVM IR → PTX），还要先把 `rustc-codegen-cuda` 编成 `.so`。如果让用户手动拼这些步骤，门槛极高。`cargo-oxide` 就是把这些步骤打包成一个 cargo 子命令的「驱动器」：

- **对外**：你只需敲 `cargo oxide run vecadd`，它就负责找到/构建后端 `.so`、组装正确的 rustflags、调用 `cargo run`、把 PTX 跑到 GPU 上。
- **对内**：它同时服务仓库开发者（在 workspace 内跑示例）和外部用户（`cargo install` 后在任意项目里用）。

它取代了早期项目常见的 `xtask` 模式，改用「正规 cargo 子命令」，这样既能被 `cargo install` 分发，又能被仓库别名直接使用。

#### 4.2.2 核心流程

`cargo oxide` 命令的「路由」分两层：

```text
第一层：把 "cargo oxide ..." 路由到 cargo-oxide 二进制
  方式 A（仓库内 / cargo alias）：
     .cargo/config.toml 的 [alias] oxide = "run --package cargo-oxide --"
     → cargo 把 "cargo oxide run vecadd" 展开为
       "cargo run --package cargo-oxide -- run vecadd"
     → argv = [".../cargo-oxide", "run", "vecadd"]
  方式 B（cargo 原生子命令约定）：
     PATH 中存在 cargo-oxide 二进制
     → cargo 把 "cargo oxide run vecadd" 转发为
       argv = ["cargo-oxide", "oxide", "run", "vecadd"]   ← 注意多了一个 "oxide"

第二层：main.rs 识别两种 argv 形态，归一化后再用 clap 解析分发
```

正因为有两种调用形态，`main.rs` 的入口要做一次「剥掉多余的 `oxide`」的归一化处理。

#### 4.2.3 源码精读

**别名定义**：

[.cargo/config.toml:3-4](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/.cargo/config.toml#L3-L4) —— `oxide = "run --package cargo-oxide --"`。冒号后面的 `--` 把后续参数原样透传给 `cargo-oxide` 二进制。

**用户文档里的速查表**（子命令全景）：

[crates/cargo-oxide/README.md:23-39](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/README.md#L23-L39) —— 列出 `new` / `run` / `build` / `emit-ltoir` / `test` / `pipeline` / `debug` / `fmt` / `doctor` / `setup` 的用法。这是了解子命令用途最快的地方。

**CLI 定义（clap derive）**：

[crates/cargo-oxide/src/main.rs:36-46](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L36-L46) —— 顶层 `Cli` 结构体，`bin_name = "cargo oxide"`，内含一个 `command: Commands` 子命令枚举。

[crates/cargo-oxide/src/main.rs:49-206](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L49-L206) —— `Commands` 枚举定义了全部子命令及其参数。每个变体上的文档注释（`///`）会被 clap 转成 `--help` 文本。值得注意几个子命令：

- `Run`（[L52-L79](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L52-L79)）：构建后端 → 编译示例 → 运行。当未指定 `--arch` 也未设 `CUDA_OXIDE_TARGET` 时，它会**自动探测本机 GPU 0 的计算能力**作为目标架构（见 `arch` 字段的注释）。
- `Build`（[L80-L112](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L80-L112)）：只编译不运行；还支持「passthrough 模式」，把 `--` 之后的参数透传给底层 cargo。
- `Pipeline`（[L157-L170](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L157-L170)）：打开全链路 verbose，打印 MIR → dialect-mir → mem2reg → LLVM dialect → LLVM IR → PTX 的中间产物。
- `Doctor` / `Setup`（[L202-L206](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L202-L206)）：无参数的环境检查与显式构建后端。

**入口归一化**：

[crates/cargo-oxide/src/main.rs:230-237](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L230-L237) —— 检测 `argv[1]` 是否为 `"oxide"`（方式 B 的特征），若是则把它剥掉，归一化成方式 A 的形态，再交给 `Cli::parse_from`。这就是两种调用形态被统一处理的地方。

**分发**：

[crates/cargo-oxide/src/main.rs:242-405](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L242-L405) —— 一个大 `match cli.command`，把每个子命令路由到 `commands::` 模块里的具体实现。注意 `Run` 在分发时先调 `commands::resolve_context()`（会触发后端发现/构建），而 `Doctor` 调用的是 `commands::resolve_doctor_context()`（绝不触发构建，见 4.3）。

#### 4.2.4 代码实践

**实践目标**：通过阅读 +（可选）运行，把 `cargo oxide` 的路由链路走一遍。

**操作步骤**：

1. 阅读 [.cargo/config.toml](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/.cargo/config.toml)，确认别名展开形式。
2. 在仓库根目录运行 `cargo oxide --help`，对照 [main.rs:49-206](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L49-L206) 的枚举变体，看 `--help` 列出的子命令是否与之一一对应。
3. 运行 `cargo oxide run --help`，观察它列出的 `--arch` / `--features` / `--no-fmad` 等标志。

**需要观察的现象**：`--help` 顶部的 `Usage:` 行应显示 `cargo oxide <COMMAND>`；子命令列表应包含 `run`、`build`、`pipeline`、`doctor`、`setup`、`new`、`fmt` 等。

**预期结果**：帮助文本与源码中的 `///` 文档注释一致。

**待本地验证**：若本机尚未安装钉死的 nightly 工具链，第一次 `cargo oxide` 可能先触发 rustup 下载工具链；这是 `rust-toolchain.toml` 在生效，不是报错。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cargo oxide` 在仓库外（`cargo install` 安装后）也能用，而不需要在每个项目里都加别名？

> **参考答案**：cargo 有内置的子命令发现约定——`PATH` 里存在名为 `cargo-oxide` 的可执行文件时，`cargo oxide ...` 会被 cargo 自动转发给它（方式 B）。仓库内的别名（方式 A）只是为了在开发期直接用 workspace 里的源码版 `cargo-oxide`，省去 install 步骤。

**练习 2**：`main.rs` 为什么要先检测 `argv[1] == "oxide"` 再剥掉它？如果不做这步会发生什么？

> **参考答案**：方式 B（原生子命令）转发时，cargo 会在 argv 里保留一个 `"oxide"` 作为第二个参数（`["cargo-oxide", "oxide", "run", ...]`）。clap 直接解析会把它当成一个不认识的「位置参数/子命令」而报错。剥掉它后两种调用形态归一化，clap 才能正确识别 `run` 子命令。

---

### 4.3 doctor 与 setup：环境校验与后端构建

#### 4.3.1 概念说明

cuda-oxide 的依赖跨越 Rust / LLVM / CUDA 三大生态，出问题时报错信息往往很晦涩（比如 `'stddef.h' file not found`、`librustc_driver-<hash>.so: cannot open shared object file`）。`cargo oxide doctor` 的存在就是为了**在出错之前**把环境一次性体检清楚。它有两个设计原则：

1. **零副作用**：`doctor` 绝不在诊断前构建后端或联网克隆仓库——否则在「什么都没装」的裸机上它自己就先崩了，根本无法报告缺失项。
2. **区分致命与信息性**：只有真正阻碍「编译」的缺失才让 `doctor` 失败退出；只阻碍「在本地 GPU 上运行」（如没有显卡、没装 cuda-gdb）的缺失只给提示。

`setup` 则是 `doctor` 的「行动版」：它显式触发后端 `.so` 的构建。日常 `run`/`build` 也会按需自动构建后端，`setup` 主要用于拉取新代码后或 CI 里预热。

#### 4.3.2 核心流程

```text
cargo oxide doctor
  └─ resolve_doctor_context()        ← 只「定位」后端路径，绝不构建/克隆
     └─ backend_so_candidate()       ← 零副作用版的发现链
  └─ doctor(ctx) 依次执行 9 项检查：
     1. Rust nightly 工具链          （致命）
     2. rust-toolchain.toml 存在      （致命）
     3. 后端 .so 是否已构建           （信息性：run/build 会按需构建）
     4. CUDA 头文件 cuda.h            （致命，cuda-bindings 需要）
     5. CUDA Toolkit (nvcc)           （致命）
     5b. libNVVM / nvJitLink / libdevice  （致命，libdevice 数学函数需要）
     6. llc (LLVM 21+)                （致命，PTX 生成需要）
     7. clang / libclang resource dir （致命，bindgen 需要）
     8. NVIDIA 驱动 / GPU             （信息性：仅 run 需要）
     9. cuda-gdb                      （可选：仅 debug 需要）
     任一致命检查失败 → 退出码 1，并打印修复建议

cargo oxide setup
  └─ resolve_context()               ← 会触发后端构建（与 run 同款）
  └─ setup(ctx) → build_backend_from_source(codegen_crate)
```

#### 4.3.3 源码精读

**为什么 `doctor` 必须零副作用**——`resolve_doctor_context` 的文档把这一设计意图讲得很清楚：

[crates/cargo-oxide/src/commands.rs:101-108](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L101-L108) —— 它与 `resolve_context` 的发现逻辑完全相同，唯一区别是后端 `.so` 只用 `backend_so_candidate`「定位」、绝不构建或克隆。注释点明：诊断命令必须能在「什么都没装」的机器上跑起来。

[crates/cargo-oxide/src/commands.rs:109-148](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L109-L148) —— `resolve_doctor_context` 的实现，关键是第 114 / 132 行调用的是 `backend::backend_so_candidate` 而非 `find_or_build_backend`。

**`cargo-oxide` 自身刻意「不依赖 CUDA Toolkit」**——这是 `doctor` 能在裸机运行的根基：

[crates/cargo-oxide/Cargo.toml:12-18](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/Cargo.toml#L12-L18) —— 注释明确：`cargo-oxide` 的任何依赖都不得拉入 `cuda-bindings`（否则其 build 脚本需要 CUDA Toolkit，`doctor` 就没法在无 toolkit 机器上构建）。`libnvvm-sys` / `nvjitlink-sys` 是「纯 Rust + 运行时 dlopen」的 crate，所以允许。

**9 项检查的实现**——`doctor` 是一个按编号顺序排列的长函数，每项检查都遵循「先打印检查名，再 `✓/✗/-`，失败时额外打印修复建议」的模式：

[crates/cargo-oxide/src/commands.rs:1361-1682](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1361-L1682) —— `doctor` 全函数体。几个代表性检查：

- 第 1 项 Rust nightly（[L1368-L1385](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1368-L1385)）：跑 `rustc --version`，必须包含 `nightly`，否则 `ok = false`。
- 第 3 项后端 `.so`（[L1397-L1405](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1397-L1405)）：**信息性**——不存在只提示 `- not built yet (run cargo oxide setup)`，不置 `ok = false`。
- 第 6 项 `llc`（[L1501-L1589](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1501-L1589)）：按 `$CUDA_OXIDE_LLC` → rustup 的 `llvm-tools` 自带 `llc` → `llc-22` / `llc-21` / `llc` 的顺序探测，并解析主版本号，**< 21 判失败**。
- 第 7 项 clang（[L1600-L1630](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1600-L1630)）：跑 `clang -print-resource-dir`，还要确认该目录下 `include/stddef.h` 存在——这正是 README 警告的「光装 `libclang1-*` 运行时不够，必须有匹配的开发头」。
- 第 8 项驱动/GPU（[L1636-L1657](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1636-L1657)）：**信息性**，甚至会在 `/proc/driver/nvidia/version` 存在但 `nvidia-smi` 不工作时给出细分提示。

**`setup` 很薄**——它只是「解析上下文 + 调后端构建」：

[crates/cargo-oxide/src/commands.rs:1730-1740](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1730-L1740) —— `setup` 直接调 `backend::build_backend_from_source(&ctx.codegen_crate)`。注意 `main.rs` 里 `Setup` 分支调的是 `resolve_context()`（会触发发现/构建）而非 `resolve_doctor_context()`（见 [main.rs:400-403](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/main.rs#L400-L403)）。

#### 4.3.4 代码实践

**实践目标**：用 `doctor` 体检本机，并定位「最可能缺失」的依赖；再理解 `setup` 与 `run` 在后端构建上的关系。

**操作步骤（源码阅读型 + 可选运行）**：

1. 阅读 [commands.rs:1361-1682](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1361-L1682)，把 9 项检查抄成一张表，标注哪些失败会把退出码置 1。
2. 在仓库根目录运行 `cargo oxide doctor`（若环境允许）。
3. 如果第 6 项 `llc` 失败，按提示 `rustup component add llvm-tools` 后重跑 `doctor`；如果第 7 项 clang 失败，按 `sudo apt install clang-21`（或 `libclang-common-21-dev`）后重跑。

**需要观察的现象**：`doctor` 输出每行前缀为 `✓`（通过）、`✗`（失败）、`-`（信息性跳过）；末尾给出 `✅ Environment looks good!` 或 `❌ Some checks failed...`。

**预期结果**：在一台只装了 Rust 但没装 CUDA Toolkit / LLVM / clang 的机器上，第 4/5/6/7 项会 `✗`，而第 3、8、9 项只会给 `-` 提示——这正体现了「致命 vs 信息性」的区分。

**待本地验证**：本机实际缺什么取决于具体环境，请以 `doctor` 的真实输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 3 项「后端 `.so` 未构建」只是信息性提示，而不是失败？

> **参考答案**：因为 `run` / `build` / `pipeline` 都会在需要时**按需自动构建**后端（走 `resolve_context` → `find_or_build_backend`）。一个全新 clone、还没跑过任何命令的仓库本来就没有 `.so`，这是健康状态，不应让 `doctor` 报红。

**练习 2**：`resolve_doctor_context` 与 `resolve_context` 的唯一区别是什么？为什么必须有这个区别？

> **参考答案**：唯一区别是前者用 `backend_so_candidate`（只定位、不构建、不克隆），后者用 `find_or_build_backend`（会构建/克隆）。原因是 `doctor` 的职责是「在裸机上报告环境缺失」，如果它自己先去构建后端（要几分钟）或联网克隆（要网络），那么在缺这缺那的机器上它会在打印任何诊断前就崩掉，违背存在意义。

**练习 3**：`llc` 探测为什么会把「rustup 自带 `llvm-tools` 的 `llc`」排在 `llc-21` 之前？

> **参考答案**：因为 `llvm-tools` 组件的 `llc` 是随钉死的 Rust 工具链一起安装的、**确定带 NVPTX 后端**，版本也随工具链确定，最可控；而 `PATH` 上的 `llc-21` 来自系统包管理器，可能不带 NVPTX 后端或版本不匹配。把最可控的候选排在前面，能减少「装了但用不了」的尴尬。

---

### 4.4 后端发现链与 CUDA_OXIDE_* 环境变量

#### 4.4.1 概念说明

后端 `librustc_codegen_cuda.so` 既不是普通 crate 依赖（它编外于 workspace），也不是系统库。`cargo-oxide` 需要一套规则来「找到它，找不到就构建它」。这套规则就是**发现链（discovery chain）**——一个有明确优先级的 5 步查找序列。

更难的是**缓存失效**。后端 `.so` 是动态链接到某个具体的 `librustc_driver-<hash>.so` 上的，而 `cargo-oxide` 二进制本身、后端源码、Rust 工具链三者任一发生变化，都可能让缓存的 `.so` 失效。`backend.rs` 用一个 `CacheStatus` 枚举把「为什么过期」分类，并对不同原因采取不同恢复策略（重新克隆 vs 原地重建）。

此外，cuda-oxide 用一大批 `CUDA_OXIDE_*` 环境变量在「cargo-oxide 进程」与「rustc 后端进程」之间传递配置。理解它们的优先级，是排查「为什么我的 `--arch` 没生效」类问题的关键。

#### 4.4.2 核心流程

**后端发现链**（优先级从高到低）：

```text
find_or_build_backend(workspace_root, configured_backend):
 1. $CUDA_OXIDE_BACKEND        ← 显式路径覆盖（不存在则告警并继续）
 2. .cargo/cuda-oxide.toml     ← 项目配置的 backend 字段（不存在则硬退出）
 3. 本地仓库源码                ← 检测到 crates/rustc-codegen-cuda 就地构建
 4. ~/.cargo/cuda-oxide/ 缓存   ← 仅当未过期，按 CacheStatus 决定是否重建
 5. 自动 git clone + 构建       ← 最后兜底，仅外部用户首次使用触发
```

**缓存失效的三条信号**（优先级从高到低）：

```text
cached_backend_status(cached_so, source_dir) 返回:
  StaleVsToolchain  最高优先级：记录的工具链指纹(rustc -vV)与当前不一致
                    → 缓存 .so 链接的 librustc_driver 找不到了，必须重新克隆+重建
  StaleVsBinary     cargo-oxide 二进制比缓存新（用户刚 cargo install 升级）
                    → 丢弃缓存源码树并重新克隆
  StaleVsSource     缓存的后端源码比 .so 新（开发者改了源码）
                    → 原地用现有源码重建（不丢弃源码树）
  Fresh             一切匹配，直接复用
```

**目标架构（arch）的优先级**（`run` 命令）：

```text
1. --arch <sm_XX>          → CUDA_OXIDE_TARGET（硬覆盖）
2. $CUDA_OXIDE_TARGET      （环境变量硬覆盖）
3. 探测到的 GPU 算力         → CUDA_OXIDE_DEVICE_ARCH（建议性 hint）
4. 后端按 IR 形状选的默认值   （如 Basic→sm_80, Cluster→sm_90, Tma→sm_100）
```

#### 4.4.3 源码精读

**发现链的文档注释**（最权威的优先级说明）：

[crates/cargo-oxide/src/backend.rs:8-15](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L8-L15) —— 5 步发现链的浓缩版，与 [crates/cargo-oxide/README.md:215-221](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/README.md#L215-L221) 的用户文档对应。

**`find_or_build_backend` 实现**——5 个步骤在源码里就是 5 个连续的 `if` 块：

[crates/cargo-oxide/src/backend.rs:89-157](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L89-L157) —— 逐段对应：

- 步骤 1（[L90-L100](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L90-L100)）：读 `CUDA_OXIDE_BACKEND`，文件存在直接返回；不存在则告警并**继续**后续步骤（容错）。
- 步骤 2（[L102-L113](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L102-L113)）：项目配置的 backend；注意它与步骤 1 不同——**不存在则硬退出**（因为这是用户显式配置的，配置错了应该停下来报错）。
- 步骤 3（[L115-L121](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L115-L121)）：检测 `crates/rustc-codegen-cuda` 目录存在，就 `build_backend_from_source` 就地构建，返回 `target/debug/librustc_codegen_cuda.so`。
- 步骤 4（[L123-L153](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L123-L153)）：检查 `~/.cargo/cuda-oxide/` 缓存，调用 `cached_backend_status` 分类，按 `CacheStatus` 决定复用 / 失效 / 原地重建。
- 步骤 5（[L155-L156](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L155-L156)）：兜底 `auto_fetch_and_build`。

**零副作用的「定位版」**——供 `doctor` 使用：

[crates/cargo-oxide/src/backend.rs:174-191](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L174-L191) —— `backend_so_candidate` 镜像发现链的顺序，但**只返回路径**，不构建、不克隆、不联网。`CUDA_OXIDE_BACKEND` 和项目配置即使文件不存在也照样返回路径（让 `doctor` 能报告「配置了但找不到」）。

**`CacheStatus` 枚举**——把「为什么过期」显式建模：

[crates/cargo-oxide/src/backend.rs:196-211](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L196-L211) —— 四个变体。`backend.rs` 顶部的长注释（[L17-L61](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L17-L61)）详细解释了为什么需要区分这三种过期——核心是「工具链换了让 `.so` 不可加载」最严重、「二进制升级了要重新克隆」、「源码改了要原地重建」。

**工具链指纹检查**——这是优先级最高、也最隐蔽的失效信号：

[crates/cargo-oxide/src/backend.rs:284-292](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L284-L292) —— 把缓存时记录的 `rustc -vV` 指纹与当前工具链对比，不一致就判 `StaleVsToolchain`。这正是为了避免那种「换了 nightly 后 `librustc_driver-<hash>.so: cannot open shared object file`」的神秘报错。

**arch 优先级**——`configured_arch` 与 `has_configured_arch`：

[crates/cargo-oxide/src/commands.rs:2114-2123](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L2114-L2123) —— `--arch` 和 `$CUDA_OXIDE_TARGET` 同属「硬覆盖」槽位；二者都没有时才回落到项目配置的 `default-arch`。

[crates/cargo-oxide/src/commands.rs:2173-2181](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L2173-L2181) —— `apply_device_arch_hint` 只在用户**没有**显式指定 `--arch` 时，把探测到的 GPU 算力以 `CUDA_OXIDE_DEVICE_ARCH`（建议性 hint）传给后端。完整的 4 级优先级注释见 [L2183-L2206](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L2183-L2206)。

**常用 `CUDA_OXIDE_*` 变量速查**（均可在 `commands.rs` 中 grep 到其 `.env(...)` 调用点）：

| 变量 | 作用 | 典型来源 |
|------|------|----------|
| `CUDA_OXIDE_BACKEND` | 后端 `.so` 的显式路径覆盖 | 用户环境 |
| `CUDA_OXIDE_TARGET` | 目标架构**硬覆盖**（如 `sm_90`） | `--arch` 或环境 |
| `CUDA_OXIDE_DEVICE_ARCH` | 探测到的 GPU 算力**建议性 hint** | `run` 自动探测 |
| `CUDA_OXIDE_LLC` | 指定 `llc` 二进制路径 | 用户环境 |
| `CUDA_OXIDE_LIBDEVICE` | 指定 `libdevice.10.bc` 路径 | 用户环境 |
| `CUDA_OXIDE_NO_FMA` | 禁用 FMA 收缩（`--no-fmad`） | CLI 或环境 |
| `CUDA_OXIDE_EMIT_NVVM_IR` | 产出 NVVM IR 而非 PTX | `--emit-nvvm-ir` |
| `CUDA_OXIDE_VERBOSE` | 打开详细编译输出 | `-v` |
| `CUDA_OXIDE_DEVICE_CODEGEN_CRATE` | 设备 codegen 的 owner crate 过滤 | `--device-codegen-crate` |

> 注意 `CUDA_OXIDE_TARGET` 与 `CUDA_OXIDE_DEVICE_ARCH` 的关键区别：前者是**硬覆盖**（后端无条件遵循），后者是**建议**（后端只在 GPU 真能跑该内核时才采用，否则回落到内核所需架构）。这让 `run` 在「消费级 sm_120 显卡上跑需要 sm_100a 的 tcgen05 内核」时也能正确处理——后端会按内核所需架构编译，模块在加载时再跳过。

#### 4.4.4 代码实践

**实践目标**：跟踪一次后端发现的决策路径，并验证 `CUDA_OXIDE_BACKEND` 覆盖是否生效。

**操作步骤（源码阅读型 + 可选运行）**：

1. 阅读 [backend.rs:89-157](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L89-L157)，给 5 个步骤画一张流程图，标注「文件不存在时是告警继续还是硬退出」。
2. 阅读 [backend.rs:223-261](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L223-L261) 的 `cached_backend_status`，确认三条失效信号的判断顺序（工具链 → 二进制 → 源码）。
3. （可选运行）在仓库目录下设置一个不存在的路径 `CUDA_OXIDE_BACKEND=/tmp/nope.so cargo oxide doctor`，观察它如何告警并继续。

**需要观察的现象**：

- 步骤 1 缺失时打印 `Warning: CUDA_OXIDE_BACKEND=... does not exist, falling back to auto-detection`。
- `doctor` 仍能正常完成（因为它用零副作用的 `backend_so_candidate`，且该函数对 `CUDA_OXIDE_BACKEND` 直接返回路径而不检查存在性——见 [backend.rs:175-177](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/backend.rs#L175-L177)）。

**预期结果**：`doctor` 不会因为 `CUDA_OXIDE_BACKEND` 指向不存在的文件而崩溃，而是继续后续发现逻辑（在 `run`/`build` 路径下会告警并继续）。

**待本地验证**：实际告警措辞以本机 `cargo-oxide` 版本为准。

#### 4.4.5 小练习与答案

**练习 1**：发现链里，步骤 1（`CUDA_OXIDE_BACKEND`）和步骤 2（项目配置 backend）在「文件不存在」时的行为为何不同？

> **参考答案**：步骤 1 是宽松的环境变量覆盖，不存在就告警并继续后续自动发现，避免「设了但路径写错」彻底卡死用户；步骤 2 是项目 `.cargo/cuda-oxide.toml` 里**显式声明**的契约，配置错了应该让用户立刻知道并修复，所以硬退出。一个偏「方便」，一个偏「严谨」。

**练习 2**：`StaleVsToolchain` 为什么优先级最高，甚至高于「二进制比缓存新」？

> **参考答案**：因为后端 `.so` 动态链接到某个具体 `librustc_driver-<hash>.so`，工具链一换，那个 hash 就对不上了，`.so` **根本无法加载**（无论 mtime 怎么样）。一个加载不了的 `.so` 必须重新克隆+重建，所以工具链指纹检查优先于一切 mtime 检查。

**练习 3**：你在 sm_120（Blackwell 消费级）显卡上 `cargo oxide run` 一个需要 `sm_100a` 的 tcgen05 内核，既没传 `--arch` 也没设 `CUDA_OXIDE_TARGET`。后端最终会按什么架构编译？为什么不会崩？

> **参考答案**：`run` 会把探测到的 sm_120 以 `CUDA_OXIDE_DEVICE_ARCH` 作为**建议性 hint** 传给后端，而非硬覆盖。后端识别出该内核需要 sm_100a（消费级 sm_120 缺少该能力），于是按 sm_100a 编译；模块在本地加载时再被跳过。因为 hint 不是硬覆盖，所以不会出现「强行按 sm_120 编译却缺少 tcgen05 指令」的崩溃。

---

## 5. 综合实践

**任务：从零搭建一个最小 cuda-oxide 项目并体检环境。**

把本讲的四个最小模块串起来，完成一次端到端的环境搭建与验证。请在有条件的环境中按序操作；若某步无法运行，改为「源码阅读 + 写出预期现象」。

1. **工具链体检**：进入仓库目录运行 `rustup show`，确认自动切到 `nightly-2026-04-03`，并核对五个组件是否齐备（对应 [4.1](#41-固定的-nightly-工具链与外部依赖)）。
2. **doctor 全身体检**：运行 `cargo oxide doctor`，把 9 项检查结果抄成一张表，标注哪些 `✓`、哪些 `✗`、哪些 `-`（对应 [4.3](#43-doctor-与-setup环境校验与后端构建)）。针对每项 `✗` 给出修复命令。
3. **理解发现链**：在 `doctor` 输出里找到「Codegen backend」那一行，判断它走的是发现链的哪一步（本地仓库源码 / 缓存 / 尚未构建）。运行 `cargo oxide setup` 显式构建后端，再跑一次 `doctor` 看该行是否变成 `✓`（对应 [4.4](#44-后端发现链与-cuda_oxide_-环境变量)）。
4. **新建并运行最小项目**：在仓库**外**的某个目录运行 `cargo oxide new hello`（若已 `cargo install`）或在仓库内运行 `cargo oxide run vecadd`，体会 `cargo oxide` 如何一条命令驱动「构建后端 → 组装 rustflags → cargo run → GPU 执行」（对应 [4.2](#42-cargo-oxide一条命令驱动整条流水线)）。
5. **环境变量实验**：用 `CUDA_OXIDE_TARGET=sm_80 cargo oxide build vecadd` 强制指定架构，再用 `cargo oxide pipeline vecadd` 观察最终 PTX 是否针对 sm_80，验证 `CUDA_OXIDE_TARGET` 的硬覆盖效果。

**验收标准**：

- 能画出 `cargo oxide run vecadd` 从「敲命令」到「GPU 执行」的工具链/发现链路径。
- 能解释 `doctor` 的「致命」与「信息性」检查各有哪些。
- 能说出 `CUDA_OXIDE_TARGET` 与 `CUDA_OXIDE_DEVICE_ARCH` 的区别。

> 若本机无 GPU 或无 CUDA Toolkit，第 4 步的 `run` 可能失败——这是预期的。请改用 `cargo oxide build vecadd`（只编译不运行，不需要 GPU）完成「编译」环节的验证，并明确记录「运行环节待本地验证」。

## 6. 本讲小结

- cuda-oxide 钉死在 `nightly-2026-04-03` + 五个组件（`rust-src` / `rustc-dev` / `rust-analyzer` / `clippy` / `llvm-tools`），写在 [rust-toolchain.toml](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/rust-toolchain.toml)；系统层还依赖 CUDA Toolkit 12.x+、LLVM 21+ 的 `llc`、clang+libclang 开发头。
- `cargo oxide` 是一个自定义 cargo 子命令，仓库内靠 `.cargo/config.toml` 的别名工作，仓库外靠 cargo 的 `cargo-xxx` 约定工作；`main.rs` 会归一化两种 argv 形态再分发到十来个子命令。
- `cargo-oxide` **刻意不依赖 CUDA Toolkit**（其 `Cargo.toml` 注释明示），这让 `doctor` 能在裸机上运行；`doctor` 用零副作用的 `resolve_doctor_context`，做 9 项检查并区分「致命 / 信息性 / 可选」。
- 后端 `librustc_codegen_cuda.so` 通过 **5 步发现链**定位或构建：`CUDA_OXIDE_BACKEND` → 项目配置 → 本地源码 → 缓存 → 自动克隆；缓存失效用 `CacheStatus` 的三种信号（工具链 / 二进制 / 源码）分级处理。
- cuda-oxide 用一批 `CUDA_OXIDE_*` 环境变量在驱动进程与 rustc 后端进程间传配置，其中 `CUDA_OXIDE_TARGET`（硬覆盖）与 `CUDA_OXIDE_DEVICE_ARCH`（建议性 hint）的区别是理解架构选择行为的关键。
- `cargo-oxide` 通过 `CARGO_ENCODED_RUSTFLAGS` 注入一组「不可被覆盖」的正确性约束（`-Zcodegen-backend=...`、`-Zmir-enable-passes=-JumpThreading` 等），放在标志列表最后以利用 rustc 的 last-one-wins 语义。

## 7. 下一步学习建议

掌握了工具链与 `cargo-oxide` 驱动之后，建议按以下顺序继续：

1. **先跑通一个真实示例**：进入 [u1-l4 Hello GPU：vecadd 端到端](u1-l4-hello-gpu-vecadd.md)，用本讲学到的 `cargo oxide run vecadd` 把第一个内核跑到 GPU 上，建立单源编译的体感。
2. **横向了解能力边界**：阅读 [u1-l5 示例导览](u1-l5-examples-tour.md)，浏览 `examples/` 目录，用 `cargo oxide build`（无需 GPU）确认若干示例能编译，为后续学习选定切入点。
3. **深入命令细节**：当你需要看中间 IR 时，回头精读 `cargo oxide pipeline` 的实现（`commands.rs` 中的 `codegen_show_pipeline`，它会打开 `CUDA_OXIDE_DUMP_MIR` / `CUDA_OXIDE_DUMP_LLVM` 等诊断变量），这部分会在 [u4 编译流水线总览](u4-l1-backend-entry-and-split.md) 单元系统讲解。
4. **若要排查环境问题**：把 `cargo oxide doctor` 的 9 项检查与本讲的对齐表放在手边；遇到 `librustc_driver` 加载失败时，回想本讲的 `StaleVsToolchain` 信号——通常 `rm -rf ~/.cargo/cuda-oxide` 或重新 `cargo install` 即可自愈。
