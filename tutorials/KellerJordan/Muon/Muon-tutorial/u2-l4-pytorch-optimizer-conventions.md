# u2-l4 · PyTorch Optimizer 扩展约定：读懂 step() 之前必备的框架知识

## 1. 本讲目标

前几讲我们啃完了 muon.py 的两个数学函数：`zeropower_via_newtonschulz5`（u2-l2）和 `muon_update`（u2-l3）。接下来就要进入四个优化器类的 `step()` 了。但在逐行精读之前，必须先补一课：**muon.py 的 `step()` 里 80% 的代码不是 Muon 的算法，而是 PyTorch 框架的「暗号」**——`param_groups`、`self.state`、`@torch.no_grad()`、`closure`、`p.mul_`/`p.add_`。这些是所有自定义优化器共有的骨架（u1-l3 已经指出这套骨架在 muon.py 中出现了六次）。

本讲是进入 `step()` 精读（u2-l5）前的最后一道前置关卡。读完本讲，你应该能够：

1. **解释 `param_groups`、`defaults`、`self.state[p]` 三个数据结构的分工**：超参如何从构造函数流到 `step()` 里的 `group["lr"]`，动量缓冲存在哪里。
2. **理解 `@torch.no_grad()` 装饰器与 `closure` 可选参数的用途**：为什么缺了前者 `step()` 第一步就会崩溃，后者为什么在 `torch.enable_grad()` 里被调用。
3. **掌握 `p.mul_(1 - lr*wd); p.add_(update, alpha=-lr)` 这种两行原地更新写法**：它就是 AdamW 式解耦权重衰减的展开形式。
4. **从零写出一个能跑的自定义优化器**：本讲综合实践中，你将模仿本仓库风格实现 `MySGDMomentum`，并与 `torch.optim.SGD(momentum=0.95)` 对拍，确认 loss 曲线重合。

## 2. 前置知识

承接前面讲义已建立的概念（一句话回顾）：

- **优化器骨架**（u1-l3）：四个类共享同一副 `step()` 骨架——遍历参数组 → 梯度缺失填零 → `state` 惰性初始化 → 调 `muon_update` / `adam_update` → 两行原地更新；这套 per-parameter 骨架在文件里出现六次。
- **`muon_update` 的契约**（u2-l3）：传入的 `grad` 和 `momentum` 都会被**原地改写**；它输出一个「正交化 + 尺度缩放」后的更新矩阵。
- **EMA 动量与求和型动量**（u2-l3）：\(m_t = \beta m_{t-1} + (1-\beta)g_t\)（EMA，`lerp_` 写法）与 \(v_t = \beta v_{t-1} + g_t\)（求和型，`torch.optim.SGD` 的记账方式）方向相同，只差常数倍 \((1-\beta)\)。本讲综合实践会再次用到这个结论。

本讲新增的框架概念：

| 概念 | 通俗解释 |
|---|---|
| `torch.optim.Optimizer` | PyTorch 所有优化器的基类。它本身不含任何算法，只负责「记账」：管理参数分组、默认超参、逐参数状态。子类只需要实现 `step()`。 |
| `param_groups` | 参数组列表，每个元素是一个 `dict`，形如 `{"params": [...], "lr": ..., ...}`。**超参的作用域是组，不是优化器实例**——不同参数可以用不同的学习率。 |
| `defaults` | 默认超参模板（一个 `dict`）。构造时对每个组**补全缺失键**（显式给出的键优先），存为 `self.defaults`。 |
| `self.state` | 一个 `defaultdict(dict)`：键是 `Parameter` **张量对象本身**，值是一个普通 `dict`（如 `{"momentum_buffer": Tensor}`），存放该参数跨步的记忆。 |
| 叶子张量（leaf tensor） | 由用户直接创建（如 `nn.Parameter`）、而非运算产生的张量，`requires_grad=True`。自动求导只对「叶子」累积梯度。 |
| 原地操作（in-place） | 方法名带下划线（`mul_`、`add_`、`lerp_`），直接修改张量本身而不是产生新张量。 |
| 解耦权重衰减 | 先把权重收缩 \(p \leftarrow p(1-\mathrm{lr}\cdot\mathrm{wd})\)，再沿更新方向走一步。与 `torch.optim.SGD` 的「耦合式」（把 \(\mathrm{wd}\cdot p\) 加进梯度）相对。 |
| `closure`（闭包） | 一个「重新计算 loss 并返回它」的可调用对象，是 PyTorch 优化器的约定参数，供 LBFGS 这类需要反复重估损失的优化器使用。 |

一个值得先想通的问题：**为什么 muon.py 不自己写一套训练循环，而要继承 `torch.optim.Optimizer`？** 因为接上这个基类，就能免费获得 PyTorch 生态的整套基础设施：`zero_grad()`、`state_dict()`/`load_state_dict()`（checkpoint 保存/恢复）、学习率热更新、`add_param_group()`……自定义优化器只需要遵守几条约定，就能无缝替换任何训练脚本里的 `torch.optim.AdamW`——这正是 Muon 能直接插进 NanoGPT speedrun 代码里的原因。

## 3. 本讲源码地图

本讲只涉及一个文件，但会把 muon.py 中所有「框架约定」相关的行号集中列出：

| 位置 | 角色 |
|---|---|
| [muon.py:L44](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44)、[muon.py:L99](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99)、[muon.py:L138](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138)、[muon.py:L228](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228) | 四个类声明，全部继承 `torch.optim.Optimizer` |
| [muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69) | `Muon.__init__`：`defaults` 组装 + `super().__init__`（4.1 节） |
| [muon.py:L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105) | `SingleDeviceMuon.__init__`：不加断言、不排序的最简写法（4.1 节） |
| [muon.py:L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182) | `MuonWithAuxAdam.__init__`：手动构造 `param_groups`、空 `defaults`（4.2 节，u3-l2 详讲） |
| [muon.py:L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248) | `SingleDeviceMuonWithAuxAdam.__init__`：同上的单机版 |
| [muon.py:L107-L113](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L107-L113) | `SingleDeviceMuon.step` 开头：`@torch.no_grad()` + `closure` 约定（4.4 节；同样 8 行在四个类中各出现一次） |
| [muon.py:L115-L116](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L115-L116) | `for group in self.param_groups:` 遍历参数组（4.2 节） |
| [muon.py:L120-L122](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L120-L122) | `self.state[p]` + 惰性初始化惯用法（4.3 节） |
| [muon.py:L214-L219](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L214-L219) | adam 分支的另一种 `state` 形态：`exp_avg` / `exp_avg_sq` / `step`（4.3 节） |
| [muon.py:L124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125) | 两行原地更新（4.5 节；六处同类位置见 4.5 节表格） |

## 4. 核心概念与源码讲解

### 4.1 torch.optim.Optimizer.\_\_init\_\_：继承时必须做的一件事

#### 4.1.1 概念说明

PyTorch 官方文档给自定义优化器规定的最小模板只有两部分：

1. `__init__` 里：把超参打包成 `defaults` 字典，然后调用 `super().__init__(params, defaults)`；
2. 实现 `step(closure=None)` 方法。

父类 `torch.optim.Optimizer.__init__` 负责所有「记账」工作（这部分代码在 PyTorch 框架的 `torch/optim/optimizer.py` 中，不在本仓库里）。它做的事可以概括为：

- 检查 `params` 不是单个张量（必须是张量的可迭代对象，或字典的可迭代对象）；
- 把参数**规范化成参数组列表**：传扁平的参数列表 → 包装成单组 `[{"params": [...]}]`；传字典列表 → 每个字典就是一组；
- 对每个组，用 `defaults` **补全缺失键**（等价于对每个键做 `setdefault`），组里显式给出的键优先；**组里的额外键会被原样保留**（这是 4.2 节 `use_muon` 能工作的前提）；
- 建立三大数据结构：`self.param_groups`（组列表）、`self.defaults`（默认模板）、`self.state = defaultdict(dict)`（逐参数状态，初始为空）。

#### 4.1.2 核心流程

```
用户调用 MyOptimizer(params, lr=..., momentum=...)
        │
        ▼
__init__: defaults = dict(lr=..., momentum=...)     ← 子类职责：组装 defaults
        │
        ▼
super().__init__(params, defaults)                   ← 父类职责：记账
        │
        ├─ self.param_groups = [ {**defaults, "params": [...]}, ... ]
        ├─ self.defaults     = defaults
        └─ self.state        = defaultdict(dict)     ← 此时还是空的
```

#### 4.1.3 源码精读

分布式版 `Muon` 的构造函数是全文件最「啰嗦」的一个，正好用它观察全部细节：

[muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69) —— 依次做四件事：组装 `defaults`、断言参数形态、按尺寸排序、交给父类。

```python
def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
    defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
    assert isinstance(params, list) and len(params) >= 1 and isinstance(params[0], torch.nn.Parameter)
    params = sorted(params, key=lambda x: x.size(), reverse=True)
    super().__init__(params, defaults)
```

逐行解读：

- **L65**：三个超参都有默认值——注意 `ns_steps` 和 `nesterov` **没有**暴露在构造函数里（u1-l3 已指出），它们藏在 `muon_update` 的函数签名中（[muon.py:L34](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34)）。
- **L66**：组装 `defaults`。这个字典之后会通过父类流入每个参数组，`step()` 里读的 `group["lr"]`、`group["momentum"]`、`group["weight_decay"]` 三个键就来自这里。
- **L67**：断言 `params` 必须是**非空的、元素为 `Parameter` 的扁平列表**。这实际上排除了 PyTorch 标准的「传字典分组」用法——为什么敢这么限制？因为分布式版需要在 L68 拿到扁平列表做排序和分片（这是 u3-l3 的主题，此处按下不表）。需要分组时应改用 `MuonWithAuxAdam`。
- **L68**：按 `size` 从大到小排序，目的同样在 u3-l3（让各 rank 分到的参数形状尽量均匀，补齐张量形状一致）。
- **L69**：`super().__init__(params, defaults)`——**自定义优化器唯一不可省略的一行**。不调用它，`self.param_groups` 和 `self.state` 就不存在，第一次 `step()` 会在 `for group in self.param_groups` 处抛 `AttributeError`。

对比单机版的最简写法：

[muon.py:L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105) —— `SingleDeviceMuon.__init__` 不断言、不排序，直接把参数交给父类；因为单机版没有排序和分片的需求，父类接受任何合法形态（扁平列表或字典列表）。

```python
def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
    defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
    super().__init__(params, defaults)
```

#### 4.1.4 代码实践

**实践目标**：给一个刚构造好的优化器「拍 X 光片」，亲眼看到 `super().__init__` 建立的三大数据结构。

**操作步骤**（在仓库根目录运行，或按 u1-l2 先 `pip install git+https://github.com/KellerJordan/Muon`）：

```python
# 示例代码
import torch
import muon  # 仓库根目录下运行；或 from muon import SingleDeviceMuon

w1 = torch.nn.Parameter(torch.randn(8, 4))
w2 = torch.nn.Parameter(torch.randn(4, 8))
opt = muon.SingleDeviceMuon([w1, w2], lr=0.05, momentum=0.9)

print(type(opt.param_groups), len(opt.param_groups))
print(opt.param_groups[0])
print(opt.defaults)
print(opt.state)
```

**需要观察的现象**：

- `param_groups` 是**列表**，长度 1（两个参数被装进了同一组）；
- 组内除了 `params` 外，出现 `lr`、`weight_decay`、`momentum` 三个键——构造时传入的 `defaults` 被复制进了组；
- `opt.state` 是空的 `defaultdict`。

**预期结果**：`opt.param_groups[0]["lr"] == 0.05`、`opt.param_groups[0]["momentum"] == 0.9`、`len(opt.state) == 0`。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果自定义优化器的 `__init__` 忘记调用 `super().__init__()`，会在什么时候、以什么方式失败？

> **答案**：构造时不会报错；第一次调用 `step()` 时，`for group in self.param_groups` 会抛 `AttributeError`，因为 `param_groups` 和 `state` 都是父类构造函数创建的属性。

**练习 2**：[muon.py:L67](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67) 的断言排除了 PyTorch 官方支持的一种参数传法，是哪种？本仓库为什么敢这样限制？

> **答案**：排除了传参数组字典列表（如 `[{"params": [...], "lr": ...}, ...]`）的标准用法。因为分布式 `Muon.step` 需要拿到**扁平的参数列表**来做按尺寸排序、按 rank 分片和空张量补齐（u3-l3 详讲）。需要分组的用户被引导到 `MuonWithAuxAdam`。

**练习 3**：`Muon.__init__` 的 `defaults` 里有几个键？`step()` 里用到其中几个？

> **答案**：3 个：`lr`、`weight_decay`、`momentum`。`step()` 里 3 个都用到：[muon.py:L91](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L91)（momentum）、[muon.py:L92-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L92-L93)（lr、weight_decay）。

### 4.2 param_groups / defaults 机制：超参数如何分组流动

#### 4.2.1 概念说明

`param_groups` 解决的问题是：**一个模型里，不同参数常常需要不同的超参**。对 Muon 来说这是刚需——隐藏层权重用 Muon 更新（lr 约 0.02，谱范数量纲），embedding 和输出头用 AdamW 更新（lr 约 3e-4），两者相差近两个数量级（u1-l3）。参数组就是「超参作用域」：每组自带一份超参，`step()` 逐组处理。

`defaults` 是兜底模板，规则只有三条：

1. 组里**缺失**的键 → 从 `defaults` 补上；
2. 组里**显式给出**的键 → 保留，不被 `defaults` 覆盖；
3. 组里 `defaults` 没有的**额外键** → 原样保留，`step()` 里可以随意取用。

第三条最容易被忽略，却是理解 `MuonWithAuxAdam` 的钥匙：`use_muon` 这个自定义标志正是靠它存活到 `step()` 里的 `group["use_muon"]`（[muon.py:L193](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L193)）。

还有一个直接推论：既然 `step()` 读的是 `group["lr"]` 而不是什么 `self.lr`，那么训练循环里直接写 `optimizer.param_groups[0]["lr"] = new_lr` 就能**热更新学习率**（warmup、decay 都这么做），不需要重建优化器。

#### 4.2.2 核心流程

muon.py 中两种构造风格的数据流：

```
风格 A（Muon / SingleDeviceMuon）:
  params（扁平列表） + defaults（3 键）
        └─► 父类包装成单组: param_groups = [{**defaults, "params": [...]}]

风格 B（MuonWithAuxAdam / SingleDeviceMuonWithAuxAdam）:
  用户传入 param_groups（每组带 use_muon 标志）
        └─► __init__ 逐组: group.get(key, default) 填超参
                          + assert 键集合恰好齐全
        └─► super().__init__(param_groups, dict())   ← defaults 为空！
```

#### 4.2.3 源码精读

风格 B 的代表是 `MuonWithAuxAdam.__init__`（其完整设计意图在 u3-l2 展开，本节只看「分组机制」本身）：

[muon.py:L165-L174](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L174) —— 逐组检查 `use_muon` 标志；对 Muon 组手动填默认超参（`group.get` 的第二参就是默认值），最后断言键集合**恰好**等于五个允许的键（多一个少一个都报错）。

```python
def __init__(self, param_groups):
    for group in param_groups:
        assert "use_muon" in group
        if group["use_muon"]:
            group["params"] = sorted(group["params"], key=lambda x: x.size(), reverse=True)
            # defaults
            group["lr"] = group.get("lr", 0.02)
            group["momentum"] = group.get("momentum", 0.95)
            group["weight_decay"] = group.get("weight_decay", 0)
            assert set(group.keys()) == set(["params", "lr", "momentum", "weight_decay", "use_muon"])
```

[muon.py:L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L182) —— 注意第二个参数是**空字典** `dict()`：

```python
super().__init__(param_groups, dict())
```

为什么敢传空 `defaults`？因为 L171-L180 已经把每组的必需键**全部填满**了，`defaults` 的「补缺」职能无事可做。代价是：如果日后对这样的优化器调用 `add_param_group(new_group)`，新组必须自带全部键，否则不会有任何兜底。

`step()` 一侧则永远是「逐组遍历、组内取超参」：[muon.py:L115-L116](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L115-L116) —— `for group in self.param_groups:` 然后从 `group` 里取 `"params"` 和各种超参（如 [L123](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L123) 的 `group["momentum"]`、[L124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125) 的 `group["lr"]` 与 `group["weight_decay"]`）。

四个类的构造风格对照：

| 类 | 构造位置 | params 形态 | defaults |
|---|---|---|---|
| `Muon` | [L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69) | 扁平列表（断言强制）+ 排序 | `dict(lr, weight_decay, momentum)` |
| `SingleDeviceMuon` | [L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105) | 任意合法形态 | 同上 |
| `MuonWithAuxAdam` | [L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182) | 手动 param_groups（`use_muon` 标志）+ muon 组排序 | `dict()`（空，组已自足） |
| `SingleDeviceMuonWithAuxAdam` | [L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248) | 手动 param_groups（无排序） | `dict()`（空，组已自足） |

另一个细节：L169 和 L171-L173 是**直接改写用户传入的字典**（`group["lr"] = ...`），也就是 `__init__` 会原地修改调用方手里的 `param_groups` 列表——阅读或复用这段代码时值得留意。

#### 4.2.4 代码实践

**实践目标**：验证 `defaults` 补全三规则（缺省补齐、显式优先、额外键保留），并体验学习率热更新。

**操作步骤**：

```python
# 示例代码
import torch
import muon

w1 = torch.nn.Parameter(torch.randn(8, 4))
w2 = torch.nn.Parameter(torch.randn(4, 8))
groups = [
    dict(params=[w1], lr=0.1),                              # 显式 lr
    dict(params=[w2], lr=0.01, my_extra_flag="hello"),      # 显式 lr + 额外键
]
opt = muon.SingleDeviceMuon(groups, lr=0.05, momentum=0.95)

g0, g1 = opt.param_groups
print(g0["lr"], g0["momentum"], g0["weight_decay"])  # 显式 0.1；后两者来自 defaults
print(g1["lr"], g1["my_extra_flag"])                 # 0.01 和 "hello"（额外键被保留）
g0["lr"] = 0.2                                       # 热更新第 0 组学习率
print(g0["lr"])
```

**需要观察的现象**：第 0 组没有显式给 `momentum`/`weight_decay`，但打印出的值是构造函数里的 0.95 和 0（被 `defaults` 补齐）；`my_extra_flag` 出现在 `param_groups[1]` 里；改 `g0["lr"]` 后组内立即生效。

**预期结果**：输出 `0.1 0.95 0`、`0.01 hello`、`0.2`。注意：把同样的 `groups` 传给分布式版 `Muon` 会在 [muon.py:L67](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67) 的断言处抛 `AssertionError`（字典不是 `Parameter`）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `step()` 里写 `group["lr"]` 而不是 `self.lr`？这对训练循环意味着什么？

> **答案**：超参的作用域是参数组而非优化器实例，同一次 `step()` 里不同组可以用不同学习率（Muon 组 0.02、Adam 组 3e-4 就是这么实现的）。训练循环里直接赋值 `optimizer.param_groups[i]["lr"] = x` 即可热更新学习率，无需重建优化器。

**练习 2**：`MuonWithAuxAdam` 给父类传了空 `defaults`（[muon.py:L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L182)），为什么 `step()` 里取 `group["betas"]` 不会 `KeyError`？

> **答案**：`__init__` 已在 L175-L181 对每组用 `group.get(key, default)` 填满了全部必需键，且 `assert set(group.keys()) == ...` 保证键集合恰好齐全。`defaults` 只负责补缺，此处无缺可补。

**练习 3**：`use_muon` 不是 PyTorch 认识的键，为什么 `step()` 里能用 `group["use_muon"]` 取到它？

> **答案**：`Optimizer` 对每个参数组只做「用 `defaults` 补缺」，不会删除或过滤额外键；任意自定义键都会原样进入 `param_groups`。`use_muon` 分支调度（[muon.py:L193](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L193)、[L259](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L259)）完全建立在这个框架行为上。

### 4.3 self.state 状态字典：每个参数一份的惰性记忆

#### 4.3.1 概念说明

`self.state` 存放**跨步记忆**：动量缓冲、Adam 的一阶/二阶矩、步数计数器。它的结构值得背下来：

```python
self.state = defaultdict(dict)   # 键：Parameter 对象；值：普通 dict
# 访问 self.state[p] 时，若 p 尚无条目，defaultdict 自动创建空 dict {}
```

三个关键设计：

1. **键是参数张量对象本身**（按对象身份），不是名字、不是索引。优化器工作在张量层，低于 `nn.Module` 的命名层——不同子模块的参数可以重名，而张量对象身份永远唯一。只有序列化时（`state_dict()`）才会换成「参数索引」。
2. **惰性初始化**：构造优化器时 `state` 是空的，第一次 `step()` 访问某参数时才为它分配缓冲。`if len(state) == 0:` 是官方优化器的统一惯用法（`torch.optim.Adam` 等同样如此）。
3. **值是普通 dict，键名自定义**：muon.py 用了两种形态——Muon 分支一个张量，Adam 分支两个张量加一个整数计数器。

惰性初始化的好处：只为**真正参与训练的参数**分配状态；且缓冲用 `torch.zeros_like(p)` 创建，自动匹配参数当时的形状、`dtype` 和设备（参数在构造优化器之后还可能被 `.to(device)` 迁移）。

#### 4.3.2 核心流程

`step()` 内处理单个参数的状态逻辑（单机版为例）：

```
state = self.state[p]            # defaultdict：首次访问自动得到 {}
if len(state) == 0:              # 「空 dict」= 第一次见到这个参数
    state["momentum_buffer"] = torch.zeros_like(p)   # 分配与 p 同形状/设备/dtype 的零缓冲
update = muon_update(p.grad, state["momentum_buffer"], ...)
        ▲ 注意：缓冲被传入后原地更新，下一 step 读到的就是新值（u2-l3）
```

#### 4.3.3 源码精读

Muon 分支的状态（单机版）：

[muon.py:L120-L122](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L120-L122) —— 取出（或首次创建）该参数的 `state`，为空则分配零动量缓冲；[L123](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L123) 把 `p.grad` 和这块缓冲一起交给 `muon_update`。

```python
state = self.state[p]
if len(state) == 0:
    state["momentum_buffer"] = torch.zeros_like(p)
update = muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])
```

Adam 分支的状态（分布式版）：

[muon.py:L214-L219](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L214-L219) —— 三个状态键：一阶动量 `exp_avg`、二阶动量 `exp_avg_sq`、步数计数器 `step`；`step` 是普通 Python 整数，每次自增，供 `adam_update` 做偏差修正 \(1-\beta^t\)（u3-l1 详讲）。

```python
state = self.state[p]
if len(state) == 0:
    state["exp_avg"] = torch.zeros_like(p)
    state["exp_avg_sq"] = torch.zeros_like(p)
    state["step"] = 0
state["step"] += 1
```

同样的惰性初始化块在文件中出现了六次（四个 `step()` 中，两个 `WithAuxAdam` 类各含 muon/adam 两个分支）：[L88-L90](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L88-L90)、[L120-L122](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L120-L122)、[L202-L204](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L202-L204)、[L214-L218](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L214-L218)、[L264-L266](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L264-L266)、[L275-L279](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L275-L279)。

因为状态键全是普通张量和整数，PyTorch 的 `optimizer.state_dict()` / `load_state_dict()` 开箱即用——这正是 u1-l2 强调的「继承基类免费获得 checkpoint 支持」。

#### 4.3.4 代码实践

**实践目标**：观察 `state` 从空到有的过程，以及 `state_dict()` 的序列化形态。

**操作步骤**：

```python
# 示例代码
import torch
import muon

p = torch.nn.Parameter(torch.randn(16, 8))
opt = muon.SingleDeviceMuon([p], lr=0.02)
print("step 前:", dict(opt.state))          # 空

for i in range(3):
    opt.zero_grad()
    loss = (p ** 2).sum()
    loss.backward()
    opt.step()
    st = opt.state[p]
    print(i, list(st.keys()), st["momentum_buffer"].shape)

sd = opt.state_dict()
print(sd.keys(), list(sd["state"].keys()))  # 顶层两键；参数被编号为 0
```

**需要观察的现象**：第一次打印 `state` 为空；每步之后它只有一个键 `momentum_buffer`，形状与 `p` 完全相同；`state_dict()` 的顶层是 `state` 与 `param_groups` 两键，且 `state` 里用整数 `0`（参数的全局索引）作键，而不是参数对象。

**预期结果**：`momentum_buffer.shape == torch.Size([16, 8])`；`list(sd["state"].keys()) == [0]`。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`self.state` 的键为什么是 `Parameter` 对象而不是参数名字符串？

> **答案**：优化器在张量层工作，看不到 `nn.Module` 的命名；参数名不保证唯一（不同子模块可重名），而张量对象身份唯一可靠。序列化时 `state_dict()` 会把对象换成按组的全局索引。

**练习 2**：`if len(state) == 0` 与 `if "momentum_buffer" not in state` 等价吗？

> **答案**：对 muon.py 的两个纯 Muon 类语义等价（state 只可能空或恰含一个键）。区别在一般性：前者不关心状态里有几个键，是 PyTorch 官方优化器（Adam、SGD 等）的统一惯用法，adam 分支有三个键时它依然简洁。

**练习 3**：adam 分支的 `state` 里为什么要有 `step` 计数器，而 muon 分支不需要？

> **答案**：`adam_update`（[muon.py:L133-L134](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L133-L134)）做偏差修正需要步数 \(t\)：除以 \(1-\beta_1^t\) 和 \(1-\beta_2^t\)。动量 EMA 没有偏差修正，无需计数（u3-l1 会逐行推导）。

### 4.4 @torch.no_grad() 与 closure：step() 的两个框架约定

#### 4.4.1 概念说明

**约定一：`step()` 要在无求导模式下运行。** 模型参数是 `requires_grad=True` 的叶子张量，而 `step()` 要对它们做原地修改（`p.mul_` / `p.add_`）。在默认的求导模式下，对叶子张量的原地操作会直接抛 `RuntimeError`（"a leaf Variable that requires grad is being used in an in-place operation"）。`@torch.no_grad()` 装饰器让整个 `step()` 关闭自动求导追踪，原地更新于是合法；同时参数更新本身也不该被记进计算图——否则下一次 `backward()` 会试图穿过优化器的更新。muon.py 四个 `step()` 全部带此装饰器。

**约定二：`step(closure=None)` 的闭包协议。** PyTorch 文档对 `closure` 的定义是「重新评估模型并返回损失的闭包」。它服务于两类场景：

- LBFGS 这类需要在一轮优化中**多次重估**损失的算法（由优化器反复调用 closure）；
- 把「前向 + 反向」打包给优化器的训练写法（closure 内部自行 `loss.backward()`）。

muon.py 遵循这一约定的写法是固定 8 行，在四个类中一字不差地出现。

#### 4.4.2 核心流程

```
@torch.no_grad()                  # 整个 step 关闭求导（原地更新合法）
def step(self, closure=None):
    loss = None
    if closure is not None:
        with torch.enable_grad(): # 内层重新开求导——closure 要建图求导
            loss = closure()      # closure 自己负责 forward + backward
    ... 实际的参数更新 ...
    return loss                   # 无 closure 时返回 None
```

两层上下文并不矛盾：外层 `no_grad` 保护的是**参数更新**，内层 `enable_grad` 服务于**损失重估**——二者目的不同，各管各的。

#### 4.4.3 源码精读

[muon.py:L107-L113](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L107-L113) —— `SingleDeviceMuon.step` 的开头，先装饰后处理 closure，然后才进入参数循环。

```python
@torch.no_grad()
def step(self, closure=None):

    loss = None
    if closure is not None:
        with torch.enable_grad():
            loss = closure()
```

同样的 8 行出现在：[L71-L77](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L71-L77)（`Muon`）、[L184-L190](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L184-L190)（`MuonWithAuxAdam`）、[L250-L256](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L250-L256)（`SingleDeviceMuonWithAuxAdam`）。

两个阅读要点：

- **muon.py 的 `step()` 只调用 closure 一次**，且不替你做 backward——梯度计算完全由 closure 自己负责。是否多次调用 closure 是具体算法的决定（LBFGS 会多次），框架只约定「closure 返回 loss」。
- 注意与 `p.grad` 填零的分工：closure（或训练循环的 `loss.backward()`）负责**产生**梯度；[L117-L119](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L117-L119) 的 `p.grad = torch.zeros_like(p)` 只在梯度缺失时兜底（u2-l5 详讲）。

#### 4.4.4 代码实践

**实践目标**：(a) 亲手复现「没有 no_grad 就会崩溃」；(b) 体验 closure 用法与返回值。

**操作步骤**：

```python
# 示例代码
import torch
import muon

# (a) 叶子张量的原地操作：默认求导模式 vs no_grad
p = torch.nn.Parameter(torch.randn(3))
try:
    p.add_(torch.ones(3))            # 求导模式下对叶子原地操作
except RuntimeError as e:
    print("报错:", e)
with torch.no_grad():
    p.add_(torch.ones(3))            # 关闭求导后合法
print("no_grad 下更新成功:", p.shape)

# (b) 用 closure 调用 step
w = torch.nn.Parameter(torch.randn(8, 4))
opt = muon.SingleDeviceMuon([w])
x, y = torch.randn(32, 8), torch.randn(32, 4)

def closure():
    opt.zero_grad()
    loss = ((x @ w - y) ** 2).mean()
    loss.backward()                  # closure 自己做 backward
    return loss

returned = opt.step(closure)
none_returned = opt.step()           # 不传 closure
print(returned is None, none_returned is None)
print("closure 返回的 loss:", returned.item())
```

**需要观察的现象**：(a) 第一段抛 `RuntimeError`，错误信息含 "a leaf Variable that requires grad is being used in an in-place operation"；`no_grad` 块内同样的操作成功。(b) `opt.step(closure)` 返回一个 loss 张量（可 `.item()`），`opt.step()` 返回 `None`。

**预期结果**：打印 `False True`（前者拿到了 loss，后者是 None）以及一个有限的 loss 数值。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：把 `@torch.no_grad()` 从 `SingleDeviceMuon.step` 上删掉，第一次调用会发生什么？为什么？

> **答案**：跑到 [muon.py:L124](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124) 的 `p.mul_(...)` 时抛 `RuntimeError`：默认求导模式下不允许对 `requires_grad=True` 的叶子张量做原地操作。`muon_update` 内部虽然也有原地操作，但其输入（`p.grad`、`zeros_like` 缓冲）不追踪梯度，所以第一处崩溃点是参数更新行。

**练习 2**：closure 里通常要做什么？muon.py 的 `step()` 会替你调用 backward 吗？

> **答案**：按约定「重新前向计算 loss 并返回它」；标准写法是在 closure 内 `zero_grad()` → 前向 → `backward()` → `return loss`。不会——muon.py 只调用 `closure()` 一次并保存返回值，梯度计算完全是 closure 的职责。

**练习 3**：`closure()` 为什么要包在 `with torch.enable_grad():` 里？

> **答案**：`step()` 整体处于 `no_grad` 上下文，而 closure 需要重新构建计算图并求导；`enable_grad` 在局部重新打开求导。外层防的是「更新被追踪」，内层保的是「loss 可求导」，两者目的不同。

### 4.5 原地更新模式：p.mul_ 与 p.add_ 两行施加更新

#### 4.5.1 概念说明

muon.py 全部六处 per-parameter 骨架的结尾都是**同样的两行**：

```python
p.mul_(1 - group["lr"] * group["weight_decay"])
p.add_(update, alpha=-group["lr"])
```

数学上就是一步「先收缩、再前进」：

\[
p \leftarrow p\,\bigl(1 - \mathrm{lr}\cdot\mathrm{wd}\bigr) - \mathrm{lr}\cdot u
\]

其中 \(u\) 是 `muon_update` / `adam_update` 算出的更新方向。这正是 **AdamW 式解耦权重衰减**的展开：

\[
p - \mathrm{lr}\cdot(\mathrm{wd}\cdot p + u) \;=\; p\,(1-\mathrm{lr}\cdot\mathrm{wd}) - \mathrm{lr}\cdot u
\]

对比 `torch.optim.SGD(weight_decay=wd)` 的**耦合式**：它把 \(\mathrm{wd}\cdot p\) 加进梯度（\(g \leftarrow g + \mathrm{wd}\,p\)）再走动量——衰减强度会被动量放大 \(\tfrac{1}{1-\beta}\) 倍。解耦式没有这个耦合，这就是它叫 decoupled 的原因。

三个写法细节：

- **`p.add_(update, alpha=-lr)`**：`alpha` 是 `Tensor.add` 的缩放参数，等价于 `p += (-lr) * update`，但少建一个中间张量、少一次 kernel 启动；
- **`update.reshape(p.shape)`**：卷积核在 `muon_update` 里被 `view` 成了 2D（u2-l3），施加前要变回原形状；adam 分支的更新不改变形状，所以不需要 reshape；
- **下划线 = 原地**：这正是 4.4 节必须 `no_grad` 的原因；若写成 `p = p * (1 - lr*wd) - lr*update`，只是把局部变量名 `p` 重新绑定到新张量，优化器外的参数对象纹丝不动——loss 永远不会降，是自定义优化器的第一大坑。

#### 4.5.2 核心流程

```
update 就绪（muon_update 或 adam_update 的返回值）
        │
        ▼
p.mul_(1 - lr·wd)        ← 第一行：解耦权重衰减（权重向 0 收缩）
        │
        ▼
p.add_(update, alpha=-lr)← 第二行：沿更新方向前进一步
```

#### 4.5.3 源码精读

六处出现的完整清单（两行完全一致，仅 muon 分支多一个 `reshape`）：

| 位置 | 所属 |
|---|---|
| [muon.py:L92-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L92-L93) | `Muon.step`（分布式 muon 分支） |
| [muon.py:L124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125) | `SingleDeviceMuon.step` |
| [muon.py:L206-L207](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L206-L207) | `MuonWithAuxAdam.step` muon 分支 |
| [muon.py:L222-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L222-L223) | `MuonWithAuxAdam.step` adam 分支 |
| [muon.py:L268-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L268-L269) | `SingleDeviceMuonWithAuxAdam.step` muon 分支 |
| [muon.py:L283-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L283-L284) | `SingleDeviceMuonWithAuxAdam.step` adam 分支 |

以 `SingleDeviceMuon.step` 为例：

[muon.py:L124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125) —— 先按 `1 - lr·wd` 收缩权重（解耦衰减），再以 `-lr` 为系数把更新加到权重上；`reshape` 把卷积核情形下被展平的更新变回参数原形状。

```python
p.mul_(1 - group["lr"] * group["weight_decay"])
p.add_(update.reshape(p.shape), alpha=-group["lr"])
```

adam 分支（如 [muon.py:L222-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L222-L223)）的两行结构相同，只是 `adam_update` 返回的 `update` 与 `p` 形状一致，无需 reshape。也请注意：Muon 组和 Adam 组共用这两行，说明「解耦衰减」对两类参数一视同仁。

#### 4.5.4 代码实践

**实践目标**：数值验证这两行在数学上恰好等于公式 \(p \leftarrow p(1-\mathrm{lr}\cdot\mathrm{wd}) - \mathrm{lr}\cdot u\)。

**操作步骤**：

```python
# 示例代码：验证两行更新的数学语义（复用 u2-l3 的 muon_update 知识）
import torch
import muon

p = torch.nn.Parameter(torch.randn(8, 8))
opt = muon.SingleDeviceMuon([p], lr=0.1, weight_decay=0.01, momentum=0.95)

loss = (p ** 2).sum()
loss.backward()
p0 = p.detach().clone()   # step 前的权重
g0 = p.grad.clone()       # muon_update 会原地改写 p.grad（u2-l3），先留档

opt.step()

# 手动复算第一次 step 的 update：初始动量缓冲为全零
momentum = torch.zeros_like(p)
update = muon.muon_update(g0, momentum, beta=0.95)
expected = p0 * (1 - 0.1 * 0.01) - 0.1 * update
print(torch.allclose(p.detach(), expected, atol=1e-6))
```

**需要观察的现象**：`opt.step()` 之后参数确实变了；手动复算的 `expected` 与 `step()` 之后的 `p` 逐元素一致。

**预期结果**：打印 `True`。两边走的是同一条确定性计算路径（同样的 `muon_update` 调用、同样的零初始动量），误差只可能来自浮点舍入，通常为 0。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：把 `p.add_(update, alpha=-lr)` 改写成 `p = p - lr * update`，训练会发生什么？

> **答案**：`p` 只是被重新绑定到一个新张量，`param_groups` 里存的原参数对象（以及模型里的权重）没有任何改变，loss 不会下降。参数更新必须用带下划线的原地操作。

**练习 2**：这两行实现的是耦合还是解耦权重衰减？与 `torch.optim.AdamW` 的公式是什么关系？

> **答案**：解耦（AdamW 式）。AdamW 的 \(p \leftarrow p - \mathrm{lr}(\mathrm{wd}\cdot p + u)\) 与这两行代数上完全等价；而 `torch.optim.SGD` 的耦合式会把衰减项混入梯度、再被动量放大。

**练习 3**：为什么 muon 分支写 `p.add_(update.reshape(p.shape), ...)` 而 adam 分支不需要 `reshape`？

> **答案**：`muon_update` 对 4D 卷积核梯度做了 `view(len(update), -1)` 展平（u2-l3），返回的是 2D，必须变回参数原形状才能相加；`adam_update` 对梯度做逐元素运算，返回形状与 `p` 相同。

## 5. 综合实践：从零实现 MySGDMomentum 并与 torch.optim.SGD 对拍

这是本讲的收官任务：把 4.1–4.5 的全部约定用一遍——模仿 muon.py 的风格，从零写一个带动量的 SGD 优化器，然后与 PyTorch 官方实现跑同一个任务，验证 loss 曲线重合。

### 5.1 实践目标

1. 写出一个结构、风格都与 muon.py 一致的 `torch.optim.Optimizer` 子类：`defaults` + `super().__init__`、`@torch.no_grad()`、closure 协议、`self.state` 惰性初始化、两行原地更新。
2. 用「对拍」（同初始权重、同数据、同超参）证明你的实现与 `torch.optim.SGD(momentum=0.95)` 数值一致。
3. 亲手验证 u2-l3 的结论：EMA 动量与求和型动量只差常数 \((1-\beta)\)。

### 5.2 操作步骤

**第一步**：实现优化器（保存为 `my_sgd_momentum.py`）。

```python
# 示例代码：my_sgd_momentum.py —— 模仿 muon.py 风格的 SGD + 动量
import torch

class MySGDMomentum(torch.optim.Optimizer):
    def __init__(self, params, lr=0.1, momentum=0.95, weight_decay=0):
        defaults = dict(lr=lr, momentum=momentum, weight_decay=weight_decay)
        super().__init__(params, defaults)

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                state = self.state[p]
                if len(state) == 0:
                    state["momentum_buffer"] = torch.zeros_like(p)
                buf = state["momentum_buffer"]
                # 求和型动量：v ← μ·v + g，与 torch.optim.SGD(dampening=0) 的记账一致
                buf.mul_(group["momentum"]).add_(p.grad)
                p.mul_(1 - group["lr"] * group["weight_decay"])
                p.add_(buf, alpha=-group["lr"])
        return loss
```

对照检查五处约定：构造函数打包 `defaults` 并交给父类（4.1）；`step()` 读 `group[...]`（4.2）；惰性初始化 `momentum_buffer`（4.3）；`@torch.no_grad()` + closure 8 行（4.4）；两行原地更新（4.5）。与 muon.py 唯一的实质差别是：用 `buf.mul_(μ).add_(grad)`（求和型）替代了 `lerp_`（EMA 型），且梯度缺失时 `continue`（跳过）而非填零——后者是 Muon 为分布式同步所做的特殊选择（u2-l5 详讲）。

**第二步**：对拍实验（保存为 `compare.py`，与上例同目录运行）。

```python
# 示例代码：compare.py —— MySGDMomentum vs torch.optim.SGD 对拍
import torch
from my_sgd_momentum import MySGDMomentum

torch.manual_seed(0)
d_in, d_out, n = 32, 16, 512
x = torch.randn(n, d_in)
W_true = torch.randn(d_in, d_out)
y = x @ W_true
loss_fn = torch.nn.MSELoss()

def train(make_opt, steps=100):
    torch.manual_seed(42)                      # 两个实验用完全相同的初始权重
    model = torch.nn.Linear(d_in, d_out, bias=False)
    opt = make_opt(model.parameters())
    losses = []
    for _ in range(steps):
        loss = loss_fn(model(x), y)
        opt.zero_grad()
        loss.backward()
        opt.step()
        losses.append(loss.item())
    return losses

mine = train(lambda ps: MySGDMomentum(ps, lr=0.1, momentum=0.95))
ref = train(lambda ps: torch.optim.SGD(ps, lr=0.1, momentum=0.95))

print(f"max |Δloss| = {max(abs(a - b) for a, b in zip(mine, ref)):.3e}")
print(f"final: mine={mine[-1]:.6f}  sgd={ref[-1]:.6f}  初始={mine[0]:.6f}")
```

### 5.3 需要观察的现象

- loss 从初始量级（\(\|W_{\text{init}} - W_{\text{true}}\|^2\) 级别）单调下降到接近 0；
- 两条曲线逐点几乎不可区分。

### 5.4 预期结果

`max |Δloss|` 在 1e-6 量级或干脆为 0：两个优化器的算术路径完全一致——`SGD(weight_decay=0, dampening=0)` 的缓冲更新就是 \(v \leftarrow \mu v + g\)（首步 \(v_1 = g_1\)，与零初始化的求和型一致），施加更新同样是 `p.add_(d_p, alpha=-lr)`。浮点结合顺序一致时应当逐位相同。待本地验证。

### 5.5 延伸实验：看见常数 \((1-\beta)\)

把 `buf.mul_(group["momentum"]).add_(p.grad)` 换成 EMA 写法 `buf.lerp_(p.grad, 1 - group["momentum"])`（即 muon.py 的记账方式），其余不动再跑一次：

- **现象**：曲线仍下降，但**明显更慢**——因为两种缓冲始终满足 \(\text{buf}_{\text{EMA}} = (1-\beta)\cdot\text{buf}_{\text{sum}}\)，等效学习率缩小了 \(1-\beta = 0.05\) 倍；
- **进一步验证**：把 lr 从 0.1 放大到 \(0.1/0.05 = 2.0\)，曲线应与参考曲线基本重合。这正是 u2-l3「常数 \((1-\beta)\) 会被正交化抹掉」结论的实验版——对 SGD 它不会被抹掉，所以肉眼可见。待本地验证。

## 6. 本讲小结

- **自定义优化器的最小骨架**：`__init__` 里组装 `defaults` 并调用 `super().__init__(params, defaults)`，再实现一个 `step()`；父类接管三大数据结构（[muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69)、[L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105)）。
- **param_groups 是超参作用域**：`defaults` 只补缺、不覆盖显式键、不删额外键（`use_muon` 靠此存活）；`step()` 读 `group["lr"]`，所以训练循环里直接改组内 lr 即可热更新（[muon.py:L165-L182](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L165-L182)）。
- **self.state 是惰性的逐参数记忆**：`defaultdict(dict)`、键为参数对象、`if len(state) == 0` 是首次访问信号；muon 分支存 `momentum_buffer`，adam 分支存 `exp_avg`/`exp_avg_sq`/`step`（[muon.py:L120-L122](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L120-L122)、[L214-L219](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L214-L219)）。
- **step() 的两个框架约定**：`@torch.no_grad()`（否则叶子上原地操作直接报错）与 `closure`（重估 loss、自行 backward、在 `enable_grad` 中被调用一次）（[muon.py:L107-L113](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L107-L113)，同样 8 行出现 4 次）。
- **更新施加 = 两行原地操作** `p.mul_(1-lr·wd); p.add_(update, alpha=-lr)`，即 AdamW 式解耦衰减的展开，在文件中出现六次（见 4.5 节表格）。

## 7. 下一步学习建议

- **下一讲 u2-l5** 将逐行精读 `SingleDeviceMuon.step`（[muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127)）。有了本讲的框架约定和 u2-l3 的 `muon_update`，那 29 行将不再有任何秘密；重点会落在 `p.grad` 缺失时**填零而非跳过**的设计（本讲综合实践中你已见过 `continue` 写法的对照）。
- **强烈建议对照阅读 PyTorch 官方源码**（非本仓库）：[torch/optim/optimizer.py](https://github.com/pytorch/pytorch/blob/main/torch/optim/optimizer.py) 中的 `Optimizer.__init__` / `add_param_group` / `state_dict`（验证本讲 4.1–4.3 描述的框架行为），以及 [torch/optim/sgd.py](https://github.com/pytorch/pytorch/blob/main/torch/optim/sgd.py) 中官方 `step()` 的写法（与你的 `MySGDMomentum` 对照）。
- 之后再进入第三单元：u3-l1 精读 `adam_update`（六行手写 AdamW，`state["step"]` 的用途在那里揭晓），u3-l2 深入 `use_muon` 参数组设计——那一讲会把本讲 4.2 节的分组机制用到真实模型上。
