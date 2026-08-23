# FA 前向 Tiling 与 InferShape 细节

## 1. 本讲目标

上一讲（u4-l1）我们从接口文档层面总览了 `flash_attention_score_enhance` 的 18 个输入、4 个输出与 14 个属性。本讲向下钻一层，进入 op_host 的两个核心文件，学完后你应该能够：

1. 说出 Tiling 入口函数 `TilingFlashAttentionScoreEnhance` 的三段式流程（参数校验 → 空输入快路径 → 责任链调度），并解释空输入快路径为什么存在。
2. 画出 InferShape 对 softmaxMax/softmaxSum/softmaxOut/attentionOut 四个输出的 shape 推导规则表，理解 BSH/BSND/SBH/BNSD/TND 五种布局如何被归一化。
3. 描述 general tiling 如何把逻辑轴 B/N2/G/S1/S2/D 切成 basic block，写出它做 UB 预算所用的输入（核数、UB 大小、dtype 字节数、缓冲份数）。
4. 解释 tilingKey 的位编码方案：Host 侧 20 个参数如何组装成一个 64bit 整数，kernel 侧又如何按位段反查。

承接关系：本讲直接依赖 u2-l3（Tiling 四项契约：blockDim/tilingKey/TilingData/workspace）与 u3-l3（TilingBase 七步流程与责任链三态返回值），并把它们落实到一个 5000 行量级的真实工业算子上。

## 2. 前置知识

- **逻辑轴记号**：FA 计算的统一抽象是五元组 \( (B, N_2, G, S_1, S_2) \) 加深度 \( D \)。\( B \) 是 batch；\( N_2 \) 是 KV 侧头数；\( G = N_1 / N_2 \) 是组查询注意力（GQA）的组数（\( N_1 \) 为 Q 侧头数，即属性 `head_num`）；\( S_1 \) 是 Q 序列长度、\( S_2 \) 是 KV 序列长度；\( D \) 是每个头的维度（Q 侧 \( D_1 \)，V 侧可以不同，记 \( D_2 \)）。
- **布局（layout）**：同一组逻辑轴在内存里可以有不同的排布方式。`BSH` 表示 3 维张量按 batch→seq→heads*dim 排；`SBH` 把 seq 放最外；`BSND`/`BNSD` 是 4 维显式分开 N 与 D；`TND` 是变长场景把 B 与 S 合并成累加 token 轴 \( T \)（回顾 u4-l1）。
- **UB（Unified Buffer）**：向量/矩阵核的片上高速存储，容量有限（典型为几十到几百 KB 量级）。tiling 的核心任务之一就是保证「一个 basic block 的所有工作缓冲同时放进 UB」。
- **basic block（基本块）**：kernel 单次循环处理的数据单元大小。\( S_1 \)、\( S_2 \)、\( D \) 三个轴各自有一个 basic block，tiling 负责为它们选值。
- **InferShape 与 Tiling 的分工**：InferShape 在图编译/shape 推导阶段决定**输出的形状**（框架要提前分配输出内存）；Tiling 在执行前决定**怎么切数据**。两者读到的上下文对象不同（`gert::InferShapeContext` vs `gert::TilingContext`）。
- **fractal（分形）= 16**：昇腾 Cube 单元的天然对齐粒度，源码中 `FRACTAL_NUM = 16`，basic block 都按 16 对齐。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `op_host/flash_attention_score_enhance_tiling.cpp`（334 行） | Tiling 入口与注册 | 模块一 |
| `op_host/flash_attention_score_enhance_infershape.cpp`（207 行） | 输出 shape/dtype 推导 | 模块二 |
| `op_host/arch32/flash_attention_score_enhance_tiling_general.cpp`（5091 行） | 六个 tiling 模板与公共基类 | 模块三 |
| `op_kernel/arch32/flash_attention_score_enhance_template_tiling_key.h`（数百行） | tilingKey 位段声明（kernel 侧契约） | 模块四 |
| `op_kernel/arch32/flash_attention_score_enhance_tiling.h`（3334 行） | TilingData 结构体定义（Host 写、Device 读） | 模块三引用 |
| `op_host/flash_attention_score_enhance_tiling_common.h`（34 行） | 跨模板共享的 `CompileInfo` 结构 | 模块一引用 |

> 提醒：`arch32` 目录名对应 A2 代芯片实现（回顾 u1-l2、u9-l1 会系统讲 arch32/arch35 的差异），本算子目前只有这一套 arch 目录。

## 4. 核心概念与源码讲解

### 4.1 模块一：Tiling 入口三段式——校验、空输入快路径、责任链调度

#### 4.1.1 概念说明

`flash_attention_score_enhance_tiling.cpp` 是 tiling 的「门卫 + 调度台」。它自己**不做任何切分计算**，只完成三件事：

1. **粗校验**（`CheckParams`）：用极低成本拦下明显非法的输入组合；
2. **空输入快路径**（`IsEmptyInput`）：query/key 元素数为 0 时，FA 的数学结果是全零输出，此时根本不需要走复杂的注意力切分，只需规划「如何把输出清零」；
3. **责任链调度**（`TilingRegistryNew::DoTilingImpl`）：把真正的切分工作交给注册表里按优先级排好的六个模板（模块三）。

#### 4.1.2 核心流程

```text
TilingFlashAttentionScoreEnhance(context)
  ├─ CheckParams(context)            # 失败 → GRAPH_FAILED
  ├─ 取 PlatformInfo（判空）
  └─ IsEmptyInput(context)
       ├─ 命中（q/k 为空且输出非空）
       │    ├─ GetEmptyArgs：按 32Byte 块把 attentionOut / softmaxSum 均分到各核
       │    ├─ SetTilingKey(FA_EMPTY_TILING_KEY = 1)
       │    ├─ SetBlockDim(CalcTschBlockDim(aivActualNum, ...))
       │    └─ workspace[0] = 100MB → return SUCCESS（短路结束）
       └─ 未命中 → TilingRegistryNew::GetInstance().DoTilingImpl(context)
                    # 按优先级 90→98 依次尝试六个模板（模块三）
```

另一条平行入口 `TilingPrepareForFlashAttentionScoreEnhance` 是 **TilingParse** 钩子：它在编译期探测一次平台参数，存进 `FlashAttentionScoreEnhanceCompileInfo`，供运行期 tiling 免探测复用。

#### 4.1.3 源码精读

**入口函数与责任链调度**（[flash_attention_score_enhance_tiling.cpp:L287-L306](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L287-L306)）：校验 → 平台信息判空 → 空输入短路 → `DoTilingImpl`。注意 `TilingRegistryNew` 正是 u3-l3 讲过的**带 socVersion 的模板注册表**，这里把 5000 行的切分逻辑整体委托出去。

**粗校验 CheckParams**（[flash_attention_score_enhance_tiling.cpp:L71-L120](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L71-L120)）：按 `input_layout` 字符串长度分派——长度 3（BSH/SBH/TND）时检查 Q/K 的 B 维（或 S 维、D 维）一致且 `kD >= vD`；长度 4（BSND/BNSD）时检查 B 维与 D 维一致。这是「进模板之前的最低门槛」，更细的约束（如 `n1Size % n2Size == 0`）由模块三的 `AnalyzeLayout` 负责——**两层校验各管一段**，这是大型算子的常见分工。

**空输入快路径 IsEmptyInput**（[flash_attention_score_enhance_tiling.cpp:L218-L285](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L218-L285)）：当 `(queryShapeSize == 0 || keyShapeSize == 0)` 且输出非空时命中。`GetEmptyArgs`（[L122-L216](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L122-L216)）把输出张量按 `MIN_COPY_UINT_SIZE = 32` 字节分块，处理「块数与核数」的三种整除关系（源码 L237-L251 的注释画得很清楚：整除 / 块数少于核数 / 块数多于核数三种主尾核划分）。随后写入 tilingKey=1、按实际用到的核数设置 blockDim、并预留 100MB workspace（L274-L281）。

**TilingParse 平台探测**（[flash_attention_score_enhance_tiling.cpp:L308-L325](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L308-L325)）：一次性取 AIV/AIC 核数与 UB/L1/L0C/L2 容量存入 `CompileInfo`（结构定义见 [flash_attention_score_enhance_tiling_common.h:L24-L32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling_common.h#L24-L32)）。运行期若 `GetPlatformInfo()` 返回空，模块三的 `GetPlatformInfo()` 就回退读这份缓存（见模块三 4.3.3 第一段）。

**注册块**（[flash_attention_score_enhance_tiling.cpp:L327-L331](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L327-L331)）：

```cpp
IMPL_OP(FlashAttentionScoreEnhance)
    .Tiling(TilingFlashAttentionScoreEnhance)
    .TilingInputsDataDependency({7, 8, 9, 10, 11})
    .TilingParse<FlashAttentionScoreEnhanceCompileInfo>(TilingPrepareForFlashAttentionScoreEnhance);
```

`TilingInputsDataDependency({7, 8, 9, 10, 11})` 声明下标 7~11 的输入（prefix、actualSeqQLen、actualSeqLenKV、qStartIdx、kvStartIdx）的**值**要在 tiling 阶段可读——这与 u4-l1 讲过的 def 侧 `ValueDepend(OPTIONAL)` 声明精确互锁：def 说「这五个张量的值影响编译」，tiling 说「我 tiling 时要读它们的值」。两侧下标必须一致，否则 tiling 读到的是空指针。

#### 4.1.4 代码实践

**实践目标**：验证「空输入快路径」与「责任链」是两条互斥路径，并理解 tilingKey=1 的去向。

**操作步骤**（源码阅读型，无需 NPU）：

1. 打开 kernel 入口 [op_kernel/flash_attention_score_enhance.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_kernel/flash_attention_score_enhance.cpp)，用编辑器搜索 `KernelTypeKey` 或值为 1 的 tilingKey 分支，找到空 tensor 的 kernel 分支（该分支负责把输出清零）。
2. 对照本模块 `FA_EMPTY_TILING_KEY = 1`（[flash_attention_score_enhance_tiling.cpp:L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L43)）与模块四 kernel 侧位段表里 `KernelTypeKey` 的注释「1: 空tensor场景」（[flash_attention_score_enhance_template_tiling_key.h:L33-L34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_kernel/arch32/flash_attention_score_enhance_template_tiling_key.h#L33-L34)），确认两侧行为对齐。
3. 数一数 `CheckParams` 里 `OP_CHECK_IF` 的个数，按「B 维检查 / D 维检查 / 布局合法性」分类。

**需要观察的现象**：kernel 入口确实存在一个只做清零/拷贝的独立分支，且其触发条件（tilingKey 最低 4 bit 为 1）与 Host 侧 `FA_EMPTY_TILING_KEY` 同值。

**预期结果**：得到「Host 写 1 → Device 按 KernelTypeKey=1 分派」的完整证据链。第 3 步的计数应与你的分类表行数一致（共 7 处 `OP_CHECK_IF`，分布为：BSH 的 B 维 1 处、TND 的 D 维 1 处、SBH 的 S 维 1 处、3 维布局共用的 `kD < vD` 1 处、4 维布局的 B 维/D 维/`kD < vD` 各 1 处）。

#### 4.1.5 小练习与答案

**练习 1**：为什么空输入场景要预留 100MB workspace，而不是 0？

**答案**：workspace 是按「最坏情况」预留的运行时缓冲（见 [L280-L281](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L280-L281) 注释「workspace上预留100M」）。框架按 tiling 报告的大小一次性分配内存池，宁可保守也不能让 kernel 越界；清零型 kernel 本身可能不需要这么多，但统一的预留策略简化了内存管理。源码未解释 100MB 的推导依据，属于经验值。

**练习 2**：如果删掉 `IsEmptyInput` 的短路，让空输入也走责任链，最可能发生什么？

**答案**：责任链模板的 UB 预算与 basic block 计算都以 \( S_1, S_2 \ge 16 \) 为前提（basic block 按 `FRACTAL_NUM=16` 对齐、循环从 16 步进），元素数为 0 时 `CalcS1S2BasicBlock` 可能找不到合法块（`s2BasicBlock` 保持 `int64_t::max()`，[tiling_general.cpp:L1991-L2003](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1991-L2003)），全部模板返回失败，算子报错。快路径本质上是把「数学上平凡的输入」从通用切分逻辑里摘出来。

### 4.2 模块二：InferShape——输出 shape 的推导规则

#### 4.2.1 概念说明

InferShape 回答的问题是：**框架该为四个输出各分配多大的内存**。它发生在 tiling 之前，因此只能依赖静态信息：输入 shape 与少量属性（`head_num`、`input_layout`、`out_dtype`）。

一个必须先纠正的常见误解：**InferShape 并不读取 `actual_seq_lengths` 张量**。变长信息（每个 batch 的真实长度）是 tiling 阶段才消费的（模块三的 `GetActualSeqLenData`）；InferShape 在 TND 布局下只取 query 的第 0 维 \( T \)（总 token 数）作为输出尺寸——因为输出张量必须按最大可能的 \( T \) 分配，而不能按运行期才知道的有效长度分配。学习目标里「根据 actualSeqQLen 推导输出」的准确表述是：**TND 布局的输出形状由 query 的 T 维（即 actualSeqQLen 累加和的上界）决定，且该值在图编译期就已在 shape 里**。

#### 4.2.2 核心流程

```text
InferShapeFlashAttentionScoreEnhance(context)
  ├─ 读 query/key/value shape + attrs(head_num, input_layout)
  ├─ 布局白名单校验（大小写不敏感，5 种）
  ├─ 解析 B/S/T：
  │    SBH: B=dim1, S=dim0；TND: T=dim0；BSND/BSH: B=dim0, S=dim1；BNSD: B=dim0, S=dim2
  ├─ softmaxMax（输出0）与 softmaxSum（输出1）：同 shape
  │    非 TND → (B, N, S, 8)，fp32
  │    TND    → (T, N, 8)，fp32
  ├─ softmaxOut（输出2）：四维全部置 0（占位输出）
  └─ attentionOut（输出3）：复制 query shape，再按布局修正 H/D 维
       BSND/BNSD → dim3 ← value.dim3 (D2)
       BSH/SBH   → dim2 ← N1 * D2（由 h1/h2/h3 反推）
       TND       → dim2 ← value.dim2 (D2)
```

#### 4.2.3 源码精读

**布局白名单**（[flash_attention_score_enhance_infershape.cpp:L57-L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_infershape.cpp#L57-L67)）：先 `toupper` 归一化再比对五种合法值，非法布局直接 `GRAPH_FAILED`。注意 def 侧 `input_layout` 只是自由字符串（[flash_attention_score_enhance_def.cpp:L387](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L387) `String()`），**真正的枚举约束在这里落地**——这再次印证 u2-l5 的结论「接口契约以 def+代码为准，docs 可能滞后」。

**B/S/T 解析**（[flash_attention_score_enhance_infershape.cpp:L69-L84](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_infershape.cpp#L69-L84)）：同一个逻辑轴在不同布局下位于不同 dim 下标，这 15 行就是布局归一化的最小实现。

**softmaxMax/softmaxSum 的 shape**（[flash_attention_score_enhance_infershape.cpp:L94-L114](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_infershape.cpp#L94-L114)）：online softmax 滚动维护的行最大值与指数和（u4-l1），每行存 8 个 fp32（`FLA_SOFTMAXMAX_F32_DIM0SHAPE = 8`，L32）。非 TND 为 `(B, N, S, 8)`；TND 把 B、S 合并成 \( T \)，为 `(T, N, 8)`。源码没有注释解释「8」的来历（推断与向量通道/对齐有关，**待确认**），但可以确定它是双侧常量：softmaxSum 直接整体赋值为 softmaxMax 的副本（L114）。

**softmaxOut 被置为全零 shape**（[flash_attention_score_enhance_infershape.cpp:L116-L124](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_infershape.cpp#L116-L124)）：注释写着 `(B, N, S, S)`，但四个 `SetDim(i, 0)` 把总元素数压到 0——即本算子**不物化** softmax 中间矩阵（\( S \times S \) 的注意力矩阵正是 FA 要避免落盘的东西，这正是 FlashAttention 的立意）。tiling 侧另有一个 `softmax_out_layout` 属性（第 13 个属性，[tiling_general.cpp:L1143-L1148](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1143-L1148)），值为 "TND" 时置 `tndSoftmaxOut=1` 并阻止布局转 BSH（[L1215-L1217](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1215-L1217)），可视为该输出的特殊开关。

**attentionOut 的 shape**（[flash_attention_score_enhance_infershape.cpp:L126-L157](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_infershape.cpp#L126-L157)）：先整体复制 query shape（保证 B/S/N 轴一致），再按布局修正最后一维：

- 4 维布局（BSND/BNSD）：`dim3 ← value.dim3`，即 \( H \) 轴的 \( D \) 部分换成 V 的 \( D_2 \)；
- 3 维布局（BSH/SBH）：H 轴是 \( N \) 与 \( D \) 的乘积，无法直接下标，于是反推：
  \( D_1 = h_1 / N_1 \)，\( N_2 = h_2 / D_1 \)，\( D_2 = h_3 / N_2 \)，最终 `dim2 ← N1 * D2`（L133-L152）。任何一步除出 0 都提前返回全零 shape（防御式）；
- TND：`dim2 ← value.dim2`。

**InferDataType**（[flash_attention_score_enhance_infershape.cpp:L161-L200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_infershape.cpp#L161-L200)）：softmaxMax/Sum 恒为 fp32（中间量高精度）；输入是三种 fp8 之一时，softmaxOut/attentionOut 的 dtype 由 `out_dtype` 属性决定（0→fp16，1→bf16），否则与输入同 dtype。这对应 def 里的 `out_dtype` 属性（[flash_attention_score_enhance_def.cpp:L393](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L393)）。

#### 4.2.4 代码实践（本讲核心实践之一）

**实践目标**：整理 BSH/BSND/SBH 三种布局下 InferShape 的输出 shape 推导规则表。

**操作步骤**：

1. 逐行阅读 [L69-L157](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_infershape.cpp#L69-L157)，填写下表（「参考答案」列先遮住）：

| 布局 | query shape | softmaxMax/Sum | attentionOut 最后一维 |
| --- | --- | --- | --- |
| BSH | (B, S, N1*D1) |  |  |
| BSND | (B, S, N1, D1) |  |  |
| SBH | (S, B, N1*D1) |  |  |

2. 用 numpy 验证 BSH 的 H 轴反推链（**示例代码**，非项目代码）：

```python
import numpy as np
B, S, N1, D1, N2, D2 = 2, 128, 8, 128, 4, 128   # GQA: G = N1/N2 = 2
q = np.zeros((B, S, N1 * D1), dtype=np.bfloat16)
k = np.zeros((B, S, N2 * D1), dtype=np.bfloat16)
v = np.zeros((B, S, N2 * D2), dtype=np.bfloat16)

h1, h2, h3 = q.shape[2], k.shape[2], v.shape[2]   # 对应 infershape L138-L150
d1 = h1 // N1
n2 = h2 // d1
d2 = h3 // n2
assert (d1, n2, d2) == (D1, N2, D2)
print("attentionOut:", (B, S, N1 * d2))           # 期望 (2, 128, 1024)
print("softmaxMax:",  (B, N1, S, 8))              # fp32 中间量
```

**需要观察的现象**：反推链在 GQA（N1 ≠ N2）与 MLA 风格（D1 ≠ D2）下都能得到正确 attentionOut 的 H 轴。

**预期结果**：规则表填毕（参考答案：softmaxMax/Sum 三种布局均为 (B, N1, S, 8)；attentionOut 最后一维 BSH/SBH 为 N1*D2、BSND 为 D2，整体 shape 与 query 同构）；numpy 断言通过，打印 `(2, 128, 1024)`。

#### 4.2.5 小练习与答案

**练习 1**：TND 布局下 softmaxMax 是 `(T, N, 8)`，为什么 B、S 可以合并而 (B, N, S, 8) 不行？

**答案**：因为 softmax 是**按行**做的：每一行（一个 query token）对应一组 max/sum。TND 里每个 token 已经带上了自己的 head 信息（T 展平了 B×S），行与行之间独立，所以 (T, N, 8) 无歧义。而 attentionOut 也复制 query shape，同样成立。反例是 softmaxOut——\( S_1 \times S_2 \) 的注意力矩阵无法用一维 T 描述边界，这也是它被置零/需特殊开关的原因之一。

**练习 2**：BSH 反推链中若 `h1 % N1 != 0` 会怎样？

**答案**：`D1 = h1 / N1` 整除失败会得到错误的 D1 并连锁污染 N2、D2。infershape 对此的防御是「结果为 0 就返回全零 shape」（L134-L149 的三个 `if == 0` 分支）；tiling 侧 `AnalyzeLayout` 则有更早的显式校验 `h1 % n1Size != 0 → 报错`（[tiling_general.cpp:L1449-L1451](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1449-L1451)）。两个阶段的容错策略不同：InferShape 倾向「退化成空输出不崩图」，tiling 倾向「明确报错」。

### 4.3 模块三：general tiling——布局归一化与 B/N2/G/S1 切分

#### 4.3.1 概念说明

`arch32/flash_attention_score_enhance_tiling_general.cpp` 是一个 5091 行的「模板家族」：一个公共基类 `FlashAttentionScoreEnhanceTilingBase`（实现 u3-l3 讲的 TilingBase 七步流程）+ 六个子类模板，按优先级注册进责任链。它解决三个问题：

1. **布局归一化**：把 5 种输入布局统一翻译成逻辑五元组 \( (B, N_2, G, S_1, S_2) + (D, D_2) \)，后续切分逻辑只认逻辑轴；
2. **basic block 选择**：为 \( S_1/S_2/D \) 选出能塞进 UB 的块大小；
3. **多核划分**：把 \( B \cdot N_2 \cdot G \cdot S_1^{outer} \) 个任务块均分到 AIV 核上。

#### 4.3.2 核心流程

基类把一次 tiling 固化为七步（承接 u3-l3 的模板方法模式）：

```text
FlashAttentionScoreEnhanceTilingBase（七步，见 L352-L373 注释）
  1 GetPlatformInfo   # AIV/AIC 核数、UB/L1/L0C/L2（优先实时探测，回退 CompileInfo）
  2 GetShapeAttrsInfo # 读 shape/attr → AnalyzeDtype/AnalyzeAttrs/AnalyzeLayout/AnalyzeOptionalInput
  3 (IsCapable)       # 子类自判是否接单
  4 DoOpTiling        # MatchTemplate 选 basic block → SetCoreParams/SetMultiCoreParams 等填 TilingData
  5 DoLibApiTiling    # Matmul/SoftMax 等高阶 API 的 tiling
  6 GetWorkspaceSize  # 子类按 stage 倍数估算 workspace
  7 PostTiling        # CalcTschBlockDim → SetBlockDim；needDropMaskOp 时追加 workspace
```

其中布局归一化的关键换算（3 维布局，H 轴是打包的）：

\[ D_1 = \frac{h_1}{N_1}, \quad G = \frac{h_1}{h_2}, \quad N_2 = \frac{h_2}{D_1}, \quad D_2 = \frac{h_3}{N_2} \]

而 UB 预算决定 \( S_1 \) 块上限（基类通用公式，\( X/Y/E \) 为该模板的缓冲仂数）：

\[ \text{maxS1} = \left\lfloor \frac{\text{UB} / \text{dtypeBytes}}{16 \cdot X + D \cdot Y + (E + 2) \cdot (32 / \text{dtypeBytes})} \right\rfloor_{16} \]

多核划分（均分 \( B \cdot N_2 \cdot G \cdot S_1^{outer} \) 个任务）：

\[ \text{coreNum} = \min(B \cdot N_2 \cdot G \cdot S_1^{outer},\ \text{aivNum}), \quad \text{splitFactorSize} = \lceil \text{totalSize} / \text{coreNum} \rceil \]

#### 4.3.3 源码精读

**（1）平台信息：实时探测 + CompileInfo 回退**（[tiling_general.cpp:L593-L624](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L593-L624)）：`GetPlatformInfo()` 先看 `context_->GetPlatformInfo()`，拿不到就读模块一 TilingParse 预填的 `CompileInfo`。L615-L621 的 `OP_LOGI` 会把 aivNum/aicNum/ubSize/l1Size/l0cSize 打进日志——这是实践环节拿真实数值的入口。

**（2）布局归一化 AnalyzeLayout → Analyze3DimLayout / Analyze4DimLayout**（[tiling_general.cpp:L1172-L1197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1172-L1197)）：先校验 shape 维数等于布局字符串长度、`n1Size % n2Size == 0`、\( D \le 768 \)，然后分派：

- **BSH**（[L1294-L1305](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1294-L1305)）：`s1=query.dim1, b=query.dim0, s2=key.dim1`，H 轴 stride 记录 `s1StrideSize=h1` 等（供 kernel 寻址），并写入 `layoutType=LAYOUT_BSH`；
- **SBH**（[L1306-L1317](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1306-L1317)）：`s1=query.dim0, b=query.dim1`，stride 相差一个 bSize 因子；
- **TND**（[L1318-L1375](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1318-L1375)）：读 `actual_seq_qlen/kv_len` 张量（正是模块一 ValueDepend 声明的下标 8/9，常量在 [L44-L45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L44-L45)）解出每个 batch 的长度（差分，`GetActualSeqLenData` [L1241-L1281](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1241-L1281)），校验累加和不超过 T，然后尝试 `CouldConvertTND2BSH`（[L1199-L1239](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1199-L1239)）：**等长变长（每个 batch 长度相同且无 rope/pse/特殊稀疏）时降级为 BSH 走通用模板**，只有真变长才保留 TND 语义；
- 3 维布局收尾的 H 轴反推（[L1448-L1455](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1448-L1455)）：与模块二 infershape 的反推公式完全一致——**两份代码各写一遍，修改时必须双侧同步**；
- **BSND/BNSD**（[L1461-L1506](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1461-L1506)）：N/D 显式分轴，直接取下标，并校验 `head_num == query 的 N 维`。

**（3）DoOpTiling：模板匹配 + 填 TilingData**（[tiling_general.cpp:L1942-L1978](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L1942-L1978)）：`MatchTemplate` 失败返回 `GRAPH_PARAM_INVALID`（让位给责任链下一模板，u3-l3 三态语义）；成功则依次调 `SetQKVStartIdx → SetCoreParams → SetMultiCoreParams → SetTensorSizeParams → SetSparseParams → SetPseAlibiParams`。

**（4）basic block 选择的两条路线**：

- *通用 UB 预算路线*（基类，[L2025-L2115](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2025-L2115)）：`CalcS1S2BasicBlock` 用双层循环（\( S_1 \) 从最小 64 开始、步进 16；\( S_2 \) 从上限往下减 16）试探，`CalcMaxS1BasicBlockSize`（L2082-L2094）与 `CalcMaxS2BasicBlockSize`（L2096-L2115）按上面的 UB 公式给出边界，逐个候选调 `CalcUBSize + SetMatMulTiling` 验证。L1982 的注释写出预算方程：`s1s2*X + s1d*Y + s1*expNum*32 + s1*64 + apiTmp ≤ UB`；
- *经验值路线*（高性能模板覆盖）：例如 `S1s2Bn2gs1` 直接 `s1BasicBlock = min(64, alignedS1)`，仅当 `B*N1*G*ceil(S1/64) > aivNum`（任务数超过核数，需要更大块摊薄调度开销）时升到 128（[L3489-L3500](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3489-L3500)）。当前六个注册模板中，五个覆盖了 `CalcS1S2BasicBlock`，通用公式是「未覆盖子类的默认路径 + 理解方法论的最佳教材」。

**（5）B/N2/G/S1 多核切分三连**：

- `SetCoreParams`（[L2141-L2170](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2141-L2170)）：为 \( S_1/S_2/D \) 各写 `BaseSize / BaseTailSize / OuterSize` 三件套（`OuterSize = ceil(size, base)`、`TailSize = 最后一块的实际大小`），并计算 `nRatio`（BMM 与 softmax 的块比，默认 `BMM_SOFTMAX_RATIO=4`，[L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L52)）；
- `SetMultiBatchCoreParams`（[L2172-L2186](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2172-L2186)）：B/N2/G 三轴 **Base 恒为 1、Outer 为总维数**——即这三个轴不块内切分，每块任务对应一个完整的 (b, n2, g) 组合；
- `SetMultiCoreParams`（[L2188-L2199](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2188-L2199)）：`totalSize = bOuter * n2Outer * gOuter * s1Outer`，`coreNum = min(totalSize, aivNum)`，`splitFactorSize = ceil(totalSize / coreNum)`。kernel 侧每个核拿 `GetBlockIdx()` 乘 `splitFactorSize` 反查自己的任务区间——与 u2-l3/u2-l4 的「Host 乘法、Device 取模」互逆契约一致。这些字段落在 `MultiCoreParams` 结构（[flash_attention_score_enhance_tiling.h:L2018-L2025](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_kernel/arch32/flash_attention_score_enhance_tiling.h#L2018-L2025)，含 48 项 `sparseStartIdx` 稀疏负载均衡表）。

**（6）PostTiling：blockDim 的最终落笔**（[tiling_general.cpp:L2212-L2252](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2212-L2252)）：`SetBlockDim(CalcTschBlockDim(coreNum, aicNum, aivNum))`；若 `needDropMaskOp==1`（drop mask 预处理算子接管），blockDim 改为全 AIV 并追加 workspace。

**（7）六个模板的注册**（[tiling_general.cpp:L5054-L5089](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L5054-L5089)）：`DropMask(90) → VarLen(94) → S1s2Bn2gs1SameAB(95) → S1s2Bn2gs1(96) → S1Bn2gs1(97) → B(98)`，socVersion 限 `ASCEND910B` 与 `ASCEND910_93`。类名即切分方案：`S1s2Bn2gs1` 表示「循环最外层按 B/N2/G/S1 组织、S2 在核内循环」；`S1s2Bn2gs1SameAB` 是 Q=K（self-attention）特化（`IsSpecialShape` 见 [L3520-L3526](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3520-L3526)）；`S1s2Bn2gs1::IsCapable` 要求 `s2Size > s2sizeLimitMin(=1024)`（[L3673-L3679](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3673-L3679)、[L3475](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3475)）——S2 太小则让位给后面的模板。

值得专门一提的是 **DropMask 模板的接力设计**（[L4993-L5029](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L4993-L5029)）：它把 drop mask 预处理的切分参数写进共享 `dropmaskParams` 后**返回 `GRAPH_PARAM_INVALID`**——故意「不及格」，让责任链继续走真正的注意力模板。这就是 u3-l3 说的「各模板经共享 context TilingData 接力合作」：优先级 90 的模板只负责填自己那部分字段。

#### 4.3.4 代码实践（本讲核心实践之二）

**实践目标**：跟踪一个真实分支，列出它计算 block 切分所用的全部输入并注明行号。

**操作步骤**：

1. 选定 `S1s2Bn2gs1::CalcS1S2BasicBlock`（[tiling_general.cpp:L3489-L3500](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3489-L3500)），整理它的输入清单（参考答案如下表）：

| 输入 | 来源 | 行号 |
| --- | --- | --- |
| `aivNum`（核数） | `GetPlatformInfo` L600 或 L608（CompileInfo 回退/实时探测） | L600, L608 |
| `bSize/n1Size/gSize/s1Size/s2Size/dSize`（逻辑轴） | `AnalyzeLayout` 系列 L1294-L1506 | L1493 |
| `alignedS1/alignedS2`（16 对齐后的轴长） | 布局分析阶段 | L3491, L3496 |
| `S2_NZTOND_SIZE_64 / D_SPECIFIC_SIZE`（NZ 格式特判阈值） | 文件头常量 L89, L72 | L3497 |

2. 再对照基类通用公式 `CalcMaxS1BasicBlockSize`（[L2082-L2094](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2082-L2094)），补全它的输入清单：`aicoreParams_.ubSize`（UB 大小）、`inputDtypeBytes`（dtype 字节数）、`bufferNum` 的三个份数 `bufferS1S2Num/bufferS1DNum/bufferExpNum`（来自子类 `GetBufferNum`，如 S1s2Bn2gs1 在 [L3484-L3487](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3484-L3487) 填 `HIGH_PERF_BUFFER_NUM=6`）、`FRACTAL_NUM=16`（[L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L35)）与 `actualD`（splitD 为 0 时取 `alignedD`，否则取 16）。
3. （可选，需 NPU 环境）在实际运行中打开 host 日志，观察 `GetPlatformInfo` 的 `OP_LOGI`（L615-L621）打印的真实 aivNum/ubSize，代入公式手算 maxS1，与日志里 `final basic block`（[L2012-L2018](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2012-L2018)）比对。无 NPU 环境时此步**待本地验证**。

**需要观察的现象**：两条路线（经验值 vs UB 公式）输入集完全不同——前者只看核数与逻辑轴，后者还依赖 dtype 字节数与缓冲份数；这正是「性能特化」的代价与收益。

**预期结果**：两张输入清单表填写完整，且能指出 `bSize*n1Size*gSize*CeilDiv(s1Size, s1BasicBlock) > aivNum` 这个判断（L3493）的含义：任务块数多于核数时才把 \( S_1 \) 块从 64 提到 128。

#### 4.3.5 小练习与答案

**练习 1**：`SetMultiBatchCoreParams` 把 B/N2/G 的 BaseSize 固定为 1，为什么这三个轴不需要「块」？

**答案**：多核并行的粒度就是 (b, n2, g, s1_block) 四元组。B/N2/G 的单项任务（一个头组的一次注意力）已经足够大，拆开只会增加索引复杂度；真正需要核内再切的是 \( S_1 \)（决定 UB 占用）与 \( S_2 \)（循环维）。`totalSize = B·N2·G·S1Outer` 直接就是任务块总数（L2192-L2193）。

**练习 2**：`nRatio` 是什么？为什么 `SetCoreParams` 里 splitS2 时要把 `s2OuterSize` 再除一次？

**答案**：`nRatio` 是 BMM1 输出（\( S_1 \times S_2 \) 的注意力打分矩阵）与 softmax 处理块之间的尺寸比（默认 4，L52；S1s2Bn2gs1 覆盖为 8/5/6 的分段值，[L3502-L3513](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3502-L3513)）。S2 被切分时（`splitS2==1`），多个核合作同一行，s2Outer 段需要按 nRatio 重新组合（L2150-L2152），保证 softmax 拿到完整行。它最终也写进 workspace 估算（[L3712](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3712) `bmm1Bytes = nRatio * bmm1ResUbSize * calcTypeSize`）。

### 4.4 模块四：tilingKey 位编码——layout 与排布组合的唯一标识

#### 4.4.1 概念说明

u2-l3 见过的朴素 tilingKey（0=BF16、1=FP16）在这里进化成**64bit 位域编码**：一次 tiling 的所有「分支选择」——布局、dtype、稀疏模式、哪些可选输入存在、切了哪些轴——各占几个 bit，拼成一个整数。kernel 侧拿到这个整数即可 O(1) 地反查自己该走哪条编译路径，而不用把这些信息再序列化一遍。

#### 4.4.2 核心流程

```text
Host 侧                                     Kernel 侧
GET_TPL_TILING_KEY(20 个参数)                ASCENDC_TPL_ARGS_DECL 位段表
  KernelTypeKey/UB0/UB1/Block/ImplMode        (bit3-0, 7-4, 11-8, 15-12, ...)
  /DataType/Layout/Bmm1Format/.../DTemplateType
  └── 按位段组装 64bit ──SetTilingKey──►       按位段反查 → 选 kernel 实例 + TilingData 结构
```

#### 4.4.3 源码精读

**Host 侧取值**（以 `S1s2Bn2gs1` 为例，[tiling_general.cpp:L3648-L3671](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3648-L3671)）：`GET_TPL_TILING_KEY(0, S1, S2, NONE, implMode, tilingKeyDType, tilingKeyLayout, tilingKeyBmm1Format, ...)` 共 20 个参数。`GET_TPL_TILING_KEY` 宏本身由 CANN 包的 `ascendc/host_api/tiling/template_argument.h` 提供（不在本仓库），本仓库负责的是**参数取值**与**位段声明**。注意 L3650 注释「not care about layout in tiling key, pass BSND(enum value is 0)」与枚举实际值有出入（见练习 2）。

**位段声明**（[flash_attention_score_enhance_template_tiling_key.h:L32-L165](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_kernel/arch32/flash_attention_score_enhance_template_tiling_key.h#L32-L165)）：`ASCENDC_TPL_ARGS_DECL` 逐段声明，每段带注释枚举合法值。节选关键段：

| 位段 | bit | 合法值 |
| --- | --- | --- |
| KernelTypeKey | 3-0 | 0=普通，1=空 tensor（与模块一 FA_EMPTY_TILING_KEY 对上） |
| UB0 / UB1 / Block | 7-4 / 11-8 / 15-12 | 0=B, 1=N2, 2=G, 3=S1, 4=S2, 5=D, 9=NONE（哪个轴进双缓冲/外层块） |
| ImplMode | 17-16 | 0=高精度, 1=高性能, 2=无效行高精度 |
| DataType | 19-18 | 0=FP16, 1=FP32, 2=BF16, 3=FP16_PRECISION |
| Layout | 22-20 | 0=NONE, 1=BSH, 2=SBH, 3=BNSD, 4=TND（[L79-L85](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_kernel/arch32/flash_attention_score_enhance_tiling_key.h#L79-L85)） |
| Bmm1Format / Bmm2Source | 23 / 24 | ND vs NZ；GM vs L1 |
| Sparse | 28-25 | 0=ALL … 9=BAND_LEFT_UP_CAUSAL |
| S1/S2/DTemplateType | 40-37 / 44-41 / 48-45 | 16 的倍数对齐档位（ALIGNED_16…ALIGNED_128） |

Host 侧与之配套的内部枚举 `LayoutType`（[tiling_general.cpp:L137-L144](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L137-L144)）里 `LAYOUT_BSH = 1` 与 `LAYOUT_BSND = 1` **故意同值**——kernel 对这两种布局走同一套已归一化的处理路径。

**key 与 TilingData 结构的绑定**（[flash_attention_score_enhance_template_tiling_key.h:L202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_kernel/arch32/flash_attention_score_enhance_template_tiling_key.h#L202)）：`ASCENDC_TPL_TILING_STRUCT_SEL(FlashAttentionScoreEnhanceGeneralTilingData)` 声明「这组 key 取值组合对应读哪个 TilingData 结构」——不同模板分支可以配不同的 TilingData 布局，key 同时承担**结构选择**职责。文件 L21 的注释还说明：kernel 通过宏按 dtype 隔离编译 tilingKey 以降低编译耗时。

**Host 侧还有一个「模板匹配用」的 TilingKey 位域类**（[tiling_general.cpp:L278-L312](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L278-L312)）：`splitS1/splitS2/splitD/dtype/layoutType/sparseType` 的 32bit 小位域，它是 `MatchTemplate` 里 `expectTemplate == actualTemplate` 的比较载体（[L391-L394](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L391-L394)、[L2006-L2010](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2006-L2010)）——**注意区分**：这个内部类只服务于「模板自己的预期与实际切分是否一致」的自检，真正写给 kernel 的是 `GetTilingKey()` 的 64bit 结果。

#### 4.4.4 代码实践

**实践目标**：手工演算一个 tilingKey，打通「参数 → 位段 → 整数」的映射。

**操作步骤**（纸笔即可，无需环境）：

1. 设定场景（**示例演算**，非实际运行值）：`KernelTypeKey=0`、`UB0=3(S1)`、`UB1=4(S2)`、`Block=9(NONE)`、`ImplMode=0`、`DataType=2(BF16)`、`Layout=1(BSH)`、`Bmm1Format=0`、`Bmm2Source=0`、`Sparse=2(ANY)`、`BigDoubleBuffer=2`、其余全部 0。
2. 按位段表逐项移位求和：
   \[ k = 3 \cdot 2^4 + 4 \cdot 2^8 + 9 \cdot 2^{12} + 2 \cdot 2^{18} + 1 \cdot 2^{20} + 2 \cdot 2^{29} = 1075352624 \]
3. 反向验证：把 1075352624 写成二进制，逐段切出 20 个字段，应与步骤 1 的设定完全一致。

**需要观察的现象**：位段之间互不干扰——任何单个字段的变化只翻转自己那几 bit。

**预期结果**：正反两个方向都能算通；随后回到 [L3648-L3671](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3648-L3671) 对照，能说出 20 个实参各自落在哪个位段。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Layout 位段只有 5 个值，BSH 和 BSND 共用 1？

**答案**：Host 侧 `Analyze3DimLayout/Analyze4DimLayout` 已经把两种布局归一化到相同的逻辑轴与 stride 信息（写入 TilingData 的 `layoutType` 字段做细微区分），kernel 的计算主干不需要再分叉；共用编码可以少一半的 kernel 实例化组合，缩短编译时间（这正是该文件 L21 注释「kernel通过宏定义隔离dtype编译tilingkey，降低耗时」的同一动机）。

**练习 2**：[L3650](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3650) 注释说「pass BSND(enum value is 0)」，但枚举里 `LAYOUT_BSND = 1`，矛盾吗？

**答案**：这是**注释与代码不一致**的实例。代码事实以枚举定义（L137-L144）为准：`tilingKeyLayout` 在 `Analyze3DimLayout` 里被赋 `LAYOUT_BSH=1`（L1305），在 `Analyze4DimLayout` 里被赋 `LAYOUT_BSND=1`（L1484），两者同值，传给 `GET_TILING_KEY` 的 Layout 位段恒为 1。注释中「enum value is 0」的说法要么过时要么笔误。读源码时遇到此类冲突，永远以可执行代码为准——这也是本手册反复强调的方法论。

## 5. 综合实践

**任务：为一条真实的 FA 调用手工「预演」一遍 Host 侧规划。**

设某次调用的静态参数为：`input_layout="BSH"`，query `(2, 4096, 8192)`（即 B=2、S1=4096、N1=64、D1=128），key `(2, 4096, 4096)`（N2=32、G=2），value `(2, 4096, 4096)`（D2=128），dtype=BF16，无 mask/dropout/pse。

1. **InferShape 演算**：按模块二的规则表写出四个输出的 shape 与 dtype（用 4.2.4 的 numpy 脚本验证 H 轴反推）。
2. **tiling 入口预判**：判断 `IsEmptyInput` 是否命中（元素数均非 0 → 不命中），写出 `CheckParams` 将执行的三条检查（BSH 分支的 B 维一致、`kD >= vD`）。
3. **责任链预判**：`DoTilingImpl` 按 90→98 依次尝试——`DropMask` 因 `needDropMaskOp==0` 返回 PARAM_INVALID 让位；`VarLen`/`SameAB`/`S1s2Bn2gs1` 谁会接单？（提示：S1s2Bn2gs1 的 `IsCapable` 只要求 `s2Size > 1024`，L3673-L3679；VarLen 与 SameAB 的接单条件需你到 [L4113-L4400](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L4113) 与 [L3807-L4000](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3807) 的 `IsCapable` 里核实，这是本任务的调研部分）。
4. **切分演算**：假设 S1s2Bn2gs1 接单，代入 `CalcS1S2BasicBlock`（L3489-L3500）与 `SetMultiCoreParams`（L2188-L2199）：`B*N1*G*ceil(4096/64) = 2*64*2*64 = 16384`，与 aivNum 比较后确定 s1BasicBlock 取 64 还是 128；再算 totalSize（用 N2 而非 N1：`2*32*2*ceil(4096/s1BasicBlock)`）与 splitFactorSize（aivNum 用你机器的真实值，无 NPU 时留符号并标注**待本地验证**）。
5. **tilingKey 演算**：按 4.4.4 的方法组装本次调用的 key（Layout=1、DataType=2、UB0=3、UB1=4、Sparse=2、BigDoubleBuffer=2，其余按本场景的 hasDropOut/hasAttenMask/hasPse 均为 0）。

**产出物**：一份 markdown 记录，含四个输出的 shape 表、责任链判定结论及依据行号、切分数字演算过程、最终 tilingKey 的二进制分解。此任务把本讲四个模块全部串起来；数字部分凡依赖真实硬件参数（aivNum/ubSize）的，在拿到环境前都标注待本地验证。

## 6. 本讲小结

- Tiling 入口是「门卫 + 调度台」：`CheckParams` 粗校验 → `IsEmptyInput` 空输入快路径（tilingKey=1、输出清零专用切分、100MB workspace）→ `TilingRegistryNew` 责任链；TilingParse 钩子在编译期预填 `CompileInfo` 平台参数。
- InferShape 只用静态 shape + `head_num`/`input_layout`/`out_dtype` 推导输出：softmaxMax/Sum 为 (B,N,S,8) 或 (T,N,8) 的 fp32；softmaxOut 被置为全零占位；attentionOut 复制 query shape 并把最后维换成 \( N_1 \cdot D_2 \)（3 维布局需 H 轴反推）。**变长张量 actual_seq_qlen 是 tiling 阶段才消费的**，不进 InferShape。
- general tiling 的三步：布局归一化（5 种 layout → \( B/N_2/G/S_1/S_2/D/D_2 \) 逻辑轴 + stride）、basic block 选择（通用 UB 预算公式 vs 高性能模板经验值覆盖）、多核划分（`totalSize = B·N2·G·S1Outer`，`coreNum = min(totalSize, aivNum)`，B/N2/G 的 Base 恒为 1）。
- 六个模板按优先级 90/94/95/96/97/98 注册；DropMask 模板填完自己的参数后故意返回 `GRAPH_PARAM_INVALID` 让位，是「共享 TilingData 接力」模式的活样本。
- tilingKey 是 64bit 位域编码：Host 侧 `GET_TPL_TILING_KEY` 20 参数组装，kernel 侧 `ASCENDC_TPL_ARGS_DECL` 位段表反查，还通过 `TILING_STRUCT_SEL` 绑定 TilingData 结构；BSH 与 BSND 共用 Layout=1，是归一化设计在编码层的体现。
- 读大文件的方法论：先用 grep 建立类/注册表地图，再按调用链精读；注释与代码冲突时（如 L3650）以代码为准。

## 7. 下一步学习建议

下一讲 **u4-l3（FA 前向 Kernel：多 layout 模板与边界处理）** 将跨过 Host/Device 边界：本讲产出的 TilingData/tilingKey 如何被 `op_kernel` 入口按位段分发到 `arch32` 下的各布局 kernel（s1s2_bn2gs1 等），以及 drop_mask_adapter 与 empty_tensor 如何消费本讲讲到的两个特殊分支（needDropMaskOp 与 KernelTypeKey=1）。建议提前浏览 [op_kernel/flash_attention_score_enhance.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_kernel/flash_attention_score_enhance.cpp) 的入口分发结构。若想先补测试视角，可跳到 u8-l2 看 `TilingContextPara` 如何伪造本讲的 `gert::TilingContext` 来单测这些切分逻辑；u9-l2 的 tiling_sink 则展示把这些 Host 侧规划下沉到设备的另一条路线。
