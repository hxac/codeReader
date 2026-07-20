# dtype、device 与 layout：张量的三大属性与 C++ TensorOptions

> 承接 [u2-l2](u2-l2-storage-and-memory-layout.md)：上一讲我们看清了一个 `Tensor` 由「数据（Storage）」与「视图（sizes/strides/storage_offset）」两部分拼成。
> 本讲回答下一个自然的问题：**这些数据按什么类型解释、放在哪台设备上、以什么方式组织**——也就是 `dtype`、`device`、`layout` 三大基础属性，以及 C++ 侧如何用一个 `TensorOptions` 把它们打包在一起。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `torch.dtype`、`torch.device`、`torch.layout` 这三个 Python 对象**到底是什么**，以及它们各自背后对应的 C++ 类型。
- 读懂设备字符串（如 `"cuda:1"`、`"cpu"`）在源码里是如何被解析成 `(类型, 索引)` 二元组的。
- 理解 C++ 的 `c10::TensorOptions` 如何用「全可选 + 默认值」的方式聚合这三轴，并能解释为什么它要被压到 128 位以内。
- 建立从 Python 关键字参数（`torch.zeros(2, 3, dtype=torch.int32, device="cuda")`）一路到 C++ `TensorOptions` 再到 `DispatchKey` 的最小心智模型，为 Unit 3 的 Dispatcher 分发做铺垫。

## 2. 前置知识

本讲假设你已经理解：

- **张量 = 数据 + 视图**（见 u2-l2）：数据由 Storage 持有，视图由 sizes/strides 描述。
- **Python 前端 + C++ 后端的分层**（见 u1-l3、u2-l1）：`torch.Tensor` 是对 C++ `TensorBase`（`torch._C.TensorBase`）的薄包装，真正的实现都在 C++。
- **算子最终要选一个 kernel 来执行**：同一个算子（如 `add`）在不同设备、不同精度下需要不同的底层实现。**选哪个 kernel** 这件事，正是由本讲的三轴共同决定的。

需要先建立的直觉（一句话）：

> 一个 `Tensor` 的「身份」，除了形状之外，主要由三个正交的属性决定——**元素类型**（dtype）、**所在设备**（device）、**内存布局类别**（layout）。它们组合起来，恰恰能决定这个张量该走哪一条计算路径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `torch/types.py` | Python 侧的类型别名，定义了「device-like」「number-like」等用于注解的复合类型。 |
| `torch/_tensor.py` | `Tensor` 的 Python 类；承接 u2-l1，**`dtype/device/layout` 三个属性并不在这里定义**，而是继承自 C++ `TensorBase`。 |
| `torch/headeronly/core/ScalarType.h` | C++ 枚举 `ScalarType`，是所有 dtype 的「单一事实来源」。 |
| `torch/headeronly/core/Layout.h` | C++ 枚举 `Layout`，列出所有内存布局类别。 |
| `c10/core/Device.h` / `c10/core/Device.cpp` | C++ `c10::Device` 结构体与设备字符串解析逻辑。 |
| `c10/core/TensorOptions.h` / `c10/core/TensorOptions.cpp` | C++ `TensorOptions` 聚合器，把 dtype/device/layout（以及 requires_grad、pinned_memory、memory_format）打包在一起。 |
| `torch/csrc/Dtype.cpp` / `torch/csrc/Device.cpp` / `torch/csrc/Layout.cpp` | 把上述 C++ 类型暴露成 Python 对象（`torch.dtype` / `torch.device` / `torch.layout`）的绑定层。 |
| `torch/__init__.py` | `set_default_dtype` / `set_default_device` 的 Python 实现。 |
| `torch/utils/_device.py` | `set_default_device` 背后真正干活的 `DeviceContext`。 |

## 4. 核心概念与源码讲解

本讲按「三轴逐个讲透 → 再看 C++ 如何聚合」的顺序，拆成四个最小模块。

### 4.1 dtype：张量的标量类型

#### 4.1.1 概念说明

`dtype`（data type）回答的问题是：**Storage 里那一块裸字节，应当按什么 C++ 类型来解释？**

同一块 4 字节内存，按 `torch.float32` 解释就是一个单精度浮点数，按 `torch.int32` 解释就是一个 32 位整数。dtype 本身不改变字节，只改变「解释方式」——这与 u2-l2 中「数据 vs 视图」的分离思想一脉相承。

在 Python 里，`torch.float32`、`torch.int64`、`torch.bool` 这些**不是类型，而是对象**：它们都是 `torch.dtype` 这个类的实例。`torch.dtype` 才是类型，`torch.float32` 是它的一个实例。

#### 4.1.2 核心流程

- C++ 层用一个枚举 `ScalarType` 列出所有支持的标量类型。
- Python 层的 `torch.dtype` 实例，本质是「一个 `ScalarType` 值 + 一个名字字符串」的包装。
- 当你不写 `dtype=` 时，框架会回退到一个「默认 dtype」（初始为 `torch.float32`）。

#### 4.1.3 源码精读

**`ScalarType` 枚举**通过一个宏列表生成，这是 PyTorch 管理海量类型的惯用手法：

[torch/headeronly/core/ScalarType.h:L103-L150](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/headeronly/core/ScalarType.h#L103-L150)

注释里的 `/* 0 */`、`/* 1 */` 就是枚举的整数值。常用的有 `Byte(0)`、`Char(1)`、`Short(2)`、`Int(3)`、`Long(4)`、`Half(5)`、`Float(6)`、`Double(7)`、`Bool(11)`、`BFloat16(15)`，后面还跟着各种量化类型、`Float8_*`、窄位宽 `UInt1..7`、`Int1..7` 等。真正声明枚举的代码很短：

[torch/headeronly/core/ScalarType.h:L259-L265](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/headeronly/core/ScalarType.h#L259-L265)

枚举底层是 `int8_t`，所以一个 dtype 在 C++ 里只占 1 个字节。

**Python 包装**：`torch.dtype` 实例由 `THPDtype_New` 创建，它持有两样东西——`scalar_type` 和 `name`：

[torch/csrc/Dtype.cpp:L11-L21](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Dtype.cpp#L11-L21)

`torch.float32`、`torch.int64` 等就是在 import 时对每个 `ScalarType` 调用一次这个工厂、并挂到 `torch` 命名空间上的。`THPDtype` 还提供 `is_floating_point`、`is_complex`、`is_signed`、`itemsize` 等方法（见同文件 L23-L66），它们都只是查询 C++ 的 `isFloatingType` 等函数。

**默认 dtype**：`set_default_dtype` 的 Python 实现最终调用的是 C++ 绑定：

[torch/__init__.py:L1764-L1814](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1764-L1814)

注意 L1774-L1782 的说明：默认 dtype **只影响浮点**（以及由它推导出的复数默认类型）；它决定了 `torch.tensor([1.2, 3])` 这种「由 Python float 推断」的张量用什么精度。配套的查询函数 `torch.get_default_dtype()` 同样是 C++ 绑定（`THPModule_getDefaultDtype`）：

[torch/_C/__init__.pyi.in:L1367-L1367](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/__init__.pyi.in#L1367-L1367)

#### 4.1.4 代码实践

**实践目标**：直观感受「dtype 决定字节如何被解释」，以及默认 dtype 的作用。

**操作步骤**（示例代码）：

```python
import torch

# 1) 同一段内存，不同 dtype 解释
a = torch.tensor([1, 2, 3], dtype=torch.int32)
b = a.view(torch.float32)   # 复用底层 Storage，换一种解释方式
print(a, a.dtype)           # 解释为整数
print(b, b.dtype)           # 同样的字节，被当成 float32（会是乱码）

# 2) 默认 dtype
print(torch.get_default_dtype())          # torch.float32
t = torch.tensor([1.2, 3])                # Python float -> 走默认 dtype
print(t.dtype)                            # torch.float32

torch.set_default_dtype(torch.float64)
print(torch.tensor([1.2, 3]).dtype)       # torch.float64
torch.set_default_dtype(torch.float32)    # 记得改回来，避免污染后续
```

**需要观察的现象**：

- `a` 与 `b` 的 `data_ptr()` 相同（共享 Storage，印证 u2-l2），但数值含义完全不同——`b` 多半是一堆看起来毫无意义的浮点数。
- 改默认 dtype 后，`torch.tensor([1.2, 3])` 的精度随之改变。

**预期结果**：`b` 的值取决于 int32 整数在内存中的位模式被重新按 IEEE 754 解读后的结果（典型表现为极大的或极小的浮点数）。这是「dtype = 解释方式」最直接的证据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `torch.bool` 的 `itemsize` 是 1 字节，而它逻辑上只需要 1 位？

**参考答案**：因为现代 CPU/GPU 的最小可寻址单位是字节。`ScalarType::Bool` 对应 C++ `bool`，`sizeof(bool) == 1`（见 `elementSize` 在 [ScalarType.h:L45-L56](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/ScalarType.h#L45-L56) 的实现）。想要真正按位打包，得用 `torch.bool` 的特殊存储或量化的 `Bits1x8` 类型。

**练习 2**：`torch.float16` 和 `torch.bfloat16` 都是 16 位，它们的主要区别在哪？

**参考答案**：位分配不同。`float16` 是 1 位符号 + 5 位指数 + 10 位尾数（精度高、动态范围小）；`bfloat16` 是 1 位符号 + 8 位指数 + 7 位尾数（动态范围与 float32 相同、精度低，训练更稳）。在 `ScalarType` 枚举里它们分别是 `Half(5)` 与 `BFloat16(15)`，是两个独立的值。

---

### 4.2 device：张量所在的设备

#### 4.2.1 概念说明

`device` 回答的问题是：**这块数据物理上放在哪里？**

CPU 是一个设备，每一张 GPU 也是独立的设备。不同设备上的张量不能直接做运算——必须先把数据搬过去（`tensor.to('cuda')`）。所以「张量在哪个设备」决定了它走 CPU kernel 还是 CUDA kernel。

在 Python 里，`torch.device` 是类型，`torch.device('cuda:0')` 是它的实例。它只有两个核心字段：**设备类型**（type，如 `cpu`/`cuda`）和**设备索引**（index，第几张卡）。

#### 4.2.2 核心流程

设备可以用三种方式指定：

1. **字符串**：`"cuda"`、`"cuda:0"`、`"cpu"`。
2. **`torch.device` 对象**：`torch.device('cuda', 0)`。
3. **整数**：`0`（解释为「当前加速器的第 0 张卡」）。

字符串解析遵循一个非正式正则：

\[ \text{device\_string} \;=\; (\text{type\_name})(?::(\text{index}))? \]

也就是「类型名」后面可选地跟一个 `:索引`。`"cuda:1"` 解析为 `(CUDA, 1)`，`"cpu"` 解析为 `(CPU, -1)`（`-1` 表示「当前设备」，是默认值）。

#### 4.2.3 源码精读

**C++ `c10::Device` 结构体**只有两个字段：

[c10/core/Device.h:L31-L46](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.h#L31-L46)

关键约束见 [c10/core/Device.h:L26-L30](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.h#L26-L30) 的注释：负索引表示「当前设备」，非负索引表示具体设备；而 CPU 的索引只能是 0（或 -1）。`DeviceIndex` 本身是 `int8_t`（[c10/core/Device.h:L19](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.h#L19)），所以设备类型 + 索引一共只占 2 字节。

**类型名查表**：解析 `"cuda"` 这样的类型名靠一张静态表：

[c10/core/Device.cpp:L13-L68](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.cpp#L13-L68)

可以看到 `cpu`、`cuda`、`ipu`、`xpu`、`hip`、`mps`、`meta`、`hpu`、`privateuse1` 等都在表里。注意 L44-L46：如果你的机器注册了自定义后端（`get_privateuse1_backend()`），那个名字也会被解析成 `PrivateUse1`——这就是 PyTorch 允许第三方设备接入的入口之一。

**字符串状态机解析**：`Device(const std::string&)` 用一个手写状态机去匹配上面那个正则：

[c10/core/Device.cpp:L73-L149](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.cpp#L73-L149)

状态机有四个状态 `START / INDEX_START / INDEX_REST / ERROR`（[c10/core/Device.cpp:L69](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.cpp#L69)）：先吃字母/下划线组成类型名，遇到 `:` 就转入吃数字（索引）。注意 L110-L113 拒绝前导零（`"cuda:01"` 非法），索引非法就抛 `Invalid device string` 错误。

**Python 包装**：`torch.device(...)` 构造由 `THPDevice_pynew` 处理，它接受两种重载——要么传一个 device，要么传 `(类型字符串, index)`：

[torch/csrc/Device.cpp:L46-L86](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Device.cpp#L46-L86)

它的 `repr` 就是你在交互环境里看到的 `device(type='cuda', index=0)`：

[torch/csrc/Device.cpp:L27-L38](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Device.cpp#L27-L38)

**Python 注解里的 `Device`**：注意 `torch/types.py` 里有一个**小写的 `Device` 类型别名**（不是 `torch.device`），它表示「任何能被当作设备的东西」：

[torch/types.py:L75-L75](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/types.py#L75-L75)

即 `torch.device | str | int | None`——这正是上面三种指定方式加上 `None`（表示用默认设备）的并集。文件顶部的 import 也印证了 `_device`、`_dtype`、`_layout` 都来自 `torch`（也就是来自 `torch._C`）：

[torch/types.py:L19-L30](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/types.py#L19-L30)

#### 4.2.4 代码实践

**实践目标**：验证设备字符串解析的几种形态，并观察 `set_default_device` 如何影响新张量。

**操作步骤**：

```python
import torch

# 1) 三种等价的指定方式
print(torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu"))
print(torch.device("cpu", 0))   # (type, index) 形式
# 以下在无 GPU 时可换成 cpu 相关对比
d = torch.device("cuda", 1) if torch.cuda.device_count() > 1 else torch.device("cpu")
print(d.type, d.index)

# 2) 非法字符串（取消注释观察报错信息）
# torch.device("cuda:01")   # Invalid device string: 前导零
# torch.device("cuda:-1")   # 索引部分不允许负号（状态机只吃数字）

# 3) 默认设备
torch.set_default_device("cpu")
x = torch.zeros(2, 3)         # 不传 device，走默认
print(x.device)
```

**需要观察的现象**：

- `torch.device("cuda:01")` 会抛出 `Invalid device string`——印证 [c10/core/Device.cpp:L110-L113](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.cpp#L110-L113) 的前导零检查。
- 若环境有 CUDA，`set_default_device('cuda')` 后 `torch.zeros(...)` 的 `device` 会变成 `cuda`（具体见 4.4 节对 `DeviceContext` 的解析）。

**预期结果**：CPU-only 环境下 `x.device` 为 `device(type='cpu')`；若你尝试 `torch.set_default_device('cuda')` 而机器没有 GPU，会在后续工厂函数真正分配时（而非 `set_default_device` 本身）报错。

> 若本机无 GPU：可只验证字符串解析与非法输入报错，`set_default_device` 部分记为「待本地验证（需 CUDA 环境）」。

#### 4.2.5 小练习与答案

**练习 1**：`torch.device("cuda")` 的 `index` 字段是多少？为什么是这个值？

**参考答案**：是 `-1`（见 [c10/core/Device.cpp:L73-L149](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.cpp#L73-L149)，没提供索引时 `index_` 保持初始 `-1`）。`-1` 表示「当前 CUDA 设备」，即 `torch.cuda.current_device()` 选中的那张卡——所以即便写了 `"cuda"`，实际落在哪张卡还取决于 `torch.cuda.set_device`。

**练习 2**：`is_cpu()` 为真时，`device.index` 的合法取值有哪些？

**参考答案**：只能是 `-1` 或 `0`（见 [c10/core/Device.h:L181-L184](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.h#L181-L184) 的断言）。因为 CPU 只有一个，不存在「第 N 张 CPU」的概念。

---

### 4.3 layout：张量的内存布局类别

#### 4.3.1 概念说明

`layout` 回答的问题是：**数据在内存里以什么结构存放？**

绝大多数张量是 `torch.strided`（稠密、按 stride 步长索引，正是 u2-l2 讲的那套 `offset = storage_offset + Σ i·stride`）。但也有稀疏存储——只存非零元素及其下标，比如 `torch.sparse_coo`、`torch.sparse_csr`。不同 layout 走完全不同的算子实现。

和 dtype 一样，`torch.layout` 是类型，`torch.strided`、`torch.sparse_coo` 是它的实例。

#### 4.3.2 核心流程

- C++ 用 `c10::Layout` 枚举列出所有布局类别。
- 默认 layout 是 `Strided`。
- 一个 `Tensor` 的 layout 决定了它会被分发到「稠密 kernel」还是「稀疏 kernel」（详见 4.4 节的 `computeDispatchKey`）。

#### 4.3.3 源码精读

**`Layout` 枚举**：

[torch/headeronly/core/Layout.h:L11-L21](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/headeronly/core/Layout.h#L11-L21)

成员有 `Strided`、`Sparse`、`SparseCsr`、`Mkldnn`、`SparseCsc`、`SparseBsr`、`SparseBsc`、`Jagged`。底层同样是 `int8_t`。打印名见 [c10/core/Layout.h:L38-L60](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Layout.h#L38-L60)。

**Python 包装**：`torch.layout` 实例由 `THPLayout_New` 创建：

[torch/csrc/Layout.cpp:L10-L10](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Layout.cpp#L10-L10)

它和 dtype/device 的套路完全一致：持有一个 C++ 枚举值 + 名字。

**常见误区提醒**：`layout` 描述的是「存储结构类别」，**不要和 `stride`（步长）混淆**。`stride` 是稠密布局（`torch.strided`）下的一个具体数值数组（见 u2-l2），而 `layout` 是更高层的选择：「用稠密方式存还是稀疏方式存」。只有 `torch.strided` 的张量才有有意义的 `stride`。

#### 4.3.4 代码实践

**实践目标**：创建稠密与稀疏张量，对比它们的 layout 与 storage 大小。

**操作步骤**：

```python
import torch

# 稠密
dense = torch.zeros(100, 100)
print(dense.layout, dense.element_size() * dense.numel(), "bytes")

# 稀疏 COO：只存两个非零元素
indices = torch.tensor([[0, 5], [10, 20]])
values = torch.tensor([3.0, 7.0])
sparse = torch.sparse_coo_tensor(indices, values, size=(100, 100))
print(sparse.layout)            # torch.sparse_coo
print(sparse.is_sparse)
```

**需要观察的现象**：

- 稠密张量占满 `100×100×4 = 40000` 字节；稀疏张量只存了 2 个值和它们的位置，存储占用远小于稠密。
- `dense.layout` 是 `torch.strided`，`sparse.layout` 是 `torch.sparse_coo`。

**预期结果**：稀疏张量的 `is_sparse` 为 `True`，且它没有传统意义上的稠密 `stride`（访问 `.stride()` 会因 layout 不同而行为不同，`torch.sparse_coo` 的张量不支持普通 `stride()`）。

#### 4.3.5 小练习与答案

**练习 1**：一个 `torch.strided` 的张量，它的 `layout` 属性和它的 `stride` 属性是什么关系？

**参考答案**：`layout == torch.strided` 说明「这个张量用稠密 + 步长」的方式存储；而 `tensor.stride()` 是这种方式下的具体步长数值（每维跨多少元素）。前者是模式选择，后者是该模式下的参数。

**练习 2**：为什么 `SparseCsr`/`SparseCsc`/`SparseBsr`/`SparseBsc` 要分成四个 layout，而不是合并成一个 `Sparse`？

**参考答案**：它们底层数据结构不同：CSR（压缩稀疏行）、CSC（压缩稀疏列）、BSR（分块稀疏行）、BSC（分块稀疏列）各自有不同的索引数组布局与适用场景。在 `computeDispatchKey` 里它们对应不同的 `SparseCsr*` 分支（见 4.4.3），需要分别匹配对应的 kernel。

---

### 4.4 TensorOptions：C++ 层的「构造轴」聚合器

#### 4.4.1 概念说明

前三节讲了三个独立的属性。在 C++ 里，PyTorch 用一个结构体 `c10::TensorOptions` 把它们（以及 `requires_grad`、`pinned_memory`、`memory_format`）打包在一起，作为**所有工厂函数**（`at::empty`、`at::zeros`、`tensor.to(...)` 等）统一的「配置参数包」。

它的设计哲学见源码注释：

[c10/core/TensorOptions.h:L49-L75](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L49-L75)

关键点：**TensorOptions 里的每一项都是可选的（optional）**。你只写 `dtype=` 也行，只写 `device=` 也行，没写的项在真正需要时才填入默认值。这正好对应 Python 里 `torch.zeros(2, 3, dtype=torch.int32)` 这种「想写哪个关键字就写哪个」的体验。

#### 4.4.2 核心流程

TensorOptions 的运作分三步：

1. **构造**：可以「整体默认」`TensorOptions()`，也可以从单个轴隐式构造——传一个 `Device`、`ScalarType` 或 `Layout` 都会自动变成一个只设了那一轴的 TensorOptions。
2. **链式修改**：`.device(...)`、`.dtype(...)`、`.layout(...)` 等**返回副本**（不可变，builder 风格），所以可以 `TensorOptions().device(kCUDA).dtype(kInt)` 链式调用。
3. **取值带默认**：读 `.device()` 时，若该项没设过，就用「默认值」补上——device 默认 CPU、layout 默认 Strided、dtype 默认「当前默认 dtype」。

最后，`TensorOptions` 把这三轴折算成一个 `DispatchKey`，用来选 kernel——这就是它通向 Unit 3 的桥梁。

#### 4.4.3 源码精读

**结构体定义与字段**：

[c10/core/TensorOptions.h:L136-L138](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L136-L138)

真正的成员变量在文件末尾，注意它们的**初始默认值**与「紧凑位存储」：

[c10/core/TensorOptions.h:L540-L556](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L540-L556)

`device_` 默认 `kCPU`、`dtype_` 默认 `float`、`layout_` 默认 `kStrided`。注意它同时存了「值」和「是否被显式设置过」（`has_device_` 等位域）——这两套信息都必要：值用于直接读取，`has_*_` 用于判断要不要走默认逻辑。整个结构被压到 128 位以内：

[c10/core/TensorOptions.h:L562-L564](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L562-L564)

这是有意的性能优化——`TensorOptions` 在热路径上按值传递，越小越好。

**默认值供应函数**：读取时若没设过，用这几个内联函数补默认：

[c10/core/TensorOptions.h:L28-L47](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L28-L47)

注意 `dtype_or_default` 调的是 `get_default_dtype_as_scalartype()`——也就是说「TensorOptions 里没指定 dtype」最终会落到 `torch.get_default_dtype()` 设的那个全局默认值上，把本讲 4.1 节和 C++ 串了起来。

**隐式构造**：传一个 `Device`/`Layout`/`ScalarType`/`TypeMeta`/`MemoryFormat` 都能隐式构造出一个 TensorOptions：

[c10/core/TensorOptions.h:L141-L181](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L141-L181)

所以 C++ 里 `at::empty({2,2}, at::kCUDA)`、`at::empty({2,2}, at::kInt)` 都能工作——`kCUDA`/`kInt` 被隐式提升成 TensorOptions。

**不可变 builder**：以 `device` 为例：

[c10/core/TensorOptions.h:L185-L199](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L185-L199)

先拷贝一份 `*this` 再改副本，返回新对象，因此可以安全链式调用而不影响原对象。

**右偏合并 merge_in**：把两个 TensorOptions 合并时，**右侧（参数）中显式设置的字段覆盖左侧**：

[c10/core/TensorOptions.h:L408-L424](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L408-L424)

只看「有没有显式设过（`has_*_`）」，没设过的字段不覆盖——这正是 `tensor.to(dtype=torch.float16)` 这种「只改一项、其余继承自原 tensor」语义的实现基础。

**打印**：`operator<<` 把内部状态和「取默认值后的实际值」都打出来（标注 `(default)`）：

[c10/core/TensorOptions.cpp:L14-L39](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.cpp#L14-L39)

这对调试非常有用：你能同时看到「用户到底指定了什么」和「最终生效的是什么」。

**通向 DispatchKey**：TensorOptions 最终被折算成一个 `DispatchKey`——这是选 kernel 的关键：

[c10/core/TensorOptions.h:L440-L443](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L440-L443)

真正的映射逻辑是那个自由函数，它就是一张「layout × device × (dtype 是否量化)」的三维查表：

[c10/core/TensorOptions.h:L624-L719](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L624-L719)

例如 `Strided + CUDA + 非量化 dtype` → `DispatchKey::CUDA`；`Sparse + CPU` → `DispatchKey::SparseCPU`；量化类型会把前缀换成 `Quantized*`。**这就是为什么本讲反复强调「三轴决定计算路径」**——它们组合出的 DispatchKey，正是 Unit 3 要讲的 Dispatcher 用来选 kernel 的那个键。

#### 4.4.4 代码实践：默认 device 是怎么生效的

**实践目标**：搞清 `set_default_device` 在源码层面到底做了什么，并验证它只影响「没显式传 device」的工厂函数。

`set_default_device` 的 Python 实现并不直接改 C++ 的某个全局变量，而是装一个 `DeviceContext`：

[torch/__init__.py:L1669-L1730](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1669-L1730)

`DeviceContext` 是一个 `TorchFunctionMode`，它会拦截 `torch.empty`/`torch.zeros`/`torch.ones` 等工厂函数，**在这些函数没收到显式 `device=` 时，替它补上默认设备**：

[torch/utils/_device.py:L68-L122](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/utils/_device.py#L68-L122)

关键看 L118-L122 的 `__torch_function__`：只有当 `func` 在 `_device_constructors()` 名单里、且调用者没传 `device` 时，才注入。这也解释了 `set_default_device` 的 docstring 里的那句「不影响 `torch.from_numpy` / `torch.frombuffer`」——它们不在名单里（见 L13-L64）。

**操作步骤**：

```python
import torch

prev = torch.get_default_device()
print("default device =", prev)

# 临时切换（CPU 上演示；有 GPU 可换成 'cuda'）
with torch.device("cpu"):
    a = torch.zeros(2, 3)          # 没传 device -> 被注入
    b = torch.zeros(2, 3, device="cpu")  # 显式传了 -> 不受影响
    print(a.device, b.device)

# 三轴一起打印（TensorOptions 是 C++ 概念，Python 侧用三属性重建）
t = torch.zeros((2, 2), dtype=torch.int32)
print(t.dtype, t.device, t.layout)
```

**需要观察的现象**：

- `with torch.device(...)` 退出后，默认设备恢复原值（`DeviceContext.__exit__` 会把栈恢复）。
- `t.dtype / t.device / t.layout` 三属性对应 C++ `TensorOptions` 的三轴——**Python 的 `Tensor` 并不直接暴露 `.options()`**，但你可以从这三个 getter 把它重建出来。

**预期结果**：CPU 环境下 `a.device` 与 `b.device` 都是 `device(type='cpu')`；若把上面换成 `with torch.device("cuda")` 且有 GPU，则 `a` 落在 `cuda`、`b` 保持你显式指定的设备。

> C++ 侧确有 `Tensor::options()`（其维护要求见 [c10/core/TensorOptions.h:L528-L535](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L528-L535) 的注释），但 Python `torch.Tensor` 并未把该方法暴露为公开 API，因此 Python 用户只能通过 `dtype`/`device`/`layout` 三个属性分别读取。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TensorOptions` 要同时保存「值」和「是否被显式设置过」两套信息？只存值不行吗？

**参考答案**：因为「没指定」和「指定成默认值」语义不同。比如用户没传 `device`，应当走「默认设备」（可能是 CPU，也可能被 `set_default_device` 改成 CUDA）；如果只存值且默认填 CPU，就会把「使用默认设备」和「明确要用 CPU」混为一谈，导致 `set_default_device` 失效。`has_*_` 位域正是为了区分这两种情况，`merge_in`（[c10/core/TensorOptions.h:L408-L424](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L408-L424)）也依赖它来决定要不要覆盖。

**练习 2**：`computeDispatchKey(Strided, CUDA, float)` 和 `computeDispatchKey(Sparse, CPU, float)` 分别返回什么？

**参考答案**：前者返回 `DispatchKey::CUDA`（见 [c10/core/TensorOptions.h:L631-L672](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L631-L672) 的 `Strided` 分支 + `CUDA` device case），后者返回 `DispatchKey::SparseCPU`（见 L673-L686 的 `Sparse` 分支）。同一个「乘法」算子，对这两个张量会落到完全不同的 kernel 实现——这正是 Unit 3 要展开的主题。

## 5. 综合实践

**任务**：用一段脚本把本讲三轴串起来，回答「为什么 dtype/device/layout 三者共同决定了一个张量的计算身份」。

```python
import torch

def describe(name, t):
    print(f"{name}: dtype={t.dtype}, device={t.device}, layout={t.layout}")

# 1) 默认三轴
a = torch.tensor([1.0, 2.0, 3.0])
describe("a", a)   # float32 / cpu / strided

# 2) 只改 dtype：同数据、不同解释
b = a.to(torch.float16);        describe("b", b)   # half / cpu / strided

# 3) 只改 layout：稠密 -> 稀疏
idx = torch.tensor([[0, 1]])
val = torch.tensor([1.0, 2.0])
s = torch.sparse_coo_tensor(idx, val, size=(4,));  describe("s", s)

# 4) 只改 device（若有 GPU）
if torch.cuda.is_available():
    c = a.to("cuda");            describe("c", c)   # float32 / cuda / strided
else:
    print("无 CUDA，跳过设备迁移示例（待本地验证）")

# 5) 临时默认设备 + 默认 dtype
with torch.device("cpu"):
    torch.set_default_dtype(torch.float64)
    d = torch.zeros(2)           # dtype 走默认(float64)，device 走默认(cpu)
    describe("d", d)
    torch.set_default_dtype(torch.float32)
```

**完成后请回答**：

1. `a`、`b`、`c` 三者的 `data_ptr` 关系是什么？谁的 `dtype` 变了？谁的 `device` 变了？（提示：`to` 换 dtype/device 时会拷贝，参考 u2-l2 的「拷贝 vs 视图」。）
2. `s.is_sparse` 为何是 `True`？它的 layout 与前三者有何不同？
3. `d` 的 dtype 为什么是 `float64`？请用 [c10/core/TensorOptions.h:L28-L47](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorOptions.h#L28-L47) 解释「没传 dtype」是如何落到 `get_default_dtype` 的。

**预期结果**：你会直观看到「同一组数值」可以以不同 dtype、不同 device、不同 layout 存在，且这三轴的组合最终决定了算子分发到哪条计算路径——这正是下一讲（Unit 3）Dispatcher 的起点。

## 6. 本讲小结

- `dtype` / `device` / `layout` 是张量的三大正交属性，分别决定「字节如何解释」「数据放在哪」「以什么结构存储」。Python 里 `torch.dtype` / `torch.device` / `torch.layout` 都是 C++ 类型的薄包装，对应 `THPDtype` / `THPDevice` / `THPLayout`。
- `dtype` 的单一事实来源是 C++ `ScalarType` 枚举（[ScalarType.h:L259-L265](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/headeronly/core/ScalarType.h#L259-L265)）；默认 dtype 是 `torch.float32`，可被 `set_default_dtype` 修改。
- `device` 由 `(类型, 索引)` 二元组构成，设备字符串由 [Device.cpp:L73-L149](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/Device.cpp#L73-L149) 的状态机解析；Python 的「device-like」类型别名见 [types.py:L75](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/types.py#L75)。
- `layout` 由 `c10::Layout` 枚举列出（[Layout.h:L11-L21](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/headeronly/core/Layout.h#L11-L21)），默认 `Strided`；注意它与「stride 步长」是两个概念。
- C++ 用 `c10::TensorOptions` 把三轴（加 requires_grad/pinned_memory/memory_format）打包成全可选、按值传递、压在 128 位内的配置包；读取时未指定项走 `*_or_default` 默认值。
- `TensorOptions.computeDispatchKey` 把三轴折算成一个 `DispatchKey`——这是本讲通向 Unit 3 Dispatcher 的桥梁。
- Python 的 `Tensor.dtype/device/layout` 三个属性继承自 C++ `TensorBase`（见 u2-l1），并不在 `_tensor.py` 里定义；`set_default_device` 的真正实现是 `torch/utils/_device.py` 里的 `DeviceContext`。

## 7. 下一步学习建议

- **立即衔接 Unit 3**：本讲结尾的 `computeDispatchKey` 直接引出 [u3-l3 DispatchKey 与 Dispatcher 分发机制](u3-l3-dispatchkey-and-dispatcher.md)。建议先读 `c10/core/DispatchKey.h`，把 CPU/CUDA/Sparse*/Autograd* 等 key 的分组和本讲的「三轴 → DispatchKey」对应起来。
- **回顾 u2-l1 / u2-l2**：本讲多次用到「Python 属性继承自 C++ TensorBase」「数据 vs 视图」这两个结论，若感觉模糊可重读这两篇。
- **进阶线索**：想深入 dtype 的类型提升规则，可读 [ScalarType.h:L264-L289](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/ScalarType.h#L264-L289) 的 `canCast` 与 `promoteTypes`；想了解 `memory_format`（本讲略过的第四轴），可在 `c10/core/MemoryFormat.h` 继续探索，它会与 u2-l2 的 `contiguous` 概念汇合。
