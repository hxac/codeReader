# Muon.step 拆解（上）：动量、正交化与权重衰减

## 1. 本讲目标

上一讲（u2-l2）我们已经把 `zeropower_via_newtonschulz5` 这个「正交化黑盒」拆开了。本讲反过来看调用它的主人——`Muon.step` 中的 **Muon 分支**，即从「拿到梯度」到「参数被更新」之间的完整流水线。学完本讲，你应该能够：

- 说清楚 `momentum_buffer` 为什么、如何在第一次 `step()` 时被惰性创建，以及它的更新公式。
- 区分 `nesterov=True/False` 两种送入正交化的矩阵形式，并理解 Nesterov 的「前瞻」直觉。
- 理解高维梯度 `view` 展平分支的意图，以及为什么它在当前玩具模型中不会触发。
- 解释权重衰减为什么以 `p.data.mul_(1 - lr * wd)` 这种**解耦**方式实现，而这正是 Moonlight 论文给 Muon 补上的两大关键技术之一（另一项是下一讲的更新 RMS 缩放）。

本讲只处理 `step()` 中 `use_muon=True` 的那部分参数（u2-l1 划定过的 84 个二维权重矩阵）；同函数内的 AdamW 后备分支留给 u2-l5。

## 2. 前置知识

本讲假设你已读过 u1-l3（训练循环）与 u2-l1、u2-l2（参数分组、Newton-Schulz）。在此基础上补充三个概念：

- **动量（momentum）**：梯度 descent 的一种「惯性」改进。不沿当前梯度 \( \mathbf{g}_t \) 走，而是沿一个指数加权的历史梯度累积方向 \(\mathbf{b}_t\) 走，可以平滑抖动、加速穿过峡谷状损失面。Muon 内部跑的就是这种最朴素的 SGD-momentum（类文档字符串自称 "MomentUm Orthogonalized by Newton-schulz"）。
- **优化器状态（optimizer state）**：PyTorch 的 `torch.optim.Optimizer` 给每个参数张量维护一个字典 `self.state[p]`，用来存放动量缓冲、步数计数器等跨 step 存活的数据。u2-l1 里见过的 `state[p]["use_muon"]` 布尔标记就存在这里。
- **解耦权重衰减（decoupled weight decay）**：AdamW 风格的衰减不把 \(\text{wd}\cdot\theta\) 加进梯度，而是直接把参数按固定比例缩小 \(\theta \leftarrow (1-\text{lr}\cdot\text{wd})\theta\)。「加进梯度」与「直接缩小」在 Adam 这类自适应优化器里不等价，对 Muon 更是如此——本讲 4.4 会看到原因。

另外回顾两个 u1-l3 的事实，本讲会直接用到：学习率调度器通过改写 `param_groups` 里的 `lr` 生效，因此 `step()` 里读到的 `group["lr"]` 是**当前步**的学习率；训练循环里 `zero_grad()` 在 `step()` 之后调用，所以进入 `step()` 时 `p.grad` 恰好是本步累积的梯度。

## 3. 本讲源码地图

本讲全部源码仍集中在唯一脚本 `examples/toy_train.py`：

| 行段 | 内容 | 与本讲关系 |
| --- | --- | --- |
| [L79-L140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L79-L140) | `Muon` 类与 `__init__`（momentum/nesterov/ns_steps 默认值） | 默认值来源 |
| [L142-L148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148) | `adjust_lr_for_muon` 按形状缩放学习率 | 本讲只引用，u2-l4 精读 |
| [L150-L239](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L150-L239) | `step()` 总入口 | 本讲主体 |
| [L162-L203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L162-L203) | `step()` 中的 Muon 分支 | **本讲主角** |
| [L205-L237](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L205-L237) | `step()` 中的 AdamW 后备分支 | 对照参考，u2-l5 精读 |
| [L48-L76](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L48-L76) | `zeropower_via_newtonschulz5` | u2-l2 已精读，本讲当黑盒调用 |
| [L287-L313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L313) | `get_optimizer` 构造 `Muon` 的入口 | 超参透传路径 |

整体流水线一句话概括：**取梯度 → 更新动量缓冲 → （可选）Nesterov 前瞻 → Newton-Schulz 正交化 → 按形状缩放学习率 → 解耦衰减参数 → 减去更新**。

## 4. 核心概念与源码讲解

### 4.1 动量缓冲

#### 4.1.1 概念说明

优化器要跨 step 记住东西。对 Muon 的 Muon 分支来说，唯一需要记住的就是每个参数的**动量缓冲** `momentum_buffer`：一个与梯度同形状的张量，每步做「乘 μ 再加当前梯度」的指数滑动累积。

它解决的问题是：单步梯度噪声大、方向不稳定，直接对单步梯度做正交化，得到的更新方向会逐步抖动。先做动量平滑，再正交化「平滑后的方向」，更新质量更稳定。

#### 4.1.2 核心流程

```
第一次 step(p):
    state[p] 里只有 use_muon 标记（__init__ 时写入）
    state[p]["momentum_buffer"] = zeros_like(g)   ← 惰性创建，全零

每次 step(p):
    buf = state[p]["momentum_buffer"]
    buf ← μ · buf + g                              ← 就地更新
    （随后 buf 或其 Nesterov 变体送入正交化，见 4.2）
```

数学上，缓冲更新式为：

\[
\mathbf{b}_t = \mu\, \mathbf{b}_{t-1} + \mathbf{g}_t, \qquad \mathbf{b}_0 = \mathbf{0}
\]

若梯度恒定为 \(\mathbf{g}\)，可展开为：

\[
\mathbf{b}_t = \frac{1-\mu^{t+1}}{1-\mu}\,\mathbf{g}
\]

即缓冲相对稳态值 \(\frac{1}{1-\mu}\mathbf{g}\) 的饱和度为 \(1-\mu^{t+1}\)。衡量记忆长度的常用指标是「半衰期」——单步旧梯度权重衰减到一半所需的步数：

\[
t_{1/2} = \frac{\ln 2}{\ln(1/\mu)}
\]

| μ | 半衰期（步） | 第 100 步饱和度 \(1-\mu^{100}\) |
| --- | --- | --- |
| 0.8 | ≈ 3.1 | ≈ 100% |
| 0.95（默认） | ≈ 13.5 | ≈ 99.4% |
| 0.99 | ≈ 69.0 | ≈ 63.4% |

这张表是第 5 节网格实验的「理论预告」：100 步的短训练里，μ=0.99 的缓冲远未饱和，等效于缩小了更新步长。

#### 4.1.3 源码精读

Muon 分支的开头：收集本组中 `use_muon=True` 的参数，并从 `param_groups` 读出当前步超参——[examples/toy_train.py:L162-L172](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L162-L172)。这里 `lr = group["lr"]` 读到的正是 u1-l3 讲过的、被 cosine 调度器逐 step 改写的当前学习率。（L169 有一行被注释掉的 `# import pdb; pdb.set_trace()`，研究代码的典型痕迹。）

动量缓冲的惰性初始化与更新——[examples/toy_train.py:L175-L189](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L175-L189)：

```python
for p in params:
    # sanity check
    g = p.grad
    if g is None:
        continue
    ...
    state = self.state[p]
    if "momentum_buffer" not in state:
        state["momentum_buffer"] = torch.zeros_like(g)
    buf = state["momentum_buffer"]
    buf.mul_(momentum).add_(g)
```

三个值得咀嚼的细节：

1. **惰性初始化**：`zeros_like(g)` 以梯度为模板创建，自动继承梯度的设备与 dtype。u1-l3 提过本脚本的优化器在 `model.to(device)` **之前**创建——如果 `__init__` 里就创建缓冲，它会落在 CPU 上而参数已在 GPU；惰性创建则保证缓冲第一次被需要时就长在正确的设备上。
2. **无梯度即跳过**：`g is None` 时 `continue`，该参数本步既不更新也**不衰减**（衰减在循环体内，见 4.4），且永远不会拥有动量缓冲。
3. **就地写法**：`buf.mul_(momentum).add_(g)` 等价于 `buf = momentum * buf + g`，但避免了每步为缓冲分配新显存，是优化器热路径的惯用优化。

顺带一提 L182 的 `assert g is not None`：它排在 `if g is None: continue` 之后，永远为真，是从上游继承的冗余防御，阅读时不必纠结。

#### 4.1.4 代码实践

**实践目标**：用一段独立脚本验证 4.1.2 的饱和度公式，建立对动量记忆长度的数值直觉。

**操作步骤**（示例代码，可保存为 `momentum_sim.py` 单独运行，不动仓库源码）：

```python
import torch

mu = 0.95
g = torch.ones(4, 4)                # 假设每步梯度完全相同
buf = torch.zeros_like(g)           # 模拟惰性初始化的全零缓冲
norms = []
for t in range(1, 101):
    buf.mul_(mu).add_(g)            # 与 L189 完全相同的更新式
    norms.append(buf.norm().item())

for t in (3, 14, 100):
    theory = ((1 - mu ** t) / (1 - mu)) * g.norm().item()
    print(f"t={t}: 模拟范数={norms[t-1]:.4f}, 理论值={theory:.4f}")
```

**需要观察的现象**：模拟范数与理论值逐点吻合；曲线先快后慢地逼近稳态 \(4/(1-\mu)=80\)（4×4 全 1 矩阵的范数为 4）。

**预期结果**：`mu=0.95` 时约 70 步后基本饱和；把 `mu` 改成 0.8 与 0.99 重跑，饱和速度差异显著（0.99 时第 100 步仍只有稳态的约 63%）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `momentum_buffer` 不在 `Muon.__init__` 里创建？

**答案**：缓冲需要与梯度同设备、同 dtype、同形状，而 `__init__` 时参数可能还没搬到 GPU、甚至还没参与过前向（没有梯度）。`step()` 里用 `zeros_like(g)` 惰性创建，天然对齐当前梯度所在的设备与类型。

**练习 2**：`buf.mul_(momentum).add_(g)` 与写成 `buf = momentum * buf + g` 数学等价，源码为何选前者？

**答案**：前者就地修改缓冲张量，不产生新分配；后者每步都创建临时张量再丢弃。`step()` 每个 training step 都会对全部 84 个 Muon 参数执行一次，热路径上省掉大量显存分配是有意义的。

**练习 3**：某参数的 `p.grad` 一直为 `None`，训练 1000 步后它的值会变吗？

**答案**：不会。`continue` 使它既不进入动量与更新逻辑，也跳过了权重衰减，参数保持原值不动。

### 4.2 Nesterov 动量

#### 4.2.1 概念说明

标准动量沿「历史累积方向」\(\mathbf{b}_t\) 前进；Nesterov 动量的直觉是**先顺着惯性走半步，在超前点上再看当前梯度**，相当于对动量方向的一次「提前修正」，在凸优化里有更好的理论保证，实践中通常更稳。类文档字符串明确标注 nesterov 为 "(recommended)"（[L96](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L96)），代码默认值也是 `nesterov=True`（[L112](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L112)）。

关键点：Nesterov 开关只改变**送入正交化的矩阵**，动量缓冲本身的更新式不变。

#### 4.2.2 核心流程

```
buf ← μ · buf + g                    （与 4.1 相同）
if nesterov:
    送入正交化的矩阵 = g + μ · buf    ← 当前梯度 + 前瞻量
else:
    送入正交化的矩阵 = buf            ← 直接用缓冲
u = Newton-Schulz(该矩阵, ns_steps)
```

两种形式：

\[
\tilde{\mathbf{g}}_t =
\begin{cases}
\mathbf{g}_t + \mu\,\mathbf{b}_t & \text{Nesterov} \\[4pt]
\mathbf{b}_t & \text{标准动量}
\end{cases}
\]

注意展开 Nesterov 形式：\(\mathbf{g}_t + \mu\mathbf{b}_t = \mathbf{g}_t + \mu^2\mathbf{b}_{t-1} + \mu\mathbf{g}_t = (1+\mu)\mathbf{g}_t + \mu^2\mathbf{b}_{t-1}\)——当前梯度的权重比标准动量（权重 μ）更大，历史惯性以 \(\mu^2\) 衰减得更快。这解释了为什么两者在梯度突变处行为不同。

#### 4.2.3 源码精读

[examples/toy_train.py:L189-L194](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L189-L194)：

```python
buf.mul_(momentum).add_(g)
if group["nesterov"]:
    g = g.add(buf, alpha=momentum)
else:
    g = buf
u = zeropower_via_newtonschulz5(g, steps=group["ns_steps"])
```

两处精读级细节：

1. `g.add(buf, alpha=momentum)` 是**非就地**操作，返回新张量，`p.grad` 本身不被污染；正交化输入只在本 step 内使用，不回写梯度。这保证了与梯度累积等用法兼容（u1-l3 讲过 PyTorch 梯度是累加语义）。
2. `else` 分支的 `g = buf` 是**引用别名**而非拷贝——这看起来危险，实际安全，因为 u2-l2 精读过的 `zeropower_via_newtonschulz5` 是纯函数式的：它的第一步 `X = G.bfloat16()`（[L61](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L61)）就新建了张量，后续从不原地修改输入 `G`。如果换成会原地改输入的正交化实现，这里必须 `buf.clone()`，否则缓冲会被悄悄破坏——这是读优化器源码时值得养成的警惕点。

#### 4.2.4 代码实践

**实践目标**：数值观察同一梯度序列下，两种模式送入正交化的矩阵差异有多大。

**操作步骤**（示例代码）：

```python
import torch

torch.manual_seed(0)
mu = 0.95
grads = [torch.randn(8, 4) for _ in range(30)]
buf_n = torch.zeros(8, 4)
buf_p = torch.zeros(8, 4)
diffs = []
for g in grads:
    buf_n.mul_(mu).add_(g)                    # 两条缓冲演化完全相同
    buf_p.mul_(mu).add_(g)
    g_nesterov = g.add(buf_n, alpha=mu)       # nesterov=True 的输入
    g_plain = buf_p.clone()                    # nesterov=False 的输入
    diffs.append((g_nesterov - g_plain).norm().item())

print("前 5 步差值:", [f"{d:.3f}" for d in diffs[:5]])
print("后 5 步差值:", [f"{d:.3f}" for d in diffs[-5:]])
```

**需要观察的现象**：两种输入矩阵的差随步数如何变化——随机梯度下差值不会消失，说明这是持续的方向偏置而非暂态。

**预期结果**：差值迅速进入稳定波动区间（因为 \(\mathbf{g}+\mu\mathbf{b}\) 与 \(\mathbf{b}\) 相差一项 \(\mathbf{g}-(1-\mu)\mathbf{b}\)，其量级与梯度噪声同级）。再分别对两个矩阵调用 `zeropower_via_newtonschulz5`（从 `toy_train.py` 导入或复制），观察正交化输出的差异通常小于输入差异——正交化对输入扰动有一定「限幅」作用。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：写出 `nesterov=True` 时送入 Newton-Schulz 的矩阵表达式，并指出 μ 出现在哪两处。

**答案**：\(\tilde{\mathbf{g}}_t = \mathbf{g}_t + \mu\,\mathbf{b}_t\)，其中 \(\mathbf{b}_t = \mu\mathbf{b}_{t-1} + \mathbf{g}_t\)。μ 一处用于缓冲的指数累积（L189），一处在前瞻外推系数（L191）。

**练习 2**：把 L191 改成 `g.add_(buf, alpha=momentum)`（就地版）会有什么问题？

**答案**：`g` 此时是 `p.grad` 的引用（二维参数未经 `view` 重绑），就地修改等于把动量信息写进了梯度本身。本脚本因 `zero_grad()` 紧随 `step()` 而侥幸不炸，但任何依赖 `p.grad` 纯净性的用法（梯度累积、梯度范数监控、梯度裁剪若放在 step 后读取）语义都会被破坏。

**练习 3**：为什么 `nesterov=False` 分支的 `g = buf` 不需要 clone？

**答案**：因为 `zeropower_via_newtonschulz5` 是纯函数，从不原地修改输入（首行 `X = G.bfloat16()` 即创建新张量），缓冲内容不会被改动。这是隐式契约——改成会改输入的实现时此处就埋雷。

### 4.3 高维梯度展平

#### 4.3.1 概念说明

Newton-Schulz 正交化只接受**二维矩阵**（u2-l2 的 `assert len(G.shape) == 2`）。但深度学习里存在三维及以上参数（如卷积权重 \([C_{out}, C_{in}, k, k]\)）。上游通用做法是：保留第 0 维，把其余维展平成矩阵再正交化——直觉是「每个输出通道对全部输入通道的映射」视为一行。

本仓库的脚本注明这段代码改编自上游 [examples/toy_train.py:L46-L47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47)（KellerJordan/Muon 仓库的 `muon.py`），展平分支正是继承来的通用写法。

#### 4.3.2 核心流程

```
if g.ndim > 2:
    g = g.view(g.size(0), -1)     # [A, B, C, ...] → [A, B×C×...]
（后续正交化、更新都在展平后的形状上进行；参数本身形状不变，更新按元素对位写回）
```

形状变换：

\[
g \in \mathbb{R}^{A \times B \times C} \;\Rightarrow\; g' \in \mathbb{R}^{A \times (BC)}
\]

#### 4.3.3 源码精读

[examples/toy_train.py:L175-L182](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L175-L182)：

```python
for p in params:
    # sanity check
    g = p.grad
    if g is None:
        continue
    if g.ndim > 2:
        g = g.view(g.size(0), -1)
    assert g is not None
```

诚实的源码阅读结论：**这个分支在当前实现中不会触发**。原因是 `Muon.__init__` 里的硬断言 [examples/toy_train.py:L134-L137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L134-L137)：

```python
for p in muon_params:
    assert p.ndim == 2, p.ndim
    self.state[p]["use_muon"] = True
```

参数必须是二维才能进 Muon 组，而梯度与参数同维，`g.ndim > 2` 恒为假。`get_optimizer` 的筛选条件是 `p.ndim >= 2`（[L296](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L296)），但对 Qwen2 这种纯 Transformer，所有 ≥2 维参数恰好都是二维（u2-l1 统计过：Muon 组 84 个全是 2D 矩阵），断言恰好不炸。若换成含卷积的模型，`get_optimizer` 会放三维参数进来、随即在 `assert p.ndim == 2` 处报错——即当前实现根本不支持高维参数，`step` 内的展平是从上游继承、供未来放宽断言时启用的「预备代码」。

识别并坦然指出这类「当前路径下的死代码」是源码阅读的重要能力：它告诉你代码的**来路**（上游通用实现）与**边界**（本仓库只支持 2D）。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `view` 展平的形状语义，并确认玩具模型中确实没有高维参数。

**操作步骤**（示例代码 + 一段只读检查）：

```python
import torch

g = torch.arange(2 * 3 * 4).float().reshape(2, 3, 4)
flat = g.view(g.size(0), -1)
print(flat.shape)          # 期望 torch.Size([2, 12])
print(torch.equal(flat[1], g[1].reshape(-1)))   # 期望 True：第 0 维语义保留
```

再运行一段只读脚本核对模型参数维度分布：

```python
from collections import Counter
from transformers import Qwen2Config, Qwen2ForCausalLM

config = Qwen2Config(hidden_size=64, num_attention_heads=16,
                     num_hidden_layers=2, intermediate_size=128,
                     tie_word_embeddings=True, vocab_size=151936)
model = Qwen2ForCausalLM(config)
print(Counter(p.ndim for _, p in model.named_parameters()))
```

**需要观察的现象**：`view` 把 \([2,3,4]\) 压成 \([2,12]\)，且每个「输出通道」的展开连续；参数维度统计只有 1 和 2。

**预期结果**：`Counter` 形如 `{2: ..., 1: ...}`，不出现 3 及以上，印证 4.3.3 的结论。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：形状为 \([A, B, C]\) 的梯度经 `g.view(g.size(0), -1)` 后变成什么形状？展平后正交化的「行」对应什么？

**答案**：变成 \([A, B \times C]\)。每一行对应原参数第 0 维的一个切片（如一个输出通道对全部输入的完整映射），正交化把这些行向量变成一组近似正交的方向。

**练习 2**：既然 `__init__` 的 `assert` 已保证参数二维，L180-L181 是否可以删掉？

**答案**：对当前仓库的功能而言可以删（分支不可达），但保留它保留了与上游实现的一致性，也为将来放宽断言支持卷积参数留了通路。删不删属于工程取舍，重要的是**知道**它当前不可达。

**练习 3**：如果想让三维参数真正使用 Muon，至少要改哪两处？

**答案**：放宽 `__init__` 中的 `assert p.ndim == 2`（如改为 `assert p.ndim >= 2`），并确保 `get_optimizer` 的筛选与 `step` 内展平逻辑衔接（后者已就绪）；还需检查 `adjust_lr_for_muon` 的 `param_shape[:2]` 取的是展平前形状 \([A, B, C]\) 的前两维 \((A, B)\)——此时用哪个形状参与缩放是个需要重新决策的问题（见 u2-l4）。

### 4.4 权重衰减与更新应用

#### 4.4.1 概念说明

原版 Muon（上游 KellerJordan 实现）没有权重衰减。Moonlight 论文的两大改进之一（u1-l1 讲过）就是**把权重衰减加回来**，代码落点正是本节的 `p.data.mul_(1 - lr * wd)`。

为什么必须「解耦」？Muon 的核心操作是正交化：无论输入矩阵的范数多大，输出 \(\mathbf{u}\) 的奇异值都被拉到 1 附近——**幅度信息被抹掉了，只留下方向**。如果把衰减项 \(\text{wd}\cdot\theta\) 加进梯度再正交化，衰减贡献会与其它梯度方向混在一起被整体归一化，既无法保证「每步按固定比例收缩参数」，其贡献还可能被方向噪声淹没。解耦式直接按乘法收缩参数，与梯度完全无关，正则效果确定。这与 AdamW 之于 Adam 的动机同构，但对 Muon 更性命攸关。

#### 4.4.2 核心流程

每个 Muon 参数在动量与正交化之后，参数更新分两步完成：

```
adjusted_lr = 0.2 * sqrt(max(A, B)) * lr        ← 按形状缩放（u2-l4 详述）
p ← p · (1 - lr · wd)                            ← 第一步：解耦衰减
p ← p - adjusted_lr · u                          ← 第二步：沿正交方向更新
```

合并成一步公式：

\[
\theta_{t+1} = (1 - \text{lr}\cdot\text{wd})\,\theta_t - \text{adjusted\_lr}\cdot \mathbf{u}_t,
\qquad \mathbf{u}_t \approx \mathrm{orth}(\tilde{\mathbf{g}}_t)
\]

其中 \(\text{adjusted\_lr} = 0.2\sqrt{\max(A,B)}\cdot\text{lr}\)。由 u2-l2 的结论，\(\mathbf{u}_t\) 的谱范数约为 1，所以单步更新的谱范数约等于 \(\text{adjusted\_lr}\)——docstring 所说 "The updates will have spectral norm of `lr`"（[L94](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L94)）在本实现中精确说的是 adjusted 后的量级。

一个容易被忽略的不对称：**衰减用原始 lr，更新用 adjusted_lr**。对 \([1024,1024]\) 的权重，\(0.2\sqrt{1024}=6.4\)，更新步长是衰减比例中 lr 的 6.4 倍。这个不对称是论文「按形状一致化更新 RMS」设计的组成部分，其来龙去脉留给 u2-l4，本讲先记住事实。

另一个细节：衰减强度随调度的 lr 同步变化。warmup 第 0 步 lr=0 时 \(1-\text{lr}\cdot\text{wd}=1\)，参数完全不衰减；lr 到达峰值时衰减也最强；余弦退火尾部衰减趋零。衰减与学习率「同进退」。

#### 4.4.3 源码精读

[examples/toy_train.py:L194-L203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L194-L203)：

```python
u = zeropower_via_newtonschulz5(g, steps=group["ns_steps"])

# scale update
adjusted_lr = self.adjust_lr_for_muon(lr, p.shape)

# apply weight decay
p.data.mul_(1 - lr * wd)

# apply update
p.data.add_(u, alpha=-adjusted_lr)
```

四行依次是：正交化（u2-l2 的黑盒在此被调用，`ns_steps` 默认 5，来自 [L113](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L113)；有趣的是 docstring L97 说 "6 is probably always enough"，文档与代码默认值并不一致）；按参数形状 `p.shape` 计算缩放学习率（`adjust_lr_for_muon` 定义在 [L142-L148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148)，一行 `0.2 * math.sqrt(max(A, B))`）；解耦衰减；沿 \(-\mathbf{u}\) 方向减去更新。

`p.data.add_(u, alpha=-adjusted_lr)` 中的 `u` 形状与 `p` 相同（二维参数未经展平重绑，或展平后形状未变），按元素对位写回。`p.data` 直接修改参数底层存储、绕过 autograd 追踪，是手写优化器的惯用写法。

还要注意：`lr` 与 `wd` 都来自 [L170-L171](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L170-L171) 的 `group["lr"]`、`group["wd"]`。u1-l2 发现过 `--wd` 参数被解析但未转发，`get_optimizer` 用默认 `wd=0.1`（[L109](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L109) 与 [L287](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287) 的签名默认值），所以命令行改 `--wd` 不会影响本行行为——这是做衰减相关实验前必须知道的坑。

#### 4.4.4 代码实践

**实践目标**：用单参数数值实验验证两步更新的数学，并直观感受「解耦」与「混入梯度」的差异。

**操作步骤**（示例代码）：

```python
import torch

torch.manual_seed(0)
p = torch.randn(256, 256)
u = torch.randn(256, 256)          # 假装这是正交化后的更新
lr, wd = 1e-3, 0.1
A, B = p.shape
adjusted_lr = 0.2 * (max(A, B) ** 0.5) * lr     # = 3.2e-3

# 手工按公式计算期望结果
expected = (1 - lr * wd) * p - adjusted_lr * u

# 复现源码的两步操作
p2 = p.clone()
p2.data.mul_(1 - lr * wd)          # 对应 L200
p2.data.add_(u, alpha=-adjusted_lr)  # 对应 L203

print("两步操作与公式一致:", torch.allclose(p2, expected))
print("adjusted_lr / lr =", adjusted_lr / lr)

# 反面对照：把衰减混入"梯度"再正交化（以单位归一化近似 orth）
g_mixed = p.grad if p.grad is not None else torch.randn_like(p)
g_mixed = g_mixed + wd * p         # 假想的 L2 正则化加法
g_mixed = g_mixed / g_mixed.norm() # 正交化会抹掉幅度（此处用归一化示意）
print("混入梯度后，wd·p 对方向的影响被归一化稀释")
```

**需要观察的现象**：第一处 `allclose` 为 True；`adjusted_lr / lr` 等于 \(0.2\sqrt{256}=3.2\)，即该形状下更新步长是名义 lr 的 3.2 倍。

**预期结果**：数值精确吻合；反面对照中，\(\text{wd}\cdot p\) 的贡献随整体归一化被缩放到固定谱范数，无法再体现「按 0.9999 比例收缩」的确定正则——这就是解耦的必要性。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Muon 的权重衰减不能用「把 \(\text{wd}\cdot\theta\) 加进梯度」的方式实现？

**答案**：正交化会抹掉输入矩阵的幅度信息（奇异值全部拉到 1 附近），衰减项混入梯度后与其它方向一起被归一化，既失去固定收缩比例、贡献量又取决于梯度噪声，正则效果不可控。解耦式 `p.mul_(1 - lr*wd)` 与梯度无关，收缩量确定。

**练习 2**：训练刚开始（warmup 第 0 步附近）权重衰减的实际效果如何？

**答案**：几乎为零。衰减因子 \(1-\text{lr}\cdot\text{wd}\) 中的 lr 由调度器控制，warmup 起点 lr=0，因子为 1；衰减强度随 lr 爬升而增强，随余弦退火而减弱，与学习率「同进退」。

**练习 3**：衰减用 `lr`、更新用 `adjusted_lr`，这个不对称说明什么？

**答案**：说明「更新幅度」与「参数收缩比例」被刻意解耦成两条尺度：更新按形状缩放以保证不同形状矩阵的更新 RMS 一致（论文核心贡献之二），衰减保持全局统一比例。两者如何配合支撑规模化训练，正是 u2-l4 的主题。

## 5. 综合实践

**任务：momentum × ns_steps 网格实验——量化两个 Muon 超参在前 100 步的敏感性。**

`momentum` 与 `ns_steps` 的默认值 0.95 与 5 写在 `Muon.__init__` 签名里（[L111](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L111)、[L113](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L113)），而 `get_optimizer` 构造 `Muon` 时并未透传它们（[L306-L311](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L306-L311)），命令行也没有对应参数。因此实验第一步是把这两个旋钮接出来。**请在自己的副本上改，不要动仓库源文件。**

**操作步骤**：

1. 复制脚本：`cp examples/toy_train.py toy_train_grid.py`。
2. 给副本加三个命令行参数（argparse 部分，示例代码）：

   ```python
   parser.add_argument("--momentum", type=float, default=0.95)
   parser.add_argument("--ns_steps", type=int, default=5)
   parser.add_argument("--max_steps", type=int, default=100)
   ```

3. 给 `get_optimizer` 的签名增加 `momentum=0.95, ns_steps=5`，并在 `Muon(...)` 构造处透传 `momentum=momentum, ns_steps=ns_steps`（示例代码）。
4. 限制训练步数：在训练循环内加 `if step >= args.max_steps: break`。
5. 避免日志互相覆盖：loguru 的 `logger.add(file)` 对同一文件是追加模式，而原文件名不含超参（[L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327)），改成带超参的名字：

   ```python
   logger.add(f"logs/grid_muon_m{args.momentum}_ns{args.ns_steps}.log")
   ```

6. 用小模型加速（hidden_size 须为 16 的倍数，u1-l2），跑 3×3 网格（示例命令）：

   ```bash
   for m in 0.8 0.95 0.99; do
     for ns in 3 5 8; do
       python toy_train_grid.py --optimizer muon --hidden_size 512 \
         --momentum $m --ns_steps $ns --max_steps 100
     done
   done
   ```

7. 汇总：取每组最后 20 步 loss 的平均值（示例代码，可复用 u1-l3 搭的窗口平均仪表盘思路）：

   ```python
   import re, glob
   import numpy as np

   rows = []
   for path in sorted(glob.glob("logs/grid_muon_*.log")):
       losses = [float(x) for x in
                 re.findall(r"Training loss: ([0-9.]+)", open(path).read())]
       m, ns = re.search(r"m([0-9.]+)_ns(\d+)", path).groups()
       rows.append((float(m), int(ns), float(np.mean(losses[-20:]))))
   for m, ns, avg in sorted(rows):
       print(f"momentum={m}  ns_steps={ns}  last20_avg_loss={avg:.4f}")
   ```

**需要观察的现象与预期结果**（结合 4.1 的饱和度表与 u2-l2 的近似质量分析，待本地验证）：

| 组合 | 预期表现（待验证） |
| --- | --- |
| momentum=0.8 | 动量半衰期仅约 3 步，正交化输入噪声大，loss 曲线抖动明显 |
| momentum=0.95 | 默认值，预期最平稳 |
| momentum=0.99 | 100 步内缓冲仅达稳态约 63%，等效更新偏小，前期 loss 偏高 |
| ns_steps=3 | 正交化欠近似（输出奇异值带更宽），通常略差 |
| ns_steps=5 与 8 | 差异预期不大（docstring 自述少量迭代即足够），但 8 每步更慢 |

**一个实验设计要点**：本脚本 warmup 恰为 100 步（[L341-L346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L341-L346)），而 4.4 指出衰减与更新强度都随 lr 变化——所以这 100 步全部处在学习率爬坡段。所有组合共用同一条调度曲线，横向对比仍然公平，但结论应表述为「**预热期内**的超参敏感性」。若想观察峰值 lr 下的行为，可把 `num_warmup_steps` 临时调小（如 10）再跑一轮对照。

## 6. 本讲小结

- Muon 分支对每个二维参数执行六步流水线：取梯度 → 动量缓冲 \(\mathbf{b}_t = \mu\mathbf{b}_{t-1}+\mathbf{g}_t\) → Nesterov 前瞻 \(\tilde{\mathbf{g}}=\mathbf{g}+\mu\mathbf{b}\) → Newton-Schulz 正交化 → 解耦衰减 → 应用更新。
- `momentum_buffer` 惰性初始化于首次 `step()`，用 `zeros_like(g)` 自动对齐设备与 dtype，无梯度的参数被完整跳过（不更新也不衰减）。
- Nesterov 开关只改正交化输入：`g.add(buf, alpha=momentum)` 非就地、不污染梯度；`g = buf` 的别名安全性依赖正交化函数是纯函数这一隐式契约。
- `g.view(g.size(0), -1)` 展平分支是上游继承的通用写法，在当前实现中被 `__init__` 的 `assert p.ndim == 2` 封死，属于不可达的预备代码。
- 权重衰减以 `p.data.mul_(1 - lr*wd)` 解耦实现——正交化会抹掉幅度信息，衰减必须独立于梯度按固定比例收缩参数；这是 Moonlight 给 Muon 补上的关键改进之一。
- 更新用 `adjusted_lr = 0.2√max(A,B)·lr` 而衰减用原始 lr，这个不对称的完整意义是下一讲的主题。

## 7. 下一步学习建议

下一讲 **u2-l4《更新 RMS 一致化：adjust_lr_for_muon 的缩放设计》** 将专门拆解那个一闪而过的 `0.2 * math.sqrt(max(A, B))`：什么是更新 RMS、为什么不同形状参数的更新 RMS 要拉平到约 0.2、它与本讲的解耦权重衰减如何配合，构成 Moonlight 论文让 Muon「免调参、可规模化」的两大支柱。读完 u2-l4 后，再回看本讲 4.4 的练习 3 会有完整答案。之后 u2-l5 会补齐 `step()` 的另一半——AdamW 后备分支，与 `torch.optim.AdamW` 逐项对照。
