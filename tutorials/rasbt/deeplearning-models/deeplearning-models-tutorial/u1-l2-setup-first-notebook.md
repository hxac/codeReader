# 环境搭建与第一个 Notebook

## 1. 本讲目标

学完本讲，你应该能够：

1. 搭建一个能运行本仓库 notebook 的 Python 环境（PyTorch + torchvision + watermark）。
2. 完整跑通仓库中最简单的 `pytorch_ipynb/basic-ml/perceptron.ipynb`。
3. 读懂 `%watermark` 魔法命令的输出，并学会用它记录自己的环境。
4. 理解 `torch.cuda.is_available()` 与 `torch.device(...)` 的自动设备选择写法。
5. 认识 Model Zoo 中几乎所有 notebook 共有的「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构，之后打开任何一个 notebook 都能快速按段落定位代码。

## 2. 前置知识

- **Python 与 pip/conda**：会用包管理器安装第三方库即可。
- **Jupyter Notebook**：一种「代码单元格（cell）+ 文本单元格」交替的交互式文档。仓库里每个模型就是一个 `.ipynb` 文件；你可以用 `jupyter lab`、`jupyter notebook` 或 VS Code 打开它，从上到下逐格执行（快捷键 `Shift+Enter`）。
- **PyTorch 是什么**：一个深度学习框架，提供张量（tensor，多维数组）运算和自动求导。本讲只用到「创建张量、做乘加运算」这一点点功能，不需要先学 PyTorch 的系统知识。
- **CPU 与 GPU**：CPU 适合通用计算，GPU 适合大规模并行数值计算。深度学习训练在 GPU 上通常快得多，但 PyTorch 代码几乎不需要改动——只需把张量和模型放到正确的 `device` 上。
- **行号约定（重要）**：`.ipynb` 文件本质是 JSON 文本。本讲引用的行号（如 `L165`）指的是该 JSON 文件的**原始行号**，永久链接锚点 `#L165-L166` 也基于原始文件；在 GitHub 上浏览渲染后的 notebook 时，请把链接里的行号理解为「原始 JSON 中的位置」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/basic-ml/perceptron.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb) | 本讲主角：Rosenblatt 感知机的完整可运行实现，也是全仓库结构最简单的 notebook |
| [pytorch_ipynb/data/perceptron_toydata.txt](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/data/perceptron_toydata.txt) | notebook 依赖的 2D toy 数据：制表符分隔的三列（两个特征 + 一个 0/1 标签），共 100 行 |
| [README.md:L12](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L12) | README 索引中指向本 notebook 的那一行（上一讲已介绍过这张总目录表） |

注意一个**路径细节**：notebook 里加载数据用的是相对路径 `'../data/perceptron_toydata.txt'`（见 [pytorch_ipynb/basic-ml/perceptron.ipynb:L102](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L102)），即「notebook 所在目录的上一级下的 `data/` 目录」。所以**必须保持仓库目录结构完整**，不要把单个 `.ipynb` 拷出去单独运行。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. 环境与 watermark（4.1）
2. device 选择：GPU/CPU 自动切换（4.2）
3. notebook 五段式结构（4.3）

### 4.1 环境与 watermark

#### 4.1.1 概念说明

`watermark` 是一个 IPython 扩展（魔法命令集），用来打印当前 notebook 的「环境快照」：Python 版本、IPython 版本、以及指定第三方库的版本。仓库里**每一个** notebook 的第一个代码单元格都是它，作者还把输出结果直接保存在了 notebook 里。

为什么深度学习教程特别在意记录环境？因为同一个 notebook 在 torch 1.0 和 torch 2.x、不同 GPU 型号上的行为和结果可能不同。作者保存的输出相当于「这份结果是在什么环境下跑出来的」的凭证；你复现实验时也应该留下同样的凭证。

#### 4.1.2 核心流程

```text
%load_ext watermark          # 1. 把 watermark 作为 IPython 扩展加载进来（每个 notebook 只需一次）
%watermark -a '作者' -v -p torch   # 2. -a 写作者名，-v 打印 Python/IPython 版本，-p 后跟要打印版本的包名
...（notebook 正文）...
%watermark -iv               # 3. notebook 末尾：-i 打印当前会话信息，-v 列出所有已导入包的版本
```

#### 4.1.3 源码精读

仓库几乎每个 notebook 的开头都是这两行（[pytorch_ipynb/basic-ml/perceptron.ipynb:L31-L32](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L31-L32)）：

```python
%load_ext watermark
%watermark -a 'Sebastian Raschka' -v -p torch
```

这两行加载 watermark 扩展并打印作者、Python/IPython 版本和 torch 版本。作者保存下来的输出是：

```text
CPython 3.6.8
IPython 7.2.0
torch 1.0.0
```

也就是说，你在 GitHub 上看到的 notebook 内全部输出，都是 2019 年前后用 **Python 3.6.8 + torch 1.0.0** 跑出来的——比现在的版本旧很多，这一点在 4.1.4 会再提醒。

notebook 的最后一个单元格（[pytorch_ipynb/basic-ml/perceptron.ipynb:L333](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L333)）是：

```python
%watermark -iv
```

它列出**本会话实际导入过的所有包**的版本。作者保存的输出只有两行——`numpy 1.15.4` 和 `torch 1.0.0`——这本身就告诉你：这个 notebook 的依赖极少，只有 NumPy 和 PyTorch（matplotlib 用于画图但未出现在这份输出中，属正常现象）。

#### 4.1.4 代码实践

1. **实践目标**：装好环境，跑通 perceptron notebook，并留下自己的环境快照。
2. **操作步骤**：
   - 在仓库根目录外任选位置创建虚拟环境并激活（示例命令，conda 同理）：
     ```bash
     python -m venv dlmodels-env
     source dlmodels-env/bin/activate
     pip install torch torchvision jupyter watermark matplotlib numpy
     ```
   - （torch 安装命令随平台和 CUDA 版本不同，以 [pytorch.org](https://pytorch.org) 官方选择器给出的命令为准。）
   - 在**仓库根目录**启动 Jupyter：`jupyter lab`，打开 `pytorch_ipynb/basic-ml/perceptron.ipynb`。
   - 从第一个单元格开始逐格执行（`Shift+Enter`）。
3. **需要观察的现象**：
   - 第一个单元格输出的 Python/torch 版本是你自己的，不再是保存的 3.6.8 / 1.0.0。
   - 执行到数据集单元格时，应打印出 `Class label counts: [50 50]`、`X.shape: (100, 2)`、`y.shape: (100,)`。
   - **版本兼容性提示**：[L104](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L104) 的 `y = y.astype(np.int)` 在 NumPy 1.24+ 中会报 `AttributeError`（`np.int` 已被移除）。如果你遇到这个报错，把 `np.int` 改成内建的 `int` 即可——这只是修复 API 变化，不改变任何语义。
4. **预期结果**：全部单元格无报错执行完毕，最后一格评估输出 `Test set accuracy: 93.33%` 附近的一个准确率（因环境而异，具体数值待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`%watermark -p torch` 和 notebook 末尾的 `%watermark -iv` 输出有何区别？

**答案**：`-p torch` 只打印**指定**包（torch）的版本；`-iv` 会扫描当前会话**所有已导入**的包并逐一列出版本，信息更全，适合在 notebook 收尾时给环境拍「全景照」。

**练习 2**：作者保存的输出显示用的是 Python 3.6.8 + torch 1.0.0。如果你用 torch 2.x 跑同一个 notebook，结果一定完全一样吗？

**答案**：不一定。随机数生成器的实现、CUDA 内核、默认数值行为都可能在版本间变化；本 notebook 对数据打乱用了固定种子（见 4.3 节），能减少不确定性，但浮点运算差异和环境差异仍可能让最终权重/准确率有微小出入。这正是每个 notebook 都记录 watermark 的意义。

### 4.2 device 选择：GPU/CPU 自动切换

#### 4.2.1 概念说明

PyTorch 中所有张量和计算都发生在某个「设备」上：`cpu` 或 `cuda:0`（第 0 块 GPU）。要让代码「有 GPU 用 GPU、没有 GPU 用 CPU」同时兼容两类机器，标准写法是一行条件表达式。仓库 notebook 统一采用这种写法，作者还在 perceptron notebook 的说明里专门标注了一句「- Runs on CPU or GPU (if available)」（[pytorch_ipynb/basic-ml/perceptron.ipynb:L39](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L39)）。

`device` 只是一个「设备描述对象」，它本身不执行任何计算；真正起作用的是把它传给张量创建函数或模型的 `.to(device)`。

#### 4.2.2 核心流程

```text
torch.cuda.is_available()  ──True──▶  device = torch.device("cuda:0")   # 用第 0 块 GPU
        │
        False
        ▼
device = torch.device("cpu")          # 退回 CPU

之后所有 torch.zeros(...) / torch.tensor(..., device=device) 都在选定的设备上创建
```

#### 4.2.3 源码精读

设备选择发生在模型定义单元格的第一行（[pytorch_ipynb/basic-ml/perceptron.ipynb:L165-L166](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L165-L166)）：

```python
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
```

这行先探测 CUDA 是否可用，可用则选第 0 块 GPU，否则退回 CPU。

紧接着，`Perceptron.__init__` 在创建参数张量时直接把 `device` 传进去（[pytorch_ipynb/basic-ml/perceptron.ipynb:L172-L178](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L172-L178)）：

```python
class Perceptron():
    def __init__(self, num_features):
        self.num_features = num_features
        self.weights = torch.zeros(num_features, 1, 
                                   dtype=torch.float32, device=device)
        self.bias = torch.zeros(1, dtype=torch.float32, device=device)
```

权重和偏置都在 `device` 指定的设备上分配。训练时，NumPy 数据也被转换成同设备上的张量（[pytorch_ipynb/basic-ml/perceptron.ipynb:L230-L231](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L230-L231)）：

```python
X_train_tensor = torch.tensor(X_train, dtype=torch.float32, device=device)
y_train_tensor = torch.tensor(y_train, dtype=torch.float32, device=device)
```

**关键规则**：参与同一次运算的张量必须在同一设备上，否则 PyTorch 报错。这就是为什么本 notebook 里「参数在哪、数据就必须搬到哪」。

一个能直观看到 device 生效的证据：作者保存的训练输出里，打印出的权重带着 `device='cuda:0'` 标注（[pytorch_ipynb/basic-ml/perceptron.ipynb:L221-L223](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L221-L223)）：

```text
Weights: tensor([[1.2734],
      [1.3464]], device='cuda:0')
```

说明这份保存的结果是在 GPU 机器上跑出来的；如果你在 CPU 机器上运行，同样的打印会显示 `device='cpu'`（或没有 device 标注）。

#### 4.2.4 代码实践

1. **实践目标**：验证自动设备选择，并对比 GPU 与强制 CPU 两种运行。
2. **操作步骤**：
   - 在模型定义单元格**之后**新插入一个单元格，执行（示例代码）：
     ```python
     print(device)
     print(torch.cuda.is_available())
     ```
   - 正常跑完一次训练（`epochs=5`），记下打印出的 `Weights` / `Bias` 数值。
   - 把 L165 那行临时改成强制 CPU：`device = torch.device("cpu")`，**Restart Kernel 后从头重跑**整个 notebook，再记一次权重数值。
3. **需要观察的现象**：
   - 插入单元格打印的设备与你机器的实际情况一致（无 GPU 则为 `cpu`、`False`）。
   - 两次运行打印的权重数值是否相同；tensor 后面的 device 标注是否从 `cuda:0`（或反之）变成了 `cpu`。
4. **预期结果**：本 notebook 的数据打乱用了固定种子 `np.random.RandomState(123)`（见 4.3 节源码），感知机更新规则本身也是确定性的，所以两次运行的权重数值**应当一致（至多浮点舍入差异）**，唯一确定的差别是 device 标注；具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果不传 `device=device`，`torch.zeros(...)` 默认建在哪里？接着会发生什么？

**答案**：默认建在 CPU 上。若输入数据张量在 GPU 而参数在 CPU（或相反），二者相乘时 PyTorch 会抛出「设备不匹配」的运行时错误；这就是 4.2.3 强调「参与同一次运算的张量必须在同一设备」的原因。

**练习 2**：`torch.device("cuda:0")` 里的 `0` 是什么意思？多卡机器上想用第二块卡怎么写？

**答案**：`0` 是 GPU 的序号（从 0 开始），对应 `CUDA_VISIBLE_DEVICES` 生效后的第 1 块物理卡；第二块卡写 `torch.device("cuda:1")`。仓库的多卡并行主题在后续 `u8-l5` 讲义中展开。

**练习 3**：`torch.cuda.is_available()` 什么时候返回 `False`？

**答案**：包括：没装 CUDA 版 torch（只装了 CPU 版）、机器没有 NVIDIA GPU、有 GPU 但驱动/CUDA 版本与 torch 不匹配等。所以「装了 GPU 却返回 False」时，先检查自己安装的到底是 CPU 版还是 CUDA 版 torch。

### 4.3 notebook 五段式结构

#### 4.3.1 概念说明

打开仓库任何一个模型 notebook，你会发现它们都遵循同一套「骨架」。以 perceptron notebook 为例，Markdown 标题把内容切成五段：

| 段落 | perceptron 中的标题 | 一般内容 |
| --- | --- | --- |
| ① Imports | `## Imports` | 导入库，常伴 watermark |
| ② Dataset/Settings | `## Preparing a toy dataset` | 加载/下载数据、划分、可视化 |
| ③ Model | `## Defining the Perceptron model` | 定义模型类与超参 |
| ④ Training | `## Training the Perceptron` | 实例化模型并执行训练 |
| ⑤ Evaluation | `## Evaluating the model` | 在测试集上评估、画图 |

这个五段式不是官方规范，而是作者的写作习惯；但因为全仓库高度一致，它成了「读懂任何一个 notebook 的地图」——先扫一遍 Markdown 标题，就知道每个单元格属于哪一段、该去哪里改超参、去哪里看结果。

#### 4.3.2 核心流程

```text
① Imports ──▶ ② Dataset ──▶ ③ Model ──▶ ④ Training ──▶ ⑤ Evaluation
   导入          准备数据      定义结构     实例化+训练     测试+可视化
                              (含 device)  (数据→tensor)
```

阅读任何一个新 notebook 的推荐顺序：先看 ② 弄清「在什么数据上做什么任务」，再看 ③ 弄清「模型长什么样」，最后才细读 ④⑤ 的训练评估细节。超参（学习率、epoch 数等）通常集中在 ①/② 或 ④ 的开头几个单元格。

#### 4.3.3 源码精读

逐段对照 perceptron notebook 的真实源码：

**① Imports**（标题在 [L60](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L60)，代码在 [L69-L72](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L69-L72)）：

```python
import numpy as np
import matplotlib.pyplot as plt
import torch
%matplotlib inline
```

只导入 NumPy、matplotlib 和 torch 三个库，`%matplotlib inline` 让图直接嵌在 notebook 里。

**② Dataset**（标题在 [L79](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L79)）：这一段用 `np.genfromtxt` 从相对路径读取 toy 数据并切成特征与标签（[L102-L104](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L102-L104)）：

```python
data = np.genfromtxt('../data/perceptron_toydata.txt', delimiter='\t')
X, y = data[:, :2], data[:, 2]
y = y.astype(np.int)
```

100 个样本、2 个特征、0/1 二分类标签。接着用**固定种子**打乱并按 70/30 划分训练/测试集（[L112-L116](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L112-L116)）：

```python
shuffle_rng = np.random.RandomState(123)
shuffle_rng.shuffle(shuffle_idx)
...
X_train, X_test = X[shuffle_idx[:70]], X[shuffle_idx[70:]]
```

最后用**训练集的**均值和标准差对两个集合做标准化（[L119-L122](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L119-L122)）——注意 `mu`/`sigma` 只从训练集统计，这是避免数据泄漏的规范做法（本手册 `u4-l3` 会专门展开）。

**③ Model**（标题在 [L156](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L156)）：这一段第一行就是 4.2 讲过的 device 选择，然后定义 `Perceptron` 类。它的核心是感知机更新规则——对第 i 个样本，若预测错误则沿误差方向调整权重：

\[ w \leftarrow w + (y^{(i)} - \hat{y}^{(i)})\,x^{(i)}, \qquad b \leftarrow b + (y^{(i)} - \hat{y}^{(i)}) \]

对应实现在 `backward`（计算误差）与 `train`（把误差乘以输入加到权重上）两个方法（[L184-L196](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L184-L196)）：

```python
def backward(self, x, y):  
    predictions = self.forward(x)
    errors = y - predictions
    return errors

def train(self, x, y, epochs):
    ...
    errors = self.backward(x[i].view(1, self.num_features), y[i]).view(-1)
    self.weights += (errors * x[i]).view(self.num_features, 1)
    self.bias += errors
```

值得注意：这里**没有用 PyTorch 的 autograd 和优化器**，梯度更新是手写的（学习率隐含为 1）。算法细节留给 `u2-l1`，本讲你只需认识「③ 段 = 模型定义」这个位置。

**④ Training**（标题在 [L208](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L208)，代码在 [L228-L233](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L228-L233)）：

```python
ppn = Perceptron(num_features=2)
X_train_tensor = torch.tensor(X_train, dtype=torch.float32, device=device)
...
ppn.train(X_train_tensor, y_train_tensor, epochs=5)
```

实例化模型 → NumPy 转 tensor（带上 device）→ 调 `train` 训练 5 个 epoch → 打印最终权重和偏置。

**⑤ Evaluation**（标题在 [L244](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L244)，代码在 [L264-L265](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L264-L265)）：

```python
test_acc = ppn.evaluate(X_test_tensor, y_test_tensor)
print('Test set accuracy: %.2f%%' % (test_acc*100))
```

在测试集上算准确率（作者保存的输出为 `93.33%`），随后还有一个画决策边界的单元格把训练/测试散点与分界线可视化在一起。

#### 4.3.4 代码实践

1. **实践目标**：不看正文，仅靠 Markdown 标题在任何一个陌生 notebook 里定位五段。
2. **操作步骤**：
   - 打开 [pytorch_ipynb/basic-ml/logistic-regression.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb)（`u2-l1` 的主角之一，提前见一面）。
   - 用 Jupyter 的目录大纲（Jupyter Lab 左侧 Outline，或 VS Code大纲视图）只看标题，填一张表：每个 `##` 标题分别对应五段中的哪一段。
   - 再任选 `pytorch_ipynb/mlp/` 或 `pytorch_ipynb/cnn/` 下任一 notebook 重复一次。
3. **需要观察的现象**：段落的措辞可能不同（如 `## Model` / `## Defining the MLP model`、`## Settings` / `## Dataset`），但「导入→数据→模型→训练→评估」的顺序不变；部分 notebook 在①②之间还有 `## Settings` 段放超参。
4. **预期结果**：你能对任意一个新 notebook 在 1 分钟内说出：任务是什么、数据是什么、超参在哪个单元格、去哪里看最终指标。

#### 4.3.5 小练习与答案

**练习 1**：想在 perceptron notebook 里把训练轮数从 5 改成 10，应该改哪个单元格？为什么不用改模型定义？

**答案**：改 ④ Training 段中 `ppn.train(X_train_tensor, y_train_tensor, epochs=5)` 的 `epochs` 实参（[L233](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L233)）。`epochs` 是 `train` 方法的参数而非模型属性，模型定义（③ 段）只关心结构和参数形状。

**练习 2**：perceptron notebook 的②段里，为什么标准化用的 `mu`/`sigma` 只从 `X_train` 计算？

**答案**：测试集代表「未来未知数据」，它的统计信息在训练时不可用；若用全量数据的统计量做标准化，相当于把测试集信息泄漏进训练流程。本 notebook 的写法（[L119-L122](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L119-L122)）是规范做法。

**练习 3**：perceptron 的 `train` 里更新权重没有出现「学习率」这个量，它是多少？

**答案**：隐含为 1。更新式 `self.weights += (errors * x[i])...` 中误差项的系数就是 1，Rosenblatt 原始感知机规则即如此（对比：逻辑回归等基于梯度的方法需要显式学习率，见 `u2-l1`）。

## 5. 综合实践

**任务：给 perceptron notebook 写一份「运行报告」。**

1. 按 4.1.4 装好环境并完整运行 `pytorch_ipynb/basic-ml/perceptron.ipynb`（遇到 `np.int` 报错按提示修复）。
2. 在 notebook 顶部 watermark 单元格之后新增一个「报告」Markdown 单元格，填写：
   - 运行日期、操作系统；
   - `%watermark -v -p torch` 的输出（你的版本）；
   - `print(device)` 与 `torch.cuda.is_available()` 的结果；
   - 最终测试准确率与打印出的 `Weights`、`Bias`。
3. 执行 4.2.4 的 GPU/CPU 对比（有 GPU 的机器做 `cuda:0` vs 强制 `cpu`；只有 CPU 的机器做 `cpu` vs `cpu` 并记录 `is_available()` 为 `False`），把两次的权重数值一并写进报告。
4. 最后用 4.3 的五段式表格给这个 notebook 写目录注释，标注每个单元格属于哪一段。

完成后你手上就有了第一份可复现的实验记录——这套「watermark + device + 结果」的报告格式将贯穿整套手册。

## 6. 本讲小结

- 仓库所有 notebook 以 `%load_ext watermark` + `%watermark` 开头记录环境快照，`%watermark -iv` 收尾列出全部已导入包的版本；作者保存的 perceptron 输出对应 Python 3.6.8 + torch 1.0.0 + GPU。
- `torch.device("cuda:0" if torch.cuda.is_available() else "cpu")` 是全仓库统一的设备自动选择写法；参数张量和数据张量都必须带上同一 `device`，否则运算报错。
- `.ipynb` 是 JSON 文件，本讲永久链接中的行号指原始 JSON 行号；notebook 用相对路径读数据，必须保持仓库目录结构完整地运行。
- Model Zoo 的 notebook 普遍遵循「Imports → Dataset/Settings → Model → Training → Evaluation」五段式，可按标题快速定位超参与结果。
- perceptron notebook 不用 autograd 和优化器，而是手写感知机更新规则（隐含学习率 1），算法细节留给 `u2-l1`。
- 在新版 NumPy（≥1.24）上需把 `y.astype(np.int)` 改为 `y.astype(int)` 才能运行。

## 7. 下一步学习建议

- 下一讲 `u1-l3 共享 helper 模块导览`：认识 `pytorch_ipynb/` 下 6 个 `helper_*.py` 共享模块（数据加载、训练循环、评估绘图、随机种子），它们是新版 notebook 的公共基础设施，也是 `u4` 单元的基础。
- 想先巩固 PyTorch 基础的读者，可以配合官方 "60 Minute Blitz" 教程补张量与 autograd 概念，再回来继续。
- 本讲的五段式地图将立即在 `u2-l1`（感知机与逻辑回归的算法细节）中反复使用，建议把综合实践的运行报告留好备查。
