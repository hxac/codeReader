# 框架对照与收官：TensorFlow 1.x 版本

## 1. 本讲目标

本讲是整套学习手册的最后一讲。我们把视角拉高，精读 `tensorflow1_ipynb/` 目录下的 notebook，与前面 30 余讲反复打磨的 PyTorch 实现逐点对照。学完后你应该能够：

1. 理解 TensorFlow 1.x 的「计算图 + 会话」（graph + session）范式：先定义静态符号图，再开会话喂数据执行。
2. 能把任何一份 TF1 notebook 的「建图三件套」（placeholder / Variable / ops）与 PyTorch 的「张量 / nn.Parameter / 训练循环」互相对译。
3. 说清两套框架在训练循环、梯度计算、变量管理、checkpoint、数据布局（NHWC vs NCHW）上的系统性差异。
4. 回顾 U1–U8 整套学习路径，规划自己后续的深度学习方向。

为什么要学一个「已经过时」的框架？三个理由：其一，大量经典论文代码、公司遗留系统仍是 TF1 风格；其二，TF1 把「符号微分」和「静态图」暴露得非常直白，反而能帮你看清 PyTorch autograd 到底替你做了什么；其三，对照学习是检验理解深度的最好方式——如果你能把 TF1 代码翻译成 PyTorch，说明你真正理解了两者。

## 2. 前置知识

本讲默认你已读完（或熟悉对应内容）：

- **u2-l3（MLP 与 nn.Module）**：PyTorch 自定义模型的标准写法——`__init__` 建层、`forward` 描述前向、`model(x)` 触发执行。
- **u3-l1（第一个 CNN）**：NCHW 布局、卷积输出尺寸公式 \( o = \lfloor (w - k + 2p)/s \rfloor + 1 \)。
- **u5-l1（自编码器基础）**：784→32→784 沙漏结构、逐像素 BCE 重建损失。
- **u2-l5（从零实现反向传播）**：反向传播四要素与逐层梯度公式——本讲会在 TF1 里再次见到同一组公式。

再用通俗语言补三个 TF1 核心概念：

**静态图（static graph）**。TF1 中，你写的 Python 代码并不直接计算任何东西，它只是在「图纸」上画节点：`tf.matmul(a, b)` 返回的不是乘积，而是一个「未来要做矩阵乘法」的符号节点。整张图搭好后，数据只在 `sess.run()` 被调用时才真正流动。这就像先写好菜谱（建图），再按菜谱做菜（执行）。PyTorch 则是 define-by-run：`a @ b` 这行代码立即算出结果，同时默默记录计算历史供 backward 使用——做菜的同时写菜谱。

**会话（session）**。`tf.Session` 是图的「执行引擎」：它持有参数的实际内存，接受 `feed_dict` 喂进来的真实数据，沿图计算你 `fetch` 的那些节点。参数初始化也必须由会话显式驱动（`sess.run(tf.global_variables_initializer())`）——在 TF1 里，`tf.Variable(...)` 只是声明，不初始化就 run 会直接报错。

**placeholder**。图是静态的，数据却是每次不同的。`tf.placeholder` 就是图上的「数据插槽」：建图时只声明形状（第一维 `None` 表示任意 batch 大小），执行时用 `feed_dict` 把 numpy 数组插进去。它对应 PyTorch 里「函数入参的张量」——只不过 PyTorch 根本不需要这个机制，张量本来就是即时数据。

版本说明：这几份 notebook 写于 TF 1.1–1.12 之间（各 notebook 的 `%watermark` cell 可查：perceptron 为 1.2.0，mlp-basic 为 1.1.0，cnn-basic 为 1.12.0）。`tf.contrib` 与 `tensorflow.examples.tutorials.mnist` 在 TF 2.x 中已彻底移除，所以这些 notebook **无法在现代环境直接运行**——本讲的实践以「源码阅读 + 翻译成 PyTorch」为主，这恰恰是最好的收官练习。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [tensorflow1_ipynb/basic-ml/perceptron.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/basic-ml/perceptron.ipynb) | TF1 版感知机：最纯的「图范式」标本——连逐样本更新规则都写进图里 |
| [tensorflow1_ipynb/mlp/mlp-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-basic.ipynb) | 同一 MLP 的两种写法：字典管理参数的低层版 vs `tf.layers.dense` 抽象版 |
| [tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb) | 三档梯度实现：`GradientDescentOptimizer` → `tf.gradients` + 手写更新 → 完全手写反向传播 |
| [tensorflow1_ipynb/cnn/cnn-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/cnn/cnn-basic.ipynb) | TF1 版 CNN：手写 `conv2d`/`fully_connected` 包装函数 + NHWC 布局 + placeholder 控制 Dropout |
| [tensorflow1_ipynb/autoencoder/ae-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/autoencoder/ae-basic.ipynb) | TF1 版自编码器：无监督 feed_dict、`tf.train.Saver` 保存与恢复 |
| [pytorch_ipynb/mlp/mlp-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb) | PyTorch 对照版 MLP（u2-l3 精读过） |
| [pytorch_ipynb/cnn/cnn-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb) | PyTorch 对照版 CNN（u3-l1 精读过） |
| [pytorch_ipynb/autoencoder/ae-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb) | PyTorch 对照版自编码器（u5-l1 精读过） |

> 提示：`.ipynb` 文件的永久链接行号指向 JSON 源文件的行。在 GitHub 上打开链接后，可按行号定位到对应 cell 的 `source` 数组。

## 4. 核心概念与源码讲解

### 4.1 TF1 计算图与会话范式

#### 4.1.1 概念说明

TF1 的编程模型是 **define-then-run**（先定义后执行），一切代码围绕三个角色展开：

| 角色 | 职责 | 生命周期 |
|---|---|---|
| `tf.placeholder` | 图上的数据插槽，声明形状不装数据 | 建图时声明，`sess.run` 时用 `feed_dict` 喂数据 |
| `tf.Variable` | 图上的可变状态，即模型参数 | 声明后需 `global_variables_initializer` 显式初始化 |
| op（算子节点） | `tf.matmul`、`tf.add`、`tf.assign_add`…… | 建图时连接成网，执行时才计算 |

而 PyTorch 是 **define-by-run**：Python 代码即执行代码，张量运算是即时求值的，「图」只是 backward 时回溯的计算历史。理解这对范式的最佳标本就是 TF1 版感知机——它是全仓库唯一一份「连参数更新规则都画进图里」的 notebook，与 u2-l1 的 PyTorch 版（命令式逐样本更新）形成了教科书级的对照。

#### 4.1.2 核心流程

TF1 notebook 的通用骨架是三段式：

```text
[第一段] 建图（纯符号，无任何数据流动）
    g = tf.Graph()
    with g.as_default():
        features = tf.placeholder(...)          # 数据插槽
        weights = tf.Variable(tf.zeros(...))    # 参数声明
        linear  = tf.matmul(features, weights)  # 符号节点
        train   = tf.group(更新op1, 更新op2)     # 把更新打包成一个 op
                 ↓
[第二段] 开会话并初始化
    with tf.Session(graph=g) as sess:
        sess.run(tf.global_variables_initializer())   # 此刻才真正分配参数内存
                 ↓
[第三段] 喂数据执行
        sess.run(['train'], feed_dict={'features:0': x, 'targets:0': y})
        # fetch 列表里的每个名字在本次 run 中被求值；
        # 为计算它们所依赖的所有上游节点也会一并执行
```

关键机制是 **fetch by name**：`sess.run` 既接受图中的张量名（如 `'cost:0'`，`:0` 表示该 op 的第 0 个输出），也接受 op 名（如 `'train'`）。一次 `run` 会执行 fetch 目标所依赖的整条子图——这就是为什么「训练 op」可以被打包成一个节点。

感知机的逐样本更新规则（与 u2-l1 的 PyTorch 版数学完全相同）：

\[ w \leftarrow w + (y - \hat{y})\,x, \qquad b \leftarrow b + (y - \hat{y}) \]

在 TF1 里，这个规则不是 Python 语句，而是图上的 `tf.assign_add` 节点。

#### 4.1.3 源码精读

**① 建图三件套**（[tensorflow1_ipynb/basic-ml/perceptron.ipynb:L165-L174](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/basic-ml/perceptron.ipynb#L165-L174)）：声明两个 placeholder（形状 `[None, n_features]` 与 `[None, 1]`，`None` 表示 batch 维任意），再用 `tf.Variable` 声明权重与偏置并初始化为零。注意：到这里为止**没有任何数值被计算**——这两行在 PyTorch 中对应 `nn.Linear(n_features, 1)` 里自动登记的 `nn.Parameter`。

**② 前向传播是符号节点**（[tensorflow1_ipynb/basic-ml/perceptron.ipynb:L178-L184](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/basic-ml/perceptron.ipynb#L178-L184)）：`linear` 是「未来的」打分节点，`prediction` 用 `tf.where(tf.less(linear, 0.), zeros, ones)` 实现阶跃函数——对应 PyTorch 版里的 `torch.where(z >= 0., 1., 0.)`。再次强调，这些只是图纸。

**③ 更新规则写进图里**（[tensorflow1_ipynb/basic-ml/perceptron.ipynb:L186-L194](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/basic-ml/perceptron.ipynb#L186-L194)）：这是全讲最值得咀嚼的一段——

```python
errors = targets - prediction
weight_update = tf.assign_add(weights,
                              tf.reshape(errors * features, (n_features, 1)))
bias_update = tf.assign_add(bias, errors)
train = tf.group(weight_update, bias_update, name='train')
```

`errors * features` 就是 \((y-\hat{y})x\)；`tf.assign_add` 是图上的「原地加法」op；`tf.group` 把两个更新打包成一个名叫 `train` 的 op。对照 PyTorch 版感知机（u2-l1）：那边是普通 Python 语句 `w = w + (y - ŷ) * x`，每执行一行立即生效。**同一数学，两种执行模型**。

**④ 会话执行**（[tensorflow1_ipynb/basic-ml/perceptron.ipynb:L204-L220](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/basic-ml/perceptron.ipynb#L204-L220)）：`with tf.Session(graph=g)` 打开引擎，先 `sess.run(tf.global_variables_initializer())` 让 Variable 真正获得内存与初值；随后双层循环里逐样本构造 `feed_dict`（键是 `'features:0'` 这种张量名），`sess.run(['train'], ...)` 驱动整条更新链。收尾时用名字取回参数（`sess.run(['weights:0', 'bias:0'])`），并用 `tf.train.Saver` 保存（详见 4.3 的 Saver 讨论）。最终训练误差为 0（该 toy 数据线性可分），与 u2-l1 PyTorch 版结论一致。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，纯靠「读图」回答问题，训练「静态图思维」。

1. 打开 [tensorflow1_ipynb/basic-ml/perceptron.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/basic-ml/perceptron.ipynb)，对照上面 ①–④ 四段链接，在纸上画出节点依赖图：`features → linear → prediction → errors → weight_update → train`，`weights → linear` 与 `weights → weight_update` 两条边都要画。
2. 回答：`sess.run(['train'], feed_dict=...)` 执行时，为什么 `prediction`、`errors` 会被顺带计算，而画图阶段的 `plt.scatter` 却与图无关？
3. 再回答：如果把 `feed_dict` 里的键换成 `features`（不带 `:0`）会发生什么？去查 TF1 文档确认。

**需要观察的现象**：你会发现自己能仅凭代码推导出「一次 run 的执行范围 = fetch 目标的全部上游依赖」。

**预期结果**：依赖图中 `train` op 可达的所有节点构成执行子图；matplotlib 代码是普通 Python，根本不在图内。第 3 问的结论「待本地验证」（需 TF 1.x 环境；文档层面 `sess.run` 的 fetch/feed 既接受张量对象也接受字符串名，`:0` 后缀用于消歧同名 op 的多个输出）。

#### 4.1.5 小练习与答案

**练习 1**：TF1 版感知机 notebook 中，感知机学习率是多少？在代码里能找到吗？

**答案**：找不到显式的 learning rate——更新规则 `w ← w + (y−ŷ)x` 隐含学习率 1，与 u2-l1 讲过的 Rosenblatt 感知机原始定义一致。这是数学层面的巧合一致，与框架无关。

**练习 2**：`tf.placeholder(shape=[None, n_features])` 中的 `None` 有什么作用？

**答案**：让 batch 维度可变——同一个图既能喂 1 个样本（感知机逐样本更新），也能喂整个测试集（`sess.run('prediction:0', feed_dict={features: X_train})`）。PyTorch 不需要这个机制，因为每次前向 `model(x)` 都是对当前真实 batch 的即时计算，形状随传入张量而定。

**练习 3**：为什么 TF1 必须调用 `sess.run(tf.global_variables_initializer())`，而 PyTorch 的 `nn.Linear` 建好就能用？

**答案**：TF1 中 `tf.Variable(...)` 只是图上的声明，参数内存由 Session 分配，初始化是图上的一次显式执行；PyTorch 的 `nn.Linear` 在 `__init__` 时立即在内存中创建张量并完成初始化（u2-l3 讲过），根本不存在「声明/执行」二分。

### 4.2 低层 API 对照：MLP 的三档梯度实现

#### 4.2.1 概念说明

PyTorch 只有一档梯度机制：`loss.backward()` 自动微分，覆盖一切可导运算。TF1 却像一部「梯度实现进化史」——`mlp-lowlevel.ipynb` 在同一份 notebook 里并排放了三档实现，讲同一个 784→128→10 的 sigmoid 隐层 MLP：

| 档位 | 梯度来源 | 参数更新 | PyTorch 中的对应物 |
|---|---|---|---|
| 1. `tf.train.GradientDescentOptimizer` | `optimizer.minimize(cost)` 一行全包 | 同一行 | `loss.backward()` + `optimizer.step()` |
| 2. `tf.gradients` | 手动调用符号微分算子 | 手写 `tf.assign` 更新节点 | `torch.autograd.grad(cost, params)` + 手动更新 |
| 3. 手写反向传播公式 | 自己推导 \(\delta\) 与 \(\nabla_W\)，用 `tf.matmul` 等基本算子拼出 | 同上 | u2-l5 的纯 tensor 手工反向传播 |

三档的微妙之处：**即便第 3 档「手写」，梯度公式依然是静态图节点**——你写的是「如何计算梯度」的图纸，执行仍由 session 完成。而 u2-l5 的 PyTorch 手工版是命令式的：每行公式立即算出真实数值。数学完全同构，执行模型相反。

三档的最终 Test ACC 全部是 **0.945**（同一网络、同一数据、同一配方），这是「梯度就是梯度，与谁算的无关」的最有力证据。

#### 4.2.2 核心流程

第 3 档手写反向传播的公式（与 u2-l5 的四要素逐条对应，softmax+交叉熵输出层、sigmoid 隐层、对 batch 取平均）：

\[ \delta^{out} = \frac{p - y_{\text{onehot}}}{N} \qquad \text{（输出层误差，无饱和因子）} \]

\[ \delta^{h} = \left(\delta^{out} W_{out}^{\top}\right) \odot a_h\left(1 - a_h\right) \qquad \text{（上游梯度} \times \text{sigmoid 局部导数）} \]

\[ \nabla_{W_h} = X^{\top}\delta^{h}, \qquad \nabla_{b_h} = \mathbf{1}^{\top}\delta^{h}, \qquad \nabla_{W_{out}} = a_h^{\top}\delta^{out}, \qquad \nabla_{b_{out}} = \mathbf{1}^{\top}\delta^{out} \]

其中 \(p\) 是 softmax 概率、\(a_h\) 是隐层激活、\(N\) 是 batch_size。注意 \(\delta^{out}\) 提前除以 \(N\)，与 `tf.reduce_mean` 的量纲对齐。

#### 4.2.3 源码精读

**① 第 1 档：一行 optimizer**（[tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb:L159-L176](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb#L159-L176)）：前向传播四行符号节点（`h1_z → h1_act → out_z → out_act/out_labels`），随后 `loss = tf.nn.softmax_cross_entropy_with_logits(...)`、`cost = tf.reduce_mean(loss)`，最后 `train = optimizer.minimize(cost, name='train')`——TF1 的 minimize 把「求梯度 + 应用更新」两个 op 打包进一个名字。对照 PyTorch：这对应五步范式里的 `backward()` 与 `step()` 两步，且 PyTorch 还多一步 `zero_grad()`（TF1 的梯度不在图外累积，无需清零；PyTorch 的梯度默认跨批累加，u2-l2 讲过）。

**② 第 2 档：符号微分 + 手写更新**（[tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb:L326-L335](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb#L326-L335)）：

```python
dc_dw_out, dc_db_out = tf.gradients(cost, [weights['out'], biases['out']])
dc_dw_1,  dc_db_1   = tf.gradients(cost, [weights['h1'], biases['b1']])

upd_w_1 = tf.assign(weights['h1'], weights['h1'] - learning_rate * dc_dw_1)
...
train = tf.group(upd_w_1, upd_b_1, upd_w_out, upd_b_out, name='train')
```

`tf.gradients(cost, vars)` 就是「对 cost 关于给定变量求导」的符号算子——它返回的是图上的梯度**节点**而非数值。这一档直接对应 PyTorch 的 `torch.autograd.grad(cost, params)`（u4-l5 讲过），更新公式 \( \theta \leftarrow \theta - \eta \nabla_\theta C \) 则由 `tf.assign` 显式写成节点。第 1、2 档的前向传播、损失、训练循环逐字相同，Test ACC 同为 0.945——「手写更新」没有改变任何数学。

**③ 第 3 档：手写反向传播公式**（[tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb:L487-L513](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb#L487-L513)）：上节四条公式逐一落地——`sigma_out = (out_act - tf_y) / batch_size`、`sigma_h = tf.matmul(sigma_out, tf.transpose(weights['out'])) * sigmoid导数`、`grad_w_h1 = tf.matmul(tf.transpose(tf_x), sigma_h)`、`grad_w_out = tf.matmul(tf.transpose(h1_act), sigma_out)`，偏置梯度 `tf.reduce_sum(sigma, axis=0)` 按样本维求和。代码注释里逐行标注了每个矩阵的输入输出维度——这份注释本身就是理解「反向传播是矩阵微积分」的最好教材。把它与 u2-l5 的 PyTorch 手工版并排读：公式一字不差，只是 TF1 版在图上、PyTorch 版在命令式代码里。

**④ `tf.layers.dense`：走向层抽象**（[tensorflow1_ipynb/mlp/mlp-basic.ipynb:L249-L253](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-basic.ipynb#L249-L253)）：同一 notebook 的后半段把字典管理的参数（对照 [L116-L127](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-basic.ipynb#L116-L127) 的 `weights = {'h1': tf.Variable(...), ...}`）换成 `tf.layers.dense(tf_x, n_hidden_1, activation=tf.nn.relu, kernel_initializer=...)`——权重、偏置、初始化全部封进层内部。这正是 PyTorch `nn.Linear` 的位置：u2-l3 讲过，`nn.Linear(num_features, num_hidden_1)` 在 `__init__` 时创建并登记参数。两种写法最终 Test ACC 都是 0.975。

**⑤ 训练循环对照**（[tensorflow1_ipynb/mlp/mlp-basic.ipynb:L149-L176](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-basic.ipynb#L149-L176) vs [pytorch_ipynb/mlp/mlp-basic.ipynb:L441-L459](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L441-L459)）：两份 notebook 的 epoch×batch 双层循环结构完全同构，核心差异只有一处——TF1 内层是 `_, c = sess.run(['train', 'cost:0'], feed_dict={...})`（一次 run 同时拿到更新与损失），PyTorch 内层是五步范式「forward → loss → zero_grad → backward → step」。TF1 没有显式 `zero_grad`，因为没有「跨批梯度累积」这回事。

#### 4.2.4 代码实践

**实践目标**：验证「三档实现的梯度数值一致」这一论断的可验证版本——用 PyTorch 复现 TF1 第 2、3 档的对等物。

1. 用 PyTorch 写一个 `784→128→10` 的 sigmoid 隐层 MLP（可直接仿照 u2-l3 的 `MultilayerPerceptron`，把激活换成 sigmoid、结构改成单隐层）。
2. 取一个 batch（64 个样本），分别用三种方式求 \(\nabla_{W_{out}}\)：
   - 方式 A：`loss.backward()` 后读 `model.linear_out.weight.grad`；
   - 方式 B：`torch.autograd.grad(cost, model.linear_out.weight)`；
   - 方式 C：照抄 TF1 第 3 档公式（4.2.2 节），用 `torch.matmul` 手工算 `grad_w_out = h1_act.T @ sigma_out`（注意 PyTorch 版要不要除 batch_size，与 `F.cross_entropy` 的默认 mean 归约对齐）。
3. 断言三者 `torch.allclose`（容差 1e-5 量级）。

**需要观察的现象**：三种方式的梯度张量完全一致（或仅差一个量纲常数——若方式 C 忘记除 N）。

**预期结果**：一致；若不一致，检查归约方式（mean vs sum）。TF1 侧的运行对照「待本地验证」（需 TF 1.x 环境）。

#### 4.2.5 小练习与答案

**练习 1**：`mlp-lowlevel.ipynb` 第 3 档中 `sigma_out = (out_act - tf_y) / batch_size`，为什么 `grad_w_out = tf.matmul(tf.transpose(h1_act), sigma_out)` 之后不再除以 `batch_size`？

**答案**：`tf.reduce_mean` 的语义是先对 batch 求和再除以 N；把 \(1/N\) 提前折进 \(\delta^{out}\)，则权重梯度 \(\nabla_{W_{out}} = a_h^{\top}\delta^{out}\) 的量纲自动正确。这是「除法提前」的代数等价，u2-l5 的 PyTorch 手工版也用了同样的技巧。

**练习 2**：TF1 的 `optimizer.minimize(cost)` 把哪些 PyTorch 步骤打包了？哪个 PyTorch 步骤在 TF1 中根本不存在？

**答案**：打包了 `backward()`（求梯度）与 `step()`（应用更新）；`zero_grad()` 在 TF1 中不存在——TF1 的梯度是图内部一次性计算的中间结果，不挂在参数上跨批累积，所以无需清零。

**练习 3**：为什么 `mlp-basic.ipynb` 里标签要用 `one_hot=True` 读入？

**答案**：因为 `tf.nn.softmax_cross_entropy_with_logits(logits, labels)` 的 labels 参数吃 one-hot 浮点矩阵 `[N, 10]`。PyTorch 的 `F.cross_entropy(logits, targets)` 吃类别索引整数向量 `[N]`（u2-l2 讲过内部做 log_softmax）。这是两套框架损失 API 的系统性差异，概念映射表（4.4 节）会再次收录。

### 4.3 卷积与自编码器的 TF1 写法：wrapper、布局与 Saver

#### 4.3.1 概念说明

TF1 没有 `nn.Module` 这样「参数随层走、层随模型走」的容器，于是产生了一种典型的时代产物——**手写 wrapper 函数**：函数内部创建 `tf.Variable`、执行运算、返回激活输出。它的作用相当于一个「穷人版 nn.Linear / nn.Conv2d」。TF1 版 CNN notebook 把这个模式写到了极致；TF1 版自编码器 notebook 则补充了两个新机制：

- **placeholder 控制运行模式**：训练时 `keep_proba=0.5`、评估时 `keep_proba=1.0`——用数据插槽模拟「模式切换」，对应 PyTorch 的 `model.train()` / `model.eval()`（u2-l4）。
- **`tf.train.Saver`**：把 Session 中所有 Variable 存盘 / 恢复，对应 PyTorch 的 `torch.save(model.state_dict())` / `load_state_dict()`（u4-l4、u8-l1 讲过 checkpoint 逻辑）。

此外还有一处贯穿所有 TF1 卷积代码的差异：**数据布局**。TF1 默认 NHWC（batch, 高, 宽, 通道），PyTorch 是 NCHW。u3-l1 背过的 NCHW 在这里要换算一次。

#### 4.3.2 核心流程

TF1 版 CNN 的建图流程：

```text
placeholder inputs [N, 784, 1] ──tf.reshape──> [N, 28, 28, 1]   # NHWC!
        │
        ├─ conv2d(8 核, 3×3, SAME, ReLU)  → [N, 28, 28, 8]      # SAME 填充保尺寸
        ├─ max_pool(2×2, stride 2)        → [N, 14, 14, 8]
        ├─ conv2d(16 核, 3×3, SAME, ReLU) → [N, 14, 14, 16]
        ├─ max_pool(2×2, stride 2)        → [N, 7, 7, 16]
        ├─ get_shape 推导 → tf.reshape 展平  → [N, 784]
        └─ fully_connected(10)            → logits [N, 10]
```

与 u3-l1 的 PyTorch 版 ConvNet 结构逐层相同，只是 u3-l1 版的形状是 `[N, 1, 28, 28] → [N, 8, 28, 28] → ...`。

#### 4.3.3 源码精读

**① 手写 `conv2d` wrapper**（[tensorflow1_ipynb/cnn/cnn-basic.ipynb:L120-L146](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/cnn/cnn-basic.ipynb#L120-L146)）：函数从 `input_tensor.get_shape().as_list()[-1]` 读输入通道数（静态图形状推理），据此构造 `[k, k, in_c, out_c]` 的卷积核 Variable——注意这个四维形状是 **TF 的 (高, 宽, 入通道, 出通道)**，而 PyTorch `nn.Conv2d` 的权重是 (出通道, 入通道, 高, 宽)。`tf.nn.conv2d(strides=(1,1,1,1), padding='SAME')` 中 strides 四元组首尾的 1 分别对应 batch 维与通道维；`SAME` 填充即 u3-l1 讲过的「同名填充」\(p=(k-1)/2\)（k=3 时 p=1，尺寸不变）。`fully_connected` wrapper（[L148-L173](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/cnn/cnn-basic.ipynb#L148-L173)）与自编码器 notebook 里的同名函数几乎逐字相同（见下）。

**② 建图：NHWC 与形状推理**（[tensorflow1_ipynb/cnn/cnn-basic.ipynb:L180-L226](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/cnn/cnn-basic.ipynb#L180-L226)）：第三个 placeholder `keep_proba`（L182）用于运行时控制 Dropout 模式；`tf.reshape(tf_x, [-1, 28, 28, 1])`（L186）把平铺像素恢复成 NHWC 图像；`pool2.get_shape().as_list()[1:]` 连乘（L216 附近）自动推得展平维度 7×7×16=784——静态图的形状推理在此发挥了「免手算」的作用，但也意味着形状错误往往要等到 run 时才暴露。损失用 `softmax_cross_entropy_with_logits_v2`（L222，1.12 版的新 API，仅反向传播行为与旧版有差异）。

**③ 喂图：给 batch 补通道维**（[tensorflow1_ipynb/cnn/cnn-basic.ipynb:L271-L297](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/cnn/cnn-basic.ipynb#L271-L297)）：训练循环与 4.2 的 MLP 版同构，只多了一行 `batch_x = batch_x[:, :, None]`（L281）——给 numpy batch 沿末尾补一个颜色通道以匹配 NHWC placeholder；评估时 `feed_dict` 里把 `keep_proba:0` 换成 1.0 关闭 Dropout。最终 3 个 epoch 就拿到 Test ACC 0.980，与 PyTorch 版（u3-l1）同一量级。

**④ 自编码器：无监督的 feed_dict 与 Saver**（[tensorflow1_ipynb/autoencoder/ae-basic.ipynb:L125-L162](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/autoencoder/ae-basic.ipynb#L125-L162)）：结构正是 u5-l1 精读过的 784→32→784 沙漏——encoder 是 `fully_connected(input_layer, 32, activation=tf.nn.relu, name='encoding')`（L138），decoder 是线性层接 `tf.nn.sigmoid`（L146-L149），重建损失 `tf.nn.sigmoid_cross_entropy_with_logits(labels=input_layer, logits=logits)`（L155）对应 PyTorch 的 `F.binary_cross_entropy_with_logits`。与分类模型唯一的不同：**图上没有 targets placeholder，feed_dict 只喂输入**（见 [训练循环 L204-L224](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/autoencoder/ae-basic.ipynb#L204-L224) 中 `feed_dict={'input:0': batch_x}`）——「目标是输入本身」这句自监督口号在 feed_dict 层面直接可见。

**⑤ 保存与恢复**（[tensorflow1_ipynb/autoencoder/ae-basic.ipynb:L264-L266](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/autoencoder/ae-basic.ipynb#L264-L266)）：训练后 `saver.save(sess, './autoencoder.ckpt')`（L224）；可视化 cell **新开一个 Session**，`saver.restore(sess, './autoencoder.ckpt')` 恢复全部变量后直接 `sess.run('decoding:0', ...)` 取重建图。这套「存 session 变量、开新 session 恢复」的流程，正对应 PyTorch 的 `torch.save(model.state_dict())` / `model.load_state_dict(torch.load(...))`；区别在于 Saver 按「图中的名字」对齐变量，state_dict 按「参数键名」对齐（u3-l5 的 MobileNet state_dict 键名案例是同一原理）。

#### 4.3.4 代码实践

**实践目标**：亲手完成 NHWC → NCHW 的布局翻译，体会「同一网络、两种布局」。

1. 打开 [tensorflow1_ipynb/cnn/cnn-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/cnn/cnn-basic.ipynb) 与 [pytorch_ipynb/cnn/cnn-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb)，做一张逐层形状对照表：TF1 侧按 NHWC 写出 conv1/pool1/conv2/pool2 后的形状，PyTorch 侧按 NCHW 写出（模型定义见 [pytorch_ipynb/cnn/cnn-basic.ipynb:L163-L205](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L163-L205)）。
2. 写一个三行 numpy 片段（示例代码）：`x_nhwc = np.random.rand(2, 28, 28, 8)`，用 `x_nhwc.transpose(3, 0, 1, 2)` 得到 NCHWC 布局的 `[2, 8, 28, 28]`，验证 `x_nhwc.transpose(3, 0, 1, 2).shape == (2, 8, 28, 28)`。
3. 回答：TF1 侧 `batch_x[:, :, None]` 与 PyTorch 侧 `features.view(-1, 1, 28, 28)` 各自解决什么问题？

**需要观察的现象**：两张形状表数字相同、维度顺序不同（通道在最后 vs 通道在第二）。

**预期结果**：形状对照成立。第 3 问答案：前者给 NHWC 的平铺像素补末尾通道维，后者把 NCHW 的平铺像素重组为 (N, C=1, 28, 28)——两者都是在「数据布局约定」与「卷积 API 期望」之间做转换。

#### 4.3.5 小练习与答案

**练习 1**：TF1 用 `keep_proba` placeholder 控制 Dropout，PyTorch 用 `model.train()/eval()`。为什么 PyTorch 不用 placeholder 方案？

**答案**：PyTorch 的 Dropout 是模块，行为由模块自身的 `self.training` 标志决定，`train()/eval()` 批量翻转该标志（u2-l4）；且 PyTorch 前向是即时执行，同一模型可以随时切换模式。TF1 的图是静态的，Dropout 行为要靠 feed_dict 喂不同 keep_prob 值来「参数化」，本质上是用数据通道传输控制信号。

**练习 2**：TF1 版 CNN 的 wrapper 函数与 PyTorch 的 `nn.Module` 相比，缺了什么？

**答案**：缺「参数与层的绑定」。wrapper 创建的 Variable 挂在全局图上，模型没有 `model.parameters()` 这样的统一入口，保存/恢复/遍历参数都要靠图级机制（Saver、collection）；`nn.Module` 让层拥有参数、模型拥有层，`parameters()` 遍历、`state_dict()` 序列化、`to(device)` 搬运（u1-l2）都顺着这棵对象树自然发生。

**练习 3**：ae-basic notebook 的可视化 cell 为什么可以直接 `sess.run('decoding:0', ...)` 而不用重建计算图代码？

**答案**：因为 `saver.restore` 恢复的是**变量值**，而计算图 `g` 是上一个 cell 在同一个 Python 进程里建好的、仍然存活的对象；新 Session 挂到这张图上，恢复变量后即可按名字 run 任意节点。PyTorch 侧对应流程是「先实例化模型（重建结构）、再 `load_state_dict`（恢复数值）」——结构定义与数值加载在两边都是分离的。

### 4.4 框架概念映射与学习路径收官

#### 4.4.1 概念说明

前三个模块逐点对照了具体 notebook；这一模块把它们收敛成一张**概念映射表**——这是本讲的最终交付物，也是你日后阅读任何 TF1 遗留代码的「翻译词典」。核心洞察只有一条：两套框架实现的是同一数学（前向计算 + 链式法则 + 梯度下降），差异全部在于「何时建图、谁来执行、参数放哪」。

#### 4.4.2 核心流程

两套训练循环的并排伪代码：

```text
┌─ TF1（define-then-run）────────┐   ┌─ PyTorch（define-by-run）────────┐
│ 建图: placeholder/Variable/ops  │   │ 建模: class MyNet(nn.Module)      │
│ train = optimizer.minimize(cost)│   │ optimizer = SGD(model.parameters)│
│                                 │   │                                   │
│ with tf.Session(graph=g):       │   │ for epoch:                        │
│   sess.run(global_variables_    │   │   for x, y in train_loader:       │
│                initializer())   │   │     logits = model(x)      # 前向│
│   for epoch:                    │   │     loss = F.cross_entropy(...)   │
│     for batch:                  │   │     optimizer.zero_grad()  # 清零│
│       sess.run(['train','cost'],│   │     loss.backward()   # 自动微分 │
│         feed_dict={'features:0' │   │     optimizer.step()    # 更新   │
│                    : batch_x,   │   │                                   │
│                    'targets:0'  │   │                                   │
│                    : batch_y})  │   │                                   │
└─────────────────────────────────┘   └───────────────────────────────────┘
```

#### 4.4.3 源码精读

**训练循环五步范式对照**（[pytorch_ipynb/mlp/mlp-basic.ipynb:L441-L459](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L441-L459)）：这段 u2-l3 精读过的代码（`logits, probas = model(features)` → `cost = F.cross_entropy(...)` → `zero_grad` → `backward` → `step`）现在再读一遍，你会发现 TF1 把其中三步（backward、step、以及隐含的梯度计算）都折叠进了 `sess.run(['train', ...], feed_dict=...)` 的一次调用里——**五步范式是 PyTorch 对 TF1 一次 run 的展开**。u8-l1 又讲过 Lightning 把五步重新折叠进 `training_step` + `Trainer.fit`——三个抽象层级，同一数学。

**PyTorch 与 TF1 概念映射总表**（依据本讲五个 TF1 notebook 与对应 PyTorch 版本归纳）：

| TF1 | PyTorch | 备注 |
|---|---|---|
| `tf.placeholder` | 函数入参张量 | 数据入口；TF1 靠 `feed_dict` 喂数据 |
| `feed_dict` | `DataLoader` 迭代 + `.to(device)` | TF1 无 DataLoader，靠 `mnist.train.next_batch` |
| `tf.Variable` | `nn.Parameter`（由 `nn.Module` 登记） | TF1 挂在图上，PyTorch 挂在模块树上 |
| `g = tf.Graph()` + `g.as_default()` | （无显式图） | 静态图 vs 动态建图 |
| `tf.Session` + `sess.run` | 直接调用 / 训练循环 | 执行引擎；PyTorch 即时执行 |
| `global_variables_initializer` | 模块 `__init__` 时即初始化 | 声明与执行的二分 vs 合一 |
| `optimizer.minimize(cost)` | `zero_grad` + `backward` + `step` | TF1 无需清零梯度 |
| `tf.gradients(cost, vars)` | `torch.autograd.grad(...)` | 手动取梯度（u4-l5） |
| `tf.assign` / `tf.assign_add` | `with torch.no_grad(): p -= ...` | 手写参数更新节点 vs 手写更新语句 |
| `tf.layers.dense` | `nn.Linear` | 层抽象；对应 mlp-basic 两段实现 |
| 手写 `conv2d`/`fully_connected` wrapper | `nn.Conv2d` / `nn.Module` 子类 | 无容器时代的工作around |
| `tf.train.Saver` save/restore | `torch.save(state_dict)` / `load_state_dict` | 按图中名字 vs 按参数键名对齐 |
| `'cost:0'`、`'train'` 按名 fetch | 变量直接引用 | 静态图名字寻址 |
| `keep_proba` placeholder | `model.train()` / `model.eval()` | 模式切换：数据通道 vs 标志位 |
| NHWC 布局 | NCHW 布局 | `transpose(3,0,1,2)` 互转 |
| one-hot 标签 + `softmax_cross_entropy_with_logits` | 类别索引 + `F.cross_entropy` | 都要求喂 logits（u2-l2） |
| `tf.set_random_seed` | `set_all_seeds`（Python/NumPy/torch 三源，u1-l3） | TF1 图级种子 vs 全随机源 |
| `input_data.read_data_sets`（contrib，已废弃） | `torchvision.datasets` + `DataLoader` | TF2 已移除 contrib |

#### 4.4.4 代码实践

**实践目标**：把概念映射表从「读到的」变成「用过的」——用一次真实翻译内化它（详细任务见第 5 节综合实践，此处做热身）。

1. 从 [tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-lowlevel.ipynb) 第 2 段（L326-L335）抄下 `tf.gradients + tf.assign` 的 10 行代码。
2. 逐行加中文注释：左侧写 TF1 语义，右侧写它映射到 PyTorch 的哪一步（`autograd.grad`？`no_grad` 原地更新？`zero_grad` 有没有对应物？）。
3. 用上表查出这 10 行涉及的每一条映射，标出你认为「最容易翻译错」的一条。

**需要观察的现象**：你会发现唯一没有对应物的是「梯度清零」，以及「更新发生在图节点里 vs 发生在 Python 语句里」这一语法差异。

**预期结果**：得到一份带双向注释的 10 行代码，与第 5 节综合实践直接衔接。

#### 4.4.5 小练习与答案

**练习 1**：用一句话向只学过 PyTorch 的同学解释：为什么 TF1 代码里到处是 `with g.as_default():`，而 PyTorch 代码里一个都看不到？

**答案**：TF1 需要先把所有节点画进一张显式的静态图（并指明画进哪张图），执行在之后的 Session 里；PyTorch 的每条张量语句就是即时计算，不需要「当前往哪张图上画」这个上下文。

**练习 2**：u8-l1 讲过 Lightning 把训练循环收进 `Trainer.fit`。TF1 的 `sess.run(['train'], feed_dict=...)` 与 Lightning 的 `trainer.fit(model, datamodule)` 有什么深层的相似之处？

**答案**：两者都是「把多步训练折叠成一个入口」——TF1 靠静态图把 backward+step 折叠成 train op，Lightning 靠框架代码把 epoch/batch 循环折叠进 Trainer。方向相反（静态图前折 vs 框架后包），但都体现了「让用户少写循环」的抽象演进。

**练习 3**：如果让你把 TF1 版 CNN 的 `conv2d` wrapper（4.3 ①）翻译成 PyTorch，函数签名会变成什么样？

**答案**：不再是「输入张量 → 输出激活」的函数，而是一个 `nn.Module` 子类：`__init__(out_channels, kernel_size, activation)` 里建 `nn.Conv2d`，`forward(x)` 里做卷积加激活。权重形状从 TF 的 `(k, k, in_c, out_c)` 变为 PyTorch 的 `(out_c, in_c, k, k)`，且由 `nn.Conv2d` 自动创建与初始化。

## 5. 综合实践

**综合任务：把 `mlp-basic.ipynb` 低层版的计算图逻辑完整翻译成 PyTorch，并交付一张概念对照表。**这是整套手册的收官练习——它逼你同时动用 U2（训练循环）、u2-l3（nn.Module）、u2-l5（手工梯度）与 u4-l4（工具链）的全部积累。

**步骤**：

1. **通读原文**。精读 [tensorflow1_ipynb/mlp/mlp-basic.ipynb:L105-L176](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/tensorflow1_ipynb/mlp/mlp-basic.ipynb#L105-L176)（低层版建图 + 训练循环），列出其中所有 TF1 特有构件：2 个 placeholder、6 个 Variable（weights/biases 字典）、softmax 损失、minimize、按名 fetch。
2. **翻译成 PyTorch**（示例代码骨架，结构对齐原文）：

```python
import torch
import torch.nn as nn
import torch.nn.functional as F

# ↔ TF1: weights = {'h1': tf.Variable(...), ...} + biases 字典
class MLP(nn.Module):                      # ↔ TF1: g = tf.Graph() 内的全部定义
    def __init__(self, n_in=784, n_h1=128, n_h2=256, n_out=10):
        super().__init__()
        self.h1  = nn.Linear(n_in,  n_h1)  # ↔ tf.Variable(truncated_normal) + matmul + bias
        self.h2  = nn.Linear(n_h1,  n_h2)
        self.out = nn.Linear(n_h2,  n_out)

    def forward(self, x):                  # ↔ 图上的 layer_1/layer_2/out_layer 节点
        x = F.relu(self.h1(x))             # ↔ tf.nn.relu(tf.add(tf.matmul(...)))
        x = F.relu(self.h2(x))
        return self.out(x)                 # ↔ out_layer（返回 logits）

model  = MLP()
opt    = torch.optim.SGD(model.parameters(), lr=0.1)  # ↔ GradientDescentOptimizer(0.1)
# 训练循环 ↔ sess.run(['train','cost:0'], feed_dict=...)
# for epoch: for xb, yb in train_loader:      # ↔ for i in range(total_batch): next_batch
#     loss = F.cross_entropy(model(xb), yb)   # ↔ softmax_cross_entropy_with_logits + reduce_mean
#     opt.zero_grad()                          # ↔（TF1 无此步：梯度不在图外累积）
#     loss.backward()                          # ↔ minimize 内部的梯度计算
#     opt.step()                               # ↔ minimize 内部的参数更新
```

   训练部分请用 u4-l4 的 `get_dataloaders_mnist` + 手写双层循环补全（替代 `input_data.read_data_sets` 与 `next_batch`），保持超参一致：`learning_rate=0.1`、`batch_size=64`、10 个 epoch、结构 784-128-256-10。
3. **逐行对照注释**：为 PyTorch 版每一行标注它对应 TF1 原文的哪一段（如上），并在文件头部写一份不少于 12 行的概念对照表（可直接扩展 4.4.3 的映射表，补充你翻译中新发现的条目）。
4. **验证翻译**：运行你的 PyTorch 版（MNIST，10 epoch），对照 TF1 原文的 Test ACC 0.975 与 u2-l3 PyTorch 版的 97.54%——三者应在同一量级（随机种子不同导致 1% 以内浮动是正常的）。
5. **复盘**：记录翻译中卡住的三处，检查它们是否都落在映射表里「无对应物」或「语义错位」的行上（如 zero_grad、feed_dict、one-hot 标签）。

**预期结果**：一份可运行的 `mlp_tf1_to_pytorch.py`（或 notebook）、一张 ≥12 行的概念对照表、以及一次 Test ACC ≈ 0.97 的验证运行。TF1 侧原文行为「待本地验证」（需 TF 1.x 环境）。

## 6. 本讲小结

- TF1 是 **define-then-run**：`placeholder`（数据插槽）+ `tf.Variable`（参数声明）+ ops（符号节点）先组装成静态图，`tf.Session` 是执行引擎，`feed_dict` 喂数据、按名 fetch 结果；PyTorch 是 define-by-run，每条语句即时计算并记录历史。
- 感知机 notebook 是图范式的极端标本：连 \((y-\hat y)x\) 更新规则都画成 `tf.assign_add` 节点、由 `tf.group` 打包成 train op；PyTorch 里同样的数学只是一行命令式语句。
- `mlp-lowlevel` 的三档梯度实现（optimizer → `tf.gradients`+`tf.assign` → 手写反向传播公式）与 PyTorch 单一的 `backward()` 对照，揭示了「符号微分 vs 自动微分」；三档 Test ACC 全为 0.945，证明数学与「谁来算梯度」无关。
- PyTorch 的五步范式 `forward → loss → zero_grad → backward → step` 是对 TF1 一次 `sess.run(['train'])` 的展开；TF1 没有 zero_grad，因为梯度不在图外累积。
- 结构组织上，TF1 靠手写 wrapper 函数与全局图变量，PyTorch 靠 `nn.Module` 对象树——由此衍生出 `parameters()`、`state_dict()`、`train()/eval()` 等一整套差异；数据布局 NHWC vs NCHW、标签 one-hot vs 类别索引是另外两条系统性差异。
- 自编码器 notebook 演示了 TF1 的无监督训练（feed_dict 只喂输入，目标即输入）与 `tf.train.Saver` 保存/恢复，分别对应 PyTorch 的同损失写法与 `state_dict` checkpoint。

## 7. 下一步学习建议

至此，整套学习手册（U1–U8 共 44 讲）已全部完成。建议从三个方向继续：

1. **横向补全框架版图**：本仓库的 `pytorch-lightning_ipynb/`（u8-l1/u8-l2）已带你会用 Lightning；接下来可以学 PyTorch 2.x 的新特性（`torch.compile`、FX 图），你会发现它把 PyTorch 重新带回了「图」的世界——与本讲的静态图对照阅读会非常有味道。JAX 的 jit + grad 则是「函数式静态图」的第三个样本。
2. **纵向深挖本仓库未覆盖的专题**：`tensorflow1_ipynb/mechanics/` 下还有 Dataset API、TFRecords、模型保存等 6 份机制 notebook（本手册未展开），可与 PyTorch 的 `tf.data` 继承者及 u4 数据管线对照阅读；`metric/siamese-1.ipynb` 是孪生网络入门。
3. **回到主线做研究**：把 u7 的评估方法（K 折、混淆矩阵）与 u8 的工程化（Lightning、数据增强、梯度检查点）用在你的自有项目上；按 u1-l1 立下的「最想学会的 5 个模型」清单逐一复现，并为每个模型补一份你自己的概念对照表——翻译，是检验理解的最终标准。
