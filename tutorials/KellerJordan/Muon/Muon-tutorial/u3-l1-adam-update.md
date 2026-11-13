# adam_update 精读：30 行手写 AdamW

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行解释 `adam_update` 这 6 行代码：两条 `lerp_` 动量更新、两次偏差修正除法、最终的 `buf1c / (buf2c.sqrt() + eps)` 输出。
2. 说清它与 `torch.optim.AdamW` 在数学上**完全等价**，并能指出两者在浮点运算顺序、默认超参、工程特性上的具体差异。
3. 解释为什么 `MuonWithAuxAdam` 要在内部手写一个 AdamW，而不是直接嵌套 `torch.optim.AdamW`。
4. 通过对拍实验亲手验证等价性，并解释残差的数值来源。

一个先交底的账目：`adam_update` 函数本体只有 6 行，但「内置 AdamW」是一套三件组合——6 行数学函数 + 7 行默认超参 + 15 行 step 分支（状态管理、调用、施加更新），合计约 30 行。这正是本讲标题的由来，也是本讲要完整覆盖的范围。

## 2. 前置知识

本讲建立在 u2-l4（PyTorch Optimizer 扩展约定）和 u2-l3（muon_update 精读）之上。开始前请确认你还记得：

- **param_groups / defaults / self.state**：超参按参数组作用；`self.state[p]` 是以参数对象为键、惰性初始化的逐参数记忆。
- **两行原地更新**：`p.mul_(1 - lr*wd)` 与 `p.add_(update, alpha=-lr)`，即 AdamW 式解耦权重衰减的展开（u2-l5 已在 SingleDeviceMuon 里见过一模一样的两行）。
- **lerp_ 语义**：`t.lerp_(end, w)` 表示 `t ← (1-w)·t + w·end`。u2-l3 里 `momentum.lerp_(grad, 1 - beta)` 就是它。

在此之上，本讲需要一点 Adam/AdamW 的背景直觉（不要求你以前用过）：

- **为什么需要「自适应」学习率**：深度网络里不同参数的梯度尺度可能相差几个数量级。SGD 对所有参数用同一个 lr，梯度大的参数被更新得猛、梯度小的参数几乎不动。Adam 的思路是给**每个坐标**单独配一个「刹车」：用梯度的二阶矩（平方的滑动平均）估计该坐标梯度 typical 的大小，再用一阶矩（梯度的滑动平均）除以它，把更新归一化到大约 ±lr 的量级。
- **指数移动平均（EMA）**：\( m_t = \beta m_{t-1} + (1-\beta) g_t \)。β 越接近 1，记忆越长，估计越平滑。
- **偏差修正（bias correction）**：缓冲区从全零开始，早期估计被「往零拉偏」。除以 \( 1-\beta^t \) 恰好抵消这个偏置——本讲 4.3 会用一行代数证明。
- **Adam 与 AdamW 的区别**：Adam 把权重衰减加进梯度（L2 正则），AdamW 把衰减**解耦**出来直接作用于参数：\( p \leftarrow p - lr \cdot \lambda \cdot p \)。本仓库的 adam 分支用的是后者——这与 Muon 分支的写法完全一致。

一个值得先记住的对照：Adam 的归一化是**逐坐标**的（每个元素除以自己的 √v̂），而 u2-l1/u2-l3 学过的 Muon 正交化是**整矩阵谱层面**的归一化。两者是同一问题（更新幅度不可控）的两种解法，这正是下一讲 MuonWithAuxAdam 把它们拼进一个优化器的原因。

## 3. 本讲源码地图

本讲只涉及一个文件 `muon.py`，但要精读其中六段：

| 位置 | 内容 | 作用 |
|---|---|---|
| [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135) | `adam_update` 函数 | **本讲主角**：无状态的 AdamW 更新方向计算器 |
| [muon.py:L270-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L270-L284) | SingleDeviceMuonWithAuxAdam 的 adam 分支 | 状态管理 + 调用 `adam_update` + 施加更新（15 行） |
| [muon.py:L209-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L209-L223) | MuonWithAuxAdam 的 adam 分支 | 分布式版中一模一样的 adam 分支 |
| [muon.py:L241-L247](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L241-L247) | adam 组默认超参（单机版） | lr=3e-4、betas=(0.9, 0.95)、eps=1e-10、wd=0 |
| [muon.py:L175-L181](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L175-L181) | adam 组默认超参（分布式版） | 与上一行完全相同 |
| [muon.py:L34-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L36) | `muon_update` 前三行 | 对照物：Muon 的动量第一行与 Adam 的一阶动量是同一条 EMA |

另外两处背景材料：[muon.py:L145-L146](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L145-L146) 的 docstring 说出了「单优化器」的设计动机；[muon.py:L158-L159](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L158-L159) 的官方示例展示了实际传参（`betas=(0.8, 0.95), eps=1e-10`）。

## 4. 核心概念与源码讲解

### 4.1 adam_update 函数总体：一个无状态的「更新方向计算器」

#### 4.1.1 概念说明

先看函数全貌（也是本讲唯一需要逐字背下来的代码）：

```python
def adam_update(grad, buf1, buf2, step, betas, eps):
    buf1.lerp_(grad, 1 - betas[0])
    buf2.lerp_(grad.square(), 1 - betas[1])
    buf1c = buf1 / (1 - betas[0]**step)
    buf2c = buf2 / (1 - betas[1]**step)
    return buf1c / (buf2c.sqrt() + eps)
```

对照 [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135)，这就是仓库里的原文。

设计上有三个关键点，全都延续 u1-l3 画代码地图时总结的架构原则——**「纯函数做数学，优化器类做编排」**：

1. **无状态**。函数自己不保存任何记忆。`buf1`、`buf2`（两个动量缓冲）和 `step`（步数计数器）全部由调用方传入。这和 `muon_update(grad, momentum, ...)` 把 `momentum_buffer` 传进来的写法完全对称。
2. **副作用有明确约定**。`lerp_` 的下划线表示原地操作：`buf1`、`buf2` 会被原地改写（这就是「记忆」的更新）；`grad` 只被读取，不被修改（注意与 `muon_update` 不同——那里的 Nesterov 写法 `grad.lerp_(momentum, beta)` 会原地覆盖梯度）；返回值是一个**新建的**张量（更新方向，不含学习率）。
3. **只算方向，不碰参数**。函数输出的是 `update`，乘以 `-lr` 施加到参数上的两行（`p.mul_` / `p.add_`）留在优化器类里。学习率、权重衰减这些「策略」与「数学」分离。

它在仓库里只有两个调用点，都在 `use_muon=False` 的 else 分支里：

- [muon.py:L220-L221](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L220-L221)：分布式版 `MuonWithAuxAdam.step` 的调用。
- [muon.py:L281-L282](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L281-L282)：单机版 `SingleDeviceMuonWithAuxAdam.step` 的调用。

#### 4.1.2 核心流程

`adam_update` 在整个 adam 分支中的位置（以单机版为例）：

```text
SingleDeviceMuonWithAuxAdam.step()
└── for group in param_groups:            # use_muon=False 的组
    └── for p in group["params"]:
        ├── p.grad 为 None → 填零张量      # 强制同步设计（u2-l5 讲过）
        ├── state 惰性初始化:
        │      exp_avg    = zeros_like(p)  # buf1 的真身
        │      exp_avg_sq = zeros_like(p)  # buf2 的真身
        │      step       = 0              # 步数计数器
        ├── state["step"] += 1             # 先加一，再调用 → 首次 step=1
        ├── update = adam_update(p.grad, exp_avg, exp_avg_sq,
        │                          step, betas, eps)      ← 本讲主角
        ├── p.mul_(1 - lr * weight_decay)  # 解耦权重衰减
        └── p.add_(update, alpha=-lr)      # 施加更新
```

也就是说：**类负责记忆和施加，函数负责算方向**。`state["step"]` 在调用前先自增（[muon.py:L280](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L280)），所以 `adam_update` 收到的 `step` 从 1 开始计数——这个细节在 4.3 讲偏差修正时至关重要。

顺带一提：参数名 `step` 与优化器方法 `Optimizer.step()` 撞名纯属巧合，前者是一个整数计数器。

#### 4.1.3 源码精读

逐段给出永久链接与说明：

- [muon.py:L130](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130)：函数签名。六个参数中 `betas` 是 `(β1, β2)` 元组、`eps` 是标量，两者都来自参数组（`group["betas"]`、`group["eps"]`）；`grad`、`buf1`、`buf2` 是同形状张量；`step` 是 int。
- [muon.py:L131-L132](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L131-L132)：两条动量更新，4.2 节精读。
- [muon.py:L133-L134](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L133-L134)：两次偏差修正除法，4.3 节精读。
- [muon.py:L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L135)：输出更新方向 `buf1c / (buf2c.sqrt() + eps)`，注意 eps 加在 sqrt **之外**、偏差修正**之后**，4.4 节会专门对比这个位置。
- 调用点上下文：[muon.py:L271-L280](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L271-L280) 是单机版 adam 分支的状态管理（grad 填零 → 三件套惰性初始化 → step 自增）；[muon.py:L283-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L283-L284) 是施加更新的两行——与 Muon 分支的 [muon.py:L268-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L268-L269) 逐字相同（除了 update 的来源）。

再看出厂默认超参（[muon.py:L243-L246](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L243-L246)，分布式版 [muon.py:L177-L180](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L177-L180) 完全一致）：

- `lr = 3e-4`（注意与 Muon 组默认 0.02 相差近两个数量级——量纲不同，u1-l3 已强调不可混用）
- `betas = (0.9, 0.95)`（torch 默认是 `(0.9, 0.999)`）
- `eps = 1e-10`（torch 默认是 `1e-8`）
- `weight_decay = 0`

而 docstring 官方示例（[muon.py:L158-L159](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L158-L159)）实际传的是 `betas=(0.8, 0.95), eps=1e-10`，且三组 adam 参数的 lr 分别是 0.22、0.6、0.04——这是 speedrun 调过的值，不是通用建议，读代码时不要把示例当默认。

#### 4.1.4 代码实践

**实践目标**：亲手调用一次 `adam_update`，验证它的「无状态 + 原地副作用」契约，并观察到 Adam 的标志性现象——首步更新近似符号矩阵。

**操作步骤**（示例代码）：

```python
# 示例代码：观察 adam_update 的无状态设计与原地副作用
import torch
from muon import adam_update

torch.manual_seed(0)
grad = torch.randn(4, 4)
buf1 = torch.zeros(4, 4)
buf2 = torch.zeros(4, 4)

u1 = adam_update(grad, buf1, buf2, step=1, betas=(0.9, 0.95), eps=1e-10)
print("buf1 == 0.1 * grad ?", torch.allclose(buf1, 0.1 * grad))   # 观察 1
print("u1:\n", u1)                                                # 观察 2
print("max |u1 - sign(grad)| =", (u1 - grad.sign()).abs().max().item())  # 观察 3

u2 = adam_update(2 * grad, buf1, buf2, step=2, betas=(0.9, 0.95), eps=1e-10)
print("函数没有返回任何状态对象；buf1/buf2 的变化就是全部记忆")     # 观察 4
```

**需要观察的现象**：

1. `buf1` 在调用后被原地改写为 `0.1 * grad`（即 `(1-β1)·grad`）——副作用发生在缓冲区上，函数无返回状态。
2. `u1` 的每个元素都在 ±1 附近，且符号与 `grad` 对应元素一致。
3. `max |u1 - sign(grad)|` 接近 0（eps=1e-10 极小，几乎不拖后腿）。
4. 第二次调用不依赖任何函数内部记忆，只依赖传入的 `buf1/buf2/step`。

**预期结果**：观察 1 打印 `True`；观察 3 打印一个 1e-5 以下的数。若把 eps 改成 1e-2 再跑，`u1` 会明显偏离 ±1（eps 项开始压制小梯度元素）——这能帮你建立对 eps 作用的直觉。完整数值结论待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`adam_update` 是有状态还是无状态的？`buf1`、`buf2`、`step` 分别存放在哪里？

答案：无状态纯函数。三者都存放在优化器类的 `self.state[p]` 字典里，键名分别是 `exp_avg`、`exp_avg_sq`、`step`（见 [muon.py:L277-L279](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L277-L279)），每次调用时作为实参传入。

**练习 2**：`adam_update` 会不会修改 `grad`？对比 `muon_update`，两者在这一点上有什么不同？

答案：不会，`grad` 只被读取（`grad.square()` 生成新张量）。而 `muon_update` 在 Nesterov 分支里执行 `grad.lerp_(momentum, beta)`，会原地覆盖梯度（[muon.py:L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L36)）。所以连续两次用同一个 `grad` 调 `adam_update` 结果可复现，而 `muon_update` 不行。

**练习 3**：仓库里 `adam_update` 一共有几个调用点？分别在哪个类的哪个分支？

答案：两个。`MuonWithAuxAdam.step` 的 `use_muon=False` 分支（[muon.py:L220-L221](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L220-L221)）和 `SingleDeviceMuonWithAuxAdam.step` 的同名分支（[muon.py:L281-L282](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L281-L282)）。

### 4.2 一阶/二阶动量的 lerp_ 更新

#### 4.2.1 概念说明

Adam 维护两份「记忆」，对应梯度的两个统计量：

- **一阶动量 `buf1`**（torch 里叫 `exp_avg`）：梯度的指数移动平均，估计「梯度的平均方向」。
- **二阶动量 `buf2`**（torch 里叫 `exp_avg_sq`）：梯度**平方**的指数移动平均，估计「每个坐标上梯度 typical 的大小」。

两条更新写在 [muon.py:L131-L132](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L131-L132)：

```python
buf1.lerp_(grad, 1 - betas[0])
buf2.lerp_(grad.square(), 1 - betas[1])
```

用 `lerp_` 的语义 `t ← (1-w)·t + w·end` 展开，就是教科书上的：

\[ m_t = \beta_1 m_{t-1} + (1-\beta_1)\, g_t \]
\[ v_t = \beta_2 v_{t-1} + (1-\beta_2)\, g_t^2 \]

一行 `lerp_` 顶一行公式，而且直接在 GPU 上以单个融合算子完成——这就是作者选择 `lerp_` 而不是 `buf1.mul_(β1).add_(grad, alpha=1-β1)` 的原因（后者读两次写一次，前者一个算子）。

**与 Muon 的深层联系**：把本讲的第一行和 `muon_update` 的第一行并排看——

- [muon.py:L35](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L35)：`momentum.lerp_(grad, 1 - beta)`
- [muon.py:L131](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L131)：`buf1.lerp_(grad, 1 - betas[0])`

**逐字相同**。Muon 的一阶动量和 Adam 的一阶动量是同一条 EMA（u2-l3 证明过：EMA 与求和型 SGD 动量只差常数因子 (1-β)，而该因子被正交化吸收）。两者真正的分岔在下一步：Adam 再维护一份二阶动量做逐坐标除法，Muon 直接把整矩阵送去正交化。可以说：**Adam = EMA(方向) ÷ EMA(幅度)，Muon = 正交化(EMA(方向))**。

#### 4.2.2 核心流程

以固定梯度序列 \( g_1, g_2, \dots \) 为输入，两个缓冲区的演化（β1=0.9，β2=0.95）：

```text
t=0（初始化）:  buf1 = 0            buf2 = 0
t=1:            buf1 = 0.1·g₁       buf2 = 0.05·g₁²
t=2:            buf1 = 0.9·(0.1g₁) + 0.1·g₂
t=3:            buf1 = 0.9²·(0.1g₁) + 0.9·0.1·g₂ + 0.1·g₃
...
```

一般地，\( m_t = (1-\beta_1)\sum_{i=1}^{t} \beta_1^{t-i} g_i \)：越新的梯度权重越大，权重按 β 的幂次衰减。这就是「指数加权」名字的由来。有效记忆长度约为 \( 1/(1-\beta_1) \)：β1=0.9 时约最近 10 步，β2=0.95 时约最近 20 步。

注意 `buf2` 存的是**未开方的**平方平均，且元素永远非负。开方发生在最后一步（L135 的 `buf2c.sqrt()`），数学上 \( \sqrt{v} \) 才与梯度同量纲，两者相除后更新无量纲（约 ±1），再乘 lr 得到步长——这就是「自适应学习率」的完整链条：

\[ \text{update} \approx \frac{\text{平均方向}}{\text{典型幅度}} \in [-1, 1] \times \text{lr} \]

#### 4.2.3 源码精读

- [muon.py:L131](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L131)：一阶动量。`1 - betas[0]` 是 lerp 权重 w，代入语义得 `buf1 ← β1·buf1 + (1-β1)·grad`。
- [muon.py:L132](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L132)：二阶动量。注意传入的是 `grad.square()`——先平方再 lerp，等价于 `β2·buf2 + (1-β2)·g²`。`square()` 会分配一个临时张量，但 `buf2` 本身的更新仍是原地的。
- 对照调用方的初始化 [muon.py:L277-L278](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L277-L278)：`exp_avg` / `exp_avg_sq` 都从全零开始（`torch.zeros_like(p)`）——全零初值正是下一节偏差修正存在的原因。

#### 4.2.4 代码实践

**实践目标**：验证 `lerp_` 的数学语义，并直观感受 EMA 的「记忆衰减」。

**操作步骤**（示例代码）：

```python
# 示例代码：lerp_ 语义验证 + EMA 记忆衰减观察
import torch

beta1 = 0.9
buf = torch.full((3,), 2.0)          # 故意用非零初值，避免 0 的巧合
grad = torch.tensor([1.0, -2.0, 0.5])
ref = beta1 * buf + (1 - beta1) * grad   # 手写公式
buf.lerp_(grad, 1 - beta1)
print("lerp_ == 公式 ?", torch.allclose(buf, ref))     # 观察 1

# 常数梯度 g=1 连续打入 EMA，看 m_t 如何爬向 1
m, g = 0.0, 1.0
for t in range(1, 11):
    m = beta1 * m + (1 - beta1) * g
    print(f"t={t:2d}  m_t={m:.4f}  m_t/(1-0.9^t)={m / (1 - 0.9**t):.4f}")  # 观察 2
```

**需要观察的现象**：

1. `allclose` 返回 `True`，确认 `lerp_` 权重就是「新数据的权重」。
2. 第二列 `m_t` 从 0.1 缓慢爬升（0.1, 0.19, 0.271, …），**永远到不了 1**——这就是零初始化造成的偏低偏差；第三列（除以 \( 1-0.9^t \) 之后）从第一步起就恒等于 1.0000。

**预期结果**：观察 1 为 `True`；观察 2 中第三列每行都是 1.0000（浮点误差在 1e-16 量级）。这份输出就是下一节偏差修正的实验版预告。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：写出 `buf2.lerp_(grad.square(), 1 - betas[1])` 展开后的公式。

答案：\( v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2 \)。注意 lerp 的权重 `1-β2` 作用在「新值」`grad.square()` 上，旧值 `buf2` 的系数是 `β2`。

**练习 2**：为什么 `buf2` 存平方平均而不直接存「幅度的平均」（|g| 的 EMA）？

答案：这是 Adam 的历史设计选择：\( v_t \) 是梯度的（未中心化）二阶矩估计，与一阶矩 \( m_t \) 同一套 EMA 机制（只是先平方再平均），实现上完全复用 `lerp_`；开方放到最后一步统一做，等价于用 RMS（均方根）做分母。数学上 RMS ≥ 均值，作分母更保守。

**练习 3**：β1=0.9、β2=0.95 时，两个动量各自的「有效记忆长度」大约是多少步？

答案：约 \( 1/(1-\beta) \) 步：一阶约 10 步，二阶约 20 步。二阶记忆更长是为了让「幅度估计」更平稳，避免个别大梯度尖峰把分母打得剧烈波动。

### 4.3 偏差修正 1 - beta**step

#### 4.3.1 概念说明

两条修正写在 [muon.py:L133-L134](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L133-L134)：

```python
buf1c = buf1 / (1 - betas[0]**step)
buf2c = buf2 / (1 - betas[1]**step)
```

变量名里的 `c` 是 corrected（修正后）。为什么需要它？因为缓冲区从**全零**开始，而 EMA 是加权和：早期几步里「历史」部分全是零，估计值被系统性拉低。看 t=1 的极端情形：

\[ m_1 = (1-\beta_1) g_1 \quad\Rightarrow\quad \frac{m_1}{1-\beta_1^1} = \frac{(1-\beta_1) g_1}{1-\beta_1} = g_1 \]

除以 \( 1-\beta_1^t \) 之后，第一步的估计**恰好**等于梯度本身，不多不少。一般地可以证明（对 EMA 的加权和展开式提公因子）：

\[ \frac{m_t}{1-\beta_1^t} = \frac{(1-\beta_1)\sum_{i=1}^{t}\beta_1^{t-i} g_i}{1-\beta_1^t} \]

分母 \( 1-\beta_1^t = \sum_{i=1}^{t} \beta_1^{t-i} \cdot (1-\beta_1) / (1-\beta_1) \)……更直接地说：分子权重之和是 \( (1-\beta_1)\sum_i \beta_1^{t-i} = 1-\beta_1^t \)，恰好等于分母。**修正就是把「权重之和不足 1」的加权和重新归一成权重之和为 1 的加权平均**。随着 t 增大，\( \beta^t \to 0 \)，分母趋于 1，修正自动消失——它是早期步专属的补偿。

两个工程细节：

1. **`step` 必须从 1 开始**。如果从 0 开始，第一步会除以 \( 1-\beta^0 = 0 \)，直接产生除零（inf/nan）。这正是调用方在 [muon.py:L280](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L280) 先执行 `state["step"] += 1` 再调用的原因（初始化为 0、用时先自增，计数器从 1 起跳）。
2. **分子分母同时修正**。`buf1c` 和 `buf2c` 各自除以自己的 \( 1-\beta^t \)，随后相除。数学上「各自修正再相除」与「先除再修正」的结果不同，前者才是 Adam 的正确形式。

**与 Muon 的对照（重要）**：`muon_update` 完全没有偏差修正。原因在 u2-l3 已建立的事实：正交化对整体缩放不变——`NS(c·M) = NS(M)`。零初始化给动量带来的常数因子偏低（权重之和 \( 1-\beta^t \)）在正交化眼里等于不存在，所以 Muon 根本不需要这套补偿。反过来，Adam 的更新是逐坐标除法，没有任何尺度不变性保护，偏差会直接进入更新幅度，必须显式修正。**偏差修正的有无，是「归一化是否尺度不变」的试纸。**

#### 4.3.2 核心流程

```text
state["step"]（调用方维护，从 0 开始）
     │ 先自增
     ▼
step = 1, 2, 3, ...
     │
     ▼
修正系数:  c1 = 1 - β1**step     c2 = 1 - β2**step
           step=1  →  c1=0.1,        c2=0.05        （修正最强）
           step=10 →  c1≈0.651,      c2≈0.401
           step=100→  c1≈0.99997,    c2≈0.994       （修正近乎消失）
     │
     ▼
buf1c = buf1 / c1      buf2c = buf2 / c2
     │
     ▼
进入 L135: return buf1c / (buf2c.sqrt() + eps)
```

#### 4.3.3 源码精读

- [muon.py:L133](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L133)：一阶修正。`betas[0]**step` 是 Python 的幂运算（step 是 int），生成一个标量除数；`buf1 / 标量` 返回新张量，`buf1` 本身保持 EMA 原值——**修正只作用于计算，不改写记忆**。
- [muon.py:L134](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L134)：二阶修正，结构完全对称。
- [muon.py:L279-L280](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L279-L280)：`state["step"] = 0` 的初始化与先自增后调用的约定。注意这里的 step 是 **Python int**，而新版 `torch.optim.AdamW` 内部把 step 存成 tensor（为了支持 torch.compile / capturable 场景）——这是两者工程上的一个小差异。

#### 4.3.4 代码实践

**实践目标**：用数值实验验证「t=1 时修正后一阶动量恰好等于梯度、首步更新近似符号矩阵」。

**操作步骤**（示例代码）：

```python
# 示例代码：验证偏差修正的首步精确性
import torch
from muon import adam_update

torch.manual_seed(0)
g = torch.randn(8, 8)
buf1 = torch.zeros(8, 8)
buf2 = torch.zeros(8, 8)

u = adam_update(g, buf1, buf2, step=1, betas=(0.9, 0.95), eps=1e-10)

# 验证 1：修正后的一阶动量 == g（即 buf1 / (1-β1) == g）
print("m_hat == g ?", torch.allclose(buf1 / (1 - 0.9), g))
# 验证 2：修正后的二阶动量 == g²
print("v_hat == g^2 ?", torch.allclose(buf2 / (1 - 0.95), g.square()))
# 验证 3：首步更新 ≈ sign(g)
print("max |u - sign(g)| =", (u - g.sign()).abs().max().item())

# 危险演示：step=0 会怎样（预期 nan/inf，仅打印观察，勿用于训练）
buf1z = torch.zeros(2, 2); buf2z = torch.zeros(2, 2)
u0 = adam_update(torch.ones(2, 2), buf1z, buf2z, step=0, betas=(0.9, 0.95), eps=1e-10)
print("step=0 output:", u0)
```

**需要观察的现象**：

1. 验证 1、2 打印 `True`——首步的修正后估计精确等于 `g` 和 `g²`。
2. 验证 3 的差接近 0：首步更新就是逐坐标归一化的符号，幅度约 ±1，与梯度绝对大小无关。
3. `step=0` 时输出全是 `nan`（0/0）——亲眼确认计数器必须从 1 开始。

**预期结果**：三项如上；`u0` 打印 `tensor([[nan, nan], [nan, nan]])`。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：用一行推导说明为什么 t=1 时修正后的 `buf1c` 恰好等于 `g`。

答案：`buf1 = (1-β1)·g`，`buf1c = buf1 / (1-β1^1) = (1-β1)·g / (1-β1) = g`。

**练习 2**：当 step 很大（比如 1000）时，这两行修正代码的数值效果是什么？为什么可以忽略？

答案：\( \beta^{1000} \approx 0 \)，除数趋于 1，修正变为恒等操作。因为 EMA 的权重之和 \( 1-\beta^t \) 在 t 远大于记忆长度 \( 1/(1-\beta) \) 后已饱和为 1，零初始化的「欠账」被后续数据冲掉了。

**练习 3**：`muon_update` 为什么不需要偏差修正？给出与 Adam 的本质区别。

答案：Muon 的更新在动量之后经过 Newton-Schulz 正交化，而正交化对常数缩放不变（\( NS(cM)=NS(M) \)），零初始化导致的早期幅度偏低被整批吸收；Adam 的逐坐标除法没有任何尺度不变性，偏差会原样进入更新幅度，必须显式除以 \( 1-\beta^t \) 补偿。

### 4.4 对照 torch.optim.AdamW：等价性、eps 位置与「为什么要手写」

#### 4.4.1 概念说明

把 `adam_update`（加上调用方的两行施加代码）与 [torch.optim.AdamW 官方文档](https://pytorch.org/docs/stable/generated/torch.optim.AdamW.html)给出的算法逐项对齐（下表右列公式据 PyTorch 文档整理）：

| 步骤 | 本仓库 | torch.optim.AdamW |
|---|---|---|
| 一阶动量 | `buf1.lerp_(grad, 1-β1)`（L131） | \( m_t = \beta_1 m_{t-1} + (1-\beta_1) g_t \) |
| 二阶动量 | `buf2.lerp_(grad.square(), 1-β2)`（L132） | \( v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2 \) |
| 偏差修正 | `buf1c = buf1/(1-β1^t)`、`buf2c = buf2/(1-β2^t)`（L133-L134） | \( \hat{m}_t = m_t/(1-\beta_1^t) \)、\( \hat{v}_t = v_t/(1-\beta_2^t) \) |
| 更新方向 | `buf1c / (buf2c.sqrt() + eps)`（L135） | \( \hat{m}_t / (\sqrt{\hat{v}_t} + \varepsilon) \) |
| 解耦衰减 | `p.mul_(1 - lr*wd)`（调用方 L283） | \( p \leftarrow p - lr \cdot \lambda \cdot p \)（先于更新施加） |
| 施加更新 | `p.add_(update, alpha=-lr)`（调用方 L284） | \( p \leftarrow p - lr \cdot \hat{m}_t / (\sqrt{\hat{v}_t} + \varepsilon) \) |

**结论：数学上完全等价**，包括两个容易出错的细节——

1. **eps 的位置**。本仓库把 eps 加在 `buf2c.sqrt()` **之后**（开方外、偏差修正后），即分母是 \( \sqrt{\hat v} + \varepsilon \)；PyTorch AdamW 同样是 \( \sqrt{\hat{v}_t} + \varepsilon \)。**两者位置一致**。真正的「eps 位置之争」存在于更广的生态里：部分实现写成 \( \hat m / \sqrt{\hat v + \varepsilon} \)（eps 在开方内），当 \( \hat v \) 很小时两者差异显著——开方外版本对小梯度元素的压制更强（分母至少是 eps）。本讲实践里会做一个「eps 塞进开方内」的变体对拍，让你亲眼看到这条分界线。
2. **衰减与更新的顺序**。两边都是先乘衰减再减更新，且衰减系数都是 \( 1 - lr \cdot \lambda \)（不是 \( 1-\lambda \)）——即衰减步长与学习率绑定，这正是「AdamW 式」而非旧式 Adam+L2 的标志，也与本仓库 Muon 分支的两行完全一致。

那实际对拍时为什么还会看到非零差异？因为**浮点运算顺序不同**：

- PyTorch 把 \( lr/\hat{c}_1 \) 融进分子的系数，分母算 \( \sqrt{v}/\sqrt{c_2} + \varepsilon \)（先开方后除）；
- 本仓库先各自除修正系数（\( v/c_2 \) 再开方），最后统一乘 `-lr`。

实数意义下恒等：\( \sqrt{v/c_2} = \sqrt{v}/\sqrt{c_2} \)；但 float32 下每次乘除都有舍入，路径不同末位会有差异。此外 PyTorch 二阶动量用的融合算子与 `lerp_(square())` 也不是同一个核。综合起来，典型差异在 1e-9～1e-7 量级（随张量尺度浮动）。

**工程特性差异**（这些是真实区别，尽管不影响数学）：

| 维度 | 手写 adam_update 分支 | torch.optim.AdamW |
|---|---|---|
| 默认 betas | `(0.9, 0.95)` | `(0.9, 0.999)` |
| 默认 eps | `1e-10` | `1e-8` |
| `p.grad is None` | 填零张量（强制同步，u2-l5） | 跳过该参数 |
| step 计数 | Python int | tensor（兼容 compile/capturable） |
| 可选项 | 无 | amsgrad / maximize / foreach / fused 等 |
| 状态存放 | `self.state[p]`（与 muon 分支同一种字典） | 优化器内部独立 state |

**为什么 MuonWithAuxAdam 内置手写 AdamW 而不嵌套 torch.optim.AdamW？** 仓库的 docstring 给出了官方答案——[muon.py:L145-L146](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L145-L146)：

> The point of this class is to allow the user to have a single optimizer in their code, rather than having both a Muon and an Adam which each need to be stepped.

展开成三条理由：

1. **单优化器的工程收益**：一次 `optimizer.step()`、一个 LR scheduler、一份 `state_dict` 保存/加载。如果训练脚本同时持有 Muon 和 AdamW 两个优化器，调度器、checkpoint、梯度裁剪的插入点都要写两遍，还容易漏 step 某一个。
2. **分支骨架的一致性**：adam 分支（[muon.py:L270-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L270-L284)）与 muon 分支（[muon.py:L259-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L259-L269)）共享同一副骨架：grad 填零 → 惰性初始化 → 算 update → `p.mul_`/`p.add_` 两行。语义（如「grad 为 None 不跳过而是填零」「解耦衰减先乘后加」）在两条路径上逐字相同。嵌套一个 `torch.optim.AdamW` 会引入第二套 param_groups/state/step 机制，且它「跳过无梯度参数」的行为与分布式 Muon 的「强制同步」直接冲突——在分布式版里，各 rank 的集合通信序列必须严格一致（u3-l3 的主题），嵌套优化器的条件跳步是死锁隐患。
3. **研究代码的可控与可读**：6 行数学摊在明面上，默认值（`betas=(0.9,0.95)`、`eps=1e-10`）就是 speedrun 生态习惯的值，想改任何一个符号都触手可及，不必穿透 torch 的层层选项。

#### 4.4.2 核心流程

对拍验证等价性的实验设计（也是本讲综合实践的骨架）：

```text
准备：W0（同一初始权重）、grads（同一份固定梯度序列，预生成）
路径 A：p_repo ← W0，用 SingleDeviceMuonWithAuxAdam(use_muon=False) 驱动
        → 命中本仓库 adam 分支（L270-L284，内部调 adam_update）
路径 B：p_ref ← W0，用 torch.optim.AdamW(同 lr/betas/eps, wd=0) 驱动
每步：p.grad = g → step() → 打印 |p_repo - p_ref|.max()
预期：0 或 1e-9~1e-7 量级（浮点顺序差异，非算法差异）
```

#### 4.4.3 源码精读

本节的「源码」是两份并排材料：

- 本仓库侧：[muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135)（数学）+ [muon.py:L283-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L283-L284)（施加，注意先衰减后更新）。
- PyTorch 侧：[AdamW 官方文档](https://pytorch.org/docs/stable/generated/torch.optim.AdamW.html)的算法伪代码（本讲不逐行引用 PyTorch 源码，以文档公式为准）。

等价性的代数核心一行：设 \( c_1 = 1-\beta_1^t \)、\( c_2 = 1-\beta_2^t \)，则

\[ \underbrace{\frac{m/c_1}{\sqrt{v/c_2}+\varepsilon}}_{\text{本仓库 L133-L135}} \;=\; \underbrace{\frac{1}{c_1}\cdot\frac{m}{\sqrt{v}/\sqrt{c_2}+\varepsilon}}_{\text{PyTorch 的计算顺序}} \]

左右两边是同一个实数，只是浮点求值路径不同——这就是「数学等价、末位不同」的全部来源。

再看施加侧的对照：本仓库两行 `p.mul_(1 - lr*wd); p.add_(update, alpha=-lr)` 一共出现了六次——[muon.py:L92-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L92-L93)、[L124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125)、[L206-L207](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L206-L207)、[L222-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L222-L223)、[L268-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L268-L269)、[L283-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L283-L284)——四个类的六个分支（两个纯 Muon 类各一个、两个组合类各 muon/adam 两支）共用同一对「解耦衰减 + 施加更新」，这就是 4.4.1 里「骨架一致性」的实体证据。

#### 4.4.4 代码实践

**实践目标**：做一次小规模对拍（3 步），确认两条路径逐步贴合，并定量观察浮点残差。

**操作步骤**（示例代码）：

```python
# 示例代码：adam_update 分支 vs torch.optim.AdamW（3 步对拍）
import torch
from muon import SingleDeviceMuonWithAuxAdam

torch.manual_seed(0)
W0 = torch.randn(64, 64)
grads = [torch.randn(64, 64) for _ in range(3)]   # 固定梯度序列

lr, betas, eps = 1e-3, (0.9, 0.95), 1e-10

p_repo = W0.clone().requires_grad_(True)
opt_repo = SingleDeviceMuonWithAuxAdam([dict(
    params=[p_repo], lr=lr, betas=betas, eps=eps,
    weight_decay=0, use_muon=False)])

p_ref = W0.clone().requires_grad_(True)
opt_ref = torch.optim.AdamW([p_ref], lr=lr, betas=betas, eps=eps, weight_decay=0)

for t, g in enumerate(grads, start=1):
    p_repo.grad, p_ref.grad = g.clone(), g.clone()
    opt_repo.step()
    opt_ref.step()
    print(f"step {t}: max|Δp| = {(p_repo - p_ref).abs().max().item():.3e}")
```

**需要观察的现象**：每步打印的 `max|Δp|` 为 0 或 1e-9～1e-7 之间的微小值，且不随步数系统性增长（残差是逐步舍入，不是误差累积发散）。

**预期结果**：3 步差异均在 1e-7 以下。若想看到**真正的算法级差异**，把路径 B 换成 eps 在开方内的变体 `update = buf1c / (buf2c + eps).sqrt()` 自己循环实现，小梯度元素上的差异会明显放大。完整 20 步版本见第 5 节综合实践。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：本仓库与 PyTorch AdamW 的 eps 都加在什么位置？「eps 位置差异」这个说法真正指的是什么？

答案：都加在偏差修正后二阶动量的平方根**之外**：分母 \( \sqrt{\hat v} + \varepsilon \)。「位置差异」指的是生态中另一类实现把 eps 放进开方内（\( \sqrt{\hat v + \varepsilon} \)）；当 \( \hat v \gg \varepsilon \) 时两者几乎无差，当某些坐标 \( \hat v \) 极小（梯度长期近零）时，开方外版本会把这些坐标的更新压得更狠。

**练习 2**：手写分支与 torch.optim.AdamW 至少有三处工程行为差异，列举之。

答案：(1) `p.grad is None` 时本仓库填零张量继续更新（为分布式同步设计），torch 直接跳过该参数；(2) step 计数一个是 Python int、一个是 tensor；(3) 默认超参不同（betas `(0.9,0.95)` vs `(0.9,0.999)`、eps `1e-10` vs `1e-8`）；另有无 amsgrad/fused 等选项、状态字典结构不同等。

**练习 3**：为什么不在 `MuonWithAuxAdam.step` 里 new 一个 `torch.optim.AdamW` 来处理非 Muon 参数？

答案：(1) 目标是单一优化器：一次 step、一个调度器、一份 state_dict（docstring L145-L146 明说）；(2) 嵌套优化器会带来第二套 param_groups/state 机制，且其「跳过无梯度参数」行为与本项目「grad 填零强制同步」的约定冲突，在分布式版中会破坏各 rank 集合通信序列的一致性，造成挂死；(3) adam 分支与 muon 分支共享同一副骨架（grad 填零、惰性初始化、两行施加），代码形状统一、语义一致，6 行数学可控可读。

## 5. 综合实践

**任务：20 步完整对拍——验证 `adam_update` 分支与 `torch.optim.AdamW` 数值等价，并解释残差来源。**

设计思路：两条路径从**同一个初始权重**出发，吃**同一份预生成的梯度序列**（正弦场 + 噪声），唯一的区别是驱动引擎。为了确保测的是仓库的**真实代码路径**，路径 A 直接用 `SingleDeviceMuonWithAuxAdam` 的 `use_muon=False` 参数组（命中 [muon.py:L270-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L270-L284) 的 adam 分支），而不是手工复刻。

```python
# 示例代码：综合实践——20 步等价性对拍（CPU 即可运行）
import torch
from muon import SingleDeviceMuonWithAuxAdam

torch.manual_seed(0)

d = 128
W0 = torch.randn(d, d)

# 固定梯度序列：正弦场 + 噪声；预生成列表保证两条路径吃到的梯度逐位一致
grads = [0.1 * torch.randn(d, d) + torch.sin(3 * torch.randn(d, d) + 0.3 * t)
         for t in range(20)]

lr, betas, eps = 1e-3, (0.9, 0.95), 1e-10

# 路径 A：本仓库真实分支（use_muon=False → adam_update）
p_repo = W0.clone().requires_grad_(True)
opt_repo = SingleDeviceMuonWithAuxAdam([dict(
    params=[p_repo], lr=lr, betas=betas, eps=eps,
    weight_decay=0, use_muon=False)])

# 路径 B：torch.optim.AdamW（同超参）
p_ref = W0.clone().requires_grad_(True)
opt_ref = torch.optim.AdamW([p_ref], lr=lr, betas=betas, eps=eps, weight_decay=0)

for t, g in enumerate(grads, start=1):
    p_repo.grad, p_ref.grad = g.clone(), g.clone()
    opt_repo.step()
    opt_ref.step()
    diff = (p_repo.detach() - p_ref.detach()).abs().max().item()
    print(f"step {t:2d}  max|Δp| = {diff:.3e}")

# 附加实验 1：eps 移进开方内的变体，观察真正的算法级差异
buf1 = torch.zeros_like(W0); buf2 = torch.zeros_like(W0)
p_alt = W0.clone().requires_grad_(True)
for t, g in enumerate(grads, start=1):
    buf1.lerp_(g, 1 - betas[0]); buf2.lerp_(g.square(), 1 - betas[1])
    u = (buf1 / (1 - betas[0]**t)) / ((buf2 / (1 - betas[1]**t) + eps).sqrt())
    with torch.no_grad():
        p_alt -= lr * u
print("eps-in-sqrt vs repo: max|Δp| =",
      (p_alt - p_repo.detach()).abs().max().item())

# 附加实验 2：两边同时开 weight_decay=0.01，验证解耦衰减顺序一致
```

**操作步骤**：

1. 安装本仓库（u1-l2 的 `pip install git+https://github.com/KellerJordan/Muon`），CPU 环境即可。
2. 运行主对拍（前 26 行），记录 20 步的 `max|Δp|`。
3. 运行附加实验 1：自己循环实现 eps 在开方内的变体，与仓库路径对比。
4. （选做）附加实验 2：把两条路径的 `weight_decay` 同时设为 0.01，确认差异量级不变——说明「先 `p.mul_` 后 `p.add_`」的顺序两边一致，衰减没有引入额外分歧。

**需要观察的现象与预期结果**：

1. 主对拍：每步 `max|Δp|` 为 0 或 1e-9～1e-7 量级，且不随步数系统性放大。这证明两条路径数学等价，残差全部来自浮点运算顺序（`sqrt(v/c2)` vs `sqrt(v)/sqrt(c2)`、lr 融合进分子 vs 最后乘）。
2. 附加实验 1：差异量级明显更大（预期比主对拍高数个数量级）——这才是 eps 位置造成的**算法级**差异，集中出现在 \( \hat v \) 很小的坐标上（梯度长期接近零的元素）。
3. 附加实验 2：开衰减后差异量级与主对拍相当——衰减项两边逐字相同。

以上数值结论待本地验证；如果你的 PyTorch 版本输出与预期不符，先检查 `betas`、`eps` 是否真的传进了 `torch.optim.AdamW`（构造期超参，事后修改不生效）。

## 6. 本讲小结

- `adam_update` 是 6 行无状态纯函数：两条 `lerp_` 更新一阶/二阶动量（与 `muon_update` 第一行是同一条 EMA）、两次除以 `1-β^t` 的偏差修正、输出 `buf1c / (buf2c.sqrt() + eps)`；记忆（`exp_avg`/`exp_avg_sq`/`step`）由优化器类放在 `self.state[p]`，施加更新的两行（`p.mul_` + `p.add_`）也留在类里——「函数做数学、类做编排」。
- 偏差修正的本质是把「权重之和不足 1」的零初始化 EMA 重新归一为权重和为 1 的加权平均；t=1 时修正后估计恰好等于梯度本身；Muon 不需要它，因为正交化对常数缩放不变——偏差修正的有无是归一化是否尺度不变的试纸。
- 与 `torch.optim.AdamW` 数学完全等价（含 eps 加在 √v̂ 之外、衰减先于更新施加这两个易错点）；实际对拍的微小残差来自浮点运算顺序，而非算法。工程差异真实存在：默认 betas/eps 不同、grad 为 None 填零而非跳过、step 是 int 而非 tensor、无 amsgrad/fused 等选项。
- 内置手写 AdamW 的动机：单优化器（一次 step、一个调度器、一份 state_dict）+ adam/muon 两分支共享同一副骨架（对分布式通信一致性至关重要）+ 研究代码的可控可读。
- Adam 与 Muon 是同一问题的两种归一化：Adam 逐坐标除以 √v̂（更新约 ±lr，与梯度尺度无关），Muon 整矩阵谱归一化（更新谱范数 ≈ lr）——下一讲将看到它们被 `use_muon` 开关拼进同一个优化器。

## 7. 下一步学习建议

下一讲 **u3-l2「MuonWithAuxAdam：use_muon 参数组设计与单优化器混合训练」** 将把本讲的 adam 分支放回它的宿主：精读构造函数里 muon 组与 adam 组各自的默认超参与严格键集合断言（本讲已见 [muon.py:L241-L247](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L241-L247)），以及 `step()` 中 `use_muon` 如何分流到 `muon_update` 与 `adam_update` 两条路径。

继续阅读的建议：

1. 通读 [muon.py:L138-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L182)（MuonWithAuxAdam 的 docstring 与构造函数），带着本讲的问题读：为什么对参数组键集合做 `assert set(group.keys()) == ...` 这种严格断言？
2. 对照 PyTorch 官方文档的 [AdamW](https://pytorch.org/docs/stable/generated/torch.optim.AdamW.html) 与 [Adam](https://pytorch.org/docs/stable/generated/torch.optim.Adam.html) 两页，弄清 L2 正则与解耦权重衰减在公式里的位置差别。
3. 想深入偏差修正的推导，可读 Adam 论文（Kingma & Ba, 2015）第 3 节；想看正交化路线的对照材料，回顾 u2-l1 的讲义与 [muon.py:L5-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L14) 的 docstring。
