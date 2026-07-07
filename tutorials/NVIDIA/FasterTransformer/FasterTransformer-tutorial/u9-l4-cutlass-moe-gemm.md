# CUTLASS 扩展与 MoE GEMM

## 1. 本讲目标

本讲是「量化与高性能 GEMM」单元的最后一篇，承接 u9-l2（权重仅量化与 CUTLASS 混合 GEMM）。学完本讲，你应当能够：

1. 说清楚 FasterTransformer（以下简称 FT）为什么要在官方 CUTLASS 之上维护一套 `cutlass_extensions`，这套扩展补齐了 cuBLAS 做不到的三件事：**混合精度 GEMM**、**分组（grouped）GEMM**、**自定义融合 epilogue**。
2. 理解 **MoE（Mixture of Experts）** 场景下「多个 expert 各自处理不同行数」的特点，以及为什么必须用专门的 **grouped MoE GEMM kernel** 一次性跑完全部 expert，而不是逐个 expert 调用 cuBLAS。
3. 看懂 `int8_gemm` 这条 w8a8（int8×int8）路径如何复用同一套 CUTLASS 设施，并理解它与 MoE GEMM 共享同一份调优启发式。
4. 掌握 `BUILD_CUTLASS_MOE` / `BUILD_CUTLASS_MIXED_GEMM` 两个 CMake 编译开关的作用，以及它们「以编译时间换运行时性能」的代价来源。

---

## 2. 前置知识

在进入本讲前，你需要先建立以下几个直觉（若不熟悉，建议先读 u9-l1 / u9-l2）：

- **GEMM 与 cuBLAS 的局限**：GEMM（通用矩阵乘）是 transformer 推理里压倒性的算力开销。FT 默认用 cuBLAS/cuBLASLt 做矩阵乘（见 u2-l3）。但 cuBLAS 是个「黑盒」：它只支持「输入 A、B 同精度、输出 C 同精度」的标准组合，无法直接表达「激活是 FP16、权重是 INT8」的混合精度，也无法把 softmax/激活/缩放等收尾操作融合进矩阵乘的尾巴。
- **CUTLASS 是什么**：CUTLASS 是 NVIDIA 开源的 C++ 模板库，把一次 GEMM 拆成可组合的零件——主循环 `Mma`（真正做乘累加的部分）和收尾 `Epilogue`（写回前对累加结果做线性变换、激活、缩放）。你可以把 CUTLASS 理解成「自己拼一个定制版 cuBLAS」的乐高积木。
- **权重仅量化（weight-only）**：u9-l2 讲过，权重存 INT8/INT4、激活仍 FP16，靠 CUTLASS 的混合精度 GEMM（fpA_intB）实现。本讲的 `int8_gemm` 是另一条路——**激活也量化成 int8**（即 w8a8），对应 u9-l1 的 `int8_mode=2/3`。
- **MoE（专家混合）**：把传统的一个 FFN 换成 \(E\) 个并行的「专家 FFN」，再加一个门控网络（gate）决定每个 token 该交给哪几个专家。MoE 是把大模型参数量做大、同时控制单次推理 FLOPs 的主流结构。

本讲会反复出现一个核心结论：**FT 的 MoE GEMM 与 int8 GEMM 共用同一套 CUTLASS 扩展骨架和同一份启发式调优代码**，差异只在「分多少组（problem_count）」和「权重是否需要反量化」。

---

## 3. 本讲源码地图

本讲涉及的关键文件分为三组：

| 分组 | 文件 | 作用 |
| --- | --- | --- |
| CUTLASS 扩展 | `src/fastertransformer/cutlass_extensions/include/cutlass_extensions/` | FT 对官方 CUTLASS 的扩展，本讲重点是其中的 MoE kernel 与 problem visitor |
| MoE GEMM | `src/fastertransformer/kernels/cutlass_kernels/moe_gemm/` | `MoeGemmRunner`：跑 grouped GEMM 的运行器 |
| MoE 编排 | `src/fastertransformer/kernels/moe_kernels.{h,cu}` | `CutlassMoeFCRunner`：把门控、置换、两次 grouped GEMM、还原串成一个完整 MoE FFN |
| int8 GEMM | `src/fastertransformer/kernels/cutlass_kernels/int8_gemm/` | `CutlassInt8GemmRunner`：s8×s8→T 的 w8a8 路径 |
| 调优 | `src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.{h,cc}` | 候选配置枚举 + 基于占用率的选优 |
| 构建开关 | `CMakeLists.txt`、`src/fastertransformer/kernels/cutlass_kernels/CMakeLists.txt` | `BUILD_CUTLASS_MOE` / `BUILD_CUTLASS_MIXED_GEMM` |
| 文档 | `docs/gpt_guide.md` | `GPT with MOE` 章节给出可运行示例 |

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要 CUTLASS 扩展：cutlass_extensions 总览

#### 4.1.1 概念说明

FT 在 `src/fastertransformer/cutlass_extensions/` 下维护了一份对官方 CUTLASS 的扩展。它的存在动机很直接：**官方 CUTLASS 不够用，而 cuBLAS 又太死板**。具体补齐了三类能力：

1. **混合精度 / 反量化 GEMM**（weight-only 量化需要）：权重是 INT8/INT4，激活是 FP16/BF16。CUTLASS 要在 warp 做乘累加的同时，**逐片把 INT 权重反量化成 FP16 再相乘**。对应扩展头 `gemm/kernel/fpA_intB_gemm.h`、`gemm/warp/mma_tensorop_dequantizer.h`、`gemm/kernel/mixed_gemm_B_layout.h`（详见 u9-l2）。
2. **分组 GEMM / 持久化 kernel**（MoE 需要）：把 \(E\) 个尺寸不同的矩阵乘塞进**同一个 kernel 的一次启动**里，让 GPU 上的线程块（threadblock）在 expert 之间动态抢任务。对应 `gemm/kernel/moe_cutlass_kernel.h`（`MoeFCGemm`）与 `gemm/kernel/moe_problem_visitor.h`。
3. **可定制的融合 epilogue**（int8 GEMM 需要）：在写回结果前，把「按行/按列乘一个 float 缩放因子」融合进去。对应 `gemm/kernel/gemm_with_epilogue_visitor.h`、`epilogue/threadblock/epilogue_per_row_per_col_scale.h`。

此外还有两个贯穿全扩展的工具头：`ft_gemm_configs.h`（定义「分块配置 / split-k / stages」的枚举）与 `compute_occupancy.h`（编译期算一个 kernel 的理论占用率，喂给启发式选优）。本讲后面会反复引用它们。

> 一句话：`cutlass_extensions` = 「官方 CUTLASS 的积木」+「FT 自己加的几块定制积木」，用来表达 cuBLAS 表达不了的结构。

#### 4.1.2 核心流程

CUTLASS 把一次 GEMM 看成「**主循环 Mma** + **收尾 Epilogue**」两段流水。FT 的扩展对这两段都做了改造，整体套路是：

```
用户调用 Runner（MoeGemmRunner / CutlassInt8GemmRunner）
   │
   ├─ dispatch_to_arch   ：按 GPU 架构（sm70/75/80）挑 CUTLASS 模板实参
   │     └─ dispatch_gemm_config ：按 tile 配置（CtaShape…）挑分块大小
   │           └─ dispatch_stages ：按 stages（2/3/4）挑流水深度
   │                 └─ generic_*_kernelLauncher ：组装 CUTLASS 类型 → gemm.run(stream)
   │
   └─ run_gemm 之前先跑启发式：get_candidate_configs → 逐个算 occupancy
                                 → estimate_best_config_from_occupancies 选最优
```

这是一棵「架构 × 分块 × 流水深度」的三维模板分派树，所有 CUTLASS kernel 都套这个套路，差别只在叶子节点组装出的 CUTLASS 类型不同。

#### 4.1.3 源码精读

先看贯穿全扩展的配置枚举。`CutlassTileConfig` 列出了所有可选的分块形状（CTA/Warp 的 M×N×K），`CutlassGemmConfig` 把「分块 + split-k + 流水 stages」打包成一个可比较的配置对象：

[ft_gemm_configs.h:22-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/ft_gemm_configs.h#L22-L56) 定义了 `CutlassTileConfig`、`SplitKStyle`、`CutlassGemmConfig` 三个类型——它们是后面所有分派函数的「货币」。

再看 epilogue 标签。FT 用空结构体当「标签类型」传给模板，告诉 CUTLASS 这次收尾要不要偏置、要哪种激活：

[epilogue_helpers.h:20-80](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/epilogue_helpers.h#L20-L80) 声明了 `EpilogueOpBiasSilu`/`EpilogueOpBiasReLU`/`EpilogueOpBiasFtGelu`/`EpilogueOpBias`/`EpilogueOpNoBias` 五个空结构体，并为每个标签特化出对应的 CUTLASS `Epilogue::Op` 类型。注意它们都带 `NoBetaScaling`——这正是「把 bias 当作 beta 缩放融进线性变换」的标准 CUTLASS 技巧，省一次单独的加 bias kernel。

#### 4.1.4 代码实践

**实践目标**：建立对 `cutlass_extensions` 目录的宏观印象，能区分「官方 CUTLASS」和「FT 扩展」。

**操作步骤**：

1. 在仓库根目录执行（仅阅读，不运行）列出扩展头文件：

   ```bash
   ls src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/
   ```

2. 对比两类 include：模板代码里 `#include "cutlass/..."`（官方）与 `#include "cutlass_extensions/..."`（FT 扩展）。
   以 [moe_gemm_kernels_template.h:21-31](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L21-L31) 为例，前 5 行 `cutlass/array.h`、`cutlass/gemm/device/gemm_grouped.h` 来自官方，后面 `cutlass_extensions/compute_occupancy.h`、`cutlass_extensions/gemm/kernel/moe_cutlass_kernel.h` 才是 FT 加的。

**需要观察的现象**：FT 的扩展集中在 `gemm/kernel/`、`gemm/warp/`、`epilogue/` 三个子目录，正好对应 CUTLASS 的「kernel 主循环 / warp 级 MMA / epilogue 收尾」三层抽象——FT 是顺着 CUTLASS 的分层去扩展的，而不是另起炉灶。

**预期结果**：你能用一句话说出三个扩展子目录各自补的是什么能力（混合精度反量化 / 分组调度 / 融合缩放收尾）。

#### 4.1.5 小练习与答案

**练习 1**：FT 为什么不直接给 cuBLAS 加功能、而要维护一份 CUTLASS 扩展？

> **参考答案**：cuBLAS 是编译好的闭源黑盒，接口只暴露标准精度组合与有限配置，无法做「混合精度」「分组」「自定义 epilogue」。CUTLASS 是开源模板库，FT 可以在每个零件层（Mma/Epilogue/ProblemVisitor）插入自定义逻辑，所以必须走 CUTLASS 扩展这条路。

**练习 2**：`EpilogueOpBiasFtGelu` 和 `EpilogueOpNoBias` 在功能上的区别是什么？

> **参考答案**：前者在写回结果时融合了「加偏置 + GELU 激活」，用于 MoE 的 fc1（升维 + 激活）；后者既不加偏置也不激活，用于 MoE 的 fc2（降维，偏置留给下游残差 kernel 处理）。

---

### 4.2 MoE GEMM：一个 kernel 跑完全部 expert 的分组矩阵乘

#### 4.2.1 概念说明

先理解 MoE 在算什么。给定一个 token 的隐状态 \(x\)，MoE FFN 的输出是：

\[
y = \sum_{i \in \text{TopK}(g(x))} g_i(x)\cdot \mathrm{FFN}_i(x)
\]

其中 \(g(x)\) 是门控网络对 \(E\) 个专家打的分，TopK 选出得分最高的 \(k\) 个专家，\(g_i(x)\) 是归一化后的权重。\(\mathrm{FFN}_i\) 是第 \(i\) 个专家自己的两层 FFN（升维→激活→降维）。

关键难点在于：**每个专家分到的 token 数不一样，而且是运行时才知道的**。比如 batch 里 32 个 token、每个 token 选 2 个专家、共 8 个专家，可能专家 0 分到 12 行、专家 1 分到 3 行、专家 7 分到 0 行。于是每个专家的 GEMM 的 M 维（行数）各不相同，且每次推理都在变。

这就引出了本讲的核心问题：怎么高效地算 \(E\) 个 M 不同的矩阵乘？

#### 4.2.2 核心流程

FT 的答案是 **grouped GEMM**（也叫 variable-batched GEMM）：把 \(E\) 个矩阵乘描述成 \(E\) 个「problem」，用一个**持久化（persistent）kernel** 一次启动全部线程块，让线程块在 problem 之间动态抢活干。整体流程在 `CutlassMoeFCRunner::run_moe_fc` 里串成：

```
输入：input_activations [num_rows, hidden]，gating_output [num_rows, num_experts]

1. topk_gating_softmax     → 每个 token 选 top-k 个专家，得到 indices/source_rows
2. CubKeyValueSorter       → 按专家编号排序，把「同一专家的 token」排到一起
                             得到 permuted_data + permuted_experts（每个 token 实际归哪个专家）
3. compute_total_rows_before_expert → 对 permuted_experts 做前缀和
                             得到 total_rows_before_expert[E]（每个专家累计处理到第几行）
                             ★ 这是 grouped GEMM 的「问题描述符」
4. moe_gemm_bias_act (fc1) → grouped GEMM：[ΣM_i, hidden] × E 个[hidden, inter] → [ΣM_i, inter] + bias + 激活
5. moe_gemm (fc2)          → grouped GEMM：[ΣM_i, inter]  × E 个[inter, hidden]  → [ΣM_i, hidden]
6. finalize_moe_routing    → 反置换 + 按 g_i 加权求和 + 残差，还原成 [num_rows, hidden]
```

第 3 步是理解 grouped GEMM 的钥匙：`total_rows_before_expert[i]` 是前 \(i\) 个专家处理的**累计行数**，于是第 \(i\) 个专家的 M 维就是 `total_rows_before_expert[i] - total_rows_before_expert[i-1]`。所有专家的输入在显存里**紧凑排列**成一块连续 buffer，每个专家只需知道自己从哪一行开始读。

#### 4.2.3 源码精读

**(a) 问题描述符与前缀和**。`compute_total_rows_before_expert` 用「每个专家一个线程」做一次扫描，写出累计行数：

[moe_kernels.cu:534-538](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/moe_kernels.cu#L534-L538) 是 `compute_total_rows_before_expert_kernel` 的签名，它接收排好序的 `sorted_experts`，输出 `total_rows_before_expert`。这个数组随后被原样传给 grouped GEMM 的 `Arguments`。

**(b) 两次 grouped GEMM 的调用点**。fc1（带偏置和激活）和 fc2（无偏置）分别调用 `MoeGemmRunner` 的两个入口：

[moe_kernels.cu:725-752](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/moe_kernels.cu#L725-L752) 展示了 fc1 用 `moe_gemm_runner_.moe_gemm_bias_act(...)`、fc2 用 `moe_gemm_runner_.moe_gemm(...)`，二者都把 `total_rows_before_expert_` 作为分组描述符传入。注意 fc1/fc2 的 `gemm_n`/`gemm_k` 互换（一个升维、一个降维）。

**(c) Runner 的入口与启发式选优**。`MoeGemmRunner` 是个模板类，两个模板参数分别是激活/计算精度 `T` 和权重精度 `WeightType`（支持 `half`/`bfloat16`/`uint8_t`/`uint4b_t`）：

[moe_gemm_kernels.h:24-52](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels.h#L24-L52) 声明了 `MoeGemmRunner<T, WeightType>` 与它的 `moe_gemm_bias_act` / `moe_gemm` 两个公开方法。

`run_gemm` 内部先枚举候选配置、逐个算占用率，再选最优——这套启发式与 int8 GEMM **共用同一份代码**：

[moe_gemm_kernels_template.h:770-802](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L770-L802) 关键三行：`is_weight_only` 由「T 是否等于 WeightType」推导；调用 `get_candidate_configs(sm_, is_weight_only, only_simt_configs)` 枚举候选；`estimate_best_config_from_occupancies(...)` 选最优。注意 [moe_gemm_kernels_template.h:791-792](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L791-L792) 写死了 `workspace_bytes = 0` 和 `split_k_limit = 1`——**MoE GEMM 不支持 split-k，也不需要 workspace**。

**(d) grouped GEMM 的 CUTLASS 类型组装**。叶子节点 `generic_moe_gemm_kernelLauncher` 把 CUTLASS 的 `DefaultGemmGrouped` 拼成一个 `GemmGrouped`，再用 FT 扩展的 `MoeFCGemm` 包一层：

[moe_gemm_kernels_template.h:120-149](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L120-L149) 先用官方 `DefaultGemmGrouped` 推出标准 `GemmKernel_`，再用 FT 的 `cutlass::gemm::kernel::MoeFCGemm<...>` 替换它的调度部分，最后包成 `GemmGrouped`。这里 `GroupScheduleMode::kDeviceOnly` 很关键——它表示「分组调度完全在 GPU 设备端做，不需要 host 预计算」。

[moe_gemm_kernels_template.h:156-175](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L156-L175) 计算启动块数 `threadblock_count = multi_processor_count * occupancy`（occupancy 上限 2），并构造 `GemmGrouped::Arguments`——注意它只传了一个 `A`、一个 `B`、一个 `C` 指针，外加 `total_rows_before_expert` 和共享的 `gemm_n`/`gemm_k`，**所有 expert 共用同一份连续输入和输出**。

**(e) 持久化 kernel 与 problem visitor**。`MoeFCGemm` 的真正计算是一个「持久化循环」——每个线程块反复问 problem visitor「下一块 tile 在哪个专家的哪个位置」，直到所有 tile 跑完：

[moe_cutlass_kernel.h:388-498](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_cutlass_kernel.h#L388-L498) 是核心循环 `while (problem_visitor.next_tile())`。两个指针计算是理解分组的关键：
- 输入 A 的偏移 `rows_to_jump = last_row_for_problem[problem_idx - 1]`（[moe_cutlass_kernel.h:400-402](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_cutlass_kernel.h#L400-L402)）——跳到当前专家的输入行起点；
- 权重 B 的偏移 `byte_ptr_B + problem_idx * bytes_per_expert_matrix`（[moe_cutlass_kernel.h:405-406](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_cutlass_kernel.h#L405-L406)）——选当前专家的权重矩阵。

[moe_cutlass_kernel.h:65-99](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_cutlass_kernel.h#L65-L99) 用 SFINAE 提供 `run_mma` 的两个重载：当 `WeightType` 是 `uint8_t`/`uint4b_t` 时走「带 scale 迭代器」的反量化分支，否则走普通 GEMM 分支。这正是「同一份 MoE kernel 既能量化也能纯 FP16」的统一机制。

problem visitor 负责把「全局 tile 序号」翻译成「(专家, 专家内 tile)」。它在设备端用 warp 内前缀和扫描 `last_row_for_problem`，从而动态地把 tile 分配给专家：

[moe_problem_visitor.h:162-177](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_problem_visitor.h#L162-L177) 的 `problem_size(idx)` 用累计行数差算出第 `idx` 个专家的 M 维——这就是「每个专家 M 不同」的体现。

[moe_problem_visitor.h:242-321](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_problem_visitor.h#L242-L321) 的 `next_tile()` 是 `kDeviceOnly` 调度的核心：用 `__shfl_sync`/`__shfl_up_sync` 在 warp 内做前缀和，定位当前 tile 属于哪个专家。**因为调度完全在设备端，host 不需要事先知道每个专家分到多少行**——这正是 MoE「运行时才知道分布」所需要的。

**(f) 编译期守卫**。整条 MoE GEMM 路径被 `BUILD_CUTLASS_MOE` 宏包起来，关掉时直接抛错：

[moe_gemm_kernels_template.h:68-72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L68-L72) 是 `#ifdef BUILD_CUTLASS_MOE` 的入口；[moe_gemm_kernels_template.h:199-202](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L199-L202) 是 `#else` 分支，提示用户重新编译时加 `-DBUILD_CUTLASS_MOE=ON`。

#### 4.2.4 代码实践

**实践目标**：回答本讲的核心问题——MoE 场景下「多个 expert 的 GEMM」为何需要专门的 grouped MoE GEMM kernel，而非逐 expert 调用 cuBLAS。

**操作步骤（源码阅读型）**：

1. 阅读 [moe_gemm_kernels_template.h:156-175](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L156-L175)，确认 grouped GEMM **只启动一次 kernel**，启动块数 = `SM 数 × occupancy`，与 expert 数量无关。
2. 阅读 [moe_problem_visitor.h:162-177](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_problem_visitor.h#L162-L177)，确认每个 expert 的 M 维由运行时的累计行数决定。
3. 做一个对比推演：假设 8 个专家、`threadblock_count = 80`、专家 0 有 60 行、专家 1~7 各只有几行。
   - **逐 expert 调 cuBLAS**：要发起 8 次 kernel 启动；处理小专家时，GEMM 的 tile 数远少于 80，大量 SM 空闲；且 8 次启动有 8 份 host↔device 开销。
   - **grouped MoE kernel**：只启动 1 次，80 个线程块由 problem visitor **跨专家负载均衡**地瓜分全部 tile，小专家的 tile 和大专家的 tile 混在一起抢，没有 SM 空闲。

**需要观察的现象 / 预期结论**：
- grouped kernel 的好处有两点：①把 \(E\) 次 kernel 启动合并成 1 次；②在专家负载不均（很常见）时避免小专家导致 SM 空闲。这正是「专门 kernel」相对「逐 expert cuBLAS」的根本优势。
- 进一步验证：`moe_gemm` 不支持 split-k（[moe_gemm_kernels_template.h:70-72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L70-L72)），因为分组调度本身已经把 tile 摊平到了所有 SM 上，再 split-k 反而要全局归约，得不偿失。

**可选运行型实践（需要 GPU + MoE 权重）**：参照 [docs/gpt_guide.md:847-882](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L847-L882) 的 `GPT with MOE` 章节，用 modelscope 的 `nlp_gpt3_text-generation_0.35B_MoE-64` 检查点跑一次推理。若无检查点/GPU，则跳过运行，仅完成上面的源码阅读推演（标注「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：`total_rows_before_expert` 这个数组解决的是什么问题？

> **参考答案**：它是一个长度为专家数的前缀和数组，第 `i` 项等于前 `i` 个专家累计处理的行数。grouped kernel 用 `total_rows_before_expert[i] - total_rows_before_expert[i-1]` 得到第 `i` 个专家的 M 维，用 `total_rows_before_expert[i-1]` 得到它的输入行起点。这样所有专家的输入/输出可以紧凑排成一块连续显存，无需为每个专家单独维护指针数组。

**练习 2**：为什么 `moe_gemm` 不支持 split-k，而普通 cuBLAS GEMM 经常用 split-k？

> **参考答案**：split-k 是把单个 GEMM 沿 K 维切开、用多个线程块并行算部分和再归约，适合「M/N 太小、tile 数撑不满 SM」的单个 GEMM。但 grouped MoE kernel 已经通过 problem visitor 把多个专家的 tile 摊到了所有 SM 上，本来就不会因为单专家 M 小而空闲；再叠 split-k 反而引入跨块归约开销，所以禁用。

**练习 3**：fc1 调 `moe_gemm_bias_act`、fc2 调 `moe_gemm`，为什么 fc2 没有「act」？

> **参考答案**：MoE 的 fc2 是降维投影（inter→hidden），其后还要做按门控权重加权求和并与残差相加，激活只发生在 fc1 之后。fc2 的偏置/残差由后续的 `finalize_moe_routing` 统一处理，所以 fc2 只需纯 GEMM，对应 `EpilogueOpNoBias`。

---

### 4.3 int8 GEMM：s8×s8→T 的 w8a8 路径

#### 4.3.1 概念说明

`int8_gemm` 是另一条 CUTLASS 路径，对应 u9-l1 讲过的 **w8a8（int8_mode=2/3）**：激活和权重**都是 int8**，在 tensor core 上做 int8×int8、用 **int32 累加**，最后按 per-column 或 per-column×per-row 的 float 缩放因子（alpha）还原成 FP16/BF16/FP32/INT32 输出。与 u9-l2 的 weight-only（激活 FP16 × 权重 INT8）不同，这里激活也量化了，整条计算链路几乎全 int8，性能更高但对量化校准更敏感。

它在结构上和 MoE GEMM 形成「对照与复用」的关系：

| 维度 | MoE GEMM | int8 GEMM |
| --- | --- | --- |
| 输入类型 | T × WeightType（T 可 FP16） | int8 × int8 |
| 累加类型 | 与 arch 相关 | 固定 int32 |
| 分组 | 是（problem_count = 专家数） | 否（`num_experts = 1`） |
| 缩放融合 | weight-only 时在 warp 反量化 | epilogue 里 per-row/col alpha |
| 守卫宏 | `BUILD_CUTLASS_MOE` | `BUILD_CUTLASS_MIXED_GEMM` |

#### 4.3.2 核心流程

```
CutlassInt8GemmRunner::gemm(A int8, B int8, alpha_col, alpha_row, C T)
   └─ run_gemm
        ├─ get_candidate_configs(sm_, is_weight_only=false, false)   ← 与 MoE 共用
        ├─ 逐个候选算 occupancy                                       ← 与 MoE 共用
        ├─ estimate_best_config_from_occupancies(..., num_experts=1) ← 与 MoE 共用
        └─ dispatch_to_arch (仅 sm80) → generic_int8_gemm_kernelLauncher
             └─ 组装 CUTLASS：DefaultGemm(s8,s8→T, int32 acc)
                  + EpilogueVisitorPerRowPerCol（融合 alpha 缩放）
                  + GemmWithEpilogueVisitor → GemmUniversalBase.run()
```

最值得注意的细节是 [int8_gemm_template.h:538-539](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm_template.h#L538-L539) 的注释与代码：调用启发式时 `num_experts = 1`，并写明「**We use the same function for MoE and regular FFN**」——这条 int8 GEMM 与 MoE GEMM 共用同一份 `estimate_best_config_from_occupancies`，只是把分组数设成 1。

#### 4.3.3 源码精读

**(a) Runner 接口**。`CutlassInt8GemmRunner<T>` 用 `QuantMode` 表达缩放方式（仅 per-col，或 per-col×per-row），用 `alpha_col`/`alpha_row` 传入缩放因子：

[int8_gemm.h:28-55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm.h#L28-L55) 顶部注释说明「int8 输入、float alpha 缩放、T 输出」，并声明 `gemm` 方法。`split_k_limit` 固定为 7（[int8_gemm.h:90](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm.h#L90)），与 MoE 的 `split_k_limit=1` 形成对比——单组 GEMM 允许 split-k 来填补 SM。

**(b) CUTLASS 类型组装**。叶子节点强制 `only TN is supported (s8 * s8 + s32)`：

[int8_gemm_template.h:61-108](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm_template.h#L61-L108) 在 `#ifdef BUILD_CUTLASS_MIXED_GEMM` 守卫下，用官方 `DefaultGemm` 组装「int8 行主序 A × int8 列主序 B → T 输出，int32 累加」。L89 的注释点明只支持 TN 布局；L76 固定 `ElementAccumulator = int32_t`。

[int8_gemm_template.h:110-142](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm_template.h#L110-L142) 用 FT 扩展的 `EpilogueVisitorPerRowPerCol` 把 alpha 缩放融进收尾，再包成 `GemmWithEpilogueVisitor` + `GemmUniversalBase`。这是 FT 扩展「可定制 epilogue」能力的典型用例。

**(c) 架构分派与守卫**。`dispatch_to_arch` 目前只支持 sm80（Ampere），sm70/75 被注释掉：

[int8_gemm_template.h:481-500](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm_template.h#L481-L500) 表明 int8 tensor core GEMM 当前只在 Ampere 上启用。`#else` 分支 [int8_gemm_template.h:189-192](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm_template.h#L189-L192) 提示加 `-DBUILD_CUTLASS_MIXED_GEMM=ON`。

**(d) 与 MoE 共用的启发式**：

[cutlass_heuristic.h:24-35](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.h#L24-L35) 声明了 `get_candidate_configs` 与 `estimate_best_config_from_occupancies`——这两个函数同时被 MoE GEMM、int8 GEMM、fpA_intB GEMM 调用，是 CUTLASS 三条路径的公共调优入口。

#### 4.3.4 代码实践

**实践目标**：体会 int8 GEMM 与 MoE GEMM 的「同骨架、不同参数」关系。

**操作步骤（源码阅读型）**：

1. 对比 [moe_gemm_kernels_template.h:770-802](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L770-L802)（MoE 的 `run_gemm`）与 [int8_gemm_template.h:518-552](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm_template.h#L518-L552)（int8 的 `run_gemm`），两者的步骤几乎一模一样：枚举候选 → 算 occupancy → 选最优 → 再跑一次。
2. 找出唯一的两处显著差异：① `is_weight_only` 取值（MoE 取决于 T 与 WeightType 是否相等；int8 固定 `false`）；②传给 `estimate_best_config_from_occupancies` 的 `num_experts`（MoE 是真实专家数；int8 是 1）。

**需要观察的现象 / 预期结论**：FT 把「选最优 CUTLASS 配置」这件事抽象成了与具体 GEMM 种类无关的通用流程，MoE 只是「多组」、int8 只是「单组」，二者复用同一份调优代码。

**预期结果**：你能解释为什么 int8 GEMM 的 `split_k_limit=7`（单组 GEMM 需要用 split-k 填满 SM），而 MoE GEMM 是 `split_k_limit=1`（分组已摊平，禁用 split-k）。

#### 4.3.5 小练习与答案

**练习 1**：int8 GEMM 的累加类型为什么必须是 int32，而不能是 int8？

> **参考答案**：两个 int8 相乘最大 127×127≈1.6 万，再沿 K 维（通常几百上千）累加会远超 int8 甚至 int16 的表示范围。CUTLASS 的 int8 tensor core 指令（IMMA）原生就是「int8 输入、int32 累加」，int32 累加器保证中间结果不溢出，最后再由 epilogue 用 alpha 缩放回低精度输出。

**练习 2**：int8 GEMM 为什么只在 sm80（Ampere）启用，而 MoE GEMM 支持 sm70/75/80？

> **参考答案**：int8 tensor core 矩阵乘（s8×s8→s32 的 IMMA）需要 Ampere 及以上的 tensor core 指令支持，且当前实现尚未为 sm70/75 实例化（代码里这两段被注释掉了，标注 TODO）。MoE GEMM 本质是 FP16/FP32 的分组 GEMM，Volta/Turing 的 tensor core 即可运行，所以支持更广。

---

### 4.4 编译开关：BUILD_CUTLASS_MOE / BUILD_CUTLASS_MIXED_GEMM

#### 4.4.1 概念说明

两条 CUTLASS 路径各自有一个 CMake 开关，**默认都是 ON**：

- `BUILD_CUTLASS_MOE` → 注入宏 `BUILD_CUTLASS_MOE` → 守卫 MoE GEMM 模板（4.2 节）。
- `BUILD_CUTLASS_MIXED_GEMM` → 注入宏 `BUILD_CUTLASS_MIXED_GEMM` → 守卫 int8 GEMM 模板（4.3 节）**以及** u9-l2 的 fpA_intB（weight-only）GEMM 模板。

这两个开关的英文描述里都有一句相同的话：「**requires CUTLASS. Increases compilation time**」。这是本节要解释的核心代价。

#### 4.4.2 核心流程

```
CMakeLists.txt: option(BUILD_CUTLASS_MOE / MIXED_GEMM ON)
   └─ add_definitions(-DBUILD_CUTLASS_MOE / -DBUILD_CUTLASS_MIXED_GEMM)
        └─ 模板代码里 #ifdef 守卫
             └─ 编译期：为每个 (T, WeightType, arch, TileShape, WarpShape, Stages) 组合实例化一份 CUTLASS kernel
                  → 翻译单元数 = 类型组合数 × 分块组合数 × 架构数
```

#### 4.4.3 源码精读

**编译开关定义**：

[CMakeLists.txt:35-45](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L35-L45) 定义了两个 option，默认都是 `ON`，并各自 `add_definitions`。两条 `message(STATUS ...)` 都明确写出「Increases compilation time」。

**模板内的守卫**：MoE 在 [moe_gemm_kernels_template.h:68](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L68) 守卫；int8 在 [int8_gemm_template.h:61](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/int8_gemm/int8_gemm_template.h#L61) 守卫。关掉时分别抛出要求重新加 `-D...=ON` 的错误。

**控制实例化爆炸的工程手段**：FT 把每种类型组合拆成独立的 `.cu` 文件，每个文件只显式实例化一个 `MoeGemmRunner` 特化，避免在一个翻译单元里实例化所有组合。例如：

[moe_gemm_kernels_fp16_uint8.cu:17-21](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_fp16_uint8.cu#L17-L21) 整个文件只有一行实质代码 `template class MoeGemmRunner<half, uint8_t>;`。moe_gemm 目录下共有 7 个这样的实例文件（`fp32_fp32`、`fp16_fp16`、`fp16_uint8`、`fp16_uint4`、`bf16_bf16`、`bf16_uint8`、`bf16_uint4`），int8_gemm 目录下有 4 个（`int32`/`fp16`/`fp32`/`bf16` 输出）。

这些实例文件被聚合成静态库：

[cutlass_kernels/CMakeLists.txt:21-43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/CMakeLists.txt#L21-L43) 用 `file(GLOB ...)` 把 `moe_gemm/*.cu`、`fpA_intB_gemm/*.cu`、`int8_gemm/*.cu` 各自编成静态库（`moe_gemm_kernels`、`fpA_intB_gemm`、`int8_gemm`），都依赖 `cutlass_heuristic`。拆成独立静态库也是为了让实例化并行编译、彼此隔离。

#### 4.4.4 代码实践

**实践目标**：理解 `BUILD_CUTLASS_MOE` 开启带来的「编译时间」代价，并学会在不需要时关掉它来加速编译。

**操作步骤（源码阅读 + 编译实验型）**：

1. 数一下 MoE GEMM 要实例化的 kernel 数量级：架构 3 档（sm70/75/80）× 候选 tile 配置约 3~4 种 × stages 2~3 档 × 7 个类型组合。即便 FT 已经用「一个 `.cu` 一个类型」把它拆开，每个 `.cu` 仍要在多种 tile/stages 上实例化沉重的 CUTLASS 模板。
2. 阅读分派树 [moe_gemm_kernels_template.h:335-405](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L335-L405)（`dispatch_gemm_config` 按 stages 分派）和 [moe_gemm_kernels_template.h:433-505](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L433-L505)（按 tile 配置分派），体会「每多一种配置就多一份编译期实例化」。
3. （可选，需本地编译环境）对比两次编译耗时：
   - `cmake .. -DBUILD_CUTLASS_MOE=ON -DSM=80` 
   - `cmake .. -DBUILD_CUTLASS_MOE=OFF -DSM=80`
   观察后者跳过整个 `moe_gemm_kernels` 静态库的编译，整体编译时间明显下降。**待本地验证**（取决于机器核数与 CUDA 版本）。

**需要观察的现象 / 预期结论**：
- CUTLASS 是高度模板化的「头文件库」，所有 kernel 在编译期实例化，单份实例化就可能耗时数秒到数十秒。MoE/int8 路径要为「多架构 × 多类型 × 多分块 × 多 stages」组合实例化，是 FT 编译时间的大头之一。
- 因此两个开关默认 ON 是为了「开箱即用」，但若你的目标架构固定、且不用 MoE/int8，关掉对应开关能显著缩短编译时间。

#### 4.4.5 小练习与答案

**练习 1**：为什么 FT 把每个 `MoeGemmRunner<T, WeightType>` 特化放到单独的 `.cu` 文件里？

> **参考答案**：CUTLASS 的模板实例化非常重。如果在一个翻译单元里同时实例化多个类型组合，编译会极慢甚至因内存爆炸失败。拆成「一个 `.cu` 一个特化」可以让每个翻译单元又小又独立，并行编译、彼此隔离，并把不需要的特化（如不用 BF16）通过不编译对应文件来跳过。

**练习 2**：关掉 `BUILD_CUTLASS_MOE` 后，运行时调用 MoE GEMM 会发生什么？

> **参考答案**：模板代码走到 `#else` 分支，抛出 `runtime_error`，提示「FasterTransformer was built with MoE support off. Please rebuild with cmake option -DBUILD_CUTLASS_MOE=ON」（见 [moe_gemm_kernels_template.h:199-202](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/moe_gemm/moe_gemm_kernels_template.h#L199-L202)）。即编译期不生成 kernel，运行期无法执行。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「MoE FFN 一次前向的数据流追踪」任务：

**任务**：假设一个 MoE FFN，`num_rows=4`、`hidden=8`、`inter=16`、`num_experts=4`、`top-k=2`。请按顺序画出/写出以下每一阶段的关键量，并标注它由哪个源码函数产生、最终喂给哪个 CUTLASS kernel：

1. 门控 + top-k 后，每个 token 选了哪些专家（自行假设一组合理的分配）。
2. 按专家排序后，`permuted_experts` 长什么样；由此推算 `total_rows_before_expert` 数组。
3. 写出 fc1 这次 grouped GEMM 的 `problem_count`、每个 problem 的 (M, N, K)，以及它传给 `MoeFCGemm::Arguments` 的 `total_rows_before_expert`、`gemm_n`、`gemm_k`。
4. 指出 `MoeFCGemm` 持久化 kernel 里，某个线程块在 `problem_visitor.next_tile()` 返回「专家 2 的第 1 块 tile」时，它的 `ptr_A`、`ptr_B` 分别怎么由 `rows_to_jump` 和 `problem_idx * bytes_per_expert_matrix` 算出来（引用 [moe_cutlass_kernel.h:400-406](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/moe_cutlass_kernel.h#L400-L406)）。
5. 说明这次推理「为什么不能简单地写成 4 次普通 cuBLAS GEMM」——结合启动开销与负载不均两点。

**验收标准**：你能画出 `total_rows_before_expert` 这条「分组描述符」如何一路从 `compute_total_rows_before_expert_kernel` 传到 `MoeFCGemm` 的 problem visitor，并在持久化循环里决定每个 tile 读哪段输入、哪份权重。

---

## 6. 本讲小结

- `cutlass_extensions` 是 FT 对官方 CUTLASS 的扩展，补齐了 cuBLAS 做不到的三件事：混合精度反量化 GEMM（weight-only）、分组持久化 GEMM（MoE）、可定制融合 epilogue（int8 缩放）。
- **MoE GEMM 的本质是 grouped GEMM**：用 `total_rows_before_expert` 前缀和数组描述「每个 expert 不同 M 维」，用一个持久化 kernel 一次启动、由 `MoeProblemVisitor` 在设备端把 tile 跨专家负载均衡地分给所有线程块。
- MoE 必须用专门 grouped kernel 而非逐 expert cuBLAS：①把 \(E\) 次启动合并为 1 次；②专家负载不均时避免小专家导致 SM 空闲。代价是不支持 split-k。
- **int8 GEMM（s8×s8→T，int32 累加）**是 w8a8 路径，用 FT 扩展的 `EpilogueVisitorPerRowPerCol` 融合 alpha 缩放，目前仅 sm80、仅 TN 布局；它与 MoE GEMM **共用同一份 `get_candidate_configs` / `estimate_best_config_from_occupancies` 启发式**，差别只是 `num_experts=1`。
- `BUILD_CUTLASS_MOE` 与 `BUILD_CUTLASS_MIXED_GEMM`（默认都 ON）分别守卫两条路径；因 CUTLASS 模板实例化极重，二者都以「显著增加编译时间」为代价，并通过「一个 `.cu` 实例化一个类型特化」来控制实例化爆炸。

---

## 7. 下一步学习建议

- 若想看 weight-only 量化那条「FP16 激活 × INT8/INT4 权重」的混合精度 GEMM 是怎么在 warp 内反量化的，去读 u9-l2 对应的 `kernels/cutlass_kernels/fpA_intB_gemm/` 与 `cutlass_extensions/gemm/kernel/fpA_intB_gemm.h`、`gemm/warp/mma_tensorop_dequantizer.h`——它与本讲的 MoE GEMM 共用同一套分派骨架。
- 若想看 FT 如何把 MoE FFN 接进真实模型，建议阅读 `src/fastertransformer/models/multi_gpu_gpt/` 下 `ParallelGpt` 里对 `CutlassMoeFCRunner` 的调用（承接 u6-l1 的 ParallelGpt 架构），以及 `docs/gpt_guide.md` 的 `GPT with MOE` 章节给出的端到端运行示例。
- 若对 CUTLASS 的模板抽象（Mma/Epilogue/ThreadblockSwizzle/ProblemVisitor）想建立更系统的理解，建议结合官方 CUTLASS 文档阅读本讲引用的 `MoeFCGemm` 与 `MoeProblemVisitor`——FT 的扩展是「官方 CUTLASS 同款抽象 + MoE 调度」的范本。
