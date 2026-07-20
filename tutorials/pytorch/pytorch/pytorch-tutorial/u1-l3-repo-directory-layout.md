# 仓库目录结构与代码组织

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `torch/`、`aten/`、`c10/`、`torch/csrc/`、`torchgen/` 五个核心目录各自承担什么职责。
- 在仓库里 **快速定位** 一段功能对应的源码：看到 `torch.add` 这种调用，能判断该去哪个目录找它的实现。
- 理解一次算子调用从 Python 到 C++ 的「目录穿越」路径，并据此建立一张源码导航地图。
- 区分「运行时目录」（`torch/`、`aten/`、`c10/`、`torch/csrc/`）与「构建期目录」（`torchgen/`）。

本讲承接 [u1-l1 PyTorch 项目定位与整体架构](./u1-l1-project-overview-and-architecture.md) 建立的分层架构认知，把那幅「Python 前端 + C++ 后端」的高层地图，细化到 **具体目录级别**。构建与安装的细节已在 [u1-l2 从源码构建与运行 PyTorch](./u1-l2-build-from-source.md) 讲过，本讲不再重复。

## 2. 前置知识

阅读本讲前，你需要了解几个通俗概念：

- **前端 / 后端**：在 PyTorch 里，「前端」指用户写 Python 时接触到的 API（`torch.tensor`、`nn.Linear` 等）；「后端」指真正干活的 C++ 代码（分配内存、调用 CUDA kernel）。前端是「门面」，后端是「厨房」。
- **绑定（binding）**：让 Python 能调用 C++ 函数的那层胶水代码。PyTorch 用 pybind11 和手写的 C 扩展来做这件事。
- **算子（operator / op）**：一个具体的张量运算，比如 `add`、`mul`、`conv2d`。PyTorch 里有上千个算子。
- **代码生成（codegen）**：因为算子太多、手写绑定太累，PyTorch 用一个工具读一份 YAML 定义，自动生成大量 C++/Python 样板代码。
- **DispatchKey**：可以暂时理解为「算子该交给哪个后端处理」的标签（CPU？CUDA？自动求导？）。这是 [u3 单元](../README.md) 的重点，本讲只需要知道这个概念「住在」`c10/` 里即可。

如果你对 `import torch` 时发生了什么还不清楚，建议先读 [u1-l4 Python 包入口与 torch 导入流程](./u1-l4-torch-import-and-entry.md) 的前置铺垫；本讲会直接使用 `torch._C` 这个名词。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 类型 | 作用 |
| ---- | ---- | ---- |
| `README.md` | 文档 | 顶层项目说明，含组件总览表 |
| `torch/` | 目录 | Python 前端包，用户 `import` 的对象 |
| `torch/__init__.py` | 源码 | `torch` 包的入口与模块说明 |
| `torch/csrc/` | 目录 | Python 绑定的 C++ 源码，编译出 `torch._C` |
| `torch/csrc/Module.cpp` | 源码 | `_C` 扩展模块的初始化入口 |
| `aten/` | 目录 | ATen 张量算子库（C++ 后端，不含 autograd） |
| `aten/src/README.md` | 文档 | 低层张量库的总体说明 |
| `aten/src/ATen/native/README.md` | 文档 | 「native function」机制与 YAML schema 说明 |
| `aten/src/ATen/native/native_functions.yaml` | 数据 | 所有算子的「单一事实来源」定义 |
| `aten/src/ATen/core/README.md` | 文档 | ATen Core（最小可部署子集）说明 |
| `c10/` | 目录 | 跨平台核心数据结构（`TensorImpl`、`Storage`、`DispatchKey`） |
| `c10/core/TensorImpl.h` | 源码 | 张量在 C++ 层的真正表示 |
| `c10/core/DispatchKey.h` | 源码 | 分发键的定义 |
| `torchgen/` | 目录 | 代码生成器（构建期工具，不参与运行时） |

下面这张精简的目录树可以先把全局结构印在脑子里（只列本讲关心的部分）：

```
pytorch/
├── torch/                 # ① Python 前端包（用户 import 的对象）
│   ├── nn/                #    神经网络模块
│   ├── autograd/          #    自动求导
│   ├── cuda/              #    CUDA 运行时管理
│   ├── distributed/       #    分布式训练
│   ├── optim/             #    优化器
│   ├── _dynamo/           #    torch.compile 的 Dynamo
│   ├── _inductor/         #    Inductor 编译后端
│   ├── _C/                #    C++ 绑定的 Python 桩（多为生成产物）
│   └── csrc/              # ② Python 绑定的 C++ 源码（编译出 torch._C）
├── aten/                  # ③ ATen 张量算子库（C++ 后端）
│   └── src/ATen/
│       ├── core/          #    ATen Core（最小子集，可移动端部署）
│       ├── native/        #    算子「现代」实现 + native_functions.yaml
│       ├── ops/           #    代码生成的算子声明 / 分发
│       ├── cuda/  cpu/    #    各后端实现
│       └── ...
├── c10/                   # ④ 跨平台核心（最底层）
│   ├── core/              #    TensorImpl / Storage / DispatchKey
│   ├── cuda/              #    CUDA 相关核心（流、缓存分配器）
│   └── ...
├── torchgen/              # ⑤ 代码生成器（构建期，读 yaml 生成绑定）
├── caffe2/                #    遗留库（ATen 之前的算子实现，逐步退役）
├── test/  docs/  tools/   #    测试 / 文档 / 维护脚本
└── third_party/           #    第三方依赖
```

我们用一个统一的「调用链」叙事把 ①~⑤ 串起来：当你写下 `torch.add(a, b)` 时，调用会从 **① `torch/`** 出发，经过 **② `torch/csrc/`** 这座桥，落到 **③ `aten/`** 的算子实现，而算子操作的底层张量对象定义在 **④ `c10/`**；这条链上的大量样板代码，则是由 **⑤ `torchgen/`** 在构建期生成的。本讲剩下的章节就按这条链依次展开。

## 4. 核心概念与源码讲解

### 4.1 torch/ —— Python 前端包

#### 4.1.1 概念说明

`torch/` 是用户 `import torch` 时实际加载的 Python 包。它的绝大部分内容是 **纯 Python**（外加一个名为 `_C` 的 C 扩展）。README 把它定位为「一个像 NumPy、但带强 GPU 支持的张量库」：

[README.md:L60-L62](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/README.md#L60-L62) —— 顶层组件表里 `torch` 一行，明确写了它的职责是「A Tensor library like NumPy, with strong GPU support」。

README 还特意强调了一个关键认知——PyTorch **不是**「对一个单体 C++ 框架的 Python 绑定」，而是「深度集成进 Python」的：

[README.md:L111-L112](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/README.md#L111-L112) —— 原文 "PyTorch is not a Python binding into a monolithic C++ framework."。这解释了为什么 `torch/` 目录里有大量「正经的 Python 逻辑」（比如 `nn/`、`optim/`、`distributed/`），而不是薄薄一层转发。

`torch/` 内部按子领域拆成了几十个子包，常见的有：

| 子包 | 职责 |
| ---- | ---- |
| `torch/nn/` | 神经网络模块（`Module`、`Linear`、`Conv2d` 等） |
| `torch/autograd/` | 自动求导（梯度模式、`Function`） |
| `torch/cuda/` | CUDA 设备管理、流、缓存分配器接口 |
| `torch/distributed/` | 分布式训练（DDP、FSDP、process group） |
| `torch/optim/` | 优化器（SGD、Adam 等） |
| `torch/fx/` | FX 图表示与变换 |
| `torch/_dynamo/` | `torch.compile` 的 Dynamo 捕获层 |
| `torch/_inductor/` | Inductor 默认编译后端 |
| `torch/_C/` | C++ 绑定的 Python 侧（多为生成产物） |

#### 4.1.2 核心流程

在调用链中，`torch/` 处于 **最顶端**：

```text
用户代码: torch.add(a, b)
   │
   ▼
torch/__init__.py 里导出的 add      ← 本模块（① torch/）
   │  （实际转发到 torch._C._VariableFunctions.add）
   ▼
torch._C （C 扩展）                 ← 下一站（② torch/csrc/）
```

也就是说，`torch/` 里的 Python 代码通常 **不直接做计算**，而是把参数整理好、转发给 C 扩展 `torch._C`。真正干活的 C++ 实现在后面几层。

#### 4.1.3 源码精读

`torch/__init__.py` 开头的模块 docstring 一句话定位了整个包：

[torch/__init__.py:L1-L9](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1-L9) —— 说明 `torch` 包「包含多维张量的数据结构，并定义这些张量上的数学运算」，还提到它「有一个 CUDA 对应物，可把张量计算放到 NVIDIA GPU 上」。

注意 docstring 之后紧跟的是一长串标准库 `import`（`builtins`、`ctypes`、`os`、`sys` 等），这说明 `torch/__init__.py` 是一个 **会真正执行初始化逻辑** 的入口文件，而不只是一个 re-export 壳子。导入流程的细节（`_load_global_deps`、`from torch._C import *`）在 u1-l1 已讲过，这里只需要记住：**`torch/__init__.py` 是整个前端包的根**。

#### 4.1.4 代码实践

**实践目标**：建立「在 `torch/` 里找东西」的直觉。

**操作步骤**：

1. 在仓库根目录执行 `ls torch/`，数一下有多少个一级子目录。
2. 针对下面三个日常用法，判断它们分别属于 `torch/` 的哪个子包：
   - `torch.nn.Linear(...)`
   - `torch.optim.Adam(...)`
   - `torch.cuda.is_available()`
3. 用 `ls torch/nn/` 与 `ls torch/optim/` 验证你的判断（应该能看到 `modules/`、`adam.py` 等文件）。

**需要观察的现象**：`torch/` 下既有面向用户的高层 API（`nn`、`optim`），也有以下划线开头的「内部」目录（`_dynamo`、`_inductor`、`_functorch`、`_subclasses`）。下划线前缀在 Python 里约定俗成表示「内部实现，不要直接依赖」。

**预期结果**：三个调用分别对应 `torch/nn/`、`torch/optim/`、`torch/cuda/`。`torch/` 是「用户门面 + 内部子系统」的混合体。

#### 4.1.5 小练习与答案

**练习 1**：`torch/` 下的 `_dynamo/` 和 `nn/` 都是子包，为什么前者用下划线前缀而后者不用？

> **参考答案**：`nn/` 是稳定公开 API（用户直接 `import torch.nn`），不带下划线；`_dynamo/` 是 `torch.compile` 的内部实现细节，下划线表示「属于内部、接口可能变动」，用户应通过 `torch.compile` 这个公开入口使用它，而不是直接 import `_dynamo` 里的东西。

**练习 2**：README 组件表里把 `torch` 描述成什么？为什么说它「不是一个对单体 C++ 框架的绑定」？

> **参考答案**：描述为「像 NumPy、带强 GPU 支持的张量库」。说它不是单体绑定，是因为 `torch/` 里有大量真实 Python 逻辑（网络层、优化器、分布式等），Python 是一等公民，而不是一层薄转发。

---

### 4.2 torch/csrc/ —— Python 绑定的 C++ 源码

#### 4.2.1 概念说明

`torch/csrc/` 是「Python 绑定的 C++ 源码」所在地。前面说 `torch/` 会把调用转发给 `torch._C`——那么 `torch._C` 这个扩展模块 **从哪里编译出来**？答案就是 `torch/csrc/`。它把后端的 C++ 能力（来自 `aten/`、`c10/`）「翻译」成 Python 可调用的函数、类、方法。

这层的存在，正是 README 所说的「深度集成进 Python」的技术落点：Python 侧的对象（如 `Tensor`）和 C++ 侧的对象（如 `at::Tensor`）通过这里的绑定代码互相对应。`torch/csrc/` 也按子系统分目录，例如 `autograd/`（自动求导引擎）、`jit/`（TorchScript）、`distributed/`、`dynamo/`、`api/`（C++ 前端 libtorch 的头文件）等。

#### 4.2.2 核心流程

在调用链中，`torch/csrc/` 是那座 **桥**：

```text
torch/  (Python)
   │  调用 torch._C.xxx
   ▼
torch._C  ← 这个扩展模块由 torch/csrc/ 编译而来（②）
   │  内部调用 at:: / c10::
   ▼
aten/  (C++ 算子实现)  ← 下一站（③）
```

所以 `torch/csrc/` 处于「Python 世界」与「C++ 后端世界」的交界处：往上对接 Python 解释器，往下对接 `aten/`、`c10/`。

#### 4.2.3 源码精读

`torch/csrc/Module.cpp` 是 `_C` 扩展模块的核心入口之一。它顶部就 `include` 了 ATen 的总头文件，说明这层直接依赖 `aten/`：

[torch/csrc/Module.cpp:L13](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L13) —— `#include <ATen/ATen.h>`，这行表明绑定层直接持有 ATen 的 C++ 接口。

文件靠后的位置定义了模块初始化函数 `initModule`，它是 Python 加载 `_C` 时真正执行的注册入口：

[torch/csrc/Module.cpp:L2491-L2493](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2491-L2493) —— `extern "C" TORCH_PYTHON_API PyObject* initModule();` 及其实现，这是把 C++ 符号注册进 Python 模块对象的关键函数。

> 提示：`Module.cpp` 是一个两千多行的大文件，初学不必通读，只要记住「`_C` 模块的初始化入口在这里」即可。

#### 4.2.4 代码实践

**实践目标**：确认 `torch._C` 与 `torch/csrc/` 的对应关系。

**操作步骤**：

1. 在仓库里 `ls torch/csrc/`，记录几个子目录（如 `autograd`、`jit`、`api`、`cuda`）。
2. 若本地已安装可运行的 torch，运行下面这段（否则跳到步骤 3）：

   ```python
   # 示例代码：确认 _C 是一个真实的编译扩展模块
   import torch
   print(type(torch._C))          # 期望是 module 类型
   print(torch._C.__file__)       # 期望指向一个 .so 共享库
   ```

3. 不运行也没关系：在 `torch/csrc/Module.cpp` 里搜索 `initModule`（约 L2491），记住这个函数就是 `_C` 的注册入口。

**需要观察的现象**：`torch._C` 不是一个普通 `.py` 文件，而是一个编译出的共享库（Linux 上是 `.so`）。它的源码就住在 `torch/csrc/`。

**预期结果**：`type(torch._C)` 为 `module`，`torch._C.__file__` 指向类似 `.../torch/lib/libtorch_python.so` 的路径（待本地验证具体路径）。这印证了「`torch/csrc/` 编译出 `torch._C`」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `torch/`（Python）和 `torch/csrc/`（C++）要分成两层，而不是把绑定代码也写在 `torch/` 里？

> **参考答案**：因为它们是两种语言、两套构建系统。`torch/csrc/` 是 C++，要由 CMake 编译成共享库；`torch/` 是纯 Python，由 pip 打包。分层让 Python 逻辑与 C++ 绑定各自独立演进，也让「替换后端」成为可能（只要保持绑定接口不变）。

**练习 2**：`torch/csrc/autograd/` 和 `torch/autograd/` 都叫 autograd，它们是什么关系？

> **参考答案**：`torch/autograd/` 是 Python 侧的自动求导 API 与模式管理（如 `no_grad`）；`torch/csrc/autograd/` 是 C++ 侧的自动求导引擎（反向图的线程化执行）。Python 侧通过 `torch._C` 调用 C++ 引擎——二者是「前端 API」与「后端实现」的关系。

---

### 4.3 aten/ —— ATen 张量算子库

#### 4.3.1 概念说明

`aten/` 是 **ATen**（"A Tensor Library"）的所在地，是 PyTorch 的 C++ 算子库。一个关键点：**ATen 不含 autograd**——它只负责「把算子算对」，自动求导是另一层（在 `torch/csrc/autograd/` 与 `torch/autograd/`）叠加在上面的。

`aten/src/README.md` 一开篇就点明了这层的定位：

[aten/src/README.md:L1-L2](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/README.md#L1-L2) —— 「This directory contains the low-level tensor libraries for PyTorch」，并提到这些库的血统可以追溯到最初的 Torch（TH/THC 等缩写的由来）。

`aten/src/ATen/` 下按职责细分：

| 子目录 | 职责 |
| ---- | ---- |
| `core/` | ATen Core：最小子集，约束二进制体积，可部署到移动端 |
| `native/` | 算子的「现代」实现 + 算子定义文件 `native_functions.yaml` |
| `ops/` | 由 `native_functions.yaml` **生成** 的算子声明与分发骨架 |
| `cuda/`、`cpu/` | 各后端的算子实现 |
| `quantized/` | 量化算子 |
| `vulkan/`、`metal/`、`mps/` | 其他后端 |

`aten/src/ATen/native/README.md` 解释了「native function」这套现代机制：

[aten/src/ATen/native/README.md:L1-L4](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L1-L4) —— 原文：「ATen "native" functions are the modern mechanism for adding operators … Native functions are declared in `native_functions.yaml`」。也就是说，**新增算子的「单一事实来源」就是那个 YAML 文件**。

而 `core/README.md` 说明了 ATen Core 的特殊定位：

[aten/src/ATen/core/README.md:L1-L4](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/core/README.md#L1-L4) —— 「ATen Core is a minimal subset of ATen which is suitable for deployment on mobile. Binary size … is an important constraint.」。这解释了为什么 `core/` 要和 `native/` 分开：前者必须「小而精」。

#### 4.3.2 核心流程

在调用链中，`aten/` 是 **执行层**：`torch._C` 转发过来的调用，最终落到 `aten/` 里某个算子的 C++ 实现去真正计算。

```text
torch._C.add(...)
   │
   ▼
at::add(...)            ← aten/ 里生成的命名空间函数
   │  按 DispatchKey 选 kernel
   ▼
at::native::add_cpu / add_cuda   ← aten/src/ATen/native/ 下的具体实现
```

注意「按 DispatchKey 选 kernel」这一步——同一个算子（如 `add`）会因为张量在 CPU 还是 CUDA 上，被分发到不同的实现函数。这种「按后端分目录 + 按键分发」是 `aten/` 的组织主旋律。DispatchKey 本身定义在下一节的 `c10/` 里。

#### 4.3.3 源码精读

`native_functions.yaml` 是整个算子体系的「源头」。以加法算子为例：

[aten/src/ATen/native/native_functions.yaml:L542-L548](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L542-L548) —— `add.Tensor` 的 schema：

```yaml
- func: add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor
  device_check: NoCheck   # TensorIterator
  structured_delegate: add.out
  variants: function, method
  dispatch:
    SparseCPU, SparseCUDA, ...: add_sparse
    SparseCsrCPU, SparseCsrCUDA, ...: add_sparse_csr
```

读法（只求看懂，细节留到 u3-l1）：

- `func` 一行声明了函数名、参数类型与返回类型。
- `variants: function, method` 表示它会同时生成 `at::add(...)` 函数和 `tensor.add(...)` 方法。
- `dispatch:` 把不同后端（这里是稀疏后端）映射到不同的实现函数。

这个 YAML 文件长达数千行，是 **PyTorch 里最重要的单一配置文件之一**——改它就等于改了算子的公开接口。

#### 4.3.4 代码实践

**实践目标**：亲手在「单一事实来源」里找一个算子，建立对 YAML schema 的第一印象。

**操作步骤**：

1. 确认文件路径：`ls aten/src/ATen/native/native_functions.yaml`（**记录下这个绝对路径**，这是本讲综合实践要交的作业之一）。
2. 在文件里搜索 `func: add.out`，找到它的 schema 行（约 L565）。注意它的参数里有 `Tensor(a!) out`——带感叹号的标注表示「这个参数会被写入」，对应 Python 里的 `out=` 用法。
3. 对比 `add.Tensor`（L542）和 `add.out`（L565）两条 schema，说出它们的区别。

**需要观察的现象**：同一个「加法」概念在 YAML 里出现了多条记录（`add.Tensor`、`add.out`，以及对应的 `add_`）。这其实是同义算子的不同调用形态（函数式、带 out、原地）。

**预期结果**：`add.Tensor` 返回新张量；`add.out` 多一个可写的 `out` 参数，用于把结果写入已有张量；它们都服务于「加法」这一个概念。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ATen 要把 `core/` 和 `native/` 分开存放？

> **参考答案**：`core/` 是为移动端等体积敏感场景保留的「最小子集」，二进制大小是硬约束；`native/` 则是完整的算子实现集合。分开存放让最小部署包可以只带走 `core/` 而不拖上全部算子。

**练习 2**：`aten/src/ATen/ops/` 里的代码是手写的还是生成的？依据是什么？

> **参考答案**：是 **生成** 的。`aten/src/ATen/native/README.md` 多次提到 code generation，且 `ops/` 里是算子声明与分发骨架，真正的算法体在 `native/`。`ops/` 的内容由 `torchgen/` 读 `native_functions.yaml` 后产出（见 4.5）。

---

### 4.4 c10/ —— 核心数据结构与 dispatcher

#### 4.4.1 概念说明

`c10/` 是 PyTorch 里 **最底层、最精简** 的跨平台核心库。名字 `c10` 可理解为「C++ 的最小核心」。无论前端是 Python 还是 C++（libtorch），无论后端是 CPU、CUDA 还是别的，**大家都依赖 `c10/`**。它提供了三件最基础的东西：

- **张量的真正表示** `TensorImpl`（张量的 sizes、strides、dtype 等元信息都在这）。
- **存储抽象** `StorageImpl`（指向真实内存的数据指针）。
- **分发机制** `DispatchKey` / `DispatchKeySet`（决定一个算子调用该走哪个实现）。

`c10/` 也按平台/职责分目录：`c10/core/`（核心数据结构）、`c10/cuda/`（CUDA 相关核心，如流、缓存分配器）、`c10/hip/`、`c10/xpu/`、`c10/util/`（通用工具）、`c10/macros/`（平台宏）等。注意它 **不依赖 `aten/`**——依赖方向是 `aten/` → `c10/`，单向的。

#### 4.4.2 核心流程

在调用链中，`c10/` 处于 **最底端**：

```text
aten/ 里的算子实现 at::native::add_cpu(...)
   │  它操作的「张量」对象，本质是
   ▼
c10::TensorImpl   ← 张量的 C++ 表示（sizes/strides/dtype）
   │  它持有的数据指针来自
   ▼
c10::StorageImpl  ← 真实内存
```

换句话说，`aten/` 的算子代码「操纵的对象」就是 `c10/` 定义的。这层把「张量是什么」这件最根本的事钉死，让上层所有后端共享同一套定义。

#### 4.4.3 源码精读

`c10/core/` 下集中了最关键的头文件，`TensorImpl.h` 是其中之一。它的 include 列表本身就揭示了「张量对象由哪些更基本的概念组成」：

[c10/core/TensorImpl.h:L1-L15](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L1-L15) —— 这段 include 了 `Device.h`、`DispatchKeySet.h`、`Layout.h`、`MemoryFormat.h`、`ScalarType.h`、`Storage.h` 等，正好对应张量的「设备 / 分发键 / 布局 / 内存格式 / 标量类型 / 存储」这几个维度。看懂这张 include 列表，就大致看懂了「一个张量需要哪些元信息」。

分发键的定义则在另一个头文件里，注释把概念讲得很直白：

[c10/core/DispatchKey.h:L13-L20](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/DispatchKey.h#L13-L20) —— 注释解释 `BackendComponent`「identifies a "backend" for our dispatch」，并说明它在 `DispatchKeySet` 里对应一个具体的「比特位」。这就是「同一个算子、不同后端」能被区分开来的底层机制。

> 这些文件的细节（`TensorImpl` 的字段布局、`DispatchKeySet` 的位运算）会在 [u3-l3 DispatchKey 与 Dispatcher 分发机制](./u3-l3-dispatchkey-and-dispatcher.md) 和 [u3-l4 TensorImpl C++ 核心数据结构](./u3-l4-tensorimpl-cpp-core.md) 深入，本讲只需建立「它们住在 `c10/core/`」的导航印象。

#### 4.4.4 代码实践

**实践目标**：熟悉 `c10/core/` 这个「最底层工具箱」里都有什么。

**操作步骤**：

1. 执行 `ls c10/core/*.h`，把文件名列出来。
2. 按名字猜职责，重点圈出下面几个：
   - `TensorImpl.h`（张量表示）
   - `StorageImpl.h`（存储表示）
   - `DispatchKey.h`、`DispatchKeySet.h`（分发机制）
   - `Device.h`、`DeviceType.h`（设备抽象）
   - `ScalarType.h`（dtype 枚举）
   - `Layout.h`、`MemoryFormat.h`（布局与内存格式）
3. 用一句话写下：为什么这些文件集中在 `c10/` 而不是 `aten/`？

**需要观察的现象**：`c10/core/` 里全是 `.h` / `.cpp` 形式的基础类型定义，**没有任何算子实现**（没有 `add`、`conv`）。它只定义「张量是什么」「如何分发」，不定义「怎么算」。

**预期结果**：你会得到一张「张量基础设施清单」——这正是上层 `aten/` 与 `torch/csrc/` 共同依赖的地基。

#### 4.4.5 小练习与答案

**练习 1**：`c10/` 和 `aten/` 谁依赖谁？为什么这个方向很重要？

> **参考答案**：`aten/` 依赖 `c10/`（方向是 `aten → c10`），反过来不成立。这个单向依赖很重要：它保证 `c10/` 可以被单独拿出 来用于最小部署（比如移动端、libtorch），而不会被算子实现的庞大体积拖累。这也呼应了 `aten/src/ATen/core/README.md` 里「最小子集 / 二进制体积」的关注点。

**练习 2**：如果你想知道「张量在 C++ 里到底长什么样」，应该去哪个目录？如果想找「加法算子的具体计算代码」，又该去哪个目录？

> **参考答案**：前者去 `c10/core/`（`TensorImpl.h`）；后者去 `aten/src/ATen/native/`（CPU 实现常在 `native/` 下、CUDA 实现在 `native/cuda/`）。这正是本讲要建立的「按问题类型选目录」的导航能力。

---

### 4.5 torchgen/ —— 代码生成工具

#### 4.5.1 概念说明

`torchgen/` 是 PyTorch 的 **代码生成器**，它是一个 **构建期工具**，不参与运行时。前面反复提到「`native_functions.yaml` 是单一事实来源」「`aten/src/ATen/ops/` 是生成的」——那么是谁读 YAML、生成那些代码？就是 `torchgen/`。

为什么需要它？因为 PyTorch 有上千个算子，每个算子都要生成：C++ 的声明、按后端的分发骨架、Python 绑定、类型桩（`.pyi`）、autograd 的接线……手写这些既枯燥又易错。`torchgen/` 的策略是：**维护一份 YAML 定义，自动产出所有样板**。

`torchgen/` 下的关键文件（本讲只需认名字，细节留到 u3-l2）：

| 文件 | 作用 |
| ---- | ---- |
| `torchgen/gen.py` | 生成流程的总入口 |
| `torchgen/model.py` | YAML 的数据模型（`NativeFunction`、`FunctionSchema` 等） |
| `torchgen/native_function_generation.py` | native function 的生成规则 |
| `torchgen/dest/` | 生成目标的描述（产物写到哪里） |
| `torchgen/decompositions/` | 算子分解（decomposition）定义 |

#### 4.5.2 核心流程

`torchgen/` 的工作时机和前几个目录不同——它发生在 **构建期**，而不是运行时：

```text
构建期（pip install / CMake 触发）：
   native_functions.yaml
        │  被 torchgen/gen.py 读取
        ▼
   生成大量代码
        ├─→ aten/src/ATen/ops/*          （算子声明 / 分发）
        ├─→ torch/csrc 里部分绑定代码
        ├─→ torch/_C/__init__.pyi 等桩
        └─→ ...
        
运行时（用户 import torch）：
   torch/ → torch._C → aten/ → c10/
   （这条链用到的代码，很多是上一步生成的）
```

也就是说，`torchgen/` 是「站在所有运行时目录之外」的元工具：它生产出 ①~④ 各层需要的部分代码，但自己不进入运行时调用链。

#### 4.5.3 源码精读

最直接的证据来自 `native/README.md` 本身——它在讲到 `dispatch` 字段时，把读者明确指向了 `torchgen/gen.py`：

[aten/src/ATen/native/README.md:L311-L312](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L311-L312) —— 原文：「Available backend options can be found by searching `dispatch_keys` in codegen …/torchgen/gen.py」。这一行同时确认了两件事：① 后端键的合法集合由 codegen 决定；② codegen 的入口就在 `torchgen/gen.py`。

同一份 README 还透露了「生成产物落在哪」：

[aten/src/ATen/native/README.md:L494](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L494) —— 提到生成的 kernel 「can be found in `<gen-out>/aten/src/ATen/CompositeViewCopyKernels.cpp`」，说明生成代码会落到一个 `gen-out` 输出目录，再被后续编译。

> 提示：初学阶段不必读 `torchgen/` 的实现，只要记住「它读 `native_functions.yaml`，产出绑定与桩」即可。深入阅读留到 [u3-l2 TorchGen 代码生成机制](./u3-l2-torchgen-codegen.md)。

#### 4.5.4 代码实践

**实践目标**：在不运行 codegen 的前提下，确认 `torchgen/` 的入口与产物形态。

**操作步骤**：

1. `ls torchgen/*.py`，确认 `gen.py`、`model.py`、`native_function_generation.py` 这几个核心文件存在。
2. 打开 `torchgen/gen.py`（只看文件头与函数名），找到它接收 `native_functions.yaml` 路径作为输入的迹象（函数名/参数名里常出现 `native_functions`、`gen_*` 字样）。
3. 写下一句话：如果有人改了 `native_functions.yaml` 里某个算子的 schema，`torchgen/` 会让哪些地方的代码跟着变？

**需要观察的现象**：`torchgen/` 是纯 Python，且与运行时无关——你可以把它看成一个「翻译器」：YAML 进，C++/Python 出。

**预期结果**：改 YAML 后，`aten/src/ATen/ops/` 的声明、相关绑定、`.pyi` 桩都会被重新生成；下次构建后这些变化才生效。这解释了为什么改算子接口要重新构建（呼应 u1-l2）。

#### 4.5.5 小练习与答案

**练习 1**：`torchgen/` 会不会出现在「用户运行 `torch.add` 」的调用栈里？为什么？

> **参考答案**：不会。`torchgen/` 是构建期工具，在 `pip install` / CMake 阶段就把代码生成好了；运行时 `torch.add` 走的是 `torch/ → torch._C → aten/ → c10/` 这条已经生成完毕的链路，不会再回头调用 `torchgen/`。

**练习 2**：既然 `native_functions.yaml` 是「单一事实来源」，为什么仓库里还能看到手写的算子实现（比如 `aten/src/ATen/native/` 下的 `.cpp`）？

> **参考答案**：YAML 定义的是算子的 **接口与分发骨架**（schema、后端映射），而 **算法体**（具体怎么算）仍由人手写在 `native/` 下的 `.cpp` / `.cu` 里。codegen 生成的是「接线」（声明、分发、绑定），不是「算法」本身。

---

## 5. 综合实践

把本讲五个目录串起来，完成下面这张「源码导航地图」作业。

**任务背景**：假设有同事问你「`torch.add(a, b)` 这个调用，源码到底分布在仓库哪些目录？」，请你用本讲学到的目录职责，给出一份可追溯的答案。

**操作步骤**：

1. **画目录职责表**：为 `torch/`、`aten/`、`c10/`、`torchgen/`、`torch/csrc/` 各写一句话职责说明（不要照抄本讲原文，用你自己的话）。
2. **定位关键文件**：找到并记录 `aten/src/ATen/native/native_functions.yaml` 的完整相对路径（这是本讲规格里点名要交的成果）。
3. **追踪一次调用**：按下面的填空，把 `torch.add` 的目录穿越路径补全：

   ```text
   torch.add(a, b)
     ① 定义在 Python 包 ________ 里（如 torch/__init__.py 导出）
     ② 转发到 C 扩展 torch._C，其源码在 ________ 目录
     ③ 最终落到 C++ 算子库 ________ 目录的实现
     ④ 算子操纵的张量对象，定义在 ________ 目录
     ⑤ 上述各层的绑定/声明骨架，由构建期工具 ________ 目录生成
   ```

4. **自检**：对照本讲 4.1–4.5 的结论，检查你填的五个空是否分别是 `torch/`、`torch/csrc/`、`aten/`、`c10/`、`torchgen/`。
5. **进阶（可选）**：用 `grep -rn "def add" torch/__init__.py` 或在 `aten/src/ATen/native/native_functions.yaml` 里搜 `add.Tensor`，亲手验证其中一两个环节（若本地有可运行 torch，还可 `print(torch._C.__file__)` 验证 ②）。

**预期成果**：一张你亲手整理的「五目录职责表 + native_functions.yaml 路径 + 调用链填空答案」。做完后，你应该能在仓库里 **凭目录名判断** 一段功能该去哪里读源码——这就是本讲要建立的导航能力。

## 6. 本讲小结

- PyTorch 仓库由五个核心目录构成一张导航地图：`torch/`（Python 前端）、`torch/csrc/`（Python↔C++ 绑定）、`aten/`（C++ 算子库）、`c10/`（最底层核心数据结构）、`torchgen/`（构建期代码生成器）。
- 一次 `torch.add` 调用穿越的目录路径是：`torch/ → torch/csrc/（编译出 torch._C）→ aten/ → c10/`，而这条链上的大量样板代码由 `torchgen/` 在构建期生成。
- `torch/` 是用户 `import` 的纯 Python 包（外加 `_C` 扩展），README 强调它「不是对单体 C++ 框架的绑定」，而是深度集成进 Python。
- `aten/` 的算子接口「单一事实来源」是 `aten/src/ATen/native/native_functions.yaml`；`aten/` 不含 autograd，只负责「把算子算对」。
- `c10/` 是最底层、最精简的跨平台核心（`TensorImpl`、`StorageImpl`、`DispatchKey`），依赖方向是 `aten/ → c10/` 单向。
- `torchgen/` 不参与运行时，只在构建期把 YAML 翻译成各层的声明、绑定与类型桩。

## 7. 下一步学习建议

- 想看懂 `import torch` 时这条链是怎么被「启动」的，接着读 [u1-l4 Python 包入口与 torch 导入流程](./u1-l4-torch-import-and-entry.md)，它会带你走一遍 `torch/__init__.py` 的加载顺序。
- 想深入张量这个最核心对象，进入 Unit 2：先读 [u2-l1 Tensor 的 Python 实现](./u2-l1-tensor-python-class.md)，再到 [u2-l2 Storage 与内存布局](./u2-l2-storage-and-memory-layout.md)，把 `c10/core/TensorImpl.h` 与 Python 侧的 `Tensor` 对应起来。
- 想搞清楚「算子如何从 YAML 变成可调用代码」，进入 Unit 3：依次读 [u3-l1 native_functions.yaml 算子模式定义](./u3-l1-native-functions-yaml-schema.md)、[u3-l2 TorchGen 代码生成机制](./u3-l2-torchgen-codegen.md)、[u3-l3 DispatchKey 与 Dispatcher 分发机制](./u3-l3-dispatchkey-and-dispatcher.md)，这三讲正好对应本讲 4.3、4.5、4.4 的深化。
- 建议在本讲综合实践的「五目录职责表」基础上，随着后续学习不断补充每个目录的细节，最终形成你自己的 PyTorch 源码地图。
