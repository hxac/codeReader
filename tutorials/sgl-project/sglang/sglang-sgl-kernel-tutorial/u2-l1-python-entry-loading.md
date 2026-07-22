# Python 入口与架构自适应加载

## 1. 本讲目标

在上一篇（u1-l2）里，我们建立了「六步内核流水线」这条贯穿全册的主线索，并学会了在源码里定位任意一个算子的四个落点。本讲我们要回答一个更前置的问题：**当用户在代码里写下 `import sgl_kernel` 的那一刻，究竟发生了什么？**

学完本讲，你应当能够：

1. 说清 `python/sgl_kernel/__init__.py` 在不同平台（macOS / Linux+CUDA / ROCm / MUSA）上走的不同分支，以及它按什么顺序完成「版本号 → 加载算子库 → 预加载 CUDA 运行时 → 再导出算子」这一整套初始化。
2. 解释 `load_utils.py` 如何根据 GPU 的 **compute capability**（计算能力版本号）在 `sm90` 与 `sm100` 两个目录之间二选一，并掌握 `_load_architecture_specific_ops` 的**三级回退**加载策略。
3. 理解 `_preload_cuda_library` 为什么要提前把 `libcudart.so` 用 `ctypes.CDLL(..., RTLD_GLOBAL)` 载入，从而消除「`libcudart.so.12 not found`」这类运行时报错。

本讲是 u2 单元「一个算子的完整链路：从 Python 到 CUDA」的第一步——先把 Python 这一端的「大门」打开，下一篇 u2-l2 才走进门里去讲 `torch.ops` 分派。

## 2. 前置知识

- **Python 的 `import` 机制**：执行 `import sgl_kernel` 时，Python 会运行包目录下的 `__init__.py`，从上到下逐行执行。因此 `__init__.py` 既是「包的入口」，也是「初始化逻辑的承载者」。
- **动态加载扩展（`.so`）**：PyTorch 的自定义算子最终会被编译成一个 C++ 扩展动态库（Linux 上是 `.so`）。Python 标准库的 `importlib.util` 可以根据一个**文件路径**把这个 `.so` 当作模块加载进来。
- **compute capability（计算能力）**：NVIDIA 给每代 GPU 一个形如 `major.minor` 的版本号，例如 H100 是 `9.0`、A100 是 `8.0`、Blackwell B200 是 `10.0`。它决定了 GPU 支持的指令集。本讲里我们把它折算成一个整数 \( \text{cc} = \text{major} \times 10 + \text{minor} \)，于是 H100 → 90、B200 → 100。
- **`libcudart.so`**：CUDA 运行时库（CUDA Runtime）。CUDA 扩展 `.so` 在运行时会去查找它；如果系统找不到，就会报 `libcudart.so.12: cannot open shared object file` 之类的错误。
- **`ctypes.RTLD_GLOBAL`**：用这个模式 `dlopen` 一个库，会把它的符号放进**全局符号表**，于是「之后」被加载的其他库都能解析到这些符号。这是预加载消除报错的关键。

如果你对上一讲的「双产物 sm90 / sm100」还有印象，会发现本讲正是这两份 `.so` 被**按需选择加载**的地方——两份产物的来源见 u1-l3。

## 3. 本讲源码地图

本讲只关心 Python 侧的「加载入口」，涉及三个文件：

| 文件 | 作用 |
| --- | --- |
| [python/sgl_kernel/__init__.py](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py) | 包入口：按平台/后端分流，加载算子库，预加载 CUDA 运行时，再把各子模块的算子 re-export 出来。 |
| [python/sgl_kernel/load_utils.py](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py) | 加载工具：探测 compute capability、在 sm90/sm100 间选择、三级回退加载 `common_ops`，以及预加载 CUDA 运行时。 |
| [python/sgl_kernel/debug_utils.py](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/debug_utils.py) | 调试包装：用 `SGLANG_KERNEL_API_LOGLEVEL` 环境变量控制是否把算子调用包一层日志。 |

此外会引用构建侧的安装目标作为佐证：

- [CMakeLists.txt](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt) 的两个 `common_ops_*_build` target 与 `install` 目标，说明 `sm90/`、`sm100/` 两个子目录是怎么来的。

## 4. 核心概念与源码讲解

### 4.1 `__init__.py` 的平台/后端分支与导入总流程

#### 4.1.1 概念说明

`sgl_kernel` 这个包要支持多种硬件后端：Apple Silicon（Metal）、NVIDIA（CUDA）、AMD（ROCm/HIP）、摩尔线程（MUSA）。这些后端的算子库**完全不同**——macOS 上只发 Metal 扩展，CUDA 上才有一堆 GPU 算子。

因此 `__init__.py` 的第一职责就是**分流**：在最顶层用 `if` 把不同平台隔开，避免在 macOS 上去 import 一个根本不存在的 CUDA 扩展而报错。分流之后，CUDA 分支再完成「加载主算子库 → 预加载 CUDA 运行时 → 再导出全部算子」三件事。

#### 4.1.2 核心流程

`import sgl_kernel` 时，`__init__.py` 自上而下的执行顺序可以概括为：

```text
1. 暴露版本号 __version__                         (所有平台都要)
2. 判断平台：
   ├── macOS arm64  → 只加载 Metal 扩展，结束
   └── 其他平台     → 进入 CUDA/ROCm/MUSA 通用分支：
        3. import torch
        4. common_ops = _load_architecture_specific_ops()   # 选 sm90/sm100 并加载 .so
        5. 若是 CUDA 构建 → _preload_cuda_library()          # 预载 libcudart
        6. 从各子模块 re-export 全部算子名
        7. 按 torch.version.hip / musa 增补后端专属算子
        8. 用 SGLANG_KERNEL_API_LOGLEVEL 决定是否给算子套调试包装
        9. 用「延迟导入」定义 greenctx 相关函数
```

步骤 4、5 是本讲的两个核心模块（4.2 与 4.3）。

#### 4.1.3 源码精读

**版本号无条件暴露**。第一段就把 `__version__` 引入，这是所有平台共有的：

[python/sgl_kernel/__init__.py:4-4](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L4-L4) —— 从 `version.py` 引入 `__version__`，对应 `python/sgl_kernel/version.py` 里那句 `__version__ = "0.4.5"`。

**平台分流**。紧接着的 `if sys.platform == "darwin" and platform.machine() == "arm64"` 是 macOS Apple Silicon 的判定：满足则只走 Metal 分支，**连 `import torch` 都不会执行**；其余所有平台进 `else`：

[python/sgl_kernel/__init__.py:6-19](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L6-L19) —— 注释明确写着「On macOS only the Metal extension is shipped」；`else` 分支里 `import torch`、引入 `_load_architecture_specific_ops` / `_preload_cuda_library`，并调用前者得到 `common_ops`。

> 注意一个细节：macOS 分支用的是 `from sgl_kernel.metal import *`，而 `metal.py` 内部还会去加载编译好的 `_metal` 扩展并注册 `.metallib`（见 [python/sgl_kernel/metal.py:13-27](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/metal.py#L13-L27)）。本讲不展开 Metal，只需记住「macOS 上算子库的入口是 metal.py，不是 common_ops」。

**预加载 CUDA 运行时**。`common_ops` 加载完之后，紧接着判断是否为 CUDA 构建：

[python/sgl_kernel/__init__.py:21-23](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L21-L23) —— 只有 `torch.version.cuda is not None`（即 CUDA 版 PyTorch）才预加载。ROCm（`torch.version.hip`）和 CPU 构建会跳过这一步。这里的顺序值得留意：**先加载算子库（第 19 行），再预加载 CUDA 运行时（第 23 行）**。预加载主要服务于「之后」才被 `dlopen` 的下游 CUDA 库，详见 4.3。

**再导出算子**。之后的几十行都是从各功能子模块把算子名 re-export 到包顶层，例如：

[python/sgl_kernel/__init__.py:35-50](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L35-L50) —— 把 `elementwise.py` 里的 `rmsnorm`、`fused_add_rmsnorm`、`silu_and_mul` 等导入到 `sgl_kernel` 命名空间，这样用户写 `sgl_kernel.rmsnorm(...)` 就能直接调用。

**后端专属算子的条件增补**。CUDA 之外的两种后端有额外算子，用 `torch.version.hip` / `torch.version.musa` 守卫：

[python/sgl_kernel/__init__.py:137-150](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L137-L150) —— ROCm 增补 `gelu_quick` 与 `deepseek_v4_topk_transform_512`；MUSA 增补一组 `musa_*` 采样/GEMV 算子。这就是「同一份 `__init__.py`，不同后端暴露不同算子集」的实现方式。

**调试包装**。文件末尾把一批算子名收集进 `_DEBUG_EXPORT_NAMES`，然后循环套上 `maybe_wrap_debug_kernel`：

[python/sgl_kernel/__init__.py:220-227](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L220-L227) —— 对每个已存在于模块全局命名空间的算子名，用 `maybe_wrap_debug_kernel` 重新绑定。包装是否生效取决于 `debug_utils.py` 里的环境变量判断（见 4.3.3）。

**延迟导入**。最后两个函数 `create_greenctx_stream_by_value` / `get_sm_available` 没有在顶层 import，而是把 import 藏在函数体里：

[python/sgl_kernel/__init__.py:229-237](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L229-L237) —— 这种「用到才 import」的写法（lazy import）可以避免在 `import sgl_kernel` 阶段就把 `spatial.py` 及其重依赖（greenctx / SM 分区相关）全部拉起来，缩短导入时间、降低不必要的依赖耦合。

#### 4.1.4 代码实践

**实践目标**：验证「macOS 分支不会 import torch」这一行为，理解平台分流的实际效果。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [python/sgl_kernel/__init__.py:6-23](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L6-L23)，确认 `import torch` 位于 `else` 分支内部（第 11 行），而非模块顶层。
2. 思考：如果某天有人把 `import torch` 误移到第 4 行（版本号之后、`if` 之前），在「装了 sgl_kernel 但没装 PyTorch」的 macOS 机器上会发生什么？

**需要观察的现象 / 预期结果**：

- 当前实现下，macOS arm64 用户即使没装 CUDA 版 PyTorch 也能 `import sgl_kernel`（只用到 Metal）。
- 若把 `import torch` 提到顶层，则 macOS 上 `import sgl_kernel` 会因为找不到 torch 而 `ImportError`——这正是平台分流要避免的。

> 若你手头就是一台 macOS arm64 机器，可以 `python -c "import sgl_kernel; print(sgl_kernel.__version__)"` 直接验证；其他平台跳过此步即可，结论标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 要用 `sys.platform == "darwin" and platform.machine() == "arm64"` 两个条件，而不只用 `sys.platform == "darwin"`？

**参考答案**：因为 `darwin`（macOS）既可能是 Apple Silicon（`arm64`）也可能是 Intel Mac（`x86_64`）。本包只为 Apple Silicon 发了 Metal 扩展；Intel Mac 上既没有 Metal 扩展也不在支持范围内，所以需要同时限定架构，避免在 Intel Mac 上误走 Metal 分支。

**练习 2**：在 ROCm 构建里，`_preload_cuda_library()` 会被调用吗？为什么？

**参考答案**：不会。第 22 行的守卫是 `if torch.version.cuda is not None`，ROCm 构建里 `torch.version.cuda` 为 `None`（取而代之的是 `torch.version.hip`），所以直接跳过。ROCm 有自己的运行时，不需要预载 `libcudart`。

---

### 4.2 compute capability 探测与 sm90/sm100 选择

#### 4.2.1 概念说明

上一篇 u1-l3 讲过：同一份 CUDA 源码会被编译成**两个** `common_ops` 产物——`sm90` 版带 `-use_fast_math`（为 Hopper/H100 优化），`sm100` 版用精确数学（兼容性更好）。这两个 `.so` 安装到包内不同的子目录：

[python/sgl_kernel/load_utils.py:56-68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L56-L68) —— 依据 compute capability 在 `sm90` / `sm100` 两个子目录之间选择。

那么运行时该选哪一个？答案就是「看当前 GPU 的 compute capability」。这把「构建期产出两份」和「运行期挑一份」连成了一个闭环。

#### 4.2.2 核心流程

选择规则只有三条，但很关键：

```text
cc = GPU 的 major*10 + minor
if cc == 90:        → sm90   (H100/Hopper，fast math)
elif cc is not None:→ sm100  (任何其他 NVIDIA GPU，precise math)
else:               → sm100  (没检测到 GPU / CPU，precise math)
```

换言之：**只有 H100（cc=90）走 fast-math 的 sm90 版；其余所有 NVIDIA 显卡（A100=80、Blackwell=100…）以及「无 GPU」的情况，都走精确数学的 sm100 版。** 这看似「sm100」名字像 Blackwell 专用，其实它是「除 H100 之外的默认通用版本」。选择逻辑见：

[python/sgl_kernel/load_utils.py:60-68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L60-L68)。

compute capability 的折算公式：

\[
\text{cc} = \text{major} \times 10 + \text{minor}
\]

由 `_get_compute_capability` 实现：

[python/sgl_kernel/load_utils.py:15-25](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L15-L25) —— 若 `torch.cuda.is_available()` 为假（无 GPU 或非 CUDA 构建），直接返回 `None`；否则取当前设备的 `major/minor` 折算成整数。

#### 4.2.3 源码精读

**compute capability 折算**：

[python/sgl_kernel/load_utils.py:15-25](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L15-L25) —— 注意它取的是 `torch.cuda.current_device()` 这**一张**卡的属性。多卡异构（混插 H100 与 A100）时，以当前默认设备为准——这是潜在的边界情况，值得留意。

**三选一**：

[python/sgl_kernel/load_utils.py:59-68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L59-L68) —— `ops_subdir` 被赋值为 `"sm90"` 或 `"sm100"`，`variant_name` 仅用于日志描述。

**与构建产物对应**。这两份 `.so` 的 `OUTPUT_NAME` 都叫 `common_ops`，只是安装目录不同，所以运行时才能靠「目录」区分：

[CMakeLists.txt:322-345](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L322-L345) —— 两个 target 的 `OUTPUT_NAME` 都是 `"common_ops"`，分别输出到 `${...}/sm90` 与 `${...}/sm100` 子目录。

[CMakeLists.txt:382-383](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L382-L383) —— `install` 把它们分别装进 wheel 内的 `sgl_kernel/sm90` 与 `sgl_kernel/sm100`。于是安装后磁盘上就是 `site-packages/sgl_kernel/sm90/common_ops.*.so` 与 `site-packages/sgl_kernel/sm100/common_ops.*.so`，正好对上 load_utils.py 里 `sgl_kernel_dir / ops_subdir / "common_ops.*"` 的查找模式。

#### 4.2.4 代码实践

**实践目标**：回答规格里提出的两个问题——H100 和 Blackwell 分别加载哪个目录的 `common_ops`。

**操作步骤**（源码阅读型；若环境允许可在 GPU 机上实测）：

1. 读 [python/sgl_kernel/load_utils.py:15-68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L15-L68)，把每代 GPU 的 `major.minor` 代入公式 \( \text{cc} = \text{major}\times 10 + \text{minor} \)。
2. 填表：

| GPU | major.minor | cc | 加载目录 | variant |
| --- | --- | --- | --- | --- |
| H100 (Hopper) | 9.0 | 90 | `sgl_kernel/sm90` | fast math |
| Blackwell B200 | 10.0 | 100 | `sgl_kernel/sm100` | precise math |
| A100 (Ampere) | 8.0 | 80 | `sgl_kernel/sm100` | precise math |
| 无 GPU / CPU | — | None | `sgl_kernel/sm100` | precise math |

**需要观察的现象 / 预期结果**：

- H100 → `sm90/common_ops.*.so`；Blackwell → `sm100/common_ops.*.so`。
- 若有 GPU 机器，可运行下面这段（**示例代码**，非项目自带脚本）观察 `variant_name` 与最终 `.so` 路径：

```python
# 示例代码：观察架构探测过程（需要在装好 sgl_kernel 的 GPU 机器上运行）
import logging
logging.basicConfig(level=logging.DEBUG)          # 打开 load_utils 的 debug 日志
logging.getLogger("sgl_kernel.load_utils").setLevel(logging.DEBUG)

from sgl_kernel import load_utils
cc = load_utils._get_compute_capability()
print("compute_capability =", cc)                 # H100 期望 90；B200 期望 100
```

> **关于规格里提到的 `SGLANG_KERNEL_API_LOGLEVEL`**：需要特别区分两种日志。`load_utils.py` 里那些 `[sgl_kernel] GPU Detection: ...` 是 **Python `logging.debug`**，靠上面的 `logging.getLogger(...).setLevel(logging.DEBUG)` 打开；而 `SGLANG_KERNEL_API_LOGLEVEL` 控制的是**算子调用追踪**（见 4.3.3），是另一回事。要观察「加载日志」请用 Python logging，不要误以为 `SGLANG_KERNEL_API_LOGLEVEL` 能打开加载日志。若无法在 GPU 机器上运行，结论请标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：一张 A100（cc=80）会加载 `sm90` 还是 `sm100`？为什么不加载 `sm90`？

**参考答案**：加载 `sm100`。因为选择规则是「cc 恰好等于 90 才走 sm90」，A100 的 cc=80 不满足，落入 `elif compute_capability is not None` 分支，走 `sm100` 的精确数学版本。`sm90` 版专为 Hopper 的 fast-math 优化，并不保证在 Ampere 上正确/最优。

**练习 2**：`_get_compute_capability()` 在「没有 NVIDIA 显卡」时返回什么？下游选择逻辑如何处理？

**参考答案**：返回 `None`（因为 `torch.cuda.is_available()` 为假）。下游 `if cc == 90` 不成立，`elif cc is not None` 也不成立，于是进 `else`，仍然选 `sm100`，日志里标注「CPU/No GPU detected (using precise math)」。也就是说无 GPU 时默认挑 sm100 版——当然此时 `common_ops.so` 真正运行 CUDA 算子仍需 GPU，这里的「选 sm100」只是文件选择层面的默认值。

---

### 4.3 CUDA 运行时预加载与三级回退

#### 4.3.1 概念说明

选定目录后，真正把 `.so` 加载进进程这件事并不简单——可能文件不在预期位置、可能是旧版安装布局、甚至可能压根没装好。`load_utils.py` 用一个**三级回退（fallback）**策略来应对，从「最理想」到「最兜底」依次尝试。

加载完成后还有第二个问题：某些环境下，下游库去 `dlopen` CUDA 相关库时会找不到 `libcudart.so.12`（或 `.13`）。`_preload_cuda_library` 用 `ctypes` 提前把它以 `RTLD_GLOBAL` 模式载入，从而把 CUDA 运行时符号放进全局符号表，让「之后」加载的库都能解析到。

#### 4.3.2 核心流程

**三级回退加载**（`_load_architecture_specific_ops`）：

```text
第 1 级：架构专属目录  <sgl_kernel_dir>/<sm90|sm100>/common_ops.*
第 2 级：扁平回退目录  <sgl_kernel_dir>/common_ops.*            （旧版安装布局）
第 3 级：标准 Python 导入  import common_ops                    （向后兼容）
三级都失败 → raise ImportError（带 CUDA 版本相关的安装提示）
```

每一级内部还先用 `_filter_compiled_extensions` 把**编译产物**（`.so/.pyd/.dll`）排在普通文件前面，避免 glob 出来一个 `.py` 占位文件却误当成扩展加载。

**CUDA 运行时预加载**（`_preload_cuda_library`）：

```text
1. _find_cuda_home()：依次猜 CUDA_HOME / CUDA_PATH → which nvcc 反推 → /usr/local/cuda
2. 在 cuda_home/{lib,lib64} 与若干系统库目录下，
   按 [torch 的 cuda 大版本号, "13", "12"] 去重后的顺序，
   查找 libcudart.so.<版本>
3. 找到后用 ctypes.CDLL(path, mode=RTLD_GLOBAL) 载入 → 符号进入全局表
```

#### 4.3.3 源码精读

**编译产物优先**。`_filter_compiled_extensions` 把 `.so/.pyd/.dll` 排到列表前面：

[python/sgl_kernel/load_utils.py:28-45](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L28-L45) —— 这样 `matching_files[0]` 拿到的就是 `.so` 而非同名的 `.py`。

**第 1 级：架构专属目录**。用 glob 模式 `sm90/common_ops.*` 或 `sm100/common_ops.*` 查找，再用 `importlib.util` 按路径加载：

[python/sgl_kernel/load_utils.py:70-101](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L70-L101) —— 三步走：`spec_from_file_location` → `module_from_spec` → `spec.loader.exec_module`。成功就 `return common_ops`。

**第 2 级：扁平回退**。若专属目录没有，退一步到包根目录找 `common_ops.*`（兼容旧式「平铺」安装）：

[python/sgl_kernel/load_utils.py:114-148](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L114-L148) —— 与第 1 级同构，只是 `alt_pattern = sgl_kernel_dir / "common_ops.*"`。

**第 3 级：标准 import**。前两级都没有时，尝试普通的 `import common_ops`：

[python/sgl_kernel/load_utils.py:150-162](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L150-L162)。

**失败提示**。三级都失败时拼出一条带诊断信息（cc、期望 variant、CUDA 版本）和**按 CUDA 版本分流的安装命令**的报错：

[python/sgl_kernel/load_utils.py:168-197](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L168-L197) —— 若 `torch.version.cuda` 以 `"12"` 开头，提示用 `--index-url https://docs.sglang.ai/whl/cu129/` 的专属源；否则提示普通 `pip install --upgrade sglang-kernel`。这条信息对排查「装错 CUDA 版本的 wheel」非常实用。

**CUDA 运行时预加载**。`_find_cuda_home` 用三段猜测定位 CUDA 安装路径：

[python/sgl_kernel/load_utils.py:200-213](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L200-L213) —— 注释标明这段是 copy & modify 自 `torch/utils/cpp_extension.py`。

随后 `_preload_cuda_library` 在多个候选目录、按版本优先级查找 `libcudart.so.X`，并用 `RTLD_GLOBAL` 载入：

[python/sgl_kernel/load_utils.py:216-247](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L216-L247) —— 版本顺序由 `lib_versions = list(dict.fromkeys([cuda_major, "13", "12"]))` 决定：先试 `torch.version.cuda` 的大版本（如 12 或 13），再补 "13"、"12"，用 `dict.fromkeys` 去重保序。注释特别提到「On CUDA 13 systems (e.g., DGX Spark), only libcudart.so.13 exists」，所以要把 13 也纳入候选。`ctypes.CDLL(..., mode=ctypes.RTLD_GLOBAL)` 是消除「libcudart 找不到」的关键：它让运行时符号全局可见。

**算子调用追踪（与加载日志区分）**。最后顺带说清 `SGLANG_KERNEL_API_LOGLEVEL` 的真实作用。它由 `debug_utils.py` 读取，决定是否给算子套一层调用日志：

[python/sgl_kernel/debug_utils.py:7-24](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/debug_utils.py#L7-L24) —— 当 `SGLANG_KERNEL_API_LOGLEVEL` 不为 `"0"` 时，尝试 `from sglang.kernel_api_logging import debug_kernel_api`（这是 **sglang 主包**提供的调试工具，不是 sgl_kernel 自带），用它包裹算子函数；若该模块不可导入则原样返回不包。所以这个环境变量打开的是「**算子被调用时**的 API 日志」，与 4.2 里观察加载过程的 Python logging 是两套独立机制。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍三级回退的「失败 → 提示」路径，理解安装提示如何随 CUDA 版本变化。

**操作步骤**（源码阅读型，可在任意机器上完成）：

1. 读 [python/sgl_kernel/load_utils.py:168-197](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L168-L197)。
2. 设想两个场景，分别写出 `install_hint` 会是什么：
   - 场景 A：`torch.version.cuda == "12.9.1"`
   - 场景 B：`torch.version.cuda == "13.0.0"`

**需要观察的现象 / 预期结果**：

- 场景 A（CUDA 12.x）：`cuda_version.startswith("12")` 为真 → 提示 `pip install sglang-kernel --index-url https://docs.sglang.ai/whl/cu129/`。
- 场景 B（CUDA 13.x）：不以 `"12"` 开头 → 提示 `pip install --upgrade sglang-kernel`。

**进阶（可选，需 GPU 机器）**：用 `SGLANG_KERNEL_API_LOGLEVEL` 观察算子调用追踪（注意它需要 sglang 主包可导入）。下面的**示例代码**展示两套日志机制的区别：

```python
# 示例代码：对比两种日志机制
import os, logging
# (1) 加载日志：开 Python logging
logging.basicConfig(level=logging.DEBUG)
# (2) 算子调用追踪：开环境变量（需 sglang.kernel_api_logging 可导入）
os.environ["SGLANG_KERNEL_API_LOGLEVEL"] = "1"

import torch, sgl_kernel
x = torch.randn(4, 8, dtype=torch.float16, device="cuda")
w = torch.ones(8, dtype=torch.float16, device="cuda")
sgl_kernel.rmsnorm(x, w)     # 若 sglang 主包在位，这里会打印算子调用 API 日志
```

> 预期：(1) 的 DEBUG 输出来自 `sgl_kernel.load_utils`，描述 `.so` 加载过程；(2) 的输出（若生效）来自 `sglang.kernel_api_logging`，描述「`rmsnorm` 被调用了一次」。二者来源不同。若环境不具备（无 GPU 或无 sglang 主包），相关现象请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：三级回退里，为什么第 1 级失败后不直接报错，而要继续试第 2、3 级？

**参考答案**：为了兼容不同的安装/打包布局。新版 wheel 把 `common_ops` 放在 `sm90/`、`sm100/` 子目录（第 1 级命中）；但旧版安装或自定义打包可能把它平铺在包根目录（第 2 级），或干脆作为一个独立可导入模块 `common_ops`（第 3 级）。逐级回退能在尽量多的环境下「尽量加载成功」，只有三级全失败才报错，提升健壮性。

**练习 2**：`_preload_cuda_library` 里 `list(dict.fromkeys([cuda_major, "13", "12"]))` 这一句的作用是什么？为什么要在 `torch.version.cuda` 的大版本之外还加上 `"13"` 和 `"12"`？

**参考答案**：`dict.fromkeys` 用来**去重并保持插入顺序**，生成一个候选版本列表，优先用 `torch.version.cuda` 的大版本（与当前 PyTorch 匹配的 CUDA），再补 `"13"` 和 `"12"`。补这两个是因为系统上实际存在的运行时文件名可能是 `libcudart.so.13`（如 CUDA 13 / DGX Spark）或 `libcudart.so.12`（CUDA 12）。把它们都纳入候选，可以在「torch 报告的版本号」与「磁盘上实际文件名」不完全一致时仍能找到并预载运行时。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出 `import sgl_kernel` 在 **「Linux + CUDA + H100」** 环境下的完整时序图，并标注每一步对应的源码行号。

**要求**：

1. 从 [python/sgl_kernel/__init__.py:4](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L4-L4) 开始，到算子被 re-export（如 `rmsnorm`）结束。
2. 在图里至少标出以下五个关键节点，并各写一行中文说明：
   - 平台判定（为何不走 macOS 分支）；
   - `_get_compute_capability()` 返回 90；
   - 选择 `sm90` 子目录（第 1 级回退命中）；
   - `_preload_cuda_library()` 以 `RTLD_GLOBAL` 载入 `libcudart.so`；
   - `rmsnorm` 从 `elementwise.py` 被 re-export 到包顶层。
3. 额外思考：如果把这台机器换成 **Blackwell B200**，时序图里哪两处会改变？换成 **macOS arm64** 又是哪一步彻底分叉？

**参考答案要点**：

- 黑盒时序大致为：`__version__`（L4）→ 平台 `else` 分支（L8 不成立）→ `import torch`（L11）→ `_load_architecture_specific_ops()`（L19）内部：`_get_compute_capability()` 得 90（load_utils L15-25）→ 选 `sm90`（load_utils L60-62）→ glob `sm90/common_ops.*`（load_utils L72）→ 第 1 级命中并 `exec_module`（load_utils L84-101）→ 返回 `common_ops` → `_preload_cuda_library()`（__init__ L22-23，因 `torch.version.cuda` 非 None）→ 大量 `from sgl_kernel.X import ...` re-export（__init__ L25 起，含 L47 的 `rmsnorm`）。
- 换 B200：`_get_compute_capability()` 返回 100；选择变为 `sm100`（load_utils L63-65），glob 改为 `sm100/common_ops.*`。其余不变。
- 换 macOS arm64：在 __init__ L8 处条件成立，直接走 `from sgl_kernel.metal import *`（L9），**完全不执行** `import torch` / 加载 common_ops / 预载 libcudart 这整段——这是彻底分叉点。

## 6. 本讲小结

- `__init__.py` 是包入口，最顶层用 `sys.platform == "darwin" and platform.machine() == "arm64"` 把 macOS（仅 Metal）与其余平台分流；macOS 分支连 `import torch` 都不执行。
- CUDA/ROCm/MUSA 通用分支按「加载算子库 → 预加载 CUDA 运行时 → re-export 算子」顺序初始化，并用 `torch.version.hip` / `musa` 条件增补后端专属算子。
- `_get_compute_capability()` 用 \( \text{cc} = \text{major}\times 10 + \text{minor} \) 折算；**只有 cc==90（H100）走 sm90（fast math），其余 NVIDIA 卡与无 GPU 情况都走 sm100（precise math）**，对上 CMake 产出的两个安装目录。
- `_load_architecture_specific_ops` 采用**三级回退**：架构专属目录 → 扁平回退 → 标准 `import`，并用 `_filter_compiled_extensions` 把 `.so` 排在 `.py` 前；全失败时按 CUDA 版本给出不同的安装提示。
- `_preload_cuda_library` 用 `ctypes.CDLL(..., RTLD_GLOBAL)` 提前载入 `libcudart.so.X`，让后续 `dlopen` 的 CUDA 库能解析到运行时符号，消除「`libcudart.so.12 not found`」类报错。
- `SGLANG_KERNEL_API_LOGLEVEL`（由 `debug_utils.py` 读取，依赖 sglang 主包的 `kernel_api_logging`）控制的是**算子调用追踪**，与 `load_utils.py` 里靠 Python logging 打开的**加载日志**是两套独立机制，不要混淆。

## 7. 下一步学习建议

本讲只把「Python 这一端的大门」打开：算子库被正确选载、CUDA 运行时被预载、算子名被 re-export 到包顶层。但 `sgl_kernel.rmsnorm(...)` 这一句**究竟是怎么落到 C++ 函数上的**？这就涉及 PyTorch 的 custom op 注册与分派机制。

下一篇 **u2-l2「torch op 分派：从 Python 调用到 C++ schema」** 将进入这扇门，讲解 `TORCH_LIBRARY_FRAGMENT(sgl_kernel)` 如何用 `m.def(schema)` + `m.impl(device, &fn)` 注册算子、Python 包装层如何转发到 `torch.ops.sgl_kernel.<name>.default`，以及 schema 里 `Tensor!` 的可变语义。建议继续阅读：

- [python/sgl_kernel/elementwise.py](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py) 中的 `rmsnorm` 包装，体会「re-export 的名字」与「`torch.ops` 调用」的衔接。
- [csrc/common_extension.cc](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc) 中 `rmsnorm` 的 `m.def` / `m.impl`，为下一篇做预热。
