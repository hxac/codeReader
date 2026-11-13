# u2-l5 · SingleDeviceMuon.step 精读：最简形态的完整 Muon 优化器

## 1. 本讲目标

u2-l3 我们读完了 `muon_update`（数学流水线），u2-l4 补齐了 `torch.optim.Optimizer` 的框架约定。本讲把两者合起来，**逐行精读 muon.py 中最短的一个完整优化器类**：`SingleDeviceMuon`（共 29 行，[muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127)）。它是四个优化器类中唯一不含任何分布式代码的，因此是观察「Muon 优化器最小骨架」的最佳标本——u1-l3 说过这副骨架在文件里出现了六次，本讲就把其中最干净的一次彻底拆开。

读完本讲，你应该能够：

1. **逐行解释 `SingleDeviceMuon.step`**：closure 段、参数组遍历、`muon_update` 调用、`update.reshape(p.shape)`，以及两行原地参数更新。
2. **解释 `p.grad is None` 时填充零张量这一『强制同步』设计**：包括它的 git 历史（原来是 `continue`）、它在分布式版中避免的死锁，以及它在单机版中的副作用（梯度缺失 ≠ 冻结参数）。
3. **理解分布式 `Muon` 构造函数中「params 必须是显式列表」断言与「按 size 降序排序」的含义**，以及 `SingleDeviceMuon` 为什么可以两行都不写。
4. **写出解耦权重衰减的精确更新公式**，并解释为什么衰减必须放在正交化之外做。

## 2. 前置知识

承接前面讲义已建立的概念（一句话回顾）：

- **`muon_update` 的契约**（u2-l3）：`momentum.lerp_(grad, 1-beta)` 做 EMA 动量，Nesterov 两行写法原地改写 `grad`，卷积核 view 成矩阵，Newton-Schulz 正交化后乘尺度因子 \(\sqrt{\max(1, m/n)}\)；**传入的 `grad` 与 `momentum` 都会被原地改写**；最终更新的 RMS ≈ \(1/\sqrt{n}\)（\(n\) 为列数 fan-in），只由形状决定。
- **Newton-Schulz 正交化**（u2-l1、u2-l2）：输出 \(US'V^T\)，\(S'\) 对角元近似 Uniform(0.5, 1.5)，谱范数约 1；正交化具有**尺度不变性**——输入乘任何正常数，输出不变。
- **框架约定**（u2-l4）：`param_groups` 是超参作用域（可热更新），`self.state` 是以参数对象为键、惰性初始化的逐参数记忆，`step()` 必须 `@torch.no_grad()`，`closure` 在 `torch.enable_grad()` 中最先求值。
- **骨架结构**（u1-l3）：四个类的 `step()` 共享同一副骨架；`ns_steps` 与 `nesterov` 没有在构造函数中暴露，固定为 5 和 True。

本讲新增的概念：

| 概念 | 通俗解释 |
|---|---|
| `p.grad is None` | 反向传播没有为该参数产生梯度（参数没参与本步 loss，或调用了 `zero_grad(set_to_none=True)` 后没 backward）。PyTorch 官方优化器的惯例是 `continue` 跳过。 |
| 强制同步（Force synchronization） | 本仓库的选择：梯度缺失时把 `p.grad` 填成零张量，而不是跳过。在分布式版中这是为了避免各 rank 执行的集合通信序列不一致。 |
| 惰性初始化 | 不在构造函数里分配状态（动量缓冲），而是在参数**第一次被 step 到**时才分配。`torch.optim.SGD` 的 `momentum_buffer` 也是这样做的。 |
| 解耦权重衰减 | \(p \leftarrow p(1-\mathrm{lr}\cdot\mathrm{wd}) - \mathrm{lr}\cdot u\)：衰减是独立的乘法收缩，不进梯度。与 `torch.optim.SGD(weight_decay=...)` 的耦合式（把 \(\mathrm{wd}\cdot p\) 加进梯度）相对。 |
| `world_size` / `rank` | 分布式训练的进程总数与本进程编号（u3-l3 详讲）。本讲只需知道：分布式 `Muon` 的构造函数断言与排序**完全是为它服务的**。 |

一个值得先想通的问题：**为什么拿 `SingleDeviceMuon` 当精读标本，而不是功能最全的 `MuonWithAuxAdam`？** 因为它是「纯 Muon + 单进程」的交集：没有 `use_muon` 分支（对比 `SingleDeviceMuonWithAuxAdam`），没有 `dist.*` 通信（对比 `Muon`），剩下的每一行要么是框架骨架，要么是 Muon 算法本身。读懂这 29 行，后面三个类只是往这副骨架上「加东西」。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会对照引用分布式版 `Muon` 的对应行：

| 位置 | 角色 |
|---|---|
| [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) | `SingleDeviceMuon` 全类：本讲主角 |
| [muon.py:L100-L102](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L100-L102) | docstring：一句话说明这是非分布式变体 |
| [muon.py:L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105) | `__init__`：3 行最简构造（4.1 节） |
| [muon.py:L107-L113](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L107-L113) | `@torch.no_grad()` + closure 约定（4.2 节） |
| [muon.py:L115-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L115-L125) | step 主循环 11 行：本讲核心（4.2–4.4 节逐行拆解） |
| [muon.py:L117-L119](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L117-L119) | `p.grad is None → torch.zeros_like(p)`：强制同步（4.3 节） |
| [muon.py:L120-L122](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L120-L122) | `self.state[p]` + `momentum_buffer` 惰性初始化（4.3 节） |
| [muon.py:L124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125) | 两行原地更新：解耦权重衰减 + 施加更新（4.4 节） |
| [muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69) | 分布式 `Muon.__init__`：断言（L67）+ 按尺寸排序（L68），4.1 节对照 |
| [muon.py:L79-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L79-L94) | 分布式 `Muon.step` 分片循环：4.1/4.3 节引用，u3-l3 详讲 |
| [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) | `muon_update`：step 主循环调用的数学流水线（u2-l3 已精读） |
| [README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47) | 调参建议：momentum/nesterov/ns_steps 用默认值，只需调 lr 与 weight_decay |

## 4. 核心概念与源码讲解

### 4.1 两个构造函数的对照：删掉的两行就是分布式的全部代价

#### 4.1.1 概念说明

`SingleDeviceMuon` 的构造函数只有两行有效代码：组装 `defaults`、调用父类。而分布式 `Muon` 在同样两行之外多了**恰好两行**——一条断言和一次排序。这「删掉的两行」不是随意的简化，而是分布式 `step()` 对参数列表的两个硬性要求：

1. **必须是显式的 `Parameter` 列表**：分布式 `step()` 要用 `params[base_i + rank]` 按下标取参数（[muon.py:L84](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L84)），还要用 `params[-1]` 做补齐模板（[muon.py:L81](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81)）——生成器和参数组字典都经不起这样的用法，所以入口处直接收紧。
2. **必须按 `size()` 降序排序**：分布式版把一批参数轮流分摊到各个 GPU，每轮用 `dist.all_gather` 汇总（[muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94)）。`all_gather` 要求所有 rank 提交**形状一致**的张量；排序让同形状参数相邻成段（例如 8 层 transformer 的同型权重矩阵排成连续 8 个），与 `world_size` 的窗口对齐。排序后 `params[-1]` 还恰好是**最小**的参数，用它做补齐空张量的模板开销最小。完整机制在 u3-l3 详讲。

单机版按顺序遍历参数、没有通信、没有按下标取参，这两行自然一行都不需要。

#### 4.1.2 核心流程

```
分布式 Muon.__init__                    SingleDeviceMuon.__init__
        │                                       │
        ▼                                       ▼
defaults = dict(lr, weight_decay, momentum)   defaults = dict(lr, weight_decay, momentum)   ← 完全相同
        │                                       │
        ▼                                       ▼
assert isinstance(params, list)               （无断言：生成器、参数组字典都能交给父类处理）
  and len(params) >= 1
  and isinstance(params[0], Parameter)
        │                                       │
        ▼                                       ▼
params = sorted(params, key=size, 降序)        （无排序：遍历顺序不影响单机正确性）
        │                                       │
        └───────────────┬───────────────────────┘
                        ▼
              super().__init__(params, defaults)
              （父类规范化成 param_groups、补全组内缺失键）
```

#### 4.1.3 源码精读

分布式版构造函数（注意多出来的两行）：

```python
def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
    defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
    assert isinstance(params, list) and len(params) >= 1 and isinstance(params[0], torch.nn.Parameter)
    params = sorted(params, key=lambda x: x.size(), reverse=True)
    super().__init__(params, defaults)
```

出处：[muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69)。第 67 行的断言要求：`params` 是非空 `list` 且首元素是 `torch.nn.Parameter`——这会拒绝三种常见误用：`model.parameters()`（返回生成器，`isinstance(..., list)` 为 False）、参数组字典列表（首元素是 `dict`，没有 `.size()` 方法，若不在这里拦下，排序那行的 `lambda` 会抛出难以理解的 `AttributeError`）、空列表（若绕过断言，[muon.py:L81](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81) 的 `params[-1]` 会直接 `IndexError`）。第 68 行按 `torch.Size` 元组做字典序降序排序，同形状参数聚成连续段。

单机版构造函数：

```python
def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
    defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
    super().__init__(params, defaults)
```

出处：[muon.py:L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105)。与上一段逐字相同的是第 104 行 `defaults` 组装；不同的恰恰是少了断言与排序。两个默认超参值得留意：`lr=0.02`（谱范数单位的学习率，比 AdamW 常用的 3e-4 大近两个数量级，u1-l3 已强调不可混用）与 `weight_decay=0`（默认不衰减；README 的用法示例里手动设为 0.01，见 [README.md:L26-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L26-L31)）。另外，由于参数被原样交给父类，单机版天然支持「参数组字典列表」的传法，可以给不同组配不同 lr——分布式版构造函数则收紧为扁平列表。

#### 4.1.4 代码实践

**实践目标**：亲眼确认两个构造函数对同一份参数的不同态度。

1. 操作步骤（示例代码）：

```python
import torch
from muon import Muon, SingleDeviceMuon

w1 = torch.nn.Parameter(torch.randn(64, 32))
w2 = torch.nn.Parameter(torch.randn(32, 16))

# (1) 生成器：分布式 Muon 直接拒绝
try:
    Muon(p for p in [w1, w2])
    print("(1) 意外通过")
except AssertionError:
    print("(1) Muon 拒绝生成器：AssertionError")

# (2) 显式列表：通过，且内部被重新排序
opt = Muon([w2, w1])
print("(2) 排序后顺序:", [tuple(p.shape) for p in opt.param_groups[0]["params"]])

# (3) 单机版：生成器也宽容（父类会规范化）
opt2 = SingleDeviceMuon(p for p in [w1, w2])
print("(3) SingleDeviceMuon param_groups 数:", len(opt2.param_groups))
```

2. 需要观察的现象：分支 (1) 抛 `AssertionError`；分支 (2) 打印的形状顺序是 `[(64, 32), (32, 16)]` 而不是传入的 `[(32, 16), (64, 32)]`——排序在构造时就完成了；分支 (3) 正常构造（PyTorch 父类接受任意可迭代参数）。
3. 预期结果：三个分支都按上述描述输出（基于源码可确定性推出；具体打印文本待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Muon(model.body.parameters())` 会立刻失败，而 `Muon(list(model.body.parameters()))` 可以？
答：`model.parameters()` 返回生成器，`isinstance(params, list)` 为 False，第 67 行断言失败；包一层 `list()` 后满足断言。断言的价值是把错误拦在构造期，给出可定位的 `AssertionError`，而不是让排序或 `params[-1]` 在训练时抛出难懂的错误。

**练习 2**：把排序行删掉，分布式 `Muon` 最可能在哪里出问题？
答：`dist.all_gather`（[muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94)）。不排序时同形状参数不再相邻，同一轮窗口内各 rank 持有的参数形状不一致，而 `all_gather` 要求各 rank 提交形状一致的张量（u3-l3 会用实验演示）。

**练习 3**：`SingleDeviceMuon` 删掉排序后，参数处理顺序会影响训练结果吗？
答：不会。单机 `step()` 只按列表顺序逐个独立更新参数，参数之间没有交互，顺序仅影响浮点累加之外的一切——这里连累加都没有，每个参数的更新只依赖自己的梯度与动量。

### 4.2 SingleDeviceMuon.step 主循环：11 行骨架逐行精读

#### 4.2.1 概念说明

`step()` 是优化器唯一真正「干活」的方法：读梯度 → 算更新 → 改参数。u2-l4 讲过，其中的 `@torch.no_grad()`、closure 段是所有自定义优化器共有的框架代码；本讲把框架部分快速带过（标注「u2-l4 已讲」），把注意力集中在 Muon 特有的调用序列上。这一节先给出全景流程，后面两节（4.3、4.4）再深挖其中两个专题。

#### 4.2.2 核心流程

```
step(closure=None)
 ├─ @torch.no_grad()                       ← u2-l4：原地更新叶子张量必须关梯度
 ├─ closure 存在？→ torch.enable_grad() 中求值 loss（先于一切参数更新）
 ├─ for group in self.param_groups         ← 超参（lr/wd/momentum）从组字典里现取
 │   └─ for p in group["params"]
 │       ├─ p.grad is None → p.grad = zeros_like(p)      …… 4.3 节
 │       ├─ state = self.state[p]；首次访问 → 建 momentum_buffer …… 4.3 节
 │       ├─ update = muon_update(p.grad, momentum, beta=group["momentum"])
 │       │    （EMA 动量 → Nesterov → [卷积 view] → NS 正交化 → 尺度缩放）
 │       ├─ p.mul_(1 - lr·wd)                             …… 4.4 节
 │       └─ p.add_(update.reshape(p.shape), alpha=-lr)    …… 4.4 节
 └─ return loss
```

一次 `step()` 对每个参数恰好做一次「查状态 → 算更新 → 两行原地写回」，参数之间完全独立。

#### 4.2.3 源码精读

step 的开头（框架部分，四个类各出现一次）：

```python
@torch.no_grad()
def step(self, closure=None):

    loss = None
    if closure is not None:
        with torch.enable_grad():
            loss = closure()
```

出处：[muon.py:L107-L113](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L107-L113)。u2-l4 已讲：装饰器保证 `p.mul_`/`p.add_` 不被 autograd 追踪；closure 若提供，则在 `enable_grad` 中**先**求值——保证更新用的是最新梯度。实际训练脚本几乎从不给 Muon 传 closure，`loss` 保持 `None` 返回。

主循环（本讲核心 11 行）：

```python
for group in self.param_groups:
    for p in group["params"]:
        if p.grad is None:
            # continue
            p.grad = torch.zeros_like(p)  # Force synchronization
        state = self.state[p]
        if len(state) == 0:
            state["momentum_buffer"] = torch.zeros_like(p)
        update = muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])
        p.mul_(1 - group["lr"] * group["weight_decay"])
        p.add_(update.reshape(p.shape), alpha=-group["lr"])
```

出处：[muon.py:L115-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L115-L125)。逐行说明：

- **L115-L116**：先遍历参数组、再遍历组内参数。所有超参都用 `group["..."]` 现取——这就是 u2-l4 说的「热更新」生效点：训练中直接改 `optimizer.param_groups[0]["lr"] = 0.01`，下一步立即生效。
- **L117-L119**：梯度缺失的处理，4.3 节专题。
- **L120-L122**：逐参数状态与惰性初始化，4.3 节专题。
- **L123**：调用 u2-l3 精读过的 `muon_update`。注意只传了 `beta=group["momentum"]`——`ns_steps`（默认 5）与 `nesterov`（默认 True）在这条调用链上**没有暴露**，想要改只能改 [muon.py:L34](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34) 的函数签名（u4-l3 的二次开发主题）。还要记住 u2-l3 的警告：`p.grad` 会在 `muon_update` 内部被 Nesterov 的 `grad.lerp_(momentum, beta)` **原地改写**，step 结束后 `p.grad` 不再是反向传播得到的原始梯度。
- **L124-L125**：两行原地更新，4.4 节专题。这里只解释 `update.reshape(p.shape)`：对二维参数它是恒等操作；对卷积参数（`ndim == 4`），`muon_update` 内部把更新 view 成了 `(out_channels, -1)` 的矩阵（[muon.py:L37-L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L38)），`reshape` 负责把形状还原成 `(out_channels, in_channels, kh, kw)` 才能 `add_` 到参数上。git 历史里这一步曾被遗漏，由提交 `c33a468`（"Added missing reshape for muon update"）补上——统一写 `reshape` 让二维与四维参数共用同一条更新路径。

最后 [muon.py:L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L127) `return loss`，按约定返回闭包求得的损失（无闭包则为 `None`）。

#### 4.2.4 代码实践

**实践目标**：验证「`p.grad` 在 step 后被原地改写」这一容易踩坑的细节，并算出精确的改写系数。

1. 操作步骤（示例代码）：

```python
import torch
from muon import SingleDeviceMuon

torch.manual_seed(0)
w = torch.nn.Parameter(torch.randn(128, 64))
opt = SingleDeviceMuon([w])
loss = (w * torch.arange(128 * 64, dtype=torch.float32).view(128, 64)).sum()
loss.backward()
raw_grad = w.grad.clone()                # 保存原始梯度
opt.step()
print("p.grad / raw_grad 的比值（期望恒定）:", (w.grad / raw_grad).mean().item())
```

2. 需要观察的现象：比值是一个恒定常数（逐元素相除全部相等，`mean` 无 NaN）。
3. 预期结果：首次 step 时动量从零开始：\(m_1 = (1-\beta)g = 0.05g\)，Nesterov 改写 \(g \leftarrow (1-\beta)g + \beta m_1 = 0.0975\,g\)。所以 `p.grad / raw_grad ≈ 0.0975`（β=0.95；float 舍入使末位略有偏差，待本地验证）。这个 0.0975 可以从 [muon.py:L35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L35-L36) 的两行 `lerp_` 直接推导出来。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `update.reshape(p.shape)` 里的 `reshape` 去掉，什么模型会立刻报错？
答：含卷积层（`p.ndim == 4`）的模型。`muon_update` 返回的 `update` 是二维矩阵，`p.add_(update)` 因形状不匹配抛错；纯线性层模型不受影响（reshape 恒等）。

**练习 2**：为什么 `group["momentum"]` 每一步都从字典里重新读取，而不是在 `__init__` 里存成 `self.momentum`？
答：因为超参的作用域是参数组（u2-l4）。`param_groups` 是 PyTorch 生态的公共接口——学习率调度器、`state_dict`、训练中手动改超参都操作这个字典；存成实例属性会脱离这套机制，热更新就会失效。

**练习 3**：`step()` 里参数更新的先后顺序重要吗——能否先 `add_` 再 `mul_`？
答：重要。先 `mul_` 后 `add_` 意味着本步的新更新**不**参与本步衰减；反过来会让刚加上的更新立即被收缩 \((1-\mathrm{lr}\cdot\mathrm{wd})\) 倍，等价于把有效学习率降低。本仓库的顺序与 `torch.optim.AdamW` 一致（见 4.4 节）。

### 4.3 状态生命周期：p.grad 填零与 momentum_buffer 惰性初始化

#### 4.3.1 概念说明

这一节回答两个「第一次」的问题：**第一次 step 时状态从哪来**（惰性初始化），以及**没有梯度时参数会发生什么**（强制同步）。

**惰性初始化**：构造函数只建了空的 `self.state`（u2-l4），`momentum_buffer` 要等到参数第一次被 step 到才分配。好处有二：只为真正参与训练的参数付出显存；与 `torch.optim.SGD` 的官方实现惯例一致（SGD 的 `momentum_buffer` 同样在 step 里懒创建）。这也意味着 `state_dict()` 保存 checkpoint 时会把动量缓冲一并保存，恢复训练时动量不丢。

**强制同步**：`p.grad is None` 时，PyTorch 官方优化器的惯例是 `continue`（跳过该参数本步更新）。本仓库却在两处留下了化石：被注释掉的 `# continue` 和一行填零。git 历史还原了演变过程——提交 `3b04428`（"skip params when grad is None"）最初加的就是 `continue`；随后提交 `5bc6993`（"Modify the code to ensure synchronization even when p.grad is None"）把它改成填零，理由写在行内注释 `# Force synchronization`。真正的动机在**分布式**版本里：`Muon.step` 的 `continue` 会跳过本轮循环体末尾的 `dist.all_gather`（[muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94) 位于 `if` 块之外、`for` 块之内）——只要有一个 rank 的参数缺梯度而别的 rank 不缺，有的进程进集合通信、有的跳过，**全体挂死**。填零保证每个 rank 每一步执行完全相同的通信序列。

单机版没有集合通信，为什么也保留填零？一是让六处 step 骨架保持一致（u1-l3 的观察）；二是语义上把「梯度缺失」定义为「梯度为零」而非「冻结参数」：衰减照常施加、动量照常衰减（\(m \leftarrow \beta m\)）、动量未耗尽时仍有正交化更新（见练习 3）。

#### 4.3.2 核心流程

单个参数 `p` 的状态生命周期：

```
构造期：self.state = defaultdict(dict)                 ← 全局空账本，p 尚无条目
   │
第 1 次 step(p)：state = self.state[p]                 ← 首次访问，自动得到空 dict
   │              len(state) == 0 成立
   │              state["momentum_buffer"] = zeros_like(p)   ← 此刻才分配显存
   │
第 2+ 次 step(p)：len(state) == 1 → 直接复用缓冲区
   │
（若本步 p.grad is None）
   │              p.grad = zeros_like(p)               ← 等价于「梯度为零」：
   │              momentum *= β（动量指数衰减）
   │              nesterov 更新 = β·momentum（动量未耗尽时非零）
   │              weight decay 照常施加
   ▼
```

#### 4.3.3 源码精读

梯度缺失的处理（注释掉的 `# continue` 是历史化石）：

```python
if p.grad is None:
    # continue
    p.grad = torch.zeros_like(p)  # Force synchronization
```

出处：[muon.py:L117-L119](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L117-L119)。填的是与参数同形状、同设备、同 dtype 的零张量；从此 `p.grad` 一直是一个合法张量。注意它**不会**在 step 结束后被清掉——真正的清理发生在用户调用 `optimizer.zero_grad()` 时（新版 PyTorch 默认 `set_to_none=True`，把 `.grad` 重置为 `None`，于是下个 step 又可能走到这个分支）。

状态的惰性初始化：

```python
state = self.state[p]
if len(state) == 0:
    state["momentum_buffer"] = torch.zeros_like(p)
```

出处：[muon.py:L120-L122](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L120-L122)。`self.state` 是 `defaultdict(dict)`（u2-l4），`self.state[p]` 首次访问自动生成空 dict，`len(state) == 0` 就是「该参数还没有任何状态」的判据。muon 分支的状态只有一个键 `momentum_buffer`（对比 adam 分支的 `exp_avg`/`exp_avg_sq`/`step` 三个键，[muon.py:L214-L218](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L214-L218)）。缓冲区随后作为**第二个参数传给** `muon_update`，在那里被 `momentum.lerp_(grad, 1 - beta)` 原地更新（u2-l3）——所以 `SingleDeviceMuon` 自己不需要写任何动量更新代码，只是「保管」这块缓冲区。

#### 4.3.4 代码实践

**实践目标**：用「一次不 backward 的 step」同时验证惰性初始化与填零路径的纯衰减行为。

1. 操作步骤（示例代码）：

```python
import torch
from muon import SingleDeviceMuon

w = torch.nn.Parameter(torch.ones(64, 64))
opt = SingleDeviceMuon([w], lr=0.02, weight_decay=0.01)

print("step 前 len(opt.state) =", len(opt.state))     # 期望 0：还没分配动量缓冲
n0 = w.norm().item()
opt.step()                                            # 没有 backward，p.grad 为 None
n1 = w.norm().item()
print("一步后 len(opt.state) =", len(opt.state))      # 期望 1：momentum_buffer 已创建
print("权重范数之比 n1/n0 =", n1 / n0)                # 期望 1 - 0.02*0.01 = 0.9998
print("momentum_buffer 全零？", bool((opt.state[w]["momentum_buffer"] == 0).all()))
```

2. 需要观察的现象：state 大小从 0 变 1；范数比恰好是 \(1 - \mathrm{lr}\cdot\mathrm{wd}\)；动量缓冲全零。
3. 预期结果：梯度填零 → 动量保持零 → NS 正交化输出全零更新 → `add_(0)` 不改参数，唯一效果是 `p.mul_(1 - 2e-4)`，故 \(n_1/n_0 = 0.9998\)（float32 下可能有 1e-7 级别的舍入偏差，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：分布式 `Muon.step` 里，如果把填零改回 `continue`，构造一个挂死场景。
答：设 `world_size=2`，参数 `p_a` 在 rank 0 上有梯度、在 rank 1 上 `p.grad is None`（例如两卡数据走了条件计算的不同分支且未用 DDP 同步梯度）。rank 0 执行到 [muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94) 的 `all_gather` 等待 rank 1；rank 1 却 `continue` 跳过了本轮 `all_gather`，直接进入下一轮的 `all_gather`——两个进程在通信序列上错位，集体通信永远无法匹配。

**练习 2**：惰性初始化为什么不能挪到 `__init__` 里（构造时就为每个参数建好缓冲）？
答：功能上可行，但会为**从未被 step 的参数**白付一份与参数同大小的显存（构造时只想统计参数、暂不训练的场景很常见）；且违背 PyTorch 惯例——`torch.optim.SGD` 也是首步懒创建，检查 `len(state) == 0` 是社区通用的「首次 step」判据。

**练习 3**：梯度被填零、但动量缓冲非零时，该参数这一步还会移动吗？
答：会。Nesterov 更新为 \(u = (1-\beta)\cdot 0 + \beta\, m = \beta m\)（[muon.py:L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L36)），只要动量没耗尽，正交化后的更新仍非零；同时动量自身以 \(m \leftarrow \beta m\) 指数衰减。这正是「梯度缺失 = 梯度为零」与「梯度缺失 = 冻结」的语义差别。

### 4.4 两行原地更新：解耦权重衰减的精确语义

#### 4.4.1 概念说明

step 主循环的最后两行把「算出来的更新」变成「参数的新值」，合并成一个公式：

\[
p \;\leftarrow\; \underbrace{\bigl(1 - \mathrm{lr}\cdot\mathrm{wd}\bigr)\,p}_{\text{L124：先收缩}} \;-\; \underbrace{\mathrm{lr}\cdot u}_{\text{L125：再走一步}}
\]

这就是 **AdamW 式解耦权重衰减**（u2-l4 已给出名字，这里给出精确语义）：衰减量与梯度大小无关，每步固定把权重收缩 \((1-\mathrm{lr}\cdot\mathrm{wd})\) 倍。它与 `torch.optim.SGD(weight_decay=wd)` 的**耦合式**（把 \(\mathrm{wd}\cdot p\) 加进梯度后再进动量）形成对照。

对 Muon 来说，解耦不只是风格选择，而是**必须的**。设想耦合式：把 \(\mathrm{wd}\cdot p\) 加进梯度，这个混合物要依次经过 EMA 动量和 Newton-Schulz 正交化。u2-l1 讲过正交化是尺度不变的——它只保留方向配对、把谱范数拉回 ~1。于是「衰减信号」与「梯度信号」的相对强弱在正交化中被彻底重组：衰减的真实强度不再由 \(\mathrm{lr}\cdot\mathrm{wd}\) 决定，而取决于衰减项恰好落在哪些奇异方向上。乘法收缩放在正交化**之外**，衰减语义才能保持精确。

顺序同样有语义：先 `mul_` 后 `add_`，本步的新更新不参与本步衰减（练习 3 已从另一角度问过）。这与 `torch.optim.AdamW` 的实现一致：AdamW 对每个参数也是先 `p.mul_(1 - lr*wd)` 再 `p.add_(update, alpha=-lr)`。

#### 4.4.2 核心流程

```
update = muon_update(...)                    ← 谱范数 ~1（σ∈[0.5,1.5]）· √max(1, m/n)
      │
      ▼
p.mul_(1 - lr·wd)          ← 解耦衰减：与梯度无关的固定收缩
      │
      ▼
p.add_(update, alpha=-lr)  ← 施加更新：p ← p - lr·update
```

数量级直觉（承接 u2-l3 的「更新 RMS ≈ 1/√n」结论）：每步参数的逐元素 RMS 改变量约为 \(\mathrm{lr}/\sqrt{n}\)（\(n\) 为该层 fan-in）。把更新近似为随机游走，可以估算衰减开启后权重范数的平衡点——每步收缩 \(2\lambda-\lambda^2\) 倍方差（\(\lambda=\mathrm{lr}\cdot\mathrm{wd}\)），与新增 \((\mathrm{lr}/\sqrt{n})^2\) 的方差平衡：

\[
p_{\mathrm{RMS}}^{\,2}\,(2\lambda - \lambda^2) = \frac{\mathrm{lr}^2}{n}
\;\;\Longrightarrow\;\;
p_{\mathrm{RMS}} \;\approx\; \sqrt{\frac{\mathrm{lr}}{2\cdot\mathrm{wd}\cdot n}}
\]

代入默认 `lr=0.02`、README 推荐的 `wd=0.01`、`n=256`：\(p_{\mathrm{RMS}} \approx \sqrt{0.02/5.12} = 0.0625\)。这是**量级参考**（真实训练的梯度信号并非随机游走），但足以预测：关掉 weight_decay 权重范数会随训练漂移增大，开启则被压制在某个上限附近。综合实践（第 5 节）会实测这一点。

#### 4.4.3 源码精读

```python
p.mul_(1 - group["lr"] * group["weight_decay"])
p.add_(update.reshape(p.shape), alpha=-group["lr"])
```

出处：[muon.py:L124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125)。

- 第 124 行：原地乘法收缩。`weight_decay` 默认为 0（[muon.py:L103](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103)），此时乘的是 1（数值上不衰减，仅多一次乘法核）；README 的替换示例显式设 `weight_decay=0.01`（[README.md:L26-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L26-L31)），此时每步收缩因子是 \(1 - 0.02\times 0.01 = 0.9998\)。
- 第 125 行：`alpha=-group["lr"]` 把「沿 update 方向走一步」写成单个原地加法；`reshape` 的作用见 4.2.3。注意 `lr` 的量纲是「每次更新的谱范数」（[muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61) 的 docstring），因此默认 0.02 意味着每步权重至多被扰动约 0.02×σ（σ∈[0.5,1.5]）的谱范数——这解释了它与 AdamW 学习率不可比的原因。

这两行与 `SingleDeviceMuonWithAuxAdam` 的 muon 分支（[muon.py:L267-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267-L269)）逐字相同，adam 分支的对应两行（[muon.py:L283-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L283-L284)）则把 `update` 换成了 `adam_update` 的输出——再次印证 u1-l3 的「骨架复用」观察。

#### 4.4.4 代码实践

**实践目标**：用 100 次「无梯度 step」实测纯衰减因子，验证公式 \(p \leftarrow (1-\mathrm{lr}\cdot\mathrm{wd})^k p\)。

1. 操作步骤（示例代码）：

```python
import torch
from muon import SingleDeviceMuon

w = torch.nn.Parameter(torch.ones(64, 64))
opt = SingleDeviceMuon([w], lr=0.02, weight_decay=0.01)

n0 = w.norm().item()
for _ in range(100):        # 从不 backward：每步只有衰减在起作用
    opt.step()
ratio = w.norm().item() / n0
print("100 步后范数比:", ratio)
print("理论值 0.9998**100 =", 0.9998 ** 100)     # ≈ 0.9802
```

2. 需要观察的现象：范数比与 \(0.9998^{100}\) 在 float32 精度内一致（约 0.9802）。
3. 预期结果：由 4.3.4 的「零更新」推理可直接推出，\(n_k/n_0 = (1-\mathrm{lr}\cdot\mathrm{wd})^k\)（待本地验证）。若改设 `weight_decay=0` 再跑 100 步，范数应严格不变——衰减是这两行中唯一「无梯度也生效」的部分。

#### 4.4.5 小练习与答案

**练习 1**：写出 `SingleDeviceMuon` 单步更新的完整公式，并标出每一项来自哪一行。
答：\(p \leftarrow (1-\mathrm{lr}\cdot\mathrm{wd})\,p - \mathrm{lr}\cdot u\)。前一项来自 [muon.py:L124](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124) 的乘法收缩，后一项来自 [muon.py:L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L125)，\(u\) 是 `muon_update` 的输出。

**练习 2**：为什么不把衰减写成 `grad += wd * p`（耦合式）再交给 `muon_update`？
答：耦合项要过 EMA 动量（时间上被平滑）和 Newton-Schulz 正交化（尺度不变、按奇异方向重组）。正交化只保留方向信息，衰减项与梯度项的相对强度会被破坏：衰减的真实速率不再等于 \(\mathrm{lr}\cdot\mathrm{wd}\)，也不再均匀作用于权重的各个方向，4.4.2 的平衡点公式随之失效。

**练习 3**：`lr=0.02`、`weight_decay=0`（默认值）时，L124 实际执行了什么？
答：`p.mul_(1)`——数值上恒等，参数不变；代价只是一次逐元素乘法。默认配置下 Muon 完全不衰减权重，这也解释了为什么 README 明确建议把 `weight_decay` 列为仅有的两个必调超参之一（[README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47)）。

## 5. 综合实践

**任务：给 `SingleDeviceMuon.step` 加仪表（instrumentation），在一个 3 层 MLP 上训练 200 步，实测「每步更新的 RMS」与「更新后权重范数」，并对比 `weight_decay=0` 与 `0.01` 两种设置。**

这个任务把本讲全部内容串起来：你会动手修改 step 骨架（4.2）、观察惰性初始化与填零是否如预期发生（4.3）、并亲手验证解耦衰减对权重范数的压制（4.4）。

### 步骤 1：复制并改造（不改动仓库源码）

把 `muon.py` 复制为练习目录里的 `muon_instr.py`，在 `SingleDeviceMuon` 上加两处（示例代码，仅展示新增部分）：

```python
class SingleDeviceMuon(torch.optim.Optimizer):
    def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
        defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
        super().__init__(params, defaults)
        self.log = []                                   # ← 新增：仪表记录

    @torch.no_grad()
    def step(self, closure=None):
        # ...closure 段与主循环开头保持原样...
                    update = muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])
                    p.mul_(1 - group["lr"] * group["weight_decay"])
                    p.add_(update.reshape(p.shape), alpha=-group["lr"])
                    self.log.append((                                  # ← 新增：在两行更新之后记录
                        update.float().pow(2).mean().sqrt().item(),    #    更新的逐元素 RMS
                        p.norm().item(),                               #    更新后的权重 Frobenius 范数
                    ))
        return loss
```

注意记录点放在**两行更新之后**：`p.norm()` 才是「更新后」的范数；而 `update` 此时尚未被 `alpha=-lr` 缩放，记录的是正交化输出的原始 RMS（可与理论值 \(1/\sqrt{n}\) 直接对照）。

### 步骤 2：合成数据 + 3 层 MLP + 200 步训练（示例代码）

```python
import torch, torch.nn as nn
from muon_instr import SingleDeviceMuon          # 你改过的副本

torch.manual_seed(0)
device = "cpu"

# 合成回归任务：用一个固定随机 teacher 生成可学的目标
x = torch.randn(1024, 256, device=device)
with torch.no_grad():
    teacher = nn.Sequential(nn.Linear(256, 256), nn.Tanh(), nn.Linear(256, 1))
    y = teacher(x)

def make_model():
    return nn.Sequential(                        # 注意全部 bias=False：Muon 只吃 ndim>=2 的参数（u1-l2）
        nn.Linear(256, 256, bias=False), nn.ReLU(),
        nn.Linear(256, 256, bias=False), nn.ReLU(),
        nn.Linear(256, 1, bias=False),
    )

def run(wd):
    model = make_model()
    opt = SingleDeviceMuon([p for p in model.parameters()], lr=0.02, weight_decay=wd)
    for step in range(200):
        loss = ((model(x) - y) ** 2).mean()
        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()
        if step % 40 == 0 or step == 199:
            logs = opt.log[-3:]                   # 最后一个 step 的 3 个参数
            rms = [f"{r:.5f}" for r, _ in logs]
            nrm = [f"{n:.2f}" for _, n in logs]
            print(f"wd={wd} step={step:3d} loss={loss.item():.4f} updateRMS={rms} weightNorm={nrm}")
    return model

run(wd=0.0)
run(wd=0.01)
```

### 步骤 3：观察什么、预期看到什么

1. **更新 RMS 几乎恒定且三层相同**：三层权重都是 fan-in \(n=256\)，u2-l3 结论「更新 RMS ≈ \(1/\sqrt{n}\)」预测仪表记录应约等于 \(1/16 = 0.0625\)（随机波动与小奇异值扰动会带来偏差）。这验证了 4.2 中「更新幅度只由形状决定」的性质。
2. **wd=0 与 wd=0.01 的权重范数走势**：`wd=0` 时三个 `weightNorm` 随训练**持续增长**（没有任何收缩机制，范数只能被梯度信号推着涨）；`wd=0.01` 时范数在几十步内进入**有界波动**。4.4.2 的平衡点估计 \(p_{\mathrm{RMS}} \approx 0.0625\) 对应 256×256 层的 Frobenius 范数约 \(0.0625 \times 256 = 16\)——实测值应与之同数量级（梯度信号非随机游走，偏大偏小都正常）。
3. **惰性初始化时刻**：在第 1 步 `opt.step()` 前后各打印一次 `len(opt.state)`，应看到 0 → 3（3 个参数的 `momentum_buffer` 同批创建）。
4. **填零分支是否触发**：本脚本的参数每步都有梯度，`p.grad is None` 分支不会触发；想验证它，可在第 100 步后只对部分层 backward（或临时跳过 `loss.backward()` 直接 `opt.step()`），观察 4.3.4 的纯衰减行为——loss 不变、`weightNorm` 缩小 \(0.9998\)。

以上数值预期均由源码逻辑推出，具体打印值**待本地验证**（本讲义编写环境未运行训练）。若实测 RMS 明显偏离 0.0625，优先检查是不是把记录点放在了 `p.add_(..., alpha=-lr)` 的影响之后、或模型的层不是 fan-in 256。

## 6. 本讲小结

- `SingleDeviceMuon` 是四个类中最小骨架：构造函数仅「组装 `defaults` + 调父类」两行；分布式 `Muon` 多出的断言（拒绝生成器/参数组字典/空列表）与按尺寸降序排序（保证 `all_gather` 的形状一致性），单机版一行都不需要（[muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69) vs [muon.py:L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105)）。
- `step()` 主循环 11 行 = 梯度缺失填零 → `state` 惰性初始化 → 调 `muon_update` → 两行原地更新；超参每次从 `group["..."]` 现取，支持热更新（[muon.py:L115-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L115-L125)）。
- `p.grad is None → torch.zeros_like(p)` 的「强制同步」是为分布式版设计的（`continue` 会跳过 `all_gather` 造成错位挂死，git 提交 `3b04428` → `5bc6993` 记录了这个演变）；在单机版中它把「梯度缺失」定义为「梯度为零」——动量衰减、残余更新、weight decay 照常进行。
- `momentum_buffer` 在参数首次被 step 到时才分配（`len(state) == 0` 判据），优化器只负责「保管」缓冲区，动量更新发生在 `muon_update` 内部。
- 两行更新 \(p \leftarrow (1-\mathrm{lr}\cdot\mathrm{wd})\,p - \mathrm{lr}\cdot u\) 是 AdamW 式**解耦**衰减；衰减必须放在 Newton-Schulz 正交化之外，否则尺度不变的正交化会摧毁衰减强度 \(\mathrm{lr}\cdot\mathrm{wd}\) 的语义。
- 记住两个可复用的数值直觉：更新 RMS ≈ \(\mathrm{lr}/\sqrt{n}\)（\(n\) 为 fan-in）；开衰减后权重 RMS 的平衡点约 \(\sqrt{\mathrm{lr}/(2\cdot\mathrm{wd}\cdot n)}\)。

## 7. 下一步学习建议

本讲之后，你已具备逐行读懂其余三个优化器类的全部前置知识。推荐路线：

1. **u3-l1（adam_update 精读）**：先读 [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135) 的 30 行手写 AdamW——它是 `MuonWithAuxAdam` 中 `use_muon=False` 分支的引擎，与本讲 4.4 的两行更新完全同构（换一个 `update` 来源而已）。
2. **u3-l2（MuonWithAuxAdam）**：观察 [muon.py:L258-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L258-L284) 如何把本讲的 step 骨架复制进 `use_muon` 的两个分支——你会认出每一个字符。
3. **u3-l3（分布式 Muon）**：本讲 4.1 删掉的两行（断言、排序）将在 `torchrun` 实验中「复活」；建议预习 [muon.py:L79-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L79-L94) 的分片循环，并思考：`range(len(params))[::world_size]` 加上 `base_i + rank` 是怎么把一批参数轮流分摊到各 GPU 的。
4. 动手方向：把第 5 节的仪表实验扩展成你的「优化器观察工具箱」——后续任何一讲的实验（u3-l4 的 lr 网格搜索、u4-l1 的 ns_steps 实验）都能复用这套 instrumentation 方法。
