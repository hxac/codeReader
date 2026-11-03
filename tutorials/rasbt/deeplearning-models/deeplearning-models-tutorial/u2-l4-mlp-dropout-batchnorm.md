# 正则化 MLP：Dropout 与 BatchNorm

## 1. 本讲目标

上一讲（u2-l3）我们搭建了 784-128-256-10 的多层感知机，在 MNIST 上拿到 97.54% 的测试准确率，同时也观察到「训练准确率 99.00% > 测试准确率 97.54%」的过拟合苗头。本讲沿着这条线索，精读仓库中另外两个几乎逐行相同的 notebook：

- `mlp-dropout.ipynb`：在两个隐藏层后插入 Dropout；
- `mlp-batchnorm.ipynb`：在两个隐藏层的线性变换后插入 BatchNorm1d。

学完本讲，你应当能够：

1. 说清 Dropout 在训练/评估两种模式下的行为差异，以及为什么必须在循环里调用 `model.train()` 和 `model.eval()`；
2. 说清 BatchNorm1d 的「训练用批统计、评估用 running statistics」双轨机制；
3. 通过三份只差几行代码的 notebook 做对照实验，量化正则化对「训练准确率 − 测试准确率」这个泛化缺口的影响，并理解正则化并非越多越好。

## 2. 前置知识

**过拟合（overfitting）**：模型在训练集上表现很好、在未见过的测试集上明显变差。度量它最直观的指标是**泛化缺口**：

\[ \text{gap} = \text{训练集准确率} - \text{测试集准确率} \]

**正则化（regularization）**：一切「限制模型拟合能力、换取更好泛化」的手段。本讲涉及两种：Dropout（随机失活神经元）和 BatchNorm（对层间激活做标准化）。参数化正则化（weight decay）不在本讲 notebook 中出现，此处不展开。

**train / eval 双模式**：PyTorch 的 `nn.Module` 内部有一个布尔标志 `self.training`。`model.train()` 把它设为 `True`，`model.eval()` 设为 `False`。Dropout 和 BatchNorm 是**仅有的两类行为随该标志改变的标准层**——训练时引入随机性或使用批统计，评估时切换为确定性推理。忘记切换是最常见的初学者 bug 之一。

**逐特征（per-feature）标准化**：对形状为 `(batch_size, num_features)` 的张量，BatchNorm 在 batch 维度上对每一列（每个特征通道）独立计算均值和方差。这和 u2-l2 提过的输入标准化（用整个训练集的统计量）不同，BatchNorm 用的是**当前 mini-batch 的统计量**，且发生在网络内部每一层。

其余前置（五步训练循环、`nn.Module` 写法、`view(-1, 784)` 展平、手工初始化要 `detach()`）沿用 u2-l2 / u2-l3 的结论，本讲不重复。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/mlp/mlp-dropout.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb) | 在 mlp-basic 基础上加 Dropout 的 MNIST 分类 notebook，10 epoch 测试准确率 96.71% |
| [pytorch_ipynb/mlp/mlp-batchnorm.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb) | 在 mlp-basic 基础上加 BatchNorm1d 的 MNIST 分类 notebook，10 epoch 测试准确率 97.82% |
| [pytorch_ipynb/mlp/mlp-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb) | 上一讲的无正则化基线，测试准确率 97.54%，本讲作对照组引用 |

> **关于行号的说明**：notebook 在 GitHub 上本质是一个 JSON 文件，本文所有永久链接的 `#L` 行号都对应 .ipynb **原始 JSON 文件的行号**（已逐行核对），在 GitHub 页面点击链接即可高亮对应位置。文中引用的准确率数字均为作者保存在 notebook 里的真实运行输出（对应环境为 Python 3.6.8 + torch 1.0.0）。

三份 notebook 的 Settings、Dataset、`compute_accuracy`、训练循环**完全相同**（`random_seed=1`、`learning_rate=0.1`、`num_epochs=10`、`batch_size=64`、同样的手工 `normal_(0.0, 0.1)` 初始化），唯一的差别是模型 `forward` 里各多了两三行——这构成了一个设计良好的**受控对照实验**，结果差异可以放心归因于 Dropout / BatchNorm 本身。

## 4. 核心概念与源码讲解

### 4.1 Dropout 与模式切换

#### 4.1.1 概念说明

Dropout（Srivastava et al., 2014）的直觉：如果网络中任何单个神经元都可能被临时「关掉」，那么没有任何一个神经元可以独自扛起来某条判断——特征必须分散地、冗余地存储在多个神经元里，这恰好是「不过分依赖个别通路」的正则化。

它同时可以看作一种廉价的**集成学习（ensemble）**：每个 mini-batch 采样到一个不同的「子网络」共同训练，推理时再把它们平均。理论上，对使用了 inverted dropout（见下）的网络做期望运算，近似等价于把所有子网络的预测做了权重平均。

#### 4.1.2 核心流程

训练时，对隐藏层输出 \( h \) 的每个元素独立采样掩码 \( m_i \sim \mathrm{Bernoulli}(1-p) \)（以概率 \( p \) 被置零），并把存活元素放大 \( \frac{1}{1-p} \) 倍：

\[ \tilde{h}_i = \frac{m_i \, h_i}{1-p}, \qquad m_i \in \{0,1\},\; P(m_i=1)=1-p \]

由于 \( E[m_i] = 1-p \)，有 \( E[\tilde{h}_i] = h_i \)——**训练时的期望输出与不Dropout时相同**，这一技巧叫 inverted dropout。好处是评估时**什么都不用做**（恒等变换），无需在测试阶段做任何缩放补偿。

伪代码总结两种模式：

```text
训练模式 (training=True):  out = out * mask / (1 - p)   # mask 每次前向随机重采样
评估模式 (training=False): out = out                     # 原样通过
```

#### 4.1.3 源码精读

**① 超参数：`dropout_prob = 0.5`**

[pytorch_ipynb/mlp/mlp-dropout.ipynb:L105-L109](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L105-L109) 定义了随机种子、学习率、epoch 数、batch size，第 109 行新增了本讲主角 `dropout_prob = 0.5`——每个隐藏层输出神经元在训练时有 50% 概率被置零。注意它是模块级全局变量，`forward` 里直接引用。

**② 模型 forward：两处 `F.dropout`**

[pytorch_ipynb/mlp/mlp-dropout.ipynb:L184-L195](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L184-L195) 是与 mlp-basic 唯一不同的 forward：每个隐藏层都是 `Linear → ReLU → Dropout` 三连，两次调用（第 187、191 行）完全一致：

```python
out = self.linear_1(x)
out = F.relu(out)
out = F.dropout(out, p=dropout_prob, training=self.training)
```

注意 `F.dropout` 是**函数式 API**，它不会自动感知模块状态，所以必须显式传入 `training=self.training`——模块的 `self.training` 标志正是由 `model.train()` / `model.eval()` 翻转的。漏传这个参数，Dropout 会在评估时依然随机失活，测试准确率会明显偏低。等价的模块式写法是在 `__init__` 里放 `self.drop = torch.nn.Dropout(p=0.5)`，它在 forward 时自动读取 `self.training`，本 notebook 选择了函数式风格。

与 mlp-dropout 相对照，[pytorch_ipynb/mlp/mlp-batchnorm.ipynb:L105-L114](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L105-L114) 的超参数块里**没有** `dropout_prob` 这一行，两份 notebook 的差异就是从这一行开始分叉的。

**③ 训练循环：每个 epoch 调 `model.train()`**

[pytorch_ipynb/mlp/mlp-dropout.ipynb:L445-L476](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L445-L476) 的训练循环与 u2-l3 的五步范式（forward → loss → `zero_grad` → `backward` → `step`）完全一致，唯一的新增点是第 447 行的 `model.train()`：每个 epoch 开始把模块（连带所有子层）切回训练模式，重新启用随机失活。这一行在 u2-l3 的 mlp-basic 里其实没有意义（纯 Linear+ReLU 网络两种模式行为相同），但从本讲起它变成必需。

**④ 评估函数：`net.eval()` + `torch.no_grad()`**

[pytorch_ipynb/mlp/mlp-dropout.ipynb:L431-L442](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L431-L442) 的 `compute_accuracy` 第 432 行先调 `net.eval()` 关闭随机性，第 434 行再包一层 `torch.no_grad()` 停止梯度追踪。两行各管一件事：前者改**层的行为**，后者省**显存与计算**。训练循环每个 epoch 末尾（第 470-472 行）和最终测试（第 493 行）都调用它，所以训练日志里的「training accuracy」其实也是 eval 模式下算出来的——Dropout 已被关闭。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认 Dropout 在两种模式下的输出差异，理解 `self.training` 的作用。
2. **操作步骤**：在 notebook 中模型实例化之后追加一个测试 cell（以下为**示例代码**，非仓库原有内容）：

```python
# 示例代码：观察 Dropout 在 train/eval 模式下的行为
torch.manual_seed(0)
x = torch.ones(1, 10)          # 10 个全为 1 的特征

model.eval()
print(model.training, model.linear_1.training)   # False False
h_eval = F.relu(model.linear_1(x))
h_eval = F.dropout(h_eval, p=0.5, training=model.training)
print(h_eval)                  # eval 模式：ReLu 后的激活原样通过

model.train()
h_train = F.relu(model.linear_1(x))
h_train = F.dropout(h_train, p=0.5, training=model.training)
print(h_train)                 # train 模式：约一半为 0，其余约为存活值的 2 倍
print(h_train.sum(), h_eval.sum())  # 均值应大致接近（inverted dropout）
```

3. **需要观察的现象**：eval 模式输出与不做 Dropout 完全一致；train 模式下约一半元素为 0，非零元素的量级约为 eval 模式的 2 倍（因为放大了 \( 1/(1-0.5)=2 \) 倍）。
4. **预期结果**：`h_train` 的平均值与 `h_eval` 的平均值接近（单次采样有随机波动，可多跑几次取平均）。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 forward 里的 `training=self.training` 误写成 `training=True`（硬编码），训练和测试会发生什么？

**答案**：训练不受影响；但评估时 Dropout 仍然随机失活——同一批测试样本每前向一次得到不同预测，`compute_accuracy` 的结果偏低且不可复现。这正是函数式 API 要求显式传 `training` 的原因。

**练习 2**：为什么 inverted dropout 要在训练时乘 \( \frac{1}{1-p} \)，而不是在测试时除以 \( 1-p \)？

**答案**：把缩放放进训练阶段，训练时的期望输出就和最终推理的网络口径一致，推理路径完全不引入额外计算；如果反过来在测试时缩放，训练与推理的数值口径不一致，且推理多一步运算（原始论文的做法正是后者，现代实现普遍采用 inverted dropout）。

**练习 3**：`p=0.5` 时，一个 256 维的隐藏层每次前向平均有多少神经元被置零？这对有效容量意味着什么？

**答案**：平均 128 个被置零。每个 batch 实际参与计算的是一个约 128 维有效宽度的子网络，多个不同子网络共享其余参数，迫使特征冗余分布、降低对个别神经元的依赖，从而抑制过拟合。

### 4.2 BatchNorm1d

#### 4.2.1 概念说明

BatchNorm（Ioffe & Szegedy, 2015）的原始动机是缓解**内部协变量偏移**：深层网络里，前面层参数一变，后面层看到的激活分布就跟着变，训练像在追逐移动靶。BatchNorm 在层间对激活按特征通道做标准化，把分布拉回均值 0、方差 1 附近，再用**可学习参数** \( \gamma \)（缩放）和 \( \beta \)（平移）恢复表达能力——归一化是「打底」，网络若觉得不该归一化，可以自己学回去。

对入门者更实用的理解是两条经验事实：它通常**显著加速收敛**（可用更大的学习率），并带轻微正则化效果（mini-batch 统计量的噪声本身就在扰动激活）。

#### 4.2.2 核心流程

设第 \( j \) 个特征通道在当前 batch（大小 \( m \)）上的取值为 \( x_{1j},\dots,x_{mj} \)。训练时：

\[ \mu_j = \frac{1}{m}\sum_{i=1}^{m} x_{ij}, \qquad \sigma_j^2 = \frac{1}{m}\sum_{i=1}^{m} (x_{ij}-\mu_j)^2 \]

\[ \hat{x}_{ij} = \frac{x_{ij}-\mu_j}{\sqrt{\sigma_j^2+\epsilon}}, \qquad y_{ij} = \gamma_j \hat{x}_{ij} + \beta_j \]

其中 \( \epsilon \) 是防止除零的小常数。同时，每个通道维护**跨 batch 的滑动统计**（running statistics），默认动量 \( \lambda = 0.1 \)：

\[ \bar{\mu}_j \leftarrow (1-\lambda)\,\bar{\mu}_j + \lambda\,\mu_j, \qquad \bar{\sigma}^2_j \leftarrow (1-\lambda)\,\bar{\sigma}^2_j + \lambda\,\sigma^2_j \]

评估时不再依赖 batch，改用累积好的 \( \bar{\mu}_j, \bar{\sigma}^2_j \) 做确定性的归一化。（细节：PyTorch 归一化用的是有偏方差 \( \sigma_j^2 \)，而 running_var 按 \( m/(m-1) \) 修正为无偏估计后累积。）

双模式对比：

| 模式 | 归一化统计量来源 | 是否更新 running stats | 是否确定性 |
| --- | --- | --- | --- |
| train（`model.train()`） | 当前 mini-batch | 是 | 否（依赖 batch 成分） |
| eval（`model.eval()`） | running stats | 否 | 是 |

#### 4.2.3 源码精读

**① `__init__`：每层 Linear 配一个 BatchNorm1d**

[pytorch_ipynb/mlp/mlp-batchnorm.ipynb:L158-L183](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L158-L183) 在与 mlp-dropout 完全相同的类结构里，第 172 行新增 `self.linear_1_bn = torch.nn.BatchNorm1d(num_hidden_1)`，第 178 行新增 `self.linear_2_bn = torch.nn.BatchNorm1d(num_hidden_2)`。**关键细节：BatchNorm1d 的参数是通道数（128 / 256），不是输入特征数 784**——它作用在 Linear 层的**输出**上，输出有 `num_hidden` 个通道，逐通道维护 \( \gamma_j, \beta_j, \bar{\mu}_j, \bar{\sigma}^2_j \)。每个 BatchNorm1d 因此持有 2 个可学习参数（weight=\( \gamma \)、bias=\( \beta \)）和 2 个缓冲区（running_mean、running_var），缓冲区不是参数、不被梯度更新，只做指数滑动平均。

**② forward：BN 放在激活之前**

[pytorch_ipynb/mlp/mlp-batchnorm.ipynb:L185-L198](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L185-L198) 的 forward 顺序是 `Linear → BatchNorm → ReLU`。第 187-188 行的注释明确说明了这个选择：*"note that batchnorm is in the classic sense placed before the activation"*——经典论文的位置是先归一化线性输出、再过非线性。输出层 `linear_out` 之后**没有**接 BN：分类头的 logits 直接进 softmax/交叉熵即可。

**③ γ 和 β 交给优化器**

[pytorch_ipynb/mlp/mlp-batchnorm.ipynb:L201-L207](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L201-L207) 第 207 行依旧是 `optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)`。BatchNorm 的 \( \gamma \)、\( \beta \) 是通过 `nn.Parameter` 注册的，会自动出现在 `model.parameters()` 里被 SGD 一起优化；而 running_mean / running_var 是 buffer，不在这份参数列表中——这就是「可学习的归一化」中『可学习』的落地方式。

**④ 模式切换同样必需**

[pytorch_ipynb/mlp/mlp-batchnorm.ipynb:L434-L445](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L434-L445) 的 `compute_accuracy` 第 435 行 `net.eval()`、训练循环 [L448-L478](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L448-L478) 第 450 行 `model.train()`，与 Dropout 版一一对应。若评估时忘记 `eval()`，BN 会用「当前那一个 batch」的统计量归一化：轻则指标随 batch 成分抖动，重则在单样本推理（batch_size=1）时因每通道只有一个观测值而直接报错——这是排查 BN 相关 bug 时首先检查的位置。

**⑤ 顺带一提：BN 让手工初始化的重要性下降**

两份 notebook 对 Linear 层都做了 `normal_(0.0, 0.1)` 手工初始化（如 [mlp-batchnorm.ipynb:L170-L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L170-L171)）。BN 紧随 Linear 之后对输出重新归一化，理论上能削弱权重初始尺度的影响——这也是 BN 论文宣称的收益之一。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 running statistics 在训练过程中被逐步更新，并在 eval 模式下被使用。
2. **操作步骤**：在 mlp-batchnorm.ipynb 中，模型实例化后、训练前，插入打印；训练 1 个 epoch 后再打印（以下为**示例代码**）：

```python
# 示例代码：观察 running_mean / running_var 的演化
bn = model.linear_1_bn
print('训练前 running_mean 前几个通道:', bn.running_mean[:4])
print('训练前 running_var  前几个通道:', bn.running_var[:4])
print('已跟踪的 batch 数 num_batches_tracked:', bn.num_batches_tracked)
# ……执行 1 个 epoch 的训练后再次打印，并对比
```

3. **需要观察的现象**：训练前 `running_mean` 全 0、`running_var` 全 1（PyTorch 的初始化值）；每个训练 batch 前向一次后 `num_batches_tracked` 加 1，`running_mean/var` 逐 batch 向数据真实统计量滑动；训练后数值明显偏离初始的 0 / 1。
4. **预期结果**：10 个 epoch（938 batch/epoch）后，`running_mean` 的各通道值是一组非零小数，`running_var` 是一组偏离 1 的正数；用 `model.eval()` 后前向两次同一输入，输出完全一致。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BatchNorm1d` 的参数写的是 `num_hidden_1`（128）而不是 `num_features`（784）？

**答案**：因为 BN 放在 `linear_1` **之后**，作用对象是 Linear 的输出激活（128 个通道），逐通道做归一化。若放在输入和第一层之间，参数才是 784。

**练习 2**：BatchNorm 放在 ReLU 之前还是之后？本 notebook 怎么选的？

**答案**：经典做法（原论文与本 notebook）是 `Linear → BN → ReLU`，先归一化再过非线性；也有实践把 BN 放在激活甚至一层开头（pre-activation，ResNet v2 风格）。两者都被广泛使用，本 notebook 第 187-188 行注释点明了它采用的是经典位置。

**练习 3**：为什么训练时不能用 running stats 来归一化，而一定要用当前 batch 的统计量？

**答案**：训练早期 running stats 还停留在初始值（均值 0、方差 1），与真实分布相去甚远，用它归一化会引入系统性偏差；用批统计可以保证每一步归一化都是「当下最准」的，同时批统计的噪声还带来轻微正则化。running stats 的职责是**为推理攒一份**与训练末期分布匹配的、确定性的全局统计量。

### 4.3 过拟合观察：三份 notebook 的对照实验

#### 4.3.1 概念说明

单看一个模型的准确率意义有限；把「基线 / +Dropout / +BatchNorm」三份**只差几行代码、其余配置完全相同**的 notebook 放在一起，才构成可归因的对照实验。我们关心三个量：第 1 个 epoch 的训练准确率（收敛速度）、第 10 个 epoch 的训练准确率（拟合能力）、测试准确率（泛化），以及 train − test 的差值（泛化缺口）。

#### 4.3.2 核心流程

作者保存的运行结果汇总（均来自同一配置：seed=1, lr=0.1, 10 epochs, batch=64, normal_(0,0.1) 初始化）：

| notebook | Epoch 1 训练准确率 | Epoch 10 训练准确率 | 测试准确率 | 泛化缺口 |
| --- | --- | --- | --- | --- |
| mlp-basic（基线，u2-l3） | 94.90% | 99.00% | 97.54% | 1.46 |
| mlp-dropout（本讲 4.1） | 93.04% | 97.52% | 96.71% | **0.81** |
| mlp-batchnorm（本讲 4.2） | 95.63% | 99.61% | 97.82% | 1.79 |

读表方法：

- **Dropout 压缩了缺口但没提升测试准确率**。缺口从 1.46 缩到 0.81，正则化确实生效；但训练准确率同时从 99.00% 掉到 97.52%，测试准确率反而降到 96.71%。在本例（MNIST + 中等容量 MLP + 仅 10 epoch）中模型本来过拟合就不严重，再砍一半有效容量属于「过度用药」。**正则化的收益取决于基线过拟合的严重程度**。
- **BatchNorm 加速收敛且略提升泛化**。第 1 个 epoch 训练准确率 95.63% > 基线 94.90%，最终训练准确率 99.61% 为三者最高，测试准确率 97.82% 也最高；代价是缺口最大（1.79）——它把模型拟合能力放得更大，在更小的数据集/更长的训练里这个缺口可能转化为过拟合。
- 三个数字都是**同一实验设计下的单次运行结果**（固定 seed），不是多次重复的平均；差异在 1% 上下，做结论时应保留这份审慎。

#### 4.3.3 源码精读

以上数字的出处（notebook 保存的输出行）：

- 基线 epoch 1 / epoch 10 训练准确率：[mlp-basic.ipynb:L230](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L230)、[mlp-basic.ipynb:L419](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L419)；测试准确率 97.54%：[mlp-basic.ipynb:L483](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L483)。
- Dropout 版 epoch 1（93.04%）/ epoch 10（97.52%）训练准确率：[mlp-dropout.ipynb:L235](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L235)、[mlp-dropout.ipynb:L424](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L424)；测试准确率 96.71%：[mlp-dropout.ipynb:L488](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L488)，由 [mlp-dropout.ipynb:L493](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L493) 打印。
- BatchNorm 版 epoch 1（95.63%）/ epoch 10（99.61%）训练准确率：[mlp-batchnorm.ipynb:L238](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L238)、[mlp-batchnorm.ipynb:L427](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L427)；测试准确率 97.82%：[mlp-batchnorm.ipynb:L490](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-batchnorm.ipynb#L490)。

#### 4.3.4 代码实践

1. **实践目标**：判断「更长的训练会不会放大三个模型的过拟合差异」。
2. **操作步骤**：把 `num_epochs` 从 10 改成 30（只需改 [mlp-dropout.ipynb:L107](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb#L107) 这一行，其余不动），对 mlp-basic 与 mlp-batchnorm 做同样修改，分别重跑并记录每 epoch 的训练准确率与最终测试准确率。
3. **需要观察的现象**：训练准确率是否持续逼近 100%；测试准确率在多少 epoch 后停止上升甚至回落；Dropout 版的训练准确率是否依旧明显低于另外两个。
4. **预期结果**：三个模型的训练准确率都会继续升高，Dropout 版始终最低；测试准确率的提升幅度递减。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Dropout 版的测试准确率反而低于无正则化基线，这与「Dropout 是正则化、正则化应该提升泛化」矛盾吗？

**答案**：不矛盾。正则化只有在「模型确实过拟合」时才有正收益；本例基线缺口仅 1.46 个百分点，过拟合本就轻微，p=0.5 的失活主要伤害了拟合能力（训练准确率降了 1.48 个百分点）。正则化强度需要与过拟合程度匹配。

**练习 2**：BatchNorm 版缺口最大（1.79）却测试准确率最高，这说明什么？

**答案**：缺口大小与泛化好坏不是一回事——高容量模型可以「训练准确率和测试准确率同时更高」而缺口仍偏大。缺口衡量的是相对过拟合程度，测试准确率才是泛化的最终度量；两者要一起看。

**练习 3**：若把 mlp-dropout 的 `learning_rate=0.1` 原样搬到加 BN 的模型（或反之），结论还能归因于「加不加 BN / Dropout」吗？

**答案**：本讲三份 notebook 恰好共用同一组超参数，所以可以粗略归因；但严格说 BN 常允许更大学习率，Dropout 常需要配合更长训练，最优超参并不相同。受控实验只保证「同超参下的对比」，跨方法的公平调参是更严格的实验设计。

## 5. 综合实践

**任务：扫描 Dropout 强度，找到本设置下的较优值。**

`dropout_prob = 0.5` 只是作者的默认选择。请对 `dropout_prob ∈ {0.3, 0.5, 0.7}` 各训练一次，绘制三条「epoch → 训练准确率」与「epoch → 测试准确率」曲线，回答：哪个值缺口最小？哪个值测试准确率最高？并解释原因。

**操作步骤**：

1. 最直接的方式：复制 [mlp-dropout.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-dropout.ipynb) 三份，只改第 109 行的 `dropout_prob`，其余（seed、lr、epochs、batch size）保持不动，分别完整运行。
2. 更工程化的方式：把 notebook 的模型与训练代码抽成一个函数，循环三组超参。可参考以下**示例代码**框架（本仓库没有现成的扫描脚本，需自己组织）：

```python
# 示例代码：dropout 强度扫描框架（伪代码级，细节请对照 notebook 补全）
results = {}
for p in [0.3, 0.5, 0.7]:
    dropout_prob = p                       # notebook 中 forward 引用的全局变量
    torch.manual_seed(random_seed)         # 每次重建模型前重置种子，保证公平
    model = MultilayerPerceptron(num_features, num_classes).to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)
    train_acc_hist, test_acc_hist = [], []
    for epoch in range(num_epochs):
        model.train()
        for features, targets in train_loader:
            # ……五步训练循环，与 notebook L448-L470 相同
            pass
        train_acc_hist.append(compute_accuracy(model, train_loader).item())
        test_acc_hist.append(compute_accuracy(model, test_loader).item())
        model.train()                      # 注意：compute_accuracy 内部切到 eval，需切回
    results[p] = (train_acc_hist, test_acc_hist)
# 用 matplotlib 画出 3 组曲线（可参考 helper_plotting.py 的绘图风格）
```

3. 两个易错点：一是 `compute_accuracy` 内部调用了 `net.eval()`，若在它之后继续训练，务必记得下一个 epoch 循环开头的 `model.train()`（notebook 已写好，自己抽函数时容易漏）；二是每组实验都要在**建模型之前**重置 `torch.manual_seed`，否则三组连初始权重都不在同一起跑线上。

**需要观察与预期的结果**（趋势性预期，具体数值**待本地验证**）：

- `p=0.3`：训练准确率最高、缺口较大，测试准确率预计与基线接近；
- `p=0.5`：训练准确率明显受抑、缺口最小（作者保存结果：97.52 / 96.71）；
- `p=0.7`：训练准确率进一步下降，10 个 epoch 内可能欠拟合，测试准确率可能不升反降。
- 综合判断：在 MNIST + 本容量 + 10 epoch 的设置下，较优值大概率是 0.3 或 0.5——正则化强度应与基线过拟合程度（缺口 1.46）匹配，而不是越大越好。

## 6. 本讲小结

- **Dropout**：训练时以概率 \( p \) 置零隐藏单元并将存活单元放大 \( 1/(1-p) \)（inverted dropout），评估时恒等通过；函数式 `F.dropout(..., training=self.training)` 必须显式接上模块的 `self.training` 标志。
- **BatchNorm1d**：训练时用当前 batch 统计归一化并同步更新 running stats，评估时改用 running stats；参数是通道数（Linear 输出维度），γ/β 经 `model.parameters()` 自动交给优化器，经典位置在 Linear 之后、激活之前。
- **模式切换是行为开关**：`model.train()` / `model.eval()` 翻转 `self.training`，Dropout 与 BatchNorm 是行为随之改变的两类标准层；评估函数中的 `net.eval()` 与 `torch.no_grad()` 各管一件事，缺一不可。
- **对照实验结果**：与基线（train 99.00 / test 97.54）相比，Dropout 把缺口从 1.46 压到 0.81 但测试准确率降至 96.71；BatchNorm 收敛更快（epoch 1 即 95.63%）且测试准确率最高（97.82%）。
- **方法论**：正则化不是越多越好，收益取决于基线过拟合程度；三份仅差几行、其余配置完全相同的 notebook 构成可归因的受控实验，这种「克隆基线、只改一处」的对比方式值得在自己项目中沿用。

## 7. 下一步学习建议

- **下一讲（u2-l5）**：从零实现反向传播（`mlp-fromscratch__sigmoid-mse.ipynb`）。本讲两个正则化层在反向传播中如何求导（Dropout 的 mask 直通梯度、BatchNorm 的批内耦合梯度）将在那里获得更底层的视角。
- **回头深读公共工具**：[pytorch_ipynb/helper_train.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py) 的 `train_classifier_simple_v1/v2` 把本讲的「train/eval 切换 + 逐 epoch 日志」工程化，可对照本讲手写循环阅读（u1-l3 已导览过其结构）。
- **正则化的后续延伸**：u7-l3 训练技巧（循环学习率、梯度裁剪）与 u8-l3 数据增强（AutoAugment / TrivialAugment）展示了从「模型侧」与「数据侧」抑制过拟合的两条路线；届时可回头检验本讲「正则化强度要匹配过拟合程度」的结论。
