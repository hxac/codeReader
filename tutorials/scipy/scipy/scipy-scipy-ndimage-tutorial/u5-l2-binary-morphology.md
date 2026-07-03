# 二值形态学：侵蚀、膨胀与开闭运算

## 1. 本讲目标

本讲聚焦 `scipy.ndimage` 形态学单元中最基础也最重要的一族函数——**二值形态学**（binary morphology）。学完后你应当能够：

- 用集合论语言说清**侵蚀（erosion）**与**膨胀（dilation）**的定义，并理解二者的**对偶关系**。
- 读懂私有内核 `_binary_erosion` 的三条执行路径，理解 `iterations`、`mask`、`border_value`、`brute_force`、`cit` 各参数如何控制行为。
- 解释为什么 `binary_dilation` **没有自己的 C 内核**，而是靠「翻转结构元 + 取反 origin + invert 标志」复用侵蚀内核。
- 理解 `binary_opening = erosion → dilation`、`binary_closing = dilation → erosion` 的组合，以及它们「去小对象 / 填小孔」的作用。
- 读懂 `binary_hit_or_miss` 用**两组结构元**做模式匹配、`binary_propagation` 在掩膜内反复膨胀、`binary_fill_holes` 从边界「入侵」补集来填孔的算法。

本讲全部源码集中在单一文件 `_morphology.py`，承接 u5-l1 的结构元（`generate_binary_structure`、`_center_is_true`）概念。

## 2. 前置知识

### 2.1 把图像看成「点的集合」

二值图像里每个像素只有「前景（True / 1）」或「背景（False / 0）」两种状态。形态学不关心像素的灰度，只关心**前景像素构成的那个集合** \( A \)。所以本讲通篇用集合记号：\( A \) 是前景集合，\( A^c \) 是它的补集（背景）。

### 2.2 结构元就是「探针」

u5-l1 已讲过：**结构元（structuring element）** \( B \) 是一小块布尔数组，中心放在某个像素上，定义「这个像素的哪些邻居参与运算」。本讲里默认结构元都是 `generate_binary_structure(rank, 1)`——即十字形、4-连通（二维下只含上下左右）。结构元的**反射**记作 \( \check{B} \)，即把 \( B \) 沿每个轴翻转：`B[::-1, ::-1, ...]`。

### 2.3 侵蚀与膨胀的直觉

- **侵蚀**：把结构元中心对准每个像素，只有当「整个结构元都落在前景里」时，该像素才保留为前景。效果是**把物体向内收缩**、削平毛刺、去掉比结构元小的孤立点。
- **膨胀**：结构元中心对准每个像素，只要「结构元至少碰到一个前景像素」，该像素就变成前景。效果是**把物体向外扩张**、填补细缝、连通相近物体。

### 2.4 共享支撑工具回顾

本讲的函数都依赖 u1-l4 讲过的支撑工具：`_ni_support._check_axes`（规范轴）、`_normalize_sequence`（标量→序列）、`_get_output`（统一输出数组）、以及 u5-l1 的 `_center_is_true`（判断结构元中心是否为真）。读源码时遇到这些可一带而过，直奔算法本身。

## 3. 本讲源码地图

本讲只引用一个文件：

| 文件 | 作用 |
|------|------|
| [`_morphology.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py) | 形态学子包的全部 Python 实现。本讲涉及其中的二值函数族：`_binary_erosion`（私有内核）、`binary_erosion` / `binary_dilation`（公开包装）、`binary_opening` / `binary_closing`（开闭运算）、`binary_hit_or_miss`（击中击不中）、`binary_propagation`（传播）、`binary_fill_holes`（填孔）。 |

底层真正的数值计算由 C 扩展 `_nd_image` 的 `binary_erosion` / `binary_erosion2` 两个方法承担（见 u6 单元），本讲只讲 Python 层如何调度它们。

## 4. 核心概念与源码讲解

### 4.1 _binary_erosion 内核：三条执行路径

#### 4.1.1 概念说明

`_binary_erosion` 是整个二值形态学的**唯一真正内核**——`binary_erosion`、`binary_dilation`、`binary_hit_or_miss`、`binary_propagation`、`binary_fill_holes` 最终全都落到它。它要解决的核心问题是：**当 `iterations` 很大（或为「膨胀到不变」）时，如何避免每次都扫描整张图？**

关键观察是：侵蚀是**单调收缩**的——一个像素一旦在某次迭代变成背景，就再也不可能变回前景。因此下一次只需检查「上一轮刚被侵蚀掉的像素的邻居」，而不必重扫全图。这正是 `brute_force=False` 默认路径的优化思路；反之 `brute_force=True` 则老老实实每轮全扫。

另一个关键开关是 `cit`（center is true）：当结构元中心为真时，侵蚀结果必为输入的子集（`A ⊖ B ⊆ A`），上面那条「只看变化像素」的优化才成立。若结构元中心为假，则不存在这种单调性，只能走 brute-force 循环。

#### 4.1.2 核心流程

`_binary_erosion(input, structure, iterations, mask, output, border_value, origin, invert, brute_force, axes)` 的执行过程：

1. **参数校验与归一**：`iterations` 必须是整数；输入不能是复数；`_check_axes` 规范 `axes`；结构元缺省时取 `generate_binary_structure(num_axes, 1)`；`axes` 为子集时用 `_filters._expand_footprint` 把结构元补齐到完整维度；计算 `cit = _center_is_true(structure, origin)`。
2. **输出与别名处理**：`_get_output` 准备输出数组；若 `input` 与 `output` 共享内存（`np.may_share_memory`），则另开一个临时缓冲，算完再拷回，避免就地读写冲突。
3. **三条路径分派**（见 4.1.3）。

对偶性背后的 `invert` 参数：传给 C 内核的第 8 个整数标志，`invert=0` 表示对输入直接侵蚀；`invert=1` 表示「先对输入取逻辑反、侵蚀、再对结果取反」（即 \(\neg(A^c \ominus B)\)）。后者的语义正是膨胀，详见 4.2。

#### 4.1.3 源码精读

参数校验与结构元归一化（注意 `cit` 的计算）：

[_morphology.py:L216-L259](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L216-L259) — 计算 `cit = _center_is_true(structure, origin)`，并用 `temp_needed` 处理输入输出别名。

三条执行路径的分派是本内核的精华：

[_morphology.py:L265-L298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L265-L298) — 三分支：

- **`iterations == 1`**：直接单次调用 `_nd_image.binary_erosion(..., cit, 0)`，最后一个 `0` 是「模式」标志，表示一次性全图侵蚀，不追踪变化像素。
- **`cit and not brute_force`（快速路径）**：第一次用模式 `1` 调用，返回 `(changed, coordinate_list)`——即「这一轮哪些像素被改变了」的坐标列表；随后**翻转结构元、取反 origin**（与 `binary_dilation` 同款翻转），再用专用方法 `_nd_image.binary_erosion2(output, structure, mask, iterations-1, origin, invert, coordinate_list)` 把剩余 `iterations-1` 轮一气呵成，每轮只扫坐标列表里的候选像素。
- **`else`（brute_force 或中心为假）**：用两个缓冲 `tmp_in` / `tmp_out` 交替读写，循环调用模式 `0` 的全图侵蚀；循环条件是 `ii < iterations` 或「`iterations < 1`（即要求膨胀到不变）且 `changed` 为真」。这就是 `binary_propagation` / `binary_fill_holes` 反复膨胀直到收敛的底层机制。

> 小贴士：第二个分支里翻转结构元与取反 origin 的三行代码，和 4.2 里 `binary_dilation` 的翻转完全一致——因为「再侵蚀一次」等价于「用反射结构元再侵蚀一次」，这是形态学的基本恒等式。

#### 4.1.4 代码实践

1. **实践目标**：体会「只追踪变化像素」的快速路径与 brute-force 路径结果相同、但代价不同。
2. **操作步骤**：

```python
import numpy as np
from scipy import ndimage

# 一个较大的实心方块
a = np.zeros((200, 200), bool)
a[50:150, 50:150] = True

# 默认快速路径（brute_force=False）
import time
t0 = time.perf_counter()
for _ in range(50):
    r1 = ndimage.binary_erosion(a, iterations=30)
t1 = time.perf_counter()

# brute_force 全扫路径
t2 = time.perf_counter()
for _ in range(50):
    r2 = ndimage.binary_erosion(a, iterations=30, brute_force=True)
t3 = time.perf_counter()

print("结果一致:", np.array_equal(r1, r2))
print("快速路径耗时 %.4fs, brute_force 耗时 %.4fs" % (t1 - t0, t3 - t2))
```

3. **需要观察的现象**：两条路径结果**逐元素相同**；但快速路径明显更快（尤其 `iterations` 较大时），因为后续轮次只扫描坐标列表而非全图。
4. **预期结果**：打印 `结果一致: True`，且快速路径耗时显著低于 brute_force。
5. 具体数值随机器而异，属「待本地验证」，但「结果一致 + 快速路径更快」这一结论应当稳定成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_binary_erosion` 在 `cit` 为假时无法走「只追踪变化像素」的快速路径？

**参考答案**：快速路径依赖「侵蚀结果 ⊆ 输入」的单调性——一个像素一旦变背景就永不再变前景，所以只需追踪上一轮新变背景的像素。当结构元中心为假（`cit=False`）时，侵蚀不再保证结果是其子集，单调性被破坏，只能退回每轮全图扫描的 brute-force 循环。

**练习 2**：`iterations=-1`（小于 1）时，第三分支的 `while` 循环何时停止？

**参考答案**：循环条件是 `ii < iterations or (iterations < 1 and changed)`。当 `iterations < 1` 时前半段恒为假，循环只由 `changed` 控制——一旦某轮 `changed` 为假（即侵蚀已收敛、结果不再变化），循环结束。这正是「膨胀/侵蚀到不变」的语义。

---

### 4.2 binary_erosion 与 binary_dilation：对偶的孪生兄弟

#### 4.2.1 概念说明

数学上，侵蚀与膨胀互为**对偶（duality）**：对图像取反、用反射结构元做侵蚀、再取反，就得到膨胀。用集合记号：

\[
A \oplus B = (A^c \ominus \check{B})^c
\]

其中 \( \check{B} \) 是 \( B \) 的反射（沿各轴翻转），\( A^c \) 是补集。这条对偶律是 `scipy.ndimage` 的设计基石：**只需实现一个侵蚀内核，膨胀免费得到**。

- **侵蚀**定义：\( A \ominus B = \{ z \mid (B)_z \subseteq A \} \)，即结构元整体落入前景时，中心点 \( z\) 才属于结果。
- **膨胀**定义：\( A \oplus B = \{ z \mid (B)_z \cap A \neq \emptyset \} \)，即结构元只要碰到前景，中心点 \( z\) 就属于结果。

#### 4.2.2 核心流程

`binary_erosion` 极其简单——它只是 `_binary_erosion` 的薄壳，把 `invert` 写死为 `0`：

```
binary_erosion(...) → _binary_erosion(..., invert=0, ...)
```

`binary_dilation` 则先做两件准备工作，再带着 `invert=1` 调同一个内核：

1. **翻转结构元**：`structure = structure[::-1, ::-1, ...]`，得到反射 \( \check{B} \)。
2. **取反 origin**：`origin[ii] = -origin[ii]`；若该轴结构元长度为偶数，再 `-= 1`（偶数长度核没有真正的中心，需像 u2-l1 卷积那样额外修正）。
3. 调用 `_binary_erosion(..., invert=1, ...)`，由内核里的 `invert` 标志完成「取反→侵蚀→取反」。

#### 4.2.3 源码精读

`binary_erosion` 一行委托：

[_morphology.py:L403-L404](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L403-L404) — 注意第 8 个位置参数 `0` 即 `invert=0`。

`binary_dilation` 的对偶变换（核心三步）：

[_morphology.py:L528-L543](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L528-L543) — 翻转结构元、取反 origin（偶数轴再 −1）、以 `invert=1` 调 `_binary_erosion`。膨胀没有独立内核。

`iterations` 与 `mask` 的语义（二者签名几乎一致）：

[_morphology.py:L305-L306](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L305-L306) — `binary_erosion` 的签名，`iterations=1` 默认一次，`iterations < 1` 表示反复直到收敛；`mask` 限制「只在掩膜为真的位置允许被修改」。

#### 4.2.4 代码实践

1. **实践目标**：用对偶律亲手验证「膨胀 = 取反→侵蚀→取反」。
2. **操作步骤**：

```python
import numpy as np
from scipy import ndimage

a = np.zeros((7, 7), bool)
a[3, 3] = True
struct = ndimage.generate_binary_structure(2, 1)  # 十字形

# 库函数膨胀
dil_lib = ndimage.binary_dilation(a, structure=struct)

# 手动用对偶律：dilation(A,B) = NOT erosion(NOT A, B_reflected)
B_ref = struct[::-1, ::-1]                 # 对称结构元，反射后不变
dil_manual = ~ndimage.binary_erosion(~a, structure=B_ref)

print("逐元素一致:", np.array_equal(dil_lib, dil_manual))
```

3. **需要观察的现象**：库函数膨胀结果与「取反→侵蚀→取反」手动实现完全一致。
4. **预期结果**：打印 `逐元素一致: True`。
5. 想进一步体会 `iterations`，可把 `a` 设为单个亮点、调用 `binary_dilation(a, iterations=2)`，与连续两次 `iterations=1` 比对，结果应一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `binary_dilation` 要在 origin 取反后，对偶数长度的轴再减 1？

**参考答案**：偶数长度结构元没有位于正中央的锚点，翻转后锚点的「等价中心」会偏移半个像素（与 u2-l1 中偶数长度卷积核的处理同源）。为了使「翻转结构元 + 取反 origin」后仍能与原结构元锚点严格对齐，必须在偶数轴上额外 `origin -= 1` 修正这半个像素的偏差。

**练习 2**：用一句话说明 `binary_dilation` 为什么不需要单独的 C 内核。

**参考答案**：由对偶律 \( A \oplus B = (A^c \ominus \check{B})^c \)，膨胀可改写为「翻转结构元 + 对补集做侵蚀」，而侵蚀内核 `_binary_erosion` 通过 `invert` 标志已能处理「取反→侵蚀→取反」，故膨胀只需在 Python 层翻转结构元、取反 origin 并置 `invert=1`，复用同一侵蚀内核即可。

---

### 4.3 binary_opening 与 binary_closing：开闭运算

#### 4.3.1 概念说明

开运算（opening）与闭运算（closing）是侵蚀与膨胀的**复合**，用来「先破坏再重建」，从而过滤掉尺度小于结构元的特征：

- **开运算** \( A \circ B = (A \ominus B) \oplus B \)：先侵蚀再膨胀。侵蚀会抹掉比结构元小的前景（毛刺、孤立点、细小物体），随后的膨胀把幸存大物体的尺寸大致恢复，但小物体不会回来。**用途：去小物体、去毛刺、平滑轮廓。**
- **闭运算** \( A \bullet B = (A \oplus B) \ominus B \)：先膨胀再侵蚀。膨胀会填掉比结构元小的背景孔洞、连通窄缝，随后的侵蚀把大物体尺寸恢复，但小孔不会重新出现。**用途：填小孔、连通断裂。**

二者也是对偶的：\( (A \circ B)^c = A^c \bullet \check{B} \)。

#### 4.3.2 核心流程

两个函数实现都极简，正对应它们的数学定义：

```
# opening = erosion → dilation（同一结构元、同一 iterations）
tmp    = binary_erosion (input, structure, iterations, ...)
return binary_dilation(tmp,   structure, iterations, ...)

# closing = dilation → erosion
tmp    = binary_dilation(input, structure, iterations, ...)
return binary_erosion (tmp,   structure, iterations, ...)
```

注意两步用**同一个** `structure` 与**同一个** `iterations`，且 `iterations` 同时作用于侵蚀步和膨胀步（各重复 `iterations` 次）。

#### 4.3.3 源码精读

`binary_opening` 的两行实现：

[_morphology.py:L670-L673](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L670-L673) — 先 `binary_erosion` 存入临时 `tmp`，再 `binary_dilation(tmp, ...)` 直接写入用户 `output`。

`binary_closing` 的两行实现（顺序相反）：

[_morphology.py:L823-L826](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L823-L826) — 先 `binary_dilation` 得 `tmp`，再 `binary_erosion(tmp, ...)` 写入 `output`。

#### 4.3.4 代码实践

1. **实践目标**：观察开运算去小物体、闭运算填小孔，并验证开运算 = 侵蚀后膨胀。
2. **操作步骤**：

```python
import numpy as np
from scipy import ndimage

a = np.zeros((7, 7), int)
a[1:6, 1:6] = 1   # 一个大方块
a[1, 1] = 0       # 边缘一个小缺口
a[6, 6] = 1       # 角落一个孤立小点

struct = np.ones((3, 3), int)

print("开运算去小点:\n", ndimage.binary_opening(a, structure=struct).astype(int))
print("与 侵蚀→膨胀 一致:",
      np.array_equal(ndimage.binary_opening(a, structure=struct),
                     ndimage.binary_dilation(ndimage.binary_erosion(a, structure=struct),
                                             structure=struct)))
```

3. **需要观察的现象**：开运算后角落的孤立小点 `(6,6)` 消失；输出与「先侵蚀再膨胀」逐元素一致。
4. **预期结果**：第二行打印 `True`，且结果数组中 `(6,6)` 变为 0。
5. 把 `binary_opening` 换成 `binary_closing`，可看到 `(1,1)` 的缺口被补上。

#### 4.3.5 小练习与答案

**练习 1**：开运算能「恢复」被侵蚀掉的大物体的尺寸，为什么却恢复不了被抹掉的小物体？

**参考答案**：侵蚀把比结构元小的物体**完全**抹成背景（这些像素已为 0）；随后的膨胀只能从「幸存的前景」向外扩张，被彻底抹掉的小物体周围没有前景种子可供膨胀复原，因而永久消失。大物体因侵蚀后仍有残留核心，膨胀能把它重新长回接近原尺寸。

**练习 2**：若想同时去掉小物体又填上小孔，应该先做开运算还是闭运算？顺序会影响结果吗？

**参考答案**：通常做法是「开→闭」或「闭→开」组合。顺序**会影响**结果，因为开运算可能改变物体形状从而影响后续闭运算能填的孔，反之亦然。实际中常依「先去噪还是先补洞」的优先级选择顺序，并无普适最优解。

---

### 4.4 binary_hit_or_miss：用两组结构元做模式匹配

#### 4.4.1 概念说明

击中击不中变换（hit-or-miss transform, HMT）是**形状模板匹配**：在图像中找出「恰好同时满足两个条件」的位置——

- **击中（hit）**：结构元 \( B_1 \) 完全落入前景（\( B_1 \) 在此处「匹配」前景形状）。
- **击不中（miss）**：结构元 \( B_2 \) 完全落入背景（\( B_2 \) 在此处「匹配」背景形状）。

数学定义：

\[
A \circledast (B_1, B_2) = (A \ominus B_1) \cap (A^c \ominus B_2)
\]

即「\( B_1 \) 能放进前景」**且**「\( B_2 \) 能放进背景」的位置集合。`structure2` 缺省时取 `structure1` 的逻辑反 `logical_not(structure1)`，这样 \( B_1 \cup B_2 \) 恰好覆盖整个结构元邻域，匹配的是「严格等于 \( B_1 \) 形状」的局部模式。

#### 4.4.2 核心流程

1. 缺省 `structure1 = generate_binary_structure(num_axes, 1)`；缺省 `structure2 = logical_not(structure1)`。
2. `origin2` 缺省时取 `origin1`。
3. 计算 `tmp1 = _binary_erosion(input, structure1, invert=0)` —— 即 \( A \ominus B_1 \)，\( B_1 \) 击中前景的位置。
4. 计算 `result = _binary_erosion(input, structure2, invert=1)`，再 `logical_not(result)` —— 借对偶得到 \( A^c \ominus B_2 \)，即 \( B_2 \) 击中背景的位置。
5. 返回 `logical_and(tmp1, result)` —— 两个条件同时成立的位置。

第 4 步看起来绕（先 `invert=1` 再 `logical_not`），原因见 4.2：`invert=1` 的语义是 \(\neg(A^c \ominus B)\)，再 `logical_not` 一次恰好抵消，得到纯净的 \( A^c \ominus B_2 \)。这是「复用膨胀的 invert 机制来算补集侵蚀」的巧妙之处。

#### 4.4.3 源码精读

结构元与 origin 的缺省处理：

[_morphology.py:L919-L932](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L919-L932) — `structure2 = logical_not(structure1)`；`origin2` 缺省取 `origin1`。

两次侵蚀 + 逻辑组合（HMT 的核心）：

[_morphology.py:L934-L944](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L934-L944) — `tmp1` 用 `invert=0`（击中前景），`result` 用 `invert=1` 后 `logical_not`（击中背景），最后 `logical_and`。注意它还区分了 `output` 是否为数组以决定就地写还是返回新数组。

#### 4.4.4 代码实践

1. **实践目标**：用 HMT 在图中定位一个特定朝向的 L 形角点。
2. **操作步骤**：

```python
import numpy as np
from scipy import ndimage

a = np.zeros((6, 6), int)
a[1:4, 1] = 1   # 一条竖线
a[3, 1:4] = 1   # 底部一条横线，组成 └ 形

# 要找的「右上角为前景、左下角为背景」的角点模式
s1 = np.array([[0, 0, 0],
               [1, 1, 0],
               [1, 1, 0]])
s2 = np.logical_not(s1)  # 缺省行为：补集

hits = ndimage.binary_hit_or_miss(a, structure1=s1).astype(int)
print("击中位置:\n", hits)
print("击中坐标:", np.argwhere(hits))
```

3. **需要观察的现象**：只有在输入图像里「\( B_1 \) 形状恰好落入前景且 \( B_2 \) 形状恰好落入背景」的像素处输出 1。
4. **预期结果**：`np.argwhere(hits)` 给出一个或少数几个坐标，对应 └ 形的角点位置。
5. 若传入不同 `structure1`，匹配位置会随之改变；这正是模板匹配的特性。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `binary_hit_or_miss` 的第 4 步要先 `invert=1` 再 `logical_not`，而不是直接做一次普通侵蚀？

**参考答案**：因为需要的是「对**补集** \( A^c \) 做侵蚀」即 \( A^c \ominus B_2 \)，而 `invert=1` 实现的是 \(\neg(A^c \ominus B)\)（为膨胀服务）。要得到不带外层取反的 \( A^c \ominus B_2 \)，就必须在 `invert=1` 之后再 `logical_not` 一次把那个外层取反抵消掉。普通侵蚀（`invert=0`）得到的是 \( A \ominus B \)，不是对补集操作，无法满足需求。

**练习 2**：当 `structure2` 取缺省值 `logical_not(structure1)` 时，HMT 匹配的是什么？

**参考答案**：此时 \( B_1 \cup B_2 \) 覆盖整个结构元邻域且不重叠，HMT 匹配的是「局部邻域**严格等于** \( B_1 \) 形状」的位置——即前景形状精确等于 \( B_1 \)、其余位置精确等于背景的精确模板。

---

### 4.5 binary_propagation 与 binary_fill_holes：膨胀到收敛的两项应用

#### 4.5.1 概念说明

这两个函数都是「让膨胀反复进行直到结果不再变化（`iterations < 0`）」的应用，但用法截然不同：

- **`binary_propagation`（传播）**：从一个**种子** `input` 出发，在 **掩膜** `mask` 限定的区域内反复膨胀，直到填满掩膜。它等价于 `binary_dilation(input, structure, iterations=-1, mask=mask)`。典型用法是「形态学重建」：先侵蚀原图得到种子，再在原图掩膜内传播，可在大物体内部「长回去」而小物体（被侵蚀光了的）不会复活——这是 `binary_opening` 的一种保轮廓变体。

- **`binary_fill_holes`（填孔）**：目标是把前景物体**内部**的背景孔洞填成前景，但不影响外部背景。它的巧妙算法是「从图像外边界入侵补集」：
  1. 令 `mask = logical_not(input)`（把背景当可通行区域）。
  2. 令种子 `tmp = 全 False`，但设 `border_value=1`，使**图像四周边界**充当膨胀起点。
  3. 在 `mask` 内反复膨胀（`iterations=-1`）：背景中**与外边界连通**的部分被填满，而**被前景包围的孔洞**（不与外边界连通）无法被入侵，保持 False。
  4. 对结果取反：未被入侵的孔洞变成前景，于是孔被填上。

数学上，填孔结果 = \( A \cup (\text{ holes}) \)，其中 holes 是「补集中不与边界连通的连通块」。

#### 4.5.2 核心流程

**`binary_propagation`** 一行：

```
return binary_dilation(input, structure, -1, mask, output, border_value, origin, axes=axes)
```

`iterations=-1` 触发 `_binary_erosion` 第三分支的「膨胀到收敛」循环；`mask` 限制膨胀只能在掩膜为真的位置生长。

**`binary_fill_holes`**（非就地分支）：

```
mask = logical_not(input)                       # 背景为可通行
tmp  = zeros(mask.shape, bool)                  # 全 False 种子
output = binary_dilation(tmp, structure, -1, mask, None,
                         border_value=1, origin, axes=axes)  # 边界为 1 当种子
output = logical_not(output)                    # 取反 → 孔被填上
return output
```

关键点：`border_value=1` 让边界充当种子，`mask` 把膨胀限制在背景，于是只有「与边界连通的背景」被填，孔洞（与边界不连通）保持 False，取反后变前景。

#### 4.5.3 源码精读

`binary_propagation` 的单行实现：

[_morphology.py:L1079-L1080](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L1079-L1080) — 即带 `mask`、`iterations=-1` 的 `binary_dilation`。其 Notes 明确指出它等价于 `binary_dilation(..., iterations<1)`，且「侵蚀 + 掩膜内传播」可作为 opening 的保轮廓替代（见 [_morphology.py:L982-L990](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L982-L990)）。

`binary_fill_holes` 的「边界入侵补集」算法：

[_morphology.py:L1160-L1171](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L1160-L1171) — `mask = logical_not(input)`、种子 `tmp` 全零、`border_value=1` 让边界当种子，膨胀到收敛后 `logical_not` 取反。区分 `output` 是否为数组以决定就地或返回。算法说明见 [_morphology.py:L1119-L1125](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L1119-L1125)。

> 坑点：`structure` 过大时，膨胀可能「跨过」把孔洞与背景隔开的细前景条带，导致本应被填的孔没被填（因为入侵者从细缝漏进来了）；反之，过大的结构元也可能让入侵「跳过」窄孔。文档示例 [_morphology.py:L1151-L1157](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_morphology.py#L1151-L1157) 就演示了 `structure=np.ones((5,5))` 时中心孔未被填上的情形。

#### 4.5.4 代码实践

1. **实践目标**：用 `binary_fill_holes` 填孔，并手动复现其「边界入侵补集」过程，验证一致性。
2. **操作步骤**：

```python
import numpy as np
from scipy import ndimage

# 一个带孔的方框
a = np.zeros((7, 7), int)
a[1:6, 1:6] = 1
a[3, 3] = 0          # 中心一个孔
a[2, 2] = 0          # 再挖一个孔

filled = ndimage.binary_fill_holes(a).astype(int)
print("填孔结果:\n", filled)

# 手动复现：边界入侵补集
mask = ~a.astype(bool)
seed = np.zeros_like(a, dtype=bool)
invaded = ndimage.binary_dilation(seed, structure=ndimage.generate_binary_structure(2, 1),
                                  iterations=-1, mask=mask, border_value=1)
manual = ~invaded
print("与手动一致:", np.array_equal(filled.astype(bool), manual))
```

3. **需要观察的现象**：`filled` 中 `(3,3)`、`(2,2)` 两个孔都变成 1，且与手动「边界入侵补集再取反」完全一致。
4. **预期结果**：打印 `与手动一致: True`，`filled` 内部两孔被填。
5. 若把 `a` 的孔改成「与外边界连通」（例如 `a[0,3]=0` 打穿到边），则该处不会被填——因为它属于「与边界连通的背景」。

#### 4.5.5 小练习与答案

**练习 1**：`binary_fill_holes` 为什么必须设 `border_value=1`？若改成 `border_value=0` 会怎样？

**参考答案**：种子 `tmp` 全为 False，膨胀的起点来自边界。`border_value=1` 把图像四周边界当作「初始前景种子」，膨胀才能从边界向内沿背景蔓延。若 `border_value=0`，则没有任何种子，膨胀结果恒为全 False，取反后变全 True——会把整张图（含真正前景外部）都填满，完全失去「只填内部孔洞」的语义。

**练习 2**：用 `binary_propagation` 实现「保留大物体轮廓、删除小物体」，并与 `binary_opening` 对比差异。

**参考答案**：先 `seed = binary_erosion(input, structure)` 得到种子，再 `binary_propagation(seed, mask=input)` 在原图掩膜内传播。被侵蚀光的小物体没有种子、不会复活；大物体残留核心会在自身掩膜内长回原形，故轮廓比 `binary_opening`（膨胀用同一结构元、可能轻微外扩）更贴近原图。`binary_opening` 的膨胀步骤不限于原图掩膜，可能改变边界；而 propagation 受 `mask=input` 约束，重建结果必为原图子集，轮廓保真度更高。

---

## 5. 综合实践

把本讲所有概念串起来，完成一个「二值图清理」小流水线：

```python
import numpy as np
from scipy import ndimage

# 造一张含多种瑕疵的二值图：大物体 + 小噪点 + 内部孔洞 + 边缘毛刺
img = np.zeros((15, 15), int)
img[3:12, 3:12] = 1      # 大方块
img[7, 7] = 0            # 内部孔
img[1, 1] = 1            # 孤立小点（噪点）
img[0, 5] = img[2, 5] = 1  # 大方块外的毛刺/小点
img[2, 3] = 1            # 紧贴大块的突出像素（毛刺）

struct = ndimage.generate_binary_structure(2, 1)

# 步骤 1：开运算去噪点与毛刺
opened = ndimage.binary_opening(img, structure=struct).astype(int)
# 步骤 2：闭运算 / 填孔
cleaned = ndimage.binary_fill_holes(opened).astype(int)

print("原图前景数:", img.sum())
print("清理后前景数:", cleaned.sum())
print("清理后:\n", cleaned)

# 验证开运算 = erosion → dilation
assert np.array_equal(opened,
                      ndimage.binary_dilation(ndimage.binary_erosion(img, structure=struct),
                                              structure=struct))
# 验证膨胀是侵蚀的对偶
assert np.array_equal(ndimage.binary_dilation(img, structure=struct),
                      ~ndimage.binary_erosion(~img.astype(bool),
                                              structure=struct[::-1, ::-1]))
print("全部断言通过")
```

**任务要求**：
1. 运行上述脚本，观察 `opened`（小噪点与孤立毛刺消失，但内部孔仍在）与 `cleaned`（孔被填上）的差异。
2. 解释为什么步骤 1 用开运算、步骤 2 用 `binary_fill_holes`，而不是反过来。
3. 把 `struct` 换成 `np.ones((3,3), int)`（8-连通），重跑并对比前景数量变化，思考结构元连通性对「何为小物体」的影响。
4. 选一个含已知角点形状的图，用 `binary_hit_or_miss` 定位该形状，把结果叠加到清理后的图上观察。

> 步骤 2 顺序提示：先开运算去噪，能让后续填孔不被外部小噪点干扰；若先填孔，外部噪点也可能被当作小物体「保护」下来，反而难以清除。

## 6. 本讲小结

- **一个内核统治一切**：`_binary_erosion` 是二值形态学的唯一真正内核，`binary_erosion` / `binary_dilation` / `binary_hit_or_miss` / `binary_propagation` / `binary_fill_holes` 全部最终落到它和 C 扩展 `_nd_image.binary_erosion` / `binary_erosion2`。
- **三条执行路径**：`iterations==1` 单次全扫；`cit and not brute_force` 用坐标列表只追踪变化像素（快速路径）；其余情况用双缓冲循环、可膨胀到收敛（`iterations<1`）。
- **对偶律省了一个内核**：膨胀没有独立 C 内核，靠「翻转结构元 + 取反 origin（偶数轴再 −1）+ `invert=1`」复用侵蚀内核，对应 \( A \oplus B = (A^c \ominus \check{B})^c \)。
- **开闭运算是侵蚀膨胀的复合**：`opening = erosion→dilation`（去小物体）、`closing = dilation→erosion`（填小孔），二者对偶。
- **击中击不中是双结构元模板匹配**：\( (A \ominus B_1) \cap (A^c \ominus B_2) \)，靠两次侵蚀（一次 `invert=0`、一次 `invert=1` 后取反）加逻辑与实现。
- **传播与填孔是「膨胀到收敛」的两项应用**：传播在掩膜内长满；填孔从边界入侵补集、取反得到孔洞，关键在 `border_value=1` 把边界当种子、`mask = logical_not(input)` 把膨胀限在背景。

## 7. 下一步学习建议

- **u5-l3 灰度形态学、梯度与 top-hat**：把本讲的「前景/背景集合」推广到灰度值，`grey_erosion` / `grey_dilation` 变成「局部最小/最大」，并引入 `morphological_gradient`、`white_tophat` 等组合。理解了本讲的对偶与开闭运算，灰度形态学只是把布尔运算换成 max/min。
- **u5-l4 距离变换 bf / cdt / edt**：另一族形态学相关函数，用于计算每个前景像素到最近背景的距离，与填孔、骨架化等配合密切。
- **u4-l1 连通区域标记 label**：`binary_fill_holes` 的「补集连通性」思路与 `label` 的连通分量算法同源，对照阅读能加深对「连通」的理解。
- **源码延伸**：若想看 `invert` 标志在 C 端如何真正「取反→侵蚀→取反」，以及坐标列表快速路径的具体实现，可在 u6 单元阅读 `_nd_image` 扩展对应的 `ni_morphology.c` 与 `methods[]` 分发表。
