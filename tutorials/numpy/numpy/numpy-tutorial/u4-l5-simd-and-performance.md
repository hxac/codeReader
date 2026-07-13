# 广播、SIMD 与性能优化

## 1. 本讲目标

本讲关注 NumPy ufunc 的「快」从哪里来。前面几讲我们已经知道：ufunc 对数组做**逐元素 + 广播**运算（见 u4-l1、u3-l4）。但「逐元素」并不等于「一个一个算」——在支持的 CPU 上，NumPy 会用 SIMD 指令一次处理多个元素，并且在程序启动时自动挑选当前 CPU 能跑的最快那条代码路径。

学完本讲你应当能够：

- 说清 NumPy 性能的三层来源：通用内联（universal intrinsics）、编译期多版本、运行时分发。
- 区分 **baseline（基线）** 与 **dispatch（可分发）** 两类 CPU 特性，并解释为什么必须「运行时」才能决定调用哪一份代码。
- 读懂 `.dispatch.c` 文件、`NPY_CPU_DISPATCH_*` 一组宏、以及 Meson 的 `mod_features.multi_targets()` 是如何把「一个源文件」编译成「多份带后缀的目标码」的。
- 用 `np.lib.introspect.opt_func_info()` 在运行时**观察**每个 ufunc 实际选中了哪条 SIMD 分支，并用 `NPY_DISABLE_CPU_FEATURES` 环境变量改变它。
- 用 `asv` 基准（`benchmarks/benchmarks/bench_ufunc.py`）和 `spin bench` **量化**性能差异。

> ⚠️ 一个重要的准确性提醒：本讲的学习任务原文提到「设置 `NPY_CPU_DISPATCH_TRACE=1`」。在当前 HEAD（`71d523a5`）的源码里，**并不存在这个环境变量**（全仓库 `getenv` 只读取 `NPY_ENABLE_CPU_FEATURES` / `NPY_DISABLE_CPU_FEATURES`）。分发信息的「追踪」在模块导入时**始终开启**，正确的人口是 `np.lib.introspect.opt_func_info()`。本讲会按真实代码讲解，并在实践中给出可运行的等效做法。

## 2. 前置知识

阅读本讲前，请先建立以下直觉（相关细节已在前面讲义给出）：

- **ufunc 执行模型**（u4-l1、u4-l3）：给定输入 dtype，ufunc 从「类型→循环」分发表里选出一条 C 内层循环（kernel），在广播后的形状上按 strides 推进指针逐元素计算。本讲关心的就是「这条 C 内层循环本身有多快」。
- **strides 与连续性**（u3-l2、u4-l2）：内存连续（C/F contiguous）的数组能用最快的「连续循环」；非连续数组只能用更慢的 strided 循环。SIMD 加速对连续内存最有效。
- **ndarray 内存模型**（u4-l2）：`data` 指针 + `dimensions` + `strides` + `descr`。

接下来需要补充的两个新概念：

- **SIMD（Single Instruction, Multiple Data，单指令多数据）**：普通标量指令一次处理一个元素，SIMD 指令一次把一整条「向量寄存器」里的多个元素同时算完。寄存器越宽、一次能塞的元素越多。

  一条 \(W\) 位宽的寄存器，装 \(s\) 字节的元素，可同时处理 \(\lfloor W/s \rfloor\) 个元素。例如 256 位的 AVX2 寄存器：

  \[
  \text{float64 (8 字节)}:\ \lfloor 256/8 \rfloor = 4 \quad/\quad
  \text{float32 (4 字节)}:\ \lfloor 256/4 \rfloor = 8
  \]

  因此理论上 AVX2 相对标量的理想加速比约为 \(4\times\)（float64）或 \(8\times\)（float32）；AVX-512（512 位）则翻倍。实际达不到理想值，因为还有内存带宽、非连续访问、循环开销等。

- **CPU 指令集是「因机而异」的**：不同年代、不同架构（x86 / ARM / Power / RISC-V）的 CPU 支持不同的 SIMD 扩展（x86 的 SSE/AVX/AVX2/AVX-512、ARM 的 NEON/ASIMD/SVE、Power 的 VSX、RISC-V 的 RVV）。NumPy 发布的 wheel 要能在「最老」的 CPU 上启动，又要在「最新」的 CPU 上跑满性能——这就是**运行时分发**要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/src/common/npy_cpu_dispatch.h` | 分发宏的对外头文件：声明 tracer 接口、定义 `NPY_CPU_DISPATCH_TRACE` 宏 |
| `meson_cpu/main_config.h.in` | 分发宏的「模板」头文件（构建时生成 `npy_cpu_dispatch_config.h`）：定义 `CURFX`/`DECLARE`/`CALL`/`INFO` 等核心宏 |
| `numpy/_core/src/common/npy_cpu_dispatch.c` | tracer 的实现：创建并填充 `__cpu_targets_info__` 字典 |
| `numpy/_core/src/common/npy_cpu_features.c` | CPU 特性探测；读取 `NPY_ENABLE/DISABLE_CPU_FEATURES` 环境变量 |
| `numpy/_core/src/umath/_umath_tests.dispatch.c` | **可分发源**示例：用 `CURFX` 定义带后缀的 func/var/attach |
| `numpy/_core/src/umath/_umath_tests.c.src` | `test_dispatch()` 测试入口：演示 `CALL`/`CALL_XB`/`CALL_ALL` 三种选择方式 |
| `numpy/_core/meson.build` | 用 `mod_features.multi_targets()` 把 `.dispatch.c` 编译成多份目标码 |
| `numpy/_core/src/multiarray/multiarraymodule.c` | 模块初始化：调用 tracer、暴露 `__cpu_baseline__`/`__cpu_dispatch__`/`__cpu_features__` |
| `numpy/_core/code_generators/generate_umath.py` | 代码生成器：为每个被分发的 ufunc 循环发射 `NPY_CPU_DISPATCH_TRACE` |
| `numpy/lib/introspect.py` | `opt_func_info()`：查询每个 ufunc 当前的 SIMD 目标 |
| `numpy/_core/src/umath/loops_unary_fp.dispatch.c.src` | 真实 SIMD 循环示例（`exp`/`sqrt`/`abs` 等），使用通用内联 `npyv_*` |
| `benchmarks/benchmarks/bench_ufunc.py` | asv 基准：覆盖全部 ufunc，强制完整性检查 |
| `benchmarks/benchmarks/common.py` | `Benchmark` 基类、`get_squares_()` 数据工厂 |
| `.spin/cmds.py` | `spin bench` 命令：封装 asv，支持 `--compare` |
| `doc/source/reference/simd/how-it-works.rst`、`index.rst` | 官方对分发机制的文档说明 |

## 4. 核心概念与源码讲解

### 4.1 性能从哪来：SIMD 与运行时分发的直觉

#### 4.1.1 概念说明

ufunc 的「逐元素循环」是 NumPy 的头号热点。提升它性能的最大杠杆就是 SIMD：把「循环 N 次、每次算 1 个元素」改成「循环 N/W 次、每次算 W 个元素」。

但这里有一个工程难题：**NumPy 在编译发布时，并不知道你的 CPU 支持哪一代 SIMD 指令**。如果直接按「最新指令」编译，旧 CPU 一执行就会触发非法指令（segfault）；如果按「最老指令」编译，新 CPU 的能力就白白浪费了。

NumPy 的解法是把性能优化分成**三层**（见官方文档 `doc/source/reference/simd/index.rst`）：

1. **写作层（universal intrinsics，通用内联）**：C 代码不直接写 `__m256` 这类平台专属内联，而是用一套抽象的 `npyv_*` 类型和函数（`simd/simd.h`）。它们被映射到各架构的真实内联上，从而一份源码能生成多份「内核（kernel）」——第一份是最低基线，其余是额外的可分发特性。
2. **编译层**：通过构建选项 `--cpu-baseline`（最低必需指令集）和 `--cpu-dispatch`（额外可分发指令集），为同一个源文件用不同的编译器开关编译多次，得到多个 kernel。
3. **运行时分发层**：程序启动（`import numpy`）时探测当前 CPU 实际支持哪些指令，**抓取最合适那份 kernel 的函数指针**，之后该 ufunc 就一直调用它。

由此引出两个关键术语：

- **baseline（基线）**：通过 `--cpu-baseline` 指定的最低指令集。它**没有预处理守卫、永远开启**，可以出现在任何源文件里。导入时还会校验本机 CPU 是否真的支持基线指令，若不支持会直接报 RuntimeError，避免运行中撞上非法指令崩溃。
- **dispatch（可分发）**：通过 `--cpu-dispatch` 指定的「额外」指令集。它们**默认不激活**，由带 `NPY__CPU_TARGET_` 前缀的 C 定义守卫；只在「可分发源」（`*.dispatch.c`）里被启用，并在运行时按需挑选。

#### 4.1.2 核心流程

整条优化链路（构建期 + 运行期）可概括为：

```text
构建期（Meson）:
  loops_unary_fp.dispatch.c.src   ← 用 npyv_* 写的「通用」SIMD 循环
        │  mod_features.multi_targets(dispatch:[AVX2, AVX512F, ...], baseline:[SSE3])
        ▼
  生成多份临时 .c（每份 #define NPY__CPU_TARGET_CURRENT=<某指令集>）
        │  各自用对应编译开关编译
        ▼
  exp_AVX512F, exp_AVX2, exp(baseline) ← 符号靠后缀区分，链接进同一静态库

运行期（import numpy）:
  npy_cpu_init()  → 探测本机 CPU 特性 → 填 npy__cpu_have[] 表
        │
        ▼
  注册 ufunc 时，NPY_CPU_DISPATCH_CALL 宏展开成「三元选择链」：
  CPU_HAVE(AVX512F) ? exp_AVX512F : (CPU_HAVE(AVX2) ? exp_AVX2 : exp)
        │  选出最高且本机支持的那份
        ▼
  把该函数指针写进 ufunc 的 functions[] 表 → 之后 np.exp(...) 直接调用它
```

注意：**广播规则本身**（u3-l4）与本讲是正交的——广播决定「在什么形状上算、strides 是多少」，SIMD 决定「连续段每个元素多快算完」。一条 ufunc 内层循环通常分两种：处理**连续段**的 `CONTIG` 循环（走 SIMD 全速）和处理**非连续段**的 `NCONTIG` 循环（gather/scatter，慢得多）。这就是为什么「让数组连续」对性能至关重要。

#### 4.1.3 源码精读

官方对「三层优化」最凝练的描述在 SIMD 文档首页：

[numpy/.../simd/index.rst:8-30](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/simd/index.rst#L8-L30) —— 说明优化在「写作 / 编译 / 运行时导入」三层完成，运行时探测 CPU 后「抓取最合适 kernel 的指针」。

`how-it-works.rst` 进一步解释 baseline 与 dispatch 的差别：

[numpy/.../simd/how-it-works.rst:90-116](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/simd/how-it-works.rst#L90-L116) —— **baseline** 特性无守卫、永远开启；**dispatch** 特性被 `NPY__CPU_TARGET_` 守卫、只在可分发源里激活。并说明导入时的校验步骤，防止 CPU 撞非法指令。

真实的 SIMD 循环长什么样？看 `loops_unary_fp.dispatch.c.src`（`exp`/`sqrt`/`abs`/`reciprocal`/`square` 等单目浮点 ufunc 的循环）：

[numpy/_core/src/umath/loops_unary_fp.dispatch.c.src:1-9](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/loops_unary_fp.dispatch.c.src#L1-L9) —— 文件顶部 `#include "simd/simd.h"` 引入通用内联；`NPY_SIMD_FORCE_128` 是一条策略：在 x86 上即使启用了 AVX2/AVX512F，对这些小操作也强制只用 128 位（SSE），因为 scatter/gather 处理非连续访问的开销在此反而比 SSE 大。

[numpy/_core/src/umath/loops_unary_fp.dispatch.c.src:72-83](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/loops_unary_fp.dispatch.c.src#L72-L83) —— 定义 SIMD kernel 时的注释，说明刻意避开 libmath 以统一各编译器/架构的浮点错误行为，并用 `npyv_load_till_*`（而非 `npyv_load_tillz_`）把尾部的寄存器通道填 1.0，避免 reciprocal 出现除零。这是「通用内联」让一份代码跨架构安全复用的典型例子。

#### 4.1.4 代码实践

**实践目标**：看清你这台机器上 NumPy 的 baseline / dispatch / 实际特性分别是什么。

**操作步骤**：

```bash
python -c "
import numpy as np
ma = np._core._multiarray_umath
print('baseline :', ma.__cpu_baseline__)
print('dispatch :', ma.__cpu_dispatch__)
feats = ma.__cpu_features__
print('features :', [k for k, v in feats.items() if v])
"
```

**需要观察的现象**：

- `__cpu_baseline__` 是构建期定的最低指令集（如 `('SSE', 'SSE2', 'SSE3')`）。
- `__cpu_dispatch__` 是构建期声明「会生成多份」的可分发指令集（如含 `AVX2`、`AVX512F` 等）。
- `__cpu_features__` 中值为 `True` 的项，是**本机 CPU 实际支持**的特性——它是运行时分发的判据。

**预期结果**：在 x86 机器上，`__cpu_dispatch__` 与「本机实际支持特性的交集」决定了哪些 kernel 会被启用。**待本地验证**（不同机器结果不同；若构建时 `--disable-optimization`，`__cpu_dispatch__` 可能为空）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NumPy 不能在编译期就把「最高指令集」写死？
**答**：因为发布的 wheel 必须能在最老的 CPU 上启动；若写死新指令，旧 CPU 执行到该指令会触发非法指令崩溃。所以必须编译多份、运行时挑。

**练习 2**：baseline 与 dispatch 在「是否有预处理守卫」上有何区别？
**答**：baseline 无守卫、永远开启，可用于任何源文件；dispatch 被 `NPY__CPU_TARGET_` 守卫、只在可分发源里被启用，运行时按需挑（见 `how-it-works.rst` L90-L116）。

---

### 4.2 CPU 分发宏体系（npy_cpu_dispatch.h + main_config.h.in）

#### 4.2.1 概念说明

「把一个源文件编译成多份」之后，还差一个机制让 C 代码能：(a) 在每份编译里给符号加上**目标后缀**避免链接冲突；(b) 在使用方做**前向声明**；(c) 在运行时按 CPU 特性**挑选**正确的那份。NumPy 用一组预处理器宏来统一完成这三件事，它们定义在构建期生成的 `npy_cpu_dispatch_config.h` 里（模板是 `meson_cpu/main_config.h.in`）。

核心宏有三个：

- **`NPY_CPU_DISPATCH_CURFX(NAME)`**：current-fix。展开为 `NAME_<当前目标>`。在被多次编译的可分发源里，它给每个导出符号加上 `_AVX512F` / `_AVX2` 之类的后缀；对基线编译则原样返回 `NAME`。
- **`NPY_CPU_DISPATCH_DECLARE(...)`**：前向声明。在使用方源文件里，展开成对所有已启用目标（含 baseline）的带后缀声明。
- **`NPY_CPU_DISPATCH_CALL(...)`**：运行时分发。展开成一条「三元选择链」：按目标从高到低，用 `NPY_CPU_HAVE(<特性>)` 在运行时探测，命中第一个就选它，否则回退到 baseline。

还有一个**只读报告**宏 `NPY_CPU_DISPATCH_INFO()`：返回一个两元素字符串数组，`[0]` 是当前实际命中的最高目标，`[1]` 是所有可用目标。

#### 4.2.2 核心流程

以一个被声明为 `dispatch: [AVX512_SKX, AVX2], baseline: [SSE3]` 的函数 `add` 为例，宏展开后的逻辑等价于：

```c
// —— 在可分发源 loops_xxx.dispatch.c 里（每份编译各定义一个）——
void NPY_CPU_DISPATCH_CURFX(add)(...) { }   // AVX512 编译里展开为 add_AVX512_SKX
                                             // AVX2 编译里展开为 add_AVX2
                                             // baseline 编译里展开为 add

// —— 在使用方源里 ——
NPY_CPU_DISPATCH_DECLARE(void add, (...))    // 声明 add_AVX512_SKX、add_AVX2、add

// 运行时分发（CALL 宏展开后的等价形式）：
func = NPY_CPU_HAVE(AVX512_SKX) ? add_AVX512_SKX :
       (NPY_CPU_HAVE(AVX2)      ? add_AVX2      : add);  // baseline 兜底
```

`CALL` 是**短路的三元链**：从「最高兴趣」目标往下问 `NPY_CPU_HAVE`，第一个返回真的就是最终选择。`NPY_CPU_HAVE` 本质是查 `npy__cpu_have[]` 表（由 `npy_cpu_init()` 在导入时填好）。

#### 4.2.3 源码精读

分发宏模板的权威来源是 `meson_cpu/main_config.h.in`：

[meson_cpu/main_config.h.in:86-144](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson_cpu/main_config.h.in#L86-L144) —— 定义 `@P@CPU_DISPATCH_CURFX`：当处于某份 dispatch 编译（`@P@MTARGETS_CURRENT` 有定义）时，把 `NAME` 拼成 `NAME_<当前目标>`；否则（baseline 或关闭优化）原样返回 `NAME`。

[meson_cpu/main_config.h.in:201-268](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson_cpu/main_config.h.in#L201-L268) —— 定义 `@P@CPU_DISPATCH_CALL`，其回调 `@P@CPU_DISPATCH_CALL_CB_` 展开为 `(TESTED_FEATURES) ? (NAME_TARGET ...) :` 的三元链片段，最后由 baseline 回调兜底。这正是「短路选择最高目标」的实现。

[meson_cpu/main_config.h.in:284-319](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson_cpu/main_config.h.in#L284-L319) —— 定义 `@P@CPU_DISPATCH_INFO()`：由 `HIGH_CB_` 在运行时挑出当前命中的最高目标、由 `INFO_CB_` 列出全部可用目标，拼成两元素字符串数组。

而对外头文件 `npy_cpu_dispatch.h` 在此基础上提供「追踪」用的宏与函数声明：

[numpy/_core/src/common/npy_cpu_dispatch.h:10-21](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/common/npy_cpu_dispatch.h#L10-L21) —— 说明 `npy_cpu_dispatch_config.h` 是构建期生成的，含平台指令集头文件与一组辅助宏；特性 `#definitions` 通过编译器参数传入。

[numpy/_core/src/common/npy_cpu_dispatch.h:94-98](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/common/npy_cpu_dispatch.h#L94-L98) —— 定义 `NPY_CPU_DISPATCH_TRACE(FNAME, SIGNATURE)` 宏：调用 `NPY_CPU_DISPATCH_INFO()` 拿到「当前/可用」目标，再交给 `npy_cpu_dispatch_trace()` 记录。这是 4.4 节「运行时观察分发」的入口。

#### 4.2.4 代码实践

**实践目标**：理解 `multi_targets` 如何把一份 `.dispatch.c` 变成多份目标。

**操作步骤**：阅读 `_umath_tests` 的 Meson 配置：

[numpy/_core/meson.build:835-847](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/meson.build#L835-L847) —— `mod_features.multi_targets(...)` 接收 `dispatch: [X86_V3, X86_V2, ASIMDHP, ASIMD, NEON, VSX3, VSX2, VSX, VXE, VX, RVV]` 和 `baseline: CPU_BASELINE`，意为：为这些可分发目标各编译一份 `_umath_tests.dispatch.c`，外加一份 baseline。

**需要观察的现象**：`prefix: 'NPY_'` 决定了宏前缀；这份 `multi_targets` 的产物是一个静态库（后续 `_umath_tests_mtargets.static_lib(...)` 被链进测试模块）。

**预期结果**：你能复述出——「同一段 C 源码，被 Meson 用 11 组不同的 `NPY__CPU_TARGET_CURRENT` 定义 + 对应编译开关，编译成 11 份目标码，加上 1 份 baseline，共 12 份，链进同一个库」。**待本地验证**（可在构建目录 `build/` 下搜索 `_umath_tests_dispatch_func_AVX2` 之类符号确认）。

#### 4.2.5 小练习与答案

**练习 1**：`CURFX(func)` 在 baseline 编译里展开成什么？在 AVX2 编译里呢？
**答**：baseline 编译里 `MTARGETS_CURRENT` 未定义，`CURFX(func)` 展开为 `func`（不加后缀）；AVX2 编译里展开为 `func_AVX2`（见 `main_config.h.in` L138-L144）。

**练习 2**：为什么必须给符号加后缀？不加会怎样？
**答**：同一份源码被编译多次，每个目标都会定义同名函数；不加后缀会在链接时出现「重复定义」错误。后缀让每份目标码的符号唯一（`add_AVX512_SKX`、`add_AVX2`、`add`）。

**练习 3**：`CALL` 宏的「三元链」是按什么顺序排列目标的？
**答**：按「最高兴趣」优先排列（由 Meson `dispatch:` 列表的兴趣度排序），运行时短路选择第一个本机支持的目标，最后由 baseline 兜底（见 `main_config.h.in` L201-L268 与 how-it-works 文档）。

---

### 4.3 dispatch 源码示例（_umath_tests.dispatch.c）

#### 4.3.1 概念说明

「可分发源」（dispatch-able source）是文件名以 `.dispatch.c`（或 `.dispatch.cpp`）结尾的特殊 C 文件——构建系统识别这个扩展后，会把它**编译多次**。`_umath_tests.dispatch.c` 是 NumPy 自带的、专门用来测试分发工具的极简示例：它不真的做数值计算，只是用 `CURFX` 定义几个「能报告自己是谁」的函数和变量，从而在测试里验证「运行时分发有没有选对」。

#### 4.3.2 核心流程

```text
_umath_tests.dispatch.c
   │  对每个 dispatch 目标各编译一次（每次 NPY__CPU_TARGET_CURRENT 不同）
   │  CURFX(_umath_tests_dispatch_func) → _umath_tests_dispatch_func_AVX2 / _AVX512 / ...
   ▼
_umath_tests.c.src 里的 test_dispatch():
   │  NPY_CPU_DISPATCH_CALL(highest_func = _umath_tests_dispatch_func, ());
   │     → 运行时选最高目标，把对应函数指针/字符串塞进 dict
   │  NPY_CPU_DISPATCH_CALL_XB(...)  → 同上，但排除 baseline
   │  NPY_CPU_DISPATCH_CALL_ALL(...) → 把所有「本机支持」的目标都跑一遍收集
   ▼
返回字典 {"func":..., "var":..., "func_xb":..., "var_xb":..., "all":[...]}
```

`_XB` 后缀 = eXclude Baseline（即便 baseline 已启用也不调用基线版本）；`_ALL` = 把所有可用目标都触发一遍（用于把每个目标的名字收集进列表）。

#### 4.3.3 源码精读

可分发源本体——

[numpy/_core/src/umath/_umath_tests.dispatch.c:7-13](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/_umath_tests.dispatch.c#L7-L13) —— 引入分发头文件，并用 `NPY_CPU_DISPATCH_DECLARE` 对 `func`/`var`/`attach` 三个符号做前向声明。

[numpy/_core/src/umath/_umath_tests.dispatch.c:15-29](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/_umath_tests.dispatch.c#L15-L29) —— 关键：用 `NPY_CPU_DISPATCH_CURFX(_umath_tests_dispatch_func)` 定义函数，并用 `NPY_TOSTRING(NPY_CPU_DISPATCH_CURFX(func))` 把当前目标名变成字符串返回。每份编译（AVX2/AVX512/…）都产生一个不同后缀、返回不同字符串的版本；`attach` 把字符串追加进一个 Python list（供 `_ALL` 收集）。

使用方——`test_dispatch()` 演示了三种分发选择：

[numpy/_core/src/umath/_umath_tests.c.src:693-697](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/_umath_tests.c.src#L693-L697) —— 包含生成的 `_umath_tests.dispatch.h`，并对三个符号做 `DECLARE`。

[numpy/_core/src/umath/_umath_tests.c.src:699-736](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/_umath_tests.c.src#L699-L736) —— `test_dispatch()` 函数：`CALL` 选最高目标（含 baseline 兜底）、`CALL_XB` 选最高目标但排除 baseline（无可用 dispatch 目标时返回 `"nobase"`）、`CALL_ALL` 收集所有本机支持的目标名，最终拼成字典返回。

配套的 Meson 编译配置见 4.2.4 引用的 [numpy/_core/meson.build:835-847](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/meson.build#L835-L847)，它决定了 `dispatch:` 列表里有哪些目标。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「运行时分发」为 `_umath_tests` 选了哪个目标。

**操作步骤**：

```bash
python -c "
from numpy._core import _umath_tests
from numpy._core._multiarray_umath import __cpu_baseline__, __cpu_dispatch__, __cpu_features__
print('dispatch :', __cpu_dispatch__)
print('supported:', [k for k,v in __cpu_features__.items() if v and k in __cpu_dispatch__])
print('test_dispatch:', _umath_tests.test_dispatch())
"
```

并对照测试 [numpy/_core/tests/test_cpu_dispatcher.py:10-48](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/tests/test_cpu_dispatcher.py#L10-L48) 来理解输出含义。

**需要观察的现象**：

- `dict["func"]` 形如 `"func_AVX2"` 或 `"func"`（baseline，无后缀）——它是本机支持的「最高兴趣」目标。
- `dict["func_xb"]` 排除 baseline：有可用 dispatch 目标时与 `func` 相同；无 dispatch 目标时为 `"nobase"`。
- `dict["all"]` 列出所有「编译进来且本机支持」的目标，末尾追加 baseline 的 `"func"`。

**预期结果**：`dict["func"]` 报告的目标，应等于「`__cpu_dispatch__` ∩ 本机实际支持特性」中兴趣度最高者。**待本地验证**（不同机器选中的目标不同；若构建关闭了优化，则全为 baseline）。

#### 4.3.5 小练习与答案

**练习 1**：`CALL` 与 `CALL_XB` 的区别是什么？
**答**：`CALL` 在没有任何 dispatch 目标可用时会回退到 baseline（返回带 baseline 字符串的结果）；`CALL_XB` 排除 baseline，无可用 dispatch 目标时返回哨兵 `"nobase"`（见 `_umath_tests.c.src` L702-L707）。

**练习 2**：`_umath_tests.dispatch.c` 里为什么用 `NPY_TOSTRING(CURFX(func))` 而不是直接写字符串？
**答**：因为每份编译里 `CURFX(func)` 展开成不同的带后缀符号（`func_AVX2` 等），`NPY_TOSTRING` 把这个符号在**编译期**转成字符串字面量，于是每份目标码都能报告「自己是哪个目标」，无需运行时再判断。

---

### 4.4 运行时观察分发结果（opt_func_info 与环境变量）

#### 4.4.1 概念说明

光知道「会分发」不够，我们还想**问 NumPy：某个 ufunc 现在实际用的是哪条 SIMD 分支？** NumPy 提供了内置的「分发追踪器（tracer）」：

- 模块导入时，每个被分发的 ufunc 循环都会调用 `NPY_CPU_DISPATCH_TRACE(ufunc名, 签名)`，把「当前命中目标」和「全部可用目标」写进模块字典 `__cpu_targets_info__`。
- 公开函数 `np.lib.introspect.opt_func_info()` 用正则过滤后返回这个字典，让你一眼看到 `np.add`、`np.exp` 各自走的是 `AVX2` 还是 `AVX512F`。
- 两个环境变量 `NPY_ENABLE_CPU_FEATURES` / `NPY_DISABLE_CPU_FEATURES` 在导入时改变 CPU 特性探测结果，从而**改变分发选择**——这是观察「换一条分支性能差多少」的官方手段。

> ⚠️ 准确性提示：本讲的原始实践任务里提到 `NPY_CPU_DISPATCH_TRACE=1` 环境变量。在当前 HEAD 全仓库中**没有**这个环境变量；追踪在导入时**始终开启**，`__cpu_targets_info__` 总会被填充。正确入口是 `opt_func_info()`。`NPY_CPU_DISPATCH_TRACE` 只是 C 里一个**宏**的名字（见 4.2.3），不是环境变量。

#### 4.4.2 核心流程

```text
import numpy
  │
  ├─ npy_cpu_init(): 探测 CPU 特性 → 读 NPY_ENABLE/DISABLE_CPU_FEATURES 调整结果
  │
  ├─ npy_cpu_dispatch_tracer_init(mod): 在模块上创建空字典 __cpu_targets_info__
  │
  └─ 注册 ufunc 时（generate_umath 生成的代码）:
        为每个被分发的循环执行:
          NPY_CPU_DISPATCH_TRACE("exp", "dd")
            → INFO() 得到 ("AVX2", "AVX512F AVX2 baseline(SSE...)")
            → trace() 写入 __cpu_targets_info__["exp"]["dd"] = {current, available}

之后:
  np.lib.introspect.opt_func_info(func_name="exp")  → 读取并过滤该字典
```

#### 4.4.3 源码精读

模块初始化里两件相关的事——

[numpy/_core/src/multiarray/multiarraymodule.c:5043-5046](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5043-L5046) —— 在 `npy_cpu_init()` 之后调用 `npy_cpu_dispatch_tracer_init(m)`，创建追踪字典（始终调用，所以追踪始终开启）。

[numpy/_core/src/multiarray/multiarraymodule.c:5175-5203](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5175-L5203) —— 把 `__cpu_features__`（本机实际支持）、`__cpu_baseline__`（构建基线）、`__cpu_dispatch__`（可分发目标）三个列表暴露到模块字典。

tracer 的实现——

[numpy/_core/src/common/npy_cpu_dispatch.c:8-30](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/common/npy_cpu_dispatch.c#L8-L30) —— `npy_cpu_dispatch_tracer_init`：新建字典并挂到模块的 `__cpu_targets_info__` 键，同时记进静态数据 `cpu_dispatch_registry`；重复初始化会报错。

[numpy/_core/src/common/npy_cpu_dispatch.c:32-78](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/common/npy_cpu_dispatch.c#L32-L78) —— `npy_cpu_dispatch_trace`：按 `func_name → signature → {current, available}` 三层结构填字典。`current` 是运行时实际命中的最高目标，`available` 是全部可用目标。

每个 ufunc 循环的「TRACE 发射点」由代码生成器产生——

[numpy/_core/code_generators/generate_umath.py:1524-1537](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/code_generators/generate_umath.py#L1524-L1537) —— 对每个被分发（`t.dispatch` 非空）的 ufunc 循环，生成 `NPY_CPU_DISPATCH_TRACE("<ufunc名>", "<签名>")` 与 `NPY_CPU_DISPATCH_CALL_XB(...)` 两行。这就是 `__cpu_targets_info__` 里条目的来源。

公开查询函数——

[numpy/lib/introspect.py:8-94](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/introspect.py#L8-L94) —— `opt_func_info(func_name=None, signature=None)`：从 `_multiarray_umath` 导入 `__cpu_targets_info__`（L68），用正则按函数名和数据类型签名过滤后返回。文档里给出了典型输出：`{"absolute": {"Ff": {"current": "FMA3__AVX2", "available": "AVX512F FMA3__AVX2 baseline(...)"}}}`。

最后是改变分发选择的环境变量——

[numpy/_core/src/common/npy_cpu_features.c:46-70](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/common/npy_cpu_features.c#L46-L70) —— `npy_cpu_init()` 里用 `getenv` 读取 `NPY_ENABLE_CPU_FEATURES` / `NPY_DISABLE_CPU_FEATURES`（L53-L54）。二者不能同时设置（L57-L63），否则导入时报 `ImportError`。这是**唯一**两个影响 CPU 分发的环境变量。

#### 4.4.4 代码实践

**实践目标**：观察 `np.exp` 选中的 SIMD 分支，并通过禁用高指令集看它「降级」。

**操作步骤**：

第一步，正常导入并查询：

```bash
python -c "
import numpy as np
info = np.lib.introspect.opt_func_info(func_name='exp', signature='float64')
import json; print(json.dumps(info, indent=2))
"
```

第二步，禁用 AVX2/AVX512F 后再查询（**必须在导入 numpy 之前**设置环境变量）：

```bash
NPY_DISABLE_CPU_FEATURES="AVX2 AVX512F" python -c "
import numpy as np
info = np.lib.introspect.opt_func_info(func_name='exp', signature='float64')
import json; print(json.dumps(info, indent=2))
"
```

**需要观察的现象**：

- 第一步：`exp` 的 `dd`（float64→float64）签名下，`current` 通常是机器支持的最高目标（如 `AVX2` 或 `FMA3__AVX2` 或 `AVX512F`），`available` 列出全部可用目标。
- 第二步：禁用 AVX2/AVX512F 后，`current` 应回退到更低的目标（如 `SSE41` 或 baseline）。

**预期结果**：两次 `current` 不同，证明分发确实随 CPU 特性探测结果而变。**待本地验证**（具体目标名因机器/构建而异；若某特性本就不在 `__cpu_dispatch__` 里，禁用它无效果）。注意：必须用大写的特性名（如 `AVX2`、`AVX512F`），且与 `__cpu_dispatch__` 里的名字一致；可用 `np._core._multiarray_umath.__cpu_dispatch__` 查可用名。

#### 4.4.5 小练习与答案

**练习 1**：`opt_func_info` 返回的 `current` 和 `available` 分别是什么？
**答**：`current` 是导入时本机实际命中的「最高兴趣」SIMD 目标；`available` 是该循环被编译进来的全部目标（含 baseline）。来源是 `NPY_CPU_DISPATCH_INFO()` 宏（见 4.2.3）。

**练习 2**：同时设置 `NPY_ENABLE_CPU_FEATURES` 和 `NPY_DISABLE_CPU_FEATURES` 会怎样？
**答**：导入 NumPy 时抛 `ImportError`——二者不能同时设置（见 `npy_cpu_features.c` L57-L63）。

**练习 3**：为什么「禁用某指令集」要**在 import numpy 之前**设环境变量？
**答**：CPU 特性探测在模块导入（`npy_cpu_init`）时只跑一次并填表，所有后续分发都查这张表。导入之后再设环境变量已无作用。

---

### 4.5 asv 基准：用数字验证性能（bench_ufunc.py）

#### 4.5.1 概念说明

「我觉得 SIMD 更快」不是工程结论——需要**测量**。NumPy 用 [asv](https://asv.readthedocs.io)（airspeed velocity）做统计稳健的微基准：

- 每个基准是一个 Python 类，继承自 `Benchmark`（在 `benchmarks/benchmarks/common.py`，本仓库里它是个空标记类）。
- 类的 `setup()` 准备数据；每个 `time_*` 方法是一次被计时的测量。asv 会多次运行、取中位数与四分位距，并对机器噪声做温机（warmup）。
- `params` / `param_names` 让一个类展开成多组参数（如「对所有 ufunc × 所有 dtype」各测一次）。

`benchmarks/benchmarks/bench_ufunc.py` 是 ufunc 性能的主战场，它甚至有一条**完整性断言**：必须为 NumPy 的**每一个** ufunc 都提供基准，否则就报错——防止「悄悄加了个 ufunc 却没人测它」。

#### 4.5.2 核心流程

```text
写基准:
  class UFunc(Benchmark):
      params = [ufuncs]              # 对 ~95 个 ufunc 各跑一次
      param_names = ['ufunc']
      def setup(self, ufuncname): ...    # 取 np.<ufuncname>，准备好各 dtype 输入
      def time_ufunc_types(self, ufuncname): [self.ufn(*arg) for arg in self.args]

跑基准:
  spin bench -t UFunc                 # 跑 UFunc 类的所有参数组合
  spin bench -t UFunc --compare       # 对比 HEAD 与 main（在隔离环境各跑一次）
  # 或直接: cd benchmarks && asv run --bench UFunc
```

`spin bench` 把命令转发给 `asv`（见 `.spin/cmds.py` 的 `_run_asv`）。`--compare` 模式会在独立环境里分别检出两个提交、各跑一次再对比，并按 `--factor`（默认 1.05，即 ±5%）报告显著变化。

#### 4.5.3 源码精读

`bench_ufunc.py` 的头部与完整性检查——

[benchmarks/benchmarks/bench_ufunc.py:1-8](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/bench_ufunc.py#L1-L8) —— 导入 `numpy as np` 与公共工具 `Benchmark`、`TYPES1`、`get_squares_`。

[benchmarks/benchmarks/bench_ufunc.py:28-41](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/bench_ufunc.py#L28-L41) —— **完整性检查**：用 `dir(np)` 找出全部 `np.ufunc` 实例，减去已列出的 `ufuncs`，若仍有遗漏就 `raise NotImplementedError`。这条断言保证基准覆盖完整。

几类典型基准——

[benchmarks/benchmarks/bench_ufunc.py:91-112](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/bench_ufunc.py#L91-L112) —— `UFunc`：对每个 ufunc，用 `get_squares_()` 给出的各种 dtype 输入跑一遍，覆盖该 ufunc 支持的全部类型组合。

[benchmarks/benchmarks/bench_ufunc.py:321-355](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/bench_ufunc.py#L321-L355) —— `UFuncSmall`：故意用很小的数组/标量，**测的是 ufunc 调用的固定开销（overhead）**，而非 SIMD 吞吐——注释明确说明了这一点。

[benchmarks/benchmarks/bench_ufunc.py:68-75](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/bench_ufunc.py#L68-L75) —— `Broadcast`：测 `d - e`（`(50000,100)` 与 `(100,)` 广播相减），反映广播路径的性能。

[benchmarks/benchmarks/bench_ufunc.py:379-415](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/bench_ufunc.py#L379-L415) —— `CustomInplace`：对比「就地运算 `np.add(f, 1., out=f)`」与「临时表达式 `1. + f + 1.`」。就地省去临时数组分配，差距明显。

[benchmarks/benchmarks/bench_ufunc.py:574-600](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/bench_ufunc.py#L574-L600) —— `BinaryBench`：对 1,000,000 元素的 float32/float64 数组测 `np.power`、`np.arctan2` 等，是观察 SIMD 实际吞吐的主力基准。

公共工具与命令——

[benchmarks/benchmarks/common.py:214-215](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/benchmarks/benchmarks/common.py#L214-L215) —— `class Benchmark: pass`，asv 靠「是否继承 `Benchmark`」识别基准类，所以它只需是个标记基类。

[.spin/cmds.py:411-435](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/.spin/cmds.py#L411-L435) —— `spin bench` 命令：支持 `-t/--bench` 选基准、`-c/--compare` 跨提交对比（默认对比 `main` 与 `HEAD`）、`-q/--quick` 快速模式（每项只跑一次，不准）、`--factor` 显著性阈值、`--cpu-affinity` 绑核。

#### 4.5.4 代码实践

**实践目标**：用 asv 量化「就地 vs 临时」和「不同 dtype」的性能差。

**操作步骤**（任选一种）：

方式 A——用 `spin bench`：

```bash
# 跑 CustomInplace（就地 vs 临时表达式对比）
spin bench -t CustomInplace

# 跑 BinaryBench（大数组 power/arctan2 的 SIMD 吞吐）
spin bench -t BinaryBench
```

方式 B——直接用 `timeit` 做一次快速手测（无需 asv 环境）：

```bash
python -c "
import numpy as np, timeit
f = np.zeros(150000, dtype=np.float32)
def inplace(): np.add(f, 1., out=f)
def temp():    1. + f + 1.
n = 1000
print('inplace us:', round(min(timeit.repeat(inplace, number=n))/n*1e6, 2))
print('temp    us:', round(min(timeit.repeat(temp,    number=n))/n*1e6, 2))
"
```

**需要观察的现象**：

- `CustomInplace` 基准里，`time_float_add_temp`（临时表达式）应明显慢于 `time_float_add`（就地）——因为临时版本要额外分配一个中间数组。
- `BinaryBench` 里，`time_pow`（数组 `**` 数组）应慢于 `time_pow_2`（数组 `**` 标量）——标量路径更省内存且循环更简单。
- 手测里 `inplace` 的单次耗时应低于 `temp`。

**预期结果**：就地比临时快（差距取决于数组大小与分配开销）；大数组上 SIMD 才能体现吞吐优势，小数组上测到的主要是 overhead（对应 `UFuncSmall` 基准）。**待本地验证**（绝对数值因机器而异；asv 报告的是相对变化与置信区间）。

#### 4.5.5 小练习与答案

**练习 1**：`UFuncSmall` 为什么故意用很小的数组？
**答**：它测的是 ufunc 调用本身的固定开销（参数解析、循环选择、调度），而非 SIMD 计算吞吐。数组很小（如 5 个元素）时，计算量可忽略，耗时几乎全是 overhead（见 `bench_ufunc.py` L321-L327 的类注释）。

**练习 2**：`bench_ufunc.py` 顶部的 `missing_ufuncs` 检查起什么作用？
**答**：它把「NumPy 全部 ufunc」减去「已写基准的 ufunc」，若有遗漏就报错，强制每个 ufunc 都有基准覆盖（见 L33-L41）。这防止新增 ufunc 后无人测它的性能回归。

**练习 3**：`spin bench --compare` 默认对比哪两个提交？为什么要「在隔离环境各跑一次」？
**答**：默认对比 `main` 与 `HEAD`（见 `.spin/cmds.py` L436-L437）。隔离环境（各 checkout 一次、独立构建）是为了让两次运行只有「代码版本」这一个变量，排除共享构建产物或依赖版本不同带来的干扰。

---

## 5. 综合实践

把本讲的知识串起来：**定位、观察、量化一个 ufunc 的 SIMD 加速。**

1. **定位**：用 `np.lib.introspect.opt_func_info(func_name="exp", signature="float64")` 找出 `np.exp` 对 float64 当前命中的 SIMD 目标（记下 `current`，比如 `AVX2`）。
2. **观察降级**：用 `NPY_DISABLE_CPU_FEATURES="<上一步的 current 目标>"`（**导入前**设置）重新查询，确认 `current` 回退到更低的目标。
3. **量化影响**：用 `timeit`（或 `spin bench -t BinaryBench`）对一个大数组（如 `np.random.rand(10_000_000)`）跑 `np.exp`，分别在「正常」与「禁用高指令集」两种环境下计时，计算加速比。

参考代码骨架：

```python
# 正常环境
python -c "
import numpy as np, timeit
a = np.random.rand(10_000_000)
n = 50
print('normal  ms:', round(min(timeit.repeat(lambda: np.exp(a), number=n))/n*1e3, 2))
print('target  :', np.lib.introspect.opt_func_info(func_name='exp', signature='float64'))
"
# 禁用环境（把 AVX2 换成你机器上 opt_func_info 报告的 current 目标）
NPY_DISABLE_CPU_FEATURES="AVX2" python -c "
import numpy as np, timeit
a = np.random.rand(10_000_000)
n = 50
print('disabled ms:', round(min(timeit.repeat(lambda: np.exp(a), number=n))/n*1e3, 2))
print('target   :', np.lib.introspect.opt_func_info(func_name='exp', signature='float64'))
"
```

**需要观察的现象**：禁用高指令集后，`np.exp` 的耗时应明显上升，加速比大致反映 SIMD 寄存器宽度差异（如 AVX2→SSE 约慢 2 倍以上，具体待本地验证）。

**预期结果**：你能用一句话回答「`np.exp` 在这台机器上走的是哪条 SIMD 分支，禁用它之后慢了多少」，并把结论与 `opt_func_info` 的 `current` 对应起来。

## 6. 本讲小结

- NumPy 的性能来自三层：用**通用内联** `npyv_*` 写一份 SIMD 循环 → 构建期按 `--cpu-baseline` / `--cpu-dispatch` 编译出 baseline + 多个 dispatch 版本 → 导入时探测 CPU 并挑选最合适的那份。
- **baseline** 无守卫、永远开启、用于任何源文件；**dispatch** 被 `NPY__CPU_TARGET_` 守卫、只在 `*.dispatch.c` 可分发源里激活，运行时按需挑。
- 一组预处理器宏完成机械工作：`CURFX` 给符号加目标后缀避免链接冲突、`DECLARE` 前向声明、`CALL` 在运行时用 `NPY_CPU_HAVE` 做「短路三元链」选最高目标、`INFO` 报告当前/可用目标。
- Meson 的 `mod_features.multi_targets(dispatch:[...], baseline:[...])` 是把一份 `.dispatch.c` 编译成多份目标码的配置入口（见 `_umath_tests` 的构建配置）。
- **观察分发**的正确入口是 `np.lib.introspect.opt_func_info()`（读取始终开启的 `__cpu_targets_info__` 字典）；改变分发的唯一环境变量是 `NPY_ENABLE/DISABLE_CPU_FEATURES`。当前 HEAD **没有** `NPY_CPU_DISPATCH_TRACE` 环境变量（那只是个 C 宏名）。
- **量化性能**用 asv：`bench_ufunc.py` 覆盖全部 ufunc 并强制完整性检查；`spin bench` 封装 asv，支持 `--compare` 跨提交对比。就地运算比临时表达式快、大数组上 SIMD 才体现吞吐、小数组上测的是 overhead。

## 7. 下一步学习建议

- **自定义 dtype 与 ArrayMethod**（u8-l3）：本讲只看了「内置 ufunc 的 SIMD 分发」。新一代 ArrayMethod 机制（NEP 43）是注册自定义循环/转换的统一接口，下一步可读 `numpy/_core/src/multiarray/descriptor.c`、`dtypemeta.c` 与 `dtype_api.h`，理解自定义 dtype 如何提供自己的 strided loop。
- **C-API 与扩展开发**（u8-l2）：若想在自己的 C 扩展里用上分发宏，需先掌握 `include/numpy/` 头文件体系与 `PyArray_SimpleNew`/`PyArray_DATA` 等访问宏。
- **继续阅读 SIMD 实现**：挑一个你常用的 ufunc（如 `np.sin`），找到它在 `numpy/_core/src/umath/loops_*.dispatch.c.src` 里的循环，对照本讲的宏体系，读懂它的 CONTIG/NCONTIG 两条路径与 `npyv_*` 调用。
- **基准体系全貌**（u9-l4）：本讲只碰了 `bench_ufunc.py`；完整理解 asv 的组织、`spin bench` 的工程命令与多平台 wheel 构建，可留到 u9-l4。
