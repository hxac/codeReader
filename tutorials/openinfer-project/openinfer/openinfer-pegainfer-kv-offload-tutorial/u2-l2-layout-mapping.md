# u2-l2 GPU 布局映射：从 page-first KvBuffer 到 per-layer strided 注册

## 1. 本讲目标

本讲精读 `pegainfer-kv-offload/src/layout.rs`——整个 crate 里数学密度最高的 100 行。学完后你应该能够：

1. 给定 `(num_layers, kv_heads, head_dim, page_size, num_blocks)`，**手算**出 `kv_block_len / layer_stride / page_stride`（元素数）与 `layer_bytes / page_stride_bytes`（字节数），进而写出每个 layer arena 的 `base_ptr` 偏移、`bytes_per_block`、`block_stride_bytes` 与整个 `Registration` 的 `size_bytes`。
2. 解释 `KvArena` 这个抽象如何用**同一套五个字段**统一两种截然不同的显存形态：qwen3 的融合（fused）布局与 GLM5.2 的 MLA latent + index-K sidecar 布局。
3. 推导 `size_bytes = (num_blocks − 1) × block_stride + bytes_per_block` 为什么是「最后一块的到达界」，而 `num_blocks × stride` 会谎报一段根本不属于本 arena 的尾巴。

本讲不涉及任何 I/O——`layout.rs` 是纯几何，全部逻辑可以在脑子里、纸上跑通。这是它适合作为进阶层第二讲的原因：把地址算对，后面 save/load 的每一步才有意义。

## 2. 前置知识

承接 u1-l2（KV 卸载心智模型）与 u2-l1（配置层）。本讲需要以下几个概念，先用人话过一遍：

- **块（block）与页（page）**：pegainfer 把 KV cache 按 `page_size`（通常 16）个 token 一组切成块。块是搬运单位——save 存的是块，load 恢复的也是块，块的内容寻址键（哈希链）在 u1-l2 讲过。
- **bf16**：brain float 16，2 字节浮点。KV cache 的每个元素是 bf16，所以 `std::mem::size_of::<half::bf16>() == 2`。`pegainfer-kv-cache` 的 `KvLayout` 里所有 stride 都以**元素数**计；要换算成字节必须 ×2。这是本讲最容易算错的一步。
- **stride（步长）与 size（大小）**：一段内存里「相邻两块起点之间的距离」叫 stride，「一块实际占多少字节」叫 copy size / bytes_per_block。GPU 拷贝原语（`cuMemcpyAsync`）只吃**连续区间**；当数据不连续时，就必须告诉底层「每次拷多少、下一块隔多远」——这两个数解耦，正是 `block_stride_bytes` 与 `bytes_per_block` 分开存在的原因。
- **融合（fused）布局**：所有层的 KV 挤在一整块显存分配里，页内按层交错（详见 4.2）。
- **sidecar（边车）布局**：主 KV 之外，每层还有独立分配的辅助状态（GLM5.2 的 index-K），与主 KV **共享同一个块号池**——块号 `b` 同时索引主 KV 的第 `b` 块和 sidecar 的第 `b` 块。
- **注册（registration）**：把 GPU 显存里「哪些区间属于哪些层、块怎么排布」告诉 pegaflow 的一次性动作。注册之后 save/load 都不再传地址，只传 `(层名, 块号)`——地址几何被「烤」进注册表。为什么地址可以烤进去？因为 cudarc 的设备分配终身不搬家（u2-l4 展开）。

一句话定位本讲的文件：`layout.rs` 就是「翻译官」——把 `pegainfer-kv-cache` 的 page-first 几何翻译成 pegaflow 要求的 per-layer strided 注册表。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
|---|---|---|
| `pegainfer-kv-offload/src/layout.rs` | **本讲主角**。`KvArena` 抽象 + `Registration` 两个构造器 + 三个单测 | 全文 |
| `pegainfer-kv-cache/src/layout.rs` | `KvLayout` 纯值类型：page-first 几何的四个维度与三个导出 stride（元素数） | `KvLayout::new` 的三行推导 |
| `pegainfer-kv-cache/src/buffer.rs` | `KvBuffer`：拥有设备内存与几何，不含分配器 | `new`（总元素数）、`device_ptr`（基址稳定性）、`layout()`/`num_blocks()` |
| `pegainfer-kv-offload/src/engine.rs` | `Registration` 的唯一消费者 | `new`/`with_arenas_on` 两条构造路径、`register` 把字段逐个喂给 pegaflow 的批量注册调用 |
| `pegainfer-kv-offload/tests/cpu_roundtrip.rs` | 端到端 roundtrip 集成测试（u1-l3、u3-l4 展开） | 开头的几何常量与「对账断言」——独立复算了一遍本讲的公式 |

## 4. 核心概念与源码讲解

### 4.1 KvArena：一条「等大拷贝单元序列」的抽象

#### 4.1.1 概念说明

pegaflow 眼里没有「KV cache」，只有**一条条 arena**：从 `base_ptr` 开始，`num_blocks` 个**等大的拷贝单元**，相邻单元起点相距 `block_stride_bytes`。每个拷贝单元 `bytes_per_block` 字节。这就是 `KvArena` 的全部：

```rust
pub struct KvArena {
    name: String,              // 引擎生命周期内save/load 扇出的键
    base_ptr: u64,             // arena 起点的裸设备地址
    num_blocks: usize,         // 拷贝单元个数（= 池块数）
    bytes_per_block: usize,    // 一个拷贝单元的字节数（copy size）
    block_stride_bytes: usize, // 相邻单元起点的字节距离（stride）
}
```

（引自 [layout.rs:L21-L27](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L21-L27)，字段注释为讲义所加。）

为什么这五个字段就够了？因为「`num_blocks` 个等大单元、固定步长排布」恰好同时刻画了两种真实形态：

- **融合布局（qwen3）**：某层的第 `b` 块被其他层的数据隔开，`stride > copy`；
- **sidecar / 独立分配（GLM5.2）**：块与块首尾相接，`stride == copy`。

抽象的威力在于：pegaflow 完全不需要知道你是什么模型、什么注意力机制，它只会搬「注册在同一个块号下的所有 arena」——文档注释说得很直白：

> A fused buffer (qwen3) contributes one arena per model layer; a model with sidecar caches (GLM5.2: MLA latent + index-K per layer, two separate allocations sharing pool block ids) contributes several arenas per model layer — pegaflow moves whatever arenas are registered under one block id together, which is what keeps sidecars in lockstep with their main cache.

（[layout.rs:L10-L20](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L10-L20)：这条注释是本讲的纲领——**「注册在同一个块号下的 arena 一起搬」就是 sidecar 与主 KV 保持同步的全部机制**，没有任何额外的锁或协议。）

`KvArena` 有三条契约，注意它们**强制方式不同**：

| 契约 | 内容 | 强制方式 |
|---|---|---|
| C1 拷贝不越步长 | `bytes_per_block ≤ block_stride_bytes` | **机器检查**：`from_arenas` 里的 `assert!`（4.3 节） |
| C2 名字唯一 | `name` 在引擎生命周期内唯一（save/load 按名字扇出到所有注册层） | **文档契约**：注释要求调用方保证，无断言 |
| C3 指针稳定 + 共享块号 | arena 的设备分配终身不搬家；所有 arena 用同一套池块号索引 | **文档契约**：写在 `with_arenas_on` 的文档里（u2-l4 展开） |

C2/C3 为什么不做机器检查？因为它们的成立范围（「引擎生命周期」「同一引擎的所有注册」）超出了 `from_arenas` 单次调用的视野——一次调用看不见别的调用。这是 Rust API 设计里很典型的一课：**构造函数只能断言局部不变量，跨调用不变量只能写成文档**。

#### 4.1.2 核心流程

一个 arena 的逻辑图：

```text
base_ptr ──► ┌────────┐   ←── block_stride_bytes ──→ ┌────────┐      ┌────────┐
             │ block 0│                              │ block 1│ ⋯    │ block N-1│
             │ C 字节  │                              │ C 字节  │      │ C 字节   │
             └────────┘                              └────────┘      └────────┘
             ├──────────── S 字节 ──────────────────────────────────────┤
             每块起点：base_ptr + b × S          （b = 0..N-1）
             每块拷贝：[base_ptr + b×S, base_ptr + b×S + C)
```

- 拷第 `b` 块 = 从 `base_ptr + b·S` 读/写连续 `C` 字节；
- `S == C`（dense）：块首尾相接，arena 就是一整条连续内存；
- `S > C`（strided）：块之间夹着「别人的数据」（融合布局里是其他层），拷贝时必须跳过；
- `C > S`：非法——下一个块的开头落在本块数据内部，重叠拷贝，`assert!` 拒绝。

#### 4.1.3 源码精读

`ELEM_SIZE` 是全文件唯一的常量——元素到字节的换算系数：

- [layout.rs:L7-L8](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L7-L8)：`bf16 KV cache: every layout stride is counted in elements, bytes are ×2.` 定义 `ELEM_SIZE = size_of::<half::bf16>()`。上游 `KvLayout` 的三个 stride 全是元素数，本 crate 边界处统一 ×2 换成字节——**换算只发生在这里一次**，下游（pegaflow 注册、拷贝）全用字节。

`KvArena` 结构体本体见 4.1.1 的引用（[layout.rs:L21-L27](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L21-L27)），文档注释见（[layout.rs:L10-L20](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L10-L20)）。

`Registration` 是 `KvArena` 列表「摊平」后喂给 pegaflow 一次批量调用的形态：

- [layout.rs:L29-L43](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L29-L43)：定义 `Registration`，八个字段全是 `Vec`——pegaflow 的 `register_context_layer_batch_strided` 只收逐层向量，所以即使是全同的标量也得复制成向量。注释里说「只有 `data_ptrs` 和 `size_bytes` 逐层不同，其余是同一标量的广播」——注意这句话描述的是**融合布局**的情形；sidecar 情形下连 `bytes_per_block` 都会逐层不同（GLM5.2 的 MLA 与 index-K 拷贝单元不同大），反正 API 收的就是向量。

最后看消费侧长什么样（细节留给 u2-l4，这里只看形状）：

- [engine.rs:L156-L171](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L156-L171)：`register` 把 `reg` 的八个向量原样传给 `host.engine.register_context_layer_batch_strided(...)`，其中 `block_stride_bytes` 以 `Some(...)` 传入——它是这个批量注册 API 的可选参数，`None` 大概意味着 dense（stride == copy）；融合布局必须显式给出 stride。注册完成后，save/load 只需 `(层名, 块号)`，地址全部由注册表还原。

#### 4.1.4 代码实践（阅读型）

1. **实践目标**：把「三条契约 + 两种布局」的对应关系焊牢。
2. **操作步骤**：
   - 打开 [layout.rs:L10-L27](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L10-L27)，逐句读文档注释，标出：「same pool block ids」「unique within the engine」「num_blocks copy units of bytes_per_block」分别对应契约 C几。
   - 不看 4.3 节，先自己回答：三条契约里哪条是 `assert!` 能查的？为什么另外两条查不了？
3. **需要观察的现象**：纯阅读，无运行现象。
4. **预期结果**：C1 机器检查（单次调用内可见）；C2/C3 跨调用、跨生命周期，只能写文档。
5. 本实践无需运行，结论可直接在下一小节核对。

#### 4.1.5 小练习与答案

**练习 1**：GLM5.2 测试里 MLA arena 是 `bytes_per_block = block_stride_bytes = 656×64 = 41984`、`num_blocks = 10`。手算它的 `size_bytes`。

**答案**：\((10-1)\times 41984 + 41984 = 10 \times 41984 = 419\,840\) 字节。当 `stride == copy` 时公式坍缩为 \(N \times C\)——dense 布局下「到达界」就是「总大小」，这也是为什么直觉公式 `num_blocks × stride` 只对 dense 情形碰巧正确。

**练习 2**：一个 arena `bytes_per_block = 2048`、`block_stride_bytes = 2048`、`num_blocks = 100`、`base_ptr = 0x2000`。它的第 7 块（从 0 数）起点和终点地址是多少？

**答案**：起点 \(0x2000 + 7 \times 2048 = 0x2000 + 0x3800 = 0x5800\)；终点再 \(+2048 = 0x6000\)。dense 情形下块地址就是简单的累加。

**练习 3**：`from_arenas` 一次调用内传入两个同名 arena，理论上可以加一条断言抓住。为什么现在的代码没有？如果要加，你会在哪加？

**答案**：现在的 `assert!` 只覆盖 C1（局部、单 arena 即可判定）；同名检测需要**跨 arena** 的状态（一个已见名字集合），在 `from_arenas` 的循环里维护一个 `HashSet` 即可加。但 C2 的完整语义是「引擎生命周期内唯一」——同一引擎**先后两次**注册同名 arena 它仍然看不见，所以文档契约不可全盘机器化；调用内断言只是一个折中的加强。

### 4.2 Registration::from_buffer：把融合缓冲区翻译成 per-layer 视图

#### 4.2.1 概念说明

先精确刻画 **page-first 融合布局**。`KvBuffer` 是一整块连续显存，按页（块）切分；**每一页内部**按层交错，每层先是 K 再是 V，背靠背：

```text
┌──────────────────────────── page 0（块 0）────────────────────────────┐
│ L0.K │ L0.V │ L1.K │ L1.V │ L2.K │ L2.V │ … │ L7.K │ L7.V           │
└───────────────────────────────────────────────────────────────────────┘
┌──────────────────────────── page 1（块 1）────────────────────────────┐
│ L0.K │ L0.V │ L1.K │ L1.V │ …                                        │
└───────────────────────────────────────────────────────────────────────┘
⋮
```

关键观察有两个，它们分别决定了两个注册参数：

1. **页内**，层 ℓ 的 K 紧跟 V，中间没有别人 → 拷贝单元可以是**整个 [K|V] 一段**（`bytes_per_block = layer_bytes`），一段搞定，不需要 K/V 分开。这正是文档里「pegaflow's K/V-split path … does not apply here」的含义——vLLM 的对称布局里 K 和 V 隔开存放，需要两段与 `kv_stride`；我们没有这个问题。
2. **跨页**，层 ℓ 的块 `b` 与块 `b+1` 之间隔着**其他所有层**在同页的数据 → 相邻块不连续，`block_stride_bytes = page_stride_bytes`。**stride ≠ copy**，这就是 `block_stride_bytes` 与 `bytes_per_block` 必须解耦的全部动机：拷贝大小（页内一段连续）与块间距（跨页跳一整页）是两个独立的几何量。

于是融合布局的翻译规则是：**每个模型层注册成一个 pegaflow 层（一个 arena）**，第 ℓ 个 arena 的 `base_ptr` 整体偏移 ℓ×layer_bytes，stride 用 page_stride，copy 用 layer_bytes。

#### 4.2.2 核心流程

几何推导链（全部源自 `KvLayout::new`，元素数；字节 = 元素数 × 2）：

\[
\text{kv\_block\_len} = P \times H \times D
\]
\[
\text{layer\_stride} = 2 \times \text{kv\_block\_len} \qquad (\text{K 接 V})
\]
\[
\text{page\_stride} = L \times \text{layer\_stride} \qquad (\text{页内 } L \text{ 层})
\]

其中 \(P\) = page_size，\(H\) = num_kv_heads，\(D\) = head_dim，\(L\) = num_layers。

层 ℓ 的块 b 的字节地址（本讲的主公式）：

\[
\text{addr}(b, \ell) = \text{base} + b \times \underbrace{2\,\text{page\_stride}}_{\text{block\_stride\_bytes}} + \ell \times \underbrace{2\,\text{layer\_stride}}_{\text{bytes\_per\_block}}
\]

`from_buffer` 的流程：

```text
输入: &KvBuffer, &CudaStream
 1. layout      = buffer.layout()          # 四个维度 + 三个 stride（元素数）
 2. num_blocks  = buffer.num_blocks()
 3. base_ptr    = buffer.device_ptr(stream) # 融合缓冲区的设备基址
 4. layer_bytes       = layout.layer_stride × 2   # 每层每块的拷贝单元（字节）
    page_stride_bytes = layout.page_stride × 2    # 相邻块间距（字节）
 5. 为每个模型层 ℓ 造一个 arena：
      name = "ℓ", base_ptr = base + ℓ×layer_bytes,
      num_blocks, bytes_per_block = layer_bytes,
      block_stride_bytes = page_stride_bytes
 6. 全部喂给 from_arenas（下一节）
```

#### 4.2.3 源码精读

先看几何从哪来——`pegainfer-kv-cache` 侧的 `KvLayout::new` 三行就是三条公式：

- [pegainfer-kv-cache/src/layout.rs:L19-L32](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L19-L32)：`kv_block_len = page_size * num_kv_heads * head_dim`，`layer_stride = 2 * kv_block_len`，`page_stride = num_layers * layer_stride`。纯值类型，无 GPU、无分配——本讲所有手算都从这三行出发。
- [pegainfer-kv-cache/src/buffer.rs:L26-L46](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L26-L46)：`KvBuffer::new` 分配 `num_blocks × layout.page_stride` 个 bf16 元素——整块缓冲区的大小，手算结果的「对账」基准。
- [pegainfer-kv-cache/src/buffer.rs:L56-L66](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L56-L66)：`device_ptr` 的文档说明基址**终身稳定**（cudarc 分配不搬家），所以注册一次、终身有效；返回的裸地址比临时的 stream 排序守卫活得久，靠的是 `Arc<Inner>` 托住切片。

然后是本节主角：

- [layout.rs:L45-L56](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L45-L56)：`from_buffer` 的文档注释，把 4.2.1 的两个关键观察说满了——「K|V 连续 → 单段拷贝」与「跨块被页内其他层隔开 → `block_stride_bytes` 解耦的正是这个 gap（stride ≠ copy size）」。

```rust
pub(crate) fn from_buffer(buffer: &KvBuffer, stream: &CudaStream) -> Self {
    let layout = buffer.layout();
    let num_blocks = buffer.num_blocks();
    let base_ptr = buffer.device_ptr(stream);

    // One block's copy unit for a layer = its whole [K|V] span in a page.
    let layer_bytes = layout.layer_stride * ELEM_SIZE;
    let page_stride_bytes = layout.page_stride * ELEM_SIZE;

    let arenas: Vec<KvArena> = (0..layout.num_layers)
        .map(|layer| KvArena {
            name: layer.to_string(),
            base_ptr: base_ptr + (layer * layer_bytes) as u64,
            num_blocks,
            bytes_per_block: layer_bytes,
            block_stride_bytes: page_stride_bytes,
        })
        .collect();
    Self::from_arenas(&arenas)
}
```

- [layout.rs:L57-L76](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L57-L76)：`from_buffer` 全文只有这么长。注意三个细节：`name` 直接用层号字符串（`"0".."7"`）；`base_ptr` 的逐层偏移 `layer * layer_bytes`；所有 arena 的 `num_blocks / bytes_per_block / block_stride_bytes` 相同——融合布局在 pegaflow 侧被**归一化**成「N 条平行、彼此错开一个拷贝单元的等距序列」。

主公式的独立复算可以在集成测试开头找到（u1-l3 介绍过）：

- [cpu_roundtrip.rs:L47-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L47-L50)：`segment_ptr(base, block_id, layer, segment) = base + (block_id × PAGE_STRIDE + layer × LAYER_STRIDE + segment × SEGMENT_LEN) × 2`——正是 \(\text{addr}(b,\ell)\) 再往里走一段 K（segment=0）或 V（segment=1）。测试不信任 `KvLayout`，自己在 [cpu_roundtrip.rs:L74-L76](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L74-L76) 用 `assert_eq!(buffer.layout().page_stride, PAGE_STRIDE)` 对账。**两套独立推导互相验证**，是几何代码防回归的标准姿势。

#### 4.2.4 代码实践（手算，本讲主实践的上半场）

这就是讲义规格里要求的几何手算练习。对 `NUM_LAYERS=8, NUM_KV_HEADS=4, HEAD_DIM=16, PAGE_SIZE=16, NUM_BLOCKS=64` 的缓冲区：

1. **实践目标**：不碰代码，推出 `from_buffer` 将生成的全部 8 个 arena 的每个字段。
2. **操作步骤**：按推导链逐项算，填下表（先遮住答案列）。

| 量 | 公式 | 手算 | 答案 |
|---|---|---|---|
| `kv_block_len`（元素） | \(P \cdot H \cdot D\) | 16×4×16 | **1024** |
| `layer_stride`（元素） | \(2 \cdot \text{kv\_block\_len}\) | 2×1024 | **2048** |
| `page_stride`（元素） | \(L \cdot \text{layer\_stride}\) | 8×2048 | **16384** |
| `layer_bytes` = `bytes_per_block` | `layer_stride × 2` | 2048×2 | **4096 B** |
| `page_stride_bytes` = `block_stride_bytes` | `page_stride × 2` | 16384×2 | **32768 B** |
| arena ℓ 的 `base_ptr` | `base + ℓ × 4096` | ℓ=0..7 | **+0, +4096, …, +28672** |
| 每个 arena 的 `size_bytes` | \((N_b-1) \cdot S + C\) | 63×32768+4096 | **2 068 480 B** |
| 整块缓冲区大小（对账用） | \(N_b \cdot \text{page\_stride} \cdot 2\) | 64×32768 | **2 097 152 B** |

3. **需要观察的现象**：纸上验证两个自洽性——(a) 层 7 的最后一个块终点 \(28\,672 + 63 \times 32\,768 + 4\,096 = 2\,097\,152\)，恰好压在缓冲区结尾上，一节不多；(b) 若错用 `num_blocks × stride = 2 097 152` 当 `size_bytes`，层 7 的界会伸到 \(28\,672 + 2\,097\,152 = 2\,125\,824\)，超出缓冲区结尾 **28 672 B**（= stride − copy）。
4. **预期结果**：与上表答案一致；8 个 arena 只有 `base_ptr` 不同。
5. 手算结果将在 4.3.4 的测试里与代码对账。

#### 4.2.5 小练习与答案

**练习 1**：把 `HEAD_DIM` 翻倍成 32，其余不变。新的 `layer_bytes`、`page_stride_bytes`、`size_bytes` 是多少？

**答案**：`kv_block_len = 16×4×32 = 2048`，`layer_stride = 4096` 元素 → `layer_bytes = 8192 B`；`page_stride = 8×4096 = 32768` 元素 → `page_stride_bytes = 65536 B`；`size_bytes = 63×65536 + 8192 = 4 136 960 B`。

**练习 2**：为什么层内 K 和 V 可以合并成一次拷贝，而同层相邻两个块不行？

**答案**：页内布局是 `… | Lℓ.K | Lℓ.V | Lℓ₊₁.K | …`，层 ℓ 的 K 与 V 背靠背**连续**，一个连续区间一次 `cuMemcpyAsync` 就能搬；而层 ℓ 的块 b 结束后紧跟的是同页其他层的数据，块 b+1 要到下一页同偏移处才出现——中间夹着别人，必须靠 stride 跳过、逐块拷。

**练习 3**：用主公式算：层 3、块 5 的起点相对 `base` 的字节偏移（4.2.4 的参数）。

**答案**：\(5 \times 32\,768 + 3 \times 4\,096 = 163\,840 + 12\,288 = 176\,128\) 字节。

### 4.3 Registration::from_arenas：断言、size_bytes 上界与三个单测

#### 4.3.1 概念说明

`from_arenas` 是两条构造路径的汇合点：`from_buffer`（融合布局自动翻译）和 `with_arenas_on`（调用方自带 arena 列表，sidecar / 共享宿主路径，u2-l4 精读）。它做三件事：

1. **执行唯一的机器检查**（C1）：`bytes_per_block ≤ block_stride_bytes`；
2. **摊平**：arena 列表 → 八个平行向量；
3. **计算 `size_bytes`**：每个 arena 的注册区间长度，采用「最后一块的到达界」公式：

\[
\text{size\_bytes} = (N_b - 1) \times S + C
\]

为什么不是直觉的 \(N_b \times S\)?画出来就一目了然：

```text
base ► ▪────────▪           ▪────────▪           ▪────────▪
       │ block0 │  ←gap→    │ block1 │  ←gap→    │ blockN-1│
       └────────┘           └────────┘           └────────┘
       ├──── (N-1)×S ─────────────────────────────┤← 最后一块的起点
       ├──────────────── (N-1)×S + C ─────────────┤← 到达界 = size_bytes
       ├──────────────────── N×S ─────────────────┤← 直觉公式：多算了
                                                    最后一块后面的 gap
```

第 \(N_b - 1\) 块的**起点**在 \((N_b-1)\times S\)，拷完它走到 \((N_b-1)\times S + C\)——这就是 arena 真正触碰过的最后一个字节。\(N_b \times S\) 把最后一块**身后那段 gap**也划了进来：那段属于**别人的数据**（融合布局里是同页其他层）。`size_bytes` 是给 pegaflow 做拷贝越界校验用的界（「pegaflow validates copies against this bound」），界本身撒谎会导致：真正的越界拷贝被放过（界太大）、或合法拷贝被误拒（界太小）。所以它必须是**精确的到达界**。

另一个统一性细节：`from_arenas` 产出的注册永远是**单段**（`segments = 1`、`kv_stride_bytes = 0`）——因为 arena 的定义就是「一个块一个拷贝单元」，K/V 已在页内连成一段；需要两段与 `kv_stride` 的是 vLLM 的对称布局，那是 vLLM 连接器自己的注册形态，不是本 crate 的。

#### 4.3.2 核心流程

```text
输入: &[KvArena]
for arena in arenas:
    assert!(arena.bytes_per_block <= arena.block_stride_bytes)   # C1，违者 panic
    push name / base_ptr
    push size_bytes = (num_blocks - 1) × block_stride_bytes      # 到达界
                      + bytes_per_block
    push num_blocks / bytes_per_block / block_stride_bytes
固定填: kv_stride_bytes = 0, segments = 1     # 单段 arena
输出: Registration（八个平行向量，喂给 register_context_layer_batch_strided）
```

#### 4.3.3 源码精读

```rust
for arena in arenas {
    assert!(
        arena.bytes_per_block <= arena.block_stride_bytes,
        "arena {} copy unit {} overruns its block stride {}",
        arena.name, arena.bytes_per_block, arena.block_stride_bytes
    );
    reg.layer_names.push(arena.name.clone());
    reg.data_ptrs.push(arena.base_ptr);
    // The arena's region must cover the strided reach of its last
    // block (pegaflow validates copies against this bound).
    reg.size_bytes
        .push((arena.num_blocks - 1) * arena.block_stride_bytes + arena.bytes_per_block);
    ...
}
```

- [layout.rs:L81-L112](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L81-L112)：`from_arenas` 全文。`assert!` 在**任何 push 之前**执行——坏 arena 一个字段都不会漏进注册表；`size_bytes` 上那行注释就是「到达界」的出处。注意 `kv_stride_bytes` 与 `segments` 在结构体字面量处初始化为 `vec![0; n]` / `vec![1; n]`（[layout.rs:L89-L90](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L89-L90)），循环里根本不碰——单段是构造器的**结构性质**，不是运行时选择。

三个单测各钉死一条性质：

- [layout.rs:L123-L152](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L123-L152) `arena_registration_geometry`：**GLM5.2 sidecar 形态**。同一模型层两个 arena——`"0.mla"`（拷贝单元 `656×64 = 41984`，dense）与 `"0.idxk"`（`132×64 = 8448`，dense），共享 `num_blocks = 10`。断言覆盖全部八个字段：`size_bytes = [10×41984, 10×8448] = [419 840, 84 480]`。两个 arena、两种拷贝单元大小、一个块号——「sidecar 与主 KV 同步」的全部机制就在这张断言表里。
- [layout.rs:L157-L167](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L157-L167) `interleaved_arena_size_is_last_block_reach`：**融合形态的 size 公式**。`num_blocks=4, copy=512, stride=4096`，断言 `size_bytes = 3×4096 + 512 = 12 800`——测试名就是结论：交错 arena 的 size 是**最后一块的到达距离**，不是 `num_blocks × stride`（那会是 16 384）。
- [layout.rs:L169-L179](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L169-L179) `arena_copy_unit_must_fit_its_stride`：**C1 的负向测试**。`copy=4096 > stride=512`，`#[should_panic(expected = "overruns its block stride")]` 钉住 assert 必须开火。

#### 4.3.4 代码实践（本讲主实践：手算 ↔ 代码对账）

把 4.2.4 的手算结果交给代码检验。**先说清一个可见性事实**：`mod layout` 是 crate 私有模块，`KvArena` 虽标 `pub` 但**没有从 lib.rs 导出**，字段也全私有——所以在 `tests/` 目录或下游 crate 里**无法构造 `KvArena`**，对账测试必须写进 `src/layout.rs` 自带的 `#[cfg(test)] mod tests` 里（这也正是三个现有单测长在那里的原因；同时意味着 `with_arenas_on` 目前是 crate 内部的扩展点，外部接线要等将来导出——u3-l5 展开）。练习在你自己的克隆里**临时**添加、跑完即还原，不要提交。

1. **实践目标**：证明 4.2.4 的手算表与 `Registration::from_arenas` 的真实输出逐字段一致。
2. **操作步骤**：在 `src/layout.rs` 的 `mod tests` 内追加（示例代码，非项目原有代码）：

```rust
/// Hand-calc check: NUM_LAYERS=8, NUM_KV_HEADS=4, HEAD_DIM=16,
/// PAGE_SIZE=16, NUM_BLOCKS=64 — the fused-buffer mapping 4.2 derived on paper.
#[test]
fn hand_calc_fused_geometry() {
    const NUM_LAYERS: usize = 8;
    const NUM_KV_HEADS: usize = 4;
    const HEAD_DIM: usize = 16;
    const PAGE_SIZE: usize = 16;
    const NUM_BLOCKS: usize = 64;

    // KvLayout::new 的三条公式（元素数），再 ×ELEM_SIZE 换字节。
    let kv_block_len = PAGE_SIZE * NUM_KV_HEADS * HEAD_DIM; // 1024
    let layer_stride = 2 * kv_block_len;                    // 2048
    let page_stride = NUM_LAYERS * layer_stride;            // 16384
    let layer_bytes = layer_stride * ELEM_SIZE;             // 4096
    let page_stride_bytes = page_stride * ELEM_SIZE;        // 32768

    // from_buffer 会造的 8 个 arena（base 取 0 便于手算对账）。
    let arenas: Vec<KvArena> = (0..NUM_LAYERS)
        .map(|l| KvArena {
            name: l.to_string(),
            base_ptr: (l * layer_bytes) as u64,
            num_blocks: NUM_BLOCKS,
            bytes_per_block: layer_bytes,
            block_stride_bytes: page_stride_bytes,
        })
        .collect();
    let reg = Registration::from_arenas(&arenas);

    assert_eq!(reg.layer_names, ["0","1","2","3","4","5","6","7"]);
    assert_eq!(reg.bytes_per_block, vec![4096; NUM_LAYERS]);
    assert_eq!(reg.block_stride_bytes, vec![32768; NUM_LAYERS]);
    assert_eq!(reg.num_blocks, vec![NUM_BLOCKS; NUM_LAYERS]);
    assert_eq!(reg.segments, vec![1; NUM_LAYERS]);        // 单段 arena
    assert_eq!(reg.kv_stride_bytes, vec![0; NUM_LAYERS]);
    assert_eq!(reg.data_ptrs[7], 7 * 4096);               // 逐层偏移 ℓ×layer_bytes
    // 到达界：63×32768 + 4096 = 2_068_480，且与手算 2_068_480 逐字相等
    assert_eq!(reg.size_bytes, vec![63 * 32768 + 4096; NUM_LAYERS]);
    assert_eq!(reg.size_bytes[0], 2_068_480);
}
```

3. **需要观察的现象 / 预期结果**：`cargo test -p pegainfer-kv-offload --lib --release layout` 应报 4 个 layout 测试全绿（原有 3 个 + 新增 1 个；纯 CPU，无需 GPU）。若 `size_bytes` 断言失败并显示 2 097 152，说明你把公式写成了 `num_blocks × stride`——回到 4.3.1 的图找差距。**待本地验证**：编写环境无法运行 cargo，以上预期由公式推导得出，请在你自己的克隆里跑一次确认。
4. 另一个零成本验证：先不改代码，直接跑原有三测 `cargo test -p pegainfer-kv-offload --lib --release`（u1-l3 介绍过，纯 CPU 的 8 个单测），确认环境可用。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：4.2.4 参数下，若 `size_bytes` 误取 `num_blocks × block_stride`，层 7 arena 的注册区间终点在哪里？超出真实缓冲区多少字节？

**答案**：终点 \(28\,672 + 64 \times 32\,768 = 2\,125\,824\)，而缓冲区结尾是 2 097 152，超出 **28 672 B**，恰好等于 `stride − copy`（32 768 − 4 096）——直觉公式把最后一块身后的那段「别人的地盘」也圈了进来；层号越靠后，超出的绝对地址越远。

**练习 2**：`interleaved_arena_size_is_last_block_reach` 里 `num_blocks=4, copy=512, stride=4096`。验证 12 800，并算 `4 × 4096` 会是多少。

**答案**：\((4-1)\times 4\,096 + 512 = 12\,288 + 512 = 12\,800\)；直觉公式给 \(16\,384\)，多出的 3 584 = 4 096 − 512 又是 `stride − copy`。

**练习 3**：`arena_copy_unit_must_fit_its_stride` 为什么把 `num_blocks` 设成 1 就够了？

**答案**：C1 断言只比较 `bytes_per_block` 与 `block_stride_bytes` 两个字段，与块数无关；`num_blocks = 1` 是能让 panic 触发的最小合法值。顺带注意一个隐含前提：`from_arenas` 并未校验 `num_blocks ≥ 1`，传 0 会让 `size_bytes` 公式里的 `num_blocks - 1` 发生 usize 下溢（debug 断言下 panic，release 下回绕成天文数字）——源码里没有这条防线，靠的是调用方（executor 按真实 GPU 块数构造 `KvBuffer`）不传 0。

## 5. 综合实践

把本讲三块知识（fused 映射、sidecar 统一、到达界）串成一个任务：**给一个「fused 主 KV + 每层 router sidecar」的假想模型手写完整注册表**。

设定：主 KV 是融合布局，`NUM_LAYERS=4, NUM_KV_HEADS=2, HEAD_DIM=8, PAGE_SIZE=16, NUM_BLOCKS=32`（即 `cpu_roundtrip.rs` 的常量，方便你与集成测试互相印证）；此外每层还有一个独立的 router 状态 sidecar，每块 8192 B、dense 排布、与主 KV 共享池块号。

1. **手算主 KV 的 4 个 arena**：`kv_block_len = 16×2×8 = 256`，`layer_stride = 512` 元素，`page_stride = 2048` 元素 → `layer_bytes = 1024`、`page_stride_bytes = 4096`；arena ℓ 的 `base_ptr = base + ℓ×1024`，`size_bytes = 31×4096 + 1024 = 128 000`（恰好一个整数，很好记）。对账：层 3 末块终点 \(3\times1\,024 + 31\times4\,096 + 1\,024 = 131\,072 = 32 \times 4\,096\)，正好是整块缓冲区大小。
2. **手写 4 个 sidecar arena**：`name = "ℓ.router"`，`base_ptr` 取各自独立分配的地址（假地址即可），`num_blocks = 32`，`bytes_per_block = block_stride_bytes = 8192`，`size_bytes = 32×8192 = 262 144`（dense 坍缩）。
3. **一次 `from_arenas` 摊平 8 个 arena**，按 4.3.4 的方式写进 `src/layout.rs` 的测试模块（示例代码，跑完还原），断言：`layer_names` 顺序、两种 `bytes_per_block`（1024 与 8192——印证「sidecar 情形连 copy unit 都逐层不同」）、两种 `size_bytes`（128 000 与 262 144）。
4. **回答收束问题**：(a) save 块 `b` 时 pegaflow 会搬哪些字节？——8 个注册在块号 `b` 下的拷贝单元：4 段 1024 B 的主 KV + 4 段 8192 B 的 router 状态，共 36 864 B，一次成组、天然同步；(b) 这个假想模型该往 `namespace` 里编入哪些几何？——层数、每 arena 的 `bytes_per_block` 清单与 `num_blocks`（u2-l1 讲过的防静默交叉命中原则：几何不同则 namespace 必须不同）。
5. **预期结果**：测试全绿（**待本地验证**），并且你能不看讲义复述主公式与到达界公式。

## 6. 本讲小结

- **`KvArena` 五字段**（name / base_ptr / num_blocks / bytes_per_block / block_stride_bytes）把「等大拷贝单元 + 固定步长」抽象出来，一套字段同时表达 qwen3 的融合布局（stride > copy）与 GLM5.2 的 sidecar 布局（stride == copy、逐层多个 arena 共享块号）。
- **融合布局翻译规则**（`from_buffer`）：每模型层一个 arena，`base_ptr = base + ℓ×layer_bytes`、`bytes_per_block = layer_bytes`（页内 K|V 连续，单段拷贝）、`block_stride_bytes = page_stride_bytes`（跨块被同页其他层隔开）——stride 与 copy size 解耦是这套布局能注册进 pegaflow 的关键。
- **主公式**：\(\text{addr}(b,\ell) = \text{base} + b \times S_{\text{page}} + \ell \times S_{\text{layer}}\)（字节）；`cpu_roundtrip.rs` 的 `segment_ptr` 是它的独立复算与对账。
- **到达界**：\(\text{size\_bytes} = (N_b-1)\times S + C\)，精确覆盖到最后一块的最后一个字节；直觉公式 \(N_b \times S\) 多圈进 `stride − copy` 字节的他人领地，会让 pegaflow 的越界校验基于一个谎报的界。
- **三条契约两种强制**：C1（copy ≤ stride）由 `assert!` + panic 测试钉死；C2（name 唯一）与 C3（指针稳定 + 共享块号）超出单次调用视野，只能写成文档契约。
- **可见性即设计**：`KvArena` 未从 lib.rs 导出、字段私有，对账测试因此必须住在 `src/layout.rs` 内部——测试的位置本身就是 API 边界的注脚。

## 7. 下一步学习建议

- **下一讲 u2-l4（引擎构造与注册）**：看 `Registration` 的完整消费链——`OffloadEngine::new`（走 `from_buffer`）与 `with_arenas_on`（走 `from_arenas`，sidecar / DP-rank 共享宿主的真实入口）如何把本讲的八个向量喂给 `register_context_layer_batch_strided`，以及 `page_first` 标志何时翻转。也可先读 u2-l3（共享宿主 OffloadHost）补齐 host 侧背景。
- **源码延伸**：`pegainfer-kv-cache/src/buffer.rs` 的 `copy_page`（整页 D2D 拷贝）用的是同一套 page-first 几何，可作交叉印证；`engine.rs` 里 `register` 之后的 `layer_names` 保存（save/load 扇出的键）承接本讲的 name 契约。
- **高级方向（u3-l4）**：逐行解剖 `tests/cpu_roundtrip.rs`，看几何常量如何在端到端 GPU→CPU→GPU 字节级回归里扮演「期望值生成器」——本讲的手算正是那个测试的地基。
