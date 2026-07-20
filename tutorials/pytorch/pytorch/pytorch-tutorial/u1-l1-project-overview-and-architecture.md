# PyTorch 项目定位与整体架构

## 1. 本讲目标

本讲是整本 PyTorch 学习手册的第一篇。读完本讲后，你应当能够：

- 说清楚 PyTorch 到底是什么——它同时是一个「GPU 张量库」和一个「动态计算图（自动求导）框架」。
- 识别 PyTorch 的顶层组件：`torch`、`torch.autograd`、`torch.nn`、`torch.jit`、`torch.multiprocessing`、`torch.utils` 等，并说出各自的职责。
- 建立「Python 前端 + C++ 后端」的分层心智模型，知道 `torch/`、`aten/`、`c10/`、`torch/csrc/`、`torchgen/`、`caffe2/` 这些顶层目录分别装了什么。
- 看懂 `torch/__init__.py` 在你执行 `import torch` 时做了哪几件关键的事。

本讲不要求你懂深度学习或 C++，只要会基本的 Python 即可。我们只建立「地图」，不深入任何一条具体机制——那些留给后续讲义。

## 2. 前置知识

- **张量（Tensor）**：可以理解成「多维数组」。NumPy 里的 `ndarray` 就是一种张量。一个形状为 `(3, 4)` 的张量，可以想象成 3 行 4 列的表格；形状 `(2, 3, 4)` 则是两层这样的表格。PyTorch 里几乎所有数据（神经网络的输入、权重、梯度）都是张量。
- **GPU 加速**：GPU 擅长大量并行的数值运算。把张量放到 GPU 上，矩阵乘法等运算可以比 CPU 快几十倍。PyTorch 的一个核心卖点就是「同一份代码，张量既能放 CPU 也能放 GPU」。
- **自动求导（autograd）**：训练神经网络本质是用梯度下降更新参数，而梯度需要对前向计算求导。autograd 能在你写前向代码的同时，自动记录一张「反向计算图」，调用 `.backward()` 时沿这张图自动算出所有梯度。这让你不必手写导数公式。
- **动态图 vs 静态图**：早期框架（如 TensorFlow 1.x）要求你先把整个网络结构搭好（静态图），再反复运行；PyTorch 则是「执行到哪、图就建到哪」（动态图），每次前向都可以不一样，调试也更直观。
- **C++ 与 Python 混合**：PyTorch 用户写的是 Python，但真正做矩阵运算的「算子（operator）」是用 C++/CUDA 实现的，再通过绑定（binding）暴露给 Python。理解这种分层，是读懂 PyTorch 源码的前提。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目说明书。包含 PyTorch 的定位说明、**组件总览表**、安装方式。本讲大量内容来自这里。 |
| `torch/__init__.py` | Python 包的入口。`import torch` 时最先执行的就是它，负责加载 C++ 后端、组装顶层符号。 |
| `CONTRIBUTING.md` | 贡献指南。其中「Codebase structure」一节是官方对仓库目录布局最权威的说明。 |
| `version.txt` | 只有一行，记录当前版本号。 |
| `torch/`、`aten/`、`c10/`、`torch/csrc/`、`torchgen/`、`caffe2/` | 顶层目录，分别承载 Python 前端、C++ 算子库、核心数据结构、Python 绑定、代码生成器、Caffe2 遗留库。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 PyTorch 的双重定位** —— 它同时是张量库和自动求导框架。
2. **4.2 顶层组件总览** —— 六大组件各管什么。
3. **4.3 分层架构：Python 前端 + C++ 后端** —— 顶层目录的职责划分。
4. **4.4 torch 包入口与 import 流程** —— `import torch` 背后发生了什么。

### 4.1 PyTorch 的双重定位

#### 4.1.1 概念说明

很多人对 PyTorch 的第一印象是「深度学习框架」。但官方在 README 里给出的定义更本质：PyTorch 是一个 Python 包，提供**两大**高层能力。这两大能力是相对独立的，你可以只用其中一个：

1. **张量计算（带强 GPU 加速）**：即使你根本不碰神经网络，也可以把 PyTorch 当成一个「GPU 版的 NumPy」来用——做大规模矩阵运算、科学计算。
2. **基于 tape 的自动求导**：在你写前向计算时，系统像一台「磁带录音机」一样把运算过程录下来，事后倒带（reverse-mode）自动算出梯度，从而支持任意可微的张量运算。

> 术语解释：**tape-based（基于磁带）** 是自动求导的一种比喻——前向计算像录音，把每一步操作记到「磁带」上；反向传播就像倒放磁带，沿记录一步步算导数。它等价于「动态计算图 + reverse-mode 自动微分」。

这两条线在源码里对应不同组件：张量计算主要在 `aten/`（C++ 算子库）和 `torch/`（Python 封装），自动求导则在 `torch/autograd/`（Python 侧）和 `torch/csrc/autograd/`（C++ 引擎）。

#### 4.1.2 核心流程

理解 PyTorch 定位的最简心智模型：

```
            ┌─────────────────────────────────────────┐
你的 Python │  1. 创建/搬运张量  →  张量计算（可上 GPU）│  ← 能力一：张量库
   代码     │  2. 写前向运算      →  自动录制成反向图   │  ← 能力二：自动求导
            │  3. loss.backward() →  沿反向图算梯度     │
            └─────────────────────────────────────────┘
```

- 你**不必**为了用张量计算而启用自动求导（用 `torch.no_grad()` 关掉即可）。
- 你也**不必**为了用自动求导而训练神经网络（任何可微函数都能求导）。
- 神经网络（`torch.nn`）只是把这两条线组合起来，外加模块化抽象。

#### 4.1.3 源码精读

README 开篇一句话就给出了这两大定位：

[README.md:L9-L11](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/README.md#L9-L11) —— 这三行是整份 README 的总纲：PyTorch 是一个 Python 包，提供「带 GPU 加速的张量计算」和「基于 tape 自动求导的深度神经网络」两大能力。

紧接着，README 在「Usually, PyTorch is used either as:」里也列举了两种典型用法——要么当 NumPy 的 GPU 替代品，要么当灵活的深度学习研究平台。

与之呼应，`torch/__init__.py` 顶部的模块 docstring 同样强调了「多维张量 + 数学运算」，并指出存在 CUDA 对应物：

[torch/__init__.py:L1-L9](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1-L9) —— 这段 docstring 说明 `torch` 包提供多维张量的数据结构与数学运算、序列化等工具，并提到它有一个运行在 NVIDIA GPU 上的 CUDA 对应实现。注意 docstring 里只描述了「张量库」这一面，「自动求导」这一面由 `torch/autograd/` 单独承载——这从侧面印证了两条能力在源码上是分离的。

#### 4.1.4 代码实践

**实践目标**：用最小代码感受「能力一（张量计算）」与「能力二（自动求导）」是两件事。

**操作步骤**（示例代码，可在本地任意 Python 环境运行）：

```python
import torch

# 能力一：纯张量计算（不记录梯度）
with torch.no_grad():                       # 关闭自动求导
    a = torch.tensor([[1.0, 2.0], [3.0, 4.0]])
    b = torch.tensor([[5.0, 6.0], [7.0, 8.0]])
    c = (a * b).sum()
    print("纯张量计算结果:", c, " grad_fn =", c.grad_fn)   # grad_fn 应为 None

# 能力二：带自动求导的张量
x = torch.tensor([1.0, 2.0, 3.0], requires_grad=True)
y = (x ** 2).sum()                          # 前向：自动录制反向图
y.backward()                                # 反向：自动算梯度
print("y =", y.item(), " dy/dx =", x.grad)  # 期望 dy/dx = 2x = [2,4,6]
```

**需要观察的现象**：
- 第一段里 `c.grad_fn` 为 `None`，说明 `no_grad` 下没有构建反向图。
- 第二段里 `x.grad` 自动得到了 `[2, 4, 6]`，正是 \(y=\sum x_i^2\) 的解析导数 \( \frac{\partial y}{\partial x_i}=2x_i \)。

**预期结果**：第一段打印 `grad_fn = None`；第二段打印 `dy/dx = tensor([2., 4., 6.])`。

> 说明：上述为示例代码，用于帮助理解两类能力的区别；具体数值结果**待本地验证**（取决于你的 torch 版本与设备）。

#### 4.1.5 小练习与答案

**练习 1**：如果只把 PyTorch 当「GPU 版 NumPy」用，是否需要调用 `.backward()`？
> **答案**：不需要。`.backward()` 属于自动求导能力；纯张量计算（创建、运算、搬运到 GPU）不依赖它，甚至可以用 `torch.no_grad()` 显式关闭以省内存、提速。

**练习 2**：`tape-based autograd` 中的「tape」指什么？
> **答案**：指前向计算时自动记录下来的运算序列（即反向计算图）。反向传播时系统沿这条记录「倒带」，逐 op 计算导数并链式相乘。

---

### 4.2 顶层组件总览

#### 4.2.1 概念说明

PyTorch 不是一个「单块」框架，而是由若干个相对独立的子包组成的「工具箱」。README 给出了一张**组件总览表**，是理解整体架构最权威的入口。每个组件都对应 `torch/` 目录下的一个子模块。

需要特别留意：README 这张表里列出了 6 个组件，其中**没有**单独列出 `torch.distributed`（分布式训练）。但 `torch.distributed` 在 `torch/` 目录里确实是一个独立的大子包，只是官方把它视作更上层的「功能域」而非核心组件。本讲我们忠实于 README 的 6 项划分，同时在目录讲解时补充 `distributed` 的存在。

#### 4.2.2 核心流程

六大组件的协作关系可以这样理解：

```
        ┌──────────────┐   用户写 nn.Module（网络结构）
        │  torch.nn    │
        └──────┬───────┘
               │  前向计算用到张量与算子
               ▼
        ┌──────────────┐   张量 + 算子（CPU/GPU）
        │    torch     │
        └──────┬───────┘
               │  可微运算被自动记录
               ▼
        ┌──────────────┐   自动求导引擎
        │ torch.autograd│
        └──────────────┘

  横向辅助：
   torch.utils (DataLoader 等工具)
   torch.multiprocessing (跨进程共享张量)
   torch.jit (把模型编译/序列化为 TorchScript)
```

- `torch.nn` 建立在 `torch` 张量之上，并深度集成 `torch.autograd`。
- `torch.utils`、`torch.multiprocessing` 是辅助工具，分别管数据加载与跨进程张量共享。
- `torch.jit` 是一条相对独立的「编译/序列化」支线，把 Python 模型转成可独立部署的 TorchScript。

#### 4.2.3 源码精读

README 的组件表是本讲最核心的一段源码：

[README.md:L58-L67](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/README.md#L58-L67) —— 这张表逐行列出了 PyTorch 的顶层组件及其一句话职责。摘录如下：

| 组件 | README 的一句话职责 |
| --- | --- |
| **torch** | A Tensor library like NumPy, with strong GPU support |
| **torch.autograd** | A tape-based automatic differentiation library that supports all differentiable Tensor operations in torch |
| **torch.jit** | A compilation stack (TorchScript) to create serializable and optimizable models from PyTorch code |
| **torch.nn** | A neural networks library deeply integrated with autograd designed for maximum flexibility |
| **torch.multiprocessing** | Python multiprocessing, but with magical memory sharing of torch Tensors across processes |
| **torch.utils** | DataLoader and other utility functions for convenience |

注意职责描述里的几个关键词：`torch.autograd` 强调「支持 torch 中**所有**可微张量运算」，说明它是和 `torch` 张量深度耦合的；`torch.nn` 强调「与 autograd **深度集成**，追求最大灵活性」——这正是 PyTorch「动态图、易调试」哲学的体现。

这 6 个组件在 `torch/` 目录下都能找到对应子目录（见 4.3 节），它们之间的关系是后续每一单元讲义的基础。

#### 4.2.4 代码实践

**实践目标**：把 README 的组件表「落到」实际的 Python 模块上，验证每个组件名都是 `torch` 下真实可导入的子包。

**操作步骤**（示例代码）：

```python
import torch, importlib

# 把 README 组件表里的「组件名」映射到 torch 下的子模块路径
components = {
    "torch":               torch,                 # 包本身
    "torch.autograd":      "torch.autograd",
    "torch.nn":            "torch.nn",
    "torch.jit":           "torch.jit",
    "torch.multiprocessing": "torch.multiprocessing",
    "torch.utils":         "torch.utils",
}
for name, target in components.items():
    mod = target if name == "torch" else importlib.import_module(target)
    # 取每个子包的 docstring 第一行作为「实测职责」
    doc = (mod.__doc__ or "(无 docstring)").strip().splitlines()[0]
    print(f"{name:24s} -> {doc[:70]}")
```

**需要观察的现象**：每个组件名都能成功导入，且其 docstring 与 README 表中的职责描述大致对应（`torch.nn` 通常会提到 "neural network"）。

**预期结果**：6 个组件全部导入成功，无 `ModuleNotFoundError`；打印出的 docstring 首行与 README 描述方向一致。**待本地验证**：不同版本下 docstring 文案可能略有差异。

#### 4.2.5 小练习与答案

**练习 1**：README 组件表里**没有**列 `torch.distributed`，但它真实存在。请说出一个判断它「确实是重要子包」的依据。
> **答案**：在仓库根目录 `torch/` 下存在 `torch/distributed/` 子目录，并且它有独立的 `__init__.py` 与大量源文件（如 `distributed_c10d.py`）；只是 README 的「核心组件表」没有把它列进去，所以读者要结合目录结构（4.3 节）来补全认识。

**练习 2**：`torch.nn` 与 `torch.autograd` 是什么关系？
> **答案**：`torch.nn`（神经网络层）**构建在** `torch.autograd`（自动求导）之上——网络层的参数是「需要梯度的张量」，前向运算被 autograd 自动记录，从而反向传播时能更新参数。README 用 "deeply integrated with autograd" 描述这一点。

---

### 4.3 分层架构：Python 前端 + C++ 后端

#### 4.3.1 概念说明

PyTorch 最容易让初学者迷惑的一点是：明明写的是 Python，但所有「重的数值运算」其实都跑在 C++（甚至 CUDA）里。理解这一点要抓住一个关键事实——

> PyTorch 不是「用 C++ 写的框架的一个 Python 绑定」，而是一个**深度嵌入 Python** 的混合系统。

README 的「Python First」一节特别澄清了这一点：它不是把一个庞大的 C++ 单体框架薄薄地包一层 Python，而是刻意让 Python 成为「一等公民」，让你能用 NumPy、SciPy、Cython 等熟悉的生态来扩展它。

在这个混合系统里，代码大致分三层：

| 层 | 目录 | 语言 | 职责 |
| --- | --- | --- | --- |
| Python 前端 | `torch/`（除 `csrc`/`lib` 外） | Python | 用户 API、模块抽象、训练循环、编译器入口 |
| Python ↔ C++ 绑定 | `torch/csrc/`、`torch/_C/` | C++ / 生成代码 | 把 C++ 对象包装成 Python 可用的对象 |
| C++ 后端 | `aten/`、`c10/`、`torch/csrc/` 的 C++ 部分 | C++/CUDA | 真正的张量实现、算子 kernel、自动求导引擎 |

> 术语解释：**绑定（binding）** 指把 C++ 的类/函数通过工具（PyTorch 主要用 pybind11）暴露给 Python 调用。`torch._C` 就是绑定层产出的 Python 扩展模块。

#### 4.3.2 核心流程

一个典型的「算子调用」分层链路（后续 u2/u3 讲义会逐层拆解，这里只建立直觉）：

```
Python 代码:  torch.add(a, b)
      │
      ▼  （torch/__init__.py 把名字导出）
绑定层:       torch._C._VariableFunctions.add   ← 来自 torch/csrc 的绑定
      │
      ▼
分发器:       Dispatcher 按 DispatchKey 选 kernel   ← c10 / aten/src/ATen
      │
      ▼
C++ 实现:     aten::native::add (CPU) 或 CUDA kernel
```

在这条链路里：
- `torch/` 负责「对用户友好的 Python API」。
- `torch/csrc/` + `torch/_C/` 负责「Python ↔ C++ 的桥」。
- `aten/` 负责「张量算子的现代实现」。
- `c10/` 负责「最底层、跨平台的核心数据结构」（如 `TensorImpl`、`Storage`、`DispatchKey`）。

#### 4.3.3 源码精读

CONTRIBUTING.md 的「Codebase structure」一节是官方对仓库目录最权威的说明。我们先看它对 `c10` 与 `aten` 的定位：

[CONTRIBUTING.md:L208-L224](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CONTRIBUTING.md#L208-L224) —— 这一段说明：
- **`c10/`** 是「核心库」，包含在服务器与移动端都能用的最基础功能；ATen/core 里的东西正在**逐步迁移**到顶层 `c10/`。它只放最关键、对二进制体积敏感的代码。
- **`aten/`** 是「不带 autograd 的 C++ 张量库」，其中 `aten/src/ATen/native/` 是「算子的现代实现」——新写的算子都应放这里；下面还按后端细分为 `cpu/`、`cuda/`、`mps/`、`sparse/`、`mkldnn/`、`cudnn/` 等。

接着看它对 `torch/` 与 `torch/csrc/` 的定位：

[CONTRIBUTING.md:L242-L258](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CONTRIBUTING.md#L242-L258) —— 这一段说明：
- **`torch/`** 是「真正的 PyTorch 库」，其中除 `csrc/` 之外的文件都是 Python 模块（遵循 PyTorch 前端模块结构）。
- **`torch/csrc/`** 是组成 PyTorch 库的 C++ 文件，「既有 Python 绑定代码，也有干重活的 C++ 代码」，并指出 `setup.py` 是 Python 绑定文件的权威清单（惯例上常以 `python_` 为前缀）。
- **`torchgen/`** 被单独点名：它「包含从算子定义（通常写在 `native_functions.yaml`）生成 PyTorch 底层 C++ 与 Python 绑定的逻辑与工具」。

为了让你对这些目录「真实存在」，下表给出本讲实测到的目录结构（节选）：

| 目录 | 实测内容（节选） |
| --- | --- |
| `c10/` | `core/`（`TensorImpl.h`、`StorageImpl.h`、`DispatchKey.h`、`Allocator.h` 等）、`cuda/`、`hip/`、`metal/`、`xpu/`、`mobile/`、`util/` |
| `aten/src/ATen/` | `native/`（算子实现）、`core/`（迁移中的核心）、`Dispatch.cpp`、`Context.cpp`、`ATen.h` 等 |
| `torch/`（Python 前端） | `_C/`、`autograd/`、`nn/`、`optim/`、`cuda/`、`distributed/`、`jit/`、`utils/`、`_dynamo/`、`_inductor/`、`fx/`、`func/`、`export/` 等（共 60+ 子目录） |
| `torch/csrc/` | `Module.cpp`、`autograd/`、`jit/`、`api/`、`distributed/`、`DynamicTypes.cpp` 等 |
| `torchgen/` | `gen.py`、`model.py`、`native_function_generation.py` 等（代码生成器） |
| `caffe2/` | `core/`、`serialize/`、`utils/`、`perfkernels/`（Caffe2 遗留库，历史包袱） |

> 补充：`caffe2/` 是历史上合并进来的另一个框架的代码库，目前主要作为遗留/兼容保留；新功能一般不写在这里。初学者了解「它存在、但不是学习重点」即可。

#### 4.3.4 代码实践

**实践目标**：用只读方式亲手「走」一遍目录，把抽象的分层落到具体文件上。

**操作步骤**：

1. 在仓库根目录执行（只读命令）：

   ```bash
   # 确认三大 C++ 后端目录存在
   ls -d aten/src/ATen/native c10/core torch/csrc

   # 看看 native 下确实按后端分了子目录
   ls aten/src/ATen/native | head

   # 看看 torch 前端有哪些子包
   ls -d torch/*/ | head -40
   ```

2. 用 `git ls-files` 快速数一下各目录的源码规模（建立体量直觉）：

   ```bash
   git ls-files 'aten/src/ATen/native/*.cpp' | wc -l   # CPU/通用算子实现数量
   git ls-files 'c10/core/*.h'          | wc -l   # 核心头文件数量
   ```

**需要观察的现象**：
- `aten/src/ATen/native` 下能看到 `cpu/`、`cuda/`、`mps/` 等后端子目录，印证「按后端组织算子」。
- `torch/` 下的子包名（`autograd`、`nn`、`cuda`、`distributed`、`jit`、`optim`、`utils`…）与 README 组件表 + 本节分层表基本一一对应。

**预期结果**：三条 `ls -d` 全部成功（目录存在）；`torch/` 子目录列表里至少出现 `autograd/`、`nn/`、`cuda/`、`distributed/`、`jit/` 五个。**待本地验证**：具体文件数量随版本变化。

#### 4.3.5 小练习与答案

**练习 1**：`c10/` 和 `aten/` 都是 C++，为什么还要分成两个目录？
> **答案**：定位不同。`c10/` 是「最小、跨平台（含移动端）、对二进制体积敏感」的核心库，只放最基础的数据结构（如 `TensorImpl`、`DispatchKey`）；`aten/` 是「功能完整的 C++ 张量算子库（不含 autograd）」。官方正在把 `aten/src/ATen/core` 里的东西逐步迁到 `c10/`，所以二者边界在缓慢移动。

**练习 2**：用户写的 Python 代码，最终在哪里被翻译成真正执行的 C++ 代码？
> **答案**：经过 `torch/csrc/` 的绑定层（暴露成 `torch._C`），再由 `aten/`/`c10/` 的分发器（Dispatcher）按设备选择具体的 C++/CUDA kernel 执行。这条链路是 u2、u3 讲义的重点。

---

### 4.4 torch 包入口与 import 流程

#### 4.4.1 概念说明

当你写下 `import torch` 时，Python 解释器会执行 `torch/__init__.py`。这个文件是整个框架的「总开关」，它做了很多用户看不见的事：

- **加载 C++ 后端**：通过加载 `.so`/`.dylib` 动态库，把 C++ 的 `libtorch` 引入进程。
- **导入绑定模块 `torch._C`**：这是 C++ 暴露给 Python 的「根对象」，里面装满了真实的算子与类型。
- **组装顶层符号**：把张量类型、函数、子包拼到 `torch` 命名空间下，构成你日常使用的 `torch.Tensor`、`torch.add`、`torch.nn` 等。
- **读取版本号**：从 `torch.torch_version` 读取 `__version__`。

理解这一步的意义在于：很多「PyTorch 为什么能这样用」的疑问，答案都藏在 `import` 流程里（例如「为什么没有 `torch._C` 就报错」「为什么 CUDA 库要在 import 时就加载好」）。

> 术语解释：**动态库预加载（preload）** 指在真正 `import torch._C` 之前，先用 `ctypes.CDLL` 提前把某些共享库装入进程地址空间，确保后续 C++ 库的依赖能被正确找到。这在 wheel 安装、CUDA/ROCm 等场景下尤其重要。

#### 4.4.2 核心流程

`import torch` 时的关键步骤（精简版）：

```
1. 执行 torch/__init__.py 顶部的 import / 工具函数定义
2. 从 torch._utils_internal 读取 USE_GLOBAL_DEPS、USE_RTLD_GLOBAL_WITH_LIBTORCH 等开关
3. 读取版本号 __version__（来自 torch.torch_version）
4. 根据 USE_GLOBAL_DEPS 决定是否调用 _load_global_deps()
     └─ 加载 libtorch_global_deps.so / .dylib（Windows 直接跳过）
5. 执行 from torch._C import *  ← 真正把 C++ 绑定拉进来
6. 后续：组装 __all__、初始化各子包（autograd/nn/cuda/...）
```

其中第 4、5 步是「Python 世界 ↔ C++ 世界」的边界，是本模块的焦点。

#### 4.4.3 源码精读

先看入口文件的顶部——版本与全局依赖开关的导入：

[torch/__init__.py:L59-L66](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L59-L66) —— 这里从 `torch._utils_internal` 导入了 `get_file_path` 以及 `USE_GLOBAL_DEPS`、`USE_RTLD_GLOBAL_WITH_LIBTORCH` 两个布尔开关；并从 `torch.torch_version` 导入 `__version__`。这两个开关决定了第 4 步「加载全局依赖」走哪条分支。

接着看「加载全局依赖」的实现：

[torch/__init__.py:L468-L516](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L469-L516) —— `_load_global_deps()` 的核心是：在非 Windows 平台上，用 `ctypes.CDLL(global_deps_lib_path, mode=ctypes.RTLD_GLOBAL)` 加载 `lib/libtorch_global_deps.{so,dylib}`。它还会针对 ROCm、CUDA wheel 做一些「预加载依赖」的兼容处理（比如提前装载 nvjitlink、nvrtc，避免 wheel 环境下找错版本）。注意函数顶部 `if platform.system() == "Windows": return`——Windows 走另一套加载逻辑。

最后看真正拉起 C++ 绑定的分支：

[torch/__init__.py:L546-L558](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L546-L558) —— 这是 `import torch` 的「边界时刻」。默认分支（else）里：若 `USE_GLOBAL_DEPS` 为真，先调用 `_load_global_deps()`，然后执行 `from torch._C import *`——这一行才真正把 C++ 后端的 `torch._C` 模块及其全部符号注入到 `torch` 命名空间。注释里把另一条 `RTLD_GLOBAL` 分支称为 "the hard way"（用于 fbcode、UBSAN 等特殊环境），把默认分支称为 "Easy way"，并解释：默认方式能避免 libtorch 的 C++ 符号污染其他库、防止莫名其妙的段错误。

至于顶层「导出哪些符号」，由 `__all__` 控制：

[torch/__init__.py:L82-L120](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L82-L120) —— `__all__` 列出了 `torch` 顶层对外公开的名字，开头就能看到 `Tensor`、各种 `*Storage`/`*Tensor`（历史遗留的类型别名）、`GradScaler`、`autocast`、`compile`、`enable_grad`、`inference_mode`、`export` 等。这个列表决定了 `from torch import *` 会拿到什么，也是「torch 顶层 API 表面积」的直观体现。

> 备注：本模块聚焦 `import torch` 的「骨架」。具体某个算子是如何从 `torch._C` 一路分发到 C++ kernel 的，是 u2-l4「算子的 Python 调用路径与 _C 绑定」的主题，本讲不展开。

#### 4.4.4 代码实践

**实践目标**：亲眼看到「C++ 后端确实在 `import torch` 时被加载」，并理解 `USE_GLOBAL_DEPS` 的作用。

**操作步骤**（示例代码 + 只读命令）：

1. 在仓库根目录用 `git grep` 找到 `USE_GLOBAL_DEPS` 的定义来源（只读）：

   ```bash
   git grep -n "USE_GLOBAL_DEPS" -- torch/_utils_internal.py
   ```

2. 写一个最小脚本，确认 `torch._C` 已被加载、版本号已就位（示例代码）：

   ```python
   import torch
   print("version    :", torch.__version__)
   print("torch._C   :", torch._C)            # 应为 <module 'torch._C'>
   print("has Tensor :", hasattr(torch, "Tensor"))
   print("running_with_deploy:", torch._running_with_deploy())  # 恒为 False，见源码 L50-L51
   ```

3. （进阶，可选）在 Linux 上观察 `libtorch_global_deps.so` 是否真的被装入进程：

   ```bash
   python -c "import torch" & PID=$!
   # 进程可能瞬间结束，更稳妥的做法是把上一条脚本写成文件后用 lsof / grep /proc/<pid>/maps 查看
   # 仅作了解：加载成功的标志是进程地址空间里出现 libtorch_global_deps 与 libtorch_cpu
   ```

**需要观察的现象**：
- 第 1 步能看到 `USE_GLOBAL_DEPS` 在 `torch/_utils_internal.py` 里被定义（通常依据构建期变量决定 True/False）。
- 第 2 步 `torch._C` 不为 `None`，说明 C++ 绑定模块已成功加载；`torch.__version__` 与仓库 `version.txt` 里的值（当前为 `2.14.0a0`）方向一致（开发构建可能带本地后缀）。
- `torch._running_with_deploy()` 恒返回 `False`（这是源码里硬编码的，见 `torch/__init__.py` 第 50–51 行）。

**预期结果**：第 2 步打印出非空 `torch._C` 模块对象与 `has Tensor : True`。**待本地验证**：`__version__` 的具体字符串取决于你安装/构建的版本；`/proc/<pid>/maps` 相关观察仅在 Linux 上可行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `from torch._C import *` 这一行被认为是 Python 与 C++ 的「边界」？
> **答案**：因为 `torch._C` 是用 C++ 编写、经 pybind11 绑定编译出的 Python 扩展模块（在 `torch/csrc/` 里实现）。执行 `from torch._C import *` 之前，`torch` 基本只是纯 Python；执行之后，所有 C++ 后端的类与算子才进入 `torch` 命名空间。

**练习 2**：`USE_GLOBAL_DEPS` 和 `USE_RTLD_GLOBAL_WITH_LIBTORCH` 这两个开关分别控制什么？
> **答案**：`USE_GLOBAL_DEPS` 控制「是否在 import 前用 `ctypes.CDLL(RTLD_GLOBAL)` 预加载 `libtorch_global_deps.{so,dylib}`」；`USE_RTLD_GLOBAL_WITH_LIBTORCH` 控制「是否走另一条更激进的、用 `RTLD_GLOBAL` 全局加载 `libtorch` 的分支（"the hard way"）」。默认情况下前者为真、后者为假，走的是「Easy way」分支。

---

## 5. 综合实践

**任务**：亲手做出一张属于你自己的「PyTorch 架构速查表」，把本讲四个模块串起来。

**要求**：

1. **导入验证**：在本机运行 `import torch; print(torch.__version__)`，记录版本号，并与仓库 `version.txt`（当前 `2.14.0a0`）对比。
2. **组件对齐**：打开 README 的组件表（[README.md:L58-L67](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/README.md#L58-L67)），为 `torch`、`torch.autograd`、`torch.nn` 三项各抄下「一句话职责」，并在 `torch/` 目录下找到它们对应的子目录。
3. **分层标注**：在一张表里列出 `torch/`、`aten/`、`c10/`、`torch/csrc/`、`torchgen/`、`caffe2/` 六个目录，分别标注：所属层（Python 前端 / 绑定 / C++ 后端 / 代码生成 / 遗留）、一句话职责（参考 CONTRIBUTING.md 的 [Codebase structure](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CONTRIBUTING.md#L208-L279)）。
4. **import 流程**：在 `torch/__init__.py` 中定位 `_load_global_deps()` 与 `from torch._C import *` 两处代码，用自己的话写一句「它们在 import 时各干了什么」。

**产出**：一张 Markdown 表格 + 一段 100 字以内的「整体架构」小结。

**参考小结示例**（你可以用自己的话改写）：

> PyTorch 是一个深度嵌入 Python 的混合系统：用户层是 `torch/` 下的 Python 子包（`nn`、`autograd`、`cuda`、`distributed` 等）；这些 API 通过 `torch/csrc/` 的绑定桥接到 C++ 后端；后端里 `c10/` 提供最底层的数据结构与分发器，`aten/` 提供海量算子的现代实现；`torchgen/` 则负责从 `native_functions.yaml` 自动生成大量绑定与注册代码。`import torch` 时，`torch/__init__.py` 会预加载全局依赖、拉起 `torch._C`、组装顶层符号，从而把这套 Python+C++ 的体系拼装成我们日常使用的那个 `torch`。

## 6. 本讲小结

- PyTorch 提供**两大**能力：带强 GPU 加速的张量计算，与基于 tape 的自动求导；二者在源码上是相对分离的。
- 顶层组件共 6 个（README 表）：`torch`、`torch.autograd`、`torch.jit`、`torch.nn`、`torch.multiprocessing`、`torch.utils`；`torch.distributed` 虽未列在表中，但是 `torch/` 下真实存在的重要子包。
- 架构是**分层**的：Python 前端（`torch/`）→ 绑定层（`torch/csrc/`、`torch/_C/`）→ C++ 后端（`c10/` 核心数据结构 + `aten/` 算子实现）；`torchgen/` 负责代码生成，`caffe2/` 是遗留库。
- `c10/`（最小核心，含移动端）与 `aten/`（完整张量算子库，不含 autograd）分工不同，且边界正在缓慢迁移（aten/core → c10）。
- `import torch` 的关键两步：`_load_global_deps()` 预加载 `libtorch_global_deps`，随后 `from torch._C import *` 把 C++ 后端注入 Python 命名空间。
- 真正的「算子如何从 Python 分发到 C++」是后续 u2/u3 单元的主题；本讲只建立地图。

## 7. 下一步学习建议

本讲建立的是「地图」。接下来建议：

1. **想真正跑起来** → 读 **u1-l2《从源码构建与运行 PyTorch》**，掌握 `pip install -e . --no-build-isolation` 与 CMake 构建体系。
2. **想摸清仓库导航** → 读 **u1-l3《仓库目录结构与代码组织》**，更系统地逛一遍 `torch/`、`aten/`、`c10/`、`torchgen/`、`torch/csrc/`。
3. **想理解 import 的细节** → 读 **u1-l4《Python 包入口与 torch 导入流程》**，深入 `torch/__init__.py` 与 `torch/csrc/Module.cpp`。
4. 建议随手翻阅的源码：`README.md` 的「More About PyTorch」各小节、`CONTRIBUTING.md` 的「Codebase structure」、以及 `aten/src/ATen/README.md`（如存在）。
