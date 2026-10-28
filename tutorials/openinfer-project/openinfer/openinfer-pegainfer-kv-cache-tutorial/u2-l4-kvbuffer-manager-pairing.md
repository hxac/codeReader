# u2-l4 · KvBuffer 与 KvCacheManager:物理层配对

## 1. 本讲目标

学完本讲,你应该能够:

1. 解释「一次性连续分配、地址永不移动」为什么是 CUDA Graph 捕获安全的必要条件,以及 `KvBuffer` 如何用 `Arc<Inner>` 兑现这一承诺。
2. 掌握 `KvCacheManager` 的三条构造路径:`new`(委托给 scratch 版本)、`new_with_scratch_pages`(为投机验证预留事务层不可见的 scratch 页)、`from_buffer`(张量并行下取最小公共逻辑容量)。
3. 说清 scratch 页的编号范围、为什么 `BlockPool` 「按构造」发不出这些页,以及整页搬运原语 `copy_page` 在投机 hedge 中的双向用法。

## 2. 前置知识

本讲建立在前面四讲之上,先快速回顾需要用到的概念:

- **分页 KV 与页优先布局(u1-l3)**:KV 缓存被切成固定大小的页,三个步长由 `KvLayout` 纯乘法推出——`kv_block_len = page_size × num_kv_heads × head_dim`,`layer_stride = 2 × kv_block_len`(K 前 V 后),`page_stride = num_layers × layer_stride`(页优先:页内按层嵌套)。
- **BlockPool 与 padding 块(u2-l1)**:逻辑块池包装 kvbm-logical 的 `BlockManager`,只发放块 ID、不持显存;构造时把块 0 永久预留为 CUDA Graph 的 padding 槽。
- **RequestKv 三拍节奏(u2-l2)**:schedule(预约块)→ view(给内核页表)→ apply(封块推进);所有发放与回收都发生在池的事务层。
- **KvView 契约(u2-l3)**:页表行必须恰好覆盖 seq_len;`KvView::desc(buffer)` 把视图与物理缓冲配成内核元数据束。

再补充几个本讲新出现的术语:

- **CUDA Graph(图捕获)**:把一段 GPU 操作序列录成一张可反复回放的「图」。回放时 GPU 直接按录制时记录的**裸设备地址**访存——如果显存地址在捕获后变了,回放就会读写失效地址。这是本讲一切设计的出发点。
- **stream-ordered allocation(流序分配)**:通过 CUDA stream 进行的异步显存分配,与该流上的 kernel 保持顺序一致。cudarc 的 `alloc_zeros` 分配并清零,返回 `CudaSlice<bf16>`。
- **device-to-device copy(D2D 拷贝)**:显存到显存的拷贝,不经过主机内存,由 `memcpy_dtod_async` 在指定流上异步完成。
- **张量并行(TP)**:把同一层的权重矩阵切到多张 GPU 上,各 rank 各算一部分、每步做集合通信。TP 下所有 rank 必须**步调完全一致**——每一步算哪些请求、用哪些块,全组相同。
- **投机解码与验证(speculative decoding)**:小模型(或 draft 头)先猜若干候选 token,大模型一次 forward 验证整段;被接受的前缀保留,被拒绝的部分回滚。qwen3 的「hedge(对冲)」变体额外验证一条备选候选链,这正是 scratch 页的第一个用户。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [src/buffer.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs) | 物理 GPU 缓冲 `KvBuffer` | 一次性分配、`device_ptr` 稳定性、`copy_page` |
| [src/manager.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs) | 引擎级配对门面 `KvCacheManager` | `new` / `new_with_scratch_pages` / `from_buffer` |
| [src/pool.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs) | 逻辑块池 `BlockPool` | 池的构造参数(块数上界)、容量口径 |
| [src/view.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs) | 内核视图 `KvView` / `KvViewDesc` | `desc(buffer)`:视图如何挂到物理缓冲 |
| [tests/lifecycle.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs) | 需 CUDA 的集成测试 | `make_manager` 的最小几何、端到端生命周期 |
| pegainfer-qwen35/src/tp_executor.rs | TP 执行器(消费方) | `from_buffer` 的最小容量协商 |
| pegainfer-qwen3/src/executor.rs | qwen3 执行器(消费方) | `new_with_scratch_pages` 与 hedge 的 `copy_page` |

后两个消费方文件在别的 crate 里,本讲只引用关键片段佐证设计意图,不做深入(它们各自属于 qwen35/qwen3 的讲义范围)。

## 4. 核心概念与源码讲解

### 4.1 KvBuffer:持有显存、但不带分配器的物理层

#### 4.1.1 概念说明

前三讲里,「块」始终是逻辑概念:池发放块 ID、请求持有块 ID、视图把块 ID 排成页表。可 GPU 内核最终要读**地址**。`KvBuffer` 就是把块 ID 翻译成地址的那一层:

- 它持有**一整块连续显存**,大小是 `num_blocks × page_stride` 个 bf16 元素,一次 `alloc_zeros` 分配、清零,之后**永不移动、永不重分配**。
- 它**没有分配器**——不管哪个块给哪个请求、何时回收,全部委托给外部的 `BlockManager`(由 `BlockPool` 包装)。`KvBuffer` 只提供「第 i 页在显存里的哪里」这一条纯函数式映射:

\[
\text{addr}(p) = \text{base} + p \times S_{\text{page}} \times 2 \quad (\text{字节};\ S_{\text{page}} = \text{page\_stride},\ \text{bf16 每元素 2 字节})
\]

为什么物理层不需要分配器?因为「页给谁、能不能共享、何时回收」是纯逻辑问题,与显存无关。两层分离后,同一套池逻辑可以服务不同的物理形态:全注意力模型的融合 K|V 缓冲(本讲的 `KvBuffer`),或者 MLA 模型自己的压缩 latent 缓冲(见 pool.rs 顶部文档注释:[src/pool.rs:L26-L35](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L26-L35) 明确写着「Owns no GPU memory — the physical layout … lives with the consumer」)。

**为什么「一次性连续分配、永不移动」是 CUDA Graph 的必要条件?** 图捕获把 kernel 参数里的裸地址固化进图。要让一张捕获好的 decode 图能无限次回放,必须保证:

1. **基址不变**:KV 缓冲的基地址在引擎生命周期内恒定(`KvBuffer` 一次分配,`Arc` 保命)。
2. **页槽稳定**:页 p 的地址只依赖 p,与「谁拥有它」无关。块被回收后,同一个块 ID 再次被发放时落在同一个物理页——图里固化的地址依然有效,只是内容换了主人。
3. **哑行有处可指**:页表里超出当前序列的空洞由 padding 块(块 0,见 u2-l1)填充,回放时读到的是一块被 `std::mem::forget` 永久保命的合法显存。

如果换成「每块单独分配、按需扩容」的方案,任何一次重新分配都会让已捕获的图整体作废——这就是 PegaInfer 宁可一次性吃下一大块显存的原因。

#### 4.1.2 核心流程

`KvBuffer::new` 的流程只有三步:

```text
输入: stream, num_layers, num_kv_heads, head_dim, page_size, num_blocks
  1. layout = KvLayout::new(...)            # 纯值类型,推导三个步长
  2. total = num_blocks × layout.page_stride
  3. buffer = stream.alloc_zeros(total)     # 一次分配 + 清零
输出: KvBuffer { inner: Arc<Inner { buffer, layout, num_blocks }> }
```

注意第 3 步分配的是**元素个数**而非字节数(`CudaSlice<bf16>` 以元素为单位),换算成字节再乘 2。清零是必要的:padding 页与尚未写满的页会被内核读到,必须是确定性的零而非遗留垃圾。

`copy_page` 的流程:

```text
输入: stream, src_page, dst_page(均为物理页号)
  1. 校验 src < num_blocks 且 dst < num_blocks   # 只认物理边界
  2. src == dst → 直接 Ok(())                     # 同页拷贝是空操作
  3. base = buffer.device_ptr(stream)
     src  = base + src_page × page_stride × 2
     dst  = base + dst_page × page_stride × 2
  4. memcpy_dtod_async(dst, src, page_stride × 2) # 异步 D2D 整页搬运
```

页优先布局在这里兑现了它的红利:一页 = 一段**连续**的 `page_stride` 区间,整页搬运就是一次 memcpy,不需要按层拆碎。

#### 4.1.3 源码精读

先看内部结构与构造。[src/buffer.rs:L10-L23](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L10-L23) 定义了 `Inner`(私有)与 `KvBuffer`(仅 101 行的文件,全部 API 就是这几个方法):

```rust
struct Inner {
    buffer: CudaSlice<bf16>,
    layout: KvLayout,
    num_blocks: usize,
}

/// GPU KV cache buffer without an allocator.
#[derive(Clone)]
pub struct KvBuffer {
    inner: Arc<Inner>,
}
```

`Clone` 只是 `Arc` 指针拷贝——执行器、offload 引擎、门面可以各自持有一份克隆,共享同一块显存;只要任何一个克隆活着,显存就不会释放。这正是「地址稳定」的第二重保险。

构造函数在 [src/buffer.rs:L26-L46](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L26-L46),一次 `alloc_zeros` 分配全部显存:

```rust
let layout = KvLayout::new(num_layers, num_kv_heads, head_dim, page_size);
let total_elements = num_blocks * layout.page_stride;
let buffer: CudaSlice<bf16> = stream
    .alloc_zeros(total_elements)
    .map_err(|e| anyhow::anyhow!("KvBuffer alloc failed: {e}"))?;
```

地址访问器 `device_ptr` 是本讲最值得逐字读的文档注释,[src/buffer.rs:L56-L66](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L56-L66):

```rust
/// Base device address of the fused KV buffer.
///
/// Stable for the buffer's lifetime — cudarc allocations don't move — so
/// the KV-offload connector registers this once with pegaflow and the
/// page-first [`KvLayout`] strides reach every (layer, block, K/V) segment
/// from it. The returned address outlives the transient stream-ordering
/// guard precisely because the `Arc<Inner>` keeps the slice alive.
pub fn device_ptr(&self, stream: &CudaStream) -> u64 {
    let (ptr, _guard) = self.inner.buffer.device_ptr(stream);
    ptr
}
```

三个要点:①cudarc 的分配不会移动,基址终身稳定;②kv-offload 连接器**只注册一次**基址给 pegaflow(offload 层用同一套步长即可寻址任意 (层, 块, K/V) 段——这也是 u3-l1 的伏笔);③`device_ptr` 返回的 `(ptr, guard)` 里 guard 是临时的流序守卫,裸 `u64` 之所以在 guard 丢弃后仍然有效,是因为 `Arc<Inner>` 托底、`CudaSlice` 不会释放。调用方(比如要把它存进捕获图的执行器)拿到的就是一个「保真到引擎关停」的地址。

最后是整页搬运原语 [src/buffer.rs:L72-L99](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L72-L99):

```rust
/// Stream-ordered device-to-device copy of one whole page (…). Both pages
/// must lie inside the buffer; scratch pages past the pool's block count
/// are valid targets.
pub fn copy_page(&self, stream: &CudaStream, src_page: usize, dst_page: usize)
    -> anyhow::Result<()>
{
    anyhow::ensure!(
        src_page < self.inner.num_blocks && dst_page < self.inner.num_blocks,
        "KV page copy out of range: {src_page} -> {dst_page} of {}",
        self.inner.num_blocks
    );
    if src_page == dst_page {
        return Ok(());
    }
    let stride = self.inner.layout.page_stride;
    let elem = std::mem::size_of::<bf16>();
    let (base, _guard) = self.inner.buffer.device_ptr(stream);
    let src = base + (src_page * stride * elem) as u64;
    let dst = base + (dst_page * stride * elem) as u64;
    unsafe {
        cudarc::driver::result::memcpy_dtod_async(dst, src, stride * elem, stream.cu_stream())
    }
    .map_err(|e| anyhow::anyhow!("KV page copy failed: {e}"))
}
```

两个设计信号:校验用的 `num_blocks` 是 **`KvBuffer` 自己的物理页数**(`Inner::num_blocks`,即构造时传入的值),校验对 src/dst **对称**;而文档注释特意说明「超出池逻辑块数的 scratch 页是合法目标」——物理层根本不知道池的存在,「逻辑块数」这个概念在 `buffer.rs` 里一次都没出现过。这句注释是为 4.3 节的投机 hedge 预留的合法性背书。

#### 4.1.4 代码实践

**实践目标**:用 tests/lifecycle.rs 的最小几何,手算 `KvBuffer` 的显存占用量,核对「元素数 → 字节数」的换算链,并验证 `device_ptr` 文档注释里读到的三个承诺。

**操作步骤**:

1. 打开 [tests/lifecycle.rs:L3-L8](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L3-L8),`make_manager` 用的是 1 层、1 个 KV 头、head_dim=1、page_size=16。
2. 在纸上按 u1-l3 的公式推导:`kv_block_len`、`layer_stride`、`page_stride`,再算 `KvBuffer::new(…, num_blocks=32)` 会向 `alloc_zeros` 传多少个**元素**、对应多少**字节**。
3. 重读 [src/buffer.rs:L56-L62](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L56-L62) 的文档注释,用自己的话写下:基址为什么稳定、offload 为什么只需注册一次、裸 `u64` 为什么在 guard 丢弃后仍有效。

**需要观察的现象 / 预期结果**:第 2 步应得到 `kv_block_len = 16`、`layer_stride = 32`、`page_stride = 32` 个元素(64 字节);`num_blocks=32` 时 `alloc_zeros(1024)` 个元素 = **2048 字节**。若你的推导与之一致,说明三步长公式与「元素 vs 字节」的边界已经吃透。第 3 步的答案应覆盖:`Arc<Inner>` 保命 → 地址终身稳定 → 注册一次即可、捕获图可固化。本实践为纯纸面推导,无需 GPU,**待本地验证**的部分仅第 2 步换算(可在有 GPU 的机器上用 `cargo test --release -p pegainfer-kv-cache --test lifecycle` 间接验证:该几何下分配能成功)。

#### 4.1.5 小练习与答案

**练习 1**:如果把 `KvBuffer::new` 改成「每页单独 `alloc_zeros(page_stride)`、存在 `Vec<CudaSlice>` 里」,最先被破坏的是哪两个能力?前缀共享会受影响吗?

**答案**:最先破坏的是 ①CUDA Graph 捕获安全——页地址不再能写成 `base + id × stride` 的纯函数,且任何一页重分配都让捕获图里的地址失效;②kv-offload 的「一次注册」——pegaflow 只注册一个基址,靠步长寻址所有 (层, 块, K/V) 段,散页布局无法用一个基址覆盖。`copy_page` 的单次连续 memcpy 也会退化为按页多段拷贝(次要)。前缀共享**不受影响**——它是纯逻辑层的块哈希匹配,与物理布局无关。

**练习 2**:`copy_page` 为什么在 `src_page == dst_page` 时直接返回 `Ok(())` 而不是照常发起 memcpy?

**答案**:同页自拷贝是空操作,提前返回既省一次无意义的 D2D 带宽,也避免某些驱动对「源地址 == 目的地址」的异步拷贝给出未定义行为。调用方(投机 hedge)在「链的第一页恰好整页属于已提交前缀」的场景下会频繁遇到 src==dst,这个短路是实际会踩到的路径。

**练习 3**:`device_ptr(&self, stream: &CudaStream) -> u64` 为什么需要传 `stream`,却又返回「裸」的 `u64` 而不是某个带生命周期的地址类型?

**答案**:cudarc 的流序分配需要按流查询设备指针(返回值附带一个临时的流序守卫 guard,保证取地址操作与流上的操作有序)。而返回裸 `u64` 是刻意的:消费方要把地址烘焙进 CUDA Graph、注册给 offload、存进原生结构体,这些场景都需要一个不携带 Rust 生命周期的纯数值;安全性不靠类型,靠 `Arc<Inner>` 保证 `CudaSlice` 活得比所有消费者长——这正是文档注释最后一句的含义。

### 4.2 KvCacheManager:同几何配对与三条构造路径

#### 4.2.1 概念说明

`BlockPool` 与 `KvBuffer` 各自独立可用,但**配对**有三个硬约束:池的 `block_size` 必须等于缓冲的 `page_size`(否则块与页对不上);池发放的块 ID 必须落在缓冲的物理页范围内(否则越界);两者的显存预算来自同一个数(否则浪费或超卖)。`KvCacheManager` 就是把这个配对做对、做一次性封装的**引擎级门面**——全注意力模型(qwen3、qwen35 等)经它拿到「池 + 缓冲」的组合;MLA 模型(kimi-k2)的物理形态不同(压缩 latent,不是逐头 K|V),**绕过门面**直接消费 `BlockPool` 并自持物理缓冲,见 [src/manager.rs:L10-L18](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L10-L18) 的结构体文档:

```rust
/// Engine-level KV cache for full-attention models: one logical
/// [`BlockPool`] + one GPU [`KvBuffer`] with matching geometry.
///
/// MLA models (kimi-k2) consume [`BlockPool`] directly and own their
/// physical buffers — this facade is the full-attention pairing only.
pub struct KvCacheManager {
    pool: BlockPool,
    buffer: KvBuffer,
}
```

门面提供三条构造路径,对应三种真实场景:

| 构造函数 | 场景 | 物理页数 | 逻辑块数 |
|---|---|---|---|
| `new` | 从零建一台单卡引擎 | `num_blocks` | `num_blocks` |
| `new_with_scratch_pages` | 同上,但给投机验证链留 scratch 页 | `num_blocks + scratch_pages` | `num_blocks` |
| `from_buffer` | 已有物理缓冲(各 rank 各自画像定容),TP 协商出公共逻辑容量 | `buffer.num_blocks()`(给多大用多大) | `num_blocks ≤ 物理页数` |

#### 4.2.2 核心流程

`new_with_scratch_pages` 是核心路径,`new` 只是它的 `scratch_pages = 0` 特例:

```text
输入: stream, 几何四元组, block_size, num_blocks, scratch_pages
  1. 溢出检查: num_blocks.checked_add(scratch_pages)     # usize 溢出直接报错
  2. buffer = KvBuffer::new(…, num_blocks + scratch_pages)  # 物理页 = 逻辑 + scratch
  3. pool   = BlockPool::new(block_size, num_blocks)        # 池只认前 num_blocks 块
输出: KvCacheManager { pool, buffer }
```

于是物理缓冲被一条看不见的线切成两段:

```text
物理缓冲(一次 alloc_zeros,共 (N + S) × page_stride 个 bf16 元素)
┌────────────┬────────────┬───────┬────────────┬──────────────────────────┐
│ 页 0       │ 页 1       │  …    │ 页 N-1     │ 页 N .. 页 N+S-1         │
│ (padding)  │            │       │            │ scratch:池的事务层不可见 │
└────────────┴────────────┴───────┴────────────┴──────────────────────────┘
 └────────── 池的块 ID 空间 = 0..N ────────────┘  └── 只归调用方(hedge)──┘
```

`from_buffer` 则走反方向:缓冲已经存在(qwen35 每个模型对象自带 `kv_buffer()`),只需要配上一个新池:

```text
输入: buffer(已有), num_blocks(协商出的逻辑容量)
  1. 校验 num_blocks ≤ buffer.num_blocks()        # 逻辑不得超过物理
  2. pool = BlockPool::new(buffer.layout().page_size, num_blocks)
                                                  # 块尺寸取自缓冲几何,杜绝配错
输出: KvCacheManager { pool, buffer }
```

#### 4.2.3 源码精读

`new` 的全部实现就是委托,见 [src/manager.rs:L21-L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L21-L38):`Self::new_with_scratch_pages(stream, …, num_blocks, 0)`。「无 scratch」不是独立分支,而是 scratch 为零的特例——少一条代码路径,就少一种配错的可能。

`new_with_scratch_pages` 在 [src/manager.rs:L40-L66](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L40-L66),文档注释把 scratch 页的契约说得非常完整,值得整段读:

```rust
/// Like [`new`](Self::new), plus `scratch_pages` extra pages in the GPU
/// buffer that the pool never hands out. Their page ids are
/// `num_blocks..num_blocks + scratch_pages`; ownership and content are the
/// caller's business (e.g. the speculative hedge's alternative-chain verify
/// KV). The transaction layer is unaware of them by construction.
pub fn new_with_scratch_pages(…, num_blocks: usize, scratch_pages: usize)
    -> anyhow::Result<Self>
{
    let buffer = KvBuffer::new(
        stream, num_layers, num_kv_heads, head_dim, block_size,
        num_blocks.checked_add(scratch_pages).ok_or_else(|| {
            anyhow::anyhow!("KV pages {num_blocks} + scratch {scratch_pages} overflow")
        })?,
    )?;
    let pool = BlockPool::new(block_size, num_blocks)?;
    Ok(Self { pool, buffer })
}
```

四个要点:①scratch 页 ID 区间是**左闭右开**的 `num_blocks..num_blocks + scratch_pages`;②「所有权与内容是调用方的事务」——crate 层只保证这些页物理存在、池不发,里面写什么、怎么轮转由消费方(qwen3 hedge)自管;③「事务层按构造不可见」——池在构造时只登记了 `num_blocks` 个块(见 [src/pool.rs:L73-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L73-L79) 的 `block_count(num_blocks)`),schedule/apply/release/evict 的全部状态机都在这个命名空间里运转,ID ≥ num_blocks 的页**在池的世界里不存在**,不是「被禁止发放」而是「无法被表达」;④`checked_add` 防 `num_blocks + scratch_pages` 溢出回绕——回绕会让缓冲反而变小、scratch 区间吞掉正常页,这类静默错误必须在构造期挡下。

`from_buffer` 在 [src/manager.rs:L68-L81](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L68-L81):

```rust
/// Pair an existing physical KV buffer with a new logical block pool.
///
/// `num_blocks` may be smaller than the physical allocation. Tensor-parallel
/// executors use this to choose the minimum common logical capacity across
/// rank-local buffers while keeping one shared page-id namespace.
pub fn from_buffer(buffer: KvBuffer, num_blocks: usize) -> anyhow::Result<Self> {
    anyhow::ensure!(
        num_blocks <= buffer.num_blocks(),
        "logical KV block count {num_blocks} exceeds physical buffer capacity {}",
        buffer.num_blocks()
    );
    let pool = BlockPool::new(buffer.layout().page_size, num_blocks)?;
    Ok(Self { pool, buffer })
}
```

注意两个细节:方向性校验是「逻辑 ≤ 物理」(**多**要逻辑块才是错误;物理大于逻辑是 TP 的常态);池的 `block_size` **取自 `buffer.layout().page_size`** 而不是让调用方再传一遍——调用方传参就有传错的自由,从缓冲几何推导则构造性保证了同几何配对。此外 `BlockPool::new` 内部还会校验 `num_blocks >= 2`(u2-l1 讲过的 padding 前提,[src/pool.rs:L66-L80](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L66-L80)),所以 `from_buffer` 实际同时受「≥ 2 且 ≤ 物理」双重约束。

门面还暴露 `pool()` / `buffer()` 两个访问器([src/manager.rs:L105-L111](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L105-L111)),以及一个本讲只点名的 `new_with_events`( [src/manager.rs:L83-L103](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L83-L103),额外挂 KV 事件广播,专讲在 u3-l2)。lifecycle 测试里「视图挂缓冲」的惯用法就是这两个访问器的组合,见 [tests/lifecycle.rs:L114-L125](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L114-L125) 的 `view.desc(mgr.buffer())` 与 [src/view.rs:L57-L65](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L57-L65) 的 `KvView::desc`。

#### 4.2.4 代码实践

**实践目标**:纸面追踪三条构造路径的参数流,验证「物理页数 / 逻辑块数」两组数字,并理解 `from_buffer` 为什么从缓冲几何取 `block_size`。

**操作步骤**:

1. 画出参数流表格:对 `new(stream, L=32, H=8, D=128, block_size=16, num_blocks=100)`、`new_with_scratch_pages(…, num_blocks=100, scratch_pages=8)`、`from_buffer(buf_120pages, num_blocks=100)` 三种调用,分别写出 `KvBuffer` 实际分配的物理页数与 `BlockPool` 登记的逻辑块数。
2. 回答:若把 `from_buffer` 的池构造改成 `BlockPool::new(caller_block_size, num_blocks)`、由调用方传块尺寸,哪个不变式可能被悄悄破坏?举一个具体事故。
3. 核对:`from_buffer(buf, 1)` 会发生什么?(提示:追踪 `BlockPool::new` → `build` 的 `num_blocks >= 2` 校验。)

**需要观察的现象 / 预期结果**:第 1 步应为——`new`:物理 100 / 逻辑 100;`new_with_scratch_pages`:物理 **108** / 逻辑 100(scratch 区间 100..108);`from_buffer`:物理 **120**(缓冲已有,不新分配)/ 逻辑 100,页 100..120 物理存在但池不可见。第 2 步:破坏「池块尺寸 == 缓冲页尺寸」——比如调用方传 32 而缓冲页是 16,则一个「块」只填半页,内核按页优先几何读出的 K/V 全错位,且这种错位**不报错**,只输出乱码。第 3 步:`from_buffer` 自己的 `ensure!(num_blocks <= …)` 通过,但 `BlockPool::new` 内部以「need at least 2 blocks (1 for padding)」报错。本实践为源码阅读型,结论可离线推得;有 GPU 时可用第 5 节的 lifecycle 测试间接验证。

#### 4.2.5 小练习与答案

**练习 1**:`new` 与 `from_buffer` 各做哪个方向的数量校验?为什么方向相反?

**答案**:`new` 路径经 `BlockPool::build` 校验 `num_blocks >= 2`(至少要有一块 padding 加一块可用),因为它是从零分配,下界是硬需求;`from_buffer` 额外校验 `num_blocks <= buffer.num_blocks()`(逻辑不得超过物理),因为它的缓冲是别人给的、容量是既成事实,上界是物理约束。两条路径最终都汇到同一个 `BlockPool::build`,所以 `from_buffer` 实际同时受两者约束。

**练习 2**:「事务层不可见(by construction)」与「事务层禁止发放(by policy)」有什么区别?为什么前者更强?

**答案**:「按策略禁止」意味着池知道这些页的存在、靠一条 if 规则拦住不放——规则可以被绕过、被未来改动打破,且每次发放都要做检查。「按构造不可见」意味着池的底层 `BlockManager` 从一开始就只登记了 `num_blocks` 个块,ID ≥ num_blocks 的页在池的全部数据结构里没有位置,schedule/apply/release/evict 任何代码路径**想碰也碰不到**。前者靠纪律,后者靠不可能;后者不需要任何运行时检查,也就没有检查被遗忘的风险。

**练习 3**:`new_with_scratch_pages` 里如果不用 `checked_add` 而直接写 `num_blocks + scratch_pages`,最坏会发生什么?

**答案**:若两数之和溢出 `usize` 会回绕成一个很小的数:缓冲被分配得极小,而池仍登记 `num_blocks` 个逻辑块,随后某次发放的块 ID 超出物理页范围,内核越界读写显存——崩溃点离事故点极远,极难排查。`checked_add` 把它变成构造期的一条明确报错,这是「fail fast」在显存管理里的典型应用。

### 4.3 消费方视角:TP 最小容量协商与投机 hedge 的 scratch 页

#### 4.3.1 概念说明

前两节讲的是 crate 内的机制,本节用两个真实消费方回答「这些设计为什么存在」。

**TP 与最小公共逻辑容量**:张量并行下,每个 rank 只持有 KV 头的一个分片,几何各不相同;更要紧的是,每个 rank 的物理缓冲容量是**各自按本机空闲显存画像**定出来的(qwen3 执行器在构造 `KvCacheManager` 之前先跑 `profile_kv_budget_on_worker` 得到 `budget.num_blocks`,见 [pegainfer-qwen3/src/executor.rs:L1113-L1127](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1113-L1127))。不同 rank 显存余量不同,`num_blocks` 就可能不同。但 TP 是锁步执行:每一步算哪些请求、引用哪些块,全组必须一致——**页 ID 命名空间必须全组共享且全组等长**。于是逻辑容量只能取各 rank 的最小值,这就是 `from_buffer` 文档里「minimum common logical capacity across rank-local buffers while keeping one shared page-id namespace」的含义。

**投机 hedge 与 scratch 页**:qwen3 的 DFlash 投机解码有一条「对冲」增强——除了验证主候选链 A,再平行验证一条备选链 B。链 B 的 KV 不能直接写进该请求的正式页(会污染链 A 尚未裁决的 KV),它需要**自己的页**来写;但池的事务层不认识「临时页」这种概念。scratch 页就是解法:物理上多分配几页、逻辑上对池隐身,链 B 的页表行指向 scratch 页,输赢裁决后再决定是否整页拷回正式页。

#### 4.3.2 核心流程

qwen35 TP 的构造协商(每 rank 各自执行,结果必须一致):

```text
1. 断言所有 rank 的 page_size 一致            # 几何分片可以不同,页大小必须相同
2. min_capacity = min over ranks(本 rank kv_buffer().num_blocks())
3. kv_cache = KvCacheManager::from_buffer(first.kv_buffer().clone(), min_capacity)
4. 可用容量 = min_capacity - 1                 # 减去 padding 块
```

qwen3 hedge 每个验证步的 scratch 轮转:

```text
预备(引擎构造时):new_with_scratch_pages(…, num_blocks=N, scratch_pages=S)
  → scratch 区间 = [N, N+S),由执行器私有光标 next_scratch 管理

每一步(验证 round):
  1. scratch_end = kv_buffer.num_blocks()      # 物理上界 = N+S
     若 scratch_end == total_blocks(即 S == 0)→ 无 hedge,直接返回
  2. 为链 B 构造页表:把链 A 视图中「span 覆盖的页」替换为 scratch 页
     若 span 的第一页与已提交前缀共享(半满页):
        copy_page(orig → scratch)              # 先拷贝共享的前缀行,链 B 才能续写
  3. 链 B 在 scratch 页里写自己的验证 KV,与链 A 同批 forward
  4. 裁决:B 严格更优 → copy_page(scratch → orig) 整页拷回正式页;否则丢弃
  5. next_scratch 回卷(下一步复用同一批 scratch 页,内容覆盖即可)
```

#### 4.3.3 源码精读

TP 协商的实现在 [pegainfer-qwen35/src/tp_executor.rs:L734-L756](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen35/src/tp_executor.rs#L734-L756):

```rust
let page_size = first.kv_buffer().layout().page_size;
let mut min_capacity_pages = usize::MAX;
for (rank, model) in models.iter().enumerate() {
    let rank_page_size = model.kv_buffer().layout().page_size;
    anyhow::ensure!(
        rank_page_size == page_size,
        "Qwen3.5 TP rank {rank} KV page size {rank_page_size} does not match rank 0 page size {page_size}"
    );
    min_capacity_pages = min_capacity_pages.min(model.kv_buffer().num_blocks());
}
let kv_cache = Qwen35PrefixCache::new(
    KvCacheManager::from_buffer(first.kv_buffer().clone(), min_capacity_pages)?,
    …,
)?;
let capacity_pages_for_requests = min_capacity_pages.saturating_sub(1);
```

三个动作一目了然:页大小一致性断言 → 取各 rank 物理容量的 min → `from_buffer` 配池并减去 padding 得到请求可用容量。注意单 rank 满配路径(`scheduler/backend.rs`、`executor.rs`、`unified_forward.rs`)则一律写 `KvCacheManager::from_buffer(model.kv_buffer().clone(), model.kv_buffer().num_blocks())`——逻辑容量就等于物理容量,`from_buffer` 退化成「给已有缓冲配池」的纯配对操作。

hedge 侧,引擎构造时的 scratch 预留在 [pegainfer-qwen3/src/executor.rs:L1119-L1127](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1119-L1127)——正是本讲 4.2 的 `new_with_scratch_pages`,`hedge_scratch_pages` 来自 draft 路径的配置。运行时的 scratch 区间在 [pegainfer-qwen3/src/executor.rs:L3674-L3690](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L3674-L3690):

```rust
let scratch_end = self.kv_buffer.num_blocks();   // 物理 = N + S
…
if cap == 0 || scratch_end == self.total_blocks { // total_blocks = 池逻辑 = N
    return Ok(None);                              // 没配 scratch 页 → hedge 关闭
}
…
let mut next_scratch = self.total_blocks;         // 光标从 N 起步,只涨到 N+S
```

「链 B 借 scratch 页」的替换逻辑在 [pegainfer-qwen3/src/executor.rs:L3734-L3748](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L3734-L3748):把链 A 视图中 span 覆盖的页号逐个换成 `next_scratch + k`,并在**第一页与已提交前缀共享**(span 不从页边界开始)时先做一次 `self.kv_buffer.copy_page(&ctx.stream, orig, scratch_page)?`——因为该页前半段是已提交前缀的 KV,链 B 的续写必须建立在这份拷贝之上,否则 scratch 页前半段是垃圾。裁决后赢家把 KV 拷回正式页,见 [pegainfer-qwen3/src/executor.rs:L3824-L3831](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L3824-L3831) 的 `self.kv_buffer.copy_page(&ctx.stream, scratch_page, orig)?`。`copy_page` 的两次调用方向恰好相反,而两次的页号都**越过了池的逻辑边界**——回到 4.1.3 那句「scratch pages past the pool's block count are valid targets」,在这里兑现。

#### 4.3.4 代码实践

**实践目标**:从消费方代码反推两个数字——TP 的 `min_capacity_pages` 与 hedge 的 scratch 页号区间,把「谁定的容量、谁管 scratch」两条线各自走通。

**操作步骤**:

1. 打开 [pegainfer-qwen35/src/tp_executor.rs:L734-L756](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen35/src/tp_executor.rs#L734-L756),假设 8 个 rank 的 `kv_buffer().num_blocks()` 分别为 {1000, 960, 1000, 980, 960, 1000, 972, 960},写出 `min_capacity_pages`、`capacity_pages_for_requests`,以及「多余的物理页」最少的 rank 与最多的 rank 各浪费几页。
2. 打开 [pegainfer-qwen3/src/executor.rs:L3674-L3690](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L3674-L3690),设 `num_blocks=1024`、`hedge_scratch_pages=64`,写出 `self.total_blocks`、`scratch_end`、scratch 页号区间;再追一步:若某请求的 span 占 3 页且第一步 `next_scratch` 已是 1088,这次展开会不会撞界?(找 `next_scratch + span_pages > scratch_end` 的判断。)

**需要观察的现象 / 预期结果**:第 1 步——`min_capacity_pages = 960`,可用 959;各 rank 浪费 0~40 页不等(显存换锁步一致性,这是刻意的取舍)。第 2 步——`total_blocks = 1024`、`scratch_end = 1088`,scratch 区间 [1024, 1088);`next_scratch(1088) + 3 > 1088` 成立,该链被 `break` 跳过——scratch 预算用尽时 hedge 优雅降级而非越界。两步均为源码阅读型,无需 GPU,结论可离线推得;**待本地验证**:在有 GPU 与模型权重的环境跑 `cargo run --release -- --model-path models/Qwen3-4B` 观察 TP/投机路径的启动日志(具体日志行以当前版本为准)。

#### 4.3.5 小练习与答案

**练习 1**:TP 中某 rank 的物理容量比 `min_capacity_pages` 多出 40 页,这 40 页处于什么状态?它们像 scratch 页一样「归调用方」吗?

**答案**:处于**闲置**状态:物理上存在且属于该 rank 的 `KvBuffer`,但池只登记了 `min_capacity_pages` 个块,ID ≥ min 的页池发不出来。它们不像 scratch 页那样被主动使用(`from_buffer` 路径没有消费者),只是「被协商裁掉的余量」——这是用显存余量换全组锁步一致性的代价。

**练习 2**:qwen3 hedge 在 `scratch_end == self.total_blocks` 时直接 `return Ok(None)`,这个判断等价于什么?为什么放在循环开始而不是每次取 scratch 页时检查?

**答案**:等价于「构造时 `hedge_scratch_pages == 0`」(`scratch_end = N + S`,`total_blocks = N`,两者相等当且仅当 S = 0),即引擎根本没开 hedge。放在最前面是一次性的能力探测:没配 scratch 页时整条 hedge 路径零开销地短路,不必在后面的每条链、每个页上反复判断越界——而真正使用 scratch 的循环里仍有 `next_scratch + span_pages > scratch_end` 的逐链预算检查,两层各司其职。

**练习 3**:链 B 的 span 第一页若与已提交前缀**共享半页**,为什么必须先 `copy_page(orig → scratch)`?如果跳过这一步,验证结果会怎么错?

**答案**:scratch 页此时内容是上一步的残影(或零)。链 B 的 attention 要把「已提交前缀 + 候选 token」整段一起算,前半页的前缀 K/V 缺失意味着链 B 读到的是错误的历史上下文——不是崩溃,而是**静默算错**,验证裁决(接受多少个候选)随之失真,可能接受本该拒绝的 token。先整页拷贝,让 scratch 页 = 已提交前缀 + 待写 span,链 B 才与链 A 站在同一起跑线上。

## 5. 综合实践

本讲的贯穿任务是一次「设计答辩」:精读 [src/manager.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs) 的 `from_buffer` 与 `new_with_scratch_pages` 实现(必要时回到 [src/buffer.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs) 与 [src/pool.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs)),书面回答三个问题:

1. **TP 容量协商**:为什么张量并行各 rank 的物理 buffer 大小可能不同,而逻辑容量必须取最小值?请同时引用 `from_buffer` 的文档注释与 qwen35 TP 执行器 [pegainfer-qwen35/src/tp_executor.rs:L734-L756](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen35/src/tp_executor.rs#L734-L756) 的代码作为论据。
2. **scratch 页**:scratch 页的 page id 范围是什么?为什么 `BlockPool` 绝不发放它们——是「禁止」还是「不可能」?请用 [src/manager.rs:L40-L44](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L40-L44)(契约)与 [src/pool.rs:L73-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L73-L79)(构造)两段代码支撑你的结论。
3. **copy_page 的边界**:`KvBuffer::copy_page` 为什么要求页号小于物理 `num_blocks`,却「允许」超过池的逻辑块数?「允许」这个词准确吗——它需要做任何额外检查吗?

**参考答案要点**(先自己写完再看):

1. 物理 buffer 由各 rank 按本机显存画像独立定容(qwen3 的 `profile_kv_budget_on_worker` 就是证据),显存余量不同则 `num_blocks` 不同;但 TP 锁步要求全组每一步引用同一组块 ID,页 ID 命名空间必须全组等长,故逻辑容量 = min(各 rank 物理容量),由 `from_buffer(first.kv_buffer().clone(), min_capacity_pages)` 落实。
2. 范围是 `num_blocks..num_blocks + scratch_pages`(左闭右开)。「不可能」而非「禁止」:池的底层 `BlockManager` 构造时 `block_count(num_blocks)` 只登记了 N 个块,ID ≥ N 在池的全部数据结构中不存在,schedule/apply/evict 任何路径都无法表达它们——文档所谓「The transaction layer is unaware of them by construction」。
3. `copy_page` 是物理层原语,它只知道 `Inner::num_blocks`(物理页数),**根本不持有池的引用**,「池的逻辑块数」对它是不存在的概念,因此谈不上「额外允许」或检查——`ensure!` 只对物理上界做对称校验。合法性由用途背书:hedge 的拷出(scratch→orig)与拷入(orig→scratch)两侧都可能越过逻辑边界,这正是文档注释「scratch pages past the pool's block count are valid targets」的由来。

**可选的 GPU 验证**(有 CUDA 环境时):运行

```bash
cargo test --release -p pegainfer-kv-cache --test lifecycle
```

观察 [tests/lifecycle.rs:L10-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L10-L50) 的端到端生命周期:构造(`new` 路径)→ 预填 → 解码 → 释放,`available_blocks()` 从 31 开始、按请求占用递减、释放后复原。无 GPU 环境则跳过,以上三问均可离线完成。

## 6. 本讲小结

- `KvBuffer` 是「持有显存但无分配器」的物理层:`Arc<Inner>` 包住一次 `alloc_zeros` 的连续 bf16 缓冲,基址终身稳定、`Clone` 只是引用计数——这是 CUDA Graph 捕获安全与 kv-offload「一次注册基址」的共同前提。
- 页优先布局让「第 p 页」恒等于一段连续的 `page_stride` 区间,`copy_page` 因此能用一次 `memcpy_dtod_async` 完成整页 D2D 搬运,校验只认物理边界、对 src/dst 对称。
- `KvCacheManager` 是全注意力模型的配对门面:`new` = `new_with_scratch_pages(…, 0)` 的特例;MLA 模型(kimi-k2)绕过门面直接消费 `BlockPool` 并自持物理缓冲。
- scratch 页的 ID 区间是 `num_blocks..num_blocks + scratch_pages`,对池的事务层「按构造不可见」——不是被规则禁止,而是在池的数据结构里无法表达;调用方(qwen3 hedge)用私有光标管理它们的轮转。
- `from_buffer` 支持「逻辑 ≤ 物理」的配对:TP 各 rank 按本机显存定容、物理大小可以不同,但锁步执行要求全组共享等长的页 ID 命名空间,故逻辑容量取 min(qwen35 TP 执行器的协商代码是标准消费方)。

## 7. 下一步学习建议

物理层配对已经闭环,下一讲 **u2-l5《前缀缓存:块哈希、匹配与 LoRA salt》** 回到逻辑层最有趣的部分:块释放后并不消失,而是带着哈希进入非活跃池,等待下一个共享前缀的请求把它们「白捡」回去——`probe_prefix`、`match_and_add_prefix`、加盐哈希与 `evict_inactive` 的 LRU 回收都在那一讲展开。如果想在读下一讲前再巩固本讲,建议重读 [src/buffer.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs)(只有 101 行,逐行读一遍)并对照 [src/view.rs:L57-L65](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L57-L65) 想清楚「块 ID → 页地址」这条翻译链在 forward 时是如何被内核消费的。
