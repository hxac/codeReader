# 环境搭建与 MSDeformAttn CUDA 算子编译

## 1. 本讲目标

上一讲（u1-l1）我们跟着 `MaskFormer.forward` 的 eval 分支走完了语义推理路径，见到了 `retry_if_cuda_oom` 的三段降级策略——其中最后一段是"把张量搬到 CPU 上重算"。但那一切有个隐含前提：**代码已经能在你的机器上跑起来**。本讲就来补上这块地基——Mask2Former 官方仓库里最"劝退"新手的一步：编译 MSDeformAttn 的 CUDA 扩展。

学完本讲，你应该能够：

1. 按 `INSTALL.md` 从零搭好 Mask2Former 的运行环境（PyTorch + detectron2 + `requirements.txt` + 评估库）。
2. 读懂 `ops/setup.py` 的编译决策逻辑：什么条件下编入 CUDA 源码、为什么**没有 GPU 的机器也必须安装 CUDA toolkit**。
3. 会用 `sh make.sh`（有 GPU）和 `TORCH_CUDA_ARCH_LIST='8.0' FORCE_CUDA=1 python setup.py build install`（无 GPU）两条路径完成编译，并解释这两个环境变量的含义。
4. 会运行并读懂 `ops/test.py` 的三组正确性检查，理解"CPU 推理回退"其实发生在 **Python 层的 try/except**，而不是 C++ 层。

## 2. 前置知识

### 2.1 detectron2 插件项目

Mask2Former 仓库本身**不含训练框架**：模型注册、配置系统（CfgNode）、训练循环（Trainer）、数据集加载（DatasetCatalog）、评估器全部来自 [detectron2](https://github.com/facebookresearch/detectron2)。Mask2Former 只是 detectron2 的一个"插件"。因此安装顺序必然是**先 detectron2、后 Mask2Former**。

### 2.2 CUDA 相关的三个概念，初学者最容易混淆

| 概念 | 是什么 | 没有它行不行 |
| --- | --- | --- |
| GPU 驱动（driver） | 操作系统层面让硬件可用的驱动程序，提供 `nvidia-smi` 等 | 编译 CUDA 扩展不强制需要 |
| CUDA toolkit | 编译工具链：`nvcc` 编译器、头文件、库，通常装在 `/usr/local/cuda` | **本项目编译扩展必需**（理由见 4.2） |
| GPU 设备 | 一块真实的显卡 | 无显卡也能**编译** CUDA 代码，只是不能**运行** |

三者可以分离：一台没插显卡的服务器照样可以装 toolkit、用 `nvcc` 编出 CUDA 代码——这正是 `INSTALL.md` 里 "Building on another system" 一节存在的意义。

另外两个术语：

- **计算能力（compute capability）**：NVIDIA GPU 的架构版本号，如 7.0（V100）、8.0（A100）、8.6（RTX 30 系）。编译 CUDA 扩展时需指定目标架构（对应编译选项 `-arch=sm_XX`）。
- **PyTorch C++/CUDA 扩展**：用 C++（必要时 CUDA）实现算子，经 `torch.utils.cpp_extension` 编译成一个能直接 `import` 的 Python 模块。Mask2Former 把多尺度可变形注意力算子做成了这样的扩展，模块名叫 `MultiScaleDeformableAttention`。

### 2.3 为什么 MSDeformAttn 要手写 CUDA 算子（直觉版）

多尺度可变形注意力（Multi-Scale Deformable Attention）的核心公式：

\[
\operatorname{MSDA}(z_q,\, p_q,\, \mathbf{x})
= \sum_{m=1}^{M} W_m \left[ \sum_{l=1}^{L} \sum_{k=1}^{K} A_{mlqk} \cdot \big(W'_m \mathbf{x}_l\big)\big(p_q + \Delta p_{mlqk}\big) \right]
\]

直观解释：普通注意力里每个 query 要和**全部** \(\sum_l H_l W_l\) 个图像 token 做点积；而可变形注意力里，每个 query \(z_q\) 只在 \(L\) 个尺度上各取 \(K\) 个采样点，把预测的偏移 \(\Delta p\) 加到参考点 \(p_q\) 上，在特征图上做**双线性插值**，再用注意力权重 \(A\)（softmax 归一化、和为 1）加权求和。计算量从"全图"降到"每个 query 只碰 \(L \times K\) 个点"。

问题在于：这是**海量的小规模插值 + 加权求和**。用纯 PyTorch 写也行（仓库确实附带了一个参考实现 `ms_deform_attn_core_pytorch`），但训练时几万个 query × 每个十几个采样点的循环开销很大，所以官方提供了 CUDA kernel。本讲只解决"把它编译出来并验证正确"；算子的模型侧用法在单元三（u3-l5 像素解码器）与单元四（u4-l1 算子精读）展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [INSTALL.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md#L1-L48) | 安装总说明：依赖要求、CUDA kernel 编译命令、conda 示例 |
| [requirements.txt](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/requirements.txt#L1-L7) | 7 个 Python 依赖 |
| [ops/setup.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/setup.py#L1-L78) | CUDA 扩展的构建脚本（setuptools + torch cpp_extension），决定编译哪些源码 |
| [ops/make.sh](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/make.sh#L1-L13) | 一键编译入口：`python setup.py build install` |
| [ops/test.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/test.py#L1-L93) | 算子正确性验证：双精度/单精度前向对比 + 数值梯度检查 |
| [ops/functions/ms_deform_attn_func.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L1-L73) | Python 封装：autograd Function、PyTorch 参考实现、未编译时的报错指引 |
| [ops/modules/ms_deform_attn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L1-L126) | `MSDeformAttn` nn.Module 封装，内含 CPU 回退的 try/except |
| [ops/src/](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/src/vision.cpp#L18-L21) | C++/CUDA 源码：`vision.cpp`（pybind11 入口）、`ms_deform_attn.h`（CPU/CUDA 分发）、`cpu/`、`cuda/` 两个实现目录 |

> 下文链接中省略仓库均指 `mask2former/modeling/pixel_decoder/ops/`。

## 4. 核心概念与源码讲解

### 4.1 依赖全景：先装什么、后装什么

#### 4.1.1 概念说明

Mask2Former 的依赖可以画成一个四层金字塔，**自底向上安装**：

```
第 1 层  PyTorch + torchvision          （版本必须互相配对）
第 2 层  detectron2                     （训练框架，源码可编辑安装）
第 3 层  requirements.txt 的 7 个包
        + panopticapi / cityscapesScripts （评估库，pip 从 GitHub 源码装）
第 4 层  MultiScaleDeformableAttention   （CUDA 扩展，本讲主角）
```

一个容易踩的坑：`requirements.txt` 里**没有** detectron2，也没有 PyTorch。它们必须按官方指引针对本机 CUDA/PyTorch 版本单独安装——因为不同环境的正确安装方式不同，无法用一行 `pip install` 表达。

#### 4.1.2 核心流程

`INSTALL.md` 给出的示例安装流程（可当作 checklist）：

```text
1. conda create --name mask2former python=3.8
2. conda install pytorch==1.9.0 torchvision==0.10.0 cudatoolkit=11.1   # 版本配对
3. pip install -U opencv-python                                        # demo 可视化需要
4. git clone detectron2 && pip install -e .                            # 可编辑安装
5. pip install git+.../panopticapi.git                                 # PQ 评估
6. pip install git+.../cityscapesScripts.git                           # Cityscapes 评估
7. git clone Mask2Former && pip install -r requirements.txt
8. cd mask2former/modeling/pixel_decoder/ops && sh make.sh             # 编译 CUDA 扩展
```

要点：detectron2 用 `pip install -e .` 是**可编辑安装**——以后改 detectron2 源码立即生效，便于调试框架层行为。

#### 4.1.3 源码精读

**依赖要求**：[INSTALL.md:L3-L10](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md#L3-L10) 逐条列出：Linux/macOS、Python ≥ 3.6、PyTorch ≥ 1.9 且 torchvision 与之匹配（并提醒注意 detectron2 对 PyTorch 版本的要求）、OpenCV 可选但 demo 与可视化需要、最后 `pip install -r requirements.txt`。

**conda 完整示例**：[INSTALL.md:L29-L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md#L29-L48) 就是 4.1.2 流程图的原始出处。示例里的 `pytorch==1.9.0 torchvision==0.10.0` 是 2021 年的配对组合，如今可以按 detectron2 官方安装页选择更新的版本——**关键是保持 PyTorch/torchvision/本机驱动三者匹配**，而不是照抄版本号。

**requirements.txt 的 7 个包**：[requirements.txt:L1-L7](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/requirements.txt#L1-L7) 列出 `cython, scipy, shapely, timm, h5py, submitit, scikit-image`。用 Grep 检索整个仓库后，它们在本仓库源码中的**直接使用证据**如下（这一点很重要：网上很多解读会想当然地编造用途）：

| 包 | 在仓库源码中的直接使用 |
| --- | --- |
| scipy | 有：[mask2former/modeling/matcher.py:L8](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L8) `from scipy.optimize import linear_sum_assignment`，训练时匈牙利匹配用（单元五精读） |
| shapely | 仅 [train_net.py:L9](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L9) 过滤其弃用告警；主要作为评估库的传递依赖存在 |
| cython / timm / h5py / submitit / scikit-image | 无直接 import（已用 Grep 验证）。属于官方安装清单要求的间接/工具链依赖，照装即可 |

#### 4.1.4 代码实践

**实践：安装后先做一次"环境体检"再继续。**

1. 实践目标：确认四件套（Python/PyTorch/torchvision/detectron2）版本互相匹配，且能定位 CUDA toolkit。
2. 操作步骤：完成 4.1.2 的第 1~7 步后，在仓库根目录运行下面的体检脚本（保存为 `check_env.py`）。

   ```python
   # 示例代码：环境体检脚本（不涉及 CUDA 扩展，扩展体检见本讲综合实践）
   import sys

   print("== Python ==")
   print(sys.version)

   import torch, torchvision
   print("== PyTorch ==")
   print("torch:", torch.__version__, "| torchvision:", torchvision.__version__)
   print("cuda available:", torch.cuda.is_available())
   from torch.utils.cpp_extension import CUDA_HOME
   print("CUDA_HOME:", CUDA_HOME)

   import detectron2
   print("== detectron2 ==")
   print("detectron2:", detectron2.__version__)
   ```

3. 需要观察的现象：`CUDA_HOME` 是否为非 `None` 的路径（如 `/usr/local/cuda`）；`torch` 与 `torchvision` 版本是否为官方配对。
4. 预期结果：各项正常打印、无 ImportError。若 `CUDA_HOME` 为 `None`，说明没装 CUDA toolkit 或没设环境变量——这会直接导致 4.2/4.3 的编译失败。具体输出数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `requirements.txt` 里不写 detectron2 和 PyTorch？
**答案**：detectron2 与 PyTorch 的正确安装方式取决于本机 CUDA 驱动、操作系统和版本组合（detectron2 官方要求源码/特定轮子安装，PyTorch 要与 torchvision 配对），无法用一行固定的 `pip install` 表达，所以 `INSTALL.md` 用文字指引单独说明。

**练习 2**：`requirements.txt` 里的 scipy 在仓库里被谁使用、做什么？
**答案**：`mask2former/modeling/matcher.py:L8` 导入 `linear_sum_assignment`，用于训练时把预测 query 与真值做一对一匈牙利匹配（详见单元五）。

**练习 3**：只装了 PyTorch 没装 OpenCV，会影响什么？
**答案**：`INSTALL.md:L9` 写明 OpenCV 是可选的，但 demo 与可视化（`demo/demo.py` 一族）需要它；不影响模型构建与训练本身。

---

### 4.2 构建系统：ops/setup.py 如何决定编译什么

#### 4.2.1 概念说明

`ops/setup.py` 是一个标准的 setuptools 构建脚本，配合 `torch.utils.cpp_extension` 把 C++/CUDA 源码编成名为 `MultiScaleDeformableAttention` 的 Python 扩展模块。

本模块最重要的一个结论（很多教程讲错了）：**这个 setup.py 里 `CppExtension` 分支是永远走不到的"死代码"**。虽然代码先把 `extension` 默认设为 `CppExtension`，但后面的 `else` 分支无条件 `raise`。也就是说：

- 无论机器有没有 GPU，**任何一次成功构建都使用 `CUDAExtension`**（都会编入 `.cu` CUDA 源码）；
- 因此 **CUDA toolkit（`CUDA_HOME`）在任何情况下都必须存在**——哪怕你只想在 CPU 上推理。

#### 4.2.2 核心流程

`get_extensions()` 的决策逻辑可以写成这样一棵决策树：

```text
收集源码：
    sources = src/*.cpp（pybind 入口） + src/cpu/*.cpp（CPU 实现，无条件编入）

判断：
    if (环境变量 FORCE_CUDA 已设置  或  torch.cuda.is_available())
       且 CUDA_HOME 不为 None:
            使用 CUDAExtension
            sources += src/cuda/*.cu
            定义宏 WITH_CUDA
            给 nvcc 追加 4 个 -D 编译选项（屏蔽 half 运算符）
    else:
            raise NotImplementedError      # ← 注意：必然失败，没有"纯 CPU 构建"这条路
```

#### 4.2.3 源码精读

**导入与全局**：[ops/setup.py:L17-L24](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/setup.py#L17-L24) 从 `torch.utils.cpp_extension` 导入三个关键名字：`CUDA_HOME`（toolkit 路径，自动探测也可由环境变量指定）、`CppExtension`（纯 C++ 扩展）、`CUDAExtension`（含 CUDA 源码的扩展）。

**源码收集**：[ops/setup.py:L26-L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/setup.py#L26-L37) 用 glob 收集三类源码：`src/*.cpp` 是 pybind11 入口 `vision.cpp`；`src/cpu/*.cpp` 是 CPU 参考实现（**无条件**加入 `sources`，即使走 CUDA 构建）；`src/cuda/*.cu` 是 CUDA kernel，只在 CUDA 分支加入。默认 `extension = CppExtension`。

**核心分支**：[ops/setup.py:L40-L54](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/setup.py#L40-L54)。

- L40 的条件 `(os.environ.get('FORCE_CUDA') or torch.cuda.is_available()) and CUDA_HOME is not None`——注释原话是 "Force cuda since torch ask for a device, not if cuda is in fact available"：构建逻辑不能只看"当前有没有显卡"，还要允许 `FORCE_CUDA` 强制编 CUDA。
- L43 的 `define_macros += [("WITH_CUDA", None)]` 定义 `WITH_CUDA` 宏，它的消费点在 C++ 分发层（见 4.4.3 的 `src/ms_deform_attn.h`）。
- L44-L49 给 nvcc 的四个 `-D__CUDA_NO_HALF*__` 选项是 PyTorch CUDA 扩展的惯用手法：屏蔽 CUDA 自带 `half` 类型的运算符重载与隐式转换，避免与 PyTorch 头文件里的半精度定义冲突。
- L50-L54 的 `else` 分支区分两种失败文案：`CUDA_HOME is None`（没装 toolkit / 没设变量）与"没检测到 CUDA runtime"（提示设 `FORCE_CUDA=1`）。**两条路都以 `raise` 告终**——所以 4.2.1 说"CppExtension 是死代码"。

**扩展构造与安装入口**：[ops/setup.py:L56-L78](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/setup.py#L56-L78) 用收集到的源码构造名为 `"MultiScaleDeformableAttention"` 的扩展（L60），`setup()` 里 `cmdclass={"build_ext": torch.utils.cpp_extension.BuildExtension}`（L77）把编译交给 PyTorch 的构建器，由它调用 `nvcc`/g++ 并处理与当前 PyTorch 的 ABI 兼容。

**WITH_CUDA 宏的消费点**：[ops/src/ms_deform_attn.h:L20-L22](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/src/ms_deform_attn.h#L20-L22) 只有定义了 `WITH_CUDA` 才 `#include` CUDA 头文件；[L25-L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/src/ms_deform_attn.h#L25-L44) 的 `ms_deform_attn_forward` 在运行时按"张量是否在 CUDA 上"分发到 CUDA 实现，否则抛错（详见 4.4）。

#### 4.2.4 代码实践

**实践：不真正编译，先"dry-run"一遍决策逻辑。**

1. 实践目标：在动手编译前，确认你的环境会走哪个分支，提前发现缺什么。
2. 操作步骤：在任意目录运行（示例代码）：

   ```python
   import os, torch
   from torch.utils.cpp_extension import CUDA_HOME

   force_cuda = bool(os.environ.get("FORCE_CUDA"))
   will_build_cuda = (force_cuda or torch.cuda.is_available()) and CUDA_HOME is not None
   print(f"FORCE_CUDA={force_cuda}, cuda_available={torch.cuda.is_available()}, "
         f"CUDA_HOME={CUDA_HOME}, 将编译CUDA版={will_build_cuda}")
   ```

   再对照四种典型组合，预测 `get_extensions()` 的行为：

   | 场景 | FORCE_CUDA | torch.cuda.is_available() | CUDA_HOME | 结果 |
   | --- | --- | --- | --- | --- |
   | 有 GPU 的训练机 | 未设 | True | 有路径 | 走 CUDAExtension ✅ |
   | 无 GPU、装了 toolkit、设了 FORCE_CUDA | `1` | False | 有路径 | 走 CUDAExtension ✅ |
   | 无 GPU、装了 toolkit、未设 FORCE_CUDA | 未设 | False | 有路径 | `raise`：提示设 `FORCE_CUDA=1` ❌ |
   | 没装 toolkit | 任意 | 任意 | `None` | `raise`：`CUDA_HOME is None` ❌ |

3. 需要观察的现象：你所在机器落在表中哪一行。
4. 预期结果：只有前两行能成功构建。脚本输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一台没有 GPU、也没装 CUDA toolkit 的机器，直接 `sh make.sh` 会发生什么？
**答案**：`get_extensions()` 走 `else` 分支，因为 `CUDA_HOME is None` 抛出 `NotImplementedError('CUDA_HOME is None. Please set environment variable CUDA_HOME.')`（setup.py:L51-L52）。先装 CUDA toolkit 才能继续。

**练习 2**：`WITH_CUDA` 宏在哪里定义、在哪里被消费？
**答案**：定义在 [ops/setup.py:L43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/setup.py#L43)（`define_macros`）；消费在 [ops/src/ms_deform_attn.h:L20-L22](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/src/ms_deform_attn.h#L20-L22)（条件包含 CUDA 头）与 L36-L41（条件调用 CUDA 实现）。

**练习 3**：`src/cpu/*.cpp` 在 CUDA 构建里会不会被编译？"被编译"等于"会被调用"吗？
**答案**：会——[ops/setup.py:L34](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/setup.py#L34) 无条件把 CPU 源码并入 `sources`。但"被编译"≠"被调用"：C++ 分发层从不路由到它（见 4.4.3 的分析）。

---

### 4.3 一键编译：make.sh 与无 GPU 构建

#### 4.3.1 概念说明

[ops/make.sh](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/make.sh#L13) 全部有效内容只有一行：`python setup.py build install`。它做的事：setuptools 按 4.2 的逻辑收集源码 → `nvcc`/g++ 编译 → 链接为 `.so` → `install` 把扩展装进**当前 Python 环境**的 site-packages。装好之后，在任何目录都能 `import MultiScaleDeformableAttention`。

无 GPU 机器构建需要理解两个环境变量：

- **`FORCE_CUDA=1`**：跳过 `torch.cuda.is_available()` 检查，强制按 CUDAExtension 构建（setup.py:L40 消费它）。
- **`TORCH_CUDA_ARCH_LIST`**：目标 GPU 架构列表，如 `'8.0'` 对应 A100（sm_80）。有 GPU 时 torch 会自动读取当前显卡架构；**无 GPU 时读不到**，必须显式指定，否则 torch 会退回到很长的默认架构列表，编译时间成倍增加甚至因工具链过旧而失败。需要支持多种卡时可用分号分隔，如 `'7.0;8.0'`（这是 `torch.utils.cpp_extension` 的通用约定）。

#### 4.3.2 核心流程

两条构建路径：

```text
路径 A（有 GPU，最常见）:
    cd mask2former/modeling/pixel_decoder/ops
    sh make.sh

路径 B（无 GPU，但有 CUDA toolkit）:
    cd mask2former/modeling/pixel_decoder/ops
    TORCH_CUDA_ARCH_LIST='8.0' FORCE_CUDA=1 python setup.py build install
```

前提（两条路径相同）：`CUDA_HOME` 指向 CUDA toolkit（[INSTALL.md:L15](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md#L15) 明确强调）。

#### 4.3.3 源码精读

**编译命令出处**：[INSTALL.md:L12-L20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md#L12-L20)——"After preparing the required environment, run the following command to compile CUDA kernel for MSDeformAttn"，并强调 `CUDA_HOME` 必须定义并指向 CUDA toolkit 目录。

**无 GPU 构建出处**：[INSTALL.md:L22-L26](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md#L22-L26)——"To build on a system that does not have a GPU device but provide the drivers"，给出 `TORCH_CUDA_ARCH_LIST='8.0' FORCE_CUDA=1 python setup.py build install`。

**make.sh 本体**：[ops/make.sh:L13](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/make.sh#L13) 就是 `python setup.py build install` 一行（前 12 行是版权注释）。

经验提示：重复编译前可先 `python setup.py clean` 或删除 `ops/build/` 目录，避免旧产物缓存干扰；`nvcc` 对本机 gcc 版本有兼容上限，报错时优先怀疑 gcc 过新（这是 CUDA 工具链的常见坑，与本项目逻辑无关）。

#### 4.3.4 代码实践

**实践：编译并验证扩展可导入。**

1. 实践目标：完成 MSDeformAttn 扩展编译，确认它进入了当前 Python 环境。
2. 操作步骤：
   1. 先跑 4.2.4 的 dry-run 脚本确认会走 CUDA 分支；
   2. 有 GPU：`cd mask2former/modeling/pixel_decoder/ops && sh make.sh`；无 GPU：改用路径 B 的命令（把 `'8.0'` 换成你目标显卡的架构）；
   3. 编译成功后回到**任意目录**执行：

      ```bash
      python -c "import MultiScaleDeformableAttention as MSDA; print(MSDA.__file__)"
      ```

3. 需要观察的现象：编译日志中 `nvcc` 是否被调用、是否出现 `src/cuda/ms_deform_attn_cuda.cu`；最后一条命令的输出。
4. 预期结果：`import` 不报错，打印出一个 `.so` 文件路径（位于当前环境的 site-packages 下）。编译时长与机器相关，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`sh make.sh` 等价于哪条命令？
**答案**：`python setup.py build install`（make.sh:L13），即先构建再安装到当前 Python 环境。

**练习 2**：`TORCH_CUDA_ARCH_LIST='8.0'` 中的 8.0 指什么？不设会怎样？
**答案**：指目标 GPU 的计算能力 8.0（A100，sm_80）。无 GPU 时 torch 无法从设备读取架构，会退回很长的默认架构列表逐个编译，耗时大增甚至失败；显式指定最稳。

**练习 3**：为什么编译完还需要 `install`，而不只是 `build`？
**答案**：`build` 只在本目录产出 `.so`；`install` 才把它放进 Python 的 site-packages，让任何目录下的 `import MultiScaleDeformableAttention` 都能找到——这正是 4.4 中 `import mask2former` 不报错的前提。

---

### 4.4 验证与回退：ops/test.py 与 CPU 推理路径

#### 4.4.1 概念说明

这个算子同时存在**两份实现**：

1. **CUDA 实现**：编译出的 `MSDA.ms_deform_attn_forward/backward`，训练/推理的性能担当；
2. **PyTorch 参考实现**：`ms_deform_attn_core_pytorch`（纯 PyTorch，基于 `F.grid_sample`），源码注释自述 "for debug and test only, need to use cuda version instead"。

`test.py` 的思路就是**用参考实现当标准答案，交叉验证 CUDA kernel**。

而"CPU 推理"能不能跑，取决于两道关卡，它们都在 Python 侧：

- **import 守卫**：扩展没编译时，`functions/ms_deform_attn_func.py` 在 import 阶段就抛出带修复指引的 `ModuleNotFoundError`。这意味着**纯 CPU 用户也必须先编译这个扩展**，否则连模型都构建不起来。
- **运行时回退**：扩展编译好后，喂给它 CPU 张量，C++ 分发层会直接抛 `Not implemented on the CPU`，而这句异常会被 `modules/ms_deform_attn.py` 里的裸 `try/except` 捕获，随即改用 PyTorch 参考实现。git 历史可以佐证：commit `16c3bee`（"support inference on CPU"）**只改了这一个文件、+6/-2 行**（可用 `git show --stat 16c3bee` 自行验证）。

一个反直觉的事实：`src/cpu/` 里其实躺着完整的 CPU 前向/反向 C++ 实现（`ms_deform_attn_cpu_forward`），而且无论走哪个构建分支它都会被编译进扩展（4.2 练习 3）；但 C++ 分发层**从不调用它**——真正在 CPU 上跑可变形注意力的是那份"仅供调试"的 Python 参考实现。（用 Grep 检索 `ms_deform_attn_cpu_forward` 可以确认它只有声明与定义、没有任何调用点。）

#### 4.4.2 核心流程

`test.py` 的三组检查：

```text
① check_forward_equal_with_pytorch_double   双精度前向：CUDA 输出 vs 参考实现，torch.allclose 默认容差
② check_forward_equal_with_pytorch_float    单精度前向：容差放宽到 rtol=1e-2, atol=1e-3
③ check_gradient_numerical                  数值梯度：gradcheck 验证 CUDA backward，
                                            通道数遍历 [30,32,64,71,1025,2048,3096]
```

CPU 张量在算子内部的完整路径：

```text
MSDeformAttn.forward（modules/ms_deform_attn.py）
  └─ try: MSDeformAttnFunction.apply(...)
           └─ MSDA.ms_deform_attn_forward（C++，vision.cpp pybind 入口）
                └─ value 在 CUDA 上？
                     ├─ 是 → cuda kernel（ms_deform_attn_cuda.cu）
                     └─ 否 → AT_ERROR("Not implemented on the CPU")   ← 抛异常
     except:  ms_deform_attn_core_pytorch(...)                        ← CPU 推理真正走的地方
  └─ output_proj 后返回
```

#### 4.4.3 源码精读

**import 守卫**：[ops/functions/ms_deform_attn_func.py:L21-L29](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L21-L29) `try: import MultiScaleDeformableAttention as MSDA`，失败时重新抛出的 `ModuleNotFoundError` 附带两行修复命令（`cd ops` + `sh make.sh`）。这就是"没编译扩展时连 `import mask2former` 都会失败"的报错源头，也是新手最常见的第一条报错。

**autograd 封装**：[ops/functions/ms_deform_attn_func.py:L32-L49](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L32-L49) `MSDeformAttnFunction` 把 `MSDA.ms_deform_attn_forward/backward` 包成 PyTorch 的 `autograd.Function`，forward 里用 `ctx.save_for_backward` 存下五个输入供反向使用。

**PyTorch 参考实现**：[ops/functions/ms_deform_attn_func.py:L52-L72](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L52-L72)。两个关键点：L58 `sampling_grids = 2 * sampling_locations - 1` 把 \([0,1]\) 的归一化坐标转到 `grid_sample` 需要的 \([-1,1]\)；L66-L67 用 `F.grid_sample(bilinear, zeros, align_corners=False)` 做批量双线性插值，最后按注意力权重加权求和。

**运行时 CPU 回退**：[ops/modules/ms_deform_attn.py:L116-L123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L116-L123)——`try` 里调用 CUDA `Function`，`except` 落到 `ms_deform_attn_core_pytorch`，注释 `# CPU`；L123 还留了一行被注释掉的"永远用参考实现"的写法，注释标明 `# For FLOPs calculation only`（计算量统计时需要走可被追踪的 PyTorch 路径，单元十的 `analyze_model.py` 会用到这一思路）。

**C++ 分发层**：[ops/src/ms_deform_attn.h:L25-L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/src/ms_deform_attn.h#L25-L44) `ms_deform_attn_forward` 只认 CUDA 张量：`value.type().is_cuda()` 为真且编译时有 `WITH_CUDA` 才进 CUDA 实现，否则分别抛 `"Not compiled with GPU support"` 或 `"Not implemented on the CPU"`；backward（L46-L66）同理。pybind 入口在 [ops/src/vision.cpp:L18-L21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/src/vision.cpp#L18-L21)，只导出 forward/backward 两个函数。

**test.py 玩具数据**：[ops/test.py:L24-L28](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/test.py#L24-L28) 设定最小规模：批 N=1、头 M=2、通道 D=2、query 数 Lq=2、尺度数 L=2、每尺度采样点 P=2；两个尺度的形状 `[(6,4),(3,2)]`，于是 token 总数 \(S = 6\times4 + 3\times2 = 30\)，`level_start_index` 由 `cumsum` 算出为 `[0, 24]`。**注意 L26 的 `shapes` 直接 `.cuda()`——整份 test.py 都硬编码在 GPU 上运行**，这决定了 4.4.4 无 GPU 分支只能做替代验证。

**检查①②（前向）**：[ops/test.py:L34-L47](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/test.py#L34-L47)（double）与 [L50-L63](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/test.py#L50-L63)（float）。随机生成的注意力权重先归一化成和为 1（L39，与公式中 \(A\) 的 softmax 语义一致），`im2col_step = 2` 是 CUDA kernel 的批处理分块参数（模块正式使用时默认 128，见 [ops/modules/ms_deform_attn.py:L52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L52)；测试里 batch 太小所以取 2）。double 检查用 `torch.allclose` 默认容差（rtol=1e-5, atol=1e-8），float 检查放宽到 `rtol=1e-2, atol=1e-3`——单精度下两种实现的浮点累加顺序不同，误差天然更大。

**检查③（梯度）**：[ops/test.py:L66-L81](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/test.py#L66-L81) 用 `torch.autograd.gradcheck`（双精度数值差分）验证 CUDA backward 对 `value`、`sampling_locations`、`attention_weights` 三个输入的梯度；[L88](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/test.py#L88) 的通道数列表 `[30, 32, 64, 71, 1025, 2048, 3096]` 有意覆盖 2 的幂/非 2 的幂/超大通道数，确保 kernel 对任意 `d_model` 都正确。主入口在 [L84-L89](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/test.py#L84-L89)。

#### 4.4.4 代码实践

**实践 A（有 GPU）：跑通 test.py 三组检查。**

1. 实践目标：验证编译出的 CUDA kernel 与 PyTorch 参考实现结果一致。
2. 操作步骤：

   ```bash
   cd mask2former/modeling/pixel_decoder/ops
   python test.py
   ```

3. 需要观察的现象：三组检查各打印一行，行首是 `True/False`，后跟误差统计。
4. 预期结果：输出格式如下（共 9 行：2 行前向 + 7 行梯度；具体数值以本地为准，**待本地验证**）：

   ```text
   * True check_forward_equal_with_pytorch_double: max_abs_err 0.00e+00 max_rel_err 0.00e+00
   * True check_forward_equal_with_pytorch_float: max_abs_err x.xx e-0x max_rel_err x.xx e-0x
   * True check_gradient_numerical(D=30)
   * True check_gradient_numerical(D=32)
   ...
   ```

**实践 B（无 GPU）：阅读 + CPU 替代验证。**

`test.py` 全程 `.cuda()`，无 GPU 跑不了；按本讲规格，无 GPU 时改为"阅读 test.py 写出验证步骤说明"，并用下面的 CPU 版对照实验补上验证环节：用**三重循环 + 手写双线性插值**朴素复现 MSDA 前向，与参考实现 `ms_deform_attn_core_pytorch` 对比（示例代码，可直接运行；前提是扩展已按 4.3 路径 B 编译，否则 import 守卫会拦截——那也可以把参考实现函数体直接复制进脚本绕开 import）：

```python
# 示例代码：CPU 上朴素复现 MSDA 前向，对照 ms_deform_attn_core_pytorch
import math
import torch
# 前提：扩展已编译（见 4.3），否则下面这行会抛出带修复指引的 ModuleNotFoundError
from mask2former.modeling.pixel_decoder.ops.functions.ms_deform_attn_func import (
    ms_deform_attn_core_pytorch,
)

def msda_naive(value, value_spatial_shapes, sampling_locations, attention_weights):
    """三重循环版 MSDA 前向：仅用于理解算子语义，切勿用于训练。"""
    N, S, M, D = value.shape
    _, Lq, _, L, P, _ = sampling_locations.shape
    value_list = value.split([int(H * W) for H, W in value_spatial_shapes], dim=1)
    out = torch.zeros(N, Lq, M, D, dtype=value.dtype)
    for n in range(N):
        for q in range(Lq):
            for m in range(M):
                acc = torch.zeros(D, dtype=value.dtype)
                for l, (H, W) in enumerate(value_spatial_shapes):
                    H, W = int(H), int(W)
                    v = value_list[l][n, :, m, :].reshape(H, W, D)  # (H, W, D)
                    for p in range(P):
                        u, vv = sampling_locations[n, q, m, l, p]   # (x, y) ∈ [0,1]
                        # 等价于 grid_sample(align_corners=False) 的像素坐标映射
                        x = float(u) * W - 0.5
                        y = float(vv) * H - 0.5
                        x0, y0 = math.floor(x), math.floor(y)
                        wx, wy = x - x0, y - y0
                        def at(yy, xx):  # 越界取 0，对应 padding_mode='zeros'
                            return v[yy, xx] if (0 <= yy < H and 0 <= xx < W) \
                                else torch.zeros(D, dtype=value.dtype)
                        sampled = (at(y0, x0) * (1 - wx) * (1 - wy)
                                 + at(y0, x0 + 1) * wx * (1 - wy)
                                 + at(y0 + 1, x0) * (1 - wx) * wy
                                 + at(y0 + 1, x0 + 1) * wx * wy)
                        acc = acc + attention_weights[n, q, m, l, p] * sampled
                out[n, q, m] = acc
    return out

torch.manual_seed(3)
N, M, D, Lq, L, P = 1, 2, 2, 2, 2, 2                      # 与 test.py:L24-L25 相同
shapes = torch.as_tensor([(6, 4), (3, 2)], dtype=torch.long)
S = sum(int(H * W) for H, W in shapes)
value = torch.rand(N, S, M, D).double() * 0.01
# 采样点限制在 [0.2, 0.8]，避开 grid_sample 的 padding 边界行为差异
loc = torch.rand(N, Lq, M, L, P, 2).double() * 0.6 + 0.2
w = torch.rand(N, Lq, M, L, P).double() + 1e-5
w = w / w.sum(-1, keepdim=True).sum(-2, keepdim=True)     # 与 test.py:L39 相同的归一化

ref = ms_deform_attn_core_pytorch(value, shapes, loc, w)
naive = msda_naive(value, shapes, loc, w)
print("shape:", tuple(ref.shape))                          # 预期形状见下方观察点
print("max_abs_err:", (ref - naive).abs().max().item())
print("allclose:", torch.allclose(ref, naive, atol=1e-10))
```

观察点与预期结果：输出形状应为 \((N, Lq, M \times D) = (1, 2, 4)\)（`ms_deform_attn_core_pytorch` 最后把头维度折进通道维返回，见其 L71-L72 的 `view(N_, M_*D_, Lq_)`）；双精度下 `max_abs_err` 应在 1e-15 量级、`allclose` 为 `True`。**待本地验证**。

这个练习同时是一次"验证步骤说明"的演练：它复刻了 test.py 检查①的构造方式（同样的形状常量、同样的权重归一化），只是把"CUDA vs 参考实现"换成了"朴素循环 vs 参考实现"——这正是无 GPU 环境下能做的等价交叉验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 double 检查用 `allclose` 默认容差，float 检查却放宽到 `rtol=1e-2, atol=1e-3`？
**答案**：单精度（float32）下，CUDA kernel 与 PyTorch 参考实现的浮点累加顺序不同，舍入误差天然偏大，必须放宽容差；双精度下舍入误差可忽略，可以用严格容差做"逐位接近"的对比。

**练习 2**：假设你忘了编译扩展，直接 `import mask2former` 再构建模型，会发生什么？报错信息长什么样？
**答案**：`ops/functions/ms_deform_attn_func.py:L21-L29` 的 import 守卫抛出 `ModuleNotFoundError`，信息里直接给出两行修复命令：`cd mask2former/modeling/pixel_decoder/ops` 和 `sh make.sh`。

**练习 3**：CPU 上推理时，真正执行可变形注意力计算的是哪段代码？
**答案**：Python 参考实现 `ms_deform_attn_core_pytorch`——CPU 张量进入 C++ 层会抛 `Not implemented on the CPU`（ms_deform_attn.h:L43），被 [ops/modules/ms_deform_attn.py:L116-L121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L116-L121) 的 `try/except` 捕获后回退。C++ 的 `src/cpu/` 实现虽然被编译但从不被分发层调用。

**练习 4**：`check_gradient_numerical` 验证的是什么？为什么通道数要取 30、71、1025 这类奇怪值？
**答案**：用 `gradcheck` 的双精度数值差分验证 CUDA **backward** 对三个输入的梯度正确性；通道数覆盖 2 的幂与非 2 的幂、小与超大（2048、3096），确保 kernel 对任意 `d_model` 都正确，而不是只对齐了好走的特例。

---

## 5. 综合实践

**综合任务：制作你自己的《Mask2Former 环境体检报告》。** 把 4.1~4.4 串成一条流水线，产出一个脚本能对任何新机器一键判定"环境是否就绪"。

1. 实践目标：把依赖安装（4.1）、编译决策（4.2）、扩展安装（4.3）、CPU 回退（4.4）四步全部跑通并自动化检测。
2. 操作步骤：
   1. 按 4.1 完成依赖安装（检测点：四件套版本 + `CUDA_HOME`）；
   2. 按 4.2.4 dry-run 判定构建分支，再按 4.3 编译（检测点：`import MultiScaleDeformableAttention` 成功）；
   3. 有 GPU 则运行 `ops/test.py` 记录 9 行输出；无 GPU 则完成 4.4 实践 B 的对照实验；
   4. 最后运行下面的一体化体检脚本（示例代码，在仓库根目录保存为 `check_env.py`）：

   ```python
   # 示例代码：Mask2Former 环境体检脚本（在仓库根目录运行）
   import sys

   def main():
       print("== Python ==");  print(sys.version)

       import torch, torchvision
       from torch.utils.cpp_extension import CUDA_HOME
       print("== PyTorch ==")
       print("torch:", torch.__version__, "| torchvision:", torchvision.__version__)
       print("cuda available:", torch.cuda.is_available(), "| CUDA_HOME:", CUDA_HOME)

       import detectron2
       print("== detectron2 ==", detectron2.__version__)

       print("== MSDeformAttn 扩展 ==")
       import MultiScaleDeformableAttention as MSDA
       print("extension:", MSDA.__file__)

       print("== CPU 回退路径 ==")
       from mask2former.modeling.pixel_decoder.ops.modules.ms_deform_attn import MSDeformAttn
       m = MSDeformAttn(d_model=32, n_levels=2, n_heads=4, n_points=2).eval()
       shapes = torch.as_tensor([[6, 4], [3, 2]])
       lsi = torch.cat([torch.zeros(1, dtype=torch.long), shapes.prod(1).cumsum(0)[:-1]])
       S = int(shapes.prod(1).sum())
       out = m(torch.rand(1, 5, 32),        # query (N, Len_q, C)
               torch.rand(1, 5, 2, 2),       # reference_points (N, Len_q, n_levels, 2)
               torch.rand(1, S, 32),         # input_flatten (N, sum(H*W), C)
               shapes, lsi)
       print("CPU forward output shape:", tuple(out.shape))

   if __name__ == "__main__":
       main()
   ```

3. 需要观察的现象：最后一节在**不使用任何 GPU** 的情况下，`MSDeformAttn` 模块前向没有报错——因为它内部走了 `try/except` 回退到参考实现。
4. 预期结果：`extension:` 指向 site-packages 里的 `.so`；`CPU forward output shape: (1, 5, 32)`。任何一步失败，都能对照本讲对应小节定位原因（import 失败→4.3 未编译；`CUDA_HOME: None`→4.2 场景 4；等等）。全部输出**待本地验证**。

## 6. 本讲小结

- Mask2Former 是 detectron2 的插件项目，依赖呈四层金字塔：PyTorch → detectron2 → requirements.txt 7 个包 + panopticapi/cityscapesScripts → CUDA 扩展；requirements.txt 里的包只有 scipy（匈牙利匹配）等少数几个能在仓库源码里找到直接 import。
- `ops/setup.py` 的 `else` 分支必然 `raise`，所以**任何成功构建都走 CUDAExtension**——没有 GPU 的机器也必须安装 CUDA toolkit 并设 `FORCE_CUDA=1`，同时用 `TORCH_CUDA_ARCH_LIST` 指定目标架构。
- `make.sh` 只有一行 `python setup.py build install`；`install` 把扩展装进 site-packages，是任何目录都能 `import MultiScaleDeformableAttention` 的关键。
- `ops/test.py` 用 PyTorch 参考实现当标准答案做三组检查（double 前向严格容差、float 前向放宽容差、gradcheck 数值梯度×7 种通道数）。
- "CPU 推理支持"是 Python 层的裸 `try/except` 回退（commit `16c3bee` 仅 +6/-2 行）；C++ 的 `src/cpu/` 实现被编译但从不被分发层调用，import 守卫决定了**纯 CPU 用户也必须先编译扩展**。
- 上一讲 `retry_if_cuda_oom` 的"搬 CPU 重算"之所以可行，底层正是本讲的这条 CPU 回退路径——环境课与推理课在此接上了头。

## 7. 下一步学习建议

- 下一讲 **u1-l3（仓库结构）**：装好环境后，正式逛一遍仓库目录树，理解 `mask2former` 与 `mask2former_video` 两个包的分工，以及"import 即注册"的机制；随后 u1-l4 就能用本讲装好的环境跑通第一个推理 demo。
- 想深入本讲的算子本身：单元三 u3-l5 会讲像素解码器如何调用 MSDeformAttn（多尺度展平、`level_embed`），单元四 u4-l1 会精读 `MSDeformAttn.forward` 的四个线性层与采样点设计。
- 延伸阅读：PyTorch 官方文档 *CppExtension / CUDA Extensions* 章节能帮你彻底理解 `setup.py` 里 `cmdclass=BuildExtension` 的工作原理。
