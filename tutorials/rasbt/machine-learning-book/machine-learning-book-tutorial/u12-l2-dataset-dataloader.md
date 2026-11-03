# Dataset 与 DataLoader 输入管线

## 1. 本讲目标

在第 11 章里,我们为了给 `NeuralNetMLP` 喂小批量数据,手写了一个 `minibatch_generator`:洗牌下标、切批、`yield` 产出。本讲学习 PyTorch 为这件事提供的官方基建——`Dataset` 与 `DataLoader`,学完后你应该能够:

1. 说清 `Dataset` 的两个方法契约 `__len__` 与 `__getitem__` 各自承担什么职责,以及为什么满足这两个方法就能被 `DataLoader` 接受。
2. 用 `TensorDataset` 一行封装「特征张量 + 标签张量」,或手写 `JointDataset` 这样的等价类。
3. 熟练组合 `DataLoader` 的 `batch_size`、`shuffle`、`drop_last` 三个旋钮,并推导批数与各批大小的公式。
4. 仿照书中的 `ImageDataset`,写一个从磁盘目录逐张读图的自定义数据集,配合 `transforms` 统一图片尺寸后送入 `DataLoader`。
5. 理解 `torchvision.datasets` 官方数据集(CelebA、MNIST)与自定义 `Dataset` 的关系:它们实现的是同一套协议。

## 2. 前置知识

- **张量与形状变换(u12-l1)**:`DataLoader` 产出的每个批次就是一个在前面**新增了 batch 维**的张量,理解 `shape`、`transpose`、`cat`/`stack` 是本讲的基础。
- **epoch 与 minibatch(u11-l2)**:一个 epoch 是把全部训练样本过一遍;minibatch 是每次参数更新用的一小撮样本(如 100 个)。第 11 章我们手工实现了「洗牌 + 切批」,本讲用 `DataLoader` 一行替代它。
- **生成器与惰性求值(u8-l2)**:`DataLoader` 遍历时是按需取样、一批批产出,不会一次性把数据复制成大张量,这与 `stream_docs` 用 `yield` 逐条读 CSV 的思想一致。
- **可迭代对象与迭代器**:能写在 `for x in obj:` 里的对象叫可迭代对象(iterable)。`DataLoader` 就是一个可迭代对象;`Dataset` 本身也常可直接 `for` 遍历(等价于从下标 0 逐个调用 `__getitem__`)。
- **协议(duck typing)**:Python 不检查「你是不是某个类的子类」,只看「你有没有那个方法」。只要一个对象实现了 `__len__()` 和 `__getitem__(idx)`,PyTorch 就把它当作合法的 `Dataset` 对待——`torch.utils.data.Dataset` 基类几乎没有逻辑,主要作用是「挂牌」声明契约。

## 3. 本讲源码地图

本讲全部源码集中在 `ch12/ch12_part1.py` 的「Building input pipelines in PyTorch」一节(第 12 章上半部分):

| 源码位置 | 作用 |
| --- | --- |
| [ch12/ch12_part1.py:L8-L10](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L8-L10) | 导入 `DataLoader`、`Dataset`、`TensorDataset`,全部来自 `torch.utils.data` |
| [ch12/ch12_part1.py:L270-L293](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L270-L293) | 从一个现成张量直接创建 `DataLoader`,认识 `batch_size` 与 `drop_last` |
| [ch12/ch12_part1.py:L295-L323](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L295-L323) | 手写 `JointDataset` 组合两个张量;`TensorDataset` 一行等价替代 |
| [ch12/ch12_part1.py:L326-L341](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L326-L341) | `shuffle=True` + 多 epoch 循环(shuffle、batch、repeat) |
| [ch12/ch12_part1.py:L344-L439](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L344-L439) | 从 `cat_dog_images` 目录读图:文件列表、标签推断、两个版本的 `ImageDataset`、`transforms` |
| [ch12/ch12_part1.py:L442-L545](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L442-L545) | `torchvision.datasets` 的 CelebA 与 MNIST:官方目录式数据集同样遵守 `Dataset` 协议 |
| [ch12/cat_dog_images/](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/cat_dog_images) | 6 张本地图片(`cat-01.jpg` ~ `dog-03.jpg`),本讲自定义数据集的素材 |

## 4. 核心概念与源码讲解

### 4.1 从现成张量直接创建 DataLoader

#### 4.1.1 概念说明

`DataLoader` 是 PyTorch 的「取数机器」:你把数据交给它,它负责按批次(batch)产出张量。最简单的用法是直接把一个张量丢给它——因为一维张量本身支持 `t[i]` 取元素和 `len(t)`,天然满足 `Dataset` 协议,所以不需要再写任何类。

这一步要建立两个直觉:

- `DataLoader` 产出的每个东西都比原始样本**多一个 batch 维**:6 个标量组成的 `t`,逐个产出时是 shape 为 `[1]` 的一维张量,而不是 0 维标量。
- `batch_size` 是切批大小;`drop_last` 决定最后不足一批的「尾巴」要不要丢掉。

#### 4.1.2 核心流程

设样本数为 \( N \),批大小为 \( B \),则 `DataLoader` 一个 epoch 产出的批数为:

\[ \text{批数} = \begin{cases} \lceil N/B \rceil & \text{drop\_last=False(默认,尾巴保留)} \\ \lfloor N/B \rfloor & \text{drop\_last=True(尾巴丢弃)} \end{cases} \]

伪代码:

```text
data_loader = DataLoader(数据, batch_size=B, drop_last=是否丢尾批)
for batch in data_loader:      # 惰性产出,每次一批
    用 batch 做事               # batch 的 shape = (B, *单样本shape)
```

#### 4.1.3 源码精读

**最简形式——不传任何参数**:书中先造了一个长度为 6 的一维浮点张量,直接交给 `DataLoader`,然后遍历:

[ch12/ch12_part1.py:L277-L284](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L277-L284) —— `t = torch.arange(6, dtype=torch.float32)` 后 `data_loader = DataLoader(t)`,`for item in data_loader` 逐个打印。不指定 `batch_size` 时默认值为 1,所以循环 6 次,每次产出一个 shape 为 `[1]` 的张量(`tensor([0.])`、`tensor([1.])`、…)。

**指定批大小与 drop_last**:

[ch12/ch12_part1.py:L289-L292](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L289-L292) —— `DataLoader(t, batch_size=3, drop_last=False)` 把 6 个元素切成 2 批,`enumerate(data_loader, 1)` 的第二个参数 `1` 让计数从 1 开始,打印出 `batch 1: tensor([0., 1., 2.])` 与 `batch 2: tensor([3., 4., 5.])`。此处 `drop_last=False` 其实是默认值,书中显式写出是为了引出这个参数。

注意对比:第 11 章的 `minibatch_generator` 需要「洗牌下标 + 切片 + yield」十来行;这里一行 `DataLoader(...)` 就完成了取数逻辑,而且同一套接口以后还能换成任意复杂的数据集。

#### 4.1.4 代码实践

1. **实践目标**:亲眼确认「batch 维是被 `DataLoader` 额外加上的」,并验证批数公式。
2. **操作步骤**:在装好 PyTorch 的环境中(`ch12` 目录下启动 Python 即可)依次执行:

```python
# 示例代码(基于书中 L277-L292 改写)
import torch
from torch.utils.data import DataLoader

t = torch.arange(6, dtype=torch.float32)

dl = DataLoader(t, batch_size=4, drop_last=False)
for i, batch in enumerate(dl, 1):
    print(f'batch {i}: shape={batch.shape}, 值={batch}')

dl2 = DataLoader(t, batch_size=4, drop_last=True)
print('drop_last=True 的批数:', len(dl2))
```

3. **需要观察的现象**:第一段循环产出 2 批——第一批 4 个元素、第二批只剩 2 个元素(\(\lceil 6/4 \rceil = 2\));第二段 `drop_last=True` 时批数变为 1(\(\lfloor 6/4 \rfloor = 1\)),尾巴被丢弃。
4. **预期结果**:`batch.shape` 分别为 `torch.Size([4])` 与 `torch.Size([2])`;`len(dl2)` 为 1。(预期由公式与代码逻辑推得,具体打印格式待本地验证。)

#### 4.1.5 小练习与答案

**练习 1**:把 `torch.arange(6)` 换成 `torch.arange(7)`、`batch_size=3`、`drop_last=False`,会产出几批?各多大?
**答案**:3 批,大小分别为 3、3、1(\(\lceil 7/3 \rceil = 3\)),最后一批是不足额的尾批;若 `drop_last=True` 则只剩 2 批。

**练习 2**:为什么 `DataLoader` 能直接接受一个裸张量?
**答案**:`DataLoader` 只要求对象支持按下标取元素(即实现 `__getitem__`)、能报出长度(即实现 `__len__`);一维张量天然具备这两个方法,按 Python 的 duck typing 就是一个合法数据集。

**练习 3**:`DataLoader(t)`(不传 `batch_size`)每次循环产出什么形状?
**答案**:shape 为 `[1]` 的一维张量——`DataLoader` 永远在最前面加 batch 维,`batch_size=1` 时产出 `tensor([x])` 而非标量 `x`。

### 4.2 Dataset 契约:JointDataset 与 TensorDataset

#### 4.2.1 概念说明

真实监督学习的数据总是成对出现:(特征 x,标签 y)。`DataLoader` 切批时必须保证「第 i 个特征配第 i 个标签」不被拆散,于是需要一个对象把两个张量**按下标绑定**起来——这就是 `Dataset`。

自定义 `Dataset` 只需实现两个方法:

| 方法 | 职责 | 缺了会怎样 |
| --- | --- | --- |
| `__len__(self)` | 返回样本总数 | `DataLoader` 不知道取到何时为止、`len()` 失效 |
| `__getitem__(self, idx)` | 给定下标,返回**一个**样本(通常是元组) | 无法按批取样 |

`torch.utils.data.TensorDataset` 是官方提供的现成实现,功能与手写的 `JointDataset` 完全一样——书中手写一遍的目的正是让你看清 `TensorDataset` 内部在做什么。

#### 4.2.2 核心流程

```text
class XxxDataset(Dataset):
    __init__ : 把要绑定的张量/文件列表/标签存成属性
    __len__  : return 样本数
    __getitem__(idx): return (样本x[idx], 标签y[idx])   # 元组

DataLoader 在每个批次里做的事(概念上):
    批 = [ dataset[i] for i in 本批下标列表 ]   # 每个元素是一个 (x, y) 元组
    再按元组位置分别堆叠 → batch[0] 是堆好的 x 批,batch[1] 是堆好的 y 批
```

也就是说,`__getitem__` 返回**单样本元组**,`DataLoader` 负责把同一位置的元素分别堆成批次张量——这正是 u12-l1 里 `torch.stack` 沿新维度堆叠的直接应用。

#### 4.2.3 源码精读

**手写 JointDataset**:

[ch12/ch12_part1.py:L300-L307](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L300-L307) —— `__init__` 只是把 `x`、`y` 存为属性;`__len__` 返回 `len(self.x)`;`__getitem__` 返回元组 `self.x[idx], self.y[idx]`。三个方法各一行,这就是 `Dataset` 契约的全部。

**实例化与官方等价物**:

[ch12/ch12_part1.py:L312-L323](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L312-L323) —— 先 `torch.manual_seed(1)` 固定随机性,造出特征 `t_x = torch.rand([4, 3])`(4 个样本、3 个特征)与标签 `t_y = torch.arange(4)`;随后 `joint_dataset = JointDataset(t_x, t_y)`,紧接着一句注释 `# Or use TensorDataset directly`,`joint_dataset = TensorDataset(t_x, t_y)` 用官方类覆盖了同一个变量——两者可完全互换。最后直接 `for example in joint_dataset:` 遍历(不经过 `DataLoader`),每个 `example` 是 `(x_i, y_i)` 元组,分别用 `example[0]`、`example[1]` 打印。

这验证了前面说的:`Dataset` 本身就是可迭代对象,`DataLoader` 是在它之上加了「批处理、洗牌、并行加载」的增强外壳。

#### 4.2.4 代码实践

1. **实践目标**:亲手体验「`__getitem__` 返回什么,批次里就堆叠什么」。
2. **操作步骤**:

```python
# 示例代码(仿照书中 L300-L323)
import torch
from torch.utils.data import Dataset, TensorDataset, DataLoader

torch.manual_seed(1)
t_x = torch.rand([4, 3], dtype=torch.float32)
t_y = torch.arange(4)

# 手写版:让 __getitem__ 额外带上样本下标,返回三元组
class JointDataset3(Dataset):
    def __init__(self, x, y):
        self.x, self.y = x, y
    def __len__(self):
        return len(self.x)
    def __getitem__(self, idx):
        return self.x[idx], self.y[idx], idx

ds = JointDataset3(t_x, t_y)
dl = DataLoader(ds, batch_size=2, shuffle=False)
for x_b, y_b, i_b in dl:
    print('x批:', x_b.shape, ' y批:', y_b, ' 下标批:', i_b)

# 官方版对照
for ex in TensorDataset(t_x, t_y):
    print(ex)
```

3. **需要观察的现象**:三元组 `(x, y, idx)` 的每个位置被分别堆叠——`x_b` 形状 `[2, 3]`,`i_b` 是形如 `tensor([0, 1])` 的下标批;`TensorDataset` 遍历产出的是二元组。
4. **预期结果**:`DataLoader` 对元组长度没有限制,元组有几个元素,批次就有几个张量。(打印顺序与具体数值待本地验证。)

#### 4.2.5 小练习与答案

**练习 1**:`__len__` 和 `__getitem__` 为什么必须**同时**实现?
**答案**:`__getitem__` 让对象可被按下标取样,`__len__` 让 `DataLoader` 知道总样本数、据此划分批次与判断迭代终点;只实现其中一个,取数管线都无法运转。

**练习 2**:`TensorDataset(t_x, t_y)` 与手写 `JointDataset` 何时选谁?
**答案**:数据已经在内存里是张量时,直接用 `TensorDataset`,零样板代码;当取样需要额外逻辑(打开图片文件、数据变换、拼接路径等)时,官方类不够用,必须自定义——本讲 4.4 的 `ImageDataset` 就是实例。

**练习 3**:若 `t_x` 有 4 个样本而 `t_y` 有 5 个元素,`TensorDataset` 会出现什么问题?
**答案**:`__len__` 以先传入的张量为准(此处为 4),两数组长度不一致属于使用错误:要么遍历漏掉未配对元素,要么下标越界报错——构造前应保证长度一致。

### 4.3 shuffle、batch 与 repeat:训练循环的三件套

#### 4.3.1 概念说明

- **shuffle(洗牌)**:每个 epoch 打乱取样顺序。回顾 u2-l3 的教训:鸢尾花数据按类别排序,若不洗牌逐样本更新,Adaline 的权重会周期性摆动。同理,`cat_dog_images` 的文件列表排序后前 3 张全是猫、后 3 张全是狗,不洗牌的批次会「整批同类别」,梯度方向剧烈震荡。**分类数据必须洗牌**。
- **repeat(重复)**:书中标题 "Shuffle, batch, and repeat" 的 repeat 指外层 epoch 循环——同一个 `DataLoader` 对象可以反复迭代多个 epoch,`shuffle=True` 时每个 epoch 会重新洗牌,得到不同的批次划分。
- **batch 与 drop_last**:`drop_last=True` 常在批归一化等「批内统计」场景使用——不足额的尾批可能让统计失真或直接报错。

#### 4.3.2 核心流程

```text
data_loader = DataLoader(dataset, batch_size=B, shuffle=True)

for epoch in range(num_epochs):          # repeat:外层重复
    for batch in data_loader:            # 每轮进入时重新洗牌、切批
        x_batch, y_batch = batch         # batch[0]/batch[1] 对应元组两个位置
        前向 → 损失 → 反向 → 更新         # (u12-l3 将补全这部分)
```

洗牌洗的是**样本下标**,样本本身(元组配对关系)不会被破坏——`x` 与 `y` 永远成对移动。

#### 4.3.3 源码精读

[ch12/ch12_part1.py:L330-L335](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L330-L335) —— 先 `torch.manual_seed(1)` 固定随机种子以便结果可复现,然后 `data_loader = DataLoader(dataset=joint_dataset, batch_size=2, shuffle=True)`。打印时 `batch[0]` 是堆叠后的特征批、`batch[1]` 是标签批——因为 4.2 中 `__getitem__` 返回二元组。

[ch12/ch12_part1.py:L337-L341](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L337-L341) —— `for epoch in range(2):` 外层循环里**再次**遍历同一个 `data_loader`。这是理解 repeat 的关键:`DataLoader` 不是一次性的生成器(u8-l2 的 `stream_docs` 只能耗尽一次),每次 `for` 都从头开始一轮新的取样;`shuffle=True` 时两轮的批次内容顺序不同,而 4 个样本、`batch_size=2` 每轮稳定产出 2 批。

#### 4.3.4 代码实践

1. **实践目标**:观察「同 epoch 内顺序打乱、样本配对不乱、跨 epoch 重新洗牌」。
2. **操作步骤**:

```python
# 示例代码(基于书中 L330-L341)
import torch
from torch.utils.data import TensorDataset, DataLoader

torch.manual_seed(1)
t_x = torch.arange(8).reshape(4, 2).float()   # 4 个样本,每样本 2 个特征
t_y = torch.arange(4) * 10                    # 标签放大 10 倍,便于肉眼配对
dl = DataLoader(TensorDataset(t_x, t_y), batch_size=2, shuffle=True)

for epoch in range(2):
    print(f'--- epoch {epoch+1} ---')
    for i, (xb, yb) in enumerate(dl, 1):
        print(f'batch {i}: x={xb.tolist()} y={yb.tolist()}')
```

3. **需要观察的现象**:每行里 `x` 的两行特征分别配着除以 10 即得的特征值(如 `y=[30, 0]` 对应的特征是第 3、第 0 个样本),配对永不错位;两个 epoch 的批次顺序不同。
4. **预期结果**:每个 epoch 稳定 2 批,批内样本组合与批间顺序在两轮中不同;`xb.shape` 为 `[2, 2]`。(具体洗牌结果取决于随机种子,待本地验证。)

#### 4.3.5 小练习与答案

**练习 1**:100 个样本、`batch_size=32`、`drop_last=True`,一个 epoch 有几批、实际消费多少样本?
**答案**:\(\lfloor 100/32 \rfloor = 3\) 批,消费 96 个样本,最后 4 个样本被丢弃(下个 epoch 洗牌后它们会重新参与)。

**练习 2**:为什么 `shuffle=True` 对按类排序的数据尤其重要?
**答案**:不洗牌时批次内样本同类别(如前 3 张全是猫),每批梯度都朝单类别方向猛推,参数来回震荡难以收敛——这正是 u2-l3 中未洗牌鸢尾花数据让 AdalineSGD 周期摆动的同一现象。

**练习 3**:书中为什么在构造 `DataLoader` 之前调用 `torch.manual_seed(1)`?
**答案**:洗牌依赖随机数;先固定全局随机种子,可使每次运行得到相同的洗牌序列,保证实验可复现(承接各章一贯的 `random_state` 习惯)。

### 4.4 从磁盘目录构建数据集:ImageDataset 与 transforms

#### 4.4.1 概念说明

前三个模块的数据都在内存里。更常见的情形是**数据在磁盘上**:几百 GB 的图片不可能全载入内存,正确的做法是——数据集只存「文件路径列表 + 标签列表」,**每次 `__getitem__` 被调用时才打开一张图**。这样内存占用只与批大小相关,与数据总量无关(out-of-core 思想在深度学习输入管线中的体现,呼应 u8-l2)。

书中用 `ch12/cat_dog_images/` 下 6 张真实图片(cat-01~03.jpg、dog-01~03.jpg)演示了两个递进的版本:

- **版本一**:`__getitem__` 返回 `(文件路径字符串, 标签)`——证明契约不在乎返回类型,但字符串不能直接喂给模型;
- **版本二**:`__getitem__` 内用 PIL 打开图片并应用 `transform`,返回 `(图像张量, 标签)`——真正可用的形态。

此外有两个配套知识点:

- **标签从文件名推断**:文件名含 `dog` 则标签 1,否则 0(cat 为 0)。
- **transforms**:6 张图尺寸参差(书中打印可见形状各异,具体数值待本地验证),不同尺寸的张量无法堆叠成批,必须先 `Resize` 统一到 (80, 120)。

#### 4.4.2 核心流程

```text
1. pathlib.Path.glob('*.jpg') + sorted() → 有序文件路径列表(长度 6)
2. 列表推导:文件名含 'dog' → 标签 1,否则 → 标签 0
3. ImageDataset(file_list, labels, transform):
     __getitem__(i):
         img = PIL.Image.open(file_list[i])     # 惰性:用到才读盘
         img = transform(img)                    # PIL → [0,1] 浮点张量(CHW) → 统一尺寸
         return img, labels[i]
4. DataLoader(image_dataset, batch_size=B, shuffle=True)
     → 每批图像张量 shape = (B, 3, 80, 120)
```

关于维度顺序:`transforms.ToTensor()` 把 PIL 图像(HWC:高×宽×通道)转成 PyTorch 约定的 **CHW**(通道×高×宽)张量,数值缩放到 [0,1];而 matplotlib 的 `imshow` 仍要求 HWC,所以书中可视化时做了 `.numpy().transpose((1, 2, 0))` 把通道维挪回最后。

#### 4.4.3 源码精读

**第一步:收集文件、查看原图**:

[ch12/ch12_part1.py:L349-L353](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L349-L353) —— `pathlib.Path('cat_dog_images')` 指向相对路径(因此脚本需在 `ch12` 目录下运行),`.glob('*.jpg')` 匹配全部 jpg,`sorted()` 保证顺序确定:cat-01、cat-02、cat-03、dog-01、dog-02、dog-03。

[ch12/ch12_part1.py:L360-L367](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L360-L367) —— 逐张 `Image.open(file)` 后 `np.array(img).shape` 打印每张图的原始形状,并排成 2×3 子图。这一步的用意是**暴露问题**:各图尺寸不一致,直接堆批会失败。

**第二步:从文件名推断标签**:

[ch12/ch12_part1.py:L376-L378](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L376-L378) —— 列表推导 `1 if 'dog' in os.path.basename(file) else 0`,得到 `[0, 0, 0, 1, 1, 1]`。这是最小可行的「标注」方案:类别信息编码在文件名里。

**第三步:版本一,返回路径字符串**:

[ch12/ch12_part1.py:L383-L398](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L383-L398) —— 第一个 `ImageDataset` 只在 `__getitem__` 里回传 `(文件路径, 标签)`,不做任何读取;`__len__` 返回 `len(self.labels)`。随后的 `for file, label in image_dataset:` 直接解包遍历。它演示了契约的宽容(返回什么都行),也暴露了局限——路径字符串无法进入神经网络。

**第四步:版本二,真正读图 + 变换**:

[ch12/ch12_part1.py:L404-L416](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L404-L416) —— 同名类重写:`__init__` 多收一个 `transform` 参数;`__getitem__` 中 `Image.open(self.file_list[index])` 现取现读,`if self.transform is not None:` 时应用变换(允许不传变换,保持灵活),最后返回 `(img, label)`。

[ch12/ch12_part1.py:L418-L425](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L418-L425) —— `img_height, img_width = 80, 120`;`transforms.Compose([transforms.ToTensor(), transforms.Resize((img_height, img_width))])` 把两步变换串成一个可调用对象:先转 [0,1] 的 CHW 浮点张量,再统一尺寸。然后 `image_dataset = ImageDataset(file_list, labels, transform)` 组装完成。

[ch12/ch12_part1.py:L430-L439](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L430-L439) —— 可视化每个样本:`example[0].numpy().transpose((1, 2, 0))` 把 CHW 转回 HWC 交给 `ax.imshow`,`example[1]` 作为标题。这是 PyTorch(CHW)与 matplotlib(HWC)两种约定的桥接,值得记住。

对比第 11 章:`ImageDataset` 之于 PyTorch,正如 `minibatch_generator` 之于 numpy——但前者把「文件从哪来、如何变成张量」封装进了 `__getitem__`,训练循环从此不关心数据细节。

#### 4.4.4 代码实践

1. **实践目标**:体验「不 transform 直接堆批会失败」,理解 transform 的必要性。
2. **操作步骤**:在 `ch12` 目录下运行:

```python
# 示例代码(基于书中 L383-L425)
import pathlib, os
import torch
from PIL import Image
from torch.utils.data import Dataset, DataLoader
import torchvision.transforms as transforms

file_list = sorted(str(p) for p in pathlib.Path('cat_dog_images').glob('*.jpg'))
labels = [1 if 'dog' in os.path.basename(f) else 0 for f in file_list]

class RawImageDataset(Dataset):                 # 故意不做 transform
    def __init__(self, file_list, labels):
        self.file_list, self.labels = file_list, labels
    def __len__(self):
        return len(self.labels)
    def __getitem__(self, idx):
        return Image.open(self.file_list[idx]), self.labels[idx]

ds = RawImageDataset(file_list, labels)
dl = DataLoader(ds, batch_size=2, shuffle=False)
for xb, yb in dl:
    print('批内图片尺寸:', [list(im.size) for im in xb], ' 标签:', yb.tolist())

# 再给 __getitem__ 末尾加上 ToTensor(去掉 Resize),单独观察尺寸问题:
class ToTensorOnlyDataset(RawImageDataset):
    def __getitem__(self, idx):
        img, label = Image.open(self.file_list[idx]), self.labels[idx]
        return transforms.ToTensor()(img), label

dl2 = DataLoader(ToTensorOnlyDataset(file_list, labels), batch_size=2, shuffle=False)
try:
    for xb, yb in dl2:
        print(xb.shape)
except Exception as e:
    print('拼批失败,报错类型:', type(e).__name__)
```

3. **需要观察的现象**:第一段遍历能逐批产出 PIL 图片对象(`xb` 是两个 PIL 图片组成的序列),打印可见**各图尺寸不同**;第二段只做 `ToTensor` 不做 `Resize`,PyTorch 会在拼批处报错——不同形状的张量无法堆叠(具体异常类型与信息随版本略有差异,待本地验证);换用书中 [L418-L423](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L418-L423) 的 `Compose` 变换(ToTensor + Resize)后一切正常。
4. **预期结果**:使用 [L418-L423](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L418-L423) 的 transform 后,`DataLoader(ds, batch_size=2)` 产出的图像批 shape 为 `torch.Size([2, 3, 80, 120])`。

#### 4.4.5 小练习与答案

**练习 1**:版本一返回路径字符串为什么也能正常工作?
**答案**:`Dataset` 契约只约定「给下标、还样本」,不约束样本类型;`DataLoader` 会照常把字符串堆成批(如 tuple/tensor of str)。只是字符串不是张量,无法参与矩阵运算,所以工程上必须在 `__getitem__` 内完成「读文件 + 转张量」。

**练习 2**:为什么 `transform` 设计成构造参数,而不是写死在 `__getitem__` 里?
**答案**:解耦数据与预处理——训练集可加随机翻转等增广、测试集只要确定性缩放;换模型输入尺寸时只改一处 transform,数据集类一行不动。这与 scikit-learn 把预处理做成 `Pipeline` 步骤(u6-l1)是同一思想。

**练习 3**:`ToTensor()` 之后的形状为什么是 `(3, 80, 120)` 而不是 `(80, 120, 3)`?
**答案**:PyTorch 的卷积层约定输入为「通道在前」(CHW),`ToTensor` 因此把 PIL 的 HWC 排布转置为 CHW 并把像素值除以 255 缩放到 [0,1];matplotlib 只认 HWC,所以书中 L434 需再 `transpose((1, 2, 0))` 转回去显示。

### 4.5 目录式官方数据集:torchvision.datasets(CelebA 与 MNIST)

#### 4.5.1 概念说明

自己下载、整理数据集繁琐易错,`torchvision.datasets` 为常用数据集提供了现成的 `Dataset` 实现:传个根目录(可选自动下载),返回的对象与 4.4 手写的 `ImageDataset` **遵守同一契约**,可以直接交给 `DataLoader`。

书中演示了两个:

- **CelebA**:约 20 万张名人人脸图 + 40 种属性标注。图片体积大,书中建议**手动下载** `img_align_celeba.zip` 与若干标注文本文件(身份、属性、边界框、关键点、train/valid/test 划分)放进 `./celeba` 子目录,再用 `download=False` 加载——这是「目录式数据集」的真实组织形态:图片文件 + 平行放置的标注文件。
- **MNIST**:手写数字(第 11 章的老朋友),`download=True` 自动下载。

顺带一个扩展知识(注意:**不是本书代码**,属于 torchvision 自带能力):`torchvision.datasets.ImageFolder` 支持「根目录/类别名/图片」的两级目录组织,类别即子目录名。本书此节没有使用它——`cat_dog_images` 是所有图片平铺一层、靠文件名区分类别,故配套的是手写 `ImageDataset`;下一册/后续章节用到的 CelebA 则是「图片 + 标注文件」形态。三者殊途同归:最终都实现 `Dataset` 协议。

#### 4.5.2 核心流程

```text
1. 准备数据(手动下载 CelebA 文件到 ./celeba,或 MNIST 自动下载)
2. dataset = torchvision.datasets.CelebA(路径, split='train', target_type='attr', download=False)
3. assert isinstance(dataset, torch.utils.data.Dataset)   # 协议检查
4. sample = next(iter(dataset))                           # 取第一个样本
5. 之后与 4.4 完全相同:DataLoader(dataset, batch_size=..., shuffle=True)
```

#### 4.5.3 源码精读

[ch12/ch12_part1.py:L442-L499](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L442-L499) —— 注释形态的下载指南:说明若 `download=True` 遇到 `BadZipfile` 错误,应从 CelebA 官网/网盘手动下载 `img_align_celeba.zip`,连同 `identity_CelebA.txt`、`list_attr_celeba.txt` 等标注文件与 `list_eval_partition.txt` 一起放进 `./celeba` 子目录,并逐一确认文件非空。这段「数据工程」在真实项目里往往比建模更耗时。

[ch12/ch12_part1.py:L503-L512](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L503-L512) —— `celeba_dataset = torchvision.datasets.CelebA(image_path, split='train', target_type='attr', download=False)`:取训练划分、目标类型为属性向量。紧接着 `assert isinstance(celeba_dataset, torch.utils.data.Dataset)` 显式断言它是 `Dataset` 的实例——官方数据集与手写类共用同一抽象。`example = next(iter(celeba_dataset))` 用「iter 转迭代器 + next 取首个」的最短写法偷看一个样本:CelebA 的样本是 `(PIL 图像, 40 维属性张量)`。

[ch12/ch12_part1.py:L517-L525](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L517-L525) —— `islice(enumerate(celeba_dataset), 18)` 只取前 18 个样本排成 3×6 网格,标题用 `attributes[31]`(第 31 号属性)——大数据集不必全遍历,`itertools.islice` 配合惰性取样即可抽样查看。

[ch12/ch12_part1.py:L530-L545](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L530-L545) —— `mnist_dataset = torchvision.datasets.MNIST(image_path, 'train', download=True)` 一行完成下载与包装,同样有 `assert isinstance(..., Dataset)`;`next(iter(mnist_dataset))` 取出的样本是 `(PIL 图像, 整数标签)`,随后抽 10 张用 `cmap='gray_r'` 反色灰度显示。第 11 章我们从本地 idx 文件手工解析 MNIST,这一行就是当时的全部工作量。

#### 4.5.4 代码实践

1. **实践目标**:确认官方数据集与手写 `Dataset` 在协议层面等价,可无缝混用。
2. **操作步骤**:

```python
# 示例代码(基于书中 L530-L535)
import torch, torchvision
import torchvision.transforms as transforms
from torch.utils.data import DataLoader

tf = transforms.ToTensor()
mnist = torchvision.datasets.MNIST('./', train=True, download=True, transform=tf)
assert isinstance(mnist, torch.utils.data.Dataset)      # 协议检查
img, label = next(iter(mnist))                           # 偷看第一个样本
print('图像类型:', type(img), '尺寸:', tuple(img.shape), ' 标签:', label)

dl = DataLoader(mnist, batch_size=32, shuffle=True)
x, y = next(iter(dl))                                    # 偷看第一个批次
print('批形状:', tuple(x.shape), ' 标签批:', tuple(y.shape))

# 对照实验:去掉 transform 直接堆批,会发生什么?
mnist_raw = torchvision.datasets.MNIST('./', train=True, download=True)
try:
    next(iter(DataLoader(mnist_raw, batch_size=32, shuffle=True)))
except Exception as e:
    print('不传 transform 时拼批失败,报错类型:', type(e).__name__)
```

3. **需要观察的现象**:传了 `transform=ToTensor()` 后,`DataLoader` 对官方数据集自动完成「取样本 → 变换 → 堆批」,`x` 是浮点张量批;对照实验里不传 transform 时,样本是 PIL 图片,`DataLoader` 默认的拼批逻辑无法把 PIL 对象堆成张量,会在取批时抛错(具体异常信息随版本略有差异,待本地验证)——这印证了 4.4 的结论:变换必须发生在 `__getitem__` 内(官方数据集用 `transform` 参数达成同样效果)。
4. **预期结果**:`img` 形状 `(1, 28, 28)`(ToTensor 后的 CHW 灰度图),`label` 为 0~9 的整数;批形状 `(32, 1, 28, 28)`,标签批形状 `(32,)`。(批内容因 shuffle 随机,待本地验证;MNIST 首次运行会下载数据文件。)

#### 4.5.5 小练习与答案

**练习 1**:`assert isinstance(celeba_dataset, torch.utils.data.Dataset)` 这行的作用是什么?
**答案**:程序化确认官方数据集实现了 `Dataset` 抽象,因此 4.2~4.4 学到的所有用法(遍历、`next(iter(...))`、交给 `DataLoader`)对它同样成立;失败则立即报错,而不是等到训练中途才莫名崩溃。

**练习 2**:`next(iter(dataset))` 为什么能取出第一个样本?
**答案**:`iter()` 把可迭代对象转成迭代器,其取数方式是反复调用 `__getitem__`(从下标 0 开始);`next()` 执行第一次取出,等价于 `dataset[0]`,但写法对任意 `Dataset` 通用。

**练习 3**:书中为什么建议 CelebA 手动下载而不是 `download=True`?
**答案**:CelebA 托管在网盘上,程序化下载常因配额/链接失效抛 `BadZipfile` 等错误;手动下载 `img_align_celeba.zip` 与标注文件、放入 `./celeba` 后以 `download=False` 加载更可靠——这也提醒我们输入管线的第一步往往是「把数据放到正确的目录结构里」。

## 5. 综合实践

**任务:实现完整的 `CatDogDataset` 并跑通「目录 → Dataset → DataLoader → 批」全管线。**

把本讲四个模块串起来:文件收集(4.4)→ 标签推断(4.4)→ 契约实现(4.2)→ 洗牌与切批(4.3)。在 `ch12` 目录下保存为任意 `.py` 文件或直接在 notebook 运行:

```python
# 示例代码:综合实践(仿照书中 L349-L425 的完整管线)
import pathlib, os
import torch
from PIL import Image
from torch.utils.data import Dataset, DataLoader
import torchvision.transforms as transforms

# ── 1. 收集文件并推断标签(对应书中 L349-L378)──
imgdir = pathlib.Path('cat_dog_images')
file_list = sorted(str(p) for p in imgdir.glob('*.jpg'))      # 不做去重,直接取 glob 结果
labels = [1 if 'dog' in os.path.basename(f) else 0 for f in file_list]

# ── 2. 自定义 Dataset:__len__ + __getitem__,读图并变换(对应 L404-L416)──
class CatDogDataset(Dataset):
    def __init__(self, file_list, labels, transform=None):
        self.file_list = file_list
        self.labels = labels
        self.transform = transform
    def __len__(self):
        return len(self.labels)
    def __getitem__(self, index):
        img = Image.open(self.file_list[index])   # 惰性读盘:被取到才打开
        if self.transform is not None:
            img = self.transform(img)
        return img, self.labels[index]

# ── 3. 变换 + 组装(对应 L418-L425)──
tf = transforms.Compose([transforms.ToTensor(),
                         transforms.Resize((80, 120))])
ds = CatDogDataset(file_list, labels, transform=tf)

# ── 4. DataLoader 遍历一轮,打印每个批的形状 ──
torch.manual_seed(1)
dl = DataLoader(ds, batch_size=2, shuffle=True, drop_last=False)
print('数据集大小:', len(ds), ' 预期批数:', len(dl))
for i, (img_batch, label_batch) in enumerate(dl, 1):
    print(f'batch {i}: 图像批 shape={tuple(img_batch.shape)}, '
          f'标签批={label_batch.tolist()}')

# ── 5. 进阶观察:两个 epoch 的洗牌差异 + drop_last 对比 ──
for epoch in range(2):
    print(f'epoch {epoch+1}:',
          [yb.tolist() for _, yb in dl])                # 顺序应随 epoch 改变
dl_drop = DataLoader(ds, batch_size=4, shuffle=False, drop_last=True)
print('batch_size=4, drop_last=True 批数:', len(dl_drop))  # 6/4 → 1 批
```

**验收清单**(预期结果,具体打印顺序待本地验证):

- `len(ds)` 为 6,`len(dl)` 为 3(6 个样本、batch_size=2);
- 每个 `img_batch` 形状 `(2, 3, 80, 120)`——batch 维 × 通道 × 高 × 宽,`label_batch` 形如 `[0, 1]`;
- 两个 epoch 打出的标签顺序不同(shuffle 生效),但每行内部标签与图像的配对关系不变;
- `batch_size=4, drop_last=True` 时批数从 \(\lceil 6/4 \rceil = 2\) 变为 \(\lfloor 6/4 \rfloor = 1\);
- 把 `tf` 中的 `Resize` 去掉后重跑,应在拼批处报错(六图尺寸不一,无法堆叠)——这一步反过来证明 transform 不可省。

完成后,你就拥有了一条与第 13~15 章训练代码同构的输入管线:届时只需把 `CatDogDataset` 换成对应数据集,训练循环一字不改。

## 6. 本讲小结

- `Dataset` 契约极简:`__len__` 报样本数、`__getitem__` 按下标返回单样本(通常是 `(x, y)` 元组);`torch.utils.data.Dataset` 基类只负责声明契约,官方 `TensorDataset` 是「两个张量按下标绑定」的现成实现。
- `DataLoader` 是 `Dataset` 的批处理外壳:自动加 batch 维、把元组各位置分别堆叠;`batch_size`/`shuffle`/`drop_last` 分别控制批大小、每 epoch 洗牌、是否丢弃不足额尾批,批数公式为 \(\lceil N/B \rceil\) 与 \(\lfloor N/B \rfloor\)。
- 同一个 `DataLoader` 可跨 epoch 反复迭代(不等同于一次性生成器),`shuffle=True` 时每轮重新洗牌,样本配对永不拆散;按类排序的数据必须洗牌,否则梯度周期性震荡。
- 磁盘数据集的正确姿势:只存路径与标签,`__getitem__` 被调用时才用 PIL 打开图片,内存占用只与批大小相关;`transforms.Compose([ToTensor(), Resize(...)])` 负责「PIL → [0,1] CHW 张量 → 统一尺寸」,尺寸不统一就无法堆批。
- `torchvision.datasets`(CelebA、MNIST)与手写 `ImageDataset` 实现同一协议(`assert isinstance(..., Dataset)` 可验证),`next(iter(ds))` 是偷看单个样本的惯用写法;CHW 是 PyTorch 的约定,给 matplotlib 显示需 `transpose((1, 2, 0))` 转回 HWC。

## 7. 下一步学习建议

数据会喂了,下一讲 **u12-l3:nn.Module、autograd 与 XOR 训练**(源码 `ch12/ch12_part2.py`)解决「谁来吃」的问题:用 `nn.Sequential` 搭建网络、用 `loss.backward()` 自动求梯度(替代第 11 章手写的 `backward`)、`zero_grad/backward/step` 三步训练循环,以及 `torch.save/torch.load` 保存模型。届时 `DataLoader` 产出的 `(x_batch, y_batch)` 将直接进入 `model(xb)` 与损失函数。建议阅读顺序:`ch12_part2.py` 中 XOR 数据构造 → `Model` 类 → 训练循环;并可回头对比 `ch11.py` 的 `NeuralNetMLP`,体会「手写梯度 → autograd」的减负幅度。
