# QAT 非对称分组量化：前向与反向 kernel

## 1. 本讲目标

本讲是 pypto QAT 系列的第三讲。在 u7-l2 对称量化（per_tensor / per_channel）的基础上，精读算法上最复杂的一对算子：`ai_infra_qat_asymmetric_per_group`（非对称分组量化前向）与 `ai_infra_qat_asymmetric_per_group_backward`（反向）。

学完本讲你应该能够：

1. 解释 offset（零点偏移）、n_levels（量化台阶数）、shift（半格平移）在非对称量化中的几何含义，说出完整公式链「减 offset → 归一化 → clip → round → 反归一化 → 加 offset」每一步的作用。
2. 读懂前向 kernel 如何用 `pypto.loop_unroll(unroll_list=[512, 256])` 对「组」这一维度做大块优先、余量降级的循环展开，并解释该策略如何影响生成代码。
3. 推导并验证反向的三路梯度公式：grad_weight（界内直通）、grad_offset（界外全导到 offset）、grad_scale（LSQ+ 链式），并把每条公式对应到 kernel 的具体代码行。
4. 看懂反向 ST 测试如何用一份 torch golden 同时做 BF16-autograd、FP64-autograd 与 NPU kernel 的三方梯度对比。

## 2. 前置知识

本讲默认你已读过 u7-l1（pypto 编程模型）与 u7-l2（对称 QAT 算子）。快速回顾并补充新概念：

**回顾（u7-l1 已建立）：**

- `@pypto.frontend.jit` 装饰器把带类型标注的 Python 函数即时编译为 NPU 设备代码；带 `pypto.Tensor(...)` 标注的参数是设备张量，无标注参数（如 `eps`、`n_levels`）是 Host 侧预计算标量，编译期定死。
- 输出走「目标传递风格」：wrapper 用 `torch.empty` 分配输出后按位置传入 kernel，参数顺序是硬契约。
- 数据流三原语：`pypto.view`（取块）、`pypto.cast`（BF16 输入输出、FP32 内部计算）、`pypto.assemble`（镜像写回）。
- `pypto.loop_unroll` 按 unroll_list 大块优先、余量降级收尾；`pypto.set_vec_tile_shapes` 定制向量 tile 形状；`runtime_options` / `pass_options` 是编译期调优旋钮。

**回顾（u7-l2 已建立）：**

- QAT 前向四步：scale 防零保护 `max(s, eps)` → 归一化 → round+clamp 伪量化 → 反量化。
- round 不可导，用直通估计器 STE（`x + (round(x) - x).detach()`）让梯度直通。
- 界内掩码用「整数差技巧」（clip 差值放大后截断）以纯向量算术生成，避免分支。
- 反向采用无缓存设计：不保存前向中间量，反向时重算。

**本讲新概念：**

| 术语 | 含义 |
|------|------|
| 非对称量化 | 量化区间不必以 0 为中心，增加可学习零点 offset，适应偏斜的权重分布 |
| LSQ+ | Learned Step Size Quantization Plus，scale 与 offset 都可学习的 QAT 算法 |
| 分组量化（per_group） | 每 group_size（64/128/256）个连续元素一组，各自拥有独立的 scale 和 offset，粒度介于 per_tensor 与 per_channel 之间 |
| n_levels | \( 2^{(\text{bit}-1)} \)，归一化值的半幅刻度数 |
| shift（=0.5） | round 前减、round 后加的半格平移，把整数舍入格变成半整数格 |
| 三路梯度 | 反向一次输出 grad_weight、grad_scale、grad_offset 三个张量 |

对称与非对称的直观对比（摘自 docs）：

| 特性 | 对称量化（u7-l2） | 非对称量化（本讲） |
|-----|---------|-----------|
| 量化范围 | \([-Q, Q]\) | \([-Q, Q] + \text{offset}\) |
| 参数数量 | scale | scale + offset |
| 适用场景 | 权重分布对称 | 权重分布不对称 |
| 计算复杂度 | 较低 | 较高 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py) | 全部 6 个 QAT 算子的 kernel + wrapper。本讲只读 L15-L240（非对称前向 L15-L100、非对称反向 L103-L240）；L243 起是对称系列，属 u7-l2 内容 |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md) | 算子文档。非对称前向在 L514-L682（核心公式 L566-L620），反向在 L686-L860（梯度公式 L746-L782） |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py) | 前向 ST 测试：torch golden + 参数化用例 |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py) | 反向 ST 测试（本讲核心测试标本）：三方梯度对比 |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py) | 测试公共库：`create_input`、`forward_test`、`backward_test_autograd`、三方精度对比 |

注意：`pypto` 框架本身（`loop_unroll`、`view`、`assemble` 的实现）不在本仓库中，是外部依赖；仓库里只有使用它的算子代码。涉及框架内部行为时本讲以 u7-l1 建立的语义为准，并在必要处标注「待确认」。

## 4. 核心概念与源码讲解

### 4.1 非对称量化的公式链：offset、n_levels 与 shift 的几何含义

#### 4.1.1 概念说明

对称量化假设权重量化区间以 0 为中心，只需一个 scale。但真实权重分布常常偏斜（比如经过 ReLU 后全为正，或整体偏向一侧），硬套对称区间会浪费一半刻度。非对称量化增加一个可学习零点 offset：先把每个元素减去 offset，让「组内分布中心」移到 0，再按对称网格量化，最后加回 offset。这就是 LSQ+ 的核心思想——scale 管刻度粗细，offset 管刻度原点，两者都由反向传播学习。

三个参数的几何含义：

- **offset**：量化网格的原点平移。重建值永远落在 \(\text{offset} + \text{网格}\) 上，网格本身关于 offset 对称。
- **n_levels \(= 2^{(\text{bit}-1)}\)**：归一化值的半幅刻度数。归一化并 clip 到 \(\pm 0.99\) 后乘以 n_levels，数值进入 \(\pm 0.99 \cdot n_{\text{levels}}\) 的连续区间。
- **shift \(= 0.5\)**：round 前减、round 后加，把「四舍五入到整数格」变成「取最近半整数格」。它与 clip_val \(< 1\) 配合，恰好把可用重建格点数控制在 \(2^{\text{bit}}\) 个（见 4.1.4 实践验证）。

#### 4.1.2 核心流程

对每个组 \(g\)（每组 group_size 个元素），记 \(s' = \max(s, \varepsilon)\)（防零保护）、\(\alpha = s' \cdot n_{\text{levels}}\)，单个元素 \(w\) 的前向公式链：

\[
\begin{aligned}
\text{Step 1（防零）}&\quad s' = \max(s,\ \varepsilon) \\
\text{Step 2（减 offset）}&\quad W_{\text{shifted}} = w - \text{offset} \\
\text{Step 3（归一化）}&\quad u = \frac{W_{\text{shifted}}}{\alpha} \\
\text{Step 4（clip）}&\quad c = \mathrm{clip}(u,\ -\text{clip\_val},\ \text{clip\_val}) \\
\text{Step 5（进刻度+半格平移）}&\quad y = c \cdot n_{\text{levels}} - \text{shift} \\
\text{Step 6（round，STE）}&\quad r = \mathrm{round}(y) \\
\text{Step 7（平移还原）}&\quad r' = r + \text{shift} \\
\text{Step 8（反归一化）}&\quad q = \frac{r'}{n_{\text{levels}}} \\
\text{Step 9（加 offset）}&\quad o = q \cdot \alpha + \text{offset}
\end{aligned}
\]

要点：

- Step 2 与 Step 9 的 offset 一减一加：**界内元素在 STE 下前向近似恒等**（\(o \approx w\)），这是反向梯度异常简洁的根源（见 4.3）。
- Step 4 的 clip 制造了「界内 / 界外」两种区域，是反向掩码的来源。
- Step 5 的 \( \times n_{\text{levels}} - \text{shift}\) 与 Step 7 的 \(+\text{shift}\) 互为逆操作：round 的对象是刻度空间里的值，round 本身造成的偏差就是量化误差。

几何上（本讲分析，可在 4.1.4 验证）：以 bit=4、n_levels=8、clip_val=0.99 为例，\(y \in [-7.92, 7.92]\)，\(r = \mathrm{round}(y - 0.5) \in \{-8, \dots, 7\}\)，重建格点 \(r' \in \{-7.5, -6.5, \dots, 6.5, 7.5\}\)，恰 16 \(= 2^4\) 个，关于 0（即关于 offset）对称，且没有格点落在 offset 正上方。

#### 4.1.3 源码精读

docs 给出的核心公式与上面一一对应：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md:L566-L620](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L566-L620)：Step 1~Step 9 的官方公式表述，其中 Step 5 明确写作 \(\text{clamp}(\cdot) \times n_{\text{levels}} - \text{shift}\)，Step 6 写作 detach 形式的 STE。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md:L622-L635](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L622-L635)：分组示意——把 (N, M) 权重按 128 个元素切成 G 组，每组挂一个 scale[g] 与 offset[g]。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md:L637-L650](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L637-L650)：约束条件——group_size 只能取 64/128/256；weight 必须 2 维且 M∈[128, 3072] 被 group_size 整除；scale/offset 形状必须为 (N\*M/group_size, 1)；bit 只能取 2/3/4；BF16 输入输出、FP32 内部计算；芯片 A2/A3。

kernel 里的公式链实现（设备侧没有 round 的可导性问题，直接 `pypto.round`）：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L55-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L55-L70)：九步公式链逐行落地——`protected_scale`（Step 1）、`alpha`（Step 3 的分母）、`weight_shifted`（Step 2）、`weight_norm`（Step 3）、`weight_clipped`（Step 4）、`weight_scaled`/`weight_shifted2`（Step 5）、`weight_rounded`（Step 6）、`weight_unshifted`（Step 7）、`weight_denorm`（Step 8）、`output`（Step 9）。变量名与 docs 公式几乎逐字对应。

torch golden 中的 STE 写法（这是 round 可导性的关键一行）：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py:L65-L66](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L65-L66)：`weight_clipped = torch.clamp(...) * n_levels - shift` 之后，`weight_rounded = (weight_clipped.round() - weight_clipped).detach() + weight_clipped`——前向值等于 round 结果，梯度却绕过 round 直通，与 u7-l2 的 STE 手法一致。

#### 4.1.4 代码实践

**实践目标**：用 CPU 上的 torch 验证「clip_val=0.99 + shift=0.5 ⇒ 重建格点恰为 \(2^{\text{bit}}\) 个」这一几何论断。

**操作步骤**（示例代码，可在任何有 torch 的机器上运行，无需 NPU）：

```python
# 示例代码：验证非对称量化的重建格点数量
import torch

def recon_levels(bit, clip_val, shift=0.5, n_samples=100001):
    n_levels = 2 ** (bit - 1)
    s = torch.tensor([0.01])                      # 任取一个合法 scale
    alpha = s * n_levels
    w = torch.linspace(-0.2, 0.2, n_samples)      # 覆盖足够宽的输入
    u = w / alpha                                  # 减 offset(=0) 后归一化
    c = u.clamp(-clip_val, clip_val)              # clip
    y = c * n_levels - shift                       # 进刻度 + 半格平移
    r = torch.round(y)                             # round
    levels = torch.unique(r + shift)               # 重建格点（刻度空间）
    return levels

for bit in (2, 3, 4):
    lv = recon_levels(bit, 0.99)
    print(f"bit={bit}: 格点数={lv.numel()} (期望 {2**bit}), "
          f"范围=[{lv.min():.1f}, {lv.max():.1f}]")

lv100 = recon_levels(4, 1.0)                      # 对照：clip_val=1.0
print(f"clip_val=1.0 时格点数={lv100.numel()} (期望 17)")
```

**需要观察的现象**：bit=2/3/4 时打印的格点数分别是 4/8/16，格点为 \(-n_{\text{levels}}+0.5\) 到 \(+n_{\text{levels}}-0.5\) 的半整数；把 clip_val 改成 1.0 后格点数变为 17（出现 8.5 这个极端格点，且该点来自 round 在 7.5 平局上的银行家舍入行为）。

**预期结果**：验证「0.99 与 0.5 的组合把格点数精确控制在 \(2^{\text{bit}}\)」。torch 的 round 是 round-half-to-even（银行家舍入），极端值处的平局行为正是 clip_val 取 0.99 而非 1.0 的原因之一（本讲分析；pypto 设备侧 round 的舍入细节待确认，但不影响格点计数结论）。

#### 4.1.5 小练习与答案

**练习 1**：bit=4 时 n_levels 是多少？归一化值 clip 后再乘 n_levels，数值落在什么区间？

**答案**：\(n_{\text{levels}} = 2^{4-1} = 8\)；clip 到 \(\pm 0.99\) 后乘 8，数值落在 \([-7.92, 7.92]\)。

**练习 2**：如果把 shift 从 0.5 改成 0（其余不变），重建格点会变成什么？格点数如何变化？

**答案**：round 的对象变成 \(c \cdot n_{\text{levels}}\) 本身，格点变成整数 \(\{-8, \dots, 8\}\)（clip_val=0.99 时端点 8 取不到，实际 \(\{-7, \dots, 7\}\) 中可取的整数，共 15~17 个，取决于边界舍入）。核心变化是：格点关于 0 对齐且 0 本身成为格点（\(w = \text{offset}\) 的元素重建回 offset），而 shift=0.5 的设计让网格「错开半格」。

**练习 3**：为什么 wrapper 里 `neg_clip_val = -clip_val` 要在 Host 侧算好再传给 kernel，而不是让 kernel 自己取负？

**答案**：无 `pypto.Tensor` 标注的参数是编译期定死的 Host 标量，kernel 内不适合做标量运算（pypto 的原语面向张量）；把 `-clip_val`、`n_levels`、`shift` 都在 wrapper 预计算，可以让 kernel 内的 `pypto.clip(weight_norm, neg_clip_val, clip_val)` 直接使用常量，减少设备侧标量逻辑（见 [op_code/ai_infra_pypto_qat.py:L77-L99](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L77-L99) 的 inputs 列表）。

### 4.2 前向 kernel：loop_unroll 分组与九步计算链

#### 4.2.1 概念说明

分组量化的数据形状是 (num_groups, group_size)，例如 (1024×2048/128, 128) = (16384, 128)。一次把所有组搬进片上缓冲不现实，kernel 用 `pypto.loop_unroll` 沿组维度（第 0 维）分块：每次取 `unroll_length` 个组，组成一个 `tile_groups × group_size` 的二维 tile，整个 tile 走完「cast → 九步公式链 → cast → assemble」后进入下一块。

`unroll_list=[512, 256]` 的语义（沿用 u7-l1 的结论）：**大块优先、余量降级**——剩余组数足够就一次取 512 组；不足 512 时降级取 256；连 256 都不足时由框架生成的收尾块处理余量。这样每个展开块的形状在编译期尽量大且确定，既减少循环次数，又避免尾块按 512 展开造成一半无效计算。

#### 4.2.2 核心流程

```text
wrapper(weight, scale, offset, group_size, bit, eps, clip_val)
  ├─ Host 预计算标量: n_levels=2^(bit-1), shift=0.5, neg_clip_val=-clip_val
  ├─ torch.empty 分配 output_bf16
  ├─ weight / output 都 view 成 (-1, group_size)     # 分组视图
  └─ kernel(weight_grouped, scale, offset, output_grouped, 5 个标量)
       └─ for (g_offset, unroll_length) in loop_unroll(0, num_groups, 1, [512, 256]):
            ① view 取 [unroll_length, group_size] 的 weight/scale/offset 块
            ② cast 到 FP32
            ③ 九步公式链（4.1.2）
            ④ cast 回 BF16
            ⑤ assemble 写回 output_bf16 的 [g_offset, 0] 位置
```

注意 scale/offset 的形状是 (num_groups, 1)，与 weight 块的第 0 维严格对齐——分组量化的「组」是 weight 按 group_size 展平后的行。

#### 4.2.3 源码精读

**kernel 签名与编译旋钮**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L15-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L15-L31)：`@pypto.frontend.jit` 装饰器带 `stitch_function_max_num=64` 与 `vec_nbuffer_setting={-1: 2, -2: 1}` 两个编译旋钮；签名声明 4 个 BF16 张量（weight、scale、offset、output_bf16，前两个用 `[pypto.DYNAMIC, ...]` 表示第 0 维动态、第 1 维编译期未知）加 5 个无标注标量参数（eps、n_levels、neg_clip_val、clip_val、shift）。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L32-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L32-L36)：循环前准备——`set_operation_options(combine_axis=True)`、`unroll_list = [512, 256]`、从 scale.shape 取 num_groups、从 weight.shape 取 group_size、`set_vec_tile_shapes(128, 128)`。shape 在这里读取，说明 tile 形状按运行期实际维度组织。

**循环骨架与取数**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L38-L53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L38-L53)：`loop_unroll(0, num_groups, 1, name="LOOP_GROUPS", idx_name="g_offset", unroll_list=unroll_list)` 产出 `(g_offset, unroll_length)` 二元组；随后 `pypto.view(weight, [tile_groups, group_size], [g_offset, 0])` 取块，scale/offset 也取 `[tile_groups, 1]` 的对应块，三个张量全部 `cast` 到 FP32。`tile_groups = unroll_length` 这一行是「展开长度即块高」的直接证据。

**计算链与写回**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L55-L74](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L55-L74)：九步公式链（4.1.2 已逐行对照），最后 `cast` 回 BF16 并 `pypto.assemble(output_tile, [g_offset, 0], output_bf16)` 把结果镜像写回全局输出的对应位置。assemble 的偏移 `[g_offset, 0]` 与 view 的偏移一致，保证读写对齐。

**wrapper（Host 侧封装）**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L77-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L77-L100)：默认参数 `group_size=128, bit=4, eps=1e-4, clip_val=0.99`；Host 计算 `n_levels = 2 ** (bit - 1)`、`shift = 0.5`、`neg_clip_val = -clip_val`；`torch.empty` 分配输出；weight 与输出都 `view(-1, group_size)`；inputs 列表严格按 kernel 签名顺序排列（4 张量 + 5 标量）后 `kernel(*inputs)`；返回前把分组视图 `view` 回 weight.shape。

#### 4.2.4 代码实践

**实践目标**：搞清 `unroll_list=[512, 256]` 对具体 shape 生成的分块序列。

**操作步骤**（示例代码，纯 CPU 可运行）：

```python
# 示例代码：模拟 loop_unroll 的大块优先降级策略
def simulate_unroll(total, unroll_list):
    chunks, pos = [], 0
    while pos < total:
        take = next((u for u in unroll_list if total - pos >= u), total - pos)
        chunks.append(take); pos += take
    return chunks

for (N, M, g) in [(1024, 2048, 128), (768, 2048, 128), (300, 2048, 128)]:
    num_groups = N * M // g
    print(N, M, "num_groups =", num_groups, "->", simulate_unroll(num_groups, [512, 256]))
```

**需要观察的现象**：

- (1024, 2048, 128) → num_groups=16384 → `[512]*32`，无尾块；
- (768, 2048, 128) → num_groups=12288 → `[512]*24`，无尾块；
- (300, 2048, 128) → num_groups=4800 → `[512]*9 + [256]`，出现一次降级。

**预期结果**：每个展开块生成一个 `块高 × 128` 的二维 tile（最大 512×128=65536 元素），块内所有 FP32 中间张量（weight_norm、weight_clipped、weight_rounded 等约 10 个）按此大小分配片上缓冲。块越大循环次数越少、指令级并行度越高，但片上缓冲需求越大——这正是 unroll_list 允许降级的原因。（框架内部对「不足 256 的余量」的收尾方式待确认；上面模拟按「取剩余全部」处理。）

**延伸观察（可选，需 NPU + torch_npu + pypto 环境，待本地验证）**：把 [op_code/ai_infra_pypto_qat.py:L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L33) 的 `unroll_list` 改成 `[256]`，重新运行前向 ST 测试，观察精度是否仍 PASS、耗时是否变化。

#### 4.2.5 小练习与答案

**练习 1**：前向 kernel 里 `num_groups` 和 `group_size` 分别从哪个张量的 shape 读出？为什么不能从 weight 的原始 shape 直接读出 group_size？

**答案**：`num_groups = scale.shape[0]`、`group_size = weight.shape[1]`（L34-L35）。因为传进 kernel 的 weight 已经是 wrapper `view(-1, group_size)` 之后的分组视图，其第 1 维就是 group_size；原始 (N, M) 形状里 M 只是恰好等于 group_size 的倍数。

**练习 2**：`pypto.view(weight, [tile_groups, group_size], [g_offset, 0])` 的三个参数各是什么含义？

**答案**：依次是：源张量、块的形状 `[块高, 块宽]`、块在源张量中的起始偏移 `[行偏移, 列偏移]`。本例中块高 = 本次展开的组数、块宽 = group_size，行偏移 = g_offset、列偏移 = 0（每组总是整组取出，不切组内）。

**练习 3**：docs 约束「M∈[128, 3072] 且被 group_size 整除」中，M 上限 3072 主要限制的是什么资源？

**答案**：主要限制单组循环里 tile 的宽度。M 最大 3072、group_size 最大 256 时组内元素最多；而 tile 形状是 `unroll_length × group_size`，块高与块宽的乘积决定 FP32 中间张量的片上缓冲占用（本讲分析，具体上限由 pypto 编译器的缓冲预算决定，待确认）。

### 4.3 反向 kernel：前向重算、数值掩码与三路梯度

#### 4.3.1 概念说明

反向 kernel 的输入是上游梯度 grad_output 与前向的全部输入（weight、scale、offset），输出三路梯度。它采用与 u7-l2 对称反向相同的**无缓存设计**：不保存前向中间量，反向时把九步公式链重算一遍（round 结果是确定性的，重算无损），从而省下前向落盘中间量的显存。

与对称反向只算 grad_weight / grad_scale 两路不同，非对称反向多出 grad_offset 一路，且三路梯度有一个非常干净的结构：

- **界内元素**（clip 未生效）：前向在 STE 下近似恒等 \(o \approx w\)，所以对 w 的梯度直通、对 offset 的梯度恰好为 0（减 offset 与加 offset 相消）、对 alpha 的梯度为 \(q - u\)（重建格点与未截断归一化值之差）。
- **界外元素**（clip 生效）：输出被钉死在边界格点，对 w 的梯度为 0；输出对 offset 的导数为 +1（只有最后的 `+offset` 项还在动）；对 alpha 的梯度只剩 \(q\)（格点位置随 alpha 移动）。

#### 4.3.2 核心流程

记号沿用 4.1.2：\(u\)（未截断归一化）、\(c\)（clip 后）、\(r\)（round 后整数）、\(q = (r+\text{shift})/n_{\text{levels}}\)（反归一化格点）、\(o = q\alpha + \text{offset}\)。STE 即 \(\partial r/\partial y = 1\)。设 \(g = \partial L/\partial o\) 为上游梯度，掩码 \(\text{mask} = \mathbb{1}[\text{界内}]\)。

**界内（\(c = u\)）逐项求导：**

\[
\frac{\partial o}{\partial w}
= \alpha \cdot \frac{1}{n_{\text{levels}}} \cdot n_{\text{levels}} \cdot \frac{1}{\alpha} = 1,
\qquad
\frac{\partial o}{\partial \text{offset}} = -1 + 1 = 0
\]

\[
\frac{\partial o}{\partial \alpha}
= q + \alpha \cdot \frac{1}{n_{\text{levels}}} \cdot n_{\text{levels}} \cdot \left(-\frac{u}{\alpha}\right)
= q - u
\]

（第一个等式来自乘积规则：\(o = q\alpha + \text{offset}\) 对 alpha 求导有「\(q\) 本身也依赖 alpha」一项。）

**界外（\(c = \pm\text{clip\_val}\) 为常数）：**

\[
\frac{\partial o}{\partial w} = 0,
\qquad
\frac{\partial o}{\partial \text{offset}} = +1,
\qquad
\frac{\partial o}{\partial \alpha} = q
\]

**合并（两段公式统一成一个表达式，这是本算子最优雅的一处）：**

\[
\frac{\partial L}{\partial W} = g \odot \text{mask}
\]

\[
\frac{\partial L}{\partial \text{offset}}
= \sum_{j \in \text{group}} g_j \odot (1 - \text{mask}_j)
\quad\text{（沿 group 维求和，scale/offset 每组一个）}
\]

\[
\frac{\partial L}{\partial \alpha}
= \sum_{j \in \text{group}} g_j \odot \left(q_j - u_j \odot \text{mask}_j\right),
\qquad
\frac{\partial L}{\partial s}
= \frac{\partial L}{\partial \alpha} \cdot n_{\text{levels}} \cdot \mathbb{1}[s > \varepsilon]
\]

最后一式的 \(n_{\text{levels}}\) 来自 \(\alpha = s' n_{\text{levels}}\)，\(\mathbb{1}[s>\varepsilon]\) 来自防零保护：\(s \le \varepsilon\) 时 \(s' = \varepsilon\) 为常数，梯度为 0。

**kernel 执行流程（五段）：**

```text
for (g_offset, unroll_length) in loop_unroll(0, num_groups, 1, [512, 256]):
  1. Load & Cast     : view 取块，四个输入 cast 到 FP32
  2. Recompute       : 重算前向链到 weight_norm / weight_denorm
  3. Mask Generation : 界内掩码 mask、inv_mask、scale 合法掩码 scale_mask（纯向量算术）
  4. Gradient        : 三路梯度（grad_weight 逐元素；grad_offset / grad_scale 组内求和）
  5. Cast & Assemble : 三路结果 cast 回 BF16 分别写回
```

#### 4.3.3 源码精读

**签名：7 张量 + 5 标量**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L103-L127](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L103-L127)：反向 kernel 签名——输入 grad_output/weight/scale/offset 四个 BF16 张量，输出 grad_weight_out（分组视图）、grad_scale_out、grad_offset_out 三个 BF16 张量，加与前向完全相同的 5 个标量（eps、n_levels、neg_clip_val、clip_val、shift，保证重算前向参数一致）。编译旋钮与前向相同。

**第 1 段 取数（源码注释 `--- 1. ---`）**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L128-L146](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L128-L146)：同样的 `loop_unroll([512, 256])` 骨架；注意 `set_vec_tile_shapes(64, 128)` 在**循环内**且第一维取 64（前向是循环外取 128，见 [L36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L36)）——反向要同时持有重算链与多个掩码，中间张量约为前向两倍，缩小 tile 换缓冲（本讲分析）。`num_groups = scale.shape[0]`、`group_size = grad_output.shape[1]`。

**第 2 段 前向重算（`--- 2. ---`）**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L148-L161](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L148-L161)：从 protected_scale、alpha 一路重算到 `weight_norm`（注释明确标注「未截断」，即公式里的 \(u\)）与 `weight_denorm`（即 \(q\)）。与前向 L55-L70 逐行同构，只是少了最后的 `+offset`（反向不需要重建输出 o）。

**第 3 段 数值掩码（`--- 3. ---`）**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L163-L174](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L163-L174)：界内掩码生成——`diff = weight_norm - weight_clipped`，界内恒为 0、界外为「越界深度」；乘 `big_number = 1e15` 放大后 `clip(0, 1)` 得到 0/1 指示 `is_out`；`mask = 1 - is_out`。与 u7-l2 对称反向的「整数差技巧」同源，但那里 `rounded - clamped` 是整数（差至少为 1）可直接 clip，这里 `weight_norm - weight_clipped` 是连续值，必须先乘 1e15 放大（docs [L794-L800](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L794-L800) 有同款伪代码）。`one_tile_g_gs` 生成同形状全 1 张量算出 `inv_mask = 1 - mask`。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L176-L180](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L176-L180)：scale 合法掩码——`max(s - eps, 0)` 放大截断得 \(\mathbb{1}[s > \varepsilon]\)，用于把被防零保护接管的组的 grad_scale 清零。

**第 4 段 三路梯度（`--- 4. ---`）**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L184-L185](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L184-L185)：`grad_weight = grad_out * mask`——公式 \(\partial L/\partial W = g \odot \text{mask}\)。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L187-L189](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L187-L189)：`grad_offset = sum(grad_out * inv_mask, dim=1, keepdim=True)`——公式 \(\sum_j g \odot (1-\text{mask})\)，沿组内（dim=1）归约。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L191-L198](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L191-L198)：`term_diff = weight_denorm - weight_norm * mask`（统一表达式 \(q - u \odot \text{mask}\)，注释给出公式），组内求和得 grad_alpha，再乘 `n_levels`（alpha 对 s 的链式）、乘 `scale_mask`（防零保护屏蔽）得 grad_scale。

**第 5 段 写回（`--- 5. ---`）与 wrapper**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L200-L207](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L200-L207)：三路 FP32 结果 cast 回 BF16，分别 assemble 到 grad_weight_out / grad_scale_out / grad_offset_out 的 `[g_offset, 0]`。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L210-L240](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L210-L240)：wrapper——`torch.empty` 分配三路输出（shape 分别同 weight / scale / offset）；grad_output、weight、grad_weight_out 三个 (N, M) 张量 view 成分组视图；inputs 按 kernel 签名顺序排列（7 张量 + 5 标量）；返回 `(grad_weight, grad_scale, grad_offset)`。

**docs 公式对照**：[pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md:L746-L782](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L746-L782) 给出与本节相同的掩码 / grad_weight / grad_offset / grad_scale 公式（其 \(W_{\text{scaled}}\) 即本讲记号 \(u\)，指未截断归一化值）；[L784-L809](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L784-L809) 说明无缓存设计与掩码数值生成。

#### 4.3.4 代码实践

**实践目标**：用 torch autograd 数值验证 4.3.2 的三路梯度公式确实等于 STE 前向的自动微分结果（这一步也是看懂 4.4 ST 测试的前提）。

**操作步骤**（示例代码，纯 CPU 可运行，无需 NPU）：

```python
# 示例代码：autograd vs 手写三路梯度公式
import torch

def asymmetric_qat(weight, scale, offset, group_size, bit, eps, clip_val):
    """BF16 输入、FP32 计算的非对称 QAT 前向（含 STE），仿照 ST golden。"""
    n_levels = 2 ** (bit - 1); shift = 0.5
    w32, s32, o32 = weight.float(), scale.float(), offset.float()
    num_groups = weight.numel() // group_size
    protected = torch.where(s32 > eps, s32, torch.full_like(s32, eps))
    alpha = protected * n_levels
    w2d = w32.view(num_groups, group_size)
    shifted = w2d - o32
    u = shifted / alpha
    y = u.clamp(-clip_val, clip_val) * n_levels - shift
    r = (y.round() - y).detach() + y                 # STE
    q = (r + shift) / n_levels
    return (q * alpha + o32).view(weight.shape)

torch.manual_seed(0)
N, M, group, bit, eps, clip_val = 4, 256, 128, 4, 1e-4, 0.99
num_groups = N * M // group
weight = torch.randn(N, M, dtype=torch.bfloat16, requires_grad=True)
scale = (torch.rand(num_groups, 1) * 0.02 + 0.005).bfloat16().requires_grad_(True)
offset = torch.randn(num_groups, 1).bfloat16().requires_grad_(True)

out = asymmetric_qat(weight, scale, offset, group, bit, eps, clip_val)
g = torch.randn_like(out)
out.backward(g)

with torch.no_grad():                                # 手写公式（kernel 的镜像）
    n_levels = 2 ** (bit - 1)
    w2d, g2d = weight.float().view(num_groups, group), g.float().view(num_groups, group)
    s32, o32 = scale.float(), offset.float()
    protected = torch.where(s32 > eps, s32, torch.full_like(s32, eps))
    alpha = protected * n_levels
    u = (w2d - o32) / alpha
    q = ((u.clamp(-clip_val, clip_val) * n_levels - 0.5).round() + 0.5) / n_levels
    mask = (u.abs() <= clip_val).float()
    ref_gw = (g2d * mask)
    ref_go = (g2d * (1 - mask)).sum(dim=1, keepdim=True)
    ref_gs = ((g2d * (q - u * mask)).sum(dim=1, keepdim=True)
              * n_levels * (s32 > eps).float())

print("grad_weight :", torch.allclose(weight.grad.float(), ref_gw, rtol=1e-3, atol=1e-5))
print("grad_offset :", torch.allclose(offset.grad.float(), ref_go, rtol=1e-3, atol=1e-5))
print("grad_scale  :", torch.allclose(scale.grad.float(), ref_gs, rtol=1e-3, atol=1e-5))
```

**需要观察的现象**：三行都打印 True。若把 `mask` 的定义改成 `(u.abs() < clip_val)`（开区间）或去掉 grad_scale 的 `(s32 > eps)` 掩码，某些种子下会出现 False——前者是边界元素归属问题，后者在造一个 \(s \le \varepsilon\) 的组后必然 False。

**预期结果**：autograd（对 clamp 自动屏蔽界外、对 round 用 STE、对 where 自动选分支）与手写公式逐项一致，验证 4.3.2 的推导与 kernel 实现等价。精确到 BF16 输入的舍入，容差取 rtol=1e-3 量级；严格逐位一致需 FP64 输入（ST 测试正是这么做的，见 4.4）。以上结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么界内元素对 offset 的梯度恰好是 0，而界外元素是 +1？

**答案**：界内前向链是 \(o = q\alpha + \text{offset}\)，而 \(q\) 经 STE 对 \(u\) 的导数为 \(1/\alpha\)、\(u\) 对 offset 的导数为 \(-1/\alpha\)，两者相乘恰与末尾 `+offset` 的 +1 相消。界外时 clip 把 \(c\) 钉死为常数，\(q\) 不再依赖 offset，只剩 `+offset` 项，导数为 +1。

**练习 2**：反向 kernel 为什么重算到 `weight_denorm` 就停，不重算最后的 `+ offset`？

**答案**：三路梯度公式只用到 \(u\)（weight_norm）、\(q\)（weight_denorm）与掩码；输出 \(o\) 本身在反向里没有用处（上游梯度 grad_output 已由外部给出），重算 `+offset` 是无效计算。

**练习 3**：symmetric per_tensor 的反向必须 `combine_axis=False` 并维护全局累加器（u7-l2），而本算子反向 `combine_axis=True` 且无累加器。为什么？

**答案**：per_tensor 全模型只有一个 scale，各 tile 的 grad_scale 部分和必须跨 tile 累加，只能禁用轴合并并显式维护 FP32 累加器（见 [op_code/ai_infra_pypto_qat.py:L469-L537](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L469-L537)）；本算子每组一个 scale/offset，grad_scale/grad_offset 的归约只发生在组内（dim=1），tile 边界不切割组，单 tile 内即可完成归约并直接 assemble，因此可以合并轴、无需累加器。

### 4.4 反向 ST 测试：golden、autograd 与三路精度对比

#### 4.4.1 概念说明

ST（System Test）在真实 NPU 上跑 kernel 并与参考实现比精度。pypto 的 ST 测试结构是「一份 torch golden 函数 + 参数化用例 + 公共对比工具」：

- **golden**：与 kernel 公式完全相同的 torch 实现（含 STE），可切换 BF16（benchmark 档）与 FP64（golden 档）两种精度。
- **反向对比**：`backward_test_autograd` 做三方对比——BF16 张量上 autograd（基准 bm）、FP64 CPU 上 autograd（金标准 golden）、NPU kernel（被测 pto），逐路梯度比较。
- **精度判据**：不是逐位相等，而是 MARE/MERE/RMSE 三指标的相对比值加小值域错误计数，属于昇腾算子精度标准（L0/L1/L2）的 triplet 形式，u8-l3 会展开。

#### 4.4.2 核心流程

```text
test_model(N, M, group, bit, eps, clip_val)
  └─ for dis in DISTRIBUTION:                       # 当前只启用 uniform_large
       └─ run_single_test(...)
            ① create_input 造 weight/scale/offset（BF16, NPU, seed=33, requires_grad=True）
            ② golden = create_asymmetric_qat_golden(...)   # 闭包捕获量化参数
            ③ backward_test_autograd(golden_inputs, pto_inputs, golden, kernel_wrapper)
                 a. clone 一份做 BF16 基准：前向 → randn 造 grad_outputs → autograd.backward
                 b. 转 FP64 CPU 做 金标准：同上
                 c. pto kernel：直接调 wrapper(grad_output, weight, scale, offset, ...)
                 d. 对 weight/scale/offset 三路梯度逐一 compare（triplet 精度）
```

#### 4.4.3 源码精读

**golden 工厂（与 4.3 的验证脚本同构）**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py:L18-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py#L18-L77)：`is_golden=False` 时升 FP32、`is_golden=True` 时升 FP64（L30-L37）；shape 校验（2 维、M 被 group_size 整除、scale/offset 形状 (num_groups, 1)，L39-L55）；公式链 L57-L69，其中 L65-L66 是 STE 的 detach 写法，L47 的 `torch.where(scale > eps, ...)` 是防零保护的可导等价形式——autograd 对 where 自动只向被选中分支传梯度，天然实现了 \(\mathbb{1}[s>\varepsilon]\) 掩码。

**用例组织**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py:L80-L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py#L80-L97)：`run_single_test` 计算组数 `N * M // group_size`，造三个 requires_grad 输入（L90-L92），组装 golden_inputs（3 张量）与 pto_inputs（3 张量 + group_size/bit/eps/clip_val 四个参数，L94-L95），交给 `backward_test_autograd`。注意 `backward_test_autograd` 的调用约定：`pto_func` 第一个参数是 grad_outputs、其余参数是 golden 各参数本身——这正好匹配反向 wrapper 的签名 `(grad_output, weight, scale, offset, group_size, bit, eps, clip_val)`。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py:L100-L111](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py#L100-L111)：参数化两组用例 (1024, 2048, 128, bit=2) 与 (768, 2048, 128, bit=3)；设备号取自环境变量 `ASCEND_DEVICE_ID`。**易踩坑**：前向测试 [test_ai_infra_qat_asymmetric_per_group.py:L111](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L111) 用的是 `TILE_FWK_DEVICE_ID`，两个文件环境变量名不一致，跑测试时需分别设置（或依赖默认值 0）。

**三方对比引擎（tests/utils.py）**：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py:L387-L413](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L387-L413)：BF16 基准支——clone 输入、跑 golden 前向、用 `torch.randn` 造 grad_outputs、`torch.autograd.backward` 反传、`collect_grads` 收集三路梯度。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py:L419-L441](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L419-L441)：FP64 金标准支（detach 到 CPU double 重跑同一 golden 与同一组 grad_outputs）与 pto 支（单输出时 `pto_func(grad_outputs[0], *pto_inputs)`）。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py:L452-L466](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L452-L466)：对每个 requires_grad 输入（顺序即 weight、scale、offset），先 `assert_allclose(pto, bm, rtol=1e-3, atol=1e-3)` 做硬校验（失败仅记日志不中断），再 `compare(pto, bm, golden)` 做 triplet 精度判定（不达标抛异常）。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py:L220-L263](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L220-L263)：`precision_compare_triple`——大值域按 MARE/MERE/RMSE 的「pto 相对 bm」比值判定（默认阈值 2/1.2/1.2，即 L2 级），小值域按错误计数比判定；三支都升 FP32 后在 CPU 上比。
- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py:L24-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L24-L32)：`DISTRIBUTION` 当前只启用 `uniform_large`（FP32 均匀 [-5, 5] 再转 BF16，见 L124-L127）；normal/outlier 等分布被注释，需要时手动打开。

#### 4.4.4 代码实践

**实践目标**：跑通（或半跑通）反向 ST，观察三路梯度的精度报告。

**操作步骤**：

1. 有 NPU 环境（torch_npu + pypto 已安装，参考 u1-l3）时，在 `pypto/src/ops-nn/quant` 目录层级下执行（测试内 `sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")` 决定了工作目录必须在 `pypto/src`）：

   ```bash
   cd pypto/src
   python -m pytest ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py -v
   ```

2. 无 NPU 环境时，做「源码阅读型实践」：把 4.3.4 的 CPU 脚本里 `asymmetric_qat` 换成 ST 文件中的 golden（[L18-L77](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py#L18-L77)），即只运行三方对比中的「金标准支」，确认三路梯度的 autograd 数值与手写公式一致。

**需要观察的现象**：NPU 路径下每个用例打印 3 组 `=== compare grad of input[i] ===`（依次 weight/scale/offset），每组带 precision result 与 mare/mere/rmse/small_value 四个指标；CPU 路径下三行 allclose 均为 True。

**预期结果**：NPU 路径所有用例 `precision result: PASS`（MARE 比 ≤ 2、MERE/RMSE 比 ≤ 1.2、小值域比 ≤ 2）；CPU 路径手写公式与 autograd 一致。本机无 NPU，以上运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么金标准支要把输入转成 FP64 CPU 张量，而基准支保持 BF16 NPU 张量？

**答案**：金标准代表「无穷精度下的正确答案」，用于消除参考实现自身的舍入误差；基准 bm 代表「同一公式在目标精度（BF16）下的表现」，其与金标准的差距就是 BF16 的固有误差。被测 kernel 只需「不比 BF16 基准差得更多」（比值判据），而不是逼近 FP64——这正是 triplet 判据用比值而非绝对误差的原因。

**练习 2**：grad_outputs 是怎么来的？为什么两支 autograd 必须用同一组 grad_outputs？

**答案**：由基准支前向输出的形状用 `torch.randn` 现场生成（utils.py L405）。金标准支把它 detach 转 FP64 后复用（L424）。若两支用不同的 grad_outputs，对比的就不是同一微分方程的解，三路梯度无从比较。

**练习 3**：如果要给 bit=4 补一个用例，参数化列表里应加哪一行？组数是多少、unroll 分块序列是什么？

**答案**：仿照 L105-L106 加 `(1024, 2048, 128, 4, 0.0001, 0.99)`（id 会自动格式化为 `N1024-M2048-group128-bit4-eps0.0001-clip_val0.99`）。组数 \(= 1024 \times 2048 / 128 = 16384\)，unroll 序列为 32 个 512 块、无尾块（见 4.2.4 的模拟）。

## 5. 综合实践

把本讲全部内容串成一份「公式链 → autograd 参考 → kernel 行号映射 → 展开策略分析」的完整作业：

**任务**：为 `ai_infra_qat_asymmetric_per_group`（前向 + 反向）编写一份 CPU 参考实现报告。

1. **公式链**：手写完整九步公式（减 offset → 归一化 → clip → 进刻度减 shift → round → 加 shift → 反归一化 → 乘 alpha → 加 offset），标注每步的数学含义（可抄 4.1.2，但要求自己推一遍界内 STE 恒等性）。
2. **torch 参考实现与梯度**：以 4.3.4 脚本为底稿，扩展三件事：
   - 前向输出同时与「直接用 round（无 STE）」的版本对比，确认前向数值相同、只有梯度不同；
   - 用 autograd 对 weight / scale / offset 求三路梯度；
   - 人为构造一个 \(s \le \varepsilon\) 的组（如把某组 scale 置 0），验证该组 grad_scale 为 0 而其余组正常。
3. **行号映射表**：把每条梯度公式对应到 kernel 代码行，填完下表：

   | 梯度公式 | kernel 位置（文件 ai_infra_pypto_qat.py） |
   |---|---|
   | \(g \odot \text{mask}\) | L185 |
   | \(\sum_j g \odot (1-\text{mask})\) | L188-L189 |
   | \(q - u \odot \text{mask}\) | L192-L193 |
   | 组内归约 \(\sum_j g \odot \text{term}\) | L194-L195 |
   | 链式 \(\times n_{\text{levels}}\) | L197 |
   | \(\mathbb{1}[s>\varepsilon]\) 掩码 | L177-L180、L198 |
   | 界内掩码（放大截断技巧） | L165-L174 |

4. **展开策略分析**：用 4.2.4 的 `simulate_unroll` 回答——(a) 测试用例的两组 shape 各产生多少个展开块？(b) 若 num_groups=4800，写出分块序列并说明第 10 块的 tile 形状；(c) 把 unroll_list 改为 `[512, 256, 128]` 对 4800 组的尾块有什么影响？（答：尾块从 256 变为 256，无变化；但对 num_groups=4352 这类 shape，余量 256→128 的组会从「不足 256 的整块收尾」变成「精确 128×2」——具体以框架生成的收尾块语义为准，待确认。）

**验收标准**：CPU 脚本三路梯度 allclose 全 True；映射表行号能在仓库当前 HEAD 打开；展开分析能把每个展开块还原成「块高 × 128 的二维 tile」。

## 6. 本讲小结

- 非对称量化在归一化前后各加一次 offset（减 offset → … → 加 offset），配合 n_levels 半幅刻度与 shift=0.5 半格平移，在 clip_val=0.99 约束下重建格点恰好 \(2^{\text{bit}}\) 个且关于 offset 对称。
- 前向 kernel 用 `loop_unroll(unroll_list=[512, 256])` 沿组维度大块优先、余量降级地展开，每个展开块是一个 `unroll_length × group_size` 的 FP32 计算 tile，九步公式链逐行对应 docs。
- 反向是无缓存设计：重算前向到 \(u\) 与 \(q\)，用「差值放大截断」的纯向量算术生成界内掩码与 scale 合法掩码。
- STE 下三路梯度异常简洁且两段（界内/界外）统一：grad_weight \(= g \odot \text{mask}\)、grad_offset \(= \sum g \odot (1-\text{mask})\)、grad_scale \(= n_{\text{levels}} \cdot \mathbb{1}[s>\varepsilon] \cdot \sum g \odot (q - u \odot \text{mask})\)。
- 反向 ST 用一份 golden 同时构造 BF16-autograd 基准、FP64-autograd 金标准与 NPU kernel 三方，按 MARE/MERE/RMSE 比值加小值域计数判定精度；注意前向/反向测试读取的设备环境变量名不同（`TILE_FWK_DEVICE_ID` vs `ASCEND_DEVICE_ID`）。

## 7. 下一步学习建议

下一讲 u7-l4《pypto 算子测试：st 用例与精度对比》会系统展开 `tests/utils.py` 的测试工具（`create_input` 的分布族、`forward_test` 与 `backward_test_autograd` 的完整契约、阈值取值依据），并把本讲手工跑过的对比逻辑放到六个 test 文件（前反向 × 三种量化粒度）的全景中。读完 u7 系列后，建议对照 u7-l1 的「一个 .py 文件替代 ascendc 四层结构」结论，回到 u6 的 torch_ops_extension 看 Ascend C 路线如何解决同样的「算子进 PyTorch」问题，两条技术路线的取舍就完整了。
