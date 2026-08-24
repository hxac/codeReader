# QAT 对称量化算子：per_tensor 与 per_channel

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出对称量化感知训练（QAT）的完整前向公式链：scale 防零保护 → 归一化 → 伪量化（round + clamp）→ 反量化。
2. 推导**直通估计器（Straight-Through Estimator, STE）**为什么能让处处导数为零的 round 函数参与训练，并用 torch 的 `detach()` 技巧亲手复现。
3. 推导对称 QAT 的反向梯度，理解两个关键结论：**grad_weight 中 scale 因子恰好相消**、**grad_scale 由「乘法路径 + 除法路径」两部分组成**。
4. 走读 `ai_infra_qat_symmetric_per_tensor` 与 `ai_infra_qat_symmetric_per_channel` 两对（前向 + 反向）pypto kernel，说明 **per_tensor 与 per_channel 在 scale 形状上的差异如何连锁地影响 kernel 的广播方式、归约维度与切分策略**。

本讲是 u7-l1（pypto 编程模型）之后的第二讲，继续使用同一个源码文件 `ai_infra_pypto_qat.py`，但视角从「pypto 怎么写」切换到「算法怎么落到 pypto 上」。

## 2. 前置知识

### 2.1 量化与 scale

把一个浮点数 \(w\) 映射到只有有限个电平的整数格点上的过程叫量化。最简单的**对称量化**只用一个缩放系数 \(s\)：

\[ q = \operatorname{round}\!\left(\frac{w}{s}\right), \qquad \hat{w} = \operatorname{clip}(q,\ V_{\min},\ V_{\max}) \times s \]

- \(s\) 叫 **scale（缩放系数）**，单位是「每个整数格点代表多少真实值」。
- \(V_{\min}, V_{\max}\) 是整数格点的上下界。对 INT8 是 \(-128\) 和 \(127\)，所以本算子的 `min_v`/`max_v` 是**浮点型整数**（小数位全 0，如 `-128.0`/`127.0`）。
- \(\hat{w}\) 是**反量化**后的值——它只能落在 \(\{k \cdot s\}\) 这些离散格点上，与 \(w\) 之间的差就是量化噪声。

### 2.2 量化感知训练（QAT）与 STE

训练后量化（PTQ）在训练结束后才量化，精度损失不可控；**QAT** 则在前向传播中就插入「量化再反量化」的模拟操作，让模型在训练中适应量化噪声。难点在反向传播：\(q = \operatorname{round}(w/s)\) 是阶梯函数，导数几乎处处为 0，梯度根本传不回去。

**STE 的解法**：前向照常取阶梯值，反向假装它是恒等函数，梯度直通：

\[ \frac{\partial q}{\partial x} \approx 1 \]

在 torch 里用一个经典技巧实现——「加上一个 detach 掉的差值」：

```python
quantized = x + (x.round() - x).detach()
```

前向时 `x + round(x) - x = round(x)`；反向时 `(...).detach()` 不建图，所以 \(\partial(\text{quantized})/\partial x = 1\)。本仓库 ST 测试的 golden 实现用的正是这一行（见 4.1.3）。

而 `clip` 的 STE 是分段常数：区间内梯度为 1，区间外为 0——这一点**可以精确实现**，不需要近似。

### 2.3 pypto 编程模型回顾（来自 u7-l1）

- `@pypto.frontend.jit` 把带 `pypto.Tensor` 类型标注的 Python 函数即时编译成 NPU 设备代码；无标注的参数是 Host 侧预计算好的标量。
- 输出走**目标传递风格**：wrapper 用 `torch.empty` 分配输出张量，按位置传给 kernel，kernel 用 `pypto.assemble` 写回。
- 数据流三原语：`pypto.view`（取块）、`pypto.cast`（BF16 进 FP32 算 BF16 出）、`pypto.assemble`（镜像写回）。
- 切分内嵌 kernel：`pypto.loop_unroll(..., unroll_list=[512, 32, 8])` 大块优先、余量逐级降级；`pypto.set_vec_tile_shapes(n, m)` 定制向量 tile 形状。

### 2.4 三个必须分清的「粒度」

| 粒度 | scale 形状 | 共享范围 | 本仓库对应算子 | 场景 |
|---|---|---|---|---|
| per_tensor | (1, 1) | 整个张量一个 scale | `ai_infra_qat_symmetric_per_tensor` | Embedding 层 |
| per_channel | (N, 1) | 每个输出通道一个 scale | `ai_infra_qat_symmetric_per_channel` | Lm Head 层 |
| per_group | (N·M/group_size, 1) | 每 128 个元素一组 | `ai_infra_qat_asymmetric_per_group`（u7-l3） | Transformer Linear 层 |

本讲只讲前两种（对称）；第三种（非对称 + 分组）留到 u7-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py` | 全部 6 个 QAT 算子的 kernel + wrapper，本讲读其中 4 个 symmetric 函数（约 L243–L393、L396–L559） |
| `pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md` | 接口文档：公式推导、参数表、约束条件、使用示例 |
| `pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py` | per_tensor 前向 ST：含 detach 版 golden 参考实现 |
| `pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor_backward.py` | per_tensor 反向 ST：用 autograd 对拍 kernel 梯度 |
| `pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py` | ST 公共设施：造数分布、`forward_test`/`backward_test_autograd` 对拍框架、精度指标 |

推荐阅读顺序：先读 `qat_ops.md` 的 symmetric 两节建立公式直觉 → 再读 `.py` 中 per_tensor 前向 → 反向 → per_channel 前向 → 反向 → 最后回看 ST 测试如何验证。

## 4. 核心概念与源码讲解

### 4.1 对称 QAT 的数学模型与 STE 的 torch 复现

#### 4.1.1 概念说明

这个模块不涉及任何设备代码，只建立本讲的数学地基。对称 QAT 前向由四步组成，文档 [qat_ops.md:L57-L95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L57-L95) 给出的定义是：

**Step 1 scale 防零保护**（防止除零）：

\[ s' = \max(s,\ \varepsilon) \]

**Step 2 归一化**：

\[ W_{\text{norm}} = \frac{W}{s'} \]

**Step 3 伪量化**（STE 近似的 round + 截断）：

\[ W_{\text{quant}} = \operatorname{detach}\!\left(\operatorname{round}(W_{\text{norm}}) - W_{\text{norm}}\right) + W_{\text{norm}} \]
\[ W_{\text{clamp}} = \operatorname{clamp}(W_{\text{quant}},\ V_{\min},\ V_{\max}) \]

**Step 4 反量化**：

\[ W_q = W_{\text{clamp}} \times s' \]

为什么需要 Step 1：scale 是可学习参数，训练初期可能学到 0 或负值，直接除会得到 inf/NaN。用 \(s' = \max(s, \varepsilon)\) 兜底后，\(s \le \varepsilon\) 时梯度也应为 0（对 \(s\) 的扰动不再影响输出）——这个「scale 掩码」在反向 kernel 里会再次出现。

#### 4.1.2 核心流程

用伪代码描述前向（不含设备细节）：

```text
输入: W[N,M] (BF16), s (BF16 标量), eps, min_v, max_v
1. s' = max(s, eps)                          # 防零保护
2. W_norm = W / s'                           # 归一化到整数格点附近
3. W_round = round(W_norm)                   # STE: 前向取整, 反向直通
4. W_clamp = clip(W_round, min_v, max_v)     # 截断, 反向区间内 1 区间外 0
5. 输出 = W_clamp * s'                       # 反量化回真实值域
输出: [N,M] (BF16)
```

一个帮助直觉的观察：Step 2 除以 \(s'\)、Step 5 乘回 \(s'\)，一进一出。对**区间内**的元素，\(W_q = \operatorname{round}(W/s') \cdot s'\)——量化格点上的值；对**区间外**的元素，\(W_q = V_{\min,\max} \cdot s'\)——被钉死在边界上，此时对 \(W\) 的微小扰动不改变输出（梯度为 0），这正是反向里 mask 的来源。

#### 4.1.3 源码精读：ST 测试里的 detach 版 golden

ST 测试中的参考实现是理解 STE 的最短路径。[test_ai_infra_qat_symmetric_per_tensor.py:L20-L54](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py#L20-L54) 定义了 golden 工厂函数，其中核心五行：

- [test_ai_infra_qat_symmetric_per_tensor.py:L42](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py#L42) —— `protected_scale = torch.where(scale_in > eps_tensor, scale_in, eps_tensor)`，即 \(s' = \max(s, \varepsilon)\)。
- [test_ai_infra_qat_symmetric_per_tensor.py:L43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py#L43) —— `weight_normalized = weight_in / protected_scale`，归一化。
- [test_ai_infra_qat_symmetric_per_tensor.py:L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py#L45) —— **本讲最重要的一行**：`weight_rounded = weight_normalized + (weight_normalized.round() - weight_normalized).detach()`，detach 技巧实现 STE。
- [test_ai_infra_qat_symmetric_per_tensor.py:L46-L47](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py#L46-L47) —— clamp 后乘回 `protected_scale`，完成反量化。

注意 golden 的双精度设计：`is_golden=False` 时用 float32（作为 benchmark，与 kernel 同精度对拍），`is_golden=True` 时用 float64（作为真值，供 MARE/MERE/RMSE 三方精度指标使用）。这套三方对拍机制由 [tests/utils.py:L335-L384](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L335-L384) 的 `forward_test` 驱动，细节在 u7-l4 展开。

另外注意设备 kernel 里**没有** detach——detach 是给 torch autograd 用的语义。pypto kernel 前向直接 `round`，STE 是通过「单独写一个反向 kernel」来实现的：前向 kernel 管数值，反向 kernel 管梯度，两者手工配对（这正是 u7-l1 讲过的「kernel 签名即契约」在反向的延伸）。

#### 4.1.4 代码实践

**实践目标**：在纯 CPU 环境（只需 torch，无需 NPU/pypto）亲手验证 STE 的「前向取整、反向直通」。

**操作步骤**（示例代码，可直接保存为 `ste_check.py` 运行）：

```python
# 示例代码：验证 STE 的梯度行为（CPU 即可运行）
import torch

torch.manual_seed(0)
W = (torch.rand(4, 8, dtype=torch.float64) * 300 - 150).requires_grad_(True)
s = torch.tensor([[0.5]], dtype=torch.float64).requires_grad_(True)
eps, min_v, max_v = 1e-4, -128.0, 127.0

sp = torch.where(s > eps, s, eps)                    # Step 1
wn = W / sp                                          # Step 2
wr = wn + (wn.round() - wn).detach()                 # Step 3a: STE
cl = torch.clamp(wr, min_v, max_v)                   # Step 3b
out = cl * sp                                        # Step 4
out.backward(torch.ones_like(out))

mask = ((wn.round() >= min_v) & (wn.round() <= max_v)).double()
print("前向 == round 后再反量化 :", torch.allclose(out, cl * sp))
print("grad_W == 1 * mask      :", torch.allclose(W.grad, mask))
print("grad_s == 两路径公式     :",
      torch.allclose(s.grad, (out / sp * 0 + 1 * cl).sum() - (1 / sp) * (mask * W).sum()))
```

**需要观察的现象**：

1. `grad_W == 1 * mask` 打印 `True`——上游梯度为全 1 时，区间内元素梯度恰为 1（直通），区间外为 0。
2. `grad_s` 的两路径公式与 autograd 结果一致（推导见 4.3.1）。

**预期结果**：三行全部 `True`。若把 `min_v/max_v` 改成 `(-8.0, 7.0)`（更窄的量化范围），会有更多元素落在区间外，`mask` 中 0 的比例上升，但等式依然成立。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 STE 写成 `wr = wn.round()`（不加 detach 差值项），autograd 会发生什么？

**答案**：`round` 的导数几乎处处为 0，反向传播到 `wn` 的梯度全为 0，进而 `W.grad` 与 `s.grad` 全为 0，训练完全停滞。detach 技巧的本质是把「取整造成的数值偏移」当成常数加回去，让计算图中从 `wn` 到 `wr` 只剩恒等路径。

**练习 2**：为什么 clamp 的梯度不需要近似，而 round 需要？

**答案**：clamp 是分段线性函数——区间内导数恒为 1、区间外恒为 0，这个次梯度可以**精确**刻画；而 round 的输出对输入的微小扰动完全不敏感（除整数点外导数严格为 0），若照实求导梯度必死，只能用「假装恒等」的 STE 近似。

**练习 3**：Step 1 的 `max(s, eps)` 在 \(s = \varepsilon\) 边界处，\(\partial s'/\partial s\) 取 0 还是 1 有区别吗？

**答案**：数值上前向无区别（\(s=\varepsilon\) 时两种取法 \(s'\) 都是 \(\varepsilon\)）。梯度上是次梯度的选取问题。本仓库 kernel 用 `pypto.ge(scale, eps)`（即 \(s \ge \varepsilon\) 取 1）实现掩码，文档表述为 \(s > \varepsilon\)——只在等值点这一个测度为零的集合上不一致，实际无影响，但读码时要知道有这个边界差异。

### 4.2 per_tensor 前向 kernel：单个 scale 的标量场景

#### 4.2.1 概念说明

`ai_infra_qat_symmetric_per_tensor` 是**张量级**对称量化：整个 (N, M) 权重共享一个标量 scale（形状 (1, 1)），适用于 Embedding 层——词表权重数量巨大但分布相对均匀，一个 scale 就够。文档明确写了这一场景定位：[qat_ops.md:L15-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L15-L17)。

因为 scale 是标量，它是整个 kernel 中**唯一的循环不变量**——这决定了 kernel 可以把 scale 的保护与类型转换提到循环外，只做一次。

#### 4.2.2 核心流程

```text
kernel 入口: weight[N,M] BF16, scale[1,1] BF16, output[N,M] BF16, 标量 eps/min_v/max_v
1. n, m = weight.shape                        # 设备侧取 shape
2. set_vec_tile_shapes(32, 512)               # 向量 tile 按 32 行 × 512 列
3. 循环外: scale → FP32, s' = max(scale, eps)  # 循环不变量外提
4. loop_unroll(0, n, unroll_list=[512, 32, 8]) # N 维大块优先, 余量降级
5. 每 tile:
   a. view 取 [tile_n, m] 权重块, cast 到 FP32
   b. expand_clone 把 s' 从 [1,1] 扩展成 [tile_n, 1]
   c. 四步公式: div → round → clip → mul
   d. cast 回 BF16, assemble 写回输出
```

#### 4.2.3 源码精读

per_tensor 前向 kernel 位于 [ai_infra_pypto_qat.py:L396-L436](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L396-L436)。逐段拆解：

- [L401-L408](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L401-L408)：kernel 签名。三个张量参数都有 `pypto.Tensor` 标注——注意 `scale` 的形状标注是 `[1, 1]`（**静态已知**，不是 DYNAMIC），而 weight/output 是 `[pypto.DYNAMIC, ...]`（N 运行期确定）。`eps/min_v/max_v` 无标注，是 Host 预计算标量。
- [L410-L413](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L410-L413)：`unroll_list = [512, 32, 8]` 与 `set_vec_tile_shapes(32, 512)`——N 维按 512/32/8 三级展开，向量 tile 取 32 行。
- [L414-L415](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L414-L415)：**循环不变量外提**——scale 的 FP32 转换与防零保护只做一次，这是标量 scale 独有的优化机会（per_channel 做不到，见 4.4）。
- [L417-L422](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L417-L422)：`loop_unroll` 展开循环，每次拿到 `(n_offset, unroll_length)`。
- [L425-L426](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L425-L426)：`view` 取 `[tile_n, m]` 块并升精到 FP32——**M 维是整条取走的，没有任何尾块逻辑**，这个事实是理解约束条件的关键（见 4.2.4）。
- [L428](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L428)：`scale_n = pypto.expand_clone(protected_scale, [tile_n, 1])`——把标量 scale 物化成与 tile 行数相同的列向量，使后续除法/乘法可以按形状广播。
- [L429-L432](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L429-L432)：四步公式的落地，与文档 Step 2–4 一一对应：`div`（归一化）→ `round`（取整，STE 的前向半边）→ `clip`（截断）→ `mul`（反量化）。
- [L434-L436](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L434-L436)：降回 BF16 并 `assemble` 写回输出张量的对应块。

wrapper 在 [L439-L450](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L439-L450)：`torch.empty` 分配输出、按位置传参、返回输出。**注意 wrapper 里没有任何 shape/dtype 校验**——约束完全靠文档约定，调用方传错形状不会有显式报错（这个空缺正是综合实践要补的）。

还有一个文档与代码的差异要留心：文档 [qat_ops.md:L30-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L30-L37) 的接口定义写了默认值 `eps: float = 1e-4, min_v: float = -128.0, max_v: float = 127.0`，但代码 [L439](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L439) 的签名**没有默认参数**，五个参数必须全部显式传入（ST 测试 [test_ai_infra_qat_symmetric_per_tensor.py:L67](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py#L67) 也是全量传参）。以代码为准。

#### 4.2.4 代码实践

**实践目标**：在 CPU 上用 numpy/torch 模拟 kernel 的「分块 + 四步公式」，验证分块处理与整体向量化结果完全一致，并理解文档约束 `M ∈ [128, 3072]` 且被 128 整除的来源。

**操作步骤**（示例代码）：

```python
# 示例代码：CPU 模拟 per_tensor kernel 的分块前向
import torch

def forward_vectorized(W, s, eps, min_v, max_v):
    sp = torch.where(s > eps, s, eps)
    return torch.clamp((W / sp).round(), min_v, max_v) * sp

def forward_tiled(W, s, eps, min_v, max_v, unroll_list=(512, 32, 8)):
    sp = torch.where(s > eps, s, eps)          # 循环不变量外提
    out = torch.empty_like(W)
    n = W.shape[0]
    start = 0
    while start < n:                            # 模拟 loop_unroll 大块优先
        for u in unroll_list:
            if start + u <= n or u == unroll_list[-1]:
                tile_n = min(u, n - start)
                break
        out[start:start+tile_n] = torch.clamp(
            (W[start:start+tile_n] / sp).round(), min_v, max_v) * sp
        start += tile_n
    return out

torch.manual_seed(0)
W = torch.randn(1000, 256, dtype=torch.float64)
s = torch.tensor([[0.05]], dtype=torch.float64)
a = forward_vectorized(W, s, 1e-4, -128.0, 127.0)
b = forward_tiled(W, s, 1e-4, -128.0, 127.0)
print("分块 == 整体 :", torch.equal(a, b))
```

**需要观察的现象**：`分块 == 整体` 打印 `True`。注意 N=1000 不是 512/32/8 的倍数，`loop_unroll` 的降级机制能处理任意 N——**但 M 没有任何尾块机制**（源码 `view` 一律整条取 m），所以 M 必须自己保证对齐。

**预期结果**：`True`。再思考约束：文档 [qat_ops.md:L97-L102](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L97-L102) 要求 M ∈ [128, 3072] 且被 128 整除。从代码能确认的部分：M 是 `set_vec_tile_shapes(32, 512)` 向量 tile 的完整搬运维度、无尾块逻辑，「128 对齐」保证任意 tile 组合下数据块都是满块（128 是 512 等 tile 粒度的公因子）。上限 3072 的原因源码未说明——推测与反向 kernel 的 `min(m, 4096)` 切分（[L472](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L472)）及 UB/寄存器预算有关，**待确认**。

#### 4.2.5 小练习与答案

**练习 1**：如果 `expand_clone` 忘了写、直接用 `[1,1]` 的 scale 去除 `[tile_n, m]` 的块，会怎样？

**答案**：数学上形状广播语义仍成立，但 pypto 的向量原语要求操作数形状一致以生成满载的向量指令；`expand_clone` 把标量**物化**成 `[tile_n, 1]`，使除法/乘法成为形状匹配的逐元素运算。漏写可能导致编译期形状校验失败或生成的指令不满载（以 pypto 编译器实际行为为准，待本地验证）。

**练习 2**：为什么不把 `round + clip` 合并成 `clip(round(x))` 一条？它们本来就这么写的，那 `div` 和 `mul` 能合并吗？

**答案**：`div`/`mul` 不能合并——归一化（除）与反量化（乘）之间夹着 round 与 clip 这两个非线性操作，代数上无法约掉。这也解释了 4.3 中梯度为什么会相消：**数值路径**上 \(s'\) 一除一乘被非线性隔开无法约去，但**梯度路径**上线性因子的偏导数恰好互为倒数。

**练习 3**：文档说 min_v/max_v「必须为浮点型整数，小数位全 0」，为什么有这个奇怪的要求？

**答案**：这是给 4.3.3 讲的「整数差掩码技巧」铺路——反向 kernel 用 `rounded - clamped` 是否为 0 判断元素是否在界内，该技巧只在这两个量都是整数值时才严格成立。文档约束与实现技巧是配套的。

### 4.3 per_tensor 反向 kernel：梯度相消与乘除双路径

#### 4.3.1 概念说明

反向 kernel `ai_infra_qat_symmetric_per_tensor_backward` 接收上游梯度 \(g = \partial L/\partial W_q\)，输出两路梯度：\(\partial L/\partial W\) 与 \(\partial L/\partial s\)。文档 [qat_ops.md:L173-L229](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L173-L229) 分五步推导，先看最重要的结果。

**结论一（grad_weight 的 scale 相消）**：沿链路 \(\text{out} = W_{\text{clamp}} \cdot s'\)、\(W_{\text{clamp}} = \text{STE}(W/s')\) 逐段求导：

\[ \frac{\partial L}{\partial W} = \underbrace{g}_{\text{上游}} \cdot \underbrace{s'}_{\text{反量化乘} } \cdot \underbrace{\mathbf{1}_{\text{mask}}}_{\text{clamp}} \cdot \underbrace{\frac{1}{s'}}_{\text{归一化除}} = g \odot \mathbf{1}_{\text{mask}} \]

STE 把 round 的导数当作 1 之后，\(s'\) 与 \(1/s'\) **精确相消**。所以 kernel 里 grad_weight 就是一行 `grad * mask`，不乘也不除任何 scale——这不是省事，是数学上就该如此。

**结论二（grad_scale 的双路径）**：\(s'\) 在前向出现两次（归一化的分母、反量化的乘子），两处贡献相加：

\[ \frac{\partial L}{\partial s} = \Big[\underbrace{\text{sum}(g \odot W_{\text{clamp}})}_{\text{乘法路径}} \ -\ \underbrace{\tfrac{1}{s'}\,\text{sum}(g \odot \mathbf{1}_{\text{mask}} \odot W)}_{\text{除法路径}}\Big] \cdot \mathbf{1}_{s > \varepsilon} \]

乘法路径来自 \(\text{out} = W_{\text{clamp}} \cdot s'\) 对 \(s'\) 的直接偏导；除法路径来自 \(W/s'\) 的 \(-W/s'^2\)，提出 \(1/s'\) 后即得。最后的 \(\mathbf{1}_{s>\varepsilon}\) 是 Step 1 防零保护的掩码。

> **读文档时的一个坑**：文档 [qat_ops.md:L221-L223](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L221-L223) 的「最终合并公式」把 grad_W 写成 \(\partial L/\partial W_q \odot \text{mask} \cdot 1/s'\)，**漏掉了反量化步骤的 \(s'\) 因子**。按同文档 L177–L206 的分步推导走到底，\(s'\) 与 \(1/s'\) 相消，结果是 \(g \odot \text{mask}\)——这也正是 kernel 实际实现（[L514](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L514)）与 ST 反向测试（autograd 全自动求导）共同验证的答案。文档一行摘要与分步推导矛盾时，以分步推导 + 代码 + 测试三方一致为准。

#### 4.3.2 核心流程

```text
kernel 入口: grad_out[N,M], weight[N,M], scale[1,1], 两个输出, 标量 eps/min_v/max_v
1. 循环外: s' = max(scale, eps); scale_mask = (scale >= eps)
2. 循环外: grad_scale_acc = 0 (FP32 全局累加器)
3. loop_unroll(0, n, [512, 32, 8]) 对每个 tile:
   a. 重算前向: normalized → rounded → clamped   (无缓存设计)
   b. 整数差技巧生成 mask (界内 1 / 界外 0)
   c. grad_weight = grad_out * mask → 写回
   d. 乘法路径: sum(grad_out * clamped, dim=1)
      除法路径: sum(grad_out * mask * (-W/s'), dim=1)
   e. 两路径沿 dim=0 再求和 → 累加进 grad_scale_acc
4. 循环外: grad_scale_acc * scale_mask → 写回 [1,1]
```

两个结构性要点：

- **无缓存设计**：反向不保存前向的任何中间量，进场后用 weight/scale **重算** normalized/rounded/clamped。这是空间换时间的反向选择——省下中间量的显存，多付一次前向计算（向量算子便宜，划算）。
- **全局累加器**：per_tensor 的 grad_scale 是 (1,1) 标量，必须把**所有 tile** 的贡献累加起来，所以累加器在循环外初始化、循环内 `[:]` 就地累加。也正因存在这个跨迭代的可变状态，该 kernel 把 `combine_axis` 设为 `False`（[L469](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L469)）——与所有前向 kernel 的 `True` 形成对照，避免编译器把含跨迭代依赖的循环做激进合并变换。

#### 4.3.3 源码精读

反向 kernel 位于 [ai_infra_pypto_qat.py:L453-L542](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L453-L542)，wrapper 在 [L545-L559](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L545-L559)。

- [L453-L458](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L453-L458)：装饰器。注意 `pass_options={"vec_nbuffer_setting": {-1: 4}}` —— per_tensor 反向给最后一维配了 **4** 份双缓冲（per_channel 反向只有 2 份），因为该 kernel 沿 M 的归约链更长，多缓冲可以拉深流水。
- [L459-L468](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L459-L468)：签名——两个输入张量 + scale + **两个输出张量**（`grad_weight_out` 形状同 weight、`grad_scale_out` 形状 [1,1]）。梯度输出也走目标传递风格，由 wrapper 预分配。
- [L475-L483](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L475-L483)：循环外三件事——scale 保护、`ge` + `where` 生成 scale 掩码（[L479-L480](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L479-L480)）、`pypto.full([1,1], 0.0, DT_FP32)` 初始化全局梯度累加器（[L483](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L483)）。
- [L498-L502](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L498-L502)：**重算前向**——expand_clone、div、round、clip，与前向 kernel 逐行对应。
- [L504-L511](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L504-L511)：**整数差掩码技巧**，本 kernel 最精巧的五行：

```python
diff = pypto.sub(rounded, clamped)      # 两者都是整数值浮点
abs_diff = pypto.abs(diff)              # 界内为 0, 界外 >= 1
out_of_bounds = pypto.clip(abs_diff, 0.0, 1.0)   # 0 或 1
neg_out_of_bounds = pypto.mul(out_of_bounds, -1.0)
mask_float = pypto.add(neg_out_of_bounds, 1.0)   # 1 或 0
```

  原理：`rounded` 是 round 的输出（整数值），`clamped` 是它被 clip 到**整数边界**后的值，所以 `diff` 只能是 0（界内）或绝对值 ≥ 1（界外）——`clip(abs, 0, 1)` 就把它变成干净的 0/1 指示符。源码注释（[L504-L506](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L504-L506)）点名了动机：**规避 where/比较类操作**，只用减、绝对值、clip、乘、加这些纯向量算术就能生成掩码。这也解释了 4.2.5 练习 3 的文档约束。
- [L513-L516](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L513-L516)：`grad_weight = grad_out * mask`——4.3.1 结论一的落地，**没有任何 scale 因子**。
- [L519-L521](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L519-L521)：**乘法路径** `sum(g * clamped, dim=1)`。
- [L523-L528](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L523-L528)：**除法路径** `g * mask * (-W / s')` 再沿 dim=1 求和——注意负号藏在 `mul(weight, -1.0)` 里。
- [L530-L532](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L530-L532)：归约方向的**现场换刀**——`set_vec_tile_shapes(512, 1)` 后沿 `dim=0` 再求和。前一步的 tile 是 (4, m) 适合行内归约，这一步要沿列归约，改成 (512, 1) 的长条 tile 让归约指令满载。这就是 u7-l1 讲过的「`set_vec_tile_shapes` 可在 kernel 中途按归约模式重设」的真实用例。
- [L534-L537](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L534-L537)：两路径相加，`grad_scale_acc[:] = add(acc, tile)` 就地累加——跨迭代状态。
- [L539-L542](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L539-L542)：出循环后乘 scale 掩码、转 BF16、写回 [0,0]。

ST 侧的对拍在 [test_ai_infra_qat_symmetric_per_tensor_backward.py:L57-L69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor_backward.py#L57-L69)：让 weight/scale `requires_grad_(True)`，用 [utils.py:L387-L467](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L387-L467) 的 `backward_test_autograd` 对 detach 版 golden 跑真 autograd，再与 kernel 输出的两路梯度做三方精度比较——kernel 手工推导的梯度公式由此获得全自动微分的背书。

#### 4.3.4 代码实践

**实践目标**：手工实现「乘法路径 + 除法路径」的 grad_scale 公式，与 torch autograd 的结果对拍，验证 4.3.1 结论二。

**操作步骤**（示例代码）：

```python
# 示例代码：验证 grad_scale 双路径公式（CPU 即可运行）
import torch

torch.manual_seed(0)
W = (torch.randn(64, 256, dtype=torch.float64) * 40).requires_grad_(True)
s = torch.tensor([[0.35]], dtype=torch.float64).requires_grad_(True)
eps, min_v, max_v = 1e-4, -128.0, 127.0

sp = torch.where(s > eps, s, eps)
wn = W / sp
cl = torch.clamp(wn + (wn.round() - wn).detach(), min_v, max_v)
out = cl * sp
g = torch.randn_like(out)                       # 随机上游梯度
out.backward(g)

mask = ((wn.round() >= min_v) & (wn.round() <= max_v)).double()
manual_gw = g * mask
manual_gs = ((g * cl).sum() - (1 / sp) * (g * mask * W).sum()) * (s > eps).double()

print("grad_weight 一致 :", torch.allclose(W.grad, manual_gw))
print("grad_scale  一致 :", torch.allclose(s.grad, manual_gs))
```

**需要观察的现象**：两行都打印 `True`。可以把 `s` 改成 `torch.tensor([[1e-6]])`（小于 eps）再跑——此时 scale 掩码为 0，`s.grad` 应变成 0，而 `W.grad` 不变（防零保护生效后 \(s'=\varepsilon\)，前向与 grad_weight 都不受影响）。

**预期结果**：`True` / `True`；改小 scale 后第二个仍为 `True` 且 `s.grad` 数值为 0。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 grad_weight 恰好等于 \(g \odot \text{mask}\)，连 \(1/s'\) 都不需要乘？

**答案**：前向中 W 先除以 \(s'\)（归一化）最后又乘回 \(s'\)（反量化），中间隔着的 round（STE 视为恒等）与 clamp（区间内恒等）对区间内元素整体也是恒等。对 W 求偏导时 \(s' \cdot (1/s') = 1\) 精确相消，只剩 clamp 的掩码。区间外元素被 clip 钉死在边界，对 W 的扰动不影响输出，梯度为 0。

**练习 2**：反向 kernel 为什么选择「重算前向」而不是「保存前向中间量」？

**答案**：见 [qat_ops.md:L784-L789](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L784-L789)（无缓存设计）：重算只需要 weight 和 scale 本身（它们本来就要传进反向），而保存 normalized/rounded/clamped 等中间量要多占一份与 weight 同尺寸的显存。重算成本是几次廉价的向量算子，显存开销却是 N×M 级别的——对 Embedding/Lm Head 这种大权重场景，空间比时间贵。对比第五单元 Sinkhorn 的「落盘中间量给反向复用」（u5-l1），两种策略各有适用面：这里中间量可以廉价重算，Sinkhorn 的迭代历史无法廉价重算。

**练习 3**：整数差掩码技巧如果用在**非对称**算子的反向（u7-l3 预告）里，还成立吗？

**答案**：成立但有陷阱——非对称算子的 clip 边界是 \(\pm\text{clip\_val}\)（0.99）而非整数，`rounded` 也移过 0.5 的 shift，直接相减不再保证整数差。非对称反向（[L165-L171](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L165-L171)）改用「差值放大 1e15 倍再 clip 到 [0,1]」的技巧：`clip(abs(diff) * big_number, 0, 1)`，任何非零差被放大后都饱和到 1。两种技巧解决同一个问题，边界性质不同选型不同。

### 4.4 per_channel 前向与反向：scale 形状差异的连锁影响

#### 4.4.1 概念说明

`ai_infra_qat_symmetric_per_channel` 是**通道级**对称量化：scale 形状为 (N, 1)，第 i 行权重共享 \(s_i\)，适用于 Lm Head 层——词表各行（对应不同 token 的输出）数值分布差异大，逐通道 scale 能显著降低量化误差（文档对比表见 [qat_ops.md:L501-L510](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L501-L510)）。

公式与 per_tensor 完全同构，只是 scale 带上了通道下标 \(i\)（文档 [qat_ops.md:L322-L352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L322-L352)）：

\[ s'_i = \max(s_i, \varepsilon), \qquad W_{\text{norm}}[i,j] = \frac{W[i,j]}{s'_i}, \qquad W_q[i,j] = W_{\text{clamp}}[i,j] \times s'_i \]

scale 形状从 (1,1) 变成 (N,1)，看似只改了一个维度，实际在 kernel 上引发**四连锁**：

| 影响点 | per_tensor | per_channel |
|---|---|---|
| scale 的类型标注 | `[1, 1]` 静态形状 | `[pypto.DYNAMIC, 1]` 动态形状 |
| 广播方式 | `expand_clone` 把标量物化成 `[tile_n, 1]` | 直接 `view` 出 `[tile_n, 1]` 的块，天然形状匹配 |
| 循环不变量外提 | 可以（scale 保护提到循环外） | 不可以（每个 tile 的 scale 不同，块内各自做） |
| grad_scale 归约 | 全 N×M 求和 → 全局累加器 + `combine_axis=False` | 仅沿 M 求和 → 无累加器，逐块 assemble |

第四点最关键：per_channel 的 grad_scale 形状是 (N, 1)，每个通道一行独立归约，**行与行之间不需要累加**，所以反向不需要跨迭代状态，每个 tile 算完直接写回自己那一段。

#### 4.4.2 核心流程

前向（[L248-L281](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L248-L281)）：

```text
1. n, m = weight.shape; set_vec_tile_shapes(32, 512)
2. loop_unroll(0, n, [512, 32, 8]):
   a. view 取 weight[tile_n, m] 与 scale[tile_n, 1], 各自 cast FP32
   b. s' = max(scale_tile, eps)              # 块内做, 无法外提
   c. normalized = weight / s'               # [tile_n,m] / [tile_n,1] 广播
   d. rounded → clamped → output = clamped * s'
   e. assemble 写回
```

反向（[L304-L376](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L304-L376)）：

```text
1. set_vec_tile_shapes(4, min(m, 4096))       # M 超过 4096 时按 4096 切
2. loop_unroll(0, n, [512, 32, 8]) 每 tile:
   a. 重算前向 (normalized/rounded/clamped)
   b. 整数差掩码 + scale 掩码 (ge + where)
   c. grad_weight = g * mask → assemble
   d. 乘法路径 sum(g*clamped, dim=1) + 除法路径 sum(g*mask*(-W/s'), dim=1)
   e. 两路径相加, 乘 scale 掩码 → assemble 到 grad_scale_out[tile_n, 1]
```

#### 4.4.3 源码精读

**前向** [ai_infra_pypto_qat.py:L243-L281](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L243-L281)：

- [L250](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L250)：`scale: pypto.Tensor([pypto.DYNAMIC, 1], pypto.DT_BF16)`——动态形状的通道向量。
- [L268-L271](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L268-L271)：weight 与 scale **在循环内各自 view 出对应块**——per_channel 下第 n_offset 块的 scale 就是 `[n_offset : n_offset+tile_n]` 那一段，天然与权重块对齐，不需要 `expand_clone`。
- [L273-L277](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L273-L277)：四步公式与 per_tensor 的 [L429-L432](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L429-L432) 逐行同构——`[tile_n, m]` 与 `[tile_n, 1]` 相除由 pypto 按广播语义展开。注意这五行**在循环内**，scale 保护无法外提。
- wrapper [L284-L295](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L284-L295)：同样五个参数无默认值、无校验。

**反向** [L298-L376](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L298-L376)：

- [L330](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L330)：`scale_tile = pypto.view(scale, [tile_n, 1], [n_offset, 0])`——反向同样按块取 scale。
- [L336-L338](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L336-L338)：块内的 scale 保护与 `ge`/`where` 掩码——对比 per_tensor 把它们放在循环外（[L475-L480](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L475-L480)），这是「循环不变量能否外提」的直接对照。
- [L341-L343](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L341-L343)：重算前向三件套。
- [L345-L352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L345-L352)：整数差掩码，与 per_tensor 反向 [L504-L511](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L504-L511) 完全相同的一段（两处复制粘贴，维护时需双侧同步）。
- [L354-L357](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L354-L357)：`grad_weight = g * mask`——scale 相消结论与粒度无关，per_channel 同样成立。
- [L360-L369](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L360-L369)：乘法路径 `sum(g * clamped, dim=1)`、除法路径 `g * mask * (-W/s')` 后 `sum(dim=1)`——**只沿 M 归约**，得到 `[tile_n, 1]`。
- [L371-L376](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L371-L376)：两路径相加、乘 scale 掩码、`assemble` 写回 `[n_offset, 0]` 起的 `[tile_n, 1]` 段——**没有全局累加器、没有 `[:]` 就地更新、没有 dim=0 归约**。与 per_tensor 反向的 [L530-L542](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L530-L542) 对照着读，scale 形状的差异如何决定归约结构一目了然。

ST 覆盖：`tests/st/` 下 per_channel 有 [test_ai_infra_qat_symmetric_per_channel.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py) 与 `test_ai_infra_qat_symmetric_per_channel_backward.py` 两件（u7-l4 精讲），参数化用例的 N 高达 153376（Lm Head 的真实词表规模量级）。

#### 4.4.4 代码实践

**实践目标**：用 CPU 上的 per_tensor 参考实现，按行独立调用，验证「per_channel ≡ 逐行 per_tensor」这一等价关系，从而确认两种粒度的公式同构、只差 scale 的形状。

**操作步骤**（示例代码）：

```python
# 示例代码：验证 per_channel 与逐行 per_tensor 的等价性（CPU 即可运行）
import torch

def qat_per_tensor_row(W, s, eps, min_v, max_v):     # 一行权重的 per_tensor 前向
    sp = torch.where(s > eps, s, eps)
    return torch.clamp((W / sp).round(), min_v, max_v) * sp

def qat_per_channel(W, S, eps, min_v, max_v):        # S 形状 (N,1)
    sp = torch.where(S > eps, S, eps)                 # 逐通道保护
    return torch.clamp((W / sp).round(), min_v, max_v) * sp

torch.manual_seed(0)
N, M = 6, 256
W = torch.randn(N, M, dtype=torch.float64) * 30
S = torch.rand(N, 1, dtype=torch.float64) * 0.4 + 0.01

whole = qat_per_channel(W, S, 1e-4, -128.0, 127.0)
rowby = torch.cat([qat_per_tensor_row(W[i:i+1], S[i:i+1], 1e-4, -128.0, 127.0)
                   for i in range(N)], dim=0)
print("per_channel == 逐行 per_tensor :", torch.equal(whole, rowby))
```

**需要观察的现象**：打印 `True`。同时留意广播写法 `W / sp` 中 `[N,M] / [N,1]` 直接广播，无需 expand——这正是 kernel 里不需要 `expand_clone` 的 Python 侧对应物。

**预期结果**：`True`。若把某一行 scale 改成 `1e-6`（低于 eps），该行会以 \(\varepsilon\) 为有效 scale 计算，其他行不受影响——逐通道保护的独立性。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：per_channel 反向为什么不需要 `combine_axis=False` 和全局累加器？

**答案**：grad_scale 形状是 (N, 1)，第 i 行的梯度只依赖第 i 行的 weight/scale/grad_output，沿 M 一维归约即可；每个 unroll tile 写回互不重叠的 `[tile_n, 1]` 段，tile 间无数据依赖，循环体是纯函数。per_tensor 的 grad_scale 是全矩阵求和成的标量，所有 tile 都要贡献到同一个 [1,1]，才需要跨迭代累加器，进而需要禁用 combine_axis 以防编译器对含依赖的循环做合并改写。

**练习 2**：两个反向 kernel 的 `set_vec_tile_shapes` 策略不同——per_channel 全程 `(4, min(m, 4096))`，per_tensor 中途切到 `(512, 1)`。为什么？

**答案**：per_channel 只做 dim=1（行内沿 M）归约，(4, m) 的扁 tile 一次吃满一行；per_tensor 做完 dim=1 后还要做 dim=0（跨行）归约，长条形的 (512, 1) tile 让列方向归约的向量指令满载。tile 形状服务于归约方向，这是 pypto 里「按归约模式定制 tile」的教科书示范。

**练习 3**：文档对比表（[qat_ops.md:L501-L510](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L501-L510)）说 per_channel「量化精度较高」，代价是什么？

**答案**：显存与维护成本——scale 及其梯度、优化器状态从 1 个标量变成 N 个值；同时 per_channel 的梯度是 (N,1) 向量，反传给优化器的通信量也变大。此外从工程角度，本仓库里 per_channel 的前向无法做 scale 的循环不变量外提、反向多了块内掩码计算，kernel 计算量也略高。精度换资源，粒度越细越是如此（per_group 更甚）。

## 5. 综合实践

把本讲全部知识点串成一个任务：**用 torch 的 detach 技巧完整复现 symmetric per_tensor 的 STE 前向与反向，验证「梯度直通」，再为算子补一个文档约束的参数校验函数**——后者填补的是真实空缺：仓库里的 wrapper（[L439](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L439)、[L545](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L545)）完全没有校验逻辑，约束（[qat_ops.md:L97-L102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L97-L102)）只写在文档里，传错形状只会得到错误结果或运行期崩溃，不会有清晰报错。对比第 2 单元 ascendc 算子 tiling 层动辄 30 处 `OP_CHECK_IF` 的防御式风格，pypto 侧目前是「全靠调用方自觉」。

**任务拆解**（示例代码，除第三步外均为纯 CPU 可运行）：

**第一步：实现 detach 版参考实现并验证梯度直通**（对应 4.1/4.3 的公式）：

```python
# 示例代码：STE 前向 + 反向 的完整 torch 复现
import torch

class SymmetricQatPerTensor(torch.autograd.Function):
    @staticmethod
    def forward(ctx, weight, scale, eps, min_v, max_v):
        sp = torch.where(scale > eps, scale, eps)
        ctx.save_for_backward(weight, sp)
        ctx.eps, ctx.min_v, ctx.max_v = eps, min_v, max_v
        return torch.clamp((weight / sp).round(), min_v, max_v) * sp

    @staticmethod
    def backward(ctx, grad_out):
        weight, sp = ctx.saved_tensors
        wn = weight / sp
        mask = ((wn.round() >= ctx.min_v) & (wn.round() <= ctx.max_v)).to(weight.dtype)
        grad_w = grad_out * mask                                   # 结论一: scale 相消
        grad_s = ((grad_out * torch.clamp(wn.round(), ctx.min_v, ctx.max_v)).sum()
                  - (1 / sp) * (grad_out * mask * weight).sum()) \
                 * (sp > ctx.eps).to(weight.dtype)                 # 结论二: 双路径
        return grad_w, grad_s, None, None, None
```

先用 `torch.autograd.gradcheck` 的思路手工验证：小规模 (8, 256) 输入下，用 4.3.4 的 detach 版 autograd 对拍上面这个 Function 的输出，`grad_w`、`grad_s` 都应 `allclose`；再取上游梯度为全 1、全部元素都在界内的输入（比如把 scale 调大到 `10.0`），观察 `grad_w` 恰为全 1——**这就是「梯度等于 1（直通）」的直接证据**。

**第二步：解释文档约束**。`M ∈ [128, 3072]` 且被 128 整除（[qat_ops.md:L99](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L99)）。从源码可确认：M 是向量 tile（`set_vec_tile_shapes(32, 512)`）的完整搬运维度、kernel 无 M 向尾块逻辑，128 对齐保证任意 unroll 组合下块都是满块；N 维有 [512, 32, 8] 三级降级处理任意余量，所以约束只压在 M 上。3072 上限的准确原因源码未说明（推测与 UB/寄存器预算相关，待确认）。把这个分析写成注释放进第三步的校验函数里。

**第三步：写校验函数**（可放进 wrapper 开头）：

```python
# 示例代码：per_tensor 对称 QAT 的参数校验（建议加在 wrapper 入口）
def _check_symmetric_qat_inputs(weight, scale, eps, min_v, max_v, name="per_tensor"):
    if weight.dim() != 2:
        raise ValueError(f"{name}: weight 必须是 2 维 (N, M), 实际 {tuple(weight.shape)}")
    n, m = weight.shape
    if not (128 <= m <= 3072):
        raise ValueError(f"{name}: M 必须在 [128, 3072], 实际 {m}")
    if m % 128 != 0:
        raise ValueError(f"{name}: M 必须被 128 整除, 实际 {m}")
    expected_scale_shape = (1, 1) if name == "per_tensor" else (n, 1)
    if tuple(scale.shape) != expected_scale_shape:
        raise ValueError(f"{name}: scale 形状应为 {expected_scale_shape}, 实际 {tuple(scale.shape)}")
    if weight.dtype != torch.bfloat16 or scale.dtype != torch.bfloat16:
        raise TypeError(f"{name}: 输入必须为 BF16, 实际 {weight.dtype}/{scale.dtype}")
    if not (0.0 < eps < 1.0):
        raise ValueError(f"{name}: eps 应在 (0, 1), 实际 {eps}")
    if not (min_v < max_v):
        raise ValueError(f"{name}: 要求 min_v < max_v, 实际 {min_v} / {max_v}")
    if float(min_v) != int(min_v) or float(max_v) != int(max_v):
        raise ValueError(f"{name}: min_v/max_v 必须为浮点型整数(小数位全 0), "
                         f"否则反向的整数差掩码技巧不成立")
```

**验证方式**：
- 合法输入 `(N=64, M=256, scale=(1,1))` → 通过；
- `M=200`（不整除）、`M=128000`（超上限）、`scale` 形状 `(N,1)`（粒度用错）、`min_v=-128.5`（非整数边界）→ 各自抛出对应 `ValueError`。
- 把校验函数接入 `ai_infra_qat_symmetric_per_tensor` 的 wrapper 并在 NPU 环境重跑 [test_ai_infra_qat_symmetric_per_tensor.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_tensor.py) 的参数化用例（N=153376, M=2048, bit=8），应全部通过——说明校验没有误伤合法输入。NPU 侧运行**待本地验证**。

## 6. 本讲小结

- **对称 QAT 前向是四步公式链**：\(s'=\max(s,\varepsilon)\) 防零 → \(W/s'\) 归一化 → round+clamp 伪量化 → \(\times s'\) 反量化；STE 用 `x + (x.round() - x).detach()` 让不可导的 round 在反向「假装恒等」。
- **两条反向结论**：STE 之下 grad_weight 的 \(s'\) 与 \(1/s'\) 精确相消，kernel 里就是 `grad * mask` 一行；grad_scale 由乘法路径 \(\text{sum}(g \odot W_{\text{clamp}})\) 与除法路径 \(-\tfrac{1}{s'}\text{sum}(g \odot \text{mask} \odot W)\) 相加再乘 scale 掩码。文档「最终合并公式」一行漏了 \(s'\) 因子，以分步推导 + kernel + ST autograd 三方一致为准。
- **掩码的数值技巧**：round 输出与整数边界 clip 结果之差必为整数，`clip(abs(diff), 0, 1)` 即 0/1 掩码——纯向量算术替代比较/where，这也是「min_v/max_v 必须为浮点型整数」约束的真正原因。
- **per_tensor vs per_channel 的差异是 scale 形状的连锁反应**：(1,1) 标量可外提保护、需 `expand_clone` 物化、grad_scale 要跨 tile 全局累加（`combine_axis=False`）；(N,1) 向量块内天然对齐、只沿 M 归约、无累加器。
- **反向采用无缓存设计**：不落盘前向中间量，进场用 weight/scale 重算——对 Embedding/Lm Head 的大权重场景，省下的 N×M 显存比几次廉价向量重算更值。
- **工程细节**：pypto 的 wrapper 完全不做参数校验（约束只在文档），且代码签名没有文档所示的默认参数——调用必须全量显式传参；文档与代码不一致时以代码为准。

## 7. 下一步学习建议

下一讲 **u7-l3（QAT 非对称分组量化：前向与反向 kernel）** 将本讲的框架推进到最复杂的一族：scale 从 (N,1) 变成 (N·M/group_size, 1)、引入第三个可学习参数 **offset**、量化范围从整数边界变成 \(\pm 0.99\) 的连续 clip_val、round 前后多了 0.5 的 **shift**——整数差掩码技巧随之升级为「差值放大 1e15 倍再饱和」的变体（可先读 [ai_infra_pypto_qat.py:L163-L174](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L163-L174) 对照本讲 4.3.3）。反向也多出第三路梯度 grad_offset。之后再进入 u7-l4 看 pypto 的 ST 测试体系如何为这六个算子做精度验证。

建议同步重读的源码：`ai_infra_pypto_qat.py` 的 L21–L100（非对称前向，与本讲 L401–L436 对照「同一编程模型、不同量化算法」的写法差异），以及 `tests/utils.py` 的 `backward_test_autograd`（本讲多次引用的三方对拍入口）。
