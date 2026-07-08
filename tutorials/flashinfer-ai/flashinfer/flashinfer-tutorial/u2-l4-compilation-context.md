# 编译上下文与 CUDA 架构目标

## 1. 本讲目标

本讲是第 2 单元「JIT 编译系统」的第四篇，承接 u2-l2（JitSpec 与工作区环境）。学完本讲你应该能够：

- 说清 `TARGET_CUDA_ARCHS` 这个集合**从哪里来**——它是「自动探测 GPU」和「读 `FLASHINFER_CUDA_ARCH_LIST` 环境变量」二选一的结果。
- 解释 `-gencode=arch=compute_90a,code=sm_90a` 这类 nvcc flag 里的 `compute`/`sm`、以及末尾 `a`/`f` 后缀的含义，看懂 `_normalize_cuda_arch` 为什么对 SM9/SM10/SM12 各有不同处理。
- 掌握 `supported_major_versions` 如何让**单个算子**把编译范围收窄到「自己能跑的几代架构」，并理解收窄失败时报的 `No supported CUDA architectures found` 是怎么触发的。
- 分清两条**正交**的编译并行通道：ninja 的 `-j`（多少个 nvcc 进程同时跑）与 nvcc 的 `--threads`（一个 nvcc 进程内部多少线程），以及它们的内存开销关系。

一句话：本讲回答「FlashInfer 到底为哪些 GPU 架构编译 kernel、由谁决定、又怎么控制编译的快慢与内存」。

## 2. 前置知识

- **compute capability（计算能力）**：NVIDIA 给每代 GPU 的版本号，如 H100 是 9.0、B200 是 10.0a。常被简写成 SM90、SM100。FlashInfer 支持的范围是 SM7.5（Turing）到 SM12.x（Blackwell）。
- **`-gencode` 的成对语义**：nvcc 用 `-gencode=arch=compute_XX,code=sm_YY` 同时指定「虚拟架构（compute_）」和「真实架构（sm_）」。`compute_XX` 决定**能用哪些指令**（决定了能写到 PTX 里的特性集），`sm_YY` 决定**生成哪张卡的机器码（SASS）**。FlashInfer 默认让两者成对相同（如 `compute_90a`/`sm_90a`），所以下面只谈架构号本身。
- **`a` / `f` 后缀**：`compute_90`（无后缀）是「可移植子集」，`compute_90a`（带 `a`）是「架构特有特性（actual）」——只有用 `a` 后缀编译的 kernel 才能使用 Hopper 的 TMA、WGMMA 等新指令。所以用 TMA 的 kernel 必须 `compute_90a`。
- **SM 主版本（major）**：本讲大量出现「`supported_major_versions=[10, 11]`」这种写法，它只关心 major 号（9=Hopper, 10=Blackwell, 11=Blackwell, 12=Blackwell），用 major 号粗粒度圈定「能跑的代」。
- **JitSpec 与注册表**：回顾 u2-l2/u2-l3，每个待编译模块抽象成一个 `JitSpec`，由 `gen_jit_spec` 装配并登记进 `jit_spec_registry`；**登记 ≠ 编译**，真正编译推迟到 `.build_and_load()`。
- **工作区目录名 = `<version>/<sorted_arch>`**：回顾 u2-l2，`FLASHINFER_WORKSPACE_DIR` 路径里那段架构串（如 `80_90a_100a`）正是本讲 `CompilationContext.TARGET_CUDA_ARCHS` 排序后的产物。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [flashinfer/compilation_context.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/compilation_context.py) | `CompilationContext` 类：探测/读取目标架构、规范化后缀、产出 nvcc 的 `-gencode` flags |
| [flashinfer/jit/env.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py) | 工作区路径解析；`_get_workspace_dir_name()` 把 `TARGET_CUDA_ARCHS` 拼进目录名 |
| [flashinfer/jit/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) | 模块级单例 `current_compilation_context`、`check_cuda_arch()` 最低架构门控、`gen_jit_spec` 装配编译选项 |
| [flashinfer/jit/cpp_ext.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py) | `get_nvcc_parallelism_flags()`（nvcc `--threads`）、`is_cuda_version_at_least()`、`run_ninja()`（ninja `-j`）以及把 flags 拼进 ninja 文件的逻辑 |
| [flashinfer/jit/mla.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/mla.py) | 真实范例：`gen_mla_module` 用 `[10,11]`、`gen_sparse_mla_sm120_module` 用 `[12]`，演示 `supported_major_versions` 的实际用法 |

本讲对应的最小模块是：**架构目标**、**SM 版本限制**、**编译并行**，下面逐一展开。

## 4. 核心概念与源码讲解

### 4.1 架构目标的确定：TARGET_CUDA_ARCHS 从哪来

#### 4.1.1 概念说明

「为哪些架构编译」是 JIT 的第一性问题。FlashInfer 不能也无必要为所有 SM 都编译——一台只有 H100 的机器，没必要浪费时间给 B200 生成代码。`CompilationContext` 就是那个回答「当前到底要为哪些架构编译」的角色，它的核心产物是一个集合 `TARGET_CUDA_ARCHS`。

它有两条信息来源，二选一：

1. **环境变量 `FLASHINFER_CUDA_ARCH_LIST`**：用户/CI 显式给定，优先级最高。
2. **自动探测**：环境变量没设时，遍历 `torch.cuda.device_count()` 张卡，读每张卡的 `get_device_capability()`。

`CompilationContext` 是一个**瞬时对象**——构造函数里就把集合算完了，之后不再变。模块级还会把它实例化成一个全局单例 `current_compilation_context`（见 4.1.3），全项目共用同一份架构决策。

#### 4.1.2 核心流程

```text
构造 CompilationContext()
        │
        ├─ 环境里有 FLASHINFER_CUDA_ARCH_LIST ?
        │     ├─ 有 → 逐个解析 "major.minor"，规范化后加入集合
        │     └─ 无 → for device in range(torch.cuda.device_count()):
        │                  读 get_device_capability → 规范化 → 加入集合
        │              （探测失败只 warning，不致命）
        │
        └─ self.TARGET_CUDA_ARCHS = { (major, minor_str), ... }   # 一个 set
```

关键点：集合元素是 `(major, minor_str)` 二元组，其中 `minor_str` 是**带后缀的字符串**（如 `"0a"`、`"0f"`），不是整数。这直接决定了 4.2 节的目录命名与 flag 拼接。

#### 4.1.3 源码精读

构造函数与探测逻辑在 [flashinfer/compilation_context.py:61-81](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/compilation_context.py#L61-L81)，这里用中文逐段说明：

- 第 62 行先把 `TARGET_CUDA_ARCHS` 初始化为空 `set`。
- 第 63 行若检测到 `FLASHINFER_CUDA_ARCH_LIST`，就进入「手动指定」分支；否则走自动探测。
- 第 76–79 行是自动探测：`torch.cuda.get_device_capability(device)` 返回 `(major, minor)` 整数对，交给 `_normalize_cuda_arch` 加后缀后入集合。
- 第 80–81 行：探测抛异常（比如 CPU-only 环境或驱动问题）只记一条 warning，**不中断**——此时集合为空，后续 `check_cuda_arch` 会兜底报错。

手动分支里有个细节值得注意 [flashinfer/compilation_context.py:69-74](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/compilation_context.py#L69-L74)：如果用户写的串里 `minor` 末位是字母（如 `"12.0f"` 里的 `"0f"`），就**原样保留**用户的写法；否则才调 `_normalize_cuda_arch` 规范化。这给了用户「我想精确指定后缀」的逃生舱。

这个全局单例的实例化点在 core.py：[flashinfer/jit/core.py:138](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L138)

```python
current_compilation_context = CompilationContext()
```

各 `gen_*_module` 函数正是引用这个 `current_compilation_context`（见 4.2）。而 `_get_workspace_dir_name` 在 env.py 模块导入时也会**再 `new` 一个** `CompilationContext()`，把它排序拼进工作区路径名 [flashinfer/jit/env.py:135-144](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L135-L144)：

```python
arch = "_".join(
    f"{major}{minor}"
    for major, minor in sorted(compilation_context.TARGET_CUDA_ARCHS)
)
return FLASHINFER_CACHE_DIR / flashinfer_version / arch
```

这正是 u2-l2 里反复出现的 `<version>/<sorted_arch>` 目录名里那段 `80_90a_100a` 的来源。`sorted()` 在注释里被强调为「关键」——否则同一组架构集合在不同运行里可能拼出 `75_80_89` 或 `89_75_80`，造成缓存碎片。

> 最低架构门控：另外有一条独立的最低要求检查 `check_cuda_arch()`，它要求探测到的架构里至少有一张 ≥ SM7.5，否则报 `FlashInfer requires GPUs with sm75 or higher`，见 [flashinfer/jit/core.py:96-108](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L96-L108)。它在 `gen_jit_spec` 里被调用，是「整库最低门槛」。

#### 4.1.4 代码实践

**实践目标**：用纯 Python 直接构造 `CompilationContext`，对比「自动探测」与「手动指定」两种来源下 `TARGET_CUDA_ARCHS` 的内容，确认环境变量优先。

**操作步骤**：

```python
# 示例代码：可直接在装了 flashinfer 的环境里跑（无 GPU 也能跑通探测分支）
import os
from flashinfer.compilation_context import CompilationContext

# 1) 不设环境变量：自动探测（无 GPU 时 device_count()==0，集合为空）
if "FLASHINFER_CUDA_ARCH_LIST" in os.environ:
    del os.environ["FLASHINFER_CUDA_ARCH_LIST"]
ctx_auto = CompilationContext()
print("auto  ->", sorted(ctx_auto.TARGET_CUDA_ARCHS))

# 2) 显式指定（避免触发 SM12 需要 CUDA>=12.9 的分支，这里只选 8/9/10 代）
os.environ["FLASHINFER_CUDA_ARCH_LIST"] = "8.9 9.0a 10.0"
ctx_manual = CompilationContext()
print("manual->", sorted(ctx_manual.TARGET_CUDA_ARCHS))
print("flags ->", ctx_manual.get_nvcc_flags_list())
```

**需要观察的现象**：

- `auto` 分支：有 GPU 时会看到本机架构（如 H100 打印 `[(9, '0a')]`）；无 GPU 时为空列表 `[]`。
- `manual` 分支：`8.9` → `(8, '9')`；`9.0a` 因末位是字母原样保留 → `(9, '0a')`；`10.0` 规范化 → `(10, '0a')`。

**预期结果**（无 GPU、手动设为 `"8.9 9.0a 10.0"`）：

```text
auto  -> []
manual-> [(8, '9'), (9, '0a'), (10, '0a')]
flags -> ['-gencode=arch=compute_89,code=sm_89',
          '-gencode=arch=compute_90a,code=sm_90a',
          '-gencode=arch=compute_100a,code=compute_100a' 的成对形式,
          '-DFLASHINFER_ENABLE_FP8_E8M0', '-DFLASHINFER_ENABLE_FP4_E2M1']
```

> 注：`get_nvcc_flags_list()` 的拼装规则见 4.2 节，flag 的精确字符串以本机实际输出为准；上面只示意结构与顺序。若你的环境 CUDA ≥ 12.9，可把列表换成 `"12.0"` 观察 SM12 的 `120f` 后缀。

#### 4.1.5 小练习与答案

**练习 1**：在一台装了 1 张 H100 的机器上，既没设环境变量、`torch.cuda.device_count()` 正常返回，`TARGET_CUDA_ARCHS` 会是什么？

**答案**：自动探测到 (9, 0)，经 `_normalize_cuda_arch(9, 0)` 得到 `(9, '0a')`，集合为 `{(9, '0a')}`。

**练习 2**：为什么 `_get_workspace_dir_name` 里必须用 `sorted()`？

**答案**：`TARGET_CUDA_ARCHS` 是 `set`，遍历顺序不确定。若不排序，同一台多卡机器不同进程可能拼出 `90a_100a` 或 `100a_90a`，导致每个排列各占一个缓存目录，造成缓存碎片与重复编译。`sorted()` 保证目录名确定性。

**练习 3**：用户写 `FLASHINFER_CUDA_ARCH_LIST="9.0a"` 与写 `"9.0"`，最终集合一样吗？

**答案**：一样，都是 `(9, '0a')`。前者因 `minor[-1].isalpha()` 原样保留；后者经 `_normalize_cuda_arch`（major==9 → `str(minor)+"a"`）也得到 `"0a"`。

### 4.2 SM 版本限制：supported_major_versions 与后缀规范化

#### 4.2.1 概念说明

`TARGET_CUDA_ARCHS` 描述的是「这台机器/这次构建**可能**要为哪些架构编译」。但**具体某个算子**未必能在所有架构上跑——例如一个只用了 Blackwell 新指令的 kernel，强行给 Hopper 编译必然失败。于是需要第二层收窄：**每个 `gen_*_module` 自己声明「我能跑哪几代」**，这就是 `get_nvcc_flags_list(supported_major_versions=...)` 的参数。

这里有两个机制叠加：

1. **`_normalize_cuda_arch`**：把 `(major, minor)` 加上正确后缀，确保 nvcc 拿到的 flag 是合法且能用到该代特性的。
2. **`supported_major_versions` 过滤**：从全局 `TARGET_CUDA_ARCHS` 里只挑出 major 号在白名单里的架构，再拼 flag；若挑完是空集，直接报错。

#### 4.2.2 核心流程

```text
某 gen_*_module 调用:
    get_nvcc_flags_list(supported_major_versions=[10, 11])
        │
        ├─ 若给了 supported_major_versions:
        │     supported_cuda_archs = [ t for t in TARGET_CUDA_ARCHS if t[0] in [10,11] ]
        │   否则:
        │     supported_cuda_archs = TARGET_CUDA_ARCHS  （全部）
        │
        ├─ 若 supported_cuda_archs 为空 → raise "No supported CUDA architectures found ..."
        │
        └─ return [ f"-gencode=arch=compute_{M}{m},code=sm_{M}{m}"
                    for M,m in sorted(supported_cuda_archs) ] + COMMON_NVCC_FLAGS
```

后缀规范化的规则（来自 `_normalize_cuda_arch` 的 docstring 与实现）：

| 输入 (major, minor) | 输出后缀 | 说明 |
|---|---|---|
| (9, 0) | `90a` | Hopper：统一加 `a`，才能用 TMA/WGMMA |
| (10, 0) | `100a` | Blackwell：major ≥ 10 一律加 `a` |
| (12, 0) | `120f` | SM120 用 `f` 后缀，**需 CUDA ≥ 12.9** |
| (12, 1) | `121a` | SM121 用 `a` 后缀，与 SM120 区分 |
| (8, 9) | `89` | Ampere 及更早：无后缀 |

为什么 SM12.x 要「每个 minor 变体各一份 cubin」？docstring 写得很直白：避免把 SM120 的代码跑到 SM121（DGX Spark）上引发 `cudaErrorIllegalInstruction`——也就是说 SM120 和 SM121 虽同属 Blackwell，但二进制不兼容，必须分别编译。

#### 4.2.3 源码精读

`COMMON_NVCC_FLAGS` 与规范化函数在 [flashinfer/compilation_context.py:28-59](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/compilation_context.py#L28-L59)，要点：

- `COMMON_NVCC_FLAGS` 固定打开两个宏：`-DFLASHINFER_ENABLE_FP8_E8M0` 与 `-DFLASHINFER_ENABLE_FP4_E2M1`（FP8 的 E8M0 缩放因子、FP4 的 E2M1 格式）。
- major==9 分支返回 `str(minor) + "a"`（第 45–46 行）。
- major==12 分支（第 47–56 行）：先 `is_cuda_version_at_least("12.9")`，不满足直接 `raise`；满足后 minor==0 给 `"0f"`，其余给 `str(minor)+"a"`。
- major ≥ 10 分支一律 `str(minor)+"a"`（第 57–58 行）。
- 其余（major < 9）无后缀。

`get_nvcc_flags_list` 自身在 [flashinfer/compilation_context.py:83-101](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/compilation_context.py#L83-L101)，关键就是「按 `supported_major_versions` 过滤 → 空集报错 → 拼成对 `-gencode` + 公共宏」。注意第 94–97 行：当过滤后为空（典型场景：在一台只有 H100 的机器上调一个 `supported_major_versions=[12]` 的 Blackwell-only 模块），会抛 `No supported CUDA architectures found for major versions ...`——这正是 CLAUDE.md 里提到的 `RuntimeError: No supported CUDA architectures found`。

最干净的真实范例在 mla.py，**两个生成器、两种限制**：

- [flashinfer/jit/mla.py:21-32](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/mla.py#L21-L32)：`gen_mla_module` 用 `[10, 11]`，即 MLA 的 CUTLASS 路径只面向 Blackwell。
- [flashinfer/jit/mla.py:35-55](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/mla.py#L35-L55)：`gen_sparse_mla_sm120_module` 用 `[12]`，专给 SM120 编译。

两者都把 `get_nvcc_flags_list(...)` 的结果当 `extra_cuda_cflags` 传给 `gen_jit_spec`，从而把架构收窄写进该模块的编译选项。仓库里几十个 `gen_*_module` 几乎都按这个套路调用（可用 grep 验证，如 `jit/gemm/core.py`、`jit/attention/modules.py`、`jit/comm.py`）。

> 还有一种「模块自带 `-gencode`」的情况：当 `gen_jit_spec` 收到的 `extra_cuda_cflags` 里**已经**包含 `-gencode=` 时，`build_cuda_cflags` 会改用「模块的架构 flag + 全局非架构 flag」，避免与全局 flag 重复，见 [flashinfer/jit/cpp_ext.py:210-229](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L210-L229)。

#### 4.2.4 代码实践

**实践目标**：复现「同一份 `TARGET_CUDA_ARCHS`、不同 `supported_major_versions` 产出不同 flag 集」，并亲手触发一次「空集报错」。

**操作步骤**：

```python
# 示例代码：纯 Python，无需 GPU
import os
from flashinfer.compilation_context import CompilationContext

# 模拟一台「同时插了 Ampere/Hopper/Blackwell」的构建机
os.environ["FLASHINFER_CUDA_ARCH_LIST"] = "8.9 9.0a 10.0a"
ctx = CompilationContext()
print("全集:", sorted(ctx.TARGET_CUDA_ARCHS))

# (a) MLA 路径：只要 Blackwell(10,11)
print("MLA:", ctx.get_nvcc_flags_list(supported_major_versions=[10, 11]))

# (b) 全都要（等价于不传）
print("ALL:", ctx.get_nvcc_flags_list(supported_major_versions=None)[:3])

# (c) 故意要 SM120，但这台机器没有 12 代 → 空集报错
try:
    ctx.get_nvcc_flags_list(supported_major_versions=[12])
except RuntimeError as e:
    print("报错:", e)
```

**需要观察的现象**：

- (a) 只出现一条 `-gencode=...sm_100a`（10 代），没有 8/9 代。
- (c) 抛 `No supported CUDA architectures found for major versions [12]`。

**预期结果**：与上面描述一致。（a）的 flag 列表会附带两条 `COMMON_NVCC_FLAGS` 宏定义，这是正常的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_normalize_cuda_arch(12, 0)` 不直接返回 `"120a"` 而要返回 `"120f"`？

**答案**：SM120 与 SM121 二进制不兼容。把 SM120 编译成 `120f`、SM121 编译成 `121a`，可防止 SM120 的 SASS 被误加载到 SM121（DGX Spark）上运行而触发 `cudaErrorIllegalInstruction`。每个 minor 变体各一份 cubin。

**练习 2**：一台纯 H100 机器（`TARGET_CUDA_ARCHS={(9,'0a')}`）上调用 `gen_mla_module()`（`supported_major_versions=[10,11]`）会发生什么？

**答案**：过滤后 `supported_cuda_archs` 为空，`get_nvcc_flags_list` 抛 `No supported CUDA architectures found for major versions [10, 11]`，模块不会编译。MLA 的 CUTLASS 路径不支持 Hopper，这是预期行为。

**练习 3**：`COMMON_NVCC_FLAGS` 里的 `-DFLASHINFER_ENABLE_FP8_E8M0` 是干嘛的？

**答案**：它在预处理期打开 FP8 的 E8M0 缩放因子（scale factor）相关代码路径。E8M0 是 NVFP4/MXFP4 里 block scaling 用的 8 位纯指数格式，编译期打开后，kernel 模板里对应的量化逻辑才会被实例化。

### 4.3 编译并行：nvcc --threads 与 ninja -j

#### 4.3.1 概念说明

确定「为哪些架构编译」之后，下一个问题是「编译要跑多快、吃多少内存」。FlashInfer 暴露**两条正交的并行通道**，理解它们的区别是避免「编译把内存撑爆」的关键：

1. **ninja 层并行**：ninja 把每个 `.cu` 源文件当成一个独立任务，可以并行起多个 nvcc 进程。并发数由环境变量 `MAX_JOBS` 控制，最终落到 ninja 的 `-j` 参数。
2. **nvcc 层并行**：单个 nvcc 进程在编译「多 gencode 目标 / 多阶段」时，自己内部也能开多线程。并发数由 `FLASHINFER_NVCC_THREADS` 控制，落到 nvcc 的 `--threads=` 参数。

两者的开销是**相乘**的：总并发编译线程数 ≈ `MAX_JOBS × FLASHINFER_NVCC_THREADS`，nvcc 编译模板繁重的 CUTLASS 代码时单线程就要数 GB 内存，所以两者乘积直接决定峰值内存。CLAUDE.md 里那条「total compilation memory ≈ MAX_JOBS × FLASHINFER_NVCC_THREADS × per-thread mem」就是这个意思。

#### 4.3.2 核心流程

```text
gen_jit_spec() 装配 cuda_cflags:
    cuda_cflags = [ get_nvcc_parallelism_flags(),  # 即 ["--threads=N"]
                    "-use_fast_math", ... ]

JitSpec.build_and_load() → build() → run_ninja():
    command = ["ninja", "-v", "-C", workdir, "-f", ninja_file]
    if MAX_JOBS 可解析为整数:
        command += ["-j", str(MAX_JOBS)]
    启动 ninja → 每个 .cu 起一个 nvcc 进程 → 每个 nvcc 进程内部 --threads=N 并行
```

两条通道互不影响：你完全可以让「少文件多架构」的模块靠 `FLASHINFER_NVCC_THREADS` 提速，让「多文件少架构」的批量构建靠 `MAX_JOBS` 提速。

#### 4.3.3 源码精读

`get_nvcc_parallelism_flags` 在 [flashinfer/jit/cpp_ext.py:94-112](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L94-L112)：

- 读 `FLASHINFER_NVCC_THREADS`，默认 `1`。
- 非法值（非整数 / `<1`）记 warning 后回退到 1。
- 返回 `["--threads=N"]`。

这个返回值被 `gen_jit_spec` 拼进默认 `cuda_cflags`（注意它出现在列表最前）[flashinfer/jit/core.py:431-432](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L431-L432)。也就是说，**每一个**经 `gen_jit_spec` 装配的模块都会带上 `--threads`。

ninja 的 `-j` 由 `_get_num_workers` + `run_ninja` 控制 [flashinfer/jit/cpp_ext.py:344-362](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L344-L362)：

- `_get_num_workers` 读 `MAX_JOBS`，必须是数字串才生效；不设则返回 `None`（不传 `-j`，让 ninja 自己决定，通常取 CPU 核数）。
- `run_ninja` 在 `num_workers` 非 None 时给 ninja 拼上 `-j <N>`。

> 小陷阱：`MAX_JOBS` 只接受纯数字（`isdigit()`），写成 `MAX_JOBS=4x` 会被静默忽略；`FLASHINFER_NVCC_THREADS` 走 `int()`，非法值会 warning 后回退。两者的容错策略不同。

#### 4.3.4 代码实践

**实践目标**：分别验证两条并行通道确实只受各自环境变量控制，互不干扰。

**操作步骤**：

```python
# 示例代码：纯 Python，无需 GPU
import os
from flashinfer.jit.cpp_ext import get_nvcc_parallelism_flags, _get_num_workers

# 通道 1：nvcc 内部线程
for v in ["1", "4", "8"]:
    os.environ["FLASHINFER_NVCC_THREADS"] = v
    print(f"NVCC_THREADS={v} ->", get_nvcc_parallelism_flags())

# 非法值回退
os.environ["FLASHINFER_NVCC_THREADS"] = "abc"
print("NVCC_THREADS=abc ->", get_nvcc_parallelism_flags())

# 通道 2：ninja 并发进程（读 MAX_JOBS）
del os.environ["FLASHINFER_NVCC_THREADS"]
for v in ["2", "8"]:
    os.environ["MAX_JOBS"] = v
    print(f"MAX_JOBS={v} ->", _get_num_workers())
os.environ["MAX_JOBS"] = "4x"   # 非纯数字 → 被忽略
print("MAX_JOBS=4x ->", _get_num_workers())
```

**需要观察的现象**：

- `get_nvcc_parallelism_flags()` 输出随 `FLASHINFER_NVCC_THREADS` 变化；`"abc"` 回退为 `['--threads=1']`。
- `_get_num_workers()` 随 `MAX_JOBS` 变化；`"4x"` 返回 `None`（即不传 `-j`）。

**预期结果**：与上面对应；`MAX_JOBS="4x"` 返回 `None` 是关键观察点——它不会限制并发。

> 进阶（**待本地验证**，需 GPU）：在一次真实 attention 编译时同时设 `export MAX_JOBS=1 FLASHINFER_NVCC_THREADS=2`，用 `FLASHINFER_JIT_VERBOSE=1` 观察日志里 nvcc 命令行带 `--threads=2`、且任一时刻只有 1 个 nvcc 进程在跑；再换成 `MAX_JOBS=4 FLASHINFER_NVCC_THREADS=1` 对比编译总时长与峰值内存（`nvidia-smi`/系统内存）。内存应当约翻 4 倍量级、时间缩短。

#### 4.3.5 小练习与答案

**练习 1**：`MAX_JOBS` 和 `FLASHINFER_NVCC_THREADS` 分别控制什么？

**答案**：`MAX_JOBS` 控制 ninja 同时启动的 nvcc **进程**数（ninja `-j`，每个进程对应一个 `.cu` 源文件）；`FLASHINFER_NVCC_THREADS` 控制单个 nvcc 进程内部编译多 gencode/多阶段时的**线程**数（nvcc `--threads=`）。

**练习 2**：一台内存吃紧的 CI 机器想保证编译不 OOM，应该怎么调这两个变量？

**答案**：降低它们的**乘积**。优先把 `MAX_JOBS` 调小（如 `MAX_JOBS=1`），因为每多一个 nvcc 进程就多一整份 CUTLASS 模板内存；`FLASHINFER_NVCC_THREADS` 保持默认 1 或也调小。峰值内存 ≈ `MAX_JOBS × FLASHINFER_NVCC_THREADS × 单线程内存`。

**练习 3**：为什么 `gen_jit_spec` 里 `get_nvcc_parallelism_flags()` 放在 `cuda_cflags` 列表最前面，而不放在 `extra_cuda_cflags` 里？

**答案**：它属于全局默认编译选项，应由 `gen_jit_spec` 统一注入给**所有**模块，而不是让每个 `gen_*_module` 自己记得传。放在默认 `cuda_cflags` 里保证一致性与零遗漏；各模块的 `extra_cuda_cflags`（含架构 flag）随后再拼接。

## 5. 综合实践

把三个最小模块串起来，完成一个「**用 `FLASHINFER_CUDA_ARCH_LIST` 把编译目标收窄到本机单卡，观察工作区目录名与编译产物数量的变化**」的小任务。这是本讲规格里指定的实践。

**背景**：默认（不设环境变量）时，FlashInfer 探测到的所有 GPU 架构都会进 `TARGET_CUDA_ARCHS`，于是每个模块的 `.so` 会为每个架构各编译一份；目录名里的 `<sorted_arch>` 段也会包含全部架构串。

**操作步骤**：

1. 先不设环境变量，清掉缓存，跑一次最简 decode，记录编译产物：

   ```bash
   rm -rf ~/.cache/flashinfer/        # 清两级缓存中的磁盘层
   python - <<'PY'
   import torch, flashinfer
   q = torch.randn(32, 128, dtype=torch.float16, device="cuda")
   k = torch.randn(8, 1024, 128, dtype=torch.float16, device="cuda")
   v = torch.randn(8, 1024, 128, dtype=torch.float16, device="cuda")
   o = flashinfer.single_decode_with_kv_cache(q, k, v)   # 首次触发 JIT
   print("ok", o.shape)
   PY
   # 记录目录名里的 <sorted_arch> 段，以及 cached_ops 下 .so 的数量
   find ~/.cache/flashinfer -name '*.so' | wc -l
   ```

2. 再把架构收窄到本机单卡，清缓存重跑：

   ```bash
   # 假设本机是 H100（cc=9.0）
   export FLASHINFER_CUDA_ARCH_LIST="9.0a"
   rm -rf ~/.cache/flashinfer/
   python - <<'PY'
   import torch, flashinfer
   q = torch.randn(32, 128, dtype=torch.float16, device="cuda")
   k = torch.randn(8, 1024, 128, dtype=torch.float16, device="cuda")
   v = torch.randn(8, 1024, 128, dtype=torch.float16, device="cuda")
   o = flashinfer.single_decode_with_kv_cache(q, k, v)
   print("ok", o.shape)
   PY
   find ~/.cache/flashinfer -name '*.so' | wc -l
   ls ~/.cache/flashinfer/*/   # 对比目录名里的架构串
   ```

**需要观察与解释**：

- **目录名**：第二步的 `FLASHINFER_WORKSPACE_DIR` 路径里 `<sorted_arch>` 段应只剩 `90a`（对应 `_get_workspace_dir_name` 的拼接），而非多卡时的 `80_90a_...`。
- **产物数量**：理论上同一模块为「1 个架构」编译出的 `.so` 数量，应 ≤ 多架构场景；具体数取决于该算子 `supported_major_versions` 是否覆盖到本机架构。若 decode 模块的 `supported_major_versions` 包含本机 major，目录里会出现对应那份 `.so`。
- **若本机架构不在某模块白名单**：该模块不会编译（4.2 的空集逻辑），这是预期，不是 bug。

**预期结果**：收窄后编译更快、目录名架构段更短、`.so` 数量更少。**本实践需要 GPU 与可用 nvcc，若当前环境无 GPU，标注为「待本地验证」**——但你可以用 4.1/4.2 的纯 Python 实践先行验证 `TARGET_CUDA_ARCHS` 与目录名拼接逻辑这两步，它们不依赖编译。

## 6. 本讲小结

- `TARGET_CUDA_ARCHS` 是 `CompilationContext` 构造时一次性算出的集合：环境变量 `FLASHINFER_CUDA_ARCH_LIST` 优先，否则用 `torch.cuda.get_device_capability` 自动探测；探测失败只 warning 不中断。
- 集合元素是 `(major, minor_str)`，`minor_str` 带后缀：SM9/SM10 统一加 `a`，SM120 用 `f`、SM121 用 `a`（需 CUDA ≥ 12.9，且二者二进制不兼容故分别编译），SM8 及更早无后缀。
- `get_nvcc_flags_list(supported_major_versions=...)` 让每个 `gen_*_module` 把全局架构集**二次收窄**到自己能跑的几代；过滤后空集会抛 `No supported CUDA architectures found`，这就是 CLAUDE.md 提到的报错。
- 工作区目录名 `<version>/<sorted_arch>` 里的架构串正是 `sorted(TARGET_CUDA_ARCHS)` 的产物，`sorted()` 保证确定性、避免缓存碎片。
- 编译并行有**两条正交通道**：`MAX_JOBS` → ninja `-j`（多少个 nvcc 进程），`FLASHINFER_NVCC_THREADS` → nvcc `--threads=`（单进程多少线程）；峰值内存 ≈ 两者乘积 × 单线程内存。
- 全项目共用一个模块级单例 `current_compilation_context`，最低架构门槛则由独立的 `check_cuda_arch()`（要求 ≥ SM7.5）在 `gen_jit_spec` 里把关。

## 7. 下一步学习建议

本讲把「为谁编译、怎么收窄、编译多快」讲完了，JIT 系统还剩最后一块拼图——**缓存与失效**。建议下一讲学习：

- **u2-l5（模块缓存与失效机制）**：`@functools.cache` 进程内缓存与磁盘 `.so` 两级缓存如何配合、URI 由哪些成分组成（注意架构也是 URI 的一部分，本讲的 `TARGET_CUDA_ARCHS` 会进入缓存键）、改源码/改 flags/换架构各会怎样触发失效。

横向延伸阅读建议：

- 想看「按架构派发到不同 `gen_*_module`」的全景，可读 [flashinfer/aot.py:478-588](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/aot.py#L478-L588)，那里 `has_sm90/has_sm100/has_sm120` 等布尔位精细控制每个模块是否进 AOT 构建列表。
- 想系统了解 `_normalize_cuda_arch` 里 SM12.x 的背景，可查 PTX ISA 文档里 Blackwell 架构特性与各 SM 变体的差异。
