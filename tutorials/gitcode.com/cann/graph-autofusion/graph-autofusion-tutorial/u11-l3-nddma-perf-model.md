# v35 NDDMA 1D 精确性能模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 NDDMA（多维 DataCopy，GM→UB 搬运）在 ATT 性能建模中的位置，以及新模型 `NDDMA_1D_MULTICORE_V1` 相比 legacy 模型「精确」在哪里。
2. 列出模型的全部输入特征（搬运字节、GM stride、UB stride、dtype、核数）与 Codegen 侧数据结构的映射关系。
3. 掌握 `nddma_model` 与 `api_perf_register` 自注册框架的衔接方式：`NddmaApi` → `TryNewNddmaModel` → `EvaluateNddmaModel` 的调用链。
4. 理解两类回退门禁：raw rank 2~5 无正式模型时回退 legacy；`kUBFuse`（CV 融合）Codegen 路径与 raw descriptor 不等价，必须回退 legacy。

本讲是 u11-l1（v35 平台扩展机制）与 u7-l1（ATT 性能建模与 gen_model_info）的交汇点：前者给出了「v35 是主仓 att 的增量目录」这一前提，后者给出了「算子单次耗时 c 由 `api_perf_register` 自注册提供」这一机制。本讲就看 v35 在这个机制上为 NDDMA 搬运新装了一台什么样的「秒表」。

## 2. 前置知识

在阅读本讲之前，你需要用通俗语言理解以下几个概念（细节在对应前置讲义中已展开）：

- **NDDMA / DataCopyNddma**：昇腾设备端的一类多维 DMA 搬运指令，把数据从全局内存（GM）搬进统一缓冲（UB）。与普通 1D DataCopy 的区别是它天然支持多维的 `repeats × strides` 描述，适合非连续（跨步）搬运。在 Autofuse 的调度结果里，一个 NDDMA 节点对应若干次设备端搬运调用。
- **性能模型（perf model）**：ATT 在编译期为每个候选 tiling 估算耗时时使用的公式。u7-l1 讲过总账公式 \( Cost_p = \sum_n t_n \cdot c_{n,p} + H_p \)：全局 pipe 头开销 \( H_p \) 由 `PipePerfExpr` 统一加，而单次调用开销 \( c_n \) 由 `api_perf_register` 子模块按算子类型提供。本讲的 NDDMA 模型就是负责 NDDMA 节点的 \( c_n \)。
- **legacy 模型**：v2 平台原有的 `GetDmaPerf` 带宽模型（见 `ascir_api_perf_v2.cpp` 文件内注释：`nddma = S/T + h`，`T = 7.61 + 6.39/blockdim`）。它把非连续搬运折算成一个等效数据量 S，精度有限。
- **符号表达式 `Expr`**：因为 Autofuse 支持动态 shape，模型不是代入数字算 cycles，而是构造一个符号多项式交给下游求解器；静态 shape 时表达式直接折叠成常数。
- **TernaryOp（三元表达式）**：当某个输入（如 `block_dim`、动态 stride）在编译期取值不定时，模型生成 `cond ? case_low : case_high` 形式的运行期分支表达式，登记进 `ternary_ops` 表，随 ModelInfo 一起进入求解器代码。
- **raw rank / effective rank**：raw rank 是 legacy「连续轴合并」之前的原始维度数；effective rank 是合并连续轴之后的等效维度数。本讲模型要求两者都为 1 才启用。
- **kUBFuse / CV 融合**：u11-l2 讲过的 Cube-Vector 融合路径。该路径下 Codegen 把 NDDMA 描述固定改写成 `{curAivM, curAlignN}` 的 2D 形态，不再是原始 descriptor。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [autofuse/v35/att/api_perf_register/nddma_model.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h) | 新模型全部对外接口：`NddmaFallbackReason` 枚举、`NddmaNormalizedDesc`/`NddmaModelResult` 结构、四个阶段函数声明。文件头 50 行注释是官方的模型设计文档。 |
| [autofuse/v35/att/api_perf_register/nddma_model.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp) | 模型实现：四组 dtype 拟合参数表、1D 多项式构造、静态/动态分支选择、descriptor 构建与校验、回退日志。 |
| [autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp) | v2 平台算子性能注册总表。`NddmaApi` 是 NDDMA 节点的评估入口，内部先试新模型、失败回 legacy；文件底部是 `ApiPerfRegister` 自注册对象。 |
| [autofuse/att/gen_model_info/api_perf_register/api_perf.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/api_perf_register/api_perf.h) | 主仓公共类型：`NddmaDescriptorInfo`（raw 描述）、`NodeDetail`（含 `nddma_descriptor` 字段）、`TernaryOpMap`。 |
| [autofuse/att/gen_model_info/parser/tuning_space.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/parser/tuning_space.h) | `NodeInfo` 定义，含本讲关键门禁字段 `is_cv_ub_fusion`。 |
| [autofuse/att/gen_model_info/gen_model_info.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp) | `is_cv_ub_fusion` 的生产端：从调度结果的 `kUBFuse` 模板类型提取，经 `ModelGenerationContext` 传入并回填到每个 `NodeInfo`。 |
| [autofuse/att/base/base_types.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/base/base_types.h) | `TensorShapeInfo`：parser 产出的张量形状/步幅信息，是新模型特征的直接来源。 |
| [autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp) | 新模型的单元测试，覆盖四种 dtype 的静态公式回放、动态 ternary、各类回退原因。 |

## 4. 核心概念与源码讲解

### 4.1 NDDMA 1D 特征建模

#### 4.1.1 概念说明

legacy 的 NDDMA 模型只有「等效字节数 S + 核数」两个自由度：\( nddma = S/T + h \)，其中 \( T = 7.61 + 6.39/blockdim \) 是经验带宽。它无法区分「连续搬 1KB」和「跨步搬 1KB」——而后者的 GM 非连续程度（stride）会显著影响 DMA 效率，因为跨步访问破坏了内存的 cache line 与 burst 传输。

新模型 `NDDMA_1D_MULTICORE_V1` 的思路是：把一次 NDDMA 调用的 cycles 建成以 **搬运字节数 B** 与 **GM 非连续程度 s** 为自变量的二元多项式，并按 **核数**（低核/高核）与 **UB 侧 stride**（os=1 / os≥2）分四档取参。当前范围只覆盖 raw rank=1、effective rank=1 的搬运（即默认 Codegen 路径下的一维搬运）。

#### 4.1.2 核心流程

先定义 1D 特征（B 为每核每次搬运字节数，is/os 为 GM/UB 元素 stride，s 是 GM 非连续度）：

\[
B = output\_dims[0] \times dtype\_size,\quad s = \min(is,\ 128)
\]

其中 GM stride 上限饱和到 128（拟合时的截断值），且 is 允许为 0（广播语义）。四档公式为：

\[
low(os) = L_0 + L_B \cdot B + L_s \cdot s + L_{Bs} \cdot B \cdot s
\]

\[
high(os) = C_0 + C_1 \cdot s + C_2 \cdot s^2 + B \cdot (E_0 + E_1 \cdot s + E_2 \cdot s^2)
\]

- `low`：`block_dim ≤ 2`（低核，无多核争用）；
- `high`：`block_dim > 2`（高核，含 \( s^2 \) 项刻画争用随跨步放大）；
- 每档内部再按 os=1 / os≥2 分两套系数（`low_os_one` / `low_os_ge_two` 等）。

最终的核数选择是一个二分支：

```
若编译期可判定 block_dim <= 2   -> cycles = low
若编译期可判定 block_dim > 2    -> cycles = high
否则（动态 block_dim）          -> 生成 TernaryOp(K_LE, block_dim, 2, low, high)
                                  并用 GetPerfVar("nddma_1d_multicore") 引入运行期变量
```

同理，动态 os 时用门函数 \( g = \max(0, \min(1, os-1)) \) 在两套 os 多项式之间连续插值：g=0 选 os=1 分支，g=1 选 os≥2 分支（合法 stride 为整数，因此插值只会落在两端点上）。

#### 4.1.3 源码精读

模型的设计文档就写在头文件注释里，值得完整读一遍：

[autofuse/v35/att/api_perf_register/nddma_model.h:17-71](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h#L17-L71)——官方注释，逐条写明：当前只注册 1D 多核模型；raw rank 2~5 保留完整 descriptor 后回退 legacy（不会因连续轴合并伪装成 1D）；kUBFuse Codegen 使用 `{curAivM, curAlignN}` 和固定 2D stride、与 raw descriptor 不等价，故经 `is_cv_ub_fusion` 门禁回退。

四组拟合参数按 dtype 字节数组织，每组含四个子结构（低核×两种 os、高核×两种 os）：

[autofuse/v35/att/api_perf_register/nddma_model.cpp:43-83](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L43-L83)——`Nddma1DParams` 结构与 `GetNddma1DParams(dtype_size)`：1 字节选 `kB8`、2 字节选 `kB16`、4 字节选 `kB32`、8 字节选 `kB64`，其余返回 nullptr（触发 `kDtypeUnsupported` 回退）。注释明确「合并模型直接表示最终多项式，避免运行时重新组合 NG、NGM 和 rho」——即这些系数是原始拟合参数在合法 os 分支上代数展开后的结果，表值直接对应最终 cycles。

多项式构造分三步：

- [autofuse/v35/att/api_perf_register/nddma_model.cpp:146-157](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L146-L157)——`BuildLowCore`/`BuildHighCore` 把参数结构与 B、s 组装成 `Expr` 多项式。
- [autofuse/v35/att/api_perf_register/nddma_model.cpp:159-171](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L159-L171)——`SelectOutputStrideModel`：静态 os 直接判 1 或 ≥2 选分支；动态 os 构造门函数 `Max(0, Min(1, os-1))` 做两端点插值。
- [autofuse/v35/att/api_perf_register/nddma_model.cpp:173-183](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L173-L183)——`BuildNddma1DBranches`：取 `output_dims[0] × dtype_size` 为 B，`Min(128, input_strides[0])` 为 s，产出 low/high 两条候选表达式。

核数分支与动态 TernaryOp：

[autofuse/v35/att/api_perf_register/nddma_model.cpp:185-205](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L185-L205)——`SelectCoreBranch`：先用 `StaticCheckLe/Gt` 尝试编译期定死；不行则 `GetPerfVar("nddma_1d_multicore", ...)` 造一个运行期性能变量（定义见 [autofuse/att/util/ternary_op.cpp:281](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/util/ternary_op.cpp#L281)），并登记 `TernaryOp(CondType::K_LE, block_dim, 2, low_case, high_case)`。这正是 u7-l2 讲过的「编译期生成求解器代码、运行期拿真实值搜索」架构的一个具体实例。

注意模型边界：头文件注释最后一句「模型只输出单次调用的 AIV_MTE2 cycles；全局 pipe head 仍由 `PipePerfExpr` 统一添加」——即本模型只负责 \( c_n \)，不负责 \( H_p \)。

#### 4.1.4 代码实践

1. **实践目标**：验证四种 dtype 的静态公式确实由参数表直接决定。
2. **操作步骤**：
   - 打开 [autofuse/v35/att/api_perf_register/nddma_model.cpp:61-65](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L61-L65) 找到 `kB32` 的 `low_os_ge_two` 四个系数：`{158.723147, 0.2427621164, 1.4596785, 0.0013874442}`。
   - 手工代入：dim=256、fp32（dtype_size=4）→ \( B = 1024 \)；is=4 → \( s = 4 \)；os=2、block_dim=2 →
     \[ cycles = 158.723147 + 0.2427621164 \times 1024 + 1.4596785 \times 4 + 0.0013874442 \times 1024 \times 4 \approx 418.833 \]
   - 对照 UT [autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp:77-84](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp#L77-L84) 中 `EvaluatesStaticB32LowCoreFormula` 的期望值 `418.8332396657097`。
3. **需要观察的现象**：手算值与断言值在小数点后 3 位一致。
4. **预期结果**：确认「参数表 = 最终多项式系数」这一论断，中间没有隐藏的 NG/NGM 重组。
5. 想实际跑这个 UT 的话（命令格式见 u1-l3 的 build.sh 三维选择体系，属 `autofuse_framework` 的 cpp UT）：`sh build.sh -m autofuse_framework -i cpp -a ut -j 8`——具体过滤参数与跑通与否**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `low` 公式没有 \( s^2 \) 项，而 `high` 公式有？

**答案**：low 分支对应 `block_dim ≤ 2`，几乎不存在多核对 GM 带宽的争用，跨步的代价近似线性（每个跨步多付一次访存启动）；high 分支对应多核并发搬运，跨步造成的 cache line 浪费与 bank 争用会随核数与跨距交互放大，拟合时需要二次项才能覆盖，故 `high` 用 \( C_2 s^2 \) 与 \( B \cdot E_2 s^2 \) 项。

**练习 2**：GM stride 为什么取 `min(is, 128)` 饱和，而 UB stride 不做饱和？

**答案**：`kInputStrideUpperBound = 128` 是拟合阶段的截断值——GM 侧跨距超过 128 个元素后对 DMA 效率的影响不再单调增长（见 nddma_model.cpp:24 的常量与 BuildNddma1DBranches 中的 `Min`），模型用饱和近似；UB 侧 stride（os）不进入连续特征，而是当作离散的分档开关（os=1 / os≥2），所以不需要饱和。

**练习 3**：动态 `block_dim` 时模型输出的是什么？静态时又是什么？

**答案**：静态时直接输出折叠后的常数 `Expr`；动态时输出一个名为 `nddma_1d_multicore` 的运行期性能变量，并附带一条 `TernaryOp(K_LE, block_dim, 2, low, high)` 登记进 `result.ternary_ops`，由下游求解器在拿到真实核数后求值（见 `SelectCoreBranch` 与 UT `BuildsDynamicCoreTernary`）。

### 4.2 descriptor 与 Codegen 映射

#### 4.2.1 概念说明

模型再准，特征喂错了也白搭。本模块回答：模型的输入特征从哪来、与 Codegen 实际生成的搬运代码是否同源。答案是三层结构：

- **`NddmaDescriptorInfo`**（主仓 [autofuse/att/gen_model_info/api_perf_register/api_perf.h:24-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/api_perf_register/api_perf.h#L24-L29)）：「legacy 连续轴合并前的原始 DataCopyNddma 描述」，三个 `Expr` 向量（output_dims / input_strides / output_strides）加轴序 `vectorized_axis`，dim 与 stride 单位分别为元素个数与元素。
- **`NddmaNormalizedDesc`**（[autofuse/v35/att/api_perf_register/nddma_model.h:85-92](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h#L85-L92)）：v35 私有的「schema 和 Codegen 一致性校验后的物理描述」，额外带 `raw_rank` / `effective_rank`。注释说明物理描述与统计特征归一化分离，为后续 2D~5D 模型留扩展位。
- **`TensorShapeInfo`**（[autofuse/att/base/base_types.h:123-143](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/base/base_types.h#L123-L143)）：parser 产出的张量信息，`repeats` / `strides` / `gm_strides` 三个向量是新模型特征的直接来源。

官方注释给出的映射表（这是本讲的核心结论，务必记住）：

| 模型输入 | Codegen/TensorShapeInfo 来源 | 含义 |
| --- | --- | --- |
| `output_dims` | `TensorShapeInfo::repeats` | 单核单次搬运的各维元素个数 |
| `input_strides` | `TensorShapeInfo::gm_strides` | GM 元素 stride |
| `output_strides` | `TensorShapeInfo::strides` | UB 元素 stride |
| 轴序 | `AscTensorAttr::vectorized_axis` | descriptor 的轴排列顺序 |
| `dtype_size` | dtype | 选 B8/B16/B32/B64 参数组 |
| `block_dim` | 核数 | low/high 分支选择 |

三组向量均取「legacy 连续轴合并前」的原始值；默认 Codegen 使用同源的 `DataCopyParams`（经 `SetNddmaParams` 左侧补 1 生成 5 元素 API 数组，补 1 不改变 effective rank 和搬运语义）——即**性能模型看到的描述与设备端实际执行的描述是同一份**，这是「精确」的根本保证。

#### 4.2.2 核心流程

新模型挂进 api_perf_register 的完整调用链：

```
NddmaApi(output_shapes, node, perf_res)                    # v2 平台 NDDMA 节点评估入口
  ├─ TryNewNddmaModel(shape_info, node, ...)
  │    ├─ 门禁1: node.is_cv_ub_fusion == true → 回退 legacy
  │    ├─ BuildNddmaDescriptor(shape_info, vectorized_axis, descriptor)
  │    │      # 从 TensorShapeInfo 原始向量构造 descriptor，不从 legacy 标量 stride 反推
  │    ├─ node_detail.nddma_descriptor = descriptor        # 透传给 NodeDetail
  │    ├─ EvaluateNddmaModel(descriptor, dtype, CreateExpr("block_dim"), result)
  │    │      ├─ NormalizeNddmaDescriptor                  # 校验 rank/向量长度/轴序/静态值
  │    │      ├─ raw_rank==1 && effective_rank==1 ?        # 否则 kNoRegisteredModel
  │    │      ├─ GetDtypeSize + GetNddma1DParams           # 选参数表
  │    │      ├─ BuildNddma1DBranches + SelectCoreBranch   # 产出 cycles
  │    │      └─ selected = true
  │    └─ 成功: perf_res.pipe_res[AIV_MTE2] = cycles; perf_res.ternary_ops ← result.ternary_ops
  └─ 未选中: MergeTensorContinuousDims → SetDims → GetDmaPerf(...)   # legacy 路径
```

注册侧的衔接（u7-l1 讲过的 `ApiPerfRegister` 自注册模式在 v35 的落点）：

- `REGISTER_EVAL_FUNC_TAG(kNddma, V2, ascir_v2::NddmaApi)` 把 `NddmaApi` 登记为 v2 平台 kNddma 节点的评估函数；
- 文件底部的 `nddma_api_perf_v2(ApiPerfRegisterV2(kNddma, GetPerfFunc(kNddma + "V2"), ...))` 全局对象在 main 之前把公式名 `NddmaV2` 写进 `ApiPerfFactory`。

#### 4.2.3 源码精读

[autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:120-142](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L120-L142)——`NddmaApi` 入口：先 `TryNewNddmaModel`，`selected` 为真直接返回；否则走注释里的 legacy 公式（`MergeTensorContinuousDims` 合并连续轴后 `GetDmaPerf`，注意 legacy 传 `kMaxNddmaLen=5`）。

[autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:30-62](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L30-L62)——`TryNewNddmaModel`：第一段就是 kUBFuse 门禁（下一模块展开）；随后 `BuildNddmaDescriptor` 组 descriptor、`node_detail.nddma_descriptor = descriptor` 透传、`EvaluateNddmaModel(..., CreateExpr("block_dim"), ...)` 求值；成功时把 cycles 写入 `perf_res.pipe_res[PipeType::AIV_MTE2]` 并合并 ternary_ops。

[autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:23-28](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L23-L28)——`GetNddmaVectorizedAxis`：轴序取自输出张量属性 `node->outputs[0].attr.vectorized_axis`，空则触发 `kCodegenMismatch` 回退。

[autofuse/v35/att/api_perf_register/nddma_model.cpp:216-234](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L216-L234)——`BuildNddmaDescriptor`：`repeats` 为空 → `kNoDescriptor`；三向量长度不一致 → `kSchemaMismatch`；`vectorized_axis` 为空 → `kCodegenMismatch`。关键设计：直接复制原始向量，**不从 legacy 标量 stride 反推**，避免合轴信息丢失。

[autofuse/v35/att/api_perf_register/nddma_model.cpp:236-254](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L236-L254)——`NormalizeNddmaDescriptor`：rank 限定在 [1,5]（越界 `kRankUnsupported`）；三个向量等长（`kSchemaMismatch`）；轴序无重复且与 dims 同长（`kCodegenMismatch`）；静态值检查（非正 dim、负 input_stride、非正 output_stride → `kStrideInvalid`）。

[autofuse/v35/att/api_perf_register/nddma_model.cpp:256-292](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L256-L292)——`EvaluateNddmaModel` 主流程：校验 → rank 双重判定（raw 与 effective 都必须为 1，否则 `kNoRegisteredModel`）→ dtype 查表 → 字节溢出保护 → 构造分支 → `selected = true`。所有失败路径都**不报错**，只填 `fallback_reason` 后返回 SUCCESS——回退是正常业务路径而非异常。

[autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:499-501](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L499-L501) 与 [autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:690-691](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L690-L691)——两处注册：`REGISTER_EVAL_FUNC_TAG(kNddma, V2, ...)` 登记 eval 函数，`nddma_api_perf_v2` 全局对象以公式名 `NddmaV2` 入工厂表（u11-l1 讲过 v2 公式名加 `V2` 后缀与 v1 共存的机制）。

#### 4.2.4 代码实践

1. **实践目标**：亲手走一遍「一个 NDDMA 节点从调度图到 cycles 表达式」的特征映射，确认表中六个输入各来自哪个结构体字段。
2. **操作步骤**：
   - 从 [autofuse/att/base/base_types.h:123-143](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/base/base_types.h#L123-L143) 的 `TensorShapeInfo` 出发，在 `autofuse/att/gen_model_info/parser/` 下用 Grep 搜 `gm_strides`，找到 parser 是在哪里给这三个向量赋值的（即从 ASCIR 张量视图抽取 repeats/strides 的位置）。
   - 再打开 [autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp:20-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp#L20-L27) 的 `MakeDescriptor`，对照 `BuildNddmaDescriptor` 确认测试构造的 descriptor 与生产路径字段一一对应。
3. **需要观察的现象**：`MakeDescriptor(dim, input_stride, output_stride)` 三个参数恰好落到 `output_dims / input_strides / output_strides` 三个向量，`vectorized_axis` 固定 `{0}`。
4. **预期结果**：能画出一张「`AscTensor` → parser → `TensorShapeInfo` → `NddmaDescriptorInfo` → `NddmaNormalizedDesc` → cycles」的数据流图，并标出每一步的过滤条件。
5. parser 内部赋值细节若与你的推断不符，以源码为准，**待本地确认**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BuildNddmaDescriptor` 强调「不从 legacy 标量 stride 反推」？

**答案**：legacy 路径（`LoadApi`/`NddmaApi` 的 `GetDmaPerf` 分支）会先做 `MergeTensorContinuousDims`，把连续维合并、只保留合并后的标量等效 stride；反推只能得到 effective 视图，raw rank、逐维 stride 等信息已不可逆地丢失。新模型需要 raw 描述（raw rank>1 时还要据此回退而不是误判成 1D），所以必须直接从 `TensorShapeInfo` 的原始三向量复制。

**练习 2**：`NddmaModelResult.ternary_ops` 最终去了哪里？

**答案**：`TryNewNddmaModel` 把它合并进 `perf_res.ternary_ops`（`PerfOutputInfo` 的字段，见 [autofuse/att/gen_model_info/api_perf_register/api_perf.h:66-72](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/api_perf_register/api_perf.h#L66-L72)），随后汇入 ModelInfo，成为 u7-l3 讲过的 tiling 求解器代码里 `block_dim <= 2 ? low : high` 这样的运行期分支。

**练习 3**：`node_detail.nddma_descriptor = descriptor` 这行赋值有什么用？cycles 不是已经算出来了吗？

**答案**：descriptor 被存进 `NodeDetail`（`std::optional<NddmaDescriptorInfo>`，见 [autofuse/att/gen_model_info/api_perf_register/api_perf.h:46](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/api_perf_register/api_perf.h#L46)）供 DFX 打印与后续扩展（如 2D~5D 模型接入时直接取用），不必再次从 shape_info 重建。

### 4.3 回退门禁（is_cv_ub_fusion）

#### 4.3.1 概念说明

「精确模型 + 保守回退」是本模块的设计哲学。新模型只在它被证明与 Codegen 同源的路径上启用，其余一律回退 legacy，且每次回退都记一个**稳定的原因码**，方便线上定位。`NddmaFallbackReason` 共八档：

| 原因码 | 触发条件 |
| --- | --- |
| `kNone` | 正常选中 |
| `kNoDescriptor` | `repeats` 为空 |
| `kRankUnsupported` | raw rank 不在 [1,5] |
| `kSchemaMismatch` | 三向量长度不一致 / dtype 查不到 / block_dim 非正 / 字节数溢出 |
| `kDtypeUnsupported` | dtype_size 不是 1/2/4/8 |
| `kStrideInvalid` | 静态 dim 非正、input_stride 为负、output_stride 非正 |
| `kCodegenMismatch` | vectorized_axis 为空/重复/长度不符，**或 is_cv_ub_fusion 为真** |
| `kNoRegisteredModel` | raw rank 或 effective rank 不为 1（即 2D~5D 场景） |

其中最重要的两条业务门禁：

1. **raw rank 2~5 → `kNoRegisteredModel`**：2D~5D 还没有正式拟合模型。注意门禁判的是 `raw_rank != 1U || effective_rank != 1U` 双条件——即使连续轴合并后 effective rank 是 1（比如 shape 为 `(1, 1, N)` 的搬运），只要 raw rank > 1 就回退，**不会因连续轴合并而伪装成 1D**。这是刻意为之：模型的特征（单 B、单 s）只在真正的一维搬运上被标定过。
2. **kUBFuse → `kCodegenMismatch`**：CV 融合（u11-l2）路径下，Codegen 会把 NDDMA 描述改写成 `{curAivM, curAlignN}` 的 2D 形态并使用固定 2D stride——这与 raw descriptor 数学上不等价。若仍按 raw 1D 建模，估出来的 cycles 对应的是一段并不存在的搬运代码。所以在专用 2D 模型接入前保守回退。

#### 4.3.2 核心流程

`is_cv_ub_fusion` 这个布尔值从调度结果一路流到模型门禁：

```
schedule_result.cube_type == ascir::CubeTemplateType::kUBFuse     # 调度结果侧的模板类型
        │  (gen_model_info.cpp:548)
        ▼
ModelGenerationContext{enable_gather_reduce_penalty, is_cv_ub_fusion}   # 文件内私有上下文
        │  (gen_model_info.cpp:549 → :459)
        ▼
GenerateSingleModelInfoWithContext(..., context.is_cv_ub_fusion)
        │  图解析后逐节点回填 (gen_model_info.cpp:184-186)
        ▼
tuning_space->node_infos[*].is_cv_ub_fusion = true
        │  NodeInfo 字段 (tuning_space.h:190-191)
        ▼
TryNewNddmaModel: if (node.is_cv_ub_fusion) → fallback_reason = kCodegenMismatch, 直接走 legacy
```

这正是 u7-l1 讲过的重构成果：`ModelGenerationContext` 把 Codegen 路径门禁传入建模流程而**不扩公开接口**。同一个标志在 Codegen 侧的同名对应物是 `codegen_kernel.cpp:74` 的 `que.is_cv_ub_fusion = (tpipe.cv_fusion_type == ascir::CubeTemplateType::kUBFuse)`——两侧读的是同一份调度事实。

#### 4.3.3 源码精读

[autofuse/v35/att/api_perf_register/nddma_model.h:72-81](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h#L72-L81)——`NddmaFallbackReason` 枚举定义，八档原因码。

[autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:35-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L35-L42)——kUBFuse 门禁的落点：`TryNewNddmaModel` 的第一件事就是查 `node.is_cv_ub_fusion`，为真则置 `kCodegenMismatch`、`LogNddmaFallback` 记日志（注意此时连 descriptor 都不建，日志传 `nullptr`），直接返回让调用方走 legacy。行内注释写明了原因：「kUBFuse Codegen 分支生成 {curAivM, curAlignN} 和固定 2D stride，与下方 raw 描述不等价」。

[autofuse/v35/att/api_perf_register/nddma_model.cpp:266-269](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L266-L269)——raw/effective rank 双重判定：`raw_rank != 1U || effective_rank != 1U` 即 `kNoRegisteredModel`。配合 `BuildNddmaDescriptor` 不做合轴的事实，保证「连续轴合并不出假的 1D」。

[autofuse/att/gen_model_info/parser/tuning_space.h:190-191](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/parser/tuning_space.h#L190-L191)——`NodeInfo::is_cv_ub_fusion` 字段及其注释：「schedule 级 Codegen 路径门禁。kUBFuse 将 NDDMA 描述固定为 2D，因此 raw 1D 必须使用 legacy 模型」。

[autofuse/att/gen_model_info/gen_model_info.cpp:548-549](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L548-L549)——生产端：从 `schedule_result.cube_type == kUBFuse` 提取标志，装进 `ModelGenerationContext`。

[autofuse/att/gen_model_info/gen_model_info.cpp:173-186](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L173-L186)——消费端：`GenerateSingleModelInfoWithContext` 在图解析完成后，把标志逐节点回填进 `tuning_space->node_infos`，使每个 `NodeInfo` 携带 Codegen 路径信息。

[autofuse/v35/att/api_perf_register/nddma_model.cpp:294-306](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L294-L306)——`LogNddmaFallback`：`GELOGW` 打一条稳定格式的 WARNING（含 node 名、raw/effective rank、dtype、候选模型名、原因码），`GELOGD` 再补 descriptor 三向量的 DEBUG 明细。线上看到 `[ATT NDDMA] fallback:` 前缀即可直接对号入座。

#### 4.3.4 代码实践

1. **实践目标**：把八档原因码各自对应的源码判定点找全，形成一张「回退原因速查表」。
2. **操作步骤**：
   - 打开 [autofuse/v35/att/api_perf_register/nddma_model.cpp:208-214](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/nddma_model.cpp#L208-L214) 的 `NddmaFallbackReasonToString`，抄下八个字符串。
   - 在 `nddma_model.cpp` 与 `ascir_api_perf_v2.cpp` 中 Grep `NddmaFallbackReason::` 的所有赋值点，为每个原因码标注触发它的精确条件与所在函数（`BuildNddmaDescriptor` / `NormalizeNddmaDescriptor` / `EvaluateNddmaModel` / `TryNewNddmaModel`）。
   - 对照 UT 的反向验证用例，如 [autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp:128-130](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp#L128-L130) 起的 `RejectsMismatchedDescriptorSchema` 等用例，以及 [autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_ascir_perf_v2.cpp:680](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_ascir_perf_v2.cpp#L680) 处直接置 `node.is_cv_ub_fusion = true` 来验证 kUBFuse 门禁的测试。
3. **需要观察的现象**：每个原因码至少有一个源码赋值点；kUBFuse 门禁发生在 `BuildNddmaDescriptor` 之前（即 descriptor 尚未构建）。
4. **预期结果**：得到一张「原因码 → 判定函数 → 判定条件 → 对应 UT」的四列速查表。
5. 速查表中「对应 UT」一列可能不全（部分原因码未必有直接用例），缺项标注**待确认**即可。

#### 4.3.5 小练习与答案

**练习 1**：一个 shape 为 `(1, N)` 的 fp16 搬运，raw rank=2 但第二维前的 stride 是连续的（effective rank=1）。新模型会用吗？

**答案**：不会。`EvaluateNddmaModel` 要求 `raw_rank == 1U && effective_rank == 1U` 双条件（nddma_model.cpp:266-269），raw rank=2 命中 `kNoRegisteredModel`，回退 legacy 的 `GetDmaPerf`。设计上宁可少用一个精确模型，也不让 1D 标定参数外推到未标定的维度组合上。

**练习 2**：为什么 kUBFuse 场景不能简单地把 `{curAivM, curAlignN}` 当作一个 2D descriptor 喂给未来的 2D 模型原型？

**答案**：可以这么扩展，但现在的门禁先挡住是有原因的：kUBFuse 的 stride 是**固定的 2D stride**（由 Codegen 按 `curAlignN` 对齐规则生成，见 u8-l2/u11-l2 的 dtype 感知对齐），与 raw descriptor 的语义（向量均为合轴前原始值）不同源。头文件注释（nddma_model.h:69-70）给出的扩展路径正是为此准备的：在 `NddmaNormalizedDesc` 归一化阶段构造 effective view，独立选择 raw 或 normalized 特征——即先分离「物理描述」与「统计特征」，2D 模型接入时再决定用哪套。

**练习 3**：线上日志看到 `[ATT NDDMA] fallback: ..., raw_rank=3, effective_rank=3, fallback_reason=no_registered_model`，说明什么？要紧吗？

**答案**：说明该 NDDMA 节点是真正的三维搬运，尚未纳入精确模型，ATT 用 legacy 带宽模型估值。不要紧——这是设计内的保守回退，精度退化到 legacy 水平而非错误；只有当 2D~5D 模型交付后这类日志才会消失。

## 5. 综合实践

**任务：为一个假想的 `NDDMA_1D_MULTICORE_V2`（比如想加入 L2 cache 命中率特征）梳理需要动到的完整修改面。**

结合本讲三个模块，按以下步骤产出一份清单：

1. **特征侧**（对应 4.2）：新特征若在 `TensorShapeInfo` 里没有，需要先在 parser 阶段把它抽进 `TensorShapeInfo` 或 `AscTensorAttr`，再决定 `NddmaDescriptorInfo`（api_perf.h）是否要加字段——注意头文件注释的约束「不改变现有 descriptor 字段及 legacy 数据结构」，优先在 `NddmaNormalizedDesc` 里扩展。
2. **模型侧**（对应 4.1）：在 `nddma_model.h/cpp` 中新增参数结构与公式分支；静态路径直接折叠、动态路径照 `SelectCoreBranch` 的模式生成新的 `GetPerfVar` 变量与 TernaryOp；把 `model_name` 改成 V2 以便日志区分。
3. **门禁侧**（对应 4.3）：若新特征只在部分场景有效，新增一个 `NddmaFallbackReason` 枚举值（同步更新 `NddmaFallbackReasonToString` 的字符串表——两处必须成对，否则日志打出 `unknown`），并在 `EvaluateNddmaModel` 的校验链中插入判定。
4. **测试侧**：仿照 `test_nddma_model_v2.cpp` 的四类用例（静态公式回放、系数出现在简化后的表达式、动态 ternary、回退原因）为 V2 各写一条。
5. 完成后对照 `git log` 中本次 NDDMA 提交实际改动的文件清单（`git log --oneline -- autofuse/v35/att/api_perf_register/nddma_model.h` 后 `git show <commit> --stat`），检查你的清单是否覆盖了全部真实修改面，漏了什么、多估了什么。

本实践不需要写一行能编译的代码，但会逼你把「特征从哪来、公式怎么建、何时不能用、怎么验证」四件事在源码层面串成一条线——这正是读懂任何性能模型代码的通用框架。

## 6. 本讲小结

- 新模型 `NDDMA_1D_MULTICORE_V1` 把一次 NDDMA 搬运的 cycles 建成以搬运字节数 B 与 GM 非连续度 s（饱和 128）为自变量的二元多项式，按核数（≤2 / >2）与 UB stride（=1 / ≥2）四档取参，每种 dtype（1/2/4/8 字节）各一组系数，参数表即最终多项式。
- 模型输入与 Codegen 严格同源：`repeats→output_dims`、`gm_strides→input_strides`、`strides→output_strides`、`vectorized_axis→轴序`、`dtype→参数组`、`block_dim→核数分支`；descriptor 直接取合轴前的原始向量，不从 legacy 标量反推。
- 衔接点是 `NddmaApi → TryNewNddmaModel → BuildNddmaDescriptor → EvaluateNddmaModel`，选中后把 cycles 写入 `pipe_res[AIV_MTE2]`；未选中无缝落回 legacy `GetDmaPerf`，回退是正常路径而非错误。
- 两类硬门禁：raw rank 或 effective rank 不为 1（`kNoRegisteredModel`，防止合轴伪装 1D）；`is_cv_ub_fusion`（`kCodegenMismatch`，因 kUBFuse Codegen 改写为 2D 描述、与 raw 不同源）。标志由 `schedule_result.cube_type == kUBFuse` 经 `ModelGenerationContext` 回填到每个 `NodeInfo`。
- 动态 shape 下模型输出运行期性能变量 `nddma_1d_multicore` 加一条 `block_dim <= 2 ? low : high` 的 TernaryOp，随 ModelInfo 进入求解器代码；模型只管单次调用开销，pipe 头开销仍由 `PipePerfExpr` 统一添加。
- 每次回退都以稳定原因码记录（`LogNddmaFallback` 的 `[ATT NDDMA] fallback:` WARNING），`NddmaNormalizedDesc` 与 raw descriptor 分离的设计为后续 2D~5D 模型预留了扩展位。

## 7. 下一步学习建议

- 下一讲（u11-l4）转向另一块 v35 新机制：IndirectLoad 的 SIMD/SIMT 寻址优化，看间接访存 `a[index]` 如何生成两套地址计算路径——同样遵循「调度用例生成 → 策略选择 → api_call 代码生成」的分层。
- 若想继续深挖性能建模主线，建议回读 u7-l2 的 `PipePerfExpr` 与 `GeneralSolver`，把本讲的 \( c_n \) 如何进入 \( \sum t_n c_{n,p} + H_p \) 目标函数、以及 TernaryOp 如何被求解器求值补全。
- 想动手验证的读者，可以通读 [autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/tests/v35/ut/att/gen_model_info/api_perf_register/test_nddma_model_v2.cpp) 全部用例，再按 u1-l3 的 build.sh 用法跑一次 `autofuse_framework` 的 cpp UT。
