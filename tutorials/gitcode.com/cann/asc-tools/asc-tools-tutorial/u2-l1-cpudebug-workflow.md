# CPU Debug 工作原理与使用流程

## 1. 本讲目标

在上一篇（u1-l4）里，我们已经用 `build.sh` 编译出了 asc-tools 的 run 包，并用 `cmake -DCMAKE_ASC_RUN_MODE=cpu` 把 `add` 样例在 CPU 上跑通，看到了 `[Success] Case accuracy is verification passed.`。本讲要回答的是：

- 为什么同一份 Ascend C 算子源码，**不改一行**就能在 CPU 和 NPU 两个域里运行？
- CPU Debug 到底是**靠什么**把 `<<<>>>` 这种 NPU 风格的核函数启动，变成 CPU 上能跑的普通 C++ 调用的？
- `cpu_debug_launch.h` 这个"入口头文件"在里面扮演了什么角色？

学完本讲，你应当能够：

1. 用"孪生调试"的思想，说清 CPU Debug 的核心价值。
2. 读懂 `cpu_debug_launch.h` 里 `AscCPUKernelLaunch` 的每一个参数。
3. 解释 `<<<>>>` 核函数调用在 CPU 模式下的"转义"机制，并说明它为何不影响 NPU 模式。

## 2. 前置知识

本讲默认你已经读过 u1 系列讲义，了解 asc-tools 的工具全景、目录结构和基本编译流程。再补充三个轻量概念：

- **`<<<>>>` 核函数启动语法**：这是借鉴自 CUDA 的写法，形如 `func<<<grid, workspace, stream>>>(args)`，表示"以 `grid` 个核、在 `stream` 上启动核函数 `func`"。它是 Ascend C（ASC 语言）层面的语法，不是标准 C++。
- **编译期 lowering（转译/降级）**：`<<<>>>` 并不是 CPU 能直接执行的指令。它需要由 **bisheng 编译器**在编译时把它"翻译（lowering）"成底层能理解的形式。在 NPU 模式下翻译成真实 NPU 启动；在 CPU 模式下翻译成一个普通 C++ 函数调用。
- **fork 子进程**：Unix 系统调用，父进程调用一次 `fork()` 会复制出一个子进程，两者从调用点继续各自执行。CPU Debug 用它来模拟 NPU 的"多核并行"（每个核一个子进程）。本讲只在概念层面提及，深入剖析放在 u3-l1。

> 提示：bisheng 编译器（解析 ASC 语言、定义 `ASCENDC_CPU_DEBUG` 宏、把 `<<<>>>` 做 lowering 的那一层）属于 CANN 闭源工具链，不在本开源仓库内。本讲凡涉及该工具链之处都会标注，你能从开源仓库确认的部分，我们都给出真实源码与行号。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/01_cpu_debug.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md) | CPU Debug 官方使用文档，定义"两步走"流程 |
| [cpudebug/include/cpu_debug_launch.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h) | CPU Debug 的**入口头文件**，定义 `AscCPUKernelLaunch` |
| [cpudebug/include/tikicpulib.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/tikicpulib.h) | 聚合整套 CPU 仿真基础头文件的总开关 |
| [cpudebug/include/kern_fwk.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h) | 真正执行核函数的 `RunKernelFunctionOnCpu` 与 `ICPU_RUN_KF` 宏 |
| [examples/02_cpudebug/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc) | Add 算子样例，含 `#ifdef` 块与 `<<<>>>` 启动 |
| cpudebug/CMakeLists.txt | 编译 cpudebug 库时定义 `ASCENDC_CPU_DEBUG=1` |

记忆线索：**入口头文件** `cpu_debug_launch.h` → 调用 **执行框架** `kern_fwk.h` → 链接 **整套仿真基础** `tikicpulib.h`；而样例 `add.asc` 用 `#ifdef` 决定是否把这条链路接进来。

---

## 4. 核心概念与源码讲解

### 4.1 CPU Debug 概念

#### 4.1.1 概念说明

CPU Debug 解决的是"**算子上 NPU 之前怎么验证**"的问题。直接在 NPU 上调试一个 Ascend C 算子非常困难：NPU 内部是黑盒，没有 gdb，没有 valgrind，崩溃了也只能拿到一段模糊的硬件日志。CPU Debug 的做法是**孪生调试**——用 CPU 构造一个 NPU 行为的"孪生体"，让同一份 Ascend C 源码经过 GCC/bisheng 编译后，变成一个普通的 CPU 可执行程序，于是你就可以用 gdb 这类成熟工具去断点、单步、看内存。

官方文档一句话定位：

> 在算子部署到 NPU 上之前，CPU Debug 工具帮助用户在 CPU 上进行功能和精度的基本验证。开发者使用 Ascend C 编写算子 Kernel 侧源码，通过 bisheng 编译器编译生成 CPU 域的可执行程序，即可使用 gdb 等常规调试手段对算子进行调试。见 [docs/01_cpu_debug.md:5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L5)。

这里有三个关键词要分清：

- **CPU 域 vs NPU 域**：算子的两种运行环境。"域"就是 execution domain。
- **功能验证 vs 精度验证**：前者检查"算得对不对"（不崩、逻辑正确），后者检查"算得准不准"（数值误差可接受）。
- **孪生调试**：CPU 域并不是把算子重新实现一遍，而是"模拟" NPU 的存储层级（Global/Local Memory）、并行模型（多核、warp）和内建函数（Add、DataCopy……），让源码感觉自己在 NPU 上跑。

#### 4.1.2 核心流程

CPU Debug 的使用被官方文档总结为"两步走"：

```
┌─────────────────────────────────────────────────────────────────┐
│  Ascend C 算子源码 (.asc)，例如 add.asc                          │
└─────────────────────────────────────────────────────────────────┘
        │
        │ 用 bisheng 编译，由模式决定走哪条 lowering 路径
        ├──────────────────────────────┐
        ▼ NPU 模式 (默认)              ▼ CPU 模式 (CMAKE_ASC_RUN_MODE=cpu)
   编译器把 <<<>>> lowering         编译器定义 ASCENDC_CPU_DEBUG，
   为真实 NPU 启动                  把 <<<>>> lowering 为 AscCPUKernelLaunch
   不引入 cpu_debug_launch.h        引入 cpu_debug_launch.h
   产物：在 NPU 上运行              链接 cpudebug 仿真库
                                    产物：CPU 可执行程序（可用 gdb 调试）
```

落到操作上就是：

1. **加头文件**：在调用 `<<<>>>` 的源文件里加一段 `#ifdef ASCENDC_CPU_DEBUG #include "cpu_debug_launch.h" #endif`。
2. **编译并运行**：`cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..` 后 `make`，得到可执行程序直接运行。

关键点：步骤 1 加的 `#ifdef` 代码，**在 NPU 模式下不会产生任何影响**——这点我们会在 4.3 节详细拆解。

#### 4.1.3 源码精读

文档把"两步走"讲得很明确，先看步骤 1（加头文件），见 [docs/01_cpu_debug.md:20-25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L20-L25)：

```c
#ifdef ASCENDC_CPU_DEBUG
#include "cpu_debug_launch.h"
#endif
```

文档紧接着点明了 CPU Debug 的核心机制——**转义**：

> bisheng 编译器在 CPU 调试模式下会通过该头文件对 `<<<>>>` 形式的核函数调用进行转义，从而在 CPU 上执行核函数，该修改不会影响代码在 NPU 模式下的编译运行。

再看步骤 2 的编译命令，见 [docs/01_cpu_debug.md:31-35](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L31-L35)：

```bash
mkdir -p build && cd build;
cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..;make -j;
./add
```

`CMAKE_ASC_RUN_MODE=cpu` 就是触发 CPU 域编译的开关；`CMAKE_ASC_ARCHITECTURES` 告诉 CMake 链接哪个 NPU 架构对应的 CPU 调试依赖库（`dav-2201` → Atlas A2/A3 系列，`dav-3510` → Ascend 950PR/950DT）。

#### 4.1.4 代码实践

**实践目标**：建立"为什么 CPU Debug 能缩短开发周期"的直觉。

**操作步骤**：

1. 打开 [docs/01_cpu_debug.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md)，阅读"概述"和"调试方法"两节。
2. 对比"在 NPU 上调试算子"和"在 CPU 上调试算子"分别能用哪些工具。

**需要观察的现象**：文档明确说 CPU 域可执行程序支持 gdb 的断点、查看寄存器/内存、单步、调用栈（见 [docs/01_cpu_debug.md:46](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L46)），这些恰恰是 NPU 上最难得到的。

**预期结果**：你能用一段话说明——CPU Debug 的价值不在"算得更快"，而在"把问题前移到有成熟调试工具的 CPU 域"，从而缩短"写算子→上板→失败→再改"的循环周期。

#### 4.1.5 小练习与答案

**练习 1**：CPU Debug 生成的可执行程序，和一段普通的 C++ 程序有什么相同点？
**答案**：两者都是 GCC/bisheng 编译出的本地 ELF 可执行程序，因此都能用 gdb、valgrind、nm、objdump 等常规工具来调试和检视。

**练习 2**：为什么说 CPU Debug 是"孪生"而不是"重写"？
**答案**：因为它没有把算子逻辑重新实现一遍，而是模拟了 NPU 的存储层级、并行模型和内建函数，让**同一份** Ascend C 源码能在 CPU 上跑起来；源码本身一行不改。

---

### 4.2 cpu_debug_launch 入口

#### 4.2.1 概念说明

`cpu_debug_launch.h` 是整个 CPU Debug 的**入口头文件**——它是源码与 cpudebug 仿真库之间的"接线板"。一旦它被 `#include` 进来，就同时发生了两件事：

1. **引入整套仿真基础**：通过 `tikicpulib.h` 把 fp16/bf16/fp8 等数据类型、stub（内建函数桩）、`kern_fwk.h`（执行框架）全部拉进来。
2. **定义启动函数 `AscCPUKernelLaunch`**：把"启动一个核函数"这件事，接到 cpudebug 的执行框架 `RunKernelFunctionOnCpu` 上。

换句话说，`#include "cpu_debug_launch.h"` 这一行，等于按下了 CPU 仿真的总开关。

#### 4.2.2 核心流程

`AscCPUKernelLaunch` 的逻辑非常简短，只做两步：

```
AscCPUKernelLaunch(numBlocks, dynicsize, stream, mangling, kernelFunc, args...)
        │
        ├─ 1) SetKernelMode(GetKenelMode(mangling))
        │      按核函数名(mangling)查注册的 kernel 模式并设置
        │
        └─ 2) RunKernelFunctionOnCpu(kernelFunc, mangling, numBlocks, args...)
               在 CPU 上真正执行核函数（fork 多核模拟，详见 u3-l1）
```

这里有一个重要的"职责分层"思想：**入口函数只负责"分发"，不负责"执行"**。`AscCPUKernelLaunch` 自己不模拟多核、不分配内存，它把这些脏活累活全部委托给 `RunKernelFunctionOnCpu`。这样编译器只需认识 `AscCPUKernelLaunch` 这一个名字，就能把核函数启动整个托付给 cpudebug。

#### 4.2.3 源码精读

先看入口头文件引入了哪些依赖，见 [cpu_debug_launch.h:14-18](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L14-L18)：

```cpp
#include "acl/acl.h"
#include "stub_def.h"
#include "tikicpulib.h"
#include "kern_fwk.h"
#include "kernel_elf_parser.h"
```

其中 `tikicpulib.h` 是一个"总开关"，它把整套仿真基础头文件聚在一起，见 [tikicpulib.h:18-30](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/tikicpulib.h#L18-L30)：

```cpp
#include "kernel_fp16.h"
#include "kernel_bf16.h"
#include "kernel_vectorized.h"
#include "kernel_fp8_e5m2.h"
#include "kernel_fp8_e4m3.h"
#include "kernel_fp8_e8m0.h"
#include "kernel_fp4_e2m1.h"
#include "kernel_fp4_e1m2.h"
#include "kernel_hif8.h"
#include "stub_def.h"
#include "stub_fun.h"
#include "stub_reg.h"
#include "kern_fwk.h"
```

可以看到，Ascend C 源码里用到的 fp16/bf16/fp8/fp4/hif8 等低精度类型，以及内建函数的 stub 注册，全部由这里一次性引入。这也是为什么 `#include "cpu_debug_launch.h"` 一行就能让源码在 CPU 上"感觉自己在 NPU 上"。

再看核心的 `AscCPUKernelLaunch` 定义，见 [cpu_debug_launch.h:21-27](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L21-L27)：

```cpp
template <typename T, typename... Args>
inline void AscCPUKernelLaunch(
    unsigned numBlocks, void* dynicsize, aclrtStream stream, const char* mangling, T kernelFunc, Args... args)
{
    AscendC::SetKernelMode(KernelModeRegister::GetInstance().GetKenelMode(mangling));
    AscendC::RunKernelFunctionOnCpu(kernelFunc, mangling, numBlocks, args...);
}
```

逐个参数解读（这是本讲最重要的细节）：

| 参数 | 类型 | 含义 | 对应 `<<<>>>` 的哪部分 |
|------|------|------|------------------------|
| `numBlocks` | `unsigned` | 启动的 block（核）数量 | `<<<` **第一参** `numBlocks` |
| `dynicsize` | `void*` | 动态参数指针（本例传 `nullptr`） | `<<<` **第二参** `nullptr` |
| `stream` | `aclrtStream` | ACL 流 | `<<<` **第三参** `stream` |
| `mangling` | `const char*` | 编译器注入的核函数名字符串 | 编译器自动补充 |
| `kernelFunc` | `T`（模板） | 核函数指针 | `>>>` 前的函数名 |
| `args...` | `Args...` | 传给核函数的实参 | `>>>()` 括号里的参数 |

> 说明：`dynicsize` 是源码里的原始命名（疑似 "dynamic ..." 之意），在 add 样例里始终传 `nullptr`。它的精确语义属工具链细节，本讲不展开。

注意第 25 行那个看起来像拼写错误的方法名 `GetKenelMode`——它是仓库里的真实写法，不是笔误，照原样引用即可。它的作用是按核函数名（`mangling`）查出该核注册的运行模式。

#### 4.2.4 代码实践

**实践目标**：把 `AscCPUKernelLaunch` 的形参与样例里 `<<<>>>` 的实参一一对应起来。

**操作步骤**：

1. 打开 [cpu_debug_launch.h:21-27](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L21-L27)，记下 6 个形参。
2. 打开 [add.asc:123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123)，找到 `add_custom<<<numBlocks, nullptr, stream>>>(xDevice, yDevice, zDevice);`。
3. 画一张映射表，把 `numBlocks / nullptr / stream` 和 `xDevice / yDevice / zDevice` 分别对应到 `AscCPUKernelLaunch` 的形参上。

**需要观察的现象**：核函数名 `add_custom` 既是 `kernelFunc`（函数指针），又会被编译器字符串化后作为 `mangling`（函数名字符串）传入。

**预期结果**：你得到一张类似上表的对应关系，并能指出 `mangling` 这一项在源码的 `<<<>>>` 调用里是**看不见**的——它由编译器在 lowering 时自动补充。

#### 4.2.5 小练习与答案

**练习 1**：`AscCPUKernelLaunch` 函数体里做了哪两件事？
**答案**：先调用 `SetKernelMode(...GetKenelMode(mangling))` 按函数名设置 kernel 模式，再调用 `RunKernelFunctionOnCpu(kernelFunc, mangling, numBlocks, args...)` 在 CPU 上执行核函数。

**练习 2**：为什么 `tikicpulib.h` 要一次聚合十几个 `kernel_fp*.h` 和 `stub_*.h` 头文件？
**答案**：因为 Ascend C 源码会用到 fp16/bf16/fp8 等多种低精度类型，以及 Add、DataCopy 等内建函数；CPU 域必须为它们各提供一套仿真实现，`tikicpulib.h` 作为总开关一次性引入，让一个 `#include` 就配齐整个仿真环境。

**练习 3**：`AscCPUKernelLaunch` 的 `dynicsize` 和 `stream` 这两个参数在 add 样例里分别是什么值？
**答案**：`dynicsize` 是 `nullptr`（`<<<>>>` 第二参），`stream` 是一个 `aclrtStream`（由 `aclrtCreateStream(&stream)` 创建，`<<<>>>` 第三参）。

---

### 4.3 核函数调用转义

#### 4.3.1 概念说明

`<<<>>>` 是 ASC 语言层面的核函数启动语法，**不是标准 C++**。CPU 的 GCC 无法直接理解它。所谓"转义"（lowering），就是 bisheng 编译器在编译期，根据当前是 CPU 模式还是 NPU 模式，把 `<<<>>>` **改写**成不同的底层代码：

- **CPU 模式**：改写成对 `AscCPUKernelLaunch` 的普通 C++ 函数调用。
- **NPU 模式**：改写成真实的 NPU kernel 启动。

这就是"同一份源码、两个域都能跑"的根本原因——切换的不是源码，而是**编译器对 `<<<>>>` 的翻译策略**。

#### 4.3.2 核心流程

下面用一段"概念性示例"展示 CPU 模式下的转义结果（注意：这是**示例代码**，用于说明编译器的 lowering 形态，不是仓库里的真实产物）：

```cpp
// 你在源码里写的（add.asc:123）：
add_custom<<<numBlocks, nullptr, stream>>>(xDevice, yDevice, zDevice);

// CPU 模式下，编译器概念上的转义结果（示例代码）：
AscCPUKernelLaunch(numBlocks, nullptr, stream, "add_custom", add_custom, xDevice, yDevice, zDevice);
```

为什么 NPU 模式完全不受影响？因为两个域各自的"接线"是互斥的：

```
┌─────────────────────── NPU 模式 ────────────────────────┐
│ ASCENDC_CPU_DEBUG 未定义                                │
│ → #ifdef 块被跳过，cpu_debug_launch.h 不被引入          │
│ → AscCPUKernelLaunch 不参与编译                         │
│ → <<<>>> lowering 为真实 NPU 启动                       │
└─────────────────────────────────────────────────────────┘

┌─────────────────────── CPU 模式 ────────────────────────┐
│ ASCENDC_CPU_DEBUG 已定义 (=1)                           │
│ → #ifdef 块生效，引入 cpu_debug_launch.h                │
│ → AscCPUKernelLaunch 可见                               │
│ → <<<>>> lowering 为 AscCPUKernelLaunch 调用            │
│ → 链接 cpudebug 仿真库                                  │
└─────────────────────────────────────────────────────────┘
```

两条路径各走各的，源码共用，所以 `#ifdef` 那几行在 NPU 模式下"如同不存在"，文档也明确说"该修改不会影响代码在 NPU 模式下的编译运行"，见 [docs/01_cpu_debug.md:25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L25)；切回 NPU 模式时也"无需移除"，见 [docs/01_cpu_debug.md:80-82](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L80-L82)。

> 关于 `ASCENDC_CPU_DEBUG` 的来源：编译 cpudebug 仿真库本身时，它由 `cpudebug/CMakeLists.txt` 的 `target_compile_definitions(... ASCENDC_CPU_DEBUG=1 ...)` 定义，见 [cpudebug/CMakeLists.txt:75](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L75)。对样例的 `.asc` 源码而言，该宏由 CANN 提供的 ASC CMake 工具链在 `CMAKE_ASC_RUN_MODE=cpu` 时注入（属闭源 CANN 工具链，本仓库不包含其实现，**待确认**其具体注入位置）。

#### 4.3.3 源码精读

先看样例里的 `#ifdef` 块，见 [add.asc:20-22](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L20-L22)：

```cpp
#ifdef ASCENDC_CPU_DEBUG
#include "cpu_debug_launch.h"
#endif
```

它正好印证了上面的流程图：只有 CPU 模式下 `ASCENDC_CPU_DEBUG` 有定义，`cpu_debug_launch.h` 才会被引入，`AscCPUKernelLaunch` 才可见。

再看被转义的那一行核函数启动，见 [add.asc:123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123)：

```cpp
add_custom<<<numBlocks, nullptr, stream>>>(xDevice, yDevice, zDevice);
```

对应的核函数定义本身（在 NPU 模式下会被编译成真实 kernel，在 CPU 模式下被当作普通函数调用），见 [add.asc:90-95](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L90-L95)：

```cpp
__global__ __vector__ void add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z)
{
    KernelAdd op;
    op.Init(x, y, z);
    op.Process();
}
```

最后看一个能帮我们理解"转义目标形态"的宏 `ICPU_RUN_KF`。它定义在 [kern_fwk.h:186-189](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L186-L189)：

```cpp
#define ICPU_RUN_KF(func, numBlocks, ...)                                       \
    do {                                                                        \
        AscendC::RunKernelFunctionOnCpu(func, #func, numBlocks, ##__VA_ARGS__); \
    } while (0)
```

这个宏非常有启发意义：它揭示了 lowering 的"等价写法"——`func` 作为函数指针传入，`#func`（字符串化）作为函数名传入。对比 `AscCPUKernelLaunch` 的实现（[cpu_debug_launch.h:26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L26)），你能看出编译器对 `<<<>>>` 的转义，本质上就是生成一个"把核函数名和核函数指针一起喂给 `RunKernelFunctionOnCpu`"的调用。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手验证"转义"机制，并解释 `#ifdef` 为何对 NPU 模式无害。

**操作步骤**：

1. 在 [add.asc:20-22](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L20-L22) 找到 `#ifdef ASCENDC_CPU_DEBUG` 块。
2. 用一句话说明：为什么把 `#include "cpu_debug_launch.h"` 写在 `#ifdef` 里，NPU 模式就不会受影响？
3. 对照 [cpu_debug_launch.h:22-23](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L22-L23) 的 `AscCPUKernelLaunch` 形参，把 [add.asc:123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123) 的 `<<<numBlocks, nullptr, stream>>>(xDevice, yDevice, zDevice)` 逐项对应上去。
4. 写出这行 `<<<>>>` 在 CPU 模式下被转义后的等价调用（示例代码形式）。

**需要观察的现象**：你应能发现，`<<<>>>` 三参正好对应 `AscCPUKernelLaunch` 的前三个形参，而核函数名 `add_custom` 同时扮演"函数指针"和"名字符串"两个角色。

**预期结果**：

- 第 2 步答：NPU 模式下 `ASCENDC_CPU_DEBUG` 未定义，`#ifdef` 块整体被预处理器剔除，`cpu_debug_launch.h` 既不引入也不参与编译，因此 `<<<>>>` 走 NPU lowering、`AscCPUKernelLaunch` 完全不参与，互不干扰。
- 第 4 步示例答案：`AscCPUKernelLaunch(numBlocks, nullptr, stream, "add_custom", add_custom, xDevice, yDevice, zDevice);`

#### 4.3.5 小练习与答案

**练习 1**：如果把 `#include "cpu_debug_launch.h"` 直接写到 `#ifdef` **外面**（即无条件引入），NPU 模式会怎样？
**答案**：`cpu_debug_launch.h` 依赖 `tikicpulib.h`、`kern_fwk.h` 等 cpudebug 仿真专用头与符号，NPU 模式下并不链接 cpudebug 仿真库，会导致编译报错或链接失败。因此必须用 `#ifdef ASCENDC_CPU_DEBUG` 包裹，让它在 NPU 模式下完全不参与编译。

**练习 2**：`ICPU_RUN_KF` 宏和 `AscCPUKernelLaunch` 是什么关系？
**答案**：两者都是把"启动核函数"接到 `RunKernelFunctionOnCpu` 上。`ICPU_RUN_KF` 是手动调用的宏（展示了转义的目标形态），`AscCPUKernelLaunch` 是编译器在 CPU 模式 lowering `<<<>>>` 后实际调用的函数，它比 `ICPU_RUN_KF` 多做了一步 `SetKernelMode`。

**练习 3**：为什么说 `mangling` 这个参数在源码的 `<<<>>>` 调用里是"看不见的"？
**答案**：因为 `mangling`（核函数名字符串）是编译器在 lowering 时根据核函数名自动生成并塞进 `AscCPUKernelLaunch` 实参列表的，源码里写 `<<<>>>` 时并不需要、也无法手填这个字符串参数。

---

## 5. 综合实践

设计一个贯穿本讲的端到端走查任务，把"概念 → 入口 → 转义"串起来。

**任务**：在 CPU 模式下完整走一次"核函数启动到 CPU 执行"的链路，并用工具确认转义确实发生。

**操作步骤**：

1. 按上一篇 u1-l4 的方式，先确保 asc-tools run 包已安装（`libcpudebug.so` 等就位）。
2. 在 `examples/02_cpudebug` 目录下编译并运行：
   ```bash
   mkdir -p build && cd build
   cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..
   make -j
   ./add
   ```
3. 用 `nm` 检查可执行程序里是否真的链接进了 cpudebug 的启动符号（**待本地验证**，符号可能因模板实例化而被内联/裁剪）：
   ```bash
   nm ./add | grep -iE 'AscCPUKernelLaunch|RunKernelFunctionOnCpu'
   ```
4. 用 `ldd` 确认程序依赖了 cpudebug 仿真库：
   ```bash
   ldd ./add | grep -iE 'cpudebug|tikcpp'
   ```
5. 在 [add.asc:123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123) 旁，手写一行这行 `<<<>>>` 被转义后的等价 `AscCPUKernelLaunch` 调用作为注释。

**需要观察的现象**：

- 第 2 步输出 `[Success] Case accuracy is verification passed.`（与 u1-l4 一致）。
- 第 3 步至少能看到 `RunKernelFunctionOnCpu` 相关符号（若 `AscCPUKernelLaunch` 因 `inline` 被内联而搜不到，也属正常，说明它已被合并进调用方）。
- 第 4 步能看到程序链接到 `libcpudebug.so` 或其软链 `libtikcpp_debug.so`（见 u1-l4 提到的软链关系）。

**预期结果**：你能在源码注释里写出形如 `AscCPUKernelLaunch(numBlocks, nullptr, stream, "add_custom", add_custom, xDevice, yDevice, zDevice);` 的等价调用，并通过 `nm`/`ldd` 的输出确认"转义后的调用确实落到了 cpudebug 仿真库上"。整条链路：**`<<<>>>` →（编译器 lowering）→ `AscCPUKernelLaunch` → `RunKernelFunctionOnCpu` →（cpudebug 仿真库）**。

---

## 6. 本讲小结

- **CPU Debug 的核心思想是孪生调试**：用 CPU 构造 NPU 行为的孪生体，让同一份 Ascend C 源码在 CPU 上变成可被 gdb 调试的普通可执行程序，从而把问题前移。
- **`cpu_debug_launch.h` 是入口头文件**：它通过 `tikicpulib.h` 一次性引入整套仿真基础（fp 类型、stub、执行框架），并定义启动函数 `AscCPUKernelLaunch`。
- **`AscCPUKernelLaunch` 只分发不执行**：它先按函数名设置 kernel 模式，再把核函数交给 `RunKernelFunctionOnCpu` 真正运行。
- **`<<<>>>` 的转义是 CPU Debug 的关键机制**：bisheng 编译器在 CPU 模式下把它 lowering 成对 `AscCPUKernelLaunch` 的普通 C++ 调用，在 NPU 模式下 lowering 成真实 NPU 启动。
- **两个域互不影响**：源码里 CPU 专用代码被 `#ifdef ASCENDC_CPU_DEBUG` 包裹，NPU 模式下该宏未定义、块被剔除，所以同一份源码不改一行就能在两个域运行。

## 7. 下一步学习建议

本讲只走到了 `AscCPUKernelLaunch → RunKernelFunctionOnCpu` 的"门口"，并没有进入 `RunKernelFunctionOnCpu` 内部。接下来的学习建议：

1. **u2-l2 Ascend C 算子源码与 `.asc` 核函数结构**：以 `add.asc` 为例，拆解 `KernelAdd` 类、CopyIn/Compute/CopyOut 三段式、`TQue`/`LocalTensor`/`GlobalTensor`，看懂核函数**内部**是怎么写的。
2. **u2-l3 使用 GDB 调试 CPU 域算子**：动手用 `set follow-fork-mode child` 跟踪子进程，在 `Compute` 里断点看内存，把本讲的"概念链路"变成"可操作的调试体验"。
3. **u3-l1 多核 fork 执行模型**（进阶）：深入 `kern_fwk.h` 的 `RunKernelFunctionOnCpu`，看它如何用 `fork` 为每个 block 创建子进程、用信号处理与 `waitpid` 管理生命周期——也就是本讲反复"托付"过去的那个执行框架的真面目。
