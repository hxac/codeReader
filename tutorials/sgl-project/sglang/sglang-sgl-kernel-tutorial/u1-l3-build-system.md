# 构建系统：CMake + scikit-build + 多扩展产物

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们知道了 sgl-kernel 是什么、目录怎么分、算子怎么从 CUDA 走到 Python。但一直有个问题被悬置：流水线第 6 步「构建」到底做了什么？更具体地：

> **「我在 `CMakeLists.txt` 里看到同一份 `.cu` 源码被编译了两次，分别叫 `common_ops_sm90_build` 和 `common_ops_sm100_build`，为什么？这两个 `.so` 最后分别落到 wheel 的哪里？我执行 `make build` 时，CPU 和内存到底被怎样调配？」**

读完本讲，你应当能够：

1. 说清 sgl-kernel 为什么用 `scikit-build-core` 而不是 `setuptools`，以及 `pyproject.toml` 里 `[tool.scikit-build]` 关键字段把 CMake 和 wheel 串起来的机制。
2. 解释 `CMakeLists.txt` 如何把**同一份 `SOURCES` 编译成两个 `common_ops` 变体**（`sm90` 用 `-use_fast_math`、`sm100` 用精确数学），并落到不同的安装子目录。
3. 掌握 `make build`、`MAX_JOBS`、`CMAKE_BUILD_PARALLEL_LEVEL`、`SGL_KERNEL_COMPILE_THREADS` 四个资源旋钮各自的层级与作用。
4. 在源码里定位任意一个 `.cu` 文件「被哪个 target 编译、加上哪些 flag、装到哪里」。

本讲是整本手册「工程化」的底座——后面任何一篇提到「重新编译」「加新源文件」「资源不够 OOM」，根都在这里。

---

## 2. 前置知识

本讲假设你已经读过 u1-l1 和 u1-l2，记得三件事：

- sgl-kernel 的构建后端是 **`scikit-build-core`**（不是 `setuptools`），它驱动 CMake + NVCC 把 CUDA 扩展编译进 wheel；运行期要求 `torch==2.11.0`（ABI 钉版），构建期下限 `torch>=2.8.0`。
- u1-l2 的六步流水线里，第 6 步「构建」把 `.cu` 收进一个 `SOURCES` 列表再编译成扩展。
- 源码五大目录：`csrc/`（CUDA 实现）、`include/`（头文件）、`python/`（包装）、`tests/`、`benchmark/`。

你还需要几个最基础的构建概念（下面都会用源码佐证）：

- **构建后端（build backend）**：`pip` / `uv` 安装一个包时，实际负责把它「造出来」的程序。普通 Python 包用 `setuptools`，而 sgl-kernel 这种含 CUDA 编译的包用 `scikit-build-core`——它的特长是「把 CMake 当作构建后端」。
- **CMake target（构建目标）**：CMake 里一个「要被编译出来的产物」，比如一个动态库。一个 target 由「源文件列表 + 编译选项 + 链接库 + 输出位置」共同定义。
- **wheel**：Python 的二进制分发包（`.whl`），`pip install` 装的就是它。sgl-kernel 的 wheel 里主要装两样：纯 Python 代码（`python/sgl_kernel/`）和编译出来的 `.so` 扩展。
- **gencode / compute capability（算力架构）**：NVIDIA GPU 的架构代号，如 `sm_80`（A100/A10）、`sm_90`（H100/Hopper）、`sm_100`（B100/B200/Blackwell）。NVCC 用 `-gencode=arch=compute_XX,code=sm_XX` 告诉编译器「为哪种架构生成机器码」。这是本讲双产物的技术根因。

> 小提示：如果没接触过 CMake，把它理解成「一个比 Make 更高级的构建脚本生成器」即可——它根据 `CMakeLists.txt` 生成实际的编译命令。本讲只关心 `CMakeLists.txt` 里**与 sgl-kernel 行为相关的那部分**，不涉及 CMake 语法大全。

---

## 3. 本讲源码地图

本讲会反复打开下面这几个文件，建议先在编辑器里把它们打开：

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| [`pyproject.toml`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml) | 构建后端配置 + wheel 打包配置 | 看 `scikit-build-core` 如何把 CMake 接入 Python 打包 |
| [`CMakeLists.txt`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt) | 主构建脚本 | 看 `SOURCES`、双 target、gencode、第三方库拉取 |
| [`Makefile`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile) | 便捷目标 + 资源控制 | 看 `make build` 背后的 `uv build` 调用与并行度旋钮 |
| [`build.sh`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/build.sh) | Docker 化 wheel 构建脚本 | 看 CI / 发布场景下的资源控制与 ccache |
| [`cmake/utils.cmake`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/cmake/utils.cmake) | CMake 辅助宏 | 看 `clear_cuda_arches` 如何清理重复 gencode |
| [`cmake/flashmla.cmake`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/cmake/flashmla.cmake) | 独立扩展的 CMake 模块 | 看多扩展如何按需引入 |

> 一句话关系：`pyproject.toml` 指定「用 scikit-build-core」，scikit-build-core 调用 `CMakeLists.txt`，`Makefile` 和 `build.sh` 是更上层的「一键构建」入口。

---

## 4. 核心概念与源码讲解

本讲分三个最小模块：

- **4.1 scikit-build-core 与 pyproject 配置**：构建后端如何把 CMake 接入 Python 打包。
- **4.2 SOURCES 列表与 sm90/sm100 双产物**：同一份源码为何编译两次，两次的差别在哪里。
- **4.3 Makefile 目标与资源控制**：`make build` 背后的命令链与并行度旋钮。

### 4.1 scikit-build-core 与 pyproject 配置

#### 4.1.1 概念说明

普通 Python 包（纯 Python 代码）用 `setuptools` 就够了。但 sgl-kernel 这种「主体是 CUDA/C++ 代码、要被 NVCC 编译成 `.so`、再嵌进 wheel」的项目，`setuptools` 的 `setup.py` 写起来很别扭——你得手动拼 NVCC 命令、管 arch、管链接。

`scikit-build-core` 的解法是：**让 CMake 来管所有编译细节，自己只负责「把 CMake 产出的 `.so` 正确装进 wheel」**。于是构建分工变成：

- **编译**：完全交给 CMake（`CMakeLists.txt`），这正是 CUDA 工程师熟悉的工具。
- **打包**：`scikit-build-core` 负责，它知道编译产物在 `build/` 哪个目录、要 `install` 到 wheel 的哪个子目录。

这套分工的好处是：`CMakeLists.txt` 可以写得「很 CMake」（用 `Python_add_library`、`target_compile_options`、`install(TARGETS ...)`），而不必把 CMake 逻辑硬塞进 Python 的 `setup.py`。

#### 4.1.2 核心流程

`pip install sglang-kernel` 或 `make build` 时，发生的事情大致是：

```text
   pip / uv build
        │
        ▼
  [build-system]  读取 pyproject.toml，确定 build-backend = scikit_build_core.build
        │
        ▼
  scikit-build-core
   ├── 读 [tool.scikit-build] 配置（cmake.build-type, wheel.packages …）
   ├── 调用 cmake 配置阶段（configure）
   ├── 调用 cmake 构建阶段（build）   ──► 产出 .so 扩展
   └── 把 python/sgl_kernel/ + 编译出的 .so 打包成 wheel
        │
        ▼
   dist/sglang_kernel-*.whl
        │
        ▼
   pip install dist/*.whl   （make build 会自动做这步）
```

关键点：`pyproject.toml` 里有两段配置分别掌管「编译」和「打包」——`[build-system]` 决定谁来编译、`[tool.scikit-build]` 决定 CMake 怎么被调用、wheel 装什么。

#### 4.1.3 源码精读

**构建后端声明**：`pyproject.toml` 顶部这段告诉 pip「装这个包时，先用 `requires` 装好构建依赖，再调用 `scikit_build_core.build` 这个后端」：

```toml
# pyproject.toml:1-7
[build-system]
requires = [
  "scikit-build-core>=0.10",
  "torch>=2.8.0",
  "wheel",
]
build-backend = "scikit_build_core.build"
```

这一段：[pyproject.toml:L1-L7](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml#L1-L7)。注意 `torch>=2.8.0` 出现在 `requires`（构建期），而 `dependencies = []`（[pyproject.toml:L24](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml#L24)）——也就是说 **pip 安装时不会自动给你装 torch**，运行期 torch 由用户自己准备并钉到 `2.11.0`。这正是 u1-l1 强调的「ABI 钉版」在配置层的体现。

**CMake 与 wheel 的衔接配置**：`[tool.scikit-build]` 段把 CMake 接进来：

```toml
# pyproject.toml:36-42
[tool.scikit-build]
cmake.build-type = "Release"
minimum-version = "build-system.requires"

wheel.py-api = "cp310"
wheel.license-files = []
wheel.packages = ["python/sgl_kernel"]
```

这一段：[pyproject.toml:L36-L42](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml#L36-L42)。三个关键字段：

- `cmake.build-type = "Release"`：CMake 用 Release 配置（对应 `-O3` 优化）。
- `wheel.packages = ["python/sgl_kernel"]`：**wheel 里只装 `python/sgl_kernel/` 这一个目录**。注意是 `python/sgl_kernel` 而不是 `sgl_kernel`，scikit-build 会把目录名映射成包名 `sgl_kernel`，并把 CMake `install` 出来的 `.so` 也放进这个包目录。
- `wheel.py-api = "cp310"`：指定 wheel 的 Python ABI 标签（cp310），与 CMake 里的 **Stable ABI（SABI）** 机制配合（见下）。

**Stable ABI（SABI）机制**：scikit-build-core 支持用「稳定 ABI」编译扩展，这样一个 wheel 能跨多个 Python 版本使用。在 `CMakeLists.txt` 里体现为 `find_package` 多带一个组件，以及 `Python_add_library` 多带两个参数：

```cmake
# CMakeLists.txt:20
find_package(Python COMPONENTS Interpreter Development.Module ${SKBUILD_SABI_COMPONENT} REQUIRED)

# CMakeLists.txt:322（sm90 target；sm100 同理）
Python_add_library(common_ops_sm90_build MODULE USE_SABI ${SKBUILD_SABI_VERSION} WITH_SOABI ${SOURCES})
```

这一段：[CMakeLists.txt:L20](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L20) 与 [CMakeLists.txt:L322](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L322)。`${SKBUILD_SABI_COMPONENT}` / `${SKBUILD_SABI_VERSION}` 是 scikit-build-core 注入的变量，`USE_SABI` + `WITH_SOABI` 让扩展用稳定 ABI 编译，最终 `.so` 文件名带 `abi3`，对应 pyproject 里的 `wheel.py-api = "cp310"`。

> 一个易被忽略的小开关：[CMakeLists.txt:L14](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L14) 的 `set(CMAKE_SHARED_LIBRARY_PREFIX "")` 把动态库的 `lib` 前缀去掉，所以产物叫 `common_ops.*.so` 而不是 `libcommon_ops.*.so`——这正是 Python 扩展的命名约定。

**多份 pyproject 与后端变体**：仓库里其实不止一份 `pyproject.toml`，还有 `pyproject_rocm.toml`（AMD）、`pyproject_cpu.toml`（CPU）、`pyproject_musa.toml`（摩尔线程）三份变体，分别对应不同后端的发布构建（本讲主线是 CUDA 版）。版本号需要在四份文件间同步，`make update` 就是干这件事的（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：用眼睛确认「构建后端是 scikit-build-core、wheel 只装一个目录」。

**操作步骤**：

1. 打开 [pyproject.toml](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml)，确认 `[build-system]` 的 `build-backend` 字段。
2. 找到 `[project]` 段，确认 `dependencies` 的值（应为 `[]`）。
3. 找到 `[tool.scikit-build]` 段，记下 `cmake.build-type` 与 `wheel.packages` 的值。
4. 在仓库根目录执行：

   ```bash
   ls pyproject*.toml
   diff pyproject.toml pyproject_rocm.toml | head -40
   ```

**需要观察的现象**：

- `build-backend = "scikit_build_core.build"`（不是 `setuptools.build_meta`）。
- `dependencies = []`，即 pip 不会拉 torch。
- `wheel.packages = ["python/sgl_kernel"]`，只打一个目录。
- `diff` 结果显示几份 pyproject 的差异主要在 `name`、版本来源、构建后端（rocm/cpu 版可能用不同的 setup 脚本）。

**预期结果**：你能口头复述「CUDA 版用 scikit-build-core + CMakeLists.txt，wheel 只装 `python/sgl_kernel` 和编译出的 `.so`」。

> 关于「能否真跑」：`pyproject.toml` 是纯文本，步骤 1～3 不需要任何环境；步骤 4 的 `diff` 也不依赖编译。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pyproject.toml` 的 `dependencies = []` 而不是写上 `torch`？

**参考答案**：因为 sgl-kernel 要与宿主引擎（SGLang）**共用同一份 torch**，且严格钉版到 `torch==2.11.0` 以保证 ABI 一致。如果让 pip 自动装一个 torch，很可能版本不匹配，导致 `.so` 加载时符号找不到（C++ ABI 不兼容）。把 torch 排除出 `dependencies`，强制由用户/引擎自己提供正确版本，是 ABI 钉版的工程保证。

**练习 2**：`wheel.packages = ["python/sgl_kernel"]` 指向的是 `python/sgl_kernel`，但 `import` 时用的是 `sgl_kernel`。这个「目录名 → 包名」的映射由谁完成？

**参考答案**：由 scikit-build-core 完成。它把 `python/sgl_kernel/` 这个子目录当作 wheel 的根包 `sgl_kernel` 安装。所以仓库里的物理路径是 `python/sgl_kernel/`，装到 site-packages 后就是 `sgl_kernel/`。CMake `install` 出来的 `.so` 也会被放进这个包目录里（`sgl_kernel/sm90/`、`sgl_kernel/sm100/` 等，见 4.2）。

---

### 4.2 SOURCES 列表与 sm90/sm100 双产物

#### 4.2.1 概念说明

这是本讲最核心、也最容易让人困惑的一节。`CMakeLists.txt` 里有一份长长的 `SOURCES` 列表（约 60 个 `.cu`/`.cc` 文件），然后这份**同一份** `SOURCES` 被拿来定义了**两个** CMake target：

- `common_ops_sm90_build`：编译时带 `-use_fast_math`，输出到 `sm90/` 子目录。
- `common_ops_sm100_build`：编译时**不带** `-use_fast_math`（精确数学），输出到 `sm100/` 子目录。

两个 target 的最终产物都叫 `common_ops`（同一个 `OUTPUT_NAME`），但分别落在不同目录，从而装到 wheel 的 `sgl_kernel/sm90/` 与 `sgl_kernel/sm100/`。运行期 `import sgl_kernel` 时，会根据当前 GPU 的 compute capability 选择加载哪一个（这是 u2-l1 的主题，本讲只到「编译出两个」为止）。

**为什么要编两份？** 因为 sgl-kernel 同时服务 Hopper（sm90）和 Blackwell（sm100+）两代架构，而两代对数值精度的最优策略不同：Hopper 上用 `-use_fast_math`（快速近似数学，吞吐更高、精度略低但够用）通常更快；Blackwell（sm100+）为了新架构上的数值正确性选择了精确数学。用同一份源码、配不同 flag 编两份 `.so`，是「一份代码 + 多产物」的典型 CMake 套路。

#### 4.2.2 核心流程

从 `SOURCES` 到两个 `.so` 的流程：

```text
   SOURCES（约 60 个 .cu/.cc，含 vendored flashinfer/flash-attention 源）
        │
        │  ┌──────────────── 同时定义两个 target，复用同一份 SOURCES ───────────────┐
        │  │                                                                       │
        ▼  ▼                                                                       │
  common_ops_sm90_build                                          common_ops_sm100_build
   编译 flag: SGL_KERNEL_CUDA_FLAGS + (-use_fast_math)            编译 flag: SGL_KERNEL_CUDA_FLAGS（无 fast math）
   输出名:   common_ops                                          输出名:   common_ops
   输出目录: build/.../sm90/                                      输出目录: build/.../sm100/
        │                                                               │
        ▼                                                               ▼
   install → sgl_kernel/sm90/                                  install → sgl_kernel/sm100/
        │                                                               │
        └─────────────────────── 打进同一个 wheel ────────────────────────┘
                                        │
                                        ▼
            运行期：__init__.py / load_utils.py 按 compute capability 选 sm90 或 sm100（u2-l1）
```

双产物之外，`CMakeLists.txt` 还会产出另外几个**独立扩展**（不同的 target、不同的源文件列表、不参与 sm90/sm100 拆分）：

| 扩展 target | 源文件 | 产物 | 装到哪里 |
| --- | --- | --- | --- |
| `common_ops_sm90_build` / `common_ops_sm100_build` | `SOURCES`（双产物） | `common_ops.so` | `sgl_kernel/sm90/`、`sgl_kernel/sm100/` |
| `flash_ops`（可选，FA3） | `FLASH_SOURCES` | `flash_ops.so` | `sgl_kernel/` |
| `infllm_ops` | `INFLLM_FLASH_SOURCES` | `infllm_ops.so` | `sgl_kernel/` |
| `spatial_ops` | `SPATIAL_SOURCES` | `spatial_ops.so` | `sgl_kernel/` |
| `flashmla_ops` | `FlashMLA_SOURCES`（来自 `cmake/flashmla.cmake`） | `flashmla_ops.so` | `sgl_kernel/` |

「为什么有这么多独立扩展」会在 u3-l1 详讲（核心是符号隔离，比如 `flash::` 命名空间冲突）。本讲你只需要知道：`common_ops` 是主扩展，且它被进一步拆成 sm90/sm100 两份。

#### 4.2.3 源码精读

**SOURCES 列表**：所有「主扩展」的源文件都收在这里，按字母序排列，注释明确要求新增源时保持字母序：

```cmake
# CMakeLists.txt:246-248
# All source files
# NOTE: Please sort the filenames alphabetically
set(SOURCES
    "csrc/allreduce/custom_all_reduce.cu"
    "csrc/attention/cutlass_mla_kernel.cu"
    ...
```

这一段：[CMakeLists.txt:L246-L308](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L246-L308)。注意 `SOURCES` 里不只有 `csrc/` 下的文件，还包括 `${repo-flashinfer_SOURCE_DIR}/csrc/norm.cu`、`${repo-flash-attention_SOURCE_DIR}/...` 等 **vendored（内嵌拉取的）第三方源码**（[CMakeLists.txt:L300-L307](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L300-L307)）——sgl-kernel 把需要的 flashinfer/flash-attention 源直接编进自己的 `.so`，而不是链接它们的库。

**两个 target：同一份 SOURCES，不同 flag、不同输出目录**：

```cmake
# CMakeLists.txt:320-332（SM90 变体，带 fast math）
# =========================== Common SM90 Build ============================= #
Python_add_library(common_ops_sm90_build MODULE USE_SABI ${SKBUILD_SABI_VERSION} WITH_SOABI ${SOURCES})
target_compile_options(common_ops_sm90_build PRIVATE
    $<$<COMPILE_LANGUAGE:CUDA>:${SGL_KERNEL_CUDA_FLAGS} -use_fast_math>)   # ← 多了 -use_fast_math
target_include_directories(common_ops_sm90_build PRIVATE ${INCLUDES})
set_target_properties(common_ops_sm90_build PROPERTIES
    OUTPUT_NAME "common_ops"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/sm90")           # ← 输出到 sm90/

# CMakeLists.txt:334-346（SM100+ 变体，精确数学）
# =========================== Common SM100+ Build ============================= #
Python_add_library(common_ops_sm100_build MODULE USE_SABI ${SKBUILD_SABI_VERSION} WITH_SOABI ${SOURCES})
target_compile_options(common_ops_sm100_build PRIVATE
    $<$<COMPILE_LANGUAGE:CUDA>:${SGL_KERNEL_CUDA_FLAGS}>)                  # ← 没有 fast math
target_include_directories(common_ops_sm100_build PRIVATE ${INCLUDES})
set_target_properties(common_ops_sm100_build PROPERTIES
    OUTPUT_NAME "common_ops"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/sm100")          # ← 输出到 sm100/
```

这一段：[CMakeLists.txt:L320-L346](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L320-L346)。两个 target 的差别只有两处：

1. **编译选项**：sm90 多了 `-use_fast_math`，sm100 没有（注释 [L321](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L321) 写明 "fast math optimization"，[L335](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L335) 写明 "precise math"）。
2. **输出目录**：`LIBRARY_OUTPUT_DIRECTORY` 一个是 `sm90/`、一个是 `sm100/`。

而 `OUTPUT_NAME` 都设成 `"common_ops"`，所以两个产物的文件名相同（都是 `common_ops.*.so`），只靠所在目录区分——这正是「同名、不同目录」的解冲突手法。

**两个 target 的后续处理是平行对称的**：

```cmake
# CMakeLists.txt:364-365（都链接同一组库）
target_link_libraries(common_ops_sm90_build  PRIVATE ${TORCH_LIBRARIES} c10 cuda cublas cublasLt)
target_link_libraries(common_ops_sm100_build PRIVATE ${TORCH_LIBRARIES} c10 cuda cublas cublasLt)

# CMakeLists.txt:379-383（分别 install 到不同子目录）
install(TARGETS common_ops_sm90_build  LIBRARY DESTINATION sgl_kernel/sm90)
install(TARGETS common_ops_sm100_build LIBRARY DESTINATION sgl_kernel/sm100)
```

这一段：[CMakeLists.txt:L364-L383](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L364-L383)。`install(... LIBRARY DESTINATION sgl_kernel/sm90)` 就是告诉 scikit-build-core「把这份 `.so` 装进 wheel 的 `sgl_kernel/sm90/`」。最终 wheel 里会同时有 `sgl_kernel/sm90/common_ops.so` 和 `sgl_kernel/sm100/common_ops.so`。

**两个 target 共用的编译 flag（`SGL_KERNEL_CUDA_FLAGS`）**：除了 `-use_fast_math`，两者共享一大段 NVCC flag，其中最关键的是 gencode（架构目标）：

```cmake
# CMakeLists.txt:121-128（基础 gencode：始终包含 sm90）
set(SGL_KERNEL_CUDA_FLAGS
    "-DNDEBUG"
    "-DOPERATOR_NAMESPACE=sgl-kernel"
    ...
    "-gencode=arch=compute_90,code=sm_90"      # ← Hopper 基线，两个 target 都有
    ...
```

这一段：[CMakeLists.txt:L121-L128](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L121-L128)。随后根据 CUDA 版本与开关，**条件性地追加**更多 gencode（注意：是追加到 `SGL_KERNEL_CUDA_FLAGS`，对两个 target 都生效）：

- sm100+ 系列（CUDA≥12.8 时追加）：[CMakeLists.txt:L208-L232](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L208-L232)，含 `compute_100a`、`compute_120a`，CUDA≥13 再加 `compute_103a` 等。
- sm80/sm89（`ENABLE_BELOW_SM90` 开启时追加）：[CMakeLists.txt:L195-L206](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L195-L206)。

> 澄清一个常见误解：`sm90/sm100` 的「双产物」指的是 **两个 target、两个 `.so` 文件、两个安装目录**；而上面这些 `gencode` 是 **同一个 `.so` 内为多种架构都生成机器码**（一个 `.so` 能在多种 GPU 上跑）。两件事不要混。两个 `.so` 各自都包含多套架构的代码，区别只在编译时的 `-use_fast_math`。

**第三方库的拉取（FetchContent）**：`SOURCES` 里出现的 `${repo-flashinfer_SOURCE_DIR}` 等路径，来自 `FetchContent`——CMake 在配置阶段从 GitHub 下载并解压第三方库源码（不进 git，构建时现拉）。五份拉取：cutlass、fmt、triton、flashinfer、flash-attention：

```cmake
# CMakeLists.txt:50-55（cutlass，其余四份结构相同）
FetchContent_Declare(
    repo-cutlass
    URL      https://${GITHUB_ARTIFACTORY}/NVIDIA/cutlass/archive/57e3cfb47a....tar.gz
    URL_HASH SHA256=09237099a70f80bff1dc8bb80c843a674bb4fdcb46e43cc6993e711c5ca89bb5
)
FetchContent_Populate(repo-cutlass)
```

这一段：[CMakeLists.txt:L48-L87](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L48-L87)。每份都有 `URL_HASH`（SHA256 校验，保证可复现），`${GITHUB_ARTIFACTORY}` 默认是 `github.com`（[CMakeLists.txt:L17](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L17)），企业内部镜像可通过 `-DGITHUB_ARTIFACTORY=...` 覆盖（见 4.3）。cutlass 给出 GEMM/Attention 模板，flashinfer 给出 norm 等模板，flash-attention 给出 sparse FA 源，triton 提供 `triton_kernels`（Python，见末尾 install），fmt 是日志格式化库。

**其他独立扩展（点一下，详见 u3-l1）**：除双 `common_ops` 外，`CMakeLists.txt` 还定义了：

- 可选的 `flash_ops`（FA3，受 `SGL_KERNEL_ENABLE_FA3` 开关控制）：[CMakeLists.txt:L388-L477](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L388-L477)。
- `infllm_ops`（vendored InfLLM-v2 注意力，独立隔离 `flash::` 符号）：[CMakeLists.txt:L479-L539](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L479-L539)。
- `spatial_ops`（green context 的 SM 分区）：[CMakeLists.txt:L541-L550](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L541-L550)。
- `flashmla_ops`（由独立模块引入）：`include(${CMAKE_CURRENT_LIST_DIR}/cmake/flashmla.cmake)`，见 [CMakeLists.txt:L552-L553](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L552-L553)，具体定义在 [cmake/flashmla.cmake:L151-L182](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/cmake/flashmla.cmake#L151-L182)。

**`clear_cuda_arches` 辅助宏**：Torch 自带会在 `CMAKE_CUDA_FLAGS` 里塞 gencode，sgl-kernel 想完全自己掌控 gencode，于是用一个宏把它们清掉，改用自己的 `SGL_KERNEL_CUDA_FLAGS`：

```cmake
# CMakeLists.txt:45-46
find_package(Torch REQUIRED)
clear_cuda_arches(CMAKE_FLAG)
```

宏实现在 [cmake/utils.cmake:L12-L19](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/cmake/utils.cmake#L12-L19)：用正则把所有 `-gencode arch=...` 从 `CMAKE_CUDA_FLAGS` 抽走，避免和 sgl-kernel 自己加的 gencode 冲突。

#### 4.2.4 代码实践

**实践目标**：在 `CMakeLists.txt` 里亲手把两个 target 的差异「对齐」出来，并解释为什么同一份源码要出两个 `.so`。（这正是本讲义规格指定的实践任务。）

**操作步骤**：

1. 打开 [CMakeLists.txt](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt)，定位 `common_ops_sm90_build`（[L320-L332](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L320-L332)）和 `common_ops_sm100_build`（[L334-L346](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L334-L346)）。
2. 准备一张三列对照表，列头为 `差异点 | sm90_build | sm100_build`，逐行比对：
   - `target_compile_options` 里是否有 `-use_fast_math`
   - `LIBRARY_OUTPUT_DIRECTORY`
   - `install` 的 `DESTINATION`
3. 用 `grep` 印证 install 目标：

   ```bash
   grep -n "install(TARGETS common_ops" CMakeLists.txt
   grep -n "OUTPUT_NAME \"common_ops\"" CMakeLists.txt
   ```

4. **思考题**（写在笔记里）：既然两个 target 共享同一份 `SOURCES`，为什么不能用「一个 target + 运行时 if 判断」来代替「两个 target」？

**需要观察的现象**：

- 两个 `install(TARGETS ...)` 分别指向 `sgl_kernel/sm90` 和 `sgl_kernel/sm100`。
- 两处 `OUTPUT_NAME` 都是 `"common_ops"`，但 `LIBRARY_OUTPUT_DIRECTORY` 不同——这就是「同名文件靠目录区分」。
- sm90 的 `target_compile_options` 行尾有 `-use_fast_math`，sm100 没有。

**预期结果**：你能说出——同一份源码编两份，是因为 Hopper 与 Blackwell 的「精度/性能权衡」不同，编译期（而非运行期）就要定型；两个产物靠不同安装目录区分，由运行期按架构加载。

> 关于「能否真跑」：步骤 1～3 是纯文本检索，无需环境。如果你有 GPU 且想验证产物，可在构建后查看 `build/` 下是否生成 `sm90/` 和 `sm100/` 两个目录各含一个 `common_ops.*.so`（见 4.3.4）。

#### 4.2.5 小练习与答案

**练习 1**：两个 target 的 `OUTPUT_NAME` 都是 `"common_ops"`，会不会导致文件名冲突？

**参考答案**：不会，因为它们的 `LIBRARY_OUTPUT_DIRECTORY` 不同（一个 `sm90/`、一个 `sm100/`）。CMake 把产物分别写到不同目录，`install` 也分别装到 `sgl_kernel/sm90/` 与 `sgl_kernel/sm100/`。同名不同目录是 CMake 里常见的「一码多产物」解冲突手法。

**练习 2**：`-use_fast_math` 是 NVCC 的一个总开关，它会开启一组快速近似数学（如 `__sinf` 代替 `sinf`、flush-to-zero 等）。为什么 Hopper（sm90）敢用，而 Blackwell（sm100+）选择精确数学？

**参考答案**：这是工程权衡。Hopper 上 fast math 带来的吞吐提升显著，且对推理任务的精度损失通常可接受；而 Blackwell 引入了新的数值路径与精度敏感的算子（部分 FP8/FP4 流水线），为保数值正确性选择了精确数学。两者都是「为各自架构选最优策略」，所以用编译期 flag 区分，而不是运行期分支。这也是为什么不能用「一个 target + 运行时 if」替代——精度策略在编译期就定型了。

**练习 3**：`SOURCES` 列表里为什么会出现 `${repo-flashinfer_SOURCE_DIR}/csrc/norm.cu` 这种第三方源文件，而不是 `#include` 一个已编译好的 flashinfer 库？

**参考答案**：因为 sgl-kernel 把需要的少量 flashinfer/flash-attention 源**直接编译进自己的 `.so`**（vendored），而不是依赖外部 flashinfer 包。好处是：版本完全锁定（配合 `URL_HASH` 可复现）、不受用户环境是否装了 flashinfer 影响、可以按需用 `-DSGL_KERNEL_ENABLE_BF16` 之类宏裁剪模板实例。代价是编译时间变长。这是高性能算子库常见做法。

---

### 4.3 Makefile 目标与资源控制

#### 4.3.1 概念说明

`CMakeLists.txt` 定义了「要编什么、怎么编」，但平时开发时没人会手敲一长串 `cmake .. && cmake --build .`。`Makefile` 提供了一组**人类友好的快捷目标**（`make build`、`make test`、`make clean`……），背后转译成 scikit-build-core / uv / pytest 的实际命令。

本节最关键的实用知识是**资源控制**。CUDA 编译非常吃 CPU 和内存（NVCC 单个文件可能占几个 GB 内存、跑几十秒），在 CI 或小机器上极易 OOM。sgl-kernel 提供了一组**分层的并行度旋钮**，你需要知道每个旋钮作用在哪一层：

| 旋钮 | 层级 | 控制什么 |
| --- | --- | --- |
| `MAX_JOBS` | Makefile 变量（→ `CMAKE_BUILD_PARALLEL_LEVEL`） | 同时编译**多少个翻译单元**（多少个 `.cu` 并行） |
| `CMAKE_BUILD_PARALLEL_LEVEL` | CMake 构建并行度 | 同上（与 `MAX_JOBS` 等价转传） |
| `SGL_KERNEL_COMPILE_THREADS` | CMake 选项（→ NVCC `--threads=`） | **单个**翻译单元内部 NVCC 用多少线程 |

`build.sh` 则是更上层的 **Docker 化发布构建**脚本，用于在干净容器里产出可发布的 wheel，额外管 ccache 缓存、架构（x86/aarch64）、Python/CUDA 版本组合等。

#### 4.3.2 核心流程

`make build` 的命令链：

```text
make build
  ├── 依赖 install-deps  ─► pip install scikit-build-core isort black
  ├── 依赖 submodule     ─► git submodule update --init --recursive
  └── 主体：
        rm -rf dist/*
        CMAKE_BUILD_PARALLEL_LEVEL=$(MAX_JOBS) \
        uv build --wheel -Cbuild-dir=build . --no-build-isolation
          │
          └─► scikit-build-core 读 pyproject.toml
                └─► cmake configure + build（受 CMAKE_BUILD_PARALLEL_LEVEL、CMAKE_ARGS 里的
                     -DSGL_KERNEL_COMPILE_THREADS=... 控制）
                        └─► 产出 build/sm90/common_ops.so、build/sm100/common_ops.so
        pip3 install dist/*whl --force-reinstall --no-deps
```

两个并行度旋钮的叠加效果，用一个粗略的「总并发编译线程数」近似：

\[
\text{并发数} \approx \text{CMAKE\_BUILD\_PARALLEL\_LEVEL} \times \text{SGL\_KERNEL\_COMPILE\_THREADS}
\]

所以「限制内存」时，往往**两个都要调小**——只调一个可能仍 OOM。这也是 `CMakeLists.txt` 注释里那句「`--threads=32` 会让 `CMAKE_BUILD_PARALLEL_LEVEL` 失效、在低内存机器触发 OOM」的来源。

#### 4.3.3 源码精读

**Makefile 资源变量与转传**：

```makefile
# Makefile:8-12
NPROC ?= $(shell nproc 2>/dev/null || echo 1)
MAX_JOBS ?= $(NPROC)
CMAKE_BUILD_PARALLEL_LEVEL ?= $(MAX_JOBS)
UV_BUILD_DIR ?= build
CMAKE_POLICY_VERSION_MINIMUM ?= 3.5
```

这一段：[Makefile:L8-L12](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L8-L12)。默认 `MAX_JOBS = 全部 CPU 核`，再赋给 `CMAKE_BUILD_PARALLEL_LEVEL`，所以默认全速编译。README 提到的 `make build MAX_JOBS=2` 就是在这里把 `MAX_JOBS` 覆盖成 2，进而把 CMake 并行度限到 2。

**`make build` 主体**：

```makefile
# Makefile:45-52
build: install-deps submodule ## Build and install wheel package
	@rm -rf dist/* || true && \
		CMAKE_POLICY_VERSION_MINIMUM=$(CMAKE_POLICY_VERSION_MINIMUM) \
		MAX_JOBS=$(MAX_JOBS) \
		CMAKE_BUILD_PARALLEL_LEVEL=$(CMAKE_BUILD_PARALLEL_LEVEL) \
		CMAKE_ARGS="$(CMAKE_ARGS)" \
		uv build --wheel -Cbuild-dir=$(UV_BUILD_DIR) . --verbose --color=always --no-build-isolation && \
		pip3 install dist/*whl --force-reinstall --no-deps
```

这一段：[Makefile:L45-L52](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L45-L52)。关键点：

- `--no-build-isolation`：不用构建隔离，直接用当前环境的 scikit-build-core/torch（否则隔离环境会重新装一份 torch，既慢又可能版本不符）。
- `CMAKE_ARGS="$(CMAKE_ARGS)"`：把外部传进来的 CMake 选项透传给 scikit-build-core。README 的 `CMAKE_ARGS="-DSGL_KERNEL_COMPILE_THREADS=1"` 就是靠这里生效的。
- 最后 `pip3 install dist/*whl --force-reinstall --no-deps`：编完立刻装到当前环境，`--no-deps` 再次强调不动 torch。

README 给的两个限流示例完全对应这里的变量：

```bash
make build MAX_JOBS=2                                          # 只限翻译单元并行度
make build MAX_JOBS=2 CMAKE_ARGS="-DSGL_KERNEL_COMPILE_THREADS=1"  # 再把 NVCC 单文件线程也限到 1
```

对应 [README.md:L35-L45](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L35-L45)。

**`SGL_KERNEL_COMPILE_THREADS`（NVCC 单文件线程）在 CMake 侧**：

```cmake
# CMakeLists.txt:159-167
set(SGL_KERNEL_COMPILE_THREADS 32 CACHE STRING "Set compilation threads, default 32")

# When SGL_KERNEL_COMPILE_THREADS value is less than 1, set it to 1
if (NOT SGL_KERNEL_COMPILE_THREADS MATCHES "^[0-9]+$")
    message(FATAL_ERROR "SGL_KERNEL_COMPILE_THREADS must be an integer, ...")
elseif (SGL_KERNEL_COMPILE_THREADS LESS 1)
    message(STATUS "SGL_KERNEL_COMPILE_THREADS was set to a value less than 1. Using 1 instead.")
    set(SGL_KERNEL_COMPILE_THREADS 1)
endif()

# CMakeLists.txt:169-171
list(APPEND SGL_KERNEL_CUDA_FLAGS
    "--threads=${SGL_KERNEL_COMPILE_THREADS}"
)
```

这一段：[CMakeLists.txt:L159-L171](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L159-L171)。默认值 32，最小钳到 1（小于 1 自动改为 1）。它最终变成 NVCC 的 `--threads=` 参数——NVCC 在编译**单个** `.cu` 文件时，内部用这么多线程并行处理。注意上面紧挨着的一段注释（[CMakeLists.txt:L138-L142](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L138-L142)）解释了为什么把写死的 `--threads=32` 抽成可配选项：因为 `--threads` 会干扰 `CMAKE_BUILD_PARALLEL_LEVEL`，低内存机器会 OOM。

**其它常用 Makefile 目标**（一览）：

- `make help`：列出所有带 `## ` 注释的目标，见 [Makefile:L22-L24](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L22-L24)。
- `make install`：开发态 `pip install -e . --no-build-isolation`（editable），见 [Makefile:L42-L43](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L42-L43)。
- `make clean` / `make rebuild`：清理 / 清理后重建，见 [Makefile:L54-L58](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L54-L58)。
- `make test`：跑 `tests/` 下所有 `test_*.py`，见 [Makefile:L60-L61](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L60-L61)。
- `make update <new_version>`：批量改 `version.py` 和三份 pyproject 的版本号，见 [Makefile:L70-L93](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L70-L93)。这里 `FILES_TO_UPDATE` 正好印证了 4.1 说的「CUDA/ROCm/CPU/MUSA 四份 pyproject 要同步版本」。

**`build.sh`：Docker 化发布构建**：`make build` 适合本地开发，而 `build.sh` 用于在干净容器里产出可发布 wheel，关心「跨 Python×CUDA×架构」的矩阵组合与缓存复用。它的资源控制是另一套变量（`BUILD_JOBS`、`NVCC_THREADS`），最终也汇流到同一组 CMake 变量：

```bash
# build.sh:150
export CMAKE_ARGS="${CMAKE_ARGS:-} -DSGL_KERNEL_COMPILE_THREADS=${NVCC_THREADS} -DGITHUB_ARTIFACTORY=${GITHUB_ARTIFACTORY}"
```

这一行：[build.sh:L150](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/build.sh#L150)。可以看到 `NVCC_THREADS`（脚本侧变量名）最终也是以 `-DSGL_KERNEL_COMPILE_THREADS=...` 的形式喂给 CMake，和 `make build` 走的是同一条路。脚本还会针对 aarch64 自动收紧并行度（[build.sh:L138-L143](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/build.sh#L138-L143)），并启用 ccache 缓存编译结果（[build.sh:L124-L136](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/build.sh#L124-L136)）。这与 `CMakeLists.txt` 里 `ENABLE_CCACHE` 选项（[CMakeLists.txt:L90-L96](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L90-L96)）呼应——只有设了 `CCACHE_DIR` 环境变量时才真正启用。

#### 4.3.4 代码实践

**实践目标**：在限制资源的前提下走一遍 `make build`，并观察「双产物」真的出现了。（本讲义规格指定任务：`make build MAX_JOBS=2`。）

**操作步骤**：

1. 先确认前置依赖：CMake ≥ 3.31、Python ≥ 3.10、scikit-build-core、CUDA toolkit（README 的 [L22-L27](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L22-L27)）。若环境不全，可跳到「待本地验证」部分。
2. 在仓库根目录执行（限制并行度为 2，避免吃满 CPU/内存）：

   ```bash
   make build MAX_JOBS=2
   ```

3. 想进一步降低峰值内存，叠加限制 NVCC 单文件线程：

   ```bash
   make build MAX_JOBS=2 CMAKE_ARGS="-DSGL_KERNEL_COMPILE_THREADS=1"
   ```
4. 构建结束后，确认产物存在：

   ```bash
   ls -la build/sm90/ build/sm100/ 2>/dev/null
   ls dist/
   ```

**需要观察的现象**：

- 构建日志里会先看到 `fetch` / `Populate` cutlass、flashinfer、flash-attention、triton、fmt（第三方库下载）。
- 然后是 `nvcc` 大量编译命令，同时并行数明显被限制在 2（而不是吃满所有核）。
- `build/sm90/` 和 `build/sm100/` 下各有一个 `common_ops.*.so`（文件名相同，目录不同），印证 4.2 的双产物。
- `dist/` 下生成 `sglang_kernel-*.whl`。

**预期结果**：

- 在 GPU 机器上：构建成功，wheel 生成，`sm90/` 与 `sm100/` 双产物可见。
- 在无 GPU / 无 CUDA toolkit 的机器上：构建会在 `find_package(CUDAToolkit REQUIRED)` 或 NVCC 阶段失败——这是正常的，因为这是 CUDA 项目。

**待本地验证**：本讲义的写作环境无法编译 CUDA，以上现象标注为「待本地验证」。你无需在本讲义环境里真的跑通；理解命令含义与预期现象即可。如果你手头没有 GPU，可以退而用「源码阅读型验证」：

```bash
# 只读检索，无需 GPU：
grep -n "CMAKE_BUILD_PARALLEL_LEVEL" Makefile        # 看 MAX_JOBS 怎么转传
grep -n "SGL_KERNEL_COMPILE_THREADS" CMakeLists.txt   # 看 NVCC 线程旋钮
grep -n "uv build" Makefile                           # 看 build 目标主体
```

#### 4.3.5 小练习与答案

**练习 1**：`make build MAX_JOBS=2` 和 `make build MAX_JOBS=2 CMAKE_ARGS="-DSGL_KERNEL_COMPILE_THREADS=1"` 都在限资源，两者分别限制的是什么？

**参考答案**：前者把 `CMAKE_BUILD_PARALLEL_LEVEL`（即 `MAX_JOBS`）设为 2，限制**同时编译多少个 `.cu` 翻译单元**；后者在此基础上额外把 `SGL_KERNEL_COMPILE_THREADS` 设为 1，限制**单个翻译单元内部 NVCC 用多少线程**（即 NVCC `--threads=` 参数）。两者相乘近似为总并发编译线程数，所以「彻底防 OOM」通常两个都要调小。

**练习 2**：为什么 `SGL_KERNEL_COMPILE_THREADS` 默认是 32，而 `CMakeLists.txt` 有一段注释说「写死 `--threads=32` 会破坏 `CMAKE_BUILD_PARALLEL_LEVEL`」？

**参考答案**：因为 `--threads` 让单个 NVCC 进程自己起 32 个线程，这与 CMake 外层的「同时编译 N 个翻译单元」是**两层并行**。两者叠加会让真实并发数 = `CMAKE_BUILD_PARALLEL_LEVEL × 32`，远超机器承受，低内存主机就会 OOM。所以开发者把写死的 32 抽成了可配置的 `SGL_KERNEL_COMPILE_THREADS`（[CMakeLists.txt:L138-L171](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L138-L171)），让用户能按机器内存显式调小。

**练习 3**：`make build` 最后会 `pip3 install dist/*whl --force-reinstall --no-deps`。这里的 `--no-deps` 和 pyproject 里 `dependencies = []` 是不是同一件事的两个表现？

**参考答案**：是的，二者目的一致但作用点不同。`dependencies = []` 是**声明层**——包元信息里没有运行期依赖，所以即便不带 `--no-deps`，pip 也不会去装 torch。`--no-deps` 是**命令层**——安装这个 wheel 时显式跳过依赖解析，进一步保险。两者共同确保「装 sgl-kernel 不会动宿主的 torch」，维护 ABI 钉版。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份《新源文件的构建档案》小任务：

**场景**：假设你按 u1-l2 的六步流水线新增了一个算子，CUDA 实现写在 `csrc/elementwise/my_new_op.cu`。请产出一份说明，讲清这个文件从「落盘」到「装进用户 wheel、能被 `import sgl_kernel` 调用」的完整构建旅程。

**任务清单**：

1. **打包链路**：画一张从 `pip install` 到 wheel 的流程图，标注 `pyproject.toml`（scikit-build-core）、`CMakeLists.txt`（CMake build）、产物 `.so`、`wheel.packages` 各自的位置（对应 4.1）。
2. **双产物归属**：说明 `my_new_op.cu` 需要被加进 `SOURCES` 列表的哪一行附近（按字母序），加进去后它会被哪两个 target 编译，分别落到 wheel 的哪两个目录（对应 4.2）。
3. **flag 归属**：指出编译 `my_new_op.cu` 时，sm90 与 sm100 两个变体在编译选项上的唯一差别是什么，并说明它将同时为哪些 gencode 架构生成代码。
4. **资源受限构建**：为「一台只有 4 核、16GB 内存的小机器」写一条 `make build` 命令，并解释你选择 `MAX_JOBS` 与 `SGL_KERNEL_COMPILE_THREADS` 的理由（对应 4.3）。

**检查清单**（自我验证）：

- [ ] 能说出 `build-backend = "scikit_build_core.build"` 的含义。
- [ ] 能说出 `wheel.packages = ["python/sgl_kernel"]` 决定了 wheel 里装哪个目录。
- [ ] 能指出 `common_ops_sm90_build` 带 `-use_fast_math`、`common_ops_sm100_build` 不带。
- [ ] 能指出两个产物的 `install` 目标分别是 `sgl_kernel/sm90` 和 `sgl_kernel/sm100`。
- [ ] 能解释 `MAX_JOBS` 与 `SGL_KERNEL_COMPILE_THREADS` 是两个不同层级的并行度旋钮。
- [ ] 能说出 `FetchContent` 拉取了 cutlass/fmt/triton/flashinfer/flash-attention 五份第三方源。

> 提示：第 2 问的「字母序」位置——`csrc/elementwise/` 下的源在 `SOURCES` 里大致从 [CMakeLists.txt:L254](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L254) 的 `activation.cu` 开始，`my_new_op.cu`（m 开头）应排在它之后、`pos_enc.cu`（p）之前。第 4 问没有标准答案，合理即可（例如 `MAX_JOBS=2 SGL_KERNEL_COMPILE_THREADS=2` 甚至更保守）。

完成这份档案后，你就具备了「读懂 sgl-kernel 构建系统、并能在新增源文件时正确接入」的工程能力。

---

## 6. 本讲小结

- sgl-kernel 用 **`scikit-build-core`** 作构建后端（不是 setuptools）：`pyproject.toml` 的 `[build-system]` 声明后端，`[tool.scikit-build]` 把 CMake 接入打包；`wheel.packages = ["python/sgl_kernel"]` 决定 wheel 只装这一个目录，运行期 torch 由用户钉版（`dependencies = []`）。
- 核心机制是「**一份 `SOURCES`、两个 target**」：`common_ops_sm90_build`（带 `-use_fast_math`，装到 `sgl_kernel/sm90/`）与 `common_ops_sm100_build`（精确数学，装到 `sgl_kernel/sm100/`），两者 `OUTPUT_NAME` 都叫 `common_ops`，靠 `LIBRARY_OUTPUT_DIRECTORY` 区分。
- 双产物之外还有 `flash_ops`/`infllm_ops`/`spatial_ops`/`flashmla_ops` 等独立扩展（详见 u3-l1），它们与 `common_ops` 共同构成「多扩展产物」。
- 第三方源（cutlass/fmt/triton/flashinfer/flash-attention）由 `FetchContent` 在配置阶段现拉、带 SHA256 校验、直接编进 `.so`（vendored）。
- `make build` 是 `uv build --wheel` + `pip install` 的便捷封装；资源控制有两层旋钮：`MAX_JOBS`/`CMAKE_BUILD_PARALLEL_LEVEL`（翻译单元并行度）与 `SGL_KERNEL_COMPILE_THREADS`（NVCC 单文件 `--threads`），两者相乘近似总并发，防 OOM 时都要调小。
- `build.sh` 是更上层的 Docker 化发布构建（跨 Python×CUDA×架构矩阵，带 ccache），其 `NVCC_THREADS` 最终也汇流到 `-DSGL_KERNEL_COMPILE_THREADS`。

---

## 7. 下一步学习建议

本讲把「六步流水线第 6 步：构建」彻底讲透了，并交代了产物（两个 `common_ops.so`）是怎么来的。但「运行期到底加载哪一个 `.so`」还没讲。建议按以下顺序继续：

1. **u2-l1 Python 入口与架构自适应加载**：这是本讲的直接续集——看 `__init__.py` 与 `load_utils.py` 如何探测 GPU 的 compute capability，在 `sgl_kernel/sm90/common_ops.so` 与 `sgl_kernel/sm100/common_ops.so` 之间二选一加载。本讲的「双产物」是它的前置。
2. **u3-l1 多扩展拆分**：本讲末尾提到 `flash_ops`/`infllm_ops`/`spatial_ops`/`flashmla_ops` 等独立扩展，u3-l1 会解释「为什么要拆成多个 `.so`」（核心是符号隔离、可选编译、独立 gencode）。
3. **u11-l3 贡献一个新算子：端到端流程**：把本讲的「加进 `SOURCES`」与 u1-l2 的六步流水线合起来，完整走一遍「写 CUDA → 接入构建 → 测试基准」。
4. **u1-l2（回顾）**：如果你对「`SOURCES` 收录的 `.cu` 如何对应到注册与 Python 包装」还有点模糊，回去重读 u1-l2 的 4.3 节，把「构建链路」和「调用链路」在脑子里拼成一张完整的图。

> 阅读建议：本讲 + u2-l1 是「构建与加载」的一对，连读能打通「编译出双产物 → 运行期按架构选一个」的完整闭环。如果你只关心怎么贡献新算子，可跳过细节，但务必记住「新 `.cu` 要按字母序加进 `SOURCES`，会被 sm90/sm100 两个 target 各编一份」这一条。
