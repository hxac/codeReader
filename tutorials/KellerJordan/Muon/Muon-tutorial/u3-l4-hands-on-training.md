# 实战迁移：把一个 AdamW 训练脚本改造成 Muon 并调参

## 1. 本讲目标

学完本讲，你应该能够：

1. **独立完成一次完整迁移**：把一个已有的 `torch.optim.AdamW` 训练脚本，按照 README 的「替换配方」（[README.md:L17-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L17-L33)）改造成 `MuonWithAuxAdam`（单机版 `SingleDeviceMuonWithAuxAdam`）的单优化器模式，并说清迁移前后训练脚本的每一处差异。
2. **正确划分参数**：用 `ndim >= 2` 打底、名称/模块语义修正，把模型参数分成「隐藏二维权重（Muon）」与「其余（内部 AdamW）」两组，并写出「不重不漏」的自检断言。
3. **判断训练循环里哪些代码不用动**：`zero_grad` / `backward` / `step` / 梯度裁剪 / 调度器 / checkpoint 在迁移后各自的命运。
4. **按 README 的调参方法论实验**：momentum（0.95）、nesterov（True）、ns_steps（5）保持默认，只在 `{0.005, 0.02, 0.05, 0.1}` 中网格搜索 Muon 组的 lr（辅以 weight_decay），并用对照实验验证「只需调 lr 和 weight_decay」这一说法。
5. 积累一份可复用的实验资产：基线 AdamW 脚本 + 迁移脚本 + 网格搜索驱动，为 u4-l2 的多优化器基准对比打基础。

本讲是 u3 单元的收官实战讲：u3-l2 已经把 `MuonWithAuxAdam` 的**构造函数与 step 分支**逐行讲完，本讲不再重复类内部实现，而是站在**使用者视角**，回答「我的训练脚本要改哪几行、参数怎么分、超参怎么调」。

## 2. 前置知识

本讲建立在 u3-l2（MuonWithAuxAdam 精读）之上，同时用到 u2-l3、u2-l5、u3-l1 的结论。开始前请确认你还记得：

- **use_muon 参数组机制**（u3-l2）：一个优化器、多个参数组；`use_muon=True` 的组走 `muon_update` 路径，`use_muon=False` 的组走 `adam_update` 路径；构造函数做严格键集合校验，多余的键会在构造期被断言拒绝。
- **两组默认超参与量纲**（u3-l2）：muon 组默认 `lr=0.02`（单位是谱范数），adam 组默认 `lr=3e-4`（逐坐标幅度），两者相差约 67 倍，不可互抄；两组的 `weight_decay` 默认都是 0，README 配方里是显式传 0.01 的。
- **muon_update 五步流水线**（u2-l3）：EMA 动量 → Nesterov → 卷积核 `view` 展平 → Newton-Schulz 正交化 → 尺度缩放 `max(1, m/n)**0.5`；输出的 RMS 近似 \( 1/\sqrt{n} \)（\( n \) 为列数 / fan-in）。
- **两行原地更新**（u2-l5）：`p.mul_(1 - lr*wd)` + `p.add_(update, alpha=-lr)`，即 AdamW 式解耦权重衰减；权重 RMS 的平衡点约为 \( \sqrt{\text{lr}/(2\cdot\text{wd}\cdot n)} \)。
- **单机版 vs 分布式版**（u3-l3）：`MuonWithAuxAdam` 的 muon 分支调用 `dist.get_world_size()`，必须先初始化进程组（torchrun）；`SingleDeviceMuonWithAuxAdam` 完全不碰 `torch.distributed`，CPU 就能跑——**本讲所有实践都用单机版**。

还需要一点 PyTorch 训练脚本的常识：

- **标准训练循环骨架**：`optimizer.zero_grad()` → `loss = model(...)` → `loss.backward()` → `optimizer.step()`。这个骨架对所有 `torch.optim.Optimizer` 子类通用，Muon 系列也不例外——这正是迁移成本极低的根本原因。
- **梯度裁剪与调度器**：`torch.nn.utils.clip_grad_norm_` 只作用于 `.grad`，与优化器无关；`torch.optim.lr_scheduler` 系列直接读写 `param_groups` 里每组的 `lr` 键（按相同比例缩放各组），与本仓库的额外键 `use_muon` 不冲突。

## 3. 本讲源码地图

本讲涉及两个文件，重点是 README 的两段「使用说明」，以及 muon.py 中构造函数里与**调用方**相关的部分：

| 位置 | 内容 | 作用 |
|---|---|---|
| [README.md:L13-L16](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L13-L16) | Usage 总述 | 铁律：Muon 只管隐藏权重，其余用标准 AdamW |
| [README.md:L17-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L17-L33) | 替换配方代码块 | 本讲迁移流程的权威蓝本（注释掉的 AdamW 行 → 两组参数组） |
| [README.md:L35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L35-L36) | 适配说明 | `model.body/head/embed` 要换成你的模型结构；ConvNet 的划分建议 |
| [README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47) | Hyperparameter tuning | 调参方法论：只调 lr 与 weight_decay，lr 自带 muP 缩放 |
| [muon.py:L151-L163](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L151-L163) | docstring 内官方四组示例 | speedrun 风格的精调划分（对照 README 的两组简化版） |
| [muon.py:L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248) | `SingleDeviceMuonWithAuxAdam.__init__` | 调用方能传哪些键、缺省值是什么（u3-l2 精读过，本讲只取「允许键清单」） |
| [muon.py:L250-L286](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L250-L286) | `SingleDeviceMuonWithAuxAdam.step` | 迁移后 `optimizer.step()` 背后发生什么（本讲只看接口契约） |
| [muon.py:L55-L63](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L63) | `Muon` docstring 建议与参数说明 | 「哪些参数不该用 Muon」的原始出处；lr 的谱范数量纲 |

数学函数 `muon_update`（[muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41)）与 `adam_update`（[muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135)）已在前几讲精读，本讲直接当黑盒使用。

## 4. 核心概念与源码讲解

### 4.1 README 替换配方：一次「外科手术式」的迁移

#### 4.1.1 概念说明

README 的 Usage 一开头就把迁移场景钉死了（[README.md:L18-L20](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L18-L20)）：它假设你已经有一行

```python
optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4, betas=(0.90, 0.95), weight_decay=0.01)
```

然后给出「替换以上写法」的完整代码。这个叙事顺序透露了迁移的本质：**Muon 的引入不改变训练循环，只改变优化器的构造方式**——就像做一台外科手术，切口只有一处。

为什么切口在优化器构造？因为 u1-l1 就确立过：Muon 与 AdamW 的分工是按**参数类别**切的（隐藏二维权重 vs 其余），而 PyTorch 恰好提供了 `param_groups` 这个「按组配超参」的机制（u2-l4）。`use_muon` 只是把「按组配算法」也编码进了同一个机制（u3-l2）。

#### 4.1.2 核心流程

把 README 的配方抽象成五步迁移流程：

```text
第 1 步  抄下旧 AdamW 行的超参：lr=3e-4, betas=(0.90,0.95), weight_decay=0.01
            │  这些值将「原样继承」给迁移后的 adam 组
第 2 步  划分参数（4.2 详讲）
            ├─ hidden_weights        ：model.body 中 ndim>=2 的参数
            ├─ hidden_gains_biases   ：model.body 中 ndim<2 的参数
            └─ nonhidden_params      ：model.head + model.embed 的全部参数
第 3 步  组装两个参数组
            ├─ dict(params=hidden_weights, use_muon=True,  lr=0.02, weight_decay=0.01)
            └─ dict(params=其余,      use_muon=False, lr=3e-4, betas=(0.9,0.95), weight_decay=0.01)
第 4 步  换类名构造：MuonWithAuxAdam(param_groups)
            （CPU/单机用 SingleDeviceMuonWithAuxAdam）
第 5 步  训练循环一行不改：zero_grad → backward → step
            转入调参阶段：网格搜索 muon 组的 lr（4.4 详讲）
```

迁移前后对照（左侧引自 [README.md:L18](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L18)，右侧引自 [README.md:L22-L32](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L22-L32)）：

```text
迁移前（1 行）                            迁移后（约 8 行）
──────────────────                       ──────────────────────────────
optimizer = torch.optim.AdamW(           from muon import MuonWithAuxAdam
    model.parameters(),                  hidden_weights = [p for p in model.body.parameters() if p.ndim >= 2]
    lr=3e-4,                             hidden_gains_biases = [p for p in model.body.parameters() if p.ndim < 2]
    betas=(0.90, 0.95),                  nonhidden_params = [*model.head.parameters(), *model.embed.parameters()]
    weight_decay=0.01)                   param_groups = [
                                             dict(params=hidden_weights, use_muon=True,
                                                  lr=0.02, weight_decay=0.01),
                                             dict(params=hidden_gains_biases+nonhidden_params, use_muon=False,
                                                  lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
                                         ]
                                         optimizer = MuonWithAuxAdam(param_groups)
```

注意一个容易忽略的细节：**adam 组的超参与被替换的 AdamW 行完全一致**（lr=3e-4、betas=(0.9, 0.95)、wd=0.01）。迁移原则是「AdamW 侧原样继承，Muon 侧只新增」——这样迁移后 embedding/head/偏置的行为与迁移前可比，实验里观察到的差异才能归因给 Muon。

#### 4.1.3 源码精读

README 配方原文（[README.md:L22-L32](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L22-L32)）：

```python
from muon import MuonWithAuxAdam
hidden_weights = [p for p in model.body.parameters() if p.ndim >= 2]
hidden_gains_biases = [p for p in model.body.parameters() if p.ndim < 2]
nonhidden_params = [*model.head.parameters(), *model.embed.parameters()]
param_groups = [
    dict(params=hidden_weights, use_muon=True,
         lr=0.02, weight_decay=0.01),
    dict(params=hidden_gains_biases+nonhidden_params, use_muon=False,
         lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
]
optimizer = MuonWithAuxAdam(param_groups)
```

逐段说明：

- 第 2-4 行（[README.md:L23-L25](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L23-L25)）完成参数三分：隐藏二维权重、隐藏增益/偏置（一维）、非隐藏参数（输出头 + embedding，含其二维权重）。划分细节在 4.2 展开。
- 第 5-10 行（[README.md:L26-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L26-L31)）组装两组：muon 组只有 4 个键（`params/use_muon/lr/weight_decay`），adam 组 6 个键（多了 `betas`）——恰好各自等于构造函数允许的键集合减去可省略项（对照 [muon.py:L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L240) 与 [muon.py:L247](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L247) 的两份键集合断言）。
- 第 11 行（[README.md:L32](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L32)）构造优化器。README 用的是分布式版 `MuonWithAuxAdam`；**没有 torchrun/多卡环境时换成 `SingleDeviceMuonWithAuxAdam`，其余代码一字不改**（两个类的构造签名与键约定完全相同，差异仅在 step 内部有无集合通信，见 u3-l3）。
- 紧接着的适配说明（[README.md:L35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L35-L36)）：`model.body/head/embed` 只是占位符，要替换成你模型里对应语义的模块；ConvNet 的建议是「除第一个卷积层外，所有卷积核都用 Muon，其余全用 AdamW」。

#### 4.1.4 代码实践

**实践目标**：搭建迁移前的基线——一个用 AdamW 训练的玩具语言模型脚本，记录 loss 曲线，作为后续所有对照的参照系。

**操作步骤**：在自己的练习目录（不要动仓库源码）创建 `baseline_adamw.py`：

```python
# 示例代码：baseline_adamw.py —— 迁移前的 AdamW 基线脚本
import torch
import torch.nn as nn

torch.manual_seed(0)

V, D, T, H = 16, 32, 8, 128   # 词表大小、嵌入维、上下文长度、隐藏宽度

class ToyLM(nn.Module):
    def __init__(self):
        super().__init__()
        self.embed = nn.Embedding(V, D)              # (V, D)
        self.body = nn.Sequential(
            nn.Linear(T * D, H), nn.ReLU(),          # (H, T*D) + bias(H,)
            nn.Linear(H, H), nn.ReLU(),              # (H, H)  + bias(H,)
        )
        self.head = nn.Linear(H, V, bias=False)      # (V, H)

    def forward(self, x):                            # x: (B, T) 的 int64
        return self.head(self.body(self.embed(x).flatten(1)))

# 合成任务：目标由输入前三列经固定规则生成（可学习、可降到接近 0 loss）
data = torch.randint(0, V, (4096, T))
targets = (data[:, 0] + 2 * data[:, 1] + 3 * data[:, 2]) % V

model = ToyLM()
optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4,
                              betas=(0.90, 0.95), weight_decay=0.01)   # README L18 原样

for step in range(500):
    idx = torch.randint(0, len(data), (256,))
    loss = nn.functional.cross_entropy(model(data[idx]), targets[idx])
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    if step % 50 == 0 or step == 499:
        print(f"step {step:3d}  loss {loss.item():.4f}")
```

**需要观察的现象**：初始 loss 在 \( \ln 16 \approx 2.77 \) 附近（均匀猜测的交叉熵）；随训练逐步下降。把 500 步的 loss 记录下来（抄进一个表格或存成列表）。

**预期结果**：loss 从约 2.77 单调（带噪声）下降，500 步后显著低于初值。下降速度与最终值待本地验证——它就是后面 Muon 实验要击败的基线；若 500 步后 loss 仍高于 1.5，可把步数加倍再比。

#### 4.1.5 小练习与答案

**练习 1**：README 注释掉的旧 AdamW 行里有三个超参（lr、betas、weight_decay）。迁移后它们分别去了哪里？

**答案**：全部「原样继承」给 `use_muon=False` 的 adam 组：`lr=3e-4`、`betas=(0.9, 0.95)`、`weight_decay=0.01`（[README.md:L29-L30](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L29-L30)）。muon 组则拿一套新的 `lr=0.02`（谱范数量纲）与相同的 `weight_decay=0.01`。

**练习 2**：为什么迁移后不能继续用 `model.parameters()` 一把梭？

**答案**：因为 Muon 只适用于隐藏层二维权重，embedding、输出头、增益与偏置必须走标准 AdamW（[README.md:L13-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L13-L14)、[muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56)）；而优化器不自动判断参数归属（u3-l2：'The user must manually specify'），必须由用户分组传入。

**练习 3**：没有 GPU、不用 torchrun 时，README 配方最后一行应该怎么改？

**答案**：把 `MuonWithAuxAdam` 换成 `SingleDeviceMuonWithAuxAdam`（`from muon import SingleDeviceMuonWithAuxAdam`）。两个类构造签名与参数组约定完全一致；单机版不调用 `torch.distributed`（[muon.py:L228-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L248) 无任何 dist 调用），CPU 可直接运行。

### 4.2 参数划分：ndim 打底、语义修正

#### 4.2.1 概念说明

划分要解决的问题是：**哪些参数交给正交化更新，哪些交给逐坐标 AdamW**。判据有两层：

1. **形状层（ndim）**：正交化是对二维矩阵的操作，`zeropower_via_newtonschulz5` 开头就断言 `G.ndim >= 2`（[muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15)）。一维参数（bias、LayerNorm 增益）根本进不了 muon 路径。**ndim>=2 是进 muon 组的必要条件**。
2. **语义层（模块归属/名称）**：embedding 表和输出头权重通常也是二维，但 [muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56) 明确建议它们用标准方法优化。**ndim>=2 不充分**——这就是 README 配方按 `model.body` / `model.head` / `model.embed` 三个模块分别取参数、而不是全模型筛 `ndim>=2` 的原因。

注意一个 u1-l2 遗留问题的解答：当时用纯 `SingleDeviceMuon` 做实验必须 `bias=False`，因为一维参数会让断言失败；**改用 `MuonWithAuxAdam` 后这个约束消失了**——bias/增益放进 `use_muon=False` 的组即可，模型不必再迁就优化器。

#### 4.2.2 核心流程

划分算法（README 两组简化版与 docstring 四组精调版共用同一逻辑）：

```text
for (name, p) in model.named_parameters():
    if p.ndim < 2:                      → adam 组   （形状判据先行）
    elif p 属于 embedding 或输出头:      → adam 组   （语义判据修正）
    else:                               → muon 组   （隐藏二维权重，含卷积核）
最后自检：不重（组间无交集）＋ 不漏（并集 = model.parameters()）
```

README 两组版用「模块归属」实现语义判据（`model.body` 里筛 ndim，head/embed 整体进 adam）；docstring 四组版（[muon.py:L152-L155](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L152-L155)）用「名称包含 embed」+ 显式点名 `model.lm_head.weight` 实现，并把 adam 侧再细拆成 head/embed/scalar 三组以各配独立 lr。**迁移时从两组版起步**，效果调通后如有需要再细分。

#### 4.2.3 源码精读

README 的划分三行（[README.md:L23-L25](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L23-L25)）按模块取参、按 ndim 二分：

```python
hidden_weights = [p for p in model.body.parameters() if p.ndim >= 2]
hidden_gains_biases = [p for p in model.body.parameters() if p.ndim < 2]
nonhidden_params = [*model.head.parameters(), *model.embed.parameters()]
```

这三行覆盖了 body 的全部参数（按 ndim 互补）加上 head/embed 的全部参数（不筛形状，二维的 head.weight、embed.weight 也进 adam 组）。

docstring 四组版（[muon.py:L152-L155](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L152-L155)）是同一思想的名称版：

```python
hidden_matrix_params = [p for n, p in model.blocks.named_parameters() if p.ndim >= 2 and "embed" not in n]
embed_params = [p for n, p in model.named_parameters() if "embed" in n]
scalar_params = [p for p in model.parameters() if p.ndim < 2]
head_params = [model.lm_head.weight]
```

它多了两个工程细节：`"embed" not in n` 防止名字里带 embed 的二维参数混进 muon 组（例如 weight-tied 或模块命名特殊的情况）；`head_params` 只点名 `lm_head.weight`——输出头若带 bias，bias 会因 ndim<2 落入 `scalar_params`，两者最终都进 adam 侧，只是 lr 不同（[muon.py:L158](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L158)：head 0.22 / embed 0.6 / scalar 0.04）。

ConvNet 的划分建议见 [README.md:L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L36)：除第一个卷积层外的所有卷积核用 Muon。四维卷积核 `(out, in, kh, kw)` 由 `muon_update` 的 `view(len(update), -1)` 分支自动展平成 `(out, in*kh*kw)` 矩阵（[muon.py:L37-L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L38)，u2-l3 精读过），用户无需手动 reshape。

#### 4.2.4 代码实践

**实践目标**：给 4.1 的 `ToyLM` 写一个可复用的划分函数，打印每个参数的归属，并用断言验证「不重不漏」。

**操作步骤**：在练习目录创建 `partition.py`：

```python
# 示例代码：partition.py —— 参数划分与自检
import torch, torch.nn as nn

class ToyLM(nn.Module):   # 与 4.1 相同，此处省略重复定义（请从 baseline_adamw.py 导入或复制）
    ...

def partition(model):
    hidden_weights = [p for p in model.body.parameters() if p.ndim >= 2]
    hidden_gains_biases = [p for p in model.body.parameters() if p.ndim < 2]
    nonhidden_params = [*model.head.parameters(), *model.embed.parameters()]
    return hidden_weights, hidden_gains_biases, nonhidden_params

model = ToyLM()
muon_params, gains_biases, others = partition(model)

union = muon_params + gains_biases + others
assert len({id(p) for p in union}) == len(union)                       # 不重
assert {id(p) for p in union} == {id(p) for p in model.parameters()}   # 不漏

muon_ids = {id(p) for p in muon_params}
for n, p in model.named_parameters():
    print(f"{n:18s} {str(tuple(p.shape)):14s} ndim={p.ndim} -> {'muon' if id(p) in muon_ids else 'adam'}")
```

**需要观察的现象**：打印出的归属表里应出现两个「ndim=2 却在 adam 组」的参数。

**预期结果**（形状由构造决定，可直接推出；具体打印格式待本地验证）：

```text
embed.weight        (16, 32)       ndim=2 -> adam   ← 二维，但语义是 embedding
body.0.weight       (128, 256)     ndim=2 -> muon
body.0.bias         (128,)         ndim=1 -> adam
body.2.weight       (128, 128)     ndim=2 -> muon
body.2.bias         (128,)         ndim=1 -> adam
head.weight         (16, 128)      ndim=2 -> adam   ← 二维，但语义是输出头
```

两条断言都应静默通过；若把 `body.0.bias` 误放进 muon 组，第一次 `step()` 就会在 [muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15) 的断言处报错。

#### 4.2.5 小练习与答案

**练习 1**：`embed.weight` 与 `head.weight` 都是二维矩阵，为什么不放进 muon 组？

**答案**：Muon 的定位是「隐藏层权重专用」（[README.md:L13-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L13-L14)）；[muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56) 明确建议输入 embedding、最终输出层、内部增益/偏置用 AdamW 等标准方法。正交化的「奇异值拉平」对这两类参数的语义（查表/读出）并不合适——ndim>=2 只是必要条件。

**练习 2**：一个 ConvNet 有 8 个卷积层和 2 个全连接隐藏层，怎么划分？

**答案**：按 [README.md:L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L36)：第 2 到第 8 个卷积层的卷积核（四维）+ 2 个全连接隐藏层权重（二维）进 muon 组（四维核会被 [muon.py:L37-L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L38) 自动展平）；第一个卷积层、所有 bias、BN/LN 参数、（若有）分类头进 adam 组。

**练习 3**：如果模型存在权重共享（例如 `lm_head.weight is embed.weight`，两个名字指向同一个 Parameter），划分自检会发生什么？

**答案**：`model.parameters()` 天然去重（PyTorch 对重复参数只产出一次），而 `named_parameters()` 会产出两个名字。用名字筛参数可能把同一个参数加进两个组，触发 4.2.4 的「不重」断言。解决办法：划分前先按 `id` 去重，或按模块归属（README 的做法）而非全模型名称筛选。

### 4.3 构造优化器并接入训练循环：哪些代码不用动

#### 4.3.1 概念说明

u3-l2 精读构造函数时关注的是**内部校验逻辑**；本讲换个视角，关注**调用方契约**：我能传什么、step 背后发生什么、训练循环里还有什么要跟着改。

调用方契约就是三句话：

1. **传什么**：一个列表，每个 dict 必含 `params` 与 `use_muon`；muon 组另可省略 `lr`（默认 0.02）/`momentum`（默认 0.95）/`weight_decay`（默认 0），adam 组另可省略 `lr`（默认 3e-4）/`betas`（默认 (0.9, 0.95)）/`eps`（默认 1e-10）/`weight_decay`（默认 0）——见 [muon.py:L237-L246](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L237-L246)。**除此之外的任何键都会被键集合断言拒绝**。
2. **step 做什么**：对所有组按 `use_muon` 分流——muon 分支（[muon.py:L259-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L259-L269)）调 `muon_update` 并两行原地更新；adam 分支（[muon.py:L270-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L270-L284)）更新 `exp_avg/exp_avg_sq/step` 状态、调 `adam_update` 并两行原地更新。梯度缺失的参数自动填零梯度而非跳过（u2-l5 的「强制同步」设计）。
3. **为什么训练循环不用改**：单优化器正是这个类的设计目的——[muon.py:L145-L146](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L145-L146)：「allow the user to have a single optimizer in their code, rather than having both a Muon and an Adam which each need to be stepped」。一次 `step()`、一份 `state_dict()`，与它替换掉的 AdamW 在编排层面完全同形。

#### 4.3.2 核心流程

迁移检查清单——训练脚本的每个组件在迁移后的命运：

| 训练脚本组件 | 是否改动 | 说明 |
|---|---|---|
| `optimizer.zero_grad()` | **不改** | 与优化器类别无关 |
| `loss.backward()` | **不改** | 反向传播不感知优化器 |
| `optimizer.step()` | **不改** | 单优化器一次 step 覆盖全部参数（[muon.py:L145-L146](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L145-L146) 的设计目标） |
| `clip_grad_norm_(model.parameters(), ...)` | **不改** | 只作用于 `.grad`，在 `step()` 之前调用即可 |
| LR scheduler | **基本不改，须知一处** | 标准调度器把每组 `lr` 乘同一比例因子（warmup/cosine 这类按比例缩放的调度器对两组同时生效）；无法只调度一组，除非用按组索引的 lambda |
| checkpoint | **简化** | 从一份 AdamW `state_dict()` 变为一份混合优化器 `state_dict()`（含各组全部 state），仍是单份 |
| 优化器构造 | **唯一必改处** | 按 4.1 五步流程重写 |

#### 4.3.3 源码精读

先看调用方能填的全部键，即单机版构造函数的两个分支（[muon.py:L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248)）：

```python
for group in param_groups:
    assert "use_muon" in group
    if group["use_muon"]:
        # defaults
        group["lr"] = group.get("lr", 0.02)
        group["momentum"] = group.get("momentum", 0.95)
        group["weight_decay"] = group.get("weight_decay", 0)
        assert set(group.keys()) == set(["params", "lr", "momentum", "weight_decay", "use_muon"])
    else:
        # defaults
        group["lr"] = group.get("lr", 3e-4)
        group["betas"] = group.get("betas", (0.9, 0.95))
        group["eps"] = group.get("eps", 1e-10)
        group["weight_decay"] = group.get("weight_decay", 0)
        assert set(group.keys()) == set(["params", "lr", "betas", "eps", "weight_decay", "use_muon"])
```

（引自 [muon.py:L233-L247](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L233-L247)。）对调用方的直接含义：README 配方里两组显式传的键恰好等于「允许键 − 可省略键」；多传一个 `scheduler` 或 `nesterov` 都会在构造期立即断言报错（这是 u3-l2 讲过的「配置错误前移」设计）。

再看 step 里两分支收尾的两行原地更新——muon 分支（[muon.py:L267-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267-L269)）：

```python
update = muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])
p.mul_(1 - group["lr"] * group["weight_decay"])
p.add_(update.reshape(p.shape), alpha=-group["lr"])
```

adam 分支（[muon.py:L281-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L281-L284)）：

```python
update = adam_update(p.grad, state["exp_avg"], state["exp_avg_sq"],
                     state["step"], group["betas"], group["eps"])
p.mul_(1 - group["lr"] * group["weight_decay"])
p.add_(update, alpha=-group["lr"])
```

两个分支的收尾两行**逐字相同**（`p.mul_(1 - lr*wd)` + `p.add_(update, alpha=-lr)`）——这是解耦权重衰减的统一施加方式（u2-l5）。也就是说，迁移后所有参数都自动获得 AdamW 式解耦衰减，行为与被替换的 `torch.optim.AdamW(weight_decay=0.01)` 对齐；唯一的差别在 `update` 的来源与量纲。调用方由此得到一个干净 guarantee：**除了隐藏权重的更新规则变了，训练脚本里其他一切语义（衰减、调度、裁剪、存档）都保持不变**。

#### 4.3.4 代码实践

**实践目标**：完成迁移——把 4.1 基线脚本的优化器替换为 `SingleDeviceMuonWithAuxAdam`，其余代码不动，重跑同样 500 步并对比 loss 曲线。

**操作步骤**：复制 `baseline_adamw.py` 为 `migrated_muon.py`，只替换优化器构造段（示例代码）：

```python
# 示例代码：migrated_muon.py 相对 baseline 的唯一改动
from muon import SingleDeviceMuonWithAuxAdam   # 若未 pip 安装，可 sys.path.insert(0, "<仓库根目录>")

hidden_weights = [p for p in model.body.parameters() if p.ndim >= 2]
hidden_gains_biases = [p for p in model.body.parameters() if p.ndim < 2]
nonhidden_params = [*model.head.parameters(), *model.embed.parameters()]
param_groups = [
    dict(params=hidden_weights, use_muon=True,
         lr=0.02, weight_decay=0.01),
    dict(params=hidden_gains_biases + nonhidden_params, use_muon=False,
         lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
]
optimizer = SingleDeviceMuonWithAuxAdam(param_groups)
# 训练循环（zero_grad / backward / step）与 baseline 完全相同
```

**需要观察的现象**：(1) 脚本能否直接跑通（验证划分正确、键集合合法）；(2) 两条 loss 曲线在同一张图/表里的相对位置；(3) 用 `len(optimizer.state)` 在训练结束后查看——应等于全部参数个数（每个参数都有 state，muon 参数是 `momentum_buffer`，adam 参数是三键字典）。

**预期结果**：loss 同样从约 2.77 下降；在 lr=0.02 下，Muon 版前中期的下降速度通常快于 AdamW 基线（README 战绩与 u1-l1 的背景支持这一方向性预期），最终 loss 亦应更低。具体快多少、是否全程占优，**待本地验证**——这正是第 5 节综合实践要量化的事。

#### 4.3.5 小练习与答案

**练习 1**：迁移后想给 muon 组再加一个 `nesterov=False` 键，会发生什么？

**答案**：构造即失败。`assert set(group.keys()) == set([...])`（[muon.py:L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L240)）要求键集合**恰好相等**，多余键直接 AssertionError。且 `MuonWithAuxAdam` 系列根本没把 `nesterov`/`ns_steps` 作为可配置项暴露——它们固定为 `muon_update` 的默认值 True/5（u2-l5 结论），这是「默认即可」调参哲学在 API 层的体现（见 4.4）。

**练习 2**：为什么本讲实践用 `SingleDeviceMuonWithAuxAdam` 而不是 README 配方里的 `MuonWithAuxAdam`？

**答案**：`MuonWithAuxAdam.step` 的 muon 分支调用 `dist.get_world_size()` / `dist.all_gather`（[muon.py:L195-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L195-L208)），未初始化进程组会在 step 时抛异常；单机版把集合通信全部去掉（u3-l3），其余行为一致。要用分布式版验证，需 `torchrun --nproc_per_node=N` + `dist.init_process_group`（u3-l3 实践做法）。

**练习 3**：迁移前脚本里有 `torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=500)`，迁移后还能用吗？

**答案**：能。标准调度器为每个 param_group 维护 `initial_lr` 并把各组 `lr` 乘同一调度因子，与本优化器的额外键 `use_muon` 不冲突（额外键只是组字典里的普通条目，u2-l4）；效果是 muon 组与 adam 组的 lr 按同一比例退火——两组基数不同（0.02 vs 3e-4），比例缩放对两组各自合理。注意调度器写入的 `initial_lr` 等键发生在构造**之后**，不会再触发键集合断言（断言只在 `__init__` 里执行一次）。

### 4.4 调参方法论：只调 lr 与 weight_decay

#### 4.4.1 概念说明

README 有一节专门回答「迁移完怎么调参」（[README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47)）：

> Typically, the default values of momentum (0.95), nesterov (True), and ns_steps (5) work well. Only the learning rate and weight decay must be tuned.

这句话拆成三层理解：

1. **不用调的**：momentum=0.95、nesterov=True、ns_steps=5 是跨任务稳健的经验默认。API 设计与之呼应——构造函数只暴露 `momentum`（默认 0.95，[muon.py:L238](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L238)），`nesterov` 与 `ns_steps` 干脆没暴露，你想调也无从传起（想改只能复制 `muon_update` 自己维护变体，那是 u4-l3 的话题）。
2. **必须调的**：muon 组的 `lr` 与 `weight_decay`。lr 的量纲是「每次更新的谱范数」（[muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61)），默认 0.02、README 配方也用 0.02，speedrun 示例精调到 0.05（[muon.py:L160](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L160)）——所以 0.02～0.05 是合理搜索区间的中心，本讲网格取 {0.005, 0.02, 0.05, 0.1}。
3. **为什么 lr 不太需要随模型宽度重调**：README 说 lr 自带 muP 缩放（[README.md:L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L47)）。直觉来自 u2-l3 的尺度因子：正交化后更新的 RMS ≈ \( 1/\sqrt{n} \)（\( n \) 为 fan-in），与初始化给出的权重典型尺度 \( \sim 1/\sqrt{n} \) 同步缩放，于是固定 lr 对应的**相对步长**近似与宽度无关。

weight_decay 的直觉来自 u2-l5 的平衡点推导：解耦衰减下权重 RMS 稳定在 \( \sqrt{\text{lr}/(2\cdot\text{wd}\cdot n)} \)，调大 wd 就是把权重范数往下压。注意 wd 与 lr 是**耦合**的（平衡点里两者同现），所以严格说调参空间是 (lr, wd) 二维网格；本讲按 README 的精神先固定 wd=0.01 调 lr，再把最优 lr 下的 wd 换 {0, 0.1} 复核。

#### 4.4.2 核心流程

```text
调参流程（README 方法论的操作化）
──────────────────────────────
1. 固定：momentum=0.95（默认）、nesterov=True、ns_steps=5（未暴露，想改也改不了）
        adam 组超参 = 原 AdamW 脚本的值，不动
2. 固定 weight_decay=0.01，网格扫描 muon 组 lr ∈ {0.005, 0.02, 0.05, 0.1}
        指标：最终 loss ／ 到达目标 loss 所需步数
3. 取最优 lr，换 weight_decay ∈ {0, 0.1} 复核（验证 wd 敏感度）
4. 与 AdamW 基线对比，回答：Muon 值不值得换？
判读预期：lr 过小（0.005）→ 学得慢；过大（0.1）→ 震荡或不稳；中间值（0.02~0.05）最优
```

#### 4.4.3 源码精读

调参建议的「权威文本」是 [README.md:L46-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L46-L47)（L46 给出三个免调超参与「只调 lr 和 weight_decay」的结论；L47 给出 muP 缩放说明）。

三个免调默认值在源码中的落点：`momentum` 是构造函数可选项（[muon.py:L238](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L238)，默认 0.95），随后作为 `beta` 传给 `muon_update`（[muon.py:L267](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267)）；`nesterov` 与 `ns_steps` 则是 `muon_update` 的函数签名默认值（[muon.py:L34](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34)：`ns_steps=5, nesterov=True`），优化器类调用时不覆盖（[muon.py:L267](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267) 只传了 `grad/momentum_buffer/beta` 三个实参）——也就是说**step 路径上它们恒为 5 与 True**。

lr 量纲的原始定义在 Muon 类 docstring（[muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61)）："The learning rate, in units of spectral norm per update."，其作用机制是 [muon.py:L268-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L268-L269) 的 `p.add_(update.reshape(p.shape), alpha=-group["lr"])`：每步对参数的最大扰动约为 \( \text{lr}\cdot 1.5 \)（正交化后谱范数 ≤ 约 1.5，u2-l2）。两个搜索锚点：不传时的注入默认 0.02（[muon.py:L237](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L237)）与 docstring speedrun 示例的 0.05（[muon.py:L160](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L160)）。

weight_decay 的作用点是与 lr 相乘的那一行 `p.mul_(1 - group["lr"] * group["weight_decay"])`（[muon.py:L268](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L268)）；两组默认 wd 都是 0（[muon.py:L239](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L239)、[muon.py:L246](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L246)），README 配方显式给两组都传 0.01（[README.md:L28-L30](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L28-L30)）——迁移时容易漏掉 adam 组的 wd，漏掉后 embedding/head 将无衰减，与基线 AdamW（wd=0.01 作用于全模型）不可比。

#### 4.4.4 代码实践

**实践目标**：对 muon 组 lr 做四点网格搜索，找到本任务的最优 lr，并复核 wd 敏感度。

**操作步骤**：把训练逻辑抽成函数后循环扫描（示例代码，`ToyLM`/`partition`/数据同 4.1、4.2）：

```python
# 示例代码：grid_search.py —— lr 网格搜索（只动 muon 组的 lr）
import torch, torch.nn as nn
from muon import SingleDeviceMuonWithAuxAdam

def train_once(muon_lr, muon_wd=0.01, steps=500):
    torch.manual_seed(0)                     # 每次实验同种子、同初始化
    model = ToyLM()                          # 每次新建模型，避免状态泄漏
    hidden_w = [p for p in model.body.parameters() if p.ndim >= 2]
    rest = [p for p in model.body.parameters() if p.ndim < 2] \
           + [*model.head.parameters(), *model.embed.parameters()]
    optimizer = SingleDeviceMuonWithAuxAdam([
        dict(params=hidden_w, use_muon=True, lr=muon_lr, weight_decay=muon_wd),
        dict(params=rest, use_muon=False, lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
    ])
    losses = []
    for step in range(steps):
        idx = torch.randint(0, len(data), (256,))
        loss = nn.functional.cross_entropy(model(data[idx]), targets[idx])
        optimizer.zero_grad(); loss.backward(); optimizer.step()
        losses.append(loss.item())
    return losses

for lr in [0.005, 0.02, 0.05, 0.1]:
    losses = train_once(lr)
    print(f"muon_lr={lr:<6} final_loss={losses[-1]:.4f}  min_loss={min(losses):.4f}")

best = min([0.005, 0.02, 0.05, 0.1], key=lambda lr: min(train_once(lr)))  # 取最优后复核 wd
for wd in [0.0, 0.1]:
    losses = train_once(best, muon_wd=wd)
    print(f"best_lr={best} wd={wd:<4} final_loss={losses[-1]:.4f}")
```

**需要观察的现象**：四个 lr 的 loss 序列形态——下降快慢、是否震荡、是否中途发散（loss 回升到接近 \( \ln 16 \)）。

**预期结果**：方向性预期是 0.02 与 0.05 表现最好，0.005 明显偏慢，0.1 可能震荡或次优；最优值大概率落在 README 默认 0.02 附近（这正是「默认值可靠」说法的体现）。wd 复核预期 0 与 0.1 略差于 0.01，但差异应小于 lr 带来的差异。**全部待本地验证**——若 0.1 直接发散，说明本任务的最优 lr 区间偏小，把观察写进实验记录即可。

#### 4.4.5 小练习与答案

**练习 1**：README 说免调的三个超参中，哪几个能通过构造函数传参修改？哪几个不能？

**答案**：`momentum` 能（muon 组允许键之一，默认 0.95，[muon.py:L238](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L238)）；`nesterov` 与 `ns_steps` 不能——它们是 `muon_update` 的签名默认值（[muon.py:L34](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34)），优化器调用时不传也不允许用户经参数组传入（键集合断言会拒绝）。

**练习 2**：muon 组默认 lr 与 adam 组默认 lr 相差多少倍？为什么不能把 AdamW 调好的 3e-4 直接抄给 muon 组？

**答案**：0.02 / 3e-4 ≈ 67 倍。两者量纲不同：muon 组 lr 的单位是「每次更新的谱范数」（更新矩阵奇异值约 0.5～1.5，RMS ≈ \( 1/\sqrt{n} \)），adam 组 lr 的单位是逐坐标更新幅度（更新逐元素约 ±1）。3e-4 对谱范数量纲而言小了两个数量级，Muon 会学得极慢——u1-l3 就强调过两种 lr 不可互抄。

**练习 3**：README 说 Muon 的 lr 自带 muP 缩放，用 u2-l3 的结论给出直觉解释。

**答案**：`muon_update` 末行的尺度因子 `update *= max(1, m/n)**0.5`（[muon.py:L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40)）使更新的 RMS 自动等于 \( \sqrt{m/n} \)（fan-in \( n \) 不小于 fan-out \( m \) 时为 \( 1/\sqrt{n} \)）：宽度增大 → 更新自动变小，与初始化权重尺度 \( \sim 1/\sqrt{n} \) 的缩小同步。因此固定 lr 的**相对**步长近似与模型宽度无关，扩大模型规模时无需重调（[README.md:L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L47)）。

## 5. 综合实践

**任务**：把本讲四个模块的产出组装成一个完整的对照实验 `train_compare.py`，量化回答两个问题：(1) Muon 相对 AdamW 在本任务上快多少？(2) README「只需调 lr 和 weight_decay」的说法是否成立？

**操作步骤**（示例代码骨架，`ToyLM`、数据同 4.1）：

```python
# 示例代码：train_compare.py —— AdamW 基线 vs 迁移后的 Muon + lr 网格
import torch, torch.nn as nn
from muon import SingleDeviceMuonWithAuxAdam

STEPS, TARGET_LOSS = 500, 1.0

def make_data():
    data = torch.randint(0, V, (4096, T))
    return data, (data[:, 0] + 2 * data[:, 1] + 3 * data[:, 2]) % V

def run(name, build_optimizer, seed=0):
    torch.manual_seed(seed)
    model = ToyLM()
    optimizer = build_optimizer(model)
    hits = None
    for step in range(STEPS):
        idx = torch.randint(0, len(data), (256,))
        loss = nn.functional.cross_entropy(model(data[idx]), targets[idx])
        optimizer.zero_grad(); loss.backward(); optimizer.step()
        if hits is None and loss.item() <= TARGET_LOSS:
            hits = step
    return name, f"{loss.item():.4f}", hits if hits is not None else ">500"

data, targets = make_data()

def adamw(model):
    return torch.optim.AdamW(model.parameters(), lr=3e-4,
                             betas=(0.90, 0.95), weight_decay=0.01)

def muon_at(lr, wd=0.01):
    def build(model):
        hidden_w = [p for p in model.body.parameters() if p.ndim >= 2]
        rest = [p for p in model.body.parameters() if p.ndim < 2] \
               + [*model.head.parameters(), *model.embed.parameters()]
        return SingleDeviceMuonWithAuxAdam([
            dict(params=hidden_w, use_muon=True, lr=lr, weight_decay=wd),
            dict(params=rest, use_muon=False, lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
        ])
    return build

rows = [run("AdamW lr=3e-4", adamw)]
rows += [run(f"Muon lr={lr}", muon_at(lr)) for lr in [0.005, 0.02, 0.05, 0.1]]
print(f"{'配置':<16} {'最终loss':>10} {'到达loss≤1.0步数':>16}")
for r in rows:
    print(f"{r[0]:<16} {r[1]:>10} {r[2]:>16}")
```

**交付物**：

1. 一张结果表（最终 loss + 到达 loss ≤ 1.0 所需步数；若某配置 500 步未达标记 `>500`）；
2. 一条 loss 曲线图（六个配置各一条线，matplotlib 或手抄关键点进表格皆可）；
3. 一段约 200 字的结论，回答：最优 muon lr 是多少？最优 Muon 配置比 AdamW 快多少步/低多少 loss？三个免调超参（momentum/nesterov/ns_steps）全程未动仍能取得该结果，是否支持 README 的说法？若 0.1 发散或 0.005 反而最优，如实记录并给出你的解释。

**预期结果**（方向性，**待本地验证**）：最优 muon lr 落在 0.02～0.05；最优 Muon 配置到达目标 loss 的步数少于 AdamW；wd 的敏感度低于 lr。无论结果偏向哪边，实验本身都完成了对 README 调参方法论的一次本地检验——这正是把「读源码」变成「做研究」的一步。

## 6. 本讲小结

- **迁移是外科手术**：README 配方（[README.md:L17-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L17-L33)）只改优化器构造一处：旧 AdamW 的超参原样继承给 `use_muon=False` 组，隐藏二维权重单独成 `use_muon=True` 组（lr=0.02）；训练循环 `zero_grad/backward/step` 一行不改。
- **参数划分两层判据**：`ndim>=2` 是必要条件（形状），「隐藏层语义」是充分性修正（模块归属/名称）——embedding 与输出头虽是二维也走 AdamW（[muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56)）；划分后要用「不重不漏」断言自检。
- **调用方契约**：muon 组允许键 `params/lr/momentum/weight_decay/use_muon`，adam 组允许键 `params/lr/betas/eps/weight_decay/use_muon`，键集合必须恰好相等（[muon.py:L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L240)/[muon.py:L247](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L247)）；两分支收尾的两行原地更新逐字相同，衰减/调度/裁剪/存档语义全部与 AdamW 对齐。
- **调参方法论**：momentum=0.95、nesterov=True、ns_steps=5 免调（后两者 API 上根本未暴露），只调 muon 组 lr（谱范数量纲，锚点 0.02/0.05）与 weight_decay；尺度因子带来 muP 式宽度无关性。
- **两组 lr 不可互抄**：0.02（谱范数）vs 3e-4（逐坐标）相差约 67 倍；adam 组漏传 weight_decay 会让对照实验失去可比性。

## 7. 下一步学习建议

- **u4-l1（数值实验）**：本讲把 `ns_steps=5` 当免调黑盒，下一讲用数值实验打开它——迭代系数、奇异值分布与 bfloat16 精度，理解「默认值为什么可靠」的底层原因。
- **u4-l2（优化器横向对比）**：本讲的 `train_compare.py` 正是 u4-l2 微型基准的雏形——把它扩展成多种子、多优化器（AdamW/SingleDeviceMuonWithAuxAdam/SGD+momentum）对比，并阅读 README 链接的 124M 参数基准（[README.md:L49-L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L49-L51)）与 Kimi 技术报告，检验玩具结论在大规模下是否成立。
- **阅读真实迁移案例**：对照 [README.md:L38-L42](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L38-L42) 链接的 NanoGPT speedrun 与 CIFAR-10 airbench 脚本，观察真实项目里参数组怎么划、lr 怎么配——你会发现它们正是本讲四组式划分（[muon.py:L151-L163](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L151-L163)）的工业化版本。
- **分布式验证（可选）**：本讲全程单机版；若想验证 README 原配方的 `MuonWithAuxAdam`，按 u3-l3 的实践用 `torchrun --nproc_per_node=2` + gloo 后端在 CPU 上跑通参数分片与 all_gather。
