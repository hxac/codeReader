# RegisterLayout：mode、spatial 与 local

## 1. 本讲目标

本讲是「布局系统」单元的第二篇，承接 [u4-l1 张量与内存空间全景](u4-l1-tensors-and-memory-spaces.md)。上一讲我们建立了静态地图：四种张量一一对应四层 GPU 内存，其中只有 `RegisterTensor` 能做算术，且它的布局可以被「延迟绑定」。本讲要回答的核心问题是：

> **一个寄存器张量里的众多元素，到底是怎么分配给线程块里的成百上千个线程的？**

具体地，读完本讲你应该能够：

1. 说出 `mode` 与 `mode_shape` 的含义，能用「维度细分」的视角把一个 `shape` 拆成一组 mode。
2. 区分 `spatial_modes`（跨线程分布）、`local_modes`（线程局部存储）与负数 `replicated` mode（多线程持有同一元素），并理解 `local_size`、`spatial_size` 这两个关键不变量。
3. 看懂 `MultiFunction` 如何形式化「逻辑索引 → (线程, 局部槽位)」的映射，并能读通 `get_local` / `get_spatial` / `get_global` 三个核心方法。

本讲只讲 RegisterLayout 的**静态结构与映射机制**；布局之间的组合（compose）、切分（divide）、规约（reduce）等**代数运算**留给 [u4-l4 布局操作：compose、divide 与 reduce](u4-l4-layout-operations-and-compose.md)。

## 2. 前置知识

在学习本讲前，请确保你已经理解以下概念（来自 u4-l1 与更早的讲义）：

- **线程块、warp、线程**：一个 GPU 线程块由若干 warp 组成，每个 warp 有 32 个线程。寄存器是**线程私有**的存储，但 Tilus 把整个线程块的寄存器看成一个「分布式张量」。
- **RegisterTensor 是分布式的**：与全局内存、共享内存不同，寄存器张量里的元素**散落在不同线程的私有寄存器里**。因此它的布局必须同时回答「在哪个线程」和「在该线程的第几个寄存器槽位」。
- **行优先线性化（row-major serialize）**：给定多维索引 \((i_0, i_1, \dots, i_{n-1})\) 与形状 \((d_0, d_1, \dots, d_{n-1})\)，行优先把它折叠成一个标量：

  \[
  \text{linear} = ((\cdots((i_0 \cdot d_1) + i_1)\cdot d_2) + i_2)\cdots)\cdot d_{n-1} + i_{n-1}
  \]

  Tilus 里这个操作由 `index_serialize` 完成，逆操作（标量展开成多维索引）由 `index_deserialize` 完成，二者定义在 `tilus.hidet.ir.utils.index_transform`。
- **身份相等（identity equality）**：所有 IR 节点（含 `RegisterLayout`）都用 `__eq__` 做结构比较、`__hash__` 用 `id`，参见 [register_layout.py:71-82](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L71-L82)。布局比较按四个字段逐一比对。

> **直觉一句话**：RegisterLayout 是一张「元素 → 线程 + 槽位」的分配表。本讲就是教你读懂这张表的语法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/ir/layout/register_layout.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py) | `RegisterLayout` 数据类、`register_layout` 工厂、`validate_layout` 校验、`visualize_layout` 可视化、规范化逻辑。本讲的主角。 |
| [python/tilus/ir/layout/mfunction/mfunction.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py) | `MultiFunction`——把「逻辑索引 → 一组线程号」抽象成一个可复合的多值函数，是 `spatial_mfunction` 的底层数学。 |
| [python/tilus/ir/layout/ops/register_ops.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py) | `spatial` / `local` / `column_spatial` / `column_local` / `auto_local_spatial` 等构造便捷函数，以及 `compose` / `reduce` 等运算。 |
| [python/tilus/ir/layout/ops/utils.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/utils.py) | `get_mode_groups`——把 `mode_shape` 按 `shape` 的维度分组的工具，是理解 `grouped_modes` 的钥匙。 |
| [docs/source/programming-guides/layout-system/register-layout.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst) | 官方教程，给出形式化定义与若干可视化样例（含 MMA 张量核布局）。 |

## 4. 核心概念与源码讲解

### 4.1 mode 与 mode_shape

#### 4.1.1 概念说明

一个张量有 `shape`，例如 \([64, 64]\)。但在描述寄存器分布时，我们常常需要把**一个维度进一步细分**成若干「子维度」，因为张量核（MMA）等硬件对元素的编排粒度往往不是整条维度，而是更细的片段。Tilus 借用 Graphene/CuTe 的术语，把每一个细分后的子维度称为一个 **mode**，所有 mode 拼起来就得到 `mode_shape`。

举两个来自官方文档的例子：

- `shape = [3, 4]`：若把第二个维度拆成 \(2 \times 2\)，第一个维度保持，则 `mode_shape = [3, 2, 2]`。
- `shape = [12, 1, 6]`：把第一维拆成 \(3 \times 4\)，第二维是 1，第三维拆成 \(2 \times 3\)，得到 `mode_shape = [3, 4, 1, 2, 3]`；由于大小为 1 的 mode 是冗余的（可以任意插入），Tilus 会把它们剪掉，最终 `mode_shape = [3, 4, 2, 3]`。

关键约束：`mode_shape` 中各 mode 的乘积必须等于 `shape` 各维度的乘积，并且 mode 要能**按顺序**归组到 `shape` 的各个维度上（由 `grouped_modes` 表达）。

#### 4.1.2 核心流程：从 shape 到 grouped_modes

给定 `shape` 和 `mode_shape`，Tilus 用贪心算法把 mode 逐个分配给 shape 的维度，得到 `grouped_modes`（一个「维度 → 它包含哪些 mode」的列表）：

```
输入 shape=[64, 64], mode_shape=[4, 2, 8, 8, 4, 2]
对 shape 的每个维度 d：
    remaining = d
    只要 remaining > 1：
        取下一个 mode 大小 m
        要求 remaining % m == 0   （否则报错）
        remaining //= m
        把该 mode 归到当前维度
输出 grouped_modes = [[0,1,2], [3,4,5]]
        （M=64 由 mode 0,1,2 组成：4*2*8=64；N=64 由 mode 3,4,5 组成：8*4*2=64）
```

这个不变量很重要：\(\text{len}(\text{grouped\_modes}) = \text{len}(\text{shape})\)，且 \(\sum |\text{group}| = \text{len}(\text{mode\_shape})\)。

#### 4.1.3 源码精读

`RegisterLayout` 是一个 frozen dataclass，由四个字段唯一确定（注意是**四个字段共同**决定一个布局，而不是只有 shape）：

[python/tilus/ir/layout/register_layout.py:34-53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L34-L53) 定义了数据类与四个字段 `shape / mode_shape / spatial_modes / local_modes`：

```python
@dataclass(frozen=True, eq=False)
class RegisterLayout(IRNode):
    shape: tuple[int, ...]
    mode_shape: tuple[int, ...]
    spatial_modes: tuple[int, ...]
    local_modes: tuple[int, ...]
```

`grouped_modes` 是按上述贪心算法算出来的缓存属性，委托给 `get_mode_groups`：

[python/tilus/ir/layout/register_layout.py:88-92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L88-L92) 与 [python/tilus/ir/layout/ops/utils.py:21-L40](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/utils.py#L21-L40)（`get_mode_groups`，含 `[64,32] / [8,8,16,2] → [[0,1],[2,3]]` 的文档示例）。

合法性校验由 `validate_layout` 完成，它检查两件事：(1) `mode_shape` 能整除并恰好覆盖 `shape`；(2) `spatial_modes` 与 `local_modes` 里的编号都在 \([0, \text{len}(\text{mode\_shape}))\) 范围内、且互不重复。见 [register_layout.py:223-264](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L223-L264)（mode_shape 与 shape 的一致性检查，从后往前消费 mode）。

> ⚠️ 注意：创建布局不要直接 `RegisterLayout(...)`，而要用工厂函数 `register_layout(...)`，因为它会先 `validate_layout` 再 `canonicalize_layout`（见 [register_layout.py:428-468](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L428-L468)）。规范化会合并 size-1 的 mode 与连续同类的 mode，使「映射相同」的布局落到同一个规范形式——这是布局相等判定与缓存的根基。

#### 4.1.4 代码实践

**实践目标**：亲手用 `register_layout` 构造一个布局，观察 `mode_shape` 与 `grouped_modes` 的关系。

**操作步骤**（示例代码，可在任意装好 tilus 的环境运行，无需 GPU）：

```python
# 示例代码
from tilus.ir.layout import register_layout

layout = register_layout(
    shape=[64, 64],
    mode_shape=[4, 2, 8, 8, 4, 2],
    spatial_modes=[0, 2, 4],
    local_modes=[1, 3, 5],
)
print("shape        =", layout.shape)
print("mode_shape   =", layout.mode_shape)
print("grouped_modes=", layout.grouped_modes)
```

**需要观察的现象与预期结果**：

- `grouped_modes` 应为 `[[0, 1, 2], [3, 4, 5]]`，即第 0 个维度（M=64）由 mode 0/1/2 组成（\(4\times2\times8=64\)），第 1 个维度（N=64）由 mode 3/4/5 组成（\(8\times4\times2=64\)）。
- 因为所有 mode 大小都大于 1、且 spatial/local 在每个组内交替不相邻，规范化不会合并任何 mode，`mode_shape` 保持 `[4, 2, 8, 8, 4, 2]`。

> 由于规范化逻辑可能随版本微调，若你的输出与上文不完全一致，以 `grouped_modes` 各组乘积等于对应 shape 维度为准。精确输出请**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：给定 `shape=[12, 1, 6]`，作者说它的 `mode_shape` 会被规范化成 `[3, 4, 2, 3]`。请说明 mode 与 shape 维度的对应关系。

**参考答案**：第 0 维 12 拆成 \(3\times4\)（mode 0,1），第 1 维 1 被剪掉（size-1 的 mode 在规范化时删除），第 2 维 6 拆成 \(2\times3\)（mode 2,3）。所以 `grouped_modes = [[0,1], [2,3]]`（注意规范化后 shape 里的大小为 1 的维度也会被相应处理）。

**练习 2**：为什么 `validate_layout` 要求 `mode_shape` 必须能「整除」shape？如果 `shape=[5]` 而 `mode_shape=[2,3]`（乘积都是 6 但不整除）会怎样？

**参考答案**：因为 mode 是对**单一维度**的细分，必须能逐级整除该维度；\(2\times3=6\neq5\) 且 5 不能被 2 整除，`get_mode_groups` 会在 `remaining % mode_shape[i] != 0` 处抛出 `LayoutOperationError`。

---

### 4.2 spatial、local 与 replicated

#### 4.2.1 概念说明

有了 mode 之后，下一步是**把每个 mode 归类**。Tilus 把 mode 分成两类（外加一种特殊的复制模式），这就回答了「元素去哪儿」：

- **spatial modes（空间模式）**：这些 mode 的取值用来**区分不同的并行工作者（线程）**。把它们串成一个多维「空间坐标」，再行优先线性化，就得到 `thread_id`。
- **local modes（局部模式）**：这些 mode 的取值用来**区分同一线程内部的不同寄存器槽位**。线性化后得到 `local_id`。
- **replicated modes（复制模式，用负数表示）**：当一个 mode 被标记为负数 \(-k\)，表示「这个元素被复制到 \(k\) 个线程」。它不对应 `mode_shape` 里的任何真实 mode，而是一个纯复制因子。

官方文档给出的形式化定义非常清晰——寄存器布局是一个映射（见 [register-layout.rst:12-22](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L12-L22)）：

\[
\text{layout} : (\text{thread\_id},\ \text{local\_id}) \mapsto \text{index}
\]

其中 `thread_id` 是线程块内的线程号，`local_id` 是该线程局部存储里的槽位号，`index` 是元素在 \(d\) 维张量里的逻辑坐标。等价地，也可以反向看成「逻辑索引 → 一组 (thread_id, local_id) 对」（之所以是「一组」，是因为复制模式会让一个元素落在多个线程）。

#### 4.2.2 核心流程：从 mode 到 (thread_id, local_id)

给定一个逻辑索引，求它落在哪个线程、哪个槽位，分四步（对应官方文档 [register-layout.rst:269-299](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L269-L299) 的例子 `shape=[4,6], mode_shape=[2,2,3,2], spatial_modes=[0,2], local_modes=[3,1]`）：

1. **展开成 mode 索引**：把每个维度的坐标按该维的 mode 组拆开。
   对索引 \((i, j)\)：`mode_index = [i//2, i%2, j//2, j%2]`。
2. **挑出 spatial 部分**：按 `spatial_modes` 顺序取对应 mode 索引，得到 `spatial_index = [mode_index[0], mode_index[2]] = [i//2, j//2]`，`spatial_shape = [2, 3]`。
3. **挑出 local 部分**：按 `local_modes` 顺序取，得到 `local_index = [mode_index[3], mode_index[1]] = [j%2, i%2]`，`local_shape = [2, 2]`。
4. **线性化**：
   - `thread_id = serialize(spatial_index, spatial_shape) = (i//2)*3 + (j//2)`
   - `local_id   = serialize(local_index, local_shape)   = (j%2)*2 + (i%2)`

由此得到三个核心不变量（都是相应 shape 的乘积）：

\[
\text{spatial\_size} = \prod \text{spatial\_shape} \quad(\text{即线程数}),\qquad
\text{local\_size} = \prod \text{local\_shape} \quad(\text{即每线程元素数}),\qquad
\text{size} = \prod \text{shape} = \text{spatial\_size} \times \text{local\_size}
\]

（最后一个等式在没有复制模式时成立；有复制模式时 `size` 会小于 `spatial_size * local_size`，因为部分线程持有重复数据。）

**复制模式**的生成主要来自 `reduce` 操作。例如对 `spatial(3, 4)`（12 个线程各持 1 元素）沿第 0 维做 `reduce(dims=[0])`，会把那一维的 3 个线程「塌缩」成一个复制因子 `-3`，得到 `spatial_modes=[-3, 0]`，于是原本分散在 3 个线程里的同一个结果元素，现在被这 3 个线程共同持有（见 [register-layout.rst:192-200](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L192-L200) 与 [register-layout.rst:302-311](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L302-L311)）。

#### 4.2.3 源码精读

三个派生属性都在 `register_layout.py` 里，实现就是上面流程的直接翻译：

[python/tilus/ir/layout/register_layout.py:94-112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L94-L112)——`spatial_shape`、`local_shape`、`local_size`、`spatial_size`、`size`：

```python
@cached_property
def spatial_shape(self) -> list[int]:
    return [self.mode_shape[i] if i >= 0 else -i for i in self.spatial_modes]

@cached_property
def local_shape(self) -> list[int]:
    return [self.mode_shape[i] for i in self.local_modes]

@cached_property
def local_size(self) -> int:
    return prod(self.local_shape)

@cached_property
def spatial_size(self) -> int:
    return prod(self.spatial_shape)
```

注意 `spatial_shape` 里对负数的处理：`if i >= 0 else -i`，即复制模式 \(-k\) 贡献的大小是 \(k\)（取绝对值）。

`validate_layout` 的后半段检查 spatial/local 的合法性——编号在范围内、且**同一个 mode 不能同时既是 spatial 又是 local**（这是 `used_dims` 去重检查的目的），见 [register_layout.py:266-281](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L266-L281)。官方文档也特意留了一个思考题提示：如果允许一个 mode 同时属于两类会发生什么（答案是不允许，见 [register-layout.rst:262-267](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L262-L267)）。

构造便捷函数 `spatial` / `local` / `column_spatial` / `column_local` 定义在 `ops/register_ops.py`，它们本质上是「把所有 mode 都标成 spatial」或「都标成 local」的快捷方式，`column_*` 变体只是把 mode 顺序倒过来（列优先）。见 [register_ops.py:29-62](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L29-L62)（`spatial`）与 [register_ops.py:126-157](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L126-L157)（`column_spatial` / `column_local`）。`reduce` 产生复制模式的逻辑见 [register_ops.py:386-396](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/ops/register_ops.py#L386-L396)，关键一行是 `spatial_modes.append(-layout.mode_shape[spatial_dim])`——把被规约的 spatial mode 改写成负数。

#### 4.2.4 代码实践

**实践目标**：直接用 `register_layout` 构造 PTX `mma.sync.aligned.m16n8k8` 指令的 C 操作数布局（官方文档用 `repeat(...)` 链式写法展示，最终落到的规范形式见 [register-layout.rst:131-166](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L131-L166)），验证其线程数与每线程元素数。

> 📌 **关于导入路径**：官方 RST 里写的是 `from tilus.ir.layout import spatial, local, visualize_layout`，但当前源码中 `tilus.ir.layout` 包只导出了 `register_layout` / `RegisterLayout` / `MultiFunction` 等（见 [layout/__init__.py:16-21](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/__init__.py#L16-L21)）。`spatial` / `local` 等构造函数要从 `tilus.ir.layout.ops` 导入，`visualize_layout` 要从 `tilus.ir.layout.register_layout` 导入。本实践直接用确定可导入的 `register_layout`。

**操作步骤**：

```python
# 示例代码
from tilus.ir.layout import register_layout
from tilus.ir.layout.register_layout import visualize_layout

# m16n8k8 的 C 操作数布局：shape [16, 8]，一个 warp(32 线程)，每线程持 4 个 fp16
mma_c = register_layout(
    shape=[16, 8],
    mode_shape=[2, 8, 4, 2],
    spatial_modes=[1, 2],   # 空间维：8 * 4 = 32 个线程
    local_modes=[0, 3],     # 局部维：2 * 2 = 4 个元素/线程
)
print("spatial_size =", mma_c.spatial_size)   # 预期 32（一个 warp）
print("local_size   =", mma_c.local_size)     # 预期 4
print("spatial_shape=", mma_c.spatial_shape)  # 预期 [8, 4]
print("local_shape  =", mma_c.local_shape)    # 预期 [2, 2]
print(visualize_layout(mma_c))
```

**预期结果**（`spatial_size`/`local_size` 由 [register_layout.py:94-108](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L94-L108) 的 `prod` 直接给出，可确信）：

- `spatial_size = 32`：恰好一个 warp 的线程数。
- `local_size = 4`：每个线程持有 4 个 fp16 元素，正是 m16n8k8 的累加器碎片（fragment）规格。
- `visualize_layout` 会打印一张 \(16\times8\) 的网格，每格形如 `t : i`，表示「该元素在线程 t 的第 i 个局部槽位」。具体网格的逐格数值请**待本地验证**（应与 [register-layout.rst:134-166](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L134-L166) 一致）。

#### 4.2.5 小练习与答案

**练习 1**：官方文档里有 `local(3,4)` 与 `spatial(3,2)` 两个布局（见 [register-layout.rst:36-64](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L36-L64)）。请分别说出它们的 `spatial_modes` / `local_modes` 与 `spatial_size` / `local_size`。

**参考答案**：
- `local(3,4)`：`spatial_modes=[]`、`local_modes=[0,1]`，`spatial_size=1`（只有 1 个线程，即 thread 0 持有全部）、`local_size=12`。
- `spatial(3,2)`：`spatial_modes=[0,1]`、`local_modes=[]`，`spatial_size=6`（6 个线程各持 1 个）、`local_size=1`。

**练习 2**：`spatial_modes=[-3, 0]` 表示什么？为什么此时一个逻辑索引会对应**多个**线程号？

**参考答案**：`-3` 是一个复制模式，表示「复制 3 份」。它不对应任何真实 mode，而是声明该位置的元素被 3 个线程同时持有。因此把逻辑索引映射到线程号时，会沿这个复制维展开成 3 个不同的 `thread_id`（取值 0、1、2），即 `get_spatial` 返回的是一个**列表**而非单值。

---

### 4.3 MultiFunction 空间映射

#### 4.3.1 概念说明

4.2 节我们用「四步法」描述了映射过程，但那只是人脑的理解方式。Tilus 在源码里用一个更精炼的数学对象来统一表达它——**`MultiFunction`（多值函数）**。

为什么叫「多值」？因为有了复制模式，一个输入可能映射到**一组**输出。`MultiFunction` 的定义是（见 [mfunction.py:31-84](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py#L31-L84)）：

\[
Y = f(x_0, x_1, \dots, x_{n-1})
\]

其中输入 \((x_0,\dots,x_{n-1})\) 是逻辑索引（落在 `shape` 定义的网格里），输出 \(Y\) 是**一个整数集合**。它由三个东西唯一决定：

- `shape`：输入网格形状。
- `mode_shape`：shape 的细分（与 RegisterLayout 同义）。
- `modes`：一个有序列表，指明用哪些 mode（按什么顺序）来构造输出；**非负整数**是 `mode_shape` 的下标，**负数** \(-k\) 是大小为 \(k\) 的复制维。

`RegisterLayout` 把 `spatial_modes` 包装成一个 `MultiFunction`，就叫 `spatial_mfunction`——它就是「逻辑索引 → 持有该元素的线程号集合」这个映射。

#### 4.3.2 核心流程：spatial_mfunction 与三个方向

`RegisterLayout` 提供三个方向的查询，底层都依赖 mode 索引的展开/线性化：

- **正向（逻辑索引 → 线程）**：`get_spatial(global_indices)` 返回一个**列表**（因为复制模式），用 `spatial_mfunction()` 实现。
- **正向（逻辑索引 → 槽位）**：`get_local(global_indices)` 返回单个标量 `local_id`。
- **反向（线程 + 槽位 → 逻辑索引）**：`get_global(spatial_index=, local_index=)` 把线程号与槽位号还原回逻辑索引，用于代码生成时「给定线程，遍历它该处理的元素」。

`MultiFunction.__call__` 的算法（见源码 [mfunction.py:123-136](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py#L123-L136)）：

```
输入 x（逻辑索引）
1. 把 x 行优先线性化成一个标量，再按 mode_shape 展开成 mode_indices
2. 对 modes 中每个非负项，取 mode_indices[mode]；每个负项 -k 先填 0
3. 对所有复制维做笛卡尔积（每个复制维取 0..k-1），
   每种组合把对应位置填入，再按 image_shape 线性化，加入结果集
返回 结果列表
```

其中 `image_shape`（即 MultiFunction 输出空间的形状）= `[mode_shape[m] if m>=0 else -m for m in modes]`，见 [mfunction.py:98-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py#L98-L100)。

#### 4.3.3 源码精读

`spatial_mfunction` 直接把布局的 `spatial_modes` 包成 MultiFunction：

[python/tilus/ir/layout/register_layout.py:114-120](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L114-L120)：

```python
def spatial_mfunction(self) -> MultiFunction:
    """Get the multi-function that maps the global indices to the spatial indices (serialized)."""
    return multi_function(
        shape=self.shape,
        mode_shape=self.mode_shape,
        modes=self.spatial_modes,
    )
```

`get_spatial` 内部其实和 `MultiFunction.__call__` 等价，但它直接访问布局字段，并对复制维显式做笛卡尔积展开（所以返回 list），见 [register_layout.py:122-144](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L122-L144)：

```python
def get_spatial(self, global_indices):
    # 1) 展开成 mode 索引（按 grouped_modes 拆每个维度）
    ...
    # 2) 复制维先记 0，正常维取 mode_indices[mode]
    for i, mode in enumerate(self.spatial_modes):
        if mode < 0:
            replicate_dims.append(i); replicate_sizes.append(-mode)
            spatial_indices.append(0)
        else:
            spatial_indices.append(mode_indices[mode])
    # 3) 笛卡尔积遍历所有复制维，生成多个结果
    for items in itertools.product(*[range(s) for s in replicate_sizes]):
        ...
        results.append(index_serialize(spatial_indices, self.spatial_shape))
    return results
```

`get_local` 更简单——local 没有复制模式，所以只取一次、线性化一次，返回单个 `Expr`，见 [register_layout.py:146-159](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L146-L159)。

`get_global` 是反演：先把 `spatial_index` 与 `local_index` 各自展开回坐标，写回 `mode_indices` 的对应位置（复制维直接跳过），再按 `grouped_modes` 重新组合成各维度的逻辑索引，见 [register_layout.py:161-178](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L161-L178)。

> 💡 **设计要点**：`MultiFunction` 不仅服务于查询，它的 `__mul__`（[mfunction.py:151-181](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py#L151-L181)）与 `cover` / `collapse`（[mfunction.py:183-197](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py#L183-L197)）还是布局组合与布局推理（u4-l5）的数学基础——把「布局运算」彻底归约成了「多值函数的复合与覆盖判定」。这也是 Tilus 把它单独抽成一个类的原因。

#### 4.3.4 代码实践

**实践目标**：用 `spatial_mfunction()` 与 `get_local` 验证「同一个逻辑索引，在 m16n8k8 布局下落到哪个线程、哪个槽位」，并用 `visualize_layout` 交叉核对。

**操作步骤**：

```python
# 示例代码
from tilus.ir.layout import register_layout
from tilus.ir.layout.register_layout import visualize_layout

mma_c = register_layout(
    shape=[16, 8],
    mode_shape=[2, 8, 4, 2],
    spatial_modes=[1, 2],
    local_modes=[0, 3],
)

mf = mma_c.spatial_mfunction()
print("spatial_mfunction:", mf)

# 取逻辑索引 (i=5, j=3)，查它落在哪个线程、哪个槽位
gi = [5, 3]
print("thread ids =", mma_c.get_spatial(gi))   # 预期单个值的列表（无复制模式）
print("local id   =", mma_c.get_local(gi))

# 反演：用上面得到的 (spatial_index, local_index) 还原逻辑索引
tid = mma_c.get_spatial(gi)[0]
lid = mma_c.get_local(gi)
print("round-trip =", mma_c.get_global(spatial_index=tid, local_index=lid))  # 预期回到 [5, 3]
```

**需要观察的现象与预期结果**：

- `get_spatial` 返回单元素列表（因为该布局没有复制模式），其值在 \([0, 32)\) 区间。
- `get_global` 是 `get_spatial`/`get_local` 的反演，`round-trip` 应当还原成原来的 `[5, 3]`（这是检验映射自洽性的好方法）。
- 把 `visualize_layout(mma_c)` 打印出来，找到网格里 `(5, 3)` 那一格，应显示与上面一致的 `tid : lid`。

> 具体数值由 mode 索引展开决定（可按 4.2.2 的四步法手算复核），精确结果请**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `spatial_mfunction` 返回的是一个「多值函数」而 `get_local` 只返回单个值？

**参考答案**：因为 spatial 维度允许出现复制模式（负数 mode），一个逻辑元素可能被多个线程持有，所以「逻辑索引 → 线程号」是一对多的，需要返回集合；而 local 维度描述的是单线程内部的槽位，不存在复制，一个元素在某个线程里只占一个槽位，所以是一对一的单值。

**练习 2**：`MultiFunction` 的 `_image_shape`（[mfunction.py:98-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py#L98-L100)）对负数 mode 取 `-mode` 作为大小。请结合 4.2 节解释这个 `-mode` 与 RegisterLayout 的 `spatial_shape` 里对负数的处理为什么是一致的。

**参考答案**：两者描述的是同一件事——复制模式 \(-k\) 表示「复制 \(k\) 份」，它贡献的「空间大小」是 \(k\)，所以都要把负数取绝对值。`spatial_shape` 用 `-i`，`_image_shape` 也用 `-mode`，语义完全一致，只是分别服务于 RegisterLayout 与 MultiFunction 两个视角。

## 5. 综合实践

**任务**：为一个 \([64, 64]\) 的 fp16 累加器张量，在 128 线程（4 个 warp）上设计一个贴近 Ampere `m16n8k16` MMA 的寄存器布局，并验证它满足「128 个线程、每线程 32 个元素」的约束。

**设计思路**（把 4.1～4.3 串起来）：

1. **M=64 由 4 个 warp 各管 16 行** → 引入一个大小为 4 的 spatial mode（warp 维）。
2. **单个 warp 内的 \([16, 8]\) MMA 碎片**沿用 4.2.4 的 m16n8k8 结构：`mode_shape` 里的 `[2, 8, 4, 2]`，其中 `[8,4]`（spatial）跨 32 个线程、`[2,2]`（local）是每线程 4 元素。
3. **N=64 = 8 个宽为 8 的 MMA 列块** → 每个 warp 要重复 8 次 \([16,8]\) 的模式，这 8 份**落到同一线程的局部存储**（每个线程累加 8 个列块），所以 N 方向的大小 8 是一个 **local** mode。
4. 把以上拼成 6 个 mode，按 `grouped_modes = [[0,1,2],[3,4,5]]` 排布：

| mode 下标 | 大小 | 含义 | 归属 |
| --- | --- | --- | --- |
| 0 | 4 | warp（M 方向 4 个 warp） | spatial |
| 1 | 2 | warp 内 m16 的 local 碎片 | local |
| 2 | 8 | warp 内 m16 的 spatial 行 | spatial |
| 3 | 8 | N 方向 8 个列块（复制到局部） | local |
| 4 | 4 | warp 内 n8 的 spatial 列 | spatial |
| 5 | 2 | warp 内 n8 的 local 碎片 | local |

**操作步骤**：

```python
# 示例代码
from tilus.ir.layout import register_layout
from tilus.ir.layout.register_layout import visualize_layout

acc = register_layout(
    shape=[64, 64],
    mode_shape=[4, 2, 8, 8, 4, 2],
    spatial_modes=[0, 2, 4],   # 4 * 8 * 4 = 128 个线程
    local_modes=[1, 3, 5],     # 2 * 8 * 2 = 32 个元素/线程
)

# 验证核心不变量
assert acc.size == 64 * 64 == 4096
assert acc.spatial_size == 128    # 4 个 warp
assert acc.local_size == 32       # 每线程 32 个 fp16
assert acc.spatial_size * acc.local_size == acc.size  # 无复制模式时的守恒

print("grouped_modes =", acc.grouped_modes)   # 预期 [[0,1,2],[3,4,5]]
print("spatial_shape =", acc.spatial_shape)   # 预期 [4, 8, 4]
print("local_shape   =", acc.local_shape)     # 预期 [2, 8, 2]

# 抽查一个元素的自洽性（反演应回到原索引）
tid = acc.get_spatial([10, 20])[0]
lid = acc.get_local([10, 20])
print("round-trip =", acc.get_global(spatial_index=tid, local_index=lid))  # 预期 [10, 20]
```

**预期结果**：

- 三个断言全部通过：`spatial_size == 128`、`local_size == 32`、`size == 4096`。这三个值由 [register_layout.py:94-112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L94-L112) 的 `prod` 直接保证。
- `round-trip` 应回到 `[10, 20]`，证明 `(thread_id, local_id) ↔ 逻辑索引` 的映射是自洽双射（在无复制模式时）。

**延伸思考**：如果你把 `local_modes` 里的 mode 3（大小 8 的列块维）从 local 挪到 spatial，会发生什么？线程数会变成 \(128\times8=1024\)，而每线程元素数降到 4——这对应「用更多线程并行处理 N 方向」的另一种分块策略。这正是布局系统表达「分块/并行策略」的威力，更多变换见 [u4-l4](u4-l4-layout-operations-and-compose.md)。

## 6. 本讲小结

- **mode / mode_shape** 是对张量维度的「细分」：一个 `shape` 维度可拆成多个 mode，所有 mode 的乘积等于 `shape` 的总元素数；`grouped_modes` 记录每个 shape 维度包含哪些 mode。
- 一个 `RegisterLayout` 由 **四个字段** 唯一确定：`shape`、`mode_shape`、`spatial_modes`、`local_modes`。创建务必走 `register_layout(...)` 工厂，它会校验并规范化。
- **spatial modes** 把元素分配给不同线程（线性化得 `thread_id`），**local modes** 区分线程内部槽位（线性化得 `local_id`）；二者不允许共用同一个 mode。
- **负数 spatial mode \(-k\)** 表示复制：该元素被 \(k\) 个线程共同持有，此时 `get_spatial` 返回的是**列表**，且 `size < spatial_size * local_size`。
- 三个核心不变量：`spatial_size`（线程数）、`local_size`（每线程元素数）、`size`（总元素数），都是相应 shape 的乘积。
- **`MultiFunction`** 把「逻辑索引 → 线程号集合」抽象成可复合的多值函数，`spatial_mfunction()` 即此映射；`get_local` / `get_spatial` / `get_global` 提供正反两个方向的查询，且无复制时正反映射自洽。

## 7. 下一步学习建议

本讲只覆盖了 RegisterLayout 的**静态结构与映射**。建议接着阅读：

1. **[u4-l3 SharedLayout、GlobalLayout 与 TMemoryLayout](u4-l3-shared-global-tmemory-layouts.md)**：另外三种布局如何用各自的 mode/swizzle/lane 描述非分布式内存，与本讲的分布式 RegisterLayout 形成对照。
2. **[u4-l4 布局操作：compose、divide 与 reduce](u4-l4-layout-operations-and-compose.md)**：本讲里点到为止的 `compose`（`*` 运算符）、`divide`、`reduce`、`reshape` 等布局代数，以及 `MultiFunction.__mul__` / `cover` 如何支撑这些运算。
3. **[u4-l5 布局自动推理（Layout Inference）](u4-l5-layout-inference.md)**：编译期如何用前向/反向规则，给那些 `optional_layout` 尚未绑定的寄存器张量自动填上合适的 RegisterLayout——这正是 u4-l1 留下的「延迟绑定」闭环。
4. 想看更多真实 MMA 布局样例，可阅读 [register-layout.rst:121-169](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/layout-system/register-layout.rst#L121-L169)，并用 `visualize_layout` 自行把 m16n8k16、wgmma 等指令的碎片布局画出来对照 PTX 手册。
