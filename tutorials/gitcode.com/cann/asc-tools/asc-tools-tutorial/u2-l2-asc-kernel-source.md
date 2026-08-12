# Ascend C 算子源码与 .asc 核函数结构

## 1. 本讲目标

本讲承接 u2-l1（CPU Debug 工作原理）。在上一讲里，我们知道了「同一份 Ascend C 源码能在 CPU 域与 NPU 域运行」，并且核函数启动语法 `<<<>>>` 会在 CPU 模式被改写为对 `AscCPUKernelLaunch` 的调用。

但是，**一份 Ascend C 算子源码到底长什么样？** 这才是我们动手调试和读懂源码的前提。

本讲以 asc-tools 自带的 `add.asc` 样例为唯一教材，逐行拆解一个完整的算子实现。学完本讲，你应当能够：

- 读懂一个完整的 Ascend C 算子实现，说清楚「Kernel 类、核函数、宿主调用」三层各自的职责。
- 理解 `CopyIn / Compute / CopyOut` 三段式搬运计算流程，以及它为何要拆成「块（block）」和「分片（tile）」。
- 掌握 `TQue / LocalTensor / GlobalTensor` 这些数据类型如何配合 `AllocTensor / EnQue / DeQue / FreeTensor` 管理 Local Memory 生命周期。
- 看懂 `__global__` 核函数的签名，以及 `add_custom<<<numBlocks, nullptr, stream>>>(...)` 这一行的启动写法。

> 本讲只讲「源码结构」，不深入多核 fork 仿真（u3-l1）和 API 校验（u4）。这些机制会在后续讲义展开。

---

## 2. 前置知识

在进入源码之前，先用通俗语言解释几个本讲用得上的概念。

**为什么 NPU 算子要写成「搬运 → 计算 → 搬运」？**
NPU 的 AI Core 内部有一种离计算单元最近的高速存储，叫 **Unified Buffer（UB，统一缓冲区）**。向量计算单元（Vector）只能直接读写 UB，不能直接读写片外大内存。而算子的输入输出通常放在片外的 **Global Memory（GM，对应设备侧显存）**。所以一次计算必然是：先把数据从 GM 搬到 UB，在 UB 里算，再把结果从 UB 搬回 GM。这就是三段式的硬件根源。

**什么是 block（块）和 tile（分片）？**
- 一个核函数可以启动多个 **block**（核），每个 block 跑同一份代码、处理数据的不同切片。`add.asc` 里启动了 8 个 block（`NUM_BLOCKS = 8`），把总量 16384 个元素平均分成 8 份，每份 2048 个。
- 一个 block 拿到 2048 个元素后，并不会一次全搬进 UB（UB 容量有限），而是再切成更小的 **tile** 分批搬运计算。这就是 `TILE_NUM` 的作用。

**什么是队列（TQue）和双缓冲（double buffer）？**
`TQue` 是 Ascend C 提供的队列容器，用来管理 UB 里的一块连续空间。给它一个 `BUFFER_NUM`（本例是 2），就相当于把这块空间切成 2 份交替使用：当 Vector 正在算第 i 份时，搬运单元可以同时把第 i+1 份搬进来。这就是 **软件流水线（pipeline）**，能让搬运和计算重叠，提高硬件利用率。CPU Debug 模式下，这种流水被仿真出来，结构上和 NPU 完全一致。

**几个源码里反复出现的 Ascend C 关键字：**

| 关键字 / 类型 | 含义 |
|---|---|
| `__aicore__` | 标注「这段代码运行在 AI Core（核侧）」，是 kernel 类方法的修饰符。 |
| `__gm__` | 地址空间修饰符，说明指针指向 Global Memory。 |
| `GM_ADDR` | 核函数参数里「Global Memory 地址」的统一类型（一个宏）。 |
| `__global__` | 标注「这是核函数入口」，可被宿主侧（CPU）调用启动。 |
| `__vector__` | 标注该核函数跑在 Vector 核上（与 Cube 核相对）。 |

---

## 3. 本讲源码地图

本讲只涉及两个文件，外加一份构建脚本辅助理解编译入口：

| 文件 | 作用 |
|---|---|
| [examples/02_cpudebug/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc) | **唯一的源码教材**。一个文件里同时包含：Kernel 类、`__global__` 核函数、宿主侧调用（ACL）、结果校验、`main` 函数。 |
| [examples/02_cpudebug/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/README.md) | 样例说明：支持的产品、输入输出规格、编译运行命令。 |
| [examples/02_cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/CMakeLists.txt) | 构建脚本，揭示 `.asc` 是如何被当成一种「语言」编译成可执行文件 `add` 的。 |

> 注意：文件名后缀是 `.asc`，但文件头注释写的是 `\file add.cpp`（[add.asc:12](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L12)）。`.asc` 表示这是一份 **Ascend C 源码**，ASC 编译器（bisheng）会先把其中的核函数转义，再和普通 C++ 一起编译。这正是 u2-l1 讲过的「编译期 lowering」。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 Kernel 类结构**、**4.2 三段式搬运计算**、**4.3 队列与 Tensor 类型**。

### 4.1 Kernel 类结构

#### 4.1.1 概念说明

一个 Ascend C 算子的核侧逻辑，通常封装成一个 **Kernel 类**。这么做的好处是：把「算子的状态（输入输出 Tensor、队列、流水线）」和「算子的行为（初始化、计算步骤）」打包在一起，结构清晰、便于复用。

`add.asc` 把这个类命名为 `KernelAdd`。它的对外接口只有三个：构造、`Init`、`Process`。真正的「业务逻辑」全在 `Process` 里驱动。

> 为什么用类而不是几个自由函数？因为每个 block 都要持有自己的 GM 视图、自己的队列和 `pipe`，这些是「状态」，用类成员天然贴合「一个 block = 一个实例」的执行模型。

#### 4.1.2 核心流程

一个 `KernelAdd` 实例的生命周期如下：

```
KernelAdd op;        // 1. 构造（空实现）
op.Init(x, y, z);    // 2. 绑定 GM 视图 + 申请 UB 队列
op.Process();        // 3. 循环执行 CopyIn/Compute/CopyOut
                     //    （析构由 pipe 统一管理）
```

这三步由核函数 `add_custom` 顺序调用，而 `add_custom` 又由宿主侧的 `kernel_add` 通过 `<<<>>>` 启动。完整的「宿主 → 核函数 → Kernel 类」调用链是：

```
main()  →  kernel_add()  →  add_custom<<<numBlocks>>>(x,y,z)  →  KernelAdd::Init/Process
（CPU）       （CPU）             （每 block 一个核）              （核侧）
```

#### 4.1.3 源码精读

先看类骨架，重点是三处：成员变量、`Init`、构造函数。

**成员变量（算子的「状态」）**[add.asc:81-87](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L81-L87)：

```cpp
AscendC::TPipe pipe;
AscendC::TQue<AscendC::TPosition::VECIN,  BUFFER_NUM> inQueueX, inQueueY;
AscendC::TQue<AscendC::TPosition::VECOUT, BUFFER_NUM> outQueueZ;
AscendC::GlobalTensor<float> xGm;
AscendC::GlobalTensor<float> yGm;
AscendC::GlobalTensor<float> zGm;
```

- `pipe` 是流水线管理对象，所有队列内存都通过它申请。
- 两个输入队列 `inQueueX/inQueueY` 位置是 `VECIN`（数据搬进来的方向）；输出队列 `outQueueZ` 位置是 `VECOUT`（数据搬出去的方向）。
- 三个 `GlobalTensor` 是对 GM 上输入输出数据的「视图」（只描述起始地址和长度，不真正持有一份 GM）。

**`Init`：把 GM 切片 + 申请 UB**[add.asc:35-43](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L35-L43)：

```cpp
xGm.SetGlobalBuffer((__gm__ float*)x + BLOCK_LENGTH * AscendC::GetBlockIdx(), BLOCK_LENGTH);
...
pipe.InitBuffer(inQueueX, BUFFER_NUM, TILE_LENGTH * sizeof(float));
```

关键是 `GetBlockIdx()`：每个 block 拿到的不是完整的 GM，而是从 `BLOCK_LENGTH * 本 block 编号` 开始的、长度为 `BLOCK_LENGTH` 的一段。8 个 block 各取 2048 个，正好覆盖 16384。`InitBuffer` 则向 `pipe` 申请每个队列的 UB 空间，大小是 `TILE_LENGTH * sizeof(float)`，份数是 `BUFFER_NUM`。

**`__global__` 核函数：Kernel 类的唯一使用者**[add.asc:90-95](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L90-L95)：

```cpp
__global__ __vector__ void add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z) {
    KernelAdd op;
    op.Init(x, y, z);
    op.Process();
}
```

核函数本身极简：实例化一个 `KernelAdd`，调用 `Init` 和 `Process`。注意它没有任何「循环」「分块」逻辑——这些都藏在 `Process` 里。`__global__` 表示它是入口，`__vector__` 表示跑在 Vector 核上。

#### 4.1.4 代码实践（源码阅读型）

**实践目标：** 厘清「宿主 → 核函数 → Kernel 类」三层，确认每层只做自己该做的事。

**操作步骤：**
1. 打开 `add.asc`，定位 `main`（[add.asc:166](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L166)）、`kernel_add`（[add.asc:97](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L97)）、`add_custom`（[add.asc:90](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L90)）。
2. 数一数 `KernelAdd` 有几个成员变量（应是 6 个：1 个 `pipe` + 3 个 `TQue` + 3 个 `GlobalTensor`，见 [add.asc:82-87](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L82-L87)）。

**需要观察的现象 / 预期结果：** 你会发现核函数 `add_custom` 只有 3 行业务代码，所有复杂度都被「下沉」到了 `KernelAdd` 类里。这是 Ascend C 算子的典型分工：核函数负责启动与装配，Kernel 类负责算法。

#### 4.1.5 小练习与答案

**练习 1：** `add_custom` 的三个参数类型都是 `GM_ADDR`。为什么不用 `float*`？
**参考答案：** `GM_ADDR` 是 Ascend C 对「Global Memory 地址」的统一抽象（一个宏）。它在 CPU Debug 模式和 NPU 模式下都能正确表达「设备侧显存地址」，而裸 `float*` 无法体现 `__gm__` 地址空间语义，也难以在两种运行域间复用。

**练习 2：** 如果把 `__global__` 去掉，会发生什么？
**参考答案：** `__global__` 是核函数入口标记，bisheng 编译器据此识别「这个函数要被宿主侧 `<<<>>>` 启动」并做相应转义。去掉后它退化为一个普通核侧函数，`add_custom<<<...>>>` 这种启动语法将无法绑定到它，编译会报错。

---

### 4.2 三段式搬运计算

#### 4.2.1 概念说明

三段式是 Ascend C 向量算子的通用骨架，源自 NPU 硬件的三条独立流水线：

- **CopyIn（MTE2）**：GM → UB，由内存搬入引擎负责。
- **Compute（Vector）**：在 UB 上做向量运算（本例是 `Add`）。
- **CopyOut（MTE3）**：UB → GM，由搬出引擎负责。

三条流水线在硬件上可以并行。Ascend C 用 **队列 + 双缓冲** 让软件层面也能表达这种并行：当 `BUFFER_NUM=2` 时，第 i 次的 Compute 和第 i+1 次的 CopyIn 可以重叠。CPU Debug 仿真保留了这套结构，所以你在 CPU 上看到的行为和 NPU 一致。

#### 4.2.2 核心流程

`Process` 把一个 block 要处理的 `BLOCK_LENGTH` 个元素，拆成 `loopCount` 次循环，每次处理 `TILE_LENGTH` 个：

```
Process():
    loopCount = TILE_NUM * BUFFER_NUM          // 见 add.asc:46
    for i in 0 .. loopCount-1:
        CopyIn(i)    // 搬 x[i], y[i] 进 UB
        Compute(i)   // z[i] = x[i] + y[i]
        CopyOut(i)   // 把 z[i] 搬回 GM
```

几个常量之间的关系是理解本讲的钥匙（数值见 [add.asc:25-30](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L25-L30)）：

| 常量 | 表达式 | 本例取值 |
|---|---|---|
| `TOTAL_LENGTH` | `8 * 2048` | 16384 |
| `NUM_BLOCKS` | 核数 | 8 |
| `BLOCK_LENGTH` | `TOTAL_LENGTH / NUM_BLOCKS` | 2048 |
| `TILE_NUM` | 每 block 的分片数 | 8 |
| `BUFFER_NUM` | 队列双缓冲份数 | 2 |
| `TILE_LENGTH` | `BLOCK_LENGTH / TILE_NUM / BUFFER_NUM` | 128 |

由此得到一个**守恒关系**——一个 block 处理的元素总数恒等于 `BLOCK_LENGTH`：

\[
\text{loopCount} \times \text{TILE\_LENGTH}
= (\text{TILE\_NUM} \times \text{BUFFER\_NUM}) \times \frac{\text{BLOCK\_LENGTH}}{\text{TILE\_NUM} \times \text{BUFFER\_NUM}}
= \text{BLOCK\_LENGTH}
\]

这个等式是后面「综合实践」里改 `TILE_NUM` 仍能通过校验的根本原因。

> 小提示：第 28 行的注释写着「split data into 1 tiles」（[add.asc:28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L28)），但实际值是 8。这是一处**过时注释**——读源码时要信任代码、警惕注释，这也是源码阅读的基本素养。

#### 4.2.3 源码精读

**Process 的循环骨架**[add.asc:44-52](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L44-L52)：

```cpp
int32_t loopCount = TILE_NUM * BUFFER_NUM;
for (int32_t i = 0; i < loopCount; i++) {
    CopyIn(i);
    Compute(i);
    CopyOut(i);
}
```

**CopyIn：GM → UB，然后入队**[add.asc:55-63](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L55-L63)：

```cpp
AscendC::LocalTensor<float> xLocal = inQueueX.AllocTensor<float>();
AscendC::DataCopy(xLocal, xGm[progress * TILE_LENGTH], TILE_LENGTH);
inQueueX.EnQue(xLocal);
```

注意 `xGm[progress * TILE_LENGTH]`：第 i 次循环搬运的是该 block 视图内、偏移 `i * TILE_LENGTH` 的那一段。`progress` 就是循环变量 `i`。

**Compute：出队 → 计算 → 入队 + 释放输入**[add.asc:64-73](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L64-L73)：

```cpp
AscendC::LocalTensor<float> xLocal = inQueueX.DeQue<float>();
AscendC::LocalTensor<float> zLocal = outQueueZ.AllocTensor<float>();
AscendC::Add(zLocal, xLocal, yLocal, TILE_LENGTH);
outQueueZ.EnQue<float>(zLocal);
inQueueX.FreeTensor(xLocal);
```

真正的算法只有一行 `AscendC::Add`，它是一个向量内建函数（在 CPU Debug 下会被绑定到 stub 实现，详见 u3-l3）。

**CopyOut：出队 → 搬回 GM → 释放输出**[add.asc:74-79](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L74-L79)：

```cpp
AscendC::LocalTensor<float> zLocal = outQueueZ.DeQue<float>();
AscendC::DataCopy(zGm[progress * TILE_LENGTH], zLocal, TILE_LENGTH);
outQueueZ.FreeTensor(zLocal);
```

至此一个 tile 的 `搬进 → 计算 → 搬出` 闭环完成。

#### 4.2.4 代码实践（源码阅读 + 推理型）

**实践目标：** 不运行代码，仅凭源码推理「`TILE_NUM` 改成 4 时，循环跑几次、每次搬多少」。

**操作步骤：**
1. 假设把 [add.asc:28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L28) 的 `TILE_NUM` 改为 4。
2. 用本节的表格手动计算新的 `TILE_LENGTH` 和 `loopCount`。
3. 代入守恒等式验证：`loopCount × TILE_LENGTH` 是否仍等于 `BLOCK_LENGTH`（2048）。

**预期结果：** `TILE_LENGTH = 2048/4/2 = 256`，`loopCount = 4*2 = 8`，`8 × 256 = 2048`，守恒关系成立。这说明改动后每个 block 仍然恰好处理 2048 个元素，结果不变。真正运行验证留到第 5 节综合实践。

#### 4.2.5 小练习与答案

**练习 1：** 为什么循环次数是 `TILE_NUM * BUFFER_NUM` 而不是 `TILE_NUM`？
**参考答案：** 因为队列采用了 `BUFFER_NUM=2` 的双缓冲，每个「逻辑 tile」对应队列里的一个缓冲槽；要消费完 `BLOCK_LENGTH` 这么多数据，循环次数必须满足 `loopCount × TILE_LENGTH = BLOCK_LENGTH`。由 `TILE_LENGTH = BLOCK_LENGTH/(TILE_NUM×BUFFER_NUM)` 反推，循环次数就是 `TILE_NUM × BUFFER_NUM`。

**练习 2：** 三段式分别对应 NPU 的哪条硬件流水？
**参考答案：** CopyIn 对应 MTE2（搬入），Compute 对应 Vector（向量计算），CopyOut 对应 MTE3（搬出）。三者硬件独立，配合双缓冲可并行。

---

### 4.3 队列与 Tensor 类型

#### 4.3.1 概念说明

本模块集中讲两类容器：

- **`GlobalTensor<T>`**：对 Global Memory 上一段连续数据的视图。它只记录「起始地址 + 长度」，并不在宿主侧真正分配显存——显存是宿主侧用 ACL API（`aclrtMalloc`）分配好后传进来的。
- **`TQue<TPosition, BUFFER_NUM>`**：管理 UB 上的一块队列内存，配合 `pipe.InitBuffer` 申请。它通过 4 个原语管理 `LocalTensor` 的生命周期。

`LocalTensor<T>` 则是对 UB 里一片内存的句柄，必须从队列里 `AllocTensor` 出来，用完 `FreeTensor` 归还。

#### 4.3.2 核心流程

一个 `LocalTensor` 在队列里的标准生命周期是一个**配对循环**，严格成对出现：

```
输入队列(VECIN):          输出队列(VECOUT):
  AllocTensor  ──┐           AllocTensor ──┐
  DataCopy(GM→UB)│           (Compute 写入) │
  EnQue ─────────┘           EnQue ─────────┘
        ↓  (跨阶段同步)             ↓
  DeQue ─────────┐           DeQue ─────────┐
  (Compute 读取) │           DataCopy(UB→GM)│
  FreeTensor ────┘           FreeTensor ────┘
```

配对口诀：**`AllocTensor ↔ FreeTensor`、`EnQue ↔ DeQue`**。少配一个就会触发 npu check 的 Buffer 错误（这是 u5-l1 的内容，本讲只需记住「必须成对」）。

> 直觉理解：`EnQue` 表示「这块数据我已经搬好/算好，可以给下一阶段用了」；`DeQue` 表示「我要开始用这块数据了」。`EnQue` 之后到 `DeQue` 之前，正是跨流水线同步的窗口——也是双缓冲能并行的关键。

#### 4.3.3 源码精读

**队列声明与 UB 申请**[add.asc:40-42](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L40-L42)（声明见 [add.asc:83-84](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L83-L84)）：

```cpp
pipe.InitBuffer(inQueueX, BUFFER_NUM, TILE_LENGTH * sizeof(float));
pipe.InitBuffer(inQueueY, BUFFER_NUM, TILE_LENGTH * sizeof(float));
pipe.InitBuffer(outQueueZ, BUFFER_NUM, TILE_LENGTH * sizeof(float));
```

三个队列各自申请 `BUFFER_NUM=2` 份、每份 `TILE_LENGTH * 4` 字节的 UB。注意输入输出队列大小一致——本例里搬运和计算的 tile 大小相同。

**输入 Tensor 的完整生命周期**，跨 CopyIn 与 Compute 两个函数：

- 申请 + 搬入 + 入队（[add.asc:57-62](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L57-L62)）：`AllocTensor → DataCopy → EnQue`。
- 出队 + 读取 + 释放（[add.asc:66-72](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L66-L72)）：`DeQue → Add → FreeTensor`。

**输出 Tensor 的完整生命周期**，跨 Compute 与 CopyOut 两个函数：

- 申请 + 计算 + 入队（[add.asc:68-70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L68-L70)）：`AllocTensor → Add → EnQue`。
- 出队 + 搬出 + 释放（[add.asc:76-78](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L76-L78)）：`DeQue → DataCopy → FreeTensor`。

四个原语严格成对，无一处遗漏。

#### 4.3.4 代码实践（源码阅读型）

**实践目标：** 在源码里把一个输入 `LocalTensor`（`xLocal`）从「出生到死亡」的轨迹画出来。

**操作步骤：**
1. 在 [add.asc:57](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L57) 找到 `xLocal` 的 `AllocTensor`（出生）。
2. 在 [add.asc:61](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L61) 找到它的 `EnQue`（交给计算阶段）。
3. 在 [add.asc:66](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L66) 找到它的 `DeQue`（计算阶段取回）。
4. 在 [add.asc:71](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L71) 找到它的 `FreeTensor`（归还）。

**需要观察的现象 / 预期结果：** `xLocal` 的 4 个原语刚好构成 `Alloc → EnQue → DeQue → Free` 的闭环，且分布在两个不同函数（CopyIn、Compute）里。这也解释了为什么三段式要拆成三个函数：队列的「入队端」和「出队端」天然分属不同流水阶段。

#### 4.3.5 小练习与答案

**练习 1：** 如果在 `Compute` 里只写了 `DeQue` 却忘了 `FreeTensor(xLocal)`，会有什么后果？
**参考答案：** UB 配额不会被归还。在双缓冲下，几个循环后队列的空闲缓冲槽就会耗尽，`AllocTensor` 拿不到空间；在 CPU Debug + npu check 下，这会被报告为 `ErrorBuffer` 类错误（缓冲泄漏/未释放）。所以原语必须严格成对。

**练习 2：** `GlobalTensor` 和 `LocalTensor` 的核心区别是什么？
**参考答案：** `GlobalTensor` 指向片外 Global Memory，容量大但访问慢，且不能直接参与向量计算；`LocalTensor` 指向片上 Unified Buffer，是向量计算单元唯一能直接读写的存储。三段式的本质就是在两者之间架桥。

---

## 5. 综合实践

本任务把第 4 节的三个模块串起来：**修改 `TILE_NUM`，重新编译运行，验证结果是否仍通过，并用守恒等式解释原因。**

### 实践目标

亲手验证「分片粒度不影响计算正确性」这一结论，加深对常量关系、循环次数、DataCopy 偏移的理解。

### 操作步骤

1. **准备环境**（若尚未编译安装 asc-tools，请先按 u1-l4 完成）：
   ```bash
   source ${install_path}/cann/set_env.sh
   ```

2. **先跑通原版**，确认基线正确（命令取自 [README.md:67-71](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/README.md#L67-L71)）：
   ```bash
   cd examples/02_cpudebug
   mkdir -p build && cd build
   cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..
   make -j
   ./add
   ```
   预期看到 `[Success] Case accuracy is verification passed.`（[add.asc:157](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L157)）。

3. **修改 `TILE_NUM`**：编辑 `add.asc` 第 [28 行](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L28)，分别尝试 `TILE_NUM = 4` 和 `TILE_NUM = 16`，每次改完回到 `build` 目录重新 `make -j && ./add`。

4. **观察输出**：记录两种取值下是否仍打印 `[Success]`。

### 需要观察的现象

- `TILE_NUM = 4`：`TILE_LENGTH = 256`，`loopCount = 8`，应仍 `[Success]`。
- `TILE_NUM = 16`：`TILE_LENGTH = 64`，`loopCount = 32`，应仍 `[Success]`。

### 预期结果与解释

两种取值都应**通过校验**。原因正是 4.2 节的守恒等式：

\[
\text{loopCount} \times \text{TILE\_LENGTH}
= (\text{TILE\_NUM} \times \text{BUFFER\_NUM}) \times \frac{\text{BLOCK\_LENGTH}}{\text{TILE\_NUM} \times \text{BUFFER\_NUM}}
= \text{BLOCK\_LENGTH}
\]

只要 `BLOCK_LENGTH`（2048）能被 `TILE_NUM × BUFFER_NUM` 整除，`TILE_LENGTH` 就是整数，每个 block 处理的元素总数始终是 2048，8 个 block 合计始终是 16384，覆盖全部输入；`CopyIn/Compute/CopyOut` 只是换了「每批搬多少、搬几批」，搬运起止偏移 `progress * TILE_LENGTH` 仍能无缝衔接（见 [add.asc:59](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L59) 与 [add.asc:77](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L77)）。所以**分片粒度改变的是性能与流水重叠，不改变功能结果**。

> **边界提示（待本地验证）：** 若把 `TILE_NUM` 改成不能整除 `BLOCK_LENGTH/BUFFER_NUM` 的值（例如让 `TILE_LENGTH` 成为非整数或过小），`DataCopy` 在真实 NPU 上会触发 32 字节对齐等硬件约束（`TILE_LENGTH * sizeof(float)` 需对齐）。CPU Debug 仿真对这些约束的严格程度可能与 NPU 不同，所以「在 CPU 通过」不等于「在 NPU 一定通过」——这正是后续 u4（API 校验）和 u5（npu check）要补上的环节。

---

## 6. 本讲小结

- 一份 Ascend C 算子源码（`.asc`）通常分三层：**宿主侧（`main`/`kernel_add`）→ `__global__` 核函数（`add_custom`）→ Kernel 类（`KernelAdd`）**，复杂度逐层下沉。
- `KernelAdd` 用成员变量持有状态（`pipe`、3 个 `TQue`、3 个 `GlobalTensor`），用 `Init/Process` 表达行为；`Init` 用 `GetBlockIdx()` 为每个 block 切出 GM 视图。
- 算法核心是 **CopyIn/Compute/CopyOut 三段式**，对应 NPU 的 MTE2/Vector/MTE3 三条流水，配合 `BUFFER_NUM=2` 双缓冲实现搬运与计算重叠。
- `TQue` 通过 **`AllocTensor ↔ FreeTensor`、`EnQue ↔ DeQue`** 四个原语管理 `LocalTensor` 的 UB 生命周期，必须严格成对。
- 守恒等式 `loopCount × TILE_LENGTH = BLOCK_LENGTH` 解释了为何改变 `TILE_NUM` 只影响性能、不影响正确性。
- 核函数用 `add_custom<<<numBlocks, nullptr, stream>>>(xDevice, yDevice, zDevice)`（[add.asc:123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123)）启动，`<<<>>>` 在 CPU 模式被转义为 `AscCPUKernelLaunch` 调用（u2-l1）。

---

## 7. 下一步学习建议

到这里，你已经能「读懂并改动」一个 Ascend C 算子的源码结构。接下来的学习路径：

1. **u2-l3（使用 GDB 调试 CPU 域算子）**：动手用 gdb 在 `CopyIn/Compute` 断点，查看 `xLocal/yLocal` 内存，把本讲的结构知识变成可见的运行时状态。
2. **u3-l1（多核 fork 执行模型）**：本讲多次提到「8 个 block」，但它们在 CPU 上到底是怎么跑起来的？答案在 `RunKernelFunctionOnCpu` 的 fork 子进程模型里。
3. **u3-l3（Stub 注册与内建函数转义）**：本讲里那一行 `AscendC::Add` 在 CPU 上到底执行了什么？去 `stub_reg.cpp` 看内建函数如何被动态绑定。
4. 若想提前理解「为什么原语不配对会报错」，可以跳读 u5-l1（npu check 错误体系）。
