# u2-l1 Tensor 对象与张量创建

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pypto.Tensor` 是「计算图中的数据描述符」而不是一块 host 内存，并解释它的 `shape` / `dtype` / `format` / `dim` / `name` 属性分别从哪里读出来。
2. 手工构造一个 `pypto.Tensor`（或等价的 `pypto.tensor(...)`），并区分静态 shape、动态 shape（`-1`）与 `SymbolicScalar` 三种维度写法。
3. 解释 `Tensor[...]` 类型注解的语法（`pypto.Tensor[[...], pypto.DT_FP16]`）在 jit 调用时如何驱动框架自动完成 torch → pypto 的转换。
4. 掌握三类张量创建入口：显式构造（`pypto.tensor` / `pypto.Tensor`）、算子创建（`pypto.zeros` / `pypto.ones` / `pypto.full` / `pypto.arange`）、外部转换（`pypto.from_torch`）。
5. 描述数据如何在 host（torch 张量）与 device（NPU）之间「按指针零拷贝」流动。

本讲是第 2 单元（Python 前端编程基础）的第一课，承接 u1-l2 中「hello_world 三要素」（jit 装饰器、类型标注、`out[:]` 写回）里的**类型标注**与**数据**两条线索，把 Tensor 这个对象彻底讲透。

## 2. 前置知识

### 2.1 Tensor 在 PyPTO 里是「图纸」而不是「仓库」

torch 的 Tensor 是一块真正存放数据的内存；PyPTO 的 Tensor 更像一张**图纸**：它只描述「将要在 NPU 上参与计算的多维数组长什么样」——形状、数据类型、排布格式、名字，以及（运行期才知道的）数据指针。官方文档明确说明：

> Tensor 在执行时才包含实际值，未初始化的 Tensor 中的值都是随机的，在执行时需要按需初始化。
> —— [docs/zh/tutorials/development/tensor_creation.md:1-5](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_creation.md#L1-L5)

这就是为什么你在 kernel 里写 `pypto.full(...)` 得到的 Tensor 可以立刻参与表达式运算——它只是往计算图里追加了一个「常量填充」节点，真正的数值在设备执行时才产生。

### 2.2 需要认识的几个类型

| 类型 | 所在 | 一句话职责 |
| --- | --- | --- |
| `pypto.Tensor` | `python/pypto/tensor.py` | 面向用户的 Tensor 门面类，内部持有 C++ 的 `_base` |
| `pypto_impl.Tensor` | C++ 绑层（`libtile_fwk_*.so`，见 u1-l3） | 真正进入 IR 的张量对象 |
| `TensorAnnotation` | `python/pypto/tensor.py` | `Tensor[...]` 下标语法产生的「注解对象」，只存元数据 |
| `DataType` / `TileOpFormat` | C++ 枚举，Python 侧转发 | 数据类型（`DT_FP16` 等）与排布格式（`TILEOP_ND` / `TILEOP_NZ`） |
| `SymbolicScalar` | `python/pypto/symbolic_scalar.py` | 符号化标量，表示运行期才确定的数值（动态维度的基础，详见 u2-l6） |

枚举的转发关系可以看 [python/pypto/enum.py:22-23](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/enum.py#L22-L23)（`DataType = pypto_impl.DataType` 等）以及常用别名列表 [python/pypto/enum.py:55-80](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/enum.py#L55-L80)（`DT_FP16`、`DT_FP32`、`DT_BF16`……）。

### 2.3 术语：零拷贝（zero-copy）

「零拷贝」指转换双方**共享同一块内存**而不是复制一份数据。判断标准很简单：比较两个对象的 `data_ptr()`（数据首地址）是否相等。后面我们会从源码验证 `from_torch` 是零拷贝的。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [python/pypto/tensor.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py) | `Tensor` 与 `TensorAnnotation` 类定义 | 构造函数、属性、`__class_getitem__`、`move` |
| [python/pypto/op/creation.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/creation.py) | 创建类算子 | `zeros` / `ones` / `full` / `arange` |
| [python/pypto/converter.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/converter.py) | torch ↔ pypto 转换器 | `from_torch`、dtype 映射表、NZ 对齐 |
| [python/pypto/frontend/parser/entry.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py) | jit 入口（u3-l1 会精读） | 调用时如何用注解元数据做自动转换 |
| [python/pypto/runtime.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/runtime.py) | 运行时辅助 | `_pto_to_tensor_data`（执行期真正用 data_ptr 的地方） |
| [python/pypto/__init__.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py) | 包导出门面 | `tensor = Tensor` 别名、`from_torch` 导出 |
| [examples/01_beginner/basic/basic_ops.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py) | 初级示例 | `Tensor[[...]]` 注解 + 直接传 torch 张量 |
| [examples/02_intermediate/basic_nn/ffn/ffn_module.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/02_intermediate/basic_nn/ffn/ffn_module.py) | FFN 模块示例 | kernel 内用 `pypto.full` 造常量 |
| [docs/zh/tutorials/development/tensor_creation.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_creation.md) | 官方教程 | 创建方式与属性查看的权威说明 |

## 4. 核心概念与源码讲解

### 4.1 Tensor 对象：一张带指针的图纸

#### 4.1.1 概念说明

`pypto.Tensor` 是 Python 侧的**门面类**（facade，见 u1-l3 的「门面文件」概念）：它自己几乎不存数据，只做三件事：

1. 把构造参数规范化（shape 归一化、dtype/format 补默认值）；
2. 用这些参数创建 C++ 对象 `pypto_impl.Tensor` 存进 `self._base`；
3. 把算子调用（`+`、`sum`、`matmul`……）转发给 `pypto` 的算子库，把属性查询（`shape`、`dtype`……）转发给 `_base`。

额外携带的 `data_ptr`（数据地址）和 `device`（所在设备）是给运行时用的：编译期只看图纸，执行期才按地址搬运真实数据。

#### 4.1.2 核心流程

构造一个 `Tensor` 的流程：

```text
Tensor(shape, dtype, name, format, data_ptr, device, ori_shape)
  │
  ├─ 1. 记录 explicit_dtype / explicit_format（是否被显式指定，后面 jit 转换要用）
  ├─ 2. dtype 缺省 → DT_FP32；format 缺省 → TILEOP_ND
  ├─ 3. shape 归一化：
  │      全 int          → 静态 shape，直接 list(shape)
  │      含 SymbolicScalar → to_syms() 转符号列表
  │      StatusType 混合列表（如 [pypto.DYNAMIC, 32]）→ status_shape（动态 shape 语法）
  ├─ 4. self._base = pypto_impl.Tensor(dtype, shape, name, format)   ← 进入 C++ IR 世界
  └─ 5. data_ptr / device / ori_shape 存在 Python 侧，供运行期使用
```

其中元素总数满足：

\[ \text{numel} = \prod_{i=0}^{d-1} s_i \]

但注意 PyPTO 的 Tensor 不需要你算 numel——tiling（u2-l4）会按 tile 重新切分整个形状空间。

#### 4.1.3 源码精读

**构造函数与 shape 归一化**（[python/pypto/tensor.py:61-95](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L61-L95)）：这段先保存 `explicit_dtype` / `explicit_format` 标记（74-76 行），给 dtype / format 补默认值（78-79 行），然后分三种情况归一化 shape（82-92 行）：全整数、`StatusType` 状态列表（进 `status_shape`）、其他交给 `to_syms` 转成符号列表；最后 93 行 `self._base = pypto_impl.Tensor(ndtype, nshape, name, nformat)` 把图纸登记进 C++ IR，94-95 行把 `data_ptr` / `device` 留在 Python 侧。

**属性全部转发给 `_base`**（[python/pypto/tensor.py:440-484](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L440-L484)）：

- `dtype` → `self._base.GetDataType()`（440-442 行）
- `shape`（444-457 行）：若存在 `status_shape` 直接返回；否则逐维读取 `_base.GetShape()`，遇到 `-1`（动态维度标记）就换成 `pypto_impl.GetInputShape(...)` 运行期查询——这正是文档里 `print(tensor.shape)` 打出 `[SymbolicScalar(...), 32]` 的原因（见 [docs/zh/tutorials/development/tensor_creation.md:86-94](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_creation.md#L86-L94)）。
- `dim`（466-468 行）、`format`（474-476 行）、`name` 及其 setter（478-484 行）。

**`pypto.tensor` 只是类别名**（[python/pypto/__init__.py:48-51](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L48-L51)）：`tensor = Tensor`，所以文档里的 `pypto.tensor([2, 3], pypto.DT_FP16, "my_tensor")`（[docs/zh/tutorials/development/tensor_creation.md:11-14](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_creation.md#L11-L14)）就是直接调用 `Tensor.__init__`。

**`out[:] = value` 的落点——`move`**（[python/pypto/tensor.py:154-211](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L154-L211) 与 [python/pypto/tensor.py:642-646](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L642-L646)）：`__setitem__` 开头判断「全切片」（209-211 行），是则直接 `self.move(value)`；而 `move` 只是 `self._base.Move(other._base)`——把另一张图纸的计算结果「搬」到本 Tensor 名下。这解释了 u1-l2 讲过的现象：kernel 没有返回值，结果通过 `out[:]` 写回。

**与 C++ 对象互转的两个类方法**（[python/pypto/tensor.py:613-621](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L613-L621)）：`from_base` / `from_logical_tensor` 让算子包装层能把 C++ 返回的张量重新包回 Python `Tensor`（下一节的 `op_wrapper` 会用到）。

#### 4.1.4 代码实践：属性体检

1. **实践目标**：确认「Tensor 是图纸」——只构造、不执行，就能读到全部属性。
2. **操作步骤**：在安装好 pypto 的环境里运行下面脚本（示例代码，仿照官方文档 [docs/zh/tutorials/development/tensor_creation.md:64-82](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_creation.md#L64-L82) 编写）：

   ```python
   # 文件名：tensor_props.py（示例代码，仅构造 IR，不需要真机）
   import pypto

   t = pypto.tensor([2, 3, 4], pypto.DT_FP16, "example")
   print("shape :", t.shape)    # 预期 [2, 3, 4]
   print("dtype :", t.dtype)    # 预期 DataType.DT_FP16
   print("dim   :", t.dim)      # 预期 3
   print("format:", t.format)   # 预期 TileOpFormat.TILEOP_ND
   print("name  :", t.name)     # 预期 example
   t.name = "renamed"
   print("name  :", t.name)     # 预期 renamed

   dyn = pypto.tensor([-1, 32], pypto.DT_FP16, "dynamic")
   print("dynamic shape:", dyn.shape)  # 预期 [SymbolicScalar(...), 32]
   ```

3. **需要观察的现象**：构造过程没有分配任何数据内存、没有报错；动态维度的 `shape[0]` 是 `SymbolicScalar` 而不是整数。
4. **预期结果**：输出与注释一致；`dyn.shape` 的打印形态与文档 [docs/zh/tutorials/development/tensor_creation.md:91-94](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_creation.md#L91-L94) 相同。具体打印文本「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pypto.Tensor` 把 `data_ptr` 存在 Python 侧而不放进 `_base`？

**答案**：`_base` 是进入 IR / 计算图的描述符，编译期只需要 shape / dtype / format / name；`data_ptr` 是运行期属性，只在启动算子搬运数据时使用（见 4.4.3 中 `_pto_to_tensor_data` 的用法）。图纸与货物分离，同一份图纸可以配上不同地址的数据复用编译产物。

**练习 2**：`pypto.tensor([2, 3], pypto.DT_FP16)` 和 `pypto.Tensor([2, 3], pypto.DT_FP16)` 有区别吗？

**答案**：没有。`__init__.py` 第 49 行 `tensor = Tensor` 是类别名，两者调用同一个构造函数。

**练习 3**：如果构造时 `shape=[-1, 32]`，`t.shape[0]` 在编译期读到什么？

**答案**：一个 `SymbolicScalar`。`shape` 属性（tensor.py 449-457 行）发现该维是 `-1` 时返回 `pypto_impl.GetInputShape(self._base, i)` 的运行期查询符号；具体数值要等执行期由真实输入决定（动态 shape 详见 u2-l6）。

### 4.2 Tensor[...] 类型注解：jit 签名的「元数据载体」

#### 4.2.1 概念说明

在 u1-l2 的 hello_world 里我们写过：

```python
def add_kernel(
    a: pypto.Tensor[[...], pypto.DT_FP16],
    b: pypto.Tensor[[...], pypto.DT_FP16],
    out: pypto.Tensor[[...], pypto.DT_FP16],
):
```

`pypto.Tensor[[...], pypto.DT_FP16]` 不是普通的 Python 类型标注，而是触发了 `Tensor.__class_getitem__`（Python 的下标类语法，类似 `list[int]`），返回一个 `TensorAnnotation` 对象。它携带的信息会在**你调用算子时**被框架读取，用来指导 torch → pypto 的自动转换：这一维是不是动态的、dtype 是否被显式指定、张量叫什么名字。

#### 4.2.2 核心流程

```text
写签名:  a: pypto.Tensor[[pypto.DYNAMIC, pypto.DYNAMIC], pypto.DT_FP16]
              │ (类下标语法)
              ▼
Tensor.__class_getitem__ 解析参数元组 → TensorAnnotation(shape, dtype, ...)
              │ (编译期签名缓存，to_tensor() 可升级成 Tensor)
              ▼
调用算子: add_kernel(a_torch, b_torch, out_torch)   ← 传的是 torch.Tensor
              │ entry.py 按位置取前 N 个张量参数
              ▼
_convert_tensors_with_metadata:
      · 从注解 shape 里挑出 DYNAMIC / SymbolicScalar 维 → dynamic_axis
      · 用 tensor_def.explicit_dtype / explicit_format 覆盖 torch 自身 dtype / 格式
      · pypto.from_torch(torch_tensor, name=..., dynamic_axis=..., dtype=..., tensor_format=...)
              ▼
得到参与编译的 pypto.Tensor 列表
```

#### 4.2.3 源码精读

**`__class_getitem__`：解析下标参数**（[python/pypto/tensor.py:97-152](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L97-L152)）：119-120 行先把单个参数包成元组；130-150 行按类型分流——`list/tuple` 当 shape、`DataType` 当 dtype、`TileOpFormat` 当 format、`str` 当名字、`dict` 可一次传全所有字段；152 行返回 `TensorAnnotation`。docstring（101-107 行）给出了官方示例：`pypto.Tensor[[pypto.STATIC, pypto.STATIC], pypto.DT_INT32]`、`pypto.Tensor[[pypto.STATIC, ...], pypto.DT_INT32]`（`...` 表示任意剩余维度）、以及带 `pypto.TileOpFormat.TILEOP_ND` 的三参数写法，并明确 `Tensor[]` 空参数不支持。

**`TensorAnnotation` 与 `to_tensor`**（[python/pypto/tensor.py:31-58](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L31-L58)）：注解对象只存 7 个元数据字段；`to_tensor()` 把它升级成一个真正的 `Tensor`，而 `Tensor.__init__` 恰好会记录 `explicit_dtype` / `explicit_format`（74-76 行）——这两个标记就是给下一步的自动转换用的。`Tensor` 自己也有一个同名 `to_tensor()`（623-627 行）直接返回自身，让两条路径对外接口统一。

**StatusType 三个关键字**（[python/pypto/enum.py:85-93](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/enum.py#L85-L93)）：`STATIC` / `DYN` / `DYNAMIC` 是 Python 侧自定义枚举，`DYNAMIC` 与 `DYN` 等价，都表示「这一维运行期才知道」。示例 [examples/01_beginner/basic/basic_ops.py:103-114](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L103-L114) 的 `dynamic_add_kernel` 用的就是 `pypto.Tensor[[pypto.DYNAMIC, pypto.DYNAMIC], pypto.DT_FP16]`，注释还说明了静态维与动态维可以混用。

**调用期的自动转换**（[python/pypto/frontend/parser/entry.py:409-429](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L409-L429)）：`_convert_tensors_with_metadata` 逐个 zip「torch 张量 × 签名张量定义」，414-418 行把注解 shape 里是 `SymbolicScalar` 或 `DYN/DYNAMIC` 的维号收集成 `dynamic_axis`；419-427 行调用 `pypto.from_torch(...)`，并把 `tensor_def.explicit_dtype`、`tensor_def.explicit_format` 传进去——424 行注释写明：注解里给了 dtype 就优先用注解的，否则回退到 torch 张量自身 dtype。

**一个重要事实：调用入口只收 torch.Tensor**（[python/pypto/frontend/parser/entry.py:317-320](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L317-L320) 与 [python/pypto/frontend/parser/entry.py:352-359](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L352-L359)）：`__call__` 先 `_validate_exact_torch_tensors` 校验前 N 个参数必须是 `torch.Tensor`，否则抛 `FeError`。所以日常用法是「签名写 pypto 注解、调用传 torch 张量」，转换由框架在内部完成。而 `compile()` 的文档（[python/pypto/frontend/parser/entry.py:473-493](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L473-L493)）说明编译入口同时接受 `list[pypto.Tensor]`（`tensor_defs=None` 时）与 torch 列表两条路径——这属于前端内部细节，u3-l1 再展开。

#### 4.2.4 代码实践：注解驱动的转换

1. **实践目标**：亲眼看到「注解里的 DYNAMIC 变成了转换时的 dynamic_axis」。
2. **操作步骤**：
   1. 打开 [examples/01_beginner/basic/basic_ops.py:26-33](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L26-L33)，确认 `add_kernel` 的三个参数都标注 `pypto.Tensor[[...], pypto.DT_FP16]`（`...` 表示全部维度交给运行期推断）。
   2. 运行 `python examples/01_beginner/basic/basic_ops.py -m sim -t add`（SIM 模式，无需真机；用法见 u1-l4）。
   3. 对照 [python/pypto/frontend/parser/entry.py:414-418](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L414-L418) 读一遍：`[...]` 里没有 `DYNAMIC`，所以 `dynamic_axis` 为空列表，转换时传 `None`。
   4. 再看同文件 `dynamic_add_kernel`（103-114 行）把注解换成 `[pypto.DYNAMIC, pypto.DYNAMIC]`，重复第 2 步跑 `-t dynamic_add`。
3. **需要观察的现象**：两次都能编译执行；区别在于后者允许用不同 shape 的输入复用同一次编译（动态 shape 语义在 u2-l6 展开）。
4. **预期结果**：`add` 用例打印通过；`dynamic_add` 在 SIM 模式下会打印 "not supported in sim mode, skip verification"（[examples/01_beginner/basic/basic_ops.py:141-144](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L141-L144)）。运行输出「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`pypto.Tensor[[...], pypto.DT_FP16]` 里的 `[...]` 和 Python 注解常见的 `Ellipsis` 是一回事吗？

**答案**：在 4.2.3 的解析器里，`[...]` 作为 shape 传入 `TensorAnnotation`，表示「维度信息留空、由调用时的真实张量决定」；而取数时的 `a[..., 1:3]`（`__getitem__`）里的 `...` 是补齐剩余维度的省略号（[python/pypto/tensor.py:1050-1061](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L1050-L1061)）。两处都借用了 Python 的 Ellipsis 语法，但语义由 PyPTO 自己定义。

**练习 2**：如果把签名写成 `pypto.Tensor[[2, 3], pypto.DT_FP16]`，调用时却传入 shape 为 `(4, 3)` 的 torch 张量，会发生什么？

**答案**：注解只描述签名张量定义，最终进入编译的 shape 以转换结果为准（`from_torch` 以 torch 张量实际 shape 构建，见 4.4.3）；静态维与实际不符会在后续图校验/执行阶段暴露为错误。精确报错位置属前端校验逻辑，「待确认」，建议本地实验观察报错信息。

### 4.3 创建类算子：zeros / ones / full / arange

#### 4.3.1 概念说明

在 kernel 内部经常需要「凭空」造一个张量：ReLU 需要一个全 0 张量做 `maximum`，GELU 需要全 1 张量算 sigmoid，序列掩码需要 `arange` 生成的下标。`python/pypto/op/creation.py` 提供四个创建算子，它们与 `pypto.add` 一样是**图算子**——调用即在计算图中登记一个节点，返回的 Tensor 是该节点的输出图纸。

四个算子的分工：

| 算子 | 生成内容 | 关键参数 |
| --- | --- | --- |
| `zeros(*size)` | 全 0 | size（多个 int 或一个序列），dtype 缺省 `DT_FP32` |
| `ones(*size)` | 全 1 | 同上 |
| `full(size, fill_value, dtype)` | 全部等于 fill_value | 支持 int/float/`SymbolicScalar`/`Element`，可带 `valid_shape` |
| `arange(start, end, step)` | 1 维等差序列 | 1~3 个参数，对应 end / (start, end) / (start, end, step) |

#### 4.3.2 核心流程

`zeros` / `ones` 是 `full` 的语法糖，而 `full` 最终落到 C++ 的 `Full` 算子：

```text
pypto.zeros(2, 3)                     pypto.ones(2, 3)
      │ 归一化 size → [2, 3]                │ 同左
      ▼                                     ▼
Element(DT_FP32, 0) ──► pypto_impl.Full(element, dtype, shape, [])  ◄── Element(DT_FP32, 1)

pypto.full([2,2], 1.0, pypto.DT_FP32)
      │ 检查无符号 dtype 不允许负填充值
      │ fill_value 是 int/float → 包成 Element(dtype, value)
      ▼
pypto_impl.Full(...)
```

`arange` 则是把标量参数逐个 `convert_to_element` 后调用 `pypto_impl.Range`。整数标量按数值范围选择 `DT_INT32` 或 `DT_INT64`，浮点标量用 `DT_FP32`（[python/pypto/op/creation.py:25-32](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/creation.py#L25-L32)）。

#### 4.3.3 源码精读

**`op_wrapper`：算子的统一包装**（[python/pypto/_op_wrapper.py:46-61](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/_op_wrapper.py#L46-L61)）：创建算子都带 `@op_wrapper`。包装器把 Python 侧 Tensor 换成 `_base` 再调实现函数，并把 C++ 返回的张量用 `Tensor.from_base` 包回 Python 对象（33-43 行的 `_from_base`），同时维护源码位置信息用于诊断。你不需要直接使用它，但读 creation.py 时要知道这一层存在。

**`full` 的参数校验与分发**（[python/pypto/op/creation.py:223-276](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/creation.py#L223-L276)）：268-270 行处理 `valid_shape` 缺省并做无符号检查（无符号整型不允许负填充值，校验函数在 [python/pypto/op/creation.py:201-220](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/creation.py#L201-L220)，会抛出带解释信息的 `PyptoError`）；271-276 行按 `fill_value` 的三种类型（`SymbolicScalar` / `Element` / 普通标量）统一交给 `pypto_impl.Full`。docstring（252-265 行）给出静态图忽略 `valid_shape`、动态图显式传 `valid_shape` 的示例。

**`zeros` / `ones`**（[python/pypto/op/creation.py:279-310](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/creation.py#L279-L310) 与 [python/pypto/op/creation.py:313-345](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/creation.py#L313-L345)）：两者结构完全对称——302-305 行（zeros）兼容 `zeros(2, 3)` 与 `zeros([2, 3])` 两种传参；307-310 行 dtype 缺省 `DT_FP32`，然后用 `Element(dtype, 0)` 调 `Full`。ones 仅把 0 换成 1。

**`arange` 的三种重载**（[python/pypto/op/creation.py:137-195](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/creation.py#L137-L195)）：175-181 行单参数视为 `end`（start 固定 0、step 固定 1）；183-189 行双参数；191-192 行参数个数不在 1~3 之间抛 `PyptoError`；194-195 行三参数完整调用 `pypto_impl.Range`。

**真实用例：FFN 里的常量**（[examples/02_intermediate/basic_nn/ffn/ffn_module.py:122-124](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/02_intermediate/basic_nn/ffn/ffn_module.py#L122-L124)）：`relu_activation_core` 里 `zero = pypto.full(x.shape, 0, x.dtype, valid_shape=x.shape)` 然后 `pypto.maximum(x, zero)` 实现 ReLU；GELU 里同样用 `pypto.full(exp_neg.shape, 1.0, ...)` 造全 1 张量参与 sigmoid（151-152 行）。注意它把 `x.shape` 和 `x.dtype` 直接传给 `full`——创建算子的参数可以是**来自其他 Tensor 的符号量**，这是图编程的典型风格。

#### 4.3.4 代码实践：kernel 内造常量

1. **实践目标**：在 jit kernel 内用 `pypto.full` / `pypto.arange` 生成常量并写回输出。
2. **操作步骤**（示例代码，仿照 [examples/01_beginner/basic/basic_ops.py:26-44](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L26-L44) 的三段式骨架编写）：

   ```python
   # 文件名：const_kernel.py（示例代码）
   import torch
   import pypto

   runtime_options = {"run_mode": pypto.RunMode.SIM}   # 无真机用 SIM；NPU 见 u1-l2


   @pypto.frontend.jit(runtime_options=runtime_options)
   def bias_kernel(x: pypto.Tensor[[...], pypto.DT_FP32],
                   out: pypto.Tensor[[...], pypto.DT_FP32]):
       pypto.set_vec_tile_shapes(32, 32)
       one = pypto.full(x.shape, 1.0, pypto.DT_FP32)   # 与 x 同形的全 1 常量
       idx = pypto.arange(0, x.shape[1])               # 0..N-1 下标序列
       bias = idx * one                                 # 广播成与 x 同型的斜坡
       out[:] = x + bias


   def test_bias(device):
       shape = (64, 64)
       x = torch.arange(shape[0] * shape[1], dtype=torch.float32,
                        device=device).reshape(shape) * 0.01
       out = torch.zeros(shape, dtype=torch.float32, device=device)
       bias_kernel(x, out)
       golden = x + torch.arange(shape[1], dtype=torch.float32, device=device)
       torch.testing.assert_close(out, golden, atol=1e-3, rtol=1e-3)
       print("bias_kernel OK")


   if __name__ == "__main__":
       test_bias("cpu")   # SIM 模式下数据放在 CPU torch 张量上
   ```

   说明：`bias = idx * one` 里 `idx` 是 1 维、`one` 与 `x` 同形，广播行为能否被编译器接受「待本地验证」；若报错，把两行简化为只用 `pypto.full(x.shape, 1.0, pypto.DT_FP32)` 实现 `out[:] = x + one`（与 ffn_module 的 ReLU 同构，一定可行），再单独跑一个只含 `pypto.arange` 的 1 维 kernel 观察输出。
3. **需要观察的现象**：`full` / `arange` 都发生在 kernel 体内（编译期登记为图节点）；输出等于 `x` 加一个逐行递增的偏置。
4. **预期结果**：打印 `bias_kernel OK`。「待本地验证」（含广播版本是否需要调整）。

#### 4.3.5 小练习与答案

**练习 1**：`pypto.zeros(2, 3)` 和 `pypto.full([2, 3], 0, pypto.DT_FP32)` 等价吗？

**答案**：等价。zeros（creation.py 307-310 行）就是构造 `Element(dtype, 0)` 后调用同一个 `pypto_impl.Full`；唯一差别是 zeros 的 dtype 缺省 `DT_FP32`，而 `full` 必须显式给 dtype。

**练习 2**：`pypto.full([2, 2], -1, pypto.DT_UINT8)` 会发生什么？

**答案**：抛 `PyptoError`。`_check_full_fill_value_unsigned`（creation.py 201-220 行）禁止对无符号整型填负值，错误信息会同时给出 fill_value 和 dtype。

**练习 3**：`pypto.arange(5.5)` 的输出 dtype 是什么？

**答案**：`DT_FP32`。单参数路径里 `end=5.5` 经 `convert_to_element` 转成 `Element(DT_FP32, 5.5)`（creation.py 25-32 行对 float 统一用 FP32），输出 `[0.0 1.0 2.0 3.0 4.0 5.0]`（docstring 58-62 行）。

### 4.4 from_torch 转换与 host/device 数据流动

#### 4.4.1 概念说明

真实工程里数据几乎都来自 torch：训练脚本用 torch 造输入、网络用 torch 管参数。`pypto.from_torch` 是两个世界的桥梁，它做四件事：

1. 校验输入确实是 `torch.Tensor` 且内存连续（contiguous）；
2. 把 torch dtype 翻译成 `pypto.DataType`；
3. 决定排布格式（默认 `TILEOP_ND`；NPU 上的 NZ 格式张量自动识别）；
4. 用 torch 张量的 `data_ptr()` 构造 `pypto.Tensor`——**不复制任何数据**。

`from_torch` 在包顶层直接导出（[python/pypto/__init__.py:29](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L29)），所以写 `pypto.from_torch(...)` 即可。官方教程把它列为标准数据准备方式（[docs/zh/tutorials/development/tensor_creation.md:48-58](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_creation.md#L48-L58)）。

#### 4.4.2 核心流程

```text
torch.Tensor (host 或 npu 上的真实数据)
      │ from_torch(tensor, name, dynamic_axis, tensor_format, dtype)
      ├─ isinstance / is_contiguous 校验
      ├─ dtype ← 参数指定 或 _dtype_from(tensor.dtype) 查表
      ├─ format ← 参数指定 或 默认 ND；npu 设备上探测 NZ（get_npu_format == 29）
      ├─ shape ← list(tensor.shape)
      │     ├─ NZ 格式 → 末两维对齐（内轴 32 字节 / 外轴 16 元素）
      │     ├─ FP4 类型 → 末维 ×2（两半字节打包）
      │     └─ dynamic_axis 指定的维 → 置 -1（动态维度）
      └─ Tensor(shape, dtype, name, data_ptr=tensor.data_ptr(), ...)
              ▼
pypto.Tensor（图纸 + 指向 torch 内存的指针，零拷贝）
              ▼ 执行期
_pto_to_tensor_data: DeviceTensorData(dtype, t.data_ptr, t.ori_shape)
              ▼
设备按指针直接读写这块内存
```

NZ 对齐的数学关系（[python/pypto/converter.py:46-76](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/converter.py#L46-L76)）：设每个元素占 \(b\) 字节，则内轴对齐粒度为 \(c_0 = \lfloor 32 / b \rfloor\) 个元素，外轴对齐粒度为 16 个元素：

\[ s_{d-1}' = \left\lceil \frac{s_{d-1}}{c_0} \right\rceil \cdot c_0, \qquad s_{d-2}' = \left\lceil \frac{s_{d-2}}{16} \right\rceil \cdot 16 \]

#### 4.4.3 源码精读

**`from_torch` 主体**（[python/pypto/converter.py:79-174](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/converter.py#L79-L174)）：

- 134-138 行：类型与连续性校验，非 torch.Tensor 或非 contiguous 直接抛 `FeError`。
- 140-147 行：dtype 与格式的确定——`_dtype_from` 查表；格式默认 `TILEOP_ND`，只有当张量在 `npu` 设备上且 `torch_npu.get_npu_format(tensor) == 29` 时识别为 `TILEOP_NZ`。
- 149-157 行：0 维 torch 标量张量被规范成 shape `[1]`。
- 158-165 行：shape 加工——NZ 对齐（`_set_shape_nz_aligned`）、FP4 末维翻倍、`dynamic_axis` 中的维度置 `-1`。
- 166-174 行：返回 `Tensor(..., data_ptr=tensor.data_ptr(), device=tensor.device, ori_shape=list(tensor.shape))`。**`data_ptr` 直接取自 torch 张量，没有任何拷贝**——这就是零拷贝；`ori_shape` 保留原始形状供运行期使用（NZ 对齐后的 shape 与 ori_shape 会不同）。

**dtype 双向映射表**（[python/pypto/converter.py:177-202](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/converter.py#L177-L202)）：`_dtype_dict` 覆盖 float16/32/64、bfloat16、int/uint 8~64、bool、三种 FP8 与 FP4 打包类型；`_dtype_from` 查不到就抛 `FeError`。反向映射 `_torch_dtype_from`（205-285 行）供 verify 场景构造 host 侧 golden 数据用，其中 FP8 类型会动态探测当前 torch 版本是否支持，不支持时抛出带升级建议的 `VerifyError`（259-274 行）。

**自动命名**（[python/pypto/converter.py:25-43](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/converter.py#L25-L43)）：`from_torch` 被 `@_count_calls` 装饰，不传名字时自动编号 `TENSOR_1`、`TENSOR_2`……这就是文档示例 `pypto.from_torch(x)` 打印出的名字来源。

**执行期才真正用指针**（[python/pypto/runtime.py:75-86](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/runtime.py#L75-L86)）：`_pto_to_tensor_data` 把 pypto.Tensor 压成 `DeviceTensorData(dtype, t.data_ptr, list(t.ori_shape))` 交给 C++ 运行时——注意它要求 `ori_shape` 必须存在（78-79 行），并再次印证数据搬运只发生在执行期。

**框架内部的两个 from_torch 使用点**：一是 jit 调用时（见 4.2.3 的 entry.py 420 行）；二是数值验证机制 `verify`（[python/pypto/runtime.py:149-152](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/runtime.py#L149-L152)）把输入输出统一命名 `IN_i` / `OUT_i` 后转换；golden 数据注入 `set_verify_golden_data` 同样接受 torch 张量并自动 `from_torch`（190、206 行）。

#### 4.4.4 代码实践：from_torch 体检与零拷贝验证

1. **实践目标**：验证 from_torch 是零拷贝转换，并观察 `dynamic_axis` 与 dtype/format 参数的效果。
2. **操作步骤**（示例代码；CPU torch 张量即可，不需要真机）：

   ```python
   # 文件名：from_torch_probe.py（示例代码）
   import torch
   import pypto

   x = torch.randn(2, 3, dtype=torch.float16)
   p = pypto.from_torch(x, "in_0")

   print("shape :", p.shape)                 # 预期 [2, 3]
   print("dtype :", p.dtype)                 # 预期 DataType.DT_FP16
   print("name  :", p.name)                  # 预期 in_0
   print("format:", p.format)                # 预期 TileOpFormat.TILEOP_ND
   print("zero-copy:", p.data_ptr == x.data_ptr())   # 预期 True

   d = pypto.from_torch(x, "dyn_0", dynamic_axis=[0])
   print("dynamic shape:", d.shape)          # 预期 [SymbolicScalar(...) 或 -1 相关输出, 3]

   q = pypto.from_torch(x, "cast_0", dtype=pypto.DT_FP32)
   print("override dtype:", q.dtype)         # 预期 DataType.DT_FP32（注解覆盖）
   ```

3. **需要观察的现象**：`data_ptr` 与原 torch 张量完全相同（零拷贝）；`dynamic_axis=[0]` 后第 0 维不再是整数；显式传 `dtype` 会覆盖 torch 自身的 float16。
4. **预期结果**：与注释一致。`d.shape` 的确切打印文本与 SymbolicScalar 展示形式「待本地验证」（可对照 [python/pypto/converter.py:119-122](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/converter.py#L119-L122) docstring 的示例输出）。注意：得到的 `p` / `d` / `q` 用于属性检查与理解数据流；**调用 jit 算子时仍直接传 torch 张量**（原因见 4.2.3 的入口校验），框架内部会做同样的转换。

#### 4.4.5 小练习与答案

**练习 1**：`pypto.from_torch(t)` 之后修改 `t`（原地 `t += 1`），转换结果会变吗？

**答案**：会「跟着变」。from_torch 只记录 `data_ptr`，两边共享同一块内存，torch 侧的原地修改对设备执行时读到的数据直接可见——这正是零拷贝的代价与便利：省了复制，但要求转换后到执行前不要意外改动或释放原张量。

**练习 2**：为什么 from_torch 要求张量 `is_contiguous()`？

**答案**：PyPTO 的 Tensor 图纸只携带 shape 与一个起始指针，没有 stride（跨步）信息，无法表达 torch 非连续张量的复杂寻址；所以 converter.py 137-138 行直接拒绝非连续张量，需要先在 torch 侧 `.contiguous()`。

**练习 3**：`torch.zeros(2, 3)` 在 NPU 上经自动探测被判成 `TILEOP_NZ` 的条件是什么？

**答案**：仅当张量 `device.type == "npu"` 且 `torch_npu.get_npu_format(tensor) == 29`（converter.py 143-147 行）；host/CPU 张量永远是 `TILEOP_ND`。数字 29 是 CANN 侧 NZ 格式的编码值。

## 5. 综合实践

把本讲四个模块串起来，完成一个「常量偏置算子 + 数据流观察」小任务：

**任务**：实现 `scale_bias_kernel(x, out)`，计算 \( \text{out} = x \cdot \alpha + \mathbf{1} \)，其中全 1 常量由 kernel 内 `pypto.full` 生成、标量 \( \alpha \) 作为非张量参数传入；host 脚本先用 `pypto.from_torch` 做一次体检（属性 + 零拷贝），再以 torch 张量调用算子并与 torch 参考实现对拍。

```python
# 文件名：u2l1_practice.py（示例代码，综合 4.3.4 与 4.4.4）
import argparse
import torch
import pypto

runtime_options = {"run_mode": pypto.RunMode.NPU}


@pypto.frontend.jit(runtime_options=runtime_options)
def scale_bias_kernel(x: pypto.Tensor[[...], pypto.DT_FP32],
                      out: pypto.Tensor[[...], pypto.DT_FP32],
                      alpha: float):
    pypto.set_vec_tile_shapes(32, 32)
    ones = pypto.full(x.shape, 1.0, x.dtype)    # 4.3：kernel 内创建常量
    out[:] = x * alpha + ones                   # 4.1：out[:] → move 写回


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--run_mode", choices=["npu", "sim"], default="npu")
    args = parser.parse_args()

    if args.run_mode == "sim":                  # 与 basic_ops.py 的 device_init 同思路
        runtime_options["run_mode"] = pypto.RunMode.SIM
        device = "cpu"
    else:
        import torch_npu
        torch.npu.set_device(0)
        device = "npu:0"

    shape = (64, 64)
    x = torch.randn(shape, dtype=torch.float32, device=device)
    out = torch.zeros(shape, dtype=torch.float32, device=device)

    probe = pypto.from_torch(x, "x_probe")      # 4.4：转换体检
    print("probe:", probe.shape, probe.dtype, probe.name,
          "zero-copy:", probe.data_ptr == x.data_ptr())

    alpha = 2.5
    scale_bias_kernel(x, out, alpha)            # 调用传 torch 张量 + 标量
    torch.testing.assert_close(out, x * alpha + 1.0, atol=1e-3, rtol=1e-3)
    print("u2l1 practice OK")


if __name__ == "__main__":
    main()
```

**验收要点**：

1. `probe` 的 shape/dtype/name 与预期一致，`zero-copy` 打印 `True`。
2. NPU 模式下断言通过；SIM 模式下至少完成编译执行（断言结果「待本地验证」，SIM 的适用范围见 u1-l2）。
3. 把 `pypto.full(x.shape, 1.0, x.dtype)` 换成 `pypto.ones(*x.shape)`（注意 ones 接收可变参数）再跑一遍，验证 4.3 练习 1 的等价性。
4. 思考题：如果把 `alpha` 放进注解当张量（`alpha: pypto.Tensor[[], pypto.DT_FP32]`）会怎样？对照 4.2.3 的参数分流规则（张量参数按位置、非张量参数按位置或关键字）给出你的解释，并在下一讲（u2-l2 算子 API）里检验。

## 6. 本讲小结

- `pypto.Tensor` 是计算图的数据描述符：Python 门面 + C++ `_base`；`data_ptr` / `device` / `ori_shape` 留在 Python 侧供执行期使用，`pypto.tensor` 只是 `Tensor` 的别名。
- `Tensor[...]` 下标语法经 `__class_getitem__` 产生 `TensorAnnotation`，jit 调用时框架读取其中的 DYNAMIC 维、显式 dtype/format，通过 `from_torch` 自动把 torch 张量转成 pypto.Tensor；调用入口当前要求直接传 torch.Tensor。
- shape 有三种维度写法：静态整数、`-1` / `DYNAMIC` 动态维（读 `shape` 时表现为 `SymbolicScalar`）、`SymbolicScalar` 符号列表。
- 创建张量三条路：显式构造（`pypto.tensor(...)`）、图算子创建（`zeros`/`ones` 是 `full` 的语法糖，`arange` 走 `Range`）、外部转换（`from_torch`）。
- `from_torch` 是零拷贝：直接记录 torch 的 `data_ptr`，要求张量连续；dtype 靠映射表翻译，NPU 上可自动识别 NZ 格式并对末两维做 32 字节 / 16 元素对齐。
- `out[:] = value` 的本质是 `Tensor.move`：把右侧子图的计算结果登记到输出张量名下，这正是 PyPTO kernel 无返回值设计的机制支撑。

## 7. 下一步学习建议

下一讲 **u2-l2「Tensor 操作与算子 API 体系」**将展开 `python/pypto/op` 目录下的算子库（math、reduction、matmul、comparison 等），你会看到本讲 4.1.3 里一笔带过的运算符重载（`__add__` → `pypto.add`）背后的完整算子世界。建议提前浏览：

- [python/pypto/op/](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/) 目录列表，数一数有多少类算子文件；
- [python/pypto/tensor.py:339-437](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L339-L437) 的运算符重载段，它们是 op 库与 Tensor 对象的粘合剂；
- 若想先弄清 `SymbolicScalar` 的来龙去脉，可以提前读 [python/pypto/symbolic_scalar.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/symbolic_scalar.py)，但系统讲解在 u2-l6。
