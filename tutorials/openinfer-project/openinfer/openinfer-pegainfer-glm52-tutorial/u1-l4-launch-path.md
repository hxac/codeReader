# 启动链路：从 ModelLine 到 EngineHandle

## 1. 本讲目标

上一讲（u1-l3）我们建立了源码地图，知道了「请求流：frontend → serve_loop → Step → run_step_body → TokenEvent」。本讲回答的问题是：**这一切在启动时是如何被搭建起来的？**

读完本讲你应该能够：

1. 复述 `start_engine` 的六个阶段，并解释它们为什么必须按这个顺序执行。
2. 理解 `Glm52MoeTopo` 如何同时决定 TP/DP/EP 组合、设备数量、每 rank 专家数，以及在两个不同层次（server 校验层与 launch 层）被校验的方式。
3. 理解「预捕获即会合」的 bootstrap barrier，以及启动失败时三个不同时间窗口各自对应的并发拆除方式。

承接已建立的概念：u1-l2 讲过 `Glm52Cli` 的 flag 与 `Glm52MoeTopo` 拓扑枚举；u1-l3 讲过 `Glm52RankCommand` 指令协议、free-running 与「预捕获即会合」。本讲不再重复它们的定义，而是看它们在启动序列中**何时、以什么顺序**被接线。

## 2. 前置知识

- **ModelLine trait**：PegaInfer 支持多条模型产品线（Qwen3、GLM5.2 等）。每条线实现一个 `ModelLine` trait，向 server 二进制提供「认领模型目录（probe）、注入专属 CLI（augment_cli）、声明消费的共享参数（consumed_shared_args）、跨 flag 校验、告知前端调度形态、启动引擎」六个钩子。server 二进制本身只做纯分发，不含任何模型逻辑。
- **TP / DP / EP**：三种并行方式。TP（tensor parallel，张量并行）把一个算子的权重矩阵切开分到多卡；DP（data parallel，数据并行）让多个副本各自独立处理不同请求；EP（expert parallel，专家并行）把 MoE 的不同专家放到不同卡上，token 按路由结果跨卡搬运。GLM5.2 的 EP 拓扑同时也是 DP：每个 rank 是一个独立调度的引擎（DP），专家按 rank 分片（EP），TP=1。
- **fail-fast**：宁可启动时报错退出，也不带病运行到服务中途才崩。本讲的启动链路里几乎每一步都是一道 fail-fast 门。
- **collective（集合通信）与 barrier**：多卡上的通信原语（如 NCCL、DeepEP 的 dispatch/combine）要求**所有参与方都到场**才能完成。「barrier」指所有参与者在此汇合对齐。任何一卡缺席，其他卡会阻塞直至超时——这是理解本讲「失败拆除」的钥匙。
- **EngineHandle**：frontend 拿到的引擎句柄，里面是「每逻辑分区一条提交通道 + 一个引擎线程 JoinHandle」加若干元数据（可服务上下文上限、KV 容量、负载指标 watch）。HTTP 层之后只跟这个句柄打交道。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/model_line.rs` | `Glm52Line`：`ModelLine` 的 GLM5.2 实现——probe、专属 CLI、跨 flag 校验、serve_plan、launch 装配 `Glm52LaunchOptions` |
| `src/lib.rs` | `Glm52LaunchOptions` / `Glm52MoeTopo` 定义；`launch()` 前置校验；`start_engine` 六阶段主体；`validate_startup` / `load_rank_weights_to_gpu` / `build_rank_models` / `build_kv_store` |
| `src/runner.rs` | `Glm52RankCommand` 指令枚举；`Glm52RankWorker::spawn`（worker 线程与启动握手）；`request_shutdown` 与 `Drop` 拆除 |
| `src/scheduler/mod.rs` | `Glm52Engine::spawn`（引擎线程）与 `run`（bootstrap → 报告 → serve_loop → teardown） |
| `../pegainfer-frontend/src/model_line.rs` | `ModelLine` trait 契约（本 crate 实现的目标接口） |
| `../pegainfer-frontend/src/engine/handle.rs` | `EngineHandle` 的组装 API（本链路的终点） |

## 4. 核心概念与源码讲解

### 4.1 launch 选项校验：两层校验夹住同一个拓扑

#### 4.1.1 概念说明

启动的第一件事不是碰 GPU，而是把「用户在命令行说的话」翻译成一组**自洽的**启动选项。这份校验分两层：

- **第一层（server 校验层）**：`Glm52Line::validate`，在 server 二进制组装期运行，纯 CPU，负责 flag 之间的组合规则（如 `--glm52-prefill-only` 必须配 `--moe-topo=tp4`）。它让用户在第一条命令上就得到人类可读的错误，而不是等到 CUDA 初始化一半才炸。
- **第二层（crate launch 层）**：`lib.rs` 里的自由函数 `launch()`，在模型 crate 内部再校验一遍。这一层是**权威**的：即使有人绕过 CLI 直接调 `crate::launch`（例如测试或未来的其他前端），规则依然生效。

两层校验的是同一组不变量——这是「服务器校验镜像 launch 期 ensure」的写法（`Glm52MoeTopo::expected_tp_size` 的文档注释原话）。

关键数据结构是 `Glm52LaunchOptions`：它把 CLI 上十几个 flag 折叠成一个结构体，是 CLI 世界与引擎世界之间唯一的传话人。

#### 4.1.2 核心流程

```text
clap 解析（augment_cli 注入的 Glm52Cli + 共享 flag）
        │
        ▼
Glm52Line::validate            ← 第一层：组合规则（dp/tp 与拓扑一致、
        │                        prefill-only 的互斥项、MTP 拓扑约束…）
        ▼
Glm52Line::launch              ← 装配 Glm52LaunchOptions
        │                        （drafter 三态判定、kv_offload 选项搬运）
        ▼
crate::launch(model_path, opts) ← 第二层：权威 ensure 链
        │   ① ranks 范围合法（0..device_count 内且非空）
        │   ② slots×(1+drafts) ≤ 96 行步预算（#812 教训）
        │   ③ ensure_blackwell_devices：本进程所持 GPU 全部 SM≥10.0
        │   ④ 多进程形态必须给 --glm52-rendezvous；TP4 必须单进程
        │   ⑤ dump_graph_png 只允许 rank-0 进程
        │   ⑥ 按拓扑钉死 tp_size/dp_size
        │   ⑦ prefill-only / kv_offload 的其余互斥项
        ▼
start_engine(...)              ← 进入 4.2
```

为什么这个顺序：全部校验都**在触碰任何 CUDA 上下文或读权重之前**完成（除了 ③ 必须开设备探架构，但那也只是只读探测）。配置错误是最廉价的错误，应该最先暴露。

#### 4.1.3 源码精读

先看第一层的入口。`validate` 从合并后的命令行解析出 `Glm52Cli`，再解析拓扑字符串：

- [src/model_line.rs:L122-L145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L122-L145)：`validate` 先解析 `--moe-topo`，然后校验 `--dp-size`（显式给出时必须等于拓扑默认值）与 `--tp-size`（必须精确等于拓扑期望值）。注意 dp 是「给了才查」，tp 是「无条件查」——因为 tp_size 有一个非 GLM5.2 的默认值 1，而 EP 拓扑恰好要求 1，TP4 则要求 4。
- [src/model_line.rs:L188-L207](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L188-L207)：native MTP 的拓扑门——只允许 EP 档（ep4/ep8/ep16/ep32/ep64）或「TP4 + prefill-only」组合。

拓扑的所有派生量都集中在 `Glm52MoeTopo` 的固有方法里，两层校验共享它们：

- [src/lib.rs:L218-L287](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L218-L287)：`default_dp_size`（TP4→1，EP 档→设备数）、`device_count`（EP8→8、EP4/TP4→4、EP16/32/64→16/32/64）、`logical_rank_count`（TP 镜像拓扑恒为 1，EP 档=设备数）、`expected_tp_size`（TP4→4，其余→1）、`expected_ep_size`、`ep_local_experts`（256 专家 ÷ EP 宽度）。**一个枚举值同时决定 TP/DP/EP 三个维度**——用户只说一个词 `ep8`，整套并行形状就被钉死，不存在「EP8 但 TP2」这类无意义组合。

然后是 `Glm52Line::launch` 如何把 CLI 折叠成选项结构体：

- [src/model_line.rs:L229-L282](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L229-L282)：drafter 三态判定（`--glm52-native-mtp` → `NativeMtp`；共享 flag `--dflash-draft-model-path` → `Dspark(path)`；否则 `None`）；`dp_size` 缺省时取拓扑默认；`--glm52-ranks` 字符串在这里解析成 `Range<usize>`；最后调用 `crate::launch(...)` 并把 `EngineHandle` 包成 `LaunchedEngine::Handle` 返回。

选项结构体本身：

- [src/lib.rs:L144-L195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L144-L195)：`Glm52LaunchOptions` 的每个字段都带文档注释说明语义与默认行为（如 `max_model_len: None` 表示从权重加载后的空闲显存推导）。

第二层权威校验的代表性片段：

- [src/lib.rs:L453-L473](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L453-L473)：行步预算门。\( \text{slots} \times (1 + \text{drafts}) \le \text{GLM52\_MAX\_STEP\_ROWS}(=96) \)。注释点名了 #812 的原始故障形态：超额的 (slots, drafts) 组合会静默截断 verify span 而不是报错，所以这里必须 fail-fast。
- [src/lib.rs:L409-L427](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L409-L427)：`ensure_blackwell_devices(local_gpus)`——注意参数是**本进程持有的 GPU 数**而非全局 EP 宽度：EP16 的某个节点只 host 4 张卡，探测设备 4+ 会直接越界。每张卡查询 `CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR`，要求 ≥10（Blackwell）。
- [src/lib.rs:L477-L498](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L477-L498)：多进程形态的门：`ranks != 0..device_count` 即多进程，必须给 `--glm52-rendezvous`；TP4 是单进程拓扑（worker 在进程内 rendezvous 设备指针），必须 host 全部 rank。
- [src/lib.rs:L506-L533](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L506-L533)：按拓扑钉死 TP/DP——TP4 要求 tp=4/dp=1 且必须 prefill-only；EP 档要求 tp=1/dp=设备数。与第一层的 `validate` 一一对应。

#### 4.1.4 代码实践

**实践目标**：不碰 GPU，亲眼看两层校验对同一组非法参数给出什么错误，并验证「拓扑派生量」的纯算术。

**操作步骤**：

1. 运行 model_line 的纯 CPU 测试组（这些测试用 `parse_for_line` 直接在内存里构造 argv，不触碰 CUDA）：

   ```bash
   cargo test --release -p pegainfer-glm52 --features glm52 --lib model_line -- --nocapture
   ```

2. 挑三个测试对照源码读断言：
   - `rejects_unknown_moe_topo`（[src/model_line.rs:L399-L408](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L399-L408)）——期望错误文案含 `ep4, ep8, ep16, ep32, ep64, or tp4`；
   - `native_mtp_rejects_tp_topology`（[src/model_line.rs:L340-L355](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L340-L355)）——期望文案含 `--moe-topo=ep4 or ep8`；
   - `serve_plan_counts_partial_fleet_ranks`（[src/model_line.rs:L476-L501](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L476-L501)）——`--glm52-ranks 4..8` 时 `scheduler_partition_count == 4`。
3. 再读 `Glm52MoeTopo` 的 `topology_tests`（[src/lib.rs:L310-L369](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L310-L369)），手工核对 EP64 行：`default_dp_size=64`、`ep_local_experts=256/64=4`。

**需要观察的现象**：测试全部通过；`--nocapture` 下无任何 GPU/CUDA 初始化日志（证明这层校验纯 CPU）。

**预期结果**：约 20 个 model_line 测试与 4 个 topology 测试全绿。具体输出**待本地验证**（本讲义未在本机运行该命令）。

#### 4.1.5 小练习与答案

**练习 1**：用户执行 `pegainfer --model-path ... --moe-topo ep4 --tp-size 2`。哪一层校验先拒绝？错误信息大致是什么？

**答案**：第一层 `Glm52Line::validate` 先拒绝（[src/model_line.rs:L139-L145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L139-L145)）：`expected_tp_size()` 对 Ep4 返回 1，与 `--tp-size 2` 不符，报 `GLM5.2 --moe-topo=ep4 requires --tp-size=1, got 2`。若绕过 CLI 直接调 `crate::launch`，第二层 [src/lib.rs:L522-L532](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L522-L532) 会以等价语义再次拒绝。

**练习 2**：为什么 `--dp-size` 是「给了才校验」而 `--tp-size` 是无条件校验？

**答案**：`dp_size` 在 `Glm52LaunchOptions` 里是 `Option`，缺省时由 `moe_topo.default_dp_size()` 补齐（[src/model_line.rs:L245](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L245)），所以只有显式给错才需要报错；`tp_size` 是必填值且共享默认值 1，恰好 EP 档要求 1、TP4 要求 4，必须无条件比对（TP4 不给 `--tp-size 4` 时默认 1 会被拒，见测试 `tp4_rejects_omitted_tp_size`）。

**练习 3**：`ensure_blackwell_devices` 为什么参数是 `local_gpus` 而不是 EP 宽度？

**答案**：多进程 fleet 上每个进程只 host 自己那段 rank 区间，本地设备 ordinal 是 `0..ranks.len()`（[src/lib.rs:L474-L476](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L474-L476) 的注释）。EP16 单节点 4 卡的进程若按全局宽度探测设备 4..16，会在不存在的设备上打开上下文而误报启动失败。

### 4.2 start_engine 六阶段：从校验到引擎就绪

#### 4.2.1 概念说明

`start_engine` 是启动的主体：它把「一份合法选项 + 一个模型目录」变成「一组已就绪、正在 serve 的引擎线程 + 一个 EngineHandle」。整个过程分六个阶段，每个阶段的产出恰好是下一阶段的输入，顺序不可调换：

| 阶段 | 函数/代码块 | 产出 | 为什么必须在此时 |
|---|---|---|---|
| ① 启动校验 | `validate_startup` | `StartupValidation`（rank 区间、每 rank 加载包、专家区间） | 纯 CPU、只读 checkpoint 索引；任何布局不符都在碰 GPU 前失败 |
| ② 权重上 GPU | `load_rank_weights_to_gpu` | worker 线程池 + 每卡空闲显存报告 | 空闲显存必须**权重落位之后**测量才真实 |
| ③ 显存预算 | `derive_max_model_len` 等 | `Glm52ContextBudget`（上下文上限 cap、池下限） | cap 决定阶段④要建的 arena 尺寸，必须先算 |
| ④ 两阶段构建 + 通信 | `build_rank_models` | 每 rank arena 描述符 + KV 池块数 + DeepEP/NCCL 上下文 | 池块数要靠阶段④内部的实测空闲显存决定；通信上下文是集体操作，必须在全员构建成功之后 |
| ⑤ 池与 KV 存储 | `BlockPool` 构造 + `post_comm_startup` | 共享 `KvStore`、（可选）DSpark 权重、构建后余量复检 | store 的 rank 表在构建时冻结，必须先于引擎 spawn |
| ⑥ 引擎 spawn 与 barrier | spawn 循环 + bootstrap 等待 | 引擎线程已预捕获 CUDA Graph 并报告；返回 `EngineHandle` | 「预捕获即会合」——只有全员报告成功，launch 才能返回 |

两个贯穿性的设计词汇：

- **两阶段构建（two-phase build，#818）**：阶段④内部又分「固定半场（BuildFixed，非集体）→ 实测池填充 → FinishKv → SetupComm（集体）」。把可独立失败的构建放在任何集体操作**之前**，一个 rank 构建失败就不会把同伴晾在 NCCL 初始化里。
- **实测优先**：凡是能用真实测量替代估算的地方（空闲显存、每块 slab 边际成本）都用测量；无法测量的（图捕获余量）才用 reserve 常数兜底。

#### 4.2.2 核心流程

```text
start_engine(model_path, options, ...)
│
├─① validate_startup
│    读 config.json → probe_config_json → 校验 ranks∈0..device_count
│    → 校验 TP/DP/EP 与拓扑一致 → 解析权重清单 → 切分每 rank 加载包
│
├─② load_rank_weights_to_gpu
│    逐 rank spawn worker 线程（每线程绑定一张卡的 CUDA 上下文）
│    → 并发下发 LoadWeights 指令 → 收集报告（张量数/字节数/空闲显存）
│
├─③ 显存预算
│    fleet 最小空闲显存 − 已知后续开销 → derive_max_model_len
│    → cap（显式给定则校验，缺省则二分推导）→ floor_blocks
│
├─④ build_rank_models（两阶段）
│    BuildFixed（非集体，全员回报空闲显存）→ min-free 决定 pool_blocks
│    → FinishKv（非集体，全员回报 arena）→ rendezvous 取 DeepEP unique id
│    → SetupComm（集体）→ 通信上下文就绪
│    ★ 任一步失败：先给所有 worker 广播 Shutdown，再返回错误
│
├─⑤ BlockPool × 逻辑 rank + post_comm_startup
│    （TP4: prefill kernel 预检）→（DSpark: 加载草稿权重）
│    → ensure_post_build_headroom 余量复检 → build_kv_store
│    ★ 失败：同样先广播 Shutdown
│
├─⑥ spawn 引擎 + bootstrap barrier
│    每逻辑 rank 一个 Glm52EngineSpec → Glm52Engine::spawn
│    → 引擎线程 bootstrap()：预捕获全 bucket CUDA Graph（即会合点）
│    → startup_tx 每引擎恰报一次 → 逐个 recv（任一失败 → abort_engines）
│    →（可选）rank0 graph dump 应答
│
└─ 组装 EngineHandle 返回
```

#### 4.2.3 源码精读

**阶段①** `validate_startup`：

- [src/lib.rs:L1635-L1666](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1635-L1666)：读入并解析 `config.json`，先过 `probe_config_json`（u2-l1 会逐字段精读），再校验 rank 区间落在 `0..device_count` 内，最后一次性校验 TP/DP/EP 三元组与拓扑派生量一致——这是第二层校验在启动路径上的延续。
- [src/lib.rs:L1668-L1675](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1668-L1675)：解析 `Glm52WeightManifest`（checkpoint 的 `model.safetensors.index.json`），调 `all_rank_load_bundles` 按拓扑切出**每个 rank 的加载包**，再从本进程 hosting 的区间抽出张量数与专家区间。产出 `StartupValidation`。

**阶段②** `load_rank_weights_to_gpu`：

- [src/lib.rs:L1710-L1719](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1710-L1719)：对区间内每个全局 rank 构造 `Glm52RankPlacement { rank, device_ordinal }` 并 `Glm52RankWorker::spawn`。设备 ordinal 是**本地序号**（区间内第 i 个），rank 是**全局编号**（`ranks.start + i`）——多进程 fleet 下这两个数不再相等。
- [src/lib.rs:L1732-L1751](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1732-L1751)：先对每个 worker 调 `load_weights_async` 拿到响应通道（指令已全部入队），再逐个 `recv` 等报告；报告里 `rank` 必须等于重算出的全局 rank 且 `loaded_to_gpu` 为真，否则视为无效报告。**「先全部发送、再统一等待」**是这个启动器贯穿始终的并发模式。

worker 线程的诞生地与启动握手：

- [src/runner.rs:L320-L353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L320-L353)：`Glm52RankWorker::spawn` 先断言加载包的 rank 与 placement 一致，建 unbounded 指令通道 + `bounded(1)` 启动通道，然后 `thread::Builder::name("glm52-rank-{rank}")` 起线程：线程内先开 `Glm52RankGpuContext`（绑定 CUDA 上下文，失败则从启动通道报错返回），成功后立即回报 `Ok(())` 再进入 `rank_worker_loop`。spawn 侧阻塞在 `startup_rx.recv()`——**子线程确认拿到 CUDA 上下文之前，spawn 不返回**。

**阶段③** 显存预算：

- [src/lib.rs:L916-L939](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L916-L939)：取 fleet 最小空闲显存；扣除两项**精确已知**的权重后开销（MLA qa|kv_a 孪生缓冲、DeepGEMM 增量），交给 `derive_max_model_len`。注释说明了记账哲学：这些分配落在权重探测之后，若不在这里精确扣款，auto 模式就会把这笔显存花在 KV 上、然后在图捕获余量门上失败。
- `derive_max_model_len` 本体（[src/lib.rs:L743-L855](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L743-L855)）是纯函数（无 CUDA），显式 cap 走校验分支（对齐、上下限、单请求池可负担），缺省 cap 在对齐候选上二分搜索。u2-l4 会精读，本讲只需知道它的产出 `Glm52ContextBudget` 决定了阶段④的 arena 尺寸与池下限。

**阶段④** `build_rank_models` 的两段式：

- [src/lib.rs:L1329-L1359](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1329-L1359)：第一段——向每个 worker 下发 `BuildFixed`（非集体）：把已驻留权重收编进除池规模 KV slab 之外的一切结构，回报**该卡实测空闲显存**。函数 doc（L1319-L1327）说明了为什么分两段：构建是 per-rank 的、可能失败（OOM 等），每个 rank 必须先回报成功，才允许任何人进入集体上下文创建，否则一个失败会把同伴晾在集体初始化里且没有有用错误。
- [src/lib.rs:L1360-L1421](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1360-L1421)：取 fleet 最小空闲；用同一个账本函数算出**每池块的边际 slab 成本**（线性账本，一次差分即边际）；`glm52_measured_pool_blocks` 把可用显存按边际成本换成块数（下限 floor 不降）；`GLM52_KV_POOL_BLOCKS` 环境变量可钉住块数，但必须落在「单请求池下限」与「实测可负担上限」两道栅栏之间，越界即拒绝启动。
- [src/lib.rs:L1435-L1447](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1435-L1447)：第二段——下发 `FinishKv`（非集体）：按定下的块数分配池规模 slab、组装 rank 运行时，回报 arena 描述符（后续 KV 卸载注册用）。
- [src/lib.rs:L1448-L1483](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1448-L1483)：集体段——EP 拓扑经 `rendezvous::unique_id` 拿 DeepEP unique id（单进程 fleet 在进程内生成、不碰网络）；TP 拓扑借用 EP8 shim 的生成器拿一个 NCCL id，并在 worker 初始化通信器**之前**设置 NCCL 通道数环境变量（默认 2 通道会把 all-reduce 卡在约 46 GB/s，16..32 通道恢复约 5 倍——用户已设的值优先）。最后全员下发 `SetupComm`（集体）。

**阶段⑤** 池与 KV 存储：

- [src/lib.rs:L1021-L1033](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1021-L1033）：每逻辑 rank 一个 `Arc<BlockPool>`。注释说明：池必须在引擎 spawn **之前**建好，因为 store 的 rank 表在构建时冻结，而 BlockPool 是无线程亲和的纯 CPU 对象，可以与引擎共享同一个 Arc。TP 镜像拓扑折叠为 1 个逻辑 rank。
- [src/lib.rs:L1034-L1077](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1034-L1077)：`post_comm_startup` 闭包按序做四件事：TP4 时先跑 prefill kernel 预检（用一行 padding batch 真跑一遍，确认 JIT/链接无缺）；DSpark 时全员加载草稿权重；`ensure_post_build_headroom` 复检每卡空闲显存 ≥ 1 GiB（reserve/账本漂移在这里崩，而不是服务中途 OOM 撕裂集体组）；最后 `build_kv_store` 注册 KV 存储（无卸载时只是把每 rank 的池登记进 store）。

**阶段⑥** 引擎 spawn 与 bootstrap barrier：

- [src/lib.rs:L1110-L1168](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1110-L1168)：把 worker 分组（镜像拓扑=全部 worker 归一个引擎；EP=每 worker 一引擎），逐逻辑 rank 建提交通道（tokio unbounded）、启动报告通道（crossbeam bounded(1)）、组装 `Glm52EngineSpec`，调 `scheduler::Glm52Engine::spawn`。**spawn 失败窗口**：若第 k 个引擎 spawn 失败，先 `drop(submit_txs)` 关闭全部提交队列，再 join 已 spawn 的引擎线程，然后返回错误。
- [src/scheduler/mod.rs:L294-L309](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L294-L309)：`Glm52Engine::spawn` 只是把 `Glm52Engine::new(spec)` 与 `run()` 搬进名为 `glm52-engine-{rank}` 的线程；构造失败也要经 `startup_tx` 报告，让 spec 的 Drop 负责拆掉本 rank 的 worker。
- [src/scheduler/mod.rs:L380-L407](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L380-L407)：`run` 的骨架：`bootstrap()` 成功 → `startup_tx.send(Ok(()))` → `serve_loop()` → `teardown()`；bootstrap 失败 → 报错 + 关停 worker 后直接返回。`bootstrap` 内部（decode 引擎）调 `precapture_step_graphs` 预捕获全部 bucket 的整步 CUDA Graph——注释点题：**每个引擎捕获同一套固定 bucket 序列，捕获内的集体操作天然跨 fleet 配对，预捕获本身就是会合，无需任何 coordinator**。这就是 u1-l3 提过的「预捕获即会合」在源码里的落点。
- [src/lib.rs:L1182-L1195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1182-L1195)：launch 侧逐引擎 `startup_rx.recv()`。三种失败：报告 `Err`（引擎自报）、通道关闭（引擎提前死亡）、graph dump 应答失败——统一走 `abort_engines`（见 4.3）。

**终点** `EngineHandle` 组装：

- [src/lib.rs:L1217-L1230](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1217-L1230)：`EngineHandle::new_with_join_handles(submit_txs, join_handles)` 再链式挂三样元数据：`with_servable_len(cap)`（把 launch 期定下的 cap 发布给前端，前端据此钳制 API 边界的 max_position_embeddings，1M 的 checkpoint 值不会漏出去）、`with_kv_capacity`（总块数=pool_blocks−1，块大小=64）、`with_metrics_watches`（每分区一条负载 watch）。
- [../pegainfer-frontend/src/engine/handle.rs:L88-L105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-frontend/src/engine/handle.rs#L88-L105)：`new_with_join_handles` 的契约：每逻辑分区一条提交通道 + 一个线程 JoinHandle；`submit` 按请求的 `data_parallel_rank` 路由，未绑定的请求去最闲分区；**最后一个句柄克隆被 drop 时关闭所有通道并按分区顺序 join 所有线程**——这就是「拆引擎」的全部机制，引擎侧不需要额外的 shutdown RPC。

#### 4.2.4 代码实践

**实践目标**：把六个阶段与启动日志一一对应，形成一张可复用的「启动时间线卡」。日志是阶段边界的可观测证据。

**操作步骤**（源码阅读型实践，无 GPU 也能完成前三步）：

1. 在 `src/lib.rs` 中 `Grep` 所有 `log::info!`，找出六个阶段各自的日志行。参考答案（均已在上文源码精读中出现）：

   | 日志关键词 | 所在函数 | 阶段 |
   |---|---|---|
   | `load-weight startup validated` | `validate_startup`（L1677） | ① |
   | `rank weight load cost` | `load_rank_weights_to_gpu`（L1764） | ② |
   | `max_model_len=` | `start_engine`（L950） | ③ |
   | `KV pool: ... blocks` | `build_rank_models`（L1422） | ④ |
   | `rank models built in` | `build_rank_models`（L1484） | ④ |
   | `post-build free VRAM per rank` | `ensure_post_build_headroom`（L1312） | ⑤ |
   | （无专属日志；上一条之后的静默段） | spawn 循环 + bootstrap | ⑥ |

2. 在 GPU 机器上以默认拓扑启动并抓取日志（**待本地验证**，本讲义未运行）：

   ```bash
   RUST_LOG=info cargo run --release --features glm52 -- \
       --model-path models/GLM5.2 2>&1 | tee /tmp/glm52-boot.log
   ```

3. 把 `/tmp/glm52-boot.log` 的行按上表归档到六个阶段，记录各阶段耗时感（①② 的日志自带秒数）。
4. 加一个对照变量重跑一次：`GLM52_KV_POOL_BLOCKS=<你在上一次日志里看到的 blocks 数>`，确认阶段④的 `KV pool:` 行从「measured … fill」语义变为 pinned 值（且能通过两道栅栏）。

**需要观察的现象**：日志行的顺序与六阶段顺序完全一致；`KV pool:` 行出现在 `rank models built` 之前（池决定在 FinishKv 之前）；`max_model_len=` 行出现在权重加载日志之后（预算依赖实测显存）。

**预期结果**：得到一张「日志行 ↔ 阶段 ↔ 关键数字（cap、blocks、每卡空闲显存）」的启动档案。GPU 上的实际数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么空闲显存测量放在阶段②（权重加载后）而不是阶段①之前？

**答案**：cap 预算的分子是「权重驻留后剩下的显存」。权重是最大的一笔固定占用（EP8 下非专家栈全量复制 + 本地专家切片），不先落位就无法知道真实余量；提前测量会把权重体积误当成可分配预算，导致 cap 过大、后续 FinishKv 或图捕获 OOM。

**练习 2**：`build_rank_models` 里为什么 BuildFixed/FinishKv 是「先全部下发、再逐个收」，而 SetupComm 也这样却多一层含义？

**答案**：「先全部下发、再统一等待」是让各 worker 并行干活的基本模式。BuildFixed/FinishKv 是非集体指令，各 worker 独立完成；SetupComm 是集体指令，各 worker 内部会在 DeepEP/NCCL 初始化处互相等待——全员**同时在场**正是集体操作完成的前提，所以启动器必须保证全员构建成功后才下发它（函数 doc，[src/lib.rs:L1319-L1327](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1319-L1327)）。

**练习 3**：阶段⑥里，若某个引擎的 `startup_rx.recv()` 返回 `Err(_)`（通道关闭、引擎提前死亡），启动器做什么？

**答案**：视为 bootstrap 失败，构造 `anyhow!("GLM5.2 engine {engine_index} exited before reporting bootstrap")`，调 `abort_engines(submit_txs, join_handles)`（drop 全部提交通道 → 存活引擎的 serve_loop 因通道关闭而退出并各自 teardown → join），然后返回错误（[src/lib.rs:L1182-L1195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1182-L1195)）。

### 4.3 EngineHandle 组装与失败拆除

#### 4.3.1 概念说明

启动失败的拆除不是一句 `return Err` 那么简单：**不同时间点的失败，进程里活着的集体上下文不同，拆除方式必须与之匹配**。本模块讲三件事：

1. `EngineHandle` 的组装语义（哪些通道、哪些线程、哪些元数据）。
2. 三个失败窗口各自的拆除路径。
3. worker 的 `Drop` 约定如何让「广播 Shutdown」成为所有路径的共同底座。

核心约束来自 DeepEP/NCCL 上下文的销毁也是集体操作：DeepEP 上下文建立之后，任何一个 rank 的 worker 被**单独** join（销毁上下文）都会在销毁 barrier 上等永远不到场的同伴，直到约 100 秒的设备超时——错误最终还是能浮出来，但那不是可接受的启动体验。所以从 SetupComm 之后的任何失败，都必须**先广播 Shutdown 再逐个 join**。

#### 4.3.2 核心流程

三个失败窗口与拆除方式：

```text
窗口 A：SetupComm 已发出之后（阶段④后半 ~ 阶段⑤失败）
   for worker in &loaded.workers: worker.request_shutdown()   ← 先广播
   return Err
   （随后 loaded.workers 顺序 Drop：每个 Drop 再 send Shutdown + join，
     但因为全员已收到 Shutdown 并退出了循环，join 立刻返回，
     销毁 barrier 的参与者齐了）

窗口 B：引擎 spawn 失败（阶段⑥ spawn 循环内）
   drop(submit_txs)          ← 关闭所有提交队列
   for handle in join_handles: handle.join()   ← 存活引擎退出 loop 并 teardown
   return Err
   （引擎 teardown 各自关停自己的 worker）

窗口 C：bootstrap barrier 失败（某引擎报告 Err / 提前死亡 / graph dump 失败）
   abort_engines = drop(submit_txs) + join(join_handles)   ← 同窗口 B 的机制
   return Err(err.context("GLM5.2 engine bootstrap failed"))
```

窗口 A 之前的失败（阶段①②③、BuildFixed/FinishKv 失败）无需广播——那时还没有任何集体上下文，`?` 直接向上冒泡，worker 由 `LoadedGlm52Runtime` 的顺序 Drop 自然回收。

#### 4.3.3 源码精读

- [src/lib.rs:L993-L1020](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L993-L1020)：窗口 A 的原文与理由。注释写明：`build_rank_models` 内部会发 SetupComm，从此刻起 DeepEP 上下文存在且其销毁是**集体**的；此后任何启动失败必须先向每个 rank 广播 Shutdown，然后才让 worker 顺序 Drop 逐个 join——否则第一个被 Drop 的 worker 阻塞在销毁 barrier 上等从未收到关机通知的 rank，启动错误要等约 100 秒的 DeepEP 设备超时才浮出来。TP LL rendezvous 拒绝某拓扑（毒丸、NVLink 探测）就是真实落在这个窗口里的失败。
- [src/lib.rs:L1078-L1086](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1078-L1086)：阶段⑤ `post_comm_startup` 失败走同一个窗口 A 模式。
- [src/lib.rs:L1158-L1167](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1158-L1167)：窗口 B——spawn 循环里第 k 个引擎 spawn 失败：drop 掉全部 `submit_txs`（此时都是刚建的，还没有请求进来），join 已 spawn 的引擎，返回带 rank 的错误。
- [src/lib.rs:L1174-L1195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1174-L1195)：窗口 C——`abort_engines` 闭包与 bootstrap 等待循环。注释重申：任一失败就关闭所有队列，让**健康**引擎退出自己的 loop 并关停各自 worker（并发拆除），然后 join。
- [src/runner.rs:L539-L561](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L539-L561)：`request_shutdown`（向指令通道发 `Shutdown`）与 `impl Drop for Glm52RankWorker`（Drop = request_shutdown + join）。这保证了**任何**持有 worker 的结构被丢弃时，worker 一定被通知并回收——广播 Shutdown 只是「提前打招呼」，让后续的 join 不必等超时。
- [src/runner.rs:L1043-L1120](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L1043-L1120)：`rank_worker_loop` 的主循环匹配指令；`Glm52RankCommand::Shutdown => break`（L1119）之后还有收尾逻辑（排空在飞的 GPU 工作、TP LL 缓冲的拆除会合等，L1120 起的注释提到 launch-ahead 回放可能仍在飞）。
- [../pegainfer-frontend/src/engine/handle.rs:L88-L105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-frontend/src/engine/handle.rs#L88-L105) 与 [L151-L167](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-frontend/src/engine/handle.rs#L151-L167)：正常路径的「拆除」其实也是同一机制——最后一个句柄克隆被 drop，通道关闭、线程按分区顺序 join。启动失败路径只是把这件事提前到 launch 内部做掉了。

#### 4.3.4 代码实践

**实践目标**：静态梳理「哪一步失败会发生什么」，产出一张失败窗口对照表——这是日后排查启动失败的地图。

**操作步骤**（纯源码阅读，无需 GPU）：

1. 从 `start_engine`（[src/lib.rs:L894-L1231](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L894-L1231)）出发，列出所有 `return Err` 点，并为每个点标注：
   - 失败时进程里已存在什么（worker 线程？权重？DeepEP 上下文？引擎线程？）；
   - 拆除走窗口 A / B / C 还是「无需拆除的早失败」。
2. 补全下表（参考答案见步骤 3）：

   | 失败点 | 已存在 | 拆除方式 |
   |---|---|---|
   | `validate_startup` 返回 Err | 无 worker | 直接冒泡（`?`） |
   | `load_rank_weights_to_gpu` 中某 worker 加载失败 | worker 线程已 spawn | ?（待你填写） |
   | `build_rank_models` 中 SetupComm 失败 | worker + 权重 + DeepEP 上下文（部分） | ?（待你填写） |
   | 引擎 3/8 spawn 失败 | worker + 上下文 + 引擎 0..2 | 窗口 B |
   | 引擎 5 bootstrap 报告 Err | 全部引擎线程 | 窗口 C（abort_engines） |

3. 对照源码核对：加载失败点在 [src/lib.rs:L1742-L1744](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1742-L1744)（`??` 直接冒泡，`LoadedGlm52Runtime` 尚未构造、workers 向量随函数作用域 Drop，逐个 worker Drop 即回收——此时尚无集体上下文，无需广播）；SetupComm 失败点在 [src/lib.rs:L1474-L1483](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1474-L1483)（`??` 冒泡到调用方，调用方的 match 走窗口 A 广播）。
4. 思考验证：假设删掉窗口 A 的广播、只依赖 worker 顺序 Drop，启动一个 8 卡 fleet 后立刻用 `Ctrl+C` 杀掉 TP 拓扑中一个失败进程——你会观察到什么？（预期：错误浮出的时间量级是 DeepEP 设备超时 ≈ 100 秒，而不是立即。）此推演**待本地验证**。

**需要观察的现象**：每个 Err 点都能归入四类拆除方式之一；「广播 Shutdown」只出现在集体上下文存在之后的窗口。

**预期结果**：一张与源码一一对应的失败窗口对照表。

#### 4.3.5 小练习与答案

**练习 1**：为什么窗口 A 要「先广播、后 Drop」，而不能只靠 `LoadedGlm52Runtime` 里的 workers 向量顺序 Drop？

**答案**：Drop 的语义是「逐个 send Shutdown + join」。SetupComm 之后 DeepEP 上下文存在，其销毁是集体 barrier：第一个被 join 的 worker 会卡在销毁 barrier 等其他从未收到 Shutdown 的 rank——它们还在各自的指令循环里等下一条指令，永远不来。先广播让全员先退出循环、一起到达销毁点，join 才能即刻完成（[src/lib.rs:L993-L1001](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L993-L1001) 的注释）。

**练习 2**：`EngineHandle` 被 drop 时引擎线程如何得知该退出？

**答案**：没有显式的 shutdown 消息。最后一个句柄克隆 drop 使全部 `submit_txs` 关闭，每个引擎线程的 `submit_rx` 因此收到「通道关闭」，serve_loop 退出、进入 teardown（契约见 [../pegainfer-frontend/src/engine/handle.rs:L88-L105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-frontend/src/engine/handle.rs#L88-L105)，然后 join 按分区顺序回收线程）。窗口 B/C 的 `drop(submit_txs)` / `abort_engines` 复用的正是这同一机制。

**练习 3**：`Glm52RankWorker::spawn` 里那个 `bounded::<Result<()>>(1)` 启动通道解决什么问题？

**答案**：它把「线程起来了」和「CUDA 上下文打开成功」区分开。`thread::Builder::spawn` 成功只保证线程存在；线程内部 `Glm52RankGpuContext::new(device_ordinal)` 可能失败（设备被占用等）。启动通道让 spawn 侧阻塞等这条一次性报告，失败直接把错误带回调用方（[src/runner.rs:L332-L348](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L332-L348)），而不是留下一个空转线程。

## 5. 综合实践

**任务：为一次真实的 EP8 启动画出完整的「启动序列图 + 失败窗口标注」。**

1. **画序列图**（纯纸面，无需 GPU）：参与者五列——`Glm52Line::launch`（启动器）、`start_engine`、`worker×8`（GPU 线程）、`engine×8`（调度线程）、`frontend`。按时间从上到下标出：
   - 指令流：`LoadWeights ×8 → BuildFixed ×8 → FinishKv ×8 → SetupComm ×8 →（引擎线程内）预捕获 Step ×逐 bucket`；
   - 每条指令旁边的响应通道（`bounded(1)`）与启动器等待点；
   - rendezvous：单进程 fleet 下 unique id 在进程内生成、不碰网络（[src/lib.rs:L1448-L1457](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1448-L1457)）；
   - 最后的 `startup_tx` 报告（`Ok`）×8 与 `EngineHandle` 交付。
2. **标注三个失败窗口**：在序列图右侧用竖线划出窗口 A/B/C 的起止位置，各写一句「此时谁已存在、拆除先做什么」。
3. **（可选，需 GPU）实测验证**：用 4.2.4 的命令抓一份启动日志，把每条 `log::info!` 贴回序列图的对应位置；再用一个非法组合（如 `--moe-topo ep8 --tp-size 2`）确认失败发生在任何日志之前（第一层校验）。日志与失败行为均**待本地验证**。

完成的序列图就是你自己的启动链路速查表：u6-l3（CUDA Graph 预捕获）与 u7-l1（free-running 引擎）都会在这张图的右下角继续展开。

## 6. 本讲小结

- 启动选项经过**两层校验**：`Glm52Line::validate`（server 组装期、纯 CPU、组合规则）与 `crate::launch` 的 ensure 链（权威层）；两层共享 `Glm52MoeTopo` 的派生量，一个拓扑枚举同时钉死 TP/DP/EP、设备数与每 rank 专家数。
- `start_engine` 六阶段不可换序：①启动校验（纯 CPU）→ ②权重上 GPU（worker 线程 + 实测空闲显存）→ ③显存预算（cap 与池下限）→ ④两阶段构建 + 集体通信（BuildFixed → 实测池填充 → FinishKv → SetupComm）→ ⑤BlockPool 与 KV 存储 → ⑥引擎 spawn 与 bootstrap barrier。
- 「两阶段构建」的要义：把可能失败的 per-rank 构建放在任何集体操作之前，全员成功才进入 DeepEP/NCCL 初始化。
- bootstrap barrier 的实现是「预捕获即会合」：每个引擎按同一套固定 bucket 序列预捕获整步 CUDA Graph，捕获内的集体操作天然配对，无需 coordinator；全部引擎经 `startup_tx` 报告成功后 launch 才返回。
- 失败拆除按时间窗口分三类：SetupComm 之后先**广播 Shutdown** 再顺序 Drop（销毁是集体 barrier）；引擎 spawn 失败与 bootstrap 失败靠关闭提交队列让健康引擎自行退出再 join；早失败直接冒泡即可。
- `EngineHandle` 是链路终点：每逻辑分区一条提交通道 + 一个线程 JoinHandle，外加 servable_len、KV 容量与负载 watch 三样元数据；句柄的 drop 语义（关通道、按序 join）同时也是正常关停机制。

## 7. 下一步学习建议

- 下一讲 u2-l1 进入**模型几何常量与 config.json 探针**：本讲阶段①只是一笔带过的 `probe_config_json`，那里会逐字段讲清 78 层/6144 hidden/256+1 专家等常量如何被钉死。
- 若想先看引擎拿到首条请求后的世界，直接跳读 `src/scheduler/mod.rs` 的 `serve_loop`（[src/scheduler/mod.rs:L380-L389](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L380-L389) 起的 `run` 骨架），配合 `docs/models/glm52/free-running-dp.md` §2 的三条纪律。
- 推荐按顺序阅读的源码：`src/runner.rs` 的 `Glm52RankCommand` 枚举（指令协议全貌）→ `src/lib.rs` 的 `build_rank_models`（两阶段构建的完整实现）→ `../pegainfer-frontend/src/model_line.rs` 的 `ModelLine` trait（理解其他模型线如何复用同一契约）。
