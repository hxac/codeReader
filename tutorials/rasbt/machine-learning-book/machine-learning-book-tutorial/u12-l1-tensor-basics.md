# u12-l1 PyTorch 张量基础

## 1. 本讲目标

第 11 章我们用纯 numpy 手写了一个完整的 MLP：自己定义权重矩阵、自己推反向传播、自己写 minibatch 循环。从第 12 章开始，我们换用 PyTorch 重做这些事。而 PyTorch 的一切都建立在一个数据结构上——**张量（tensor）**。本讲聚焦 `ch12/ch12_part1.py` 的「First steps with PyTorch」部分，学完后你应该能够：

1. 用 `torch.tensor`、`torch.from_numpy`、`torch.ones`、`torch.rand` 四种方式创建张量，并说出 `torch.tensor`（复制数据）与 `torch.from_numpy`(共享内存）的差别。
2. 读懂张量的三大属性：`shape`、`dtype`、`device`，理解「PyTorch 张量是 ndarray 的 GPU 加速版」这一定位。
3. 掌握 `to()` 的两种用法：`to(dtype)` 换数据类型（本章代码）、`to(device)` 在 CPU/GPU 间搬数据（后续章节），并理解跨设备搬运是**复制**语义。
4. 熟练进行形状变换：`transpose`（交换维度）、`reshape`（重排元素）、`squeeze`（删长度 1 的维度），并像第 11 章那样全程追踪形状。
5. 区分逐元素运算（`multiply`，支持广播）、归约统计（`mean`）、矩阵乘（`matmul`）与范数（`linalg.norm`）。
6. 区分切分（`chunk`/`split`）与拼接（`cat` 沿已有维度、`stack` 沿新维度）——这是下一讲 DataLoader 切 batch 的概念基础。

## 2. 前置知识

本讲假设你已学完 u11-l1/u11-l2（numpy 手写 MLP）。需要回顾的概念：

- **ndarray**：numpy 的多维数组。第 11 章里 `X_train`、`weight_h`、`a_h` 全是 ndarray；矩阵乘法形状规则 `(n, k) · (k, m) → (n, m)` 在本讲继续反复出现。
- **广播（broadcasting）**：u11-l1 讲过，`forward` 里 `(n, hidden)` 的净输入加 `(hidden,)` 的偏置时，偏置被自动「复制」到每一行。本讲会正式梳理广播规则。
- **dtype**：数据在内存里的类型。u1-l2 的 `python_environment_check.py` 检查过 numpy 版本；numpy 数组有 `int32`/`float64` 等 dtype，张量也有对应的 `torch.int32`/`torch.float64` 等。
- **向量范数**：u2 系列的权重、u11 的梯度都是向量，常需要度量它们的长度（例如 L2 正则化），本讲会算 L2 范数。

一个先建立的全局图景：**张量 ≈ ndarray + 存放位置（device）+ 自动微分（requires_grad）**。本讲只动用前两项；第三项（自动微分）是第 13 章的主角。本章代码推荐的 torch 最低版本是 1.9.0（见 [ch12/ch12_part1.py:L37-L42](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L37-L42) 的版本检查字典），notebook 实际运行在 1.10.0 上。

另外注意：`ch12_part1.py` 是 notebook 的脚本转换产物（u1-l1 讲过的双轨制），它本身**不含输出**；本讲引用的所有打印结果都来自 [ch12/ch12_part1.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.ipynb) 中保存的真实输出。想交互式跑这些代码，用 notebook；想按行号读代码，用 `.py`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ch12/ch12_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py) | 第 12 章前半部分的脚本版。L104-L267 是本讲主读的「创建张量 / 类型与形状 / 数学运算 / 切分组合」四节；L270 以后是下一讲的输入管线内容 |
| [ch12/ch12_part1.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.ipynb) | 与 `.py` 等价的 notebook，保留了每个单元格的真实输出，本讲引用的输出均出自此处 |
| [ch13/ch13_part4_ignite.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py) | 前向指针：第 13 章示范了 `to(device)` 把模型搬到 GPU 的写法（L152-L154），本讲 4.2 节会引用 |
| [ch12/ch12_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py) | 下一讲（u12-l3）主读文件：`nn.Module` 与训练循环，本讲只作路标 |

## 4. 核心概念与源码讲解

### 4.1 torch.tensor 创建与张量属性

#### 4.1.1 概念说明

**张量（tensor）**就是 PyTorch 世界里的多维数组：0 维是标量、1 维是向量、2 维是矩阵，更高维就是「矩阵的堆叠」（比如一批彩色图片是 `(批, 通道, 高, 宽)` 的 4 维张量）。它与 numpy ndarray 的关系可以这样理解：

| | numpy ndarray | torch.Tensor |
|---|---|---|
| 多维容器、索引、切片、形状 | ✅ | ✅ 几乎同构 |
| 向量化运算、广播 | ✅ | ✅ 同一套规则 |
| 记录存放设备（CPU/GPU） | ❌ 只能在 CPU | ✅ `.is_cuda`、`.device` |
| 自动微分（记录计算图） | ❌ | ✅ `requires_grad`（第 13 章） |

所以书里说 PyTorch 张量可以看作「ndarray 的 GPU 加速版」：你从第 2 章到第 11 章练出的所有 numpy 直觉（形状追踪、广播、矩阵乘）都可以直接迁移，只是换了前缀。

创建张量有三条主要路径，语义关键差别在于**是否复制数据**：

1. `torch.tensor(数据)`：把 Python 列表或 numpy 数组**复制**一份放进新张量，之后互不影响。
2. `torch.from_numpy(数组)`：与原 numpy 数组**共享同一段内存**（零拷贝），改一边另一边跟着变；反向的 `.numpy()` 同样共享内存。
3. 工厂函数 `torch.ones`、`torch.zeros`、`torch.rand`：直接生成指定形状的新张量，不依赖已有数据。

张量的三个核心属性：

- `.shape`：形状，类型是 `torch.Size`（本质是元组，可以像元组一样解包、比较）。
- `.dtype`：元素类型，如 `torch.int64`、`torch.float32`。类型推断规则：Python 整数列表默认推断为 `int64`，浮点默认 `float32`（注意不是 numpy 的 `float64`）；从 numpy 数组转换则**保留原 dtype**。
- `.device`：存放设备（本讲都在 CPU，4.2 节展开）。

#### 4.1.2 核心流程

```
创建:  python list --torch.tensor--> 张量(复制, int64)
       ndarray    --from_numpy----> 张量(共享内存, 保留dtype)
       形状       --ones/zeros/rand--> 新张量(float32)
检查:  torch.is_tensor(x) 判断是否为张量
属性:  t.shape / t.dtype / t.device
```

#### 4.1.3 源码精读

[ch12/ch12_part1.py:L108-L115](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L108-L115) —— 同一段代码演示两条创建路径：`a` 是 Python 列表，`b` 是 dtype 为 `int32` 的 numpy 数组：

```python
a = [1, 2, 3]
b = np.array([4, 5, 6], dtype=np.int32)

t_a = torch.tensor(a)      # 复制数据，推断为 int64
t_b = torch.from_numpy(b)  # 共享内存，保留 int32

print(t_a)   # tensor([1, 2, 3])
print(t_b)   # tensor([4, 5, 6], dtype=torch.int32)
```

注意打印结果的差异：`t_b` 比 `t_a` 多了一行 `dtype=torch.int32`——PyTorch 只在 dtype **不是默认值**时才显式标注，这正好印证了 `t_a` 用的是默认的 `int64`。

[ch12/ch12_part1.py:L120](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L120) —— 用 `torch.is_tensor` 区分「张量」与「普通对象」，列表 `a` 不是张量、`t_a` 是，输出 `(False, True)`：

```python
torch.is_tensor(a), torch.is_tensor(t_a)
```

[ch12/ch12_part1.py:L125-L139](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L125-L139) —— 工厂函数创建：`ones(2, 3)` 生成全 1 的 \(2 \times 3\) 矩阵（dtype 为 `float32`，所以打印成 `1.`），`rand(2, 3)` 生成同形状的均匀随机矩阵。`.shape` 返回 `torch.Size([2, 3])`：

```python
t_ones = torch.ones(2, 3)
t_ones.shape          # torch.Size([2, 3])
print(t_ones)         # tensor([[1., 1., 1.], [1., 1., 1.]])

rand_tensor = torch.rand(2, 3)
```

[ch12/ch12_part1.py:L95-L97](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L95-L97) —— 一个小但实用的细节：`np.set_printoptions(precision=3)` 把 numpy 数组打印精度限制为 3 位小数。这就是后文 numpy 输出 `0.678` 而张量输出 `0.6785` 的原因——两者数值相同，只是打印格式不同。

#### 4.1.4 代码实践

**实践：验证「复制 vs 共享内存」**（示例代码，可在任意装好 torch 的环境运行）：

1. 实践目标：亲手确认 `torch.tensor` 复制数据、`torch.from_numpy` 共享内存。
2. 操作步骤：

```python
import numpy as np
import torch

b = np.array([4, 5, 6], dtype=np.int32)
t_copy = torch.tensor(b)      # 复制
t_share = torch.from_numpy(b) # 共享

b[0] = 99                     # 修改原 numpy 数组
print(t_copy)     # tensor([4, 5, 6], dtype=torch.int32) —— 不受影响
print(t_share)    # tensor([99, 5, 6], dtype=torch.int32) —— 跟着变了

t_share[1] = -1              # 反向修改共享张量
print(b)          # [99 -1  6] —— numpy 侧也变了
print(t_copy.dtype, t_share.dtype)  # torch.int32 torch.int32
```

3. 需要观察的现象：改 `b` 之后 `t_share` 同步变化而 `t_copy` 不变；改 `t_share` 之后 `b` 也变化。
4. 预期结果：如注释所示。共享内存省了复制开销，但也意味着「改一边动两边」，混用时要多留个心眼。
5. 以上输出为依据共享内存语义的推导，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`torch.tensor([1.0, 2.0])` 与 `torch.from_numpy(np.array([1.0, 2.0]))` 的 dtype 分别是什么？

**答案**：前者是 `torch.float32`（Python 浮点默认推断为 float32）；后者是 `torch.float64`（numpy 的浮点默认是 float64，`from_numpy` 原样保留）。dtype 不一致是深度学习中最常见的报错来源之一（比如矩阵乘两个输入一个 float32 一个 float64 会直接报错），创建张量时最好显式写 `dtype=torch.float32`。

**练习 2**：为什么说 `torch.tensor(a)` 之后修改 `a` 是安全的，而 `torch.from_numpy(b)` 之后随意修改 `b` 有风险？

**答案**：`torch.tensor` 把数据复制进张量的独立内存，两者再无关联；`from_numpy` 是零拷贝视图，两个对象指向同一段内存，任何一侧的就地修改都会反映到另一侧。风险在于：训练代码若还持有原数组的引用并原地更新它（比如数据增强、打乱），会「不知不觉」改变张量内容，造成难以排查的 bug。

**练习 3**：`torch.is_tensor` 的输出 `(False, True)` 里，为什么第一个是 `False`？

**答案**：第一个对象是 Python 列表 `a = [1, 2, 3]`，它没有 shape/dtype/device 属性、不能参与张量运算，只是普通容器；只有经过 `torch.tensor` 包装成张量后（第二个对象 `t_a`）才算。这提示我们：进入 PyTorch 世界前，数据必须先「张量化」。

### 4.2 dtype 与形状变换：to()、transpose、reshape、squeeze

#### 4.2.1 概念说明

深度学习代码的一半 bug 来自「形状不对」，另一半来自「类型不对」。本模块解决这两件事。

**类型变换：`to()` 的重载与链式调用。** `to()` 是个多面手，能接两种参数：

- `t.to(dtype)`：转换数据类型，如 `t.to(torch.int64)`、`t.to(torch.float32)`。返回**新张量**（`to` 不做就地修改），这也是它能链式调用的原因——每次调用都返回一个张量，可以接着 `.to(...)` 下去，例如先搬设备再换类型：`t.to(device).to(torch.float64)`。
- `t.to(device)`：把张量搬到指定设备（CPU 或 GPU）。这是本章代码没有直接用到、但第 13 章立刻要用的用法，见下文。

补充一个 PyTorch 通用 API（仓库代码未直接使用）：无参调用 `t.type()` 返回类型描述字符串（如 `'torch.LongTensor'`），带参调用 `t.type(dtype)` 等价于类型转换——可以把它理解为 `to(dtype)` 的旧式写法。

**GPU 复制语义**：CPU 张量调用 `.to('cuda')` 时，数据必须**复制**到显存里（两段内存在两块物理芯片上，不可能共享），原 CPU 张量不变；GPU 张量想转回 numpy 必须先 `.cpu()` 搬回来再 `.numpy()`（`.numpy()` 只对 CPU 张量可用）。所以「张量在设备间的搬运」天然是复制语义，与 4.1 节同一设备内的零拷贝共享（`from_numpy`）形成对比。习惯写法是：

```python
device = "cuda" if torch.cuda.is_available() else "cpu"
model = model.to(device)
```

**形状变换三件套**（都不改变元素总数与数值，只改变「怎么排」）：

- `torch.transpose(t, 0, 1)`：交换维度 0 和维度 1，`(3, 5) → (5, 3)`。第 11 章的 `weight_h.T` 在 PyTorch 里就是 `transpose(w, 0, 1)` 或 `w.T`。
- `t.reshape(5, 6)`：按行优先顺序把 30 个元素重新装进 \(5 \times 6\) 的形状。与 transpose 的关键区别：transpose 只交换维度含义（第 i 行变第 i 列），reshape 是把元素**摊平再重排**，两者即使得到相同形状，内容排布也不同（综合实践里会让你亲眼对比）。
- `t.squeeze(dim)`：删掉指定位置上长度为 1 的维度（不传 dim 则删掉所有长度 1 的维度）。它解决的是「形状里混进了无意义的 1 维」的问题，比如 `(1, 2, 1, 4, 1)` 这种从广播或额外索引产生的「皮骨」维度。

#### 4.2.2 核心流程

```
类型:  t --to(dtype)--> 新张量(类型变了, 值尽量保序)     [本章用法]
      t --to(device)--> 新位置张量(跨设备=复制)          [第13章用法]
      链式: t.to(device).to(torch.float64)

形状:  (3,5) --transpose(t,0,1)--> (5,3)   交换维度, 元素跟着维度走
      30    --reshape(5,6)------> (5,6)    摊平重排, 只要求总数相等
      (1,2,1,4,1) --squeeze(t,2)--> (1,2,4,1)  删除指定位置长度1的维度
```

矩阵乘的形状规则继续有效：\[ (n \times k) \cdot (k \times m) \rightarrow (n \times m) \] 转置常用来把形状「对齐」成可乘的形式。

#### 4.2.3 源码精读

[ch12/ch12_part1.py:L146-L148](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L146-L148) —— `to(torch.int64)` 把 `t_a`（原 int64，此处演示语法）转成 int64，并读取 `.dtype` 验证。`to` 返回新张量，赋值给 `t_a_new`：

```python
t_a_new = t_a.to(torch.int64)
print(t_a_new.dtype)   # torch.int64
```

在实际训练代码里这个方向更常见的用法是把整数标签张量转成浮点（或反过来），比如下一讲你会看到 [ch12/ch12_part2.py:L99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L99) 的 `torch.from_numpy(y_train).float()`——`.float()` 是 `to(torch.float32)` 的快捷方式。

[ch12/ch12_part1.py:L153-L156](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L153-L156) —— 转置：`torch.transpose(t, 0, 1)` 交换第 0、1 维，`torch.Size([3, 5]) --> torch.Size([5, 3])`：

```python
t = torch.rand(3, 5)
t_tr = torch.transpose(t, 0, 1)
print(t.shape, ' --> ', t_tr.shape)
```

[ch12/ch12_part1.py:L161-L165](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L161-L165) —— reshape：30 个 0 重排成 \(5 \times 6\)，输出 `torch.Size([5, 6])`。唯一硬性约束是新旧形状元素总数相等（\(5 \times 6 = 30\)）：

```python
t = torch.zeros(30)
t_reshape = t.reshape(5, 6)
```

[ch12/ch12_part1.py:L170-L174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L170-L174) —— squeeze：形状 `(1, 2, 1, 4, 1)` 的张量，指定删除 2 号位（从 0 数）那个长度为 1 的维度，得到 `(1, 2, 4, 1)`。注意其余两个长度 1 的维度（0 号、4 号位）因为没被点名而保留：

```python
t = torch.zeros(1, 2, 1, 4, 1)
t_sqz = torch.squeeze(t, 2)
print(t.shape, ' --> ', t_sqz.shape)  # torch.Size([1, 2, 1, 4, 1]) --> torch.Size([1, 2, 4, 1])
```

前向指针——设备搬运的正式写法在 [ch13/ch13_part4_ignite.py:L152-L154](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L152-L154)：先探测 GPU 是否可用得到设备字符串，再把整个模型搬到该设备。张量搬运用完全相同的 `to()`，例如 `t = t.to(device)`：

```python
device = "cuda" if torch.cuda.is_available() else "cpu"
model = get_model().to(device)
```

#### 4.2.4 代码实践

**实践：transpose 与 reshape 同形状、不同排布**（示例代码）：

1. 实践目标：证明 transpose 和 reshape 即使得到相同形状，元素排布也不同；同时体验 `to(dtype)` 返回新对象。
2. 操作步骤：

```python
import torch

t = torch.arange(6).reshape(2, 3)   # [[0,1,2],[3,4,5]]
print(torch.transpose(t, 0, 1))     # [[0,3],[1,4],[2,5]]  行列互换
print(t.reshape(3, 2))              # [[0,1],[2,3],[4,5]]  摊平后按行重填

t2 = t.to(torch.float32)
print(t.dtype, t2.dtype)            # torch.int64 torch.float32
print(t is t2)                      # False —— to 产生新张量

print(torch.zeros(1, 3, 1).squeeze().shape)  # torch.Size([3]) 不指定 dim 删掉全部单维
```

3. 需要观察的现象：两个 `(3, 2)` 结果的内容完全不同；`t is t2` 为 `False`；无参 squeeze 删掉所有长度 1 的维度。
4. 预期结果：如注释所示。reshape 是「先摊平再按行填」，transpose 是「行列互换」，二者不可混用。
5. 以上输出为依据规则的推导，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`torch.rand(4, 5)` 的张量执行 `reshape(5, 4)` 后，`(0, 3)` 位置的元素原来在哪里？

**答案**：`reshape(5, 4)` 把原张量按行摊平成 20 个元素再按行填入新形状。新位置 `(0, 3)` 是摊平序列的第 \(0 \times 4 + 3 = 3\) 个元素（从 0 数），对应原张量摊平序列的第 3 个，即原位置 `(0, 3)`——恰好相同纯属位置巧合；换成新位置 `(1, 0)`（摊平序号 4）就对应原位置 `(0, 4)` 了。可见 reshape 只保「摊平顺序」不变。

**练习 2**：`squeeze(t, 2)` 为什么不把 `(1, 2, 1, 4, 1)` 里其它两个长度 1 的维度也删掉？

**答案**：指定了 `dim=2` 就只删 2 号位。`squeeze` 的设计哲学是「只删你明确指出的（或显然多余的）维度」，避免意外删除有语义的维度——例如批维度 `(1, 特征数)` 里的 1 可能表示 batch size 恰好为 1，误删会破坏后续广播与矩阵乘。想要「全部删光」就调用无参 `squeeze()`。

**练习 3**：以下两行都合法吗？分别改变的是什么？

```python
x = t.to(torch.float64)
y = t.to('cuda')     # 假设有 GPU
```

**答案**：都合法。第一行改 **dtype**：数值类型转换，张量仍在 CPU；第二行改 **device**：把数据复制到 GPU 显存，dtype 不变。两者都是复制式操作、都返回新张量，因此都能链式调用（`t.to('cuda').to(torch.float64)`）。这也是「先统一搬设备、再统一类型」能写成一行流水线的原因。

### 4.3 逐元素运算、广播与统计运算

#### 4.3.1 概念说明

张量运算按「结果的形状」分三类，这是本模块的主线：

1. **逐元素运算（element-wise）**：`torch.multiply(t1, t2)`（即 `t1 * t2`）、加、减、除，在相同位置的两两元素上独立计算，结果形状不变。当两个操作数形状**不同但兼容**时触发**广播**：从最后一个维度向前对齐，某维度长度为 1 或缺失的一方被「虚拟复制」扩展到与另一方相同。本章源码里 `2 * torch.rand(5, 2) - 1` 就是标量广播：标量 2 和 1 被扩展成 `(5, 2)` 参与运算（等价于 numpy 的写法）。
2. **归约运算（reduction）**：`torch.mean(t, axis=0)`、`sum`、`max` 等，沿指定维度压缩求统计量，该维度在结果中消失：`(5, 2)` 沿 `axis=0` 求均值得到 `(2,)`——即「每列的平均」。u11-l2 的 `mse_loss` 在 numpy 里做的 `np.mean((y - probas)**2)` 就是同款归约。
3. **矩阵乘（matrix multiplication）**：`torch.matmul(A, B)`，遵循 \((n \times k) \cdot (k \times m) \rightarrow (n \times m)\)，中间维度必须相消。它与逐元素乘是两码事：`t1 * t2` 要求形状完全相同（或可广播），`matmul` 要求相邻维度匹配。第 11 章前向传播的 `X @ W.T` 在 PyTorch 里就是一次 matmul。

再加一个常用统计量——**L2 范数**，对每行向量 \(x\) 计算：

\[ \|x\|_2 = \sqrt{\sum_i x_i^2} \]

`torch.linalg.norm(t, ord=2, dim=1)` 沿第 1 维对每行求范数。它在后续章节反复出现（权重衰减正则化、梯度裁剪），这里先用 numpy 交叉验证一遍 PyTorch 的计算。

#### 4.3.2 核心流程

```
准备:  manual_seed(1) 固定随机种子 (对应 u2-l3 讲过的可复现性)
      t1 = 2 * rand(5,2) - 1     # 标量广播: rand∈[0,1) 线性映射到 [-1,1)
      t2 = normal(0, 1, (5,2))   # 正态随机

逐元素: multiply(t1, t2)          (5,2) -> (5,2)
归约:   mean(t1, axis=0)          (5,2) -> (2,)   每列均值
矩阵乘: matmul(t1, transpose(t2)) (5,2)·(2,5) -> (5,5)   逐行配对
        matmul(transpose(t1), t2) (2,5)·(5,2) -> (2,2)   逐列配对
范数:   linalg.norm(t1, 2, dim=1) (5,2) -> (5,)   每行的L2长度
```

注意两个 matmul 的对比：转置放在**不同的张量**上，得到完全不同的结果形状——`(5,5)` 是「t1 的 5 行与 t2 的 5 行两两做内积」，`(2,2)` 是「两个张量按 2 个特征维做内积」。第 11 章读权重矩阵时练的形状心算，在这里就是全部门槛。

#### 4.3.3 源码精读

[ch12/ch12_part1.py:L181-L184](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L181-L184) —— 固定随机种子后构造两个 `(5, 2)` 随机张量。`t1` 的构造式里藏着两次广播（标量 2 乘、标量 1 减），把 `[0,1)` 均匀随机数线性映射到 `[-1, 1)`：

```python
torch.manual_seed(1)

t1 = 2 * torch.rand(5, 2) - 1
t2 = torch.normal(mean=0, std=1, size=(5, 2))
```

[ch12/ch12_part1.py:L189-L190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L189-L190) —— 逐元素乘：相同形状 `(5,2)` 位置对位置相乘，notebook 中的真实输出（保留 4 位小数）：

```python
t3 = torch.multiply(t1, t2)
print(t3)
# tensor([[ 0.4426, -0.3114],
#         [ 0.0660, -0.5970],
#         ...
```

[ch12/ch12_part1.py:L195-L196](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L195-L196) —— 归约：沿 `axis=0`（行方向）求均值，5 行压成 1 行，`(5,2) → (2,)`：

```python
t4 = torch.mean(t1, axis=0)
print(t4)   # tensor([-0.1373,  0.2028]) —— 两列各自的均值
```

[ch12/ch12_part1.py:L201-L210](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L201-L210) —— 两种方向的矩阵乘。`t5 = matmul(t1, transpose(t2))` 是 \((5,2)\cdot(2,5)\to(5,5)\)；`t6 = matmul(transpose(t1), t2)` 是 \((2,5)\cdot(5,2)\to(2,2)\。转置在这里的职责就是把形状「转」成可乘：

```python
t5 = torch.matmul(t1, torch.transpose(t2, 0, 1))   # (5, 5)
t6 = torch.matmul(torch.transpose(t1, 0, 1), t2)   # (2, 2)
```

[ch12/ch12_part1.py:L215-L222](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L215-L222) —— L2 范数与 numpy 交叉验证：先用 `torch.linalg.norm(t1, ord=2, dim=1)` 对每行求范数，再用 `t1.numpy()` 转回 numpy 手工实现 \(\sqrt{\sum x_i^2}\)。两侧数值一致（`0.6785` vs 打印精度截断后的 `0.678`，差异只是 4.1 节说的打印精度）：

```python
norm_t1 = torch.linalg.norm(t1, ord=2, dim=1)
print(norm_t1)    # tensor([0.6785, 0.5078, 1.1162, 0.5488, 0.1853])

np.sqrt(np.sum(np.square(t1.numpy()), axis=1))
# array([0.678, 0.508, 1.116, 0.549, 0.185], dtype=float32)
```

这一段是全讲的缩影：**同一套数学，numpy 和 PyTorch 各写一遍，结果一致**——这正是「张量是 ndarray 的 GPU 加速版」的实证。

#### 4.3.4 代码实践

**实践：手算验证广播与 matmul**（示例代码）：

1. 实践目标：不靠运行，先在纸上算出结果，再用代码验证形状直觉。
2. 操作步骤：

```python
import torch

torch.manual_seed(0)
t1 = torch.tensor([[1., 2.], [3., 4.]])   # (2, 2)
row = torch.tensor([10., 20.])            # (2,)

print(t1 + row)          # 广播: row 复制到每一行 -> [[11.,22.],[13.,24.]]
print(t1 * 2 - 1)        # 标量广播: [[1,3],[5,7]]
print(torch.mean(t1, axis=1))             # (2,) 每行均值 -> [1.5, 3.5]
print(torch.matmul(t1, torch.transpose(t1, 0, 1)).shape)  # (2,2)·(2,2) -> (2,2)
print(torch.multiply(t1, t1))             # 逐元素平方 -> [[1,4],[9,16]]
```

3. 需要观察的现象：`(2,)` 加 `(2,2)` 成功广播；`mean(axis=1)` 与 `mean(axis=0)` 方向不同；逐元素乘与 matmul 的区别。
4. 预期结果：如注释所示（元素值是确定性的，可先手算再核对）。
5. 以上输出为依据广播规则的手算推导，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：形状 `(4, 3)` 与 `(3,)` 的张量相加会发生什么？`(4, 3)` 与 `(4,)` 相加呢？

**答案**：前者成功：从尾维对齐，`(3,)` 缺失的行维被扩展成 4 行，等价于每行加同一个 `(3,)` 向量（u11-l1 里加偏置正是这个模式）。后者报错：尾维 3 ≠ 4 且都不为 1，无法广播。记忆口诀：** broadcasting 永远从最后一个维度向前对齐**。

**练习 2**：`torch.matmul(A, B)` 中 `A` 形状 `(3, 4)`，`B` 形状 `(4, 5)`，结果形状是什么？若改成 `A @ B.T` 呢？

**答案**：前者 \((3,4)\cdot(4,5)\to(3,5)\)。后者 `B.T` 形状为 `(5, 4)`，`(3,4)\cdot(5,4)` 相邻维度 4 ≠ 5，直接报错。想要可乘的组合只有 `A @ B`（得 `(3,5)`）与 `B.T @ A.T`（\((5,4)\cdot(4,3)\to(5,3)\)）；而 `A.T @ B` 是 \((4,3)\cdot(4,5)\)，同样 3 ≠ 4 不可乘。这题的教训：转置插入的位置决定了中间维度能否相消。

**练习 3**：为什么 `2 * torch.rand(5, 2) - 1` 能把均匀随机数从 `[0, 1)` 变到 `[-1, 1)`？

**答案**：设 \(u \in [0,1)\)，则 \(2u - 1 \in [-1, 1)\)，是线性变换。计算时标量 2 与 1 分别广播成 `(5,2)` 参与逐元素运算。这个小技巧在初始化权重（对称区间小随机数，回忆 u11-l1 的对称性问题）时经常出现。

### 4.4 切分与组合：chunk、split、cat、stack

#### 4.4.1 概念说明

训练神经网络时，数据总要「切开喂、拼起来算」：DataLoader 把大数据集切成 minibatch（u11-l2 的 `minibatch_generator` 干的事），多路输出又要拼回一个张量。本模块讲四个函数，分两组记：

**切分组（一分为多）**：

- `torch.chunk(t, k)`：指定要切成 **几块**，块的大小由 PyTorch 自动决定（元素不整除时最后一块会小一点）。
- `torch.split(t, split_size_or_sections)`：指定**每块多大**，参数既可以是单个整数（等分），也可以是列表（精确指定每块长度，如 `[3, 2]`）。当总长不能整除时用 split 而不是 chunk 最稳妥。

**拼接组（合多为一）**：

- `torch.cat([A, B], axis=0)`：沿**已有维度**拼接，要求各张量除拼接维外其余形状全部相同，结果**不增加维度**。`(3,) + (2,)` 沿 axis 0 拼成 `(5,)`。
- `torch.stack([A, B], axis=0)`：沿**新插入的维度**堆叠，要求所有张量形状完全相同，结果**多出一个维度**。两个 `(3,)` 堆成 `(2, 3)`（axis=0）或 `(3, 2)`（axis=1）。

一句话区分：**cat 是「接长」，stack 是「摞高」**。cat 后总元素数是相加，且维度数不变；stack 后维度数加一，总元素数是「个数 × 每个」。

#### 4.4.2 核心流程

```
切分:
  rand(6) --chunk(t, 3)------------> 3 个长度 2 的张量
  rand(5) --split(t, [3, 2])-------> 长度 3 与 长度 2 各一个

拼接 (A = ones(3), B = zeros(3/2)):
  cat([A(3), B(2)], axis=0)  -> (5,)    长度相加, 不增维
  stack([A(3), B(3)], axis=1)-> (3, 2)  新维度插在 1 号位, 增一维
```

注意 cat 的一个隐含约束：不沿拼接维的形状必须一致。`(3,)` 和 `(2,)` 只能沿 axis 0 拼（一维张量仅有的维度）；若两者是 `(2,3)` 与 `(4,3)`，则只能 axis 0（得 `(6,3)`），axis 1 会报错。

#### 4.4.3 源码精读

[ch12/ch12_part1.py:L229-L237](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L229-L237) —— chunk 按块数切：6 个元素的随机张量切成 3 块，每块恰好 2 个。返回值是**元组**，所以用列表推导逐个 `.numpy()` 转回 numpy 打印（又一次张量↔ndarray 互转的演练）：

```python
torch.manual_seed(1)
t = torch.rand(6)
t_splits = torch.chunk(t, 3)
[item.numpy() for item in t_splits]
# [array([0.758, 0.279]), array([0.403, 0.735]), array([0.029, 0.8])]
```

[ch12/ch12_part1.py:L242-L249](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L242-L249) —— split 按每块大小切：5 个元素用列表 `[3, 2]` 精确指定两块长度（5 不能被 3 整除，chunk(5,3) 会给出 2+2+1，split 更可控）：

```python
t = torch.rand(5)
t_splits = torch.split(t, split_size_or_sections=[3, 2])
# [array([0.758, 0.279, 0.403]), array([0.735, 0.029])]
```

[ch12/ch12_part1.py:L254-L258](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L254-L258) —— cat 沿已有维度拼接：长度 3 的全 1 与长度 2 的全 0 接成长度 5，不产生新维度：

```python
A = torch.ones(3)
B = torch.zeros(2)
C = torch.cat([A, B], axis=0)
print(C)   # tensor([1., 1., 1., 0., 0.])
```

[ch12/ch12_part1.py:L263-L267](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part1.py#L263-L267) —— stack 沿新维度堆叠：两个长度 3 的向量沿 axis=1 堆叠，新维度插在第 1 位，得到 `(3, 2)` 矩阵——每一行是「(A 的第 i 个, B 的第 i 个)」组成的对：

```python
A = torch.ones(3)
B = torch.zeros(3)
S = torch.stack([A, B], axis=1)
print(S)   # tensor([[1., 0.], [1., 0.], [1., 0.]])
```

顺带留意：这四段代码两两成对地重复了 `torch.manual_seed(1)`——同一种子下 `rand(6)` 与 `rand(5)` 的前 5 个数相同（0.7576, 0.2793, 0.4031, 0.7347, 0.0293），可以从输出里直接看出随机数流的前缀关系。

#### 4.4.4 代码实践

**实践：cat 与 stack 的对照实验**（示例代码）：

1. 实践目标：用同一对张量分别做 cat 和 stack，从形状与内容两方面记住差别。
2. 操作步骤：

```python
import torch

A = torch.ones(3)
B = torch.zeros(3)

print(torch.cat([A, B], axis=0).shape)    # torch.Size([6])   接长
print(torch.cat([A, A], axis=0))          # tensor([1., 1., 1., 1., 1., 1.])

print(torch.stack([A, B], axis=0))        # [[1,1,1],[0,0,0]]，形状 (2, 3)
print(torch.stack([A, B], axis=0).shape)  # torch.Size([2, 3])
print(torch.stack([A, B], axis=1).shape)  # torch.Size([3, 2])

# 反向拆解: stack 的结果可以 split 回去
S = torch.stack([A, B], axis=0)
print(torch.split(S, [1, 1], dim=0)[0])   # tensor([[1., 1., 1.]]) —— 取回了 A
```

3. 需要观察的现象：cat 结果维度数不变、长度相加；stack 结果维度数加一；stack 后可用 split 沿堆叠维无损拆回。
4. 预期结果：如注释所示。stack 本质是「先各自 `unsqueeze` 升维再 cat」。
5. 以上输出为依据定义的推导，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`torch.chunk(torch.rand(7), 3)` 会切出什么样的结果？

**答案**：7 个元素切成 3 块，不能整除时 PyTorch 按「每块约 \(\lceil 7/3 \rceil = 3\) 个」来切，得到 3、3、1 三块（第三块只剩 1 个元素）。若业务上要求块长相等，应改用 `torch.split(t, 3)`，它会给出 3、3、1 同样结果但语义更显式，或直接设计数据长度可整除。

**练习 2**：两个形状 `(2, 3)` 的张量 `X`、`Y`，`cat([X, Y], axis=1)` 的形状是什么？`stack([X, Y], axis=0)` 呢？

**答案**：cat 沿已有维度 1 拼接：`(2, 3+3) → (2, 6)`，维度数仍是 2；stack 沿新维度 0 堆叠：`(2, 2, 3)`，维度数变 3，新维度大小等于堆叠的张量个数 2。

**练习 3**：为什么说下一讲的 DataLoader「切 batch」与本讲的切分函数一脉相承？

**答案**：DataLoader 本质上就是把整个数据集张量按 `batch_size` 切块（类似 split 的等长切分）、可选用 shuffle 打乱顺序，再逐块产出——与 u11-l2 手写的 `minibatch_generator`（打乱下标、切批、yield）逻辑相同。先在本讲把 chunk/split 的语义搞清，下一讲看到 `DataLoader(t, batch_size=3)` 输出 `tensor([0., 1., 2.])`、`tensor([3., 4., 5.])` 两批时就一目了然。

## 5. 综合实践

**任务：形状变换全家福 + 循环 vs tolist 两种逐元素乘法**。这个任务把本讲四个模块串成一段可运行的代码（示例代码，独立于仓库）：

```python
import torch

torch.manual_seed(1)

# ── 第一步：创建两个形状不同的张量 ─────────────────────────
a = torch.arange(6, dtype=torch.float32).reshape(2, 3)  # (2, 3): [[0,1,2],[3,4,5]]
b = torch.tensor([10., 20., 30.])                        # (3,)

# ── 第二步：逐个演示形状操作，每次打印结果形状 ─────────────
print('广播  a+b     :', (a + b).shape)                    # (2, 3)：b 广播到每一行
print('广播值 a+b    :\n', a + b)                          # [[10,21,32],[13,24,35]]
print('转置  a       :', torch.transpose(a, 0, 1).shape)   # (3, 2)：行列互换
print('重排  a       :', a.reshape(3, 2).shape)            # (3, 2)：摊平重填, 排布不同于转置
print('stack [a,a]   :', torch.stack([a, a], axis=0).shape) # (2, 2, 3)：新维度在 0 号位
print('cat   axis=0  :', torch.cat([a, a], axis=0).shape)  # (4, 3)：沿行拼接
print('cat   axis=1  :', torch.cat([a, a], axis=1).shape)  # (2, 6)：沿列拼接

# ── 第三步：显式循环 vs tolist 两种逐元素乘法 ──────────────
x = torch.tensor([1., 2., 3., 4.])
y = torch.tensor([5., 6., 7., 8.])

# 方式一：显式循环 + 张量下标（每次索引返回 0 维张量）
prod_loop = torch.tensor([x[i] * y[i] for i in range(len(x))])

# 方式二：先 tolist 转成 Python float 列表再循环
prod_tolist = torch.tensor([xi * yi for xi, yi in zip(x.tolist(), y.tolist())])

print('循环   :', prod_loop)    # tensor([ 5., 12., 21., 32.])
print('tolist :', prod_tolist)  # tensor([ 5., 12., 21., 32.])
print('两者一致:', torch.equal(prod_loop, prod_tolist))     # True

# 方式三（对照）：向量化才是 PyTorch 的正确姿势
print('向量化 :', torch.multiply(x, y))
```

**要求与检查点**：

1. 运行前先在纸上写出每个打印的形状与数值，再运行核对（广播后的值：`[[10, 21, 32], [13, 24, 35]]`）。
2. 对比 reshape 与 transpose 的结果内容：同为 `(3, 2)`，`transpose` 的第一行是 `[0, 3]`（原第一列），`reshape` 的第一行是 `[0, 1]`（摊平后重填），确认「同形状、不同排布」。
3. 比较三种乘法：方式一每次 `x[i]` 都要做一次张量索引、返回 0 维张量，开销最大；方式二 `tolist()` 一次性转成普通 Python 浮点数列表，循环体里是纯 Python 运算，写起来自然；方式三一行向量化完成，在实际代码中永远优先。三者数值完全一致（`torch.equal` 返回 `True`）——这正呼应 u8-l2 讲过的「能向量化就不要写 Python 循环」。
4. 若手头有 GPU，可追加实验：把 `a` 用 `a.to('cuda')` 搬上 GPU，再尝试 `a_gpu.numpy()` 观察报错，然后 `a_gpu.cpu().numpy()` 成功——亲手体验「跨设备复制、numpy 只认 CPU」的语义。
5. 以上输出为依据规则的推导，待本地验证。

## 6. 本讲小结

- **张量 ≈ ndarray + device + 自动微分**：`torch.tensor`（复制）与 `torch.from_numpy`（共享内存）是两条创建路径；`.shape`、`.dtype`、`.device` 是三大属性；本章所有运算都能在第 11 章的 numpy 代码里找到同构写法（L215-L222 的范数交叉验证就是明证）。
- **`to()` 是多面手**：`to(dtype)` 换类型（返回新张量，可链式），`to(device)` 搬设备（跨设备必然复制；第 13 章的 `device = "cuda" if torch.cuda.is_available() else "cpu"` 是标准写法）。
- **形状变换只动排布不动数值**：`transpose` 交换维度、`reshape` 摊平重填（同形状时两者内容不同）、`squeeze` 删指定长度 1 的维度。
- **运算按结果形状分三类**：逐元素（支持从尾维对齐的广播）、归约（沿 axis 压缩）、矩阵乘（中间维度相消）；`2 * rand(5,2) - 1` 是源码中现成的标量广播例子。
- **切分与拼接四个函数两两成对**：`chunk`（按块数）/`split`（按块长或列表）切开，`cat`（沿已有维度接长、不增维）/`stack`（沿新维度摞高、增一维）拼回；它们是下一讲 DataLoader 切 batch 的概念前奏。

## 7. 下一步学习建议

下一讲 **u12-l2（Dataset 与 DataLoader 输入管线）** 将顺着本讲结尾的伏笔展开：`ch12/ch12_part1.py` 的 L270 之后就是输入管线部分——先用 `DataLoader(t, batch_size=3)` 把张量切成批次，再实现 `JointDataset`（自定义 `__len__`/`__getitem__` 契约）与 `TensorDataset`，最后用 `ImageDataset` 从 `cat_dog_images` 目录读图。建议阅读源码时带着两个本讲的问题：① DataLoader 的批次切分与本讲的 `split` 有何异同（提示：shuffle 与 drop_last）；② `ImageDataset` 返回的图片张量为什么是 `(通道, 高, 宽)` 而不是 `(高, 宽, 通道)`（提示：`example[0].numpy().transpose((1, 2, 0))` 那一行就在做维度变换，用的正是本讲的 transpose）。如果想在读下一讲前再巩固本讲，可以把综合实践的代码改写成 numpy 版本对照一遍，体会「同一套数学、两个框架」。
