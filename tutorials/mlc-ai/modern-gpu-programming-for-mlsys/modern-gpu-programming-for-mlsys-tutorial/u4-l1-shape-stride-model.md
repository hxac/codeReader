# 第 4 单元第 1 讲：Shape-Stride 模型与 Tile Layout 函数

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「数据布局（data layout）」的本质：把张量的**逻辑索引**映射到**物理位置**的函数。
2. 给定一个布局 \( S[(\text{shape}) : (\text{strides})] \)，用点积公式算出任意元素的线性地址。
3. 把「分块（tiling）」表达为对原始索引的**再切分**：把 `(i, j)` 拆成 `(tile_row, row_in_tile, tile_col, col_in_tile)` 四个坐标，并推导出对应的 shape 与 strides。
4. 写出**一般形式的布局函数** \( f_D(x) \)：先按 shape 做 unflatten，再与 strides 做点积。

本讲是单元四（数据布局与记号）的第一讲，只依赖你在 u2-l2 建立的四种存储空间（GMEM/SMEM/TMEM/RF）的概念——布局回答的正是「在给定存储空间内部，元素究竟落在哪个位置」。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（不熟悉也没关系，下面会用通俗语言快速补齐）：

- **逻辑形状（logical shape）**：机器学习程序里描述张量的方式，例如一个 `8×8` 矩阵。它只说明「有几个维度、每维多长」，**不说明字节实际存在哪里、按什么顺序排**。
- **一维物理存储**：无论张量逻辑上是几维，真实内存总是一维的字节序列。PyTorch 张量的底层就是一个一维 storage。
- **行主序 / 列主序**：行主序指同一行的相邻元素在存储中也相邻（C 语言的惯例）；列主序则让同一列的相邻元素相邻（Fortran 惯例）。本讲会看到它们只是同一个模型的两组不同参数。
- **整除与取模**：`i // 2` 表示 `i` 除以 2 的商（整除），`i % 2` 表示余数。它们是把一个索引切分成「第几块 + 块内位置」的基本工具：\( i = 2 \cdot (i//2) + (i\%2) \)。
- **四种存储空间（来自 u2-l2）**：GMEM（显存）、SMEM（CTA 内共享内存）、TMEM（Tensor Core 累加器存储）、RF（每线程寄存器）。本讲的布局先只映射到「一个线性地址」，下一讲（u4-l2）才把物理位置扩展到 TMEM 与寄存器的多维坐标。

一个直觉性的动机（书章开篇的原话）：**对同样的数值做同样的计算，仅仅因为数据在物理上的排列方式不同，同一块 GPU 上的性能可以相差一个数量级。**布局决定了 32 个 lane 的访存能否合并成一个事务、共享内存访问是否撞 bank、以及一个 tile 是否具有某个硬件单元要求的字节排列。这就是为什么在写内核之前，必须先学会**精确地描述**布局。

## 3. 本讲源码地图

本讲的核心源码只有一章，但我们会按主题分段精读：

| 文件 | 作用 |
| --- | --- |
| [chapter_data_layout/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md) | 「Data Layout and Its Notation」整章。本讲精读 L28–L171（Shape-Stride 模型、Tile Layout、一般布局函数）；L173 之后的命名轴、replication/offset、swizzle 分别留给 u4-l2、u4-l3、u4-l4 |
| [zh/chapter_data_layout/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_data_layout/index.md) | 同一章的中文镜像，结构与英文版一致，可对照阅读 |
| [_extra/demo/tiled_layout.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiled_layout.html) | 分块布局的交互演示：点击单元格可对照它的 tile 坐标与物理地址（书站在构建后站点的 `_extra/demo/` 路径下以 iframe 嵌入正文） |

章首的三条概览（[chapter_data_layout/index.md:L4-L10](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L4-L10)）概括了全章结构，其中第一条就是本讲的定义：**数据布局把张量的逻辑索引映射到物理位置，这个映射不仅决定程序是否读到正确的数据，还决定全局访存是否合并、共享内存是否 bank conflict、tile 是否符合硬件单元要求的格式。**

## 4. 核心概念与源码讲解

### 4.1 Shape-Stride 模型：逻辑索引如何变成线性地址

#### 4.1.1 概念说明

一个张量的逻辑索引 `(i, j, …)` 并不能告诉我们它的字节存在哪里。**数据布局补上的正是这块缺失的物理信息**：它说明逻辑索引 `(i, j, …)` 处的元素驻留在哪个物理位置——可能在内存、可能在寄存器、也可能在其他硬件存储空间（见 [chapter_data_layout/index.md:L20-L26](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L20-L26)）。

Shape-Stride 模型用两个东西定义这个映射：

- **shape**：每个张量维度的大小；
- **strides**：某个维度的逻辑索引加 1 时，物理位置移动多少个**元素**。

写成记号就是 `S[(shape) : (strides)]`。**一个逻辑索引的物理位置 = 索引向量与步长向量的点积。**

这个模型你其实每天都在用：PyTorch 和 NumPy 的张量内部就是「一个一维 storage 缓冲 + shape/strides 元数据」。

#### 4.1.2 核心流程

对一个布局 \( S[(e_0, e_1) : (s_0, s_1)] \)，二维索引的地址计算是：

\[ \text{addr}(i, j) = i \cdot s_0 + j \cdot s_1 \]

两个最重要的特例（以 `4×4` 矩阵为例）：

| 布局 | 记号 | 地址公式 | 直觉 |
| --- | --- | --- | --- |
| 行主序 | `S[(4, 4) : (4, 1)]` | \( \text{addr}(i,j) = 4i + j \) | 沿行走地址不变快（+1），沿列走 +4 |
| 列主序 | `S[(4, 4) : (1, 4)]` | \( \text{addr}(i,j) = i + 4j \) | 行列角色互换 |

「视图（view）」类操作（转置、`view`、兼容的 `reshape`）**只改 shape 和 strides，不搬动任何数据**——这是该模型威力的直接体现：转置一个上 GB 的张量是 O(1) 的元数据操作。

#### 4.1.3 源码精读

**① 模型定义与行主序例子。**书在 [chapter_data_layout/index.md:L28-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L28-L40) 给出定义：shape 给出每维大小，strides 说明「逻辑索引沿该维加 1 时移动多少个物理元素」，记号写作 `S[(shape) : (strides)]`，物理位置是索引与步长的点积；行主序 `4×4` 矩阵就是 `S[(4, 4) : (4, 1)]`，即 `addr(i, j) = i·4 + j·1`。

**② PyTorch 印证：张量本来就是这个模型。**[chapter_data_layout/index.md:L45-L51](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L45-L51)：

```python
import torch

t = torch.arange(12).reshape(3, 4)
t.shape        # torch.Size([3, 4])
t.stride()     # (4, 1)        ← exactly S[(3, 4) : (4, 1)]
```

这段代码构造 `3×4` 张量并打印 shape 与 stride，注释明确指出 `t.stride()` 返回的 `(4, 1)` 恰好就是 `S[(3, 4) : (4, 1)]`。

**③ 底层仍是一维存储。**[chapter_data_layout/index.md:L53-L57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L53-L57) 指出 `t` 的底层 storage 是一维序列 `[0, 1, 2, …, 11]`；`t` 用 `S[(3, 4) : (4, 1)]` 解释它：每行占 4 个连续元素，相邻列在存储中相邻。

**④ 视图操作只换元数据。**[chapter_data_layout/index.md:L59-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L59-L75)：对二维张量，`permute(1, 0)`（等价于 `t.T`）把 shape 变为 `[4, 3]`、strides 交换为 `(1, 4)`，而 `untyped_storage().data_ptr()` 不变——数据一个都没动。转置视图用 `S[(4, 3) : (1, 4)]`，所以 `tt[i, j]` 的地址偏移是 `i·1 + j·4`，恰好就是 `t[j, i]` 的位置。书还提醒：NumPy 遵循同一模型，只是它的 `.strides` 以**字节**为单位，而 PyTorch 以**元素**为单位。

#### 4.1.4 代码实践

**实践目标**：亲眼验证「PyTorch 张量 = 一维 storage + shape/strides 元数据」，并用自己的点积函数复现 `t[i, j]` 的物理位置。

**操作步骤**（以下为示例代码，任何有 Python 环境的地方都能跑，无需 GPU）：

```python
# 示例代码：验证 shape-stride 模型与 PyTorch 的一致性
import torch

t = torch.arange(12).reshape(3, 4)
print(t.shape, t.stride())            # 预期: torch.Size([3, 4]) (4, 1)

tt = t.permute(1, 0)                  # 或 t.T
print(tt.shape, tt.stride())          # 预期: torch.Size([4, 3]) (1, 4)
print(tt.untyped_storage().data_ptr() == t.untyped_storage().data_ptr())
                                      # 预期: True（同一底层存储）

# 自己实现点积地址函数，与「行主序展开后的位置」对照
def addr(index, strides):
    return sum(i * s for i, s in zip(index, strides))

flat = t.flatten()
for i in range(3):
    for j in range(4):
        # t[i, j] 在行主序 storage 中的位置 = 4*i + j
        assert addr((i, j), t.stride()) == i * 4 + j
        assert flat[addr((i, j), t.stride())] == t[i, j]
print("all assertions passed")
```

**需要观察的现象**：

1. `t.stride()` 打印 `(4, 1)`，与书中 `S[(3, 4) : (4, 1)]` 完全一致；
2. 转置后 strides 变成 `(1, 4)` 而 `data_ptr` 不变——证明转置只是换了「解释方式」；
3. 断言全部通过，说明点积公式算出的地址确实能取回正确的元素。

**预期结果**：脚本输出 `all assertions passed`。（以上断言基于书中 L45–L75 的行为说明与点积定义推演，属确定性逻辑；请运行后核对。）

#### 4.1.5 小练习与答案

**练习 1**：布局 `S[(3, 4) : (4, 1)]` 中，元素 `(2, 3)` 的地址偏移是多少？

**答案**：\( \text{addr}(2,3) = 2 \cdot 4 + 3 \cdot 1 = 11 \)。它正是该张量行主序展开后的第 12 个元素（下标 11）。

**练习 2**：一个形状为 `(4, 3)` 的张量 `tt` 的 strides 是 `(1, 4)`。它的第 0 行（`tt[0, :]`）在存储中是否连续？

**答案**：不连续。沿第 1 维（列）走 stride 是 4，即 `tt[0, 0]`、`tt[0, 1]`、`tt[0, 2]` 的地址是 `0, 4, 8`，中间隔着别的元素——这正是转置视图（列主序读行主序存储）的典型形态。反过来 `tt[:, 0]`（第 0 列）才是连续的（stride 为 1）。

**练习 3**：为什么 NumPy 的 `.strides` 与 PyTorch 的 `.stride()` 数值可能不同，即使两个张量逻辑排列一样？

**答案**：单位不同。NumPy 以字节计（`int64` 的 `(3,4)` 行主序 strides 是 `(32, 8)`），PyTorch 以元素计（`(4, 1)`）。书在 [chapter_data_layout/index.md:L74-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L74-L75) 明确指出了这一点。

### 4.2 Tiling：把索引再切分为四维坐标

#### 4.2.1 概念说明

GPU 内核很少一次处理整个矩阵，而是把它切成小的 **tile**。于是一个元素的物理位置需要**两层信息**才能描述：

1. 这个元素属于**哪个 tile**（tile 在 tile 网格中的行、列坐标）；
2. 它是**tile 内的哪个元素**（tile 内的行、列坐标）。

也就是说，分块不是新发明一套机制，而是对原始索引 `(i, j)` 做一次**再切分（re-split）**：

\[ (i, j) \;\longrightarrow\; (\underbrace{i//2}_{\text{tile\_row}},\ \underbrace{i\%2}_{\text{row\_in\_tile}},\ \underbrace{j//4}_{\text{tile\_col}},\ \underbrace{j\%4}_{\text{col\_in\_tile}}) \]

关键洞察：切分之后的四个坐标**仍然可以套用 Shape-Stride 模型**——分块布局无非是一个四维的 `S[(shape) : (strides)]`。

#### 4.2.2 核心流程

以书中例子为主线：把 `8×8` 矩阵切成 **2×4 大小的 tile**（每个 tile 2 行 4 列，tile 网格为 4 行 × 2 列），tile 之间按行主序存放，tile 内部也按行主序存放。推导分四步：

1. **扁平化**：从逻辑坐标 `(i, j)` 出发，按原始 `8×8` 形状展平：\( x = i \cdot 8 + j \)。
2. **确定切分 shape**：行坐标 `i` 拆成「4 个 tile 行 × 每 tile 2 行」，列坐标 `j` 拆成「2 个 tile 列 × 每 tile 4 列」，所以分解 `x` 用的 shape 是 `(4, 2, 2, 4)`。
3. **unflatten**（行主序意义下的多维分解，高位在前）：

   \[ (c_0, c_1, c_2, c_3) = \operatorname{unflatten}(x;\ 4, 2, 2, 4) \]

   即 \( c_0 = x // 16,\ c_1 = (x//8)\%2,\ c_2 = (x//4)\%2,\ c_3 = x\%4 \)。

4. **代入化简**（把 \( x = 8i + j \) 代回去）：

   \[ c_0 = i//2 = \text{tile\_row},\quad c_1 = i\%2 = \text{row\_in\_tile},\quad c_2 = j//4 = \text{tile\_col},\quad c_3 = j\%4 = \text{col\_in\_tile} \]

最后一步是**用物理排列反推 strides**（见 4.2.3 的 ③）：每个 tile 有 \( 2 \times 4 = 8 \) 个元素，每行 tile 有 2 个 tile，tile 内每行有 4 个连续元素，于是

\[ f_D(x) = (c_0 \cdot 2 + c_2) \cdot 8 + c_1 \cdot 4 + c_3 = c_0 \cdot 16 + c_1 \cdot 4 + c_2 \cdot 8 + c_3 \cdot 1 \]

#### 4.2.3 源码精读

**① 分块问题设定。**[chapter_data_layout/index.md:L77-L82](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L77-L82)：GPU 内核通常把矩阵切成小 tile；书选择把 `8×8` 矩阵切成 `2×4` tile，tile 按行主序存放、tile 内元素也按行主序存放，并配了一张交互图（[L85-L88](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L85-L88) 的 iframe 嵌入 `demo/tiled_layout.html`，见本章第 3 节源码地图）。

**② flatten 与 unflatten。**[chapter_data_layout/index.md:L90-L117](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L90-L117)：描述这个排列需要「tile 在矩阵中的位置」和「元素在 tile 内的位置」两个层级。先按原始 `8×8` 形状把 `(i, j)` 展平成 `x = i·8 + j`；分块后行坐标拆成 4 个 tile 行 × 每 tile 2 行，列坐标拆成 2 个 tile 列 × 每 tile 4 列，因此分解 `x` 的 shape 是 `(4, 2, 2, 4)`；布局函数按这个 shape 做 `unflatten`，书给出四个整除/取模公式（`c0 = x // 16`、`c1 = (x // 8) % 2`、`c2 = (x // 4) % 2`、`c3 = x % 4`）。

**③ 从物理排列到地址。**[chapter_data_layout/index.md:L119-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L119-L43)：把 `x = i·8 + j` 代回 unflatten 公式化简，得到 `c0..c3` 分别是 `tile_row / row_in_tile / tile_col / col_in_tile`；再按「每个 tile 含 `2×4=8` 个元素、每个 tile 行含 2 个 tile、tile 内每行含 4 个连续元素」把四个坐标映射到物理地址，展开成点积形式后得到最终布局：

```text
S[(4, 2, 2, 4) : (16, 4, 8, 1)]
```

注意 strides 的对应关系：`tile_row` 步长 16（一个 tile 行 = 2 个 tile × 8 元素）、`row_in_tile` 步长 4（tile 内一行）、`tile_col` 步长 8（一个 tile）、`col_in_tile` 步长 1。书在 [L145-L146](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L145-L146) 提示：在交互图里点击任意单元格，即可对照它的 tile 坐标、物理地址与 unflatten 过程及 \( f_D(x) \)。

#### 4.2.4 代码实践

**实践目标**：用代码验证书中 `unflatten` 公式与化简结果 `(c0, c1, c2, c3) = (i//2, i%2, j//4, j%4)` 完全一致。

**操作步骤**（示例代码，无需 GPU）：

```python
# 示例代码：实现 unflatten 并验证书中 tile 坐标公式
def unflatten(x, shape):
    """按行主序把扁平索引 x 拆成各维坐标（高位维在前，与书中公式一致）。"""
    coords = []
    for e in reversed(shape):
        coords.append(x % e)
        x //= e
    return tuple(reversed(coords))

for i in range(8):
    for j in range(8):
        x = i * 8 + j
        c0, c1, c2, c3 = unflatten(x, (4, 2, 2, 4))
        assert (c0, c1, c2, c3) == (i // 2, i % 2, j // 4, j % 4)
print("unflatten matches the book's formulas")

# 抽查一个点：x = 47 对应 (i, j) = (5, 7)
print(unflatten(47, (4, 2, 2, 4)))    # 预期: (2, 1, 1, 3)
```

**需要观察的现象**：全部 64 个 `(i, j)` 的断言通过；抽查点 `x = 47` 拆出 `(2, 1, 1, 3)`，即第 3 个 tile 行、tile 内第 2 行、第 2 个 tile 列、tile 内第 4 列。

**预期结果**：输出 `unflatten matches the book's formulas` 与 `(2, 1, 1, 3)`。（基于书中 L111–L126 的公式推演，属确定性逻辑；请运行后核对。）然后打开交互演示（构建站点中的 `demo/tiled_layout.html`，或在 GitHub 上直接查看 [_extra/demo/tiled_layout.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiled_layout.html)），点击几个单元格，把图上显示的 tile 坐标和物理地址与你代码算出的对照。

#### 4.2.5 小练习与答案

**练习 1**：元素 `(i, j) = (5, 7)` 在 `S[(4, 2, 2, 4) : (16, 4, 8, 1)]` 下的物理地址是多少？

**答案**：\( c = (5//2,\ 5\%2,\ 7//4,\ 7\%4) = (2, 1, 1, 3) \)，\( \text{addr} = 2 \cdot 16 + 1 \cdot 4 + 1 \cdot 8 + 3 \cdot 1 = 47 \)。有趣的是它恰好等于 `x = 5·8+7 = 47`——这只是巧合（该元素碰巧在「正确」的相对位置），换 `(0, 4)` 试：\( c = (0,0,1,0) \)，\( \text{addr} = 8 \ne 4 \)。

**练习 2**：为什么 `unflatten(x; 4, 2, 2, 4)` 里的 shape 是 `(4, 2, 2, 4)` 而不是 `(2, 4, 2, 4)`（「2×4 tile」的字面顺序）？

**答案**：unflatten 的 shape 是**分解扁平索引 x 的各维大小**，顺序由「行坐标的两半在前、列坐标的两半在后」的展平顺序决定：`x = i·8 + j` 中 `i` 是高位、`j` 是低位，`i` 拆成 (tile 行数 4, 每 tile 行数 2)，`j` 拆成 (tile 列数 2, 每 tile 列数 4)，所以是 `(4, 2, 2, 4)`。「2×4」描述的是单个 tile 的形状，不是分解顺序。

**练习 3**：如果把 tile 内部改成**列主序**存放（tile 间仍按行主序），strides 会怎么变？

**答案**：shape 不变，仍是 `(4, 2, 2, 4)`；tile 内列主序意味着 `col_in_tile` 的步长是 2（一列有 2 个元素）、`row_in_tile` 的步长是 1，所以 strides 变为 `(16, 1, 8, 2)`。这印证了 4.3 节的主题：shape 管「怎么切」，strides 管「怎么放」，两者独立。

### 4.3 一般布局函数：S[(e) : (s)] 的统一形式

#### 4.3.1 概念说明

把 4.1 的点积公式和 4.2 的切分技巧合起来，就得到**一般形式的布局函数**。对任意布局

\[ S[(e_0, e_1, \ldots, e_{n-1}) : (s_0, s_1, \ldots, s_{n-1})] \]

和一个扁平的逻辑索引 \( x \)（按 shape 行主序展平），布局函数分两步：

1. 按 shape 分解：\[ (c_0, c_1, \ldots, c_{n-1}) = \operatorname{unflatten}(x;\ e_0, e_1, \ldots, e_{n-1}) \]
2. 与 strides 点积：\[ f_D(x) = \sum_{k=0}^{n-1} c_k \cdot s_k \]

这就是全书的统一语言：

- **shape 决定 \( x \) 如何被分解成坐标**（怎么切分索引）；
- **strides 决定这些坐标如何映射到物理位置**（怎么摆放数据）。

两个自由度正交：同一个 shape 配不同 strides，就是「同一种切分、不同物理排列」（4.2 练习 3 已演示）。4.1 的行主序 `S[(4,4):(4,1)]`、4.2 的分块 `S[(4,2,2,4):(16,4,8,1)]`，都只是这同一个公式的两组参数。

#### 4.3.2 核心流程

拿到一个想要的物理排列，写出布局的通用流程：

1. 写出逻辑张量形状，把逻辑坐标展平成 \( x \)；
2. 想清楚「索引切分的层级」（例如分块 → 4 维；更复杂的层级 → 更多维），得到 shape；
3. 按物理存放顺序，从最外层到最内层数出「每走一层跨多少元素」，得到 strides；
4. 用 \( f_D(x) = \sum_k c_k s_k \) 验证若干抽样点。

反过来，读到一个陌生布局时的流程：先由 shape 写出 unflatten 公式，再与 strides 点积，就得到每个元素的物理地址——这正是读 GPU 内核代码、TMA 描述符、TMEM 布局说明时的通用解码手段。

#### 4.3.3 源码精读

一般布局函数的完整定义在 [chapter_data_layout/index.md:L148-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L148-L171)。这段源码只有三个要点：

1. 布局写成 `S[(e0, e1, ..., en-1) : (s0, s1, ..., sn-1)]`（[L152-L154](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L152-L154)）；
2. 对扁平逻辑索引 \( x \)，先按 shape 做 unflatten（[L156-L162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L156-L162)）；
3. 再取坐标与 strides 的点积得到 \( f_D(x) \)（[L164-L167](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L164-L167)）。

书的收束句（[L169-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L169-L171)）值得背下来：**shape 决定 \( x \) 如何分解为坐标，strides 决定坐标如何映射到物理位置；前面的 tile 布局就是选了 shape `(4, 2, 2, 4)`、strides `(16, 4, 8, 1)` 的结果。**

顺带预告：本章后续内容都是对 \( f_D(x) \) 的扩展——[L173-L177](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L173-L177) 指出，有些存储空间（TMEM、寄存器 fragment）一个线性地址不够用，需要**命名轴**（`@TLane`、`@TCol`、`@laneid`、`@reg`），那是 u4-l2 的主题；`R[...]` 复制与偏移（[L247 起](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L247-L249)）是 u4-l3；swizzle（[L430 起](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L430-L432)）是 u4-l4。

#### 4.3.4 代码实践

**实践目标**：实现本讲规格要求的核心任务——一个通用的 shape-stride → 线性地址映射函数，并对**行主序、列主序、(2,3) 分块**布局各写测试用例验证正确性（外加书中 `2×4` 分块作为基准）。

**操作步骤**（示例代码，无需 GPU；`(2,3)` 分块指每个 tile 2 行 3 列，取 `6×6` 矩阵、tile 网格 3×2，方法与书中 `2×4` 例子完全同构）：

```python
# 示例代码：通用布局函数 f_D(x) 与三类布局测试

def unflatten(x, shape):
    coords = []
    for e in reversed(shape):
        coords.append(x % e)
        x //= e
    return tuple(reversed(coords))

def layout_addr(shape, strides, x):
    """S[(shape):(strides)] 的一般布局函数 f_D(x)。"""
    return sum(c * s for c, s in zip(unflatten(x, shape), strides))

def flatten(index, logical_shape):
    """逻辑多维索引 -> 行主序扁平索引 x。"""
    x = 0
    for i, e in zip(index, logical_shape):
        x = x * e + i
    return x

# ---- 测试 1：行主序 8x8，S[(8,8):(8,1)] ----
for i in range(8):
    for j in range(8):
        x = flatten((i, j), (8, 8))
        assert layout_addr((8, 8), (8, 1), x) == 8 * i + j

# ---- 测试 2：列主序 8x8，S[(8,8):(1,8)] ----
for i in range(8):
    for j in range(8):
        x = flatten((i, j), (8, 8))
        assert layout_addr((8, 8), (1, 8), x) == i + 8 * j

# ---- 测试 3：书中的 2x4 分块，S[(4,2,2,4):(16,4,8,1)] ----
for i in range(8):
    for j in range(8):
        x = flatten((i, j), (8, 8))
        expect = 16 * (i // 2) + 4 * (i % 2) + 8 * (j // 4) + (j % 4)
        assert layout_addr((4, 2, 2, 4), (16, 4, 8, 1), x) == expect

# ---- 测试 4：(2,3) 分块：6x6 矩阵、tile 2 行 3 列 ----
# 手推（与书 L100-L137 同法）：
#   shape = (tile行数 3, tile内行数 2, tile列数 2, tile内列数 3) = (3,2,2,3)
#   strides：col_in_tile=1，row_in_tile=3，tile_col=2*3=6，tile_row=2*6=12
#   => S[(3,2,2,3) : (12,3,6,1)]
for i in range(6):
    for j in range(6):
        x = flatten((i, j), (6, 6))
        expect = 12 * (i // 2) + 3 * (i % 2) + 6 * (j // 3) + (j % 3)
        assert layout_addr((3, 2, 2, 3), (12, 3, 6, 1), x) == expect

print("all layout tests passed")
```

**需要观察的现象**：四个测试全部通过。特别留意测试 4 中 `(i, j) = (2, 4)` 这个点：\( x = 16 \)，unflatten 得 `(1, 0, 1, 1)`，地址 \( = 12 + 0 + 6 + 1 = 19 \)；对照物理排列——tile(1,1) 从地址 18 开始，元素 `(2,4)` 是该 tile 的第 2 个元素，地址正是 19。

**预期结果**：输出 `all layout tests passed`。（测试 3、4 的期望公式均按书中方法手推并逐点核对；请运行后确认。）

#### 4.3.5 小练习与答案

**练习 1**：把 `64×64` 矩阵切成 `32×32` 的 tile（tile 间行主序、tile 内行主序），写出布局记号。

**答案**：shape：tile 行数 2、tile 内行数 32、tile 列数 2、tile 内列数 32，即 `(2, 32, 2, 32)`；strides：`col_in_tile=1`、`row_in_tile=32`、`tile_col=1024`、`tile_row=2048`，即 `S[(2, 32, 2, 32) : (2048, 32, 1024, 1)]`。

**练习 2**：tile 形状取什么值时，分块布局会退化为普通行主序？

**答案**：当 tile 的行数为 1（`tile_m = 1`）且 tile 列数等于整行宽度时（`tile_n = N`），`i//1 = i`、`i%1 = 0`、`j//N = 0`、`j%N = j`，布局退化为 `S[(M, 1, 1, N) : (N·1, 0, 0, 1)]`，地址就是 \( N \cdot i + j \)。更极端地，tile 取 `1×1` 时任何「分块」都消失。这说明行主序只是分块布局族的退化端点。

**练习 3**：一个布局的 shape 是 `(4, 2, 2, 4)`、strides 是 `(16, 4, 8, 1)`；另一个布局 shape 相同、strides 是 `(8, 16, 1, 4)`。它们的逻辑切分一样吗？物理排列一样吗？

**答案**：逻辑切分完全一样（同一个 shape → 同一套 unflatten），物理排列完全不同（strides 不同 → 同一个 \( x \) 映射到不同地址）。这正是「shape 管切分、strides 管摆放」两个自由度正交的体现。

## 5. 综合实践

**任务**：实现一个「分块重排观察器」，把本讲三个模块串起来——flatten（4.1）→ unflatten（4.2）→ 点积（4.3），并用 PyTorch 的 `as_strided` 从另一个方向交叉验证。

**要求**：

1. 写一个生成器 `tile_block_layout(M, N, tile_m, tile_n)`，返回 `(shape, strides)`，规则：tile 间行主序、tile 内行主序（与书中 8×8 例子同一规则）；
2. 写一个重排函数 `reorder(t, tile_m, tile_n)`：输入行主序张量 `t`（形状 `M×N`），按布局把元素搬进新的物理序列；
3. 用 `torch.as_strided` 在重排后的 storage 上按 `(shape, strides)` 取回四维视图，断言 `view[a, b, c, d] == t[2*a+b...]`（一般式为 `t[tile_m*a + b, tile_n*c + d]`）——这一步从「物理侧」反向确认布局函数正确；
4. 分别对书中的 `(M=8, N=8, tile 2×4)` 与规格要求的 `(M=6, N=6, tile 2×3)` 运行，打印前 24 个「逻辑扁平索引 x → 物理地址」对照表。

**参考实现骨架**（示例代码）：

```python
import torch

def tile_block_layout(M, N, tile_m, tile_n):
    shape = (M // tile_m, tile_m, N // tile_n, tile_n)
    strides = (
        (N // tile_n) * tile_m * tile_n,  # tile_row：一个 tile 行的元素数
        tile_n,                            # row_in_tile
        tile_m * tile_n,                   # tile_col：一个 tile
        1,                                 # col_in_tile
    )
    return shape, strides

def reorder(t, tile_m, tile_n):
    M, N = t.shape
    shape, strides = tile_block_layout(M, N, tile_m, tile_n)
    out = torch.empty(M * N, dtype=t.dtype)
    for i in range(M):
        for j in range(N):
            x = i * N + j
            a, b, c, d = unflatten(x, shape)          # 复用 4.3.4 的 unflatten
            out[a*strides[0] + b*strides[1] + c*strides[2] + d*strides[3]] = t[i, j]
    return out

t = torch.arange(64).reshape(8, 8)
out = reorder(t, 2, 4)
shape, strides = tile_block_layout(8, 8, 2, 4)
view = out.as_strided(shape, strides)
for a in range(4):
    for b in range(2):
        for c in range(2):
            for d in range(4):
                assert view[a, b, c, d] == t[2*a + b, 4*c + d]
print("as_strided cross-check passed")
```

**预期结果**：断言全部通过。这个实践里最值得体会的一点是：**`as_strided` 能工作，恰恰证明 `S[(4,2,2,4):(16,4,8,1)]` 描述的是一套独立的物理排列**——你不能对原始行主序张量直接套这组 strides 得到分块视图（那会把 `row_in_tile` 与 `tile_col` 的步长搞混），必须先做一次真正的数据重排。布局函数描述的正是「逻辑读序」与「物理写序」之间的这张映射表。对比 `(2,3)` 分块打印出的对照表与 `(2,4)` 的差异，直观感受 tile 形状如何改变整张映射。

## 6. 本讲小结

- **数据布局 = 逻辑索引到物理位置的映射**；它不仅决定读到对的数据，还决定访存合并、bank conflict 与硬件单元的格式要求。
- **Shape-Stride 模型**：`S[(shape) : (strides)]`，物理位置 = 索引与步长的点积；PyTorch/NumPy 张量内部就是这个模型，视图操作（转置、`view`）只改 shape/strides、不搬数据。
- **分块 = 对索引再切分**：`(i, j)` 拆成 `(tile_row, row_in_tile, tile_col, col_in_tile)`，通过「flatten → unflatten」完成，书中的 `8×8 → 2×4 tile` 例子得到 `S[(4, 2, 2, 4) : (16, 4, 8, 1)]`。
- **一般布局函数** \( f_D(x) = \sum_k c_k s_k \)：shape 决定 \( x \) 如何分解、strides 决定坐标如何落地，两个自由度正交，行主序只是这个族的一个退化端点。
- 这套记号是全书的地基：后续命名轴、replication/offset、swizzle 都是在 \( f_D(x) \) 上做扩展。

## 7. 下一步学习建议

下一讲 **u4-l2「命名轴：从线性地址到物理坐标」** 把本讲的 \( f_D(x) \) 从「返回一个整数地址」扩展为「返回一组命名物理坐标」：TMEM 的二维 `@TLane/@TCol` 地址空间与 warp 寄存器 fragment 的 `@laneid/@reg` 坐标。建议先阅读 [chapter_data_layout/index.md:L173-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L173-L205)（命名轴与 TMEM 二维地址空间），思考一个问题作热身：本讲的 `addr(i, j) = 4i + j` 只有一个返回值，那 `S[(128, 256) : (1@TLane, 1@TCol)]` 的 \( f_D(x) \) 返回什么？之后再依次进入 u4-l3（replication 与 offset）、u4-l4（swizzle），最后在 TMA 与 GEMM 章节里看到这些布局记号如何被真实硬件路径消费。
