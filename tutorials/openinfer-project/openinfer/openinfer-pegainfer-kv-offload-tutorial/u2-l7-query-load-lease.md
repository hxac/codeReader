# 讲义 u2-l7：query/load 链路：前缀命中、lease 生命周期与 H2D 拷贝

## 1. 本讲目标

上一讲（u2-l6）剖析了数据的「写侧」——save 链路如何把 GPU 块尽力而为地搬进 host 层。本讲进入「读侧」：当一个新请求到来、GPU 命中只覆盖了前缀的前一段时，如何向 pegaflow 查询「CPU 层还有多少连续命中」、如何把命中的块钉住（lease）、如何按序拷回 GPU（load）、以及决定不加载时如何释放（release_query_lease）。

学完本讲你应该能够：

1. 说明 `QueryOutcome::Loading` 与 `Ready` 的语义差别，以及为什么 `Loading` 只在配置了更深层数据面（SSD / 远端 P2P peer）时出现、host-memory-only 部署下查询一定是终态。
2. 解释 query lease 的作用：命中块被租约钉住、不可被驱逐；以及不释放（也不 load）的后果——被钉住的 host 块要挂到租约 TTL 过期。
3. 描述 load 的顺序契约：第 i 个租约块映射到第 i 个目标块，跨所有注册层同时成立。
4. 画出 query→lease→load→release 的完整状态机，并说清哪条路径必须显式调用 `release_query_lease`、哪条路径 lease 会被 `load` 自动消费。

## 2. 前置知识

- **前缀命中与内容寻址**（u1-l2）：KV 按 16-token 块组织，块键是「父块哈希 + 本块 token」的链式哈希。查询时调用方交给本 crate 的是一组 `Vec<u8>` 哈希前缀（按序），本层不关心哈希怎么算——它只回答「这个前缀从头开始有多少块在 CPU 层」。
- **可轮询句柄**（u2-l5）：`OffloadHandle<T>` 包着 oneshot 接收端。`poll()` 非阻塞、可反复调用，`None` 表示在途；`wait()` 阻塞结账。本讲的 `QueryHandle`、`LoadHandle` 都是它的类型别名。
- **lease（租约）**：一个由 pegaflow 签发的不透明 token（`QueryLeaseId`）。它代表「host 层里这批命中块已被我预订，不许驱逐」。token 本身**没有 Drop 逻辑**（是个 inert token）——所以 Rust 的所有权系统帮不了你，忘记释放就只能等 TTL。
- **in-proc API**：pegaflow 暴露的进程内直连调用（不经网络 RPC），`batch_load_kv_blocks_multi_layer_inproc` 的名字即由此而来。它同步提交拷贝并直接返回 oneshot 接收端。
- **H2D / DMA**：Host-to-Device 拷贝，经 DMA 引擎（默认 `TransferMode::Direct`，见 u2-l4）把 host pinned 池里的字节搬进 GPU 显存。
- **TTL**：Time To Live，租约的存活期限。过期后 pegaflow 自动作废租约、解钉块（u2-l3 讲过的 GC 每分钟双清扫就是它的兜底同伴）。

## 3. 本讲源码地图

| 文件 | 本讲涉及的部分 |
|------|----------------|
| [src/engine.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L308-L427) | `submit_query` / `query` / `load` / `release_query_lease` 四个方法，本讲主战场 |
| [src/handle.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L21-L98) | `QueryHandle` / `LoadHandle` 类型别名与 `QueryHit` / `QueryOutcome` 两个结果类型 |
| [tests/cpu_roundtrip.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L110-L134) | 把本讲链路在真实 GPU 上串起来的集成测试（save→flush→query→load 主干） |
| pegainfer-qwen3/src/executor.rs | 生产调用方：query 的两个调用点、release 的三处调用、`start_prefetch_load`（仅作旁证，详细解剖留给 u3-l3） |
| pegainfer-qwen3/src/executor/remote_fetch.rs | `QueryView` / `RemoteFetchAction`：把每次查询 tick 的决策抽成无 GPU 可测的纯函数（同样留给 u3-l3 精读，本讲只借用其分支语义） |

> 注：`count_prefix_hit_blocks_with_prefetch`、`create_query_lease`、`batch_load_kv_blocks_multi_layer_inproc`、`release_query_lease` 是 pegaflow-core 的外部 API（git 依赖，不在本仓库内）。本讲对它们的行为描述全部来自 engine.rs 的文档注释与调用方式，不臆测其内部实现。

## 4. 核心概念与源码讲解

### 4.1 submit_query 与 PrefetchStatus 两分支

#### 4.1.1 概念说明

`submit_query` 回答一个问题：「给定按序的块哈希前缀，CPU 层从头开始能连续命中多少块？」它有两个关键设计：

1. **查询会触发预取（prefetch）**。如果命中的尾巴缺了几块、而 pegaflow 知道去哪里补（远端 peer 的 P2P 副本或 SSD），查询会顺手启动一次后台拉取——这就是方法名 `count_prefix_hit_blocks_with_prefetch` 里 with_prefetch 的含义。
2. **结果分两个分支**，对应 pegaflow 返回的 `PrefetchStatus`：
   - `Ready`：终态。给你命中块数，并附一份钉住这些块的租约。
   - `Loading`：非终态。后台拉取进行中，本 tick 还不能给你完整答案；**带着同一个 `req_id` 下个 tick 再查一次**即可轮询进度。

`req_id` 的作用就在这里：它把一次在途预取和它的轮询者绑定起来。文档注释要求它非空且足够唯一（请求 id 天然合适）。

为什么 `Loading` 只在配置了更深层数据面时出现？因为 host-memory-only 部署下，CPU 层就是存储的尽头——没查到的块无处可拉，不存在「正在补货」这个中间态，查询一步到位返回 `Ready`（哪怕是 0 命中的 `Ready`）。

#### 4.1.2 核心流程

```text
submit_query(req_id, block_hashes, wait_for_full_prefix)
│
├── block_hashes 为空？
│     └── 是 → 直接返回已结算句柄：Ready { lease: None, num_blocks: 0 }
│              （不 spawn 任务、不碰 pegaflow）
│
└── spawn 到 host.runtime：
      │
      ├── count_prefix_hit_blocks_with_prefetch(instance, req_id,
      │                                            hashes, wait_for_full_prefix)
      │        │
      │        ├── PrefetchStatus::Loading
      │        │        └── → QueryOutcome::Loading        （非终态，同 req_id 重查）
      │        │
      │        └── PrefetchStatus::Ready { blocks, .. }
      │                 ├── blocks 为空 → Ready { lease: None, num_blocks: 0 }   （全 miss，无租约）
      │                 └── 否则 → create_query_lease(instance, blocks)
      │                            → Ready { lease: Some(lease), num_blocks }    （命中 + 租约）
      │
      └── 结果经 oneshot 发回 QueryHandle
```

对外可见性：`submit_query` 是私有方法，公开的包装是 `query`（submit-then-wait 同步形式，供测试与同步调用方使用，入口有 `assert_outside_runtime` 防御）。`QueryHandle` 本身是 `pub(crate)`——在 crate 内部由调度器逐 tick poll，不外流。

#### 4.1.3 源码精读

**空输入快速路径**——没有可查的就等于全 miss，返回 `settled` 句柄（u2-l5 讲过 `settled` 兼作 engine 的空输入快速路径），连任务都不 spawn：

- [src/engine.rs:L334-L339](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L334-L339) —— `submit_query` 收到空 `block_hashes` 时直接构造 `QueryOutcome::Ready(QueryHit { lease: None, num_blocks: 0 })`。

**两分支的判定核心**：

- [src/engine.rs:L347-L354](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L347-L354) —— 调 pegaflow 的 `count_prefix_hit_blocks_with_prefetch`，四个入参：实例 id、`req_id`（作用域一次在途预取）、哈希前缀、`wait_for_full_prefix` 开关。
- [src/engine.rs:L355-L371](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L355-L371) —— `match status` 的三个出口：
  - `Loading` → `Ok(QueryOutcome::Loading)`；
  - `Ready` 但 `blocks` 为空 → `Ready { lease: None, num_blocks: 0 }`（全 miss 也走终态，**不产生租约**）；
  - `Ready` 且非空 → `create_query_lease` 把命中块钉住，返回带租约的 `Ready`。注意 `create_query_lease` 带 `?`：签发失败会把这个错误作为查询错误发回句柄。

**结果回传**：

- [src/engine.rs:L374](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L374) —— `let _ = tx.send(result);`。对比 save 链路（u2-l6）：save 在接收端已丢弃时会打日志兜底；query **没有**这个兜底——因为 query 丢弃句柄的代价不是「丢个错误」而是「滞留一份租约」，文档注释直接把它写成纪律要求。

**文档注释里的两条纪律**（比代码本身更重要）：

- [src/engine.rs:L315-L317](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L315-L317) —— `req_id` 必须非空且足够唯一以圈定在途预取。
- [src/engine.rs:L325-L327](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L325-L327) —— 句柄在结算前被 drop，可能让一个 `Ready` 结果的租约滞留到 TTL 过期；**每个提交的查询都要 poll 到结算**。

**结果类型**（handle.rs 侧）：

- [src/handle.rs:L28-L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L28-L31) —— `QueryHandle = OffloadHandle<QueryOutcome>`，crate 私有。
- [src/handle.rs:L89-L98](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L89-L98) —— `QueryOutcome` 两变体的文档：`Ready` 终态（`num_blocks` 个前缀块已在 host 层且被租约持有）；`Loading` 非终态（远端 peer / SSD 拉取中，同 `req_id` 重查；**只在配了更深层时出现，host-memory-only 部署永不返回它**）。

**集成测试的印证**：

- [tests/cpu_roundtrip.rs:L114-L119](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L114-L119) —— host-memory-only 环境下查询必须一步到终态：`matches!(...)` 断言结果不是 `Loading` 就地 panic。

#### 4.1.4 代码实践

**实践目标**：不靠运行，纯靠读代码枚举 `submit_query` 的全部出口，并理解「为什么空输入不 spawn」。

**操作步骤**：

1. 打开 [src/engine.rs:L328-L377](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L328-L377)，数一数 `QueryHandle` 的结果一共有几种可能（提示：空输入短路、Loading、0 命中 Ready、带租约 Ready、以及 `create_query_lease` 的 `?` 带来的 Err——共 5 种）。
2. 运行 `cargo test -p pegainfer-kv-offload --lib`（纯 CPU 可跑），观察输出里测试的归属模块。

**需要观察的现象**：`--lib` 测试全部来自 `layout` 与 `vllm_hash` 模块，没有任何测试覆盖 `submit_query`。

**预期结果**：`submit_query` 的行为验证完全依赖 `tests/cpu_roundtrip.rs`（需要真实 GPU）与下游 qwen3 的集成测试。这是一个刻意的取舍：查询逻辑是「向外部引擎发 RPC + 两分支翻译」，纯单测能 Mock 的部分很少。无 GPU 环境下本步骤的运行结果为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么空 `block_hashes` 要短路成一个「已结算的全 miss」，而不是照常 spawn 任务让 pegaflow 返回 0？

**答案**：空前缀的答案恒为 0 命中，没有任何信息需要向 pegaflow 询问；短路省掉一次任务 spawn、一次跨 runtime 往返和一次无意义的 RPC。同时它让调用方（如 remote_fetch 决策层）拿到的类型统一是 `QueryOutcome`——「没有可查的」和「查了全 miss」在下游眼里是同一种 miss，不需要特判。

**练习 2**：`Loading` 结果出现时，调用方下一个 tick 应该怎么做？如果换了 `req_id` 会怎样？

**答案**：带**同一个** `req_id` 重新提交查询（qwen3 的 `poll_remote_fetch` 每个 tick 重查，见 [executor.rs:L2043-L2053](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2043-L2053)）。`req_id` 是在途预取的作用域键：换了 id，pegaflow 找不到「那次拉取」的上下文——轻则重新发起一次拉取浪费带宽，重则旧预取变成无人认领的孤儿（u2-l3 讲过 GC 要等 5 分钟才清扫这类条目）。

**练习 3**：全 miss（`Ready { lease: None, num_blocks: 0 }`）为什么不需要、也不可能释放租约？

**答案**：租约只在「有命中块需要钉住」时才签发——代码里 `blocks.is_empty()` 的分支显式返回 `lease: None`（[src/engine.rs:L357-L363](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L357-L363)）。没有租约就没有被钉住的块，自然没有释放义务；`Option<QueryLeaseId>` 的 `None` 在类型上就表达了「无账可还」。

### 4.2 create_query_lease 与 load：in-proc API 与顺序契约

#### 4.2.1 概念说明

查询（`Ready` 非空）与加载之间隔着一个**租约**。为什么必须有它？

考虑这个竞争：查询说「hash 前 3 块在 host 层」，等调用方腾出 GPU 目标块、提交 load 时，中间可能隔了一个调度 tick（几毫秒到几十毫秒）。这期间 host 层的容量压力可能想驱逐这 3 块——如果真被驱逐、又被新数据覆写，load 拷回来的就是**错误字节**，而且会以正确的 hash 身份进入 GPU 前缀缓存，静默腐蚀推理结果。这正是 u1-l2 立下的红线：丢命中只损性能，错数据进键才致命。

租约封死这个窗口：`create_query_lease` 把命中块钉住（不可驱逐），直到 `load` 消费掉租约开始拷贝。租约本身是不透明 token `QueryLeaseId`，从 pegaflow 再导出（u1-l1）。

`load` 是三个操作里唯一的**同步提交**：不像 `submit_save` / `submit_query` 先 spawn 到 host runtime 再返回句柄，`load` 直接调用 in-proc API `batch_load_kv_blocks_multi_layer_inproc`，把 oneshot 接收端原样包进 `LoadHandle` 返回。因此它的**提交期错误同步上抛**（`Result<LoadHandle, EngineError>`），而 save/query 的提交永远是成功的（错误延迟到句柄结算）。这个差别直接决定了 4.3 的释放纪律。

顺序契约：`dst_block_ids[i]` 接收第 i 个租约块的拷贝，对**每一个注册层**同时成立。写成公式：

\[ \forall i \in [0, n),\ \forall L \in \text{layers}:\quad \text{GPU}_L[\,\text{dst}[i]\,] \leftarrow \text{host}_L[\,\text{hash}_i\,] \]

其中 \( n \) 是租约块数，要求 \( |\text{dst}| = n \)。哈希按链式前缀有序，所以「第 i 个租约块」就是查询时传入的第 i 个哈希——目标块列表的顺序就是前缀逻辑顺序，这就是 cpu_roundtrip 注释里「load 必须把第 i 个租约块恢复到第 i 个目标」的含义（[tests/cpu_roundtrip.rs:L34-L35](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L34-L35)）。

#### 4.2.2 核心流程

```text
Ready { lease: Some(lease), num_blocks: n }        （来自 submit_query，host 块已被钉住）
        │
        ▼
load(lease, dst_block_ids)            ← 同步调用，不 spawn
        │  · layer_names 借出 &[&str]
        │  · i32 块号 → usize（与 build_saves 同一条边界约定）
        │  · 包一层：loads = [(lease, dst)]
        ▼
batch_load_kv_blocks_multi_layer_inproc(instance, TP_RANK, device_id, layers, loads)
        │
        ├── Err(e)  → 同步返回 Err：提交失败，租约可能仍未被消费 → 调用方须 release（见 4.3）
        │
        └── Ok(rx)  → LoadHandle::from_rx(rx)     （DMA 进行中）
                          │
                          ▼
                    poll 到 Some(Ok(()))  → H2D 完成，目标块内容就绪
                                            （调用方还需在自己的计算流上同步后再用）
```

对比记忆：save 用「fire-and-forget + 日志兜底」，query 用「逐 tick 轮询到结算」，load 用「同步提交 + 异步等 DMA」——三种形态对应三种调用方节奏。

#### 4.2.3 源码精读

- [src/engine.rs:L364-L365](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L364-L365) —— `create_query_lease(&instance_id, blocks)`：Ready 非空分支内联签发租约；`?` 把签发失败折算成查询错误。租约从诞生起就钉住 `blocks`。
- [src/engine.rs:L392-L397](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L392-L397) —— `load` 的文档注释：`dst_block_ids.len()` 必须等于租约块数；pegaflow 把第 i 个租约块映射到 `dst_block_ids[i]`，逐层成立。
- [src/engine.rs:L398-L414](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L398-L414) —— `load` 全文。三个细节：
  - L403：`layer_names`（引擎构造时注册的层名，u2-l4）借出 `&[&str]`——注册时「烤进」的名字在这里兑现为拷贝目的地的枚举；
  - L404-L405：`i32 → usize` 转换，与 `build_saves`（u2-l6）同一条「pegainfer 用 i32、pegaflow 用 usize，只在边界转一次」的约定；
  - L406-L413：`loads = [(lease, dst)]` 单元素数组直接传给 in-proc API，拿到 `rx` 包成 `LoadHandle`。注意没有 `assert_outside_runtime`——这个方法不阻塞，任何线程都能调。
- [src/handle.rs:L21-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L21-L22) —— `LoadHandle = OffloadHandle<()>`，**公开**类型：qwen3 的调度器要跨 tick 持有它（存进 `PrefetchPhase::Loading`），所以必须逃逸出 crate（u2-l5 的可见性规则）。
- [src/handle.rs:L80-L86](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L80-L86) —— `QueryHit { lease, num_blocks }`：命中数与租约配对出现，`num_blocks == 0` 时 `lease` 必为 `None`。

**生产接线旁证**（qwen3，细节留给 u3-l3）：

- [executor.rs:L2158-L2159](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2158-L2159) —— `start_prefetch_load` 里 `offload.load(lease, page_ids)`：目标块来自 `reserve_loaded_blocks(num_blocks)` 预留的页 id，数量恰好等于租约块数——调用方用「预留 n 块」来满足顺序契约的前半条（长度相等）。
- [tests/cpu_roundtrip.rs:L126-L134](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L126-L134) —— 测试里 `load(lease, dst_ids)` 后 `wait()` 等完再 `stream.synchronize()`：H2D 落到 pegaflow 的传输流之后，还要等**调用方自己的流**看到这些字节（对账逐层逐段比对在此之后）。

#### 4.2.4 代码实践

**实践目标**：手算一次顺序契约的映射，验证「目标块顺序 = 前缀逻辑顺序」。

**操作步骤**：

1. 假设一次查询传入哈希前缀 `[h0, h1, h2, h3]`，返回 `Ready { num_blocks: 4, lease: Some(l) }`。调用方预留了 GPU 目标块 `[7, 3, 9, 5]`（注意不是升序——预留顺序就是传给 load 的顺序）。
2. 写出映射表：哪份 host 数据（对应哪个哈希）落到哪个 GPU 块？
3. 回答两个追问：(a) 如果只传 `[7, 3]` 两个目标会发生什么？(b) 顺序写反（`[5, 9, 3, 7]`）会发生什么——会报错吗？

**需要观察的现象 / 预期结果**（源码阅读型，无需运行）：

映射表参考答案：

| i | 租约块（哈希） | 目标 GPU 块 |
|---|---------------|------------|
| 0 | h0（链上第 1 块） | 7 |
| 1 | h1 | 3 |
| 2 | h2 | 9 |
| 3 | h3 | 5 |

(a) 长度不等违反文档契约（[src/engine.rs:L395-L397](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L395-L397)）——具体的校验/报错行为在 pegaflow 侧，本仓库不可见，**待确认**；qwen3 的做法是从源头保证相等（预留数量 = `num_blocks`）。(b) 不会报错——顺序契约只关心「按传入顺序配对」，`[5,9,3,7]` 是合法输入，但它会把 h0 装进块 5、h3 装进块 7。如果调用方的前缀账本仍按 `[7,3,9,5]` 记账，KV 与 token 的对位就全错了——**顺序契约把对位责任完全交给调用方**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load` 不像 `submit_save` / `submit_query` 那样 spawn 到 host runtime？

**答案**：`batch_load_kv_blocks_multi_layer_inproc` 是 in-proc 同步 API——提交本身不阻塞（只是登记拷贝、返回接收端），没有需要包进异步任务的等待；剩下的 DMA 完成信号已经由返回的 oneshot 承载。而 save/query 要先 `await` pegaflow 的 RPC（`batch_save_kv_blocks_from_ipc` / `count_prefix_hit_blocks_with_prefetch` 都是 async），必须跑在 runtime 上。形态差异源于底层 API 形态差异，不是为了风格。

**练习 2**：租约钉住的是「host 块」还是「GPU 目标块」？

**答案**：host 块。租约在查询时签发，那时 GPU 目标块还没预留（qwen3 是拿到租约后才 `reserve_loaded_blocks`）。它防的是「查询到加载之间 host 命中块被驱逐/覆写」；GPU 目标块的独占由调用方的 allocator 预留机制保证（reservation 在 load 完成前不释放，见 [executor.rs:L2160-L2170](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2160-L2170) 的 `PrefetchPhase::Loading { reservation, handle }`）。

**练习 3**：`LoadHandle` 为什么是 `OffloadHandle<()>`，而 `QueryHandle` 是 `OffloadHandle<QueryOutcome>`？

**答案**：load 的结果只有「成功完成 DMA」一个有意义的正结果，错误经 `Result` 的 `Err` 侧表达，`()` 够用；query 的结果本身携带信息（命中数 + 租约 / Loading 状态），需要一个数据载体 `QueryOutcome`。

### 4.3 release_query_lease：不加载时的显式释放口

#### 4.3.1 概念说明

租约的两条出路：被 `load` **消费**（拷贝开始，钉住解除责任转移给本次拷贝），或被 `release_query_lease` **显式释放**。第三条「出路」——把 `QueryLeaseId` 丢着不管——不是出路，是泄漏：token 无 Drop 逻辑，Rust 所有权系统在它身上失灵，被钉住的 host 块要保持不可驱逐直到租约 TTL 过期（u2-l3 的 GC 是最后兜底）。

什么时候会「拿到租约却不加载」？真实的场景至少有三类，qwen3 全都遇上了：

1. **预算不足**：命中 10 块，但 GPU 池的可用块减去承诺给已入场请求的保留水位后只剩 6 块——加载会饿死别人，宁可放弃命中、本地重算。
2. **目标预留失败**：决定加载、但 `reserve_loaded_blocks` 拿不到块。
3. **load 提交失败**：`load()` 同步返回 `Err`——但提交失败的路径上租约**可能已被消费也可能没有**（文档说消费发生在「过了早期校验之后」），此时 release 一个已消费的租约是无害的 no-op。

`release_query_lease` 对已消费租约是 no-op 这一点很关键：它让「出错路径上无脑调一次 release」成为安全习惯，不需要精确追踪租约状态。

#### 4.3.2 核心流程

```text
持有 lease（Ready 已结算）
│
├── 决定加载 → load(lease, dst) 成功 → lease 被消费，无释放义务
│                                  └─ DMA 完成 → 一切两清
│
├── 决定不加载（预算/预留失败）→ release_query_lease(lease) → 块解钉，可被驱逐
│
├── load 返回 Err（提交失败）  → release_query_lease(lease) → no-op if 已消费
│
└── 什么都不做（泄漏）         → 块钉到 TTL 过期（禁止！GC 每分钟兜底，但 TTL 内容量被白占）
```

#### 4.3.3 源码精读

- [src/engine.rs:L417-L424](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L417-L424) —— 文档注释全文即本模块的规范：query 把命中块钉在租约后面直到 `load` 消费；决定不加载（例如没有空闲 GPU 目标块）必须在此释放；`QueryLeaseId` 是无 Drop 的裸 token；对已被 `load` 消费的租约调用是 no-op。
- [src/engine.rs:L425-L427](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L425-L427) —— 实现只有一行转发 `host.engine.release_query_lease(&lease)`——本 crate 对租约生命周期不做任何本地记账，全部信任 pegaflow。
- [executor.rs:L2151-L2157](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2151-L2157) —— 场景 2（预留失败）：注释明说释放是为了「不让钉住的 host 块被占满整个租约 TTL」。
- [executor.rs:L2173-L2180](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2173-L2180) —— 场景 3（提交失败）：注释精确复述了消费时序——「`load` 只在过了早期校验后才消费租约；提交错误可能留下未消费的钉住块，所以 release 一次（若已消费则为 no-op）」。
- [executor.rs:L2091-L2097](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2091-L2097) 与 [executor.rs:L2608-L2613](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2608-L2613) —— 场景 1（预算不足）：决策层（remote_fetch.rs）判出 `RemoteFetchAction::Release(lease)`，执行层收到后调 `offload.release_query_lease(lease)`。决策纯函数的泛型 lease 类型让「静默丢弃租约」在类型上不可表示（每个收到 lease 的分支必须把它路由进返回值，见 [remote_fetch.rs:L13-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor/remote_fetch.rs#L13-L16)）。

#### 4.3.4 代码实践

**实践目标**：给 qwen3 里全部 `release_query_lease` 调用点分类，验证 4.3.1 的三类场景在真实代码里各有落点。

**操作步骤**：

1. 在 `pegainfer-qwen3/src/executor.rs` 中搜索 `release_query_lease`，应找到 4 处（约 L2094、L2155、L2178、L2611）。
2. 逐处阅读上下文 10 行左右，为每处填表：触发条件、所处函数、对应 4.3.1 的哪类场景、若漏调后果。
3. 再打开 [remote_fetch.rs:L109-L113](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor/remote_fetch.rs#L109-L113)，确认预算判定的精确不等式：`available_blocks.saturating_sub(reserve_floor) < num_blocks` → Release。

**需要观察的现象**：4 处调用没有一处出现在「load 成功提交之后」——成功路径不需要释放。

**预期结果**：参考分类表：

| 调用点 | 所在路径 | 场景类别 |
|--------|----------|----------|
| executor.rs:L2094 | `poll_remote_fetch` 收到 `Release(lease)` 决策 | 1 预算不足（重查 tick） |
| executor.rs:L2155 | `start_prefetch_load` 预留失败分支 | 2 目标预留失败 |
| executor.rs:L2178 | `start_prefetch_load` 的 `load()` 返回 Err | 3 提交失败（no-op if 已消费） |
| executor.rs:L2611 | 首查 tick 收到 `Release(lease)` 决策 | 1 预算不足（首查 tick） |

#### 4.3.5 小练习与答案

**练习 1**：为什么不让 `QueryLeaseId` 实现 `Drop`，drop 时自动释放？

**答案**：自动 Drop 释放会和「消费」语义打架——`load(lease, dst)` 按 move 语义拿走 lease，提交后局部变量被 drop，若带 Drop 逻辑就会把一个刚被消费的租约再释放一次（还得区分 no-op）；更重要的是 `load` 的 Err 路径需要「先尝试消费、失败再释放」的顺序控制，隐式 Drop 的执行时机（作用域退出）与这个时序对不上。显式释放把时序交给最清楚状态的人——调用方。代价是靠纪律（与下游 remote_fetch 的类型化路由）防泄漏。

**练习 2**：租约泄漏（不 release 也不 load）与丢一次 save（u2-l6）的后果有何本质区别？

**答案**：丢 save 只损失一次**未来的命中机会**，正确性无损；租约泄漏也不损正确性，但它**占用容量**：被钉住的 host 块在 TTL 内不可驱逐，等于把 pinned 池的一角冻结成死库存，极端时会挤压新 save 的空间（save 失败是 best-effort 日志），性能持续受损直到 TTL 过期。一个是「少赚」，一个是「占着茅坑」。

**练习 3**：`release_query_lease` 为什么不返回 `Result`？

**答案**：它面对的全部情况都是「释放一个可能已消费的租约」——已消费是 no-op，未消费是解钉，两种情况都没有可操作的失败（lease id 非法属于对端 bug，无事可做）。返回 `Result` 会逼所有调用点处理一个永不出现的 Err，掩盖「这条路径本来就不会失败」的设计事实。

## 5. 综合实践

把本讲三个模块串成一张状态机图。建议先自己画，再对照参考答案。

**任务**：画一张 query→lease→load→release 的状态机图，状态集：`查询中`、`Loading`、`Ready+leased`、`加载中`、`已恢复`、`已释放`。要求标注：(a) 每次状态迁移调用 engine.rs 的哪个方法（含私有 `submit_query` 经由 `query` 的路径）；(b) 哪一步出错必须显式调用 `release_query_lease`；(c) 哪些路径 lease 会被 `load` 自动消费；(d) 哪条边代表纪律违例（滞留到 TTL）。然后再用 5 句话解释 `wait_for_full_prefix` 在 P/D handoff（true）与普通 host 恢复（false）下为什么取值不同。

**参考答案（状态机）**：

```text
                 query(req_id, hashes, wfp)            [engine.rs:query → submit_query]
  ─────────────────────────────────────────────▶ ┌──────────┐
                         │                        │  查询中  │
                         │                        └────┬─────┘
        poll → Loading（仅深层配置；同 req_id 下 tick 重查）│        │ poll → Ready{lease:Some}
                         │        ▲                ▼        ▼
                         │        └──────── ┌───────────┐  Ready{lease:None,num=0}
                         │                 │  Loading  │  = 全 miss → 直接走
                         │                 └───────────┘    「本地重算」，无租约无释放
                         │                        │ poll → Ready{lease:Some,n}
                         │                        ▼
                         │                 ┌───────────────┐
                         │                 │ Ready+leased  │  （host 命中块被钉住）
                         │                 └──┬─────────┬──┘
       预算不足 / 决定不加载：                   │         │ load(lease, dst) 提交成功
       release_query_lease(lease)              │         │ ◀── lease 在此被 load 消费
                         │         load() 返回 Err：       ▼
                         │         release（no-op if 已消费）┌────────┐
                         │                     │          │ 加载中 │ LoadHandle
                         │                     │          └───┬────┘  poll
                         ▼                     ▼              ▼
                  ┌────────────────────────────────┐   ┌────────┐
                  │            已释放              │   │ 已恢复 │ DMA 完成，
                  └────────────────────────────────┘   └────────┘ 调用方流同步后可用
                                     ▲
                                     └── 纪律违例边：句柄在 Ready 结算前被 drop
                                         （submit_query 文档明令禁止）→ lease 滞留到 TTL
```

要点核对：**必须显式 release 的错误步**有两处——`Ready+leased` 后决定不加载（预算 / 预留失败），以及 `load()` 同步返回 `Err`（提交失败，租约可能未消费）。**lease 被 load 自动消费**的唯一路径是 `load` 成功提交；此后 `release_query_lease` 变 no-op。全 miss（`lease: None`）与空查询短路都不进入租约账本。

**参考答案（wait_for_full_prefix 的 5 句话）**：

1. 这个开关只决定「命中不完整时查询何时从 Loading 变 Ready」：false 时部分命中也算 Ready（拿到多少报多少），true 时必须等缺失前缀**全部**落到 host 层才 Ready，是 all-or-nothing 语义（[src/engine.rs:L319-L323](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L319-L323)）。
2. 普通 host 恢复场景下，调用方总能对未命中的尾巴做本地 prefill 重算，一段部分前缀照样缩短 prefill，所以取 false——有多少用多少。
3. 原生 P/D handoff 场景下，解码节点不打算重算：它要么拿到完整前缀、要么整个 handoff 无从谈起（重算正是 P/D 分离要消灭的代价），所以取 true——宁可停在 Loading 也不接受半截命中。
4. 归根到底，取值由「miss 的兜底能力」决定：能重算则 partial 是净收益，不能重算则 partial 是无用功。
5. 值得注意：树内 qwen3 的两个真实调用点今天都传 false（[executor.rs:L2048-L2052](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2048-L2052) 与 [executor.rs:L2568-L2572](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2568-L2572)，注释写明 partial 依然缩短本地 prefill）；文档里 true 的场景是为「不能重算的部署形态」预留的语义，两者并不矛盾。

## 6. 本讲小结

- `submit_query` 把哈希前缀交给 pegaflow 的 `count_prefix_hit_blocks_with_prefetch`，按 `PrefetchStatus` 翻译成两分支：`Ready`（终态，含命中数与租约）与 `Loading`（非终态，同 `req_id` 逐 tick 重查）；`Loading` 只在配了 SSD / P2P 深层时出现，host-memory-only 部署查询必为终态。
- 空哈希前缀短路成「已结算的全 miss」，不 spawn 任务；全 miss 的 `Ready` 不签发租约——`lease: None` 在类型上就表示无账可还。
- 租约钉住 host 命中块防「查询到加载之间被驱逐覆写」；`QueryLeaseId` 无 Drop 逻辑，唯一正确出路是被 `load` 消费或被 `release_query_lease` 显式释放，泄漏则钉到 TTL。
- `load` 是三操作中唯一的同步提交（in-proc API），提交期错误同步上抛——这正是出错路径必须补一次 release（no-op 安全）的原因；顺序契约要求 \( |\text{dst}| = n \) 且第 i 租约块进第 i 目标块、逐层成立，对位责任完全在调用方。
- qwen3 的生产接线给出三类必释放场景：预算不足（决策层 `Release`）、目标预留失败、load 提交失败；而 `LoadHandle` 是三句柄中唯一公开的——调度器要跨 tick 持有它。

## 7. 下一步学习建议

本讲读完，engine.rs 的四条链路只剩最后一条：**flush 屏障**（u2-l8）——`flush_saves_then` 如何等在途 save、排空写管线、等 MetaServer 注册送达，以及它作为 P/D KV-ready 信号如何扣住 `Finished` 事件。学完 u2-l8 你就集齐了本 crate 的全部运行时行为，可以进入专家层：u3-l1（vllm_hash 的 xxh3_128+CBOR 哈希链——本讲里「哈希从哪来」的另一半答案）和 u3-l3（remote_fetch.rs 决策纯函数的完整解剖——本讲只借用了它的 Release/Load 分支语义）。建议继续精读的源码：[src/engine.rs:L443-L504](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L443-L504)（屏障实现）与 [pegainfer-qwen3/src/executor/remote_fetch.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor/remote_fetch.rs#L80-L115)（决策级联）。
