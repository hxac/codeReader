# 显存优化：梯度检查点

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚训练时 GPU 显存都被什么占用，以及为什么「中间激活」是随网络深度和 batch size 线性膨胀的大头。
2. 解释梯度检查点（gradient checkpointing）的核心思想——**用重计算换显存**，以及它在「标准反向传播」与「低内存反向传播」两个极端之间如何折中出 \( O(\sqrt{n}) \) 的显存复杂度。
3. 会用 `torch.utils.checkpoint.checkpoint_sequential` 包装 `nn.Sequential` 模型，理解仓库 notebook 中三处 `###### NEW` 修改各自的用意（尤其是 `x.requires_grad = True` 这个著名的坑）。
4. 会实测「显存峰值 vs 训练时间」：GPU 上用 `torch.cuda.max_memory_allocated()`，并批判性地理解仓库 notebook 用 `tracemalloc` 得到的数字到底测的是什么。
5. 建立「不改变数学、只改变内存策略」的受控对比意识：checkpoint 化前后逐 batch 损失应当完全一致。

本讲是第 8 单元「工程化与高级专题」的一讲，聚焦单卡显存不够时的**软件解法**（下一讲 u8-l5 的多 GPU 并行则是**硬件扩展解法**）。

## 2. 前置知识

### 2.1 训练时的显存账本

一次训练中，GPU 显存大致由四部分构成：

| 组成 | 大小 | 随什么增长 |
|---|---|---|
| 模型参数 | 每参数 4 字节（float32） | 与层数成正比 |
| 梯度 | 与参数同尺寸 | 与层数成正比 |
| 优化器状态 | Adam 额外存两份动量，约 2×参数量 | 与层数成正比 |
| **中间激活** | 每层的输入/输出特征图 | 与**层数 × batch size × 特征图尺寸**成正比 |

前三项只与参数量有关，对固定模型是常数；第四项「中间激活」才是真正的变量——batch size 翻倍、网络加深，它就翻倍。**梯度检查点针对的正是第四项。**

### 2.2 为什么中间激活必须保存

回顾 u2-l5 从零实现反向传播：第 \( l \) 层的权重梯度需要「上游误差项 × 本层输入」，即 \( \nabla_{W^{(l)}} = \delta^{(l)\top} a^{(l-1)} \)。PyTorch autograd 正是为此在前向时把每层的输入激活存进计算图，`loss.backward()` 时直接取用。也就是说：**标准 autograd 的「自动」是以把中间结果全留在显存里为代价的。**

### 2.3 本讲的主角模型：NiN

本讲沿用仓库 notebook 的载体——Network-in-Network（NiN）。u3-l3 已详细讲过它的设计哲学（1×1 卷积做跨通道升降维、末层输出类别数通道 + 全局平均池化、无全连接分类头），本讲只需记住它在这里的**结构角色**：一个由 23 个子模块顺序堆叠而成的 `nn.Sequential`，正好适合 `checkpoint_sequential` 这种「按序切段」的 API。作者在 notebook 中也说明了选择它的理由：结构简单、可读性好；网络越深，检查点的收益越大。

### 2.4 其余前置

- 标准训练循环五步范式 `forward → loss → zero_grad → backward → step`（u2-l2 建立、u4-l4 交给 `train_classifier_simple_v1`）。
- `set_all_seeds` 固定随机性（u1-l3）：本讲两次对比实验依赖它保证「除了内存策略，其余一切相同」。
- CIFAR-10 数据管线 `get_dataloaders_cifar10`（u4-l1/u4-l3）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb` | 本讲核心：梯度检查点对照实验 notebook | 理论说明、三处 `NEW` 修改、tracemalloc 实测与结论 |
| `pytorch_ipynb/helper_train.py` | 共享训练循环 | `train_classifier_simple_v1` 中的五步范式（两次实验共用，保证唯一变量是内存策略） |
| `pytorch_ipynb/helper_data.py` | 共享数据加载 | `get_dataloaders_cifar10` 提供 CIFAR-10 的三个 loader |
| `pytorch_ipynb/helper_evaluate.py` | 共享评估 | `compute_accuracy` 被训练函数调用 |
| `pytorch_ipynb/cnn/nin-cifar10.ipynb` | NiN 的标准训练版（u3-l3 已精读） | 对照参考：不带检查点的原始 NiN 用法 |

## 4. 核心概念与源码讲解

### 4.1 检查点原理：重计算换显存

#### 4.1.1 概念说明

设网络有 \( n \) 层。反向传播需要每层的中间激活，问题是：**这些激活什么时候存、存多少？** 两个极端：

- **标准反向传播（vanilla）**：前向时把所有中间激活都存下来。显存最省心，但显存开销 \( O(n) \)。
- **低内存反向传播（low-memory）**：前向什么都不存（只留输入），反向传到第 \( i \) 层时从输入**重新前向算到第 \( i \) 层**。显存几乎不随深度增长，但计算量变成 \( O(n^2) \)。

**梯度检查点是两者的折中**：只把少数层的激活定为「检查点」保存，其余激活反向时从最近的检查点**重算**出来。最优策略是每隔 \( \sqrt{n} \) 层设一个检查点，得到显存 \( O(\sqrt{n}) \)、额外计算约多一次前向的开销。这一思路的经典出处是 Chen et al. 2016《Training Deep Nets with Sublinear Memory Cost》（仓库 notebook 未直接引用该论文，但引用了四篇博客/教程，见下文源码精读）。

一句话直觉：**激活是可以「按需再生产」的商品——检查点就是仓库网点，网点越密，库存（显存）越高、运费（重算）越低；网点越疏，库存越省、运费越高。**

#### 4.1.2 核心流程

三种策略的伪代码对比（示例代码，用于理解而非仓库原文）：

```text
vanilla（标准 autograd）:
    前向: for i in 1..n: a[i] = layer_i(a[i-1])   # 所有 a[i] 留在显存
    反向: 逐层用现成的 a[i] 求梯度                  # 计算一遍

low-memory:
    前向: 只保留 a[0]（网络输入）
    反向: 求第 i 层梯度前，从 a[0] 一路重算到 a[i-1] # 每层都要重算 → O(n²)

gradient checkpointing（每 √n 层设检查点）:
    前向: 保存 a[0], a[√n], a[2√n], ...             # 只存 √n 份
    反向: 在每个检查点段内，从检查点激活重算段内前向   # 每段最多重算 √n 层
```

三者的权衡可以写成一张表（\( n \) 为层数）：

| 策略 | 中间激活显存 | 前向计算量 | 何时重算 |
|---|---|---|---|
| vanilla | \( O(n) \) | 1 次前向 | 从不 |
| low-memory | \( O(1) \) | \( O(n^2) \) | 每层都从头算 |
| checkpointing | \( O(\sqrt{n}) \) | 1 次前向 + 反向时各段重算（合计约多 1 次前向） | 每段从检查点重算 |

\[ \text{checkpointing 显存} = \underbrace{\sqrt{n}\ \text{份检查点激活}}_{O(\sqrt{n})} \quad\Longleftrightarrow\quad \text{额外计算} \approx \text{一次完整前向} \]

注意 notebook 原文的表述角度略有不同：它说 checkpointing 的内存需求「比 low-memory 版本增加 \( \sqrt{n} \）」，且处理节点数介于 vanilla 的 \( n \) 与 low-memory 的 \( n^2 \) 之间——同一结论的另两种说法。

#### 4.1.3 源码精读

仓库 notebook 开头的理论说明 cell 完整讲述了上述三种策略：

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L77-L115](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L77-L115) —— 这段 markdown 依次给出 Vanilla / Low-memory / Gradient Checkpointing 三节解释，并配了三张计算图示意图（`figures/gradient-checkpointing-*.png`），末尾列出 4 篇参考文献（cybertronai 的 gradient-checkpointing 库、TensorFlow 官方博客、pytorch_memonger 教程、sicara.ai 的显存优化博文）。

其中几个关键句的原文与翻译对照：

- 「In vanilla backpropagation, the required memory grows linearly with the number of layers *n*」——标准反传显存随层数线性增长，因为前向的所有节点都被保留在内存中，直到其所有子节点处理完毕。
- 「the low-memory version processes \( n^2 \) nodes」——低内存版每个节点都被反复重算，计算量平方级。
- 「The optimal choice is to designate every \( \sqrt{n} \)-th node as a checkpoint node」——最优做法是每 \( \sqrt{n} \) 个节点设一个检查点。
- 「we can implement models that are 4x to 10x larger than architectures that would usually fit into GPU memory」——引用文献的说法：检查点可以让我们训练比常规显存容量大 4 到 10 倍的模型。

作者还特别说明：本 notebook 只演示**顺序模型**的检查点（`checkpoint_sequential`），非顺序模型同样可以用（需要 `checkpoint` 函数逐段包装），详见参考文献 [3]。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 4.1.2 的表格具体化到本讲的 NiN 上，体会 \( \sqrt{n} \) 到底是多大的数。
2. **操作步骤**：
   - 打开 notebook 的模型定义 cell（见 4.2.3），数一数 `self.classifier` 这个 `nn.Sequential` 里有几个子模块（提示：三个 mlpconv 块，前两块各 8 个模块「conv/relu/conv/relu/conv/relu/pool/dropout」，第三块 7 个模块，共 **23** 个）。
   - 分别计算：vanilla 保存几份激活？low-memory 反向第 \( i \) 层平均要重算几层？若按每 \( \lceil\sqrt{23}\rceil = 5 \) 个模块设一个检查点，需要保存几份检查点激活、每个检查点段内最多重算几层？
3. **需要观察的现象**：\( \sqrt{23} \approx 4.8 \)，即检查点数量只是层数的约 1/5，段内重算上限也只有 5 层左右——这就是「显存与重算同时被压到次线性」的直观来源。
4. **预期结果**：vanilla 存 23 份；low-memory 平均重算约 \( n/2 = 11.5 \) 层、总计 \( O(n^2) \approx 529 \) 层次计算；checkpointing 存约 5 份检查点、反向最多多算约 23 层次（≈ 一次前向）。
5. 以上为纯阅读推算，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么说梯度检查点「不改变训练的数学，只改变内存策略」？

**答案**：重算得到的是同一组中间激活（相同权重、相同输入下前向是确定性的），因此各层梯度与标准反传完全一致，最终参数更新也一致。它只是把「保存」变成「需要时再算」，改变的是显存与时间的分配，不是计算结果。（本讲 4.3.3 会看到两个版本逐 batch 损失完全一致的实证。）

**练习 2**：如果你的模型只有 2 层，用梯度检查点划算吗？

**答案**：不划算。\( \sqrt{2} \approx 1.4 \)，能省下的激活寥寥无几，而反向时的重算开销是实打实的。检查点的收益随深度增长，所以 notebook 作者也说「架构越深，收益越大」；对极浅模型应优先考虑减小 batch size 或改用混合精度。

**练习 3**：检查点数量（`segments` 语义见 4.2）取两个极端——「每层都是检查点」和「全网只有 1 个检查点」——分别退化成 4.1.2 表中的哪种策略？

**答案**：每层都是检查点 ⇒ 反向时几乎不需要重算、但全部激活都保存 ⇒ 退化成 vanilla；全网 1 个检查点（只保存网络输入）⇒ 反向时要重算整个前向 ⇒ 显存最省、重算最多，接近 low-memory（但重算只发生在反向一次，仍是 \( O(n) \) 而非 \( O(n^2) \)）。仓库 notebook 选择的 `segments=1` 正是后者的温和版。

### 4.2 `torch.utils.checkpoint` 的使用

#### 4.2.1 概念说明

PyTorch 把检查点机制做进了标准库：`torch.utils.checkpoint` 模块提供两个入口——

- `checkpoint(function, *args)`：把**任意一段可微计算**包成一个检查点段；
- `checkpoint_sequential(functions, segments, input)`：**顺序模型专用**，把一个模块列表切成 `segments` 段，段边界即检查点。

从外部看，`checkpoint_sequential` 的输入输出与直接调用原模型没有任何区别（forward 的返回值一模一样），因此 notebook 中**训练循环、损失函数、优化器一行都不用改**——这正是它「即插即用」的工程价值。变化全部发生在 autograd 内部：

- **前向**：段内以不建图的方式（`no_grad` 语义）执行，中间激活用完即弃，只保存每个段的**输入**；
- **反向**：autograd 走到某个段时，从该段的输入**重算**段内前向、这次建图，然后在图内正常反传。

`segments` 是显存-时间旋钮：段数越少越省显存、重算越多；段数越多越接近 vanilla。仓库作者的取舍写在 Part 2 的说明里——对该 NiN 而言 `segments=1` 已经是好的折中。

#### 4.2.2 核心流程

把 23 个模块的 NiN 接入检查点后的单步训练流程：

```text
1. x (B,3,32,32) 进入 forward
2. x.requires_grad = True          # 让"段输入"参与梯度图（关键，见下）
3. checkpoint_sequential(modules, segments=1, x)
   ├─ 前向（不建图）: 逐模块算到 AvgPool2d(8)，输出 (B,10,1,1)，中间激活即弃
   └─ autograd 登记"重算闭包"：(段输入 x, 段内模块列表)
4. view 成 (B,10) 得 logits → cross_entropy → loss
5. loss.backward()
   └─ 走到检查点段: 用保存的 x 重算段内前向（这次建图）→ 正常反传得到所有参数梯度
6. optimizer.step()                # 与不检查点化完全相同的更新
```

**`x.requires_grad = True` 为什么必须？** 输入图像是数据张量，`requires_grad` 默认为 `False`；而被检查点包裹的段要求「至少一个输入带梯度」，反向传播才会为该段触发重算建图——否则段内参数会拿不到梯度。这是 `torch.utils.checkpoint` 文档明示的经典陷阱，notebook 用一行赋值解决。（较新版本的 PyTorch 提供了非重入实现 `use_reentrant=False`，对这一要求有所放宽；不同版本行为有差异，使用时请以你所用版本的官方文档为准。）

**Dropout 怎么办？** 重算的前向必须与第一次前向产生**相同**的结果，否则梯度就不精确。NiN 里有两个 `Dropout(0.5)`——两次前向若采样出不同的掩码，重算就不再等价。PyTorch 的 `checkpoint` 默认会在前向时保存随机数状态、重算时恢复（`preserve_rng_state=True`），以保证 Dropout 这类随机层前后一致。这一行为是默认保证而非巧合，但仍建议你在自己版本上核对文档。

#### 4.2.3 源码精读

对照实验的组织方式：notebook 分成 Part 1（基线，无检查点）与 Part 2（检查点版），**模型结构、超参、随机种子、数据完全相同，唯一的差异是 forward 是否走 `checkpoint_sequential`**。两边的设置（`BATCH_SIZE = 256`、`LEARNING_RATE = 0.0001`、`RANDOM_SEED = 1`）见 [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L300-L321](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L300-L321)，数据来自共享的 `get_dataloaders_cifar10`（[L370-L380](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L370-L380)），训练循环统一外包给 `train_classifier_simple_v1`——其五步范式在 [pytorch_ipynb/helper_train.py:L40-L49](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L40-L49)（`logits = model(features)` → `loss_fn` → `zero_grad` → `backward` → `step`）。正因如此，checkpoint 的改动被完全隔离在模型类内部。

**基线模型**（Part 1，无检查点）：

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L452-L495](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L452-L495) —— 基线 `NiN` 类：23 个子模块全部装进一个 `nn.Sequential`，`forward` 只是 `self.classifier(x)` 后 `view(x.size(0), num_classes)`。这就是 u3-l3 讲过的 NiN，标准 autograd 会为它保存全部中间激活。

**检查点版模型**（Part 2）——与基线相比只有三处 `###### NEW` 标注的修改：

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L628-L685](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L628-L685) —— 修改版 `NiN` 类全文。三处 `NEW` 分别是：

  1. **导入**（L634）：`from torch.utils.checkpoint import checkpoint_sequential`——只多这一行 import。
  2. **模块列表化**（L671）：`self.classifier_modules = [module for k, module in self.classifier._modules.items()]`——从 `Sequential` 的内部有序字典 `_modules` 里按顺序取出 23 个子模块组成 Python 列表，因为 `checkpoint_sequential` 的 `functions` 参数接受的是**可迭代的模块序列**而非 `Sequential` 对象本身。
  3. **forward 改写**（L677-L680）：`x.requires_grad = True` 后 `x = checkpoint_sequential(functions=self.classifier_modules, segments=1, input=x)`——把整条 Sequential 当作 **1 个段**：前向只保存网络输入 x，反向时重算全部 23 个模块。

  注意 `__init__` 中的 `self.classifier = nn.Sequential(...)`（L640-L668）与基线**逐字相同**——修改没有动任何层定义，这正是「受控对比」的范本。

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L618-L620](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L618-L620) —— Part 2 的说明 markdown：段数越少内存效率越高、但重算更多；作者实测对该架构 `segments=1` 是好的折中。

另外一个容易被忽略的实验卫生细节：

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L602-L605](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L602-L605) —— Part 1 结束后 `model.cpu()` + `del model`：先把模型搬回 CPU 再删除引用，释放显存/内存，**防止基线模型污染紧随其后的检查点实验的内存统计**。做内存对比实验时这是必须的习惯。

#### 4.2.4 代码实践

1. **实践目标**：亲手调节 `segments` 旋钮，体会「段数 ⇆ 显存/重算」的此消彼长。
2. **操作步骤**：
   - 复制 notebook 的 Part 2 模型 cell，把 `segments=1` 分别改为 `1`、`4`、`8`（可同时把 `NUM_EPOCHS` 降为 2 以节省时间——notebook 两次对照实验本来就只训 2 个 epoch）。
   - 每个 `segments` 取值下用相同种子重新初始化模型并训练 2 个 epoch，记录总训练时间与第 2 个 epoch 结束时的训练准确率。
3. **需要观察的现象**：段数增多时，显存峰值应当**上升**、训练时间应当**下降**（重算变少）；三个设置下逐 batch 的损失序列应当仍然从相同数值起步（同种子同数学）。
4. **预期结果**：`segments=1` 显存最低、时间最长；`segments=8` 接近基线。准确率三者应基本一致（2 epoch 时都在 25% 左右量级，与 notebook 输出相符）。
5. 本实践需要可运行环境，具体数字**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`checkpoint_sequential(functions=..., segments=..., input=...)` 中的 `segments=1` 是什么含义？保存了什么、丢掉了什么？

**答案**：`segments=1` 表示把整个模块列表切成 1 段——只保存该段的输入（即网络输入 x）与输出，其余 22 个模块间激活全部不保存；反向传播时从 x 重算整条前向（这次建图）再反传。显存最省，重算等于多跑一次完整前向。

**练习 2**：如果删掉 `x.requires_grad = True` 这一行，会发生什么？

**答案**：被包裹段的全部输入（只有 x）都不带梯度标志，反向传播不会为该段触发重算建图，段内所有卷积核**拿不到梯度**（PyTorch 还会给出警告），训练实际上失效。这是官方文档明示的使用前提，notebook 用这一行显式满足它。

**练习 3**：为什么重算时 Dropout 的掩码必须与第一次前向一致？PyTorch 如何保证？

**答案**：若两次前向的 Dropout 掩码不同，「重算出的激活」与「损失所依据的激活」就不是同一个函数的输出，梯度方向就是错的。PyTorch 的 `checkpoint` 默认在前向时保存 RNG 状态、重算前恢复（`preserve_rng_state=True`），使第二次采样与第一次完全一致。

### 4.3 显存与时间实测

#### 4.3.1 概念说明

「显存优化了多少」必须实测。两条路线：

- **`tracemalloc`（Python 标准库）**：追踪 **Python 解释器堆**上的内存分配（`tracemalloc.start()` 开始追踪，`get_traced_memory()` 返回 `(当前, 峰值)`，单位字节）。它是**CPU 侧**的度量——**看不到 CUDA 显存**（GPU 张量由 CUDA 分配器管理，不经过 Python 堆）。
- **`torch.cuda.max_memory_allocated()`**：PyTorch CUDA 缓存分配器的统计口径，返回自上次 `torch.cuda.reset_peak_memory_stats()` 以来**实际分配的 GPU 显存峰值**——这才是「显存够不够」的直接证据。

仓库 notebook 用的是 `tracemalloc`。这带来了一个值得批判性阅读的点：notebook 的训练明明跑在 GPU 上（`Device: cuda:2`），但 143,683 字节（约 140 KiB）这个峰值显然**不是** GPU 显存占用（光模型参数与梯度就远超此数）——它测到的是 Python 侧对象（如 autograd 图的簿记等）的堆内存差异。作为「检查点减少了需要簿记的中间对象」的**间接证据**它是有效的，但严格意义上的 GPU 显存收益应当用 `torch.cuda.max_memory_allocated()` 直接测量——这正是本讲实践任务要补上的实验。

同理，时间维度的度量要用 `time.perf_counter()`（或训练日志里的 `Time elapsed` / `Total Training Time`），并注意首次运行包含 CUDA 预热，正式计时前最好先跑几个 batch 热身。

#### 4.3.2 核心流程

一次规范的「显存 vs 时间」实测流程：

```text
1. 实验隔离: model.cpu(); del model; torch.cuda.empty_cache()   # 清掉上一个实验
2. 重建: set_all_seeds(seed) → 模型 → optimizer                  # 与基线完全同参
3. 计时器清零: start = time.perf_counter()
   显存统计清零: torch.cuda.reset_peak_memory_stats()
4. 训练固定 epoch 数（训练循环与基线完全相同）
5. 读数: elapsed = time.perf_counter() - start
        peak   = torch.cuda.max_memory_allocated() / 1024**2      # MB
6. 填表对比，计算相对变化时先声明以谁为基准
```

#### 4.3.3 源码精读

**基线实测 cell**（Part 1）：

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L582-L593](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L582-L593) —— `tracemalloc.start()` 后调用 `train_classifier_simple_v1(num_epochs=2, ...)` 训练 2 个 epoch，随后 `tracemalloc.get_traced_memory()` 取 `(current, peak)` 并 `tracemalloc.stop()`。输出为 `87518, 143683`（当前 87.5 KB / 峰值 143.7 KB 的 Python 堆内存），训练用时 `Total Training Time: 0.80 min`（输出见 [L566-L580](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L566-L580)）。

**检查点版实测 cell**（Part 2）：

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L731-L739](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L731-L739) —— 与基线 cell **逐字相同**的测量代码，只是此刻 `model` 已是 `segments=1` 的检查点版。输出为 `57806, 115055`，训练用时 `0.93 min`（输出见 [L712-L728](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L712-L728)）。

**结论 cell**：

- [pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb:L753](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L753) —— 作者结论原文：峰值内存效率提升约 **22%**，运行时间变差约 **14%**。

把两次运行的原始数字整理成对照表（并自己核算百分比口径）：

| 指标 | 基线（无检查点） | 检查点（segments=1） | 变化（以基线为基准） |
|---|---|---|---|
| Python 堆峰值（tracemalloc，字节） | 143,683 | 115,055 | **−19.9%** |
| Python 堆当前（字节） | 87,518 | 57,806 | −34.0% |
| 2 epoch 总训练时间 | 0.80 min | 0.93 min | **+16.3%** |
| 首 batch 损失 / 末次记录损失 | 2.3045 / 1.9474 | 2.3045 / 1.9474 | **完全一致** |

三点解读：

1. **口径问题**：作者写的「22% / 14%」与本表按基线为基准算出的「19.9% / 16.3%」略有出入（例如 14% 对应以检查点版 0.93 为分母的算法 (0.93−0.80)/0.93）。这不是谁对谁错，而是提醒我们：**报告百分比前先声明基准**。
2. **损失逐 batch 完全一致**是本实验最有说服力的一行：同种子下两个版本的损失序列（2.3045 → 2.2849 → … → 1.9474）一字不差，实证了 4.1.5 练习 1 的论断——检查点只改变内存策略，不改变训练数学。
3. **tracemalloc 的局限**再次强调：表中的字节数是 Python 堆口径（仅百 KB 量级），并非 GPU 显存。真正的显存账要用下一节的 `torch.cuda.max_memory_allocated()` 来算。

#### 4.3.4 代码实践（本讲核心实践）

1. **实践目标**：对 NiN 实测「开/关梯度检查点」的 **GPU 显存峰值**与**单 epoch 耗时**，填出一张「显存 vs 时间」对照表（这也是本讲规格指定的实践任务）。
2. **操作步骤**（需有 CUDA GPU；无 GPU 的回退方案见第 5 步）：

   准备工作：`cd pytorch_ipynb/mechanics`（notebook 经 `sys.path.insert(0, "..")` 引用上级目录的 helper 模块，保持仓库目录结构完整，见 [L336-L343](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/gradient-checkpointing-nin.ipynb#L336-L343)）。

   以下为**示例代码**（在 notebook 代码基础上改写：保留其模型与数据管线，把 `tracemalloc` 换成 CUDA 显存统计，并加上计时）：

   ```python
   import time, torch
   from helper_data import get_dataloaders_cifar10
   from helper_train import train_classifier_simple_v1

   # 复用 notebook 的设置
   BATCH_SIZE, LEARNING_RATE, SEED = 256, 0.0001, 1
   DEVICE = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
   train_loader, valid_loader, _ = get_dataloaders_cifar10(
       batch_size=BATCH_SIZE, num_workers=2, validation_fraction=0.1)

   def run_benchmark(model_cls, tag, num_epochs=1):
       set_all_seeds(SEED)                       # notebook 中的同名函数
       model = model_cls(num_classes=10).to(DEVICE)
       optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

       # 热身: 先跑几批, 触发 cuDNN/CUDA 初始化, 避免污染计时
       for i, (x, y) in enumerate(train_loader):
           if i >= 3: break
           loss = torch.nn.functional.cross_entropy(
               model(x.to(DEVICE)), y.to(DEVICE))
           optimizer.zero_grad(); loss.backward(); optimizer.step()

       torch.cuda.synchronize()                  # 确保异步 CUDA 工作全部落地
       torch.cuda.reset_peak_memory_stats()      # 显存峰值计数器清零
       start = time.perf_counter()

       train_classifier_simple_v1(num_epochs=num_epochs, model=model,
                                  optimizer=optimizer, device=DEVICE,
                                  train_loader=train_loader,
                                  valid_loader=valid_loader, logging_interval=100)

       torch.cuda.synchronize()
       elapsed = time.perf_counter() - start
       peak_mb = torch.cuda.max_memory_allocated() / 1024**2
       print(f'[{tag}] 1 epoch: {elapsed:.1f}s | peak GPU mem: {peak_mb:.1f} MB')

       model.cpu(); del model, optimizer         # 实验隔离, notebook 同款习惯
       torch.cuda.empty_cache()

   run_benchmark(NiN_baseline, 'baseline')       # 4.2.3 的基线 NiN 类
   run_benchmark(NiN_ckpt_s1,   'ckpt segments=1')  # 4.2.3 的检查点版
   run_benchmark(NiN_ckpt_s4,   'ckpt segments=4')  # 把 segments 改成 4 的变体
   ```

   然后把打印出的数字填进对照表：

   | 配置 | 单 epoch 耗时 (s) | 显存峰值 (MB) | 相对基线 |
   |---|---|---|---|
   | baseline | | | — |
   | checkpoint, segments=1 | | | 显存 −?% / 时间 +?% |
   | checkpoint, segments=4 | | | 显存 −?% / 时间 +?% |

3. **需要观察的现象**：
   - 检查点版的显存峰值明显低于基线（激活不再全部驻留显存）；
   - 检查点版耗时更高（反向时多一次段内前向）；
   - `segments=4` 的显存峰值与耗时都落在 `segments=1` 与基线之间；
   - 各配置的损失序列起步值一致（同种子）。
4. **预期结果**：定性关系如上；具体百分比取决于 GPU 型号、PyTorch 版本与 cuDNN 行为，**待本地验证**。可以参照的方向：文献与作者的 ResNet-18 基准中，显存降幅通常在两位数百分比、时间增幅在个位数到十几百分比之间。
5. **无 GPU 的回退方案**：直接复现 notebook 的 `tracemalloc` 对比（在 CPU 上运行 L582-L593 与 L731-L739 两个 cell），并额外记录 `Total Training Time`——同样能观察到「峰值内存下降、耗时上升、损失序列不变」三个定性结论（数字与 GPU 版不同属正常）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tracemalloc` 的读数（约 140 KB）不能当作该实验的真实显存占用？

**答案**：`tracemalloc` 追踪的是 Python 解释器堆的分配；GPU 张量的存储由 PyTorch 的 CUDA 缓存分配器经 CUDA API 管理，不经过 Python 堆，因此对 `tracemalloc` 不可见。140 KB 只反映 Python 侧对象（如 autograd 簿记）的差异。GPU 显存要用 `torch.cuda.max_memory_allocated()` 度量。

**练习 2**：为什么实测脚本里在正式计时前要做「热身」并调用 `torch.cuda.synchronize()`？

**答案**：首次执行会触发 cuDNN 卷积算法选择、CUDA 上下文与 kernel 编译等一次性开销，直接计入会高估耗时；CUDA 调用是异步的，`perf_counter` 取读数前必须 `synchronize()` 确保此前提交的 GPU 工作全部完成，否则测到的是「提交时间」而非「执行时间」。

**练习 3**：`max_memory_allocated` 与 `memory_allocated` 有何区别？为什么对比实验更应关注前者？

**答案**：`memory_allocated` 是**当前**时刻已分配的显存，`max_memory_allocated` 是自峰值计数器重置以来的**历史最高值**。反向传播阶段（激活 + 梯度 + 优化器状态叠加）往往才是显存峰值，用瞬时值会系统性低估「是否 OOM」的风险，因此 OOM 分析与省显存对比都应看峰值。

## 5. 综合实践

**任务：用梯度检查点「救活」一个 OOM 的训练配置，并量化代价。**

把本讲所有内容串成一次完整实验（建议在 GPU 环境进行）：

1. **制造显存压力**：以 notebook 的检查点版 NiN 为基础，逐步调大 `BATCH_SIZE`（256 → 512 → 1024 …），只训基线版（无检查点），找到**第一个**触发 `CUDA out of memory` 的 batch size，记为 \( B_{\text{oom}} \)。这一步验证 2.1 的账本：显存压力主要来自「中间激活 × batch size」。
2. **检查点救援**：对同一个 \( B_{\text{oom}} \)（可先试 \( B_{\text{oom}} \) 与其一半），切换到 `segments=1` 的检查点版重新训练——预期能跑通；若仍 OOM，把模型中两个池化后的子列表再各包一层 `checkpoint_sequential`（改 `segments` 或分组包装）。
3. **量化代价**：用 4.3.4 的 `run_benchmark` 流程，对「基线 + 可行的最大 batch」「检查点 + 同 batch」「检查点 + \( B_{\text{oom}} \)」三组配置测量单 epoch 耗时与显存峰值。
4. **产出一张实验报告表**，至少包含：配置、batch size、是否 OOM、显存峰值 (MB)、单 epoch 耗时 (s)、2 epoch 后训练准确率，以及一段结论：**你的环境下检查点把可行的 batch size 上限推高了多少？时间代价是多少？**

预期：检查点版在相同 batch size 下显存显著下降、耗时上升；在 \( B_{\text{oom}} \) 下「跑得慢总比跑不了强」——这正是仓库 notebook 开头说的场景：「当 GPU 显存是硬限制、连减小 batch size 都无法绕开时，梯度检查点是让一切跑起来的简单选项」。具体数字**待本地验证**。

## 6. 本讲小结

- 训练显存的变量大头是**中间激活**（随层数 × batch size 线性增长）；标准 autograd 之所以「自动」，是因为它把所有中间激活留在显存里备用。
- 梯度检查点在「全保存（vanilla，显存 \( O(n) \)）」与「全重算（low-memory，计算 \( O(n^2) \)）」之间折中：每 \( \sqrt{n} \) 层存一个检查点，显存 \( O(\sqrt{n}) \)，代价约多一次前向——**用重计算换显存**。
- 仓库 notebook 的接入方式只有三处修改：导入 `checkpoint_sequential`、把 `Sequential._modules` 取成模块列表、`forward` 里 `x.requires_grad = True` 后调用 `checkpoint_sequential(functions=..., segments=1, input=x)`；训练循环零改动。
- `x.requires_grad = True` 是关键坑：段的所有输入都不带梯度时，反向不会为该段重算建图，参数拿不到梯度；Dropout 的一致性由 checkpoint 默认保存/恢复 RNG 状态保证。
- 两个版本逐 batch 损失**完全一致**（2.3045 → … → 1.9474），实证检查点不改变训练数学；实验卫生上，两次运行之间要 `model.cpu(); del model` 隔离内存。
- notebook 用 `tracemalloc` 测得峰值 143,683 → 115,055 字节（作者结论约 −22%）、耗时 0.80 → 0.93 min（约 +14%）；注意 `tracemalloc` 是 Python 堆口径、看不到 CUDA 显存，严格的显存实测应使用 `torch.cuda.reset_peak_memory_stats()` + `torch.cuda.max_memory_allocated()`。

## 7. 下一步学习建议

- **下一讲 u8-l5（多 GPU 并行）**：当单卡显存与算力都不够时，`DataParallel` 把 batch 拆分到多卡、流水线并行把模型按层切分——与本讲的「单卡省显存」互补，两者可以叠加使用。
- **精读 PyTorch 官方文档**中 `torch.utils.checkpoint` 的 `use_reentrant` 参数说明与非顺序模型的 `checkpoint(fn, *args)` 用法（notebook 理论部分引用的 pytorch_memonger 教程 [3] 有完整示范）。
- **回到代码**：把 4.3.4 的 benchmark 脚本套用到你自己在 u3/u5 学过的更深的模型上（如 ResNet-50、DenseNet-121），验证「越深收益越大」的说法；再对照仓库 [pytorch_ipynb/cnn/nin-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb) 看 NiN 完整 40 epoch 训练版的用法。
- **延伸主题**：混合精度训练（AMP）是另一条省显存路线（激活以 fp16/bf16 存储），与检查点正交，可叠加；若你使用 PyTorch Lightning，`Trainer` 的相关策略可参考 u8-l1/l2 的内容。
