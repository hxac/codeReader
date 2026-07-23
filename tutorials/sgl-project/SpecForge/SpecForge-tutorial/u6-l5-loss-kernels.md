# 损失与核心算子

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 EAGLE3 草稿模型每一步损失到底在最小化什么，以及为什么这等价于「让草稿分布逼近教师分布」。
- 读懂 `core/loss.py` 里那个用 Triton 写的融合 `LogSoftmaxLoss`：它如何把前向的 log-softmax、加权求和与反向梯度熔进一个 kernel，省掉 `[B,T,V]` 的中间 fp32 张量。
- 读懂 `core/lk_loss.py` 的 LK 损失：它如何把「接受率」这个推理期指标直接写进训练目标，分 `alpha` 与 `lambda` 两种模式。
- 读懂 `core/compact_teacher.py` 的分块教师投影：为什么它能让 EAGLE3 离线训练的峰值显存大幅下降、却完全不改变教师分布，以及它为何只支持离线。
- 了解 `chunking.py` 与 `eagle3_adapters.py` 在整个损失/算子体系里的辅助定位。

本讲是 u6-l2（训练策略 `DraftTrainStrategy`）的下游：策略把 batch 交给草稿模型前向、产出若干步 logits 后，最终「把 logits 变成一个标量 loss」这件事，就发生在 `specforge/core/` 这几个文件里。

## 2. 前置知识

阅读本讲前，请先建立以下直觉（相关术语在 u1-l3、u1-l4、u6-l2 已铺垫，这里只做最简回顾）：

- **目标模型 / 教师与草稿模型 / 学生**：SpecForge 用目标模型当老师、训练一个小草稿模型当学生。学生要学的，是老师在每一个位置上的「下一个 token 概率分布」。
- **KL 散度与交叉熵**：让两个分布 `p`（教师）和 `q`（草稿）变近，最常用的目标是最小化 \(\mathrm{KL}(p\|q)=\sum_i p_i\log(p_i/q_i)\)。由于教师分布 \(p\) 在训练时是固定的，最小化 \(\mathrm{KL}(p\|q)\) 与最小化交叉熵 \(H(p,q)=-\sum_i p_i\log q_i\) 只差一个常数，所以二者等价。
- **log-softmax**：直接算 `softmax` 再取 `log` 在数值上容易下溢；工程上总是先算 \(\log q_i = x_i - m - \log\sum_j e^{x_j-m}\)（\(m=\max_j x_j\)），既稳定又省一次指数。
- **接受率（acceptance rate）**：投机解码里草稿 token 被目标模型接受的概率。u1-l3 给出过拒绝采样的接受概率公式，本讲会把它升级成「逐 token 期望接受率」并写进损失。
- **Triton**：OpenAI 开源的 GPU kernel DSL，可以在 Python 里写接近手写 CUDA 的高性能 kernel，SpecForge 用它把损失的前向+反向融合在一起。

如果你对上面任意一项陌生，建议先回看对应讲义；本讲会直接使用这些概念。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `specforge/core/` 这个「共享训练数学与后端适配」包里：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [specforge/core/loss.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py) | Triton 融合 `LogSoftmaxLoss`（蒸馏损失主体） | 最小模块 1 |
| [specforge/core/lk_loss.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/lk_loss.py) | 接受率计算与 LK 损失（`alpha`/`lambda`） | 最小模块 2 |
| [specforge/core/compact_teacher.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py) | 分块教师投影（不物化全词表 logits） | 最小模块 3 |
| [specforge/core/chunking.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/chunking.py) | 通用「分块累加 + 激活检查点」规约工具 | 辅助算子 |
| [specforge/core/eagle3_adapters.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py) | SDPA / USP 后端的张量视图与分布式归约适配 | 辅助算子 |
| [specforge/algorithms/eagle3/model.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py) | EAGLE3 在线/离线前向；调用上面三者算损失 | 消费方 |
| [specforge/training/strategies/base.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py) | `Eagle3TrainStrategy`：装配 compact teacher 入口 | 消费方 |
| [specforge/benchmarks/benchmark_loss.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/benchmark_loss.py) | Triton vs PyTorch 参考实现的速度/显存基准 | 实践依据 |

一个总览：`loss.py` 负责「把 logits 压成一个标量蒸馏 loss」，`lk_loss.py` 在它基础上叠加「接受率」目标，`compact_teacher.py` 负责「在算 loss 之前，用更省显存的方式把教师分布造出来」。三者在 [specforge/algorithms/eagle3/model.py:47-97](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L47-L97) 的 `_compute_loss_and_acceptance_rate` 里被串成一句话。

## 4. 核心概念与源码讲解

### 4.1 LogSoftmaxLoss：融合 log-softmax 蒸馏损失

#### 4.1.1 概念说明

EAGLE3 草稿模型在 TTT（训练时测试，见 u1-l4）的每一步都会输出一份 logits，记作 `q` 的未归一化形式。教师的下一 token 分布是 `p`。我们要最小化：

\[
\mathcal{L} = -\frac{1}{BT}\sum_{b,t}\,\mathbb{1}_{(b,t)\in\text{valid}}\sum_{i} p_{b,t,i}\,\log q_{b,t,i}
\]

其中 `position_mask` 标出哪些 token 位置是有效的（assistant 区间，见 u5-l2 的 loss mask），`B` 是 batch、`T` 是序列长、`V` 是（草稿）词表大小。

这就是交叉熵 \(H(p,q)\)，等价于最小化 \(\mathrm{KL}(p\|q)\)。它的朴素 PyTorch 实现在文件开头就给出了——一份 `@torch.compile` 的参考实现 [specforge/core/loss.py:15-21](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L15-L21)：

```python
@torch.compile(dynamic=None)
def _compute_loss(logits, target_p, position_mask):
    logits = logits.float()
    out_logp = nn.LogSoftmax(dim=2)(logits)
    plogp = target_p * out_logp
    loss = -torch.sum(position_mask * plogp, 2).mean()
    return loss
```

这段代码语义正确，但它会物化多个 `[B,T,V]` 的 fp32 中间张量（`log_softmax` 的输出、`plogp`、`position_mask*plogp`）。当 `V` 是几万、`T` 是几千时，这是巨大的显存开销——而 EAGLE3 每个 TTT step 都要算一次、默认连走 7 步。

`LogSoftmaxLoss` 就是为「干掉这些中间张量」而生的：它把 log-softmax、加权求和、mask、reduce 全部熔进一个 Triton kernel，前向只产出每个 `(b,t)` 的标量 loss，反向直接把梯度写回 logits。

#### 4.1.2 核心流程

记单行 logits 为 \(x\in\mathbb{R}^{V}\)、教师分布 \(p\)、位置 mask \(m\in\{0,1\}\)。前向要算的是：

\[
\text{loss}_{b,t} = -m_{b,t}\sum_i p_i \log q_i,\qquad
\log q_i = x_i - m^* - \log d
\]

其中数值稳定项 \(m^*=\max_i x_i\)、\(d=\sum_i e^{x_i-m^*}\)。最后对所有 `B*T` 个位置取均值。

kernel 用经典的「**online softmax**」单遍扫描（参考文件头注里点名的 Liger-Kernel / Unsloth 思路）：边扫边维护当前最大值 \(m\) 与归约和 \(d\)，每读入一个 block 就把历史值重新定标（rescale）：

\[
m_{\text{new}}=\max(m,\text{block\_max}),\qquad
d \leftarrow d\cdot e^{m-m_{\text{new}}}+\sum_{\text{block}}e^{x_i-m_{\text{new}}}
\]

扫描两遍：第一遍求 \(m^*\) 与 \(d\)，第二遍用它们算 \(\log q_i\)、加权求和得到 loss。反向则利用链式法则：

\[
\frac{\partial \text{loss}}{\partial x_i}\Big|_{b,t} \propto -m_{b,t}\big(p_i\cdot g - q_i\cdot\textstyle\sum_j p_j g\big)
\]

（其中 \(g\) 是上游传回的 `grad_output`，已含 `1/(B*T)` 缩放。）关键巧思是：反向需要 \(m^*\)、\(d\) 与「\(p\) 的加权和」这三样中间量，前向把它们存进 `ctx.save_for_backward`，反向直接复用，从而反向也只扫两遍、不再物化大张量。

整体调用形态（来自 EAGLE3 模型）是 [specforge/algorithms/eagle3/model.py:74](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L74)：

```python
kl_loss = LogSoftmaxLoss.apply(logits, target_p, position_mask)
```

#### 4.1.3 源码精读

**前向 kernel**：两遍扫描求 \((m^*, d)\) 再求 loss。`position_mask==0` 的位置提前 `return`，根本不算（省时间）：

```python
position_mask = tl.load(position_mask_ptr)
if position_mask == 0:
    return
```
见 [specforge/core/loss.py:68-70](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L68-L70)。online softmax 的 rescale 在 [specforge/core/loss.py:75-86](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L75-L86)，第二遍加权求 loss 在 [specforge/core/loss.py:88-108](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L88-L108)，最后把 `loss`、`m`、`d` 三个标量写回（每个 program 处理一个 `(b,t)` 行）。

**`_calculate_settings`** 按 `V` 选 `BLOCK_SIZE`（≥V 的最近 2 的幂）和 `num_warps`，并在 ROCm 上把 warp 数减半（AMD GPU 特性）：

```python
if hasattr(torch.version, "hip") and torch.version.hip is not None:
    num_warps //= 2
```
见 [specforge/core/loss.py:43-44](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L43-L44)。`V` 超过 `MAX_FUSED_SIZE=131072` 会直接报错 [specforge/core/loss.py:29-32](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L29-L32)。

**`LogSoftmaxLoss.forward`** 把 `[B,T,V]` 拍成 `[B*T, V]`，以每个行作为一个 program（`grid=(B*T,)`），分配 `m`、`d` 两个 `[B*T]` 缓冲，调前向 kernel，最后 `loss.squeeze(1).mean()` 得标量 [specforge/core/loss.py:175-201](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L175-L201)。注意它 `save_for_backward(logits.detach(), target, position_mask, m, d)`——把 kernel 算出的 `m`/`d` 存下来给反向复用。

**`backward`** 读回 `m`/`d` 与 `grad_output`，按 `scaling_factor=1/(B*T)` 缩放后跑两遍扫描写回梯度，返回 `(logits, None, None)`——只对 logits 有梯度，target 与 mask 不反传 [specforge/core/loss.py:203-228](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/loss.py#L203-L228)。

> 小贴士：这正是 u6-l2 里强调的「算法差异收敛进策略插件」之外的另一层收敛——**数值实现差异收敛进 `core/`**。策略只调一行 `LogSoftmaxLoss.apply(...)`，不必关心 kernel 细节。

#### 4.1.4 代码实践

**实践目标**：验证 Triton `LogSoftmaxLoss` 与 PyTorch 参考实现 `_compute_loss` 在数值上等价（前向 loss 与反向梯度都对齐），并直观感受显存差距。

**操作步骤**：

1. 打开 [tests/test_utils/test_loss.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_utils/test_loss.py)，阅读 `TestLogSoftmaxLoss._test_loss_and_gradient_calculation`（[第 13-30 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_utils/test_loss.py#L13-L30)）：它对同一份 `logits` 同时跑 `LogSoftmaxLoss.apply` 与 `_compute_loss`，用 `torch.testing.assert_close(rtol=1e-4, atol=1e-4)` 比对前向值与 `.backward()` 后的 `logits.grad`。
2. 运行该测试：`python -m pytest tests/test_utils/test_loss.py -v`（无 GPU 时测试会自动落到 CPU，见文件里 `device = "cuda" if torch.cuda.is_available() else "cpu"`）。
3. 想看「省了多少显存」，运行基准脚本 [specforge/benchmarks/benchmark_loss.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/benchmark_loss.py)：`python -m specforge.benchmarks.benchmark_loss`（需 CUDA）。它会打印一张 `PyTorch vs Triton` 的时间/峰值显存对照表。

**需要观察的现象**：

- 测试通过，说明两条路径在 `1e-4` 容差内一致。
- 基准表里 `V` 越大、`T` 越大，Triton 相对 PyTorch 的 `Memory Save` 百分比越高——这正是融合 kernel 砍掉 `[B,T,V]` fp32 中间张量带来的收益。

**预期结果**：测试 `PASS`；基准表里 `Triton Mem (GB) < PyTorch Mem (GB)`，`Speedup > 1`。若你当前环境没有 GPU，基准脚本会在 `CUDA not available` 分支打印提示并无法给出有意义的显存数字——**这种情况属于「待本地验证」**，可先靠阅读测试断言确认数值正确性。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LogSoftmaxLoss` 把 `[B,T,V]` 拍成 `[B*T, V]` 再以「每行一个 program」的方式启动？换成「一个 program 处理整个 batch」会有什么问题？

> **参考答案**：每行（一个 token 位置）的 log-softmax 只依赖本行的 `V` 个元素，行与行之间完全独立，所以一行一个 program 天然可并行、且每个 program 的工作量就是一次 `V` 维规约，便于按 `V` 选 `BLOCK_SIZE`。若一个 program 处理整个 batch，要么得在 kernel 内做二层循环、浪费并行度，要么 `BLOCK_SIZE` 要覆盖 `T*V` 远超 `MAX_FUSED_SIZE` 而无法启动。

**练习 2**：前向 kernel 里第一遍扫描已经求出了 `m` 和 `d`，为什么还要有第二遍扫描？

> **参考答案**：online softmax 是「单遍求 \(m^*\) 与 \(d\)」，但最终 loss 需要 \(\log q_i = x_i - m^* - \log d\) 对每个 \(i\) 的值并加权求和 \(\sum_i p_i\log q_i\)。\(m^*\) 与 \(d\) 要等整行扫完才确定，所以必须带着最终值重扫一遍把每个 \(i\) 的 \(\log q_i\) 算出来。这就是经典的两遍 softmax 结构。

**练习 3**：`backward` 返回的三元组 `(logits, None, None)` 里两个 `None` 分别对应哪个输入？为什么是 `None`？

> **参考答案**：分别对应 `target` 与 `position_mask`。因为教师分布 `target` 是 detached 的常数（不更新教师），`position_mask` 只是 0/1 选择开关、其对 loss 的「导数」没有意义也不需要回传，所以这两路梯度为 `None`。

---

### 4.2 LK 损失：把「接受率」写进训练目标

#### 4.2.1 概念说明

`LogSoftmaxLoss` 让草稿分布逼近教师分布（最小化 KL），这是 EAGLE3 的默认目标（`lk_loss_type=None` 时直接用 `kl_loss`）。但 KL 是一个「形状」指标——它和投机解码真正关心的「**草稿 token 被接受多少**」并不完全等价。

LK 损失的想法是：既然推理期的加速取决于接受率，那干脆把一个「可微的期望接受率」估计直接加进损失。这需要两步：

1. 用草稿分布 `q` 与教师分布 `p` 估计「逐 token 期望接受率」。
2. 用它构造一个新损失（`alpha` 或 `lambda`），与 KL 组合。

这正是 [specforge/core/lk_loss.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/lk_loss.py) 的全部内容。

#### 4.2.2 核心流程

**期望接受率**：拒绝采样下（u1-l3），草稿按 \(q\) 采样出 token \(i\)、目标按 \(\min(1, p_i/q_i)\) 决定接受。对 \(i\) 求期望，单 token 被接受的概率为：

\[
\text{acc} = \sum_i q_i\cdot\min\!\Big(1,\frac{p_i}{q_i}\Big)=\sum_i \min(p_i, q_i)
\]

代码里就是 [specforge/core/lk_loss.py:7-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/lk_loss.py#L7-L17) 的 `expected_acceptance_rate`，一行 `torch.minimum(target_probs, draft_probs).sum(dim=-1)`。注意它是可微的（对 `draft_probs` 有梯度），所以能进反向。

**两种 LK 目标**（`compute_lk_loss`，[specforge/core/lk_loss.py:83-99](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/lk_loss.py#L83-L99)）：

- `alpha`：直接最大化期望接受率，等价于最小化
  \[
  \mathcal{L}_{\alpha} = -\log(\text{acc})
  \]
  取 log 是为了让「接受率从 0.5→0.6」和「0.9→0.95」这样的边际改进被不同力度地奖励。
- `lambda`：自适应地把 KL 与「1-acc」混起来：
  \[
  w = \text{kl\_scale}\cdot e^{-\text{kl\_decay}\cdot \text{acc}},\qquad
  \mathcal{L}_{\lambda} = w\cdot \mathcal{L}_{\text{KL}} + (1-w)\cdot(1-\text{acc})
  \]
  关键在 \(w\) 随 `acc` 增大而减小：接受率还低时多靠 KL 拉形状，接受率高了就转向直接奖励接受率。

**梯度开关**：在 EAGLE3 里，接受率项只在 `lk_loss_type is not None` 时才需要梯度；否则 `compute_acceptance_rate` 被包在 `torch.set_grad_enabled(lk_loss_type is not None)` 下，仅当监控指标用（见 [specforge/algorithms/eagle3/model.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L78-L84)）。

**配置入口**：三个旋钮在 schema 的 `training` 段 [specforge/config/schema.py:506-508](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L506-L508)：`lk_loss_type`（`None`/`"lambda"`/`"alpha"`）、`kl_scale`、`kl_decay`。

#### 4.2.3 源码精读

`_masked_mean` [specforge/core/lk_loss.py:20-40](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/lk_loss.py#L20-L40) 是把逐 token 值聚合成标量的通用工具：分子是 `(values*mask).sum()`、分母是 `mask.sum().clamp_min(eps)`。它接一个可选 `reduce_fn`——这是 USP/DP 并行时把跨卡的分子分母 all-reduce 到一起的钩子（见 u8 分布式），单卡时为 `None`。

`compute_acceptance_rate` [specforge/core/lk_loss.py:52-80](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/lk_loss.py#L52-L80) 同时返回两个标量：`acceptance_rate`（均值接受率）与 `log_acceptance_rate`（先逐 token 取 log 再均值——注意是「log then mean」，不是「mean then log」，由测试 `test_compute_acceptance_rate_log_before_mean` 锁定）。

`compute_lk_loss` [specforge/core/lk_loss.py:83-99](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/lk_loss.py#L83-L99) 是一个纯函数分发器，`alpha`/`lambda`/其它分别走三条路，未知类型抛 `ValueError`。注意 `lambda` 分支里 `acc_det = acceptance_rate.detach()`——自适应权重 \(w\) 自己不参与反传，只作为调度系数。

整个组装点在 [specforge/algorithms/eagle3/model.py:47-97](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L47-L97)：先 `LogSoftmaxLoss.apply` 得 `kl_loss`，再按需 `compute_acceptance_rate`，最后 `lk_loss_type is None` 时直接用 `kl_loss`，否则 `compute_lk_loss` 组合。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `expected_acceptance_rate` 的公式与 `compute_lk_loss` 两种模式的行为，不依赖 GPU。

**操作步骤**：

1. 打开 [tests/test_utils/test_lk_loss_utils.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_utils/test_lk_loss_utils.py)。
2. 看 `test_expected_acceptance_rate`（[第 14-25 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_utils/test_lk_loss_utils.py#L14-L25)）：目标 `[[0.7,0.3],[0.1,0.9]]`、草稿 `[[0.6,0.4],[0.2,0.8]]`，断言接受率等于 `[[0.9,0.9]]`。自己在纸上算：\(\min(0.7,0.6)+\min(0.3,0.4)=0.6+0.3=0.9\) ✓。
3. 看 `test_compute_lk_loss_alpha`（[第 112-123 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_utils/test_lk_loss_utils.py#L112-L123)）：断言 `alpha` 模式的 loss 恰好等于 `-log_acceptance_rate`。
4. 运行：`python -m pytest tests/test_utils/test_lk_loss_utils.py -v`（纯 CPU、几秒结束）。

**需要观察的现象**：所有用例 PASS；尤其确认 `log_acceptance_rate` 是「先逐 token 取 log、再 masked mean」。

**预期结果**：测试全绿。你可以再自己写一个三行小脚本，构造 `p==q` 的情形，确认接受率为 1.0、`alpha` loss 为 0（草稿完美等于教师时损失最小）。

#### 4.2.5 小练习与答案

**练习 1**：当草稿分布与教师分布完全相同（\(q=p\)）时，`expected_acceptance_rate` 等于多少？

> **参考答案**：\(\sum_i \min(p_i,p_i)=\sum_i p_i=1\)。完美匹配时接受率为 1，`alpha` 损失 \(-\log 1=0\)，与「损失越小越好」的直觉一致。

**练习 2**：`lambda` 模式里 `acc_det = acceptance_rate.detach()`，如果漏掉这个 `.detach()` 会出什么问题？

> **参考答案**：权重 \(w\) 本来只起「调度 KL 与接受率两项比例」的作用，不应被优化器当作目标去最小化。若不 detach，\(w\) 会带上梯度，等于让优化器去「直接调权重」而非「调分布」，目标语义被污染、梯度方向也可能错乱。`.detach()` 把 \(w\) 钉成纯调度系数。

**练习 3**：`compute_acceptance_rate` 为什么同时返回 `acceptance_rate` 和 `log_acceptance_rate` 两个标量，而不是只返回一个？

> **参考答案**：因为 `alpha` 模式要的是 \(-\log(\text{acc})\)，而「先逐 token 取 log 再均值」与「先均值再取 log」数学上不同。直接返回正确口径的 `log_acceptance_rate` 能让 `compute_lk_loss` 一行 `-log_acceptance_rate` 就拿到正确目标，避免下游用错聚合顺序。

---

### 4.3 compact teacher：分块教师投影

#### 4.3.1 概念说明

回到损失公式 \(\mathcal{L}=-\sum_i p_i\log q_i\) 里的教师分布 \(p\)。在 EAGLE3 的**离线**训练中，样本存的是目标模型的「最后一层隐藏状态」`hidden`（见 u5-l3 的 `last_hidden_states`）。要得到 \(p\)，得再过一次目标模型的 `lm_head`：

\[
\text{logits} = \text{hidden}\cdot W_{\text{lm\_head}}^\top,\qquad p=\text{softmax}(\text{logits})
\]

问题：`lm_head` 的全词表 `V` 通常很大（几万到十几万），`[B,S,V]` 的 fp32 logits 是个巨大的张量。EAGLE3 还要在 TTT 多步、可能多卡上反复用它。

`compact_teacher.py` 解决的就是这个「教师 logits 太大」的问题。它的关键洞察来自文件头注 [specforge/core/compact_teacher.py:3-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L3-L17)：EAGLE3 其实只在**草稿词表**（draft vocab）空间里训练（通过 `t2d` 掩码切片，见 u4-l4）。所以：

- 草稿词表那部分 logits，只需 `F.linear(hidden, W[t2d])`，产出 `[B,S,draft_vocab]`（小）。
- 全词表的归一化常数（logsumexp）与 argmax，可以**分块流式累加**得到，全程不物化 `[B,S,V]`。

这样得到的教师量与「物化全词表再 softmax」在 fp 容差内**完全一致**——它不是近似，是把同一个数学公式换了个省内存的求值顺序。文件把它称为「reproduces the teacher quantities ... without materializing the full `[B,S,vocab_size]` fp32 logits」。

#### 4.3.2 核心流程

`compute_target_from_hidden` 要产出四件教师量 [specforge/core/compact_teacher.py:106-150](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L106-L150)：

1. **`target_p`**：草稿词表空间内的 softmax 分布，供 KL 损失用。来自 `F.linear(hidden, W[t2d])` 再 softmax。
2. **`target_p_on_draft`**：原目标分布在草稿词表上的（未重归一化）概率，供接受率指标用。等于 \(\exp(\text{draft\_logits}-\log Z)\)，其中 \(\log Z\) 是**全词表** logsumexp。
3. **`target_token_ids`**：全词表 argmax（教师最可能的 token），用来构造 `position_mask`。
4. **`position_mask`**：`t2d[target_token_ids] * loss_mask`——只有当教师 top token 落在草稿词表内、且该位置是有效 assistant token 时，才参与损失。

其中第 2、3 件都依赖「全词表」信息，靠 `tiled_logsumexp_argmax` 流式求出：

\[
\log Z = m^* + \log\sum_{\text{chunks}} e^{\text{chunk\_logits}-m^*},\qquad
\text{argmax}=\arg\max_{\text{chunks}}\text{chunk\_logits}
\]

分块大小默认 `DEFAULT_VOCAB_CHUNK_SIZE=32768`（[第 24 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L24)），即一次只物化 `[B,S,32768]` 而非 `[B,S,V]`。

**为什么是离线专属**：文件头注与 `validate_compact_teacher_enabled` 都明确——教师头用的是**完整未分片**的 `lm_head` 权重（每个 rank 都拿到同样的 `W`、算出同样的教师量），所以无法与目标模型的在线张量并行（TP）协作。在线捕获时教师 logits 由外部 SGLang 服务出，根本不经过这条路径。故 `is_online=True` 时直接抛错（[第 248-252 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L248-L252)）。

**为什么省显存**：朴素路径要存 `[B,S,V]` fp32 logits（还要上 softmax/log_softmax 的中间张量）。compact 路径峰值只有 `[B,S,draft_vocab]` + `[B,S,chunk]` 两块小张量，draft_vocab 通常只有几千。

#### 4.3.3 源码精读

**`tiled_logsumexp_argmax`** [specforge/core/compact_teacher.py:56-103](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L56-L103) 是核心算法。它对词表维度分块扫描，维护 `running_max`、`running_sumexp`、`running_argmax` 三组累加器。注意这段「**严格大于才更新**」的注释 [第 95-100 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L95-L100)：

```python
# Ascending scan + strict-greater update keeps the lowest global index on ties.
take = chunk_val > running_argval
```

它保证「平局取最小下标」，与 `torch.argmax` 的语义对齐——这是「等价而非近似」的一个细节。

**`compute_target_from_hidden`** [specforge/core/compact_teacher.py:106-150](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L106-L150)：先用 `W[t2d]` 算草稿 logits 与 `target_p`，再调 `tiled_logsumexp_argmax` 拿全词表 \(\log Z\) 与 argmax，进而得 `target_p_on_draft` 与 `position_mask`。全部 `.detach()`——教师量不参与反传。

**`compute_target_p_padded_from_hidden`** [specforge/core/compact_teacher.py:153-189](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L153-L189)：在上者基础上做 TTT 用的 padding（见 u1-l4 的 TTT 位移），把序列尾补 `length` 个位置，pad 常数与朴素路径 `_compute_target_p_padded` 完全一致——这是「等价」的又一处保证。

**`build_offline_teacher_inputs`** [specforge/core/compact_teacher.py:192-214](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L192-L214)：是策略层（u6-l2）与 compact 数学之间的翻译层。`compact=False` 时返回 `(target_model(hidden), {})`（朴素全 logits）；`compact=True` 时**不调用** `target_model.forward`，只取出 `target_model.fc.weight`（lm_head 权重）丢给模型去流式分块：

```python
return None, {
    "target_hidden_for_compact": target_hidden,
    "target_head_weight": target_model.fc.weight,
    "compact_teacher_chunk_size": chunk_size,
}
```

**校验三件套**：`validate_compact_teacher_config`（[第 27-53 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L27-L53)，要求 `draft_vocab_size < vocab_size`、`t2d` 是 bool、选中的 token 数等于 `draft_vocab_size`）、`validate_vocab_mapping_consistency`（[第 217-231 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L217-L231)，要求 `nonzero(t2d) == d2t + arange`）、`validate_compact_teacher_enabled`（[第 234-273 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L234-L273)，拒在线、拒缺权重、拒 chunk 非正）。

**策略层入口**：`Eagle3TrainStrategy` 构造时若 `compact_teacher=True` 就先跑 `_validate_compact_teacher`（[specforge/training/strategies/base.py:150-206](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L150-L206)）；`forward_loss` 里 compact 分支调 `build_offline_teacher_inputs` 把教师量交给模型（[第 240-262 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L240-L262)）。配置入口是 `training.compact_teacher` 与 `training.compact_teacher_chunk_size`（[specforge/config/schema.py:539-540](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L539-L540)），且 schema 强制「设了 chunk_size 就必须 `compact_teacher=true`」（[第 728-733 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L728-L733)）。

#### 4.3.4 代码实践

**实践目标**：读懂 `compact_teacher=true` 为何「降显存却不改教师分布」，并说清它拒绝哪些运行模式。

**操作步骤**：

1. 打开 [tests/test_runtime/test_compact_teacher_strategy.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_compact_teacher_strategy.py)。先看 `_reference_teacher`（[第 31-40 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_compact_teacher_strategy.py#L31-L40)）：它用「物化全词表 logits 再 softmax/logsumexp/argmax」的朴素方式造教师量，作为对照真值。
2. 在该文件里搜索对 `compute_target_from_hidden` / `tiled_logsumexp_argmax` 的断言（文件顶部已 import，[第 10-14 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_compact_teacher_strategy.py#L10-L14)），看它们如何与 `_reference_teacher` 做 `assert_close`。
3. 打开 [specforge/core/compact_teacher.py:248-252](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L248-L252) 与 [specforge/training/strategies/base.py:161-165](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L161-L165)，记录两条「被拒绝」的分支。
4. 运行：`python -m pytest tests/test_runtime/test_compact_teacher_strategy.py -v`（CPU 即可）。

**需要观察的现象**：

- compact 路径产出的 `target_p`、`target_p_on_draft`、`target_token_ids`、`position_mask` 与朴素 `_reference_teacher` 在容差内一致——这就是「不改变教师分布」的证据。
- `validate_compact_teacher_enabled(is_online=True, ...)` 与「缺 `target_head`」两种情况都抛 `ValueError`。

**预期结果**：测试 PASS。结合源码你能回答：compact teacher 拒绝「**在线训练（online）**」（教师头未分片、无法与在线 TP 协作）和「**没有 `target_head` 的情形**」（在线捕获时策略拿不到冻结的 lm_head，见 [strategies/base.py:161-165](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L161-L165)）。

> 「降显存」的定量对比需 GPU 与较大词表才明显，若本机无 GPU，**显存收益待本地验证**；数值等价性可由上面的 CPU 测试确认。

#### 4.3.5 小练习与答案

**练习 1**：用一句话说明 compact teacher 为什么能降低峰值显存，且为什么说它「不改变教师分布」？

> **参考答案**：它把「物化 `[B,S,V]` fp32 全词表 logits 再 softmax」换成「只算草稿词表 logits + 分块流式求全词表 logsumexp/argmax」，峰值从 `[B,S,V]` 降到 `[B,S,draft_vocab]+[B,S,chunk]`；但求的是同一组数学量（甚至 argmax 平局也按 `torch.argmax` 取最小下标），所以在 fp 容差内与朴素路径完全一致，不是近似。

**练习 2**：`tiled_logsumexp_argmax` 里 `take = chunk_val > running_argval`（严格大于）有什么作用？换成 `>=` 会怎样？

> **参考答案**：严格大于 + 升序扫描保证「多个 chunk 出现相同最大值时，保留下标最小的那个」，与 `torch.argmax` 的平局规则一致。若换成 `>=`，后扫到的 chunk 会覆盖前者，平局时会得到更大的下标，导致 `target_token_ids` 与朴素路径不一致，`position_mask` 也会跟着错。

**练习 3**：假如有人想给在线（online）EAGLE3 也开 compact teacher，会卡在哪一道校验？为什么这条限制是合理的？

> **参考答案**：会卡在 `validate_compact_teacher_enabled` 的 `is_online` 分支（[compact_teacher.py:248-252](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/compact_teacher.py#L248-L252)）与策略层「`target_head is None`」分支（[strategies/base.py:161-165](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L161-L165)）。合理之处在于：compact 的前提是手里有一份完整未分片的 `lm_head` 权重来本地算教师量，而在线的目标 logits 由外部 SGLang 服务（可能做了 TP 分片）产出，本地既没有也不该重算这份权重，因此 compact 只在离线、本地持有冻结 target head 时成立。

---

### 4.4 辅助算子：chunking 与 eagle3_adapters

这两个文件不直接算损失，但服务于「在显存/并行约束下算损失」这一目标，学习目标里要求「了解其辅助作用」。

#### 4.4.1 chunking.py：分块累加 + 激活检查点

`checkpointed_chunk_reduce` [specforge/core/chunking.py:15-101](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/chunking.py#L15-L101) 是一个通用的「把一个会把中间张量撑爆的 reduce，拆成沿某维分块、逐块累加」的工具：

- `chunk_size=0`：整块算一次、不做检查点（`chunk_size or length` 的短路，[第 55 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/chunking.py#L55)）。
- `chunk_size>0`：逐块调 `function`，把返回的多项张量逐元素相加；当梯度开启且某块输入需要梯度时，用 `torch.utils.checkpoint.checkpoint(..., use_reentrant=False)` 包住，省下反向时的中间激活。

它的定位与 compact_teacher 类似（都是「换求值顺序省显存」），但更通用：DFlash 家族那些「目标 logits 很大」的目标（u6-l2 提到的 `objective_chunk_blocks`）就用它。`chunking.py` 与本讲的损失主体 `loss.py` 是平级的两套省显存机制：前者是「通用分块」，后者是「专用 Triton 融合」。

#### 4.4.2 eagle3_adapters.py：后端视图与分布式归约

[specforge/core/eagle3_adapters.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py) 把「TTT 多步前向时，如何从全局张量切出第 `idx` 步的视图」抽象成 `BackendAdapter`：

- `SdpaLikeAdapter`（[第 57-95 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py#L57-L95)）：标准注意力，直接按 `idx:idx+seq_length` 切片，`reduce_*` 是恒等映射（单卡或非 SP）。
- `UspAdapter`（[第 98-159 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py#L98-L159)）：USP（Ulysses×Ring 序列并行，见 u8-l2）下，本地只持有序列的一段，`reduce_metrics` 通过 `sp_group` 做 all-reduce 把接受率指标的分子分母汇总——这正是 4.2 里 `_masked_mean` 的 `reduce_fn` 钩子的实现方。

换言之：损失/接受率公式本身（`loss.py`/`lk_loss.py`）与并行拓扑无关，分布式归约被收口进 adapter，通过 `reduce_fn`/`reduce_loss_fn` 回调注入。这与 u6-l2「算法差异收敛进策略」、本讲「数值实现差异收敛进 core」是同一种解耦哲学。

## 5. 综合实践

把本讲三个模块串起来，做一次「**读一条 EAGLE3 离线 step 的损失计算链**」的源码追踪：

1. **入口**：从 [specforge/training/strategies/base.py:231-298](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L231-L298) 的 `Eagle3TrainStrategy.forward_loss` 出发。分两种情况各画一条分支：
   - `compact_teacher=False`：走 `_prepare_target`（教师 logits 已在 batch 里）。
   - `compact_teacher=True`：走 `build_offline_teacher_inputs`（教师量由 hidden + lm_head 权重流式算出）。
2. **前向**：进入 [specforge/algorithms/eagle3/model.py:244](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L244) 的 `OnlineEagle3Model.forward`，注意它在 [第 279-306 行](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L279-L306) 据 `target_hidden_for_compact` 是否为 `None` 分流到 compact 或朴素教师构造，然后 TTT 连走多步。
3. **损失组装**：每一步 logits 都进 [specforge/algorithms/eagle3/model.py:47-97](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L47-L97) 的 `_compute_loss_and_acceptance_rate`——这里你能看到本讲三者的同框：`LogSoftmaxLoss.apply`（模块 1）→ `compute_acceptance_rate`（模块 2 的指标）→ 可选 `compute_lk_loss`（模块 2 的目标）。
4. **TTT 聚合**：回到 `forward_loss` 末尾，多步 loss 按 `ploss_decay` 几何衰减求和（u1-l4、u6-l2 已讲）。

**产出**：画一张包含上述节点的流程图，并在每个节点旁标注「调用了 `core/` 里哪个函数、输入输出张量形状、是否物化大张量」。重点标出三个「省显存」设计点：① `LogSoftmaxLoss` 融合前向/反向；② compact teacher 不物化全词表 logits；③ `chunking.py` 的通用分块（DFlash 用）。

> 若有 GPU，可额外做一次对比实验：用 [specforge/benchmarks/benchmark_loss.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/benchmark_loss.py) 量出 Triton 相对 PyTorch 参考实现的显存节省，把它作为「设计点①」的量化佐证贴进流程图。无 GPU 则该项**待本地验证**。

## 6. 本讲小结

- EAGLE3 的基础损失是交叉熵 \( -\sum_i p_i\log q_i \)，等价于最小化 \(\mathrm{KL}(p\|q)\)，即让草稿分布逼近教师分布。
- `core/loss.py` 的 `LogSoftmaxLoss` 用 Triton 把 log-softmax、加权、mask、reduce 与反向梯度熔进一个 kernel，靠 online softmax 两遍扫描 + 反向复用前向的 `m`/`d`，干掉了 `[B,T,V]` 的中间 fp32 张量。
- `core/lk_loss.py` 把推理期的「接受率」写成可微目标：`expected_acceptance_rate = sum min(p,q)`；`alpha` 模式最大化 \(-\log\text{acc}\)，`lambda` 模式用 \(w=\text{kl\_scale}\cdot e^{-\text{kl\_decay}\cdot\text{acc}}\) 自适应混合 KL 与接受率。
- `core/compact_teacher.py` 让离线 EAGLE3 不物化 `[B,S,V]` 全词表 logits：草稿词表部分用 `W[t2d]`，全词表 logsumexp/argmax 用 `tiled_logsumexp_argmax` 分块流式求，结果与朴素路径在 fp 容差内**完全一致**（含 argmax 平局规则）。
- compact teacher 是**离线专属**：它依赖本地完整未分片的 `lm_head` 权重，在线训练（教师 logits 由外部 SGLang 出）与缺 `target_head` 的情形都会被校验拒绝。
- `chunking.py`（通用分块+激活检查点）与 `eagle3_adapters.py`（SDPA/USP 视图与分布式归约）是辅助算子，体现了「损失公式与并行/显存策略解耦」的同一套设计哲学。

## 7. 下一步学习建议

- **横向对比各策略的损失**：回看 u6-l2 的 `DraftTrainStrategy`，把 EAGLE3（本讲的 TTT 多步 `plosses`）、DFlash（硬标签单标量，常配 `chunking.py`）、Domino（带 `linear_lambda_base` 衰减）三者的损失形态列成对照表，巩固「策略插件」心智。
- **进入分布式**：本讲多次出现的 `reduce_fn`/`reduce_metrics_fn` 钩子，其实现就在 `eagle3_adapters.UspAdapter` 与 u8 的 `distributed.py`。建议接着读 u8-l1（分布式初始化与设备网格）与 u8-l2（USP 与 ring attention），把「损失如何跨卡归约」补全。
- **工程化收尾**：损失算完就要存盘与评测。可继续 u9-l1（检查点与恢复，关注 `checkpoint_state_filter` 如何过滤草稿权重）与 u9-l4（导出/评测/基准），其中 `benchmarks/sglang.py` 会把本讲训出的草稿模型放到真实投机解码里度量加速比。
- **二次开发**：若你想新增一种损失目标，参考 u10-l2（新增训练算法）——在策略的 `forward_loss` 里调本讲的 `core/` 工具即可，无需改动 kernel。
