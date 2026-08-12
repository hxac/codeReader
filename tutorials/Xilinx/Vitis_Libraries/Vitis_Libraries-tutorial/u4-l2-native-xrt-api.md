# 原生 XRT C++ API（xrt::device/kernel/bo/run）

## 1. 本讲目标

本讲承接 [u4-l1（xcl2 OpenCL 主机辅助库）](u4-l1-xcl2-helper.md)。上一讲我们学了用 **OpenCL** 抽象层（`cl::Device`/`cl::Program`/`cl::Kernel`）写主机程序；这一讲我们换成 **原生 XRT C++ API**，即 `xrt::device`、`xrt::kernel`、`xrt::run`、`xrt::bo`。

学完本讲，你应当能够：

1. 说出原生 XRT C++ API 与 OpenCL 路线的本质区别，以及它为什么是 AIE/嵌入式场景的首选。
2. 独立写出「加载 xclbin → 取内核 → 建 run → 设参 → 启动 → 等待 → 取回结果」这条完整的主机控制链。
3. 理解 `start()` 与 `wait()` 的并发含义，知道多个内核如何在没有主机线程干预的情况下并发执行。
4. 理解 `xrt::bo`（buffer object）如何借助 `group_id` 挂到正确的 DDR/HBM bank 上。

本讲全部以 `dsp/L2/examples/vss_fft_ifft_1d/host.cpp` 这个真实主机程序为蓝本。

## 2. 前置知识

在进入源码前，先用通俗语言建立四个直觉。

**XRT 是什么。** XRT（Xilinx RunTime）是运行在主机侧（x86 或嵌入式 ARM）的运行时库。它负责把「主机想做的事」翻译成「FPGA / AI Engine 上的实际动作」——加载比特流、分配设备内存、启动内核、搬运数据。XRT 提供两套等价的 API：一套是符合开放标准的 **OpenCL**，另一套是 Xilinx 自己的 **原生 C++ API**（本讲的主角）。两者底层调用同一套驱动，功能基本等价，选哪套主要是风格与能力边界的问题。

**原生 API 相对 OpenCL 的优势。** OpenCL 的抽象（context/queue/program/event）更通用，但对 AIE 数据流图（ADF graph）这种 Versal 专有的概念没有原生表达。原生 XRT API 提供了 `xrt::graph`、`xrt::ip` 等类型，可以直接驱动 AIE 图与 PL IP。因此凡涉及 AIE 的例子（本讲的 `vss_fft_ifft_1d` 就是 PL+AIE 混合系统），主机几乎一律用原生 XRT API。`host.cpp` 顶部的两组头文件正反映了这一点：

```cpp
#include <xrt/xrt_device.h>   // 设备、xclbin
#include <xrt/xrt_kernel.h>   // 内核、run、bo（本讲重点）
#include <experimental/xrt_aie.h>
#include <experimental/xrt_graph.h>  // AIE 图（OpenCL 没有）
#include <experimental/xrt_ip.h>
```

**设备、内核、run 的关系（核心心智模型）。** 可以这样类比：

| XRT 概念 | 通俗类比 | 说明 |
|---|---|---|
| `xrt::device` | 一块加速卡 | 物理设备句柄，所有操作的根 |
| `load_xclbin` | 给卡「烧」一份电路 | 把编译好的 `.xclbin` 加载到设备，返回 uuid 作为这次加载的标识 |
| `xrt::kernel` | 电路里的一个「函数槽」 | 描述某个内核的签名（有哪些参数），是静态的 |
| `xrt::run` | 「调用一次这个函数」 | 一次具体执行：绑定参数值、可以 start/wait，是动态的 |
| `xrt::bo` | 设备内存里的一块缓冲 | 主机与设备交换数据的载体 |

关键点：**kernel 是「定义」，run 是「调用」**。一个 kernel 可以创建多个 run（并发执行多次），每个 run 绑定不同的参数。

**bo 与存储分区。** `xrt::bo` 是设备侧内存的抽象（DDR/HBM 上的一块）。创建 bo 时要指定它挂在哪个 **memory bank/group** 上，这决定了数据物理落在哪片 DDR，从而决定带宽。`kernel.group_id(arg_index)` 返回某个参数对应的 group 编号——把 bo 建在这个 group 上，才能保证内核的那个参数确实指向这片内存。

> 如果你还没读过 [u4-l1](u4-l1-xcl2-helper.md) 的 `aligned_allocator` 与 `find_binary_file`，建议先读。本讲里 `host.cpp` 不再使用 xcl2，而是把 xclbin 名字硬编码为 `kernel.xclbin`，并由 `example.mk` 在打包阶段固定下来。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp) | 本讲主蓝本：用原生 XRT API 驱动 mm2s/s2mm 内核与 AIE 图 |
| [dsp/L2/examples/vss_fft_ifft_1d/example.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk) | 构建 host.elf 的交叉编译命令，链接 `-lxrt_coreutil`/`-ladf_api_xrt` |
| [dsp/L2/examples/vss_fft_ifft_1d/system.cfg](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg) | 连接描述：定义 mm2s/s2mm 内核实例、存储端口（LPDDR）、PL↔AIE 流连接 |

辅助理解（非本讲重点，但端到端会用到）：`my_params.cfg`（AIE 图参数）、`run_script.sh`（嵌入式启动脚本）。

## 4. 核心概念与源码讲解

本讲按主机控制链拆成四个最小模块：**设备与 xclbin** → **内核** → **run（启动/等待）** → **bo（缓冲）**。它们的顺序正好是 `host.cpp` 的执行顺序。

### 4.1 xrt::device 与 load_xclbin

#### 4.1.1 概念说明

一切从「拿到设备」开始。`xrt::device` 代表一块加速卡（数据中心 Alveo 卡或 Versal 嵌入式平台）。拿到设备后，第一步是把编译好的电路——`.xclbin` 文件——加载进去。`.xclbin` 是一个容器，里面同时封装了 PL 内核（mm2s/s2mm）、AIE 图（`libadf.a` 的产物）以及它们的连接关系。

`load_xclbin` 返回一个 **uuid**，它是「这次加载」的唯一标识。后续创建 kernel / graph 时都要带上这个 uuid，告诉 XRT「从这次加载的电路里找内核」。

#### 4.1.2 核心流程

```
xrt::device(index)            # 按序号打开第 index 块卡
   │
   └─ load_xclbin("kernel.xclbin")
          │
          └─ 返回 xclbin_uuid   # 后续所有对象的「户口本」
```

两步即可完成「上电 + 烧电路」。注意设备用**序号**（`dev_index = 0`）打开，而不是像 OpenCL 那样先枚举 `cl::Platform` 再取 `cl::Device`——原生 API 把这些步骤折叠掉了。

#### 4.1.3 源码精读

加载 xclbin 的代码在 `host.cpp` 的 "Load XCLBIN" 段：

```cpp
char xclbinFilename[] = "kernel.xclbin";
unsigned dev_index = 0;
auto my_device = xrt::device(dev_index);                  // 打开第 0 块设备
auto xclbin_uuid = my_device.load_xclbin(xclbinFilename); // 烧电路，拿 uuid
```

这段代码做了三件事：固定 xclbin 文件名（由打包阶段生成）、打开 0 号设备、加载 xclbin 得到 uuid。详见 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp:110-116](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L110-L116)。

> 对比上一讲的 `xcl::get_xil_devices()`：那里要先拿到设备向量再构造 `cl::Device`；这里 `xrt::device(0)` 一步到位。文件名也不再走 `find_binary_file` 的多级搜索，因为嵌入式打包时 `example.mk` 已把 `kernel.xclbin` 放在与 host 同目录（见 `run_script.sh` 在 SD 卡上直接 `./host.elf`）。

#### 4.1.4 代码实践

**实践目标**：理解「序号打开设备」与「文件名加载 xclbin」这两个最小动作。

**操作步骤**（源码阅读型，无需硬件）：

1. 打开 [host.cpp:110-116](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L110-L116)。
2. 假设机器上有两块卡，回答：要把程序改到第二块卡上跑，需要改哪个变量？
3. 在 [example.mk 的 `example_sd_card` 目标](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L40-L42)里找到 `--package.kernel_image` 与生成的 `kernel.xclbin`，确认这个文件名就是 `host.cpp` 里硬编码的名字。

**预期结果**：改 `dev_index` 为 `1` 即可切换设备；`example.mk` 第 42 行 `v++ ... -o kernel.xclbin` 产出的文件名与 `host.cpp` 第 110 行字面量一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `load_xclbin` 的返回值（uuid）要保留下来传给后面的 `xrt::kernel` / `xrt::graph`？

**答案**：因为同一台设备上可能先后加载过多个 xclbin，uuid 用来精确指明「从哪一次加载的电路里查找内核/图」。不带 uuid，XRT 无法区分同名内核属于哪次加载。

**练习 2**：如果 `kernel.xclbin` 不在 host.elf 的工作目录下，程序会怎样？

**答案**：`load_xclbin` 会因找不到文件而抛异常（原生 API 不像 xcl2 那样做多级路径搜索）。所以必须靠打包阶段（`example_sd_card`）把 xclbin 与 host 放到同一目录。

---

### 4.2 xrt::kernel：拿到一个内核句柄

#### 4.2.1 概念说明

xclbin 里可能有多个内核（本例就有 `mm2s_wrapper` 与 `s2mm_wrapper` 两个 PL 搬运内核，外加一个 AIE 图）。`xrt::kernel` 就是从 xclbin 里「按名字取出一个内核」得到的句柄。

它描述的是内核的**静态信息**：函数签名、有哪些参数、每个参数挂到哪个存储端口。它本身还**不是一次执行**——要执行，还得再创建一个 `xrt::run`（见 4.3）。

#### 4.2.2 核心流程

```
xrt::kernel(device, uuid, "名字")   # 取内核
        │
        ├── group_id(arg_index)       # 查：第 arg_index 个参数挂在哪个存储 bank
        └── 后续用 xrt::run(kernel)   # 创建一次执行（4.3）
```

内核名的格式是 `<内核函数名>:<实例名>`，例如 `mm2s_wrapper:{mm2s}`。其中 `{mm2s}` 是**实例名**，由 `system.cfg` 的 `nk` 行定义——这允许同一个内核函数在系统里有多个副本（多实例），靠实例名区分。

#### 4.2.3 源码精读

取出两个搬运内核的代码在 "Load and Start DDR Source/Sink PL Kernels" 段：

```cpp
auto mm2s = xrt::kernel(my_device, xclbin_uuid, "mm2s_wrapper:{mm2s}");
auto s2mm = xrt::kernel(my_device, xclbin_uuid, "s2mm_wrapper:{s2mm}");
```

详见 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp:131-135](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L131-L135)。

- `mm2s`（memory-mapped to stream）：把 DDR 里的数据搬成 AXI 流，喂给 AIE 图的入口。
- `s2mm`（stream to memory-mapped）：把 AIE 图出口的 AXI 流收回，写回 DDR。

这两个名字的「实例名」部分（`{mm2s}`/`{s2mm}`）对应 `system.cfg` 里的实例声明：

```
nk = mm2s_wrapper:1:mm2s     # 1 个 mm2s_wrapper 实例，实例名叫 mm2s
nk = s2mm_wrapper:1:s2mm
```

详见 [system.cfg:10-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L10-L11)。也就是说，主机里的 `{mm2s}` 必须和 `system.cfg` 的实例名严格对齐，否则 XRT 找不到内核。

#### 4.2.4 代码实践

**实践目标**：理解「内核函数名」与「实例名」的对应关系。

**操作步骤**：

1. 打开 [system.cfg:10-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L10-L11)，读 `nk = mm2s_wrapper:1:mm2s` 这一行的三段含义：函数名=`mm2s_wrapper`、数量=`1`、实例名=`mm2s`。
2. 对照 [host.cpp:131](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L131)，确认 `"mm2s_wrapper:{mm2s}"` 的两段分别来自 `system.cfg` 的哪一部分。
3. 思考：如果 `system.cfg` 改成 `nk = mm2s_wrapper:1:my_mm2s`，`host.cpp` 要怎么改？

**预期结果**：主机侧 `"函数名:{实例名}"` 必须与 `system.cfg` 的 `nk` 行一致；改实例名后 `host.cpp` 第 131 行要改成 `"mm2s_wrapper:{my_mm2s}"`。

#### 4.2.5 小练习与答案

**练习 1**：`xrt::kernel` 对象本身会触发内核执行吗？

**答案**：不会。`xrt::kernel` 只是「内核的元信息句柄」（签名、参数到存储端口的映射）。真正执行要靠 `xrt::run`（下一节）。

**练习 2**：为什么内核名字里要带 `{mm2s}` 这种实例名，而不是直接用 `mm2s_wrapper`？

**答案**：因为同一个内核函数可能在设计里被实例化多次（例如 4 个并行的 mm2s）。实例名用来在多副本中唯一指认某一个。即便本例只有 1 个，也必须按 `函数名:{实例名}` 的格式写全。

---

### 4.3 xrt::run：set_arg / start / wait（本讲核心）

#### 4.3.1 概念说明

`xrt::run` 是本讲最重要的类型。如果说 `xrt::kernel` 是「函数声明」，那么 `xrt::run` 就是「一次函数调用」。

一个 run 对象做三件事：

1. **绑定参数**：`set_arg(index, value)` 把第 index 个参数设成某个值（通常是一个 `xrt::bo`）。
2. **启动**：`start()` 把这次调用提交给硬件执行——**非阻塞**，立刻返回。
3. **等待**：`wait()` 阻塞当前主机线程，直到这次执行结束。

`start()`/`wait()` 分离，正是并发的来源：主机可以连续 `start()` 多个 run，让它们在硬件里同时跑，再分别 `wait()`。

#### 4.3.2 核心流程

本例的数据通路是：

```
DDR --(mm2s)--> AXI流 --(front_transpose)--> AIE FFT图 --(back_transpose)--> AXI流 --(s2mm)--> DDR
```

主机控制时序如下（关键：start 都是非阻塞的）：

```
s2mm_run.set_arg(0, s2mm_bo)    # 给 sink 绑输出缓冲
mm2s_run.set_arg(0, mm2s_bo)    # 给 source 绑输入缓冲
mm2s_run.start()                 # 非阻塞：开始往里灌数据
s2mm_run.start()                 # 非阻塞：开始往外收数据
s2mm_run.wait()                  # 阻塞：等收完
mm2s_run.wait()                  # 阻塞：等灌完
```

由于 mm2s 与 s2mm 在硬件里被 AXI 流（经 AIE 图）串成一条管道，二者必须**同时活着**——source 边灌、sink 边收。主机先 `start()` 两个 run（让管道两端同时启动），再 `wait()` 收尾。

> 注意 AIE 图是单独控制的：`my_graph.run(NUM_ITER)` 在更早处（4.1 之后、PL 内核之前）就把图跑起来了。`NUM_ITER = -1`（见 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp:83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L83) 的注释）表示**自由运行**，图的终止由 s2mm 收够数据来决定——这正是 `host.cpp` 第 83 行注释 `Let the graph run and have s2mm terminate things` 的含义。AIE 图的细节留待 [u13-l1](u13-l1-adf-graph-boundary.md)。

#### 4.3.3 源码精读

创建 run 与驱动执行的完整代码：

```cpp
xrt::run s2mm_run = xrt::run(s2mm);   // 用 s2mm 内核造一次调用
xrt::run mm2s_run = xrt::run(mm2s);   // 用 mm2s 内核造一次调用

// 绑定参数（第 0 个参数是各自的 DDR 缓冲）
mm2s_run.set_arg(0, mm2s_bo);
s2mm_run.set_arg(0, s2mm_bo);

// 启动（非阻塞）
mm2s_run.start();
s2mm_run.start();

// 等待（阻塞）
s2mm_run.wait();
mm2s_run.wait();
```

- run 的创建见 [host.cpp:137-141](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L137-L141)。
- `set_arg`/`start`/`wait` 见 [host.cpp:190-206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L190-L206)。

**start()/wait() 的并发含义（重点）**：

- `start()` 提交后立刻返回，硬件开始执行，主机线程不阻塞。于是主机能在 `mm2s_run.start()` 之后**立即**执行 `s2mm_run.start()`——两条 start 之间几乎没有时间差，保证管道两端几乎同时开工。
- `wait()` 是阻塞的。主机先 `wait()` s2mm（收数据的 sink），因为「数据收完」是整条管道完成的可靠信号；再 `wait()` mm2s（此时它通常早已结束）。
- 整个过程中，**mm2s 与 s2mm 在硬件里并发执行**，主机线程只是在最后两个 `wait()` 处同步。主机没有创建任何额外线程——并发发生在 FPGA/AIE 硬件里，由 AXI 流连接驱动，主机只是「点火」与「收工」。

#### 4.3.4 代码实践

**实践目标**：把 `start()`/`wait()` 的时序画成时间线，直观理解并发。

**操作步骤**：

1. 打开 [host.cpp:190-206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L190-L206)。
2. 在纸上画两条横线，分别代表 mm2s 与 s2mm，把 `start()` 标成起点、`wait()` 标成终点。
3. 回答：如果把 `s2mm_run.wait()` 和 `mm2s_run.wait()` 的顺序对调（先 wait mm2s 再 wait s2mm），程序还正确吗？为什么？

**预期结果**：时间线上两个 start 几乎对齐，两个 wait 依次在后。对调 wait 顺序在功能上通常仍正确（两者都会结束），但语义上「先等收完」更安全——因为 sink 的结束才是管道整体完成的标志；若 source 因某种原因先结束而 sink 还在等数据，先 wait mm2s 没有副作用，但理解上应以 s2mm 为准。

#### 4.3.5 小练习与答案

**练习 1**：`start()` 和 `wait()` 哪个阻塞？为什么这样设计？

**答案**：`start()` 非阻塞，`wait()` 阻塞。这样主机可以连续启动多个内核让它们并发，只在需要结果时才同步，最大化硬件并行度。

**练习 2**：本例中主机有没有创建任何 std::thread 来让 mm2s 和 s2mm 并发？

**答案**：没有。并发发生在硬件里——mm2s 与 s2mm 是两个独立的 PL 内核，由 AXI 流（经 AIE 图）连接，硬件本身并行工作。主机只是按序 `start()` 两者，再用 `wait()` 收尾。

**练习 3**：`my_graph.run(NUM_ITER)` 中 `NUM_ITER = -1` 是什么意思？

**答案**：负的迭代计数表示 AIE 图**自由运行**（不限次数地持续处理流入的数据），不靠迭代次数停机；本例由 s2mm 收够数据来终止整条流水（见 [host.cpp:83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L83) 注释）。

---

### 4.4 xrt::bo：buffer object 与 group_id

#### 4.4.1 概念说明

`xrt::bo`（buffer object）是设备内存（DDR/HBM）里的一块缓冲，是主机与设备交换数据的载体。它的生命周期是：

1. **创建**：`xrt::bo(device, 字节数, group_id)`——在指定存储 bank 上分配。
2. **映射**：`bo.map<T*>()` 把这块设备内存映射成主机可读写的指针（零拷贝语义）。
3. **同步**：`bo.sync(方向)` 在主机与设备之间搬运数据。`XCL_BO_SYNC_BO_TO_DEVICE` 把主机写好的数据送到设备；`XCL_BO_SYNC_BO_FROM_DEVICE` 把设备算好的结果取回主机。

**group_id 的意义**。Alveo/Versal 有多片 DDR 或多个 HBM bank。内核的每个 AXI 主端口在 `system.cfg` 里被绑定到某片存储（`sp=mm2s.mem:LPDDR`）。`kernel.group_id(arg_index)` 返回「该参数对应哪个存储 bank」的编号。创建 bo 时传入这个编号，就能保证 bo 与内核参数落在同一片 DDR，避免跨 bank 访问。

#### 4.4.2 核心流程

```
xrt::bo(device, size, kernel.group_id(0))   # 在内核参数 0 对应的 bank 上分配
        │
        ├── map<real_dtype*>()               # 拿主机指针，填/读数据
        │
        └── sync(XCL_BO_SYNC_BO_FROM_DEVICE) # 设备→主机（取结果）
            sync(XCL_BO_SYNC_BO_TO_DEVICE)   # 主机→设备（送输入）
```

本例中：`mm2s_bo` 是**输入**（主机填 stimulus，送给 mm2s 灌入）；`s2mm_bo` 是**输出**（s2mm 收回的结果，主机 sync 后读出校验）。

#### 4.4.3 源码精读

创建与映射缓冲：

```cpp
auto mm2s_bo = xrt::bo(my_device, DDR_BUFFSIZE_I_BYTES, mm2s.group_id(0));
auto mm2s_bo_mapped = mm2s_bo.map<real_dtype*>();

auto s2mm_bo = xrt::bo(my_device, DDR_BUFFSIZE_O_BYTES, s2mm.group_id(0));
auto s2mm_bo_mapped = s2mm_bo.map<real_dtype*>();
```

- `mm2s.group_id(0)` 把输入 bo 挂到 mm2s 第 0 个参数对应的存储 bank。
- `map<real_dtype*>()` 后，主机就能像普通数组一样写 stimulus（见 [host.cpp:172-182](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L172-L182) 从 `input_front.txt` 读数据填进 `mm2s_bo_mapped`）。

详见 [host.cpp:147-160](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L147-L160)。

取回结果（在 start/wait 之后）：

```cpp
s2mm_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE);   // 把设备结果拉回主机
mm2s_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE);   // 顺便同步输入 bo（便于校验/调试）
```

详见 [host.cpp:212-216](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L212-L216)。

`group_id` 与 `system.cfg` 的存储端口声明的对应关系：

```
sp=mm2s.mem:LPDDR    # mm2s 的 mem 参数接到 LPDDR 这片存储
sp=s2mm.mem:LPDDR
```

见 [system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14) 与 [system.cfg:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33)。所以 `mm2s.group_id(0)` 返回的正是 LPDDR 对应的 bank 编号，bo 也随之落在 LPDDR 上。

#### 4.4.4 代码实践

**实践目标**：把 bo 的「分配→映射→填数据→同步→读结果」全链路在源码里走一遍。

**操作步骤**：

1. 在 [host.cpp:147-160](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L147-L160) 找到两个 bo 的创建与 `map`。
2. 在 [host.cpp:172-182](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L172-L182) 看主机如何用 `mm2s_bo_mapped` 写 stimulus。
3. 在 [host.cpp:212-216](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L212-L216) 找到取回结果的 `sync`。
4. 回答：为什么读取 `s2mm_bo_mapped` 之前必须先 `s2mm_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE)`？

**预期结果**：因为 `map` 给的是主机侧视图，设备算完的结果先落在设备 DDR 里，不 `sync` 就读不到最新值；`FROM_DEVICE` 方向把设备数据搬进主机视图。注意本例输入 bo 没有 `TO_DEVICE` 的显式 sync——因为对 `CL_MEM_USE_HOST_PTR`/零拷贝路径，运行时在内核启动时会按需同步；但取结果方向的 `FROM_DEVICE` 是必须显式调用的。

#### 4.4.5 小练习与答案

**练习 1**：`group_id(0)` 里的 `0` 指什么？

**答案**：指内核签名的**第 0 个参数**。mm2s/s2mm 的第 0 个参数就是那个指向 DDR 的 memory-mapped 指针，`group_id(0)` 返回它绑定的存储 bank 编号。

**练习 2**：如果把 `s2mm_bo` 创建时的 `s2mm.group_id(0)` 换成一个硬编码的别的 bank 编号，可能出什么问题？

**答案**：bo 可能落在与 s2mm 的 mem 端口不同的 DDR bank 上，导致内核访问不到这片缓冲，或引发跨 bank 访问降低带宽、甚至运行时错误。始终用 `group_id` 才能保证 bo 与内核参数在同一 bank。

**练习 3**：`map()` 之后、`sync()` 之前，主机指针指向的数据和设备里的数据一定一致吗？

**答案**：不一定。`map` 只是建立主机虚拟地址到设备缓冲的映射，不保证内容已同步。写完主机数据后通常要 `sync(TO_DEVICE)`；设备算完后要 `sync(FROM_DEVICE)` 才能在主机侧读到结果。

---

## 5. 综合实践

**任务**：通读 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp) 的 `main` 函数（[第 105-262 行](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L105-L262)），完成下面三件事，把本讲四个模块串起来。

1. **列出 API 调用序列**。从加载 xclbin 之后开始，按源码顺序写出主机控制 mm2s/s2mm 的每一条 XRT 调用（到取回结果为止）。参考答案骨架：

   ```
   xrt::device(0) → load_xclbin → xrt::graph(...) → graph.reset() → graph.run(-1)
   → xrt::kernel(mm2s) → xrt::kernel(s2mm)
   → xrt::run(mm2s) → xrt::run(s2mm)
   → xrt::bo(mm2s, group_id(0)) → bo.map() → 填 stimulus
   → xrt::bo(s2mm, group_id(0)) → bo.map()
   → mm2s_run.set_arg(0, mm2s_bo) → s2mm_run.set_arg(0, s2mm_bo)
   → mm2s_run.start() → s2mm_run.start()
   → s2mm_run.wait() → mm2s_run.wait()
   → s2mm_bo.sync(FROM_DEVICE) → 读 s2mm_bo_mapped 校验
   ```

2. **解释 start()/wait() 的并发含义**。用自己的话写一段：为什么两个 `start()` 几乎同时发生、而 `wait()` 要排在后面？为什么不需要主机线程？把答案和 `NUM_ITER = -1`（图自由运行、由 s2mm 终止）联系起来。

3. **画一张端到端数据流图**。结合 [system.cfg 的连接声明](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L22-L31)，标出 `mm2s.sig_o_*` → `front_transpose` → AIE 图 → `back_transpose` → `s2mm.sig_i_*` 这条链路，并在图上标出 `mm2s_bo`（输入）与 `s2mm_bo`（输出）的位置。

**预期结果**：你能用一句话讲清「主机只是点火与收工，真正的并发在硬件里由 AXI 流驱动」；并能在源码里逐行指认每个 XRT 对象对应的行号。

> 本实践为源码阅读型，无需硬件；若要在 hw_emu 上真正运行，需按 [example.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk) 的 `all` 目标执行 `make -f example.mk all PLATFORM=... DSPLIB_ROOT_DIR=...`（耗时可达数百分钟，待本地验证）。

## 6. 本讲小结

- 原生 XRT C++ API（`xrt::device/kernel/run/bo`）与 OpenCL 等价但更直接，且提供 `xrt::graph` 等 AIE 专有能力，是 AIE/嵌入式例子的首选。
- 控制链固定为：`xrt::device` → `load_xclbin`（得 uuid）→ `xrt::kernel`（取内核）→ `xrt::run`（造一次调用）→ `set_arg` → `start` → `wait` → `bo.sync` 取回。
- **kernel 是定义，run 是调用**；一个 kernel 可创建多个 run。
- `start()` 非阻塞、`wait()` 阻塞，二者分离是并发的来源；本例 mm2s 与 s2mm 在硬件里并发，主机不创建线程。
- `xrt::bo` 用 `group_id` 挂到内核参数对应的 DDR bank，`map` 建主机视图，`sync` 决定数据流向（`FROM_DEVICE` 取结果）。
- 内核名 `函数名:{实例名}` 必须与 `system.cfg` 的 `nk` 声明严格对齐。

## 7. 下一步学习建议

- 想深入 bo 同步方向与多 bank 分区对带宽的影响，继续读 [u4-l3（buffer object、同步与存储分区）](u4-l3-bo-sync-and-memory.md)。
- 想了解 v++ 如何把 mm2s/s2mm 编译成 XO 并链接成 xclbin，读 [u5-l1（v++ L2 构建流程）](u5-l1-vpp-l2-build.md)。
- 想搞清 `xrt::graph` 的 `reset/run/end` 与 AIE 图结构，读 [u13-l1（ADF 图、窗口/流与 PL↔AIE 边界）](u13-l1-adf-graph-boundary.md) 与 [u13-l2（AIE 图主机控制与 SD 卡打包）](u13-l2-aie-host-packaging.md)。
- 建议对照阅读另一个原生 XRT 主机例子的 `host.cpp`（如 `dsp/L2/examples` 下其他目录），对比它们在 run 创建与参数绑定上的异同，巩固本讲的调用链模式。
