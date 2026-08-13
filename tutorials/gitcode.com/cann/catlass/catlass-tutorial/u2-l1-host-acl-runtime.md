# Host 侧代码、ACL 运行时与精度验证

## 1. 本讲目标

本讲承接 [u1-l4（环境搭建与编译运行首个样例）](u1-l4-build-and-run.md)：你已经能编译并运行 `00_basic_matmul`，看到 `Compare success.`。本讲要回答的问题是——**这句「Compare success」是怎么来的？从命令行参数到最终精度对比，Host 侧代码到底做了哪些事？**

学完本讲，你应当能够：

1. 说出 `aclInit / aclrtSetDevice / aclrtCreateStream / aclrtMalloc / aclrtMemcpy / aclrtFree` 这一串 ACL 调用的标准顺序与各自职责。
2. 画出 Host 侧「初始化—分配—拷贝—执行—对比—释放」六个阶段在 `basic_matmul.cpp` 中的代码区间。
3. 说清楚 `golden.hpp` 背后那三个子头文件（`fill_data.hpp` / `matmul.hpp` / `compare_data.hpp`）分别负责「造数据—算真值—比精度」，并解释 `CompareData` 用的相对误差阈值是怎么定的。

本讲只读 Host 侧（`Run()` 函数 + `main()` + 公共组件），**不涉及** Kernel/Block/Tile 的内部实现——那是 u2-l2 之后的内容。Host 侧组装 GEMM 类型的那几行 `using` 也只做「指认」，不展开模板参数含义。

## 2. 前置知识

在进入源码前，先用大白话建立三个概念。

**ACL 是什么。** ACL（Ascend Computing Language）运行时是 Host（CPU 侧）和 Device（NPU 侧）之间的「接线员」。Host 想让 NPU 干活，必须先通过 ACL 把设备打开、建一条命令流（stream）、在设备的全局内存（Global Memory，简称 GM）里申请显存、把数据搬过去，再把算子丢到流上执行。这一套调用都以 `acl` / `aclrt` 开头。

**Host 内存 vs Device 显存。** CPU 能直接访问的是 Host 内存（代码里的 `std::vector<fp16_t> hostA`），NPU 算子能直接访问的是 Device 的 GM（代码里的 `uint8_t* deviceA`）。两者物理上不共享，所以必须用 `aclrtMemcpy` 在它们之间搬数据：

- Host → Device：记作 H2D（枚举 `ACL_MEMCPY_HOST_TO_DEVICE`），搬输入。
- Device → Host：记作 D2H（枚举 `ACL_MEMCPY_DEVICE_TO_HOST`），把算完的结果取回来。

**真值（golden）与精度对比。** NPU 上用 fp16 跑出来的结果，会和「理论上正确的答案」有舍入误差。怎么判断算得对？做法是：在 CPU 上用更高精度（这里用 `float`）把同一道矩阵乘重新算一遍，得到「真值」`hostGolden`；再把 NPU 的输出 `hostC` 和真值逐元素比，只要每个元素的相对误差小于一个阈值，就算通过、打印 `Compare success.`。这套机制在 `examples/common/golden.hpp` 里。

> 名词提示：代码里 `half` 和 `fp16_t` 都是半精度浮点（IEEE fp16）。`helper.hpp` 里 `using op::fp16_t;` 把 CANN 提供的 `fp16_t` 类型引入，样例里 Host 缓冲区用 `std::vector<fp16_t>`，而模板参数写成 `half`——在本样例中二者等价，都表示 16 位浮点。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `examples/00_basic_matmul/basic_matmul.cpp` | 样例主文件，含 `main()` 与 `Run()` | Host 侧六阶段全流程 |
| `examples/common/helper.hpp` | 公共工具头 | `ACL_CHECK` 宏、`IsNeedPadding`、`RunAdapter` |
| `examples/common/golden.hpp` | golden 聚合头 | 把三个 golden 子头打成一个包 |
| `examples/common/golden/fill_data.hpp` | 数据填充 | `FillRandomData`（造随机输入） |
| `examples/common/golden/matmul.hpp` | CPU 真值计算 | `ComputeMatmul`（三重循环算 C=A·B） |
| `examples/common/golden/compare_data.hpp` | 精度对比 | `CompareData`（相对误差阈值判断） |
| `examples/common/options.hpp` | 命令行参数解析 | `GemmOptions::Parse`（解析 m/n/k/deviceId） |
| `include/catlass/layout/matrix.hpp` | 布局类型 | `RowMajor::GetOffset`（坐标→字节偏移） |

注意：`golden.hpp` 本身只有一行实质内容——它把四个 golden 子头 `#include` 进来：

```cpp
#include "golden/compare_data.hpp"
#include "golden/fill_data.hpp"
#include "golden/matmul.hpp"
#include "golden/conv2d.hpp"
#include "golden/matrix_inverse.hpp"
```

见 [examples/common/golden.hpp:14-18](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden.hpp#L14-L18)。本讲只用到前三个（`fill_data` / `matmul` / `compare_data`），后两个（conv2d、matrix_inverse）是给其它样例用的。

---

## 4. 核心概念与源码讲解

### 4.1 Host 侧全流程总览：六个阶段

#### 4.1.1 概念说明

一次完整的样例运行，Host 侧代码的生命周期可以切成六个阶段。这不是项目文档强加的框架，而是从代码结构自然浮现的顺序——记住这六个词，就记住了「一段 Host 代码该长什么样」：

1. **初始化**：打开 ACL 运行时、选定 NPU 设备、建一条流。
2. **分配**：在 Host 上准备输入缓冲区，在 Device GM 上申请 A/B/C（以及 workspace）显存。
3. **拷贝**：把 Host 上的随机输入搬（H2D）到 Device GM。
4. **执行**：组装算子、查 workspace 大小、初始化、把算子丢到流上、等流跑完。
5. **对比**：把结果搬（D2H）回 Host，CPU 算真值，逐元素比精度。
6. **释放**：按申请的相反顺序释放显存、销毁流、复位设备、关闭运行时。

#### 4.1.2 核心流程

整个 Host 流程用伪代码概括：

```
main(argc, argv):
    options.Parse()          # 解析 m/n/k/deviceId
    Run(options)

Run(options):
    # ① 初始化
    aclInit(); aclrtSetDevice(devId); aclrtCreateStream(&stream)
    # ② 分配（Host 缓冲 + Device 显存）
    hostA, hostB = vector<fp16_t>(...)
    aclrtMalloc(deviceA/B/C)
    # ③ 拷贝（填随机数 + H2D）
    FillRandomData(hostA/B); aclrtMemcpy(deviceA/B <- hostA/B)
    # ④ 执行（组装 + 跑）
    matmulOp.CanImplement / GetWorkspaceSize / Initialize / (stream, coreNum)
    aclrtSynchronizeStream(stream)
    # ⑤ 对比（D2H + 真值 + 比对）
    aclrtMemcpy(hostC <- deviceC); ComputeMatmul(hostGolden); CompareData(hostC, hostGolden)
    # ⑥ 释放（与申请逆序）
    aclrtFree(A/B/C/workspace); aclrtDestroyStream; aclrtResetDevice; aclrtFinalize
```

注意两个对称性：**显存的申请顺序与释放顺序相反**（先申请的最后释放，workspace 在 A/B/C 之后申请、最先释放）；**初始化与关闭成对**（`aclInit↔aclFinalize`、`aclrtSetDevice↔aclrtResetDevice`、`aclrtCreateStream↔aclrtDestroyStream`）。

#### 4.1.3 源码精读

入口 `main` 极其简单：解析命令行，失败返回 -1，成功调用 `Run`。见 [examples/00_basic_matmul/basic_matmul.cpp:143-151](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L143-L151)。

`Run()` 的六阶段在源码里对应的行区间如下：

| 阶段 | 大致行号 | 关键调用 |
| --- | --- | --- |
| ① 初始化 | [L38-42](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L38-L42) | `aclInit / aclrtSetDevice / aclrtCreateStream` |
| ② 分配 | [L67-81](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L67-L81) | `std::vector` + `aclrtMalloc`（A/B/C） |
| ③ 拷贝 | [L69-70](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L69-L70) 与 [L74,L78](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L74-L78) | `FillRandomData` + `aclrtMemcpy`（H2D） |
| ④ 执行 | [L86-116](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86-L116) | 组装 + `CanImplement/GetWorkspaceSize/Initialize/operator()/Sync` |
| ⑤ 对比 | [L121-132](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L121-L132) | D2H + `ComputeMatmul` + `CompareData` |
| ⑥ 释放 | [L117-119](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L117-L119) 与 [L134-140](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L134-L140) | `aclrtFree` + `aclrtDestroyStream/ResetDevice/Finalize` |

#### 4.1.4 代码实践

**实践目标**：在源码里把六个阶段亲手标出来，建立「读任何 CATLASS 样例 Host 代码」的通用框架。

**操作步骤**：

1. 打开 `examples/00_basic_matmul/basic_matmul.cpp`，定位到 `Run()` 函数（[L36](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L36)）。
2. 用注释或纸笔，在代码左侧依次标注 `// ① 初始化`、`// ② 分配`、`// ③ 拷贝`、`// ④ 执行`、`// ⑤ 对比`、`// ⑥ 释放`，行号参考上表。
3. 注意阶段④里「组装类型别名」（L86-105）和「真正调用算子」（L106-116）是两小段，前者只是 `using` 声明、不产生运行时调用，后者才有实际的 ACL/device 交互。

**需要观察的现象**：标注完成后，你会看到 Host 代码严格按「①→②→③→④→⑤→⑥」线性推进，没有任何跳跃；而阶段⑥的释放恰好是阶段①②申请资源的逆序回收。

**预期结果**：得到一张行号—阶段对照表，与 4.1.3 的表格一致。

**待本地验证**：无（纯阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：如果把阶段⑥（释放）整段删掉，程序还能正确算出结果吗？会有什么问题？

> **答案**：算的结果依然正确（⑤对比已打印），但会**泄漏显存和运行时资源**——`aclrtFree` 没调用则 GM 显存不归还，`aclrtResetDevice`/`aclFinalize` 没调用则 ACL 运行时不正常关闭。短跑一次看不出来，长期或反复调用会耗尽显存。

**练习 2**：阶段④里 `CanImplement`、`GetWorkspaceSize`、`Initialize`、`operator()` 四步的顺序能调换吗？

> **答案**：不能。`GetWorkspaceSize` 要先于 `Initialize`（后者需要把 workspace 指针传进去），`Initialize` 要先于 `operator()`（前者把 `Arguments` 转成 Kernel 能用的 `Params` 并完成 tiling 等准备）。`CanImplement` 是前置校验，放最前。这是 Device 层适配器的固定协议，u2-l3 会展开。

---

### 4.2 ACL 运行时初始化（最小模块 1）

#### 4.2.1 概念说明

「初始化」阶段做三件事，对应 ACL 运行时的三层概念：

- **`aclInit(nullptr)`**：初始化 ACL 运行时本身。整个进程只需调用一次，传 `nullptr` 表示用默认配置文件。它是「打开电源」。
- **`aclrtSetDevice(deviceId)`**：选定一块 NPU 卡（设备号来自命令行，默认 0）。它是「插上某一号卡」。
- **`aclrtCreateStream(&stream)`**：在选定设备上创建一条流（stream）。流是「命令队列」——你把算子丢到流上，设备就按顺序执行。它是「拉一条排队的传送带」。

这三层是**包含关系**：运行时 ⊃ 设备 ⊃ 流。后面所有 `aclrt*` 调用都隐式作用在「当前 set 的设备」上，算子都丢到「这条 stream」上。

#### 4.2.2 核心流程

```
aclInit()                    # 一次性的运行时初始化
aclrtSetDevice(deviceId)     # 选卡
aclrtCreateStream(&stream)   # 建流
... 全部算子与搬运都用这条 stream ...
aclrtSynchronizeStream(stream)  # 阻塞等流跑完
aclrtDestroyStream(stream)   # 销毁流
aclrtResetDevice(deviceId)   # 复位卡
aclrtFinalize()              # 关闭运行时
```

这里有一个**容易踩坑的细节**：本样例用的错误检查宏 `ACL_CHECK` **只打印、不中断**。下一节源码精读会展开。

#### 4.2.3 源码精读

初始化三连见 [basic_matmul.cpp:40-42](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L40-L42)：

```cpp
ACL_CHECK(aclInit(nullptr));
ACL_CHECK(aclrtSetDevice(options.deviceId));
ACL_CHECK(aclrtCreateStream(&stream));
```

其中 `deviceId` 来自 `options`，而 `options` 由 `main` 里 `GemmOptions::Parse` 解析命令行得到——`./00_basic_matmul 256 512 1024 0` 的最后一个 `0` 就是它。`Parse` 的实现在 [examples/common/options.hpp:42-66](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/options.hpp#L42-L66)：它校验参数个数（3 或 4 个，device 可选），用 `std::atoi` 读 m/n/k（及可选 deviceId），失败返回 -1。

收尾的三连见 [basic_matmul.cpp:138-140](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L138-L140)：`aclrtDestroyStream` → `aclrtResetDevice` → `aclrtFinalize`，正好与初始化反向配对。

**重点看 `ACL_CHECK` 宏**，定义在 [examples/common/helper.hpp:32-38](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/helper.hpp#L32-L38)：

```cpp
#define ACL_CHECK(status)                                                                   \
    do {                                                                                    \
        aclError error = status;                                                            \
        if (error != ACL_ERROR_NONE) {                                                      \
            std::cerr << __FILE__ << ":" << __LINE__ << " aclError:" << error << std::endl; \
        }                                                                                   \
    } while (0)
```

关键点：宏体里 `if (error != ACL_ERROR_NONE)` 分支**只做了一件事——往 `std::cerr` 打印错误码**，没有 `return`、没有 `exit`、没有抛异常。也就是说，如果某个 ACL 调用失败，`ACL_CHECK` 打印一行错误后，**程序会继续往下跑**。这是「样例代码」的简化处理（方便演示流程），生产代码通常会改成失败即返回。读源码时要意识到这一点：看到满屏 `ACL_CHECK` 不要误以为出错就会停。

> 补充：`helper.hpp` 还提供了一个更省事的模板函数 `RunAdapter`（[L73-89](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/helper.hpp#L73-L89)），它把阶段④的「查 workspace→申请→Initialize→执行→Sync→释放 workspace」打包成一个调用。`00_basic_matmul` 为了把每步讲清楚、选择了手写，后续很多样例会直接用 `RunAdapter`。

#### 4.2.4 代码实践

**实践目标**：确认初始化与关闭的成对关系，并亲眼看到 `ACL_CHECK` 的「不中断」行为。

**操作步骤**：

1. 在 [basic_matmul.cpp:40](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L40) 的 `aclInit` 调用点，数清楚它和文件末尾 [L140](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L140) 的 `aclFinalize` 是不是同一作用域里唯一的成对调用。
2. 做一个思想实验：如果故意把 `aclrtSetDevice(options.deviceId)` 里的 `deviceId` 改成一个不存在的卡号（比如 `99`），按 `ACL_CHECK` 的实现，程序会停在哪儿？

**需要观察的现象**（思想实验）：`aclrtSetDevice` 返回错误码，`ACL_CHECK` 向 `stderr` 打印一行 `aclError:...`，然后**继续执行**后续的 `aclrtCreateStream`、`aclrtMalloc`……一连串都会接着报错打印，但程序不会主动退出，最终走到对比阶段、可能打印 `Compare failed`。

**预期结果**：理解「ACL_CHECK 只记录不阻断」，从而明白为什么样例里不靠它做错误控制流。

**待本地验证**：上述错误行为需在真实环境或 `--simulator` 仿真下运行才能看到实际打印（本讲不强制执行）。

#### 4.2.5 小练习与答案

**练习 1**：`aclInit` 在一个进程里可以调用多次吗？

> **答案**：不应多次调用。`aclInit` 负责运行时的一次性初始化，通常整个进程开始时调用一次，与进程结束时的一次 `aclFinalize` 配对。重复初始化属于误用。

**练习 2**：为什么 `aclrtCreateStream` 的参数是 `&stream`（指针的指针/引用），而 `aclrtSetDevice` 的参数是值？

> **答案**：`aclrtSetDevice(deviceId)` 是「设定」一个已有值（设备号），传值即可；`aclrtCreateStream(&stream)` 是「创建并返回」一个新对象（流句柄），需要把 `stream` 的地址传进去，让函数内部把创建好的句柄写回调用者的变量，所以用输出参数（`aclrtStream*`）。

---

### 4.3 GM 显存分配与拷贝（最小模块 2）

#### 4.3.1 概念说明

这个阶段解决「数据从哪来、放哪去」。要做三件事：

1. **算大小**：根据命令行给的 m/n/k，算出 A、B、C 三个矩阵各有多少元素、占多少字节。
2. **造输入数据**：在 Host 上用随机数填满 A、B（C 是纯输出，不用填）。
3. **申请 Device 显存并搬运**：在 GM 给 A/B/C 各 `aclrtMalloc` 一块，再把 Host 的 A、B `aclrtMemcpy`（H2D）搬过去。C 只申请、不搬入（因为算的是 \(C=A\cdot B\)，没有 \(\beta C\) 项，C 不需要初值）。

这里的数学很直接：

- A 形状 \((m,k)\)，元素数 \(m\cdot k\)，字节数 \(m\cdot k\cdot \text{sizeof}(ElementA)\)。
- B 形状 \((k,n)\)，元素数 \(k\cdot n\)。
- C 形状 \((m,n)\)，元素数 \(m\cdot n\)。

#### 4.3.2 核心流程

```
m,n,k = options.problemShape 的三个分量
lenA = m*k; lenB = k*n; lenC = m*n
sizeA = lenA * sizeof(half); ...

# Host 缓冲 + 随机填充
hostA = vector<fp16_t>(lenA);  hostB = vector<fp16_t>(lenB)
FillRandomData(hostA, -5.0, 5.0);  FillRandomData(hostB, -5.0, 5.0)

# Device 显存
aclrtMalloc(deviceA, sizeA, HUGE_FIRST);  aclrtMemcpy(deviceA <- hostA, H2D)
aclrtMalloc(deviceB, sizeB, HUGE_FIRST);  aclrtMemcpy(deviceB <- hostB, H2D)
aclrtMalloc(deviceC, sizeC, HUGE_FIRST)   # 仅申请，不拷入
```

随机数的范围由 `FillRandomData` 的两个参数 `low`/`high` 决定；本样例默认 `[-5.0, 5.0]`。

#### 4.3.3 源码精读

先看尺寸计算，[basic_matmul.cpp:44-58](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L44-L58)。`problemShape` 是个 `GemmCoord`，用 `.m()/.n()/.k()` 取三个维度；元素数和字节数按上面公式算：

```cpp
uint32_t m = options.problemShape.m();
uint32_t n = options.problemShape.n();
uint32_t k = options.problemShape.k();
...
size_t sizeA = lenA * sizeof(ElementA);   // ElementA = half → 每元素 2 字节
```

再看 Host 缓冲与随机填充，[basic_matmul.cpp:67-70](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L67-L70)：

```cpp
std::vector<fp16_t> hostA(lenA);
std::vector<fp16_t> hostB(lenB);
golden::FillRandomData<fp16_t>(hostA, -5.0f, 5.0f);
golden::FillRandomData<fp16_t>(hostB, -5.0f, 5.0f);
```

`FillRandomData` 的实现在 [examples/common/golden/fill_data.hpp:32-40](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/fill_data.hpp#L32-L40)，逐元素把 `rand()/RAND_MAX` 缩放到 \([low, high]\)：

```cpp
ElementRandom randomValue =
    low + (static_cast<ElementRandom>(rand()) / static_cast<ElementRandom>(RAND_MAX)) * (high - low);
data[i] = static_cast<Element>(randomValue);
```

> 小知识：因为用的是标准库 `rand()` 而没有 `srand` 种子，每次运行序列是**确定**的（默认种子），所以同一组 m/n/k 反复跑、输入数据其实一样——这有利于复现问题。`int8_t` 另有一个特化版本（[L42-49](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/fill_data.hpp#L42-L49)），用整数 `rand()%(high-low+1)` 避免浮点取整问题。

然后是 Device 显存申请与搬运，[basic_matmul.cpp:72-81](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L72-L81)。A、B 的模式完全一样——先 `aclrtMalloc` 再 `aclrtMemcpy`：

```cpp
uint8_t* deviceA{nullptr};
ACL_CHECK(aclrtMalloc(reinterpret_cast<void**>(&deviceA), sizeA, ACL_MEM_MALLOC_HUGE_FIRST));
ACL_CHECK(aclrtMemcpy(deviceA, sizeA, hostA.data(), sizeA, ACL_MEMCPY_HOST_TO_DEVICE));
```

- `aclrtMalloc(pptr, size, policy)`：在 GM 申请 `size` 字节，句柄写回 `deviceA`。`ACL_MEM_MALLOC_HUGE_FIRST` 是分配策略，表示**优先用大页（huge page）**，对大块连续显存更友好。
- `aclrtMemcpy(dst, dstMax, src, count, kind)`：搬 `count` 字节。`kind=ACL_MEMCPY_HOST_TO_DEVICE` 即 H2D。注意目标在前（`deviceA`）、源在后（`hostA.data()`）。

C 则**只申请、不拷入**（[L80-81](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L80-L81)），呼应 README 给的公式 \(C=A\cdot B\)——没有 \(\beta C\) 项，C 无需初值。

> 关于 `layout`：本阶段还构造了 `layoutA/layoutB/layoutC`（[L60-65](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L60-L65)），它们在本样例里**主要供后面的 golden 计算用**（golden 需要按布局算偏移）。`RowMajor` 的 `GetOffset((row,col))` 实现见 [include/catlass/layout/matrix.hpp:80-83](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L80-L83)，即 `row*stride[0] + col`，而 `stride[0]` 对行优先就是列数 `cols`（构造函数 [L44-46](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L44-L46)）。布局的深入拆解留给 u3-l1。

#### 4.3.4 代码实践

**实践目标**：亲手改输入数据范围，观察它对结果的影响（或不影响），从而理解随机输入只是测试手段。

**操作步骤**：

1. 打开 [basic_matmul.cpp:69-70](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L69-L70)，把两处 `FillRandomData<fp16_t>(..., -5.0f, 5.0f)` 的参数改为 `FillRandomData<fp16_t>(..., -1.0f, 1.0f)`。
2. 重新编译该样例（`bash scripts/build.sh 00_basic_matmul`），运行 `./00_basic_matmul 256 512 1024 0`。

**需要观察的现象**：仍然输出 `Compare success.`。如果想验证数据范围真的变了，可以在 `FillRandomData` 之后加一行打印（示例代码，非项目原有）：

```cpp
// 示例代码：仅用于观察，验证后请删除
std::cout << "hostA[0]=" << hostA[0] << " max|hostA|≈<5(改后<1)" << std::endl;
```

**预期结果**：改范围不影响「是否通过」——因为 golden 真值是用**同样的** hostA/hostB 重算的，输入变了、真值也跟着变，相对误差仍在阈值内。这正说明精度对比是「NPU 输出 vs 同输入 CPU 真值」，与具体数值范围无关。

**待本地验证**：编译运行的输出需在 CANN 环境或 `--simulator` 下确认（本讲不强制执行）。

#### 4.3.5 小练习与答案

**练习 1**：`sizeA = lenA * sizeof(ElementA)`。若 `m=256, k=512`，`ElementA=half`，`sizeA` 是多少字节？

> **答案**：`lenA = 256*512 = 131072` 个元素；`sizeof(half)=2` 字节；`sizeA = 131072*2 = 262144` 字节 = 256 KiB。

**练习 2**：为什么 A、B 都做了 `aclrtMemcpy`（H2D），而 C 只 `aclrtMalloc` 不拷入？

> **答案**：因为本算子是 \(C=A\cdot B\)，C 是纯输出、不参与输入（无 \(\beta C\) 项），不需要初值，所以只申请显存、不搬入。若算子变成 \(C=\alpha A\cdot B+\beta C\)（带残差），就需要先把 C 的初值 H2D 搬进去。

**练习 3**：`ACL_MEM_MALLOC_HUGE_FIRST` 这个策略名里「HUGE」指什么？

> **答案**：指大页（huge page）。NPU 的 GM 分配可以走普通页或大页；优先大页能减少地址翻译开销、提升大块连续显存的访问效率，所以样例默认选 `HUGE_FIRST`。

---

### 4.4 Golden 真值与精度对比（最小模块 3）

#### 4.4.1 概念说明

算子跑完后，Device GM 的 `deviceC` 里是 NPU 用 fp16 算出的结果。要判断它对不对，本样例走「CPU 高精度真值 + 相对误差阈值」这条路：

1. **D2H 取回**：把 `deviceC` 搬回 Host 的 `hostC`（fp16）。
2. **CPU 算真值**：用 **`float`** 精度，在 CPU 上按 \(C_{i,j}=\sum_k A_{i,k}B_{k,j}\) 重算一遍，存入 `hostGolden`（`vector<float>`）。
3. **逐元素比对**：对每个元素算相对误差，超过阈值则记一个错误下标；全过则打印 `Compare success.`。

为什么 golden 用 `float` 而不是 `fp16`？因为 CPU 这边要扮演「更高精度的裁判」——用 `float` 累加能避免 fp16 累加的额外舍入，得到更接近「真值」的参考，从而暴露 NPU fp16 计算的固有误差。

#### 4.4.2 核心流程

相对误差判据（对应 `CompareData` 主模板）：

\[
\text{diff}_i = |\,\text{actual}_i-\text{expect}_i\,|,\qquad
\text{通过当且仅当}\quad \text{diff}_i \le \text{rtol}\cdot \max(1.0,\,|\text{expect}_i|)
\]

其中阈值 `rtol` 由「计算规模」二选一：

\[
\text{rtol}=\begin{cases} 1/256 & \text{computeNum} < 2048 \\ 1/128 & \text{computeNum} \ge 2048 \end{cases}
\]

注意一个细节：调用处传的第三个参数是 `k`（[basic_matmul.cpp:127](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L127)），所以这里的 `computeNum` 取的是 **K 维**，不是总元素数。规模越大（累加步数越多），误差累积越大，阈值就放宽一档（`1/128` 比 `1/256` 宽松）。

#### 4.4.3 源码精读

取回与真值计算，[basic_matmul.cpp:121-125](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L121-L125)：

```cpp
std::vector<fp16_t> hostC(lenC);
ACL_CHECK(aclrtMemcpy(hostC.data(), sizeC, deviceC, sizeC, ACL_MEMCPY_DEVICE_TO_HOST));

std::vector<float> hostGolden(lenC);
golden::ComputeMatmul(options.problemShape, hostA, layoutA, hostB, layoutB, hostGolden, layoutC);
```

`aclrtMemcpy` 这次的方向是 `ACL_MEMCPY_DEVICE_TO_HOST`（D2H），且**源在前（`deviceC`）、目标在后（`hostC.data()`）**——方向不同，参数顺序也变，读代码时要留意。

`ComputeMatmul` 是朴素三重循环，见 [examples/common/golden/matmul.hpp:24-47](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/matmul.hpp#L24-L47)，核心循环体（[L30-46](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/matmul.hpp#L30-L46)）：

```cpp
for (uint32_t i ...) for (uint32_t j ...) {
    ElementGolden accumulator = 0;                 // ElementGolden = float
    for (uint32_t kk = 0; kk < problemShape.k(); ++kk) {
        offsetA = layoutA.GetOffset(MakeCoord(i, kk));
        offsetB = layoutB.GetOffset(MakeCoord(kk, j));
        accumulator += static_cast<ElementGolden>(dataA[offsetA]) * static_cast<ElementGolden>(dataB[offsetB]);
    }
    dataGolden[layoutGolden.GetOffset(MakeCoord(i, j))] = accumulator;
}
```

注意 `dataA/dataB`（fp16）都被 `static_cast` 成 `ElementGolden`（float）后再相乘累加——这正是「高精度裁判」的体现。每个元素的偏移都通过 `Layout::GetOffset` 计算，所以 A/B/C 用什么 `Layout`（行优先/列优先）会影响取数下标、但不会改变数学结果。

最后是逐元素比对，[basic_matmul.cpp:127-132](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L127-L132)：

```cpp
std::vector<uint64_t> errorIndices = golden::CompareData(hostC, hostGolden, k);
if (errorIndices.empty()) {
    std::cout << "Compare success." << std::endl;
} else {
    std::cerr << "Compare failed. Error count: " << errorIndices.size() << std::endl;
}
```

`CompareData` 主模板见 [examples/common/golden/compare_data.hpp:90-109](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/compare_data.hpp#L90-L109)，阈值常量与判据在 [L94-107](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/compare_data.hpp#L94-L107)：

```cpp
const uint32_t computeNumThreshold = 2048;
const float rtolGeneral = 1.0f / 256;
const float rtolOverThreshold = 1.0f / 128;
float rtol = computeNum < computeNumThreshold ? rtolGeneral : rtolOverThreshold;
...
ElementCompare diff = std::fabs(actualValue - expectValue);
if (diff > rtol * std::max(1.0f, std::fabs(expectValue))) {
    errorIndices.push_back(i);
}
```

模板被推导为 `CompareData<fp16_t, float>`：`result=hostC(fp16)`、`expect=hostGolden(float)`，比较时把 `result[i]` 提升成 `float` 再算差。返回的是「错误元素下标列表」，空即通过。

> 补充：同一文件里还有更严格的 `ComputeErrorMetrics`（[L30-88](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/compare_data.hpp#L30-L88)），它会同时算 NPU 与「同精度 CPU」相对「高精度 golden」的误差比（MARE/MERE/RMSE），用于更专业的精度评估；`00_basic_matmul` 用的是轻量的 `CompareData`。

#### 4.4.4 代码实践

**实践目标**：根据命令行参数预测本次运行用的误差阈值，把「公式→代码→命令行」串起来。

**操作步骤**：

1. 默认命令 `./00_basic_matmul 256 512 1024 0`，对照 [compare_data.hpp:94-98](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/golden/compare_data.hpp#L94-L98) 与调用处传入的 `k`（[basic_matmul.cpp:127](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L127)），回答：`computeNum` 是多少？`rtol` 取 `1/256` 还是 `1/128`？
2. 再假设运行 `./00_basic_matmul 256 512 4096 0`（k=4096），重新判断 `rtol`。

**需要观察的现象**：阈值只跟 K 维有关，与 m、n 无关。

**预期结果**：
- k=1024 < 2048 → `rtol = 1/256`。
- k=4096 ≥ 2048 → `rtol = 1/128`（更宽松，因为累加 4096 次误差更大）。

**待本地验证**：可在 `CompareData` 返回后打印 `errorIndices.size()`（示例代码：`std::cout << "errors=" << errorIndices.size() << "\n";`）观察错误个数，正常应为 0。

#### 4.4.5 小练习与答案

**练习 1**：`hostGolden` 为什么声明成 `vector<float>` 而不是 `vector<fp16_t>`？

> **答案**：为了用更高精度做裁判。float 的累加舍入误差远小于 fp16，作为「真值」能更准确地暴露 NPU 端 fp16 计算的误差；若 golden 也用 fp16，裁判本身就带较大误差，就难以判断是 NPU 算错还是裁判不准。

**练习 2**：判据里 `max(1.0, |expect|)` 这个 `1.0` 起什么作用？

> **答案**：当真值 `expect` 接近 0 时，纯相对误差 `diff/|expect|` 会趋向无穷大、误判失败。用 `max(1.0, |expect|)` 把分母兜底为至少 1，等价于「真值小时退化为绝对误差 ≤ rtol」，避免对接近零的元素过度严苛。

**练习 3**：`aclrtMemcpy` 在 D2H 和 H2D 两个方向上，`dst/src` 参数顺序一样吗？

> **答案**：函数签名都是 `aclrtMemcpy(dst, dstMax, src, count, kind)`，参数顺序不变，**始终是目标在前、源在后**。变的是 `kind`（H2D 用 `ACL_MEMCPY_HOST_TO_DEVICE`，D2H 用 `ACL_MEMCPY_DEVICE_TO_HOST`）以及谁当 dst、谁当 src：H2D 时 dst=deviceA、src=hostA；D2H 时 dst=hostC、src=deviceC。

---

## 5. 综合实践

把三个模块串起来，做一次「端到端跟踪 + 修改验证」的小任务。

**任务**：选定一个小规模形状（例如 `./00_basic_matmul 64 64 64 0`），完成下面四步：

1. **算尺寸**：手算 `lenA/lenB/lenC` 与 `sizeA/sizeB/sizeC`（对应 4.3）。
2. **定阈值**：由 k=64 推断本次 `CompareData` 的 `rtol`（对应 4.4）。
3. **改范围**：把输入随机数据范围从 `[-5,5]` 改成 `[-1,1]`（对应 4.3 实践），重新编译运行。
4. **加观察**：在对比之后用一行示例打印输出 `errorIndices.size()`，确认仍为 0、得到 `Compare success.`。

**完成判据**：
- 尺寸：`lenA=lenB=lenC=4096`，`sizeA=sizeB=sizeC=8192` 字节。
- 阈值：k=64 < 2048 → `rtol=1/256`。
- 改范围后仍 `Compare success.`（因为 golden 用同输入重算，精度与数值范围无关）。
- 这四步分别落在阶段②③⑤，能清楚指认每一行代码。

> 这个综合实践要求改一行源码并重新编译运行；若手头没有 NPU，可加 `--simulator` 仿真构建（见 [u1-l4](u1-l4-build-and-run.md)）。改动属于本地实验，验证后请还原，避免污染样例。

## 6. 本讲小结

- 一段 CATLASS 样例的 Host 代码可切成「初始化—分配—拷贝—执行—对比—释放」六阶段，`basic_matmul.cpp` 的 `Run()` 严格按此线性推进，释放与申请逆序、初始化与关闭成对。
- 初始化三连 `aclInit → aclrtSetDevice → aclrtCreateStream` 建立起「运行时—设备—流」三层；`ACL_CHECK` 宏**只打印不中断**，是样例的简化处理，读代码时不要误以为它会拦截错误。
- 数据准备走 `尺寸计算 → FillRandomData 造随机输入 → aclrtMalloc 申请 GM → aclrtMemcpy(H2D) 搬入`；输出 C 是纯输出、只申请不搬入，呼应 \(C=A\cdot B\)。
- 精度验证走 `aclrtMemcpy(D2H) 取回 → ComputeMatmul 用 float 算真值 → CompareData 逐元素比相对误差`；阈值 `rtol` 取 `1/256` 或 `1/128`，由 K 维是否 ≥ 2048 决定。
- `golden.hpp` 是聚合头，背后三个子头分工明确：`fill_data.hpp` 造数据、`matmul.hpp` 算真值、`compare_data.hpp` 比精度——这套「造—算—比」是所有 CATLASS 样例精度验证的通用骨架。
- 本讲全程停留在 Host 侧；阶段④里那串 `using ... BlockMmad/BasicMatmul/DeviceGemm` 类型别名只是「指认」，它们的内部机制是下一讲 [u2-l2（四层组装范式总览）](u2-l2-four-layer-assembly.md) 的主题。

## 7. 下一步学习建议

接下来建议按顺序阅读：

- **u2-l2 四层组装范式总览**：把本讲里「没展开」的 `BlockMmad → BlockEpilogue → BlockScheduler → BasicMatmul → DeviceGemm` 五步组装讲透，串起 Host 到 Kernel 的完整调用链。
- **u2-l3 Device 层适配器 DeviceGemm**：本讲阶段④用到的 `CanImplement/GetWorkspaceSize/Initialize/operator()` 四个接口的内部实现。
- 如果你对 `Layout::GetOffset`、`RowMajor/ColumnMajor` 的物理排布还好奇，可以提前跳到 **u3-l1 Layout 布局抽象**。

另外，建议自行浏览 `examples/common/golden/compare_data.hpp` 里 `CompareDataBfloat16` 与 `ComputeErrorMetrics` 两个函数，对比它们与本讲所用 `CompareData` 在阈值与判据上的差异——这是理解 CATLASS 精度体系的好练习。
