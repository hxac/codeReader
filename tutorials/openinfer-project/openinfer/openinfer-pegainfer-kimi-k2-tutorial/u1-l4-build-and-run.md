# 构建与运行：feature、CLI 与引擎入口

> 单元 1 · 讲义 u1-l4 · 层级：beginner · 前置：u1-l1（PegaInfer 与 Kimi-K2 crate 的定位）。建议把 u1-l3 的模块地图放在手边对照。

## 1. 本讲目标

学完本讲，你应当能够：

1. 独立完成 Kimi-K2 引擎的构建与启动：`cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2`，并能解释为什么必须 `--release`。
2. 讲出 `MODEL_LINE` 挂进前端调度的三步流程：`probe`（身份探测）→ `augment_cli`（CLI 合并）→ `launch`（启动），以及 `--ep-backend` 参数的取值与拓扑约束：TP1/DP8 需要 `deepep`，TP8/DP1 需要 `nccl`。
3. 读懂 `launch` 如何把 CLI 原始值组装成 `ParallelConfig`、`device_ordinals`、`seed` 等字段，装进 `EngineLoadOptions` 交给 `runner::start_engine`，以及后者如何按 (TP, DP) 分派到两条启动路径并做设备数与后端匹配校验。

## 2. 前置知识

- **cargo feature（特性开关）**：Rust 的条件编译开关。一个 feature 可以开启本 crate 的部分模块，也可以「传导」到依赖 crate（`kimi-k2 = ["pegainfer-kernels/kimi-k2"]` 表示打开本 crate 的 `kimi-k2` 时，同时要求 `pegainfer-kernels` 打开它自己的 `kimi-k2`）。PegaInfer 用它实现「每个模型线一个开关，默认构建零额外负担」。
- **clap 的 `augment_args`**：clap 允许一个 crate 定义自己的参数结构体（`#[derive(Args)]`），再用 `augment_args` 把这些参数「合并」进别人已有的命令行定义。Kimi-K2 用这个机制把 `--ep-backend` 注入服务器的 CLI。
- **ModelLine（模型线）**：pegainfer-frontend 定义的分发接口。服务器二进制不做任何模型特定逻辑（CLAUDE.md 称之为 "pure dispatch"），每个模型 crate 提供一个实现：报上自己的名字、如何识别自己的 checkpoint、需要哪些专属命令行参数、以及如何启动引擎。
- **TP / DP / EP 三种并行**（一句话版）：
  - **TP（tensor parallel，张量并行）**：把一个算子切成几份，多卡合作算同一批 token；
  - **DP（data parallel，数据并行）**：不同卡各自服务不同请求，互不切分算子；
  - **EP（expert parallel，专家并行）**：MoE 的 384 个专家分散到多卡，token 按路由结果找卡。
  - Kimi-K2 支持两种 8 卡拓扑：TP8/DP1（MoE 走 NCCL 汇聚）与 TP1/DP8（MoE 走 DeepEP all-to-all），两者的 EP 世界大小都是 8。
- **NCCL 与 DeepEP**：NCCL 是 NVIDIA 的集合通信库（all-reduce / all-gather 等）；DeepEP 是面向 MoE 的高效 all-to-all 分发/汇聚库。Kimi-K2 的两条拓扑各绑定一种 MoE 通信后端。
- **为什么必须 `--release`**：CLAUDE.md 明确 "Always use `--release`"——debug 构建对 GPU/CUDA 路径极慢，会超时。这不是风格建议，是硬约束。
- **引擎入口的三个名字**（本讲反复出现，先记住层次）：
  - `model_line.rs` 的 `KimiK2Line::launch`：服务器 CLI → crate 的门；
  - `lib.rs` 的 `launch`：CLI 值 → 中性装载选项 `EngineLoadOptions`；
  - `runner/bringup.rs` 的 `start_engine`：装载选项 → 真正拉起线程与 GPU。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pegainfer-kimi-k2/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs) | Kimi-K2 的 `ModelLine` 实现：身份探测、专属 CLI（`--ep-backend`）、launch 入口；末尾带两个单测 |
| [pegainfer-kimi-k2/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs) | crate 门面：feature 门、`start_engine` 公共入口（含无 feature 时的报错桩）、`KimiLaunchOptions` 与私有 `launch` |
| [pegainfer-kimi-k2/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml) | feature 定义与传导、测试/二进制的 `required-features` 把门 |
| [pegainfer-kimi-k2/src/runner.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner.rs) | runner 子树根（10 行）：声明子模块并转出 `bringup::start_engine` |
| [pegainfer-kimi-k2/src/runner/bringup.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs) | 真正的启动实现：`resolving engine startup` 日志、设备数校验、按 (TP, DP) 分派两条启动路径 |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md) | 构建/运行命令、环境变量（如 `PEGAINFER_NCCL_ROOT`）与架构图的权威说明 |

`pegainfer-frontend` 中的 `ModelLine` trait 本体（`pegainfer-frontend/src/model_line.rs`）在另一个 crate，不在本讲验证范围内；下文只按 kimi-k2 侧的实际调用点还原它的契约，trait 定义的精确行号**待确认**。

## 4. 核心概念与源码讲解

### 4.1 构建命令与 feature 体系

#### 4.1.1 概念说明

PegaInfer 的默认构建只包含默认模型线（qwen3），其余每个模型线都藏在自己的 cargo feature 后面。对 kimi-k2 来说有三层含义：

1. **不开 feature 时 crate 几乎是空壳**：运行时模块全部被 `#[cfg]` 掉，`start_engine` 只是一个报错桩。
2. **`kimi-k2` 是总开关**：它点亮本 crate 的运行时模块，并把同名 feature 传导给 `pegainfer-kernels`——真正的 CUDA kernel 编译（nvcc）发生在那里。
3. **`kernel-report` 是观测超集**：它包含 `kimi-k2` 再叠加 `kernel-call-trace`，同时解锁两个报告二进制。

#### 4.1.2 核心流程

```text
cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2
        │
        ├─ workspace 根的 server 二进制接受 feature "kimi-k2" 并转发给本 crate
        │   （证据：CLAUDE.md 的构建命令即从仓库根运行；根 manifest 的转发行不在本讲验证范围）
        ├─ 本 crate 打开 kimi-k2 feature
        │   ├─ lib.rs 中 model_line / runner / weights / typed_scratch 等模块开始编译
        │   └─ 传导：pegainfer-kernels/kimi-k2 → 编译 Kimi 专属 CUDA 源码
        └─ --model-path 之后的部分原样传给 server CLI（--ep-backend 等由 4.2 的机制注入）
```

#### 4.1.3 源码精读

feature 的权威定义只有四行：

- [pegainfer-kimi-k2/Cargo.toml:L31-L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L31-L35) —— 定义 feature 表：`default = []`（默认什么都不开）、`kernel-call-trace` 传导给 core、`kernel-report = ["kimi-k2", "kernel-call-trace"]`（超集）、`kimi-k2 = ["pegainfer-kernels/kimi-k2"]`（点亮本 crate 运行时并把 CUDA 编译传导给 kernels）。
- [pegainfer-kimi-k2/Cargo.toml:L37-L40](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L37-L40) —— 精度门禁测试 `vllm_golden_gate` 标注 `required-features = ["kimi-k2"]`：不开 feature 时 `cargo test` 根本不会收集它（u7-l4 精读）。
- [pegainfer-kimi-k2/Cargo.toml:L42-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L42-L50) —— 两个报告二进制 `kimi_kernel_report` / `kimi_model_report` 都要求 `kernel-report`（u7-l3 会用到）。

feature 门在源码侧的落点（承接 u1-l3 的模块地图）：

- [pegainfer-kimi-k2/src/lib.rs:L19-L34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L19-L34) —— `batch_decode_trace`、`model_line`、`runner`、`typed_scratch`、`weights` 等模块各自挂着 `#[cfg(feature = "kimi-k2")]`；只有 `config` 无门（u1-l2 已讲）。
- [pegainfer-kimi-k2/src/lib.rs:L38-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L38-L47) —— 两个同名 `start_engine`：开了 feature 的真身只是转发 `runner::start_engine`；**没开 feature 时是报错桩**，直接 `bail!("Kimi-K2 runtime is feature-gated; rebuild with --features kimi-k2")`。这是「feature 忘开」时用户看到的第一条报错。

#### 4.1.4 代码实践：feature 组合矩阵（无 GPU 可做）

1. **实践目标**：用几次 `cargo check` 亲眼看 feature 门如何改变「这个 crate 编译了什么」。
2. **操作步骤**（在仓库根目录，本实践不需要 GPU）：
   ```bash
   cargo check -p pegainfer-kimi-k2                          # 1) 默认（default = []）
   cargo check -p pegainfer-kimi-k2 --features kimi-k2       # 2) 只开运行时
   cargo check -p pegainfer-kimi-k2 --features kernel-report # 3) 观测超集
   cargo build -p pegainfer-kimi-k2 --features kernel-report --bins
   ```
3. **需要观察的现象**：第 1 次能编译通过（`start_engine` 桩合法），但编译单元里没有任何 runner 代码；第 2 次开始编译 `pegainfer-kernels` 的 CUDA 部分（build.rs 阶段明显变慢、日志出现 nvcc 相关输出）；第 4 次在 `target/` 下出现 `kimi_kernel_report` 与 `kimi_model_report` 两个二进制。
4. **预期结果**：三次 check 都应成功；默认构建不产出本 crate 任何二进制（`autobins = false` 且两个 `[[bin]]` 都有 required-features）。
5. 以上为基于 Cargo.toml 与 build.rs 分工的推断，**待本地验证**（本讲写作环境未执行构建）。

#### 4.1.5 小练习与答案

**练习 1**：同事说「我 `cargo run --release -- --model-path models/Kimi-K2` 直接报错 feature-gated」，哪里出了问题？

答案：命令少了 `--features kimi-k2`。没开 feature 时链接到的是 [lib.rs:L44-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L44-L47) 的报错桩，错误文案里明确写着 "rebuild with --features kimi-k2"。

**练习 2**：`--features kernel-report` 之后还需要再加 `--features kimi-k2` 吗？

答案：不需要。`kernel-report = ["kimi-k2", "kernel-call-trace"]`（[Cargo.toml:L34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L34)）已把 `kimi-k2` 声明为依赖，cargo 会自动联合启用。

**练习 3**：为什么 `kimi-k2` feature 要传导到 `pegainfer-kernels`，而不是在本 crate 里编 CUDA？

答案：CLAUDE.md 的构建体系一节说明 CUDA/Triton 编译统一由 `pegainfer-kernels/build.rs` 拥有；模型 crate 只声明依赖与 feature，`kimi-k2 = ["pegainfer-kernels/kimi-k2"]`（[Cargo.toml:L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L35)）就是把「要编 Kimi 的 kernel」这个请求传导给唯一的编译 owner。

### 4.2 ModelLine 接入：probe → CLI 合并 → launch

#### 4.2.1 概念说明

服务器二进制要同时面对 8 个模型 crate，它自己却不写任何模型逻辑。解法是 `ModelLine` 接口：每个模型 crate 导出一个静态实例（这里是 `MODEL_LINE`），服务器拿着 checkpoint 的 `config.json` 依次问「你是它吗」，选中后再问「你要加什么命令行参数」，最后把合并好的命令行交回给该模型的 `launch`。对 Kimi-K2，这套对话是：

1. **probe（身份探测）**：config.json 的 `model_type` 是不是 Kimi 家族？是则继续交给几何校验（u1-l2 的 `probe_config_json`）。
2. **augment_cli（CLI 合并）**：把 `--ep-backend` 注入服务器的命令行定义，并声明 `tp_size` / `dp_size` / `cuda_graph` 三个共享参数由本模型线消费。
3. **launch（启动）**：从合并后的参数里取出自己的 `KimiCli` 与共享参数，调 `crate::launch`。

#### 4.2.2 核心流程

```text
server 启动
  │  读 <model-path>/config.json
  ▼
MODEL_LINE.probe(config)          ├── 拒绝 → 试下一个模型线
  │ 通过                           └── 全拒绝 → 不是已知模型
  ▼
MODEL_LINE.augment_cli(cmd)       # 注入 --ep-backend（默认 deepep）
MODEL_LINE.consumed_shared_args() # 声明接管 tp_size / dp_size / cuda_graph
  │ 用户命令行解析完成
  ▼
MODEL_LINE.launch(LaunchContext { matches, model_path, shared })
  ├─ KimiCli::from_arg_matches → ep_backend
  ├─ shared.dp_size.unwrap_or(8) → dp_size 缺省补 8
  └─ crate::launch(model_path, KimiLaunchOptions { … })   # 进入 4.3
```

#### 4.2.3 源码精读

- [pegainfer-kimi-k2/src/model_line.rs:L12-L14](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L12-L14) —— 导出静态实例 `pub static MODEL_LINE: KimiK2Line = KimiK2Line;`。零大小的单元结构体：模型线不携带状态，只是一组函数表。
- [pegainfer-kimi-k2/src/model_line.rs:L17-L29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L17-L29) —— Kimi 独占的 CLI：`KimiCli` 只有 `ep_backend` 一个字段，`#[arg(long, default_value = "deepep")]` 即 `--ep-backend`，默认 `deepep`；`CliEpBackend` 是 clap 的 `ValueEnum`，取值 `nccl` / `deepep`。doc 注释一句话写死约束："TP1/DP8 requires deepep; TP8/DP1 requires nccl"。
- [pegainfer-kimi-k2/src/model_line.rs:L31-L38](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L31-L38) —— 把 crate 内部枚举翻译成前端的 `pegainfer_frontend::engine::EpBackend`（`Nccl` / `DeepEp` 两个变体）。CLI 层与引擎层解耦：clap 的 ValueEnum 不泄漏进运行时。
- [pegainfer-kimi-k2/src/model_line.rs:L56-L66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L56-L66) —— `probe` 的身份门：外层 `model_type` 为 `kimi_k25` 或 `kimi_k2`，或内层 `text_config.model_type == "kimi_k2"`，任一命中即过门；否则返回 "model_type … is not a Kimi-K2 identity"。过门后交给 `crate::config::probe_config_json` 做几何校验（u1-l2 已逐项讲过），失败信息直接透传。
- [pegainfer-kimi-k2/src/model_line.rs:L68-L74](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L68-L74) —— `augment_cli` 用 `KimiCli::augment_args(cmd)` 合并参数；`consumed_shared_args` 返回 `["tp_size", "dp_size", "cuda_graph"]`，声明这三个共享参数（即 `--tp-size` / `--dp-size` / `--cuda-graph`，clap 的 kebab-case 惯例）由本模型线在 launch 里消费。
- [pegainfer-kimi-k2/src/model_line.rs:L76-L89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L76-L89) —— `launch` 的全部动作：`KimiCli::from_arg_matches` 解析自己的参数，组装 `KimiLaunchOptions`，调 `crate::launch`，最后 `.map(LaunchedEngine::Handle)` 把 `EngineHandle` 包装成前端统一的 `LaunchedEngine`。注意 **`dp_size: ctx.shared.dp_size.unwrap_or(8)`**：用户不传 `--dp-size` 时 Kimi 自己补 8——默认拓扑因此是 TP1/DP8，与 `--ep-backend` 默认 `deepep` 配对。
- [pegainfer-kimi-k2/src/model_line.rs:L92-L115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L92-L115) —— 两个单测固化身份门行为：`probe_rejects_foreign_model_type`（喂 `{"model_type": "qwen3"}`，断言错误文案含 "Kimi-K2"）与 `probe_accepts_text_config_identity_gate`（喂 `{"text_config": {"model_type": "kimi_k2"}}`，断言即便报错也不是身份错误——说明内层身份过门后，失败只会来自几何探针）。

#### 4.2.4 代码实践：跑身份门单测（无 GPU 可做）

1. **实践目标**：验证 `probe` 的接受/拒绝行为与源码注释一致。
2. **操作步骤**：
   ```bash
   cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib model_line
   ```
   必须带 `--features kimi-k2`：`model_line` 模块本身被 feature 门住（[lib.rs:L27-L28](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L27-L28)），不带 feature 会提示 0 个测试。测试只做 JSON 判断，不需要 GPU。
3. **需要观察的现象**：输出中出现 2 个测试：`probe_rejects_foreign_model_type` 与 `probe_accepts_text_config_identity_gate`。
4. **预期结果**：2 passed, 0 failed。
5. 测试名与断言已从源码核实；运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`--ep-backend` 的合法取值有哪些？不传时是什么？

答案：`nccl` 与 `deepep`（[model_line.rs:L24-L29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L24-L29) 的 `ValueEnum`），不传时默认 `deepep`（L20 的 `default_value`）。传别的值（如 `--ep-backend=auto`）会在 clap 解析阶段直接报「invalid value」。

**练习 2**：为什么 `--ep-backend` 默认是 `deepep` 而不是 `nccl`？

答案：与 dp_size 的缺省值配套。launch 里 `dp_size` 缺省补 8（[model_line.rs:L83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L83)），默认拓扑是 TP1/DP8，而该拓扑只有 DeepEP 一条 MoE 路径（见 4.3 的 `start_engine_tp1_dp8` 校验）。若默认 `nccl`，默认命令行将必然启动失败。

**练习 3**：`probe` 和 u1-l2 讲的 `probe_config_json` 是什么关系？

答案：两级门。`probe` 先做粗粒度身份判断（是不是 Kimi 家族的 model_type），过门后把 config 交给 `probe_config_json` 做几何/量化/路由语义的逐项校验（[model_line.rs:L65](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L65)）。身份不过时，根本轮不到几何报错。

### 4.3 start_engine / launch 入口：把 CLI 组装成 EngineLoadOptions

#### 4.3.1 概念说明

`lib.rs` 的 `launch` 是「EP 拓扑策略」的唯一 owner：它的 doc 注释明说，服务器只传原始 CLI 值，「validating TP/DP, deriving the EP world and its device ordinals」都在这里做，服务器永远不硬编码 Kimi 的并行布局。它做三件事：

1. 校验 tp/dp 为正数；
2. 用 `ParallelConfig::new(tp, dp)` 推出 EP 世界大小并打一条日志；
3. 把 CLI 值翻译成前端中性的 `EngineLoadOptions`（CUDA 图开关、设备序号表、并行配置、MoE 后端、采样种子），交给 `runner::start_engine`。

`runner::start_engine` 只是转出 `bringup::start_engine`（[runner.rs:L10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner.rs#L10)），后者才是启动的真正实现：打日志、校验设备数、按 (TP, DP) 分派。

#### 4.3.2 核心流程

```text
crate::launch(model_path, KimiLaunchOptions)            # lib.rs L64
  ├─ ensure tp_size>0 && dp_size>0                       # L68
  ├─ parallel = ParallelConfig::new(tp, dp)              # L72
  ├─ info!("Kimi-K2 EP options: … ep_world=…")           # L73
  └─ runner::start_engine(model_path, &EngineLoadOptions{# L80
        enable_cuda_graph, device_ordinals: 0..ep_world, parallel_config: Some,
        ep_backend, seed: 42 })                          # L82-L88
bringup::start_engine(model_path, options)               # bringup.rs L51
  ├─ parallel = resolve_parallel_config(options)         # L52（None → ParallelConfig::new(8,1)）
  ├─ info!("resolving engine startup: …")                # L53-L61（本讲实践要抓的日志行）
  ├─ ensure 设备序号数 == ep_world                        # L62-L68
  ├─ match (tp_world, dp_world):                         # L70
  │    (8,1) → start_engine_tp8_dp1                      # L71
  │    (1,8) → start_engine_tp1_dp8                      # L72
  │    _     → bail "not yet supported (v1: TP8DP1 or TP1DP8)"  # L73-L77
  └─ 分支内部：
       tp8_dp1:  ensure ep_backend == Nccl    （L141-L145）
       tp1_dp8:  ensure ep_backend == DeepEp  （L190-L193）
       → build_runner_config → spawn_workers → 加载权重 → init_tp_nccl / install_deepep_backends
         （后三步属于 u2 / u3 的内容，本讲只画到入口）
```

一个值得注意的设计细节：**两条默认路径不一样**。CLI 路径上 dp 缺省补 8（TP1/DP8，[model_line.rs:L83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L83)）；而库调用者若不传 `parallel_config`，`resolve_parallel_config` 回退到 `ParallelConfig::new(8, 1)`（[bringup.rs:L81-L85](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L81-L85)），即 TP8/DP1。读代码时不要把两个「默认」混为一谈。

#### 4.3.3 源码精读

- [pegainfer-kimi-k2/src/lib.rs:L49-L60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L49-L60) —— `KimiLaunchOptions`：`tp_size` / `dp_size` / `ep_backend` / `cuda_graph` 四个字段全部私有、`Copy`。注释写明分工：二进制传原始 CLI 值，本结构只对 crate 内的 `launch` 可见。
- [pegainfer-kimi-k2/src/lib.rs:L64-L90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L64-L90) —— `launch` 全函数：正数校验（L68-L71，报错文案 "Kimi-K2 --tp-size and --dp-size must be positive"）、`ParallelConfig::new`（L72）、EP 选项日志（L73-L79）、组装 `EngineLoadOptions` 并转发（L80-L89）。
- [pegainfer-kimi-k2/src/lib.rs:L82-L88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L82-L88) —— `EngineLoadOptions` 的组装逐字段看：

  | 字段 | 来源 | 含义 |
  | --- | --- | --- |
  | `enable_cuda_graph` | `options.cuda_graph`（即 `--cuda-graph`） | 是否启用 CUDA Graph 捕获（u7-l1 主题） |
  | `device_ordinals` | `(0..parallel.ep_world()).collect()` | 顺序使用 0..N 号 GPU；本 crate 不支持自定义设备表 |
  | `parallel_config` | `Some(parallel)` | 已解析的 TP/DP/EP 拓扑 |
  | `ep_backend` | CLI 的 `--ep-backend` | NCCL 或 DeepEP |
  | `seed` | 写死 `42` | 采样种子基值（目前不从 CLI 暴露；DP 路径每 rank 每步再派生独立种子，见 u7-l2） |

- [pegainfer-kimi-k2/src/runner/bringup.rs:L51-L79](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L51-L79) —— `start_engine` 主体：L53-L61 打出本讲实践要抓的 `resolving engine startup` 日志（字段依次为 model_path、tp_size、dp_size、ep_size、ep_backend、devices）；L62-L68 校验 `device_ordinals.len() == ep_world`；L70-L78 按 `(tp_world, dp_world)` 分派，其余组合报 "Kimi-K2 TP{}/DP{} not yet supported (v1: TP8DP1 or TP1DP8)"。
- [pegainfer-kimi-k2/src/runner/bringup.rs:L62-L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L62-L68) —— 细读这条设备数校验：CLI 路径上 `device_ordinals` 恒为 `(0..ep_world)`，长度必然相等，**这条 ensure 防的是绕过 launch、手工构造 `EngineLoadOptions` 的库调用者**。若在 8 卡以下的机器上用 CLI 启动，报错不会来自这里，而会来自更后面真正打开 CUDA 设备的阶段（本讲不展开）。
- [pegainfer-kimi-k2/src/runner/bringup.rs:L135-L145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L135-L145) —— TP8/DP1 分支开头：`info!("starting TP8/DP1 engine")` 后立刻 `ensure!(options.ep_backend == EpBackend::Nccl, …)`，报错文案 "Kimi-K2 TP8/DP1 routes MoE through the NCCL backend; --ep-backend={:?} has no TP8 path"。随后以 `KimiK2ParallelShape::tp8_ep8()` 与 `KIMI_TP8_KV_POOL_PAGES`（L46，8192 页）构建 runner config。
- [pegainfer-kimi-k2/src/runner/bringup.rs:L184-L193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L184-L193) —— TP1/DP8 分支开头：对称的 `ensure!(options.ep_backend == EpBackend::DeepEp, "Kimi-K2 TP1/DP8 requires --ep-backend=deepep")`；随后以 `KimiK2ParallelShape::tp1_dp8()` 与每 rank 1024 页（L49）构建配置。两条分支的 KV 页池常量差异（约 9.2 GiB/rank vs 1.15 GiB/rank）背后是「TP 复制 KV / DP 只背自己请求」的显存权衡，u3-l1 会展开。
- [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md) —— 权威命令表与环境变量：`cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2`；DeepEP 相关的 `PEGAINFER_NCCL_ROOT`（NCCL ≥ 2.30.4）；Docker 开发流（`docker/Dockerfile.dev` + `docker/dev.sh`）。

#### 4.3.4 代码实践：启动引擎并收集两条报错（需 8 GPU；无 GPU 见路径 B）

**路径 A：有 8 GPU 与模型权重**

1. **实践目标**：完整跑通一次启动，抓到 `resolving engine startup` 日志行，并故意触发两条后端/拓扑校验报错。
2. **操作步骤**：
   ```bash
   # 1) 正常启动（默认拓扑 TP1/DP8 + deepep）
   cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2

   # 2) 后端与拓扑不匹配之一：默认 TP1/DP8 却选 nccl
   cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2 --ep-backend nccl

   # 3) 后端与拓扑不匹配之二：TP8/DP1 却选 deepep
   cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2 \
       --tp-size 8 --dp-size 1 --ep-backend deepep

   # 4)（附加）不支持的拓扑
   cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2 --tp-size 2
   ```
3. **需要观察的现象**：
   - 步骤 1：日志先出现 `Kimi-K2 EP options: tp_size=…, dp_size=…, ep_world=…, ep_backend=…`（[lib.rs:L73-L79](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L73-L79)），随后是 `resolving engine startup: model_path=…, tp_size=…, dp_size=…, ep_size=…, ep_backend=…, devices=[…]`（[bringup.rs:L53-L61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L53-L61)），再是 `starting TP1/DP8 engine`。把 resolving 行的 6 个字段抄下来。
   - 步骤 2：报错应含 `Kimi-K2 TP1/DP8 requires --ep-backend=deepep`（[bringup.rs:L190-L193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L190-L193)）。
   - 步骤 3：报错应含 `Kimi-K2 TP8/DP1 routes MoE through the NCCL backend`（[bringup.rs:L141-L145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L141-L145)）。
   - 步骤 4：报错应含 `not yet supported (v1: TP8DP1 or TP1DP8)`（[bringup.rs:L73-L77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L73-L77)）。
4. **预期结果**：每条报错与源码 ensure/bail 的文案逐字对应；resolving 行的 `ep_size` 恒为 8，`devices` 为 `[0,1,2,3,4,5,6,7]`。
5. 本实践在写作环境中未运行，全部输出**待本地验证**。

**路径 B：无 GPU 的替代任务**

1. **实践目标**：验证 feature 门与 CUDA 编译传导，不启动引擎。
2. **操作步骤**：
   ```bash
   cargo check --features kimi-k2 2>&1 | tee /tmp/kimi-check.log
   grep -c "pegainfer-kernels" /tmp/kimi-check.log
   ```
3. **需要观察的现象**：check 日志里出现 `pegainfer-kernels` 的 build.rs 阶段（nvcc 编译输出）。
4. **预期结果**：`kimi-k2` feature 成功传导到 kernels（[Cargo.toml:L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L35)），日志可佐证；nvcc 产物清单的具体文件名不在本讲验证范围（build.rs 属于 pegainfer-kernels crate），如实记录看到的即可，不臆测。
5. **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`seed` 能通过命令行改吗？

答案：当前不能。[lib.rs:L87](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L87) 写死 `seed: 42`，`KimiLaunchOptions`（[lib.rs:L55-L60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L55-L60)）也没有 seed 字段。想改只能改代码。（DP 路径上每 rank 每步由协调器从它派生独立 philox 种子，u7-l2 详述。）

**练习 2**：如果不通过 CLI、而是写库代码直接调 `pegainfer_kimi_k2::start_engine` 且 `parallel_config` 传 `None`，会发生什么？

答案：`resolve_parallel_config` 回退 `ParallelConfig::new(8, 1)`（[bringup.rs:L81-L85](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L81-L85)），走 TP8/DP1 分支；此时若 `ep_backend` 不给 `nccl`，就会在该分支开头的 ensure 处失败——库调用者必须自己构造与拓扑匹配的 `EngineLoadOptions`。这条回退默认与 CLI 路径的 dp=8 默认不同，是刻意的两级策略。

**练习 3**：`--tp-size 8 --dp-size 1` 加什么后端能启动？此时 KV 页池多大？

答案：`--ep-backend nccl`。TP8/DP1 分支用 `KIMI_TP8_KV_POOL_PAGES = 8192` 页（[bringup.rs:L46](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L46)），注释说明每 rank 约 9.2 GiB；TP1/DP8 分支则是每 rank 1024 页、约 1.15 GiB（L49）。

## 5. 综合实践：从一条命令到一行日志的对账表

**任务**：为下面四个命令行各预测一行结果，再（在有 GPU 的机器上）实际运行验证，做成一张对账表。命令均省略共同前缀 `cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2`：

| 追加参数 | 预测的 resolving 行 (tp/dp/ep/backend) | 命中的分支或报错 |
| --- | --- | --- |
| （不追加参数） | 待你填写 | 待你填写 |
| `--ep-backend nccl` | 待你填写 | 待你填写 |
| `--tp-size 8 --dp-size 1 --ep-backend nccl` | 待你填写 | 待你填写 |
| `--tp-size 2 --dp-size 4` | 不适用（bail 于分派前） | 待你填写 |

**步骤**：

1. 先只读代码填「预测」列：线索在 [model_line.rs:L76-L89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L76-L89)（dp 缺省补 8）、[lib.rs:L72-L88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L72-L88)（ParallelConfig 与 EngineLoadOptions）与 [bringup.rs:L51-L79](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L51-L79)（日志、校验、分派）。
2. 逐条运行，抄录 `Kimi-K2 EP options` 与 `resolving engine startup` 两行日志及最终结果（成功进入 `starting … engine` 或报错文案）。
3. 对照两列，任何不一致都回到对应源码行找原因。

**产出**：一张 4 行对账表 + 一张从「server 进程读 config.json」到「start_engine_tp8_dp1 / start_engine_tp1_dp8 被调用」的时序图（节点：config.json、MODEL_LINE.probe、augment_cli、clap 解析、KimiK2Line::launch、crate::launch、runner::start_engine、bringup::start_engine、两条分支）。

**参考答案（第 1 行示例）**：默认不追加参数时，dp_size 缺省补 8、tp_size 取共享 CLI 值，ep_backend 默认 deepep；resolving 行预期形如 `tp_size=1, dp_size=8, ep_size=8, ep_backend=DeepEp, devices=[0,…,7]`，随后打印 `starting TP1/DP8 engine`。tp_size 的共享缺省值由 frontend 定义（**待确认**），但无论它缺省为何值，非法组合都会在 bringup 的 ensure/match 处得到明确报错——这正是这套校验链的设计意图。

## 6. 本讲小结

- 构建 Kimi-K2 只有一条正路：仓库根 `cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2`；`--release` 是硬约束，feature 忘开只会链接到报错桩（[lib.rs:L44-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L44-L47)）。
- feature 体系是三层传导：根命令行 → 本 crate `kimi-k2` → `pegainfer-kernels/kimi-k2`（CUDA 编译）；`kernel-report` 是包含运行时的观测超集（[Cargo.toml:L31-L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L31-L35)）。
- 服务器纯分发，模型行为由 `ModelLine` 三步对话决定：`probe` 过身份门（`kimi_k25` / `kimi_k2` 两种外层 + `text_config` 内层身份）→ `augment_cli` 注入 `--ep-backend` 并声明消费 tp_size / dp_size / cuda_graph → `launch` 组装 `KimiLaunchOptions`（dp 缺省补 8）。
- `lib.rs::launch` 是 EP 拓扑策略的唯一 owner：正数校验 → `ParallelConfig::new` → `EngineLoadOptions`（设备表 `0..ep_world`、`seed: 42`）→ `runner::start_engine`。
- `bringup::start_engine` 用三道闸收口：`resolving engine startup` 日志 → 设备数校验 → `(TP, DP)` match 分派；每条分支开头再用 ensure 锁死后端匹配（TP8/DP1 必须 nccl，TP1/DP8 必须 deepep），报错文案都能逐字对上源码。
- 两级默认不同是刻意设计：CLI 路径默认 TP1/DP8（dp 补 8 + deepep），库路径 `parallel_config=None` 时回退 TP8/DP1。

## 7. 下一步学习建议

- 顺序上的下一讲是 **u2-l1（index 解析与 KimiK2WeightManifest）**：启动链过了本讲的入口后，第一件重活就是读 `model.safetensors.index.json`。
- 若想先把启动链看完，可跳读 **u3-l1（start_engine：双拓扑启动流程）**：它从本讲止步的 `start_engine_tp8_dp1` / `start_engine_tp1_dp8` 继续往下走（worker 线程、NCCL/DeepEP 集体初始化、KV 页池预算）。
- 阅读延伸：[docs/models/kimi-k2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2) 目录下的 dp-design、deepep-migration 等设计文档（u7-l5 会系统导读）。
- 动手建议：把综合实践的对账表存进自己的笔记；等学完 u3-l1 后回来补全时序图的后半段（spawn_workers → 集体初始化 → EngineHandle 就绪）。
