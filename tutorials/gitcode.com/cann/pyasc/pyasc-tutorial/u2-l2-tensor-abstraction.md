# Tensor 抽象：LocalTensor、GlobalTensor 与切片操作

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 pyasc 中 `GlobalTensor`（对应 Global Memory）与 `LocalTensor`（对应 Unified Buffer）的使用差异，知道各自在算子里扮演什么角色。
2. 掌握两种 Tensor 的标准创建写法：`GlobalTensor()` + `set_global_buffer(...)` 二段式，以及 `LocalTensor(dtype, pos, addr, tile_size)` 四参式。
3. 读懂 `__getitem__` 中 `builder.create_asc_GlobalTensorSubIndexOp` / `create_asc_LocalTensorSubIndexOp` 的 IR 生成逻辑，并能写出 `tensor[offset:]` 切片在 IR 中对应的节点名。
4. 区分 `TensorShape`（编译期形状辅助）与 `ShapeInfo`（设备侧运行时形状）这两套看似相似、实则完全不同的机制。

## 2. 前置知识

### 2.1 两级存储：GM 与 UB

上一讲运行 Add 示例时（u1-l4），我们已经接触过昇腾 AI Core 的两级数据存储：

- **Global Memory（GM）**：容量大、所有核共享，但访问慢。Host 侧传入的 `torch.Tensor` 数据就在这里。设备侧用 `GlobalTensor` 来"指向"这段内存。
- **Unified Buffer（UB）**：AI Core 内部的本地内存，容量小（通常 256KB 量级）、访问快。计算部件（Vector/Cube）只直接读写 UB。设备侧用 `LocalTensor` 来描述 UB 上的一块缓冲。

一个算子的典型数据路径是：`GM --(MTE2 搬入)--> UB --(Vector 计算)--> UB --(MTE3 搬出)--> GM`。

### 2.2 Tensor 是「IR 值的包装」，不是数据容器

这是本讲最重要的心智模型。在普通 Python 里，`torch.Tensor` 对象内部真的存着数据。而在 pyasc 的 kernel 函数里：

```python
x_gm = asc.GlobalTensor()          # 此时只是创建了一个"空壳"Python 对象
x_gm.set_global_buffer(x, length)  # 这一步才在 IR 里生成了真正的操作
```

Tensor 对象**不持有任何数据**，它只持有两样东西：一个数据类型 `dtype`（上一讲 u2-l1 的 `DataType`），和一个指向 MLIR IR 中某个值的**句柄（handle）**。所有方法调用（切片、`get_size`、`reinterpret_cast`……）都不是在读写内存，而是在向 IR 中追加新的操作。

这个句柄体系来自 `IRValue` 抽象基类（下一讲 u2-l3 会展开）：

- [python/asc/language/core/ir_value.py:20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L20) 定义 `IRHandle` 只是 `ir.Value`（MLIR Value）的类型别名；
- [python/asc/language/core/ir_value.py:23-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L23-L32) 规定了每个 IR 值包装类必须实现 `to_ir()`（交出句柄）和 `from_ir()`（从句柄重建）两个方法。

### 2.3 `@require_jit` 与 `global_builder`

Tensor 的几乎所有方法都装饰了 `@require_jit`，见 [python/asc/language/core/utils.py:196-207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L196-L207)：它在函数入口检查全局的 `global_builder` 是否已初始化，未初始化就抛出 `RuntimeError`。

这是因为生成 IR 需要"插入点"。[python/asc/language/core/utils.py:136-170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L136-L170) 中的 `GlobalBuilder` 单例在 JIT 编译开始时被设置（持有 `ir.Builder` 和 `ir.ModuleOp`），编译结束后 teardown。如果你在 Host 侧普通 Python 代码里直接调用 `x_gm.get_size()`，没有 builder，也就没有地方放生成的 IR——`require_jit` 把这种误用挡在门口。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/language/core/tensor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py) | 本讲主战场：`BaseTensor`/`GlobalTensor`/`LocalTensor`/`LocalTensorAuto` 全部在此 |
| [python/asc/language/core/types.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py) | `ShapeInfo`（IR 值）与 `TensorShape`（编译期元组）等类型辅助结构 |
| [python/asc/language/core/ir_value.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py) | `IRValue`/`IRHandle`/`GlobalAddress` 基础协议 |
| [python/asc/language/core/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py) | `OverloadDispatcher`（构造函数重载分发）、`global_builder`、`require_jit` |
| [python/asc/language/core/enums.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py) | `TPosition` 等硬件位置枚举（u2-l4 详讲） |
| [include/ascir/Dialect/Asc/IR/Core/Tensor.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td) | Tensor 相关 IR 操作的 TableGen 定义（本讲用于对照 IR 节点名） |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | 手动管理 Tensor 的完整示例 |

回忆 u1-l3 建立的目录镜像规律：Python 前端的 `core/tensor.py` 对应后端 `include/ascir/Dialect/Asc/IR/Core/Tensor.td` 中的 `Core` 目录，检索链是 `create_asc_XxxOp` → 同象限 `.td` 里的 `def AscendC_XxxOp` → IR 名 `asc.xxx`。

## 4. 核心概念与源码讲解

### 4.1 BaseTensor：Tensor 家族的公共基座

#### 4.1.1 概念说明

`BaseTensor` 是 `GlobalTensor` 和 `LocalTensor` 的共同父类，本身非常薄：它只做两件事——记住 `dtype`，以及提供形状校验工具。它不定义任何"怎么创建 IR"的逻辑，创建逻辑完全下放到两个子类。

设计它的意义在于统一"Tensor 是什么"的抽象：**一个 dtype + 一套与 IR 互转的协议**。所有 Tensor 都能回答"我是什么类型的数据"，都能通过继承来的 `to_ir()` 协议参与 IR 构建。

#### 4.1.2 核心流程

一个 Tensor 对象的生命周期有两条路径：

```text
路径 A（用户显式构造）：
    用户在 kernel 里写 asc.LocalTensor(dtype, pos, addr, size)
        -> 构造函数内部调用 global_builder.get_ir_builder()
        -> builder.create_asc_LocalTensorV2Op(...)
        -> 把返回的 IR 句柄存进 self.handle

路径 B（框架重建）：
    TQue 的 alloc_tensor / deque 等接口在 IR 里产生一个 LocalTensor 值
        -> 需要包装成 Python 对象交给用户继续操作
        -> LocalTensor.from_ir(handle) 从 IR 类型反推出 dtype（和 shape）
```

路径 B 是 u2-l6（TPipe/TQue）的入口，本讲先记住 `from_ir` 这个洞口。

#### 4.1.3 源码精读

基类定义只有十几行：

```python
class BaseTensor(IRValue):

    def __init__(self, dtype: DataType):
        self.dtype = dtype

    @staticmethod
    def ensure_shape(shape, allow_none=True) -> Optional[TensorShape]:
        if shape is None:
            if allow_none:
                return None
            raise ValueError("Tensor shape must be provided, got None")
        return TensorShape(shape)

    @classmethod
    def from_ir(cls, handle: IRHandle) -> NoReturn:
        raise NotImplementedError(f"{cls.__name__} cannot be constructed from IR handle")
```

- [python/asc/language/core/tensor.py:24-27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L24-L27)：`__init__` 仅保存 `dtype`。注意 `GlobalTensor` 空构造时连这一步都跳过（见 4.2），此时对象处于"未初始化"状态。
- [python/asc/language/core/tensor.py:29-35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L29-L35)：`ensure_shape` 把任意可迭代形状规整成 `TensorShape`（见 4.4），`allow_none=False` 时强制要求形状——`LocalTensorAuto` 用它保证静态 shape 路径必有形状。
- [python/asc/language/core/tensor.py:37-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L37-L39)：基类的 `from_ir` 故意抛异常，因为"从 IR 重建"必须由子类结合自己的 IR 类型信息实现，基类无从得知该抽取出什么。

`IRValue` 协议见 [python/asc/language/core/ir_value.py:23-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L23-L32)，就是 `from_ir`/`to_ir` 两个抽象方法。整个继承树是：`IRValue → BaseTensor → GlobalTensor / LocalTensor → LocalTensorAuto`。

#### 4.1.4 代码实践

**实践目标**：在 Host 侧验证 Tensor 类的继承结构，确认它们确实是 `IRValue` 的子类。

**操作步骤**：

1. 在装好 pyasc 的环境里进入 Python 交互式解释器；
2. 执行以下命令（示例代码）：

```python
import asc
print(asc.LocalTensor.__mro__)
print(asc.GlobalTensor.__mro__)
print(asc.LocalTensor().dtype if False else "LocalTensor() 不能在 Host 侧空参构造，跳过")
```

3. 观察 `__mro__`（方法解析顺序）输出。

**需要观察的现象**：`__mro__` 元组中依次出现 `LocalTensor` → `BaseTensor` → `IRValue` → `abc.ABC`。

**预期结果**：两个类的 MRO 都包含 `BaseTensor` 和 `IRValue`。这些只是读取类属性，不触发 JIT，也不需要 builder。

**待本地验证**：`asc.LocalTensor()` 空参构造在 Host 侧的具体报错形式（可能是 `OverloadDispatcher` 报"No viable candidates"，因为它没有注册空参重载），建议实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaseTensor.from_ir` 直接抛 `NotImplementedError`，而不是也做成抽象方法？

**答案**：`IRValue.from_ir` 已经是 `@abc.abstractmethod`，子类 `GlobalTensor`/`LocalTensor` 都必须实现它。`BaseTensor` 再抛异常是一种防御式写法：如果未来新增的 Tensor 子类忘记实现 `from_ir`，调用时会得到一条带类名的明确错误（`f"{cls.__name__} cannot be constructed from IR handle"`），而不是静默继承错误行为。

**练习 2**：`BaseTensor.__init__` 只存了 `dtype`，那 `self.handle` 是谁设置的？

**答案**：由各子类构造函数设置。例如 `GlobalTensor.set_global_buffer` 在创建 `asc.global_tensor` 操作后执行 `self.handle = handle`（见 4.2.3）；`LocalTensor` 的三个重载分支各自给 `self.handle` 赋值。`GlobalTensor()` 空构造时 `handle` 属性甚至不存在，直到 `set_global_buffer` 被调用。

---

### 4.2 GlobalTensor：Global Memory 的设备侧视图

#### 4.2.1 概念说明

`GlobalTensor` 用来存放 Global Memory（外部存储）的全局数据，类的中文文档字符串就写明了这一点（[python/asc/language/core/tensor.py:42-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L42-L46)）。

它解决的问题是：Host 传给 kernel 的是一个**裸的设备指针**（参数标注为 `asc.GlobalAddress`），而 `data_copy`、`asc.add` 这些算子 API 需要一个带类型的"内存视图"才能工作。`GlobalTensor` 就是把裸指针 + 数据类型 + 长度打包成视图的机制，对应 Ascend C 里的 `GlobalTensor<T>` 类。

它的生命周期是**二段式**的：

```python
x_gm = asc.GlobalTensor()                    # 第一段：创建空壳（不生成 IR）
x_gm.set_global_buffer(x + offset, length)   # 第二段：绑定地址（生成 IR）
```

为什么不能一步到位？因为类型信息藏在 `GlobalAddress` 里：Host 侧传入 `torch.Tensor` 时，JIT 参数特化（u2-l1 的 KnownTypes）会把 `torch.float32` 映射成带类型的指针。空构造的 `GlobalTensor` 自己不知道类型，必须等 `set_global_buffer` 从指针参数上"取"下来。

#### 4.2.2 核心流程

`set_global_buffer` 的执行流程：

```text
1. 校验 buffer（GlobalAddress）非空且带 dtype，否则报 ValueError
2. 从 buffer.dtype 推导出自己的 dtype，调用 super().__init__(dtype)
3. ir_type = ir.get_global_tensor_type(dtype.to_ir())     # 构造 asc.global_tensor<T> 类型
4. handle = builder.create_asc_GlobalTensorOp(ir_type)    # 生成 IR：实例化全局张量
5. self.handle = handle                                    # 空壳从此"有魂"
6. 若给了 buffer_size：
     create_asc_GlobalTensorSetGlobalBufferOp(self, buffer, size)
   否则：
     create_asc_GlobalTensorSetGlobalBufferOp(self, buffer)
```

切片 `x_gm[k:]` 的执行流程：

```text
1. require_jit 检查 builder
2. 入参是 RuntimeInt（运行时整型表达式）？ -> 直接物化为 IR 值作 index
   入参是 slice？                            -> 只接受 [start:] 形式，
                                               stop/step 必须为 None，取 start 作 index
3. handle = builder.create_asc_GlobalTensorSubIndexOp(类型, self.to_ir(), index)
4. 返回一个新的 GlobalTensor（包装新 handle），原对象不变
```

关键语义：`t[k:]` 表示"从 `t` 的起始地址向前偏移 \( k \) 个**元素**的新视图"，字节地址满足：

\[ \text{addr}(t[k:]) = \text{addr}(t) + k \times \text{sizeof}(T) \]

这对应 Ascend C 的 `operator[]`，Python 切片语法只是它的外壳。

#### 4.2.3 源码精读

**空构造**——[python/asc/language/core/tensor.py:48-59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L48-L59)：

```python
@overload
def __init__(self, handle: IRHandle) -> None:
    """This contructor should not be called by user"""
    ...

def __init__(self, handle: Optional[IRHandle] = None) -> None:
    self.shape = None
    if handle is not None:
        dtype = DataType.from_ir(ir.get_element_type(handle.get_type()))
        super().__init__(dtype)
        self.handle = handle
        return
```

用户空参调用时 `handle is None`，只设置 `self.shape = None` 就返回——此时对象没有 dtype、没有 handle，是个纯粹的占位符。带 handle 的分支供 `from_ir`（[python/asc/language/core/tensor.py:85-90](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L85-L90)）内部重建用，从 IR 类型中反查元素类型。

**set_global_buffer**——[python/asc/language/core/tensor.py:149-167](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L149-L167)：

```python
@require_jit
def set_global_buffer(self, buffer=None, buffer_size=None) -> None:
    if buffer is None or buffer.dtype is None:
        raise ValueError("Either DataType or typed GlobalAddress must be provided ...")
    dtype = buffer.dtype
    super().__init__(dtype)
    builder = global_builder.get_ir_builder()
    ir_type = ir.get_global_tensor_type(dtype.to_ir())
    handle = builder.create_asc_GlobalTensorOp(ir_type)
    self.dtype = dtype
    self.handle = handle

    if buffer_size:
        builder.create_asc_GlobalTensorSetGlobalBufferOp(self.to_ir(), buffer.to_ir(),
                                                         _mat(buffer_size).to_ir())
    else:
        builder.create_asc_GlobalTensorSetGlobalBufferOp(self.to_ir(), buffer.to_ir())
```

三个观察点：

1. 这段代码一次生成**两个** IR 操作：`create_asc_GlobalTensorOp`（实例化）和 `create_asc_GlobalTensorSetGlobalBufferOp`（绑定）。
2. `buffer_size` 是**元素个数**而非字节数（可对照 [python/asc/language/core/utils.py:245-250](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L245-L250) 中该接口的文档说明）。
3. `buffer` 参数可以是做过指针运算的地址——01_add 里的 `x + offset` 走的是 `GlobalAddress.__add__`（[python/asc/language/core/ir_value.py:45-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L45-L51)），生成 `emitasc.PtrOffset` IR，实现"每个核绑定到自己负责的数据段"。

后端对照（TableGen 定义）：

- `create_asc_GlobalTensorOp` 对应 [include/ascir/Dialect/Asc/IR/Core/Tensor.td:49-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L49-L54)，`AscendC_Op<"global_tensor", [AscConstructor]>`，IR 节点名 **`asc.global_tensor`**；
- `create_asc_GlobalTensorSetGlobalBufferOp` 对应 [include/ascir/Dialect/Asc/IR/Core/Tensor.td:139-148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L139-L148)，IR 节点名 **`asc.global_tensor.set_global_buffer`**，其 `size` 操作数声明为 `Optional<AnyInteger>`，正对应 Python 侧"可传可不传 buffer_size"的两个分支。

**切片 `__getitem__`**——[python/asc/language/core/tensor.py:61-74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L61-L74)：

```python
@require_jit
def __getitem__(self, slices: Any) -> GlobalTensor:
    builder = global_builder.get_ir_builder()
    if isinstance(slices, RuntimeInt):
        handle = builder.create_asc_GlobalTensorSubIndexOp(self.to_ir().get_type(), self.to_ir(),
                                                           _mat(slices).to_ir())
        return GlobalTensor(handle=handle)
    if isinstance(slices, slice):
        if slices.step is not None or slices.stop is not None:
            raise RuntimeError("Slice operation with provided stop and step is not supported for GlobalTensor")
        handle = builder.create_asc_GlobalTensorSubIndexOp(self.to_ir().get_type(), self.to_ir(),
                                                           _mat(slices.start).to_ir())
        return GlobalTensor(handle=handle)
    raise RuntimeError(f"Tensor subscript operation is not supported with {slices}")
```

逐行拆解：

- `isinstance(slices, RuntimeInt)` 分支：处理 `t[k]`（方括号包的是运行时整型表达式，如循环变量派生值）。`RuntimeInt` 是延迟求值的整型 IR 值（u2-l3 详讲），`_mat` 即 `materialize_ir_value`，负责把 Python 数字或 IR 值统一落成 IR 常量/值。
- `isinstance(slices, slice)` 分支：处理 `t[k:]`。Python 会把 `t[k:]` 解析成 `slice(k, None, None)`，所以这里强校验 `stop`/`step` 必须是 `None`——`t[0:10]`、`t[::2]` 这类"真切片"不被支持，pyasc 只把 `[start:]` 借用为"偏移视图"语法。
- 两个分支殊途同归：都调用 `create_asc_GlobalTensorSubIndexOp(结果类型, 原张量, 偏移)`，返回**新的** `GlobalTensor`，原对象不动——这是纯函数式语义，`[Pure]`。

对应后端定义 [include/ascir/Dialect/Asc/IR/Core/Tensor.td:128-137](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L128-L137)：

```tablegen
def AscendC_GlobalTensorSubIndexOp
    : AscendC_Op<"global_tensor.subindex", [Pure]> {
  let summary = "Call `AscendC::GlobalTensor::operator[]` method";
  let arguments = (ins AscendC_GlobalTensor:$tensor, AnySignlessIntegerOrIndex:$index);
  let results = (outs AscendC_GlobalTensor:$result);
  let assemblyFormat = [{
    $tensor `[` $index `]`  attr-dict `:` qualified(type($tensor)) `,`
    type($index) `,` qualified(type($result))
  }];
}
```

三个信息：IR 节点名是 **`asc.global_tensor.subindex`**；summary 印证它就是 Ascend C `operator[]` 的 IR 化；`assemblyFormat` 说明它在 dump 出的 `.mlir` 文本里长成 `xxx[index] : ...` 的中括号样子，和 Python 写法神似。

另外还有一个孪生操作 `__call__`（[python/asc/language/core/tensor.py:76-83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L76-L83)）：`t(k)` 圆括号形式，生成 `asc.global_tensor.bracket`（td 定义在 [include/ascir/Dialect/Asc/IR/Core/Tensor.td:56-65](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L56-L65)）。`[]` 与 `()` 在 Ascend C 里都是 `operator[]` 的重载，pyasc 各给一个 IR 操作。

再看 01_add 中的实际用法——[examples/01_add/add.py:31-37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31-L37)：

```python
offset = asc.get_block_idx() * block_length
x_gm = asc.GlobalTensor()
...
x_gm.set_global_buffer(x + offset, block_length)
```

每个核用自己的 `block_idx` 算出偏移，把 `x_gm` 绑定到 GM 上属于本核的那段数据。而循环体内 [examples/01_add/add.py:53-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L53-L54) 的 `x_gm[i * tile_length:]` 则是在这段数据里再按 tile 前进——两层偏移，一层切核、一层切块。

其余常用成员方法（都遵循"生成一个 IR 操作"的套路）：

| 方法 | IR 节点 | 说明 |
| --- | --- | --- |
| `get_phy_addr(offset=0)` | `asc.global_tensor.get_phy_addr` | 返回 `GlobalAddress`，见 [tensor.py:100-107](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L100-L107) |
| `get_size()` | `asc.global_tensor.get_size` | shape 未知时生成 IR 查询；已知时编译期累乘，见 [tensor.py:120-127](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L120-L127) |
| `get_value(offset)` / `set_value(offset, v)` | `...get_value` / `...set_value` | 标量读写 GM |
| `get_shape_info()` / `set_shape_info(si)` | `...get_shape_info` / `...set_shape_info` | 运行时形状，见 4.4 |
| `set_l2_cache_hint(...)` | `...set_l2_cache_hint` | L2 缓存提示 |

#### 4.2.4 代码实践

**实践目标**：把 01_add 中 `x_gm` 的两层偏移关系写成一张地址表。

**操作步骤**：

1. 打开 [examples/01_add/add.py:29-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L29-L54)，取参数 `USE_CORE_NUM=8`、`BUFFER_NUM=2`、`TILE_NUM=8`、总长度 `8*2048`、`float32`；
2. 手工计算：`block_length`、第 3 号核（`block_idx=2`）的 `offset`、`tile_length`、循环第 `i=5` 次时 `x_gm[i * tile_length:]` 相对 GM 首地址的元素偏移；
3. 用公式 \( \text{偏移} = \text{block\_idx} \times \text{block\_length} + i \times \text{tile\_length} \) 核对结果。

**需要观察的现象**：`x_gm[i * tile_length:]` 的偏移量表达式恰好由两个运行时值（`block_idx` 派生的指针偏移 + 循环变量派生的 subindex）在不同层次组成。

**预期结果**：`block_length = 2048`；核 2 的 `offset = 4096` 个元素；`tile_length = 2048/8/2 = 128`；`i=5` 时 subindex 偏移为 `5*128 = 640`，即该次 `data_copy` 读取 GM 中第 `4096+640=4736` 号元素起的 128 个。**待本地验证**：可运行示例后用 dump 的 IR 中 index 值核对（若被折叠成常量则直接可见）。

#### 4.2.5 小练习与答案

**练习 1**：在 kernel 里写 `x_gm[10:20]` 会发生什么？为什么 pyasc 不支持它？

**答案**：抛出 `RuntimeError("Slice operation with provided stop and step is not supported for GlobalTensor")`，判定位置在 [python/asc/language/core/tensor.py:68-70](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L68-L70)。因为底层只映射到 Ascend C 的 `operator[]`（单偏移视图），没有"区间拷贝"语义；真正的区间数据移动由 `data_copy` 的 count 参数表达。

**练习 2**：`set_global_buffer` 为什么必须先 `create_asc_GlobalTensorOp` 再 `create_asc_GlobalTensorSetGlobalBufferOp`，而不是一步生成？

**答案**：这镜像了 Ascend C 的两步写法：先 `GlobalTensor<T> gm;`（构造对象），再 `gm.SetGlobalBuffer(ptr, size);`（绑定缓冲）。IR 层面，`asc.global_tensor` 操作**产生**一个值（results 里是 `$tensor`），而 `set_global_buffer` **消费**这个值（arguments 里第一个操作数是 `$tensor`），两者是"定义—使用"关系，天然需要两个节点。

**练习 3**：`x_gm[0:]` 偏移为 0，生成的 IR 是什么？它可能被优化掉吗？

**答案**：仍然生成一个 `asc.global_tensor.subindex` 操作，index 是常量 0。从 td 看 `AscendC_GlobalTensorSubIndexOp` 未声明 `hasCanonicalizeMethod`（对比 `AscendC_GlobalTensorOp` 在 [Tensor.td:53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L53) 声明了），所以它不保证在 Pass 流水线中被折叠；最终是否消除取决于后续 Pass 与发射层处理。**待本地验证**：dump 对比 `[0:]` 与不带切片的 IR 差异。

---

### 4.3 LocalTensor：Unified Buffer 上的手工排布

#### 4.3.1 概念说明

`LocalTensor` 用于存放 AI Core 中 Local Memory（内部存储）的数据，支持的逻辑位置 `TPosition` 为 VECIN、VECOUT、VECCALC、A1、A2、B1、B2、CO1、CO2（类文档字符串，[python/asc/language/core/tensor.py:199-202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L199-L202)）。枚举定义见 [python/asc/language/core/enums.py:248-261](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L248-L261)。

与 `GlobalTensor` 的本质差异不在类结构，而在**你用它做什么**：

| 维度 | GlobalTensor | LocalTensor |
| --- | --- | --- |
| 对应存储 | GM（外部大内存） | UB 等 Local Memory |
| 地址来源 | Host 传入的设备指针，`set_global_buffer` 绑定 | 用户在构造时给出 `addr`（字节偏移），手工排布 |
| 典型角色 | 算子的输入/输出口 | 计算的草稿纸，`data_copy` 与向量算子的直接操作对象 |
| 创建后是否变长 | 由 `buffer_size` 声明 | 可用 `set_buffer_len`/`set_size` 调整 |

01_add 用的是**手动风格**：用户自己算好每个缓冲在 UB 里的字节偏移。02_add_framework（u2-l6）会用 TPipe/TQue 把这层手工排布托管给框架。

#### 4.3.2 核心流程

`LocalTensor.__init__` 没有直接写 if/else，而是用 `OverloadDispatcher` 注册了三个重载分支：

```text
分支 1  LocalTensor(handle, dtype, shape)
        内部路径：从已有 IR 句柄重建（from_ir / 框架返回值包装）
分支 2  LocalTensor(dtype, pos=TPosition.VECIN, addr=0, tile_size=0)
        用户路径（手动风格）：生成 asc.local_tensor_v2，带位置/地址/长度
分支 3  LocalTensor(dtype)
        用户路径（极简）：生成 asc.local_tensor，位置与地址留空，交给后续 Pass 补全
```

手动排布的地址算术（以 01_add 为例）：

\[ \text{tile\_length} = \frac{\text{block\_length}}{\text{TILE\_NUM} \times \text{BUFFER\_NUM}} \]

\[ \text{buffer\_size} = \text{tile\_length} \times \text{BUFFER\_NUM} \times \text{sizeof}(T) \quad (\text{字节}) \]

三个缓冲的排布：

\[ \text{addr}(x) = 0,\quad \text{addr}(y) = \text{buffer\_size},\quad \text{addr}(z) = 2 \times \text{buffer\_size} \]

注意单位约定：**`addr` 是字节，`tile_size` 是元素个数**。切片 `[k:]` 的 `k` 同样以元素为单位，字节偏移按 \( k \times \text{sizeof}(T) \) 折算。

#### 4.3.3 源码精读

**构造函数**——[python/asc/language/core/tensor.py:225-253](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L225-L253)：

```python
def __init__(self, *args, **kwargs) -> None:
    """This contructor should not be called by user"""
    dispatcher = OverloadDispatcher(__name__)

    @dispatcher.register(handle=Optional[IRHandle], dtype=DataType, shape=Optional[Iterable])
    def _(handle, dtype, shape=None):
        dtype = DataType.from_ir(ir.get_element_type(handle.get_type()))
        super(LocalTensor, self).__init__(dtype)
        self.handle = handle
        self.shape = self.ensure_shape(shape)

    @dispatcher.register(dtype=DataType, pos=Optional[TPosition], addr=RuntimeInt, tile_size=RuntimeInt)
    def _(dtype, pos=TPosition.VECIN, addr=0, tile_size=0):
        super(LocalTensor, self).__init__(dtype)
        builder = global_builder.get_ir_builder()
        self.shape = None
        self.handle = builder.create_asc_LocalTensorV2Op(
            ir.get_local_tensor_type(dtype.to_ir()),
            ir.TPosition.symbolize(pos),
            _mat(addr, KnownTypes.uint32).to_ir(),
            _mat(tile_size, KnownTypes.uint32).to_ir())

    @dispatcher.register(dtype=DataType)
    def _(dtype):
        super(LocalTensor, self).__init__(dtype)
        builder = global_builder.get_ir_builder()
        self.shape = None
        self.handle = builder.create_asc_LocalTensorOp(ir.get_local_tensor_type(dtype.to_ir()))

    dispatcher(*args, **kwargs)
```

要点：

- `OverloadDispatcher`（[python/asc/language/core/utils.py:41-133](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L41-L133)）按"参数名 → 类型标注"逐个匹配已注册分支，全都不匹配则抛出列出所有候选签名的 `RuntimeError`。这就是它能提供 `LocalTensor(dtype)` / `LocalTensor(dtype, pos, addr, tile_size)` 等多种入口的原因。
- 四参分支里 `ir.TPosition.symbolize(pos)` 把 Python 枚举转成 IR 属性；`_mat(addr, KnownTypes.uint32)` 强制地址按 `uint32` 物化——与 td 中 `UI32:$addr` 对齐。
- `tile_size` 参数在 IR 里名为 `tileSize`，单位是元素。

对应后端定义（同一文件的镜像目录）：

- 四参分支 → [include/ascir/Dialect/Asc/IR/Core/Tensor.td:161-166](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L161-L166)：

```tablegen
def AscendC_LocalTensorV2Op : AscendC_Op<"local_tensor_v2"> {
  let arguments = (ins AscendC_TPositionAttr:$pos, UI32:$addr, UI32:$tileSize);
  let results = (outs AscendC_LocalTensor:$result);
  let assemblyFormat = "$pos `,` $addr `,` $tileSize attr-dict `:` qualified(type($result))";
}
```

IR 节点名 **`asc.local_tensor_v2`**，三个操作数正是位置/地址/长度。

- 单参分支 → [include/ascir/Dialect/Asc/IR/Core/Tensor.td:154-159](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L154-L159)，IR 节点名 **`asc.local_tensor`**，不带任何参数。

**切片 `__getitem__`**——[python/asc/language/core/tensor.py:255-268](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L255-L268)：

结构与 `GlobalTensor.__getitem__` 几乎逐行相同，只有两处差异：调用的 builder 方法换成 `create_asc_LocalTensorSubIndexOp`，返回值用三参形式 `LocalTensor(handle, self.dtype, self.shape)` 重建以保留 shape。对应 IR 节点名 **`asc.local_tensor.subindex`**（[include/ascir/Dialect/Asc/IR/Core/Tensor.td:297-306](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L297-L306)，summary 同样写着 `operator[]`）。

01_add 里的双缓冲切片——[examples/01_add/add.py:49-61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49-L61)：

```python
for i in range(TILE_NUM * BUFFER_NUM):
    buf_id = i % BUFFER_NUM
    asc.data_copy(x_local[buf_id * tile_length:], x_gm[i * tile_length:], tile_length)
    ...
    asc.add(z_local[buf_id * tile_length:], x_local[buf_id * tile_length:],
            y_local[buf_id * tile_length:], tile_length)
```

同一个 `x_local` 被交替切成 `x_local[0:]` 与 `x_local[tile_length:]` 两半，配合 `buf_id` 的乒乓切换实现双缓冲：搬入写一半的同时计算读另一半。每次 `[...]` 都是一个新的 `asc.local_tensor.subindex` IR 节点。

**`from_ir`**——[python/asc/language/core/tensor.py:278-281](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L278-L281)：从 IR 句柄重建时，除了从 IR 类型反查 dtype，还会调 `ir.get_shape` 抽出静态 shape。这是 TQue 的 `alloc_tensor`/`deque` 能直接返回一个"活的" `LocalTensor` 的关键路径（u2-l6 见）。

**常用成员速查**（全部是"生成一个 IR 操作"的同一套路）：

| 方法 | 位置 | 一句话说明 |
| --- | --- | --- |
| `get_length()` | [tensor.py:290-295](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L290-L295) | 取数据长度（字节） |
| `get_position()` | [tensor.py:317-322](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L317-L322) | 取 TPosition 逻辑位置 |
| `get_size()` / `set_size(n)` | [tensor.py:331-342](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L331-L342) / [tensor.py:421-425](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L421-L425) | 元素粒度的长度查询/设置 |
| `reinterpret_cast(dtype)` | [tensor.py:377-389](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L377-L389) | 同地址同字节重解释为新类型，dtype 相同则直接返回自身 |
| `set_addr_with_offset(src, n)` | [tensor.py:395-399](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L395-L399) | 以 src 为基址偏移 n 个元素定义新地址 |
| `set_buffer_len(len)` | [tensor.py:405-409](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L405-L409) | 设置字节长度；官方建议 `operator[]` 切片后调用，便于编译器自动优化同步 |
| `get_value` / `set_value` | [tensor.py:359-365](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L359-L365) / [tensor.py:441-445](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L441-L445) | 标量读写（大量使用会拖慢性能） |
| `print(len)` / `to_file(name)` | [tensor.py:371-375](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L371-L375) / [tensor.py:447-450](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L447-L450) | CPU 调试打印 / dump 到文件 |

最后看变体 `LocalTensorAuto`（[python/asc/language/core/tensor.py:453-484](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L453-L484)）：它接收 `shape` 参数，静态 shape 全为 int 时把 shape 编进 IR 类型（`create_asc_LocalTensorAutoOp` 的重载类型带 shape）；含运行时维度时把各维作为操作数传入。后端定义为 [include/ascir/Dialect/Asc/IR/Ops.td:127](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Ops.td#L127) 的 `AscendC_LocalTensorAutoOp : APIOp<"local_tensor_auto", "LocalTensorAuto">`，即 IR 名 `asc.local_tensor_auto`，配套有 Pass 专门为它插入 tbuf/queue/allocation（见 Transforms，u6 展开）。

#### 4.3.4 代码实践

**实践目标**：亲手验证 01_add 的 UB 排布数学，理解 addr 单位是字节而 tile_size 单位是元素。

**操作步骤**：

1. 阅读 [examples/01_add/add.py:39-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L39-L47)，代入 `total_length=16384`、`USE_CORE_NUM=8`、`TILE_NUM=8`、`BUFFER_NUM=2`、`dtype=float32`；
2. 手算 `block_length`、`tile_length`、`buffer_size`，以及 `x_local`/`y_local`/`z_local` 三个构造调用的 `addr` 与 `tile_size` 实参；
3. 检查三个缓冲是否恰好铺满 `3 * buffer_size` 字节且互不重叠。

**需要观察的现象**：`tile_size` 实参都是 `tile_length * BUFFER_NUM`（元素），而 `addr` 实参是 `0`、`buffer_size`、`buffer_size + buffer_size`（字节）——两套单位并存于同一个构造调用。

**预期结果**：`block_length=2048`，`tile_length=128`，`buffer_size=128*2*4=1024` 字节；三个 addr 为 0 / 1024 / 2048，每个 `tile_size=256` 个元素（=1024 字节），正好首尾相接铺满 3072 字节。

#### 4.3.5 小练习与答案

**练习 1**：把 01_add 的 `z_local` 构造写成 `asc.LocalTensor(data_type, asc.TPosition.VECOUT, buffer_size, tile_length * BUFFER_NUM)`（漏乘一个 buffer_size），会发生什么？

**答案**：`z_local` 的 addr 与 `y_local` 相同（都是 `buffer_size`），两块缓冲在 UB 上完全重叠。IR 生成阶段不会报错（`create_asc_LocalTensorV2Op` 不校验重叠），但运行时 `y_local` 与 `z_local` 读写同一片内存，计算结果被互相覆盖，最终输出错误。这正是手动排布的风险所在，也是框架风格（TPipe/TQue）要解决的问题。

**练习 2**：`asc.local_tensor` 与 `asc.local_tensor_v2` 两个 IR 节点为什么都要存在？

**答案**：它们对应两种使用心智：`local_tensor_v2` 保留用户给的位置/地址/长度信息，服务手动排布风格（01_add）；`local_tensor` 只声明"这里需要一个本地张量"，把分配决策留给编译器后续 Pass（配合框架风格或自动分配）。前端构造函数的单参/四参重载（[tensor.py:236-251](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L236-L251)）分别映射到这两个节点。

**练习 3**：用 u1-l3 的检索链，找出 `set_addr_with_offset` 对应的 IR 节点名。

**答案**：从 [tensor.py:397-399](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L397-L399) 的 `create_asc_LocalTensorSetAddrWithOffsetOp` 出发，在 `include/ascir/Dialect/Asc/IR/Core/Tensor.td` 中找到 `def AscendC_LocalTensorSetAddrWithOffsetOp : APIOp<"local_tensor.set_addr_with_offset", "SetAddrWithOffset", ...>`（[Tensor.td:261-265](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L261-L265)），故 IR 节点名是 `asc.local_tensor.set_addr_with_offset`，第二列字符串 `"SetAddrWithOffset"` 即发射到 Ascend C 时的方法名。

---

### 4.4 TensorShape 与 ShapeInfo：两种"形状"

#### 4.4.1 概念说明

pyasc 里有两个名字很像的形状机制，务必分清：

| | `TensorShape` | `ShapeInfo` |
| --- | --- | --- |
| 定义位置 | [python/asc/language/core/types.py:469](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L469) | [python/asc/language/core/types.py:295](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L295) |
| 本质 | `Tuple[int, ...]` 的子类，纯 Python 编译期对象 | `IRValue` 子类，包装一个 IR 值 |
| 是否进入 IR | 否（只在前端做校验/记形状） | 是（对应 IR 类型 `asc.shape_info` 与 Ascend C 的 `ShapeInfo` 结构体） |
| 何时用 | 构造/重建 Tensor 时描述静态形状 | 设备侧运行时查询真实 shape（如 GM 里数据的实际维度） |

一句话：`TensorShape` 是**写代码时**前端手里的尺子；`ShapeInfo` 是**跑起来后**设备侧才知道的形状信息，二者一个活在编译期、一个活在设备运行期。

#### 4.4.2 核心流程

`TensorShape` 的构造归一化流程：

```text
TensorShape()                     -> 空形状 ()
TensorShape(5)                    -> (5,)
TensorShape([2, 3]) / (2, 3)      -> (2, 3)
TensorShape(another TensorShape)  -> 原样返回（不可变元组可直接复用）
每个元素都必须能 int() 转换，否则 TypeError
```

`ShapeInfo` 的使用流程：

```text
写入方向：ShapeInfo(shape_array, original_shape, data_format)
            -> create_asc_ConstructOp(asc_ShapeInfoType, [长度, 数组, ...], 类型表)
            -> tensor.set_shape_info(si)   生成 asc.*.set_shape_info
读取方向：si = tensor.get_shape_info()     生成 asc.*.get_shape_info
          si.shape(dim) / si.original_shape(dim)
          asc.get_shape_size(si)           各维累乘
```

#### 4.4.3 源码精读

**TensorShape**——[python/asc/language/core/types.py:469-523](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L469-L523)：

```python
class TensorShape(Tuple[int, ...]):
    ...
    def __new__(cls, *args):
        num_args = len(args)
        if num_args == 0:
            return cls.new_impl(tuple())
        if num_args > 1:
            return cls.new_impl(tuple(cls.as_int(a) for a in args))
        arg = args[0]
        if arg is None:
            return cls.new_impl(tuple())
        if isinstance(arg, TensorShape):
            return arg
        if isinstance(arg, Iterable):
            return cls.new_impl(tuple(cls.as_int(a) for a in arg))
        return cls.new_impl((cls.as_int(arg), ))
```

- [types.py:495-512](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L495-L512)：因为 tuple 是不可变类型，自定义构造走 `__new__` 而非 `__init__`；多个单独参数、单个可迭代对象、单个整数、None 各有归一化路径。
- [types.py:514-519](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L514-L519)：`as_int` 用 `int(value)` 尝试转换，失败抛出带原类型名的 `TypeError`——这保证 `TensorShape` 里永远是编译期确定的 Python int。

它唯一的消费者是 `BaseTensor.ensure_shape`（见 4.1.3）以及 `LocalTensor.from_ir` 抽出的 `self.shape`、`LocalTensorAuto` 的静态 shape 分支（[tensor.py:470-477](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L470-L477)）。当 `self.shape` 已知时，`get_size()` 直接 `itertools.accumulate` 编译期累乘（[tensor.py:342](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L342)），不生成任何 IR。

**ShapeInfo**——[python/asc/language/core/types.py:295-366](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L295-L366)：

```python
class ShapeInfo(IRValue):
    """ShapeInfo用来存放LocalTensor或GlobalTensor的shape信息。"""
    ...
    def __init__(self, shape=None, original_shape=None, data_format=None, handle=None):
        if handle is not None:
            self.handle = handle
            return
        operands = []
        types = []
        builder = global_builder.get_ir_builder()
        if shape is not None:
            builder.set_emit_as_unsigned(shape.to_ir().get_defining_op())
            shape_len = _mat(len(shape), KnownTypes.int8).to_ir()
            operands += [shape_len, shape.to_ir()]
            ...
        types_attr = builder.get_type_array_attr(types)
        self.handle = builder.create_asc_ConstructOp(builder.get_asc_ShapeInfoType(), operands, types_attr)
```

- [types.py:314-337](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L314-L337)：与 Tensor 不同，`ShapeInfo` 是"参数包"式类型——shape 数组（`Array` 类型）、原始 shape、`DataFormat` 枚举一起塞进一个 `create_asc_ConstructOp` 聚合构造操作。这镜像了 Ascend C 中 `ShapeInfo` 是一个结构体而非类模板。
- [types.py:347-352](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L347-L352) 与 [types.py:358-363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L358-L363)：`shape(dim)` / `original_shape(dim)` 分别生成 `asc.shape_info.shape` / `asc.shape_info.original_shape`（td 见 [Tensor.td:27-37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L27-L37)）。
- 配套的自由函数 `asc.get_shape_size(si)`（[types.py:374-405](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L374-L405)）生成 `asc.get_shape_size`，返回各维累乘，等价于设备侧 `GetShapeSize(const ShapeInfo&)`。

与 Tensor 的联动接口：`GlobalTensor.get_shape_info`（[tensor.py:109-114](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L109-L114)）、`set_shape_info`（[tensor.py:180-184](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L180-L184)）与 `LocalTensor` 的同名方法（[tensor.py:324-329](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L324-L329)、[tensor.py:411-415](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L411-L415)）。

#### 4.4.4 代码实践

**实践目标**：通过源码阅读，确认 `TensorShape` 不生成 IR、而 `ShapeInfo` 生成 IR。

**操作步骤**：

1. 通读 [python/asc/language/core/types.py:469-523](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L469-L523)，统计 `TensorShape` 中出现 `global_builder` 或 `create_asc_` 的次数；
2. 再通读 [python/asc/language/core/types.py:314-337](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L314-L337)，做同样统计；
3. 追一个行为差异：`t.get_size()` 在 `t.shape is None` 与 `t.shape` 已知时分别走哪条路（[tensor.py:336-342](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L336-L342)）。

**需要观察的现象**：`TensorShape` 全文没有任何 builder 调用；`ShapeInfo.__init__` 里有两处 `create_asc_`/`get_ir_builder`。

**预期结果**：`TensorShape` 次数为 0，`ShapeInfo` 至少 2 处（`get_ir_builder` 与 `create_asc_ConstructOp`）；`get_size()` 在 shape 已知时走 `itertools.accumulate` 编译期累乘（不生成 IR），shape 未知时生成 `asc.local_tensor.get_size` IR。这是纯阅读实践，结论可直接从源码得出。

#### 4.4.5 小练习与答案

**练习 1**：`LocalTensor.from_ir` 里 `self.shape = self.ensure_shape(shape)` 用到的 shape 来自哪里？为什么 GlobalTensor 的 from_ir 不做这件事？

**答案**：来自 IR 类型本身——[tensor.py:280-281](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L280-L281) 调 `ir.get_shape(handle.get_type())` 抽取静态 shape（`LocalTensorAuto` 静态分支会把 shape 编进类型）。`GlobalTensor` 描述的是 Host 传入的动态数据段，前端没有可靠静态形状，其 `shape` 恒为 `None`（[tensor.py:53-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L53-L54)），运行期形状要靠 `get_shape_info()` 走 `ShapeInfo` 获取。

**练习 2**：`ShapeInfo` 的构造为什么用 `create_asc_ConstructOp`（聚合构造），而不是像 Tensor 那样每种方法一个专用 Op？

**答案**：`ShapeInfo` 在 Ascend C 中是一个普通值类型（结构体），不是带成员方法的 Service 对象；它的"构造"就是把一组值（各维长度、数组、格式）打包成一个聚合值。`ConstructOp` 正是 pyasc 表达"值聚合"的通用节点（本文件中 `BinaryRepeatParams`、`DataCopyParams` 等参数包也都用它），只有"行为"（如 `shape(dim)` 查询）才需要专属 Op。

## 5. 综合实践

把本讲四个模块串成一个可运行任务（即大纲指定的实践）：**写一个最小核函数，覆盖 GlobalTensor 二段式创建、两个不同 position 的 LocalTensor、以及切片操作，dump IR 后找到切片对应的 IR 节点名。**

### 5.1 编写示例（示例代码）

在仓库根目录新建 `slice_demo.py`（注意：这是练习文件，不属于仓库源码）：

```python
import torch
import asc
import asc.runtime.config as config
import asc.lib.runtime as rt


@asc.jit
def slice_kernel(x: asc.GlobalAddress, out: asc.GlobalAddress, length: int):
    # 模块 1+2：GlobalTensor 空构造，再 set_global_buffer 绑定 GM 地址
    x_gm = asc.GlobalTensor()
    out_gm = asc.GlobalTensor()
    x_gm.set_global_buffer(x, length)
    out_gm.set_global_buffer(out, length)

    # 模块 3：两个不同 position 的 LocalTensor，手工排布 UB
    # a 在 VECIN，从字节 0 开始；b 在 VECCALC，紧接 a 之后
    a_local = asc.LocalTensor(x.dtype, asc.TPosition.VECIN, 0, length)
    b_local = asc.LocalTensor(x.dtype, asc.TPosition.VECCALC,
                              length * x.dtype.sizeof(), length)

    # 模块 4（切片）：[] 语法生成 subindex IR 节点
    asc.data_copy(a_local[0:], x_gm[0:], length)
    asc.data_copy(b_local[0:], a_local[0:], length)
    asc.data_copy(out_gm[0:], b_local[0:], length)


def main():
    config.set_platform(config.Backend.Model)
    length = 1024
    x = torch.rand(length, dtype=torch.float32)
    out = torch.zeros(length, dtype=torch.float32)
    slice_kernel[1, rt.current_stream()](x, out, length)
    assert torch.allclose(out, x)
    print("slice demo passed")


if __name__ == "__main__":
    main()
```

### 5.2 运行并导出 IR

```bash
export PYASC_DUMP_PATH=/tmp/pyasc_dump
python3 slice_demo.py
```

（Model 仿真模式无需 NPU；若提示缺少平台参数，参考 [examples/01_add/add.py:94-106](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L94-L106) 补 `-r` 参数。）

### 5.3 检查点（需观察的现象与预期结果）

1. **程序输出**：打印 `slice demo passed`，`out` 与 `x` 逐元素相等（数据经过 GM → VECIN → VECCALC → GM 三跳仍未损坏）。**待本地验证**。
2. **dump 目录**：`/tmp/pyasc_dump` 下出现以 kernel 命名的 `.mlir` 与 `.cpp` 文件（命名规则见 u1-l5）。**待本地验证**。
3. **IR 节点名**（重点，请在 **codegen.mlir** 即 Pass 前的 IR 中查找）：

```bash
grep -n "subindex" /tmp/pyasc_dump/*.mlir
grep -nE "local_tensor_v2|global_tensor" /tmp/pyasc_dump/*.mlir
```

预期分别命中：

- `asc.local_tensor.subindex` × 3（三个 `a_local[0:]` / `b_local[0:]` 类切片，local 侧）；
- `asc.global_tensor.subindex` × 2（`x_gm[0:]`、`out_gm[0:]`）；
- `asc.local_tensor_v2` × 2（两个四参 LocalTensor，分别带 `VECIN` 与 `VECCALC` 位置属性）；
- `asc.global_tensor` × 2 与 `asc.global_tensor.set_global_buffer` × 2。

4. **对照后端定义**：把 grep 到的每个节点名回到 [include/ascir/Dialect/Asc/IR/Core/Tensor.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td) 中找到对应 `def AscendC_...`，核对操作数/结果与 4.2/4.3 的分析一致。
5. **思考题**：为什么建议看 `codegen.mlir` 而不是 `ascir.mlir`？——因为 Pass 之后 UB 分配会被 `MaterializeTensor`/`HoistUBAllocation` 等 Pass 改写（u6-l2 展开），`local_tensor_v2` 的形态可能已变；切片 subindex 的表现也以 Pass 前的 IR 最贴近本讲源码逻辑。

### 5.4 延伸（可选）

把 `a_local[0:]` 改成 `a_local[length // 2:]` 并同步调整三处 `data_copy` 的 count，重新 dump，观察 subindex 节点的 index 操作数变化——验证"切片偏移以元素为单位"。

## 6. 本讲小结

- pyasc 的 Tensor（`BaseTensor` 及其子类）是 **IR 值的包装**：只有 `dtype` + `handle`，不持有数据；所有方法调用都是向 IR 追加操作，`@require_jit` 保证它们只在 kernel 编译期发生。
- `GlobalTensor` 是 GM 视图，**二段式**创建：`asc.GlobalTensor()` 空壳 + `set_global_buffer(addr, size)` 从 Host 指针取类型并生成 `asc.global_tensor` + `asc.global_tensor.set_global_buffer` 两个 IR 节点；`buffer_size` 单位是元素。
- `LocalTensor` 是 UB 缓冲，四参构造 `LocalTensor(dtype, pos, addr, tile_size)` 生成 `asc.local_tensor_v2`；**addr 单位是字节、tile_size 单位是元素**，手动风格下用户自己负责缓冲不重叠（01_add 的 0/buffer_size/2×buffer_size 排布）。
- 切片 `t[k:]` 只允许"有 start、无 stop/step"，生成 `asc.global_tensor.subindex` / `asc.local_tensor.subindex`，语义是"偏移 \( k \) 个元素的新视图"（对应 Ascend C `operator[]`），返回新对象、原对象不变；圆括号 `t(k)` 则生成 `.bracket` 孪生节点。
- `TensorShape` 是编译期 `Tuple[int, ...]`（不进 IR），`ShapeInfo` 是进 IR 的运行时形状（`ConstructOp` 聚合 + `get_shape_info`/`shape(dim)`/`get_shape_size` 查询），一个服务前端校验、一个服务设备侧真实形状。
- 检索规律再次生效：`create_asc_XxxOp`（Python）→ `def AscendC_XxxOp`（Core/Tensor.td）→ IR 节点 `asc.xxx`，前端与后端目录一一镜像。

## 7. 下一步学习建议

本讲刻意回避了一个问题：`buf_id * tile_length`、`x + offset` 这些**运行时表达式**凭什么能当切片下标用？答案是 `RuntimeInt`/`PlainValue`/`materialize_ir_value` 组成的 IRValue 延迟求值体系——**下一讲 u2-l3（IRValue 体系）** 将正面拆解它，建议先复习本讲 2.2 节，再带着"每个子表达式是什么类型"的问题去读 [python/asc/language/core/ir_value.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py)。

之后的两条线：

- 想摆脱手工排布：学 **u2-l6（TPipe/TQue）**，看 `LocalTensor.from_ir` 这个洞口如何被队列的 `alloc_tensor`/`deque` 使用；
- 想看 Tensor 在 IR 里被 Pass 如何改写：提前翻 `lib/Dialect/Asc/Transforms/MaterializeTensor.cpp` 与 `HoistUBAllocation.cpp`（u6-l2 系统讲解）。

阅读源码时可以带着两个问题：`set_buffer_len` 为什么官方建议在 `operator[]` 后调用（提示：便于编译器自动同步优化，u6-l3 的 InsertSync 会用到长度信息）；`LocalTensorAuto` 的 shape 何时进类型、何时进操作数（提示：看 [tensor.py:473-482](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L473-L482) 的两个分支条件）。
