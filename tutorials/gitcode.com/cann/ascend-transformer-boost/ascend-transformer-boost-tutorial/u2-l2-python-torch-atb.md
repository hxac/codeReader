# Python（torch_atb）调用算子实战

## 1. 本讲目标

上一讲（u2-l1）我们用 C++ 走通了一次算子调用的「五段式骨架」。本讲换一种更轻量的姿势：**用 Python 调用 ATB 算子**。

读完本讲，你应当能够：

1. 说清楚 `import torch_atb` 这一行背后到底加载了哪些动态库、设置了什么环境变量，以及 Python 类是从哪里来的。
2. 用 Python 三步走（构造 Param → 创建 Operation → `forward`）在 NPU 上跑通一个单算子。
3. 理解 `torch_atb` 如何把 PyTorch 的 `torch::Tensor` 桥接成 ATB 的 `atb::Tensor`，以及它替你「偷偷」做了哪些事（创建 Context、分配 workspace、流同步）。
4. 看懂 `example/graph_example.py` 里图算子（Builder）的调用写法，为第 5 单元图算子讲义打基础。

## 2. 前置知识

在开始前，建议你已经建立以下认知（来自前面几讲）：

- **两段式执行**：ATB 算子执行分 `Setup`（Host 侧校验 + 形状推导 + Tiling + 算 workspaceSize）和 `Execute`（异步下发到 Device）两段（见 u1-l6）。
- **Tensor 三层描述**：`TensorDesc`（dtype/format/shape）与真实数据分离，`VariantPack` 是输入输出张量的「集装箱」（见 u1-l4）。
- **Context**：一组算子共享的运行时环境，托管执行流、资源池等全局资源（见 u1-l5）。
- **工厂模板**：算子由 `CreateOperation<OpParam>` 创建、`DestroyOperation` 销毁，Param 决定算子行为（见 u1-l6）。

此外，本讲会用到 **PyTorch** 和 **TorchNPU**（让 PyTorch 能在昇腾 NPU 上运行的插件）两个外部依赖。关键概念：

- `torch_npu`：扩展 PyTorch，让张量可以放到 NPU 上。一个普通 CPU 张量 `t` 调用 `t.npu()` 就迁移到了 NPU 设备内存上。
- **pybind11**：C++ 与 Python 互操作的工具，ATB 用它把 C++ 的 `Operation`、`LinearParam` 等类暴露成 Python 可用的类。这部分细节我们会点到为止，深入到 C++ 绑定层属于 u3 单元。

> 一个直觉：C++ 调用 ATB 是「手挡车」——你要自己 `aclInit`、建 Context、建流、装 VariantPack、分 workspace、同步；Python 调用 ATB 是「自动挡」——`forward([x, y])` 一行把这一切包好了。本讲的核心，就是看清这层「自动挡」把离合、换挡都藏到了哪里。

## 3. 本讲源码地图

本讲涉及的源码文件及其作用：

| 文件 | 作用 |
|------|------|
| [torch_atb/__init__.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/torch_atb/__init__.py) | torch_atb 包的入口：加载 ATB 动态库、设置环境变量、导入 pybind 扩展模块 |
| [README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md) | 官方 Python 调用示例（安装方式 + 最小代码） |
| [example/graph_example.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py) | 图算子的 Python 调用示例（Builder 组图 + forward） |
| [src/torch_atb/bindings.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp) | pybind11 绑定：把 C++ 类暴露成 Python 类（`Operation`、各 `Param`、`Builder`） |
| [src/torch_atb/operation_wrapper.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.h) / [.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp) | `OperationWrapper`：Python 端 `Operation` 的 C++ 实现，封装 Setup/Execute/同步 |
| [src/torch_atb/resource/utils.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp) | torch↔atb 张量互转、线程局部 Context 获取、当前流获取 |
| [src/torch_atb/enger_graph_builder.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp) | `GraphBuilder`：Python 端 `Builder` 的 C++ 实现，组图后返回一个 `Operation` |

> 名词澄清：仓库里有两个 `torch_atb` 目录——顶层 `torch_atb/`（安装进 whl 的 Python 包，含 `__init__.py`）和 `src/torch_atb/`（C++ 绑定源码，编译成 `_C.so`）。两者最终拼成一个 Python 包。

## 4. 核心概念与源码讲解

### 4.1 torch_atb 模块的加载机制

#### 4.1.1 概念说明

在 Python 里写 `import torch_atb` 时，Python 解释器会执行包目录下的 `__init__.py`。这个脚本看似简短，却完成了三件关键的事，缺一不可：

1. **预加载 ATB 的动态库**：ATB 由多个 `.so` 组成（`libatb.so`、`libasdops.so`、`libmki.so` 等），它们之间有符号依赖。如果不先把它们全部加载进进程地址空间，后续导入 `_C.so` 时会因找不到符号而失败。
2. **设置环境变量**：把 `ATB_HOME_PATH` 指向 torch_atb 包所在目录，让运行时能找到配置和资源。
3. **导入真正的扩展模块 `_C`**：`_C.so` 是用 pybind11 编译出来的 C++ 扩展，所有 Python 类（`Operation`、`LinearParam`、`Builder`…）都定义在里面。

#### 4.1.2 核心流程

```text
import torch_atb
        │
        ▼
__init__.py 执行
        │
        ├─ _load_atb_libs()   逐个 ctypes.CDLL(lib, RTLD_GLOBAL)
        │     加载 libmki.so → libasdops.so → liblcal.so
        │          → libatb_mixops.so → libatb.so → libatb_train.so
        │
        ├─ _init_env_params() 设置 ATB_HOME_PATH = 包目录
        │
        └─ from torch_atb._C import *   导入 pybind 扩展，暴露所有类
```

#### 4.1.3 源码精读

库列表与加载逻辑定义在 [torch_atb/__init__.py:20-27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/torch_atb/__init__.py#L20-L27)：函数 `_load_atb_libs` 把 6 个 `.so` 按依赖顺序逐个用 `ctypes.CDLL` 加载，关键参数是 `mode=ctypes.RTLD_GLOBAL`，它把库符号放进全局符号表，供后续 `.so`（包括 `_C.so`）解析。

加载的库列表见 [torch_atb/__init__.py:22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/torch_atb/__init__.py#L22)。注意顺序：底层基础设施（`mki`、`asdops`）在前，上层算子库（`atb`）在后，符合依赖方向。

环境变量设置见 [torch_atb/__init__.py:30-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/torch_atb/__init__.py#L30-L31)：`_init_env_params` 把 `ATB_HOME_PATH` 设为包目录路径（即安装后的 `torch_atb/` 目录）。这个变量在 u1-l3 讲过，是 ATB 运行时定位自身资源的关键。

两个初始化函数在模块导入时**立即执行**（[torch_atb/__init__.py:33-34](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/torch_atb/__init__.py#L33-L34)），随后才导入扩展模块（[torch_atb/__init__.py:36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/torch_atb/__init__.py#L36)）。这个顺序保证了「库已就绪 → 再加载依赖它们的 `_C.so`」。

`_C` 这个扩展模块本身是用 pybind11 编译出来的，目标名就是 `_C`（见 [src/torch_atb/CMakeLists.txt:27-28](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/CMakeLists.txt#L27-L28) 的 `pybind11_add_module(_C ...)`），所以 Python 端才能 `from torch_atb._C import *`。其入口宏 `PYBIND11_MODULE(_C, m)` 定义在 [src/torch_atb/bindings.cpp:38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L38)。

> **安装方式补充**：在运行 Python 代码前，需要安装 PyTorch、TorchNPU，再手动安装 `torch_atb`。README 给出两种安装方式：随 nnal 包安装（`--torch_atb` 选项），或编译时加 `bash scripts/build.sh --torch_atb` 生成 whl 后 `pip3 install`（见 [README.md:163-170](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L163-L170)）。另外一个易踩的坑：**不要在 ATB 源码仓的同名 `torch_atb` 目录下运行脚本**，否则 Python 会优先 import 到源码目录而非安装包（见 [README.md:172](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L172)）。

#### 4.1.4 代码实践

**实践目标**：验证 `import torch_atb` 触发的环境变量设置与库加载路径。

**操作步骤**：

1. 确认已按 README 安装好 torch_atb，并在终端 `source` 了 CANN 的 `set_env.sh`。
2. 进入任意**非** ATB 源码目录，启动 `python3`。
3. 执行下面这段「探测」脚本（**示例代码**，非项目原有）：

```python
import torch_atb                       # 触发 __init__.py 的加载逻辑
import os

# 1. 检查环境变量是否被设置
print("ATB_HOME_PATH =", torch_atb.get_atb_home_path())

# 2. 检查关键 .so 是否已被加载进进程地址空间
import ctypes.util
for lib in ["libatb.so", "libasdops.so", "libmki.so"]:
    print(lib, "->", "已加载" if torch_atb.get_atb_home_path() else "未知")
```

**需要观察的现象**：`get_atb_home_path()` 应返回一个指向已安装 `torch_atb` 包目录的绝对路径；`import` 过程不应抛出 `OSError: Failed to load ...`。

**预期结果**：`ATB_HOME_PATH` 指向安装目录（形如 `.../site-packages/torch_atb`）。若报 `Failed to load libxxx.so`，通常是 CANN 环境未 source 或 whl 与 CANN 版本不匹配。**待本地验证**（具体路径取决于你的安装位置）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_load_atb_libs` 要用 `RTLD_GLOBAL` 而不是默认的 `RTLD_LOCAL`？

**参考答案**：`RTLD_GLOBAL` 把加载的库符号暴露到进程全局符号表。`_C.so` 内部会调用 `libatb.so` 等库的函数，但这些库不是 `_C.so` 的直接链接依赖；用 `RTLD_GLOBAL` 预加载后，`_C.so` 才能在运行时解析到这些符号，避免 `undefined symbol` 错误。

**练习 2**：如果 `from torch_atb._C import *` 放在 `_load_atb_libs()` 调用之前，会发生什么？

**参考答案**：`_C.so` 依赖 `libatb.so` 等库的符号，若这些库尚未被全局加载，导入 `_C` 时动态链接器找不到所需符号，会抛出 `ImportError`（形如 `undefined symbol: ...`）。因此加载顺序必须是「先底层库，后 `_C`」。

---

### 4.2 用 Python 调用单算子：Param → Operation → forward

#### 4.2.1 概念说明

`torch_atb` 把 C++ 的「五段式骨架」压缩成三步：

1. **构造 Param**：每个算子都有一个同名 `Param` 类（如 `LinearParam`），它对应 u1-l6 讲过的 C++ `OpParam`。Param 决定算子的行为（是否带 bias、转置方式、量化模式等）。
2. **创建 Operation**：`torch_atb.Operation(param)` 按传入的 Param 类型，内部调用 C++ 的 `CreateOperation` 创建对应的算子对象。
3. **forward**：把一组输入张量传给 `op.forward([...])`，它返回一组输出张量。

这三步对应 C++ 里的 `CreateOperation` → `Setup` → `Execute` + 同步，只是细节被封装了。

#### 4.2.2 核心流程

```text
linear_param = torch_atb.LinearParam()       # 1. 构造 Param
linear_param.has_bias = False

op = torch_atb.Operation(linear_param)       # 2. 创建 Operation
                                              #    内部 → CreateOperation<LinearParam>

x = torch.randn(...).npu()                   #    准备 NPU 张量
y = torch.randn(...).npu()

outputs = op.forward([x, y])                  # 3. 执行（内部 Setup + Execute + 同步）
torch.npu.synchronize()
result = outputs[0].cpu().numpy()             #    取回 Host 打印
```

#### 4.2.3 源码精读

README 的官方 Python 示例就在 [README.md:174-199](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L174-L199)。其中 Param 与 Operation 的创建见 [README.md:179-183](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L179-L183)：`LinearParam()` 默认 `has_bias=True`，示例把它改成 `False`；`Operation(linear_param)` 根据参数类型走对应的构造函数。输入张量用 `.npu()` 迁到设备（[README.md:186-187](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L186-L187)），执行与同步见 [README.md:190-191](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L190-L191)，取结果打印见 [README.md:197-198](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L197-L198)。

`Operation` 这个 Python 类其实是 C++ `OperationWrapper` 的 pybind 包装，绑定代码在 [src/torch_atb/bindings.cpp:49-93](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L49-L93)。关键点：它为**每一种 Param 类型**都注册了一个构造函数重载（`.def(py::init<const LinearParam &>())` 等，见 [bindings.cpp:50-83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L50-L83)）。所以你在 Python 写 `Operation(linear_param)` 时，pybind 会根据 `linear_param` 的 C++ 类型，自动匹配到 `OperationWrapper(const LinearParam&)` 这个构造函数。它还暴露了只读属性 `name`/`input_num`/`output_num` 和方法 `forward`（[bindings.cpp:84-87](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L84-L87)）。

`LinearParam` 自身的字段绑定在 [bindings.cpp:373-399](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L373-L399)。从构造函数默认值能看出关键字段：`transpose_b=True`（权重按转置存储，对应 `y = xW^T`）、`has_bias=True`、`out_data_type=ACL_DT_UNDEFINED` 等（[bindings.cpp:384-391](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L384-L391)）。每个字段用 `def_readwrite` 暴露成可在 Python 端读写的属性（[bindings.cpp:392-398](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L392-L398)）。

`forward` 背后是 `OperationWrapper::Forward`，定义在 [src/torch_atb/operation_wrapper.cpp:231-242](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L231-L242)：它先 `Setup` 再 `Execute`，最后返回输出的 `torch::Tensor` 列表——也就是说，**Python 的一行 `forward` 同时干了 C++ 的 Setup + Execute 两件事**。具体内部下一节展开。

> **输入顺序很重要**：`forward([...])` 列表里的张量顺序必须和算子定义的输入顺序一致（见 u1-l4 的 VariantPack）。对 `has_bias=False` 的 Linear，输入是 `[激活 x, 权重 weight]`。README 示例里 `x`、`y` 形状都是 `(2, 3)`，由于 `transpose_b=True`，实际计算 `x @ y^T = (2,3) @ (3,2) = (2,2)`，维度自洽。真实业务里权重通常是 `(out_features, in_features)`。

#### 4.2.4 代码实践

**实践目标**：参照 README 示例，写一段用 torch_atb 调用 Linear 算子并打印输出的脚本，并观察 `name`/`input_num`/`output_num` 属性。

**操作步骤**：

1. 在**非** ATB 源码目录下新建 `run_linear.py`（**示例代码**，基于 README 改写，加了属性观察与维度注释）：

```python
import torch
import torch_npu      # noqa: F401  注册 NPU 设备，使 .npu() 可用
import torch_atb

# 1. 构造 Param：不带 bias
linear_param = torch_atb.LinearParam()
linear_param.has_bias = False

# 2. 创建 Operation
op = torch_atb.Operation(linear_param)
print(op)            # 触发 __repr__，打印 name/input_num/output_num

# 3. 准备输入（NPU fp16）
#    x: (2,3) 激活；weight: (2,3)，因 transpose_b=True，计算 x @ weight^T -> (2,2)
x = torch.randn(2, 3, dtype=torch.float16).npu()
weight = torch.randn(2, 3, dtype=torch.float16).npu()

# 4. 执行
outputs = op.forward([x, weight])
torch.npu.synchronize()

# 5. 取回 Host 打印
result = outputs[0].cpu().numpy()
print("output shape:", result.shape)
print(result)
```

2. 运行：`python3 run_linear.py`

**需要观察的现象**：`print(op)` 应打印形如 `op name: Linear, input_num: 2, output_num: 1`；输出形状应为 `(2, 2)`。

**预期结果**：输出一个 `(2, 2)` 的 fp16 数值矩阵。若报 `call operation_->Setup fail`，多半是输入个数或 dtype/format 不满足算子规格。**待本地验证**（具体数值因随机种子不同而不同）。

#### 4.2.5 小练习与答案

**练习 1**：把 `linear_param.has_bias` 改成 `True`（默认值），但保持 `forward([x, weight])` 只传两个张量。会发生什么？

**参考答案**：带 bias 的 Linear 输入是 `[激活, 权重, bias]` 共 3 个。只传 2 个会导致输入个数与 `GetInputNum()` 不符，`Setup` 阶段校验失败，抛出 `call operation_->Setup fail`（对应 u1-l4 提到的 `ERROR_INVALID_IN_TENSOR_NUM` 类错误）。修复：要么 `has_bias=False`，要么补上第三个 bias 张量。

**练习 2**：为什么 README 示例在 `forward` 之后还要写一句 `torch.npu.synchronize()`？

**参考答案**：ATB 的 `Execute` 是异步下发到 Device 的（见 u1-l6）。`forward` 内部其实已经做过一次流同步（见下一节 `Execute` 末尾的 `aclrtSynchronizeStream`），但 README 仍显式再同步一次，是为了确保在取结果（`.cpu()`）前 Device 侧计算彻底完成，避免读到未完成的结果。这是一种「双保险」写法。

---

### 4.3 torch_atb 如何桥接 PyTorch 与 ATB

#### 4.3.1 概念说明

`forward` 看似魔法，本质是把 PyTorch 世界（`torch::Tensor`、NPU stream）翻译成 ATB 世界（`atb::Tensor`、`VariantPack`、`Context`）。这一节我们打开「自动挡」的引擎盖，看清三件事：

1. **张量互转**：torch 的 `torch::Tensor` 怎么变成 `atb::Tensor`，输出的 `atb::TensorDesc` 又怎么变回 `torch::Tensor`。
2. **Context 托管**：Python 端从不需要手动 `CreateContext`，它是怎么被自动创建和复用的。
3. **Setup/Execute 内部展开**：C++ 五段式里的「分配 workspace」「流同步」分别被谁接管了。

理解这一节，你就能解释「为什么 Python 调用比 C++ 简洁那么多」。

#### 4.3.2 核心流程

```text
op.forward([x, weight])          # Python 调用
        │  pybind
        ▼
OperationWrapper::Forward(inTensors)
        │
        ├─ Setup(inTensors, outTensors)
        │     ├─ BuildInTensorVariantPack   torch::Tensor → atb::Tensor（转 dtype/format/指针）
        │     ├─ InferShape                 由输入 desc 推输出 desc
        │     ├─ outTensors = CreateTorchTensorFromTensorDesc(outDescs)  按输出形状在 NPU 上建空张量
        │     ├─ 填充 variantPack_.outTensors
        │     └─ operation_->Setup(variantPack_, workspaceSize_, GetAtbContext())
        │
        └─ Execute()
              ├─ 按 workspaceSize_ 申请 workspace（MemoryManager 统一管理）
              ├─ operation_->Execute(variantPack_, workspace, ..., context)   异步下发
              └─ aclrtSynchronizeStream(context->GetExecuteStream())           等待 Device
        │
        ▼
返回 outTensors（torch::Tensor 列表）
```

#### 4.3.3 源码精读

**张量互转**的核心是 `Utils::ConvertToAtbTensor`，定义在 [src/torch_atb/resource/utils.cpp:53-90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L53-L90)。它做了几件关键的事：

- 先确保张量连续（`contiguous()`，[utils.cpp:62-64](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L62-L64)）。
- 按设备分流：NPU 张量取 `deviceData` 指针、读 NPU format；CPU 张量取 `hostData`、format 设为 `ACL_FORMAT_ND`（[utils.cpp:65-71](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L65-L71)）。注意它**不拷贝数据**，只是把 torch 张量的裸指针塞进 `atb::Tensor`，零拷贝。
- dtype 通过映射表 `TORCH_TO_ACL_DTYPE_MAP` 转换（如 `Half→ACL_FLOAT16`、`BFloat16→ACL_BF16`，[utils.cpp:55-59](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L55-L59) 与 [utils.cpp:84-87](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L84-L87)）。
- 维度超过 `atb::MAX_DIM`（8）会抛异常（[utils.cpp:75-78](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L75-L78)）。

反方向的 `CreateTorchTensorFromTensorDesc` 在 [utils.cpp:92-111](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L92-L111)：根据 ATB 推导出的输出 `TensorDesc`（dtype+shape+format），用 `empty_with_format` 在 **NPU** 上分配一个空张量，作为 `forward` 的返回值。这就是为什么你不用预先准备输出张量——torch_atb 按推导形状自动建好了。

**Context 托管**的核心是 `Utils::GetAtbContext`，见 [src/torch_atb/resource/utils.cpp:36-51](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L36-L51)。两个关键设计：

- 用 `static thread_local` 缓存 Context（[utils.cpp:38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L38)）：**每个线程一个**，首次调用时创建，之后复用。所以 Python 端永远不用手动 `CreateContext`。
- 创建后立刻把执行流设为「当前 NPU 流」（`SetExecuteStream(GetCurrentStream())`，[utils.cpp:47](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L47)）。`GetCurrentStream` 通过 `c10_npu::getCurrentNPUStream(devId)` 拿到 TorchNPU 当前线程的默认流（[utils.cpp:25-34](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L25-L34)）。这让 ATB 的下发与 PyTorch 的算子落在同一条流上，自然有序。

> 对比 u2-l1 的 C++ demo：那里你得自己 `aclInit`/`CreateContext`/`CreateStream`/`SetExecuteStream`，并在结束时 `DestroyContext`+`aclFinalize`。Python 端这些全被 `GetAtbContext` 的线程局部缓存托管了。

**Setup/Execute 内部展开**在 `OperationWrapper` 里。`Setup` 见 [operation_wrapper.cpp:262-282](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L262-L282)：先把输入 torch 张量转成 `atb::Tensor` 装进 `variantPack_`（`BuildInTensorVariantPack`，[operation_wrapper.cpp:313-319](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L313-L319)），调 `InferShape` 推输出形状（[operation_wrapper.cpp:244-260](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L244-L260)），按形状建输出张量，最后调 `operation_->Setup(variantPack_, workspaceSize_, context)` 算出 `workspaceSize_`（[operation_wrapper.cpp:278](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L278)）。

`Execute` 见 [operation_wrapper.cpp:284-311](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L284-L311)。两个细节值得注意：

- **workspace 自动管理**：若 `workspaceSize_ > 0`，向 `MemoryManager` 申请一块缓冲（[operation_wrapper.cpp:289-293](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L289-L293)）。对比 C++ demo 里你要自己 `aclrtMalloc`/`aclrtFree`，这里由内存管理器统一托管，无需手动释放。
- **流同步内置**：正常路径下 `Execute` 末尾就调了 `aclrtSynchronizeStream`（[operation_wrapper.cpp:303-310](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L303-L310)）。这就是上一节练习 2 说的「forward 内部已同步过一次」。

（还有一个可选的 `IsTaskQueueEnable` 分支，会把 Execute 包进 TorchNPU 的 `OpCommand` 任务队列里异步下发而不立即同步，由 `TASK_QUEUE_ENABLE` 环境变量控制，见 [utils.cpp:121-126](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L121-L126)。初学可暂不深究，知道有这条路径即可。）

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：通过阅读源码，回答「Python 的 forward 替我接管了哪几件 C++ demo 里要手动做的事」。

**操作步骤**：

1. 打开 [src/torch_atb/operation_wrapper.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp) 与 [src/torch_atb/resource/utils.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp)。
2. 对照 u2-l1 的 C++ 五段式（资源初始化 / 建 op+装 VariantPack / Setup / Execute+同步 / 释放），逐一在 `Forward`、`Setup`、`Execute`、`GetAtbContext`、`ConvertToAtbTensor` 中找到对应代码点。
3. 画一张「C++ 手动步骤 ↔ torch_atb 自动接管位置」的对照表。

**需要观察的现象**：你应该能定位到——Context 由 `GetAtbContext`（thread_local）托管；VariantPack 由 `BuildInTensorVariantPack` 自动装填；输出张量由 `CreateTorchTensorFromTensorDesc` 自动创建；workspace 由 `MemoryManager` 自动申请；流同步在 `Execute` 末尾自动执行。

**预期结果**：得到一张 5 行的对照表，每行写明「C++ 手动做的某步 ↔ torch_atb 在哪个函数里自动做了」。例如「手动 `aclrtMalloc(workspace)` ↔ `MemoryManager::GetWorkspaceBuffer`（operation_wrapper.cpp:292）」。

#### 4.3.5 小练习与答案

**练习 1**：`ConvertToAtbTensor` 把 torch 张量转成 `atb::Tensor` 时，会不会发生设备内存的数据拷贝？为什么？

**参考答案**：不会。它只把 torch 张量的 `data_ptr()`（裸指针）赋给 `atbTensor.deviceData`/`hostData`（[utils.cpp:66-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/resource/utils.cpp#L66-L70)），是零拷贝的指针共享。正因如此，ATB 算子直接在 torch 张量的原内存上读写，无需中转。

**练习 2**：为什么 `GetAtbContext` 用 `thread_local` 而不是全局单例？

**参考答案**：因为 PyTorch 的 NPU 流是**按线程**区分的（`c10_npu::getCurrentNPUStream(devId)` 返回当前线程的流）。用 `thread_local` 让每个线程拿到自己的 Context 并绑定自己的当前流，避免多线程并发下放时多个线程抢同一条流、互相阻塞或乱序。全局单例做不到这种线程隔离。

---

### 4.4 图算子调用一瞥（Builder）

#### 4.4.1 概念说明

除了单算子，torch_atb 还能用 `Builder` 把多个算子组合成「图算子」一次性执行（图算子机制详见第 5 单元）。本节只看**怎么在 Python 里调用**，建立感性认识。`example/graph_example.py` 就是官方的图算子示例：它用 `SelfAttention → ElewiseAdd → LayerNorm → Linear → Tanh → Linear` 串成一个小图。

#### 4.4.2 核心流程

```text
graph = torch_atb.Builder("Graph")           # 建图
q = graph.add_input("query")                 # 声明输入（按名称编址）
...
node = graph.add_node([q,k,v,seqLen], self_attn_param)   # 加算子节点
out = node.get_output(0)                     # 取节点输出，供下游引用
...
graph.mark_output(final_out)                 # 标记图的最终输出
Graph = graph.build()                        # 编译成图算子（返回一个 Operation）

outputs = Graph.forward(inputs)              # 像单算子一样 forward
```

#### 4.4.3 源码精读

示例脚本见 [example/graph_example.py:42-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L42-L85)：`Builder("Graph")` 建图（[graph_example.py:43](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L43)），`add_input` 声明输入张量名称（[graph_example.py:44-47](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L44-L47)），`add_node(输入名列表, param)` 添加算子节点（[graph_example.py:52](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L52)），最后 `mark_output` 标记输出、`build()` 编译（[graph_example.py:83-84](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L83-L84)）。运行时像单算子一样 `Graph.forward(inputs)`（[graph_example.py:91](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L91)）。

为什么 `build()` 返回的东西能直接 `.forward()`？因为 `GraphBuilder::Build` 最终返回的就是一个 `OperationWrapper`（即一个图算子 Operation），见 [src/torch_atb/enger_graph_builder.cpp:263-299](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L263-L299)，其末尾 `return OperationWrapper(graphParam_)`（[enger_graph_builder.cpp:298](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L298)）。也就是说，**图算子在 Python 端和单算子一样，都是 `Operation`，都用 `forward` 执行**——这正是 ATB 图算子机制「统一调度」的体现。

> 注意 `get_inputs()` 里每个张量都 `.npu()` 了（[graph_example.py:24-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L24-L39)），与单算子一致——所有参与计算的张量必须在 NPU 上。图算子的输入顺序按 `add_input` 的调用顺序对应 `forward(inputs)` 列表。

#### 4.4.4 代码实践（阅读型）

**实践目标**：读懂 `graph_example.py` 的组图与执行流程，为第 5 单元铺路。

**操作步骤**：

1. 打开 [example/graph_example.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py)。
2. 在 `graph_build()` 中数出共有几个 `add_input`、几个 `add_node`，分别是什么算子。
3. 追踪 `self_attention_out` 这个中间张量是如何从上一个节点的输出变成下一个节点（ElewiseAdd）的输入的。

**需要观察的现象**：节点之间通过「`node.get_output(0)` → 作为下游 `add_node` 的输入名」串联，形成 DAG。

**预期结果**：能画出 6 个节点（SelfAttention、ElewiseAdd、LayerNorm、Linear、Tanh、Linear）的有向无环图，标出每条边的张量来源。

#### 4.4.5 小练习与答案

**练习 1**：`graph.build()` 返回的对象和 `torch_atb.Operation(linear_param)` 返回的对象，在 Python 类型上有什么共同点？

**参考答案**：两者都是 C++ `OperationWrapper` 包装出来的同一个 Python 类 `Operation`，都有 `forward` 方法。区别只在于内部持有的 `atb::Operation` 一个是单算子、一个是图算子（`GraphParam` 构造）。这统一了调用接口。

**练习 2**：为什么图算子要把多个算子合成一个 `Operation` 再 `forward`，而不是连续调用多个单算子 `forward`？

**参考答案**：合成图算子后，ATB 可以对整图统一调度、复用 workspace、减少 Host 侧重复的 Setup/launch 开销，从而缓解 Host Bound（见 u1-l1）。逐个单算子调用则每次都要独立 Setup、独立下发，开销更大。这正是图算子相对于「单算子手动拼接」的核心收益。

---

## 5. 综合实践

把本讲的「单算子调用」和「图算子一瞥」串起来，完成下面这个小任务：

**任务**：用 torch_atb 构建一个最小图算子，包含两个 Linear（无 bias）串联，并在 NPU 上跑通、打印输出。

要求：

1. 用 `Builder` 建图，`add_input` 声明一个输入 `x`。
2. 用 `LinearParam`（`has_bias=False`）加第一个 Linear 节点，输入为 `x` 和一个权重 `w0`（也用 `add_input` 声明）。
3. 把第一个 Linear 的输出接到第二个 Linear（权重 `w1`）。
4. `mark_output` 标记第二个 Linear 的输出，`build()` 后 `forward`。
5. 打印输出形状。

**参考思路（示例伪代码）**：

```python
import torch
import torch_npu  # noqa
import torch_atb

graph = torch_atb.Builder("TwoLinear")
x   = graph.add_input("x")
w0  = graph.add_input("w0")
w1  = graph.add_input("w1")

linear_param = torch_atb.LinearParam()
linear_param.has_bias = False

n0 = graph.add_node([x, w0], linear_param)
n1 = graph.add_node([n0.get_output(0), w1], linear_param)
graph.mark_output(n1.get_output(0))
Graph = graph.build()

k = 4        # in_features
m = 3        # mid_features
n = 2        # out_features
# transpose_b=True：权重形状按 (out, in) 给
xin = torch.randn(2, k, dtype=torch.float16).npu()
win0 = torch.randn(m, k, dtype=torch.float16).npu()   # -> (2,m)
win1 = torch.randn(n, m, dtype=torch.float16).npu()   # -> (2,n)
out = Graph.forward([xin, win0, win1])
torch.npu.synchronize()
print("out shape:", out[0].shape)   # 预期 torch.Size([2, 2])
```

**验收**：输出形状为 `(2, 2)`，且与「两次 `Linear.forward` 串联」的数值一致（可选：再写一段单算子串联版本对比）。**待本地验证**。

> 如果遇到维度不匹配错误，复习 4.2.3 末尾对 `transpose_b=True` 的说明：权重按 `(out_features, in_features)` 给出，内部按转置参与矩阵乘。

## 6. 本讲小结

- `import torch_atb` 会先 `ctypes.CDLL` 全局预加载 6 个 ATB `.so`、设置 `ATB_HOME_PATH`，再导入 pybind 扩展 `_C`；顺序不能颠倒。
- Python 调用算子三步走：构造 `Param` → `Operation(param)` → `forward([输入张量])`；Param 类型决定走哪个 C++ 构造函数。
- `forward` 内部 = `Setup` + `Execute` + `aclrtSynchronizeStream`，一行顶 C++ 五段；输入张量顺序必须匹配算子定义。
- torch↔atb 张量转换是**零拷贝**的指针共享；输出张量由 ATB 推导形状后在 NPU 上自动创建。
- Context 由 `GetAtbContext` 以 `thread_local` 方式自动托管，并绑定 TorchNPU 当前流；workspace 由 `MemoryManager` 自动管理——Python 端无需手动建 Context、分内存、做释放。
- 图算子用 `Builder` 组图，`build()` 返回的也是 `Operation`，统一用 `forward` 执行；图算子的收益是统一调度、复用 workspace、缓解 Host Bound。

## 7. 下一步学习建议

- **横向**：本讲和 u2-l1（C++ demo）、u2-l3（参数体系）同属「调用实战」单元，建议接着读 u2-l3，系统掌握各算子 Param 的字段与公共枚举，这样你就能举一反三地调用任意算子。
- **纵向（深入内核）**：若想知道 `operation_->Setup`/`Execute` 内部到底怎么跑到 Kernel，进入第 3 单元，从 u3-l1（OperationBase）→ u3-l2（Runner）→ u3-l3（AclnnRunner）依次读，本讲的 `OperationWrapper` 正是它们的 Python 外壳。
- **纵向（图算子）**：若对 4.4 的 Builder 感兴趣，第 5 单元（u5-l2～u5-l4）系统讲解图算子的 GraphParam/Node、GraphOpBuilder 与端到端示例。
- **动手**：综合实践完成后，可尝试把某个单算子（如 RMSNorm）也用 Python 跑通，巩固「Param → Operation → forward」三步法。
