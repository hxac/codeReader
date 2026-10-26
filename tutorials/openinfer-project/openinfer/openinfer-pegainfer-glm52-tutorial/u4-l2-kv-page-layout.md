# KV 页布局：64-token 页、MLA 与 index-K 切片

> 讲义 ID：u4-l2 · 阶段：intermediate · 依赖：u4-l1（MLA 前半）、u2-l1（模型几何常量）

## 1. 本讲目标

学完本讲，你应该能：

1. 解释 GLM5.2 的 KV 缓存为什么以 64 token 为页，以及这个「64」同时扮演的三重角色：FlashMLA 稀疏页、index-K 缓存块、上下文上限对齐单位。
2. 独立算出**一页（64 token）的内容字节构成**：78 个 MLA 切片 + 21 个 full-indexer 层的 index-K 切片 + 第 78 层 MTP 镜像 = 3,502,592 B，页距（stride）= 3,503,040 B。
3. 说明 cap（单请求上下文上限）为什么必须 64 对齐：页表宽 = cap / 64，非整数的 cap 会留下页表无法寻址的「半页」。
4. 理解**悬空 token（dangling token）契约**：每个请求按全生命周期预留 `ceil((prompt + max_tokens) / 64)` 页——比真正写 KV 的位置多算一个 token 的页。
5. 说清 padding 页（BlockPool 的 block 0）的角色：free-running 引擎的 padding 行与 CUDA Graph 预捕获步的「垃圾写入」全部落进这一页，谁都读不到。

## 2. 前置知识

本讲在 u4-l1（MLA 前半）和 u2-l1（几何常量）之上继续，先复述三个会用到的结论：

- **MLA 的 latent KV（u4-l1）**：GLM5.2 每层把 hidden 投影成压缩的 kv_a 表示，decode 时以 fp8_ds_mla 布局持久化，**每个 token 每层固定 656 字节**。这个 656 来自 `pegainfer-kernels` 的常量 `GLM52_FLASHMLA_SPARSE_BYTES_PER_TOKEN`，其字节的内部构成要到 u4-l3（MLA 解码后半）才拆开；本讲把它当「每 token 每层 656 B」的黑盒。
- **78 层与 21 个 full indexer 层（u2-l1）**：`GLM52_LAYERS = 78`，其中带独立 DSA 索引器权重的层由谓词 `glm52_layer_has_full_indexer` 选出，共 21 层。u2-l1 只给了谓词；本讲第一次看到它的**真实消费点**——它决定每层在一页里有没有 index-K 切片。
- **fp8 e4m3 + 逐组 scale（u3-l1）**：index-K 切片是「128 B 的 fp8 key + 4 B 的 f32 scale」按 DeepGEMM 交错块布局存放，即每 token 132 B。

再补两个本讲要用到的一般概念：

- **paged KV（分页 KV 缓存）**：把一个请求的历史 KV 切成固定大小的「页」分散存放，用一张页表（page table）记录「第几个 64-token 段对应哪个物理页号」。好处是显存碎片可控、请求只占它真正需要的页数。
- **TMA（Tensor Memory Accelerator）**：Hopper/Blackwell GPU 上的 DMA 引擎，kernel 用它按固定步长批量搬运显存。FlashMLA 稀疏 kernel 用 TMA 读 KV 页，**每页步长直接取页距**——这解释了本讲最重要的约束：页距必须是 656（每 token 行宽）的整数倍。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| `src/model/mod.rs` | 页几何的唯一权威：页/切片/内容/页距常量、页布局函数、`glm52_pool_blocks` 与 `glm52_table_width` 推导、`finish_kv` 分配 slab、`Glm52StepKv` 步内页表 |
| `src/layer.rs` | 页的消费者视角：`Glm52LayerCaches` 偏移、`Glm52KvSlab` 结构、`Glm52DecodeStep` 把页表带进每层前向 |
| `src/config.rs` | 几何常量源头：78 层、`GLM52_INDEX_HEAD_DIM = 128`、full-indexer 谓词 |
| `src/scheduler/admission.rs` | 悬空 token 契约的预算侧：`lifetime_blocks` |
| `src/lib.rs` | `BlockPool` 构造（来自 `pegainfer-kv-store`）、`GLM52_KV_POOL_BLOCKS` 两道栅栏、悬空页池单测 |
| `src/model/mtp.rs` | `MTP_SCRATCH_PAGES_PER_SLOT`（slab 尾部的 MTP scratch 页） |

## 4. 核心概念与源码讲解

### 4.1 页几何常量与推导

#### 4.1.1 概念说明

GLM5.2 的 KV 不是「一整条大数组按请求切段」，而是一个 **rank 级页池**：一大块 slab 显存被切成等大的页，**每页恰好装 64 个 token 在全部 78 层（外加 MTP 镜像）的缓存内容**。这种「先按 token 分页、页内再按层切」的布局叫 page-first（页优先），与之相对的是 layer-first（每层一张大数组）。

先看 crate 的自述：[src/model/mod.rs:L6-L10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L6-L10) 这段模块文档一句话点题——KV 活在 64-token 页构成的 rank 级池里，rank 引擎的 `BlockPool` 给请求分页，每步的 `Glm52StepKv` 携带每行的页表行和平铺写槽，padding 行骑在池里保留的 padding 页上，原文形容为「whose garbage writes nobody reads」（垃圾写入无人读）。

「64」在这套几何里同时是三个东西，一处定义、处处同一个值：

| 身份 | 常量 | 定义/导入处 |
|---|---|---|
| FlashMLA 稀疏注意力页 | `GLM52_FLASHMLA_SPARSE_PAGE_SIZE` | `pegainfer-kernels`，[src/model/mod.rs:L34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L34) 导入 |
| index-K 缓存块（DeepGEMM paged MQA 的块） | `INDEX_CACHE_BLOCK` | [src/model/mod.rs:L408](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L408) |
| 上下文上限对齐单位 | `GLM52_MODEL_LEN_ALIGN` | [src/model/mod.rs:L116](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L116) |

调度器侧还给它起了第四个名字：[src/scheduler/mod.rs:L102-L104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L102-L104) 定义 `const PAGE = GLM52_MODEL_LEN_ALIGN`，注释写明「one 64 everywhere」。三个角色共用一个 64 不是巧合：稀疏解码按页读 MLA、索引器按块读 index-K、页表按页寻址，若三者粒度不一致，同一段 token 的两种缓存就会落在不同的块边界上，寻址与换页都要做特判。统一成 64 之后，「第 p 页」同时是 MLA 的第 p 个 TMA 页、index-K 的第 p 个块、页表的第 p 项。

#### 4.1.2 核心流程

一页的内容按「层序」依次摆放，生成规则（伪代码，与 `glm52_page_layout` 一一对应）：

```text
offset = 0
for layer in 0..78:                                      # 78 个 decoder 层
    mla_offset[layer] = offset;  offset += 64 * 656       # MLA 切片 41,984 B（每层必有）
    if glm52_layer_has_full_indexer(layer):               # 仅 21 个 full indexer 层
        index_k_offset[layer] = offset;  offset += 64 * 132   # index-K 切片 8,448 B
# 第 78 层 MTP「提交镜像」：1 个 MLA 切片 + 1 个 index-K 切片
mtp.mla_offset = offset;  mtp.index_k_offset = offset + 41,984
offset += 41,984 + 8,448
断言 offset == 3,502,592
```

其中 21 个 full indexer 层的集合是 \(\{0,1,2\}\cup\{6,10,14,\dots,74\}\)。注意谓词里的 `saturating_sub`：层 0 和层 1 减 2 都饱和到 0，所以前 3 层（恰好是 dense MLP 层）全是 full indexer 层——这是 u2-l1 那句「层号饱和减 2 再模 4」容易读漏的细节。

逐项算字节（MLA 切片 = 64 token × 656 B/token；index-K 切片 = 64 token × (128 B fp8 key + 4 B f32 scale)）：

\[ \text{MLA 切片} = 64 \times 656 = 41{,}984 \text{ B/层/页} \qquad \text{index-K 切片} = 64 \times (128+4) = 8{,}448 \text{ B/层/页} \]

\[ \text{页内容} = 78 \times 41{,}984 + 21 \times 8{,}448 + (41{,}984 + 8{,}448) = 3{,}274{,}752 + 177{,}408 + 50{,}432 = 3{,}502{,}592 \text{ B} \]

页距（相邻页首的字节距离）把内容向上取整到 656 的整数倍：

\[ \text{页距} = \lceil 3{,}502{,}592 / 656 \rceil \times 656 = 5{,}340 \times 656 = 3{,}503{,}040 \text{ B} \quad (\text{尾部填充} = 448 \text{ B} < 656) \]

为什么必须取整到 656 的倍数？FlashMLA 的 TMA 用页距推导「读下一页走多远」，步长若不是整 token 行，页尾会切开一行 656 B 的缓存行。页距因此成为**线格式身份（wire-layout identity）**：KV 卸载的 namespace 和 native P/D 的 fingerprint 都把它折叠进去（u7-l5、u9-l4 会再遇到它）。

再往外一层，从「一页」推到「一个 rank 的池与页表」（cap 是单请求上下文上限，64 对齐）：

\[ \text{页表宽} = \lceil \text{cap} / 64 \rceil \qquad \text{池块数} = \text{slots} \times \lceil (\text{cap}+1) / 64 \rceil + 1 \]

页表宽公式没有「多一」，池块数公式却有两处「多一」：每 slot 悬空 token 的 +1 页（4.3 详解），以及 block 0 的 padding 页。cap 必须 64 对齐的原因藏在页表宽公式里：页表只能以整页为单位寻址，一个非 64 倍数的 cap 会让最后一个 token 落在「表宽覆盖不到的半页」上——干脆禁止。

#### 4.1.3 源码精读

**① 对齐单位与池/表推导。** [src/model/mod.rs:L112-L137](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L112-L137)：`GLM52_MODEL_LEN_ALIGN` 的文档注释直接回答了本讲目标 3——非 64 倍数的 cap 会「搁浅一个页表无法寻址的半页」；`glm52_pool_blocks` 与 `glm52_table_width` 就是上面两个公式的源码形态（节选）：

```rust
// 节选自 src/model/mod.rs:L128-L137
pub(crate) fn glm52_pool_blocks(max_model_len: usize, pool_slots: usize) -> usize {
    pool_slots * (max_model_len + 1).div_ceil(GLM52_FLASHMLA_SPARSE_PAGE_SIZE) + 1
}
pub(crate) fn glm52_table_width(max_model_len: usize) -> usize {
    max_model_len.div_ceil(GLM52_FLASHMLA_SPARSE_PAGE_SIZE)
}
```

**② 四个字节常量与编译期断言。** [src/model/mod.rs:L139-L163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L139-L163)：MLA 切片、index-K 切片、页内容、页距四个常量，全部是纯乘法或字面量；两条 `const assert` 把「页距是 656 的倍数」「页距就是内容向上取整的最小 656 倍数」变成编译期事实（节选）：

```rust
// 节选自 src/model/mod.rs:L142-L163
pub(crate) const GLM52_KV_PAGE_MLA_BYTES: usize =
    GLM52_FLASHMLA_SPARSE_PAGE_SIZE * GLM52_FLASHMLA_SPARSE_BYTES_PER_TOKEN;
pub(crate) const GLM52_KV_PAGE_IDXK_BYTES: usize =
    INDEX_CACHE_BLOCK * (GLM52_INDEX_HEAD_DIM + 4);
pub(crate) const GLM52_KV_PAGE_CONTENT_BYTES: usize = 3_502_592;
pub(crate) const GLM52_KV_PAGE_STRIDE: usize = 3_503_040;

const _: () = assert!(GLM52_KV_PAGE_STRIDE % GLM52_FLASHMLA_SPARSE_BYTES_PER_TOKEN == 0);
const _: () = assert!(
    GLM52_KV_PAGE_STRIDE >= GLM52_KV_PAGE_CONTENT_BYTES
        && GLM52_KV_PAGE_STRIDE - GLM52_KV_PAGE_CONTENT_BYTES
            < GLM52_FLASHMLA_SPARSE_BYTES_PER_TOKEN
);
```

注意 `GLM52_KV_PAGE_MLA_BYTES` 的文档注释（L139-L141）还预告了 P/D 的一致性：**每个拓扑都持久化这一行**——prefill-only 的线格式就是 EP decode 消费者的缓存格式（u5-l4 呼应）。

**③ 页布局的唯一构造源。** [src/model/mod.rs:L165-L203](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L165-L203)：`Glm52PageLayout` 结构与 `glm52_page_layout()` 函数。文档注释强调它是 `finish_kv`、MTP attach 与布局单测**三处共享的同一个构造**——偏移表不可能与线格式常量漂移；函数末尾的 `assert_eq!(offset, GLM52_KV_PAGE_CONTENT_BYTES)` 把 4.1.2 的手算变成运行期防线（节选）：

```rust
// 节选自 src/model/mod.rs:L180-L201
for layer in 0..GLM52_LAYERS {
    let mla_offset = offset;
    offset += GLM52_KV_PAGE_MLA_BYTES;
    let index_k_offset = glm52_layer_has_full_indexer(layer).then(|| {
        let at = offset;
        offset += GLM52_KV_PAGE_IDXK_BYTES;
        at
    });
    layers.push(Glm52LayerCaches { mla_offset, index_k_offset });
}
let mtp = Glm52LayerCaches {
    mla_offset: offset,
    index_k_offset: Some(offset + GLM52_KV_PAGE_MLA_BYTES),
};
offset += GLM52_KV_PAGE_MLA_BYTES + GLM52_KV_PAGE_IDXK_BYTES;
assert_eq!(offset, GLM52_KV_PAGE_CONTENT_BYTES, ...);
```

谓词本体在 [src/config.rs:L64-L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L64-L68)：

```rust
// 节选自 src/config.rs:L64-L68
pub(crate) fn glm52_layer_has_full_indexer(layer: usize) -> bool {
    layer
        .saturating_sub(GLM52_INDEX_SKIP_TOPK_OFFSET - 1)
        .is_multiple_of(GLM52_INDEX_TOPK_FREQ)
}
```

其中频次 4 与偏移 3 定义于 [src/config.rs:L53-L56](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L53-L56)（`GLM52_INDEX_TOPK_FREQ = 4`、`GLM52_INDEX_SKIP_TOPK_OFFSET = 3`）。

**④ 线格式测试。** [src/model/mod.rs:L1673-L1711](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1673-L1711) 的 `page_layout_matches_the_wire_constants` 把本讲的全部数字钉死：内容 3,502,592、页距 3,503,040、页距等于内容的 `next_multiple_of(656)`、78 层、恰好 21 个 `index_k_offset.is_some()`、切片按声明序 16 字节对齐且互不越界、层 0 的 index-K 偏移是 `Some(41_984)`、MTP 的 index-K 偏移是「内容减 8,448」。这是一条**纯 CPU 单测**（没有 `#[ignore]` 标记），是本讲实践的核对工具（节选）：

```rust
// 节选自 src/model/mod.rs:L1674-L1690
let layout = glm52_page_layout();
assert_eq!(GLM52_KV_PAGE_CONTENT_BYTES, 3_502_592);
assert_eq!(GLM52_KV_PAGE_STRIDE, 3_503_040);
assert_eq!(
    GLM52_KV_PAGE_STRIDE,
    GLM52_KV_PAGE_CONTENT_BYTES.next_multiple_of(GLM52_FLASHMLA_SPARSE_BYTES_PER_TOKEN)
);
assert_eq!(layout.layers.len(), GLM52_LAYERS);
assert_eq!(
    layout.layers.iter()
        .filter(|caches| caches.index_k_offset.is_some())
        .count(),
    21
);
```

#### 4.1.4 代码实践

**实践 A：手算一页的内容，与线格式测试对账。**

1. 实践目标：不依赖运行，徒手复现 4.1.2 的三步乘加，确认你能从源码常量推出 `3_502_592` 与 `3_503_040`。
2. 操作步骤：
   - 从 [src/config.rs:L11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L11)（78 层）与 [src/config.rs:L54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L54)（index head dim 128）出发，写出两个切片字节数；
   - 用谓词数出 21 个 full indexer 层（记得 `saturating_sub` 让层 0、1、2 全入选）；
   - 相加得内容，再取 656 的倍数得页距。
3. 需要观察的现象：你的手算值与 `GLM52_KV_PAGE_CONTENT_BYTES = 3_502_592`、`GLM52_KV_PAGE_STRIDE = 3_503_040` 完全一致，尾填充 448 B。
4. 预期结果：三项乘积 3,274,752 / 177,408 / 50,432 与总和 3,502,592。
5. 可选核对（需能编译 glm52 feature 的环境，build.rs 要求 nvcc 与 Blackwell 目标）：`cargo test --release -p pegainfer-glm52 --features glm52 --lib page_layout`——该测试是纯 CPU 断言。本讲撰写环境未运行此命令，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：假想把 `GLM52_INDEX_HEAD_DIM` 改成 192（仅作算术练习），页内容与页距变成多少？

答案：index-K 切片 = 64 × (192+4) = 12,544 B；页内容 = 3,274,752 + 21×12,544 + (41,984+12,544) = 3,274,752 + 263,424 + 54,528 = 3,592,704 B；页距 = ⌈3,592,704/656⌉×656 = 5,478×656 = 3,593,568 B。两个 `const assert` 会在编译期重新把关页距的 656 倍数约束。（示例计算，源码并未这样改。）

**练习 2**：为什么 `cap = 4100` 不被接受？

答案：4100 不是 64 的倍数。页表宽按整页寻址，落不进整数页的 cap 会「搁浅」一个页表无法寻址的半页（[src/model/mod.rs:L112-L115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L112-L115) 的注释原话）；u2-l4 讲过 `derive_max_model_len` 的四道门里就有 64 对齐这一道（`GLM52_MODEL_LEN_ALIGN`）。

**练习 3**：78 层里 full indexer 层和 shared 层各多少？77 号层是哪种？

答案：full = {0,1,2} ∪ {6,10,…,74} 共 21 层；shared = 57 层。层 77：77 饱和减 2 = 75，75 不是 4 的倍数，是 shared 层（它复用层 74 的 top-k，见 u4-l4 的跨层共享）。

### 4.2 Glm52KvSlab 与层缓存

#### 4.2.1 概念说明

页几何只回答「一页长什么样」；把页变成可用的缓存还需要两样东西：**一块按页切分的大显存**（`Glm52KvSlab`）和**一张每层的页内偏移表**（`Glm52LayerCaches` 的 78+1 项数组）。运行期任何一个 KV 字节的地址都是三要素之和：

```text
地址 = slab 基址 + 物理页号 block_id × page_stride + 层内偏移 mla_offset 或 index_k_offset
```

三要素各来自一处：基址与页距来自 slab 分配；物理页号是**运行期数据**（引擎每步把页表上传进设备 buffer，被捕获的 CUDA Graph 只读那些 buffer——文档注释原话是「a page physical id is data, never baked」，页的物理 id 是数据、绝不烘焙进图）；层内偏移是纯架构函数（4.1 的 `glm52_page_layout`）。

两个容易混淆的「MTP 相关页」要分清：

- **MTP 提交镜像（committed mirror）**：每页末尾的 1 个 MLA 切片 + 1 个 index-K 切片，**是每页的一部分**。native MTP 提交的第 78 层 KV 与目标层同页，radix/前缀命中时按池页号整体复用（u7-l5、u9-l3）。
- **MTP scratch 页**：池区域**之后**追加的整页，每 decode slot 2 页（`MTP_SCRATCH_PAGES_PER_SLOT`），给草稿轮写草稿 KV 用，不占 `pool_blocks`。

#### 4.2.2 核心流程

slab 的分配时机承接 u2-l4 的两阶段 build：

1. `build_fixed` 建好一切**与池规模无关**的东西（权重、rope 表、每个 bucket 的调度元数据与 block 表——block 表按 `glm52_table_width` 定长）。
2. launch 实测 fleet 各 rank 的空闲显存，决定 `pool_blocks`（u2-l4 的实测填充与两道栅栏）。
3. `finish_kv` 才分配池规模化的一切。它依次：
   - 校验 `pool_blocks > 0`，并校验持久布局确实是每 token 656 B 的 fp8_ds_mla（576 B 的 FlashInfer 布局在 slab 里没有家）；
   - 用 `glm52_index_cache_layout(max_model_len, pool_blocks)` 造 index-K 缓存布局，并断言页表宽与 `build_fixed` 一致（布局漂移直接启动失败）；
   - 给每个 bucket 的 sched/scratch 上报块数；
   - 取 `glm52_page_layout()` 的偏移表；
   - `slab_blocks = pool_blocks + MTP scratch 页数`，`slab_bytes = slab_blocks × 3,503,040 + 3,502,592`，一次 `alloc_zeros` 落位；
   - MTP 与 prefill 执行器各自 attach 到这个池。

为什么 slab 末尾要多加「一页内容」的尾冗余（tail slack）？kernels 层的越界检查是保守的（按「层偏移 + 块数 × 页距」估计最大偏移），最高偏移的切片（MTP 镜像）会超出按此估计的紧界一点点；与其每层做紧界检查，不如整体多给一页内容的余量。这也解释了 `glm52_arena_bytes` 的形状：`num_blocks × stride + CONTENT`。

#### 4.2.3 源码精读

**① 偏移表与 slab 结构。** [src/layer.rs:L87-L111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L87-L111)：`Glm52LayerCaches` 的文档注释就是本讲的浓缩——「fp8_ds_mla MLA 切片（64 × 656 B）与 full-indexer 层的 DeepGEMM 布局 index-K 切片（64 × 132 B）；字节偏移按 block × page_stride 寻址所属的 Glm52KvSlab」；`Glm52KvSlab` 本体只有三个字段（节选）：

```rust
// 节选自 src/layer.rs:L93-L111
#[derive(Clone, Copy, Debug)]
pub(crate) struct Glm52LayerCaches {
    pub(crate) mla_offset: usize,
    pub(crate) index_k_offset: Option<usize>,
}

pub(crate) struct Glm52KvSlab {
    pub(crate) slab: CudaSlice<u8>,
    pub(crate) page_stride: usize,
    pub(crate) num_blocks: usize,
}
```

**② finish_kv 的分配主体。** [src/model/mod.rs:L950-L1021](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L950-L1021)（节选，中间校验与解构略）：

```rust
// 节选自 src/model/mod.rs:L1001-L1021
let page = glm52_page_layout();
let scratch_blocks = mtp.as_ref().map_or(0, |mtp| {
    if mtp.slab_resident() {
        glm52_decode_slots() * mtp::MTP_SCRATCH_PAGES_PER_SLOT
    } else { 0 }
});
let slab_blocks = pool_blocks + scratch_blocks;
let slab_bytes = slab_blocks
    .checked_mul(GLM52_KV_PAGE_STRIDE)
    .and_then(|bytes| bytes.checked_add(GLM52_KV_PAGE_CONTENT_BYTES))
    .context("GLM5.2 KV slab byte count overflow")?;
let kv_slab = Glm52KvSlab {
    slab: ctx.stream.alloc_zeros::<u8>(slab_bytes)?,
    page_stride: GLM52_KV_PAGE_STRIDE,
    num_blocks: slab_blocks,
};
```

注意它同时把 `page.layers` 存进模型（`caches` 字段，[src/model/mod.rs:L1032-L1035](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1032-L1035)），把 `page.mtp` 交给 `mtp.attach_cache`（[src/model/mod.rs:L1022-L1024](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1022-L1024)）——页布局的唯一构造被两个消费者共享。

**③ index-K 与 MLA 同池。** [src/model/mod.rs:L249-L262](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L249-L262)：`glm52_index_cache_layout` 显式声明 index-K 切片「用与 MLA 切片相同的池块号索引」，`cache_blocks = num_blocks`、`cache_block_stride_bytes = GLM52_KV_PAGE_STRIDE`——两种缓存共享同一个页表，没有第二套块号体系。

**④ 每步的页表信封。** [src/model/mod.rs:L346-L366](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L346-L366)：`Glm52StepKv` 是调度层递给执行层的 KV 寻址信封（节选）：

```rust
// 节选自 src/model/mod.rs:L352-L366
pub(crate) struct Glm52StepKv {
    // [bucket, table_width] 行主序页号；覆盖不到的表项与全部 padding 行
    // 填池的 padding 页号
    pub(crate) pages: Box<[[i32]]>,
    // 每行平铺写槽：pages[position/64]*64 + position%64；
    // fp8_ds_mla 与 index-K 共享这一 token 下标空间
#[serde(with = "serde_step_rows")]
    pub(crate) slot_mapping: [[i64; GLM52_MAX_STEP_ROWS]],
    // native P/D 边界恢复：跨 arena 的 (src, dst) 页对，
    // 在本步 kernel 之前复制
    pub(crate) boundary_copies: Vec<(i32, i32)>,
}
```

（注释为讲义改写的中文摘要；原始英文注释见链接行号。）

**⑤ 前向如何消费页。** [src/layer.rs:L174-L187](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L174-L187)：`glm52_layer_attention_half` 的签名同时接住本讲的三个主角——`slab: &mut Glm52KvSlab`（页池）、`caches: Glm52LayerCaches`（本层页内偏移）、`step: &Glm52DecodeStep`（含 `slot_mapping` 与 `block_table` 两个设备 buffer，见 [src/layer.rs:L125-L127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L125-L127)）。78 层循环每层用同一张页表、不同的 `caches` 偏移，读写各自的页内切片。

**⑥ P/D 线格式一致性测试。** [src/model/mod.rs:L1661-L1671](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1661-L1671) 的 `tp4_prefill_matches_ep_decode_persistent_mla_layout` 断言：TP4 prefill 与 EP decode 的持久化都是 656 B/token、`GLM52_KV_PAGE_MLA_BYTES = 41_984`、`GLM52_KV_PAGE_IDXK_BYTES = 8_448`——P 侧写入的页就是 D 侧读出的页，换拓扑不换格式。

#### 4.2.4 代码实践

**实践 B：手推一张页内偏移表。**

1. 实践目标：按 4.1.2 的规则徒手写出若干层的 `mla_offset` 与 `index_k_offset`，直到你能不假思索地回答「任意一层的切片在哪」。
2. 操作步骤：从 offset = 0 开始，逐层累加；层 0、1、2 各吃一个 MLA 切片 + 一个 index-K 切片（各 50,432 B），层 3 起的 shared 层只吃 41,984 B，full 层吃 50,432 B。
3. 需要观察的现象：层 0 的 index-K 偏移 = 41,984；MTP 的 index-K 偏移 = 3,502,592 − 8,448 = 3,494,144。
4. 预期结果（对照 [src/model/mod.rs:L1705-L1710](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1705-L1710) 的断言）：

   | 对象 | mla_offset | index_k_offset |
   |---|---|---|
   | 层 0 | 0 | 41,984 |
   | 层 1 | 50,432 | 92,416 |
   | 层 2 | 100,864 | 142,848 |
   | 层 3 | 151,296 | 无 |
   | 层 6（首个 MoE full 层） | 277,248 | 319,232 |
   | 层 77 | 3,410,176 | 无 |
   | MTP（第 78 层镜像） | 3,452,160 | 3,494,144 |

5. 无需运行即可核对；若要机器对账，用 4.1.4 的同一条测试命令（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：层 5 的 `mla_offset` 是多少？有没有 index-K 切片？

答案：前 3 层各占 50,432 B，层 3、4 各占 41,984 B，所以层 5 的 mla_offset = 3×50,432 + 2×41,984 = 235,264；层 5 饱和减 2 = 3，不是 4 的倍数，没有 index-K 切片。

**练习 2**：为什么页布局函数要坚持「finish_kv、MTP attach、单测三处共用一个构造」？

答案：页内容/页距是线格式（wire）常量——KV 卸载的 namespace、native P/D 的 fingerprint 都折叠页距（[src/model/mod.rs:L152-L155](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L152-L155)）。若三处各写一份偏移表，任何一处改动都会让拷贝/恢复读到错位的字节；单一构造源 + 末尾 assert 让漂移在构造时就炸掉（文档注释 L165-L169 的原意）。

**练习 3**：slab 尾部那 3,502,592 B 的 tail slack 防的是什么？

答案：kernels 对越界的估计是保守的「层偏移 + 块数 × 页距」，最高偏移切片（页尾的 MTP 镜像）按这个公式会越出紧界；与其每层精确检查，不如整体多留一页内容的余量，让最高偏移也可寻址（[src/layer.rs:L98-L106](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L98-L106) 的注释）。

### 4.3 padding 页角色与悬空 token 契约

#### 4.3.1 概念说明

池块数公式里的两个「多一」各有出处，本模块把它们讲透。

**padding 页（block 0）。** `BlockPool` 把 0 号块永久保留为 padding 页：free-running 引擎每步都有可能空转（u7-l1 的纪律：空闲 rank 也要全速跑 padding 行），CUDA Graph 预捕获时也会先跑一遍全 padding 步（u6-l3）——这些行的 KV 写必须有确定的去处，否则会写到随机地址。去处就是 padding 页：模块文档（[src/model/mod.rs:L6-L10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L6-L10)）的原话是「whose garbage writes nobody reads」。0 号块从不分给任何请求，所以这些垃圾永远不可能被当成某个请求的历史读出来。

**悬空 token（dangling token）。** 请求生成最后一个 token 时，采样出的 token id 会被追加进序列（下一轮对话它就是 prompt 的一部分），但引擎不会再为它做一次前向、也就不写它的 KV——这个 token 就「悬空」在序列末尾。可它万一将来要被写（比如 MTP 提交、边界续写），它的页必须在 admission 时就预留好。于是预算公式按 `prompt + max_tokens`（比最后一个写 KV 的位置多一个 token）向上取整页数。admission 的注释还留了一句工程史：「与 Kimi 的 admission 不得不凭经验学会的同一个 off-by-one」。

两个「多一」对上号：`pool_slots × ceil((cap+1)/64) + 1` 里，乘积项的 +1 是**每个 slot** 的悬空 token 页（cap 对齐时 ceil((cap+1)/64) = cap/64 + 1），最外层的 +1 是**整个池**的 padding 页。

#### 4.3.2 核心流程

一个请求与页池的完整生命周期（细节在 u7-l4，这里只看页数）：

```text
validate_request:  prompt_tokens + max_tokens <= cap + 1      # 门口快速拒绝
admit:            lifetime_blocks = ceil((prompt+max_tokens)/64)  # 一次性预留全部页
decode 每步:       pages 表覆盖不到的表项、全部 padding 行 → 填 padding 页号
                  slot_mapping = pages[pos/64] * 64 + pos % 64    # 平铺写槽
finish:           RAII 归还全部页（含悬空页）→ 可被前缀缓存匹配     # u7-l5
```

写成公式：

\[ \text{请求全生命周期页数} = \lceil (\text{prompt} + \text{max\_tokens}) / 64 \rceil \]

页的**复制单位**也在这里定死：跨页搬运（前缀命中、P/D 边界恢复的 `boundary_copies`）永远只拷 3,502,592 B 的**内容**，尾部 448 B 填充从不移动（[src/model/mod.rs:L147-L150](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L147-L150)）；`copy_kv_page` 按此把「一页的全部层切片（MTP 镜像在内）」作为一个整体从共享页拷进请求自己的页。

#### 4.3.3 源码精读

**① 池块数与两个「多一」。** [src/model/mod.rs:L118-L130](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L118-L130)：注释把契约讲得很完整——「一个请求的全生命周期取页是 ceil((prompt+max_tokens)/page)，其中 prompt+max_tokens <= cap+1（validate_request 保证）：比它实际写的 KV 多一页，因为 kvbm 会追加最终生成的 token 并提前占好它的页（悬空 token 契约）」，且「引擎的 BlockPool 与 rank arena（finish_kv）必须对这个数达成一致：池块号直接索引 arena 的页」（节选）：

```rust
// 节选自 src/model/mod.rs:L128-L130
pub(crate) fn glm52_pool_blocks(max_model_len: usize, pool_slots: usize) -> usize {
    pool_slots * (max_model_len + 1).div_ceil(GLM52_FLASHMLA_SPARSE_PAGE_SIZE) + 1
}
```

**② admission 的预算侧。** [src/scheduler/admission.rs:L105-L112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/admission.rs#L105-L112)：

```rust
// 节选自 src/scheduler/admission.rs:L105-L112
/// Pool pages a request draws over its whole lifetime, reserved at
/// admission. One more token than the last KV-written position ...
pub(super) fn lifetime_blocks(prompt_tokens: usize, max_tokens: usize) -> usize {
    (prompt_tokens + max_tokens).div_ceil(PAGE)
}
```

单测 [src/scheduler/admission.rs:L665-L674](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/admission.rs#L665-L674) 用四组边界把 off-by-one 钉死：

```rust
// 节选自 src/scheduler/admission.rs:L665-L674
#[test]
fn lifetime_blocks_counts_the_dangling_token() {
    // 64 prompt + 1 max_tokens: the generated token is appended to the
    // sequence (dangling) and provisions page 2 even though its KV is
    // never written.
    assert_eq!(lifetime_blocks(64, 1), 2);
    assert_eq!(lifetime_blocks(63, 1), 1);
    assert_eq!(lifetime_blocks(64, 64), 2);
    assert_eq!(lifetime_blocks(64, 65), 3);
}
```

**③ 池级测试。** [src/lib.rs:L2077-L2085](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L2077-L2085)：cap = 4096、slots = 32（`GLM52_MAX_BATCH_PER_RANK`）时，每 slot 页数 = ceil(4097/64) = 65，池 = 32×65 + 1 = 2081 块（节选）：

```rust
// 节选自 src/lib.rs:L2078-L2084
let cap = 4096usize;
let pages_per_slot = (cap + 1).div_ceil(GLM52_MODEL_LEN_ALIGN);
assert_eq!(
    glm52_pool_blocks(cap, model::GLM52_MAX_BATCH_PER_RANK),
    model::GLM52_MAX_BATCH_PER_RANK * pages_per_slot + 1
);
```

**④ BlockPool 与手工 pin。** [src/lib.rs:L1028-L1031](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1028-L1031)：`BlockPool::new(GLM52_MODEL_LEN_ALIGN, pool_blocks)`——它是 `pegainfer-kv-store` 里的**纯 CPU 对象**（页表在建时冻结），块号与 arena 页一一对应。手工 pin 池块数的环境变量 `GLM52_KV_POOL_BLOCKS` 被两道栅栏夹住：低于单请求下限拒绝（[src/lib.rs:L1397](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1397)）、高于实测可负担值拒绝（[src/lib.rs:L1408](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1408)）——u2-l4 已展开。

**⑤ 页拷贝的单位。** [src/model/mod.rs:L643-L652](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L643-L652)：`copy_kv_page` 先断言 src/dst 都在池内，再调 `glm52_copy_page_content`——注释点明「一页的全部层切片（MTP 镜像在内）按构造一起搬」，这正是 `Glm52StepKv.boundary_copies` 在每步 kernel 之前做的事（native P/D 与前缀恢复复用同一机制）。

#### 4.3.4 代码实践

**实践 C：当一次 admission 的预算员。**

1. 实践目标：给定几组 (prompt, max_tokens) 与池参数，预测 `lifetime_blocks` 与 `glm52_pool_blocks` 的返回值，再用源码公式核对。
2. 操作步骤：
   - 请求甲 (prompt=200, max_tokens=10)：ceil(210/64) = 4 页；
   - 请求乙 (prompt=4096, max_tokens=1)：ceil(4097/64) = 65 页——比「写满 4096 个 KV 位置的 64 页」多一页，多出的那页只装一个悬空 token 的占位；
   - 池参数 cap=4096、slots=32：32 × 65 + 1 = 2081 块，其中 1 块是 padding 页。
3. 需要观察的现象：任何「prompt + max_tokens 恰好是 64 的倍数」的请求都会比看起来多占一页——例如 (64, 1) 占 2 页、(64, 64) 占 2 页、(64, 65) 占 3 页。
4. 预期结果：与 [src/scheduler/admission.rs:L670-L673](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/admission.rs#L670-L673) 的断言完全一致。
5. 机器核对（需可编译环境）：`cargo test --release -p pegainfer-glm52 --features glm52 --lib dangling`——可同时命中 admission 与 lib.rs 的两条悬空 token 测试。本讲撰写环境未运行，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 padding 页必须是「从不分配」的保留页，而不是随便挑一块当前空闲页？

答案：padding 行的写入内容是无意义的垃圾。若 padding 页可能属于某个请求，垃圾会覆盖真实历史；保留 0 号块且从不分配（`BlockPool` 建池即保留），「垃圾写入无人读」才成立——这是 free-running 纪律（u7-l1）「padding 行是协议面，必须构造性确定」在存储侧的落地。

**练习 2**：cap=8192、slots=16 时池多大？slab 多大字节？

答案：每 slot 页数 = ceil(8193/64) = 129（64×128 = 8192 < 8193）；池 = 16×129 + 1 = 2065 块；slab = 2065 × 3,503,040 + 3,502,592 = 7,237,280,192 B ≈ 6.74 GiB（不含 MTP scratch 页）。

**练习 3**：页距 3,503,040 里那 448 B 尾填充，为什么拷页时可以「从不移动」？

答案：没有任何切片落在尾填充里（内容精确到 3,502,592 B 断言过），它是给 TMA 的 656 对齐余量；拷贝单位按内容算即可，搬运尾填充只会浪费 448 B 带宽（[src/model/mod.rs:L147-L150](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L147-L150)）。

## 5. 综合实践

**任务：为一次 EP8 部署手写完整的「页预算表」。**设想 rank 参数 cap = 8192、decode slots = 16（MTP 关闭），把本讲三个模块串起来：

1. **页表宽**（4.1）：cap / 64 = 128——每个 bucket 的 block 表按此定宽。
2. **池规模**（4.3）：每 slot 页数 = ceil(8193/64) = 129（悬空 token 多一页）；池 = 16 × 129 + 1 = 2065 块（含 1 块 padding 页）。
3. **slab 字节**（4.2）：2065 × 3,503,040 + 3,502,592 = 7,237,280,192 B ≈ 6.74 GiB；折合每 token 摊销 ≈ 3,503,040 / 64 ≈ 54.7 KB，其中 MLA 78×656 = 51,168 B、index-K 21×132 = 2,772 B、MTP 镜像 (656+132) = 788 B、尾填充 448/64 = 7 B——与 u2-l4「每块 slab 单价约 3.5 MB」对得上（3,503,040 B = 3.50 MB）。
4. **页内地图**（4.1+4.2）：把 4.2.4 预期结果表抄进来作为附录，标注层 6 是「首个 MoE + full indexer」层、层 77 是最后一个 shared 层、MTP 镜像骑在每页末尾。
5. **机器对账**：`cargo test --release -p pegainfer-glm52 --features glm52 --lib page_layout` 与 `... --lib dangling`（两条都是纯 CPU 断言；需能编译 glm52 feature 的环境，**待本地验证**）。

做完后自测三个问题：把 cap 提到 8256（仍是 64 倍数）池变多大？（每 slot 130 页，池 2081 块）；把 slots 提到 32 且开 5 草稿的 native MTP，为什么 u1-l2 说 slots×(1+drafts) ≤ 96 而本讲公式没变？（96 是**步行**预算，与**池容量**是两个维度，见 [src/model/mod.rs:L277-L288](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L277-L288) 的注释）；TP4 prefill-only 也用同样的页吗？（是——线格式一致性测试钉死了这一点。）

## 6. 本讲小结

- **一个 64 走天下**：FlashMLA 稀疏页 = index-K 缓存块 = 上下文对齐单位；MLA 切片 64×656 = 41,984 B，index-K 切片 64×(128+4) = 8,448 B。
- **一页 = 78 MLA 切片 + 21 index-K 切片 + MTP 镜像（1+1 切片）= 3,502,592 B**；页距取 656 的整数倍得 3,503,040 B，448 B 尾填充从不移动——页距是卸载 namespace 与 P/D fingerprint 共同折叠的线格式身份。
- **页表宽 = cap/64**，所以 cap 必须 64 对齐；**池 = slots × ceil((cap+1)/64) + 1**，两个「多一」分别是每 slot 的悬空 token 页与全池的 padding 页。
- **Glm52KvSlab + Glm52LayerCaches** 是「一块大显存 + 一张偏移表」：物理页号是运行期数据（图安全），页布局由 `glm52_page_layout` 单一构造，`finish_kv` 按 `blocks × stride + CONTENT` 一次落位，尾冗余喂保守的越界检查。
- **padding 页（block 0）** 承接 free-running 与图预捕获的全部垃圾写入，「无人读」靠「从不分配」保证；**悬空 token 契约** 让 admission 按全生命周期留页，decode 中途永不缺页。

## 7. 下一步学习建议

- **u4-l3（MLA 解码后半）**：拆开 656 B/token 的 fp8_ds_mla 行内部构成，看吸收式解码如何直接消费本讲的页。
- **u4-l4（DSA 索引器）**：看 index-K 切片的**写入方**——DeepGEMM paged MQA 如何按 64-token 块打分，以及 top-k 跨层共享（本讲的 {0,1,2} ∪ {6,10,…,74} 在那里变成 topk_carry 的复用图）。
- **u7-l5（分页 KV 与前缀缓存）**：本讲只到「页怎么摆」；BlockPool 的内容哈希页、RAII 归还与命中即 suffix-only prefill 在那一讲展开。
- 阅读顺序上，若你已经学过 u2-l4，可对照 `glm52_cap_bytes`（精确账本）看本讲的页距如何变成「每块约 3.5 MB」的 slab 单价。
