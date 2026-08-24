# MHC 后处理算子:post、post_grad 与 mhc_post_grad

## 1. 本讲目标

本讲是 MHC(Manifold Constrained Hyper Connection,流形约束超连接)家族的第 4 讲。学完本讲,你应该能够:

1. 说清 post 前向算子在 MHC 层中的位置:它把「Sinkhorn 归一化后的双随机矩阵」和「Atten/MLP 层输出」汇聚成下一层输入。
2. 读懂 post 的两级 tiling(薄入口 + 切分实现)与 AIV 纯向量 kernel 的 CopyIn→Compute→CopyOut 流水。
3. **区分三个名字相近的算子**:`post`(前向)、`post_grad`(纯向量反向)、`mhc_post_grad`(AIC:AIV=1:2 混合核反向)。后两者在数学上完全等价,是「同一反向的两种实现策略」。
4. 说明 mhc_post_grad 如何在**一次 kernel 调用里聚合产出四路梯度**——AIV 侧算三路逐元素/归约梯度,AIC 侧用 Cube 矩阵乘算 grad_h_res。
5. 画出 MHC 训练一步的完整算子协作图(pre → 超连接计算 → post → sinkhorn → 反向链)。

## 2. 前置知识

在进入源码前,先回顾几个本讲要用到的概念(详细推导见 u5-l1~u5-l3)。

- **MHC 层的数据流**:pre 算子(u5-l3)对 n 路残差输入做 RMS 归一化并做一次融合矩阵乘,产出 `h_in`、`H_post`、以及交给 Sinkhorn 的 `H_res`;Atten/MLP 在 `h_in` 上算出 `h_out`;最后由本讲的 **post 算子**把三样东西合成下一层输入。
- **双随机矩阵(doubly stochastic matrix)**:每行、每列元素之和都为 1 的方阵。Sinkhorn 迭代(u5-l1)负责把 `H_res` 逼成双随机矩阵,post 直接消费这个结果。
- **AIV 与 AIC**:昇腾 AI Core 中的两类核。AIV(Vector)擅长逐元素运算与归约;AIC(Cube)擅长矩阵乘。一个算子声明为 `KERNEL_TYPE_AIV_ONLY` 时 AIC 核直接空转;声明为 `KERNEL_TYPE_MIX_AIC_1_2` 时按 1 个 AIC 配 2 个 AIV 的比例混合调度。
- **两级 tiling**:`_tiling_base.cpp` 是薄入口,只做 `IMPL_OP_OPTILING` 注册并把调用转给 `TilingRegistry`;`_tiling.cpp` 里继承 `TilingBase` 的子类负责真正的校验与切分(u3-l3 讲过这套责任链框架)。
- **tilingKey 与对齐标志**:Host 侧 tiling 写入、Device 侧 kernel 读取的分支信号。本讲三个算子都用一组「对齐标志」做编译期模板选择,而不是传统的 tilingKey 位域。

一个符号约定:本讲所有公式中 \( T = B \times S \)(变长场景下 T 为各 batch 序列长度累加和),n 为超连接的路数(支持 4、6、8),D 为 head 维度。

## 3. 本讲源码地图

三个算子目录都在 `ascendc/src/ops-transformer/mhc/` 下:

| 文件 | 作用 |
| --- | --- |
| `ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_def.cpp` | post 前向原型注册(4 输入 1 输出) |
| `ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.h` | post 的 TilingData 结构(16 字段) |
| `ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling_base.cpp` | post 两级 tiling 的薄入口 |
| `ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp` | post 切分实现(校验 + UB 预算 + 对齐标志) |
| `ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.cpp` | post kernel 入口(8 路模板分发) |
| `ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.h` | post kernel 模板类(ComputeTile 核心计算) |
| `ai_infra_manifold_constrained_hyper_connection_post_grad/op_host/ai_infra_manifold_constrained_hyper_connection_post_grad_def.cpp` | post_grad 原型(5 输入 4 输出) |
| `ai_infra_manifold_constrained_hyper_connection_post_grad/op_kernel/ai_infra_manifold_constrained_hyper_connection_post_grad.cpp/.h` | post_grad 纯向量反向 kernel |
| `ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.h` | mhc_post_grad 的 TilingData(多出 AIC 分核与 matmulTiling) |
| `ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.cpp` | mhc_post_grad 切分实现(AIC/AIV 双轨分核 + Cube tiling) |
| `ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.cpp/.h` | mhc_post_grad 混合核反向(AIV 三路 + AIC 矩乘一路) |
| `ai_infra_mhc_post_grad/tests/st/test_ai_infra_mhc_post_grad.py` | ST 精度测试,内含 fp64 golden 公式 |
| `ai_infra_mhc_post_grad/docs/npu_ai_infra_mhc_post_grad.md` | mhc_post_grad 的 npu 接口文档 |
| `ai_infra_mhc_post_grad/docs/aclnnAiInfraMhcPostGrad.md` | mhc_post_grad 的 aclnn 两段式接口文档 |

三份 npu 文档的调用示例互相印证了 MHC 家族的统一调用方式(`torch.ops.custom.npu_*`)。注意:`post_grad` 文档「函数原型」一节写的是 `torch.ops.custom.npu_manifold_constrained_hyper_connection_post_grad`(缺 `ai_infra` 前缀),而同文件「调用示例」与 aclnn 文档都是全名——以示例和源码为准,这又是一处「文档可能滞后」的实例(见 u2-l5 的同类结论)。

## 4. 核心概念与源码讲解

### 4.1 post 前向:残差汇聚的数学与原型

#### 4.1.1 概念说明

MHC 层的输出侧要做两件事:

1. **Post Mapping**:把 Atten/MLP 的输出 \( h_l^{out} \)(shape [T,D])按每路的权重 \( H_t^{post} \)(shape [T,n])放大复制到 n 路;
2. **ResMapping**:把本层输入 \( x_l \)(shape [T,n,D])经双随机矩阵 \( H_l^{res} \)(shape [T,n,n])重新混合。

二者残差相加得到下一层输入 \( x_{l+1} \)(shape [T,n,D])。文档给出的公式:

\[
x_{l+1} = (H_{l}^{res})^{T} \times x_l + h_{l}^{out} \otimes H_{t}^{post}
\]

按 head 展开,等价的逐元素写法是:

\[
x_{l+1}[t,i,d] = \sum_{j=0}^{n-1} H_{l}^{res}[t,j,i] \cdot x_l[t,j,d] + H_{t}^{post}[t,i] \cdot h_{l}^{out}[t,d]
\]

注意 \( H^{res} \) 的索引是 **[j,i]**(转置方向),这是后面读 kernel 代码时最容易看错的一个点。

由于 \( n \le 8 \)、矩阵极小,这里的「矩阵乘」并不需要 Cube:每个 d 位置只是 n 个标量加权和,向量核的 Muls/Axpy 原语恰好够用——这解释了 post 为什么选 AIV_ONLY。

#### 4.1.2 核心流程

```
输入: x[T,n,D](bf16/fp16)、h_res[T,n,n](fp32)、h_out[T,D](bf16/fp16)、h_post[T,n](fp32)
对每个 token t(一个 item):
    取出 h_res[t] (n×n)、h_post[t] (n)
    对 D 的每个 tile:
        取 h_out[t, tile]、x[t, :, tile]
        对每路 i in [0,n):
            out = h_post[i] * h_out[tile]           # Muls
            out += Σ_j h_res[j,i] * x[j, tile]      # n 次 Axpy
            写 output[t,i,tile]
输出: output[T,n,D](bf16/fp16)
```

#### 4.1.3 源码精读

先看原型定义。post 的 `_def.cpp` 声明了 4 个必选输入和 1 个输出:

- [ai_infra_manifold_constrained_hyper_connection_post_def.cpp:L24-L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_def.cpp#L24-L52)——`x`/`h_out` 允许 BF16 或 FP16,`h_res`/`h_post` 强制 FP32,输出与 `x` 同 dtype。四路输入全部 `AutoContiguous()`,框架保证进入 kernel 的张量内存连续。
- [ai_infra_manifold_constrained_hyper_connection_post_def.cpp:L61-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_def.cpp#L61-L62)——`AICore().AddConfig` 同时注册 `ascend910_93` 与 `ascend910b`,即 A3/A2 双芯片白名单。

再看 kernel 的核心计算(完整走读见 4.3):

- [ai_infra_manifold_constrained_hyper_connection_post.h:L306-L320](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.h#L306-L320)——`ComputeTile` 的主循环:`Muls(outF32, hOutF32, hPostLocal.GetValue(i), ...)` 实现 \( h_{post}[i]\cdot h_{out} \);内层 `for j` 循环用 `Axpy(outF32, xF32[j*tileD], hResJI, ...)` 累加 \( \sum_j h_{res}[j,i]\cdot x[j] \)。第 314 行 `hResLocal.GetValue(j * n_ + i)` 正是 [j,i] 转置索引,与公式一致。

最后用一个能独立运行的对照实现印证。ST 测试目录里的 golden 前向(属测试代码,可在 CPU 上复现):

- [test_ai_infra_mhc_post_grad.py:L152-L155](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/tests/st/test_ai_infra_mhc_post_grad.py#L152-L155)——`hc_post_forward` 用广播语义写出同一个公式:`h_post.unsqueeze(-1) * h_out.unsqueeze(-2) + torch.sum(h_res.unsqueeze(-1) * x.unsqueeze(-2), dim=-3)`,`dim=-3` 沿 j 轴求和,与 kernel 的 Axpy 链一一对应。

#### 4.1.4 代码实践

**实践目标**:用 PyTorch 在 CPU 上验证你对 post 前向公式的理解,并与「朴素 for 循环版本」对比。

1. 阅读上面的 `_def.cpp` 与 `ComputeTile` 代码。
2. 编写下面这样的脚本(示例代码,依赖 torch,可在任意有 PyTorch 的机器上运行):

```python
import torch

torch.manual_seed(0)
T, n, D = 8, 4, 64
x     = torch.randn(T, n, D, dtype=torch.float16)
h_res = torch.randn(T, n, n, dtype=torch.float32)
h_out = torch.randn(T, D,    dtype=torch.float16)
h_post= torch.randn(T, n,    dtype=torch.float32)

# 版本 A:向量化(等价 ST 测试的 hc_post_forward)
y_a = (h_post.unsqueeze(-1) * h_out.unsqueeze(-2).float()
       + torch.sum(h_res.unsqueeze(-1) * x.unsqueeze(-2).float(), dim=-3)).half()

# 版本 B:逐元素 for 循环(等价 kernel ComputeTile 的 Muls+Axpy 链)
y_b = torch.zeros(T, n, D, dtype=torch.float16)
for t in range(T):
    for i in range(n):
        acc = h_post[t, i] * h_out[t].float()          # Muls
        for j in range(n):                              # Axpy × n
            acc += h_res[t, j, i].item() * x[t, j].float()
        y_b[t, i] = acc.half()

print("max diff:", (y_a.float() - y_b.float()).abs().max().item())
```

3. **需要观察的现象**:两个版本的 `max diff` 应为 0 或 1 个 fp16 量化台阶(纯加法/乘法顺序不同可能引入极微小的舍入差)。
4. **预期结果**:输出接近 0,证明 [j,i] 转置索引方向理解正确。如果你把 `h_res[t, j, i]` 误写成 `h_res[t, i, j]`,diff 会明显变大——这也是本实践最有价值的「错误对照组」。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `h_res`/`h_post` 强制 FP32,而 `x`/`h_out` 允许 BF16/FP16?

**参考答案**:权重类数据(`h_res` 的 n×n 系数、`h_post` 的 n 个系数)在反向和 Sinkhorn 迭代中会被反复消费,精度损失会被放大;而激活类数据(`x`/`h_out`)体量大(T×n×D),半精度存储能显著省带宽和显存。kernel 内部统一 `Cast` 到 FP32 计算、写出时再 `Cast` 回半精度,兼顾精度与访存量。

**练习 2**:post 前向的输出 shape 为什么是 [T,n,D] 而不是 [T,D]?

**参考答案**:因为 ResMapping 分支 \( (H^{res})^T x_l \) 对每一路 i 都产出一个 D 维向量,n 路都要保留,交给下一层继续作为 n 路残差输入;[T,D] 的只有 Post Mapping 分支的源头 `h_out`。

### 4.2 post 的两级 tiling:16 字段契约与对齐三标志

#### 4.2.1 概念说明

post 的 tiling 沿用 u5-l1 讲过的两级组织,但与 Sinkhorn 有一处不同:它是**单实现**——整个 `_tiling.cpp` 只注册一个模板(优先级 2000),没有责任链接力。它要回答的问题有三个:

1. **分核**:T 个 token 怎么分给 AIV 核?
2. **分 tile**:D 维一次放不进 UB 怎么办?
3. **对齐判定**:n、n²、D 是否满足向量指令的 32 字节对齐?决定 kernel 走快路径还是慢路径。

其中第 3 点是本算子的特色:对齐结果不编码进 tilingKey 位域,而是作为 TilingData 的三个普通字段下传,kernel 入口再把它们组装成 3 位模板选择键。

#### 4.2.2 核心流程

```
tiling_base.cpp(薄入口):
    IMPL_OP_OPTILING(AiInfraManifoldConstrainedHyperConnectionPost)
        → TilingRegistry::GetInstance().DoTilingImpl(context)   # 转发责任链
tiling.cpp(唯一模板,优先级 2000):
    GetPlatformInfo     # 取 AIV 核数与 UB 大小
    GetShapeAttrsInfo   # 取 x 的 shape(4D→BSND / 3D→TND)
    CheckParam          # 空指针/ dtype / shape 一致性 / 规格五连检
    ComputeTiling       # 分核 + UB 预算求 tileD + 对齐三标志
    PostTiling          # SetBlockDim(usedCores) + 序列化 TilingData
    GetTilingKey        # 恒返回 0
```

规格校验:`totalItems ∈ (0, 512K]`、`n ∈ {4,6,8}`、`D ∈ [384, 24576]`。

UB 预算公式(每 tileD 元素的字节数,推导自 kernel 各队列宽度):

\[
\text{bytesPerTileD} = \underbrace{(2n+2)\times 2}_{\text{bf16 队列}} + \underbrace{(2n+4)\times 4}_{\text{fp32 中间缓冲}} = 12n + 20
\]

于是 \( \text{tileD}_{max} = \lfloor (\text{UB} - \text{smallBufferBytes}) / (12n+20) \rfloor \),再向下对齐到 16。若 \( \text{alignedD} \le \text{tileD}_{max} \) 则单 tile 装下;否则从 maxTileD 往下找一个能整除 alignedD 的值。

对齐三标志(这是贯穿三个算子的同一套约定):

| 标志 | 判定 | n=4 | n=6 | n=8 |
| --- | --- | --- | --- | --- |
| `isNAligned` | n 满足 fp32 对齐(见下文差异) | 0 | 0 | 1 |
| `isNNAligned` | (n×n) % 8 == 0 | 1(16) | 0(36) | 1(64) |
| `isDAligned` | D % 16 == 0 且单 tile | 看 D | 看 D | 看 D |

fp32 向量指令要求 32 字节(8 元素)对齐。注意一个细节:post 前向 tiling 里 `isNAligned` 的判定是 `n_ % FLOAT32_ALIGN_SIZE == 0`(即 n%8),所以 n=4 时为 0;而两个反向算子改成了 `n_ == 8`——n=4 时同样为 0。对本仓库支持的 n∈{4,6,8},两种写法结果一致,只是表达方式不同。

#### 4.2.3 源码精读

- [ai_infra_manifold_constrained_hyper_connection_post_tiling_base.cpp:L35-L38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling_base.cpp#L35-L38)——薄入口的全部内容:`IMPL_OP_OPTILING` 绑定 Tiling/TilingParse,`TilingFor...` 一行转给 `TilingRegistry::DoTilingImpl`。
- [ai_infra_manifold_constrained_hyper_connection_post_tiling.h:L24-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.h#L24-L41)——16 字段 TilingData:`totalItems/itemsPerCore/remainderItems/usedCores` 描述分核,`S/n/D/tileD/nTilesD/alignedD/lastTileD` 描述 D 维切分,`alignedN/alignedNN` 是补齐后的拷贝宽度,`isNAligned/isNNAligned/isDAligned` 是模板分支信号。
- [ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp:L283-L312](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp#L283-L312)——`CheckShapeConsistency` 按 dimNum 分派:4 维按 BSND 解析出 B/S/n/D 并令 `totalItems = B*S`;3 维按 TND 解析,`B_=S_=1` 仅占位。随后逐一核对 h_res/h_out/h_post/output 的形状(完整校验延续到 L445)。
- [ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp:L451-L469](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp#L451-L469)——规格三连检:totalItems 上限、n 枚举、D 区间。注意第 42 行常量 `MAX_TOTAL_ITEMS = 512*1024*4`(注释 BS max 2M),比文档宣称的 512k 宽 4 倍,两个反向算子则用 `512*1024`——同一文档口径下三个算子源码不一致,**以哪个为准待确认**,保守起见按文档 512k 使用。
- [ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp:L494-L554](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp#L494-L554)——`ComputeTiling` 主体:先 `usedCores = min(totalItems, AIV核数)`、带 remainder 的均分(L497-L500);再按 12n+20 公式求 maxTileD(L514-L531);多 tile 时优先找整除 alignedD 的 tileD(L543-L548)。
- [ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp:L563-L588](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp#L563-L588)——对齐三标志计算与 TilingData 逐字段回填。
- [ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp:L635-L645](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp#L635-L645)——`PostTiling`:`SetBlockDim(usedCores_)` + workspace 回填 + `SaveToBuffer` 序列化;`GetTilingKey` 恒返回 0(L647-L650)。
- [ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp:L668-L669](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp#L668-L669)——单模板注册,优先级 2000(与 Sinkhorn、两个反向算子相同)。

#### 4.2.4 代码实践

**实践目标**:不开 NPU,手工推演一组真实参数的 tiling 结果,并核对 kernel 侧消费方式。

1. 设 `n=6, D=5000, T=4096`,UB 大小按 910B 类芯片典型值 196608 字节估算(准确值依芯片型号,**待本地验证**,可用 UT 打印确认)。
2. 手算:alignedN = AlignUp(6,8) = 8;alignedNN = AlignUp(36,8) = 40;bytesPerTileD = 12×6+20 = 92;smallBufferBytes = (8+40)×4 = 192;maxTileD = (196608−192)/92 ≈ 2134 → AlignDown 到 16 的倍数 = 2128;alignedD = AlignUp(5000,16) = 5008 > 2128,故多 tile:nTilesD = CeilDiv(5008, 2128) = 3,post 前向还会尝试把 tileD 降到能整除 5008 的值;isNAligned=0, isNNAligned=0(36%8=4), isDAligned=0(多 tile)。
3. 对照源码核对你每一步用的公式行号:`AlignUp` 在 [ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp:L53-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_tiling.cpp#L53-L56)。
4. **需要观察的现象**:模板键 `(0<<2)|(0<<1)|0 = 0`,即 kernel 入口会落入「最慢路径」分支。
5. **预期结果**:与 4.3 节 kernel 入口的 case 0 分支对应。若把 n 换成 8、D 换成 5120,则三标志全 1,落入 case 7 最快路径——同一算子不同 shape 的性能差异在 tiling 阶段就已注定。

#### 4.2.5 小练习与答案

**练习 1**:`itemsPerCore`/`remainderItems` 为什么要成对出现?

**参考答案**:T 不一定能被核数整除。方案是前 `remainderItems` 个核各多干 1 个 item(`myItemCount = itemsPerCore+1`),其余核干 `itemsPerCore` 个。kernel 侧用 `blockIdx_ < remainderItems_` 二分反推自己的起点(见 4.3 的 Init 代码),这与 Host 侧的乘法严格互逆,漏改任何一侧都会导致 token 重复或遗漏。

**练习 2**:为什么 `alignedN`/`alignedNN` 要单独存进 TilingData,而不是 kernel 里现算?

**参考答案**:Host 侧算一次,所有核、所有 tile 复用,省掉设备上的重复除法/分支;更重要的是它同时决定了「分配多宽的 UB 队列」与「用 Duplicate 清零到什么宽度」,两处必须同源,放进 TilingData 是最不易失同步的做法。

### 4.3 post 前向 kernel 与 post_grad:纯向量路线

#### 4.3.1 概念说明

post 前向 kernel 与 post_grad 反向 kernel 共享同一套骨架(AIV_ONLY + 8 路对齐模板 + item/tile 双层循环),所以放在一起讲。前者是后者的热身,后者是本模块主角。

反向的数学:对前向 \( x_{l+1}[i] = \sum_j H^{res}[j,i]\,x_l[j] + h_{post}[i]\cdot h_{out} \),给定上游梯度 \( g = \partial L/\partial x_{l+1} \)(shape [T,n,D]),链式法则给出四路梯度:

\[
\text{grad\_x}[t,j,d] = \sum_{i} H^{res}[t,j,i] \cdot g[t,i,d]
\]

\[
\text{grad\_h\_res}[t,j,i] = \sum_{d} x_l[t,j,d] \cdot g[t,i,d]
\]

\[
\text{grad\_h\_out}[t,d] = \sum_{i} H^{post}[t,i] \cdot g[t,i,d]
\]

\[
\text{grad\_h\_post}[t,i] = \sum_{d} g[t,i,d] \cdot h_{out}[t,d]
\]

观察计算形态:grad_x、grad_h_out 是「加权和」(Muls/Axpy 擅长);grad_h_res、grad_h_post 是**长为 D 的内积**(归约)。post_grad 把四路全部塞进 AIV:前两类用 Axpy 链,后两类用 Mul + `HierarchicalReduceSum`。其中 grad_h_res 最重——每个 token 要做 n×n 次 D 长度内积,这正是 4.4 节 mhc_post_grad 要用 Cube 拆走的负担。

#### 4.3.2 核心流程

post 前向 kernel 入口:

```
extern "C" __global__ __aicore__ void ...(x, hRes, hOut, hPost, output, workspace, tiling)
    KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)   # 纯向量核
    if (g_coreType == AIC) return                     # AIC 核直接退出
    GET_TILING_DATA(tilingData, tiling)               # 解包 16 字段
    templateKey = (isNAligned<<2)|(isNNAligned<<1)|isDAligned
    switch(templateKey): 8 个分支实例化 Kernel<T, N, NN, D>
    op.Init(...) → op.Process()
```

post_grad 的 `Process` 主循环(每个 item):

```
CopyInWeights     # h_post/h_res 进 UB(慢路径先 Duplicate 清零再 DataCopyPad)
ComputeInitGrads  # gradHPost、gradHRes 缓冲清零(post 前向无此步)
for tileId in [0, nTilesD):
    CopyInTile    # g/h_out/x 的 tile 进 UB
    ComputeTile   # 四路梯度计算(见 4.3.1 公式)
    CopyOutGradHOut / CopyOutGradX   # 拆开两段写回,便于流水重叠
CopyOutWeightGrads  # gradHPost/gradHRes 跨 tile 累加完毕后一次写出
```

`HierarchicalReduceSum` 的分层归约策略:长度对齐到 8 → 反复「后半加到前半」直到 ≤64 → `WholeReduceSum` 收尾。相比一次超长 ReduceSum,分块对齐的累加顺序能避免大数吃小数的精度损失。

#### 4.3.3 源码精读

- [ai_infra_manifold_constrained_hyper_connection_post.cpp:L23-L32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.cpp#L23-L32)——前向入口:入口参数顺序(x, hRes, hOut, hPost, output)与 `_def.cpp` 的 Input 声明顺序一致;`KERNEL_TYPE_AIV_ONLY` + AIC 早退;`GET_TILING_DATA` 解包。
- [ai_infra_manifold_constrained_hyper_connection_post.cpp:L58-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.cpp#L58-L65)——读三个对齐标志,组装 `templateKey = (isNAligned<<2)|(isNNAligned<<1)|isDAligned`。注意前向用 switch 写法,注释里逐 case 标注了典型(n,D)组合(如 case 3 对应 n=4,D=2560)。
- [ai_infra_manifold_constrained_hyper_connection_post.h:L140-L146](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.h#L140-L146)——remainder 分核的反解:`blockIdx_ < remainderItems_` 决定本核 item 数与起点,与 tiling 的 L497-L500 互逆。
- [ai_infra_manifold_constrained_hyper_connection_post.h:L61-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.h#L61-L73)——队列布局:数据队列(hOut/x/output)开 2 深 Double Buffer,权重队列(hPost/hRes)单缓冲(item 级复用),另有 4 个 fp32 中间 TBuf。
- [ai_infra_manifold_constrained_hyper_connection_post.h:L177-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.h#L177-L192)——前向 `Process`:item 外循环 → CopyInWeights → tile 内循环(CopyIn/Compute/CopyOut)→ CopyOutWeights(实际只是归还权重队列)。
- [ai_infra_manifold_constrained_hyper_connection_post.h:L302-L304](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_kernel/ai_infra_manifold_constrained_hyper_connection_post.h#L302-L304)——半精度输入先 `Cast` 到 fp32 再计算,精度策略在类型系统之外由代码显式控制。

反向侧:

- [ai_infra_manifold_constrained_hyper_connection_post_grad_def.cpp:L24-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/op_host/ai_infra_manifold_constrained_hyper_connection_post_grad_def.cpp#L24-L73)——post_grad 原型:`grad_output` 作为第 0 号输入前置,四个输出 `grad_x`(bf16/fp16)、`grad_h_res`(fp32)、`grad_h_out`(bf16/fp16)、`grad_h_post`(fp32)。
- [ai_infra_manifold_constrained_hyper_connection_post_grad.cpp:L27-L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/op_kernel/ai_infra_manifold_constrained_hyper_connection_post_grad.cpp#L27-L31)——同样声明 `KERNEL_TYPE_AIV_ONLY`,AIC 核早退。**这是它与 mhc_post_grad 的第一个分水岭**(后者见 4.4)。
- [ai_infra_manifold_constrained_hyper_connection_post_grad.h:L417-L431](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/op_kernel/ai_infra_manifold_constrained_hyper_connection_post_grad.h#L417-L431)——`ComputeTile` 梯度核心:外层 i 循环同时推进三件事——Axpy 累加 grad_h_out(第 420 行)、Mul+归约算 grad_h_post[i](第 422-L424 行)、内层 j 循环 Mul+归约算 grad_h_res[i,j](第 426-L430 行,共 n² 次归约)。
- [ai_infra_manifold_constrained_hyper_connection_post_grad.h:L450-L459](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/op_kernel/ai_infra_manifold_constrained_hyper_connection_post_grad.h#L450-L459)——grad_x 的计算:`Muls` 打底 + n−1 次 `Axpy`,hRes 此处用 [i,j] 正向索引(与前向的 [j,i] 互为转置,数学上正是 \( H \) 与 \( H^T \) 的关系)。
- [ai_infra_manifold_constrained_hyper_connection_post_grad.h:L560-L578](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/op_kernel/ai_infra_manifold_constrained_hyper_connection_post_grad.h#L560-L578)——`HierarchicalReduceSum` 实现起点:小计数直接 ReduceSum,大计数走折半累加 + WholeReduceSum(完整实现延续到 L630 附近)。

#### 4.3.4 代码实践

**实践目标**:数清楚 post_grad 在一个 token 内要做多少次「D 长度归约」,为 4.4 的对比做铺垫。

1. 打开 [ai_infra_manifold_constrained_hyper_connection_post_grad.h:L417-L431](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/op_kernel/ai_infra_manifold_constrained_hyper_connection_post_grad.h#L417-L431),统计 `HierarchicalReduceSum` 的调用次数与循环层级。
2. 分别对 n=4 与 n=8 计算每 token 每 tile 的归约次数:grad_h_post 贡献 n 次,grad_h_res 贡献 n² 次。
3. **需要观察的现象**:总归约次数 = n + n²,n=8 时为 72 次/tile;而 Muls/Axpy 类调用只有 O(n) 次。归约占了绝对主体。
4. **预期结果**:你会得出「post_grad 的算力瓶颈在 n² 次 D 长度内积」的结论——这正是 mhc_post_grad 把这一路搬去 Cube 的动机。无需 NPU,纯静态分析即可完成。

#### 4.3.5 小练习与答案

**练习 1**:post_grad 的 `ComputeInitGrads` 为什么要把 gradHPost、gradHRes 清零,而前向 post 没有对应步骤?

**参考答案**:这两个梯度要跨 tile 累加——每个 D-tile 贡献一部分内积和(见 L443-L446 的 `Add` 累加),所以 item 开始前必须清零、所有 tile 跑完才能写出。前向的输出每个 tile 独立自洽,无需跨 tile 状态。

**练习 2**:kernel 里 `Cast(gradHOutTile, accumLocal, RoundMode::CAST_RINT, ...)`,为什么用 RINT(四舍五入到最近偶数)而不是截断?

**参考答案**:fp32 转 bf16/fp16 时 RINT 是无偏舍入,大量元素求和后的统计误差趋近于零;截断则始终朝负方向偏,梯度累加千步后会引入系统性漂移。u2-l4 的 aggregate_hidden 也用了同样的收尾 Cast。

### 4.4 mhc_post_grad:AIC:AIV=1:2 混合核聚合四路梯度

#### 4.4.1 概念说明

先给一个重要结论,纠正一个容易产生的误会:**mhc_post_grad 不是数学上不同的另一个反向算子**。对比两份 `_def.cpp`(post_grad 的 [L20-L85](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/op_host/ai_infra_manifold_constrained_hyper_connection_post_grad_def.cpp#L20-L85) 与 mhc_post_grad 的 [L20-L85](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_def.cpp#L20-L85)),输入输出、dtype 约束、芯片配置逐行相同;两份 npu 文档的公式也一字不差。二者是**同一反向的两种实现**:

| 维度 | post_grad | mhc_post_grad |
| --- | --- | --- |
| 数学 | 四路梯度公式相同 | 相同 |
| 核类型 | `KERNEL_TYPE_AIV_ONLY` | `KERNEL_TYPE_MIX_AIC_1_2` |
| grad_h_res 路线 | AIV:n² 次 Mul+分层归约 | AIC:Cube 矩阵乘 \( x \cdot g^{T} \) |
| 平台探测 | `GetCoreNumAiv()` | `GetCoreNumAic()` |
| TilingData | 16 字段 | 18 字段 + `TCubeTiling` |
| tilingKey | 恒 0 | 恒 0 |

「聚合多路梯度」的真正含义:mhc_post_grad **一次 kernel 调用、两类核并行**地产出全部四路梯度——AIV 侧负责逐元素形态的三路(grad_x、grad_h_out、grad_h_post),AIC 侧独占矩阵乘形态的一路(grad_h_res)。两类核各拿各的 item 分配表,互不等待,这是 1:2 混合核的典型用法(u4-l3 的 FA、u4-l6 的 LightningIndexer 同款)。

为什么 grad_h_res 值得上 Cube?把 \( \text{grad\_h\_res}[t] = x_l[t] \cdot g[t]^{T} \)(n×D 乘 D×n 得 n×n)对 t 批量看,是一串 M=N=8、K=D 的瘦高矩阵乘;Cube 的 Mmad 恰好把 K 维乘加折叠进硬件,K 越长优势越大(D 可到 24k),而向量核做同样的事需要 n² 趟全宽度归约。

#### 4.4.2 核心流程

tiling 侧(与 post_grad 的差异点):

```
GetPlatformInfo: 取 AIC 核数(不是 AIV!)
ComputeTiling:
    usedAic_  = min(totalItems, AIC 核数)
    usedCores_= min(totalItems, AIC 核数 × 2)      # 1:2 混合比
    两套 itemsPerCore/remainder:AIV 用一套,AIC 用一套
    matmulTiling: M=N=n, Ka=Kb=D,
        singleCoreK = AlignDown(D/4, 16)            # K 方向拆 4 次累加
        baseM/baseN/baseK 按 L0A/L0B 各 64KB 双缓冲推算
PostTiling: SetBlockDim(usedAic_)                    # blockDim 按 AIC 组计
```

kernel 侧:

```
Init:
    if ASCEND_IS_AIV: AIV 分配表 + 初始化 UB 队列(与 post_grad 同构,但无 x 队列)
    if ASCEND_IS_AIC: AIC 分配表 + mm.Init(&matmulTiling)
Process:
    AIV: for item → CopyInWeights → ComputeInitGrads
         → for tile → CopyInTile → ComputeTile(三路梯度)→ CopyOutGradHOut/GradX
         → CopyOutWeightGrads(grad_h_post)
    AIC: for item → SetMMParaAndCompute(grad_h_res)
SetMMParaAndCompute:
    for offsetK in range(0, D, singleCoreK):
        mm.SetSingleShape(n, n, curK)
        mm.SetTensorA(x[item, offsetK:])            # A = x 的 K 段
        mm.SetTensorB(gradOutput[item, offsetK:], true)  # B = g 的 K 段,转置
        mm.IterateAll(gradHRes[item], offsetK==0 ? 0 : 1)  # 非首段累加
    mm.End()
```

注意 `IterateAll` 的最后一个参数:第 0 段是覆盖写,后续段是累加——这是 K 维分块的标准接力写法。

#### 4.4.3 源码精读

- [ai_infra_mhc_post_grad.cpp:L23-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.cpp#L23-L28)——入口签名:9 个业务地址(5 输入 4 输出)+ workspace + tiling;第 28 行 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 声明混合核,**没有** `if (g_coreType == AIC) return` 的早退——AIC 核要走自己的分支。
- [ai_infra_mhc_post_grad.cpp:L45-L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.cpp#L45-L86)——与 post 前向的 switch 等价的 if-else 链:同样的三个对齐标志、8 组模板实例化。
- [ai_infra_mhc_post_grad_tiling.h:L24-L44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.h#L24-L44)——TilingData 在 16 个同名字段之外,新增 `itemsPerAic`/`remainderItemsAic`(AIC 侧分配表)与 `TILING_DATA_FIELD_DEF_STRUCT(TCubeTiling, matmulTiling)`(Cube 的 M/N/K/baseM 等全套)。
- [ai_infra_mhc_post_grad_tiling.cpp:L139-L153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.cpp#L139-L153)——`GetPlatformInfo` 用 `GetCoreNumAic()` 取核数(post_grad 用 AIV 数),这是两套分配表的源头。
- [ai_infra_mhc_post_grad_tiling.cpp:L633-L642](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.cpp#L633-L642)——双轨分核:`usedCores_` 按 AIC 核数的 2 倍截断(注释:AIV 的核数是 AIC 的两倍),`usedAic_` 按 AIC 核数截断,各自算 itemsPerCore/remainder。
- [ai_infra_mhc_post_grad_tiling.cpp:L719-L748](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.cpp#L719-L748)——`matmulTiling` 配置:M=N=n、Ka=Kb=D;`singleCoreK = AlignDown(D/4, 16)` 把 K 拆约 4 段;`baseN/baseK/baseM` 由 L0A/L0B 各 64KB、双缓冲、2 字节元素推算;`depthA1/depthB1/dbL0A/dbL0B/dbL0C` 全部开 2 份双缓冲。
- [ai_infra_mhc_post_grad_tiling.cpp:L794-L805](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.cpp#L794-L805)——`PostTiling` 用 `SetBlockDim(usedAic_)`:1:2 混合核的 blockDim 以「AIC 组」为单位,每组自动带 2 个 AIV。
- [ai_infra_mhc_post_grad.h:L102-L105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L102-L105)——kernel 成员里的矩阵乘对象:`MatmulType<GM, ND, T>` × 2(B 侧 `true` 表示转置)+ fp32 C 型,`matmul::MatmulImpl` 直接在 GM 上取数。
- [ai_infra_mhc_post_grad.h:L178-L221](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L178-L221)——`Init` 的双轨:`if ASCEND_IS_AIV` 分支做 AIV 分配表与队列初始化;`if ASCEND_IS_AIC` 分支做 AIC 分配表并 `mm.Init(&tilingData->matmulTiling, pipe_)`。同一份 TilingData 被两类核按需取用。
- [ai_infra_mhc_post_grad.h:L225-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L225-L257)——`Process` 双轨:AIV 走 CopyIn/Compute/CopyOut 流水(三个梯度),AIC 只有一个循环调 `SetMMParaAndCompute`(第四个梯度)。没有跨核同步——四路梯度写入四个不相交的输出张量,天然无竞争。
- [ai_infra_mhc_post_grad.h:L390-L398](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L390-L398)——AIV 侧的 grad_h_out(Axpy 累加 hPost[i]·g[i])与 grad_h_post(Mul + `HierarchicalReduceSum`,与 post_grad 同名同构);注意此处**没有** grad_h_res 的 n² 循环。
- [ai_infra_mhc_post_grad.h:L409-L418](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L409-L418)——AIV 侧的 grad_x(Muls+Axpy 链,hRes 正向 [i,j] 索引)。
- [ai_infra_mhc_post_grad.h:L580-L598](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L580-L598)——AIC 侧全部逻辑 `SetMMParaAndCompute`:按 `singleCoreK` 步进切 K;`SetTensorA(xGm_[...])`、`SetTensorB(gradOutputGm_[...], true)`(转置);`IterateAll(gradHResGm_[outputBase], offsetK == 0 ? 0 : 1)` 首段覆盖、后续段累加;循环外 `mm.End()`。
- [ai_infra_mhc_post_grad_tiling.cpp:L832](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_tiling.cpp#L832)——同样以优先级 2000 注册单模板。

ST 测试侧(golden 的组织方式):

- [test_ai_infra_mhc_post_grad.py:L131-L149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/tests/st/test_ai_infra_mhc_post_grad.py#L131-L149)——golden 用 CPU fp64 + autograd:把前向包成可微函数,`y.backward(grad_output)` 一次拿四路梯度,这是验证手写反向公式最省力的范式。
- [test_ai_infra_mhc_post_grad.py:L384-L386](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/tests/st/test_ai_infra_mhc_post_grad.py#L384-L386)——被测调用:`torch.ops.custom.npu_ai_infra_mhc_post_grad(...)`,与 docs 示例一致。
- [test_ai_infra_mhc_post_grad.py:L247-L249](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/tests/st/test_ai_infra_mhc_post_grad.py#L247-L249)——双基准判定的通过线:NPU 对 golden 的 MARE 比值 ≤10、MERE/RMSE 比值 ≤2(以小算子拼接 bench 为参照,u8-l3 详讲)。

#### 4.4.4 代码实践

**实践目标**:用 torch.autograd 验证 mhc_post_grad 的四路梯度公式,并对照源码标注每路的实现位置。

1. 在有 PyTorch 的机器上运行下面的脚本(示例代码,CPU 即可):

```python
import torch

torch.manual_seed(0)
T, n, D = 4, 4, 32
g       = torch.randn(T, n, D, dtype=torch.float64)
x       = torch.randn(T, n, D, dtype=torch.float64).requires_grad_(True)
h_res   = torch.randn(T, n, n, dtype=torch.float64).requires_grad_(True)
h_out   = torch.randn(T, D,   dtype=torch.float64).requires_grad_(True)
h_post  = torch.randn(T, n,   dtype=torch.float64).requires_grad_(True)

# 前向(公式同 4.1)
y = h_post.unsqueeze(-1) * h_out.unsqueeze(-2) \
    + torch.sum(h_res.unsqueeze(-1) * x.unsqueeze(-2), dim=-3)
y.backward(g)   # 一次拿到四路梯度

# 手写四路反向(对应 kernel 的四条通路)
grad_x_v2      = torch.einsum("tji,tid->tjd", h_res, g)          # AIC 无 / AIV Muls+Axpy
grad_h_res_v2  = torch.einsum("tjd,tid->tji", x, g)              # AIC Cube 矩乘
grad_h_out_v2  = (g * h_post.unsqueeze(-1)).sum(dim=1)           # AIV Axpy 累加
grad_h_post_v2 = (g * h_out.unsqueeze(-2)).sum(dim=-1)           # AIV Mul+ReduceSum

for name, a, b in [("grad_x", x.grad, grad_x_v2), ("grad_h_res", h_res.grad, grad_h_res_v2),
                   ("grad_h_out", h_out.grad, grad_h_out_v2), ("grad_h_post", h_post.grad, grad_h_post_v2)]:
    print(name, (a - b).abs().max().item())
```

2. 对照源码标注:`grad_x_v2` 对应 [ai_infra_mhc_post_grad.h:L409-L418](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L409-L418);`grad_h_res_v2` 的 einsum `tjd,tid->tji` 对应 [ai_infra_mhc_post_grad.h:L588-L595](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L588-L595) 的 `mm.SetTensorA(x)/SetTensorB(gradOutput, true)`;`grad_h_out_v2`/`grad_h_post_v2` 对应 [ai_infra_mhc_post_grad.h:L390-L397](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_kernel/ai_infra_mhc_post_grad.h#L390-L397)。
3. **需要观察的现象**:四行 diff 全部精确为 0(fp64 下 autograd 与手写公式应逐位一致)。
4. **预期结果**:全部为 0 即通过。若 `grad_h_res` 不为 0,多半是 einsum 下标顺序抄错——注意输出下标是 `tji` 不是 `tij`。
5. NPU 环境下可追加一步(可选,待本地验证):按 [test_ai_infra_mhc_post_grad.py:L292-L309](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/tests/st/test_ai_infra_mhc_post_grad.py#L292-L309) 的 net_shapes 跑 `pytest tests/st/test_ai_infra_mhc_post_grad.py`,观察四路梯度各自的 MARE/MERE/RMSE 输出。

#### 4.4.5 小练习与答案

**练习 1**:mhc_post_grad 的 AIV 侧和 AIC 侧之间为什么可以不做任何跨核同步?

**参考答案**:四路梯度写入四个互不相交的输出张量;AIC 读的 x/grad_output 与 AIV 读的相同,但都是只读输入;两侧不存在生产-消费依赖。只要各自按自己的分配表跑完 item 集合,输出即完整。这与 FA 前向 Cube/Vector 需要乒乓同步(u4-l3)形成对比——是否需要同步取决于数据依赖,不取决于是否混合核。

**练习 2**:`singleCoreK = AlignDown(D/4, 16)` 为什么不直接让单核一次吃完整个 K=D?

**参考答案**:L0A/L0B 缓冲有限,baseK 由 64KB/双缓冲/元素宽度推出来,K 一次进不完就要分批;拆 4 段还能让 Mmad 与搬数形成更细的流水。对齐到 16 是 Cube 基本块(fractal)的要求。分段后用 `IterateAll(..., offsetK==0 ? 0 : 1)` 完成累加接力。

**练习 3**:既然 mhc_post_grad 在 grad_h_res 上更快,post_grad 还有存在价值吗?

**参考答案**:有。AIV_ONLY 版本不依赖 Cube 资源,在 Cube 被其他算子占满的图里可作调度备选;实现更简单、不需要 matmulTiling,维护与验证成本更低;且对本算子 n≤8 的小矩阵,Cube 优势在特定 shape 区间未必显著。仓库同时保留两条路线,恰好给了我们对比两种实现策略的活标本。(哪个版本在哪些 shape 下更快,**待本地 benchmark 验证**。)

## 5. 综合实践

把本讲三个算子放回 MHC 训练一步的大图里。请完成两份产出:

**任务 A:MHC 训练一步算子协作图**。用你熟悉的工具(mermaid、纸笔均可)画出下面的数据流,并给每个节点标注其 docs 中的函数原型(本讲源码与 grep 均可查到):

```
前向:
  pre        : npu_manifold_constrained_hyper_connection_pre(x, phi, alpha, bias, *, gamma, out_flag=1, ...)
               → (h_in, H_post, H_res, 以及落盘的 inv_rms/mm_res/h_pre)   [u5-l3]
  Atten/MLP  : h_in → h_out                                              [非本仓库算子]
  sinkhorn   : npu_sinkhorn(H_res, *, out_flag=1, eps, num_iters)
               → (H_res', norm_out, sum_out)                             [u5-l1]
  post       : npu_ai_infra_manifold_constrained_hyper_connection_post(x, H_res', h_out, H_post)
               → x_{l+1}                                                 [本讲]
反向:
  sinkhorn_grad : npu_sinkhorn_grad(grad_output, norm_out, sum_out) → grad_H_res   [u5-l2]
  post_grad 或 mhc_post_grad(二选一):
                 npu_ai_infra_mhc_post_grad(grad_output, x, H_res', h_out, H_post)
                 → (grad_x, grad_H_res, grad_h_out, grad_H_post)         [本讲]
  pre_grad      : npu_manifold_constrained_hyper_connection_pre_grad(...) [u5-l3]
                 → (dx1, dphi1, da1, db1, dgamma1)
```

画完后自检两点:① 前向 post 消费的 `h_res` 是 **Sinkhorn 输出**(双随机化之后的),反向同理——画成直接吃 pre 的原始 H_res 就错了;② out_flag=1 的落盘量(norm_out/sum_out)只在反向被消费,前向图里不应出现丢失。

**任务 B:三个 post 相关算子的输入差异对比表**。填写并核对:

| 算子 | 输入数 | 输入列表 | 输出数 | kernel 核型 | grad_h_res 实现位置 |
| --- | --- | --- | --- | --- | --- |
| post | 4 | x, h_res, h_out, h_post | 1 | AIV_ONLY | —(前向) |
| post_grad | 5 | grad_output, x, h_res, h_out, h_post | 4 | AIV_ONLY | ComputeTile 内 n² 次分层归约 |
| mhc_post_grad | 5 | grad_output, x, h_res, h_out, h_post | 4 | MIX_AIC_1_2 | AIC 侧 SetMMParaAndCompute 矩阵乘 |

填写时逐项回源码核对(`_def.cpp` 数输入输出、kernel 入口看核型常量),并补一列「grad_h_res 的调用次数特征」:post_grad 每 token 每 tile 是 n+n² 次归约,mhc_post_grad 是每 token 约 D/singleCoreK 段 × n×n×K 次 Mmad 折叠。

有 NPU 环境的读者可以加一步终局验证(**待本地验证**):把 4.4.4 的脚本搬到 NPU,前向用 4.1.4 的公式、反向分别调 `post_grad` 与 `mhc_post_grad`,断言两者输出在 bf16 容差内一致——这是「同数学、不同实现」结论的实验闭环。

## 6. 本讲小结

- post 是 MHC 的输出侧汇聚算子:\( x_{l+1} = (H^{res})^T x_l + h_{post} \otimes h_{out} \),消费 Sinkhorn 产出的双随机矩阵;n×n 极小矩阵乘用 AIV 的 Muls/Axpy 即可,无需 Cube。
- post 的 tiling 是「两级组织 + 单模板(优先级 2000)」:16 字段 TilingData 携带分核、D 维切分与对齐三标志;kernel 入口把三标志组装成 3 位模板键,8 路模板覆盖 n∈{4,6,8}×D 对齐与否的全部组合。
- post_grad 与 mhc_post_grad 是**同一反向的两种实现**,def 与文档逐行相同;分水岭在 kernel 架构:AIV_ONLY 纯向量 vs AIC:AIV=1:2 混合核。
- 「聚合多路梯度」的准确含义:mhc_post_grad 一次调用由两类核并行产出全部四路梯度——AIV 三路(逐元素+归约),AIC 一路(grad_h_res 走 `MatmulImpl` 矩阵乘,K=D 分段累加),两侧无同步、无竞争。
- 混合核的配套改动是一整套:AIC 核数探测、双轨分配表、TilingData 增补 `TCubeTiling`、blockDim 以 AIC 组为单位——「把一路计算搬去 Cube」远不止改一个函数。
- 验证反向的金标准是 CPU fp64 + autograd 一次拿四路梯度,ST 测试的 golden/bench/自定义三方对比与 MARE≤10×bench、MERE/RMSE≤2×bench 判定线都建立在其上。

## 7. 下一步学习建议

本讲结课后,MHC 家族七个目录只剩 torch 侧适配没有展开。建议:

1. 进入 u6 单元,读 `ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/` 下的 csrc/converter/test 三件套,看 mhc_post 系列如何被包成 `torch.ops.custom.*`(本讲 docs 里的调用示例正是这层的产物)。
2. 顺手对照 u6-l2 的 Autograd Function:思考「post 前向 + post_grad/mhc_post_grad 反向」如何拼成一个可微的 torch 算子,mask/落盘量在 backward 里如何取舍。
3. 若对混合核意犹未尽,回看 u4-l6 LightningIndexer 的 CrossCoreSetFlag/WaitFlag 跨核同步,对比本讲「无同步」设计,体会数据依赖对混合核复杂度的决定作用。
4. 测试视角的完整拼图在 u8 单元:MARE/MERE/RMSE 指标推导、L0/L1/L2 精度分级与本讲 ST 判定线的关系将在 u8-l3 展开。
