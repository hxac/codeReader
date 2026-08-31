# 多视角 batch 训练与梯度合并

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `batch_size > 1` 时一次迭代内部发生了什么：B 个视角如何逐个 `render → loss → backward`，梯度如何跨视角累积。
2. 解释 `visibility_count` 的计算方式，以及三类 per-view 输出（viewspace 梯度、`visibility_filter`、`radii`）分别用什么规则合并（求和 / 取或 / 取最大）。
3. 从数学上推导 `× batch_size / visibility_count` 这个归一化因子为什么恰好把统计量还原成「可见视角上的平均梯度」。
4. 区分 `add_densification_stats` 与 `add_densification_stats_grad` 两条致密化统计路径的适用场景与输入形态。
5. 说明 4D 场景下 `_t`（时间中心）梯度为什么需要同款归一化，以及它与 viewspace 梯度在读取时机、求和方式上的三点差异。

本讲只讲「梯度怎么合并、统计怎么累积」，致密化的 clone/split 判定算法本身留给下一讲（u5-l4）。

## 2. 前置知识

### 2.1 PyTorch 的梯度累积语义

这是本讲最重要的前置。PyTorch 中调用 `loss.backward()` 时，梯度不是「覆盖」而是**累加**到参数的 `.grad` 属性上：

```python
p = torch.nn.Parameter(torch.tensor([1.0]))
loss1 = (p * 3 - 1).pow(2)
loss1.backward()   # p.grad = 某个值 g1
loss2 = (p * 5 - 1).pow(2)
loss2.backward()   # p.grad = g1 + g2，而不是 g2
```

只有调用 `optimizer.zero_grad()` 之后 `.grad` 才被清空。所以「一个 batch 分多次 backward」等价于「对 batch 总损失做一次 backward」——前提是每次 backward 前把 loss 除以 batch 大小，这样累加结果才是「平均梯度」而非「总和梯度」。这就是经典的**梯度累积（gradient accumulation）**技巧，4C4D 用它来在有限显存下模拟大 batch。

### 2.2 叶子张量与 screenspace_points 的特殊身份

u4-l1 已经建立过这个认知，这里只回顾结论：`render()` 每次被调用都会新建一个全零、`requires_grad=True` 的张量 `screenspace_points`（形状 `(N, 3)`，即 `means2D`），它是一个**新鲜的叶子张量**。因此每个视角各自的 `viewspace_point_tensor.grad` 只包含**本视角**反传回来的梯度；而 `_t`、`_xyz` 这类 `nn.Parameter` 是**持久对象**，它们的 `.grad` 会跨视角一直累积到调用 `zero_grad` 为止。这个差异正是本讲 4.3 节两条读取路径的根源。

### 2.3 visibility_filter 与致密化信号回顾

- `visibility_filter` 就是 `radii > 0`：高斯投影到屏幕上的半径大于 0，即未被视锥剔除、真正参与本视角渲染。
- `viewspace_point_tensor.grad[:, :2]` 的范数（屏幕空间 x/y 方向的梯度大小）是 3DGS 一脉相承的**致密化信号**：某个高斯在屏幕上被推动得越厉害，说明当前几何在该处误差越大，越值得 clone/split。
- `xyz_gradient_accum / denom` 是这个信号在多次迭代上的平均，与 `densify_grad_threshold`（默认 0.0002）比较决定是否致密化。

### 2.4 一个关键事实：不可见视角的梯度为零

被视锥剔除或投影半径为 0 的高斯不参与该视角任何像素的 alpha 合成，因此反传时它得到的梯度为 0。这一点是后面所有归一化推导的地基。

## 3. 本讲源码地图

| 文件 | 本讲涉及的部分 | 作用 |
| --- | --- | --- |
| `train.py` | L104-105、L129-152、L171-186、L239-244、L259-261 | batch 循环、梯度累积、跨视角合并、统计路径选择、step/zero_grad 时机 |
| `scene/gaussian_model.py` | L72-74、L87、L479-495、L561-582、L670-674、L748-780 | 统计累加器的创建、对齐、清零，以及两条 `add_densification_stats*` 路径 |
| `gaussian_renderer/__init__.py` | L26、L195-202 | `screenspace_points` 的每次新建、`visibility_filter` 的定义、render 返回的 dict |
| `arguments/__init__.py` | L99-103 | 致密化相关阈值默认值 |

## 4. 核心概念与源码讲解

### 4.1 batch 梯度合并：跨视角累积 loss、合并可见性

#### 4.1.1 概念说明

`batch_size` 默认为 1，官方配置里可以调大（例如某些 4DGS 配置用 4）。当 `batch_size = B > 1` 时，一次 iteration 的含义从「看一个视角」变成「看 B 个视角」：

- **参数更新层面**：对 B 个视角各自计算 loss，各自除以 B 再 backward。由于 2.1 节的累积语义，B 次 backward 之后参数 `.grad` 里存的是 \(\frac{1}{B}\sum_v \ell_v\) 的梯度——即 batch 平均损失的梯度，与「把 B 张图拼成一个大 batch 做一次 backward」数学等价，但显存只需容纳一张图。
- **统计合并层面**：render 每次只返回「本视角」的 `visibility_filter`、`radii`、`viewspace_points`。致密化需要的是「这次 iteration（即这 B 个视角整体）」的统计量，所以必须把 B 份 per-view 输出合并成一份。三类输出各有一套合并规则（见下表）。

为什么必须归一化？直觉是：一个只被 1 个视角看见的高斯，它的梯度天然只来自 1 份 loss；一个被 B 个视角都看见的高斯，梯度来自 B 份 loss。如果直接把累加值喂给阈值，前者会被系统性低估——即使它在「被看见的那个视角」里误差很大，也可能过不了阈值。归一化因子 \(\frac{B}{c}\)（\(c\) 为可见次数）把两者都还原到同一个口径：「**被看见时的平均梯度**」。

#### 4.1.2 核心流程

一次 iteration（`batch_size = B`）的伪代码：

```text
初始化 batch_point_grad = []        # 每视角一个 (N,) 张量
初始化 batch_visibility_filter = [] # 每视角一个 (N,) bool
初始化 batch_radii = []             # 每视角一个 (N,)

for v in range(B):
    render 视角 v → image, viewspace_point_tensor, visibility_filter, radii
    loss_v = (1-λ)·L1 + λ·(1-SSIM)
    (loss_v / B).backward()              # 梯度累进 _xyz/_t 等持久参数
    记录 ‖viewspace_point_tensor.grad[:, :2]‖₂、radii_v、filter_v

# ---- 合并（仅 B > 1 时）----
visibility_count = stack(filters, dim=1).sum(1)   # (N,)：B 个视角中被看见的次数
visibility_filter = visibility_count > 0           # 至少一个视角可见
radii = stack(radii_list, dim=1).max(1)            # 取 B 个视角中的最大半径

grad_sum = stack(grad_norm_list, dim=1).sum(1)     # (N,)：Σ_v ‖h_v‖/B
grad_sum[visibility_filter] *= B / visibility_count[visibility_filter]
                                                   # (N,)：可见视角上的平均范数
```

三类 per-view 输出的合并规则：

| 输出 | 合并规则 | 语义 | 后续用途 |
| --- | --- | --- | --- |
| `visibility_filter`（bool） | `count > 0`（逻辑或） | B 个视角中至少一个可见 | 决定哪些高斯参与统计更新与 `max_radii2D` |
| `radii`（float） | `max` | 最坏情况下的屏幕半径 | 供 `max_radii2D` 剪枝（屏幕过大者删） |
| viewspace 梯度范数 | 先按视角求范数、再求和、再乘 `B/count` | 可见视角上的平均屏幕梯度范数 | 致密化统计 |

数学推导。设 batch 内 B 个视角，第 v 个视角的损失为 \(\ell_v\)，代码传给 backward 的是 \(\ell_v / B\)。对一个持久参数 \(\theta\)（例如 `_t`），batch 循环结束后：

\[
\theta.\text{grad} \;=\; \sum_{v=1}^{B} \frac{1}{B}\,\frac{\partial \ell_v}{\partial \theta} \;=\; \frac{1}{B}\sum_{v=1}^{B} g_v
\]

由 2.4 节，不可见视角贡献 \(g_v = 0\)。记可见次数为 \(c\)（即 `visibility_count`），则：

\[
\frac{B}{c}\cdot\frac{1}{B}\sum_{v\in \mathrm{vis}} g_v \;=\; \frac{1}{c}\sum_{v\in \mathrm{vis}} g_v
\]

右边正是「可见视角上的平均梯度」。viewspace 梯度的推导完全同构，只是每视角先取范数再求和：

\[
\frac{B}{c}\cdot\frac{1}{B}\sum_{v\in\mathrm{vis}} \lVert h_v \rVert_2 \;=\; \frac{1}{c}\sum_{v\in\mathrm{vis}} \lVert h_v \rVert_2
\]

反事实对照：**不做**归一化时统计量为 \(\frac{c}{B}\cdot\overline{g}_{\mathrm{vis}}\)，可见次数越少被压得越低（\(c=1, B=4\) 时只剩真实平均的四分之一）。归一化之后阈值 0.0002 对所有高斯拥有统一含义——「被看见时平均每次被推动多少」，这就是对致密化阈值的公平性。

注意两层平均的分工：`loss / B` 服务于**参数更新**（batch 平均梯度）；`× B/c` 服务于**致密化统计**（可见平均）。后者作用在 detached 副本上，不会影响 optimizer 看到的梯度。

#### 4.1.3 源码精读

DataLoader 的组织方式。`collate_fn=lambda x: x` 让每个 batch 保持为 B 个 `(gt_image, viewpoint_cam)` 元组的列表，`drop_last=True` 保证每个 batch 恒满 B 个视角（不足 B 的尾部直接丢弃）：

- [train.py:104-105](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L104-L105)：训练用 DataLoader，`batch_size=batch_size`、`shuffle=True`、自定义 collate、`drop_last=True`。

batch 循环前的三个收集容器：

- [train.py:129-131](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L129-L131)：声明 `batch_point_grad`、`batch_visibility_filter`、`batch_radii` 三个空列表。

视角内循环——渲染、损失、除以 B、反传、收集：

- [train.py:133-152](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L133-L152)：对 `batch_data[batch_idx]` 逐视角执行 `render` → 解包四元组 → L1+SSIM loss → `loss / batch_size` → `backward()`，随后把三个 per-view 量 append 进列表。
- [train.py:147-148](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L147-L148)：`loss = loss / batch_size` 后 `loss.backward()`——梯度累积的关键两行。
- [train.py:150](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L150)：`torch.norm(viewspace_point_tensor.grad[:,:2], dim=-1)`。`screenspace_points` 形状为 `(N, 3)`，只取前两维（屏幕 x/y）；由于它是本视角新建的叶子（见 [gaussian_renderer/__init__.py:26](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L26) 的 `torch.zeros_like(pc.get_xyz, ..., requires_grad=True) + 0`），这里的 `.grad` 只含本视角的贡献，且已经带上了 `/B` 缩放。

合并段（`batch_size > 1` 分支）：

- [train.py:172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L172)：`torch.stack(batch_visibility_filter, 1).sum(1)` 把 B 个 `(N,)` 布尔张量堆成 `(N, B)` 再沿视角维求和，得到 `visibility_count`——每个高斯在 B 个视角中被看见的次数。
- [train.py:173-174](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L173-L174)：`visibility_count > 0` 得到合并版 `visibility_filter`（任一视角可见即可）；`radii` 取 B 个视角的最大值——`max_radii2D` 剪枝关心的是最坏情况下的屏幕尺寸。
- [train.py:176-178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L176-L178)：先 `stack(...,1).sum(1)` 得到 `(N,)` 的范数总和，再对可见项乘 `batch_size / visibility_count`，最后 `unsqueeze(1)` 变 `(N,1)` 以匹配 `xyz_gradient_accum` 的形状。注意赋值只发生在 `visibility_filter` 选中的项上，因此 `count = 0` 的高斯不会触发除零（它们的统计量本来就是 0）。

`zero_grad` 的时机——整个 batch 之后才清零，这是 `_t.grad` 能被「一次性读取」的前提：

- [train.py:259-261](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L259-L261)：`optimizer.step()` 之后才 `zero_grad(set_to_none=True)`。从上一个 batch 结束到本 batch 结束之间，所有持久参数的 `.grad` 一直在累积。

辅助事实——`visibility_filter` 的定义与 render 的返回：

- [gaussian_renderer/__init__.py:195-202](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L195-L202)：`visibility_filter = radii_all > 0`，连同 `viewspace_points`（即 `screenspace_points` 原对象）、`radii` 一起放进返回 dict。文件内的注释也点明了语义：被视锥剔除或半径为 0 的高斯不算可见，将不参与分裂判据的更新。

#### 4.1.4 代码实践

**实践目标**：用两个小张量亲手验证 2.1/2.2 节声称的行为——「持久参数的 `.grad` 跨 backward 累积，而每次新建的叶子只含本视角梯度」，这正是 batch 合并代码能成立的前提。

**操作步骤**（示例代码，纯 CPU 即可运行，不依赖本仓库的 CUDA 扩展）：

```python
# 示例代码：模拟 batch 内两个视角的梯度累积
import torch

B = 2
persistent = torch.nn.Parameter(torch.tensor([1.0]))   # 模拟 _t：持久参数
fresh_grads = []                                        # 收集每个视角的新鲜叶子

for w in [3.0, 5.0]:                                   # 两个"视角"，权重不同
    fresh = torch.zeros(1, requires_grad=True)         # 模拟 screenspace_points：每次新建
    y = (fresh + persistent) * w
    loss = (y - 1.0).pow(2) / B                        # 对应 train.py 的 loss / batch_size
    loss.backward()
    fresh_grads.append(fresh.grad.clone())

print("persistent.grad =", persistent.grad.item())     # 两个视角梯度之和
print("fresh grads =", [g.item() for g in fresh_grads])
```

**需要观察的现象**：`persistent.grad` 是一个数，`fresh_grads` 是两个互相独立的数。

**预期结果**：每个视角的原始梯度为 \((w_v\cdot 1-1)\cdot w_v\)，即视角一为 6、视角二为 20（已含 `/B` 缩放，原始值分别是 12 和 40）。因此打印应为 `persistent.grad = 26.0`、`fresh grads = [6.0, 20.0]`——持久参数拿到两者之和 26（等于 \(\frac{12+40}{B}\)，即 batch 平均），每个新鲜叶子只拿到自己视角的 6 / 20。若把 `persistent.grad` 与 `fresh grads` 对不上，说明你对累积语义的理解有偏差。（待本地验证：在不同 torch 版本上数值一致，仅打印格式可能不同。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `train.py` 中 `loss = loss / batch_size` 这一行删掉，参数更新和致密化统计分别会发生什么？

**答案**：参数更新会变成「B 个视角 loss 之和的梯度」，等效于学习率放大 B 倍（B 个视角平均损失乘 B）。致密化统计会被 `× batch_size / visibility_count` 部分抵消：合并值变为 \(\frac{B}{c}\sum_v \lVert h_v\rVert\)，对全程可见（\(c=B\)）的高斯恰好等于「B 倍平均」，统计整体膨胀 B 倍，阈值 0.0002 的含义被破坏；而对 \(c<B\) 的高斯膨胀幅度是 \(B/c\) 倍不等，公平性也被破坏。

**练习 2**：为什么 `radii` 用 `max` 合并而 `visibility_filter` 用「或」合并？

**答案**：二者服务的判定不同。`max_radii2D` 用于剪枝「屏幕上过大的高斯」，只要任何一个视角把它投得很大就应当被标记，所以取最坏情况 max；`visibility_filter` 用于决定「哪些高斯的统计需要更新」，只要有一个视角看见它，它的梯度与统计就有效，所以取「或」。若 `radii` 改成取平均，会漏掉「多数视角很小、个别视角巨大」的异常高斯。

**练习 3**：`visibility_count = 0` 的高斯在 [train.py:177](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L177) 会发生除零错误吗？

**答案**：不会。赋值语句的左边索引是 `batch_viewspace_point_grad[visibility_filter]`，右侧除法的分母 `visibility_count[visibility_filter]` 也被同一个掩码过滤——只有 `count > 0` 的项参与运算。`count = 0` 的项根本不进入表达式，其统计值保持为 0。

### 4.2 add_densification_stats 与 add_densification_stats_grad：两条统计路径

#### 4.2.1 概念说明

合并之后的梯度范数还不能直接用——单次 iteration 的屏幕梯度噪声很大，3DGS 的做法是**在多次迭代上累积再取平均**：`xyz_gradient_accum` 累加梯度范数，`denom` 累加次数，致密化触发时用 `xyz_gradient_accum / denom` 得到区间平均，再与阈值比较。

`GaussianModel` 提供了两个几乎相同的成员函数：

- `add_densification_stats(viewspace_point_tensor, ...)`：接收**原始的 screenspace 张量**，函数内部自己取 `grad[:, :2]` 的范数。这是 3DGS 原版路径，对应 `batch_size == 1`——每个 iteration 只有一个视角，`viewspace_point_tensor` 的 `.grad` 就是全部信号。
- `add_densification_stats_grad(viewspace_point_grad, ...)`：接收**已经合并、已经归一化好的 `(N, 1)` 梯度张量**，函数内部只做累加。对应 `batch_size > 1`——范数与归一化已经在 train.py 的合并段完成。

二者对 `denom` 的处理完全一致：每次调用 `+1`，即 **denom 数的是 iteration 次数，不是视角次数**。这一点很关键：无论 batch 多大，一次 iteration 只加 1，所以 `accum / denom` 的语义统一为「**每次 iteration（内部已对可见视角平均）的梯度范数的 iteration 平均**」。

#### 4.2.2 核心流程

统计量的完整生命周期：

```text
training_setup / densification_postfix
    xyz_gradient_accum = zeros(N,1); denom = zeros(N,1)   # 创建或重建

每个 iteration（densify_until_iter 之前）:
    merged_grad = (B>1 ? 归一化后的 batch 平均 : 单视角范数)   # (N,1)
    xyz_gradient_accum[visibility_filter] += merged_grad[visibility_filter]
    t_gradient_accum[visibility_filter]  += batch_t_grad[visibility_filter]  # 4D 时
    denom[visibility_filter] += 1

每 densification_interval 次触发致密化:
    grads   = xyz_gradient_accum / denom     # iteration 平均
    grads_t = t_gradient_accum  / denom
    densify_and_clone / densify_and_split    # 用 grads 选点（见 u5-l4）
    prune_points(...)                        # 统计向量随存活点裁剪对齐
    densification_postfix(...)               # clone/split 后全部统计清零重建
```

注意 `+=` 只发生在 `visibility_filter` 选中的行上：不可见高斯的 `denom` 不增长。于是 \(\text{denom}_i\) 是「高斯 i 被统计的 iteration 数」，\(\text{accum}_i/\text{denom}_i\) 是「被统计的那些 iteration 上的平均」，分母分子天然对齐，不会出现除以未参与统计的次数。

#### 4.2.3 源码精读

两条统计路径的函数体：

- [scene/gaussian_model.py:770-774](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L770-L774)：`add_densification_stats` 内部计算 `torch.norm(viewspace_point_tensor.grad[update_filter,:2], dim=-1, keepdim=True)` 并累加，`denom[update_filter] += 1`；4D 时把 `avg_t_grad` 累进 `t_gradient_accum`。
- [scene/gaussian_model.py:776-780](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L776-L780)：`add_densification_stats_grad` 唯一的区别是不再取范数——输入 `viewspace_point_grad` 已是 train.py 合并段产出的 `(N,1)` 张量，直接 `+=`。

train.py 侧的路径选择：

- [train.py:239-244](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L239-L244)：`batch_size == 1` 时传原始 `viewspace_point_tensor` 走 `add_densification_stats`；否则传 `batch_viewspace_point_grad` 走 `add_densification_stats_grad`。4D 时两者都把 `batch_t_grad` 作为第三个参数传入。

统计累加器的创建、对齐与清零：

- [scene/gaussian_model.py:479-482](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L479-L482)：`training_setup` 中把 `xyz_gradient_accum`、`denom` 建为 `(N,1)` 零张量；[scene/gaussian_model.py:495](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L495) 同步创建 `t_gradient_accum`。`__init__` 阶段它们只是空占位（[scene/gaussian_model.py:72-74](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L72-L74)、[scene/gaussian_model.py:87](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L87)）。
- [scene/gaussian_model.py:748-759](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L748-L759)：`densify_and_prune` 触发时计算 `grads = self.xyz_gradient_accum / self.denom`（NaN 置 0），4D 时同样算出 `grads_t`，传给 clone/split。
- [scene/gaussian_model.py:572-582](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L572-L582)：`prune_points` 用同一个 `valid_points_mask` 裁剪所有统计向量，保证点集与统计行号一一对应。
- [scene/gaussian_model.py:670-674](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L670-L674)：`densification_postfix` 在 clone/split 拼接出新点集后把三个统计量与 `max_radii2D` 全部重建为零——致密化之后统计重新从零开始。

阈值默认值：

- [arguments/__init__.py:99-103](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L99-L103)：`densify_from_iter = 500`、`densify_until_iter = 15_000`、`densify_grad_threshold = 0.0002`、`densify_grad_t_threshold = 0.0002 / 40`。开启 `opacity_decay` 时 `densify_until_iter` 会被 train.py 覆盖为总迭代数（u5-l1 已讲）。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读回答「统计口径」问题，加深对 `denom` 的理解。这是一个源码阅读型实践，无需 GPU。

**操作步骤**：

1. 打开 [scene/gaussian_model.py:770-780](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L770-L780)，逐行对比两个函数，写出它们全部的三处差异（提示：输入形态、是否取范数、其余是否完全一致）。
2. 假设 `densification_interval = 100`、某个高斯在两次致密化之间的 100 个 iteration 里全部可见，且每次 iteration 归一化后的 viewspace 梯度范数恒为 0.001。手算 `xyz_gradient_accum`、`denom`、以及 `densify_and_prune` 里 `grads` 的值。
3. 再考虑第二个高斯，它在 100 个 iteration 里只有 40 次进入 `visibility_filter`，其余条件相同（每次被统计时范数也是 0.001）。同样手算三个值。

**需要观察的现象 / 预期结果**：

- 第一问：两函数的差异只有「输入是张量还是梯度范数」与「是否在函数内取范数」，`denom` 与 `t_gradient_accum` 的处理逐字相同。
- 第二问：`accum = 100 × 0.001 = 0.1`，`denom = 100`，`grads = 0.001`。由于 0.001 > 0.0002，这个高斯会进入 clone/split 候选。
- 第三问：`accum = 40 × 0.001 = 0.04`，`denom = 40`，`grads = 0.001`——与第一个高斯完全相同。这正是设计的妙处：**被统计的次数少并不会拉低平均值**，因为分子分母同步缩减。真正会被「少看见」影响的是 4.1 节合并层（如果不做 `×B/c` 归一化），而不是这一层。

**待本地验证**：第 2、3 问的结论可以随后在真实训练中用 TensorBoard 的 `total_points` 曲线间接验证（改 `densify_grad_threshold` 观察致密化数量变化，见 u5-l4 的实践）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_densification_stats_grad` 不像 `add_densification_stats` 那样在函数内取范数？

**答案**：因为 batch 模式下的范数必须**逐视角**先取、再跨视角求和（\(\sum_v \lVert h_v\rVert\)），这个信息在合并成单个张量之后就丢失了。若把 B 个视角的梯度先求和再取范数（\(\lVert\sum_v h_v\rVert\)），不同视角的梯度方向会互相抵消，统计被系统性低估。所以 train.py 必须在视角循环里逐个取范数（[train.py:150](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L150)），函数只负责累加。

**练习 2**：`denom` 为什么在 `visibility_filter` 之外不递增？如果改成无条件 `denom += 1` 会有什么后果？

**答案**：`denom` 的语义是「该高斯实际被统计的 iteration 数」。若无条件递增，一个经常被剔除的高斯会拥有很大的 `denom` 但很小的 `accum`，`grads` 被稀释，变得几乎永远达不到阈值——恰好惩罚了最需要致密化的边缘高斯。当前实现让分子分母同步增长，平均值的口径保持纯净。

**练习 3**：致密化发生后（`densification_postfix` 执行完），为什么统计必须全部清零、而不是保留旧值？

**答案**：两个原因。其一，点集变了：clone/split 产生的新高斯没有历史统计可言，被剪掉的高斯的统计也无从对应，行号已经错位；其二，致密化本身改变了几何，旧的梯度统计反映的是「致密化之前」的误差分布，继续沿用会重复触发已修复的区域。清零让每个致密化周期从干净状态重新评估。

### 4.3 _t 梯度：4D 时间中心的同款归一化

#### 4.3.1 概念说明

`gaussian_dim == 4` 时模型多出一组时间维参数，其中**时间中心 `_t`**（形状 `(N,1)`，记录每个高斯在 `time_duration` 上的活跃时刻）是本讲的主角。4DGS 原始设计里，时间中心也有一份对应的致密化信号：某高斯的时间中心梯度大，说明它的时间位置对渲染误差敏感，是「时间上没放对」的候选。因此 train.py 对 `_t` 的梯度做了与 viewspace 梯度完全同款的 `× batch_size / visibility_count` 归一化——公平性论证一模一样：一个只被 1 个视角看见的高斯，其 `_t` 梯度同样只来自 1 份 loss。

但 `_t` 的读取方式与 viewspace 梯度有**三点结构性差异**：

1. **读取时机不同**。`screenspace_points` 每次渲染都新建（4.1 节），所以必须在视角循环内逐个收集 `.grad`；而 `_t` 是持久 `nn.Parameter`，B 次 backward 之后它的 `.grad` 已经自动累加了整个 batch 的贡献，循环外**读一次即可**。
2. **求和方式不同**。viewspace 统计是「逐视角取范数再求和」；`_t` 统计直接累加**带符号的梯度值**（向量求和），没有取范数。不同视角的时间梯度方向可以相互抵消——这属于原实现的口径选择。
3. **必须 clone**。读取 `_t.grad` 后要做原位的掩码赋值，而基本索引（`[:,0]`）返回的是视图，原位赋值会**写穿**到 `.grad` 本体。不加 `.clone()` 的话，归一化因子会污染真正的优化器梯度。

还有一点值得如实指出：在本仓库当前实现里，`grads_t` 与 `grad_t_threshold` 虽然一路计算并传递，但 `densify_and_clone` / `densify_and_split` 的**函数体并没有消费它们**（只出现在形参列表中）。也就是说时间梯度统计目前处于「被计算但不被使用」的状态，属于从上游继承的预留接口；上游 4DGS 是否在选点时使用时间梯度，待确认。

#### 4.3.2 核心流程

`_t` 梯度的处理流水线（`batch_size > 1` 且 `gaussian_dim == 4`）：

```text
视角循环中:  (loss_v / B).backward()        # _t.grad 逐视角累加 (1/B)·g_v
循环结束后:
    batch_t_grad = _t.grad.clone()[:,0].detach()   # (N,1)→(N,)，clone 防写穿
    batch_t_grad[visibility_filter] *= B / visibility_count[visibility_filter]
    batch_t_grad = batch_t_grad.unsqueeze(1)       # (N,)→(N,1)

统计累积:    t_gradient_accum[visibility_filter] += batch_t_grad[visibility_filter]
致密化时:    grads_t = t_gradient_accum / denom   # 当前未被 clone/split 消费
```

数学上，循环结束时有：

\[
\_t.\text{grad} \;=\; \frac{1}{B}\sum_{v\in\mathrm{vis}} g_v^{(t)}, \qquad
\frac{B}{c}\cdot \_t.\text{grad} \;=\; \frac{1}{c}\sum_{v\in\mathrm{vis}} g_v^{(t)}
\]

即「可见视角上的平均时间梯度」，与 viewspace 的推导逐字同构。`batch_size == 1` 时（[train.py:184-186](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L184-L186)）退化为直接 `clone().detach()`——`c = 1`，归一化因子本来就是 1，无需 `[:,0]` 的形状往返。

再次强调分工：`optimizer.step()` 用的是**未归一化**的 `_t.grad`（batch 平均，用于参数更新）；`batch_t_grad` 是 detached 副本上的**归一化**值，只进致密化统计。两条数据流互不干扰，而维护这条边界的正是 `.clone()`。

#### 4.3.3 源码精读

- [train.py:180-183](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L180-L183)：`batch_t_grad = gaussians._t.grad.clone()[:,0].detach()`——`.clone()` 先复制整个 `(N,1)` 梯量，`[:,0]` 切成 `(N,)` 视图，`.detach()` 切断计算图；随后同样的 `× batch_size / visibility_count` 掩码赋值，再 `unsqueeze(1)` 回 `(N,1)`。
- [train.py:184-186](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L184-L186)：`batch_size == 1` 分支直接 `_t.grad.clone().detach()`，形状天然是 `(N,1)`。
- [train.py:239-244](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L239-L244)：两条统计路径都接收 `batch_t_grad if gaussians.gaussian_dim == 4 else None` 作为时间梯度参数。
- [scene/gaussian_model.py:495-497](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L495-L497)：`training_setup` 创建 `t_gradient_accum` 并把 `_t` 注册进优化器参数组（`position_t_lr_init < 0` 时回退空间位置学习率，u3-l2 已讲）。注意 `_t` 的学习率**全程不衰减**，与 xyz 组不同——时间中心靠持续的大步长纠偏。
- [scene/gaussian_model.py:773-774](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L773-L774) 与 [scene/gaussian_model.py:779-780](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L779-L780)：两条路径都执行 `self.t_gradient_accum[update_filter] += avg_t_grad[update_filter]`——注意累加的是带符号值，不取范数、不求绝对值。
- [scene/gaussian_model.py:752-756](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L752-L756)：`densify_and_prune` 中 `grads_t = self.t_gradient_accum / self.denom`（NaN 置 0），随后传入 clone/split。
- [scene/gaussian_model.py:676-683](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L676-L683) 与 [scene/gaussian_model.py:723-728](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L723-L728)：`densify_and_split` / `densify_and_clone` 的签名都接收 `grads_t, grad_t_threshold`，但函数体的选点掩码只用了空间梯度 `grads`（`padded_grad >= grad_threshold` 与 `torch.norm(grads, dim=-1) >= grad_threshold`），`grads_t` 在两个函数体内均未出现——即 4.3.1 节所述「被计算但不被消费」。
- [scene/gaussian_model.py:582](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L582)：`prune_points` 裁剪 `t_gradient_accum`，与其他统计保持行对齐。

#### 4.3.4 代码实践

**实践目标**：亲手验证「基本索引返回视图、原位掩码赋值会写穿本体」这一事实，理解 [train.py:181](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L181) 里 `.clone()` 为什么不可省。

**操作步骤**（示例代码，纯 CPU 可运行）：

```python
# 示例代码：演示 clone 与不 clone 的差别
import torch

def pipeline(clone_first):
    grad = torch.tensor([[1.0], [2.0], [4.0], [8.0]])   # 模拟 _t.grad，(N,1)
    if clone_first:
        t = grad.clone()[:, 0]                          # 仓库的做法：先 clone 再切片
    else:
        t = grad[:, 0]                                  # 错误做法：直接切片得到视图
    visibility = torch.tensor([True, True, False, True])
    count = torch.tensor([2, 2, 0, 1])
    t[visibility] = t[visibility] * 4 / count[visibility]   # 模拟 train.py 的原位归一化
    return grad, t

grad_safe, _ = pipeline(clone_first=True)
grad_bad,  _ = pipeline(clone_first=False)
print("clone 后的 _t.grad:", grad_safe.flatten().tolist())   # 应保持原值
print("不 clone 的 _t.grad:", grad_bad.flatten().tolist())   # 被污染
```

**需要观察的现象**：`clone_first=False` 时，对切片视图的掩码赋值改动了 `grad` 本体。

**预期结果**：`clone` 版打印 `[1.0, 2.0, 4.0, 8.0]`（原梯度完好）；不 `clone` 版打印 `[2.0, 4.0, 4.0, 32.0]`——第 0、1、3 项分别被乘上 `4/2`、`4/2`、`4/1`。放在真实训练里，这意味着 `optimizer.step()` 拿到的是被 `B/count` 放大过的梯度：全程可见的高斯不受影响（因子为 1），越少见的高斯时间中心更新被放大得越厉害，训练动态被悄悄改变。因此 `.clone()` 不是防御式编程的点缀，而是正确性必需。（待本地验证：数值可手算复核。）

**补充阅读步骤**：在仓库根目录执行 `grep -n "grads_t" scene/gaussian_model.py`，确认它只出现在 L676、L723（签名）、L753-759（计算与传递），两个致密化函数体内确实没有消费点。

#### 4.3.5 小练习与答案

**练习 1**：为什么 viewspace 梯度必须在视角循环内逐个收集，而 `_t` 梯度可以循环外读一次？

**答案**：`screenspace_points` 是 `render()` 每次调用新建的叶子张量，本次视角的 `.grad` 在下一次渲染后旧对象就不再更新（且会被垃圾回收，除非手动保留引用），所以必须循环内立刻取。`_t` 是模型持有的 `nn.Parameter`，`.grad` 由 autograd 持久累积，且 `zero_grad` 只在整个 batch 结束后调用（[train.py:261](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L261)），所以循环外读一次拿到的就是全 batch 之和。

**练习 2**：`t_gradient_accum` 累加的是带符号的梯度值。若两个视角的 `_t` 梯度分别是 +0.01 与 −0.01，累加结果是多少？这对「时间梯度统计」意味着什么？

**答案**：累加为 0。两个视角把时间中心往相反方向拉时，带符号求和会互相抵消，统计上看不出任何敏感性。这与 viewspace 统计「先取范数再求和、永不抵消」的口径不同，是原实现的取舍——考虑到 `grads_t` 当前并未被 clone/split 消费，这个口径差异目前没有实际影响，但若你想启用时间梯度致密化（比如把 `grads_t` 的范数加进选点条件），就需要先决定是否改为按范数累积。

**练习 3**：如果把 [train.py:181](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L181) 的 `.detach()` 去掉（保留 `.clone()`），程序还能正常跑吗？行为有变化吗？

**答案**：能跑，行为也基本不变。`_t.grad` 本身已经不在计算图内（梯度张量不是 autograd 追踪的叶子），`.detach()` 在这里是保守写法，保证后续的原位赋值不会引起任何 autograd 报错（对 `requires_grad=True` 的张量做原位修改在某些版本会报错；`clone()` 出来的张量继承 `requires_grad` 标志，而 `_t.grad` 的 `requires_grad` 为 False，所以去掉 `.detach()` 也安全）。真正不可省的是 `.clone()`，见 4.3.4 的实验。

## 5. 综合实践

**任务**：用一个小张量实验完整复现 train.py 的合并公式，验证「3 个高斯、`batch_size = 4`、`visibility_count` 分别为 4/2/1 时，归一化后的统计量恰好等于每个高斯在其可见视角上的平均贡献」，并解释这为什么让致密化阈值公平。

**实验设计**（示例代码，纯 CPU 可运行，不依赖 CUDA 扩展）：

```python
# 示例代码：复现 train.py 的 batch 梯度合并
import torch

B = 4                                    # batch_size
N = 3                                    # 3 个高斯
# 每个高斯在每个视角的"原始屏幕梯度范数"（不可见视角为 0）
# G0 在 4 个视角全部可见；G1 只在视角 0、2 可见；G2 只在视角 1 可见
raw = torch.tensor([
    [1.0, 2.0, 3.0, 4.0],                # G0: c=4
    [0.5, 0.0, 0.7, 0.0],                # G1: c=2
    [0.0, 0.9, 0.0, 0.0],                # G2: c=1
])

# ---- 第 1 步：模拟 loss/B 之后收集的 per-view 范数（对应 train.py L150）----
per_view = raw / B                       # backward 前 loss 除以了 batch_size

# ---- 第 2 步：模拟合并段（对应 train.py L172-L178）----
visibility = raw > 0                     # per-view visibility_filter
visibility_count = visibility.sum(1)     # (N,)
merged_filter = visibility_count > 0
grad_sum = per_view.sum(1)               # stack(...,1).sum(1)
normalized = grad_sum.clone()
normalized[merged_filter] = normalized[merged_filter] * B / visibility_count[merged_filter]

# ---- 第 3 步：直接计算"可见视角上的平均"，作为对照 ----
direct_mean = raw.sum(1) / visibility_count

# ---- 第 4 步：不做归一化的版本，观察偏差 ----
unnormalized = grad_sum

tau = 0.5                                # 假想的致密化阈值（放大版，便于观察）
print("visibility_count:", visibility_count.tolist())
print("normalized      :", normalized.tolist())
print("direct_mean     :", direct_mean.tolist())
print("unnormalized    :", unnormalized.tolist())
print("normalized 过阈值:", (normalized >= tau).tolist())
print("不归一化 过阈值  :", (unnormalized >= tau).tolist())
```

**需要观察的现象**：

1. `normalized` 与 `direct_mean` 逐项相等。
2. `unnormalized` 中 `c < B` 的高斯数值被压低。
3. 在阈值 0.5 下，两个版本的「过阈值」判定对 G1、G2 给出不同结论。

**预期结果**（可手算复核，待本地验证）：

| 高斯 | c | \(\sum_v\lVert h_v\rVert\) | normalized = direct_mean | unnormalized (\(\times\frac{c}{B}\) 偏差) | 归一化后 ≥ 0.5 | 不归一化 ≥ 0.5 |
| --- | --- | --- | --- | --- | --- | --- |
| G0 | 4 | 10.0 | **2.5** | 2.5（无偏差，\(c=B\)） | 是 | 是 |
| G1 | 2 | 1.2 | **0.6** | 0.3（½×） | 是 | **否（漏判）** |
| G2 | 1 | 0.9 | **0.9** | 0.225（¼×） | 是 | **否（漏判）** |

**分析要求**：用 200 字左右回答——`× batch_size / visibility_count` 之后，阈值 \(\tau\) 的语义对三个高斯统一为「被看见时平均每次的屏幕梯度范数」；不归一化时统计量是 \(\frac{c}{B}\) 倍的折扣，越少见的高斯折扣越重，导致 G1、G2 这类「少见但误差大」的高斯永远跨不过阈值，致密化资源只会流向本就被频繁看见的区域。这就是归一化对致密化阈值的公平性：**判据衡量的是高斯被观测时的误差敏感度，而不是它被观测的频率**。

**延伸（可选）**：把 `raw` 改成带符号的一维「时间梯度」（允许正负），按 [train.py:181-182](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L181-L182) 的 `_t` 路径（先整体求和再归一化、不取范数）重做实验，观察正负抵消现象，与 4.3.5 练习 2 的结论对照。

## 6. 本讲小结

- `batch_size = B` 时一次 iteration = B 次 `render → loss → (loss/B).backward()`，靠 PyTorch「backward 累加、batch 末统一 `zero_grad`」的语义实现梯度累积；参数拿到的是 batch 平均梯度。
- 合并规则三件套：`visibility_filter` 取「或」（`visibility_count > 0`）、`radii` 取 `max`（最坏屏幕尺寸）、viewspace 梯度「逐视角取范数 → 求和 → ×B/count」。
- \(\frac{B}{c}\cdot\frac{1}{B}\sum_{v\in\mathrm{vis}} g_v = \frac{1}{c}\sum_{v\in\mathrm{vis}} g_v\)：归一化把统计量还原为「可见视角上的平均」，使 `densify_grad_threshold` 对所有高斯含义一致——这是致密化阈值的公平性来源。
- `add_densification_stats`（B==1，函数内取范数）与 `add_densification_stats_grad`（B>1，接收预归一化梯度）是同一统计的两个入口；`denom` 数的是 iteration 次数且只对可见高斯递增，`accum/denom` 因而是「被统计的那些 iteration 上的平均」。
- 4D 的 `_t` 梯度走同款归一化，但读取时机（循环外读持久参数 `.grad` 一次）、求和方式（带符号向量和不取范数）、以及 `.clone()` 防写穿三点不同；归一化只影响致密化统计，不影响 optimizer 使用的梯度。
- 如实提醒：`grads_t` / `densify_grad_t_threshold` 在本仓库的 `densify_and_clone/split` 函数体中未被消费，时间梯度统计目前是「被计算但不被使用」的预留接口。

## 7. 下一步学习建议

- **u5-l4（自适应致密化与剪枝）**：本讲的 `grads` 交给 `densify_and_clone/split` 之后发生了什么——clone 与 split 的判定条件差异、`rot_4d` 时在 xyzt 四维空间采样新位置、`cat_tensors_to_optimizer` 如何同步 Adam 状态。
- **u5-l5（检查点与日志）**：TensorBoard 里 `total_points`、`opacity_histogram` 曲线如何反过来验证本讲的统计口径。
- **u6-l4（联合优化）**：开启 `opacity_decay` 后 `densify_until_iter` 被拉长到全程、`reset_opacity` 被禁用，本讲的统计窗口因此发生变化，值得带着本讲的理解重读 [train.py:246-256](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L246-L256)。
- 动手方向：试着把 `grads_t` 的范数接进 `densify_and_split` 的选点条件（需先解决 4.3.5 练习 2 的口径问题），在小型数据上对比高斯数量曲线与测试 PSNR——这会是一次小而完整的二次开发练习。
