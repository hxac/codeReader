# 构建环境准备与特性开关：编译 deepseek-v2-lite

## 1. 本讲目标

上一讲（u1-l1）我们从架构图上认识了 PegaInfer，并知道 dsv2-lite 藏在一个叫 `deepseek-v2-lite` 的编译期特性开关后面。本讲解决的是「把它编译出来」这件事本身。读完本讲，你应该能：

1. 说出 `cargo run/build --release --features deepseek-v2-lite` 这条命令在 Cargo 工作区里逐层触发了什么（特性接力链）。
2. 理解为什么必须加 `--release`，以及为什么工作区根目录没有 `build.rs`，所有 CUDA 编译都归 `pegainfer-kernels/build.rs` 管。
3. 讲清 `pegainfer-kernels/build.rs` 的 nvcc 编译流水线：SM 目标如何探测、`csrc/` 里的 `.cu` 文件如何按特性被筛选、对象如何并行编译并归档链接。
4. 会配置 `PEGAINFER_CUDA_SM`、`PEGAINFER_BUILD_TIMING`、`PEGAINFER_NVCC_JOBS` 等环境变量，并且知道 `PEGAINFER_NCCL_ROOT` 只在 `moe`（DeepEP shim）特性下才被读取——这是一个容易误解的关键点。
5. 会用 `docker/Dockerfile.dev` 和 `docker/dev.sh` 搭一个开箱即用的 GPU 开发环境。

## 2. 前置知识

本讲会用到以下概念，先用一段话讲清直觉，细节后面结合源码展开：

- **Cargo 工作区（workspace）与虚拟清单（virtual manifest）**：多个 crate 共用一个 `Cargo.toml` 管依赖版本和输出目录。如果根 `Cargo.toml` 只有 `[workspace]` 节、没有 `[package]` 节，它就叫虚拟清单——根目录本身不是一个 crate，也就不可能有根级 `build.rs`。PegaInfer 正是这种结构。
- **feature（特性开关）**：Cargo 的编译期开关。`--features xxx` 打开后，代码里 `#[cfg(feature = "xxx")]` 标注的模块才会参与编译。feature 还可以「接力」：crate A 的 feature 可以声明「启用 crate B 的某个 feature」，于是一个开关从顶层一路传到底层。
- **可选依赖（optional dependency）**：写成 `pegainfer-kernels = { workspace = true, optional = true }` 的依赖默认不会被编译，只有某个 feature 通过 `dep:pegainfer-kernels` 把它打开时才进入构建图。dsv2-lite 正是用这个手法让「不开特性就完全不编译模型 crate」。
- **build.rs（构建脚本）**：crate 根目录下名为 `build.rs` 的 Rust 程序，cargo 在编译该 crate 之前先编译并运行它，用它生成代码、调用外部编译器（这里是 nvcc）、输出 `cargo:rustc-link-lib=...` 之类的链接指令。
- **SM / compute capability（计算能力）**：NVIDIA GPU 的架构版本号，如 `80`（A100）、`89`（4090）、`90`（H100）、`120`（Blackwell 消费卡）。nvcc 用 `-gencode arch=compute_XX,code=sm_XX` 为指定架构生成 GPU 机器码（SASS）。架构不对，编译可能失败或运行时内核无法加载。
- **NCCL 与 DeepEP shim**：NCCL 是 NVIDIA 的集合通信库（all-reduce、all-to-all 等），多卡推理离不开它。仓库里的「DeepEP shim」是 `csrc/deepep/` 下对 DeepEP 专家并行通信的封装，属于 `moe` 特性（kimi-k2、glm52 使用），构建期需要一份 ≥ 2.30.4 的 NCCL 安装（头文件 + 库）。而 dsv2-lite 自己的 2 卡 EP 走的是 cudarc 的 NCCL Rust 绑定，运行时动态加载，构建期不需要 NCCL——这个区分本讲会反复强调。
- **Docker 开发容器**：把 CUDA 工具链、Rust nightly、NCCL、Triton 等版本全部钉死的镜像，避免「每台机器装一遍环境」的漂移问题。

如果这些概念里有陌生的，不必担心，下面每一节都会从直觉讲起再落到源码。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L1-L27)（工作区根） | 虚拟清单：`[workspace]`、成员列表、共享依赖版本 | `default-members` 决定了根目录 `cargo run/build` 到底在构建谁 |
| [pegainfer-server/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-server/Cargo.toml#L34-L48) | 服务端二进制 crate（`pegainfer`）的清单 | `deepseek-v2-lite` feature 的第一跳 |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L10-L15)（本 crate） | dsv2-lite 模型 crate 清单 | feature 第二跳 + 可选依赖 + `required-features` 门 |
| [pegainfer-kernels/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/Cargo.toml#L31-L50) | 内核 crate 清单 | `deepseek-v2-lite = []` 这个「空 feature」的含义；`moe` 与它的关系 |
| [pegainfer-kernels/build.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2206-L2230) | 全仓库唯一的 CUDA 编译入口 | SM 探测、特性门控的 `.cu` 筛选、并行 nvcc、归档链接 |
| [pegainfer-kernels/src/ops.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ops.rs#L6-L21) / [ffi.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi.rs#L9-L10) | 内核 crate 的 Rust 侧导出 | 特性接力的「最后一棒」：`#[cfg]` 门控的模块 |
| [nccl_backend.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/nccl_backend.rs#L29-L32)（本 crate） | dsv2-lite 自己的 NCCL 后端 | 证明 dsv2-lite 的 EP 用 cudarc 绑定，而非 DeepEP shim |
| [docker/Dockerfile.dev](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L1-L60) | 开发镜像定义 | NCCL 钉位、环境变量预置、Rust nightly 安装 |
| [docker/dev.sh](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/dev.sh#L1-L59) | 容器包装脚本 | build/shell/run 三种用法、缓存挂载、SM 转发 |
| [docker/README.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/README.md#L1-L34) | 容器使用说明 | 官方推荐的命令样本 |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L21-L46) | 仓库协作指南 | `--release` 铁律、环境变量表、构建体系描述 |

## 4. 核心概念与源码讲解

### 4.1 工作区与特性开关机制

#### 4.1.1 概念说明

PegaInfer 是一个「一工作区、多模型」的引擎：Qwen3、dsv2-lite、Kimi-K2……每个模型一个 crate，再加上 frontend、core、kernels 等公共 crate。为什么不把所有模型编进一个二进制？因为每个模型都拖着专属的 CUDA 内核和依赖（Kimi-K2 要 Marlin INT4，GLM5.2 要 FP8），全编进去既慢又要求构建机装齐所有第三方库。所以仓库的策略是：**每条模型线一个 cargo feature，默认只开 qwen3**——[CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L9) 一句话说清：「Every model line is behind a cargo feature; only `qwen3` is a default feature, so the stock build is pure Rust + CUDA with no Python.」

理解本讲的钥匙是「**特性接力链**」：`--features deepseek-v2-lite` 是对 `pegainfer-server` 说的，server 的 feature 打开 dsv2-lite crate 的同名 feature，后者再打开 kernels crate 的同名 feature。三跳全部走完，`csrc/deepseek_v2_lite/` 下的 CUDA 源和 Rust 侧的 dsv2 模块才会进入编译。少任何一跳，链就断了。

#### 4.1.2 核心流程

先看根命令为什么「打得到」server。根 `Cargo.toml` 是虚拟清单，`default-members` 把裸 `cargo build/run` 指向 `pegainfer-server`：

```text
cargo run --release --features deepseek-v2-lite -- --model-path models/DeepSeek-V2-Lite
   │
   │  default-members = ["pegainfer-server"]  → 实际构建的是 pegainfer-server 的 pegainfer 二进制
   │
   ├─ 第 1 跳：pegainfer-server 的 feature "deepseek-v2-lite"
   │     = ["dep:pegainfer-deepseek-v2-lite",            ← 激活可选依赖，模型 crate 进构建图
   │        "pegainfer-deepseek-v2-lite/deepseek-v2-lite"] ← 并转发同名 feature
   │
   ├─ 第 2 跳：pegainfer-deepseek-v2-lite 的 feature "deepseek-v2-lite"
   │     = ["dep:pegainfer-kernels",                       ← kernels 由可选变必需
   │        "pegainfer-kernels/deepseek-v2-lite"]          ← 继续向下转发
   │
   └─ 第 3 跳：pegainfer-kernels 的 feature "deepseek-v2-lite"（空列表）
         → build.rs 里 cfg!(feature = "deepseek-v2-lite") 为真
            → csrc/deepseek_v2_lite/*.cu 参与编译
         → src/ffi.rs、src/ops.rs 里 #[cfg(feature = "deepseek-v2-lite")] 模块导出
```

这个设计还有两个直接后果：

1. **不开特性时 dsv2-lite crate 依然能通过 `cargo test --release --workspace --lib`**：因为它的 `default = []` 且 kernels 是 optional，纯逻辑单测（约 9 秒、无需 GPU，见 [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L63-L66)）在不开特性时也要能编译。
2. **需要 GPU + 权重的集成测试用 `required-features` 上锁**：`tests/e2e_ep2.rs` 只有开特性才会被编译成测试目标，避免 stock 构建被「找不到内核符号」卡死。

#### 4.1.3 源码精读

**根清单：虚拟工作区 + default-members。**
[Cargo.toml:L1-L2](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L1-L2) 声明 `[workspace]` 并把默认成员设为 `pegainfer-server`；[L3-L26](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L3-L26) 列出全部成员（dsv2-lite 在 [L12](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L12)）。注意根清单里没有 `[package]`——所以「虚拟工作区根没有构建脚本」是字面事实，CUDA 编译必须另找主人（4.2 节）。

**第一跳：server 的 feature 定义。**
[pegainfer-server/Cargo.toml:L34-L42](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-server/Cargo.toml#L34-L42)：

```toml
[features]
# Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time.
default = ["qwen3"]
qwen3 = ["dep:pegainfer-qwen3"]
...
deepseek-v2-lite = [
  "dep:pegainfer-deepseek-v2-lite",
  "pegainfer-deepseek-v2-lite/deepseek-v2-lite",
]
```

`dep:pegainfer-deepseek-v2-lite` 打开的是 [L18](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-server/Cargo.toml#L18) 里声明的 `optional = true` 依赖。注意 `--features deepseek-v2-lite` 不会关闭 `default`，所以 qwen3 仍被编译——两种模型可以共存于同一个二进制。

**第二跳：模型 crate 的 feature 与 required-features。**
[pegainfer-deepseek-v2-lite/Cargo.toml:L10-L15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L10-L15)：

```toml
[features]
deepseek-v2-lite = [
  "dep:pegainfer-kernels",
  "pegainfer-kernels/deepseek-v2-lite",
]
default = []
```

其中 `pegainfer-kernels` 在 [L28](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L28) 是 `optional = true`。再看两把「特性锁」：[L42-L45](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L42-L45) 给集成测试 `e2e_ep2` 上锁，[L47-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L47-L50) 给归因分析工具二进制 `dsv2_lite_ep2_decode_attribution` 上锁——没开特性时这两个目标根本不编译。

**第三跳：kernels 的「空 feature」。**
[pegainfer-kernels/Cargo.toml:L31-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/Cargo.toml#L31-L50)：

```toml
[features]
default = []
...
deepseek-v2-lite = []          # 空 feature：不引别的 feature，只当 cfg 开关
...
glm52 = ["moe"]
kimi-k2 = ["moe"]
moe = []                       # DeepEP/DeepGEMM/FlashMLA 第三方底座
```

`deepseek-v2-lite = []` 是个空列表——它的全部作用就是让 build.rs 和 `#[cfg]` 能探测到「开关已打开」。注意 dsv2-lite **不启用 `moe`**：`moe` 是 DeepEP shim（kimi-k2 / glm52 的第三方底座），两者是平行关系，这一点在 4.3 节还会用到。

**最后一棒：Rust 侧的 cfg 门。**
[pegainfer-kernels/src/ffi.rs:L9-L10](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi.rs#L9-L10) 与 [L31-L32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi.rs#L31-L32) 把 dsv2 的 FFI 符号表整体挂到 `#[cfg(feature = "deepseek-v2-lite")]` 下；[pegainfer-kernels/src/ops.rs:L10-L21](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ops.rs#L10-L21) 同样门控 `dsv2_routed_moe` 模块及其导出（如 `Dsv2RoutedMoeScratch`、`dsv2_lite_route_logits_into`）。本 crate 的 [nccl_backend.rs:L40-L41](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/nccl_backend.rs#L40-L41) 就消费了其中的 `dsv2_lite_accumulate_fixed_expert_into` 等 FFI 函数——特性不开，这些符号就不存在。

#### 4.1.4 代码实践

**实践目标**：亲眼看到特性接力链，而不只是相信上面的文字。

**操作步骤**：

1. 在仓库根目录（有依赖缓存的环境，例如 dev 容器；否则 cargo 会先拉取 git 依赖）执行：

   ```bash
   cargo tree -e features -p pegainfer-server --features deepseek-v2-lite \
     | grep -E "deepseek|kernels" | head -40
   ```

2. 依次打开三份清单文件，为接力链的每一跳找到对应行号：
   - [pegainfer-server/Cargo.toml:L39-L42](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-server/Cargo.toml#L39-L42)
   - [pegainfer-deepseek-v2-lite/Cargo.toml:L11-L14](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L11-L14)
   - [pegainfer-kernels/Cargo.toml:L36](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/Cargo.toml#L36)
3. 对照执行 `cargo tree -e features -p pegainfer-server`（不带 `--features`），观察差异。

**需要观察的现象**：带特性时，依赖树里应出现 `pegainfer-deepseek-v2-lite` 节点，且 `pegainfer-kernels` 节点上标注了 feature `"deepseek-v2-lite"`；不带特性时，`pegainfer-deepseek-v2-lite` 整个消失（server 依赖是 optional，未被激活）。

**预期结果**：三条清单行号串成一条链，`cargo tree` 的输出与之一一对应。具体输出文本待本地验证（依赖 git 仓库是否已缓存会影响前几行，但 feature 标注部分不受影响）。

#### 4.1.5 小练习与答案

**练习 1**：在根目录执行 `cargo build --release`，构建的是哪些 crate？dsv2-lite 会被编译吗？

答案：构建 `default-members` 指向的 `pegainfer-server`（见 [根 Cargo.toml:L2](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L2)）及其依赖。由于 server 的 `default = ["qwen3"]`，编译的是 stock 形态：qwen3 + frontend + core + kernels 等。dsv2-lite 不会被编译——它在 server 里是 optional 依赖，只有 `deepseek-v2-lite` feature 才激活它。

**练习 2**：为什么 `pegainfer-deepseek-v2-lite` 的 `default` 必须是空的 `[]`？

答案：因为 `cargo test --release --workspace --lib`（[CLAUDE.md:L63-L65](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L63-L65)）会无特性地构建所有成员。如果 `default` 里带了 kernels 相关 feature，纯 CPU 环境跑单测也会触发 CUDA 编译与 SM 探测（4.2 节会看到探测失败直接 panic），单测就不再「约 9 秒、无需 GPU」了。

**练习 3**：如果只执行 `cargo build --release -p pegainfer-deepseek-v2-lite`（不带 `--features`），能成功吗？为什么？

答案：能。此时该 crate 以 `default = []` 编译：kernels 是 optional 依赖不参与编译，凡引用 kernels 的模块在源码里也必然有 `#[cfg(feature = ...)]` 保护，否则 stock 单测早就失败了。但 `e2e_ep2` 测试和归因二进制不会产出——它们带 `required-features = ["deepseek-v2-lite"]`（[Cargo.toml:L42-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L42-L50)）。

### 4.2 build.rs 的 nvcc 与 SM 探测流程

#### 4.2.1 概念说明

整个工作区只有一份会调用 nvcc 的构建脚本：[pegainfer-kernels/build.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2206-L2230)（[CLAUDE.md:L109-L111](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L109-L111) 的「Build system」三条正是它的摘要）。为什么这样设计？因为「编译哪些 `.cu`、用什么架构、链哪些库」必须**一处决策**：如果每个模型 crate 各自为政，同一份 `csrc/` 会被编译多次，链接配置也可能互相冲突。集中到 kernels 后，模型 crate 只通过 FFI 声明「我要哪些符号」，构建全归 build.rs 管。

这个脚本解决三件事：

1. **编什么**：递归收集 `csrc/` 下所有 `.cu`，再按当前特性集合做减法——每个模型专属目录（`deepseek_v2_lite/`、`kimi_k2/`、`glm52/`、`qwen35/`……）没开对应特性就剔除。
2. **为什么架构编**：探测本机 GPU 的 SM（或读环境变量覆盖），生成 nvcc `-gencode` 参数。
3. **怎么收尾**：并行跑 nvcc，把所有 `.o` 用 `ar` 归档成 `libkernels_cuda.a`，输出静态链接与 CUDA 运行库的链接指令。

#### 4.2.2 核心流程

把 [main()](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2206-L2230) 的执行过程提炼成伪代码：

```text
main():
  ensure_git_submodules_initialized()            # DeepEP/DeepGEMM/FlashMLA 等 submodule 缺失则自动 init
  toolkit = pegainfer_build::CudaToolkit::discover()   # 找 CUDA 工具链（nvcc、include、lib64）
  sm_targets = detect_sm_targets()               # ① SM 目标：环境变量 > nvidia-smi > panic
  nvcc_sm_targets = normalize_nvcc_sms(...)      # ② 对照 nvcc 支持的 arch 列表做规整（如 120→120f）
  arch_args = nvcc_arch_args(nvcc_sm_targets)    # ③ 生成 -gencode 参数（外加最高档的 compute_ PTX）
  读取所有特性布尔值                                 # deepseek_v2_lite_enabled / moe_enabled / ...

  cu_files = 递归收集 csrc/ 下的文件，逐个过滤：
      未开 deepseek-v2-lite 且路径含 deepseek_v2_lite/ → 剔除   # ④ 特性门控
      未开 moe 且路径含 deepep/               → 剔除
      （kimi_k2/ glm52/ gemma4/ qwen35/ qwen4/ k3/ 同理）
      扩展名不是 .cu，或属于已退役的三个 TU       → 剔除

  为每个 .cu 组装 nvcc 参数（-O3 -fPIC + arch_args + 各文件专属 include/宏）
  按 PEGAINFER_NVCC_JOBS（默认 CPU 数）开线程并行执行 nvcc     # ⑤ 每个任务被 time_phase 计时
  ar rcs libkernels_cuda.a *.o                   # ⑥ 归档
  输出 cargo:rustc-link-lib=static=kernels_cuda、cudart、cublas、cublasLt、cuda、nvrtc
  （仅 moe 特性：校验并链接 PEGAINFER_NCCL_ROOT 下的 NCCL）
```

关于「探测失败就 panic」：这不是缺陷而是刻意为之——编译出不匹配架构的内核，问题会在运行时以更难排查的方式爆发（内核加载失败或跑错路径），不如在构建期就大声失败。

#### 4.2.3 源码精读

**SM 探测三级瀑布。** [detect_sm_targets（L435-L465）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L435-L465)：先读 `PEGAINFER_CUDA_SM`（兼容旧名 `CUDA_SM`），按逗号拆分并用 [parse_sm_token（L319-L352）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L319-L352) 容错解析（接受 `sm_120`、`compute_12.0`、`120a` 等写法）；失败则调 [sm_targets_from_nvidia_smi（L410-L433）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L410-L433) 执行 `nvidia-smi --query-gpu=compute_cap`；再失败打印警告并 `panic!("GPU detection failed")`。这就是容器里看不到 GPU 时必须设 `PEGAINFER_CUDA_SM` 的原因。

**架构规整与 -gencode。** [normalize_nvcc_sm（L383-L400）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L383-L400) 会用 nvcc 自己的 `--list-gpu-arch` 输出（[L367-L381](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L367-L381)）把 `120` 升级成家族后缀 `120f`（前提是 nvcc 支持）。[nvcc_arch_args（L467-L483）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L467-L483) 为每个目标发一条 `arch=compute_X,code=sm_X`，并额外附上最高目标的 `code=compute_X`——PTX 字节码留给未来驱动 JIT，老二进制遇到新卡时不至于直接跑不起来。

**特性门控的 `.cu` 筛选（本讲主角）。** 收集逻辑在 [L2263-L2312](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2263-L2312)，dsv2 的那一行在 [L2270-L2272](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2270-L2272)：未开特性且文件位于 `csrc/deepseek_v2_lite/` 目录下则直接丢弃。「位于该目录」的判定函数是 [is_deepseek_v2_lite_source（L708-L715）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L708-L715)——只看路径组件里有没有 `deepseek_v2_lite`。这个目录下现有两个 TU：`dsv2_lite_moe.cu` 与 `dsv2_lite_attention.cu`，只有开了特性它们才会被 nvcc 编进 `libkernels_cuda.a`。另外 [L2248-L2249](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2248-L2249) 的 `replaced_cuda_files`（`activation.cu`、`embedding.cu`、`fused_attention.cu`）是已退役的旧 TU，即使存在也不编译（[L2304-L2310](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2304-L2310)）。

**并行编译与归档。** [L2794-L2841](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2794-L2841) 用 `thread::scope` + Mutex 队列开 `nvcc_jobs` 个工作线程抢任务，每个 nvcc 调用都被 `time_phase(format!("nvcc {}", ...))` 包住（开着 `PEGAINFER_BUILD_TIMING` 时打印 `cargo:warning=build-timing nvcc ... N.NNNs`）；任务顺序还按 [nvcc_task_priority（L304-L317）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L304-L317) 重排，让最重的 `paged_attention` 系列先入队缩短总耗时。随后 [L2847-L2863](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2847-L2863) 用 `ar rcs` 打包出 `libkernels_cuda.a`；[L2882-L2899](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2882-L2899) 输出静态库与 `cudart`/`cublas`/`cublasLt`/`cuda`/`nvrtc` 的链接指令。

**缓存失效声明。** [L2907-L2914](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2907-L2914) 用 `cargo:rerun-if-env-changed` 盯住 `PEGAINFER_CUDA_SM`、`PEGAINFER_BUILD_TIMING`、`PEGAINFER_NVCC_JOBS`、`PEGAINFER_NCCL_ROOT` 等——改这些环境变量会自动重跑构建脚本，不需要手动 `cargo clean`。

#### 4.2.4 代码实践

**实践目标**：把「SM 探测 → -gencode」这条链从源码推演到日志验证。

**操作步骤**：

1. 纸面推演：设 `PEGAINFER_CUDA_SM=120,80`。查 [parse_sm_token](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L319-L352)，两个 token 解析为 `120` 与 `80`；查 [normalize_nvcc_sm](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L383-L400)，若 nvcc 支持 `compute_120f` 则 `120` 变 `120f`；最后按 [nvcc_arch_args](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L467-L483) 手写出全部 `-gencode` 参数和那条额外的 PTX。
2. 在有 CUDA 工具链的机器上（无 GPU 也可以，这正是 `PEGAINFER_CUDA_SM` 的用途）执行：

   ```bash
   PEGAINFER_CUDA_SM=120,80 PEGAINFER_BUILD_TIMING=1 \
     cargo build --release -p pegainfer-kernels --features deepseek-v2-lite
   ```

3. 在构建输出里找三行 warning：`Detected CUDA SM targets: ...`、`Compiling CUDA kernels for nvcc targets: ...`（[L2231-L2246](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2231-L2246) 打印）以及 `build-timing nvcc .../dsv2_lite_moe.cu ...`。
4. 把第 2 步换成不带 `--features deepseek-v2-lite` 再跑一次，对比 build-timing 列表。

**需要观察的现象**：带特性时出现 `nvcc .../csrc/deepseek_v2_lite/dsv2_lite_moe.cu` 与 `dsv2_lite_attention.cu` 两条计时行；不带特性时这两行消失，其余 TU 计时行基本不变。

**预期结果**：日志中的 `nvcc targets` 与第 1 步的手写推演一致（例如出现 `sm_120f`）。具体数值待本地验证（取决于本机 nvcc 支持的 arch 列表）。

#### 4.2.5 小练习与答案

**练习 1**：构建机装了 CUDA 工具链但没插 GPU，`cargo build --release --features deepseek-v2-lite` 会发生什么？怎么救？

答案：[detect_sm_targets](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L435-L465) 读不到环境变量、`nvidia-smi` 也失败（或不存在），直接 `panic!("GPU detection failed")`。救法是设 `PEGAINFER_CUDA_SM=<目标架构>`（如 `120` 或 `120,80`），让探测走第一级瀑布。

**练习 2**：为什么 [nvcc_arch_args](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L467-L483) 除了每个目标的 `code=sm_X` 还要追加一条 `code=compute_<max>`？

答案：`sm_X` 是面向具体架构的 SASS 机器码，只在该架构 GPU 上能跑；`compute_X` 是 PTX 中间码，驱动可以在更新的架构上 JIT 编译执行。带上最高目标的 PTX，同一份二进制被拷到更新的卡上时仍有兜底路径。

**练习 3**：`is_deepseek_v2_lite_source` 靠「路径里有没有 `deepseek_v2_lite` 目录组件」来判定。如果把一个不相关的公共工具 `.cu` 放进 `csrc/deepseek_v2_lite/`，会发生什么？

答案：它会被当作 dsv2 专属源，只在 `deepseek-v2-lite` 特性开启时编译，stock/qwen3 构建里彻底消失。所以 `csrc/` 的目录名就是编译边界的声明——放错目录等于改变文件的编译条件，这是维护时要守住的约定。

### 4.3 关键环境变量：谁在读、什么时候读

#### 4.3.1 概念说明

构建期的行为开关全部用 `PEGAINFER_*` 环境变量表达（清单见 [CLAUDE.md:L38-L46](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L38-L46)）。对编译 dsv2-lite 而言，真正会碰到的只有四个：`PEGAINFER_CUDA_SM`（SM 覆盖）、`PEGAINFER_BUILD_TIMING`（分阶段计时）、`PEGAINFER_NVCC_JOBS`（nvcc 并行度）、`PEGAINFER_NVCC_LAUNCHER`（给 nvcc 加前缀启动器，交叉编译场景用）。

**最容易误解的是 `PEGAINFER_NCCL_ROOT`。** [CLAUDE.md:L42](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L42) 写明它服务于「DeepEP shim（`moe` feature）」。对照 4.1 节：`deepseek-v2-lite` 特性**不启用** `moe`，所以纯粹的 `--features deepseek-v2-lite` 构建根本不会读这个变量——[build.rs:L2340](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2340) 的 `let deepep_nccl = moe_enabled.then(deepep_nccl_root);` 只在 `moe` 开启时才去校验 NCCL。dsv2-lite 的 2 卡 EP 通信走的是本 crate 自己的 NCCL 后端：[nccl_backend.rs:L29-L32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/nccl_backend.rs#L29-L32) 直接 `use cudarc::nccl::sys::{ncclComm_t, ncclDataType_t, ...}`——cudarc 在**运行时**动态加载 `libnccl.so.2`，构建期既不需要 NCCL 头文件也不需要链接它。（当然，运行时机器上仍要有可用的 NCCL 库；dev 镜像已经装好。）

#### 4.3.2 核心流程

| 环境变量 | 谁读它 | 何时读 | 对 dsv2-lite 构建是否必需 |
| --- | --- | --- | --- |
| `PEGAINFER_CUDA_SM`（旧名 `CUDA_SM`） | [build.rs:L436](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L436) | 构建 kernels crate 时（无 `nvidia-smi` 时必须） | 条件必需 |
| `PEGAINFER_BUILD_TIMING` | [build_timing_enabled，L260-L265](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L260-L265) | 构建 kernels crate 时 | 可选，观测用 |
| `PEGAINFER_NVCC_JOBS` | [nvcc_job_count，L293-L302](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L293-L302) | 构建 kernels crate 时（回退到 `NUM_JOBS` 或 CPU 数） | 可选 |
| `PEGAINFER_NVCC_LAUNCHER` | [nvcc_command，L354-L365](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L354-L365) | 每次 nvcc/arch 探测调用前 | 可选 |
| `PEGAINFER_NCCL_ROOT` | [deepep_nccl_root，L861-L900](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L861-L900) | 仅 `moe` 特性开启时（kimi-k2 / glm52） | **不需要**（dsv2-lite 不开 `moe`） |
| `PEGAINFER_FLASHINFER_INCLUDE` | [flashinfer_includes，L1065-L1074](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L1065-L1074) | 构建 kernels crate 时 | 一般不需要（自动探测 include 目录） |

#### 4.3.3 源码精读

**计时开关。** [L260-L280](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L260-L280)：`build_timing_enabled` 把空串/`0`/`false`/`off` 都视为关，其余视为开；`time_phase(label, f)` 在开着时包裹任意闭包并打印 `cargo:warning=build-timing <label> <秒>`。它包住的阶段包括每个 nvcc TU、`ar libkernels_cuda.a`、Triton/TileLang 生成等——这就是综合实践里「记录各构建阶段耗时」的数据来源。

**并行度。** [L282-L302](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L282-L302)：`PEGAINFER_NVCC_JOBS` 非正整数时打警告并忽略，回退 `NUM_JOBS`（cargo 传入的默认并行度）再回退 CPU 数；[L2332-L2333](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2332-L2333) 会把最终作业数打进日志。nvcc 单个 TU 就能吃满多核，机器紧张时调小它比调小 cargo 的 `-j` 更精准。

**NCCL 校验（仅 `moe`）。** [deepep_nccl_root，L861-L900](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L861-L900)：未设该变量且开了 `moe` 则直接 panic；随后读取 `include/nccl.h`，解析 `NCCL_MAJOR/MINOR/PATCH` 三个宏拼成整数（2.30.4 → 23004）并断言 `version >= 23004`（[L884-L891](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L884-L891)），最后确认 `lib/libnccl.so.2` 存在。函数上方的注释（[L853-L860](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L853-L860)）还给出了用 pip wheel 快速准备一份 NCCL 的命令。[link_deepep_nccl（L906-L918）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L906-L918) 再在 `OUT_DIR` 里造一个不带版本号的 `libnccl.so` 软链喂给链接器。而这一切的触发条件只有一个：[L2340](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2340) 的 `moe_enabled`。反过来，不开 `moe` 时 [L2722-L2726](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2722-L2726) 只打一行「MoE third-party substrate disabled」的警告。

**为什么运行时和构建时必须指向同一份 NCCL。** 注释 [L853-L857](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L853-L857)：cudarc 运行时 dlopen 找到的 `libnccl.so.2` 若和构建期头文件版本不一致，DeepEP 的 device API 布局可能对不上。dev 镜像正是为了消灭这种漂移（见 4.4 节）。

#### 4.3.4 代码实践

**实践目标**：用源码推演 `PEGAINFER_NCCL_ROOT` 的校验行为，并验证「dsv2-lite 构建不读它」。

**操作步骤**：

1. 纸面推演：假设设了 `PEGAINFER_NCCL_ROOT=/tmp/bad-nccl`，其中 `include/nccl.h` 定义 `NCCL_MAJOR 2 / NCCL_MINOR 27 / NCCL_PATCH 0`，且缺 `lib/libnccl.so.2`。对照 [L861-L900](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L861-L900) 回答：版本断言先失败还是文件存在性断言先失败？（版本码 2×10000+27×100+0=20700 < 23004，版本断言在 [L887-L891](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L887-L891) 先触发，走不到 lib 检查。）
2. 在 dev 容器（或任何装有 NCCL 开发包的机器）里核对镜像预置的那份：

   ```bash
   grep -E "NCCL_(MAJOR|MINOR|PATCH)" "$PEGAINFER_NCCL_ROOT/include/nccl.h"
   ```

3. 实验验证触发条件：故意设一个坏值，然后分别构建两种特性，观察差异：

   ```bash
   PEGAINFER_NCCL_ROOT=/does/not/exist cargo build --release -p pegainfer-kernels --features deepseek-v2-lite
   PEGAINFER_NCCL_ROOT=/does/not/exist cargo build --release -p pegainfer-kernels --features kimi-k2
   ```

**需要观察的现象**：第 3 步中，`deepseek-v2-lite` 那条应照常构建（或因其他环境问题失败，但报错与 NCCL 无关）；`kimi-k2` 那条（它启用 `moe`）应在 `deepep_nccl_root` 处 panic，报错文本即 [L863-L867](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L863-L867) 的提示。

**预期结果**：与推演一致。待本地验证（第 3 步需要能编译 kernels 的环境）。

#### 4.3.5 小练习与答案

**练习 1**：`PEGAINFER_BUILD_TIMING=0` 和完全不设，行为有区别吗？

答案：行为相同。[build_timing_enabled（L260-L265）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L260-L265) 把 `0`/`false`/`off`/空串都当成关。区别只在缓存层面：[L2912](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2912) 声明了 `rerun-if-env-changed=PEGAINFER_BUILD_TIMING`，改值会触发构建脚本重跑。

**练习 2**：同事在 8 核机器上跑 `cargo build --release -p pegainfer-kernels --features deepseek-v2-lite -j2`，抱怨 nvcc 阶段还是把机器打满。为什么 `-j2` 没用？

答案：`-j` 控制 cargo 的 rustc 并行度，而 nvcc TU 的并行度由 [nvcc_job_count（L293-L302）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L293-L302) 单独决定，默认取 CPU 数。想限制 nvcc 要设 `PEGAINFER_NVCC_JOBS=2`。

**练习 3**：dsv2-lite 构建期不读 `PEGAINFER_NCCL_ROOT`，那运行期它需要 NCCL 吗？

答案：需要。dsv2-lite 的 EP 通信由本 crate 的 [nccl_backend.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/nccl_backend.rs#L29-L32) 通过 cudarc 的 NCCL 绑定完成，cudarc 在运行时 dlopen `libnccl.so.2`。所以运行机器上要装 NCCL 运行库（dev 镜像的 `libnccl2` 包或系统包），只是构建期不需要头文件与链接。

### 4.4 Docker 开发环境

#### 4.4.1 概念说明

按 [CLAUDE.md:L25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L25)，Docker 是官方推荐的开发方式。它解决的是「工具链漂移」：CUDA 13.2、Rust nightly（[rust-toolchain.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/rust-toolchain.toml#L1-L3) 钉在 `nightly-2026-07-10`）、NCCL ≥ 2.30.4、Triton/TileLang 版本——任何一项和队友不一致，都可能复现不了彼此的构建。镜像把这些一次钉死，还顺手把 4.3 节那个「构建期与运行期 NCCL 不一致」的坑填了：镜像把系统 NCCL 集中到固定路径 `/opt/nccl` 并设 `PEGAINFER_NCCL_ROOT`，让构建和运行永远指向同一份。

#### 4.4.2 核心流程

`docker/dev.sh` 是唯一入口（[docker/README.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/README.md#L7-L18)）：

```text
docker/dev.sh build      # 用 docker/Dockerfile.dev 构建镜像（默认 pegainfer-dev:cu132）
docker/dev.sh shell      # 交互式容器：仓库按原绝对路径挂载，cargo 注册表/编译缓存持久化
docker/dev.sh run CMD    # 一次性容器里执行 CMD，如 run cargo build --release --features ...

环境变量入口：
  CUDA_IMAGE              换 CUDA 基础镜像版本
  PEGAINFER_DEV_CACHE     持久缓存根目录（默认 ~/.cache/pegainfer-dev）
  PEGAINFER_MODEL_DIR     把模型权重目录只读挂进容器（绝对路径）
  PEGAINFER_CUDA_SM       由脚本转发进容器（dev.sh L82-L84）
  EP_DISABLE_GIN          无 GIN 网卡的主机上禁用 GIN（dev.sh L78-L80）
```

缓存的命名空间化值得一提：[dev.sh:L39-L46](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/dev.sh#L39-L46) 从镜像 label 读出 CUDA 基础镜像标识，把它揉进 `target/` 缓存目录名——换基础镜像后缓存自动分开，不会出现「CUDA 12 编译的目标文件被 CUDA 13 链接」的错配。

#### 4.4.3 源码精读

**镜像里装了什么。** [Dockerfile.dev:L20-L41](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L20-L41) apt 安装 build-essential、clang、cmake、`libnccl-dev`/`libnccl2`（[L30-L31](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L30-L31)，来自 CUDA 基础镜像已配好的 NVIDIA apt 源）、protobuf 等。[L43-L55](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L43-L55) 把多架构路径下的 NCCL 头与库软链到 `/opt/nccl/{include,lib}`，并用一段小程序断言 `NCCL_VERSION_CODE >= 23004`（[L51-L55](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L51-L55)）——版本不够，镜像直接构建失败，把问题挡在最前面。

**预置的环境变量。** [L57-L60](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L57-L60) 设 `CUDA_HOME=/usr/local/cuda`、`PEGAINFER_NCCL_ROOT=/opt/nccl`、`LD_LIBRARY_PATH`（含 `/opt/nccl/lib`）；[L86-L90](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L86-L90) 设 `RUSTUP_HOME`/`CARGO_HOME`/`PEGAINFER_TRITON_PYTHON`/`PEGAINFER_TILELANG_PYTHON` 并把 cargo、CUDA bin 加进 PATH。Rust 工具链按 [L93-L100](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/Dockerfile.dev#L93-L100) 从仓库根的 `rust-toolchain.toml` 读出 channel 安装——工具链版本跟着仓库走，镜像只提供安装机制。

**dev.sh 的挂载与转发。** [L48-L59](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/dev.sh#L48-L59)：`--gpus all`、host IPC/网络、仓库按原绝对路径挂载（workdir 同路径，构建缓存路径与宿主机一致，`target/` 挂到按镜像命名的持久目录）；[L82-L84](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/dev.sh#L82-L84) 把 `PEGAINFER_CUDA_SM` 转发进容器；[L86-L96](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/dev.sh#L86-L96) 校验 `PEGAINFER_MODEL_DIR` 必须是存在的绝对目录后只读挂载。[L98-L120](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/dev.sh#L98-L120) 是 build/shell/run 三条子命令的分发。

#### 4.4.4 代码实践

**实践目标**：从零起一个能编译 dsv2-lite 的容器环境。

**操作步骤**：

1. 按 [docker/README.md:L7-L11](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/README.md#L7-L11) 构建镜像（首次约需下载 CUDA 基础镜像与全部 apt 包）：

   ```bash
   docker/dev.sh build
   ```

2. 用一次性容器执行特性构建（对照 [README:L26-L28](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/README.md#L26-L28) 的 `run` 用法）：

   ```bash
   PEGAINFER_BUILD_TIMING=1 docker/dev.sh run \
     cargo build --release --features deepseek-v2-lite
   ```

   注意：`PEGAINFER_BUILD_TIMING` 不在 dev.sh 的转发清单里，`run` 形式下要改成先进 `shell` 再执行，或把它加进自己的 docker 调用。
3. 进入 `docker/dev.sh shell`，执行 `echo $PEGAINFER_NCCL_ROOT $CUDA_HOME` 和 `grep -E "NCCL_(MAJOR|MINOR)" $PEGAINFER_NCCL_ROOT/include/nccl.h`，核对 4.4.3 节的预置值。

**需要观察的现象**：第 2 步构建日志出现 `Detected CUDA SM targets`（容器内 `--gpus all` 能看到卡时）、`Compiling CUDA translation units with N nvcc job(s)`，以及含 `deepseek_v2_lite/` 两个 TU 的 build-timing 行；第 3 步输出 `/opt/nccl /usr/local/cuda` 和 ≥ 2.30 的版本宏。

**预期结果**：构建成功产出 `target/release/pegainfer` 二进制。待本地验证（需要 Docker + NVIDIA 容器工具链 + GPU 主机）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 dev.sh 把仓库挂载到**原绝对路径**（`--volume "$repo_root:$repo_root"`）而不是统一的 `/workspace`？

答案：因为构建产物与工具要按绝对路径互相引用。镜像里 `WORKDIR /workspace/pegainfer` 只是缺省值；把宿主机路径原样映射进容器，宿主机的 IDE、日志、缓存路径在容器内不变，`target/` 缓存目录也与宿主机约定一致，编译缓存才能跨容器/裸机复用。

**练习 2**：主机驱动只支持 CUDA 12.8，默认 `cu132` 镜像构建的二进制能跑吗？怎么办？

答案：nvcc 编译出的目标依赖工具链与驱动能力，驱动过旧可能无法加载新架构内核或新运行库特性。做法是按 [README:L49-L55](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/README.md#L49-L55) 用 `CUDA_IMAGE=nvidia/cuda:<12.8 版本>-devel-ubuntu24.04 docker/dev.sh build` 覆盖基础镜像；[dev.sh:L39-L46](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docker/dev.sh#L39-L46) 会因镜像标识变化自动切到新的缓存命名空间，不会污染旧缓存。

**练习 3**：镜像里同时装了 Triton 和 TileLang 并设了对应的 `PEGAINFER_*_PYTHON`。编译 dsv2-lite 时会用到它们吗？

答案：不会。构建期 Python 只被 `qwen35`（Triton AOT，[build.rs:L2874-L2880](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2874-L2880)）和 gemma4/k3/glm52 的 TileLang 家族（[L2743-L2792](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2743-L2792)）触发。dsv2-lite 的内核是手写 CUDA（`csrc/deepseek_v2_lite/*.cu`），整条构建链不含 Python——这也是 CLAUDE.md 说 stock 构建无 Python、dsv2-lite 同样不引入 Python 的原因。

## 5. 综合实践

把本讲四个模块串起来做一次「双构建对比」，这是后续所有 GPU 实验的起手式。

**任务**：在同一台机器上完成 stock 与 dsv2-lite 两种构建，量化特性开关带来的构建差异。

**步骤**：

1. **准备环境**（按 4.4 节）：`docker/dev.sh build` 后进入 `docker/dev.sh shell`。裸机方案：自备 CUDA 工具链，无 GPU 时设 `PEGAINFER_CUDA_SM`（按 4.2 节），并确认 NCCL 运行库可用（4.3 节，dsv2-lite 构建不需要 `PEGAINFER_NCCL_ROOT`）。
2. **基线（stock）构建**：

   ```bash
   PEGAINFER_BUILD_TIMING=1 cargo build --release 2>&1 | tee /tmp/stock-build.log
   ```

3. **特性构建**（先换缓存位置避免混用，或直接依赖 cargo 的特性感知缓存）：

   ```bash
   PEGAINFER_BUILD_TIMING=1 cargo build --release --features deepseek-v2-lite 2>&1 | tee /tmp/dsv2-build.log
   ```

4. **记录与对比**，至少回答四个问题：
   - 两份日志里 `build-timing nvcc ...` 行各有哪些 TU？`dsv2_lite_moe.cu`、`dsv2_lite_attention.cu` 只应出现在特性构建里（4.2 节的目录门控）。
   - `ar libkernels_cuda.a` 与各 nvcc TU 的耗时分别是多少？dsv2 两个 TU 占比多大？
   - `cargo tree -e features -p pegainfer-server --features deepseek-v2-lite`（4.1 节）与 stock 的 `cargo tree -e features -p pegainfer-server` 相比多了哪些节点？
   - `ls target/release/` 与 `nm`/`ar t` 检查 `libkernels_cuda.a`（在 `target/release/build/pegainfer-kernels-*/out/` 下）里是否出现 `dsv2_lite_*_cuda.o`。
5. **写一段说明**（200 字左右）：用自己的话解释「`--features deepseek-v2-lite` 到底改变了什么」——从 server 的 feature 表出发，沿 4.1 节的接力链讲到 4.2 节的目录门控，最后落到二进制里多出的 crate 与 `.o`。

**预期结果**：特性构建比 stock 多编译 `pegainfer-deepseek-v2-lite` crate 与 `csrc/deepseek_v2_lite/` 下两个 TU；两个 `pegainfer` 二进制都产出，但特性版能加载 DeepSeek-V2-Lite 权重、stock 版不能（服务运行层面的验证留给 u1-l3）。所有数值待本地验证。

## 6. 本讲小结

- `--features deepseek-v2-lite` 是一条三跳接力链：server 的 feature → dsv2-lite crate 的 feature（同时把 optional 的 kernels 依赖激活）→ kernels 的空 feature，最终同时打开 build.rs 的 CUDA 目录门（`csrc/deepseek_v2_lite/`）和 Rust 侧的 `#[cfg]` 模块门。
- 工作区根是**虚拟清单**（无 `[package]`、无根 build.rs），`default-members = ["pegainfer-server"]` 决定裸 `cargo run/build` 构建的是服务端二进制；所有 CUDA/Triton 编译集中在 `pegainfer-kernels/build.rs` 一处。
- build.rs 的流水线是：SM 探测（`PEGAINFER_CUDA_SM` > `nvidia-smi` > panic）→ 特性门控筛选 `csrc/**/*.cu` → 并行 nvcc（`PEGAINFER_NVCC_JOBS`）→ `ar` 归档 `libkernels_cuda.a` → 输出链接指令。
- `--release` 不是习惯而是铁律（[CLAUDE.md:L23](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L23)）：debug 构建的 GPU 路径慢到会超时。
- `PEGAINFER_NCCL_ROOT`（≥ 2.30.4）只被 `moe` 特性（DeepEP shim，kimi-k2/glm52）读取；dsv2-lite 的构建期不碰 NCCL，它的 EP 通信走 cudarc 的运行时 NCCL 绑定。
- `docker/Dockerfile.dev` + `docker/dev.sh` 把 CUDA 13.2、Rust nightly、NCCL（钉在 `/opt/nccl`）、Triton/TileLang 一次钉死，且 `target/` 缓存按 CUDA 基础镜像自动分命名空间。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：环境就绪后，把权重放进 `models/DeepSeek-V2-Lite`，用 `cargo run --release --features deepseek-v2-lite -- --model-path models/DeepSeek-V2-Lite` 起服务、发 `/v1/completions` 请求，观察启动日志里 EP 与专家相关的输出。
- **再下一讲（u1-l4）**：用 `cargo tree` 和 `src/` 目录走读，建立 dsv2-lite crate 内部「config / weights / engine / scheduler / runtime / ep / nccl_backend」的文件地图。
- **延伸阅读**（本讲提到的构建机制深处）：[pegainfer-kernels/build.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L1532-L1551) 的 tilelang 三层策略（generate / pre-gen / stub）与 [pegainfer-build](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2209) crate 的 `CudaToolkit::discover`，它们会在 u4-l2（kernels 构建系统）专门展开。
