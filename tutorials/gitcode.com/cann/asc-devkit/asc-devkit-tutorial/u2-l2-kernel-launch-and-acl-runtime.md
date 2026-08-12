# Kernel 启动语法 `<<<>>>` 与 ACL 运行时

## 1. 本讲目标

本讲解决一个最基础也最关键的问题：**Host 侧（CPU）写好的 Kernel，到底是怎么被「送到」Device 侧（AI Core）上跑起来的？数据又是怎么在主机和设备之间来回搬运的？**

学完本讲，你应当能够：

- 看懂 `kernel<<<numBlocks, ..., stream>>>(...)` 这一行启动语法里每一个参数的含义，并能区分 SIMD 与 SIMT 两种写法。
- 理解 ACL（Ascend Computing Language）运行时提供的 Device 申请、Host/Device 内存申请与拷贝接口。
- 说清楚 Stream（计算流）的作用，以及为什么 Kernel 启动后必须做一次 `aclrtSynchronizeStream` 同步。
- 独立地把 `add` 样例的启动核数从 8 改成 4，并解释这对结果与性能的影响。

本讲承接 [u2-l1](./u2-l1-asc-file-host-device-model.md)：上一讲我们知道了 `.asc` 文件里 `__global__ __vector__` 限定符划定了 Host/Device 边界，本讲就来补上「Host 如何启动这个 Kernel、数据如何流动」的完整拼图。

## 2. 前置知识

在进入源码之前，先用三段通俗的话建立直觉。

**第一，异构计算 = 主机 + 设备两套内存。** Ascend 的算子程序跑在两类硬件上：Host 是你服务器上的 CPU，Device 是 AI 处理器（NPU）。两者各有各的内存——CPU 内存叫 Host 内存，NPU 自带的内存叫 Device 内存（也就是后面会反复出现的 GM，Global Memory）。CPU 不能直接读写 NPU 的内存，反之亦然。所以算子计算的套路永远是：**Host 准备数据 → 搬到 Device → Kernel 在 Device 上算 → 结果搬回 Host**。

**第二，Kernel 启动是「异步」的。** 当 Host 执行到 `kernel<<<...>>>()` 这一行时，它只是把「请帮我跑这个 Kernel」这个任务**提交**出去，然后**立刻继续往下执行**，并不会等 Kernel 算完。这叫「下发即返回」。好处是 Host 可以一边提交任务、一边做别的事（比如准备下一批数据），让 CPU 和 NPU 重叠工作。坏处是：如果你接下来马上就要读结果，必须显式地「等一等」，否则读到的可能是还没算完的脏数据。这个「等一等」就是同步。

**第三，Stream（计算流）是一个先进先出的任务队列。** 你提交的所有拷贝、所有 Kernel 启动，都会排进某个 Stream 里，**按入队顺序依次执行**。同一个 Stream 里的任务天然有序，你不用操心先来后到。「同步」就是等这个队列里所有任务都排空。

把这三点记在心里，下面的源码就会非常好读。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [hello_world.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc) | **最小启动样例**：只做「申请设备 → 启动 Kernel 打印 → 同步」，不含任何数据搬运，是理解 `<<<>>>` 与 Stream 的最干净切入点。 |
| [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) | **完整搬运样例**：包含 `aclrtMalloc`/`aclrtMemcpy`/启动/同步/释放的全生命周期，是理解 ACL 内存接口的最佳范例。 |

我们以 `hello_world.asc` 建立启动与同步的最小骨架，再用 `add.asc` 把内存搬运的完整流程补齐。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`<<<>>>` 启动语法**——一行启动代码里四个（或三个）参数分别是什么。
2. **ACL 运行时内存接口**——Host/Device 内存怎么申请、怎么拷贝。
3. **流与同步**——Stream 的有序队列模型，以及为什么必须同步。

### 4.1 `<<<>>>` 启动语法

#### 4.1.1 概念说明

`<<<...>>>` 叫**内核调用符**（kernel invocation operator），是 Ascend C 在 C/C++ 基础上扩展出来的语法。它只能出现在 Host 侧代码里，作用是：**向 Device 下发一个 Kernel 的执行配置**——告诉运行时「这个 Kernel 要在几个核上跑、每个核/线程块有多少线程、要不要额外动态内存、排到哪个流上」。

它有一个**通用（完整）形式**和两个**按编程模型裁剪的形式**：

```
// SIMT 通用形式：4 个参数
kernel<<<blocks_per_grid, threads_per_block, dyn_ubuf_size, stream>>>(args)

// SIMD 形式：3 个参数（没有 threads_per_block）
kernel<<<numBlocks, dynUBufSize, stream>>>(args)
```

为什么 SIMD 只有 3 个参数？因为 SIMD（基础 API / C API）的并行单位是「核（Block）」，一个核内只有一条向量数据通路，不存在「线程」概念；而 SIMT 的并行单位是「线程」，一个核里要再切分出成百上千个线程，所以多出一个 `threads_per_block` 参数。

本讲的两个样例都是 SIMD 矢量算子，因此实际只用到 3 参数形式。但理解 4 参数的通用形式，能帮你在后面学 SIMT（[u9-l1](./u9-l1-simt-programming-model.md)）时无缝迁移。

#### 4.1.2 核心流程

一次 `<<<>>>` 启动在背后做了这几件事（对 Host 线程而言是「立刻返回」的）：

```text
Host 执行到 kernel<<<cfg>>>(args)
   │
   ├─ 1. 解析执行配置（numBlocks / threads_per_block / dynUbufSize / stream）
   ├─ 2. 把「启动 Kernel」这个任务追加到 stream 队列尾部
   ├─ 3. 立刻把控制权还给 Host 线程（异步，不等算完）
   └─ 4. 运行时按队列顺序，到这个任务时再把 Kernel 派发到对应 AI Core 上执行
```

四个参数的含义对照如下（SIMD 形式取前三列）：

| 参数 | 类型 | 含义 | 默认值 | 约束 |
|------|------|------|--------|------|
| `numBlocks` / `blocks_per_grid` | 整数 / `dim3` | 在几个核（线程块）上执行 | —— | SIMD：受芯片核数限制；SIMT：grid 总线程块数 ≤ 65535 |
| `threads_per_block` | `dim3` | **仅 SIMT**：每个线程块的线程数 | —— | 每块线程数 ≤ `__launch_bounds__`（≤ 2048） |
| `dynUbufSize` / `dyn_ubuf_size` | `size_t` | 每核（每块）额外**动态**分配的 UB/共享内存字节数 | `0` | 仅 UB，不含 L1 等 |
| `stream` | `aclrtStream` | 关联的计算流 | `nullptr`（默认流） | 需提前 `aclrtCreateStream` 创建 |

启动后，每个被执行该 Kernel 的核会被分配一个逻辑 ID——`block_idx`（取值 `[0, numBlocks - 1]`），核函数内部正是用它来切分数据。

#### 4.1.3 源码精读

先看 `hello_world.asc` 里最干净的一行启动：

启动 Kernel 并同步（[hello_world.asc:26](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L26)）：

```cpp
hello_world<<<8, 0, stream>>>();
```

这一行就是 `kernel<<<numBlocks, dynUBufSize, stream>>>(args)` 的实例：`numBlocks=8` 表示在 8 个核上并行执行；`dynUBufSize=0` 表示不额外申请动态 UB；`stream` 是上一行刚创建的计算流；参数列表为空（`hello_world` 无入参）。注意它紧跟的下一行就是同步（详见 4.3 节）。

再看 `add.asc` 里带模板参数与入参的启动：

启动模板 Kernel（[add.asc:90](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L90)）：

```cpp
add_custom<blockLength><<<numBlocks, 0, stream>>>(xDevice, yDevice, zDevice);
```

这里有两个细节值得点出：

1. `<blockLength>` 是 **C++ 模板实参**，不是 `<<<>>>` 的一部分。`add_custom` 被声明为 `template <uint32_t blockLength>`（见 [add.asc:27-28](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L27-L28)），所以先写模板实参 `<blockLength>`，再写启动符 `<<<numBlocks, 0, stream>>>`，最后是 Kernel 的三个 `__gm__` 指针入参。模板参数在**编译期**就定好了每核处理长度，`<<<>>>` 的参数则在**运行时**下发执行配置——两者一前一后，不要混淆。
2. `numBlocks` 和 `blockLength` 都是 `constexpr` 常量（[add.asc:67-68](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L67-L68)）：`numBlocks = 8`、`blockLength = 2048`。它们必须满足一个覆盖关系：

\[
\text{numBlocks} \times \text{blockLength} \geq \text{totalLength}
\]

本样例 `totalLength = 8 \times 2048 = 16384`（[add.asc:134](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L134)），刚好 `8 \times 2048 = 16384`，每核各处理 2048 个元素、互不重叠，正好铺满。

核函数内部用 `block_idx` 切分数据（[add.asc:32-35](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L32-L35)）：

```cpp
AscendC::GlobalTensor<float> xGm, yGm, zGm;
xGm.SetGlobalBuffer(x + block_idx * blockLength, blockLength);  // 第 block_idx 个核偏移到自己的数据段
yGm.SetGlobalBuffer(y + block_idx * blockLength, blockLength);
zGm.SetGlobalBuffer(z + block_idx * blockLength, blockLength);
```

这里的 `block_idx` 就是启动配置 `numBlocks=8` 派发给每个核的逻辑 ID（取值 0~7）。第 0 个核偏移 0、第 1 个核偏移 2048……每个核只读自己那一段，从而实现 8 核并行、互不干扰。这是「启动语法」与「核内切分」协同的关键一环。

> **延伸阅读**：SIMT 的 4 参数形式官方说明见仓库文档 [AI-Core-SIMT编程/核函数.md:39-47](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编程模型/AI-Core-SIMT编程/核函数.md#L39-L47)；SIMD 的 3 参数形式说明见 [AI-Core-SIMD编程/核函数.md:56-66](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编程模型/AI-Core-SIMD编程/核函数.md#L56-L66)。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `numBlocks` 改变后，参与计算的核数确实变了。

**操作步骤**（源码阅读 + 修改型实践）：

1. 打开 [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc)，定位到 Kernel 内部的调试 `printf` 注释段（[add.asc:49-59](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L49-L59)），把它从 `#if 0` 改成 `#if 1`，并在 `DumpTensor` 上方加一行打印当前核号：

   ```cpp
   AscendC::printf("add blockIdx=%d\n", AscendC::GetBlockIdx());
   ```

2. 先不改启动参数，按 README 编译运行一次（命令见 4.2.4 节），观察打印出几个不同的 `blockIdx`（应当是 0~7，共 8 个）。
3. 再把 `numBlocks` 从 8 改成 4、`blockLength` 从 2048 改成 4096（保持乘积 = 16384），重新编译运行。

**需要观察的现象**：第 3 步后，打印的 `blockIdx` 应当只剩 0~3 共 4 个；而结果校验仍应输出 `test pass!`。

**预期结果**：核数减半但每核处理长度翻倍，总数据覆盖不变，结果正确。精确耗时变化**待本地用 `msopprof ./demo` 验证**。

> ⚠️ 注意：本实践会修改样例源码。请在样例目录下本地实验，不要把改动提交回仓库。

#### 4.1.5 小练习与答案

**练习 1**：`add.asc` 里写成 `<<<numBlocks, 0, stream>>>`，第二个参数 `0` 是什么意思？如果省略它会怎样？

**答案**：第二个参数是 `dynUBufSize`，即「每个核额外动态分配的 UB 字节数」。`0` 表示不额外申请动态 UB（本样例的 UB 由 `LocalMemAllocator` 在核内自行静态分配，见 [add.asc:37-40](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L37-L40)）。它的默认值就是 0，所以写成 `0` 是显式表达；不能省略，因为 `<<<>>>` 的参数是按位置匹配的，省略会改变 `stream` 的位置。

**练习 2**：SIMD 的 `<<<numBlocks, dynUBufSize, stream>>>` 和 SIMT 的 `<<<blocks_per_grid, threads_per_block, dyn_ubuf_size, stream>>>` 差在哪一个参数？为什么？

**答案**：差在第二个参数 `threads_per_block`。SIMD 以「核」为并行单位，核内没有线程概念；SIMT 以「线程」为并行单位，每个核（线程块）内部还要再切分成百上千个线程，所以需要 `threads_per_block` 指定每块线程数。其余三个参数含义对应相同。

### 4.2 ACL 运行时内存接口

#### 4.2.1 概念说明

Kernel 在 Device 上算，数据却由 Host 准备——中间必须有人负责「申请 Device 内存」和「在 Host/Device 之间搬数据」。这套接口由 **ACL Runtime**（CANN 运行时）提供，全部以 `acl` / `aclrt` 为前缀。

你需要记住三类接口：

| 类别 | 申请 | 释放 | 说明 |
|------|------|------|------|
| Device 内存（NPU 的 GM） | `aclrtMalloc` | `aclrtFree` | Kernel 直接访问的输入/输出必须落在这里 |
| Host 内存（CPU 侧 pinned） | `aclrtMallocHost` | `aclrtFreeHost` | 用于高性能地承接 Device 回拷的结果 |
| Host/Device 拷贝 | `aclrtMemcpy` | —— | 指定方向：H2D / D2H / D2D |

此外还有两个生命周期接口：`aclInit`（运行时初始化）和 `aclFinalize`（运行时去初始化），以及 `aclrtSetDevice` / `aclrtResetDevice`（申请/释放某号设备的使用权）。

> 为什么 `add.asc` 用 `aclrtMallocHost` 申请一个 `zHost`，而不是直接用 `std::vector` 的内存接结果？因为 `aclrtMallocHost` 申请的是**锁页内存（pinned memory）**，DMA 搬运效率更高。当然，最简写法里直接用 vector 内存拷贝也可以（`x.data()` 就是普通 Host 内存，输入侧正是这么用的），输出侧单独用 pinned 是为了更高效地接 D2H。

#### 4.2.2 核心流程

ACL Runtime 算子运行的标准生命周期是固定的 8 步，这是本模块最重要的一张「地图」：

```text
1. aclInit(nullptr)                     // 运行时初始化
2. aclrtSetDevice(deviceId)             // 选定设备
   aclrtCreateStream(&stream)           // 创建计算流
3. aclrtMalloc(...) × N                 // 在 Device 申请 GM
   aclrtMallocHost(...)                 // （可选）在 Host 申请 pinned
   aclrtMemcpy(..., H2D)                // 输入数据 Host → Device
4. kernel<<<numBlocks, 0, stream>>>(...) // 异步启动 Kernel（详见 4.1）
5. aclrtSynchronizeStream(stream)       // 等 Kernel 算完（详见 4.3）
6. aclrtMemcpy(..., D2H)                // 结果 Device → Host
7. aclrtFree(...) × N / aclrtFreeHost   // 释放 Device / Host 内存
   aclrtDestroyStream(stream)           // 销毁流
   aclrtResetDevice(deviceId)           // 释放设备
8. aclFinalize()                        // 运行时去初始化
```

这张图对应官方文档 [异步执行.md:9-19](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编译与运行/异步执行.md#L9-L19)。把它和下面的源码逐行对一遍，整个 `kernel_add` 函数就一目了然了。

#### 4.2.3 源码精读

`add.asc` 的 `kernel_add` 函数是上述 8 步的教科书式实现。我们分段精读。

**初始化与设备/流申请（第 1~2 步）**，见 [add.asc:76-80](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L76-L80)：

```cpp
aclInit(nullptr);
int32_t deviceId = 0;
aclrtSetDevice(deviceId);
aclrtStream stream = nullptr;
aclrtCreateStream(&stream);
```

注意 `hello_world.asc` 里没有 `aclInit/aclFinalize`（[hello_world.asc:23-29](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L23-L29)），只做了 `aclrtSetDevice` / `aclrtCreateStream`——这说明设备/流接口可以在不显式 `aclInit` 的情况下使用（运行时会做隐式处理），但 `add.asc` 作为「正式」写法把头尾都补齐了，是更规范的范式。

**内存申请（第 3 步前半）**，见 [add.asc:82-85](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L82-L85)：

```cpp
aclrtMalloc((void**)&xDevice, totalByteSize, ACL_MEM_MALLOC_HUGE_FIRST);
aclrtMalloc((void**)&yDevice, totalByteSize, ACL_MEM_MALLOC_HUGE_FIRST);
aclrtMalloc((void**)&zDevice, totalByteSize, ACL_MEM_MALLOC_HUGE_FIRST);
aclrtMallocHost((void**)&zHost, totalByteSize);
```

`aclrtMalloc` 三个参数依次是：输出指针地址、字节数、分配策略。`ACL_MEM_MALLOC_HUGE_FIRST` 表示「优先用大页（Huge Page）」，通常能带来更好的搬运性能。三个 Device 缓冲分别给输入 x、y 和输出 z；`zHost` 是 Host 侧锁页内存，准备接回拷结果。

**输入数据搬入 Device（第 3 步后半，H2D）**，见 [add.asc:87-88](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L87-L88)：

```cpp
aclrtMemcpy(xDevice, totalByteSize, x.data(), totalByteSize, ACL_MEMCPY_HOST_TO_DEVICE);
aclrtMemcpy(yDevice, totalByteSize, y.data(), totalByteSize, ACL_MEMCPY_HOST_TO_DEVICE);
```

`aclrtMemcpy(dst, dstMax, src, count, kind)` 五个参数：目标地址、目标最大容量、源地址、拷贝字节数、方向。这里 `x.data()` 是 Host 上 `std::vector` 的普通内存，方向是 `ACL_MEMCPY_HOST_TO_DEVICE`。这一步之后，x、y 的数据就躺在了 NPU 的 GM 里，Kernel 就能通过 `__gm__ float* x` 读到它们。

**Kernel 启动 + 同步（第 4~5 步）**，见 [add.asc:90-91](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L90-L91)：

```cpp
add_custom<blockLength><<<numBlocks, 0, stream>>>(xDevice, yDevice, zDevice);
aclrtSynchronizeStream(stream);
```

启动语法已在 4.1 讲透。关键再次强调：第 90 行是**异步**的，第 91 行的同步**必须**紧跟，否则第 93 行去 `zDevice` 里取结果时，Kernel 可能还没算完。

**结果搬回 Host（第 6 步，D2H）**，见 [add.asc:93-94](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L93-L94)：

```cpp
aclrtMemcpy(zHost, totalByteSize, zDevice, totalByteSize, ACL_MEMCPY_DEVICE_TO_HOST);
std::vector<float> z((float*)zHost, (float*)(zHost + totalByteSize));
```

方向换成 `ACL_MEMCPY_DEVICE_TO_HOST`，把 Device 上的 `zDevice` 搬到 Host 的 `zHost`，再用迭代器构造成 `std::vector<float>` 返回给调用方校验。

**资源释放（第 7~8 步）**，见 [add.asc:96-103](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L96-L103)：

```cpp
aclrtFree(xDevice);
aclrtFree(yDevice);
aclrtFree(zDevice);
aclrtFreeHost(zHost);

aclrtDestroyStream(stream);
aclrtResetDevice(deviceId);
aclFinalize();
```

注意释放顺序与申请**严格对称**：先释放内存（`aclrtFree`/`aclrtFreeHost`），再销毁流（`aclrtDestroyStream`），再释放设备（`aclrtResetDevice`），最后 `aclFinalize`。如果先 `aclrtResetDevice` 再 `aclrtFree`，会因设备已释放而报错——这是新手常踩的坑。

#### 4.2.4 代码实践

**实践目标**：完整跑通 `add` 样例，亲历一遍 8 步生命周期。

**操作步骤**（参照样例 [README.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/README.md) 的「编译运行」一节）：

```bash
source ${install_path}/cann/set_env.sh          # 1. 配置 CANN 环境变量
cd examples/01_simd_cpp_api/00_introduction/01_add/add
mkdir -p build && cd build
cmake -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..      # 2. 配置工程（默认 npu 模式）
make -j                                          # 3. 编译，产出 ./demo
./demo                                           # 4. 运行
```

**需要观察的现象**：终端最后输出 `test pass!`。

**预期结果**：`test pass!` 表示 Kernel 算出的 `output` 与 Host 侧用纯 C++ 算出的 `golden` 逐元素相等（校验逻辑见 [add.asc:108-130](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L108-L130) 的 `VerifyResult`）。

> 若没有真实 NPU，可改用 CPU 调试或 NPU 仿真模式：`cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..`（切换前需 `rm CMakeCache.txt` 清缓存）。具体能否在你本机跑通**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 [add.asc:91](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L91) 的 `aclrtSynchronizeStream(stream);` 直接删掉，程序会发生什么？

**答案**：第 90 行 Kernel 是异步启动的，删掉同步意味着 Host 不会等它算完，紧接着第 93 行 `aclrtMemcpy(zHost, ..., zDevice, ..., D2H)` 就会读到 `zDevice` 里**尚未被写完**的数据，最终 `VerifyResult` 大概率输出 `test failed!`（或数据时好时坏）。同步不是「可选优化」，而是「正确性前提」。

**练习 2**：`aclrtMemcpy` 的第五个参数 `kind` 有哪些取值？本样例用了哪两个？

**答案**：常见取值有 `ACL_MEMCPY_HOST_TO_DEVICE`（H2D）、`ACL_MEMCPY_DEVICE_TO_HOST`（D2H）、`ACL_MEMCPY_DEVICE_TO_DEVICE`（D2D）。本样例输入搬入用了 H2D（[add.asc:87-88](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L87-L88)），结果搬回用了 D2H（[add.asc:93](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L93)）。方向写反会导致数据流向错误、结果全错。

### 4.3 流与同步

#### 4.3.1 概念说明

**Stream（计算流）** 本质上是一个**有序的任务队列**。你向同一个 Stream 提交的所有操作——`aclrtMemcpy`、`kernel<<<...>>>`——都会**严格按照提交顺序依次执行**，前一个没完，后一个不会开始。这就给了你一种免费的「先序保证」：你只要按逻辑顺序把任务排进同一个 Stream，运行时就保证它们按这个顺序跑。

CANN Runtime 内置了一个**默认流**（`nullptr` 即代表默认流）。当你不显式创建 Stream、或把启动参数写成 `nullptr` 时，任务就排进默认流。生产代码里通常会显式 `aclrtCreateStream` 创建独立流，以便多个流之间并行、与默认流互不干扰。

**同步** 解决的是「Host 线程等不等 Device」的问题。因为 Kernel 启动是异步的，Host 想读 Device 产出的结果，就必须有办法让 Host 「阻塞等待」，直到 Stream 队列排空。最常用的两个同步接口：

| 同步接口 | 作用范围 |
|----------|----------|
| `aclrtSynchronizeStream(stream)` | 等**这一个 Stream** 里所有任务完成 |
| `aclrtSynchronizeDevice()` | 等**当前设备**上所有 Stream 的所有任务完成 |

显然 `aclrtSynchronizeStream` 粒度更细、等待时间通常更短，是算子里最常用的。

#### 4.3.2 核心流程

把 Stream 和同步放进时间轴，一次算子调用的时序是这样的：

```text
Host 线程                  Stream 队列                 Device(AI Core)
   │
   ├─ aclrtMemcpy(H2D) ──▶ [ memcpy ]
   │                       [ memcpy ] ──────────────▶ 搬 x,y 到 GM
   ├─ kernel<<<...>>>  ──▶ [ launch ]                 (按序执行)
   │                       [ launch ] ──────────────▶ 跑 add_custom
   ├─ (立刻返回,异步)
   ├─ ... 可继续提交别的任务 ...
   ├─ aclrtMemcpy(D2H) ──▶ [ memcpy ]
   │                       [ memcpy ] ──────────────▶ 搬 z 回 Host
   ├─ aclrtSynchronizeStream(stream)
   │       ▲ 阻塞在这里,直到队列清空
   ▼ Host 继续,此时结果可读
```

关键点：同一 Stream 内任务天然有序，所以 `D2H` 一定排在 `launch` 之后执行——但这只是「Device 侧的执行顺序」有序，**Host 线程本身并不会因此阻塞**。`aclrtSynchronizeStream` 才是真正让 Host 阻塞等待的那一行。

#### 4.3.3 源码精读

`hello_world.asc` 是理解 Stream/同步最干净的样例，因为它没有任何数据搬运干扰：

```cpp
aclrtSetDevice(0);                    // 选 0 号设备
aclrtStream stream = nullptr;
aclrtCreateStream(&stream);           // 创建流
hello_world<<<8, 0, stream>>>();      // 提交 Kernel 到该流（异步，立刻返回）
aclrtSynchronizeStream(stream);       // 阻塞 Host,等 8 个核都打印完
aclrtDestroyStream(stream);           // 销毁流
aclrtResetDevice(0);                  // 释放设备
```

这几行对应 [hello_world.asc:23-29](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L23-L29)。如果把 `aclrtSynchronizeStream(stream);`（[hello_world.asc:27](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L27)）去掉，程序可能在 Kernel 还没来得及 `printf` 时就执行到 `aclrtDestroyStream` / `aclrtResetDevice`，导致打印丢失或运行时报错。

`add.asc` 里同样在启动后紧跟同步（[add.asc:90-91](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L90-L91)），目的就是确保第 93 行回拷 `z` 时，Kernel 已经把 `zDevice` 写完。

> **延伸阅读**：Stream 的有序队列模型、默认流、异步执行策略详见官方文档 [异步执行.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编译与运行/异步执行.md)，尤其是「Stream 管理」「异步调用」「同步等待」三节。

#### 4.3.4 代码实践

**实践目标**：体会「异步」与「同步」的差异。

**操作步骤**（源码阅读 + 改参数观察型实践）：

1. 在 `add.asc` 的 `kernel_add` 里，启动 Kernel 前后各加一行 Host 侧打印：

   ```cpp
   std::cout << "[host] before launch" << std::endl;
   add_custom<blockLength><<<numBlocks, 0, stream>>>(xDevice, yDevice, zDevice);
   std::cout << "[host] after launch (async, not finished)" << std::endl;
   aclrtSynchronizeStream(stream);
   std::cout << "[host] after sync (kernel done)" << std::endl;
   ```

2. 编译运行（命令同 4.2.4）。

**需要观察的现象**：`after launch` 这一行会**很快**打印（因为启动是异步的，Host 没等）；而 `after sync` 会有一段可感知的等待后才打印（Host 在这里阻塞，直到 Kernel 真正算完）。

**预期结果**：三行打印按顺序出现，且 `after launch` 与 `after sync` 之间存在时间差——这个时间差就是 Kernel 在 NPU 上的实际执行耗时。精确耗时**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：同一个 Stream 里，如果我先启动 Kernel A、再启动 Kernel B，B 会在 A 之前跑完吗？

**答案**：不会。同一 Stream 是严格有序的先进先出队列，B 一定排在 A 之后执行，必须等 A 完成后才开始。若想让 A、B 并行，需要把它们放到**不同的 Stream**里。

**练习 2**：`aclrtSynchronizeStream` 和 `aclrtSynchronizeDevice` 有何区别？算子里通常用哪个？

**答案**：前者只等**指定 Stream** 排空，后者等**当前设备所有 Stream** 排空。算子里通常用 `aclrtSynchronizeStream`，因为它粒度更细、等待范围更小、不阻塞其他不相关的流。`hello_world.asc` 与 `add.asc` 用的都是它。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿性小任务**——它也正是本讲指定的代码实践任务。

> **任务**：在 [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) 的 `kernel_add` 函数基础上，**把启动核数从 8 改成 4，并相应调整每核处理长度**，然后说明这对结果和性能的影响。

**操作步骤**：

1. 打开 `add.asc`，定位到常量定义（[add.asc:67-68](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L67-L68)）：

   ```cpp
   constexpr uint32_t numBlocks = 8;      // 改成 4
   constexpr uint32_t blockLength = 2048; // 改成 4096
   ```

2. 确认 `main` 里 `totalLength = 8 * 2048`（[add.asc:134](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L134)）**保持不变**（仍为 16384）。这样满足 `numBlocks × blockLength = 4 × 4096 = 16384 = totalLength`，数据仍被完整覆盖。
3. 按 4.2.4 节的命令重新编译运行。

**分析（结果）**：

- **结果不变**。Kernel 内部用 `block_idx * blockLength` 做偏移切分（[add.asc:33-35](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L33-L35)）。改成 4 核后，第 0 核偏移 0 处理 [0, 4096)、第 1 核偏移 4096 处理 [4096, 8192)……四段拼起来仍是 [0, 16384)，与原来 8 核各处理 2048 的覆盖范围**逐元素一致**。`VerifyResult` 仍应输出 `test pass!`。

**分析（性能）**：这是一个「拆东墙补西墙」的权衡：

| 维度 | 8 核 × 2048（原） | 4 核 × 4096（改） | 影响 |
|------|------------------|------------------|------|
| 多核并行度 | 8 核同时算 | 4 核同时算 | ↓ 并行度减半，端到端耗时倾向于变长 |
| 单核搬运粒度 | 每次 2048 float = 8 KB | 每次 4096 float = 16 KB | ↑ 搬运粒度翻倍，带宽利用率提升、启动开销摊薄 |
| UB 占用 | 每核 3×2048 float | 每核 3×4096 float | ↑ 每核 UB 占用翻倍（仍在容量内） |

净效果取决于硬件核数与数据量：若芯片核数远多于 4，减少核数会明显拉长耗时（浪费并行度）；若数据量很小、4 核已能打满带宽，则差距不大。README 的「可优化方向」也同时提到了「多核动态分配」（多用核）与「增大搬运粒度」（每核多搬）两个方向——本任务正好把它们对立起来，是理解这两者权衡的好例子。**精确耗时变化待本地用 `msopprof ./demo` 对比两次运行后验证。**

> ⚠️ 本任务会修改样例源码，请仅在本地实验，不要提交回仓库。

## 6. 本讲小结

- `<<<>>>` 是 Host 向 Device 下发 Kernel 的内核调用符。**SIMD 用 3 参数** `<<<numBlocks, dynUBufSize, stream>>>`，**SIMT 用 4 参数** `<<<blocks_per_grid, threads_per_block, dyn_ubuf_size, stream>>>`，多出的 `threads_per_block` 是线程级并行的需要。
- 两个样例都是 SIMD，启动代码分别是 [hello_world.asc:26](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/00_quickstart/hello_world/hello_world.asc#L26) 与 [add.asc:90](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L90)；启动配置 `numBlocks × blockLength` 必须覆盖 `totalLength`，核内用 `block_idx` 切分数据。
- ACL Runtime 的标准生命周期是 8 步：`aclInit` → `SetDevice`+`CreateStream` → `Malloc`+`Memcpy(H2D)` → `<<<>>>` 启动 → `SynchronizeStream` → `Memcpy(D2H)` → `Free`+`DestroyStream`+`ResetDevice` → `aclFinalize`，申请与释放严格对称。
- Host/Device 内存分离：Device 用 `aclrtMalloc/aclrtFree`，Host pinned 用 `aclrtMallocHost/aclrtFreeHost`，二者用 `aclrtMemcpy` 按方向（H2D/D2H）搬运。
- Kernel 启动是**异步**的；Stream 是**有序任务队列**；Host 读结果前**必须** `aclrtSynchronizeStream` 同步，否则读到脏数据。
- 把 `add` 改成 4 核×4096，结果不变、性能是「并行度↓ vs 搬运粒度↑」的权衡。

## 7. 下一步学习建议

本讲把「Host 如何启动 Kernel、数据如何搬运」讲清楚了，但 Kernel **内部**怎么用这些 GM 地址、怎么把数据搬到 UB、怎么做加法，还一笔带过。建议接下来：

1. **[u2-l3 端到端跑通第一个矢量加法算子](./u2-l3-run-first-add-operator.md)**：把本讲的编译运行流程再走一遍，重点看 `gen_data`/`verify_result` 的数据生成与校验，亲手跑通一个算子。
2. **[u3-l1 Ascend 内存层级与地址空间限定符](./u3-l1-memory-hierarchy-address-spaces.md)**：进入 Kernel 内部，搞清楚 GM、UB、L1、L0 的多级内存层次，以及 `__gm__`/`__ubuf__` 背后的物理存储。
3. 若对 SIMT 的线程模型好奇，可提前浏览 [u9-l1 SIMT 编程模型](./u9-l1-simt-programming-model.md)，对比 `<<<blocks_per_grid, threads_per_block, ...>>>` 4 参数形式与本章 SIMD 3 参数形式的差异。
