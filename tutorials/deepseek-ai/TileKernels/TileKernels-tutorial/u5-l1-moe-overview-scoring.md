# MoE 路由概览与打分函数

## 1. 本讲目标

本讲是 MoE（Mixture of Experts，专家混合）路由单元的第一篇，目标是建立「MoE 路由要解决什么问题」的整体心智模型，并把其中最基础、也是后面所有路由 kernel 都要用到的一步——**打分函数（scoring function）**——讲透。

学完后你应该能够：

1. 用一句话说清 MoE 路由问题：给定一批 token，要为每个 token 选出几个「专家」，并算出每个被选专家的权重。
2. 写出三种打分函数 `sigmoid` / `softmax` / `sqrtsoftplus` 的数学定义，并说清它们在归一化范围、是否竞争上的区别。
3. 读懂 `ScoringFunc` 枚举（含 `from_str` / `__str__` 的字符串往返）和 `@T.macro softplus` 这个数值稳定宏的作用。
4. 读懂 `tile_kernels/torch/topk.py` 里 PyTorch 参考实现是如何调用打分函数的，尤其抓住 **SOFTMAX 把 bias 加在原始 logits 上、而其它打分把 bias 加在打完分之后**这一关键差异。
5. 理解打分结果如何流入分组路由（`moe/common.py` 的 `get_topk_group_idx`）。

本讲**不**深入 top-k 选取的 warp 原语与并列处理细节（那是 u5-l2、u5-l3 的内容），也**不**展开分组计数的完整推导（u5-l3），只在「数据流」层面把打分和分组连起来。

## 2. 前置知识

阅读本讲前，建议你已掌握（对应前置讲义 u1-l3、u2-l3）：

- **TileLang 算子骨架**（u2-l1）：`@tilelang.jit` 构造器 + `@T.prim_func` 内核 + `T.dynamic` 运行时符号的两层结构。本讲的打分代码是「填进这个骨架里的肉」。
- **`@T.macro` 与规约原语**（u2-l3）：`@T.macro` 是可被 prim_func 调用的「代码片段宏」，会内联展开；`T.reduce_max`、warp shuffle 等规约原语会在 SOFTMAX 路径里用到。
- **包结构与 torch 参考层**（u1-l3）：`tile_kernels/torch/` 存放纯 PyTorch 参考实现，用于和底层 GPU kernel 对拍。本讲的打分函数在底层 kernel 和 torch 参考里各实现一次，二者必须数值等价。

几个 MoE 领域的通俗概念：

- **token**：模型处理的基本单位，可理解为「一个序列位置上的向量」。一次前向有 `num_tokens` 个 token。
- **expert（专家）**：MoE 里若干个并行的子网络，每个 token 只被其中少数几个专家处理。专家数 `num_routed_experts` 通常远大于每 token 选中的专家数。
- **logits / 打分**：模型先给每个 token 对每个专家打出一个原始分数 `logits[token, expert]`（一个 `(num_tokens, num_routed_experts)` 的 float32 矩阵），再经过打分函数变换、选出 top-k。
- **分组（group）**：把全部专家等分成 `num_groups` 组，先在「组」这一层做粗筛（选 `num_topk_groups` 个最好的组），再在组内细选 top-k，是一种常见的加速 + 平衡手段。

## 3. 本讲源码地图

本讲涉及三个文件，职责清晰、各管一层：

| 文件 | 作用 | 在本讲的定位 |
| --- | --- | --- |
| `tile_kernels/moe/scoring.py` | 定义 `ScoringFunc` 枚举（4 种打分）和 `@T.macro softplus` 数值稳定宏 | **核心**：打分函数的「身份证」与底层 kernel 复用的宏 |
| `tile_kernels/moe/common.py` | 定义 `@T.macro get_topk_group_idx`，做组级粗筛 | 打分结果如何流入分组路由 |
| `tile_kernels/torch/topk.py` | `top2_sum_gate` 等 PyTorch 参考实现 | 打分函数的「黄金标准」，与底层 kernel 对拍 |

此外会引用两个「消费打分」的文件作为佐证：

- `tile_kernels/moe/top2_sum_gate_kernel.py`：把 `scoring_type`（即枚举的整数值）分派到不同打分分支的真实 kernel。
- `tile_kernels/moe/topk_gate_kernel.py`：直接接收**已经打好分**的 `scores`，只做 top-k 选取，反衬出「打分」与「选取」是解耦的两步。

## 4. 核心概念与源码讲解

### 4.1 MoE 路由整体问题与数据流

#### 4.1.1 概念说明

MoE（专家混合）的核心想法是：与其让一个巨大的前馈网络处理所有 token，不如准备很多个「专家」子网络，每个 token 只挑少数几个专家去算。这样能在「总参数量」很大的同时，把「单 token 的实际计算量」压下来。

要做到「挑」，就必须回答两个问题：

1. **路由（routing）**：每个 token 该送给哪几个专家？→ 输出 `topk_idx`，形状 `(num_tokens, num_topk)`，每个元素是一个专家下标。
2. **加权（weighting）**：被选中的几个专家的输出该怎么混合？→ 输出 `topk_weights`，形状与 `topk_idx` 相同，每个元素是一个权重。

整套流程的「入口原料」是模型给每个 token 对每个专家打的**原始分 logits**。从 logits 到最终的 `(topk_idx, topk_weights)`，要经过一连串步骤。本讲只聚焦最前面的「打分」这一步，但要先把整条链路看清，才知道打分处在什么位置。

#### 4.1.2 核心流程

`tile_kernels/torch/topk.py` 里的 `top2_sum_gate` 是这条链路的**完整 PyTorch 参考**，它的步骤就是 MoE 路由的标准流程（下面的编号对应源码里的注释）：

```text
logits (num_tokens, num_routed_experts)            # 原始分
   │
   1. 打分函数 scoring(logits) ─────────────────►  scores_wo_bias   # sigmoid/softmax/sqrtsoftplus
   │
   2. 加 bias 得到「用于排序的分」  ──────────────►  scores_biased    # 注意 SOFTMAX 的 bias 位置不同
   │
   3. 分组粗筛（可选）  ─────────────────────────►  只保留 topk_groups 个组内的专家
   │
   4. 在筛后专家里选 top-k  ─────────────────────►  topk_idx_local
   │
   5. 用 scores_wo_bias 在选中下标上 gather ───►  topk_score_local
   │
   7. 归一化 topk_sum ──────────────────────────►  topk_weights
   │
   8/9/10. 追加共享专家、logical→physical 映射、EP/TP 掩码
   ▼
topk_idx, topk_weights
```

一句话总结打分的位置：**打分是把「原始 logits」变成「可比较、可归一化的 scores」的第一步变换**，它发生在加 bias、分组、选 top-k 之前。打分函数选得好不好，直接决定路由是否合理、训练是否稳定。

#### 4.1.3 源码精读

打分在参考实现里的位置很集中。注意 **第 1 步**和**第 2 步**是分开的两行：

[tile_kernels/torch/topk.py:107-115](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L107-L115) 这段代码先把 logits 喂给打分函数得到 `scores_wo_bias`，再用一行三元表达式算出 `scores_biased`。

关键在于第 115 行那行三元表达式：

```python
scores_biased = (logits_a + bias_b) if scoring == ScoringFunc.SOFTMAX else (scores_wo_bias + bias_b)
```

这是全篇最容易被忽略、却最关键的细节：

- 对 **sigmoid / sqrtsoftplus**：bias 加在**打完分之后**，即 `(scores_wo_bias + bias)`。
- 对 **softmax**：bias 加在**原始 logits 上**，即 `(logits + bias)`，因为 softmax 的归一化是对「整体」做的，bias 必须先进入指数里才合理。

`bias` 是每个专家一个的可学习偏置（形状 `(num_routed_experts,)`），用来在排序时人为抬高/压低某些专家的优先级。注意第 5 步（gather 权重）用的是 `scores_wo_bias`，即**权重不带 bias**；bias 只影响「谁被选中」，不影响「选中后的权重大小」。这个设计我们会在 4.3 再细讲。

#### 4.1.4 代码实践

> 实践目标：在没有 GPU 的纯 CPU 环境下，亲手把 `logits → scores_wo_bias` 这一步跑一遍，体会三种打分函数的数值范围差异。

操作步骤（示例代码，可在任意装了 PyTorch 的环境运行）：

```python
# 示例代码：对比三种打分函数的数值范围（CPU 即可运行）
import torch
import torch.nn.functional as F

torch.manual_seed(0)
logits = torch.randn(4, 8)          # (num_tokens, num_experts) 的迷你版

sigmoid_score      = torch.sigmoid(logits)
softmax_score      = torch.softmax(logits, dim=-1)
sqrtsoftplus_score = F.softplus(logits).sqrt()

print("sigmoid      范围:", sigmoid_score.min().item(),      sigmoid_score.max().item())
print("softmax      范围:", softmax_score.min().item(),      softmax_score.max().item())
print("sqrtsoftplus 范围:", sqrtsoftplus_score.min().item(), sqrtsoftplus_score.max().item())
```

需要观察的现象：

- `sigmoid` 的每个元素都在 `(0, 1)`，且**彼此独立**（一个专家分高不会压低另一个）。
- `softmax` 的每行**和为 1**，元素间**互相竞争**（一个专家分高，其它专家的 softmax 值会被压低）。
- `sqrtsoftplus` 的每个元素都 `≥ 0`，但**没有上界**（随 logits 增大而增大，只是开方压低了增长速度）。

预期结果：三者的数值范围截然不同；同一行里 `softmax` 的最大值通常明显小于 `sigmoid` 的最大值（因为分母是全行指数和）。

#### 4.1.5 小练习与答案

**练习 1**：MoE 路由要输出两个张量，分别是什么？各自形状里哪个维度等于「每 token 选中的专家数」？

> **答案**：输出 `topk_idx`（被选中的专家下标）和 `topk_weights`（对应权重）。两者形状都是 `(num_tokens, num_topk + num_shared_experts)`，第二个维度等于每 token 路由出去的专家数。

**练习 2**：打分函数发生在加 bias 之前还是之后？权重计算用的是带 bias 的分还是不带 bias 的分？

> **答案**：打分发生在加 bias 之前（bias 加在 `scores_wo_bias` 或 logits 上）。计算最终权重时用的是 `scores_wo_bias`（不带 bias）——参考实现第 5 步 gather 与第 7 步归一化都基于 `scores_wo_bias`，bias 只参与「排序选谁」，不参与「选中后权重多大」。

---

### 4.2 打分函数：ScoringFunc 枚举与 softplus 宏

#### 4.2.1 概念说明

打分函数接收原始 logits，输出一个**非负、可比**的分数。TileKernels 内置三种实际使用的打分（外加一个 `IDENTITY` 占位），全部收纳在 `tile_kernels/moe/scoring.py` 的小文件里。

三种打分的数学定义（记第 `i` 个专家的原始分为 \(z_i\)，打分输出为 \(s_i\)）：

1. **Sigmoid（逐元素，互不竞争）**

\[
s_i = \sigma(z_i) = \frac{1}{1+e^{-z_i}}
\]

每个专家独立映射到 \((0,1)\)，一个专家分高不会让别的专家变低。适合「专家之间可以并列选中」的路由策略。

2. **Softmax（全行竞争，和为 1）**

\[
s_i = \frac{e^{z_i}}{\sum_{j} e^{z_j}}
\]

整行归一化，元素互相竞争。这正是上节强调的「bias 必须先进 logits」的原因——bias 要进入分子分母的指数里。

3. **SqrtSoftplus（逐元素，无上界但被开方压平）**

\[
s_i = \sqrt{\mathrm{softplus}(z_i)} = \sqrt{\ln(1+e^{z_i})}
\]

`softplus` 是 `ReLU` 的光滑近似，恒非负；再开方进一步压缩大值。同样逐元素、不竞争，但相比 sigmoid 没有 `1` 的上界，对很大的正 logits 更敏感。

注意 `sqrtsoftplus` 名字里带 `softplus`，在底层 kernel 里它正是一个 `@T.macro softplus` 再套一层 `T.sqrt`。

#### 4.2.2 核心流程

`ScoringFunc` 用一个 `IntEnum` 统一管理这几种打分。它的设计有两个要点：

- **整数值**会作为**编译期参数**烤进 kernel：wrapper 调 `ScoringFunc.from_str(scoring_func).value` 拿到 `0/1/2/3`，传给 kernel 构造器的 `scoring_type` 形参，kernel 里再用 `if scoring_type == 0 ...` 分派。不同打分各自特化出一份编译产物。
- **字符串往返**：用户接口用人类可读的字符串 `'sigmoid'/'sqrtsoftplus'/'softmax'`（见 wrapper 形参 `scoring_func: str`），靠 `__str__`（枚举名转小写）和 `from_str`（字符串转大写再取下标）在字符串与枚举之间互转。

打分函数本身在 kernel 里是「就地变换 scores」，但 `softplus` 涉及 `exp`，直接算 `log(1+e^x)` 在 `x` 很大时会数值溢出（`e^x` 爆炸），所以需要数值稳定版本——这就是 `softplus` 宏存在的理由。

#### 4.2.3 源码精读

先看枚举本体：

[tile_kernels/moe/scoring.py:5-19](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/scoring.py#L5-L19) 定义了 `ScoringFunc(IntEnum)`，含 `SIGMOID=0 / SQRTSOFTPLUS=1 / SOFTMAX=2 / IDENTITY=3`，以及 `__str__`（返回小写名）和 `from_str`（按大写名查找，找不到抛 `ValueError`）。

四点要点：

```python
class ScoringFunc(IntEnum):
    SIGMOID = 0
    SQRTSOFTPLUS = 1
    SOFTMAX = 2
    IDENTITY = 3

    def __str__(self):
        return self.name.lower()          # str(ScoringFunc.SIGMOID) -> 'sigmoid'

    @classmethod
    def from_str(cls, label: str):
        return cls[label.upper()]         # from_str('sigmoid') -> ScoringFunc.SIGMOID
```

- `__str__` 让 `str(ScoringFunc.SQRTSOFTPLUS) == 'sqrtsoftplus'`，这正是测试和 wrapper 传参时用的字符串。
- `from_str` 用 `cls[label.upper()]`，即把 `'sigmoid'` 转成 `'SIGMOID'` 再取枚举成员；非法值抛 `ValueError`。
- `IDENTITY=3` 表示「不打分、原样使用」，但 `top2_sum_gate` 明确**拒绝**它（见 4.3），它更多是占位 / 留作扩展。

再看数值稳定的 `softplus` 宏：

[tile_kernels/moe/scoring.py:22-25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/scoring.py#L22-L25) 用 `threshold=20.0` 做分段：`x>20` 时直接返回 `x`，否则返回 `log1p(exp(x))`。

```python
@T.macro
def softplus(x: T.Ref):
    threshold = 20.0
    return T.if_then_else(x > threshold, x, T.log1p(T.exp(x)))
```

为什么需要这个分段？因为当 \(x\) 很大时，\(e^x\) 会溢出：

\[
\mathrm{softplus}(x) = \ln(1+e^x)
\]

当 \(x \gg 0\)，\(1+e^x \approx e^x\)，故 \(\mathrm{softplus}(x) \approx x\)。所以 `x > 20` 时直接用 `x` 近似（相对误差 < 1e-9）；`x ≤ 20` 时 `exp(20) ≈ 4.85e8` 还在 float32 安全范围内，用 `log1p(exp(x))` 精确计算。`log1p` 而非 `log(1+...)` 是为了在 `x` 很负、`exp(x)` 接近 0 时避免 `1+` 吞掉精度。

`@T.macro` 的意义（承接 u2-l3）：它是一段**会被内联展开**的代码片段，在 prim_func 里像函数一样调用 `softplus(x)`，编译后展开成对应的 CUDA 语句。底层 kernel 正是这样用它的：

[tile_kernels/moe/top2_sum_gate_kernel.py:173-183](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L173-L183) 这段代码在打分主循环里按 `scoring_type` 分派：`0`→`T.sigmoid`，`1`→`T.sqrt(softplus(...))`，`2`→除以 softmax 分母，`3`→`pass`（IDENTITY 原样），并断言不会有其它分支。

注意第 176 行 `scores_local[local_idx] = T.sqrt(softplus(scores_local[local_idx]))` 正是 `sqrtsoftplus` 的字面落地：先调用上面那个宏，再开方。这也解释了枚举名为何叫 `SQRTSOFTPLUS`。

#### 4.2.4 代码实践

> 实践目标：用纯 PyTorch 复现三种打分，验证与 `ScoringFunc` 枚举的字符串往返一致，并比较大 logits 下「朴素 softplus」与「数值稳定 softplus」的差异。

```python
# 示例代码：打分函数自实现 + 枚举往返 + 数值稳定性观察（CPU 即可）
import math
import torch
import torch.nn.functional as F
from tile_kernels.moe.scoring import ScoringFunc   # 需安装本包；否则可只跑打分部分

# (a) 字符串往返：确认 str() 与 from_str() 互为逆操作
for name in ['sigmoid', 'sqrtsoftplus', 'softmax']:
    enum = ScoringFunc.from_str(name)
    assert str(enum) == name, (name, str(enum))
    print(f"{name:14s} -> 枚举 {enum.name} (value={enum.value}) -> 字符串 {str(enum)}")

# (b) 三种打分自实现
logits = torch.randn(3, 6)
score_map = {
    ScoringFunc.SIGMOID:      torch.sigmoid(logits),
    ScoringFunc.SOFTMAX:      torch.softmax(logits, dim=-1),
    ScoringFunc.SQRTSOFTPLUS: F.softplus(logits).sqrt(),
}

# (c) 数值稳定 vs 朴素 softplus：构造一个大 logits
big = torch.tensor([30.0, 50.0, -50.0])
naive      = torch.log(1 + torch.exp(big))        # 朴素：exp(50) 会溢出成 inf
stable     = F.softplus(big)                       # torch 内部已做稳定处理
manual_stb = torch.where(big > 20, big, torch.log1p(torch.exp(big)))  # 复刻 scoring.py 的宏
print("朴素 softplus :", naive)        # 预期出现 inf
print("稳定 softplus :", stable)
print("手写稳定版    :", manual_stb)
```

需要观察的现象：

- 第 (a) 部分：三种名字都能 `from_str → 枚举 → str` 往返还原，且 `value` 分别是 `0/2/1`（softmax 是 2，注意不是按名字字母序）。
- 第 (c) 部分：朴素 `log(1+exp(50))` 会得到 `inf`（因为 `exp(50)` 溢出），而 `F.softplus` 与手写稳定版都给出约 `50`、约 `-5.2e-22` 这种合理值，**证明 `scoring.py` 里 `threshold=20` 的分段是必要的**。

预期结果：第 (c) 部分朴素版第 2 个元素为 `inf`，稳定版为 `50.0`。第 (a) 部分三行往返全部通过。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `softplus` 宏在 `x > 20` 时直接返回 `x` 而不是 `log1p(exp(x))`？

> **答案**：当 `x` 很大时 `exp(x)` 会溢出 float32 上限（`exp(20)≈4.85e8` 尚可，再大就有溢出风险），且此时 `log(1+e^x) ≈ x` 的相对误差极小（< 1e-9），所以用线性近似 `x` 既避免溢出又不损失精度。

**练习 2**：`ScoringFunc.from_str('Sigmoid')`、`from_str('SIGMOID')`、`from_str('sigmoid')` 三者结果是否相同？为什么？

> **答案**：相同。`from_str` 内部先 `label.upper()` 再查表，三种写法都会归一到 `'SIGMOID'`，返回 `ScoringFunc.SIGMOID`。这正是 wrapper 接收任意大小写字符串都能正常工作的原因。

**练习 3**：枚举里有个 `IDENTITY=3`，它会在 `top2_sum_gate` 里被使用吗？

> **答案**：不会。`top2_sum_gate` 的 wrapper 明确断言 `ScoringFunc.from_str(scoring_func) != ScoringFunc.IDENTITY`（见 4.3 源码），它更多是占位/留作其它算子扩展。

---

### 4.3 PyTorch 参考实现：打分如何落地，以及 SOFTMAX 的 bias 陷阱

#### 4.3.1 概念说明

底层 GPU kernel 必须有一个「黄金标准」来对拍——这就是 `tile_kernels/torch/topk.py` 里的纯 PyTorch 实现（承接 u1-l3 的「torch 参考层」职责）。理解参考实现有两层价值：

1. 它是读懂路由全流程的「说明书」：每一步都用高层 PyTorch 一行写清，比 kernel 好读。
2. 它暴露了「打分函数与 bias 如何配合」的**正确语义**，底层 kernel 必须逐位复刻这个语义。

本节聚焦打分相关的那几行，尤其那个最容易写错的 **SOFTMAX bias 位置**问题。

#### 4.3.2 核心流程

`top2_sum_gate` 里打分相关的流程是：

```text
1. scoring_func 字符串 → ScoringFunc 枚举
2. scores_wo_bias = 打分函数(logits)         # 第 1 步：纯打分
3. scores_biased  = bias 加到「正确位置」      # 第 2 步：bias 进入排序分
   - SOFTMAX:     scores_biased = logits + bias       # bias 进原始 logits
   - 其它:        scores_biased = scores_wo_bias + bias  # bias 进打分后
4. 后续用 scores_biased 排序选 top-k
5. 用 scores_wo_bias 在选中下标 gather 权重     # 权重不带 bias
```

「为什么 SOFTMAX 要把 bias 加在 logits 上」可以这样理解：softmax 是对全行做归一化（分母是所有专家的指数和）。如果你先 softmax 再加 bias，就破坏了「行内和为 1」的归一化意义；正确做法是让 bias 进入指数，即 softmax(logits + bias)。而 sigmoid / sqrtsoftplus 是逐元素的，加 bias 在打分前还是打分后只是数值不同、语义都自洽，项目选择了「打分后再加 bias」。

#### 4.3.3 源码精读

打分的分派与 bias 位置：

[tile_kernels/torch/topk.py:107-115](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L107-L115) 这段先用 `if/elif/else` 三分支算出 `scores_wo_bias`（SIGMOID → SQRTSOFTPLUS → SOFTMAX(else)），再用第 115 行的一行三元表达式决定 bias 加在哪。

```python
# 1. Apply scoring function
if scoring == ScoringFunc.SIGMOID:
    scores_wo_bias = torch.sigmoid(logits_a)
elif scoring == ScoringFunc.SQRTSOFTPLUS:
    scores_wo_bias = F.softplus(logits_a).sqrt()
else:  # SOFTMAX
    scores_wo_bias = torch.softmax(logits_a, dim=-1)

# 2. Biased scores for ranking (softmax uses raw logits + bias)
scores_biased = (logits_a + bias_b) if scoring == ScoringFunc.SOFTMAX else (scores_wo_bias + bias_b)
```

要点是：三个分支一一对应三种打分，而第 115 行的三元表达式才是 bias 位置的真正关键所在。

随后用 `scores_wo_bias`（不带 bias）来取权重：

[tile_kernels/torch/topk.py:158-160](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L158-L160) 这段做 top-sum 归一化，分母是选中专家的 `scores_wo_bias` 之和（夹一个 `1e-20` 防全零除零），分子也是 `scores_wo_bias`，乘以 `routed_scaling_factor`。

```python
topk_sum = topk_score_local.sum(dim=-1, keepdim=True).clamp(min=1e-20)
topk_weights_routed = topk_score_local / topk_sum * routed_scaling_factor
```

这印证了 4.1 的结论：**bias 只影响选谁，不影响权重大小**；权重来自不带 bias 的纯打分值。

wrapper 侧对 `IDENTITY` 的拒绝：

[tile_kernels/moe/top2_sum_gate_kernel.py:367](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L367) 这行断言 `ScoringFunc.from_str(scoring_func) != ScoringFunc.IDENTITY`，即 `top2_sum_gate` 不允许传 `'identity'`。

而 `scoring_type` 整数如何进入 kernel：

[tile_kernels/moe/top2_sum_gate_kernel.py:406-413](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L406-L413) wrapper 把 `ScoringFunc.from_str(scoring_func).value` 作为第一个编译期参数传给 `get_top2_sum_gate_kernel`，于是 `0/1/2` 各自特化出一份产物，kernel 内用 `scoring_type == 0/1/2` 分派（见 4.2.3 引用的 173-183 行）。

最后看一个反衬：「打分」与「选取」其实是解耦的。`topk_gate` 这个 kernel **根本不做打分**：

[tile_kernels/moe/topk_gate_kernel.py:20-24](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L20-L24) 它的 prim_func 形参直接叫 `scores`——也就是说调用方要**先自己打好分**再传进来，kernel 只负责选 top-k。这反过来说明：本讲的打分函数是一个**可独立复用**的步骤，`top2_sum_gate` 把「打分 + 分组 + 选取」融在一个 kernel 里，而 `topk_gate` 只做「选取」。

#### 4.3.4 代码实践

> 实践目标：亲手验证「SOFTMAX 的 bias 加在 logits 上」与「错误地加在 scores 上」会得到不同的 top-k 排序，体会这个陷阱的真实影响。

```python
# 示例代码：SOFTMAX bias 位置对排序的影响（CPU 即可）
import torch

torch.manual_seed(1)
logits = torch.randn(2, 6)
bias   = torch.randn(6) * 2.0   # 放大 bias 的影响
k      = 3

# 正确（项目语义）：softmax(logits + bias) 后取 top-k
correct = torch.softmax(logits + bias, dim=-1)
correct_topk = correct.topk(k, dim=-1).indices

# 错误：先 softmax 再加 bias
wrong   = torch.softmax(logits, dim=-1) + bias
wrong_topk = wrong.topk(k, dim=-1).indices

print("logits:\n", logits)
print("bias:\n", bias)
print("正确(softmax(logits+bias)) top-k:\n", correct_topk)
print("错误(softmax(logits)+bias)  top-k:\n", wrong_topk)
print("两份排序是否一致:", torch.equal(correct_topk, wrong_topk))
```

需要观察的现象：

- 两份 top-k 下标经常**不一致**（取决于随机种子与 bias 大小）。一旦不一致，就意味着 token 被路由到了不同的专家——这正是 bias 位置写错会引发的隐蔽 bug。
- 即使下标碰巧一致，对应的 `softmax` 数值（权重）也不同。

预期结果：`两份排序是否一致: False`（多数随机种子下）。这正是项目在 [topk.py:115](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L115) 用三元表达式专门区分 SOFTMAX 的原因。

> 说明：上面这段只用了 PyTorch，CPU 即可运行，无需 GPU。若要在真实 GPU kernel 上验证，可跑 `tests/moe/test_top2_sum_gate.py`（见 4.4.4），但那需要 SM90/SM100 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么权重计算用 `scores_wo_bias` 而不是 `scores_biased`？

> **答案**：bias 的作用是「在排序时调整专家优先级」（选谁），它不是模型对专家真实置信度的估计。如果把它带进权重，专家的真实贡献会被 bias 污染。所以「选谁」用带 bias 的分，「权重多大」用不带 bias 的纯打分值，二者解耦。

**练习 2**：`top2_sum_gate` 的 wrapper 形参 `scoring_func` 是什么类型？它在内部如何变成 kernel 可用的形式？

> **答案**：类型是 `str`（如 `'sigmoid'`）。wrapper 内部先 `ScoringFunc.from_str(scoring_func)` 得到枚举，断言非 IDENTITY，再取 `.value`（整数 0/1/2）作为编译期参数 `scoring_type` 传给 kernel 构造器，使每种打分特化出一份编译产物。

---

### 4.4 common 模块：分组打分 get_topk_group_idx

#### 4.4.1 概念说明

打分之后、选 top-k 之前，`top2_sum_gate` 还有一个可选的「分组粗筛」步骤（当 `num_groups != num_topk_groups` 时启用）：把 `num_routed_experts` 个专家等分成 `num_groups` 组，先选出最好的 `num_topk_groups` 个组，再只在组内细选。这一步的算法在 `tile_kernels/moe/common.py` 的 `get_topk_group_idx` 宏里。

> 本讲**只到「打分如何进入分组」这一层**，即分组用的是「组内 top-2 打分之和」。warp shuffle 的完整计数逻辑、稳定排序保证留到 u5-l3 详讲。

「组内 top-2 之和」是一种衡量「这一组整体有多好」的指标：对每个组，取该组里打分最高的 2 个专家的分数求和，和越大的组越值得保留。注意这里参与求和的「打分」——参考实现里 SOFTMAX 用的是 `logits+bias`、其它用 `scores_wo_bias+bias`（即 `scores_biased`），与 4.3 一致。

#### 4.4.2 核心流程

`get_topk_group_idx` 宏在一个 warp（32 线程）内处理若干 token，每个 lane 负责一个组，三步走：

```text
1. 每个组内求 top-2 打分之和  → topk_sum_var（每 lane 一个）
2. 用 warp shuffle 数「比我大的组有几个」→ count_var
3. 按 count_var 把自己的组号写到对应位置 → topk_group_idx_shared
```

第 2 步是关键：每个 lane 通过 `shfl_sync` 拿到其它 lane 的 `topk_sum_var`，数一下「比我大的组有几个」（并列时用下标 tie-break 保证稳定），这个计数就是自己应当被写到的输出位置。这是一种**全互联比较 + 计数排序**，天然稳定。

#### 4.4.3 源码精读

组内 top-2 之和：

[tile_kernels/moe/common.py:25-39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L25-L39) 这段在 `lane_idx < num_groups` 时，让每个 lane 扫描自己负责的组内所有专家，维护 `top1_var/top2_var` 两个变量，得到组内最大的两个打分；第 39 行 `T.Select(num_topk_sum == 1, top1_var, top1_var + top2_var)` 求出「top-2 之和」（`num_topk_sum` 通常为 2）。

```python
if lane_idx < num_groups:
    ...
    for i in T.unroll(num_vec_experts_per_group):
        for j in T.vectorized(num_vectorize_for_grouped_expert):
            ...
            if scores_vec_local[j] > top1_var:
                top2_var = top1_var
                top1_var = scores_vec_local[j]
            elif scores_vec_local[j] > top2_var:
                top2_var = scores_vec_local[j]
    topk_sum_var = T.Select(num_topk_sum == 1, top1_var, top1_var + top2_var)
```

跨组计数（warp shuffle）：

[tile_kernels/moe/common.py:42-45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L42-L45) 每个 lane 用 `shfl_sync` 把自己的 `topk_sum_var` 广播给全 warp，并统计「比我大的组有几个」；并列（`==`）时用 `i < lane_idx` 做 tie-break，保证稳定。

```python
for i in T.unroll(num_groups):
    other_top2_sum = T.shfl_sync(topk_sum_var, i)
    if other_top2_sum > topk_sum_var or (other_top2_sum == topk_sum_var and i < lane_idx):
        count_var += 1
```

按计数写入组号：

[tile_kernels/moe/common.py:48-52](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L48-L52) 只有排名在前 `num_topk_groups` 的 lane 才把自己的 `lane_idx`（即组号）写到 `topk_group_idx_shared`，最后 `sync_warp` 确保所有线程写完。

```python
if count_var < num_topk_groups:
    topk_group_idx_shared[token_idx, count_var] = lane_idx
T.sync_warp()
```

至此，打分（4.2）→ 加 bias 后的 `scores_biased`（4.3）→ 进入 `get_topk_group_idx` 算出最好的几个组（本节）→ 再在组内细选 top-k（u5-l2）这条链路就接上了。打分函数是这一切的起点。

#### 4.4.4 代码实践

> 实践目标：跟踪一个真实测试如何把「打分 + 分组」串起来对拍，并理解测试为何要刻意拉开打分差距。

操作步骤（源码阅读型实践，无需运行）：

1. 打开 [tests/moe/test_top2_sum_gate.py:80-88](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_top2_sum_gate.py#L80-L88)，找到 `get_scores` 内联参考。它对 `SIGMOID/SOFTMAX/SQRTSOFTPLUS` 分别用 `logits.sigmoid() / logits.softmax(dim=-1) / F.softplus(logits).sqrt()`——和 4.2 的数学定义一字不差。
2. 看构造输入的循环 [tests/moe/test_top2_sum_gate.py:90-120](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_top2_sum_gate.py#L90-L120)，注释解释了为什么要反复重抽数据：底层 kernel 用低精度运算，**必须让第 k 大与第 k+1 大的打分有足够差距**（阈值 `2e-6` / `1e-6`），否则并列会让对拍失败。
3. 看对拍断言 [tests/moe/test_top2_sum_gate.py:206-214](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_top2_sum_gate.py#L206-L214)：底层 kernel 与 torch 参考的 `topk_idx` 用 `assert_equal`（位精确），`topk_weights` 用 `torch.allclose`（浮点近似）。

需要观察的现象：

- 测试在构造数据时**主动避开并列**，这反过来说明打分函数的输出会被进一步用于排序，而排序对并列非常敏感——这正是后面 u5-l2 要专门讲「稳定处理并列」的动机。
- `scoring_func` 在测试里通过 `for scoring_func in ScoringFunc` 枚举遍历（跳过 IDENTITY），覆盖三种打分。

预期结果（若在 GPU 上运行 `pytest tests/moe/test_top2_sum_gate.py`）：所有配置对拍通过。**待本地验证**（需要 SM90/SM100 GPU 与 CUDA 13.1+）。

> 若想只跑 CPU 部分验证打分本身，可复用 4.2.4 的脚本，确认 `get_scores` 的三种实现与你的自实现一致——这部分 CPU 即可。

#### 4.4.5 小练习与答案

**练习 1**：`get_topk_group_idx` 用「组内 top-2 之和」而非「组内所有专家之和」来衡量一个组的好坏，为什么？

> **答案**：因为最终每个 token 只会从每组里选有限个专家（top-k），「组里最差的几个专家」对路由决策没有贡献。用 top-2 之和能更准确地反映「这一组里能被选中的好专家有多强」，避免被组内的差专家稀释。`num_topk_sum` 是可配置的（通常为 2）。

**练习 2**：在并列（两个组的 top-2 之和相等）时，`get_topk_group_idx` 如何保证结果稳定可复现？

> **答案**：通过 `i < lane_idx` 这个 tie-break 条件——当两个组的和相等时，规定「下标更小的组排在前面」（count 只在 `i < lane_idx` 时才加）。这样无论运行多少次，相同输入都得到相同的组序，结果可复现。

**练习 3**：`get_topk_group_idx` 是 `@T.macro` 而非独立的 prim_func kernel，这带来了什么？

> **答案**：作为宏，它会被**内联展开**进调用方（`top2_sum_gate_kernel`）的 prim_func 里，直接复用调用方已分配的 `scores_shared` 等共享内存与线程上下文，省去额外的 kernel 启动与数据搬运。这正是 u2-l3 讲过的「宏 = 可复用代码片段」的典型用法。

## 5. 综合实践

把本讲的三块知识（打分函数、bias 位置、打分进入分组）串起来，完成下面这个**纯 CPU、无需 GPU** 的小任务：

**任务**：自己实现一个迷你版「打分 + bias + 分组粗筛 + top-k」参考，与 `tile_kernels/torch/topk.py` 的语义对齐，并对三种打分函数各跑一遍。

```python
# 示例代码：迷你 MoE 路由参考（CPU 即可，对照 torch/topk.py 语义）
import torch
import torch.nn.functional as F
from tile_kernels.moe.scoring import ScoringFunc   # 需本包；否则把枚举常量自定义为 0/1/2

def scoring_apply(logits, scoring):
    """对应 topk.py 第 1 步：scores_wo_bias"""
    if scoring == ScoringFunc.SIGMOID:
        return torch.sigmoid(logits)
    elif scoring == ScoringFunc.SQRTSOFTPLUS:
        return F.softplus(logits).sqrt()
    elif scoring == ScoringFunc.SOFTMAX:
        return torch.softmax(logits, dim=-1)
    raise ValueError

def mini_route(logits, bias, scoring, num_topk, num_groups, num_topk_groups):
    # 1. 打分
    scores_wo_bias = scoring_apply(logits, scoring)
    # 2. bias 位置：SOFTMAX 进 logits，其它进 scores
    if scoring == ScoringFunc.SOFTMAX:
        scores_biased = logits + bias
    else:
        scores_biased = scores_wo_bias + bias
    # 3. 分组粗筛（简化版：组内 top-2 之和 → 选 num_topk_groups 个最好的组）
    per_group = num_routed_experts // num_groups
    g = scores_biased.view(num_tokens, num_groups, per_group)
    group_score = g.topk(2, dim=-1).values.sum(-1)         # 组内 top-2 之和
    keep_groups = group_score.topk(num_topk_groups, dim=-1).indices
    mask = torch.ones_like(scores_biased).bool()
    mask.view(num_tokens, num_groups, per_group).scatter_(1, keep_groups.unsqueeze(-1), False)
    scores_masked = scores_biased.masked_fill(~mask, float('-inf'))
    # 4. 选 top-k（用 scores_biased 排序）
    topk_idx = scores_masked.topk(num_topk, dim=-1).indices
    # 5. 权重：用 scores_wo_bias gather（不带 bias）
    topk_score = scores_wo_bias.gather(1, topk_idx)
    topk_weights = topk_score / topk_score.sum(-1, keepdim=True)
    return topk_idx, topk_weights

# 跑三种打分
torch.manual_seed(7)
num_tokens, num_routed_experts, num_groups, num_topk_groups, num_topk = 5, 8, 2, 1, 2
logits = torch.randn(num_tokens, num_routed_experts)
bias   = torch.randn(num_routed_experts)
for sf in [ScoringFunc.SIGMOID, ScoringFunc.SQRTSOFTPLUS, ScoringFunc.SOFTMAX]:
    idx, w = mini_route(logits, bias, sf, num_topk, num_groups, num_topk_groups)
    print(f"{str(sf):14s} topk_idx={idx[0].tolist()} weights={[round(x,3) for x in w[0].tolist()]}")
```

需要观察与思考：

1. 三种打分选出的 `topk_idx` 是否相同？为什么即便用同一份 logits 也可能不同？（提示：bias 位置不同 + 打分的归一化范围不同。）
2. 把 `num_topk_groups` 设成和 `num_groups` 相等（即不分组），观察结果变化——这对应 kernel 里 `skip_group_sort=True` 的分支。
3. 故意把 SOFTMAX 的 bias 改成加在 `scores_wo_bias` 上（写错），观察 top-k 是否变化，体会 4.3 的 bias 陷阱。

预期结果：三种打分给出的 `topk_idx` 经常不同；SOFTMAX 的权重之和（归一化前）量级通常小于 SIGMOID。若你安装了真实环境，可把本脚本的 SOFTMAX 分支输出与 `tile_kernels.moe.top2_sum_gate(..., scoring_func='softmax')` 的结果对比（**待本地验证**）。

## 6. 本讲小结

- MoE 路由 = 给每个 token 选几个专家（`topk_idx`）并算权重（`topk_weights`），原料是模型打的原始分 `logits`。
- **打分函数**是路由的第一步变换，把 logits 变成「非负、可比」的 scores。TileKernels 内置三种：`sigmoid`（逐元素、有界）、`softmax`（全行竞争、和为 1）、`sqrtsoftplus`（逐元素、无界、开方压平）。
- `ScoringFunc(IntEnum)` 用整数值 `0/1/2/3` 作编译期参数特化 kernel，并用 `__str__`/`from_str` 实现字符串往返；`IDENTITY=3` 在 `top2_sum_gate` 里被禁用。
- `@T.macro softplus` 用 `threshold=20` 分段避免 `exp` 溢出，是 `sqrtsoftplus` 的底层依赖。
- **关键陷阱**：SOFTMAX 把 bias 加在原始 logits 上（`softmax(logits+bias)`），而 sigmoid/sqrtsoftplus 把 bias 加在打分之后；权重计算一律用不带 bias 的 `scores_wo_bias`。
- 打分结果（加 bias 后的 `scores_biased`）流入 `common.py` 的 `get_topk_group_idx` 做「组内 top-2 之和」的分组粗筛，再由后续 kernel 在组内细选 top-k。

## 7. 下一步学习建议

本讲只讲了「打分」这一步，路由链路的后半段还很长。建议按下面的顺序继续：

1. **u5-l2 Top-k 门控 kernel：reducer 与 warp shuffle**：打完分后怎么在 GPU 上高效选出 top-k？重点看 `topk_gate_kernel` 用 `reduce_max + alloc_reducer('min')` 逐个稳定选最大值，以及 `top2_sum_gate` 里 `shfl_xor` 的全 warp 归约。这是本讲 4.4 提到的「并列处理」的完整答案。
2. **u5-l3 分组路由与 group counting**：深入 `get_topk_group_idx` 的 warp shuffle 计数排序全流程，把本讲 4.4 跳过的细节补齐。
3. 若想验证本讲的数值直觉，可先读 `tests/moe/test_top2_sum_gate.py` 的对拍范式（承接 u3-l2 的测试三件套），再在 GPU 上跑一遍 `--run-benchmark` 观察 SOFTMAX 因要做两遍 warp 归约（max + sum）而比 SIGMOID 略慢的现象（**待本地验证**）。
