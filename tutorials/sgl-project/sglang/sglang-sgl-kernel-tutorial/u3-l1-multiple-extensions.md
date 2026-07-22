# 多扩展拆分：common_ops / flash_ops / infllm_ops / flashmla_ops / spatial_ops

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 sgl-kernel 为什么不把所有 CUDA 算子塞进**一个** `.so`，而是拆成五个独立的 Python 扩展模块；
- 在 `CMakeLists.txt` 里定位每个扩展对应的构建 target、源文件列表、编译开关与安装目录；
- 解释 `REGISTER_EXTENSION(name)` 宏到底生成了什么、为什么 Python 能把一个 `.so` 当成模块来 `import`；
- 区分两种截然不同的注册风格：加入 `torch.ops.sgl_kernel` 命名空间（四个扩展）vs 纯 pybind11 直调（`infllm_ops`），以及为什么后者要单独链接 `torch_python`。

本讲承接 u1-l3（构建系统：一份 SOURCES、sm90/sm100 双产物）与 u2-l2（`TORCH_LIBRARY_FRAGMENT` + `m.def/m.impl` 注册机制），把视角从「单个算子」拉高到「整个扩展模块」。

## 2. 前置知识

在进入本讲前，先用三句话回顾两个前置概念（细节见 u1-l3、u2-l2）：

- **Python 扩展模块（extension module / `.so`）**：C++/CUDA 代码被编译成一个动态库，文件名形如 `common_ops.cpython-312-x86_64-linux-gnu.so`，可以被 Python 当作一个普通模块 `import`。要让 Python 识别它，这个 `.so` 必须导出一个名为 `PyInit_<模块名>` 的入口函数——这是 CPython 的硬性约定。
- **`TORCH_LIBRARY_FRAGMENT(sgl_kernel, m)`**：PyTorch 提供的「命名空间片段」注册宏。它可以在**多个** `.so` 里被反复调用，每次调用都往同一个全局算子库 `sgl_kernel` 里**追加**算子。这就是「多个 `.so` 共享 `torch.ops.sgl_kernel` 命名空间」的基础。
- **CMake target**：在 `CMakeLists.txt` 里，`Python_add_library(<名字> MODULE ...)` 会定义一个「编译产物为 `.so`」的构建目标。一个 target 对应一个 `.so`，因此「五个扩展」=「五个 CMake target」=「五个 `.so`」。

一句话点题：**拆扩展的核心动机不是「算子太多装不下」，而是「符号隔离 + 可选编译 + 独立编译参数」。**

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `CMakeLists.txt` | 定义五个扩展的构建 target、源文件、编译开关、安装目录 |
| `csrc/common_extension.cc` | 主扩展 `common_ops` 入口：注册绝大多数算子 |
| `csrc/flash_extension.cc` | FA3 Flash Attention 扩展 `flash_ops` 入口 |
| `csrc/spatial_extension.cc` | green context（SM 分区）扩展 `spatial_ops` 入口 |
| `csrc/flashmla_extension.cc` | FlashMLA 扩展 `flashmla_ops` 入口 |
| `csrc/infllm_v2/flash_extension.cc` | InfLLM-v2 注意力扩展 `infllm_ops` 入口（纯 pybind，**不**走 torch op 注册） |
| `include/sgl_kernel_ops.h` / `include/sgl_flash_kernel_ops.h` | `REGISTER_EXTENSION` 宏定义 |
| `python/sgl_kernel/load_utils.py` | 运行期按 GPU 架构加载 `common_ops` |
| `python/sgl_kernel/flash_attn.py` / `spatial.py` / `flash_mla.py` / `infllm_v2/_loader.py` | 其余四个扩展的「延迟加载器」 |

## 4. 核心概念与源码讲解

### 4.1 扩展拆分动机与符号隔离

#### 4.1.1 概念说明

初学者常有的疑问是：「既然 `TORCH_LIBRARY_FRAGMENT` 可以不断追加算子，那为什么不把所有 `.cu` 都编进 `common_ops` 一个 `.so`，省事？」

sgl-kernel 选择拆成五个 `.so`，有三条具体理由：

1. **C++ 符号隔离（最主要）**。sgl-kernel 同时 vendored（内嵌）了多套各自独立的 Flash Attention 实现：自己的 `flash_ops`、DeepSeek 系的 FlashMLA、以及 InfLLM-v2 的注意力后端。这些代码都使用 `flash::` 这一类 C++ 命名空间，函数签名甚至类名会**撞车**。如果把它们链进同一个 `.so`，链接器会因为「一个符号有多个定义」（ODR 冲突）而报错或选错实现。拆成各自独立的 `.so` 后，每个动态库有独立的符号表，且 CPython 默认以 `RTLD_LOCAL` 方式 `dlopen`，符号互不可见，冲突自然消失。

2. **可选编译**。FA3（Flash Attention 3）只在特定 CUDA 版本与架构上才有意义，且编译耗时长、`ptxas` 容易爆。把它做成一个独立的、用开关控制的 target，能让不需要 FA3 的用户/平台跳过整段编译。

3. **独立编译参数**。不同扩展需要不同的 NVCC 编译选项（如 `-use_fast_math`、`-std=c++20`、专属的 `gencode`、不同的宏开关）。CMake 里一个 target 一套 `target_compile_options`，比在一个大 target 里用条件宏硬塞要清晰得多。

#### 4.1.2 核心流程

一个扩展从「源文件」到「可被 `import` 的模块」，固定走这五步：

```text
源文件列表 (*.cc/*.cu)
   │  Python_add_library(<target> MODULE ...)
   ▼
CMake target（一个 target = 一个 .so）
   │  target_compile_options / target_include_directories / target_link_libraries
   ▼
install(TARGETS ... LIBRARY DESTINATION sgl_kernel[/sm90|sm100])
   │  打包进 wheel，落到 sgl_kernel 安装目录
   ▼
Python 端 import（直接 import 或 importlib 按路径加载）
   │  执行 PyInit_<name>，触发 .so 内静态全局对象构造
   ▼
TORCH_LIBRARY_FRAGMENT 块执行 → 算子注册进 torch.ops.sgl_kernel
```

关键点：**「import 这个 `.so`」本身就是「注册算子」的触发器**。`.so` 被加载时，C++ 的静态全局对象（`TORCH_LIBRARY_FRAGMENT` 宏背后生成的那个）会自动执行，把 `m.def/m.impl` 登记的算子写进全局调度器。所以「加载模块」和「算子可用」是同一件事。

#### 4.1.3 源码精读

先看 CMake 里五个扩展的「地界」。`common_ops` 是唯一产生**两份**产物（sm90 / sm100）的扩展，承接 u1-l3 讲过的双 target 设计：

[sgl-kernel/CMakeLists.txt:L320-L346](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L320-L346) —— 用同一份 `SOURCES` 构建出 `common_ops_sm90_build`（带 `-use_fast_math`）与 `common_ops_sm100_build`（精确数学）两个 target，`OUTPUT_NAME` 都叫 `common_ops`，靠 `LIBRARY_OUTPUT_DIRECTORY` 的 `sm90/`、`sm100/` 子目录区分。

[sgl-kernel/CMakeLists.txt:L382-L383](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L382-L383) —— `common_ops` 安装到两个子目录 `sgl_kernel/sm90` 与 `sgl_kernel/sm100`，运行期由 `load_utils.py` 二选一加载。

`infllm_ops` 是理解「符号隔离」的最佳样本，CMake 顶部的注释把动机写得非常直白：

[sgl-kernel/CMakeLists.txt:L479-L486](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L479-L486) —— 明确说明 `infllm_ops` 是「独立模块，使其 `flash::` 符号与 sgl-kernel 自身的 flash attention 隔离」，并且只编译 hdim 64/128 的 bf16 前向（推理专用，故意的，省编译量）。

再看 `infllm_ops` 与其余四个的**注册方式根本不同**。其余四个用 `TORCH_LIBRARY_FRAGMENT` + `REGISTER_EXTENSION`：

[sgl-kernel/csrc/flash_extension.cc:L21-L21](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/flash_extension.cc#L21-L21) —— `TORCH_LIBRARY_FRAGMENT(sgl_kernel, m)` 打开命名空间片段，向公共的 `sgl_kernel` 库追加 `fwd`、`get_scheduler_metadata` 等算子。

而 `infllm_ops` 走的是纯 pybind11，**不**加入 `torch.ops.sgl_kernel`：

[sgl-kernel/csrc/infllm_v2/flash_extension.cc:L54-L57](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/infllm_v2/flash_extension.cc#L54-L57) —— `PYBIND11_MODULE(infllm_ops, m)` 直接把 C++ 函数 `mha_varlen_fwd_stage1` 暴露成 Python 属性 `infllm_ops.varlen_fwd_stage1`，Python 侧用 `infllm_ops.varlen_fwd_stage1(...)` 直调，**不经过** `torch.ops.sgl_kernel`。

> 用 `grep -c REGISTER_EXTENSION csrc/infllm_v2/flash_extension.cc` 验证，结果是 `0`——这是五个入口文件里唯一不含 `REGISTER_EXTENSION` 的。

最后看 `spatial_ops` 与 `flashmla_ops` 这两个相对「小」的扩展 target，它们和 `infllm_ops` 一样无条件构建、都装到 `sgl_kernel` 顶层目录：

[sgl-kernel/CMakeLists.txt:L541-L550](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L541-L550) —— `spatial_ops` 只含 `greenctx_stream.cu` 与 `spatial_extension.cc` 两个源文件，独立成扩展是为了 green context（SM 分区）这条独立能力可被单独加载。

#### 4.1.4 代码实践

**实践目标**：把「扩展 → 入口文件 → 注册方式 → 是否可选 → 安装目录」整理成一张对照表，亲手把五个扩展的全貌画出来。

**操作步骤**：

1. 打开 `CMakeLists.txt`，分别定位 `common_ops_*_build`、`flash_ops`、`infllm_ops`、`spatial_ops`、`flashmla_ops` 五段 `Python_add_library(...)`；
2. 对每段，记录：target 名、入口 `.cc` 文件、`install(... DESTINATION ...)` 的目录、是否被 `if(...)` 守卫；
3. 打开对应入口 `.cc`，确认它用的是 `REGISTER_EXTENSION(name)` 还是 `PYBIND11_MODULE`；
4. 汇总成下表（这张表本身就是答案，供你对照自己的整理结果）：

| CMake target | Python 模块名 | 入口文件 | 注册方式 | 是否可选 | install 目录 |
|---|---|---|---|---|---|
| `common_ops_sm90_build` / `common_ops_sm100_build` | `common_ops` | `csrc/common_extension.cc` | `TORCH_LIBRARY_FRAGMENT` + `REGISTER_EXTENSION(common_ops)` | 必选（两份，按架构二选一） | `sgl_kernel/sm90`、`sgl_kernel/sm100` |
| `flash_ops` | `flash_ops` | `csrc/flash_extension.cc` | `TORCH_LIBRARY_FRAGMENT` + `REGISTER_EXTENSION(flash_ops)` | **可选**（`SGL_KERNEL_ENABLE_FA3`） | `sgl_kernel` |
| `infllm_ops` | `infllm_ops` | `csrc/infllm_v2/flash_extension.cc` | `PYBIND11_MODULE(infllm_ops, m)`（纯 pybind） | 必选 | `sgl_kernel` |
| `spatial_ops` | `spatial_ops` | `csrc/spatial_extension.cc` | `TORCH_LIBRARY_FRAGMENT` + `REGISTER_EXTENSION(spatial_ops)` | 必选（功能运行期可选） | `sgl_kernel` |
| `flashmla_ops` | `flashmla_ops` | `csrc/flashmla_extension.cc` | `TORCH_LIBRARY_FRAGMENT` + `REGISTER_EXTENSION(flashmla_ops)` | 必选（SM100 源条件加入） | `sgl_kernel` |

**需要观察的现象**：注意 `common_ops` 是唯一有「两份产物」的扩展（因为要按架构选 fast/precise math），其余四个都只有一份、统一装到 `sgl_kernel/` 顶层；注意 `infllm_ops` 这一行的「注册方式」与其余四行不同。

**预期结果**：你整理出的表应与上表一致；尤其能注意到 `infllm_ops` 是唯一的「纯 pybind」扩展。

**待本地验证**：若你已 `pip install sglang-kernel`，可用 `ls $(python -c "import sgl_kernel,os;print(os.path.dirname(sgl_kernel.__file__))")` 查看安装目录，确认 `sm90/`、`sm100/` 子目录与若干 `*.so` 是否真实存在（无 GPU 环境则跳过）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `flash_ops` 和 `infllm_ops` 强行合并进 `common_ops` 一个 `.so`，最可能在哪一步出错？

> **答案**：链接阶段。二者都 vendored 了带 `flash::` 命名空间的 Flash Attention 实现，同名符号会触发 ODR/重复定义冲突；即使侥幸链接通过，运行期也可能调用到「错误的那一份」实现。这正是拆扩展的首要动机。

**练习 2**：`common_ops` 为什么需要 `sm90`、`sm100` 两个子目录，而 `flash_ops` 只需要一个顶层目录？

> **答案**：`common_ops` 的两份产物是「同一份源码 + 不同数学精度（fast/precise）」的组合，运行期要按 GPU 架构二选一，故用目录区分；`flash_ops` 只有一份产物（其内部已用 `gencode` 覆盖多架构机器码），不需要按精度分目录，直接装到 `sgl_kernel/` 即可。这是 u1-l3「双产物 vs gencode」区分的延伸应用。

---

### 4.2 REGISTER_EXTENSION 宏

#### 4.2.1 概念说明

CPython 加载一个 `.so` 作为模块时，会去这个动态库里找一个名为 `PyInit_<模块名>` 的 C 函数，调用它来创建模块对象。这个函数是「`.so` 能否被 `import`」的入场券。

但 sgl-kernel 的算子注册全靠 `TORCH_LIBRARY_FRAGMENT`（它注册的是 torch 算子，不是 Python 模块属性），并不自然地产生一个 `PyInit_`。于是需要一个「空壳」模块入口——**只负责让 Python 认得这个 `.so`，注册算子的副作用交给静态全局对象去完成**。`REGISTER_EXTENSION(name)` 就是生成这个空壳入口的宏。

#### 4.2.2 核心流程

宏展开逻辑（伪代码）：

```text
REGISTER_EXTENSION(flash_ops)
   │  预处理器展开 CONCAT(PyInit_, flash_ops) → PyInit_flash_ops
   ▼
PyMODINIT_FUNC PyInit_flash_ops() {
    static struct PyModuleDef module = { ..., "flash_ops", ... };
    return PyModule_Create(&module);   // 返回一个几乎为空的模块对象
}
```

也就是说，`import flash_ops` 时：

1. CPython 找到并调用 `PyInit_flash_ops`；
2. 该函数返回一个**空模块对象**（没有任何 Python 属性）；
3. 但在 `.so` 被加载的过程中，所有 C++ 静态全局对象都已构造，`TORCH_LIBRARY_FRAGMENT(sgl_kernel, m){...}` 块已经执行，`fwd`、`get_scheduler_metadata` 等算子已注册进 `torch.ops.sgl_kernel`。

所以这个 `flash_ops` 模块对象本身「没有内容」并不重要——**它的存在就是注册算子的副作用证明**。真正调用算子时，走的是 `torch.ops.sgl_kernel.fwd.default(...)`，而不是 `flash_ops.fwd`。

`infllm_ops` 是反例：它用 `PYBIND11_MODULE`，会把 C++ 函数**直接绑成** Python 模块属性，所以调用时是 `infllm_ops.varlen_fwd_stage1(...)`，模块对象「有内容」、且不经过 torch 调度器。

#### 4.2.3 源码精读

宏定义在两个头文件里各有一份（内容相同）：

[sgl-kernel/include/sgl_kernel_ops.h:L43-L47](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h#L43-L47) —— `REGISTER_EXTENSION(NAME)` 用 `CONCAT(PyInit_, NAME)` 拼出入口函数名，用 `STRINGIFY(NAME)` 把名字塞进 `PyModuleDef`，最后 `PyModule_Create` 返回空壳模块。`CONCAT`/`STRINGIFY` 是经典的「先展开再粘贴」两段宏技巧（见同文件 L35-L39），保证传入的 `NAME` 会被展开而不是被当作字面量。

四个扩展各自在文件末尾调用它：

[sgl-kernel/csrc/common_extension.cc:L484-L484](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L484-L484) —— `REGISTER_EXTENSION(common_ops)`，生成 `PyInit_common_ops`。

[sgl-kernel/csrc/flash_extension.cc:L102-L102](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/flash_extension.cc#L102-L102) —— `REGISTER_EXTENSION(flash_ops)`，生成 `PyInit_flash_ops`。

> `spatial_extension.cc:29` 的 `REGISTER_EXTENSION(spatial_ops)`、`flashmla_extension.cc:127` 的 `REGISTER_EXTENSION(flashmla_ops)` 同理。

对照 `infllm_ops` 的不同写法：

[sgl-kernel/csrc/infllm_v2/flash_extension.cc:L54-L57](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/infllm_v2/flash_extension.cc#L54-L57) —— `PYBIND11_MODULE(infllm_ops, m)` 由 pybind11 自己生成 `PyInit_infllm_ops`，并 `m.def("varlen_fwd_stage1", &mha_varlen_fwd_stage1, ...)` 把函数绑成属性。**它没有、也不需要 `REGISTER_EXTENSION`。**

再看 Python 侧如何触发这些 `PyInit_`。`common_ops` 由 `load_utils.py` 用 `importlib` 按架构路径加载：

[sgl-kernel/python/sgl_kernel/load_utils.py:L48-L101](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/load_utils.py#L48-L101) —— 按 compute capability 选 `sm90`/`sm100` 子目录，`spec.loader.exec_module(common_ops)` 触发 `PyInit_common_ops` → 注册 common 算子。

其余三个 torch 风格扩展采用「延迟导入」（lazy import），在第一次用到时才 `import`：

[sgl-kernel/python/sgl_kernel/flash_attn.py:L7-L12](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/flash_attn.py#L7-L12) —— `from sgl_kernel import flash_ops`，注释式的副作用就是「触发 FA3 算子注册」。真正调用在 [L230](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/flash_attn.py#L230-L230) 的 `torch.ops.sgl_kernel.fwd.default(...)`。

[sgl-kernel/python/sgl_kernel/spatial.py:L5-L9](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/spatial.py#L5-L9) —— `from . import spatial_ops  # triggers TORCH extension registration`，注释直接点明「import 即注册」。

[sgl-kernel/python/sgl_kernel/flash_mla.py:L7-L11](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/flash_mla.py#L7-L11) —— `from sgl_kernel import flashmla_ops  # triggers TORCH extension registration`，并捕获导入失败、转换成友好的 `ImportError`。

而 `infllm_ops` 因为是 pybind 模块，需要直接拿到模块对象来调函数，所以专门写了一个更健壮的、按文件路径 `glob` 查找 `.so` 的加载器：

[sgl-kernel/python/sgl_kernel/infllm_v2/_loader.py:L70-L98](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/infllm_v2/_loader.py#L70-L98) —— `load_infllm_ops()` 先试普通 `import`，失败则在 `site-packages`、源码树等多处 `glob infllm_ops*.so`，再用 `importlib` 按路径加载，最后缓存返回。它返回的是「模块对象本身」（要直调 `.varlen_fwd_stage1`），而不像 torch 风格扩展那样只关心副作用。

#### 4.2.4 代码实践

**实践目标**：追踪「一行 Python 调用」背后，到底是哪个 `.so` 的 `PyInit_` 被触发、算子注册到了哪里。

**操作步骤**（源码阅读型，无需 GPU）：

1. 读 `python/sgl_kernel/__init__.py` 第 [L19](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L19-L19) 行 `common_ops = _load_architecture_specific_ops()`，确认 `import sgl_kernel` 时只有 `common_ops` 被**立即**加载；
2. 在 `flash_attn.py` 找到 `from sgl_kernel import flash_ops`，说明 `flash_ops` 是「用到 FA3 时才加载」；
3. 对比：调用 FA3 走 `torch.ops.sgl_kernel.fwd.default(...)`（torch 调度器），而调用 InfLLM-v2 在 [infllm_v2/attention.py:L57](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/infllm_v2/attention.py#L57-L57) 走 `infllm_ops.varlen_fwd_stage1(...)`（模块直调）。

**需要观察的现象**：两条调用路径形似实不同——前者 `import flash_ops` 只为副作用、调用却落在 `torch.ops.sgl_kernel`；后者 `load_infllm_ops()` 返回的模块对象本身就是调用入口。

**预期结果**：你能用自己的话讲清「为什么 `flash_ops` 模块对象是空的，而 `infllm_ops` 模块对象里有 `varlen_fwd_stage1`」。

**待本地验证**：若环境允许，可在 Python 里 `import sgl_kernel` 后执行 `import sgl_kernel.flash_ops as f; print(dir(f))`，应看到一个几乎没有公开属性的模块；再 `from sgl_kernel.infllm_v2._loader import load_infllm_ops; print(dir(load_infllm_ops()))`，应看到 `varlen_fwd_stage1`（无 GPU/未编译则报 `ImportError`，属正常）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `REGISTER_EXTENSION` 宏里要用 `CONCAT(PyInit_, NAME)` 两段宏（`_CONCAT` + `CONCAT`），而不是直接写 `PyInit_##NAME`？

> **答案**：当 `NAME` 本身是另一个宏时，直接 `##` 粘贴会阻止它被展开。两段宏技巧（`CONCAT` 先展开参数再交给 `_CONCAT` 粘贴）确保 `NAME` 先完全展开、再拼接。在本项目里 `NAME` 都是普通标识符（如 `flash_ops`），效果看似相同，但这是 C 预处理器里稳健写法的标准防御。

**练习 2**：`flash_ops`、`spatial_ops`、`flashmla_ops` 的模块对象都是「空壳」，那它们注册的算子到底存到哪里？为什么不会互相覆盖？

> **答案**：注册到 PyTorch 全局唯一的算子库 `sgl_kernel`（一个 namespace），由 `TORCH_LIBRARY_FRAGMENT` 的「片段」语义保证可被多次追加。每个扩展注册的**算子名不同**（`fwd`、`create_greenctx_stream_by_value`、`fwd_kvcache_mla`……），所以不覆盖；它们通过 `torch.ops.sgl_kernel.<算子名>` 统一访问。

---

### 4.3 可选扩展与编译开关

#### 4.3.1 概念说明

五个扩展里，只有 `flash_ops`（FA3）是「真正的可选」——它的整段构建被 `if(SGL_KERNEL_ENABLE_FA3)` 守卫。其余四个（`common_ops`、`infllm_ops`、`spatial_ops`、`flashmla_ops`）都是无条件构建的（其中 `flashmla_ops` 只是**内部** SM100 相关源文件条件加入，模块本身始终构建）。

FA3 之所以要做成可选项，原因有三：

- FA3 只在 sm80/sm86/sm89/sm90a 等特定架构上工作，且对 CUDA 版本有要求；
- FA3 实例化文件极多（按 hdim × dtype × 架构 glob 出一大批 `.cu`），编译耗时与显存占用远高于普通算子；
- 在 aarch64/arm64 等平台上 FA3 默认应关闭。

sgl-kernel 用 CMake 的 `option(...)` 机制，把「是否开启」做成一个可被命令行 `-D` 覆盖的缓存变量，并用一段默认值推导逻辑给出「合理的开箱默认」。

#### 4.3.2 核心流程

CMake 选项开关的标准三段式：

```text
① 推导默认值：根据 CUDA_VERSION / 架构 给出 DEFAULT_SGL_KERNEL_ENABLE_FA3
② 声明 option：option(SGL_KERNEL_ENABLE_FA3 "..." ${DEFAULT_...})  ← 可被 -D 覆盖
③ 用 if() 守卫 target：if(SGL_KERNEL_ENABLE_FA3) ... Python_add_library(flash_ops ...) ... endif()
```

`flashmla_ops` 的「条件性」体现在另一个层面：模块始终构建，但 SM100 专属的源文件列表用 `if(FLASHMLA_ENABLE_SM100)` 决定是否 `list(APPEND ...)`。

#### 4.3.3 源码精读

FA3 默认值推导：

[sgl-kernel/CMakeLists.txt:L108-L114](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L108-L114) —— 默认 `OFF`；若 `CUDA_VERSION >= 12.4` 且**非** aarch64/arm64，则默认 `ON`。

[sgl-kernel/CMakeLists.txt:L176-L176](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L176-L176) —— `option(SGL_KERNEL_ENABLE_FA3 "Enable FA3" ${DEFAULT_SGL_KERNEL_ENABLE_FA3})`，把上面的推导结果固化为可被 `-DSGL_KERNEL_ENABLE_FA3=OFF` 覆盖的缓存选项。同组的还有 `SGL_KERNEL_ENABLE_BF16/FP8/FP4/SM90A/SM100A` 等开关（L173-L179）。

FA3 整段构建被守卫，且其源文件用 `file(GLOB ...)` 按 hdim/dtype 分批收集（这也是 u7-l2 会展开的 FA3 编译策略）：

[sgl-kernel/CMakeLists.txt:L385-L477](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L385-L477) —— 整个 `flash_ops` target、`FA3_BF16/FP16/FP8_GEN_SRCS` 的 glob、`install(TARGETS flash_ops ...)` 全部包在 `if(SGL_KERNEL_ENABLE_FA3)` 内；关闭时这段完全不产生任何产物。

`flashmla_ops` 的条件性（模块必建、SM100 源条件加入）：

[sgl-kernel/cmake/flashmla.cmake:L37-L42](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/cmake/flashmla.cmake#L37-L42) —— 当 `CUDA_VERSION > 12.8` 时追加 sm100a 的 gencode 并 `set(FLASHMLA_ENABLE_SM100 ON)`。

[sgl-kernel/cmake/flashmla.cmake:L131-L149](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/cmake/flashmla.cmake#L131-L149) —— `if(FLASHMLA_ENABLE_SM100)` 才把 sm100 的 dense/sparse prefill/decode 源文件追加进 `FlashMLA_SOURCES`；而 `Python_add_library(flashmla_ops ...)`（L151）与 `install`（L182）在 `if` 之外，始终执行。

`infllm_ops` 与 `spatial_ops` 则是**无条件**构建（CMake 里没有 `if` 守卫它们的 `Python_add_library`），只是各自的源文件清单被精确裁剪过：`infllm_ops` 只列了 hdim 64/128 的 bf16 forward 四个 `.cu`（见 [CMakeLists.txt:L518-L525](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L518-L525)），`spatial_ops` 只有两个源文件（[L542-L545](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L542-L545)）。

#### 4.3.4 代码实践

**实践目标**：亲手确认 FA3 的「开箱默认」在什么平台/版本下开启，并理解「无条件构建 ≠ 功能一定可用」。

**操作步骤**：

1. 读 L108-L114 的默认值推导，回答：在一台 x86 + CUDA 12.4 的机器上，FA3 默认开还是关？在一台 aarch64 + CUDA 12.8 的机器上呢？
2. 读 L388 的 `if(SGL_KERNEL_ENABLE_FA3)`，确认若用户执行 `cmake -DSGL_KERNEL_ENABLE_FA3=OFF ...`，`flash_ops` 这个 `.so` 是否还会生成；
3. 读 `flashmla.cmake` L37-L42 与 L131-L149，区分「模块构建（无条件）」与「SM100 源加入（条件）」两件事。

**需要观察的现象**：注意「编译开关」有两层粒度——`flash_ops` 是「整个扩展是否构建」，`flashmla_ops` 是「扩展内部分架构源文件是否纳入」。

**预期结果**：

- x86 + CUDA ≥ 12.4 → FA3 默认 `ON`，`flash_ops` 会构建；
- aarch64/arm64 → FA3 默认 `OFF`，`flash_ops` 不构建（即便 CUDA 版本足够）；
- `-DSGL_KERNEL_ENABLE_FA3=OFF` 可在任何平台强制关闭 FA3；
- `flashmla_ops` 总会构建，但只有 CUDA > 12.8 时才包含 SM100 内核。

**待本地验证**：实际是否生成 `flash_ops*.so` 取决于你的构建环境，可用 `find build -name "flash_ops*.so"` 在构建后确认（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`spatial_ops` 在 CMake 里没有 `if` 守卫，但注释称它是 "optional extension for green contexts"。这里的 "optional" 指什么？

> **答案**：指**运行期**使用上的可选——green context（把 GPU 的 SM 划分给不同流）是一项按需启用的能力，不用的模型完全不碰它；但它的**编译**是无条件进行的，`.so` 总会被装进 wheel。要区分「编译期可选」（`flash_ops`）与「运行期可选」（`spatial_ops`）。

**练习 2**：假设你想为本项目新增一个「只对 Blackwell(sm100) 有意义、且编译很慢」的扩展 `foo_ops`，应该参考五个扩展里的哪一个来写 CMake？

> **答案**：参考 `flash_ops`——用 `option(SGL_KERNEL_ENABLE_FOO ...)` 配一段默认值推导，再用 `if(SGL_KERNEL_ENABLE_FOO)` 守卫整段 `Python_add_library(foo_ops ...)`，把昂贵的、架构受限的构建隔离成可选项；入口 `.cc` 用 `TORCH_LIBRARY_FRAGMENT + REGISTER_EXTENSION(foo_ops)`，装到 `sgl_kernel/`。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「多扩展架构说明书」小任务：

1. **画一张加载时序图**。从 `import sgl_kernel` 开始，标出：
   - 立即加载的 `common_ops`（经 `load_utils._load_architecture_specific_ops`，按 sm90/sm100 二选一）；
   - 四个延迟加载点（`flash_attn.py` / `spatial.py` / `flash_mla.py` / `infllm_v2/_loader.py`）分别在「第一次用到对应功能时」才触发；
   - 每个加载点对应的注册方式（torch fragment 还是 pybind）。

2. **解释 `infllm_ops` 为何要单独链接 `torch_python`**。提示在 [CMakeLists.txt:L535-L538](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L535-L538)：`infllm_ops` 是纯 pybind 模块，其绑定的 C++ 函数 `mha_varlen_fwd_stage1` 形参含 `at::Generator`（随机数生成器，用于 dropout）。把 `at::Generator` 暴露给 Python 会牵出 `THPGeneratorClass`，而这个符号住在 `libtorch_python` 里——`find_package(Torch)` 给出的 `TORCH_LIBRARIES` **并不包含** `libtorch_python`，所以必须 `find_library(TORCH_PYTHON_LIBRARY torch_python ...)` 显式补链。其余四个扩展走 torch 算子调度器（`m.impl` 绑设备、不直接绑 `at::Generator` 到 Python），因此不需要这一步。

3. **对照检验**：用本讲 4.1.4 的五扩展对照表，自查你的时序图里每个扩展的「注册方式」「是否可选」「安装目录」是否都与表一致。

> 这个任务把「符号隔离（为什么拆）→ `REGISTER_EXTENSION`（怎么成为模块）→ 编译开关（哪些可选）」三条主线一次性贯通，是后续阅读 u7（Attention 各后端）与 u3-l2（多后端）的地图基础。

## 6. 本讲小结

- sgl-kernel 把算子拆成五个独立 `.so`（`common_ops`/`flash_ops`/`infllm_ops`/`flashmla_ops`/`spatial_ops`），首要动机是**隔离 vendored 的 `flash::` 符号**，其次是可选编译与独立编译参数。
- `common_ops` 是唯一产生**两份产物**（sm90 fast-math / sm100 precise）的扩展，运行期按 compute capability 二选一；其余四个各一份，统一装到 `sgl_kernel/`。
- 四个扩展用 `TORCH_LIBRARY_FRAGMENT(sgl_kernel, m)` + `REGISTER_EXTENSION(name)`，向公共命名空间追加算子，调用走 `torch.ops.sgl_kernel.<name>`；`infllm_ops` 是唯一的**纯 pybind** 扩展，用 `PYBIND11_MODULE`，调用走 `infllm_ops.varlen_fwd_stage1(...)`。
- `REGISTER_EXTENSION(name)` 只生成一个空壳 `PyInit_<name>` 入口；算子注册是 `.so` 被加载时的**副作用**，由静态全局对象完成。
- 只有 `flash_ops`（FA3）是「编译期可选」，由 `option(SGL_KERNEL_ENABLE_FA3)` 守卫，默认在 x86 + CUDA ≥ 12.4 开启、arm 关闭；`flashmla_ops` 总会构建，但 SM100 源条件加入。
- `infllm_ops` 因纯 pybind 绑定 `at::Generator`，需额外链接 `libtorch_python`（`TORCH_LIBRARIES` 不含它）；这是它与其余四个在链接期的关键差异。

## 7. 下一步学习建议

- 下一讲 **u3-l2 多后端支持**会从「同一扩展的不同后端变体」切入，讲 `common_extension.cc` / `common_extension_rocm.cc` / `common_extension_musa.cc` 与 CPU/Metal 入口如何并存——那是「横向多扩展（本讲）」之外的「纵向多后端」维度。
- 想深入某个扩展的算子本身，可按功能族跳读：FA3 细节见 u7-l2，FlashMLA/InfLLM 见 u7-l3，green context 见 u10-l2。
- 若你打算自己加一个新扩展，u11-l3「贡献一个新算子」会把本讲的 target/注册/install 三件套与六步贡献流程串成端到端清单。
