# crate 定位：pegainfer 与 pegaflow 之间的 KV 卸载桥

## 1. 本讲目标

读完本讲，你应该能够：

1. **说清三方角色**：pegainfer（GPU 分页 KV 的所有者）、pegaflow（host/SSD/RDMA 深层存储的所有者）与 `OffloadEngine`（连接器"大脑"）各自负责什么、边界在哪里。
2. **列出并分类 `lib.rs` 的全部公开导出**：按「配置 / 引擎 / 句柄 / pegaflow 再导出 / 哈希」五类归位，并说出每个导出服务于 save、query/load 还是 P2P 数据面。
3. **解释 `Cargo.toml` 里的三个关键决策**：为什么 `pegaflow-core` 要用 git rev pin、为什么要 `default-features = false`、为什么单独开 `rdma` feature。

本讲不运行任何代码，全部实践都是**源码阅读型**——这是读任何新 crate 的第一步：先看清它的门面（`lib.rs`）和它的外部承诺（`Cargo.toml`），再进去看实现。

## 2. 前置知识

### 2.1 KV cache 与前缀复用

大模型推理时，attention 层需要用到**之前所有 token** 的 Key/Value 向量（合称 KV cache）。如果两个请求的开头相同（比如同一个系统提示词、同一段多轮对话历史），那么这些开头 token 的 KV 其实是**一字不差**的——重算是浪费。**前缀缓存**（prefix cache）就是把算好的 KV 按前缀存起来，下一个请求命中就直接复用。

KV cache 非常占显存：以 Qwen3-4B 这类 dense attention 模型为例，每个 token 每层都要存 K 和 V 两份向量。GPU 显存（HBM）装不下太多前缀，于是就有了分层存储的动机。

### 2.2 分页 KV 与分层存储

- **分页 KV（paged KV）**：把连续的 KV cache 切成固定大小的**块**（block/page，比如一块装 16 个 token），用块表管理。这和操作系统的虚拟内存分页是同一个思想：离散分配、按需映射。pegainfer 的 `pegainfer-kv-cache::KvBuffer` 就是页优先（page-first）布局的分页 KV 缓冲区。
- **分层存储（tiered storage）**：HBM（快、小、贵）→ host 主存 pinned memory（CPU DRAM，慢一些、大得多）→ SSD（更大更慢）→ RDMA（直接读**别的机器**的内存）。把不常用的 KV 块「卸载」（offload）到低层、命中时再「恢复」（restore/load）回 GPU，等价于给 KV cache 做了虚拟内存。

### 2.3 内容寻址（content-addressable）

怎么判断「另一个请求存过的块就是我想要的块」？答案是**用块内容的哈希当键**：同一前缀算出的 KV 相同 → 哈希相同 → 直接命中。这叫内容寻址，和 Git 用文件内容哈希做 commit id 是同一个思路。后面会看到本 crate 的 `namespace` 字段就是内容寻址的「作用域」。

### 2.4 Rust 的 workspace、feature 与 git 依赖

- **workspace**：多个 crate 共用一个根 `Cargo.toml` 管依赖版本，子 crate 用 `{ workspace = true }` 继承，保证全仓同一个依赖只有一份。
- **feature**：条件编译开关。依赖的 feature 是**取并集**的——两个 crate 都依赖 `cudarc`，一个开了 `cuda-12080`、另一个开了 `cuda-12090`，最终编译的是并集选出的高版本。这一点是理解本讲 `default-features = false` 的关键。
- **git 依赖 + rev pin**：`{ git = "...", rev = "..." } }` 把依赖钉死在某个具体 commit 上，保证任何人任何时间构建得到的都是同一份代码。升级 = 提 PR 到上游 → 合入 → 换一个新的 rev。

## 3. 本讲源码地图

本 crate 只有 6 个源码模块加 1 个测试文件，全部在 `pegainfer-kv-offload/` 下：

| 文件 | 作用 | 本讲涉及程度 |
| --- | --- | --- |
| `src/lib.rs` | crate 大门：crate 级文档（定位声明）、6 个私有模块声明、11 个公开导出 | **本讲主战场** |
| `Cargo.toml` | 依赖声明，含 pegaflow-core git pin 的大段注释 | **本讲主战场** |
| `src/config.rs` | `OffloadConfig`（引擎配置）与 `P2pConfig`（跨实例 P2P） | 只看导出 |
| `src/engine.rs` | `OffloadEngine`：连接器大脑，save/query/load/flush 的全部入口 | 只看导出 |
| `src/handle.rs` | `OffloadHandle<T>` 可轮询句柄及三个类型别名 | 只看导出 |
| `src/host.rs` | `OffloadHost`：共享宿主（PegaEngine + tokio runtime + P2P 生命周期） | 只看导出 |
| `src/layout.rs` | GPU 布局 → pegaflow per-layer 注册的几何映射 | 只确认它**没有**导出 |
| `src/vllm_hash.rs` | `VllmBlockHasher`：逐字节复刻 vLLM 的块哈希算法 | 只看导出 |
| `tests/cpu_roundtrip.rs` | GPU→CPU→GPU 字节级回归测试（需真实 GPU） | 不涉及 |

本讲还需要两份 crate 之外的「证据文件」：

- `docs/subsystems/runtime/pegaflow-offload-integration.md` —— 本 crate 的设计 spec，§1 是定位章节。
- `pegainfer-qwen3/src/executor.rs` 与 `pegainfer-qwen3/Cargo.toml` —— 唯一下游调用方，用来验证「再导出」设计是否成立。

## 4. 核心概念与源码讲解

### 4.1 三方角色：谁拥有哪一层 KV

#### 4.1.1 概念说明

想象一个三层抽屉柜：

- **第一层（GPU/HBM）**——pegainfer 的地盘。`pegainfer-kv-cache::KvBuffer` 持有 page-first 布局的分页 KV 缓冲区；逻辑前缀缓存（kvbm 的 `BlockPool`）也归 pegainfer。**GPU 内的前缀命中完全由 kvbm 自己处理，本 crate 不插手。**
- **第二、三层（CPU DRAM / SSD / 远端机器）**——pegaflow 的地盘。`pegaflow-core` 是一个独立的 Rust 库（novitalabs 出品，Apache-2.0），原本是 vLLM 的 KV connector 服务端，懂怎么干 D2H/H2D 拷贝、怎么管理 pinned pool、怎么做 RDMA，但**完全不懂** pegainfer 的模型、调度和前缀语义。
- **中间的搬运工大脑**——本 crate 的 `OffloadEngine`。它决定**何时**搬、**哪些**块、搬到哪层；pegaflow 只是它手下语义无关的「肌肉」。

为什么要这样切？因为 pegaflow 原本的「大脑」在 vLLM 的 Python connector 里，pegainfer 不是 vLLM，那部分一行用不上，必须用 Rust 自建。设计文档把这条分层写得非常直白。

#### 4.1.2 核心流程

设计 spec 里的三层职责图（策略 → 机制 → 机制底座）：

```text
per-model scheduler   ← 策略：哪些 block 该 resident（full 前缀 / MLA 全前缀 / 未来稀疏选择）
  ↓ 产出 load/save 意图（一组 block）
connector（kvbm logical/physical 思想）← 机制：block identity、状态机、GPU slot 编排、transfer 调度
  ↓ 语义无关的 raw transfer
pegaflow-core         ← 机制底座：D2H/H2D、DRAM/SSD/RDMA 分层
```

落到本 crate：

```text
pegainfer-qwen3 (scheduler + executor)        ← 策略层：决定何时 save / query / load
        │  调用
        ▼
pegainfer-kv-offload::OffloadEngine           ← 连接器：布局翻译 + 句柄轮询 + 时序契约
        │  驱动（内嵌 tokio runtime）
        ▼
pegaflow_core::PegaEngine                     ← 深层：host pinned pool / SSD / RDMA
```

v1 的一条重要设计红线（下一讲展开）：**save 是尽力而为的 fire-and-forget（丢了不损正确性，只是少一次命中机会）；load 在关键路径上、强排序，但绝不阻塞 admission**——每个操作都通过可轮询的 `OffloadHandle` 在调度器的每个 tick 上非阻塞地收账。

#### 4.1.3 源码精读

`lib.rs` 的第一段 crate 级文档就是整个 crate 的「定位声明」，值得逐句读：

> [src/lib.rs:L1-L6](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L1-L6) —— 开头一句点名身份：「pegainfer 与 pegaflow 之间的**进程内** KV cache 卸载桥」。注意 *in-process*：没有第二个进程、没有 IPC，pegaflow 以库的形式被嵌进 pegainfer 进程里。随后三行写清所有权划分：pegainfer 拥有 GPU 分页 KV（`KvBuffer`，page-first 布局）和逻辑前缀缓存（kvbm `BlockPool`）；pegaflow 拥有更深层（host pinned memory、SSD、RDMA）；`OffloadEngine` 是在两者之间搬块并决定何时搬的连接器「大脑」。

紧接着的段落划出 v1 的边界，这段是 dense-attention 第一版（Qwen3-4B）的行为契约：

> [src/lib.rs:L8-L15](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L8-L15) —— 三个要点：① GPU 前缀命中仍是 kvbm `BlockPool` 的原生职责，本 crate 只覆盖 CPU 层，并把「CPU 命中前缀」**叠加**在「GPU 命中前缀」之上；② 两段命中都锚定在前缀 0，所以组合命中是一段**连续前缀**，在单点切开——GPU→CPU→GPU 交错这种复杂形态被**刻意排除**；③ save 尽力而为、load 强排序但不阻塞 admission，一切操作经 `OffloadHandle` 由请求在每个调度 tick 轮询。

三方分工的权威出处是设计 spec 的 §1：

> [docs/subsystems/runtime/pegaflow-offload-integration.md:L26-L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L26-L38) —— 「定位：pegaflow 是 raw 后端，connector 大脑要自建」。解释了 pegaflow 原本是 vLLM 的 KV connector 服务端，编排逻辑全在 vLLM 的 Python 侧，`pegaflow-core` 只是底下干活的肌肉；pegainfer 接入就要用 Rust 自建那颗大脑。随后给出上面那张三层图。

这个设计已经过实测验证——spec 的 TL;DR 记录了纯 L2 基准下 Qwen3-4B 的 mean TTFT 从 195ms 降到 40ms（−79%），见 [docs/subsystems/runtime/pegaflow-offload-integration.md:L1-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L1-L5)。

#### 4.1.4 代码实践

1. **实践目标**：不看本讲正文，独立复述三方角色。
2. **操作步骤**：
   - 打开 `src/lib.rs`，只读前 15 行文档注释；
   - 打开 `docs/subsystems/runtime/pegaflow-offload-integration.md` 的 §1，把三层图抄在纸上；
   - 在三层图的每一层旁边，用 lib.rs 的原词标注「谁拥有什么」（例如第一层标 `KvBuffer` + `BlockPool`）。
3. **需要观察的现象**：lib.rs 的文档注释与 spec §1 的图**互相印证**——文档注释里的每个角色名（`KvBuffer`、`BlockPool`、`OffloadEngine`）都能在图里找到对应层。
4. **预期结果**：你能不看任何材料说出「pegainfer 管 GPU 层、pegaflow 管深层、本 crate 是中间的大脑和搬运工」，并指出 *in-process*（进程内、无第二进程）这个词的位置。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 pegaflow 是「语义无关的 raw transfer 后端」？如果让它直接理解「前缀」概念会怎样？

<details><summary>参考答案</summary>

pegaflow 只认「把 N 个字节从地址 A 搬到地址 B」这类裸传输指令（D2H/H2D、SSD、RDMA），不持有任何模型语义。lib.rs 文档与 spec §1 都强调：KV 编排（何时 save、query 几个 block、prefix 匹配、与 scheduler 的 admission 交互）原本在 vLLM 的 Python connector 侧，pegainfer 不是 vLLM，那套大脑必须用 Rust 自建。如果 pegaflow 理解「前缀」，它就得理解每个模型的 KV 几何与调度策略，每当 pegainfer 加一个模型线就要改 pegaflow——分层耦合会让两边都难以独立演进。
</details>

**练习 2**：`lib.rs:L10-L12` 说不允许 GPU→CPU→GPU 交错，只允许「GPU 命中段 + CPU 命中段 = 一段连续前缀」。为什么交错被排除？

<details><summary>参考答案</summary>

交错（比如 GPU 有块 0-2 和 6-8，CPU 有块 3-5）意味着前缀中间有「洞」，恢复时既有块也要按序拼回，且 kvbm 的 `match_and_add_prefix` 只认连续前缀。v1 把形态收紧为「都锚定 prefix 0、单点切开」，组合命中退化为一段连续前缀，GPU 命中部分交给 kvbm 原生逻辑，CPU 命中部分 load 回来注册后自然续上——实现简单、正确性容易论证。交错形态收益有限（真实请求的前缀命中极少呈斑马纹），复杂度却高得多。
</details>

### 4.2 lib.rs：模块划分与公开导出面

#### 4.2.1 概念说明

Rust crate 的 `lib.rs` 是它的**门面**（facade）。本 crate 用了一种保守而清晰的门面模式：

- 6 个模块**全部私有**（`mod xxx;` 而非 `pub mod xxx;`），外部看不见模块内部结构；
- 只通过 `pub use` 把**精心挑选的 11 个名字**递到 crate 根；
- 其中 3 个名字不是本 crate 定义的，而是从依赖 `pegaflow_core` **再导出**（re-export）来的。

这种写法的好处：内部重构（拆模块、改文件名）不破坏外部 API；调用方的 `use` 路径短且稳定；依赖边界一目了然——外部只需要 `use pegainfer_kv_offload::...`，不需要知道（也不被允许知道）底下还有个 pegaflow。

#### 4.2.2 核心流程

lib.rs 的结构可以画成一张「漏斗」：

```text
6 个私有模块                     11 个公开导出（crate 根）
─────────────────               ─────────────────────────
config    ───┐                  配置：  OffloadConfig, P2pConfig
engine    ───┼── pub use ──→    引擎：  OffloadEngine
handle    ───┤                  句柄：  OffloadHandle, LoadHandle,
host      ───┤                           QueryHit, QueryOutcome
layout    ───┘（无导出!）        宿主：  OffloadHost
                                  再导出：EngineError, PegaEngine, QueryLeaseId（来自 pegaflow_core）
vllm_hash ────── pub use ──→    哈希：  VllmBlockHasher
```

注意两个「刻意不导出」的细节（这是读门面时最有信息量的地方）：

1. **`layout` 模块零导出**：`KvArena`/`Registration` 是 `pub struct`，但处在私有模块里、crate 根没有对应 `pub use`，所以它们是 crate 内部机制，外部拿不到（全仓 grep 可证：`KvArena` 只在本 crate 的 `layout.rs` 与 `engine.rs` 出现）。
2. **`SaveHandle`/`QueryHandle` 也没导出**：它们在 `handle.rs` 里是 `pub(crate)` 类型别名（见 [src/handle.rs:L24-L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L24-L31)）——save 是 fire-and-forget，query 的句柄由引擎内部消化后只把结果类型 `QueryOutcome` 给你；外部唯一需要直接观察的只有 load（`LoadHandle`）。

#### 4.2.3 源码精读

模块声明区——6 个模块全私有：

> [src/lib.rs:L17-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L17-L22) —— `mod config; mod engine; mod handle; mod host; mod layout; mod vllm_hash;`。没有 `pub mod`，模块内部结构是私有实现细节。

自有类型导出区：

> [src/lib.rs:L24-L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L24-L31) —— 8 个自有导出：`OffloadConfig`/`P2pConfig`（配置）、`OffloadEngine`（引擎本体）、`LoadHandle`/`OffloadHandle`/`QueryHit`/`QueryOutcome`（句柄与查询结果）、`OffloadHost`（共享宿主）。

pegaflow 再导出区，带一行说明注释：

> [src/lib.rs:L32-L33](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L32-L33) —— 注释原文「Re-exported so callers name pegaflow's engine types through this bridge」（再导出，让调用方**透过这座桥**来命名 pegaflow 的引擎类型）。`EngineError`（错误类型）、`PegaEngine`（pegaflow 引擎本体）、`QueryLeaseId`（查询租约 id）三个名字从 `pegaflow_core` 进入本 crate 的命名空间。

哈希导出：

> [src/lib.rs:L34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L34) —— `VllmBlockHasher`：为了和 vLLM prefill 节点存块互通而逐字节复刻 vLLM 块哈希算法的工具（xxh3_128 + canonical CBOR，u3-l1 讲算法细节）。

「再导出让调用方不直接依赖 pegaflow」不是一句口号，下游有实证——唯一下游 `pegainfer-qwen3` 的依赖声明里**只有**本 crate，没有 pegaflow-core：

> [pegainfer-qwen3/Cargo.toml:L23-L24](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L23-L24) —— qwen3 只声明 `pegainfer-kv-cache` 与 `pegainfer-kv-offload` 两个 workspace 依赖。

但 qwen3 的代码里确实用到了 pegaflow 的类型——全部走 `pegainfer_kv_offload::` 前缀：

> [pegainfer-qwen3/src/executor.rs:L33-L35](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L33-L35) —— `use pegainfer_kv_offload::{LoadHandle, OffloadConfig, OffloadEngine};`
>
> [pegainfer-qwen3/src/executor.rs:L1978](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1978) —— 错误类型写作 `pegainfer_kv_offload::EngineError`，而不是 `pegaflow_core::EngineError`。
>
> [pegainfer-qwen3/src/executor.rs:L2147](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2147) —— 租约 id 写作 `pegainfer_kv_offload::QueryLeaseId`。

#### 4.2.4 代码实践

1. **实践目标**：亲手数一遍导出面，确认「11 个名字、5 个来源模块、3 个再导出」。
2. **操作步骤**：
   - 读 [src/lib.rs:L24-L34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L24-L34)，数 `pub use` 行数；
   - 对每个名字，用编辑器跳转（或 grep）找到它的定义模块，确认哪些来自本 crate、哪些来自 `pegaflow_core`；
   - 特别验证「`layout` 模块零导出」：grep 全仓 `KvArena`，确认命中只在 `pegainfer-kv-offload/src/` 内。
3. **需要观察的现象**：`pub use` 共 8 行；其中 1 行同时导出 3 个 pegaflow 名字（`pub use pegaflow_core::{EngineError, PegaEngine, QueryLeaseId};`）；没有任何一行来自 `layout`。
4. **预期结果**：8 行 `pub use`、共 11 个名字（8 自有 + 3 再导出）；`KvArena` 的全仓命中只在 `src/layout.rs` 与 `src/engine.rs`。

#### 4.2.5 小练习与答案

**练习 1**：外部调用方想观察一次 save 的完成时机，它能拿到 `SaveHandle` 吗？该怎么观察？

<details><summary>参考答案</summary>

拿不到。`SaveHandle` 是 `pub(crate)` 类型别名（handle.rs L26），未出现在 lib.rs 导出面。save 的公开形态是 fire-and-forget（`save`）或阻塞到完成（`save_blocking`）；如果调用方确实要等一批 save 落地（例如 P/D handoff 前要保证 KV 对 peer 可见），用的不是单条 save 的句柄，而是 `OffloadEngine::flush_saves_then` 屏障（u2-l8 专讲）。
</details>

**练习 2**：`OffloadHost` 被导出了，但它的字段全是 `pub(crate)`（见 [src/host.rs:L30-L37](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L30-L37)）。外部拿到一个 `OffloadHost` 能用它做什么？

<details><summary>参考答案</summary>

只能把它当作**共享宿主的身份句柄**传递和持有。多个 rank 级 `OffloadEngine` 可以挂在同一个 `OffloadHost` 上（「一个 host 服务任意多个引擎」的 DP-rank 共享模型）；持有句柄还意味着宿主的生命周期——drop 最后一个句柄会 drop 内嵌 tokio runtime、放弃在途 save、停掉 P2P 服务。字段不可见，所以外部无法绕过 `OffloadEngine` 直接戳 pegaflow。
</details>

### 4.3 Cargo.toml：git rev pin 与 feature 取舍

#### 4.3.1 概念说明

`Cargo.toml` 是 crate 的**外部承诺**：它依赖谁、开什么 feature，决定了所有下游的编译行为。本 crate 的依赖清单短，但 `pegaflow-core` 那一行浓缩了三个决策：

1. **git rev pin（钉死 commit）**：`rev = "1411240..."` 把 pegaflow 钉在上游 master 的一个具体提交上。任何人、任何时候、任何机器构建这个 crate，拿到的 pegaflow 代码都相同——可复现性。注释里写明这个 rev 的来历：它包含 PR #395（P 侧 partial-tail save + 首 token 路由转发），以及更早合入的 PR #381（metaserver flush barrier + 可嵌入的 P2pTransferService）。
2. **`default-features = false`（砍掉默认 feature）**：pegaflow-core 默认带一个 `cuda-12` feature，会拉进它自己选的 `cudarc/cuda-12080`。而 pegainfer workspace 统一用 `cudarc/cuda-12090`。Cargo 的 feature 并集规则意味着两个 cudarc 版本会共存打架（selector 冲突），所以必须砍掉 pegaflow 的默认 feature，让 workspace 那一份 cudarc（12090 + nvrtc + runtime）同时覆盖两边。
3. **`features = ["rdma"]`（单独开 RDMA）**：`rdma` 拉进 pegaflow-transfer——跨节点块拉取（RDMA READ）+ MetaServer 发现，即 **P/D 分离（prefill/decode 分离）的数据面**。这是本 crate 服务跨实例 P2P 共享的通道。

其余依赖各司其职：`pegainfer-kv-cache`（拿 `KvBuffer` 与稳定基址 `device_ptr`）、`tokio`/`tokio-stream`（内嵌异步 runtime 与网络流）、`half`（bf16 布局计算）、`xxhash-rust`（vLLM 哈希兼容）、`log`（日志）。

#### 4.3.2 核心流程

升级 pegaflow 的固定工作流（spec §5.2 记录）：

```text
临时把 git 依赖换成 path 依赖（指向本地 clone）共同开发
  → 向 novitalabs/pegaflow 提 PR
  → 上游合入 master
  → 回到本 Cargo.toml，把 rev 换成合入后的新 commit
```

这个 crate 的历史正是这么走的：spec §5.2 里记录的依赖行还是旧 rev `07cac7e`（含 PR #331+#333），如今 Cargo.toml 已 re-pin 到 `1411240`（含 PR #395）。**spec 是过程记录，Cargo.toml 是当下事实**——两者 rev 不一致时以 Cargo.toml 为准。

#### 4.3.3 源码精读

依赖区整体（注释比代码长，是本文件最有价值的部分）：

> [Cargo.toml:L7-L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/Cargo.toml#L7-L31) —— `[dependencies]` 全表：cudarc / half / log / pegainfer-kv-cache / pegaflow-core / tokio / tokio-stream / xxhash-rust，全部 `{ workspace = true }`（pegaflow-core 除外，它是 git 依赖）。

注释块逐层解释 pin 与 feature：

> [Cargo.toml:L8-L20](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/Cargo.toml#L8-L20) —— 「进程内嵌入：pegaflow-core 是 host/SSD/RDMA 卸载层」；钉在落了 PR #395 的上游 master rev 上；`default-features=false` 砍掉会与 workspace 冲突的 `cuda-12`（cudarc/cuda-12080 selector 打架），workspace 的 cudarc 已提供 cuda-12090 + nvrtc + runtime，同时覆盖 pegaflow-core 与 pegaflow-transfer；`rdma` 启用 pegaflow-transfer：跨节点块拉取 + MetaServer 发现——P/D 分离数据面。

依赖行本体：

> [Cargo.toml:L25-L27](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/Cargo.toml#L25-L27) —— `pegaflow-core = { git = "https://github.com/novitalabs/pegaflow.git", rev = "14112403bc1d0ed5b44717c0a93743fb0de66fee", default-features = false, features = ["rdma"] }`。四个要素齐活：仓库、rev、砍默认、开 rdma。

升级工作流的出处：

> [docs/subsystems/runtime/pegaflow-offload-integration.md:L67-L71](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L67-L71) —— §5.2 依赖分析：记录了当时的依赖行（旧 rev `07cac7e`）以及「下次再改 pegaflow：临时换回 path dep 共同开发 → 提 PR → 合入后 re-pin rev」的工作流。

另有一处旁证说明 feature 冲突的真实性——`pegainfer-kv-store` 的 Cargo.toml 专门写了注释解释它为什么也要 `default-features = false` 来避开本 crate 拉进的 cuda-12 冲突（[pegainfer-kv-store/Cargo.toml:L21](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-store/Cargo.toml#L21)）。cudarc 的版本管理是整个 workspace 的敏感点。

#### 4.3.4 代码实践

1. **实践目标**：验证「spec 与 Cargo.toml 的 rev 可以不一致，以 Cargo.toml 为准」，并理解 rev pin 的可复现含义。
2. **操作步骤**：
   - 读 [Cargo.toml:L25](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/Cargo.toml#L25) 记下 rev 前 7 位（`1411240`）；
   - 读 [docs/subsystems/runtime/pegaflow-offload-integration.md:L67-L71](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L67-L71) 找出 spec 记录的旧 rev（`07cac7e`）；
   - 对比两处注释各自声称包含的 PR 编号（spec：#331+#333；Cargo.toml：#381+#395）。
3. **需要观察的现象**：两处 rev 不同，且 Cargo.toml 注释声称的 PR 更新——说明 spec 写于更早时点，之后发生了一次 re-pin。
4. **预期结果**：能说出「rev pin 的含义 = 构建永远拿同一份 pegaflow 代码；升级 pegaflow 必须显式改这一行」；发现 spec 是快照、Cargo.toml 是实时真相这个文档学事实。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉 `default-features = false`，构建会发生什么？

<details><summary>参考答案</summary>

pegaflow-core 的默认 feature `cuda-12` 会拉进它自选的 `cudarc/cuda-12080`，与 workspace 已有的 `cudarc/cuda-12090` 形成两个 cudarc 版本共存的 selector 冲突（Cargo.toml L16-18 注释原话："clashing cudarc/cuda-12080 selector"）。砍掉默认 feature 后，pegaflow-core 复用 workspace 那一份 cudarc（12090 + nvrtc + runtime），一份代码同时服务两边。
</details>

**练习 2**：为什么 `rdma` 要单独作为 feature 开，而不是 pegaflow-core 默认打开？

<details><summary>参考答案</summary>

`rdma` 会拉进 pegaflow-transfer（跨节点块拉取 + MetaServer 发现），这条 P2P 数据面只有做 P/D 分离 / 跨实例共享的部署才需要，而且对环境有额外要求（RDMA NIC、MetaServer 地址等，见 `P2pConfig` 的 `rdma_nics`/`metaserver_addr`）。做成可选 feature 让不需要跨节点互通的部署不必编译这条链路。本 crate 因为要提供完整的 P/D 数据面而显式打开它。顺带一提：设计文档把「rdma feature 未被 gate 进本 crate 的可选 feature」列为已知打包欠账（u3-l5 展开）。
</details>

## 5. 综合实践

现在完成本讲的完整实践任务：**不运行任何代码**，仅凭阅读 `lib.rs` 与 `Cargo.toml`，产出三样东西。

### 任务 A：一句话定位

用一句话说清这个 crate 是什么。先自己写，再对照参考答案。

> **参考答案**：pegainfer-kv-offload 是把 pegainfer 的 GPU 分页 KV（`KvBuffer` + kvbm `BlockPool`）与 pegaflow 的 host/SSD/RDMA 深层存储接起来的**进程内连接器**，其核心 `OffloadEngine` 决定 KV 块何时在两层之间搬运，并以可轮询句柄的形态向调度器暴露 save/query/load。

检验标准：你的句子应包含「进程内」「连接器/桥」「GPU 分页 KV 归 pegainfer」「深层归 pegaflow」「OffloadEngine 是大脑」这五个要素中的至少四个。

### 任务 B：公开 API 分类表

对 `lib.rs` 的全部 11 个导出，按五行分类表填写，每个名字标注它主要服务于 save、query/load 还是 P2P：

| 分类 | 导出 | 服务的链路 |
| --- | --- | --- |
| 配置 | `OffloadConfig` | 全部（构造引擎的入口参数） |
| 配置 | `P2pConfig` | P2P（MetaServer 地址 / 广播地址 / RDMA 网卡） |
| 引擎 | `OffloadEngine` | 全部（save/query/load/flush 的所有入口） |
| 句柄 | `OffloadHandle<T>` | 全部（三类操作统一的观察窗口） |
| 句柄 | `LoadHandle` | query/load（H2D 恢复完成即 resolve） |
| 句柄 | `QueryHit` | query/load（命中块数 + 租约） |
| 句柄 | `QueryOutcome` | query/load（Ready / Loading 两分支） |
| 宿主 | `OffloadHost` | P2P + 多引擎共享（一个 host 服务任意多 rank 引擎） |
| pegaflow 再导出 | `EngineError` | 全部（错误类型的统一语言） |
| pegaflow 再导出 | `PegaEngine` | 全部（pegaflow 引擎本体，宿主持有） |
| pegaflow 再导出 | `QueryLeaseId` | query/load（钉住命中块的租约凭证） |
| 哈希 | `VllmBlockHasher` | save + query（生成内容寻址键；跨引擎互通时两端用同一哈希） |

把你自己的表与上表对答案。容易错的点：`OffloadHost` 不是「引擎的配置」而是「可共享的宿主句柄」；`VllmBlockHasher` 同时服务 save（写入时算键）和 query（查询时算键），不是只有一边。

### 任务 C：为什么再导出，而不是让调用方直接依赖 pegaflow-core

写下你的 3-5 句理由，对照下面的参考论证：

> **参考答案**：
> 1. **依赖边收敛**：调用方（pegainfer-qwen3）只需声明对 `pegainfer-kv-offload` 的依赖，不必在自己的 Cargo.toml 里出现 pegaflow-core——实测 qwen3 的 Cargo.toml 确实没有它（[pegainfer-qwen3/Cargo.toml:L23-L24](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L23-L24)），但代码里能写 `pegainfer_kv_offload::EngineError`（[executor.rs:L1978](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1978)）。
> 2. **版本唯一性**：pegaflow 的 git rev pin 只写在这一个 Cargo.toml 里。如果每个下游都自己依赖 pegaflow-core，rev 可能各自漂移，出现「桥用的是 A 版、调用方用的是 B 版」的类型不兼容；再导出从机制上保证全仓只有一份 pegaflow。
> 3. **可替换性**：`lib.rs:L32` 的注释「callers name pegaflow's engine types through this bridge」点明意图——pegaflow 是实现细节，调用方透过桥命名它。将来若换掉 pegaflow（或升级其 API），调用方代码里所有 `pegainfer_kv_offload::EngineError` 路径不变，只有桥内部改。
> 4. **feature 冲突隔离**：`default-features = false` + `rdma` 的取舍也只有桥这一处需要操心（cudarc 版本打架是 workspace 级风险，见 4.3），下游全部被屏蔽。

## 6. 本讲小结

- **三方角色**：pegainfer 拥有 GPU 分页 KV（`KvBuffer` + kvbm `BlockPool`，GPU 前缀命中是 kvbm 的原生职责）；pegaflow 拥有 host/SSD/RDMA 深层且语义无关；`OffloadEngine` 是进程内的连接器大脑，决定何时搬哪些块。
- **v1 边界**：本 crate 只覆盖 CPU 层，CPU 命中前缀叠加在 GPU 命中前缀之上（同锚 prefix 0、单点切开、连续），GPU→CPU→GPU 交错被刻意排除；save 尽力而为，load 不阻塞 admission，一切经可轮询句柄收账。
- **导出面**：6 个私有模块 → 11 个名字（8 自有 + 3 个 pegaflow 再导出），按「配置 / 引擎 / 句柄 / 宿主 / 哈希」分类；`layout` 模块（`KvArena`）与 `SaveHandle`/`QueryHandle` 刻意不导出。
- **再导出的意义**：调用方透过桥命名 pegaflow 类型，依赖边收敛到一处、rev pin 唯一、pegaflow 可整体替换——qwen3 是活证据。
- **Cargo.toml 三决策**：git rev pin（可复现 + 记录包含哪些上游 PR）、`default-features = false`（避开 cudarc 12080/12090 selector 冲突）、`features = ["rdma"]`（拉进 P/D 分离数据面）。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：`心智模型：为什么需要 KV 卸载，三层存储与 P/D 分离`——把本讲的三方角色放进一次真实的多轮对话时序里，理解 save 的 best-effort 与 load 的强排序在关键路径上的差别。
- **再下一讲（u1-l3）**：`编译、测试与代码走读准备`——本 crate 哪些测试纯 CPU 可跑（layout/vllm_hash 内联单测）、哪些必须真 GPU（`tests/cpu_roundtrip.rs`）。
- **源码预读**：如果你想在进入 u2 前热身，先通读一遍 [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L1-L35)（35 行，5 分钟）和 [docs/subsystems/runtime/pegaflow-offload-integration.md](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/subsystems/runtime/pegaflow-offload-integration.md#L1-L5) 的 TL;DR 与 §0 实现状态。
