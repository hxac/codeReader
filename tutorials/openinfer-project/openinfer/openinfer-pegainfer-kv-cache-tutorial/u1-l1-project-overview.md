# PegaInfer 全景与 kv-cache 的位置

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `pegainfer-kv-cache` 在整个 PegaInfer 推理引擎中的**职责边界**——它管什么、不管什么。
- 理解本 crate 最核心的设计决策：**逻辑块池（`BlockPool`）与物理显存（`KvBuffer`）两层分离**，以及为什么要这样分。
- 打开 `src/lib.rs`，列出全部公共导出类型，并为每个类型写出一句话职责说明——这是后续所有讲义的「词汇表」。

本讲是整个学习手册的第一讲，**不要求你理解任何实现细节**，只要求建立正确的全景地图。

## 2. 前置知识

用通俗语言快速过一遍本讲需要的基础概念。

### 2.1 KV Cache 是什么

大语言模型生成文本是逐个 token 进行的：每生成一个新 token，注意力层都要拿新 token 的查询向量（Query）去和**序列里所有历史 token** 的键（K）、值（V）做注意力计算。如果每步都重算全部历史 token 的 K/V 投影，代价是平方级的浪费。

**KV Cache 的做法**：把每个 token 每一层的 K、V 向量算一次后缓存下来，后续步骤直接读缓存。推理显存的大部分通常就被这块缓存占用，所以「怎么存、怎么分配、怎么回收」直接决定引擎的吞吐和并发能力。

### 2.2 分页（Paging）

类比操作系统虚拟内存：与其给每个请求预先划一大块**连续**显存（请求长短不一，会产生大量碎片），不如把 KV 缓存切成固定大小的**页（page/block）**，请求按需领页，用一个「页表」（page indices 数组）把逻辑顺序映射到物理页。请求长了多领几页，结束了把页还回去，同一前缀的多个请求还能**共享**同一批页（前缀缓存）。

### 2.3 Rust workspace 与 crate

本仓库是一个 Cargo workspace：根 `Cargo.toml` 列出所有成员 crate（member），每个成员在自己的目录里有独立的 `Cargo.toml` 和 `src/`。`pegainfer-kv-cache` 是其中一个成员——一个被各模型 crate 复用的**库 crate**，没有 `main` 函数。

### 2.4 CUDA 显存的两个事实

- GPU 显存（device memory）需要显式分配，地址在分配后**不会移动**。
- 推理引擎普遍使用 **CUDA Graph**：把解码路径的一串内核调用录制重放以消除启动开销，这要求所有缓冲区**地址稳定**——本 crate 的物理缓冲正是一次性分配、永不搬家的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `../CLAUDE.md` | 仓库级开发指南：引擎架构图、各 crate 分工、构建与测试约定。本讲只读「Architecture」一节 |
| `Cargo.toml` | 本 crate 的包声明与 7 个 workspace 依赖，看依赖就能猜出它「靠什么干活」 |
| `src/lib.rs` | 库根，仅 19 行：5 个私有模块声明 + 公共类型再导出。**本讲主角** |
| `src/pool.rs` | 逻辑块池与请求生命周期（本讲只看头部的类型文档注释） |
| `src/buffer.rs` | 物理 GPU 缓冲（本讲只看头部文档注释） |
| `src/manager.rs` | 逻辑/物理配对门面（本讲只看头部文档注释） |
| `src/view.rs` | 内核视图（本讲只看头部文档注释） |
| `src/layout.rs` | 页几何纯值类型（本讲只看头部文档注释） |

## 4. 核心概念与源码讲解

### 4.1 PegaInfer 引擎全景与本 crate 的职责边界

#### 4.1.1 概念说明

PegaInfer 是一个**纯 Rust + CUDA** 的 LLM 推理引擎——没有 PyTorch、没有 Python 框架依赖，对外提供 OpenAI 兼容的 `/v1/completions` API。每条模型线（qwen3、qwen35、kimi-k2、glm52……）各占一个独立的 crate，**每个模型 crate 自己持有调度器和执行器**。

在这么多模型线之间，有一件事是共性的：分页 KV 缓存的块分配、前缀匹配、回收。如果每条模型线各写一套，逻辑会重复且容易分叉。`pegainfer-kv-cache` 就是把这套**共享分页 KV 原语**抽出来的 crate。

它的职责边界（管什么 / 不管什么）：

- **管**：逻辑块池（谁拥有哪个块、块能否共享、何时回收）、全注意力模型的物理缓冲分配、页几何、给内核的视图打包、前缀缓存探测、CPU→GPU 恢复预留、KV 事件流。
- **不管**：请求排队/调度策略（各模型调度器的事）、注意力计算本身（`pegainfer-kernels` 编译的 CUDA/FlashInfer 内核的事）、host/SSD/RDMA 分层存储（`pegainfer-kv-offload` 的事）。

仓库的 `CLAUDE.md` 用一句话给 KV state 定位（这条是理解本 crate 的锚点）：

> **KV state** — model schedulers own request state; shared paged-KV primitives live in `pegainfer-kv-cache`; host/SSD/RDMA offload bridge in `pegainfer-kv-offload`.

翻译过来：**模型调度器持有请求状态；共享的分页 KV 原语住在 `pegainfer-kv-cache`；host/SSD/RDMA 卸载桥住在 `pegainfer-kv-offload`。**

#### 4.1.2 核心流程

一次请求在引擎中的完整数据流（照着 `CLAUDE.md` 的架构图走）：

```
HTTP Request
   │
   ▼
vLLM frontend（pegainfer-frontend：EngineHandle / GenerateRequest / TokenEvent 契约）
   │
   ▼
模型调度器/执行器（如 pegainfer-qwen3）──请求状态自持
   │   │
   │   ├──调度时──▶ BlockPool（pegainfer-kv-cache）：领块 / 还块 / 前缀匹配
   │   │
   │   └──前向时──▶ 用块 ID 索引 KvBuffer，打包成 KvView/KvViewDesc
   │                    │
   │                    ▼
   │              attention 内核（pegainfer-kernels 编译的 CUDA/FlashInfer）
   ▼
TokenEvent 流回前端
```

记住一条主线：**调度器问 BlockPool「给我块」，内核拿块 ID 到 KvBuffer 里算**。逻辑与物理的分离贯穿本 crate 的每个文件。

#### 4.1.3 源码精读

引擎分层图与关键抽象在仓库级开发指南里：

- [../CLAUDE.md:L87-L87](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L87-L87) — 一行架构总图：`HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent`，本 crate 服务于中间的 scheduler/executor 一层。
- [../CLAUDE.md:L109-L113](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L109-L113) — 四条关键抽象连起来读：每个模型 crate 自持 config/权重/调度器（L109）；`pegainfer-kernels` 负责 CUDA/cuBLAS/FlashInfer/Triton 内核编译（L111）；CUDA Graph 要求预分配缓冲保持**指针稳定**（L112）；KV state 三分——调度器持有请求状态、共享分页原语在本 crate、卸载桥在 `pegainfer-kv-offload`（L113）。

逻辑层不持有 GPU 显存这一点，写在 `BlockPool` 的文档注释里，说得非常直白：

- [src/pool.rs:L26-L35](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L26-L35) — `BlockPool` 自述：「Logical KV block pool… **Owns no GPU memory** — the physical layout (full-attention `KvBuffer`, MLA dual ckv/kpe buffers, ...) lives with the consumer and is indexed by the block IDs this pool hands out.」（逻辑块池不拥有任何 GPU 显存——物理布局由消费者持有，并用本池发放的块 ID 索引。）
- [src/buffer.rs:L16-L23](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L16-L23) — `KvBuffer` 的自述正好是另一面：「GPU KV cache buffer **without an allocator**… delegates block allocation to an external `BlockManager`」（不自带分配器的 GPU KV 缓冲，把块分配委托给外部 BlockManager）。

两段注释对照读，「两层分离」就不是抽象口号，而是两个结构体各自的合同。

#### 4.1.4 代码实践

1. **实践目标**：把职责边界从「读到的」变成「能复述的」。
2. **操作步骤**：
   - 打开仓库根目录的 `CLAUDE.md`，只读 `## Architecture` 一节（架构图 + Key abstractions 列表）。
   - 对照上面的数据流图，找出 `pegainfer-kv-cache` 应该插在哪两个环节之间。
3. **需要观察的现象**：架构图里**没有**任何一个模型 crate 直接画到 CUDA/cuBLAS 那一行——它们都经过 `pegainfer-core runtime + pegainfer-kernels`；而 KV state 一行又单独点名了本 crate。
4. **预期结果**：你能不看资料说出三分格局——请求状态在模型调度器、分页原语在本 crate、卸载桥在 `pegainfer-kv-offload`。

#### 4.1.5 小练习与答案

**练习 1**：为什么请求状态（每个请求占到第几个 token）不放进 `pegainfer-kv-cache`，而是留在各模型调度器里？

**参考答案**：不同模型线的调度语义差异极大（全注意力、线性注意力混合、MoE 专家并行、投机解码），请求何时预填、何时解码、批怎么组是**模型调度策略**；而块池只关心通用的「发放/回收/共享块 ID」事务。把策略留在模型 crate、把机制抽到本 crate，才能让七八条模型线共享同一套分页原语而不互相牵制（`CLAUDE.md` L113 的原文即此分工）。

**练习 2**：`pegainfer-kv-offload` 和 `pegainfer-kv-cache` 是什么关系？

**参考答案**：分工是「事务层 vs 搬运层」。本 crate 决定哪些块值得存/取（如 `PrefixProbe` 产出 CPU 层查询键、`LoadReservation` 预留 GPU 目的地块）；`pegainfer-kv-offload` 负责真正的 host/SSD/RDMA 搬运桥（`CLAUDE.md` L113，其内部结构在后续 u3-l1 讲展开）。

### 4.2 `src/lib.rs`：19 行的公共表面

#### 4.2.1 概念说明

一个库 crate 对外的全部脸面就是 `lib.rs` 里的 `pub use`。本 crate 的 `lib.rs` 只有 19 行，却是后面十讲的「目录页」：**5 个私有模块 + 11 个自有公共类型 + 2 条来自 `kvbm-logical` 的再导出**。

结构上分三组理解：

1. **几何/物理组**：`KvLayout`（一页的几何尺寸）、`KvBuffer`（物理显存）。
2. **配对门面组**：`KvCacheManager`（把同几何的池和缓冲配成一对，全注意力模型用）。
3. **逻辑事务组**（全部来自 `pool.rs`）：`BlockPool`、`RequestKv`、`PrefixProbe`、`LoadReservation`、`KvBlockGuard`、`RegisteredBlock`，加上 `view.rs` 的 `KvView`/`KvViewDesc`。

另有两条再导出值得注意：`pub use kvbm_logical;` 把上游块管理器 crate 整体暴露给使用者（模型 crate 可以直接用它的 `BlockManager` 相关类型），`pub use kvbm_logical::events::KvCacheEvent;` 则暴露块事件类型——本 crate 自身没有定义任何新类型给它，只是转发。

#### 4.2.2 核心流程

模型 crate 消费这组公共表面的典型顺序（细节在 u2 展开，这里只建立主干）：

```
KvCacheManager::new(stream, 几何参数…)      // 一次配好 BlockPool + KvBuffer
   │
   ▼
pool.new_request(...) → RequestKv            // 每个请求一份逻辑状态
   │
   ▼
RequestKv::schedule_prefill / schedule_decode // 向池要块（只调度不应用）
   │
   ▼
RequestKv::prefill_view / decode_view → KvView // 生成页表视图
   │
   ▼
KvView::desc(&buffer) → KvViewDesc           // 打包布局+指针+页表给内核
   │
   ▼
RequestKv::apply_prefill / apply_decode       // 应用（或 revert_schedule 回退）
```

#### 4.2.3 源码精读

整个文件值得逐行读一遍——它就是本 crate 的目录页：

- [src/lib.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L1-L5) — 5 个**私有**模块声明：`buffer`、`layout`、`manager`、`pool`、`view`。注意没有任何 `pub mod`——外部世界只能通过下面的再导出按类型名访问，模块内部结构（比如 `pool.rs` 有 750+ 行）完全隐藏。
- [src/lib.rs:L7-L19](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L7-L19) — 全部公共再导出，按模块分组：`buffer::KvBuffer`（L7）；`kvbm_logical` 整体再导出与 `KvCacheEvent`（L8-L9）；`layout::KvLayout`（L10）；`manager::KvCacheManager`（L11）；`pool` 的六个类型 `BlockPool`/`KvBlockGuard`/`LoadReservation`/`PrefixProbe`/`RegisteredBlock`/`RequestKv`（L12-L17）；`view::KvView`/`KvViewDesc`（L18-L19）。

配对门面的定位写在 `KvCacheManager` 的文档注释里，顺带解释了为什么 MLA 模型不走这个门面：

- [src/manager.rs:L10-L18](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L10-L18) — 「Engine-level KV cache for full-attention models: one logical `BlockPool` + one GPU `KvBuffer` with matching geometry. MLA models (kimi-k2) consume `BlockPool` directly and own their physical buffers — this facade is the full-attention pairing only.」（全注意力模型的引擎级门面；MLA 模型直接消费 `BlockPool` 并自持物理缓冲，此门面只做全注意力配对。）

11 个自有类型的一句话职责（**这张表是整个学习手册的词汇表，后续每讲都会用到**）：

| 类型 | 源码 | 一句话职责 |
| --- | --- | --- |
| `KvLayout` | [src/layout.rs:L4-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L4-L16) | 页优先几何的纯值类型：4 个维度 + 3 个推导步长，无 GPU 无分配 |
| `KvBuffer` | [src/buffer.rs:L20-L23](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L20-L23) | 持有连续 GPU 显存与几何的「无分配器」物理缓冲，一次性分配永不移动 |
| `KvCacheManager` | [src/manager.rs:L15-L18](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L15-L18) | 全注意力模型的引擎级门面：一个逻辑池 + 一个同几何物理缓冲 |
| `BlockPool` | [src/pool.rs:L31-L35](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L31-L35) | 逻辑块池：包装 kvbm-logical 的 BlockManager 并预留 padding 块，不持有 GPU 显存 |
| `RequestKv` | [src/pool.rs:L379-L388](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L379-L388) | 每请求 KV 状态：包装 `SchedulableSequence`，走 schedule→view→apply 生命周期 |
| `KvView` | [src/view.rs:L12-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L12-L16) | 请求 KV 状态的轻量非拥有视图：`page_indices + seq_len + page_size` 三字段 |
| `KvViewDesc` | [src/view.rs:L69-L75](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L69-L75) | 内核元数据束：布局 + 缓冲指针 + 页表 + 序列长度一次打包 |
| `PrefixProbe` | [src/pool.rs:L263-L274](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L263-L274) | prompt 前缀对 GPU 缓存的解析结果：GPU 命中块数、CPU 查询键、持住的强引用 |
| `LoadReservation` | [src/pool.rs:L354-L356](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L354-L356) | 为 CPU→GPU 加载预留的目的地块集合，DMA 落地后由 `commit_loaded_blocks` 消费 |
| `KvBlockGuard` | [src/pool.rs:L350-L350](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L350) | 已注册块的强引用「销钉」：在途异步 offload 保存期间阻止物理槽位被重分配 |
| `RegisteredBlock` | [src/pool.rs:L728-L733](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L728-L733) | 请求刚注册的完整 KV 块：路由侧 u64 哈希 + 引擎 128 位 lineage 哈希，用于 KV 路由事件 |

#### 4.2.4 代码实践

1. **实践目标**：不借助资料，说出每个公共类型属于哪个模块、解决什么问题。
2. **操作步骤**：
   - 读 [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L1-L19) 全文。
   - 对每个再导出的类型，用编辑器跳转到定义（或读上表给出的链接），**只读类型上方的文档注释**，不读实现。
   - 合上资料，自己复述 4.2.3 的表格。
3. **需要观察的现象**：`pool.rs` 一个文件贡献了 6 个公共类型，是公共表面的主产地；`view.rs` 和 `buffer.rs` 各只有 2 个和 1 个。
4. **预期结果**：能默写「11 个类型 = pool 6 个 + view 2 个 + buffer/layout/manager 各 1 个」，并能按 4.2.1 的三组分 组。

#### 4.2.5 小练习与答案

**练习 1**：`lib.rs` 为什么用 `mod pool;`（私有）+ `pub use pool::BlockPool;`，而不是直接 `pub mod pool;`？

**参考答案**：私有模块 + 选择性再导出把内部文件组织（哪个类型放在哪个文件）变成实现细节，外部只能按类型名访问。这样后续重构（比如把 `RequestKv` 拆到新文件）不会破坏任何使用者的路径——公共 API 面最小、最稳定。

**练习 2**：`pub use kvbm_logical;`（整体再导出一个 crate）和 `pub use kvbm_logical::events::KvCacheEvent;` 各自的意图是什么？

**参考答案**：前者让下游模型 crate 能以 `pegainfer_kv_cache::kvbm_logical::...` 的路径直接使用上游块管理器的类型（本 crate 的公共类型签名里出现了一些 kvbm 类型，如 `PrefixProbe` 内部持有 `SequenceHash`、`RequestKv` 包装 `SchedulableSequence`，下游需要能命名这些类型才能顺利互操作）；后者单独转发事件类型，让缓存感知路由的消费者不必知道事件住在 `kvbm_logical::events` 这么深的路径下。

**练习 3**：`KvCacheManager` 的文档说 MLA 模型（kimi-k2）「consume `BlockPool` directly」——这对「两层分离」设计是佐证还是例外？

**参考答案**：是佐证。正因为逻辑池与物理缓冲解耦，几何不同的模型线（MLA 的物理布局是 ckv/kpe 双缓冲，见 `pool.rs` L28-L30 的注释）才能复用同一个 `BlockPool` 的事务逻辑、自持各自的物理缓冲。分离让「换物理布局」不必重写「块事务」。

### 4.3 workspace 中的邻居：依赖与兄弟 crate

#### 4.3.1 概念说明

看一个 crate「靠什么干活」，最快的办法是读它的 `Cargo.toml`。本 crate 有 7 个 workspace 依赖，每个都对应一块职责：

| 依赖 | 在本 crate 中的角色 |
| --- | --- |
| `cudarc` | CUDA 驱动 API 的 Rust 绑定：`KvBuffer` 用它分配/持有 `CudaSlice<bf16>` 显存 |
| `half` | `bf16` 半精度类型——KV 缓存的存储格式 |
| `kvbm-logical` | 逻辑块管理的核心：`BlockManager`、块注册/哈希、事件；本 crate 对它的包装即 `BlockPool` |
| `dynamo-kv-hashing` | 加盐哈希（如 LoRA 名做盐），服务前缀缓存隔离 |
| `tokio` | 异步运行时，这里主要用于 KV 事件流的 broadcast 通道 |
| `anyhow` | 错误类型（构造失败等返回 `anyhow::Result`） |
| `pegainfer-kernels` | workspace 兄弟 crate；**在当前源码中声明了依赖但没有直接 `use`**（见下方源码精读），是事实上的预留/传递性声明 |

值得强调的依赖方向：**没有任何模型 crate 依赖在本 crate 之下**——依赖箭头是「模型 crate → 本 crate → kvbm-logical/cudarc」，共享原语位于依赖图的底层，这正是它被多条模型线复用的前提。

#### 4.3.2 核心流程

本 crate 与邻居的位置关系（依赖自上而下）：

```
pegainfer-qwen3 / qwen35 / gemma4 / …        ← 模型 crate（自持调度器与请求状态）
        │ 依赖
        ▼
pegainfer-kv-cache                            ← 本讲主角：共享分页 KV 原语
        │ 依赖                    │ 兄弟（不互相依赖）
        ▼                        ▼
kvbm-logical（本地 fork）      pegainfer-kv-offload（host/SSD/RDMA 桥）
  dynamo-kv-hashing（git 依赖）  pegainfer-kernels（CUDA/FlashInfer 编译）
  cudarc / half / tokio / anyhow
```

#### 4.3.3 源码精读

- [Cargo.toml:L1-L17](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L1-L17) — 包声明（edition 2024、Apache-2.0）与 7 个 workspace 依赖。注意 `[lints] workspace = true`：lint 规则也从 workspace 继承，仓库级的整洁约束对每个成员生效。
- [../Cargo.toml:L3-L26](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L3-L26) — workspace 成员表：`pegainfer-kv-cache` 在 L21，兄弟 `pegainfer-kv-offload`（L22）、`pegainfer-kv-store`（L23）紧随其后；L25 的 `kvbm/kvbm-logical` 是从 ai-dynamo/dynamo **本地 fork** 进仓库的块管理器（L24 注释标明 Apache-2.0 溯源）。
- [../Cargo.toml:L41-L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L41-L47) — `kvbm-logical` 走 `path = "kvbm/kvbm-logical"` 本地 fork，而 `dynamo-kv-hashing` 锁定在同一 git rev——注释说明同源依赖必须同 rev，否则内部共享类型（dynamo-tokens）会分裂成两个不同类型。

关于 `pegainfer-kernels` 的诚实说明：它出现在 [Cargo.toml:L13](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L13)，但在 `src/` 与 `tests/` 里检索不到任何 `pegainfer_kernels::` 引用——当前代码没有直接使用它。它是预留的（例如未来视图层直接对接内核类型），本讲不做更多推断，以源码为准。

#### 4.3.4 代码实践

1. **实践目标**：用工具亲自验证依赖关系，而不是背结论。
2. **操作步骤**：
   - 在**仓库根目录**执行 `cargo tree -p pegainfer-kv-cache --depth 1`。
   - 再执行 `grep -rn "pegainfer-kv-cache" --include=Cargo.toml pegainfer-qwen3 pegainfer-qwen35 pegainfer-gemma4 2>/dev/null`，看哪些模型 crate 声明了对本 crate 的依赖。
3. **需要观察的现象**：`cargo tree` 第一层应恰好列出 4.3.1 表中的 7 个依赖；grep 应能在模型 crate 的 `Cargo.toml` 里找到本 crate。
4. **预期结果**：与 4.3.1/4.3.2 的描述一致。若 `cargo tree` 的输出与 7 个依赖不符（workspace 解析可能合并特征），以各自 `Cargo.toml` 为准。本实践的命令输出**待本地验证**（本讲义未在目标环境运行过）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `kvbm-logical` 要 fork 进本仓库（`kvbm/kvbm-logical`），而不是直接用上游发布的版本？

**参考答案**：从 [../Cargo.toml:L24-L26](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L24-L26) 可见两点：fork 允许引擎按自己的需要修改块管理器（上游是 NVIDIA dynamo 项目的通用实现）；同时同 rev 约束（L41-L44 注释）要求 dynamo 系依赖版本统一以保住内部类型一致性，本地 fork 把这层控制权收进仓库。

**练习 2**：如果让 `pegainfer-kv-cache` 反过来依赖 `pegainfer-qwen3`（比如为了「复用 qwen3 的调度器」），会发生什么？

**参考答案**：会形成错误方向的依赖环——qwen3 已经依赖本 crate。更重要的是职责会坍塌：调度策略属于模型 crate（4.1 的三分格局），共享原语必须待在依赖图底层才能被全部模型线复用。这是「机制下沉、策略上浮」的典型分层约束。

## 5. 综合实践

把本讲三块内容串成一个任务（这是本讲的正式实践作业）：

**任务：手绘数据流图 + 公共类型词汇表**

1. **实践目标**：产出一张自己画的数据流图和一张自己写的类型职责表，作为后续十讲的随身参考资料。
2. **操作步骤**：
   - 读 [../CLAUDE.md:L87-L113](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L87-L113)（Architecture 一节）与 [src/lib.rs:L1-L19](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L1-L19)。
   - 在纸上（或任意画图工具）画出：`HTTP 请求 → vLLM frontend → 模型调度器 → BlockPool + KvBuffer → attention 内核 → TokenEvent`，并标注每一步发生在哪个 crate。
   - 为 `lib.rs` 导出的 11 个自有公共类型各写一句话职责（不抄 4.2.3 的表，写完再对照）。
3. **需要观察的现象**：画图时会自然发现两个「交汇点」——调度器与 `BlockPool` 之间传递的是**块 ID**（逻辑），`KvView` 与内核之间传递的是**指针+页表**（物理）。
4. **预期结果**：数据流图与 4.1.2 的图同构；类型职责表与 4.2.3 的表格语义一致（措辞可以不同）。全部纯源码阅读即可完成，无需 GPU、无需编译。

## 6. 本讲小结

- PegaInfer 是纯 Rust + CUDA 的推理引擎；KV state 三分格局：**请求状态在模型调度器、共享分页原语在 `pegainfer-kv-cache`、卸载桥在 `pegainfer-kv-offload`**（`CLAUDE.md` L113）。
- 本 crate 的核心设计是**逻辑/物理两层分离**：`BlockPool` 管「块归谁、能否共享、何时回收」且不持有显存；`KvBuffer` 管连续显存与几何、不自带分配器，两者靠块 ID 关联。
- `src/lib.rs` 只有 19 行：5 个私有模块 + 11 个自有公共类型（pool 贡献 6 个）+ 2 条 `kvbm-logical` 再导出——这张词汇表贯穿后续所有讲义。
- `KvCacheManager` 是全注意力模型的配对门面；MLA 模型（kimi-k2）绕过门面直接消费 `BlockPool`，正是两层分离的价值证明。
- 依赖图自下而上：模型 crate → 本 crate → kvbm-logical（本地 fork）/cudarc 等；`pegainfer-kernels` 目前是声明未用的预留依赖。

## 7. 下一步学习建议

- 下一讲（u1-l2）将动手构建与测试本 crate：区分无需 GPU 的纯逻辑测试（`tests/restore_commit.rs`）与需要 CUDA 的集成测试（`tests/lifecycle.rs`），并核实五个模块的行数分布。
- 想提前建立直觉的读者，可以直接跳读 [src/layout.rs:L18-L33](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L18-L33) 的 `KvLayout::new`——三个步长的推导只用了三行乘法，是 u1-l3 的主角。
- 对 `kvbm-logical` 好奇的读者可以浏览 `kvbm/kvbm-logical/` 目录（workspace 成员），但学习手册会按需引用它，不要求通读。
