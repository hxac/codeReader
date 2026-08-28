# Conkernels：并发 kernel 与 CUDA stream

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 CUDA stream（流）是什么：同一流内操作按序执行、不同流之间可以并发执行。
2. 读懂 `timed_kernel` 如何用设备端 `clock()` 构造一段"精确到毫秒"的 GPU 忙等负载，以及为什么每个 kernel 只用 `<<<1,1>>>` 一个线程。
3. 用 `cudaGetDeviceProperties` 查询 `prop.concurrentKernels` 属性，先确认硬件支持并发 kernel 再做实验。
4. 掌握一套完整的流管理生命周期：`cudaStreamCreate` → 把 kernel 用启动配置的第 4 个参数指派到流 → `cudaEventRecord` + `cudaStreamWaitEvent` 建立流间依赖 → `cudaEventSynchronize` 收尾 → `cudaStreamDestroy` 释放。
5. 理解仓库为什么额外提供 `Makefile_serialized`：整个对照实验（并发 vs 串行）只靠一个 `-DFORCE_SERIALIZED` 宏切换，其余代码一行不改。

本讲对应的仓库定位：在主 README 的总结表里，Conkernels 演示的低效模式是"Launch multiple kernel instances on one GPU"（在一块 GPU 上启动多个 kernel 实例），优化技术是"Use concurrent kernels technique"（使用并发 kernel 技术），参见 [README.md:L33-L35](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L33-L35)。

## 2. 前置知识

### 2.1 stream：CUDA 中的"任务队列"

在 u2-l3 我们已经知道：kernel 启动是异步的，host 端提交启动请求后立刻返回；不指定流时，所有操作进入**默认流（default stream，代码里写作 `0`）**，默认流像一个隐式的全局队列，按提交顺序依次执行。

**stream（流）** 就是把这个"队列"显式化、可复制化：

- 同一条流内的操作严格**按入队顺序**执行（先进先出，FIFO）。
- 不同流之间的操作**没有顺序保证**——如果 GPU 资源足够，它们可以真正同时跑在硬件上；资源不够时则分时共享。

一句话直觉：默认流是一条单车道，所有车排队通行；多条 stream 是多条车道，车流可以并行。

### 2.2 kernel 并发需要什么条件

把 8 个 kernel 分别放进 8 条流，并不保证它们同时执行，还需要：

1. **设备支持并发 kernel**：计算能力 2.0（Fermi 架构）起的 GPU 都支持，可用 `prop.concurrentKernels` 查询（本讲 4.2 节）。
2. **每个 kernel 占用的资源足够小**：GPU 的每个 SM 能驻留（resident）的 block 数量有限。如果一个 kernel 就铺满了整个 GPU，第二个 kernel 只能等它让出资源——形式上"在不同流里"，物理上仍是串行。

这就是为什么本基准的每个 kernel 都故意只启动 `<<<1,1>>>`（1 个 block、1 个线程）：让 8 个 kernel 轻松同时驻留在不同 SM 上，把"能否并发"这个变量干净地隔离出来。

### 2.3 事件（event）：流之间的"信号灯"

流本身互不知晓。要让"A 流的某步等 B 流全部完成"这种跨流依赖成立，需要 **event（事件）**：

- `cudaEventRecord(event, stream)`：在流里插入一个"完成标记"，该标记在它前面的所有操作完成后被触发。
- `cudaStreamWaitEvent(streamA, event, 0)`：让 streamA 后续的操作阻塞，直到 event 被触发。

本讲 4.3 节会看到，Conkernels 用这两个调用实现了一个经典的"**汇合（join）**"模式：汇总 kernel 所在的流等全部 8 个计时 kernel 的事件。

### 2.4 与前几讲的衔接

u2-l3 讲过 `cudaMalloc`/`cudaMemcpy`/`cudaDeviceSynchronize` 的基础流程与默认流的隐式排序；u1-l4 讲过 nvprof 的用法与"程序打印的时间 ≠ 纯 kernel 时间"。本讲把默认流推广到多条显式流，并首次接触 `cudaMemcpyAsync`（异步拷贝）与页锁定（page-locked / pinned）主机内存。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Conkernels/concurrentKernels.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L1-L175) | 唯一的源码文件：两个 kernel（`timed_kernel`、`sum_clocks`）加一个 `main`，本讲的主体 |
| [Conkernels/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L1-L335) | 编译**并发版**可执行文件（CUDA Samples 风格的多架构模板） |
| [Conkernels/Makefile_serialized](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile_serialized#L1-L335) | 编译**串行版**：与上面的 Makefile 仅差一个 `-DFORCE_SERIALIZED` 宏定义 |
| [Conkernels/README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/README.md#L1-L67) | 沿自 CUDA Samples 的说明：本示例演示 streams 并发执行与 `cudaStreamWaitEvent` 建立流间依赖（第 5 行），要求 CUDA 11.1（第 27 行） |

两点实地观察（延续 u1-l1 的"读文档须核验"原则）：

- **本目录没有 `test.sh`**，也没有归档的 `.output.*.txt` 结果文件——与 CoMem_AXPY、BankRedux 等目录不同，Conkernels 的实验要靠读者自己跑（本讲第 5 节给出完整命令）。
- Makefile 里有 `INCLUDES := -I../../Common`（[Conkernels/Makefile:L274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L274)），但从 `Conkernels` 出发 `../../Common` 指向仓库**外面**；好在 `concurrentKernels.cu` 只 include 了 4 个标准 C++ 头（[Conkernels/concurrentKernels.cu:L6-L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L6-L9)），从未用到 Common 里的 helper 头，所以这个失效的 include 路径不影响编译。这是从 CUDA Samples 搬运时留下的"化石"，u1-l1 已提示过此疑点，这里得到确认。

## 4. 核心概念与源码讲解

### 4.1 timed_kernel 与 sum_clocks：构造并汇总一段"精确时长"的 GPU 负载

#### 4.1.1 概念说明

要研究"多个 kernel 能否并发"，首先需要一个**工作负载**。真实 kernel 的耗时随硬件、数据、编译选项波动，不适合做受控实验。Conkernels 的做法是写一个"自计时忙等"kernel：

- `timed_kernel`：不做什么有用的计算，只原地空转（busy-wait），直到消耗掉指定数量的时钟周期，然后把自己实际消耗的周期数写进输出数组。
- `sum_clocks`：一个块内共享内存归约 kernel，把 8 个 kernel 各自报告的周期数加起来（它就是 BankRedux 那类树形归约的翻版，u4-l5 会展开讲归约本身）。

这套设计把"每个 kernel 该跑多久"变成一个**可精确控制的参数**（毫秒数），让实验者能提前算出理论总时长，再去和实测对照。

#### 4.1.2 核心流程

`timed_kernel` 的逻辑：

```text
start = clock()                    # 记下设备时钟计数
while (clock() - start < 时钟周期数):
    空转                           # 反复读时钟，直到烧够周期
clocksArray[kernelIdx] = elapsed    # 把实际烧掉的周期数写回全局内存
```

"要烧多少周期"由 host 端根据 GPU 主频换算。`prop.clockRate` 的单位是 **kHz**，目标时长单位是 **ms**，于是：

\[ \text{ticks} = f_{\text{kHz}} \times t_{\text{ms}} = (f \times 10^{3}\,\text{s}^{-1}) \times (t \times 10^{-3}\,\text{s}) \]

kHz 与 ms 的量纲（\(10^3\) 与 \(10^{-3}\)）恰好抵消，直接相乘就是周期数。源码注释也写明了这步换算。例如一块 1.5 GHz 的卡，`clockRate = 1500000` kHz，目标 50 ms，则需要 \(1.5 \times 10^6 \times 50 = 7.5 \times 10^7\) 个周期。

`sum_clocks` 的逻辑（单 block、32 线程的树形归约）：

```text
每线程先把跨步元素累进 cache[threadIdx.x]（步长 32）
__syncthreads()
折半归约：i 从 16 递减到 1，前 i 个线程做 cache[tid] += cache[tid+i]，每步同步
result[0] = cache[0]
```

#### 4.1.3 源码精读

`timed_kernel` 定义——注意设备端的 `clock()`（CUDA 内建函数，返回从 kernel 启动开始累计的时钟周期数）与写回自己专属的 `clocksArray[kernelIdx]` 槽位（8 个 kernel 各写各的下标，即使并发执行也不存在数据竞争）：

[Conkernels/concurrentKernels.cu:L12-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L12-L17)——定义忙等 kernel：读取 `clock()` 空转直到烧满 `clockTicks`，把实际耗时（周期数）写入 `clocksArray[kernelIdx]`。

`sum_clocks` 定义——注释明确假设只用一个 block（多 block 需要块间同步，作者指向 CUDA Samples 的示例 4.1/4.2）：

[Conkernels/concurrentKernels.cu:L21-L36](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L21-L36)——32 个线程先用跨步循环把 `clocks[]` 累加进大小为 32 的共享内存数组 `cache`，`__syncthreads()` 后做折半树归约，最终总和写入 `result[0]`。

主频来源——`clockRate` 就来自设备属性：

[Conkernels/concurrentKernels.cu:L94-L94](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L94)——把设备属性 `prop.clockRate`（单位 kHz）保存为 `CLOCK_FREQ_kHz`，供后续换算时钟周期与打印 GPU 主频。

单位换算的注释——就在串行分支内部，解释了上面 \[ \text{ticks} = f_{\text{kHz}} \times t_{\text{ms}} \] 的量纲抵消：

[Conkernels/concurrentKernels.cu:L100-L102](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L100-L102)——注释推导 `clock ticks = CLOCK_FREQ_kHz × KERNEL_EXECUTION_TIME_ms`，即 kHz 乘 ms 恰得周期数。

两个常量参数：

[Conkernels/concurrentKernels.cu:L52-L55](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L52-L55)——定义 `NUM_KERNELS = 8`（kernel/流数量）与 `KERNEL_EXECUTION_TIME_ms = 50`（每个 kernel 的目标执行时长）；上一行注释提示"改这个值可以探测设备支持的最大并发 kernel 数"。

#### 4.1.4 代码实践

**实践目标**：验证 `timed_kernel` 的"自计时"机制，体会"参数即时长"的受控实验设计。

**操作步骤**：

1. 阅读 [Conkernels/concurrentKernels.cu:L12-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L12-L17)，在纸上回答：为什么用 `while (elapsed < clockTicks)` 忙等，而不是空 `for` 循环转固定次数？（提示：空转次数换算成时间依赖指令发射速率，忙等直接以时钟周期为单位，与指令吞吐无关。）
2. 查你所用 GPU 的主频（`nvidia-smi -q -d CLOCK` 或 [Conkernels/concurrentKernels.cu:L141-L141](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L141) 打印的 `Clock` 行），手算 50 ms 对应多少 ticks。
3. 把 [L55](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L55) 的 `KERNEL_EXECUTION_TIME_ms` 从 50 改成 100，重新编译运行。

**需要观察的现象**：输出中 `Computed kernel execution time`（8 个 kernel 实际耗时的最大值，见 [L144-L145](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L144-L145)）应约等于你设置的目标值。

**预期结果**：改成 100 后，`Computed kernel execution time` 约为 100 ms，`Sum of kernel execution times` 约为 800 ms（8 个 kernel 各 100 ms）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`timed_kernel` 里为什么每个 kernel 只启动 `<<<1,1>>>`（1 个 block、1 个线程）？改成 `<<<1,1024>>>` 会怎样？

**参考答案**：并发的前提是多个 kernel 能同时驻留在 SM 上；每个 kernel 的资源占用越小，能同时驻留的越多。`<<<1,1>>>` 让 8 个 kernel 几乎不争抢资源，把"流并发性"这个被测变量隔离出来。改成 `<<<1,1024>>>` 后每个 kernel 占满一个 block 的线程资源，kernel 数量多时部分 kernel 会因资源不足退回排队，即便放在不同流里也测不出理想并发。（这正是第 5 节综合实践要实测的问题。）

**练习 2**：8 个 kernel 并发执行时都写 `clocksArray`，为什么不需要加锁或原子操作？

**参考答案**：每个 kernel 实例的 `kernelIdx` 不同，写的是 `clocksArray` 的不同下标（[L16](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L16)），不同地址的普通写互不干扰。数据竞争只发生在"多个执行流读写**同一**地址且至少一个是写"时。

**练习 3**：`sum_clocks` 为什么必须在所有 `timed_kernel` 之后执行？由谁保证这一点？

**参考答案**：它要读的 `dev_clocks` 是 8 个 kernel 的输出，读早了就是未初始化数据。保证来自 4.3 节讲的汇合模式：每个 kernel 所在流里记录事件，`sum_clocks` 所在的 `time_compute_stream` 用 `cudaStreamWaitEvent` 等待全部事件后，`sum_clocks` 才被放行。

### 4.2 concurrency 属性检查：先问硬件"你支持吗"

#### 4.2.1 概念说明

CUDA 的能力随架构演进（并发 kernel、动态并行、Unified Memory……），写程序前先查设备属性是良好习惯，也是可移植性的第一道防线。`cudaDeviceProp` 是一个巨大的结构体，几乎每种硬件能力都有对应字段：

- `concurrentKernels`：int，非 0 表示支持同设备多 kernel 并发执行。
- `clockRate`：int，主频，单位 kHz（本讲 4.1 节换算周期数用的就是它）。
- 另有 `multiProcessorCount`、`name`、`major`/`minor` 等，读者可以自行打印浏览。

注意本程序对"不支持"的处理是**打印警告后继续运行**（不支持并发的设备上，多流会退化为串行，程序仍然正确，只是体现不出加速）——这本身就是"能力查询 + 优雅降级"的示范。

#### 4.2.2 核心流程

```text
cudaGetDevice(&currentDevice)                # 当前用的是哪块卡
cudaGetDeviceProperties(&prop, currentDevice)# 把它的属性填进结构体
if (prop.concurrentKernels == 0)             # 不支持并发 kernel
    打印 "kernels will be serialized" 警告   # 提示但不退出
CLOCK_FREQ_kHz = prop.clockRate              # 主频供周期换算
```

#### 4.2.3 源码精读

[Conkernels/concurrentKernels.cu:L42-L49](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L42-L49)——`main` 的第一件事：`cudaGetDevice` 取当前设备号，`cudaGetDeviceProperties` 填充 `prop`，若 `prop.concurrentKernels == 0` 则打印"不支持并发 kernel、将退化为串行"的警告（只警告、不终止）。

[Conkernels/concurrentKernels.cu:L94-L94](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L94)——属性结构体的第二个用途：取出 `prop.clockRate` 作为周期换算的基准。

#### 4.2.4 代码实践

**实践目标**：亲手查询并打印自己设备的并发能力与主频。

**操作步骤**：

1. 在 `Conkernels/` 下新建一个 `myprops.cu`（**示例代码**，仅用于练习；不要提交到源码仓库）：

   ```cpp
   // 示例代码：打印与并发 kernel 相关的设备属性
   #include <iostream>
   int main() {
       cudaDeviceProp prop = cudaDeviceProp();
       int dev = -1;
       cudaGetDevice(&dev);
       cudaGetDeviceProperties(&prop, dev);
       std::cout << "name                = " << prop.name << std::endl;
       std::cout << "compute capability  = " << prop.major << "." << prop.minor << std::endl;
       std::cout << "SM count            = " << prop.multiProcessorCount << std::endl;
       std::cout << "concurrentKernels   = " << prop.concurrentKernels << std::endl;
       std::cout << "clockRate (kHz)     = " << prop.clockRate << std::endl;
       return 0;
   }
   ```

2. 编译（不需要 GPU，运行才需要）：`nvcc -o myprops myprops.cu`。
3. 运行 `./myprops`，记下 `concurrentKernels` 与 `clockRate`。

**需要观察的现象**：`concurrentKernels` 为 1；`clockRate` 换算成 GHz 后与 `nvidia-smi` 显示的主频同量级。

**预期结果**：现代 NVIDIA GPU 上 `concurrentKernels = 1`。若你拿到的是极老的卡（计算能力 < 2.0），值为 0，此时 Conkernels 的并发版也将退化为串行。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 [L42-L49](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L42-L49) 的检查，程序在不支持并发的设备上会怎样？

**参考答案**：不会出错——多条流仍然存在，只是硬件一次只让一个 kernel 执行，多流自动退化为串行（与 `FORCE_SERIALIZED` 版的行为类似），`Total measured execution time` 会接近 400 ms。检查的价值在于**提前向用户解释**为什么看不到并发收益，而不是保证正确性。

**练习 2**：`prop.concurrentKernels` 只告诉你"能不能并发"，不告诉你"最多几个 kernel 同时跑"。结合 [L51-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L51-L52) 的注释，怎么用这个程序测出上限？

**参考答案**：不断调大 `NUM_KERNELS`（4、8、16、32……）重新编译运行，观察 `Total measured execution time`：一旦它不再维持在"约一个 kernel 的时长"而是开始成倍增长，说明同时驻留的 kernel 数到达上限，多出的 kernel 在排队。这正是 [L51](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L51) 注释"change this value to find the maximum number of concurrent kernels supported"的含义。

### 4.3 stream 创建与管理：并发发射、事件依赖与汇合

#### 4.3.1 概念说明

这是本讲的核心模块。程序一共创建 **9 条流**：

- `kernel_streams[0..7]`：8 条"生产者"流，每条承载一个 `timed_kernel`。
- `time_compute_stream`：1 条"消费者"流，承载汇总 kernel `sum_clocks` 和两次异步回拷，它必须等 8 个计时 kernel **全部**完成后才能开跑。

配套的同步设施有三类：

1. **kernel 事件（`kernel_events[k]`）**：用 `cudaEventCreateWithFlags(..., cudaEventDisableTiming)` 创建——这些事件只用来表达"完成与否"的依赖关系，不需要计时功能，禁用计时开销更低。与之相对，`start`/`stop` 两个事件是**计时事件**，用普通 `cudaEventCreate` 创建，最后用 `cudaEventElapsedTime` 求差。
2. **`cudaStreamWaitEvent`**：把"等某个事件"这一步排进 `time_compute_stream`，实现 join。
3. **`cudaEventRecord(stop, 0)` + `cudaEventSynchronize(stop)`**：stop 事件记录在默认流（`0`）上。传统语义的默认流对其余（阻塞型）流有隐式同步作用，因此 stop 会在**所有流的工作**完成后才触发；host 端再对 stop 做一次同步，就得到了完整的栅栏。源码注释（[L133-L135](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L133-L135)）正是这样解释的。

另外两个配套概念：

- **页锁定主机内存**：`cudaHostAlloc(..., cudaHostAllocPortable)` 分配的内存不会被操作系统换出，异步拷贝（`cudaMemcpyAsync`）用它才能真正与计算重叠。源码注释（[L85-L86](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L85-L86)）写"async 操作*总是*需要页锁定内存"——严格说，用普通分页内存的 `cudaMemcpyAsync` 会退化为经中转缓冲的同步式行为，并非非法，但失去了异步意义；作者的表述是工程上的简化。
- **kernel 启动的第 4 个参数**：u2-l1 里我们用 `kernel<<<grid, block>>>` 两参数形式；完整形式是 `<<<gridDim, blockDim, sharedMemBytes, stream>>>`。本讲首次用第 4 个参数把 kernel 指派到指定流——这正是"多流并发"的发射入口。

#### 4.3.2 核心流程

并发版（默认编译）的完整时间线：

```text
host 侧：
  cudaEventRecord(start, 0)                 # 计时起点
  for k in 0..7:
      timed_kernel<<<1,1,0, kernel_streams[k]>>>(...)   # 第 k 个 kernel 进第 k 条流
      cudaEventRecord(kernel_events[k], kernel_streams[k])  # 在该流打完成标记
      cudaStreamWaitEvent(time_compute_stream, kernel_events[k], 0)  # 汇合流等它
  sum_clocks<<<1,32,0, time_compute_stream>>>(...)       # 等全部事件后执行
  cudaMemcpyAsync(clock_sum/clocks ← dev, ..., time_compute_stream)   # 结果回拷
  cudaEventRecord(stop, 0)                  # 默认流：等一切完成后触发
  cudaEventSynchronize(stop)                # host 阻塞到 stop
  cudaEventElapsedTime(&elapsed, start, stop)

设备侧（理想并发时）：
  stream[0]: ──kernel0 (≈50ms)──┐
  stream[1]: ──kernel1 (≈50ms)──┤  8 个 kernel 时间上重叠
     ...                        │
  stream[7]: ──kernel7 (≈50ms)──┘
                                └─→ sum_clocks → D2H 回拷
  总墙钟 ≈ 50ms + 少量汇总开销
```

串行版（`-DFORCE_SERIALIZED`，见 4.4 节）的时间线：

```text
  stream[0]: ─k0─┬─k1─┬─k2─┬─k3─┬─k4─┬─k5─┬─k6─┬─k7─┐  同一条流 FIFO 排队
                                                        └─→ sum_clocks → 回拷
  总墙钟 ≈ 8 × 50ms = 400ms
```

观察并发程度的指标可以从输出直接算出：

\[ C = \frac{\text{Sum of kernel execution times}}{\text{Total measured execution time}} \]

- 串行版：\( C \approx 1 \)（400 ms / 400 ms）。
- 并发版理想情况：\( C \approx 8 \)（400 ms / 50 ms）。

#### 4.3.3 源码精读

事件与流的容器声明——注意 kernel 事件和 kernel 流都是长度为 `NUM_KERNELS` 的 `std::vector`：

[Conkernels/concurrentKernels.cu:L57-L60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L57-L60)——声明计时事件 `start`/`stop`、每 kernel 一个的 `kernel_events`、汇总用的 `time_compute_stream` 与每 kernel 一条的 `kernel_streams`。

创建计时事件：

[Conkernels/concurrentKernels.cu:L62-L64](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L62-L64)——`cudaEventCreate` 创建 `start`/`stop`，这对事件最终用于 `cudaEventElapsedTime` 计时。

创建 kernel 事件（禁用计时）：

[Conkernels/concurrentKernels.cu:L66-L71](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L66-L71)——循环用 `cudaEventCreateWithFlags(&e, cudaEventDisableTiming)` 创建 8 个 kernel 事件；它们只承担依赖同步职责，禁用计时更省开销。

创建 9 条流：

[Conkernels/concurrentKernels.cu:L73-L80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L73-L80)——先建 `time_compute_stream`（注释说明它必须等所有 kernel 事件），再循环建 8 条 `kernel_streams`。

页锁定主机内存与设备内存：

[Conkernels/concurrentKernels.cu:L82-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L82-L92)——`cudaHostAlloc`（带 `cudaHostAllocPortable`）分配页锁定主机内存存各 kernel 周期数及其总和（供之后的异步回拷），`cudaMalloc` 分配对应的设备内存；L82 的 TODO 注释提到可换用带页锁定分配器的 `std::vector`。

发射循环（并发分支）——本讲最核心的 7 行：

[Conkernels/concurrentKernels.cu:L110-L116](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L110-L116)——`#else` 分支（默认编译路径）：第 k 个 `timed_kernel` 以 `<<<1,1,0, kernel_streams[k]>>>` 发射进第 k 条流（第 4 个启动参数就是流）；随后在该流记录 `kernel_events[k]`，并让 `time_compute_stream` 用 `cudaStreamWaitEvent` 等待它——三次调用逐 kernel 重复，构建出"8 条生产者流 + 1 条汇合流"的依赖网。

汇总与回拷，全部排进汇合流：

[Conkernels/concurrentKernels.cu:L122-L125](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L122-L125)——`sum_clocks<<<1,32,0, time_compute_stream>>>` 求和，随后两次 `cudaMemcpyAsync` 把总和与各 kernel 周期数异步拷回主机（页锁定内存）。它们排在同一条流里，自然先求和后回拷。

终止事件与同步：

[Conkernels/concurrentKernels.cu:L127-L138](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L127-L138)——`cudaEventRecord(stop, 0)` 把 stop 记在默认流上（注释解释：默认流不专属于任何一条流，因此它在*所有*流的事件之后才触发）；`cudaEventSynchronize(stop)` 让 host 等到 stop 触发，即等到全部 GPU 工作结束；`cudaEventElapsedTime` 算出 start↔stop 之间的 GPU 时间线时长。

结果输出——读懂每行含义：

[Conkernels/concurrentKernels.cu:L140-L148](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L140-L148)——依次打印 GPU 主频（`clockRate×1e-6` 转 GHz）、kernel 数量、目标时长、实际最大单 kernel 时长（`max_element`）、各 kernel 耗时之和、GPU 时间线总时长（`elapsed_time`）与 host CPU 耗时。

资源释放与收尾：

[Conkernels/concurrentKernels.cu:L150-L171](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L150-L171)——销毁 8 个事件、销毁 9 条流、`cudaFreeHost` 释放页锁定内存、`cudaFree` 释放设备内存，最后 `cudaDeviceReset()`；L169-L170 注释提到调用它是为了让 profiler/tracer 显示完整轨迹（呼应 u1-l4 的 nvprof）。

#### 4.3.4 代码实践

**实践目标**：用 profiler 直观"看见"并发版与串行版时间线的差异，而不是只看程序自己的打印。

**操作步骤**：

1. 按 4.4 节先编译出并发版与串行版两个可执行文件。
2. 对并发版执行时间线采集（nvprof 已被新版工具链取代，二选一）：
   - 老工具：`nvprof ./concurrentKernels`
   - 新工具：`nsys profile -o conc ./concurrentKernels && nsys stats conc.nsys-rep`
3. 对串行版重复上一步。
4. 查看两种工具输出的 kernel 时间轴（nsys 用 `nsys stats --report cuda_gpu_trace` 或 GUI）。

**需要观察的现象**：并发版中 8 个 `timed_kernel` 的时间条**在同一横轴区间内重叠**，随后紧跟一个 `sum_clocks`；串行版中 8 个时间条**首尾相接**排成一列。

**预期结果**：并发版总 GPU 时长约 50 ms 量级、串行版约 400 ms 量级，与 4.4 节程序打印的 `Total measured execution time` 一致。待本地验证（本目录无 `test.sh` 与归档输出，需要自己在有 GPU 的机器上采集）。

#### 4.3.5 小练习与答案

**练习 1**：`kernel_events` 用 `cudaEventDisableTiming` 创建，而 `start`/`stop` 用普通 `cudaEventCreate`。为什么两处不同？

**参考答案**：事件的用途分两类。`kernel_events` 只表达依赖（"这条流完成了吗"），`cudaStreamWaitEvent` 只关心触发与否，禁用计时可以省掉计时硬件的开销；`start`/`stop` 的唯一用途是事后 `cudaEventElapsedTime` 求差，必须保留计时功能。

**练习 2**：`cudaEventRecord(stop, 0)` 里的 `0` 是什么意思？如果程序改用每线程默认流（`cudaStreamPerThread`）来记录 stop，还安全吗？

**参考答案**：`0` 指传统（legacy）默认流，它对其余阻塞型流有隐式同步作用，因此排在 stop 之前的所有流的工作都会先完成，stop 触发即"万事俱毕"。若换成 `cudaStreamPerThread`（每线程默认流**不**与其他流隐式同步），stop 只对当前线程的默认流负责，可能先于其他流的工作触发，`cudaEventSynchronize(stop)` 就不再是完整栅栏，测得的总时长可能偏小。

**练习 3**：程序最后 `cudaMemcpyAsync` 回拷到 `clocks`/`clock_sum` 后，host 端直接读取它们打印（[L144-L146](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L144-L146)）。host 读的时候回拷一定完成了吗？

**参考答案**：完成了。回拷排在 `time_compute_stream` 里，之后 host 走到 `cudaEventRecord(stop, 0)` 与 `cudaEventSynchronize(stop)`；由于传统默认流的隐式同步，stop 触发时所有流（含 `time_compute_stream` 的回拷）都已完成，`cudaEventSynchronize` 返回后读 `clocks` 是安全的。这也解释了为什么异步拷贝的目标必须用页锁定内存分配。

### 4.4 两种 Makefile：一个 `-DFORCE_SERIALIZED` 宏制造的对照实验

#### 4.4.1 概念说明

微基准的方法论是控制变量：并发版与串行版的**源码必须完全相同**，唯一差异应该就是"kernel 是否排进不同流"。Conkernels 用 `#ifdef FORCE_SERIALIZED` + 两份 Makefile 实现了这一点：

- `make`（用 `Makefile`）→ 并发版：每个 kernel 进自己的流。
- `make -f Makefile_serialized` → 串行版：所有 kernel 排进 `kernel_streams[0]` 这同一条流，流内 FIFO 自然串行。

注意串行化的手段**不是**"销毁其他流"，而是"全部用同一条流"——这恰好印证了 2.1 节的规则：同一条流内操作按序执行。两份 Makefile 的差异经 `diff` 核验只有两处 `-DFORCE_SERIALIZED`（见下），其余 330 余行逐字相同。

顺带认识这套 CUDA Samples 风格的 Makefile 工程（u6-l3 会专门展开）：它按 `SMS` 变量为每种 SM 架构生成 `-gencode`，并额外生成最高架构的 PTX 以向前兼容；编译产物还会被复制到 `../../bin/$(TARGET_ARCH)/$(TARGET_OS)/$(BUILD_TYPE)`——注意从 `Conkernels` 出发 `../../bin` 在仓库**外面**（Samples 原始目录层级有两层，本仓库拍平了），运行 `make` 会在仓库上级创建 `bin/` 目录，属预期副作用。

#### 4.4.2 核心流程

```text
make -f Makefile            →  nvcc（无宏）      →  #else 分支   →  8 流并发
make -f Makefile_serialized →  nvcc -DFORCE_SERIALIZED →  #ifdef 分支 →  全进 stream[0] 串行
```

`#ifdef` 两个分支的行为对比：

| | 并发分支（`#else`） | 串行分支（`#ifdef FORCE_SERIALIZED`） |
| --- | --- | --- |
| kernel 发射流 | `kernel_streams[k]`（每 kernel 一条） | `kernel_streams[0]`（全部同一条） |
| 事件记录 | 每个 kernel 后记录 `kernel_events[k]` | 仅在最后一个 kernel 后记录 `kernel_events[0]` |
| `cudaStreamWaitEvent` 次数 | 8 次（逐 kernel） | 1 次（最后一 kernel 后） |
| 预期 `Total measured execution time` | ≈ 50–60 ms | ≈ 400 ms |
| 预期 `Sum of kernel execution times` | ≈ 400 ms（两版相同） | ≈ 400 ms |

#### 4.4.3 源码精读

发射循环的串行分支：

[Conkernels/concurrentKernels.cu:L98-L109](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L98-L109)——`#ifdef FORCE_SERIALIZED` 分支：所有 `timed_kernel` 都发射进 `kernel_streams[0]`（同一条流 FIFO 串行），只在 `k == NUM_KERNELS-1` 时记录一次事件并让汇合流等待——由于流内顺序性，这一个事件就代表全部 8 个 kernel 完成。

并发分支见 4.3.3 精读的 [L110-L116](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L110-L116)；两 branch 仅以 `#ifdef/#else` 相隔，源码一字不差地共存于同一文件，这正是"一套源码、一个宏、两份 Makefile"的对照设计。

Makefile 之一处差异——`NVCC` 变量定义追加宏：

[Conkernels/Makefile:L160-L160](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L160) 与 [Conkernels/Makefile_serialized:L160-L160](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile_serialized#L160)——对比可见串行版在 `NVCC := $(CUDA_PATH)/bin/nvcc -ccbin $(HOST_COMPILER)` 末尾多了 `-DFORCE_SERIALIZED`。

Makefile 之二处差异——编译规则再补一次宏（冗余但无害）：

[Conkernels/Makefile:L321-L321](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L321) 与 [Conkernels/Makefile_serialized:L321-L321](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile_serialized#L321)——串行版的编译命令行末尾也追加了 `-DFORCE_SERIALIZED`；由于 L160 的 `NVCC` 变量已含该宏，这里属于"双保险"式的重复定义，真正起作用的是编译期宏。

架构清单与多架构生成（两份 Makefile 相同）：

[Conkernels/Makefile:L280-L300](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L280-L300)——x86_64 默认 `SMS ?= 35 37 50 52 60 61 70 75`，对每种 SM 生成 `-gencode arch=compute_X,code=sm_X`，再对最高架构生成 PTX 保前向兼容。注意较新 CUDA 工具链（12.x）已移除 sm_35/37/50 等老旧目标的编译支持，直接 `make` 可能报错，需用 `make SMS="80 86"`（按你的 GPU 架构）覆盖；Conkernels 的 README 也标注本示例针对 CUDA 11.1、支持 SM 3.5–8.6（[Conkernels/README.md:L11-L27](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/README.md#L11-L27)）。

编译、链接与产物复制：

[Conkernels/Makefile:L320-L329](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L320-L329)——`concurrentKernels.o` 由 nvcc `-c` 编出，再链接为 `concurrentKernels`，复制到 `../../bin/...`（如上所述会写到仓库上级目录）；`make run` = 编译加运行。两份 Makefile 产出的可执行文件**同名**，做对比实验时记得先改名保存。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手完成"并发 vs 串行"对照实验，并观察 kernel/流数量对并发收益的影响。

**操作步骤**：

1. **确认差异**：在 `Conkernels/` 下执行
   `diff --strip-trailing-cr Makefile Makefile_serialized`
   核实只有 L160、L321 两处 `-DFORCE_SERIALIZED` 差异（以及文件末尾换行差异）。
2. **编译并发版**：
   `make SMS="80"`（把 `80` 换成你 GPU 的计算能力，如 `86`、`89`；旧工具链可省略 `SMS`）
   编译成功后 `cp concurrentKernels concurrent.bin` 保存。
3. **编译串行版**：`make clean && make -f Makefile_serialized SMS="80"`，然后 `cp concurrentKernels serialized.bin`。
4. **对比运行**：分别执行 `./concurrent.bin` 与 `./serialized.bin`，把输出的 7 行（尤其 `Sum of kernel execution times` 与 `Total measured execution time`）抄成对照表，计算并发度 \( C = \text{Sum}/\text{Total} \)。
5. **数量减半/加倍**：把 [Conkernels/concurrentKernels.cu:L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L52) 的 `NUM_KERNELS` 分别改为 4 和 16（`kernel_events`、`kernel_streams` 等容器都由它定长，会自动跟随；`NUM_CLOCKS` 亦然），重复步骤 2–4。`sum_clocks` 的单 block 32 线程归约对 `NUM_KERNELS ≤ 32` 都适用，不要超过 32。
6. 若无 GPU 环境，退化为源码阅读实践：完成步骤 1 的 diff，然后只做预测（下一项的预期结果表），并注明"待本地验证"。

**需要观察的现象**：两版输出中 `Sum of kernel execution times` 几乎相同（每个 kernel 都各自忙等约 50 ms），差别集中在 `Total measured execution time`；`NUM_KERNELS` 增大后，串行版 Total 按比例线性增长，并发版 Total 基本维持不变（直到超出可同时驻留的 kernel 数）。

**预期结果**（均为理论预测，**待本地验证**）：

| 版本 | `Sum of kernel execution times` | `Total measured execution time` | 并发度 C |
| --- | --- | --- | --- |
| 并发版 N=4 | ≈ 200 ms | ≈ 50–60 ms | ≈ 3–4 |
| 并发版 N=8 | ≈ 400 ms | ≈ 50–60 ms | ≈ 7–8 |
| 并发版 N=16 | ≈ 800 ms | ≈ 50–150 ms（视设备上限） | 介于 5–16 |
| 串行版 N=8 | ≈ 400 ms | ≈ 400 ms | ≈ 1 |

若你的 GPU 同时驻留的 kernel 数不足 16，`N=16` 的并发版 Total 会明显超过 50 ms——这正好把"最大并发 kernel 数"这个硬件属性测出来了。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接写两份 `.cu` 源文件（一份并发、一份串行），而要用 `#ifdef` + 两份 Makefile？

**参考答案**：微基准的可信度来自"只改一个变量"。两份源文件难免在维护中漂移（改了一处忘了另一处），对照就混入了无关差异；单源码 + 编译期宏保证两版的每行代码都逐字相同，唯一差异就是宏控制的发射方式，且 `diff Makefile Makefile_serialized` 可以随时审计这个差异。

**练习 2**：串行版把所有 kernel 发进 `kernel_streams[0]`。既然只用一条流，程序为什么还创建了 9 条流？

**参考答案**：创建流的代码（[L77-L80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L77-L80)）不区分版本，串行版只是**使用了**其中一条；多出来的流空着不用，只浪费极小的创建/销毁开销。这也是"两版代码路径尽量重合"设计的自然结果。

**练习 3**：`make` 成功后仓库上级目录多了个 `bin/`，里面也有一个 `concurrentKernels`。这会给你对比两版带来什么坑？

**参考答案**：两份 Makefile 的链接产物同名（`concurrentKernels`），且都被复制到同一个 `../../bin/...` 路径——第二次构建会覆盖第一次的产物，本地目录的 `concurrentKernels` 也会被覆盖。所以步骤 2/3 中每编译完一版要立即 `cp` 改名保存（或干脆只依赖改名后的副本），否则你对比的可能是同一版本跑两次。

## 5. 综合实践

**任务：把"8 条流"改造成"2 条流"，并定量预测与实测。**

这个任务串起本讲全部三个模块：设备属性（4.2）、流与事件依赖（4.3）、对照实验方法（4.4）。

1. **改造**：在 [Conkernels/concurrentKernels.cu:L111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L111) 把发射流从 `kernel_streams[ k ]` 改为 `kernel_streams[ k % 2 ]`（事件记录同样改为 `kernel_events[ k % 2 ]`，或在每条流最后一个 kernel 后记录，注意保持"汇合流要等到**全部** kernel 完成"——最简单的做法：保留逐 kernel 的事件记录与等待，只是流下标取模）。
2. **预测**：8 个 kernel 轮流进 2 条流，每条流 4 个 kernel 串行，两条流并发。写出你预期的 `Total measured execution time`（提示：约 \( 4 \times 50 = 200 \) ms）与并发度 \( C \)。
3. **实测**：按 4.4.4 的流程编译运行，与预测对照；再用 `nsys`/`nvprof` 看时间轴，应看到两条流各自串行 4 段、两条流之间重叠。
4. **扩展一步**：把发射配置从 `<<<1,1,0,stream>>>` 改为 `<<<1,512,0,stream>>>`（512 线程的忙等 kernel，需相应把 kernel 内写入 `clocksArray` 的线程改为 `threadIdx.x==0` 的分支或保持每线程写同一下标会冲突——建议直接只让 0 号线程忙等写入，其余线程立即退出），重新观察 8 流并发版：Total 是否仍接近 50 ms？用"每个 kernel 占用的资源变大，可同时驻留的 kernel 变少"解释你的观察。
5. **结论**：写三五句话总结——决定多流并发收益的两个要件（硬件并发能力 + 每 kernel 资源足迹），以及 `Sum/Total` 这个并发度指标怎么用。

全部运行结果**待本地验证**（本目录无 `test.sh`，也无归档输出可参照）。

## 6. 本讲小结

- **stream 是显式的任务队列**：同一条流内严格 FIFO，不同流之间可并发；让多个 kernel 并发，靠的是把 `kernel<<<grid, block, shmem, stream>>>` 的第 4 个参数指向不同的流。
- **先查能力再实验**：`cudaGetDeviceProperties` 的 `concurrentKernels` 字段回答"能不能"，`clockRate` 顺便提供了忙等 kernel 周期换算的基准；不支持时程序打印警告并优雅降级为串行。
- **`timed_kernel` 是受控负载发生器**：设备端 `clock()` 忙等指定周期数（ticks = 主频 kHz × 目标 ms），`<<<1,1>>>` 把每个 kernel 的资源足迹压到最小，从而把"流并发性"隔离为唯一变量；`sum_clocks` 用单 block 共享内存树形归约汇总结果。
- **跨流依赖 = 事件 + 等待**：`cudaEventRecord(kernel_events[k], stream_k)` 打完成标记，`cudaStreamWaitEvent(time_compute_stream, ...)` 逐个排队等待，构成 join；`cudaEventRecord(stop, 0)` 借传统默认流的隐式同步成为全局栅栏，`cudaEventSynchronize(stop)` 后 host 读页锁定内存里的异步回拷结果才安全。
- **对照实验由一个宏切换**：`Makefile` 与 `Makefile_serialized` 仅差 `-DFORCE_SERIALIZED`（L160、L321 两处），源码里 `#ifdef` 分支把全部 kernel 发进同一条流即得串行基线；两版 `Sum` 相同、`Total` 相差约 8 倍，并发度 \( C = \text{Sum}/\text{Total} \)。
- **工程细节**：两份 Makefile 产物同名且会被复制到仓库外的 `../../bin/...`，对比前先改名；默认 `SMS` 里的老旧架构在新工具链上需用 `make SMS="80"` 之类覆盖。

## 7. 下一步学习建议

- 下一讲 **u3-l4（TaskGraph：CUDA Graph 与共轭梯度求解器）** 继续本单元主题：当"多个 kernel 的提交顺序"固定且反复执行时，CUDA Graph 用"一次捕获、多次整体启动"替代逐个 kernel/逐流提交，进一步削减 CPU 侧启动开销——可以把本讲的"8 条流 + 事件依赖网"理解为将被 capture 成图的那种工作负载。
- 回顾 **u2-l3** 的默认流隐式排序与本讲 4.3 的传统默认流栅栏语义，两处对照着读会加深理解。
- 想深入流与事件的官方语义，可阅读 CUDA C++ Programming Guide 中 *Asynchronous Concurrent Execution* 一章（Streams、Events、Host Synchronization 节），以及 `cudaStreamWaitEvent`、`cudaEventCreateWithFlags` 的 API 文档。
- 结合 **u1-l4**：用 `nsys` 时间轴（或老工具 `nvprof`）看本讲两版程序，是"眼见为实"理解并发最直接的方式；`cudaDeviceReset` 对轨迹完整性的作用（[L169-L171](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L169-L171)）在 profiler 实验时值得留意。
