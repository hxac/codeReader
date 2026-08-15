# HOSTCPU 常量折叠框架：AiCPU 算子的 host 侧执行基础设施

## 1. 本讲目标

本讲讲解本版本（提交 `193fa7cd`，2026-08-15 合入）新增的 HOSTCPU 常量折叠框架基础设施。学完后你应该能够：

1. 说清楚什么是常量折叠（constant folding），以及为什么要把 AiCPU 算子放到 host 侧执行。
2. 逐行读懂 `OPS_CV_REGISTER_CPU_KERNELV2` 注册宏在 host 构建与 device 构建下的两次展开，理解 weak 符号 `RegistCpuKernelV2` 的作用。
3. 说出 `cmake/func.cmake` 中 `HOSTCPU` 参数生成 `<算子名>_host_const_obj` 目标需要满足的全部条件。
4. 跟踪从算子源码 → x86 OBJECT → `libopconstant_folding_cv.so` → 安装目录 `opp/built-in/op_impl/host_cpu` 的完整构建链路。
5. 判断当前仓库中是否已有算子实际接入了这套框架（ spoiler：还没有，这是"地基"而非"大厦"）。

本讲是专家层内容，承接 u8-l1（AiCPU 算子开发）与 u1-l3（编译体系走读）。

## 2. 前置知识

阅读本讲前，请先回顾以下概念（在前置讲义中均已讲过，这里只做一句话唤醒）：

- **AiCPU 算子**：跑在昇腾设备上 AI CPU（一个通用 ARM 核）里的算子，适合控制流密集、不适合向量并行的逻辑（如 NMS）。工程特征是源码放在 `op_kernel_aicpu/` 目录，注册用 `REGISTER_CPU_KERNEL` 宏（见 u8-l1 的 `add_example_aicpu`）。
- **Host 侧 / Device 侧**：Host 指服务器上的 x86 CPU 进程（图引擎 GE、算子编译、aclnn 第一段都在这里）；Device 指 NPU 卡。AiCPU 属于 Device 侧的"CPU"，但它的指令集是 aarch64，不是 x86。
- **built-in 包 / 自定义包**：仓库以 built-in 整包方式出包时，安装前缀是 `opp/built-in/...`；以 `ENABLE_CUSTOM` 开启自定义包时，安装到 `packages/vendors/<厂商>/...`（见 u1-l3、u3-l5）。
- **`BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG`**：CMake 变量，表示"基于已安装的 CANN 包编译"（build.sh 走 `--pkg` 时的默认路径），区别于 CANN 源码仓联合构建。
- **weak 符号**：GNU 扩展。声明为 `__attribute__((weak))` 的函数，如果最终链接时没人提供强定义，对其取地址会得到 `nullptr`，调用则崩溃；有人提供则正常绑定。C/C++ 标准库的 `pthread` 钩子就常用这个技巧做"可选依赖"。
- **常量折叠（constant folding）**：编译原理里的经典优化——如果表达式的所有输入在"编译/构图期"就已知（常量），那么直接算出结果替换掉该表达式，运行期就不用算了。在图执行引擎里，它把"整图下到设备跑一遍才能得到的常量结果"提前到 host 侧构图/优化阶段算掉，省掉一次设备往返。

一个直觉类比：整张计算图里有一个 `NMS(boxes, scores)`，而 `boxes`、`scores` 都是构图期已知的常量张量。没有常量折叠时，这两个张量也要拷到设备、启动 AiCPU 核、再把结果拷回来；有了 host 侧常量折叠，GE 直接在 x86 进程里调用同一份 C++ 算子逻辑把结果算出来，图里该节点被折叠成一个常量。要做到这一点，前提是**同一份 AiCPU 算子源码能被编译成 x86 目标代码**——这正是本讲框架做的事。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [common/inc/aicpu/cv_aicpu_register.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h) | 本次新增的头文件。声明 weak 符号 `RegistCpuKernelV2`，提供 `OPS_CV_REGISTER_CPU_KERNELV2` 双路径注册宏 |
| [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake) | 构建函数库。`add_all_modules_sources` 新增 `HOSTCPU` 参数；`add_aicpu_host_kernel_modules` 定义 `OPS_CV_AICPU_HOST_KERNEL` 编译宏并登记目标 |
| [cmake/variables.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake) | 全局变量定义。`AICPU_INCLUDE` 本次补上了 `common/inc`，使算子源码能 include 新头文件；`AICPU_HOST_KERNEL_IMPL` 定义 host 侧 so 的安装目录 |
| [cmake/symbol.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake) | 链接收口。`gen_aicpu_const_symbol` 把所有 host OBJECT 链成 `libopconstant_folding_cv.so` 并安装（本讲作为链路终点引用） |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/CMakeLists.txt) | 根工程文件。`BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG`、`ENABLE_CUSTOM`、`DISABLE_AICPU` 三个开关是 HOSTCPU 生效条件的输入 |
| [examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu_aicpu.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu_aicpu.cpp) | 对照样本：现有 AiCPU 算子仍用旧宏 `REGISTER_CPU_KERNEL` 注册，尚未接入新框架 |

## 4. 核心概念与源码讲解

### 4.1 常量折叠与 host 侧执行：问题定义

#### 4.1.1 概念说明

回顾 u8-l1：AiCPU 算子源码（`op_kernel_aicpu/*_aicpu.cpp`）最终被交叉编译为 aarch64 代码，链成 `aicpu_kernels.so` 放进算子包，在设备的 AI CPU 上执行。这对"运行期才算"的输入没问题，但对构图期就确定的常量输入是浪费：

- 常量数据要经历 H2D 拷贝 → 设备侧调度 → AiCPU 计算 → D2H 拷回；
- 整图执行时，这类"可提前算掉"的节点拖慢了图优化和首次执行。

常量折叠的解法是：**把同一份算子源码再编译一份 x86 版本，让 GE 在 host 进程里直接调用**。难点在于同一份源码有两条生命周期：

1. **device 构建**：交叉编译到 aarch64，注册进设备的 AiCPU kernel 表（老路径，不能动）；
2. **host 构建**：用 x86 编译器编成 OBJECT，链接进 `libopconstant_folding_cv.so`，由常量折叠框架在运行期加载、查表调用。

两条构建路径要求同一份 `.cpp` 里的**注册代码走不同的注册入口**，而且不能要求算子作者写两份源码。这就是 `OPS_CV_REGISTER_CPU_KERNELV2` 宏要解决的问题。

#### 4.1.2 核心流程

```text
整包编译（built-in、--pkg 模式）时，一个 AiCPU 算子的源码会被编译两次：

  op_kernel_aicpu/xxx_aicpu.cpp
        │
        ├──[device 路径] 交叉编译(aarch64) → aicpu_kernels.so → 设备 AI CPU 执行
        │     注册方式：REGISTER_CPU_KERNEL（原有路径，不变）
        │
        └──[host 路径] HOSTCPU TRUE 的算子 → x86 OBJECT(<算子名>_host_const_obj)
              → 汇入 AICPU_HOST_OBJ_TARGETS
              → 链接成 libopconstant_folding_cv.so
              → 安装到 opp/built-in/op_impl/host_cpu
              → GE 常量折叠框架在 host 进程内加载并调用
                    注册方式：OPS_CV_REGISTER_CPU_KERNELV2
                             （定义了 OPS_CV_AICPU_HOST_KERNEL 宏时走 RegistCpuKernelV2）
```

本讲三个最小模块分别对应这条链路的三个环节：注册宏（4.2）、编译开关（4.3）、链接产出（4.4）。

#### 4.1.3 源码精读

先看提交的全貌。`git show 193fa7cd --stat` 显示这次只改了 3 个文件、净增 47 行：

- 新增 `common/inc/aicpu/cv_aicpu_register.h`（31 行）；
- [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L623-L632) 增加 HOSTCPU 参数与 host 常量折叠收集块（+15 行，其中 1 行是给已有的 `add_aicpu_host_kernel_modules` 补 `OPS_CV_AICPU_HOST_KERNEL` 宏）；
- [cmake/variables.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake#L212-L221) 的 `AICPU_INCLUDE` 补充 `common/inc` 路径（+1 行）。

注意一个容易误判的点：`add_aicpu_host_kernel_modules` 函数与 `cmake/symbol.cmake` 里的 `gen_aicpu_const_symbol` 链接函数**在本次提交之前就已存在**（diff 上下文可见），本次提交做的是"接上最后一根线"——补注册宏、补编译宏、补头文件搜索路径，让算子源码真正可以无感地双路编译。

#### 4.1.4 代码实践

**实践目标**：用 git 命令亲眼确认"哪些是本次新增、哪些是原有地基"。

**操作步骤**：

1. 在仓库根目录执行 `git show 193fa7cd --stat`，核对变更文件清单与上面三处一致。
2. 执行 `git show 193fa7cd -- cmake/func.cmake`，观察 `add_aicpu_host_kernel_modules` 的 diff 是"函数已存在、只加了一行 `OPS_CV_AICPU_HOST_KERNEL`"。
3. 执行 `git log --oneline -3 -- cmake/symbol.cmake`，确认 `gen_aicpu_const_symbol` 的引入早于本次提交。

**需要观察的现象**：diff 中 `+` 行很少但位置关键——一次"基础设施合入"往往长这样，而不是推倒重来。

**预期结果**：能明确说出"本提交新增了注册宏与开关，复用了既有的 host OBJECT 收集与链接函数"。若 `git show` 输出与上述不符（例如仓库状态不同），以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：常量折叠对图执行性能的收益来自哪里？是否对所有算子都有收益？

**参考答案**：收益来自（1）省掉常量输入的 H2D/D2H 拷贝；（2）省掉一次设备侧任务调度与 AiCPU 核启动；（3）把图中的可折叠节点替换为常量后，后续图优化（如融合）可以在更小的图上进行。只对"输入在构图期已知"的子图有收益；输入依赖运行期数据的算子无法折叠。

**练习 2**：为什么不能直接用设备上编好的 `aicpu_kernels.so` 做 host 侧常量折叠？

**参考答案**：它是 aarch64 目标代码，host 进程是 x86（或不同 ABI 环境），指令集不兼容，无法加载执行；所以必须用 x86 编译器把同一份源码再编一份。

---

### 4.2 注册宏 `OPS_CV_REGISTER_CPU_KERNELV2`：一份源码，两条注册路径

#### 4.2.1 概念说明

[cv_aicpu_register.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h) 是本次提交唯一的新增源码文件，只有 31 行，但每一行都有讲究。它解决的问题是：算子源码里必须有一句"把算子类注册到运行时"的代码，而 host 构建与 device 构建的注册入口不同。做法是**用编译宏分流**：

- 定义了 `OPS_CV_AICPU_HOST_KERNEL`（host 构建）→ 走新入口 `RegistCpuKernelV2`；
- 未定义（device 构建）→ 原样回退到 CANN 头文件 `cpu_kernel.h` 提供的标准宏 `REGISTER_CPU_KERNEL`。

#### 4.2.2 核心流程

```text
算子源码中写：OPS_CV_REGISTER_CPU_KERNELV2(MyOp, MyOpCpuKernel);
                    │
                    ├─ host 构建（目标 <算子名>_host_const_obj，
                    │            编译时带 -DOPS_CV_AICPU_HOST_KERNEL）：
                    │   1. 生成工厂函数 Creator_MyOp_Kernel() → MakeShared<MyOpCpuKernel>()
                    │   2. 生成静态变量 g_MyOp_Kernel_Creator，其初始化表达式在
                    │      main 之前（so 加载时）执行注册：
                    │      - 取 &RegistCpuKernelV2 判断 weak 符号是否被链接期解析
                    │        （即常量折叠宿主是否提供了强定义）
                    │      - 解析了 → RegistCpuKernelV2(type, creator)   注册到 V2 表
                    │      - 没解析 → RegistCpuKernel(type, creator)     回退老表
                    │
                    └─ device 构建（无该宏）：
                        宏直接展开为 REGISTER_CPU_KERNEL(MyOp, MyOpCpuKernel)
                        —— 与改动前完全一致，设备侧行为零变化
```

选型要点：为什么用 weak 符号而不是再加一个编译宏开关？因为算子 so 在**不同的宿主环境**里加载时，"宿主是否提供 V2 注册表"是**链接/加载期**才知道的事实，编译期宏只能区分"host 构建 / device 构建"，区分不了"host 构建出的 so 被谁加载"。weak 符号把判断推迟到加载时刻，一份产物通吃。

#### 4.2.3 源码精读

weak 符号声明：

[common/inc/aicpu/cv_aicpu_register.h:L16-L18](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h#L16-L18) 在命名空间 `aicpu` 里声明了 weak 的 `RegistCpuKernelV2`，签名与标准注册函数一致：接收算子类型名字符串 `type` 和一个 `KERNEL_CREATOR_FUN` 工厂（`KERNEL_CREATOR_FUN`、`CpuKernel`、`MakeShared` 均来自 [第 14 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h#L14) include 的 CANN 头文件 `cpu_kernel.h`）。这里只有声明没有定义——强定义由常量折叠框架的宿主库在链接期提供。

宏的 host 分支（两次展开中最关键的一次）：

[common/inc/aicpu/cv_aicpu_register.h:L20-L26](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h#L20-L26) 定义了 `OPS_CV_REGISTER_CPU_KERNELV2(type, clazz)` 的 host 展开：

- 第 22 行生成静态工厂函数 `Creator_##type##_Kernel`，用 `MakeShared<clazz>()` 创建算子实例；
- 第 23-26 行生成静态 bool 变量 `g_##type##_Kernel_Creator`，利用 **C++ 静态变量的初始化在 so 加载时执行**这一性质完成自动注册。初始化表达式是个三目运算：
  - `((&::aicpu::RegistCpuKernelV2) != nullptr)`——对 weak 函数取地址，链接期没有强定义则为空；
  - 非空走 `RegistCpuKernelV2((type), Creator_##type##_Kernel)`；
  - 为空回退 `RegistCpuKernel`（标准注册入口）。

device 回退分支：

[common/inc/aicpu/cv_aicpu_register.h:L27-L29](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h#L27-L29) 在未定义 `OPS_CV_AICPU_HOST_KERNEL` 时，宏原样转发给 `REGISTER_CPU_KERNEL(type, clazz)`。对照现有算子的旧写法 [add_example_aicpu_aicpu.cpp:L111](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu_aicpu.cpp#L111)（`REGISTER_CPU_KERNEL(kAddExample, AddExampleCpuKernel);`）可见：device 路径下新宏与旧宏完全等价，这就是"算子作者只需把旧宏换成新宏，设备侧行为零变化"的保证。

对照：算子作者要做的全部改动，理论上就是把 `REGISTER_CPU_KERNEL(...)` 换成 `#include "aicpu/cv_aicpu_register.h"` + `OPS_CV_REGISTER_CPU_KERNELV2(...)`。

#### 4.2.4 代码实践

**实践目标**：手工把宏展开一遍，画出 host/device 两条注册路径的分支图。

**操作步骤**：

1. 读 [cv_aicpu_register.h:L20-L29](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h#L20-L29)，把 `OPS_CV_REGISTER_CPU_KERNELV2(kAddExample, AddExampleCpuKernel)` 在两种宏状态下分别手工展开成 C++ 代码（替换 `##` 拼接）。
2. 画出分支图：顶层分叉是"是否定义 `OPS_CV_AICPU_HOST_KERNEL`"；host 分支内再分叉一次"weak 符号是否解析到强定义"，共三个叶子（V2 注册 / 老表注册 / device 标准 `REGISTER_CPU_KERNEL`）。
3. 验证静态变量技巧：在任意 C++ 环境写一个最小示例（**示例代码**，非项目源码）：

```cpp
// 示例代码：验证“静态变量初始化先于 main”这一注册技巧
#include <cstdio>
static bool g_registered = (std::printf("registered before main\n"), true);
int main() { std::printf("in main\n"); return 0; }
```

编译运行 `g++ test.cpp && ./a.out`，观察第一行输出先于 `in main`。这正是 `g_##type##_Kernel_Creator` 能在 so 加载阶段自动注册的原理。

**需要观察的现象**：示例程序先打印 `registered before main`；展开后的宏代码里，host 分支比 device 分支多了"取地址判空"这一层。

**预期结果**：得到一张三分支图；能口头解释"为什么判空发生在运行期而非编译期"（weak 符号是否解析由最终链接进 so 的宿主库决定）。宏展开与 `cpu_kernel.h` 相关部分依赖 CANN 环境，**待本地验证**（可在配套 toolkit 的 `include` 下找到 `cpu_kernel.h` 对照 `KERNEL_CREATOR_FUN` 定义）。

#### 4.2.5 小练习与答案

**练习 1**：如果不加第 23 行的 `__attribute__((unused))`，会发生什么？

**参考答案**：`g_##type##_Kernel_Creator` 是一个从未被读写的静态变量，编译器可能产生 `-Wunused-variable` 告警（视编译选项可能升级为错误）。该属性显式告诉编译器"这个变量的价值在它的初始化副作用"，抑制告警。

**练习 2**：host 分支里既然已经确定是 host 构建了，为什么还要判空回退 `RegistCpuKernel`？

**参考答案**：host 构建只说明"这份代码编给 x86 常量折叠用"，但 so 装进什么环境、宿主是否真的提供了 `RegistCpuKernelV2` 的强定义，是加载期才确定的。判空保证在老宿主环境（只认老注册表）里产物仍然可用，多一层兼容保险。

**练习 3**：算子作者从 `REGISTER_CPU_KERNEL` 迁移到新宏后，device 构建的产物会变吗？

**参考答案**：不会。`OPS_CV_AICPU_HOST_KERNEL` 只在 host 常量折叠目标上定义（见 4.3），device 构建时新宏按 [第 28 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h#L28) 原样展开为 `REGISTER_CPU_KERNEL`，逐字符等价。

---

### 4.3 `cmake/func.cmake`：HOSTCPU 参数与 `_host_const_obj` 目标的生成条件

#### 4.3.1 概念说明

注册宏解决了"同一份源码两条注册路径"，接下来要解决"什么时候编 x86 版本"。答案在 [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake) 的 `add_all_modules_sources` 宏：它是仓库中 image/objdetect 类算子的标准编译入口（各算子 `op_host/CMakeLists.txt` 调用，见 u1-l3），本次给它加了第 7 个参数 `HOSTCPU`。

#### 4.3.2 核心流程

`HOSTCPU TRUE` 后，需要**同时满足 4 个条件**才会真正生成 `<算子名>_host_const_obj` 目标：

```text
add_all_modules_sources(... HOSTCPU TRUE)
        │
        ├─ 条件1: MODULE_HOSTCPU 为真          ← 算子显式开启（本次提交后全仓暂无算子开启）
        ├─ 条件2: BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG 为真 ← 基于 CANN 包编译（--pkg 路径）
        ├─ 条件3: NOT ENABLE_CUSTOM            ← 仅 built-in 包；自定义/vendor 包不编
        ├─ 条件4: GLOB 到 op_kernel_aicpu/*_aicpu.cpp 且 NOT DISABLE_AICPU ← 有源且未全局禁用 AiCPU
        │
        └─ 全部满足 → HOST_OBJ_NAME = ${OP_NAME}_host_const_obj
                      → add_aicpu_host_kernel_modules() 创建 OBJECT 库
                        （定义 OPS_CV_AICPU_HOST_KERNEL，登记进 AICPU_HOST_OBJ_TARGETS）
                      → target_sources 挂入 *_aicpu.cpp 源文件
```

与之对照，device 侧 AiCPU 收集块（[L615-L621](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L615-L621)）的条件恰好是 `NOT BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG`：源码仓联合构建时编 device 版，CANN 包构建时编 host 版，两条路径互斥，同一份源码不会在一个构建里被编两次。

#### 4.3.3 源码精读

宏签名与参数说明：

[cmake/func.cmake:L534-L546](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L534-L546) 更新了 `add_all_modules_sources` 的用法注释并注册了新的单值参数 `HOSTCPU`（[第 543 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L543) 把它加进 `oneValueArgs`）。[第 541 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L541) 的注释明确写了语义：「设置是否编译 host 侧常量折叠 OBJECT，布尔类型：TRUE，FALSE，仅在 built-in 包生效」。

host 常量折叠收集块（本次新增的核心 9 行）：

[cmake/func.cmake:L623-L632](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L623-L632) 是整个开关的落地处。第 625 行串联 4.3.2 列出的前三个条件；第 626 行 GLOB 收集 `op_kernel_aicpu/*_aicpu.cpp`（注意通配比 device 路径的 `*_aicpu*.cpp` 更严格，只匹配以 `_aicpu.cpp` 结尾的文件）；第 627 行同时要求未设置 `DISABLE_AICPU`（该全局开关在根 [CMakeLists.txt:L51](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/CMakeLists.txt#L51) 定义，默认 OFF）；满足后第 628-630 行以 `${OP_NAME}_host_const_obj` 为名建目标并挂源文件。

`add_aicpu_host_kernel_modules` 函数（host OBJECT 目标长什么样）：

[cmake/func.cmake:L258-L289](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L258-L289) 创建一个普通 OBJECT 库并配置编译环境。本次提交的关键一行是 [第 270 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L270)：给目标加上编译定义 `OPS_CV_AICPU_HOST_KERNEL`——正是 4.2 中让注册宏分流到 `RegistCpuKernelV2` 的那个宏。其余配置构成 host 编译环境：头文件搜索路径用 `${AICPU_INCLUDE}` 加 Eigen（[L262-L265](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L262-L265)），编译选项沿用 `AICPU_DEFINITIONS`（[L272-L276](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L272-L276)），并链接 Eigen3。与 device 版的 [add_aicpu_kernel_modules](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L205-L224) 相比，最大差异是不再指定交叉编译器，直接用宿主 x86 工具链。

目标登记：

[cmake/func.cmake:L283-L287](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L283-L287) 把目标名追加进 `AICPU_HOST_OBJ_TARGETS` 缓存变量（去重）。这是给 4.4 的链接函数留的"账本"：每个开启 HOSTCPU 的算子各建一个 OBJECT 目标，最后统一收账。

#### 4.3.4 代码实践

**实践目标**：确认"当前仓库还没有任何算子接入 HOSTCPU"，并演练接入一个算子需要改哪里。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "HOSTCPU TRUE" --include=CMakeLists.txt .`，验证返回为空（没有任何算子的 `add_all_modules_sources` 传了 `HOSTCPU TRUE`）。
2. 执行 `grep -rn "OPS_CV_REGISTER_CPU_KERNELV2" --include='*.cpp' --include='*.h' . | grep -v tutorial`，确认新宏除定义处外无任何调用点。
3. 对照 [examples/add_example_aicpu/op_host/CMakeLists.txt:L12](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example_aicpu/op_host/CMakeLists.txt#L12)：该算子用的是旧宏 `add_modules_sources`（连 `HOSTCPU` 参数都不支持），且其 aicpu 源走的是 [op_kernel_aicpu/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt) 中 `add_aicpu_cust_kernel_modules` 的自定义路径——它不会被 HOSTCPU 收集块命中。
4. 假设要把某个使用 `add_all_modules_sources` 的 AiCPU 算子接入：在其 `op_host/CMakeLists.txt` 的调用里加 `HOSTCPU TRUE`，并把 kernel 源里的 `REGISTER_CPU_KERNEL` 换成 `OPS_CV_REGISTER_CPU_KERNELV2`（只是推演，**不要真的修改源码**）。

**需要观察的现象**：两个 grep 均无结果；`add_example_aicpu` 的构建走的是"cust"（自定义）函数而非本次的 host 函数。

**预期结果**：得出结论——本提交只交付了"开关 + 注册宏"，算子接入是后续工作。这也解释了为什么 `gen_aicpu_const_symbol` 在 `AICPU_HOST_OBJ_TARGETS` 为空时会打印 "No builtin host aicpu targets found, skipping." 后直接返回。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `HOSTCPU` 只在 built-in 包（`NOT ENABLE_CUSTOM`）生效？

**参考答案**：常量折叠的宿主（GE 图引擎、`RegistCpuKernelV2` 注册表）随 CANN built-in 出包，host 侧 so 也安装到 `opp/built-in/op_impl/host_cpu` 这个内置路径；自定义 vendor 包面向用户最小化交付，没有对应的 host 常量折叠宿主环境，编了也没人加载。所以用 `NOT ENABLE_CUSTOM` 挡掉。

**练习 2**：如果某算子的 AiCPU 源文件名是 `foo_aicpu_impl.cpp`，`HOSTCPU TRUE` 后会被收集吗？

**参考答案**：不会。收集 GLOB 是 `op_kernel_aicpu/*_aicpu.cpp`，只匹配以 `_aicpu.cpp` 结尾的文件；`foo_aicpu_impl.cpp` 不匹配（device 路径的 `*_aicpu*.cpp` 倒是能匹配）。文件命名在这里是隐性契约。

**练习 3**：`BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG` 在 build.sh 常规 `--pkg` 编译下是什么值？

**参考答案**：ON。根 [CMakeLists.txt:L23-L29](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/CMakeLists.txt#L23-L29) 中，当本仓作为顶层工程独立编译（library 模式）时该 option 默认 ON；作为 CANN 源码仓子目录联合构建时默认 OFF。即：日常单仓 `--pkg` 出包满足条件 2，CANN 全源码树联合构建不满足（那时走 device 路径）。

---

### 4.4 从 OBJECT 到 `libopconstant_folding_cv.so`：variables.cmake 的路径支撑与链接收口

#### 4.4.1 概念说明

第三个最小模块是 `cmake/variables.cmake`。它看似只加了一行 include 路径，但承载了两件事：让算子源码能 `#include "aicpu/cv_aicpu_register.h"`；定义 host 侧产物的安装目录。产物的最终链接由 `cmake/symbol.cmake` 的 `gen_aicpu_const_symbol` 完成，本模块把整条链走到终点。

#### 4.4.2 核心流程

```text
AICPU_HOST_OBJ_TARGETS（多个 <算子名>_host_const_obj）
        │ gen_aicpu_const_symbol（CMake 配置期由 gen_norm_symbol 调用）
        ▼
${CMAKE_CXX_COMPILER} -shared ${ALL_OBJECTS}        ← 宿主 x86 编译器
  + libaicpu_context_host.a / libaicpu_nodedef_host.a / libhost_ascend_protobuf.a（--whole-archive）
  + -lgraph -lexe_graph -lregister -lc_sec -lpthread -ldl
        │
        ▼
libopconstant_folding_cv.so
        │ install
        ▼
opp/built-in/op_impl/host_cpu/   （AICPU_HOST_KERNEL_IMPL）
```

`--whole-archive` 在这里的作用与 u6-l3 ONNX 插件库相同：OBJECT 里的注册代码全是"没人引用的静态变量初始化"，链接器默认会裁掉，必须整档保留。

#### 4.4.3 源码精读

include 路径补充（本次提交在 variables.cmake 的唯一一行）：

[cmake/variables.cmake:L212-L221](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake#L212-L221) 定义 `AICPU_INCLUDE`，其中 [第 218 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake#L218) 的 `${OPS_CV_DIR}/common/inc` 是本次新增。4.3 中 `add_aicpu_host_kernel_modules`（以及 device/cust 两个 AiCPU 函数）都把这个变量灌进目标的 include 路径，补上之后，算子源码里 `#include "aicpu/cv_aicpu_register.h"` 才能按 `common/inc + aicpu/` 前缀解析到新头文件。

安装目录定义：

[cmake/variables.cmake:L97](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake#L97) 在 built-in 分支定义 `AICPU_HOST_KERNEL_IMPL = opp/built-in/op_impl/host_cpu`，即 host 常量折叠 so 的落盘位置；对比 [第 95 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake#L95) 的 `AICPU_KERNEL_IMPL`（device AiCPU 的安装位置）可见 host/device 两套产物在包内的目录划分。

链接收口：

[cmake/symbol.cmake:L430-L484](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L430-L484) 的 `gen_aicpu_const_symbol` 遍历 `AICPU_HOST_OBJ_TARGETS` 汇总所有 `$<TARGET_OBJECTS:...>`（[L438-L441](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L438-L441)）；为空则跳过（[L431-L434](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L431-L434)，即当前仓库的实际行为）。[L456-L476](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L456-L476) 用 `${CMAKE_CXX_COMPILER}`（宿主编译器，对比 device 版 [L404](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L404) 用的 `ARM_CXX_COMPILER`）链接出 `libopconstant_folding_cv.so`，`--whole-archive` 打入三个 host 版静态库（`libaicpu_context_host.a`、`libaicpu_nodedef_host.a`、`libhost_ascend_protobuf.a`），再链 graph/exe_graph/register 等宿主库；[L478-L483](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L478-L483) 安装到 `AICPU_HOST_KERNEL_IMPL`。该函数由 [gen_norm_symbol:L574](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L574) 在内置包链接阶段统一调度。

#### 4.4.4 代码实践

**实践目标**：在编译产物中找到（或确认缺失）`libopconstant_folding_cv.so`，打通"源码 → 产物"的心智验证。

**操作步骤**：

1. 在配套环境按 u1-l3 的方式整包编译：`./build.sh --pkg`（不指定 `--ops`，全部算子参与）。
2. 在构建目录与安装目录分别查找：`find build_out -name "libopconstant_folding_cv.so"` 与 `find build_out -path "*host_cpu*"`。
3. 同时查看 CMake 配置期日志中是否有 `"No builtin host aicpu targets found, skipping."`（来自 [symbol.cmake:L432](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L432)）或 `"add aicpu host kernel modules for ..."`（来自 [func.cmake:L259](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L259)）。

**需要观察的现象**：按 4.3.4 的结论（当前无算子开启 HOSTCPU），预期日志出现 skipping 信息、产物中**没有**该 so。若日后有算子接入，则应看到 host kernel modules 的 STATUS 日志和 `opp/built-in/op_impl/host_cpu/libopconstant_folding_cv.so` 产物。

**预期结果**：以本地实际输出为准（本实践需要 CANN 编译环境，**待本地验证**）。核心检验点：能说清"没有 so"是因为条件 1（`MODULE_HOSTCPU`）无人满足，而不是构建坏了。

#### 4.4.5 小练习与答案

**练习 1**：链接 `libopconstant_folding_cv.so` 时为什么必须 `--whole-archive`？

**参考答案**：算子 OBJECT 里的注册代码只以"静态变量初始化"的形式存在，没有任何外部强引用；不带 `--whole-archive` 时链接器会把未被引用的成员全部裁掉，注册代码不会进最终 so，常量折叠框架查不到任何算子。

**练习 2**：`variables.cmake` 那一行 `${OPS_CV_DIR}/common/inc` 如果不加，最直接的症状是什么？

**参考答案**：开启 HOSTCPU 的算子源码 `#include "aicpu/cv_aicpu_register.h"` 时会报"找不到头文件"的编译错误——`AICPU_INCLUDE` 是所有 AiCPU 目标（含 host 目标）的统一头文件搜索路径，新头文件必须放进这条路径的可达范围内。

**练习 3**：host so 链接的 `libaicpu_context_host.a` 等静态库与 device 版 `libaicpu_context.a` 是什么关系？

**参考答案**：同一套 AiCPU 运行时接口（context、nodedef、protobuf 序列化）分别面向 x86 host 与 aarch64 device 编译的两个版本，命名以 `_host` 后缀区分。这保证同一份算子源码在两边 include 的头文件、调用的 API 一致，仅目标架构不同——这正是"一份源码双路编译"能成立的配套前提。

---

## 5. 综合实践

**任务：为 `add_example_aicpu` 设计一份（纸面）HOSTCPU 接入方案。**

`add_example_aicpu` 目前完全没有走这套框架（见 4.3.4 的 grep 证据）。请基于本讲三个最小模块，写一份不落盘的接入分析报告，包含：

1. **构建侧**：该算子当前用旧宏 `add_modules_sources`（[op_host/CMakeLists.txt:L12](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example_aicpu/op_host/CMakeLists.txt#L12)），且其 aicpu 源码通过 [op_kernel_aicpu/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt) 的 `add_aicpu_cust_kernel_modules` 走自定义路径。分析：要命中 [func.cmake:L623-L632](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L623-L632) 的收集块，构建脚本需要迁移到 `add_all_modules_sources` 并加 `HOSTCPU TRUE`；同时检查其源文件名 `add_example_aicpu_aicpu.cpp` 是否满足 `*_aicpu.cpp` 通配（满足）。
2. **源码侧**：把 [add_example_aicpu_aicpu.cpp:L111](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu_aicpu.cpp#L111) 的 `REGISTER_CPU_KERNEL(kAddExample, AddExampleCpuKernel)` 替换为 `OPS_CV_REGISTER_CPU_KERNELV2` 需要额外 include 哪个头文件；写出替换后的两行代码。
3. **产物侧**：推演接入后在 `--pkg` 整包编译下应出现的新目标名（`add_example_aicpu_host_const_obj`）、新 so 及其安装路径（`opp/built-in/op_impl/host_cpu/libopconstant_folding_cv.so`）。
4. **验证**：写出验证清单——cmake 日志里找哪两条 STATUS、产物目录找哪个文件、`--soc` 与运行环境的一致性要求（回顾 u1-l4 的 161001 教训）。

完成后，可在本地环境中实际演练第 3、4 步（**待本地验证**）。注意：不要向仓库提交真实改动，除非你打算走 u8-l3 的社区贡献流程。

## 6. 本讲小结

- 常量折叠让"构图期已知输入"的 AiCPU 算子直接在 host（x86）进程内算掉，省去设备往返；前提是把同一份算子源码再编译一份 x86 版本。
- [cv_aicpu_register.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h) 的 `OPS_CV_REGISTER_CPU_KERNELV2` 用编译宏 `OPS_CV_AICPU_HOST_KERNEL` 分流：host 构建经 weak 符号 `RegistCpuKernelV2` 注册（运行期取地址判空、失败回退老注册表），device 构建原样展开为 `REGISTER_CPU_KERNEL`，设备侧行为零变化。
- [func.cmake:L623-L632](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L623-L632) 的收集块要求四个条件同时成立才生成 `<算子名>_host_const_obj`：算子显式传 `HOSTCPU TRUE`、基于 CANN 包编译、非自定义包、存在 `op_kernel_aicpu/*_aicpu.cpp` 且未禁用 AiCPU。
- [variables.cmake:L218](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake#L218) 给 `AICPU_INCLUDE` 补的 `common/inc` 是新头文件可达的关键一行；host 产物最终由 `gen_aicpu_const_symbol` 用宿主编译器 `--whole-archive` 链成 `libopconstant_folding_cv.so`，安装到 `opp/built-in/op_impl/host_cpu`。
- 本次提交（`193fa7cd`）是"地基"而非"大厦"：注册宏、编译开关、链接函数、安装路径全部就绪，但截至当前 HEAD 没有任何算子传 `HOSTCPU TRUE`，也没有源码调用新宏，`AICPU_HOST_OBJ_TARGETS` 为空时链接函数直接跳过。

## 7. 下一步学习建议

- **向前追溯使用方**：`RegistCpuKernelV2` 的强定义与 `libaicpu_context_host.a` 在 CANN toolkit（`lib64/` 与 `include/`）侧，可用 `nm -D` 与头文件检索确认宿主注册表的形态，理解常量折叠框架如何按算子名字符串查工厂函数。
- **关注后续接入提交**：用 `git log --oneline -- common/inc/aicpu/cv_aicpu_register.h` 和 `grep -rn "HOSTCPU TRUE"` 持续观察哪些算子（NMS 类是大概率首批）真正接入，届时可对照本讲的接入清单验证推演。
- **回到编译主线**：若对 `BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG`、`ENABLE_CUSTOM` 与 build.sh 的关系仍有模糊，复习 u1-l3；对 AiCPU 算子本体的开发模式，复习 u8-l1。
- **横向对比同类机制**：u6-l3 的 ONNX 插件库同样依赖"静态注册 + `--whole-archive`"，两相对照可以提炼出 CANN 生态里"自动注册型 so"的通用构建范式。
