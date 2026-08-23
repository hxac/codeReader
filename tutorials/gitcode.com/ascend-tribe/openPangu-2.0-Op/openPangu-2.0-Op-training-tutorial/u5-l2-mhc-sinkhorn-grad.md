# SinkhornGrad 反向：保存中间量的复用设计

## 1. 本讲目标

上一讲（u5-l1）我们读懂了 MHC 家族的 SinkhornEnhance 前向算子：它做双随机矩阵归一化，并在训练路径下把每轮迭代的 `norm_out`（归一化后的矩阵）与 `sum_out`（归一化分母）共 `2×num_iters` 份落盘保存。本讲沿着这条线索，精读它的「另一半」——反向算子 `ai_infra_sinkhorn_grad`，搞清楚三件事：

1. **数学上**：反向为什么只需要「沿前向同一轴做内积、减去、再除以分母」这三步？`norm_out`/`sum_out` 各自在链式法则中扮演什么角色？
2. **工程上**：两级 tiling（`tiling_base.cpp` 薄入口 + `tiling.cpp` 切分实现）如何为这个「多轮迭代、逐 token 独立」的算子做核间/核内切分；Kernel 为什么先把数据转置成 `[n, n, t]` 再算。
3. **测试上**：`tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h` 这类「fixture + 错误注入枚举」的 tiling UT 是怎么组织的，如何给它新增用例。

学完本讲，你应当能独立推导 Sinkhorn 反向公式、说清 12 个 TilingData 字段的来龙去脉，并能为这个算子补一个自己的 tiling UT 用例。

## 2. 前置知识

本讲默认你已读过 u5-l1（Sinkhorn 前向）与 u3-l3（tiling_base 框架）。这里补齐几个数学与测试概念：

- **链式法则（chain rule）**：复合函数 \( y = f(g(x)) \) 的梯度满足 \( \frac{\partial L}{\partial x} = \frac{\partial g}{\partial x} \cdot \frac{\partial L}{\partial g} \)。反向传播就是从输出端出发，把梯度一层层「乘」回输入端。
- **除法算子求导**：对 \( y = c/s \)（s 也依赖 c），有 \( \frac{\partial y_i}{\partial c_j} = \frac{\delta_{ij}}{s} - \frac{c_i}{s^2} \)。这个二阶小矩阵正是本讲公式的全部来源，下面会完整推导。
- **softmax 反向**：对 \( p = \mathrm{softmax}(x) \)，经典结论 \( \frac{\partial L}{\partial x_j} = p_j \left(g_j - \langle g, p \rangle\right) \)，即「减去内积投影后乘回 p」。它与除法反向的唯一区别是最后一步**乘**而不是**除**。
- **空间换时间**：前向多写 \(2 \times num\_iters\) 份中间量（显存开销），换反向不必重算整个前向迭代链。这是训练算子常见的设计取舍，FA 反向保存 `softmax_max/softmax_sum`（u4-l4）是同一个思路。
- **fixture（测试夹具）**：gtest 中提供公共测试上下文的类。本算子的 fixture 把「构造 tiling 输入上下文」与「注入非法输入」都封装成可复用的静态方法，测试文件里一行一个用例。
- **伪逆视角**：每一步「除以行和/列和」的归一化，其反向就是「除以同一个分母、再减去一个投影项」——可以把反向算子理解为沿时间轴倒序执行的一串「伪逆步骤」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md` | 反向算子文档：计算公式、接口规格、约束（本讲的数学基准） |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_def.cpp` | 原型注册：3 输入 1 输出，全 float32，无属性 |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling_base.cpp` | tiling 薄入口：`IMPL_OP_OPTILING` 注册 + 转发到模板注册表 |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.h` | TilingData 结构（12 字段）与 CompileInfo 定义 |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp` | tiling 主体：校验、UB 预算、核间/核内切分 |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad.cpp` | kernel 入口：AIV-only，解包 TilingData，Init+Process |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h` | kernel 主体：转置、三段反向循环、strided 拷贝 |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h` | UT fixture：上下文工厂 + 错误注入枚举 |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_tiling.cpp` | tiling UT 用例集（8 正常 + 20 异常） |
| `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/st/test_sinkhorn_grad.py` | ST 精度测试：CPU golden 与 NPU 结果比对 |
| `ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.{h,cpp}` | UT 公共执行器：断言 status / tilingKey / tilingData / workspace |

另有 `docs/aclnnAiInfraSinkhornGrad.md`（aclnn 接口文档，本讲不展开）。注意：本算子目录下**没有 op_api 源码目录**，`torch.ops.custom.npu_sinkhorn_grad` 的 aclnn 符号由已安装的算子包在运行期提供（回顾 u1-l2 的结论）。

## 4. 核心概念与源码讲解

### 4.1 反向数学原理：从除法求导到「减投影、再除分母」

#### 4.1.1 概念说明

前向的每一步归一化（回顾 u5-l1）都是：对当前矩阵 \( C \)，沿某个轴求和加 ε 得到分母 \( s \)，然后逐元素相除：

\[ s = \sum_{\text{axis}} c + \varepsilon, \qquad y = \frac{c}{s} \]

前向在每一步之后保存两个东西：

- `norm_out[k]`：该步**除完之后**的矩阵（即 \( y = c/s \) 本身）；
- `sum_out[k]`：该步的分母 \( s \)（含 ε）。

反向要回答的问题是：已知输出端梯度 \( g = \partial L / \partial y \)，如何一步还原 \( \partial L / \partial c \)，而不重算前向？关键观察是：这一步的雅可比矩阵只依赖 \( s \) 和 \( y \)——**恰好就是保存的两个张量**。这就是「保存中间量的复用设计」的含义。

#### 4.1.2 核心流程

对单步 \( y_i = c_i / s \)（\( s = \sum_j c_j + \varepsilon \) 沿 axis 求和），求偏导：

\[ \frac{\partial y_i}{\partial c_j} = \frac{\delta_{ij}}{s} - \frac{c_i}{s^2} = \frac{1}{s}\left(\delta_{ij} - \frac{c_i}{s}\right) \]

代入链式法则（\( c_i / s = y_i = norm\_out \)）：

\[ \frac{\partial L}{\partial c_j} = \sum_i g_i \frac{\partial y_i}{\partial c_j} = \frac{1}{s}\left(g_j - \sum_i g_i \cdot norm\_out_i\right) = \frac{g_j - \langle g,\ norm\_out \rangle_{\text{axis}}}{sum\_out} \]

结论（三步口诀）：**沿前向同一轴做内积 → 减去 → 除以 sum_out**。内积 \( \langle g, norm\_out \rangle \) 沿哪个轴，取决于前向的求和轴。

对第一步 softmax（\( p = \mathrm{softmax}(x) \)），同理得：

\[ \frac{\partial L}{\partial x_j} = p_j \left(g_j - \langle g,\ p \rangle\right) = norm\_out_0 \odot \left(g - \langle g,\ norm\_out_0 \rangle\right) \]

注意最后是**乘** norm_out 而不是除 sum_out——softmax 没有保存分母（ST 脚本里 `sum_list[0] = None`），也不需要。

把前向保存顺序与反向消费顺序列成索引表（**本讲最重要的表格**）：

| 索引 k | 前向阶段 | 前向求和轴（[T,n,n] 视角） | 反向操作 | kernel 对应函数 |
|---|---|---|---|---|
| 0 | softmax 输出 p | dim=-1（行） | \((g - \langle g,p \rangle_{-1}) \times p\) | `softmaxGrad` |
| 1 | 初始列归一化 | dim=-2（列） | \((g - \langle g,n_1 \rangle_{-2}) / s_1\) | `colNormGrad` |
| 2i（i≥1） | 第 i 轮行归一化 | dim=-1 | \((g - \langle g,n_{2i} \rangle_{-1}) / s_{2i}\) | `rowNormGrad` |
| 2i+1（i≥1） | 第 i 轮列归一化 | dim=-2 | \((g - \langle g,n_{2i+1} \rangle_{-2}) / s_{2i+1}\) | `colNormGrad` |

反向沿 k = 2·num_iters−1 递减到 0 依次执行；总共恰好 2×num_iters 步（num_iters 轮的行列对 + softmax 步 + 初始列步，与前向保存量严格相等）。另一个细节：前向里有 `curr = prob + eps`（softmax 后加常数），常数平移的导数为 0，所以反向完全忽略它；ε 只通过各步分母 s 进入梯度。

#### 4.1.3 源码精读

文档中的计算公式就是上表的形式化陈述——前 num_iters−1 轮按 i 递减先做 `dot_prod_{2i+1}`（dim=-2 求和、除 `sum_out_{2i+1}`）再做 `dot_prod_{2i}`（dim=-1、除 `sum_out_{2i}`），最后一轮处理索引 1 与索引 0，且第 0 步是「乘 norm_out_0」：

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md:L22-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md#L22-L49)：完整反向公式。注意最后一行 `\mathbf{grad}_{\text{input}} \gets (\mathbf{grad}_{\text{curr}} - \mathbf{dot\_prod}_{0}) \cdot \mathbf{norm\_out}_{0}` 是乘法，与其余步的除法形成对照。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md:L52-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md#L52-L56)：函数原型 `torch.ops.custom.npu_sinkhorn_grad(grad_output, norm_out, sum_out) -> (Tensor)`——**只有三个输入**，中间量作为显式输入传入，而不是像 FA 那样塞进输出列表。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md:L94-L112](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md#L94-L112)：`norm_out` 形状 `[2*num_iters, n, n, B, S]` 或 `[2*num_iters, n, n, T]`、`sum_out` 形状 `[2*num_iters, n, B, S]` 或 `[2*num_iters, n, T]`，与 u5-l1 前向落盘的布局（t 折叠到末维）一致。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md:L174-L183](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/docs/npu_sinkhorn_grad.md#L174-L183)：规格约束 `num_iters ∈ [1,100]`、`n ∈ {4,6,8}`。注意：`n` 的取值约束**没有**在 tiling 代码里硬校验（见 4.2.3），这是「文档规格宽于代码校验」的实例。

原型注册侧与 u5-l1 前向对称，3 输入 1 输出全部 float32/ND：

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_def.cpp:L24-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_def.cpp#L24-L46)：`grad_output`/`norm_out`/`sum_out` 三个必选输入 + `grad_input` 输出，全部带 `AutoContiguous()`。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_def.cpp:L55-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_def.cpp#L55-L60)：`AddConfig("ascend910b")` 与 `AddConfig("ascend910_93")` 双芯片注册（A2/A3），`OP_ADD` 入注册表。

ST 测试里的 CPU golden 实现就是 4.1.2 索引表的直译，值得对照阅读：

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/st/test_sinkhorn_grad.py:L74-L105](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/st/test_sinkhorn_grad.py#L74-L105)：`sinkhorn_grad_with_saved_tensors`——先 `permute` 到 [n,n,t]，`for i in range(num_iters-1, 0, -1)` 依次 dim=0/dim=1 求和，最后索引 1 除法、索引 0 乘法，再 permute 回 [t,n,n]。它与 kernel 的循环结构逐行对应（见 4.3.2）。

#### 4.1.4 代码实践

单步公式数值验证（CPU、无 NPU 依赖，30 秒可完成）。

1. **实践目标**：用 `torch.autograd` 验证「减投影、除分母」公式对**单步**归一化是精确的（不是近似）。
2. **操作步骤**：运行下面的示例代码（标注为「示例代码」，非仓库文件）：

```python
import torch
torch.manual_seed(0)

T, n, eps = 5, 4, 1e-6
c = torch.randn(T, n, n, dtype=torch.float64, requires_grad=True)
# 一步"列归一化"：沿 dim=-2 求和做分母
s = c.sum(dim=-2, keepdim=True) + eps          # [T,1,n]
y = c / s                                      # norm_out = y, sum_out = s
g = torch.randn(T, n, n, dtype=torch.float64)  # 上游梯度
(y * g).sum().backward()                       # loss = <y, g>，y 依赖 c，梯度回流到 c
grad_autograd = c.grad.clone()

# 手工公式：(g - <g, norm_out>_{dim=-2}) / sum_out
dot = (g * y.detach()).sum(dim=-2, keepdim=True)
grad_manual = (g - dot) / s.detach()

print((grad_autograd - grad_manual).abs().max())
```

3. **需要观察的现象**：打印的最大误差。
4. **预期结果**：float64 下误差在 1e-15 量级（机器精度级别），说明公式精确成立。待本地验证具体量级。
5. 把 `dim=-2` 换成 `dim=-1` 再跑一次，验证行归一化同理成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么第 0 步（softmax）的反向是乘 `norm_out[0]`，而其他步是除 `sum_out[k]`？

**答案**：softmax 的雅可比是 \( \partial p_i/\partial x_j = p_i(\delta_{ij}-p_j) \)，展开后天然得到 \( p \odot (g - \langle g,p \rangle) \)，没有「分母」参与；且前向根本没保存 softmax 的分母（`sum_list[0] = None`）。除法归一化的雅可比则带有 \(1/s\) 因子，所以必须除以保存的 `sum_out[k]`。

**练习 2**：前向的 `curr = prob + eps`（softmax 后加 ε）会不会让反向公式不精确？

**答案**：不会。对输入加常数是仿射变换中平移项，导数为 0；ε 只通过后续各步分母 \( s = \sum c + \varepsilon \) 进入梯度，而公式里的 `sum_out` 已包含它。

**练习 3**：当 `num_iters = 1` 时，反向要经过哪几步？kernel 的主循环还执行吗？

**答案**：只经过索引 1（初始列归一化，除法）和索引 0（softmax，乘法）。kernel 中 `for (int j = numIters_ - 1; j > 0; --j)` 在 num_iters=1 时不进入循环体，直接执行「最后一次列归一化 + softmaxGrad」两段（见 4.3.2）。

### 4.2 两级 Tiling：薄入口 tiling_base 与 12 字段切分契约

#### 4.2.1 概念说明

回顾 u3-l3：`TilingBase` 框架用模板方法把一次 tiling 固定为七步（取 shape/attr → 平台信息 → `IsCapable` → `DoOpTiling` → `DoLibApiTiling` → `GetWorkspaceSize`/`GetTilingKey` → `PostTiling`），并支持「一算子多模板、按优先级责任链调度」。SinkhornGrad 采用的是其中最朴素的形态，文件组织上分成两级：

- `ai_infra_sinkhorn_grad_tiling_base.cpp`：**薄入口**。只做两件事——把 CANN 的 tiling 回调转发给模板注册表 `TilingRegistry::DoTilingImpl`，以及用 `IMPL_OP_OPTILING` 把算子名与回调绑定。它的名字里虽然有 "base"，但**不含任何切分逻辑**。
- `ai_infra_sinkhorn_grad_tiling.cpp`：**真正的实现**。定义 `AiInfraSinkhornGradTilingBase`（继承 `TilingBase`，命名有点绕——这是实现类不是入口），以优先级 2000 注册为唯一模板。

这个算子没有属性（Attr），`num_iters` 不是标量参数，而是**编码在 `norm_out` 的第 0 维长度里**（dim0 = 2×num_iters），tiling 从 shape 反推。这是它与前面见过的大多数算子（FA 有 14 个属性）截然不同的接口风格。

#### 4.2.2 核心流程

七步流程在本算子的落点：

```text
TilingForAiInfraSinkhornGrad(context)          [tiling_base.cpp 薄入口]
  └─> TilingRegistry::DoTilingImpl(context)    [责任链：仅 1 个模板，优先级 2000]
        └─> AiInfraSinkhornGradTilingBase::DoTiling 流程
              ① GetPlatformInfo   取 AIV 核数、UB 大小
              ② GetShapeAttrsInfo 取 3 输入 1 输出的 shape/dtype（全 fp32 校验）
              ③ DoOpTiling
                   ├─ CheckInputShape  维度数判定 TNN(3维)/BSNN(4维) → 折叠为 totalLength
                   │                   dim0 奇偶性 → numIters ∈ [1,100]
                   │                   三个输入的 n/T(B,S) 一致性
                   ├─ CheckOutputShape grad_input 与 grad_output 同形
                   └─ SplitCores       UB 预算 → maxTokensPerLoop（核内一次处理的 token 数）
                                   核间均分 → perCore/lastCore 两组四元组
              ④ DoLibApiTiling    空实现（无高阶 API）
              ⑤ GetTilingKey      恒返回 0（唯一模板）
              ⑥ GetWorkspaceSize  固定 16MB 预留
              ⑦ PostTiling        SetBlockDim(needCoreNum) + 序列化 TilingData
```

SplitCores 的两级切分（伪代码）：

```text
maxTokensPerLoop = (190KB / (4B × (7n² + 3n))) 向下对齐到 8 的倍数
若 < 128：改用「预留转置栈缓冲」的备用公式，分母换成 (5n² + 2n)

perCoreElements = ceil(totalLength / AIV核数)          # 核间
若 perCoreElements < 32 且 totalLength >= 32：抬到 32   # 限核，避免尾核浪费
perCoreLoops = ceil(perCoreElements / maxTokensPerLoop) # 核内分loop
若 perCoreLoops == 1：perCoreElements 向上对齐到 8
needCoreNum = ceil(totalLength / perCoreElements)
lastCoreElements = totalLength − perCoreElements × (needCoreNum−1)  # 末核单独一套参数
```

UB 预算公式 \( (7n^2 + 3n) \times 4\text{B} \) 的来源见 4.3.3：gradIn×1 + normIn×2 + gradOut×1 + gradTranspose×1 + calcTmp×2 共 7 份 `t·n·n`，加上 sumIn×2 份 `t·n`。

#### 4.2.3 源码精读

**薄入口（tiling_base.cpp 全部有效代码只有十几行）：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling_base.cpp:L24-L33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling_base.cpp#L24-L33)：`TilingForAiInfraSinkhornGrad` 一行转发给 `TilingRegistry::GetInstance().DoTilingImpl(context)`；`TilingPrepareForAiInfraSinkhornGrad` 是编译期钩子，本算子无事可做直接返回 SUCCESS。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling_base.cpp:L35-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling_base.cpp#L35-L37)：`IMPL_OP_OPTILING(AiInfraSinkhornGrad).Tiling(...).TilingParse<AiInfraSinkhornGradCompileInfo>(...)`——算子名对齐四层的锚点（回顾 u2-l2）。

**TilingData 契约（12 个字段，kernel 全部消费）：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.h:L24-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.h#L24-L38)：`totalLength`（T 或 B×S）、`n`、`numIters` 三个语义字段 + `needCoreNum` + 「普通核四元组 / 末核四元组」共 8 个切分字段；`REGISTER_TILING_DATA_CLASS` 把该结构与算子名绑定，kernel 侧的 `GET_TILING_DATA` 据此解包。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.h:L40-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.h#L40-L43)：`AiInfraSinkhornGradCompileInfo { aicNum, aivNum }` 供编译期 TilingParse 缓存。

**shape 解析与校验（tiling.cpp）：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L281-L302](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L281-L302)：`isTShape_ = (grad_output 维度数 == 3)`——用**输入维度数**判定 TNN/BSNN 两种 layout；BSNN 时 `totalLength_ = bSize_ × sSize_`。这一步是「BSNN 折叠为 TNN」的关键：连续内存下 (B,S,n,n) ≡ (B·S,n,n)，kernel 无需感知 B/S。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L303-L315](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L303-L315)：`normOutDim0 & 1` 奇偶校验后 `numIters_ = normOutDim0 / 2`，再校验 `numIters_ ∈ [1,100]`。**num_iters 从 shape 反推而非 Attr 读取**。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L219-L267](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L219-L267)：`CheckNormOutShape`/`CheckSumOutShape` 按 TNN/BSNN 分支核对 `[2I,n,n,T/B,S]` 与 `[2I,n,T/B,S]`。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L164-L188](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L164-L188)：三个输入逐一校验 dtype 必须是 `DT_FLOAT`（本算子只有 fp32 一种精度，与前向 sinkhorn 支持多 dtype 不同）。

**切分与写回：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L373-L391](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L373-L391)：UB 预算注释（`7tnn + 3nt`）与 `maxTokensPerLoop` 主公式、`< 128` 时的备用公式、`< 8` 报错兜底。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L393-L416](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L393-L416)：核间均分、`MIN_PER_CORE_ELEMENTS = 32` 限核策略、普通核/末核各自的三元组（Loops/PerLoop/LastLoop）。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L418-L433](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L418-L433)：12 个字段中 8 个切分字段在这里 `set_*` 写入；末尾的 `OP_LOGI` 把全部切分结果打成一条日志——这是手工验算的最佳对照物。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L469-L483](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L469-L483)：`PostTiling` 里 `SetBlockDim(needCoreNum)`（blockDim=实际需要的核数，而非物理核数）、16MB workspace 写入、TilingData 序列化到 RawTilingData；`GetTilingKey` 恒返回 `TILING_KEY_GENERALIZED = 0`。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp:L491](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_tiling.cpp#L491)：`REGISTER_OPS_TILING_TEMPLATE(AiInfraSinkhornGrad, AiInfraSinkhornGradTilingBase, 2000)`——唯一模板、优先级 2000，与 u5-l1 前向的 SinkhornTilingBase 同优先级段。

#### 4.2.4 代码实践

手工验算一次切分，再用 UT 日志对照。

1. **实践目标**：对 `case_normal_typical_network_tnn`（T=8192, n=4, numIters=20，fixture 里 compileInfo 为 48 核 AIV）手算全部切分字段。
2. **操作步骤**：
   - 计算 `7n²+3n = 7×16+12 = 124`，每 token 占 `4B × 124 = 496B`；
   - `maxTokensPerLoop = ⌊190×1024 / 496⌋ = 392`，已是 8 的倍数；
   - `perCoreElements = ⌈8192/48⌉ = 171`（≥32，不触发限核）；`perCoreLoops = ⌈171/392⌉ = 1`；因 loops==1，向上对齐 `perCoreElements = 176`；
   - `needCoreNum = ⌈8192/176⌉ = 47`，`lastCoreElements = 8192 − 176×46 = 96`。
3. **需要观察的现象**：运行 UT 后（命令见综合实践），在输出日志里找 `Core splitting: needCoreNum=...` 那一行 `OP_LOGI`。
4. **预期结果**：日志应为 `needCoreNum=47, perCoreElements=176, lastCoreElements=96, maxTokensPerLoop=392, perCoreLoops=1, perCorePerLoopElements=176, perCoreLastLoopElements=176, lastCoreLoops=1, lastCorePerLoopElements=96, lastCoreLastLoopElements=96`（待本地验证）。
5. 若与你手算不符，回到 L373-L435 逐行核对是哪个分支算错。

#### 4.2.5 小练习与答案

**练习 1**：TilingData 共 12 个字段，kernel 侧实际消费几个？

**答案**：全部 12 个。`Init` 读取 12 个字段中的全部（3 个语义字段 + 8 个切分字段用于确定本核身份，见 4.3.3 L132-L151），`Process` 再使用 loops/elements 字段驱动循环；`needCoreNum` 还同时作为 blockDim 下发。对比 aggregate_hidden（u2-l4）也是「字段全被消费」——跨侧契约失配会静默出错，所以 tiling 多写字段无害、kernel 不读才有害。

**练习 2**：`maxTokensPerLoop` 为什么要做 `& ~0x7`（向下取 8 对齐）？

**答案**：float32 × 8 = 32 字节，恰是 Ascend C 数据搬运的最小块（BLOCK_BYTES）。对齐后 UB 各 buffer 的尺寸与 `DataCopyPad` 的目的侧 stride（按 32B 块计）都能整块对齐，避免非整块访问；kernel 侧的 `Align()`/`AlignBytes()`（generalized.h L43-L54）与之配套。

**练习 3**：文档说 `n ∈ {4,6,8}`，tiling 代码里能找到这条校验吗？UT 用例支持你的结论吗？

**答案**：找不到。`CheckGradOutputShape` 只校验最后两维相等，`CheckNormOutShape`/`CheckSumOutShape` 只校验与 grad_output 一致，没有任何 `n` 取值白名单；UT 的 `case_normal_small_n` 甚至用 n=1 跑正常路径，ST 随机用例会在 1~12 间随机取 n。所以 `n ∈ {4,6,8}` 是文档层面的「规格承诺」，代码是 generalized（泛化）实现，只要 UB 装得下（`maxTokensPerLoop ≥ 8`）都能跑。

### 4.3 Kernel：转置到 [n, n, t] 的三段反向循环

#### 4.3.1 概念说明

kernel 文件组织与 aggregate_hidden（u2-l4）一样是「入口 .cpp + 实现 .h」两件套。三个值得先建立的概念：

1. **AIV-only 纯向量算子**：入口声明 `KERNEL_TYPE_AIV_ONLY`，`g_coreType == AIC` 的核直接返回。整个反向只有向量指令（Mul/ReduceSum/Sub/Div/Transpose），没有矩阵乘，与 FA 的 AIC:AIV 混合核形成对比。
2. **先转置再计算**：`grad_output` 是 [t,n,n]（t 在前），而 `norm_out`/`sum_out` 是 [·,n,n,t]（t 在末轴）。kernel 先把 grad 转置成 [n,n,t]，让「同一归一化步、一批 token」的数据在内存上连续，`ReduceSum` 沿最前/次前维一次满载完成；算完再转置回去写出。这与 u5-l1 前向「先转置为 [n,n,t] 使向量指令满载」是同一个手法。
3. **倒序三段循环**：主循环 `j` 从 `numIters−1` 递减到 1，每轮做「列（2j+1）+ 行（2j）」两次归一化反向；循环结束后补「索引 1 的列归一化」与「索引 0 的 softmax 反向」两段。三段分别对应 `colNormGrad` / `rowNormGrad` / `softmaxGrad` 三个函数，前两者结构完全相同只差归约轴，后者把最后的 Div 换成 Mul。

#### 4.3.2 核心流程

单个核（blockIdx = b）的处理流程：

```text
Init:  本核身份四选一（b < needCoreNum-1 ? 普通核四元组 : 末核四元组）
       按 perCorePerLoopElements 分配 6 块 UB（tAlign 对齐）
       gradOutputGm_/gradInputGm_ 只绑定本核的 token 段（偏移 b×perCoreElements×n²）

for i in 0 .. 本核loops-1:                     # 核内分 loop
    currentLoopElements = 本 loop 的 token 数（末 loop 较小）
    CopyInX        拷入 [t,n,n] 的 grad（整段连续）
    TransposeXIn   [t,n,n] ──NHWC2NCHW──> [n,n,t]，结果常驻 gradTransposeBuf

    for j = numIters-1 .. 1:                   # 倒序前 num_iters-1 轮
        k = 2j+1（列）: CopyInNormOut/CopyInSumOut(k) → colNormGrad
        k = 2j  （行）: CopyInNormOut/CopyInSumOut(k) → rowNormGrad

    k = 1（初始列）: CopyInNormOut/CopyInSumOut(1) → colNormGrad
    k = 0（softmax）: CopyInNormOut(0)          → softmaxGrad（乘法收尾）

    TransposeXOut  [n,n,t] ──NCHW2NHWC──> [t,n,n]
    CopyOut        只写 currentLoopElements×n² 个有效元素回 grad_input
```

`colNormGrad` 单步的四条向量指令（rowNormGrad 同构，仅归约轴不同）：

```text
mul    = gradLocal ⊙ normLocal                    # 逐元素
dot    = ReduceSum(mul, 沿第一维 n)               # 内积 <g, norm_out>
sub    = gradLocal − dot                          # 减投影（广播）
gradLocal = sub / sumLocal                        # 除以分母（广播）
```

`softmaxGrad` 的差别仅在最后一步 `gradLocal = sub ⊙ normLocal`（乘回 p）。

`norm_out` 的拷入是**尾轴分段拷贝**：GM 上一个归一化平面是 `[n², T]` 的二维表，本核只要其中 `[n², t]` 的一块，于是用 `blockCount = n²` 行、每行 `blockLen = t×4B`、行间隔 `srcStride = (T−t)×4B` 的分块 DataCopyPad 一次搬完。

#### 4.3.3 源码精读

**入口（ai_infra_sinkhorn_grad.cpp，本手册见过的最简入口）：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad.cpp:L23-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad.cpp#L23-L37)：`extern "C" __global__ __aicore__` 入口。注意**没有** `TILING_KEY_IS` 分支——单一模板、tilingKey 恒 0，所以不需要按 key 选实现；`GET_TILING_DATA(tilingData, tiling)` 把 GM 字节流解包为 `AiInfraSinkhornGradTilingData`（比 aggregate_hidden 的 `GET_TILING_DATA_WITH_STRUCT` 更简，因为结构已由 `REGISTER_TILING_DATA_CLASS` 绑定）。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad.cpp:L26-L43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad.cpp#L26-L43)：`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` + AIC 核早退；workspace 为空指针时早退（16MB workspace 属预留，`Init` 实际未消费它）。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad.cpp:L45-L49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad.cpp#L45-L49)：栈上 `TPipe` + `AiInfraSinkhornGradGeneralized op`，`Init` → `Process` → `Destroy` 三段式。

**Init：本核身份与 UB 划分：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L132-L151](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L132-L151)：读出 TilingData 全部字段，`blockIdx_ = GetBlockIdx()`，再用三个三元运算符在「普通核四元组 / 末核四元组」之间选择——这就是 Host 侧 blockDim 乘法的 Device 侧逆过程（与 u2-l4 CutHBS 的取模反解同思想，这里更简单：只有末核特殊）。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L153-L166](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L153-L166)：六块 UB 的分配。队列深度：gradInQueue×1（tnn）、normInQueue×2（2tnn）、sumInQueue×2（2tn）、gradOutQueue×1（tnn）、gradTransposeBuf×1（tnn）、calcTmpBuf（2tnn+tn 或转置栈缓冲取大）。合计恰为 tiling 注释的 **7tnn + 3tn**。注意所有核（含末核）都按 `perCorePerLoopElements_` 分配同一尺寸——末核 loop 小，UB 里多出的部分是 padding，不会被 CopyOut 写出。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L168-L172](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L168-L172)：`gradOutputGm_`/`gradInputGm_` 加上本核偏移 `blockIdx_ × perCoreElements_ × n²` 后绑定——本核从此只看得见自己的 token 段；`normOutGm_`/`sumOutGm_` 绑全量（每个归一化平面都要按 tOffset 切段访问）。

**Process：倒序三段循环：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L181-L194](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L181-L194)：`normOutBlockLen = n²×totalLength`、`sumOutBlockLen = n×totalLength` 是 GM 上「一份中间量」的跨度；`tOffset = blockIdx×perCoreElements + i×perLoopElements` 是本 loop 在 T 轴上的起点，`k×BlockLen + tOffset` 即第 k 份中间量中本核段的偏移。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L200-L229](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L200-L229)：`for (j = numIters_ - 1; j > 0; --j)` 内先列（2j+1 → colNormGrad）后行（2j → rowNormGrad）；循环后补索引 1 的 colNormGrad 与索引 0 的 softmaxGrad（注意索引 0 **不拷 sum_out**——softmax 没有分母）。与 4.1.2 索引表、ST golden 逐行对应。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L231-L234](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L231-L234)：反向算完转置回 [t,n,n] 并写出。

**三个归一化反向函数：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L326-L357](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L326-L357)：`colNormGrad`——`Mul` 后 `ReduceSum<Pattern::Reduce::RA>` 以 `shape={n, tAlign×n}` **沿第一维**归约得到 `dot_prod [n×t]`（即 [n,n,t] 视角沿 dim0 求和 = [T,n,n] 视角沿 dim=-2 求和），再逐行 `Sub`+`Div`。`calcTmpBuf_` 用 `GetWithOffset` 一块切成三段（mulResult / dot_prod / subResult），是「一个 TBuf 多用」的典型写法。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L359-L397](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L359-L397)：`rowNormGrad`——归约轴换到 dim1：外层 `for i in n` 每次 `ReduceSum` 以 `shape={n, tAlign}` 归约一段，内层逐 (i,j) 做 `Sub`+`Div`。与 colNormGrad 只差归约方向与广播位置。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L399-L437](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L399-L437)：`softmaxGrad`——归约同 rowNormGrad，但收尾是 `Mul(gradLocal, subResultTensor, normLocal)`（L434，乘回 p 而非除分母），且不 DeQue 任何 sum 队列。

**转置与分段拷贝：**

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L248-L268](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L248-L268)：`TransposeXIn` 用高阶 API `Transpose`（`TRANSPOSE_NHWC2NCHW`，cSize=n²、wSize=tAlign）把 [t,n,n] 转成 [n,n,t]，`calcTmpBuf_` 充当该 API 需要的栈缓冲——这正是 tiling 里 `TRANSPOSE_BUFFER_SIZE=128` 备用公式预留的那块内存。`TransposeXOut`（L270-L290）反向同理用 `TRANSPOSE_NCHW2NHWC`。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L295-L307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L295-L307)：`CopyInNormOut` 的分块拷贝参数：`blockCount=n²`、`blockLen=t×4B`、`srcStride=(T−t)×4B`（跳过其他核的段）、`dstStride=(tAlign−t)×4B/32`（目的侧按 32B 块计的补齐）。`CopyInSumOut`（L312-L324）同构，只是行数换成 n。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h:L237-L246](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_kernel/ai_infra_sinkhorn_grad_generalized.h#L237-L246)：`CopyInX`——grad 段在本核内连续，一条 `DataCopyPad` 整段搬入；`CopyOut`（L439-L446）对称地只搬 `currentLoopElements×n²` 个有效元素，padding 不落 GM。

#### 4.3.4 代码实践

源码阅读型实践：填一张「UB 预算对照表」。

1. **实践目标**：验证 tiling 的 UB 公式与 kernel 实际分配严格一致，理解每块缓冲的消费者。
2. **操作步骤**：对照 L153-L166 的六个 `InitBuffer` 调用，填写下表（以 n=4、tAlign=176 为例，单位：元素个数）：

| 缓冲 | 深度 × 单份大小 | 总元素数 | 消费者 |
|---|---|---|---|
| gradInQueue | 1 × t·n² | 2816 | CopyInX / TransposeXIn |
| normInQueue | 2 × t·n² | 5632 | CopyInNormOut / 三个 Grad 函数 DeQue |
| sumInQueue | 2 × t·n | 1408 | CopyInSumOut / col/rowNormGrad |
| gradOutQueue | 1 × t·n² | 2816 | TransposeXOut / CopyOut |
| gradTransposeBuf | 1 × t·n² | 2816 | 梯度驻留缓冲（三个 Grad 函数的原位更新对象） |
| calcTmpBuf | max(2·t·n² + t·n, 转置栈) | ≥ 6336 | Mul 结果 / dot_prod / Sub 结果 / Transpose 栈 |
| **合计** | | **7·t·n² + 3·t·n = 21824**（85KB < 190KB） | |

3. **需要观察的现象**：表中「合计」行与 tiling.cpp L375-L378 注释、L380 公式的分母 `(7n²+3n)` 是否一致。
4. **预期结果**：完全一致（7 份 tnn + 3 份 tn）。再回答一个思考题：为什么 normInQueue/sumInQueue 深度是 2 而 gradIn/gradOut 是 1？——每轮迭代要连续拷入「列、行」两组 norm+sum，双缓冲让第 k 份的 DeQue/计算与第 k+1 份的 CopyIn 重叠；grad 只在 loop 首尾各用一次，无需双缓冲。
5. 把 n 换成 8 重算一遍（7×64+24=472，maxTokensPerLoop=⌊194560/1888⌋=103→对齐到 96），检验你对 `& ~0x7` 的理解。

#### 4.3.5 小练习与答案

**练习 1**：为什么一定要先 TransposeXIn，直接在 [t,n,n] 布局上算不行吗？

**答案**：`norm_out`/`sum_out` 在 GM 上 t 在末轴，分块拷贝（L295-L324）天然得到 [n,n,t] 布局；若梯度保持 [t,n,n]，每次 Mul/ReduceSum 都要跨 stride 访问，且 ReduceSum 无法一次覆盖一批 token。转置后归约轴在最前/次前维、t 连续排布，一批 token 的同一步反向可以整段向量满载。代价是首尾各一次 Transpose（及转置栈缓冲）。

**练习 2**：BSNN（4 维）输入时 kernel 里哪段代码处理了 B 和 S？

**答案**：没有专门处理。tiling 已把 `totalLength_ = bSize × sSize` 折叠成 T（L298-L301），连续内存下 (B,S,n,n) ≡ (B·S,n,n)、[2I,n,n,B,S] ≡ [2I,n,n,B·S]，kernel 全程只认 token 数 totalLength。这就是「layout 差异在 tiling 层消化」的例子。

**练习 3**：16MB workspace（tiling L30、L462-L467）被 kernel 用在哪里？

**答案**：没被用。入口在 workspace 为空时早退（L34-L43），`Init` 收下 `userWS` 参数但函数体不引用它——属「预留」。阅读时不要想当然认为 workspaceSize 一定对应实际显存消费，以 kernel 代码为准。

### 4.4 UT fixture：错误注入枚举与用例工厂

#### 4.4.1 概念说明

tiling UT 的目的是在**无 NPU、无真实图引擎**的环境下验证 Host 侧 tiling 函数的行为（回顾 u3-l4 桩机制与 u8-l1 faker 框架）。本算子的 UT 分两个文件：

- `test_ai_infra_sinkhorn_grad_fixture.h`：fixture（夹具）。三件事：① 用 `gert::TilingContextPara` 伪造 tiling 上下文（算子名 + 输入输出描述 + CompileInfo）；② 定义 `ErrorType` 枚举，枚举出约 30 种非法输入形态；③ 提供 `ApplyErrorModifications` 把合法上下文「打坏」成指定的非法形态，以及 `TestAiInfraSinkhornGradTNN/BSNN` 两个用例工厂把一切打包交给 `ExecuteTestCase`。
- `test_ai_infra_sinkhorn_grad_tiling.cpp`：纯用例列表。每个 `TEST_F` 一行调用，正常用例只给 (T/B,S,n,numIters)，异常用例多传一个 `ErrorType`。

这种「fixture 承载结构、test 文件只写参数」的分工让新增用例的成本降到一行。

#### 4.4.2 核心流程

一个用例从声明到断言的路径：

```text
TEST_F(AiInfraSinkhornGradTiling, case_normal_3d)
  └─ TestAiInfraSinkhornGradTNN(caseName, T=100, n=4, numIters=3)
       ├─ BuildTilingContextTNN(100,4,3)
       │     构造 TilingContextPara：
       │       算子名 "AiInfraSinkhornGrad"        ← 路由到 IMPL_OP_OPTILING 注册的 tiling
       │       3 输入: [T,n,n] / [2I,n,n,T] / [2I,n,T]  全 DT_FLOAT + FORMAT_ND
       │       1 输出: [T,n,n]
       │       空 attr 列表                        ← 本算子无属性
       │       compileInfo {48,48}                 ← aicNum/aivNum，决定 GetCoreNumAiv
       ├─ (异常用例) ApplyErrorModifications(...)  ← 按 ErrorType 改坏 shape/dtype/删输入
       ├─ 期望值推导: expectStatus = NONE ? SUCCESS : GRAPH_FAILED
       │             expectTilingKey = 0（恒为 0）
       │             expectTilingDataStr = "" / expectWorkspaces = {}（跳过断言）
       └─ ExecuteTestCase(para, expectStatus, expectTilingKey, "", {}, 0, TilingData2Str<int64_t>)
             ├─ faker 把 para 变成 gert::TilingContext
             ├─ 调用注册的 TilingForAiInfraSinkhornGrad → 责任链 → 唯一模板
             └─ ASSERT_EQ(status) / ASSERT_EQ(tilingKey)
                 （tilingData、workspace 仅在期望值非空时才比较）
```

#### 4.4.3 源码精读

- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h:L21-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h#L21-L64)：`ErrorType` 枚举，覆盖五类错误——维度数（3/4/5 维不符）、n 或 T/B/S 不一致、dtype 非 fp32、空 Tensor/空 Desc、num_iters 越界（<1 或 >100）。基本与 tiling.cpp 各 `OP_LOGE` 分支一一对应，是「校验分支 ↔ 测试枚举」的镜像关系。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h:L84-L101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h#L84-L101)：`BuildTilingContextTNN`——注意 shape 是 `{storageShape, originShape}` 二元组，`static compileInfo = {48, 48}` 会被 faker 当作平台核数。`BuildTilingContextBSNN`（L106-L123）同构，只是 4/5 维形态。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h:L128-L268](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h#L128-L268)：`ApplyErrorModifications` 的 switch——每种 ErrorType 对 context 做最小破坏。例如 `NORM_OUT_2I_NOT_EVEN` 把 dim0 改成 `2*numIters+1`（奇数，L158-L166）；`SUM_OUT_NONE` 直接 `pop_back()` 删掉第三个输入（L235-L237）；dtype 错误只改 `dtype_` 为 `DT_FLOAT16`（L145-L147 等）。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h:L273-L290](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_fixture.h#L273-L290)：`TestAiInfraSinkhornGradTNN` 用例工厂——期望状态由「是否 NONE」推导，`expectTilingKey = 0` 硬编码（与 tiling 的 `TILING_KEY_GENERALIZED` 对应）。L276 的局部 `compileInfo = {40, 40}` 是死代码（未被使用，真正生效的是 Build 函数里的 static {48,48}），阅读时别被它带偏。`TilingData2Str<int64_t>`（L313-L323）只是把实际 tilingData 序列化成日志字符串。
- [ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:L272-L292](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L272-L292)：执行器的断言语义——`expectWorkspaces` 非空才逐项 `ASSERT_EQ`；`tilingKey` **总是**断言；`expectTilingData != ""` 才比较序列化串。所以本算子 fixture 传空串/空列表等于「只断言 status + tilingKey」，切分数值正确性交给 ST 与人工读 `OP_LOGI` 日志。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_tiling.cpp:L19-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_tiling.cpp#L19-L57)：8 个正常用例——小 T/大 T、n=1/4/6/8、numIters=1/28/100、TNN/BSNN、网络典型值 (8192,4) 与 (2,4096,4)。
- [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_tiling.cpp:L61-L198](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/ut/op_host/test_ai_infra_sinkhorn_grad_tiling.cpp#L61-L198)：20 个异常用例，按 grad_output / norm_out / sum_out / grad_input / 空指针 / num_iters 六组组织，与枚举分组一致。

#### 4.4.4 代码实践

找茬型阅读实践（纯静态，无需编译）。

1. **实践目标**：确认「枚举 ↔ 错误注入 ↔ 用例」三层是否完全对齐，找出未接线的枚举。
2. **操作步骤**：
   - 把 `ErrorType` 枚举成员逐个与 `ApplyErrorModifications` 的 case 比对；
   - 再与 test 文件里实际使用的 ErrorType 比对。
3. **需要观察的现象**：是否有枚举成员没有对应 case、也没有任何用例使用。
4. **预期结果**：`GRAD_OUTPUT_NONE` 与 `GRAD_INPUT_NONE` 两个枚举（fixture L52、L57）在 switch 中**没有 case**（switch 只处理 `*_NONE_DESC` 版本与 norm/sum 的 `_NONE`），测试文件也未使用它们。原因：`TilingContextPara` 只有 `pop_back()` 能删**尾部**输入（norm_out/sum_out 可以），删第 0 个输入或输出没有便捷手段。若强行用它们写用例，会落入 `default` 分支（不做任何修改），期望 GRAPH_FAILED 而实际跑出 SUCCESS，用例必失败。
5. 顺手再发现一处小瑕疵：`NORM_OUT_NUM_ITERS_LESS_1` 的 case 末尾有两个连续 `break;`（L254-L255），第二个是不可达死代码——不影响行为，但说明 fixture 也是人写的代码，值得带着怀疑读。

#### 4.4.5 小练习与答案

**练习 1**：fixture 构造的 `TilingContextPara` 为什么能让执行流程走到本算子的 tiling 实现？

**答案**：para 里的算子名字符串 `"AiInfraSinkhornGrad"` 与 `IMPL_OP_OPTILING(AiInfraSinkhornGrad)` 的注册名对齐，faker 据此构造 context 并调用注册的 `TilingForAiInfraSinkhornGrad`，再经 `TilingRegistry::DoTilingImpl` 进入责任链，命中优先级 2000 的唯一模板——与生产路径完全相同的调度链。

**练习 2**：`expectTilingDataStr` 传空串意味着什么？本算子为什么可以这样做？

**答案**：执行器仅在期望串非空时才逐字符比较序列化的 tilingData（tiling_case_executor.cpp L287-L292），空串即跳过该断言，同理空 `expectWorkspaces` 跳过 workspace 断言。本算子 tilingData 是 12 个 int64 的纯数值切分，正常路径只断言「跑成功且 key=0」，数值正确性由 ST（与 CPU golden 比对）兜底；若想加固，可手算一组切分值拼成期望串（4.2.4 的验算结果就是现成素材）。

**练习 3**：要覆盖「grad_output 为空指针」这个 tiling 分支（`OP_CHECK_IF(gradOutputShape_ == nullptr, ...)`），现有枚举够用吗？

**答案**：不够。枚举里的 `GRAD_OUTPUT_NONE` 未接线（见 4.4.4），而 `GRAD_OUTPUT_NONE_DESC`（shape 置空）走的是 `GetInputShape` 返回空指针的路径，与 `CheckInputShape` 里的判空分支并非同一处。要做到精确覆盖需扩展 fixture（例如为 para 增加「输入描述缺失」的构造方式），这属于框架层改动，超出单算子 UT 的范畴——这也是阅读 UT 时要区分「测到的分支」与「想测的分支」的原因。

## 5. 综合实践

把本讲三块内容（公式、tiling/kernel、UT）串成一条线。关于任务描述中的「对 x/scale 的梯度」先做一个澄清：经查证，u5-l1 前向算子 `npu_sinkhorn` 的张量输入只有 `x` 一个（见 `manifold_constrained_hyper_connection_sinkhorn_enhance/docs/npu_sinkhorn.md` 参数表），没有 scale 参数，因此本实践验证「输出对 x 的梯度」即可完整覆盖本算子的反向语义。

### 任务 A：torch.autograd 全链验证（CPU 即可）

用 autograd 作为裁判，验证你手工组织的多轮反向循环（4.1.2 索引表）与自动微分一致。示例代码（非仓库文件）：

```python
import torch
torch.manual_seed(0)

def sinkhorn_fwd(x, num_iters, eps):
    """可微前向：同时保存 norm_out/sum_out（与 u5-l1 / ST 脚本一致）"""
    norms, sums = [torch.softmax(x, dim=-1).clone()], [None]
    curr = torch.softmax(x, dim=-1) + eps
    col = curr.sum(dim=-2, keepdim=True) + eps
    curr = curr / col
    norms.append(curr.clone()); sums.append(col.clone())
    for _ in range(num_iters - 1):
        row = curr.sum(dim=-1, keepdim=True) + eps
        curr = curr / row
        norms.append(curr.clone()); sums.append(row.clone())
        col = curr.sum(dim=-2, keepdim=True) + eps
        curr = curr / col
        norms.append(curr.clone()); sums.append(col.clone())
    return curr, norms, sums

T, n, num_iters, eps = 16, 4, 2, 1e-6          # 综合实践指定：n=4, num_iters=2
x = torch.randn(T, n, n, dtype=torch.float64, requires_grad=True)
y, norms, sums = sinkhorn_fwd(x, num_iters, eps)
g = torch.randn(T, n, n, dtype=torch.float64)   # 假想上游梯度
(y * g).sum().backward()

gc = g.clone()                                  # 手工反向：k = 2*num_iters-1 → 0
for i in range(num_iters - 1, 0, -1):
    gc = (gc - (gc * norms[2*i+1]).sum(dim=-2, keepdim=True)) / sums[2*i+1]   # 列
    gc = (gc - (gc * norms[2*i]).sum(dim=-1, keepdim=True))   / sums[2*i]     # 行
gc = (gc - (gc * norms[1]).sum(dim=-2, keepdim=True)) / sums[1]               # 初始列
grad_manual = (gc - (gc * norms[0]).sum(dim=-1, keepdim=True)) * norms[0]     # softmax

print("max |autograd - manual| =", (x.grad - grad_manual).abs().max().item())
assert torch.allclose(x.grad, grad_manual, atol=1e-10)
```

**预期结果**：float64 下最大误差在 1e-12 量级、断言通过（待本地验证）。再把 num_iters 改成 20、T 改成 2048（文档典型值）复跑一次；若想同时复现 ST 的 NPU 对比，可参考 [ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/st/test_sinkhorn_grad.py:L156-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/st/test_sinkhorn_grad.py#L156-L187)（需要 NPU 与已安装的 torch_ops_extension wheel）。

### 任务 B：fixture 阅读报告

读完 fixture 后，用一张表回答「它预置了哪些测试上下文」：

| 预置项 | 取值 | 作用 |
|---|---|---|
| 算子名 | `"AiInfraSinkhornGrad"` | 路由到 IMPL_OP_OPTILING 注册的 tiling 责任链 |
| 输入描述 | 3 个输入的 storage/origin shape + DT_FLOAT + FORMAT_ND | 喂给 GetInputShape/GetInputDesc |
| 输出描述 | 1 个输出 [T,n,n] 或 [B,S,n,n] | 喂给 CheckOutputShape |
| attr 列表 | 空 | 本算子无属性，num_iters 由 shape 推导 |
| CompileInfo | {aicNum=48, aivNum=48} | 决定 GetCoreNumAiv → 核间切分 |
| 期望断言 | status（NONE→SUCCESS 否则 FAILED）、tilingKey=0 | 执行器仅断言这两项（tilingData/workspace 传空跳过） |

### 任务 C：新增 N=4、num_iters=2 的用例并编译运行

在 `test_ai_infra_sinkhorn_grad_tiling.cpp` 末尾追加（示例代码，按仓库现有风格）：

```cpp
TEST_F(AiInfraSinkhornGradTiling, case_normal_tnn_n4_iter2)
{
    TestAiInfraSinkhornGradTNN("case_normal_tnn_n4_iter2", 64, 4, 2);
}

TEST_F(AiInfraSinkhornGradTiling, case_normal_bsnn_n4_iter2)
{
    TestAiInfraSinkhornGradBSNN("case_normal_bsnn_n4_iter2", 2, 32, 4, 2);
}
```

然后在容器内编译运行 op_host UT（参数含义见 u1-l4：`-u` 开启测试模式、`--ophost` 只构建 op_host UT 目标、`-n` 白名单、`-c` 目标芯片）：

```bash
bash build.sh -u -n ai_infra_sinkhorn_grad -c ascend910_93 --ophost
```

**需要观察的现象**：新用例出现在 gtest 输出且 PASS；正常用例的日志里能看到 `total_length=64, n=4, numIters=2` 与 `Core splitting: ...` 两个 `OP_LOGI`。
**预期结果**：全部用例通过；用 4.2.4 的方法核对 n=4、T=64 时的切分数值（48 核下 perCoreElements 会触发 `MIN_PER_CORE_ELEMENTS=32` 限核：⌈64/48⌉=2 < 32 → 抬到 32 → needCoreNum=⌈64/32⌉=2，待本地验证）。
**无 NPU 环境时**：至少完成任务 A/B 与用例代码编写；UT 编译需要 CANN 工具链与 bisheng 编译器（见 u1-l3/u1-l4），缺失时列出环境清单，标注「待本地验证」。

## 6. 本讲小结

- **反向公式三步口诀**：沿前向同一轴做内积 \( \langle g, norm\_out \rangle \)、减去、除以 `sum_out`；唯独 softmax 步（索引 0）是**乘** `norm_out[0]`——三种 kernel 函数 colNormGrad/rowNormGrad/softmaxGrad 的全部差异就这一点。
- **保存中间量 = 空间换反向时间**：前向落盘 2×num_iters 份 norm/sum，反向即可从 k=2·num_iters−1 倒序执行 2×num_iters 个「伪逆步骤」，不必重算前向迭代链。
- **两级 tiling**：`tiling_base.cpp` 是十几行的注册薄壳（`IMPL_OP_OPTILING` + 转发责任链），真正的校验与切分在 `tiling.cpp` 的 TilingBase 子类；`num_iters` 从 norm_out 的 dim0 反推而非 Attr；BSNN 在 tiling 层折叠成 TNN，kernel 无感。
- **切分契约 12 字段全部被消费**：UB 预算 7tnn+3tn 决定核内一次处理多少 token（8 对齐），核间均分 + 末核单独四元组，blockDim=needCoreNum；tilingKey 恒 0、workspace 16MB 预留未用。
- **Kernel 是 AIV-only 的向量流水**：转置到 [n,n,t] 让归约满载，`ReduceSum(Pattern::Reduce::RA)` + Mul/Sub/Div 原语直接对应公式；norm/sum 走「尾轴分段 + stride」的分块 DataCopyPad。
- **UT fixture 范式**：`TilingContextPara` 伪造上下文 + `ErrorType` 枚举注入约 30 种非法输入 + 用例工厂一行一例；执行器只硬断言 status 与 tilingKey；枚举里有未接线的成员（GRAD_OUTPUT_NONE/GRAD_INPUT_NONE），读 fixture 也要带怀疑。

## 7. 下一步学习建议

- **u5-l3（MHC 前处理算子 pre 与 pre_grad）**：将首次走读 MHC 家族中带完整 op_api 源码的算子（aclnn 两段式全链路），本讲的 `torch.ops.custom.npu_sinkhorn_grad` 调用方式将在那里看到 C++ 侧的来源。
- **回看 u4-l4（FA 反向）**：对比另一种「保存中间量」的风格——FA 把 softmaxMax/Sum 作为**前向输出**传给反向，Sinkhorn 则把 norm/sum 作为**反向输入**由调用方保管，两种契约各有取舍。
- **u8-l2（编写 Tiling UT）**：本讲的 fixture 是入门样本，u8 系列会系统讲解 faker/executor 框架与 `TilingContextPara` 的完整能力。
- **延伸阅读源码**：`ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/st/test_sinkhorn_grad.py` 的 `test_sinkhorn_grad_uncontinue`（非连续 Tensor 用 torch.as_strided 构造），理解 `AutoContiguous` 约束在测试侧如何被反向利用。
