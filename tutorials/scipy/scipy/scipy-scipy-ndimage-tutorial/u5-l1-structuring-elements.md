# 结构元 generate_binary_structure 与 iterate_structure

## 1. 本讲目标

本讲是「形态学」单元（u5）的第一篇。形态学操作（侵蚀、膨胀、开闭运算、距离变换……）都离不开一个共同的角色——**结构元（structuring element）**：它是一小块布尔数组，用来定义「当前像素的哪些邻居参与运算」。

读完本讲，你应该能够：

- 说清楚 `generate_binary_structure(rank, connectivity)` 里 `rank` 与 `connectivity` 各自的含义，以及 `connectivity` 为何取值范围是 `1..rank`。
- 用一句话解释 `generate_binary_structure` 的核心公式 `np.add.reduce(np.fabs(np.indices([3]*rank) - 1)) <= connectivity` 算的是什么。
- 理解 `iterate_structure` 如何通过「自我膨胀」把一个最小结构元扩张成更大的等价结构元，并能推导输出形状公式。
- 知道 `_center_is_true` 这个内部辅助函数为何会决定侵蚀算法走「快速路径」还是「暴力路径」。

承接：本讲依赖 u1-l4 讲过的 `_normalize_sequence`、`_get_output` 等共享支撑工具，也用到 u2-l2 提过的「邻域/footprint」概念。本讲产出的结构元，将被 u5-l2（二值形态学）和 u5-l3（灰度形态学）直接使用。

## 2. 前置知识

- **邻域与 footprint**：在 ndimage 里，很多操作都是「以每个像素为中心，取它周围一小块区域做计算」。这块区域由一个布尔数组描述，True 表示该邻居参与，False 表示不参与。在滤波单元里它叫 footprint，在形态学里它叫 structure（结构元）。
- **L1 距离（曼哈顿距离）**：两点在各坐标轴上位移的绝对值之和。例如 2D 网格里，中心点到正上方/正左方邻居的 L1 距离是 1，到对角邻居的 L1 距离是 2。
- **连通性（connectivity）**：在离散网格上判断两个像素是否「相邻」的规则。只认上下左右（4-连通）是一种规则，连对角线也算（8-连通）是另一种规则。结构元就是这种规则的具象化。
- **膨胀（dilation）**：把一个前景集合按结构元「撑大」——如果结构元是一个十字，膨胀一步就把每个前景点变成一个十字。`iterate_structure` 内部正是用 `binary_dilation` 来扩张结构元自身。

## 3. 本讲源码地图

本讲全部内容集中在 `_morphology.py` 这一个文件里，涉及三个函数：

| 函数 | 行号 | 作用 |
|---|---|---|
| `_center_is_true` | [_morphology.py:48-52](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L48-L52) | 内部辅助：判断结构元在「考虑 origin 偏移后」的中心格是否为 True |
| `iterate_structure` | [_morphology.py:55-121](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L55-L121) | 把一个结构元自我膨胀 `iterations` 次，得到更大的等价结构元 |
| `generate_binary_structure` | [_morphology.py:124-213](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L124-L213) | 按 `rank` 和 `connectivity` 生成一个 `3×3×…×3` 的最小结构元 |

此外会顺带引用：

- `_binary_erosion` 中对 `_center_is_true` 返回值的使用：[_morphology.py:253](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L253)，以及随后据此分发的算法路径 [_morphology.py:265-298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L265-L298)。
- C 端侵蚀宏 `CASE_NI_ERODE_POINT` 中 `_center_is_true` 的快速分支：[src/ni_morphology.c:51-83](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_morphology.c#L51-L83)。
- 测试用例 `test_generate_structure0*` 与 `test_iterate_structure0*`：[tests/test_morphology.py:693-763](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_morphology.py#L693-L763)。

## 4. 核心概念与源码讲解

### 4.1 generate_binary_structure：用一个公式生成最小结构元

#### 4.1.1 概念说明

`generate_binary_structure(rank, connectivity)` 生成形态学运算中最常用的「最小结构元」：它一定是一个边长全为 3 的 `rank` 维布尔超立方体（2D 是 3×3，3D 是 3×3×3），中心格永远是 True，其余格子是否为 True 取决于 `connectivity`。

两个参数的直觉：

- `rank` = 数组的维度数（等于 `np.ndim(array)`）。它决定结构元有几维。
- `connectivity` = 「邻接到中心需要多近」。`connectivity=1` 表示只把轴向邻居（上下左右等）算作邻居，不含任何对角；`connectivity=rank` 表示把整个 `3^rank` 邻域全部算作邻居。所以合法范围是 `1..rank`。

举两个 2D 例子（`rank=2`）：

- `connectivity=1` → 十字形（4-连通）：

  ```
  0 1 0
  1 1 1
  0 1 0
  ```

- `connectivity=2` → 全 3×3 块（8-连通）：

  ```
  1 1 1
  1 1 1
  1 1 1
  ```

#### 4.1.2 核心流程

核心思想：在 `3×3×…×3` 的超立方体里，**每个格子的坐标偏移量都在 `{-1, 0, 1}` 中**。把每个格子的「到中心的 L1 距离」算出来，再与 `connectivity` 比较即可。

对位于偏移 \((o_1, o_2, \ldots, o_r)\) 的格子，它到中心的 L1 距离是：

\[
d_1(o_1, \ldots, o_r) = \sum_{i=1}^{r} |o_i|
\]

该格子被纳入结构元，当且仅当：

\[
d_1(o_1, \ldots, o_r) \le \text{connectivity}
\]

由于这里每个 \(o_i \in \{-1, 0, 1\}\)，有 \(|o_i| = o_i^2\)，所以 L1 距离恰好等于平方欧氏距离：

\[
\sum_{i=1}^{r} |o_i| = \sum_{i=1}^{r} o_i^2 = d_2^2
\]

这正是官方 docstring 里「squared distance … up to connectivity」一说的来历——在 `{-1,0,1}` 偏移范围内，两种距离数值相等。代码选择了用 `np.fabs`（绝对值）来算。

由此也能解释 `connectivity` 的取值范围：

- 最小值 1：只保留 \(d_1 \le 1\)，即只有「恰好一个轴偏移 1」的轴向邻居。
- 最大值 `rank`：保留 \(d_1 \le \text{rank}\)，而最远对角格的 \(d_1 = \text{rank}\)（所有轴都偏移 1），于是整个邻域全 True。
- 若传入 `connectivity < 1`，代码会把它钳到 1（见下文）；传入大于 `rank` 的值则等价于 `rank`（再多也只是全 True）。

执行步骤（伪代码）：

```
1. 若 connectivity < 1：connectivity = 1     # 不允许「只有中心」
2. 若 rank < 1：返回标量 True                 # 0 维退化
3. idx = np.indices([3]*rank)                  # 形状 (rank, 3, 3, ..., 3)
4. idx = np.fabs(idx - 1)                      # 每格到中心的各轴绝对偏移
5. d  = np.add.reduce(idx, axis=0)             # 沿 rank 轴求和 = L1 距离
6. return d <= connectivity                    # 布尔结构元
```

#### 4.1.3 源码精读

完整实现非常短，核心只有 7 行：

```python
# _morphology.py:207-213
if connectivity < 1:
    connectivity = 1
if rank < 1:
    return np.array(True, dtype=bool)
output = np.fabs(np.indices([3] * rank) - 1)
output = np.add.reduce(output, 0)
return output <= connectivity
```

[_morphology.py:207-213](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L207-L213) —— 这里 `np.indices([3] * rank)` 生成「形状为 `(rank, 3, 3, ..., 3)`、第 `i` 个切片是沿第 `i` 轴的坐标网格」的数组；减 1 把坐标原点移到中心；`np.fabs` 取绝对值；`np.add.reduce(output, 0)` 沿第 0 轴（即 `rank` 那一维）求和，得到每格的 L1 距离；最后 `<= connectivity` 转成布尔掩码。

手算验证 `rank=2, connectivity=1`：

```
np.indices([3,3]) - 1 的两个切片：
  轴0偏移 = [[-1,-1,-1],[ 0, 0, 0],[ 1, 1, 1]]
  轴1偏移 = [[-1, 0, 1],[-1, 0, 1],[-1, 0, 1]]
fabs 后逐元素相加（L1 距离）：
  d = [[2,1,2],[1,0,1],[2,1,2]]
d <= 1：
  [[0,1,0],[1,1,1],[0,1,0]]     # 正是十字形
```

这与 docstring 给出的示例、测试 `test_generate_structure03`（[tests/test_morphology.py:703-708](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_morphology.py#L703-L708)）完全一致。

#### 4.1.4 代码实践

**实践目标**：亲手生成并打印 `rank=3` 时两种 `connectivity` 的结构元，验证「`connectivity` 控制是否含对角邻居」。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import ndimage

# rank=3：结构元是 3x3x3
s1 = ndimage.generate_binary_structure(3, 1)   # 只含轴向邻居
s2 = ndimage.generate_binary_structure(3, 2)   # 含「一格对角」邻居

print("connectivity=1, True 总数 =", s1.sum())
print(s1.astype(int))

print("connectivity=2, True 总数 =", s2.sum())
print(s2.astype(int))
```

**需要观察的现象**：

- `s1` 只有 7 个 True：中心 + 3D 里 6 个轴向邻居（上下、左右、前后各 1）。
- `s2` 有 19 个 True（中心 + 6 个轴向 + 12 个「恰在一个轴上不偏移、另两轴偏移 1」的对角格）。
- 沿任一轴把 `s1` 切成 3 个 3×3 层：中间层（该轴偏移 0）是个 2D 十字（5 个 True），上下两层（该轴偏移 ±1）各只剩 1 个 True（即该轴方向的那个面邻居，位于层中心）。而 `s2` 的中间层是满的 3×3（9 个 True），上下两层各是一个 2D 十字（5 个 True），合计 9+5+5=19。

**预期结果**：`s1.sum() == 7`，`s2.sum() == 19`。`s1` 沿任一轴的中间切片都打印为：

```
0 1 0
1 1 1
0 1 0
```

> 待本地验证：具体打印格式取决于 numpy 的打印宽度，3D 数组会按第一轴分页显示。

#### 4.1.5 小练习与答案

**练习 1**：`generate_binary_structure(2, 3)` 会得到什么？为什么？

**答案**：得到一个全 True 的 3×3 数组。因为 `connectivity=3 > rank=2`，条件 `d_1 <= 3` 对所有格子（最远 `d_1=2`）都成立，效果与 `connectivity=rank=2` 相同。

**练习 2**：为什么 `generate_binary_structure` 注释里说它只能生成「边长为 3」的最小结构元？想要更大的结构元该怎么办？

**答案**：因为 `np.indices([3]*rank)` 写死了每维长度为 3。要更大的结构元，可以用 `iterate_structure` 把它扩张（见 4.2），或直接用 `numpy` 自行构造（如 `np.ones((5,5), dtype=bool)`），官方 docstring 的 Notes 段（[_morphology.py:152-157](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L152-L157)）明确说明了这一点。

---

### 4.2 iterate_structure：把结构元自我膨胀

#### 4.2.1 概念说明

`generate_binary_structure` 只能给出最小的 3×3×… 邻域。但很多时候我们需要更大的结构元——例如要一次侵蚀掉好几个像素厚的外壳。`iterate_structure(structure, iterations)` 解决的就是这件事：它把传入的结构元「自我膨胀」若干次，得到一个更大的、**等价于「用原结构元做 `iterations` 次膨胀」**的新结构元。

关键直觉：连续做 `n` 次膨胀 \(\delta_B\)，等价于用「膨胀 \(n\) 次后的结构元」做一次膨胀：

\[
\delta_B^n(A) = \delta_{\delta_B^{n-1}(B)}(A)
\]

所以与其在侵蚀/膨胀时循环 `iterations` 次，不如先用 `iterate_structure` 算出「等效大结构元」，再用它做一次运算。这正是 `_binary_erosion` 内部对多次迭代做加速的思路（见 4.3）。

#### 4.2.2 核心流程

设原结构元每维长度为 \(s_i\)，膨胀次数 `ni = iterations - 1`（原结构元自身算第 1 次）。每做一次自我膨胀，结构元在每个维度上向外扩展 \((s_i - 1)\)。因此输出结构元的每维长度为：

\[
\text{shape}_i = s_i + n_i \cdot (s_i - 1), \qquad n_i = \text{iterations} - 1
\]

执行步骤（伪代码）：

```
1. 若 iterations < 2：直接返回 structure 的副本（无需扩张）
2. ni = iterations - 1
3. 按上面公式算出新形状 shape
4. 算出原结构元在新画布中的居中放置位置 pos
5. 在全零画布上把原结构元贴到中央（out[slc] = structure != 0）
6. 用原 structure 作为结构元，对画布做 binary_dilation(out, structure, iterations=ni)
7. 若调用者传了 origin：把每个轴的 origin 乘以 iterations 后一起返回
```

为什么要先把原结构元「贴到中央再膨胀」，而不是直接膨胀一个标量点？因为这样得到的扩张形状边界更可控，且 `binary_dilation` 接收的是数组而非单点。

`origin` 形参的含义：原结构元在膨胀时若带有 `origin`（锚点偏移），那么多次迭代后锚点也会等比例偏移，所以返回时把 `origin` 乘以 `iterations` 一并交还（见 [tests/test_morphology.py:748-763](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_morphology.py#L748-L763) 的 `test_iterate_structure03`）。

#### 4.2.3 源码精读

```python
# _morphology.py:105-121
structure = np.asarray(structure)
if iterations < 2:
    return structure.copy()
ni = iterations - 1
shape = [ii + ni * (ii - 1) for ii in structure.shape]
pos = [ni * (structure.shape[ii] // 2) for ii in range(len(shape))]
slc = tuple(slice(pos[ii], pos[ii] + structure.shape[ii], None)
            for ii in range(len(shape)))
out = np.zeros(shape, bool)
out[slc] = structure != 0
out = binary_dilation(out, structure, iterations=ni)
if origin is None:
    return out
else:
    origin = _ni_support._normalize_sequence(origin, structure.ndim)
    origin = [iterations * o for o in origin]
    return out, origin
```

[_morphology.py:105-121](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L105-L121) —— 逐行说明：

- `shape = [ii + ni*(ii-1) ...]`：上面推导的形状公式。
- `pos`：原结构元贴到新画布中央的起始坐标，等于 `ni * (s_i // 2)`，保证居中。
- `slc`：构造一个多维 `slice`，把原结构元精准贴到 `out` 的中央。
- `out[slc] = structure != 0`：把任意非零值都规范化成布尔 True 再贴入。
- `out = binary_dilation(out, structure, iterations=ni)`：以原结构元为核，膨胀 `ni` 次。
- 末尾的 `origin` 分支用到了 u1-l4 讲过的 `_ni_support._normalize_sequence`，把标量 origin 规范成各轴序列后再按 `iterations` 放大。

手算验证 `iterations=2`、原结构元为 3×3 十字（`s_i=3`，`ni=1`）：

\[
\text{shape}_i = 3 + 1 \times (3-1) = 5
\]

输出是 5×5，原十字贴到 `[1:4, 1:4]`，再膨胀 1 次，得到菱形：

```
0 0 1 0 0
0 1 1 1 0
1 1 1 1 1
0 1 1 1 0
0 0 1 0 0
```

与 docstring 示例（[_morphology.py:89-94](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L89-L94)）及测试 `test_iterate_structure01`（[tests/test_morphology.py:717-730](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_morphology.py#L717-L730)）一致。

#### 4.2.4 代码实践

**实践目标**：用 `iterate_structure` 把十字结构元扩张 2 次，观察形状与 True 元素分布；体会它与「直接膨胀两次」的等价性。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import ndimage

struct = ndimage.generate_binary_structure(2, 1)   # 3x3 十字
print("原始结构元 (3x3):")
print(struct.astype(int))

big = ndimage.iterate_structure(struct, 2)         # 扩张 2 次
print("\n扩张后形状:", big.shape, " True 数:", big.sum())
print(big.astype(int))

# 等价性验证：对一张只有一个前景点的图，分别膨胀 1 次 vs 用大结构元膨胀 1 次
img = np.zeros((7, 7), bool)
img[3, 3] = True
r1 = ndimage.binary_dilation(ndimage.binary_dilation(img, struct), struct)
r2 = ndimage.binary_dilation(img, big)
print("\n两种方式结果是否完全相同:", np.array_equal(r1, r2))
```

**需要观察的现象**：

- `big.shape == (5, 5)`，True 数为 13（菱形：中心行 5 个、相邻两行各 3 个、首尾两行各 1 个，即 5+3+3+1+1=13）。
- 等价性断言打印 `True`，说明「连续膨胀两次」与「用扩张后的结构元膨胀一次」结果一致。

**预期结果**：`big` 打印为上文那个 5×5 菱形；等价性为 `True`。

#### 4.2.5 小练习与答案

**练习 1**：`iterate_structure(struct, 1)` 返回什么？为什么？

**答案**：返回 `struct` 的副本。因为代码里 `if iterations < 2: return structure.copy()`——扩张 1 次就是原结构元本身，无需膨胀。

**练习 2**：把一个 3×3 十字结构元 `iterate_structure` 到 `iterations=3`，输出形状和 True 数各是多少？

**答案**：`ni = 2`，`shape_i = 3 + 2×2 = 7`，所以是 7×7。菱形从中心行向上下逐行少 2 个 True：7 + 5 + 3 + 1（上半）+ 1 + 3 + 5（下半）= 共 25 个 True，与 docstring 的 `iterations=3` 示例（[_morphology.py:95-102](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L95-L102)）逐行数 True（1+3+5+7+5+3+1=25）一致。

---

### 4.3 _center_is_true：决定侵蚀算法走哪条路

#### 4.3.1 概念说明

`_center_is_true` 是个**内部**辅助函数（前导下划线表示私有，不在公开 API 里），但它对接下来的二值形态学（u5-l2）至关重要。它回答一个问题：**在考虑了 `origin` 偏移之后，结构元的「锚点格」是不是 True？**

为什么锚点是否为 True 这么重要？这里有个深刻的不等式：

> 若结构元的中心格是 True，那么侵蚀结果一定是输入的**子集**——一个原本就是 False 的像素，永远不可能在侵蚀后变成 True。

直觉：侵蚀要求「结构元覆盖的所有邻居都是 True，中心像素才保留」。如果结构元中心格是 True，那么「中心像素自己」也是被检查的邻居之一；中心为 False 的像素必然通不过检查，结果必为 False。于是 False 像素只会保持 False、不会变 True。

这条「单调性」让 C 内核可以走一条**只重新扫描上一轮发生变化的像素**的快速路径（所谓 hit-list / coordinate-list 算法）。反之，若中心格是 False，侵蚀可能让原本 False 的像素变 True，单调性不成立，只能老老实实暴力扫描。

#### 4.3.2 核心流程

```
1. coor[i] = origin[i] + shape[i] // 2     # 每个轴的「锚点」下标
2. return bool(structure[coor])            # 该格是否为 True
```

当 `origin=0`（默认）时，锚点就是几何中心 `shape//2`。当用户传入非零 `origin` 时，锚点相应偏移——这与 u2-l1 讲过的 `origin` 控制锚点偏移的语义一脉相承。

随后 `_binary_erosion` 把这个布尔值（命名为 `cit`，center is true）一路传进 C 内核：

```python
# _morphology.py:253
cit = _center_is_true(structure, origin)
```

并据此在 [_morphology.py:265-298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L265-L298) 三选一：

- `iterations == 1`：单次直接侵蚀。
- `cit and not brute_force`：用 `binary_erosion2` 的「变化像素列表」快速路径做多轮迭代。
- 否则：暴力循环多轮。

C 端的对应分支在 [src/ni_morphology.c:51-83](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_morphology.c#L51-L83)，宏 `CASE_NI_ERODE_POINT` 里有：

```c
if (_center_is_true && _in == _false) {
    _changed = 0;
    _out = _in;        // 已经是 False 的像素直接判定为 False，无需扫描邻域
}
```

即「中心为真且当前像素为假」时，直接短路，跳过对整个邻域的扫描。

#### 4.3.3 源码精读

```python
# _morphology.py:48-52
def _center_is_true(structure, origin):
    structure = np.asarray(structure)
    coor = tuple([oo + ss // 2 for ss, oo in zip(structure.shape,
                                                 origin)])
    return bool(structure[coor])
```

[_morphology.py:48-52](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L48-L52) —— `zip(structure.shape, origin)` 把每维长度 `ss` 与该维 origin `oo` 配对，`oo + ss//2` 得到锚点在该维的下标；用 `tuple` 包成多维索引后取值并转成 Python `bool`。注意它假定 `origin` 已是「每维一个整数」的序列——调用方 `_binary_erosion` 在 [_morphology.py:251-252](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L251-L252) 已先用 `_normalize_sequence` 与 `_expand_origin` 规范过了。

#### 4.3.4 代码实践

**实践目标**：直接调用内部函数 `_center_is_true`，观察「中心为真」与「中心为假」两种结构元的判定差异。

**操作步骤**：

```python
# 示例代码（读取私有函数，仅用于学习）
import numpy as np
from scipy.ndimage import _morphology

# (a) 中心为 True：十字结构元
struct_true = np.array([[0,1,0],[1,1,1],[0,1,0]], dtype=bool)
# (b) 中心为 False：把中心抠掉
struct_false = struct_true.copy()
struct_false[1,1] = False

print("中心为真 ->", _morphology._center_is_true(struct_true, [0,0]))
print("中心为假 ->", _morphology._center_is_true(struct_false, [0,0]))
```

**需要观察的现象**：

- 第一个打印 `True`，第二个打印 `False`。
- 进而理解：用 `struct_false` 做多次迭代侵蚀时，`_binary_erosion` 会因为 `cit=False` 而走暴力循环路径（[_morphology.py:284-298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L284-L298)），不能用快速路径。

**预期结果**：输出 `True` 与 `False` 各一行。

> 待本地验证：从 `scipy.ndimage._morphology` 导入私有函数在某些安装下可能触发导入路径差异；若导入失败，可改用 `from scipy import ndimage` 后通过 `ndimage.generate_binary_structure` 构造结构元，再用本讲的公式手算中心格是否为 True。

#### 4.3.5 小练习与答案

**练习 1**：为什么「中心为真」能保证侵蚀结果是输入的子集？

**答案**：侵蚀定义要求结构元覆盖的所有邻居都为 True 才保留中心像素。中心格为 True 意味着「像素自身」也在被检查的邻居集合里，于是自身为 False 的像素必然被判定为 False，不会从 False 变 True，结果必为输入子集。

**练习 2**：`_center_is_true` 为什么要接收 `origin` 参数，而不是直接取几何中心？

**答案**：因为 `origin` 会移动结构元的锚点位置。当用户指定非零 `origin` 时，真正起作用的「中心」不再是 `shape//2`，而是 `origin + shape//2`，所以必须把 `origin` 算进去才能正确判断锚点格的真假。

## 5. 综合实践

把本讲三个模块串起来完成一个小任务：**用 `generate_binary_structure` + `iterate_structure` 构造一个「大十字」结构元，再用它对一张含粗线条的二值图做一次侵蚀，并验证它等价于「用小十字侵蚀多次」。**

```python
# 示例代码
import numpy as np
from scipy import ndimage

# 1) 构造最小十字与扩张 3 次后的「大十字」
small = ndimage.generate_binary_structure(2, 1)
big   = ndimage.iterate_structure(small, 3)
print("大十字形状:", big.shape, "True 数:", big.sum())

# 2) 造一张含粗十字的 11x11 二值图
img = np.zeros((11, 11), bool)
img[5, :] = True
img[:, 5] = True

# 3) 方式 A：用小十字连续侵蚀 3 次
outA = img.copy()
for _ in range(3):
    outA = ndimage.binary_erosion(outA, structure=small)

# 4) 方式 B：用大十字只侵蚀 1 次
outB = ndimage.binary_erosion(img, structure=big)

print("两种侵蚀结果是否相同:", np.array_equal(outA, outB))
print("侵蚀后剩余 True 数:", outB.sum())
```

**体会要点**：

- `iterate_structure` 给出的「大十字」与「小十字迭代多次」在数学上等价，本例用 `np.array_equal` 验证为 `True`。
- 这正是 `_binary_erosion` 内部 `iterations>1` 时的优化思路：把多次迭代折叠成一次「等价大结构元」运算（结合 `_center_is_true` 走快速路径）。
- 改变 `iterate_structure` 的 `iterations`，观察剩余 True 数随结构元变大而减少。

> 待本地验证：具体剩余 True 数取决于边界模式（默认 `border_value=0`）和图像尺寸，请本地运行确认。

## 6. 本讲小结

- `generate_binary_structure(rank, connectivity)` 用一行核心公式 `np.add.reduce(np.fabs(np.indices([3]*rank) - 1)) <= connectivity` 生成边长为 3 的最小布尔结构元；它本质是「到中心的 L1 距离 ≤ connectivity」的掩码。
- `connectivity` 取值 `1..rank`：1 只含轴向邻居（4-连通），`rank` 含全部对角邻居（全连通）；`connectivity<1` 被钳到 1。
- 由于偏移量限定在 `{-1,0,1}`，L1 距离恰好等于平方欧氏距离，这就是 docstring 里「squared distance」一说的数学根源。
- `iterate_structure(structure, iterations)` 通过「把原结构元贴到中央再自我膨胀 `iterations-1` 次」生成等价大结构元，输出每维长度满足 `shape_i = s_i + (iterations-1)·(s_i-1)`。
- 连续膨胀 `n` 次等价于用「扩张后的结构元」膨胀 1 次——这是形态学的基本恒等式，也是迭代侵蚀加速的依据。
- `_center_is_true` 判断结构元锚点是否为 True；中心为真时侵蚀结果必为输入子集，从而允许 C 内核走「只扫描变化像素」的快速路径。

## 7. 下一步学习建议

- 接下来学 **u5-l2 二值形态学**：`binary_erosion` / `binary_dilation` 正是本讲结构元的最主要消费者，你会看到 `_center_is_true` 的返回值 `cit` 如何在 [_morphology.py:265-298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L265-L298) 决定算法路径，以及 `iterations` 与 `iterate_structure` 的协作。
- 回顾 **u4-l1 连通区域标记**：`label` 的默认结构元就是 `generate_binary_structure(ndim, 1)`，`connectivity` 参数则直接对应本讲的 `connectivity`；把两讲对照阅读能加深对「结构元 = 连通性规则」的理解。
- 进阶可读 **src/ni_morphology.c** 的 `CASE_NI_ERODE_POINT` 宏（[src/ni_morphology.c:51-83](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_morphology.c#L51-L83)），看 C 端如何具体利用 `_center_is_true` 做短路优化。
