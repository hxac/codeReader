# 项目定位与工作区全景

> 适用对象：第一次接触 PegaInfer、甚至第一次接触 Cargo workspace 的读者。
> 本讲是《pegainfer-build 学习手册》第一讲（u1-l1），无前置讲义要求。

## 1. 本讲目标

学完本讲，你应当能够：

1. **说清 pegainfer-build 解决的问题**：一个纯 Rust + CUDA 的推理引擎，为什么必须在**构建期**（而不是运行期）找到本机的 CUDA 工具包——nvcc 编译器、头文件目录、库目录；pegainfer-build 就是把「找到它们」这件重复劳动收敛成一个可复用的库。
2. **在工作区中定位该 crate 并找到全部消费方**：能打开根 `Cargo.toml` 指出 `pegainfer-build` 在成员列表中的位置；能用一次全仓 grep 列出所有把它声明为 `build-dependency` 的 crate（共两个：`pegainfer-kernels` 与 `pegainfer-cupti`）。
3. **理解这个 crate 的最小形态**：为什么它只有一个 `src/lib.rs`、`Cargo.toml` 里没有 `[lib]` 段、`publish = false`、连 `version` 字段都没有、唯一依赖是测试专用的 `tempfile`——每一项背后都是一条 Cargo 的设计规则。

## 2. 前置知识

### 2.1 PegaInfer 是什么

PegaInfer 是一个**用纯 Rust 和 CUDA 写的 LLM 推理引擎**：不依赖 PyTorch、不带 Python 运行时，对外提供 OpenAI 兼容的 `/v1/completions` API。它支持 Qwen3、Qwen3.5、DeepSeek-V2-Lite、Gemma 4、Kimi-K2、GLM5.2、Kimi-K3 等模型线；每条模型线是一个独立 crate，由 cargo feature 控制是否参与编译（默认只开 `qwen3`）。

### 2.2 Cargo workspace（工作区）

一个仓库里有几十个 crate 时，Cargo 允许用一个根 `Cargo.toml` 把它们组织成 **workspace**：

- `[workspace] members = [...]`：列出全部成员 crate；
- `[workspace.dependencies]`：统一定义依赖的版本与来源，成员用 `{ workspace = true }` 继承，避免各写各的、版本漂移；
- `resolver = "3"`：配套 Rust 2024 版的依赖解析策略。

### 2.3 build.rs：编译 crate 之前先跑一段 Rust

有些 crate 在「编译自己」之前需要先干活：编译 CUDA `.cu` 文件、生成代码、找系统库。Cargo 的机制是在 crate 根目录放一个 `build.rs`（构建脚本）：它在编译该 crate **之前**被编译和运行，通过 `println!("cargo:...")` 向 Cargo 发指令。本讲会遇到三条：

| 指令 | 作用 |
| --- | --- |
| `cargo:rustc-link-search=native=<目录>` | 让 rustc 到该目录找 `.so`/`.a`（相当于链接器 `-L`） |
| `cargo:rerun-if-env-changed=<变量>` | 只有该环境变量变化时才重跑本构建脚本 |
| `cargo:warning=<文本>` | 在构建日志里向用户输出一条警告 |

构建脚本自己的依赖声明在 **`[build-dependencies]`** 段：只服务于 `build.rs`，**不会**进入最终编译产物。pegainfer-build 正是只出现在这一段的库。

### 2.4 CUDA 工具包长什么样

装好 CUDA Toolkit 后，典型安装根目录下有：

- `bin/nvcc` —— CUDA C/C++ 编译器；
- `include/` —— `cuda.h` 等头文件；
- `lib64/`（或 `lib/`）—— `libcudart`、`libcublas` 等库。

环境变量 `CUDA_HOME` / `CUDA_PATH` 常用于指定安装根；未设置时惯例回退到 `/usr/local/cuda`。麻烦在于**不同安装方式的目录布局不同**（系统安装、conda 安装、NVIDIA HPC SDK 各一套）——这正是 pegainfer-build 要抹平的差异，细节留给下一讲；本讲只需知道它的职责边界：构建期定位工具包，运行期加载完全不管。

### 2.5 怎么读本讲的源码链接

链接形如 `https://github.com/<组织>/<仓库>/blob/<commit>/<路径>#L67-L73`：commit 已固定，永远指向同一段代码；`#L67-L73` 让 GitHub 直接高亮该行区间。本讲所有行号均基于 HEAD 提交 `223206b7`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用 |
| --- | --- | --- |
| [README.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/README.md) | 项目门面：定位、快速开始、模型线与架构 | §4.1 引用其对项目的定位句 |
| [Cargo.toml（仓库根）](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml) | workspace 的「户口本」：成员清单、共享依赖、共享 lint | §4.1/§4.2 精读成员列表与 `[workspace.dependencies]` |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md) | 仓库的工程说明书：构建命令、架构图、测试入口 | §4.1 佐证构建与架构约定 |
| [pegainfer-build/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/Cargo.toml) | 本 crate 的全部配置，仅 8 行 | §4.3 逐行精读 |
| [pegainfer-build/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs) | 本 crate 唯一源码文件，共 299 行 | §4.3 看文件骨架；逐函数精读留给后两讲 |
| [pegainfer-cupti/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/Cargo.toml)、[build.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs) | 消费方之一：34 行的最小构建脚本 | §4.2 精读完整生命周期 |
| [pegainfer-kernels/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/Cargo.toml)、[build.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/build.rs) | 消费方之二：约 2933 行的「构建总管」 | §4.2 只看依赖声明与 4 处 CudaToolkit 调用点 |

> **行号可信度说明**：`pegainfer-build/` 目录内、根 `Cargo.toml`、两个消费方的 `Cargo.toml` 以及 `pegainfer-cupti/build.rs` 的行号均经过逐行核对；`pegainfer-kernels/build.rs` 的 4 处调用行号来自规划清单并与抓取全文抽查一致（本讲义撰写环境无法在该目录外运行 grep 复核），若漂移请以函数名搜索为准；`README.md` 与 `CLAUDE.md` 经 Markdown 抓取后物理行号不可靠，只给文件级链接。

## 4. 核心概念与源码讲解

### 4.1 项目定位与架构图

#### 4.1.1 概念说明

PegaInfer 的卖点是「引擎层纯 Rust + CUDA」：模型调度、KV 管理、HTTP 前端都是 Rust，GPU 内核是 CUDA C++。要编译这些 GPU 内核（`pegainfer-kernels/csrc/*.cu`）和 CUPTI 剖析桩（`pegainfer-cupti`），构建脚本必须回答三个问题：

1. **nvcc 在哪**——没有它，`.cu` 文件编译不了；
2. **CUDA 头文件在哪**——没有 `-I` 路径，`#include <cuda.h>` 找不到；
3. **CUDA 库在哪**——没有 `-L` 路径，链接不到 `libcudart` 等库。

这两个 crate 的 `build.rs` 都要做同一件事。pegainfer-build 的定位就是：**把这件事收敛成一个库**，让每个消费方一行 `CudaToolkit::discover()` 拿到全部答案。它不参与任何运行时行为——只在 `cargo build` 的一瞬间存在，是工作区的「工具间」。

#### 4.1.2 核心流程

从 clone 到启动，pegainfer-build 出现在链条的「编译前」环节：

```
git clone → cargo run --release（仓库根）
   │  Cargo 读根 Cargo.toml：发现 21 个 workspace 成员
   │
   ├─ 构建 pegainfer-kernels / pegainfer-cupti 之前，先编译并运行各自的 build.rs
   │     ├─ 调 pegainfer_build::CudaToolkit::discover()
   │     │     └─ 回退链：$CUDA_HOME → $CUDA_PATH → /usr/local/cuda
   │     │        并按「经典 / conda / HPC SDK」三种布局补全 include 与 lib 目录
   │     ├─ 用 toolkit.nvcc 编译 .cu；用 toolkit.include_dirs 提供头文件路径
   │     └─ 用 toolkit.link_search() 把库目录交给最终链接
   │
   └─ 产出引擎（pegainfer-server），运行时提供 OpenAI 兼容的 /v1/completions
```

#### 4.1.3 源码精读

**(a) 项目定位句（README）。** README 开篇即说明引擎边界：「PegaInfer serves LLMs through an OpenAI-compatible API. Each model owns its scheduler, state, and kernels; serving and KV infrastructure are shared. No PyTorch or Python runtime.」——每条模型线自带调度器与内核，服务与 KV 基础设施共享，无 Python 运行时。见 [README.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/README.md)（文件级链接）。

**(b) 工程说明书的同义表述（CLAUDE.md）。** [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md) 开头写着「Pure Rust + CUDA LLM inference engine. No PyTorch, no frameworks. OpenAI-compatible `/v1/completions` API.」，并在 Architecture 一节画出 `pegainfer-frontend → per-model scheduler/executor → pegainfer-core runtime + pegainfer-kernels` 的分层——`pegainfer-kernels` 正是 pegainfer-build 的头号消费方（文件级链接）。

**(c) 根 Cargo.toml：成员清单。** 本讲最重要的一个文件：

[根 Cargo.toml:L1-L27](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L1-L27) —— 声明默认成员、21 个成员 crate 与解析器版本：

```toml
[workspace]
default-members = ["pegainfer-server"]
members = [
  "pegainfer-frontend",
  "pegainfer-sim",
  "pegainfer-server",
  "pegainfer-core",
  "pegainfer-cupti",
  "pegainfer-kernels",
  "pegainfer-bench",
  "pegainfer-build",
  # ……以下还有 deepseek-v2-lite、gemma4、glm52、k3、kimi-k2、
  # qwen3、qwen35、qwen4、sample、kv-cache、kv-offload、kv-store，
  # 以及注释后的 dynamo fork "kvbm/kvbm-logical"
]
resolver = "3"
```

三个观察点：

- [根 Cargo.toml:L11](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L11) —— `pegainfer-build` 排在运行时基础设施与构建相关 crate（cupti/kernels）之后，与 `pegainfer-bench` 相邻：都是「工具型」成员；
- [根 Cargo.toml:L2](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L2) —— `default-members` 只有 `pegainfer-server`：仓库根直接 `cargo build` 只以 server 为顶层目标（传递依赖仍会被编译，其中就包括 kernels 的 build 依赖 pegainfer-build）；
- [根 Cargo.toml:L27](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L27) —— `resolver = "3"` 与成员 crate 的 edition 2024 配套。

#### 4.1.4 代码实践：数一遍工作区成员

1. **实践目标**：亲手确认成员数量与 pegainfer-build 的位置，建立「打开文件就能定位」的手感。
2. **操作步骤**（仓库根目录执行）：
   ```bash
   grep -n 'pegainfer-build' Cargo.toml    # 应看到成员行 L11 与依赖行 L122
   git ls-files pegainfer-build            # 只读命令：列出本 crate 的全部受管文件
   ```
3. **需要观察的现象**：第一条命令给出两个行号；第二条列出本 crate 的文件清单。
4. **预期结果**：本 crate 受管文件只有 `Cargo.toml` 与 `src/lib.rs`；成员清单共 21 项。**待本地验证**（本讲义撰写环境只读，未运行上述命令）。

#### 4.1.5 小练习与答案

1. **问**：`default-members` 只有 `pegainfer-server`，那 `cargo build`（不带 `-p`）会不会编译 pegainfer-build？
   **答**：会——作为传递依赖。构建 server 需要构建 pegainfer-kernels，而 kernels 的 `build.rs` 依赖 pegainfer-build，所以它会被编进构建图；但只有 `cargo build -p pegainfer-build` 才把它当**顶层目标**单独构建/测试。
2. **问**：为什么 README 与 CLAUDE.md 都强调 GPU 构建「Always use --release」？
   **答**：debug 构建的 CUDA/主机代码慢几个数量级，实际使用会超时；对本 crate 的纯 CPU 单元测试则无此约束。
3. **问**：pegainfer-build 在 CLAUDE.md 的运行时架构图里处于哪一层？
   **答**：哪一层都不是——它只存在于**构建期**，挂在 `pegainfer-kernels`（架构图最底层的 kernel 构建所有者）与 `pegainfer-cupti` 的 build.rs 里。

### 4.2 工作区成员与 build-dependency 依赖关系

#### 4.2.1 概念说明

workspace 里 crate 之间用 **path 依赖**互指（`pegainfer-build = { path = "pegainfer-build" }`），版本统一收口在根的 `[workspace.dependencies]`，成员以 `{ workspace = true }` 继承——一份定义，处处同步。pegainfer-build 的特殊之处在于它**只**出现在 `[build-dependencies]` 段：消费方是「构建脚本」而不是 Rust 库代码。全仓共有两个这样的消费方：构建总管 `pegainfer-kernels` 和剖析桩 `pegainfer-cupti`。

#### 4.2.2 核心流程

```
根 Cargo.toml [workspace.dependencies]（唯一版本来源）
  ├─ pegainfer-build = { path = "pegainfer-build" }     ← L122
  └─ tempfile = "3"                                      ← L170（供本 crate 测试继承）
        │  成员以 { workspace = true } 继承
        ▼
pegainfer-kernels/Cargo.toml [build-dependencies]  L24-L26
  └─ build.rs（约 2933 行）：4 处 CudaToolkit 调用
pegainfer-cupti/Cargo.toml   [build-dependencies]  L7-L9
  └─ build.rs（34 行）：discover() → include_dirs → link_search()
```

#### 4.2.3 源码精读

**(a) 版本收口处（根 Cargo.toml）。**
[根 Cargo.toml:L121-L124](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L121-L124) —— `[workspace.dependencies]` 里连续的内部 path 依赖：

```toml
pegainfer-bench = { path = "pegainfer-bench" }
pegainfer-build = { path = "pegainfer-build" }
pegainfer-core  = { path = "pegainfer-core" }
pegainfer-cupti = { path = "pegainfer-cupti" }
```

**(b) 消费方一：pegainfer-cupti（最小完整生命周期）。**
[pegainfer-cupti/Cargo.toml:L7-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/Cargo.toml#L7-L9) —— 声明构建依赖：cc 负责编译 C++，pegainfer-build 负责找 CUDA：

```toml
[build-dependencies]
cc = { workspace = true }
pegainfer-build = { workspace = true }
```

它的 `build.rs` 只有 34 行，却展示了 pegainfer-build 的完整用法：

[pegainfer-cupti/build.rs:L4-L8](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L4-L8) —— 发现工具包，并准备 CUPTI 特有的子目录（CUPTI 是 NVIDIA 剖析库，装在 toolkit 根的 `extras/CUPTI/` 下，不在主 include/lib 里——这正演示了公有字段 `toolkit.root` 的用途）：

```rust
let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
let toolkit = pegainfer_build::CudaToolkit::discover();
// CUPTI ships outside the main toolkit dirs in classic installs.
let cupti_include = toolkit.root.join("extras/CUPTI/include");
let cupti_lib64 = toolkit.root.join("extras/CUPTI/lib64");
```

[pegainfer-cupti/build.rs:L16](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L16) —— 把 `toolkit.include_dirs` 交给 cc 编译器（头文件搜索路径）。

[pegainfer-cupti/build.rs:L22-L25](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L22-L25) —— 先用库统一打印常规库目录，CUPTI 自己的 lib64 存在时再补一条：

```rust
toolkit.link_search();
if cupti_lib64.is_dir() {
    println!("cargo:rustc-link-search=native={}", cupti_lib64.display());
}
```

**(c) 消费方二：pegainfer-kernels（构建总管，4 处调用点）。**
[pegainfer-kernels/Cargo.toml:L24-L26](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/Cargo.toml#L24-L26) —— 同样的三行声明（cc + pegainfer-build）。

[pegainfer-kernels/build.rs:L929](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/build.rs#L929) —— 内核实验室的共享库链接函数以 `&CudaToolkit` 为参数，内部用 `toolkit.root.join("lib64")` 给 nvcc 传 `-L`：

```rust
fn link_kernel_lab_shared(
    nvcc: &str,
    toolkit: &pegainfer_build::CudaToolkit,
    out_dir: &Path,
    obj_files: &[PathBuf],
) { /* …… cmd.arg(format!("-L{}", toolkit.root.join("lib64").display())); …… */ }
```

[pegainfer-kernels/build.rs:L2209-L2213](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/build.rs#L2209-L2213) —— 主路径开头「三连问」一次拿齐 nvcc、头目录（找不到 `cuda.h` 时回退 `<root>/include`）：

```rust
let toolkit = pegainfer_build::CudaToolkit::discover();
let nvcc = toolkit.nvcc.to_string_lossy().into_owned();
let cuda_include = toolkit
    .header_dir("cuda.h")
    .unwrap_or_else(|| toolkit.root.join("include"));
```

[pegainfer-kernels/build.rs:L2884-L2887](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/build.rs#L2884-L2887) —— 收尾把库目录交给链接器（Windows 布局不同，单独走 `lib/x64` 分支）：

```rust
if cfg!(target_os = "windows") {
    println!("cargo:rustc-link-search=native={}/lib/x64", toolkit.root.display());
} else {
    toolkit.link_search();
}
```

> 注：kernels/build.rs 的四个行号来自规划清单并与抓取全文抽查一致；若行号漂移，请以 `grep -n CudaToolkit pegainfer-kernels/build.rs` 重新定位。

#### 4.2.4 代码实践：全仓搜索消费方

1. **实践目标**：用一条 grep 找出「谁在用 pegainfer-build」，并记录每个消费方 build.rs 的调用行号。
2. **操作步骤**（仓库根目录执行）：
   ```bash
   grep -rn --include=Cargo.toml 'pegainfer-build' .   # 依赖声明处
   grep -rn --include='*.rs' 'pegainfer_build::' .      # 源码调用处（连字符→下划线）
   ```
3. **需要观察的现象**：第一条命令命中 3 个文件：根 `Cargo.toml`（版本收口）与两个消费方的 `Cargo.toml`；第二条只命中两个 `build.rs`。
4. **预期结果**：消费方恰为 `pegainfer-kernels` 与 `pegainfer-cupti` 两个，无第三者。**待本地验证**。

#### 4.2.5 小练习与答案

1. **问**：`[dependencies]` 与 `[build-dependencies]` 里都写 `pegainfer-build` 有何不同？
   **答**：前者会编译进 crate 本身、进入最终产物；后者只供 `build.rs` 使用、不进入产物。pegainfer-build 全仓只出现在后者——它是构建期工具，运行时不需要它。
2. **问**：两个消费方都用 `{ workspace = true }` 继承，版本定义在哪里？
   **答**：根 `Cargo.toml` 的 `[workspace.dependencies]`（L122 的 `pegainfer-build = { path = "pegainfer-build" }`）。
3. **问**：cupti 的 build.rs 为什么还要自己多拼一条 `extras/CUPTI/lib64`？
   **答**：CUPTI 的头文件与库不在 CUDA 主 include/lib 目录里，`CudaToolkit` 只覆盖主布局；消费方基于公有字段 `toolkit.root` 自行补齐特殊子树。

### 4.3 crate 的最小形态

#### 4.3.1 概念说明

pegainfer-build 的 Cargo.toml 只有一段半：`[package]` 五行 + `[dev-dependencies]` 两行，共 8 行。这不是偷懒，而是三条 Cargo 设计规则的自然结果：

- **约定优于配置**：不写 `[lib]` 段，Cargo 默认把 `src/lib.rs` 当作库目标；
- **不发布就不需要版本**：`publish = false` 的内部 path 依赖不进 crates.io，`version` 字段（仅发布时必需）干脆省掉；
- **构建工具越轻越好**：库本体零运行时依赖（只用 `std`），`tempfile` 只在测试时编译。

#### 4.3.2 核心流程

Cargo 读这 8 行时的决策链：

```
读到 [package]：name = "pegainfer-build"，无 version、publish = false
  → 视为不发布的内部库
没有 [lib] 段
  → 采用默认约定：库目标 = src/lib.rs，crate 名即 lib 名（连字符→下划线 pegainfer_build）
没有 [dependencies] 段
  → 正常构建零依赖，只连 std
[dev-dependencies] tempfile
  → 仅 cargo test 时编译；TempTree 测试夹具用它建临时目录树
```

#### 4.3.3 源码精读

[pegainfer-build/Cargo.toml:L1-L8](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/Cargo.toml#L1-L8) —— 全部 8 行，逐行注释如下：

```toml
[package]                            # 包定义开始
edition = "2024"                     # 2024 版 Rust（let-chains 等新语法已稳定）
license = "Apache-2.0"               # 许可证
name = "pegainfer-build"             # 包名（代码里用下划线形式 pegainfer_build 引用）
publish = false                      # 内部工具，禁止发布到 crates.io

[dev-dependencies]                   # 只在测试时启用的依赖
tempfile = { workspace = true }      # 临时目录：测试夹具 TempTree 用它模拟各种安装布局
```

注意三处「缺席」：没有 `[lib]` 段、没有 `[dependencies]` 段、没有 `version` 字段——分别对应 4.3.1 的三条规则。`edition 2024` 与根 `resolver = "3"` 配套；`tempfile` 版本 `3` 由根 Cargo.toml 的 `[workspace.dependencies]` 统一收口。

[src/lib.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs) 共 299 行、一个文件，骨架如下（行号经本地 grep 核对）：

| 行号 | 内容 |
| --- | --- |
| [L12-L52](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L12-L52) | `#[cfg(test)]` 的 `find_package`——通用探测助手，如今只服务测试 |
| [L56-L63](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L56-L63) | `fn target_dirs()`——生成 `targets/<arch>` 候选目录名 |
| [L67-L73](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L67-L73) | `pub struct CudaToolkit`——对外的心脏类型 |
| [L75-L142](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L75-L142) | `impl CudaToolkit`——`discover()`（L76）、`from_root()`（L91）、`header_dir()`（L125）、`link_search()`（L132） |
| [L145-L159](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L145-L159) | `existing_deduped`——过滤不存在的目录并按真实路径去重 |
| [L161-L299](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L161-L299) | `#[cfg(test)] mod tests`——TempTree 夹具（L165-L191）+ 7 个测试 |

心脏类型的字段设计也值得看一眼：

```rust
pub struct CudaToolkit {
    pub root: PathBuf,
    /// `{root}/bin/nvcc` when present, otherwise bare `nvcc` from `$PATH`.
    pub nvcc: PathBuf,
    pub include_dirs: Vec<PathBuf>,
    lib_dirs: Vec<PathBuf>,   // 私有：只经 link_search() 对外生效
}
```

`root/nvcc/include_dirs` 公有（消费方要自己拼子路径，如 cupti 的 `extras/CUPTI`、kernels 的 `lib64`）；`lib_dirs` 私有——链接路径只允许通过 `link_search()` 以 `cargo:rustc-link-search` 指令的形式输出，防止消费方绕过协议乱用。

#### 4.3.4 代码实践：跑通全部 7 个测试

1. **实践目标**：确认这个零依赖 crate 的测试在纯 CPU 环境即可全部通过。
2. **操作步骤**（仓库根目录执行）：
   ```bash
   cargo test --release -p pegainfer-build --lib
   ```
   本 crate 无 GPU 依赖，去掉 `--release` 也可（编译更快）；CLAUDE.md 要求 `--release` 是针对 GPU 构建的约定。
3. **需要观察的现象**：测试列表逐个输出：`classic_layout`（[L194](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L194)）、`conda_layout`（[L209](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L209)）、`hpc_sdk_layout_adds_math_libs_sibling`（[L227](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L227)）、`symlinked_dirs_dedupe`（[L238](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L238)，仅 Unix）、`unknown_layout_yields_nothing`（[L261](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L261)）、`find_package_returns_matching_root_and_check_file`（[L272](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L272)）、`missing_header_panics_with_all_candidates`（[L289](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L289)）。
4. **预期结果**：Linux 上 `test result: ok. 7 passed; 0 failed`（Windows 上 6 个——`symlinked_dirs_dedupe` 带 `#[cfg(unix)]`）。7 的依据是源码中恰好 7 处 `#[test]`（已 grep 核对）；实际运行**待本地验证**（本讲义撰写环境只读，未执行 cargo）。

#### 4.3.5 小练习与答案

1. **问**：为什么没有 `[lib]` 段也能编译成库？
   **答**：Cargo 的约定——库 crate 的默认源码路径就是 `src/lib.rs`，库目标自动创建；`[lib]` 段只在你需要改 name、path、crate-type 等默认值时才写。
2. **问**：为什么连 `version = "0.x.y"` 都没有？
   **答**：`version` 只在发布到 crates.io 时是必需字段；本 crate `publish = false` 且以 path 依赖被引用，版本号没有语义，省掉反而明确表达了「不发布」的意图。
3. **问**：`tempfile` 若误写进 `[dependencies]` 会怎样？
   **答**：功能上测试仍能过，但正常构建也会编译并链接 tempfile，违背「构建工具零依赖」的初衷；放 `[dev-dependencies]` 保证生产构建路径完全不碰它。

## 5. 综合实践

**任务：亲手生成 pegainfer-build 的「生产—消费」全景表。** 把本讲三个模块串起来，产出一张可长期维护的表格（也是手册后续讲义的检查基准）。

1. **实践目标**：验证测试基线 + 找齐全部消费方 + 记录每处调用的行号。
2. **操作步骤**（仓库根目录执行）：
   ```bash
   # 第一步：测试基线
   cargo test --release -p pegainfer-build --lib
   # 第二步：找声明
   grep -rn --include=Cargo.toml 'pegainfer-build' .
   # 第三步：找调用
   grep -rn 'CudaToolkit' pegainfer-kernels/build.rs pegainfer-cupti/build.rs
   ```
3. **需要观察的现象**：第一步逐个跑完 7 个测试；第二步命中根与两个消费方共 3 个 Cargo.toml；第三条列出两个 build.rs 里全部 CudaToolkit 出现点。
4. **预期结果**：填出下表（行号按你的 grep 输出为准，本表数值为 HEAD `223206b7` 的核对结果）：

   | 消费方 | Cargo.toml 声明 | build.rs 调用点 | 用到的 API |
   | --- | --- | --- | --- |
   | pegainfer-kernels | [L24-L26](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/Cargo.toml#L24-L26) | 主路径 L2209-L2213（discover/nvcc/header_dir）、L929 起 link_kernel_lab_shared（root/lib64）、收尾 L2884-L2887（link_search） | discover、nvcc、header_dir、link_search、root |
   | pegainfer-cupti | [L7-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/Cargo.toml#L7-L9) | [L4-L8](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L4-L8)（discover+root）、[L16](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L16)（include_dirs）、[L22-L25](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L22-L25)（link_search） | discover、root、include_dirs、link_search |

   两个消费方之外应无第三者。**命令输出待本地验证。**

## 6. 本讲小结

- PegaInfer 是纯 Rust + CUDA 的 LLM 推理引擎；pegainfer-build 是工作区里的**构建辅助库**，职责单一：构建期定位 CUDA 工具包（nvcc / 头文件 / 库目录），运行期毫无瓜葛。
- 根 `Cargo.toml` 共 21 个成员（pegainfer-build 在 L11），`resolver = "3"`，`default-members` 只有 `pegainfer-server`；本 crate 的版本由 `[workspace.dependencies]`（L122）的 path 依赖统一提供。
- 全仓恰有**两个**消费方把它声明为 `build-dependency`：`pegainfer-kernels`（约 2933 行构建总管，4 处调用）与 `pegainfer-cupti`（34 行最小脚本，完整生命周期示范）。
- crate 形态极简的根因是三条 Cargo 规则：无 `[lib]` 段（src/lib.rs 约定）、`publish = false` 且无 version（不发布）、`tempfile` 只在 dev-dependencies（零运行时依赖）。
- 7 个单元测试纯 CPU 即可运行，用 tempfile 的 TempTree 夹具模拟经典 / conda / HPC SDK 三种安装布局——`cargo test --release -p pegainfer-build --lib` 应 7 passed（待本地验证）。

## 7. 下一步学习建议

按手册规划，下一讲将进入 `src/lib.rs` 的核心：`CudaToolkit::discover()` 的回退链（`$CUDA_HOME` → `$CUDA_PATH` → `/usr/local/cuda`）、`from_root()` 对三种安装布局的适配、以及 `target_dirs()` 的 aarch64 双候选处理。在此之前，建议先自行通读两份材料：

1. [pegainfer-cupti/build.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs) 全文——只有 34 行，是 pegainfer-build 用法的最小完整样例；
2. [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md) 的 Build & Run 与 Architecture 两节——理解构建系统全貌（`pegainfer-kernels/build.rs` 拥有 CUDA/Triton 编译）。
