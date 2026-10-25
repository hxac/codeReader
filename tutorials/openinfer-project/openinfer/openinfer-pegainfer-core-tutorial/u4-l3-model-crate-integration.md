# u4-l3 从模型 crate 看核心：pegainfer-core 的消费面

## 1. 本讲目标

前几讲我们一直"站在 pegainfer-core 里往外看"：tensor 是什么、kv_pool 怎么分配、cuda_graph 怎么捕获。本讲把镜头调转 180 度，**站在消费者 pegainfer-qwen3（默认 feature、无 Python 的那条模型线）里往回看 core**。

学完本讲，你应该能够：

1. 用一两条 grep 命令枚举模型 crate 对 `pegainfer_core::` 的全部使用，并整理成一张按模块归类的"消费清单"。
2. 对照 CLAUDE.md 的 `HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent` 主链路，把 core 的每个被消费模块钉到请求生命周期（装配 → 调度 → prefill → decode → 图回放）的具体时刻。
3. 回答扩展性问题：**新增一种模型架构时，core 哪些模块可以原样复用、哪些可能要动、哪些根本不用碰。**

本讲所有 grep 统计均在 HEAD `09a1a38f` 下实测得到，数字会随代码演进漂移，方法不会。

## 2. 前置知识

### 2.1 先复习：core 的 13 个模块各自是什么

[pegainfer-core/src/lib.rs:1-15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L1-L15) 用 13 个 `pub mod` 组成了 core 的全部公开面（头注释一句话定位："Shared runtime API used by pegainfer model crates"）。前几讲已经逐一精读，这里只放一张速查表：

| 模块 | 一句话职责 | 详见讲义 |
|------|-----------|---------|
| `tensor` | 一行再出口 kernels 的张量类型（HiddenStates/DeviceContext/DeviceVec/DeviceMatrix） | u2-l1 |
| `ffi` | 一行再出口 kernels 的 `unsafe extern "C"` CUDA 符号声明 | u2-l2 |
| `ops` | 算子封装：attention/paged_plan 自研 + kernels 大宗再出口 | u2-l3、u2-l4 |
| `kv_pool` | page-first KV 几何（KvLayout）与池/状态（KvPool/KvState/KvDesc） | u3-l2 |
| `page_pool` | 固定页分配器 + RAII 许可（KvPool 的地基） | u3-l1 |
| `cuda_graph` | CudaGraphState 图捕获/回放状态机 | u4-l1 |
| `weight_loader` | safetensors 解析 + pinned 双缓冲分块 H2D | u3-l3 |
| `rope` / `decodable_vocab` / `eos` | 装配期三件套：RoPE 表 / 词表宽度 / EOS 集合 | u3-l4 |
| `cpu_topology` / `logging` / `tracing` | 宿主侧服务：NUMA 拓扑 / 统一日志 / fastrace→OTLP | u4-l2 |

### 2.2 workspace 依赖与 feature 穿透

pegainfer 是一个 20+ crate 的 workspace（`pegainfer-kernels`、`pegainfer-frontend`、`pegainfer-kv-cache`、`pegainfer-kv-offload`、`pegainfer-sample`、`pegainfer-server`、各模型 crate 等）。qwen3 对 core 的依赖在 [pegainfer-qwen3/Cargo.toml:19-22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/Cargo.toml#L19-L22) 声明：`pegainfer-core`、`pegainfer-frontend`、`pegainfer-kernels` 全部 `{ workspace = true }`。注意两点：

- qwen3 **同时直接依赖 kernels**，所以它既有"经 core::ops 走通用算子"的路径，也有"直连 kernels"的路径（4.1.3 会看到实例）。
- feature 是穿透的：[pegainfer-qwen3/Cargo.toml:35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/Cargo.toml#L35) 定义 `kernel-call-trace = ["pegainfer-core/kernel-call-trace"]`，qwen3 开这个 feature 会点亮 core 的 traced 换出口（u2-l3 讲过的 cfg 机制）。

### 2.3 frontend 契约是什么

CLAUDE.md 的架构图给出主链路：HTTP 请求进入 vLLM 风格的 frontend，经 `EngineHandle` 交给模型 crate 的 scheduler/executor，事件以 `TokenEvent` 流回。frontend 侧的关键类型：[pegainfer-frontend/src/engine/handle.rs:52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-frontend/src/engine/handle.rs#L52) 的 `EngineHandle`、[pegainfer-frontend/src/engine/wiring.rs:145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-frontend/src/engine/wiring.rs#L145) 的 `Engine`。qwen3 在 [pegainfer-qwen3/src/lib.rs:33-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/lib.rs#L33-L35) 引入 `Engine / EngineLoadOptions / EpBackend` 来完成插接。**core 不在契约里**——它是模型 crate 实现 executor 时的共享零件库。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|------|--------------|
| `pegainfer-core/src/lib.rs` | 公开面基准：13 个 pub mod，与消费面对照 |
| `pegainfer-core/src/ops.rs`、`src/ops/attention.rs`、`src/ops/paged_plan.rs` | 算子门面与"池无关"注意力契约 |
| `pegainfer-core/src/kv_pool.rs` | KvLayout 几何与 kernel_layout() 转换 |
| `pegainfer-qwen3/Cargo.toml` | 依赖与 feature 穿透的物证 |
| `pegainfer-qwen3/src/executor.rs` | 消费重镇：装配、decode 循环、LocalQwen3Lane |
| `pegainfer-qwen3/src/weights/load.rs`、`src/weights.rs` | 装配幕的完整链条与显存预算 |
| `pegainfer-qwen3/src/prefill.rs`、`src/unified_forward.rs`、`src/batch_decode*.rs`、`src/verify_graph.rs` | 前向幕与回放幕的 core 消费点 |
| `pegainfer-qwen3/src/scheduler.rs`、`src/frontend_adapter.rs`、`src/model_line.rs` | 契约层：core 不出现的证明 |
| `pegainfer-gemma4/src/kv.rs`、`src/serve.rs`；`pegainfer-kimi-k2/src/runner/affinity.rs` | 其他模型 crate 的对照样本 |
| `CLAUDE.md` | 主链路描述与模型线清单 |

## 4. 核心概念与源码讲解

### 4.1 core 公开面清单：一张 grep 出来的消费表

#### 4.1.1 概念说明

- **公开面（public surface）**：core 通过 `pub mod` 对外承诺的全部 API，共 13 个模块。
- **消费面（consumed surface）**：模型 crate 源码里实际出现的 `pegainfer_core::` 引用集合。

公开面是设计意图，消费面是使用事实。两者的差集能直接告诉你哪些模块是"为特定模型线准备的"（如 cpu_topology）、哪些是"所有人的公共底座"（如 tensor）。衡量一个共享 crate 是否真的"稳定门面"，最硬的证据就是统计消费面——这也是 CLAUDE.md 里"模型 crate 实现契约、core 做共享运行时"这条架构叙述的可验证形式。

#### 4.1.2 核心流程

统计方法只需要 grep + sort + uniq，两层进行（命令为示例代码，本讲实测用 `git grep -h -o -E ...` 等价实现）：

```text
第一层（模块级）：
  grep -rhoE 'pegainfer_core::[a-z_]+' <crate>/src | sort | uniq -c | sort -rn
  → 得到"模块 → 引用次数"表

第二层（符号级）：
  grep -rhoE 'pegainfer_core::[a-z_]+::[A-Za-z_]+' <crate>/src | sort | uniq -c | sort -rn
  → 得到"具体类型/函数 → 引用次数"表
```

#### 4.1.3 源码精读

**入口证据：qwen3 的 executor 一开始就声明了它要 core 的什么。**

[pegainfer-qwen3/src/executor.rs:12-18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L12-L18) 的 use 块一次引入 7 项：`cuda_graph::CudaGraphDumpSummary`、`kv_pool::KvLayout`、`ops`、`tensor::DeviceContext`、`tensor::HiddenStates`、`weight_loader::WeightPrefetch`、`weight_loader::load_shard_info`——本讲后面四幕（装配/调度/前向/回放）几乎都能在这 7 行里对上号。

**模块级消费表（qwen3 实测，共 154 处引用）：**

| core 模块 | qwen3 引用数 | 备注 |
|-----------|------------|------|
| tensor | 61 | ≈ 40%，最大头 |
| ops | 37 | 含 12 处 `ops::call_spec` |
| weight_loader | 24 | 装配期集中 |
| rope | 10 | 全在装配期 |
| kv_pool | 10 | **全部是 `KvLayout`，无一处 KvPool/KvState** |
| cuda_graph | 6 | 全是 `CudaGraphState` |
| ffi | 3 | cublas_init/cublas_destroy/cuda_set_device |
| decodable_vocab | 2 | 装配期一次性 |
| eos | 1 | 装配期一次性 |
| page_pool / logging / tracing / cpu_topology | 0 | qwen3 不直接消费 |

**符号级 Top（qwen3 实测）**：`tensor::HiddenStates` 27 次、`tensor::DeviceContext` 17 次、`ops::call_spec` 12 次、`tensor::DeviceMatrix` 10 次、`kv_pool::KvLayout` 10 次、`tensor::DeviceVec` 6 次、`weight_loader::load_shard_info` 4 次、`cuda_graph::CudaGraphState` 4 次。算子调用侧（`ops::xxx` 形式）最多的符号是 `gemm_rows_into` / `gemm_rows_into_checked` 各 12 次、`rms_norm_batch_into` 11 次、`numeric_policy` 11 次、`embedding_batch` 11 次、`gemm_lt_tune` 10 次——一张 GEMM + 归一化 + 查表的 dense 前向画像。

**发现一：core 的第一身份是"张量类型提供者"。** 61/154 ≈ 40% 的引用是 tensor 门面。HiddenStates 是贯穿 qwen3 全部前向代码的激活值容器（batch_decode.rs 的函数签名里直接写 `&pegainfer_core::tensor::HiddenStates`，如 [pegainfer-qwen3/src/batch_decode.rs:491-493](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode.rs#L491-L493)），DeviceContext 是所有 GPU 操作的上下文句柄。

**发现二：qwen3 对 core 的 kv_pool 只取几何，真正的池在 pegainfer-kv-cache。** 这是本讲最重要的结构性事实，也回答了 u1-l1 遗留的"core 自建 kv_pool 与 pegainfer-kv-cache crate 分工"待确认项：

- qwen3 的 10 处 `kv_pool::` 引用全部是 `KvLayout`（[batch_decode.rs:6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode.rs#L6)、[prefill.rs:6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/prefill.rs#L6)、[executor.rs:13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L13)、[unified_forward.rs:11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L11) 等）——它是前向函数签名里"KV 长什么样"的描述。
- qwen3 的 KV 显存与页管理来自 **pegainfer-kv-cache**：[executor.rs:27-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L27-L32) 引入 `KvBlockGuard / KvBuffer / KvCacheManager / KvView / LoadReservation / PrefixProbe`，[executor.rs:892](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L892) 用 `HashMap<RequestId, RequestKv>` 管每请求状态（带前缀缓存/恢复语义，超出 core 范畴）。
- 两个 crate 的几何在 [pegainfer-qwen3/src/weights.rs:839-851](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights.rs#L839-L851) 处对拍：qwen3 先用 `crate::sizing::product` 手乘每块字节数，再构造 `pegainfer_kv_cache::KvLayout` 并 `debug_assert_eq!` 其 `page_stride` 与手乘一致——core/kv-cache/kernels 三方对"page-first 几何"达成同一份默契。

**发现三：KV 设施存在"两档采用深度"。** gemma4 与 qwen3 相反，直接消费 core 的整池：[pegainfer-gemma4/src/kv.rs:8](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs#L8) `use pegainfer_core::kv_pool::KvPool;`，[pegainfer-gemma4/src/serve.rs:1109](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/serve.rs#L1109) 用 `KvPool::with_storage(...)` 建池——滑动窗族 + 全注意力族的**双池**结构（[serve.rs:1006-1007](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/serve.rs#L1006-L1007) 的 `local_pool / global_pool` 两个字段）。也就是说：core 的 kv_pool 既是"几何库"（qwen3 用法）也是"整池方案"（gemma4 用法）。

**发现四：也有绕开 core::ops 直连 kernels 的路径。** [pegainfer-qwen3/src/batch_decode_dag.rs:297](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_dag.rs#L297) 与 [batch_decode_dag.rs:318](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_dag.rs#L318) 直接调用 `pegainfer_kernels::ops::paged_attention_batch_decode_into` / `..._split_kv_into`（DAG 编排路径要拿裸 kernel 函数组装调用图）；而常规前向走 core 封装：[prefill.rs:296](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/prefill.rs#L296)、[unified_forward.rs:562](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L562)、[unified_forward.rs:599](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L599) 的 `ops::prefill_attention_paged_into` 等。两条路最终都汇到 kernels 的同一个入口，core 只是把默认路径包装得更顺手。

**发现五：cpu_topology 是"专属零件"。** 四个抽样 crate 里只有 kimi-k2 消费它：[pegainfer-kimi-k2/src/runner/affinity.rs:6-13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/affinity.rs#L6-L13) 一次引入 8 个符号（`pin_current_thread_to_cpu`、`split_rank_cpu_slices`、`cuda_device_numa_node` 等）做 8 卡 EP 的 NUMA 绑核——u4-l2 留下的"cpu_topology 消费点待确认"在这里落定。

**跨 crate 模块计数对照（同一统计方法实测）：**

| core 模块 | qwen3 | gemma4 | deepseek-v2-lite | kimi-k2 |
|-----------|-------|--------|------------------|---------|
| tensor | 61 | 26 | 35 | 0 |
| ops | 37 | 7 | 5 | 2 |
| weight_loader | 24 | 12 | 5 | 0 |
| kv_pool | 10 | 12 | 0 | 0 |
| rope | 10 | 2 | 0 | 0 |
| cuda_graph | 6 | 2 | 0 | 1 |
| ffi | 3 | 6 | 2 | 0 |
| decodable_vocab | 2 | 0 | 0 | 0 |
| eos | 1 | 1 | 1 | 2 |
| cpu_topology | 0 | 0 | 0 | 8 |
| logging | 0 | 0 | 0 | 1 |

读法：dense 线（qwen3/gemma4/dsv2-lite）重 tensor + ops + weight_loader；重 EP 的线（kimi-k2）几乎自带全套张量/算子栈，只从 core 拿拓扑、EOS、日志这类"宿主小件"。**core 的消费面因模型线而异，这正是"共享运行时"该有的形状。**

#### 4.1.4 代码实践

**实践目标**：亲手复现上表的 qwen3 列，验证"消费面统计法"。

**操作步骤**（纯 CPU，无需 GPU，在仓库根目录执行）：

1. 模块级统计：`git grep -h -o -E 'pegainfer_core::[a-z_]+' -- pegainfer-qwen3/src | sort | uniq -c | sort -rn`
2. 符号级统计：`git grep -h -o -E 'pegainfer_core::[a-z_]+::[A-Za-z_]+' -- pegainfer-qwen3/src | sort | uniq -c | sort -rn | head -20`
3. 定位最重消费者：`git grep -c 'pegainfer_core' -- pegainfer-qwen3/src | sort -rn | head -10`

**需要观察的现象**：模块级输出应与 4.1.3 的表一致（tensor 61 → 排第一）；文件级前几名是 `batch_decode_dag.rs`（16 处）、`weights/load.rs` 与 `weights.rs`（各 14 处）。

**预期结果**：tensor + ops + weight_loader 三项合计约 122/154 ≈ 79%，可得出"core 的消费面以张量、算子、装载荷载为中心"的结论。若统计时 HEAD 已前进，允许数字小幅漂移，但排序结构应稳定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 qwen3 的 `kv_pool` 引用数恰好等于 `KvLayout` 引用数？这说明什么？
**答案**：qwen3 只把 core 的 kv_pool 当"几何库"用（KvLayout 描述 page-first 布局，喂给前向签名与 kernel 转换），页分配、显存、每请求状态全部交给 pegainfer-kv-cache；说明 core 的 KV 设施可以按需拆开采用，池与几何并非捆绑销售。

**练习 2**：`page_pool` 在 qwen3 里引用数为 0，它是不是死代码？
**答案**：不是。它是 core 内部 `kv_pool::KvPool` 的地基（u3-l1），被 gemma4 这类直接用 core 池的模型线间接消费；"某模型 crate 引用为 0"只说明该模型没走这条采用路径。

**练习 3**：batch_decode_dag.rs 为什么直连 `pegainfer_kernels::ops` 而不用 `core::ops` 的同名封装？
**答案**：DAG 路径要在调用图里编排裸 kernel 函数（并配合 core 的 `ops::call_spec::PagedDecodeCallSpec` 记录调用规格，见 [batch_decode_dag.rs:12-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_dag.rs#L12-L32) 的 use 块），core 封装面向的是"直接调用"的普通前向；qwen3 的 Cargo.toml 同时依赖 kernels，为此留了合法通道。

### 4.2 请求生命周期落点：装配、调度、前向、回放

#### 4.2.1 概念说明

CLAUDE.md 的主链路是 `HTTP → frontend(EngineHandle) → per-model scheduler/executor → TokenEvent`。core 的全部消费点都藏在"per-model executor"这个黑盒里。qwen3 把黑盒拆成三层，分工在源码注释里写得很清楚：

- **scheduler（纯宿主逻辑）**：[pegainfer-qwen3/src/scheduler.rs:1-9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/scheduler.rs#L1-L9) 的模块文档明说"engine-contract-free：只拥有 PendingRequest 队列、产出纯 StepEffects 数据；一切触碰契约的东西都在 frontend_adapter"。这一层 **0 处 core 引用**（scheduler.rs 不在含 core 引用的文件列表里）。
- **frontend_adapter / model_line（契约层）**：[frontend_adapter.rs:34-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/frontend_adapter.rs#L34-L35) 引入 frontend 的 `Engine/EngineInfo` 并在 [frontend_adapter.rs:208-210](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/frontend_adapter.rs#L208-L210) 组装出 `Engine { ..., info: EngineInfo { ... } }`；[model_line.rs:124](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/model_line.rs#L124) `impl ModelLine for Qwen3Line` 完成配置探测与启动接线。
- **executor（GPU 执行层）**：core 的 154 处引用几乎全部落在这里。

由此可以给请求生命周期分四幕：**装配幕**（进程启动，一次性）、**调度幕**（每步宿主决策）、**前向幕**（每步 GPU 计算）、**回放幕**（decode 稳态走 CUDA Graph）。core 的模块按幕分布——这张"落点表"就是本讲的第二个最小模块。

#### 4.2.2 核心流程

一次 `cargo run --release -- --model-path models/Qwen3-4B` 的生命周期（示例伪代码，标注 core 参与者）：

```text
[装配幕 · 一次性]
DeviceContext::new_with_device(ordinal)          ← core::tensor
Config::from_file + decodable_vocab_width        ← core::decodable_vocab（load.rs L77）
aligned_logits_rows + SuppressIds::upload        ← core::decodable_vocab / core::ops（L85）
eos_token_ids 填进 config.stop_token_ids        ← core::eos（weights.rs L240）
cublas_init / cuda_set_device                    ← core::ffi（executor.rs L521-L543）
load_shard_info → mmap → StagedWeightLoader     ← core::weight_loader（L135）
precompute_rope(RopeTableSpec{...})              ← core::rope（L263）
cuMemGetInfo 余额 → KV 预算 → KvLayout::new      ← 模型 crate 自管（weights.rs L929 / executor.rs L3288）
CudaGraphState::new() × 桶数 × 路径数            ← core::cuda_graph（L299-L303）

[稳态循环 · 每个调度步]
scheduler 产出 StepEffects（0 个 core 引用）
executor:
  prefill 步: ops::prefill_attention_paged_into  ← core::ops（prefill.rs L296）
  decode 步:  图回放 or ops::paged_attention_*   ← core::cuda_graph / core::ops
  KV 写入:    kv_buffer(kv-cache) + layout(core::KvLayout) + plan(core::ops)
TokenEvent 流回 frontend
```

其中装配幕的 KV 预算公式（[weights.rs:839-844](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights.rs#L839-L844) 的手乘）：

\[
\text{bytes per block} = L \times 2 \times H_{kv} \times D_{head} \times P \times \mathrm{sizeof}(\mathrm{bf16})
\]

（\(L\) 层数、\(H_{kv}\) KV 头数、\(D_{head}\) 头维、\(P\) 页大小；因子 2 是 K 与 V。）core 不参与预算协商——u3-l3 已确认"模型 crate 先装权重，再按 cuMemGetInfo 剩余读数推导 KV 预算"，qwen3 的 [weights.rs:929](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights.rs#L929)（生产路径 `profiled_kv_budget`）与 [weights.rs:679](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights.rs#L679)（kernel-call-trace 专用分支）就是这套逻辑的两个实例。

#### 4.2.3 源码精读

**装配幕：一条 70 行的链条把 core 的五个模块串起来。** [pegainfer-qwen3/src/weights/load.rs:77-85](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L77-L85)：先 `decodable_vocab_width(model_path)` 求"最宽可解码 id"（校验它不超过 checkpoint 的 vocab_size），再 `aligned_logits_rows` 把输出投影行数对齐——这直接决定后面的输出 GEMM 形状，并把越界 id 收进 `ops::SuppressIds` 做尾段抑制。随后 [weights/load.rs:135](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L135) `StagedWeightLoader::new(&ctx, &shards, &weight_map)`（对应 core [weight_loader.rs:488](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L488) 的三段状态机，u3-l3 精读过），[weights/load.rs:263-265](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L263-L265) `precompute_rope(..., &RopeTableSpec{...})` 生成 RoPE 表（core [rope.rs:46](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L46)）。EOS 在 [weights.rs:240](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights.rs#L240) 一行完成：`config.stop_token_ids = pegainfer_core::eos::eos_token_ids(model_path, config.vocab_size)?`。ffi 三兄弟在 [executor.rs:521-543](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L521-L543)：worker 线程先 `cublas_destroy()` 清掉可能的残留句柄，`cuda_set_device` 绑定设备，再 `cublas_init()` 重建。

**调度幕：core 缺席本身就是架构信息。** scheduler 只操作宿主队列与纯数据（StepEffects），把"何时 prefill、何时 decode、批多大"的决定与"怎么算"彻底解耦。这解释了 4.1.3 表里 scheduler.rs 引用数为 0 的原因。

**前向幕：core 的注意力契约是"池无关"的。** 看 core 侧签名（[pegainfer-core/src/ops/attention.rs:12-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L12-L31)）：`kv_buffer: &CudaSlice<bf16>`、`layout: &KvLayout`、`plan: &PrefillPagedPlan`——**它不关心 buffer 是谁分配的**。qwen3 因此能把 kv-cache crate 的 `KvBuffer` 和 core 的 `KvLayout` 拼在一起用；DAG 路径直连 kernels 时则显式调 [kv_pool.rs:102](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L102) 的 `kernel_layout()` 把 core 几何转成 kernels 的 `PagedKvLayout`（[batch_decode_dag.rs:297-306](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_dag.rs#L297-L306) 调用现场）。消费侧的全景在 [executor.rs:3208-3219](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L3208-L3219) 的 `LocalQwen3Lane` 结构体里一眼可见：

| 字段 | 来源 | 语义 |
|------|------|------|
| `bufs: BatchDecodeBuffers` | qwen3 自有 | 预分配 decode 缓冲（内含若干 `CudaGraphState`） |
| `model: Qwen3Model` | qwen3 自有 | 权重与投影（经 core weight_loader 装配） |
| `kv_buffer: KvBuffer` | pegainfer-kv-cache | KV 显存 |
| `layout: KvLayout` | **core** | kernel 面几何（[L3215](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L3215)） |
| `unified_plan: Option<ops::PrefillPagedPlan>` | **core** | 原地重填的注意力计划（[L3219](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L3219)） |
| `padding_block_id: i32` | kv-cache | padding 行共享死页（[L3240](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L3240)，作用同 core 池的 `padding_page_id()`，见 [kv_pool.rs:270](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L270)） |

dense 前向的其余算子都是 core::ops 再出口的一次调用：[unified_forward.rs:686-687](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L686-L687) 的 `silu_mul_batch_into` + `gemm_into`（MLP 融合路径）、[unified_forward.rs:704](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L704) 的 `add_batch_into`（残差）。

**回放幕：qwen3 决定分桶，core 提供状态机。** [batch_decode_buffers.rs:204-212](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L204-L212)：每个 `(bucket, attention_path)` 组合一张图——`graphs: Vec<CudaGraphState>`（NonPartition 路径）与 `graphs_split: Vec<CudaGraphState>`（split-KV 路径），构造期在 [L299-L303](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L299-L303) 用 `CudaGraphState::new()` 铺满空态（u4-l1 的"空态→捕获→实例化→首启→纯回放"状态机）。投机解码的 verify 图同理：[verify_graph.rs:62-63](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/verify_graph.rs#L62-L63)。core 只提供"一张图怎么被捕获与回放"，桶怎么划、闭包录什么，是模型 crate 的自由度。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的生命周期伪代码在真实运行里对上号。

**操作步骤**：

1. （需 GPU + 权重）从仓库根目录运行 CLAUDE.md 给出的命令：`cargo run --release -- --model-path models/Qwen3-4B`。
2. 观察启动日志中的装配幕痕迹：模型加载路径、decodable vocab / logits 行数（对应 [weights/load.rs:87-91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L87-L91) 的 info! 输出）、KV 预算推导。
3. （无 GPU 替代）做"源码跟踪型实践"：从 [prefill.rs:296](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/prefill.rs#L296) 的调用点出发，向上找到谁传入了 `layout` 与 `plan`，向下读 core [ops/attention.rs:12-52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L12-L52)，画出"qwen3 调用方 → core 包装 → kernels 入口"三段式调用链。

**需要观察的现象**：装配期各日志条目与 4.2.2 伪代码各行的对应关系；decode 稳态期没有新的权重类日志（图已捕获，进入回放）。

**预期结果**：能够把每条启动日志归属到 core 的某个模块（decodable_vocab/weight_loader/rope/eos）或模型 crate 自有逻辑（KV 预算）。步骤 1-2 的具体日志文案**待本地验证**（本讲义写作环境无 GPU，未实际运行）。

#### 4.2.5 小练习与答案

**练习 1**：scheduler.rs 为什么能做到 0 个 core 引用？删掉 scheduler 会不会影响 core 的任何模块？
**答案**：因为调度只产生纯数据（StepEffects），不碰 GPU；core 的模块全部围绕"执行"（张量、算子、显存状态）。删掉/替换 scheduler 不影响 core 任何一行——这正是"core 服务 executor、不服务调度"的边界证据。

**练习 2**：core 的 `prefill_attention_paged_into` 收 `&CudaSlice<bf16>` 而不是 `&KvPool`，这个签名设计换来了什么？
**答案**：池无关性。qwen3（kv-cache 池）与 gemma4（core KvPool 双池）都能调用同一份封装；core 只通过 `&KvLayout` + `&PrefillPagedPlan`（i32 索引）拿到它需要的全部信息，实现 u2-l4 讲过的"数据不动、索引动"契约。

**练习 3**：`LocalQwen3Lane` 的字段注释特意强调 graphs 必须在 `model.tp_comm`（NCCL 通信子）之前 drop，这与 core 的什么纪律同源？
**答案**：与 u4-l1 的图生命周期纪律同源：NCCL teardown 会轮询等待所有录入了集合通信的图销毁完毕，所以图状态（core CudaGraphState 包装）必须先于通信子释放——RAII 顺序即正确性。

### 4.3 扩展面评估：新增一种模型架构，core 要动什么

#### 4.3.1 概念说明

CLAUDE.md 的模型表列出 7 条模型线，全部共享同一个 core。评估"新增模型"的扩展面，判断标准只有一条：**模型间的差异应尽量落在模型 crate 与 kernels，core 只接收"被多条线共同需要的新能力"。** core 的 [ops.rs:1-15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L1-L15) 就是扩展点的地图：`mod attention; pub mod call_spec; mod paged_plan;`（自研封装）+ 从第 17 行起对 kernels 的大宗再出口——新增算子的标准路径是"kernels 实现 + core 一行 `pub use`"。

#### 4.3.2 核心流程

新增一个纯 dense 模型（假设叫 NewDense-7B，标准 full attention + 标准 FFN）的决策树（示例）：

```text
新算子（如某种新归一化变体）？
  ├─ kernels 已有 → core::ops 直接用，模型 crate 零成本
  ├─ kernels 新增 → kernels 加实现；若多线共享，core ops.rs 加一行再出口
  └─ 仅本模型用   → 模型 crate 直连 kernels（qwen3 DAG 已示范此通道）
新 KV 布局？
  ├─ page-first 标准几何 → core KvLayout 原样用（或像 qwen3 一样只借几何、池自选）
  └─ 非标准（latent MLA 等）→ 模型 crate 自带（kimi-k2/k3 路线，core kv_pool 引用为 0）
新调度/投机/EP 协调？
  └─ 全部模型 crate 自建（qwen3 的 dspark/dflash、k3 的 CP 车道都不在 core）
装配三件套（rope 表/词表宽度/EOS）与权重加载？
  └─ core 原样复用——除非 checkpoint 格式变了才动 weight_loader
```

#### 4.3.3 源码精读

**证据一：模型差异确实不落在 core。** qwen3 在此前区间新增的 DFlash2 检查点与 selector（提交 8b5b84a9）全部落在 `pegainfer-qwen3/src/dflash*`，core 无一行改动；它的两个专属算子 `dflash2_select_into`、`dflash2_grouped_conv_into` 以 `ops::` 前缀出现在 qwen3 源码中（可在 4.1.4 的符号级统计里检索到），即经由 core::ops 再出口获得——"kernels 实现 + core 一行再出口"的扩展路径真实在用。

**证据二：KV 设施的两档采用深度就是为扩展准备的。** 新 dense 模型按 qwen3 模式（kv-cache 池 + core KvLayout 几何）或 gemma4 模式（core KvPool 整池）任选其一；两者都已有多模型线背书（[gemma4/src/kv.rs:8](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs#L8) vs [qwen3/src/executor.rs:13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L13)）。

**证据三：core 为"非 dense 世界"预留的模块是可选消费。** cpu_topology/logging 只被 kimi-k2 消费、tracing 走 OTLP 端点开关（u4-l2）——dense 新模型可以一行都不碰。

**结论表：**

| core 模块 | 新 dense 模型 | 说明 |
|-----------|--------------|------|
| tensor / ffi / ops / weight_loader / rope / decodable_vocab / eos / cuda_graph | **原样复用** | 这 8 项就是"共享运行时"的实体 |
| kv_pool | **二选一采用** | 只借 KvLayout（qwen3 式）或整池（gemma4 式） |
| page_pool | 间接复用 | 仅整池路线经 KvPool 触达 |
| cpu_topology / logging / tracing | 按需 | 多卡 EP / 深度诊断时才需要 |

#### 4.3.4 代码实践

**实践目标**：为假想的 NewDense-7B 写一份消费清单草案。

**操作步骤**：

1. 以 4.1.3 的 qwen3 模块表为模板，删掉 `dspark/dflash/eagle3` 相关项（NewDense 无投机解码）。
2. 对每一行标注：复用 core（引用哪个符号）还是自建（落在模型 crate 哪个文件）。
3. 找出草案中"必须进 core 的新增项"——理想情况下应该是空集；若不为空，写出理由（例如：某新算子被第二条模型线也需要）。

**需要观察的现象 / 预期结果**：清单应收敛到 tensor（HiddenStates/DeviceContext/DeviceMatrix）+ ops（gemm/rms_norm/silu/attention 四类）+ weight_loader（StagedWeightLoader/load_shard_info 系列）+ rope/decodable_vocab/eos 各 1-2 处 + cuda_graph（CudaGraphState）+ kv_pool（KvLayout）——与 qwen3 的实测形状几乎一致。此实践为纸面推导，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：如果 NewDense 需要"QK-Norm + RoPE 融合"这种 qwen3 已有的算子，core 要改吗？
**答案**：不用。该能力已在 core::ops 再出口清单中（qwen3 符号统计里的 `qk_norm_rope_batch_decode_into` 即证据），新模型直接 `use pegainfer_core::ops::...`。

**练习 2**：什么时候必须动 core？
**答案**：当且仅当新能力是跨模型线共享的运行时能力且需要新公开面时——例如新增一种所有线都要的算子封装（kernels 实现 + ops.rs 一行再出口），或张量/加载层的通用行为变化。只服务单模型的逻辑永远不该进 core。

**练习 3**：kimi-k2 的消费面里 tensor 引用为 0，却引用 core 的 cpu_topology 和 logging。这给出什么设计提示？
**答案**：core 同时承担两种角色——"GPU 底座"（dense 线重消费）与"宿主工具箱"（EP 线轻消费）；一个 crate 的公开面允许不同消费者取完全不相交的子集，这正是模块化 crate 的健康形态。

## 5. 综合实践

把本讲三个最小模块串成一个完整任务（即本讲规格里的代码实践任务）：

1. **检索**：在工作区检索 `pegainfer_core::` 的全部 use/引用语句——重点 `pegainfer-qwen3/src`，抽样 `pegainfer-gemma4/src`、`pegainfer-deepseek-v2-lite/src`、`pegainfer-kimi-k2/src`（命令见 4.1.4 步骤 1）。
2. **制表**：整理"core 消费清单"表格，列为 `core 模块 → 被谁用（文件:行） → 用在哪一幕（装配/prefill/decode/回放）`。装配幕的证据锚点：weights/load.rs:77/85/135/263、weights.rs:240、executor.rs:521-543/3288；前向幕：prefill.rs:296、unified_forward.rs:562/599/686-704；回放幕：batch_decode_buffers.rs:204-212/299-303、verify_graph.rs:62-63。
3. **判断**：基于清单写约 200 字回答"新增一个纯 dense 模型，core 哪些模块可以原样复用"。

**预期结果**（参考答案要点）：tensor/ffi/ops/weight_loader/rope/decodable_vocab/eos/cuda_graph 八个模块原样复用；kv_pool 按采用深度二选一（借 KvLayout 或用整池）；page_pool 仅整池路线间接复用；cpu_topology/logging/tracing 可完全不碰。模型差异（新注意力变体、投机解码、调度策略）应全部留在模型 crate 与 kernels——参照 qwen3 新增 DFlash2 未改 core 的先例。

本综合实践纯纸面 + grep，无需 GPU；表格行数预期约 12 行（core 模块数）。

## 6. 本讲小结

- **消费面统计法**：`git grep -o -E 'pegainfer_core::[a-z_]+' | sort | uniq -c` 一条命令就能把"core 为谁服务"从架构口号变成数字事实；qwen3 实测 154 处引用、9/13 个模块被消费，tensor + ops + weight_loader 占近八成。
- **KV 双轨**：qwen3 只借 core 的 `KvLayout` 几何、真池在 pegainfer-kv-cache；gemma4 直接消费 core `KvPool` 双池——core 的 KV 设施支持"几何/整池"两档采用深度（u1-l1 的待确认项就此落定）。
- **生命周期落点**：core 的模块全部落在 executor 层；scheduler 是 engine-contract-free 的纯宿主逻辑（0 引用），frontend_adapter/model_line 负责契约插接——四幕（装配/调度/前向/回放）落点表是读懂任何模型 crate 的通用框架。
- **池无关契约**：core 注意力封装收 `&CudaSlice<bf16> + &KvLayout + &PrefillPagedPlan`，不绑池实现；`KvLayout::kernel_layout()` 是 core 几何到 kernels 入口的官方转换器。
- **扩展面结论**：新增 dense 模型 core 八个模块原样复用、kv_pool 二选一、宿主服务按需；模型差异走"kernels 实现 +（必要时）core 一行再出口"，qwen3 直连 kernels 的 DAG 路径证明连这行再出口都可省。

## 7. 下一步学习建议

- **下一讲 u5-l1（并发正确性：两段式准入、RAII 纪律与 EP 自由跑动）**：本讲看到的所有消费点都默认"页不泄漏、padding 确定、图稳定"；下一讲把这些现象上升为不变量，并对接 CLAUDE.md 的 EP free-running 纪律。
- **推荐继续阅读的源码**：`pegainfer-qwen3/src/batch_decode_dag.rs`（core call_spec 与 kernels 直连的编排现场）、`pegainfer-qwen3/src/weights.rs` 的预算函数（模型 crate 如何独自协商显存）、`pegainfer-gemma4/src/serve.rs`（core KvPool 整池消费的完整样本）。
- **对照阅读**：`pegainfer-kv-cache/src/lib.rs`（qwen3 真池的公开面：KvCacheManager/BlockPool/KvView），理解它与 core kv_pool 的互补关系。
