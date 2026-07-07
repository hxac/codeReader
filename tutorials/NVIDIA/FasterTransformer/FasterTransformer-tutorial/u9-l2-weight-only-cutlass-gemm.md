# 权重仅量化与 CUTLASS 混合 GEMM

## 1. 本讲目标

在 [u9-l1](u9-l1-int8-quantization.md) 中我们认识了 INT8 量化推理的两条路线：weight-only（仅权重量化）与 w8a8（权重激活都量化，SmoothQuant 走这条路）。本讲专门深入 **weight-only 量化**这条路线在 FasterTransformer（下称 FT）里是如何用 CUTLASS 实现的。

学完本讲，你应当能够：

- 说清楚 weight-only 量化（fpA_intB）为什么对「小 batch、大权重矩阵」的 GPT 推理收益最大；
- 读懂 `CutlassFpAIntBGemmRunner` 这条从高层 API 到底层 CUTLASS kernel 的层层分派（dispatch）链路；
- 解释 `cutlass_preprocessors` 在**离线**阶段对权重做了哪四步重排与改写，以及为什么要这么做才能喂给 tensor core；
- 理解 `cutlass_heuristic` 如何在**运行期**根据 GEMM 形状与「占用率（occupancy）」挑选最优 CUTLASS tile 配置，并且这与 cuBLAS 的离线调优是两套机制；
- 区分 `cutlass_preprocessors` 里的 CPU 逐通道量化与 `calibrate_quantize_weight_kernels.cu` 里的 GPU 逐通道量化两条路径。

本讲是 [u9-l1](u9-l1-int8-quantization.md)（INT8/SmoothQuant）的直接延续，并承接 [u2-l3](u2-l3-cublas-gemm.md)（cuBLAS GEMM）与 [u2-l4](u2-l4-gemm-autotuning.md)（离线 GEMM 调优）的概念。

## 2. 前置知识

### 2.1 weight-only 量化的直觉

普通 GEMM 里，矩阵 \(A\) 是激活、\(B\) 是权重，二者类型相同（比如都是 FP16）。在自回归生成（GPT 解码）时：

- \(A\) 的行数 \(M\) = 当前 batch 内的 token 数。在 decoder 阶段，每步只生成一个 token，\(M\) 很小（常常就是 `batch × beam`，比如 1～32）；
- \(B\) 的规模 \(K \times N\)（hidden × inter，或 inter × hidden）由模型决定，对大模型而言非常大。

也就是说，**这种场景下访存瓶颈在权重 \(B\) 上，计算量却不大**。既然 \(A\) 已经很小，没必要费力把激活也压成低精度（反而引入误差与反量化开销）；而把 \(B\) 从 FP16 压成 INT8/INT4，权重显存带宽直接减半甚至降到 1/4，能在小 batch 下换来明显加速。这就是 **weight-only**：**权重 \(B\) 存为低精度（intB），激活 \(A\) 仍是浮点（fpA）**，所以这条路径在 FT 里命名为 **fpA_intB**。

> 注意 weight-only 的前提是激活用浮点。`docs/gpt_guide.md` 明确写道："Weight only PTQ only works for FP16/BF16 compute"（[docs/gpt_guide.md:64-72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L64-L72)）。

### 2.2 为什么要专门的混合精度 GEMM

把 \(B\) 压成 INT8 后，标准 cuBLAS 不能直接算 `FP16 × INT8`——它要求两边类型一致。所以必须用一套支持「输入两边类型不同、在寄存器里把 INT8 反量化成 FP16 再乘累加」的 GEMM。FT 选用 NVIDIA 的 CUTLASS 库，并对其做了扩展（`cutlass_extensions`），实现了 `OpMultiplyAddDequantizeInterleavedBToA` 这类「加载后反量化」的乘加算子。

### 2.3 tensor core 对数据布局的苛求

tensor core 通过 `ldmatrix`（LDSM）指令从共享内存加载片段，它对 INT8/INT4 数据在显存里的**排列方式**有非常具体的要求（哪些行相邻、哪些列交错、是否带偏移）。普通的行主序 `[K, N]` INT8 矩阵**不能**直接喂给 tensor core，必须先经过一套「预处理」重排成特定布局。这是本讲第二个核心模块 `cutlass_preprocessors` 存在的根本原因。

### 2.4 两条关键对比

| 维度 | cuBLAS（[u2-l3](u2-l3-cublas-gemm.md)） | CUTLASS fpA_intB（本讲） |
| --- | --- | --- |
| 输入类型 | 两边相同（FP16/FP32/INT8） | 两边不同（FP16 × INT8/INT4） |
| kernel 选择 | 运行期查 `cublasAlgoMap`（[u2-l4](u2-l4-gemm-autotuning.md) 离线调优表） | 运行期用 `cutlass_heuristic` 按占用率估算 |
| 权重预处理 | 无（直接用原始权重） | 必须**离线**重排（`cutlass_preprocessors`） |
| 适用场景 | 通用、各种 batch | 小 batch、大权重 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm.h) | `CutlassFpAIntBGemmRunner` 模板类的接口声明 |
| [src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h) | 从 `gemm()`/`gemm_bias_act()` 到 CUTLASS kernel 启动的整条分派链 |
| [src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_fp16_int4.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_fp16_int4.cu) 等 | 对每种 `(激活类型, 权重类型)` 组合做一次显式模板实例化 |
| [src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc) | 运行期按形状、占用率挑选最优 CUTLASS tile 配置 |
| [src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc) | 离线权重预处理（行重排、转置、列交错、加偏移）与 CPU 端对称量化 |
| [src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu) | GPU 端逐通道权重校准/量化 kernel（INT8 路径） |
| [src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/mixed_gemm_B_layout.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/mixed_gemm_B_layout.h) | 定义权重 \(B\) 在各架构/量化类型下的目标布局 |
| [src/fastertransformer/layers/FfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc) | `int8_mode==1` 时 FFN 层如何调用 weight-only runner（真实调用点） |
| [src/fastertransformer/th_op/common/WeightOnlyQuantOps.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/common/WeightOnlyQuantOps.cc) | 把预处理/量化函数暴露成 PyTorch 自定义 op 的桥接层 |
| [tests/gemm_dequantize/th_gemm_dequantize.py](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/gemm_dequantize/th_gemm_dequantize.py) | weight-only GEMM 的正确性验证测试 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1** fpA_intB 混合精度 GEMM 的分派链（`CutlassFpAIntBGemmRunner`）
2. **4.2** 离线权重预处理（`cutlass_preprocessors`）
3. **4.3** 运行期 kernel 选择（`cutlass_heuristic`）
4. **4.4** GPU 端逐通道权重校准量化（`calibrate_quantize_weight_kernels`）

---

### 4.1 fpA_intB 混合精度 GEMM 的分派链

#### 4.1.1 概念说明

`CutlassFpAIntBGemmRunner<T, WeightType>` 是 weight-only GEMM 对外的统一入口。它只支持：

- 激活类型 `T ∈ {half, __nv_bfloat16}`（FP32 会被特化成「永远报错」的空壳，见下文）；
- 权重类型 `WeightType ∈ {int8_t (即 uint8_t), cutlass::uint4b_t}`。

这一点写在头文件的类注释里（[fpA_intB_gemm.h:26-36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm.h#L26-L36)），并强调：**激活、偏置、scale、输出都按行主序，但权重 \(B\) 必须是经过 `cutlass_preprocessors` 预处理的特殊布局**。

它对外只暴露两个计算接口：

- `gemm(A, B, weight_scales, C, m, n, k, workspace, ...)`：纯 GEMM，不带偏置和激活；
- `gemm_bias_act(A, B, weight_scales, biases, C, m, n, k, activation_type, workspace, ...)`：GEMM + 偏置 + 激活融合在 CUTLASS epilogue 里。

注意第三个参数 `weight_scales`：因为权重是 INT8/INT4，反量化时需要一个浮点 scale（每列一个），这个 scale 与权重一起在离线阶段算好。

#### 4.1.2 核心流程

调用从最高层一直分派到 CUTLASS kernel，是一条 **7 层的模板分派链**。理解这条链是理解整个 weight-only 推理的关键：

```
gemm_bias_act(activation_type)        // ① 按"激活类型"选 EpilogueTag
   └─ run_gemm<EpilogueTag>()         // ② 枚举候选配置、算占用率、选最优
        ├─ get_candidate_configs()    //     (来自 cutlass_heuristic)
        ├─ dispatch_to_arch(occupancy=&) //  逐候选只算 occupancy、不真正启动
        └─ dispatch_to_arch(chosen)   //     用选中配置真正启动
             └─ dispatch_gemm_to_cutlass(tile_config) // ③ 按 tile 形状选 GemmShape
                  └─ dispatch_gemm_config(stages)     // ④ 按 pipeline stages 选 Stages
                       └─ dispatch_stages::dispatch() // ⑤ 模板特化(Sm80 才允许 stages>2)
                            └─ generic_mixed_gemm_kernelLauncher<arch, EpilogueTag, TBShape, WarpShape, Stages>()
                                 └─ 构造 cutlass GemmKernel 并 initialize()+run()  // ⑥⑦
```

这条链的本质是：把「激活类型、GPU 架构、tile 形状、流水线级数」四个**编译期**维度，逐层用 `switch`/模板特化拆开，最终拼出一个具体的 CUTLASS `GemmKernel` 类型。CUTLASS 是重度模板库，每一种 `(形状, 架构, 精度, stages)` 组合都是一个独立的 C++ 类型，必须在编译期全部实例化出来——这就是为什么开启 `BUILD_CUTLASS_MIXED_GEMM` 会显著拖慢编译。

#### 4.1.3 源码精读

**① 入口与激活分发**——`gemm_bias_act` 用 `switch` 把激活类型映射成 epilogue 标签（[fpA_intB_gemm_template.h:510-554](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L510-L554)）：

```cpp
case ActivationType::Relu:  run_gemm<EpilogueOpBiasReLU>(...);    break;
case ActivationType::Gelu:  run_gemm<EpilogueOpBiasFtGelu>(...);  break;
case ActivationType::Silu:  run_gemm<EpilogueOpBiasSilu>(...);    break;
case ActivationType::Identity: run_gemm<EpilogueOpBias>(...);     break;
```

`gemm`（不带激活）则统一走 `EpilogueOpNoBias`（[fpA_intB_gemm_template.h:556-570](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L556-L570)）。这里把「加偏置 + 激活」融进了 CUTLASS 的 epilogue，避免单独再发一个 elementwise kernel。

**② 候选枚举 + 占用率估算 + 选最优**——`run_gemm`（[fpA_intB_gemm_template.h:459-508](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L459-L508)）是这条链的「大脑」：

```cpp
static constexpr bool is_weight_only = !std::is_same<T, WeightType>::value;
std::vector<CutlassGemmConfig> candidate_configs = get_candidate_configs(sm_, is_weight_only, false);
std::vector<int>               occupancies(candidate_configs.size());
for (size_t ii = 0; ii < candidate_configs.size(); ++ii) {
    dispatch_to_arch<EpilogueTag>(..., candidate_configs[ii], ..., &occupancies[ii]); // 只算占用率
}
CutlassGemmConfig chosen_config = estimate_best_config_from_occupancies(...);
dispatch_to_arch<EpilogueTag>(..., chosen_config, ..., stream);  // 真正启动
```

> 关键技巧：`dispatch_to_arch` 在传入 `occupancy != nullptr` 时，会走到 `generic_mixed_gemm_kernelLauncher` 的开头，只调用 `compute_occupancy_for_kernel<GemmKernel>()` 然后 `return`，**不真正启动 kernel**（[fpA_intB_gemm_template.h:134-137](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L134-L137)）。所以 `run_gemm` 实际上「先对每个候选做一次廉价的理论占用率探测，再选最好的真正跑」。

`get_candidate_configs` 与 `estimate_best_config_from_occupancies` 来自 4.3 节的 `cutlass_heuristic`。

**③ 架构分发**——`dispatch_to_arch` 按 `sm_`（运行期读取的架构版本）选 `cutlass::arch::Sm70/Sm75/Sm80`（[fpA_intB_gemm_template.h:424-457](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L424-L457)）。注意 Sm90（Hopper）在这里会**抛错**——fpA_intB 只支持到 Ampere，Hopper 上请走 FP8（[u9-l3](u9-l3-fp8-inference.md)）。

**④ tile 形状分发**——`dispatch_gemm_to_cutlass` 用 `switch(tile_config)` 把 3 种 weight-only tile 各自映射成 `cutlass::gemm::GemmShape<M,N,K>`（[fpA_intB_gemm_template.h:345-406](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L345-L406)）。注释点明一个经验：weight-only 只实例化 `threadblockShapeM == warpShapeM` 的配置，因为这类混合 GEMM 这样最快。

**⑤⑥⑦ 真正构造 CUTLASS kernel**——`generic_mixed_gemm_kernelLauncher`（[fpA_intB_gemm_template.h:38-197](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L38-L197)）把所有编译期类型拼成一个 `GemmKernel`，组装 `Arguments`，最后 `gemm.initialize(args, workspace, stream)` + `gemm.run(stream)`。它受 `#ifdef BUILD_CUTLASS_MIXED_GEMM` 守卫，编译时关闭该选项则直接抛错（[fpA_intB_gemm_template.h:60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L60), [:193-196](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L193-L196)）。

**FP32 的空壳特化**——为了让模板在 `T=float` 时也能通过编译（FT 别处需要这个模板存在），头文件给了一个对 `CutlassFpAIntBGemmRunner<float, WeightType>` 的特化，其 `gemm`/`gemm_bias_act` 函数体只抛错（[fpA_intB_gemm.h:110-141](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm.h#L110-L141)，实现见 [fpA_intB_gemm_template.h:583-623](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L583-L623)）。它对应 docs 里"Weight only PTQ only works for FP16/BF16"这条限制。

**显式实例化**——为了避免在头文件里暴露全部 CUTLASS 模板（会污染整个工程的编译时间），实现被放在 `.cu` 文件里，每种组合一个文件做一次 `template class CutlassFpAIntBGemmRunner<half, cutlass::uint4b_t>;`（[fpA_intB_gemm_fp16_int4.cu:19-21](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_fp16_int4.cu#L19-L21)），分别是 `fp16_int8`、`fp16_int4`、`bf16_uint8`、`bf16_uint4` 四个文件。

#### 4.1.4 代码实践

**实践目标**：把 4.1.2 的分派链在源码里逐层「点」一遍，确认每一层的开关维度。

**操作步骤**：

1. 打开 [fpA_intB_gemm_template.h:510](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L510)，从 `gemm_bias_act` 开始。
2. 跟进 `run_gemm`（[:459](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L459)），记下它两次调用 `dispatch_to_arch`：第一次带 `&occupancies[ii]`（只探测），第二次带 `chosen_config`（真跑）。
3. 跟进 `dispatch_to_arch`（[:424](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L424)）、`dispatch_gemm_to_cutlass`（[:345](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L345)）、`dispatch_gemm_config`（[:300](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L300)）、`dispatch_stages`（[:199](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L199)）、`generic_mixed_gemm_kernelLauncher`（[:38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L38)）。
4. 在一张纸上为每一层标注「它用哪个量做 switch/特化」。

**需要观察的现象**：每一层的判定量分别是 `activation_type`、`sm_`、`tile_config`、`stages`、（架构模板参数）——五个维度层层细化。

**预期结果**：你能画出 4.1.2 那张分派链图，并解释「为什么 CUTLASS 必须把所有这些组合都在编译期实例化出来」。

> 待本地验证：若你已用 `-DBUILD_CUTLASS_MIXED_GEMM=ON` 编译过 `libtransformer-shared.so`，可在 `run_gemm` 入口加一行 `FT_LOG_INFO("candidate_configs=%zu", candidate_configs.size());`，运行任意 weight-only 推理观察候选数量（理论上 = tile 数 × stages 数）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CutlassFpAIntBGemmRunner<float, WeightType>` 要存在却永远抛错？
**答案**：FT 里别的代码（如 `FfnLayer<T>` 对 `T=float` 实例化）会引用这个模板类，必须能通过编译；但 weight-only 在数学上要求激活是浮点而权重是整型，两边类型相同（float/float）就不再是「混合精度」，所以运行期调用直接 `FT_CHECK_WITH_INFO(false, ...)` 拦下。

**练习 2**：`dispatch_stages` 里 Sm80 有一个 `enable_if<(Stages > 2)>` 的特化（[fpA_intB_gemm_template.h:259-298](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L259-L298)），而 Sm70/Sm75 没有。这说明了什么？
**答案**：说明 3/4 级软件流水线（stages）是 Ampere（Sm80）才支持的特性，Volta/Turing 只能跑 stages=2。这也呼应 4.3 节 `max_stages = sm >= 80 ? 4 : 2`。

---

### 4.2 离线权重预处理：cutlass_preprocessors

#### 4.2.1 概念说明

这是本讲回答实践任务的核心模块。tensor core 的 `ldmatrix` 指令对 INT8/INT4 权重的内存排列有极其具体的要求，原始的行主序 `[K, N]` 量化矩阵**不能直接用**。`cutlass_preprocessors` 的职责就是在**离线**（权重加载/转换阶段，而非每步推理）把量化后的权重重排成 CUTLASS kernel 期望的布局。

它有两个入口：

- `preprocess_weights_for_mixed_gemm`：只做布局重排，输入已经是量化后的 INT8/INT4；
- `symmetric_quantize`：一步到位——输入是 FP16/BF16 原始权重，同时完成「逐通道对称量化」和「布局预处理」，输出预处理后的量化权重 + 每列 scale。

两者都由 `QuantType` 枚举控制（[cutlass_preprocessors.h:26-29](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.h#L26-L29)）：

| `QuantType` | 含义 | 每元素位数 |
| --- | --- | --- |
| `INT8_WEIGHT_ONLY` | INT8 权重仅量化 | 8 |
| `PACKED_INT4_WEIGHT_ONLY` | 两个 INT4 打包进一个字节 | 4 |

#### 4.2.2 核心流程

预处理是一条**有条件的四步流水线**，由 `preprocess_weights_for_mixed_gemm` 编排（[cutlass_preprocessors.cc:500-539](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L500-L539)）。具体做哪几步取决于「目标布局」`LayoutDetails`，而布局由 `quant_type` + GPU 架构共同决定（`getLayoutDetailsForTransform`，[:115-131](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L115-L131)）：

```
原始: row-major [K,N] 量化权重
  │
  ├─ 若 uses_imma_ldsm (Turing+ 量化)  → permute_B_rows_for_mixed_gemm   # 步骤1: 行重排
  │
  ├─ 若 layoutB == COLUMN_MAJOR         → subbyte_transpose               # 步骤2: 转置
  │
  ├─ 若 columns_interleaved > 1         → interleave_column_major_tensor  # 步骤3: 列交错
  │
  └─ 总是执行                            → add_bias_and_interleave_*_inplace # 步骤4: 加偏移+寄存器重排
  │
最终: ColumnMajorTileInterleave<64, 2或4> 布局
```

**为什么是这四步**——根本原因在权重布局定义 [mixed_gemm_B_layout.h:60-88](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/mixed_gemm_B_layout.h#L60-L88)。对 Turing+ 上的量化类型，目标布局是 `ColumnMajorTileInterleave<ThreadblockK=64, ColumnsInterleaved>`，且算子是 `OpMultiplyAddDequantizeInterleavedBToA`（加载后再反量化）。其中：

- INT8（uint8_t）：`ElementsPerCacheLine = 128*8/8 = 128`，`ColumnsInterleaved = 128/64 = 2` → `ColumnMajorTileInterleave<64, 2>`；
- INT4（uint4b_t）：`ElementsPerCacheLine = 128*8/4 = 256`，`ColumnsInterleaved = 256/64 = 4` → `ColumnMajorTileInterleave<64, 4>`。

预处理的目标就是把 `[K,N]` 行主序变成这种「以 64 行为一块、块内若干列交错」的列主序分块布局，并加上去符号偏移。

#### 4.2.3 源码精读

**步骤 1：行重排 `permute_B_rows_for_mixed_gemm`**（[cutlass_preprocessors.cc:139-201](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L139-L201)）。它把每 16 行（INT8）或每 32 行（INT4）按一张固定的置换表重排，目的是匹配 `ldmatrix`（LDSM）指令从共享内存取片段时的物理顺序。源码注释给出了精确的置换表（[:134-138](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L134-L138)）：

```
INT8 每 16 行:  0 1 8 9 2 3 10 11 4 5 12 13 6 7 14 15
INT4 每 32 行:  0 1 8 9 16 17 24 25 2 3 10 11 18 19 26 27 4 5 12 13 20 21 28 29 6 7 14 15 22 23 30 31
```

**步骤 2：子字节转置 `subbyte_transpose`**（[cutlass_preprocessors.cc:207-348](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L207-L348)）。把矩阵转置。难点在于 INT4 是「两个值打包进一个字节」，朴素转置会非常慢，所以这里用 64×64 的 L1 cache tile + 手工位操作完成，并刻意让循环步长对齐以触发 GCC 的向量指令（注释 [:203-206](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L203-L206) 解释了这点）。

**步骤 3：列交错 `interleave_column_major_tensor`**（[:437-498](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L437-L498)）。按 `ColumnsInterleaved`（2 或 4）把若干列交错到一起，让 tensor core 一次 cache line 能恰好取满 64 行（即 `ThreadblockK`），提升访存效率。

**步骤 4：加偏移 + 寄存器重排 `add_bias_and_interleave_*_inplace`**（INT8 版 [:350-370](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L350-L370)，INT4 版 [:372-422](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L372-L422)）。两件事：

1. 把**有符号**整数转成**无符号**：INT8 做 `v + 128`（[-128,127]→[0,255]），INT4 做 `v + 8`（[-8,7]→[0,15]）。注释明确说这是为了让反量化用尽量少的指令（[:375-378](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L375-L378)）。
2. 把一个 32 位寄存器里的多个元素按特定顺序重排（INT8 是 `[e3 e2 e1 e0]→[e3 e1 e2 e0]`），让 GEMM 主循环里提取每个元素所需的移位/掩码指令最少（[:356-369](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L356-L369)）。

**对称量化 `symmetric_quantize`**（[cutlass_preprocessors.cc:576-673](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L576-L673)）在 CPU 上对每一列（最后一轴）独立做对称量化。对第 \(j\) 列，先求该列绝对值最大值 \(a_j = \max_i |w_{ij}|\)，然后（[:605-630](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L605-L630)）：

\[
\text{quant\_range\_scale} = \frac{1}{2^{b-1}} \quad (b=8 \Rightarrow \tfrac{1}{128},\ b=4 \Rightarrow \tfrac{1}{8})
\]

\[
s_j = a_j \cdot \text{quant\_range\_scale} = \frac{a_j}{2^{b-1}}, \qquad
\hat w_{ij} = \mathrm{round}\!\left(\frac{w_{ij}}{s_j}\right),\ \text{clip 到 } [-2^{b-1},\,2^{b-1}-1]
\]

注意它用的是除以 \(2^{b-1}\)（INT8 是 128，不是 127）。量化完后再调用 `preprocess_weights_for_mixed_gemm` 完成布局重排（[:672](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L672)）。

#### 4.2.4 代码实践（对应总实践任务）

**实践目标**：解释清楚「为什么权重存 INT8/INT4、激活存 FP16」，并说清 `cutlass_preprocessors` 离线对权重做了什么。

**操作步骤**：

1. 先回答动机部分：在 `FfnLayer` 的 GPT decoder 单步前向里，FFN 第一段 GEMM 的 \(M\) 是当前 token 数（很小），而 \(B\) 是 `[hidden, inter]` 大权重。打开 [docs/gpt_guide.md:64-72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L64-L72)，把 docs 列出的三条限制（hidden 需是 64 倍数、仅小 batch 受益、仅 FP16/BF16）抄到你的笔记里，并把每条限制对应到本讲源码：
   - 「hidden 需是 64 的倍数」← `ThreadblockK = 64`（[mixed_gemm_B_layout.h:62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/gemm/kernel/mixed_gemm_B_layout.h#L62)）与 `is_valid_split_k_factor` 里 `k % 64 == 0`（[cutlass_heuristic.cc:66-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L66-L69)）；
   - 「仅小 batch 受益」← weight-only 省的是权重带宽，batch 大时计算变瓶颈，省带宽收益被稀释；
   - 「仅 FP16/BF16」← FP32 特化直接抛错（[fpA_intB_gemm.h:111-141](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm.h#L111-L141)）。
2. 再回答预处理部分：画出 4.2.2 的四步流水线图，并为每步标注「它解决 tensor core 的哪个要求」。
3. 阅读 [tests/gemm_dequantize/th_gemm_dequantize.py:26-43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/gemm_dequantize/th_gemm_dequantize.py#L26-L43)，看测试如何用单位矩阵 \(A=I\) 作为激活来验证：`fused_gemm_dq(I, 预处理后的权重, scale=1)` 应当还原出原始权重值，从而证明「预处理只是改布局、不改数值语义」。

**需要观察的现象**：测试里 `cuda_weights = self.preprocess_weights_for_mixed_gemm(packed_weight, quant_type)` 之后，权重的**数值**已无法人眼读懂（因为经过了行重排+转置+交错+加 128 偏移），但 `fused_gemm_dq` 跑完又精确还原。

**预期结果**：你能用自己的话讲清——weight-only 存低精度权重是为了省带宽，激活保持浮点是为了精度；而预处理是为了把权重摆成 tensor core 的 `ldmatrix` 指令物理上要求的形状（列主序分块 + 列交错），并把有符号数转无符号、把寄存器位序对齐，从而让反量化在 GEMM 内循环里几乎零开销。

#### 4.2.5 小练习与答案

**练习 1**：`preprocess_weights_for_mixed_gemm` 能否在 GPU 加载权重时「每步推理都跑一遍」？为什么？
**答案**：不应该。它只在权重转换/加载时跑一次（离线或启动期）。原因有二：预处理本身有开销（尤其是 `subbyte_transpose`，注释说朴素实现曾导致大模型预处理极慢）；且预处理结果只与「权重 + 目标 GPU 架构」有关，与每次输入无关。docs 也强调预处理后的权重「MUST be preprocessed for the GPU intended to be used with inference」（[docs/gpt_guide.md:71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L71)）——换架构要重做。

**练习 2**：INT8 预处理里 `v + 128` 这一步，如果不做会怎样？
**答案**：tensor core 的反量化算子 `OpMultiplyAddDequantizeInterleavedBToA` 假设权重是无符号整数。若保留有符号值，反量化结果会整体偏差 128 个量化单位（或者说需要额外一条减法指令来校正），要么精度错误、要么 GEMM 内循环多一条指令、失去 weight-only 的性能意义。

---

### 4.3 运行期 kernel 选择：cutlass_heuristic

#### 4.3.1 概念说明

4.1 节里 `run_gemm` 选 `chosen_config` 用到的就是 `cutlass_heuristic`。它与 [u2-l4](u2-l4-gemm-autotuning.md) 的 cuBLAS 离线调优（`gemm_test` 跑 100 次取最快、写入 `gemm_config.in`）是**两套完全不同的机制**：

- cuBLAS：**离线实测调优**，对每个形状真的跑很多次取最快算法，结果存盘；
- CUTLASS fpA_intB：**运行期理论估算**，不实测，只根据「每个候选配置能在这块 GPU 上达到的理论占用率」和「波浪（wave）填充率」打分挑最优。

为什么 CUTLASS 走运行期估算？因为 CUTLASS 的候选配置是有限的、结构化的（少数几种 tile × stages × split-k），用占用率/波浪模型就能较准地预测性能，无需离线实测；而且 weight-only 的形状会随 batch 变，离线穷举不现实。

#### 4.3.2 核心流程

启发式分三步（[cutlass_heuristic.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc)）：

1. **枚举候选**：`get_candidate_configs(sm, is_weight_only, ...)` 生成所有 `(tile, stages)` 组合；
2. **算占用率**：由 `run_gemm` 逐个候选调用 `dispatch_to_arch(&occupancy)` 得到理论占用率（一个 block 里能同时驻留多少 warp）；
3. **打分挑最优**：`estimate_best_config_from_occupancies` 用「波浪填充率」模型挑出浪费 SM 最少的配置。

**波浪（wave）模型**：GPU 由 `multi_processor_count` 个 SM 组成。一个 tile 配置会在 `ctas_in_m_dim × ctas_in_n_dim` 个 CTA 上跑这个 GEMM（split-k 时再乘 `split_k_factor`）。每个 SM 同时能跑 `occupancy` 个 block，所以「一个波浪」能容纳 `occupancy × multi_processor_count` 个 CTA。需要跑的波浪数：

\[
\text{waves} = \left\lceil \frac{\text{ctas\_for\_problem}}{\text{ctas\_per\_wave}} \right\rceil
\]

最后一个波浪常常只有部分 CTA，造成 SM 空转。打分函数把「空转比例」定义为（[cutlass_heuristic.cc:176-178](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L176-L178)）：

\[
\text{score} = \lceil \text{waves} \rceil - \text{waves}
\]

score 越小，最后一个波浪越「满」，浪费越少。函数挑 score 最小的配置；并列时优先更深流水线（stages 大）、更小 split-k（[:191-201](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L191-L201)）。

#### 4.3.3 源码精读

**候选枚举 `get_candidate_configs`**（[cutlass_heuristic.cc:110-126](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L110-L126)）。先由 `get_candidate_tiles(is_weight_only, ...)` 取候选 tile（[:93-108](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L93-L108)）——weight-only 专用的是 `quant_B_configs`（注意三个配置的 `threadblockM == warpM`：32/64/128）。然后对每个 tile 配上 `stages ∈ [2, max_stages]`，`max_stages = sm>=80 ? 4 : 2`。

**split-k 合法性 `is_valid_split_k_factor`**（[:53-91](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L53-L91)）。对 weight-only，要求 `k` 和 `k/split_k_factor` 都是 64（`k_tile`）的倍数——这正是 docs 里「hidden 需是 64 倍数」的来源。同时还检查 workspace 是否够（split-k 需要 workspace 存部分和，[:82-88](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L82-L88)）。

**打分 `estimate_best_config_from_occupancies`**（[:128-211](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L128-L211)）。除了波浪 score，还有一条闸门：当 \(n \ge \text{SM 数} \times 256\) 时禁止 split-k（`max_split_k = 1`，[:152](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L152)）——此时问题已经足够宽，split-k 只会增加通信开销。还有一个倾向小 tile 的剪枝（[:162-166](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_heuristic.cc#L162-L166)）。`split_k_limit` 在 runner 里固定为 7（[fpA_intB_gemm.h:101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm.h#L101)）。

**workspace 大小 `getWorkspaceSize`**（[fpA_intB_gemm_template.h:572-581](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/fpA_intB_gemm/fpA_intB_gemm_template.h#L572-L581)）。最坏情况每个 block 4 字节、乘 `split_k_limit`，由 `FfnLayer::allocateBuffer` 用 `reMalloc` 申请并跨步复用（[FfnLayer.cc:487-494](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L487-L494)）。

#### 4.3.4 代码实践

**实践目标**：手算一次波浪 score，验证启发式的选择逻辑。

**操作步骤**：

1. 假设一块 108 个 SM 的 GPU（A100 是 108），跑一个 weight-only GEMM，形状 \(m=8, n=4096, k=4096\)，`split_k_factor=1`。
2. 对 `CtaShape128x128x64` 配置（tile.m=128, tile.n=128）：算 `ctas_in_m_dim = ceil(8/128) = 1`，`ctas_in_n_dim = ceil(4096/128) = 32`，总共 32 个 CTA。若 occupancy=2，则 `ctas_per_wave = 2×108 = 216`，`waves = 32/216`，`score = 1 - 32/216 ≈ 0.85`（最后一个波浪严重空载）。
3. 改用 `CtaShape32x128x64`（tile.m=32, tile.n=128）：`ctas_in_m_dim=1`，仍 32 个 CTA，结论类似；但注意当 \(m\) 更大时小 tile 能铺出更多 CTA 填满波浪。
4. 思考：为什么 \(m\) 很小（decoder 单步）时，启发式会偏好能配合 split-k 的小 tile？因为 split-k 沿 \(k\) 维拆出更多 CTA 来填满空闲的 SM。

**需要观察的现象**：score 越接近 0，最后一个波浪越满，该配置越被偏好。

**预期结果**：你能解释「decoder 单步 \(m\) 很小时，weight-only GEMM 靠 split-k 把 \(k\) 维拆开，用更多 CTA 填满 SM」这一关键优化。

> 待本地验证：可在 `estimate_best_config_from_occupancies` 返回前加 `FT_LOG_INFO` 打印 `chosen_config` 的 tile/stages/split_k，跑不同 batch 对比启发式选择的变化。

#### 4.3.5 小练习与答案

**练习 1**：`estimate_best_config_from_occupancies` 里为什么 `n >= multi_processor_count * 256` 就禁止 split-k？
**答案**：split-k 的收益是「问题太小时沿 \(k\) 拆出更多 CTA 填满 SM」。当 \(n\) 已经很大（≥ SM 数 × 256），沿 \(n\) 维的 CTA 数量已足以铺满所有 SM 的多个波浪，再 split-k 只会带来额外的部分和归约通信，得不偿失。

**练习 2**：这套启发式与 cuBLAS 的 `gemm_test` 离线调优相比，各有什么优劣？
**答案**：CUTLASS 启发式无需离线实测、随形状自适应、无配置文件维护，但本质是「理论占用率模型」，对某些形状可能不是真正最优；cuBLAS 离线调优是「真跑取最快」，精度高但要为每个形状生成配置、换硬件/形状要重跑。两者服务对象不同：fpA_intB 形状多变（随 batch），适合运行期估算；cuBLAS 形状相对固定，适合离线穷举。

---

### 4.4 GPU 端逐通道权重校准量化：calibrate_quantize_weight

#### 4.4.1 概念说明

4.2 的 `symmetric_quantize` 是 **CPU** 端的逐通道量化（配合 `cutlass_preprocessors` 的布局重排，用于 weight-only）。本模块 [calibrate_quantize_weight_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu) 是一组 **GPU** kernel，做同样「逐通道求 amax、算 scale、量化」的事，但跑在 GPU 上、产出标准 INT8 权重（不带 fpA_intB 的布局重排），主要服务于 INT8（含 SmoothQuant w8a8，见 [u9-l1](u9-l1-int8-quantization.md)）的权重准备路径。理解它有助于把「量化」和「布局预处理」两件事彻底分开看。

文件里三个工具，命名按「scale 沿哪个轴」区分：

| 函数 | 输入布局 | 输出布局 | scale 轴 | 是否同时量化 |
| --- | --- | --- | --- | --- |
| `invokeLdnCalibrateWeightPerChannel` | `[k,n]` | 只产出 scale `[n]` | 沿 n（每列一个） | 否（仅校准） |
| `invokeLdkCalibrateQuantizeWeightPerChannel` | `[n,k]` | `[n,k]` INT8 + scale `[n]` | 沿 n（每行一个） | 是 |
| `invokeLdnTransposeQuantizeWeightPerChannel` | `[k,n]` | `[n,k]` INT8（转置+量化） | 沿 n（外部给定 scale） | 是（转置+量化） |

#### 4.4.2 核心流程

以 `ldk_calibrate_quantize_weight_per_channel` 为例（[calibrate_quantize_weight_kernels.cu:76-105](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu#L76-L105)），每个 block 负责一行（一个通道）：

1. 用 `blockReduceMax` 求该通道的 amax（[:84-94](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu#L84-L94)），scale = amax / 127（注意这里用 127，与 4.2 的 128 不同）；
2. 用 `float_to_int8_rn`（PTX 饱和取整，见 [u9-l1](u9-l1-int8-quantization.md)）把每个元素量化到 INT8（[:101-104](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu#L101-L104)）。

grid/block 配置在 host 端固定为 `grid(n)` × `block(ceil(k/32)*32)`（上限 1024，[:108-117](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu#L108-L117)）。

#### 4.4.3 源码精读

- `ldn_calibrate_weight_per_channel`（[:27-44](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu#L27-L44)）：只算 scale 不量化，用 `blockReduceMax` 做 warp/block 归约（承接 [u3-l1](u3-l1-core-kernels.md) 的归约套路）。
- `ldn_transpose_quantize_weight_per_channel`（[:130-149](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu#L130-L149)）：用 32×33 的共享内存 tile 做转置（多一列避免 bank conflict，是 [u3-l1](u3-l1-core-kernels.md) 讲过的技巧），再除以外部给定的 scale 量化。

> 与 weight-only 路径的区别：这些 kernel **不**做 `cutlass_preprocessors` 的布局重排，产出的是「普通 INT8 权重」，要喂给 `int8_gemm`（INT8 路径，[u9-l1](u9-l1-int8-quantization.md)）而非 fpA_intB。

#### 4.4.4 代码实践

**实践目标**：对比 CPU 端 `symmetric_quantize` 与 GPU 端 `invokeLdkCalibrateQuantizeWeightPerChannel`，理解两条量化路径的差异。

**操作步骤**：

1. 打开 [cutlass_preprocessors.cc:605-630](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/cutlass_kernels/cutlass_preprocessors.cc#L605-L630)（CPU，weight-only，scale=amax/128）。
2. 打开 [calibrate_quantize_weight_kernels.cu:94-104](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/calibrate_quantize_weight_kernels.cu#L94-L104)（GPU，INT8，scale=amax/127）。
3. 列一张对比表：运行位置、scale 分母、是否做布局重排、产出喂给谁。

**需要观察的现象**：两者都用「逐通道 amax」做对称量化，但分母（128 vs 127）和后续处理（重排 vs 不重排）不同。

**预期结果**：你能说清——`symmetric_quantize` 是「量化 + fpA_intB 布局预处理」一体的 CPU 工具（weight-only 专用）；`calibrate_quantize_weight_kernels` 是「只量化、不重排」的 GPU 工具（INT8 路径用）。两者服务于不同的 GEMM 后端。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ldn_transpose_quantize_weight_per_channel` 的共享内存 tile 用 `shm[32][33]` 而不是 `shm[32][32]`？
**答案**：第 33 列是 padding，用来打破共享内存的 bank conflict——32 个 bank 若正好按 32 宽度访问会全部撞同一个 bank。这是 [u3-l1](u3-l1-core-kernels.md) 讲过的经典技巧。

**练习 2**：GPU 校准用 `amax/127`，CPU weight-only 用 `amax/128`，哪个更「激进」？
**答案**：`/127` 更激进（scale 更大、量化后更易触及 ±127 饱和边界，精度略低但用满量程）；`/128` 留一个单位的余量（最大映射到 127×amax/128 ≈ 0.992·amax，更保守）。两者都是有意的工程取舍。

---

## 5. 综合实践

**任务**：把 weight-only 推理「从 FP16 权重到一次 GEMM 输出」的完整链路在源码里串起来，并解释每一步。

**背景**：假设你要为 GPT 的 FFN 层开启 weight-only INT8（`int8_mode=1`）。

**步骤**：

1. **离线量化与预处理**（4.2）：阅读 [WeightOnlyQuantOps.cc:139-222](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/common/WeightOnlyQuantOps.cc#L139-L222) 的 `symmetric_quantize_helper`，确认它被注册成 torch op `fastertransformer::symmetric_quantize_last_axis_of_batched_matrix`（[:338-340](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/common/WeightOnlyQuantOps.cc#L338-L340)）。说明在 PyTorch checkpoint 转换脚本（如 `examples/pytorch/gpt/utils/`）里，FP16 权重经此 op 变成「预处理后的 INT8 权重 + scale」并随 checkpoint 存盘。
2. **权重加载**（[u2-l5](u2-l5-weight-containers.md)）：确认 `DenseWeight` 同时持有 `int8_kernel`（预处理后的 INT8）与 `weight_only_quant_scale`（FP16 scale）。在 [FfnLayer.cc:200-202](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L200-L202) 看到 `int8_mode_==1` 时强制要求这两个指针非空。
3. **构造 runner**（4.1）：[FfnLayer.cc:412-416](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L412-L416) 在 `int8_mode_==1` 时 `std::make_shared<CutlassFpAIntBGemmRunner<T, uint8_t>>()`。
4. **分配 workspace**（4.3）：[FfnLayer.cc:487-494](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L487-L494) 用 `getWorkspaceSize` 申请 `mixed_gemm_workspace_`。
5. **运行期调用**（4.1）：[FfnLayer.cc:198-217](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L198-L217) 调 `weight_only_int8_fc_runner_->gemm_bias_act(...)`，把 FP16 激活、INT8 权重、FP16 scale、偏置、激活类型一起传进去，epilogue 融合 bias+gelu。
6. **正确性验证**（4.2）：用 [tests/gemm_dequantize/th_gemm_dequantize.py](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/gemm_dequantize/th_gemm_dequantize.py) 的单位矩阵法证明数值正确。

**产出**：一张端到端流程图，从「FP16 权重」开始，依次经过 `symmetric_quantize`（量化+预处理）→ 存盘 → `DenseWeight` 加载 → `CutlassFpAIntBGemmRunner` 构造 → workspace 分配 → `gemm_bias_act`（含启发式选 kernel + CUTLASS 启动）→ FP16 输出，每一步标注对应的源码文件与行号。

> 待本地验证：完整跑通需要一张 Volta/Turing/Ampere 的 GPU，并按 docs 用 `-DBUILD_PYT=ON -DBUILD_CUTLASS_MIXED_GEMM=ON` 编译，再用 `examples/pytorch/gpt/` 的转换脚本生成 int8 checkpoint。若无 GPU，本实践降级为「纯源码阅读型」——只完成流程图与行号标注即可。

## 6. 本讲小结

- **weight-only（fpA_intB）** 把权重存为 INT8/INT4、激活保持 FP16，专治「小 batch、大权重」的 GPT decoder 带宽瓶颈；它只支持 FP16/BF16、hidden 需是 64 倍数、仅 Volta+。
- **`CutlassFpAIntBGemmRunner`** 是一条 7 层模板分派链（激活类型→架构→tile→stages），最终拼出一个具体的 CUTLASS `GemmKernel`；bias+激活融在 epilogue。
- **`cutlass_preprocessors`** 在**离线**把量化权重做四步重排（行重排→转置→列交错→加偏移+寄存器重排），变成 tensor core 的 `ldmatrix` 物理布局（`ColumnMajorTileInterleave<64, 2或4>`），并把有符号数转无符号。
- **`cutlass_heuristic`** 在**运行期**用占用率与「波浪填充率」模型挑最优 CUTLASS tile，**不实测**，与 cuBLAS 的离线 `gemm_test` 调优是两套机制。
- **`symmetric_quantize`（CPU，amax/128，含重排）** 与 **`calibrate_quantize_weight_kernels`（GPU，amax/127，不重排）** 是两条不同的逐通道量化路径，分别喂给 fpA_intB 与 int8_gemm 后端。
- 整套设计的哲学仍是 FT 一贯的「低精度存储 + 高精度计算」：权重低精度省带宽，scale 与激活浮点保精度，靠 CUTLASS 在寄存器里反量化把两者桥接起来。

## 7. 下一步学习建议

- 想看 FP8 如何用类似的「低精度存储 + 高精度计算」思路推进到 Hopper，继续 [u9-l3 FP8 推理](u9-l3-fp8-inference.md)；
- 想了解 MoE 场景下「多个 expert 的 weight-only GEMM」如何用 grouped GEMM 一次性算，看 [u9-l4 CUTLASS 扩展与 MoE GEMM](u9-l4-cutlass-moe-gemm.md)，那里会用到本讲的 `CutlassMoeFCRunner`；
- 若对「离线调优 vs 运行期启发式」的对比感兴趣，可回看 [u2-l4 GEMM 自动调优](u2-l4-gemm-autotuning.md) 并与本讲的 `cutlass_heuristic` 做一张完整对照表；
- 想动手验证正确性，直接跑 [tests/gemm_dequantize/th_gemm_dequantize.py](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/gemm_dequantize/th_gemm_dequantize.py)，它是最小、最直接的 weight-only GEMM 验证入口。
