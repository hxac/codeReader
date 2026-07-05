# Workspace 与 crate 地图

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `Cargo.toml` 里**每一个 workspace 成员 crate** 的职责，不再面对一长串名字发懵。
- 用**三层（用户层 / 编译器层 / 工具层）** 的视角给所有 crate 分类，并进一步区分它属于**设备端、宿主端、编译期还是工具**。
- 解释为什么 `rustc-codegen-cuda` 这个最核心的后端 crate **不在 workspace 里**、为什么它必须被 `dylib` 编译、为什么它不能写 `{ workspace = true }`。
- 认识本轮（#314）新增的 **`cuda-oxide-codegen`**——一个 `rustc 无关` 的实验性 PTX 后端，知道它在编译器层的位置。
- 在脑海里建立一张「crate 分层依赖草图」，为后续逐层深挖（u2 设备端、u3 宿主端、u4 编译流水线）打好地图。

本讲只读两个文件：根 `Cargo.toml` 与 `README.md`，外加 `rustc-codegen-cuda/Cargo.toml` 的头部。不涉及 GPU，全程可以离线阅读。

## 2. 前置知识

本讲承接 [u1-l1 项目定位与编译流水线总览](./u1-l1-project-overview.md)。那里我们已经建立了几个关键认知，本讲直接复用、不重复：

- **cuda-oxide 是一个自定义 rustc 后端**，把纯 Rust 的 `#[kernel]` 函数编译成 CUDA PTX。
- **单源编译**：host 与 device 代码写在同一个文件里，一次 `cargo oxide build` 同时产出。
- 端到端流水线是 **Rust → MIR → dialect-mir → mem2reg → LLVM dialect → LLVM IR → llc → PTX**。
- 后端入口符号是 `__rustc_codegen_backend`，由 rustc 动态加载；device 判定靠保留命名空间 `cuda_oxide_kernel_*` + 调用图可达性，不需要 `#[cfg]`。

如果你对「workspace」「crate」这两个 Cargo 概念还不熟，这里用一句话复习：

- **crate**：Rust 的最小编译单元，一个 crate 对应一个 `Cargo.toml`，产出要么是可被 `use` 的库，要么是可执行文件，要么是（像本项目的）编译器插件。
- **workspace**：一组 crate 共享同一个解析与锁定环境（`Cargo.lock`、公共依赖版本、`[workspace.dependencies]`）。workspace 让多个 crate 之间可以用 `path = "..."` 互相引用，又统一对齐外部依赖版本。

下面所有讨论都建立在这两个概念之上。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| `Cargo.toml`（仓库根） | 定义 workspace 的成员列表、公共包元数据、共享依赖版本 | 看 `members = [...]` 给出的「官方点名册」 |
| `README.md` 的 *Crate Overview* 段 | 用三张表（User-Facing / Compiler / Build Tooling）概括每个 crate | 作为职责速查表 |
| `crates/rustc-codegen-cuda/Cargo.toml` | 被隔离的后端 crate 自己的清单 | 解释它「为何不在 workspace」 |
| `crates/cuda-oxide-codegen/Cargo.toml` | 本轮新增的 rustc 无关后端的清单 | 对比「在 workspace 里、能 `{ workspace = true }`」的普通成员 |
| `.cargo/config.toml` | cargo 别名 `oxide` 与构建期环境变量 | 解释 `cargo oxide` 如何被识别为子命令 |

## 4. 核心概念与源码讲解

### 4.1 workspace members 列表：cuda-oxide 的官方点名册

#### 4.1.1 概念说明

一个 workspace 的「成员」写在根 `Cargo.toml` 的 `[workspace] members = [...]` 里。可以把它理解为 cuda-oxide **正式承认的、由 `cargo` 统一编译管辖的 crate 清单**。注意：仓库目录里有的文件夹不一定就是 workspace 成员——`rustc-codegen-cuda` 就是典型反例（见 4.4）。

读这份清单时，最关键的不是单个名字，而是**作者用注释把成员分成的几组**。注释是作者留下的「分组意图」，比任何外部文档都权威。

#### 4.1.2 核心流程

读取 `members` 列表的标准做法：

1. 先看每行**注释**，得到分组（Core / FFI bindings / Internal naming contract / Fuzzer）。
2. 再看注释**下方**紧跟的 crate 名，把它归入该组。
3. 把分组与 README 的 *Crate Overview* 三张表做**交叉验证**：members 是「全部」，README 表是「面向用户的精选+职责说明」。members 里多出来的，就是「内部支撑」crate（见 4.3 末尾）。

#### 4.1.3 源码精读

先看 members 列表本身，注意四组注释：

[Cargo.toml:L1-L28](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/Cargo.toml#L1-L28) — 这就是整张点名册。中文要点：

- 第 1–2 行：`[workspace]` + `resolver = "3"`。`resolver = "3"` 是 Rust 2024 edition 默认的依赖解析器（feature 合并更精细），与第 31 行的 `edition = "2024"` 配套。
- 第 4 行 `# Core crates` 下面挂了 **13 个** crate，是项目骨架：用户接口（`cuda-device`/`cuda-host`/`cuda-macros`）、编译流水线（`mir-importer`/`cuda-oxide-codegen`/`mir-lower`/`dialect-mir`/`dialect-nvvm`/`llvm-export`/`mir-transforms`/`nvvm-transforms`）、工具（`cargo-oxide`）、制品格式（`oxide-artifacts`）。其中 `cuda-oxide-codegen` 是本轮（#314）新加入的「rustc 无关后端」（见 4.3）。
- 第 18 行 `# FFI bindings` 下面是 5 个与 CUDA/LLVM C 库打交道的 crate：`cuda-bindings`/`cuda-core`/`cuda-async`/`libnvvm-sys`/`nvjitlink-sys`。
- 第 24 行 `# Internal naming contract (workspace-private; publish = false)` 下面只有 `reserved-oxide-symbols`——它是符号命名契约，私有、不发布。
- 第 26 行 `# Differential codegen fuzzer support` 下面是 `fuzzer`——差分代码生成模糊测试支持。

紧接着第 30–34 行是 `[workspace.package]`，统一声明全体成员的公共元数据（edition / authors / license / repository）。注意本轮（#331）把整个项目的许可统一为 `license = "Apache-2.0"`——所有成员 crate 现在都继承同一个许可，不再有旧版的 LICENSE-NVIDIA。

再往下有一段非常关键的解释，直接预告了 4.4 节的主题：

[Cargo.toml:L36-L39](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/Cargo.toml#L36-L39) — 明确写了 `rustc-codegen-cuda` **不是** workspace 成员，因为它「需要特殊的 nightly 特性和不同的构建流程」，并提示用 `cargo oxide run <example>` 来构建运行示例。

再看共享依赖里最关键的一段——Pliron IR 框架的版本固定：

[Cargo.toml:L49-L51](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/Cargo.toml#L49-L51) — workspace 把 `pliron` / `pliron-derive` / `pliron-llvm` 三个都钉死在同一个 git rev `222dd96...`。注释里有一句警告（[Cargo.toml:L47-L48](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/Cargo.toml#L47-L48)）：`rustc-codegen-cuda/Cargo.toml` **必须**保持同一个 rev，否则 pliron 会被解析成两个不同的 crate，类型无法统一。这条线索会在 4.4 再次出现。

最后看新增成员如何被声明为「普通 workspace 成员」——`cuda-oxide-codegen` 用 `{ workspace = true }` 继承公共元数据：

[crates/cuda-oxide-codegen/Cargo.toml:L4-L8](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/Cargo.toml#L4-L8) — `edition.workspace = true`、`license.workspace = true` 一路继承，`description = "Experimental rustc-independent cuda-oxide PTX backend"`。这与 4.4 的 `rustc-codegen-cuda`（被隔离、不能 `{ workspace = true }`）形成鲜明对比：**`cuda-oxide-codegen` 是一个正常成员，只是定位上是一个实验性的独立后端**。

#### 4.1.4 代码实践

**实践目标**：亲手把 members 列表按注释分组数清楚。

**操作步骤**：

1. 打开根 `Cargo.toml`，定位到 `members = [`。
2. 按四组注释，把每个 crate 填进下表（答案见 4.1.5）：

   | 注释分组 | 包含的 crate |
   |---------|-------------|
   | Core crates | ? |
   | FFI bindings | ? |
   | Internal naming contract | ? |
   | Differential codegen fuzzer | ? |

3. 数总数：Core 应为 **13**（本轮新增 `cuda-oxide-codegen`），FFI bindings 应为 5，其余各 1，合计 **20 个成员**。

**需要观察的现象**：`cargo-oxide` 被归在 **Core crates** 组，而不是单独的「工具」组——这说明作者把工具链视为项目核心的一部分；同时 `cuda-oxide-codegen` 也在 Core 组，与 `mir-importer`/`mir-lower` 并列。

**预期结果**：得到一张 20 项的分组表；同时你会发现 README 的 *Crate Overview* 表里**没有** `cuda-oxide-codegen`、`mir-transforms`、`nvvm-transforms`、`oxide-artifacts`、`reserved-oxide-symbols`、`fuzzer` 这 6 个——它们是「内部支撑」crate，对最终用户不可见。

#### 4.1.5 小练习与答案

**练习 1**：members 列表里，哪些 crate 既出现在 members 中、又出现在 README 的 *User-Facing Crates* 表里？哪些只出现在 members、不出现在任何 README 表里？

**答案**：出现在 *User-Facing Crates* 表两边的是 `cuda-device`、`cuda-host`、`cuda-macros`、`cuda-bindings`、`cuda-core`、`cuda-async`、`libnvvm-sys`、`nvjitlink-sys`（共 8 个用户面 crate）。只在 members、不在任何 README 表的是 `cuda-oxide-codegen`、`mir-transforms`、`nvvm-transforms`、`oxide-artifacts`、`reserved-oxide-symbols`、`fuzzer`（共 6 个内部 crate，其中 `cuda-oxide-codegen` 是本轮新增）。

**练习 2**：`resolver = "3"` 与 `edition = "2024"` 是什么关系？

**答案**：`resolver = "3"` 是 Rust 2024 edition 引入的新依赖解析器（feature unification 更精细，避免不必要的 feature 被激活）。`edition = "2024"`（`[workspace.package]` 第 31 行）声明本 workspace 全体 crate 使用 2024 edition，二者是配套关系。

---

### 4.2 用户面 crate 概览表：你写 GPU 程序时直接 import 的东西

#### 4.2.1 概念说明

README 的 *Crate Overview* 把 crate 分成三档：User-Facing（用户面）、Compiler（编译器）、Build Tooling（工具）。**用户面**是普通开发者写 cuda-oxide 程序时真正会 `use` 的 crate——你不需要懂编译器内部，只要会用这些 API 就能跑通 GPU 程序。

用户面内部还要再切一刀：**设备端（device）** vs **宿主端（host）**。

- 设备端 crate 的代码**会被编译进 PTX、在 GPU 上运行**，例如 `thread::index_1d()` 最终变成一条 PTX 指令。
- 宿主端 crate 的代码**运行在 CPU 上**，负责分配显存、加载模块、启动内核、搬运数据。

这层区分非常重要，因为同一个项目里 host 和 device 代码共享类型但执行环境完全不同。

#### 4.2.2 核心流程

给一个用户面 crate 分类，按以下决策树：

```
该 crate 的代码最终在哪里执行？
├─ 在 GPU 上（编译进 PTX）        → 设备端（device）
├─ 在 CPU 上（管理/启动 GPU 工作） → 宿主端（host）
└─ 在编译期（proc macro 展开）     → 编译期工具（可归入宿主构建期）
```

对照 README 的 Quick Start 示例，最顶部的 `use` 就是典型的「用户面组合」：

```rust
use cuda_device::{cuda_module, kernel, thread, DisjointSlice};
use cuda_core::{CudaContext, DeviceBuffer, LaunchConfig};
```

这里 `cuda_device` 提供 `#[kernel]` 宏与 `thread` 索引（设备端），`cuda_core` 提供 `CudaContext`/`DeviceBuffer`/`LaunchConfig`（宿主端）。一句话：**设备端写算什么，宿主端写怎么跑**。

#### 4.2.3 源码精读

README 的用户面表：

[README.md:L271-L280](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L271-L280) — 列出 8 个用户面 crate。中文整理如下，并补上 README 没明说的「执行位置」一列：

| Crate | README 描述 | 执行位置 | 一句话直觉 |
|-------|------------|---------|-----------|
| `cuda-device` | 设备 intrinsics（`thread::*`、`warp::*`、barriers） | **设备端** | 在 GPU 上用的「语法糖 + 内建函数」 |
| `cuda-host` | 类型化模块加载、启动助手、LTOIR 加载器 | 宿主端 | 把编译好的 PTX 模块「装填进枪膛」 |
| `cuda-macros` | 过程宏（`#[cuda_module]`、`#[kernel]`、`gpu_printf!`） | 编译期 | 把你的 `#[kernel]` 改写成可启动的代码 |
| `cuda-bindings` | 对 `cuda.h` 的原始 `bindgen` FFI 绑定 | 宿主端 | CUDA Driver API 的 unsafe 薄包装 |
| `cuda-core` | 安全 RAII 包装（`CudaContext`、`CudaStream`、`DeviceBuffer<T>`…） | 宿主端 | 给 Driver API 套上 Rust 的所有权安全 |
| `cuda-async` | 异步执行层（`DeviceOperation`、`DeviceFuture`、`DeviceBox<T>`） | 宿主端 | 让 GPU 工作可组合、可 `.await` |
| `libnvvm-sys` | 对 libNVVM 的 `dlopen` 绑定（`cuda-host::ltoir` 用） | 宿主端 | 运行时按需加载 NVVM 库 |
| `nvjitlink-sys` | 对 nvJitLink 的 `dlopen` 绑定（`cuda-host::ltoir` 用） | 宿主端 | 运行时按需加载 JIT 链接库 |

几个值得记住的设计要点：

- **`libnvvm-sys` / `nvjitlink-sys` 用 `dlopen` 而不是静态链接**：它们在运行时动态打开 CUDA 库，所以**编译时不需要 CUDA Toolkit**。这一点和 `cargo-oxide`「故意不含 CUDA 依赖」的设计（见 4.4）一脉相承。
- **`cuda-bindings` 是唯一需要 CUDA Toolkit 才能编译的 crate**，因为它的 `build.rs` 跑 `bindgen` 读 `cuda.h` 头文件。这也是 README 反复强调要装 clang 的原因。
- **`cuda-core` 是安全层，`cuda-bindings` 是 unsafe 层**：正常用户写程序用 `cuda-core`；只有需要直接调 Driver API 时才碰 `cuda-bindings`。

#### 4.2.4 代码实践

**实践目标**：验证「设备端 vs 宿主端」的分类直觉。

**操作步骤**：

1. 回到 README 的 Quick Start（[README.md:L28-L77](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L28-L77)）。
2. 把示例里的每个标识符归类：它来自哪个 crate？属于设备端还是宿主端？

   | 标识符 | crate | 设备/宿主 |
   |--------|-------|----------|
   | `thread::index_1d()` | ? | ? |
   | `DisjointSlice` | ? | ? |
   | `CudaContext::new(0)` | ? | ? |
   | `DeviceBuffer::from_host(...)` | ? | ? |
   | `kernels::load(&ctx)` | ? | ? |
   | `LaunchConfig::for_num_elems(1024)` | ? | ? |

**需要观察的现象**：`#[cuda_module] mod kernels { ... }` 块内部的代码（`thread::index_1d`）是设备端，而块外部的 `main` 函数（`CudaContext::new`、`DeviceBuffer`）是宿主端——它们却写在**同一个文件**里。这就是 u1-l1 讲的「单源编译」在 crate 层面的体现。另外注意 Quick Start 里启动内核的调用现在被包在 `unsafe { ... }` 里——因为 raw `LaunchConfig` 是未经证明的原始数据（详见 u1-l1 / u2-l4）。

**预期结果**：`thread`、`DisjointSlice` 来自 `cuda-device`（设备端）；`CudaContext`、`DeviceBuffer`、`LaunchConfig` 来自 `cuda-core`（宿主端）；`kernels::load` 是 `#[cuda_module]` 宏生成的方法（宿主端调用，加载设备端产物）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cuda-core` 和 `cuda-bindings` 要分成两个 crate，而不是合在一起？

**答案**：分层隔离 unsafe。`cuda-bindings` 是 `bindgen` 生成的原始 FFI（`unsafe`、贴近 C ABI），`cuda-core` 在其之上构建安全的 RAII 类型（`CudaContext`、`DeviceBuffer`）。分开后，普通用户只依赖 `cuda-core`，审计 unsafe 时只需看 `cuda-core` 一层，而不是整个 Driver API 表面。

**练习 2**：一个只装了 Rust 工具链、没装 CUDA Toolkit 的机器，能编译 `cuda-async` 吗？能编译 `cuda-bindings` 吗？

**答案**：能编译 `cuda-async`（它依赖 `cuda-core` 等纯 Rust 逻辑，运行时才 `dlopen` 库）；**不能**顺利编译 `cuda-bindings`，因为它的 `build.rs` 要跑 `bindgen` 读 `cuda.h`，需要 Toolkit 与 clang 的 `stddef.h`。这正是 `cargo oxide doctor` 要提前检查项之一（README 第 211–212 行提到缺 clang 会报 `'stddef.h' file not found`）。

---

### 4.3 编译器 crate 概览表：Rust 变成 PTX 的流水线工位

#### 4.3.1 概念说明

*Compiler Crates* 表列出了把 Rust 编译成 PTX 的核心工位。这张表必须和 u1-l1 的流水线地图对照看——**表里每一个 crate 几乎就是流水线的一个阶段**。

回顾 u1-l1 的流水线（device 路径）：

```
Rust 源码
   │  rustc 前端
   ▼
MIR（rustc 的中级 IR，host/device 共享）
   │  mir-importer：把 MIR 翻译成 Pliron IR
   ▼
dialect-mir（用 Pliron 建模的「Rust MIR 方言」）
   │  mem2reg 等变换（mir-transforms / cuda-oxide-codegen 编排）
   ▼
LLVM dialect（Pliron 里建模的 LLVM 方言）
   │  mir-lower：dialect-mir → LLVM dialect
   │  llvm-export：导出成文本 .ll
   ▼
LLVM IR（.ll 文本）
   │  llc（外部工具）
   ▼
PTX（在 GPU 上运行）
```

每个箭头几乎都对应一个 crate。

#### 4.3.2 核心流程

把 README 的 Compiler 表与流水线阶段做映射：

| 流水线阶段 | 负责 crate | 说明 |
|-----------|-----------|------|
| 自定义后端总入口 | `rustc-codegen-cuda` | 被 rustc 动态加载的后端，编排整条流水线（**不在 workspace**，见 4.4） |
| MIR → Pliron IR | `mir-importer` | 把 rustc 的 MIR 翻译成 `dialect-mir`（翻译完后委托给 `cuda-oxide-codegen` 跑后段） |
| **后段编排（verify/mem2reg/unroll/lower/export）** | **`cuda-oxide-codegen`** | **本轮（#314）新增**：rustc 无关的实验性 PTX 后端，把后段流水线抽成一个可复用编排器，被 `mir-importer` 调用 |
| MIR 方言建模 | `dialect-mir` | 用 Pliron 框架定义「Rust MIR 方言」的操作与类型 |
| MIR → LLVM dialect | `mir-lower` | 把高层的 dialect-mir 降级成 LLVM dialect |
| 导出 .ll 文本 | `llvm-export` | Pliron-LLVM 的 shim，把 LLVM dialect 写成可被 `llc` 消费的文本 `.ll` |
| NVVM intrinsics 建模 | `dialect-nvvm` | 用 Pliron 定义 NVVM 内建函数方言（GPU 特有操作） |

> 注：`mir-transforms`（MIR 级变换，如 mem2reg）与 `nvvm-transforms`（NVVM 级变换）也是流水线的一部分，但它们**不在 README 表里**，属于内部支撑 crate。本轮新增的 `cuda-oxide-codegen` 同样不在 README 表里——它是 experimental 的内部 API，但它在流水线中**编排**了 `mir-transforms`/`mir-lower`/`llvm-export`/`nvvm-transforms` 这几个工位。

#### 4.3.3 源码精读

README 的编译器表：

[README.md:L284-L291](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L284-L291) — 6 个编译器 crate。对照上面的流水线表阅读，你会发现「crate 名 ≈ 它在流水线里的工位」。注意：**这张表里没有 `cuda-oxide-codegen`**，尽管它已经是 workspace 成员。原因是它的公共边界（`experimental` 模块）还没稳定，作者暂时只把它作为内部支撑暴露，而把 `mir-importer` 作为对外的「翻译+驱动」入口。

再看 README 的构建工具表（只有一行，但地位特殊）：

[README.md:L295-L297](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L295-L297) — `cargo-oxide` 是唯一的工具 crate，但它**驱动**了上面所有 crate 的构建与编排。它被 rustc-codegen-cuda 之外的所有东西「调用」来发起一次编译。

新增的 `cuda-oxide-codegen` 把后段工位「打包」的依赖关系，可以直接看它自己的 `[dependencies]`：

[crates/cuda-oxide-codegen/Cargo.toml:L10-L19](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/Cargo.toml#L10-L19) — 它一次性依赖 `dialect-mir`、`dialect-nvvm`、`mir-lower`、`mir-transforms`、`llvm-export`、`nvvm-transforms`、`libnvvm-sys`，外加 `pliron`。也就是说，**整条后段流水线所需的全部 crate 都汇聚在 `cuda-oxide-codegen` 这一个 crate 里**——这正是它能作为「可复用后段编排器」的前提。

一个容易忽略的细节：这些编译器 crate 之间是通过 workspace 的 `[workspace.dependencies]` 用 `path = "..."` 互相引用的（[Cargo.toml:L53-L60](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/Cargo.toml#L53-L60)）。比如 `mir-lower` 依赖 `dialect-mir`，写法是 `dialect-mir = { path = "crates/dialect-mir" }`；新增的 `cuda-oxide-codegen = { path = "crates/cuda-oxide-codegen" }` 也出现在同一片区域。这就是 workspace 的价值：本地 path 引用 + 统一版本，改一个 crate 立刻在所有依赖者身上生效。

#### 4.3.4 代码实践

**实践目标**：把编译器 crate 与流水线阶段一一对应，形成「名字 ↔ 工位」的肌肉记忆，并定位 `cuda-oxide-codegen` 的位置。

**操作步骤**：

1. 在终端运行（不需要 GPU，只看 IR 产物）：

   ```bash
   cargo oxide pipeline vecadd
   ```

   README（[README.md:L116-L117](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L116-L117)）说明它会打印完整流水线：`Rust MIR → dialect-mir → mem2reg → LLVM dialect → LLVM IR → PTX`。

2. 对应到 crate：`MIR → dialect-mir` 这步是 `mir-importer` 干的；`mem2reg` 是 `mir-transforms` 干的（本轮起由 `cuda-oxide-codegen` 编排调用）；`dialect-mir → LLVM dialect` 是 `mir-lower` 干的；`LLVM IR` 文本输出是 `llvm-export` 干的。

**需要观察的现象**：pipeline 输出的每个中间产物（`.mir`、`.ll`、`.ptx`）都能找到一个 crate 对它负责；后段（mem2reg 之后）的编排者现在是 `cuda-oxide-codegen`。

**预期结果**：你能指着 pipeline 的某一阶段，说出「这是 `<某 crate>` 产出的」，并指出 `cuda-oxide-codegen` 是后段的编排者。如果暂时没有 GPU/工具链，则改为「源码阅读型实践」：直接读 [README.md:L284-L291](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L284-L291) 的描述，把每个 crate 名抄到上面的流水线箭头旁——**待本地验证** pipeline 实际产物。

#### 4.3.5 小练习与答案

**练习 1**：`mir-importer` 和 `mir-lower` 都带 `mir`，它们的输入输出分别是什么？

**答案**：`mir-importer` 是**入口**：输入 rustc 的 MIR，输出 `dialect-mir`（Pliron 里的 Rust MIR 方言）；翻译完成后，它把后段（verify/mem2reg/unroll/lower/export）委托给 `cuda-oxide-codegen`。`mir-lower` 是**降级**：输入 `dialect-mir`，输出 LLVM dialect（再经 `llvm-export` 变成 `.ll`）。一个是「把 MIR 请进 Pliron」，一个是「把 Pliron IR 往下推到 LLVM 层」。

**练习 2**：为什么 `dialect-nvvm` 要单独成一个 crate，而不是塞进 `dialect-mir`？

**答案**：两者建模的对象不同。`dialect-mir` 建模的是**通用 Rust MIR 语义**（与 GPU 无关），`dialect-nvvm` 建模的是 **GPU/NVML 特有的内建函数**（如 warp、共享内存、屏障）。分开成独立方言，让通用变换只作用于 `dialect-mir`，GPU 专属逻辑隔离在 `dialect-nvvm`，符合 MLIR/Pliron「分方言、分阶段」的设计哲学。

**练习 3**：既然 `cuda-oxide-codegen` 是「rustc 无关后端」，那 rustc 之外的程序能用它直接把 `dialect-mir` 编译成 PTX 吗？

**答案**：理论上可以——这正是它被抽成独立 crate 的目的。它的公共边界是 `dialect-mir` IR（用 `mark_kernel_entry` 标记入口的 `CodegenModule`），不依赖任何 `rustc_*` 内部 crate，所以可以在普通 nightly 环境里独立编译运行。目前它对外以 `experimental` 模块暴露（v1 实验契约）。`mir-importer` 在 rustc 内部翻译完 MIR 后，复用的就是这同一个后端。具体深潜见 [u6-l6 独立后端 cuda-oxide-codegen](./u6-l6-standalone-codegen-backend.md)。

---

### 4.4 rustc-codegen-cuda 的特殊构建说明：为什么最核心的 crate 反而不在 workspace

#### 4.4.1 概念说明

这是本讲最容易让人困惑、也最能体现项目架构本质的一点：**整条编译流水线的总指挥 `rustc-codegen-cuda`，却不在 workspace 里**。它躺在 `crates/rustc-codegen-cuda/` 目录下，却对根 `Cargo.toml` 的 `members` 列表隐身。

原因在于它**根本不是一个普通的 Rust crate**，而是一个**被 rustc 在编译期动态加载的插件**。普通 crate 是「编译产物 → 被 `use` 或被运行」，而它是「编译成 dylib → rustc 通过入口符号 `__rustc_codegen_backend` 把它 `dlopen` 进来 → 替换掉默认的 LLVM 后端」。

对比一下本轮新增的 `cuda-oxide-codegen`（4.3）：它虽然也叫「后端」，但**不需要** rustc 内部 crate，是个正常 workspace 成员。两者的本质区别就在「要不要 `rustc_private`」。

#### 4.4.2 核心流程

`rustc-codegen-cuda` 的特殊之处有四条，每条都对应一段源码：

1. **必须是 dylib**：rustc 只能 `dlopen` 动态库，所以 `crate-type = ["dylib"]`。
2. **需要 rustc 内部 crate**：它用 `#![feature(rustc_private)]` + `extern crate` 访问 rustc 的内部 API（如 `rustc_middle`、`rustc_codegen_ssa`）。这些 crate **不是 crate.io 上的依赖**，只有「在 rustc 自身环境里编译」时才能解析。
3. **必须从 workspace 隔离**：它在自己 `Cargo.toml` 里写了一个**空的 `[workspace]`**，把自己从父 workspace 摘出去。因为父 workspace 用普通依赖解析，无法提供 rustc 内部 crate。
4. **手动同步版本**：因为被隔离，它**不能**用 `{ workspace = true }`，必须手写 `pliron` 的 git+rev，且 rev 必须**与根 Cargo.toml 完全一致**，否则 pliron 会解析成两个不同的 crate。

#### 4.4.3 源码精读

先看它自己的 `Cargo.toml` 头部，三条关键证据挨在一起：

[crates/rustc-codegen-cuda/Cargo.toml:L10-L15](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/Cargo.toml#L10-L15) — 中文逐行解读：

- 第 10–12 行：`# IMPORTANT: Must be dylib ...` + `[lib]` + `crate-type = ["dylib"]`。**这是它作为插件的物理形态**。
- 第 14–15 行：`# Keep out of workspace - requires special nightly build` 紧跟一个**空的 `[workspace]`**。在 Cargo 里，子目录写 `[workspace]`（哪怕下面没成员）意味着「我自成一个新的 workspace 根」，从而**主动退出**父 workspace。这就是它从点名册消失的机制。

再看它如何手动同步 pliron（与 4.1.3 里根 Cargo.toml 的警告呼应）：

[crates/rustc-codegen-cuda/Cargo.toml:L28-L33](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/Cargo.toml#L28-L33) — 注释直说：「This crate is isolated from the main workspace, so we cannot use `{ workspace = true }`. Keep these versions synchronized with root Cargo.toml」。下面手写的 `pliron = { git = "...", rev = "222dd962a124cba8ec5f119ae6e0ecf202630854" }` 与根 [Cargo.toml:L49](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/Cargo.toml#L49) 的 rev **逐字符相同**。这不是巧合，而是硬约束：一旦两边 rev 不一致，`mir-importer`（在 workspace 内）和 `rustc-codegen-cuda`（在 workspace 外）会各自拉到一份 pliron，类型不再 unify，编译直接失败。

它访问 rustc 内部 crate 的方式也别具一格——**不写进 `[dependencies]`**：

[crates/rustc-codegen-cuda/Cargo.toml:L39-L41](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/Cargo.toml#L39-L41) — 注释：「rustc internal crates are NOT listed here. They're accessed via `extern crate` with `#![feature(rustc_private)]`」。也就是说 `rustc_middle`、`rustc_codegen_ssa` 这些**不在 Cargo.toml 里**，而是在源码里靠 nightly feature `rustc_private` 直接 `extern crate`。这正是它「需要特殊的 nightly 构建」的根源，也是它必须被隔离的根本原因。

最后补一个周边事实：`cargo-oxide` 虽然在 workspace 里，但它也有自己的「刻意设计」——它故意不依赖任何会拉入 CUDA Toolkit 的 crate：

[crates/cargo-oxide/Cargo.toml:L14-L18](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/Cargo.toml#L14-L18) — 注释写「Deliberately CUDA-free」，目的是让 `cargo oxide doctor` 能在**没装 Toolkit、没装驱动**的机器上构建运行。这是与 `libnvvm-sys`/`nvjitlink-sys`（运行时 dlopen）同样的设计哲学：把「CUDA 在场」这件事尽量推迟到运行时。

而 `cargo oxide` 这个子命令本身，是通过 cargo 别名生效的：

[.cargo/config.toml:L3-L4](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/.cargo/config.toml#L3-L4) — `oxide = "run --package cargo-oxide --"`。cargo 会把 `cargo oxide xxx` 翻译成 `cargo run --package cargo-oxide -- xxx`，这就是为什么在仓库内 `cargo oxide` 开箱即用。

#### 4.4.4 代码实践

**实践目标**：亲眼确认「隔离 + dylib + 手动同步」这三件事。

**操作步骤**：

1. 打开 `crates/rustc-codegen-cuda/Cargo.toml`。
2. 找到 `[lib]` 段，确认 `crate-type` 的值，并解释为什么是它（而不是 `rlib`/`bin`）。
3. 找到那个空的 `[workspace]`，理解它「主动退出父 workspace」的语义。
4. 把它里面的 `pliron` rev（约第 32 行）与根 `Cargo.toml` 第 49 行的 rev **逐字符对比**，确认完全一致。
5. 确认 `[dependencies]` 里**没有** `rustc_middle`/`rustc_codegen_ssa`，然后到源码里 `grep` `extern crate rustc_` 验证它们是用 `extern crate` 拉进来的。

**需要观察的现象**：这是一个用 `path` 依赖（`mir-importer = { path = "../mir-importer" }`）引用 workspace 内兄弟 crate、却又不属于 workspace 的「半连体」crate。注意它没有直接 `path` 依赖 `cuda-oxide-codegen`——它通过 `mir-importer` 间接复用那个独立后段后端。

**预期结果**：你能在 30 秒内向别人讲清「为什么最核心的 crate 反而不在 workspace」——因为它是被 rustc `dlopen` 的 dylib 插件，需要 `rustc_private` 内部 crate，只能用独立的 nightly 环境构建，所以主动用空 `[workspace]` 退出父 workspace。**待本地验证**：第 5 步的 `grep` 结果（预期会看到若干 `extern crate rustc_*`）。

#### 4.4.5 小练习与答案

**练习 1**：如果有人不小心把 `rustc-codegen-cuda/Cargo.toml` 里的 pliron rev 改成另一个 commit，会发生什么？

**答案**：cargo 会把 pliron 解析成**两个不同的 crate**——workspace 内的 crate（如 `mir-importer`、`cuda-oxide-codegen`）用的是根 Cargo.toml 的 rev，而 `rustc-codegen-cuda` 用的是新 rev。两者定义的同名类型在 Rust 看来**不兼容**，类型无法 unify，编译失败。这正是 [Cargo.toml:L47-L48](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/Cargo.toml#L47-L48) 注释警告的内容。

**练习 2**：为什么 `cargo-oxide` 要「故意不依赖 CUDA」？

**答案**：为了让 `cargo oxide doctor`（环境自检）能在**裸机**（无 CUDA Toolkit、无驱动）上构建并运行，先帮用户诊断缺什么，而不是一编译就因为缺 Toolkit 失败。这把「诊断」和「真正编译」解耦——诊断门槛极低，编译门槛按需提高。`libnvvm-sys`/`nvjitlink-sys` 用 `dlopen` 是同一思路的另一个体现。

**练习 3**：`crate-type = ["dylib"]` 如果改成 `["rlib"]`，会怎样？

**答案**：rustc 加载 codegen 后端的方式是 `dlopen` 一个**动态库**并查找 `__rustc_codegen_backend` 符号。`rlib` 是静态档案（供 `use` 链接），不是可 `dlopen` 的动态库，rustc 无法加载它，后端也就无法被识别。所以 dylib 是硬性要求，不是风格选择。

---

## 5. 综合实践

把本讲四个模块串起来，画出一张**完整的 crate 分层依赖草图**。这是本讲的交付物，也是后续 u2/u3/u4 的导航图。

**任务**：在一张图（纸笔或任意画图工具）上画出四层，把全部 **20 个 workspace 成员 + 1 个编外成员（rustc-codegen-cuda）** 全部归位，并标注每个 crate 的「执行位置」（设备端 / 宿主端 / 编译期 / 工具）。本轮记得把新增的 `cuda-oxide-codegen` 放进编译器层、并标出它被 `mir-importer` 调用的位置。

建议的画法：

```
┌─ 工具层 ─────────────────────────────────────────────┐
│  cargo-oxide（宿主/工具，刻意无 CUDA 依赖）            │
└──────────────────────────────────────────────────────┘
          │ 驱动
          ▼
┌─ 编译器层（device 路径流水线）──────────────────────┐
│  rustc-codegen-cuda（★编外★，dylib 插件，rustc_private）│
│   调用 → mir-importer（MIR→dialect-mir 翻译）          │
│          └─ 后段委托 → cuda-oxide-codegen（★本轮新增★  │
│              rustc 无关后端，编排 verify/mem2reg/unroll │
│              /lower/export，复用 mir-lower 等）        │
│                → dialect-mir → mir-transforms          │
│                → mir-lower → LLVM dialect              │
│                → llvm-export(.ll) → llc → PTX          │
│   方言建模：dialect-mir、dialect-nvvm                  │
│   辅助：nvvm-transforms                                │
└──────────────────────────────────────────────────────┘
          │ 产出 PTX，内嵌进制品
          ▼
┌─ 用户面·宿主端 ──────────────────────────────────────┐
│  cuda-core（安全 RAII）  cuda-async（异步执行）       │
│  cuda-host（模块加载）   cuda-macros（过程宏，编译期） │
│  cuda-bindings（unsafe FFI，需 Toolkit）              │
│  libnvvm-sys / nvjitlink-sys（运行时 dlopen）         │
└──────────────────────────────────────────────────────┘
          │ 加载并启动
          ▼
┌─ 用户面·设备端（编译进 PTX，在 GPU 运行）─────────────┐
│  cuda-device（thread::*, warp::*, 共享内存, 屏障…）    │
└──────────────────────────────────────────────────────┘

横切支撑（不属某一层，被多层共用）：
  oxide-artifacts（制品格式：后端写、宿主读）
  reserved-oxide-symbols（符号命名契约：宏↔后端↔加载）
  fuzzer（差分模糊测试）
```

**验收清单**（逐条自检）：

- [ ] `cargo-oxide` 在工具层，且能说出它「故意无 CUDA 依赖」的原因。
- [ ] `rustc-codegen-cuda` 单独标注为「编外」，能说出 dylib + rustc_private + 空 `[workspace]` 三条理由。
- [ ] 编译器层的 crate 顺序大致符合 u1-l1 的流水线（mir-importer → cuda-oxide-codegen → mir-lower → llvm-export），且 `cuda-oxide-codegen` 被画成 `mir-importer` 的后段委托对象。
- [ ] `cuda-device` 在设备端，`cuda-core`/`cuda-host`/`cuda-async` 在宿主端，没有搞混。
- [ ] `oxide-artifacts`、`reserved-oxide-symbols`、`fuzzer`、`cuda-oxide-codegen`、`mir-transforms`、`nvvm-transforms` 被识别为「内部支撑 / 方言建模」，不在 README 的三张表里。
- [ ] 能指出 `cuda-bindings` 是唯一编译期就需要 CUDA Toolkit 的用户面 crate。

画完后，把它保存下来——u2（设备端）、u3（宿主端）、u4（编译流水线）都会从这张图的某一层切入深挖。

## 6. 本讲小结

- cuda-oxide 的 workspace 有 **20 个成员**，按 `Cargo.toml` 的注释分成 Core(13，本轮新增 `cuda-oxide-codegen`) / FFI bindings(5) / 内部命名契约(1) / Fuzzer(1) 四组；README 的 *Crate Overview* 只展示了其中面向用户的一部分。
- 用户面 crate 内部要区分**设备端**（`cuda-device`，编译进 PTX）与**宿主端**（`cuda-core`/`cuda-host`/`cuda-async`/`cuda-bindings` 等，运行在 CPU）；`cuda-macros` 是编译期过程宏。
- 编译器 crate 几乎「一个 crate 对应流水线一个工位」：`mir-importer`(MIR→dialect-mir) → `cuda-oxide-codegen`(本轮新增的后段编排器) → `mir-lower`(→LLVM dialect) → `llvm-export`(→.ll)，`dialect-mir`/`dialect-nvvm` 负责方言建模。
- **本轮新增的 `cuda-oxide-codegen`** 是一个 rustc 无关的实验性 PTX 后端：它把 verify/mem2reg/unroll/lower/export 抽成一个可复用编排器，被 `mir-importer` 调用，将来也能脱离 rustc 独立使用；目前以 `experimental` 内部 API 暴露，所以未进 README 表。
- **最核心的 `rustc-codegen-cuda` 反而编外**：它是被 rustc `dlopen` 的 dylib 插件，靠 `#![feature(rustc_private)]` 访问 rustc 内部 crate，必须用空 `[workspace]` 主动退出父 workspace，并手动同步 pliron 的 rev；这与「在 workspace 里、能用 `{ workspace = true }`」的 `cuda-oxide-codegen` 形成对比。
- 设计哲学是「把 CUDA 在场推迟到运行时」：`cargo-oxide`、`libnvvm-sys`、`nvjitlink-sys` 都尽量不在编译期硬依赖 CUDA Toolkit（`dlopen` 或纯逻辑），只有 `cuda-bindings` 因 `bindgen` 读 `cuda.h` 而必须编译期有 Toolkit。

## 7. 下一步学习建议

本讲建立了「地图」，接下来按层深挖：

- **想先会跑** → [u1-l3 工具链与 cargo-oxide 驱动](./u1-l3-toolchain-and-cargo-oxide.md) 和 [u1-l4 Hello GPU：vecadd 端到端](./u1-l4-hello-gpu-vecadd.md)，把工具层和一次完整编译跑通。
- **想写 GPU 内核** → 进入 u2，从 [u2-l1 `#[kernel]` 与 `#[cuda_module]` 宏](./u2-l1-kernel-and-cuda-module-macros.md) 开始，它会用到本讲的 `cuda-macros`（用户面·编译期）和 `reserved-oxide-symbols`（横切支撑），你会看到命名契约如何把宏和后端绑死。
- **想懂宿主运行时** → 进入 u3，从 `cuda-core`/`cuda-host`/`cuda-async` 三个宿主端 crate 切入。
- **想懂编译原理** → 进入 u4，届时请重看本讲 4.3 的流水线表，它会成为 u4 的目录；想专门深潜本轮新增的独立后端，直接看 [u6-l6 独立后端 cuda-oxide-codegen](./u6-l6-standalone-codegen-backend.md)。

阅读建议：在进入任何一层的讲义前，先回头确认本讲 §5 的综合实践草图——它是整本手册的导航图。
