# 零拷贝互操作：as_tensor 与 DLPack

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CV-CUDA Python 绑定中 DLPack 协议的**实现位置**与**完整数据流**：一个 cupy/torch/pycuda 数组从进入 `cvcuda.as_tensor` 到变成 `nvcv.Tensor`，中间经过了哪些类、哪些检查。
2. 熟练使用 `cvcuda.as_tensor` 把 numpy（经中转）、cupy、PyTorch 的 GPU 张量**零拷贝**包装成 `cvcuda.Tensor`，并用 `tensor.cuda()` 反向导出回这些框架。
3. 准确识别哪些包装条件会**失败**：设备不在 CUDA 上、dtype 不受支持、stride 不是元素大小整数倍、秩越界、layout 标签与 shape 秩不一致等，并能对照源码说出每条报错的出处。
4. 理解包装（wrap）张量的**生命周期**：谁持有谁、为什么删掉原数组后张量仍然有效。

本讲是第二单元的收官：u2-l1 讲了 Tensor 的 shape/layout/stride 模型，u2-l2 讲了 dtype 与 ImageFormat，u2-l3 讲了变长批。本讲回答的问题是——**这些张量如何与 Python 生态里的其他框架共享同一块显存，而不发生任何拷贝**。

## 2. 前置知识

### 2.1 为什么需要"互操作协议"

GPU 显存里的数据本质上就是一段设备指针 + 元数据（形状、类型、步长）。如果每个框架（cupy、PyTorch、CV-CUDA）都自定义一套"导入别人数组"的接口，组合数会爆炸。所以社区形成了两个标准协议：

- **DLPack**：一个跨框架的张量交换协议。核心是一个 C 结构体 `DLTensor`（含数据指针、设备、dtype、shape、strides），包在 Python 的 `PyCapsule`（名为 `dltensor` 或 `dltensor_versioned`）里传递。任何对象只要实现 `__dlpack__()` 和 `__dlpack_device__()` 两个方法，就能被别人导入。
- **CUDA Array Interface（CAI）**：CUDA 生态专用的协议，对象暴露一个 `__cuda_array_interface__` 字典（含 `shape`、`typestr`、`data`、`strides`、`version` 等字段）。cupy、pycuda 都实现了它。

CV-CUDA 两个协议都支持。理解这两个协议的差别是本讲的关键之一：

| 维度 | DLPack | CAI |
|---|---|---|
| 载体 | PyCapsule（C 结构体） | Python dict |
| dtype 表达 | `DLDataType{code, bits, lanes}` | numpy 风格 typestr 如 `"<f4"` |
| strides 单位 | **元素个数** | **字节** |
| 设备表达 | `DLDevice{device_type, device_id}` | 隐含为 CUDA（从指针推断） |
| 是否支持 CPU 张量 | 是（可表达 kDLCPU） | 否（只描述 CUDA 内存） |

### 2.2 PyCapsule 与 pybind11 type_caster

`PyCapsule` 是 CPython 提供的"装 C 指针的容器"，DLPack 用它把 C 结构体安全地传过 Python 世界。DLPack 约定：消费者取出张量后要把胶囊改名为 `used_dltensor`，防止被二次消费。

pybind11 的 **type_caster** 是"把 Python 对象翻译成 C++ 参数"的挂钩点。CV-CUDA 给自己的 `ExternalBuffer` 类注册了一个自定义 type_caster，使得任何实现了上述协议的对象都能直接出现在 `as_tensor(buffer, ...)` 的参数位置上——这就是"入口"所在。

### 2.3 承接 u2-l1：stride 与 layout

u2-l1 建立的两个事实在本讲反复用到：

- nvcv.Tensor 的 stride 以**字节**为单位，地址公式为 \(\text{addr} = \text{basePtr} + \sum_d i_d \cdot s_d\)；而 DLPack 的 stride 以**元素**为单位。两者换算时必须乘上每个元素的字节数。
- layout 只是"维度标签"，`as_tensor` 默认不给包装张量贴标签（`layout` 为 `None`），需要调用者显式传入。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [python/mod_cvcuda/nvcv/ExternalBuffer.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp) | 互操作**入口**：识别 CAI/DLPack 对象、校验设备与指针、解析胶囊、以及反向导出 `__dlpack__`/`__cuda_array_interface__` |
| [python/mod_cvcuda/nvcv/DLPackUtils.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp) | `DLTensor` 的 RAII 封装 `DLPackTensor`，以及 DLDataType ↔ nvcv::DataType 的双向翻译 |
| [python/mod_cvcuda/nvcv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp) | `as_tensor` 的绑定与 `Tensor::Wrap`（导入路径核心）、`Tensor::cuda`（导出路径核心） |
| [python/mod_cvcuda/nvcv/Tensor.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.hpp) | `Tensor::Key` 定义（包装张量在对象缓存中的特殊键） |
| [samples/interoperability/numpy_interop.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/numpy_interop.py) | 官方示例：numpy 数据经四种途径上 GPU 再包装 |
| [samples/interoperability/cupy_interop.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cupy_interop.py) | 官方示例：cupy ↔ CV-CUDA 双向最小流程 |
| [samples/interoperability/pytorch_interop.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pytorch_interop.py) | 官方示例：PyTorch ↔ CV-CUDA 双向最小流程 |
| [tests/cvcuda/python/test_tensor.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py) | 官方测试：wrap/export 的行为断言，是"契约文档" |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **互操作的三座桥**——入口在哪，协议探测顺序是什么。
2. **导入路径**——`ExternalBuffer` 如何把外部缓冲翻译成 `DLTensor`，哪些条件会失败。
3. **从 DLTensor 到 nvcv.Tensor**——`Tensor::Wrap` 如何零拷贝落地，生命周期如何保证。
4. **导出路径**——`tensor.cuda()` 如何把数据还给其他框架。

### 4.1 模块一：互操作的三座桥

#### 4.1.1 概念说明

`cvcuda.as_tensor(buffer, layout=None)` 的第一个参数在 Python 侧看起来能接受"任何数组"。这不是魔法，而是三座桥依次尝试的结果：

1. **桥 A（类型完全匹配）**：参数本身就是 `ExternalBuffer`（CV-CUDA 自己导出过的缓冲），直接复用。
2. **桥 B（CAI）**：对象有 `__cuda_array_interface__` 属性 → 解析该字典。
3. **桥 C（DLPack）**：对象有 `__dlpack__` 方法 → 索取胶囊并解析。

三座桥都不通时，`load` 返回 `false`，pybind11 报出经典的 `TypeError: incompatible function arguments`。

#### 4.1.2 核心流程

```
cvcuda.as_tensor(cupy_array)
        │
        ▼
pybind11 要把实参转成 ExternalBuffer        ← type_caster<ExternalBuffer>::load
        │
        ├─ 实参就是 ExternalBuffer？ ──是──► 直接取 shared_ptr
        │
        └─ 否 ──► ExternalBuffer::load(obj)
                ├─ hasattr(obj, "__cuda_array_interface__") ──► loadCudaArrayInterface
                ├─ hasattr(obj, "__dlpack__")               ──► loadDLPack
                └─ 都没有                                    ──► return false → TypeError
```

#### 4.1.3 源码精读

入口挂钩在文件末尾的 pybind11 命名空间里。[python/mod_cvcuda/nvcv/ExternalBuffer.cpp:780-801](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L780-L801)：先判断实参类型是否就是 `ExternalBuffer` 本尊；不是则现场 `make_shared<ExternalBuffer>()` 并调用 `load()` 去尝试解析。这段代码就是"任何数组都能传给 as_tensor"的实现位置。

协议探测顺序在 [python/mod_cvcuda/nvcv/ExternalBuffer.cpp:517-536](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L517-L536)：CAI 优先于 DLPack。注意 cupy 数组**两个协议都实现了**，所以它会走 CAI 分支；而 PyTorch 张量没有 `__cuda_array_interface__`、只有 `__dlpack__`，所以必然走 DLPack 分支。这也解释了官方测试为什么要专门构造"只有单一协议"的假对象来分别测试两条路径。

`as_tensor` 本身在哪里绑定？在 [python/mod_cvcuda/nvcv/Tensor.cpp:546-549](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L546-L549)：`m.def("as_tensor", ...)` 有两个重载——一个包装任意缓冲（`Tensor::Wrap`，本讲主角），另一个把 `cvcuda.Image` 包装成张量（`Tensor::WrapImage`，u2-l2 已提过图像语义在此被丢弃）。两个入口都被 `NvtxTrace` 包裹以便性能分析（u7-l4 会讲）。

#### 4.1.4 代码实践

**实践目标**：用 `hasattr` 亲手验证"三座桥"的探测结果，把抽象流程落到可观察的事实上。

**操作步骤**（示例代码，需在装有 cvcuda、cupy、torch 的 GPU 环境运行）：

```python
# 示例代码：探测各框架实现的互操作协议
import numpy as np, cupy, torch

objs = {
    "numpy(cpu)": np.zeros((4, 4), np.float32),
    "cupy(gpu)":  cupy.zeros((4, 4), cupy.float32),
    "torch(cpu)": torch.zeros(4, 4),
    "torch(gpu)": torch.zeros(4, 4).cuda(),
}
for name, o in objs.items():
    print(f"{name:12s} CAI={hasattr(o, '__cuda_array_interface__')} "
          f"DLPack={hasattr(o, '__dlpack__')}")
```

**需要观察的现象**：四个对象各自打了什么勾。

**预期结果**（待本地验证）：`cupy(gpu)` 两项都是 True（走 CAI 分支）；两个 torch 只有 DLPack 是 True；numpy 的 DLPack 是 True（numpy ≥ 1.22 实现了 `__dlpack__`）但 CAI 是 False——注意 numpy 有 DLPack 桥不代表能包装成功，它的设备是 CPU，会在 4.2 讲的设备检查处被拒绝。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ExternalBuffer::load` 要先探测 CAI 再探测 DLPack，反过来会有问题吗？

**答案**：功能上两条路径等价时结果一致，顺序主要是实现选择——CAI 是纯 Python 字典，解析无需回调对方代码；DLPack 需要调用对方的 `__dlpack__()` 方法并处理 v0/v1 两种胶囊。先试便宜的、无副作用的路径。对调用者可观察的差别在于：一个对象只实现其中一个协议时（torch 只有 DLPack，自定义 CAI 对象只有 CAI），只要有一条桥通就能包装，顺序不影响能否成功，只影响走哪条实现。

**练习 2**：`cvcuda.as_tensor(some_int)` 会发生什么？

**答案**：`int` 既没有 `__cuda_array_interface__` 也没有 `__dlpack__`，`ExternalBuffer::load` 返回 `false`（见 [python/mod_cvcuda/nvcv/ExternalBuffer.cpp:517-536](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L517-L536) 的最后 `return false`），pybind11 抛出 `TypeError`，提示参数无法转换成 `cvcuda.ExternalBuffer`。

### 4.2 模块二：导入路径——ExternalBuffer 的翻译与校验

#### 4.2.1 概念说明

`ExternalBuffer` 是"海关":把外来数组统一翻译成内部的 `DLTensor` 描述（一个数据指针 + 设备 + dtype + shape + strides），并做安全检查。它不拷贝任何像素数据——它只整理"这块内存的说明书"。

两条翻译路线：

- **CAI 路线**：字典里拿 `data[0]` 当指针、`typestr` 当 dtype、`shape`/`strides` 当形状步长；设备类型直接认定 `kDLCUDA`，设备号从指针反查。
- **DLPack 路线**：先问 `__dlpack_device__()` 确认设备，再调 `__dlpack__(stream=1, max_version=(1,0))` 索取胶囊；胶囊里已是现成的 `DLTensor`。

#### 4.2.2 核心流程

```
loadCudaArrayInterface(obj):                    loadDLPack(obj):
  校验必需字段齐全(shape/typestr/data/version)     dev = obj.__dlpack_device__()
  version >= 2 ?                                  IsCudaAccessible(dev) ? ──否──► 抛错
  ptr = iface["data"][0]                          cap = obj.__dlpack__(stream=1,
  CheckValidCUDABuffer(ptr)  ← 设备检查                 max_version=(1,0))
  dtype = typestr → numpy dtype → nvcv            解析胶囊(v1 "dltensor_versioned"
  shape/strides(字节→元素, 须整除 itemsize)             或 v0 "dltensor")，取出 DLTensor
  解析 CAI v3 stream 字段(生产者流)                 记录生产者流 = legacy 默认流
```

**失败条件速查表**（对应学习目标三，全部可对照源码）：

| # | 失败条件 | 检查位置（源码） | 报错/结果 |
|---|---|---|---|
| 1 | 设备是 CPU（如直接包装 numpy 数组） | [ExternalBuffer.cpp:600-607](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L600-L607) | 抛 `Only CUDA-accessible memory buffers can be wrapped` |
| 2 | 两个协议都没有 | [ExternalBuffer.cpp:517-536](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L517-L536) | pybind11 `TypeError` |
| 3 | 空指针缓冲 | [ExternalBuffer.cpp:53-67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L53-L67) | 抛 `NULL CUDA buffer not accepted` |
| 4 | 指针不是 CUDA 可寻址内存 | 同上 | 抛 `Buffer is not CUDA-accessible` |
| 5 | CAI 版本 < 2 或 ndim < 1 | [ExternalBuffer.cpp:547-576](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L547-L576) | 返回 false → 最终 TypeError |
| 6 | dtype lanes > 4（如 numpy 结构化多通道） | [DLPackUtils.cpp:315-317](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L301-L317) | 抛 `DLPack buffer's data type must have at most 4 lanes` |
| 7 | dtype code 不支持（如 bfloat16） | [DLPackUtils.cpp:347-348](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L332-L349) | 抛 `Data type code not supported, must be Int, UInt, Float, Complex or Bool` |
| 8 | CAI stride（字节）不是元素大小整数倍 | [ExternalBuffer.cpp:394-410](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L394-L410) | 抛 `Stride must be a multiple of the element size in bytes` |
| 9 | 秩为 0 或超过 15 | [Tensor.cpp:122-131](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L122-L131) | 抛 `Number of dimensions must be between 1 and 15, not N` |
| 10 | 显式 layout 长度 ≠ shape 秩 | TensorShape 秩一致性检查（u2-l1；[TensorDataImpl.hpp:41-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/detail/TensorDataImpl.hpp#L41-L57) 构造 TensorShape 时触发） | 抛 TensorShape 异常 |

其中第 9 条的上界 15 来自宏 `NVCV_TENSOR_MAX_RANK`，定义在 [src/nvcv/src/include/nvcv/TensorLayout.h:34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.h#L34)。

#### 4.2.3 源码精读

**(a) 设备准入**。[python/mod_cvcuda/nvcv/DLPackUtils.cpp:280-291](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L280-L291) 定义了"什么设备算 CUDA 可访问"：`kDLCUDA`（显存）、`kDLCUDAHost`（页锁定主机内存，可被 DMA）、`kDLCUDAManaged`（统一内存）三种放行，其余（包括最常见 的 `kDLCPU`）一律拒绝。这就是 numpy 数组不能直接包装的根本原因——不是协议没接上，而是设备检查主动拒绝。

**(b) CAI 解析**。[python/mod_cvcuda/nvcv/ExternalBuffer.cpp:538-596](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L538-L596)：先查必需字段（[L428-431](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L428-L431)）与版本号，然后 `CheckValidCUDABuffer` 用 `cudaPointerGetAttributes` 验证指针真的登记在 CUDA 里（源码注释同时处理了失败时清理 sticky error 的细节）。strides 若缺失，按 CAI 规范补成紧凑行主序（[ExternalBuffer.cpp:394-426](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L394-L426) 的尾部循环）。字节 stride 除以 itemsize 换算成元素 stride——除不尽即失败条件 #8。

**(c) DLPack 胶囊索取**。[python/mod_cvcuda/nvcv/ExternalBuffer.cpp:433-445](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L433-L445)：CV-CUDA 以消费者身份请求 `__dlpack__(stream=1, max_version=(1,0))`；若对方是不认识 `max_version` 的老式（v0）生产者，捕获异常后去掉该参数重试。随后 [ExternalBuffer.cpp:626-650](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L626-L650) 按胶囊名分流：`dltensor_versioned`（v1）或 `dltensor`（v0），取出 `DLTensor` 后**立即把胶囊改名**为 `used_dltensor*`，遵守 DLPack 的"一次性消费"约定。

**(d) dtype 双向翻译**。[python/mod_cvcuda/nvcv/DLPackUtils.cpp:293-352](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L293-L352) 把 `DLDataType{code,bits,lanes}` 翻译成 u2-l2 学过的 `nvcv::DataType = DataKind × Packing`：`lanes` 映射成 1~4 通道的 swizzle（`S_X000`/`S_XY00`/`S_XYZ0`/`S_XYZW`），每个 lane 占 `bits` 位；`code` 映射成 DataKind（Bool 归入 SIGNED）。反向翻译在 [DLPackUtils.cpp:354-390](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L354-L390)，并要求所有通道位宽相同（`All lanes must have the same bit depth`）。注意 numpy 的 `float64`、`complex64` 都能通过这张映射表——官方测试 [tests/cvcuda/python/test_tensor.py:86-98](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L86-L98) 的参数表里明确包含它们，但**算子层未必支持每种 dtype**（各算子的支持矩阵见其 C 头文件 Limitations）。

**(e) 生产者流记录**。两条路径都会记录"这块数据是谁在哪条流上生产的"：CAI 路径解析 v3 的 `stream` 字段（[ExternalBuffer.cpp:103-149](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L103-L149)），DLPack 路径因为自己请求了 `stream=1` 就记下 legacy 默认流（[ExternalBuffer.cpp:613-624](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L613-L624)）。这是为跨流正确性埋的伏笔，4.3 会看到它的用途，完整机制在 u4-l1 展开。

#### 4.2.4 代码实践

**实践目标**：亲手触发失败条件 #1 与 #8，把"限制条件"变成看过的报错。

**操作步骤**（示例代码）：

```python
# 示例代码：触发两类包装失败
import numpy as np, cupy, cvcuda

# 失败 1：直接包装 CPU numpy 数组
cpu = np.zeros((4, 4), np.float32)
try:
    cvcuda.as_tensor(cpu)
except Exception as e:
    print("numpy 直接包装失败:", type(e).__name__, e)

# 失败 2：构造字节 stride 不整除 itemsize 的 CAI 字典（float32, itemsize=4, stride=6B）
gpu = cupy.zeros((2, 4), cupy.float32)   # 保证缓冲存在且被引用
fake = type("Fake", (), {})()
fake.__cuda_array_interface__ = {
    "shape": (2, 4), "typestr": "<f4",
    "data": (gpu.__cuda_array_interface__["data"][0], False),
    "strides": (6, 4),        # 6 字节不是 4 的倍数 → 触发条件 #8
    "version": 3,
}
try:
    cvcuda.as_tensor(fake)
except Exception as e:
    print("非法 stride 失败:", type(e).__name__, e)
```

**需要观察的现象**：两条报错的原文。

**预期结果**（待本地验证）：第一条得到 `Only CUDA-accessible memory buffers can be wrapped`（如果 numpy 版本过老没有 `__dlpack__`，则会变成 `TypeError`，对应条件 #2——两种结果都值得记录）；第二条得到 `Stride must be a multiple of the element size in bytes`。注意 fake 对象必须持有对 `gpu` 的引用，否则指针悬空（本例中 `gpu` 变量在本作用域内存活，故安全）。

#### 4.2.5 小练习与答案

**练习 1**：torch 的 CPU 张量有 `__dlpack__`，`cvcuda.as_tensor(torch.zeros(4))` 会走到哪一步失败？

**答案**：走桥 C 进入 `loadDLPack`，先调用 `__dlpack_device__()` 得到 `(kDLCPU, 0)`，在 [ExternalBuffer.cpp:600-607](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L600-L607) 处 `IsCudaAccessible(kDLCPU)` 为 false，抛出与 numpy 相同的错误。设备检查发生在解析胶囊**之前**，所以连数据指针都还没碰。

**练习 2**：为什么 CAI 路径必须检查 `cudaPointerGetAttributes`，而 DLPack 路径的 `CheckValidCUDABuffer` 在另一个构造函数里？

**答案**：CAI 的字典只给一个裸整数地址，设备号也是"从指针推断"（源码里两处 REVISIT 注释承认了这一点），所以必须用 CUDA runtime 反查指针属性来防伪造/防错误；DLPack 的胶囊自带 `DLDevice` 描述，设备检查可以基于 `__dlpack_device__` 提前完成，指针校验则由 `ExternalBuffer` 构造函数统一兜底（[ExternalBuffer.cpp:474-487](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L474-L487)）。

**练习 3**：一个 `shape=(3,5,7,2,4,2,5)` 的 7 维 cupy 数组能包装成功吗？

**答案**：能。秩 7 在 1~15 之间，dtype 合法即可。官方测试 [tests/cvcuda/python/test_tensor.py:265-282](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L265-L282) 甚至给它贴了自定义 layout `"abcdefg"`——layout 标签内容不校验语义，长度等于秩即可。

### 4.3 模块三：从 DLTensor 到 nvcv.Tensor——Tensor::Wrap

#### 4.3.1 概念说明

`ExternalBuffer` 只产出"说明书"（`DLTensor`），真正把它变成 `cvcuda.Tensor` 的是 [Tensor::Wrap](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L168-L203)。它做三件事：

1. 把 `DLTensor` 的元数据逐字段抄进 `NVCVTensorData`（C 层数据描述），指针原样保留——**零拷贝发生在这里**；
2. 用 `nvcv::TensorWrapData` 在这块外部内存上"挂名"成一个张量，不分配任何显存；
3. 处理生命周期（引用持有）与缓存身份（wrapper key）。

#### 4.3.2 核心流程

```
DLTensor ──FillNVCVTensorData──► NVCVTensorData{dtype, layout?, rank, shape[],
                                        bufferType=STRIDED_CUDA,
                                        strides[](字节), basePtr}
                    │
                    ▼
        nvcv::TensorWrapData(data)   ← 不分配内存，只登记外部缓冲
                    │
                    ▼
        Tensor(data, 持有 ExternalBuffer 的 py::object)
                    │
                    ├─ 用包装键加入对象缓存（便于安全销毁；但包装张量永不复用）
                    └─ 若生产者流已知 → seedLastStream(生产者流, 设备)
```

关键换算：DLPack 的元素 stride → nvcv 的字节 stride，\( s_d^{byte} = s_d^{elem} \times \lceil bits \times lanes / 8 \rceil \)。

#### 4.3.3 源码精读

**(a) 逐字段翻译**。[python/mod_cvcuda/nvcv/Tensor.cpp:107-159](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L107-L159) 的 `FillNVCVTensorData`：dtype 经 `ToDType(ToNVCVDataType(...))` 规范化；layout 只有调用者显式传了才写入，否则保持默认（无标签）；秩检查（失败条件 #9）与设备检查（`Only CUDA-accessible tensors are supported for now`，与 4.2 的检查互为冗余防线）都在这里；最后 `basePtr = tensor.data + tensor.byte_offset`——注意 DLPack 允许 `byte_offset` 非零，nvcv 直接把它折进基址指针，这也是零拷贝的一个细节。

**(b) Wrap 主体与生命周期**。[python/mod_cvcuda/nvcv/Tensor.cpp:168-203](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L168-L203)。第 181-182 行 `new Tensor(data, py::cast(buffer.shared_from_this()))`：新建的 `cvcuda.Tensor` 通过 `m_wrappedObject` 成员**持有 ExternalBuffer**，而 ExternalBuffer 又持有原始 Python 对象（`m_wrappedObj`）。于是引用链 `Tensor → ExternalBuffer → cupy数组/torch张量` 保证了：即使你在 Python 里 `del` 掉原数组，只要张量还活着，显存就不会被释放。官方测试 [tests/cvcuda/python/test_tensor.py:391-403](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L391-L403) 正是验证这一点：包装后删掉 cupy 数组，再分配新数组时拿到的必须是**不同的**指针。

**(c) 包装键与缓存身份**。`Tensor::Key()` 默认构造把 `m_wrapper` 置 true（[python/mod_cvcuda/nvcv/Tensor.hpp:67-77](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.hpp#L67-L77)），所有包装张量共享同一个缓存键且哈希为 0（[Tensor.cpp:332-343](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L332-L343)）。Wrap 时趁机 `removeAllNotInUseMatching` 清掉不在使用中的旧包装（[Tensor.cpp:174-179](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L174-L179)），随后仍把新包装 add 进缓存——源码注释解释了原因：防止张量最后一次被 CUDA 流使用、而 Python 侧已无引用时被提前析构。语义结论：**包装张量进缓存只为安全析构，从不被复用**（这与 u4-l2 将讲的非包装张量缓存复用形成对照，官方测试也断言了包装张量 `nbytes_in_cache == 0`，见 [tests/cvcuda/python/test_tensor.py:585-593](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L585-L593)）。

**(d) 生产者流播种**。[python/mod_cvcuda/nvcv/Tensor.cpp:184-196](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L184-L196)：若缓冲的生产者流已知且未声明"已同步"，就把该流记为张量的"最后写入流"。这样第一个读它的 cvcuda 算子会自动插入跨流等待，保证你从 cupy/torch 搬来的数据一定先于 cvcuda 的 kernel 就绪。细节留给 u4-l1，这里只需记住：**as_tensor 不拷贝数据，但会记账流**。

**(e) 官方示例对照**。包装后的基本断言见 [tests/cvcuda/python/test_tensor.py:101-107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L101-L107)：`tensor.shape`、`tensor.dtype` 与源数组一致，`tensor.layout is None`（除非显式传了 layout）。numpy 数据上 GPU 的四条官方途径集中在 [samples/interoperability/numpy_interop.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/numpy_interop.py)：cuda-python 手工 memcpy（[L41-47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/numpy_interop.py#L41-L47)）、torch（[L68-74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/numpy_interop.py#L68-L74)）、cupy（[L91-96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/numpy_interop.py#L91-L96)）、pycuda（[L116-120](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/numpy_interop.py#L116-L120)）——四条路殊途同归于一句 `cvcuda.as_tensor(...)`。注意**唯一发生 PCIe 拷贝的环节是"H2D 上传"那一步**（如 `torch.from_numpy(x).cuda()`），包装本身永远零拷贝。

#### 4.3.4 代码实践

**实践目标**：用"包装后再改写原数组"证明共享的是同一块显存。

**操作步骤**（示例代码）：

```python
# 示例代码：零拷贝的直接证据
import numpy as np, cupy, cvcuda

a = cupy.zeros((4, 8, 3), cupy.uint8)
t = cvcuda.as_tensor(a, "HWC")           # 包装
a[...] = cupy.arange(4*8*3, dtype=cupy.uint8).reshape(4, 8, 3)  # 之后才改写
b = cupy.asarray(t.cuda())               # 导出回 cupy
print("shape:", t.shape, "dtype:", t.dtype, "layout:", t.layout)
print("看到改写后的数据?", cupy.array_equal(b, a))   # 预期 True → 同一块内存
```

**需要观察的现象**：包装发生在改写**之前**，但导出后读到的是改写**之后**的值；同时 `layout` 打印出 `HWC`（显式传入的结果）。

**预期结果**（待本地验证）：`shape: (4, 8, 3) dtype: uint8 layout: HWC`，最后一行 `True`。若包装是拷贝语义，最后一行应为 `False`（会看到全零）。

#### 4.3.5 小练习与答案

**练习 1**：`as_tensor(arr)` 与 `as_tensor(arr, "HWC")` 的返回值有什么本质区别？

**答案**：显存、指针、shape、dtype 完全相同；唯一区别是元数据 layout——前者为 `None`（TENSOR_NONE），后者被贴上 `HWC` 标签。贴标签本身不搬数据，但后续算子会按标签解释维度：没有 layout 时，很多图像算子无法判断哪个维度是通道，可能直接拒绝该张量。官方测试 [tests/cvcuda/python/test_tensor.py:265-282](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L265-L282) 验证的正是这层"贴标签"语义。

**练习 2**：`t = cvcuda.as_tensor(big_cupy_array)` 之后执行 `del big_cupy_array`，显存会泄漏还是会安全？

**答案**：安全。引用链 `t → ExternalBuffer → big_cupy_array` 使 cupy 数组至少活到 `t` 被回收为止（[Tensor.cpp:181-182](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L181-L182) + `m_wrappedObj`）。反向提醒：这也意味着只要张量活着，原数组的显存就**不会**被释放——包装大量大数组时要注意引用是否还挂在别处。

### 4.4 模块四：导出路径——tensor.cuda() 与反向 DLPack

#### 4.4.1 概念说明

互操作是双向的：算子算完，结果要能交回 cupy/torch 继续用。入口是 `tensor.cuda()`——它**不搬数据**，而是返回一个 `ExternalBuffer` Python 对象，该对象同时实现了 `__cuda_array_interface__` 和 `__dlpack__`/`__dlpack_device__`，于是任何框架都能用自己的标准导入函数接收它：

- cupy：`cupy.asarray(t.cuda())` 或 `cupy.from_dlpack(t.cuda())`
- torch：`torch.as_tensor(t.cuda())`（需要独立缓冲时再 `.clone()`）

#### 4.4.2 核心流程

```
t.cuda()
  │ m_impl.exportData() → TensorData（C 层导出，u5-l2 展开）
  ▼
ToPython: 只接受 pitch-linear(TensorDataStrided) 否则抛错
  │ DLPackTensor(TensorDataStrided)   ← nvcv 元数据 → DLTensor
  ▼
ExternalBuffer::Create(dlTensor, owner=自己, exportStream=最后写入流)
  │ 挂 __cuda_array_interface__（含 CAI v3 stream 字段）
  ▼
返回给 Python ──► cupy.asarray / torch.as_tensor / from_dlpack 零拷贝导入
```

#### 4.4.3 源码精读

**(a) 导出入口**。[python/mod_cvcuda/nvcv/Tensor.cpp:386-401](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L386-L401) 的 `Tensor::cuda()`：先 `exportData()` 拿到 C 层数据描述，再通过 `getLastStreamHandle()` 查出"这块数据最后被哪条 cvcuda 流写过"，把它作为 CAI 的 `stream` 字段广播出去——下游框架因此知道该同步哪条流。源码注释特别说明：返回的 ExternalBuffer **不能**进缓存，因为它持有对我们的引用，缓存它会造成循环引用泄漏。

**(b) pitch-linear 限制**。[python/mod_cvcuda/nvcv/Tensor.cpp:371-384](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L371-L384)：导出前先 `cast<TensorDataStrided>()`，失败则抛 `Only tensors with pitch-linear data can be exported`。u2-l1 讲过 nvcv 张量默认就是 strided（行距对齐），所以常规张量都能导出；块线性（block-linear）等特殊布局才会踩中这条。

**(c) nvcv → DLTensor**。[python/mod_cvcuda/nvcv/DLPackUtils.cpp:128-182](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L128-L182)：与导入方向相反——`TensorDataStridedCuda` 时设备设为 `kDLCUDA` 并用 `GetOwningDevice`（[DLPackUtils.cpp:41-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L41-L55)）从指针反查设备号；字节 stride 除以 `strideBytes()` 换回元素 stride（除不尽抛错，与失败条件 #8 对称）。

**(d) 对外双协议**。`ExternalBuffer` 导出时：CAI 字典在 [ExternalBuffer.cpp:652-713](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L652-L713) 生成，`version` 为 3、`data` 第二元素为 `false`（可写），并带 `stream` 字段（默认保守值 `1` = legacy 默认流；生产者声明已同步时为 `-1`）；`__dlpack__` 在 [ExternalBuffer.cpp:715-749](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L715-L749)，按对方给的 `max_version` 决定产出 v1（`dltensor_versioned`，[L312-340](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L312-L340)）还是 v0（`dltensor`）胶囊，并在对方传了流时插入 `cudaEventRecord`+`cudaStreamWaitEvent` 跨流同步（[ExternalBuffer.cpp:361-374](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L361-L374)）。绑定注册在 [ExternalBuffer.cpp:762-771](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L762-L771)。

**(e) 官方示例对照**。cupy 方向的最小闭环在 [samples/interoperability/cupy_interop.py:31-45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cupy_interop.py#L31-L45)（包进去 → `cupy.asarray(t.cuda())` 取回来 → 断言逐元素相等）；torch 方向在 [samples/interoperability/pytorch_interop.py:31-51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pytorch_interop.py#L31-L51)，其中 `.clone()` 的注释值得注意：`torch.as_tensor(t.cuda())` 与原张量**共享**同一块显存，想要独立副本必须显式 clone。导出的正确性测试见 [tests/cvcuda/python/test_tensor.py:347-361](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L347-L361)（`cupy.from_dlpack` 往返比对）。

#### 4.4.4 代码实践

**实践目标**：检查导出对象的 CAI 字段，确认 version、stream 与只读标志。

**操作步骤**（示例代码）：

```python
# 示例代码：检视导出缓冲的 CAI 字典
import cupy, cvcuda

t = cvcuda.as_tensor(cupy.zeros((2, 6, 4), cupy.uint8), "HWC")
buf = t.cuda()
iface = buf.__cuda_array_interface__
print({k: iface[k] for k in ("shape", "typestr", "strides", "version")})
print("data:", hex(iface["data"][0]), "read_only:", iface["data"][1])
print("stream 字段:", iface.get("stream"))
```

**需要观察的现象**：`strides` 的数值与 u2-l1 的行距对齐是否吻合（W=4、C=1 字节元素时行距可能被对齐到 32 的倍数而非紧凑的 4）；`stream` 字段的取值。

**预期结果**（待本地验证）：`version == 3`、`typestr == '|u1'`、`read_only == False`；若该张量从未被算子写过，`stream` 缺省或为保守值 `1`。strides 若出现"行距 > W×itemsize"即为 u2-l1 所讲的对齐填充的直接证据。

#### 4.4.5 小练习与答案

**练习 1**：`cupy.asarray(t.cuda())` 和 `cupy.from_dlpack(t.cuda())` 有何区别？

**答案**：走的外部协议不同：`asarray` 优先读取 `__cuda_array_interface__`（桥 B 的产出），`from_dlpack` 调用 `__dlpack__()` 拿胶囊（桥 C 的产出）。两者都是零拷贝，最终 cupy 数组与 `t` 共享显存。官方测试两条路都覆盖：[tests/cvcuda/python/test_tensor.py:327-361](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L327-L361)。

**练习 2**：为什么 `Tensor::cuda()` 的返回对象不能像普通张量那样进对象缓存？

**答案**：源码注释（[Tensor.cpp:396-401](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L396-L401)）写明：该对象持有源张量的引用（keep_alive），缓存它会形成"缓存 → 缓冲 → 张量"的循环持有，导致内存泄漏。

**练习 3**：从 torch 导入、执行 `cvcuda.flip`、再导回 torch，全程发生了几次数据拷贝？

**答案**：0 次（包装与导出都是元数据操作），外加 1 次 GPU 内的 kernel 写入（flip 本身的输出张量写入，属于计算而非搬运）。如果最开始数据在 CPU，则另有 1 次 `.cuda()` 的 H2D 拷贝——那是 torch 的事，与 cvcuda 无关。

## 5. 综合实践

**任务**：把本讲全部知识串成一条"三框架同图翻转"管线——同一张图准备三份副本（numpy 原件、cupy 副本、torch 副本），统一用 cvcuda 翻转，再各自回到原生框架断言三者一致，并记录每种框架的包装限制条件。

**完整脚本**（示例代码，保存为 `tri_framework_flip.py`，需 GPU 环境，运行结果待本地验证）：

```python
# 示例代码：三框架 as_tensor 翻转对比
import numpy as np
import cupy
import torch
import cvcuda

H, W, C = 8, 12, 3
rng = np.random.default_rng(42)
img = rng.integers(0, 255, (H, W, C), dtype=np.uint8)   # ① 唯一的数据源（CPU）
gold = np.flip(img, axis=1)                             # flipCode=1 的黄金参考

# --- 副本一：numpy 直接包装（预期失败，记录报错） ---
try:
    t_np = cvcuda.as_tensor(img, "HWC")
    print("numpy 直接包装：居然成功了？")
except Exception as e:
    print(f"numpy 直接包装失败（预期）：{type(e).__name__}: {e}")

# --- 副本二：cupy（CAI + DLPack 双协议，走 CAI 路径） ---
t_cp = cvcuda.as_tensor(cupy.asarray(img), "HWC")
out_cp = cvcuda.flip(t_cp, 1)                            # allocating 变体
res_cp = np.asnumpy(cupy.asarray(out_cp.cuda()))

# --- 副本三：torch（仅 DLPack，走胶囊路径） ---
t_th = cvcuda.as_tensor(torch.from_numpy(img).cuda(), "HWC")
out_th = cvcuda.flip(t_th, 1)
res_th = torch.as_tensor(out_th.cuda()).cpu().numpy()

# --- 三方一致性断言 ---
assert np.array_equal(res_cp, gold), "cupy 路径结果与黄金参考不一致"
assert np.array_equal(res_th, gold), "torch 路径结果与黄金参考不一致"
assert np.array_equal(res_cp, res_th)
print("两条路径均与黄金参考一致 ✔")

# --- 附加实验：跨框架零拷贝互相可见 ---
t_x = cvcuda.as_tensor(cupy.asarray(img), "HWC")
th_view = torch.as_tensor(t_x.cuda())                   # torch 视角看 cupy 的内存
assert th_view.shape == (H, W, C)
print("跨框架共享显存验证通过，shape =", tuple(th_view.shape))
```

**操作步骤**：

1. 对照 4.1 的流程图，先在纸上预判：三份副本各自走哪座桥、哪一步会失败。
2. 运行脚本，抄下 numpy 直接包装的报错原文，与 4.2 失败条件速查表的第 1 条核对。
3. 把 `flipCode=1` 换成 `0` 与 `-1`，同步修改 `gold`（垂直翻转 / 双向翻转），验证翻转语义（参考 [python/mod_cvcuda/operators/OpFlip.cpp:96-113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L96-L113) 的 docstring）。
4. 填写限制条件记录表（预期内容如下表，报错原文以实际运行为准）。

| 框架 | 可否直接 as_tensor | 走的协议路径 | 关键限制 |
|---|---|---|---|
| numpy（CPU） | 否 | `__dlpack_device__` 报 kDLCPU → 拒绝 | 必须先上传 GPU（经 cupy/torch/pycuda/cuda-python） |
| cupy（GPU） | 是 | CAI 优先 | dtype 须可映射（≤4 lanes 等）；stride 字节须整除 itemsize |
| torch（GPU） | 是 | DLPack 胶囊（v1 优先，v0 回退） | bfloat16 等无映射的 code 会被拒；CPU 张量同 numpy |

**预期结果**（待本地验证）：三条断言全部通过；附加实验证明 cupy 与 torch 通过 cvcuda 这个"中间人"看到的是同一块显存。

## 6. 本讲小结

- `cvcuda.as_tensor` 的入口是 pybind11 自定义 type_caster（[ExternalBuffer.cpp:780-801](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L780-L801)），按 **CAI 优先、DLPack 兜底**的顺序探测协议，两者都不通则 TypeError。
- 导入路径的安检在 `ExternalBuffer`：设备必须 CUDA 可访问（CPU numpy/torch 被拒）、指针须经 `cudaPointerGetAttributes` 验证、dtype 的 lanes ≤ 4 且 code 可映射、CAI 的字节 stride 须整除元素大小、秩在 1~15 之间。
- 零拷贝落地在 `Tensor::Wrap`：`DLTensor` 元数据逐字段抄入 `NVCVTensorData`，`TensorWrapData` 只登记不分配；引用链 `Tensor → ExternalBuffer → 原数组` 保证生命周期，包装张量在缓存中只为安全析构、从不复用。
- DLPack 的 stride 以元素为单位、CAI 与 nvcv 以字节为单位，两处换算（[Tensor.cpp:148-153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L148-L153) 与 [DLPackUtils.cpp:164-175](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L164-L175)）是理解 stride 相关报错的钥匙。
- 导出路径 `tensor.cuda()` 返回同时实现 CAI v3 与 DLPack v0/v1 的 `ExternalBuffer`，并广播"最后写入流"供下游框架同步；包装与导出全程不搬运一个字节。
- `as_tensor` 还会记录生产者流（seedLastStream），使第一个消费它的算子自动插入跨流等待——这是 u4-l1 流模型的前哨。

## 7. 下一步学习建议

- **下一讲（u3-l1）**：带着本讲的 wrapper 知识进入算子实战，重点观察 `resize`/`flip` 对 Tensor 与 ImageBatchVarShape 两类输入的不同入口，以及算子对无 layout 张量的处理。
- **u4-l1（Stream 执行模型）**：本讲两处埋的伏笔——包装时的 `seedLastStream` 与导出时的 CAI `stream` 字段/`InsertDLPackStreamSync`——在那里汇成完整的跨流同步故事；相关源码 [ExternalBuffer.cpp:103-149](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L103-L149) 与 [ExternalBuffer.cpp:361-374](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L361-L374) 值得再读一遍。
- **u4-l2（对象缓存）**：对照本讲的"包装张量永不复用"，理解非包装张量如何按 shape/dtype 键复用，以及 `nbytes_in_cache` 的意义。
- **延伸阅读源码**：[tests/cvcuda/python/test_interop_cai_stream.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_interop_cai_stream.py) 与 [tests/cvcuda/python/test_re_export.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_re_export.py) 覆盖了本讲未展开的流竞争与重导出边界情形；samples/interoperability 下还有 cuda-python、pynvvideocodec 等示例可作 u9-l3 的预习。
