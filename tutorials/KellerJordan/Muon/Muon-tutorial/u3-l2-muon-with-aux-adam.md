# MuonWithAuxAdam：use_muon 参数组设计与单优化器混合训练

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `use_muon` 标志与 `param_groups` 混合分组机制：为什么一个优化器里能同时跑 Muon 和 AdamW 两条路径，以及用户为什么必须**手动**指定每个参数组走哪条路。
2. 逐行读懂 `MuonWithAuxAdam.__init__`（[muon.py:L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182)）：muon 组与 adam 组各自被注入哪些默认超参、为什么最后传给父类的 `defaults` 是个**空字典**、严格键集合断言 `assert set(group.keys()) == ...` 防的是什么错。
3. 说明 `step()` 中 `use_muon=True` 与 `use_muon=False` 两条分支的差异：复用 `muon_update` 还是 `adam_update`、`self.state[p]` 里存什么、分布式版里为什么只有 muon 分支需要集合通信。
4. 掌握按 `named_parameters` 的 `ndim` 与名称划分参数的惯用法（隐藏矩阵 / embedding / 标量 / 输出头四分法），并能写出「不重不漏」的自检代码。
5. 亲手把一个带 embedding、3 个隐藏线性层和输出头的 MLP 交给 `SingleDeviceMuonWithAuxAdam` 训练，并触发一次严格断言报错。

本讲是 u3 单元的第二讲：u2-l5 给出了不含 AdamW 的最简骨架 `SingleDeviceMuon.step`，u3-l1 给出了 6 行数学函数 `adam_update`——本讲把这两块拼装进同一个优化器，覆盖 [muon.py:L138-L287](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L287) 的全部 150 行。

## 2. 前置知识

本讲建立在 u2-l4（PyTorch Optimizer 扩展约定）、u2-l5（SingleDeviceMuon.step 精读）与 u3-l1（adam_update 精读）之上。开始前请确认你还记得：

- **param_groups / defaults / self.state**（u2-l4）：超参按参数组作用；父类构造函数用 `defaults` 给缺键的组**补默认值**、不覆盖显式键、保留 `use_muon` 这类额外键；`self.state[p]` 是以参数对象为键、惰性初始化的逐参数记忆。
- **单机版骨架**（u2-l5）：`for group → for p → grad 填零 → 惰性初始化 state → 算 update → p.mul_/p.add_ 两行`；分布式版多出的「params 必须显式列表 + 按 size 降序排序」服务于 `all_gather` 的形状一致。
- **muon_update 五步流水线**（u2-l3）：EMA 动量 → Nesterov → 卷积核展平 → Newton-Schulz 正交化 → 尺度缩放；输出是二维矩阵，所以施加更新前要 `update.reshape(p.shape)`。
- **adam_update 六行数学**（u3-l1）：两条 `lerp_` 动量、两次偏差修正、输出 `buf1c / (buf2c.sqrt() + eps)`；`exp_avg` / `exp_avg_sq` / `step` 三个状态由**调用方**（也就是本讲的 adam 分支）保管。
- **两种 lr 量纲**（u1-l3）：Muon 组的 lr 单位是「每次更新的谱范数」（默认 0.02），Adam 组的 lr 单位是逐坐标更新幅度（默认 3e-4），两者相差近两个数量级，不可混用。

本讲几乎不需要新的数学知识，但需要一点 PyTorch 建模常识：

- **`named_parameters()`**：遍历模型产出 `(名字, 参数)` 对，名字携带模块路径（如 `blocks.0.fc.weight`）。参数的**形状**（`p.ndim`）能告诉你它是不是矩阵，但**语义**（是隐藏层还是输出头）只能靠名字推断——这正是参数划分要同时用上两者的原因。
- **embedding 与输出头的特殊性**：它们通常也是二维张量，但 [muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56) 明确建议输入 embedding、最终输出层、增益与偏置都用标准方法（如 AdamW）优化。所以「二维 ⇒ 用 Muon」是**必要不充分**条件。
- **数据并行下的梯度同步**：若训练前梯度已经过 `all_reduce` 平均，各卡梯度一致，则确定性计算能保证各卡参数始终一致——这是理解「adam 分支零通信」的前提。

## 3. 本讲源码地图

本讲的主战场是 [muon.py:L138-L287](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L287)（两个组合优化器类），外加 README 的替换配方作对照：

| 位置 | 内容 | 作用 |
|---|---|---|
| [muon.py:L138-L164](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L164) | `MuonWithAuxAdam` docstring | 设计动机（单优化器）+ 官方四参数组示例 |
| [muon.py:L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182) | `MuonWithAuxAdam.__init__` | 校验 use_muon、muon 组排序、逐组注入默认超参、严格键集合断言 |
| [muon.py:L184-L225](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L184-L225) | `MuonWithAuxAdam.step` | **分布式版双分支**：muon 分支（L193-L208）+ adam 分支（L209-L223） |
| [muon.py:L228-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L248) | `SingleDeviceMuonWithAuxAdam.__init__` | 与 L165-L182 几乎逐行相同，唯一差别：**没有排序行** |
| [muon.py:L250-L286](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L250-L286) | `SingleDeviceMuonWithAuxAdam.step` | **单机版双分支**：muon 分支（L259-L269）+ adam 分支（L270-L284），本讲实践的主对象 |
| [muon.py:L44-L96](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L96) / [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) | `Muon` / `SingleDeviceMuon` | 对照物：两条分支体分别从这两个类「原样搬来」 |
| [README.md:L17-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L17-L33) | README 替换配方 | 两组式简化用法（与 docstring 的四组式对照） |

复用的两个数学函数无需重读，需要时回看：[muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41)（`muon_update`，u2-l3）与 [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135)（`adam_update`，u3-l1）。

## 4. 核心概念与源码讲解

### 4.1 从两个优化器到一个：use_muon 混合参数组的总体设计

#### 4.1.1 概念说明

u1-l1 就确立过铁律：Muon 只用于**隐藏层二维权重**，embedding、输出头、增益与偏置要用 AdamW。于是一个真实模型天然有两种参数，对应两种优化算法。在没有 `MuonWithAuxAdam` 之前，训练脚本只能这样写（示意代码）：

```python
# 示例代码：双优化器旧方案（本仓库不推荐，仅示意痛点）
muon = SingleDeviceMuon(hidden_params, lr=0.02)
adam = torch.optim.AdamW(other_params, lr=3e-4)
# 每个训练步：
loss.backward()
muon.step()
adam.step()          # 忘记写这一行 → 一半参数静默不训练
# 调度器、checkpoint、梯度裁剪的插入点都要写两遍
```

痛点不在算法而在**编排**：两个优化器要各 step 一次、配两个 LR scheduler、保存两份 `state_dict`，漏掉任何一处都是静默错误。docstring 给出了官方动机——[muon.py:L145-L146](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L145-L146)：

> The point of this class is to allow the user to have a single optimizer in their code, rather than having both a Muon and an Adam which each need to be stepped.

解决方案：**用一个优化器、多个参数组**。PyTorch 的 `param_groups` 机制本来就允许不同组携带不同超参（u2-l4），本项目再往前走一步——给每组加一个自定义布尔键 `use_muon`，`step()` 里按它分流：

- `use_muon=True` 的组走 muon 路径：调 `muon_update`，lr 语义是谱范数；
- `use_muon=False` 的组走 adam 路径：调 `adam_update`，lr 语义是逐坐标幅度。

注意一个关键设计取舍：**优化器不自动判断参数该走哪条路**。理论上可以按 `p.ndim` 自动分流（二维走 muon、一维走 adam），但 embedding 和输出头也是二维——「哪个参数算隐藏层」是模型结构语义，优化器无法从形状推断，所以 docstring 明说「The user must manually specify」（[muon.py:L141-L143](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L141-L143)）。显式指定 + 构造期严格校验，比「聪明默认 + 静默猜错」安全。

#### 4.1.2 核心流程

一个混合优化器的完整生命周期：

```text
用户侧                                       优化器侧
──────                                       ────────
model.named_parameters()
  │ 按 ndim 与名称划分（不重不漏）
  ├─ hidden_matrix_params ──► dict(params=…, use_muon=True)           ─┐
  ├─ embed_params ─────────► dict(params=…, lr=…, use_muon=False)     ─┤► param_groups
  ├─ scalar_params ────────► dict(params=…, lr=…, use_muon=False)     ─┤  （列表，可含多组）
  └─ head_params ──────────► dict(params=…, lr=…, use_muon=False)     ─┘
                                                  │
                    MuonWithAuxAdam(param_groups) ◄┘
                       __init__：断言 + 排序(muon组) + 注入默认超参
                                                  │
     训练循环：loss.backward() → optimizer.step() ◄┘
                                                  │
                     for group in self.param_groups:
                        group["use_muon"]? ──True───► muon_update 路径（L193-L208）
                                        └────False──► adam_update 路径（L209-L223）
```

数学上两条路径的唯一相同点是结尾两行原地更新（\( p \leftarrow (1-\text{lr}\cdot\lambda)\,p - \text{lr}\cdot u \)，解耦权重衰减）；update 的来源与量纲完全不同。组与组之间互不引用，因此**组的先后顺序不影响最终结果**——README 配方把 muon 组放第一（[README.md:L26-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L26-L31)），docstring 示例把它放最后（[muon.py:L161](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L161)），都对。

#### 4.1.3 源码精读

docstring 里嵌着一份**官方四组示例**（[muon.py:L151-L163](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L151-L163)），先看划分四行（L152-L155）：按 `model.blocks` 内 `ndim>=2` 且名字不含 `embed` 取隐藏矩阵；按名字含 `embed` 取 embedding；按 `ndim<2` 取标量/向量；输出头按名字显式取 `model.lm_head.weight`。再看组装（L158-L162）：

```python
adam_groups = [dict(params=head_params, lr=0.22), dict(params=embed_params, lr=0.6), dict(params=scalar_params, lr=0.04)]
adam_groups = [dict(**g, betas=(0.8, 0.95), eps=1e-10, use_muon=False) for g in adam_groups]
muon_group = dict(params=hidden_matrix_params, lr=0.05, momentum=0.95, use_muon=True)
param_groups = [*adam_groups, muon_group]
optimizer = MuonWithAuxAdam(param_groups)
```

（引自 [muon.py:L158-L162](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L158-L162)，格式略作折行。）

三个值得注意的细节：

1. **adam 侧拆成三组而非一组**：head / embed / scalar 各拿一份独立 lr（0.22 / 0.6 / 0.04）。这是 speedrun 风格的精调——不同类别参数的最优 lr 可以差一个数量级。列表推导 `dict(**g, ..., use_muon=False)` 是「先填类别特有键、再补公共键」的惯用写法。
2. **示例里的 lr 不是默认值**：muon 组 0.05、betas=(0.8, 0.95) 都来自 modded-nanogpt speedrun 的精调（背景链接见 [muon.py:L150](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L150)）；不传时构造函数会注入另一套默认值（4.2 详讲）。README 的简化配方（[README.md:L26-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L26-L31)）只用两组：一个 muon 组 + 一个合并的 adam 组，是迁移已有 AdamW 脚本时的推荐起点。
3. **`use_muon` 键能穿透父类构造**：u2-l4 讲过父类不会剥离组里的额外键，所以 step() 里 `group["use_muon"]` 一直可用——这是整个分流机制成立的前提。

#### 4.1.4 代码实践

**实践目标**：用最小组装跑通 `SingleDeviceMuonWithAuxAdam`（单机版，CPU 可用），并观察混合 `param_groups` 的真实结构。

**操作步骤**（示例代码）：

```python
# 示例代码：观察混合 param_groups 的结构（CPU 可运行）
import torch
from muon import SingleDeviceMuonWithAuxAdam

torch.manual_seed(0)
hidden_w = torch.nn.Parameter(torch.randn(64, 64))   # 假装是隐藏层权重
head_w   = torch.nn.Parameter(torch.randn(16, 64))   # 假装是输出头
bias_v   = torch.nn.Parameter(torch.randn(64))       # 一维：偏置/增益

param_groups = [
    dict(params=[hidden_w], lr=0.02, momentum=0.95, use_muon=True),
    dict(params=[head_w, bias_v], lr=3e-4, betas=(0.9, 0.95), eps=1e-10, use_muon=False),
]
optimizer = SingleDeviceMuonWithAuxAdam(param_groups)

for i, g in enumerate(optimizer.param_groups):
    keys = sorted(k for k in g if k != "params")     # params 是张量列表，单独打印
    print(f"group {i}: use_muon={g['use_muon']}  超参键={keys}  lr={g['lr']}")
    print(f"         参数形状={[tuple(p.shape) for p in g['params']]}")
```

**需要观察的现象**：group 0 的超参键恰好是 `['lr', 'momentum', 'use_muon', 'weight_decay']`（5 键），group 1 是 `['betas', 'eps', 'lr', 'use_muon', 'weight_decay']`（6 键）；`weight_decay` 是构造函数替你补上的默认 0。

**预期结果**：两组打印如上；两组的键集合互不相同——muon 组没有 `betas`/`eps`，adam 组没有 `momentum`。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么不做成「优化器按 `p.ndim` 自动分流」，而要求用户手动写 `use_muon`？

答案：`ndim>=2` 只是能用 Muon 的必要条件；embedding 与输出头同样二维，却是 docstring（[muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56)）明确要求走 AdamW 的参数。「哪个矩阵是隐藏层」属于模型结构语义，只有用户知道。自动猜会静默出错且难以发现；显式声明 + 构造期断言把错误前移到实例化的那一刻。

**练习 2**：README 两组配方与 docstring 四组示例的适用场景分别是什么？

答案：README 两组（[README.md:L26-L32](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L26-L32)）适合把现成 AdamW 脚本迁移到 Muon：一个 muon 组管隐藏权重、一个 adam 组兜住其余全部，心智负担最小。docstring 四组（[muon.py:L158-L161](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L158-L161)）适合精调阶段：head/embed/scalar 分别设 lr，榨取速度，是 speedrun 生态的用法。

**练习 3**：把 muon 组放在 param_groups 的第一个还是最后一个，会影响训练结果吗？为什么？

答案：不会。两条分支都只读写各自参数的 `p.grad`、`self.state[p]` 与 `p` 本身，组间没有任何共享状态或交叉引用；更新规则 \( p \leftarrow (1-\text{lr}\cdot\lambda)p-\text{lr}\cdot u \) 逐参数独立。组的顺序只影响执行顺序，不影响最终状态。

### 4.2 MuonWithAuxAdam.__init__：逐组注入默认超参与严格键集合断言

#### 4.2.1 概念说明

构造函数只有 18 行（[muon.py:L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182)），却承担四件事：查漏（use_muon 必填）、排序（仅 muon 组、仅分布式版需要）、**逐组注入默认超参**、**严格键集合断言**。它做了一件与前面所有类都不同的事：不给父类传 defaults——

```python
super().__init__(param_groups, dict())   # 第二个参数是空字典！
```

为什么？回顾 u2-l4：父类的 defaults 机制是「一张默认值表，补齐所有缺键的组」。它隐含假设**所有组共享同一套超参模式**。而混合优化器里 muon 组需要 `momentum`、adam 组需要 `betas`/`eps`，两组 lr 默认值还差两个数量级（0.02 vs 3e-4）——一张表表达不了「不同种类的组有不同默认值」。于是本类把默认值逻辑写在自己的循环里，按组的 `use_muon` 逐组注入，再传空 defaults 给父类。**手动注入 + 严格断言**取代了**单一 defaults 表**。

严格断言的全文是（以 adam 组为例，[muon.py:L181](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L181)）：

```python
assert set(group.keys()) == set(["params", "lr", "betas", "eps", "weight_decay", "use_muon"])
```

它防的错，按出错频率排：

| 传入情况 | 后果（若无断言） | 断言后的行为 |
|---|---|---|
| 漏写 `use_muon` | step() 取键时 `KeyError`（在训练第一步才爆） | 构造即 `AssertionError`（L167） |
| muon 组误传 `betas` / adam 组误传 `momentum` | 键被静默忽略，用户以为生效 | 构造即 `AssertionError`（键集合不符） |
| 传入不支持的开关（`nesterov`、`ns_steps`、`scheduler` 等） | 被静默忽略——u2-l5 讲过这两个量固定为 5/True，不暴露 | 构造即 `AssertionError` |
| 键名拼错（`learning_rate`、`weightdecay`） | 真超参保持默认，训练「能跑但不对」 | 构造即 `AssertionError` |

注意断言用 `==`（恰好相等）而非 `⊆`（子集）：因为断言执行前默认值已注入完毕，合法组的键集合理论上是**封闭的**——任何多余键都说明用户误解了接口，宁可拒绝。这是研究型代码「快失败」哲学的典型样本。

#### 4.2.2 核心流程

```text
__init__(param_groups):
  for group in param_groups:                    # 逐组处理
    ├─ assert "use_muon" in group               # 查漏：开关必填
    ├─ 若 use_muon=True:
    │    ├─ group["params"] = sorted(按 size 降序)   # 仅分布式版有此行（L169）
    │    ├─ group["lr"]           ← 缺省 0.02
    │    ├─ group["momentum"]     ← 缺省 0.95
    │    ├─ group["weight_decay"] ← 缺省 0
    │    └─ assert 键集合 == {params, lr, momentum, weight_decay, use_muon}   # 5 键
    └─ 若 use_muon=False:
         ├─ group["lr"]           ← 缺省 3e-4
         ├─ group["betas"]        ← 缺省 (0.9, 0.95)
         ├─ group["eps"]          ← 缺省 1e-10
         ├─ group["weight_decay"] ← 缺省 0
         └─ assert 键集合 == {params, lr, betas, eps, weight_decay, use_muon} # 6 键
  super().__init__(param_groups, dict())         # 空 defaults：默认值已手动注入
```

两个默认超参对照表（两处 `__init__` 完全一致）：

| 键 | muon 组默认（L171-L173 / L237-L239） | adam 组默认（L177-L180 / L243-L246） |
|---|---|---|
| `lr` | 0.02（谱范数单位） | 3e-4（逐坐标单位） |
| 动量 | `momentum=0.95` | `betas=(0.9, 0.95)` |
| `eps` | 无此键 | 1e-10 |
| `weight_decay` | 0 | 0 |

#### 4.2.3 源码精读

逐行读 [muon.py:L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182)：

- [muon.py:L166-L167](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L166-L167)：遍历每个组，先断言 `use_muon` 存在——这是整个分流机制的入口键。
- [muon.py:L169](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L169)：muon 组的参数按 `x.size()` 降序排序（`torch.Size` 是元组，字典序比较）。与纯 `Muon` 类的 L68 完全同一行。它服务于 step() 里 `all_gather` 的通信形状一致性（u3-l3 主题）；本讲只需记住：**排序只发生在分布式版的 muon 组**，单机版 `__init__`（L232-L248）没有这一行，adam 组永不排序。
- [muon.py:L171-L173](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L171-L173)：`group.get("lr", 0.02)` 等三行——「有则不动、无则补默认」。注意这是**原地改写用户传入的 dict**：构造之后你手里的 dict 也多了这些键（副作用式 API，调试时有用）。
- [muon.py:L174](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L174)：muon 组键集合断言（5 键）。
- [muon.py:L177-L180](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L177-L180)：adam 组四项默认值，与 u3-l1 讲过的手写分支默认完全一致（`betas=(0.9,0.95)`、`eps=1e-10`，均异于 `torch.optim.AdamW` 的默认）。
- [muon.py:L181](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L181)：adam 组键集合断言（6 键）。
- [muon.py:L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L182)：空 defaults 交给父类。父类此后只负责建立 `param_groups` / `self.state` 的容器（u2-l4），不再补任何键。

单机版 [muon.py:L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248) 与上面 17 行**逐行相同，仅缺 L169 的排序行**——因为单机没有 `all_gather`，不需要形状对齐。两个 `__init__` 是刻意的复制粘贴，保持并排可读。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「默认值注入」与「严格断言拒绝」两种行为。

**操作步骤**（示例代码）：

```python
# 示例代码：默认值注入与严格断言（CPU 可运行）
import torch
from muon import SingleDeviceMuonWithAuxAdam

p1 = torch.nn.Parameter(torch.randn(8, 8))
p2 = torch.nn.Parameter(torch.randn(8, 8))

# 实验 1：只给必填键，其余靠构造函数注入
g = dict(params=[p1], use_muon=True)          # 故意不给 lr / momentum / weight_decay
opt = SingleDeviceMuonWithAuxAdam([g])
print("注入后用户手里 的键:", sorted(g.keys()))
print("lr =", opt.param_groups[0]["lr"], " momentum =", opt.param_groups[0]["momentum"])

# 实验 2：多余的键 → 构造即报错
bad = dict(params=[p2], lr=3e-4, betas=(0.9, 0.95), eps=1e-10,
           weight_decay=0, use_muon=False, scheduler="cosine")   # 多了 scheduler
try:
    SingleDeviceMuonWithAuxAdam([bad])
    print("没有触发断言（不应发生）")
except AssertionError:
    print("AssertionError：参数组含有多余键 scheduler")

# 实验 3：把 adam 专属键传进 muon 组 → 同样报错
bad2 = dict(params=[p2], lr=0.02, momentum=0.95, betas=(0.9, 0.95), use_muon=True)
try:
    SingleDeviceMuonWithAuxAdam([bad2])
except AssertionError:
    print("AssertionError：muon 组不接受 betas")
```

**需要观察的现象**：

1. 实验 1 打印出 5 个键（`lr`、`momentum`、`params`、`use_muon`、`weight_decay`），`lr=0.02`、`momentum=0.95`——默认值被补进了**你自己的 dict**。
2. 实验 2、3 都在构造阶段抛出 `AssertionError`（断言没有自定义消息，报错信息为空，需要看 traceback 指向 [muon.py:L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L240) / [muon.py:L247](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L247) 的那一行）。

**预期结果**：三件事都发生如上。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `super().__init__(param_groups, dict())` 的第二个参数是空字典，而 `SingleDeviceMuon` 传的是三项 defaults（对照 [muon.py:L104-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L104-L105)）？

答案：单一优化器的所有组共享同一套超参模式，一张 defaults 表就够；混合优化器的 muon 组与 adam 组需要**不同的键集合与不同的 lr 默认值**，父类的「一张表补所有组」机制表达不了，于是默认值改为在子类循环里按 `use_muon` 逐组注入，父类只当容器用。

**练习 2**：断言为什么用 `set(group.keys()) == set([...])`（恰好相等），而不是允许用户多传键？

答案：断言执行时默认值已注入完毕，合法键集合是封闭的；多出来的键只有一种来源——用户以为某个开关会生效（如 `nesterov`、`scheduler`），实际它会被静默忽略。与其让训练「能跑但配置没生效」，不如在构造期拒绝。这是典型的快失败设计。

**练习 3**：用户传 `dict(params=[...], use_muon=True, nesterov=False)` 想关掉 Nesterov，会发生什么？正确的做法是什么？

答案：构造函数抛 `AssertionError`（键集合多了 `nesterov`）。u2-l5 讲过 `nesterov` 与 `ns_steps` 没有在构造函数暴露，固定为 `True`/`5`（在 `muon_update` 的默认参数里，[muon.py:L34](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34)）。想改只能复制并修改 `muon_update`（u4-l3 的二次开发主题）。

### 4.3 step() 之一：use_muon=True 分支如何复用 muon_update

#### 4.3.1 概念说明

`MuonWithAuxAdam.step` 的 muon 分支（[muon.py:L193-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L193-L208)）没有任何新算法——它就是纯 `Muon` 类 step 循环体（[muon.py:L79-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L79-L94)）原样搬进来，只是多缩进一层、外面套一个 `if group["use_muon"]:`。逐行对照可确认两者语句完全一致。

这个分支解决的问题是**分布式下的算力分摊**：一批参数不按数据切分、而按「参数」切分——设 `W = dist.get_world_size()`，第 `r` 个 rank 只负责下标为 `r, r+W, r+2W, …` 的参数（[muon.py:L196-L198](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L196-L198)），把 Newton-Schulz 正交化的矩阵乘开销摊到各卡；随后一次 `dist.all_gather`（[muon.py:L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L208)）把各卡算好的**更新后的参数值**广播给所有 rank，保证每张卡都持有全部参数的最新版本。注意 `all_gather` 同步的是参数本身而不是更新量——因为 `p.mul_`/`p.add_` 已经在 gather 之前完成了。

排序（`__init__` 的 L169）与补齐（L195 的 `params_pad`）都是为这组通信服务的：所有 rank 必须以完全相同的顺序执行同一串集合通信，每个通信窗口内的张量才能对得上。深入细节（补齐规则、rank 与参数的配对、去掉排序会发生什么）是下一讲 u3-l3 的主题，本讲只要求理解「分支体 = 纯 Muon 的 step + 一层 if」这个结构事实。

单机版 `SingleDeviceMuonWithAuxAdam` 的 muon 分支（[muon.py:L259-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L259-L269)）则把分布式三件套（排序、补齐、all_gather）全部剥掉，剩下的就是 u2-l5 精读过的 `SingleDeviceMuon.step` 循环体（L115-L125）。

#### 4.3.2 核心流程

分布式版 muon 分支（本讲只到结构层面，通信细节见 u3-l3）：

```text
for group in self.param_groups:
  use_muon=True?
    ├─ params：__init__ 时已按 size 降序排序
    ├─ params_pad = params + 空张量补齐（L195）
    └─ for base_i in 0, W, 2W, …：                 # 每个「窗口」一批通信
         ├─ 本 rank 的下标 base_i + rank 在范围内?
         │    ├─ p = params[base_i + rank]         # 本 rank 分到的参数
         │    ├─ p.grad 为 None → 填零（强制同步，u2-l5）
         │    ├─ state 惰性初始化 momentum_buffer
         │    ├─ update = muon_update(grad, momentum, beta=组 momentum)
         │    └─ p.mul_(1-lr·wd); p.add_(update.reshape(p.shape), alpha=-lr)
         └─ dist.all_gather(…)                     # 把更新后的参数广播给所有 rank
```

要点：`update.reshape(p.shape)` 只在参数是卷积核（四维）时真正改变形状——`muon_update` 内部把四维核 view 成矩阵、正交化后返回二维（u2-l3），这里负责还原；对普通二维权重它是恒等操作。

#### 4.3.3 源码精读

并排对照两段「同一循环体」——纯 Muon 类（左）与组合类 muon 分支（右）：

```python
# Muon.step 循环体，L79-L94（节选）          # MuonWithAuxAdam.step muon 分支，L192-L208（节选）
for group in self.param_groups:               for group in self.param_groups:
    params = group["params"]                      if group["use_muon"]:
    params_pad = params + [torch.empty_like(          params = group["params"]
        params[-1])] * (W - len(params) % W)          params_pad = params + [...同左...]
    for base_i in range(len(params))[::W]:            for base_i in range(len(params))[::W]:
        if base_i + rank < len(params):                   if base_i + rank < len(params):
            p = params[base_i + rank]                         p = params[base_i + rank]
            if p.grad is None:                                if p.grad is None:
                p.grad = torch.zeros_like(p)                      p.grad = torch.zeros_like(p)
            state = self.state[p]                             state = self.state[p]
            if len(state) == 0:                               if len(state) == 0:
                state["momentum_buffer"] = ...                   state["momentum_buffer"] = ...
            update = muon_update(p.grad,                      update = muon_update(p.grad,
                state["momentum_buffer"],                         state["momentum_buffer"],
                beta=group["momentum"])                           beta=group["momentum"])
            p.mul_(1 - lr * wd)                              p.mul_(1 - lr * wd)
            p.add_(update.reshape(p.shape),                  p.add_(update.reshape(p.shape),
                   alpha=-lr)                                       alpha=-lr)
        dist.all_gather(...)                             dist.all_gather(...)
```

左侧引自 [muon.py:L79-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L79-L94)，右侧引自 [muon.py:L192-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L192-L208)——逐语句相同，差异只有缩进与 `if use_muon` 这层壳。这正是 u1-l3 总结的架构原则在起作用：**优化器类只做编排，算法全在可复用的函数里**，所以「搬进组合类」不需要改动任何计算逻辑。六处 step 分支共用同一副骨架的事实，u3-l1 已经用六对行号证实过。

一个值得注意的边界：`__init__`（L165-L182）里没有任何 `dist.*` 调用——分布式能力只在 step() 里用。这意味着**没有初始化进程组也能构造 `MuonWithAuxAdam`**（下面的实践正是利用这一点在 CPU 上观察排序行为），只是不能调 step()。

#### 4.3.4 代码实践

**实践目标**：在不初始化 `torch.distributed` 的 CPU 环境里，利用「构造函数无通信」这一点，直接观察分布式版 `__init__` 对 muon 组参数的排序行为（并与单机版对照）。

**操作步骤**（示例代码）：

```python
# 示例代码：观察 muon 组的排序（CPU 可运行，只构造、不 step）
import torch
from muon import MuonWithAuxAdam, SingleDeviceMuonWithAuxAdam

torch.manual_seed(0)
p_small = torch.nn.Parameter(torch.randn(16, 32))   # size (16,32)
p_mid   = torch.nn.Parameter(torch.randn(64, 64))   # size (64,64)
p_big   = torch.nn.Parameter(torch.randn(128, 64))  # size (128,64)

shapes = lambda opt: [tuple(p.shape) for p in opt.param_groups[0]["params"]]

# 分布式版：构造函数会排序（不需要 dist.init_process_group，因为 __init__ 无通信）
opt_dist = MuonWithAuxAdam([dict(params=[p_small, p_big, p_mid], use_muon=True)])
print("分布式版 muon 组顺序:", shapes(opt_dist))    # 期待按 size 降序

# 单机版：不排序，保持传入顺序
opt_sd = SingleDeviceMuonWithAuxAdam([dict(params=[p_small, p_big, p_mid], use_muon=True)])
print("单机版   muon 组顺序:", shapes(opt_sd))      # 期待保持 (16,32),(128,64),(64,64)
```

**需要观察的现象**：分布式版打印 `[(128, 64), (64, 64), (16, 32)]`——`torch.Size` 元组按字典序降序；单机版打印 `[(16, 32), (128, 64), (64, 64)]`——原顺序保留。同一份 params 列表，两个类的 `__init__` 行为差异就浓缩在这一行排序上。

**预期结果**：如上两组顺序。若尝试对 `opt_dist` 调用 `step()`，会在 `dist.get_world_size()` 处报「进程组未初始化」类错误——分布式能力确实只在 step() 里。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 muon 分支的参数要排序 + 补齐 + all_gather，而 adam 分支（下一节）什么都不做？

答案：muon 分支把**更新计算本身**按参数分摊到各 rank（每卡只算自己分到的参数），算完必须用 `all_gather` 把结果广播回去，且所有 rank 的通信序列要完全一致——排序与补齐为此服务。adam 分支没有分摊：每张卡都对全组参数独立做同样的确定性计算，无需通信（见下一节）。

**练习 2**：`update.reshape(p.shape)` 什么时候真正改变形状？什么时候是恒等？

答案：参数是四维卷积核时，`muon_update` 内部 `view(len(update), -1)` 把它压成 `(out_channels, in*k*k)` 的矩阵（[muon.py:L37-L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L38)），返回的 update 是二维，需要 reshape 回四维；普通二维权重矩阵时 update 形状本来就和 p 一致，reshape 是恒等。adam 分支没有这一步，因为 `adam_update` 全程保持参数原形状。

**练习 3**：组合类的 muon 分支与纯 `Muon` 类的 step 循环体是什么关系？这种「搬用」为什么不会引入 bug？

答案：逐语句相同（只差一层缩进和 `if use_muon` 判断），可对照 [muon.py:L79-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L79-L94) 与 [muon.py:L193-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L193-L208) 逐行核对。不引入 bug 的原因正是「函数做数学、类做编排」：分支体只编排 `muon_update` 与两行施加，算法语义全部封装在无状态纯函数里，搬动编排代码不触及计算。

### 4.4 step() 之二：use_muon=False 分支如何复用 adam_update

#### 4.4.1 概念说明

adam 分支（分布式版 [muon.py:L209-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L209-L223)，单机版 [muon.py:L270-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L270-L284)）是 u3-l1 精读过的「15 行内嵌 AdamW」的宿主。它做四件事：grad 填零、惰性初始化**三键状态**（`exp_avg`、`exp_avg_sq`、`step`）、步数自增、调 `adam_update` 后两行施加。

与 muon 分支最有趣的对比在分布式行为上：adam 分支**一次集合通信都没有**。这看起来危险——分布式训练里各卡参数不一致就全错了——但其实安全：数据并行训练中，梯度在 step() 之前已由框架（如 DDP 的 `all_reduce`）平均，各卡梯度一致；`adam_update` 是纯确定性函数，相同输入产出相同更新，于是各卡参数天然保持同步。代价是每张卡**冗余地**重复计算全组参数的更新（相比 muon 分支的分摊）。两种策略的取舍：

| 维度 | use_muon=True | use_muon=False |
|---|---|---|
| 更新函数 | `muon_update`（动量 + NS 正交化） | `adam_update`（AdamW 数学） |
| `state[p]` 的键 | `momentum_buffer`（1 个） | `exp_avg`、`exp_avg_sq`、`step`（3 个） |
| lr 语义 / 默认值 | 谱范数每步 / 0.02 | 逐坐标幅度 / 3e-4 |
| 分布式策略 | 参数分摊计算 + `all_gather` 广播 | 各 rank 独立全量计算（零通信） |
| `reshape` | 需要（卷积核场景） | 不需要（形状全程不变） |
| 结尾两行 | `p.mul_(1-lr·wd)`; `p.add_(update, alpha=-lr)` | 完全相同 |

还有一处与 muon 分支不同的细节：`state["step"] += 1` 写在 `len(state)==0` 初始化块**之外**（[muon.py:L219](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L219)、[muon.py:L280](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L280)），因此首个 `adam_update` 收到的步数是 1 而不是 0——u3-l1 的实践里已经验证过 `step=0` 会算出 `nan`（\( 1-\beta^0 = 0 \) 做分母），这行自增的位置正是避免它的关键。且计数器放在**逐参数**的 `self.state[p]` 里（而非参数组里），与 `torch.optim.AdamW` 的惯例一致：即便日后用 `add_param_group` 动态加参，每个参数的偏差修正也各自独立正确。

#### 4.4.2 核心流程

```text
for group in self.param_groups:
  use_muon=False?
    └─ for p in group["params"]:                 # 每 rank 完整遍历，零通信
         ├─ p.grad 为 None → 填零（强制同步）
         ├─ state = self.state[p]
         ├─ len(state)==0? → 初始化 {exp_avg: 0, exp_avg_sq: 0, step: 0}
         ├─ state["step"] += 1                   # 首步即为 1，避开 1-β^0=0 的除零
         ├─ update = adam_update(p.grad, exp_avg, exp_avg_sq, step, betas, eps)
         │            # 内部：lerp_ 更新两个动量 → 偏差修正 → buf1c/(buf2c.sqrt()+eps)
         └─ p.mul_(1 - lr·wd); p.add_(update, alpha=-lr)
```

update 的量纲：`adam_update` 输出的每个元素约在 ±1 量级（首步近似 `sign(grad)`，u3-l1 验证过），所以一步参数改动的逐坐标幅度 ≈ lr（默认 3e-4）；而 muon 分支的 update 是近似正交矩阵，RMS ≈ \( 1/\sqrt{n} \)（n 为列数，u2-l3）。两支共用「两行施加」，但 update 的「1」含义完全不同——这就是两组 lr 不能互抄的根源。

#### 4.4.3 源码精读

单机版 adam 分支全文（分布式版 [muon.py:L209-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L209-L223) 与之逐语句相同）：

```python
for p in group["params"]:                                    # L271
    if p.grad is None:                                       # L272
        p.grad = torch.zeros_like(p)  # Force synchronization  # L273-L274（L273 是注释行）
    state = self.state[p]                                    # L275
    if len(state) == 0:                                      # L276
        state["exp_avg"] = torch.zeros_like(p)               # L277  一阶动量
        state["exp_avg_sq"] = torch.zeros_like(p)            # L278  二阶动量
        state["step"] = 0                                    # L279  步数计数器
    state["step"] += 1                                       # L280  注意在初始化块之外
    update = adam_update(p.grad, state["exp_avg"], state["exp_avg_sq"],
                         state["step"], group["betas"], group["eps"])   # L281-L282
    p.mul_(1 - group["lr"] * group["weight_decay"])          # L283  解耦衰减
    p.add_(update, alpha=-group["lr"])                       # L284  施加更新
```

引自 [muon.py:L271-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L271-L284)。四个关键点：

1. **三键状态的形状**：`exp_avg`/`exp_avg_sq` 与参数同形（不是矩阵专属的二维），`step` 是 Python int。对照 muon 分支只有一个 `momentum_buffer`——state 的键集合就是「这个参数被哪条路径管」的指纹，本讲实践会直接打印它。
2. **超参从组里取**：`group["betas"]`、`group["eps"]`、`group["lr"]`、`group["weight_decay"]` 四个键正是构造函数断言保障的那 6 键中的 4 个（另两个是 `params` 与 `use_muon`）；键集合断言 = step() 里取键永不 `KeyError` 的前置证明。
3. **「grad 填零」同样适用于 adam 分支**：语义与 u2-l5 讲的一致——梯度缺失不等于冻结参数，衰减与动量更新照常进行（本例中填零梯度会让动量衰减、参数仍按 `p.mul_` 衰减）。
4. **两行施加与 muon 分支逐字相同**：再次印证六处骨架共用；衰减先于更新施加（AdamW 语义）。

#### 4.4.4 代码实践

**实践目标**：训练一个同时含 muon 参数与 adam 参数的极小模型 3 步，检查两类参数 `self.state[p]` 的键集合与 `step` 计数的值。

**操作步骤**（示例代码）：

```python
# 示例代码：对比两类参数的 state（CPU 可运行）
import torch
from muon import SingleDeviceMuonWithAuxAdam

torch.manual_seed(0)
W = torch.nn.Parameter(torch.randn(32, 32))    # → muon 组
b = torch.nn.Parameter(torch.randn(32))        # → adam 组（ndim<2）
opt = SingleDeviceMuonWithAuxAdam([
    dict(params=[W], lr=0.02, momentum=0.95, use_muon=True),
    dict(params=[b], lr=3e-4, betas=(0.9, 0.95), eps=1e-10, use_muon=False),
])

for t in range(1, 4):                          # 走 3 步
    W.grad = torch.ones_like(W)                # 手动喂固定梯度（演示 state 结构即可）
    b.grad = torch.ones_like(b)
    opt.step()

print("muon 参数 state 键:", list(opt.state[W].keys()))
print("adam 参数 state 键:", list(opt.state[b].keys()), " step =", opt.state[b]["step"])
```

（说明：示例刻意用手动赋 grad 的方式驱动 step()，避免搭建完整模型；梯度来源本身对 state 结构无影响。）

**需要观察的现象**：muon 参数的 state 键是 `['momentum_buffer']`；adam 参数的键是 `['exp_avg', 'exp_avg_sq', 'step']` 且 `step = 3`——每 step() 一次自增一次。

**预期结果**：如上；`opt.state` 的键就是两个参数对象本身（u2-l4 的「以参数对象为键」）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：adam 分支在分布式版里没有任何 `dist.*` 调用，为什么各卡的参数还能保持一致？

答案：数据并行下梯度在 step() 前已被平均（如 DDP 的 `all_reduce`），各卡梯度一致；`adam_update` 是确定性的纯函数，相同梯度与状态产出相同更新；初始参数相同 ⇒ 每步后参数仍相同。同步成本被转嫁到了反向传播的梯度聚合上，优化器阶段零通信。muon 分支不同——它把计算分摊到各卡，算出的结果只有部分 rank 有，必须 `all_gather` 广播。

**练习 2**：把 `state["step"] += 1` 挪进 `if len(state) == 0:` 块里会发生什么？

答案：step 永远停在 1，偏差修正恒为除以 \( 1-\beta^1 \)。由于 `lerp_` 更新的一阶动量每步都被 \( 1-\beta \) 缩小而修正系数不再随 t 增大，等效于更新幅度被系统性放大（后期约放大 \( 1/(1-\beta_2)\cdot(1-\beta_2^t) \) 倍量级），训练行为悄悄改变却不报任何错——这就是 u3-l1 用 `step=0 → nan` 实验强调「计数器位置很重要」的原因。

**练习 3**：一个参数的梯度某步为 `None`（比如它在当前 batch 的损失里没被用到），adam 分支会如何处理？这意味着什么？

答案：[muon.py:L272-L274](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L272-L274) 会给它填一个零张量，然后照常更新：零梯度让两个动量向零衰减、`step` 照常自增，权重衰减照常施加。语义是「梯度缺失 ≠ 冻结」：参数仍会随解耦衰减缩小。这与 `torch.optim.AdamW`（跳过该参数）不同，是为分布式各 rank 通信序列一致而设计的统一行为（u2-l5）。

### 4.5 SingleDeviceMuonWithAuxAdam 与参数划分惯用法

#### 4.5.1 概念说明

单机版 `SingleDeviceMuonWithAuxAdam`（[muon.py:L228-L287](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L287)）是分布式版的「减法」：`__init__` 去掉排序行（L232-L248 对比 L165-L182），step() 的 muon 分支去掉补齐与 `all_gather`（L259-L269 对比 L193-L208），其余逐行相同。它不 import 任何分布式依赖就能用——**本讲全部实践都在这个类上做**，CPU 即可跑。

本节另一半主题是**参数划分的惯用法**。docstring 的四行划分（[muon.py:L152-L155](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L152-L155)）浓缩成一张决策表：

| 参数类别 | 判断方法 | 去向 | 理由 |
|---|---|---|---|
| 隐藏矩阵权重 | `p.ndim >= 2` 且名字不含 embed/head | muon 组 | 正交化的适用对象（docstring L55-L58） |
| embedding | 名字含 `"embed"` | adam 组 | 二维但属查表参数，官方建议标准方法 |
| 增益/偏置（一维） | `p.ndim < 2` | adam 组 | 正交化需要矩阵（`assert G.ndim >= 2`，L15） |
| 输出头权重 | 显式按名字取（如 `model.lm_head.weight`） | adam 组 | 二维但直接决定 logits，官方建议标准方法 |

惯用法的三个要点：

1. **形状判断打底，名称判断修正**。`ndim` 是客观、无遗漏的过滤条件；`"embed" in n` 与显式取 head 是在形状之上叠加的语义修正——因为「二维」必要不充分。
2. **名称来自 `named_parameters()` 的模块路径**（如 `blocks.0.fc.weight`、`embed.weight`），它是唯一能区分「同是二维矩阵的不同角色」的信息源。
3. **划分必须不重不漏**。重复：PyTorch 父类构造函数会检查同一参数出现在多个组的情况并抛 `ValueError`（`add_param_group` 的查重逻辑）；遗漏：没有任何机制报警，该参数静默不训练——所以值得自己写一行自检（见实践）。

README 的两组配方（[README.md:L23-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L23-L31)）是同一惯用法的粗粒度版本：`model.body` 里按 ndim 分两半，head/embed 合并进 adam 组。它把「四类」折叠成「两组」，学习时先用它，精调时再展开成 docstring 的四组。

#### 4.5.2 核心流程

单机版 step 的双分支总览（全貌）：

```text
optimizer.step()
  └─ for group in self.param_groups:          # 组顺序 = 传入顺序，不影响结果
       ├─ group["use_muon"] 为 True:            # L259-L269（≈ SingleDeviceMuon 循环体）
       │    for p in group["params"]:
       │      grad 填零 → state{momentum_buffer} 惰性初始化
       │      → muon_update(...) → reshape → p.mul_/p.add_ 两行
       └─ group["use_muon"] 为 False:           # L270-L284（u3-l1 的 15 行 adam 分支）
            for p in group["params"]:
              grad 填零 → state{exp_avg, exp_avg_sq, step} 惰性初始化
              → step += 1 → adam_update(...) → p.mul_/p.add_ 两行
```

#### 4.5.3 源码精读

- [muon.py:L228-L231](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L231)：docstring 只有一句「Non-distributed variant of MuonWithAuxAdam」——单机版定位为分布式版的别名式变体，行为差异全部体现在代码里而非文档。
- [muon.py:L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248)：`__init__` 与分布式版逐行相同、仅缺排序行（对照 4.2.3 的逐行讲解）。断言、默认值注入、空 defaults 完全一致。
- [muon.py:L259-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L259-L269)：muon 分支 = `SingleDeviceMuon.step` 循环体（[muon.py:L115-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L115-L125)）加一层 `if use_muon`。
- [muon.py:L270-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L270-L284)：adam 分支，4.4.3 已逐行精读。

四个类的选型矩阵（u1-l3 的复习，加上本讲的实证）：

| 类 | 分布式 | muon 分支 | adam 分支 | 行数 |
|---|---|---|---|---|
| [Muon](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L96) | ✔ | ✔ | ✗ | 53 |
| [SingleDeviceMuon](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) | ✗ | ✔ | ✗ | 29 |
| [MuonWithAuxAdam](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L225) | ✔ | ✔ | ✔ | 88 |
| [SingleDeviceMuonWithAuxAdam](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L287) | ✗ | ✔ | ✔ | 60 |

#### 4.5.4 代码实践

**实践目标**：对一个小模型执行「四类划分」，并用 id 集合自检「不重不漏」——这是每次迁移新模型时都值得跑一遍的 30 秒检查。

**操作步骤**（示例代码）：

```python
# 示例代码：参数四分法 + 不重不漏自检（CPU 可运行）
import torch
from muon import SingleDeviceMuonWithAuxAdam

torch.manual_seed(0)
VOCAB, D = 64, 64

class ToyLM(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.embed = torch.nn.Embedding(VOCAB, D)                 # embedding
        self.blocks = torch.nn.ModuleList([                       # 3 个隐藏层
            torch.nn.ModuleDict(dict(fc=torch.nn.Linear(D, D),
                                     ln=torch.nn.LayerNorm(D)))
            for _ in range(3)])
        self.lm_head = torch.nn.Linear(D, VOCAB, bias=False)      # 输出头

model = ToyLM()

# —— docstring 四分法（muon.py L152-L155 的直译）——
hidden_matrix_params = [p for n, p in model.blocks.named_parameters()
                        if p.ndim >= 2 and "embed" not in n]
embed_params   = [p for n, p in model.named_parameters() if "embed" in n]
scalar_params  = [p for p in model.parameters() if p.ndim < 2]
head_params    = [model.lm_head.weight]

# —— 不重不漏自检 ——
groups = [hidden_matrix_params, embed_params, scalar_params, head_params]
ids = [id(p) for g in groups for p in g]
assert len(ids) == len(set(ids)), "有参数被重复分组"
assert set(ids) == {id(p) for p in model.parameters()}, "有参数被遗漏"
print("划分通过：3 隐藏矩阵 + 1 embed + 9 一维 + 1 head =",
      sum(len(g) for g in groups), "个参数")

optimizer = SingleDeviceMuonWithAuxAdam([
    dict(params=head_params,   lr=3e-3, betas=(0.9, 0.95), eps=1e-10, use_muon=False),
    dict(params=embed_params,  lr=3e-3, betas=(0.9, 0.95), eps=1e-10, use_muon=False),
    dict(params=scalar_params, lr=3e-3, betas=(0.9, 0.95), eps=1e-10, use_muon=False),
    dict(params=hidden_matrix_params, lr=0.05, momentum=0.95, use_muon=True),
])
print("param_groups 数量:", len(optimizer.param_groups))
```

**需要观察的现象**：自检打印 `划分通过：… = 14 个参数`（3 个 `fc.weight` + 1 个 `embed.weight` + 3×3 个一维参数 + 1 个 `lm_head.weight`）；优化器含 4 个组。若故意把 `head_params` 同时塞进两个组再构造，父类会抛 `ValueError: some parameters appear in more than one parameter group`。

**预期结果**：如上；把某个划分列表改成空列表（模拟遗漏）后第二个 assert 报「有参数被遗漏」。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`SingleDeviceMuonWithAuxAdam` 与 `MuonWithAuxAdam` 的 `__init__` 有且只有一行之差，是哪一行？为什么单机版不需要它？

答案：分布式版的 [muon.py:L169](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L169)（muon 组参数按 size 降序排序）。排序服务于 step() 里各 rank 通信序列的一致性（`all_gather` 窗口内张量要对得上）；单机版没有集合通信，参数按什么顺序更新结果都一样，所以省掉。

**练习 2**：如果一个隐藏层权重的名字里碰巧含 `"embed"`（例如某模型把块内投影叫 `proj_embed`），docstring 的 `hidden_matrix_params` 过滤会怎么处理？这说明名称过滤有什么风险？

答案：`"embed" not in n` 会把它排除出 muon 组；它若也不满足其他组的条件（ndim>=2 使它进不了 scalar_params，名字过滤使 embed_params 匹配到它——本例会进 embed_params），最终被分到 adam 组。名称过滤是子串匹配，对命名约定敏感：换个模型命名风格就可能漏配或错配。这正是划分后要跑「不重不漏」自检、并逐组打印参数名肉眼复核的原因。

**练习 3**：为什么「漏分一个参数」比「重复分组」更危险？

答案：重复分组由 PyTorch 父类构造函数查重并抛 `ValueError`，构造即失败、立即被发现；遗漏没有任何运行时报警——参数只是从不被更新（`p.grad` 一直是 None 也不影响训练继续跑），模型「能训练但少学了一块」，损失曲线看起来还正常。静默错误永远比崩溃错误更难排查。

## 5. 综合实践

**任务：把一个带 embedding、3 个隐藏线性层和输出头的 MLP 交给 `SingleDeviceMuonWithAuxAdam` 做单优化器混合训练，验证 loss 下降、两类 state 的差异，并触发一次严格断言。**

设计思路：模型结构对齐 docstring 示例的角色划分（embedding / 3×(Linear+LayerNorm) / lm_head）；数据用「每个 token 连续出现两次」的合成序列，让「预测下一个 token = 复制当前 token」成为可学习规律，loss 才会明显下降（纯随机数据只会停在 \(\ln(\text{VOCAB})\approx 4.16\)）。分组用 docstring 的四组式（adam 三组 + muon 一组），lr 取适合 toy 任务的量级（非 speedrun 值）。

```python
# 示例代码：综合实践——单优化器混合训练（CPU 可运行）
import torch
import torch.nn.functional as F
from muon import SingleDeviceMuonWithAuxAdam

torch.manual_seed(0)
VOCAB, D = 64, 64

class ToyLM(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.embed = torch.nn.Embedding(VOCAB, D)
        self.blocks = torch.nn.ModuleList([
            torch.nn.ModuleDict(dict(fc=torch.nn.Linear(D, D),
                                     ln=torch.nn.LayerNorm(D)))
            for _ in range(3)])
        self.lm_head = torch.nn.Linear(D, VOCAB, bias=False)

    def forward(self, x):
        h = self.embed(x)
        for blk in self.blocks:
            h = blk["ln"](F.relu(blk["fc"](h)))
        return self.lm_head(h)

model = ToyLM()

# 第 1 步：四分法划分 + 不重不漏自检（同 4.5.4）
hidden_matrix_params = [p for n, p in model.blocks.named_parameters()
                        if p.ndim >= 2 and "embed" not in n]
embed_params   = [p for n, p in model.named_parameters() if "embed" in n]
scalar_params  = [p for p in model.parameters() if p.ndim < 2]
head_params    = [model.lm_head.weight]
ids = [id(p) for g in (hidden_matrix_params, embed_params,
                       scalar_params, head_params) for p in g]
assert len(ids) == len(set(ids)) == len(list(model.parameters()))

# 第 2 步：组装四组 param_groups（docstring L158-L161 的同构写法）
adam_groups = [dict(params=head_params,   lr=3e-3),
               dict(params=embed_params,  lr=3e-3),
               dict(params=scalar_params, lr=3e-3)]
adam_groups = [dict(**g, betas=(0.9, 0.95), eps=1e-10, use_muon=False) for g in adam_groups]
muon_group  = dict(params=hidden_matrix_params, lr=0.05, momentum=0.95, use_muon=True)
optimizer   = SingleDeviceMuonWithAuxAdam([*adam_groups, muon_group])

# 第 3 步：合成数据（每个 token 连续出现两次）+ 训练
x = torch.randint(0, VOCAB, (256, 9))
data = torch.stack([x, x], dim=-1).reshape(256, 18)
inputs, targets = data[:, :-1], data[:, 1:]

for step in range(201):
    logits = model(inputs)
    loss = F.cross_entropy(logits.reshape(-1, VOCAB), targets.reshape(-1))
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    if step % 40 == 0:
        print(f"step {step:3d}  loss {loss.item():.4f}")

# 第 4 步：两类参数的 state 指纹
print("muon 参数 state 键:", list(optimizer.state[hidden_matrix_params[0]].keys()))
print("adam 参数 state 键:", list(optimizer.state[head_params[0]].keys()),
      " step =", optimizer.state[head_params[0]]["step"])

# 第 5 步：触发严格断言（spec 的第二个实验）
bad_group = dict(params=head_params, lr=3e-3, betas=(0.9, 0.95), eps=1e-10,
                 weight_decay=0, use_muon=False, scheduler="cosine")  # 多余键
try:
    SingleDeviceMuonWithAuxAdam([bad_group])
    print("未触发断言（不应发生）")
except AssertionError:
    print("AssertionError 已触发：参数组含多余键 scheduler，构造被拒绝")
```

**操作步骤**：

1. 按 u1-l2 安装本仓库（`pip install git+https://github.com/KellerJordan/Muon`），CPU 环境即可。
2. 运行脚本，记录 step 0/40/…/200 的 loss。
3. 检查第 4 步打印的两组 state 键。
4. 确认第 5 步抛出 `AssertionError`。
5. （选做）把 muon 组的 `lr` 换成 adam 组的 3e-3 再跑一遍，观察 loss 下降显著变慢——亲身体会「两种 lr 量纲不可互抄」。

**需要观察的现象与预期结果**：

1. loss 从约 \(\ln 64 \approx 4.16\) 起步，随训练明显下降（模型学到了「复制当前 token」的规律）。
2. muon 参数 state 键为 `['momentum_buffer']`，adam 参数为 `['exp_avg', 'exp_avg_sq', 'step']` 且 `step = 201`。
3. 第 5 步打印 `AssertionError 已触发…`，traceback 指向 [muon.py:L247](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L247)。
4. （选做）lr 互换后 Muon 更新幅度只有原来的约 1/17（0.003 vs 0.05），同步数下 loss 明显更高。

以上数值结论待本地验证；若 loss 不降，先检查数据是否真的构成了可学习规律（`data` 每 两个位置相同）以及两组 lr 是否传对位置。

## 6. 本讲小结

- `MuonWithAuxAdam` / `SingleDeviceMuonWithAuxAdam` 用一个自定义键 `use_muon` 把 Muon 与 AdamW 拼进**同一个优化器**：一次 step、一个调度器、一份 state_dict，解决双优化器编排痛点（docstring L145-L146 的官方动机）；划分必须由用户手动指定，因为「哪个二维矩阵算隐藏层」是模型语义，形状推断不出来。
- 构造函数对每组做四件事：断言 `use_muon` 必填 → （仅分布式 muon 组）按 size 降序排序 → 逐组注入默认超参（muon：lr 0.02/momentum 0.95/wd 0 共 5 键；adam：lr 3e-4/betas (0.9,0.95)/eps 1e-10/wd 0 共 6 键）→ **键集合恰好相等断言**；最后传**空 defaults** 给父类——因为「一张表补所有组」表达不了两类组的不同默认值。
- 严格断言 `set(group.keys()) == ...` 把四类错误（漏开关、跨组错传键、传不支持的开关、键名拼错）从「训练中静默失效」前移到「构造即失败」，是快失败设计的范本。
- step() 双分支：muon 分支（L193-L208 / L259-L269）逐语句搬自纯 Muon 类的 step 循环体，state 只有一个 `momentum_buffer`，分布式下按参数分摊计算并用 `all_gather` 广播更新后的参数；adam 分支（L209-L223 / L270-L284）是 u3-l1 那 15 行内嵌 AdamW，state 三键 `exp_avg`/`exp_avg_sq`/`step`，`step += 1` 在初始化块外保证首步为 1，分布式下零通信、靠「梯度一致 + 确定性计算」保持各卡同步。
- 单机版与分布式版的 `__init__` 仅差一行排序、step 的 muon 分支仅差补齐与 `all_gather` 三件套；本讲全部实践基于单机版与「构造函数无通信」这一事实，CPU 即可完成。
- 参数划分惯用法：`ndim >= 2` 打底、名称（`"embed" in n`、显式取 head）修正、`named_parameters()` 是名称来源；划分必须不重不漏——重复由父类 `ValueError` 兜底，遗漏完全静默，值得写 id 集合自检。

## 7. 下一步学习建议

下一讲 **u3-l3「分布式 Muon：Muon.step 的跨卡参数分片与 all_gather 同步」** 将攻克本讲只到结构层面的部分：`params_pad` 的补齐规则、`range(len(params))[::world_size]` 的窗口划分、rank 与参数的配对关系、为什么排序是通信形状一致的必要条件，以及用 `torchrun --nproc_per_node=2` + gloo 后端在 CPU 上做分布式验证实验。

继续阅读的建议：

1. 通读 [muon.py:L184-L225](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L184-L225)，把本讲 4.3 的结构图与真实代码逐行对上，特别注意 `all_gather` 的参数是 `params_pad` 的切片而不是 `params`。
2. 看 docstring 引用的官方实战示例（[muon.py:L150](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L150) 链接的 modded-nanogpt 记录），观察真实训练脚本里四组式划分与 lr 的取值。
3. 完成下一讲后进入 u3-l4 实战迁移：把一个现成的 AdamW 训练脚本按 README 配方（[README.md:L17-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L17-L33)）改造成 MuonWithAuxAdam 模式，并做 lr / weight_decay 的网格搜索。
