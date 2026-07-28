# 仓库目录结构与代码组织

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 tilelang-metax 仓库顶层每个目录的职责，并画出一张目录树。
- 区分 **Python 前端**（`tilelang/`）与 **C++ 编译核心**（`src/`）这两大代码体，理解它们各自的分工。
- 在 `tilelang/` 中快速定位 `language`、`jit`、`engine`、`backend`、`maca` 等关键子模块。
- 找到各 GPU 后端目录（`cuda`/`rocm`/`maca`/`metal`/`webgpu`），并理解「同一个后端在 Python 侧和 C++ 侧各有一份」的双层结构。
- 在 `examples/`、`docs/`、`testing/` 中分别找到可运行示例、文档与测试。

本讲承接 [u1-l1 项目概览](./u1-l1-project-overview.md)（你已经知道 tilelang 是什么、metax 分支多了 MACA 后端）和 [u1-l2 环境搭建](./u1-l2-build-and-install.md)（你已经能 `import tilelang`）。本讲要做的，是把这两讲里提到的名字（DSL、JIT、engine、backend、maca）落回到磁盘上的真实目录。

## 2. 前置知识

在开始之前，先用通俗语言建立几个概念。

- **前端（frontend）与后端（backend）**：在编译器里，前端负责「读懂用户写了什么」，后端负责「生成能在硬件上跑的代码」。tilelang 的前端是 Python 写的（长得像普通 Python 函数的 DSL），后端是 C++ 写的（基于 TVM 做代码生成）。
- **DSL（领域特定语言）**：专门为「写高性能 kernel」这一类问题设计的小语言。你写的不是真的会立刻执行的 Python，而是被「翻译」成 GPU 代码的规格说明。
- **TVM**：一个开源的深度学习编译器框架。tilelang 不是从零造编译器，而是「站在 TVM 的肩膀上」做扩展——所以你会看到大量 `tvm.xxx` 的引用。
- **backend（这里指 GPU 后端，不是编译器后端一词的狭义用法）**：tilelang 支持多种硬件目标，例如 NVIDIA CUDA、AMD ROCm/HIP、Apple Metal、WebGPU，以及本 fork 的主角 MetaX MACA。每种硬件就叫一个 backend。
- **`warp_size`**：GPU 上「一组同步执行的线程」的大小。NVIDIA CUDA 是 32，而 MACA 是 64。这个数字在阅读后端代码时会反复出现。

> 术语提示：本仓库里有两个不同含义的 "backend"。一个是「编译器后端」= C++ 代码（`src/`）；另一个是「GPU 后端」= cuda/rocm/maca 等。后文我会尽量用「C++ 编译核心」和「GPU 后端」来区分，避免混淆。

## 3. 本讲源码地图

本讲涉及的关键文件如下。先有一个整体印象，第 4 节再逐个精读。

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目门面：定位、安装方式、GEMM 示例、benchmark。 |
| `tilelang/__init__.py` | Python 包入口：加载 C++ 动态库、暴露 `jit`/`compile`/`lower`，并 import 各 GPU 后端。 |
| `tilelang/maca/__init__.py` | MACA 后端的 Python 包入口：注册 maca 的 target 检测、codegen、pipeline、intrinsics。 |
| `tilelang/backend/README.md` | 一份珍贵的「多后端布局设计文档」，解释了 Python/C++ 双层结构。 |
| `src/ir.cc` | C++ 编译核心的入口文件之一：扩展 TVM script 前端，构造线程绑定循环帧。 |
| `docs/index.md` | 文档站点的总目录（Sphinx toctree），列出所有文档分区。 |

## 4. 核心概念与源码讲解

本讲把仓库拆成四个最小模块来读：

1. `tilelang/` —— Python 前端
2. `src/` —— C++ 编译核心
3. `examples/` —— 示例与可运行 kernel
4. `docs/` 与 `testing/` —— 文档与测试基础设施

### 4.1 tilelang 前端：Python DSL 与编译入口

#### 4.1.1 概念说明

`tilelang/` 是你 `import tilelang` 时真正加载的 Python 包。它承担三件事：

1. **提供 DSL 语法**：`T.Kernel`、`T.copy`、`T.gemm`、`T.alloc_shared` 这些你写 kernel 时用的语法糖，都定义在这里。
2. **驱动编译流程**：把 DSL 函数下译成 IR，再交给 C++ 核心生成代码、编译、加载、运行。
3. **注册 GPU 后端**：在 import 时把 cuda/rocm/maca 等后端的 pass pipeline、codegen、intrinsics 注册进框架。

关键设计原则（来自 `tilelang/backend/README.md`）是 **「前端语言层保持后端中立」**：`tilelang/language` 和 `tilelang/tileop` 里不能出现某个具体 GPU 后端的代码；后端相关的实现一律放在 `tilelang/<backend>/` 里。

#### 4.1.2 核心流程

当你写下 `import tilelang` 时，`tilelang/__init__.py` 会按以下顺序初始化（非「轻量导入」模式时）：

```text
1. 计算版本号、初始化日志
2. _lazy_load_lib(): 预加载 torch，加载 C++ 动态库 libtilelang.so
3. 暴露编译入口: jit / compile / par_compile
4. import 语言与编译核心子模块: language, engine, transform, tileop ...
5. import 各 GPU 后端包: cpu, cuda, rocm, metal, maca  ← 在此完成注册
```

第 5 步是理解多后端的关键：**每个后端包在被 import 时，会把自己的 pass pipeline、device codegen、target 检测器注册到全局表里**。之后编译器看到 `target="maca"` 时，就去这些表里查 maca 对应的实现。

`tilelang/` 下与编译/后端直接相关的关键子目录：

| 子目录 | 职责 |
| --- | --- |
| `language/` | DSL 语法层：`T.Kernel`、循环、内存分配、copy、gemm 等语法糖。**后端中立**。 |
| `jit/` | JIT 编译入口：`@tilelang.jit`、`tilelang.compile`、`JITKernel` 对象。 |
| `engine/` | 编译流水线主入口：`lower()`、语义检查、postproc 回调注册。 |
| `backend/` | 后端无关的公共基础设施：pass pipeline / device codegen / host codegen 的注册与解析。 |
| `maca/` | **MACA 后端的 Python 实现**：target 检测、codegen 注册、mfma intrinsics、pipeline。 |
| `cuda/` `rocm/` `metal/` `cpu/` `webgpu/` | 其它 GPU/CPU 后端的 Python 实现，结构与 `maca/` 对称。 |

#### 4.1.3 源码精读

**入口文件 `tilelang/__init__.py`**。它先把 C++ 库加载进来（注意 `_lazy_load_lib` 这个上下文管理器，它通过 `ctypes.CDLL` 加载 `libtilelang.so`）：

加载 C++ 动态库，[tilelang/__init__.py:177-184](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L177-L184) —— 这一步把 `src/` 编译出的 `libtilelang.so` 挂进 Python 进程，让后续的 IR、pass、codegen 都能调用到 C++ 实现。

随后暴露编译入口与 `lower`，[tilelang/__init__.py:186](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L186) 一行就导出了 `jit`、`JITKernel`、`compile`、`par_compile`，这正是你写完 kernel 后调用 `matmul.compile(...)` 的来源；[tilelang/__init__.py:207](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L207) 则导出 `lower` 与各后端的 postproc 注册函数。

最关键的几行是末尾对 GPU 后端的 import，[tilelang/__init__.py:211-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L211-L215) 依次 import `cpu / cuda / rocm / metal / maca`——其中最后一行 `from . import maca as maca` 就是把本 fork 的 MACA 后端挂上。删掉这一行，metax 分支就退化成上游 tilelang。

**MACA 后端的 Python 入口 `tilelang/maca/__init__.py`**，非常简洁：

[tilelang/maca/__init__.py:1-6](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/__init__.py#L1-L6) 只做一件事：在 import 时把 maca 的 `intrinsics`、`op`、`pipeline`、`target`、`execution_backend`、`transform` 子模块拉进来，从而触发各模块里的注册逻辑（`register_pipeline`、`register_device_codegen`、`register_target_detector` 等）。

**多后端设计文档 `tilelang/backend/README.md`**。这份文档明确点出了双层布局：

[tilelang/backend/README.md:11-16](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/README.md#L11-L16) 说明 Python 后端层分成 `tilelang/backend/`（公共管道）和 `tilelang/<backend>/`（后端自有实现），native 侧则在 `src/<backend>/` 镜像这个划分。

文档里还有一张后端注册对照表，[tilelang/backend/README.md:68-74](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/README.md#L68-L74) 列出 `tilelang/cuda`→`cuda`、`tilelang/rocm`→`hip`、`tilelang/cpu`→`c`/`llvm`、`tilelang/metal`→`metal` 的对应关系。注意一个反直觉的点：`tilelang/rocm` 这个包注册的 target kind 是 `hip`（包名和 target 名并不总是一致）。MACA 后端虽未进这张表（它是 fork 新增的），但遵循完全相同的模式：`tilelang/maca`→target kind `maca`。

#### 4.1.4 代码实践

**实践目标**：用一行 `import tilelang` 触发的副作用，验证「后端是在 import 时注册的」。

**操作步骤**：

1. 确认已按 [u1-l2](./u1-l2-build-and-install.md) 完成安装并能 `import tilelang`。
2. 打开 Python，执行：

   ```python
   import tilelang
   # 查看 maca 后端是否被加载
   print(tilelang.maca)
   # 查看顶层包里都暴露了哪些后端
   for name in ["cpu", "cuda", "rocm", "metal", "maca", "webgpu"]:
       print(name, "->", getattr(tilelang, name, "(未导出)"))
   ```

**需要观察的现象**：`tilelang.maca` 是一个有效模块对象（而非报错），且 `cpu/cuda/rocm/metal/maca` 都能被 `getattr` 取到。

**预期结果**：每个后端名都打印出一个模块对象地址，不出现 `(未导出)`。这说明 [tilelang/__init__.py:211-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L211-L215) 的 import 全部成功执行。

> 若无可用环境，可改为「源码阅读型实践」：在 `tilelang/__init__.py` 第 211–215 行依次注释掉某个后端的 import（不要提交改动），仅从依赖关系推测：注释掉 `maca` 后，`import tilelang` 还能成功吗？`target="maca"` 还能编译吗？（预期：import 仍成功，但 maca 编译会因找不到注册而失败——待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tilelang/language/` 里不应该出现 `mfma`（MACA 专用）或 `wgmma`（CUDA 专用）字样？

> **参考答案**：因为 `language/` 被设计为「后端中立」的 DSL 语法层（见 `backend/README.md` 的 Guidelines）。后端专属的指令发射应放在 `tilelang/<backend>/intrinsics/` 里。这样同一份 DSL 代码可以被编译到不同硬件。

**练习 2**：`tilelang/rocm` 包注册的 target kind 是什么？为什么包名和 target 名不一致？

> **参考答案**：target kind 是 `hip`。包名 `rocm` 是平台名（AMD ROCm），而底层编程模型叫 HIP，TVM 用 `hip` 作为 target kind。这正说明文档强调「优先用显式 target-kind 注册，而非靠文件夹名匹配」。

---

### 4.2 src 后端：C++ 编译核心

#### 4.2.1 概念说明

`src/` 是 tilelang 的 C++ 编译核心，编译后产出 `libtilelang.so`，由 Python 侧通过 `ctypes` 加载。如果说 `tilelang/` 是「指挥」，`src/` 就是「干活的工人」：所有 IR 变换（pass）、指令选择、代码生成都发生在这里。

它同样遵循「公共 + 各后端自有」的双层布局：`src/backend/common/` 放跨后端共享的 C++ 工具，`src/<backend>/` 放各后端的 C++ 实现。

#### 4.2.2 核心流程

`src/` 顶层目录职责一览：

| 目录 / 文件 | 职责 |
| --- | --- |
| `ir.cc` | 扩展 TVM script 前端，提供 TileLang 专用的循环帧构造。 |
| `config.h` | 编译期全局配置宏。 |
| `op/` | tile 算子的 C++ 基类与各算子实现（gemm、copy、reduce、scan 等）。 |
| `transform/` | 编译 pass 体系：lower_tile_op、split_host_device、layout_inference、inject_pipeline 等数十个 pass。 |
| `layout/` | 内存布局推断与 swizzle。 |
| `tl_templates/` | 各后端的指令模板（cuda/hip/maca/cpu），封装 MMA/WGMMA/MFMA 等硬件指令。 |
| `backend/` | 共享的 C++ 后端工具（如 target_utils）。 |
| `runtime/` | TVM runtime 集成。 |
| `support/` | 通用辅助（check 等）。 |
| `cuda/` `rocm/` `maca/` `metal/` `cpu/` `webgpu/` | 各 GPU/CPU 后端的 C++ 实现：codegen、runtime、op、transform。 |

特别地，**MACA 后端的 C++ 实现 `src/maca/`** 与 cuda/rocm 完全对称，也分成四个子目录：

```text
src/maca/
  codegen/    # CodeGenTileLangMACA、intrin 规则、runtime module
  op/         # gemm/copy/reduce 等算子的 MACA 下译
  runtime/    # maca target kind 注册、device API、module 加载
  transform/  # lower_maca_intrin 等 MACA 专属 pass
```

#### 4.2.3 源码精读

**入口文件 `src/ir.cc`**。它的注释开门见山说明了定位，[src/ir.cc:1-5](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L1-L5) 写着「Extension for the tvm script frontend」——即它是 TVM script 前端的 TileLang 扩展。

它实现的第一个重要函数 `MakeThreadBindingFrame`，[src/ir.cc:26-55](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L26-L55)，构造一个「目标中立的线程绑定循环帧」。注释里有一句话点明了它和后续 pass 的关系：这种启动维度（kernel-launch dimension）会被 `tl.MaterializeKernelLaunch` pass 在编译期、当 Target 已知时，物化成目标相关形式（GPU 上是 `thread_extent` AttrStmt，CPU 上是普通 serial 循环）。这句话串起了「前端写法」与「后端物化」之间的桥梁。

**算子基类目录 `src/op/`**，[src/op/](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.cc) 定义了所有 tile 算子共有的抽象基类 `TileOperatorNode`（其两个核心虚方法 `Lower()` 与 `InferLayout()` 会在 u5-l1、u9-l2 详细讲）。你能在这里看到 `gemm.cc`、`copy.cc`、`reduce.cc`、`scan.cc` 等算子的 C++ 下译逻辑。

**transform pass 体系 `src/transform/`**，[src/transform/](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc) 目录下有几十个 pass 文件，文件名通常就是 pass 名。注意其中 `lower_maca_memcpy_async.cc`、`maca_memcpy_async_injector.h` 这类带 `maca` 字样的文件——它们是 metax 分支为 MACA 异步拷贝新增的 pass，是 fork 的差异化痕迹。

**MACA 后端 C++ 入口 `src/maca/runtime/maca_target_kind.cc`**，[src/maca/runtime/maca_target_kind.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc) 注册了 MACA 的 target kind，并设定了 `warp_size = 64`（与 CUDA 的 32 不同）等硬件属性。这是 metax 分支最核心的注册点之一，u7-l1 会逐行精读。

#### 4.2.4 代码实践

**实践目标**：从磁盘结构验证「每个 GPU 后端在 C++ 侧都有 codegen/op/runtime/transform 四件套」。

**操作步骤**：

1. 在仓库根目录执行（仅查看目录，不修改任何文件）：

   ```bash
   ls src/maca
   ls src/cuda
   ls src/rocm
   ```

2. 对比三个目录的子目录名，找出共同的与各自的差异。

**需要观察的现象**：三个后端目录下都有 `codegen/` 和 `op/`；`cuda`/`rocm` 多了 `stubs/`（驱动/运行时桩），`maca` 多了 `runtime/`（直接把 runtime 放在后端目录内）。

**预期结果**：你会得到一张类似下表的对照：

| 后端 | 子目录 |
| --- | --- |
| `src/cuda` | codegen, op, stubs, transform |
| `src/rocm` | codegen, op, stubs, transform |
| `src/maca` | codegen, op, runtime, transform |

> 这些差异不影响功能，只反映了各后端对代码的组织偏好（待本地验证目录树）。

#### 4.2.5 小练习与答案

**练习 1**：`src/ir.cc` 里构造的「线程绑定循环帧」最终由哪个 pass 物化成 GPU 的 `thread_extent`？

> **参考答案**：由 `tl.MaterializeKernelLaunch` pass 物化（见 [src/ir.cc:26-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L26-L30) 的注释）。该 pass 在编译期、Target 已知时执行。

**练习 2**：如果你要在 C++ 侧为 MACA 新增一个 tile 算子，应该把实现文件放在哪里？

> **参考答案**：放在 `src/maca/op/` 下（参考已有的 `src/maca/op/gemm.cc`、`src/maca/op/copy.cc`），与 cuda/rocm 的 `op/` 对称。跨后端共享的基类则在 `src/op/`。

---

### 4.3 examples：示例与可运行 kernel

#### 4.3.1 概念说明

`examples/` 是 tilelang 的「活教材」：每个子目录是一个真实算子的完整可运行示例，通常包含 kernel 实现、参考实现（用 PyTorch/cuDNN 算正确答案）、测试脚本和 README。这是学习新算子写法的最佳起点，也是本手册后续讲义频繁引用的素材。

#### 4.3.2 核心流程

`examples/` 顶层有近 50 个子目录，覆盖大量算子。按用途可大致分组：

| 类别 | 代表目录 |
| --- | --- |
| 入门 / 快速上手 | `quickstart.py`、`gemm/`、`elementwise/`、`cast/` |
| 注意力家族 | `flash_attention/`、`flash_attention_sm100/`、`linear_attention/`、`deepseek_mla/`、`deepseek_nsa/`、`seer_attention/` |
| 量化 / 稀疏 GEMM | `dequantize_gemm/`、`gemm_fp8/`、`gemm_int4/`、`gemm_sp/`（2:4 稀疏） |
| GEMM 变体 | `gemm_splitk/`、`gemm_streamk/`、`grouped_gemm/`、`blockscaled_gemm_sm100/` |
| 调试 / 工具 | `plot_layout/`（布局可视化）、`visual_layout_inference/` |
| 平台专属 | `amd/`（AMD 专属示例） |

顶层还有两个共享文件：`conftest.py`（pytest fixture）和 `pytest.ini`（pytest 配置），说明 examples 既是文档也是测试套件——你可以用 pytest 直接跑它们。

最经典的入门示例 `examples/gemm/`，[examples/gemm/](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py) 目录下有：`example_gemm.py`（基础 GEMM）、`example_gemm_persistent.py`（persistent kernel）、`example_gemm_intrinsics.py`（显式用 intrinsic）、`example_gemm_autotune.py`（自动调优）、`test_example_gemm.py`（pytest 数值正确性测试）。

#### 4.3.3 源码精读

**项目门面 README 中的示例指引**，[README.md:45-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L45-L50) 列出了主要算子示例入口（GEMM、Dequant GEMM、FlashAttention、LinearAttention、Flash MLA Decoding、Native Sparse Attention），每个都指向 `examples/` 下的子目录。

**测试过的设备清单**，[README.md:39-40](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L39-L40) 明确提到「for MetaX GPUs, it includes the C500」——这是本 fork 区别于上游的直接证据：上游 tilelang 不会列出 MetaX GPU。

**入门 GEMM 示例**，[examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py) 是 u1-l4「第一个 kernel」要精读的对象，这里只需记住它在 `examples/gemm/` 下。

#### 4.3.4 代码实践

**实践目标**：在不运行的情况下，学会「从 examples 目录反推一个算子的学习路径」。

**操作步骤**：

1. 进入 `examples/gemm/`，阅读 `README.md`（若有）和文件名，回答：
   - 哪个文件是「最朴素的可运行版」？（提示：`example_gemm.py`）
   - 哪个文件演示「显式 intrinsic」？哪个演示「自动调优」？
2. 浏览 `examples/plot_layout/`，找出布局可视化脚本。

**需要观察的现象**：每个算子目录基本都遵循「基础版 → 高级版（persistent/intrinsics/autotune）→ 测试」的固定模式。

**预期结果**：你能说出 `gemm/` 下五个 `example_*.py` 文件分别对应什么主题。这是本手册 u6（张量核与 intrinsics）、u8（调优）讲义的素材来源。

> 若想真正运行：参考 `examples/conftest.py` 与 `examples/pytest.ini`，用 `pytest examples/gemm/test_example_gemm.py` 跑数值正确性测试（需对应硬件，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果想看「2:4 结构化稀疏 GEMM」怎么写，应该读 `examples/` 下哪个目录？

> **参考答案**：`examples/gemm_sp/`（2:4 稀疏张量核）。对应的 DSL 入口是 `T.gemm_sp`。

**练习 2**：`examples/` 顶层的 `conftest.py` 和 `pytest.ini` 说明了 examples 目录的双重身份是什么？

> **参考答案**：examples 既是「教学文档」也是「可执行的测试套件」。借助 pytest，每个示例既能被阅读学习，也能被自动运行来验证正确性。

---

### 4.4 docs 与 testing：文档与测试基础设施

#### 4.4.1 概念说明

`docs/` 是基于 Sphinx 的文档站点源码（编译后发布到 tilelang.com），按分区组织了从入门到内部的全部说明。`testing/` 是项目自有的、与 `examples/` 互补的单元/集成测试目录（C++ 与 Python 两套）。两者共同构成「验证代码是否正确」的基础设施。

#### 4.4.2 核心流程

`docs/index.md` 是文档总目录（toctree），分区清晰：

| 分区 | 内容 |
| --- | --- |
| GET STARTED | 安装、概览、targets |
| TUTORIALS | 调试工具、自动调优、日志 |
| PROGRAMMING GUIDES | 语言基础、指令、控制流、软件流水线、类型系统、自动调优 |
| DEEP LEARNING OPERATORS | elementwise、gemv、matmul、稀疏 matmul、DeepSeek MLA |
| COMPILER INTERNALS | letstmt_inline、inject_fence_proxy、tensor_checks |
| DEVELOPER GUIDE | C++ 代码风格 |
| API Reference | autoapi 自动生成的 Python API |

`testing/` 目录分两层：

```text
testing/
  cpp/        # C++ 侧测试（Google Test 风格）
  python/     # Python 侧测试，按主题分子目录
    cuda/  rocm/  metal/  cpu/  ...   # 按后端分
    language/  transform/  layout/    # 按编译阶段分
    autotune/  profiler/  jit/        # 按功能模块分
```

仓库根目录还有一组 `requirements-test-*.txt`（cuda/rocm/maca/metal），为不同后端的测试声明额外依赖——这印证了「不同后端测试环境不同」。

#### 4.4.3 源码精读

**文档总目录 `docs/index.md`**，[docs/index.md:11-18](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/index.md#L11-L18) 是 GET STARTED 分区的 toctree，列出了 `Installation`、`overview`、`targets` 三篇入门文档。本手册的 u1-l2（安装）、u1-l1（概览）、u3-l1（targets）正对应这三篇官方文档。

[docs/index.md:29-41](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/index.md#L29-L41) 是 PROGRAMMING GUIDES 分区，列出 `language_basics`、`instructions`、`control_flow`、`software_pipeline`、`type_system` 等——这些是本手册 U2（TileLang 语言基础）讲义的主要依据。

**MACA 安装文档**，[docs/get_started/Installation_maca.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md) 是 metax 分支独有的文档，专讲 MACA SDK 与 `USE_MACA=ON` 构建（u1-l2 已讲过）。

**测试目录**，[testing/python/](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/test_version_provider.py) 按主题/后端分子目录组织 Python 测试。注意它与 `examples/` 的分工：`examples/` 面向「教学 + 端到端算子」，`testing/` 面向「编译器各阶段的细粒度单元测试」（如 `transform/` 测单个 pass、`layout/` 测布局推断、`target/` 测 target 解析）。

#### 4.4.4 代码实践

**实践目标**：用文档目录反查「我接下来该读哪篇官方文档」。

**操作步骤**：

1. 打开 [docs/index.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/index.md)。
2. 针对本手册的下面几讲，在 toctree 里找到对应的官方文档文件名：
   - 「软件流水线」→ 哪个 `.md`？
   - 「类型系统」→ 哪个 `.md`？
   - 「调试工具」→ 在哪个分区？
3. 进入 `testing/python/`，数一数按后端分的测试目录有哪几个。

**需要观察的现象**：每个 toctree 条目都对应 `docs/` 下一个真实的 `.md` 文件；`testing/python/` 下能找到 `cuda/`、`rocm/`、`metal/` 等后端测试目录。

**预期结果**：你会确认 `software_pipeline.md`、`type_system.md` 存在于 `docs/programming_guides/`，`debug_tools_for_tilelang.md` 在 `docs/tutorials/`。MACA 后端的专属测试在 u9-l4 会详讲。

> 待本地验证：`testing/python/` 下是否已有 `maca/` 目录（取决于 fork 进度）。

#### 4.4.5 小练习与答案

**练习 1**：官方文档把内容分成哪几个大区？「编译器内部机制」相关文档在哪个区？

> **参考答案**：大区有 GET STARTED、TUTORIALS、PROGRAMMING GUIDES、DEEP LEARNING OPERATORS、COMPILER INTERNALS、DEVELOPER GUIDE、API Reference。「编译器内部机制」在 COMPILER INTERNALS 区（如 `letstmt_inline`、`inject_fence_proxy`、`tensor_checks`）。

**练习 2**：`testing/python/` 和 `examples/` 都有测试，它们的分工是什么？

> **参考答案**：`examples/` 偏「端到端算子的正确性与教学」，每个目录是一个完整算子；`testing/python/` 偏「编译器内部各阶段的细粒度单元测试」，按 pass/layout/target/后端等维度切分，用于保证编译器本身各部件正确。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面的「仓库地图」任务。这是本讲的主线实践。

**任务**：产出两份产物。

**产物 1：仓库顶层目录树**。仿照下面的格式，补全每个顶层条目的一句话职责（基于你本讲学到的，不要复制粘贴）：

```text
tilelang-metax/
├── tilelang/      # Python 前端：DSL 语法 + 编译入口 + 各 GPU 后端注册
├── src/           # C++ 编译核心：IR/pass/算子/codegen，编译成 libtilelang.so
├── examples/      # 算子示例，既是教材也是 pytest 可执行测试
├── docs/          # Sphinx 文档站点源码
├── testing/       # 编译器各阶段的 C++/Python 单元测试
├── 3rdparty/      # 第三方依赖：定制版 TVM、cutlass、hip-headers 等
├── benchmark/     # 性能基准脚本
├── maint/         # 维护脚本（gemm、精度校验、host 检查）
├── cmake/         # CMake 辅助脚本
├── docker/        # Dockerfile
├── CMakeLists.txt # C++ 构建总入口
├── pyproject.toml # Python 包构建配置（scikit-build-core）
└── requirements*.txt # 运行/测试/各后端依赖声明
```

> 提示：用 `ls`（仅查看，不改动）核对每个目录确实存在；`3rdparty/` 下能看到 `tvm/`（定制版 TVM 子模块，u1-l2 讲过）、`cutlass/`、`hip-headers/`、`composable_kernel/`。

**产物 2：tilelang/ 五大子模块一句话职责**。为下面五个子目录各写一句话（不超过 25 字）说明它的职责，要求体现出「它解决什么问题」：

| 子目录 | 一句话职责（请填写） |
| --- | --- |
| `tilelang/language/` | |
| `tilelang/jit/` | |
| `tilelang/engine/` | |
| `tilelang/backend/` | |
| `tilelang/maca/` | |

**参考答案（先自己写完再对照）**：

| 子目录 | 参考职责 |
| --- | --- |
| `tilelang/language/` | 后端中立的 DSL 语法层：定义 T.Kernel / 循环 / 内存 / copy / gemm 等语法糖。 |
| `tilelang/jit/` | JIT 编译入口：`@tilelang.jit`、`compile`，产出可调用的 JITKernel 对象。 |
| `tilelang/engine/` | 编译流水线主控：`lower()`、语义检查、host/device 拆分与 postproc 回调。 |
| `tilelang/backend/` | 后端无关的公共管道：pass pipeline / device codegen / host codegen 的注册与解析。 |
| `tilelang/maca/` | MACA 后端的 Python 实现：target 检测、codegen 注册、mfma intrinsics、pipeline。 |

**自检**：如果你写出的职责里，`language/` 出现了「mfma/wgmma」、或 `backend/` 出现了「maca 专属」，说明你混淆了「公共层」与「后端层」——回去重读 4.1.3 的 `backend/README.md` 设计原则。

## 6. 本讲小结

- 仓库分为 **Python 前端 `tilelang/`** 与 **C++ 编译核心 `src/`** 两大代码体，前者指挥、后者干活，两者通过 `ctypes` 加载 `libtilelang.so` 衔接。
- `tilelang/` 采用「公共 + 后端自有」的双层布局：`language/tileop` 后端中立，各 GPU 后端实现放在 `tilelang/<backend>/`，公共管道放在 `tilelang/backend/`。
- 各 GPU 后端（cuda/rocm/maca/metal/cpu/webgpu）在 import 时把自身注册进全局表；`tilelang/__init__.py:211-215` 的 `from . import maca` 是 metax 分支挂载 MACA 后端的那一行。
- C++ 侧 `src/` 镜像同样的双层布局，`src/maca/` 含 codegen/op/runtime/transform 四件套，与 cuda/rocm 对称。
- `examples/` 是「教材 + 测试」二合一的算子集合；`docs/` 是 Sphinx 文档源码；`testing/` 是编译器各阶段的细粒度单元测试。
- metax 分支的物理痕迹散布各处：`README.md` 的 MetaX C500、`docs/get_started/Installation_maca.md`、`tilelang/maca/`、`src/maca/`、`src/transform/*maca*`、`requirements-test-maca.txt`。

## 7. 下一步学习建议

本讲帮你建立了仓库的「空间地图」。接下来：

- **立刻动手写第一个 kernel** → 进入 [u1-l4 第一个 kernel：跑通 GEMM 快速上手](./u1-l4-first-gemm-kernel.md)，用 `examples/gemm/example_gemm.py` 把本讲的目录认知变成可运行代码。
- **想深入某个后端的注册细节** → 记住 `tilelang/maca/` 与 `src/maca/` 的位置，它们会在 U3（运行）和 U7（MACA 后端深入）中被逐行精读。
- **想理解编译流水线** → `tilelang/engine/` 和 `src/transform/` 是 U4（编译流水线）和 U5（代码生成与后端）的主战场。
- **建议先存一份本讲的「产物 1 目录树」**，后续每读一讲，就往树上标注你新认识的文件——它会成为你贯穿全本的导航图。
