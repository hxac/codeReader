# 卷积运算从零实现

## 1. 本讲目标

从本讲开始，我们进入专家层的第一个主题：卷积神经网络（CNN）。在跑起完整 CNN 之前，先回答一个更根本的问题——**「卷积」这个运算到底在做什么？**

学完本讲，你应该能够：

1. 逐行读懂 `ch14/ch14_part1.py` 中手写的 `conv1d` 与 `conv2d` 两个 numpy 函数，理解「滑窗 + 内积 + 核翻转」的循环结构。
2. 用输出尺寸公式 \( O = \lfloor (N + 2p - M)/s \rfloor + 1 \) 预测任意填充 `p`、步幅 `s` 下卷积输出的形状。
3. 掌握 `nn.Conv2d` / `nn.MaxPool2d` / `nn.Dropout` 的参数含义与 `nn.Sequential` 搭建 CNN 的写法，理解多通道卷积的权重形状 `(C_out, C_in, kH, kW)`。
4. 理解卷积的两大立身之本——**稀疏连接**与**参数共享**，并能逐层推演特征图尺寸（28×28 → 14×14 → 7×7 → 3136）与感受野的增长。

本讲只讲「积木块」，完整 CNN 的数据加载与训练循环留给下一讲（u14-l2）。

## 2. 前置知识

- **全连接层的回顾**（见 u12-l3）：`nn.Linear(in, out)` 中每个输出单元与**所有**输入单元相连。对 28×28 的 MNIST 图像，输入展平后是 784 维，一个隐藏层单元就要 784 个权重——图像越大，参数爆炸越严重。卷积正是为解决这个问题而生的。
- **图像即数字张量**（见 u1-l3、u12-l2）：灰度图是一个 `高×宽` 的数值矩阵，彩色图是 `3×高×宽`（RGB 三通道）。PyTorch 的图像约定是**通道在前**（CHW）。
- **内积（点积）**：两个同形状数组逐元素相乘再求和。卷积在每个窗口位置做的正是这件事。
- **向下取整** \( \lfloor \cdot \rfloor \)：当输入尺寸不能被步幅整除时，卷积会丢弃边缘多余的像素，公式中体现为取整。

一个需要提前澄清的术语坑：深度学习文献里说的「卷积」，严格数学意义上是**互相关（cross-correlation）**——它不翻转卷积核；真正的卷积要先把核旋转 180°。本讲会反复对比这两者，因为代码实践任务正是围绕这个区别展开的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ch14/ch14_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py) | 本讲主战场：手写 `conv1d`/`conv2d`、图像读取、L2 正则示例、MNIST CNN 的搭建与训练（约 500 行的章脚本） |
| [ch14/ch14_part1.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.ipynb) | 与 `.py` 等价的 notebook 版本（按 u1-l1 的双轨约定，`.py` 由它转换而来），本讲引用的运行结果均可在其中找到 |
| [ch14/example-image.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/example-image.png) | 配套示例图片，用于演示 RGB 三通道读取 |

按 u1-l1 的约定：读码、引用行号用 `.py`；想交互实验就开 `.ipynb`。

## 4. 核心概念与源码讲解

### 4.1 一维离散卷积：conv1d 滑窗内积

#### 4.1.1 概念说明

一维卷积是把一个短数组（**卷积核** kernel，长度 \( M \)）沿一个长数组（**输入**，长度 \( N \)）滑动，每个位置计算窗口与核的内积，得到输出序列。

数学上的一维离散卷积定义为：

\[ y[i] = \sum_{j=0}^{M-1} x[i+j] \cdot w[M-1-j] \]

注意 \( w[M-1-j] \)：核的下标是**反着取**的，等价于先把核翻转再逐位相乘。这就是「真卷积要翻转核」的出处。信号处理库（`np.convolve`、`scipy.signal.convolve2d`）都遵循这个定义。

为什么先讲一维？因为它把卷积的全部要素——滑窗、内积、翻转、填充、步幅——压缩在最少的下标里，二维只是把单下标换成双下标的直接推广。

#### 4.1.2 核心流程

`conv1d(x, w, p, s)` 的执行过程：

```text
1. 翻转核：  w_rot = w[::-1]                    # 真卷积定义
2. 填充：    x 两端各补 p 个 0 → x_padded       # 长度 N + 2p
3. 滑窗：    起点 i = 0, s, 2s, ...             # 每次右移 s 格
4. 内积：    res[i/s] = sum(x_padded[i:i+M] * w_rot)
5. 输出长度 = ⌊(N + 2p − M)/s⌋ + 1
```

#### 4.1.3 源码精读

函数定义在 [ch14/ch14_part1.py:116-127](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L116-L127)：手写的 `conv1d`，完成「翻转核 → 零填充 → 按步幅滑窗取内积」三步。

```python
def conv1d(x, w, p=0, s=1):
    w_rot = np.array(w[::-1])          # ① 核翻转：真卷积的定义
    x_padded = np.array(x)
    if p > 0:
        zero_pad = np.zeros(shape=p)
        x_padded = np.concatenate(     # ② 两端各补 p 个 0
            [zero_pad, x_padded, zero_pad])
    res = []
    for i in range(0, (int((len(x_padded) - len(w_rot))/s) + 1) * s, s):
        res.append(np.sum(             # ③ 窗口与核逐元素相乘求和
            x_padded[i:i + w_rot.shape[0]] * w_rot))
    return np.array(res)
```

最费解的是第 ③ 步的 `range` 写法：`(int((len(x_padded) - len(w_rot))/s) + 1) * s` 是「窗口个数 × 步幅」。窗口个数正是输出尺寸公式 \( \lfloor (N_{pad} - M)/s \rfloor + 1 \)；`range(0, K*s, s)` 恰好生成 `0, s, …, (K-1)*s` 共 K 个合法起点——起始位置加核长不能越过填充后的边界。

测试代码在 [ch14/ch14_part1.py:130-138](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L130-L138)：用书中数据 `x = [1,3,2,4,5,6,1,3]`、`w = [1,0,3,1,2]`，`p=2, s=1`，并与 `np.convolve(x, w, mode='same')` 对拍。

翻转后的核是 `w_rot = [2,1,3,0,1]`，填充后 `x_padded = [0,0,1,3,2,4,5,6,1,3,0,0]`（长度 12）。逐窗口手算（本表格为笔者按源码手工推演，与 notebook 实际输出一致）：

| 窗口 | x_padded[i:i+5] | 与 [2,1,3,0,1] 的内积 |
|------|-----------------|----------------------|
| i=0  | [0,0,1,3,2]     | 0+0+3+0+2 = **5**    |
| i=1  | [0,1,3,2,4]     | 0+1+9+0+4 = **14**   |
| i=2  | [1,3,2,4,5]     | 2+3+6+0+5 = **16**   |
| i=3  | [3,2,4,5,6]     | 6+2+12+0+6 = **26**  |
| i=4  | [2,4,5,6,1]     | 4+4+15+0+1 = **24**  |
| i=5  | [4,5,6,1,3]     | 8+5+18+0+3 = **34**  |
| i=6  | [5,6,1,3,0]     | 10+6+3+0+0 = **19**  |
| i=7  | [6,1,3,0,0]     | 12+1+9+0+0 = **22**  |

输出长度 = 12 − 5 + 1 = 8。notebook 中两个实现的输出均为 `[5 14 16 26 24 34 19 22]`，逐元素相等——手写版与 `np.convolve` 的一致性得到验证。

#### 4.1.4 代码实践

1. **实践目标**：用输出尺寸公式预测 `conv1d` 在不同 `p`、`s` 下的输出长度，再用代码验证。
2. **操作步骤**（示例代码，可直接在 `ch14/` 目录下的 Python 会话中运行）：

   ```python
   from ch14_part1 import conv1d   # 或直接复制函数定义
   for p, s in [(0,1), (2,1), (0,2), (2,2)]:
       n, m = 8, 5
       expect = (n + 2*p - m)//s + 1
       got = len(conv1d([1,3,2,4,5,6,1,3], [1,0,3,1,2], p=p, s=s))
       print(f'p={p}, s={s}: 公式={expect}, 实际={got}')
   ```

3. **需要观察的现象**：四组参数下公式值与实际长度是否全部相等。
4. **预期结果**：`p=0,s=1 → 4`；`p=2,s=1 → 8`；`p=0,s=2 → 2`；`p=2,s=2 → 4`（按公式 \(\lfloor(8+2p-5)/s\rfloor+1\) 计算）。以上为公式推演结果，具体数值待本地验证。
5. 注意 `p=0, s=2` 时：8 − 5 = 3，3/2 向下取整 = 1，加 1 得 2——最后一个位置的多余元素被丢弃了，这正是取整符号的物理含义。

#### 4.1.5 小练习与答案

**练习 1**：把 `conv1d` 中的 `w_rot = np.array(w[::-1])` 改成 `w_rot = np.array(w)`（不翻转），输出的数值会怎么变？它将等价于哪个运算？

**答案**：输出数值会改变（不再是 `[5,14,16,26,24,34,19,22]`），此时函数实现的是**互相关**。对拍对象从 `np.convolve`（翻转核）换成「不翻转核」的滑窗内积即可验证。深度学习框架（如 `nn.Conv1d`/`nn.Conv2d`）采用的正是这种不翻转的互相关定义。

**练习 2**：输入长度 10、核长度 3、`p=1`、`s=2`，输出长度是多少？

**答案**：\( \lfloor(10 + 2\times1 - 3)/2\rfloor + 1 = \lfloor 9/2 \rfloor + 1 = 4 + 1 = 5 \)。

**练习 3**：为什么 `mode='same'` 要求手写实现配 `p=2`？

**答案**：`same` 模式的语义是「输出与原输入等长」，即 \( O = N = 8 \)。代入公式 \( 8 = 8 + 2p - 5 + 1 \) 解得 \( p = 2 \)——填充量恰好是核长减一的一半（核长 5 为奇数时取整无歧义）。

### 4.2 二维卷积 conv2d：填充、步幅与输出尺寸

#### 4.2.1 概念说明

图像是二维的，卷积核也变成 \( M_1 \times M_2 \) 的小矩阵（典型如 3×3、5×5），沿「行、列」两个方向滑动。二维离散卷积定义为：

\[ Y[i, j] = \sum_{m=0}^{M_1-1} \sum_{n=0}^{M_2-1} X[i+m,\, j+n] \cdot W_{\text{rot}}[m, n] \]

其中 \( W_{\text{rot}} \) 是把核**旋转 180°**（行、列同时反转）后的核。两个超参数控制输出形状：

- **填充 p**：在输入四周补 0。作用有二：控制输出尺寸（如让输出与输入同尺寸，即 "same" 卷积）；避免边缘像素只被窗口扫过一两次而「信息被稀释」。
- **步幅 s**：窗口每次滑动的格数。\( s > 1 \) 时输出变小，相当于内置了下采样。

每个方向的输出尺寸独立满足：

\[ O = \left\lfloor \frac{N + 2p - M}{s} \right\rfloor + 1 \]

#### 4.2.2 核心流程

`conv2d(X, W, p, s)` 的执行过程（`p=(p_h, p_w)`、`s=(s_h, s_w)` 均为二元组，允许高、宽方向取不同值）：

```text
1. 旋转核：  W_rot = W[::-1, ::-1]              # 180° 旋转，二维版"翻转"
2. 填充：    新建 (H+2p_h, W+2p_w) 全零矩阵，
            把 X 镶嵌进中心                     # 与 conv1d 的 concatenate 不同
3. 双层循环：行起点 i 以 s_h 步进，列起点 j 以 s_h/s_w 步进
4. 内积：    res 行追加 sum(子窗口 * W_rot)
```

#### 4.2.3 源码精读

函数定义在 [ch14/ch14_part1.py:159-177](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L159-L177)：手写 `conv2d`，实现「核旋转 180° → 零填充 → 行列双层滑窗」。

```python
def conv2d(X, W, p=(0, 0), s=(1, 1)):
    W_rot = np.array(W)[::-1,::-1]      # ① 行、列同时反转 = 旋转 180°
    X_orig = np.array(X)
    n1 = X_orig.shape[0] + 2*p[0]
    n2 = X_orig.shape[1] + 2*p[1]
    X_padded = np.zeros(shape=(n1, n2)) # ② 新建全零画布，再把 X 镶进去
    X_padded[p[0]:p[0]+X_orig.shape[0],
    p[1]:p[1]+X_orig.shape[1]] = X_orig

    res = []
    for i in range(0, (int((X_padded.shape[0] -
                            W_rot.shape[0]) / s[0]) + 1) * s[0], s[0]):
        res.append([])                  # ③ 每个行起点开一个新行
        for j in range(0, (int((X_padded.shape[1] -
                                W_rot.shape[1]) / s[1]) + 1) * s[1], s[1]):
            X_sub = X_padded[i:i + W_rot.shape[0],
                    j:j + W_rot.shape[1]]
            res[-1].append(np.sum(X_sub * W_rot))   # ④ 子窗口内积
    return(np.array(res))
```

三个值得咀嚼的细节：

- **① `[::-1, ::-1]` 是旋转不是转置**。`W.T` 交换行列（沿对角线翻转），`W[::-1,::-1]` 旋转 180°（每个元素 \( (m,n) \to (M_1-1-m, M_2-1-n) \)）。两者只在对称核上恰好重合。这个区分是本讲综合实践的关键。
- **② 填充方式换成了「画布镶嵌」**：一维用 `concatenate` 两端拼接即可；二维要四边都补，所以先建全零数组再用切片赋值把 `X` 放到中心。这也是它比 `conv1d` 多几行的原因。
- **③④ 双层循环共用同一个 range 技巧**：外层生成输出行，内层生成输出列；`res[-1].append` 把当前行的结果追加到「最后一个已开出的行」里。

测试代码在 [ch14/ch14_part1.py:179-187](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L179-L187)：对 4×4 输入、3×3 核、`p=(1,1), s=(1,1)` 调用手写版，并与 `scipy.signal.convolve2d(X, W, mode='same')` 对拍。

手算左上角一个元素验证（笔者的手工推演）：填充后画布为 6×6，第一个 3×3 窗口是

```text
0 0 0          1 1 0
0 1 3     ·    1 2 1    = 0+0+0+0+2+3+0+0+6 = 11
0 5 6          3 0 1
```

与 notebook 实际输出的 `[0][0] = 11` 一致；notebook 中两个实现输出完全相同，均为 `[[11,25,32,13],[19,25,24,13],[13,28,25,17],[11,17,14,9]]`（4×4，same 尺寸）。输出尺寸验证：\( \lfloor(4+2-3)/1\rfloor+1 = 4 \)，高宽同。

#### 4.2.4 代码实践

1. **实践目标**：建立「先公式、后代码」的习惯——预测形状再验证。
2. **操作步骤**（示例代码）：先在纸上算，再运行

   ```python
   # X 为 4×4，W 为 3×3
   print(conv2d(X, W, p=(2, 2), s=(1, 1)).shape)   # 预测: ?
   print(conv2d(X, W, p=(0, 0), s=(2, 2)).shape)   # 预测: ?
   print(conv2d(X, W, p=(1, 0), s=(1, 2)).shape)   # 预测: ?（高宽不对称）
   ```

3. **需要观察的现象**：三组输出形状与你的手算是否一致，特别是高宽不对称的第三组。
4. **预期结果**：按公式分别算高、宽两个方向：`p=(2,2),s=1` → 高宽各 \( 4+4-3+1=6 \) → `(6, 6)`；`p=0,s=2` → 各 \( \lfloor(4-3)/2\rfloor+1=1 \) → `(1, 1)`；`p=(1,0),s=(1,2)` → 高 \( 4+2-3+1=4 \)、宽 \( \lfloor(4-3)/2\rfloor+1=1 \) → `(4, 1)`。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：输入 32×32、核 5×5，想要输出仍是 32×32，`p` 和 `s` 应取多少？

**答案**：\( s=1 \)，且 \( 32 = 32 + 2p - 5 + 1 \Rightarrow 2p = 4 \Rightarrow p = 2 \)。这就是后文 CNN 里 `nn.Conv2d(kernel_size=5, padding=2)` 的由来。

**练习 2**：`s=3, p=1, N=10, M=3` 时输出多大？丢掉了几个输入元素？

**答案**：\( \lfloor(10+2-3)/3\rfloor+1 = 3+1 = 4 \)。最后一个窗口起点在 \( 3\times3=9 \)，覆盖 [9, 11]，输入只用到下标 9（填充占了两位）——填充列中第 10、11 位置的 0 从未被任何完整窗口使用。

**练习 3**：手写 `conv2d` 的输出为什么和 `scipy.signal.convolve2d` 一致而不是 `correlate2d`？

**答案**：因为源码第 ① 步执行了 `[::-1, ::-1]` 旋转，与 `convolve2d` 同属「真卷积」阵营。若删掉这一步，它就会与 `correlate2d(X, W, mode='same')` 一致——这正是综合实践要动手证明的事。

### 4.3 从手写卷积到 nn.Conv2d：多通道、MaxPool2d 与 Dropout

#### 4.3.1 概念说明

手写版只处理**单通道**灰度图。真实图像与真实网络涉及两类「多」：

- **多输入通道**：RGB 图像有 3 个通道。卷积核必须为每个输入通道准备一份 \( k_H \times k_W \) 的权重，各通道分别做卷积后**求和**，得到一个输出特征图。所以单个卷积核的权重形状是 \( (C_{in}, k_H, k_W) \)。
- **多输出通道**：一个网络层想同时提取多种模式（边缘、纹理……），就准备多个卷积核，每个核产出一个特征图。`out_channels` 就是卷积核的个数。

于是 `nn.Conv2d` 的权重张量形状为 `(out_channels, in_channels, kH, kW)`，加上形状为 `(out_channels,)` 的偏置。**框架的卷积不旋转核**（互相关），但由于核是从数据中学出来的，「先旋转再学」与「直接学」完全等价，所以深度学习干脆省掉翻转。

配套的两个层：

- **`nn.MaxPool2d`**：最大池化。在每个窗口内只保留最大值，用于下采样、扩大后续层的感受野。**没有任何可学习参数**（所以叫「层」不叫「卷积核」）；`stride` 缺省等于 `kernel_size`。
- **`nn.Dropout(p)`**：训练时以概率 `p` 随机置零一部分激活值并按 \( 1/(1-p) \) 缩放（防止期望改变），评估时自动关闭——这与 u13-l1 讲过的 `train()`/`eval()` 双模式机制一致。补充知识：对特征图还有空间变体 `nn.Dropout2d`（整通道置零），但本书代码用的是普通 `nn.Dropout`。

#### 4.3.2 核心流程

一个卷积层的前向（单样本、无偏置写法）：

```text
输入  X: (C_in, H, W)
权重  W: (C_out, C_in, kH, kW)
for each 输出通道 co:                      # C_out 个核
    特征图 co = Σ_{ci=0..C_in-1} 互相关(X[ci], W[co, ci])
输出: (C_out, O_h, O_w)    # O 由 4.2 的公式给出
```

#### 4.3.3 源码精读

**多通道输入的实证**：[ch14/ch14_part1.py:211-216](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L211-L216) 用 `torchvision.io.read_image` 读入示例图片并打印形状、通道数与数据类型。notebook 的实际输出是 `torch.Size([3, 252, 221])`、`3`、`torch.uint8`——通道维在最前，这正是 `nn.Conv2d(in_channels=3, ...)` 的输入形态，也和 u12-l2 讲过的 CHW 约定一致。

**Conv2d 的参数化**：[ch14/ch14_part1.py:233-234](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L233-L234) 构造 `nn.Conv2d(in_channels=3, out_channels=5, kernel_size=5)` 并遍历其 `parameters()` 计算逐参数平方和（L2 惩罚的手工实现；`optimizer(weight_decay=…)` 是等价的内置写法）。这行代码本身就在示范多通道卷积的接口：3 通道进、5 个特征图出，单个核形状 `(3, 5, 5)`，整层权重 `(5, 3, 5, 5)`。

**CNN 的搭建**：[ch14/ch14_part1.py:345-371](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L345-L371) 用 `nn.Sequential` + `add_module` 串起两个「卷积块」和一个分类头，每块的结构是 `Conv2d → ReLU → MaxPool2d`：

```python
model.add_module('conv1', nn.Conv2d(in_channels=1,  out_channels=32, kernel_size=5, padding=2))
model.add_module('relu1', nn.ReLU())
model.add_module('pool1', nn.MaxPool2d(kernel_size=2))
model.add_module('conv2', nn.Conv2d(in_channels=32, out_channels=64, kernel_size=5, padding=2))
model.add_module('relu2', nn.ReLU())
model.add_module('pool2', nn.MaxPool2d(kernel_size=2))
# ... Flatten → Linear(3136, 1024) → ReLU → Dropout(0.5) → Linear(1024, 10)
```

注意两处「通道接力」：`conv1` 的 `out_channels=32` 必须等于 `conv2` 的 `in_channels=32`——上一层的特征图数就是下一层的输入通道数；MNIST 是灰度图所以第一层 `in_channels=1`。

**参数量对比（稀疏连接 + 参数共享的威力）**：`conv1` 的权重共 \( 32 \times 1 \times 5 \times 5 = 800 \) 个（加偏置 832 个）；`conv2` 为 \( 64 \times 32 \times 5 \times 5 = 51200 \) 个（加偏置 51264 个）。假如用全连接层从 784 维输入直接产出同样规模的特征图（28×28×32 个单元、每单元 784 个权重），需要约 1967 万个权重。832 对 1967 万——卷积核在整个图像上**共享**同一组 25 个权重，且每个输出单元只「看见」局部 5×5 的**稀疏连接**，这两点正是 CNN 参数效率的来源。

#### 4.3.4 代码实践

1. **实践目标**：用真实权重形状印证 `(C_out, C_in, kH, kW)` 约定。
2. **操作步骤**（示例代码）：

   ```python
   conv = nn.Conv2d(in_channels=3, out_channels=5, kernel_size=5)
   for name, p in conv.named_parameters():
       print(name, tuple(p.shape))
   ```

3. **需要观察的现象**：打印出的名字与形状。
4. **预期结果**：`weight (5, 3, 5, 5)` 与 `bias (5,)`——与 4.3.1 的约定一致。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`nn.Conv2d(16, 32, 3, padding=1)` 的权重和偏置各有多少个元素？

**答案**：权重 \( 32 \times 16 \times 3 \times 3 = 4608 \)，偏置 32，合计 4640。

**练习 2**：为什么 `MaxPool2d` 没有出现在 `parameters()` 里？

**答案**：最大池化只是固定的取最大运算，不含任何可学习权重，也就没有参数可注册（对比 u13-l1 的参数注册规则：只有 `nn.Parameter` 才会被 `parameters()` 认领）。

**练习 3**：本书 CNN 里 `nn.Dropout(p=0.5)` 夹在两个全连接层之间，为什么放在卷积块之后而不是卷积层之间？

**答案**：书中做法是对进入分类头的高维组合特征做正则化，抑制全连接层的协同适应（co-adaptation）。Dropout 的摆放是设计选择而非铁律——放在卷积层之间（常配合 `nn.Dropout2d` 整通道置零）也是文献中常见的做法，但那不是本仓库代码的用法。

### 4.4 特征图尺寸推演与感受野：从 28×28 到 3136

#### 4.4.1 概念说明

- **特征图（feature map）**：一个卷积核扫过整张输入后得到的输出矩阵。通道数 = 特征图数 = 核数。
- **感受野（receptive field）**：某个特征图单元的取值所依赖的**原始输入区域大小**。堆叠卷积层，深层单元的感受野逐层扩大——这正是 CNN 学到「层级特征」（浅层边缘 → 深层部件 → 物体）的机制基础。

尺寸推演只有一个工具：4.2 的公式 \( O = \lfloor(N + 2p - M)/s\rfloor + 1 \)，对高、宽各算一遍。

#### 4.4.2 核心流程

对本书 CNN（MNIST，输入 `(batch, 1, 28, 28)`）逐层推演：

| 层 | 参数 | 尺寸计算 | 输出形状 |
|----|------|----------|----------|
| 输入 | — | — | (1, 28, 28) |
| conv1 | k=5, p=2, s=1 | (28+4−5)/1+1 = 28 | (32, 28, 28) |
| pool1 | k=2（s 缺省=2） | (28−2)/2+1 = 14 | (32, 14, 14) |
| conv2 | k=5, p=2, s=1 | (14+4−5)/1+1 = 14 | (64, 14, 14) |
| pool2 | k=2（s 缺省=2） | (14−2)/2+1 = 7 | (64, 7, 7) |
| Flatten | — | 64×7×7 | 3136 |

**感受野的递推**（补充推导，非书中代码）：设第 \( l \) 层前特征图上间隔 1 的两单元对应前一层上相距 \( j_{l-1} \) 的两单元（\( j \) 为累计步幅），则

\[ r_l = r_{l-1} + (k_l - 1)\cdot j_{l-1}, \qquad j_l = j_{l-1}\cdot s_l \]

从输入（\( r_0 = 1, j_0 = 1 \)）开始：conv1 → \( r = 1+4\times1 = 5 \)；pool1 → \( r = 5+1\times1 = 6,\ j = 2 \)；conv2 → \( r = 6+4\times2 = 14 \)；pool2 → \( r = 14+1\times2 = 16,\ j = 4 \)。即第二 blocks 结束后，**每个特征单元已看到原图 16×16 的区域**——两个「卷积+池化」块就把局部感受野从 5 扩到 16，若再加深会迅速覆盖全图。

#### 4.4.3 源码精读

- [ch14/ch14_part1.py:353-354](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L353-L354)：六个卷积块模块挂好后，用假输入 `x = torch.ones((4, 1, 28, 28))` 做一次前向查形状。notebook 输出 `torch.Size([4, 64, 7, 7])`，与上表逐项吻合——batch 维 4 原样保留。
- [ch14/ch14_part1.py:359-362](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L359-L362)：追加 `nn.Flatten()` 后再次前向，输出 `torch.Size([4, 3136])`。`Flatten` 把 (64, 7, 7) 摊平成 3136 维向量，卷积特征由此接入全连接分类头。
- [ch14/ch14_part1.py:367-371](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L367-L371)：`fc1 = nn.Linear(3136, 1024)`、ReLU、`Dropout(p=0.5)`、`fc2 = nn.Linear(1024, 10)`。第一个数 `3136` 不是拍脑袋——它必须**精确等于** `C_out × O_h × O_w`，否则前向直接报维度错误。这就是「先算尺寸再写网络」的原因。

这套「`torch.ones` 假输入 + 前向查形状」是搭建任何新网络的标配自检手法，比手算更快也更可靠。

#### 4.4.4 代码实践

1. **实践目标**：逐层（而非整网）观察形状流转，把 4.4.2 的表格「跑」出来。
2. **操作步骤**（示例代码，逐层切出子模块前向）：

   ```python
   x = torch.ones((4, 1, 28, 28))
   for name, layer in model.named_children():
       x = layer(x)
       print(f'{name:8s} -> {tuple(x.shape)}')
       if name == 'pool2':
           break   # 卷积部分到此为止
   ```

3. **需要观察的现象**：每行打印的形状变化是否与 4.4.2 表格一致。
4. **预期结果**：`conv1 → (4, 32, 28, 28)`、`relu1 → 不变`、`pool1 → (4, 32, 14, 14)`、`conv2 → (4, 64, 14, 14)`、`relu2 → 不变`、`pool2 → (4, 64, 7, 7)`。待本地验证（注意 ReLU 不改变形状）。

#### 4.4.5 小练习与答案

**练习 1**：若把两个 `MaxPool2d` 都去掉，`fc1` 的输入维度应改成多少？

**答案**：两个池化层各贡献一个因子 2，去掉后特征图为 (64, 28, 28)，`fc1` 输入 \( 64 \times 28 \times 28 = 50176 \)。

**练习 2**：把 `conv2` 的 `padding` 从 2 改成 0，后面哪一层会崩、为什么？

**答案**：`fc1`（或更早的 `Flatten` 之后的 `Linear`）会崩。conv2 输出变为 \( 14 - 5 + 1 = 10 \)，pool2 后为 5，摊平得 \( 64 \times 5 \times 5 = 1600 \neq 3136 \)，与 `nn.Linear(3136, 1024)` 的输入维度不匹配，前向抛出形状错误。

**练习 3**：只堆卷积（不池化）要多少层 3×3、s=1 的卷积才能让感受野达到 16？

**答案**：每层感受野增加 \( k-1 = 2 \)，需 \( r = 1 + 2n \ge 16 \Rightarrow n = 8 \) 层（得 r=17）。而书里用两层卷积加两层池化就达到 16——**池化（更大的累计步幅）是扩张感受野的加速器**，这也是下采样层的核心价值之一。

## 5. 综合实践

**任务：证明「手写 numpy 互相关 = `scipy.signal.correlate2d` = `nn.Conv2d`」，并厘清它们与书中 `conv2d`（真卷积）的差异。**

这个任务把 4.2（滑窗实现）与 4.3（框架行为）串成一条线，一劳永逸地解决「卷积还是互相关」的困惑。

### 步骤 1：实现 valid 模式的二维卷积（numpy，不翻转核）

「valid」即不加填充、窗口必须完全落在输入内。以下为示例代码（基于 4.2 的 `conv2d` 删去旋转与填充得到）：

```python
import numpy as np
import scipy.signal
import torch
import torch.nn as nn

def conv2d_valid(X, W):
    X = np.array(X); W = np.array(W)          # 关键：不翻转核 → 互相关
    H, Wd = X.shape; kh, kw = W.shape
    out_h, out_w = H - kh + 1, Wd - kw + 1    # p=0, s=1 的尺寸公式
    res = np.zeros((out_h, out_w))
    for i in range(out_h):
        for j in range(out_w):
            res[i, j] = np.sum(X[i:i+kh, j:j+kw] * W)
    return res

X = [[1, 3, 2, 4], [5, 6, 1, 3], [1, 2, 0, 2], [3, 4, 3, 2]]
W = [[1, 0, 3], [1, 2, 1], [0, 1, 1]]

mine = conv2d_valid(X, W)
ref  = scipy.signal.correlate2d(X, W, mode='valid')
assert np.allclose(mine, ref), '与 correlate2d 不一致！'
print('numpy 互相关 == correlate2d ✓')
```

**预期现象**：断言通过，输出为 2×2 矩阵（\( 4-3+1 = 2 \)）。待本地验证。

### 步骤 2：同一个 W 喂给 nn.Conv2d 对比

框架的卷积是批式的，输入需补 batch 维与通道维；权重则直接注入（示例代码）：

```python
conv = nn.Conv2d(in_channels=1, out_channels=1, kernel_size=3, bias=False)
with torch.no_grad():
    conv.weight[0, 0] = torch.tensor(W, dtype=torch.float32)   # 注入同一个核

x_t = torch.tensor(X, dtype=torch.float32).reshape(1, 1, 4, 4)
out = conv(x_t)[0, 0].numpy()
print(out)
assert np.allclose(out, ref), '与 correlate2d 不一致！'
print('nn.Conv2d == correlate2d ✓')
```

**预期现象**：第二个断言也通过——`nn.Conv2d` 的前向与「不翻转核」的 `correlate2d` 逐元素相等。待本地验证。

### 步骤 3：解释「一致还是相差一个转置」

把四路结果摆在一起（可在步骤 2 后继续运行）：

```python
print('correlate2d(X, W)      =\n', scipy.signal.correlate2d(X, W, mode='valid'))
print('convolve2d(X, W)       =\n', scipy.signal.convolve2d(X, W, mode='valid'))
print('书中 conv2d (p=0, s=1) =\n', conv2d(X, W, p=(0, 0), s=(1, 1)))   # 会旋转核
```

结论分三层：

1. **一致的部分**：`conv2d_valid`、`correlate2d(X, W)`、`nn.Conv2d`（注入 W）三者相等；书中手写 `conv2d`（旋转核）与 `convolve2d(X, W)` 相等。还可以验证恒等式 `correlate2d(X, W) == convolve2d(X, W 旋转180°)`。
2. **「相差一个转置」的说法从何而来**：把书中 `conv2d` 的输出与 `correlate2d` 的输出对比，差的是核的 **180° 旋转**（`W[::-1, ::-1]），**不是**线性代数里的矩阵转置 `W.T`（沿对角线镜像）。若你在自己的实现里误用 `W.T`，两个断言都会失败——只有当 W 恰好中心对称（如某些平均核）时，旋转、转置才与原核重合，此时各种「卷积」的输出才会碰巧一致。
3. **为什么框架敢不翻转**：CNN 的核是从数据学出来的。真卷积「旋转一个可学习的核」与互相关「直接学一个核」在表达能力上完全等价（学到的东西互为旋转），所以 `nn.Conv2d` 选择省掉旋转。**但读别人的固定核（如经典边缘检测算子）时必须分清两套约定**，否则数值对不上。

### 观察清单

- [ ] 步骤 1 断言通过（输出 2×2）。
- [ ] 步骤 2 断言通过（`nn.Conv2d` 与 `correlate2d` 一致）。
- [ ] 步骤 3 中 `convolve2d` 的结果与 `correlate2d` **不相等**（除非核对称），且前者等于书中 `conv2d` 的结果。
- [ ] 能口头回答：`W.T` 与 `W[::-1,::-1]` 什么时候相等？

## 6. 本讲小结

- 卷积 = **滑窗 + 内积**：`conv1d`/`conv2d` 的循环结构完全一致，二维只是行、列双层循环，输出尺寸统一由 \( O = \lfloor(N + 2p - M)/s\rfloor + 1 \) 给出（高宽各算一遍）。
- **填充 p 控尺寸**（`padding=2` 让 5×5 核保持 28×28 不变）、**步幅 s 控下采样**；`MaxPool2d` 的 `stride` 缺省等于 `kernel_size`，两层 2×2 池化把 28×28 缩到 7×7。
- **真卷积旋转核、互相关不旋转**：书中手写版（`[::-1, ::-1]`）与 `np.convolve`/`convolve2d` 同阵营；`nn.Conv2d` 与 `correlate2d` 同阵营。核可学习时二者等价，核固定时必须分清。
- 多通道卷积的权重形状是 `(out_channels, in_channels, kH, kW)`，各输入通道的结果**求和**成一个特征图；上一层 `out_channels` 必须等于下一层 `in_channels`。
- 卷积的参数效率来自**稀疏连接**（每个输出单元只看局部窗口）与**参数共享**（同一组核权重滑遍全图）：conv1 只要 832 个参数就产出 32 张 28×28 特征图。
- 搭网络的标准自检是「`torch.ones` 假输入 + 前向查形状」：本书 CNN 的 `fc1` 输入维度 3136 = 64×7×7 是逐层算出来的，不是魔法数字。

## 7. 下一步学习建议

本讲只搭好了积木并验证了形状。下一讲 **u14-l2（手写 CNN 训练 CIFAR-10 与 ResNet）** 将把这些积木组装成完整训练流程：`torchvision.datasets` + `DataLoader` 的标准数据管线（含 `transforms` 标准化）、`train` 函数的训练/验证双循环（就在 [ch14/ch14_part1.py:387-424](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L387-L424)，本讲刻意未展开），以及 `torchvision.models.resnet18` 的预训练用法。建议先行阅读 `ch14/ch14_part1.py` 第 283–430 行的 MNIST 训练部分作为衔接——它正是 u13-l3 训练循环骨架在图像任务上的直接复用。
