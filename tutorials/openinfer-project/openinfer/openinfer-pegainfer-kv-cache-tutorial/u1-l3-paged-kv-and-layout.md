# 分页 KV 与页优先布局 KvLayout

> 本讲属于「入门：认识 pegainfer-kv-cache」单元第 3 讲，承接 u1-l1 的两层分离设计（BlockPool 逻辑层 / KvBuffer 物理层）与 u1-l2 的五模块分工。

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释推理引擎为什么要把 KV 缓存切成固定大小的页，而不是给每个请求分配一整块连续显存——碎片控制、前缀块共享、CUDA Graph 捕获所需的指针稳定、整页搬运，四个理由各自的机理。
2. 独立推导 `KvLayout` 的三个步长：`kv_block_len`、`layer_stride`、`page_stride`，并说清每个步长在显存里「跨过了什么」。
3. 画出「页优先」排布的示意图（每页内 `[层][K|V]` 嵌套），并与假想的「层优先」排布对比，说清为什么本 crate 选页优先。
4. 完成从模型几何参数（层数、KV 头数、头维度、页大小）到「一页占多少字节显存」「一块卡能装多少页」的换算。

## 2. 前置知识

本讲只需要一点 Transformer 推理的直觉，不需要 CUDA 编程经验。以下术语按由浅入深排列：

- **token（词元）**：模型处理文本的最小单位。生成文本就是一个 token 一个 token 地往外吐。
- **自回归解码与 KV 缓存**：每生成一个新 token，注意力层都要让新 token 的 Query 与**历史所有 token** 的 Key、Value 做点积。如果把历史 K/V 重新算一遍，代价是平方级的；所以引擎把每层算出的 K/V **缓存**下来。这份缓存就是 KV cache，它随序列长度线性增长，是推理显存的大头。
- **GQA（分组查询注意力）与 `num_kv_heads`**：现代模型（如 Qwen3）的 Query 头很多（例如 32 个），但 KV 头只有 8 个，若干 Query 头共享一组 KV。所以 KV 缓存的大小按 `num_kv_heads`（而非 Query 头数）计算——这是缓存能省显存的关键。
- **`head_dim`（头维度）**：每个头向量的长度，例如 128。
- **bf16**：Brain Float 16，每个元素 2 字节。本 crate 的物理缓冲 `KvBuffer` 就是 `CudaSlice<bf16>`。
- **显存碎片（外部碎片）**：如果每个请求按其最大上下文一次性申请一整条连续显存，请求进出之后显存里会留下大小不一的「洞」——总空闲量够，却没有任何一个连续洞装得下新请求。分页（固定大小的小块）从根上消除这个问题，代价是每请求平均浪费半页（内部碎片）。
- **CUDA Graph**：把一串 kernel 启动录制（capture）成一张图，之后整图重放以消灭 CPU 启动开销。重放要求**录制的指针地址不能变**——这解释了为什么 `KvBuffer` 只在构造时分配一次、之后永不移动（详见 4.1）。

一个贯穿全讲的示例几何（与 Qwen3 同量级，非精确对应某个具体模型）：

\[ \text{num\_layers}=32,\quad \text{num\_kv\_heads}=8,\quad \text{head\_dim}=128,\quad \text{page\_size}=16 \]

## 3. 本讲源码地图

本讲涉及 3 个本 crate 源码文件，外加 1 个消费者文件用于交叉验证：

| 文件 | 行数 | 在本讲中的角色 |
|------|------|----------------|
| [src/layout.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L1-L33) | 33 | 主角：`KvLayout` 纯值类型，三个步长的唯一推导处 |
| [src/buffer.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L1-L100) | 100 | 几何的物理消费者：一次性分配、`copy_page` 整页搬运、指针稳定承诺 |
| [src/pool.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L26-L123) | 1009 | 几何的逻辑消费者：按块 ID 发放/回收，自身不持显存（本讲只读前 123 行的池构造与容量部分） |
| [../pegainfer-qwen3/src/weights.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L820-L861) | — | 真实消费者：用 `page_stride` 换算「一页多少字节」来规划显存预算 |
| [src/manager.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L40-L81) | 112 | 佐证：`from_buffer` 证明池的 `block_size` 就是 `layout.page_size` |

永久链接约定：本 crate 内文件基于 `blob/785d0487…/pegainfer-kv-cache/`，仓库其他 crate 基于同一 commit 的仓库根。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**为什么分页** → **三个步长的推导** → **页优先排布** → **从布局到字节数**。

### 4.1 为什么把 KV 切成固定大小的页

#### 4.1.1 概念说明

设想两种极端方案：

- **方案 A（连续 per-request 分配）**：每个请求按其最大上下文一次申请一条连续显存。问题是：请求 A（长）走了、请求 B（短）来了又走，显存里留下形状各异的洞——**外部碎片**；而且必须按「最大可能长度」预留，短对话浪费巨大。
- **方案 B（分页）**：把物理缓冲划成 thousands 个固定大小的页（page，也叫 block），每个页装 `page_size` 个 token 的、**所有层**的 K/V。请求要多少页就领多少页，页与页之间**不必相邻**。任何空闲页都同样可用，外部碎片归零；浪费只剩每个请求最后那个没写满的页（内部碎片，平均半页）。

除了碎片，分页还买来三个引擎级的能力：

1. **前缀块共享**：页是不可变的最小单位。两个请求共享同一段 system prompt 时，前若干页可以在池里注册一次、被两个请求同时引用，省下整段预填的计算与显存。
2. **CUDA Graph 指针稳定**：页的「分配/回收」只是逻辑层账本上的 ID 变动，物理显存从头到尾是同一块、同一地址。CUDA Graph 录制的 kernel 参数（基址 + 步长）永远有效。
3. **整页搬运**：一页在显存里是一段**连续**的 `page_stride` 元素。无论换页、下刷到 CPU（kv-offload 桥）还是投机验证链的备份，都是一次连续拷贝。

#### 4.1.2 核心流程

两条职责线在两个类型上分开（承接 u1-l1 的两层分离）：

```text
逻辑层 BlockPool                      物理层 KvBuffer
  发放块 ID（page id）                  一次性 alloc_zeros 出全部显存
  记账/共享/回收（kvbm-logical）  ←块 ID 索引→  按 KvLayout 的步长定位第 p 页第 l 层
  不持任何显存                          不带分配器、地址永不移动
```

#### 4.1.3 源码精读

BlockPool 的文档注释一句话讲清了分工——逻辑池发放块 ID，物理布局（全注意力的 `KvBuffer` 或 MLA 的双缓冲）由消费者持有并被这些 ID 索引：

- [src/pool.rs:L26-L35](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L26-L35) —— `BlockPool` 的文档注释与结构体：`Owns no GPU memory`，物理布局由消费者持有、按池发放的块 ID 索引；字段只有 `block_manager`、`block_size`、`padding_block_id` 三个，没有任何设备指针。
- [src/buffer.rs:L16-L23](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L16-L23) —— `KvBuffer` 的文档注释：`GPU KV cache buffer without an allocator`，拥有设备显存与布局几何，但把块分配委托给外部的 `BlockManager`（kvbm-logical）。`inner: Arc<Inner>` 的设计让克隆共享同一块显存。
- [src/buffer.rs:L56-L66](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L56-L66) —— `device_ptr` 的文档注释明确承诺：cudarc 的分配**不会移动**，地址在缓冲生命周期内稳定，所以 kv-offload 连接器只注册一次基址，页优先的 `KvLayout` 步长即可从它出发够到任意 `(layer, block, K/V)` 段——这就是 CUDA Graph 与 offload 所需的指针稳定。
- [src/pool.rs:L82-L95](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L82-L95) —— 构造池时预留块 0 作为 CUDA Graph 的 padding 槽：先 `stage` 再注册成不可变块（RAII 使其不回空闲池），最后 `std::mem::forget` 让它与引擎同寿。CUDA Graph 重放时批次里空槽位会指向这个无害的 padding 页，避免读到野地址（细节在 u2 展开）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲眼确认「池不持显存、缓冲不带分配器」的分工不是文档口号。
2. **操作步骤**：分别打开上面四段链接，只读注释与字段定义。
3. **需要观察的现象**：`BlockPool` 的三个字段里没有 `CudaSlice`；`KvBuffer::Inner` 的三个字段是 `buffer: CudaSlice<bf16>`、`layout: KvLayout`、`num_blocks: usize`——**没有任何分配器或空闲链表**。
4. **预期结果**：能回答——块 ID 由谁发放（`BlockPool` 背后的 `BlockManager`）、显存由谁持有（`KvBuffer`）、二者靠什么关联（块 ID/页号）。

#### 4.1.5 小练习与答案

**练习 1**：请求结束时，它的页为什么不需要「归还显存」？
**答案**：物理显存从未按请求分配过。归还的是逻辑层的块 ID——`BlockManager` 把 ID 放回空闲池，等下一个请求复用；那块显存里的旧数据只是等着被覆盖（本讲不展开释放路径，详见 u2-l2 的 `release`）。

**练习 2**：如果把「按最大上下文连续分配」改成「按当前长度连续分配、增长时搬迁」，能同时解决碎片和预留浪费吗？代价是什么？
**答案**：不能两全。增长时搬迁意味着 KV 数据的地址会变，CUDA Graph 录制的指针立即失效（必须重录），且每次搬迁是一次全量 device-to-device 拷贝；更糟的是长序列搬迁时仍需一段「旧+新」同时在场的峰值显存。分页方案用「逻辑回收、物理不动」同时绕开了这两点。

### 4.2 KvLayout：三个步长的推导

#### 4.2.1 概念说明

`KvLayout` 是一个**纯值类型**（pure value type）：只有 7 个 `usize` 字段、不碰 GPU、不做任何分配。它回答一个问题——

> 「第 p 页、第 l 层的 K（或 V）数据，在物理缓冲的哪个元素下标开始？」

答案由三个步长的乘法链给出。设 `page_size = P`、`num_kv_heads = H`、`head_dim = D`、`num_layers = L`：

\[
\text{kv\_block\_len} = P \times H \times D
\]
——一个 K 块（或 V 块）的元素数：`P` 个 token × `H` 个 KV 头 × 每头 `D` 维。

\[
\text{layer\_stride} = 2 \times \text{kv\_block\_len}
\]
——同一页内跨一层走的距离：一层占 K 块 + V 块两段，K 在前 V 在后。

\[
\text{page\_stride} = L \times \text{layer\_stride}
\]
——一整页的元素数：`L` 层依次排开。页与页之间就按 `page_stride` 步进。

三条式子是纯乘法链，所以元素下标可以一步展开成：

\[
\text{offset}(p, l, s, t, h, d) = p \cdot \text{page\_stride} + l \cdot \text{layer\_stride} + s \cdot \text{kv\_block\_len} + ((t \cdot H + h) \cdot D + d)
\]

其中 \( s \in \{0, 1\} \) 选 K 或 V。注意：`KvLayout` 本身只约束到 `kv_block_len` 这一级——块内部 token/头/维度的微观排布由写入它的注意力内核约定，`layout.rs` 不做规定；上式块内部分应理解为示意。

#### 4.2.2 核心流程

`KvLayout::new` 的推导流程就是三条乘法，没有任何分支与校验：

```text
输入 num_layers, num_kv_heads, head_dim, page_size
  ├─ kv_block_len = page_size × num_kv_heads × head_dim
  ├─ layer_stride = 2 × kv_block_len
  └─ page_stride  = num_layers × layer_stride
输出：7 字段的结构体（4 个原始维度 + 3 个派生步长）
```

#### 4.2.3 源码精读

- [src/layout.rs:L1-L3](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L1-L3) —— 文件头注释自述设计：`Page-first geometry`（页优先几何）、`Pure value type — no GPU, no allocation`。
- [src/layout.rs:L4-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L4-L16) —— 结构体定义：前 4 个字段是模型原始维度，后 3 个字段的文档注释各自写明推导式（`kv_block_len` 是 `page_size × num_kv_heads × head_dim`，`layer_stride` 是 `2 × kv_block_len` 即 K 后接 V，`page_stride` 是 `num_layers × layer_stride`）。
- [src/layout.rs:L18-L33](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L18-L33) —— `KvLayout::new` 全部实现：三行乘法赋值、打包返回。注意它 `#[derive(Clone, Copy, Debug)]`——小到可以按值随处传递。
- [src/manager.rs:L73-L81](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L73-L81) —— `KvCacheManager::from_buffer` 里的 `BlockPool::new(buffer.layout().page_size, num_blocks)` 是「池的 `block_size`（token 数）就是布局的 `page_size`」这一等价的直接证据：逻辑块与物理页共用同一个尺寸参数。

#### 4.2.4 代码实践（本讲主实践：手算 + 写测试验证）

对示例几何 \( L=32, H=8, D=128, P=16 \)：

1. **实践目标**：先手算三个步长，再写一个无需 GPU 的测试用 `KvLayout::new` 验证，最后换算一页的字节数。
2. **操作步骤**：
   1. 手算（先别运行任何代码）：
      - `kv_block_len = 16 × 8 × 128 = 16384`
      - `layer_stride = 2 × 16384 = 32768`
      - `page_stride = 32 × 32768 = 1048576`
   2. 在 `pegainfer-kv-cache/tests/` 下新建 `layout_geometry_practice.rs`（示例代码，临时练习用，练完可删）：

      ```rust
      // 示例代码：tests/layout_geometry_practice.rs —— 纯逻辑，无需 GPU
      use pegainfer_kv_cache::KvLayout;

      const BF16_BYTES: usize = 2; // half::bf16 每元素 2 字节

      #[test]
      fn hand_computed_strides_match_layout() {
          let layout = KvLayout::new(32, 8, 128, 16);
          assert_eq!(layout.kv_block_len, 16 * 8 * 128);   // 16384
          assert_eq!(layout.layer_stride, 2 * 16384);      // 32768
          assert_eq!(layout.page_stride, 32 * 32768);      // 1048576
          // 一页显存字节数 = 元素数 × 2
          assert_eq!(layout.page_stride * BF16_BYTES, 2 * 1024 * 1024); // 2 MiB
      }
      ```

   3. 运行（与 u1-l2 讲过的纯逻辑测试同一跑法）：

      ```bash
      cargo test --release -p pegainfer-kv-cache --test layout_geometry_practice
      ```
3. **需要观察的现象**：测试通过，说明手算与 `KvLayout::new` 的乘法链完全一致。
4. **预期结果**：`kv_block_len=16384`、`layer_stride=32768`、`page_stride=1048576`，一页 bf16 恰好 **2 MiB**（运行输出待本地验证；本讲写作环境未执行该命令）。
5. 顺带验证：每 token 的 KV 字节数 \( = 2 \times 32 \times 8 \times 128 \times 2\,\text{B} = 131072\,\text{B} = 128\,\text{KiB} \)，而一页 16 token × 128 KiB = 2 MiB，与 `page_stride × 2` 交叉吻合。

#### 4.2.5 小练习与答案

**练习 1**：若 `page_size` 从 16 提到 64（其余不变），三个步长各变为多少？一页多少字节？
**答案**：`kv_block_len = 64 × 8 × 128 = 65536`；`layer_stride = 131072`；`page_stride = 32 × 131072 = 4194304`；一页 = 4194304 × 2 B = 8 MiB。`page_size` 是乘法链的最外层因子，它放大 4 倍，所有步长与页字节数都放大 4 倍。

**练习 2**：`layer_stride` 为什么是 `2 × kv_block_len` 而不是 `kv_block_len`？
**答案**：因为一层之内要同时放 K 和 V 两份同长的数据，K 块在前、V 块紧随其后。`2` 这个因子就是「K|V 成对」的体现。

**练习 3**：`KvLayout::new` 里没有任何合法性检查（比如维度为 0）。这样的纯乘法类型，溢出防护放在哪里？
**答案**：放在消费者侧。例如 qwen3 在构造布局**之前**先用 checked 乘法算总字节数（见 4.4.3 引用的 `pegainfer-qwen3/src/weights.rs` L839-L849 的注释：`KvLayout::new` 用普通乘法推导步长，而全乘积支配所有部分乘积，先检查它即可），再用 `debug_assert_eq!` 与 `page_stride × 2` 对账。

### 4.3 页优先排布：每页内 [层][K|V]

#### 4.3.1 概念说明

三个步长不仅给出「多大」，还隐含规定了显存里的**嵌套顺序**。本 crate 是**页优先**（page-first）：最外层是页，页内按层排，层内 K 在前 V 在后：

```text
物理缓冲（一维 bf16 数组，单位 = 元素下标）
┌─────────────────── 第 0 页 ───────────────────┐┌── 第 1 页 ──┐
│ L0K │ L0V │ L1K │ L1V │ … … │ L31K │ L31V ││ L0K │ L0V │ …
└← kv_block_len →← kv_block_len →
│←────── layer_stride ────────→│
└←───────────── page_stride ──────────────────→┘
```

对比假想的**层优先**（layer-first）排布：最外层是层，每层内部再按页排：

```text
│←── 第 0 层：页0K|V │ 页1K|V │ … ──→│←── 第 1 层：页0K|V │ … ──→│ …
```

两种排布装的数据一样多，区别在「谁是连续的」：

| 维度 | 页优先（本 crate） | 层优先 |
|------|-------------------|--------|
| 一页（所有层、16 token） | **一段连续的 `page_stride`** | 散成 `num_layers` 段 |
| 一层（所有页） | 等距跨页跳，步长 `page_stride` | 连续 |
| 整页拷贝 / 换页 / offload | 一次 memcpy | `num_layers` 次 memcpy |
| 与池的块粒度对齐 | 完全对齐（块=页=管理单位） | 错位 |

引擎选择页优先，因为**管理的单位是块**：池发放/共享/回收的是整页，offload 桥搬运的是整页，投机验证链备份的还是整页——让「一页连续」赢过「一层连续」。至于注意力内核按层读取时跨页等距跳步，交给内核的页表（page table）加步长处理即可（页表与内核契约是 u2-l3 的主题）。

#### 4.3.2 核心流程

给定页号 `p`，页基址（字节）一步算出：

\[
\text{base}_p = \text{buffer\_ptr} + p \times \text{page\_stride} \times 2\,\text{B}
\]

页内第 \( l \) 层的 K 段起点在 \( l \times \text{layer\_stride} \)，V 段再偏移 \( \text{kv\_block\_len} \)。整页搬运 = 一次长度 `page_stride × 2` 字节的 device-to-device 拷贝。

#### 4.3.3 源码精读

- [src/buffer.rs:L26-L46](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L26-L46) —— `KvBuffer::new`：先用五个参数构造 `KvLayout`，再算 `total_elements = num_blocks * layout.page_stride`，然后 `stream.alloc_zeros(total_elements)` **一次性**分配整块 `CudaSlice<bf16>`。这就是「页优先」落到物理层的样子：一个一维大数组，页只是其中等宽的条带。
- [src/buffer.rs:L72-L99](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L72-L99) —— `copy_page` 的文档注释点明页优先的红利：一整页（每层的 K/V、`page_size` 个 token）就是页优先布局里**一段连续的 `page_stride` 区间**。
- [src/buffer.rs:L90-L98](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L90-L98) —— 拷贝的地址算术：`stride = layout.page_stride`、`elem = size_of::<bf16>()`，源/目标地址 = 基址 + `页号 × stride × elem`，最后一次 `memcpy_dtod_async`。若排布是层优先，同样的语义要拆成 `num_layers` 次不连续拷贝。注释还说明目标可以是池块数之外的 scratch 页（u2-l4 展开）。
- [src/pool.rs:L104-L123](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L104-L123) —— 容量访问器全部以「块」为单位（`block_size`/`available_blocks`/`total_blocks`/`max_request_blocks`），一个块对应物理上的一页——管理粒度与连续区间严格对齐。

#### 4.3.4 代码实践（手推地址，纸笔即可）

1. **实践目标**：用 4.2 的示例几何，亲手定位一个具体元素的地址，体会三个步长各自跨过什么。
2. **操作步骤**：对 \( L=32, H=8, D=128, P=16 \)，求「第 5 页、第 10 层的 **V** 段、页内第 3 个 token、第 2 个 KV 头、头内第 64 维」的元素下标（块内按 token 主序示意展开）：
   - `5 × page_stride = 5 × 1048576 = 5242880`
   - `+ 10 × layer_stride = + 327680` → `5570560`
   - `+ 1 × kv_block_len`（选 V）`= + 16384` → `5586944`
   - `+ (3 × 8 + 2) × 128 + 64 = 3328 + 64` → **`5590336`**
   - 字节地址 = 基址 + `5590336 × 2` = 基址 + `11180672`。
3. **需要观察的现象**：三个步长分别吃掉了页间、层间、K→V 三级跳；剩下的 `(t × H + h) × D + d` 是块内微观寻址（由内核约定，`KvLayout` 只保证 `kv_block_len` 这一段的长度）。
4. **预期结果**：元素下标 `5590336`、字节偏移 `11180672`。可再对照 `copy_page` 的算术验证：页 5 的页基址字节偏移正是 `5 × 1048576 × 2 = 10485760`，你算的地址落在本页内第 `694912` 字节处（= 347456 元素 = 10 层 `327680` + V 段 `16384` + 块内偏移 `3392`，自洽）。

#### 4.3.5 小练习与答案

**练习 1**：页优先排布下，注意力内核读「第 10 层、第 0..N 页」时地址如何跳？层优先呢？
**答案**：页优先下第 10 层第 p 页的 K 段起点 = `p × page_stride + 10 × layer_stride`——跨页时按固定步长 `page_stride` 等距跳，内核可用页表 + 乘加完成；层优先下则是 `10 × (总页数 × 单层页宽) + p × 单层页宽`，一层之内连续。两种都能算，区别在连续性的归属。

**练习 2**：`copy_page` 为什么敢断言「一页 = 一个连续 `page_stride` 区间」？这个断言依赖哪个排布决策？
**答案**：依赖页优先决策——页是最外层维度，所以一页的所有层、所有 K/V 都落在同一页基址起的 `page_stride` 个连续元素里。若改成层优先，同一页的数据会被 `num_layers` 个大段切开。

**练习 3**：MLA 模型（kimi-k2）不走 `KvCacheManager` 门面（见 [src/manager.rs:L10-L14](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L10-L14) 的文档注释），那 `KvLayout` 对它们还有意义吗？
**答案**：有但角色不同。MLA 的物理缓冲是自持的 ckv/kpe 双缓冲（不是本 crate 的 `KvBuffer`），但逻辑侧仍直接消费 `BlockPool` 的块 ID 发放；`KvLayout` 描述的是全注意力融合缓冲的几何，MLA 按自己的几何自行推导步长（本讲不展开，见 u2-l4 与各模型讲义）。

### 4.4 从布局到显存字节数的换算

#### 4.4.1 概念说明

步长是「元素个数」，显存规划要的是「字节」。换算只差一个因子：

\[
M_{\text{page}} = \text{page\_stride} \times 2\,\text{B}, \qquad
M_{\text{total}} = N_{\text{blocks}} \times M_{\text{page}}
\]

这个换算是引擎启动时「**给定空闲显存，能开多大的池**」预算问题的核心：先算一页多少字节，再用空闲字节数除出块数。反过来，每 token 的 KV 开销是：

\[
m_{\text{token}} = 2 \,(\text{K 与 V}) \times L \times H \times D \times 2\,\text{B}
\]

示例几何下 \( m_{\text{token}} = 2 \times 32 \times 8 \times 128 \times 2 = 131072\,\text{B} = 128\,\text{KiB} \)——这就是 KV 缓存「随序列线性增长」的增长率。GQA 把因子从 Query 头数（32）压到 KV 头数（8），直接把这笔账缩小 4 倍。

#### 4.4.2 核心流程

```text
模型配置 (L, H, D) + page_size P
   │  KvLayout::new(L, H, D, P)
   ▼
page_stride（元素/页）
   │  × size_of::<bf16>()
   ▼
一页字节数 M_page
   │  空闲显存 ÷ M_page
   ▼
可分配块数 N ──→ KvBuffer::new(..., num_blocks = N)：total_elements = N × page_stride
```

#### 4.4.3 源码精读

- [src/buffer.rs:L34-L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L34-L38) —— 总量换算的现场：`total_elements = num_blocks * layout.page_stride`，随后 `alloc_zeros` 申请这么多 **bf16 元素**（cudarc 按元素数分配，字节数由 `bf16` 类型定）。
- [pegainfer-qwen3/src/weights.rs:L820-L825](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L820-L825) —— 消费者侧的公式唯一化实践：`kv_page_bytes` 自述为「一页 KV 值多少钱」公式的**唯一持有者**，所有要给 KV 页记账的地方（投机 hedge 的 scratch、预算）都调它，禁止各自重推导。
- [pegainfer-qwen3/src/weights.rs:L838-L861](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L838-L861) —— `kv_bytes_per_block` 的实现：先用 checked 乘法算 \( L \times 2 \times H \times D \times P \times 2\,\text{B} \)（注释说明全乘积支配一切部分乘积，先检查它即可清空所有溢出风险），再构造 `KvLayout` 并用 `debug_assert_eq!` 对账 `bytes == layout.page_stride * size_of::<bf16>()`——两条独立推导互为回归锚。
- [pegainfer-qwen3/src/weights.rs:L34-L36](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L34-L36) —— 现实约束：默认 `page_size = 16`，且合法值限定 `{16, 64}`——不是任意偏好，而是 FlashInfer 分页注意力内核能接受的页尺寸（注释引 #545）。几何不是自由的，要迁就内核。

#### 4.4.4 代码实践（纯手算 + 对照源码）

1. **实践目标**：独立完成一次「预算反推」，并理解 qwen3 的对账断言。
2. **操作步骤**：
   1. 换一个几何：\( L=36, H=8, D=128, P=16 \)（36 层、8 KV 头）。手算 `kv_block_len`、`layer_stride`、`page_stride` 与一页字节数。
   2. 打开上面 `kv_bytes_per_block` 的链接，把你的算式与 L842-L849 的 checked 乘法逐因子对照。
   3. 思考并在笔记里回答：`debug_assert_eq!`（L856-L859）若在 CI 的 release 构建中不生效，为什么它仍值得写？
3. **需要观察的现象**：你的手算乘法链与 qwen3 的因子顺序一致（层数 × 2 × KV 头 × 头维 × 页大小 × 2 字节）。
4. **预期结果**：`kv_block_len = 16384`、`layer_stride = 32768`、`page_stride = 36 × 32768 = 1179648`、一页 = `2359296` B = **2.25 MiB**（不是 2 的幂！这正是练习的意义——别依赖幂次直觉）。

#### 4.4.5 小练习与答案

**练习 1**：示例几何（2 MiB/页）下，一张卡划出 18 GiB 给 KV 池，能开多少块？
**答案**：\( 18 \times 1024\,\text{MiB} / 2\,\text{MiB} = 9216 \) 块。其中 1 块会被池的 padding 槽吃掉（4.1.3），可服务请求的理论上限是 9215 块（`max_request_blocks` 还要再受调度约束，见 u2）。

**练习 2**：为什么 qwen3 把「一页字节数」公式的持有权集中到一个函数，而不是各处直接乘？
**答案**：防漂移。几何公式一旦散落多处，将来改页尺寸合法性、加 dtype 或计费因子时极易改漏一处。集中持有 + `debug_assert` 对账，让任何一处不一致在开发期就炸出来（这正是 CLAUDE.md 说的「每个改动文件都要服务同一个生产不变量」的微观体现）。

## 5. 综合实践

**任务：做一次真实的 KV 显存规划，并写成可运行的断言。**

场景：你要在一块 24 GB 卡上部署示例几何（\( L=32, H=8, D=128, P=16 \)）的全注意力模型。权重与激活预留 6 GiB，其余全部给 KV 池。

1. **手算预算**：可用 = \( 24 - 6 = 18\,\text{GiB} = 18432\,\text{MiB} \)；由 4.4 已知一页 2 MiB → 块数 = \( 18432 / 2 = 9216 \)。
2. **写成测试**：在 4.2.4 的 `tests/layout_geometry_practice.rs` 里追加（示例代码）：

   ```rust
   #[test]
   fn kv_budget_for_24gb_card() {
       let layout = KvLayout::new(32, 8, 128, 16);
       let page_bytes = layout.page_stride * 2;
       let budget_bytes = 18 * 1024 * 1024 * 1024usize;
       assert_eq!(page_bytes, 2 * 1024 * 1024);      // 2 MiB / 页
       assert_eq!(budget_bytes / page_bytes, 9216);  // 可开 9216 块
   }
   ```

3. **运行**：`cargo test --release -p pegainfer-kv-cache --test layout_geometry_practice`（待本地验证；预期 2 个测试全绿）。
4. **变式思考**（写进你的笔记）：
   - 把 `page_size` 提到 64（qwen3 的另一个合法页尺寸）：每页 8 MiB，块数变 2304——块更少但内部碎片更小、单页搬运更宽，这是页大小的基本权衡；
   - 你的预算里没有扣 padding 块——回看 [src/pool.rs:L120-L123](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L120-L123) 的 `max_request_blocks`，说明实际可用为何是 `total - 1`。
5. **交付物**：一个两测试文件 + 一段 200 字左右的「页大小取舍」笔记（碎片 vs 搬运宽度 vs 内核合法值）。

## 6. 本讲小结

- 分页 KV 用「固定大小、可散布的页」同时消灭外部碎片、使前缀块按页共享、给 CUDA Graph 提供永不移动的指针，并让换页/offload 退化成一次连续拷贝。
- 逻辑层 `BlockPool` 只发放块 ID 不持显存；物理层 `KvBuffer` 一次性 `alloc_zeros` 出 `num_blocks × page_stride` 个 bf16 元素、地址终身稳定；二者靠块 ID（=页号）关联。
- 三个步长是一条纯乘法链：`kv_block_len = P×H×D`、`layer_stride = 2×kv_block_len`（K 前 V 后）、`page_stride = L×layer_stride`；`KvLayout` 是无 GPU、无分配的纯值类型。
- 页优先排布让「一页 = 一段连续 `page_stride`」，与池以块为管理粒度完全对齐；`copy_page` 的一次 `memcpy_dtod_async` 就是这个决策的直接红利。
- 字节换算只差 `× 2`（bf16）；qwen3 把「一页多少字节」集中到 `kv_page_bytes` 唯一持有，并用 `debug_assert_eq!` 与 `page_stride × 2` 对账；页尺寸不是自由参数，要服从内核的合法值集合（`{16, 64}`）。

## 7. 下一步学习建议

- **下一讲 u2-l1（BlockPool：逻辑块池与 padding 块）**：进入池的内部——`BlockManager` 的构建参数（`BlockDuplicationPolicy::Allow`、LRU 后端）、padding 块为何要 `std::mem::forget`，以及本讲反复出现的容量 API 的准确语义。
- **再往后 u2-l3（KvView 与注意力内核的契约）**：页表如何把本讲的页几何交给 paged attention 内核，理解「页数必须恰好覆盖 seq_len」的硬契约。
- **延伸阅读**：对照 [src/buffer.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L1-L100) 的 `device_ptr` 注释与兄弟 crate `pegainfer-kv-offload` 如何用「基址 + 步长」直接寻址每一段 K/V，体会「布局即接口」。
