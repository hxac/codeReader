# 运行时 CPU 变体检测与加载

## 1. 本讲目标

上一篇（u2-l2）讲的是**构建期**的故事：CMake 把 AMX、AVX512、AVX2 等不同指令集分别编译成多份 `.so`，塞进同一个 wheel 里。本篇讲的是**运行期**的故事：当用户执行 `import kt_kernel` 的那一刻，Python 是如何在一瞬间判断「这台机器 CPU 支持什么指令集」，然后从 wheel 里挑出最优的那一份 `.so` 装载进来的。

学完本讲你应该能够：

1. 说清 `import kt_kernel` 时 CPU 变体检测与扩展加载的完整时序。
2. 理解 `amx` / `avx512_bf16` / `avx512_vbmi` / `avx512_vnni` / `avx512_base` / `avx2` 六个变体的优先级与判定规则。
3. 掌握「多变体 `.so`」与「单变体 `.so`」两种构建产物的加载差异，以及逐级回退链。
4. 会用 `KT_KERNEL_DEBUG=1` 观察检测过程、用 `KT_KERNEL_CPU_VARIANT` 手动覆盖检测结果来排查问题。

## 2. 前置知识

- **共享库（`.so`）与 Python 扩展模块**：C/C++ 代码可以被编译成动态链接库（Linux 上是 `.so`），Python 通过 `import` 把它当作一个模块加载进来。kt-kernel 的 CPU 算子是用 C++ 写的，编译后就是这种 `.so` 扩展模块。
- **pybind11**：一个让 C++ 代码可以被 Python 调用的工具库。它在 C++ 里用 `PYBIND11_MODULE` 宏注册一个模块，并生成对应的初始化函数（形如 `PyInit_模块名`）。
- **CPU 指令集**：现代 CPU 支持很多「向量计算」指令集，例如 `AVX2`、`AVX512` 系列、`AMX`（高级矩阵扩展）。指令集越新，矩阵乘法这类算子算得越快，但旧 CPU 用不了新指令集。
- **`/proc/cpuinfo`**：Linux 内核暴露的一个虚拟文件，里面有 CPU 的型号、核数，以及一行 `flags` 列出 CPU 支持的全部指令集标志（如 `avx2`、`avx512f`、`amx_tile` 等）。
- **`import` 只执行一次**：Python 在一个进程里对同一个模块只会真正执行一次模块顶层代码（之后重复 `import` 命中的是缓存）。这一点对本讲的实践任务很关键：**环境变量覆盖必须在启动新进程前设置，进程内后改无效**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `kt-kernel/python/_cpu_detect.py` | 本讲的主角：探测 CPU 特性、挑选最优变体、加载对应 `.so`、提供回退链。 |
| `kt-kernel/python/__init__.py` | 包入口：在 `import kt_kernel` 的最开始就调用 `_cpu_detect.initialize()`，并把加载结果注册进 `sys.modules`。 |
| `kt-kernel/setup.py`（辅助引用） | 构建期产生 6 份带变体后缀的 `.so` 文件，是运行期可挑选的「库存」。 |
| `kt-kernel/ext_bindings.cpp`（辅助引用） | C++ 侧用 `PYBIND11_MODULE(kt_kernel_ext, m)` 注册模块，决定了所有变体 `.so` 对外都叫 `kt_kernel_ext`。 |
| `kt-kernel/pyproject.toml`（辅助引用） | `package-dir` 把磁盘上的 `python/` 目录映射成导入名 `kt_kernel`，决定了 `.so` 与 `_cpu_detect.py` 的相对位置。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**特性探测**、**扩展加载**、**回退链**。它们恰好对应 `_cpu_detect.py` 里的三个核心函数。

### 4.1 特性探测

#### 4.1.1 概念说明

「特性探测」要回答的问题只有一个：**这台机器的 CPU 到底能跑哪个变体？**

kt-kernel 的设计是**渐进式匹配（progressive matching）**：把 6 个变体按「从最优到最差」排成一张优先级表，每个变体都关联一组「必须全部满足」的 CPU 标志。代码从表头开始逐个检查，**第一个所有标志都命中的变体就是答案**。

为什么是「渐进」？因为这些指令集本身是逐代累进的：能用 AMX 的 CPU 一定也能用 AVX512，能用 AVX512 的也一定能用 AVX2。所以表是层层包含的关系，找到最优的那一档即可。

> 与上一篇 u2-l2 的区别：u2-l2 讲的是「构建时用 `CPU_FEATURE_MAP` 把 `NATIVE/AVX512/AVX2` 翻译成编译开关」；本篇讲的是「运行时读 `/proc/cpuinfo` 选 `.so`」。一个是编译器看到的，一个是运行库看到的，两条路径独立但用同一套能力名词。

#### 4.1.2 核心流程

`detect_cpu_features()` 的决策流程可以用下面的伪代码概括：

```
读环境变量 KT_KERNEL_CPU_VARIANT
  └─ 若是合法变体之一 → 直接返回（人为覆盖，最高优先级）
  └─ 否则继续自动探测

尝试打开 /proc/cpuinfo
  ├─ 成功：
  │    取出 flags 那一行 → 拆成集合 cpu_flags
  │    按优先级遍历变体表：
  │      for 变体 in [amx, avx512_bf16, avx512_vbmi, avx512_vnni, avx512_base, avx2]:
  │        if 该变体所有要求标志 ⊆ cpu_flags:
  │          return 该变体
  │    若都不满足 → return "avx2"（兜底）
  └─ 失败（非 Linux / 容器无该文件）：
       尝试 import cpufeature 包走跨平台兜底
       若 cpufeature 也没有 → return "avx2"
```

形式化地，设 CPU 实际拥有的标志集合为 \(C\)，第 \(i\) 个变体的需求集合为 \(R_i\)（\(i=0\) 最优），则选中的变体是：

\[
\text{variant} = R_k,\quad \text{其中 } k = \min \{\, i \mid R_i \subseteq C \,\}
\]

也就是说：**在优先级顺序下，第一个需求被 CPU 能力完全包含的变体胜出**。

#### 4.1.3 源码精读

首先是函数开头的环境变量覆盖逻辑——这也是最优先、最「霸道」的判定，只要用户设了合法值就直接返回，连 `/proc/cpuinfo` 都不看：

读取 `KT_KERNEL_CPU_VARIANT` 并校验合法取值（[_cpu_detect.py:42-48](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L42-L48)）。这里定义了 6 个合法变体名 `["amx", "avx512_bf16", "avx512_vbmi", "avx512_vnni", "avx512_base", "avx2"]`，命中即返回。

接着是读取 `/proc/cpuinfo` 并把 `flags` 那一行解析成集合（[_cpu_detect.py:50-61](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L50-L61)）。注意它只取**第一个** CPU 核的 `flags`（遇到第一行 `flags:` 就 `break`），假设所有核能力一致——这在常规桌面/服务器上是成立的。

然后是核心的「优先级变体需求表」（[_cpu_detect.py:64-83](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L64-L83)）。这张表正是本讲的「地图」，值得逐行看：

| 优先级 | 变体名 | 必须满足的 flags |
| --- | --- | --- |
| 1（最优） | `amx` | `amx_tile amx_int8 amx_bf16` + 全套 AVX512 |
| 2 | `avx512_bf16` | `avx512f avx512bw avx512_vnni avx512_vbmi avx512_bf16` |
| 3 | `avx512_vbmi` | `avx512f avx512bw avx512_vnni avx512_vbmi` |
| 4 | `avx512_vnni` | `avx512f avx512bw avx512_vnni` |
| 5 | `avx512_base` | `avx512f avx512bw` |
| 6（兜底） | `avx2` | `avx2` |

可以看到需求是严格递增累加的：每一档都包含了下一档的全部要求再加一个新能力。`amx` 档最特殊，它要求 AMX 三件套**加**完整 AVX512，缺一不可。

匹配循环还做了一件细致的事：兼容「带下划线」与「不带下划线」两种写法（[_cpu_detect.py:86-101](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L86-L101)）。因为 `/proc/cpuinfo` 里写的是 `avx512_bf16`（带下划线），而有些工具写 `avx512bf16`（不带），代码对每个 flag 都同时尝试 `flag` 和 `flag.replace("_","")` 两种形式，任一命中即算通过。

最后是三道兜底防线（[_cpu_detect.py:103-162](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L103-L162)）：① Linux 上若无任何高级特性则返回 `avx2`；② 若 `/proc/cpuinfo` 文件不存在（`FileNotFoundError`），改用第三方 `cpufeature` 包做跨平台探测；③ 连 `cpufeature` 都装不上，则 `ImportError` 分支也返回 `avx2`。三层 try/except 保证这个函数**几乎不可能抛异常**——最差也能给一个 `avx2`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看 `detect_cpu_features()` 如何读取本机 CPU 并给出判定。
2. **操作步骤**：

   在 kt-kernel 已安装的环境下，新建一个脚本 `probe.py`（示例代码，非项目原有文件）：

   ```python
   # 示例代码
   import os
   os.environ["KT_KERNEL_DEBUG"] = "1"          # 打开调试输出
   from kt_kernel._cpu_detect import detect_cpu_features

   variant = detect_cpu_features()
   print("===> 最终选中的变体：", variant)
   ```

   运行：`python probe.py`
3. **需要观察的现象**：终端会打印 `[kt-kernel] Detected xxx support via /proc/cpuinfo` 和 `Matched flags: ...`，最后打印 `===> 最终选中的变体： amx`（或你机器实际支持的那一档）。
4. **预期结果**：在一台 Sapphire Rapids / Zen 4 级别、带 AMX 的机器上应得到 `amx`；在较老的 Skylake-X 上可能得到 `avx512_base`；在 Apple Silicon 等 ARM 机器上（无 `/proc/cpuinfo`），会走 `cpufeature` 或直接 `avx2`。**具体取值取决于本机硬件，待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `detect_cpu_features()` 只读 `/proc/cpuinfo` 里第一个 CPU 核的 `flags`，而不是逐核检查？

> **参考答案**：因为同一颗物理 CPU 的所有核能力一致（异构 big.LITTLE 在 x86 服务器上极少见），取第一个核即可代表整机；逐核检查既无必要又拖慢 import。

**练习 2**：一台 CPU 的 flags 里有 `avx512f`、`avx512bw`、`avx512_vnni`，但**没有** `amx_*` 也没有 `avx512_bf16`。`detect_cpu_features()` 会返回什么？为什么？

> **参考答案**：返回 `avx512_vnni`。因为它满足第 4 档（`avx512f + avx512bw + avx512_vnni`），但不满足更高的 `avx512_vbmi`（缺 `avx512_vbmi`）。渐进匹配从最优往下找，第一个完全满足的就是它。

---

### 4.2 扩展加载

#### 4.2.1 概念说明

探测出变体名之后，下一步是**把对应的 `.so` 文件真正加载成 Python 模块**。这里的难点在于：同一个 kt-kernel 安装可能有两种截然不同的磁盘形态。

- **多变体构建**（`CPUINFER_BUILD_ALL_VARIANTS=1`，官方 PyPI wheel 默认）：wheel 里有 6 份 `.so`，文件名形如 `_kt_kernel_ext_amx.cpython-311-x86_64-linux-gnu.so`，文件名里带变体后缀。
- **单变体构建**（用户源码编译、未开多变体）：wheel 里只有 1 份 `.so`，文件名形如 `kt_kernel_ext.cpython-311-x86_64-linux-gnu.so`，不带变体后缀。

`load_extension()` 必须同时兼容这两种形态，并且无论加载哪一份 `.so`，对外暴露的模块名都得统一——因为 C++ 侧所有变体都用同一个 `PYBIND11_MODULE(kt_kernel_ext, m)` 注册（见 [ext_bindings.cpp:482](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp#L482)），导出的初始化函数都是 `PyInit_kt_kernel_ext`。

> 这里有个精妙的「改名但保真」设计：构建时每份 `.so` 都被重命名加上变体后缀（见 setup.py 的 `build_multi_variants`，[setup.py:401-419](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L401-L419)），但文件内部的 `PyInit_kt_kernel_ext` 符号不变。所以加载时用 `importlib` 按「模块名 `kt_kernel_ext`」去装载任意一份带后缀的文件都能成功。

#### 4.2.2 核心流程

```
load_extension(variant):
  在 kt_kernel 包目录下 glob "_kt_kernel_ext_{variant}.*.so"
  ├─ 找到（多变体）→ 取第一个匹配
  └─ 没找到 → 再 glob "kt_kernel_ext.*.so"（单变体）
       ├─ 找到 → 用单变体 .so
       └─ 也没有 → 抛 ImportError

  用 importlib 按 "kt_kernel_ext" 模块名手动装载该 .so
  执行 ext = module_from_spec(...); loader.exec_module(ext)
  返回 ext
```

#### 4.2.3 源码精读

文件名约定与注释（[_cpu_detect.py:190-193](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L190-L193)）说明了两种命名和「都导出 `PyInit_kt_kernel_ext`」这一关键事实。

定位包目录用的是 `__file__` 而非 import（[_cpu_detect.py:196-198](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L196-L198)）。注释明确指出「这里不能 `import kt_kernel`，否则循环导入」，因为此刻 `kt_kernel/__init__.py` 还没执行完。所以它靠 `__file__`（即 `_cpu_detect.py` 自身路径）反推出包目录——而包目录是 `python/`，这由 `pyproject.toml` 的 `package-dir` 把 `python` 映射成 `kt_kernel` 决定（[pyproject.toml:68-69](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L68-L69)）。`.so` 就被安装在这个目录下，所以 `dirname(__file__)` 正好是找 `.so` 的起点。

两段式 glob 查找（[_cpu_detect.py:200-215](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L200-L215)）：先找带变体后缀的 `_kt_kernel_ext_{variant}.*.so`，找不到再退而求其次找单变体 `kt_kernel_ext.*.so`，两者都空才抛带详细 pattern 的 `ImportError`。

手动装载 `.so`（[_cpu_detect.py:222-233](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L222-L233)）：用 `importlib.util.spec_from_file_location("kt_kernel_ext", so_file)` 创建模块规格，再 `module_from_spec` + `loader.exec_module` 完成装载。这里把模块名硬编码为 `kt_kernel_ext`，与 C++ 侧 `PYBIND11_MODULE(kt_kernel_ext, m)` 对齐，这样 pybind11 才能在 `exec_module` 时正确找到 `PyInit_kt_kernel_ext`。

而 `__init__.py` 拿到这个 `ext` 之后，会把它**同时注册到两个 sys.modules 键**下，保证包内其他模块无论用哪种 import 写法都能找到它（[__init__.py:44-50](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py#L44-L50)）。

#### 4.2.4 代码实践

1. **实践目标**：观察多变体 `.so` 在磁盘上的真实文件名，并验证 `load_extension` 能否把它们各自加载成功。
2. **操作步骤**：

   先找到安装目录里的 `.so`（示例命令，需在本机有 kt-kernel 安装时运行）：

   ```bash
   # 示例命令
   python -c "import kt_kernel, os; d=os.path.dirname(kt_kernel.__file__); print(d)"
   ls <上面打印的目录> | grep -E "kt_kernel_ext.*\.so"
   ```

   然后写脚本逐个装载（示例代码）：

   ```python
   # 示例代码
   from kt_kernel._cpu_detect import load_extension
   for v in ["amx", "avx512_bf16", "avx512_vnni", "avx2"]:
       try:
           ext = load_extension(v)
           print(f"{v:12s} -> OK, module name =", ext.__name__)
       except Exception as e:
           print(f"{v:12s} -> FAIL:", type(e).__name__)
   ```
3. **需要观察的现象**：在多变体 wheel 里，应能看到形如 `_kt_kernel_ext_amx.cpython-3XX-x86_64-linux-gnu.so` 等多个文件；脚本对每个变体打印 `OK, module name = kt_kernel_ext`（注意：无论哪份 `.so`，`ext.__name__` 都是 `kt_kernel_ext`）。若某变体在磁盘上不存在，会先打印 `Falling back ...` 再尝试更低的变体（回退链，见 4.3）。
4. **预期结果**：多变体安装下，每个被请求的变体若存在则成功、`__name__` 统一为 `kt_kernel_ext`；单变体安装下，无论请求哪个变体最终都会落到唯一的 `kt_kernel_ext.*.so`。**实际文件清单与是否带 CUDA 有关，待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_extension` 必须用 `importlib.util.spec_from_file_location` 手动装载，而不是直接 `import _kt_kernel_ext_amx`？

> **参考答案**：因为这些 `.so` 是数据文件式的产物，文件名里带变体后缀，本身不是合法的 Python 标识符，也没有对应的 Python 源文件让普通 import 机制找到它们。手动 `spec_from_file_location` 可以用任意文件路径装载，并用统一的模块名 `kt_kernel_ext` 注册——这正是 pybind11 在 C++ 里注册的名字。

**练习 2**：`ext.__name__` 为什么无论加载哪一份变体 `.so` 都是 `kt_kernel_ext`？

> **参考答案**：因为所有变体的 C++ 源码都用同一个 `PYBIND11_MODULE(kt_kernel_ext, m)` 注册模块，导出的初始化函数都是 `PyInit_kt_kernel_ext`。`.so` 文件名虽然变了（带 `_amx`/`_avx2` 后缀），但内部注册的模块名没变。`spec_from_file_location("kt_kernel_ext", ...)` 也正是用这个名字去匹配 `PyInit_` 符号。

---

### 4.3 回退链

#### 4.3.1 概念说明

设想一个矛盾场景：`detect_cpu_features()` 探测出本机是 `amx`，但用户装的是一个**只编译了 avx2 单变体**的旧 wheel，磁盘上根本没有 `_kt_kernel_ext_amx.so`。如果直接报错，用户体验会很差。

「回退链」就是为这种情况设计的保险：**当目标变体加载失败时，自动退到下一档更低的变体重试，直到成功或彻底无路可退**。这条链严格对应 4.1 里那张优先级表——每一档失败就退到紧邻的下一档。

整条链的终点是 `avx2`：

```
amx → avx512_bf16 → avx512_vbmi → avx512_vnni → avx512_base → avx2 → 单变体
```

注意链尾还有一个隐藏环节：当所有带后缀的变体 `.so` 都找不到时，`load_extension` 还会尝试单变体 `kt_kernel_ext.*.so`（见 4.2 的 glob 逻辑），所以「单变体构建」其实是链的最末端兜底。

#### 4.3.2 核心流程

```
load_extension(variant) 失败（ImportError / ModuleNotFoundError / FileNotFoundError）时：
  查 fallback_chain 字典得到下一档 next_variant
  ├─ next_variant 存在 → 递归调用 load_extension(next_variant)
  └─ next_variant == None（即 variant 已是 avx2，无更低的了）
       → 抛出最终 ImportError，提示「kt_kernel 未正确安装」
```

因为是用**递归**实现的，所以失败会逐级下探，每一级都重新走完整的「glob → 装载」流程，直到某一档成功或触底。

#### 4.3.3 源码精读

回退链的数据结构是一个字典（[_cpu_detect.py:240-247](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L240-L247)），明确写出每一档退到哪一档，`avx2` 的值是 `None`，标记「到底了」。

递归回退与触底报错（[_cpu_detect.py:249-263](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L249-L263)）：捕获 `(ImportError, ModuleNotFoundError, FileNotFoundError)` 后查链，能继续就 `return load_extension(next_variant)`（递归），不能继续（`next_variant is None`）就抛出带原始错误信息的 `ImportError`，提示用户「通常是 kt_kernel 包未正确安装」。

把三步串起来的总入口 `initialize()`（[_cpu_detect.py:266-294](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L266-L294)）：它先 `detect_cpu_features()` 得到变体，再 `load_extension(variant)` 得到模块，返回 `(ext, variant)` 二元组。而 `__init__.py` 在 import 最开头就解包这个二元组（[__init__.py:38-41](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py#L38-L41)），把模块存为 `_kt_kernel_ext`、把变体名存为包级属性 `__cpu_variant__`。这就是为什么用户能 `import kt_kernel; print(kt_kernel.__cpu_variant__)` 看到结果。

`KT_KERNEL_DEBUG` 的开关散布在三个函数里（例如 [_cpu_detect.py:46-47](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L46-L47)、[_cpu_detect.py:285-292](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py#L285-L292)），每一步（覆盖、探测、匹配、装载、回退）都会打印 `[kt-kernel] ...` 日志，是排查加载问题的第一手段。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：用 `KT_KERNEL_DEBUG` 观察完整检测与加载过程；再用 `KT_KERNEL_CPU_VARIANT` 强制覆盖，对比两次差异。
2. **操作步骤**：

   **第一次**（自动检测）——开一个**新的** shell 进程：

   ```bash
   # 示例命令
   KT_KERNEL_DEBUG=1 python -c "import kt_kernel; print('VARIANT =', kt_kernel.__cpu_variant__)"
   ```

   **第二次**（强制覆盖为 avx2）——再开一个**新的** shell 进程：

   ```bash
   # 示例命令
   KT_KERNEL_DEBUG=1 KT_KERNEL_CPU_VARIANT=avx2 python -c "import kt_kernel; print('VARIANT =', kt_kernel.__cpu_variant__)"
   ```
3. **需要观察的现象**：

   - 第一次会打印类似：`[kt-kernel] Detected amx support via /proc/cpuinfo` → `[kt-kernel] Selected CPU variant: amx` → `[kt-kernel] Loading amx from: .../_kt_kernel_ext_amx....so` → `[kt-kernel] Successfully loaded AMX variant`，最后 `VARIANT = amx`。
   - 第二次会打印：`[kt-kernel] Using environment override: avx2`（注意：覆盖分支**不会**再去读 `/proc/cpuinfo`）→ `[kt-kernel] Loading avx2 from: .../_kt_kernel_ext_avx2....so`，最后 `VARIANT = avx2`。
4. **预期结果**：两次 `__cpu_variant__` 不同（分别是本机最优档与强制 `avx2`），日志里第二次会出现 `Using environment override` 而**没有** `Detected ... via /proc/cpuinfo`，这正好印证 4.1.3 里「环境变量覆盖最高优先级、命中即返回」的逻辑。
5. **若想触发回退链**：在一台 amx 机器上，`KT_KERNEL_CPU_VARIANT=avx512_vnni KT_KERNEL_DEBUG=1` 重复实验——若你的 wheel 恰好缺该变体（少见，多为自定义单变体构建），会看到 `[kt-kernel] Falling back from avx512_vnni to avx512_base`。官方多变体 wheel 通常六个变体齐全，可能看不到回退；**是否能看到取决于具体安装，待本地验证。**

> **小贴士**：必须「新开 shell 进程」而不是在同一个 Python 里改 `os.environ`，因为 `_cpu_detect` 只在首次 import 时执行一次，进程内后改环境变量不会重新检测。

#### 4.3.5 小练习与答案

**练习 1**：回退链用递归实现。如果用户强制设了 `KT_KERNEL_CPU_VARIANT=amx` 但磁盘上只有 `avx2` 这一份变体，递归会经过哪些步骤？最终加载哪个变体？

> **参考答案**：`detect_cpu_features()` 因覆盖直接返回 `amx` → `load_extension("amx")` 找不到 `_kt_kernel_ext_amx.so` 抛 `ImportError` → 查链退到 `avx512_bf16` 再失败 → 依次 `avx512_vbmi` → `avx512_vnni` → `avx512_base` → `avx2`。若 `avx2` 那份存在则加载它并成功；若连 `avx2` 都没有，`next_variant` 为 `None`，抛最终 `ImportError`。最终加载的是 `avx2`（或单变体 `kt_kernel_ext.*.so`）。

**练习 2**：为什么把 `avx2` 设为链的终点（`fallback_chain["avx2"] = None`），而不是再往下设一个「纯标量」兜底？

> **参考答案**：`avx2` 是 x86-64 上几乎人人支持的基线指令集（Haswell 2013 之后普及），再往下没有更通用的向量加速档可退。所以 `avx2` 既是检测的兜底返回值，也是回退链的终点；若它都加载失败，说明安装本身坏了，只能抛错让用户重装，没有更低的档可以掩盖问题。

---

## 5. 综合实践

把三个最小模块串起来：**画一张完整的「import 时序图」并用调试日志验证它**。

任务步骤：

1. 画出从 `import kt_kernel` 到 `kt_kernel.__cpu_variant__` 可读的完整时序，至少包含以下节点，并标注每一步发生在哪个文件：
   - `kt_kernel/__init__.py` 调用 `_initialize_cpu()`；
   - `detect_cpu_features()` 读 `KT_KERNEL_CPU_VARIANT` → 读 `/proc/cpuinfo` → 匹配优先级表 → 返回变体名；
   - `load_extension()` glob 两类 `.so` → `importlib` 装载 → 失败则查 `fallback_chain` 递归回退；
   - `__init__.py` 把模块注册进 `sys.modules`、把变体名存为 `__cpu_variant__`。
2. 用以下命令（示例命令）跑两次，把日志贴到时序图对应节点上做对照：

   ```bash
   # 示例命令：自动检测
   KT_KERNEL_DEBUG=1 python -c "import kt_kernel as k; print(k.__cpu_variant__, k.__version__)"
   # 示例命令：强制覆盖 + 故意制造一个不存在的变体看回退
   KT_KERNEL_DEBUG=1 KT_KERNEL_CPU_VARIANT=avx512_vbmi python -c "import kt_kernel as k; print(k.__cpu_variant__)"
   ```
3. 在图中用不同颜色（或标注）区分三种来源的判定：**环境变量覆盖**、**`/proc/cpuinfo` 自动探测**、**回退链兜底**。
4. 验收标准：你能指着日志里的每一行，说清它对应时序图的哪一步、由哪个函数（`detect_cpu_features` / `load_extension` / `initialize`）打印。

> 如果本机没有装 kt-kernel，可以把第 2 步降级为「源码阅读型实践」：只读 [_cpu_detect.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/_cpu_detect.py) 与 [__init__.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py)，手工推导在「amx 机器 + 单变体 avx2 wheel」组合下日志会怎么打印，写出预期的每一行 `[kt-kernel] ...`。

## 6. 本讲小结

- **import 即探测**：`kt_kernel/__init__.py` 在最开头调用 `_cpu_detect.initialize()`，返回 `(扩展模块, 变体名)`，变体名被存为 `kt_kernel.__cpu_variant__`。
- **三级判定优先级**：`KT_KERNEL_CPU_VARIANT` 环境变量覆盖 > `/proc/cpuinfo` 自动渐进匹配 > `cpufeature`/`avx2` 兜底，三级 try/except 几乎不抛异常。
- **六档渐进变体表**：`amx → avx512_bf16 → avx512_vbmi → avx512_vnni → avx512_base → avx2`，每档需求严格包含下一档，第一个被 CPU 能力完全覆盖的就是最优解。
- **双形态 `.so` 兼容**：`load_extension` 先找多变体 `_kt_kernel_ext_{variant}.*.so`，再退到单变体 `kt_kernel_ext.*.so`；所有 `.so` 内部都注册为同名模块 `kt_kernel_ext`。
- **递归回退链**：目标变体加载失败时按优先级表逐档下探，`avx2` 为终点，触底才报「未正确安装」。
- **排查两件套**：`KT_KERNEL_DEBUG=1` 看全过程日志，`KT_KERNEL_CPU_VARIANT=xxx` 强制指定变体做对照实验，二者必须在**新进程**启动前设置。

## 7. 下一步学习建议

本讲解决的是「`.so` 是怎么被选中和装载的」，但你还没看过装载进来的这个 C++ 扩展模块**内部到底暴露了哪些 Python 接口**。接下来的进阶层会从两个方向深入：

- **若关心 Python 推理 API**：进入单元 4，先读 [u4-l1 KTMoEWrapper 工厂与后端分发]——它正是建立在本讲加载的 `kt_kernel_ext` 之上，用工厂模式把请求分发到 AMX/Native/Llamafile/General 各后端。
- **若关心 C++ 绑定细节**：进入单元 8，先读 [u8-l1 pybind11 绑定层]——它会精读 [ext_bindings.cpp](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp) 的 `PYBIND11_MODULE(kt_kernel_ext, m)`，讲清 `CPUInfer`、`MOEConfig`、各 `*_MOE` 类是如何被导出给 Python 的。

此外，建议把本讲的 `detect_cpu_features()` 与上一篇 u2-l2 的构建期 `CPU_FEATURE_MAP`（[setup.py:113](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L113)）对照阅读，理解「构建期翻译开关」与「运行期读取标志」如何用同一套能力名词把多份 `.so` 与本机 CPU 串起来。
