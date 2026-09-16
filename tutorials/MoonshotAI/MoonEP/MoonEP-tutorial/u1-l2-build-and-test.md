# u1-l2 构建、安装与运行测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 MoonEP 的「两层代码」结构：Python 内核层（`moonep/`）与 C++/CUDA 扩展层（`csrc/` → `moonep._C`），以及它们分别由谁编译、何时被 `import`。
2. 逐项解释 [setup.py](setup.py) 中 `nvcc_flags` 的每个编译开关为什么存在。
3. 掌握两种构建方式：`pip install -e .`（可编辑安装，推荐）与 `python setup.py build_ext --inplace`（就地构建），知道两者产出的 `.so` 落在哪里。
4. 理解 [tests/conftest.py](tests/conftest.py) 中两个 fixture 的分工：为什么「必须用 torchrun 启动」是靠检查 `RANK` 环境变量实现的，为什么每个测试结束后都要自动销毁 Buffer。
5. 在有 GPU 的机器上独立完成构建并跑通 `tests/` 下的六组测试；在没有 GPU 的机器上也能完成等价的源码走读实践。

## 2. 前置知识

本讲会用到几个你可能不熟悉的构建工具概念，先用大白话解释：

- **Python C/C++ 扩展（extension module）**：把 C/C++（这里还有 CUDA）源码编译成一个共享库（Linux 上是 `.so` 文件），Python 用 `import` 就能像普通模块一样调用里面的函数。它存在的意义是：有些东西纯 Python 做不到或太慢——MoonEP 需要直接调用 CUDA 驱动层的虚拟内存管理（VMM）接口（如 `cuMemCreate`、`cuMemMap`），这些只能用 C++ 写。
- **pybind11**：一个头文件库，让你在 C++ 里用一行 `m.def("函数名", &C++函数, "文档")` 就能把 C++ 函数暴露给 Python。[csrc/bindings.cu](csrc/bindings.cu) 整个文件的核心就是一组这样的注册语句。
- **`torch.utils.cpp_extension`**：PyTorch 官方的构建辅助库。`CUDAExtension` 描述「一个要编译的 CUDA 扩展」（源文件、头文件路径、链接库、编译选项），`BuildExtension` 是替代标准 `build_ext` 的命令类，负责自动注入 PyTorch 的头文件路径、ABI 宏（例如把扩展名 `_C` 定义成 `TORCH_EXTENSION_NAME`）等约定，让编译出的 `.so` 能安全地和当前版本的 PyTorch 共存。
- **nvcc**：NVIDIA 的 CUDA 编译器。一个 `.cu` 文件里既有给 CPU 的代码（host 部分），也有给 GPU 的代码（device 部分），nvcc 负责把两部分分开编译再链接。
- **torchrun**：PyTorch 的多进程启动器。你给它 `--nproc_per_node=8`，它就启动 8 个进程，并在每个进程里设置好 `RANK`、`WORLD_SIZE`、`LOCAL_RANK`、`MASTER_ADDR/PORT` 等环境变量，再各自执行你给的命令。注意它**不会**替你初始化进程组——`dist.init_process_group` 仍要代码自己调。
- **pytest fixture**：pytest 的「测试前后钩子」。`yield` 之前的代码在测试前运行，之后的代码在测试后运行（类似 `try/finally`）；`autouse=True` 表示对所有测试自动生效，无需显式声明。

上一讲（u1-l1）已经建立了符号系统（S/K/E/R/B/NvS/H/H′）和 MoonEP 的三大设计。本讲不涉及算法，只解决「让代码在你的机器上活起来」这一工程问题。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [setup.py](setup.py) | 唯一的构建入口：把 `csrc/bindings.cu` 编译成 `moonep._C` | `nvcc_flags`、`CUDAExtension`、依赖声明 |
| [csrc/bindings.cu](csrc/bindings.cu) | pybind11 绑定文件，C++ 扩展的全部源码 | 暴露了哪些函数给 Python |
| [csrc/nvl_shared_buffer.cuh](csrc/nvl_shared_buffer.cuh) | 被 bindings.cu 包含的实现头文件（VMM/NVLink 逻辑） | 只看它「提供什么」，细节留给 u2 单元 |
| [tests/conftest.py](tests/conftest.py) | pytest 全局配置 | 两个 fixture 的行为 |
| [tests/kernel_test_utils.py](tests/kernel_test_utils.py) | 测试公共工具 | `local_device_index`、`destroy_active_buffers` |
| [moonep/buffer.py](moonep/buffer.py) | Python 侧消费 `_C` 的地方 | 第 10 行的 `from moonep._C import ...` |
| [README.md](README.md) | 项目说明 | 「Build & Test」章节 |

另外可以确认：仓库根目录**没有** `pyproject.toml`、`Makefile`、Dockerfile 或 CI 配置——`setup.py` 是唯一的构建入口，README 的六条 `torchrun` 命令是唯一的测试入口。

## 4. 核心概念与源码讲解

### 4.1 setup 构建脚本

#### 4.1.1 概念说明

MoonEP 的代码分两层：

1. **Python 层**（`moonep/` 下的 `api.py`、`planning.py`、`dispatch.py` 等）：通信内核用 NVIDIA 的 **CuTe DSL**（`nvidia-cutlass-dsl` 这个 pip 包）以 Python 写成，由 DSL 在运行时 JIT 编译成 GPU 内核。
2. **C++/CUDA 层**（`csrc/` 下的两个文件）：负责 CuTe DSL 做不了的事——调用 CUDA 驱动 API 完成跨进程显存共享（VMM、fabric 句柄、NVSwitch 组播）。这一层要**提前编译**成 `.so`，也就是模块 `moonep._C`。

`setup.py` 做的事只有一件：定义并构建这个扩展。它的模块级文档字符串直接写明了两种使用方式——

[setup.py:1-13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L1-L13) —— 文档字符串说明：编译出的 `.so` 会落在 `moonep/_C.<abi-tag>.so`（abi-tag 是形如 `cpython-312-x86_64-linux-gnu` 的后缀），支持 `pip install -e .`（可编辑安装，推荐）和 `python setup.py build_ext --inplace`（不安装、就地构建）两条路。

#### 4.1.2 核心流程

`pip install -e .` 之后发生的事，按顺序：

```text
pip install -e .
  └─> 以可编辑模式运行 setup.py
        ├─ find_packages 找到 moonep 包
        ├─ install_requires 解析依赖 → 安装 nvidia-cutlass-dsl==4.4.2
        └─ BuildExtension 触发编译
              ├─ nvcc 编译 csrc/bindings.cu（注入 nvcc_flags + torch 头文件路径）
              ├─ 链接 -lcuda（CUDA 驱动库，cuMemCreate 等函数在里面）
              └─ 产出 moonep/_C.cpython-3xx-....so
之后任意脚本里：import moonep → moonep.buffer → from moonep._C import ...
```

`build_ext --inplace` 路径跳过安装环节，直接把 `.so` 写进源码树里的 `moonep/` 目录；此后你必须**在仓库根目录下运行 Python**，`import moonep` 才能同时找到 `.py` 源码和这个 `.so`。

#### 4.1.3 源码精读

**(a) 扩展对象的定义。**

[setup.py:50-65](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L50-L65) —— 定义名为 `moonep._C` 的 CUDAExtension：源文件只有 `csrc/bindings.cu` 一个；头文件搜索路径包含仓库的 `csrc/` 目录（这样 `#include "nvl_shared_buffer.cuh"` 才能被找到）；`libraries=["cuda"]` 表示链接 CUDA **驱动**库 `libcuda`（而不是只链接运行时库 `libcudart`）——因为代码里调用的是 `cuMemCreate`/`cuMemMap` 这类驱动 API。`extra_compile_args` 把编译选项分成 `cxx`（host 编译器）和 `nvcc`（设备编译器）两组，这是 CUDAExtension 提供的约定。

**(b) 包元数据与唯一的运行时依赖。**

[setup.py:68-77](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L68-L77) —— `setup()` 调用声明包名 `moonep`、版本 `0.0.1`、要打包的 Python 目录，以及 `install_requires=["nvidia-cutlass-dsl==4.4.2"]`。注意两点：

- 依赖里**没有** torch 和 pybind11——因为构建 CUDA 扩展时 `torch.utils.cpp_extension` 会自动使用当前环境里已安装的 PyTorch（版本必须先满足），torch 是隐式前提而非声明式依赖。
- `nvidia-cutlass-dsl` 用 `==` 精确锁到 4.4.2：Python 内核代码（u4 单元会读的 `_common.py` 等）直接依赖该版本 DSL 的 API 形态和 lowering 行为，所以不能浮动。

**(c) nvcc 编译开关逐项解读。**

[setup.py:24-41](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L24-L41) —— `nvcc_flags` 列表。逐项说明：

| 开关 | 作用 |
| --- | --- |
| `-O3` | 设备代码开最高级别优化（发布构建的标准选择） |
| `--use_fast_math` | 允许编译器用快速数学近似（如除法变乘倒数），牺牲一点精度换速度 |
| `-std=c++20` | 指定 C++ 标准；CUTLASS 风格头文件需要较新的标准 |
| `--expt-extended-lambda` | 允许在 lambda 上标注 `__device__`（扩展 lambda），模板化 GPU 代码常用 |
| `--expt-relaxed-constexpr` | 允许未标 `__device__` 的 `constexpr` 函数在设备代码中调用 |
| `-forward-unknown-to-host-compiler` | nvcc 遇到不认识的选项时不报错，而是原样转发给 host 编译器 |
| `-Xcompiler=-Wno-psabi` | 向 host 编译器传 `-Wno-psabi`，屏蔽 GCC 关于 C++ ABI 变化的提示噪音（PyTorch 扩展常见） |
| `-Xcompiler=-fno-strict-aliasing` | 关闭 host 代码的严格别名优化，是 PyTorch 官方扩展的惯用安全开关 |
| `-DNDEBUG` | 定义 `NDEBUG` 宏，关闭 `assert`，发布构建标配 |
| `-lineinfo` | 给可执行文件嵌入行号信息（不含完整调试信息），供 Nsight Compute 等剖析器把指令映射回源码行 |
| `-diag-suppress=3189` | 屏蔽 nvcc 的一条特定诊断信息（编号 3189），减少编译输出噪音 |
| `-ftemplate-backtrace-limit=0` | 模板实例化报错时打印**完整**回溯而不截断——重模板代码（CUTLASS 系）排错必需 |
| `-D__CUDA_NO_HALF_OPERATORS__` / `-D__CUDA_NO_HALF_CONVERSIONS__` / `-D__CUDA_NO_BFLOAT16_CONVERSIONS__` / `-D__CUDA_NO_HALF2_OPERATORS__` | 禁用 nvcc 自带的 half/bfloat16 隐式运算符与类型转换，避免与 PyTorch 自己的 `at::Half`/`c10::BFloat16` 类型系统冲突（PyTorch 扩展的标准防御性开关） |

**(d) host 编译器选项。**

[setup.py:43-47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L43-L47) —— `cxx_flags` 只有 `-O3`、`-fno-strict-aliasing`、`-Wno-psabi` 三项，与 nvcc_flags 里对应项含义相同，只是作用在 `.cpp`/host 部分（本项目没有独立 `.cpp`，作用于 `bindings.cu` 的 host 部分编译）。

#### 4.1.4 代码实践

**实践 A（任何机器，包括无 GPU）：逐项注释编译开关。**

1. **实践目标**：不靠记忆，写脚本从 `setup.py` 源码文本里真实提取 `nvcc_flags`，并对照上表逐项输出注释，确认你理解的开关集合与仓库实际使用的完全一致。
2. **操作步骤**：把下面的脚本存为 `explain_nvcc_flags.py`（示例代码，放在仓库根目录外任意位置均可，它只读取源码、不修改任何东西）：

   ```python
   # 示例代码：从 setup.py 源文本中提取 nvcc_flags 并逐项注释
   import re

   src = open("setup.py", encoding="utf-8").read()
   m = re.search(r"nvcc_flags\s*=\s*\[(.*?)\]", src, re.S)
   flags = re.findall(r'"([^"]+)"', m.group(1))

   NOTES = {
       "-O3": "设备代码最高级别优化",
       "--use_fast_math": "快速数学近似，牺牲精度换速度",
       "-std=c++20": "C++20 标准",
       "--expt-extended-lambda": "允许 __device__ lambda",
       "--expt-relaxed-constexpr": "constexpr 可在设备代码调用",
       "-forward-unknown-to-host-compiler": "未知选项转发给 host 编译器",
       "-Xcompiler=-Wno-psabi": "屏蔽 host 端 ABI 提示噪音",
       "-Xcompiler=-fno-strict-aliasing": "关闭 host 端严格别名优化",
       "-DNDEBUG": "关闭 assert（发布构建）",
       "-lineinfo": "嵌入行号信息供剖析器使用",
       "-diag-suppress=3189": "屏蔽 nvcc 诊断 #3189",
       "-ftemplate-backtrace-limit=0": "模板报错打印完整回溯",
       "-D__CUDA_NO_HALF_OPERATORS__": "禁用 CUDA half 运算符",
       "-D__CUDA_NO_HALF_CONVERSIONS__": "禁用 CUDA half 隐式转换",
       "-D__CUDA_NO_BFLOAT16_CONVERSIONS__": "禁用 CUDA bf16 隐式转换",
       "-D__CUDA_NO_HALF2_OPERATORS__": "禁用 half2 打包运算符",
   }

   for f in flags:
       print(f"{f:42s} # {NOTES.get(f, '<< 请补一条注释 >>')}")
   ```

3. **需要观察的现象**：输出共 16 行（与 [setup.py:24-41](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L24-L41) 的条目数一致），且没有任何 `<< 请补一条注释 >>`。
4. **预期结果**：脚本输出的开关集合与源码完全一致；如果将来仓库增删开关，脚本会以「请补一条注释」提示你跟进。
5. 本实践只读文件，可直接运行验证。

**实践 B（有 CUDA 环境时替代实践 A）：真实构建。**

1. **实践目标**：完成一次可编辑安装并确认 `.so` 生成。
2. **操作步骤**：`pip install -e .`（需要已装好与 GPU 匹配的 PyTorch，且 `nvcc` 在 PATH 中），然后 `ls moonep/*.so`。
3. **需要观察的现象**：出现形如 `moonep/_C.cpython-3xx-x86_64-linux-gnu.so` 的文件。
4. **预期结果**：`python -c "from moonep import _C; print(_C.__file__)"` 输出该 `.so` 的路径。
5. **待本地验证**（本讲义写作环境无 GPU，未实际执行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `libraries=["cuda"]` 链接的是驱动库而不是 `cudart`？
**答案**：扩展里调用的是 `cuMemCreate`、`cuMemMap`、`cuMemGetAllocationGranularity` 等 CUDA **驱动 API**（前缀 `cu`，定义在 `libcuda`），而不是运行时 API（前缀 `cuda`，定义在 `libcudart`）。这些驱动 API 是第二单元要讲的 VMM 对称内存的基础，Python/CuTe DSL 层拿不到它们，所以必须进 C++ 扩展并链接 `-lcuda`。

**练习 2**：如果把 `-D__CUDA_NO_BFLOAT16_CONVERSIONS__` 去掉，最可能出什么问题？
**答案**：nvcc 会启用自带的 bfloat16 隐式转换与运算符，与包含 `torch/extension.h` 引入的 `c10::BFloat16`/`at::BFloat16` 类型系统产生重载二义性，典型症状是编译期 ambiguous conversion 报错。这类开关是 PyTorch 扩展的惯例防御。

**练习 3**：`pip install -e .` 和 `python setup.py build_ext --inplace` 产出的 `.so` 位置有何异同？
**答案**：两者最终都把 `.so` 放在源码树的 `moonep/` 目录下（可编辑安装本来就指向源码树）。区别在于前者还会把 `moonep` 注册到当前 Python 环境（任何目录都能 `import moonep`，并自动安装 `nvidia-cutlass-dsl` 依赖），后者只是就地编译，必须在仓库根目录运行 Python 才能导入。

### 4.2 CUDA 扩展绑定

#### 4.2.1 概念说明

[csrc/bindings.cu](csrc/bindings.cu) 是 C++ 扩展的**全部**源码，但它几乎不含实现——真正的实现写在同目录的 `nvl_shared_buffer.cuh` 头文件里（这是一个 header-only 的实现文件，[csrc/nvl_shared_buffer.cuh](csrc/nvl_shared_buffer.cuh) 开头是 `#pragma once` 和错误检查宏、设备探测函数等）。bindings.cu 的职责是「翻译官」：把 C++ 函数逐一登记到 pybind11 模块 `_C` 上，让 Python 侧能按名字调用。

这个模块解决的问题是：**跨进程共享显存的底层能力（VMM 分配/映射、fabric 句柄、NVSwitch 组播）只有 CUDA 驱动 API 能提供，而 CuTe DSL 只管写内核，不管分配显存**。于是 MoonEP 用一个极小的 C++ 扩展补上这块拼图。

#### 4.2.2 核心流程

Python 侧的引用关系（谁在用 `_C`）：

```text
moonep/buffer.py:10   from moonep._C import (12 个符号)
                          │
                          ▼
moonep._C  (csrc/bindings.cu 编译产物)
                          │  #include
                          ▼
csrc/nvl_shared_buffer.cuh   (cuMemCreate / cuMemMap / 组播对象等驱动 API 封装)
```

即：整个 `moonep` 包里**只有 `buffer.py`** 直接 import `_C`；其他模块（planning、dispatch、combine……）都是纯 Python/CuTe DSL，只消费 buffer 建好的内存。这也解释了为什么 C++ 源码只需要一个 `.cu` 文件。

#### 4.2.3 源码精读

**(a) 模块入口与唯一的「纯 C++」函数。**

[csrc/bindings.cu:1-11](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L1-L11) —— 包含 `torch/extension.h`（拉入 pybind11 与 ATen）和实现头文件；`get_vmm_granularity()` 是本文件唯一直接实现的函数，返回当前设备的 VMM 分配粒度（字节数），它转调头文件里的 `nvl_granularity_max`。

**(b) PYBIND11_MODULE：全部导出清单。**

[csrc/bindings.cu:13-53](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L13-L53) —— `PYBIND11_MODULE(_C, m)` 定义名为 `_C` 的模块。可以按功能分成四组记：

| 分组 | 导出内容 | 行号 | 用途（一句话） |
| --- | --- | --- | --- |
| 常量 | `FABRIC_HANDLE_BYTES` | [L14](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L14) | fabric 句柄的固定字节数（64），Python 侧按它开缓冲区 |
| VMM 对称内存 | `nvl_dist_alloc` / `nvl_dist_map` | [L15-24](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L15-L24) | 分配本 rank 物理内存块并导出句柄；把所有 rank 的块映射成一块连续虚拟地址 |
| 能力查询 | `nvl_fabric_supported` / `get_vmm_granularity` / `get_multicast_granularity` / `nvl_multicast_supported` | [L25-33](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L25-L33) | 探测驱动/设备支持哪些共享机制 |
| 组播对象 | `nvl_multicast_create` / `nvl_multicast_import` / `nvl_multicast_add_device` / `nvl_multicast_bind_map` | [L34-49](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L34-L49) | 创建/导入 NVSwitch 组播对象并绑定物理内存（「一次写、全 rank 收」的广播基础） |
| 资源释放 | `nvl_release_mem_handle` | [L50-52](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L50-L52) | 释放 `nvl_dist_alloc` 返回的内存句柄 |

注意文档字符串本身就在告诉你协议：`nvl_dist_alloc` 的句柄「same node 用 POSIX fd，same NVLink domain 用 64 字节 fabric handle」——这是 u2-l3 的伏笔，本讲只需知道有这两种模式。

**(c) Python 侧的对应消费点。**

[moonep/buffer.py:10-23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10-L23) —— `from moonep._C import (...)` 一次性导入上表中的 12 个符号（`FABRIC_HANDLE_BYTES` 改名 `_FABRIC_HANDLE_BYTES`，其余原名使用）。这行就是「`.so` 没编译时你看到的第一个报错」的出处：`ModuleNotFoundError: No module named 'moonep._C'`。

#### 4.2.4 代码实践

1. **实践目标**：亲眼确认「未构建时 `_C` 缺失、构建后符号齐全」这两种状态，建立对扩展边界的直觉。
2. **操作步骤**：
   - 未构建（或无 GPU）环境：`python -c "from moonep._C import nvl_dist_alloc"`。
   - 已构建环境：`python -c "import moonep._C as C; print([n for n in dir(C) if n.startswith('nvl_') or 'granularity' in n or n == 'FABRIC_HANDLE_BYTES'])"`。
3. **需要观察的现象**：
   - 前者抛 `ModuleNotFoundError`，提示找不到 `moonep._C`——因为 `.so` 尚未生成；
   - 后者打印出 12 个符号名。
4. **预期结果**：打印列表与 [csrc/bindings.cu:13-53](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L13-L53) 中 `m.attr`/`m.def` 登记的名字一一对应；同时 `dir` 里还有 pybind11 自动附加的 `__doc__`、`__file__` 等属性。
5. **待本地验证**（本环境未构建扩展，无法实际执行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么把实现放进 `.cuh` 头文件而不是单独的 `.cpp`/`.cu` 再链接？
**答案**：header-only 写法让 `setup.py` 的 `sources` 只需一个文件、一次 nvcc 编译，构建配置最简（见 [setup.py:53-55](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L53-L55)）。代价是编译单元少、无法并行，但这个扩展体量很小，不值得拆分。

**练习 2**：`nvl_multicast_bind_map` 返回值为什么设计成「一个 keepalive 的 int32 张量」而不是普通整数？
**答案**：从其文档字符串看（[csrc/bindings.cu:44-49](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L44-L49)），它返回的张量 `data_ptr` 就是 multimem 地址，同时张量对象本身的生命周期被 Python 引用计数管理，起到「持有可能映射的临时资源不被提前回收」的 keepalive 作用。细节在 u2-l4 展开。

### 4.3 pytest 配置与 torchrun 测试

#### 4.3.1 概念说明

MoonEP 的测试全是**分布式多进程测试**：一个用例要同时在 R 个进程（各自绑定一张 GPU）里跑，内核行为才成立。这带来三个工程问题，[tests/conftest.py](tests/conftest.py) 用两个 fixture 分别解决：

1. **进程组怎么来**：必须由 torchrun 启动（它设置 `RANK`/`LOCAL_RANK`/`WORLD_SIZE`），代码再据此 `init_process_group`。如果有人直接 `pytest`，应该优雅跳过而不是崩溃。
2. **每个进程绑哪张卡**：`LOCAL_RANK` 区分同节点内的进程编号，用它 `torch.cuda.set_device`，8 个进程各占一张卡。
3. **测试之间怎么不互相污染**：Buffer 持有 VMM/组播等**进程级** OS 资源（不是普通显存，不是 GC 能回收的），每个测试结束必须显式销毁。

#### 4.3.2 核心流程

一次 `torchrun --nproc_per_node=8 -m pytest tests/test_planning.py` 的完整时序：

```text
torchrun 启动 8 进程，各自注入 RANK/LOCAL_RANK/WORLD_SIZE/MASTER_ADDR/PORT
每个进程独立运行 pytest，收集并执行同一批测试
  ├─ [每个测试前] autouse fixture cleanup_moonep_buffers：注册（本讲无前置动作）
  ├─ [会话内首个分布式测试] fixture dist_env：
  │     1. RANK 不在环境变量 → pytest.skip（直接 pytest 时走这条分支）
  │     2. init_process_group(backend="nccl")
  │     3. local_device_index() 取 LOCAL_RANK → torch.cuda.set_device
  │     4. yield (rank, world_size) 交给测试用例使用
  ├─ 测试体：测试文件按 rank 生成输入、调内核、与参考实现对拍
  └─ [每个测试后] cleanup_moonep_buffers：destroy_active_buffers()
        逐个销毁本测试创建的 Buffer（LIFO 弹栈），释放 VMM/组播资源
会话结束：dist_env 收尾 → barrier → destroy_process_group
```

#### 4.3.3 源码精读

**(a) 自动清理 fixture。**

[tests/conftest.py:8-13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L8-L13) —— `autouse=True` 的 `cleanup_moonep_buffers`：`yield` 前没有动作（纯 teardown 型），测试结束后调用 `destroy_active_buffers()`。它调用的工具函数在 [tests/kernel_test_utils.py:68-72](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L68-L72) —— 从模块级列表 `_ACTIVE_BUFFERS`（[tests/kernel_test_utils.py:13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L13)）里弹出一个销毁一个，已销毁的跳过。为什么必须这样做：第二单元会看到，一个 Buffer 持有 VMM 虚拟地址映射、IPC/fabric 句柄、组播对象，这些都绑定进程生命周期，跨测试累积会耗尽资源；`yield`-teardown 的写法保证测试即使断言失败也会执行清理（pytest 对 fixture teardown 的处理类似 `finally`）。

**(b) 分布式环境 fixture。**

[tests/conftest.py:16-33](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L16-L33) —— `scope="session"` 的 `dist_env`，逐行拆解：

- [tests/conftest.py:18-19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L18-L19) —— **「必须用 torchrun」的判定就这一行**：环境变量里没有 `RANK` 就 `pytest.skip`，跳过理由写得很明白（"distributed kernel tests must be launched with torchrun"）。torchrun 一定会给每个子进程设置 `RANK`，所以这是最可靠的判别器。
- [tests/conftest.py:23-24](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L23-L24) —— 尚未初始化时以 **NCCL** 后端建进程组（NCCL 是 GPU 集合通信的标配后端；MoonEP 内核自己走 NVLink 对称内存，NCCL 进程组主要服务于测试里的 barrier 与协调）。
- [tests/conftest.py:21-27](https://github.com/MoonshotAI/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L21-L27) —— `local_device_index()` 的实现在 [tests/kernel_test_utils.py:16-17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L16-L17)：优先 `LOCAL_RANK`、退回 `RANK`、再退回 0。`torch.cuda.set_device` 让本进程的默认 CUDA 上下文落在对应卡上——每个 rank 一张卡。
- [tests/conftest.py:29-33](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L29-L33) —— 收尾：先 `dist.barrier(device_ids=[device])` 让所有 rank 对齐（防止有 rank 还在用通信资源时别的进程先退出），再销毁进程组。顺序不能反。

**(c) 六组测试与硬件前提。**

[README.md:180-192](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L180-L192) —— 「Build & Test」章节给出的六条命令，对应 `tests/` 下六个测试文件：

| 命令 | 测试文件 | 验证内容 |
| --- | --- | --- |
| `torchrun --nproc_per_node=8 -m pytest tests/test_planning.py` | test_planning.py | 在线规划内核 vs PyTorch 参考实现 |
| `... tests/test_dispatch.py` | test_dispatch.py | dispatch（token 派发）内核 |
| `... tests/test_combine.py` | test_combine.py | combine（结果归并）内核 |
| `... tests/test_e2e.py` | test_e2e.py | dispatch→combine 端到端 |
| `... tests/test_grad_reduce.py` | test_grad_reduce.py | 梯度归约内核 |
| `... tests/test_prefetch.py` | test_prefetch.py | 权重预取内核 |

README 明确标注 **"requires multiple GPUs + NVLink"**：不只是多卡，卡间还要有 NVLink 互联（MoonEP 的通信内核直接依赖 NVLink 对称内存与 NVSwitch 组播，PCIe 互联的机器跑不了）。另外，每个用例由 `KernelCase` 参数化（[tests/kernel_test_utils.py:24-33](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L24-L33)），并通过 `skip_if_unsupported_world_size`（[tests/kernel_test_utils.py:75-79](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L75-L79)）按实际 rank 数跳过不满足 `min_R`/`max_R` 的用例——所以少于 8 卡也能启动，只是部分用例会被 skip。

#### 4.3.4 代码实践

1. **实践目标**：验证「torchrun 判定」确实如源码所述工作——不带 torchrun 直接 pytest 时，分布式测试应被跳过而不是失败。
2. **操作步骤**：在已完成实践 4.1.4-B（构建成功）的机器上，仓库根目录执行：

   ```bash
   python -m pytest tests/test_planning.py -v
   ```

   不带 torchrun。观察每个用例的状态行。
3. **需要观察的现象**：用例状态为 `SKIPPED`，skip 理由是 `distributed kernel tests must be launched with torchrun`（正是 [tests/conftest.py:19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L19) 的字符串）。注意：即使走 skip 分支，import 链仍会加载 `moonep`（进而加载编译好的 `moonep._C`），所以这一步的前提是扩展已构建。
4. **预期结果**：全部用例 skipped、退出码为 0（pytest 不会因 skip 判失败）。随后在多卡机器上再跑 README 原样命令 `torchrun --nproc_per_node=8 -m pytest tests/test_planning.py`，此时应显示 `passed`。
5. **待本地验证**（本讲义写作环境无 GPU，未实际执行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cleanup_moonep_buffers` 是 `autouse`，而 `dist_env` 不是？
**答案**：清理必须覆盖**每一个**测试（哪怕它没用 `dist_env`），所以 autouse 全局生效；而 `dist_env` 按需注入（测试函数签名里写 `dist_env` 参数才触发），`scope="session"` 说明整个会话只建一次进程组。两者一个管「每测必清」，一个管「会话一次」。

**练习 2**：`dist_env` 的收尾为什么先 `barrier` 再 `destroy_process_group`？
**答案**：barrier 保证所有 rank 都完成了各自最后一个测试的 GPU 工作（包括清理 fixture 触发的销毁动作）之后，才允许任何进程拆除进程组；如果先销毁，先到的进程退出可能导致后到进程的 NCCL 调用挂起或报错。

**练习 3**：`local_device_index()` 为什么要做 `LOCAL_RANK` → `RANK` → `0` 的三级回退？
**答案**：单节点多卡场景 torchrun 会同时设置 `RANK` 和 `LOCAL_RANK` 且两者相等，用 `LOCAL_RANK` 最准确；多节点场景 `RANK` 是全局编号、不能直接当设备号用（设备号必须取节点内编号），这里是对单节点 torchrun 的合理简化；裸进程（无 torchrun）时退回 0，至少让单进程场景可运行。真正的多节点测试还需要按节点屏蔽可见 GPU，本仓库测试主要面向单节点多卡。

## 5. 综合实践

**任务：写一份「构建体检报告」脚本 `build_checkup.py`（示例代码），把本讲三个模块串起来。**

要求脚本依次输出并回答：

1. **环境探测**：打印 `torch.__version__`、`torch.version.cuda`、`torch.cuda.device_count()`、`shutil.which("nvcc")`；并据此在报告末尾给出建议路径（有 GPU 且有 nvcc → 走 4.1.4-B 真实构建；缺任一 → 走 4.1.4-A 开关注释实践）。
2. **扩展状态检查**：尝试 `import moonep._C`，成功则打印 `dir(_C)` 中 `nvl_` 开头的符号数（预期 8：alloc/map/fabric_supported/multicast_×4/release），失败则捕获 `ModuleNotFoundError` 并打印「需要先构建」提示。
3. **测试入口自检**：检查 `os.environ` 中是否存在 `RANK`，据此打印「当前处于 torchrun 子进程」或「直接运行：分布式测试将因 [tests/conftest.py:18-19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L18-L19) 的判定被跳过」。
4. **编译开关报告**：内嵌实践 4.1.4-A 的解析逻辑，输出带注释的 `nvcc_flags` 表。

验收标准：

- 无 GPU 机器上运行：第 2 项给出构建提示、第 3 项给出跳过解释、第 4 项输出 16 行完整注释——全程不报错退出；
- 有 8×GPU + NVLink 的机器上：运行脚本后再执行 `pip install -e . && torchrun --nproc_per_node=8 -m pytest tests/test_planning.py`，应看到全部用例 `passed`（**待本地验证**）。

## 6. 本讲小结

- MoonEP 分两层：CuTe DSL 写的 Python 内核（依赖 `nvidia-cutlass-dsl==4.4.2`，运行时 JIT）+ 一个极小的 C++ 扩展 `moonep._C`（只负责 CuTe DSL 做不了的 VMM/fabric/组播驱动调用），构建入口唯一：[setup.py](setup.py)。
- 两种构建方式产出同一个 `moonep/_C.<abi-tag>.so`：`pip install -e .`（注册包 + 装依赖，推荐）与 `python setup.py build_ext --inplace`（就地编译，需在仓库根目录运行）。
- `nvcc_flags` 的 16 个开关可分为四类：优化（`-O3`、`--use_fast_math`）、语言特性（`-std=c++20`、两个 `--expt-*`）、与 PyTorch 共存的防御（四个 `__CUDA_NO_*`、`-fno-strict-aliasing`、`-Wno-psabi`）、可诊断性（`-lineinfo`、`-ftemplate-backtrace-limit=0`、`-diag-suppress`）。
- [csrc/bindings.cu](csrc/bindings.cu) 是纯绑定层：1 个属性 + 11 个函数，唯一消费者是 [moonep/buffer.py:10-23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10-L23)。
- 测试必须由 torchrun 启动（判定依据是 `RANK` 环境变量，缺失则整组 skip）；每个测试后被 autouse fixture 自动销毁 Buffer，释放进程级 VMM/组播资源。
- 硬件前提是**多 GPU + NVLink 互联**（README 明示）；不足 8 卡时部分用例会按 `min_R`/`max_R` 被 `skip_if_unsupported_world_size` 跳过。

## 7. 下一步学习建议

下一讲 **u1-l3《代码地图：目录结构与模块职责》** 会把 `moonep/` 下每个子模块（api、buffer、planning、dispatch/combine、prefetch、grad_reduce）的职责和入口逐一说清，并覆盖 `tests/` 与 `benchmarks/` 的组织方式——为后续深入任何一条调用链建立索引。

如果你已经完成本讲构建，可以提前做一件事热身：在构建好的环境里 `python -c "from moonep import Buffer; help(Buffer.__init__)"`，浏览一遍构造参数列表，u1-l4 将逐个解释它们。对 `moonep._C` 背后的 VMM 机制好奇的读者，可以在学完 u1-l3 后直接预习 [csrc/nvl_shared_buffer.cuh](csrc/nvl_shared_buffer.cuh)（u2-l2 的主角）。
