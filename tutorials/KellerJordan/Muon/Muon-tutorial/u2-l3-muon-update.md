# u2-l3 · muon_update 精读：动量、Nesterov、卷积权重与尺度缩放

## 1. 本讲目标

上一讲（u2-l2）我们啃掉了纯数学函数 `zeropower_via_newtonschulz5`：五次 Newton-Schulz 迭代、奇异值层面的标量映射、输出 \(US'V^\top\)。本讲顺着调用链往下走一步，精读它的**唯一调用者**——[muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) 的 `muon_update`。这 8 行是整个 Muon 的「算法总装车间」：优化器类（u1-l3 讲过的四个类）把梯度 `p.grad` 和动量缓冲 `state["momentum_buffer"]` 交给它，它返回一个可以直接乘以学习率施加到参数上的更新矩阵。

读完本讲，你应该能够：

1. **逐行解释这 8 行**：动量缓冲的 `lerp_` 更新、Nesterov 动量的两行写法、卷积核 `view` 成矩阵、调用 Newton-Schulz 正交化、最后的尺度缩放。
2. **证明一个漂亮的事实**：前两行动量运算产生的方向，与 `torch.optim.SGD(momentum=0.95, dampening=0, nesterov=True/False)` 的更新方向**只差一个常数倍** \((1-\beta)\)，而这个常数会被正交化的归一化抹掉——所以 docstring 里「Muon internally runs standard SGD-momentum」是字面成立的。
3. **说清楚 `grad.lerp_(momentum, beta)` 的副作用**：它原地改写了 `p.grad`，这是阅读源码时容易漏掉的细节。
4. **解释卷积核如何被当成矩阵**：`(out_channels, in_channels, kH, kW)` 如何被 `view(len(update), -1)` 压成 `(out_channels, in_channels·kH·kW)`，以及 2025 年 12 月一个针对此分支的真实 bug 修复（commit `6456082`）。
5. **推导尺度因子** `max(1, rows/cols)**0.5` 的效果：缩放后更新的逐元素 RMS 只由**列数**决定（\(\approx 1/\sqrt{\text{cols}}\)）、Frobenius 范数只由**行数**决定（\(\approx \sqrt{\text{rows}}\)），并用实验验证。

## 2. 前置知识

承接前几讲已建立的概念（一句话回顾）：

- **正交化**：把更新矩阵的奇异值全部拉到约 1（u2-l1）；`zeropower_via_newtonschulz5` 用五次迭代输出 \(US'V^\top\)，\(S'\) 的对角元约 \(\mathrm{Uniform}(0.5,1.5)\)（u2-l2）。
- **两把尺子**：Frobenius 范数 \(\|X\|_F = \sqrt{\sum_{ij}X_{ij}^2}\)（总能量）；谱范数 \(\sigma_{\max}\)（最大奇异值）。
- **优化器骨架**：`step()` 遍历参数 → 惰性初始化 `state["momentum_buffer"]` → 调 `muon_update` → 两行原地更新参数（u1-l3）。

本讲新增的基础概念：

| 概念 | 通俗解释 |
|---|---|
| `lerp_`（线性插值） | `t.lerp_(end, w)` 表示 `t ← t + w·(end − t)`，即从 `t` 向 `end` 滑动比例 `w`。等价展开：`t ← (1−w)·t + w·end`。`lerp_` 结尾的下划线表示**原地修改**。 |
| EMA（指数滑动平均） | \(m_t = \beta m_{t-1} + (1-\beta)g_t\)。历史梯度权重按 \(\beta^k\) 几何衰减，等效平均窗口约 \(1/(1-\beta)\) 步；\(\beta=0.95\) 时约 20 步。 |
| 求和型动量（经典 SGD 动量） | \(v_t = \beta v_{t-1} + g_t\)（PyTorch `SGD(dampening=0)` 的记账方式）。方向与 EMA 型完全相同，只差常数倍 \((1-\beta)\)。 |
| Nesterov 动量 | 「先按惯性前瞻一步，再在前方取梯度」。代数上等价于：更新方向里**最新梯度**的相对权重翻倍。 |
| RMS（均方根） | \(\mathrm{RMS}(X) = \sqrt{\tfrac{1}{mn}\sum_{ij}X_{ij}^2} = \|X\|_F/\sqrt{mn}\)，即「平均每个元素有多大」。Adam/Muon 社区常用它比较不同优化器的更新量纲。 |
| `view` 与 `reshape` | `view` 只换形状不拷贝数据（要求内存连续）；`reshape` 必要时会拷贝。卷积核梯度是连续的，`view` 零开销。 |
| fan-in / fan-out | 对 `nn.Linear(in, out)`，权重形状是 `(out, in)`：行数 = fan-out（输出维），列数 = fan-in（输入维）。本讲的尺度因子只看列数。 |

一个需要先想通的问题：`lerp_` 到底在算什么？把两处调用分别展开：

\[
\text{L35: } m \leftarrow m + (1-\beta)(g - m) = \beta m + (1-\beta)g
\]
\[
\text{L36: } g \leftarrow g + \beta(m - g) = (1-\beta)g + \beta m
\]

第一行是标准的 EMA 动量更新；第二行把「新鲜梯度」和「动量平均」按 \((1-\beta):\beta\) 混合——这就是 Nesterov 方向。两行合起来构成的代数结构，是 4.2 节的主角。

## 3. 本讲源码地图

| 文件与位置 | 角色 |
|---|---|
| [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) | **本讲主角**：`muon_update` 全函数（8 行） |
| [muon.py:L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31) | 上一讲精读的 `zeropower_via_newtonschulz5`，本函数第 39 行调用它 |
| [muon.py:L44-L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L64) | `Muon` 类 docstring：「内部跑标准 SGD 动量」、卷积核用法、lr 语义都在这里 |
| [muon.py:L88-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L88-L93) | 上层调用链（`Muon.step`）：初始化缓冲 → 调 `muon_update` → `update.reshape(p.shape)` 施加更新 |
| [muon.py:L120-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L120-L125) | `SingleDeviceMuon.step` 中的同款调用链（单机版） |
| [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135) | `adam_update`：第 131 行 `buf1.lerp_(grad, 1 - betas[0])` 与本讲的 EMA 记账同款（u3-l1 伏笔） |
| [README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47) | 超参建议：momentum/nesterov/ns_steps 默认即可，只调 lr 与 weight_decay |
| [commit 6456082](https://github.com/KellerJordan/Muon/commit/64560829f70a216f8263ea8ae0fefeac8b451d36) | 2025-12-04 修复卷积参数尺度因子的 bug（4.3 节详讲） |

先给 `muon_update` 画一张内部地图（后面逐段精读）：

| 行号 | 代码 | 作用 |
|---|---|---|
| [L34](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34) | `def muon_update(grad, momentum, beta=0.95, ns_steps=5, nesterov=True):` | 签名：梯度、动量缓冲（**均会被原地修改**）、三个默认超参 |
| [L35](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L35) | `momentum.lerp_(grad, 1 - beta)` | EMA 动量更新（4.2 节） |
| [L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L36) | `update = grad.lerp_(momentum, beta) if nesterov else momentum` | Nesterov 方向或直接用动量（4.2 节；注意原地改 `grad`） |
| [L37-L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L38) | `if update.ndim == 4: update = update.view(len(update), -1)` | 卷积核压成矩阵（4.3 节） |
| [L39](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39) | `update = zeropower_via_newtonschulz5(update, steps=ns_steps)` | 正交化（4.4 节） |
| [L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40) | `update *= max(1, update.size(-2) / update.size(-1))**0.5` | 尺度缩放（4.5 节） |

## 4. 核心概念与源码讲解

### 4.1 muon_update 总览：五步流水线与调用契约

#### 4.1.1 概念说明

`muon_update` 是**无状态数学**（`zeropower_via_newtonschulz5`）与**有状态编排**（优化器类的 `step()`）之间的桥。它自己不是 `torch.optim.Optimizer` 的方法，而是一个普通函数，但通过「传入缓冲、原地改写」的方式携带了状态。四个优化器类的 `step()` 都把最难的工作委托给它：

- [muon.py:L91](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L91)（`Muon.step`，分布式）
- [muon.py:L123](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L123)（`SingleDeviceMuon.step`）
- [muon.py:L205](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L205)（`MuonWithAuxAdam.step`）
- [muon.py:L267](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267)（`SingleDeviceMuonWithAuxAdam.step`）

四处调用的写法完全一样：`muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])`——只传了 `beta`，`ns_steps=5` 和 `nesterov=True` 永远用默认值（这与 u1-l3 的结论呼应：这两个超参没有暴露到构造函数）。

#### 4.1.2 核心流程

```
输入: grad（本步梯度）, momentum（动量缓冲, 会被改写）, beta, ns_steps, nesterov

① 动量      momentum ← β·momentum + (1−β)·grad          # EMA, 原地
② 方向      update ← (1−β)·grad + β·momentum   (nesterov=True, 原地改写 grad)
       或   update ← momentum                  (nesterov=False)
③ 展平      若 update.ndim == 4: update ← update.view(第0维, 其余维度乘积)   # 仅卷积核
④ 正交化    update ← zeropower_via_newtonschulz5(update, steps=ns_steps)    # u2-l2
⑤ 缩放      update ← update · max(1, 行数/列数)^0.5
返回: update（bfloat16；形状与③之后的 update 相同）
```

调用方（以 [muon.py:L92-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L92-L93) 为例）随后做两件事：解耦权重衰减 `p.mul_(1 - lr*wd)`，再 `p.add_(update.reshape(p.shape), alpha=-lr)` 把更新放回**原始形状**——`reshape(p.shape)` 正是为了接住第 ③ 步把 4D 卷积核压成 2D 的情况。

#### 4.1.3 源码精读

完整函数只有 8 行（[muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41)）：

```python
def muon_update(grad, momentum, beta=0.95, ns_steps=5, nesterov=True):
    momentum.lerp_(grad, 1 - beta)
    update = grad.lerp_(momentum, beta) if nesterov else momentum
    if update.ndim == 4: # for the case of conv filters
        update = update.view(len(update), -1)
    update = zeropower_via_newtonschulz5(update, steps=ns_steps)
    update *= max(1, update.size(-2) / update.size(-1))**0.5
    return update
```

读签名时请注意三个「调用契约」：

1. **两个输入参数都会被原地修改**：`momentum.lerp_` 改写动量缓冲（这是有意的状态更新）；`grad.lerp_` 在 `nesterov=True` 时改写梯度本身（副作用，见 4.2.3 的警告）。
2. **返回值是 bfloat16**：因为第 ④ 步的 `zeropower_via_newtonschulz5` 内部转 bfloat16 计算并直接返回（[muon.py:L17](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L17)），第 ⑤ 步乘一个 Python 浮点数不改变 dtype。
3. **返回形状是「展平后」的形状**：4D 输入返回 2D，调用方负责 `reshape(p.shape)`。

这个「签名简单、契约靠注释和习惯维持」的风格贯穿 muon.py——没有类型标注、没有输入校验（除了 NS5 里那一条 `assert G.ndim >= 2`），是典型的研究型代码。

#### 4.1.4 代码实践

**实践目标**：建立对调用契约的手感——确认形状、dtype、副作用与确定性。

操作步骤（示例代码，保存为任意 `.py` 文件运行；环境准备见 u1-l2）：

```python
import torch
from muon import muon_update

torch.manual_seed(0)
g = torch.randn(256, 256)
mom = torch.zeros_like(g)

u = muon_update(g, mom)
print("输出形状:", u.shape, " dtype:", u.dtype)
print("输出 Frobenius 范数:", u.float().norm().item())
print("调用后 momentum 是否仍为全零:", bool((mom == 0).all()))   # 预期 False：被 L35 原地改写
print("调用后 grad 是否仍等于原始值:", bool(torch.equal(g, g)))  # 恒 True，无信息量；改用下面的对照

# 换一份梯度对照，验证 nesterov 分支会改写 grad（4.2 节的副作用）
g2 = torch.randn(256, 256); g2_copy = g2.clone(); mom2 = torch.zeros_like(g2)
muon_update(g2, mom2)
print("grad 是否被原地修改:", not torch.equal(g2, g2_copy))      # 预期 True

# 确定性：同样输入（重置缓冲）→ 完全相同的输出
mom_a = torch.zeros_like(g); mom_b = torch.zeros_like(g)
ua = muon_update(g.clone(), mom_a)
ub = muon_update(g.clone(), mom_b)
print("两次调用输出是否逐位相同:", torch.equal(ua, ub))          # 预期 True
```

需要观察的现象与预期结果：

- 输出形状 `(256, 256)`、dtype `torch.bfloat16`；
- `momentum` 缓冲被改写（不再全零）；
- `grad` 也被原地改写（`g2` 与其拷贝不再相等）；
- 函数自身无状态，同样输入两次调用逐位相同；
- 输出 Frobenius 范数应在 8 到 24 之间（理论值 \(\sqrt{256}=16\)，实际奇异值约在 \([0.5,1.5]\) 波动，所以范数在 \(0.5\cdot16\) 到 \(1.5\cdot16\) 之间；见 4.5 节推导）。（具体数值待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：四个优化器类调用 `muon_update` 时都只传了 `beta`，`ns_steps` 和 `nesterov` 用默认值。请从源码中找出这会导致哪些超参组合**永远不可能**出现。

**答案**：`ns_steps` 恒为 5、`nesterov` 恒为 `True`。构造函数（如 [muon.py:L65](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65)）只接收 `lr/weight_decay/momentum`，没有任何入口改这两个参数；想改只能复制源码自己维护（u4-l3 的主题）。

**练习 2**：为什么调用方施加更新时写 `update.reshape(p.shape)` 而不是 `update.view(p.shape)`？

**答案**：`update` 是正交化后的新张量，内存上连续，两者其实都能用；但 `reshape` 更稳健——不依赖目标形状与现有 stride 兼容。更关键的原因是语义：`muon_update` 返回的可能是 2D（卷积核被展平过），必须变回 `p.shape` 才能与 4D 参数做 `add_`。参考 [muon.py:L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L93)。

**练习 3**：列出 `muon_update` 的全部副作用。

**答案**：① 原地改写 `momentum`（EMA 更新，有意为之，这就是优化器状态）；② 当 `nesterov=True` 时原地改写 `grad`（`grad.lerp_`）；③ 返回的新张量与输入不共享内存（`view` 除外——第 ③ 步的 view 与输入共享，但随后第 ④ 步产生了新张量）。

### 4.2 动量与 Nesterov：两行 lerp_ 的数学

#### 4.2.1 概念说明

[muon.py:L35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L35-L36) 这两行实现了「带 Nesterov 选项的动量」。`Muon` 类 docstring 明说（[muon.py:L50-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L50-L53)）：

> Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-processing step...

本节要证明这句话**字面成立**：这两行的产出方向与 `torch.optim.SGD(momentum=0.95, dampening=0)` 完全一致，只是记账方式（EMA 型 vs 求和型）差一个常数倍——而这个常数会被第 ④ 步的 Frobenius 归一化抹掉。理解了这一点，你就知道 Muon 对动量的「创新」不在动量本身，而全在正交化后处理。

#### 4.2.2 核心流程

**第 35 行（动量更新）**：

\[
m_t = \beta\, m_{t-1} + (1-\beta)\, g_t, \qquad m_0 = 0
\]

展开成历史加权和（这解释了「动量是对历史的记忆」）：

\[
m_t = (1-\beta)\sum_{k=0}^{t-1} \beta^k \, g_{t-k}
\]

**第 36 行（Nesterov 方向）**：

\[
d_t = (1-\beta)\, g_t + \beta\, m_t \quad (\text{nesterov=True}), \qquad d_t = m_t \quad (\text{nesterov=False})
\]

把 \(m_t\) 的展开式代入 Nesterov 方向，得到历史权重表（\(\beta = 0.95\)）：

| 方向 | \(g_t\)（最新） | \(g_{t-1}\) | \(g_{t-2}\) | 最新:上一条 权重比 |
|---|---|---|---|---|
| 普通动量 \(m_t\) | 5.00% | 4.75% | 4.51% | \(1 : 0.95\)（几乎平权） |
| Nesterov \(d_t\) | 9.75% | 4.51% | 4.29% | \(\approx 2.16 : 1\) |

（Nesterov 权重的推导：\(d_t = (1-\beta)(1+\beta)g_t + (1-\beta)\beta^2 g_{t-1} + \cdots\)，最新:上一条 \(= (1+\beta)/\beta^2 \approx 2.16\)。）

**直觉**：Nesterov 动量相当于「先顺着惯性往前迈半步，再在新的位置看梯度」——几何上等价于把最新梯度的相对权重提高约一倍。动量平均回答「过去 20 步整体往哪走」，Nesterov 方向在此之上叠加「刚看到的这步更要紧」。

**与 `torch.optim.SGD` 的精确等价**（本讲最重要的推导）。设 PyTorch 求和型缓冲 \(v_t = \beta v_{t-1} + g_t\)，\(v_1 = g_1\)（PyTorch 用第一步梯度初始化缓冲；Muon 用全零初始化 EMA，见 [muon.py:L90](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L90)）。用数学归纳法可以证明对**所有** \(t\):

\[
m_t = (1-\beta)\, v_t
\]

（归纳：\(m_t = \beta m_{t-1} + (1-\beta)g_t = \beta(1-\beta)v_{t-1} + (1-\beta)g_t = (1-\beta)(\beta v_{t-1} + g_t) = (1-\beta)v_t\)。）

于是：

\[
\underbrace{d_t^{\text{Muon}}}_{\text{nesterov}} = (1-\beta)g_t + \beta m_t = (1-\beta)\bigl(g_t + \beta v_t\bigr) = (1-\beta)\, \underbrace{d_t^{\text{PyTorch SGD(nesterov=True)}}}_{g_t + \beta v_t}
\]
\[
d_t^{\text{Muon}}\big|_{\text{nesterov=False}} = m_t = (1-\beta)\, v_t = (1-\beta)\, d_t^{\text{PyTorch SGD(nesterov=False)}}
\]

**结论**：两行 `lerp_` 的方向与标准 SGD 动量逐位相同，只差整体常数 \((1-\beta)=0.05\)。而 `zeropower_via_newtonschulz5` 第一步就除以 Frobenius 范数（[muon.py:L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L22)），对整体缩放完全不敏感——所以 EMA 记账与求和记账在 Muon 里**没有任何可观测区别**，选 EMA 型只是与 Adam 的 `exp_avg` 记账统一（对照 [muon.py:L131](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L131)）。

还有一个值得预判的现象：**第一步**（\(t=1\)，缓冲从零开始）时 \(m_1 = (1-\beta)g_1\)，两个方向都严格平行于 \(g_1\)——此时 nesterov 开关不产生任何方向差异；差异要等梯度随步数**转向**后才显现（练习实验里会用到这一点）。

#### 4.2.3 源码精读

```python
momentum.lerp_(grad, 1 - beta)                                        # L35
update = grad.lerp_(momentum, beta) if nesterov else momentum        # L36
```

- [L35](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L35)：`momentum ← momentum + (1−β)(grad − momentum)`。注意实参名就叫 `momentum`，它就是优化器 `state` 里那个缓冲（在 [muon.py:L90/L122](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L90) 创建），原地写回等价于更新状态。
- [L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L36)：三重身份——
  1. **条件表达式**选方向：Nesterov 用混合方向，否则直接用动量；
  2. `grad.lerp_` 是**原地操作**：`p.grad` 这个张量本身被改写了。之所以无碍，是因为 `step()` 结束后梯度会在下一轮 `zero_grad()` + 反向传播时重新生成；但如果你在训练循环里复用同一个 `p.grad`（比如做梯度累积、或调用两次 `step()` 不重算梯度），这个副作用就会咬人。自己写实验时想保留原始梯度，请传 `g.clone()`；
  3. `nesterov=False` 分支的 `update = momentum` 不是拷贝而是**别名**——好在后续第 ④ 步 `update = zeropower...` 立刻重新绑定，momentum 不会在第 ④⑤ 步被误改。

#### 4.2.4 代码实践

**实践目标**：用实验回答三个问题——(a) nesterov 开关对**返回的更新**的范数几乎无影响（幅度由形状决定）；(b) 但对**方向**有影响，且首步看不出来；(c) EMA 记账与求和型记账方向一致。

操作步骤（示例代码）：

```python
import torch
from muon import muon_update

torch.manual_seed(0)
m_shape = n_shape = 256; T, beta = 50, 0.95

# 构造一条方向缓慢漂移的"梯度流"：每步保留 90% 旧方向 + 新噪声
g_list, g = [], torch.randn(m_shape, n_shape)
for t in range(T):
    g = 0.9 * g + 0.15 * torch.randn(m_shape, n_shape)
    g_list.append(g.clone())

rms = lambda x: x.float().pow(2).mean().sqrt()
cos = lambda a, b: torch.cosine_similarity(a.flatten().float(), b.flatten().float(), dim=0)

# (a)(b) 两条独立轨迹：nesterov 开 vs 关
mom_n, mom_s = torch.zeros(m_shape, n_shape), torch.zeros(m_shape, n_shape)
v = None  # 求和型缓冲（PyTorch SGD 记账），用于 (c)
for t, g in enumerate(g_list):
    u_n = muon_update(g.clone(), mom_n, beta=beta, nesterov=True)
    u_s = muon_update(g.clone(), mom_s, beta=beta, nesterov=False)
    if t in (0, 4, 24, 49):
        print(f"t={t:>2}  nesterov: ‖u‖_F={u_n.float().norm():6.2f} RMS={rms(u_n):.4f} | "
              f"plain: ‖u‖_F={u_s.float().norm():6.2f} RMS={rms(u_s):.4f} | "
              f"cos(u_nesterov, u_plain)={cos(u_n, u_s):.4f}")

# (c) 验证 m_t == (1-beta) * v_t（方向与 torch.optim.SGD 一致）
torch.manual_seed(0)                       # 重新生成同一条梯度流
g = torch.randn(m_shape, n_shape); mom = torch.zeros(m_shape, n_shape)
for t in range(1, T + 1):
    if t > 1:
        g = 0.9 * g + 0.15 * torch.randn(m_shape, n_shape)
    mom.lerp_(g, 1 - beta)                 # 复刻 L35
    v = g.clone() if v is None else beta * v + g   # PyTorch 求和型
    ratio = (mom.float() / v.float()).median()
    if t in (1, 10, 50):
        print(f"t={t:>2}  m_t / v_t 的中位数 = {ratio:.4f}（理论值 1-beta = {1-beta}）")
```

需要观察的现象与预期结果：

- (a) 每一行两种设置的 `‖u‖_F` 与 RMS 几乎相同（都围绕 \(\sqrt{256}=16\) 与 \(1/16=0.0625\) 波动，单次落点由奇异值分布决定）——**幅度被正交化+缩放完全接管，与动量方式无关**；
- (b) `cos` 在 `t=0` 时等于（或极其接近）1.0000（首步两个方向都平行于 \(g_1\)），随后逐渐降到 1 以下（梯度转向后两种方向出现真实分歧；漂移越快分歧越大）；
- (c) `m_t / v_t` 的中位数在三 个时刻都 ≈ 0.0500，验证 \(m_t = (1-\beta)v_t\)。
- 以上具体数值**待本地验证**；若 (c) 出现偏差，检查是否漏掉了「PyTorch 缓冲用 \(v_1 = g_1\) 初始化」这一细节（本脚本的 `v is None` 分支处理了它）。

#### 4.2.5 小练习与答案

**练习 1**：不运行代码，推断「首步（动量缓冲全零）时 `nesterov=True` 与 `False` 返回的更新是否相同」，并说明理由。

**答案**：相同（方向意义上）。\(m_1 = (1-\beta)g_1\)，Nesterov 方向 \(= (1-\beta)g_1 + \beta\, m_1 = (1-\beta)g_1 + \beta(1-\beta)g_1 = (1-\beta)(1+\beta)\,g_1 = (1-\beta^2)\,g_1\)，与普通动量方向 \(m_1\) 都严格平行于 \(g_1\)，只差正常数倍；NS5 的归一化把常数抹掉，输出完全一致。

**练习 2**：如果把 `beta` 从 0.95 改成 0.5，动量的「记忆窗口」和 Nesterov 的「最新梯度强化倍数」分别怎么变？

**答案**：记忆窗口 \(1/(1-\beta)\) 从 20 步缩短到 2 步；Nesterov 的最新:上一条权重比 \((1+\beta)/\beta^2\) 从约 2.16 提高到 \(1.5/0.25 = 6\)——\(\beta\) 越小，Nesterov 对新鲜梯度的强调越强。

**练习 3**：为什么说「EMA 记账 vs 求和型记账」的选择在 Muon 中无关紧要，但在 `torch.optim.SGD` 中会实质影响参数更新？

**答案**：两种记账只差常数 \((1-\beta)\)。SGD 直接拿缓冲乘 lr 更新参数，常数会改变有效步长（所以 SGD 的 momentum=0.95 配 lr 时，缓冲放大的 ~20 倍是必须考虑的因素）；而 Muon 在动量之后立刻做 Frobenius 归一化 + 正交化，整体尺度信息本来就被丢弃，方向才是唯一被保留的东西。

### 4.3 卷积核展平：ndim==4 的 view 分支

#### 4.3.1 概念说明

正交化只对**二维矩阵**有意义（奇异值、\(UV^\top\) 都是矩阵概念），但卷积层的权重是 4 维张量：`nn.Conv2d` 的 `weight` 形状为 \((O,\, C_{in}/g,\, k_H,\, k_W)\)（输出通道、每组输入通道、核高、核宽）。`Muon` docstring 给出了官方口径（[muon.py:L57-L58](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L57-L58)）：

> Hidden convolutional weights can be trained using Muon by viewing them as 2D and then collapsing their last 3 dimensions.

即把每个输出通道的**全部**感受野参数（\(C_{in}/g \times k_H \times k_W\) 个数）当作一「行」，得到 \(O \times (C_{in}k_Hk_W/g)\) 的矩阵再正交化。README 对 ConvNet 的建议同款（[README.md:L35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L35-L36)：用 Muon 优化除第一层外的所有卷积核）。

#### 4.3.2 核心流程

```
输入 update.ndim == 4，形状 (O, C, kH, kW)
        │
        ▼
update.view(len(update), -1)     # len(update) == update.size(0) == O
        │                        # -1 自动推算为 C·kH·kW
        ▼
形状 (O, C·kH·kW) 的矩阵 → 进入正交化
        │
        ▼ （在调用方 step() 里）
update.reshape(p.shape)           # 放回 (O, C, kH, kW)
```

`view` 不搬运数据、只改「解读方式」：内存里连续的 \(O \cdot C \cdot k_H \cdot k_W\) 个数，从 4 维索引换成 2 维索引。

#### 4.3.3 源码精读

```python
if update.ndim == 4: # for the case of conv filters        # L37
    update = update.view(len(update), -1)                   # L38
```

- [L37](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37)：只处理 `ndim == 4`。二维权重（Linear/Embedding）不进分支；**三维张量也不进分支**——它会原样传给 NS5，而 NS5 对 `ndim >= 2` 的输入按「最后两维是矩阵、前面是批量维」处理（[muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15) 的批量实现注释）。也就是说，若真有 3 维参数混进 Muon 组，它不会报错，而是被**逐切片各自正交化**——这不是文档承诺的行为，划分参数时应避免（README 的 `p.ndim >= 2` 划分规则在纯 transformer/convnet 里只会产生 2D/4D，天然安全）。
- [L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L38)：`len(update)` 是张量第 0 维大小（Python `len()` 语义），等价于 `update.size(0)`。

**一段真实的修复历史**（学习源码的绝佳案例）。2025 年 12 月的 [commit 6456082](https://github.com/KellerJordan/Muon/commit/64560829f70a216f8263ea8ae0fefeac8b451d36)（"Fix the adjusted lr for conv params"）修掉了缩放行的一个潜伏 bug：

```diff
-    update *= max(1, grad.size(-2) / grad.size(-1))**0.5
+    update *= max(1, update.size(-2) / update.size(-1))**0.5
```

旧代码用的是 **`grad`（4 维）** 的最后两维——对卷积核来说那是 \((k_H, k_W)\)，核的**空间尺寸**，跟矩阵的行数列数毫无关系！它之所以长期没被发现，是因为常见核都是方的（\(7\times7\)、\(3\times3\)：\(k_H/k_W = 1\)，因子恰好为 1，等于没缩放）。对非方核则给出完全错误的因子——练习 3 会让你算一笔账。修复后的 `update` 是展平后的矩阵，`.size(-2)/.size(-1)` 才是真正的行数/列数。**教训**：同一行代码里变量的「形状语义」随分支改变（4D→2D），引用错变量不会报错，只会悄悄算错。

#### 4.3.4 代码实践

**实践目标**：验证卷积核走 `view` 分支的结果与手动路径逐位一致，并体会 reshape 回原形状的位置。

操作步骤（示例代码）：

```python
import torch
from muon import muon_update, zeropower_via_newtonschulz5

torch.manual_seed(0)
conv_grad = torch.randn(64, 3, 7, 7)          # 模拟 Conv2d(out=64, in=3, kernel=7x7) 的梯度
mom = torch.zeros_like(conv_grad)

u = muon_update(conv_grad, mom)
print("muon_update 输出形状:", u.shape)         # 预期 torch.Size([64, 147])，即 (64, 3*7*7)

# 手动复刻 L37-L40：展平 → 正交化 → 缩放
flat = conv_grad.view(len(conv_grad), -1)      # (64, 147)
manual = zeropower_via_newtonschulz5(flat, steps=5)
manual *= max(1, manual.size(-2) / manual.size(-1))**0.5
print("与手动路径逐位相同:", torch.equal(u, manual))   # 预期 True

# 调用方视角：step() 里的 reshape（对照 muon.py L93/L125）
p = torch.randn_like(conv_grad)                # 假装这是参数本身
p.add_(u.reshape(p.shape), alpha=-0.02)        # 必须先变回 (64,3,7,7) 才能 add_
print("reshape 后形状:", u.reshape(p.shape).shape)
```

需要观察的现象与预期结果：

- `muon_update` 返回 `(64, 147)` 的 **2D** 张量（147 = 3×7×7）；
- `torch.equal(u, manual)` 为 `True`（两条路径的算子与顺序完全一致，CPU 上确定执行）；
- `u.reshape(p.shape)` 恢复 `(64, 3, 7, 7)`。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：`update.view(len(update), -1)` 中的 `-1` 会被推算成什么？写出 `(128, 64, 3, 3)` 与 `(32, 16, 5, 5)` 两种卷积核展平后的形状。

**答案**：`-1` = 其余维度乘积 \(C \cdot k_H \cdot k_W\)。(128,64,3,3) → (128, 576)；(32,16,5,5) → (32, 400)。

**练习 2**：三维张量（比如 Conv1d 的权重 \((O, C_{in}, k)\)）传进 `muon_update` 会发生什么？

**答案**：不进 `ndim==4` 分支、不报错，直接作为 \((O, C_{in}, k)\) 传给 NS5；NS5 按「批量维 + 矩阵」处理（断言只要求 `ndim >= 2`，[muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15)），于是 \(O\) 个 \((C_{in}, k)\) 小矩阵**各自**正交化，最后整体乘一个 `max(1, C_in/k)^0.5`。这与「整层权重联合正交化」的语义完全不同，属于未定义用法。

**练习 3**（历史 bug 复盘）：用旧代码（`grad.size(-2)/grad.size(-1)`）和新代码分别计算形状 `(64, 3, 3, 1)` 卷积核的缩放因子，旧代码让这层的有效学习率差多少倍？

**答案**：新代码：view 后为 (64, 3)，因子 \(\sqrt{\max(1, 64/3)} = \sqrt{21.33} \approx 4.62\)。旧代码：直接取核的 \((k_H, k_W) = (3, 1)\)，因子 \(\sqrt{\max(1, 3/1)} = \sqrt{3} \approx 1.73\)。两者之比 \(4.62/1.73 \approx 2.67\)——旧代码让这类非方核卷积层的更新被低估约 2.7 倍（等效于 lr 缩小 2.7 倍）。方核（\(k_H = k_W\)）时旧代码因子恒为 1，恰好无害，这正是 bug 潜伏至今的原因。

### 4.4 正交化调用：把方向交给 zeropower_via_newtonschulz5

#### 4.4.1 概念说明

[muon.py:L39](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39) 一行完成从「方向」到「正交更新」的转换。它本身没有新数学（全部内容在 u2-l2），本节讲的是**调用契约**——函数签名背后的四条性质，它们是理解 4.5 节尺度缩放、以及上一节「常数倍无关紧要」论断的前提。

#### 4.4.2 核心流程

```
update（float32, 形状 (..., m, n)）
        │
        ▼  zeropower_via_newtonschulz5(update, steps=ns_steps)
        │   ① 转 bfloat16                          (L17)
        │   ② 若 m > n 先转置（Gram 矩阵更小）      (L18-L19)
        │   ③ 除以 Frobenius 范数 + 1e-7            (L22)   ← 尺度不变性的来源
        │   ④ 五次 X ← aX + (bA + cA²)X 迭代        (L24-L27)
        │   ⑤ 转回原方向                            (L29-L30)
        ▼
输出（bfloat16, 形状与输入完全相同, 奇异值 ≈ Uniform(0.5, 1.5)）
```

四条调用契约（均可从 u2-l2 的推导直接得出）：

1. **形状保持**：进去什么形状，出来什么形状（内部转置会转回来）。
2. **尺度不变**：\(\text{NS5}(cX) = \text{NS5}(X)\)（\(c>0\)，第 ③ 步除以 Frobenius 范数把整体幅度抹掉；`+1e-7` 与 bfloat16 舍入带来的偏差可忽略）。
3. **方向改写**：奇异向量配对保留，但每个奇异值被改写到约 \([0.5,1.5]\)，**不再保留输入的相对奇异值结构**——「哪个方向强」的信息在这一步被有意丢弃。
4. **精度降低**：返回 bfloat16，后续数值测量应先 `.float()`。

#### 4.4.3 源码精读

```python
update = zeropower_via_newtonschulz5(update, steps=ns_steps)   # L39
```

调用时 `steps=ns_steps` 是关键字传参，而 `ns_steps` 来自函数默认值 5（调用方从未覆盖）。结合 u2-l2 的结论「steps=5 与 steps=10 的奇异值范围都大致落在 \([0.5,1.5]\)，10 步不会更接近全 1」，这个默认值是「精度换速度」的定案选择，README 也把它列入「无需调整」清单（[README.md:L46](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L46)）。

值得强调契约 2 的一个推论：`muon_update` 的最终输出**不携带梯度的幅度信息**——梯度暴涨 100 倍，输出范数几乎不变。这是特性不是缺陷：更新幅度由「形状 + 尺度因子 + lr」三元组完全决定（下一节），使 Muon 对损失曲面的局部剧烈程度天然鲁棒。

#### 4.4.4 代码实践

**实践目标**：用最小实验验证契约 2（尺度不变性），它是 4.2 节等价性论证与 4.5 节幅度推导的基石。

操作步骤（示例代码）：

```python
import torch
from muon import zeropower_via_newtonschulz5

torch.manual_seed(0)
X = torch.randn(200, 100)
u1 = zeropower_via_newtonschulz5(X, steps=5)
u2 = zeropower_via_newtonschulz5(100.0 * X, steps=5)
u3 = zeropower_via_newtonschulz5(0.01 * X, steps=5)
for name, u in [("x1", u1), ("x100", u2), ("x0.01", u3)]:
    print(f"{name:>6}:  ‖u‖_F={u.float().norm():.3f}  RMS={u.float().pow(2).mean().sqrt():.5f}")
print("u(x) 与 u(100x) 的最大绝对差:",
      (u1.float() - u2.float()).abs().max().item())
```

需要观察的现象与预期结果：

- 三组输出的 Frobenius 范数与 RMS 在 bfloat16 舍入误差内一致（理论期望 \(\|u\|_F \approx \sqrt{100}=10\)、\(\mathrm{RMS} \approx 1/\sqrt{200}\approx 0.0707\)，单次因奇异值分布波动）；
- `u(x)` 与 `u(100x)` 的最大绝对差应在 1e-2 量级以下（bfloat16 相对精度约 0.4%，元素量级约 0.07）。（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：某个参数的梯度因为损失函数数值问题突然放大 1000 倍，`muon_update` 返回的更新范数会怎样？

**答案**：几乎不变。NS5 第 ③ 步除以 Frobenius 范数（[muon.py:L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L22)），任何正常数倍缩放都在归一化中消去（前提是范数远大于 1e-7 且不溢出 bfloat16 范围）。但注意：动量缓冲本身仍会被这个大梯度污染，后续多步的方向会受影响。

**练习 2**：既然 NS5 输出奇异值约 \(\mathrm{Uniform}(0.5,1.5)\) 而非全 1，4.5 节用「奇异值 ≈ 1」做推导还合理吗？

**答案**：合理，但要带着误差意识。单个矩阵的 Frobenius 范数会在 \(0.5\sqrt{\min(m,n)}\) 到 \(1.5\sqrt{\min(m,n)}\) 之间波动，期望值为 \(\sqrt{\min(m,n)}\)（\(E[\sigma]\approx 1\)）。所以 4.5 的公式是「期望意义」的恒等式，实测数值会围绕它波动——实验里打印的正是这种波动。

### 4.5 尺度缩放：max(1, rows/cols)\*\*0.5 与更新的量纲

#### 4.5.1 概念说明

[muon.py:L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40) 是 `muon_update` 的最后一行，也是最容易被略读的一行——它只是一次标量乘法，却决定了「lr=0.02 到底意味着什么」。`Muon` docstring 对 lr 的定义是（[muon.py:L60-L63](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L60-L63)）：

> lr: The learning rate, in units of spectral norm per update.

尺度因子的职责：在「谱范数单位」的基础上做**形状校正**，让同一个 lr 在不同形状的参数上产生可比的逐元素更新幅度——这是 README 声称「学习率内建 muP 缩放、放大模型不用重调」（[README.md:L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L47)）的机制来源之一。

#### 4.5.2 核心流程

设正交化后的更新为 \(m \times n\) 矩阵（\(m=\) 行数 `size(-2)`，\(n=\) 列数 `size(-1)`），奇异值约 1、共 \(\min(m,n)\) 个。逐项推导：

**未缩放时的范数**（RMS = Frobenius/√元素数）：

\[
\|U\|_F \approx \sqrt{\min(m,n)}, \qquad
\mathrm{RMS}(U) = \frac{\|U\|_F}{\sqrt{mn}} \approx \frac{1}{\sqrt{\max(m,n)}}
\]

**尺度因子**：

\[
f = \max\!\left(1,\ \frac{m}{n}\right)^{1/2} =
\begin{cases}
\sqrt{m/n} & m > n \ \text{（高瘦矩阵）}\\[4pt]
1 & m \le n \ \text{（方阵 / 宽矩阵）}
\end{cases}
\]

**缩放后的范数**——代入两种情形化简，得到两条统一的规律：

\[
\|fU\|_F \approx \sqrt{m} = \sqrt{\text{行数}}, \qquad
\mathrm{RMS}(fU) \approx \frac{1}{\sqrt{n}} = \frac{1}{\sqrt{\text{列数}}}
\]

（高瘦情形验证：\(\sqrt{n}\cdot\sqrt{m/n} = \sqrt{m}\)，\(\tfrac{1}{\sqrt{m}}\cdot\sqrt{\tfrac{m}{n}} = \tfrac{1}{\sqrt{n}}\)；宽矩阵情形 \(f=1\)，\(\sqrt{\min}=\sqrt m\)、\(\tfrac{1}{\sqrt{\max}}=\tfrac1{\sqrt n}\)，直接成立。）

**用 (512,1024) 与 (1024,512) 对照**（数值取期望值）：

| 形状 | 未缩放 RMS | 因子 \(f\) | 缩放后 RMS | 缩放后 \(\|\cdot\|_F\) | 缩放后谱范数 |
|---|---|---|---|---|---|
| (512, 1024) 宽 | \(1/\sqrt{1024}\approx 0.0313\) | 1 | \(0.0313\) | \(\sqrt{512}\approx 22.6\) | \(\approx 1\) |
| (1024, 512) 高瘦 | \(1/\sqrt{1024}\approx 0.0313\) | \(\sqrt 2\approx 1.414\) | \(1/\sqrt{512}\approx 0.0442\) | \(\sqrt{1024}=32\) | \(\approx\sqrt2\) |

注意两个反直觉的点：

1. **未缩放时**两种形状的 RMS 反而相同（都 \(= 1/\sqrt{\max(m,n)}\)，转置对称）；**缩放后**不再相同（差 \(\sqrt 2\)）——尺度因子专门「抬高」行多于列的矩阵。
2. 缩放后的一致性是一条**规则**而非一个数值：「逐元素 RMS 只由列数决定」。对 `nn.Linear(in, out)`（权重 `(out, in)`），列数 = fan-in，即**更新的逐元素幅度只由输入维决定**。

**为什么这是想要的性质**：

- **同一个 lr 管所有层**。参数每步实际变化 \(\mathrm{lr} \cdot fU\)，其逐元素 RMS \(= \mathrm{lr}/\sqrt{\text{fan-in}}\)——只依赖该层输入宽度。模型加深（行数变化）不改变任何一层的更新幅度，这与 muP 式「放大模型不必重调 lr」的方向一致（[README.md:L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L47)）。
- **与 AdamW 的量纲可比**。AdamW 的逐元素更新是 \(O(1)\) 的（除以了梯度的 RMS），Muon 缩放后是 \(O(1/\sqrt{\text{fan-in}})\)——同量级、可对比，这也是为什么 Muon 组 lr≈0.02 与 Adam 组 lr≈3e-4 相差两个数量级仍各自合理（u1-l3 的结论在这里有了数值解释）。Kimi.ai 在大规模训练中进一步把更新乘以 \(0.2\sqrt{\max(m,n)}\)，让逐元素 RMS 精确对齐 AdamW 的 0.2——那是在本行基础上的追加调整（见 [README.md:L65](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L65) 链接的 K2 技术报告）。
- **谱范数语义近似保留**。宽/方阵缩放后谱范数仍 \(\approx 1\)，docstring 的「lr 以谱范数为单位」字面成立；高瘦阵被抬到 \(\approx\sqrt{m/n}\)，可理解为「谱范数单位 + 宽度校正」。

#### 4.5.3 源码精读

```python
update *= max(1, update.size(-2) / update.size(-1))**0.5   # L40
```

- `update.size(-2)` / `update.size(-1)`：取**最后两维**（而非 `size(0)`/`size(1)`）——与 NS5 的批量风格一致，理论上对 3 维以上的张量也「能算」，但对正规 2D 路径就是行数/列数。`/` 在 Python 3 中是真除法，得到 float，`**0.5` 即开方。
- 原地 `*=`：bfloat16 张量乘 Python float，dtype 保持 bfloat16。
- 施加更新时与 lr 相乘发生在调用方（如 [muon.py:L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L125)）：`p.add_(update.reshape(p.shape), alpha=-group["lr"])`——所以每个参数每步的逐元素平均变化量为 \(\mathrm{lr} \cdot f/\sqrt{n}\) 量级。代入默认 lr=0.02：768×768 层约 \(0.02/27.7 \approx 7.2\times10^{-4}\)。

#### 4.5.4 代码实践

**实践目标**：验证「缩放后 RMS 只由列数决定」的规律，回答本讲规格中的核心问题——(512,1024) 与 (1024,512) 的更新 RMS 是否一致。

操作步骤（示例代码）：

```python
import torch
from muon import muon_update, zeropower_via_newtonschulz5

torch.manual_seed(0)
rms = lambda x: x.float().pow(2).mean().sqrt()
print(f"{'形状':>14} {'未缩放RMS':>10} {'缩放后RMS':>10} {'Frobenius':>10} {'理论RMS':>10}")
for shape in [(512, 1024), (1024, 512), (768, 768)]:
    g = torch.randn(*shape)
    mom = torch.zeros_like(g)
    u = muon_update(g, mom)                              # 正交化 + 缩放
    o = zeropower_via_newtonschulz5(g, steps=5)          # 只正交化，不缩放
    print(f"{str(shape):>14} {rms(o):10.5f} {rms(u):10.5f} "
          f"{u.float().norm():10.2f} {shape[1]**-0.5:10.5f}")
```

需要观察的现象与预期结果（理论值见 4.5.2 的表格；实测会因奇异值 \(\in[0.5,1.5]\) 波动约 ±50%）：

| 形状 | 未缩放 RMS | 缩放后 RMS | 理论（\(1/\sqrt{\text{列}}\)） |
|---|---|---|---|
| (512, 1024) | ≈ 0.031 | ≈ 0.031 | \(1/\sqrt{1024}=0.0313\) |
| (1024, 512) | ≈ 0.031 | **≈ 0.044** | \(1/\sqrt{512}=0.0442\) |
| (768, 768) | ≈ 0.036 | ≈ 0.036 | \(1/\sqrt{768}=0.0361\) |

**结论先行**：两种转置形状缩放后的 RMS **不一致**（差约 \(\sqrt2\) 倍）——一致的是规则本身「RMS 只由列数决定」；未缩放时它们反而一致（都 \(1/\sqrt{1024}\)，由较大维决定）。（待本地验证）

#### 4.5.5 小练习与答案

**练习 1**：MLP 中两个隐藏层权重分别为 (3072, 768) 与 (768, 768)（fan-in 都是 768）。默认 lr=0.02 下，两层每步逐元素平均变化各是多少？说明了什么？

**答案**：都是 \(0.02/\sqrt{768} \approx 7.2\times10^{-4}\)。说明缩放后逐元素更新幅度只由 fan-in 决定——加深网络（堆叠同宽层）不需要逐层调 lr；若没有尺度因子，(3072,768) 层的 RMS 是 \(1/\sqrt{3072}\)，比 (768,768) 层小 \(\sqrt4=2\) 倍（未缩放时由较大维决定）。

**练习 2**：把 L40 整行删掉，哪一类参数受影响最大？

**答案**：行数远大于列数的高瘦矩阵（fan-out ≫ fan-in 的层）。它们的未缩放 RMS 是 \(1/\sqrt{m}\)（由较大的行数决定），本应被抬到 \(1/\sqrt n\)；删掉后这些层的有效步长相对偏小，偏差倍数为 \(\sqrt{m/n}\)。宽矩阵（\(m \le n\)）完全不受影响（\(f=1\)）。

**练习 3**：为什么实验测出的 RMS 与理论值会有明显偏差（甚至差 30%）？这算实现缺陷吗？

**答案**：因为 NS5 输出的奇异值约 \(\mathrm{Uniform}(0.5,1.5)\)（u2-l2 的核心结论），单个矩阵的 RMS 会在理论值 ×[0.5, 1.5] 区间内波动；多测几个种子取平均会收敛到理论值。这是「零点斜率最大化」系数的**有意取舍**，不是缺陷——方向（奇异向量配对）是精确保留的。

## 5. 综合实践

把三个模块实验串成一个诊断脚本 `practice_u2_l3.py`（示例代码，全部可在 CPU 上运行）：

```python
# practice_u2_l3.py —— u2-l3 综合实践：muon_update 三联实验
import torch
from muon import muon_update, zeropower_via_newtonschulz5

torch.manual_seed(0)
rms = lambda x: x.float().pow(2).mean().sqrt()
cos = lambda a, b: torch.cosine_similarity(a.flatten().float(), b.flatten().float(), dim=0)

# ── 实验 1：nesterov 开/关（对应 4.2）─────────────────────────────
T, beta, shape = 50, 0.95, (256, 256)
g_seq, g = [], torch.randn(*shape)
for t in range(T):
    g = 0.9 * g + 0.15 * torch.randn(*shape)        # 方向漂移的梯度流
    g_seq.append(g.clone())
mom_n, mom_s = torch.zeros(*shape), torch.zeros(*shape)
print("== 实验 1: nesterov True/False ==")
for t, gt in enumerate(g_seq):
    u_n = muon_update(gt.clone(), mom_n, beta=beta, nesterov=True)
    u_s = muon_update(gt.clone(), mom_s, beta=beta, nesterov=False)
    if t in (0, 9, 49):
        print(f"  t={t:>2}: ‖u_n‖_F={u_n.float().norm():6.2f} RMS_n={rms(u_n):.4f} | "
              f"‖u_s‖_F={u_s.float().norm():6.2f} RMS_s={rms(u_s):.4f} | cos={cos(u_n, u_s):.4f}")

# ── 实验 2：形状与 RMS（对应 4.5）────────────────────────────────
print("== 实验 2: (512,1024) vs (1024,512) ==")
for shape in [(512, 1024), (1024, 512)]:
    g2 = torch.randn(*shape)
    u = muon_update(g2, torch.zeros(*shape))
    print(f"  {shape}: 缩放后 RMS={rms(u):.5f}  理论 1/√cols={shape[1]**-0.5:.5f}  "
          f"‖u‖_F={u.float().norm():.2f}  理论 √rows={shape[0]**0.5:.2f}")

# ── 实验 3：卷积核分支（对应 4.3）────────────────────────────────
print("== 实验 3: (64,3,7,7) 卷积核 ==")
cg = torch.randn(64, 3, 7, 7)
u = muon_update(cg, torch.zeros_like(cg))
flat = cg.view(len(cg), -1)
manual = zeropower_via_newtonschulz5(flat, steps=5)
manual *= max(1, manual.size(-2) / manual.size(-1))**0.5
print(f"  输出形状={tuple(u.shape)}  与手动路径逐位相同={torch.equal(u, manual)}  "
      f"reshape回原形={tuple(u.reshape(cg.shape).shape)}")
```

跑完后回答三道分析题（写进你的学习笔记）：

1. **实验 1 中两种设置的范数为何几乎相同，而 `cos` 随 t 增长逐渐离开 1？** 用「NS5 尺度不变 + 4.2.2 权重表」解释：幅度由形状决定（尺度不变性抹掉一切常数与大小差异），方向由「新鲜梯度 vs 历史平均」的配比决定，梯度流转向后配比差异才显现；t=0 时两者都平行于 \(g_1\)，cos=1。
2. **实验 2 中转置对的 RMS 为何不相等、未缩放时反而相等？** 用 4.5.2 的推导回答：未缩放 RMS \(=1/\sqrt{\max(m,n)}\) 转置对称；缩放后 \(=1/\sqrt{n}\) 只看列数，因子只抬高 \(m>n\) 的一侧。
3. **如果给 `(64, 3, 3, 1)` 的非方核卷积层配 lr，用修复前（commit 6456082 之前）的代码会发生什么？** 按 4.3.5 练习 3 的计算回答：有效步长被压低约 2.67 倍，表现为该层「训不动」。

预期结果：实验 1 的范数列两种设置几乎重合并围绕 16/0.0625 波动；实验 2 两行 RMS 之比接近 \(\sqrt2\)；实验 3 输出 `(64, 147)` 且逐位相等判定为 `True`。（全部待本地验证）

## 6. 本讲小结

- `muon_update`（[muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41)）是五步流水线：**EMA 动量 → Nesterov 方向 → 卷积核展平 → Newton-Schulz 正交化 → 形状尺度缩放**，它是四个优化器类 `step()` 的共同委托对象，`ns_steps=5` 与 `nesterov=True` 在调用点从未被覆盖。
- 两行 `lerp_` 与 `torch.optim.SGD(momentum=0.95, dampening=0, nesterov=...)` 的方向**只差常数 \((1-\beta)\)**，而该常数被 NS5 的 Frobenius 归一化抹掉——docstring 的「internally runs standard SGD-momentum」字面成立；Nesterov 的实质是把最新梯度的相对权重提高约 2.16 倍。
- 两个容易漏掉的源码细节：`grad.lerp_(momentum, beta)` **原地改写 `p.grad`**（副作用）；`len(update)` 即 `size(0)`，`view(len, -1)` 把 \((O,C,k_H,k_W)\) 压成 \((O, C k_H k_W)\) 的矩阵。
- 尺度因子 \(\max(1, m/n)^{1/2}\) 让更新满足两条统一规律：\(\mathrm{RMS} \approx 1/\sqrt{\text{列数}}\)（只由 fan-in 决定）、\(\|\cdot\|_F \approx \sqrt{\text{行数}}\)；它只抬高高瘦矩阵，是「一个 lr 管所有层」和 muP 式缩放体验的机制来源。
- [commit 6456082](https://github.com/KellerJordan/Muon/commit/64560829f70a216f8263ea8ae0fefeac8b451d36) 修复了缩放行误用 `grad`（核的空间维 \(k_H,k_W\)）而非 `update`（矩阵行列数）的潜伏 bug——方核恰好掩盖了它五年，非方核层的有效 lr 被错压约 2.7 倍。
- 正交化的**尺度不变性**意味着 Muon 更新不携带梯度幅度信息：输出幅度由「形状 × 尺度因子 × lr」三元组完全决定——梯度暴涨百倍，单步更新范数几乎不变。

## 7. 下一步学习建议

- **下一讲（u2-l4）**：`muon_update` 已被吃透，但它是被 `step()` 调用的——要真正读懂 [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) 的 `SingleDeviceMuon`，你需要先补 `torch.optim.Optimizer` 的框架约定：`param_groups`、`defaults`、`self.state` 惰性初始化、`@torch.no_grad()`、`closure`、`p.mul_/p.add_` 原地更新。u2-l4 专门讲这些，并让你手写一个 SGD-Momentum 练手。
- **复习建议**：若 4.2.2 的等价推导（\(m_t = (1-\beta)v_t\)）没有亲手推一遍，回到本讲用纸笔补上归纳步骤——它是「Muon 的创新全在正交化、不在动量」这一论断的根据。
- **延伸阅读**：README 链接的 [Kimi K2 技术报告](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L65)讨论了在大规模训练中如何在本讲的尺度因子之上再做 RMS 对齐（乘 \(0.2\sqrt{\max(m,n)}\)），适合学完 u4-l2 后回看。
- **源码预习**：浏览 [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127)，找到它调用 `muon_update` 的那一行（L123）与施加更新的两行（L124-L125），体会「函数做数学、类做编排」的分工。
