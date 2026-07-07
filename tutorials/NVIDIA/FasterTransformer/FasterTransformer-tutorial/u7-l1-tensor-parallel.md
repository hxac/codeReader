# 张量并行：基于 NCCL 的切分与 all-reduce

## 1. 本讲目标

本讲是「并行与分布式推理」单元的第一篇，回答一个核心问题：**当一层 transformer 的权重矩阵大到单卡放不下（或单卡算得太慢）时，FasterTransformer（FT）如何把它拆到多张 GPU 上，并且让拆分后的结果与单卡在数学上完全等价？**

学完本讲你应当能够：

- 说清「张量并行（Tensor Parallelism, TP）」与流水并行（Pipeline Parallelism, PP）的分工，以及为什么 FT 把 TP 用在节点内、PP 用在节点间。
- 读懂 `NcclParam` 这个贯穿全库的通信域描述符，以及 `ftNcclAllReduceSum` / `ftNcclAllGather` / `ftNcclBroadCast` 等通信原语做了什么。
- 用「列并行（column parallel）」与「行并行（row parallel）」的语言，精确指出注意力层和 FFN 层各自在哪一步通信、哪一步不通信，并解释**为什么一个 transformer block 在 TP 下恰好只需要 2 次 all-reduce**。
- 理解 `do_all_reduce_`、`enable_custom_all_reduce_` 等通信开关的用途，以及它们与自定义 all-reduce（u7-l3）的衔接点。

本讲承接 u3-l3（注意力层的 Unfused/Fused/TensorParallel 三条路径）、u3-l4（FFN 层与张量并行 FFN 变体）与 u6-l1（ParallelGpt 的 context/decoder 两阶段），把此前「构造时传 `head_num/world_size`、末尾一次 all-reduce」的结论彻底讲透。

## 2. 前置知识

在进入源码前，先用最朴素的方式建立几个直觉。

**为什么要并行？** GPT-3 有 1750 亿参数，FP16 下约 350 GB，一张 A100（80 GB）根本装不下。即便装得下，单卡串行算完一层也太慢。所以必须把模型拆到多卡甚至多节点上。

**两种拆法。**

| 并行方式 | 切什么 | 通信频率 | FT 推荐位置 |
| :--- | :--- | :--- | :--- |
| 张量并行（TP） | 把**单层**的权重矩阵按行/列切碎，分给多卡 | 每层都要通信（all-reduce） | 节点内（共享 NVLink，带宽高） |
| 流水并行（PP） | 把**不同层**分到不同卡，按微批次流水线推进 | 仅阶段边界通信 | 节点间（网络带宽低） |

TP 的特点是「通信频繁但每次通信量与隐藏维成正比」，所以它极度依赖高带宽互联（NVLink/NVSwitch），适合放在同一节点内；PP 通信稀疏，适合跨节点。这正是 gpt_guide 给出的建议（见 4.1）。

**NCCL 是什么？** NVIDIA Collective Communications Library，多 GPU 之间的集合通信库，提供 all-reduce、all-gather、broadcast、send/recv 等原语，是 FT 做多卡通信的底层。本讲你会看到 FT 把 NCCL 的 C API 封装成一组更好用的 `ftNccl*` 模板函数。

**集合通信速查。**

- **all-reduce**：每张卡提供一个缓冲，所有卡最后都拿到「各卡缓冲的逐元素求和（或求最大等）」。TP 里行并行 GEMM 后用它合并部分和。
- **all-gather**：每张卡提供自己那一段，最后所有卡都拿到「所有人拼起来的完整缓冲」。TP 里 logits 投影后用它把切开的词表拼回来。
- **broadcast**：根卡把数据广播给所有卡。

> 本讲不要求你已经写过 NCCL 代码，但需要你记得 u3-l3/u3-l4 的结论：注意力层和 FFN 层各自在「输出投影之后」做一次 all-reduce。本讲要回答的是**为什么是这里、为什么只有两次**。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/fastertransformer/utils/nccl_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.h) | 声明 `NcclParam` 描述符与全部 `ftNccl*` 通信原语模板。 |
| [src/fastertransformer/utils/nccl_utils.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc) | 通信原语的实现，含 `ftNcclInitialize` 用 MPI 二维笛卡尔拓扑划分 TP/PP 通信域。 |
| [src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc) | 张量并行注意力层：构造时切头、forward 末尾 all-reduce。 |
| [src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc) | GPT context 阶段的张量并行注意力，all-reduce 点与上一文件同构。 |
| [src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc) | 张量并行 FFN 层：构造时切 `inter_size`、forward 末尾 all-reduce。 |
| [src/fastertransformer/layers/FfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc) | FFN 基类，GEMM1（升维）与 GEMM2（降维）的形状定义，是看懂「切输出/切输入」的关键。 |
| [src/fastertransformer/utils/custom_ar_comm.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.h) | `AbstractCustomComm` 抽象，自定义 all-reduce 的接口（u7-l3 深入）。 |
| [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) | 官方对 TP/PP 设计动机与「每 block 2 次 all-reduce」的文字说明。 |

## 4. 核心概念与源码讲解

### 4.1 张量并行的动机与「每 block 仅 2 次 all-reduce」

#### 4.1.1 概念说明

张量并行（TP）的目标是：把一个**单层**的大矩阵乘切成若干份，分给多张卡同时算，最后用一次集合通信把结果合起来。它的对立面是「数据并行」（每张卡算不同的样本、各自有完整模型）和「流水并行」（每张卡算不同的层）。TP 切的是模型本身的张量维度，所以叫 *tensor* parallel。

FT 的 TP 实现遵循 [Megatron-LM](https://arxiv.org/pdf/1909.08053.pdf) 的切分方案。Megatron 的核心观察是：一个 transformer block 里有两个子模块——注意力（self-attention）和前馈网络（FFN），每个子模块都是「两个矩阵乘夹一个非线性」。只要把**第一个矩阵乘切成不需要通信的形状**、把**第二个矩阵乘切成需要 all-reduce 的形状**，就能让整个 block 的通信压到最低。

#### 4.1.2 核心流程

官方文档对这套设计的文字描述只有一段，但信息量极大：

> For both self-attention block and feed forward network block, we split the weights of first matrix multiplication by row and split the weights of second matrix multiplication by column. By optimization, we can reduce the reduction operation to 2 times for each transformer block.

翻译过来：对注意力块和 FFN 块，**第一个矩阵乘按「行」切、第二个矩阵乘按「列」切**，可以把每个 transformer block 的 all-reduce 次数降到 2 次。

这里有个**极易踩坑的术语陷阱**，必须先澄清（见 4.3）：FT 用 cuBLAS 时把权重存成「转置」布局 `[输出维, 输入维]`，所以文档说的「按行切」其实对应数学上「切输出特征」（即 Megatron 术语的 *column parallel*）。两套说法都对，只是参照的矩阵朝向不同。本讲统一用 Megatron 的数学术语（列并行 = 切输出、行并行 = 切输入）来讲，因为它最能直接解释「哪里要通信」。

一个 TP transformer block 的整体流程（设 TP=N）：

```
隐状态 X  (每张卡都有完整副本, 因为上一子模块已 all-reduce 出完整结果)
  │
  ▼  [注意力块]
  QKV 投影      列并行: 每卡只算自己那份头 (head_num/N 个头)     ← 无通信
  注意力打分    每卡在自己那份头上独立做 QK^T→softmax→PV         ← 无通信(头之间本就独立)
  输出投影      行并行: 每卡算出 hidden 的部分和                 ← ★ all-reduce #1
  │  (此时每卡又拿到完整 hidden)
  ▼  [FFN 块]
  升维 GEMM1    列并行: 每卡只算 inter_size/N 列                ← 无通信
  激活
  降维 GEMM2    行并行: 每卡算出 hidden 的部分和                 ← ★ all-reduce #2
  │
  ▼
完整隐状态 (交给下一层)
```

**结论：一个 block 恰好 2 次 all-reduce。** 这不是巧合，而是「列并行 + 行并行」配对设计的必然结果——通信只发生在行并行 GEMM 之后。

#### 4.1.3 源码精读

上述结论在 [docs/gpt_guide.md:199](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L199) 的 Optimization 小节里。同一段还给出了 TP/PP 的部署建议：

> We recommend to use tensor parallel intra node, and use pipeline parallel inter node because tensor parallel requires more NCCL communication.

这印证了前置知识里的表格：TP 通信频繁→放节点内走 NVLink；PP 通信稀疏→可跨节点走以太网/InfiniBand。

#### 4.1.4 代码实践

**实践目标**：在不看本讲后续内容的前提下，自己先预测「2 次 all-reduce 分别在哪个 GEMM 之后」。

**操作步骤**：

1. 打开 [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md)，定位到第 199 行（Optimization 第 3 条 Model parallelism）。
2. 找到「reduce the reduction operation to 2 times for each transformer block」这句话。
3. 在纸上画出一个 transformer block 的两个子模块（注意力、FFN），每个子模块画两个 GEMM。
4. 在你认为「需要 all-reduce」的 GEMM 后面打勾（共 2 个）。

**需要观察的现象**：你会发现自己打的两个勾，分别落在「注意力输出投影」和「FFN 降维 GEMM」之后——也就是每个子模块的**第二个**矩阵乘。

**预期结果**：每个子模块 = 1 次列并行 GEMM（不通信）+ 1 次行并行 GEMM（all-reduce）= 2 子模块 × 1 次通信 = 每 block 2 次 all-reduce。

#### 4.1.5 小练习与答案

**练习 1**：如果把整个 block 的两个子模块都改成「两次列并行」（即都不 all-reduce），会发生什么？

**参考答案**：列并行只切输出，每张卡拿到的是输出的不同切片（不同特征），而不是完整结果。如果不在子模块末尾 all-reduce，下一个子模块的输入就只是「局部特征」，无法还原成与单卡等价的完整隐状态，结果会错。所以必须有一个「把切片合并回完整 hidden」的步骤——行并行 GEMM + all-reduce 正是干这件事。

**练习 2**：为什么 TP 推荐「节点内 + NVLink」，而 PP 可以跨节点？

**参考答案**：TP 每个 block 要做 2 次 all-reduce，通信频率高，每次通信量正比于 `batch × hidden`，强依赖高带宽低延迟互联（NVLink/NVSwitch 通常只在同一节点内）；PP 只在流水阶段边界通信（把中间激活传给下一组卡），频率低，可容忍较低带宽的网络，因此适合跨节点。

---

### 4.2 NCCL 通信抽象：NcclParam 与 ftNccl 通信原语（最小模块一）

#### 4.2.1 概念说明

要让多张卡协同，FT 需要一个东西来回答三个问题：「我是谁（rank）」「我们组多大（world_size）」「我们用哪条通信通道（nccl communicator）」。这就是 `NcclParam`——一个**轻量的、可值拷贝的通信域描述符**。

注意它和 u2-l1 的 `Tensor`、u2-l2 的 `IAllocator` 一样，都是「贯穿全库的基础抽象」：你会在 `ParallelGpt` 的构造参数里看到它（u6-l1 的 `tensor_para`、`pipeline_para`），也会在每一个 `TensorParallel*Layer` 的成员变量里看到它。理解了它，就理解了 FT 多卡代码的「通行证」。

围绕 `NcclParam`，FT 把裸 NCCL C API 封装成一组模板函数 `ftNccl*`，统一了数据类型派发、错误检查（`NCCLCHECK`）和分组（`ncclGroupStart/End`）。

#### 4.2.2 核心流程

通信域的建立分两步：

1. **用 MPI 划分二维拓扑**。`ftNcclInitialize` 先用 MPI 建一个二维笛卡尔通信子：一维是 TP 组（行），一维是 PP 组（列）。这样每张卡同时属于一个 TP 组和一个 PP 组，分别拿到 `tp_rank` 和 `pp_rank`。
2. **为每个组建 NCCL communicator**。每个组的 rank 0 生成一个 `ncclUniqueId`（相当于「房间号」），用 `MPI_Bcast` 广播给组内所有卡，然后大家各自调 `ncclCommInitRank` 加入同一个 NCCL 通信子。

建好之后，业务代码只需调 `ftNcclAllReduceSum(buf, buf, n, tensor_para_, stream)`，不用关心底层 handle。

数据类型派发由 `getNcclDataType<T>()` 在编译期完成（`float→ncclFloat`、`half→ncclHalf`、`int→ncclInt` 等），BF16 还受 `ENABLE_BF16_NCCL`（NCCL ≥ 2.10）宏守卫。

#### 4.2.3 源码精读

**`NcclParam` 结构**——三个字段，简洁明了（[nccl_utils.h:60-86](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.h#L60-L86)）：

```cpp
struct NcclParam {
    int rank_{0};          // 我在本组里的编号
    int world_size_{1};    // 本组总卡数
#ifdef BUILD_MULTI_GPU
    ncclUniqueId nccl_uid_;
    ncclComm_t   nccl_comm_ = nullptr;  // 真正的 NCCL 通信子
#endif
```

注意 `rank_`/`world_size_` 永远存在，而 `nccl_comm_` 只在编译时打开 `BUILD_MULTI_GPU`（u1-l2 的并行开关）才有。这意味着单卡模式下 `NcclParam` 退化成 `{0, 1}`，所有 `ftNccl*` 调用都被 `#ifdef` 短路成空操作——同一份代码兼容单卡与多卡。

**`ftNcclAllReduceSum` 实现**（[nccl_utils.cc:55-67](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L55-L67)）：

```cpp
template<typename T>
void ftNcclAllReduceSum(const T* send_buf, T* recv_buf, const int data_size,
                        NcclParam nccl_param, cudaStream_t stream) {
#ifdef BUILD_MULTI_GPU
    ncclDataType_t nccl_data_type = getNcclDataType<T>();
    NCCLCHECK(ncclGroupStart());
    NCCLCHECK(ncclAllReduce((const void*)send_buf, (void*)recv_buf, data_size,
                            nccl_data_type, ncclSum, nccl_param.nccl_comm_, stream));
    NCCLCHECK(ncclGroupEnd());
#endif
}
```

三个细节：(1) `send_buf` 和 `recv_buf` 可以是同一块缓冲（原地 all-reduce），这正是 4.4/4.5 里「`attention_out` 既是输入又是输出」的用法；(2) 操作符固定 `ncclSum`——所以函数名带 `Sum`，TP 行并行合并部分和必须用求和；(3) 整个调用包在 `ncclGroupStart/End` 里，便于和其他通信合并成一次集合调用（延迟更优）。

**`ftNcclAllGather`**（[nccl_utils.cc:69-82](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L69-L82)）——注意它的 `send_buf + rank * data_size` 偏移，说明每张卡从全局缓冲里「自己那段」开始发，拼回完整缓冲。这就是 u6-l1 里「logits GEMM（TP>1 时 all-gather）」所用的原语：词表按 TP 切开后，每卡算一段 logits，再 all-gather 拼成完整词表供采样。

**`ftNcclInitialize` 的二维拓扑划分**（[nccl_utils.cc:366-401](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L366-L401)）：

```cpp
int dims[2]    = {pipeline_para_size, tensor_para_size};  // 二维网格
MPI_Cart_create(MPI_COMM_WORLD, 2, dims, periods, 0, &grid_comm);
// 行=TP组, 列=PP组
MPI_Cart_sub(grid_comm, tp_remain_dims, &tp_comm);  // {false, true} 留列→TP
MPI_Cart_sub(grid_comm, pp_remain_dims, &pp_comm);  // {true, false} 留行→PP
...
NCCLCHECK(ncclCommInitRank(&tp_nccl_comm, tensor_para_size, tp_uid, tp_rank));
NCCLCHECK(ncclCommInitRank(&pp_nccl_comm, pipeline_para_size, pp_uid, pp_rank));
```

这段是 u7-l2（MPI 组织）的前奏：MPI 负责把全局 rank 映射到 `(tp_rank, pp_rank)` 二维坐标，NCCL 再据此建两个独立的通信子。本讲你只需记住：**`tensor_para` 和 `pipeline_para` 是两个互不干扰的 `NcclParam`，各管一组通信。**

#### 4.2.4 代码实践

**实践目标**：确认 `NcclParam` 在单卡模式下的退化行为，理解「一份代码兼容单卡/多卡」。

**操作步骤**：

1. 读 [nccl_utils.h:60-86](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.h#L60-L86)，对比 `#ifdef BUILD_MULTI_GPU` 两个分支：多卡分支有 `nccl_comm_`，单卡分支只有 `rank_`/`world_size_`。
2. 读 [nccl_utils.cc:55-67](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L55-L67)，注意 `ftNcclAllReduceSum` 的整个函数体都在 `#ifdef BUILD_MULTI_GPU` 里——单卡编译时它是空函数。
3. 读 [nccl_utils.cc:341-348](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L341-L348)，看 `ftNcclInitialize` 在 `tensor_para_size==1 && pipeline_para_size==1` 时直接 early return，根本不碰 NCCL。

**需要观察的现象**：单卡场景下，`NcclParam` 退化为 `{rank=0, world_size=1, comm=nullptr}`，所有 `ftNccl*` 调用变成 no-op。

**预期结果**：这就是为什么 4.4/4.5 的 TP 层里都有一句 `if (tensor_para_.world_size_ > 1)` 守卫——但即便没有这句守卫，单卡下也不会崩，因为底层已是空操作。守卫只是省一次无谓的函数调用。

#### 4.2.5 小练习与答案

**练习 1**：`ftNcclAllReduceSum` 为什么函数名带 `Sum`？能不能用 `Max`？

**参考答案**：因为 TP 行并行 GEMM 后，每张卡拿到的是完整输出在不同输入维切片上的**部分和**，必须逐元素求和才能还原完整结果，所以用 `ncclSum`。`ncclMax` 一般用于规约统计（如求全局最大值），不用于合并矩阵乘部分和。

**练习 2**：`NcclParam` 为什么设计成可值拷贝（有拷贝构造函数）而不是只能传引用？

**参考答案**：`NcclParam` 只持有一个 rank、一个 world_size 和一个 `ncclComm_t` 句柄（裸指针，非拥有），拷贝它非常廉价，且句柄本身就是共享的（多个 `NcclParam` 副本指向同一个底层通信子是安全的）。把它按值传进每个 layer 的构造函数、存成成员变量，代码更简洁；而 `ncclComm_t` 的生命周期由 `ftNcclParamDestroy` 统一管理，不需要 RAII。

---

### 4.3 列并行与行并行：切分点的数学原理

#### 4.3.1 概念说明

要理解 TP 的切分点，必须先搞懂两种切法在数学上意味着什么。设一个线性层 \( Y = XW \)，其中输入 \( X \in \mathbb{R}^{m \times k} \)，权重 \( W \in \mathbb{R}^{k \times n} \)，输出 \( Y \in \mathbb{R}^{m \times n} \)（\(m\)=token 数，\(k\)=输入特征，\(n\)=输出特征）。

**列并行（column parallel）——切输出特征 \(n\)。** 把 \(W\) 沿列切成 \(p\) 份：\(W = [W_1, W_2, \dots, W_p]\)，每份 \(W_i \in \mathbb{R}^{k \times n/p}\)。每张卡持有完整的 \(X\) 和自己那份 \(W_i\)，算出 \(Y_i = X W_i \in \mathbb{R}^{m \times n/p}\)。因为输入 \(X\) 每卡都有，**这个 GEMM 本身不需要任何通信**；代价是输出只是「部分列」（部分输出特征）。

**行并行（row parallel）——切输入特征 \(k\)。** 把 \(W\) 沿行切：\(W = \begin{bmatrix} W_1 \\ W_2 \\ \vdots \\ W_p \end{bmatrix}\)，每份 \(W_i \in \mathbb{R}^{k/p \times n}\)；同时把 \(X\) 沿列切：\(X = [X_1, X_2, \dots, X_p]\)，\(X_i \in \mathbb{R}^{m \times k/p}\)。每张卡算 \(Y_i = X_i W_i \in \mathbb{R}^{m \times n}\)——注意输出形状是完整的 \(m \times n\)，但只是**部分和**。由分块矩阵乘：

\[
Y = XW = \sum_{i=1}^{p} X_i W_i = \sum_{i=1}^{p} Y_i
\]

所以完整结果 = 各卡 \(Y_i\) 逐元素求和，**这正是 all-reduce（sum）要干的事**。通信代价就在这一步。

#### 4.3.2 核心流程

把两种切法配对，就得到 TP 子模块的标准骨架（\(p\)=TP size）：

```
输入 X (完整, 每卡都有)
  │
  ▼ GEMM_1: 列并行  W₁ ∈ [k, n/p]   →  Y₁ ∈ [m, n/p]   无通信
  │ (非线性/注意力在此作用, 必须逐元素)
  ▼ GEMM_2: 行并行  W₂ ∈ [n/p, n]   →  Z_i ∈ [m, n]    部分和
  │
all-reduce(sum)  →  Z = Σ Z_i  (完整输出, 每卡一致)
```

两个关键不变量：

1. **GEMM_1 与 GEMM_2 之间的非线性必须是逐元素的**（如 softmax、GELU、注意力打分）。因为列并行后每卡只有部分输出特征，逐元素操作可以独立进行；若中间有跨特征的混合操作，列并行就不成立。注意力之所以能列并行，正是因为**注意力是按头独立计算的**，切头 = 切输出特征，头之间无交互。
2. **行并行 GEMM 的输出必须 all-reduce 才完整**。这就是每子模块 1 次通信的来源。

#### 4.3.3 源码精读

现在能看懂 [FfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc) 里两个 GEMM 的形状了。先看**升维 GEMM1（列并行）**（[FfnLayer.cc:265-275](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L265-L275)）：

```cpp
cublas_wrapper_->Gemm(CUBLAS_OP_N, CUBLAS_OP_N,
                      inter_size_,          // m = 输出特征(已被 TP 切小)
                      m,                    // n = token 数
                      hidden_units_,        // k = 输入特征(完整)
                      ffn_weights->intermediate_weight.kernel,
                      inter_size_,          // lda = 输出特征 → 权重存为 [out, in]
                      input_tensor,
                      hidden_units_,        // ldb = 输入特征
                      inter_buf_,
                      inter_size_);         // ldc = 输出特征
```

`inter_size_` 在 TP 构造时已被除以 `world_size`（见 4.5），所以这个 GEMM 只算 `inter_size/p` 个输出特征——**列并行**。注意 cuBLAS 的列主序约定下，权重 leading dimension 是 `inter_size_`（输出维），说明 FT 把权重存成 `[out, in]` 转置布局。

再看**降维 GEMM2（行并行）**（[FfnLayer.cc:360-370](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L360-L370)）：

```cpp
cublas_wrapper_->Gemm(CUBLAS_OP_N, CUBLAS_OP_N,
                      hidden_units_,        // m = 输出特征(完整!)
                      m,                    // n = token 数
                      inter_size_,          // k = 输入特征(已被 TP 切小)
                      ffn_weights->output_weight.kernel,
                      hidden_units_,        // lda = 输出特征 → [out, in]
                      inter_buf_,
                      inter_size_,          // ldb = 输入特征(切片)
                      output_tensor,
                      hidden_units_);       // ldc = 输出特征
```

这次输出 `hidden_units_` 是完整的，而输入 `inter_size_` 是切片——**行并行**，每卡算出完整 hidden 的部分和，等后面的 all-reduce 求和。

**回看 gpt_guide 的术语**（[docs/gpt_guide.md:199](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L199)）说的「first matrix multiplication by row」对应这里 GEMM1 的 `[out, in]` 布局里**切 out 维 = 切存储矩阵的行**，即数学上的列并行（切输出特征）；「second matrix multiplication by column」对应 GEMM2 的 `[out, in]` 布局里**切 in 维 = 切存储矩阵的列**，即数学上的行并行（切输入特征）。两套术语至此完全对齐。

#### 4.3.4 代码实践

**实践目标**：亲手从 GEMM 形状推断「列并行/行并行」，而不依赖结论。

**操作步骤**：

1. 打开 [FfnLayer.cc:265-275](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L265-L275)（GEMM1）和 [FfnLayer.cc:360-370](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L360-L370)（GEMM2）。
2. 对每个 GEMM，记下三件事：`m`（输出维）是完整的还是切片？`k`（输入维）是完整的还是切片？leading dimension 暗示权重存成什么形状？
3. 套用 4.3.1 的定义判定列并行还是行并行。

**需要观察的现象**：

| GEMM | m（输出） | k（输入） | 判定 |
| :--- | :--- | :--- | :--- |
| GEMM1（升维） | `inter_size_`（切片） | `hidden_units_`（完整） | 列并行（切输出） |
| GEMM2（降维） | `hidden_units_`（完整） | `inter_size_`（切片） | 行并行（切输入） |

**预期结果**：判定表与 4.1 的流程图一致——GEMM1 不通信、GEMM2 后 all-reduce。

#### 4.3.5 小练习与答案

**练习 1**：列并行 GEMM 之后、行并行 GEMM 之前，如果插入一个「跨输出特征求均值」的操作，TP 还正确吗？

**参考答案**：不正确。列并行后每卡只有部分输出特征，跨特征求均值需要知道全部特征，而它们分散在各卡上、未被合并。只有**逐元素**操作（如 GELU、softmax-per-head）才能在列并行切片上独立执行。这也是 FFN 中间只能放逐元素激活的原因。

**练习 2**：行并行 GEMM 把 \(X\) 也沿列切了，意味着每张卡的输入 \(X_i\) 不同。但 4.1 的流程图说「输入 X 每卡都有完整副本」，矛盾吗？

**参考答案**：不矛盾。4.1 指的是「进入子模块时的隐状态 X 是完整的」（因为上一子模块末尾已 all-reduce）。行并行 GEMM 的「切 X」是指在该 GEMM 内部，把完整的隐藏输入沿其特征维（`inter_size` 那一维，即 GEMM2 的输入）切成若干份——而这份输入 `inter_buf` 正是上一个列并行 GEMM1 的输出，它本就是切好的。所以行并行切的是「列并行产出的、天然分块的中间结果」，不需要额外通信。

---

### 4.4 注意力层的张量并行：切头 + all-reduce（最小模块二·注意力侧）

#### 4.4.1 概念说明

注意力层天然适合 TP，因为**多头注意力的各个头彼此独立**。一个有 16 个头的注意力层，在 TP=4 时每卡只算 4 个头——每个头内部完整地做 \(QK^T \to \text{softmax} \to PV\)，头与头之间无任何数据依赖。所以「切头」就是「切输出特征」，完美对应列并行。

注意力层有两个矩阵乘：QKV 投影（输入→Q/K/V）和输出投影（注意力结果→隐藏）。按 Megatron 方案：QKV 投影**列并行**（切头，无通信），输出投影**行并行**（all-reduce）。FT 的 `TensorParallel*AttentionLayer` 类正是这么实现的——它**继承**单卡注意力层，只做两件事：构造时把 `head_num` 除以 `world_size`、forward 末尾插一次 `ftNcclAllReduceSum`。

#### 4.4.2 核心流程

```
TensorParallelAttentionLayer.forward:
  1. (可选) 若启用自定义 all-reduce, 先 swapInternalBuffer 换出内部缓冲
  2. 调用基类 forward  ── 内部做:
        QKV 投影  (列并行, 每卡只算 local_head_num 个头)   无通信
        注意力打分 (每卡在自己的头上独立算)               无通信
        输出投影   (行并行, 产生 hidden 部分和)            待合并
  3. if (world_size > 1):
        ftNcclAllReduceSum(attention_out, attention_out, size, ...)   ★ 唯一通信点
```

关键点：**通信点只有一个，且在输出投影之后**。QKV 投影、注意力打分全程零通信。

#### 4.4.3 源码精读

**构造时切头**——`TensorParallelUnfusedAttentionLayer` 把 `head_num / world_size` 传给基类（[TensorParallelUnfusedAttentionLayer.cc:79-94](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L79-L94)）：

```cpp
UnfusedAttentionLayer<T>(...,
                         head_num / tensor_para.world_size_,   // ← 只算自己那份头
                         size_per_head,
                         ...),
tensor_para_(tensor_para), ...
{
    FT_CHECK(head_num % tensor_para_.world_size_ == 0);   // 头数必须能被 TP 整除
}
```

`FT_CHECK` 把 u1-l4 里那条并行硬约束「`head_num % TP == 0`」固化在代码里——切头必须整除。

**forward 末尾 all-reduce**（[TensorParallelUnfusedAttentionLayer.cc:48-60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L48-L60)）：

```cpp
UnfusedAttentionLayer<T>::forward(output_tensors, input_tensors, attention_weights);  // 先跑单卡逻辑

T* attention_out = output_tensors->getPtr<T>("hidden_features");
if (tensor_para_.world_size_ > 1) {
    if (!use_custom_all_reduce_kernel) {
        ftNcclAllReduceSum(attention_out, attention_out, size, tensor_para_,
                           UnfusedAttentionLayer<T>::stream_);   // ★ 原地 all-reduce
    } else {
        custom_all_reduce_comm_->customAllReduce(size, ...);     // 或自定义 kernel
        output_tensors->at("hidden_features").data = hidden_features_reduce[0].data;
    }
    sync_check_cuda_error();
}
```

`attention_out` 同时作为 `send_buf` 和 `recv_buf`——原地 all-reduce，合并输出投影的部分和。这就是「注意力层唯一通信点」。

**GPT context 阶段同理**——`TensorParallelGptContextAttentionLayer` 的结构与上面完全同构（[TensorParallelGptContextAttentionLayer.cc:46-60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc#L46-L60)）：调基类 `GptContextAttentionLayer::forward`（内部做 QKV 投影、写 KV cache、算 context 注意力、输出投影），末尾 `ftNcclAllReduceSum`。区别只是基类走 cuBLAS 批量 GEMM（u6-l1 的 context 阶段），而 `UnfusedAttentionLayer` 是通用版本。两者共享「切头 + 末尾 all-reduce」的 TP 模式。

`do_all_reduce_` 开关（出现在 context 版本，[TensorParallelGptContextAttentionLayer.cc:50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc#L50)）允许**推迟/跳过**这次通信——当某层是 block 的最后一层、且其结果马上要被行并行消费时，可以把通信合并到下游，减少通信次数。这是「延迟通信」优化。

#### 4.4.4 代码实践

**实践目标**：追踪一次注意力 forward，确认「QKV 投影和打分阶段确实零通信，只有末尾一次 all-reduce」。

**操作步骤**：

1. 打开 [TensorParallelUnfusedAttentionLayer.cc:22-61](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L22-L61)。
2. 在第 48 行（`UnfusedAttentionLayer<T>::forward(...)`）处「跳进」基类（你需要额外打开 `UnfusedAttentionLayer.cc`），确认基类内部只有 cuBLAS GEMM 和注意力 kernel，**没有任何 `ftNccl*` 调用**。
3. 回到 TP 包装层，确认唯一的 `ftNcclAllReduceSum` 在第 53 行。

**需要观察的现象**：基类 `UnfusedAttentionLayer` 完全不感知多卡——它只看到 `local_head_num` 个头，照常做 QKV、打分、输出投影。多卡逻辑全在 TP 包装层那一行 all-reduce 里。

**预期结果**：这印证了 u3-l3 的结论——「TensorParallel 注意力层 = 继承单卡层 + 构造时切头 + 末尾一次 all-reduce」。TP 是套在单卡实现外面的一层薄壳。

#### 4.4.5 小练习与答案

**练习 1**：为什么 QKV 投影用列并行（切头），而不用行并行？

**参考答案**：因为注意力是按头独立的，切头 = 切输出特征，每卡能完整算出自己那几个头的 Q/K/V 并完成注意力打分，无需通信。若改用行并行（切输入特征），Q/K/V 的输出会是部分和，但 softmax 需要每个头完整的 Q/K/V 才能正确归一化，部分和无意义，且每步都要 all-reduce，通信次数大增。

**练习 2**：`head_num=12, TP=8` 会怎样？

**参考答案**：构造函数里的 `FT_CHECK(head_num % tensor_para_.world_size_ == 0)` 会失败（12 % 8 = 4 ≠ 0），程序直接 abort。这正是 u1-l4 提到的硬约束。要跑 TP=8，`head_num` 必须是 8 的倍数。

---

### 4.5 FFN 层的张量并行：切 inter_size + all-reduce + 通信开关（最小模块二·FFN 侧）

#### 4.5.1 概念说明

FFN 是 transformer block 的另一个子模块，结构是「升维 GEMM → 激活 → 降维 GEMM」（u3-l4）。它的 TP 切分与注意力同构：升维 GEMM1 **列并行**（切 `inter_size`，无通信），降维 GEMM2 **行并行**（all-reduce）。

为什么 GEMM1 切 `inter_size` 不影响正确性？因为 `inter_size` 维上的每个神经元经过激活后是**逐元素**独立的，切 `inter_size` = 切输出特征，每卡算自己那份中间神经元，激活逐元素作用，再交给行并行的 GEMM2 合并。激活函数必须是逐元素的（GELU/ReLU/SiLU 都是），这条不变量在 4.3.2 已强调。

`TensorParallelGeluFfnLayer`（以及 Silu/Relu 三个变体，u3-l4 已说明它们逐行同构）同样是「继承单卡 FFN + 构造切 `inter_size` + forward 末尾 all-reduce」的薄壳。

#### 4.5.2 核心流程

```
TensorParallelGeluFfnLayer.forward:
  1. (可选) 自定义 all-reduce 的 swapInternalBuffer
  2. 调用基类 GeluFfnLayer::forward ── 内部做:
        GEMM1 升维  (列并行, inter_size/world_size)     无通信
        GELU 激活   (逐元素)                           无通信
        GEMM2 降维  (行并行, hidden 部分和)             待合并
  3. if (do_all_reduce_ && world_size > 1):
        ftNcclAllReduceSum(ffn_out, ffn_out, token_num*hidden_units, ...)  ★ 通信点
```

注意 `do_all_reduce_` 这个开关：FFN 的 all-reduce 受它控制（注意力层的 unfused 版没有这个开关，总是 reduce；context 版有）。它允许在「下一层会立刻行并行消费本层输出」时，把本层 all-reduce 与下一层合并，进一步省通信。

#### 4.5.3 源码精读

**构造时切 `inter_size`**（[TensorParallelGeluFfnLayer.cc:86-105](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L86-L105)）：

```cpp
GeluFfnLayer<T>(...,
                inter_size / tensor_para.world_size_,   // ← 只算自己那份中间维
                ...),
tensor_para_(tensor_para), ...
{
    FT_CHECK(inter_size % tensor_para_.world_size_ == 0);   // inter_size 必须能被 TP 整除
}
```

`inter_size` 被除以 `world_size`——这就是 4.3.3 里 GEMM1 的 `m=inter_size_` 自动变成切片、GEMM2 的 `k=inter_size_` 自动变成切片的原因。整除约束同样由 `FT_CHECK` 守护。

**forward 末尾 all-reduce**（[TensorParallelGeluFfnLayer.cc:51-65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L51-L65)）：

```cpp
GeluFfnLayer<T>::forward(output_tensors, input_tensors, ffn_weights);   // 先跑单卡 FFN

PUSH_RANGE("FFN all reduce sum");
T* ffn_out = out_tensor.getPtr<T>();
if (do_all_reduce_ && tensor_para_.world_size_ > 1) {
    if (!use_custom_all_reduce_kernel) {
        ftNcclAllReduceSum(ffn_out, ffn_out, token_num * hidden_units, tensor_para_,
                           GeluFfnLayer<T>::stream_);   // ★ 原地 all-reduce
    } else {
        custom_all_reduce_comm_->customAllReduce(token_num * hidden_units, ...);
        output_tensors->at("ffn_output").data = swap_tensors[0].data;
    }
    sync_check_cuda_error();
}
POP_RANGE;
```

通信量 = `token_num * hidden_units`（每个 token 一整行 hidden）。这与注意力层的 all-reduce 量级相同——每 block 两次 all-reduce，每次都搬一个 `[token_num, hidden]` 的缓冲。

**自定义 all-reduce 接口**（[custom_ar_comm.h:31-40](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.h#L31-L40)）：

```cpp
class AbstractCustomComm {
    virtual void customAllReduce(size_t elts, cudaStream_t stream) = 0;
    virtual bool swapInternalBuffer(std::vector<Tensor>* tensor_buffer, size_t elts) = 0;
    ...
};
```

当 `enable_custom_all_reduce_` 打开、且 `custom_all_reduce_comm_` 非空时，TP 层会先调 `swapInternalBuffer` 把输出缓冲换成自定义通信的内部缓冲，跑完基类 forward 后用 `customAllReduce` 代替 NCCL。这是 u7-l3 的主题——在 DGX-A100 等全互联拓扑上，自定义 CUDA kernel 比 NCCL all-reduce 延迟更低。本讲只需知道：**TP 层的 all-reduce 通道是可插拔的（NCCL 或自定义），由构造参数决定。**

至此，一个 TP transformer block 的两次 all-reduce 全部定位完毕：

| 子模块 | 列并行 GEMM（无通信） | 行并行 GEMM（all-reduce） |
| :--- | :--- | :--- |
| 注意力 | QKV 投影（切头） | 输出投影 → ★ all-reduce #1 |
| FFN | 升维 GEMM1（切 inter） | 降维 GEMM2 → ★ all-reduce #2 |

#### 4.5.4 代码实践

**实践目标**：对比注意力层与 FFN 层的 TP 包装代码，体会「同构的薄壳模式」。

**操作步骤**：

1. 并排打开 [TensorParallelUnfusedAttentionLayer.cc:48-60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L48-L60) 和 [TensorParallelGeluFfnLayer.cc:51-65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L51-L65)。
2. 逐行比对：两者都是「调基类 forward → `if (world_size>1)` → `ftNcclAllReduceSum`（或 custom）→ `sync_check_cuda_error`」。
3. 找出唯一结构差异：FFN 版多了 `do_all_reduce_` 守卫和 `PUSH_RANGE("FFN all reduce sum")` 的 NVTX 标记（u1-l5）。

**需要观察的现象**：两类 TP 层的通信代码几乎可以逐行对应，说明「列并行 + 行并行 + 末尾 all-reduce」是 FT 里**正交于具体子模块**的通用模式。

**预期结果**：你会得出结论——任何「两个 GEMM 夹逐元素非线性」的子模块，都能套同一个 TP 薄壳；新增模型时（u11-l2）只需复用，不必重写通信逻辑。

#### 4.5.5 小练习与答案

**练习 1**：FFN 的 all-reduce 受 `do_all_reduce_` 控制，关掉它（设为 false）会发生什么？

**参考答案**：本层 GEMM2 后的 partial sum 不合并，直接交给下游。若下游是该 block 之外、且需要完整 hidden 的消费者（如残差加 + 下一层），结果会错。`do_all_reduce_=false` 只有在「下游会立刻做行并行消费、能把本层合并与之融合」时才安全，是一种延迟通信优化，需调用方谨慎配置。

**练习 2**：一个 TP=4、`hidden=1024`、`batch×seq=4096 tokens` 的 block，两次 all-reduce 各搬运多少元素？

**参考答案**：每次 all-reduce 搬一个 `[token_num, hidden]` = `4096 × 1024 = 4,194,304` 个元素。两次共搬约 8.4M 元素（FP16 下约 16 MB）。这正是 TP 通信频繁、强依赖节点内高带宽的量化依据。

---

## 5. 综合实践

**任务**：以一个 TP=4 的 transformer block 为例，画出完整数据流，标出两次 `ftNcclAllReduceSum` 的精确位置，并解释列切分（不通信）与行切分（需 all-reduce）的区别。

**操作步骤**：

1. 选定一组具体参数：`head_num=16, size_per_head=64, hidden=1024, inter_size=4096, TP=4`。
2. 推导每张卡上的局部维度：
   - 注意力：`local_head_num = 16/4 = 4` 个头；QKV 投影输出 `4×64×3 = 768` 维（列并行切片）。
   - FFN：`local_inter_size = 4096/4 = 1024`（列并行切片）。
3. 画数据流图（参考 4.1.2 的流程图），在以下两个位置标注 **★ ftNcclAllReduceSum**：
   - 注意力输出投影之后（对应 [TensorParallelUnfusedAttentionLayer.cc:53](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L53) 或 context 版 [TensorParallelGptContextAttentionLayer.cc:52](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc#L52)）。
   - FFN 降维 GEMM2 之后（对应 [TensorParallelGeluFfnLayer.cc:57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L57)）。
4. 在图上用两种颜色区分：
   - **列切分（不通信）**：QKV 投影、注意力打分、FFN 升维 GEMM1。理由：切的是输出特征，每卡独立算自己那份，头/中间神经元彼此无依赖。
   - **行切分（需 all-reduce）**：注意力输出投影、FFN 降维 GEMM2。理由：切的是输入特征，每卡只算出 hidden 的部分和，必须 `Σ partial = full` 才完整。
5. 写一段话解释：为什么这种配对能让通信次数压到每 block 2 次，而不是 naive 实现的 4 次（每个 GEMM 后都 reduce）或更多。

**预期结果**：你的图应当清楚展示——每个子模块内部，第一个 GEMM（列并行）把数据「打散」到各卡而无需通信，非线性操作在切片上逐元素进行，第二个 GEMM（行并行）把打散的数据「收拢」回完整 hidden 并用一次 all-reduce 合并。两次「收拢」= 两次 all-reduce。

**进阶（可选）**：再在图上标出 logits 投影后的 `ftNcclAllGather`（u6-l1 提到 TP>1 时 logits 要 all-gather），指出它发生在 block 之外、模型最末层，与 block 内的 2 次 all-reduce 是不同性质的一次性通信。

> 说明：本实践为「源码阅读 + 推导型」任务，无需多卡环境即可完成。若你有多卡环境，可对照 u1-l4 的 `multi_gpu_gpt_example` 与 `gpt_config.ini` 设置 `tensor_para_size=4`，用 `FT_LOG_LEVEL=DEBUG` 运行，在日志中观察 NCCL 通信的实际触发（运行结果「待本地验证」）。

## 6. 本讲小结

- **TP 切的是单层张量维度**，遵循 Megatron 方案：每个子模块「第一个 GEMM 列并行 + 第二个 GEMM 行并行」，使一个 transformer block 的 all-reduce 恰好 **2 次**（注意力、FFN 各一）。
- **`NcclParam`** 是贯穿全库的轻量通信域描述符（`rank_`/`world_size_`/`nccl_comm_`），单卡下退化为 `{0,1}`，使同一份代码兼容单卡/多卡；`ftNccl*` 系列模板函数（`AllReduceSum`/`AllGather`/`BroadCast`/`Send`/`Recv`）封装了裸 NCCL。
- **列并行（切输出特征）不通信**，行并行（切输入特征）的输出是部分和、**必须 `ftNcclAllReduceSum` 求和**才完整；gpt_guide 说的「by row/column」是 FT 转置权重布局 `[out,in]` 下的说法，与 Megatron 数学术语一一对应。
- **注意力 TP** = 构造时 `head_num/world_size` 切头（QKV 投影列并行、打分零通信、输出投影行并行）+ 末尾一次 all-reduce；**FFN TP** = 构造时 `inter_size/world_size`（GEMM1 列并行、GEMM2 行并行）+ 末尾一次 all-reduce。两者是同构的「薄壳套在单卡层之外」。
- **通信通道可插拔**：`enable_custom_all_reduce_` + `custom_all_reduce_comm_`（`AbstractCustomComm`）允许用自定义 CUDA kernel 代替 NCCL（u7-l3 主题）；`do_all_reduce_` 允许延迟/合并通信。
- **TP 适合节点内（NVLink）、PP 适合节点间**，因为 TP 通信频繁、PP 通信稀疏——这是部署时的核心取舍。

## 7. 下一步学习建议

- **u7-l2 流水并行与 MPI 组织**：本讲只用到 `ftNcclInitialize` 建好的 `tensor_para`/`pipeline_para`，下一讲深入 `mpi_utils` 如何用 MPI 划分二维拓扑、`multi_gpu_gpt_example` 如何设定 `tensor_para_size × pipeline_para_size = world_size`，以及多节点启动方式。
- **u7-l3 自定义 all-reduce kernel**：本讲多次提到的 `custom_all_reduce_comm_`/`custom_ar_kernels`，下一讲讲清它在 DGX-A100 拓扑上如何用 CUDA 直连代替 NCCL 降延迟，以及 `enable_custom_all_reduce` 的开启条件与两条限制。
- **回看 u6-l1 ParallelGpt**：带着本讲的「2 次 all-reduce」结论重读 `ParallelGpt::forward`，你会更清楚 logits GEMM 后那次 `ftNcclAllGather` 与 block 内 all-reduce 的区别——前者是词表切分的「收拢」，后者是行并行的「求和」。
- **延伸阅读**：[Megatron-LM 论文](https://arxiv.org/pdf/1909.08053.pdf) 的 Section 3 给出了本讲列并行/行并行的原始数学推导；NCCL 官方文档可对照 `ftNccl*` 各原语的语义与性能特性。
