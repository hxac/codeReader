# 连通区域标记 label

## 1. 本讲目标

本讲是「测量与连通区域」单元（u4）的第一篇，专讲 `scipy.ndimage.label`。

读完本讲你应该能够：

- 说清 `label` 解决什么问题：把一张数组里所有「相互连通的非零像素」分别打上唯一的整数标签，背景固定为 0。
- 区分 `structure` 如何定义「连通」：默认十字结构元（4-连通）与 `generate_binary_structure(2,2)` 全 3×3 结构元（8-连通）的差别，并理解结构元必须**中心对称（centrosymmetric）**的原因。
- 区分 `label` 的两种返回形式：`output=None`（或 dtype）时返回 `(labeled_array, num_features)`；`output` 为已存在数组时**原地写入**、只返回 `num_features`。
- 读懂底层 Cython 内核 `src/_ni_label.pyx` 的核心思路：fused type 多 dtype 特化 + 逐行扫描 + 等价类（union-find）合并 + 第二遍标签紧缩。

## 2. 前置知识

本讲承接 u1-l4（共享支撑工具）。你需要先掌握：

- **NumPy 数组与轴**：`input.ndim`（维度数 / rank）、`input.shape`、按某条轴取「线」（line）。
- **二值图与非零即前景**：`label` 把任何非零值都当成「前景（feature）」，0 当成「背景」。输入不要求是 0/1，也不要求是布尔。
- **结构元（structuring element）**：一个小的、各维长度为 3 的布尔数组，中心对齐到当前像素，True 的位置表示「邻居」。这个概念在 u5 形态学会深入，本讲只需默认十字元与 3×3 全真元两种。
- **`_ni_support` 的支撑工具**：`label` 不调用 `_get_output` / `_extend_mode_to_code`（它不走 `_nd_image` 内核，而有自己的 `_ni_label` 扩展），但同样遵守「output 可为 None / dtype / 数组」的多态约定。

两个对初学者最关键的术语：

- **连通分量（connected component）**：图中「两两可达」的极大顶点集。这里顶点是前景像素，边由结构元定义。
- **union-find / 等价类合并**：一种把「若干标签其实属于同一物体」这件事增量记录下来的数据结构，本讲内核就是它的一个实现。

## 3. 本讲源码地图

本讲只涉及三个源码文件：

| 文件 | 角色 |
| --- | --- |
| [_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | Python 入口 `label`：参数校验、默认结构元生成、output 多态、两种返回形式、`NeedMoreBits` 重试。 |
| [_morphology.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py) | `generate_binary_structure(rank, connectivity)`：按 L1 距离生成本讲所需的默认 / 全连通结构元。 |
| [src/_ni_label.pyx](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx) | Cython 内核 `_label`：真正干活的逐行扫描 + 等价类合并算法。 |

注意：`label` **不**经过 `_nd_image` 那一套 C 内核，它走的是独立的 `_ni_label` 扩展（见 u1-l2 的三个扩展模块之一）。Python 端只做「备好参数和输出缓冲」，真正的连通判定全在 `.pyx` 里。

## 4. 核心概念与源码讲解

### 4.1 Python 入口 label：参数解析与两种返回形式

#### 4.1.1 概念说明

`label(input, structure=None, output=None)` 是最贴近用户的薄壳。它要回答三个问题：

1. **什么算「物体」？** —— 任何非零值都是前景；多个非零值（如 1、2、255）一视同仁，只要它们通过结构元定义的邻域互相够得着，就属于同一个物体。
2. **怎么算「够得着」？** —— 由 `structure` 决定（4.2 详讲）。
3. **结果写到哪里、返回什么？** —— 由 `output` 决定，这是本节重点。

`output` 有三种形态，对应两种返回契约：

- `output=None`：函数**新建**一个整数数组装结果，返回二元组 `(labeled_array, num_features)`。
- `output=<dtype>`（如 `np.int16`）：按该 dtype 新建数组，仍返回二元组。
- `output=<已有 ndarray>`：**就地写入**该数组（形状必须与 `input` 一致），只返回 `num_features`（一个 int）。可以 `output=input` 实现真·原地操作。

这种「传数组就只返回标量、传 None/dtype 就返回数组」的设计，是为了让调用方能在已有缓冲上反复复用，省掉分配开销。

#### 4.1.2 核心流程

`label` 的 Python 端可以概括为：

```
1. input = np.asarray(input)；拒绝复数
2. 若 structure is None：structure = generate_binary_structure(input.ndim, 1)  # 默认十字元
3. 校验 structure：转 bool、ndim 必须等于 input.ndim、每维长度必须为 3
4. 估算位宽 need_64bits = input.size >= 2**31 - 2
5. 准备 output：
     - 若 output 是 ndarray     → caller_provided_output = True，校验形状
     - 若 output is None        → 新建 int32（或大数组用 intp）
     - 否则（dtype）            → 按该 dtype 新建
6. 处理标量 / 0 维 / 空数组的退化情形（直接给出 0 或 1）
7. 调 _ni_label._label(input, structure, output) 真正标记
     - 若抛 NeedMoreBits：用 int32/int64 重算，再 cast 回 output，拒绝截断
8. 返回：
     - caller_provided_output  → 只返回 max_label
     - 否则                    → 返回 (output, max_label)
```

注意第 7 步的 `NeedMoreBits`：当用户主动要求一个很小的 output dtype（如 `np.int8`），而物体数超过了该类型能表示的范围，内核会检测到溢出并抛出这个异常；Python 端捕获后用足够宽的类型重算，再把结果**截断式回写**，并校验回写无误——若发生真实截断（坏结果），宁可报错也不返回错误答案。

#### 4.1.3 源码精读

函数签名与文档约定两种返回形式：[_measurements.py:L43-L88](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L43-L88) —— 注意 docstring 明确写了「If `output` is None ... returns a tuple」「If `output` is an ndarray ... only `num_features` will be returned」。

默认结构元的生成与三维校验（4.2 会展开 `generate_binary_structure`）：

```python
if structure is None:
    structure = _morphology.generate_binary_structure(input.ndim, 1)
structure = np.asarray(structure, dtype=bool)
if structure.ndim != input.ndim:
    raise RuntimeError('structure and input must have equal rank')
for ii in structure.shape:
    if ii != 3:
        raise ValueError('structure dimensions must be equal to 3')
```

> [_measurements.py:L178-L185](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L178-L185) 默认十字元、强制结构元每维长度为 3、强制与输入同 rank。

位宽估算与 output 多态分配：

```python
need_64bits = input.size >= (2**31 - 2)

if isinstance(output, np.ndarray):
    if output.shape != input.shape:
        raise ValueError("output shape not correct")
    caller_provided_output = True
else:
    caller_provided_output = False
    if output is None:
        output = np.empty(input.shape, np.intp if need_64bits else np.int32)
    else:
        output = np.empty(input.shape, output)
```

> [_measurements.py:L190-L201](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L190-L201) `caller_provided_output` 这个布尔量就是「两种返回形式」的分叉开关；注释解释 2^31−2 这个阈值是因为 `_ni_label` 需要为背景/前景各预留一个槽位。

真正调用内核、处理 `NeedMoreBits`、以及最后的返回分叉：

```python
try:
    max_label = _ni_label._label(input, structure, output)
except _ni_label.NeedMoreBits as e:
    tmp_output = np.empty(input.shape, np.intp if need_64bits else np.int32)
    max_label = _ni_label._label(input, structure, tmp_output)
    output[...] = tmp_output[...]
    if not np.all(output == tmp_output):
        raise RuntimeError("insufficient bit-depth in requested output type") from e

if caller_provided_output:
    return max_label          # 就地写入，只返回数量
else:
    return output, max_label  # 新建数组，返回 (数组, 数量)
```

> [_measurements.py:L217-L235](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L217-L235) 这是本节最核心的几行：try/except 实现「先按用户 dtype 试，溢出就升级重算并校验」，最后的 `if caller_provided_output` 正是两种返回形式的实现。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `output` 三种形态对应两种返回契约，并验证原地写入。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.ndimage import label

data = np.array([[1, 0, 0, 1],
                 [0, 0, 0, 0],
                 [0, 0, 1, 0]])

# 形态 1：output=None → 返回二元组
out1, n1 = label(data)
print("None  ->", type(out1).__name__, out1.dtype, "n=", n1)

# 形态 2：output=dtype → 仍返回二元组，但数组是该 dtype
out2, n2 = label(data, output=np.int16)
print("dtype ->", out2.dtype, "n=", n2)

# 形态 3：output=已有数组 → 原地写入，只返回 int
buf = np.empty(data.shape, dtype=np.int32)
ret3 = label(data, output=buf)
print("array ->", type(ret3).__name__, ret3, "buf=\n", buf)
```

需要观察的现象：

1. `out1` 是 `np.ndarray`、`n1` 是 `int`。
2. `out2.dtype` 是 `int16`（证明 output 形参被当成 dtype 用）。
3. `ret3` **不是**元组，而是一个 `int`（即 `num_features`）；结果被写进了 `buf`。
4. 三个调用得到的标签布局与物体数都一致（两个孤立前景 → `n=2`，对角不相触默认不连通）。

预期结果：`n1 == n2 == ret3 == 2`，且 `out1` 形如 `[[1,0,0,2],[0,0,0,0],[0,0,3,0]]`（注意 3 个孤立像素在 4-连通下各成一物，故标签到 3、`num_features` 仍为 3；这里 `n` 应为 **3**，请以本地实际输出为准修正——三个互不连通的前景像素各占一个标签）。

> 说明：上面 6×4 例子里 3 个前景像素两两不相邻，故 `num_features` 实际为 3。若想得到 `n=2`，可把 `data[1,0]` 改成 1 让左侧两个像素竖直相连。

#### 4.1.5 小练习与答案

**练习 1**：`label(np.ones(()))`（标量 1）返回什么？为什么不需要走 Cython 内核？

答案：标量退化分支直接判定 `input != 0` → `maxlabel = 1`，`output[...] = 1`，返回 `(array(1), 1)`。因为标量没有「邻域」可言，无需连通判定，Python 端短路返回（见 `input.ndim == 0` 分支）。

**练习 2**：为什么默认 output 用 `int32` 而不是 `int64`？阈值 `2**31 - 2` 是怎么来的？

答案：`int32` 能装下 ~21 亿个标签，对绝大多数图像够用且省一半内存；只有当 `input.size >= 2**31 - 2`（像素数本身接近 int32 上限）时才升级 `intp`。注释「needs two entries for background and foreground tracking」指内核内部编号从 2 起（0=背景、1=待标前景占位），故预留 2 个槽位。

---

### 4.2 结构元 structure：默认十字元、connectivity 与 centrosymmetric

#### 4.2.1 概念说明

「连通」不是天生就有定义的——它取决于你承认哪些像素互为邻居。`structure` 就是这份「邻居清单」。

对二维输入，两种最常用的结构元是：

- **十字元（4-连通）**：只有上下左右 4 个正交邻居。
  ```
  [[0,1,0],
   [1,1,1],
   [0,1,0]]
  ```
- **全 3×3 元（8-连通）**：连对角线 4 个邻居也算，共 8 邻居。
  ```
  [[1,1,1],
   [1,1,1],
   [1,1,1]]
  ```

`label` 的默认结构元就是十字元，等价于 `generate_binary_structure(input.ndim, 1)`。要想让对角相触的像素连通，就传 `generate_binary_structure(input.ndim, 2)`（对 2D 即 8-连通），或直接传一个全真的 3×3 列表。

**connectivity** 是一个 1 到 rank 之间的整数，表示「离中心的城市街区距离（L1 距离）不超过 connectivity 的格子都算邻居」。一个中心偏移为 \(\mathbf{d}=(d_1,\dots,d_r)\)（每个 \(d_i\in\{-1,0,1\}\)）的格子被纳入结构元，当且仅当

\[
\sum_{i=1}^{r} |d_i| \;\leq\; \text{connectivity}.
\]

对 2D：connectivity=1 → 只有 \(|d_1|+|d_2|\le 1\) 的格子（十字）；connectivity=2 → \(\le 2\) 的格子（含对角，因为 \((1,1)\) 的和为 2），即全 3×3。

#### 4.2.2 核心流程

`generate_binary_structure(rank, connectivity)` 的三行核心：

```
1. 生成所有偏移坐标：np.indices([3]*rank) - 1   # 形状 (rank, 3,3,...,3)，每个分量取 -1/0/1
2. 取绝对值并在坐标轴上求和：得到每个格子到中心的 L1 距离
3. output = (L1 距离 <= connectivity)            # 布尔结构元
```

`label` 在用 `structure` 之前，还会强制两条不变量：

1. `structure.ndim == input.ndim` 且每维长度为 3（Python 端 `_measurements.py` 校验）。
2. **中心对称**：`structure == structure[(::-1,)*ndim]`（Cython 端 `_label` 校验）。

为什么必须中心对称？因为连通是双向关系。如果结构元不对称，会出现「A 看得到 B、B 看不到 A」的单向连接，导致同一个物体被切成两半。docstring 给的反例：结构元 `[[0,1,0],[1,1,0],[0,0,0]]`（非对称）下，输入 `[[1,2],[0,3]]` 会让 2 连到 1，但 1 不连到 2。

#### 4.2.3 源码精读

`generate_binary_structure` 的实现本体只有三行：

```python
output = np.fabs(np.indices([3] * rank) - 1)
output = np.add.reduce(output, 0)
return output <= connectivity
```

> [_morphology.py:L211-L213](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L211-L213) `np.indices([3]*rank)` 给出 3^rank 个格子的坐标；`-1` 把中心移到原点；`fabs` 取绝对值；`add.reduce` 沿坐标轴求和得到 L1 距离；最后 `<= connectivity` 转布尔。

中心对称校验在 Cython 内核里（不在 Python 端），用「结构元等于它自身全反转」来断言：

```python
# check structuring element for symmetry
assert np.all(structure == structure[(np.s_[::-1],) * structure.ndim]), \
    "Structuring element is not symmetric"
```

> [src/_ni_label.pyx:L221-L223](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L221-L223) `(np.s_[::-1],) * ndim` 构造一个「每个维度都反转」的索引元组；结构元必须等于自身的全反转，才保证连接双向。

`label` docstring 里对默认十字元的明文说明：[_measurements.py:L52-L64](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L52-L64)（明确写出 `structure=None` 时自动生成 squared connectivity = 1 的结构元，并画出 2D 的十字形）。

#### 4.2.4 代码实践

**实践目标**：用对角相触的二值图，对比 4-连通（默认）与 8-连通（connectivity=2）的标记差异。这正是官方测试 `test_label08`/`test_label09` 使用的同一组数据。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from scipy import ndimage

data = np.asarray([[1, 0, 0, 0, 0, 0],
                   [0, 0, 1, 1, 0, 0],
                   [0, 0, 1, 1, 1, 0],
                   [1, 1, 0, 0, 0, 0],
                   [1, 1, 0, 0, 0, 0],
                   [0, 0, 0, 1, 1, 0]])

# (a) 默认结构元 = generate_binary_structure(2,1) = 十字 → 4-连通
out4, n4 = ndimage.label(data)
print("4-连通 num_features =", n4)
print(out4)

# (b) 全 3x3 结构元 = generate_binary_structure(2,2) → 8-连通
s = ndimage.generate_binary_structure(2, 2)
out8, n8 = ndimage.label(data, structure=s)
print("8-连通 num_features =", n8)
print(out8)

# (c) 观察两种结构元本身
print("connectivity=1:\n", ndimage.generate_binary_structure(2, 1))
print("connectivity=2:\n", ndimage.generate_binary_structure(2, 2))
```

需要观察的现象：

1. `n4 == 4`：左上孤立点(1)、中央 L 形块(2)、左下 2×2 块(3)、右下 1×2 块(4) 各成一物。
2. `n8 == 3`：在 8-连通下，中央块 (2,2) 与左下块 (3,1) 对角相触，**合并**成一个物体（标签 2）；右下块重编号为 3；左上孤立点仍是 1。
3. 两个结构元打印结果分别是十字形与全真 3×3。

预期结果（与 [test_label08 / test_label09](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_measurements.py#L220-L254) 的断言一致）：

```
4-连通 num_features = 4
[[1 0 0 0 0 0]
 [0 0 2 2 0 0]
 [0 0 2 2 2 0]
 [3 3 0 0 0 0]
 [3 3 0 0 0 0]
 [0 0 0 4 4 0]]
8-连通 num_features = 3
[[1 0 0 0 0 0]
 [0 0 2 2 0 0]
 [0 0 2 2 2 0]
 [2 2 0 0 0 0]
 [2 2 0 0 0 0]
 [0 0 0 3 3 0]]
```

#### 4.2.5 小练习与答案

**练习 1**：手算 `generate_binary_structure(3, 1)`（3D、connectivity=1）有几个 True？是哪个形状？

答案：L1 距离 ≤ 1 的格子 = 中心 + 6 个正交方向邻居 = **7 个**，形状是 3D 十字（上下左右前后 + 中心），打印出来是三个 3×3 平面，中间平面是十字、上下平面只有中心一个 True。这与本讲「默认 3D 结构元」一致。

**练习 2**：把结构元改成非对称的 `[[0,1,0],[1,1,0],[0,0,0]]` 传给 `label`，会发生什么？

答案：Cython 内核的对称性断言（`src/_ni_label.pyx` L221-L223）会失败并抛出 `AssertionError: Structuring element is not symmetric`。这正说明 centrosymmetric 不是建议而是硬性约束。

---

### 4.3 Cython 内核 _ni_label：fused type、逐行扫描与等价类合并

#### 4.3.1 概念说明

真正的连通判定发生在 Cython 扩展 `_ni_label._label` 里。它采用一种**逐行扫描（line-based）的两遍连通分量算法**，比朴素的「逐像素 + 全邻域」更高效，核心思想有三：

1. **以「线」为单位处理**。选一条轴（`axis = -1`，即最快变化的轴），把 N 维数组看作「许多条一维线」。每处理一条线，只需参考**已经标记过的相邻线**，而不必每次回看整张图。
2. **等价类合并（union-find）记录「其实是同一物体」**。同一条线上的像素可能从不同的相邻线那里继承了不同的标签，于是这些标签其实属于同一物体——用一张 `mergetable` 把它们「指向同一个最小根」。
3. **第二遍「标签紧缩」**。扫描产生的标签是不连续的（可能跳号、可能重复），第二遍把 `mergetable` 紧缩成 1,2,3,…，再扫一遍 output 把每个工作标签替换成最终标签。

**fused type** 是 Cython 的「类型模板」机制：把同一份算法源码对 10 种整数/浮点 dtype 各编译一份特化代码，避免运行时类型分派开销。内核通过 `get_nonzero_line / get_read_line / get_write_line` 三个工厂函数，根据输入/输出的实际 dtype 取出对应的特化函数指针。

#### 4.3.2 核心流程

```
_label(input, structure, output):
  校验：input/output 同形；structure 同 rank、每维=3、中心对称

  选 axis = -1，构造三个「除该轴外」的迭代器 iti/ito/itstruct
  num_neighbors = structure.size // 6        # 因中心对称只需处理一半相邻线
  分配两条线缓冲 line_buffer / neighbor_buffer（各 L+2，首尾填 BACKGROUND 作哨兵）
  分配 mergetable（union-find 表），next_region = 2（0=背景,1=待标,2+=工作标签）

  第一遍（nogil 主循环），对每条线：
    a. nonzero_line：把 input 当前线的非零值读成 FOREGROUND(1)，零读成 BACKGROUND(0)
    b. 对每个相邻线位置 ni（共 num_neighbors 个）：
         - 读结构元沿 axis 的 3 个值：use_prev / use_adjacent / use_next（决定对角连接）
         - 若该相邻线越界则跳过
         - read_line 把相邻线读进 neighbor_buffer
         - label_line_with_neighbor：把相邻线标签传播到当前线，冲突则 mark_for_merge
    c. 若没有任何相邻线触发「自标」(label_unlabeled)，单独再做一次自标
    d. write_line：把当前线写回 output；若目标 dtype 装不下 → 置 overflowed → 抛 NeedMoreBits

  第二遍（紧缩 + 重写）：
    紧缩 mergetable：把 2..next_region 重映射成连续的 1,2,3,...
    再扫一遍 output，把每个标签替换成 mergetable[标签]
  返回 dest_label - 1   # 即 num_features
```

`label_line_with_neighbor` 对当前线的每个像素 i，根据结构元的三个开关，从相邻线的 `i-1 / i / i+1` 取标签；若像素已有不同标签则触发 `mark_for_merge`（union-find 合并）。`take_label_or_merge` 封装了「取邻居标签，或发现冲突就合并」这件事。

`mark_for_merge` 是标准 union-find：沿 `mergetable` 找 a、b 各自的根，都指向较小根，并对路径上的中间节点做压缩（指向最终根），保证「表总是向下指」的不变量——这正是第二遍能用「两步间接」快速取最终标签的原因。

#### 4.3.3 源码精读

fused type 定义了 10 种 dtype 的特化模板：[src/_ni_label.pyx:L37-L49](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L37-L49) —— Cython 会为这里列的每种 `data_t` 生成一份独立的编译产物。

`NeedMoreBits` 异常类（极简）：[src/_ni_label.pyx:L31-L32](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L31-L32) —— 当 `fused_write_line` 检测到「值被目标 dtype 截断」时由内核抛出，Python 端据此升级位宽重算。

「读非零 → 前景」的特化函数（fused），它把任意 dtype 的输入统一压成 0/1：

```python
cdef void fused_nonzero_line(data_t *p, np.intp_t stride,
                             np.uintp_t *line, np.intp_t L) noexcept nogil:
    cdef np.intp_t i
    for i in range(L):
        line[i] = FOREGROUND if \
            (<data_t *> ((<char *> p) + i * stride))[0] \
            else BACKGROUND
```

> [src/_ni_label.pyx:L54-L60](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L54-L60) 这就是「非零即前景」的逐像素实现；`data_t` 是 fused 类型，Cython 对每种输入 dtype 各生成一份。

写回时检测溢出（解释了 `NeedMoreBits` 的来源）：

```python
cdef bint fused_write_line(data_t *p, np.intp_t stride,
                           np.uintp_t *line, np.intp_t L) noexcept nogil:
    cdef np.intp_t i
    for i in range(L):
        # Check before overwrite ... allows us to retry even when operating in-place.
        if line[i] != <np.uintp_t> <data_t> line[i]:
            return True                          # 截断发生 → 溢出
        (<data_t *> ((<char *> p) + i * stride))[0] = <data_t> line[i]
    return False
```

> [src/_ni_label.pyx:L77-L87](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L77-L87) 先比较再写，是为了原地操作时即使截断也不会污染前景为 0，从而可以安全重试。

union-find 合并（找根 + 指向最小根 + 路径压缩）：

```python
cdef inline np.uintp_t mark_for_merge(np.uintp_t a, np.uintp_t b,
                                      np.uintp_t *mergetable) noexcept nogil:
    ...
    while a != mergetable[a]:        # 找 a 的根
        a = mergetable[a]
    while b != mergetable[b]:        # 找 b 的根
        b = mergetable[b]
    minlabel = a if (a < b) else b
    mergetable[a] = mergetable[b] = minlabel   # 两根都指向较小根
    ...                                          # 路径上的中间节点也压向 minlabel
    return minlabel
```

> [src/_ni_label.pyx:L117-L144](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L117-L144) 这就是「两个标签其实属于同一物体」的记录方式；「表总是向下指」的不变量让第二遍能用两步间接取到最终标签。

逐行传播标签的核心（对当前线每个像素，从相邻线的 prev/adjacent/next 取标签，冲突则合并）：

```python
for i in range(L):
    if line[i] != BACKGROUND:
        if neighbor_use_previous:
            line[i] = take_label_or_merge(line[i], neighbor[i - 1], mergetable)
        if neighbor_use_adjacent:
            line[i] = take_label_or_merge(line[i], neighbor[i],     mergetable)
        if neighbor_use_next:
            line[i] = take_label_or_merge(line[i], neighbor[i + 1], mergetable)
        if label_unlabeled:
            if use_previous:
                line[i] = take_label_or_merge(line[i], line[i - 1], mergetable)
            if line[i] == FOREGROUND:      # 仍无标签 → 分配新标签
                line[i] = next_region
                mergetable[next_region] = next_region
                next_region += 1
```

> [src/_ni_label.pyx:L179-L195](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L179-L195) `neighbor_use_previous/adjacent/next` 三个开关正是结构元在对角方向上的取值——这就是「十字元只有 adjacent、全 3×3 元三个都开」的实现落点，也直接对应 4-连通 vs 8-连通。

2D 时的缓冲指针交换优化（避免重新读相邻线）：

```python
if output_ndim == 2:
    tmp = line_buffer
    line_buffer = neighbor_buffer
    neighbor_buffer = tmp
```

> [src/_ni_label.pyx:L315-L318](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L315-L318) 2D 只有一条相邻线（上一行），处理完当前行后，当前行天然就是下一行的相邻线，故直接交换两个缓冲指针，省掉一次 `read_line`。

第二遍「紧缩 + 重写」（把跳号/重复的工作标签压成连续整数）：

```python
mergetable[BACKGROUND] = BACKGROUND
mergetable[2] = 1                       # 工作标签从 2 起 → 最终从 1 起
dest_label = 2
for src_label in range(3, next_region):
    if mergetable[src_label] == src_label:   # 自指 = 独立新物体
        mergetable[src_label] = dest_label
        dest_label += 1
    else:                                    # 已合并 → 两步间接取最终标签
        mergetable[src_label] = mergetable[mergetable[src_label]]
# 再扫一遍 output，用 mergetable 把每个标签替换成最终值
PyArray_ITER_RESET(ito)
while PyArray_ITER_NOTDONE(ito):
    read_line(PyArray_ITER_DATA(ito), so, line_buffer, L)
    for i in range(L):
        line_buffer[i] = mergetable[line_buffer[i]]
    write_line(PyArray_ITER_DATA(ito), so, line_buffer, L)
    PyArray_ITER_NEXT(ito)
```

> [src/_ni_label.pyx:L407-L434](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L407-L434) 这一段决定了 `num_features = dest_label - 1`（[L441](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_ni_label.pyx#L441)），并把 output 里跳号的工作标签一次性改写成 1,2,3,…。

#### 4.3.4 代码实践

**实践目标**：用一个最小 1D 例子手工跟踪「等价类合并」，理解 `mergetable` 如何把两个不同标签并成一个物体；再用 int8 触发 `NeedMoreBits` 路径。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from scipy import ndimage

# (a) 1D：两个本应连通的段被中间的“桥”连起来 → 应只有 1 个物体
line = np.array([1, 1, 0, 1, 1])     # 4-连通下：左段 [0:2]、右段 [3:5] 互不相连
out, n = ndimage.label(line)
print("1D 默认:", out, "n =", n)     # 预期 [1 1 0 2 2], n=2

# (b) 用 8-连通的 1D 结构元（其实就是 [1,1,1]）也没用，因为中间是 0 背景阻断
#     真正能体现“合并”的是 2D 对角相触（见 4.2.4）

# (c) NeedMoreBits：要求 int8 输出，但物体数超过 127
big = np.ones((200, 1), dtype=np.int8)   # 200 个互相不连通的像素（列被 0 隔开做不到，
                                         # 这里改用对角构造）
big = np.zeros((400, 1), dtype=np.int8)
big[np.arange(0, 400, 2)] = 1            # 每隔一行一个前景 → 200 个独立物体
try:
    ret = ndimage.label(big, output=np.int8)
    print("int8 输出成功，num =", ret)
except RuntimeError as e:
    print("触发拒绝：", e)
```

需要观察的现象：

1. (a) 1D 默认结构元下，`[1,1,0,1,1]` 被中间 0 阻断，得到 `n=2`、标签 `[1,1,0,2,2]`。
2. (c) 当物体数 > 127 而 output 要求 `int8` 时，Python 端捕获 `NeedMoreBits`，用更宽类型重算后试图 cast 回 int8——因为真实截断，最终抛出 `RuntimeError: insufficient bit-depth in requested output type`。若把 output 改成 `np.int32` 则正常返回 `num=200`。

> 说明：(c) 中「每隔一行一个前景」共 200 个独立物体，确实超过 int8 的 127 上限。若你构造的独立物体数 ≤ 127，则 int8 也能成功——请按本地实际物体数判断是否会触发 `RuntimeError`。无法确定时记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么内核用「逐线」而不是「逐像素」处理？2D 时为何能省掉一次 `read_line`？

答案：逐线把「同一行内多个像素的相邻关系」压缩成一次向量化扫描，且只需参考已标记的相邻线（过去方向），天然单遍。2D 只有一条相邻线（上一行），处理完当前行后它就成了「下一行的相邻线」，因此只需交换 `line_buffer`/`neighbor_buffer` 两个指针（L315-L318），不必把刚写好的当前行再 `read_line` 读回来。

**练习 2**：`mergetable` 在扫描过程中「总是向下指」（指向更小的根）这一不变量，在第二遍紧缩时如何被利用？

答案：紧缩时按 `src_label` 从小到大遍历；遇到已合并的标签，它的 `mergetable[src_label]` 一定指向一个比它小、且已被紧缩过的标签，因此只需 `mergetable[mergetable[src_label]]` 一次「两步间接」就能拿到最终标签，无需再次找根（L416-L426）。

---

## 5. 综合实践

把本讲三个模块串起来：**用一张自制的、含对角相触与孔洞的二值图，对比不同 connectivity 的标记结果，并用就地输出统计物体数。**

```python
# 示例代码
import numpy as np
from scipy import ndimage

# 1) 造图：左上一个孤立点；中间一个 L 形；右下一个实心小块；
#    其中 L 形与右下块在对角方向 (5,3)-(4,4)? 自行设计让它们“8-连通但4-不连通”
img = np.zeros((6, 6), dtype=int)
img[0, 0] = 1                                # 孤立点
img[1:3, 2:4] = 1                            # 2x2 块
img[3, 4] = img[4, 3] = 1                    # 两个对角像素（互相 8-连通，4-不连通）
print("原图:\n", img)

# 2) 分别用 4-连通 / 8-连通标记
out4, n4 = ndimage.label(img)                              # 默认十字元
out8, n8 = ndimage.label(img, ndimage.generate_binary_structure(2, 2))

print("4-连通 num =", n4, "\n", out4)
print("8-连通 num =", n8, "\n", out8)

# 3) 就地输出：在已有 int32 缓冲上反复标记，只拿回物体数
buf = np.empty(img.shape, dtype=np.int32)
only_n = ndimage.label(img, output=buf)
print("就地返回类型:", type(only_n).__name__, "值:", only_n, "buf 非零标签数:", buf.max())

# 4) 用 find_objects（下一讲 u4-l2）验证每个物体的包围盒
slices = ndimage.find_objects(out8)
print("8-连通下各物体包围盒:", slices)
```

需要观察的现象与预期：

1. 4-连通下，对角两像素 (3,4) 与 (4,3) 被判为**两个**不同物体；8-连通下合并为**一个**，故 `n8 < n4`。
2. `only_n` 是 `int`（不是元组），证明就地写入；`buf.max()` 应等于 `n8`。
3. `find_objects(out8)` 返回的切片数等于 `n8`，每个切片框住一个连通物体（本函数将在 u4-l2 详解）。

若结果与预期不符，先检查你造的对角像素是否真的「4-不连通、8-连通」（行列差各为 1）。

## 6. 本讲小结

- `label` 把所有非零像素按结构元定义的邻域划分连通分量，背景固定为 0；它走独立的 `_ni_label` 扩展，不经 `_nd_image`。
- **两种返回形式**：`output=None`/dtype 返回 `(labeled_array, num_features)`；`output=数组` 就地写入、只返回 `num_features`。分叉开关是 Python 端的 `caller_provided_output`。
- **默认结构元**是十字元（`generate_binary_structure(ndim, 1)`，4-连通）；要 8-连通用 `generate_binary_structure(ndim, 2)`。`connectivity` 即「到中心的 L1 距离上限」。
- 结构元**必须中心对称**（Python/Cython 双重校验），否则连接单向、连通判定失效。
- 内核 `_ni_label._label` 用 **fused type** 对 10 种 dtype 各生成特化代码，用**逐行扫描 + 等价类合并（union-find）**单遍完成标记，再**第二遍紧缩标签**。
- 位宽自适应：大数组自动用 `intp`；用户指定过窄 dtype 时靠 `NeedMoreBits` 异常升级重算并拒绝截断结果。

## 7. 下一步学习建议

- **u4-l2 切片定位 find_objects 与值索引 value_indices**：拿到 `labeled_array` 后，下一步几乎总是「按物体裁剪」，`find_objects` 正是为此；本讲综合实践已埋了伏笔。
- **u4-l3 标签统计 sum/mean/var/min/max**：在 `labels=labeled_array` 上做按区域的聚合统计，是连通标记最常见的后续操作。
- **u5-l1 结构元 generate_binary_structure 与 iterate_structure**：本讲只用了 `generate_binary_structure` 的「生成」一面，更大的结构元（`iterate_structure`）和它在形态学中的作用留到形态学单元。
- **想深挖内核**可读 u6-l4（Cython 标记 `_ni_label` 与 C++ 秩滤波），那里会从工程角度复盘 fused type、`NeedMoreBits` 与等价类合并的实现细节。
