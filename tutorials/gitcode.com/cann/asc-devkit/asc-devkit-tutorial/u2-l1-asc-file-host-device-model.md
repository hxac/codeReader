# .asc 源文件与 Host/Device 混合编译模型

## 1. 本讲目标

本讲是「第一个算子」单元的第一篇。学完后你应当能够：

- 说清楚 `.asc` 文件是什么，以及为什么 Host（主机/CPU）代码和 Device（设备/NPU）Kernel 代码可以写在**同一个文件**里。
- 看到一个 `.asc` 文件时，能准确划分出哪几行是 Host 侧代码、哪几行是 Device 侧 Kernel 代码、两者的边界由什么决定。
- 掌握 `__global__` / `__vector__` / `__cube__` / `__aicore__` 等**函数限定符**的含义，知道它们分别把函数「贴」到哪种硬件上执行。
- 掌握 `__gm__` / `__ubuf__` 等地址空间限定符的含义，理解为什么 Kernel 的指针入参必须带 `__gm__`。
- 理解「单源混合编译」相对于「Host 与 Device 分两个文件」的优势。

本讲只聚焦 `.asc` 文件本身的结构与两类限定符；`<<<>>>` 启动语法与 ACL 运行时接口的细节是下一讲（u2-l2）的主题，本讲只把它们当作 Host 与 Device 的「边界标记」。

## 2. 前置知识

阅读本讲前，你应已经具备 u1-l1、u1-l2、u1-l3 建立的认知。简单回顾对本讲最相关的三点：

1. **Ascend C 是 CANN 的算子开发语言**，原生 C/C++ 加最小化语法扩展，源码就放在 asc-devkit 仓库里。
2. **入口头文件是聚合器**：基础 API / 框架编程的主入口是 `kernel_operator.h`，调试打印入口是 `utils/debug/asc_printf.h`（见 u1-l2 介绍的入口头文件体系）。本讲的 `add.asc` 就会 `#include "kernel_operator.h"`。
3. **源码会被编译成可执行 demo**（u1-l3 讲过 `build.sh` 与样例 CMakeLists）。本讲要回答的是：编译器到底从一个 `.asc` 文件里读出了什么、怎么把它拆成 Host 部分和 Device 部分。

补充两个本讲要用到的直觉概念：

- **Host（主机侧）**：运行你程序入口 `main()` 的 CPU。它负责准备数据、申请 NPU 显存、下发任务、回收结果。
- **Device（设备侧）**：昇腾 AI Core，算子真正做并行计算的地方。一段被「贴」上限定符的 Kernel 函数会被实例化成很多份，分别调度到多个 AI Core 上并行跑。

如果你接触过 CUDA，可以把 `.asc` 类比为 `.cu`：同样是「单源、Host/Device 混写、用限定符区分」。

## 3. 本讲源码地图

本讲只围绕两个最小样例展开，它们是整个仓库里最简单的 `.asc` 文件：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc) | 最小 Kernel 直调样例，仅打印一行字符串 | 用最短代码展示 `.asc` 的 Host/Device 拆分与 `__global__ __vector__` 限定符 |
| [examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) | 矢量加法 `z = x + y` 样例 | 在 hello_world 基础上展示 `__gm__` 指针入参、模板 Kernel、Host 编排函数、结果校验 |

另外会引用两份权威文档作为限定符语义的依据：

| 文档 | 作用 |
|------|------|
| [docs/zh/guide/编程指南/编程模型/AI-Core-SIMD编程/核函数.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编程模型/AI-Core-SIMD编程/核函数.md) | 核函数定义规则、Host/Kernel/Device 三类函数的调用关系 |
| [docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md) | 函数执行空间限定符与地址空间限定符的权威定义表 |

## 4. 核心概念与源码讲解

### 4.1 .asc 文件结构

#### 4.1.1 概念说明

`.asc` 是 Ascend C 算子源文件的后缀。它最大的特点是**单源混合编写（Single-Source）**：Host 侧的 C++ 代码与 Device 侧的 Kernel 代码写在**同一个文件**里。

为什么要单源？因为算子开发高度依赖「Host 编排 + Device 计算」的协作：Host 端申请显存、搬数据、下发任务，Device 端做真正的并行计算。如果两边的代码分属两个文件、两套工程，开发者就要反复跳转、手动保持接口一致。Ascend C 借鉴了 CUDA `.cu` 的思路，把两边放进一个文件，由编译器（bisheng-compiler，见 u1-l3 提到的版本锁定）自动拆分。

那么编译器靠什么判断「这一段是 Host、那一段是 Device」？**靠函数头上的限定符**，而不是靠文件物理拆分。这是本讲最核心的一句话：

> `.asc` 文件里 Host 与 Device 的边界，由函数限定符（`__global__` 等）隐式划定，而不是由「哪个文件」决定。

没有限定符的普通函数（比如 `main`、`kernel_add`、`VerifyResult`）默认就是 Host 函数；带 `__global__` 的函数是 Device Kernel 入口；带 `__aicore__` 的函数是 Device 侧辅助函数（只能被 Kernel 或其它 `__aicore__` 函数调用）。

#### 4.1.2 核心流程

编译器处理一个 `.asc` 文件的简化流程：

```text
.asc 源文件
   │
   ├── 扫描每个函数的限定符
   │     ├─ 无限定符 / __host__        → 归入 Host 编译单元（如 main、kernel_add）
   │     ├─ __global__ (+__vector__…)  → 归入 Device 编译单元，作为 Kernel 入口
   │     └─ __aicore__                 → 归入 Device 编译单元，作为 Kernel 的辅助函数
   │
   ├── Host 编译单元 → 用主机编译器编译
   ├── Device 编译单元 → 用设备编译器编译成 AI Core 指令
   │
   └── 链接 → 生成一个可执行 demo（如 ./demo）
```

运行时的调用方向是单向的：

```text
Host main()  ──<<<>>>异构调用──▶  Device Kernel（在多个 AI Core 上并行）
            ◀──aclrtSynchronizeStream 同步──
```

官方文档把 `.asc` 里的函数严格划分为三类（Host 侧执行函数、核函数、Device 侧执行函数），并规定了调用方向：Host 用 `<<<>>>` 下发 Kernel，Kernel 可以再调 Device 辅助函数，但 Device 函数不能反过来调 Host 函数。详见 [核函数.md:47-54](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编程模型/AI-Core-SIMD编程/核函数.md#L47-L54)。

#### 4.1.3 源码精读

先用最短的 hello_world 看清结构。整个文件只有三段：

**第 1 段：头文件包含（Host 与 Device 共用）**

[hello_world.asc:16-17](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L16-L17) 引入了 printf 调试头与 ACL 运行时头：

```cpp
#include "utils/debug/asc_printf.h"   // 提供 Kernel 内的 printf（Device 侧也会用到）
#include "acl/acl.h"                  // 提供 aclrtXxx 运行时接口（Host 侧用到）
```

注意：头文件本身不区分 Host/Device，两边的代码都可能用到它们。

**第 2 段：Device 侧 Kernel（一行）**

[hello_world.asc:19-19](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L19-L19) 就是整个算子：

```cpp
__global__ __vector__ void hello_world() { printf("Hello World!!!\n"); }
```

`__global__` 说明它是 Kernel 入口，`__vector__` 说明它在 Vector 核上跑。函数体只有一句 printf，但它在第 4.2 节你会看到，这行会被实例化成多份并行执行。

**第 3 段：Host 侧 main**

[hello_world.asc:21-31](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L21-L31) 是普通的 C++ `main`，没有任何限定符，所以是 Host 函数：

```cpp
int main(int argc, char const* argv[]) {
    aclrtSetDevice(0);                 // 申请运行资源
    aclrtStream stream = nullptr;
    aclrtCreateStream(&stream);
    hello_world<<<8, 0, stream>>>();   // ← Host→Device 的边界：用 <<<>>> 下发 Kernel
    aclrtSynchronizeStream(stream);    // 等待 Kernel 跑完
    aclrtDestroyStream(stream);
    aclrtResetDevice(0);               // 释放运行资源
    return 0;
}
```

这里 `<<<8, 0, stream>>>` 就是 Host 调用 Device 的「跨界桥」，本讲只需把它当成边界标记，参数含义留给 u2-l2。

再看更完整、更贴近真实算子的 add.asc，它的 `.asc` 结构比 hello_world 丰富，但边界规则完全一样：

**头文件段**（[add.asc:16-25](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L16-L25)）：标准 C++ 头加上 `acl/acl.h` 和 [add.asc:25-25](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L25-L25) 的 `kernel_operator.h`（u1-l2 讲过的基础 API 入口头）。

**Device Kernel 段**（[add.asc:27-63](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L27-L63)）：一个模板 Kernel `add_custom`，带 `__vector__ __global__` 限定符，入参是三个 `__gm__ float*`。这部分是真正的「搬入—计算—搬出」算子逻辑。

**Host 编排段**（[add.asc:65-106](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L65-L106)）：普通函数 `kernel_add`，负责 `aclrtMalloc` 申请显存、`aclrtMemcpy` 搬数据、[add.asc:90-90](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L90-L90) 用 `<<<numBlocks, 0, stream>>>` 下发 Kernel、再搬回结果。

**校验与入口段**（[add.asc:108-151](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L108-L151)）：`VerifyResult` 比对输出与 golden，`main` 生成输入数据、调用 `kernel_add`、做精度校验。注意这个样例**没有单独的 gen_data 文件**，数据生成直接写在 `main` 里。

把两个文件对照，就能提炼出 `.asc` 的通用骨架：

| 段落 | hello_world.asc | add.asc | 归属 |
|------|----------------|----------|------|
| 头文件 | 16-17 | 16-25 | 共用 |
| Kernel 定义 | 19 | 27-63 | Device |
| Host 编排/资源 | 21-31（main 内联） | 65-106（kernel_add） | Host |
| 校验/入口 | 无 | 108-151 | Host |

结论：**hello_world 是 add 的「最小骨架版」**——同样的 Host/Device 拆分规则，只是把数据搬运、计算、校验都省略了。

#### 4.1.4 代码实践

**实践目标**：亲手在两个文件里划出 Host/Device 边界，巩固「限定符决定边界」的判断力。

**操作步骤**：

1. 打开 [hello_world.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc)，逐行标注「H」（Host）或「D」（Device）。提示：只有第 19 行是 D，其余函数体都是 H。
2. 打开 [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc)，做同样的标注。提示：`add_custom`（27-63）是 D；`kernel_add`、`VerifyResult`、`main` 都是 H。
3. 回答：`add_custom` 函数体内部调用的 `AscendC::DataCopy(...)`、`AscendC::Add(...)` 这些接口，运行在 Host 还是 Device？为什么？

**需要观察的现象 / 预期结果**：

- 你会发现 Device 段一定以 `__global__` 开头；Host 段没有任何 `__global__` / `__aicore__` 限定符。
- `AscendC::DataCopy` / `AscendC::Add` 出现在 `add_custom` 函数体内，因此它们运行在 Device 侧——它们是 Kernel 内部的计算/搬运指令，不是 Host 端的 ACL 接口。

> 待本地验证：如果你已按 u1-l3 配好环境，可在样例目录执行 `cmake -DCMAKE_ASC_RUN_MODE=cpu ..; make -j; ./demo`，CPU 调试模式下运行不会真正上板，但能验证「Host 与 Device 代码被正确编译并链接到同一个 demo」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `add.asc` 里的 `main` 函数也加上 `__global__` 限定符，会发生什么？

**参考答案**：`main` 会被编译器当成 Device Kernel 入口，但它实际上需要被 C++ 运行时作为程序入口调用、且其内部调用了 `aclrtMalloc` 等 Host 接口——这与 `__global__` 的语义（Device 侧执行、只能被 Host 用 `<<<>>>` 调用、返回 void）冲突，编译期就会报错。Host 入口函数必须保持「无限定符」。

**练习 2**：hello_world.asc 里 Kernel 中的 `printf` 和 main 里的 `aclrtSetDevice`，分别属于哪一侧的接口？

**参考答案**：`printf`（来自 `utils/debug/asc_printf.h`）是 Device 侧调试接口，运行在 AI Core 上；`aclrtSetDevice`（来自 `acl/acl.h`）是 Host 侧 ACL 运行时接口，运行在 CPU 上。它们虽然来自同一个 `.asc` 文件，却分属两侧。

---

### 4.2 Kernel 限定符

#### 4.2.1 概念说明

Kernel 限定符分为两类，它们常常**组合使用**：

1. **函数类型限定符 `__global__`**：声明「这是一个 Kernel 函数」。它的性质是：在 Device 上执行、只能被 Host 用 `<<<>>>` 调用、必须返回 `void`、不能是类的成员函数。`__global__` 只表明「这是 Device 入口」，**不指定**具体核类型。权威定义见 [SIMD-BuiltIn关键字.md:123-129](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L123-L129)。

2. **函数执行空间限定符**：指定 Kernel 究竟在哪种核上跑。常见的有：
   - `__vector__`：仅在 **Vector 核（AIV）** 执行，用于矢量计算算子。见 [SIMD-BuiltIn关键字.md:221-227](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L221-L227)。
   - `__cube__`：仅在 **Cube 核（AIC）** 执行，用于矩阵/Cube 计算算子。见 [SIMD-BuiltIn关键字.md:208-219](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L208-L219)。
   - `__aicore__`：在 Device AI 核上执行，不区分 Vector/Cube（常用于耦合模式）。它也可作为 Device 辅助函数的限定符（只能被 `__global__` 或其它 `__aicore__` 调用）。见 [SIMD-BuiltIn关键字.md:131-147](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L131-L147)。
   - `__mix__(cube, vec)`：同时在 Cube 核和 Vector 核上执行，用于融合算子。见 [SIMD-BuiltIn关键字.md:229-231](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L229-L231)。

官方对核函数定义规则的总结（必须 `__global__` + 一个执行空间限定符 + 指针入参加 `__gm__` + 返回 void）见 [核函数.md:10-22](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编程模型/AI-Core-SIMD编程/核函数.md#L10-L22)。

一句话直觉：`__global__` 回答「它是不是 Kernel」，执行空间限定符回答「它在哪种核上跑」。

#### 4.2.2 核心流程

当你写下：

```cpp
__global__ __vector__ void add_custom(...) { ... }
```

编译器据此做三件事：

1. 把 `add_custom` 归入 Device 编译单元，而不是 Host 编译单元。
2. 限定它只能被 Host 用 `<<<>>>` 调用（直接用普通函数调用语法 `add_custom(...)` 会报错）。
3. 生成面向 Vector 核的指令；运行时它会被实例化为多份，分发到多个 AI Core 并行执行。

限定符书写的**先后顺序不影响语义**——下面两种写法等价，两个样例恰好各用了一种：

```cpp
__global__ __vector__ void hello_world();   // hello_world.asc 的写法
__vector__  __global__ void add_custom(...); // add.asc 的写法
```

#### 4.2.3 源码精读

hello_world 用了最简形式，[hello_world.asc:19-19](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L19-L19)：

```cpp
__global__ __vector__ void hello_world() { ... }
```

它只有 `__global__ __vector__`，没有入参，因此也没有地址空间限定符——这是观察「纯限定符」最干净的例子。`hello_world<<<8, 0, stream>>>()` 中的 `8` 表示在 8 个核上并行执行，于是这一句 `printf` 实际会被打印多次（每核一份）。

add 的 Kernel 头把限定符写在了前面，并带上了模板与入参，[add.asc:28-28](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L28-L28)：

```cpp
template <uint32_t blockLength>
__vector__ __global__ void add_custom(__gm__ float* x, __gm__ float* y, __gm__ float* z)
```

两点对照：

- 两处都用 `__vector__`，因为矢量加法 \( z_i = x_i + y_i \) 是典型的 Vector 计算，不需要 Cube 矩阵单元。
- add 用了 `template <uint32_t blockLength>`，说明 Kernel 也支持 C++ 模板，`blockLength` 在 Host 端以 `add_custom<blockLength><<<...>>>` 显式实例化（[add.asc:90-90](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L90-L90)）。

#### 4.2.4 代码实践

**实践目标**：依据算子类型，判断它该用哪种执行空间限定符。

**操作步骤**：

1. 阅读 [add.asc:28-46](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L28-L46)，确认它只用到 `AscendC::Add` 这类矢量接口，理解为什么限定符是 `__vector__` 而不是 `__cube__`。
2. 做如下推断填空（不必改源码，纯思考）：
   - 一个只做矩阵乘（Mmad）的算子 → 应使用 `______`；
   - 一个同时做矩阵乘和矢量后处理（如 Matmul + Bias + 激活）的融合算子 → 应使用 `______(__, __)`。
3. 把 add.asc 的 `__vector__` 改成 `__cube__`（**仅本地试错，勿提交**），观察编译报错方向，理解「限定符与算子实际用到的计算单元必须匹配」。

**需要观察的现象 / 预期结果**：

- 矩阵乘算子用 `__cube__`；融合算子用 `__mix__(1,1)` 之类。
- 把矢量算子强行贴上 `__cube__` 后，编译/链接阶段会因算子里调用了 Vector 专用接口却跑在 Cube 核上而报错。

> 待本地验证：步骤 3 的具体报错信息取决于编译器版本，建议在本地 CPU 调试模式下编译一次记录报错原文。

#### 4.2.5 小练习与答案

**练习 1**：`__global__` 和 `__vector__` 能否只用其中一个？

**参考答案**：不能任意省略。一个合格的 Kernel 必须同时有「函数类型限定符 `__global__`」和「某个执行空间限定符」（如 `__vector__`/`__cube__`/`__aicore__`）。只有 `__global__` 没有执行空间限定符，编译器无法确定它在哪种核上跑；只有 `__vector__` 没有 `__global__`，则它只是 Device 辅助函数，不能被 Host 用 `<<<>>>` 直接调用。

**练习 2**：为什么 hello_world 用 `__vector__` 而不是 `__cube__`？

**参考答案**：hello_world 只做一次 printf，不涉及任何 Cube 矩阵计算，选 `__vector__`（或在耦合模式下选 `__aicore__`）即可。`__cube__` 专为矩阵/Cube 计算保留，用在这里既无必要、也可能与硬件调度预期不符。

---

### 4.3 地址空间限定符

#### 4.3.1 概念说明

AI Core 内部有**多级独立编址**的片上/片外存储：Global Memory（GM，芯片外大容量显存）、Unified Buffer（UB，Vector 专用片上缓存）、L1、L0A/L0B/L0C（Cube 通路）等。不同存储有各自的访存指令，地址空间彼此独立。

地址空间限定符的作用，就是**告诉编译器一个指针所指向的对象位于哪一级存储**，从而：

- 生成正确的访存指令（访问 GM 和访问 UB 的指令完全不同）；
- 做合法性检查（比如把 UB 指针传给只接受 GM 的接口会报错）。

权威映射表见 [SIMD-BuiltIn关键字.md:311-322](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L311-L322)，摘录与本讲最相关的几项：

| 限定符 | 指向的物理存储 | 本讲是否出现在样例 |
|--------|---------------|--------------------|
| `__gm__` | 设备侧全局内存 GM（Host 用 `aclrtMalloc` 申请的大显存） | ✅ add.asc 的三个入参 |
| `__ubuf__` | Vector Unified Buffer（矢量计算专用片上缓存） | ❌ 被 LocalTensor 封装隐藏 |
| `__cbuf__` / `__ca__` / `__cb__` / `__cc__` | Cube L1 / L0A / L0B / L0C | ❌ 仅 Cube 算子用到 |
| `__fbuf__` | Fixpipe Buffer | ❌ |

重要约束（来自 [SIMD-BuiltIn关键字.md:338-356](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L338-L356)）：

- 地址空间限定符**只能用在指针**上（指针入参、指针返回值、指针变量），不能修饰非指针类型；
- 同一个类型上**不允许叠加多个**地址空间限定符；
- Kernel 的**指针入参必须用 `__gm__`** 修饰，因为 Host 传进来的就是 GM 显存地址。

关于 `__ubuf__`：本讲的两个样例都**没有直接出现** `__ubuf__`。原因是 add.asc 用的是 C++ Tensor 抽象——`LocalTensor<float>`（由 `LocalMemAllocator<Hardware::UB>` 分配）在内部帮你管理了 UB 内存，把裸的 `__ubuf__` 指针藏了起来。`__ubuf__` 的字面写法要到语言扩展层 C API（指针式编程，u8 单元）才会大量出现。权威说明见 [SIMD-BuiltIn关键字.md:394-403](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/语言扩展层/SIMD-BuiltIn关键字.md#L394-L403)。

#### 4.3.2 核心流程

数据在两级存储间的典型流动（以 add 为例）：

```text
Host: aclrtMalloc 在 GM 申请 x/y/z 显存
        │  把 __gm__ 指针作为入参传给 Kernel
        ▼
Device Kernel:
   xGm.SetGlobalBuffer(__gm__ 指针)        # 用 __gm__ 指针描述 GM 上的一段
   DataCopy(xLocal, xGm, ...)              # GM ──搬入──▶ UB（LocalTensor）
   Add(zLocal, xLocal, yLocal, ...)        # 在 UB 上做矢量计算
   DataCopy(zGm, zLocal, ...)              # UB ──搬出──▶ GM
```

关键点：Kernel **不能直接拿 GM 指针做矢量计算**——Vector 单元只能访问 UB。所以必须先用 DataCopy 把数据从 GM（`__gm__`）搬到 UB，计算完再搬回 GM。地址空间限定符正是这条「GM ↔ UB」搬运边界的类型化表达。

#### 4.3.3 源码精读

add.asc 里唯一出现的地址空间限定符就是 `__gm__`，集中在 Kernel 入参上，[add.asc:28-28](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L28-L28)：

```cpp
__vector__ __global__ void add_custom(__gm__ float* x, __gm__ float* y, __gm__ float* z)
```

这三个 `__gm__` 声明：`x`、`y`、`z` 三个指针各自指向 GM 上的一段 float 内存——也就是 Host 端 `aclrtMalloc` 申请出来、再通过 `<<<>>>` 传进来的显存（见 [add.asc:82-90](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L82-L90)）。注意这里的 `blockLength` 是模板参数，不在限定符讨论范围内。

随后 Kernel 用 `SetGlobalBuffer` 把 `__gm__` 指针包装成 `GlobalTensor`，并按 `block_idx`（多核索引）做偏移，[add.asc:32-35](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L32-L35)：

```cpp
AscendC::GlobalTensor<float> xGm, yGm, zGm;
xGm.SetGlobalBuffer(x + block_idx * blockLength, blockLength);  // 每个 GM 指针按核号切分
yGm.SetGlobalBuffer(y + block_idx * blockLength, blockLength);
zGm.SetGlobalBuffer(z + block_idx * blockLength, blockLength);
```

这里 `x + block_idx * blockLength` 仍是 `__gm__` 指针运算——指针保持在 GM 地址空间内，只是起点偏移了一段。`GlobalTensor`（u3 单元细讲）只是给 `__gm__` 指针套了一层「带长度」的 C++ 外壳，底层地址空间没变。

对照看 hello_world：[hello_world.asc:19-19](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L19-L19) 的 Kernel **没有任何入参**，所以也就没有任何地址空间限定符——它不访问 GM。这是一个很有教学意义的对照：**地址空间限定符只在用到对应存储时才出现**，没有数据搬运就不需要 `__gm__`。

#### 4.3.4 代码实践

**实践目标**：在真实代码里定位每一个地址空间限定符，并解释其作用。

**操作步骤**：

1. 在 [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) 中搜索 `__gm__`，确认它们只出现在 Kernel 入参（第 28 行），共 3 处。
2. 追踪一个 `__gm__` 指针的「一生」：从 Host 的 `aclrtMalloc`（[add.asc:82-84](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L82-L84)）→ 通过 `<<<>>>` 传入（第 90 行）→ 被 `SetGlobalBuffer` 包装（第 33 行）→ 被 `DataCopy` 读取（第 42 行）。画出这条链路。
3. 在 [hello_world.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc) 中搜索 `__gm__`，确认结果是 0 处，并解释原因。

**需要观察的现象 / 预期结果**：

- add.asc 中 `__gm__` 恰好 3 处，全部修饰 Kernel 入参指针；函数体内 `SetGlobalBuffer`、`DataCopy` 的目标虽然也涉及 GM，但都通过 `GlobalTensor` 对象访问，不再写裸限定符。
- hello_world.asc 中 `__gm__` 为 0 处——因为该 Kernel 不接收任何 GM 数据，自然不需要这个限定符。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Kernel 的指针入参必须是 `__gm__`，而不能写成普通 `float*`？

**参考答案**：Host 通过 `aclrtMalloc` 申请的是设备侧 GM 显存，传入 Kernel 的就是 GM 地址。若写成普通 `float*`（默认 private 地址空间），编译器无法知道它指向 GM，会生成错误的访存指令或直接合法性报错。`__gm__` 既告诉编译器「这是 GM 地址」，也开启了 GM 专用的访存路径与检查。

**练习 2**：add.asc 里为什么不出现 `__ubuf__`？它「藏」在哪里？

**参考答案**：add.asc 用 C++ Tensor 抽象——`LocalMemAllocator<Hardware::UB>` 在 UB 上分配内存，返回 `LocalTensor<float>`（第 37-40 行）。`LocalTensor` 在内部封装了 `__ubuf__` 指针，使开发者无需手写裸的 `__ubuf__`。等价的裸 `__ubuf__` 写法要到 C API（指针式编程）样例才会直接出现。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读 + 写」小任务。

**任务**：假设你要新写一个**矢量数乘算子** \( z_i = x_i \cdot k \)（`k` 是标量），请基于本讲学到的 `.asc` 结构与限定符规则，**在纸上**完成一份骨架设计（无需真正编译）。

要求产出：

1. **文件分段表**：列出这个新 `.asc` 文件应当包含哪些段（头文件 / Device Kernel / Host 编排 / 校验入口），并标注每段的归属（H/D）。
2. **Kernel 签名**：写出 Kernel 函数头，要求：
   - 同时具备「函数类型限定符」和「执行空间限定符」（说明你选哪个、为什么）；
   - 入参包含输入 `x`、输出 `z`（都是 GM 上的 float 数组）和标量 `k`，正确的入参上要带地址空间限定符；
   - 返回类型正确。
3. **对照验证**：拿你设计的签名和 [add.asc:28-28](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L28-L28) 对比，说明哪些地方相同、哪些地方因「数乘 vs 加法」「多一个标量入参」而不同。

**参考思路（先自己想再对照）**：

- 段落与 add.asc 完全同构（头文件 / Kernel / `kernel_xxx` 编排 / `VerifyResult` / `main`）。
- Kernel 头形如 `__vector__ __global__ void scale_custom(__gm__ float* x, __gm__ float* z, float k)`——数乘是矢量运算，故用 `__vector__`；`x`、`z` 是 GM 指针故带 `__gm__`；标量 `k` 是普通按值传递，**不加**地址空间限定符（限定符只能修饰指针，不能修饰非指针）。
- 与 add 的差异：少一个 `__gm__` 入参（没有 `y`）、多一个标量 `k`、模板参数按需保留；Kernel 内部把 `Add(...)` 换成 `Muls(...)`（标量乘，后续矢量计算单元会讲）。

> 待本地验证：若环境就绪，可仿照 add 样例的 CMakeLists 新建工程，把上面的骨架补全后在 CPU 调试模式下编译运行，验证你对结构与限定符的理解是否正确。

## 6. 本讲小结

- `.asc` 是 Ascend C 的**单源**算子文件，Host（CPU）与 Device（AI Core）代码混写在同一文件里，由编译器自动拆分。
- Host/Device 的边界**由函数限定符隐式决定**，而非由文件拆分决定：带 `__global__` 的是 Device Kernel 入口，无限定符的普通函数（`main`、`kernel_add` 等）是 Host 函数。
- 一个合法 Kernel 必须同时有「函数类型限定符 `__global__`」和「执行空间限定符」（`__vector__` / `__cube__` / `__aicore__` / `__mix__`），二者**顺序可互换**；`__global__` 表示「是 Kernel」，执行空间限定符表示「在哪种核上跑」。
- 地址空间限定符（`__gm__` / `__ubuf__` / `__cbuf__` …）指明指针指向哪一级物理存储，**只能修饰指针**，且 Kernel 的指针入参必须带 `__gm__`。
- hello_world 是 `.asc` 结构的最小骨架；add 在其上增加了 `__gm__` 入参、模板 Kernel、Host 编排与结果校验，二者共用同一套边界规则。
- 本讲两个样例都未直接出现 `__ubuf__`——它被 C++ 的 `LocalTensor` 封装隐藏，裸写形式要到 C API（u8 单元）才会出现。

## 7. 下一步学习建议

本讲只把 `.asc` 文件当成「静态文本」来理解结构与限定符，还没有真正解释 Host 如何把任务「发」给 Device。建议接下来：

1. **学习 u2-l2《Kernel 启动语法 `<<<>>>` 与 ACL 运行时》**：搞清楚 `<<<numBlocks, dynUBufSize, stream>>>` 三个参数的准确含义，以及 `aclrtMalloc` / `aclrtMemcpy` / `aclrtSynchronizeStream` 如何配合完成 Host 与 Device 的内存管理与同步。
2. **学习 u2-l3《端到端跑通第一个矢量加法算子》**：亲手编译运行 add 样例，把本讲的「纸面理解」变成可观察的运行结果。
3. **后续延伸**：等进入 u3（内存层级）时再回头看本讲的 `__gm__` 与 `GlobalTensor`，你会对「地址空间限定符 ↔ 物理存储」的对应有更立体的认识；进入 u8（C API）时则会第一次直接手写 `__ubuf__`。

建议在进入 u2-l2 之前，先回头重读 [add.asc:65-106](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L65-L106) 的 Host 编排段，带着「这一段里每个 aclrt 接口到底干了什么」的问题去学下一讲，效果最好。
