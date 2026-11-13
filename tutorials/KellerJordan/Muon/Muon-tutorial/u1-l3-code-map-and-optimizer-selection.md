# muon.py 代码地图：3 个函数、4 个优化器类如何选型

## 1. 本讲目标

前两讲我们搞清楚了 Muon 是什么（u1-l1）、怎么安装并跑通第一个例子（u1-l2）。本讲要把整个项目的核心——287 行的 [muon.py](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py) —— 完整「拆开看」。

学完本讲，你应该能够：

1. 画出 muon.py 的模块地图：3 个函数、4 个优化器类各自的行号范围、职责与调用关系。
2. 根据两个维度——「单机还是分布式」「纯 Muon 还是带辅助 AdamW」——为给定训练场景立刻选出正确的类。
3. 识别贯穿所有优化器类的共同代码骨架（`step()` 循环、`state` 惰性初始化、`p.mul_` / `p.add_` 两行原地更新），后续精读任何一个类时都能对号入座。

本讲是「地图课」：我们只看清地形、不深入任何一个山洞。正交化的数学、动量的细节、分布式通信的机制，分别在第二、三单元展开。

## 2. 前置知识

### 2.1 承接前两讲

- u1-l1 告诉我们：Muon 是**只为隐藏层二维权重矩阵设计**的优化器，算法一句话是「先跑 SGD 动量，再把更新正交化」，学习率的单位是「每次更新的谱范数」；embedding、输出头、增益和偏置仍用 AdamW 训练。
- u1-l2 告诉我们：`from muon import ...` 可以拿到 3 个函数和 4 个优化器类；单机版（`SingleDevice*`）完全不碰 `torch.distributed`，CPU 就能跑。

### 2.2 函数与类：本讲的地名词汇

| 术语 | 通俗解释 |
|---|---|
| 函数（function） | 一段「输入→输出」的纯计算，不保存任何状态。muon.py 的 3 个函数都是数学工具。 |
| 类（class） | 把「数据 + 操作数据的方法」打包在一起的模板。muon.py 的 4 个优化器类各自保存参数列表和动量缓存。 |
| 方法（method） | 定义在类里的函数，习惯通过 `optimizer.step()` 这样的形式调用。 |
| 继承 | 4 个类都写成 `class Xxx(torch.optim.Optimizer)`，表示「我是一个 PyTorch 优化器」，免费获得 `param_groups`、`state` 等框架设施。 |

### 2.3 PyTorch 优化器的三个关键词（概念级）

| 关键词 | 是什么 | 在 muon.py 里的体现 |
|---|---|---|
| `param_groups` | 一个列表，每项是一个字典，描述「哪一批参数 + 这批参数的超参（lr 等）」 | 每个类的 `step()` 都从 `for group in self.param_groups:` 开始 |
| `self.state` | 字典：参数 → 该参数的私有状态（如动量缓冲区） | `state["momentum_buffer"]`、`state["exp_avg"]` 等 |
| `step()` | 每个训练步调用一次的方法，读 `p.grad`、算更新、改参数 | 4 个类的核心方法（本讲主角） |

另外两个会在源码里反复出现的写法：`@torch.no_grad()` 装饰器表示「这个方法里不做梯度追踪」（更新参数本身不该被 autograd 记录）；`p.add_(...)`、`p.mul_(...)` 这类带下划线的方法是**原地操作**（in-place），直接修改张量本身。

> 这些约定的深入讲解在 u2-l4（PyTorch Optimizer 扩展约定），本讲只需「认识它们」。

### 2.4 torch.distributed 的三个词（概念级）

muon.py 顶部导入了 [torch.distributed as dist](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L1-L2)。本讲只需要认识三个词：

| 词汇 | 含义 |
|---|---|
| `world_size` | 一共几张卡（几个进程）参与训练 |
| `rank` | 当前进程的编号（0, 1, 2, ...） |
| `all_gather` | 一个通信原语：每个进程各拿出一份张量，广播汇总后所有进程都拿到全组的张量列表 |

分布式版 `Muon` 的思路是：**不按数据切分参数，而是把「一批参数」分摊到各张卡上计算更新，再用 `all_gather` 汇总**。本讲只标注这段代码的位置，机制精读在 u3-l3。

## 3. 本讲源码地图

整个仓库只有 [muon.py](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py) 一个源码文件（`setup.py` 只是打包配置，见 u1-l2），所以本讲的「源码地图」就是这一张表：

| 行号 | 对象 | 类型 | 一句话职责 |
|---|---|---|---|
| [L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31) | `zeropower_via_newtonschulz5` | 函数 | 用 Newton-Schulz 迭代把矩阵近似正交化（计算矩阵的「零次幂」） |
| [L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) | `muon_update` | 函数 | 流水线：动量 → Nesterov → 卷积核展平 → 正交化 → 尺度缩放 |
| [L44-L96](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L96) | `Muon` | 类（分布式） | 纯 Muon 优化器，多卡分摊参数更新 |
| [L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) | `SingleDeviceMuon` | 类（单机） | 纯 Muon 优化器，单进程版 |
| [L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135) | `adam_update` | 函数 | 6 行手写 AdamW 更新核心 |
| [L138-L225](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L225) | `MuonWithAuxAdam` | 类（分布式） | 一个优化器同时管 Muon 组和 AdamW 组，多卡版 |
| [L228-L286](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L286) | `SingleDeviceMuonWithAuxAdam` | 类（单机） | 同上的单进程版 |

它们之间的调用关系（**这张图是本讲最重要的产出**）：

```text
Muon.step ──────────────────────┐
SingleDeviceMuon.step ──────────┤
MuonWithAuxAdam.step ───────────┼──▶ muon_update ──▶ zeropower_via_newtonschulz5
SingleDeviceMuonWithAuxAdam.step ┘        （L34-41）        （L5-31）

MuonWithAuxAdam.step ───────────┐
                                └──▶ adam_update（L130-135）
SingleDeviceMuonWithAuxAdam.step ┘      ↑ 只有带 AuxAdam 的两个类会调用
```

可以看出清晰的分层：**函数层做数学，类层做编排**——4 个类自己不算任何东西，只在 `step()` 里调度 `muon_update` / `adam_update`。

## 4. 核心概念与源码讲解

### 4.1 zeropower_via_newtonschulz5：正交化纯函数

#### 4.1.1 概念说明

这是整份代码的数学心脏。它解决的问题是：给定一个梯度矩阵 \( G \)，求它的「零次幂」\( G^0 \)——对 SVD 分解 \( G = USV^T \) 而言就是 \( UV^T \)，即**把所有奇异值拉平为 1**（保持方向、只重整能量）后的最近正交矩阵。

为什么需要它？u1-l1 讲过：把更新替换成近似正交矩阵后，每次更新的谱范数（最大奇异值）被控制在 1 附近，让学习率拥有「谱范数」这一稳定量纲，各方向获得的更新能量也均衡。

为什么不直接调 `torch.linalg.svd`？函数 docstring 的末尾（在 `Muon` 类的 [L52-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L52-L53)）给出了答案：Newton-Schulz 迭代**只由矩阵乘法组成**，可以在 GPU 上以 bfloat16 低精度稳定、快速地运行，而 SVD 做不到。

它是一个**无状态纯函数**：同样的输入永远得到同样的输出，不依赖也不修改任何优化器状态。

#### 4.1.2 核心流程

```text
输入 G（任意 ≥2 维张量，最后两维是矩阵）
  1. 断言 G.ndim >= 2（一维参数没有正交化可言）
  2. 取五次迭代系数 a, b, c = (3.4445, -4.7750, 2.0315)
  3. 转成 bfloat16；若行数 > 列数，先转置成「扁」矩阵
  4. 谱范数归一化：X = X / (||X||_spectral 的近似 + 1e-7)，保证谱范数 ≤ 1
  5. 循环 steps 次：X ← aX + (bA + cA²)X，其中 A = XXᵀ
  6. 若第 3 步转置过，把 X 转置回原方向，返回
输出：近似 US'Vᵀ，S' 的对角元约在 0.5 ~ 1.5 之间
```

其中第 5 步的多项式迭代之所以叫「五次」（quintic），是因为 \( aX + (bA + cA^2)X \) 展开后关于 \( X \) 是五次的。迭代收敛的目标是把 \( A = XX^T \) 的特征值（即 \( X \) 奇异值的平方）推向 1。

#### 4.1.3 源码精读

函数签名与 docstring（占整个函数近一半篇幅，值得先读）：

- [muon.py:L5-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L14) —— docstring 明确说了两件重要的事：①系数选成让零点处斜率最大，即使超过「迭代不再处处收敛到 1」的临界点也继续加大斜率；②因此输出**不是**精确的 \( UV^T \)，而是 \( US'V^T \)，\( S' \) 对角元约服从 Uniform(0.5, 1.5)——实践证明这不伤害模型效果。

- [muon.py:L15-L19](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15-L19) —— 断言 `G.ndim >= 2`（注释说明支持批量矩阵，即 3 维以上输入按最后两维分批处理）；取出三个迭代系数；`X = G.bfloat16()` 把输入降到 bfloat16 精度；若行数大于列数则 `X = X.mT` 转置，让后续格拉姆矩阵 \( XX^T \) 是较小的那个。

- [muon.py:L21-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L21-L27) —— 先做谱范数归一化（用 Frobenius 范数做上界估计，加 `1e-7` 防除零），保证迭代在收敛域内启动；然后执行 `steps` 次三矩阵乘法循环。注意 `X.norm(dim=(-2, -1))` 是对最后两维求 Frobenius 范数，它是谱范数的上界，所以归一化后谱范数必然 ≤ 1。

- [muon.py:L29-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L29-L31) —— 若之前转置过则转置回去，保持「输出形状 = 输入形状」的约定。这个约定很重要：`muon_update` 拿到结果后可以直接乘尺度因子、直接加到参数上。

#### 4.1.4 代码实践

**实践目标**：直观感受「正交化」的效果——输出的奇异值被压到 1 附近，而输入的奇异值原本散布很宽。

**操作步骤**（示例代码，非项目原有）：

```python
import torch
from muon import zeropower_via_newtonschulz5

torch.manual_seed(0)
G = torch.randn(64, 32)

print("输入 G 的奇异值范围:",
      torch.linalg.svdvals(G).min().item(),
      torch.linalg.svdvals(G).max().item())

for steps in (5, 10):
    X = zeropower_via_newtonschulz5(G, steps=steps)
    s = torch.linalg.svdvals(X.float())   # bfloat16 转回 float32 再算
    print(f"steps={steps}: 输出 shape={tuple(X.shape)}, dtype={X.dtype}, "
          f"奇异值范围 [{s.min().item():.3f}, {s.max().item():.3f}]")
```

**需要观察的现象**：

1. 输入 `G` 的奇异值从接近 0 一直散布到十几（64×32 标准正态矩阵的最大奇异值约为 \( \sqrt{64}+\sqrt{32}\approx 13.7 \)）。
2. 输出的奇异值被压缩到 1 附近的一个窄带里，`steps=10` 比 `steps=5` 更窄、更贴近 1。

**预期结果**：输出 `shape=(64, 32)`、`dtype=torch.bfloat16`；`steps=5` 时奇异值大致落在 1 附近的窄区间（docstring 所述约 0.5～1.5 的带内，实际集中更紧），`steps=10` 时更接近 1。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 muon.py 不直接用 `torch.linalg.svd` 实现？

**答案**：`Muon` 类 docstring（[L52-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L52-L53)）说明：Newton-Schulz 迭代只含矩阵乘法，可在 GPU 上用 bfloat16 稳定高效运行；SVD 在 GPU 上慢且低精度下不稳定。而且本应用根本不需要精确的 \( UV^T \)——奇异值在 0.5～1.5 之间就够了。

**练习 2**：函数输出等于精确的 \( UV^T \) 吗？

**答案**：不等于。输出是 \( US'V^T \)，\( S' \) 对角元约在 0.5～1.5 之间（[L11-L13](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L11-L13)）。这是「为减少迭代步数而加大零点斜率」的刻意取舍，实测不影响效果。

**练习 3**：[L18-L19](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L18-L19) 为什么要在行数大于列数时先转置？

**答案**：让 \( X \) 变成「行 ≤ 列」的扁矩阵，这样格拉姆矩阵 \( A = XX^T \) 是两个当中较小的那个，迭代中参与乘法的矩阵更小、数值上更稳定；最后再转置回来保持形状约定。

### 4.2 muon_update：从梯度到 Muon 更新的流水线

#### 4.2.1 概念说明

如果说 `zeropower_via_newtonschulz5` 是「正交化」这一个动作，`muon_update` 就是把一个参数的梯度加工成**最终更新量**的完整流水线。它同样是无状态纯函数——「状态」（动量缓冲区）由调用它的优化器类保管并传入。

流水线一共五道工序：

1. **动量累加**：把当前梯度混入历史动量；
2. **Nesterov 动量**（默认开启）：用「向前看一步」的混合梯度作为待正交化的更新；
3. **卷积核展平**：4 维卷积核权重压成 2D 矩阵（这就是 u1-l1 说「卷积核也可以用 Muon」的实现基础）；
4. **正交化**：调用 4.1 的函数；
5. **尺度缩放**：乘一个只取决于矩阵形状的因子，让更新幅度不随形状漂移。

#### 4.2.2 核心流程

```text
输入：grad（当前梯度）、momentum（动量缓冲区，会被原地更新）
  1. momentum ← momentum + (1-β) · (grad - momentum)        # 即 momentum.lerp_(grad, 1-β)
  2. update = grad + β · (momentum - grad)  （nesterov=True）  # 即 grad.lerp_(momentum, β)
     或 update = momentum                （nesterov=False）
  3. 若 update.ndim == 4：update = update.view(批量数, -1)     # 卷积核 → 2D
  4. update = zeropower_via_newtonschulz5(update, steps=ns_steps)
  5. update ← update · max(1, 行数/列数)^0.5
返回：update（与 grad 同形状——第 3 步展平的会在类里 reshape 回去）
```

其中 `lerp_` 是 PyTorch 的线性插值原地操作：`t.lerp_(other, w)` 等价于 \( t \leftarrow t + w \cdot (other - t) \)。

第 5 步的尺度因子：

\[ \text{update} \leftarrow \text{update} \cdot \max\!\left(1,\ \frac{\text{rows}}{\text{cols}}\right)^{1/2} \]

由于第 4 步之后奇异值约等于 1，更新的整体幅度（RMS）几乎完全由矩阵形状决定、与梯度大小无关——这正是「学习率以谱范数为单位」的物理基础。推导留到 u2-l3，本讲记住结论即可。

#### 4.2.3 源码精读

完整函数只有 8 行（本讲唯一一个可以整段引用的函数）：

- [muon.py:L34-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L36) —— 签名带四个可调参数：`beta=0.95`（动量系数）、`ns_steps=5`（正交化迭代步数）、`nesterov=True`。第 35 行更新动量缓冲区；第 36 行是 Nesterov 写法。注意一个细节：`grad.lerp_(momentum, beta)` 会**原地修改 grad**（也就是优化器里的 `p.grad`）——后面实践会验证这一点。

- [muon.py:L37-L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L38) —— 卷积核分支：`(out_channels, in_channels, kH, kW)` 的 4 维权重被 `view` 成 `(out_channels, in_channels·kH·kW)` 的 2D 矩阵再正交化。这就是「隐藏卷积权重视为 2D 并折叠后三维」的落点。

- [muon.py:L39-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39-L41) —— 调用正交化，然后乘上尺度因子 `max(1, update.size(-2) / update.size(-1))**0.5`，返回更新量。

值得注意的地图级事实：**4 个优化器类调用 `muon_update` 时都只传了 `beta`**（如 [L91](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L91)、[L123](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L123)、[L205](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L205)、[L267](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267)），也就是说 `ns_steps` 和 `nesterov` 没有在任何类的构造函数里暴露——想改它们只能改 `muon_update` 的默认值（这是 u4-l3 二次开发的话题）。

#### 4.2.4 代码实践

**实践目标**：验证两件事——①卷积核分支的形状变换；②`grad.lerp_` 会原地改写梯度。

**操作步骤**（示例代码，非项目原有）：

```python
import torch
from muon import muon_update

torch.manual_seed(0)
# ① 4 维卷积核梯度：8 个 3x3x3 卷积核
grad = torch.randn(8, 3, 3, 3)
momentum = torch.zeros_like(grad)
grad_before = grad.clone()

update = muon_update(grad, momentum, beta=0.95)
print("输入形状:", tuple(grad.shape), "→ 输出形状:", tuple(update.shape))

# ② 检查 grad 是否被原地修改
print("grad 被修改了吗:", not torch.equal(grad, grad_before))
print("momentum 被更新了吗:", momentum.abs().sum().item() > 0)
```

**需要观察的现象**：输出形状从 `(8, 3, 3, 3)` 变成 `(8, 27)`；`grad` 与副本不再相等。

**预期结果**：输出 `torch.Size([8, 27])`（3×3×3 = 27 被压平）；`grad 被修改了吗: True`，`momentum 被更新了吗: True`。因为第 36 行 `grad.lerp_(momentum, beta)` 原地改写了 grad。具体数值待本地验证。

**延伸观察**（可选）：把输入换成 `(512, 1024)` 和 `(1024, 512)` 各跑一次，比较两次 `update` 的整体 RMS（`update.pow(2).mean().sqrt()`），思考为什么形状互转会得到不同缩放——答案在 u2-l3 揭晓。

#### 4.2.5 小练习与答案

**练习 1**：`momentum.lerp_(grad, 1 - beta)` 等价于什么公式？

**答案**：\( momentum \leftarrow momentum + (1-\beta)(grad - momentum) = \beta \cdot momentum + (1-\beta) \cdot grad \)，即标准的指数滑动平均动量更新，β=0.95 时新梯度只占 5%。

**练习 2**：`nesterov=True` 分支里，被送去正交化的 `update` 由哪两份信息构成？

**答案**：`grad.lerp_(momentum, beta)` = \( (1-\beta) \cdot grad + \beta \cdot momentum \)，即当前梯度与动量的再次混合——「先沿动量方向迈一步，再在展望点取梯度」的 Nesterov 风格紧凑写法。

**练习 3**：`(16, 32, 3, 3)` 的卷积核经过 `muon_update` 后形状是什么？优化器类最后如何把它加回参数？

**答案**：`view(16, 32·3·3)` = `(16, 288)`。类里施加更新前有 `update.reshape(p.shape)`（如 [L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L93)、[L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L125)），先还原成 4 维再 `p.add_`。

### 4.3 Muon 与 SingleDeviceMuon：纯 Muon 优化器的两种形态

#### 4.3.1 概念说明

这一对类对应选型的第一个维度：**单机还是分布式**。

- `Muon`：原版，设计给多 GPU 训练。它的 `step()` 会把参数**分摊**给各个 rank 计算，再用 `all_gather` 把结果广播给所有人——用通信换计算并行。
- `SingleDeviceMuon`：单进程版，`step()` 就是最朴素的「遍历所有参数逐个更新」，**完全不调用** `torch.distributed`（u1-l2 我们已经在 CPU 上用过它）。

「纯 Muon」意味着：你传给它的参数**必须全部是 ≥2 维的隐藏层权重**。README（[L13-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L13-L14)）说得很清楚：embedding、分类头、增益/偏置要用标准 AdamW——那是 4.5 节两个 `*WithAuxAdam` 类的职责。

#### 4.3.2 核心流程

**所有 4 个类共享的 `step()` 骨架**（本讲的第三条学习目标，先在这里建立）：

```text
step(closure):
  （可选）执行 closure 计算并返回 loss
  for group in self.param_groups:                  # 遍历参数组
    for p in 本进程负责的参数:                        # 遍历参数
      若 p.grad 为 None: p.grad = 全零张量            # "Force synchronization"
      state = self.state[p]
      若 state 为空: 初始化动量缓冲区（惰性初始化）      # 第一次 step 才分配
      update = muon_update(p.grad, 动量, beta=...)    # 4.2 的流水线
      p.mul_(1 - lr · weight_decay)                  # ① 解耦权重衰减
      p.add_(update.reshape(p.shape), alpha=-lr)     # ② 施加更新
```

这个骨架在文件里出现了 6 次（4 个类，其中两个 Aux 类各含 Muon 分支和 Adam 分支）。看清骨架后，`Muon` 与 `SingleDeviceMuon` 的差异就只剩两处：

1. `__init__`：`Muon` 额外做了「参数必须是显式列表」的断言和**按尺寸降序排序**；
2. `step()`：`Muon` 用 `world_size` / `rank` / `all_gather` 把「for p in 参数」改造成分片循环。

`Muon.step` 的分布式循环（概述，细节在 u3-l3）：

```text
params 已按尺寸从大到小排序
补齐：params_pad = params + 若干个空张量，使总数是 world_size 的倍数
for base_i in 0, world_size, 2·world_size, ...:      # 每轮处理 W 个参数
    本 rank 处理 params[base_i + rank]               # 各 rank 分摊不同的参数
    all_gather 把这一轮 W 个参数的新值广播给所有 rank  # 保证所有卡参数一致
```

#### 4.3.3 源码精读

- [muon.py:L44-L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L64) —— `Muon` 类的 docstring：全称 MomentUm Orthogonalized by Newton-schulz；「内部跑标准 SGD 动量，再做正交化后处理」；以及最重要的使用约束——只用于隐藏权重层。`lr` 的文档是「学习率，单位是每次更新的谱范数」。

- [muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69) —— `Muon.__init__`：默认超参 `lr=0.02, weight_decay=0, momentum=0.95`；断言 `params` 必须是**非空的 Parameter 列表**（不能传生成器或 `model.parameters()` 的直接结果）；随后 `sorted(params, key=lambda x: x.size(), reverse=True)` 按尺寸降序排序——排序的目的是让同一轮被各 rank `all_gather` 的参数形状一致（通信要求同位置张量形状相同，u3-l3 详述）。

- [muon.py:L71-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L71-L94) —— `Muon.step` 的分布式主体。对照 4.3.2 的骨架：L81 补齐空张量、L82 按 `world_size` 为步长循环、L83-L84 本 rank 领取第 `base_i + rank` 个参数、L85-L87 梯度缺失时填零（注释 `# Force synchronization`——分布式下所有 rank 必须执行完全相同的通信序列，谁也不能中途跳过一个参数，否则 `all_gather` 对不齐）、L88-L90 惰性初始化动量、L91-L93 调 `muon_update` 并施加两行更新、L94 `all_gather` 同步。

- [muon.py:L99-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L105) —— `SingleDeviceMuon.__init__`：与 `Muon` 默认超参完全相同，但**没有**列表断言、**没有**排序——单机不需要为通信做任何准备。

- [muon.py:L107-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L107-L127) —— `SingleDeviceMuon.step`：这是**骨架的最纯净样本**——L115-L125 与 4.3.2 的伪代码逐行对应，没有任何分布式代码。建议把它当作 memorize 的模板。

两类的关键差异对照表：

| | `Muon`（L44-L96） | `SingleDeviceMuon`（L99-L127） |
|---|---|---|
| 需要 `dist.init_process_group` | 是（`step` 内用 `get_world_size`/`all_gather`） | 否 |
| `params` 要求 | 非空 Parameter 列表（L67 断言） | 任意 PyTorch 接受的形式 |
| 排序 | 按尺寸降序（L68） | 不排序 |
| 每步负责的参数 | 本 rank 分到的第 rank、rank+W、… 个 | 全部 |
| 适用参数 | 仅隐藏层 ≥2 维权重 | 同左 |

#### 4.3.4 代码实践

**实践目标**：用三个小实验分别验证「单机版骨架」「一维参数报错位置」「分布式类未初始化时的行为」。

**操作步骤**（示例代码，非项目原有）：

```python
# 实验 A：观察 state 里的动量缓冲区（CPU 可跑）
import torch
from muon import SingleDeviceMuon

w1 = torch.nn.Parameter(torch.randn(64, 32))
w2 = torch.nn.Parameter(torch.randn(16, 8))
opt = SingleDeviceMuon([w1, w2], lr=0.02)
(w1.sum() + w2.sum()).backward()
print("step 前 state:", opt.state)          # 空
opt.step()
print("step 后 state 键:", list(opt.state[w1].keys()))
print("动量形状:", tuple(opt.state[w1]["momentum_buffer"].shape))
```

```python
# 实验 B：误传一维参数（bias），观察报错位置
b = torch.nn.Parameter(torch.randn(64))     # 一维！
opt = SingleDeviceMuon([b])
b.sum().backward()
opt.step()   # 预期在 zeropower_via_newtonschulz5 的 assert 处抛 AssertionError
```

```python
# 实验 C：不初始化 distributed 就用分布式版
from muon import Muon
w = torch.nn.Parameter(torch.randn(8, 4))
opt = Muon([w])            # 构造可以成功（__init__ 不碰 dist）
w.sum().backward()
opt.step()                 # 预期 dist.get_world_size() 抛 RuntimeError
```

**需要观察的现象**：A 中 `step()` 调用前后 `opt.state` 从空变成包含 `momentum_buffer`；B 抛出断言错误；C 抛出分布式未初始化的错误。

**预期结果**：A —— `state` 键为 `['momentum_buffer']`，形状 `(64, 32)` 与参数相同（这验证了 4.3.2 骨架里的「惰性初始化」：状态在第一次 `step` 才创建）。B —— `AssertionError`，来自 [muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15) 的 `assert G.ndim >= 2`（一维梯度一路传到正交化函数才被拦下）。C —— `RuntimeError`，信息类似 "Default process group has not been initialized"（因为 [L81](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81) 首次调用 `dist.get_world_size()`）；这也印证了 u1-l2 的结论：**没有分布式环境就用 `SingleDevice*`**。三个实验的具体报错文本待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`Muon.__init__` 为什么断言 `params` 必须是列表，还要按尺寸排序？

**答案**：列表断言（[L67](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67)）是因为构造函数要检查 `params[0]` 并对同一列表做排序，生成器只能消费一次，不满足要求。排序（[L68](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L68)）是为了让同一轮 `all_gather` 中各 rank 拿到的参数形状一致——通信原语要求所有 rank 同位置张量形状相同。`SingleDeviceMuon` 没有通信，所以两者都不需要。

**练习 2**：`p.grad` 为 `None` 时为什么填零张量而不是 `continue` 跳过？

**答案**：[L87](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L87) 注释写明 `# Force synchronization`——分布式下每个 rank 必须执行完全相同的 `all_gather` 调用序列；若某个 rank 因梯度缺失跳过一个参数，其他 rank 的 `all_gather` 会永远等不到对应数据。单机版（[L119](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L119)）保留了同样的写法，让两个类的行为保持一致。

**练习 3**：骨架最后「两行更新」分别做什么？

**答案**：`p.mul_(1 - lr·weight_decay)` 是 AdamW 式**解耦权重衰减**（把参数自身往 0 收缩，与梯度无关）；`p.add_(update, alpha=-lr)` 沿负更新方向迈出步长 `lr`。学习率乘在已经被正交化、缩放过的 `update` 上，所以其单位是「谱范数/步」。

### 4.4 adam_update：6 行手写 AdamW

#### 4.4.1 概念说明

Muon 的使用守则是「隐藏矩阵权重用 Muon，其余参数用 AdamW」。为了让「其余参数」也活在这同一个优化器里，作者手写了 AdamW 的更新核心——这就是 `adam_update`，6 行代码，无状态纯函数（一阶、二阶动量由调用方保管并传入）。

它对应的数学就是标准 Adam 更新规则（不含权重衰减——权重衰减由类里那行 `p.mul_` 统一处理）：

\[
m_t = \beta_1 m_{t-1} + (1-\beta_1) g_t, \qquad
v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2
\]

\[
\hat{m}_t = \frac{m_t}{1-\beta_1^t}, \qquad
\hat{v}_t = \frac{v_t}{1-\beta_2^t}, \qquad
\Delta_t = \frac{\hat{m}_t}{\sqrt{\hat{v}_t} + \varepsilon}
\]

#### 4.4.2 核心流程

```text
输入：grad、buf1（一阶动量 m）、buf2（二阶动量 v）、step（步数 t）、betas=(β1, β2)、eps
  1. buf1 ← β1·buf1 + (1-β1)·grad            # lerp_ 原地更新
  2. buf2 ← β2·buf2 + (1-β2)·grad²           # lerp_ 原地更新
  3. buf1c = buf1 / (1 - β1^t)               # 偏差修正（从 0 初始化导致早期偏小）
  4. buf2c = buf2 / (1 - β2^t)
  5. return buf1c / (√buf2c + eps)
输出：与 grad 同形状的更新量
```

为什么不用 `torch.optim.AdamW` 而要手写？看 `MuonWithAuxAdam` 的 docstring（[L145-L146](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L145-L146)）：这个类的意义就是让用户代码里**只有一个优化器、只调用一次 step()**。手写核心之后，AdamW 参数的更新能和 Muon 参数的更新共用同一个 `step()` 循环、同一套参数组机制（以及将来同一套分布式同步逻辑）。与 `torch.optim.AdamW` 的逐步等价性验证是 u3-l1 的实战内容。

#### 4.4.3 源码精读

- [muon.py:L130-L132](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L132) —— 签名接收六个参数；两行 `lerp_` 分别更新一阶动量 `buf1`（梯度的滑动平均）和二阶动量 `buf2`（梯度平方的滑动平均），与上面公式逐项对应。注意 `grad.square()`：二阶动量混合的是梯度的平方。

- [muon.py:L133-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L133-L135) —— 两次偏差修正除法，然后返回 \( \hat{m} / (\sqrt{\hat{v}} + \varepsilon) \)。细节：`eps` 加在 `sqrt` **之后**（分母是 \( \sqrt{\hat v} + \varepsilon \)），与 torch 官方 AdamW 的处理方式一致，但默认值是 `1e-10` 而非 torch 的 `1e-8`（见 4.5 节 [L179](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L179)）——speedrun 场景下梯度往往较大，更小的 eps 让更新更「诚实」。

#### 4.4.4 代码实践

**实践目标**：通过「逐行对照公式」把 6 行代码变成你自己的 AdamW 知识（源码阅读型实践，无需运行）。

**操作步骤**：

1. 打开 PyTorch 官方文档中 `torch.optim.AdamW` 的算法描述页。
2. 对照 [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135)，在笔记里填写下面的表格：

| 代码 | 对应公式 | 作用 |
|---|---|---|
| `buf1.lerp_(grad, 1 - betas[0])` | \( m_t = \beta_1 m_{t-1} + (1-\beta_1) g_t \) | 待填 |
| `buf2.lerp_(grad.square(), 1 - betas[1])` | \( v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2 \) | 待填 |
| `buf1c = buf1 / (1 - betas[0]**step)` | \( \hat m_t = m_t / (1-\beta_1^t) \) | 待填 |
| `buf2c = buf2 / (1 - betas[1]**step)` | \( \hat v_t = v_t / (1-\beta_2^t) \) | 待填 |
| `return buf1c / (buf2c.sqrt() + eps)` | \( \Delta_t = \hat m_t / (\sqrt{\hat v_t} + \varepsilon) \) | 待填 |

**需要观察的现象**：填表时会发现「AdamW 的 W（解耦权重衰减）不在这个函数里」。

**预期结果**：作用列依次为「更新一阶动量 / 更新二阶动量 / 修正一阶偏差 / 修正二阶偏差 / 合成最终更新」。而权重衰减确实缺席——它在所有调用方的骨架里由 `p.mul_(1 - lr·weight_decay)` 统一完成（如 [L222](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L222)、[L283](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L283)），Muon 分支和 Adam 分支用的是同一行代码。想进一步用数值实验验证它与 `torch.optim.AdamW` 等价，留到 u3-l1。

#### 4.4.5 小练习与答案

**练习 1**：`adam_update` 的 `step` 参数从哪里来？

**答案**：来自优化器的 `state["step"]` 计数器——`*WithAuxAdam` 的 Adam 分支在惰性初始化时置 0、每次 `step()` 自增 1（[L218-L219](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L218-L219)、[L278-L280](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L278-L280)）。

**练习 2**：为什么 `MuonWithAuxAdam` 不直接在内部持有一个 `torch.optim.AdamW` 实例？

**答案**：docstring（[L145-L146](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L145-L146)）点明设计意图：让用户代码里只有**一个**优化器、只 `step()` 一次。手写 `adam_update` 后，Adam 参数与 Muon 参数共用同一个 `param_groups` 机制和同一个 `step()` 循环，超参管理和（分布式的）同步逻辑都只需写一遍。

**练习 3**：函数里为什么没有权重衰减？

**答案**：`adam_update` 只负责「梯度 → 更新量」；AdamW 的解耦权重衰减是「参数自身乘 `(1 - lr·wd)`」的动作，由调用方骨架中统一的 `p.mul_` 行完成，对 Muon 分支和 Adam 分支一视同仁。

### 4.5 MuonWithAuxAdam 与 SingleDeviceMuonWithAuxAdam：一个优化器管所有参数

#### 4.5.1 概念说明

这一对类对应选型的第二个维度：**是否需要 AdamW 混合**。

实际模型里既有隐藏矩阵权重（想用 Muon），也有 embedding、输出头、增益和偏置（必须用 AdamW）。朴素做法是建两个优化器、每步分别 `step()`。`*WithAuxAdam` 的做法是：**一个优化器、多个参数组、每组一个 `use_muon` 开关**——`use_muon=True` 的组走 Muon 路径，`False` 的组走内置 AdamW 路径。

这正是 README 给出的标准用法（[L17-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L17-L33)）：把 `torch.optim.AdamW(model.parameters(), ...)` 替换成一个 `MuonWithAuxAdam(param_groups)`。

于是一个 **2×2 选型矩阵**就成立了（本讲核心结论）：

| | 纯 Muon（只喂隐藏矩阵权重） | 带 Aux Adam（喂全部参数） |
|---|---|---|
| **单机**（不初始化 distributed） | `SingleDeviceMuon` | `SingleDeviceMuonWithAuxAdam` |
| **分布式**（多 GPU + `init_process_group`） | `Muon` | `MuonWithAuxAdam` |

经验法则：**绝大多数真实模型选右列**（因为总有 embedding/head/bias）；左列适合「纯矩阵模型」或你想自己另配一个 AdamW 优化器的场景。**没有多卡环境就选上行**。

#### 4.5.2 核心流程

`__init__` 是一个「参数组校验 + 默认值注入」的网关：

```text
for group in param_groups:
  断言 "use_muon" in group                       # 忘了打标签直接报错
  if use_muon 为 True:
      （仅分布式版）参数按尺寸降序排序
      注入默认值：lr=0.02, momentum=0.95, weight_decay=0
      断言 group 的键集合 == {params, lr, momentum, weight_decay, use_muon}
  else:
      注入默认值：lr=3e-4, betas=(0.9,0.95), eps=1e-10, weight_decay=0
      断言 group 的键集合 == {params, lr, betas, eps, weight_decay, use_muon}
交给 torch.optim.Optimizer.__init__ 收尾
```

注意两类默认超参的**量纲差别**：Muon 组 `lr=0.02`（谱范数单位）vs Adam 组 `lr=3e-4`（AdamW 常规单位）——差了近两个数量级，混用必然翻车，这也是必须分组的原因之一。

`step()` 则是骨架 + 双分支：

```text
for group in self.param_groups:
  if group["use_muon"]:
      （分布式版）参数分片 + all_gather 的 Muon 循环     # 即 4.3 的 Muon.step 主体
  else:
      for p in group["params"]:
          梯度缺失填零；惰性初始化 exp_avg / exp_avg_sq / step
          update = adam_update(...)                     # 即 4.4 的函数
          p.mul_(1 - lr·wd) ; p.add_(update, alpha=-lr)
```

#### 4.5.3 源码精读

- [muon.py:L139-L163](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L139-L163) —— `MuonWithAuxAdam` 的 docstring，内含一段可以直接抄的参数组构造示例：按 `named_parameters()` 的维度和名字把参数切成 `hidden_matrix_params` / `embed_params` / `scalar_params` / `head_params`，分别装进带 `use_muon` 标志的组。README 的简化版配方（[L26-L32](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L26-L32)）是同一思想的两分组写法。

- [muon.py:L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182) —— `MuonWithAuxAdam.__init__`：Muon 组默认 `lr=0.02 / momentum=0.95 / weight_decay=0`（L171-L173），Adam 组默认 `lr=3e-4 / betas=(0.9,0.95) / eps=1e-10 / weight_decay=0`（L177-L180）；L167 的断言强制每个组必须显式声明 `use_muon`；L174 / L181 两个**键集合断言**非常严格——多一个键、少一个键都会失败，这是把「参数组配置写错」提前到构造期就炸出来的防御式设计。

- [muon.py:L184-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L184-L208) —— `step()` 的 `use_muon=True` 分支：与 `Muon.step`（4.3）逐行相同——排序补齐、分片领取、填零、惰性初始化 `momentum_buffer`、`muon_update`、两行更新、`all_gather`。

- [muon.py:L209-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L209-L223) —— `step()` 的 `use_muon=False` 分支：Adam 路径。状态是三个键 `exp_avg` / `exp_avg_sq` / `step`（L216-L218），每步自增（L219），调 `adam_update` 后施加与 Muon 分支**完全相同**的两行更新（L222-L223）——再次印证 4.3.2 的骨架。

- [muon.py:L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248) —— `SingleDeviceMuonWithAuxAdam.__init__`：与分布式版的默认值、断言**完全一致**，唯一差别是 Muon 组**不做排序**（对比 [L169](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L169) 与 [L235-L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L235-L240)）——单机无通信，排序没有意义。

- [muon.py:L250-L286](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L250-L286) —— `SingleDeviceMuonWithAuxAdam.step`：两个分支分别是 `SingleDeviceMuon.step` 与 Adam 路径的拼合，无任何分布式代码。CPU 学习者的主力类（u1-l1、u1-l2 的实践用的都是它或它的纯版）。

两个 Aux 类的差异对照：

| | `MuonWithAuxAdam`（L138-L225） | `SingleDeviceMuonWithAuxAdam`（L228-L286） |
|---|---|---|
| 需要 `dist` 初始化 | 是 | 否 |
| Muon 组参数排序 | 是（L169） | 否 |
| Muon 分支 `all_gather` 分片 | 是（L208） | 否（L259-L269 朴素循环） |
| `__init__` 默认值与断言 | 相同 | 相同 |
| Adam 分支 | 相同（L209-L223 vs L270-L284） | 相同 |

#### 4.5.4 代码实践

**实践目标**：亲手触发一次「严格键校验」的断言，理解防御式设计的意图；并核对默认超参注入。

**操作步骤**（示例代码，非项目原有）：

```python
import torch
from muon import SingleDeviceMuonWithAuxAdam

w = torch.nn.Parameter(torch.randn(8, 4))   # 假装的隐藏权重
b = torch.nn.Parameter(torch.randn(8))      # 假装的 bias

# ① 故意给 muon 组塞一个 Adam 才有的键 betas
opt = SingleDeviceMuonWithAuxAdam([
    dict(params=[w], use_muon=True, betas=(0.9, 0.95)),   # 多余键！
    dict(params=[b], use_muon=False),
])
```

```python
# ② 修正后再构造，打印默认值注入的结果
opt = SingleDeviceMuonWithAuxAdam([
    dict(params=[w], use_muon=True),
    dict(params=[b], use_muon=False),
])
for g in opt.param_groups:
    print({k: v for k, v in g.items() if k != "params"})
```

**需要观察的现象**：①在构造阶段就抛 `AssertionError`；②打印出两类参数组各自的默认超参。

**预期结果**：① 断言来自 [muon.py:L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L240)：muon 组的键集合只允许 `{params, lr, momentum, weight_decay, use_muon}`，`betas` 是多余键——把「Muon 组配了 Adam 超参」这类配置错误拦在训练开始之前。② muon 组打印 `{'use_muon': True, 'lr': 0.02, 'momentum': 0.95, 'weight_decay': 0}`，adam 组打印 `{'use_muon': False, 'lr': 3e-04, 'betas': (0.9, 0.95), 'eps': 1e-10, 'weight_decay': 0}`。具体输出格式待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`use_muon=True` 的参数组允许出现哪些键？

**答案**：恰好五个：`{params, lr, momentum, weight_decay, use_muon}`（[L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L240) / [L174](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L174)），多一个少一个都会断言失败；不填的键会先被默认值补齐。

**练习 2**：说出 `MuonWithAuxAdam` 与 `SingleDeviceMuonWithAuxAdam` 的三个差异。

**答案**：①是否要求分布式初始化（`step` 中是否调用 `dist.get_world_size` / `all_gather`）；②Muon 组参数是否按尺寸降序排序（[L169](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L169) 有、[L235-L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L235-L240) 无）；③Muon 分支更新如何施加（分片 + `all_gather` vs 本地循环）。`__init__` 的默认值和断言两者完全一致。

**练习 3**：你在 8 张卡的机器上训练一个带 embedding 和 lm_head 的 transformer，该选哪个类？只有一台 MacBook 呢？

**答案**：8 卡 → `MuonWithAuxAdam`（分布式 + 模型含非矩阵参数）；MacBook CPU → `SingleDeviceMuonWithAuxAdam`。如果模型恰好只有隐藏矩阵权重（例如 u1-l2 的 toy 回归），才可以退化用 `Muon` / `SingleDeviceMuon`。

## 5. 综合实践

把整张代码地图「写一遍 + 标一遍」，完成本讲配套的两个任务。

### 任务一：制作你的选型速查表（学习笔记）

新建一个 markdown 笔记，画一张 7 行表格（4 个优化器类 + 3 个函数），列为：**所在行号、是否需要 torch.distributed 初始化、适用参数、一句话用途**。先凭记忆填，再对照源码检查。

参考答案（可直接核对）：

| 对象 | 所在行号 | 需要 dist 初始化？ | 适用参数 | 一句话用途 |
|---|---|---|---|---|
| `zeropower_via_newtonschulz5` | L5-L31 | 否 | 任意 ≥2 维矩阵（支持批量） | NS 迭代近似正交化，输出 US'Vᵀ |
| `muon_update` | L34-L41 | 否 | 2D 矩阵 / 4D 卷积核 | 动量→Nesterov→展平→正交化→缩放 |
| `Muon` | L44-L96 | 是（step 内） | 仅隐藏层 ≥2 维权重 | 多卡分摊更新的纯 Muon 优化器 |
| `SingleDeviceMuon` | L99-L127 | 否 | 仅隐藏层 ≥2 维权重 | 单机纯 Muon 优化器 |
| `adam_update` | L130-L135 | 否 | 任意形状 | 6 行手写 AdamW 更新核心 |
| `MuonWithAuxAdam` | L138-L225 | 是（step 内） | 全部参数（按 use_muon 分组） | 多卡、单优化器混合 Muon+AdamW |
| `SingleDeviceMuonWithAuxAdam` | L228-L286 | 否 | 全部参数（按 use_muon 分组） | 单机、单优化器混合 Muon+AdamW |

### 任务二：制作你的「导航注释版」muon.py

把 muon.py 复制到自己的练习目录（例如 `my_notes/muon.py`），在文件顶部粘贴下面这段导航注释，并在每个类/函数定义行的上一行各加一条对应注释：

```python
# ---------- my_notes/muon.py 导航注释（示例代码） ----------
# L5-31    zeropower_via_newtonschulz5   正交化纯函数（NS5 迭代）
# L34-41   muon_update                   梯度 → Muon 更新的五道工序
# L44-96   Muon                          分布式 · 纯 Muon
# L99-127  SingleDeviceMuon              单机 · 纯 Muon（骨架最纯净样本）
# L130-135 adam_update                   手写 AdamW 核心
# L138-225 MuonWithAuxAdam               分布式 · 混合（use_muon 分组）
# L228-286 SingleDeviceMuonWithAuxAdam   单机 · 混合
```

然后做一次「骨架巡检」：在副本里用编辑器搜索 `p.mul_(1 - group["lr"]`，数一数命中次数。

**预期结果**：命中 6 处——`Muon`、`SingleDeviceMuon` 各 1 处，两个 Aux 类各 2 处（Muon 分支 + Adam 分支）。这正是 4.3.2 所说「同一个骨架在文件里出现 6 次」的直接证据；以后你精读任何一处，其余 5 处都可以跳读。

（本任务只操作你复制出的副本，不要改动仓库源码。）

## 6. 本讲小结

- muon.py 总共 287 行、7 个顶层对象，分成两层：**3 个无状态函数做数学**（`zeropower_via_newtonschulz5` 正交化、`muon_update` 五道工序流水线、`adam_update` 手写 AdamW），**4 个优化器类做编排**（在 `step()` 里调度前两者）。
- 选型只需两个维度：单机/分布式 × 纯 Muon/带辅助 AdamW，对应 2×2 矩阵——`SingleDeviceMuon`、`Muon`、`SingleDeviceMuonWithAuxAdam`、`MuonWithAuxAdam`。真实模型几乎总要选带 Aux Adam 的一列。
- 所有类共享同一副 `step()` 骨架：遍历参数组 → 梯度缺失填零（Force synchronization）→ `state` 惰性初始化 → 调用更新函数 → `p.mul_(1-lr·wd)` + `p.add_(update, alpha=-lr)` 两行原地更新。该骨架在文件里出现 6 次。
- 分布式版与单机版的差异集中在两处：`__init__` 里按尺寸排序（为 `all_gather` 形状一致服务）、`step()` 里的参数分片循环；`*WithAuxAdam` 的构造函数用严格的键集合断言把配置错误拦在训练开始前。
- 值得记住的默认值：Muon 侧 `lr=0.02, momentum=0.95`（谱范数量纲）；Adam 侧 `lr=3e-4, betas=(0.9, 0.95), eps=1e-10`——两组学习率相差近两个数量级，不可混用。
- `ns_steps` 和 `nesterov` 没有在任何类的构造函数中暴露，类调用 `muon_update` 时永远用默认值 5 / True；想改它们只能进函数内部（u4-l3 的二次开发主题）。

## 7. 下一步学习建议

下一讲 u2-l1「数学预热：梯度正交化与 SVD 的直觉」将第一次钻进 4.1 节标出的山洞：用 `torch.linalg.svd` 亲手构造 \( UV^T \)，理解 Frobenius 范数与谱范数在正交化前后的变化，为逐行精读 Newton-Schulz 迭代（u2-l2）建立数学地基。

在进入下一讲之前，建议你：

1. 把 4.3.2 的共享骨架伪代码抄写一遍——它是第二单元所有精读的「参照系」。
2. 重跑 u1-l2 的 toy 训练脚本，但现在**边跑边在脑中标注**：训练循环每调一次 `opt.step()`，代码正走过本讲地图的哪些行。
3. 通读 [muon.py:L45-L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L45-L64) 的 `Muon` docstring 与 [README.md](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47) 的调参建议（momentum/nesterov/ns_steps 用默认值即可，只需调 lr 和 weight_decay），体会「地图」与「用法」如何互相印证。
