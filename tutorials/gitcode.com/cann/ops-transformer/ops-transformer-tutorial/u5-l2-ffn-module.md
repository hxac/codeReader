# u5-l2 FFN 模块与 swin 变体算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 FFN 算子融合了哪些子计算（两段矩阵乘、激活、bias、量化/伪量化），以及为什么把它们融进单个 kernel 能减少 kernel 启动与访存开销。
2. 读懂 `ffn_def.cpp` 中「多 dtype 变体列表 + 可选输入 + 属性」的注册方式，以及 `expertTokens` 如何让一个算子同时覆盖 FFN 与 MoEFFN 两种形态。
3. 理解 FFN tiling 如何为两段 matmul 分别生成执行计划、如何用 tiling key 路由量化/伪量化/GLU 等几十种场景变体。
4. 浏览 swin_attention_ffn、swin_transformer_ln_qkv、ffn_worker_scheduler 等「场景变体」算子，归纳出算子域内「主算子 + 场景变体」的组织方式，并对比 FFN 与 attention 算子在目录范式上的异同。

本讲承接 u5-l1 的 MoE 链路认知（expertTokens 正是 MoE 路由的输出），也是 u5-l3 mc2 通信融合的前奏。

## 2. 前置知识

- **FFN 是什么**：Feed-Forward Network，transformer block 中 attention 之后的「两层 MLP」。经典形式是 \( y = \mathrm{act}(x W_1 + b_1) W_2 + b_2 \)：先升维（K1→N1）、过非线性激活、再降维（N1→K1）。它占 transformer 参数量的大头，也是 MoE 化的主要对象。
- **GLU 类激活**：geglu/swiglu/reglu 等变体把激活函数与「门控相乘」结合，要求第一段 matmul 输出宽度翻倍（N1 = 2×K2），前半做激活、后半做门控，逐元素相乘后再进第二段 matmul。
- **量化与伪量化**：回顾 u4-l5 的概念——「量化」是输入 x 和权重都以 INT8 进入、靠 deqScale 反量化回浮点；「伪量化」是 x 为浮点、只有权重是 INT8/INT4，靠 antiquantScale/antiquantOffset 在线还原。FFN 同时支持这两种场景。
- **AICore 的 Cube/Vector 分工**：回顾 u2-l3——矩阵乘走 Cube 单元（数据经 L1/L0A/L0B/L0C 分层缓存），激活等逐元素计算走 Vector 单元（数据在 UB 上）。FFN 这种「matmul + 向量计算 + matmul」的融合算子，tiling 必须同时给两类单元排计划。
- **kernel 启动与访存开销**：如果不融合，`matmul→bias→activation→matmul` 要发射多个 kernel，中间结果必须写回 GM（Global Memory）再被下一个 kernel 读回；融合后中间结果留在 UB/workspace 里，只剩一次 GM 写出。这是本讲反复出现的动机。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ffn/ffn/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/README.md) | FFN 主算子的产品支持表、三种计算公式、参数与约束说明 |
| [ffn/ffn/op_host/ffn_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp) | 算子原型注册：15 个输入（3 必选 + 12 可选）、3 个属性、多 SoC 配置 |
| [ffn/ffn/op_host/ffn_infershape.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_infershape.cpp) | 输出 shape 与 dtype 推导（输出 shape 等于输入 x） |
| [ffn/ffn/op_host/ffn_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp) | 约 2000 行的 tiling 实现：两段 matmul 切分、tiling key 路由、workspace 报价 |
| [ffn/ffn/examples/test_aclnn_ffn.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/examples/test_aclnn_ffn.cpp) | aclnnFFNV3 两段式调用示例 |
| [ffn/swin_attention_ffn/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/README.md) | swin 场景变体：公式、固定 shape 约束、「不支持用户直接调用」 |
| [ffn/swin_attention_ffn/op_host/swin_attention_ffn_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/op_host/swin_attention_ffn_def.cpp) | 变体算子的精简 def 注册 |
| [ffn/swin_attention_ffn/op_host/swin_attention_ffn_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/op_host/swin_attention_ffn_tiling.cpp) | 变体算子的 tiling（含 transpose 空间切分逻辑） |
| [ffn/swin_transformer_ln_qkv/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_transformer_ln_qkv/README.md) | 另一变体：LayerNorm + QKV 投影融合，图模式专用 |
| [ffn/ffn_worker_scheduler/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn_worker_scheduler/README.md) | Attention/FFN 分离场景下的数据扫描算子（AICPU 载体） |

`ffn/` 顶层共有 6 个算子目录：`ffn`（主算子）、`swin_attention_ffn`、`swin_transformer_ln_qkv`、`swin_transformer_ln_qkv_quant`、`ffn_worker_scheduler`、`ffn_worker_batching`。注意 MoE 域的 aclnn 实现放在 `op_host/op_api/` 子目录（`aclnn_ffn.cpp`、`aclnn_ffn_v2.h`、`aclnn_ffn_v3.h`），而不是像 attention 那样放在顶层 `op_api/` 目录——这与 u5-l1 观察到的 MoE 组织方式一致。

## 4. 核心概念与源码讲解

### 4.1 FFN 融合算子：功能与数学模型

#### 4.1.1 概念说明

FFN 主算子把 transformer 前向中「升维 matmul → bias → 激活 →（量化/反量化）→ 降维 matmul → bias」整条链路融合为**一个算子、一次下发**。它同时服务两种业务形态：

- **无专家（expertTokens 为空）**：就是普通 FFN，权重是二维 `[K1, N1]` / `[K2, N2]`。
- **有专家（expertTokens 非空）**：MoEFFN，权重是三维 `[E, K1, N1]`，E 为专家数（上限 256），expertTokens 记录每个专家分到的 token 数——正是 u5-l1 中 `moe_token_permute` 排完序后交给专家层的「分组说明书」。

README 用三个公式分别描述非量化、量化、伪量化场景（[ffn/ffn/README.md:19-37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/README.md#L19-L37)）：

\[ y = \mathrm{activation}(x W_1 + b_1) W_2 + b_2 \]

\[ y = ((\mathrm{activation}((x W_1 + b_1) \times deqScale_1) \times scale + offset) W_2 + b_2) \times deqScale_2 \]

\[ y = \mathrm{activation}(x \times ((W_1 + antiquantOffset_1) \times antiquantScale_1) + b_1) \times ((W_2 + antiquantOffset_2) \times antiquantScale_2) + b_2 \]

**为什么融合能加速**（本讲实践任务的核心论点）：

1. **减少 kernel 启动**：不融合时两段 matmul、bias 加法、激活、量化缩放各自是独立 kernel，每个 kernel 都有启动延迟（下发音件、blockDim 分配、核间同步）；融合后只启动一次。
2. **中间结果不出 GM**：第一段 matmul 的输出 `[M, N1]` 若不融合必须写回 GM 再读回；融合后它留在 Cube 输出缓冲/UB（或 workspace 中的受控区域），第二段 matmul 直接就地消费，省掉一整轮 GM 往返——对 `[M, N1]` 这种大中间矩阵，访存收益往往比省下的启动开销更大。
3. **Cube/Vector 流水衔接**：tiling 保证 Cube 算一块、Vector 算一块、再 Cube，形成生产者-消费者流水（见 4.2）。

#### 4.1.2 核心流程

从调用方视角，FFN 的一次执行是：

```text
调用方（aclnn 或图引擎）
  └─ GetWorkspaceSize / 图编译期 tiling
       ├─ 校验 dtype/format/属性组合
       ├─ 推导输出 shape（等于 x 的 shape）
       ├─ 读取平台信息（UB/L1/L0 尺寸、AIC/AIV 核数）
       ├─ 分别为 MM1、MM2 生成 Cube tiling
       ├─ 选 tiling key（路由到对应 kernel 变体）
       └─ 报价 workspace（中间矩阵 + 同步区 + 系统区）
  └─ Run：下发一个融合 kernel
       └─ device 侧按 tiling data 执行
            GM(x) → [MM1: Cube] → workspace/UB → [激活: Vector] → [MM2: Cube] → GM(y)
```

#### 4.1.3 源码精读

**① 输入签名：一列 dtype 一个场景。** def 文件里每个输入的 `DataType({...})` 列表长度都是 10——每个下标对应一种「场景组合」。[ffn_def.cpp:22-29](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L22-L29) 注册了 x 的 10 种取值（fp16/int8/fp16/bf16/...），而 weight1 在 [ffn_def.cpp:30-37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L30-L37) 同位置是 `{DT_FLOAT16, DT_INT8, DT_INT8, DT_BF16, DT_INT4, ...}`：第 0 列是「x=fp16, w=fp16」的非量化场景，第 1 列是「x=int8, w=int8」的量化场景，第 4 列是「x=fp16, w=int4」的伪量化场景。**同一下标的各输入 dtype 必须自洽**，这是比逐输入独立白名单更强的约束形式（u5-l1 称之为「dtype 变体列表等长约定」）。

**② expertTokens：一个可选输入区分两种业务。** [ffn_def.cpp:46-53](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L46-L53) 把 `expert_tokens` 注册为 OPTIONAL 的 INT64 输入。README 进一步说明它是 Host 侧 aclIntArray、非空时最长 256（[ffn/ffn/README.md:79-84](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/README.md#L79-L84)）。注意它是**值在 Host 上可见的张量**，tiling 阶段能直接读到每个专家的 token 数，从而决定专家间如何分核。

**③ 三个属性控制激活与精度。** [ffn_def.cpp:142-149](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L142-L149)：`activation`（必选字符串，取值 fastgelu/relu/silu/gelu/geglu/swiglu/reglu）、`inner_precise`（0 高精度 / 1 高性能）、`output_dtype`（仅 int8 输入时生效，决定输出 fp16 还是 bf16）、`tokens_index_flag`（expertTokens 里存的是计数还是索引）。

**④ 输出与多 SoC 注册。** 输出 y 的 shape 推导极其简单——直接等于输入（[ffn_infershape.cpp:23-31](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_infershape.cpp#L23-L31)），因为约束 K1=N2 保证了 FFN 是「进多少维出多少维」；dtype 推导则对 int8 输入按 `output_dtype` 属性分流（[ffn_infershape.cpp:33-48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_infershape.cpp#L33-L48)）。SoC 侧，[ffn_def.cpp:151-164](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L151-L164) 用同一份 `aicoreConfig` 注册 ascend910b 与 ascend910_93（ND 格式），随后复用变量改写为 FRACTAL_NZ 格式注册 ascend310p（[ffn_def.cpp:165-240](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L165-L240)），Kirin 系列则单独走 `GetKirinCoreConfig()`（[ffn_def.cpp:242-244](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L242-L244)）。再次印证 u2-l1 的结论：def 的 AddConfig 是「可编译范围」，README 产品表（A3 √、A2 ×，[ffn/ffn/README.md:3-14](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/README.md#L3-L14)）是「产品化交付范围」，二者不必一致。

**⑤ 两段式调用示例。** [test_aclnn_ffn.cpp:110-119](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/examples/test_aclnn_ffn.cpp#L110-L119) 展示了 `aclnnFFNV3GetWorkspaceSize` 的调用：3 个必选张量之后跟着一长串 `NULL`（对应 12 个可选输入全不使用），再是 `"relu"` 激活、`inner_precise=1`、`tokens_index_flag=false`。对照 def 的输入顺序即可理解每个位置的含义。

#### 4.1.4 代码实践

**实践目标**：亲手拆解 FFN 的「融合清单」，并跑通（或走读）官方 aclnn 示例。

**操作步骤**：

1. 打开 [ffn/ffn/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/README.md)，把「参数说明」表（39-156 行）中的 15 个输入分成四组：必选主输入（x/weight1/weight2）、专家分组（expertTokens）、bias 组（bias1/bias2）、量化组（scale/offset/deqScale1/deqScale2）、伪量化组（antiquantScale1/antiquantScale2/antiquantOffset1/antiquantOffset2）。
2. 对照 [ffn_def.cpp:22-149](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp#L22-L149) 逐个核对：每个输入的 ParamType（REQUIRED/OPTIONAL）与 dtype 列表长度是否都是 10。
3. 有 NPU 环境时，参照 u1-l4/u2-l4 的方式编译并运行示例：

   ```bash
   bash build.sh --ophost --opapi --ops=ffn --soc=ascend910_93   # 编译 host+api
   bash build.sh --run_example ffn eager                          # 编译并运行示例
   ```

4. 无 NPU 环境时，走读 [test_aclnn_ffn.cpp:71-131](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/examples/test_aclnn_ffn.cpp#L71-L131)，写出 18 个位置参数各自对应 def 里的哪个输入/属性。

**需要观察的现象**：示例中 x/weight1/weight2 都是 `{4,2}`/`{2,2}` 的极小 fp16 矩阵、激活取 `"relu"`——这是非量化、无专家、N1=K2 的最简场景；若把 `"relu"` 换成 `"swiglu"`，按 README 约束（161 行）必须同时保证 N1=2×K2 且全 fp16 高性能场景，否则第一段接口会报参数错。

**预期结果**：得到一张「融合子计算清单表」：子计算 = 2×matmul + 2×bias 加法 + 1×激活（或 GLU 门控乘法）+ 可选的量化缩放/偏移与反量化；运行示例则输出 `test aclnnFFNV3` 并打印结果张量。运行结果**待本地验证**（本讲义写作环境无 NPU）。

#### 4.1.5 小练习与答案

**练习 1**：FFN 输出的 shape 推导为什么可以只写 `*out_shape = *in_shape` 一行？依据是什么？

**答案**：因为约束说明（README 163 行）要求 K1=N2（升维后再降回原维度），且 M 轴（token 数）在计算中不变，所以输出的每一维都与输入 x 相同；这行代码是数学约束在代码上的直接投影。

**练习 2**：为什么 `expertTokens` 设计成 Host 侧 aclIntArray 而不是普通 Device 张量？

**答案**：tiling 发生在 Host 侧（GetWorkspaceSize/图编译期），算子需要在此阶段就知道各专家的 token 数才能决定专家间分核与 baseM；aclIntArray 的值在 Host 可直接读取（`context->GetOptionalInputTensor` 拿到的张量数据在 Host 内存），若放 Device 则 tiling 阶段读不到，只能按最坏情况保守切分。

**练习 3**：对照 def 中 dtype 列表，说出下标 4 对应的场景组合中 x、weight1、y 的 dtype。

**答案**：x=DT_FLOAT16（第 5 个）、weight1=DT_INT4、y=DT_FLOAT16——即「x 浮点 + 权重 INT4」的伪量化场景，对应 README 公式三。

### 4.2 FFN 的 tiling：两段 matmul 的联合执行计划

#### 4.2.1 概念说明

FFN 的 tiling 是工业级 tiling 的典型样本，复杂度远超 u2-l2 教学算子的「写死参数」：

- **两段 matmul 要分别切分**：MM1 是 `[M, K1] × [K1, N1]`，MM2 是 `[M, K2] × [K2, N2]`，各自的 baseM/baseN/baseK 受不同资源约束（L0A/L0B/L0C/L1/UB），还要保证 Cube 输出块能被 Vector 就地消费（Cube baseM 必须是 Vector baseM 的整数倍）。
- **tiling key 是场景路由器**：量化、伪量化 per-channel、伪量化 per-group、BF16 高精度、MSD（小 token 量化特化）、GLU、甚至「两段 matmul 能否合成一段」（`ONE_MATMUL` 特征位）……每一种都需要不同的 device 代码路径，靠 tiling key 整数路由（回顾 u2-l3：运行期 tiling key → 编译期二进制变体）。`op_kernel` 目录下 `ffn_quant.h`、`ffn_antiquant.h`、`ffn_glu.cpp`、`ffn_high_precision.h` 等文件正是按这个维度拆分的。
- **workspace 是中间结果的落脚点**：MM1 的输出 `[maxTokens, N1]` 太大放不进 UB 时，workspace1/workspace2 承接中间矩阵，tiling 要精确报价字节数。

#### 4.2.2 核心流程

tiling 主入口 `RunFusionKernelTiling`（[ffn_tiling.cpp:652-699](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L652-L699)）的骨架：

```text
RunFusionKernelTiling(context)
  ├─ Init()                       # 复位场景标志
  ├─ CheckAndGetBasicInfo()       # 平台信息检查 + 参数/dtype/format 校验 + 解析输入 shape
  │     ├─ FFNParamsCheck()       # 激活字符串 -> ActiveType 枚举，inner_precise 校验
  │     └─ GetInputShape()        # 有专家读 weight 的 E 维，无专家读 N 维；解析 M/K1/N1/N2
  ├─ UpdateMaxTokens()            # 有专家时 maxTokens=bs（最坏情况），无专家时=bs
  ├─ CheckMSD()                   # 判定是否命中小 token 量化特化
  ├─ bs/k1/n2 全 0 → 空算子直接返回（tilingKey=0, blockDim=0）
  ├─ SetTilingBaseParams()        # blockDim、scheduleMode=1、基础 shape 写入 tiling data
  ├─ 激活是 GLU 类 → FFNGlu()     # 独立的 GLU tiling 分支，tilingKey=2
  ├─ TilingWithDifferentKN()      # 通用分支：先试性能分支，失败回退单核分支 + MatmulApiTiling
  ├─ FFNGetScaleGroupNum()        # 判定伪量化 per-group（scale 是 2D/3D）
  └─ FFNSetTilingData()           # 选 tiling key、算 workspace、SaveToBuffer
```

#### 4.2.3 源码精读

**① 平台信息的采集与缓存。** [ffn_tiling.cpp:186-198](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L186-L198) 定义 `FFNCompileInfo`，缓存核数、UB/L1/L0A/L0B/L0C 尺寸与 SoC 版本；这些值只在编译期由 `TilingPrepareForFFN` 填一次（[ffn_tiling.cpp:1997-2018](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1997-L2018)），运行期 tiling 直接复用——这是「编译期平台信息 + 运行期 shape 信息」两阶段合作的标准写法。最后的注册行（[ffn_tiling.cpp:2019](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L2019)）：

```cpp
IMPL_OP_OPTILING(FFN).Tiling(TilingFFN).TilingParse<FFNCompileInfo>(TilingPrepareForFFN);
```

把 tiling 函数与 compile-info 解析函数挂到 FFN 这个算子名上，与 def 文件的 `OP_ADD(FFN)` 遥相呼应。

**② 有专家/无专家的 shape 解析分叉。** [ffn_tiling.cpp:546-571](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L546-L571)：`tokensArrTensor` 非空时从三维 weight 的 `GetDim(2)` 取 N1/N2、`GetDim(0)` 取专家数，并校验「expertTokens 元素个数 == 专家数 ≤ 256」；为空时 N1/N2 取自二维 weight 的 `GetDim(1)`，专家数置 1。**同一个 tiling 函数用一个 if 服务两种业务**，这就是「一个算子覆盖 FFN + MoEFFN」在源码上的落点。

**③ tiling key：场景路由表。** [ffn_tiling.cpp:49-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L49-L61) 定义了全部 key 常量（`QUANT_KEY=1`、`HIGH_PRECISION_KEY=3`、`ANTI_QUANT_KEY=6`、`HIGH_PRECISION_BF16_KEY=7`、`ANTI_QUANT_PERGROUP_KEY=12`、`QUANT_BF16_KEY=11`、`ANTI_QUANT_MSD_KEY=15` 等），[SelectTilingKey（ffn_tiling.cpp:1883-1911）](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1883-L1911) 按 dtype 组合与场景标志查表返回。更精妙的是 [FFNSetTilingKey（ffn_tiling.cpp:1913-1935）](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1913-L1935) 中的**特征位叠加**：若两段 matmul 的 baseM/baseN/baseK 完全一致，key 加上 `ONE_MATMUL (=2000)`，提示 device 侧可把两段 matmul 当作同构循环执行，进一步减少切换开销。

**④ workspace 报价。** [FFNSetTilingData（ffn_tiling.cpp:1937-1988）](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1937-L1988) 计算 workspace1（MM1 中间结果，高精度场景按 float 4 字节、其余按 fp16 2 字节）、workspace2（激活后矩阵）、同步区（`(aicNum<<1)*32`）与系统区之和；伪量化场景还要追加把 INT4/INT8 权重在线还原成 fp16 的两块 buffer（[ffn_tiling.cpp:1978-1982](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1978-L1982)）。这直接印证 4.1 的论点：**融合省掉的是 GM 往返，但代价是要精确管理 workspace 里的中间结果**。

**⑤ Cube/Vector 块对齐。** [CalMM1TilingBaseMNK（ffn_tiling.cpp:1087-1095）](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1087-L1095) 在算完 Cube 的 baseM1 后，还要反推一个能整除它的 Vector baseM（从大到小找第一个能整除 `cubeMFactor` 的 `vectorMFactor`），保证 Cube 产出一块、Vector 消费整数块，不留残块。

#### 4.2.4 代码实践

**实践目标**：追踪一次 tiling key 的选择过程，理解「输入组合 → kernel 变体」的路由。

**操作步骤**：

1. 在 [SelectTilingKey](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1883-L1911) 里手动模拟三组输入，写出各自命中的 key：
   - A：x=fp16、weight=fp16、inner_precise=0、无专家；
   - B：x=int8、weight=int8、y=fp16、deqScale=UINT64；
   - C：x=bf16、weight=int8、antiquantScale 为 3 维（per-group）。
2. 对每组，再检查 [FFNSetTilingKey](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1913-L1935)：若 MM1/MM2 的 baseMNK 恰好一致，key 会 +2000。
3. 到 `ffn/ffn/op_kernel/` 目录对照：`ffn_quant.h`、`ffn_antiquant.h`、`ffn_high_precision.h` 各自服务哪些 key（文件名即提示；`ffn.cpp` 是总入口，按 tiling key 分发——可与 u2-l3 的 `if constexpr` 路由对照）。
4. （可选，需环境）用 `bash build.sh --ophost --ops=ffn --noexec` 仅编译 host 侧，确认 tiling 代码可独立通过编译。

**需要观察的现象**：三组输入分别命中 `HIGH_PRECISION_KEY=3`、`QUANT_KEY=1`（可能 +2000+1 变成 stepN=2 特化）、`ANTI_QUANT_PERGROUP_KEY=12`；kernel 目录里的头文件名与 key 语义一一对应。

**预期结果**：画出一张「输入组合 → 场景标志（isPerGroup/isMsdCase/isQuantBf16...）→ tiling key → op_kernel 变体文件」的路由表。编译验证**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`UpdateMaxTokens()`（[ffn_tiling.cpp:424-431](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L424-L431)）里，为什么有专家时 maxTokens 取 bs、无专家取 bs 看似一样，代码却要区分 `maxTokensCheckOpt`？

**答案**：有专家时单个专家可能分到 0~bs 任意多个 token，tiling 必须按最坏情况 maxTokens=bs 切分；但 `maxTokensCheckOpt` 取均值 `bs/expertNum`，用于**判断是否可启用优化分支**（如 MSD 特化、stepN=2），即「按最坏情况保守切分、按平均情况试探优化」的双轨策略。

**练习 2**：`ONE_MATMUL (=2000)` 特征位为什么只对 `HIGH_PERFORMANCE_KEY` 和 `QUANT_KEY` 生效？

**答案**：只有这两个高性能/量化场景的 kernel 实现里写了「两段 matmul 同构合并」的代码路径（mm1/mm2 用同一套循环模板），高精度、BF16、伪量化等场景的中间还要做反量化/格式转换，两段 matmul 不同构，合并无收益，device 侧也没有对应变体。

**练习 3**：伪量化场景的 workspace 为什么比非量化场景大出两块？

**答案**：见 [ffn_tiling.cpp:1978-1982](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1978-L1982)：INT4/INT8 权重不能直接进 Cube，kernel 要先按 `(w + offset) * scale` 在线还原成 fp16，还原后的 W1'（k1×n1×2 字节）与 W2'（n1×n2×2 字节）需要额外的 workspace 存放——这是「伪量化省内存（权重存低精度）但费 workspace（还原需临时空间）」的权衡。

### 4.3 swin 变体与「主算子 + 场景变体」组织

#### 4.3.1 概念说明

`ffn/` 目录下的另外 5 个算子都是「场景变体」——它们不追求通用性，而是针对一个具体网络/场景把某段固定计算融合到极致：

| 变体 | 融合内容 | 特点 |
| --- | --- | --- |
| swin_attention_ffn | matmul + bias + 残差加法（\( y = x1 \cdot x2 + bias + x3 \)） | shape 固定为 [B,64,128]×[128,128]，仅 fp16，图模式专用 |
| swin_transformer_ln_qkv | LayerNorm + transpose + matmul + split 出 Q/K/V | 输入 [B,S,H] 固定形态，图模式专用 |
| swin_transformer_ln_qkv_quant | 上者的量化版 | 同上 |
| ffn_worker_scheduler | Attention/FFN 分离场景下的数据扫描整理 | AICPU 载体（`op_kernel_aicpu` 目录），需与 AttentionToFFN/FFNWorkerBatching 配套 |
| ffn_worker_batching | 配套的数据批处理 | 同上 |

这与 attention 域的组织方式（u4 系列看到的 FA 家族按「版本演进」分层）不同：**ffn 域按「主算子通用 + 变体贴场景」分层**。变体的收益逻辑：当输入 shape 与网络结构完全固定（如 swin 的窗口大小 64×128），tiling 可以退化为常数，kernel 可以按这个 shape 手工调优到极限，连「读 shape、算切分」的开销都省掉。

#### 4.3.2 核心流程

以 swin_attention_ffn 为例，它在图中的执行路径：

```text
GE 图引擎（swin transformer 整网）
  └─ 匹配到 SwinAttentionFFN 节点（op_graph/swin_attention_ffn_proto.h 提供 IR 原型，
     fusion_pass/ 目录可把拆散的 matmul+bias+add 子图融合回本算子）
       └─ 图编译期：TilingFuncForSwinAttentionFFN
            ├─ SAFFNTilingTPROLL: 读平台核数/UB/L1/L0C，解析 shifts 属性与固定 shape，
            │   计算 transpose 空间切分（tpBlockSize/tpSpaceH/tpSpaceW...）
            ├─ SAFFNTilingMM: 按 MATMUL_SIZE=512 切 batch，算 bmm 分核
            └─ MatmulApiTiling bmm: 生成 Cube tiling，SetBlockDim(aicNum)
       └─ 运行期：单次下发，Cube 做 bmm，Vector 做 bias+残差
```

#### 4.3.3 源码精读

**① 极简 def：与主算子的反差。** [swin_attention_ffn_def.cpp:24-55](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/op_host/swin_attention_ffn_def.cpp#L24-L55)：只有 4 个输入（x1/x2/bias 必选、x3 残差可选）、1 个输出、1 个 `shifts` 列表属性，dtype 全部是 `DT_FLOAT16` 单元素列表——对比主算子每输入 10 个 dtype 变体，场景变体把「通用性」完全砍掉了。SoC 注册也只有 ascend910b 与 Kirin 两系。

**② 「不支持用户直接调用」的交付形态。** [swin_attention_ffn/README.md:116-118](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/README.md#L116-L118) 明确写着「当前不支持用户直接调用」；对照目录结构，它没有 aclnn 接口层（无顶层 `op_api`，aclnn 头文件不存在），只有 `op_graph/`（proto + fusion_pass）。也就是说这类算子**只在图引擎把 swin 整网下发的链路上生效**，用户侧入口是导入 ONNX/构图，而不是调 aclnn。swin_transformer_ln_qkv 同理，README 给出的调用样例是图模式 `test_geir_swin_transformer_ln_qkv.cpp`（[swin_transformer_ln_qkv/README.md:111-115](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_transformer_ln_qkv/README.md#L111-L115)），其公式（[README.md:18-27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_transformer_ln_qkv/README.md#L18-L27)）把 LayerNorm、转置、投影、切分四步合为一。

**③ tiling 的「场景特化」写法。** [swin_attention_ffn_tiling.cpp:35-42](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/op_host/swin_attention_ffn_tiling.cpp#L35-L42) 直接 `#define MATMUL_SIZE 512`、`#define WORKSPACE_SIZE (100 * 1024 * 1024)`——切分参数与 workspace 都是**为固定 shape 量身的常量**；[SAFFNTilingTPROLL（100-139 行）](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/op_host/swin_attention_ffn_tiling.cpp#L100-L139) 还按 `aiv:aic == 2:1` 的 910B 硬件比例硬编码核分配，Kirin 则单独分支。对比主算子 tiling 的几百行资源推导，这里几乎全是「查表填常数」。注册方式仍是标准的 `IMPL_OP_OPTILING(...).Tiling(...).TilingParse<...>(...)`（[swin_attention_ffn_tiling.cpp:232-234](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/op_host/swin_attention_ffn_tiling.cpp#L232-L234)）。

**④ 与主算子、与 attention 的目录范式对比。**

| 维度 | ffn 主算子 | swin 变体 | attention（FA 家族，u4） |
| --- | --- | --- | --- |
| aclnn 接口 | 有（v1/v2/v3 多版本，位于 `op_host/op_api/`） | 无，图模式专用 | 有（顶层 `op_api/`，L2 入口 + 共用 base） |
| op_graph | fallback 脚本 | proto + fusion_pass（核心交付） | proto + fusion_pass |
| op_kernel | 按 tiling key 拆多个变体头文件 | 单一实现 | arch22/arch35 多架构目录 |
| dtype 灵活性 | 每输入 10 种组合 | 仅 fp16 | 支持量化/稀疏但按版本分口 |
| tiling | 资源推导 + 场景路由，约 2000 行 | 固定 shape 常量填空，235 行 | 多 SoC + 场景双重路由 |

结论：**范式骨架（op_host/op_kernel/op_graph/tests/examples 五层）完全一致，差异在「哪一层是重心」**——通用算子重心在 op_host/op_api，场景变体重心在 op_graph（融合回图里），attention 大算子重心还在多 SoC 的 op_kernel。

#### 4.3.4 代码实践

**实践目标**：归纳 swin 变体与基础 ffn 的输入差异，体会「场景变体」的取舍。

**操作步骤**：

1. 对照阅读两份 README 的参数表：[ffn/ffn/README.md:39-156](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/README.md#L39-L156) 与 [swin_attention_ffn/README.md:60-114](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_attention_ffn/README.md#L60-L114)，逐项填一张对比表：输入个数/名字、可选性、dtype 种类、shape 是否固定、是否有激活属性、是否有量化参数、调用方式。
2. 打开 [swin_transformer_ln_qkv/README.md:28-103](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/swin_transformer_ln_qkv/README.md#L28-L103)，注意它的输出是**三个张量**（query_output/key_output/value_output）——与 FFN 单输出的差异源于它融合的是 attention 之前的 QKV 投影段。
3. 列出两个 swin 算子目录的子目录（`ls ffn/swin_attention_ffn ffn/swin_transformer_ln_qkv`），确认都没有顶层 `op_api`，且都含 `op_graph`。
4. 浏览 [ffn_worker_scheduler/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn_worker_scheduler/README.md) 的功能说明（17-60 行左右），记下它的 AICPU 载体（目录为 `op_kernel_aicpu`，回顾 u2-l5）与「需与 AttentionToFFN、FFNWorkerBatching 配套」的约束。

**需要观察的现象**：swin 变体的每个输入都标注了「仅支持 [64,128]」这类固定 shape；主算子则允许 2~8 维动态输入；ffn_worker_scheduler 甚至不接收普通 tensor 而是接收约定好内存排布的 ScheduleContext 结构体。

**预期结果**：产出一张差异表，核心结论形如——swin_attention_ffn 相比基础 ffn：输入从 15 个减到 4 个（无量化/伪量化/专家参数）、dtype 从 10 种组合减到 1 种、shape 从动态变为固定、新增残差输入 x3、调用方式从 aclnn 直调变为仅图模式。本实践为纯源码阅读，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：swin_attention_ffn 的公式 \( y = x1 \cdot x2 + bias + x3 \) 里没有激活函数，它为什么还叫「FFN」？

**答案**：它是 swin transformer 网络中 FFN 段的一部分——上游已经把激活等计算折进了 x1 的产生过程或其他算子，本算子只承载「投影 matmul + bias + 残差」这一段；命名按**在网络中的位置**而非数学形式，这正是场景变体的特征：算子边界服从整网切分方案，而不是服从教科书公式。

**练习 2**：为什么 swin 变体把 tiling 写成常量（`MATMUL_SIZE 512`、固定 workspace 100MB）是合理的，而主算子绝不能这样做？

**答案**：变体在 README 里锁死了输入 shape（[B,64,128]×[128,128]），切分空间只有 B 一个自由度，穷举后取最优常量即可；主算子面对任意 M/K/N 与十种 dtype 组合，必须在线推导。固定 shape 换来的是：图编译期 tiling 近乎零开销、kernel 可按该 shape 深度手工调优。

**练习 3**：如果你要为新网络 N 写一个类似 swin_attention_ffn 的场景变体，按本仓库范式需要交付哪些文件？

**答案**：op_host 三件套（def/infershape/tiling）、op_kernel 的 AscendC 实现、`op_host/config/<soc>/` 下的 ini 与 binary 配置（对照 swin_attention_ffn/op_host/config 目录）、op_graph 的 proto 声明（供 GE 识别 IR）与可选的 fusion_pass（把等价子图融合成本算子）、tests 与 README；**不需要** op_api/aclnn 层，因为变体只走图模式。

## 5. 综合实践

**任务：写一篇《FFN 融合收益分析》短文（约 500 字 + 两张图/表）。**

1. **融合清单**：基于 [ffn/ffn/README.md:19-37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/README.md#L19-L37) 的三个公式与 [ffn_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_def.cpp) 的输入签名，列出非量化/量化/伪量化三种场景下该算子各融合了哪些子计算（matmul、bias、激活/GLU、量化缩放、反量化、权重在线还原……）。
2. **收益论证**：从 kernel 启动次数、GM 访问字节数（算一遍 `[M,N1]` 中间矩阵一轮 GM 往返的字节数，代入 M=4096、N1=10240、fp16）、Cube/Vector 流水三个角度说明融合动机，并指出代价（tiling 复杂度、workspace 增大，引用 [ffn_tiling.cpp:1937-1988](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L1937-L1988) 的 workspace 报价作为证据）。
3. **变体对比表**：完成 4.3.4 的差异表，附一句结论：什么情况下值得为主算子再造一个场景变体（输入完全固定 + 图模式链路 + 极致性能诉求）。

## 6. 本讲小结

- FFN 主算子把「两段 matmul + bias + 激活（含 GLU 门控）+ 可选量化/伪量化」融进一次下发，核心收益是省掉 `[M,N1]` 中间矩阵的 GM 往返与多次 kernel 启动；代价是近 2000 行的 tiling 与更大的 workspace。
- 一个可选输入 `expertTokens`（Host 侧 aclIntArray）让同一算子覆盖 FFN 与 MoEFFN：tiling 里用 `GetOptionalInputTensor` 的有无分叉解析二维/三维权重，与 u5-l1 的 MoE 路由链路无缝衔接。
- def 中每个输入的 dtype 列表长度 10、按同一下标组合取值，是「场景组合」级的强约束；tiling key（量化/伪量化/BF16/GLU/MSD/ONE_MATMUL 特征位）把这些场景路由到 `op_kernel` 下按变体拆分的实现文件。
- tiling 的工业级要素：编译期 `FFNCompileInfo` 缓存平台资源、运行期按 shape 推导、Cube baseM 与 Vector baseM 整数倍对齐、workspace 精确报价、空输入短路（bs=0 直接 tilingKey=0）。
- ffn 域采用「主算子 + 场景变体」组织：swin 系变体砍掉通用性（固定 shape、仅 fp16、无 aclnn 层、tiling 写常量、重心在 op_graph 的 proto/fusion_pass）；ffn_worker_scheduler 则用 AICPU 载体做配套数据整理。
- 与 attention 域对比：五层目录范式相同，差异在重心——通用算子在 op_host/op_api，场景变体在 op_graph，attention 大算子在多 SoC 的 op_kernel。

## 7. 下一步学习建议

- **下一讲 u5-l3（mc2 通信计算融合）**：FFN/MoE 之后的前向还差一步跨卡通信——matmul_all_reduce 如何把 AllReduce 折进矩阵乘，与本讲的「计算融合」构成「通信+计算融合」的对照阅读。
- 继续阅读源码：`ffn/ffn/op_kernel/ffn.cpp`（device 侧总入口，验证 tiling key 分发）、`ffn/ffn/op_host/op_api/aclnn_ffn.cpp`（v1/v2/v3 三版本入口如何共用实现，对照 u4-l2 的 FA base 模式）。
- 若对 MoE 全链路感兴趣，可回读 u5-l1 的 moe_token_permute 与本讲 expertTokens 的衔接点，并预习 u5-l4 的分布式 dispatch/combine。
