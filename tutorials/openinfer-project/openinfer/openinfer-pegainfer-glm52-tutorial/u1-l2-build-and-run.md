# 构建与运行：feature、CLI 与环境变量

> 本讲编号：u1-l2（beginner）。前置：u1-l1（GLM5.2 架构速览与 crate 定位）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `--features glm52` 完成 GLM5.2 的 release 构建，并解释为什么必须 `--release`。
2. 逐一说出 `Glm52Cli` 中 8 个 GLM5.2 专属 CLI 参数的用途、默认值与约束。
3. 分清「共享参数」（frontend 定义）与「模型专属参数」（各 ModelLine 注入）的归属，理解 `consumed_shared_args` 的含义。
4. 掌握运行期环境变量 `GLM52_DECODE_SLOTS` 与 `GLM52_MTP_DRAFTS` 的取值范围、默认值，以及二者共同满足的「96 行步预算」约束。

## 2. 前置知识

- **cargo feature（特性门控）**：Rust 的条件编译开关。PegaInfer 把每条模型线放在一个 feature 后面（默认只开 `qwen3`），所以默认构建是纯 Rust + CUDA、不含任何 Python 依赖；换模型线时用 `--features` 显式打开。
- **clap 与「合并式」命令行**：server 二进制只有一个命令行界面。frontend 定义共享参数（如 `--model-path`、`--tp-size`），每条模型线再通过 `augment_cli` 把自己的专属参数段合并进同一个界面；解析后各模型线从同一份 matches 里取走自己的字段。
- **debug 与 release 的差别**：GPU kernel、CUDA Graph 捕获这类路径在未优化构建下会慢几个数量级。仓库 CLAUDE.md 明确要求始终用 `--release`——debug 构建对 GPU/CUDA 极慢、会超时。
- **环境变量与 CLI 的分工**：CLI 参数描述「这次启动要什么」（拓扑、drafter、上下文上限）；`GLM52_*` 环境变量是更细的部署 profile 旋钮（decode 槽位数、草稿深度），它们没有 CLI 形态，进程内用 `OnceLock` 只读一次。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml` | crate 清单：本 crate 没有自己的 `[features]` 段，但依赖 `pegainfer-kernels` 时打开了它的 `glm52` feature |
| `src/model_line.rs`（502 行） | `Glm52Cli`（8 个专属 flag）、`ModelLine` 实现的 `probe`/`validate`/`serve_plan`/`launch`，以及 18 个 CLI 校验单测 |
| `src/lib.rs`（2097 行） | `Glm52LaunchOptions`、`Glm52MoeTopo` 拓扑枚举、`launch()` 启动入口 |
| `src/model/mod.rs` | `glm52_decode_slots()` 读取 `GLM52_DECODE_SLOTS`，`GLM52_MAX_STEP_ROWS = 96` |
| `src/mtp.rs` | `glm52_mtp_draft_len()` 读取 `GLM52_MTP_DRAFTS` |

表中行数来自 `wc -l` 实测；后两个文件只取本讲相关的两个函数讲解。

## 4. 核心概念与源码讲解

### 4.1 构建与 feature 门控

#### 4.1.1 概念说明

PegaInfer 是多模型推理引擎：一条模型线一个 crate、一个 feature。CLAUDE.md 的模型表写明 GLM5.2 的启用方式是 `--features glm52`，架构是「MLA + MoE + FP8，8-GPU EP（bring-up）」。

这里有一个容易误解的点：**`pegainfer-glm52` 这个 crate 自己没有 `[features]` 段**。feature 门控发生在组装 server 二进制的更上层——`glm52` feature 决定是否把本 crate 拉进构建图；而本 crate 一旦参与编译，就会顺手把 `pegainfer-kernels` 的 `glm52` feature 打开（见下方 `Cargo.toml` 的依赖声明），CUDA 侧的 glm52 kernel 由 `pegainfer-kernels` 的 `build.rs` 统一用 nvcc 编译。

（server 侧 `glm52` feature 的声明位于本 crate 之外的 server 包清单，本讲不展开，细节留到 u1-l4 对照确认。）

#### 4.1.2 核心流程

从敲下命令到进程就绪，构建与选择链路是：

1. `cargo run --release --features glm52 -- --model-path models/GLM5.2`
   - `--release`：GPU 路径必须优化编译；
   - `--features glm52`：把 `pegainfer-glm52` 拉进本次构建。
2. 依赖解析：`pegainfer-glm52` 的依赖声明又打开 `pegainfer-kernels` 的 `glm52` feature，其 `build.rs` 开始编译 glm52 的 CUDA 源。
3. 运行期 server 读取 `--model-path` 下的 `config.json`，依次调用各模型线的 `ModelLine::probe` 自检。
4. `Glm52Line::probe` 要求 `model_type == "glm_moe_dsa"`，再交给 `probe_config_json` 逐字段钉死几何常量（u2-l1 详解）。
5. 被选中的线进入 `validate`（本讲 4.2），随后 `serve_plan` 与 `launch`（u1-l4 详解）。

#### 4.1.3 源码精读

[Cargo.toml:L21-L21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/Cargo.toml#L21-L21)
依赖声明打开 kernels 的 glm52 feature——本 crate 一参与构建，CUDA 侧 glm52 kernel 就随之编译。

[model_line.rs:L89-L100](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L89-L100)
`impl ModelLine for Glm52Line` 的开头：`name()` 返回 `"GLM5.2"`；`probe()` 检查 `config.json` 的 `model_type` 必须是 `"glm_moe_dsa"`，否则报错退出，再调用 `crate::probe_config_json` 做几何校验。

[lib.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1-L9)
crate 顶部文档给出定位句：GLM5.2 引擎面（TP4 replicated、EP4..EP64 free-running）——启动期校验官方 FP8 checkpoint 布局、按 rank 切片加载权重、每个逻辑 DP rank 一个自治引擎。

#### 4.1.4 代码实践：观察 feature 拉动

1. 实践目标：亲眼确认「编译 pegainfer-glm52 就会连带编译 glm52 的 CUDA」。
2. 操作步骤：
   - 在仓库根执行 `cargo build --release -p pegainfer-glm52`（需要本机有 CUDA 工具链，nvcc 可用；编译本身不要求有 GPU 设备）。
   - 观察构建日志里 `pegainfer-kernels` 的编译动作；也可前置 `PEGAINFER_BUILD_TIMING=1` 打印分阶段耗时（该变量由 kernels 的 build.rs 读取，属于构建期旋钮）。
3. 需要观察的现象：`pegainfer-glm52` 这个包自身只有纯 Rust 代码，重活在依赖 `pegainfer-kernels` 的构建脚本里。
4. 预期结果：构建成功即证明 feature 链路（glm52 → kernels/glm52）通畅。
5. 本机若无 CUDA 工具链则无法完成，此步「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pegainfer-glm52` 自己不定义 `[features]`？

**答案**：模型线的开关在 server 组装层——`glm52` feature 决定是否把本 crate 纳入构建。本 crate 一旦被编译就意味着这条线被选中，因此它直接在依赖里打开 `pegainfer-kernels` 的 `glm52` feature（`Cargo.toml` L21），不需要再给自己设门。

**练习 2**：为什么必须 `--release`？

**答案**：GPU kernel 与 CUDA Graph 捕获路径在 debug 构建下慢几个数量级，CLAUDE.md 原话是 debug builds「extremely slow for GPU/CUDA and will timeout」。

**练习 3**：与 `qwen35` 相比，glm52 线构建最大的省心之处是什么？

**答案**：qwen35 需要构建期 Python + Triton 做 AOT kernel 生成；glm52 线不需要任何 Python（CLAUDE.md 模型表），纯 Rust + CUDA 工具链即可。

### 4.2 Glm52Cli 逐项解读

#### 4.2.1 概念说明

server 只有一个命令行界面，但参数分两拨：

- **共享参数**：由 `pegainfer-frontend` 定义，所有模型线可见，例如 `--model-path`、`--tp-size`、`--dp-size`、`--no-prefix-cache`、`--kv-offload` 系列、`--dump-graph-png`、`--dflash-draft-model-path`。
- **模型专属参数**：由各模型线的 `augment_cli` 注入，GLM5.2 的 8 个专属 flag 全部定义在 `Glm52Cli` 结构体里。

归属的关键在 `consumed_shared_args`：GLM5.2 声明「这 11 个共享 flag 的语义由我接管」——`tp_size`、`dp_size`、`dflash_draft_model_path`、`no_prefix_cache`、`kv_offload`、`kv_offload_host_gib`、`kv_offload_hugepages`、`kv_p2p_metaserver_addr`、`kv_p2p_advertise_addr`、`kv_p2p_nics`、`dump_graph_png`。同名 flag 在别的模型线下可能有别的含义（或不被消费），这就是「参数归属」的含义。

#### 4.2.2 核心流程

一条 argv 从解析到启动的路径：

1. frontend 的 `parse_for_line` 把 argv 解析成（共享参数, matches, provided 显式给出集合）三元组。
2. `cli(ctx)` 用 `Glm52Cli::from_arg_matches` 从合并 matches 里取出专属段。
3. `moe_topo(&cli)` 用 `FromStr` 把 `--moe-topo` 字符串解析成 `Glm52MoeTopo` 枚举。
4. `validate` 跑一遍组合规则（下文逐条列出）。
5. `serve_plan` 决定前端调度分区数与 prefill-only 标记。
6. `launch` 选出 drafter 三态之一，把共享参数与专属参数组装成 `Glm52LaunchOptions`，调用 `crate::launch`。

#### 4.2.3 源码精读

**8 个专属参数一览**（定义于 [model_line.rs:L28-L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L28-L75) 的 `Glm52Cli`）：

| flag（定义行） | 类型与默认值 | 作用与约束 |
| --- | --- | --- |
| `--max-model-len`（L32） | `Option<usize>`，缺省 | 每请求上下文上限：`prompt + max_tokens - 1` 必须放下。缺省时按权重加载后的空闲 VRAM（fleet 最小值）推导；显式给值仍会对照该预算校验，超预算在启动期就失败 |
| `--glm52-prefill-only`（L36） | `bool`，默认 false | TP4 prefill-only + 前缀缓存、无 decode 的 serving 形态 |
| `--glm52-prefill-chunk-size`（L40） | `usize`，默认 16384 | 每个 prefill chunk 的 token 行数，必须是 64 的正倍数（`GLM52_PREFILL_CHUNK_ALIGN`），且仅 prefill-only 下合法 |
| `--moe-topo`（L50） | `String`，默认 `"ep8"` | MoE 分片拓扑，六选一：ep4/ep8/ep16/ep32/ep64/tp4，派生量见下表 |
| `--glm52-native-mtp`（L54） | `bool` | 用 checkpoint 原生 MTP 层做投机 drafter；要求 EP decode 或 TP4 prefill-only，与 `--dflash-draft-model-path` 互斥 |
| `--glm52-weight-staging`（L60） | `bool` | 权重经 pinned 双缓冲 staging 加载：热页缓存显著加速，冷网络文件系统应关闭 |
| `--glm52-ranks`（L67） | `Option<String>`，形如 `4..8` | 本进程 host 的全局 DP rank 区间；缺省 = 整个拓扑（单节点）；部分区间即多进程跨节点形态，必须配 `--glm52-rendezvous` |
| `--glm52-rendezvous`（L73） | `Option<String>`，`host:port` | bootstrap 握手地址：host 全局 rank 0 的进程 bind 并分发 DeepEP unique id，其余进程来取。一次性握手，没有运行时控制面 |

[model_line.rs:L102-L104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L102-L104)
`augment_cli`：把 `Glm52Cli` 的参数段合并进 server 的 clap Command——这就是「专属参数注入共享界面」的机制点。

[model_line.rs:L106-L120](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L106-L120)
`consumed_shared_args`：逐字列出被 GLM5.2 接管的 11 个共享 flag 名。

**拓扑枚举与派生量**（枚举 [lib.rs:L200-L216](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L200-L216)，方法 [lib.rs:L218-L287](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L218-L287)）：

| `--moe-topo` | 设备数 | 默认 `--dp-size` | 要求 `--tp-size` | 每 rank 整专家数 | 定位 |
| --- | --- | --- | --- | --- | --- |
| `ep8`（默认） | 8 | 8 | 1 | 32 | 高吞吐主力：DeepEP dispatch/combine，decode bucket 1..=8（CLI 文档原话 buckets 1-8） |
| `ep4` | 4 | 4 | 1 | 64 | GB300 四卡高吞吐拓扑，与 EP8 同一 DeepEP 协议、独立 shim 实例化 |
| `ep16` | 16 | 16 | 1 | 16 | GB300 NVL72 跨 tray 宽度，每 tray 一个进程经 `--glm52-ranks` 各 host 自己的 rank |
| `ep32` | 32 | 32 | 1 | 8 | 同上，更宽 |
| `ep64` | 64 | 64 | 1 | 4 | 同上，最宽 |
| `tp4` | 4 | 1 | 4 | 镜像全部专家（无此概念） | GB300 prefill-only 张量并行（NCCL all-reduce），必须配 `--glm52-prefill-only` |

派生关系（全部可在 [lib.rs:L218-L287](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L218-L287) 找到）：

- `default_dp_size()`：tp4 是 1，其余等于设备数。
- `expected_tp_size()`：tp4 是 4，其余是 1。
- `ep_local_experts()`（[lib.rs:L275-L283](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L275-L283)）= `GLM52_ROUTED_EXPERTS / expected_ep_size()`：ep8 → 32、ep4 → 64、ep16 → 16、ep32 → 8、ep64 → 4。tp4 是 tensor-replicated（`uses_tensor_replicated_moe`），此方法对它无意义。
- 合法字符串由 [lib.rs:L289-L309](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L289-L309) 的 `FromStr` 白名单钉死；非法值（如 `tp2`）报「must be ep4, ep8, ep16, ep32, ep64, or tp4」。
- 交叉验证：拓扑自身有一组单测 `topology_tests`（[lib.rs:L311](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L311) 起），逐个变体断言这些派生量。

小注：ep4 的专家 GEMM 路径两处文档表述不同——CLI 注释（[model_line.rs:L44-L49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L44-L49)）说 weight-only expert GEMM，枚举注释（[lib.rs:L200-L216](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L200-L216)）说 SM100 DeepGEMM masked 链。以枚举注释为准，细节留到 u5 讲 MoE 时对源码核实。

**validate 的组合规则**（[model_line.rs:L122-L210](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L122-L210)）：

- `--dp-size` 若显式给出，必须等于拓扑的 `default_dp_size()`，否则报错并提示正确值（省略则取默认）。
- `--tp-size` 必须等于 `expected_tp_size()`：ep 系要求 1，tp4 要求 4（tp4 连「省略」都不行——共享默认是 1，单测 `tp4_rejects_omitted_tp_size` 钉住了这一点）。
- `--glm52-prefill-only` 必须 `--moe-topo=tp4`；必须保留前缀缓存（拒 `--no-prefix-cache`）；拒 DSpark drafter；KV offload 必须配 `--glm52-native-mtp`；拒 `--dump-graph-png`（prefill-only 没有 decode 图可导）。
- `--glm52-prefill-chunk-size` 只在 prefill-only 下合法，且必须是 64 的正倍数。
- `--glm52-native-mtp` 只允许 EP decode（ep4..ep64）或「TP4 且 prefill-only」，且与 `--dflash-draft-model-path` 互斥。

**serve_plan 与 launch**：

- [model_line.rs:L211-L227](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L211-L227)：前端调度分区数 = `--glm52-ranks` 区间长度（部分 fleet 时）或拓扑 `logical_rank_count()`；tp4 镜像形态恒为 1。
- [model_line.rs:L229-L283](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L229-L283)：drafter 三态（`NativeMtp` / `Dspark(path)` / `None`）；`kv_offload_host_gib` 换算成字节；两个 p2p 地址（metaserver + advertise）齐备才启用 `Glm52P2pOptions`；最终组装 [lib.rs:L144-L195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L144-L195) 的 `Glm52LaunchOptions` 并调用 `crate::launch`。

#### 4.2.4 代码实践：跑 CLI 校验单测

1. 实践目标：用 18 个纯 CPU 单测验证本节全部规则，并亲手预测报错文案。
2. 操作步骤：
   - `cargo test --release -p pegainfer-glm52 --lib model_line -- --nocapture`
   - 再跑拓扑单测：`cargo test --release -p pegainfer-glm52 --lib topology_tests`
3. 需要观察的现象：`model_line` 模块的 18 个测试（`accepts_graph_png_dump` 到 `serve_plan_counts_partial_fleet_ranks`，[model_line.rs:L285-L502](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L285-L502)）全部通过。
4. 预期结果示例：`rejects_unknown_moe_topo`（L400 起）断言报错文案含「ep4, ep8, ep16, ep32, ep64, or tp4」；`ep4_rejects_non_dp4`（L417 起）断言文案含「--dp-size=4」；`prefill_only_rejects_decode_features`（L437 起）逐个试 `--no-prefix-cache`、DSpark、`--dump-graph-png` 三种非法叠加。
5. 说明：编译期需要 CUDA 工具链（nvcc），但测试本身只做 clap 解析与规则判断、不初始化 GPU。运行输出「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`--moe-topo tp4 --tp-size 4 --glm52-native-mtp`（不带 prefill-only）能通过 validate 吗？

**答案**：不能。native MTP 只允许 EP decode 或「TP4 且 prefill-only」，此组合命中 validate 尾部的组合检查（[model_line.rs:L122-L210](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L122-L210)），报「--glm52-native-mtp requires EP decode (--moe-topo=ep4 or ep8 and up) or TP4 prefill-only」。对应单测 `native_mtp_rejects_tp_topology`（L341 起）。

**练习 2**：`--moe-topo ep8 --glm52-ranks 4..8`（其余省略）是什么部署形态？

**答案**：部分 fleet 的多进程形态：本进程只 host 全局 DP rank 4..8，必须再给 `--glm52-rendezvous`；`serve_plan` 的分区数为 4（单测 `serve_plan_counts_partial_fleet_ranks`，L477 起）。

**练习 3**：为什么 `--max-model-len` 缺省反而是默认推荐？

**答案**：缺省时引擎按权重加载后的空闲 VRAM（取 fleet 最小值）自动推导上限；显式给值也可以，但同样对照该预算校验，给一个装不下的值会在启动期失败而不是第一个长请求时才炸（字段文档见 [lib.rs:L144-L195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L144-L195)，推导算法 u2-l4 详解）。

### 4.3 运行期环境变量

#### 4.3.1 概念说明

`GLM52_DECODE_SLOTS` 与 `GLM52_MTP_DRAFTS` 是两个部署 profile 旋钮：前者决定每个 DP rank 的 decode 槽位（并发请求泳道）数，后者决定原生 MTP 每步提出并验证的草稿长度。它们没有 CLI 形态，进程内用 `OnceLock` 只读一次——改了就要重启进程。

#### 4.3.2 核心流程与数学

- `GLM52_DECODE_SLOTS`：解析后 clamp 到 \([1, 32]\)（上限常量 `GLM52_MAX_BATCH_PER_RANK = 32`），默认 8（延迟优先的 P/D 档）。
- `GLM52_MTP_DRAFTS`：clamp 到 \([1, 5]\)（编译期上限 `GLM52_MTP_DRAFTS = 5`），默认 5。所有 wire/数组形状按 5 定死，短跨度只是尾部行不再提出——因此 P（prefill）与 D（decode）两侧甚至可以取不同值。
- 二者的组合约束在启动期硬校验：

\[ \text{slots} \times (1 + \text{drafts}) \le 96 \]

  其中 96 是 `GLM52_MAX_STEP_ROWS`（整步 decode graph 的行预算）。几组典型组合：默认 8×(1+5)=48 合法；16×(1+5)=96 恰好卡满；32×(1+2)=96 合法（宽 EP 吞吐档 = 32 slots + 2 drafts）；32×(1+5)=192 超预算，启动即被拒。

#### 4.3.3 源码精读

[model/mod.rs:L97-L110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L97-L110)
`GLM52_MAX_BATCH_PER_RANK = 32` 与 `glm52_decode_slots()`：读环境变量、clamp 到 1..=32、默认 8，结果缓存进 `OnceLock`。文档注释点名宽 EP 吞吐部署用「32 slots + GLM52_MTP_DRAFTS=2」。

[mtp.rs:L43-L61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mtp.rs#L43-L61)
`GLM52_MTP_DRAFTS = 5` 常量与 `glm52_mtp_draft_len()`：同样的 clamp 模式，并解释了 P/D 两侧可不一致的原因（wire 形状不变，只是尾部行不提出）。

[lib.rs:L429-L468](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L429-L468)
`launch()` 开头：解构 `Glm52LaunchOptions`，校验 `--glm52-ranks` 区间落在 `0..device_count` 内且非空，然后读出 slots 与 drafts 做乘积校验。其中 [lib.rs:L463-L468](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L463-L468) 的 `ensure!` 就是 96 行预算的执行点，报错文案直接给出修法「32 slots need GLM52_MTP_DRAFTS=2」。旁边注释点名 #812：超额组合曾静默截断 verify span、拖垮投机收益，所以现在启动期 fail-fast。

[model/mod.rs:L288-L288](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L288-L288)
`GLM52_MAX_STEP_ROWS = 96` 的定义处。

#### 4.3.4 代码实践：步预算手算与启动观察

1. 实践目标：把「环境变量 → 96 行预算」变成肌肉记忆。
2. 操作步骤：
   - 无 GPU：对 (slots, drafts) 取 (8,5)、(16,5)、(32,2)、(32,5)、(2,5) 五组手算 `slots × (1 + drafts)`，预测哪些会被 [lib.rs:L463-L468](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L463-L468) 拒绝。
   - 有 GPU + 权重：先设 `export GLM52_DECODE_SLOTS=16` 与 `export GLM52_MTP_DRAFTS=5` 启动一次，再改 `export GLM52_DECODE_SLOTS=32`（drafts 保持 5）启动一次，命令行同 4.1.2。
3. 需要观察的现象：合法组合的启动日志出现「GLM5.2 decode profile: slots=16 mtp_draft_span=5 …」一类行；非法组合启动即报「exceeds the …-row step budget; 32 slots need GLM52_MTP_DRAFTS=2」。
4. 预期结果：16×6=96 通过；32×6=192 被拒。日志原文「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GLM52_MTP_DRAFTS` 的上限是 5？

**答案**：5 是编译期常量（[mtp.rs:L43](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mtp.rs#L43-L43)），所有 wire 与数组形状按 5 定死；运行期只能在这个天花板下取子集，clamp 是防御性写法。

**练习 2**：`GLM52_DECODE_SLOTS=40` 会怎样？

**答案**：被 clamp 到 32（[model/mod.rs:L107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L107-L107)），不报错；但 32 再配默认 5 drafts 就撞上 96 行预算，在启动期被拒。

**练习 3**：默认 8×6=48 行离 96 还有一半富余，为什么不默认开更大？

**答案**：默认档是延迟优先的 P/D 形态（注释原话「latency-lean P/D profile」）；吞吐档交给部署用环境变量切换（32/2）。旋钮与档位分离是设计意图——此为讲义解读，源码只承诺 clamp 与预算约束。

## 5. 综合实践

为下面三种部署各写出完整启动命令，并逐项标注：它命中 `validate` 的哪条规则、`serve_plan` 的分区数是多少。

1. **A 单节点 8 卡 EP8 默认档**：不加任何 GLM5.2 专属 flag。
2. **B 单节点 8 卡 EP8 吞吐档**：在 A 的基础上把 profile 切到 32 slots + 2 drafts。
3. **C 双节点 EP8 跨节点**：节点一 host 全局 rank 0..4 并 bind rendezvous，节点二 host rank 4..8 连接。

参考答案（命令形态，均在仓库根执行）：

- A：`cargo run --release --features glm52 -- --model-path models/GLM5.2`
- B：先 `export GLM52_DECODE_SLOTS=32` 与 `export GLM52_MTP_DRAFTS=2`，再执行 A 的命令。分区数 8。
- C 节点一：`cargo run --release --features glm52 -- --model-path models/GLM5.2 --glm52-ranks 0..4 --glm52-rendezvous 0.0.0.0:9237`
- C 节点二：`cargo run --release --features glm52 -- --model-path models/GLM5.2 --glm52-ranks 4..8 --glm52-rendezvous <节点一地址>:9237`

检验方法：

- 静态：对照 [model_line.rs:L122-L210](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L122-L210) 的规则清单逐条打钩；C 的两个 ranks 区间都必须落在 `0..8` 内（[lib.rs:L429-L468](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L429-L468) 开头的区间校验）。
- 动态：在能编译的机器上先跑 4.2.4 的单测作为回归底线。三种部署的真实运行行为「待本地验证」（需要 8 卡 Blackwell 与 GLM5.2 权重）。

## 6. 本讲小结

- feature 门控在 server 组装层：`pegainfer-glm52` 无自有 `[features]`，一经编译就打开 `pegainfer-kernels` 的 `glm52` feature（[Cargo.toml:L21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/Cargo.toml#L21-L21)）；构建必须 `--release`。
- GLM5.2 有 8 个专属 flag（`Glm52Cli`），核心是 `--moe-topo`：ep4/ep8/ep16/ep32/ep64 五档 EP 加 tp4 prefill-only，拓扑派生量决定 `--tp-size`/`--dp-size` 的合法值。
- 11 个共享 flag 被 `consumed_shared_args` 接管，语义归 GLM5.2 所有——读参数先想归属。
- 运行期环境变量 `GLM52_DECODE_SLOTS`（1..=32，默认 8）与 `GLM52_MTP_DRAFTS`（1..=5，默认 5）必须满足 `slots × (1 + drafts) ≤ 96`，启动期 fail-fast（#812 的教训）。
- 所有 CLI 组合规则都有对应的纯 CPU 单测（`model_line::tests` 18 个 + `topology_tests`），改规则必改测试。

## 7. 下一步学习建议

- 下一讲 u1-l3（源码地图）：把 `src/` 下约 40 个模块按算子层、执行层、调度层归类，为后续讲义建立阅读坐标系。
- 之后 u1-l4（启动链路）深入本讲只点了名的 [lib.rs:L429](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L429-L429) `launch()` 与 [lib.rs:L894](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L894-L894) `start_engine()` 的完整启动流程。
- 顺带留意：显存相关的 `GLM52_KV_POOL_BLOCKS` 在 [lib.rs:L1408](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1408-L1408) 的报错文案中出现，它的读取与 KV 池实测填充留到 u2-l4 讲。
