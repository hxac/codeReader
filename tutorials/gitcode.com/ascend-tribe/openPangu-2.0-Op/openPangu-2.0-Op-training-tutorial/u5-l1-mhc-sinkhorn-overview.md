# u5-l1 MHC 算法背景与 SinkhornEnhance 前向

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 MHC（Manifold Constrained Hyper Connection，流形约束超连接）架构中 Sinkhorn 归一化要解决的问题：把一个方阵迭代打磨成**近似双随机矩阵**。
2. 按文档公式（`norm_out[k]` / `sum_out[k]` 的交替生成规则）手算一轮 Sinkhorn 迭代，并说出中间量为什么必须落盘保存——给反向算子 `ai_infra_sinkhorn_grad` 复用。
3. 解释本算子「两级 tiling」的复用关系：`*_tiling_base.cpp` 是只有十几行的薄入口，真正的切分逻辑全部住在 `*_tiling.cpp` 的 `SinkhornTilingBase` 里，靠 u3-l3 学过的 tiling_base 责任链框架串起来。
4. 走读训练路径 kernel `SinkhornGeneralized` 的五个步骤（CopyIn→Transpose→InitialSoftmax→行列交替归一化→Transpose+CopyOut），并把公式里的 softmax/sum/div 逐一对应到 `Exp`/`ReduceSum`/`Div` 等向量原语。
5. 用 numpy 独立实现完整 Sinkhorn 迭代并验证输出近似双随机。

## 2. 前置知识

### 2.1 MHC 与 Sinkhorn：算法直觉

本仓库只有算子、没有模型侧代码，以下是理解算子所需的算法背景（模型细节以 MHC 原论文为准）：

- **超连接（Hyper Connection）**：一类改造 Transformer 残差流的架构，把「每层一个残差分支」推广为「多路并行分支按动态权重混合」，其中分支间混合权重由一个 \( N \times N \) 的小方阵描述（\(N\) 即权重矩阵的行列数，本算子支持 4/6/8）。
- **流形约束（Manifold Constrained）**：希望这个混合矩阵落在「双随机矩阵」流形上——即**每一行元素之和为 1，每一列元素之和也为 1**。行和为 1 保证各路信号的能量守恒（不放大不衰减），列和为 1 则保证每一路都被完整分配。这既是正则化手段，也让训练更稳定。
- **Sinkhorn 迭代**：把任意正矩阵变成近似双随机矩阵的经典算法。直觉非常朴素：行和不是 1 吗？每行除以自己的行和；除完列和又不是 1 了，每列再除以列和；反复交替，行和与列和同时收敛到 1。数学上可以证明该迭代收敛到与原矩阵「最接近」（KL 散度意义下）的双随机矩阵。

本算子输入文档明确写了它的定位：对 mHC 架构中的 \(\mathbf{H}'_{\text{res}}\) 矩阵执行 Sinkhorn 迭代归一化，得到双随机矩阵 \(\mathbf{H}_{\text{res}}\)（见 [npu_sinkhorn.md:L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/docs/npu_sinkhorn.md#L17)）。

### 2.2 防除零的 epsilon

每一步「除以行/列和」都可能出现和为 0（输入全 0 或下溢），所以每次求和后都加一个小正数 \(\epsilon\)（默认 `1e-6`）再除。这个细节贯穿公式与 kernel 代码，是阅读时的「高频配角」。

### 2.3 承接前讲已建立的心智模型

本讲不重复以下内容，只直接使用：

- **u2-l2/u2-l3**：`_def.cpp` 的 OpDef 注册语义（Input/Output/Attr 链式声明、`AICore().AddConfig` 芯片白名单、`OP_ADD` 宏），以及 Tiling 的四项产出契约（blockDim / tilingKey / TilingData 字节流 / workspace）。
- **u2-l4**：kernel 入口 `extern "C" __global__ __aicore__` 三件事——按 `TILING_KEY_IS` 选分支、用 `GET_TILING_DATA` 解包 TilingData、实例化模板类跑 `Init + Process`；以及 `TPipe`/`TQue`/`TBuf` 的 UB 管理角色。
- **u3-l3**：tiling_base 框架的模板方法七步流程（`GetPlatformInfo → GetShapeAttrsInfo → IsCapable → DoOpTiling → DoLibApiTiling → GetWorkspaceSize → PostTiling`）与 `REGISTER_OPS_TILING_TEMPLATE` 优先级注册。本讲是这套框架的一个「单实现」落地样本。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/docs/npu_sinkhorn.md` | 算子规格文档：产品支持、计算公式、函数原型、参数/返回值/约束 |
| `.../op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp` | 原型注册：1 必选输入、3 输出（2 可选）、3 属性，A2/A3 双芯片 |
| `.../op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_proto.cpp` | InferShape/InferDataType 注册 |
| `.../op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.h` | `SinkhornTilingData` 结构体定义与 `SinkhornCompileInfo` |
| `.../op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp` | **第一级 tiling**：把算子名绑到框架注册表，十几行的薄入口 |
| `.../op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp` | **第二级 tiling**：`SinkhornTilingBase` 全部切分与校验逻辑 |
| `.../op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance.cpp` | kernel 入口：按 tilingKey 分发训练/推理两条路径 |
| `.../op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h` | **训练路径** kernel（本讲主角）：转置布局 + 行列交替归一化 |
| `.../op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized_infer.h` | 推理路径 kernel：DataCopyPad 布局，无中间量输出（对照用） |
| `.../tests/st/test_npu_sinkhorn.py` | ST 测试，自带 CPU 参考实现（本讲实践的对照标杆） |

（表中 `...` 为 `ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance` 的缩写。）

## 4. 核心概念与源码讲解

### 4.1 MHC 算子家族与 Sinkhorn 的角色

#### 4.1.1 概念说明

MHC 是本仓库三大算子家族之一。`ascendc/src/ops-transformer/mhc/` 下共 7 个目录，覆盖 MHC 训练一步所需的完整算子链：

| 目录 | 角色 |
| --- | --- |
| `ai_infra_manifold_constrained_hyper_connection_pre`（+ `_grad`） | MHC 前处理及其反向 |
| `ai_infra_manifold_constrained_hyper_connection_post`（+ `_grad`） | MHC 后处理及其反向 |
| `ai_infra_mhc_post_grad` | 聚合多路梯度的反向算子 |
| `manifold_constrained_hyper_connection_sinkhorn_enhance` | **本讲**：Sinkhorn 归一化前向 |
| `ai_infra_sinkhorn_grad` | Sinkhorn 反向（u5-l2 主角） |

前向/反向成对出现，再次印证 u1-l1 的结论：训练算子库的典型特征是每个前向算子都有配套 `_grad`。

#### 4.1.2 核心流程

MHC 训练步中与 Sinkhorn 相关的一段：

```text
超连接权重打分 → H'_res（N×N 方阵，每个 token 一份）
      │
      ▼
SinkhornEnhance（本算子）：H'_res → H_res（近似双随机）
      │  ├─ output          = 最终归一化结果
      │  ├─ norm_out[0..2K-1] = 每次归一化后的矩阵快照
      │  └─ sum_out[1..2K-1]  = 每次归一化用的分母（行/列和+ε）
      ▼
H_res 参与后续计算 …… 反向时 SinkhornGrad 消费 norm_out/sum_out 复原雅可比
```

#### 4.1.3 源码精读

算子功能的官方一句话定义在文档中：

- [npu_sinkhorn.md:L15-L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/docs/npu_sinkhorn.md#L15-L21)：功能说明——对 mHC 的 \(\mathbf{H}'_{\text{res}}\) 执行 Sinkhorn 迭代归一化得到双随机矩阵 \(\mathbf{H}_{\text{res}}\)，并支持输出中间的 norm_out/sum_out 供反向梯度计算。

产品支持情况（同文档 L3-L12）：仅 A2（`ascend910b`）与 A3（`ascend910_93`）支持。这与 `_def.cpp` 的芯片注册互证：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp:L47-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp#L47-L56)：`OpAICoreConfig` 打开动态 shape/格式/维数等能力开关后，`AddConfig("ascend910_93")` 与 `AddConfig("ascend910b")` 各注册一份——编译期芯片白名单与文档表格一致（910_95 不在列）。

#### 4.1.4 代码实践

**实践目标**：建立 MHC 家族的目录级认知，确认 Sinkhorn 在链路中的位置。

**操作步骤**：

1. 执行 `ls ascendc/src/ops-transformer/mhc/`，核对上表的 7 个目录。
2. 对每个目录执行 `ls <目录>/op_host/ | grep def`，确认哪些算子有成对的 `_grad` 目录（pre/post/sinkhorn 三族）。
3. 打开 `ai_infra_sinkhorn_grad/docs/` 下的文档，只看「功能说明」一节，确认它消费的是前向保存的中间量。

**需要观察的现象**：7 个目录中 5 个与 pre/post/sinkhorn 三族前反向对应，反向目录数 ≥ 前向目录数。

**预期结果**：能画出 4.1.2 的流程图并标注每格对应的算子目录名。（目录清单已由上文 `ls` 结果证实；grad 文档细节待本地阅读确认。）

#### 4.1.5 小练习与答案

**练习 1**：MHC 家族为什么需要 `ai_infra_mhc_post_grad` 这个独立的聚合反向算子，而不是并入 `post_grad`？
**答案**：post 前处理可能被多条路径/多路输出共享，反向时同一路前向会产生多份梯度，需要聚合（累加）后再沿链路回传；聚合逻辑与前向逐点对应的求导逻辑职责不同，故独立成算子。（具体接口差异在 u5-l4 展开。）

**练习 2**：文档表格里 `昇腾910_95` 不支持，源码里对应哪一行？
**答案**：`_def.cpp` 的 `AddConfig` 只调用了 `"ascend910_93"` 与 `"ascend910b"` 两次（L55-L56），没有 `ascend950`，因此 950 编译期就排除，与文档一致。

---

### 4.2 算法规格：接口、公式与 norm_out / sum_out 契约

#### 4.2.1 概念说明

这是本讲的「算法核心」模块。要回答三个问题：

1. 输入输出长什么样？（接口契约）
2. 迭代到底怎么算？（公式）
3. 为什么要保存 \(2 \times num\_iters\) 份中间量？（与反向的契约）

关键洞察：Sinkhorn 迭代是**一串除法**，反向求导时每个除法的分母都要用到；若前向不保存，反向就得把整个迭代重放一遍。因此训练路径（`out_flag=1`）把每次归一化的「结果矩阵」和「分母」全部落盘成 `norm_out`/`sum_out`，用空间换反向时间。

#### 4.2.2 核心流程

**接口**（[npu_sinkhorn.md:L49-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/docs/npu_sinkhorn.md#L49-L73)）：

```python
torch.ops.custom.npu_sinkhorn(x, *, out_flag=0, eps=1e-6, num_iters=20)
    -> (output, norm_out, sum_out)
```

- `x`：`[B,S,N,N]` 或 `[T,N,N]`，仅 float32，N ∈ {4,6,8}。
- `out_flag`：0 只算 output（推理路径）；1 额外输出 norm_out/sum_out（训练路径）。
- `norm_out`：`[2*num_iters, N, N, B,S]` 或 `[2*num_iters, N, N, T]` —— 注意**做了维度重排，t 维被挪到最后**。
- `sum_out`：`[2*num_iters, N, B,S]` 或 `[2*num_iters, N, T]`。

**公式**（[npu_sinkhorn.md:L22-L44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/docs/npu_sinkhorn.md#L22-L44)）：

初始化（第 0 次迭代，先做一次 softmax 保证矩阵为正，再做一次列归一化）：

\[
\begin{aligned}
\mathbf{norm\_out}[0] &= \mathrm{softmax}(\mathbf{x}, \dim=-1), \\
\mathbf{cur} &= \mathbf{norm\_out}[0] + \epsilon, \\
\mathbf{sum\_out}[1] &= \sum_{\dim=-2}\mathbf{cur} + \epsilon, \qquad
\mathbf{norm\_out}[1] = \frac{\mathbf{cur}}{\mathbf{sum\_out}[1]},
\end{aligned}
\]

第 \(i\) 次迭代（\(i = 1, \dots, num\_iters-1\)），行、列各归一化一次，各产生一对 sum/norm：

\[
\begin{aligned}
\mathbf{sum\_out}[2i] &= \sum_{\dim=-1}\mathbf{norm\_out}[2i-1] + \epsilon, \qquad
\mathbf{norm\_out}[2i] = \frac{\mathbf{norm\_out}[2i-1]}{\mathbf{sum\_out}[2i]}, \\
\mathbf{sum\_out}[2i+1] &= \sum_{\dim=-2}\mathbf{norm\_out}[2i] + \epsilon, \qquad
\mathbf{norm\_out}[2i+1] = \frac{\mathbf{norm\_out}[2i]}{\mathbf{sum\_out}[2i+1]},
\end{aligned}
\]

最终：

\[
\mathbf{output} = \mathbf{norm\_out}[2 \times num\_iters - 1]
\]

下标规律：**偶数下标 = 行归一化（沿 \(\dim=-1\) 即每行求和），奇数下标 = 列归一化（沿 \(\dim=-2\) 即每列求和）**；`sum_out[0]` 恒为 0（占位）。`npu_sinkhorn.md` 把初始的 \(+\epsilon\) 直接并进 `norm_out[0]`，而同目录 aclnn 文档与 kernel 实现（见 4.4.3）是「先存不含 ε 的 softmax 结果，再加 ε 进 cur」——两者数值只差每元素一个 ε，实现以 aclnn 文档为准。

**小规模手算例子**（建议读者亲手算一遍）：取 \(N=2\)、\(\epsilon=0\)、\(\mathbf{x}=\begin{pmatrix}1 & 0\\ 0 & 1\end{pmatrix}\)。softmax 后仍是单位阵，行和列和已是 1，任意次迭代输出不变——这提示 softmax 初始化后矩阵每行和为 1，第一次只需「列归一化」补齐列和，与公式的「先 softmax、再列归一化、之后行+列交替」结构吻合。

#### 4.2.3 源码精读

`_def.cpp` 把上述接口翻译成注册代码：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp:L23-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp#L23-L46)：`x` 为必选输入（仅 `DT_FLOAT`、`FORMAT_ND`、`AutoContiguous`）；`output` 必选，`norm_out`/`sum_out` 为 **OPTIONAL 输出**（对应 `out_flag=0` 时不物化）；三个属性 `out_flag`（int，默认 0）、`eps`（float，默认 1e-6）、`num_iters`（int，默认 20）与文档一一对应。
- [manifold_constrained_hyper_connection_sinkhorn_enhance_proto.cpp:L62-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_proto.cpp#L62-L64)：`IMPL_OP_INFERSHAPE` 注册 InferShape/InferDataType。

属性合法性校验不在 def 层，而在 tiling 层（`eps > 0`、`num_iters ∈ [1,100]`、`out_flag ∈ {0,1}`），这正是 u2-l2 讲过的分工：「def 管能力开关，数值校验归 tiling」。

#### 4.2.4 代码实践

**实践目标**：用 numpy 手写公式，验证你真的理解了下标规律（完整代码见第 5 节综合实践，这里是规格对表练习）。

**操作步骤**：

1. 写一个 `sinkhorn_ref(x, eps, num_iters)`，按 4.2.2 公式逐步实现，用两个列表收集 `norm_cache` / `sum_cache`。
2. 取 `x = torch.randn(2, 3, 4, 4)`（B=2,S=3,N=4），`num_iters=3`，检查 `norm_cache` 长度是否等于 \(2 \times 3 = 6\)。
3. 对照仓库自带的 CPU 参考实现 [test_npu_sinkhorn.py:L31-L74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/tests/st/test_npu_sinkhorn.py#L31-L74)（`_cpu_sinkhorn`：Step 0 softmax → Step 1 列归一化 → Step 2 循环「行归一化+列归一化」），核对你的循环结构是否一致。

**需要观察的现象**：`sum_cache[0]` 是 None（占位零矩阵），其余每步一个分母；`norm_cache` 恰有 \(2 \times num\_iters\) 项。

**预期结果**：你的实现与 `_cpu_sinkhorn` 在相同输入下数值一致（差异在浮点误差内）。（本机无 NPU，该对照为纯 CPU 计算，可直接验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么第一次初始化用 softmax 而不是直接从 \(\mathbf{x}\) 开始除行和？
**答案**：Sinkhorn 的收敛性要求矩阵元素严格为正。原始 \(\mathbf{x}\) 可能有负值甚至全 0 行，softmax 先把每行变成和为 1 的正分布，保证后续除法安全且迭代收敛。

**练习 2**：`norm_out` 的 layout 为什么是 `[2*num_iters, N, N, B, S]` 而不是 `[2*num_iters, B, S, N, N]`？
**答案**：把 t 维（B×S 展开后的 token 维）放到最后一维、使其在内存连续，反向算子 `ai_infra_sinkhorn_grad` 按 token 读取中间量时访问完全连续；同时这正好是 kernel 转置后的 UB 布局（见 4.4.3 的 `TransposeXIn`），拷出无需再转置一次。

**练习 3**：`num_iters` 越大，行和/列和越接近 1，那为什么不设成很大？
**答案**：每多一次迭代就多两次全矩阵的 sum+div，且 `norm_out`/`sum_out` 显存随 \(2 \times num\_iters\) 线性增长；tiling 层因此把 `num_iters` 限制在 [1,100]（见 4.3.3）。实际收敛很快（线性收敛），默认 20 已足够。

---

### 4.3 两级 Tiling：薄入口 tiling_base 与 SinkhornTilingBase

#### 4.3.1 概念说明

本算子的 op_host 有两个 tiling 文件，这是 u3-l3 tiling_base 框架的标准用法：

- **第一级 `*_tiling_base.cpp`**：只做「挂接」——用 `IMPL_OP_OPTILING` 把算子的 tiling 函数指到框架注册表的 `DoTilingImpl`，再用 `TilingParse` 挂编译期上下文。它不含任何切分逻辑。
- **第二级 `*_tiling.cpp`**：定义 `SinkhornTilingBase`（继承框架的 `TilingBase`），实现七步流程的全部内容，并用 `REGISTER_OPS_TILING_TEMPLATE` 以优先级 2000 注册进模板注册表。

复用关系：正因为调度逻辑全部委托给框架，未来若要给 Sinkhorn 增加第二种切分策略（如 FA 那样的多模板责任链），只需再写一个子类、再注册一个优先级，**第一级入口一行都不用改**。这就是「入口与实现分离」的收益。

#### 4.3.2 核心流程

一次 tiling 调用的完整链路：

```text
CANN 调 TilingForSinkhorn(context)
        │
        ▼
TilingRegistry::GetInstance().DoTilingImpl(context)     ← 框架责任链（按优先级）
        │  取出注册表里优先级 2000 的 SinkhornTilingBase
        ▼
TilingBase 七步模板方法（u3-l3）：
  GetPlatformInfo   → AIV 核数、UB 大小
  GetShapeAttrsInfo → x 的 shape/dtype、三个属性、三个输出 shape
  DoOpTiling        → CheckInputShape / CheckOutputShape / (CheckOptionalOutputShape)
                      → SplitCores（核间 + 核内两级切分）
                      → 预计算 reduceMask（仅推理路径）
  GetWorkspaceSize  → 固定 16MB 预留
  PostTiling        → SetBlockDim(needCoreNum)、SaveToBuffer 写 TilingData
  GetTilingKey      → out_flag=0 → key=1（推理）；out_flag=1 → key=0（训练）
```

切分维度设计：输入把 batch/序列维统统视为「t 维」（`[B,S,N,N]` 时 `totalLength = B*S`；`[T,N,N]` 时 `totalLength = T`），于是数据成为 `totalLength` 个独立的 \(N \times N\) 小方阵，**方阵之间没有任何数据依赖**——这是最理想的按 t 均分多核的场景。

#### 4.3.3 源码精读

**第一级薄入口**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp:L23-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp#L23-L36)：`TilingForSinkhorn` 只有一行——转发给 `TilingRegistry::DoTilingImpl`；`IMPL_OP_OPTILING(ManifoldConstrainedHyperConnectionSinkhornEnhance).Tiling(TilingForSinkhorn).TilingParse<SinkhornCompileInfo>(...)` 完成算子名与框架的绑定。整个文件不到 40 行。

**TilingData 契约**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.h:L23-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.h#L23-L47)：`SinkhornTilingData` 共 16 个字段，分四组——属性透传组（`nNum/tAlign/outFlag/eps/numIters`）、总量组（`totalLength/needCoreNum`）、**核间切分组**（`perCoreElements/lastCoreElements`）、**核内切分组**（头核与尾核各三件套：`Loops/PerLoopElements/LastLoopElements`），外加推理专用的 `reduceMask`。注意这里用的是 `REGISTER_TILING_DATA_CLASS` 注册，因此 kernel 侧解包宏是 `GET_TILING_DATA`（而非 u2-l4 见过的 `GET_TILING_DATA_WITH_STRUCT`）。

**平台与输入信息**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L143-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L143-L155)：`GetPlatformInfo` 取 AIV 核数与 UB 大小。
- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L157-L227](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L157-L227)：`GetShapeAttrsInfo` 校验 dtype 必须是 float32，读取三个属性并校验范围（`out_flag ∈ {0,1}`、`eps > 0`、`num_iters ∈ [1,100]`，L183-L206），`out_flag=1` 时才去取 norm_out/sum_out 的 shape（L216-L224）。

**shape 校验**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L258-L316](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L258-L316)：`CheckInputShape` 接受 3 维（`[T,N,N]`）或 4 维（`[B,S,N,N]`），要求末两维相等（方阵）；训练路径 N ≤ 12（`MAX_N_NUM_FLAG1`），推理路径 N ≤ 8（`MAX_N_NUM_FLAG0`）。注意文档承诺「N 仅支持 4、6、8」，tiling 放得更宽——文档是对外契约，代码是内部上界。
- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L359-L435](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L359-L435)：`CheckOptionalOutputShape` 逐维核对 `norm_out = [2*numIters, N, N, T或B,S]`、`sum_out = [2*numIters, N, T或B,S]`——把 4.2.2 的输出 layout 变成了硬校验。

**两级切分**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L437-L489](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L437-L489)：`SplitCores`。核间：`perCoreElements = CeilDiv(totalLength, aivNum)`，训练路径不小于 32 且向 8 对齐（32bit/4B），推理路径允许细到 8；由此得 `needCoreNum` 与 `lastCoreElements`。核内：`tAlign = CalTAlign()` 决定「一个核一次循环装多少个 t」，头核/尾核各算出 `Loops / PerLoopElements / LastLoopElements` 三件套写入 TilingData。
- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L229-L256](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L229-L256)：`CalTAlign` 就是 UB 预算公式：训练路径需容纳 \( (5tn^2 + 4tn) \times 4\text{B} + (2n^2+1)\times 512\text{B} < 190\text{KB}\)（五个 \(t n^2\) 级 buffer + 四个 \(tn\) 级 buffer + Transpose 共享 buffer），解出最大 t 再向下对齐到 8；推理路径只需 6 个 buffer 槽（\(6 \times t \times n \times 8 \times 4\text{B}\)）且不超过 32。这与 4.4.3 里 kernel `InitBuffer` 的分配逐项对应——**Host 的公式就是 Device 的账本**。

**收尾与注册**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L539-L561](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L539-L561)：`GetWorkspaceSize` 固定 16MB 预留（L48 常量）；`PostTiling` 把 `needCoreNum` 写入 blockDim、TilingData 序列化进 RawTilingData；`GetTilingKey` 返回 `out_flag=0 → 1`（推理）、`out_flag=1 → 0`（训练）——注意 key 值与语义是「反着的」，读代码时别想当然。
- [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L580](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L580)：`REGISTER_OPS_TILING_TEMPLATE(算子名, SinkhornTilingBase, 2000)` ——优先级 2000，注册表中只有一个实现，责任链退化为单节点（对比 FA 前向的六级链）。

#### 4.3.4 代码实践

**实践目标**：验证你理解 UB 预算公式与核间切分。

**操作步骤**：

1. 手算：A2 平台 UB 约 256KB（代码按 190KB 预留），N=8、float32、`out_flag=1` 时，代入 `CalTAlign` 公式求 `tAlign`。
   - 分母 \((5 \times 64 + 4 \times 8) \times 4 = 1312\) B/t；分子 \(190 \times 1024 - (2 \times 64 + 1) \times 512 = 189440\)；\(t_{max} \approx 144\)，向下对齐 8 → **tAlign = 144**。
2. 再算：`totalLength = B*S = 8*1024 = 8192`，A3 有 50 个 AIV 核时，`perCoreElements = CeilDiv(8192, 50) = 164`，对齐到 8 → 168，`needCoreNum = CeilDiv(8192,168) = 49`，`lastCoreElements = 8192 - 168*48 = 128`。
3. 打开 UT 测试 [test_manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/tests/ut/op_host/test_manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp)，找一条用例的期望 `blockDim`/`perCoreElements`，与你的手算对照（按用例标注的核数）。

**需要观察的现象**：手算的 `tAlign`、`needCoreNum` 与 UT 断言一致（核数以用例为准）。

**预期结果**：若不一致，检查是否用错了核数或忘了 8 对齐。（UT 具体断言值待本地运行验证；手算方法本身已给出。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `perCoreElements` 要对齐到 8（32bit）？
**答案**：NPU 的 DataCopy/向量指令按 32B 对齐搬运数据；每个 t 的矩阵在 GM 上连续排布，t 数不对齐 8（float32 时 8 个元素恰 32B）会让核间边界落在非对齐地址，搬运效率下降甚至越界。tAlign 也同理用 `AlignDown(..., 8)`。

**练习 2**：第一级 `tiling_base.cpp` 里 `TilingPrepareForSinkhorn` 什么都不做直接返回 SUCCESS，那 `TilingParse<SinkhornCompileInfo>` 有什么用？
**答案**：`TilingParse` 是编译期（图编译/算子缓存命中判断）钩子，`SinkhornCompileInfo`（tiling.h L49-L52，仅 `aivNum/aicNum`）描述算子依赖的编译信息；本算子的实现为空说明它对编译缓存无特殊依赖，但仍需占位注册以满足框架接口。

**练习 3**：训练路径 `MIN_PER_CORE_ELEMENTS=32`，推理路径放宽到 8，为什么？
**答案**：训练 kernel 每次要装 5 个 \(tn^2\) 级 buffer，t 太小则 UB 利用率低、循环开销占比大，故要求每核至少 32 个 t；推理 kernel 用 DataCopyPad 把每行 pad 到 8、buffer 只有 6 个槽，单 t 开销小，允许更细粒度分核以提高小批量场景的多核利用率（tiling.cpp L445 的注释即此意）。

---

### 4.4 训练路径 Kernel：SinkhornGeneralized 的转置布局与行列交替归一化

#### 4.4.1 概念说明

kernel 入口按 tilingKey 分发两条路径：

- **key=0（训练，`out_flag=1`）**：`SinkhornGeneralized<T>`。布局策略是「**整体 Transpose**」——把每个核分到的 `[t, n, n]` 转成 `[n, n, t]` 再计算。原因：归一化的除法都是「同一矩阵位置、跨 t 的批量操作」（SIMD 一次管 128 个 t），t 放最后一维正好让向量指令满载；算完再转置回去。
- **key=1（推理，`out_flag=0`）**：`SinkhornInferGeneralized<T, N>`。不转置，直接 DataCopyPad 每行 pad 到 8（`INFER_ALIGN_N=8`），用 `BlockReduce` + Host 预计算的 `reduceMask` 归约；N=4 时有编译期特化分支。本模块以训练路径为主，推理路径只做对照。

#### 4.4.2 核心流程

训练路径每个核的主循环（`Process`，对 `currentCoreloops_ 次` 循环）：

```text
CopyInX(offset)            # GM→UB，搬 currentLoopElements*n*n 个元素（DataCopyPad）
TransposeXIn               # [t,n,n] → [n,n,t]（Transpose 原语 + tmpBuf 共享空间）
InitialSoftmax             # norm_out[0]=softmax(x, dim=-1)；cur = norm_out[0]+eps
  └ out_flag=1 时拷出 norm_out[0]
ColNormalize               # sum_out[1]=列和+eps；norm_out[1]=cur/sum_out[1]
  └ 拷出 norm_out[1]、sum_out[1]
for iter in 1..num_iters-1:
    RowNormalize           # sum_out[2i]=行和+eps；  norm_out[2i]=…/sum_out[2i]   → 拷出
    ColNormalize           # sum_out[2i+1]=列和+eps；norm_out[2i+1]=…/sum_out[2i+1] → 拷出
TransposeXOut              # [n,n,t] → [t,n,n]
CopyOut(offset)            # 写回 output
```

注意「先 ColNormalize 一次、再循环 Row+Col」的结构与 4.2.2 公式严格对应：softmax 已保证行和为 1，初始化只需补列归一化。

#### 4.4.3 源码精读

**入口分发**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance.cpp:L23-L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance.cpp#L23-L67)：`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 声明纯向量核，`g_coreType == AIC` 直接返回；`GET_TILING_DATA(tilingData, tiling)` 解包（配合 tiling.h 的 `REGISTER_TILING_DATA_CLASS`）；`TILING_KEY_IS(1)` 走推理模板（N=4 特化 + N=0 通用），`TILING_KEY_IS(0)` 走训练模板 `SinkhornGeneralized<DTYPE_X>`。

**Init：tiling 字段全量消费 + UB 账本**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h:L120-L178](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h#L120-L178)：`Init` 把 TilingData 的 16 个字段几乎全部搬进成员（L132-L146）；用 `GetBlockIdx()` 与 `needCoreNum-1` 比较决定本核用「头核三件套」还是「尾核三件套」（L149-L153）——与 u2-l4 的 `CutHBS` 同款思想；GM 基址按 `blockIdx * perCoreElements * n * n` 偏移（L158-L160）。`InitBuffer` 逐条对上 4.3.3 的 UB 公式：`xInQueue_/outputQueue_/transposeBuf_` 各 \(tn^2\)，`out_flag=1` 时 `normOutQueue_`（深度 2 双缓冲）再占 \(2tn^2\)，合计 5 个 \(tn^2\)；`maxBuf_/sumBuf_` 加 `sumOutQueue_`（深度 2）合计 4 个 \(tn\)；`tmpBuf_` 为 \((2n^2+1) \times 512\) 字节的 Transpose 共享空间（L166-L177）。

**Process：公式 → 代码的总装**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h:L180-L249](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h#L180-L249)：主循环按步骤 1-5 组织；`normOutBlockLen = n*n*totalLength`、`sumOutBlockLen = n*totalLength`（L186-L188）正是 GM 上 `[2*num_iters, n, n, B*S]` 布局里「相邻迭代槽」的跨度；迭代循环里 `rowOffset = 2*iter`、`colOffset = 2*iter+1`（L225-L226）与公式下标一字不差；`tOffset` 是本核本循环在 t 维上的全局起点（L198）。

**InitialSoftmax：softmax 的手写展开**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h:L300-L353](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h#L300-L353)：softmax 被拆成原始语——`Max` 逐列求最大值（L315-L321，先取前两行再滚动更新）、`Sub + Exp` 算 \(e^{x-\max}\)（L327-L330）、`Add` 累加分母（L333）、`Div` 归一（L341）。随后**先**把 softmax 结果拷进 `normOutQueue_`（L345-L349，即 `norm_out[0]`，不含 eps，与 aclnn 文档一致），**再** `Adds(xLocal, xLocal, eps_)` 给工作区加 ε（L351-L352）。这里遍历的外层是转置后的第 i 列块、内层是行 j，配合 `[n,n,t]` 布局每次向量操作长度都是 `tAlign_`。

**ColNormalize / RowNormalize：sum + eps + div 三步**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h:L355-L378](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h#L355-L378)：`ColNormalize`（奇数步，沿 \(\dim=-2\)）用一条 `AscendC::ReduceSum<float, Pattern::Reduce::RA, false>` 沿第一轴把 n 行压成 1 行（L362，`shape = {n, tAlign*n}`），`Adds(eps)` 后逐列块 `Div`（L364-L368），最后把 norm/sum 各拷一份进队列。
- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h:L380-L413](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h#L380-L413)：`RowNormalize`（偶数步，沿 \(\dim=-1\)）对每个行块 i 单独 `ReduceSum(shape={n, tAlign})`，同样 `Adds(eps)` 后逐列 `Div`。两者的差别只在归约的轴与循环层级。

**转置与拷出**：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h:L262-L298](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h#L262-L298)：`TransposeXIn`/`TransposeXOut` 用 `TransposeParamsExt` 描述 `[t, n*n] ↔ [n*n, t]` 的 NHWC/NCHW 互转，`tmpBuf_` 充当共享暂存，转置后 `PipeBarrier<PIPE_V>()` 保证向量队列同步。
- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h:L415-L451](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized.h#L415-L451)：`CopyOutNormOut`/`CopyOutSumOut` 是本 kernel 最精巧的搬运：UB 里 t 维 padded 到 `tAlign_`，GM 里同一 (迭代, i, j) 位置的相邻 t 段相距 `totalLength` 个元素，于是 `DataCopyExtParams` 以 `blockCount = n*n`（或 n）、`blockLen = currentLoopElements`、`srcStride` 跳过 UB padding、`dstStride = totalLength - currentLoopElements` 跨过 GM 上其它 t 段——一次 `DataCopyPad` 就把 `[n,n,t]` 的 UB 块散写到 `[n,n,B*S]` 的 GM 区，这正是 `norm_out` 转置布局的来历。

**推理路径对照**（不展开）：

- [manifold_constrained_hyper_connection_sinkhorn_enhance_generalized_infer.h:L24-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_kernel/manifold_constrained_hyper_connection_sinkhorn_enhance_generalized_infer.h#L24-L37)：每行 pad 到 `INFER_ALIGN_N=8`（一个 32B datablock），模板参数 N>0 时用 `if constexpr` 消除循环（N=4 特化由入口 switch 选择）；L96-L97 直接读 Host 预计算的 `reduceMask` 做 BlockReduce；L103-L108 的 6 个 buffer 槽对应 4.3.3 推理路径的 UB 公式。

#### 4.4.4 代码实践

**实践目标**：把「公式步骤 ↔ kernel 函数 ↔ 向量原语」三方对齐。

**操作步骤**：

1. 打开 `generalized.h`，从 `Process`（L180）出发，沿调用链依次跳到 `CopyInX → TransposeXIn → InitialSoftmax → ColNormalize → RowNormalize → TransposeXOut → CopyOut`。
2. 为下表填空（答案见第 5 节综合实践的对照表）：

| 公式步骤 | kernel 函数（行号） | 关键向量原语 |
| --- | --- | --- |
| softmax(x) | InitialSoftmax（L300） | Max / Sub / Exp / Add / Div |
| sum_out[奇]（列和+ε） | ColNormalize（L355） | ？ |
| sum_out[偶]（行和+ε） | RowNormalize（L380） | ？ |
| norm_out[k] = a/sum | ？ | ？ |

3. 修改观察：把 `InitialSoftmax` 里 L351 的 `Adds(xLocal, xLocal, eps_, ...)` 想象成删掉（不要真改源码），推演输出会发生什么——分母少了 ε，当某列完全下溢（列和为 0）时出现除零。

**需要观察的现象**：每个公式步骤都能在 kernel 中找到唯一对应函数；「+ε」出现在每个 `ReduceSum` 之后。

**预期结果**：填表结果为 ColNormalize=ReduceSum+Adds+Div（L362-L368）、RowNormalize 同款原语不同轴（L391-L401）、除法分别在各 Normalize 的 Div 与 InitialSoftmax 的 L341。（纯源码阅读，无需 NPU，可直接完成。）

#### 4.4.5 小练习与答案

**练习 1**：为什么训练路径选 `[n,n,t]` 转置布局，而推理路径不转置？
**答案**：训练路径每个 t 要跑 2×num_iters≈40 轮归一化，计算占比极高，把 t 维放连续方向让 `Div/Exp/ReduceSum` 每次满向量宽度执行、双缓冲 `normOutQueue_` 还能掩盖拷出延迟，两次 Transpose 的开销被摊薄；推理路径 `out_flag=0` 无中间量拷出、迭代轮数一样但访存模式不同，用 DataCopyPad 每行 pad 到 8 即可让 BlockReduce 直接工作，省掉转置更划算。

**练习 2**：`normOutQueue_` 为什么深度是 2（`DOUBULE_BUFFER`），而 `xInQueue_` 深度是 1？
**答案**：`normOutQueue_` 每轮迭代要搬出两份大块数据（norm+sum），深度 2 让「上一块的 DataCopyPad 搬出」与「当前块的归一化计算」重叠（双缓冲）；`xInQueue_` 每循环只搬入一次且转置后立刻消费，深度 1 足够——但这也是可优化点，深度 2 可进一步掩盖搬入延迟。

**练习 3**：kernel 入口为什么先判 `g_coreType == AIC` 就返回？
**答案**：本算子是纯向量算子（`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)`），没有 Cube 计算；混合调度下若该核被编排为 AIC 核则无事可做，直接返回避免执行非法向量指令。这与 u4-l3 AIC:AIV 混合核算子形成对照。

---

## 5. 综合实践

**任务**：用 numpy 从零实现完整 Sinkhorn 迭代，验证输出近似双随机，并产出「迭代步骤 ↔ kernel 实现」对照表。全程 CPU 可完成。

```python
# 示例代码：sinkhorn_numpy.py（本讲编写，非仓库代码）
import numpy as np

def sinkhorn_numpy(x, eps=1e-6, num_iters=20):
    """x: [T, N, N] float64；返回 output, norm_out[2K,T,N,N], sum_out[2K,T,N]"""
    T, N, _ = x.shape
    norm_out = np.zeros((2 * num_iters, T, N, N), dtype=x.dtype)
    sum_out = np.zeros((2 * num_iters, T, N), dtype=x.dtype)

    # 步骤0：norm_out[0] = softmax(x, dim=-1)  ← kernel InitialSoftmax（Max/Sub/Exp/Add/Div）
    x_max = x.max(axis=-1, keepdims=True)            # ← Max（L315-L321）
    e = np.exp(x - x_max)                            # ← Sub + Exp（L327-L330）
    prob = e / e.sum(axis=-1, keepdims=True)         # ← Add 累加 + Div（L333, L341）
    norm_out[0] = prob
    cur = prob + eps                                 # ← Adds（L351-L352）

    # 步骤1：sum_out[1] = 列和 + eps；norm_out[1] = cur / sum   ← ColNormalize（L355-L378）
    col_sum = cur.sum(axis=-2) + eps                 # ← ReduceSum(RA) + Adds（L362-L364）
    cur = cur / col_sum[:, :, None]                  # ← Div（L367）
    norm_out[1] = cur
    sum_out[1] = col_sum

    # 步骤2：行、列交替   ← RowNormalize（L380-L413）+ ColNormalize
    for it in range(1, num_iters):
        row_sum = cur.sum(axis=-1) + eps             # ← ReduceSum + Adds（L391-L395）
        cur = cur / row_sum[:, :, None]              # ← Div（L401）
        norm_out[2 * it] = cur
        sum_out[2 * it] = row_sum

        col_sum = cur.sum(axis=-2) + eps
        cur = cur / col_sum[:, :, None]
        norm_out[2 * it + 1] = cur
        sum_out[2 * it + 1] = col_sum

    return cur, norm_out, sum_out

if __name__ == "__main__":
    np.random.seed(0)
    T, N, K, eps = 64, 4, 20, 1e-8
    x = np.random.randn(T, N, N).astype(np.float64)
    out, norm_out, sum_out = sinkhorn_numpy(x, eps=eps, num_iters=K)

    row_sums = out.sum(axis=-1)   # 每行和
    col_sums = out.sum(axis=-2)   # 每列和
    print(f"row sums: max|1-r| = {np.abs(row_sums - 1).max():.3e}")
    print(f"col sums: max|1-c| = {np.abs(col_sums - 1).max():.3e}")
    print(f"norm_out slots = {norm_out.shape[0]} (expect {2 * K})")
```

**操作步骤与观察点**：

1. 运行脚本：应看到行和、列和与 1 的最大偏差随 `num_iters` 增大而减小（K=20 时通常已达 1e-5 量级以下）——这就是「近似双随机」的定量含义。
2. 把 `num_iters` 从 1 逐步调到 20，打印每步的 `max|1-行和|` 与 `max|1-列和|`，观察交替收敛：奇数步后列和精确为 1（除法刚做完），偶数步后行和精确为 1，另一方逐次逼近。
3. 设 `eps=1e-8` 与 `eps=1e-2` 各跑一次：ε 变大时收敛值偏离 1 的残差也变大（每步都多加了 ε）。
4. 构造含全 0 行的输入验证防除零：`x[0, 0, :] = -1e4`，softmax 后该行近似 one-hot，仍能安全完成。
5. （可选，需 NPU 环境）与仓库参考实现对拍：把 `_cpu_sinkhorn`（[test_npu_sinkhorn.py:L31-L74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/tests/st/test_npu_sinkhorn.py#L31-L74)）换成你的 numpy 版跑 ST，或按 docs 单算子示例（[npu_sinkhorn.md:L85-L105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/docs/npu_sinkhorn.md#L85-L105)）调用 `torch.ops.custom.npu_sinkhorn` 对比 NPU 与 numpy 输出（容差参考 ST 的 L0/L1 等级，见 u8-l3）。本机无 NPU，此步**待本地验证**。

**预期结果**：行和与列和同时趋于 1；`norm_out` 恰有 `2*num_iters` 槽、`sum_out[0]` 为全零占位；代码注释里的每个 numpy 操作都能对应到 `generalized.h` 的具体行号（对照表已在代码注释中给出）。

## 6. 本讲小结

- MHC（流形约束超连接）用 Sinkhorn 迭代把分支混合矩阵 \(H'_{res}\) 打磨成**近似双随机矩阵**（行和、列和均为 1），本算子是该家族 7 个算子中承前启后的归一化前向。
- 算法骨架：softmax 初始化（保证正性、行和为 1）→ 一次列归一化 → (行归一化 + 列归一化)×(num_iters−1)；偶数下标是行、奇数下标是列，`output = norm_out[2*num_iters−1]`。
- 训练路径（`out_flag=1`，tilingKey=0）把每次归一化的结果 `norm_out[k]` 与分母 `sum_out[k]` 全部落盘，layout 为 `[2K, N, N, t]` / `[2K, N, t]`——t 维连续，专供反向算子 `ai_infra_sinkhorn_grad` 复用，是典型的「空间换反向时间」。
- 两级 tiling：`_tiling_base.cpp` 是十几行的薄入口（`IMPL_OP_OPTILING` → `DoTilingImpl` 责任链），`_tiling.cpp` 的 `SinkhornTilingBase` 以优先级 2000 注册，承载七步流程全部逻辑——单实现下责任链退化为单节点，但保留了扩展为多模板的能力。
- 切分要点：B×S 折叠成 t 维（方阵间零依赖，天然可均分）；核间按 `tAlign` 预算（UB 公式 \(5tn^2+4tn\) 个 float + Transpose 共享区 < 190KB）确定每循环装载量，核内头核/尾核三件套；推理路径（tilingKey=1）另走 DataCopyPad pad-8 + BlockReduce + `reduceMask` 的免转置实现。
- kernel 层公式↔原语映射：softmax=Max/Sub/Exp/Add/Div，行/列归一化=ReduceSum+Adds(ε)+Div；`CopyOutNormOut` 用 blockCount/srcStride/dstStride 一次搬运完成 UB `[n,n,t]` → GM `[n,n,B*S]` 的散写。

## 7. 下一步学习建议

- **u5-l2（SinkhornGrad 反向）**：本讲埋了所有伏笔——反向为什么必须复用 `norm_out/sum_out`、链式法则如何在多轮迭代间累加，下一讲逐条兑现；并顺带学习该算子的 UT fixture 设计。
- **u5-l3 / u5-l4（MHC pre/post 系列）**：看 MHC 家族另外两条链如何与本算子组成完整训练步，并见识带完整 op_api（aclnn 两段式）的 MHC 算子全链路。
- **源码延伸阅读**：对照 `generalized_infer.h` 完整走一遍推理路径的 `if constexpr` 特化与 BlockReduce，体会「同一算子、两条 tilingKey 路径」的性能取舍；再回看 u3-l3 的 `tiling_templates_registry.h`，思考若给 Sinkhorn 增加第二种切分模板需要改哪几处（提示：新增子类 + `REGISTER_OPS_TILING_TEMPLATE` 一个更小优先级即可）。
