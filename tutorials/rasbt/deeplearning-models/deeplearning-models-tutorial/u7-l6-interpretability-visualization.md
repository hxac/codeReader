# 模型可解释性可视化

> 对应 notebook：`pytorch_ipynb/viz/cnns/cats-and-dogs/` 目录下的两份可视化 notebook 与两个配套 `.py` 模块。
> **行号说明**：`.ipynb` 文件的行号指向该 notebook 的 JSON 源文件行（在 GitHub blob 页面左侧可见）；`.py` 文件行号即普通文本行号。所有永久链接均锚定本手册使用的 HEAD `59beda3`。

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解「对输入求损失梯度」（vanilla gradient / 显著性图，saliency map）这条最基础的模型可解释性路线的直觉与实现。
2. 掌握 guided backpropagation 的原理：如何用模块级反向 hook 把 ReLU 反向传播中的负梯度截断为零。
3. 会把梯度张量加工成灰度热力图，并叠加（overlay）到原图上，对比两种方法的定位能力差异。
4. 顺带熟悉本目录的支撑代码：本地实现的 VGG16（`vgg16.py`）与数据工具（`datautils.py`）。

## 2. 前置知识

- **梯度与链式法则**：回顾 u2-l5，反向传播就是链式法则的矩阵实现。本讲不更新任何权重，只是把同一套反向传播「用在不同的问题上」——问的是：损失对**输入像素**的梯度是多少，而不是对权重的梯度。
- **hook 机制**：回顾 u4-l5。我们已知两种 hook：张量级 `register_hook`（反向触发、拿到该张量的梯度）与模块级 `register_forward_hook`（前向触发、拿到层输出）。本讲引入第三种玩法：模块级**反向** hook，它不仅能「观察」梯度，还能**改写**梯度流。
- **叶子张量与 `.grad`**：PyTorch 默认只为 `requires_grad=True` 的叶子张量保存 `.grad`。训练时叶子是权重；本讲我们把**输入图像张量**变成叶子，于是 `.grad` 里装的就是「损失对每个像素的梯度」。
- **标准化与反标准化**：回顾 u4-l3。可视化前必须用 `UnNormalize` 把张量还原到 [0,1] 像素域，否则图是一片异常值。
- **迁移学习中的 VGG16**：回顾 u7-l4 用过 `torchvision.models.vgg16`（ImageNet 预训练）。注意本讲的 VGG16 是**另一个东西**——`vgg16.py` 里手写的缩小版，自己在 64×64 的猫狗缩略图上从头训练。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py) | 手写 VGG16：5 个卷积 block + 改装过的分类头，输入 64×64，返回 `(logits, probas)` |
| [pytorch_ipynb/viz/cnns/cats-and-dogs/datautils.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/datautils.py) | 数据工具箱：`CatsDogsDataset`、`create_cats_and_dogs_dataloaders`、`UnNormalize`、`convert_rgb_to_grayscale` |
| [pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb) | 本讲主角一：vanilla gradient（损失对输入的原始梯度）可视化 |
| [pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb) | 本讲主角二：guided backprop 可视化，与前一份**只差一个 hook 类** |
| `pytorch_ipynb/viz/cnns/cats-and-dogs/_cnn-basemodel__vgg16-cats-dogs.ipynb` | 前置基线：在 Kaggle 猫狗数据上训练 VGG16 并保存 `vgg16-cats-dogs.state_dict`，两份可视化 notebook 都要加载这份权重 |

两份可视化 notebook 是几乎逐单元格相同的「受控对比」（呼应 u2-l4 的方法论：克隆基线、只改一处）——唯一实质差异是封装类 `VanillaBackprop` 换成 `GuidedBackprop`，后者多了一步 `setup_hooks()`。

## 4. 核心概念与源码讲解

### 4.1 实验舞台：自训 VGG16 与 cats-and-dogs 数据管线

#### 4.1.1 概念说明

要做可解释性可视化，首先得有一个「被解释的模型」和一套确定的数据预处理。仓库作者的选择是：

- **模型**：`vgg16.py` 中手写的 VGG16 缩小版。它不是 torchvision 预训练模型，而是由基线 notebook `_cnn-basemodel__vgg16-cats-dogs.ipynb` 在 Kaggle 的 dogs-vs-cats 数据（64×64 缩略图、二分类）上从头训练 50 个 epoch（Adam，学习率 0.001），训练完执行 `torch.save(model.state_dict(), 'vgg16-cats-dogs.state_dict')` 保存权重。
- **数据**：Kaggle dogs-vs-cats 原始 train 目录按文件编号一切三——编号 >11249 移入 test、>9999 移入 valid、其余留 train；标签由文件名前缀决定（`cat.*.jpg` → 0，`dog.*.jpg` → 1）。
- **预处理契约**：可视化 notebook 硬编码了基线 notebook 算出的训练集统计量 `train_mean = [0.4875, 0.4544, 0.4164]`、`train_std = [0.2521, 0.2453, 0.2481]`（只用训练集统计，避免数据泄漏，见 u4-l3）。**可视化时的 transform 必须与训练时完全一致**，否则模型看到的是「另一种分布」的输入，梯度解释就失真了。

为什么要强调这一节？因为可解释性方法的所有计算都发生在「标准化后的输入张量」空间里，最后又要还原回像素空间画图——来回两次变换（`Normalize` 进、`UnNormalize` 出）是本目录代码的固定动作。

#### 4.1.2 核心流程

整条前置链路：

```text
Kaggle 下载 dogs-vs-cats
  → 按编号切分 train / valid / test
  → 基线 notebook：训练集算 mean/std → 训练 VGG16 50 epoch
  → torch.save(state_dict)
可视化 notebook：
  → 硬编码同样的 mean/std 构造 data_transforms
  → VGG16(num_classes=2) + load_state_dict
  → 对单张测试图求梯度 / 改写梯度 → 热力图
```

#### 4.1.3 源码精读

**模型定义**。VGG16 类的整体骨架是 5 个 `nn.Sequential` 卷积 block 加一个分类头：

- [vgg16.py:L19-L35](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py#L19-L35)：block_1 的写法——两组 `Conv2d(3×3, stride 1, padding 1) + ReLU` 接一个 `MaxPool2d(2,2)`。文件开头的注释给出了 same padding 的推导公式 \( p = (s(o-1) - w + k)/2 \)，即「卷积保尺寸、池化减半」；五个 block 通道按 64→128→256→512→512 增长（block_3 到 block_5 见 [vgg16.py:L54-L140](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py#L54-L140)），空间尺寸链为 64→32→16→8→4→2。
- [vgg16.py:L142-L148](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py#L142-L148)：分类头被明显改装过——注释写着 "changed 4096 to 1024"，且第一层输入是 `512*2*2 = 2048` 而非原版 VGG 的 `512*7*7`。这正是 64×64 小图输入（5 次池化后剩 2×2）与原版 224×224 输入的差异适配。
- [vgg16.py:L150-L159](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py#L150-L159)：构造期初始化——所有 Conv2d/Linear 权重 `normal_(0, 0.05)`、偏置清零。注意：可视化时我们会 `load_state_dict` 用训练好的权重**覆盖**这套初始化，它只在训练起点有意义。
- [vgg16.py:L161-L172](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py#L161-L172)：`forward` 依次过 5 个 block，`x.view(-1, 512*2*2)` 展平后进分类头，**同时返回 `(logits, probas)`**——两份可视化 notebook 里 `logits, probas = model(image_tensor)` 的解包就是与这个签名对齐的；损失用 logits 喂 `F.cross_entropy`（喂 softmax 后的概率是错的，见 u2-l2）。

**数据工具**（全部集中在 `datautils.py`）：

- [datautils.py:L8-L39](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/datautils.py#L8-L39)：`CatsDogsDataset`，就是 u4-l2 讲过的 Dataset 三要素模板——`__init__` 列目录建标签清单（文件名前缀 `cat` → 0，否则 → 1）、`__getitem__` 拼路径用 PIL 读图并逐样本应用 transform、`__len__` 报规模。
- [datautils.py:L45-L71](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/datautils.py#L45-L71)：`create_cats_and_dogs_dataloaders` 一次造出 train/valid/test 三个 loader；train loader `shuffle=True, drop_last=True`，其余两个不 shuffle（与 u4-l1 的约定一致）。
- [datautils.py:L74-L92](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/datautils.py#L74-L92)：`UnNormalize`，逐通道乘 std 加 mean 的逆变换（u4-l3 的老朋友），本讲用它把标准化张量还原后再画图。
- [datautils.py:L95-L105](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/datautils.py#L95-L105)：`convert_rgb_to_grayscale`，本讲的**新工具**，把三通道梯度压成单通道热力图，细节在 4.4 节精读。

**notebook 侧的装载代码**（两份 notebook 相同）：

- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L140-L141](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L140-L141)：硬编码的训练集 mean/std（guided 版对应 [L146-L147](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L146-L147)）。
- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L199-L220](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L199-L220)：`data_transforms` 字典——`'train'` 挂随机增强（旋转、翻转、随机裁剪），`'valid'` 只有确定性的 `Resize([64,64]) + ToTensor + Normalize`。可视化用的正是 `'valid'` 这套（guided 版对应 [L205-L226](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L205-L226)）。
- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L261](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L261)：`model.load_state_dict(torch.load('vgg16-cats-dogs.state_dict'))` 加载基线训练产物，输出 `<All keys matched successfully>` 说明结构与权重完全对上（guided 版对应 [L267](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L267)）。

#### 4.1.4 代码实践

**实践：结构侦查——hook 将覆盖哪些层？**

1. **实践目标**：在跑可视化之前，先静态确认 `vgg16.py` 里的模型有多少个 `nn.ReLU` 模块（4.3 节的 hook 会在每一个上面注册），并验证特征图尺寸链。
2. **操作步骤**（示例代码，可在任意能 import 该目录 `vgg16` 的环境运行）：

   ```python
   from vgg16 import VGG16
   import torch.nn as nn

   model = VGG16(num_classes=2)
   relu_modules = [m for m in model.modules() if type(m) == nn.ReLU]
   print(f"ReLU 模块数量: {len(relu_modules)}")

   n_params = sum(p.numel() for p in model.parameters())
   print(f"总参数量: {n_params:,}")
   ```

3. **需要观察的现象**：ReLU 数量的打印值。
4. **预期结果**：按源码数卷积层——block_1/2 各 2 个、block_3/4/5 各 4 个，共 16 个卷积各跟一个 ReLU，加上分类头里 2 个 ReLU（[vgg16.py:L144](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py#L144)、[L146](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/vgg16.py#L146)），应为 **18**；尺寸链 64→32→16→8→4→2（5 次 2×2 池化）。参数量具体数值待本地验证（用上面的代码打印即可，不建议手算）。
5. 参数量打印结果属于「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：为什么两份可视化 notebook 要硬编码 `train_mean/train_std`，而不是重新在测试图上算一遍均值标准差？

**答案**：标准化统计量必须与训练时一致，模型是在「训练集统计量定义的输入分布」上学习的；换一组统计量等于给模型喂分布漂移的输入，梯度和预测都不可信。这也是 u4-l3 讲过的「防数据泄漏」原则的另一面——统计量只能来自训练集，且训练与推理要用同一套。

**练习 2**：`vgg16.py` 构造函数里的权重初始化（`normal_(0, 0.05)`）对可视化结果有影响吗？

**答案**：没有。`load_state_dict` 会用保存的权重逐参数覆盖初始化值；初始化只决定「训练起点」，可视化加载的是训练终点。输出 `<All keys matched successfully>` 即证明覆盖完整。

**练习 3**：`forward` 返回 `(logits, probas)` 二元组，为什么求损失时用 `logits` 而不是 `probas`？

**答案**：`F.cross_entropy` 内部做数值稳定的 `log_softmax`，只接受 logits；喂 softmax 概率会重复做 log-softmax，得到错误的损失值（u2-l2 已验证过这一点）。

---

### 4.2 对输入求梯度：vanilla gradient 显著性图

#### 4.2.1 概念说明

模型把一张 64×64 的图判成「猫」，它**看的是哪里**？vanilla gradient（通常称显著性图，saliency map；该方法一般追溯到 Simonyan et al. 2013 的可视化工作，notebook 本身只给出了公式未列文献）给出的回答是：

\[ \nabla_{\mathbf{x}} L = \left[\frac{\partial L}{\partial x_1},\ \frac{\partial L}{\partial x_2},\ \cdots,\ \frac{\partial L}{\partial x_d}\right]^{\top} \]

即**损失对每个输入像素的梯度**（两份 notebook 开头的 markdown 单元都写了这个式子）。直觉上：把每个像素当成一个旋钮，\( \partial L / \partial x_i \) 告诉你「把第 \( i \) 个像素稍微调亮一点，这个类别的损失会怎么变」。绝对值大的像素，就是模型决策最敏感、最「在意」的位置——把这些敏感度画成图，就得到显著性图。

为什么这与训练「用的是同一套机器」？训练时我们对**权重**求梯度并更新权重；这里对**输入**求梯度且不更新任何东西。autograd 不关心叶子是权重还是图像——只要输入张量 `requires_grad=True`，反向传播就会一路算到像素。

#### 4.2.2 核心流程

vanilla gradient 的完整流程（单张图）：

```text
1. 图片路径 → data_transforms['valid'] → CHW 标准化张量
2. unsqueeze(0) → NCHW（N=1），并设 requires_grad = True（成为叶子）
3. logits, probas = model(x)
4. cost = F.cross_entropy(logits, label)      # 标量损失
5. model.zero_grad()                          # 清空参数梯度（卫生习惯）
6. cost.backward()                            # 反传到输入
7. gradients = x.grad[0]                      # 取回 CHW 梯度张量
8. 后处理（min-max 归一化 → 灰度化）→ 画图
```

对比 u2-l2 确立的训练五步范式 `forward → loss → zero_grad → backward → step`：这里是**前四步原封不动、去掉第五步**，并把关注点从「参数的 .grad」换成「输入的 .grad」。

#### 4.2.3 源码精读

先看预处理辅助函数（两份 notebook 相同）：

- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L290-L296](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L290-L296)：`preprocess_image(image_path, transform_func, unnormalize_func)` 一次返回三个东西——原始 PIL 图、标准化张量 `img_tensor`（喂给模型求梯度用）、反标准化张量 `unnormed_img_tensor`（画图用）。一次读图、两个空间各取所需。

再看核心。notebook 里有两份实现——一个自由函数和一个类：

- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L305-L326](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L305-L326)：`get_viz(image_tensor, label, model)`，注释就写着 "Computes loss gradient wrt inputs"。关键四行：`image_tensor.requires_grad = True` → `cost = F.cross_entropy(logits, label)` → `cost.backward()` → `gradients = image_tensor.grad[0]`。
- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L337-L356](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L337-L356)：`VanillaBackprop` 类，`run()` 方法与 `get_viz` 逻辑相同，差别仅在 `label` 包装成 `torch.tensor([label])`（一维张量）而 `get_viz` 用零维标量。
- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L365](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L365)：实例化 `vanillabackprop = VanillaBackprop(model, DEVICE)`。

**注意一个读源码的坑**（延续 u7-l4 的「以代码为准」方法论）：`get_viz` 在两份 notebook 里都**定义了但从未被调用**——真正干活的是下面的类。读 notebook 时要以调用链为准，别把未使用的函数当成主实现。

几个值得咀嚼的细节：

1. **`image_tensor.requires_grad = True` 在 `unsqueeze(0)` 之后设置**。输入张量就此成为计算图的叶子，`backward()` 后它的 `.grad` 被填充——不需要 u4-l5 里中间张量那套 `retain_grad()`，因为它是叶子。
2. **`model.zero_grad()`** 在这里并非必需（没有 optimizer 要读参数梯度），属于卫生习惯；但注意它清的是**参数**梯度，不影响输入的 `.grad`。
3. **梯度算在标准化空间**：`∂L/∂x` 里的 x 是 Normalize 之后的张量。由于后处理只看相对大小（min-max 归一化），这不影响可视化结论，但要心里有数。
4. **label 用的是真实标签**：循环里 `zip(['cat.11252.jpg', ...], [0, 1, 0, 1])`，即解释「模型对这张图的真实类别做了什么判断」。你也可以换成预测类或错误类做对比（见综合实践的思考题）。

#### 4.2.4 代码实践

**实践：梯度对账——两种取法应完全一致**

1. **实践目标**：验证 `image_tensor.grad` 与 `torch.autograd.grad` 两条取梯度路径结果一致（衔接 u2-l5 的「手工梯度对账」与 u4-l5 的 `autograd.grad` 用法）。
2. **操作步骤**（示例代码，接在 `vanillabackprop` 之后运行；需要 `dogs-vs-cats/test` 里有图且已加载权重）：

   ```python
   # 路径 A：类的 run()，内部用 x.grad
   g_a = vanillabackprop.run(image_tensor=img_tensor, label=0)

   # 路径 B：autograd.grad 显式指定对哪个张量求导
   x = img_tensor.to(DEVICE).unsqueeze(0)
   x.requires_grad = True
   logits, _ = model(x)
   cost = torch.nn.functional.cross_entropy(logits, torch.tensor([0]).to(DEVICE))
   g_b = torch.autograd.grad(cost, x)[0][0]

   print(torch.allclose(g_a, g_b))
   ```

3. **需要观察的现象**：`allclose` 的输出，以及两张灰度图是否相同。
4. **预期结果**：`True`——两条路径构建的是等价计算图。注意路径 B 用 `torch.autograd.grad` 而不是再次 `cost.backward()`，避免了「同一张图二次 backward 需要 `retain_graph=True`」的问题。
5. 本实践需要下载数据并训练（或取得）基线权重后才能运行，具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `image_tensor.requires_grad = True` 这行删掉，运行会发生什么？

**答案**：`cost.backward()` 会抛错（默认背景下没有需要梯度的叶子，RuntimeError: element 0 of tensors does not require grad...），或者即便不抛错（若参数仍 requires_grad），`image_tensor.grad` 也是 `None`——梯度只流向权重，输入拿不到 `.grad`。

**练习 2**：vanilla gradient 与训练中的梯度下降在「对谁求导」上有什么区别？

**答案**：训练对**权重** \( \theta \) 求导并沿负梯度更新 \( \theta \leftarrow \theta - \eta \nabla_\theta L \)；vanilla gradient 对**输入** \( \mathbf{x} \) 求导且不更新任何东西，只把 \( |\nabla_\mathbf{x} L| \) 当作敏感度图谱来解读。前四步（forward→loss→zero_grad→backward）完全同构，差别只在叶子的选择和有没有 `step()`。

**练习 3**：为什么显著性图通常取梯度的**绝对值**（或正负分开看），而不是直接画有符号梯度？

**答案**：像素梯度可正可负——正值表示调亮会增大该类别损失、负值表示调亮会减小损失，两者都说明模型「在意」这个像素。直接混在一幅图里会正负抵消、把强敏感位置画成中间灰度；取绝对值（后文的 `convert_rgb_to_grayscale` 就是 `np.sum(np.abs(...))`）才能完整呈现敏感强度。

---

### 4.3 guided backprop：用反向 hook 改写 ReLU 的梯度

#### 4.3.1 概念说明

vanilla gradient 的通病是**噪声大**：整张图包括背景都有杂乱梯度，定位能力差。guided backpropagation（guided backprop，出自 Springenberg et al. 2014《Striving for Simplicity: The All Convolutional Net》，guided 版 notebook 的 markdown 单元明确引用了这篇文献）做了一个额外约束，让信号更干净。

关键在 ReLU。前向时 ReLU 把负激活截为零：

\[ a = \max(0, z) \]

反向时，链式法则给 ReLU 的梯度天然带一个掩码 \( \mathbb{1}[z > 0] \)——前向被截断的位置（\( z \le 0 \)），梯度也传不过去：

\[ \frac{\partial L}{\partial z} = \frac{\partial L}{\partial a} \cdot \mathbb{1}[z > 0] \quad\text{（vanilla backprop）} \]

guided backprop **再补一刀**：把负梯度也截为零，只放行「正影响」：

\[ \frac{\partial L}{\partial z} = \frac{\partial L}{\partial a} \cdot \mathbb{1}[z > 0] \cdot \mathbb{1}\!\left[\frac{\partial L}{\partial a} > 0\right] \quad\text{（guided backprop）} \]

notebook 原文说得很清楚：前向已经被 ReLU 截断的负激活，其梯度本来就是 0；guided backprop 更进一步，把反向传播中的**负梯度**也 clamp 到 0，「只关注那些对目标类别有正影响的激活」。实现上不需要改任何模型结构——用**模块级反向 hook** 拦截每个 ReLU 的梯度流，就地改写即可。这正是 hook 机制「改写」能力的经典应用（u4-l5 里我们只用 hook「观察」过）。

#### 4.3.2 核心流程

```text
1. 遍历 model.modules()，找出所有 type(module) == nn.ReLU 的模块
2. 对每个 ReLU 注册反向 hook：relu_backward_hook_fn(module, grad_in, grad_out)
   - 取 grad_in[0]（流到该 ReLU 输入上的梯度）
   - 返回 (torch.clamp(grad_in[0], min=0.0), )  ← 负梯度归零
3. 之后每次 cost.backward()，梯度穿过每个 ReLU 时都被截负
4. 其余流程与 vanilla 完全一致，仍取 image_tensor.grad[0]
```

对照关系一句话：**vanilla = 不装 hook 的同一套代码；guided = 装上截负 hook 的同一套代码**。

#### 4.3.3 源码精读

- [cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb:L343-L372](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L343-L372)：`GuidedBackprop` 类全貌。`run()` 方法（[L358-L372](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L358-L372)）与 vanilla 版 `VanillaBackprop.run` **逐行相同**；全部新意集中在 `setup_hooks()`（[L348](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L348)）。

- 核心三行：

  - [cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb:L351-L352](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L351-L352)：hook 函数签名是 `(module, grad_in, grad_out)`，返回一个**与 grad_in 等长的元组** `(torch.clamp(grad_in[0], min=0.0), )`——返回值会**替换**默认梯度，这就是「改写」而非「观察」。`grad_in[0]` 是流到该 ReLU **输入**上的梯度（已经含 \( \mathbb{1}[z>0] \) 掩码），再 clamp 负值正好实现上式的双重掩码。
  - [cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb:L355-L356](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L355-L356)：遍历 `self.model.modules()`，用 `type(module) == nn.ReLU` **精确类型匹配**（不是 `isinstance`，ReLU 的子类不会被挂上）筛选后逐个 `register_backward_hook`。

- [cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb:L381-L382](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L381-L382)：实例化后**显式调用一次** `guidedbackprop.setup_hooks()` 才生效——`__init__` 里并不注册，忘记这行的话 guided 会静默退化成 vanilla。

三个工程要点：

1. **hook 一旦挂上不会自动消失**。它持续改写这个 model 的 ReLU 梯度流——如果之后拿同一个 model 对象去训练，参数梯度已被污染。可视化与训练要分开，或保存 `register_backward_hook` 返回的 handle 并在用完后 `handle.remove()`（呼应 u4-l5「hook 用完应移除」）。
2. **API 年代**：watermark 显示作者环境是 torch 1.4.0（见 [cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb:L23-L27 附近的 watermark 输出]）。`register_backward_hook` 在较新版本的 PyTorch 中已被弃用并更名为 `register_full_backward_hook`；对 `nn.ReLU` 这种无参数、单输入单输出的模块，两者行为一致，新代码建议直接用后者（待确认：你所用 torch 版本的具体弃用警告文案）。
3. **为什么 hook 挂在 ReLU 而不是卷积层**：guided backprop 的定义只改 ReLU 的反向语义；卷积与池化的反向保持原样。改写点选在哪里，决定了这是哪种可解释性方法。

#### 4.3.4 代码实践

**实践：hook 消融实验——证实「差异全在这个 hook」**

1. **实践目标**：通过开关 hook 与改变 clamp 方向，直观验证 guided 的全部额外效果来自那一行 `torch.clamp(grad_in[0], min=0.0)`。
2. **操作步骤**：
   1. 先完整运行 guided notebook，保存 4 张热力图；
   2. 注释掉 `guidedbackprop.setup_hooks()` 这一行（[L382](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L382)）再跑，对比热力图；
   3. 恢复 hook，把 `torch.clamp(grad_in[0], min=0.0)` 改成 `torch.clamp(grad_in[0], max=0.0)`（只放行**负**影响）再跑。
3. **需要观察的现象**：三组热力图的噪声水平与聚焦程度的差异。
4. **预期结果**：步骤 2 的图应与 vanilla 版 notebook 的输出几乎一样（等于关掉 guided）；步骤 3 会得到一张「亮暗互补」的图——只剩对目标类别有负影响的像素。需要数据与权重，**待本地验证**。
5. 若暂时没有数据，可以只在代码层面做「思想实验」：`run()` 与 vanilla 逐行相同这一事实（对照 [cnn-viz-grad__vgg16-cats-dogs.ipynb:L337-L356](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L337-L356) 与 [guided 版 L358-L372](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L358-L372)）本身就说明唯一变量是 hook。

#### 4.3.5 小练习与答案

**练习 1**：hook 函数为什么必须返回**元组** `(clamped, )` 而不是单个张量 `clamped`？

**答案**：模块级反向 hook 的约定是：返回值须与 `grad_in` 结构对应（`grad_in` 是「对该模块各输入的梯度」组成的元组），PyTorch 用它逐项替换默认梯度。返回单个张量无法与多元组输入对齐，会报错或不生效。

**练习 2**：guided backprop 与「把模型里所有 ReLU 换成别的激活函数」是一回事吗？

**答案**：不是。guided 只改**反向**梯度流（前向计算与损失完全不变），是一种「解释期的后处理」；换激活函数则同时改变前向输出、损失值和预测本身。前者不扰动模型决策，后者会。

**练习 3**：`setup_hooks` 里用 `type(module) == nn.ReLU` 而不是 `isinstance(module, nn.ReLU)`，有什么实际区别？

**答案**：`type` 精确匹配类本身，`isinstance` 还会匹配其子类。若有人继承 `nn.ReLU` 定义了自定义激活（如带泄漏的变体），`isinstance` 会把 guided 的截负语义强加给它，可能不符合预期；`type` 匹配更保守。本模型里两者结果相同（所有 ReLU 都是原生 `nn.ReLU`）。

---

### 4.4 热力图叠加可视化与方法对比

#### 4.4.1 概念说明

拿到 CHW 梯度张量后，离「一张能看的热力图」还有一段后处理路：三通道梯度要压成单通道、动态范围要归一化、上色后最好能**叠回原图**看位置对应关系。仓库的方案分三步：

1. **min-max 归一化**：`gradient = gradient - gradient.min(); gradient /= gradient.max()`，把任意范围的梯度压到 [0,1]；
2. **通道绝对值求和 + 鲁棒裁剪**：`convert_rgb_to_grayscale`；
3. **并排展示**：左原图、右灰度热力图。

**叠加（overlay）**——把热力图伪彩色后以半透明方式覆在原图上——仓库代码里**没有**现成实现（notebook 只做并排两栏），但它是对比定位能力最直观的方式，本节给出示例代码补齐这一步，并作为综合实践的主任务。

#### 4.4.2 核心流程

`convert_rgb_to_grayscale` 的内部流程（对 min-max 归一化后的 CHW 梯度）：

```text
输入: CHW 梯度数组（值域已被压到 [0,1]）
1. grayscale = np.sum(np.abs(im_as_arr), axis=0)   # 三通道绝对值求和 → HW
2. im_max = np.percentile(grayscale, 99)            # 用 99 分位数当"最大值"
   im_min = np.min(grayscale)
3. grayscale = clip((grayscale - im_min)/(im_max - im_min), 0, 1)
4. expand_dims 回 CHW（C=1，方便统一走 CHW→HWC 画图路径）
```

为什么用 **99 分位数**而不是最大值做分母？梯度图里常有极少数异常大的尖峰，若用 max 做归一化，其余 99% 的像素会被压成近乎全黑；用 99 分位截断，牺牲 1% 的过曝换取整体对比度——这是可视化里典型的鲁棒缩放技巧。

#### 4.4.3 源码精读

- [datautils.py:L95-L105](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/datautils.py#L95-L105)：`convert_rgb_to_grayscale` 全文，即上面的四步。注意 `np.sum(np.abs(...), axis=0)` 在通道维求和——正负影响都计入强度。
- [cnn-viz-grad__vgg16-cats-dogs.ipynb:L423-L456](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L423-L456)：vanilla 版主循环——`model.eval()`（[L426](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-grad__vgg16-cats-dogs.ipynb#L426)）之后，对 4 张测试图（2 猫 2 狗，标签 0/1/0/1）逐张：`preprocess_image` → `vanillabackprop.run` → min-max 归一化 → `convert_rgb_to_grayscale` → `plt.subplots(1, 2)` 左边画反标准化原图、右边画灰度梯度。两处形状操作值得注意：`np.transpose(..., (1, 2, 0))` 把 CHW 转 HWC 供 `imshow`（与 u7-l1 的 NCHW→NHWC 同理），热力图一侧再 `[:, :, 0]` 取出单通道并配 `cmap='gray'`。
- [cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb:L440-L473](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L440-L473)：guided 版主循环，与 vanilla 版**逐行相同**，只是 `run` 的对象换成 `guidedbackprop`（[L458-L459](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/cnn-viz-guided-backprop__vgg16-cats-dogs.ipynb#L458-L459) 附近做 min-max 归一化）。两份 notebook 的这一段是理想的「受控对比」阅读材料：唯一变量就是有没有那个 hook。

一个容易忽略的小点：循环里调用了 `model.eval()`。本模型没有 Dropout/BatchNorm，eval() 对计算没有实际影响，但它是评估/推理代码的标准收尾；换成含这些层的模型时（如 u3-l5 的 MobileNet）漏掉会直接改变行为。

**两种方法对比预期**（与仓库 notebook 的输出图对照）：

| 维度 | vanilla gradient | guided backprop |
| --- | --- | --- |
| 噪声 | 大，背景也有明显梯度 | 小，背景基本被压暗 |
| 聚焦 | 弥散在物体与周边区域 | 集中在物体的边缘与纹理细节（猫脸轮廓、毛发、狗耳等） |
| 实现 | 纯 backward | backward + ReLU 反向 hook 截负 |
| 语义 | 全部敏感像素（正负影响混在一起取绝对值） | 只保留对目标类的正影响路径 |

#### 4.4.4 代码实践

**实践：把热力图叠加到原图上**

1. **实践目标**：实现 overlay 函数，将灰度热力图伪彩色后半透明覆在原图上，为综合实践的 8 图对照做准备。
2. **操作步骤**（**示例代码**——仓库本身只有并排画法，以下为本讲补写的扩展实现）：

   ```python
   import numpy as np
   import matplotlib.pyplot as plt
   import torch.nn.functional as F

   def overlay_heatmap(orig_hw, heatmap_hw, alpha=0.5, cmap='jet'):
       """orig_hw: HWC 原图（0-1）；heatmap_hw: HW 热力图（0-1）"""
       fig, ax = plt.subplots(figsize=(4, 4))
       ax.imshow(orig_hw)
       hm = ax.imshow(heatmap_hw, cmap=cmap, alpha=alpha)
       ax.axis('off')
       return fig

   # 在 notebook 主循环里替换画图段：
   #   gray_gradient 已是 (1, H, W)
   #   overlay_heatmap(np.transpose(unnormed_img_tensor.cpu().numpy(), (1, 2, 0)),
   #                   gray_gradient[0])
   ```

3. **需要观察的现象**：热力图高亮区与原图中猫脸/狗身位置的重合程度。
4. **预期结果**：jet 色标下红黄区应落在动物主体上；guided 版的红黄区应比 vanilla 版更贴合物体轮廓、背景更蓝（更冷）。运行效果**待本地验证**。
5. `alpha` 取 0.4~0.6 之间通常可读性最好，可自行调整。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `convert_rgb_to_grayscale` 里的 `np.percentile(grayscale_im, 99)` 换成 `grayscale_im.max()`，图会怎么变？

**答案**：绝大多数梯度值远小于最大值（梯度分布是重尾的），用 max 做分母会把整体亮度压得很低，热力图几乎全黑、只剩个别尖峰可见——这正是该函数用 99 分位做鲁棒缩放的原因。

**练习 2**：画热力图前做了 `gradient - gradient.min()` 再除以 `max`，为什么先减最小值？

**答案**：min-max 归一化把值域线性映射到 [0,1]，先减 min 保证最小值映射到 0；若直接除以 max，负梯度部分会丢失或溢出 [0,1] 之外，`imshow` 会按越界裁剪处理，图的整体对比失真。

**练习 3**：notebook 并排展示时，左边画的是 `unnormed_img_tensor` 而不是 `img_tensor`，为什么？

**答案**：`img_tensor` 是标准化后的张量（值域大约在 [-2, 2] 之外还可能更宽），直接 `imshow` 会得到颜色诡异的图；`UnNormalize` 把它还原回 [0,1] 像素域（u4-l3 讲过的逆变换），肉眼才能对照原图解读热力图位置。

---

## 5. 综合实践

**任务：8 张图 × 2 种方法的叠加对照实验**（本讲指定实践任务）

前置：按 [\_cnn-basemodel__vgg16-cats-dogs.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/viz/cnns/cats-and-dogs/_cnn-basemodel__vgg16-cats-dogs.ipynb) 下载数据（Kaggle dogs-vs-cats，"Download All"）、切分目录并训练得到 `vgg16-cats-dogs.state_dict`（完整 50 epoch 较慢，可先把 `NUM_EPOCHS` 调小做通流程，再补足训练）。

步骤：

1. 分别运行 `cnn-viz-grad` 与 `cnn-viz-guided-backprop` 两份 notebook 至主循环之前，确认两处加载的是**同一份权重文件**。
2. 把主循环的图片列表从 4 张扩到 8 张（4 猫 4 狗，从 `dogs-vs-cats/test` 里挑选，标签与前缀对应：cat→0、dog→1）。
3. 用 4.4.4 的 `overlay_heatmap`（示例代码）为每张图生成两幅叠加图，排成 8 行 2 列的对照矩阵：每行左 vanilla、右 guided。
4. 逐行记录观察，回答三个问题：
   - 哪种方法的热力图更集中在动物主体上？
   - 猫图和狗图的高亮区域各有何形态规律（如集中在脸部/耳朵/皮毛纹理）？
   - 挑一张图故意把 `label` 换成**错误类别**（猫图标 1），热力图指向哪里？这说明「解释」是相对于哪个类别而言的？
5. 附加：数一数两种方法热力图中「亮过 99 分位阈值」的像素占整图比例（可用 `(gray_gradient[0] > 0.99 分位值).mean()` 之类的统计），量化 guided 比 vanilla「干净」多少。

预期结果：guided 的热力图噪声显著更少、边界更锐利；错误标签下热力图会转向对错误类别敏感的区域（例如猫图配 dog 标签时，高亮可能落在背景或与狗相关的纹理上）。所有运行结论**待本地验证**。

## 6. 本讲小结

- 可解释性可视化的最小闭环：**对输入求损失梯度 → 后处理成热力图 → 叠加原图对照**；它与训练共享前四步范式（forward→loss→zero_grad→backward），只是把求导对象从权重换成输入像素，且不 `step()`。
- vanilla gradient（显著性图）实现极简——输入张量设 `requires_grad=True`，`backward()` 后读 `x.grad[0]`——但噪声大、定位弥散。
- guided backprop 的全部新意是一个 ReLU 反向 hook：`(torch.clamp(grad_in[0], min=0.0), )`，在反向传播中把负梯度也截为零，只回传对目标类别的正影响；前向与损失完全不变。
- hook 机制的三种用法至此集齐：张量级 hook 观察梯度（u4-l5）、forward hook 抓激活（u4-l5）、backward hook **改写**梯度流（本讲）；hook 挂上不会自动消失，训练与可视化应隔离或及时 remove。
- 后处理细节决定成败：min-max 归一化、通道绝对值求和、99 分位鲁棒裁剪、`UnNormalize` 还原原图，每一步都有明确动机。
- 两份 notebook 是教科书级的受控对比：除一个 hook 类外逐单元格相同——「克隆基线、只改一处」的方法论在可视化领域同样成立。

## 7. 下一步学习建议

- **读仓库内相关代码**：`pytorch_ipynb/viz/` 目录目前只有 `cnns/cats-and-dogs` 这一个子目录，除本讲两份 notebook 外还有前置基线 ` _cnn-basemodel__vgg16-cats-dogs.ipynb`，可对照阅读其训练循环与权重保存逻辑，理解「被解释的模型」是如何来的。
- **延伸方法**：在 vanilla gradient 与 guided backprop 之外，建议按此顺序继续了解：Guided Grad-CAM（定位能力更强）、Integrated Gradients（满足公理化性质）、SHAP。这些不在本仓库中，需要查阅外部资料。
- **动手方向**：把本讲的 overlay 工具函数推广到你自己在 u3/u7 训练过的模型（如 ResNet-18 MNIST），观察不同架构的显著性图差异；注意含 BatchNorm 的模型务必 `model.eval()` 后再做可视化。
- **衔接下一讲**：本单元（训练评估与技巧）到此收官；下一单元 u8 进入工程化专题，首讲 u8-l1 PyTorch Lightning 基础——hook 与「解释期不改训练」的思想在 Lightning 的回调（callback）机制中会以新面目重现。
