# u1-l3 crate 模块地图与 feature 门

## 1. 本讲目标

学完本讲，你应该能够：

1. **不借助工具**画出 `pegainfer-kimi-k2/src` 的完整模块树，并说出 `runner/` 与 `weights/` 两个子树里每个文件的职责。
2. 理解仓库采用的**扁平模块布局**约定（`foo.rs` + `foo/` 目录，不用 `mod.rs`），以及它带来的 `use super::*` 紧耦合子模块风格。
3. 看懂 `lib.rs` 里每一处 `#[cfg(feature = ...)]`：知道 **默认构建只有 `config` 模块和一个报错的 `start_engine` 桩**，运行时本体全部藏在 `kimi-k2` feature 后面。
4. 学会用「文档时间戳 + 源码交叉验证」的方法读 `docs/models/kimi-k2/source-layout.md` 这类**历史快照文档**，分清哪些描述仍然准确、哪些已经漂移。

本讲承接 u1-l1（crate 定位与 feature 体系概览）和 u1-l2（config.rs 的模型几何），不重复它们的内容；本讲要回答的新问题是：**「代码到底怎么组织、每扇门后面是哪个文件」**。

## 2. 前置知识

- **Rust 模块（module）**：一个 `.rs` 文件就是一个潜在的模块，但它必须被某个父模块用 `mod xxx;` 声明后才会被编译。crate 的根是 `src/lib.rs`（库 crate）或 `src/main.rs`（二进制 crate）。`pegainfer-kimi-k2` 是库 crate，它的全部模块都从 `src/lib.rs` 的 `mod` 声明长出来。
- **cargo feature 与 `#[cfg(feature = "...")]`**：feature 是编译期开关。开了某个 feature，标注了对应 `#[cfg(feature = ...)]` 的模块、函数、import 才会被编译进产物；没开就当这些代码不存在。这是纯编译期机制，**没有任何运行时开销**。
- **feature 的传导**：feature 可以依赖其他 crate 的 feature。例如本 crate 的 `kimi-k2 = ["pegainfer-kernels/kimi-k2"]` 表示「开我的 `kimi-k2`，同时把 `pegainfer-kernels` 的 `kimi-k2` 也打开」——这正是 u1-l1 讲过的「向 kernels 传导 CUDA 编译」的机制落点。
- **`required-features`（构建系统层）**：`Cargo.toml` 里 `[[bin]]` / `[[test]]` 目标可以声明 `required-features`。它和 `#[cfg]` 是两套互补的门：`#[cfg]` 管源码块，`required-features` 管整个构建目标（这个二进制/测试文件要不要参与编译）。
- **Rust 可见性阶梯**：`pub`（对外公开）> `pub(crate)`（crate 内公开）> `pub(in path)` / `pub(super)`（限定局部可见）> 默认私有。本讲会看到这个 crate 把四种级别全用上了。
- **历史快照文档**：这个仓库的 `docs/models/<line>/` 下很多文档是某次工作的**执行记录**（做了什么、踩了什么坑），写完就不再随代码演进而更新；它的新鲜度靠 TL;DR 和 `Last touched:` 日期表达（见仓库 CLAUDE.md 的文档工作流）。读它们必须带时间戳意识。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pegainfer-kimi-k2/src/lib.rs` | crate 根：所有模块声明与 feature 门、`start_engine` / `launch` / `KimiLaunchOptions` |
| `pegainfer-kimi-k2/Cargo.toml` | feature 定义与传导、`[[test]]` / `[[bin]]` 的 `required-features` |
| `pegainfer-kimi-k2/src/runner.rs` | runner 模块根：声明 runner 子树的 8 个子模块，re-export `start_engine` |
| `pegainfer-kimi-k2/src/weights.rs` | weights 模块根：文档注释明说扁平布局约定，声明 weights 子树并 re-export 类型 |
| `pegainfer-kimi-k2/src/runner/worker.rs` | runner 里最大的模块：rank worker 本体，也是 `worker/` 子目录的「共享头文件」 |
| `pegainfer-kimi-k2/src/batch_decode_trace.rs` | decode kernel 调用图 trace（模块级门 `kimi-k2`，运行时部分门 `kernel-call-trace`） |
| `pegainfer-kimi-k2/src/runner/worker/state.rs` | worker 命令处理，含一处 `kernel-call-trace` 采集钩子 |
| `docs/models/kimi-k2/source-layout.md` | 历史快照：一次文件拆分工作的记录（本讲用作「文档漂移」教学样本） |

其余文件本讲只在「模块树总览」里点名职责，深入讲解留给后续单元。

## 4. 核心概念与源码讲解

### 4.1 模块树总览

#### 4.1.1 概念说明

一个 30+ 文件的 crate，读源码的第一件事不是逐个打开，而是先建立**地图感**：哪棵子树管什么、数据从哪棵子树流向哪棵子树。

`pegainfer-kimi-k2` 的模块树有清晰的三层分工：

- **常量与配置**（`config`）：模型几何，无 feature 门，永远编译（u1-l2 已讲）。
- **weights 子树**：把磁盘上的 safetensors checkpoint 变成显存里的 GPU 权重（单元 2 逐文件精读）。
- **runner 子树**：启动引擎、调度请求、执行前向（单元 3–6 逐文件精读）。
- **外围**：观测工具（`kernel_report`、`batch_decode_trace`、`bin/`）与测试（`tests/`）。

#### 4.1.2 核心流程

模块树的「生长规则」：

1. `src/lib.rs` 用 `mod xxx;` 声明顶层模块 → 编译器去找 `src/xxx.rs` 或 `src/xxx/mod.rs`（本仓库只用前者）。
2. `src/xxx.rs` 再声明自己的子模块 → 对应 `src/xxx/yyy.rs`。如此递归。
3. 子模块想被父级以上使用，需要沿树向上 `pub(crate) use` / `pub(super) use` re-export；对外 API 只在 `lib.rs` 收口。
4. `#[cfg(...)]` 可以贴在任何 `mod` 声明上，把整棵子树按需「开关」。

#### 4.1.3 源码精读

先看 crate 根的模块声明区——这是整棵树的种子：

[src/lib.rs:19-34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L19-L34) 集中声明全部顶层模块，并给每个模块贴 feature 门：`batch_decode_trace` / `model_line` / `runner` / `typed_scratch` / `weights` 五个模块挂在 `kimi-k2` 门后，`kernel_report` 挂在 `kernel-report` 门后，只有 `config`（第 24 行 `pub(crate) mod config;`）**无门**、默认编译。

把这 34 行展开成完整模块树（叶子文件后面的一句话职责来自各文件的文档注释与代码内容，均为真实源码）：

```
src/
├── lib.rs                    # crate 根：模块声明 + feature 门 + start_engine/launch/KimiLaunchOptions
├── config.rs                 # [无门] 模型几何常量、probe_config_json 校验、KimiK2ParallelShape/KimiLocalDims
├── model_line.rs             # [kimi-k2] ModelLine trait 实现（MODEL_LINE、KimiCli 的 --ep-backend）
├── typed_scratch.rs          # [kimi-k2] 类型化解码 scratch 缓冲（尺寸编译期定型）
│   └── top1.rs               #   top-1 采样包缓冲（TOP1_PACKET_BYTES = 8）
├── weights.rs                # [kimi-k2] 权重模块根（显式说明扁平布局约定）
│   ├── manifest.rs           #   index.json 解析 → 权重清单 + rank 切分计划
│   ├── load.rs               #   mmap + 按 rank 切片加载上 GPU
│   ├── package.rs            #   Marlin INT4 打包后的 GPU 权重包
│   ├── context.rs            #   KimiRankGpuContext：每 rank 的 CUDA 上下文
│   └── tests.rs              #   [cfg(test)] 权重加载测试
├── runner.rs                 # [kimi-k2] runner 模块根（声明 8 个子模块）
│   ├── bringup.rs            #   start_engine：TP8/DP1 与 TP1/DP8 双拓扑启动
│   ├── config.rs             #   KimiK2RunnerConfig（含 kv_pool_pages 等启动配置）
│   ├── affinity.rs           #   线程绑核计划（调度线程 + rank worker 线程分核）
│   ├── load_balancer.rs      #   DpLoadBalancer：DP 路径按空槽选 rank
│   ├── scheduler.rs          #   TP8/DP1 单调度器 KimiK2Scheduler
│   │   ├── lifecycle.rs      #     请求生命周期 / KV 容量校验 / 发送调度结果
│   │   └── dp.rs             #     DpCoordinator：lock-step 驱动全部 DP rank
│   ├── executor.rs           #   ForwardExecutor trait + DP_MAX_BATCH_PER_RANK
│   │   ├── tp8_dp1.rs        #     TP8/DP1 执行器（单执行器持 8 个 worker，NCCL）
│   │   ├── tp1_dp8.rs        #     TP1/DP8 执行器（每 rank 一个，DeepEP）
│   ├── moe_nccl.rs           #   NCCL all-gather/reduce-scatter MoE 后端
│   ├── moe_deepep.rs         #   DeepEP elastic all-to-all MoE 后端
│   └── worker.rs             #   KimiRankWorker（rank 线程 + 命令通道）+ 共享常量
│       ├── cache.rs          #     物理 KV 页池 + decode arena
│       ├── load.rs           #     原始张量 → 层前向缓存装载与形状检查
│       ├── runtime.rs        #     集合通信 / RoPE / 采样等运行时 helper
│       ├── forward.rs        #     prefill/decode 前向 kernel 链
│       ├── state.rs          #     KimiRankThreadState：worker 命令处理
│       └── (内联 #[cfg(test)] mod tests)   # decode bucket / 页表单测
├── kernel_report.rs          # [kernel-report] 计时与统计提供者（复用 pegainfer-bench 框架）
├── batch_decode_trace.rs     # [kimi-k2] 静态 decode kernel DAG；运行时 trace 部分 [kernel-call-trace]
├── bin/
│   ├── kimi_kernel_report.rs # [kernel-report] per-op kernel 报告 CLI
│   └── kimi_model_report.rs  # [kernel-report] model 级 operator 报告 CLI
└── tests/
    └── vllm_golden_gate.rs   # [required-features = kimi-k2] vLLM golden 精度门禁
```

再看两个「模块根」文件，确认子树的组织方式：

[src/runner.rs:1-11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner.rs#L1-L11) 是 runner 子树的全部内容：8 行 `mod` 声明加一行 re-export——`pub(crate) use bringup::start_engine;` 把真正的引擎入口从子模块提到 runner 根，供 `lib.rs` 调用。

[src/weights.rs:64-83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L64-L83) 展示了 weights 子树同样的模式：`mod context; mod load; mod manifest; mod package;` 加 `#[cfg(test)] mod tests;`，随后一串 `pub(crate) use` 把 13 个类型（`KimiK2WeightManifest`、`KimiRankGpuWeights` 等）提升到 `crate::weights` 命名空间——runner 子树只通过这些名字消费权重，不直接摸 `weights::manifest` 内部。

runner 子树最深的一层在 worker：[src/runner/worker.rs:805-814](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L805-L814) 声明 `mod cache; mod load; mod runtime; mod forward; mod state;` 并 re-export runtime 的四个通信 helper；[src/runner/worker.rs:848-849](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L848-L849) 则是内联测试模块 `#[cfg(test)] mod tests`（覆盖 decode 桶取整与槽位页表），它与 `weights/tests.rs` 的「独立文件测试」是两种并存风格。

#### 4.1.4 代码实践

**实践目标**：亲手重建上面这棵模块树，验证「每个文件都有归属、每条 `mod` 声明都找得到文件」。

**操作步骤**：

1. 在仓库根目录运行：`rg --files pegainfer-kimi-k2 -g '*.rs'`（或 `find pegainfer-kimi-k2 -name '*.rs'`）。
2. 对每个文件，用 `rg -n "^mod |^pub" <文件>` 找它的子模块声明与导出，逐层把树画在纸上或笔记里。
3. 给树的每个叶子写一句话职责，再与本讲 4.1.3 的树对账。

**需要观察的现象**：文件清单总数、目录嵌套深度（最深 `src/runner/worker/` 和 `src/runner/executor/`，共三层 `.rs`）、是否存在 `mod.rs` 文件。

**预期结果**：`.rs` 文件共 35 个——`src/` 下 34 个（含 `bin/` 下 2 个）+ `tests/vllm_golden_gate.rs` 1 个；**没有任何 `mod.rs`**。（本讲写作时已用等价的文件枚举核实过这份清单。）另外你会发现树里没有 `layers/` 目录——这正是 4.4 节文档漂移的伏笔。

#### 4.1.5 小练习与答案

**练习 1**：`start_engine` 的函数体在哪个文件？沿模块树写出从 `lib.rs` 到它的调用链。

**答案**：[src/lib.rs:40-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L40-L42) 调 `runner::start_engine` → [src/runner.rs:11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner.rs#L11) re-export 自 `bringup` → 实体在 [src/runner/bringup.rs:51](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L51) `pub(crate) fn start_engine`。

**练习 2**：`ForwardExecutor` trait 有几个实现文件？trait 定义在哪？

**答案**：定义在 [src/runner/executor.rs:14](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/executor.rs#L14)（`pub(super) trait ForwardExecutor`），实现在 `executor/tp8_dp1.rs` 与 `executor/tp1_dp8.rs` 两个文件，由 [src/runner/executor.rs:1-6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/executor.rs#L1-L6) 声明并 `pub(super) use` 提升。

**练习 3**：单元测试有几种落位方式？各举一例。

**答案**：两种。独立文件式：`weights.rs` 里 `#[cfg(test)] mod tests;` 指向 `src/weights/tests.rs`；内联式：`worker.rs` 第 848-849 行直接在文件尾部写 `#[cfg(test)] mod tests { ... }`。

### 4.2 扁平模块布局约定

#### 4.2.1 概念说明

Rust 声明子模块有两种等价写法：

| 风格 | 模块根 | 子模块位置 |
| --- | --- | --- |
| 旧式（mod.rs） | `src/foo/mod.rs` | `src/foo/bar.rs` |
| **扁平式（本仓库）** | `src/foo.rs` | `src/foo/bar.rs` |

本仓库统一用**扁平式**：`weights.rs` 和 `weights/` 目录并排存在，`runner/worker.rs` 和 `runner/worker/` 并排存在。这个约定不是口口相传的，而是**写在代码里的**。

#### 4.2.2 核心流程

扁平布局在这个 crate 里的具体玩法：

1. 模块根（如 `weights.rs`）负责：声明子模块、集中 import 共享类型、`pub(crate) use` 提升子模块的类型——它同时是子树的「命名空间收口」和「共享头文件」。
2. 子模块（如 `weights/manifest.rs`、`worker/cache.rs`）开头一行 `use super::*;` 直接继承父模块的整个命名空间。
3. 父模块改动 import 或 re-export 会波及所有子模块——因此 crate 根特意为这种风格关掉了两个 pedantic lint。

#### 4.2.3 源码精读

约定的一手证据有三处：

[src/weights.rs:1-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L1-L5) 的文档注释直接说明：本文件是 `crate::weights` 模块根，兄弟目录 `weights/` 放实现模块，**遵循仓库的扁平 Rust 模块布局（`foo.rs` + `foo/`，无 `mod.rs`）**。

[src/lib.rs:6-12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L6-L12) 在 crate 级别为这个风格「买单」：注释说 `use super::*` 是紧密耦合子模块（`weights.rs + weights/*`、`runner/worker.rs + worker/*`）的扁平布局惯用法，权重结构体的字段名照抄 safetensors（`gate_proj`、`weight_packed`…）也更好读，因此显式 `#![allow(clippy::wildcard_imports, clippy::struct_field_names)]`。

子模块侧的呼应：[src/weights/manifest.rs:1-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L1-L5) 开头就是从父模块 `load` 借类型加一行 `use super::*;`；[src/runner/worker/cache.rs:1-3](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/cache.rs#L1-L3) 同样是 `use super::load::*; use super::runtime::*; use super::*;` 三连——worker.rs 顶部那几十行 import（[src/runner/worker.rs:1-90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L1-L90)）实际上是五个子模块共享的公共前缀。

#### 4.2.4 代码实践

**实践目标**：体会「模块根 = 子树共享头文件」这一耦合关系，理解改名的影响半径。

**操作步骤**：

1. 打开 `src/runner/worker.rs` 第 1–90 行，抄下它 import 的 `pegainfer_kernels::ops` 里的任意 3 个函数名（例如 `kimi_mla_split_qkv_a`、`kimi_flashinfer_batch_decode_mla_rt`）。
2. `rg -n "kimi_mla_split_qkv_a" pegainfer-kimi-k2/src/runner/worker/` 查这些名字在子目录里的使用位置。
3. 注意：子模块文件里**没有**对它们单独 `use`——它们是顺着 `use super::*` 从 `worker.rs` 继承来的。

**需要观察的现象**：子模块直接使用父模块 import 的符号，自身 import 区远比「自给自足」的模块短。

**预期结果**：例如 `forward.rs` / `runtime.rs` 里能搜到这些 kernel 名字的使用点，而对应的 `use` 语句只出现在 `worker.rs` 顶部。若有人把 `worker.rs` 的某个 import 删掉，会同时在多个子模块里引发「cannot find」编译错误——这正是 lib.rs 注释所说「tightly-coupled」的含义。

**小练习与答案**（见 4.2.5）之外，如果想在本地彻底验证编译耦合：运行 `cargo check -p pegainfer-kimi-k2 --features kimi-k2`（需要 CUDA 工具链，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：把 `worker.rs` 重命名/移动成 `worker/mod.rs`，功能上有区别吗？为什么仓库不这么做？

**答案**：功能完全等价（Rust 两种写法语义相同），区别只在组织与检索：扁平式让 `worker.rs` 与 `worker/` 目录并排出现，且与全仓库其他模块（`runner.rs`、`weights.rs`、`typed_scratch.rs`）风格统一。

**练习 2**：`weights/tests.rs` 和 `weights.rs` 是什么关系？为什么它默认不编译？

**答案**：它是 `crate::weights::tests` 子模块，由 [src/weights.rs:68-69](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L68-L69) 的 `#[cfg(test)] mod tests;` 声明；`cfg(test)` 意味着只有 `cargo test` 时才编译，普通构建不参与。

### 4.3 feature 门与可见性

#### 4.3.1 概念说明

u1-l1 已给出 feature 体系的鸟瞰（default 为空、`kimi-k2` 门住运行时、`kernel-report` 是观测超集）。本讲深入到**文件与行号级别**：每个 feature 到底门住哪些文件、门住什么内容、以及为什么要这样设计。

三个 feature 的定义与关系（来自 Cargo.toml）：

```
default           = []
kernel-call-trace = ["pegainfer-core/kernel-call-trace"]   # 只激活下游 core 的 trace 设施
kernel-report     = ["kimi-k2", "kernel-call-trace"]        # 超集：运行时 + trace + 报告
kimi-k2           = ["pegainfer-kernels/kimi-k2"]           # 运行时本体 + 激活 kernels 的 CUDA 编译
```

关键设计意图：**不开 feature 时，这个 crate 仍然是一个可编译、可被 workspace 依赖的轻量库**——只有 `config`（几何常量与探针）和一个会明确报错的 `start_engine` 桩。任何把 `pegainfer-kimi-k2` 当普通依赖引入的 crate，不会被拖进 CUDA 编译。

#### 4.3.2 核心流程

不同 feature 组合下，编译产物的差异：

```
cargo build -p pegainfer-kimi-k2                    # default
  → lib 含：config 全部常量/类型、pub use KIMI_K2_LAYERS、start_engine 桩（bail!）
  → 不含：runner/weights/typed_scratch/model_line/batch_decode_trace/kernel_report
  → 不含任何 [[bin]]、[[test]]

cargo build -p pegainfer-kimi-k2 --features kimi-k2
  → + model_line、runner 全树、weights 全树、typed_scratch
  → + batch_decode_trace（静态 DAG 部分）
  → + 真 start_engine / launch / KimiLaunchOptions
  → + tests/vllm_golden_gate（required-features = kimi-k2）
  → pegainfer-kernels 的 kimi-k2 CUDA 子模块被连带编译

cargo build -p pegainfer-kimi-k2 --features kernel-call-trace
  → 在 kimi-k2 产物之上（注意：单独开它并不会拉起 kimi-k2 模块树，见练习 2）
  → + batch_decode_trace::trace_runtime_decode_kernel_calls
  → + worker.rs / state.rs 内的 trace 采集钩子
  → pegainfer-core 的 kernel-call-trace 设施被激活

cargo build -p pegainfer-kimi-k2 --features kernel-report
  → = kimi-k2 + kernel-call-trace + kernel_report 模块 + 两个 bin
```

可见性阶梯在代码里四处可见，共同指向一个原则——**内部实现不外泄，公开面尽量小**：

| 级别 | 例证 | 含义 |
| --- | --- | --- |
| `pub` | `pub mod kernel_report`、`pub mod batch_decode_trace`、`pub use config::KIMI_K2_LAYERS` | crate 外可见——两个 trace 模块必须 pub，因为 `bin/` 下的两个二进制以 `pegainfer_kimi_k2::kernel_report::...` 方式 import |
| `pub(crate)` | `pub(crate) mod config`、`pub(crate) use bringup::start_engine`、weights.rs 的 13 个 re-export | crate 内自由使用，库使用者看不到 |
| `pub(in crate::runner)` | [src/runner/scheduler.rs:6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/scheduler.rs#L6) `pub(in crate::runner) mod dp;` | 只有 runner 子树能见 `dp`（DpCoordinator 不外泄） |
| `pub(super)` | [src/runner/executor.rs:5-6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/executor.rs#L5-L6)、[src/runner/worker.rs:78-79](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L78-L79) | 只对父模块（runner / worker）可见 |

#### 4.3.3 源码精读

**（1）feature 的定义与构建目标门。** [Cargo.toml:31-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L31-L35) 定义三个 feature 及其传导；[Cargo.toml:37-40](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L37-L40) 给集成测试 `vllm_golden_gate` 挂 `required-features = ["kimi-k2"]`；[Cargo.toml:42-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L42-L50) 给两个报告二进制挂 `required-features = ["kernel-report"]`——不开门，`cargo build --bins` 连这两个目标都不存在。

**（2）双版本 `start_engine`：桩与本体。** [src/lib.rs:38-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L38-L42) 是真实现（门 `kimi-k2`），转发给 `runner::start_engine`；[src/lib.rs:44-47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L44-L47) 是 `#[cfg(not(feature = "kimi-k2"))]` 的桩，直接 `anyhow::bail!("Kimi-K2 runtime is feature-gated; rebuild with --features kimi-k2")`。两份同名函数靠互斥的 cfg 共存——这是 feature 门最经典的形态：**API 形状不变，能力按需编译**。

**（3）一个模块、两级门。** `batch_decode_trace` 是最有教学价值的样本：模块级门在 [src/lib.rs:22-23](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L22-L23)（`#[cfg(feature = "kimi-k2")] pub mod batch_decode_trace;`）；而它**内部**靠近口处的 6 个 import（`pegainfer_core::ops::call_trace`、`EngineLoadOptions`、`GenerateRequest`、`TokenEvent`、`TokenSink`、`SamplingParams`）以及 `trace_runtime_decode_kernel_calls` 函数本体，再各自挂一层 `kernel-call-trace` 门：[src/batch_decode_trace.rs:3-14](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/batch_decode_trace.rs#L3-L14) 与 [src/batch_decode_trace.rs:118-119](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/batch_decode_trace.rs#L118-L119)。因此静态 trace（`trace_decode_kernel_calls`，纯形状推演、无需 GPU）在 `--features kimi-k2` 下就能用，而**运行时** trace（真的启动引擎跑一步）要 `kernel-call-trace`。

**（4）运行时采集钩子。** [src/runner/worker.rs:24-25](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L24-L25) 把 `pegainfer_core::ops::call_trace` 的 import 门在 `kernel-call-trace` 后；[src/runner/worker/state.rs:238-253](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L238-L253) 在 decode 步骤里，当 `rank == 0 && call_trace::is_enabled()` 时调用 `batch_decode_trace::trace_decode_kernel_calls` 把整层 kernel DAG 记录进 trace。不开 feature 时这两段代码不存在，decode 热路径分毫未动。

**（5）feature → 模块映射表**（本讲实践任务的参考答案，依据是对 `src/` 全量 `cfg(feature` 搜索）：

| feature | 门住的内容 | 证据位置 |
| --- | --- | --- |
| 无（default） | `config` 模块、`pub use KIMI_K2_LAYERS`、`start_engine` 报错桩 | lib.rs:24、36、44-47 |
| `kimi-k2` | `model_line`、`runner` 全树、`weights` 全树、`typed_scratch`、`batch_decode_trace`（静态部分）、真 `start_engine`/`launch`/`KimiLaunchOptions`；`vllm_golden_gate` 测试 | lib.rs:19-34、38-90；Cargo.toml:37-40 |
| `kernel-call-trace` | `batch_decode_trace.rs` 内 6 个 import + `trace_runtime_decode_kernel_calls`；`worker.rs:24` 的 `call_trace` import；`state.rs:238` 的采集钩子；激活 `pegainfer-core/kernel-call-trace` | 三处文件行号如上；Cargo.toml:33 |
| `kernel-report` | `kernel_report` 模块 + `kimi_kernel_report` / `kimi_model_report` 两个 bin（= kimi-k2 + kernel-call-trace 之和再加观测面） | lib.rs:25-26；Cargo.toml:34、42-50 |

注意一个细节：`kernel-call-trace` 单独开启时**并不会**把 `runner`/`weights` 等模块拉进来（它们只听 `kimi-k2` 的门），但 `state.rs` 的钩子代码在 `kimi-k2` 开启且 `kernel-call-trace` 也开启时才两头都满足——实践中总是通过 `kernel-report`（超集）一起开。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手枚举 crate 内全部 feature 门，产出一张与 4.3.3（5）对照的 feature → 模块映射表。

**操作步骤**：

1. 在仓库根目录运行：`rg -n "cfg\(feature" pegainfer-kimi-k2/src`。
2. 把每一处命中按「文件 + 行号 + 门的名字 + 门住的是什么（模块/import/函数/代码块）」整理成清单。
3. 归类到 `kimi-k2` / `kernel-report` / `kernel-call-trace` 三类，画成映射表。
4. 用三条命令交叉验证（需要 CUDA 工具链的构建步骤**待本地验证**）：
   - `cargo tree -e features -p pegainfer-kimi-k2 --features kernel-report` 观察 feature 传导；
   - `cargo build -p pegainfer-kimi-k2` 后确认 `target/` 下没有 `kimi_kernel_report` 二进制；
   - `cargo build -p pegainfer-kimi-k2 --features kernel-report` 后确认出现了它。

**需要观察的现象**：命中集中在哪几个文件；`not(feature` 只有几处；`Cargo.toml` 的 `required-features` 与源码 `#[cfg]` 如何分工。

**预期结果**（本讲写作时已用 ripgrep 等价搜索核实）：源码中的 `cfg(feature` 共约 21 处，分布只有 4 个文件——

| 文件 | 命中行 | feature |
| --- | --- | --- |
| `src/lib.rs` | 19, 22, 25, 27, 29, 31, 33, 38, 44(`not`), 53, 63 | `kimi-k2` ×10、`kernel-report` ×1 |
| `src/batch_decode_trace.rs` | 3, 5, 7, 9, 11, 13, 118 | `kernel-call-trace` ×7 |
| `src/runner/worker.rs` | 24 | `kernel-call-trace` ×1 |
| `src/runner/worker/state.rs` | 238 | `kernel-call-trace` ×1 |

`runner/` 与 `weights/` 两个子树里**除上述两处 trace 钩子外没有任何 feature 门**——它们的整棵树只被 `lib.rs` 的模块级门一次性开关。这份「门极少、且集中在根部」的分布本身就是设计信息：门的粒度是模块，不是函数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `kernel_report` 和 `batch_decode_trace` 是 `pub mod`，而 `runner`、`weights`、`typed_scratch` 是私有 `mod`？

**答案**：两个 trace 模块要被 crate 外的二进制使用——`src/bin/kimi_kernel_report.rs` 以 `pegainfer_kimi_k2::kernel_report::MeasuredCall` 等路径 import（[src/bin/kimi_kernel_report.rs:13-18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/bin/kimi_kernel_report.rs#L13-L18)），所以必须 `pub`；runner/weights/typed_scratch 只被 crate 自己用，私有 + `pub(crate)` re-export 即可，避免膨胀库的公开 API。

**练习 2**：只开 `--features kernel-call-trace`（不开 `kimi-k2`）时，`trace_runtime_decode_kernel_calls` 能用吗？

**答案**：不能。它的容器模块 `batch_decode_trace` 被 `kimi-k2` 门住（lib.rs:22-23），模块不存在时内部的 `kernel-call-trace` 门无从谈起——外层门先决。要同时具备两个 feature，最省事的方式是开超集 `kernel-report`。

**练习 3**：`pub(in crate::runner) mod dp;` 换成 `pub(crate) mod dp;` 会破坏什么吗？

**答案**：功能上多半仍能编译，但它会把 `scheduler::dp`（DpCoordinator）暴露给整个 crate，违背「最小可见面」的意图；当前写法把它限定在 runner 子树内，因为只有 bringup（同属 runner）需要构造 DpCoordinator。

### 4.4 文档快照的历史性

#### 4.4.1 概念说明

`docs/models/kimi-k2/source-layout.md` 记录的是 2026-05 一次「把超大文件按职责拆分」的重构工作。这个仓库的文档哲学（见 CLAUDE.md「Team Documentation Workflow」）是：**文档新鲜度由 TL;DR 与 `Last touched:` 表达，不靠目录位置**；完成的工作记录与领域文档共存，不设归档目录。因此读这类文档的第一课是：它描述的是**写下那一刻**的代码，今天的代码可能已经又走了几步。

#### 4.4.2 核心流程

「文档主张 → 源码验证」的标准流程：

1. 从文档提取**可验证主张**（具体的文件名、行数、目录结构、命令）。
2. 对每条主张用 `Glob` / `rg` 在当前 HEAD 上验证。
3. 标注三态：**仍然准确 / 已过时 / 无法验证**。
4. 对已过时的部分，不要直接否定文档——先判断它是否本就是历史记录（执行日志），过时是预期行为。

#### 4.4.3 源码精读

[docs/models/kimi-k2/source-layout.md:1-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md#L1-L5) 的 TL;DR 宣称「当前 `src` 下最大的 Rust 文件是 `layers/attention.rs`，950 行」，并标注 `Last touched: 2026-05`——这两行就是本讲要检验的主张与时间戳。

逐条核对结果：

| 文档主张 | 出处 | 当前状态 |
| --- | --- | --- |
| 拆分前 `runner/worker.rs` 2799 行、`layers/attention.rs` 1146 行、`layers/experts.rs` 1008 行 | [source-layout.md:28-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md#L28-L31) | **已过时（部分）**：`runner/worker.rs` 仍存在（今天 917 行，含尾部内联测试），但 `src/layers/` 目录**整个不存在**——4.1.4 的文件清单里没有任何 `layers/` 路径 |
| attention 张量包装移到 `layers/attention/tensors.rs`、专家测试移到 `layers/experts/tests.rs` | [source-layout.md:33-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md#L33-L37) | **已过时**：这些目标路径同样不存在，说明 `layers/` 子树后来又被整体重组（注意力/专家逻辑现在分布在 `runner/worker/` 的 runtime/forward 等文件里，后续单元会读到） |
| `worker.rs` 按五个所有权边界拆成 `state.rs` / `cache.rs` / `forward.rs` / `load.rs` / `runtime.rs` | [source-layout.md:41-45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md#L41-L45) | **仍然准确**：与今天的 [src/runner/worker.rs:805-814](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L805-L814) 的 `mod` 声明一一对应 |
| 默认构建只保留 config/tokenizer 探测，kernel 后端运行时藏在 `kimi-k2` feature 后 | [source-layout.md:50-51](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md#L50-L51) | **仍然准确**：与 4.3 节的门清单完全一致 |
| TL;DR「最大文件是 layers/attention.rs 950 行」 | [source-layout.md:3](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md#L3) | **已过时**：`layers/` 不存在；今天的 `runner/worker.rs` 为 917 行，但「哪个文件最大」未逐一核实，**待确认** |

结论：这篇文档的**过程性内容**（怎么拆、为什么拆、踩了什么坑，见 [source-layout.md:53-61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md#L53-L61) 的 Debrief）仍然有价值；它的**目录清单**已经部分失真。这正是 CLAUDE.md 说的「doc body is authoritative，但 freshness 靠 TL;DR/日期自证」——读者要自己做时间换算。

#### 4.4.4 代码实践

**实践目标**：独立完成一次「文档主张 → 代码落点」的漂移审计，形成可复用的读文档方法。

**操作步骤**：

1. 重读 `docs/models/kimi-k2/source-layout.md` 全文，把每条含文件路径/行数/结构的主张抄成清单（至少 6 条：3 个大文件行数、2 次 layers 拆分、worker 五拆、feature 门总结）。
2. 对每条主张运行验证：`rg --files pegainfer-kimi-k2/src -g '*.rs'`（路径存在性）、`wc -l <文件>`（行数）、`rg -n "mod (cache|load|runtime|forward|state)" pegainfer-kimi-k2/src/runner/worker.rs`（拆分结构）。
3. 给每条标「仍然准确 / 已过时 / 待确认」，写出证据（命令输出片段）。
4. 回答一个判断题：这篇文档应该被删除吗？用 CLAUDE.md 的文档工作流原则（「完成的工作与领域文档共存」「if a doc no longer earns its keep, delete it」）给出你的理由。

**需要观察的现象**：路径验证里 `layers` 相关查询零命中；行数验证里 `worker.rs` 远小于文档记载的 2799 行（拆分确实发生了）；模块声明验证里五个 `mod` 全部命中。

**预期结果**：得到一张类似 4.4.3 表格的审计表；核心发现是「worker 拆分长存、layers 子树已消失」。第 4 步的判断题没有标准答案，但合理的论证应包含：文档的 Debrief（pitfalls/lessons，第 53-61 行）仍承载不可再生的经验（例如「Kimi default-feature 编译应保留 config 探测、运行时藏 feature 后」这一教训直接解释了今天 lib.rs 的结构），因此不满足「不再产出价值」的删除条件。

#### 4.4.5 小练习与答案

**练习 1**：`docs/models/kimi-k2/` 下还有 dp-design.md、deepep-migration.md 等设计文档。它们一定描述当前代码吗？读之前先看什么？

**答案**：不一定。先看文件头部的 TL;DR 与 `Last touched:` 日期（本仓库文档约定的「新鲜度合同」），再对关键主张做源码交叉验证——本讲的审计方法对它们同样适用。

**练习 2**：为什么本讲不把 source-layout.md 的过时当作「文档写错了」？

**答案**：它是一次重构工作的**执行记录**（Execution Log / Debrief 结构），价值在决策过程与踩坑；CLAUDE.md 明确这类文档的新鲜度由 TL;DR 自证、允许与现状有距离，「目录清单过时」是这类文档的预期演化路径，不是错误。

**练习 3**：如果你想确认 `layers/` 是什么时候消失的，用哪条 git 命令？

**答案**：`git log --oneline --diff-filter=D -- 'pegainfer-kimi-k2/src/layers/*'`（列出删除这些文件的提交；属只读命令）。再配合 `git show <commit>` 看当时的重组方式。

## 5. 综合实践

把本讲四块内容串成一份**《Kimi-K2 crate 速查表》**（放个人笔记即可，不写入仓库）：

1. **模块树**：手绘 4.1.3 的树，遮住职责列自测复述，重点检验 `runner/` 与 `weights/` 两棵子树（各 13 与 5 个文件）。
2. **feature 矩阵**：做一张三列表格——行是 8 个顶层模块 + 2 个 bin + 1 个 test，列是 `default` / `kimi-k2` / `kernel-report`，格子里填「编译 / 不编译」，并在表下注明每列的「额外产物」（桩、golden 测试、报告二进制）。
3. **门清单**：附上你自己在 4.3.4 跑出的 `rg` 命中清单，与 4.3.3（5）的参考表对账。
4. **漂移附录**：摘录 4.4.4 审计表里标「已过时」的两条主张，各配一句「今天的真相」。

验收标准：不打开任何源码，仅凭这张速查表回答——「`KIMI_K2_LAYERS` 常量在 default 构建下可用吗？」「`kimi_model_report` 二进制要开什么 feature？」「DpCoordinator 在哪个文件、什么可见性？」——三题全对即通过。

## 6. 本讲小结

- `pegainfer-kimi-k2/src` 共 35 个 `.rs` 文件、三层深，由 `lib.rs` 的 7 个顶层 `mod` 声明长出；`weights/` 管「checkpoint → 显存」，`runner/` 管「启动 → 调度 → 前向」，外围是 trace/bin/tests。
- 仓库统一**扁平模块布局**（`foo.rs` + `foo/`，无 `mod.rs`），且模块根兼任子树共享头文件——子模块靠 `use super::*` 继承父模块命名空间，lib.rs 专门为此关掉两个 pedantic lint。
- feature 门**集中在根部**：`lib.rs` 的模块级门一次开关整棵子树；全 crate 源码里 `cfg(feature` 只有约 21 处、分布在 4 个文件；`kimi-k2` 传导给 `pegainfer-kernels`，`kernel-report` 是 `kimi-k2 + kernel-call-trace` 的超集。
- 默认构建只有 `config` 模块和 `start_engine` 报错桩——crate 可以被无害地当普通依赖引用；`[[bin]]`/`[[test]]` 靠 `required-features` 在构建系统层把门。
- 可见性四级阶梯（`pub` / `pub(crate)` / `pub(in crate::runner)` / `pub(super)`）把公开面压到最小：只有 trace 相关模块对外公开，因为外部 bin 要用。
- `source-layout.md` 是历史快照：worker 五拆的记载今天仍准确，但 `layers/` 子树已整体消失——读模型文档必须先看 `Last touched:`，再对主张做源码交叉验证。

## 7. 下一步学习建议

- **下一讲 u1-l4《构建与运行》**：把本讲的地图变成可运行的现实——亲手用 `--features kimi-k2` 构建并启动引擎，观察 `start_engine` → `bringup` 的启动日志，理解 `--ep-backend` 与两条拓扑路径。
- 若想趁热打铁读源码，推荐两个无 GPU 也能读的目标：`src/runner/bringup.rs:51` 起的 `start_engine`（下一单元的主角），以及 `src/batch_decode_trace.rs` 的静态 kernel DAG（它把整个 decode 前向链路用「形状描述」的方式写了一遍，是 u5-l1 前向链路的预告片）。
- 带着本讲的速查表进入单元 2：`weights/manifest.rs` 是第一个被逐行精读的文件。
