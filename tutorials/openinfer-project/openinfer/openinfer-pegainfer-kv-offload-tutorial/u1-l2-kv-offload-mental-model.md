# 心智模型：为什么需要 KV 卸载，三层存储与 P/D 分离

## 1. 本讲目标

上一讲（u1-l1）我们看清了这个 crate 的**门面**：三方角色、导出面、依赖决策。本讲把视角从「静态结构」切换到「动态行为」，建立一整套能陪你走完整个学习手册的**运行时心智模型**。读完本讲，你应该能够：

1. **解释前缀缓存为什么能跨请求复用**：块哈希链如何让「同一前缀 = 同一内容 = 同一哈希」无需任何协调就成立；GPU 前缀命中（kvbm `BlockPool`）与 CPU 前缀命中（本 crate）如何**叠成一段连续前缀**（都锚定 prefix 0、单点切开），以及为什么 GPU↔CPU 交错形态被刻意排除。
2. **区分 save 与 load 的时序语义**：save 是 fire-and-forget 尽力而为（丢了只损失一次命中机会），load 在关键路径上、强排序，但**绝不阻塞 admission**——一切经可轮询句柄在每个调度 tick 收账。
3. **讲清 P/D 分离数据面**：prefill 节点与 decode 节点如何靠内容寻址**免掉一切 handle 协议**完成 KV 交接，以及故障时如何优雅退化。

本讲的实践仍是**源码阅读 + 手推演算**型——心智模型先立起来，u1-l3 再谈怎么跑测试。

## 2. 前置知识

### 2.1 prefill 与 decode：两种完全不同的负载

LLM 推理分两个阶段：

- **prefill（预填充）**：处理输入 prompt 的全部 token，**一次性并行**算出它们的 KV cache 并产出第一个输出 token。计算量大、显存带宽压力小、耗时与 prompt 长度成正比。
- **decode（解码）**：逐个生成后续 token，每步只算 1 个 token 的 KV，但要**读全量历史 KV**。计算量小、带宽压力大、耗时与生成长度成正比。

多轮对话是天然的重复计算温床：第 N 轮的 prompt = 系统提示 + 前 N−1 轮的全部问答。这些历史 token 的 KV 在上一轮**已经算过且一字不差**——重算是纯浪费，这就是前缀缓存的动机，也是「把 KV 存到便宜层、命中时搬回来」的全部价值来源。

### 2.2 一个 KV 块有多大（示例计算）

以本 crate v1 的目标模型 Qwen3-4B 为例（几何 36 层 / 8 个 KV 头 / 头维 128，bf16，出处见 [docs/models/qwen3/pd-disaggregation-m2.md:L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L22)），每个 token 的 KV 体积为：

\[
\text{bytes/token} = L_{\text{layers}} \times 2_{(K|V)} \times H_{kv} \times d_{\text{head}} \times s_{\text{dtype}} = 36 \times 2 \times 8 \times 128 \times 2 = 147456\ \text{B} \approx 144\ \text{KiB}
\]

分页 KV 按块（page）管理，一块 16 个 token：

\[
\text{bytes/block} = 144\ \text{KiB} \times 16 = 2304\ \text{KiB} = 2.25\ \text{MiB}
\]

一个 ~600 token 的 prompt ≈ 38 块 ≈ 84 MiB——GPU HBM（几十 GB，还要放权重和活跃请求）装不了多少个对话的历史，而 CPU DRAM 动辄几百 GB。**「块」是整个体系的计量单位**：save 存的是块、query 数的是块、lease 钉的是块、RDMA 拉的也是块（实测 33 块 = 74.2 MiB，见 §4.4）。

### 2.3 关键路径 vs best-effort（尽力而为）

- **关键路径（critical path）**：直接影响用户可见延迟的操作（比如生成第一个 token 前必须拿到 KV）。它错了、慢了，用户立刻有感。
- **best-effort（尽力而为）**：失败了只损失「优化机会」而不影响正确性的操作。它可以随便丢、随时重试，永远不该让请求等待它。

本讲的核心张力就在这：**save 是 best-effort，load 在关键路径上但不许阻塞**。记住这条红线，后面所有源码设计都能对上号。

### 2.4 admission（准入）与调度 tick

- **admission（准入）**：调度器决定「哪个等待中的请求可以进入本步骤的批次」的动作。准入时要为新请求分配 KV 块、做前缀匹配。「load 不阻塞 admission」= 就算 KV 还在从 CPU 搬来的路上，调度器照样继续接纳、运行其他请求，搬完再说。
- **调度 tick**：调度循环的一次迭代。本 crate 的所有异步操作都以「提交后立即返回句柄、每个 tick 非阻塞 `poll()` 一下」的节奏收账——像收邮件，而不是守在门口等快递。

### 2.5 承接 u1-l1

一句话回顾三方角色：**pegainfer 拥有 GPU 分页 KV（`KvBuffer` + kvbm `BlockPool`）；pegaflow 拥有 host/SSD/RDMA 深层；`OffloadEngine` 是进程内的连接器大脑。** 本讲就沿着这条分界线看数据怎么流。

## 3. 本讲源码地图

本讲跨越 crate 源码与两份设计文档，重心在「文档注释与设计 spec」——它们是行为契约的权威出处：

| 文件 | 作用 | 本讲涉及程度 |
| --- | --- | --- |
| `src/lib.rs` | crate 级文档注释：定位声明 + v1 行为契约（组合命中形态、save/load 语义红线） | **主战场**（只读前 15 行文档） |
| `src/engine.rs` | `OffloadEngine`：save/query/load/flush 的入口与时序契约 | 精读 save/query/load 的**文档注释与方法签名**，实现细节留给 u2 |
| `src/handle.rs` | `OffloadHandle<T>` 可轮询句柄、`QueryOutcome` 两分支 | 精读模块文档与 `poll` |
| `docs/subsystems/runtime/pegaflow-offload-integration.md` | 本 crate 设计 spec：分层架构图（§1）、live 接线设计（§9） | **主战场** |
| `docs/models/qwen3/pd-disaggregation-m2.md` | P/D 分离 M2 实测报告：架构（§1）、验收数据（§3） | **主战场**（P/D 背景权威出处） |
| `README.md` | 仓库总览：Architecture 一节的 KV infrastructure 行 | 旁证 |

> 说明：本 crate 目录下没有单独的 README，`README.md` 指仓库根目录那份，其中一行「PegaFlow supplies host, SSD, and RDMA storage/transfer」是分层存储最简短的外部陈述。

## 4. 核心概念与源码讲解

### 4.1 前缀缓存如何跨请求复用：内容寻址的块哈希链

#### 4.1.1 概念说明

怎么让「请求 B 存到 CPU 的块」被「请求 C」认出来是自己的前缀？如果靠中央登记簿（请求号 → 块号），跨请求、跨进程、跨机器都要协调，复杂度爆炸。本体系选择**内容寻址（content addressing）**：

- KV 缓冲区按块切分（一块 16 token）。第 i 块的键 = **哈希(父块键 ‖ 第 i 块的 token 序列)**——一条**链式哈希**，和 Git commit、区块链头的构造同理。
- 于是「相同的前缀 token」必然推出**完全相同的键链**：请求 C 拿自己的 prompt 逐块算键，拿这些键去查 CPU 层，查到的就是别人存的那份一模一样的 KV。**存的人和使用者之间不需要任何协议、登记或握手**——这就是后面 P/D 免 handle 协议的根基。

「哈希链」还有个关键性质：**键只认内容不认位置**。GPU 上的物理块号（slot）、CPU pinned pool 里的位置都不进键，所以块被驱逐、重排、搬到别的机器后，键依然有效。

#### 4.1.2 核心流程

一个新请求 admission 时，前缀匹配被拆成两段**叠加**：

```text
prompt 的块哈希链:  h0 → h1 → h2 → h3 → h4 → h5 ...
                    └──── GPU 命中 G 块 ────┘└── 未命中段，去 CPU 查 ──┘

第一步  kvbm BlockPool（GPU 层，pegainfer 原生）: 数出连续命中前缀 G 块
第二步  OffloadEngine.query（CPU 层，本 crate）: 对 h[G..F] 数出连续命中 C 块
        C == 0            → 纯 GPU hit，直接 prefill 剩余 suffix
        C > 0             → load 把 C 块拷回 GPU，注册进 BlockPool
组合结果  [0, G+C) 是一段连续前缀，单点切开于 G —— GPU 命中段在前，CPU 命中段紧随其后
```

两条形态约束（v1 设计边界，lib.rs 文档原文）：

1. **双向都锚定 prefix 0**：GPU 命中段从块 0 开始，CPU 命中段从块 G 开始，组合命中永远是一段**连续**前缀。GPU→CPU→GPU 的斑马纹交错被**刻意排除**——交错意味着前缀中有洞，恢复与匹配都复杂得多，而真实负载里几乎不出现（u1-l1 练习 2 已论证过）。
2. **dense attention v1**：这条边界只对稠密全注意力（Qwen3-4B）成立。Qwen3.5 的 linear/recurrent 状态每请求独立、零跨请求复用，明确不进 connector（u3-l5 展开）。

load 完成后的收尾很巧妙：恢复的 C 块被 `register_block` 注册进 kvbm registry，再调一次现成的 `match_and_add_prefix`——在它眼里 GPU+CPU 命中就是**一段连续前缀**，kvbm 零改动。

#### 4.1.3 源码精读

v1 行为契约的第一权威出处是 crate 级文档：

> [src/lib.rs:L8-L15](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L8-L15) —— 逐句读：①「GPU 前缀命中仍是 kvbm `BlockPool` 的原生职责，本连接器只覆盖 CPU 层」；②「CPU 命中前缀**叠加**在 GPU 命中前缀之上（**都锚定 prefix 0**，组合命中是一段连续前缀、单点切开——GPU→CPU→GPU 交错被刻意排除）」；③「save 尽力而为 fire-and-forget；load 在关键路径上、强排序，但**绝不阻塞 admission**，一切操作经可轮询 `OffloadHandle` 由请求在每个调度 tick 收账」。

键的形态（kvbm↔bytes 桥）在 spec §0 有一行总结：

> [docs/subsystems/runtime/pegaflow-offload-integration.md:L15](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L15) —— kv-cache 侧的 `RequestKv` 暴露 `prompt_block_hashes` / `assigned_block_hashes` / `prefix_matched_blocks` 三个桥接口；`SequenceHash::as_u128()` 把 kvbm 的序列哈希转成 16 字节内容键。（与 vLLM 互操作的那套 xxh3_128+CBOR 哈希是另一条链，u3-l1 专讲。）

admission 的 G+C 拆解，权威出处是 spec §9 的 LOAD 设计：

> [docs/subsystems/runtime/pegaflow-offload-integration.md:L135-L142](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L135-L142) —— 六步：建 `RequestKv` 取 F 块哈希 → `match_blocks` 数 GPU 命中 G（**持其 `ImmutableBlock` 不 drop，防 load 期间被 evict**）→ `query` 查 CPU [G..F] 得 C + lease → 分配 C 个 GPU 目标块、`load` 起异步 H2D，请求进 holding 态**不 prefill** → 每 tick `poll()`，`Ready` 后注册 C 块再 `match_and_add_prefix` 自然命中 G+C 连续前缀 → `C==0` 直接 prefill。
>
> [docs/subsystems/runtime/pegaflow-offload-integration.md:L144](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L144) —— 「为何 register→rematch 而非直接注入 sequence」：复用现成匹配逻辑、零 kvbm 改动；最坏（真被 evict）只是少命中、退化为多 prefill，**不损正确性**。

组合命中形态已被真实 GPU 测试钉死：

> [docs/subsystems/runtime/pegaflow-offload-integration.md:L17](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L17) —— `tests/kv_offload_cpu_hit.rs` 单测序跑两幕：纯 CPU restore（`gpu_hit==0`）与 GPU+CPU 组合 hit（**G=3+C=3 拼成一段连续前缀**），恢复后首 token logits 与冷算一致。

#### 4.1.4 代码实践

1. **实践目标**：不写代码，手推一个两轮对话的「块账本」，预测 G 和 C 的取值。
2. **操作步骤**（块大小 16 token，哈希链从块 0 起）：
   - 设定：系统提示 32 token；第 1 轮用户输入 40 token，助手回复 8 token；第 2 轮用户输入 16 token。
   - 画出每轮结束时 prompt 的 token 总数与**已封块**（sealed = 恰好装满 16 token 的块；尾块未封不保存——这是 spec §9 SAVE 规则的推论）。
   - 对第 2 轮 admission 写出哈希链区间：GPU 查 [0..F) 的哪段？CPU query 的是哪个子区间？
   - 分三种情形填表：情形 a「GPU 未逐出」、情形 b「GPU 已逐出后 2 块（blocks 3-4）」、情形 c「GPU 全逐出」——各自的 G、C、组合命中长度、需要新 prefill 的块数。
3. **需要观察的现象**：第 1 轮结束 = 80 token = 恰好 5 个满块（0-4），无尾块；第 2 轮 prompt = 96 token = 6 块整，新增的只有 block 5。
4. **预期结果**：情形 a：G=5、C=0（block 5 是新的，CPU 必 miss）、prefill 1 块；情形 b：G=3、C=2、组合命中 5、prefill 1 块；情形 c：G=0、C=5、组合命中 5、prefill 1 块。三种情形**新 prefill 的块数相同**——命中只省计算，不改变正确性。对照本讲 4.1.2 的流程图核对你的推导。

#### 4.1.5 小练习与答案

**练习 1**：块哈希链里为什么必须包含「父块的哈希」？只哈希本块 token 会怎样？

<details><summary>参考答案</summary>

只哈希本块 token，则两个不同位置的相同 16-token 片段会得到同一个键——但它们前面的上下文不同，KV 值也不同，就会发生「键相同、内容不同」的静默错配。把父块哈希掺进来，键就编码了「从块 0 到本块的完整路径」：键相同 ⇔ 前缀 token 完全相同 ⇔ KV 必然一字不差。这正是链式（lineage）哈希的意义。
</details>

**练习 2**：为什么 CPU 命中段必须**紧贴着** GPU 命中段（从块 G 开始），而不能命中 [0..C) 然后接 GPU 的 [C..G+C)？

<details><summary>参考答案</summary>

「锚定 prefix 0 + 单点切开」要求组合命中是一段连续前缀。kvbm 的 `match_and_add_prefix` 只认连续前缀，attention 的因果计算也要求从块 0 起连续的 KV 无洞；若 CPU 命中在前、GPU 命中在后，物理上 GPU 的块本来就在，逻辑上等价于「GPU 命中 = G+C、CPU 命中 = 0」的另一种记法——所以统一规定命中段从 0 连续生长，在哪一层找到就从哪一层续，形态永远是 [0, G+C)。
</details>

**练习 3**：一个刚 admission 的请求，query 返回 `C = 3`，但 GPU 空闲块只够分配 2 个目标块。这时能 load 吗？该怎么做？

<details><summary>参考答案</summary>

不能整批 load——`load` 要求目标块数等于 lease 的块数（i-th 租约块映射 i-th 目标块，见 [src/engine.rs:L395-L397](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L395-L397)）。调用方应显式调 `release_query_lease` 放掉租约（不加载就不许丢着不管，否则钉住的 host 块要挂到 TTL 过期，见 4.3），请求退化为正常 prefill 或等待资源。丢失的只是命中机会，正确性无损。
</details>

### 4.2 三层存储与 scheduler→connector→pegaflow 分层架构

#### 4.2.1 概念说明

把存储想成一栋三层楼，**每层有自己的房东**：

| 层 | 介质 | 容量/速度 | 房东 |
| --- | --- | --- | --- |
| L1 | GPU HBM（`KvBuffer`，page-first 分页布局） | 最小最快 | pegainfer（kvbm `BlockPool` 管逻辑前缀） |
| L2 | host pinned DRAM（pegaflow pinned pool） | 大一个数量级 | pegaflow |
| L3 | SSD / 远端机器内存（RDMA） | 更大更慢 | pegaflow |

「卸载（offload）」= 块从 L1 搬到 L2/L3（save 方向，D2H）；「恢复（restore/load）」= 命中后从 L2/L3 搬回 L1（load 方向，H2D）。这等价于给 KV cache 做了**虚拟内存**：热前缀留 GPU，温前缀留 DRAM，冷/远端前缀按需拉回。

但搬运工有两个，谁是大脑？spec §1 的回答：pegaflow 原本是 vLLM 的 KV connector **服务端**，编排逻辑全在 vLLM 的 Python connector 里；pegainfer 不是 vLLM，那套大脑一行用不上，必须用 Rust 自建——于是分层为「策略（scheduler）→ 机制（connector，本 crate）→ 机制底座（pegaflow）」。

#### 4.2.2 核心流程

spec §1 的三层架构图（本手册的总纲图）：

```text
per-model scheduler   ← 策略：哪些 block 该 resident（full 前缀 / MLA 全前缀 / 未来稀疏选择）
  ↓ 产出 load/save 意图（一组 block）
connector（kvbm logical/physical 思想）← 机制：block identity、状态机、GPU slot 编排、transfer 调度
  ↓ 语义无关的 raw transfer            ↑—— 这一行就是 pegainfer-kv-offload
pegaflow-core         ← 机制底座：D2H/H2D、DRAM/SSD/RDMA 分层
```

一次 save 的物理旅程：executor 在 forward 步同步后调用 `OffloadEngine::save(block_ids, hashes, keep_alive)` → 引擎把块按层翻译成裸拷贝指令 → 提交到 pegaflow runtime → pegaflow 在**自己的私有 stream** 上发 D2H（`cuMemcpyDtoHAsync`）→ 落进 pinned pool，键值登记。load 反向：lease 钉住 host 块 → H2D DMA 进 GPU 目标块 → `LoadHandle` resolve。

注意「进程内」：没有第二个进程、没有 IPC，pegaflow 以 Rust 库形态被嵌进 pegainfer 进程，`OffloadEngine` 自己持有一个内嵌 tokio runtime 来驱动这些异步拷贝（u2-l3 精读）。

#### 4.2.3 源码精读

分层图的权威出处：

> [docs/subsystems/runtime/pegaflow-offload-integration.md:L26-L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L26-L38) —— §1「定位：pegaflow 是 raw 后端，connector 大脑要自建」。第一段讲 pegaflow 的出身（vLLM connector 服务端，编排全在 Python 侧，`pegaflow-core` 只是干 D2H/H2D + 分层存储的肌肉）；第二段「pegainfer 不是 vLLM，那套 Python connector 一行用不上……用 Rust 自建那颗 connector 大脑」；随后就是上面那张三层图。

仓库 README 从外部视角给了一句最简短的分层陈述：

> [README.md:L180](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/README.md#L180) —— Architecture 边界表「KV infrastructure」行：`pegainfer-kv-store` 与既有的 `pegainfer-kv-cache` / `pegainfer-kv-offload` 路径；**PegaFlow supplies host, SSD, and RDMA storage/transfer**（PegaFlow 提供 host、SSD、RDMA 的存储与传输）。

进程内嵌入 + 自持 runtime 的一手描述：

> [docs/subsystems/runtime/pegaflow-offload-integration.md:L13](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L13) —— 「`pegainfer-kv-offload::OffloadEngine`：**拥有 `PegaEngine` + 内嵌 tokio runtime**……所有操作统一为 `OffloadHandle<T>` 可轮询句柄：`submit_save`/`submit_query`/`load` 提交到 pegaflow runtime 后**立即返回 handle**」。

再对照 crate 自己的定位声明，三方叙述完全咬合：

> [src/lib.rs:L3-L6](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L3-L6) —— 「pegainfer 拥有 GPU 分页 KV（`pegainfer-kv-cache::KvBuffer`，page-first 布局）与逻辑前缀缓存（kvbm `BlockPool`）；pegaflow 拥有更深层（host pinned memory、SSD、RDMA）；`OffloadEngine` 是在两者之间搬块并决定何时搬的连接器大脑。」

#### 4.2.4 代码实践

1. **实践目标**：亲手把三层架构图「对账」到三个独立出处，确认它们说的是同一件事。
2. **操作步骤**：
   - 抄下 spec §1 的三层图（上面的链接）；
   - 在三层图的每一层旁标注：对应 crate / 谁做决策 / 懂不懂「前缀」语义；
   - 用 [README.md:L180](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/README.md#L180) 和 [src/lib.rs:L3-L6](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L3-L6) 交叉验证你的标注。
3. **需要观察的现象**：三个出处对「哪一层属于谁」的陈述零冲突；pegaflow 那一层在所有叙述里都不出现「prefix/前缀」这个词。
4. **预期结果**：能不看材料复述：scheduler 出策略（哪些块）、connector 出机制（身份/状态机/编排，本 crate）、pegaflow 出运力（D2H/H2D/SSD/RDMA）；并说出一个推论——**给 pegaflow 换实现不碰策略，改策略不碰 pegaflow**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 D2H 拷贝要由 pegaflow 的「私有 stream」执行，而不是直接复用 pegainfer 的 compute stream？

<details><summary>参考答案</summary>

私有 stream 让卸载 I/O 与推理计算可以在 GPU 上并发（不同 stream 可交错执行），compute stream 不必等拷贝。代价是两条 stream 之间**没有隐式依赖**：compute stream 刚写完的 KV，私有 stream 上的 D2H 不一定看得见——这正是 ORDERING CONTRACT（只能在 forward 步同步之后保存已封块）存在的物理原因，4.3 展开（spec §5 第 4 条风险同此）。
</details>

**练习 2**：KV 卸载像「给 KV cache 做虚拟内存」。这个类比里，页错误（page fault）、换入/换出（swap in/out）分别对应本体系的什么操作？

<details><summary>参考答案</summary>

swap out = save（封块后 best-effort D2H 到 host 层）；swap in = query 命中 + load（H2D 恢复进 GPU 目标块）；page fault ≈ 本地 miss 且深层有货时的 `QueryOutcome::Loading`（后台从 SSD/远端拉取，请求 park 等下一 tick 重查）。差别：虚拟内存的 page fault 会阻塞进程，而这里**任何 miss 都不阻塞 admission**——请求要么先算别的、要么退化为重算（prefill）。
</details>

### 4.3 v1 设计红线：save 尽力而为，load 强排序但不阻塞 admission

#### 4.3.1 概念说明

lib.rs 文档里那句话是整个 v1 的时序总纲：**「Save is best-effort fire-and-forget; load is on the critical path, strongly ordered, but never blocks admission」**。拆开读：

- **save（GPU→CPU）best-effort**：调用方把块号和哈希交给引擎就返回，拷贝在后台跑；失败（pinned pool 满、拷贝出错）只写一条 WARN 日志，不上报、不重试、不影响任何请求。丢了这次 save，下次就少一个命中机会，仅此而已。
- **load（CPU→GPU）强排序**：第 i 个租约块必须落进第 i 个目标块，逐块对位、跨所有层一起拷；它在关键路径上（load 不完成就不能 prefill suffix），所以**顺序不能乱**。
- **但绝不阻塞 admission**：load 是异步提交的，拿到 `LoadHandle` 就走人；请求进 holding 态，调度器每个 tick `poll()` 一次，期间照样服务其他请求。阻塞等待只允许发生在测试和同步线程（`wait`/`save_blocking` 里还有 `debug_assert` 防误用，u2 展开）。

一个必须分清的对偶：**「丢一次 save」不损正确性，「搬错数据」才损正确性**。前者只是少一次命中（退化为重算）；后者——比如把撕裂的半写 KV、或被复用覆写后的 KV 存进旧哈希——会把**错误的数据**永久钉在一个合法键下，后续命中它的请求全都悄悄算错。所以引擎为 save 立了两条**调用方必须维护的契约**（ORDERING / REUSE，u2-l6 精读），却是为了让「尽力而为」不越界成「静默腐蚀」。

#### 4.3.2 核心流程

save 的时间线（🔴 = 关键路径，🟢 = best-effort 后台）：

```text
🟢 forward step 计算 KV ──> 步末同步（block 封块）
🟢 save(block_ids, hashes, keep_alive)   提交即返回（毫秒不占）
🟢 pegaflow runtime: D2H 异步拷贝 ……（可能几十 ms）
🟢 拷贝完成: 键值登记进 host 层；keep_alive 此刻才 drop（块才允许被复用）
   —— 全程不在任何请求的关键路径上；失败只 log::warn
```

load 的时间线：

```text
🔴 admission: match G 块（GPU 命中，持 ImmutableBlock 防逐出）
🔴 query h[G..F] → Ready{C 块, lease}（lease 钉住 host 块防 evict）
🔴 load(lease, dst) → 立即返回 LoadHandle；请求进 holding，调度器继续跑别的
🔴 每 tick handle.poll()：None=还在飞；Some(Ok)=H2D 完成 → 注册 C 块 → 连续前缀 G+C → suffix-only prefill
   —— 强在对位（i→i）与顺序，弱在绝不原地干等
```

query 的结果只有两种（[src/handle.rs:L88-L98](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L88-L98)）：`Ready`（终态，给命中数 + 租约）或 `Loading`（**非终态**：更深层正在后台拉，带同一 `req_id` 下个 tick 重查）。注意 `Loading` 只在配置了 SSD/远端等更深层时出现，纯 host 内存的部署永远不会有。

#### 4.3.3 源码精读

save 的 fire-and-forget 形态与错误去向：

> [src/engine.rs:L284-L294](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L284-L294) —— `save` 的文档写明「任何失败（pinned pool 满、拷贝错误）只记日志、**永不上报**」；函数体就一行：`drop(self.submit_save(...))`——提交后立刻丢掉句柄，连观察完成的兴趣都不保留。
>
> [src/engine.rs:L265-L271](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L265-L271) —— 后台任务里那句注释道破了语义：「接收端已被 drop 就是 fire-and-forget 路径：这个失败只归我们记日志，**没有别人会看到它**」→ `log::warn!("pegaflow save failed (best-effort)")`。

两条时序契约的原文（本讲只立概念，u2-l6 逐行精读）：

> [src/engine.rs:L231-L245](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L231-L245) —— **ORDERING CONTRACT**：pegaflow 的 D2H 跑在它自己的 stream 上、与 compute stream 无依赖，所以只能保存「KV 写入已完成」的块（forward 步同步之后的封块时刻）；保存写入仍在飞的块会读到**撕裂数据**。**REUSE CONTRACT**：拷贝在返回之后异步读 GPU 块，块必须保持稳定——`keep_alive`（源块的 allocator guard）随任务持有、**D2H 落地才 drop**，否则块被复用覆写后，在途 D2H 会把错 KV 快照进旧哈希。
>
> [src/engine.rs:L272-L274](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L272-L274) —— 「**直到此刻**（D2H 落地）才释放源块 pin；在此之前块不得被复用」——`drop(keep_alive)` 被精确放在 `batch_save_kv_blocks_from_ipc` 之后。

load 的强排序与租约语义：

> [src/engine.rs:L392-L397](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L392-L397) —— 「把租约的 CPU 块拷进 `dst_block_ids` 指名的 GPU 块，**跨所有已注册层**；目标块数必须等于租约块数，pegaflow 把 **i-th 租约块映射到 `dst_block_ids[i]`**」。
>
> [src/engine.rs:L417-L424](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L417-L424) —— `release_query_lease`：query 会把命中块钉在租约后面直到 `load` 消费它；决定不加载就必须显式释放——`QueryLeaseId` 是个无 Drop 的裸 token，丢着不管的话被钉住的 host 块要**挂到 TTL 过期**才能被驱逐。

「不阻塞」的机制载体是可轮询句柄：

> [src/handle.rs:L1-L7](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L1-L7) —— 模块文档：调度器关心的每个卸载操作都经同一个 `OffloadHandle` 收账——「提交给引擎，然后在**步边界轮询——绝不阻塞服务循环**」；「drop 句柄只是分离观察者，不是取消 I/O：底层任务继续跑（并继续持有 keep-alive 载荷）直到它自己 settle」。
>
> [src/handle.rs:L38-L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L38-L47) —— `poll`：非阻塞查一次，「调度 tick 用；在途返回 `None`」；worker 掉线（`Closed`）映射成错误而不是永远 None。

#### 4.3.4 代码实践

1. **实践目标**：只用眼睛追一次「失败」，验证 save 与 load 的错误去向确实不同。
2. **操作步骤**：
   - 在 [src/engine.rs:L284-L294](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L284-L294) 找到 `save` 的返回类型——它是 `()` 还是 `Result`？失败的警告日志在哪一行发出？
   - 对比 [src/engine.rs:L398-L402](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L398-L402) 的 `load` 签名——它返回什么？错误走哪条通道？
   - 读 [src/handle.rs:L38-L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L38-L47)，写出 `poll()` 的三种返回各代表什么。
3. **需要观察的现象**：`save` 无返回值、失败仅 `log::warn!`；`load` 返回 `Result<LoadHandle, EngineError>`，且后续完成状态还能从句柄再拿到 `Result`——**load 的错误有两层出口，save 的错误零出口**。
4. **预期结果**：能口头回答「为什么允许 save 零出口」——因为它是 best-effort，任何消费方都不该基于它的成败做决策；而 load 在关键路径上，调用方必须知道成败以决定是否退化重算。（运行层面的验证在 u1-l3：`cargo test -p pegainfer-kv-offload --lib` 可跑通 handle 相关单测，无需 GPU。）

#### 4.3.5 小练习与答案

**练习 1**：`save_blocking` 为什么存在？既然 save 是 fire-and-forget，谁需要一个阻塞版？

<details><summary>参考答案</summary>

见 [src/engine.rs:L296-L298](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L296-L298) 的文档：「Submit-then-wait 形态，给**同步调用方**：返回那一刻 GPU 块就可以被复用了，且错误会浮出」。两类用户：测试（`cpu_roundtrip.rs` 要确认字节真的落进 host 层才能继续 query）和需要确定性边界的调用方。注意它内部有 `assert_outside_runtime` 防止在 pegaflow runtime 线程里调用造成自锁。
</details>

**练习 2**：「丢一次 save 不损正确性」与「REUSE CONTRACT 违反会静默腐蚀」——两者都涉及「save 没成功」，为什么定性天差地别？

<details><summary>参考答案</summary>

丢 save = 键下**没有**数据 → 后续 query miss → 重算，正确。违反 REUSE/ORDERING = 键下**有**数据但**是错的**（撕裂写或复用覆写后的 KV 被快照进旧哈希）→ 后续 query 命中并加载错 KV → 所有命中该前缀的请求都悄悄算错，且无法自愈。前者的代价是延迟（可观测、可接受），后者的代价是正确性（静默、致命）。这就是为什么「尽力而为」只许丢、不许错——两条契约正是守这条底线的。
</details>

**练习 3**：为什么 `QueryOutcome::Loading` 被设计成**非终态**，要求带同一 `req_id` 重查，而不是给调用方一个「拉取完成」回调？

<details><summary>参考答案</summary>

整个体系的服务循环是「逐 tick 非阻塞」风格（EP free-running 纪律：不等待、不睡眠轮询）。非终态 + 重查让请求在 scheduler 的 `loading` 队列里每 tick 顺手 poll 一次，与现有调度节奏严丝合缝；回调/等待则要求调度器挂起或登记回调上下文，破坏「每步循环全速跑」的不变量。同一设计也出现在 `LoadHandle` 的 `poll` 上——一个风格贯彻到底。
</details>

### 4.4 P/D 分离：跨实例数据面与免 handle 协议

#### 4.4.1 概念说明

**P/D 分离（prefill/decode disaggregation）**：把两种负载拆到不同实例——prefill 节点（P）专门算 prompt，decode 节点（D）专门逐 token 生成，中间必须把 P 算出的 KV **交接**给 D。动机是资源解耦（P 要算力、D 要带宽，配比不同）。

传统交接要一套协议：P 把 KV 放哪、什么句柄、D 拿句柄来取、何时可回收。本体系**一行协议都不写**——靠 4.1 的内容寻址：

> D 拿到 prompt，自己算出同一组哈希链，直接拿键去查「谁有这些块」。P/D 对同一 commit 推出同一 key，**免 handle**。

配套两块基础设施：

- **MetaServer**：一张 `(namespace, hash) → owner` 的目录。D 本地 miss 时问它「谁有最长前缀」。
- **namespace**：内容寻址的**作用域**，编入模型几何（层数/头数/维度等）。不同模型的键即使偶然相同也互相不可见——防止几何相同的两个模型（Qwen3-4B 与 8B 几何同为 36L/8H/128d）静默交叉命中错 KV。

#### 4.4.2 核心流程

M2 实测拓扑（1P+1D + router + metaserver）的一次完整交接：

```text
🟢 控制面   router 收请求 → 改 max_tokens=1 发 P
           P: prefill + 1 步 decode；Finished 事件被扣住（withhold）
           P: flush 屏障 = 等 save 落地 + MetaServer 注册送达 → 才释放 Finished
           P 的 HTTP 响应本身就是「KV-ready」信号 → router 原样转发原请求给 D
🟢 数据面   D: begin_kv_prefetch 本地 miss → 问 metaserver 谁有最长前缀
           → gRPC 握手 + QueryBlocksForTransfer（owner 侧 transfer lock 防 evict）
           → 单边 RDMA READ 直接拉进 D 的本地 pinned pool（无 P 参与 CPU 拷贝）
           D 侧请求: QueryOutcome::Loading（park 在 loading 队列逐 tick 重查）
           → Ready → 走既有 H2D prefetch → suffix-only prefill
```

故障退化语义（这是「免协议」敢用的底气）：杀 metaserver → D 查询失败打 WARN，退化为本地全量 prefill；杀 P → D 冷请求照常完成。**没有任何路径 hang 或 crash**——因为 D 从不「指望」远端数据，miss 到底就是自己算。

#### 4.4.3 源码精读

P/D 三要素（控制面/数据面/无 handle）的权威出处：

> [docs/models/qwen3/pd-disaggregation-m2.md:L20](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L20) —— 控制面：P 的 `Finished` 事件被扣住，「等 save+metaserver 注册对 peer 可见后由 offload runtime 异步释放（`flush_on_finish`，scheduler 线程不等屏障）」，P 的 HTTP 响应即 KV-ready 信号。
>
> [docs/models/qwen3/pd-disaggregation-m2.md:L21](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L21) —— 数据面：本地 miss → metaserver → gRPC 握手（owner 侧 transfer lock 防 evict）→ 单边 RDMA READ 拉进本地 pinned pool → `QueryOutcome::Loading` 期间 park 逐 tick 重查询 → `Ready` 后走既有 H2D → suffix-only prefill。
>
> [docs/models/qwen3/pd-disaggregation-m2.md:L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L22) —— 无 handle：「kvbm `SequenceHash` 由 token 序列确定性推导，P/D 同 commit 必然同 key」；namespace 含 `hidden_size/...` + KV 几何——并记录了 4B/8B 几何相同导致纯几何 namespace 会静默交叉命中的 toxic review 抓虫。

flush 屏障在本 crate 源码里的身份就是「KV-ready 信号」：

> [src/engine.rs:L443-L447](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L443-L447) —— `flush_saves_then` 文档：「屏障 save 管线然后调 `then`，不阻塞调用方。`then` 一运行，随后的 query（本地的或 P2P peer 的）就能观察到此前保存的每个块——**这就是 P/D 的 KV-ready 信号，prefill 节点借此扣住请求的 `Finished` 事件直到 KV 对 peer 可见**」。（屏障内部结构 u2-l8 精读。）

实测数字（验收表）：

> [docs/models/qwen3/pd-disaggregation-m2.md:L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L38) —— P2P 实证：D 侧 `RDMA fetch summary: blocks=33/33 bytes_mib=74.2`，rdma_wait 仅 2.6ms（33 块 × 2.25 MiB/块 ≈ 74.2 MiB，与 4.1 的块体积公式精确吻合）。
>
> [docs/models/qwen3/pd-disaggregation-m2.md:L39-L41](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L39-L41) —— 故障退化：杀 metaserver / 杀 P 均优雅退化为本地 prefill，无 crash 无 hang；多轮压测 turn2+ TTFT 恒定 105-111ms——D 每轮只拉新增 suffix（64 块 ~15ms），P 前缀缓存逐轮全量命中。

#### 4.4.4 代码实践

1. **实践目标**：把 P/D 交接的每一步映射到「本 crate 的哪个机制」，确认 4.1–4.3 的概念在跨实例场景下原封不动复用。
2. **操作步骤**：
   - 按 [docs/models/qwen3/pd-disaggregation-m2.md:L20-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L20-L22) 抄下控制面与数据面的时序；
   - 给每一步标注它用到的本讲机制：内容寻址键（4.1）/ 分层搬运（4.2）/ 可轮询句柄与 Loading 态（4.3）/ flush 屏障（本节）；
   - 在时序上标两个故障注入点（杀 metaserver、杀 P），写出各自之后每一步的退化走向。
3. **需要观察的现象**：数据面的 D 侧流程与 4.3.2 的 load 时间线**逐帧同构**——只多了「Loading 期间的后台 RDMA 拉取」这一层；P 侧的 withhold+flush 是唯一新增控制逻辑。
4. **预期结果**：能回答「P/D 为什么不需要新协议」：D 端的 query/load、H2D 恢复、tick 轮询全部复用单实例已有的机制；唯一新的是 MetaServer 目录与 RDMA 传输（pegaflow 提供），以及 P 侧把「KV 可见」变成可等待的信号（flush 屏障）。若本地无法起双实例验证，标注「待本地验证」并仅完成纸面推演。

#### 4.4.5 小练习与答案

**练习 1**：为什么 namespace 必须把「KV 几何」编进去？给出一个不编几何会出事的具体场景。

<details><summary>参考答案</summary>

场景就在 M2 文档里：Qwen3-4B 与 Qwen3-8B 的 KV 几何完全相同（36L/8H/128d）、tokenizer 相同——若 namespace 只含模型名以外可共享的抽象标识或干脆为空，两个模型的同 token 序列会推出**相同的键**，8B 的 D 节点可能命中 4B 的 P 存的 KV，静默算错。namespace 编入 hidden_size 等 token 几何 + KV 几何后，跨模型键必然不同，错配在寻址层面就不可能发生（toxic review 抓出的正是这条）。
</details>

**练习 2**：P 侧为什么要「扣住 Finished 事件」，而不是算完立刻回复 router？

<details><summary>参考答案</summary>

如果 P 算完立刻回复，router 马上转发 D，而 P 的 KV 可能还在 D2H/注册途中——D 一查 miss 就自己重算了，P/D 的全部意义（省掉 D 的 prefill）落空。扣住 Finished 直到 flush 屏障确认「save 落地 + MetaServer 注册送达」，才把「KV-ready」作为回复发出，D 的查询必然命中。屏障是异步的（scheduler 线程不等），所以 P 自己的吞吐不受损——这又是 4.3「不阻塞」红线的延伸。
</details>

**练习 3**：D 侧冷 prompt 会先经历一次「本地全 miss → Loading → 空手而归」的往返（M2 已知边界）。为什么说这是「设计使然」而不是 bug？

<details><summary>参考答案</summary>

D 的查询是**乐观**的：先假设深层可能有货（本机、SSD、远端 P 都问一遍），Loading 表示后台在拉。没有任何货时这次往返确实白付几十 ms，但换来的是「任何 miss 都不阻塞、不重算优先」的统一路径——不为冷请求特判一条快速通道，代码路径单一、行为可预测。M2 的实测也表明多轮场景下这笔一次性的代价被 turn2+ 恒定 ~107ms 的 TTFT 远远赚回来。
</details>

## 5. 综合实践

现在完成本讲的完整实践任务：**以一次 5 轮多轮对话为例，画出 KV 块的生命周期时序图，区分关键路径与 best-effort，并论证「丢一次 save 为什么不损正确性」**。纸笔或任意画图工具皆可，不运行代码。

### 任务设定

- 块大小 16 token；哈希链 h0→h1→…（同 4.1.4 的推演规则：只保存已封块）。
- 第 1 轮：系统提示 32 tok + 用户 40 tok + 回复 8 tok = 80 tok = 恰好 5 个满块（h0–h4）。
- 第 2~4 轮：每轮「用户 16 tok + 回复 16 tok」= 恰好 2 个满块。第 N 轮结束后封块数为 5+2(N−1)。
- 第 5 轮只有用户输入 16 tok（要画的就是这一次 admission）。

### 参考时序图

用 🔴（关键路径）/ 🟢（best-effort）两种标记代替两种颜色：

```text
── 第 1 轮 ────────────────────────────────────────────────────────
🔴 admission: G=0, C=0（全新）→ 全量 prefill 80 tok（5 块）
🔴 decode 生成 8 tok；步末同步，blocks 0–4 全部封块
🟢 save(h0..h4) fire-and-forget ── D2H → host 层登记（不在关键路径）
🟢 请求 Finished，GPU BlockPool 仍持有这 5 块（可被逐出）

── 第 2 轮 ── 三种可能的分支（按当时的驻留状态取其一）─────────────
分支 a  GPU 未逐出：🔴 G=5 → 🟢query h[5..6]→C=0 → 🔴 prefill 新块 5
分支 b  GPU blocks 3–4 被逐出：🔴 G=3 → 🔴query h[3..6]→Ready C=2+lease
        → 🔴load 2 块 H2D（holding，每 tick poll）→ 注册 → G+C=5 连续前缀
        → 🔴 suffix-only prefill 块 5
分支 c  P/D 部署，请求落在 D：🔴 本地 G=0 → 🔴query → Loading（远端 P 有货）
        → 🟢 RDMA READ 后台拉进 pinned pool（metaserver 发现 owner）
        → 🔴 Ready → H2D → suffix-only prefill

── 第 3、4 轮 ── 同第 2 轮模式；每轮末 🟢 save 新封的 2 块 ──────────
        （两轮间隙若 GPU 繁忙，块被逐出越多，分支越从 a 滑向 b/c）

── 第 5 轮（画的主角）──────────────────────────────────────────────
🔴 admission: prompt = 5+2×3=11 块（h0..h10）+ 新输入第 12 块
🔴 GPU match → G；🟢/🔴 query h[G..11] → C；组合命中 [0, G+C)
🔴 load（若有 C）→ poll → 注册 → 只 prefill 剩余 suffix
🟢 回复生成中每封一块 save 一块；Finished 后块留在池中等下一轮
```

自查三个要点：① 组合命中永远画成**一段连续**区间，GPU 段 [0,G) 在左、CPU 段 [G,G+C) 紧贴——没有斑马纹；② 所有 save、所有后台 RDMA 拉取、所有 keep_alive 的持有期都标 🟢；③ admission 决策、prefill、poll 收账后的注册与 rematch 标 🔴。

### 文字论证：丢一次 save 为什么不损正确性

> 正确性的根基是「KV 永远可以由 prefill 从 token 重算出来」，哈希键只决定**能否复用**、从不参与计算本身。丢一次 save 只是让某个键在 host 层**缺席**：下一个请求 query 该键时 miss，调度器退化为正常 prefill——多花一次计算与延迟，输出与缓存命中的情况逐位一致（两幕测试正是用 logits 一致性钉死这一点，见 4.1.3）。反之，若「有数据但数据是错的」（违反 ORDERING/REUSE 契约的撕裂/复用覆写），命中它的一切请求都会静默算错且无法自愈。所以设计把「丢」设计成零成本（fire-and-forget、错误只写日志），把「错」设计成不可能（两条时序契约 + keep_alive 钉到 D2H 落地）——**best-effort 的对象是 availability of the cache，从来不是 correctness of the output**。

## 6. 本讲小结

- **内容寻址是全局钥匙**：块哈希链（父键 ‖ 本块 token）让「同一前缀 ⇒ 同一键」无需任何协调成立，跨请求、跨实例（P/D）通用；namespace 是键的作用域，把模型几何编进去防静默交叉命中。
- **组合命中形态**：GPU 命中（kvbm 原生）+ CPU 命中（本 crate）叠加为一段连续前缀 [0, G+C)，双向锚定 prefix 0、单点切开；GPU↔CPU 交错被刻意排除，收尾用 register→rematch 复用现成匹配逻辑、kvbm 零改动。
- **分层架构**：scheduler 出策略、connector（本 crate）出机制、pegaflow 出语义无关的运力（D2H/H2D/DRAM/SSD/RDMA）；进程内嵌入，`OffloadEngine` 自持 tokio runtime。
- **v1 时序红线**：save 尽力而为 fire-and-forget（失败仅记日志）；load 强排序（i-th 租约块 → i-th 目标块）但在关键路径上绝不阻塞 admission；一切经 `OffloadHandle` 逐 tick 非阻塞 poll 收账。
- **丢 save ≠ 错数据**：丢 save 只损失命中机会（退化为重算）；撕裂/复用覆写的数据被存进合法键才是致命的——ORDERING 与 REUSE 两条契约守住的就是这条底线。
- **P/D 免协议交接**：D 从同一 prompt 推出同一组键，经 MetaServer 发现 owner、单边 RDMA READ 拉进本地 pinned pool，复用单实例的 query/load 机制；P 侧用 flush 屏障把「KV-ready」做成扣住 Finished 的信号；任何故障都退化为本地 prefill，无 hang 无 crash。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：`编译、测试与代码走读准备`——把本讲纸面上的模型落到可运行的验证：哪些单测纯 CPU 可跑（`--lib`），`tests/cpu_roundtrip.rs` 为什么必须真 GPU，以及 `--release` 的必要性。
- **进入 u2 后的第一站（u2-l1）**：`配置层`——`OffloadConfig` 的 namespace、pinned_pool_bytes 等字段是本讲概念的参数化落点。
- **源码预读**：通读 [src/engine.rs 的文档注释区](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L226-L449)（save/query/load/flush 四个方法的 `///` 块，约 100 行）——本讲引用的所有契约原文都在那里，读完它们 u2 的精读会非常顺。
- **文档预读**：[docs/subsystems/runtime/pegaflow-offload-integration.md 的 §9](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L127-L148)（live 接线设计）与 [docs/models/qwen3/pd-disaggregation-m2.md 的 §1-§3](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/pd-disaggregation-m2.md#L7-L41)——本讲 4.1.2/4.4.2 两张时序图的完整版出处。
