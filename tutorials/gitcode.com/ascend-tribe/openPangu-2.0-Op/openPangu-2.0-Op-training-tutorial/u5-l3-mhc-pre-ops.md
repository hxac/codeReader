# MHC 前处理算子：pre 与 pre_grad

## 1. 本讲目标

本讲是 MHC（Manifold Constrained Hyper Connection，流形约束超连接）算子族的第三篇。u5-l1、u5-l2 精读了 Sinkhorn 前反向，但 Sinkhorn 只是 MHC「混合矩阵」的归一化环节；真正给整条 MHC 链路「供料」的，是本讲的前处理算子 `ai_infra_manifold_constrained_hyper_connection_pre`（下文简称 **pre**）。它也是 MHC 家族中**唯一带完整 op_api 源码的算子**，因此是走读「aclnn 两段式接口 → l0op 内部封装 → op_host → op_kernel」全链路的最佳标本。

学完本讲，你应该能够：

1. 说出 pre 算子的功能：一次调用同时产出 Attention/MLP 层的输入 \(h_{in}\)、MHC 的混合矩阵 \(H^{post}\) 与 \(H^{res}\)（\(H^{res}\) 即 Sinkhorn 算子的输入），以及三个专供反向的中间量。
2. 独立走读一个「含完整 op_api」的算子全链路：aclnn 外层（校验/连续化/装配）与 l0op 内层（AllocTensor/INFER_SHAPE/ADD_TO_LAUNCHER_LIST_AICORE）的分工。
3. 解释 tilingKey 0/1 双模式的选择条件，以及 `split_ND` kernel 头对小 T（解码）场景沿 nD（K）轴切分的流水线设计。
4. 说明 pre 算子在整个 MHC 训练步中的位置：为什么训练时 `out_flag` 必须为 1，pre 的输出如何被 `pre_grad` 反向算子逐个消费。

## 2. 前置知识

本讲默认你已读过以下讲义的概念，这里只做一句话回顾并补充少量新术语：

- **四层算子模型**（u1-l2）：一个算子由 `_def.cpp` 原型注册、op_host（InferShape + Tiling）、op_kernel 设备侧 Kernel、op_api 对外 aclnn 接口四层组成，四层靠**算子名**对齐。
- **aclnn 两段式**（u2-l5）：第一段 `aclnnXxxGetWorkspaceSize` 在 Host 上校验参数、把整条任务流水线记入 `aclOpExecutor` 并汇总 workspace；第二段 `aclnnXxx` 只有 4 个参数，由 `CommonOpExecutorRun` 把任务下发到 stream。op_api 内部分两级：L2（aclnn 文件）与 L0（namespace `l0op` 的内部算子原子）。
- **tiling 契约**（u2-l3 / u3-l3）：Tiling 产出 blockDim、tilingKey、TilingData 字节流与 workspace 四项契约；tilingKey 由 Host 写、Device 读。本算子的 tiling 用了 u3-l3 讲过的 `TilingBase` 七步模板框架与 `REGISTER_TILING_TEMPLATE` 注册宏，但只注册了**一个**模板（优先级 1000），没有责任链接力。
- **混合核（AIC:AIV = 1:2）**（u4-l3/u4-l4 首次出现）：一个 kernel 进程同时编译出 Cube 核（AIC，做 Matmul）与 Vector 核（AIV，做向量运算），`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 声明 1 个 AIC 配 2 个 AIV；代码里用 `if ASCEND_IS_AIC` / `if ASCEND_IS_AIV` 区分执行路径，`GetSubBlockIdx()` 区分同一 AIC 下的两个向量子核，`CrossCoreSetFlag/CrossCoreWaitFlag` 做 AIV→AIC 的事件同步。
- **MHC 与 Sinkhorn**（u5-l1）：MHC 要求多路残差分支的混合矩阵接近双随机矩阵，Sinkhorn 迭代负责把 pre 输出的 \(H^{res}\)（未归一化）变成双随机矩阵。前向「落盘中间量、反向复用」的空间换时间设计，在本讲的 pre/pre_grad 对上会再次出现。

新术语：

- **变长折叠 TND**：把 [B,S] 两轴折叠成 T，输入 x 变为 [T,N,D]。pre 同时接受 [B,S,N,D]（BSND）与 [T,N,D]（TND）两种排布，tiling 与 InferShape 都要双分支处理。
- **fusionSize**：φ 矩阵的行数 \(n^2+2n\)，即 matmul 的 N 维，名字来源于它把 pre/post/res 三段投影「融合」在一次矩阵乘里。
- **部分和（partial sum）归约**：把一个大矩阵乘按 K 轴拆到多个 Cube 核，每核算出部分和写入 workspace，再由向量核 ReduceSum 合并。这是本讲 `split_ND` kernel 的核心手法。

## 3. 本讲源码地图

以下链接均指向当前 HEAD `c1d24e3`。pre 算子目录：`ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/`。

| 文件 | 层 | 作用 |
|:---|:---|:---|
| [docs/npu_manifold_constrained_hyper_connection_pre.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/docs/npu_manifold_constrained_hyper_connection_pre.md) | 文档 | 功能说明、计算公式、接口规格、约束与调用示例 |
| [op_host/..._def.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp) | op_def | 5 输入 / 6 输出 / 3 属性的原型注册与双芯片白名单 |
| [op_host/..._infershape.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_infershape.cpp) | op_host | 按 BSND/TND 两分支推导 6 个输出的 shape |
| [op_host/..._tiling.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.h) / [.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp) | op_host | TilingData 定义；TilingBase 子类，双模式判定（tilingKey 0/1） |
| [op_api/aclnn_..._pre.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h) | op_api | 对外 aclnn 两段式接口声明（L2 契约） |
| [op_api/aclnn_..._pre.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp) | op_api | aclnn 外层实现：Builder 装配、五步校验、Contiguous、ViewCopy |
| [op_api/ai_infra_..._pre.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/ai_infra_manifold_constrained_hyper_connection_pre.h) / [.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/ai_infra_manifold_constrained_hyper_connection_pre.cpp) | op_api/L0 | `l0op::AiInfraManifoldConstrainedHyperConnectionPre` 内部算子原子 |
| [op_kernel/..._pre.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre.cpp) | op_kernel | 设备侧入口：按 tilingKey 分发到两个 kernel 类 |
| [op_kernel/..._pre.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre.h) | op_kernel | 基础 kernel 类（tilingKey=0，大 T / 带 gamma / outFlag=1） |
| [op_kernel/..._pre_split_ND.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h) | op_kernel | **split_ND kernel 类**（tilingKey=1，小 T 解码场景），本讲精读重点 |
| [tests/st/test_mhc_pre.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/tests/st/test_mhc_pre.py) | 测试 | CPU golden 参考实现 + 两个 st 用例（恰好分别覆盖 tilingKey 0/1） |

反向算子目录 `ai_infra_manifold_constrained_hyper_connection_pre_grad/` 结构与之镜像，见 4.5 节对照表。

> 阅读提示：两个 kernel 头文件的文件头注释都是「陈旧」的——`_def.cpp` 头注释写的是 `lower_triangular_inverse_def.cpp.cpp`，`split_ND.h` 头注释写的是 `..._split_N.h`（少了 D）。以实际代码为准，这是读历史较长的算子库的常态。

## 4. 核心概念与源码讲解

### 4.1 功能定位与接口契约：公式、def 原型与 aclnn 签名

#### 4.1.1 概念说明

MHC 层每个 token 有 \(n\) 路残差输入 \(x \in \mathbb{R}^{T \times n \times D}\)。pre 算子做三件事：

1. **RMS 归一化统计**：对每行（一个 token 的 \(nD\) 个元素）算 \(\mathrm{invRms}\)；
2. **一次融合矩阵乘**：\((\gamma \odot x)\varphi^{\top}\)，把 pre/post/res 三段投影一次算完（φ 有 \(n^2+2n\) 行）；
3. **三路激活与拆分**：从乘积中按列拆出 \(H^{pre}\)、\(H^{post}\)、\(H^{res}\)，分别过 sigmoid 等激活；再用 \(H^{pre}\) 对 \(n\) 路 x 加权求和得到 \(h_{in}\)。

其中 \(h_{in}\) 喂给 Attention/MLP 层；\(H^{res}\) 是「未做 Sinkhorn 变换」的混合矩阵，交给 u5-l1 的 Sinkhorn 算子；\(H^{post}\) 与 \(H^{res}\) 一起参与 MHC 的输出混合（u5-l4 的 post 算子）。

为什么要有 `out_flag`？训练时反向需要 \(inv\_rms\)、\(mm\_res\)（矩阵乘结果）、\(h\_pre\)（sigmoid 前后的中间量）这三个中间量；推理时不需要，落盘纯浪费带宽。于是用 `out_flag` 控制「训练全输出 / 推理不输出」，这是与 Sinkhorn 前向 `out_flag=1` 落盘 norm_out/sum_out 完全同构的「空间换反向时间」设计。

#### 4.1.2 核心流程

docs 给出的公式（整理后）：

\[
\mathrm{invRms}[t] = \frac{1}{\sqrt{\frac{1}{nD}\sum_{k=0}^{nD-1} x[t,k]^2 + norm\_eps}}
\]

\[
H^{mix}[t,j] = \mathrm{invRms}[t] \cdot \sum_{k}(\gamma \odot x)[t,k]\cdot\varphi[j,k],\quad j \in [0,\, n^2+2n)
\]

\[
H^{pre} = \sigma\big(\alpha_{pre}\, H^{mix}_{[:,\,0:n]} + b_{pre}\big) + hc\_eps,\qquad
H^{post} = 2\,\sigma\big(\alpha_{post}\, H^{mix}_{[:,\,n:2n]} + b_{post}\big)
\]

\[
H^{res} = \alpha_{res}\, H^{mix}_{[:,\,2n:]} + b_{res},\qquad
h_{in}[t,d] = \sum_{i=0}^{n-1} H^{pre}[t,i] \cdot x[t,i,d]
\]

两个工程细节（docs 公式是示意，以下以 st 测试的 golden 与 kernel 实现为准）：

- **invRms 乘在矩阵乘之后**。数学上 \(\mathrm{invRms}\) 是行标量，\((\mathrm{invRms}\odot x)\varphi^{\top} = \mathrm{invRms}\odot(x\varphi^{\top})\)，两者等价；但 \(n^2+2n \ll nD\)（如 n=4、D=2560 时 24 ≪ 10240），乘在后面便宜几百倍。kernel 因此先算 \((\gamma\odot x)\varphi^{\top}\)，再对 [T, n²+2n] 的结果按行乘 invRms。
- **RMS 统计量用「原始 x」算**，gamma 不参与统计；矩阵乘的输入才是 \(\gamma \odot x\)（见 [test_mhc_pre.py:53-61](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/tests/st/test_mhc_pre.py#L53-L61) 的 golden：先 `inv_rms` 后 `x_rs = x * gamma`）。而 \(h_{in}\) 的加权求和用的也是**原始 x**，不是 \(\gamma\odot x\)。

接口数据流（以 TND 为例）：

```text
x [T,n,D] BF16/FP16 ─┐
phi [n²+2n, nD] FP32 ─┤→  pre 算子  → hin [T,D] BF16/FP16（必选）
alpha [3] FP32 ───────┤              → h_post [T,n] FP32（必选）
bias [n²+2n] FP32 ────┤              → h_res [T,n,n] FP32（必选，未 Sinkhorn）
gamma [n,D] FP32 可选─┘              → inv_rms [T] / mm_res [T,n²+2n] / h_pre [T,n]（可选，out_flag=1 才有意义）
```

#### 4.1.3 源码精读

**（1）def 原型：5 输入、6 输出、3 属性。** [ai_infra_manifold_constrained_hyper_connection_pre_def.cpp:22-46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp#L22-L46) 依次声明 `x`（REQUIRED，BF16/FP16）、`phi`/`alpha`/`bias`（REQUIRED，FP32）、`gamma`（OPTIONAL，FP32）。注意**输入声明顺序就是运行期索引**——tiling 侧的 `X_INDEX=0 … GAMMA_INDEX=4` 与之一一对应。

[ai_infra_manifold_constrained_hyper_connection_pre_def.cpp:48-78](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp#L48-L78) 声明 6 个输出：`hin`（REQUIRED，与 x 同 dtype）、`h_post`/`h_res`（REQUIRED，FP32）、`inv_rms`/`mm_res`/`h_pre`（OPTIONAL，FP32）。**可选输出 + outFlag 属性**的组合是「训练/推理一体算子」的典型写法：推理时框架传空 tensor，kernel 走不落盘分支。

[ai_infra_manifold_constrained_hyper_connection_pre_def.cpp:80-91](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp#L80-L91) 配置 `OpAICoreConfig`（开启动态 shape/维数支持）并 `AddConfig("ascend910b")`、`AddConfig("ascend910_93")` 双芯片注册（A2/A3），随后声明三个带默认值的属性 `outFlag=0`、`normEps=0.0`、`hcEps=0.0`；第 95 行 `OP_ADD` 把原型登记进 CANN 注册表（u2-l2 讲过：漏写 OP_ADD 会静默失败）。

**（2）aclnn 头文件：两段式契约。** [aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h:42-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h#L42-L47) 声明第一段：5 个输入 tensor + 3 个标量 + 6 个输出 tensor + `workspaceSize`/`executor` 双出参；[aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h:59-60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h#L59-L60) 声明第二段，固定 4 参数。头注释里写清了每个 tensor 的 shape 约定，是接口的**第一权威来源**。

**（3）一个文档不一致实例。** docs 返回值表把 `out_h_post` 的 shape 写成 \((B,S,D)\)/\((T,D)\)（[npu_manifold_constrained_hyper_connection_pre.md:59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/docs/npu_manifold_constrained_hyper_connection_pre.md#L59)），但 aclnn 头注释与 InferShape 都是 \((B,S,N)\)/\((T,N)\)（\(H^{post}\) 每 token 只有 n 个数）。这与 u2-l5 的结论一致：**读接口以 `_def.cpp`、aclnn 头与 InferShape 源码为准，docs 可能滞后**。

#### 4.1.4 代码实践

**实践目标**：建立 pre 算子的「接口契约卡片」，并训练「三个来源交叉验证」的习惯。

**操作步骤**：

1. 打开 [npu_manifold_constrained_hyper_connection_pre.md:44-63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/docs/npu_manifold_constrained_hyper_connection_pre.md#L44-L63) 的参数表，抄下每个参数的 shape。
2. 与 [aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h:25-37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h#L25-L37) 的注释、[ai_infra_manifold_constrained_hyper_connection_pre_infershape.cpp:118-145](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_infershape.cpp#L118-L145) 的 TND 推导逐项对照。
3. 把不一致处标红，并写下「以谁为准」的判断依据。

**需要观察的现象**：除 4.1.3 第（3）条指出的 `out_h_post` shape 差异外，还应注意到 docs 参数表允许 \((S,B,n,D)\)（S 在前）排布，而 aclnn 层的 shape 校验只按维度数（3 维或 4 维）区分、不检查 B 与 S 的先后——即 **S 在前是靠 tiling/infershape 的「折叠成 T」语义自然兼容的**（BSND 分支里 `totalLength_ = batch * sequence`，两种先后序都会被折叠，但输出 shape 的 B/S 顺序会跟随输入）。

**预期结果**：得到一张 11 行（5 输入 + 6 输出）的契约卡片，其中 `out_h_post` 一行有三个来源的两个不同答案，判定以代码为准。

#### 4.1.5 小练习与答案

**练习 1**：φ 为什么是 \(n^2+2n\) 行？三段各占多少？

**答案**：pre 段 \(n\) 行 + post 段 \(n\) 行 + res 段 \(n^2\) 行（res 是 \(n \times n\) 的混合矩阵摊平），合计 \(n^2+2n\)。bias 的布局与之完全同构：`bias[0:n]` 是 \(b_{pre}\)、`bias[n:2n]` 是 \(b_{post}\)、`bias[2n:]` 是 \(b_{res}\)（见 golden [test_mhc_pre.py:48-50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/tests/st/test_mhc_pre.py#L48-L50)）。

**练习 2**：\(h_{in}\) 的 shape 为什么从 [T,n,D] 变成 [T,D]？

**答案**：\(h_{in}[t,d]=\sum_i H^{pre}[t,i]\,x[t,i,d]\) 是沿 n 轴的加权求和，n 轴被消掉，D 轴保留；这正是 InferShape 里 `outHinShape` 比 x 少一维、末维取 D 的原因。

**练习 3**：docs 说「n 支持 4、6、8，D 需 16 对齐且 n·D ≤ 65535」。这三条约束分别在哪份源码里强制？

**答案**：都在 tiling 的 `GetInputShape` 里：n 白名单 {4,6,8} 在 [ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp:94-98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp#L94-L98)，D 的 16 对齐在 L100-103，n·D ≤ 65535 在 L105-108（65535 是 uint16 寻址上限的量级，n·D 作为 matmul 的 K 维不能超限）。aclnn 层只查维度数与 dtype，不查这些规格——规格约束由 tiling 兜底。

### 4.2 op_api 全链路：aclnn 外层适配与 l0op 内层封装

#### 4.2.1 概念说明

u2-l5 以 FlashAttention 为标本讲过 aclnn 两段式的通用骨架；本算子是该骨架的**最小完整实现**，适合逐行精读。MHC pre 的 op_api 目录有四个文件，分两级：

- **L2（aclnn 层，对外契约与适配）**：`aclnn_..._pre.h/.cpp`。职责是参数校验、把非连续输入变连续（`l0op::Contiguous`）、调用 L0 原子、把 L0 产出的内部 tensor 拷回用户输出（`l0op::ViewCopy`）。
- **L0（l0op 层，内部算子原子）**：`ai_infra_..._pre.h/.cpp`，namespace `l0op`。职责是给 6 个输出分配内部 tensor、触发 InferShape（`INFER_SHAPE` 宏）、把算子挂进 executor 的发射列表（`ADD_TO_LAUNCHER_LIST_AICORE` 宏）。

与 FA 相比的差异：FA 需要在 aclnn 层做 Transpose/Pad 等重布局（因为 kernel 只接受特定排布），pre 的输入输出全是 ND 排布，所以 aclnn 层只需要 `Contiguous` 一种预处理。**「aclnn 层要做多少预处理」直接反映 kernel 对输入排布的挑剔程度**——这是读 op_api 代码时的一个快速判据。

#### 4.2.2 核心流程

一次完整的第一段调用（`aclnnAiInfraManifoldConstrainedHyperConnectionPreGetWorkspaceSize`）流程：

```text
CREATE_EXECUTOR()                          # 创建独占 executor
  └─ Builder 装配 ParamsBase               # 5输入 + 3属性 + 6输出 打包成值对象
      └─ CheckParams（五步校验，任一步失败即返回错误码）
          1) CheckNotNull    非空指针（gamma 可为 null）
          2) CheckEmptyTensor 非空 tensor
          3) CheckInputOutDims 维度数：x 3/4 维，phi 2 维，alpha/bias 1 维，gamma 2 维
          4) CheckInputOutShape 从 x 推出 n、D，校验 alpha==(3)、phi==(n²+2n, nD)、bias==(n²+2n)、gamma==(n,D)
          5) CheckDtypeValid + CheckFormat  dtype 与非私有格式
      └─ CovertDataContiguous              # l0op::Contiguous × 5，任务记入 executor
      └─ l0op::AiInfraManifoldConstrainedHyperConnectionPre(...)
            ├─ executor->AllocTensor × 6   # 内部分配输出 tensor（此时才定 shape/dtype）
            ├─ INFER_SHAPE(...)            # 调 op_host 的 InferShape 推 shape
            └─ ADD_TO_LAUNCHER_LIST_AICORE(...)  # 触发 tiling 并把 kernel launch 记入 executor
      └─ l0op::ViewCopy × 6                # 内部输出 → 用户输出 tensor，任务记入 executor
*workspaceSize = executor->GetWorkspaceSize()
uniqueExecutor.ReleaseTo(executor)         # 移交所有权给调用者
```

第二段只有一句 `CommonOpExecutorRun(workspace, workspaceSize, executor, stream)`：按 executor 里记录的顺序（Contiguous → 本算子 kernel → 6 次 ViewCopy kernel）下发到 stream。

#### 4.2.3 源码精读

**（1）参数对象与 Builder。** [aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp:36-58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp#L36-L58) 定义 `ParamsBase` 值对象（额外挂了 5 个 `*_contiguous` 指针存放连续化结果）；L60-114 的 Builder 用链式 `SetInput/SetAttr/SetOutput/SetOptionalOutput/Build` 把 14 个散参数组装成该对象——纯粹的语法糖，让入口函数只面对一个对象。

**（2）从 x 推导 n 与 D 的校验。** [aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp:232-274](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp#L232-L274)：先强制 `alpha.shape == (3)`，再按 x 是 4 维（BSND，取 dim2/dim3）或 3 维（TND，取 dim1/dim2）解出 `n`、`d`，随后用 \(n^2+2n\) 与 \(nD\) 分别核对 phi 的两维与 bias 的维。这段是「**算子族内一致性**」的守护者：phi/bias/alpha 是模型权重，shape 错了整条 MHC 链都会算错，必须在进 kernel 前拦下。

**（3）公共流程：Contiguous → l0op → ViewCopy。** [aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp:421-461](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp#L421-L461) 的 `aiInfraMHCPreCommonProcess` 是三步曲的核心：L427 调 `CovertDataContiguous`（其内部 L398-419 对 5 个输入逐个 `l0op::Contiguous`，这些转换任务**也被记入 executor**，第二段会真实执行搬运 kernel）；L431-434 一次调用 l0op 原子拿回 6 元组；L436-458 对 6 个输出逐个 `l0op::ViewCopy(内部tensor, 用户tensor, executor)`。

为什么要 ViewCopy？因为 l0op 原子内部用 `AllocTensor` 分配的是**算子自己形状推导出来的新 tensor**，与用户传入的输出 tensor 是两块内存（用户 tensor 可能带自己的 stride/视图），必须显式拷贝并把拷贝任务挂进 executor。

**（4）两段入口。** [aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp:463-488](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp#L463-L488)：第一段开头 `L2_DFX_PHASE_1` 是打点宏（记录输入输出元数据），`CREATE_EXECUTOR()` 创建 executor，末尾两行把 workspace 大小写给调用者、`ReleaseTo` 移交所有权。[aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp:490-495](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp#L490-L495) 的第二段只有 `L2_DFX_PHASE_2` + `CommonOpExecutorRun`，完全不复核参数——**所有 Host 侧工作都必须在第一段完成**，这是两段式的纪律。

**（5）l0op 内层原子。** [ai_infra_manifold_constrained_hyper_connection_pre.cpp:23-55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/ai_infra_manifold_constrained_hyper_connection_pre.cpp#L23-L55)：第 23 行 `OP_TYPE_REGISTER` 注册算子类型；L34-40 用 `executor->AllocTensor` 分配 6 个输出（**hin 用 `x->GetDataType()` 继承输入精度，其余 5 个固定 FP32**——dtype 契约在代码里的落点）；L42-44 `INFER_SHAPE` 宏触发 op_host 的 InferShape；L47-50 `ADD_TO_LAUNCHER_LIST_AICORE` 宏触发 tiling 并装配 launch 参数；L55 返回 6 元组。函数签名见 [ai_infra_manifold_constrained_hyper_connection_pre.h:18-22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/ai_infra_manifold_constrained_hyper_connection_pre.h#L18-L22)。对照 u3-l4：这些 `aclnn_kernels/contiguous.h` 等头文件正是 stub 桩镜像的那一层，UT 里由 `opapi_stub.cpp` 替身。

#### 4.2.4 代码实践

**实践目标**：数清「一次 aclnn 第一段调用共往 executor 挂了多少个任务」，从而理解 executor 是一条**任务流水线**而非单 kernel。

**操作步骤**（源码阅读型，无需 NPU）：

1. 在 [aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_hyper_connection_pre.cpp) 里 grep `l0op::`，列出每个调用点（文件名以实际为准：`aclnn_ai_infra_manifold_constrained_hyper_connection_pre.cpp`）。
2. 按「Contiguous 若干 + 本算子 1 + ViewCopy 6」分类计数，画出 executor 里的任务序列。
3. 回答：若用户传入的 5 个输入全部连续、gamma 为空，任务序列是什么？

**需要观察的现象**：`l0op::Contiguous` 对已经连续的输入通常返回原指针、不产生搬运任务（这是 L0 层的常见优化约定，具体行为以 CANN 包内 contiguous 实现为准，**待本地验证**）；`ViewCopy` 则无论如何都会挂任务，因为内部输出与用户输出是两块内存。

**预期结果**：任务序列 = [可选的 Contiguous 搬运] → [pre 算子 kernel（内部又含 AIC/AIV 多个核的任务）] → [ViewCopy × 6]。第二段 `CommonOpExecutorRun` 按此顺序下发。

#### 4.2.5 小练习与答案

**练习 1**：aclnn 层的校验（维度/shape/dtype）与 tiling 层的校验（n 白名单、D 对齐等）重复吗？为什么分两层？

**答案**：不重复，是纵深防御。aclnn 层查「接口契约」（维度数、dtype、phi/bias 与 x 的相对一致性），在框架最外沿快速失败并给出可读日志；tiling 层查「规格约束」（n∈{4,6,8}、D 16 对齐、n·D≤65535）与平台资源，这些与 kernel 实现强绑定。aclnn 挡得住的错不必等 tiling；tiling 兜底的是 aclnn 没查或查不了的（如走图模式绕过 aclnn 的调用路径）。

**练习 2**：`CovertDataContiguous` 里 gamma 的处理为什么有 `if (params.gamma != nullptr)` 包裹，而 x/phi/alpha/bias 没有？

**答案**：gamma 是 OPTIONAL 输入，推理路径常传空；对空指针调 `Contiguous` 会崩溃。x/phi/alpha/bias 是 REQUIRED，已在 `CheckNotNull` 保证非空。

**练习 3**：如果去掉 6 次 `ViewCopy`，接口还能对吗？

**答案**：不能。l0op 原子 `AllocTensor` 出来的输出挂在 executor 的内部生命周期上，用户拿到的是自己传入的输出 tensor 地址；不拷回去，用户 tensor 里是未初始化数据。（更深一层：ViewCopy 还承担了「内部推导 shape/stride 与用户 tensor 视图不一致时的搬运适配」。）

### 4.3 op_host：InferShape 输出推导与双模式 Tiling

#### 4.3.1 概念说明

op_host 层两个职责：**InferShape**（图编译期推输出 shape，给框架分配 tensor 用）与 **Tiling**（kernel 启动前的 Host 侧规划）。pre 的 tiling 有一处值得专门讲的设计：**同一个算子配两个 kernel 实现，用 tilingKey 做运行期选择**——

- tilingKey=0：基础 kernel（`..._pre.h` 里的 `AiInfraManifoldConstrainedHyperConnectionPreKernel`），按 T 分块（chunkTSize 最大 192）多轮迭代，支持 gamma、支持 outFlag 落盘，适合大 T（训练 prefill）。
- tilingKey=1：split_ND kernel（`..._pre_split_ND.h`），沿 nD（K）轴切分到全部核，一轮算完，适合小 T（解码），但**不支持 gamma 与 outFlag**。

为什么不统一用一个实现？T=4 时按 192 一块只能切出 1 个块，绝大多数核闲置；把 K 轴摊开才能喂饱所有核。而 K 轴切分要求「部分和暂存 + 二次归约」，中间结果布局与 outFlag 的直出布局不兼容，也不便插入 gamma 逐元素乘——所以小 T 场景干脆收窄能力集（tiling 保证 gamma==0 且 outFlag==0 才会选它），换流水线最简。

#### 4.3.2 核心流程

**InferShape 规则表**（TND 分支，BSND 分支同构地多一维）：

| 输出 | shape（TND 输入 [T,N,D]） | dtype |
|:---|:---|:---|
| hin | [T, D] | 同 x（BF16/FP16） |
| h_post | [T, N] | FP32 |
| h_res | [T, N, N] | FP32 |
| inv_rms | [T] | FP32 |
| mm_res | [T, φ.shape[0]] 即 [T, n²+2n] | FP32 |
| h_pre | [T, N] | FP32 |

**Tiling 流程**（挂在 u3-l3 的 `TilingBase` 七步模板上，本算子把全部工作塞进 `DoOpTiling`）：

```text
DoOpTiling
 ├─ GetInputShape        解析 x（BSND/TND）→ totalLength/N/D；校验 n∈{4,6,8}、D%16==0、nD≤65535、phi[1]==nD
 ├─ ParseInputAndAttr    平台信息（AIC 核数、UB/L1/L0）、读 3 个属性
 │    └─ 模式判定：totalLength>180 || hasGamma || outFlag==1 → SPLIT_ND_MODE=0（key 0）
 │                 否则                          → SPLIT_ND_MODE=1（key 1），blockDim 按 T 分档 8/16/20
 ├─ TilingProcess        matmul tiling（MultiCoreMatmulTiling）→ workspace → tilingKey
 ├─ FillTilingData       填 13 个字段的 TilingData + matmulTiling
 └─ PostTiling           SaveToBuffer、SetBlockDim、SetScheduleMode(1)、SetWorkspaceSizes
```

workspace 公式与 tilingKey 的绑定关系（key 1 时 workspace 是「三大块部分和区」）：

\[
\text{userWs}_{key=1} = \big(\underbrace{nD \cdot T}_{x_{fp32}\text{区}} + \underbrace{T \cdot coreNum \cdot 2}_{inv\_rms\text{部分和区}} + \underbrace{coreNum \cdot T \cdot (n^2{+}2n)}_{mm\_res\text{部分和区}}\big)\times 4\ \text{字节}
\]

外加固定 20MB 系统 workspace。

#### 4.3.3 源码精读

**（1）InferShape 的 TND 分支。** [ai_infra_manifold_constrained_hyper_connection_pre_infershape.cpp:118-145](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_infershape.cpp#L118-L145)：逐个 `SetDimNum` + `SetDim` 填六个输出；`mm_res` 的末维取 `phiShape->GetDim(0)`（L82 取出，L139-141 使用），即 φ 的行数。注册在文末 [ai_infra_manifold_constrained_hyper_connection_pre_infershape.cpp:163-165](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_infershape.cpp#L163-L165) 的 `IMPL_OP_INFERSHAPE`。注意 L155-161 的 `InferDataType4mHCPre` 连写三次 `SetOutputDataType(0, DT_FLOAT)`——复制粘贴痕迹，实际输出 dtype 由 l0op 层 `AllocTensor` 显式指定（见 4.2.3（5）），这里未产生实际错误，但属于「以代码为准、勿盲信」的又一例。

**（2）TilingData 与注册。** [ai_infra_manifold_constrained_hyper_connection_pre_tiling.h:25-40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.h#L25-L40) 定义 13 个字段（matmulTiling 结构 + coreNum/outFlag/hasGamma/chunkTSize/v1ChunkDSize/totalLength/nD/fusionSize/N/D/normEps/hcEps/scaleMean），kernel 侧逐字段消费。[ai_infra_manifold_constrained_hyper_connection_pre_tiling.h:56-79](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.h#L56-L79) 声明 `AiInfraManifoldConstrainedHyperConnectionPreBaseTiling : public TilingBase`，覆写 `DoOpTiling/GetTilingKey/PostTiling`，`IsCapable` 恒真（单模板无接力）。注册见 [ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp:45-46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp#L45-L46)（优先级 1000）与 L381-383 的 `IMPL_OP_OPTILING`（含 `TilingParse` 钩子缓存平台信息，对应 u4-l1 讲过的编译期缓存机制）。

**（3）模式判定——本讲最关键的一段。** [ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp:176-193](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp#L176-L193)：

```cpp
if (totalLength_ > 180 || hasGamma_ == 1 || outFlag_ == 1) { // BS大于180，hasGamma等于1，outFlag等于1时走大BS的模板
    blockDim_ = maxBlockDim;
    SPLIT_ND_MODE = 0U; // 大BS
} else {
    SPLIT_ND_MODE = 1U; // 小BS
    if (totalLength_ >= 160)      blockDim_ = min(20, maxBlockDim);
    else if (totalLength_ >= 40)  blockDim_ = min(16, maxBlockDim);
    else                          blockDim_ = min(8,  maxBlockDim);
}
chunkTSize_ = Ceil(Ceil(totalLength_, blockDim_), 32) * 32;  // 32 对齐，上限 192
```

三个条件任一成立都强制走基础 kernel：T 太大必须分块迭代；gamma/outFlag 只有基础 kernel 支持。随后的 [ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp:259-267](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp#L259-L267) 把模式写进 tilingKey 并按 4.3.2 的公式算 workspace（key 0 时 workspace 是「每核 ping-pong 两块」的小缓存区，公式在 L242）。

**（4）PostTiling 的契约回写。** [ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp:317-334](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp#L317-L334)：TilingData 序列化进 RawTilingData、`SetBlockDim`、`SetScheduleMode(1)`（与 kernel 侧 `KERNEL_TYPE_MIX_AIC_1_2` 混合核声明配套）。

#### 4.3.4 代码实践

**实践目标**：给定两组真实输入，手工执行模式判定，算出 tilingKey、blockDim 与 userWorkspace 大小。

**操作步骤**：

1. **A 组（训练 prefill）**：T=1024、n=4、D=2560、有 gamma、out_flag=1。代入 [ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp:176-193](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_tiling.cpp#L176-L193)：totalLength=1024 > 180 → SPLIT_ND_MODE=0。blockDim=平台 AIC 核数。userWorkspace 按 L242 公式 \((2\cdot192\cdot256 + 2\cdot192\cdot80)\times4\times blockDim = 516096\times blockDim\) 字节。
2. **B 组（解码）**：T=4、n=4、D=2560、无 gamma、out_flag=0（即 st 测试的 decode 用例参数）。totalLength=4 < 40 → SPLIT_ND_MODE=1、blockDim=8。代入 L262 公式：\((10240\cdot4 + 4\cdot8\cdot2 + 8\cdot4\cdot24)\times4 = 41792\times4 = 167168\) 字节 ≈ 163KB，再加 20MB。
3. 打开 [tests/st/test_mhc_pre.py:79-135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/tests/st/test_mhc_pre.py#L79-L135)，核对两个 st 用例的参数与你的 A/B 组是否对应。

**需要观察的现象**：两个 st 用例不是随手写的——`test_mhc_pre_case`（T=1024、gamma 有、out_flag=1）恰好触发 key 0，`test_mhc_pre_case_decode`（T=4、gamma None、out_flag 默认 0）恰好触发 key 1，**一组测试完整覆盖两条 kernel 路径**。

**预期结果**：A 组 → key 0 / 基础 kernel；B 组 → key 1 / split_ND kernel。若有 NPU 环境，可开 `ASCEND_GLOBAL_LOG_LEVEL=0` 跑 st 用例，在日志里找 `PrintTilingData` 输出的 blockDim/chunkTSize 与手算对照（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：T=100、无 gamma、out_flag=0 会走哪个 key？blockDim 是多少？

**答案**：totalLength=100 ≤ 180 且其余两个条件不成立 → key 1（split_ND）；100 ≥ 40 且 < 160 → blockDim=16（不超过平台核数）。

**练习 2**：为什么 key 1（split_ND）的 workspace 随 T 与 nD 线性增长，而 key 0 的 workspace 只随 blockDim 线性增长？

**答案**：key 1 把整份 \(x_{fp32}\)（\(nD\times T\)）和全部部分和都摊在 workspace 里，一轮算完；key 0 按 chunkTSize=192 分块流式处理，workspace 只需为每核保留当前块的双缓冲（ping-pong），大小与总 T 无关。**「一次摊平」换并行度，「分块流式」换内存**，是切分策略的一对基本权衡。

**练习 3**：`SetScheduleMode(1)` 与 kernel 里的 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 是什么关系？

**答案**：一对配套声明——tiling 侧告知运行时按混合核模式调度（AIC 与 AIV 分开编排），kernel 侧声明本 kernel 以 1:2 的 AIC:AIV 比例编译。两侧失配会导致核编排错乱；这类「跨侧成对契约」与 tilingKey 同属高危同步点。

### 4.4 op_kernel：split_ND 的多输入排布与三段混合核流水线

#### 4.4.1 概念说明

kernel 入口 [ai_infra_manifold_constrained_hyper_connection_pre.cpp:23-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre.cpp#L23-L49) 只做四件事：`GET_TILING_DATA` 解包 TilingData、`GetUserWorkspace` 切出用户 workspace、声明混合核类型，然后按 tilingKey 二选一实例化 kernel 类（L35-41 → 基础类；L42-48 → split_ND 类）。入口参数共 13 个（5 输入 + 6 输出 + workspace + tiling），**顺序与 def 声明顺序一致**——u2-l4 讲过的跨侧契约。

`split_ND` 的「ND」指 nD（即 \(n\times D\)，matmul 的 K 维）。该 kernel 把 **K 维切给多个 Cube 核**、每个核算部分和，再由向量核归约合并。「多输入排布」指 5 个输入在设备侧各有不同的搬运与消费方式，这是本节要重点拆解的：

| 输入 | shape | 排布/消费方式 |
|:---|:---|:---|
| x | [T,N,D] | 唯一的大流量输入；V0 阶段 `DataCopyPad` 分块进 UB，Cast 成 FP32 后**写回 workspace** 供 Cube 用；V1 阶段**再次**从 GM 读原始 x 算 \(h_{in}\) |
| phi | [n²+2n, nD] | 从不进 UB；AIC 侧 `mm.SetTensorB(phiGm_, true)` 直接以 GM 为家、转置读 |
| alpha | [3] | 三个标量 `GetValue` 读出，在 UB 里展开成 [n²+2n] 的逐列向量 |
| bias | [n²+2n] | 一次 `DataCopyPad` 整条进 UB，常驻复用 |
| gamma | [n,D] | **split_ND 不支持**（tiling 已把带 gamma 的请求挡去 key 0） |

#### 4.4.2 核心流程

split_ND 在 AIC:AIV=1:2 混合核上的三段流水线（每个 AIC 配 2 个 AIV 子核，`GetSubBlockIdx()` 区分）：

```text
┌─ 第 1 段 V0（每个 AIV 子核）──────────────────────────────────────────┐
│  K 轴（nD）先按 AIC 核数均分（c0UsedCoreNum_ 份），每核再对半给自己的   │
│  两个 AIV 子核（VectorComputeOffsetV0）                               │
│  循环 T（步长 v0ChunkTSize_，由 40KB 队列预算反推）：                   │
│    DataCopyPad x 分块 → Cast FP32 → Mul 自乘（x²，用于 RMS）           │
│    → DataCopyOutToWorkSpace：FP32 的 x 写入 workspace 的 xFp32 区      │
│    → ReduceSum(行) 得每行部分和 → 写入 workspace 的 invRms 部分和区    │
│  完成后 CrossCoreSetFlag(SYNC_V2C) 通知 Cube                          │
└──────────────────────────────────────────────────────────────────────┘
┌─ 第 2 段 AIC（每个 Cube 核）─────────────────────────────────────────┐
│  CrossCoreWaitFlag(SYNC_V2C) 等第 1 段完成                            │
│  mm.SetTensorA(xFp32 的本核 K 分片) · SetTensorB(phi, 转置)            │
│  IterateAll → 本核部分和 [T, n²+2n] 写入 workspace 的 mmRes 区         │
│  （K 分片超出 nD 的核直接返回）                                        │
└──────────────────────────────────────────────────────────────────────┘
                    ══ SyncAll（全核栅栏，Cube 完成才能进第 3 段）══
┌─ 第 3 段 V1（每个 AIV 子核）─────────────────────────────────────────┐
│  T 轴按 2×核数均分给各子核（VectorComputeOffset），步长 V1_BASE_SIZE=8 │
│  AIVPreLoad（一次性）：alpha 标量展开成 [n²+2n]；bias 整条搬入 UB；     │
│     构造 pre/post/res 三张 Gather 偏移表                               │
│  对每个 8 行块：                                                       │
│   ① InvRmsCopyIn 读 2×c0UsedCoreNum_ 行部分和 → ReduceSum → ×(1/nD)   │
│      +normEps → Sqrt → Div(1/x) ⇒ invRms                              │
│   ② HMixCopyIn 读 c0UsedCoreNum_ 行 mm 部分和 → ReduceSum ⇒ H_mix     │
│   ③ H_mix ×invRms(广播) ×alpha(广播) +bias(广播)                       │
│   ④ Gather×3 切出 H_pre / H_post / H_res；H_res 直接写出 GM           │
│   ⑤ H_post = 2σ(·) 写出 GM；H_pre = σ(·)+hcEps 留 UB                 │
│   ⑥ h_in 循环：逐 token 逐 D 块逐 n 路：DataCopyPad 原始 x → Cast     │
│      → Muls(H_pre[t,n]) → ReduceSum(沿 N) → Cast RINT → 写出 GM       │
└──────────────────────────────────────────────────────────────────────┘
```

workspace 三段布局（与 4.3.2 的公式逐项对应）：

```text
workspace 起址:  [ xFp32: nD×T 个 FP32 ][ invRms 部分和: coreNum×2 行 × T ][ mmRes 部分和: coreNum 份 × T×(n²+2n) ]
```

#### 4.4.3 源码精读

**（1）Init：GM 绑定与 workspace 布局计算。** [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:126-190](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L126-L190)：先把 11 个 GM_ADDR 绑成 `GlobalTensor`——注意 L138 `xFloatGm_` 绑到 workspace 起址（FP32 化的 x 落脚点）；L174-177 用**指针算术**把 `invRmsGm_`、`mmResGm_` 分别绑到 workspace 的第二、三段偏移上（偏移量 = 前段元素数 × 4 字节，与 tiling 公式严格互锁——改一处必须改另一处）。L166-172 由 nD 与核数算出 `c0UsedCoreNum_`（16 对齐的 K 分片）与 `v0ChunkTSize_`（40KB 队列能装下的 T 步长）。L184-189：AIV 核初始化 UB 并用 `Duplicate` 造一段全 1 的 tensor（后面算倒数用 `Div(1, x)`），最后 `SyncAll()`。

**（2）Process：三段的编排。** [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:231-249](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L231-L249)：AIV 先跑 V0 与 PreLoad，AIC 跑矩阵乘，`SyncAll<false>()` 栅栏后 AIV 跑 V1。三段之间只有两次同步（V0→AIC 用事件标志，AIC→V1 用全核栅栏），段内无全局等待。

**（3）V0：Cast、自乘、写 workspace、部分和。** [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:322-364](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L322-L364)：L342-353 是「搬入 → Cast → Mul 自乘 → 写回 workspace」的流水；L357 `ReduceSum<Pattern::Reduce::AR>` 沿行做前缀归约得每行和；L363 `CrossCoreSetFlag<0x2, PIPE_MTE3>(SYNC_V2C)` 通知 Cube 数据就绪。K 分片越界的子核在 L328-331 提前置标志返回。

**（4）AIC：一次 IterateAll 的 K 分片矩阵乘。** [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:275-296](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L275-L296)：L289 `CrossCoreWaitFlag(SYNC_V2C)` 等 V0；L292-294 三行完成整次矩阵乘——A 是 workspace 里的 FP32 x 的本核 K 分片，B 是 phi 且 `true` 表示转置（对应 [ai_infra_manifold_constrained_hyper_connection_pre.h:97](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre.h#L97) 的 `MatmulType<..., float32_t, true>`），`IterateAll` 把部分和写到 mmRes 区的本核行。与基础 kernel 的 AIC（[ai_infra_manifold_constrained_hyper_connection_pre.h:378-410](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre.h#L378-L410)，K 步进 256 逐片循环、ping-pong 双缓冲、逐片 SetFlag）对照着读，能清楚看到「迭代式」与「一次摊平式」两种风格的差异。

**（5）AIV1Prologue：两轮部分和归约 + 三次广播 + Gather 切片。** [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:444-508](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L444-L508) 是本 kernel 的算法心脏：

- L452-455 `InvRmsCopyIn`（搬 `c0UsedCoreNum_*2` 行部分和，见 L612-626 的 blockCount）+ `ReduceSum` 合并所有子核的部分和；
- L458-466 依次 `Muls(scaleMean_)`（即 ×1/nD）→ `Adds(normEps)` → `Sqrt` → `Div(oneUb_)`（除以全 1 tensor 即取倒数）⇒ invRms，四条向量指令正好对应公式里的 RMS 定义；
- L472-476 `HMixCopyIn`（blockCount=`c0UsedCoreNum_`，见 L596-610）+ `ReduceSum` 合并矩阵乘部分和 ⇒ \(H^{mix}\)；
- L479-489 三次 `Broadcast`：invRms 按行广播、alpha 按列广播、bias 按列广播，做 \(H^{mix}\cdot\mathrm{invRms}\cdot\alpha + b\)——**行标量与列向量都在 [lenT, n²+2n] 的矩阵上广播**，把公式三步融成一段 UB 内计算；
- L495-497 三条 `Gather` 用预生成的偏移表（[ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:252-273](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L252-L273) 的 `AIV1GetHSliceOffset` 按 [pre: n | post: n | res: n²] 的列布局生成**字节偏移**）从混合结果里抽列切出三份；
- L499-507 h_res 经队列写出 GM。

**（6）激活与 \(h_{in}\) 尾段。** [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:510-543](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L510-L543)：`AIV1ProcessHPre` 只做 `Sigmoid`+`Adds(hcEps)` 留在 UB（**不写 GM**——因为 tiling 保证此路径 outFlag==0，h_pre 输出是占位空 tensor；对照基础 kernel 的同名函数 [ai_infra_manifold_constrained_hyper_connection_pre.h:655-675](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre.h#L655-L675)，那里 outFlag=1 时会写出 h_pre）；`AIV1ProcessHPost` 做 `2σ(·)` 写出。[ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:386-440](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L386-L440) 是 \(h_{in}\) 三重循环（t × D 块 × n 路）：每路 `DataCopyPad` 原始 x → `Cast` → `Muls(H_pre[t,n])`，n 路在 UB 里摞成 [N, lenD]，L419 `ReduceSum<Pattern::Reduce::RA>` 沿 N 归约一次完成加权求和，L424 `Cast(CAST_RINT)` 收回 bf16 后写出。

**（7）PreLoad：小参数的「一次搬运、全程复用」。** [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:545-561](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L545-L561)：alpha 只有 3 个标量，用 `GetValue` 逐个读 GM，再在 UB 里 `SetValue` 展开成 [n²+2n] 的逐列向量（前 n 个填 α_pre、接着 n 个填 α_post、其余 n² 个填 α_res，与 4.1.5 练习 1 的列布局一致）；bias 一次 `DataCopyPad` 整条进 UB。两者在第 3 段的每个 8 行块里被反复广播使用，搬一次用到底。

#### 4.4.4 代码实践

**实践目标**：把「多输入排布 + 三段流水线」内化为一张自己画的图，并回答「新增 gamma 支持要改哪几处」。

**操作步骤**（源码阅读型 + 画图）：

1. 画流水线图：三列分别标 V0（AIV×2）/ AIC / V1（AIV×2），纵向按时间；用箭头标出两次同步（`CrossCoreSetFlag(SYNC_V2C)` → `CrossCoreWaitFlag`，`SyncAll`）与 workspace 三块区域的写入/读取方。
2. 在图上用五种颜色标 5 个输入的路径：x（进两次）、phi（只到 Cube 的 SetTensorB）、alpha（标量读 + UB 展开）、bias（一次整搬）、gamma（画一个「✗ 本路径不支持」）。
3. 挑战题：若要让 split_ND 支持 gamma，写出改动点清单（提示：V0 阶段 x·γ 的乘法在哪里插？tiling 的模式判定条件要删哪一项？workspace 是否要变？）。

**需要观察的现象**：x 是唯一被消费两次的输入——第一次为矩阵乘服务（Cast 成 FP32 进 workspace），第二次为 \(h_{in}\) 加权求和服务（直接用原始低精度 x，保证 \(h_{in}\) 输出精度与输入一致）。两次读的是同一块 GM，但_dtype 不同、目的不同。

**预期结果**：得到一张三段流水线图；挑战题参考答案：① 在 V0 的 `Cast` 之后、`DataCopyOutToWorkSpace` 之前插入 gamma 的搬运与逐元素乘（参照基础 kernel [ai_infra_manifold_constrained_hyper_connection_pre.h:467-478](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre.h#L467-L478) 的写法：注意 RMS 统计必须用乘 gamma **之前**的 x²）；② tiling 判定条件 `hasGamma_ == 1 ||` 要删掉；③ gamma 还影响 \(h_{in}\) 求和是否用 γ⊙x（本实现用原始 x，不受影响）；④ workspace 不必变（gamma 只改 xFp32 区的内容不改大小）。改完后必须同步审视 RMS 语义是否与 golden 一致——这是一个非常好的「改代码先改对公式」的练习。

#### 4.4.5 小练习与答案

**练习 1**：`Gather` 的偏移表为什么存 `curOffset * sizeof(P)`（字节）而不是元素下标？

**答案**：Ascend C 的 `Gather` 原语按字节偏移取数（` Gather(dst, src, offsetVec, 0, len)` 的 offset 向量是字节粒度）；FP32 占 4 字节，所以生成表时就把元素下标乘上 `sizeof(P)`。生成逻辑在 [ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h:252-273](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_split_ND.h#L252-L273)，每个 8 行块复用同一张表。

**练习 2**：V1 里算 invRms 为什么是 `Div(oneUb_, invRmsBuff_)`（1÷x）而不是 `Rec` 之类？

**答案**：用全 1 tensor 做被除数、逐元素除，等价于取倒数。这是向量指令集里「没有独立倒数指令或倒数精度不达标」时的惯用写法；`oneUb_` 在 Init 里由 `Duplicate<float>(oneUb_, 1.0f, ...)` 一次性造好（L186）。

**练习 3**：`AIV1Process` 的 h_in 循环里，为什么每路 x 只搬 `lenD` 个元素、却要 `Ceil(lenD,16)*16 - lenD` 个右填充？

**答案**：NPU 的向量指令与 DataCopy 按 32 字节（FP32×8 或 BF16×16）对齐高效工作；`DataCopyPadExtParams` 的右填充把不满 16 对齐的尾段补齐，避免越界读和性能惩罚。这与 u2-l4 aggregate_hidden、u4-l3 FA kernel 里的对齐处理是同一套平台约束。

### 4.5 pre 在 MHC 训练步中的位置：与 pre_grad 的镜像关系

#### 4.5.1 概念说明

pre 不是孤立算子，而是 MHC 训练步的「入口生产者」。把本家族（u5-l1~u5-l4）串起来看一个训练 step 的数据流：

```text
前向：  pre ──┬─ hin ──────────────→ Attention / MLP（主体网络）
              ├─ h_res（未归一化）─→ sinkhorn（双随机化）──┐
              └─ h_post ────────────────────────────────→(混合输出，见 u5-l4 post 算子)
反向：  主体网络回传 h_in_grad、混合输出回传 h_post_grad / h_comb_before_grad
        ─→ pre_grad（还消费前向落盘的 inv_rms / mm_res / h_pre / h_post）
        ─→ x_grad、hc_weight_grad(即 φ 的梯度)、alpha_grad、bias_post_grad、(gamma_grad)
```

关键结论：**pre_grad 把 `inv_rms`、`mm_res`、`h_pre`、`h_post` 声明为 REQUIRED 输入**（[ai_infra_manifold_constrained_hyper_connection_pre_grad_def.cpp:52-70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad/op_host/ai_infra_manifold_constrained_hyper_connection_pre_grad_def.cpp#L52-L70)），而这四个量只有前向 `out_flag=1` 时才会真实产出——所以**训练路径必须以 out_flag=1 调用 pre**，out_flag=0 是纯推理选项。这与 Sinkhorn 前向 out_flag=1 落盘 norm_out/sum_out 供 sinkhorn_grad 复用（u5-l1/u5-l2）是同一设计模式在家族内的二次出现：**前向多算多存、反向免重算**。

#### 4.5.2 核心流程

前反向的输入输出对应关系（读两个 def 文件即可完整列出）：

| pre（前向） | pre_grad（反向） | 说明 |
|:---|:---|:---|
| 输入 x | 输出 x_grad | 残差输入的梯度 |
| 输入 phi | 输出 hc_weight_grad | φ 即 MHC 权重矩阵的梯度 |
| 输入 alpha | 输出 alpha_grad | 三路缩放的梯度 |
| 输入 bias | 输出 bias_post_grad | bias 的梯度 |
| 输入 gamma（可选） | 输出 gamma_grad（可选） | RMSNorm 缩放因子梯度 |
| 输出 hin | 输入 h_in_grad | 主体网络回传 |
| 输出 h_post | 输入 h_post_grad + 输入 h_post | 反向既收梯度也复用前向值 |
| 输出 h_res（→sinkhorn 链） | 输入 h_comb_before_grad | sinkhorn/post 链回传的梯度 |
| 输出 inv_rms / mm_res / h_pre（out_flag=1） | 输入 inv_rms / mm_res / h_pre | 前向落盘、反向复用的中间量 |
| —（反向特有） | 输入 grad_x_post（可选） | 残差直连支路的既有梯度累加 |
| 属性 normEps/hcEps | 属性 hc_eps | 反向只需要 hcEps（sigmoid 次梯度） |

#### 4.5.3 源码精读

**（1）pre_grad 的原型。** [ai_infra_manifold_constrained_hyper_connection_pre_grad_def.cpp:22-81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad/op_host/ai_infra_manifold_constrained_hyper_connection_pre_grad_def.cpp#L22-L81)：13 个输入（10 必选 + gamma/grad_x_post 两个可选）；[ai_infra_manifold_constrained_hyper_connection_pre_grad_def.cpp:83-107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad/op_host/ai_infra_manifold_constrained_hyper_connection_pre_grad_def.cpp#L83-L107)：5 个输出（4 必选 + gamma_grad 可选）；L114-116 同样注册 ascend910b/ascend910_93 并只保留 `hc_eps` 一个属性。dtype 约定值得注意：x_grad、grad_x_post 与 x 一样是 BF16/FP16，其余全 FP32。

**（2）目录镜像。** 两个目录的文件一一对应（见 4.5.4 的对照表实践）。差异点有三处：① pre 的 op_kernel 有**两个** kernel 头（基础 + split_ND），pre_grad 只有一个；② pre_grad 的 op_api 多一个 [aclnn_..._pre_grad_v2.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre_grad_v2.h) 与两份 aclnn 文档（V1/V2 两个接口版本），且其 aclnn 实现有 1043 行（含更多分支校验）；③ pre_grad 的 tiling 同样以 `REGISTER_TILING_TEMPLATE`（[ai_infra_manifold_constrained_hyper_connection_pre_grad_tiling.cpp:56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad_tiling.cpp#L56)）注册、kernel 入口同样按 `TILING_KEY_IS(0)` 分发（[ai_infra_manifold_constrained_hyper_connection_pre_grad.cpp:38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad/op_kernel/ai_infra_manifold_constrained_hyper_connection_pre_grad.cpp#L38)）——反向精读留在本讲之后读者可自行按同一框架展开。

**（3）st 测试佐证训练路径。** [test_mhc_pre.py:93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/tests/st/test_mhc_pre.py#L93) 的训练型用例显式传 `out_flag=1` 并核对全部 6 个输出（L96-99），golden 函数 [test_mhc_pre.py:36-77](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/tests/st/test_mhc_pre.py#L36-L77) 的返回顺序 `(h_in, H_post, H_comb_before, inv_rms, H_mix, H_pre)` 与算子 6 输出一一对应——**golden 函数是公式最忠实的机器可读版本**，比 docs 公式更适合当实现对照基准。

#### 4.5.4 代码实践

**实践目标**：列出 pre 与 pre_grad 的目录/文件一一对应关系表（本讲综合实践的第 2 项在此先做口径准备，完整表格见第 5 节）。

**操作步骤**：

1. 用 `find` 列出两个目录的全部文件（或直接对照 4.5.3（2））。
2. 对每个文件标注：属于哪一层（def/infershape/tiling/kernel/api/tests）、前向有反向是否也有、反向独有的文件是什么。
3. 用 `grep -n "Input\|Output" ..._pre_grad_def.cpp` 把 4.5.2 的对应表逐行落到 def 行号上。

**需要观察的现象**：反向目录比前向多出 `aclnn_..._grad_v2.h` 与第二份 aclnn 文档，说明反向接口经历过一次版本演进（V2）；前向多出的 `split_ND.h` 说明前向有「解码小 T」的专属优化，而反向没有——解码场景通常不需要反向，这是训练库前反向能力集不对称的常见成因。

**预期结果**：一张「前向文件 ↔ 反向文件」两列对照表 + 一张「前向 IO ↔ 反向 IO」对应表（4.5.2 即参考答案）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 pre_grad 不需要 `norm_eps` 属性，却需要 `hc_eps`？

**答案**：反向里 invRms 直接拿前向落盘的 inv_rms 用，不再重算 RMS，normEps 无用；而 \(H^{pre}=\sigma(\cdot)+hc\_eps\) 的次梯度 \(\sigma'(z)\) 不含 hcEps，但 \(H^{post}=2\sigma(\cdot)\) 与 sigmoid 次梯度的还原、以及链式法则里对 \(H^{pre}\) 的偏导形式依赖 hcEps 出现的位置——保留 hcEps 供反向公式使用（具体用法可读 pre_grad 的 kernel，本讲不展开）。

**练习 2**：训练框架里若有人误用 `out_flag=0` 跑前向再接反向，会在哪一步以什么方式失败？

**答案**：不会在 pre 前向失败（out_flag=0 是合法推理路径）；会在反向构图/执行时失败——inv_rms/mm_res/h_pre 是空/垃圾 tensor，pre_grad 的 aclnn 层空 tensor 校验（`CheckEmptyTensor` 一类）或 tiling 校验会报参数无效；最坏情况（校验恰好放行）是静默算出错误梯度。**可选输出 + 反向必选输入是「调用方必须懂语义」的契约**，torch 侧 Autograd 封装（u6-l2）正是为了把这种约定封装到框架里。

**练习 3**：pre 的 h_res 输出与 Sinkhorn 算子（u5-l1）是什么关系？

**答案**：pre 产出的 h_res 是「未做 sinkhorn 变换」的混合矩阵（docs 明确标注），它作为 sinkhorn 算子的输入做双随机归一化；归一化后的矩阵才进入 MHC 输出混合。因此 pre → sinkhorn 是家族内**算子间流水线**，而 pre ↔ pre_grad 是**前反向镜像**——两种关系不要混淆。

## 5. 综合实践

综合实践把本讲两根主线收拢：**（A）画出 pre 从 aclnn 接口到 kernel 的完整调用时序图；（B）列出 pre 与 pre_grad 的目录结构一一对应表。**

### 任务 A：完整调用时序图

要求覆盖：两段式调用、executor 的装配（Contiguous/本算子/ViewCopy）、tiling 的触发点、kernel launch 与核内三段。参考答案（文字时序图，可直接抄画成 UML sequence）：

```text
用户(torch侧)        aclnn层(L2)                l0op层(L0)              CANN运行时/op_host          设备(AIC+AIV)
    │  aclnn...PreGetWorkspaceSize                  │                          │                        │
    ├─────────────────>│ CREATE_EXECUTOR            │                          │                        │
    │                  │ Builder 装配 14 参数        │                          │                        │
    │                  │ CheckParams 五步校验        │                          │                        │
    │                  │ l0op::Contiguous ×N ───────>│ (连续化任务入 executor)   │                        │
    │                  │ l0op::AiInfraMHCPre ───────>│ AllocTensor ×6           │                        │
    │                  │                            │ INFER_SHAPE ────────────>│ InferShape4mHCPre      │
    │                  │                            │ ADD_TO_LAUNCHER_LIST ───>│ TilingFunc4mHCPre:     │
    │                  │                            │                          │  模式判定(key 0/1)     │
    │                  │                            │                          │  FillTilingData        │
    │                  │                            │                          │  SetBlockDim/Ws        │
    │                  │ l0op::ViewCopy ×6 ─────────>│ (回拷任务入 executor)     │                        │
    │                  │ *wsSize = GetWorkspaceSize  │                          │                        │
    │  <─ executor, wsSize                            │                         │                        │
    │  申请 workspace, aclnn...Pre(ws, size, executor, stream)                  │                        │
    ├─────────────────>│ CommonOpExecutorRun ───────────────────────────────────┼───────────────────────>│ 按序执行:
    │                  │                            │                          │                        │  Contiguous kernels
    │                  │                            │                          │                        │  pre kernel:
    │                  │                            │                          │                        │   AIV:V0(cast/平方/部分和)
    │                  │                            │                          │                        │   AIC:matmul K分片
    │                  │                            │                          │                        │   SyncAll
    │                  │                            │                          │                        │   AIV:V1(归约/广播/Gather/hin)
    │                  │                            │                          │                        │  ViewCopy kernels
```

画完后自查三个锚点：① tiling 发生在第一段的 `ADD_TO_LAUNCHER_LIST_AICORE` 内（不是第二段）；② kernel 侧按 `TILING_KEY_IS` 选类发生在 launch 之后的设备侧入口；③ 六次 ViewCopy 是第二段真实执行的独立 kernel。

### 任务 B：前反向目录对照表

参考答案（`✓` 表示存在该文件；相对 `ascendc/src/ops-transformer/mhc/`）：

| 层 | pre（前向） | pre_grad（反向） |
|:---|:---|:---|
| 原型注册 | op_host/..._pre_def.cpp ✓ | op_host/..._pre_grad_def.cpp ✓ |
| shape 推导 | op_host/..._pre_infershape.cpp ✓ | op_host/..._pre_grad_infershape.cpp ✓ |
| tiling | op_host/..._pre_tiling.h/.cpp ✓ | op_host/..._pre_grad_tiling.h/.cpp ✓ |
| kernel 入口 | op_kernel/..._pre.cpp ✓ | op_kernel/..._pre_grad.cpp ✓ |
| kernel 实现 | op_kernel/..._pre.h（基础）+ **..._pre_split_ND.h（小 T 专属）** | op_kernel/..._pre_grad.h（单实现） |
| aclnn 对外 | op_api/aclnn_..._pre.h/.cpp ✓ | op_api/aclnn_..._pre_grad.h/.cpp ✓ + **aclnn_..._pre_grad_v2.h** |
| l0op 内层 | op_api/ai_infra_..._pre.h/.cpp ✓ | op_api/ai_infra_..._pre_grad.h/.cpp ✓ |
| 文档 | docs/npu_..._pre.md + aclnnAiInfra...Pre.md | docs/npu_..._pre_grad.md + **两份** aclnn 文档（Grad 与 GraV2） |
| st | tests/st/test_mhc_pre.py ✓ | tests/st/test_mhc_pre_grad.py ✓ |
| ut | tests/ut/op_host/（tiling+infershape）+ tests/ut/op_api/ ✓ | 同构三件 ✓ |
| 构建脚本 | CMakeLists.txt（根 + tests 三级）✓ | 同构 ✓ |

### 任务 C（可选，需 NPU 环境）

在装好算子包与 torch_npu 的环境里跑通 [tests/st/test_mhc_pre.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/tests/st/test_mhc_pre.py)，并把 4.3.4 手算的 tiling 参数与 `PrintTilingData` 日志对照。无 NPU 环境时，缺的前置条件是：u1-l3 的 docker 环境、u1-l4 的算子包编译安装（`bash build.sh -n ai_infra_manifold_constrained_hyper_connection_pre -c ascend910_93`）、以及 torch_ops_extension 的 wheel（u6-l1）——本项**待本地验证**。

## 6. 本讲小结

- pre 是 MHC 训练步的入口生产者：一次调用产出 \(h_{in}\)（喂 Attention/MLP）、\(H^{post}\) 与 \(H^{res}\)（\(H^{res}\) 交 Sinkhorn 归一化），`out_flag=1` 时额外落盘 inv_rms/mm_res/h_pre 三个反向必需的中间量——训练路径必须开 out_flag。
- op_api 全链路分工清晰：aclnn 层（Builder 装配 → 五步校验 → Contiguous → l0op → 6×ViewCopy）是对外契约与适配；l0op 层（AllocTensor → INFER_SHAPE → ADD_TO_LAUNCHER_LIST_AICORE）是内部原子，tiling 在 ADD_TO_LAUNCHER 时被触发；所有 Host 工作必须在第一段完成。
- tiling 用单模板实现「双模式」：`totalLength>180 || hasGamma || outFlag==1` 走 tilingKey=0 基础 kernel（T 分块迭代、ping-pong workspace）；小 T 走 tilingKey=1 split_ND kernel（K 轴摊到全部核、一次摊平），后者以收窄能力集（无 gamma、无落盘）换解码场景的核利用率。
- split_ND 的三段流水线：V0（AIV：Cast/自乘/部分和 + FP32 x 落 workspace）→ AIC（K 分片矩阵乘部分和）→ SyncAll → V1（AIV：两轮部分和归约、invRms 四连指令、三次 Broadcast、Gather 切三份、sigmoid 激活、h_in 加权归约）；workspace 三段布局与 tiling 公式逐字节互锁。
- 5 个输入 5 种排布消费：x 两进两出（workspace + 原始直读）、phi 只在 Cube 转置读、alpha 标量展开、bias 一次整搬常驻、gamma 由 tiling 挡在基础 kernel。
- pre 与 pre_grad 目录逐层镜像，反向把前向落盘的三个中间量声明为 REQUIRED 输入；「可选输出（前向）↔ 必选输入（反向）」是这类训练算子对的核心契约，torch 侧 Autograd 封装负责把它藏进框架。

## 7. 下一步学习建议

- **u5-l4（下一篇）**：MHC 后处理算子 post、post_grad 与聚合反向 mhc_post_grad——把 pre →（主体网络/sinkhorn）→ post 的前向闭环和反向聚合补全，你会再次看到「前向落盘中间量」的模式。
- **回头补线**：pre_grad 的 tiling（[ai_infra_manifold_constrained_hyper_connection_pre_grad_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad/op_host/ai_infra_manifold_constrained_hyper_connection_pre_grad_tiling.cpp)）与 1043 行的 aclnn 实现（[aclnn_ai_infra_manifold_constrained_hyper_connection_pre_grad.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre_grad/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre_grad.cpp)）是很好的自测材料：能否独立用本讲的框架走读？
- **向上游看**：torch.ops.custom.npu_manifold_constrained_hyper_connection_pre 这一层的 csrc/converter 封装在 torch_ops_extension 里（u6-l2/u6-l3 精读），Autograd Function 如何自动带上 out_flag=1 是那里的话题。
- **横向对照**：把本讲的 split_ND「K 轴部分和归约」与 u4-l4 FA 反向的确定性版本（每核独立 workspace + 固定顺序合并）对照，体会「部分和 + 归约」这一手法在确定性与并行度之间的取舍。
