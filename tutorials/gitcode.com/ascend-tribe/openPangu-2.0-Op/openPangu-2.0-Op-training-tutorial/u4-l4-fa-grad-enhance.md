# FlashAttentionScoreGradEnhance 反向算子

## 1. 本讲目标

学完本讲，你应该能够：

1. **说清反向算子为什么必须复用前向的 `softmax_max` / `softmax_sum` / `attention_in` 中间量**，并写出 FA 反向的五步计算骨架。
2. **读懂 tiling 入口的三段式流程**（粗校验 → 空输入快路径 → 责任链调度），并说出本算子注册的 9 个切分模板及其优先级顺序。
3. **解释 kernel 入口如何用 64bit tilingKey 的位域把请求分发到 Pre → Sfmg → 主计算 → Post 三段式模板**，理解 AIC/AIV 混合核（1:2）的分工。
4. **梳理反向 kernel 中 5 次矩阵乘（2 次 mm1 + 2 次 mm3 + 1 次 mm4）与向量侧 dP 计算的先后顺序**，理解 `basic_modules` 把 vec/cube 拆成可复用模块的意图。
5. **对照 det（确定性）版本**，说明确定性计算与性能之间的取舍点。

本讲是 u4-l3（FA 前向 Kernel）的直接续篇：前向留下的三个中间输出，正是反向的三个关键输入。

## 2. 前置知识

- **反向传播的链式法则**：若 \( O = P \cdot V \)，则 \( \partial L / \partial V = P^T \cdot \partial L / \partial O \)，\( \partial L / \partial P = \partial L / \partial O \cdot V^T \)。本讲把 \( \partial L / \partial O \) 记作 **dy（代码中的 `dy` 输入）**。
- **softmax 反传为什么需要额外项**：对一行归一化 softmax \( P_i = e^{S_i - m} / \sum_j e^{S_j - m} \)，其梯度不是逐元素的，而是
  \[ \frac{\partial L}{\partial S_i} = P_i \left( dP_i - \sum_k dP_k P_k \right) \]
  其中 \( dP = dy \cdot V^T \)。括号里的「行内积分修正项」\( \sum_k dP_k P_k \) 有一个等价写法：\( \sum_k dP_{ik} P_{ik} = \sum_d dy_{id} \cdot O_{id} \)，即 **dy 与前向输出 attention_out 逐元素相乘后沿 D 维求和**。这就是本算子 `attention_in` 输入存在的意义。
- **online softmax 的代价**：前向用分块滚动方式维护行最大值与指数和（u4-l1），**没有落盘完整的注意力矩阵 P**。反向要用 P，只能二选一：(a) 用 `Q·K^T` 重算打分矩阵，再用前向保存的 `softmax_max`/`softmax_sum` 免 max 免 sum 地重建 P；(b) 只需要行修正项时，直接用 `attention_in`（前向输出）乘 dy。本算子两条路都走了。
- **AIC/AIV 混合核**：AIC（Cube 核）做矩阵乘，AIV（Vector 核）做逐元素/归约类向量计算。本算子声明 `KERNEL_TYPE_MIX_AIC_1_2`（1 个 AIC 配 2 个 AIV），是典型的「cube 出中间矩阵、vector 修梯度」协作模式（u4-l3 已见过 1:2 混排）。
- **tilingKey 位域与模板参数**：Host 侧把约 20 个决策位编码进 64bit tilingKey，Device 侧入口把它反解成 C++ 模板参数并用 `if constexpr` 编译期分发（u4-l3 已建立该模型，本讲看它的反向版）。
- **TilingBase 责任链**：多个 tiling 模板按优先级注册（数值越小越先执行），`IsCapable()` 自判能力，`GRAPH_PARAM_INVALID` 让位给下一个（u3-l3）。

## 3. 本讲源码地图

本讲涉及的核心文件（均在 `ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/` 下，简写为 `<OP>`）：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `<OP>/op_host/flash_attention_score_grad_enhance_def.cpp` | 1640 | 原型注册：22 个输入、7 个输出、双芯片 AddConfig |
| `<OP>/op_host/flash_attention_score_grad_enhance_tiling.cpp` | 337 | tiling 入口：校验、空输入快路径、责任链调度、TilingParse |
| `<OP>/op_host/arch32/flash_attention_score_grad_enhance_tiling_common.cpp` | 396 | 各 tiling 模板共享的 shape/dtype 校验函数 |
| `<OP>/op_host/arch32/flash_attention_score_grad_enhance_tiling_s1s2_bn2gs1s2.cpp` | 2653 | 主力切分模板 s1s2_bn2gs1s2（优先级 16000） |
| `<OP>/op_kernel/flash_attention_score_grad_enhance.cpp` | 1888 | kernel 入口：tilingKey 位域 → 模板分发 |
| `<OP>/op_kernel/arch32/flash_attention_score_grad_enhance_template_tiling_key.h` | — | tilingKey 位段声明（UB0/UB1/Block/IsSameAb/...） |
| `<OP>/op_kernel/arch32/basic_modules/vec_op.h` | 465 | vector 侧基础模块：SubGrapA（重建 P）/ SubGrapB（算 dP） |
| `<OP>/op_kernel/arch32/basic_modules/cube_op.h` | 348 | cube 侧基础模块：Cube1/2/3/23Process 与 L1/L0 缓冲管理 |
| `<OP>/op_kernel/arch32/flash_attention_score_grad_enhance_sfmg.h` | 331 | softmax 梯度前置阶段：SoftmaxGradFront 求行修正项 |
| `<OP>/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h` | 3486 | 主力模板：mm1/mm3/mm4 五次矩阵乘的编排 |
| `<OP>/docs/npu_flash_attention_score_grad_enhance.md` | — | 官方公式与调用示例（torch.ops.custom 接口） |

> 提示：`arch32` 目录对应 A2/A3 类芯片（`ascend910b` / `ascend910_93`），与 u4-l8 将讲的 `arch35`（A3 的 AttentionPioneer）相对。本算子没有 arch35 目录。

## 4. 核心概念与源码讲解

### 4.1 反向算子的数学骨架与输入输出契约

#### 4.1.1 概念说明

前向（u4-l1）计算的是：

\[ S = Mask\left(\frac{QK^T + pse}{\sqrt{d}}\right),\quad P = Dropout(Softmax(S), keep\_prob),\quad O = PV \]

官方文档给出了反向的三个矩阵乘（[docs/npu_flash_attention_score_grad_enhance.md:L27-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/docs/npu_flash_attention_score_grad_enhance.md#L27-L46)，文档中 ∇V/∇Q/∇K 即 dV/dQ/dK）：

\[ dV = P^T \cdot dY,\qquad dQ = (dS \cdot K) \cdot scale,\qquad dK = (dS^T \cdot Q) \cdot scale \]

其中真正的难点是 \( dS \)——softmax+dropout 之后的完整梯度：

\[ dP = dY \cdot V^T,\qquad dS = \left(dP - \mathrm{rowsum}(dY \odot O)\right) \odot P \]

把三件事拆开看，反向算子需要「重获」三类前向信息：

| 前向留下的量 | 反向中的用途 |
| --- | --- |
| `attention_in`（前向输出 O） | 行修正项 \( \mathrm{rowsum}(dY \odot O) \)，即 sfmg 阶段 |
| `softmax_max`（fp32） | 重建 P 时免去重新求行最大值 |
| `softmax_sum`（fp32） | 重建 P 时免去重新求行指数和 |

这就是 u4-l1 结论「训练 FA 比推理 FA 多输出三个中间量」在反向侧的兑现：**前向多写的每一个字节，都是反向省下的一次重算**。

#### 4.1.2 核心流程

一次反向计算的完整数据流（以主力模板为例）：

```text
输入: query, key, value, dy, attention_in, softmax_max, softmax_sum, (可选 mask/dropout/pse/rope/sink...)

[阶段0 Pre]      AIV: 初始化输出 workspace（dq/dk/dv 清零）、预处理 drop_mask
[阶段1 Sfmg]     AIV: rowsum(dy ⊙ attention_in)  → sfmgWorkspace        (行修正项)
[阶段2 mm1-a]    AIC: dP_raw = dy · Vᵀ           → mm1Workspace (fp32)
[阶段2 mm1-b]    AIC: S_raw  = Q · Kᵀ            → mm2Workspace (fp32)   (打分矩阵重算)
[阶段3 SubGrapA] AIV: scale·S_raw → 加 mask → 用 softmax_max/sum 重建 P → dropout → dropWorkSpace
[阶段3 SubGrapB] AIV: dP = (dP_raw − sfmg) ⊙ P                          → mulWorkSpace
[阶段4 mm4]      AIC: dQ = dP · K                → dqWorkSpace
[阶段4 mm3-a]    AIC: dK = dPᵀ · Q               → dkWorkSpace
[阶段4 mm3-b]    AIC: dV = Pᵀ · dy               → dvWorkSpace
[阶段5 Post]     AIV: workspace 中的 fp32 结果 Cast 回 bf16/fp16，按原 layout 写回 dq/dk/dv
```

注意「5 次矩阵乘」的准确口径：**mm1 复用同一个 Matmul 对象打两次**（dP_raw 与 S_raw），mm3 也打两次（dK 与 dV），mm4 打一次（dQ），共 5 次矩阵乘调用；而 **dP 本身不是矩阵乘**——它是向量侧逐元素算出来的，这是与直觉最容易出错的地方。

#### 4.1.3 源码精读

**输入输出的完整清单**在 `_def.cpp` 中声明。必选的 4 个与关键的可选输入：

- [flash_attention_score_grad_enhance_def.cpp:L24-L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L24-L78)：声明 `query` / `key` / `value` / `dy` 四个必选输入——反向算子的「题面」。
- [flash_attention_score_grad_enhance_def.cpp:L158-L206](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L158-L206)：声明可选输入 `softmax_max` / `softmax_sum` / `softmax_in` / `attention_in`——前向中间量的回接点。tiling 侧会用 `GetOptionalInputShape` 判断它们是否真的存在（见 4.2.3）。
- [flash_attention_score_grad_enhance_def.cpp:L435-L530](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L435-L530)：声明 7 个输出 `dq` / `dk` / `dv` / `dpse` / `dq_rope` / `dk_rope` / `dsink`——比前向的 4 个输出多出 rope 与 sink 的梯度通道。
- [flash_attention_score_grad_enhance_def.cpp:L1633-L1638](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L1633-L1638)：`AICore().AddConfig("ascend910b"/"ascend910_93")` 编译期芯片白名单 + `OP_ADD(FlashAttentionScoreGradEnhance)` 入注册表。

对照输入索引（`_def.cpp` 声明顺序即索引）：0=query、1=key、2=value、3=dy、4=pse_shift、5=drop_mask、6=padding_mask、7=atten_mask、8=softmax_max、9=softmax_sum、10=softmax_in、11=attention_in、12=prefix、13=actual_seq_qlen、14=actual_seq_kvlen、15=q_start_idx、16=kv_start_idx、……、22=query_rope、23=key_rope、24=sink。tiling 源码里的常量与之一一对应（见 4.2.3 的 `SOFTMAX_MAX = 8`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「行修正项两种写法等价」，理解 `attention_in` 为什么能省掉一次对 P 的依赖。

**操作步骤**（示例代码，可在任何有 PyTorch 的 CPU 环境运行）：

```python
import torch

torch.manual_seed(0)
S1, S2, D = 4, 4, 8                     # 小规模：单头单 batch
S = torch.randn(S1, S2)                 # 打分矩阵
P = torch.softmax(S, dim=-1)            # 注意力矩阵（前向没存它！）
V = torch.randn(S2, D)
O = P @ V                               # 前向输出 == attention_in
dy = torch.randn(S1, D)

dP = dy @ V.T                           # 写法一的原料
t1 = (dP * P).sum(dim=-1)               # 修正项写法一：rowsum(dP ⊙ P)
t2 = (dy * O).sum(dim=-1)               # 修正项写法二：rowsum(dy ⊙ attention_in)
print(torch.allclose(t1, t2, atol=1e-5))  # 预期 True
```

**需要观察的现象**：`t1` 与 `t2` 逐元素相等——这说明反向只要拿到前向输出 `attention_in`，就**不需要**完整重建 P 也能算出 softmax 的行修正项；而要算 dP 对 S 的逐元素展开（\(P_i(\cdot)\) 那个因子）时才必须重建 P。

**预期结果**：打印 `True`。若把 `O` 换成错误的前向输出则不相等。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：前向已经输出了 `softmax_out`（u4-l1 的第三个输出），为什么反向重建 P 还需要再算一次 `Q·K^T`？

**答案**：`softmax_out` 只是占位张量（u4-l2 的 InferShape 把它置零，前向并不物化注意力矩阵），真正落盘的只有 `softmax_max`/`softmax_sum` 两个 fp32 中间量。重建 P 需要「未归一化的打分 + max + sum」三样，max/sum 前向给了，打分只能用 `Q·K^T` 重算——这正是反向 mm1-b 存在的原因。

**练习 2**：`dpse`（位置编码的梯度）是从哪一步「顺路」算出来的？

**答案**：pse 在前向是加进打分矩阵的（`S = (QK^T + pse)·scale`），因此 \( \partial L/\partial pse = dS \)（同 shape）。向量侧算出 dS 后做一次同 shape 拷贝/规约即可得到 dpse，不需要额外的矩阵乘——它是五次 Matmul 之外的「免费搭车」输出。

**练习 3**：`sink` 机制（u4-l1）会让反向多出什么输出？对应 def 里的哪个声明？

**答案**：多出 `dsink`（sink 通道的梯度），见 [flash_attention_score_grad_enhance_def.cpp:L525](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L525) 的 `Output("dsink")`；Post 阶段的 `Init(..., dsink, ...)` 参数（见 4.3.3）负责把它写出。

### 4.2 Tiling 入口：校验、空输入快路径与责任链调度

#### 4.2.1 概念说明

`flash_attention_score_grad_enhance_tiling.cpp` 是所有 tiling 模板的**公共入口**。它不自己切分数据，只做三件事：粗校验（参数齐不齐、shape 合不合法）、空输入快路径（输出为空时只做清零规划）、然后把剩余工作全权委托给 `TilingRegistryNew` 责任链（u3-l3 讲过的模板注册表）。各切分模板共享的校验函数抽在 `arch32/flash_attention_score_grad_enhance_tiling_common.cpp`，避免九个模板各抄一份。

#### 4.2.2 核心流程

入口函数 `TilingFlashAttentionGradScore` 的三段式：

```text
1. CheckParams(context)        # attrs 非空、rope 成对出现、q/k/v/dy/max/sum/atten_in 形状齐备
2. socVersion 白名单            # 只允许 ASCEND910B / ASCEND910_93
   └─ IsEmptyOutput(context)?  # dq/dk/dv 任一 shape size 为 0
       └─ 是 → RunEmptyTiling  # tilingKey=0，只规划"把输出清零"
3. TilingRegistryNew::GetInstance().DoTilingImpl(context)
                               # 按优先级逐个尝试 9 个已注册模板
```

九个模板的注册全景（`REGISTER_TILING_TEMPLATE_WITH_SOCVERSION`，**数值越小优先级越高**）：

| 优先级 | tiling 类 | 注册文件 | 定位 |
| --- | --- | --- | --- |
| 10 | `...TilingS1s2Bn2gs1s2SameAb` | `arch32/..._tiling_s1s2_bn2gs1s2_sab.cpp` | S1==S2 且变长平均序列够长（`isTndSABHit`）时的高性能同 AB 模板 |
| 1000 | `...TilingDeterministic` | `arch32/..._tiling_s1s2_bn2.cpp` | 确定性场景（继承 S1s2Bn2） |
| 1001 | `...TilingMla`（basic） | `arch32/..._tiling_s1s2_bn2gs1s2_basic.cpp` | MLA 场景基础模板（走 VecOp/CubeOp 模块） |
| 1002 | `...TilingBasicDet` | `arch32/..._tiling_s1s2_bn2gs1s2_basic_det.cpp` | MLA + 确定性 |
| 1100 | `...TilingSameABDeterministic` | `arch32/..._tiling_s1s2_bn2gs1s2_sab.cpp` | SameAb + 确定性 |
| 2000 | `...TilingUnpaddedAttension` | `arch32/..._tiling_unpadded_attension.cpp` | 非padding注意力场景 |
| 10000 | `...Ubngs1s2BbTiling`（bngs1s2_b） | `arch32/..._tiling_bngs1s2_b.cpp` | 沿 B 切块的通用回退 |
| 11000 | `...`（ngs1s2_bn） | `arch32/..._tiling_ngs1s2_bn.cpp` | 沿 N 切块的通用回退 |
| 15000 / 16000 | `...TilingS1s2Bn2` / `...TilingS1s2Bn2gs1s2` | `arch32/..._tiling_s1s2_bn2.cpp` / `..._tiling_s1s2_bn2gs1s2.cpp` | 最后两道通用防线 |

责任链的语义（u3-l3）：排在前面的模板 `IsCapable()` 返回 false 时返回 `GRAPH_PARAM_INVALID` 让位，返回 `GRAPH_FAILED` 立即中止，成功则终止链条。

#### 4.2.3 源码精读

- [flash_attention_score_grad_enhance_tiling.cpp:L27-L49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_tiling.cpp#L27-L49)：定义输出索引（`OUTPUT_IDX_DQ=0`…`OUTPUT_IDX_DK_ROPE=5`）、输入索引（`QUERY=0`…`SOFTMAX_MAX=8`、`SOFTMAX_SUM=9`、`ATTENTION_IN=11`）与空输入 tilingKey（`FAG_EMPTY_TILING_KEY=0`）、100MB workspace 常量。**这些下标就是 4.1.3 里 def 声明顺序的镜像**——调换 def 里 Input 顺序会静默弄错这里。
- [flash_attention_score_grad_enhance_tiling.cpp:L274-L294](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_tiling.cpp#L274-L294)：入口函数本体。socVersion 只认 `ASCEND910B` / `ASCEND910_93`，空输出走 `RunEmptyTiling`，否则落到 `TilingRegistryNew::GetInstance().DoTilingImpl(context)` 责任链。
- [flash_attention_score_grad_enhance_tiling.cpp:L64-L137](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_tiling.cpp#L64-L137)：`RunEmptyTiling`——把 dq/dk/dv/dpse 的元素总数按 AIV 核数做「均分 + 尾核」三组参数（Former/Single/Tail）写进 `emptyTensorTilingData`，设 tilingKey=0、按 `CalculateTschBlockDim` 设核数。kernel 侧对应 4.3.3 将看到的空输入早退分支。
- [flash_attention_score_grad_enhance_tiling.cpp:L296-L329](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_tiling.cpp#L296-L329)：`TilingPrepareFor...` 在**编译期**执行一次，把 AIV/AIC 核数、UB/L1/L0A/L0B/L0C/L2 尺寸、socVersion 探测进 `FlashAttentionScoreGradEnhanceCompileInfo` 缓存——运行期 tiling 直接 `GetCompileInfo()` 取用，不必每次查询平台（u4-l2 的 TilingParse 钩子机制）。
- [flash_attention_score_grad_enhance_tiling.cpp:L331-L335](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_tiling.cpp#L331-L335)：`IMPL_OP_OPTILING(FlashAttentionScoreGradEnhance).Tiling(...).TilingInputsDataDependency({12, 13, 14, 15, 16}).TilingParse<...>(...)`。注意数据依赖声明的是**下标 12~16**：prefix、actual_seq_qlen、actual_seq_kvlen、q_start_idx、kv_start_idx——变长与稀疏元数据必须先物化成真实值，tiling 才能按真实序列长度切分（与 u4-l1 前向的 `TilingInputsDataDependency` 精确互锁，只是两算子的输入排列不同导致下标不同）。

公共校验（`tiling_common.cpp`）里最有代表性的一组：

- [flash_attention_score_grad_enhance_tiling_common.cpp:L22-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_common.cpp#L22-L51)：`CheckSoftmaxMaxShape`——`softmax_max` 必须是 4 维 `(b, n1, s1, 8)`，**末维固定 pad 到 8**。这个 8 是向量指令按 8 个 fp32（32 字节）一次处理的对齐痕迹，前向 InferShape 与反向校验两侧必须一致。
- [flash_attention_score_grad_enhance_tiling_common.cpp:L209-L226](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_common.cpp#L209-L226)：`CheckSoftmaxDtype`——`softmax_max` 与 `softmax_sum` 必须同为 `DT_FLOAT`。反向的数值稳定性依赖这两个量是 fp32，bf16 存不下足够的指数和精度。
- [flash_attention_score_grad_enhance_tiling_common.cpp:L178-L207](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_common.cpp#L178-L207)：`CheckAttentionInShape`——`attention_in` 除最后一维（D 允许不同）外必须与 query 同 shape。
- [flash_attention_score_grad_enhance_tiling_common.cpp:L364-L394](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_common.cpp#L364-L394)：`isTndSABHit`——读 `actual_seq_qlen/kvlen` 张量**内容**（`GetData<int64_t>()`），当平均序列长 ≥ 1024 时判定走 SameAb 高性能模板。这是「tiling 依赖张量取值」的实例，也是 `TilingInputsDataDependency` 必须包含 13/14 号输入的原因。

#### 4.2.4 代码实践

**实践目标**：跑通本算子的 tiling UT，观察责任链选中的模板与 tilingKey。

**操作步骤**：

1. 进入已装好 CANN 与 bisheng 的容器（u1-l3/u1-l4 的环境）。
2. 编译并运行 op_host UT：
   ```bash
   cd ascendc
   bash build.sh -u -n flash_attention_score_grad_enhance -c ascend910_93 --ophost
   ```
3. 打开用例文件 `src/ops-transformer/attention/flash_attention_score_grad_enhance/tests/ut/op_host/test_flash_attention_score_grad_enhance_tiling.cpp`，找到断言 `tilingKey` / `blockDim` / `workspace` 的用例，记下用例的 B/N/S/D 参数。

**需要观察的现象**：日志里出现 `TilingParseContext succ. aivNum:... aicNum:...`（来自 4.2.3 的 TilingPrepare）；不同 shape 的用例命中不同模板（如大 S1S2 命中 sab 或 s1s2 模板）。

**预期结果**：UT 全部通过，且能从日志分辨出责任链最终选中的实现。无 NPU/编译环境时，本实践退化为「阅读 UT 用例 + 对照 4.2.2 优先级表预测命中模板」，预测结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RunEmptyTiling` 里要同时写 `Former/Single/Tail` 三组数字，而不是简单地 `dqNum / aivNum`？

**答案**：元素总数往往不能被核数整除。代码把「有余数的核（Former，多干一个）」「普通核（Single）」「尾核（Tail，少干一个）」三种角色分开描述（[flash_attention_score_grad_enhance_tiling.cpp:L84-L105](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_tiling.cpp#L84-L105)），kernel 侧按 `GetBlockIdx()` 与三组数反解自己负责的区间。整除时 Former=aivNum、Tail=0，退化为均分。

**练习 2**：`CalculateTschBlockDim`（[flash_attention_score_grad_enhance_tiling.cpp:L51-L59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_tiling.cpp#L51-L59)）在算什么？为什么除法是 `(sliceNum + ration - 1) / ration`？

**答案**：混合核下 1 个 AIC 带 2 个 AIV（`ration = aivCoreNum / aicCoreNum`），一个「任务片」要成组启动；blockDim 按 AIC 组数向上取整，保证每片任务有配套的 AIC。`(a+b-1)/b` 是不借助 math_util 时手写的向上取整（u3-l1 讲过 `CeilDiv` 标准件）。

**练习 3**：如果用户传入的 `softmax_max` 是 bf16，会在哪一步、以什么方式失败？

**答案**：在某个切分模板调用 `CheckDtypeValid` → `CheckSoftmaxDtype`（4.2.3 第 2 条链接）时，`OP_CHECK_IF` 打出「softmaxMaxType should be DT_FLOAT and same with softmaxSumType」错误日志并返回 `GRAPH_FAILED`，整条责任链立即中止，不会到 kernel。

### 4.3 Kernel 入口：tilingKey 位域分发与三段式执行

#### 4.3.1 概念说明

`op_kernel/flash_attention_score_grad_enhance.cpp` 是设备侧总入口。它做三件事：声明一个带 19 个模板参数的 `__global__ __aicore__` 函数；把 tilingKey 位域映射成这些模板参数；用 `if constexpr` 在**编译期**把每个 (dtype × layout × 模板 × 对齐度) 组合实例化成一份专属代码。每个组合的执行都被 INVOKE 宏组织成固定的三段式：**Pre（准备）→ 主计算 → Post（收尾）**，部分模板在中间再插一个 **Sfmg** 阶段。

#### 4.3.2 核心流程

tilingKey 位段（声明于 [flash_attention_score_grad_enhance_template_tiling_key.h:L30-L90](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_template_tiling_key.h#L30-L90)）摘录：

| 位段 | 含义 | 取值 |
| --- | --- | --- |
| bit 0-3 `UB0` / bit 4-7 `UB1` | 数据在 UB 中沿哪个逻辑轴切开 | 0=B, 1=N2, 2=G, 3=S1, 4=S2, 5=D, 9=NONE |
| bit 8-11 `Block` | 核间（blockDim）沿哪个轴切 | 同上 |
| bit 12 `IsSameAb` | 是否 S1==S2 的同 AB 场景 | 0/1 |
| bit 13-14 `DataType` | 0=FP16, 1=FP32, 2=BF16, 3=FP16 高精度 | — |
| bit 15-16 `Layout` | 0=BSH, 1=SBH, 2=BNSD, 3=TND | — |
| bit 17-20 `Sparse` | 稀疏模式枚举（ALL/NONE/CAUSAL/BAND/...） | — |
| … | MatmulCfg、Mm12IsNZOut、HasDropOut、HasPse、HasAttenMask、EnableL1Reuse、TNDS1Pingpong、S1/S2/DTemplateType、IsDeterministic、HasRope 等 | — |

入口按 `(UB0, UB1, Block, IsSameAB)` 的元组选出模板族（源码注释原文标注）：

| 元组（源码注释） | 模板类 | 三段式组成 |
| --- | --- | --- |
| `UB0==0 && UB1==0 && Block==0` | `EmptyTensor` | 只清零 dq/dk/dv/dpse |
| `4,3,4,IsSameAB=1`（sameab） | `...S1s2Bn2gs1s2SameAB` | Pre → (AIV) Sfmg → 主 → Post |
| `4,3,4,IsSameAB=0`（s1s2） | `...S1s2Bn2gs1s2` | Pre → 主 → Post |
| `4,3,1`（bn2） | `...S1s2Bn2` | Pre → 主 → Post(Cast) |
| `9,9,1`（bn / ngs1s2_bn） | `...Ungs1s2Bbn` | Pre → 主 → Post |
| `9,9,0`（b / bngs1s2_b） | `...Ubngs1s2Bb` | Pre → 主 → Post |
| `9,9,9`（basic） | `...Basic`（MLA，走 VecOp/CubeOp） | 单段 Process |
| `9,9,9 + IsDeterministic`（basic det） | `...BasicDet` | 单段 Process（确定性版） |

以 sameab 模板为例的三段式编排（INVOKE 宏展开后的语句顺序）：

```text
GET_TILING_DATA_WITH_STRUCT(...SameAb, tiling_data_in, tiling_data)   # 解包 TilingData
opPre.Init(...); opPre.Process();            # 阶段0：AIV 清零输出 workspace、展开 drop_mask
if ASCEND_IS_AIV {
    opSfmg.Init(dy, attention_in, ...); opSfmg.Process();   # 阶段1：行修正项 → sfmgWorkspace
}
op.InitTscmBuffer(&pipeBase); op.Init(key, value, dy, query, ...);      # 绑 GM
op.ProcessFirstMM(); op.InitBuffer(&pipeBase); op.Process();            # 阶段2-4：五次 Matmul + 向量修正
op.SyncALLCores();
if ASCEND_IS_AIV { opPost.Init(...); opPost.Process(); }  # 阶段5：fp32 → 目标 dtype，按 layout 写回
```

#### 4.3.3 源码精读

- [flash_attention_score_grad_enhance.cpp:L1044-L1102](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1044-L1102)：19 个模板参数 + 35 个 `__gm__` 入参（顺序即 def 声明顺序）+ `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 声明 AIC:AIV = 1:2 混合核。`SetMaskNorm()` 与 `GetUserWorkspace(workspace)` 取用户 workspace 基址。
- [flash_attention_score_grad_enhance.cpp:L1106-L1124](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1106-L1124)：空输入早退——`REGISTER_TILING_FOR_TILINGKEY("(TILING_KEY_VAR & 0x0)", ...)` 把 tilingKey=0 绑定到空 TilingData 结构，按 `ORIG_DTYPE_QUERY` 选 half/float/bfloat16_t 实例化 `EmptyTensor`，清零后 `return`。与 4.2 的 `RunEmptyTiling` 是同一契约的两侧。
- [flash_attention_score_grad_enhance.cpp:L1126-L1141](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1126-L1141)：格式与布局的编译期换算——`Mm12IsNZOut` 决定 mm1/mm2 输出走 NZ 还是 ND；`mock_layout` 把 Layout 枚举换算成 kernel 内部常量（注意 `Layout == 0 ? 2 : Layout == 2 ? 0 : Layout` 的 0↔2 互换，kernel 内部 BNGSD=0/BSNGD=2 与 tilingKey 的 BSH=0/BNSD=2 编码不一致，这里做翻译）。随后是 FP16 分发链的开头（`#if (ORIG_DTYPE_QUERY == DT_FLOAT16)` 包裹）。
- [flash_attention_score_grad_enhance.cpp:L1334-L1441](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1334-L1441)：BF16 分发链。sameab 分支里再按 `DTemplateType`（D 维对齐度 0/1/5/6 → NotAligned/Aligned64/Aligned128/Aligned192）与 `HasRope` 二次细分——对齐度是编译期特化的「模板套餐」，让对齐场景省去补零逻辑。
- [flash_attention_score_grad_enhance.cpp:L205-L297](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L205-L297)：`INVOKE_FAG_GENERAL_S1S2_BN2GS1S2_SAMEAB_L1_CUSTOM_IMPL` 宏——三段式最完整的样本：`opPre` → `if ASCEND_IS_AIV { opSfmg }` → 主计算（`ProcessFirstMM` 先发射第一拍矩阵乘，`InitBuffer` 再配 UB，`Process` 进入主循环）→ `op.SyncALLCores()` → `if ASCEND_IS_AIV { opPost }`。注意 `REGIST_MATMUL_OBJ`（如 [L88-L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L88-L89)）把 `op.mm1/mm3/mm4` 三个 Matmul 对象与 TilingData 里的 `mm1TilingData/mm2TilingData/mm3TilingData` 绑定到同一 TPipe。
- [flash_attention_score_grad_enhance.cpp:L1014-L1041](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1014-L1041)：`INVOKE_FAG_DETERMINISTIC_BASIC_IMPL`——det 版入口，用 `FAG_TYPE<>` 聚合模板参数后实例化 `FlashAttentionScoreGradEnhanceBasicDet`，一次 `Process` 完成全部阶段（详见 4.5）。

#### 4.3.4 代码实践

**实践目标**：建立「tilingKey 位段 → 模板实例」的映射表。

**操作步骤**：

1. 打开 [flash_attention_score_grad_enhance_template_tiling_key.h:L30-L120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_template_tiling_key.h#L30-L120)，抄下每个位段的名字与取值枚举。
2. 在 kernel 入口文件里 `grep -n "else if constexpr (UB0" `，把每个分支的元组与源码注释（`// sameab`、`// s1s2`、`// bn2`、`// bn`、`// b`、`// basic det`）填进 4.3.2 的表格。
3. 回到 4.2.2 的优先级表，标注每条责任链模板会写出哪个 `(UB0, UB1, Block)` 组合（在对应 `arch32/..._tiling_*.cpp` 里 grep 模板参数赋值即可）。

**需要观察的现象**：同一个模板族名（如 s1s2_bn2gs1s2）在 FP16/BF16/FP32 三条 `#if` 链里各出现一次，但分支数量不同——FP32 链明显更短（不支持 sameab、basic 等）。

**预期结果**：得到一张三列对照表（位段元组 ↔ 模板类 ↔ 责任链 tiling 实现），后续读任何 FA 类算子入口都可以复用这张表的生成方法。

#### 4.3.5 小练习与答案

**练习 1**：为什么入口要用 `if constexpr` 而不是普通 `if`？

**答案**：每个分支里的 INVOKE 宏会实例化完全不同的模板类（不同的 `IS_ATTEN_MASK/IS_PSE/MM_OUT_FORMAT` 模板实参、不同的 TilingData 结构、不同的 UB 布局偏移）。`if constexpr` 让未选中的分支在编译期被丢弃，一份 kernel 二进制只为选中的组合生成代码；普通 `if` 会实例化所有分支，编译时间和代码体积都不可接受。

**练习 2**：`FlashAttentionScoreGradEnhancePre/Post` 为什么有的模板里被 `if ASCEND_IS_AIV` 包住，有的没有？

**答案**：Pre/Post 是纯向量工作（清零、Cast、layout 回写）。混合核下同一个 blockIdx 空间里 AIC 与 AIV 共存，`ASCEND_IS_AIV` 让只有向量核执行这些阶段、cube 核跳过，避免重复写与同步开销；个别模板里 Pre 需要所有核参与初始化（如 workspace 边界设置）时不加保护，改用 `opPre.SyncALLCores()` 做核间同步。

**练习 3**：入口参数有 35 个 `__gm__` 指针，比 def 里 22 个输入 + 7 个输出还多，多出来的是什么？

**答案**：还有 `workspace`（用户 workspace 基址，`GetUserWorkspace` 从中切出各阶段缓冲）、`tiling_data`（Host 序列化的 TilingData 字节流），以及框架为可选输入保留的空槽位——可选输入不存在时对应指针传入空地址，kernel 侧用 `INPUT_EXIST/INPUT_NONE` 等模板参数在编译期剪枝。

### 4.4 basic_modules：vec/cube 模块化拆分与五次 Matmul 的调用顺序

#### 4.4.1 概念说明

`op_kernel/arch32/basic_modules/` 是本算子的「积木箱」：把反向计算中**与具体切分策略无关**的最小计算单元抽成独立模块，供 basic / basic_det 等模板组装。分三层：

- **cube_op.h**（+ `cube_modules/cube1/2/3/23_*.h`）：Cube 核积木。`Cube1Process/Cube2Process/Cube3Process/Cube23Process` 封装「GM→L1→L0A/L0B→Mmad→Fixpipe→GM」的完整矩阵乘流水，含 ping-pong 双缓冲。
- **vec_op.h**（+ `vec_modules/vec_pre/vec_post/vec_sfmg/vec_addr`）：Vector 核积木。核心是 `SubGrapA`（从 mm2 的打分矩阵重建 P）与 `SubGrapB`（把 mm1 的 dP_raw 修正成 dP）两个子图。
- **sfmg.h**：独立成类的 softmax 梯度前置阶段——用 `SoftmaxGradFront` 高阶 API 算行修正项 \( \mathrm{rowsum}(dy \odot attention\_in) \)。

**复用意图**：FA 反向的「五次矩阵乘 + 两次向量修正」计算骨架对所有切分策略都相同，变化的只是任务怎么切给核。把它沉到 basic_modules 后，basic（MLA）与 basic_det（确定性）两个模板直接组装同一套积木，新增切分模板时也不必重写数值逻辑。主力模板 s1s2_bn2gs1s2 因为深度流水线化（ping-pong、NZ 格式直传）把等价逻辑内联进了自己的 SubGrapA/SubGrapB，但变量名与步骤顺序仍与 basic_modules 一一对应——读懂数值骨架，两份实现都能读。

#### 4.4.2 核心流程

五次矩阵乘与向量修正的调用顺序（以 s1s2_bn2gs1s2 主循环一轮为例，行号见 4.4.3）：

| 步 | 阶段 | 计算公式 | 核型 | 数据去向 |
| --- | --- | --- | --- | --- |
| 1 | mm1 第 1 打 | \( dP_{raw} = dY \cdot V^T \) | AIC | `mm1Workspace`（fp32） |
| 2 | mm1 第 2 打 | \( S_{raw} = Q \cdot K^T \)（+rope 项累加） | AIC | `mm2Workspace`（fp32） |
| 3 | Sfmg / CalcSoftMaxGrad | \( r = \mathrm{rowsum}(dY \odot attention\_in) \) | AIV | `sfmgWorkspace` |
| 4 | SubGrapA | \( P = softmax_{max,sum}(scale \cdot S_{raw} + mask) \)，再 dropout | AIV | `dropWorkSpace`（目标 dtype） |
| 5 | SubGrapB | \( dP = (dP_{raw} - r) \odot P \) | AIV | `mulWorkSpace`（目标 dtype） |
| 6 | mm4 | \( dQ = dP \cdot K \) | AIC | `dqWorkSpace`（fp32 累加） |
| 7 | mm3 第 1 打 | \( dK = dP^T \cdot Q \) | AIC | `dkWorkSpace` |
| 8 | mm3 第 2 打 | \( dV = P^T \cdot dY \) | AIC | `dvWorkSpace` |
| 9 | Post | fp32 → bf16/fp16，按 layout 写回 dq/dk/dv | AIV | 输出张量 |

几个工程要点：

- **转置藏在 MatmulType 里**：`bType1 = MatmulType<..., true>` 的 `true` 即「B 矩阵转置使用」，于是 `SetTensorB(valueGm, true)` 在数学上就是 \( \cdot V^T \)；而 mm4 的 B（key）不转置、mm3 的 A（dP/P）转置，分别实现 \( \cdot K \) 与 \( (\cdot)^T \cdot \)。
- **两份中间矩阵都以 fp32 落盘**：mm1/mm2 的输出类型 `T2=float`，softmax 重建与 dP 修正全在 fp32 域完成，只在送回 cube 前一次 Cast——精度关键路径不长。
- **ping-pong 双缓冲**：`mulWorkSpaceGm[pingpongIdx * coreNum * cubeBaseMN + ...]` 的偏移公式让第 i 轮向量计算写 pong 区的同时，cube 消化第 i-1 轮的 ping 区，向量与 cube 两级流水不断流。

#### 4.4.3 源码精读

**先看主力模板里的三次 Matmul 对象与五次调用**：

- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L162-L182](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L162-L182)：声明 `mm1`（A/B 均 ND、B 转置、输出 MM_OUT_FORMAT）、`mm3`/`mm4`（A 可为 NZ 且转置、输出 MM2_OUT_FORMAT，NZ 时挂 `MatmulCallBackFunc<DataCopyOut>` 回调）。**三个 Matmul 对象打五次**的分工从这里定下。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L2792-L2795](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L2792-L2795)：**第 1 次矩阵乘**——`SetTail(s1CvExtend, s2CvExtend, value_d)`（M/N/K），`SetTensorA(dxGm[...])`、`SetTensorB(valueGm[...], true)`，`IterateAll<false>(mm1WorkspaceGm, ...)`：\( dP_{raw} = dY \cdot V^T \)。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L2811-L2834](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L2811-L2834)：**第 2 次矩阵乘**——同一个 `mm1` 对象换 Tensor：`SetTensorA(queryGm)`、`SetTensorB(keyGm, true)` → \( S_{raw} = Q \cdot K^T \) 写 `mm2Workspace`；`HAS_ROPE` 时第三打 `queryRope·keyRope^T` 以 `IterateAll<false>(mm2WorkspaceGm, true, ...)`（第二参 true 表示**累加**）并入同一份打分矩阵。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L2912-L2924](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L2912-L2924)：调用 `CalcSoftMaxGrad`（行修正项）后紧跟两次 `mm1.WaitIterateAll()`（rope 时三次）——**向量计算与 cube 异步并行**，此处才等待两打 mm1 落盘。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L886-L950](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L886-L950)：`CalcSoftMaxGrad` 实现——按 D 维分块（`sfmgdOuter/sfmgdInner`）搬运 `dy` 与 `attention_in`，Cast 到 fp32 后调 `SoftmaxGradFront<float, isBasicBlock>`，把各 D 块的部分和 `Add` 累加进 `sfmgClc3`。**行修正项沿 D 分块累加**正是 \( \sum_d dy_{id} O_{id} \) 的分块实现。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L2314-L2333](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L2314-L2333)：SubGrapA 的收尾——`CalcSoftMax`（P 重建）后 `ComputeDropMask` 再 Cast，写入 `dropWorkSpaceGm`（ping-pong 偏移公式见同段）。P 重建的本体在 [L1003-L1019](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L1003-L1019)：`SimpleSoftMax<T2, true, true>(dst, src, src[s1Extend*32/sizeof(float)], ...)`——第三个实参就是紧跟在 softmax_sum 后面存放的 softmax_max，**免 max 免 sum 的「除法版」softmax**；非基本块时手工展开为 `Sub(max) → Exp → Div(sum)`（[L1024-L1058](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L1024-L1058)）。scale 折算在进入 softmax 之前（[L2214-L2217](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L2214-L2217) 的 `Muls(..., scaleValue)`）。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L3166-L3205](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L3166-L3205)：**第 3 次矩阵乘（mm4）**——`SetTail(preS1Extend, d, s2CvExtend)`，`SetTensorA(mulWorkSpaceGm[...pingpong...])`（即 dP）、`SetTensorB(keyGm)`（不转置）→ \( dQ = dP \cdot K \)，`IterateAll<false>(dqWorkSpaceGm[dqOffset], true)` 尾参 true 表示跨轮累加。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L3259-L3292](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L3259-L3292)：**第 4 次矩阵乘（mm3 第 1 打）**——`SetTail(s2CvExtend, d, preS1Extend)`，`SetTensorA(mulWorkSpaceGm[...], true)`（dP 转置）、`SetTensorB(queryGm)` → \( dK = dP^T \cdot Q \)。
- [flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h:L3326-L3350](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L3326-L3350)：**第 5 次矩阵乘（mm3 第 2 打）**——`SetTensorA(dropWorkSpaceGm[...], true)`（**P** 转置）、`SetTensorB(dxGm)` → \( dV = P^T \cdot dY \)；fp32 时直接 `IterateAll<true>(dvGm[...])`（直写输出），半精度时写 `dvWorkSpaceGm` 由 Post 阶段 Cast。

**再看 basic_modules 里的对应积木**：

- [vec_op.h:L177-L251](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/vec_op.h#L177-L251)：`VecOp::Process`——按 `VecAddrInfo` 里的任务块循环：先算变长偏移（`GetSeqQlenKvlenByBidx`）与 ping-pong 拷贝参数，再依次调 `SubGrapA(i, blockInfo, ...)` 与 `SubGrapB(i, blockInfo, ...)`。**A/B 两个子图共用一套偏移计算**，这正是模块化带来的简洁。
- [vec_op.h:L360-L412](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/vec_op.h#L360-L412)：`SubGrapA`——`CopyInSoftMax`（搬 softmax_sum/softmax_max）→ 搬 `mm2Workspace`（\( QK^T \)）→ `Muls(scaleValue)` → 稀疏模式补 `CalcAttenMaskBool` → `CalcSoftMax` 重建 P → Cast → 写 `dropWorkSpaceGm`。与主力模板 SubGrapA 步骤完全同构。
- [vec_op.h:L414-L464](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/vec_op.h#L414-L464)：`SubGrapB`——搬 `sfmgWorkspace`（行修正项）与 `mm1Workspace`（\( dY V^T \)）→ `Sub`（减修正项）→ `Mul`（乘 SubGrapA 产出的 P）→ Cast → 写 `mulWorkSpaceGm`，源码注释 `// dyv = dp -> ds` 点明这一步就是 dP→dS。
- [vec_op.h:L319-L358](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/vec_op.h#L319-L358)：`CalcSoftMax` 的两版实现——8/64 对齐时走 `SimpleSoftMax`（高阶 API），否则手工 `Sub/Exp/Div` 三连。**「对齐则 API、不对齐则手工展开」是本仓库向量代码的通用范式**。
- [cube_op.h:L26-L60](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/cube_op.h#L26-L60)：`CubeOp` 类的四个 Process 入口（Cube1/2/3/23），签名统一为 `(const CubeAddrInfo &addrs, left, right[, right2], out[, out2])`——**任务描述（addrs）与数据（GM 指针）分离**，调度器只生产 `CubeAddrInfo`，积木不关心切块从哪来。
- [cube_op.h:L242-L288](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/cube_op.h#L242-L288)：`CubeOp::Init`——用 `asdopsBuf` 在 L1/L0A/L0B/L0C 上以硬编码偏移（如 `SIZE_256*SIZE_ONE_K`）划分 ping/pong 双缓冲，并预设 `MmadParams/FixpipeParamsV220/Nd2NzParams` 等固定参数结构（[L107-L174](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/cube_op.h#L107-L174)）。basic block 恒为 128×128（`BASE_BLOCK_LENGTH`）。
- [cube_op.h:L342-L345](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/basic_modules/cube_op.h#L342-L345)：`#include "cube_modules/cube1_op.h"` 等四个实现文件——cube 积木的「声明在 .h、实现拆进 cube_modules/」组织方式，vec 侧同理（`vec_modules/vec_pre/vec_post/vec_sfmg/...`，其中 `vec_sfmg.h`/`vec_pre.h` 等各有 `_det` 后缀的确定性变体）。
- [flash_attention_score_grad_enhance_sfmg.h:L231-L323](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_sfmg.h#L231-L323)：独立 `Sfmg` 类的 `Process`——按 `usedCoreNum` 分核，每核循环 `CopyInSfmg → Cast ×2 → SoftmaxGradFront → DataCopy`。其中 [L194-L229](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_sfmg.h#L194-L229) 的 `CopyInSfmg` 实现「行借 N、N 借 B」的跨轴搬运（中文注释「需要借N或借B」即出自此处），把变长序列的碎片行拼成连续 burst；[L305-L312](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_sfmg.h#L305-L312) 按行数 8 对齐、D 64 对齐选择 `SoftmaxGradFront` 的 basicBlock 版本。

#### 4.4.4 代码实践

**实践目标**：把 4.4.2 的九步表格从「我讲的」变成「你验证的」。

**操作步骤**：

1. 在仓库根目录执行（只读操作）：
   ```bash
   cd ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance
   grep -n "SetTensorA\|SetTensorB\|IterateAll" op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h
   ```
2. 对 grep 出的每一处，抄下四元组：`所在函数 / SetTensorA 的实参 / SetTensorB 的实参与是否带 true / 输出 Gm`。
3. 对照 4.4.3 的行号链接，把每一处标注为五次矩阵乘中的第几打，并写出数学公式。
4. 再执行 `grep -n "SubGrapA\|SubGrapB\|CalcSoftMaxGrad\|SoftmaxGradFront" op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h`，确认向量侧三个函数的调用点位于 mm1 的 `IterateAll` 与 `mm1.WaitIterateAll()` 之间（异步发射区）。

**需要观察的现象**：`SetTensorB(..., true)` 恰好出现在 mm1 的两打（V、K）与 mm3 的两打 A 侧（dP、P 转置）；mm4 的 B（key）不带 true；mm1 第 2 打与 rope 打共用 `mm2WorkspaceGm` 输出但第二打多了累加标志。

**预期结果**：得到与 4.4.2 表格逐步对应的验证清单；若发现任何一步对不上，说明读的是别的模板分支（如 `S1s2Bn2` 用 `mm3_1`，宏名不同但公式同构）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 dV 用「P 转置乘 dY」而不是「dY 转置乘 P」？

**答案**：\( dV \) 的形状是 \([S2, D_v]\)，\( P^T \) 是 \([S2, S1]\)、\( dY \) 是 \([S1, D_v]\)，只有 \( P^T \cdot dY \) 形状正确。转置通过 `SetTensorA(dropWorkSpaceGm[...], true)` 的 `true` 完成，不需要真的搬运转置数据——cube 硬件在取数时按转置排布解释。

**练习 2**：`mm1Workspace` 与 `mm2Workspace` 为什么必须是 fp32？

**答案**：它们承载 \( dY V^T \) 与 \( QK^T \) 的原始累加值。softmax 重建要对 \( scale \cdot S_{raw} - max \) 取指数，bf16 的 8 位尾数会让远端小值全部下溢、行修正项 \( r \) 也会丢有效位；前向把 max/sum 存成 fp32（4.2.3 的 dtype 校验）正是为了让反向在这两个 workspace 上以 fp32 对齐精度。

**练习 3**：`basic_modules/vec_modules/` 下为什么有 `vec_sfmg.h` 与 `vec_sfmg_det.h` 两份？

**答案**：行修正项 \( r \) 是一个沿 D 与沿 S2 的归约。普通版允许把归约拆到多核再用原子/乱序合并（快但浮点加法顺序不定）；det 版按固定顺序、固定分块累加（结果可复现但慢）。模块化拆分让两种策略共享接口、按 `IsDeterministic` 模板参数选择（见 4.5）。

### 4.5 det 确定性版本：确定性/性能取舍

#### 4.5.1 概念说明

浮点加法不满足结合律：\( (a+b)+c \ne a+(b+c) \)。当一次归约（如 dQ/dK/dV 沿 S1/S2 的跨核累加）由多个核并发完成、合并顺序取决于核完成先后时，**同一输入两次运行会得到 bit 级不同的结果**——分布式训练里这会导致梯度校验、崩溃恢复、复现调试全部失效。PyTorch 用 `torch.use_deterministic_algorithms(True)` 声明「我宁可慢也要可复现」，CANN 则把这个意图经 `TilingContext::GetDeterministic()` 传到 tiling。

本算子的确定性支持分两层：

- **责任链层**：两个专用的 det tiling（优先级 1000 的 `FlashAttentionScoreGradEnhanceTilingDeterministic`、1002 的 `FlashAttentionScoreGraTilingBasicDet`），以及普通模板在 det 模式下**主动让位或改走保守分支**。
- **kernel 层**：`FlashAttentionScoreGradEnhanceBasicDet` 单模板（tilingKey 低位 `0x1999`），归约改为「每核写独立 workspace，再按固定顺序合并」。

#### 4.5.2 核心流程

```text
torch.use_deterministic_algorithms(True)
        │ 框架把 deterministic 标志传入 TilingContext
        ▼
责任链按优先级尝试：
  1000  TilingDeterministic      ── GetDeterministic()!=1 则 IsCapable=false（让位）
  1002  TilingBasicDet           ── IsCapable 七连检：det 开启 / 非 fp32 / TND / 无 pse /
  │                                 mask 兼容 / dropout 兼容 / shape 兼容
  ▼ 命中后
workspace 规划：为 dq/dk/dv 各开辟 cubeCoreNum × D 的 fp32 缓冲（每核独占一段）
        ▼
kernel（BasicDet）：CubeProcess（五次矩阵乘，逐 taskId 串行推进）
                  + VectorProcess（SoftmaxGrad 用 vec_sfmg_det 固定顺序累加）
                  + VecDetMainProcess（各核部分和按核号顺序合并 → 输出）
```

**取舍的本质**：非 det 版可以让多核对同一输出区间做原子累加（快、无额外显存）；det 版必须给每核留独立缓冲并在最后串行合并（多耗 `cubeCoreNum × D × sizeof(float)` 级别的 workspace，多一次合并 pass）。精度本身不变——变的是**结果的复现性**。

#### 4.5.3 源码精读

- [flash_attention_score_grad_enhance_tiling_s1s2_bn2gs1s2_basic_det.cpp:L153-L186](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_s1s2_bn2gs1s2_basic_det.cpp#L153-L186)：`IsCapable` 的约束清单（源码注释原文列了 6 条）：`GetDeterministic()==0` 直接让位；fp32 不支持；仅 TND；不支持 PSE；atten mask / drop mask / shape 各自的兼容性检查。**det 是能力子集而非超集**——为了可复现牺牲了适用范围。
- [flash_attention_score_grad_enhance_tiling_s1s2_bn2gs1s2_basic_det.cpp:L415-L431](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_s1s2_bn2gs1s2_basic_det.cpp#L415-L431)：确定性 workspace 规划——`GetDeterministic()==1` 时额外开辟 `DqDetWorkspaceOffset/DkDetWorkspaceOffset/DvDetWorkspaceOffset` 三块 `cubeCoreNum × GM_ALIGN × D × sizeof(float)` 的缓冲。**每核独占一行 D**，这是消除合并顺序不确定性的物质基础。
- [flash_attention_score_grad_enhance_tiling_s1s2_bn2.cpp:L2189-L2194](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_s1s2_bn2.cpp#L2189-L2194)：优先级 1000 的 `FlashAttentionScoreGradEnhanceTilingDeterministic` 注册（类定义在 [flash_attention_score_grad_enhance_tiling_s1s2_bn2.h:L146](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_s1s2_bn2.h#L146)，继承自 S1s2Bn2 并重写 `IsCapable` 强制 det 路径）。
- [flash_attention_score_grad_enhance.cpp:L1325-L1330](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1325-L1330)：kernel 侧的 det 分支选择——`UB0==9 && UB1==9 && Block==9 && IsDeterministic==1` 时 `REGISTER_TILING_FOR_TILINGKEY("TILING_KEY_VAR & 0xFFF = 0x1999", FlashAttentionGradBasicDetTilingData)` 并进入 `INVOKE_FAG_DETERMINISTIC_BASIC_IMPL`；**det 判断排在 basic 之前**，否则会被普通 basic 分支抢先命中（对比 [L1328-L1330](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1328-L1330) 的 basic 分支条件没有 IsDeterministic）。
- [flash_attention_score_grad_enhance.cpp:L1014-L1041](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/flash_attention_score_grad_enhance.cpp#L1014-L1041)：`INVOKE_FAG_DETERMINISTIC_BASIC_IMPL`——`FAG_TYPE<INPUT_TYPE, ..., SEQLEN_TYPE, DROP_ENABLE, DETERMINISTIC_ENABLE>` 把四个决策打包进类型，`FlashAttentionScoreGradEnhanceBasicDet::Process` 内部再按 `ASCEND_IS_AIC/AIV` 分流到 `CubeProcess`（`Cube12Process` + `Cube345Process` 交替推进，见 [flash_attention_score_grad_enhance_s1s2_bn2gs1s2_basic_det.h:L247-L263](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2_basic_det.h#L247-L263)）与 `VectorProcess`（`opPre → opSoftmaxGrad → VecDetMainProcess → vecPost`，见同文件 [L276-L328](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2_basic_det.h#L276-L328)）——后者正是 4.4 提到的 `vec_modules/*_det.h` 积木的消费现场。
- [npu_flash_attention_score_grad_enhance.md:L258-L259](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/docs/npu_flash_attention_score_grad_enhance.md#L258-L259)：调用示例中的 `torch.use_deterministic_algorithms(True)`——用户侧重启确定性开关的唯一入口。

#### 4.5.4 代码实践

**实践目标**：用最小实验感知「浮点顺序不定 → 结果 bit 级漂移」。

**操作步骤**（示例代码，CPU 即可）：

```python
import torch

torch.manual_seed(0)
x = torch.randn(10000, dtype=torch.float32)
# 模拟"多核并发归约"：把求和拆成乱序的两段
a = x[:5000].sum()
b = x[5000:].sum()
s1 = (a + b).item()
s2 = (b + a).item()          # 仅交换加法顺序
s3 = x.sum().item()          # torch 内部的归约顺序
print(f"{s1:.10f}\n{s2:.10f}\n{s3:.10f}")
print(s1 == s2, s1 == s3)
```

**需要观察的现象**：三个和在小数点后若干位开始不一致（`==` 可能返回 False）。差值极小，但**只要存在，两次训练的梯度就不可能 bit 级复现**——这正是 det 版用「独立 workspace + 固定顺序合并」来消灭的东西。

**预期结果**：多数随机种子下能看到末位差异；若某次恰好相等，换个种子再跑。运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：det 版为什么限制「仅 TND、不支持 PSE、非 fp32」？

**答案**：确定性实现要求每条归约路径都能被拆成「固定分块 + 固定合并顺序」。变长 TND 是训练主场景所以优先支持；PSE 会在打分矩阵上叠加非规则形状的编码，归约路径难以固定；fp32 的 det 收益低（本身精度高、且 fp32 训练场景少）。这是典型的「确定性先覆盖主路径」的工程取舍。

**练习 2**：普通（非 det）模板在 `GetDeterministic()==1` 时会怎样？

**答案**：以 s1s2_bn2 系模板为例（[flash_attention_score_grad_enhance_tiling_s1s2_bn2.cpp:L169-L170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_s1s2_bn2.cpp#L169-L170)），`IsCapable` 里 `GetDeterministic() != 1` 才通过——即 det 开启时普通模板让位，责任链自然落到优先级 1000/1002 的 det 模板。用户不需要换算子名，开关一开路由自动切换。

**练习 3**：det 版多出的三块 workspace 大小是多少（910B2、D=128 时量级）？

**答案**：每块 `cubeCoreNum × GM_ALIGN × D × sizeof(float)`。以 25 个 cube 核、GM_ALIGN 对齐、D=128 估算：`25 × 128 × 128 × 4B ≈ 1.6MB`，dq/dk/dv 三块合计约 5MB 量级——这就是确定性付出的显存代价（精确值取决于 `GM_ALIGN` 与实际 D，公式见 4.5.3 第二条链接）。

## 5. 综合实践

**任务：用 torch autograd 做小规模 FA 反向，逐项对照算子实现的九步流水线。**

这个实践把本讲全部知识串起来：你先用 PyTorch 的自动微分算出「标准答案」，再把它拆解成算子实现的每一步，验证两者是同一个公式的两种写法。

**步骤 1：写一个显式前向并让 autograd 求梯度**（示例代码，CPU 或 NPU 均可）：

```python
import torch

torch.manual_seed(42)
B, N, S1, S2, D = 1, 1, 64, 64, 32          # 小规模；S1==S2 对应 SameAb 模板场景
q = torch.randn(S1, D, requires_grad=True)
k = torch.randn(S2, D, requires_grad=True)
v = torch.randn(S2, D, requires_grad=True)
scale = D ** -0.5

def fa_forward(q, k, v):
    s = (q @ k.T) * scale                    # 步骤2: S_raw = Q·Kᵀ, scale 在前折算
    p = torch.softmax(s, dim=-1)             # 步骤4: 重建 P（autograd 会记录 max/sum）
    o = p @ v                                # 前向输出 == attention_in
    return o, p

o, p = fa_forward(q, k, v)
dy = torch.randn_like(o)
o.backward(dy)                               # autograd 的"标准答案": q.grad/k.grad/v.grad
```

**步骤 2：手写反向九步，与 autograd 对比**：

```python
dP_raw = dy @ v.T                                       # 步骤1: mm1 第1打
S_raw = (q @ k.T) * scale                               # 步骤2: mm1 第2打
r = (dy * o).sum(dim=-1, keepdim=True)                  # 步骤3: Sfmg, 用 attention_in
P = torch.softmax(S_raw, dim=-1)                        # 步骤4: SubGrapA 重建 P
dP = (dP_raw - r) * P                                   # 步骤5: SubGrapB
dQ = dP @ k                                             # 步骤6: mm4
dK = dP.T @ q                                           # 步骤7: mm3 第1打
dV = P.T @ dy                                           # 步骤8: mm3 第2打

print(torch.allclose(dQ, q.grad, atol=1e-5))
print(torch.allclose(dK, k.grad, atol=1e-5))
print(torch.allclose(dV, v.grad, atol=1e-5))
```

**步骤 3：与算子源码逐项对号**——为上面 8 行代码各写一行「kernel 位置」注记，直接引用 4.4.3 的行号链接（如 `dP_raw` ↔ [s1s2_bn2gs1s2.h:L2792-L2795](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_kernel/arch32/flash_attention_score_grad_enhance_s1s2_bn2gs1s2.h#L2792-L2795)）。

**步骤 4（有 NPU 环境时）**：把步骤 1 的前向换成仓库 ST 测试的调用方式（参见 `tests/st/test_flash_attention_score_grad_enhance.py`，及 [docs/npu_flash_attention_score_grad_enhance.md:L262-L275](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/docs/npu_flash_attention_score_grad_enhance.md#L262-L275) 的 `torch.ops.custom.npu_flash_attention_score_grad_enhance(...)` 示例），传入 CPU 参考实现算出的 `softmax_max`/`softmax_sum`/`attention_in`，比较 NPU 的 dq/dk/dv 与 CPU 手写反向的 MARE 误差（指标定义见 u8-l3）。

**预期结果**：步骤 2 的三个 `allclose` 全部为 `True`——这证明算子的九步流水线与 autograd 是同一组公式的展开；步骤 4 的 NPU 结果与 CPU 参考在 bf16 容差内一致（阈值参考 ST 用例）。CPU 部分运行结果待本地验证；NPU 部分依赖 u1-l3/u1-l4 搭好的环境。

## 6. 本讲小结

- **反向的题眼是「重获前向没存的东西」**：注意力矩阵 P 没有落盘，反向用 mm1 重算 \( QK^T \) + 前向保存的 `softmax_max`/`softmax_sum` 重建 P；softmax 行修正项则用 `attention_in` 直接算 \( \mathrm{rowsum}(dy \odot O) \)，等价于 \( \mathrm{rowsum}(dP \odot P) \) 但免除了对 P 的依赖。
- **五次矩阵乘 + 两次向量修正**：mm1 打两次（\( dY V^T \) 与 \( QK^T \)）、mm4 一次（\( dQ=dP\cdot K \)）、mm3 打两次（\( dK=dP^T Q \) 与 \( dV=P^T dY \)）；dP 本身由向量侧 `(dP_raw − r) ⊙ P` 得到，不是矩阵乘。转置全部藏在 `SetTensorA/B` 的 `true` 参数里。
- **tiling 入口三段式 + 九模板责任链**：校验（含 softmaxMax/Sum 必须 `(b,n1,s1,8)` 且 fp32 的共享校验函数）→ 空输入快路径（tilingKey=0 只清零）→ 按优先级 10/1000/1001/1002/1100/2000/10000/11000/15000/16000 调度切分模板；`TilingInputsDataDependency({12..16})` 保证变长元数据先物化。
- **kernel 入口是位域 → 模板参数的编译期分发**：`(UB0, UB1, Block, IsSameAB)` 元组选模板族，dtype/layout/对齐度/rope 再细分；执行统一组织为 Pre → (Sfmg) → 主计算 → Post 三段式，AIC:AIV=1:2 混合核分工。
- **basic_modules 是数值骨架与切分策略解耦的积木箱**：`CubeOp`（L1/L0 ping-pong 的矩阵乘流水）与 `VecOp`（SubGrapA 重建 P / SubGrapB 算 dP）让 basic 与 basic_det 共享实现；「对齐走高阶 API、不对齐手工展开」是通用范式。
- **确定性 = 固定归约顺序的物质代价**：det 版用每核独立 workspace + 固定顺序合并换取 bit 级复现，代价是额外显存、更窄的能力子集（仅 TND、无 PSE、非 fp32）；`torch.use_deterministic_algorithms(True)` 是用户侧唯一开关，责任链自动路由。

## 7. 下一步学习建议

- **u4-l5（SparseFlashAttention）**：本讲的五次矩阵乘骨架在稀疏场景下如何被索引张量裁剪——对照阅读能加深对「计算骨架不变、mask/索引前移」的理解。
- **u4-l6/u4-l7（LightningIndexer 家族）**：稀疏注意力训练链路里「谁来生产本算子消费的索引」，把 FA 反向放进完整训练步骤。
- **提前翻阅 `tests/ut/op_api/test_aclnn_flash_attention_score_grad_enhance.cpp`**：看两段式 aclnn 接口（u2-l5）如何为反向算子准备 `softmax_max`/`softmax_sum` 假数据，为 u8 单元的测试体系学习热身。
- **回看 u4-l3 的前向 kernel**：对比 `S1s2Bn2gs1` 前向与本文 `S1s2Bn2gs1s2` 反向的三级流水线组织，体会「前向省下的落盘」与「反向多出的重算」如何在不同模板间转移成本。
