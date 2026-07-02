# 数据模型：全局数组 Array

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 **全局数组（global array）** 在 cuTile 中扮演的角色，以及它和 tile 的根本区别。
- 用 `base_addr + sizeof(dtype) * Σ(stride_i * index_i)` 这个公式，手算任意多维数组元素的字节偏移量。
- 解释「数组只能由 host 分配、tile 代码里只能创建视图」这条规则的含义与原因。
- 知道 CuPy、PyTorch 等外部框架的张量是如何通过 **DLPack** 与 **CUDA Array Interface** 被传进内核的，以及这两条路径在底层源码里的优先级。

本讲承接 [u2-l1 执行模型](u2-l1-execution-model.md) 中建立的 grid/block/执行空间直觉：我们已经知道一次 `ct.launch` 会用一串 block 去并行执行 kernel 函数体，但「block 算的是什么数据」一直没有展开。本讲就来回答这个问题——cuTile 处理的基本数据单元就是 **全局数组**。

## 2. 前置知识

阅读本讲前，请先具备以下认知（来自前面几讲）：

- **cuTile 的执行单元是 block，不暴露单个线程**。tile 运算由整个 block 集体并行完成（详见 u2-l1）。
- **三种执行空间**：host code（CPU 上跑，如 `ct.launch`）、tile code（kernel 函数体内）、SIMT code（`@ct.function` 中标注 `device` 的代码）。
- 一个内核由「装饰器 + 函数体 + host 端 `ct.launch`」组成，函数体在被 `launch` 之前不会执行（详见 [u1-l2](u1-l2-install-and-first-kernel.md)）。

下面几个名词是本讲的新术语，先给一句话直觉，后面会精读源码：

- **global array（全局数组）**：放在 GPU 全局显存里、可被读写、由 host 分配的多维数组。它是 host 与 tile 代码之间交换数据的主通道。
- **strides（步幅）**：一个和 shape 等长的整数元组，决定「沿某一维前进一格，物理地址要跳几个元素」。它让同一个 shape 可以对应行优先、列优先等不同物理布局。
- **DLPack / CUDA Array Interface**：两套跨框架共享 GPU 张量的标准协议。任何实现了其中任一协议的对象（如 PyTorch 张量、CuPy 数组）都能作为内核参数传进来。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [docs/source/data.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst) | 数据模型的官方说明，本讲的概念基础几乎都在它的 "Global Arrays" 一节里。 |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py) | `Array` 类的 Python 类型存根（type stub），定义了 `shape`/`strides`/`dtype`/`ndim`/`slice` 等属性签名。 |
| [cext/tile_kernel.cpp](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp) | C++ 运行时桥接，真正把外部张量「翻译」成 cuTile 内部 Array 表示（含 DLPack / CUDA Array Interface 两条解析路径）的地方。 |
| [samples/quickstart/VectorAdd_quickstart.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py) | 快速入门示例，演示用 CuPy 数组当内核参数的最小写法。 |

> 说明：`_stub.py` 里的 `class Array` 只是给前端类型检查与文档用的「签名壳」，真正的数据搬运发生在 C++ 侧。这种「Python 存根 + C++ 实现」的拆分是 cuTile 的常见架构，我们在 [u1-l3](u1-l3-repo-layout-and-build.md) 里已经见过 `_cext` 这座桥。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **全局数组 Array**——它是什么、谁分配、谁只读。
2. **Strides 内存布局与地址计算**——逻辑下标如何映射到物理地址。
3. **DLPack 与 CUDA Array Interface**——宿主张量如何进入内核。

### 4.1 全局数组 Array：cuTile 的根本数据结构

#### 4.1.1 概念说明

cuTile 官方文档开宗明义：它是一个 **基于数组（array-based）的编程模型**，只暴露数组、不暴露指针。文档给出的理由是：

- 数组知道自己的边界，访问可以做安全检查；
- 基于数组的 load/store 可以高效地降级到硬件最快的访存机制（如 TMA）；
- Python 程序员对 NumPy 这类数组框架已经很熟悉；
- 指针对 Python 不自然。

一个 **全局数组（global array）** 是「某种 `dtype` 元素在逻辑多维空间里排布的容器」。它有三个核心属性：

- **`shape`**：一个整数元组，每一项是该维的长度；元组长度等于维数（rank），所有项的乘积等于元素总数。
- **`strides`**：和 `shape` 等长的整数元组，决定物理布局（详见 4.2）。
- **`dtype`**：所有元素的统一数据类型（cuTile 不支持异质数组）。

最关键的一条所有权规则：

> **新数组只能由 host 分配，并作为参数传给 tile 内核；tile 代码里只能对已有数组创建视图（view），例如 `Array.slice`，绝不拷贝、绝不新建底层存储。**

这和 Python 的引用语义一致：`b = a` 不拷贝数据，只是多一个引用指向同一片内存。

#### 4.1.2 核心流程

一个数组的生命周期可以画成下面这条单向流：

```text
  host code                              tile code (kernel 体内)
┌───────────────────────┐               ┌─────────────────────────┐
│ 1. 分配张量           │               │ 4. 接收 Array 参数       │
│   (torch/cupy/numpy)  │               │ 5. 用 slice 创建视图     │
│ 2. (可选)创建输出张量 │   launch时    │ 6. ct.load 取出 tile     │
│ 3. 作为实参传入 launch│ ───────────► │ 7. ct.store 写回 tile    │
└───────────────────────┘   桥接        └─────────────────────────┘
        （数组内存始终在全局显存，内核内只能「看」不能「造」）
```

要点：

- 第 1、2、3 步都在 host code 里完成；第 4~7 步在 tile code 里。
- 「视图」操作（`slice`、`tiled_view`）不分配新显存，只是换了 `(base_ptr, shape, strides)` 的组合。
- 若传入两个或以上数组参数，它们的显存 **不得重叠**，否则行为未定义（这是为了避免数据竞争，相关保证在 [u4-l3 内存模型](u4-l3-memory-model-and-atomics.md) 详述）。

#### 4.1.3 源码精读

官方文档在 "Global Arrays" 一节里写明了所有权规则：

[docs/source/data.rst:51-54](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L51-L54) — 这几行说明「新数组只能由 host 分配、内核内只能创建视图、赋值只是新建引用不拷贝」。

`Array` 类的存根定义在 `_stub.py`，它的核心属性签名如下：

[src/cuda/tile/_stub.py:138-176](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L138-L176) — `class Array` 声明了 `dtype` / `shape` / `strides` / `ndim` 四个只读属性。注意 `shape` 和 `strides` 的返回类型标注是 `tuple[int32, ...]`，而 `dtype` / `ndim` 是常量（constant）：

- `Array.shape` 返回 `tuple[int32, ...]`——是 **运行时值（非常量）**。文档专门解释了为什么用 `int32`：用 32 位整数存形状能提升性能，代价是单维最多约 21 亿个元素（`2,147,483,647`），这个限制未来会放宽。这一点在 [docs/source/data.rst:62-65](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L62-L65) 有明确说明。
- `Array.dtype` 返回 **常量** `DType`——因为元素类型在编译期就确定了。
- `Array.ndim` 返回常量 `int`。

`slice` 是「创建视图」的典型入口，它的 docstring 明确写了 **不拷贝数据**：

[src/cuda/tile/_stub.py:177-234](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L177-L234) — `slice(axis, start, stop)` 沿单一轴切片，返回的数组指向同一片内存、只是限制了范围，"No data is copied"。`start`/`stop` 还可以是运行时标量（动态切片）。

> 形状/步幅的「索引整数类型」也可以被注解强制成 `int64`：`ArrayAnnotation(index_dtype=int64)`（[src/cuda/tile/_stub.py:998-1011](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L998-L1011)），对应公开的 `ct.IndexedWithInt64` 别名（[src/cuda/tile/_stub.py:1059-1068](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L1059-L1068)）。当某个张量的形状或步幅可能超过 32 位范围时，用它来开启 i64 支持。

#### 4.1.4 代码实践

**实践目标**：通过阅读 docstring，确认 `Array.shape` 与 `Array.dtype` 在「常量性」上的差异，并理解为什么这会影响编译。

**操作步骤**：

1. 打开 [src/cuda/tile/_stub.py:150-157](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L150-L157)（`shape` 属性）与 [src/cuda/tile/_stub.py:141-148](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L141-L148)（`dtype` 属性）。
2. 在一个内核里写 `n = x.shape[0]`，再写 `d = x.dtype`。
3. 思考：`n` 能不能用在 `range(...)` 的上界里、或者参与 `ct.load` 的 tile 形状？`d` 能不能？

**需要观察的现象**：

- `x.shape[0]` 是运行时 `int32` 标量，可以参与运行时计算（如 `ct.num_tiles`、`range` 边界），但 **不能** 用来当 tile 的 shape（tile 形状必须是编译期常量、且每维为 2 的幂，这会在 [u2-l3](u2-l3-data-model-tile.md) 详述）。
- `x.dtype` 是编译期常量，可以传给 `ct.zeros(shape, dtype=x.dtype)` 这类需要常量 dtype 的工厂函数。

**预期结果**（待本地验证）：把 `x.shape[0]` 误用为 tile 形状时，编译器应报「tile 形状必须是常量」类错误；而 `x.dtype` 可以正常用在工厂函数里。

#### 4.1.5 小练习与答案

**练习 1**：cuTile 为什么选择「数组模型」而不是「指针模型」？请列出至少两条理由。

> **参考答案**：（1）数组自带边界，可做安全/越界检查；（2）基于数组的 load/store 能高效降级到 TMA 等硬件机制；（3）契合 Python/NumPy 习惯；（4）指针对 Python 不自然。原文见 [docs/source/data.rst:16-21](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L16-L21)。

**练习 2**：在 tile 代码里写 `y = x.slice(axis=0, start=1, stop=3)`，这会分配新的显存吗？

> **参考答案**：不会。`slice` 只创建一个指向同一片物理内存、范围受限的新视图，不拷贝数据（"No data is copied"）。

---

### 4.2 Strides 内存布局与地址计算

#### 4.2.1 概念说明

同一个逻辑 shape（比如 `(4, 8)`）在显存里可以有多种排布方式：行优先（C order）、列优先（F order）、甚至带「洞」的非连续布局。**strides** 就是描述这种排布的机制。

文档把全局数组定义为 **strided memory layout**（步幅式内存布局）：除了 shape，每个数组还有一个等长的 strides 元组，负责把「逻辑下标」映射到「物理内存位置」。

> 注意 cuTile 里的 strides 单位是 **元素个数**（不是字节）。这一点从 `Array.strides` 的 docstring「The number of **elements** to step」可以确认（见 4.2.3）。后面会看到，外部协议 `__cuda_array_interface__` 用的是字节步幅，C++ 桥接会做一次单位换算。

#### 4.2.2 核心流程

对一个三维 `float32` 数组，strides 为 `(s1, s2, s3)`，元素 `(i1, i2, i3)` 的字节地址为：

\[
\text{addr}(i_1, i_2, i_3) = \text{base\_addr} + 4 \cdot (s_1 i_1 + s_2 i_2 + s_3 i_3)
\]

其中 `4` 是 `float32` 的字节数。一般化到任意维度与任意 `dtype`：

\[
\text{addr}(\mathbf{i}) = \text{base\_addr} + \text{sizeof}(\text{dtype}) \cdot \sum_{k} s_k \, i_k
\]

两种典型布局的 stride 推导（以 shape `(M, N)`、行优先/列优先为例）：

| 布局 | strides（元素单位） | 含义 |
| --- | --- | --- |
| 行优先（C order，默认） | `(N, 1)` | 沿第 0 维走一格跳 N 个元素；沿第 1 维走一格跳 1 个元素 |
| 列优先（F order） | `(1, M)` | 沿第 0 维走一格跳 1 个元素；沿第 1 维走一格跳 M 个元素 |

当 strides 缺省（如某些外部协议没给）时，cuTile 默认按 **行优先** 推导 strides。这个「缺省补全」逻辑在 C++ 源码里叫 `fill_row_major_strides`。

#### 4.2.3 源码精读

文档给出的地址计算公式原文：

[docs/source/data.rst:39-49](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L39-L49) — 明确写出 `base_addr + 4 * (s1 * i1 + s2 * i2 + s3 * i3)`，并说明 `4` 是 `float32` 的字节数。

`Array.strides` 属性签名：

[src/cuda/tile/_stub.py:159-166](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L159-L166) — 返回 `tuple[int32, ...]`，docstring 写 "The number of **elements** to step in each dimension while traversing the array"，确认单位是「元素」。

C++ 侧解析 `__cuda_array_interface__` 的 strides 时，做的正是「字节步幅 → 元素步幅」的换算，并处理缺省情况：

[cext/tile_kernel.cpp:925-943](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L925-L943) — 这段代码先取出外部协议给出的 `strides`：若为空或 `None`，调用 `fill_row_major_strides` 按行优先补全（默认布局）；若是一个元组，则逐维 `stride / dtype_bytewidth`，把字节步幅除以每个元素的字节数，转成 cuTile 内部使用的「元素步幅」。

> 这是一个非常关键的细节：**外部世界（CUDA Array Interface）用字节，cuTile 内部用元素**。这条换算保证了不同框架、不同布局的张量进入内核后都用同一套地址公式。

#### 4.2.4 代码实践

**实践目标**：手算一个真实数组的元素字节偏移量，验证对 strided 布局的理解。

**操作步骤**：

1. 给定一个 `float32`、`shape=(4, 8)`、`strides=(8, 1)` 的数组（行优先）。
2. 求元素 `(1, 3)` 相对 `base_addr` 的 **字节偏移量**。

**推导**：

\[
\text{offset}_{\text{elem}} = s_0 \cdot i_0 + s_1 \cdot i_1 = 8 \times 1 + 1 \times 3 = 11 \text{（个元素）}
\]

\[
\text{offset}_{\text{byte}} = \text{sizeof}(\text{float32}) \times 11 = 4 \times 11 = 44 \text{（字节）}
\]

**预期结果**：字节偏移量为 **44 字节**（即从 `base_addr` 起第 11 个 `float32` 元素）。

**延伸观察（待本地验证）**：用 NumPy 可以直接核对你的手算结果——下面是 **示例代码**（非项目源码）：

```python
import numpy as np
a = np.empty((4, 8), dtype=np.float32)   # 行优先，strides 以字节计
print(a.strides)                          # 期望 (32, 4) 字节，即元素单位 (8, 1)
# 低层地址差：
addr00 = a.__array_interface__['data'][0]
print((a[1, 3].__array_interface__['data'][0] - addr00))   # 期望 44
```

注意 NumPy 的 `a.strides` 是 **字节** 步幅 `(32, 4)`，恰好等于 cuTile 元素步幅 `(8, 1)` 乘以 `sizeof(float32)=4`——这正是上面 C++ 代码做的那步换算。

#### 4.2.5 小练习与答案

**练习 1**：一个 `shape=(3, 4, 5)` 的行优先 `float32` 数组，它的元素单位 strides 是多少？

> **参考答案**：`(20, 5, 1)`。第 0 维走一格要跨过 `4×5=20` 个元素，第 1 维跨 `5` 个，第 2 维跨 `1` 个。

**练习 2**：如果把上题改成列优先（F order），strides 变成什么？

> **参考答案**：`(1, 3, 12)`。列优先下最左维最连续（步幅 1），向右逐维累乘前面的形状：`(1, 3, 3×4=12)`。

**练习 3**：为什么 `__cuda_array_interface__` 的 strides 是字节单位，而 cuTile 内部用元素单位？

> **参考答案**：CUDA Array Interface 协议规定 strides 以字节计，以便支持任意对齐；cuTile 内部统一用元素单位，使地址公式与 dtype 解耦，便于跨 dtype 复用同一套访存代码。桥接层在 [cext/tile_kernel.cpp:934-938](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L934-L938) 用 `stride / dtype_bytewidth` 做换算。

---

### 4.3 DLPack 与 CUDA Array Interface：宿主张量的入口

#### 4.3.1 概念说明

cuTile 自己 **不提供** 在 GPU 上分配张量的 API——分配永远是宿主框架（PyTorch、CuPy 等）的事。那么这些外部张量是怎么「变成」cuTile 内部 `Array` 的？答案是两套跨框架的零拷贝交换协议：

- **DLPack**：一个通用的张量交换标准，对象暴露 `__dlpack__()` 方法，返回一个携带 `(data_ptr, shape, strides, dtype, device)` 的 capsule。PyTorch、CuPy 等都支持。
- **CUDA Array Interface（CAI）**：NumPy 阵营为 GPU 张量定义的协议，对象暴露 `__cuda_array_interface__` 属性，返回一个字典，含 `shape` / `typestr` / `data` / `strides` 等键。

文档明确写道：「任何实现了 DLPack 或 CUDA Array Interface 的对象都能作为内核参数——例如 CuPy 数组和 PyTorch 张量。」

因为两者都是零拷贝（只传指针与元数据），把一个 `torch.Tensor` 传进内核 **不会复制显存**，内核直接读写它背后的那片全局显存。

#### 4.3.2 核心流程

C++ 运行时在收到一个 Python 参数时，会按下面的优先级 **判定它属于哪一类**，再走对应的解析路径：

```text
           classify_arg(arg)
                 │
   ┌─────────────┼───────────────────────────┐
   ▼             ▼                           ▼
torch.Tensor?  有 __dlpack__?          有 __cuda_array_interface__?
   │             │                           │
TorchTensorDlpack  DlpackArray              CudaArray
   │             │                           │
   └─────────────┴─────────────┬─────────────┘
                               ▼
                  统一归为 ParameterKind::Array
                               │
              解析出 (base_ptr, shape, strides, dtype)
                               ▼
                  交给后续 IR / launch 流程
```

要点：

- **PyTorch 张量走专门的快路径**：因为它能通过 `torch._C._to_dlpack` 直接拿到 capsule，比调用 `arg.__dlpack__()` 更快（少了 Python 层开销）。
- **CuPy 等其他框架**：既支持 `__dlpack__` 也支持 `__cuda_array_interface__`，但判定顺序里 `__dlpack__` 先于 CAI，所以通常走 DLPack 路径。
- 三条路径最终都归一成同一种内部 Array 表示（`ParameterKind::Array`），后端无需关心来源。

#### 4.3.3 源码精读

参数分类的优先级定义在 `classify_arg`：

[cext/tile_kernel.cpp:472-500](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L472-L500) — 先判基本类型（bool/int/float/list），再判 `torch.Tensor`（走快路径 `TorchTensorDlpack`），接着判有无 `__dlpack__`（→ `DlpackArray`），最后判 `__cuda_array_interface__`（→ `CudaArray`），都不满足则抛 `TypeError`。注释里点明了为什么 torch 走快路径：`torch._C._to_dlpack` 直入 C++、绕开 Python。

三类参数都映射到同一种内部种类：

[cext/tile_kernel.cpp:459-470](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L459-L470) — `param_kind_from_pyarg_kind` 把 `TorchTensorDlpack`/`DlpackArray`/`CudaArray` 统统映射成 `ParameterKind::Array`，证明三条入口最终归一。

CUDA Array Interface 路径的解析（取 `typestr`/`shape`/`data`，再处理 strides）：

[cext/tile_kernel.cpp:876-953](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L876-L953) — `arrayrepr_cuda_array_iface` 从对象的 `__cuda_array_interface__` 属性里取出字典，校验它是 dict，解析 `typestr`（dtype）、`data`（设备指针）、`shape`，再把 `strides` 换算成元素单位（见 4.2.3），最终组装成一个 `ArrayRepr`（含 `base_ptr`、shape、strides、dtype、index 位宽）。

DLPack 路径的公共解析逻辑：

[cext/tile_kernel.cpp:955-969](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L955-L969) — `arrayrepr_dlpack_common` 从 capsule 取出 `DLManagedTensor`，**校验设备必须是 CUDA**（`kDLCUDA`，否则报错 "Input array is not on a CUDA device"），并把 `byte_offset` 加到 `data` 上得到真正的 `base_ptr`。这就是为什么传一个 CPU 上的 NumPy 数组会失败——它不在 CUDA 设备上。

> 文档对「可传入对象类型」的原话见 [docs/source/data.rst:56-60](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L56-L60)，其中也强调了多数组参数的显存不得重叠。

#### 4.3.4 代码实践

**实践目标**：写一个最小的内核启动代码，让它同时接受 `torch.Tensor` 与 `cupy.ndarray`，验证两者都能作为内核参数（走不同的 DLPack 路径却得到相同结果）。

**操作步骤**：

1. 准备一个把两个向量相加的内核（直接借用快速入门范式）。
2. 分别用 `torch` 和 `cupy` 构造输入，调用同一个内核。
3. 比较两条路径的结果是否一致。

下面是 **示例代码**（基于 [samples/quickstart/VectorAdd_quickstart.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py) 改写，非项目原有文件）：

```python
# 示例代码：演示 torch.Tensor 与 cupy.ndarray 都能当内核参数
import cuda.tile as ct

@ct.kernel
def add_kernel(a, b, c, TILE: ct.Constant[int]):
    pid = ct.bid(0)
    a_tile = ct.load(a, index=(pid,), shape=(TILE,))
    b_tile = ct.load(b, index=(pid,), shape=(TILE,))
    ct.store(c, index=(pid,), tile=a_tile + b_tile)

def run_with_torch():
    import torch
    N, TILE = 1 << 12, 1 << 4
    a = torch.randn(N, device='cuda'); b = torch.randn(N, device='cuda')
    c = torch.empty_like(a)
    grid = (ct.cdiv(N, TILE), 1, 1)
    ct.launch(torch.cuda.current_stream(), grid, add_kernel, (a, b, c, TILE))
    torch.cuda.synchronize()
    torch.testing.assert_close(c, a + b)
    print("torch 路径 OK")        # torch.Tensor -> TorchTensorDlpack 快路径

def run_with_cupy():
    import cupy as cp
    N, TILE = 1 << 12, 1 << 4
    a = cp.random.randn(N); b = cp.random.randn(N); c = cp.empty_like(a)
    grid = (ct.cdiv(N, TILE), 1, 1)
    ct.launch(cp.cuda.get_current_stream(), grid, add_kernel, (a, b, c, TILE))
    cp.testing.assert_allclose(cp.asnumpy(c), cp.asnumpy(a) + cp.asnumpy(b))
    print("cupy 路径 OK")          # cupy.ndarray -> DlpackArray 路径

if __name__ == "__main__":
    run_with_torch()
    run_with_cupy()
```

**需要观察的现象**：

- 两个框架的张量都无需任何手动转换，直接塞进 `ct.launch` 的参数元组即可。
- torch 路径在 C++ 里命中 `TorchTensorDlpack`；cupy 路径命中 `DlpackArray`。
- 内核本身对「参数来自哪个框架」完全无感——它只看到一个 `Array`。

**预期结果**（待本地验证）：两条路径都打印 OK，结果数值与各自框架的原生加法一致。若误传一个 CPU 上的 `numpy.ndarray`，应在 C++ 侧抛出 "Input array is not on a CUDA device"（DLPack 路径的设备校验失败）。

#### 4.3.5 小练习与答案

**练习 1**：把一个 CPU 上的 `numpy.ndarray` 直接传给 `ct.launch` 会发生什么？为什么？

> **参考答案**：会报错。NumPy 数组在 CPU 上，DLPack 路径会校验 `device_type == kDLCUDA` 失败（[cext/tile_kernel.cpp:961-962](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L961-L962)）；它也没有 `__cuda_array_interface__`，最终被 `classify_arg` 判为不支持类型而抛 `TypeError`。

**练习 2**：CuPy 数组同时实现了 `__dlpack__` 和 `__cuda_array_interface__`，cuTile 实际会走哪条？为什么？

> **参考答案**：走 DLPack（`DlpackArray`）。因为 `classify_arg` 先检查 `__dlpack__`（[cext/tile_kernel.cpp:493-494](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L493-L494)），命中后直接返回，不会再走到 CAI 那条分支。

**练习 3**：为什么 PyTorch 张量要单独走 `TorchTensorDlpack` 快路径，而不是和其他框架一样调用 `__dlpack__()`？

> **参考答案**：因为 `torch._C._to_dlpack` 直接在 C++ 内拿到 capsule，省去了从 Python 调用 `__dlpack__()` 的解释器开销，更快（注释见 [cext/tile_kernel.cpp:486-490](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L486-L490)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「布局感知的元素定位」小任务。

**任务**：给定一个由宿主框架分配的二维张量，要求你：

1. **在 host 端**查出它的 `shape`、字节 `strides`，推算出 cuTile 内部的元素单位 `strides`。
2. **在内核里**用 `ct.load` 取出左上角 `(2, 4)` 的 tile 并打印，验证取到的元素符合你按 strides 推算的位置。
3. 故意构造一个 **非连续** 张量（例如对一个 `(4, 8)` 行优先张量做转置得到 `(8, 4)`），重复第 2 步，观察 cuTile 是否仍能正确定位元素（即 strides 是否被正确解析）。

**提示**：

- 第 1 步用 4.2 的公式：元素 strides = 字节 strides ÷ `sizeof(dtype)`。
- 第 2 步内核写法参考 [samples/quickstart/VectorAdd_quickstart.py:15-28](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py#L15-L28)，把 `load+store` 换成 `print`。
- 第 3 步的关键观察点：转置后字节 strides 变成 `(4, 32)`（即元素 `(1, 8)`），cuTile 在 [cext/tile_kernel.cpp:934-938](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp#L934-L938) 把它换算回元素单位，因此 `ct.load` 仍能取到「逻辑上」正确的 tile。

**预期结果**（待本地验证）：连续与非连续两种情况下，`ct.load` 取出的 tile 数值都与你用宿主框架直接索引得到的一致——这正是 strided 布局 + DLPack 零拷贝带来的「正确性不依赖物理排布」的体现。

## 6. 本讲小结

- **全局数组是 cuTile 的根本数据结构**：基于数组的模型带来边界安全、高效降级与 NumPy 一致性；cuTile 只暴露数组、不暴露指针。
- **数组只能由 host 分配，内核内只能创建视图**：`slice`/`tiled_view` 等操作不拷贝显存；多个数组参数的显存不得重叠。
- **strided 布局的地址公式**：\(\text{addr} = \text{base\_addr} + \text{sizeof}(\text{dtype}) \cdot \sum_k s_k i_k\)，其中 cuTile 的 strides 以 **元素** 为单位；外部 CAI 协议以字节为单位，桥接层负责换算。
- **`shape`/`strides` 是运行时 `int32`**，而 `dtype`/`ndim` 是编译期常量——这决定了它们各自能用在哪里。
- **外部张量经 DLPack / CUDA Array Interface 零拷贝进入内核**：`classify_arg` 按 torch 快路径 → `__dlpack__` → `__cuda_array_interface__` 的优先级判定，三类最终归一为同一种内部 `Array`。
- **设备校验**：DLPack 路径会强制要求张量在 CUDA 设备上，CPU 数组会被拒绝。

## 7. 下一步学习建议

- 下一讲 [u2-l3 数据模型：Tile、Scalar 与形状广播](u2-l3-data-model-tile.md) 会讲 **tile**——即本讲数组被 `ct.load` 取出来后的那个「不可变、编译期形状为 2 的幂」的对象，以及零维 tile（scalar）和 NumPy 式广播。建议把它和本讲对照阅读：**数组是 host 的、可变的；tile 是 kernel 内的、不可变的**。
- 想了解 `dtype` 细节（含 `float8`/`bfloat16`/`tfloat32` 与类型提升）可接着看 [u2-l4 数据类型 DType 与类型提升](u2-l4-dtype-and-promotion.md)。
- 想深入 `ct.load`/`ct.store` 的 tile space 索引语义，可跳到 [u3-l1 load/store 与 load-compute-store 范式](u3-l1-load-store-pattern.md)。
- 对 C++ 桥接感兴趣的同学，可顺带阅读 [cext/tile_kernel.cpp](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/tile_kernel.cpp) 中 `classify_arg` 与 `arrayrepr_*` 系列函数，那是本讲「外部张量如何变成 Array」的全部真相。
