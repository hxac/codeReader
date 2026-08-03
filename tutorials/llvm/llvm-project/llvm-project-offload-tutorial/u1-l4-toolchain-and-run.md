# 工具链、编译运行与设备信息

## 1. 本讲目标

本讲解决一个非常具体的问题：**拿到一份 offload 子项目后，我该怎么把一个 OpenMP 目标卸载程序编译出来、跑起来，并看到运行时内部到底在干什么？**

读完本讲你应当能够：

- 用 `clang`/`flang` 配合 `-fopenmp -fopenmp-targets=<三元组>` 编译并运行一个最小的 `target` 卸载程序，知道「主机三元组」与「设备三元组」的区别。
- 使用 `llvm-offload-device-info` 工具枚举本机可见的设备及其属性，并理解它走的是 **liboffload 新 API**。
- 理解 `include/Shared` 提供的通用环境变量处理框架 `Envar`，以及它和「直接 `getenv`」的关系。
- 区分三套不同的运行时观测手段：`LIBOMPTARGET_INFO`（信息位掩码）、`LIBOMPTARGET_DEBUG`（详细调试，需编译期开启）、`OFFLOAD_TRACE`（仅 liboffload 的 API 追踪）。

本讲是**入门层**的实操课，承接 u1-l2（构建系统）与 u1-l3（目录地图）：你已经知道怎么构建 offload、知道它分哪几层，本讲带你完成「编译 → 运行 → 观测」的最后一公里。

## 2. 前置知识

在动手前，请先建立两个心智模型（u1-l1 已建立，这里复习关键点）：

1. **主机 / 设备 与 target 卸载**。OpenMP 的 `target` 指令把一段代码放到「设备」上执行；`map(to:/from:/tofrom:/alloc:)` 子句控制主机和设备之间的数据搬运。编译器（Clang）会在编译期为设备生成一份**设备镜像（device image）**，并在主机代码里插入对运行时入口 `__tgt_*` 的调用。运行时不负责「翻译」代码，只负责「装在哪台设备、把数据搬过去、启动内核」。

2. **两套并列的上层**。offload 子项目里有两条并行的上层路径（见 u1-l3）：
   - **libomptarget**：成熟的、绑定 OpenMP 的运行时，处理编译器生成的 `__tgt_*` 调用。
   - **liboffload**：开发中的、不绑定 OpenMP 的统一 API（`ol*` 函数），供其它语言运行时复用。

   本讲的「环境变量」其实分属这两条路径：`LIBOMPTARGET_*` 属于 libomptarget，`OFFLOAD_TRACE` 属于 liboffload。**不要把它们混为一谈**——这是初学者最容易踩的坑。

还需要一点 Shell 基础：能用 `export VAR=value` 或 `VAR=value cmd` 的方式临时设置环境变量。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `README.txt` | 说明 libomptarget 支持的主机与设备架构范围。 |
| `tools/deviceinfo/llvm-offload-device-info.cpp` | 设备信息工具：基于 liboffload API 枚举所有设备并打印属性。 |
| `tools/deviceinfo/CMakeLists.txt` | 声明该工具链接 `LLVMOffload` 库。 |
| `include/Shared/EnvironmentVar.h` | 通用环境变量处理模板 `Envar<Ty>`（int/float/string/bool）。 |
| `include/Shared/Debug.h` | `LIBOMPTARGET_INFO` / `LIBOMPTARGET_DEBUG` 的实现（位掩码、`ODBG`/`INFO` 宏）。 |
| `liboffload/src/OffloadImpl.cpp`（节选） | `OFFLOAD_TRACE` 在 liboffload 初始化时被读取的位置。 |
| `CMakeLists.txt`（顶层，节选） | `LIBOMPTARGET_ENABLE_DEBUG` 选项如何决定编译期 `OMPTARGET_DEBUG` 宏。 |
| `plugins-nextgen/host/CMakeLists.txt` | host 插件声明的「系统目标三元组」（决定 `-fopenmp-targets` 写什么）。 |
| `test/env/omp_target_debug.c`、`test/offloading/info.c` | 官方测试，演示如何用 `LIBOMPTARGET_DEBUG` / `LIBOMPTARGET_INFO` 验证输出。 |

## 4. 核心概念与源码讲解

### 4.1 工具链与编译运行入门

#### 4.1.1 概念说明

要把 OpenMP 代码卸载到设备上，编译器需要同时产出**两份**机器码：

- 主机代码：正常运行的部分，外加对运行时入口 `__tgt_*` 的调用。
- 设备镜像：`target` 区域对应的设备端代码，被打包进一个「offload binary」嵌在可执行文件里。

Clang/Flang 通过两个关键开关来做到这件事：

- `-fopenmp`：开启 OpenMP 支持（主机侧）。
- `-fopenmp-targets=<三元组>`：告诉编译器「为这个目标三元组额外生成一份设备镜像」。

「三元组（triple）」形如 `x86_64-unknown-linux-gnu`，描述目标架构、厂商、操作系统、ABI。你写的设备三元组必须和运行时实际加载的某个插件声明的三元组**匹配**，运行时才能找到正确的插件去执行这份镜像。

#### 4.1.2 核心流程

最小编译运行流程：

1. 确定本机主机架构（如 x86_64）。
2. 选择要卸载到的设备三元组：
   - 想卸载回**主机本身**（用 host 插件做学习/调试）：写主机三元组，如 `x86_64-unknown-linux-gnu`。
   - 想卸载到 GPU：写 GPU 三元组，如 `nvptx64-nvidia-cuda`（NVIDIA）或 `amdgcn-amd-amdhsa`（AMD）。
3. 编译：`clang -fopenmp -fopenmp-targets=<三元组> prog.c -o prog`。
4. 运行：`./prog`。运行时（libomptarget）会自动从嵌在可执行文件里的 offload binary 中挑出与设备匹配的镜像并加载执行。

host 插件特别适合学习：它把主机 CPU 当作「设备」，让你在没有 GPU 的机器上也能完整跑通整条卸载链路。

#### 4.1.3 源码精读

`README.txt` 明确列出了受支持的主机与设备架构：

[README.txt:L8-L23](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.txt#L8-L23) —— 这段说明主机仅在 Linux 上测试（Intel 64 / Power / AArch64），而设备架构除 CPU 外还包含 NVIDIA CUDA 与 AMD GPU。这也决定了你写的 `-fopenmp-targets` 三元组要落在这些受支持范围内。

host 插件声明的「系统目标三元组」在它自己的 CMakeLists 里：

[plugins-nextgen/host/CMakeLists.txt:L23-L26](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/host/CMakeLists.txt#L23-L26) —— 当 `CMAKE_SYSTEM_PROCESSOR` 为 `x86_64` 时，host 插件登记的目标三元组是 `x86_64-unknown-linux-gnu`。这就是你在 x86_64 机器上用 host 插件时，`-fopenmp-targets` 应当填写（或与之匹配）的值。其它架构（ppc64le/aarch64/s390x/riscv64/loongarch64）的同文件内也各自登记了对应三元组。

官方测试展示了一个完整且真实的用法。注意第一行 `RUN:` 里的编译方式：

[test/offloading/info.c:L1-L4](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/test/offloading/info.c#L1-L4) —— `%libomptarget-compile-generic` 是 lit 测试框架里的一个** substitution（占位替换）**，它最终会展开成「用本机构建的 clang，带上正确的 `-fopenmp -fopenmp-targets=<本机设备三元组>`」的命令。lit 用这种方式让同一份测试能在不同设备后端上复用。你手写命令时，就是把那个 substitution 替换成显式的 clang 调用。

#### 4.1.4 代码实践

**实践目标**：在 host 插件上编译并运行一个最小的 `target parallel for` 程序。

**操作步骤**：

1. 写一个示例程序（**示例代码**，非项目原有文件）`vec_add.c`：

   ```c
   #include <stdio.h>
   #define N 16
   int main(void) {
     int a[N], b[N], c[N];
     for (int i = 0; i < N; ++i) { a[i] = i; b[i] = i * 2; }
   #pragma omp target map(to : a, b) map(from : c)
   #pragma omp parallel for
     for (int i = 0; i < N; ++i) c[i] = a[i] + b[i];
     printf("c[0..3]=%d %d %d %d\n", c[0], c[1], c[2], c[3]);
     return 0;
   }
   ```

2. 编译（假设本机为 x86_64、用 host 插件）：

   ```bash
   clang -fopenmp -fopenmp-targets=x86_64-unknown-linux-gnu vec_add.c -o vec_add
   ```

   其中 `-fopenmp-targets=` 的值要与你构建的 host 插件登记的三元组一致；GPU 场景则换成 `nvptx64-nvidia-cuda` 等。

3. 运行：`./vec_add`。

**需要观察的现象**：程序正常打印 `c[0..3]=0 3 6 9`，说明 target 区域确实在「设备」（host 插件下就是主机 CPU）上执行，且数据被正确搬运。

**预期结果 / 待本地验证**：本讲无法替你运行——上述命令要求本机已构建好 LLVM 工具链（`clang`）与 offload 运行时（含 host 插件），且运行时库目录在动态链接路径上。**待本地验证**：请在你本地构建产物上执行并记录实际输出。若运行时报「无法找到 libomptarget.so」之类，多半是库输出目录（`LIBOMPTARGET_LIBRARY_DIR`，见 u1-l2）未加入 `LD_LIBRARY_PATH`。

#### 4.1.5 小练习与答案

**练习 1**：为什么用 host 插件学习时，`-fopenmp-targets` 写的是主机三元组而不是 `nvptx64-nvidia-cuda`？

> **参考答案**：因为 host 插件把主机 CPU 当作「设备」，它登记的三元组就是主机三元组（如 `x86_64-unknown-linux-gnu`）。运行时按「设备镜像三元组 ↔ 插件登记三元组」匹配来选择插件；写错三元组会导致运行时找不到能加载该镜像的插件。

**练习 2**：`-fopenmp` 和 `-fopenmp-targets` 各自负责什么？

> **参考答案**：`-fopenmp` 开启 OpenMP 主机侧支持；`-fopenmp-targets=<三元组>` 让编译器额外为指定目标生成一份设备镜像，并插入对运行时 `__tgt_*` 入口的调用。

---

### 4.2 llvm-offload-device-info 设备信息工具

#### 4.2.1 概念说明

`llvm-offload-device-info` 是一个命令行小工具：运行后，它会**枚举本机所有可见的卸载设备**，并把每个平台/设备的属性（名称、后端、计算单元数、显存大小、浮点能力等）打印出来。

理解它的关键在于：**它走的是 liboffload 新 API，不是旧的 `__tgt_*`**。也就是说，这个工具是 liboffload（见 u1-l3 / u3-l11）的一个真实「客户端」示例——它让我们能直观看到「liboffload 之上看到的设备世界」长什么样。

#### 4.2.2 核心流程

工具的执行非常线性：

1. 调 `olInit(nullptr)` 初始化 liboffload（这一步会拉起所有可用插件并初始化设备）。
2. 调 `olIterateDevices(...)` 用回调收集所有设备句柄。
3. 对每个设备：先取它所属的平台（`OL_DEVICE_INFO_PLATFORM`），打印平台属性（名称、厂商、后端）；再打印设备属性（名称、类型、显存、计算单元……）。
4. 调 `olShutDown()` 收尾。
5. 任意一步返回错误码就立即向上传播，`main` 里把错误打印到 `stderr` 并以非零状态退出。

#### 4.2.3 源码精读

文件开头的注释直接点明了工具的定位——「by using the new liboffload API」：

[tools/deviceinfo/llvm-offload-device-info.cpp:L8-L12](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/llvm-offload-device-info.cpp#L8-L12) —— 它是「使用新 liboffload API 打印所有设备与属性」的命令行工具。

它只 `#include <OffloadAPI.h>`（liboffload 的公共头）并用 `ol*` 函数，对应的 CMakeLists 里链接的就是 `LLVMOffload`：

[tools/deviceinfo/CMakeLists.txt:L7-L9](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/CMakeLists.txt#L7-L9) —— 该工具的可执行文件只依赖 `LLVMOffload` 这一个库，体现了「它是 liboffload 的一个薄客户端」。

「后端（backend）」枚举的打印逻辑展示了 liboffload 抽象出的几类后端：

[tools/deviceinfo/llvm-offload-device-info.cpp:L33-L56](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/llvm-offload-device-info.cpp#L33-L56) —— 把 `ol_platform_backend_t` 转成可读字符串：UNKNOWN / CUDA / AMDGPU / LEVEL_ZERO / HOST。这正好对应 offload 支持的几类插件后端。

整个程序的入口 `printRoot` 完成了「初始化 → 枚举 → 逐个打印 → 关闭」的全过程：

[tools/deviceinfo/llvm-offload-device-info.cpp:L259-L281](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/llvm-offload-device-info.cpp#L259-L281) —— 注意三处：`olInit(nullptr)` 初始化；`olIterateDevices` 用一个 lambda 回调把每个 `ol_device_handle_t` 收进 vector（回调返回 `true` 表示「继续遍历」）；最后 `olShutDown()`。

每个设备的属性通过模板函数 `printDeviceValue` 统一读取与打印：

[tools/deviceinfo/llvm-offload-device-info.cpp:L132-L154](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/llvm-offload-device-info.cpp#L132-L154) —— 它根据类型是否为指针，分别走「先查 size 再取值」或「直接取定长值」两条路径，调用 `olGetDeviceInfoSize` / `olGetDeviceInfo`。这是 liboffload「两段式查询（先问长度，再取内容）」API 风格的典型写法。

错误处理用了一个极简的宏：一旦某步返回非空错误就立刻 `return`：

[tools/deviceinfo/llvm-offload-device-info.cpp:L18-L21](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/llvm-offload-device-info.cpp#L18-L21) —— `OFFLOAD_ERR(X)` 把 liboffload 的错误对象 `ol_result_t` 向上冒泡；`main` 里最终把它打印并返回 1。

#### 4.2.4 代码实践

**实践目标**：运行设备信息工具并读懂它的输出（**待本地验证**）。

**操作步骤**：

1. 确认你已构建出 `llvm-offload-device-info`（构建 offload 时会随工具一起产出）。
2. 运行：`./llvm-offload-device-info`。

**需要观察的现象**：输出顶部有 `Liboffload Version: x.y.z` 和 `Num Devices: N`；之后每个设备用 `[产品名]` 起头一块，列出 `Platform Backend`（如 HOST/CUDA/...）、`Type`、`Num Compute Units`、`Global Mem Size` 等。

**预期结果 / 待本地验证**：在只构建了 host 插件的环境下，应至少看到一台 `Type: CPU` 或 `HOST` 后端的设备，`Num Devices` 与可用插件数相关。**待本地验证**：请记录你本机的实际输出，并对照 4.2.3 的源码理解每个字段由哪个 `OL_DEVICE_INFO_*` 查询得来。

> 提示：liboffload 官方文档（`liboffload/README.md`）写明「host 插件目前不被支持（not currently supported）」。因此在你本机，这个基于 liboffload 的工具在纯 host 环境下能否列出设备，取决于当前代码的实际完成度——这正是「liboffload 仍在开发中」的体现。若工具返回 0 设备或报错，请结合 u1-l1 提到的「liboffload 不完整」来理解，而不是认为构建出错。

#### 4.2.5 小练习与答案

**练习 1**：这个工具用的是 `__tgt_*` 还是 `ol*` API？依据是什么？

> **参考答案**：用的是 `ol*`（liboffload 新 API）。依据是源码 `#include <OffloadAPI.h>`、全程调用 `olInit`/`olIterateDevices`/`olGetDeviceInfo`/`olShutDown`，且 CMakeLists 只链接 `LLVMOffload`。

**练习 2**：`olIterateDevices` 的回调返回 `true` 是什么含义？

> **参考答案**：返回 `true` 表示「继续遍历下一个设备」；返回 `false` 会提前终止遍历。本工具始终返回 `true` 以收集全部设备。

---

### 4.3 通用环境变量处理框架 Envar

#### 4.3.1 概念说明

offload 运行时和插件有大量可调参数（栈大小、堆大小、队列模式、record/replay 开关等），它们大多通过**环境变量**来配置。如果每个地方都手写 `getenv` + 字符串解析 + 类型转换 + 默认值回退，代码会很啰嗦且容易出错。

`include/Shared/EnvironmentVar.h` 提供了一个模板类 `Envar<Ty>`：把「读取环境变量 → 解析成指定类型 → 失败回退默认值」这套逻辑封装成一行声明。它支持 `int`、`int32_t`、`int64_t`、`uint32_t`、`uint64_t`、`std::string`、`bool` 等类型。

需要澄清一点：`LIBOMPTARGET_INFO` / `LIBOMPTARGET_DEBUG` 这两个最知名的变量**并不**走 `Envar`，而是在 `Debug.h` 里直接 `getenv` 读取（见 4.4）。`Envar` 是给「其它配置型环境变量」用的通用框架。理解这种区分，能避免你在源码里找错地方。

#### 4.3.2 核心流程

`Envar<Ty>` 的构造流程：

1. 记录变量名 `Name` 和默认值 `Default`，置 `Initialized=true`。
2. 用 `getenv(Name)` 取环境字符串。
3. 若存在，用 `StringParser::parse<Ty>` 把字符串解析成 `Ty`：
   - 解析成功 → `IsPresent=true`，`Data` 为解析值。
   - 解析失败（值非法）→ 打印一条 debug 日志说明「忽略非法值」，`Data` 回退为 `Default`。
4. 若环境变量不存在 → `Data` 保持为 `Default`。

之后通过 `get()` 取最终值，或用 `isPresent()` 判断用户是否显式设置过。`bool` 类型的解析特别宽松：`true/yes/on/1` 与 `false/no/off/0` 都被接受（大小写不敏感）。

#### 4.3.3 源码精读

`Envar` 是一个模板类，持有名字、数据、是否出现、是否初始化四个字段：

[include/Shared/EnvironmentVar.h:L32-L39](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/EnvironmentVar.h#L32-L39) —— 它的成员 `Name/Data/IsPresent/Initialized` 构成了「带默认值的环境变量」的最小状态。

最常用的构造函数读取环境变量并在解析失败时回退：

[include/Shared/EnvironmentVar.h:L58-L71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/EnvironmentVar.h#L58-L71) —— 注意 `getenv(Name.data())` 取值后，由 `StringParser::parse<Ty>` 决定是否合法；非法时用 `ODBG(OLDT_Init)` 记一条日志并把数据重置为默认值。`ODBG`/`OLDT_Init` 来自 `Debug.h`（4.4 节会讲），是运行时的调试日志机制。

`bool` 的解析规则（大小写不敏感、支持多种写法）：

[include/Shared/EnvironmentVar.h:L137-L155](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/EnvironmentVar.h#L137-L155) —— 这是为什么很多开关变量（如 record/replay、队列跟踪）都能接受 `on/off`、`yes/no`、`1/0`。

文件末尾为常用类型定义了别名，让使用处更简洁：

[include/Shared/EnvironmentVar.h:L128-L135](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/EnvironmentVar.h#L128-L135) —— `BoolEnvar`、`StringEnvar`、`UInt64Envar` 等就是 `Envar<bool>`、`Envar<std::string>`、`Envar<uint64_t>` 的别名。

这套框架在真实代码里被广泛使用，例如 Level Zero 插件读取编译选项、内存池、命令模式等：

[plugins-nextgen/level_zero/src/L0Options.cpp:L24-L27](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/level_zero/src/L0Options.cpp#L24-L27) —— `StringEnvar("...", "").get()` 与 `BoolEnvar("...", true)` 是典型用法：一行声明即拿到「环境变量值或默认值」。运行时核心也用它，例如 libomptarget 用 `BoolEnvar` 控制 record/replay：`libomptarget/device.cpp:90` 处 `BoolEnvar OMPX_RecordKernel("LIBOMPTARGET_RECORD", false)`。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：体会 `Envar` 如何把「读环境变量」收敛成一行声明。

**操作步骤**：

1. 在仓库内搜索 `Envar` 的实际使用点（例如 `BoolEnvar`、`StringEnvar`、`UInt64Envar`）。
2. 选 2～3 处，记录：变量名、类型、默认值、它控制的运行时行为。
3. 对照 4.3.3 的构造函数，推断：如果用户把一个 `BoolEnvar` 设成 `"maybe"`（既非 true 系也非 false 系），运行时会怎样？

**需要观察的现象**：你会发现大量插件/运行时行为都暴露成了环境变量，且默认值在源码里一目了然。

**预期结果**：对 `BoolEnvar("X", false)`，若设成非法值 `"maybe"`，`parse<bool>` 返回 `false`（解析失败），`Data` 回退为默认值 `false`，并打印一条 `OLDT_Init` 日志。这就是 4.3.3 构造函数里 `if (!IsPresent)` 分支的效果。**结论**：非法值不会让程序崩溃，而是静默回退——这是 `Envar` 的容错设计。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LIBOMPTARGET_INFO` 不需要用 `Envar`？

> **参考答案**：它是一个**位掩码**（一组按位或的标志），且需要在运行期被原子地读取/修改（见 4.4 的 `std::atomic<uint32_t>`）。`Envar` 面向「一次性读取的配置值」，不提供原子与按位语义，所以 `Debug.h` 用了专门的 `getInfoLevelInternal()` 直接 `getenv` + `std::stoi`。

**练习 2**：`Envar<bool>` 接受哪些「真」值？

> **参考答案**：`true`、`yes`、`on`、`1`（大小写不敏感）；「假」值为 `false`、`no`、`off`、`0`。其余值视为非法并回退默认。

---

### 4.4 运行时调试、信息与追踪（LIBOMPTARGET_INFO / LIBOMPTARGET_DEBUG / OFFLOAD_TRACE）

#### 4.4.1 概念说明

这是本讲最重要的「防混淆」模块。offload 有**三套**观测手段，分属两条上层路径：

| 环境变量 | 属于 | 作用 | 是否默认可用 |
| --- | --- | --- | --- |
| `LIBOMPTARGET_INFO` | libomptarget | 按位掩码选择性打印「关键事件」（数据搬运、映射变化、内核启动等） | 是 |
| `LIBOMPTARGET_DEBUG` | libomptarget（及插件） | 打印**非常详细**的调试日志（带组件前缀与级别） | **否**，需编译期开启 |
| `OFFLOAD_TRACE` | liboffload | 追踪每一次 `ol*` API 调用 | 是（仅对 liboffload 程序） |

三者互不通用：`OFFLOAD_TRACE` 不会影响 libomptarget 程序的输出，`LIBOMPTARGET_INFO` 也不会影响 `llvm-offload-device-info`（后者是 liboffload 程序）。

一个关键事实：`LIBOMPTARGET_DEBUG` 的「详细调试」只有在运行时被编译进调试模式时才有效——对应编译期宏 `OMPTARGET_DEBUG`。默认 Release 构建里，这套日志被**完全编译消除**（宏展开为空），设环境变量不会有任何输出。

#### 4.4.2 核心流程

**`LIBOMPTARGET_INFO`（位掩码）**：

1. 初始化时（`std::call_once`）读一次 `LIBOMPTARGET_INFO`，用 `std::stoi` 转成整数存入一个 `std::atomic<uint32_t>`。
2. 运行时各处用 `INFO(flags, id, ...)` 宏输出：当 `(当前级别 & flags) != 0` 时打印。
3. `flags` 是一组按位或的 `OpenMPInfoType`：

\[ \text{mask} = b_{args}\,|\,b_{exists}\,|\,b_{table}\,|\,\dots \]

   常用值：`OMP_INFOTYPE_KERNEL_ARGS=0x1`、`OMP_INFOTYPE_MAPPING_EXISTS=0x2`、`OMP_INFOTYPE_DUMP_TABLE=0x4`、`OMP_INFOTYPE_MAPPING_CHANGED=0x8`、`OMP_INFOTYPE_PLUGIN_KERNEL=0x10`、`OMP_INFOTYPE_DATA_TRANSFER=0x20`、`OMP_INFOTYPE_EMPTY_MAPPING=0x40`、`OMP_INFOTYPE_ALL=0xffffffff`。

   因此 `LIBOMPTARGET_INFO=63`（即 `0x3F`）= 打开除 `ALL` 外的全部位，是「全功能信息」的常用设定；`LIBOMPTARGET_INFO=4` 则只 dump 映射表。

**`LIBOMPTARGET_DEBUG`（详细调试）**：

1. 编译期：仅当定义了 `OMPTARGET_DEBUG` 时，相关宏（`ODBG` 系列）才生成实际代码；否则宏为空。
2. 运行期（若已编译进调试）：读 `LIBOMPTARGET_DEBUG`（回退读 `LIBOFFLOAD_DEBUG`）。值可以是数字（调试级别）或逗号分隔的 `类型:级别` 过滤器。
3. 通过 `ODBG(type, level) << ...` 输出，带 `组件 --> ` 前缀。

**`OFFLOAD_TRACE`（liboffload 追踪）**：

1. `olInit` 初始化时读 `OFFLOAD_TRACE`，存入上下文 `TracingEnabled`。
2. 之后每一次 `ol*` 调用都打印一行形如 `---> olInit(nullptr)-> OL_SUCCESS` 的记录。

#### 4.4.3 源码精读

`LIBOMPTARGET_INFO` 的位掩码定义就在 `OpenMPInfoType` 枚举里：

[include/Shared/Debug.h:L50-L68](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Debug.h#L50-L68) —— 每个常量是一个 bit；把它们按位或即可同时开启多类信息。`OMP_INFOTYPE_ALL = 0xffffffff` 是「全开」。

实际读取环境变量并转成级别的地方：

[include/Shared/Debug.h:L70-L81](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Debug.h#L70-L81) —— `getInfoLevelInternal()` 用 `std::call_once` 保证只读一次 `LIBOMPTARGET_INFO`，`std::stoi` 转整数后存入 `static std::atomic<uint32_t>`；`getInfoLevel()` 原子加载供各处查询。

`INFO` 宏的判定逻辑（信息位掩码的「开关」语义就在这里）：

[include/Shared/Debug.h:L142-L149](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Debug.h#L142-L149) —— 若调试开启则走 `INFO_DEBUG_INT`（详细路径），否则当 `getInfoLevel() & _flags` 非零时用 `INFO_MESSAGE` 打印。这正是「位掩码选择」的核心。

`LIBOMPTARGET_DEBUG` 的读取与过滤器解析（注意外层的 `#ifdef OMPTARGET_DEBUG`）：

[include/Shared/Debug.h:L295-L348](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Debug.h#L295-L348) —— 这里先尝试 `LIBOMPTARGET_DEBUG`，再回退 `LIBOFFLOAD_DEBUG`；值为 `"0"` 时不启用；纯数字当作默认级别；否则按逗号拆成 `类型:级别` 的过滤器列表。但**整段被包在 `#ifdef OMPTARGET_DEBUG`（见 Debug.h:L264）内**——没定义该宏时 `isDebugEnabled()` 恒为 `false`、`ODBG` 展开为空。

这个 `OMPTARGET_DEBUG` 宏由顶层 CMakeLists 控制：

[CMakeLists.txt:L244-L251](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L244-L251) —— Debug 构建类型默认开启 `LIBOMPTARGET_ENABLE_DEBUG`，否则默认关闭；开启时 `add_definitions(-DOMPTARGET_DEBUG)`。也就是说，你在 Release 构建上设 `LIBOMPTARGET_DEBUG=1` 通常**看不到任何输出**——必须用 Debug 构建（或显式 `-DLIBOMPTARGET_ENABLE_DEBUG=ON`）重新编译。

官方测试也明确把这一点标为前置条件：

[test/env/omp_target_debug.c:L1-L5](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/test/env/omp_target_debug.c#L1-L5) —— `REQUIRES: libomptarget-debug` 表示该测试只在带调试的构建上运行；它验证 `LIBOMPTARGET_DEBUG=1` 时 stderr 含 `omptarget`，而 `=0` 时不含。

`LIBOMPTARGET_INFO` 的官方用法示范（位掩码 `63`）：

[test/offloading/info.c:L1-L6](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/test/offloading/info.c#L1-L6) —— 用 `env LIBOMPTARGET_INFO=63` 运行，并用 FileCheck 校验输出里出现「Entering OpenMP data region」「Copying data from host to device」「Launching kernel ...」等信息行。这条测试也是理解「INFO 能打印什么」的最佳参考。

`OFFLOAD_TRACE` 属于 liboffload，在 `olInit` 初始化上下文时被读取：

[liboffload/src/OffloadImpl.cpp:L321-L322](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/src/OffloadImpl.cpp#L321-L322) —— `Context.TracingEnabled = std::getenv("OFFLOAD_TRACE");` 紧挨着还有 `OFFLOAD_DISABLE_VALIDATION`（校验开关）。这说明 `OFFLOAD_TRACE` 与 libomptarget 的两套变量**在源码上就分属不同模块**。

liboffload README 给出了它的用法与输出样例：

[liboffload/README.md:L19-L28](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md#L19-L28) —— `OFFLOAD_TRACE=1 ./offload.unittests` 会打印 `---> olInit(nullptr)-> OL_SUCCESS` 之类的调用序列；并注明「host 插件暂不支持」。

#### 4.4.4 代码实践

**实践目标**：用三套观测手段分别观察同一个程序，体会它们的差异（**待本地验证**）。

**操作步骤**（沿用 4.1 的 `vec_add`，假设 host 插件已构建）：

1. **INFO（位掩码，默认可用）**：

   ```bash
   LIBOMPTARGET_INFO=63 ./vec_add 2>&1 | grep info
   ```

2. **DEBUG（需 Debug 构建）**：仅当运行时以 Debug 构建（或 `-DLIBOMPTARGET_ENABLE_DEBUG=ON`）时有效：

   ```bash
   LIBOMPTARGET_DEBUG=1 ./vec_add 2>&1 | head
   ```

3. **TRACE（仅 liboffload 程序）**：对基于 liboffload 的程序（如 4.2 的设备信息工具）有效，对 `vec_add`（libomptarget 程序）**无效**：

   ```bash
   OFFLOAD_TRACE=1 ./llvm-offload-device-info 2>&1 | head
   ```

**需要观察的现象**：
- INFO：能看到数据搬运（`Copying data ...`）、映射表变化、内核启动等结构化信息。
- DEBUG（若可用）：输出远比 INFO 详细，带 `组件 --> ` 前缀和级别。
- TRACE：每行是一次 `ol*` 调用与返回码；对 `vec_add` 不会产生 trace（因为它不调用 liboffload）。

**预期结果 / 待本地验证**：INFO 与（在 Debug 构建下的）DEBUG 输出可对照 `test/offloading/info.c` 与 `test/env/omp_target_debug.c` 的 FileCheck 期望来理解。TRACE 仅在 liboffload 程序上生效。**待本地验证**：请在本地构建产物上记录实际输出；若在 Release 构建上 `LIBOMPTARGET_DEBUG=1` 无输出，属预期行为（见 4.4.3 的编译期裁剪）。

#### 4.4.5 小练习与答案

**练习 1**：`LIBOMPTARGET_INFO=63` 和 `LIBOMPTARGET_INFO=4` 有何区别？

> **参考答案**：`63 = 0x3F`，打开了 `OpenMPInfoType` 中除 `OMP_INFOTYPE_ALL(0xffffffff)` 外的所有位（内核参数、映射变化、数据搬运、内核信息等），输出最全；`4 = OMP_INFOTYPE_DUMP_TABLE`，只在内核退出/失败时 dump 主机-设备指针映射表。位掩码允许用按位或组合任意子集。

**练习 2**：为什么我在 Release 构建上设了 `LIBOMPTARGET_DEBUG=1` 却看不到任何调试输出？

> **参考答案**：因为详细调试日志的宏（`ODBG` 系列）被包在 `#ifdef OMPTARGET_DEBUG` 内，而该宏仅在 Debug 构建（或显式 `-DLIBOMPTARGET_ENABLE_DEBUG=ON`）时由 CMake `-DOMPTARGET_DEBUG` 定义。Release 构建里这些宏被编译消除，环境变量无从生效。对应测试也用 `REQUIRES: libomptarget-debug` 标注了这一前置条件。

**练习 3**：`OFFLOAD_TRACE=1` 能用来调试 4.1 的 `vec_add` 吗？

> **参考答案**：不能。`vec_add` 走 libomptarget（`__tgt_*`），`OFFLOAD_TRACE` 只追踪 liboffload 的 `ol*` 调用。要观测 `vec_add` 应使用 `LIBOMPTARGET_INFO` / `LIBOMPTARGET_DEBUG`。`OFFLOAD_TRACE` 适用于 4.2 的 `llvm-offload-device-info` 这类 liboffload 程序。

---

## 5. 综合实践

把本讲内容串起来，完成一次「端到端」的观测：

1. **准备**：按 u1-l2 构建 offload，至少包含 host 插件；额外用 `-DLIBOMPTARGET_ENABLE_DEBUG=ON`（或 Debug 构建类型）再构建一次，以便对比 INFO 与 DEBUG。
2. **写程序**：写一个含 `target data map(to:)/map(from:)` 与 `target parallel for` 的小程序（可在 4.1 的 `vec_add` 基础上加一个 `target data` 区域）。
3. **编译运行**：用 `clang -fopenmp -fopenmp-targets=x86_64-unknown-linux-gnu`（按本机架构调整三元组）编译，在 host 插件上运行，确认结果正确。
4. **看设备**：运行 `llvm-offload-device-info`，记录它列出的平台后端与设备类型，并对照源码（4.2.3）说明每个字段由哪个 `OL_*_INFO_*` 查询得来。
5. **开 INFO**：用 `LIBOMPTARGET_INFO=63` 重新运行你的程序，截取并标注：哪几行对应「建立映射」、哪几行对应「主机→设备 / 设备→主机 数据搬运」、哪一行对应「内核启动」。可参照 `test/offloading/info.c` 的 FileCheck 注释作为「答案对照表」。
6. **开 DEBUG（若为 Debug 构建）**：用 `LIBOMPTARGET_DEBUG=1` 运行，对比它与 INFO 的详尽程度，注意每行带 `组件 --> ` 前缀。
7. **试 TRACE**：对 `llvm-offload-device-info` 用 `OFFLOAD_TRACE=1` 运行，确认能看到 `ol*` 调用序列；再对 `vec_add` 试一次，确认**没有** trace 输出——从而亲手验证「两套变量分属两条上层」。

把第 5、6、7 步的输出整理成一张表（变量名 / 作用于哪个程序 / 典型输出行），这张表就是你今后调试任何 offload 程序的速查卡。

> 若本地暂无可用构建环境，本综合实践可作为「源码阅读 + 命令预案」完成：把每一步**预期**的命令与现象写下来，并标注「待本地验证」。重点是理清三套观测手段的边界，而非编造输出。

## 6. 本讲小结

- 编译 OpenMP 卸载程序的关键是 `-fopenmp -fopenmp-targets=<三元组>`；host 插件的三元组就是主机三元组（x86_64 上为 `x86_64-unknown-linux-gnu`），适合无 GPU 环境下学习整条链路。
- `llvm-offload-device-info` 是 liboffload 新 API（`ol*`）的真实客户端，用于枚举设备与属性；它链接 `LLVMOffload`，与旧的 `__tgt_*` 是两条路径。
- `include/Shared/EnvironmentVar.h` 的 `Envar<Ty>` 是「带类型与默认值的环境变量」通用框架，被各插件/运行时广泛复用（如 `BoolEnvar`/`StringEnvar`）；非法值会静默回退默认。
- `LIBOMPTARGET_INFO` 是**位掩码**（`OpenMPInfoType`，`63`≈全开），默认可用，选择性打印关键事件。
- `LIBOMPTARGET_DEBUG` 是详细调试，**只在编译期定义 `OMPTARGET_DEBUG`（Debug 构建或 `-DLIBOMPTARGET_ENABLE_DEBUG=ON`）时才生效**，Release 构建里被编译消除。
- `OFFLOAD_TRACE` **只属于 liboffload**，只追踪 `ol*` 调用，对纯 libomptarget 程序无效；三者不可混用。

## 7. 下一步学习建议

本讲让你具备了「把程序跑起来并观测运行时」的能力。接下来：

- **进入运行时内部**：学习 u2-l1（运行时初始化与库注册入口 `__tgt_register_lib` 等），看 `vec_add` 运行时那句「Launching kernel」背后，运行时是如何被初始化、如何把设备镜像注册到插件的。
- **理解你看到的信息**：u2-l4（主机-设备数据映射）和 u2-l5（target data begin/end/update 流程）会解释 `LIBOMPTARGET_INFO` 里那些「Creating new map entry / Copying data / Removing map entry」到底对应源码里的哪段逻辑。
- **深入设备信息来源**：若你对 `llvm-offload-device-info` 背后的 liboffload 抽象感兴趣，可先读 u3-l1（通用插件接口 `GenericPluginTy`），再看 u3-l11（liboffload 统一 API），理解「设备属性」是如何从底层插件一路暴露到 `olGetDeviceInfo` 的。

建议在本讲基础上，先跑通一次「INFO=63 看完整调用」，带着那份输出再进入第二单元——它会成为你阅读源码时的「活地图」。
