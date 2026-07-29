# SharedLayout、GlobalLayout 与 TMemoryLayout

## 1. 本讲目标

本讲承接 u4-l2（RegisterLayout 的 mode/spatial/local），把布局系统从「寄存器」推广到另外三种内存空间对应的布局。学完后你应该能够：

- 说出 SharedLayout 用 `shape / mode_shape / mode_strides / optional_swizzle` 四元组描述共享内存排布的原理，并能解释 swizzle 为什么能消除 bank conflict。
- 理解 GlobalLayout 为什么用「`axes`（变量）+ `offset`（表达式）」这种符号化方式建模，且 `shape` 可以是符号表达式。
- 了解 Blackwell 专用 TMemoryLayout 的 lane/column 结构及其约束（`shape[0]` 必须是 32/64/128）。
- 用 Tilus 提供的工厂函数亲手构造三种布局，并对比带/不带 swizzle 时同一索引的地址差异。

## 2. 前置知识

在进入本讲前，请先回忆 u4-l1 与 u4-l2 建立的几条共识：

- **四种张量 ↔ 四层内存**：`RegisterTensor`（寄存器）、`SharedTensor`（共享内存 SRAM）、`GlobalTensor`（显存 DRAM）、`TMemoryTensor`（Blackwell 张量内存 TMEM）。本讲覆盖后三种的「布局」。
- **什么是布局**：布局就是「逻辑索引 → 物理偏移」的映射函数 `layout(i, j, ...) = offset`。寄存器布局关心「哪个线程持有哪些元素」，而共享/全局/TMEM 布局关心「元素在各自内存里的字节地址」。
- **mode 与 mode_shape**（u4-l2）：把张量的一个维度细分为若干 mode（子维度），是 Tilus 布局系统的通用语言。SharedLayout 会复用这套术语。

两个本讲新引入的硬件背景知识：

- **共享内存的 bank 与 bank conflict**：GPU 共享内存被分成 32 个 bank，每个 bank 宽 4 字节，同一周期内同一 bank 只能响应一次访问。若一条指令里多个线程访问同一 bank 的不同地址，就会发生 **bank conflict**，访问被串行化、性能骤降。这正是 swizzle 要解决的核心问题。
- **符号表达式（Expr/Var）**：Tilus 底层复用 Hidet IR 的标量表达式。`Var` 是符号变量，`Expr` 是由变量、加减乘除、位运算组成的表达式树。布局里的「符号」就是这些东西。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [shared_layout.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py) | `SharedLayout` 类、`Swizzle` 类、`shared_layout` 工厂与规范化函数 |
| [global_layout.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py) | `GlobalLayout` 类及 `global_row_major`/`global_strides`/`global_compose`/`global_slice` 等工厂 |
| [tmem_layout.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/tmem_layout.py) | `TMemoryLayout` 类，约束 lane 数与列步长 |
| [ops/shared_ops.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/shared_ops.py) | `shared_row_major`、`shared_compose`、`shared_row_major_swizzle`（ldmatrix 友好的 swizzle 布局）等 |
| [ops/tmemory_ops.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/tmemory_ops.py) | `tmemory_row_major`：TMemoryLayout 的标准构造 |
| [hidet/ir/primitives/swizzle.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/swizzle.py) | swizzle 原语函数的 CUDA 实现与 Python 接口 |
| [utils/veceval.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/veceval.py) | `vectorized_evaluate`：用 numpy 在 CPU 上求值布局偏移（实践任务的关键工具） |
| [docs/.../shared-layout.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/shared-layout.rst) | 官方对 SharedLayout 的用户文档 |

## 4. 核心概念与源码讲解

### 4.1 SharedLayout 与 swizzle

#### 4.1.1 概念说明

`SharedLayout` 描述共享内存里一个张量的排布方式，回答的问题只有一个：**给定逻辑索引 `(i, j, ...)`，它在共享内存里的偏移是多少？**

它用四个字段唯一确定（见 [shared_layout.py:115-118](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L115-L118)）：

- `shape: tuple[int, ...]`：张量形状，**每一维必须是常数整数**（与 GlobalLayout 的符号 shape 形成对比）。
- `mode_shape: tuple[int, ...]`：把每一维拆成若干 mode 后的子形状（沿用 u4-l2 的 mode 概念）。
- `mode_strides: tuple[int, ...]`：每个 mode 的步长（偏移单位）。
- `optional_swizzle: Optional[Swizzle]`：作用在最终偏移上的位级「重排」函数，可为 `None`。

文档里明确点出 SharedLayout 与 GlobalLayout 的两点差异（[shared-layout.rst:8-11](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/shared-layout.rst#L8-L11)）：SharedLayout 要求常数 shape，而 GlobalLayout 支持符号 shape；SharedLayout 的偏移计算可使用「张量生命周期内的不变量（invariant）」，而 GlobalLayout 只允许「网格不变量（grid-invariant）」。

**swizzle 是什么**：swizzle 是一种用按位异或（XOR）打乱偏移低位的技术，目的是让「逻辑上相邻」的元素在物理上落到不同的 bank，从而消除 bank conflict。它源自 CUTLASS/CuTe 的同名机制。一个 `Swizzle` 由三个整数参数刻画（见 [shared_layout.py:30-43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L30-L43)）：

- `base`：要保留不变的最低有效位数（`MBase`）。
- `bits`：参与异或的位段宽度（`BBits`）。
- `shift`：异或位段的移动距离（`SShift`）。

#### 4.1.2 核心流程

给定索引，SharedLayout 计算偏移分两步（见 [shared_layout.py:135-148](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L135-L148)）：

1. **按 mode 展开求和**：
   - 用 `get_mode_groups(shape, mode_shape)` 把每个维度对应的 mode 分组。
   - 对每个索引调用 `index_deserialize` 把它拆成各 mode 上的子索引。
   - 做加权和：`total_index = Σ mode_index[k] * mode_strides[k]`。
2. **应用 swizzle**（若有）：`total_index = swizzle(total_index)`，即对偏移的低位做异或重排。

其偏移函数可写成：

\[
\text{offset}(i_0, \dots, i_{d-1}) = \text{swizzle}\!\left(\sum_{k} m_k \cdot s_k\right)
\]

其中 \(m_k\) 是第 \(k\) 个 mode 的子索引，\(s_k\) 是其步长。swizzle 本身的位运算语义为：

\[
\text{swizzle}(x) = x \oplus \big((x\ \&\ \text{y\_mask}) \gg \text{shift}\big),\quad \text{y\_mask} = ((2^{\text{bits}}-1) \ll (\text{base}+\text{shift}))
\]

即从 `base+shift` 位起取 `bits` 位，右移 `shift` 位后与原值异或。当 `bits == 0` 时 swizzle 退化为恒等（见 [swizzle.py:49-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/swizzle.py#L49-L51) 与 [swizzle.py:33](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/swizzle.py#L33)）。

一个布局所需的最小共享内存容量由 `count_size()` 给出：取每个 mode 的最大子索引求加权和再加 1（[shared_layout.py:259-271](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L259-L271)）：

\[
\text{size} = 1 + \sum_k (mode\_shape_k - 1)\cdot mode\_strides_k
\]

#### 4.1.3 源码精读

**Swizzle 类** 定义在 [shared_layout.py:29-84](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L29-L84)。`__call__` 把异或运算委托给 Hidet 的 `swizzle` 原语（[shared_layout.py:44-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L44-L51)），原语本身用一条三目表达式实现（[swizzle.py:29-33](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/swizzle.py#L29-L33)）。注意 `Swizzle` 是 `eq=True` 的 frozen dataclass（按值相等），而 `SharedLayout` 是 `eq=False` 的 IR 节点（按身份相等，自己另写了 `__eq__`/`__hash__`，见 [shared_layout.py:189-200](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L189-L200)）。

```python
# shared_layout.py:44-51  Swizzle 的调用即一次原语异或
def __call__(self, index: Expr) -> Expr:
    from tilus.hidet.ir.primitives.swizzle import swizzle
    if self.bits == 0:
        return index
    return swizzle(index, self.base, self.bits, self.shift)
```

> 「元素级」与「字节级」swizzle 的区别：直接 `layout(i,j) * nbytes` 会先在元素偏移上异或、再乘字节；而 `byte_offset` 先把偏移乘成字节再异或，并相应地把 swizzle 的 `base` 平移 `log2(nbytes)` 位（[shared_layout.py:53-72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L53-L72)）。两者数学等价，但字节级形式更利于代码生成——乘法被折进地址、省去尾部一次乘法。这正是实践任务要观察的差异之一。

**SharedLayout.create** 在 [shared_layout.py:208-245](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L208-L245) 做三道校验：shape 全正、`mode_shape` 与 `mode_strides` 等长、`prod(mode_shape) == prod(shape)`。

**构造工厂** 在 `ops/shared_ops.py`：

- `shared_row_major(*shape)`（[shared_ops.py:49-64](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/shared_ops.py#L49-L64)）：最内维步长 1 的标准行优先，`mode_shape = shape`。
- `shared_row_major_swizzle(shape, dtype_nbytes)`（[shared_ops.py:283-442](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/shared_ops.py#L283-L442)）：专门为 `ldmatrix` 服务的 swizzle 布局。它把每行划分成 16 字节的「bank group」，根据 `n_vector_size * dtype_nbytes`（128/64/32/16）选择不同 `bits`（3/2/1/0）的 swizzle（[shared_ops.py:385-435](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/shared_ops.py#L385-L435)）。其文档注释里那张 `0 1 2 3 ... / 1 0 3 2 ...` 的表就是 swizzle 后的 bank group 分布，保证了「同行不同 bank、同列不同 bank」。

**规范化**：`canonicalize_shared_layout`（[shared_layout.py:324-408](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L324-L408)）会合并同维且步长相容的连续 mode、去掉 size-1 的空 mode、并把 `bits==0` 的 swizzle 归一为 `None`，使「功能等价」的布局有相同表示——这是缓存键稳定与相等判定的前提。`shared_layout` 工厂（[shared_layout.py:411-444](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/shared_layout.py#L411-L444)）总是返回规范化后的结果。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个带 swizzle 的 SharedLayout，对比「带 swizzle」与「不带 swizzle」时同一索引的偏移差异，直观看到 swizzle 如何打乱 bank 分布。

**操作步骤**：把下面这段「示例代码」存成 `swizzle_demo.py` 并运行（纯 CPU，无需 GPU）。

```python
# 示例代码：对比带/不带 swizzle 的共享内存偏移
import numpy as np
from tilus.ir.layout.ops.shared_ops import (
    shared_row_major,
    shared_row_major_swizzle,
    visualize_layout,
)

# 8 行 × 64 列、元素为 fp16（2 字节）的共享内存 tile
shape = (8, 64)
nbytes = 2

plain = shared_row_major(*shape)
swizzled = shared_row_major_swizzle(shape, dtype_nbytes=nbytes)

print("=== plain（无 swizzle）===")
print("mode_shape :", plain.mode_shape)
print("mode_strides:", plain.mode_strides)
print("swizzle    :", plain.optional_swizzle)
print(visualize_layout(plain))

print("\n=== swizzled ===")
print("mode_shape :", swizzled.mode_shape)
print("mode_strides:", swizzled.mode_strides)
print("swizzle    :", swizzled.optional_swizzle)
print(visualize_layout(swizzled))

# 逐元素对比同一索引 (i, j) 的偏移
plain_grid = plain.as_numpy_grid()
swizz_grid = swizzled.as_numpy_grid()
diff_count = int(np.sum(plain_grid != swizz_grid))
print(f"\n被 swizzle 改变偏移的元素个数: {diff_count} / {plain.size}")
```

**需要观察的现象**：
- `plain` 的 `optional_swizzle` 为 `None`，`visualize_layout` 输出一个整齐的行优先表格（每行 `0,1,2,...,63`，下一行整体加 64）。
- `swizzled` 的 `optional_swizzle` 形如 `Swizzle(base=3, bits=3, shift=3)`（因为 `n_vector_size=64`、`64*2=128`，命中 `bits=3` 分支），`visualize_layout` 的表格呈现「对角线交错」的 bank group 排布（对应 [shared_ops.py:385-397](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/shared_ops.py#L385-L397) 注释里的那张表）。
- `as_numpy_grid()` 能正常求值 swizzle，是因为 `vectorized_evaluate` 在 `visit_Call` 里特判了名为 `swizzle` 的函数调用（[veceval.py:83-90](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/veceval.py#L83-L90)）。
- 进阶：把 `shape` 改成 `(8, 32)`（`n_vector_size*2=64` → `bits=2`）与 `(8, 16)`（`32` → `bits=1`），观察 swizzle 参数与「打乱比例」的变化。

**预期结果**：swizzled 表格中每行内偏移不再单调递增，而是被 XOR 打乱；`diff_count` 会显示相当一部分元素的偏移被改变——这正是「逻辑列相同 bank」被打散的体现，从而避免 `ldmatrix` 时的 bank conflict。

> 说明：本实践的精确数值输出（如 `diff_count` 具体等于多少）依赖上述工厂函数的当前实现，请以本地实际运行为准。

#### 4.1.5 小练习与答案

**练习 1**：`Swizzle(base=3, bits=3, shift=3)` 作用在偏移 `x` 上，写出结果表达式。

**参考答案**：`x ^ ((x & (((1<<3)-1) << (3+3))) >> 3)`，即取 `x` 的第 6–8 位、右移 3 位后与原值异或，等价于把第 6–8 位异或进第 3–5 位（参见 [swizzle.py:33](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/swizzle.py#L33)）。

**练习 2**：为什么 `shared_row_major_swizzle` 当 `n_vector_size * dtype_nbytes == 16` 时返回 `swizzle = None`？

**参考答案**：此时每行只有一个 16 字节 bank group（见 [shared_ops.py:422-433](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/shared_ops.py#L422-L433) 的表，只有一列），无可打乱的列间 bank，swizzle 无意义，故置 `None`。

### 4.2 GlobalLayout 与符号化 offset

#### 4.2.1 概念说明

`GlobalLayout` 描述显存（DRAM）中一个全局张量的排布。它的设计哲学与 SharedLayout 截然不同：**用「变量 + 表达式」直接描述任意映射函数**，而不是固定的 `mode_shape/mode_strides` 四元组。

它有四个字段（见 [global_layout.py:48-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L48-L51)）：

- `shape: tuple[Expr, ...]`：张量形状，**每一维可以是表达式（grid-invariant）或常数**。这是它能支持「符号 shape」的关键。
- `size: Expr`：存储所需元素数。紧凑布局下 `size == prod(shape)`；带 padding 时更大、共享存储时更小。
- `axes: tuple[Var, ...]`：每一维对应的索引变量，长度与 shape 一致。
- `offset: Expr`：由 `axes`（及 grid-invariant 变量）组成的偏移表达式。

文档（[global-layout.rst:4-14](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/global-layout.rst#L4-L14)）说明：用 `global_view`/`global_tensor` 时，可给 `strides`、`layout` 或都不给（默认行优先紧凑布局），最终都会归约成一个 `GlobalLayout`。

#### 4.2.2 核心流程

GlobalLayout 的核心是「用闭包捕获映射函数」：

1. `GlobalLayout.create(shape, size, f_offset)` 用 `index_vars` 为每一维新建一个 `Var` 作为 `axes`，再调用用户传入的 `f_offset(axes)` 把这个表达式「冻结」进 `offset` 字段（[global_layout.py:98-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L98-L100)）。
2. 求值时 `__call__(*indices)` 用 `rewrite` 把 `axes` 替换成具体索引，得到该点的偏移（[global_layout.py:53-71](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L53-L71)）：

\[
\text{offset}(\text{indices}) = \text{rewrite}(\text{offset}, \{axis_k \mapsto index_k\})
\]

这种「符号表达式 + 变量代换」的建模，天然支持符号 shape：例如一个 `[M, N]` 输出 tile 的列步长可以是符号 `N`，而不是常数。

#### 4.2.3 源码精读

**GlobalLayout.create**（[global_layout.py:73-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L73-L100)）：

```python
# global_layout.py:98-100  用 index_vars 建轴，再调用 f_offset 冻结偏移表达式
expr_shape = tuple(as_expr(s) for s in shape)
axes: list[Var] = index_vars(num_vars=len(shape))
return GlobalLayout(shape=expr_shape, size=size, axes=tuple(axes), offset=f_offset(axes))
```

**通用构造器**：

- `global_row_major(*shape)` / `global_column_major(*shape)`（[global_layout.py:131-162](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L131-L162)）：都走 `_generic_repeat`（[global_layout.py:103-111](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L103-L111)），按维度「秩（rank）」反推步长 `Σ axes[i]*strides[i]`。
- `global_strides(shape, strides)`（[global_layout.py:191-218](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L191-L218)）：直接给定每维步长，`offset = Σ axes[i]*strides[i]`，shape 与 strides 都可以是表达式。
- `global_compose(lhs, rhs, ...)`（[global_layout.py:114-188](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L114-L188)）：把外层分块布局与内层线程布局组合，`offset = lhs_offset * rhs.size + rhs_offset`，`size = lhs.size * rhs.size`，是论文 §4.2 的布局复合算子。
- `global_slice(layout, offsets, dims, shape)`（[global_layout.py:221-258](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L221-L258)）：在原布局上取子块，`f_offset` 把被切片维度的轴替换成 `axis + offsets[dim]`，并减去基点偏移使子块从 0 起算。

> 注意：`offset` 表达式只能使用 `axes` 与 **grid-invariant** 变量（即整个网格 launch 期间不变的值，如运行时尺寸 `N`）。这与 SharedLayout 允许「张量生命周期内的 invariant」是不同的不变性等级（[shared-layout.rst:8-11](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/shared-layout.rst#L8-L11)）。

#### 4.2.4 代码实践

**实践目标**：构造一个带符号 shape 的 GlobalLayout，观察 `offset` 是如何用符号变量表达的，并验证 `__call__` 的代换求值。

**操作步骤**（「示例代码」，纯 CPU 可运行）：

```python
# 示例代码：符号化 GlobalLayout
from tilus.hidet.ir.expr import Var
from tilus.ir.layout.global_layout import global_strides, global_row_major

N = Var("N", dtype=None)  # 一个符号变量，代表运行时列数
# 形状 (8, N)，列步长 = N，行步长 = 1（列优先访问）
layout = global_strides(shape=(8, N), strides=(1, N))

print("shape :", layout.shape)        # (8, N)
print("axes  :", layout.axes)          # 两个符号轴变量
print("offset:", layout.offset)        # 形如 ax0 + N * ax1 的表达式
print("size  :", layout.size)

# 用具体索引 (i=2, j=3) 代换求值
print("at (2,3):", layout(2, 3))       # 2 + N*3
```

**需要观察的现象**：
- `layout.offset` 不是某个数字，而是一棵含符号 `N` 与两个轴变量的表达式树。
- `shape` 里出现了符号 `N`——这正是 SharedLayout 做不到的「符号 shape」。
- `layout(2, 3)` 的输出仍是含 `N` 的表达式 `2 + N*3`（因为 `N` 本身未赋值），印证了「`__call__` 只是变量代换」。

**预期结果**：能清楚看到 GlobalLayout 把「地址计算」完全留给表达式系统，符号维度被原样保留，等到代码生成时才与真实运行时尺寸结合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `global_view` 创建的全局张量其 `size` 通常等于 `prod(shape)`，但 `global_compose` 后 `size = lhs.size * rhs.size`？

**参考答案**：`global_view` 是紧凑布局，无 padding，故 `size == prod(shape)`；而 `global_compose` 把外层每个元素展开成内层一整块（`offset = lhs_offset * rhs.size + rhs_offset`），所以总存储量是两层 size 的乘积（[global_layout.py:123-127](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L123-L127)）。

**练习 2**：`global_slice` 的 `f_offset` 末尾为什么要 ` - layout(*offsets)`？

**参考答案**：为了让切片后的子布局偏移从 0 起算（相对偏移），而不是继承原布局的全局绝对地址，便于后续按独立张量处理（[global_layout.py:252-256](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L252-L256)）。

### 4.3 TMemoryLayout 与 lane 结构

#### 4.3.1 概念说明

`TMemoryLayout` 是 Blackwell（SM 10.0+）张量内存（TMEM）专用布局。TMEM 是一块紧挨着张量核的片上存储，物理上组织成「lane（行）× column（列）」的二维结构，每个格子 32 bit，lane 数（行数）必须是 32、64 或 128（见 [tensor.py:659-676](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L659-L676) 的 `TMemoryTensor` 文档）。

它只有三个字段（[tmem_layout.py:25-27](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/tmem_layout.py#L25-L27)）：

- `shape: tuple[int, ...]`：约定 `shape[0]` 是 lane（行）维，其余维都是「列步长」维。
- `column_strides: tuple[int, ...]`：每个维度的列步长。
- `lane_offset: int`：lane 起始偏移（用于切片）。

与寄存器/共享/全局布局最大的不同：TMEM 布局**不映射到字节地址**，而是映射到「lane 号 + 列号」这一硬件原生坐标——因为 TMEM 的访问单位就是 lane/column，由 `tcgen05` 指令组直接消费。

#### 4.3.2 核心流程

TMEM 布局的偏移可理解为：

\[
\text{坐标}(i_0, i_1, \dots) = \big(\text{lane} = i_0 + \text{lane\_offset},\quad \text{column} = \sum_{k\ge 1} i_k \cdot \text{column\_strides}_k\big)
\]

即第 0 维直接决定 lane 行号，其余维度按列步长线性组合成列号。标准构造 `tmemory_row_major`（[tmemory_ops.py:20-28](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/tmemory_ops.py#L20-L28)）就是把列步长设成行优先（最内维步长 1），lane 维步长恒为 0：

```python
# tmemory_ops.py:20-28  标准 TMEM 行优先布局
def tmemory_row_major(shape: Sequence[int]) -> TMemoryLayout:
    column_strides = [0] * len(shape)   # lane 维(column_strides[0])必须为 0
    stride = 1
    for dim in reversed(range(1, len(shape))):
        column_strides[dim] = stride
        stride *= shape[dim]
    return TMemoryLayout.create(shape, column_strides, lane_offset=0)
```

#### 4.3.3 源码精读

**TMemoryLayout.create**（[tmem_layout.py:29-48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/tmem_layout.py#L29-L48)）做了四道校验，把硬件约束写死在类型里：

1. `len(shape) == len(column_strides)`：维度匹配。
2. `len(shape) >= 2`：TMEM 至少二维。
3. `shape[0] in [32, 64, 128]`：lane 数只能是这三个合法值（对应一个 warp 的 32 线程到 4 个 warp 的 128 线程）。
4. `column_strides[0] == 0`：lane 维没有列步长（lane 与 column 是正交的两套坐标）。

`tmemory_slice`（[tmemory_ops.py:31-36](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/tmemory_ops.py#L31-L36)）则在切片时累加 `lane_offset` 并按 `slice_dims` 取出对应列步长，是 `tcgen05.slice` 指令布局推理的基础。

在自动布局推理（u4-l5）中，`tcgen05.alloc` 指令会通过 `Tcgen05AllocRule` 给未绑定布局的 TMEM 张量默认套上 `tmemory_row_major`（见 [inference_rules/tcgen05/alloc.py:29-33](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/tcgen05/alloc.py#L29-L33)）——这与 u4-l1 讲过的「TMEM 必须显式 `tcgen05.alloc` 分配、可延迟绑定布局」呼应。

#### 4.3.4 代码实践

**实践目标**：亲手构造一个 TMemoryLayout，验证 lane 维与列维的步长约束，并理解一个 `[128, M, N]` 的 TMEM 张量如何映射到 lane/column 坐标。

**操作步骤**（「示例代码」，纯 CPU）：

```python
# 示例代码：构造并检查 TMemoryLayout
from tilus.ir.layout.ops.tmemory_ops import tmemory_row_major
from tilus.ir.layout.tmem_layout import TMemoryLayout

# 128 lane × 64 × 16 的 TMEM 张量
layout = tmemory_row_major([128, 64, 16])
print("shape          :", layout.shape)
print("column_strides :", layout.column_strides)   # [0, 16, 1]
print("lane_offset    :", layout.lane_offset)       # 0

# 故意构造一个非法布局，观察校验报错
try:
    TMemoryLayout.create(shape=[64, 16], column_strides=[1, 1], lane_offset=0)
except ValueError as e:
    print("校验报错:", e)   # lane 维的 column_strides[0] 必须为 0
```

**需要观察的现象**：
- `column_strides` 为 `[0, 16, 1]`：lane 维（第 0 维）步长恒为 0，第 1 维步长 = 最内维大小 16，最内维步长 = 1（标准行优先）。
- 把 `column_strides[0]` 设成非 0 会触发 `create` 的第 4 条校验报错。
- 把 `shape[0]` 设成 100 会触发「must be 32, 64, or 128」报错。

**预期结果**：读者能据此推断，一个元素 `(l, m, n)` 落在 `lane = l`、`column = m*16 + n` 的 TMEM 格子上，这正是 `tcgen05` 指令组读写 TMEM 时使用的原生坐标。

> 说明：TMemoryLayout 是 Blackwell 专属，本实践只验证布局对象的构造与约束，不涉及真实 TMEM 硬件访问（那需要 sm_100+ GPU）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TMemoryLayout.create` 强制 `shape[0] in [32, 64, 128]`？

**参考答案**：TMEM 物理上行数由参与张量核运算的 warp 数决定——1 个 warp 32 线程对应 32 lane，最多 4 个 warp 对应 128 lane，硬件只支持这三档（[tensor.py:659-664](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L659-L664)）。

**练习 2**：TMemoryLayout 与 SharedLayout/GlobalLayout 在「映射目标」上最本质的区别是什么？

**参考答案**：后两者把索引映射成「字节偏移」，而 TMemoryLayout 把索引映射成「lane 号 + 列号」这一 TMEM 原生二维坐标，且 lane 维与列维正交（`column_strides[0] == 0`），不涉及字节地址。

## 5. 综合实践

把三种布局放在一起对比，建立完整心智模型。请用一张表填空（参考答案见下方，先自己填写再核对）：

| 布局 | 内存空间 | shape 是否可符号 | 偏移映射目标 | 关键额外字段 | 标准构造工厂 |
| --- | --- | --- | --- | --- | --- |
| SharedLayout | 共享内存 SRAM | 否（常数） | 元素/字节偏移 | `optional_swizzle` | `shared_row_major_swizzle` |
| GlobalLayout | 显存 DRAM | ? | ? | `axes`/`offset` | `global_strides` |
| TMemoryLayout | TMEM（Blackwell） | 否（常数） | ? | `column_strides`/`lane_offset` | `tmemory_row_major` |

并完成一个小任务：编写一段脚本，分别用 `shared_row_major(8,8)`、`global_row_major(8,8)`、`tmemory_row_major([128,8,8])` 构造三种布局，打印它们各自的核心字段；再总结一句话：**当你要为一个新指令设计布局推理规则（u4-l5）时，三种布局各自最需要小心处理的约束是什么？**

> 参考答案要点：GlobalLayout 的 shape 可为符号（是）、映射到字节偏移；TMemoryLayout 映射到 lane/column 坐标而非字节。设计推理规则时——SharedLayout 要兼顾 swizzle 与 bank、`count_size` 决定分配量；GlobalLayout 要确保 `offset` 只用 grid-invariant 变量并正确维护 `size`；TMemoryLayout 必须满足 `shape[0] ∈ {32,64,128}` 且 `column_strides[0]==0`。

## 6. 本讲小结

- **SharedLayout** 用 `shape + mode_shape + mode_strides + optional_swizzle` 四元组描述共享内存排布，偏移 = 「mode 加权和」再经 swizzle 的位异或；swizzle 的存在是为了把逻辑相邻元素打散到不同 bank，消除 bank conflict，尤其服务于 `ldmatrix`。
- **字节级 swizzle**（`byte_offset`/`to_byte_swizzle`）把乘法折进地址、base 平移 `log2(nbytes)` 位，比「先 swizzle 再乘字节」更利于代码生成。
- **GlobalLayout** 用 `axes`（变量）+ `offset`（表达式）的符号化方式建模，`shape` 可含符号表达式，`__call__` 本质是变量代换；只允许使用 grid-invariant 变量。
- **TMemoryLayout** 是 Blackwell 专属，把索引映射到「lane + column」原生坐标而非字节地址；强制 `shape[0] ∈ {32,64,128}` 且 `column_strides[0] == 0`，标准形态由 `tmemory_row_major` 给出。
- 三者共同点：都是「逻辑索引 → 物理位置」的纯函数、都是 frozen 不可变 IR 节点、都参与 u4-l5 的自动布局推理；差异在于映射目标（字节 vs lane/column）、shape 是否可符号、以及是否需要 swizzle。

## 7. 下一步学习建议

- 下一篇 **u4-l4 布局操作：compose、divide 与 reduce** 会讲布局代数：如何用 `compose` 把本讲的外层分块布局与 u4-l2 的内层线程布局组合、如何用 `divide`/`reshape` 变换布局。建议先熟练本讲的三种「原子」布局。
- 若想看 swizzle 在真实内核里的作用，可先跳读 **u7-l1（Ampere matmul：ldmatrix + MMA）**，再回到 u4-l4。
- 想理解这些布局如何被自动填进张量的 `optional_layout`，请直接进入 **u4-l5 布局自动推理**，那里会用本讲的 `Tcgen05AllocRule` 等规则把推理闭环串起来。
