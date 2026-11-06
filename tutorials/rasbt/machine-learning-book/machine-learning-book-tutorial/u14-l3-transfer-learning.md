# 迁移学习实战：COVID 胸片分类

## 1. 本讲目标

学完本讲，你应该能够：

1. 把任何一个卷积神经网络解剖成「特征提取器（卷积基座）+ 分类头（全连接层）」两段，并理解 ResNet18 的分类头输入维度是 512。
2. 用 `param.requires_grad = False` 冻结卷积基座，替换最后的 `fc` 层，完成迁移学习的「两步改造法」。
3. 说明为什么仓库 `ch14/ch14_part2.py` 中的 `train()` 训练循环与测试评估循环可以原封不动地复用到迁移学习模型上。
4. 理解 Top-1 与 Top-5 两种评估口径的定义、适用条件，以及为什么小数据集上「预训练 + 微调」优于从零训练。

> **仓库边界声明（先读我）**：书第 14 章末节介绍了用预训练 ResNet18 做 COVID 胸片分类的迁移学习实验，但**配套代码仓库并未收录这一节的代码**——`ch14/` 目录下只有 `ch14_part1.py`（MNIST CNN）与 `ch14_part2.py`（CelebA 微笑 CNN）。这与 u14-l2 的澄清一致。因此本讲的策略是：**结构、训练循环、评估循环、`requires_grad` 机制全部引用仓库真实源码；迁移学习特有的改造代码（冻结、换头、Top-5）全部明确标注为「示例代码」**，供你在自己的环境中运行验证。凡未在本地跑过的结果，均标注「待本地验证」。

## 2. 前置知识

### 2.1 迁移学习（transfer learning）

迁移学习的直觉：你学会了开车，再学开卡车就快得多——因为「看路、打方向、控速」这些底层技能是通用的。卷积网络同理：在一个大数据集（通常是 ImageNet，约 120 万张图、1000 类）上训练出来的网络，它的前几层学到的是边缘、颜色、纹理等通用视觉特征，中间层学到的是局部结构（眼睛、轮子、纹理拼图）。这些特征对胸片、人脸、猫狗等**其他图像任务同样有用**。

迁移学习的标准做法分两个层次：

| 策略 | 卷积基座 | 分类头 | 适用场景 |
|------|----------|--------|----------|
| 特征提取（feature extraction） | 冻结，不动 | 重新训练 | 目标数据集很小 |
| 微调（fine-tuning） | 解冻部分/全部，用小学习率继续训练 | 一起训练 | 目标数据集中等以上 |

### 2.2 为什么小数据集上预训练优于从零训练

从零训练一个深层 CNN 需要海量数据，原因有二：

1. **参数远多于样本会过拟合**。ResNet18 约有 1120 万个参数（可用下方示例代码自己数出来）。如果只有几千张胸片，网络会直接把训练集「背下来」。
2. **随机初始化的优化起点很差**。低层参数从一个糟糕的起点出发，在小数据上还没有学会「什么是边缘」就已经过拟合了。

预训练相当于把优化的起点搬到「已经会看图」的位置，再在新任务上做小幅修正。冻结基座后，可训练参数可以从约 1120 万骤降到几百个（一个新的二分类头只有 512×1 + 1 = 513 个参数），过拟合风险随之骤降。

### 2.3 requires_grad：参数的「梯度开关」

u13-l1 讲过 PyTorch 的 autograd：前向计算构建计算图，`loss.backward()` 沿图回传，把梯度**累加**到每个 `requires_grad=True` 的叶子参数的 `.grad` 上。关键推论：

- `requires_grad=False` 的参数**不参与梯度计算**，`.grad` 始终为 `None`；
- 优化器的 `step()` 遇到没有 `.grad` 的参数会直接跳过——所以「冻结」不需要换优化器，只需关掉开关。

「冻结」与 u13-l3 见过的 `torch.no_grad()` 上下文（推理时不建图）是两回事：前者是**参数属性**，后者是**上下文管理器**。

### 2.4 Top-1 与 Top-5 准确率

模型输出一组类别得分（logits）后：

- **Top-1 准确率**：得分最高的那个类别恰好是正确答案才算对。这就是 u14-l2 中所有「accuracy」的口径。
- **Top-5 准确率**：正确答案出现在得分前 5 名里就算对。

Top-5 是 ImageNet 1000 类时代的惯例——类别又多又相近（上百种狗），用 Top-5 衡量「模型是否至少把正确类别排进了前几名」。注意一个约束：**k 不能超过类别数**。本讲的胸片二分类只有 2 个类别，Top-5 没有意义（前 5 名必然包含正确答案，Top-5 恒等于 100%），所以二分类实验只需报告 Top-1。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
|------|------|--------------|
| [ch14/ch14_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py) | CelebA 微笑分类 CNN 的完整脚本 | 模型的两段式结构、`train()` 循环、测试评估循环、整模型保存 |
| [ch13/ch13_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py) | PyTorch 动态图与 autograd 基础 | `requires_grad` 开关的机制演示（u13-l1 已精读，此处承接） |

迁移学习本身在仓库中的「亲戚」：第 16 章的 GPT-2 / BERT 微调（[ch16/ch16-part2-gpt2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part2-gpt2.py)、[ch16/ch16-part3-bert.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py)）也是「加载预训练权重 + 换分类头 + 微调」，学完本讲你会在 u16 看到同一套路在 NLP 上的重现。

## 4. 核心概念与源码讲解

### 4.1 特征提取器 + 分类头：CNN 的两段式解剖

#### 4.1.1 概念说明

几乎所有的分类 CNN 都可以切成两段：

```
输入图像 → [卷积基座：卷积+激活+池化 ×N 层] → 特征图 → [池化压平 + 全连接层] → 类别得分
            \___________ 特征提取器 ___________/            \_____ 分类头 _____/
```

- **特征提取器**：负责「看」。逐层把像素抽象成越来越高级的特征，输出一个高维特征图（形状 `[batch, C, H, W]`）。
- **分类头**：负责「判」。把特征图压成一个向量，再用一个全连接层（`nn.Linear`）线性映射到类别数。

迁移学习只动分类头、（最多）微调基座，正是利用了这个结构：**换任务时，「看」的能力大体保留，「判」的标准必须重学**。

#### 4.1.2 核心流程

以 torchvision 的 ResNet18 为例（示例代码，基于仓库 environment.yml 钉死的 torchvision 0.9.0）：

```
输入 [B, 3, 224, 224]
  → conv1 + bn1 + relu + maxpool        （低层特征：边缘、颜色）
  → layer1 / layer2 / layer3 / layer4   （残差块，逐层抽象；layer4 输出 512 通道）
  → avgpool                              （全局平均池化 → [B, 512]）
  → fc: nn.Linear(512, 1000)             （分类头：512 → ImageNet 1000 类）
```

两步改造法只改最后一行：`model.fc = nn.Linear(512, num_classes)`。二分类时 `num_classes=1`（配 sigmoid），多分类时 `num_classes=K`（配 softmax/交叉熵）。

#### 4.1.3 源码精读

仓库 CelebA CNN 用 `nn.Sequential` + `add_module` 完整展示了这个两段式。先看卷积基座（特征提取器）：

- [ch14/ch14_part2.py:L289-L306](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L289-L306)：用 `add_module` 逐个挂上 4 个卷积块（Conv2d → ReLU → MaxPool2d，前两块还带 Dropout），通道数逐层翻倍 3→32→64→128→256。这一段就是「特征提取器」，与 ResNet18 的 conv1~layer4 角色相同。

接着是全书第一次出现的**全局平均池化**压缩与分类头：

- [ch14/ch14_part2.py:L317-L318](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L317-L318)：`AvgPool2d(kernel_size=8)` 把 `[B, 256, 8, 8]` 的特征图在 8×8 空间上取平均，得到 `[B, 256, 1, 1]`；`nn.Flatten()` 再压成 `[B, 256]`。**256 这个数就是本网络的「特征维度」**，对应 ResNet18 的 512。
- [ch14/ch14_part2.py:L326-L327](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L326-L327)：`add_module('fc', nn.Linear(256, 1))` 是分类头——输入 256 维特征，输出 1 个 logit，接 sigmoid 得「微笑概率」。**把它换成 `nn.Linear(256, K)`，同一个基座就能服务 K 分类任务**；换成 ResNet18 就是 `nn.Linear(512, K)`。

书中还有一个非常值得学的工程习惯——**用假张量检查中间形状**：

- [ch14/ch14_part2.py:L311-L312](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L311-L312)、[L320-L321](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L320-L321)、[L332-L333](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L332-L333)：三处 `x = torch.ones((4, 3, 64, 64)); model(x).shape`，在挂载新模块的前后各做一次前向，随时确认特征图形状。迁移学习改造后同样应该做这件事。

设备与训练装备的定义：

- [ch14/ch14_part2.py:L343-L345](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L343-L345)：`device = torch.device("cuda:0")` 并 `model.to(device)`，模型与后续每个 batch 必须同设备。
- [ch14/ch14_part2.py:L350-L351](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L350-L351)：`BCELoss`（配上面的 sigmoid 输出）与 `Adam(model.parameters(), lr=0.001)`。注意 `model.parameters()` 会拿到**全部**参数——迁移学习冻结后，这里可以只喂可训练子集（见 4.2.3）。

最后，训练好的整模型保存：

- [ch14/ch14_part2.py:L465-L466](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L465-L466)：`torch.save(model, path)`。u13-l2 讲过：整模型保存依赖类的代码位置，`state_dict` 方式更稳健，迁移实验里两者皆可。

#### 4.1.4 代码实践

**实践目标**：亲手数出「基座」与「分类头」各自的参数量，建立对「冻结后省了多少参数」的直觉。

操作步骤（示例代码，本地运行）：

```python
import torch
import torch.nn as nn
import torchvision

# 仓库 CelebA CNN 的分类头（对应 ch14_part2.py L317-L327 的结构）
gap = nn.Sequential(nn.AvgPool2d(8), nn.Flatten())
fc  = nn.Linear(256, 1)

featmap = torch.ones(4, 256, 8, 8)      # 假装这是第 4 个卷积块的输出
print(gap(featmap).shape)               # 期望 [4, 256]
print(fc(gap(featmap)).shape)           # 期望 [4, 1]

# 参数量盘点
n_base = sum(p.numel() for p in fc.parameters())  # 分类头
print(f'CelebA 分类头参数量: {n_base}')            # 256*1 + 1 = 257

# torchvision 的 ResNet18（分类头换成 2 分类）
resnet = torchvision.models.resnet18(num_classes=2)
total = sum(p.numel() for p in resnet.parameters())
head  = sum(p.numel() for p in resnet.fc.parameters())
print(f'ResNet18 总参数: {total}, 其中 fc: {head}')  # 约 11.2M 总量, fc 仅 1026
```

需要观察的现象：`gap` 输出形状 `[4, 256]`；ResNet18 的 `fc` 只占总参数量的万分之一左右。

预期结果：特征提取器占绝对多数参数，分类头微不足道——这就是「冻结基座只训分类头」能防过拟合的量化依据。（ResNet18 总参数约 1120 万，具体数字待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AvgPool2d(kernel_size=8)` 恰好能把 `[B, 256, 8, 8]` 压成 `[B, 256, 1, 1]`？若特征图是 `[B, 256, 7, 7]` 会怎样？

答案：全局平均池化的 `kernel_size` 等于特征图的空间边长时，池化窗口恰好覆盖整张特征图，输出 1×1。若输入是 7×7 而 kernel 是 8，窗口超出边界会报错（或按实现裁剪后输出 0×0）；因此基座输出尺寸变化时必须同步改 kernel_size，而 ResNet18 用自适应池化 `AdaptiveAvgPool2d((1,1))` 规避了这个问题。

**练习 2**：把 CelebA CNN（`nn.Linear(256, 1)`）改造成 10 分类，哪一行要变？输出层之后的 sigmoid 还能保留吗？

答案：改成 `nn.Linear(256, 10)`；sigmoid 不能保留——u12-l3 讲过，对多个输出单元各自套 sigmoid 得到的值相互独立、加和不为 1，不构成概率分布。10 分类应输出 10 个 logits 并配 `CrossEntropyLoss`。

### 4.2 `param.requires_grad = False`：冻结参数的机制

#### 4.2.1 概念说明

冻结（freeze）就是把基座里每个参数张量的 `requires_grad` 开关置为 `False`。效果链条是：

```
requires_grad=False → backward 时该参数不进计算图的梯度路径
                    → .grad 保持 None → optimizer.step() 跳过它
                    → 参数值在整个训练过程中不变
```

它的价值在迁移学习里有两个：

1. **防止破坏预训练特征**：新换的分类头是随机初始化的，最初的梯度噪声很大；若此时基座也一起更新，随机头的「错误指挥」会把预训练好的特征搅乱。
2. **省显存、加速**：不为冻结参数建图，反向传播的计算量与激活值的梯度维护都显著减少。

#### 4.2.2 核心流程

两步改造法的标准顺序（示例代码）：

```python
# 第 1 步：冻结整个基座
for param in model.parameters():
    param.requires_grad = False

# 第 2 步：替换分类头（新层的参数默认 requires_grad=True，不受上面循环影响）
model.fc = nn.Linear(512, num_classes)
```

顺序很重要：先冻结再换头，新头才不会被误冻。若先换头后冻结，需要改成 `for name, p in model.named_parameters(): if not name.startswith('fc'): p.requires_grad = False`。

#### 4.2.3 源码精读

仓库中 `requires_grad` 的机制演示在第 13 章（u13-l1 精读过，这里只取与冻结直接相关的部分）：

- [ch13/ch13_part1.py:L101-L102](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L101-L102)：创建张量时 `torch.tensor(3.14, requires_grad=True)` 声明「请为它计算梯度」。`nn.Linear` 等层内部的权重默认就是这样创建的。
- [ch13/ch13_part1.py:L109-L123](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L109-L123)：先用 `a.requires_grad` 读取开关状态，再演示原地切换 `w.requires_grad_()` 把 False 改成 True。冻结用的是同一开关的关闭方向：`p.requires_grad_(False)` 或直接赋值 `p.requires_grad = False`，二者等价。

回看第 14 章的优化器定义：

- [ch14/ch14_part2.py:L350-L351](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L350-L351)：`torch.optim.Adam(model.parameters(), lr=0.001)` 把全部参数交给优化器。迁移学习冻结后，更清晰的写法是只喂可训练参数：

```python
# 示例代码：只优化「还开着梯度开关」的参数
params = [p for p in model.parameters() if p.requires_grad]
optimizer = torch.optim.Adam(params, lr=1e-3)
```

即便仍传 `model.parameters()`，Adam 在 `step()` 时也会因冻结参数没有 `.grad` 而跳过它们，训练结果不受影响——但显式过滤能让「谁在被训练」一目了然，也避免优化器维护无用的状态（Adam 会为收到过的参数保存动量缓存）。

顺带说明版本差异：仓库环境是 torchvision 0.9.0，加载预训练权重用 `torchvision.models.resnet18(pretrained=True)`；torchvision 0.13 起该写法被弃用，改为 `weights=torchvision.models.ResNet18_Weights.DEFAULT`。本讲示例沿用仓库环境的写法。

#### 4.2.4 代码实践

**实践目标**：验证「冻结开关」真的改变了可训练参数集合。

操作步骤（示例代码，本地运行）：

```python
import torchvision, torch.nn as nn

model = torchvision.models.resnet18(pretrained=True)   # torchvision 0.9.0

def count(m, tag):
    n = sum(p.numel() for p in m.parameters() if p.requires_grad)
    print(f'{tag}: 可训练参数 {n:,}')

count(model, '冻结前')
for p in model.parameters():
    p.requires_grad = False
model.fc = nn.Linear(512, 1)                            # 二分类新头
count(model, '两步改造后')
print(model.fc.weight.requires_grad)                     # 新头不受冻结影响
```

需要观察的现象：冻结前约 11,689,512 个可训练参数（ResNet18 含 1000 类头），两步改造后骤降为 513（512×1 + 1）。

预期结果：数字下降四个数量级；最后一行打印 `True`，证明「先冻结、后换头」的顺序保住了新头。（精确总数待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：如果把两步改造的顺序颠倒（先换头、再对 `model.parameters()` 全量冻结），会发生什么？如何补救？

答案：新换的 `fc` 也被冻结，训练中它始终是随机初始化的权重，模型输出恒定、损失不下降。补救：改用 `named_parameters()` 按名字排除 fc，或换头后再执行 `for p in model.fc.parameters(): p.requires_grad = True`。

**练习 2**：冻结参数后忘记改优化器（仍传 `model.parameters()`），训练会出错吗？结果对吗？

答案：不会报错，结果也正确——`backward()` 不为冻结参数生成梯度，`step()` 跳过 `.grad is None` 的参数。差别仅在于优化器内部会为它们维护空的动量状态，代码意图也不如显式过滤清晰。

**练习 3**：`p.requires_grad = False` 与推理时包裹 `with torch.no_grad():` 有什么不同？

答案：前者是参数的持久属性，影响的是「训练时梯度是否回传到该参数」；后者是临时上下文，影响的是「这一段前向是否构建计算图」，常用于评估阶段省显存提速（见 4.3.3 的 `model.eval()` 分支）。二者作用对象不同，常常同时使用。

### 4.3 替换分类头与训练循环复用

#### 4.3.1 概念说明

迁移学习最容易被低估的一点是：**训练循环本身不需要任何改动**。仓库的 `train()` 函数只依赖三样东西：

1. `model` 是一个可调用的 `nn.Module`（能 `model(x_batch)` 前向）；
2. `loss_fn` 能比较预测与标签；
3. `optimizer` 持有要更新的参数。

两步改造后的 ResNet18 同样满足这三条，因此 [ch14/ch14_part2.py:L353-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L353-L390) 的 `train()` 可以原样复用——这就是「训练循环复用」模块的含义，也是 u13-l2 五段式流水线（数据、预处理、模型、训练循环、评估保存）中「训练循环」独立成段的价值。

需要小心的只有输出接口对齐：仓库循环假设模型输出可写作 `model(x_batch)[:, 0]`（形状 `[B, 1]` 取出第 0 列）。改造后的 ResNet18 二分类头输出正是 `[B, 1]`，天然兼容；若你用 `nn.Linear(512, 2)` 的双输出写法，就得改循环里的取值与损失函数。

#### 4.3.2 核心流程

完整的迁移学习实验流程（以二分类胸片为例）：

```
1. 数据管线：transforms（Resize 224 + ToTensor + 按预训练模型的均值方差 Normalize）
            → Dataset → DataLoader（train/valid/test 三份）
2. 模型改造：resnet18(pretrained=True)
            → 冻结全部参数 → model.fc = nn.Linear(512, 1)
3. 训练复用：loss_fn + optimizer(可训练参数) → 复用 train(model, epochs, train_dl, valid_dl)
4. 逐阶段微调（可选）：解冻 layer4 → 更小学习率 → 再跑一遍 train()
5. 评估：复用测试循环算 Top-1；多分类时补 Top-5
```

一个数据管线细节值得强调：预训练模型对输入分布敏感，Normalize 必须用**预训练时相同的统计量**（ImageNet 的三个通道均值 `[0.485, 0.456, 0.406]`、标准差 `[0.229, 0.224, 0.225]`），而不是在胸片数据上重新统计——基座学到的特征是按 ImageNet 分布校准的。

#### 4.3.3 源码精读

逐段读 `train()`，看它为什么与「哪个模型」无关：

- [ch14/ch14_part2.py:L353-L357](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L353-L357)：函数签名 `train(model, num_epochs, train_dl, valid_dl)`——模型与数据加载器都是参数，四个列表按 epoch 记录损失与准确率。没有任何一行绑定 CelebA 网络的结构。
- [ch14/ch14_part2.py:L359-L370](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L359-L370)：训练分支。`model.train()` 切换模式（启用 Dropout）；u12-l3 总结的五步骨架在此完整呈现——前向 `pred = model(x_batch)[:, 0]` → 算损失 `loss_fn(pred, y_batch.float())` → `loss.backward()` → `optimizer.step()` → `optimizer.zero_grad()`。梯度只流向 `requires_grad=True` 的参数，所以这段代码对「冻结模型」自动生效。
- [ch14/ch14_part2.py:L368-L370](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L368-L370)：损失按样本数加权累加、`is_correct` 用 `(pred>=0.5)` 把概率阈值化成 0/1 标签再与真值比较——这就是内嵌的 **Top-1** 判定（二分类版）。
- [ch14/ch14_part2.py:L375-L387](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L375-L387)：验证分支。`model.eval()` + `with torch.no_grad():` 双保险——前者关 Dropout，后者不建图省显存（4.2.5 练习 3 的两个机制在此同时出场）。注意验证分支没有 backward/step。
- [ch14/ch14_part2.py:L389](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L389)：每轮结束打印训练/验证准确率，迁移学习阶段一（只训 fc）与阶段二（解冻微调）都能直接用它观察「解冻是否带来增益、是否开始过拟合」。

调用侧与模型保存：

- [ch14/ch14_part2.py:L392-L394](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L392-L394)：固定种子、`num_epochs = 30`、`hist = train(...)`。固定 `torch.manual_seed` 保证每次实验可比——对比两种迁移策略时尤其重要。
- [ch14/ch14_part2.py:L465-L466](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L465-L466)：整模型 `torch.save`。注意：这里保存的是**训练完的对象**，若先冻结后保存，加载回来的模型参数里 `requires_grad` 状态也会一并保留，续训前别忘了按阶段重新设置开关。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完成规格指定的两阶段实验——阶段 A「冻结全部卷积层，只训练新 fc 层」；阶段 B「解冻最后一个卷积块（layer4）微调」；分别记录测试 Top-1 准确率并对比。

**数据准备**：仓库没有收录 COVID 胸片数据与管线。书中该节的数据获取与目录组织以书第 14 章对应小节的说明为准（组合 COVID 患者胸片与常规胸片构成二分类数据集）。若暂时拿不到胸片数据，可以先用仓库现成的 CelebA 管线（[ch14/ch14_part2.py:L194-L275](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L194-L275)，需按 [L66-L75](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L66-L75) 的说明手动下载）作载体演练完整流程，代码结构完全相同。

操作步骤（示例代码；基于仓库环境 torchvision 0.9.0 / torch 1.8.0）：

```python
# --- 阶段 A：冻结基座，只训新分类头 ---
import torch, torch.nn as nn, torchvision
from torchvision import transforms

# 1) 数据管线：Resize 到 224 + ImageNet 统计量归一化（示例代码）
tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
])
# 数据集请按上方「数据准备」说明挂载；DataLoader 写法与 ch14_part2.py L270-L275 相同
# train_dl, valid_dl, test_dl = ...

# 2) 两步改造
model = torchvision.models.resnet18(pretrained=True)
for p in model.parameters():
    p.requires_grad = False
model.fc = nn.Linear(512, 1)          # 二分类：1 个 logit

# 3) 损失与优化器（只喂可训练参数；BCEWithLogitsLoss 把 sigmoid 融进损失，数值更稳）
loss_fn = nn.BCEWithLogitsLoss()
optimizer = torch.optim.Adam(
    [p for p in model.parameters() if p.requires_grad], lr=1e-3)

# 4) 复用仓库 train() 循环（从 ch14_part2.py L353-L390 原样拷贝）
hist = train(model, 5, train_dl, valid_dl)   # 小数据集 3~5 epoch 通常足够

# 5) 测试 Top-1（复用 ch14_part2.py L422-L435 的循环）
# ...

# --- 阶段 B：解冻最后一个卷积块，小学习率微调 ---
for p in model.layer4.parameters():     # torchvision ResNet18 的最后一个残差阶段
    p.requires_grad = True
optimizer = torch.optim.Adam([
    {'params': model.layer4.parameters(), 'lr': 1e-5},   # 旧特征：小步慢调
    {'params': model.fc.parameters(),    'lr': 1e-4},    # 新头：步子稍大
])
hist2 = train(model, 3, train_dl, valid_dl)
# 再跑一遍测试循环，记录阶段 B 的 Top-1
```

需要观察的现象：

1. 阶段 A 训练速度快（反向只过新头），验证准确率通常几个 epoch 内就冲高然后走平；
2. 阶段 B 每 epoch 更慢，但验证/测试 Top-1 若继续上升，说明基座末段针对胸片微调有增益；若训练准确率猛涨而验证回落，则是学习率过大冲坏了预训练特征，应调小再试；
3. 两个阶段都应固定 `torch.manual_seed`（对照 [L392](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L392)），否则对比无意义。

预期结果：在同类小数据集二分类任务上，阶段 B（特征提取 + 微调）的测试 Top-1 一般不低于阶段 A（纯特征提取），代价是训练时间与过拟合风险；具体数字**待本地验证**——请在自己的数据与机器上记录后填入下表：

| 阶段 | 可训练层 | 学习率 | 测试 Top-1 | 每 epoch 耗时 |
|------|----------|--------|-----------|---------------|
| A：只训 fc | fc | 1e-3 | 待验证 | 待验证 |
| B：+layer4 微调 | layer4 + fc | 1e-5 / 1e-4 | 待验证 | 待验证 |

#### 4.3.5 小练习与答案

**练习 1**：阶段 B 中为什么给 `layer4` 和 `fc` 配不同的学习率，而不是统一用 1e-3？

答案：`fc` 是随机初始化的新层，离最优解很远，可以用较大学习率；`layer4` 的权重已在 ImageNet 上收敛到很好的位置，大步长会把预训练特征「冲坏」（灾难性遗忘）。分组小学习率让旧特征只做精细修正。

**练习 2**：复用仓库 `train()` 时，若把新头写成 `nn.Linear(512, 2)`（双输出）而不改其他代码，会出什么问题？

答案：循环里 `pred = model(x_batch)[:, 0]` 只取了第 0 个 logit，`loss_fn(pred, y_batch.float())` 的形状虽可对上，但语义错误（丢掉了第 1 个输出的信息），训练无法正常收敛。双输出写法应改为取全部 logits、标签转长整型、损失换 `CrossEntropyLoss`。

**练习 3**：阶段 A 只训练 513 个参数，为什么也能得到不错的准确率？

答案：因为 512 维特征本身已经是「看过」上百万张图的基座提炼出来的判别性表示，二分类头只需在这些线性可分性很好的特征上拟合一个超平面——这正是迁移学习价值的最小化体现。

### 4.4 Top-1 与 Top-5 对比评估

#### 4.4.1 概念说明

评估多分类模型时，「预测对了没有」可以有不同的宽容度：

\[ \text{Top-}k\text{ 准确率} = \frac{1}{N}\sum_{i=1}^{N} \mathbb{1}\left[\, y_i \in \text{TopK}(\hat{z}_i, k)\, \right] \]

其中 \(\hat{z}_i\) 是模型对第 \(i\) 个样本输出的类别得分向量，\(\text{TopK}(\hat{z}_i, k)\) 取得分最大的 \(k\) 个类别的集合，\(\mathbb{1}[\cdot]\) 是指示函数。\(k=1\) 时就是普通准确率。

- Top-1 严格：必须一锤定音才得分。
- Top-5 宽容：衡量「模型的短名单里有没有正确答案」，适合 ImageNet 这种千类别、类间相近（183 种狗）的任务。

两个工程要点：

1. **k 受类别数约束**：类别数为 \(C\) 时 Top-k 在 \(k \ge C\) 恒等于 100%，没有信息量。二分类胸片只报 Top-1。
2. **对比评估的意义**：从零训练 vs 迁移学习、阶段 A vs 阶段 B，都要在**同一测试集、同一评估口径**下比较，结论才成立。

#### 4.4.2 核心流程

```
model.eval() + torch.no_grad()
遍历 test_dl 的每个 batch：
    logits = model(x_batch)                 # [B, C]
    top1 = logits.argmax(dim=1)             # 每行最大值下标
    top5 = logits.topk(5, dim=1).indices    # 每行前 5 大下标
    Top-1 对：top1 == y
    Top-5 对：y 出现在 top5 的某一列
累计后除以测试集样本数
```

#### 4.4.3 源码精读

仓库的测试评估循环是 Top-1 的参考实现：

- [ch14/ch14_part2.py:L422-L435](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L422-L435)：`model.eval()` + `torch.no_grad()` 双保险后遍历 `test_dl`，`(pred>=0.5).float() == y` 做的正是二分类 Top-1 判定（概率过阈值视为预测类别 1），`is_correct.sum()` 累加正确数，最后 `accuracy_test /= len(test_dl.dataset)` 以测试集样本数归一。多分类 Top-k 版只需把阈值判定换成 `argmax`/`topk`（见 4.4.2）。
- [ch14/ch14_part2.py:L440-L460](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L440-L460)：测试批上的定性检查——把模型输出乘 100 显示为百分比，配 `imshow` 与真值标签逐张对照。迁移学习实验里抽看几张「错得最离谱」的样本往往能发现数据问题（如胸片摆位、造影差异），是只看准确率得不到的信息。

#### 4.4.4 代码实践

**实践目标**：写一个同时统计 Top-1 / Top-k 的通用评估函数，验证「类别数不足时 Top-k 退化」。

操作步骤（示例代码，本地运行）：

```python
import torch

@torch.no_grad()
def evaluate_topk(model, data_loader, device, k=5):
    model.eval()
    top1 = topk = total = 0
    for x, y in data_loader:
        x, y = x.to(device), y.to(device)
        logits = model(x)                       # 多分类模型：输出 [B, C]
        pred1 = logits.argmax(dim=1)
        predk = logits.topk(k, dim=1).indices   # [B, k]
        top1 += (pred1 == y).sum().item()
        topk += (predk == y.unsqueeze(1)).any(dim=1).sum().item()
        total += y.size(0)
    return top1 / total, topk / total

# 退化实验：3 类随机模型，k 取 5
torch.manual_seed(0)
dummy = torch.nn.Linear(4, 3)                   # 3 类
xs = torch.randn(10, 4); ys = torch.randint(0, 3, (10,))
loader = [(xs, ys)]
t1, t5 = evaluate_topk(dummy, loader, torch.device('cpu'), k=5)
print(f'Top-1={t1:.2f}, Top-5={t5:.2f}')        # Top-5 必为 1.00
```

需要观察的现象：无论模型多差，Top-5 都等于 1.00——因为 3 类任务的前 5 名必然包含真值。

预期结果：Top-1 在 0~1 之间随机波动，Top-5 恒为 1.0，印证「k 必须远小于类别数才有评估价值」。（待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：ImageNet 预训练 ResNet18 的 Top-5 错误率约 11%（官方数据），为什么大家还认为它「很好」？

答案：1000 类里随机猜的 Top-5 正确率只有 5×0.1% = 0.5%；模型把正确答案保持在前 5 名的概率约 89%，说明其类别排序能力极强。Top-5 衡量的是排序质量，而非只看第一选择。

**练习 2**：本讲胸片二分类实验为什么报告 Top-1 就够了？什么情况下必须补充 Top-5？

答案：C=2 时 Top-2 已恒等于 100%，更大的 k 无意义。当任务类别数多（如胸部疾病多标签、肺炎病原体多分类）且类间易混淆时，Top-5 才能提供额外信息。

**练习 3**：对比「从零训练的 ResNet18」与「迁移学习的 ResNet18」时，除了 Top-1 还应报告哪些量才算公平对比？

答案：至少还应包括：同一测试集与同一数据划分（固定种子）、训练 epoch 数与每 epoch 耗时（算力成本）、训练/验证曲线（区分欠拟合与过拟合）、参数量与可训练参数量。只比一个 Top-1 数字无法判断收益来自哪里。

## 5. 综合实践

把本讲四个模块串成一个完整的迁移学习对比实验（全部为示例代码，结果待本地验证）：

**任务**：在同一份小规模图像二分类数据（优先 COVID 胸片，取数以书第 14 章对应小节指引为准；退而求其次用 CelebA 微笑子集）上，比较三种方案的测试 Top-1：

| 方案 | 模型 | 说明 |
|------|------|------|
| 基线 | 自己的小 CNN | 直接复用 [ch14/ch14_part2.py:L289-L327](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L289-L327) 的结构（把输入尺寸按数据调整） |
| 方案一 | ResNet18 特征提取 | 两步改造：全冻结 + `fc = nn.Linear(512, 1)`（4.2） |
| 方案二 | ResNet18 逐步微调 | 方案一训练后解冻 layer4，分组小学习率续训（4.3） |

步骤：

1. 搭好 train/valid/test 三份数据管线（ResNet 方案的 transform 必须含 224 Resize 与 ImageNet Normalize；基线 CNN 用仓库原 transform 即可）。
2. 三种方案都复用从 [ch14/ch14_part2.py:L353-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L353-L390) 拷贝的 `train()` 与 [L422-L435](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L422-L435) 拷贝的测试循环；只允许改模型定义与优化器参数，不改循环。
3. 每种方案固定 `torch.manual_seed(1)`，用相同 epoch 预算，记录：验证曲线（判断过拟合）、测试 Top-1、总训练时间、可训练参数量（4.1.4 的 `count` 函数）。
4. 画一张 2×1 子图对比三个方案的训练/验证准确率曲线（仿照 [ch14/ch14_part2.py:L399-L417](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L399-L417) 的画法）。
5. 用一段话回答：小数据集上预训练为什么赢？赢在哪个环节（初始化位置、可训练参数量、还是特征通用性）？你的证据是哪几列数字？

## 6. 本讲小结

- 任何分类 CNN 都可解剖为「卷积基座（特征提取器）+ 全连接头（分类器）」；仓库 CelebA CNN 用 `AvgPool2d` + `Flatten` + `Linear(256, 1)`（[ch14_part2.py:L317-L327](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L317-L327)）展示了这一结构，ResNet18 对应的特征维度是 512。
- 迁移学习两步改造法：先 `requires_grad=False` 冻结基座，再 `model.fc = nn.Linear(512, K)` 换头；顺序不能颠倒，否则新头也被冻住。`requires_grad` 的机制根源在 [ch13_part1.py:L101-L123](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L101-L123)。
- 训练循环与模型解耦：`train()`（[ch14_part2.py:L353-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L353-L390)）只依赖 `nn.Module` 接口与输出形状约定，冻结模型无需改一行循环；`model.train()`/`model.eval()` + `no_grad()` 的双模式设计对任何模型通用。
- 逐阶段解冻微调要配分组小学习率：预训练层 1e-5 量级、新头 1e-4~1e-3 量级，防止大步长冲坏预训练特征。
- Top-1 要求一锤定音，Top-5 衡量「短名单里有没有答案」；k 必须远小于类别数，二分类只报 Top-1。仓库测试循环（[ch14_part2.py:L422-L435](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L422-L435)）是 Top-1 的参考实现。
- 仓库未收录书第 14 章末节的 COVID 迁移学习代码；本讲的改造代码均为示例代码，动手实验时请以仓库真实代码为骨架、书中小节为数据指引。

## 7. 下一步学习建议

- **本单元收官，下一单元进入循环神经网络**：u15-l1 将从手算隐状态递推开始讲 RNN。迁移学习的思维（「预训练表示 + 轻量适配」）在 NLP 里会更加极致，可提前留意。
- **预告 u16（Transformer 与注意力机制）**：`from_pretrained` + 换分类头 + 微调的完整代码在 [ch16/ch16-part2-gpt2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part2-gpt2.py) 与 [ch16/ch16-part3-bert.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py) 中真实存在，学完本讲再读它们，你会发现「冻结/换头/微调」的套路完全同构。
- **源码阅读建议**：通读 [ch14/ch14_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py) 全文，重点体会「数据增强 transform（L194-L205）→ 双 DataLoader（L270-L275）→ 模型分三段搭（L289-L327）→ train/eval 双模式循环（L353-L390）→ 测试与保存（L422-L466）」这条完整链路——它就是你在综合实践中复用的骨架。
- **延伸阅读**：书第 14 章末节关于迁移学习与 COVID 胸片分类的正文（含学习率调度的讨论）；若你的 torchvision ≥ 0.13，顺手对比 `pretrained=True` 与 `weights=ResNet18_Weights.DEFAULT` 两种 API 的迁移。
