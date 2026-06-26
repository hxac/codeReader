# 训练循环增强：warmup / 余弦衰减 / 梯度裁剪

> 对应源码：`appendix-D/01_main-chapter-code/appendix-D.ipynb`（正文与训练代码）
> 汇总脚本：`appendix-D/01_main-chapter-code/previous_chapters.py`（复用第 2~5 章成品）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么大模型（LLM）训练时**不能一上来就用最大学习率**，并解释「学习率 warmup」如何缓解这个问题。
- 手写一段**线性 warmup** 代码，把学习率从一个极小值线性抬升到峰值。
- 用**余弦退火（cosine decay）** 让学习率在 warmup 之后沿半条余弦曲线平滑回落到接近 0。
- 理解**梯度裁剪（gradient clipping）** 的数学定义（全局 L2 范数）并调用 PyTorch 的 `clip_grad_norm_`。
- 把这三件套整合进第 5 章的 `train_model_simple`，得到附录 D 的增强版 `train_model`，并看懂它与原版的差异。

本讲是**训练工程（training engineering）** 的入门：模型结构（第 4 章）和损失函数（第 5 章）都不变，我们只动「怎么更新权重」这件事，却能让训练更稳、更收敛。

## 2. 前置知识

本讲承接 [u5-l2 训练循环 train_model_simple](./u5-l2-training-loop.md)，假设你已经熟悉下面这些概念：

- **训练四件套**：每个 batch 都执行 `optimizer.zero_grad()` → 前向算 `loss` → `loss.backward()` 反传梯度 → `optimizer.step()` 更新参数。
- **学习率（learning rate, lr）**：控制每步权重更新幅度的标量。lr 太大训练发散（loss 爆炸），lr 太小训练极慢。
- **`optimizer.param_groups`**：PyTorch 优化器内部用一个列表管理参数组，每个组是一个字典，`param_group["lr"]` 就是该组当前的学习率。**改学习率就是改这个字典字段**，而不是新建优化器。
- **梯度属性 `.grad`**：`backward()` 后，每个参数张量的 `.grad` 里存着它本次的梯度。梯度也是一个张量，和参数同形状。
- **AdamW 优化器**：自适应学习率 + 解耦权重衰减（`weight_decay`），是本书预训练的默认优化器。

一句话复习：第 5 章的 `train_model_simple` 用**恒定学习率**从头训练 10 个 epoch，结果训练损失一路下降、验证损失早早停滞——典型的小数据集过拟合。本讲不解决过拟合（那是数据量问题），而是给训练循环加上让大模型**真正能训得动**的三个稳定性技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [appendix-D.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb) | 附录 D 正文 notebook。分四节：D.1 warmup、D.2 余弦衰减、D.3 梯度裁剪、D.4 整合后的 `train_model`。本讲绝大部分源码都来自这里。 |
| [previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/previous_chapters.py) | 「精选成品汇总器」，把第 2~5 章会被复用的稳定类/函数收在一起。本讲用到其中的 `GPTModel`、`create_dataloader_v1`、`calc_loss_batch`、`evaluate_model`、`generate_and_print_sample`、`plot_losses`。 |

阅读约定（来自 [u1-l3](./u1-l3-repo-reading-map.md)）：notebook 里的 `from previous_chapters import ...` 就是「复用旧章成品」的信号；附录 D 的 `train_model` 正是站在第 5 章 `train_model_simple` 的肩膀上改造而来。

## 4. 核心概念与源码讲解

### 4.1 学习率线性 warmup

#### 4.1.1 概念说明

**问题**：训练刚开始时，模型权重是随机初始化的，它对「正确答案」几乎一无所知。如果第一步就用很大的学习率，单次梯度更新可能把权重甩到一个非常离谱的位置，loss 突然飙升甚至变成 `NaN`，整个训练就崩了。这在大模型里尤其常见，因为参数多、层数深，单步更新很容易放大误差。

**直觉**：就像开车——刚起步时轻踩油门，等速度稳了再加速，比一启动就地板油安全得多。

**学习率 warmup（预热）**：在训练最初的若干步（warmup 阶段），把学习率从一个极小值 `initial_lr` **线性**增大到目标峰值 `peak_lr`；warmup 结束后再进入正常训练（或配合下一节的余弦衰减）。

- 这样训练初期每步更新都很小，模型先在一个安全的「小步幅」区间里找到大致方向，再逐步放开步幅。
- warmup 步数通常占总步数的 0.1%~20%，本书取 20%。

#### 4.1.2 核心流程

设总训练步数为 `total_steps`，warmup 步数为 `warmup_steps`，则每步的「学习率增量」是：

\[
\text{lr\_increment} = \frac{\text{peak\_lr} - \text{initial\_lr}}{\text{warmup\_steps}}
\]

第 `global_step` 步（从 0 开始）的学习率为：

\[
\text{lr}(\text{global\_step}) = \text{initial\_lr} + \text{global\_step} \times \text{lr\_increment}, \quad \text{global\_step} < \text{warmup\_steps}
\]

伪代码：

```
warmup_steps = int(0.2 * total_steps)
lr_increment = (peak_lr - initial_lr) / warmup_steps

每个 batch:
    global_step += 1
    若 global_step < warmup_steps:
        lr = initial_lr + global_step * lr_increment   # 线性爬坡
    否则:
        lr = peak_lr                                   # (本节先恒定，下节改余弦)
    把 lr 写进 optimizer.param_groups[0]["lr"]
```

> 关键点：**学习率不是写死的，而是每一步重新算、再塞回优化器**。PyTorch 优化器在 `step()` 时会读取 `param_group["lr"]`，所以「动态改学习率」=「每步覆写这个字段」。

#### 4.1.3 源码精读

notebook 先算 warmup 步数（总步数的 20%）：

[:L264 — warmup 步数取总步数的 20%](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L264)

```python
total_steps = len(train_loader) * n_epochs
warmup_steps = int(0.2 * total_steps) # 20% warmup
```

> 说明：`len(train_loader)` 是每个 epoch 的 batch 数，乘以 `n_epochs` 得到总优化步数。本书数据极小，输出为 `warmup_steps = 27`（即 `total_steps = 135`，每 epoch 9 个 batch × 15 epoch）。

然后是 warmup 的演示循环（D.1 节，纯演示学习率轨迹、尚未真正训权重）：

[:L295-L298 — warmup 分支：线性爬坡，否则用峰值](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L295-L298)

```python
        if global_step < warmup_steps:
            lr = initial_lr + global_step * lr_increment
        else:
            lr = peak_lr

        # Apply the calculated learning rate to the optimizer
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr
```

> 说明：前 `warmup_steps` 步线性爬坡；之后（本节）直接用 `peak_lr`。`for param_group in optimizer.param_groups: param_group["lr"] = lr` 这行就是「把算好的学习率写回优化器」的标准写法——只有一个参数组时它等价于 `optimizer.param_groups[0]["lr"] = lr`。画出来的曲线是一条从 `initial_lr` 斜升到 `peak_lr` 再走平的折线（notebook 中 `1.pdf`）。

#### 4.1.4 代码实践

**目标**：单独验证 warmup 的学习率轨迹，不依赖训练。

**操作步骤**：

1. 打开 `appendix-D/01_main-chapter-code/appendix-D.ipynb`，从头运行到 D.1 节的绘图 cell（`plt.plot(range(total_training_steps), track_lrs)`）。
2. 把 `warmup_steps = int(0.2 * total_steps)` 临时改成 `int(0.05 * total_steps)`，重跑，观察曲线斜升段变短。
3. 打印 `track_lrs[0]`、`track_lrs[warmup_steps-1]`、`track_lrs[warmup_steps]` 三个值。

**需要观察的现象**：

- 曲线前段是一条斜率恒定的直线（线性），后段是水平线。
- `track_lrs[0]` 应等于 `initial_lr`（因为 `global_step=0` 时 `0 * lr_increment = 0`）。
- `track_lrs[warmup_steps]` 应正好等于 `peak_lr`（warmup 结束切到 else 分支）。

**预期结果**：以本书参数 `initial_lr=0.0001, peak_lr=0.01`，曲线从 0.0001 线性升到约 0.01 再走平。具体数值受 `warmup_steps` 取整影响，**待本地验证**你机器上的精确拐点位置。

#### 4.1.5 小练习与答案

**练习 1**：为什么是「线性」warmup，而不是一开始就跳到 `peak_lr`？
**参考答案**：初始权重随机、梯度方向噪声大，大学习率会放大错误更新导致 loss 发散或 NaN；线性爬坡让模型先在小步幅下找到大致方向，再逐步放开。

**练习 2**：若把 `warmup_steps` 设成 0 会怎样？
**参考答案**：代码里 `lr_increment = (peak_lr - initial_lr) / warmup_steps` 会触发除零错误。这正是后续整合时要确保 `warmup_steps >= 1` 的原因。

---

### 4.2 余弦衰减（cosine decay）

#### 4.2.1 概念说明

**问题**：warmup 把学习率抬到 `peak_lr` 后，如果一直保持这个峰值，训练后期会反复在最优点附近「震荡」，难以精细收敛——大步幅让你接近盆底，却也让你跨过盆底。

**直觉**：调焦距——先大范围粗调（大 lr），再小范围微调（小 lr），越接近清晰越要轻拧。

**余弦衰减**：让学习率在 warmup 之后，沿**半条余弦曲线**从 `peak_lr` 平滑下降到 `min_lr`（通常设得很小，接近 0）。相比线性衰减，余弦曲线在中段下降更平缓、过渡更柔和，是当下 LLM 预训练最主流的衰减方式（也有项目如 OLMo 用线性衰减）。

#### 4.2.2 核心流程

定义 warmup 之后的「进度」：

\[
\text{progress} = \frac{\text{global\_step} - \text{warmup\_steps}}{\text{total\_steps} - \text{warmup\_steps}}, \quad \text{progress} \in [0, 1]
\]

余弦衰减公式：

\[
\text{lr}(\text{global\_step}) = \text{lr}_{\min} + \frac{1}{2}\,(\text{peak\_lr} - \text{lr}_{\min})\,\bigl(1 + \cos(\pi \cdot \text{progress})\bigr)
\]

代入两个端点验证（这是必记的检验）：

- 训练刚开始衰减（`progress = 0`）：\(\cos(0)=1\)，则 \(\text{lr} = \text{lr}_{\min} + \tfrac{1}{2}(\text{peak}-\text{lr}_{\min})\cdot 2 = \text{peak\_lr}\)。
- 训练结束（`progress = 1`）：\(\cos(\pi)=-1\)，则 \(\text{lr} = \text{lr}_{\min} + 0 = \text{lr}_{\min}\)。

即衰减**从峰值开始、终于最小值**，中间是平滑的 S 形过渡。伪代码：

```
若 global_step < warmup_steps:        # 线性 warmup
    lr = initial_lr + global_step * lr_increment
否则:                                  # 余弦衰减
    progress = (global_step - warmup_steps) / (total_steps - warmup_steps)
    lr = min_lr + 0.5 * (peak_lr - min_lr) * (1 + cos(pi * progress))
```

#### 4.2.3 源码精读

notebook 在 D.1 演示的基础上加了 `else` 分支（D.2 节）：

[:L382-L386 — warmup 之后的余弦退火分支](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L382-L386)

```python
        else:
            # Cosine annealing after warmup
            progress = ((global_step - warmup_steps) /
                        (total_training_steps - warmup_steps))
            lr = min_lr + (peak_lr - min_lr) * 0.5 * (
                1 + math.cos(math.pi * progress))
```

> 说明：这正是上面公式的直译。`min_lr` 在本演示中设为 `0.1 * initial_lr`（一个经验值：衰减下限通常远小于峰值，让训练末期步幅极小）。配合 warmup 分支（[:L378-L380](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L378-L380)），画出的完整曲线是「先线性升到峰值，再沿余弦平滑回落」——这就是教科书级的 LLM 学习率调度形状（notebook 中 `2.pdf`）。

#### 4.2.4 代码实践

**目标**：验证余弦公式的两个端点与中点行为。

**操作步骤**：

1. 运行 D.2 节到绘图 cell，确认曲线形状为「爬坡 + 余弦回落」。
2. 单独执行下面这段**示例代码**（不属于项目，用于核验公式），人工算几个点：

```python
import math
peak_lr, min_lr = 0.01, 0.0001
for progress in [0.0, 0.5, 1.0]:
    lr = min_lr + (peak_lr - min_lr) * 0.5 * (1 + math.cos(math.pi * progress))
    print(progress, round(lr, 5))
```

**需要观察的现象**：

- `progress=0.0` 输出 `0.01`（= peak_lr），`progress=1.0` 输出 `0.0001`（= min_lr）。
- `progress=0.5` 输出 `0.00505`，即恰为 `(peak_lr + min_lr)/2`——余弦中点等于算术中点，这正是「平滑过渡」的体现。

**预期结果**：三点数值与上面对应。曲线在两端斜率小、中段下降快，呈现柔和的 S 形。

#### 4.2.5 小练习与答案

**练习 1**：为什么余弦衰减到 `min_lr` 而不是 0？
**参考答案**：保留一个极小的学习率，让模型在训练最末期仍能做微调，避免完全停滞；同时也避免除以或逼近 0 带来的数值问题。

**练习 2**：把余弦衰减换成线性衰减（`lr = min_lr + (peak_lr - min_lr)*(1 - progress)`），曲线形状会怎样？
**参考答案**：会从峰值沿直线均匀降到 `min_lr`，中点恰为中值，但两端没有余弦那样的「缓起缓收」，过渡更生硬；在大模型上通常余弦更稳。

---

### 4.3 梯度裁剪（gradient clipping）

#### 4.3.1 概念说明

**问题**：训练中有时某个 batch 会产生**异常大的梯度**（梯度爆炸，exploding gradients），导致 `optimizer.step()` 把权重推到极离谱的位置，loss 瞬间变 `NaN`。这在深层网络、长序列、不收敛的学习率下都可能发生。

**梯度裁剪**：给所有参数梯度的「整体大小」设一个上限 `max_norm`。一旦梯度的总范数超过这个上限，就**按比例缩小**所有梯度，使其总范数恰好等于 `max_norm`；若没超过，则原样不动。注意：它是「等比缩放」，只压幅度、不改方向。

这里用的是 **L2 范数（欧几里得范数）**。对一个梯度向量 \(\mathbf{v} = [v_1, v_2, \dots, v_n]\)：

\[
\|\mathbf{v}\|_2 = \sqrt{v_1^2 + v_2^2 + \dots + v_n^2}
\]

notebook 用一个小矩阵举例（便于直观理解）。设梯度矩阵：

\[
G = \begin{bmatrix} 1 & 2 \\ 2 & 4 \end{bmatrix}
\]

其 L2 范数为：

\[
\|G\|_2 = \sqrt{1^2 + 2^2 + 2^2 + 4^2} = \sqrt{25} = 5
\]

若 `max_norm = 1`，因 \(5 > 1\)，需把所有元素按 \(\frac{\text{max\_norm}}{\|G\|_2} = \frac{1}{5}\) 缩放：

\[
G' = \frac{1}{5} G = \begin{bmatrix} \tfrac{1}{5} & \tfrac{2}{5} \\ \tfrac{2}{5} & \tfrac{4}{5} \end{bmatrix}
\]

> ⚠️ 重要澄清：notebook 这个矩阵例子是**讲原理用的简化模型**。PyTorch 的 `torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)` 实际计算的是**所有参数的梯度拼成一个大向量后的全局 L2 范数**，再统一按 `max_norm / 全局范数` 缩放所有梯度——而不是逐参数分别裁剪。这样保证「整步更新的大小」被限制住。

#### 4.3.2 核心流程

```
loss.backward()                                  # 算出所有参数的 .grad
total_norm = sqrt( sum( 每个 param.grad 的元素平方和 ) )   # 全局 L2 范数
若 total_norm > max_norm:
    缩放因子 = max_norm / total_norm
    每个 param.grad *= 缩放因子                    # 等比缩小，方向不变
optimizer.step()                                 # 再用裁剪后的梯度更新
```

顺序很关键：**必须先 `backward()`、再裁剪、最后 `step()`**。裁剪的是 `.grad`，发生在反传之后、更新之前。

#### 4.3.3 源码精读

notebook 先用一个工具函数度量「最大梯度」，方便观察裁剪效果：

[:L531 — find_highest_gradient：遍历所有参数取最大梯度值](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L531)

```python
def find_highest_gradient(model):
    max_grad = None
    for param in model.parameters():
        if param.grad is not None:
            grad_values = param.grad.data.flatten()
            max_grad_param = grad_values.max()
            if max_grad is None or max_grad_param > max_grad:
                max_grad = max_grad_param
    return max_grad
```

> 说明：它把每个参数的梯度展平，取最大元素，再在所有参数里取最大——只是一个「诊断」函数，用来肉眼确认裁剪确实把大梯度压小了，不参与训练逻辑。

随后是对一个真实 batch 反传 + 裁剪：

[:L567 — 调用 clip_grad_norm_ 做全局 L2 范数裁剪](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L567)

```python
torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
print(find_highest_gradient(model))
```

> 说明：`backward()` 后调用此行，PyTorch 计算全局梯度范数，超过 1.0 就等比缩放。notebook 输出显示裁剪前最大梯度约 `0.0446`、裁剪后约 `0.0201`（数值来自作者 Mac 的 `mps:0`，**待本地验证**你机器上的精确值，但「裁剪后变小」这一结论成立）。

#### 4.3.4 代码实践

**目标**：亲手验证裁剪的等比缩放性质。

**操作步骤**：

1. 运行 D.3 节到 `find_highest_gradient` 的两次 print（裁剪前/后），对比数值。
2. 执行下面这段**示例代码**（不属于项目，用于复现 4.3.1 的矩阵例子）：

```python
import torch
g = torch.tensor([[1., 2.], [2., 4.]], requires_grad=True)
g.grad = torch.tensor([[1., 2.], [2., 4.]])   # 直接把 G 当作梯度
total_norm = torch.nn.utils.clip_grad_norm_([g], max_norm=1.0)
print("total_norm =", round(total_norm.item(), 4))   # 应为 5.0
print(g.grad)                                        # 应全为原来的 1/5
```

**需要观察的现象**：

- `total_norm` 打印为 `5.0`（即 \(\sqrt{25}\)）。
- 裁剪后 `g.grad` 每个元素都是原来的 1/5：`[[0.2, 0.4], [0.4, 0.8]]`，方向完全不变，只是整体缩小。

**预期结果**：与 4.3.1 的手算一致，印证「按 \(\text{max\_norm}/\|G\|_2\) 等比缩放」。

#### 4.3.5 小练习与答案

**练习 1**：梯度裁剪放在 `optimizer.step()` 之后会怎样？
**参考答案**：无效且错误。`step()` 已经用未裁剪的梯度更新了权重，之后再改 `.grad` 对本次更新没有任何作用；裁剪必须在 `backward()` 与 `step()` 之间。

**练习 2**：`max_norm` 设得过大或过小分别有什么后果？
**参考答案**：过大≈形同虚设，挡不住梯度爆炸；过小会持续压扁梯度、严重拖慢训练。LLM 常用 `max_norm=1.0`，是个经验上的平衡值。

---

### 4.4 三件套整合：从 train_model_simple 到 train_model

#### 4.4.1 概念说明

前三节是「零件」，本节把它们装回训练循环。思路很直接：以第 5 章 `train_model_simple`（[u5-l2](./u5-l2-training-loop.md)）为蓝本，在每个 batch 的 `zero_grad → backward → step` 骨架里插入三处改动：

1. **算学习率**：每步根据 warmup/余弦公式算出 `lr`，写回 `optimizer.param_groups`（替换原版的恒定学习率）。
2. **梯度裁剪**：在 `backward()` 之后、`step()` 之前调用 `clip_grad_norm_`。
3. **额外记录**：收集每步学习率 `track_lrs`，便于事后画调度曲线。

这就是附录 D 的 `train_model` 函数。它复用了 `previous_chapters.py` 里的 `calc_loss_batch`、`evaluate_model`、`generate_and_print_sample`（第 5 章成品），**只改造训练循环本身**。

#### 4.4.2 核心流程

```
peak_lr  = optimizer.param_groups[0]["lr"]          # 从优化器读峰值
total_steps = len(train_loader) * n_epochs
lr_increment = (peak_lr - initial_lr) / warmup_steps

for epoch:
    model.train()
    for batch:
        optimizer.zero_grad()
        global_step += 1

        # ① 学习率调度（warmup + 余弦）
        if global_step < warmup_steps:
            lr = initial_lr + global_step * lr_increment
        else:
            progress = (global_step - warmup_steps) / (total_steps - warmup_steps)
            lr = min_lr + 0.5*(peak_lr - min_lr)*(1 + cos(pi*progress))
        optimizer.param_groups[0]["lr"] = lr

        # ② 前向 + 反传（复用第 5 章 calc_loss_batch）
        loss = calc_loss_batch(...); loss.backward()

        # ③ 梯度裁剪（warmup 之后才启用）
        if global_step >= warmup_steps:
            clip_grad_norm_(model.parameters(), max_norm=1.0)

        optimizer.step()
        # 周期性 evaluate_model + generate_and_print_sample（同第 5 章）
```

#### 4.4.3 源码精读

函数签名比 `train_model_simple` 多了 `warmup_steps / initial_lr / min_lr` 三个调度参数：

[:L602-L604 — train_model 签名，新增调度参数](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L602-L604)

```python
def train_model(model, train_loader, val_loader, optimizer, device,
                n_epochs, eval_freq, eval_iter, start_context, tokenizer,
                warmup_steps, initial_lr=3e-05, min_lr=1e-6):
```

> 说明：注意 `peak_lr` 不在参数里——它从优化器读出（[:L616](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L616) `peak_lr = optimizer.param_groups[0]["lr"]`），即「优化器初始化时给的 lr 就是峰值」。`initial_lr` / `min_lr` 都给了默认值。

每个 batch 里的学习率调度，正是 4.1 + 4.2 的合体：

[:L625-L632 — 整合后的 warmup + 余弦调度](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L625-L632)

```python
            if global_step < warmup_steps:
                # Linear warmup
                lr = initial_lr + global_step * lr_increment
            else:
                # Cosine annealing after warmup
                progress = ((global_step - warmup_steps) /
                            (total_training_steps - warmup_steps))
                lr = min_lr + (peak_lr - min_lr) * 0.5 * (1 + math.cos(math.pi * progress))
```

梯度裁剪，注意 `ORIG_BOOK_VERSION` 这个开关和 `>=` 的修正：

[:L645-L649 — 梯度裁剪：>= 修正了原书 > 的漏裁 bug](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L645-L649)

```python
            if ORIG_BOOK_VERSION:
                if global_step > warmup_steps:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            else:
                if global_step >= warmup_steps:  # 原书用 > 会导致 warmup 后第一步漏裁
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
```

> 说明：这是仓库相对纸质书的一处**勘误**。原书用 `global_step > warmup_steps`，使得恰在 `global_step == warmup_steps`（即 warmup 刚结束、学习率首次达到峰值）那一步**跳过了裁剪**——而那恰恰是最可能出大梯度的一步。仓库改成 `>=` 补上这个漏洞。另外裁剪只在 warmup 之后启用（warmup 期学习率很小、梯度本就温和，无需裁）。

训练入口（D.4 末尾）：

[:L744 — 峰值学习率 0.001（纸质书误写 5e-4，已勘误）](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L744)

```python
peak_lr = 0.001  # 纸质书原本误写为 5e-4
optimizer = torch.optim.AdamW(model.parameters(), lr=peak_lr, weight_decay=0.1)
```

[:L749-L753 — 用 train_model 跑 15 个 epoch](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L749-L753)

```python
train_losses, val_losses, tokens_seen, lrs = train_model(
    model, train_loader, val_loader, optimizer, device, n_epochs=n_epochs,
    eval_freq=5, eval_iter=1, start_context="Every effort moves you",
    tokenizer=tokenizer, warmup_steps=warmup_steps,
    initial_lr=1e-5, min_lr=1e-5
)
```

> 说明：注意 `initial_lr=1e-5` 与 `min_lr=1e-5` 取了相同的极小值，`peak_lr=0.001`。复用的 `calc_loss_batch` / `evaluate_model` / `generate_and_print_sample` / `plot_losses` 全部来自 [previous_chapters.py:L249-L311](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/previous_chapters.py#L249-L311)，与第 5 章完全一致——本讲没有改动损失与评估逻辑。

#### 4.4.4 代码实践（本讲主实践）

**目标**：完整跑通 `train_model`，对比「恒定学习率」与「warmup+余弦+裁剪」两种策略下的损失曲线稳定性。

**操作步骤**：

1. 依 [u1-l2](./u1-l2-environment-setup.md) 装好依赖，进入 `appendix-D/01_main-chapter-code/` 目录，从头运行 `appendix-D.ipynb` 到 D.4 的训练 cell。CPU 也能跑（数据极小），约几分钟。
2. 训练结束后运行后续两个绘图 cell：第一个画 `lrs`（学习率调度），第二个用 `plot_losses` 画训练/验证损失。
3. **对照实验**：把 `train_model` 内的学习率调度临时改成恒定峰值——即把 [:L625-L632](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L625-L632) 整段替换为 `lr = peak_lr`，并把 [:L648-L649](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/appendix-D.ipynb#L648-L649) 的裁剪注释掉，重训一遍，保存为另一组损失曲线。

**需要观察的现象**：

- 学习率调度图应为「先线性升到 0.001、再沿余弦平滑回落到 1e-5 附近」。
- 增强版训练初期（warmup 阶段）loss 下降**更平缓、不抖**；恒定学习率版第一步就可能出现较大跳变。
- notebook 原始输出参考：从 `Ep 1 Train loss 10.969` 一路降到 `Ep 15 Train loss 1.312`，验证损失则停在约 `6.16`（典型过拟合——训练集太小）。

**预期结果**：增强版曲线更平滑、初期无突刺；两种策略最终都会因数据太小而过拟合（验证 loss 不降），这正好说明本讲解决的是「训练稳定性」而非「泛化」。具体 loss 数值依赖设备与随机种子，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`train_model` 相比 `train_model_simple` 改了哪几处？
**参考答案**：三处——(1) 每步动态计算并写回学习率（warmup+余弦）；(2) warmup 后插入梯度裁剪；(3) 额外记录并返回 `track_lrs`。损失计算、评估、采样逻辑全部复用第 5 章，未改。

**练习 2**：为什么梯度裁剪只在 `global_step >= warmup_steps` 时启用？
**参考答案**：warmup 期间学习率被刻意压得很小，权重更新本就温和、梯度爆炸风险低，裁剪意义不大；裁剪主要针对 warmup 结束、学习率拉到峰值后可能出现的异常大梯度。

## 5. 综合实践

把本讲三件套与第 5 章的损失/评估串起来，完成一次「调度曲线诊断」：

1. 运行 4.4.4 的增强版训练，把返回的 `lrs` 和 `train_losses`/`val_losses` 都拿到。
2. 在**同一张图**上画出三条曲线（双 y 轴）：学习率（warmup+余弦）、训练损失、验证损失。
3. 在图上标出三个关键时刻：(a) warmup 结束点 `global_step == warmup_steps`；(b) 余弦衰减到一半（`progress=0.5`）的位置；(c) 训练结束。
4. 回答：训练损失明显加速下降，发生在学习率接近峰值的阶段，还是衰减到很小的阶段？这与「大 lr 粗调、小 lr 微调」的直觉是否吻合？

> 提示：可借助 [previous_chapters.py 的 plot_losses](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-D/01_main-chapter-code/previous_chapters.py#L295-L311) 的双 x 轴写法。若你是在 CPU 上跑，把 `n_epochs` 调小到 5 即可看出趋势。

## 6. 本讲小结

- **学习率 warmup**：训练初期把学习率从 `initial_lr` 线性抬升到 `peak_lr`，避免随机初始化权重下的大步更新把训练打飞；warmup 步数常取总步数的 20%。
- **余弦衰减**：warmup 之后让学习率沿半条余弦曲线从 `peak_lr` 平滑回落到 `min_lr`，公式为 \(\text{lr}=\text{lr}_{\min}+\tfrac{1}{2}(\text{peak}-\text{lr}_{\min})(1+\cos(\pi\cdot\text{progress}))\)，端点恰为峰值与最小值。
- **梯度裁剪**：用 `clip_grad_norm_` 计算所有参数梯度的全局 L2 范数，超过 `max_norm` 就按比例等比缩小，压幅度不改方向；必须夹在 `backward()` 与 `step()` 之间。
- **动态改学习率 = 改 `optimizer.param_groups[0]["lr"]`**，每步算、每步写，无需重建优化器。
- **整合**：附录 D 的 `train_model` 在 `train_model_simple` 骨架上插入「调度 + 裁剪 + 记录」三处，损失与评估完全复用第 5 章；仓库还修正了纸质书两处勘误（`>=` vs `>` 的漏裁、`peak_lr` 误写 5e-4）。
- 本讲解决的是**训练稳定性**，不解决小数据集的**过拟合**——后者要靠更大规模数据或加载预训练权重（见 [u5-l4](./u5-l4-weight-loading.md)）。

## 7. 下一步学习建议

- 想把多卡训练也加上？继续 [u8-l3 多卡分布式训练（DDP）](./u8-l3-distributed-training-ddp.md)，把本讲的 `train_model` 放进 `DistributedDataParallel` 进程组。
- 想看这套调度在「真·预训练」里的样子？阅读 `ch05/03_bonus_pretraining_on_gutenberg/`（notebook 末尾推荐的更大规模预训练），对比它的学习率配置与附录 D 的异同。
- 回到 PyTorch 底层？[u8-l1 PyTorch 核心基础](./u8-l1-pytorch-essentials.md) 系统讲了 `param_groups`、`.grad`、`zero_grad` 的原理，是理解本讲每行代码的后盾。
