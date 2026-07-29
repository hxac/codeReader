# 布局操作：compose、divide 与 reduce

## 1. 本讲目标

上一讲（u4-l3）我们已经认识了三种 Layout 的静态结构：SharedLayout、GlobalLayout、TMemoryLayout，以及更早（u4-l2）讲的 RegisterLayout 的 mode/spatial/local 模型。但「一个布局是怎么造出来的」一直是个黑箱——本讲就把这个黑箱打开。

本讲聚焦 `python/tilus/ir/layout/ops/` 下的**布局代数（layout algebra）**。学完后你应当能够：

1. 用 `spatial` / `local` / `replicated` 三大原语从零拼出一个 `RegisterLayout`，并说出它对应多少线程、每线程持有多少元素。
2. 解释 `compose`（外层×内层）的 **mode 拼接语义**，能手算 `compose(outer, inner)` 后的 `shape` / `mode_shape` / `spatial_modes` / `local_modes`。
3. 理解 `divide` 与 `left_divide` 是 compose 的左、右逆运算，能判断一组布局能否相除。
4. 会用 `reduce` / `reshape` / `permute` / `concat` 对布局做形状变换。
5. 理解 `MultiFunction` 上的 `cover` / `collapse` 如何作为「布局等价性验证」的工具，被布局推理的验证规则（如 `AssignRule`）调用。

一句话定位：**布局操作是把「数学映射」当对象做代数运算的函数库**，它是布局自动推理（u4-l5）和后端发射器（U6）共同依赖的底座。

## 2. 前置知识

在进入源码前，先用通俗语言回顾三个关键概念（细节见 u4-l2、u4-l3）。

- **mode（模式）**：把张量的一个维度进一步细分成若干小段，每段叫一个 mode。例如维度 64 可以拆成 `mode_shape=[8, 8]` 两个 mode。`grouped_modes` 记录「每个维度由哪些 mode 组成」。
- **spatial / local**：对 `RegisterLayout` 而言，mode 分两类——spatial mode 跨线程分布（决定一个元素归哪个线程），local mode 是线程内槽位（决定一个线程在自己寄存器里存几个元素）。
- **负数 mode（复制）**：spatial mode 里出现负数 `-k` 表示「这个元素被 k 个线程重复持有」，即 replicated。

本讲要做的，本质上是对「逻辑索引 → 物理位置」这层映射做加减乘除：拼接、切分、归并、置换。你会看到这套代数和 `numpy` 的 reshape/transpose 很像，但作用对象是「带线程分布信息的布局」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/tilus/ir/layout/ops/register_ops.py` | RegisterLayout 的全部代数运算：`spatial`/`local`/`replicated` 创建、`compose`/`divide`/`left_divide`、`reduce`/`reshape`/`permute`/`concat` 等。本讲主角。 |
| `python/tilus/ir/layout/ops/shared_ops.py` | SharedLayout 的代数运算：`shared_compose`/`shared_permute`/`shared_reshape` 等，结构与 register 版对应但作用于共享内存布局。 |
| `python/tilus/ir/layout/mfunction/ops.py` | `MultiFunction` 上的运算：`identity`/`collapse`/`collapse_by_shape`/`cover`，是布局验证的核心。 |
| `python/tilus/ir/layout/ops/utils.py` | `get_mode_groups`（把 mode 归并到维度）与 `LayoutOperationError`，被所有运算共用。 |
| `python/tilus/ir/layout/register_layout.py` | `RegisterLayout` 类定义、`register_layout` 工厂、`canonicalize_layout` 规范化，以及 `__mul__`/`__truediv__` 运算符重载。 |
| `python/tilus/ir/layout/inference/validation_rules/assign.py` | `AssignRule`，真实调用 `cover` 验证赋值合法性的范例。 |

---

## 4. 核心概念与源码讲解

### 4.1 创建布局的三大原语：spatial、local、replicated

#### 4.1.1 概念说明

任何 `RegisterLayout` 都可以用三个原语拼出来：

- `spatial(*shape)`：所有维度都做成 **spatial mode**，即把张量**完全铺到线程上**，每个线程只拿 1 个元素（线程数 = 元素总数）。
- `local(*shape)`：所有维度都做成 **local mode**，即张量**整体存在每个线程的局部**（每个线程都持有一份完整副本，线程数为 1）。
- `replicated(*shape, num_workers)`：**完全复制**，整个张量被 `num_workers` 个线程**人手一份**。

三者都接受可选的 `ranks` 参数，控制维度线性化成线程号 / 局部号的顺序（行列优先）。`ranks=None` 时默认行主序 `[0,1,2,...]`。

#### 4.1.2 核心流程

`spatial` 的构造逻辑很简洁：把 `shape` 直接当 `mode_shape`，每个维度对应一个 spatial mode，`local_modes` 留空。

```text
spatial(d0, d1, ...) →
    shape        = (d0, d1, ...)
    mode_shape   = (d0, d1, ...)
    spatial_modes= [0, 1, ...]   # 每个 mode 都是 spatial
    local_modes  = []
```

`local` 完全对称，只是把 mode 全部归入 `local_modes`、`spatial_modes` 留空。

`replicated` 稍巧：它先造一个「空 shape 的复制单元」`spatial_modes=(-num_workers,)`（负数表示复制），再用 compose（见 4.2）拼上 `local(*shape)`，从而得到「每个线程各持一整份」的布局。

#### 4.1.3 源码精读

`spatial` 在校验 `ranks` 后，直接调工厂 `register_layout`，`local_modes` 为空：

[python/tilus/ir/layout/ops/register_ops.py:29-62](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L29-L62) —— `spatial` 把所有 mode 标记为 spatial，决定元素如何分配给线程。

`local` 与之镜像，`spatial_modes=[]`：

[python/tilus/ir/layout/ops/register_ops.py:65-98](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L65-L98) —— `local` 把所有 mode 标记为 local，决定每线程持有的局部元素。

`replicated` 用「复制单元 × local」实现，关键在第一行的 `spatial_modes=(-num_workers,)`：

[python/tilus/ir/layout/ops/register_ops.py:101-123](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L101-L123) —— `replicated` 先造复制单元，再用乘法（即 compose）拼上 local。

> 💡 运算符糖：`RegisterLayout.__mul__` 就是 `compose`，`__truediv__` 就是 `divide`（见 [register_layout.py:55-69](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L55-L69)）。所以 `spatial(2,4).local(2,1)` 等价于 `compose(spatial(2,4), local(2,1))`，本讲后面会大量用到。

#### 4.1.4 代码实践

**目标**：体会三个原语对应的线程数与每线程元素数。

```python
# 示例代码
from tilus.ir.layout.ops import spatial, local, replicated

s = spatial(2, 4)                 # 8 个元素全铺到线程
print(s.spatial_size, s.local_size)   # 预期 8 1  （8 线程，每线程 1 元素）

l = local(2, 4)                   # 整份存于单线程
print(l.spatial_size, l.local_size)   # 预期 1 8  （1 线程，持 8 元素）

r = replicated(2, 4, num_workers=4)  # 4 个线程各持一整份
print(r.spatial_size, r.local_size)   # 预期 4 8
```

**操作步骤**：把上面代码存成 `try_primitive.py`，用 `python try_primitive.py` 运行（需已安装 tilus）。

**需要观察的现象**：`spatial` 的 `spatial_size` 等于元素总数、`local_size` 为 1；`local` 恰好相反；`replicated` 的 `spatial_size` 等于 `num_workers`。

**预期结果**：注释中的「预期」值。若你的环境无 GPU 也不影响——布局代数是纯 CPU 计算，不依赖 CUDA。

#### 4.1.5 小练习与答案

**练习 1**：`spatial(8)` 和 `spatial(2, 4)` 在「线程数」「每线程元素数」上有何异同？

**答案**：两者 `spatial_size` 都是 8（8 个线程）、`local_size` 都是 1（每线程 1 元素）。区别只在维度的切分方式（一维 vs 二维），这会影响与其它布局 compose/permute 时的 mode 对齐关系，但「线程分布的总量」一致。

**练习 2**：`replicated(16, num_workers=4)` 中每个线程持有多少元素？`spatial_modes` 里会出现什么？

**答案**：每个线程持 16 个元素（一整份）。`spatial_modes` 会出现 `-4`（复制 4 份），其余 mode 进入 `local_modes`。

---

### 4.2 compose：外层×内层的 mode 拼接

#### 4.2.1 概念说明

`compose(outer, inner)` 是布局代数里**最核心**的运算，它把两个布局「嵌套」起来：外层 `outer` 描述粗粒度的分块（例如 tile 划分到线程位置），内层 `inner` 描述细粒度的内部排布（例如每个线程位置内持有的 fragment）。

直观上，compose 逐维度做乘法：

\[
\text{shape}_{\text{new}}[i] = \text{shape}_{\text{outer}}[i] \times \text{shape}_{\text{inner}}[i]
\]

而 mode 列表则是**逐维度地把 outer 的 mode 放前面、inner 的 mode 放后面**拼起来。所以：

\[
\text{len}(\text{mode\_shape}_{\text{new}}) = \text{len}(\text{mode\_shape}_{\text{outer}}) + \text{len}(\text{mode\_shape}_{\text{inner}})
\]

compose 不改变 mode 的「类别」——outer 的 spatial mode 仍是 spatial，inner 的 local mode 仍是 local，只是各自重编号后顺序拼接。这正是 `spatial(8,8).local(8,8)` 能造出「64 个线程、每线程 64 元素」的 [64,64] 布局的原因。

#### 4.2.2 核心流程

```text
compose(outer, inner):
  1. 对齐 ndims：用 unsqueeze 给较短的 shape 前补若干长度为 1 的维度
  2. 新 shape[i] = outer.shape[i] * inner.shape[i]
  3. 逐维度遍历 grouped_modes：
       - 先把 outer 在该维度的所有 mode 追加进新 mode_shape（记录重编号 outer_map）
       - 再把 inner 在该维度的所有 mode 追加进新 mode_shape（记录重编号 inner_map）
  4. spatial_modes = [重编号后的 outer.spatial_modes] + [重编号后的 inner.spatial_modes]
     local_modes  = [重编号后的 outer.local_modes ] + [重编号后的 inner.local_modes ]
  5. 交给 register_layout 工厂做规范化（canonicalize）
```

关键点：**拼接是「按维度分组」进行的**，outer 和 inner 在同一维度上的 mode 会交织（outer 在前 inner 在后），但不会跨维度混淆。这依赖 `grouped_modes`——把 `mode_shape` 重新归并回 `shape` 的每个维度。

#### 4.2.3 源码精读

`get_mode_groups` 是 compose/divide 的公共地基，它按 `shape` 把 `mode_shape` 切回各维度：

[python/tilus/ir/layout/ops/utils.py:24-64](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/utils.py#L24-L64) —— `get_mode_groups` 把 mode 序列按维度分组，例如 `shape=[64,32], mode_shape=[8,8,16,2]` 得到 `[[0,1],[2,3]]`。

`compose` 主体：先 `unsqueeze` 对齐 ndims，再按 `grouped_modes` 逐维度拼接 mode 并维护 `outer_map`/`inner_map` 重编号表，最后重组 spatial/local：

[python/tilus/ir/layout/ops/register_ops.py:231-290](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L231-L290) —— compose 的逐维度拼接核心：mode_shape 来自 outer+inner 拼接，spatial/local 各自重编号后拼接。

配套测试给出了一个可手算的样例：

[tests/ir/layout/register_layout/test_layout_compose.py:19-31](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/ir/layout/register_layout/test_layout_compose.py#L19-L31) —— `compose(spatial(2,4), local(2,1))` 期望得到 `shape=[4,4], mode_shape=[2,2,4], spatial_modes=[0,2], local_modes=[1]`。

我们来手算这个样例，验证 4.2.2 的流程：

- outer=`spatial(2,4)` → shape=(2,4), mode_shape=(2,4), spatial_modes=(0,1), local_modes=()
- inner=`local(2,1)` → 规范化后 shape=(2,1), mode_shape=(2,), spatial_modes=(), local_modes=(0)（长度 1 的 singleton mode 被去掉）
- 新 shape = `[2·2, 4·1] = [4, 4]` ✓
- 维度 0：outer 的 mode 0（size 2）→新 mode 0；inner 的 mode 0（size 2）→新 mode 1。mode_shape 累积 `[2, 2]`
- 维度 1：outer 的 mode 1（size 4）→新 mode 2；inner 无 mode。mode_shape 累积 `[2, 2, 4]` ✓
- spatial_modes = outer 的 [0,1] 重编号为 [0,2]，inner 无 spatial → `[0, 2]` ✓
- local_modes = outer 无 local，inner 的 [0] 重编号为 [1] → `[1]` ✓

结果与测试断言完全一致。它描述一个 16 元素的 [4,4] 张量：spatial_modes [0,2]（size 2 与 4）给出 8 个线程，local_modes [1]（size 2）给出每线程 2 元素，8×2=16 ✓。

#### 4.2.4 代码实践

**目标**：用 compose 把「外层分块」与「内层线程布局」组合，验证 shape 与 mode 数量的关系。

```python
# 示例代码
from tilus.ir.layout.ops import spatial, local, compose

outer = spatial(8, 8)        # 外层：64 个线程位置（粗分块）
inner = local(8, 8)          # 内层：每个位置持 8x8=64 元素（细排布）
layout = compose(outer, inner)

print("shape        :", layout.shape)            # 预期 (64, 64)
print("mode_shape   :", layout.mode_shape)       # 预期 (8, 8, 8, 8)
print("spatial_modes:", layout.spatial_modes)    # 预期 (0, 2)
print("local_modes  :", layout.local_modes)      # 预期 (1, 3)
print("spatial_size :", layout.spatial_size)     # 预期 64  (= 线程数)
print("local_size   :", layout.local_size)       # 预期 64  (每线程元素数)
```

**操作步骤**：保存运行。重点是核对两个数量关系。

**需要观察的现象**：
1. 组合后 `shape = (64, 64)`，是外层 `(8,8)` 与内层 `(8,8)` 逐维相乘。
2. `mode_shape` 的长度 = 外层 mode 数 + 内层 mode 数 = 2 + 2 = 4，即 `(8,8,8,8)`。
3. `spatial_size`（线程数）与 `local_size`（每线程元素数）相乘恰好等于总元素数 64×64=4096。

**预期结果**：注释中的「预期」值——这些值可由 4.2.2 的流程严格推出。

#### 4.2.5 小练习与答案

**练习 1**：若把上面的 `inner = local(8, 8)` 换成 `inner = local(4, 16)`，组合后的 `shape` 和 `local_size` 会变成什么？

**答案**：`shape` 变为 `(8·4, 8·16) = (32, 128)`。`local_size = 4·16 = 64`（每线程仍持 64 元素），`spatial_size` 仍为 64。可见「内层 mode 的切分方式」影响最终 shape，但只要 local 乘积不变，每线程元素数就不变。

**练习 2**：`compose` 为什么要求 outer 与 inner 的 ndims 一致（不一致时用 unsqueeze 对齐）？

**答案**：compose 是**逐维度**做 mode 拼接的，必须让两个布局的维度一一对应。ndims 不同时，给较短者在最前面补长度 1 的维度（unsqueeze），使其与较长者对齐，才能正确地把「同一维度」的 outer/inner mode 配对。

---

### 4.3 divide 与 left_divide：compose 的左右逆运算

#### 4.3.1 概念说明

既然有「乘」（compose），自然就有「除」。Tilus 提供两个方向的除法：

- `divide(lhs, rhs)`：求一个布局 `result`，使得 \(\text{lhs} = \text{compose}(\text{result}, \text{rhs})\)。即 **rhs 是 lhs 的「后缀」内层**，divide 把它剥掉。
- `left_divide(layout, lhs_divisor)`：求 `result`，使得 \(\text{layout} = \text{compose}(\text{lhs\_divisor}, \text{result})\)。即 **lhs_divisor 是 layout 的「前缀」外层**，left_divide 把它剥掉。

直觉上：

\[
\text{divide}(A, B) = C \;\iff\; A = C \cdot B
\]
\[
\text{left\_divide}(A, B) = C \;\iff\; A = B \cdot C
\]

「除不尽」时（B 不是 A 的合法后缀/前缀），抛 `LayoutOperationError`。因此 divide/left_divide 既是运算也是**判定**：能除就说明存在子布局关系。

#### 4.3.2 核心流程

`divide` 的流程（注释里写得很清楚）：

```text
divide(lhs, rhs):
  0. 规范化两者；若 lhs.ndim < rhs.ndim 直接报错；否则给 rhs 补维
  1. 细化 lhs 的 mode_shape，使 rhs 的 grouped_modes 恰成 lhs 每组的后缀
  2. 校验：
     2.1 rhs 的 grouped_modes 必须是 lhs 每组的后缀（mode 尺寸逐一相等）
     2.2 rhs 的 spatial/local modes 也必须是 lhs 对应序列的后缀
  3. 构造结果：
     3.1 结果 shape[i] = lhs.shape[i] // rhs.shape[i]
     3.2 结果 mode_shape = 从 lhs 每组「砍掉后缀」剩下的 mode
     3.3 结果 spatial/local = lhs 砍掉后缀后、按 mode_map 重编号
```

`left_divide` 流程对称，只是把「后缀」换成「前缀」：rhs 的 grouped_modes 必须是 lhs 每组的**前缀**，spatial/local 也是前缀对齐。

#### 4.3.3 源码精读

`divide` 的判定与构造（含详细的分步注释）：

[python/tilus/ir/layout/ops/register_ops.py:606-737](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L606-L737) —— divide：先细化 mode_shape 让 rhs 成为后缀，再校验后缀关系，最后砍后缀、重编号得到结果。

`left_divide` 与之镜像，处理前缀：

[python/tilus/ir/layout/ops/register_ops.py:740-883](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L740-L883) —— left_divide：把 lhs_divisor 作为前缀剥除，是 compose 左因子分解。

配套测试展示了多个可除 / 不可除的情形：

[tests/ir/layout/register_layout/test_layout_divide.py:19-36](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/ir/layout/register_layout/test_layout_divide.py#L19-L36) —— 例如 `divide(spatial(2,4).local(2,1), local(2,1))` 还原出 `spatial(2,4)`，验证了 divide 是 compose 的右逆。

> 💡 真实用法：Blackwell 的 tcgen05 发射器用 `left_divide` 从整体布局里剥除外层的 warp 划分，得到单个 warp 内的布局——见 [backends/emitters/cuda/tcgen05/ldst.py:86](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/tcgen05/ldst.py#L86) `left_divide(entire_layout, lhs_divisor=spatial(num_warps, 1))`。

#### 4.3.4 代码实践

**目标**：验证 `divide` 是 `compose` 的右逆——把内层剥掉，应还原外层。

```python
# 示例代码
from tilus.ir.layout.ops import spatial, local, compose, divide

outer = spatial(8, 8)
inner = local(8, 8)
layout = compose(outer, inner)        # 见 4.2.4

back = divide(layout, inner)          # 剥掉内层
print("divide 还原 outer :", back == outer)   # 预期 True
print("back             :", back)
```

**操作步骤**：在 4.2.4 的脚本后追加运行。

**需要观察的现象**：`divide(layout, inner)` 的结果与最初的 `outer` 完全相等。

**预期结果**：`back == outer` 为 `True`。该结论可由 divide 的「砍后缀」流程严格推出（inner 的两个 local mode 恰是 layout 每组的后缀）。若尝试 `divide(layout, spatial(8,8))`（剥外层），则得到 `local(8,8)`——可自行验证。

#### 4.3.5 小练习与答案

**练习 1**：`divide(spatial(6), spatial(3))` 为什么能成功，结果是什么？（提示：看测试里的 `spatial(6) / spatial(3) == spatial(2)`）

**答案**：虽然 `spatial(6)` 的 mode_shape 是 `(6,)`、`spatial(3)` 是 `(3,)`，6≠3，但 divide 的第 0 步会**细化** mode_shape：把 6 拆成 `(2, 3)`，使 `3` 成为后缀，校验通过后砍掉后缀 `3`，剩下 `2`，结果 `spatial(2)`。这正是 divide 注释里「refine the mode_shape」的作用。

**练习 2**：把 `left_divide` 用一句话与 `divide` 区分。

**答案**：`divide(A,B)` 剥的是 **A 的内层（后缀）**，满足 A = result·B；`left_divide(A,B)` 剥的是 **A 的外层（前缀）**，满足 A = B·result。

---

### 4.4 形状变换：reduce、reshape、permute、concat

#### 4.4.1 概念说明

除了乘除，布局代数还提供一组「单布局变换」，语义与 numpy 对应操作接近，但要同步维护 mode 的类别与线程分布：

- `reduce(layout, dims, keepdims=...)`：沿 `dims` 归约。被归约的 spatial/local mode 不会消失，而是**变成复制 mode**（负数），表示该维度上所有线程现在持有同一值——这对应「归约后结果广播回原线程分布」。
- `reshape(layout, new_shape)`：改变形状，元素总数不变。它会把相邻 mode 拆分/合并以适配新 shape，**无法表示时抛错**。
- `permute(layout, dims)`：维度置换（类似 transpose），同步重排 mode 与 spatial/local 序列。
- `concat(lhs, rhs)`：把两个布局的维度**首尾拼接**（不是按元素乘，而是 ndim 相加）。

SharedLayout 有同名的对应版本（`shared_reshape`/`shared_permute`/`shared_compose`），区别仅在于作用于 `mode_strides + swizzle` 而非 spatial/local。

#### 4.4.2 核心流程

`reduce` 的特别之处——它不删 mode，而是把被归约的 mode 改写为复制：

```text
reduce(layout, dims):
  对每个属于 dims 的 mode m：
      若 m 是 spatial mode  -> 改写成 -mode_shape[m]（变成复制 mode）
      若 m 是 local  mode   -> 直接丢弃
  结果 shape：keepdims=True 时被归约维度置 1，否则删除该维度
```

把 spatial mode 变成负数（复制）的用意：归约发生后，原本分布在不同线程的值现在**每个线程都有一份相同的结果**，所以这些线程在归约维度上「复制」持有。

`reshape` 则依赖 `canonicalize_layout` 先合并相邻同类 mode，再尝试用新 shape 逐维「吸收」mode，遇不能整除即报错。

#### 4.4.3 源码精读

`reduce`：把归约维度上的 spatial mode 转成复制（负数），local mode 丢弃：

[python/tilus/ir/layout/ops/register_ops.py:338-409](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L338-L409) —— reduce 的关键：`spatial_modes.append(-layout.mode_shape[spatial_dim])`，把归约维度变成复制 mode，保证归约结果广播回所有线程。

`reshape`：先规范化，再逐维用新 shape 切分 mode，无法切分则报错：

[python/tilus/ir/layout/ops/register_ops.py:465-542](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L465-L542) —— reshape 通过 `q % p == 0` / `p % q == 0` 两条分支切分 mode，体现「只能在 mode 边界重新分组」的约束。

`permute`：按 `dims` 重排 grouped_modes，并构造 mode_map 重编号 spatial/local：

[python/tilus/ir/layout/ops/register_ops.py:293-335](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L293-L335) —— permute 改变维度顺序，同步维护 mode 到 spatial/local 的映射。

`concat`：直接拼接两个布局的 shape/mode_shape，rhs 的 mode 编号整体偏移：

[python/tilus/ir/layout/ops/register_ops.py:438-462](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L438-L462) —— concat 拼接维度而非相乘，rhs 的 spatial/local mode 加偏移后追加。

SharedLayout 的 `shared_reshape` 结构对应，但作用于 strides：

[python/tilus/ir/layout/ops/shared_ops.py:195-253](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/shared_ops.py#L195-L253) —— shared_reshape 用 gcd 切分 mode，同样受「必须在 mode 边界切分」约束。

#### 4.4.4 代码实践

**目标**：观察 `reduce` 把 spatial mode 变成复制 mode 的现象。

```python
# 示例代码
from tilus.ir.layout.ops import spatial, local, reduce

layout = spatial(4, 4).local(2, 2)    # 16 线程，每线程 4 元素，shape (8,8)
print("归约前 spatial_modes:", layout.spatial_modes)

r = reduce(layout, dims=[1], keepdims=False)   # 沿维度 1 归约
print("归约后 shape        :", r.shape)
print("归约后 spatial_modes:", r.spatial_modes)  # 维度1的spatial mode 变成负数(复制)
print("归约后 spatial_size :", r.spatial_size)
```

**操作步骤**：保存运行，对比归约前后的 `spatial_modes`。

**需要观察的现象**：归约前 `spatial_modes` 全是非负数（正常分布）；归约后维度 1 对应的 spatial mode 变成负数（如 `-4`），表示该维度上 4 个线程现在复制持有相同值；`local_size` 不变。

**预期结果**：维度 1 的 spatial mode 由正变负；`shape` 的第 1 维被移除（`keepdims=False`）。具体数值可由 reduce 流程推出；若环境不便运行，此为「待本地验证」的观察项，重点是理解「正→负」的语义。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `reduce` 把 spatial mode 改成复制（负数），而不是直接删掉？

**答案**：因为归约后，原本分布在线程上的部分和被合并成一个总值，这个总值需要**广播回所有参与归约的线程**，让每个线程都拿到同一份结果。改成复制 mode（`-k`）正好表达「k 个线程各持一份相同值」，保留了线程分布结构，便于后续与其它布局对齐。

**练习 2**：`reshape(layout, new_shape)` 在什么情况下会抛 `LayoutOperationError`？

**答案**：当 `new_shape` 无法在 mode 边界重新切分时——即某个新维度大小与现有 mode 大小既不能整除也不能被整除（`q % p != 0` 且 `p % q != 0`）。源码里对应两条切分分支都失败即报错。

---

### 4.5 MultiFunction 的 cover 与 collapse：布局验证

#### 4.5.1 概念说明

`MultiFunction`（u4-l2 已介绍）是「逻辑索引 → 一组线程号」的抽象多值函数。布局代数在 `MultiFunction` 上提供了两个**验证型**运算，它们不改变布局，而是判断两个布局的线程映射是否相容：

- `collapse(func, dims)`：把 `func` 在 `dims` 上「塌缩」，使仅在这些维度上不同的输入映射到同一个像。直观说就是**忽略某些维度的区分度**。
- `cover(fa, fb)`：判断 `fa` 是否「覆盖」`fb`——对定义域里每个 `x`，都有 \(f_b(x) \subseteq f_a(x)\)。形象理解：`fa` 把元素分得**更细或同等**，`fb` 所需的线程映射都被 `fa` 满足。

这两个运算的真实舞台是**布局推理的验证规则**：当编译器推断出输入/输出布局后，要用 `cover` 校验「这个指令在这组布局下真的能正确执行」。

#### 4.5.2 核心流程

`cover(fa, fb)` 的判定：

```text
cover(fa, fb):
  1. 若 size 或 image_size 不等 -> False
  2. 用 gcd 把两者的 mode_shape 细化到共同的最细粒度；遇 gcd=1 -> False
  3. 把 fa、fb 都改写为该最细 mode_shape
  4. 从后向前逐个比对 mode：
       - 两个都非负：必须相等
       - fa 负、fb 负：gcd 能整除则折减，否则 False
       - fa 负、fb 非负：fa 的复制数必须整除 fb 的 mode 大小
       - fa 非负、fb 负：False（fa 不复制，无法覆盖 fb 的多值）
```

核心思想：`fa` 要覆盖 `fb`，`fa` 在每个 mode 上的「粒度」必须**不粗于** `fb`，且方向（非负/复制）要兼容。

`collapse` 则简单：从 `func.modes` 里删掉属于 `dims` 的 mode，使这些维度不再影响像。

#### 4.5.3 源码精读

`cover` 的判定（含 size 预检、gcd 细化、逐 mode 比对三阶段）：

[python/tilus/ir/layout/mfunction/ops.py:152-237](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/ops.py#L152-L237) —— cover：fa 非负而 fb 为复制时直接返回 False（第 227-228 行），体现了「不复制者无法覆盖复制者」。

`collapse` 与 `collapse_by_shape`：

[python/tilus/ir/layout/mfunction/ops.py:40-108](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/ops.py#L40-L108) —— collapse 删除指定维度的 mode；collapse_by_shape 按 shape 自动决定要塌缩哪些维度。

**真实用法**：`AssignRule` 用 `cover` 校验赋值指令——源张量 `x` 的线程映射必须覆盖目标 `y` 的线程映射，赋值才合法：

[python/tilus/ir/layout/inference/validation_rules/assign.py:27-30](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/validation_rules/assign.py#L27-L30) —— AssignRule 直接 `return fa.cover(fb)`，其中 fa/fb 是 x/y 的 spatial_mfunction。

元素级二元运算的验证规则更复杂：先用 `identity(...).collapse_by_shape(...)` 构造一个「广播后」的映射，再与操作数映射 compose，最后用 `cover` 比对输出映射：

[python/tilus/ir/layout/inference/validation_rules/elementwise_binary.py:36-40](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/validation_rules/elementwise_binary.py#L36-L40) —— `identity(y.shape).collapse_by_shape(x.shape) * x.layout.spatial_mfunction()` 构造 fa，再 `fa.cover(fb)` 判定。

#### 4.5.4 代码实践

**目标**：亲手调用 `cover`，验证「方向一致的映射相互覆盖，方向相反的不覆盖」。

```python
# 示例代码
from tilus.ir.layout.mfunction import multi_function
from tilus.ir.layout.mfunction.ops import identity, cover, collapse

fa = identity((4, 4))                              # (i,j) -> { (i,j) }，image 大小 16
fb_same = multi_function((4, 4), (4, 4), (0, 1))   # 与 fa 同向
fb_swap = multi_function((4, 4), (4, 4), (1, 0))   # 维度交换，方向相反

print("cover(同向):", cover(fa, fb_same))          # 预期 True
print("cover(反向):", cover(fa, fb_swap))          # 预期 False（mode 顺序不一致）

fc = collapse(fa, dims=[1])                        # 塌缩维度 1：忽略 j
print("collapse 后 modes:", fc.modes)              # 预期只剩 (0,)
print("collapse 后 image_size:", fc.image_size)    # 预期 4（从 16 降到 4）
```

**操作步骤**：保存运行。

**需要观察的现象**：
1. 同向的两个 identity 相互 `cover` 为 `True`。
2. 交换 mode 顺序后 `cover` 为 `False`——说明 `cover` 区分线程映射的方向。
3. `collapse` 删除维度 1 的 mode 后，`image_size` 从 16 降到 4，即塌缩后不再区分 j。

**预期结果**：注释中的「预期」值。`cover(反向)=False` 来自 cover 源码第 230-231 行：从后向前比对时，fa 的 mode 1 与 fb 的 mode 0 不等即返回 False。

#### 4.5.5 小练习与答案

**练习 1**：在 `AssignRule` 里，为什么是 `x.layout.spatial_mfunction().cover(y.layout.spatial_mfunction())`，即用源 x 覆盖目标 y，而不是反过来？

**答案**：赋值 `y = x` 要求「x 的数据分布能够提供 y 所需的每个线程上的值」。即对 y 中每个线程需要的元素集合，x 在对应线程上必须都持有（y 的像 ⊆ x 的像）。这正是 `cover(fa=x, fb=y)` 的定义。反过来则意味着 y 比 x 分得更细，x 无法提供 y 所需的全部元素，赋值非法。

**练习 2**：`cover` 的判定里，为什么「fa 非负、fb 为复制」时直接返回 False？

**答案**：fb 为复制 mode 表示它在某些维度上把同一个值映射给多个线程（像集合更大）。若 fa 在该位置是非负（单值）mode，它的像集合只有一个值，无法「包含」fb 的多个值，故不可能覆盖。源码 [mfunction/ops.py:227-228](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/ops.py#L227-L228) 直接 `return False`。

---

## 5. 综合实践

把本讲四个主题串起来：**造一个分块布局 → 验证它的线程/局部量 → 用 divide 拆回因子 → 用 cover 验证一次等价性**。

```python
# 示例代码：模拟「输出 tile 划分到 warp，每 warp 持 fragment」的布局构造与验证
from tilus.ir.layout.ops import spatial, local, compose, divide
from tilus.ir.layout.mfunction.ops import identity, cover

# 第 1 步：构造一个 [64, 64] 的分块布局
#   外层 spatial(8,8)：64 个线程位置（可理解为 64 个 warp-lane 组）
#   内层 local(8,8)：每个位置持 8x8 fragment
outer = spatial(8, 8)
inner = local(8, 8)
layout = compose(outer, inner)
assert layout.shape == (64, 64)
assert layout.spatial_size * layout.local_size == 64 * 64

# 第 2 步：用 divide 把内层剥掉，确认能还原 outer
assert divide(layout, inner) == outer
# 用 left_divide 剥外层，确认能得到 inner（运算符 / = divide，无左除运算符，用函数）
from tilus.ir.layout.ops import left_divide
assert left_divide(layout, outer) == inner

# 第 3 步：布局的线程映射与其自身的 identity 映射应相互覆盖
mf = layout.spatial_mfunction()
print("spatial_mfunction :", mf)
# 自覆盖恒为真：同一映射当然覆盖自己
print("cover(mf, mf)     :", cover(mf, mf))   # 预期 True

print("综合实践全部断言通过 ✔")
```

**说明**：
- 第 1 步用 `compose` 造布局，验证 `spatial_size × local_size == 总元素数`（线程与元素的守恒）。
- 第 2 步用 `divide` / `left_divide` 分别从右、从左拆因子，验证 compose 的双可逆性——这是分块布局能被后端「分解到逐 warp / 逐线程」的理论保证。
- 第 3 步把布局转成 `MultiFunction`，用 `cover` 做一次自覆盖验证，串起 4.5 的验证工具。

**预期结果**：所有 `assert` 通过，最后打印「综合实践全部断言通过 ✔」。第 1、2 步的断言可由本讲流程严格推出；第 3 步 `cover(mf, mf)` 因两个多值函数完全相同必为 True。若某些断言的具体数值在你的环境有出入，请以「待本地验证」记录实际输出并回头核对流程。

## 6. 本讲小结

- **三大原语**：`spatial`（全分布到线程）、`local`（全存于线程局部）、`replicated`（全复制）是构造任何 `RegisterLayout` 的积木；`RegisterLayout.__mul__`/`__truediv__` 即 `compose`/`divide`。
- **compose 是 mode 拼接**：逐维度把 outer 的 mode 放前、inner 的 mode 放后，shape 逐维相乘，mode 总数相加；spatial/local 各自重编号后顺序拼接，类别不变。
- **divide / left_divide 是左右逆运算**：分别剥除后缀内层与前缀外层，能除即存在子布局关系，不能除则抛 `LayoutOperationError`；`divide` 会先细化 mode_shape 让因子对齐。
- **形状变换各有讲究**：`reduce` 把归约维度的 spatial mode 变成复制（负数）而非删除，以广播归约结果；`reshape` 只能在 mode 边界重新切分；`permute` 同步重排 mode 与类别；`concat` 拼接维度。
- **cover/collapse 是验证工具**：`MultiFunction` 上的 `cover` 判断线程映射的覆盖关系，`collapse` 塌缩维度区分度；二者被布局推理的验证规则（如 `AssignRule`、元素级二元规则）用来校验指令在推断布局下可正确执行。

## 7. 下一步学习建议

本讲解完了布局代数的「运算」，但这些运算的**调用者**才是重点：

1. **下一讲 u4-l5《布局自动推理》**：讲 `infer_layout` 如何在「布局缺失」的张量上，通过前向/反向传播 + `cover` 验证，迭代求解出完整布局——你会看到本讲的 `compose`/`cover`/`collapse` 如何被推理规则驱动。
2. **U6《后端代码生成》**：发射器（emitter）会用 `divide`/`left_divide` 把整体布局分解到单线程，再用 `get_local`/`get_spatial` 生成每个线程的标量地址——届时回看 4.3 的 left_divide 真实用法会有更深的体会。
3. **建议阅读**：`python/tilus/ir/layout/ops/register_ops.py` 通读一遍（函数都不长），再对照 `tests/ir/layout/register_layout/` 下的测试逐个手算，是把本讲知识内化的最快路径。
