# TransformerBlock 与残差连接

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清**残差连接（residual / shortcut connection）**解决什么问题，以及它如何缓解深层网络的「梯度消失」。
2. 理解 **pre-LayerNorm**（归一化在子层之前）这种排布，以及它和经典 post-LayerNorm 的区别。
3. 掌握 `drop_shortcut`（短路路径上的 Dropout）放在**哪个位置**、为什么放在那里。
4. 读懂 `ch04/01_main-chapter-code/gpt.py` 中的 `TransformerBlock`，并能徒手画出它内部的两条「归一化 → 子层 → dropout → 相加」数据流。

本讲是把上一讲（u4-l1）的三个零件 `LayerNorm` / `GELU` / `FeedForward` 和第 3 章的 `MultiHeadAttention` 拼装成一个完整 **Transformer 块**的关键一步。拼好这一块，下一讲（u4-l3）就能把它们堆叠成完整的 `GPTModel`。

## 2. 前置知识

在进入本讲前，确认你已理解以下概念（它们在前序讲义中已建立）：

- **多层感知机 / 深度网络**：多个线性层串联起来。层数越深，表达能力越强，但训练也越难。
- **反向传播与梯度**：训练时，损失对每层权重的梯度由链式法则逐层相乘得到。乘的项越多，梯度越容易「指数级缩小」——这就是**梯度消失（vanishing gradient）**。
- **LayerNorm**（u4-l1）：沿特征维把每个 token 的激活拉成均值 0、方差 1，再用可学习的 `scale` / `shift` 做仿射变换。
- **FeedForward（FFN）**（u4-l1）：`emb_dim → 4·emb_dim → emb_dim` 的瓶颈前馈网络，中间夹 GELU。
- **多头因果注意力 `MultiHeadAttention`**（u3-l3）：带因果掩码的自注意力，输入输出形状都是 `(batch, num_tokens, emb_dim)`。

一个贯穿本讲的关键直觉：**注意力层和前馈层各自都保持形状不变**——输入 `(b, T, emb_dim)`，输出仍是 `(b, T, emb_dim)`。正是因为「形状不变」，我们才能把它们串起来、并在外围套上「`输出 = 输入 + 子层(输入)`」这样的相加结构。

## 3. 本讲源码地图

本讲只涉及一个核心源码文件，但其中包含多个已在前序讲义实现的零件。

| 文件 / 类 | 作用 | 本讲角色 |
| --- | --- | --- |
| [ch04/01_main-chapter-code/gpt.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py) — `TransformerBlock` | 把注意力、前馈、LayerNorm、dropout、残差连接组装成一块 | **本讲主角** |
| 同文件 — `MultiHeadAttention`（第 3 章成品） | 子层之一：因果多头注意力 | 作为 `self.att` 被组装进来 |
| 同文件 — `FeedForward`（第 4 章，u4-l1） | 子层之二：两层前馈网络 | 作为 `self.ff` 被组装进来 |
| 同文件 — `LayerNorm`（第 4 章，u4-l1） | 子层前的归一化 | 作为 `self.norm1 / self.norm2` |
| ch04/01_main-chapter-code/ch04.ipynb（4.4 节） | 用一个 5 层深度网络对比「有无残差」的梯度大小 | 提供「为什么需要残差」的直观证据 |

`gpt.py` 是一个**自包含汇总脚本**（参见 u1-l3 的约定），它把第 2~4 章的稳定成品全部内联进单文件，因此你不需要 `previous_chapters.py` 就能直接运行本讲涉及的所有代码。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开，逻辑顺序是「先讲为什么需要残差，再讲 pre-LayerNorm 的排布，再讲 drop_shortcut 的位置，最后把它们组装成 TransformerBlock」。

### 4.1 残差连接（residual / shortcut connection）

#### 4.1.1 概念说明

GPT 由 12 个（`n_layers=12`）Transformer 块堆叠而成。每块内部又有注意力层和前馈层，叠在一起就是个相当深的网络。深度网络最大的训练难题之一是**梯度消失**：反向传播时，浅层权重的梯度是后面多层导数的连乘积，每经过一层（尤其是会被压到接近 0 的激活函数），梯度就可能被缩小一点，连乘若干次后浅层几乎「学不动」。

**残差连接**（也叫 shortcut / skip connection，最初来自 ResNet）给梯度修了一条「近路」：把一层的**输入直接加到它的输出**上。写成公式，假设某个子层学到的映射是 \(F(x)\)，那么带残差的输出是：

\[
y = x + F(x)
\]

注意这里 \(x\) 和 \(F(x)\) 必须形状一致（这正是注意力/前馈「保持形状不变」的意义）。对 \(x\) 求导：

\[
\frac{\partial y}{\partial x} = 1 + \frac{\partial F}{\partial x}
\]

多出来的那个常数 `1` 是关键——即使 \(\partial F/\partial x\) 很小，梯度仍能沿 `y = x + ...` 这条加法边「无损」地直接回传到浅层，不会再被层层缩小。直观上，残差让网络学到的是「相对输入的**修正量**」\(F(x)\)，而不是全新的表示 \(y\)。

#### 4.1.2 核心流程

notebook 第 4.4 节用一个 5 层小网络（`ExampleDeepNeuralNetwork`）直观证明残差的作用。流程是：

1. 构造一个 5 层全连接网络（每层线性 + GELU），用开关 `use_shortcut` 控制是否加残差。
2. 前向算输出，对一个固定目标算 MSE 损失。
3. 反向传播，打印每层权重的「平均绝对梯度」。
4. 对比 `use_shortcut=False` 与 `True` 两种情况下的梯度大小。

#### 4.1.3 源码精读

残差的核心实现在 `forward` 中只有一行——但它是整个 GPT 能堆到 12 层以上的根本原因。下面先看 `TransformerBlock` 里**注意力子层**的残差写法：

[TransformerBlock.forward 的注意力残差](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L167-L173)：先把输入存进 `shortcut`，做完归一化、注意力、dropout 后，再用 `x = x + shortcut` 把原始输入加回去。

```python
def forward(self, x):
    # Shortcut connection for attention block
    shortcut = x
    x = self.norm1(x)
    x = self.att(x)   # Shape [batch_size, num_tokens, emb_size]
    x = self.drop_shortcut(x)
    x = x + shortcut  # Add the original input back
```

`shortcut = x` 在子层运算之前**保存原始输入的引用**；注意 `x` 之后被重新赋值，但 `shortcut` 仍指向最开始的张量，所以最后的 `x + shortcut` 就是 \(x + F(x)\)。

notebook 第 4.4 节用梯度对比给了「为什么」的硬证据（这里摘录自 `ch04.ipynb` 的运行结果）：

```
# 不加残差：浅层(layers.0)梯度只有 0.0002，几乎学不动
layers.0.0.weight has gradient mean of 0.00020173587836325169
layers.4.0.weight has gradient mean of 0.005049645435065031

# 加残差：浅层梯度跃升到 0.22，深层(layers.4)更达到 1.32
layers.0.0.weight has gradient mean of 0.22169792652130127
layers.4.0.weight has gradient mean of 1.3258540630340576
```

可以看到，没有残差时浅层梯度比深层小约 25 倍（典型的梯度消失曲线）；加上残差后各层梯度被整体拉平、显著增大，这正是 `TransformerBlock` 要把残差用上的原因。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：通过复现上面的梯度对比，亲眼看残差如何「拯救」浅层梯度。
2. **操作步骤**：打开 `ch04/01_main-chapter-code/ch04.ipynb`，找到 4.4 节「Adding shortcut connections」，依次运行定义 `ExampleDeepNeuralNetwork` 和 `print_gradients` 的单元，再分别运行 `use_shortcut=False` 与 `use_shortcut=True` 的两个单元。
3. **需要观察的现象**：`layers.0.0.weight` 的梯度均值，在关残差时是 `~0.0002` 量级，开残差时跃升到 `~0.2` 量级。
4. **预期结果**：开残差后，`layers.0` 到 `layers.4` 的梯度量级趋于接近，不再随层数指数衰减。
5. 若本地未安装环境无法运行：标注「待本地验证」，改为阅读上面摘录的输出文本，自行计算「关/开」两种情况下 `layers.0` 梯度的比值。

#### 4.1.5 小练习与答案

**练习 1**：如果把残差写成 `y = x - F(x)`（减号），对梯度回传有什么影响？

**参考答案**：导数变成 \(\partial y/\partial x = 1 - \partial F/\partial x\)。加法边提供的「+1」常数通路仍然存在，梯度仍可直接回传，符号变了但不影响「缓解梯度消失」这一核心作用；不过残差的标准约定是加号。

**练习 2**：为什么残差相加要求 `x` 和 `F(x)` 形状必须一致？本项目是怎么保证的？

**参考答案**：逐元素相加要求两个张量形状完全相同。本项目里 `MultiHeadAttention` 和 `FeedForward` 都设计成输入输出同为 `(batch, num_tokens, emb_dim)`（FFN 通过 `emb_dim → 4·emb_dim → emb_dim` 先扩后收回到原维度），所以可以直接 `x + F(x)`，无需像 ResNet 那样额外做 1×1 投影来对齐维度。

### 4.2 pre-LayerNorm 结构

#### 4.2.1 概念说明

Transformer 块里有两个子层（注意力、前馈），每个子层都要配一个 LayerNorm。问题是：**LayerNorm 放在子层之前还是之后？** 这有两种流派：

- **post-LayerNorm**（原始 Transformer 论文）：`y = LayerNorm(x + Sublayer(x))`，先相加再归一化。
- **pre-LayerNorm**（GPT-2 及现代 LLM 普遍采用）：`y = x + Sublayer(LayerNorm(x))`，先归一化再进子层。

GPT 用的是 **pre-LayerNorm**。它的好处是：残差路径 `x → +x` 上**没有任何归一化操作**，主路是一条「干净」的加法通道，梯度能沿 `+1` 通路毫无阻挡地贯穿整个模型（这正是 4.1 讲的梯度回传的关键）。如果用 post-LayerNorm，残差主路被 LayerNorm 打断，反而容易重新引入训练不稳定。

#### 4.2.2 核心流程

一个 pre-LayerNorm 子层的计算顺序是：

1. 拷贝输入 `shortcut = x`（为残差做准备）。
2. **先归一化**：`h = LayerNorm(x)`。
3. **再进子层**：`h = Sublayer(h)`（注意力或前馈）。
4. dropout（见 4.3）。
5. **最后相加**：`y = h + shortcut`。

注意第 2 步只把 `x` 的一个归一化副本喂给子层，**原始 `x` 本身没被改动**，所以第 5 步加回去的是真正的原始输入。

#### 4.2.3 源码精读

[TransformerBlock.__init__](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L152-L165)：构造函数里为**两个子层各准备一个独立的 LayerNorm**（`norm1` 给注意力、`norm2` 给前馈），它们各有自己的 `scale` / `shift` 可学习参数，不共享。

```python
self.att = MultiHeadAttention(
    d_in=cfg["emb_dim"],
    d_out=cfg["emb_dim"],
    context_length=cfg["context_length"],
    num_heads=cfg["n_heads"],
    dropout=cfg["drop_rate"],
    qkv_bias=cfg["qkv_bias"])
self.ff = FeedForward(cfg)
self.norm1 = LayerNorm(cfg["emb_dim"])
self.norm2 = LayerNorm(cfg["emb_dim"])
self.drop_shortcut = nn.Dropout(cfg["drop_rate"])
```

pre-LayerNorm 的「先归一化」体现在 forward 的顺序上——`self.norm1(x)` 出现在 `self.att(x)` 之前：

[注意力子层的 pre-LN 顺序](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L170-L171)：`x = self.norm1(x)` 然后 `x = self.att(x)`，归一化在前、子层在后，而 `shortcut` 保存的是归一化之前的原始 `x`。

把整块用伪代码概括（pre-LN 写法）：

```
# 子层 1：注意力
shortcut = x
x = LayerNorm_1(x)     # 先归一化
x = MultiHeadAttention(x)  # 子层
x = drop_shortcut(x)
x = x + shortcut       # 残差（加的是归一化前的原 x）

# 子层 2：前馈
shortcut = x
x = LayerNorm_2(x)     # 先归一化
x = FeedForward(x)     # 子层
x = drop_shortcut(x)
x = x + shortcut       # 残差
```

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：在源码里辨认 pre-LayerNorm 与 post-LayerNorm 的区别。
2. **操作步骤**：对照本节伪代码，逐行读 [gpt.py 的 forward](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L167-L182)，把「归一化」和「子层」的先后顺序抄一遍。
3. **需要观察的现象**：`norm1`/`norm2` 调用都出现在 `att`/`ff` 调用之前，且都在 `x + shortcut` 之前。
4. **预期结果**：确认本实现属于 pre-LayerNorm，残差主路上没有 LayerNorm。
5. 思考题（无需运行）：如果把 `norm1` 挪到 `x = x + shortcut` 之后，会变成哪种流派？为什么残差梯度通路会被打断？

#### 4.2.5 小练习与答案

**练习 1**：pre-LayerNorm 中，残差相加时加的 `shortcut` 是归一化前还是归一化后的 `x`？为什么这很重要？

**参考答案**：加的是**归一化前**的原始 `x`。因为 `shortcut = x` 在 `self.norm1(x)` 之前就保存了。这保证了残差主路是一条未被打磨过的原始通道，梯度可直接回传；若加的是归一化后的值，就削弱了 pre-LN 的优势。

**练习 2**：`norm1` 和 `norm2` 能否共用同一个 LayerNorm 实例？

**参考答案**：不合适。两者作用在统计特性不同的激活上（一个在注意力前、一个在前馈前），各自需要学习不同的 `scale`/`shift`；本项目用两个独立实例，各有独立可学习参数。

### 4.3 drop_shortcut 的位置

#### 4.3.1 概念说明

Dropout 是一种正则化手段：训练时随机把一部分神经元置零，防止过拟合。本项目配置 `drop_rate=0.1`，即训练时随机丢弃 10% 的激活。

关键问题不是「要不要 dropout」，而是「**dropout 放在残差路径的哪个位置**」。本项目特意把它放在 `x + shortcut` 相加**之前**、且作用在「子层输出」上（而不是原始输入 `shortcut` 上）。这种放法叫 **dropout shortcut / 残差路径上的 dropout**：它在训练时随机削弱子层 \(F(x)\) 的贡献，相当于告诉模型「别太依赖某个子层，要学会和原始输入共存」。而原始的 `shortcut` 始终不被 dropout，保证主路信息稳定。

推理时（`model.eval()`）Dropout 自动关闭，全部神经元参与、不做任何丢弃。

#### 4.3.2 核心流程

带 dropout 的残差子层，其前向流程在 4.2 基础上多一步：

1. `shortcut = x`
2. `x = LayerNorm(x)`
3. `x = Sublayer(x)`
4. `x = drop_shortcut(x)`  ← **本节新增**：只对子层输出做 dropout
5. `x = x + shortcut`

注意第 4 步只丢弃 `x`（子层输出），第 5 步相加时 `shortcut` 完整保留。

#### 4.3.3 源码精读

构造函数里 `drop_shortcut` 是一个普通 Dropout 层，**被两个子层共用**：

[drop_shortcut 的定义](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L165)：`self.drop_shortcut = nn.Dropout(cfg["drop_rate"])`，丢弃率来自配置 `drop_rate`。

它在两个子层里被各调用一次，位置都在子层之后、相加之前。先看前馈子层这一段（与注意力子层结构对称）：

[前馈子层的 drop_shortcut](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L176-L180)：`x = self.ff(x)` 后立刻 `x = self.drop_shortcut(x)`，再 `x = x + shortcut`。

```python
# Shortcut connection for feed-forward block
shortcut = x
x = self.norm2(x)
x = self.ff(x)
x = self.drop_shortcut(x)
x = x + shortcut  # Add the original input back
```

可以看到 dropout 只作用在 `self.ff(x)` 的输出上，`shortcut`（原始输入）直接进入相加，不受丢弃影响。

> 小结本项目里 Dropout 的三类用法，方便区分：① `drop_shortcut`（本节）：残差子层内、丢弃子层输出；② `MultiHeadAttention` 内部的 `self.dropout`（u3-l3）：丢弃注意力权重；③ `GPTModel` 里的 `drop_emb`（u4-l3）：丢弃嵌入。它们都共用同一个 `drop_rate`，但作用对象不同。

#### 4.3.4 代码实践（动手型）

1. **实践目标**：直观看到 `drop_shortcut` 在训练/推理两种模式下的差异。
2. **操作步骤**：在仓库根目录启动 Python，复制下面的「示例代码」（非项目原有代码）：

   ```python
   import torch
   import torch.nn as nn

   drop = nn.Dropout(0.5)        # 用 0.5 让效果更明显
   x = torch.ones(1, 5)
   shortcut = x.clone()

   drop.train()                  # 训练模式：会丢弃并放大
   out_train = (drop(x) + shortcut)
   print("train mode:", out_train)

   drop.eval()                   # 推理模式：恒等，不丢弃
   out_eval = (drop(x) + shortcut)
   print("eval mode: ", out_eval)
   ```
3. **需要观察的现象**：训练模式下 `drop(x)` 中约一半元素变 0、其余元素被放大到 2（PyTorch 的 inverted dropout：保留的值乘以 `1/(1-p)`，保证期望不变）；推理模式下 `drop(x)` 等于原值。
4. **预期结果**：训练模式输出元素在 `{1, 3}` 附近波动（1 + 0 或 1 + 2）；推理模式输出全是 `2`（1 + 1）。
5. 若本地无法运行：标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `drop_shortcut(x)` 改成 `drop_shortcut(shortcut)`（丢弃原始输入而不是子层输出），会有什么问题？

**参考答案**：那样会在「干净的主路」上引入随机丢弃，破坏残差「稳定回传」的优势；训练时浅层梯度通路会被随机打断，违背了 4.1 所讲的残差初衷。本项目特意只丢弃子层输出、保留主路。

**练习 2**：为什么推理时不需要 dropout，而 `model.eval()` 能自动关闭它？

**参考答案**：推理要的是确定性、且要用全部神经元的贡献，丢弃会降低质量并引入随机性。`nn.Dropout` 在内部根据模块的 `training` 标志决定行为，`model.eval()` 会把整个模型（含子模块）的 `training` 设为 `False`，从而自动跳过丢弃。

### 4.4 TransformerBlock 组装

#### 4.4.1 概念说明

把前三个模块（残差、pre-LN、drop_shortcut）拼起来，就得到了一个完整的 **TransformerBlock**。它是 GPT 的基本重复单元：`GPTModel` 把这种块**堆叠 12 次**（`n_layers=12`），每次结构完全相同。

一个块内部有**两条对称的残差子路**：
- 子层 A：`LayerNorm → 多头因果注意力 → dropout → 残差相加`。
- 子层 B：`LayerNorm → 前馈网络 → dropout → 残差相加`。

注意力的输出再喂给前馈，两者串联；每个子层各自独立地做 pre-LN 和残差。整个块的输入输出形状恒为 `(batch, num_tokens, emb_dim)`，因此可以无限堆叠。

#### 4.4.2 核心流程

`TransformerBlock.forward` 的完整数据流如下（请配合下面的数据流图理解）：

```
            输入 x  (batch, num_tokens, emb_dim)
              │
              ├──→ shortcut_A = x ─────────────────────┐
              │                                        │
              ▼                                        │
          norm1(x)  LayerNorm                          │
              │                                        │
              ▼                                        │
          att(h)   多头因果注意力                       │
              │                                        │
              ▼                                        │
       drop_shortcut(h)  Dropout                       │
              │                                        │
              ▼                                        ▼
              h + shortcut_A  ──── 残差相加 ────────────┘
                  │
                  ├──→ shortcut_B = h ─────────────────┐
                  │                                    │
                  ▼                                    │
              norm2(h)  LayerNorm                      │
                  │                                    │
                  ▼                                    │
              ff(h)   前馈网络                          │
                  │                                    │
                  ▼                                    │
           drop_shortcut(h)  Dropout                   │
                  │                                    │
                  ▼                                    ▼
                  h + shortcut_B ──── 残差相加 ─────────┘
                      │
                      ▼
            输出  (batch, num_tokens, emb_dim)
```

两条「子层残差支路」结构完全对称，区别仅在于中间的子层一个是注意力、一个是前馈。

#### 4.4.3 源码精读

完整 `TransformerBlock` 类如下，请重点读 `forward` 里两段对称的残差写法：

[TransformerBlock 完整实现](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L152-L182)：

```python
class TransformerBlock(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.att = MultiHeadAttention(
            d_in=cfg["emb_dim"],
            d_out=cfg["emb_dim"],
            context_length=cfg["context_length"],
            num_heads=cfg["n_heads"],
            dropout=cfg["drop_rate"],
            qkv_bias=cfg["qkv_bias"])
        self.ff = FeedForward(cfg)
        self.norm1 = LayerNorm(cfg["emb_dim"])
        self.norm2 = LayerNorm(cfg["emb_dim"])
        self.drop_shortcut = nn.Dropout(cfg["drop_rate"])

    def forward(self, x):
        # Shortcut connection for attention block
        shortcut = x
        x = self.norm1(x)
        x = self.att(x)   # Shape [batch_size, num_tokens, emb_size]
        x = self.drop_shortcut(x)
        x = x + shortcut  # Add the original input back

        # Shortcut connection for feed-forward block
        shortcut = x
        x = self.norm2(x)
        x = self.ff(x)
        x = self.drop_shortcut(x)
        x = x + shortcut  # Add the original input back

        return x
```

几个值得留意的细节：

- `forward` 里 `shortcut` 这个变量名被**复用了两次**——第一段用完相加后，第二段又重新赋值 `shortcut = x`（此时的 `x` 已是注意力子层的输出）。这是有意的简洁写法，不会出错，因为前一个 `shortcut` 在相加之后就不再被需要。
- `__init__` 里的参数全部来自配置字典 `cfg`（即 `GPT_CONFIG_124M`）：`emb_dim=768` 决定所有层的特征维度，`n_heads=12` 决定注意力头数，`context_length=1024` 决定因果掩码大小，`drop_rate=0.1` 决定所有 dropout 强度。
- 这个块会被 `GPTModel` 用列表推导式复制 12 份并塞进 `nn.Sequential`：

[trf_blocks 的堆叠](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L192-L193)：`*[TransformerBlock(cfg) for _ in range(cfg["n_layers"])]`，每份是结构相同、参数不同的独立块。

#### 4.4.4 代码实践（动手型，本讲核心实践）

这是本讲要求的实践：实例化一个 `TransformerBlock`，输入随机张量，验证输入输出形状一致，并对照源码画出残差数据流图。

1. **实践目标**：确认 TransformerBlock 是「形状保持」的，并用手画/文本方式还原残差数据流。
2. **操作步骤**：
   - 进入 `ch04/01_main-chapter-code/` 目录，因为 `gpt.py` 是自包含脚本，可直接 import：

     ```bash
     cd ch04/01_main-chapter-code
     python3 -c "import gpt"   # 确认能正常导入（会定义所有类，不会跑 main）
     ```
   - 新建一个临时脚本（示例代码，**不要**写入仓库），内容如下：

     ```python
     # 示例代码：验证 TransformerBlock 形状保持
     import torch
     from gpt import TransformerBlock, GPT_CONFIG_124M

     torch.manual_seed(123)
     cfg = GPT_CONFIG_124M
     block = TransformerBlock(cfg)

     x = torch.rand(2, 4, cfg["emb_dim"])   # (batch=2, num_tokens=4, emb_dim=768)
     out = block(x)

     print("Input shape :", tuple(x.shape))
     print("Output shape:", tuple(out.shape))
     print("Shape preserved:", x.shape == out.shape)
     ```
   - 运行该脚本（与 `gpt.py` 同目录，确保 `from gpt import ...` 可用）。
3. **需要观察的现象**：输入输出形状都是 `(2, 4, 768)`，`Shape preserved` 打印 `True`；输出数值与输入不同（说明块确实做了变换）。
4. **预期结果**：`Input shape: (2, 4, 768)` 与 `Output shape: (2, 4, 768)` 完全一致。
5. **画数据流图**：参照 4.4.2 的流程图，在纸上（或文本里）把 `shortcut = x → norm1 → att → drop_shortcut → +shortcut → norm2 → ff → drop_shortcut → +shortcut` 这条链画出来，标注每一步张量形状仍为 `(2, 4, 768)`。
6. 若本地未安装 torch/tiktoken 无法运行：标注「待本地验证」；可改为纯阅读实践——逐行读 [forward 源码](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L167-L182) 并在注释里手动标注每一步的 `(2,4,768)` 形状。

#### 4.4.5 小练习与答案

**练习 1**：`forward` 中第一个 `shortcut` 变量在第二段残差里被重新赋值，会丢失第一段的输入吗？为什么安全？

**参考答案**：不会丢失。第一段的 `shortcut` 在 `x = x + shortcut` 执行完（残差相加完成）后就不再被使用，此时它已完成了使命；第二段重新赋值 `shortcut = x`（指向注意力子层的输出）是符合预期的，没有任何逻辑依赖前一个值，所以安全。

**练习 2**：如果把同一个 `TransformerBlock` 实例连放两次（`block(block(x))`），形状会对吗？这和 `GPTModel` 堆叠 12 个块是一回事吗？

**参考答案**：形状会对（仍是 `(2,4,768)`），因为块是形状保持的。但这**不是** `GPTModel` 的做法：`GPTModel` 用列表推导式创建了 12 个**结构相同但参数独立**的块（`[TransformerBlock(cfg) for _ in range(12)]`）。连放同一实例两次会让两层共享权重，等价于一个 2 层但权重绑定的网络，表达能力远不如 12 个独立块。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个小任务：

**任务**：用「关掉残差」的对比实验，亲手验证残差对深层 Transformer 的作用。

1. 复制 `gpt.py` 中 `TransformerBlock` 的定义到你的临时脚本（示例代码），并改造出一个**无残差版本** `TransformerBlockNoResidual`：把两处 `x = x + shortcut` 删掉（保留 pre-LN、子层和 drop_shortcut）。
2. 用 `GPT_CONFIG_124M` 分别实例化一个 10 层的「有残差」版本和「无残差」版本（用 `nn.Sequential(*[Block(cfg) for _ in range(10)])`）。
3. 输入一个随机张量 `x = torch.rand(2, 8, 768)`，分别前向得到输出，再对一个固定目标算 MSE 损失并 `backward()`。
4. 打印每个块里 `att.W_query.weight` 的平均绝对梯度（用 `named_parameters` 过滤）。
5. **观察**：有残差版本各层梯度量级接近；无残差版本靠近输入的块梯度明显更小（梯度消失）。
6. **预期结果**：与 4.1.3 notebook 的梯度对比结论一致——残差让浅层梯度显著增大、各层更均匀。
7. 若无法运行：标注「待本地验证」，改为阅读理解——解释为什么删掉 `x = x + shortcut` 后，梯度回传通路会重新变长、从而加剧梯度消失。

这个任务同时检验了你对**残差连接（4.1）、pre-LayerNorm（4.2）、drop_shortcut（4.3）、块的堆叠（4.4）**四个模块的理解。

## 6. 本讲小结

- **残差连接**把输入直接加到子层输出上 \(y = x + F(x)\)，靠加法边的常数导数 `1` 给梯度开了一条近路，缓解深层网络的梯度消失；notebook 4.4 节的梯度对比是其硬证据。
- **pre-LayerNorm** 把归一化放在子层**之前**（`x + Sublayer(LayerNorm(x))`），让残差主路保持「干净」，是 GPT-2 及现代 LLM 的通用选择。
- **drop_shortcut** 放在「子层输出 → 相加」之间，只丢弃子层贡献、不丢弃原始 `shortcut`，既正则化又不破坏主路稳定；推理时随 `model.eval()` 自动关闭。
- **TransformerBlock** 由两条对称的残差子路组成（注意力 + 前馈），输入输出形状恒为 `(batch, num_tokens, emb_dim)`，因此可被 `GPTModel` 堆叠 12 次。
- 本讲涉及的零件（`MultiHeadAttention`、`LayerNorm`、`GELU`、`FeedForward`）全部来自前序章节，`gpt.py` 把它们内联汇总成可独立运行的脚本。

## 7. 下一步学习建议

下一讲 **u4-l3 GPT 模型组装与配置** 将把本讲的 `TransformerBlock` 嵌入完整 `GPTModel`：在块的前面加上 token/位置嵌入与 `drop_emb`，在后面加上 `final_norm`（又一个 LayerNorm）和 `out_head`（线性输出头），并解读 `GPT_CONFIG_124M` 中每个超参的含义。建议继续阅读：

- [ch04/01_main-chapter-code/gpt.py 的 GPTModel](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L185-L207)：看 `trf_blocks` 如何堆叠本讲的块。
- ch04.ipynb 的 4.6 节「Coding the GPT model」：完整 GPT 的组装与参数量核算（含 weight tying 的讨论）。
- 之后再进入 u4-l4 的简单文本生成，以及第 5 章的预训练循环。
