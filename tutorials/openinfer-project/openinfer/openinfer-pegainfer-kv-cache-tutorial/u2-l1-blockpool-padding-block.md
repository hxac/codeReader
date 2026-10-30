# BlockPool：逻辑块池与 padding 块

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `BlockPool` 与上游 `kvbm-logical` 的 `BlockManager` 之间的**包装关系**：谁管逻辑、谁管显存、为什么要包一层。
2. 逐步复述 `BlockPool::build` 构造链路中 **padding 块的完整预留过程**——`allocate_blocks(1)` → `stage(SequenceHash::default(), block_size)` → `register_block` + `std::mem::forget`——以及每一步之后块处于什么状态。
3. 解释 padding 块**为什么必须与引擎同寿、绝不能被回收入空闲池**（CUDA Graph 的页表固化）。
4. 掌握容量查询 API（`block_size` / `available_blocks` / `total_blocks` / `padding_block_id` / `max_request_blocks`）的语义，以及「至少 2 块」的构造约束。

本讲是进阶层第一讲，只聚焦**池本身的构造与容量**；请求如何从池里拿块、如何归还，是下一讲（u2-l2 `RequestKv` 生命周期）的内容。

## 2. 前置知识

### 2.1 块（block / page）与 block_size

回顾 u1-l3：KV 缓存不是按请求连续分配的，而是切成**固定大小的块**（也叫页）。`block_size` 是每块容纳的 token 数（不是字节数）。例如 `block_size = 16` 表示一个块存放 16 个 token 在所有层上的 K/V。池以块为最小单位发放，消费者（各模型 crate）拿到**块 ID** 后自己去物理显存里索引对应区域。

### 2.2 kvbm-logical 是什么

`kvbm-logical` 是本仓库内的一个本地 fork 依赖（fork 自 ai-dynamo 的 kvbm，Apache-2.0），见根 [Cargo.toml:L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L47) 的 `kvbm-logical = { path = "kvbm/kvbm-logical" }`，本 crate 经 workspace 继承引入：[Cargo.toml:L12](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L12)。它提供了块管理器的**通用逻辑内核**：`BlockManager`（池与分配）、`MutableBlock` / `ImmutableBlock`（块的两态）、`SchedulableSequence`（每请求序列状态）、`BlockRegistry`（注册表与事件）等。`pegainfer-kv-cache` 不重复造这套轮子，而是在其上包一层引擎适配。

### 2.3 Rust 的 RAII 与 `std::mem::forget`

Rust 靠 RAII 管资源：值离开作用域时自动运行 `Drop::drop`，资源随之释放。`ImmutableBlock` 就是一个 RAII 守卫——**持有它 = 钉住这个块**；守卫被 drop，块才回到池。而 [`std::mem::forget`](https://doc.rust-lang.org/std/mem/fn.forget.html) 故意**跳过 Drop**，把值「泄漏」掉。听起来危险，但对「应当与程序同寿、永不回收」的资源，这正是最明确的表达方式。本讲的 padding 块就用它。

### 2.4 CUDA Graph 为什么需要一个「哑块」

CLAUDE.md 的架构一节说明：解码路径被捕获成 CUDA Graph，用预分配缓冲保证指针稳定。捕获一旦完成，图里内核启动的形状（批大小、页表行数）就**固化**了。运行时若实际请求的页表行数少于捕获形状的固定行数，多出来的槽位需要填一个「永远合法」的页号——即 padding 块。仓库根 CLAUDE.md「EP Free-Running Discipline」一节的原则「Padding rows are protocol surface（哑行也是协议表面）」在 KV 层的体现就是这块永远预留的页。

> 说明：源码注释本身只写了「CUDA Graph padding slot」（见下方源码精读）；上面对捕获形状固定→需要哑页的机制解释，是结合 CLAUDE.md CUDA Graph 段落的设计动机还原，属于通行原理而非该注释原文。

### 2.5 `anyhow::ensure!`

`ensure!(cond, "msg")`：条件不成立时立刻返回 `Err`，消息进入错误链。`BlockPool::new` 返回 `anyhow::Result<Self>`，构造期的不变式检查全靠它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [src/pool.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L26-L123) | 逻辑块池与每请求 KV 状态，本 crate 的核心（约占 src 七成行数） | `BlockPool` 结构、`build` 构造链路、容量 API |
| [tests/restore_commit.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L1-L5) | 纯逻辑集成测试（自述「no GPU」），兼作 #704 性能回归锚 | 实践任务的风格范本 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L12) | 公共导出 | `pub use pool::BlockPool`（池是公共 API） |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L12)（crate）与根 [Cargo.toml:L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L47) | 依赖声明 | `kvbm-logical` 的本地 fork 来源 |

## 4. 核心概念与源码讲解

### 4.1 BlockPool 与 BlockManager 的包装关系

#### 4.1.1 概念说明

`BlockPool` 的文档注释一句话定位：**「Logical KV block pool: a `BlockManager` plus the reserved padding block」**——一个逻辑块池 = 上游块管理器 + 一个被预留的 padding 块。

它解决的问题是「职责分层」：

- `BlockManager`（来自 kvbm-logical）负责通用块逻辑：空闲池、活跃/非活跃（LRU 缓存）池、分配、注册、匹配。
- `BlockPool` 在其上做三件引擎适配的事：
  1. 构造时**预留 padding 块**（上游不管这件事）；
  2. 暴露一套面向引擎的**容量查询 API**；
  3. 后续讲义会看到，它还承载前缀探测（`probe_prefix`）、恢复提交（`reserve/commit_loaded_blocks`）、事件通道（`with_events`）等引擎级能力。

关键澄清（承接 u1-l1 的两层分离）：**`BlockPool` 自己不持有任何显存**。文档注释原话是 "Owns no GPU memory — the physical layout ... lives with the consumer and is indexed by the block IDs this pool hands out"——物理显存在消费者一侧（全注意力模型的 `KvBuffer`、MLA 模型的双缓冲等），本池发放的**块 ID** 就是索引物理页的页号。

#### 4.1.2 核心流程

```text
各模型 crate 的调度器
        │  new_request / schedule / release …（u2-l2 详述）
        ▼
BlockPool（本 crate，src/pool.rs）
        │  内部持有
        ▼
BlockManager（kvbm-logical）
   ├─ 空闲池 free pool ── available_blocks() 计数来源
   ├─ 活跃池 active ──── 被请求/守卫持有的块
   └─ 非活跃池 inactive ─ LRU 前缀缓存（with_lru_backend）
        │  块 ID（= 物理页号）
        ▼
KvBuffer 等物理显存（消费者一侧，按块 ID 索引）
```

#### 4.1.3 源码精读

**结构体定义**——三个字段，一个小结构件包住上游管理器：

- [src/pool.rs:L26-L35](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L26-L35)：`BlockPool` 的文档注释与字段。`block_manager: BlockManager<()>` 是泛型参数为 `()` 的上游管理器（`()` 表示块的附加载荷为空——本引擎不在块上挂额外数据）；`block_size` 记住每块 token 数；`padding_block_id` 记住预留块的编号。

**公共入口**——[src/pool.rs:L37-L40](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L37-L40)：`new(block_size, num_blocks)` 只是给 `build` 传一个「无事件」的默认注册表。旁边还有一个 `pub(crate)` 的 [`with_events`](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L50-L64)（给缓存感知路由广播块事件，默认关闭），本讲不展开，留到 u3-l2。

**上游依赖面**——[src/pool.rs:L4-L15](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L4-L15)：`use` 区一口气从 `kvbm_logical` 导入 12 个类型，这正是「包装关系」的直观证据。按用途分四类：

| 类别 | 类型 | 用途 |
|------|------|------|
| 哈希 | `SequenceHash`、`KvbmSequenceHashProvider` | 块的血缘哈希及访问器（如 `all_sequence_hashes`） |
| 块 | `blocks::MutableBlock`、`blocks::ImmutableBlock` | 块的两态：可写 / 已封存（RAII 守卫） |
| 管理器 | `manager::BlockManager`、`pools::BlockDuplicationPolicy`、`registry::BlockRegistry` | 池本体、复制策略、注册表 |
| 序列与事件 | `integrations::SchedulableSequence` 等、`events::*` | 每请求状态机、可选事件流 |

**公共导出**——[src/lib.rs:L12](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L12)：`pub use pool::BlockPool;`。池是本 crate 公共 API 的一部分，模型 crate 直接消费它（MLA 线甚至绕过 `KvCacheManager` 门面直用 `BlockPool`，见 u2-l4）。

#### 4.1.4 代码实践

1. **实践目标**：通过读 `use` 区，亲手确认「BlockPool 的一切块操作都委托给 kvbm-logical」。
2. **操作步骤**：打开 `src/pool.rs` 的 L4-L15，把 12 个 `kvbm_logical` 导入按上表四类抄写一遍；然后在文件里用编辑器的「查找用法」分别搜 `block_manager.`，数一数 `BlockPool` 的方法里有多少处是对 `self.block_manager` 的一行转发（如 `available_blocks`、`total_blocks`、`evict_inactive`）。
3. **需要观察的现象**：绝大多数 `BlockPool` 公共方法体只有一行 `self.block_manager.xxx(...)`；真正多出来的逻辑只有构造期的 padding 预留和错误包装（`map_err` 转 `anyhow`）。
4. **预期结果**：你会得到「薄包装 + 构造期扩展」的清晰印象——这正是设计意图：通用逻辑留在 kvbm-logical，引擎语义留在这里。

#### 4.1.5 小练习与答案

**练习 1**：`BlockPool` 的泛型写法是 `BlockManager<()>`，这个 `()` 是什么意思？

**答案**：它是上游块管理器给每块附加的用户载荷类型。本引擎选择 `()`（空载荷）——块上不需要挂任何引擎侧数据，块的身份完全由块 ID 和哈希表达。

**练习 2**：如果 `BlockPool` 直接把 `BlockManager` 重新导出（`pub use kvbm_logical::manager::BlockManager`）给模型 crate 用，会失去什么？

**答案**：失去构造期的 padding 预留保障（任何人自己建 `BlockManager` 就没有哑块，CUDA Graph 捕获会踩到非法页号）；失去统一的容量语义（`available == total - 1`）；也失去 `anyhow` 错误类型的统一包装。包装层存在的意义就是把上游能力「收紧」成引擎不变式。

### 4.2 构造链路：`build` 与 padding 块的预留

#### 4.2.1 概念说明

`BlockPool::build` 是本讲的核心函数。它做四件事：

1. **校验**：`num_blocks >= 2`——至少要有一块给 padding、一块给真实请求。
2. **建管理器**：用 builder 模式配置 `BlockManager`：块数、块大小、注册表、**允许块复制**、**LRU 后端**。
3. **预留 padding**：从新鲜的管理器里分配第一个块，把它「封存 + 注册 + 泄漏」，使其永远脱离流通。
4. **组装返回**。

两个 builder 参数值得先解释：

- `BlockDuplicationPolicy::Allow`：允许同一个已注册块被多个请求「复制持有」。这是前缀缓存的前提——第二个请求命中缓存时拿到的不是新物理块，而是指向同一已注册块的引用（duplicate）。
- `with_lru_backend()`：请求释放已注册块时，块不是直接回空闲池，而是进入 LRU 管理的非活跃（inactive）缓存，仍可被后续请求的前缀匹配命中；内存吃紧时才按 LRU 逐出。这就是「前缀缓存」在池层的落点（详见 u2-l5）。

而 padding 块的预留，本质是在这两个机制**生效之前**，先从流通中永久摘走一块。

#### 4.2.2 核心流程

`build` 的伪代码与块状态变迁：

```text
build(block_size, num_blocks, registry):
  ensure num_blocks >= 2                      # 至少 2 块
  manager = BlockManager::builder()
      .block_count(num_blocks)                # 总块数
      .block_size(block_size)                 # 每块 token 数
      .registry(registry)                     # 注册表（可带事件管理器）
      .duplication_policy(Allow)              # 允许块复制 → 前缀缓存前提
      .with_lru_backend()                     # 释放的注册块进 LRU 缓存
      .build()
  padding = manager.allocate_blocks(1)        # 取一块可写块（注释：块 0）
  padding_id = padding.block_id()
  complete = padding.stage(默认哈希, block_size)  # 封存为"满块"
  std::mem::forget(manager.register_block(complete))  # 注册后立即泄漏守卫
  return BlockPool { manager, block_size, padding_id }
```

padding 块在每一步之后的状态：

| 步骤 | 操作 | 块的状态 | 能否被再分配 / 匹配 |
|------|------|----------|--------------------|
| 0 | 构造 `BlockManager` | 在空闲池 | 可分配 |
| 1 | `allocate_blocks(1)` | 可写块（`MutableBlock`），离开空闲池 | 不可再分配 |
| 2 | `stage(default, block_size)` | 封存为完整块（写满 `block_size` 个 token、哈希为默认值） | 不可再分配 |
| 3 | `register_block(...)` | 注册进注册表，处于活跃池，被返回的 `ImmutableBlock` 守卫钉住 | 不可再分配；哈希是默认值，不会被真实序列匹配 |
| 4 | `std::mem::forget(守卫)` | 守卫被泄漏，**永不 Drop** | 永久不可再分配、不可逐出、不可匹配 |

#### 4.2.3 源码精读

**构造下限校验**——[src/pool.rs:L71](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L71)：

```rust
anyhow::ensure!(num_blocks >= 2, "need at least 2 blocks (1 for padding)");
```

错误消息直接写明动机：1 块给 padding，剩下的至少还得有 1 块能发给真实请求。`num_blocks == 1` 时构造直接返回 `Err`。

**管理器构建**——[src/pool.rs:L73-L80](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L73-L80)：按上一节解释的五个 builder 参数链出 `BlockManager`；`.build()` 的上游错误被 `map_err` 包成 `anyhow::Error`（"BlockManager build failed"）。

**padding 预留三连**——[src/pool.rs:L82-L95](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L82-L95)，这是本讲最值得逐行读的片段：

```rust
// Reserve block 0 as CUDA Graph padding slot.
let padding_blocks = block_manager.allocate_blocks(1)...;
let padding_block_id = padding_blocks[0].block_id();
let padding_complete = padding_blocks...stage(SequenceHash::default(), block_size)...;
// Register so it stays alive (ImmutableBlock RAII keeps it out of the
// free pool), then leak — padding lives for the lifetime of the engine.
std::mem::forget(block_manager.register_block(padding_complete));
```

- 注释 `Reserve block 0 as CUDA Graph padding slot` 点明用途：因为这是全新管理器的**第一次分配**，拿到的就是块 0；它将作为 CUDA Graph 的 padding 槽。
- `stage(SequenceHash::default(), block_size)` 把可写块封存为「写满 `block_size` 个 token、内容哈希为默认值」的完整块。用**默认哈希**是刻意为之：真实块的哈希由 token 内容按血缘链计算（u3-l1 详述），默认值不对应任何真实序列，因此 padding 块**永远不会被前缀匹配命中**——注册它是为了「活着」，不是为了「被复用」。
- `register_block` 返回的 `ImmutableBlock` 是 RAII 守卫：注释写得很清楚，注册让它保持存活（守卫使它留在活跃池、不进空闲池）。如果守卫随后被正常 drop，块会被释放归还；`std::mem::forget` 跳过 Drop——**故意的、一次性的、与引擎同寿的泄漏**。代价仅是一个块 ID 和一条注册表项，收益是这块永远安全。

**组装返回**——[src/pool.rs:L97-L101](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L97-L101)：把管理器、`block_size`、`padding_block_id` 装进 `BlockPool`。此后除了只读的 `padding_block_id()`，这个编号不再参与任何流通。

#### 4.2.4 代码实践

1. **实践目标**：把 4.2.2 的状态变迁表「跑」一遍眼动验证，理解 forget 的作用。
2. **操作步骤**：
   - 打开 [src/pool.rs:L82-L95](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L82-L95)，对照状态表逐步注释每一行；
   - 再打开同文件里 `commit_loaded_blocks`（[src/pool.rs:L248-L257](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L248-L257)），对比它对「恢复块」执行的 `stage → register` 两连——注意它**没有** `forget`，因为守卫被存进 `probe.held` 向量，由探测器持有到请求预填为止。
3. **需要观察的现象**：同是 `stage + register`，padding 路径以 forget 收尾（永久），恢复路径以存进 `held` 收尾（临时）。
4. **预期结果**：你能说出两条路径各自「谁在什么时候 drop 守卫」——padding 永远没人 drop；恢复块的守卫在探测器 drop 时统一释放。
5. 本实践为纯阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `std::mem::forget(...)` 删掉（`register_block` 的返回值直接丢弃），会发生什么？

**答案**：返回的 `ImmutableBlock` 守卫立即被 drop，块随即被释放回空闲池。此后 `available_blocks()` 会把块 0 计入可用数，第一个构造请求就可能拿到块 0 当真实 KV 页；而 CUDA Graph 捕获的页表若仍把哑槽位指向块 0，回放时就会读写该请求的 KV——静默数据损坏。这正是那条注释「Register so it stays alive ... then leak」防住的事故。

**练习 2**：为什么 padding 块的 `stage` 要传 `SequenceHash::default()`，而不是随便编一个哈希？

**答案**：`stage` 的哈希参数决定块注册后「以什么身份」进入注册表。默认哈希不等于任何真实 token 序列的血缘哈希，因此不会有任何请求的前缀匹配命中它；随便编的固定哈希在语义上同样是「不可命中」，但 `default()` 明确表达了「这块没有内容身份」——它是哑块，不是缓存块。

**练习 3**：`with_lru_backend()` 与 padding 预留的先后关系重要吗？

**答案**：重要。padding 是在 LRU 机制启用后的第一次分配，但它在进入 LRU 流通之前就被注册 + 泄漏钉死在活跃池——所以它既不会被 LRU 逐出（`evict_inactive` 只排非活跃池，见 [src/pool.rs:L125-L131](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L125-L131) 的文档「In-use blocks are untouched」），也不会回到空闲池。

### 4.3 容量查询 API 与「至少 2 块」约束

#### 4.3.1 概念说明

`BlockPool` 暴露五个容量相关访问器，语义如下：

| API | 语义 | 构造后（无请求时）的值 |
|-----|------|----------------------|
| `block_size()` | 每块容纳的 token 数 | 等于构造入参 |
| `total_blocks()` | 池的总块数（含 padding） | 等于构造入参 `num_blocks` |
| `available_blocks()` | 当前空闲可分配块数（不含活跃/非活跃/padding） | `total - 1` |
| `padding_block_id()` | 预留哑块的编号，返回 `i32` | 通常为 0（见注释 "block 0"） |
| `max_request_blocks()` | 单个请求最多可占用的块数 | `total - 1`（`saturating_sub`） |

两条不变式贯穿始终：

\[ \text{available\_blocks} \;=\; \text{total\_blocks} - 1 \quad (\text{构造后、无请求时}) \]

\[ \text{max\_request\_blocks} \;=\; \text{total\_blocks} - 1 \]

减掉的 1 就是 padding。注意 `padding_block_id()` 返回 `i32` 而非 `usize`——消费侧的页表（如 `RequestKv` 的 `page_indices`）元素类型就是 `i32`，哑页号要能直接填进页表。

#### 4.3.2 核心流程

```text
BlockPool::new(16, 64)
  └─ build:
       ensure 64 >= 2 ✓
       manager: 64 块 × 16 token/块，Allow 复制，LRU 后端
       allocate 1 → padding(块 0)，stage → register → forget
  └─ 结果:
       total_blocks()      = 64
       available_blocks()  = 63      ← 1 块被永久摘走
       max_request_blocks()= 63
       padding_block_id()  = 0
```

运行期：请求调度分配块 → `available_blocks()` 下降；请求释放后已注册块先进 LRU 非活跃缓存（仍不可分配，但可被匹配），被逐出或按复位语义归还后才回到可用数。容量如何随生命周期涨落，是 u2-l2 的主线。

#### 4.3.3 源码精读

**五个访问器**——[src/pool.rs:L104-L123](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L104-L123)：`block_size` / `available_blocks` / `total_blocks` 三个是对 `block_manager` 的一行转发；`padding_block_id` 把存的 `usize` 转成 `i32` 返回；`max_request_blocks` 的文档注释写明 "Maximum blocks a single request can consume (total minus padding)"，实现是 `total_blocks().saturating_sub(1)`。

**交叉证据：issue #681 的测试**——[src/pool.rs:L934-L950](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L934-L950)：

```rust
// BlockPool reserves one additional CUDA-graph padding page.
let pool = BlockPool::new(16, 161).unwrap();
assert_eq!(pool.max_request_blocks(), 160);
let baseline = pool.available_blocks();
```

注意参数顺序是 `new(block_size, num_blocks)`：16 token/块、161 块。测试注释再次确认「额外预留一页 CUDA Graph padding」，并断言 `max_request_blocks() == 160`；`baseline` 捕获构造后的可用数（即 160），后续两轮完整请求都断言可用数回到 `baseline`——这是「请求必须还清 KV 页」这一容量不变式的回归守卫（对应 issue #681）。

**测试范本的模块注释**——[tests/restore_commit.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L1-L5)：开头即声明 "Pure logical-pool tests, no GPU"——只用 `BlockPool` 的公共 API，不需要 CUDA 设备，这就是下面实践任务选择仿照它的原因。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：用纯逻辑测试亲手验证 padding 预留与构造约束。
2. **操作步骤**：在本 crate 的 `tests/` 目录下**新建**文件 `tests/padding_block.rs`（不改任何现有文件）：

   ```rust
   //! padding 块与容量语义的纯逻辑测试（无 GPU）。
   //! 风格仿照 tests/restore_commit.rs：只依赖 BlockPool 的公共 API。

   use pegainfer_kv_cache::BlockPool;

   #[test]
   fn padding_block_is_reserved_and_single_block_pool_is_refused() {
       // 64 块的池：1 块被 padding 永久占用，available = 63。
       let pool = BlockPool::new(16, 64).expect("pool");
       assert_eq!(pool.total_blocks(), 64);
       assert_eq!(pool.available_blocks(), 63);
       assert_eq!(pool.max_request_blocks(), 63);

       // num_blocks < 2 直接拒绝：至少留 1 块给真实请求。
       let err = BlockPool::new(16, 1).expect_err("single-block pool must be rejected");
       assert!(
           err.to_string().contains("at least 2 blocks"),
           "unexpected error: {err}"
       );
   }

   // padding 块绝不能被回收入空闲池，原因有三：
   // 1. CUDA Graph 捕获把页表内容固化进图，空批次槽位的页表行指向
   //    padding 页；若该页被回收并发给真实请求，图回放时哑行会
   //    读写那个请求的 KV —— 静默数据损坏。
   // 2. available == total - 1 与 max_request_blocks 的容量语义
   //    都建立在「这一块永远不可用」之上。
   // 3. 构造用 std::mem::forget 泄漏了注册守卫，代码路径上根本
   //    不存在「归还 padding」的入口。
   ```

   然后运行：

   ```bash
   cargo test --release -p pegainfer-kv-cache --test padding_block
   ```

3. **需要观察的现象**：两个断言块全部通过；错误信息一栏能看到 `need at least 2 blocks (1 for padding)`。
4. **预期结果**：`available_blocks() == 63`、`max_request_blocks() == 63`、`BlockPool::new(16, 1)` 返回 `Err`。这些数值直接由源码推导（L71 的 `ensure`、L83 的 `allocate_blocks(1)`、L121-L123 的 `saturating_sub`），并与 issue #681 测试的 161 → 160 模式一致；本讲义写作环境未运行 cargo，运行输出**待本地验证**。
5. 常见坑：把参数顺序记反（`new(num_blocks, block_size)`）会让 63 变成别的数——第一个参数是**每块 token 数**。

#### 4.3.5 小练习与答案

**练习 1**：`BlockPool::new(16, 2)` 能构造成功吗？成功后 `available_blocks()` 是多少？

**答案**：能——`num_blocks = 2` 恰好满足 `>= 2`。构造后 `available_blocks() == 1`、`max_request_blocks() == 1`：除 padding 外只剩一块，任何超过 16 个 KV token 的请求都无法在这个池里完成。

**练习 2**：为什么 `max_request_blocks()` 用 `saturating_sub(1)` 而不是普通减法？

**答案**：防御式写法。`total_blocks()` 至少为 2（构造已保证），普通减法在此不会下溢；`saturating_sub` 保证将来即使有人绕过 `new` 用更小的池构造（例如上游 API 变化），也是收敛到 0 而不是 panic 或回绕成天文数字。

**练习 3**：请求全部释放后，`available_blocks()` 一定立刻回到 `total - 1` 吗？

**答案**：不一定。已注册的完整块在请求释放后进入 LRU 非活跃缓存（`with_lru_backend` 的作用），它们不在 `available` 里但可被前缀匹配复用；要回到 `total - 1` 需要显式 `evict_inactive()`，或像 issue #681 测试那样在释放前调用 `mark_blocks_reset_on_release()` 让块复位归还。这条语义是 u2-l2 / u2-l5 的伏笔。

## 5. 综合实践

把本讲三个模块串起来：构造不变式 → padding 的稳定性 → 一次完整请求循环前后的容量涨落。继续在 `tests/padding_block.rs` 里追加：

```rust
//! 综合实践：padding 块在一次请求循环中保持不可见、不可回收。
use pegainfer_kv_cache::BlockPool;

#[test]
fn padding_survives_a_full_request_cycle() {
    let pool = BlockPool::new(16, 64).expect("pool");
    let baseline = pool.available_blocks(); // 63：padding 永不参与发放
    assert_eq!(baseline, 63);

    // 35 token 的 prompt：ceil(35/16) = 3 个块。
    let prompt: Vec<u32> = (0..35).collect();
    let mut req = pool.new_request(prompt, 1, None);
    req.schedule_prefill(35, &pool).expect("schedule");
    assert!(pool.available_blocks() < baseline, "调度应消耗空闲块");

    req.apply_prefill(7, &pool).expect("apply");
    req.release().expect("release");

    // 已注册的完整块此时躺在 LRU 非活跃缓存里；排空它，
    // 可用数应精确回到 baseline —— padding 从未混入流通。
    pool.evict_inactive();
    assert_eq!(pool.available_blocks(), baseline);
}
```

- **目标**：验证「padding 永远不可见」——无论请求如何进出，可用容量的下界与上界都把那 1 块排除在外。
- **观察重点**：`schedule` 之后可用数下降多少（35 token 理论上占 3 块，但 kvbm 对生成块有预分配行为，精确数字**待本地验证**）；`release` 后、`evict_inactive()` 前后可用数的差值——它就是留在 LRU 缓存里的完整块数。
- **对照阅读**：[src/pool.rs:L934-L950](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L934-L950) 的 issue #681 测试做的是同一件事的复杂版（1643 token、分块预填、投机解码、两轮循环、复位语义）。
- 运行：`cargo test --release -p pegainfer-kv-cache --test padding_block`。若环境没有 Rust 工具链或依赖未拉取，先 `cargo build --release -p pegainfer-kv-cache`；运行输出**待本地验证**。

## 6. 本讲小结

- `BlockPool` 是 `kvbm-logical` 的 `BlockManager` 之上的**薄包装**：通用块逻辑在上游，引擎语义（padding 预留、容量口径、前缀探测/恢复/事件）在包装层；它自己不持有任何显存，只发放块 ID。
- 构造链路 `build`：`ensure(num_blocks >= 2)` → builder 五参数（`block_count` / `block_size` / `registry` / `BlockDuplicationPolicy::Allow` / `with_lru_backend`）→ 预留 padding → 组装。
- padding 预留三连：`allocate_blocks(1)`（拿到块 0）→ `stage(SequenceHash::default(), block_size)`（封存为默认哈希的满块，永不参与前缀匹配）→ `register_block` + `std::mem::forget`（注册守卫被故意泄漏，块与引擎同寿）。
- padding 必须永不回收：CUDA Graph 把哑页号固化进捕获的页表，复用该页会导致图回放时静默读写真实请求的 KV。
- 容量不变式：构造后 `available == total - 1`，`max_request_blocks == total - 1`，减的都是 padding；`padding_block_id()` 以 `i32` 暴露，供直接填入页表。
- 释放的已注册块进 LRU 非活跃缓存而非立即归还——这是前缀缓存（u2-l5）的池层基础，也让 `available` 的涨落比「释放即回升」更微妙。

## 7. 下一步学习建议

下一讲 **u2-l2（RequestKv：一个请求的 KV 生命周期）**沿着本讲铺好的池往下走：`new_request → schedule_prefill → prefill_view → apply_prefill → schedule_decode → decode_view → apply_decode → release` 的完整状态机，以及 schedule 与 apply 两阶段分离带来的失败恢复（drop 守卫归还块、`revert_schedule`）。建议先读 `tests/lifecycle.rs` 的 `single_request_prefill_decode_release`，带着「每一步之后 `available_blocks()` 是多少」的问题去读——那正是本讲综合实践的放大版。之后再进入 u2-l3（KvView 与注意力内核契约）。
