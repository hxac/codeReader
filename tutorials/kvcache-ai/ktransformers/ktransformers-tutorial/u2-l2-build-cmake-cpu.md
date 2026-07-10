# 构建系统与 CPU 指令集配置

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 `kt-kernel/CMakeLists.txt` 里那一长串 `option(...)`，知道 `KTRANSFORMERS_USE_CUDA`、`KTRANSFORMERS_CPU_USE_AMX` 等开关分别控制什么。
- 理解从「敲下 `./install.sh`」到「CMake 真正开始编译」之间的三层翻译链路：`install.sh`（shell）→ `setup.py`（Python）→ `CMakeLists.txt` + `cmake/DetectCPU.cmake`（CMake）。
- 掌握 `CPUINFER_CPU_INSTRUCT`（`NATIVE` / `AVX512` / `AVX2` / `FANCY`）和 `CPUINFER_ENABLE_*` 系列环境变量如何映射成 `-D` 选项，以及「多变体构建」如何在一个 wheel 里塞进 6 份针对不同 CPU 代际的 `.so`。
- 看懂 `DetectCPU.cmake` 如何读取 `/proc/cpuinfo` 自动探测指令集，并在 `setup.py` 已经「替它决定」时主动让位。
- 能够手动配置一次面向分发的可移植构建（例如 `AVX2 + AMX=OFF`），并能解释自己设的每一个环境变量的作用。

## 2. 前置知识

在进入源码之前，先用大白话建立三个直觉。

### 2.1 为什么要让用户「选指令集」

CPU 每隔几年就会新增一批指令。比如：

- **AVX2**（2013 年，Haswell 起）：256 位向量运算，几乎所有现代 x86 都有。
- **AVX-512**（2017 年服务器起，Skylake-X）：512 位向量，又分很多子集（`F`/`BW`/`DQ`/`VL`/`VNNI`/`BF16`/`VBMI` …）。
- **AMX**（2023 年，Intel Sapphire Rapids 起）：专门做矩阵乘法的「tile」指令，对 INT8/BF16 的 MoE 推理加速极明显。

用越新的指令编译，在本机跑得越快；但二进制里一旦包含了 AMX 指令，拿到没有 AMX 的老 CPU 上就会直接「非法指令」崩溃。所以构建系统必须让用户在「本机最快」和「到处能跑」之间做取舍——这正是 `CPUINFER_CPU_INSTRUCT` 要解决的问题。

### 2.2 kt-kernel 的算子是 C++/CUDA，但要被 Python 调用

`kt-kernel` 把高性能算子写成 C++（CPU 侧）和 CUDA（GPU 侧），再用 **pybind11** 把它们编译成一个 Python 扩展模块（`.so` 文件），Python 里 `import kt_kernel` 时加载它。因此「构建」本质上是：用 CMake 编译 C++/CUDA → 产出 `_kt_kernel_ext.*.so` → 打包进 wheel。`setup.py` 就是负责把 CMake 这一步嵌进 `pip install` 流程的胶水。

### 2.3 NUMA、hwloc、numa 库

多插槽服务器有多个 NUMA 节点，跨节点访存很慢。kt-kernel 的线程池要感知 NUMA，所以构建时**强制依赖** `libnuma`（NUMA 绑定库）和 `libhwloc`（硬件拓扑探测库）。这一点在 CMake 里是硬要求，缺了会直接 `FATAL_ERROR`。

> 关键术语：**CMake option**（构建开关）、**ARCH_FLAGS**（传给编译器的 `-mavx512f` 之类架构标志）、**多变体**（multi-variant，同一个 wheel 里多份 `.so`）、**pybind11**（C++↔Python 桥）、**NUMA**（非一致内存访问）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [kt-kernel/CMakeLists.txt](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt) | 构建主脚本：声明所有 `option()`、探测 CPU、收集源码、链接库、产出 pybind 模块。 |
| [kt-kernel/cmake/DetectCPU.cmake](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cmake/DetectCPU.cmake) | CMake 侧的指令集自动探测：读 `/proc/cpuinfo`，判断 AVX2/AVX512 各子集/AMX 是否可用。 |
| [kt-kernel/setup.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py) | Python 打包脚本：把 `CPUINFER_*` 环境变量翻译成 `-D` CMake 参数，支持单变体与多变体构建。 |
| [kt-kernel/install.sh](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh) | 一键安装入口：自动探测模式与 `--manual` 手动模式，最终调用 `pip install .` 触发 `setup.py`。 |
| [kt-kernel/CMakePresets.json](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakePresets.json) | 预设的 4 套 CMake 配置（`avx512`/`avx`/`amx`/`amd`），供直接用 `cmake --preset` 时参考。 |
| [kt-kernel/README.md](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md) | 官方构建文档，给出 `CPUINFER_*` 变量的取值表与示例。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**CMake 选项**、**指令集检测**、**环境变量**。三者由一条翻译链路串起来：

```
install.sh (shell)   ──export CPUINFER_*──▶  setup.py (Python)
                                                     │
                                  翻译成 -D 参数 + 自检 CPU
                                                     ▼
                                            CMakeLists.txt  ──include──▶  DetectCPU.cmake
                                                     │
                                            编译 ARCH_FLAGS → 产出 .so
```

### 4.1 CMake 构建选项全景

#### 4.1.1 概念说明

`CMakeLists.txt` 顶部用一串 `option(NAME "描述" 默认值)` 声明了所有构建开关。这些开关分成四类：

1. **指令集类**（`LLAMA_NATIVE` / `LLAMA_AVX2` / `LLAMA_AVX512*` …）：决定编译器要不要加 `-mavx512f` 这类标志。名字带 `LLAMA_` 是历史遗留——这套检测逻辑最早来自 llama.cpp。
2. **GPU 后端类**（`KTRANSFORMERS_USE_CUDA` / `_ROCM` / `_MUSA` / `_MACA`）：四选一（互斥），决定编译哪条 GPU 路径。
3. **CPU 算子后端类**（`KTRANSFORMERS_CPU_USE_AMX` / `_USE_AMX_AVX512` / `_USE_KML` / `_MOE_KERNEL` / `_MOE_AMD`）：决定编译哪些 CPU 算子目录、接入哪个矩阵库。
4. **工程类**（`USE_CONDA_TOOLCHAIN` / `CPUINFER_ENABLE_LTO` / `KTRANSFORMERS_ENABLE_CPPTRACE` / `KTRANSFORMERS_CUDA_STATIC_RUNTIME`）：编译器选择、链接期优化、崩溃栈追溯、CUDA 运行时静态/动态链接。

理解这些开关的关键是：**它们最终只做两件事**——往 `ARCH_FLAGS` 里追加编译器标志，或往 `.so` 上链接额外的库。

#### 4.1.2 核心流程

CMake 配置阶段（`cmake ..`）的执行顺序大致是：

1. 声明所有 `option()`（含默认值）。
2. **早探测**：若没有用 `LLAMA_NATIVE` 且不是 MSVC，就 `include(cmake/DetectCPU.cmake)` 自动探测指令集（见 4.2）。
3. 选择编译器（系统 GCC 或 conda 工具链）。
4. 判定架构（ARM / x86 / PowerPC），按架构和开关往 `ARCH_FLAGS` 塞 `-m...` 标志。
5. 处理 GPU 后端（如 CUDA：`find_package(CUDAToolkit)`、`enable_language(CUDA)`、设架构 `80;86;89;90`）。
6. 用 `aux_source_directory` 收集各算子目录的源码。
7. `pybind11_add_module(...)` 产生扩展模块，链接 `llama`、`OpenMP`、`HWLOC`、`numa` 等。

#### 4.1.3 源码精读

**所有 CPU/GPU 开关的声明**（kt-kernel/CMakeLists.txt 顶部）：

[kt-kernel/CMakeLists.txt:16-30](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L16-L30) —— 这里集中声明了 `KTRANSFORMERS_USE_CUDA`（是否编译 GPU 路径）、`KTRANSFORMERS_CPU_USE_AMX`（是否启用 AMX 算子）、`KTRANSFORMERS_CPU_USE_KML`（ARM 上用华为 KML 矩阵库）、`KTRANSFORMERS_CPU_MOE_KERNEL` / `_MOE_AMD`（通用 MoE kernel 与 AMD BLIS 路径）、`CPUINFER_ENABLE_LTO`（链接期优化）等。注意它们**默认都是 `OFF`**——是否启用由 `setup.py` 根据环境变量或探测结果来「点亮」。

**早探测的触发条件**：

[kt-kernel/CMakeLists.txt:34-37](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L34-L37) —— 只要 `LLAMA_NATIVE=OFF` 且不是 MSVC，就引入 `DetectCPU.cmake`。这是「自动探测」与「全权交给 `-march=native`」的分水岭。

**AMX / AVX512 算子的标志注入**：

[kt-kernel/CMakeLists.txt:377-410](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L377-L410) —— 关键逻辑：
- `KTRANSFORMERS_CPU_USE_AMX_AVX512` 是个「伞形开关」，打开后会定义宏 `USE_AMX_AVX_KERNEL=1`，告诉 C++ 代码「编译 AMX/AVX512 版本的算子」。
- 在伞形开关内部，若进一步 `KTRANSFORMERS_CPU_USE_AMX=ON`，则定义 `HAVE_AMX=1` 并追加 `-mamx-tile -mamx-bf16 -mamx-int8` 三条编译标志。
- 无条件追加 `-mavx512vl`（因为 AMX/SFT kernel 用到 256 位 AVX512 掩码访存，GCC 需要 AVX512VL）。
- 末尾打印一行 `AVX512 extensions: F=... BF16=... VNNI=... VBMI=...`，正是排查构建问题时最该看的一行日志。

**CUDA 后端的启用**：

[kt-kernel/CMakeLists.txt:421-452](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L421-L452) —— `KTRANSFORMERS_USE_CUDA=ON` 时：`find_package(CUDAToolkit REQUIRED)`（找不到就报错）、`enable_language(CUDA)`、默认目标架构 `80;86;89;90`（Ampere/Ada/Hopper）、加 `-O3 --use_fast_math`。注意这里的「静态 CUDA 运行时」默认开启（`KTRANSFORMERS_CUDA_STATIC_RUNTIME=ON`），这正是上一讲（u2-l1）提到的「装了官方 wheel 就不需要 CUDA toolkit」的根因。

**硬依赖：numa 库**：

[kt-kernel/CMakeLists.txt:753-759](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L753-L759) —— `find_library(NUMA_LIBRARY numa)` 找不到就直接 `FATAL_ERROR`，提示 `sudo apt install libnuma-dev`。NUMA 感知是 kt-kernel 多 socket 性能的基础，不可省略。`hwloc` 同样是 `REQUIRED`（第 615 行）。

#### 4.1.4 代码实践

**实践目标**：在不真正编译的前提下，验证「CMake 选项 → 编译器标志」的映射。

**操作步骤**：

1. 打开 [kt-kernel/CMakeLists.txt:377-410](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L377-L410)。
2. 手动模拟两种配置，推演 `ARCH_FLAGS` 的内容：
   - 配置 A：`KTRANSFORMERS_CPU_USE_AMX_AVX512=ON` 且 `KTRANSFORMERS_CPU_USE_AMX=ON`。
   - 配置 B：`KTRANSFORMERS_CPU_USE_AMX_AVX512=ON` 且 `KTRANSFORMERS_CPU_USE_AMX=OFF`。
3. 真正跑一次「只配置不编译」来对照（在 `kt-kernel/` 目录下）：

   ```bash
   mkdir -p build && cd build
   cmake .. -DKTRANSFORMERS_CPU_USE_AMX_AVX512=ON -DKTRANSFORMERS_CPU_USE_AMX=ON \
            -DLLAMA_AVX512=ON -DLLAMA_AVX512_BF16=ON -DLLAMA_AVX512_VNNI=ON \
            -DKTRANSFORMERS_USE_CUDA=OFF
   ```

**需要观察的现象**：CMake 配置日志里出现 `AMX enabled`、`ARCH_FLAGS: ... -mamx-tile -mamx-bf16 -mamx-int8 -mavx512vl ...`。

**预期结果**：
- 配置 A 的 `ARCH_FLAGS` 应包含 `-mamx-tile -mamx-bf16 -mamx-int8 -mavx512vl -mf16c`，且日志含 `HAVE_AMX=1` 对应的 `AMX enabled`。
- 配置 B 不应包含 `-mamx-*` 三件套，但仍含 `-mavx512vl -mf16c`。

> 若本机没有 AMX，配置 A 仍能「配置通过」（CMake 不校验 CPU 是否真支持，只管加标志），但要等「编译/运行」才会暴露问题。这一步本身**待本地验证**实际日志输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KTRANSFORMERS_CPU_USE_AMX_AVX512` 被称为「伞形开关」，它和 `KTRANSFORMERS_CPU_USE_AMX` 是什么关系？

**参考答案**：伞形开关 `KTRANSFORMERS_CPU_USE_AMX_AVX512` 控制是否编译「AMX 或 AVX512 版本」的算子（定义 `USE_AMX_AVX_KERNEL=1`）；它内部的 `KTRANSFORMERS_CPU_USE_AMX` 进一步决定到底用不用 AMX 指令（定义 `HAVE_AMX=1` 并加 `-mamx-*`）。换句话说：伞形开关管「要不要这套算子」，AMX 子开关管「这套算子里要不要 AMX 那条快路径」。

**练习 2**：`KTRANSFORMERS_CUDA_STATIC_RUNTIME=ON` 对最终用户有什么实际好处？

**参考答案**：它把 CUDA 运行时静态链接进 `.so`，于是用户安装 wheel 后**不需要本机装 CUDA toolkit** 也能跑 GPU 路径（只要机器有驱动和 GPU）。这正是官方 PyPI wheel「无 toolkit 即可用」的来源。

---

### 4.2 CPU 指令集自动检测

#### 4.2.1 概念说明

「自动检测」出现在**两个层面**，读者很容易混淆，要分清：

- **shell 层**（`install.sh` 里的 `detect_cpu_features()`）：在调用 `pip` 之前，先用 `grep` 扫 `/proc/cpuinfo`，决定要不要 `export CPUINFER_ENABLE_AMX=ON` 等。
- **CMake 层**（`cmake/DetectCPU.cmake`）：在 `cmake` 配置时再扫一次 `/proc/cpuinfo`，把 `LLAMA_AVX2`、`LLAMA_AVX512*`、`KTRANSFORMERS_CPU_USE_AMX` 等开关按探测结果「自动点亮」。

两层都扫同一个文件，看起来重复，其实有分工：shell 层负责「面向用户的便捷默认」，CMake 层负责「当用户直接跑 `cmake`（绕过 install.sh）时也有合理默认」。更重要的是，**CMake 层会主动让位**——如果它发现 shell/setup.py 已经把 `LLAMA_AVX512_*` 设好了，就跳过自动探测，避免覆盖用户意图。

#### 4.2.2 核心流程

`DetectCPU.cmake` 的逻辑分三段：

1. **定义 `detect_cpu_features()` 函数**：读 `/proc/cpuinfo` 的 `flags` 行，逐个判断 `avx2`、`avx512f`、`avx512_vnni`、`avx512_bf16`、`avx512_vbmi` 是否存在；对 AMX 额外要求 `amx_tile`、`amx_int8`、`amx_bf16` **三者同时**存在。
2. **「是否已被前置设置」判断**：若 `LLAMA_AVX512_VNNI` / `_BF16` / `_VBMI` 任一已被定义，说明 `setup.py` 已经下了决定，直接 `return()`，跳过本脚本的自动探测。
3. **自动点亮**：否则调用 `detect_cpu_features()`，对每个探测到的能力，用 `set(... CACHE BOOL ... FORCE)` 写入开关（仅当该开关「尚未被用户 `-D` 指定」时）。

伪代码：

```
if 已定义 LLAMA_AVX512_VNNI 或 _BF16 或 _VBMI:
    打印 "Detected configuration from install.sh/setup.py"
    return                          # 让位，不自动探测
读取 /proc/cpuinfo 的 flags
对 [avx2, avx512f, avx512_vnni, avx512_bf16, avx512_vbmi, amx(三者)] 逐项判断
对每个为真且未被 -D 指定的开关: set(开关 ON FORCE)
```

#### 4.2.3 源码精读

**AMX 需要三个标志同时存在**：

[kt-kernel/cmake/DetectCPU.cmake:49-58](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cmake/DetectCPU.cmake#L49-L58) —— 用 `AMX_COUNT` 计数 `amx_tile`/`amx_int8`/`amx_bf16` 命中数，只有 `EQUAL 3` 才认为 `HAS_AMX=ON`。因为 AMX 的 tile 数据类型同时依赖这三条扩展，少一个都用不了。注意它同时兼容下划线写法（`avx512_vnni`）和无下划线写法（`avx512vnni`），因为不同内核版本报告的 flags 格式不同。

**「让位」机制——本脚本的灵魂**：

[kt-kernel/cmake/DetectCPU.cmake:74-87](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cmake/DetectCPU.cmake#L74-L87) —— 检测 `LLAMA_AVX512_VNNI` 等是否 `DEFINED`。一旦定义，打印 `Skipping auto-detection (using install.sh settings)` 并 `return()`。这是 DetectCPU 与 setup.py 协作的关键握手：setup.py 只要传了任意一个 AVX512 子集 `-D`，DetectCPU 就把决定权完全交出去。

**自动点亮（仅当未被指定时）**：

[kt-kernel/cmake/DetectCPU.cmake:106-136](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cmake/DetectCPU.cmake#L106-L136) —— 每条都是 `if(NOT DEFINED XXX AND HAS_XXX)` 模式：只有当用户没显式指定、且硬件确实支持时，才用 `FORCE` 写入 `ON`。这种写法保证了「用户显式 `-D` 永远优先于自动探测」。注意 AMX 写入的是 `KTRANSFORMERS_CPU_USE_AMX`（第 133-136 行），而 AVX2/AVX512 写入的是 `LLAMA_*`。

**shell 侧的等价探测**：

[kt-kernel/install.sh:151-196](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh#L151-L196) —— bash 版 `detect_cpu_features()`，用 `grep -qE` 扫 `/proc/cpuinfo`，返回 5 个 `0/1`（`has_amx has_avx512f has_avx512_vnni has_avx512_bf16 has_avx512_vbmi`）。macOS 分支直接全部置 0（Apple Silicon 是 ARM，无 AMX/AVX512）。这是 `install.sh` 自动模式做决定的依据。

#### 4.2.4 代码实践

**实践目标**：用本机 `/proc/cpuinfo` 预测 `DetectCPU.cmake` 会点亮哪些开关。

**操作步骤**：

1. 查看本机 flags：

   ```bash
   grep -m1 '^flags' /proc/cpuinfo | tr ' ' '\n' | \
     grep -E 'avx2|avx512f|avx512_vnni|avx512vnni|avx512_bf16|avx512bf16|avx512_vbmi|avx512vbmi|amx_tile|amx_int8|amx_bf16'
   ```

2. 对照 [kt-kernel/cmake/DetectCPU.cmake:49-58](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cmake/DetectCPU.cmake#L49-L58) 判断：AMX 三件套是否齐全？各 AVX512 子集在不在？

**需要观察的现象**：列出命中的 flag，手写出 `HAS_AMX`、`HAS_AVX512_VNNI` 等应该是 `ON` 还是 `OFF`。

**预期结果**：
- 若 `amx_tile`、`amx_int8`、`amx_bf16` 三者都在 → `HAS_AMX=ON`；缺任何一个 → `OFF`。
- 这与 [kt-kernel/install.sh:163-165](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh#L163-L165) 的 bash 判断（只要命中其一就认为有 AMX）**判定口径不同**：CMake 要求三者齐全，shell 只看是否出现。在绝大多数真机上两者结果一致，但理论上存在差异——这是一个值得留意的细节（待本地验证差异场景）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DetectCPU.cmake` 要在脚本开头先检查 `LLAMA_AVX512_VNNI` 等是否 `DEFINED`？

**参考答案**：为了和 `setup.py`/`install.sh` 协作，避免「自动探测」覆盖「用户/上层已经显式指定的配置」。当 setup.py 已经把 AVX512 子集 `-D` 传进来时，说明决定已下达，DetectCPU 就 `return()` 不再插手；只有当用户直接 `cmake ..`、谁也没指定时，它才帮忙自动点亮。

**练习 2**：`HAS_AMX=ON` 需要哪几个 flag 同时存在？为什么不能只看一个？

**参考答案**：需要 `amx_tile`、`amx_int8`、`amx_bf16` 三个同时存在。因为 AMX 的 tile 矩阵运算依赖「tile 架构 + INT8 指令 + BF16 指令」三者合力，缺任何一个都无法完整执行 MoE 所需的 INT8/BF16 矩阵乘法，所以必须三者齐全才算可用。

---

### 4.3 CPUINFER_\* 环境变量与多变体构建

#### 4.3.1 概念说明

`CPUINFER_*` 是面向**用户**的环境变量（名字好记、取值简单，如 `NATIVE`/`AVX512`/`ON`/`OFF`），而 CMake 的 `-D` 选项是面向**构建系统**的（名字冗长、带前缀）。`setup.py` 就是这两套「词汇表」之间的翻译官：

| 用户环境变量（好记） | 翻译成的 CMake `-D`（冗长） |
|---|---|
| `CPUINFER_CPU_INSTRUCT=AVX512` | 一串 `-DLLAMA_NATIVE=OFF -DLLAMA_AVX2=ON -DLLAMA_AVX512=ON ...` |
| `CPUINFER_ENABLE_AMX=ON` | `-DKTRANSFORMERS_CPU_USE_AMX=ON` |
| `CPUINFER_ENABLE_AVX512=ON` | `-DKTRANSFORMERS_CPU_USE_AMX_AVX512=ON`（伞形开关） |
| `CPUINFER_ENABLE_AVX512_VNNI=ON` | `-DLLAMA_AVX512_VNNI=ON` |
| `CPUINFER_ENABLE_KML=ON` | `-DKTRANSFORMERS_CPU_USE_KML=ON`（ARM 矩阵库） |
| `CPUINFER_ENABLE_BLIS=ON` | `-DKTRANSFORMERS_CPU_MOE_AMD=ON`（AMD BLIS） |
| `CPUINFER_USE_CUDA=1` | `-DKTRANSFORMERS_USE_CUDA=ON` |

**多变体构建**（multi-variant）是另一层巧思：与其让用户猜目标机支持什么指令集，不如在一个 wheel 里**同时塞进 6 份 `.so`**（`avx2`/`avx512_base`/`avx512_vnni`/`avx512_vbmi`/`avx512_bf16`/`amx`），到 `import` 时再按本机能力选最合适的那份。这就是上一讲（u2-l1）说的「官方 wheel 一个包内置多个 CPU 变体」的实现。

#### 4.3.2 核心流程

单变体构建（`setup.py` 默认行为）：

1. `cpu_feature_flags()` 读 `CPUINFER_CPU_INSTRUCT`，从 `CPU_FEATURE_MAP` 查出一串 `-DLLAMA_*` 标志。
2. `detect_cpu_info()` 自检本机 CPU，得到 vendor/features 集合。
3. 对每个 `CPUINFER_ENABLE_*`：若用户设了就用 `_forward_bool_env` 转成 `-D`；否则按探测结果自动决定。
4. 拼出 `cmake_args`，调用 `cmake` 配置 + 编译，产出单个 `_kt_kernel_ext.*.so`。

多变体构建（`CPUINFER_BUILD_ALL_VARIANTS=1`）：

1. 依次循环 6 组预设的 `CPUINFER_*` 环境变量（每组对应一种 CPU 代际）。
2. 每组都跑一遍单变体逻辑，但在各自的 `build_temp` 子目录里配置。
3. 把每次产出的 `.so` 重命名成 `_kt_kernel_ext_<variant>.so`。
4. 最终 wheel 里同时包含 6 份，交由运行时 `_cpu_detect.py`（下一讲 u2-l3）挑选。

#### 4.3.3 源码精读

**`CPU_FEATURE_MAP`：指令集档位 → CMake 标志**：

[kt-kernel/setup.py:113-118](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L113-L118) —— 四个档位的精确定义。注意它们是**累进的**：`AVX2` ⊂ `AVX512` ⊂ `FANCY`（`FANCY` 比 `AVX512` 多了 `LLAMA_AVX512_FANCY_SIMD`，即 VL/BW/DQ/VNNI 全套）；而 `NATIVE` 最特殊，只设 `-DLLAMA_NATIVE=ON`，把一切交给编译器的 `-march=native`。这正是 install.sh 里「`NATIVE` = 本机最快但不可移植」的来源。

**环境变量转发函数**：

[kt-kernel/setup.py:82-89](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L82-L89) —— `_forward_bool_env`：若环境变量存在，就追加 `-D<flag>=ON/OFF` 并返回 `True`；否则返回 `False`。配合「`if not _forward_bool_env(...): 自动探测`」的写法，实现了「用户显式优先，否则自动决定」的统一模式。`setup.py` 顶部 docstring [kt-kernel/setup.py:14-42](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L14-L42) 给出了完整的「环境变量 → `-D`」对照表，是最权威的速查表。

**AMX 与 AVX512 伞形的转发**：

[kt-kernel/setup.py:577-592](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L577-L592) —— 两段关键逻辑：
- AMX：用户没设 `CPUINFER_ENABLE_AMX` 时，若探测到本机有 AMX 就自动 `-DKTRANSFORMERS_CPU_USE_AMX=ON`。
- 伞形：用户没设 `CPUINFER_ENABLE_AVX512` 时，**只有当 `cpu_mode ∈ {NATIVE, FANCY, AVX512}` 且硬件确实有 AMX/AVX512** 才自动开启伞形；**`AVX2` 模式下故意不开伞形**（注释明确说：这样 RAWINT4/K2 kernel 不会被编译）。这是「档位」与「算子后端」联动的核心约束。

**多变体构建的 6 组配置**：

[kt-kernel/setup.py:311-381](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L311-L381) —— 逐代际累进的 6 个变体：从最基础的 `avx2`（Haswell+，2013），逐步叠加 VNNI→VBMI→BF16，直到顶配 `amx`（Sapphire Rapids+，2023）。每组都精确指定「开哪些 `CPUINFER_ENABLE_AVX512_*`」，保证每个 `.so` 只含对应代际的指令。构建完会重命名为 `_kt_kernel_ext_<variant>.so`，交给运行时挑选。

**install.sh 的「分发构建对照表」**：

[kt-kernel/install.sh:43-69](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh#L43-L69) —— 把「配置 → 目标 CPU → 用途」列成表：`AVX512+AMX=OFF` 适合通用分发（2017+），`AVX2+AMX=OFF` 适合最大兼容性（2013+），`FANCY+AMX=OFF` 仅面向 Ice Lake+/Zen4+。这就是本讲综合实践要用的决策依据。

**CMakePresets.json：4 套预设**：

[kt-kernel/CMakePresets.json:8-58](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakePresets.json#L8-L58) —— 给「绕过 install.sh、直接 `cmake --preset`」的用户准备的 4 套配置：`avx512`（AMX=OFF、伞形=ON）、`avx`（纯 AVX2）、`amx`（AMX=ON、伞形=ON）、`amd`（AVX2 + AMD BLIS MoE kernel）。可看成 CMake 版的「环境变量速查表」。

#### 4.3.4 代码实践

**实践目标**：把 `CMakePresets.json` 的 4 个 preset 还原成等价的 `CPUINFER_*` 环境变量组合，验证两套词汇表确实对应。

**操作步骤**：

1. 打开 [kt-kernel/CMakePresets.json:8-58](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakePresets.json#L8-L58) 与 [kt-kernel/setup.py:14-42](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L14-L42)。
2. 对每个 preset，写出能复现它的 `CPUINFER_*` 组合。例如 `amx` preset（`KTRANSFORMERS_CPU_USE_AMX=ON` + 伞形 `ON` + CUDA `ON`）对应：

   ```bash
   export CPUINFER_CPU_INSTRUCT=AVX512   # 让伞形自动开
   export CPUINFER_ENABLE_AMX=ON         # -> -DKTRANSFORMERS_CPU_USE_AMX=ON
   export CPUINFER_ENABLE_AVX512=ON      # -> 伞形 -DKTRANSFORMERS_CPU_USE_AMX_AVX512=ON
   export CPUINFER_USE_CUDA=1            # -> -DKTRANSFORMERS_USE_CUDA=ON
   ```

3. 对照 `amd` preset（多了 `KTRANSFORMERS_CPU_MOE_AMD=ON` + `KTRANSFORMERS_CPU_MOE_KERNEL=ON`），写出它需要 `CPUINFER_ENABLE_BLIS=ON`。

**需要观察的现象**：每个 preset 都能找到一个等价的 `CPUINFER_*` 组合。

**预期结果**：四组对应关系成立。`avx` preset 是唯一不打开伞形的（`KTRANSFORMERS_CPU_USE_AMX_AVX512` 未出现），对应 `CPUINFER_CPU_INSTRUCT=AVX2` 且不设 `CPUINFER_ENABLE_AVX512`。

> 注意：preset 与环境变量并非 1:1 字面映射（preset 直接写 CMake 变量，环境变量要经 setup.py 翻译），但语义可对应。这一步**待本地验证**：可分别用 `cmake --preset amx` 和「设好环境变量后 `pip install . -v`」对比两者打印的 `-D` 参数是否一致。

#### 4.3.5 小练习与答案

**练习 1**：`CPUINFER_CPU_INSTRUCT=AVX2` 时，`setup.py` 为什么**不**自动开启 AVX512 伞形开关？

**参考答案**：见 [kt-kernel/setup.py:587-592](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L587-L592) 的注释——伞形开关会触发 RAWINT4/K2 等 AVX512 算子的编译，而 AVX2 档位的目标是「最大兼容性（2013+ 的 CPU）」，这些算子里的 AVX512 指令会让二进制在老 CPU 上崩溃。所以在 AVX2 模式下故意不开伞形，保证产物干净。

**练习 2**：多变体构建为什么需要 6 个 `.so`，而不是 1 个用 CPUID 在运行时分流？

**参考答案**：把不同指令集的算子编译进同一个 `.so`、运行时按 CPUID 选函数指针，会让 `.so` 体积膨胀、且编译期仍需让所有指令集路径都能过编译。分成 6 个独立 `.so` 后，每个只含对应代际的指令，`import` 时 `_cpu_detect.py` 按本机能力加载最优那一份，既不牺牲兼容性、也不让二进制无谓变大，还能在加载失败时沿回退链降级（详见下一讲 u2-l3）。

## 5. 综合实践

**任务**：用 `--manual` 模式配置一次「面向最大兼容性」的可分发构建：`AVX2 + AMX=OFF`，并逐条解释你设置的环境变量。

**为什么做这个**：它同时贯穿三个最小模块——用环境变量（4.3）驱动 install.sh（4.3），经 setup.py 翻译成 CMake 选项（4.1），并和 DetectCPU.cmake 的自动探测（4.2）产生交互。

**操作步骤**：

1. 进入 `kt-kernel/` 目录，先看帮助确认参数：

   ```bash
   cd kt-kernel
   ./install.sh --help
   ```

2. 设置面向分发的环境变量并构建（**先不真正全量编译，只观察配置阶段**）：

   ```bash
   export CPUINFER_CPU_INSTRUCT=AVX2     # 选 AVX2 档位：兼容 2013+ 的 CPU
   export CPUINFER_ENABLE_AMX=OFF        # 关 AMX：老 CPU 没有，避免非法指令
   export CPUINFER_BUILD_TYPE=Release    # 发布构建
   export CPUINFER_PARALLEL=8            # 8 路并行编译（按核数调整）
   ./install.sh build --manual           # --manual 跳过自动探测，用上面的手动配置
   ```

3. 在配置日志里重点核对：
   - install.sh 打印的 `Building kt-kernel with configuration:` 一栏，确认 `CPUINFER_CPU_INSTRUCT = AVX2`、`CPUINFER_ENABLE_AMX = OFF`。
   - setup.py 转发打印的 `-- Forward CPUINFER_ENABLE_AMX -> -DKTRANSFORMERS_CPU_USE_AMX=OFF`。
   - CMake 打印的 `ARCH_FLAGS: ...`，确认其中**不含** `-mamx-tile`、`-mavx512f` 等。

**逐条解释你设的变量**：

| 变量 | 值 | 作用 |
|---|---|---|
| `CPUINFER_CPU_INSTRUCT` | `AVX2` | 经 `CPU_FEATURE_MAP` 翻译成 `-DLLAMA_NATIVE=OFF -DLLAMA_FMA=ON -DLLAMA_F16C=ON -DLLAMA_AVX=ON -DLLAMA_AVX2=ON`，编译器据此加 `-mavx2 -mfma ...`，产物可在任何 AVX2 CPU（2013+）上运行。 |
| `CPUINFER_ENABLE_AMX` | `OFF` | 翻译成 `-DKTRANSFORMERS_CPU_USE_AMX=OFF`，不定义 `HAVE_AMX=1`、不加 `-mamx-*`，避免 AMX 指令混入。 |
| `CPUINFER_BUILD_TYPE` | `Release` | CMake 构建类型，启用 `-O3`。 |
| `CPUINFER_PARALLEL` | `8` | `cmake --build --parallel 8` 的并行度。 |

**需要重点观察/验证的交互（源码层面的坑）**：

在 `AVX2` 手动模式下，`setup.py` 不会设置任何 `LLAMA_AVX512_*` 子集变量。根据 [kt-kernel/cmake/DetectCPU.cmake:74-87](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cmake/DetectCPU.cmake#L74-L87) 的握手条件，此时 `FROM_INSTALL_SH=OFF`，DetectCPU **会**运行自己的自动探测。如果**构建机本身有 AVX512**，第 113 行的 `if(NOT DEFINED LLAMA_AVX512 AND HAS_AVX512F)` 会把 `LLAMA_AVX512` 强制设为 `ON`，进而 [CMakeLists.txt:275-277](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L275-L277) 会追加 `-mavx512f ...`，**导致「AVX2 分发」二进制里混入 AVX512 指令**。

**预期结果与排查**：
- 在只有 AVX2 的构建机上：`ARCH_FLAGS` 干净（仅 `-mavx2 -mfma -mf16c -msse3`），产物可分发。
- 在有 AVX512 的构建机上：`ARCH_FLAGS` 可能意外包含 `-mavx512f`。若要严格保证 AVX2 可分发，应额外显式压制：

  ```bash
  export CMAKE_ARGS="-DLLAMA_AVX512=OFF -DLLAMA_AVX512_VNNI=OFF -DLLAMA_AVX512_BF16=OFF -DLLAMA_AVX512_VBMI=OFF"
  ```

  这样这些变量被「定义」为 `OFF`，DetectCPU 的 `NOT DEFINED` 判断不成立，就不会自动点亮（同时也满足握手条件，跳过整个自动探测）。

> 本实践是否真正完成全量编译取决于本机工具链（CUDA、numa、hwloc 是否齐全）。若只关注配置逻辑，跑到「CMake 配置日志打印 `ARCH_FLAGS`」即可停止，无需等编译结束。`ARCH_FLAGS` 的实际取值**待本地验证**。

## 6. 本讲小结

- kt-kernel 的构建是一条三层翻译链：`install.sh`（shell 探测/收集 `CPUINFER_*`）→ `setup.py`（翻译成 `-D` 并自检 CPU）→ `CMakeLists.txt` + `DetectCPU.cmake`（真正编译）。
- CMake 选项分四类：指令集（`LLAMA_*`）、GPU 后端（`KTRANSFORMERS_USE_CUDA/ROCM/MUSA/MACA`，互斥）、CPU 算子（`KTRANSFORMERS_CPU_USE_AMX` 等）、工程类（LTO/静态运行时/cpptrace）。`KTRANSFORMERS_CPU_USE_AMX_AVX512` 是「伞形开关」，`KTRANSFORMERS_CPU_USE_AMX` 是其内部的 AMX 快路径。
- `DetectCPU.cmake` 读 `/proc/cpuinfo` 自动探测，但通过「检查 `LLAMA_AVX512_*` 是否已定义」实现**让位**：上层已下决定就不插手；AMX 要求 `amx_tile`+`amx_int8`+`amx_bf16` 三者齐全。
- `CPU_FEATURE_MAP` 把 `NATIVE/AVX512/AVX2/FANCY` 四个档位翻译成累进的 `-DLLAMA_*`；`AVX2` 档位故意不开 AVX512 伞形，以保证可分发性。
- 多变体构建（`CPUINFER_BUILD_ALL_VARIANTS=1`）在一个 wheel 里塞进 6 份代际累进的 `.so`，运行时再按本机能力挑选，兼顾兼容性与性能。
- NUMA（`libnuma`）和 `hwloc` 是硬依赖，缺则 `FATAL_ERROR`；静态 CUDA 运行时默认开启，让 wheel 无需用户装 toolkit。

## 7. 下一步学习建议

- 构建产物里的 6 个 `_kt_kernel_ext_<variant>.so` 是怎么在 `import` 时被挑选的？请进入下一讲 **u2-l3 运行时 CPU 变体检测与加载**，阅读 `python/_cpu_detect.py` 与 `python/__init__.py`，理解回退链与 `KT_KERNEL_CPU_VARIANT` 覆盖。
- 想看「翻译官」的完整对照表，直接读 [kt-kernel/setup.py:14-42](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L14-L42) 的 docstring，它比 README 更全。
- 后续进阶（u5/u8）会用到本讲讲过的 `KTRANSFORMERS_CPU_USE_AMX`、`KTRANSFORMERS_CPU_MOE_KERNEL` 等开关——它们决定了哪些 C++ 算子目录会被编译进 `.so`，届时可回头对照 [CMakeLists.txt:511-545](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/CMakeLists.txt#L511-L545) 的 `aux_source_directory` 收集逻辑。
