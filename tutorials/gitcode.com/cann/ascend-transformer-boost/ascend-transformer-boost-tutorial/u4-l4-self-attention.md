# Self-Attention 融合算子

## 1. 本讲目标

Self-Attention（自注意力）是 Transformer 的计算核心，也是 ATB 里最复杂、分支最多的一个算子。本讲学完后你应当能够：

- 说清 `SelfAttentionParam` 用「字段组合」覆盖了多少种注意力形态，并能指出 `calcType`、`kvcacheCfg`、`maskType`、`inputLayout` 这四把「开关」各自控制什么。
- 读懂 `SelfAttentionOperation` 的校验链与 `InferShapeImpl` 输出形状推导规则。
- 画出 `CreateRunner` 的决策树，解释 `self_attention/` 目录下为何会出现 fusion / bypass / encoder / prefix / aclnn 这么多 Runner，以及哪些是 910A 专用。
- 说明 `InputLayout` 的 `BSND` 与 `BNSD` 两种排布在功能约束与 KV Cache 格式上的差异。
- 理解 950 芯片走 `SelfAttentionAclnnRunner` 桥接 CANN `aclnnFusedInferAttentionScoreV5` 的适配套路（承接 u3-l3）。

本讲承接 u3-l1（OperationBase 骨架）、u3-l2（Runner 体系）、u3-l3（AclnnRunner）与 u4-l1（LinearParam 的「单 Param 覆盖 N 种行为」设计哲学），是它们在注意力场景下的集中体现。

## 2. 前置知识

在进入源码前，先用最直白的话复习几个概念。

**缩放点积注意力（Scaled Dot-Product Attention）。** 给定 query \(Q\)、key \(K\)、value \(V\)，先算 \(Q\) 与 \(K\) 的点积衡量「相关度」，除以缩放因子防止数值过大，再做 softmax 得到注意力权重，最后用权重对 \(V\) 加权求和：

\[
\mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^{\mathsf{T}}}{\sqrt{d_k}}\right)V
\]

ATB 里缩放因子由 `qkScale` 字段提供（默认为 1，对应 \(\frac{1}{\sqrt{d_k}}\) 已由上层折算进权重或由 kernel 内部处理）。

**多头注意力（MHA / GQA）。** 把隐藏维拆成 `headNum` 个头，每个头独立做注意力。GQA（分组查询注意力）允许 KV 的头数 `kvHeadNum` 小于 query 头数，多个 query 头共享一组 KV。`kvHeadNum = 0` 表示与 `headNum` 一致（纯 MHA）。

**Prefill 与 Decode。** 大模型推理分两阶段：prefill 一次性处理整段输入 prompt（query 序列长），decode 每次只生成一个新 token（query 序列长 1，但要从历史 KV Cache 里读很长的 K/V）。ATB 用 `calcType` 区分这两类以及 paged 场景。

**Flash Attention / Paged Attention。** Flash Attention 通过分块（tiling）把中间的 \(N\times N\) 注意力矩阵留在片上，避免读写 HBM，是「prefill / 短序列 decode」的主力。Paged Attention 把 KV Cache 按 block 组织（类似操作系统的分页），用 block table 索引，是「长序列 decode」的主力。ATB 的 `SelfAttention` 把两条路线统一在一个算子里。

**KV Cache 写入。** decode 时新 token 的 K/V 要追加进缓存。ATB 把「写入缓存」做成了 KVCache kernel 节点，与 FA 节点拼在同一张 `KernelGraph` 里——这就是后面会反复出现的「fusion（含 KVCache 节点）」与「bypass（直接传 KV，跳过写入节点）」之分。

## 3. 本讲源码地图

本讲涉及的文件都集中在 `src/ops/ops_infer/self_attention/`，外加公共参数头。

| 文件 | 作用 |
| --- | --- |
| [infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h) | `SelfAttentionParam` 结构与 `InputLayout` 等公共枚举的权威定义。 |
| [self_attention_operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.h) | `SelfAttentionOperation` 类声明，列出它重写的钩子与一堆私有校验函数。 |
| [self_attention_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp) | 本讲主战场：`CreateOperation` 校验、`InferShapeImpl`、`GetInputNum/GetOutputNum`、`CreateRunner` 决策树。 |
| [self_attention_aclnn_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.h) / [.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp) | 950 芯片走的 aclnn 适配 Runner。 |
| 各 `*_ops_runner*.h` | fusion / bypass / encoder / prefix 等 OpsRunner 子类声明，是 910B/910A/310P 的执行后端。 |

---

## 4. 核心概念与源码讲解

### 4.1 SelfAttentionParam：四个枚举开关决定算子形态

#### 4.1.1 概念说明

和 u4-l1 的 `LinearParam` 一样，ATB 没有把「encoder 注意力」「decoder 注意力」「paged 注意力」「prefix 注意力」拆成四个算子，而是用**一个** [`SelfAttentionParam`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1704-L1851) 靠字段组合表达。理解这个算子的第一步，就是把它的字段分成两类：

- **四个「形态开关」枚举**：决定算子长什么样、走哪条 Runner、有几个输入。
- **一组「数值/修饰」字段**：`headNum`、`kvHeadNum`、`qkScale`、`quantType`、`scaleType`、`kernelType`、`mlaVHeadSize`、`windowSize` 等，修饰具体行为。

末尾照例有 `uint8_t rsv[64]` 预留字段充当版本闸门（见 u2-l3）。

#### 4.1.2 核心流程

四个形态开关的取值与含义如下（这是本讲最重要的一张表）：

| 开关字段 | 取值 | 含义 / 影响路径 |
| --- | --- | --- |
| `calcType` | `UNDEFINED` / `ENCODER` / `DECODER` | Flash Attention 路径，prefill（encoder）或 decode |
| | `PA_ENCODER` | **Paged Attention** 路径（带 block table） |
| | `PREFIX_ENCODER` | Prefix 编码器（仅 910B） |
| `kvcacheCfg` | `K_CACHE_V_CACHE` | 由算子内部把新 K/V 写入 cache（带 KVCache 节点） |
| | `K_BYPASS_V_BYPASS` | 直接把 cache 当输入传进来（bypass 写入） |
| `maskType` | `MASK_TYPE_UNDEFINED` 等 9 种 | 决定是否多一个 mask 输入、用何种 mask kernel |
| `inputLayout` | `TYPE_BSND` / `TYPE_BNSD` | 数据排布，决定 KV Cache 格式与可用功能 |

`calcType` 与 `kvcacheCfg` 的合法组合是受约束的：例如 `PA_ENCODER` 不允许 `K_BYPASS_V_BYPASS`（paged 必须有 cache）。

#### 4.1.3 源码精读

枚举定义都集中在 [`SelfAttentionParam`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1704-L1851) 内部。`CalcType` 明确区分了 flash 与 paged：

```cpp
enum CalcType : int {
    UNDEFINED = 0,  // decoder&encoder for flashAttention
    ENCODER,        // encoder for flashAttention
    DECODER,        // decoder for flashAttention
    PA_ENCODER,     // encoder for pagedAttention
    PREFIX_ENCODER, // prefix encoder for flashAttention
};
```

> 见 [infer_op_params.h:L1710-L1716](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1710-L1716)，中文注释点明了 flash / paged 之分。

`KvCacheCfg` 区分是否由算子写 cache：

```cpp
enum KvCacheCfg : int {
    K_CACHE_V_CACHE = 0, // 进行 kvcache 处理
    K_BYPASS_V_BYPASS,   // 直接传入 kvcache
};
```

> 见 [infer_op_params.h:L1758-L1761](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1758-L1761)。这就是后文 Runner 名字里 "Fusion"（含 KVCache 写入节点）与 "FusionBypass"（bypass 写入）的由来。

`MaskType` 一共 9 种，覆盖倒三角、ALiBi、压缩 mask、滑动窗口等：

> 见 [infer_op_params.h:L1741-L1752](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1741-L1752)。每种 mask 会在 `GetInputNum` 里决定是否多一个 mask / slopes 输入张量。

`InputLayout` 是与 `LinearParam` 等共享的公共枚举，只有两个值：

```cpp
enum InputLayout : int {
    TYPE_BSND = 0, // 数据排布为 BSND
    TYPE_BNSD      // 数据排布为 BNSD
};
```

> 见 [infer_op_params.h:L39-L42](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L39-L42)。

值得专门一提的还有几个修饰字段：`kvHeadNum`（GQA 支持，0 表示退化为 MHA）、`quantType`（QKV INT8 在线/离线量化，仅 910B + `PA_ENCODER`）、`scaleType`（`SCALE_TYPE_LOGN` 长序列外推缩放）、`mlaVHeadSize`（>0 时开启 MLA 合并 KV，承接 u4-l7）、`windowSize`（>0 开启 SWA 滑动窗口）。

#### 4.1.4 代码实践

**实践目标**：建立「改一个枚举 → 算子变一种形态」的直觉。

1. 打开 [infer_op_params.h:L1704-L1851](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1704-L1851) 的 `SelfAttentionParam`。
2. 假设你要为「decode 阶段、GQA、带倒三角 mask、BSND」配置参数，写下这四个字段应取什么值。
3. 再为「prefill 阶段、Paged Attention、ALiBi mask、BSND」写一组值。
4. 对比两组，体会「同一个结构、不同字段组合 = 不同执行路径」。

**需要观察的现象**：你会发现只要改 `calcType` / `kvcacheCfg` / `maskType` / `inputLayout` 这四个字段，下游的合法性校验、输入个数、Runner 选择都会随之改变——这正是 4.2、4.3 要讲的内容。

**预期结果**：能说出第一组用 `calcType=DECODER, kvcacheCfg=K_CACHE_V_CACHE, maskType=MASK_TYPE_NORM, inputLayout=TYPE_BSND`；第二组用 `calcType=PA_ENCODER, maskType=MASK_TYPE_ALIBI, inputLayout=TYPE_BSND`。

#### 4.1.5 小练习与答案

**练习 1**：`kvHeadNum = 0` 和 `kvHeadNum = headNum / 2` 分别代表什么？

> **答案**：`0` 表示 KV 头数与 query 头数相同，即标准 MHA；`headNum / 2` 表示每 2 个 query 头共享 1 组 KV，即 GQA（分组查询注意力）。

**练习 2**：`K_BYPASS_V_BYPASS` 字面是「绕过」，它绕过的是哪一步？

> **答案**：绕过「把新 token 的 K/V 写入 KV Cache」这一步——cache 由外部直接作为输入张量传入，算子内部不再插 KVCache 写入节点。

---

### 4.2 SelfAttentionOperation：校验链与形状推导

#### 4.2.1 概念说明

[`SelfAttentionOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.h#L25-L86) 继承 `OperationBase`（u3-l1），只重写必要钩子：`InferShapeImpl`、`CreateRunner`，以及 `GetInputNum` / `GetOutputNum` 与两个 check 钩子。它的特点是**校验极重**——因为一个 Param 覆盖的形态太多，必须在创建期把「字段组合是否合法」「是否被当前芯片支持」一次性卡死，避免把不支持的组合带到 Device 端才崩。

校验分两层：工厂函数 `CreateOperation` 做**跨字段、跨芯片**的静态约束检查；`InferShapeCheckImpl` / `SetupCheckImpl` 做**张量维度**的动态检查。

#### 4.2.2 核心流程

`CreateOperation` 的校验流程（伪代码）：

```
CreateOperation(param):
    OP_PARAM_RSV_CHECK(param)          # rsv 版本闸门，非 0 即拒
    HeadNumCheck                       # headNum>0、kvHeadNum 合法、整除关系
    kvcacheCfg ∈ {K_CACHE_V_CACHE, K_BYPASS_V_BYPASS} ?
    quantType != TYPE_DEQUANT_FUSION ?
    if 芯片 == 950:
        SelfAttentionAclnnRunner::LoadMethod()   # dlopen 加载 aclnn 符号
        ParamCheck950                             # 950 能力子集
    QKV 量化相关约束
    BNSDParamCheck / MlaParamCheck / SWAParamCheck
    DeviceParamCheck                  # 芯片能力（910A 限制最多）
    KernelTypeRangeCheck / InputLayoutRangeCheck
    PrefixEncoderParamCheck（若 PREFIX_ENCODER）
    new SelfAttentionOperation(param)
```

形状推导 `InferShapeImpl` 的主线：注意力输出形状 = query 的 batch/序列维 ×（`headNum × vHeadSize`）。注意头与头维会被**合轴**——输出最后一维是 `headNum * vHeadSize`，而不是保留头维。

#### 4.2.3 源码精读

工厂模板 [`CreateOperation<SelfAttentionParam>`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L62-L158) 一上来就是一连串校验，体现了「形态越多、闸门越严」的设计：

```cpp
OP_PARAM_RSV_CHECK(opParam);            // rsv 版本闸门
if (!HeadNumCheck(opParam)) return ERROR_INVALID_PARAM;
if (opParam.kvcacheCfg != ...K_BYPASS_V_BYPASS &&
    opParam.kvcacheCfg != ...K_CACHE_V_CACHE) { ... return ERROR_INVALID_PARAM; }
if (Mki::PlatformInfo::Instance().GetPlatformType() == Mki::PlatformType::ASCEND_950) {
    Status status = SelfAttentionAclnnRunner::LoadMethod();   // 950 预加载 aclnn
    if (!ParamCheck950(opParam)) return ERROR_INVALID_PARAM;
}
// ...BNSDParamCheck / MlaParamCheck / SWAParamCheck / DeviceParamCheck ...
*operation = new (std::nothrow) SelfAttentionOperation(opParam);
```

> 见 [self_attention_operation.cpp:L62-L158](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L62-L158)。

芯片能力校验的典型例子是 `DeviceParamCheck`，它对 910A（Atlas 800）限制最严——只支持 `PA_ENCODER`、不支持 ALiBi 压缩 mask、不支持滑动窗口、不支持 logN：

```cpp
if (GetSingleton<Config>().Is910A()) {
    if (opParam.calcType != ...PA_ENCODER) { ... "Atlas 800 product only supports PA ENCODER"; }
    if (opParam.windowSize > 0)            { ... "does not support sliding window attention"; }
    if (opParam.scaleType == ...SCALE_TYPE_LOGN) { ... "does not support logN"; }
}
```

> 见 [self_attention_operation.cpp:L261-L280](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L261-L280)。这段直接解释了「为什么 910A 需要 [910a 适配]」——它的能力子集小，必须用专门的 Runner 与 kernel。

950 的能力子集由 [`ParamCheck950`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L390-L443) 单独约束：只能非量化、只能 `PA_ENCODER`、只能 `K_CACHE_V_CACHE`、只能 `SCALE_TYPE_TOR`、不开 SWA / MLA、只支持 `BSND`、只支持 `CACHE_TYPE_NORM`、mask 限 `undefined/alibi/norm/normCompress`。

输入个数 [`GetInputNum`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L741-L792) 完全由字段组合决定，是「VariantPack 该装几个张量」的依据。先看顶部常量：

```cpp
static constexpr uint32_t FUSION_IN_TENSOR_NUM = 8;        // K_CACHE_V_CACHE 基线
static constexpr uint32_t FUSION_BYPASS_IN_TENSOR_NUM = 6; // K_BYPASS_V_BYPASS 基线（少 q/k/v 写入相关）
```

> 见 [self_attention_operation.cpp:L36-L42](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L36-L42)。

`PA_ENCODER` 分支按 mask 类型给 4 / 5 / 6 个基线输入，再按量化、MLA 增减；`DECODER/ENCODER` 分支以 6 或 8 为基线，再按 batch / mask / slopes / logN 递增。例如开 ALiBi 压缩 mask 会 `+1`（多一个 slopes），开 logN 会 `+1`（多一个 scale）。

输出个数 [`GetOutputNum`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L794-L801) 始终返回 1（即注意力输出 `attentionOut`）。

形状推导 [`InferShapeImpl`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L881-L902) 按芯片分流，910B/950 走更完整的 [`InferShapeImpl910B`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L831-L879)。其核心规则可概括为：**输出 = query 的前两维（batch、seq） × （headNum × vHeadSize）合轴**：

```cpp
if (inTensorDescs.at(0).shape.dimNum == 4) {            // q: [B, S, N, D]
    outTensorDescs.at(0).shape.dimNum = 3;              // out: [B, S, N*D]
    outTensorDescs.at(0).shape.dims[0] = ...dims[0];    // B
    outTensorDescs.at(0).shape.dims[1] = ...dims[1];    // S
    outTensorDescs.at(0).shape.dims[2] = ...dims[2] * vHiddenSize;  // N * vHeadSize
}
```

> 见 [self_attention_operation.cpp:L845-L854](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L845-L854)。MLA 场景下 `vHiddenSize` 取 `mlaVHeadSize`；QKV 量化场景下还会把输出 dtype 改成 `param_.outDataType`（见 [L874-L877](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L874-L877)）。

#### 4.2.4 代码实践

**实践目标**：用源码反推「一组配置下算子需要几个输入、输出长什么样」。

1. 选定配置：`calcType = DECODER`，`kvcacheCfg = K_CACHE_V_CACHE`，`maskType = MASK_TYPE_NORM`，`batchRunStatusEnable = false`，`scaleType = SCALE_TYPE_TOR`，ALiBi 压缩关闭。
2. 跟着 [`GetInputNum`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L741-L792) 末尾的 `DECODER/ENCODER` 分支手算：基线 `FUSION_IN_TENSOR_NUM = 8`，`maskType != UNDEFINED` 所以 `+1`。
3. 再跟 [`InferShapeImpl910B`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L831-L879) 推输出：若 query 是 4 维 `[B,S,N,D]`，输出为 3 维 `[B,S,N×vHeadSize]`。

**需要观察的现象**：输入个数随 mask / quant / logN 增减；输出始终把头维合轴进最后一维。

**预期结果**：上述配置输入数 = 9（8 基线 + 1 mask），输出 1 个、形状 `[B, S, headNum × vHeadSize]`。**待本地验证**：若你在 910B 上实际构造该 Param 并调用 `GetInputNum`，应得到 9。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CreateOperation` 要在 `new` 算子对象之前做这么多 `return ERROR_INVALID_PARAM`？

> **答案**：因为一个 Param 覆盖的合法形态非常多，但每种芯片只支持其中一个子集。把不合法组合挡在创建期，能给出精确错误信息，避免到 Setup 甚至 Device 端 kernel 才崩溃、难以定位。

**练习 2**：`InferShapeImpl910B` 里 MLA 场景的 `vHiddenSize` 为什么取 `param_.mlaVHeadSize` 而不是 value 张量的最后一维？

> **答案**：MLA 把 KV 合并压缩了，实际参与注意力输出的 V 维度由 `mlaVHeadSize` 指定（详见 u4-l7），与传入 value 张量的物理维度不一定相同，因此推导输出形状时以 Param 字段为准。

---

### 4.3 CreateRunner 决策树：九大 OpsRunner 的分派

#### 4.3.1 概念说明

这是本讲的核心。`SelfAttentionOperation` 不直接 launch kernel（见 u3-l1），而是通过 `CreateRunner` 在首次 Setup 时延迟创建一个 Runner。问题是：`self_attention/` 目录下足足有 **9 个 OpsRunner 子类 + 1 个 AclnnRunner**。为什么这么多？

原因是两个正交的变化维：

1. **算子形态维**：`calcType` × `kvcacheCfg` × `inputLayout` 决定组图方式不同（是否带 KVCache 节点、是否 paged、是否 BNSD），需要不同的 OpsRunner。
2. **芯片维**：910B（A2）、910A（A800）、310P、950（A3）的 kernel 实现与能力差异大，910A/310P 还要用 `RunnerPool` 复用对象，于是多了「910A 后缀」一整套。

命名约定非常规整，记住这组词缀就能「望文生义」：

| 名字片段 | 含义 |
| --- | --- |
| `Fusion` | `K_CACHE_V_CACHE` 路径，组图里**含 KVCache 写入节点**，再接 UnpadFlashAttention |
| `FusionBypass` | `K_BYPASS_V_BYPASS` 路径，KV cache 作为 tensor list 直接喂给 FA，**不含写入节点** |
| `Encoder` | `PA_ENCODER`（Paged Attention）路径 |
| `PrefixEncoder` | `PREFIX_ENCODER` 路径（仅 910B） |
| `BNSD`（后缀） | `inputLayout = TYPE_BNSD` 的排布变体 |
| `910A`（后缀） | 910A / 310P 芯片专用变体 |

#### 4.3.2 核心流程

[`CreateRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2086-L2143) 的决策树（自顶向下）：

```
CreateRunner(context):
  if 芯片 == ASCEND_950:
      return SelfAttentionAclnnRunner                    # 走 CANN aclnn，唯一例外
  if Is910B:                                              # 910B 直接 make_shared
      PA_ENCODER        -> SelfAttentionEncoderFusionOpsRunner
      PREFIX_ENCODER    -> SelfAttentionPrefixEncoderOpsRunner
      K_BYPASS_V_BYPASS -> BNSD ? FusionBypassOpsRunnerBNSD : FusionBypassOpsRunner
      else              -> SelfAttentionFusionOpsRunner
  else:                                                   # 910A / 310P，走 RunnerPool 复用
      PA_ENCODER        -> SelfAttentionEncoderFusionOpsRunner910A
      K_BYPASS_V_BYPASS -> BNSD ? FusionBypassOpsRunnerBNSD910A : FusionBypassOpsRunner910A
      else              -> SelfAttentionFusionOpsRunner910A
```

注意 910B 分支与 910A 分支**结构完全对称**，只是后者多了 `910A` 后缀、并且改用 `RunnerPool::MallocRunner` 复用对象。

#### 4.3.3 源码精读

950 是第一个特判——无论 `calcType` 一律走 aclnn：

```cpp
if (Mki::PlatformInfo::Instance().GetPlatformType() == Mki::PlatformType::ASCEND_950) {
    return std::make_shared<SelfAttentionAclnnRunner>(param_);
}
```

> 见 [self_attention_operation.cpp:L2093-L2096](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2093-L2096)。这正是 u3-l3 讲过的「同一算子存在 aclnn / ops 多条后端」——950（A3）用 CANN 官方融合算子，其余芯片用自家 KernelGraph。

910B 分支用 `std::make_shared` 直接新建：

```cpp
if (GetSingleton<Config>().Is910B()) {
    if (param_.calcType == ...PA_ENCODER) {
        return std::make_shared<SelfAttentionEncoderFusionOpsRunner>(param_);
    } else if (param_.calcType == ...PREFIX_ENCODER) {
        return std::make_shared<SelfAttentionPrefixEncoderOpsRunner>(param_);
    } else if (param_.kvcacheCfg == ...K_BYPASS_V_BYPASS) {
        if (param_.inputLayout == ...TYPE_BNSD)
            return std::make_shared<SelfAttentionFusionBypassOpsRunnerBNSD>(param_);
        else
            return std::make_shared<SelfAttentionFusionBypassOpsRunner>(param_);
    } else {
        return std::make_shared<SelfAttentionFusionOpsRunner>(param_);
    }
}
```

> 见 [self_attention_operation.cpp:L2097-L2110](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2097-L2110)。

910A/310P 分支结构对称，但换成 `RunnerPool` 复用（承接 u3-l5 的对象池机制）：

```cpp
int64_t runnerTypeIdx = RunnerTypeRegister::GetRunnerTypeIdx("SelfAttentionEncoderFusionOpsRunner910A");
RunnerPool &pool = contextBase->GetRunnerPool(runnerTypeIdx);
Runner *runner = pool.MallocRunner<SelfAttentionEncoderFusionOpsRunner910A, infer::SelfAttentionParam>(param_);
return runner ? std::shared_ptr<Runner>(runner, [&pool](Runner *r) { pool.FreeRunner(r); })
              : std::make_shared<SelfAttentionEncoderFusionOpsRunner910A>(param_);
```

> 见 [self_attention_operation.cpp:L2112-L2141](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2112-L2141)。`MallocRunner` 命中池就复用（只 `SetParam` 换参数），否则才新建；`shared_ptr` 的自定义删除器在引用归零时调 `FreeRunner` 归还。这样把 KernelGraph 等昂贵构造摊薄。

每个 OpsRunner 子类都用「重写 `ModifyKernelGraph`」来拼自己的 `KernelGraph`（u3-l2）。例如 `Fusion` 路径带 KVCache 节点，`FusionBypass` 路径把 cache 当 tensor list 直接喂 FA：

```cpp
// Fusion 路径：含 KVCache 写入节点
class SelfAttentionFusionOpsRunner : public OpsRunner {
    Status ModifyKernelGraph(...) override;
    void SetKVCacheParam(...);
    bool ModifyKVCacheNode(...);          // 有「修改 KVCache 节点」的职责
};
// FusionBypass 路径：KV cache 直接以 tensor list 传入 FA
class SelfAttentionFusionBypassOpsRunner : public OpsRunner {
    Status SetKVCacheTensorList(...);     // 把 cache 当输入列表，无写入节点
};
```

> Fusion 见 [self_attention_fusion_ops_runner.h:L20-L36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_ops_runner.h#L20-L36)；FusionBypass 见 [self_attention_fusion_bypass_ops_runner.h:L19-L35](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_bypass_ops_runner.h#L19-L35)。两者差异正是「是否插入 KVCache 写入节点」。

完整的 10 个 Runner 一览（本讲实践的产出）：

| Runner 类 | 适用场景 / 芯片 | 声明位置 |
| --- | --- | --- |
| `SelfAttentionFusionOpsRunner` | 910B，flash + K_CACHE_V_CACHE（decoder/encoder 写 cache） | [fusion_ops_runner.h:L20](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_ops_runner.h#L20) |
| `SelfAttentionFusionBypassOpsRunner` | 910B，flash + K_BYPASS_V_BYPASS，BSND | [fusion_bypass_ops_runner.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_bypass_ops_runner.h#L19) |
| `SelfAttentionFusionBypassOpsRunnerBNSD` | 910B，flash + bypass，BNSD | [fusion_bypass_ops_runner_BNSD.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_bypass_ops_runner_BNSD.h#L19) |
| `SelfAttentionEncoderFusionOpsRunner` | 910B，PA_ENCODER（paged） | [encoder_fusion_ops_runner.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_encoder_fusion_ops_runner.h#L19) |
| `SelfAttentionPrefixEncoderOpsRunner` | 910B，PREFIX_ENCODER | [prefix_encoder_ops_runner.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_prefix_encoder_ops_runner.h#L19) |
| `SelfAttentionFusionOpsRunner910A` | 910A/310P，flash + K_CACHE_V_CACHE | [fusion_ops_runner_910a.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_ops_runner_910a.h#L19) |
| `SelfAttentionFusionBypassOpsRunner910A` | 910A/310P，flash + bypass，BSND | [fusion_bypass_ops_runner_910a.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_bypass_ops_runner_910a.h#L19) |
| `SelfAttentionFusionBypassOpsRunnerBNSD910A` | 910A/310P，flash + bypass，BNSD | [fusion_bypass_ops_runner_BNSD_910a.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_fusion_bypass_ops_runner_BNSD_910a.h#L19) |
| `SelfAttentionEncoderFusionOpsRunner910A` | 910A/310P，PA_ENCODER | [encoder_fusion_ops_runner_910a.h:L19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_encoder_fusion_ops_runner_910a.h#L19) |
| `SelfAttentionAclnnRunner` | 950（A3），桥接 `aclnnFusedInferAttentionScoreV5` | [aclnn_runner.h:L59](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.h#L59) |

注意：`PrefixEncoder` 没有 910A 变体（因为 prefix encoder 仅 910B 支持）；`Encoder` 与 `PrefixEncoder` 没有 BNSD 变体（paged 路径只走 BSND）。

#### 4.3.4 代码实践

**实践目标**：本讲的指定实践——列出 `self_attention/` 目录下的 Runner 种类并说明适用场景。

1. 在仓库根目录执行（只读检索，不修改源码）：

   ```bash
   ls src/ops/ops_infer/self_attention/*_runner*.h
   ```

2. 对照上表，把每个头文件里的 `class XXXRunner : public OpsRunner`（或 `: public AclnnRunner`）摘出来。
3. 打开 [`CreateRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2086-L2143)，为每个 Runner 标注它在决策树的哪个分支被选中、对应的 `calcType / kvcacheCfg / inputLayout / 芯片` 条件。
4. 回答：为什么 910A 分支用 `RunnerPool` 而 910B 分支用 `make_shared`？

**需要观察的现象**：9 个 OpsRunner + 1 个 AclnnRunner；910B 与 910A 两套对称命名；BNSD 只在 `FusionBypass` 路径出现。

**预期结果**：得到与上表一致的分类，并能解释「910A/310P 资源更紧张、FA kernel 图构造昂贵，故用 RunnerPool 复用对象；910B 算力充裕、且 kernel 路径不同，直接新建更简单」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SelfAttentionPrefixEncoderOpsRunner` 没有 `910A` 版本？

> **答案**：`DeviceParamCheck` 与 `PrefixEncoderParamCheck` 都限定 prefix encoder 仅 910B（A2/A3）支持，910A 根本不会走到 `PREFIX_ENCODER` 分支，自然无需 910A 变体。

**练习 2**：`Fusion` 与 `FusionBypass` 的「Fusion」都指融合，那它俩到底融合了什么、区别在哪？

> **答案**：二者都把「KV 处理 + Flash Attention」融合进一张 `KernelGraph`。区别在于 `Fusion`（K_CACHE_V_CACHE）多融合了一个 **KVCache 写入节点**（把新 token 的 K/V 写进 cache 再读出做注意力）；`FusionBypass`（K_BYPASS_V_BYPASS）跳过写入，直接把外部 cache 作为 tensor list 喂给 FA。

---

### 4.4 AclnnRunner 适配与 BSND/BNSD 排布

#### 4.4.1 概念说明

本模块讲两件收尾的事。

**第一**，950 专用的 [`SelfAttentionAclnnRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.h#L59) 如何把 ATB 的 Tensor 适配成 CANN aclnn 的接口。它是 u3-l3 `AclnnRunner` 模式的典型实例：实现三个纯虚钩子 `BuildAclnnVariantPack` / `SetAclNNWorkspaceExecutor` / `LaunchAclnnKernel`，对应 aclnn 两段式协议（先 `GetWorkspaceSize` 产 `aclOpExecutor`，再 `Execute` 下发）。

**第二**，`BSND` 与 `BNSD` 两种排布的差异。`B`=batch、`S`=sequence、`N`=headNum、`D`=headSize：

- `BSND`：`[B, S, N, D]`，常合并为 `[B*S, N, D]`（unpadded，序列维连续）。是 ATB 默认与最通用的排布。
- `BNSD`：`[B, N, S, D]`，头维在外、序列维在内，是某些 FA kernel（如 910A BNSD kernel、310P m8v2）偏爱的排布。

#### 4.4.2 核心流程

`SelfAttentionAclnnRunner` 的执行链（与 u3-l3 一致）：

```
BuildAclnnVariantPack:    # atb::Tensor -> aclTensor / aclTensorList
    识别 isDecoder（q 第 0 维 == batch → decode，每 batch q 序列长 1）
    建 query / key(tensor list) / value(tensor list)
    按 maskType 建 pseShift(ALiBi) 或 attenMask(norm)
    建 actualSeqLengths（Int Array，TND 布局下做累加）
    建 attentionOut
SetAclNNWorkspaceExecutor: # 调 aclnnFusedInferAttentionScoreV5GetWorkspaceSize
    传入 numHeads、scaleValue、inputLayout 串、numKeyValueHeads 等
    大量可选参数传 nullptr（950 路径不用量化/blockTable/antiquant）
    产出 aclOpExecutor + workspaceSize
LaunchAclnnKernel:         # 调 aclnnFusedInferAttentionScoreV5(workspace, executor, stream)
```

排布适配的关键映射：ATB 的 `inputLayout` 要翻译成 aclnn 认的字符串。

```
TYPE_BSND -> "TND"   # aclnn 的 unpadded token 布局（B*S 合并为 T）
TYPE_BNSD -> "BNSD"
ALiBi mask 特例 -> "BSND"（4D 视图，CreateQueryAclnnTensor 内重排）
```

#### 4.4.3 源码精读

[`LoadMethod`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L49-L60) 在创建期就被 `CreateOperation` 调用，用 `dlopen` 把两个 aclnn 符号地址缓存到静态成员：

```cpp
return LoadFromSharedObjectFile("aclnnFusedInferAttentionScoreV5GetWorkspaceSize",
    "aclnnFusedInferAttentionScoreV5",
    aclnnFusedInferAttentionScoreV5GetWorkspaceSizeFunc_,
    aclnnFusedInferAttentionScoreV5Func_);
```

> 见 [self_attention_aclnn_runner.cpp:L49-L60](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L49-L60)。

[`BuildAclnnVariantPack`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L62-L125) 负责把 atb 张量序列转成 aclnn 的张量与张量列表，并按 `maskType` 决定附加哪个 mask 张量：

```cpp
switch (param_.maskType) {
    case MASK_TYPE_ALIBI:        CreatePseShiftAclnnTensor();   break;
    case MASK_TYPE_NORM:
    case MASK_TYPE_NORM_COMPRESS: CreateAttenMaskAclnnTensor(); break;
    default: break;
}
```

> 见 [self_attention_aclnn_runner.cpp:L93-L114](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L93-L114)。

排布映射在 [`InitAclnnParam`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L256-L269)，注意 `BSND` 映射成 `"TND"`：

```cpp
if (param_.inputLayout == infer::TYPE_BSND)      aclnnParam_.inputLayoutStr = "TND";
else if (param_.inputLayout == infer::TYPE_BNSD) aclnnParam_.inputLayoutStr = "BNSD";
aclnnParam_.numHeads = param_.headNum;
aclnnParam_.scaleValue = param_.qkScale;
aclnnParam_.numKeyValueHeads = param_.kvHeadNum == 0 ? param_.headNum : param_.kvHeadNum;
```

`SetAclNNWorkspaceExecutor` 把绝大多数可选参数传 `nullptr`，因为 950 路径由 `ParamCheck950` 限定为「非量化、无 blockTable、无 antiquant」：

> 见 [self_attention_aclnn_runner.cpp:L157-L189](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L157-L189)，注意 `deqScale1/quantScale1/blockTable/antiquantScale/...` 全为 `nullptr`，只有 `numHeads/scaleValue/inputLayout/numKeyValueHeads/sparseMode/...` 是实参。最后由 [`LaunchAclnnKernel`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L203-L223) 在 stream 上真正下发。Runner 用 [`REG_RUNNER_TYPE(SelfAttentionAclnnRunner)`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L585) 注册进 RunnerPool 分桶（u3-l5）。

BSND 与 BNSD 的功能差异则集中在 [`BNSDParamCheck`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L177-L231)（位于 operation.cpp）：BNSD **不能**与 `scaleType != TOR`、量化、prefix encoder 共存，`PA_ENCODER` BNSD 还不支持高精度 kernel 与 MLA；310P 上 BNSD 的 `PA_ENCODER` 仅支持 `KERNELTYPE_EXP_M8V2`：

```cpp
if (opParam.inputLayout == atb::infer::InputLayout::TYPE_BNSD) {
    if (opParam.scaleType != ...SCALE_TYPE_TOR) { ... "BNSD feature and scaleType feature cannot coexist"; }
    if (opParam.quantType != ...TYPE_QUANT_UNQUANT) { ... "BNSD feature and quantType feature cannot coexist"; }
    if (opParam.calcType == ...PREFIX_ENCODER) { ... "BNSD feature does not support prefix encoder"; }
    ...
}
```

> 见 [self_attention_operation.cpp:L177-L231](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L177-L231)。这说明 BNSD 是「为特定 kernel 牺牲通用性」的排布，BSND 才是功能最全的通用排布。

此外，KV Cache 的内存格式也随排布与芯片不同：910B/950 上 cache 用 `ACL_FORMAT_ND`，910A/310P 上用 `ACL_FORMAT_FRACTAL_NZ`（5 维分形），由 [`DtypeCheck`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L904-L921) 校验。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 950 上的 aclnn 适配，并对比两种排布的约束。

1. 从 [`CreateRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2086-L2143) 的 950 分支出发，依次跳读 [`LoadMethod`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L49-L60) → [`InitAclnnParam`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L256-L269) → [`SetAclNNWorkspaceExecutor`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L127-L201) → [`LaunchAclnnKernel`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_aclnn_runner.cpp#L203-L223)，画出「atb 张量 → aclTensor → aclOpExecutor → stream 下发」的链路。
2. 打开 [`BNSDParamCheck`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L177-L231)，列出 BNSD 相比 BSND 缺失的能力（logN、量化、prefix、高精度 kernel、MLA…）。

**需要观察的现象**：aclnn 适配层大量参数为 `nullptr`，对应 950 能力子集；BNSD 的限制明显多于 BSND。

**预期结果**：能复述 aclnn 两段式协议在本算子的落地，并能解释「BSND 通用、BNSD 受限」的原因。**待本地验证**：若在 950 上以 `inputLayout=TYPE_BNSD` 构造 `SelfAttentionParam`，`InitAclnnParam` 会把 `inputLayoutStr` 设为 `"BNSD"`，但 `ParamCheck950` 实际只允许 BSND——会在 `CreateOperation` 阶段就被拒绝。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BSND` 在 aclnn 适配里被翻译成 `"TND"` 而不是 `"BSND"`？

> **答案**：ATB 的 BSND 常把 batch 与序列维合并成 `[B*S, N, D]`（unpadded，变长序列紧凑存放），这在 aclnn 的语义里是「Token」维 `T`，故映射为 `"TND"`；`actualSeqLengths` 数组用来记录每个 batch 的真实长度。

**练习 2**：同样是 Self-Attention，910B 用 OpsRunner、950 用 AclnnRunner，本质原因是什么？

> **答案**：950（A3）的 CANN 提供了高性能的官方融合算子 `aclnnFusedInferAttentionScoreV5`，直接桥接性价比最高；而 910B（A2）用 ATB 自家维护的 KernelGraph（UnpadFlashAttention + KVCache）更能针对其芯片微架构与功能（量化、MLA、SWA 等）做深度优化。这是「同一算子按芯片选不同后端」的策略，与 u3-l3 Linear 在 950 走 aclnn 的逻辑同构。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「配置 → 校验 → 选 Runner → 适配」的完整推理。

**任务**：假设你在一台 Atlas 800I A2（910B）上，要为「decode 阶段、GQA（`headNum=32, kvHeadNum=8`）、带倒三角 mask、`K_CACHE_V_CACHE`、`BSND`、开 logN 缩放」配置 `SelfAttention`。

1. 写出 `SelfAttentionParam` 各关键字段的取值。
2. 跟 [`CreateOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L62-L158) 的校验链，判断这组配置在 910B 上是否合法（提示：关注 `HeadNumCheck` 的整除关系、`InferLogNCheck` 对 logN 输入张量与 dtype 的要求）。
3. 用 [`GetInputNum`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L741-L792) 推算需要装几个输入张量。
4. 用 [`CreateRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2086-L2143) 判定会选中哪个 Runner。
5. 若把芯片换成 Atlas 800（910A），同样配置会发生什么？

**参考答案要点**：
1. `calcType=DECODER, kvcacheCfg=K_CACHE_V_CACHE, maskType=MASK_TYPE_NORM, inputLayout=TYPE_BSND, scaleType=SCALE_TYPE_LOGN, headNum=32, kvHeadNum=8`。
2. `32 % 8 == 0` 通过 `HeadNumCheck`；logN 要求多传一个 1 维 scale 输入且 910B 上 dtype 需为 float（非 fp16），见 [`InferLogNCheck`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L1958-L1988)；整体合法。
3. 910B + DECODER + K_CACHE：基线 8，`+1`（norm mask），`+1`（logN scale），共 **10** 个输入（**待本地验证**）。
4. 910B 分支、非 PA/PREFIX、非 bypass → `SelfAttentionFusionOpsRunner`（含 KVCache 写入节点）。
5. 换 910A：`DeviceParamCheck` 直接拒绝——910A 不支持 logN，且 910A 仅支持 `PA_ENCODER`。

---

## 6. 本讲小结

- `SelfAttentionParam` 用「字段组合」一个结构覆盖 flash/paged/prefix 注意力、KV Cache 写入或旁路、9 种 mask、BSND/BNSD 两种排布；四个形态开关是 `calcType`、`kvcacheCfg`、`maskType`、`inputLayout`。
- `SelfAttentionOperation` 的特点是「校验极重」：`CreateOperation` 在建对象前用一连串 `XxxCheck` 卡死跨字段与芯片能力的合法子集，`GetInputNum` 完全由字段组合决定，`InferShapeImpl` 把输出推为 `[B,S,N×vHeadSize]` 的合轴形状。
- `CreateRunner` 是一棵三层决策树：950 → `SelfAttentionAclnnRunner`；910B → `make_shared` 新建 5 种 OpsRunner 之一；910A/310P → 经 `RunnerPool` 复用 4 种 `910A` OpsRunner 之一。
- 10 个 Runner 的命名遵循 `Fusion`/`FusionBypass`/`Encoder`/`PrefixEncoder` × `BNSD` × `910A` 的词缀规则，看名字即可定位适用场景；`PrefixEncoder` 无 910A 版、paged 路径无 BNSD 版。
- 950 走 aclnn 适配：`SelfAttentionAclnnRunner` 把 atb 张量转 `aclTensor`/`aclTensorList`，按 aclnn 两段式协议调用 `aclnnFusedInferAttentionScoreV5`，`BSND` 映射为 `"TND"`。
- BSND 是通用排布，BNSD 为特定 kernel 牺牲通用性（不能与 logN/量化/prefix/高精度/MLA 共存）；KV Cache 格式 910B/950 用 ND、910A/310P 用 FRACTAL_NZ。

## 7. 下一步学习建议

- 下一篇 **u4-l5 PagedAttention 与 KV Cache 机制** 会专门讲 `PA_ENCODER` 路径的 `PagedAttention` 与 `kv_cache`/`reshape_and_cache` 算子，与本讲的 `EncoderFusionOpsRunner`、`K_CACHE_V_CACHE` 写入节点紧密相关，建议紧接着读。
- 想深入 MLA，直接进 **u4-l7 MLA 多头潜在注意力**，理解 `mlaVHeadSize > 0` 时本算子的形状推导为何改用 `mlaVHeadSize`。
- 想彻底弄懂 Runner 内部如何拼 `KernelGraph`（KVCache 节点 + FA 节点），回顾 **u3-l2 Runner 体系** 与 **u3-l4 Kernel/MKI 框架**，并可挑选 `self_attention_fusion_ops_runner.cpp` 的 `ModifyKernelGraph` 精读。
- 若关注通信与多卡注意力，**u5-l1 通信算子与 HCCL** 与 `ring_mla` 等多卡变体是后续方向。
