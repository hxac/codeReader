# u1-l2 构建与运行：workspace 成员、feature 与环境变量

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `pegainfer-bench` 在 pegainfer workspace 里的身份：它是一个 **lib-only 成员 crate**（当前 HEAD 下没有二进制入口），由其他 crate 作为库依赖使用。
2. 用 `-p pegainfer-bench` 精确选择构建目标，并解释 `workspace = true` 依赖继承如何把版本和 feature 集中到根 `Cargo.toml`。
3. 解释为什么这个项目「永远要 `--release`」：debug 构建对 GPU/CUDA 代码慢到会超时。
4. 说清 `PEGAINFER_CUDA_SM` 与 `PEGAINFER_TEST_MODEL_PATH` 分别解决什么问题。
5. 修正一个常见误会：旧资料（含本手册早期规划稿）提到的 `src/main.rs`、`src/cli.rs`、`src/loop/` 在当前 HEAD **并不存在**——`pegainfer-bench/src/` 只有一个 `lib.rs`。

## 2. 前置知识

本讲依赖 u1-l1（项目定位与全景）的结论：PegaInfer 是纯 Rust + CUDA 的 LLM 推理引擎，不依赖 PyTorch；workspace 大致分层为 `pegainfer-frontend`（HTTP 服务前端）→ 各模型 crate（qwen3 / qwen35 / kimi-k2 / glm52 …）→ `pegainfer-core` / `pegainfer-kernels`（共享算子与 kernel 构建）→ CUDA / cuBLAS / Triton / FlashInfer。

在此基础上，本讲需要几个 Cargo 概念（不熟悉 Rust 的读者看这里）：

- **workspace（工作区）**：多个 crate 共用一个 `Cargo.lock` 和一个 `target/` 目录。根 `Cargo.toml` 只写 `[workspace]`、不写 `[package]` 时称为**虚拟清单（virtual manifest）**——pegainfer 正是这种。
- **member（成员）**：列在 `members` 里的子目录，每个目录是一个 crate。
- **`workspace = true`（依赖继承）**：子 crate 写 `anyhow = { workspace = true }`，表示版本与 feature 由根 `Cargo.toml` 的 `[workspace.dependencies]` 统一决定，全仓库只有一个版本来源。
- **`-p` 包选择**：`cargo build -p <名字>` 只构建指定 crate。
- **feature 门控**：Cargo 的条件编译开关，`--features qwen35` 会打开对应模型 crate 的编译路径。
- **SM（Streaming Multiprocessor）架构版本**：NVIDIA GPU 的「代次号」，如 `120` 对应 sm_120；nvcc 需要知道目标 SM 才能生成对应指令集，`120,80` 表示同时为两代 GPU 出码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`Cargo.toml`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml) | 本 crate 清单，仅 15 行：包元信息 + 5 个 workspace 继承依赖 + lints 继承；**无 `[features]`、无 `[[bin]]`** |
| [`../Cargo.toml`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml) | 虚拟 workspace 根清单：`members` / `default-members` / `[workspace.dependencies]` / `[workspace.lints]` 全在这里 |
| [`../CLAUDE.md`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md) | 仓库级开发指南：`--release` 纪律、全部 `PEGAINFER_*` 环境变量、构建系统说明 |
| [`src/lib.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs) | 当前 HEAD 下 crate 的**唯一**源文件：模型无关的 kernel 基准 harness |
| `../pegainfer-kernels/build.rs` | （只提及，不精读）CUDA/Triton 编译入口，SM 目标探测在这里发生，是 `PEGAINFER_CUDA_SM` 的消费方 |

> 结构勘误：规划稿曾按 `src/main.rs` + `src/cli.rs` + `src/loop/{mod,serve,kv}.rs` 描述本 crate。经在 HEAD `223206b7` 用 GitHub contents API 核实，`pegainfer-bench/src/` 下**只有 `lib.rs`（8370 字节）一个文件**，`main.rs` 返回 404。后续讲义涉及 CLI 结构的内容请以实际源码为准。

## 4. 核心概念与源码讲解

### 4.1 workspace 与依赖继承

#### 4.1.1 概念说明

一个推理引擎为什么要拆成 20 多个 crate？因为不同模型（Qwen3、Kimi-K2、GLM5.2…）各有独立的权重格式与 kernel，但共享运行时、KV cache、CUDA 封装。workspace 让这些 crate 共享一份依赖版本表（避免版本漂移）、共享一个 `target/`（增量重编），并用 feature 在同一份源码里裁剪出「纯 Rust + CUDA 的默认构建」与「需要 Python/Triton 的模型线」。

`pegainfer-bench` 在棋盘上的位置很特殊：它不是给最终用户跑的服务，而是一个**被别人依赖的基准工具库**——根清单把它登记进 `[workspace.dependencies]`，各模型 crate 的 kernel-report 工具就能以 `pegainfer-bench = { workspace = true }` 复用它的计时统计等构件。

#### 4.1.2 核心流程

Cargo 解析构建目标的流程（示例代码，非项目源码）：

```text
cargo build -p pegainfer-bench
  ├─ 读取根 Cargo.toml（虚拟清单）
  │    ├─ members 里找到 "pegainfer-bench"
  │    └─ 不带 -p 时只构建 default-members（= pegainfer-server）
  ├─ 读取 pegainfer-bench/Cargo.toml
  │    ├─ 依赖是 { workspace = true } → 回根清单查版本与 feature
  │    └─ cudarc → 根清单 cudarc(0.19.3+features)
  │         └─ 依赖链拉进 pegainfer-kernels → 触发其 build.rs（CUDA 编译）
  └─ 产出 target/release/ 下的库产物（不是可执行文件）
```

关键点：**在仓库根目录裸跑 `cargo build` 不会构建 bench**——`default-members` 限定为 `pegainfer-server`，想构建 bench 必须显式 `-p pegainfer-bench`。

#### 4.1.3 源码精读

**① 根清单声明成员与默认目标**——[../Cargo.toml:L1-L27](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L1-L27)：定义 `[workspace]`、`default-members = ["pegainfer-server"]`、完整 `members` 列表（第 10 行即 `pegainfer-bench`）与 `resolver = "3"`。不带 `-p` 的命令只作用于 server。

**② bench 自己的清单：全部继承，零本地版本号**——[Cargo.toml:L7-L12](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml#L7-L12)：

```toml
[dependencies]
anyhow = { workspace = true }
cudarc = { workspace = true }
pegainfer-kernels = { workspace = true }
serde = { workspace = true }
serde_json = { workspace = true }
```

五个依赖没有一个写版本号——`anyhow`/`serde`/`serde_json` 的版本在根清单 [L49](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L49)、[L166-L167](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L166-L167) 统一决定；`pegainfer-kernels` 是 path 依赖（[L129](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L129)），自带 `build.rs`，是 CUDA 编译入口。

**③ bench 被别的 crate 依赖的通道**——[../Cargo.toml:L120](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L120)：`pegainfer-bench = { path = "pegainfer-bench" }` 把 bench 登记为可继承依赖。这与 [src/lib.rs:L1-L3](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L1-L3) 的模块文档自述一致：「Model-agnostic kernel benchmarking harness……每个模型 crate 的 kernel-report 工具都需要同样的构建块」。

**④ lint 也走继承**——[Cargo.toml:L14-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml#L14-L15) 的 `[lints] workspace = true`，配合根清单 [L198](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L198) 起的 `[workspace.lints.clippy]`，全仓库共用一套 clippy 标准。

#### 4.1.4 代码实践

1. **实践目标**：用只读命令亲眼看到「依赖继承」的效果。
2. **操作步骤**：在仓库根目录执行 `cargo tree -p pegainfer-bench --depth 1`。
3. **需要观察的现象**：`pegainfer-bench` 节点下挂出的直接依赖恰好是清单里那 5 个；`cudarc` 的解析版本来自 workspace。
4. **预期结果**：输出形如 `pegainfer-bench v0.1.0 ├── anyhow v1.x ├── cudarc v0.19.x ├── pegainfer-kernels ├── serde v1.x └── serde_json v1.x`（具体小版本以本地 `Cargo.lock` 为准）。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么在仓库根目录直接 `cargo build` 没有编译 `pegainfer-bench`？
答案：根清单 `default-members = ["pegainfer-server"]` 把无 `-p` 的默认目标限定为 server；构建 bench 必须显式 `-p pegainfer-bench`。

**练习 2**：`anyhow = { workspace = true }` 与 `anyhow = "1.0"` 的本质区别？
答案：前者把版本（及 feature）委托给根 `[workspace.dependencies]`，全仓库统一；后者是本 crate 自锁版本，易产生版本分叉。

**练习 3**：`pegainfer-kernels` 在 bench 依赖里意味着什么额外负担？
答案：它是 path 依赖且自带 `build.rs`，会触发 CUDA（nvcc）编译——构建 bench 不只是编 Rust，还牵动整套 kernel 构建。

### 4.2 release 构建纪律与 feature 门控

#### 4.2.1 概念说明

**为什么必须 `--release`**：Rust debug 构建默认不优化、开启溢出检查与调试断言，CPU 侧代码慢一到两个数量级。对一个以 GPU 吞吐为生命的推理引擎来说，CPU 侧的调度、张量元数据、拷贝编排一旦变慢，整条流水线被拖垮，测试和基准会**超时失效**——所以仓库指南用加粗句式规定「永远用 `--release`」。

**feature 门控是什么**：Cargo 的 feature 是编译期开关。PegaInfer 用它实现「同一个仓库，按需编译模型线」：默认只有 `qwen3`（纯 Rust + CUDA，构建过程零 Python）；要跑 Qwen3.5 就 `--features qwen35`（构建期要 Python + Triton 生成 AOT kernel）。

**一个必须诚实交代的事实**：规划稿说 bench 有「`cuda` 默认 feature（映射 `cudarc/cuda`）」。但当前 HEAD 的 [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml) **没有 `[features]` 段**——本 crate 自身不设 feature 门控（该说法在本 HEAD 无法从源码证实，待确认；可能来自旧版本）。CUDA 能力的门控实际发生在两个层面：workspace 层面统一给 `cudarc` 开一组 CUDA feature；模型 crate 层面用 `qwen3`/`qwen35`/`kimi-k2` 等 feature 控制模型线。

#### 4.2.2 核心流程

构建一条命令的全链路（示例代码）：

```text
cargo build --release -p pegainfer-bench
  ├─ 解析 workspace 清单（4.1 的流程）
  ├─ 取 cudarc = 0.19.3 + features [cuda-12090, cublas, f16, nccl, nvrtc]
  ├─ 编译 pegainfer-kernels：build.rs 用 nvcc 编 csrc/*.cu，探测本机 GPU 的 SM 目标
  │    （探测不到时需要 PEGAINFER_CUDA_SM，见 4.3）
  └─ 以 release 优化编译 bench 的 lib.rs → 库产物
```

#### 4.2.3 源码精读

**① `--release` 是仓库级纪律**——[../CLAUDE.md:L23](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L23)：

> **Always use `--release`** — debug builds are extremely slow for GPU/CUDA and will timeout.

指南随后在 [L27-L36](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L27-L36) 给出标准构建命令，例如 `cargo run --release -- --model-path models/Qwen3-4B`（注意：这是根目录的 server 二进制，不是 bench）。

**② 模型线的 feature 门控**——[../CLAUDE.md:L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L9) 说明只有 `qwen3` 是默认 feature；[L11-L19](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L11-L19) 的表格列出每条模型线的 crate、feature 与架构（Qwen3.5 混合 GDN + 全注意力、Kimi-K2 的 MLA + MoE + Marlin INT4 等）。

**③ CUDA feature 在 workspace 层统一配置**——[../Cargo.toml:L77-L87](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L77-L87)：

```toml
cudarc = { version = "0.19.3", features = [
  "cuda-12090",
  "cublas",
  "f16",
  "nccl",
  ...
  "nvrtc",
] }
```

一次声明，所有 `cudarc = { workspace = true }` 的 crate（含 bench）共享同一组 CUDA 绑定。其上方 [L69-L76](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L69-L76) 的注释解释了版本区间钉住的原因（与 dynamo 子 workspace 的 `=0.19.3` pin 对齐、保持 0.19.5+ 的惰性 nvrtc 符号加载）。

**④ bench 清单里确实没有 feature 段**——通读 [Cargo.toml:L1-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml#L1-L15) 只有 `[package]`、`[dependencies]`、`[lints]` 三段，也无 `[[bin]]`——这就是 4.2.1 勘误的源码依据。

#### 4.2.4 代码实践

1. **实践目标**：亲手确认「bench 是 lib-only crate」并完成一次真实构建。
2. **操作步骤**：先 `cargo run --release -p pegainfer-bench -- --help`；观察报错后改跑 `cargo build --release -p pegainfer-bench`。
3. **需要观察的现象**：第一条命令应报「没有可运行的 bin 目标」类错误（`src/` 只有 `lib.rs`）；第二条命令开始编译，可能输出 nvcc/CUDA 构建日志。
4. **预期结果**：`cargo build` 最终在 `target/release/` 下产出 bench 库产物（如 `libpegainfer_bench.rlib`）。两条命令的具体输出文本**待本地验证**。
5. 本实践同时验证了本讲最重要的结构事实：没有 `--help` 可看，因为它不是命令行程序。

#### 4.2.5 小练习与答案

**练习 1**：`cargo run --release -p pegainfer-bench` 失败的原因？
答案：当前 HEAD 的 `pegainfer-bench` 是 lib-only crate（清单无 `[[bin]]`，`src/` 无 `main.rs`），`cargo run` 需要 bin 目标，因此报错；它以库形式被其他 crate 依赖。

**练习 2**：默认 `cargo build --release`（在仓库根、不加 `-p`）构建出的二进制是什么？为什么 bench 不在其中？
答案：构建 `default-members` 指定的 `pegainfer-server`；bench 不在默认成员里，需显式 `-p pegainfer-bench`。

**练习 3**：想构建 Qwen3.5 支持时，feature 加在 bench 上吗？
答案：不是。feature 作用于整个构建的解析（如 `cargo build --release --features qwen35`），`qwen35` 属于对应模型 crate；bench 自身没有 `[features]` 段，不感知模型线。

### 4.3 关键环境变量

#### 4.3.1 概念说明

构建期行为不完全由 Cargo.toml 决定，`pegainfer-kernels/build.rs` 还读一组 `PEGAINFER_*` 环境变量。本讲聚焦两个：

- **`PEGAINFER_CUDA_SM`**：SM 目标覆盖。build.rs 正常靠运行 `nvidia-smi` 探测本机 GPU 架构号来决定 nvcc 的 `-arch`；在没有 GPU（或没有 `nvidia-smi`）的机器上（CI 容器、交叉编译机）探测会失败，这时设 `PEGAINFER_CUDA_SM=120`（或 `120,80` 多目标）显式告诉构建系统为哪些架构出码。
- **`PEGAINFER_TEST_MODEL_PATH`**：测试模型路径覆盖，默认 `models/Qwen3-4B`。模型 crate 的 GPU 集成测试要加载真实权重，本地模型放在别处时用它指路。注意：它影响的是**模型 crate 的测试**；bench 的依赖里没有任何模型 crate，构建/使用 bench 不需要模型权重。

顺带认识两个构建观测开关：`PEGAINFER_BUILD_TIMING=1` 打印各阶段（nvcc、Triton AOT 等）耗时；`PEGAINFER_NVCC_JOBS` 控制并行 nvcc 任务数。

#### 4.3.2 核心流程

build.rs 决定 nvcc 目标架构的决策树（示例代码）：

```text
SM 目标 = ?
  ├─ 环境里有 PEGAINFER_CUDA_SM ? → 直接用它（如 "120" 或 "120,80"）
  └─ 否则 → 尝试运行 nvidia-smi 探测本机 GPU
        ├─ 成功 → 得到 SM 号，交给 nvcc -arch
        └─ 失败 → 构建受阻（这正是无 GPU 环境必须设该变量的原因）
```

（build.rs 内部实现的精确行为属下一层次内容，本讲以 CLAUDE.md 的说明为准。）

#### 4.3.3 源码精读

**① `PEGAINFER_CUDA_SM` 的官方定义**——[../CLAUDE.md:L39](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L39)：

> `PEGAINFER_CUDA_SM` — GPU SM target override when nvidia-smi unavailable (e.g. `120` or `120,80`)

**② `PEGAINFER_TEST_MODEL_PATH` 的官方定义**——[../CLAUDE.md:L44](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L44)：

> `PEGAINFER_TEST_MODEL_PATH` — override test model path (default: `models/Qwen3-4B`)

**③ 构建观测开关**——[../CLAUDE.md:L45-L46](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L45-L46) 定义 `PEGAINFER_BUILD_TIMING=1` 与 `PEGAINFER_NVCC_JOBS`。

**④ 变量为什么对 bench 构建生效**：bench 依赖 `pegainfer-kernels`（[Cargo.toml:L10](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml#L10)），后者的 `build.rs` 负责 CUDA 编译并读取这些变量——即使「只是构建 bench」，也会走一遍这条 CUDA 构建链。

#### 4.3.4 代码实践

1. **实践目标**：在无 `nvidia-smi` 的环境下完成一次 bench 构建，并从构建日志找到 SM 相关输出。
2. **操作步骤**：`PEGAINFER_CUDA_SM=120 PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-bench 2>&1 | tee build-bench.log`，然后 `grep -iE 'sm_|arch|nvcc|phase|timing' build-bench.log`。
3. **需要观察的现象**：日志中出现 nvcc 调用与各阶段耗时（`PEGAINFER_BUILD_TIMING=1` 的效果）；SM 目标相关行体现 `120`（或本机探测到的架构）。
4. **预期结果**：构建成功；日志能定位到 CUDA 编译阶段。若本机有 GPU，可再跑一次不设 `PEGAINFER_CUDA_SM` 的构建对比探测行为。具体日志文本**待本地验证**（不同机器/缓存状态下差异较大；若 `pegainfer-kernels` 已缓存，可先 `cargo clean -p pegainfer-kernels` 再试）。

#### 4.3.5 小练习与答案

**练习 1**：CI 容器里没有 GPU，`cargo build --release -p pegainfer-bench` 在 nvcc 阶段失败，该怎么办？
答案：设 `PEGAINFER_CUDA_SM` 为目标部署 GPU 的 SM 架构号（如 `120`；需兼容多代就写 `120,80`），让 build.rs 跳过 `nvidia-smi` 探测。

**练习 2**：`PEGAINFER_TEST_MODEL_PATH` 对「构建 bench」有用吗？对什么场景有用？
答案：对构建 bench 没用——bench 不依赖任何模型 crate；它服务于模型 crate 的 GPU 集成测试（如 `PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test … --test hf_golden_gate`），用于指定本地权重目录。

**练习 3**：`120` 和 `120,80` 两种取值的区别？
答案：单值只为一代 GPU 出码；逗号列表同时为多代架构生成代码，构建更慢但产物可在多类卡上运行。

## 5. 综合实践

**任务：制作一份「pegainfer-bench 构建档案」。** 在仓库根目录依次执行并记录：

1. `cargo tree -p pegainfer-bench --depth 1` → 抄下 5 个直接依赖及解析版本（验证 4.1 的继承关系）。
2. `cargo run --release -p pegainfer-bench -- --help` → 记录报错文本（验证 lib-only 事实）。
3. `PEGAINFER_CUDA_SM=120 PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-bench 2>&1 | tee build-bench.log` → 从日志摘出：nvcc 编译阶段、SM/arch 相关行、各阶段耗时。
4. 在 [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml) 与 [../Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml) 里，为步骤 1 看到的每个依赖找到它「版本真身」所在的行。

把结果整理成一张三列表（命令 / 现象 / 对应源码行），就同时验证了本讲的三个模块：workspace 继承、release/feature 门控、环境变量。全部命令输出**待本地验证**（本讲义撰写环境无法运行 cargo）。

## 6. 本讲小结

- pegainfer 是虚拟 workspace：根清单 `members` 收录 20+ crate（bench 在列），`default-members = ["pegainfer-server"]` 决定裸 `cargo build` 只编 server——构建 bench 必须 `-p pegainfer-bench`。
- `pegainfer-bench` 当前是 **lib-only crate**：`src/` 只有 `lib.rs`（模型无关 kernel 基准 harness），清单无 `[[bin]]`、无 `[features]`；`cargo run` 对它无效，它被模型 crate 经 `[workspace.dependencies]` 复用。
- 所有依赖 `{ workspace = true }` 继承自根清单：`cudarc 0.19.3`（cuda-12090/cublas/f16/nccl/nvrtc feature）、`pegainfer-kernels`（path 依赖，自带 CUDA build.rs）等。
- **永远 `--release`**：debug 构建慢到让 GPU/CUDA 流水线超时，是仓库级纪律。
- `PEGAINFER_CUDA_SM` 解决无 `nvidia-smi` 环境下 nvcc 目标架构的问题；`PEGAINFER_TEST_MODEL_PATH` 只影响模型 crate 的集成测试，与 bench 构建无关。
- 模型线（qwen3 默认 / qwen35 / kimi-k2 …）的 feature 门控在模型 crate 与统一构建命令层面，不在 bench 自身。

## 7. 下一步学习建议

- 下一讲 u1-l3 进入 bench 的**基准入口与 CLI**。注意：其规划稿描述的 `src/main.rs`、`src/cli.rs`、`src/loop/` 与当前 HEAD 不符——阅读时请先 `ls pegainfer-bench/src/` 以实际文件为准（本 HEAD 只有 `lib.rs`），把规划稿里的 CLI 叙述按「待确认」处理。
- 想看 CUDA 构建链源头，接着读 `pegainfer-kernels/build.rs` 与 `CLAUDE.md` 的 "Build system" 段落。
- 想理解 bench 作为库提供什么构件，通读 [src/lib.rs:L1-L3](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L1-L3) 起的模块文档，再用 `cargo tree -i pegainfer-bench` 反查谁依赖它（待本地验证）。
