# 环境变量、调试与性能剖析

## 1. 本讲目标

本讲是「FP4、扩展与工程实践」单元的第四篇，也是整本手册的收官篇之一。前面九个单元带你从 Python 调用一路下钻到 tensor core 指令，但你可能已经注意到：DeepGEMM 是一个**运行时 JIT 编译**的库——每个新形状都会在首次调用时真正编译一个 GPU kernel。这意味着「我这次到底编译了什么、它为什么慢、它有没有用错寄存器」这三类问题，无法靠读静态代码回答，必须靠**运行期可观测性**。

DeepGEMM 把几乎所有可观测性旋钮都设计成了**环境变量**（`DG_*` / `DG_JIT_*`）。本讲的目标就是系统梳理这套环境变量体系，并把它和两类外部 NVIDIA 工具（`compute-sanitizer` 与 `nsight compute / ncu`）串起来。学完本讲，你应当能够：

1. 说出每一类 `DG_JIT_*` / `DG_*` 环境变量的作用，并能正确组合使用（例如「dump SASS」与「打开 lineinfo」该分别设哪个变量）；
2. 理解 `DG_JIT_DUMP_PTX/SASS/ASM` 与 `DG_JIT_WITH_LINEINFO` 在调优链路中的不同角色——前者产出可读的反汇编，后者给 NCU 提供「源代码行号 ↔ 指令」的映射；
3. 看懂 `tests/test_sanitizer.py` 如何用 `compute-sanitizer` 做 memcheck/synccheck、`scripts/run_ncu_mega_moe.sh` 如何多进程拉起 NCU 剖析，并理解 `DG_USE_NVIDIA_TOOLS` 这个「让位开关」为何必不可少；
4. 掌握一条完整的调优闭环：dump SASS → 用 `cuobjdump`/NCU 定位寄存器占用与热点 → 用 `set_tc_util` / `set_num_sms` 调参验证。

## 2. 前置知识

进入源码前，先建立三组通俗概念。

**环境变量 vs 函数参数。** 普通程序的可调旋钮写在代码里、当参数传。但 JIT 库的特殊之处是：**编译发生在 `import` 之后、运行期之中**，很多旋钮（比如要不要 dump 反汇编、用哪个 C++ 标准）只在你「跑这一次」时才有意义，不该写死在代码里。环境变量正好满足「每次运行可临时改、不改源码、不改安装包」的需求。Linux 下用 `DG_JIT_DEBUG=1 python xxx.py` 这种方式在进程启动前注入即可。

**`0` 表示默认——DeepGEMM 的统一约定。** 这套环境变量几乎全部遵循一个约定：整数型变量传 `0`（或不设）等于「关闭/默认」，传非零（通常 `1`）等于「开启」；字符串型变量留空等于「用默认值」。这个约定不是巧合，而是源于一个统一的读取函数 `get_env<T>`（下文精读）。所以你看到的判分支几乎都是朴素的真值判断 `if (get_env<int>("DG_JIT_DEBUG"))`。

**三个外部工具，三件事。** 调优 GPU kernel 离不开 NVIDIA 官方三件套：

- **`ptxas`**：NVCC/NVRTC 后端把 PTX（一种虚拟指令）编译成机器码 SASS 时用的汇编器。加 `--verbose` 它会打印「这个 kernel 用了多少寄存器、多少共享内存、有没有溢出到 local memory」。
- **`cuobjdump`**：把编译好的 cubin 反汇编成 SASS 文本，让你看到「真实跑在 GPU 上的每一条指令」。
- **`nsight compute`（命令行叫 `ncu`）**：运行期剖析器，能统计每个 kernel 的吞吐、访存延迟、stall 原因，并能借助 `-lineinfo` 把指令映射回源代码行。

如果你对 JIT 编译链路（`generate → build → compile → load`）还不熟，建议先回顾 [u3-l4](u3-l4-nvcc-vs-nvrtc.md)（NVCC vs NVRTC）与 [u4-l1](u4-l1-device-runtime-config.md)（DeviceRuntime 与运行时配置），本讲大量复用其中建立的 `compiler`、`device_runtime`、`flags`、`signature` 等概念。

## 3. 本讲源码地图

本讲围绕「环境变量在哪里被读取、被如何使用」展开，核心是三个文件：

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| `csrc/jit/compiler.hpp` | `Compiler` 基类构造函数（读取并组装绝大多数编译期环境变量）、`build()`（dump 开关）、`disassemble()`（调 cuobjdump）、编译后端二选一 | ✅ 精读 |
| `tests/test_sanitizer.py` | 用 `compute-sanitizer` 对每个 `test_*` 函数做 memcheck/synccheck 的驱动脚本，并注入一组协作环境变量 | ✅ 精读 |
| `README.md` | 环境变量的官方文档表（唯一的「权威清单」） | ✅ 精读 |

辅助理解、本讲只引用不精读的配套文件：

- `csrc/utils/system.hpp`：`get_env<T>()` 模板（环境变量统一读取入口）、`call_external_command()`（用 `popen` 跑外部命令）。
- `csrc/jit/kernel_runtime.hpp`：`DG_JIT_DEBUG` / `DG_JIT_PRINT_LOAD_TIME` 控制加载阶段的打印。
- `csrc/jit/device_runtime.hpp`：cuBLASLt 相关的 `DG_USE_PYTORCH_CUBLASLT_HANDLE` / `DG_USE_TEMP_CUBLASLT_WORKSPACE`。
- `csrc/jit_kernels/heuristics/common.hpp` 与 `mega_moe.hpp`：`DG_PRINT_CONFIGS` 打印选中的配置。
- `csrc/apis/mega.hpp`：`DG_COMM_KERNEL_DEBUG`（Mega MoE 调试用清零缓冲）。
- `deep_gemm/testing/bench.py`：`DG_USE_NVIDIA_TOOLS`（剖析时让位）。
- `scripts/run_ncu_mega_moe.sh`：多进程拉起 NCU 的剖析脚本。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲整套环境变量的「读取机制 + 分类地图 + 一个关键副作用」；再聚焦「dump 与 lineinfo」这条调优链路；最后讲 sanitizer 与 NCU 两类外部剖析如何与 DeepGEMM 协作。

### 4.1 环境变量体系：读取、分类与缓存键副作用

#### 4.1.1 概念说明

DeepGEMM 的环境变量数量并不少（README 列了 20 余个），但它们全部经过同一个入口读取，并按「影响编译期 / 仅影响运行期」自然分成两类。理解这一分类，比死记每一个变量更重要——因为它直接决定「我改了这个变量，会不会触发重新编译」。

所有变量最终服务于一个目标：**在不修改源码、不重新安装的前提下，临时改变这一次运行的可观测性与编译行为**。

#### 4.1.2 核心流程

一个环境变量从「你在 shell 里 export」到「影响 DeepGEMM 行为」，经过三步：

1. **读取**：C++ 侧用统一的 `get_env<T>(name, default)` 模板读 `std::getenv`，按 `T` 把字符串转成 `int` 或保持 `std::string`；缺省值是 `T()`（即 `0` 或空串），这就是「`0`/空 = 默认」约定的物理来源。
2. **分发到三类作用点**之一：
   - **编译期 flag**（如 `DG_JIT_CPP_STANDARD`→`-std=c++{n}`）：写进 `flags` 字符串，进而进入缓存键。
   - **运行期行为**（如 `DG_JIT_DUMP_SASS`、`DG_PRINT_CONFIGS`）：只在某个 `if` 里决定要不要多打印/多生成文件，不进缓存键。
   - **路径/句柄选择**（如 `DG_JIT_CACHE_DIR`、`DG_JIT_NVCC_COMPILER`）：在构造期决定去哪里编译、用哪个编译器。
3. **副作用评估**：若该变量进了 `flags`，则它会改变 `signature`，从而改变缓存目录名——**等于让所有形状重新编译一遍**。这是调优时最容易踩的坑（详见 4.1.5）。

#### 4.1.3 源码精读

**统一读取入口 `get_env<T>`。** 所有环境变量都从这里进来，见 [csrc/utils/system.hpp:L18-L33](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/system.hpp#L18-L33)。它对 `std::getenv` 做了薄封装：读不到就返回默认值 `dtype_t()`，`int` 用 `sscanf` 解析、`std::string` 原样返回。这正是「`get_env<int>("X")` 不写第二个参数时默认为 `0`」的原因——调用方据此把真值判断写成朴素的 `if (...)`。

**编译期 flag 的组装（最关键的一段）。** `Compiler` 基类构造函数读取了一批环境变量并拼成 `flags` 字符串，见 [csrc/jit/compiler.hpp:L55-L61](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L55-L61)：

```cpp
// C++ 标准来自环境变量，默认 20
flags = fmt::format("-std=c++{} --diag-suppress=... --ptxas-options=--register-usage-level=10",
                    get_env<int>("DG_JIT_CPP_STANDARD", 20));
// 调试类开关 → 让 ptxas 打印寄存器/共享内存/local memory 用量
if (get_env("DG_JIT_DEBUG", 0) or get_env("DG_JIT_PTXAS_VERBOSE", 0) or get_env("DG_JIT_PTXAS_CHECK", 0))
    flags += " --ptxas-options=--verbose,--warn-on-local-memory-usage";
// lineinfo → 给 NCU 等剖析器提供「指令 ↔ 源码行」映射
if (get_env("DG_JIT_WITH_LINEINFO", 0))
    flags += " -Xcompiler -rdynamic -lineinfo";
```

这段代码同时说明了三件事：`DG_JIT_CPP_STANDARD`（默认 20）被填进 `-std=c++{}`；`--register-usage-level=10` 是**全库固定**的硬约束（把寄存器用量压到最低、换最多活跃 warp，这是性能的基石，不受环境变量控制）；`DG_JIT_DEBUG` / `DG_JIT_PTXAS_VERBOSE` / `DG_JIT_PTXAS_CHECK` 三者任一为真，都会追加 `--ptxas-options=--verbose,...`。

**缓存目录的覆盖。** 默认落在 `$HOME/.deep_gemm`，可被 `DG_JIT_CACHE_DIR` 覆盖，见 [csrc/jit/compiler.hpp:L49-L51](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L49-L51)。这是字符串型变量的典型用法——空串即用默认。

**nvcc 路径覆盖。** `DG_JIT_NVCC_COMPILER` 可指定一个非默认的 `nvcc` 路径（默认取 `torch.utils.cpp_extension.CUDA_HOME`），见 [csrc/jit/compiler.hpp:L197-L198](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L197-L198)。

**编译后端二选一。** `DG_JIT_USE_NVRTC` 决定用进程内 NVRTC（快）还是外部 nvcc（稳），默认 nvcc，见 [csrc/jit/compiler.hpp:L354-L360](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L354-L360)。这条选择最终体现为 `signature` 写成 `NVCC12.9` 还是 `NVRTC12.9`——同样会进入缓存键（详见 u3-l4）。

**官方变量清单。** README 把全部环境变量分 6 组列出，是唯一的权威文档，见 [README.md:L159-L185](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L159-L185)。下表按本讲的「作用点分类」重新整理（便于判断改了它会不会重编译）：

| 变量 | 类型/默认 | 作用点 | 改了是否触发重编译 |
|------|-----------|--------|--------------------|
| `DG_JIT_DEBUG` | int/0 | 同时进 flag（ptxas verbose）+ 多处运行期打印 | **是**（因追加 ptxas flag） |
| `DG_PRINT_CONFIGS` | int/0 | 运行期打印选中配置 | 否 |
| `DG_JIT_CACHE_DIR` | str/`$HOME/.deep_gemm` | 路径选择 | 否（只改位置） |
| `DG_JIT_USE_NVRTC` | int/0 | 后端选择（signature） | **是** |
| `DG_JIT_NVCC_COMPILER` | str/默认 | 编译器路径（signature 含版本号） | **可能**（若换的 nvcc 版本号不同） |
| `DG_JIT_CPP_STANDARD` | int/20 | flag（`-std=c++{}`） | **是** |
| `DG_JIT_PRINT_COMPILER_COMMAND` | int/0 | 运行期打印编译命令 | 否 |
| `DG_JIT_PTXAS_VERBOSE` | int/0 | flag（ptxas verbose） | **是** |
| `DG_JIT_PTXAS_CHECK` | int/0 | flag + 编译后断言无 local memory | **是** |
| `DG_JIT_PRINT_LOAD_TIME` | int/0 | 运行期打印 cubin 加载耗时 | 否 |
| `DG_JIT_WITH_LINEINFO` | int/0 | flag（`-lineinfo`） | **是** |
| `DG_JIT_DUMP_ASM` | int/0 | 运行期同时 dump PTX+SASS | 否 |
| `DG_JIT_DUMP_PTX` | int/0 | 运行期 dump PTX | 否 |
| `DG_JIT_DUMP_SASS` | int/0 | 运行期 dump SASS | 否 |
| `DG_COMM_KERNEL_DEBUG` | int/0 | 运行期（Mega MoE 每次清零对称缓冲） | 否 |
| `DG_USE_NVIDIA_TOOLS` | int/0 | 运行期（剖析时让位） | 否 |
| `DG_USE_PYTORCH_CUBLASLT_HANDLE` | int/0 | 句柄选择 | 否 |
| `DG_USE_TEMP_CUBLASLT_WORKSPACE` | int/0 | 每次调用临时分配 workspace | 否 |
| `DG_SKIP_CUDA_BUILD` / `DG_FORCE_BUILD` | int/0 | **安装期**（setup.py） | N/A |
| `DG_JIT_USE_RUNTIME_API` | int/0 | **安装期**宏（setup.py 传 `-D`） | N/A |

> **判别窍门**：凡是会写进 `flags` 字符串的变量（`DG_JIT_CPP_STANDARD`、`DG_JIT_PTXAS_VERBOSE`、`DG_JIT_PTXAS_CHECK`、`DG_JIT_WITH_LINEINFO`，以及间接的 `DG_JIT_DEBUG`/`DG_JIT_USE_NVRTC`/`DG_JIT_NVCC_COMPILER`），都会进缓存键；纯粹的「打印/生成文件/路径/句柄」类变量则不会。

#### 4.1.4 代码实践

**实践目标**：亲手验证「进 flag 的变量会触发重编译、不进 flag 的变量不会」这条判别窍门。

**操作步骤**：

1. 先跑一次基线，把缓存目录记下来：

   ```bash
   cd DeepGEMM
   python tests/test_fp8_fp4.py 2>&1 | tail -n 5
   ls -lt $HOME/.deep_gemm/cache | head -n 5
   ```

2. 设一个**不进 flag** 的变量再跑，观察缓存目录是否新增：

   ```bash
   DG_PRINT_CONFIGS=1 python tests/test_fp8_fp4.py 2>&1 | grep -i config
   ls -lt $HOME/.deep_gemm/cache | head -n 5
   ```

3. 设一个**进 flag** 的变量再跑，观察缓存目录是否新增（即重编译）：

   ```bash
   DG_JIT_WITH_LINEINFO=1 python tests/test_fp8_fp4.py
   ls -lt $HOME/.deep_gemm/cache | head -n 5
   ```

**需要观察的现象**：第 2 步缓存目录数量不变、第 3 步新增了目录；`DG_PRINT_CONFIGS=1` 时 stdout 多出形如 `GemmDesc{...}: GemmConfig{...}, ...` 的行。

**预期结果**：`DG_PRINT_CONFIGS` 不重编译（目录数不变），`DG_JIT_WITH_LINEINFO` 重编译（新目录）。若想看变量到底有没有写进 `flags`，可加 `DG_JIT_PRINT_COMPILER_COMMAND=1`，stdout 会打印完整 nvcc 命令行，在其中搜索 `-lineinfo` 即可确认。**待本地验证**：具体目录数与形状有关，以你机器实际为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `DG_JIT_DEBUG=1` 当作「日常调试」常开，是一个坏习惯？

**参考答案**：因为 `DG_JIT_DEBUG` 会向 `flags` 追加 `--ptxas-options=--verbose,...`（见 [csrc/jit/compiler.hpp:L58-L59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L58-L59)），从而改变 `signature` 与缓存目录名，**导致所有形状重新编译一遍**，且这些「调试 cubin」和「正常 cubin」互不共享缓存。日常只需 `DG_PRINT_CONFIGS=1` 这类不进 flag 的变量即可。

**练习 2**：`DG_JIT_NVCC_COMPILER` 改了路径就一定会重编译吗？

**参考答案**：不一定。`signature` 里写的是 `NVCC{major}.{minor}` 版本号（见 [csrc/jit/compiler.hpp:L200](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L200)），只有当新 nvcc 的**版本号不同**时 signature 才变、才重编译；换成同版本的另一份 nvcc 二进制则不重编译。

### 4.2 PTX/SASS dump 与 lineinfo：调优的可读产物

#### 4.2.1 概念说明

调优一个 JIT kernel 的第一步是「看见它」。CUDA 的编译产物 cubin 是二进制，对人不可读。DeepGEMM 提供三个 dump 开关，让你拿到编译链路上不同阶段的可读文本：

- **PTX**（`DG_JIT_DUMP_PTX`）：NVCC/NVRTC 把 `.cu` 翻译成的「虚拟指令集」文本，介于 C++ 与机器码之间。看 PTX 适合确认「我写的内联汇编/模板到底展开成了什么指令」。
- **SASS**（`DG_JIT_DUMP_SASS`）：`ptxas` 把 PTX 汇编成的「真实机器码」，是真正跑在 GPU 上的指令。看 SASS 适合数「这条热点循环里有几条 FFMA、有没有 bank-conflict 访问」。
- **ASM**（`DG_JIT_DUMP_ASM`）：等于「PTX + SASS 全都要」的快捷方式。

与 dump 并列的另一个关键开关是 **`DG_JIT_WITH_LINEINFO`**。它不产出可读文本，而是往 cubin 里**嵌 入「指令 ↔ 源码行号」的映射表**（`-lineinfo`）。这条信息本身不影响运行结果，但它是 `ncu`、Nsight Compute GUI 把每条指令定位回 `.cuh` 源码行的前提——没有它，NCU 的 `SourceCounters` / `--import-source` 一律失效。

理解 dump 与 lineinfo 的分工，是本模块的核心。

#### 4.2.2 核心流程

dump 的产出发生在 `compiler->build()` 内部，时机是「cubin 编译完成之后、原子重命名上线之前」，见 [csrc/jit/compiler.hpp:L116-L127](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L116-L127)：

```
build(name, code):
  1. 计算缓存目录 dir_path
  2. 命中运行期缓存 → 直接返回
  3. 在 tmp/<uuid> 里编译出 kernel.cubin
     ├ 若 DG_JIT_DUMP_ASM 或 DG_JIT_DUMP_PTX → 顺带产出 kernel.ptx
     └ 调子类 compile()
  4. 若 DG_JIT_DUMP_ASM 或 DG_JIT_DUMP_SASS
     └ disassemble(cubin) → kernel.sass   # 用 cuobjdump --dump-sass
  5. fsync 整个 tmp 目录
  6. 原子 rename 到正式缓存目录
  7. 写入运行期缓存并返回
```

两个要点：其一，dump 产物（`.ptx`/`.sass`）和 `.cubin` 一起躺在缓存目录里，路径形如 `$HOME/.deep_gemm/cache/kernel.{name}.{digest}/kernel.sass`；其二，dump 是**纯运行期行为**，不进 `flags`、不进 `signature`，所以**打开它不会触发重编译**（与你 4.1.5 的判别一致）。

`lineinfo` 则不同——它在**编译期**通过 `-lineinfo` flag 注入（见 [csrc/jit/compiler.hpp:L60-L61](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L60-L61)），会进 `flags`、会进缓存键，**会触发重编译**。所以典型调优流程是「先开 lineinfo 重编译一次（建一份带映射的缓存），之后所有 NCU 剖析都复用这份缓存」。

#### 4.2.3 源码精读

**dump PTX/SASS 的判定与调用。** 见 [csrc/jit/compiler.hpp:L116-L127](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L116-L127)：只要 `DG_JIT_DUMP_ASM` 或 `DG_JIT_DUMP_PTX` 之一为真，就给 `compile()` 传一个非空 `ptx_path`，让后端顺带写出 PTX（NVCC 走 `-ptx` 再编译一遍，NVRTC 走 `nvrtcGetPTX`）。只要 `DG_JIT_DUMP_ASM` 或 `DG_JIT_DUMP_SASS` 之一为真，就调 `disassemble()` 反汇编出 SASS。注意 `DG_JIT_DUMP_ASM` 同时命中两条——这就是它「PTX+SASS 全要」的由来。

**反汇编实现：调 cuobjdump。** `disassemble()` 拼一条 `cuobjdump --dump-sass {cubin} > {sass}` 命令并执行，失败则断言，见 [csrc/jit/compiler.hpp:L151-L161](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L151-L161)。这里的 `cuobjdump_path` 在 `prepare_init` 时就定为 `cuda_home/bin/cuobjdump`。也就是说，DeepGEMM 并不自己实现反汇编，而是**直接复用 CUDA Toolkit 自带的 `cuobjdump`**——你完全可以脱离 DeepGEMM，手动对缓存里的 `kernel.cubin` 跑 `cuobjdump --dump-sass` 得到同样结果。

**寄存器用量的来源：ptxas verbose（不是 SASS）。** 调优时最常被问「这个 kernel 占了多少寄存器」。答案来自 `--ptxas-options=--verbose`，它在编译时往 stdout 打印一行形如 `ptxas info : Used 96 registers, 384 bytes cmem[0], ...`。这条 flag 由 `DG_JIT_DEBUG` / `DG_JIT_PTXAS_VERBOSE` / `DG_JIT_PTXAS_CHECK` 任一开启（见 [csrc/jit/compiler.hpp:L58-L59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L58-L59)），并由 NVCC 后端在编译后把它打印出来，见 [csrc/jit/compiler.hpp:L249-L250](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L249-L250)。另外，若开了 `DG_JIT_PTXAS_CHECK`，编译后还会**断言输出里不出现 `Local memory used`**（寄存器溢出到 local memory 是性能红灯），见 [csrc/jit/compiler.hpp:L245-L246](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L245-L246)。

**lineinfo 的 flag 注入。** 见 [csrc/jit/compiler.hpp:L60-L61](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L60-L61)，`-Xcompiler -rdynamic -lineinfo`：`-lineinfo` 给设备码嵌源码行表，`-Xcompiler -rdynamic` 让宿主侧（cubin 里嵌入的少量 host 元数据）带上完整符号，二者配合才能让 NCU 的 `--import-source yes` 工作。

把以上信息拼成一张「我想看 X，该开什么」对照表：

| 调优需求 | 该开的环境变量 | 产物位置 | 是否重编译 |
|----------|----------------|----------|------------|
| 看 kernel 占多少寄存器 | `DG_JIT_PTXAS_VERBOSE=1` | stdout | 是（进 flag） |
| 断言无寄存器溢出 | `DG_JIT_PTXAS_CHECK=1` | 编译期断言 | 是 |
| 读 PTX 虚拟指令 | `DG_JIT_DUMP_PTX=1` | 缓存目录 `kernel.ptx` | 否 |
| 读 SASS 机器码 | `DG_JIT_DUMP_SASS=1` | 缓存目录 `kernel.sass` | 否 |
| PTX+SASS 都要 | `DG_JIT_DUMP_ASM=1` | 两个文件 | 否 |
| NCU 映射回源码行 | `DG_JIT_WITH_LINEINFO=1` | 嵌入 cubin | 是 |

#### 4.2.4 代码实践

**实践目标**：dump 一个 FP8 GEMM kernel 的 SASS，定位它的寄存器占用，并手动对 cubin 跑 `cuobjdump` 复核。

**操作步骤**：

1. 开 dump 重跑测试（不进 flag，不会因 dump 本身重编译，但若同时想要寄存器数需再加 verbose）：

   ```bash
   DG_JIT_DUMP_SASS=1 DG_JIT_PTXAS_VERBOSE=1 python tests/test_fp8_fp4.py 2>&1 | tee /tmp/dg.log
   ```

2. 在 stdout 里抓寄存器用量：

   ```bash
   grep -i "registers" /tmp/dg.log | head
   ```

3. 找到刚生成的 SASS 文件并查看热点：

   ```bash
   SASS=$(ls -t $HOME/.deep_gemm/cache/*/kernel.sass | head -1)
   echo "SASS file: $SASS"
   head -n 40 "$SASS"          # 看文件头（含寄存器/共享内存声明）
   grep -cE "FFMA|HFMA|HMMA|WG?MMA" "$SASS"   # 数 tensor core / 乘加指令条数
   ```

4. 脱离 DeepGEMM，手动对同一 cubin 跑 `cuobjdump` 复核（产物应与第 3 步一致）：

   ```bash
   CUBIN=$(ls -t $HOME/.deep_gemm/cache/*/kernel.cubin | head -1)
   cuobjdump --dump-sass "$CUBIN" | head -n 40
   ```

**需要观察的现象**：第 2 步看到 `Used N registers`；第 3 步 SASS 文件头有 `.reg .b32 %r<...>` 之类的寄存器声明；第 4 步与第 3 步内容一致。

**预期结果**：SM90 FP8 GEMM 的 `__launch_bounds__(..., 1)` 决定每 SM 仅驻留 1 个 CTA，寄存器用量通常很高（接近 `--register-usage-level=10` 的上限）。`cuobjdump` 手动复核应得到与 DeepGEMM 自动 dump 完全相同的 SASS，证明 DeepGEMM 没有做任何额外改写。**待本地验证**：具体寄存器数随形状与架构而变。

#### 4.2.5 小练习与答案

**练习 1**：如果你只想用 NCU 看「源码行级别的 stall 原因」，但完全不想读 PTX/SASS 文本，最少需要开哪几个变量？

**参考答案**：只需 `DG_JIT_WITH_LINEINFO=1`（让 cubin 带行号表）。`DG_JIT_DUMP_*` 不必开——它们只是把文本写到磁盘给人读，NCU 直接从 cubin 里读 lineinfo 即可。

**练习 2**：为什么 `disassemble()` 用 `cuobjdump` 而不是自己解析 cubin？

**参考答案**：因为 cubin 是 NVIDIA 私有二进制格式，`cuobjdump` 是官方唯一可靠的反汇编器，且随 CUDA Toolkit 一起分发（路径已知）。复用它能保证 SASS 文本与 toolkit 版本严格一致、零维护成本（见 [csrc/jit/compiler.hpp:L151-L161](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L151-L161)）。

### 4.3 sanitizer 与 NCU 剖析：与外部工具协作

#### 4.3.1 概念说明

Dump 出来的 SASS 是「静态文本」，能告诉你「指令长什么样」，却不能告诉你「运行时哪里越界访问了显存、哪里有未同步的 hazard、哪个 stall 原因最致命」。这三类问题分别交给两个外部工具：

- **`compute-sanitizer`**：NVIDIA 的运行期正确性检查器，内置多个 `--tool`：`memcheck`（越界/释放后使用）、`synccheck`（缺失同步导致的 race）、`racecheck`（共享内存 race）等。它是「DeepGEMM 有没有写错显存」的最终裁判。
- **`nsight compute`（`ncu`）**：NVIDIA 的运行期性能剖析器，能逐 kernel 报告吞吐、带宽、stall 原因、寄存器占用、采样到的指令分布。它是「DeepGEMM 为什么慢」的最终裁判。

两个工具有一个共同点：它们要**劫持** CUDA 运行时（注入自己的层），这与 DeepGEMM 自带的性能采样（`torch.profiler`/kineto）和某些缓存行为**互相冲突**。因此 DeepGEMM 专门设计了一个「让位开关」`DG_USE_NVIDIA_TOOLS`：设为 `1` 时，DeepGEMM 主动关闭自己的内部剖析、改用临时 workspace，把舞台让给外部工具。本模块的关键，就是看懂这个「让位」是如何在代码里实现的。

#### 4.3.2 核心流程

**sanitizer 流程**（`tests/test_sanitizer.py`）：

```
对每个 test_* 函数、每个 --tool：
  1. 拼一段「只 import 并调用该函数」的内嵌 Python 脚本
  2. 注入一组协作环境变量（CUDA_LAUNCH_BLOCKING=1、DG_JIT_PTXAS_CHECK=1、
     DG_USE_NVIDIA_TOOLS=1、DG_USE_TEMP_CUBLASLT_WORKSPACE=1 等）
  3. 以 compute-sanitizer --tool={memcheck|synccheck} ... python -c <脚本>
     方式启动子进程
  4. 任何子进程返回非 0 → 整体退出码非 0（CI 判失败）
```

**NCU 流程**（`scripts/run_ncu_mega_moe.sh`，针对 Mega MoE）：

```
1. export DG_JIT_WITH_LINEINFO=1            # 让 cubin 带源码行表
2. 先跑一遍 --ncu-profile-only 预热 JIT 缓存（避免把编译耗时算进剖析）
3. 为每个 rank 拉起一个 ncu 进程，配多进程协同（--communicator tcp 等）：
   ncu --kernel-name sm100_fp8_fp4_mega_moe_impl \
       --import-source yes --section SourceCounters --section PmSampling \
       --rule LocalMemoryUsage ... \
       -o <输出> python tests/test_mega_moe.py --local-rank-idx=$i --ncu-profile-only
4. wait 全部完成
```

注意 sanitizer 与 NCU 是**两类不同目的**的协作：sanitizer 不需要 lineinfo（它查的是内存正确性），但需要 `DG_USE_NVIDIA_TOOLS` 让位；NCU 反过来，需要 lineinfo，且其剖析对象是单个 mega-kernel。

#### 4.3.3 源码精读

**sanitizer 的协作环境变量注入。** `test_sanitizer.py` 在启动子进程前注入 6 个变量，见 [tests/test_sanitizer.py:L52-L58](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_sanitizer.py#L52-L58)：

- `CUDA_LAUNCH_BLOCKING=1`：把所有 kernel launch 变成同步，让 sanitizer 能精确定位是哪一次 launch 出错。
- `DG_JIT_PTXAS_CHECK=1`：编译期断言无 local memory 溢出（见 4.2.3）。
- `DG_USE_NVIDIA_TOOLS=1`：**让位开关**，下文详述。
- `DG_USE_TEMP_CUBLASLT_WORKSPACE=1`：每次调用临时分配 cuBLASLt workspace，避免「进程退出时 workspace tensor 的析构晚于 CUDA driver 卸载」导致的 `cudaErrorCudartUnloading` 崩溃（这个坑的根因记录在 [csrc/jit/device_runtime.hpp:L40-L42](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L40-L42)）。
- `PYTORCH_NO_CUDA_MEMORY_CACHING=1`：关闭 PyTorch 的显存缓存，让 sanitizer 能抓到每一处真实分配。
- `TORCH_SHOW_CPP_STACKTRACES=1`：报错时打印 C++ 调用栈。

**让位开关 `DG_USE_NVIDIA_TOOLS` 的实现。** 它只在一个地方被读：基准采样函数开头直接 `return`，跳过整个 `torch.profiler` 流程，见 [deep_gemm/testing/bench.py:L87-L90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L87-L90)。注释写得很直白：`torch.profiler` 与 Nsight Systems / Nsight Compute / Compute Sanitizer **冲突**，所以在这些工具下必须关掉内部采样。这就是为什么 sanitizer 测试和 NCU 剖析都离不开它——否则 kineto 注入会和外部工具打架，得到错误数据甚至 hang。

**sanitizer 的命令行组装。** 见 [tests/test_sanitizer.py:L63-L74](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_sanitizer.py#L63-L74)：用 `--target-processes=application-only`（只跟踪目标进程）、`--force-blocking-launches`（强制阻塞 launch）、`--kernel-name-exclude kns=nvjet`（**排除 cuBLASLt 的 `nvjet` kernel**——因为 cuBLASLt 是闭源黑盒，sanitizer 抓它没意义）。`--destroy-on-device-error=context` 保证出错即销毁 context，立刻暴露问题。

**NCU 脚本的关键设计。** 见 [scripts/run_ncu_mega_moe.sh:L48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/scripts/run_ncu_mega_moe.sh#L48) 先 `export DG_JIT_WITH_LINEINFO=1`（与 4.2 呼应——剖析必须有行表）；NCU 参数见 [scripts/run_ncu_mega_moe.sh:L55-L75](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/scripts/run_ncu_mega_moe.sh#L55-L75)，其中 `--import-source yes`（导入源码）、`--section SourceCounters`（源码行级 stall 统计）、`--section PmSampling`（指令采样）、`--rule LocalMemoryUsage`（local memory 告警）都**依赖 lineinfo**；多进程协同见 [scripts/run_ncu_mega_moe.sh:L79-L85](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/scripts/run_ncu_mega_moe.sh#L79-L85)，每个 rank 一个 ncu 进程，靠 `--communicator tcp` 与 `--communicator-tcp-num-peers` 把多张卡的剖析结果汇总（Mega MoE 是多卡融合 kernel，单进程剖析无意义）。

**`DG_COMM_KERNEL_DEBUG`（Mega MoE 专用）。** 它在每个 mega-kernel 调用前把整个对称缓冲 `zero_()`，见 [csrc/apis/mega.hpp:L252-L253](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L252-L253) 与 [csrc/apis/mega.hpp:L332-L333](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L332-L333)。作用是排查 Mega MoE 的通信/combine 阶段是否残留了上一轮数据——因为对称内存跨 rank 共享，残留数据会伪装成正确结果，让 bug 极难复现。注释明确要求调用方必须在每次调用前**重新拷贝输入**。

#### 4.3.4 代码实践

**实践目标**：跑一次 sanitizer，确认你的本地构建在 memcheck 下无越界。

**操作步骤**：

1. 确认 `compute-sanitizer` 可用（随 CUDA Toolkit 安装）：

   ```bash
   /usr/local/cuda/bin/compute-sanitizer --version
   ```

2. 只对一个函数、一个工具跑（避免全量太慢）：

   ```bash
   python tests/test_sanitizer.py --funcs test_fp8_fp4.test_fp8_fp4_gemm_nt --tools memcheck
   ```

3. 若想看「让位开关」的效果，对比开/关 `DG_USE_NVIDIA_TOOLS` 时同一基准函数的行为：

   ```bash
   # 关（默认）—— kineto 会注入，可能与 sanitizer 冲突
   DG_USE_NVIDIA_TOOLS=0 compute-sanitizer --tool=memcheck python -c "import deep_gemm; print('ok')"
   ```

**需要观察的现象**：第 2 步 stdout 出现 `Running test_fp8_fp4.test_fp8_fp4_gemm_nt with compute-sanitizer memcheck`，且最终无 `Memcheck: detected X errors`；第 3 步若关掉让位开关，可能出现 sanitizer 与 kineto 抢占导致的告警或 hang。

**预期结果**：在正确构建下，sanitizer 报 0 error、退出码 0。若报错，说明 kernel 存在越界访问——这是回归测试要捕获的头号 bug。**待本地验证**：是否拥有 SM90/SM100 GPU 与合法的 compute-sanitizer 路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 sanitizer 脚本要用 `--kernel-name-exclude kns=nvjet` 排除 cuBLASLt 的 kernel？

**参考答案**：`nvjet` 是 cuBLASLt 的闭源 kernel，sanitizer 无法理解它的内部访存模式，对其报错既无法修复也无意义；DeepGEMM 用 cuBLASLt 仅作参考基准（见 u4-l1），排除它能把 sanitizer 的注意力聚焦在 DeepGEMM 自己的可修复 kernel 上。

**练习 2**：NCU 脚本为什么要先跑一遍 `--ncu-profile-only`「预热」，再正式剖析？

**参考答案**：第一次调用会触发 JIT 编译，编译耗时远大于 kernel 运行。若不预热，NCU 会把「编译 + 加载」也算进剖析窗口或采样区间，污染数据。预热让缓存命中（见 u3-l3），正式剖析时只剩纯 kernel 执行，数据才干净。

## 5. 综合实践

把本讲三个模块串成一条完整调优闭环。任务：**对一个 FP8 GEMM，定位其寄存器占用与热点指令，并用 `set_tc_util` / `set_num_sms` 调参，验证性能变化。**

下面这段「示例代码」以 `tests/test_fp8_fp4.py` 的真实用法为蓝本，可直接存为 `tests/tune_fp8.py`（与 `generators.py` 同目录，以便 `from generators import generate_normal`）。它本身不修改 DeepGEMM 源码，只调用公开 API。

```python
# 示例代码：调优闭环的最小骨架（参照 tests/test_fp8_fp4.py）
import torch, deep_gemm
from deep_gemm.testing import bench_kineto
from generators import generate_normal, get_mk_alignment_for_contiguous_layout  # 复用现有生成器

# 1. 复用 generate_normal 构造一对 FP8 张量 + 缩放因子 + 参考输出
#    （major/out_dtype/kernel_type 等参数与 test_fp8_fp4.py 保持一致，此处从简）
a, b, c, d, ref_d = generate_normal(4096, 4096, 4096,
                                    major_a, major_b, accumulate,
                                    out_dtype, kernel_type, use_ue8m0=True)
def run():
    deep_gemm.fp8_fp4_gemm_nt(a, b, d, c=c, disable_ue8m0_cast=False)

# 2. 基线性能：bench_kineto(fn, kernel_names) —— kernel_names 是 kernel 名子串
#    注意：剖析时切勿同时设 DG_USE_NVIDIA_TOOLS=1，否则 bench_kineto 会直接 return（见 4.3）
deep_gemm.set_num_sms(0); deep_gemm.set_tc_util(0)   # 0 = 用默认（满 SM / tc_util=100）
base = bench_kineto(run, 'gemm_', suppress_kineto_output=True)
print('baseline time(s):', base)

# 3. 调参：限制 SM 数 / 调 tensor core 利用率，观察耗时变化
for sms in [0, 64, 128]:
    for tc in [0, 80, 100]:
        deep_gemm.set_num_sms(sms); deep_gemm.set_tc_util(tc)
        t = bench_kineto(run, 'gemm_', suppress_kineto_output=True)
        print(f'sms={sms} tc_util={tc} -> {t}')
```

> 说明：`generate_normal` 的完整参数（`major_a`/`major_b`/`accumulate`/`out_dtype`/`kernel_type`）请照搬 `tests/test_fp8_fp4.py:46` 那一行，本示例只展示「调优旋钮 + bench」骨架。`bench_kineto` 的真实签名是 `bench_kineto(fn, kernel_names, ...)`，其中 `fn` 是无参 callable、`kernel_names` 是用于匹配 kineto 事件名的子串（见 [tests/test_fp8_fp4.py:L58-L59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L58-L59)）。

完整闭环操作（命令行 + Python 配合）：

1. **dump SASS + 寄存器**（不进 flag 的是 dump，进 flag 的是 verbose，见 4.2）：

   ```bash
   DG_JIT_DUMP_SASS=1 DG_JIT_PTXAS_VERBOSE=1 python tests/tune_fp8.py 2>&1 | tee /tmp/dg.log
   grep -i "registers" /tmp/dg.log   # 记下基线寄存器数
   ```

2. **NCU 剖析热点**（必须先开 lineinfo 重编译一次，见 4.2.2）：

   ```bash
   DG_JIT_WITH_LINEINFO=1 python tests/tune_fp8.py   # 建一份带行表的缓存
   ncu --kernel-name regex:sm.*fp8.*gemm \
       --import-source yes --section SourceCounters --section PmSampling \
       --launch-count 1 -o /tmp/tune python tests/tune_fp8.py
   ncu -i /tmp/tune.ncu-rep --page raw    # 查看热点指令与 stall 原因
   ```

3. **调参验证**：运行上面 `tests/tune_fp8.py` 第 3 步的循环，把不同 `(num_sms, tc_util)` 下的耗时制表，结合 NCU 报告判断「限制 SM 数是否缓解了 L2 抖动」「调高 tc_util 是否改变了 `--register-usage-level` 烘焙的 tile 形状」。

**需要观察的现象**：第 1 步 dump 出 `.sass` 文件并读到寄存器数；第 2 步 NCU 报告能定位到某几行 `.cuh` 源码的 stall；第 3 步不同旋钮下耗时不同。

**预期结果**：`set_num_sms` 降到远低于物理 SM 数时性能下降（wave 数变多/尾波变差）；`set_tc_util` 会作为编译期常量烤进设备 kernel 模板（见 [csrc/jit_kernels/impls/sm100_bf16_gemm.hpp:L68](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_bf16_gemm.hpp#L68)，它是 `GemmDesc.tc_util` 字段、被传进 `generate_impl` 的模板实参），改变它会重新编译出不同的 kernel 特化版本，进而影响 tensor core 的线程分配。**待本地验证**：绝对数值与符号取决于你的 GPU 架构与形状，重点观察「旋钮→性能」的单调性是否与原理一致。

## 6. 本讲小结

- DeepGEMM 把几乎所有可观测性旋钮做成环境变量，统一经 `get_env<T>` 读取，遵循「`0`/空 = 默认」约定；README 的 [L159-L185](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L159-L185) 是唯一权威清单。
- 关键判别：凡写进 `flags` 字符串的变量（`DG_JIT_CPP_STANDARD`、`DG_JIT_PTXAS_VERBOSE/CHECK`、`DG_JIT_WITH_LINEINFO`，以及间接的 `DG_JIT_DEBUG`/`USE_NVRTC`/`NVCC_COMPILER`）都会进缓存键、**会触发重编译**；纯打印/dump/路径类变量则不会。
- **dump 与 lineinfo 分工**：`DG_JIT_DUMP_PTX/SASS/ASM` 产出可读反汇编（运行期、不重编译、靠 `cuobjdump`）；`DG_JIT_WITH_LINEINFO` 给 cubin 嵌源码行表（编译期、会重编译），是 NCU 映射回源码的前提。寄存器用量来自 `DG_JIT_PTXAS_VERBOSE`，不是 SASS。
- **sanitizer**（`test_sanitizer.py`）用 `compute-sanitizer --tool=memcheck/synccheck` 查正确性，靠 `DG_USE_NVIDIA_TOOLS` 让 DeepGEMM 主动关闭 kineto 内部采样以避免冲突；**NCU**（`run_ncu_mega_moe.sh`）查性能，靠 `DG_JIT_WITH_LINEINFO=1` 提供行表、靠多进程协同剖析 Mega MoE 融合 kernel。
- 完整调优闭环：`DG_JIT_DUMP_SASS=1 DG_JIT_PTXAS_VERBOSE=1` 看指令与寄存器 → `DG_JIT_WITH_LINEINFO=1` + ncu 定位热点行 → 用 `set_tc_util` / `set_num_sms` 调参并用 `bench` 验证。

## 7. 下一步学习建议

- **回到测试矩阵**：本讲多次引用 `tests/test_fp8_fp4.py` 与 `deep_gemm/testing/bench.py`，建议配合 [u10-l3](u10-l3-testing-benchmark-numeric.md) 系统理解 `generators.py` / `bench_kineto` / `calc_diff` 这套工程脚手架，把「调优闭环」补全为「正确性 + 性能」双闭环。
- **深入 JIT 缓存**：若你想搞清「为什么换了个环境变量就重编译」，回到 [u3-l3](u3-l3-jit-cache-include-hash.md)（缓存与头文件哈希）与 [u3-l4](u3-l4-nvcc-vs-nvrtc.md)（NVCC vs NVRTC），看 `signature` 与 `flags` 如何共同决定缓存目录名。
- **Mega MoE 剖析进阶**：本讲的 NCU 脚本针对 `sm100_fp8_fp4_mega_moe_impl`，若要理解它剖析的到底是什么，先读 [u8-l4](u8-l4-mega-moe-fused-overlap.md)（融合 mega 内核与通信重叠），再回来用 NCU 的 `PmSampling` 数据对照阅读。
- **动手扩展**：尝试为本机某个新形状同时跑 sanitizer + NCU，把报告里的 stall 原因与 [u6-l1](u6-l1-sm90-fp8-gemm-1d1d-entry.md)（内核入口）的共享内存划分、软件流水线对照，验证「理论结构 ↔ 实测指令」是否吻合——这是把本手册从「读懂」推向「能调」的临门一脚。
