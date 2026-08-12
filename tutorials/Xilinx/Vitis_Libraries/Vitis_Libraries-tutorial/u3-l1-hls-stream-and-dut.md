# hls::stream、ap_int 与 DUT 封装约定

## 1. 本讲目标

本讲是「HLS 内核与流式数据模型」单元的第一讲。前面你已经把 `utils/L1/tests/stream_dup` 这个 L1 用例跑通（见 u2-l2），也看懂了 `csim/csynth` 报告里的 II、latency、资源（见 u2-l3）。本讲要回答一个更底层的问题：

> 在 Vitis 加速库里，**数据到底是怎么在内核之间流动的？那个会被综合成硬件的「顶层函数」又是怎么写出来的？**

学完本讲，你应该能够：

1. 说清楚 `hls::stream` 是什么、它的读写方法（`read/write/read_nb/write_nb`）各自的行为，以及为什么 HLS 里几乎不用数组下标而用流。
2. 解释「end-flag 流」约定：为什么每条数据流都要配一条 `hls::stream<bool>` 来标记结束，并掌握 `stream_dup` 的「前瞻式」end-flag 消费规律。
3. 掌握 `ap_int<N>` / `ap_uint<N>` 任意位宽整数与 `.range()` 位切片的用法，理解位宽（datawidth）对吞吐的意义。
4. 学会把一个模板化的库内核封装成 `extern "C"` 的 DUT（Design Under Test，被测设计），这是后续所有 HLS 内核上板的统一入口形式。

## 2. 前置知识

本讲假设你已经具备 u2-l2、u2-l3 的认知：

- **L1 用例的「目录即用例」约定**：每个 L1 测试是一个独立可 `make` 的目录，核心三件套是 `test.cpp` + `Makefile` + `description.json`。
- **DUT 与 testbench 同居一文件**：`test.cpp` 里，会被综合的顶层函数（DUT）放在外面，仅仿真运行的测试逻辑用 `#ifndef __SYNTHESIS__` 包起来；`main` 用 `argv[1]` 选择跑哪个测试，csim 固定传 `"0"`。
- **csim 是纯软件仿真**：不综合，只验功能；II、latency、资源这些硬件指标要到 `csynth` 才有。

此外需要两个最基础的概念：

- **FPGA 的「时序」是周期级的**。硬件在每一个时钟周期里同时做很多事，数据像流水线上的零件一样一拍一拍往前走。软件里 `for` 循环「算完一个再算下一个」的思维，在硬件里要换成「每个周期都能吞一个、吐一个」的流式思维。
- **C++ 模板**。库内核大多是模板函数（如 `streamDup<typename _TIn, int _NStrm>`），类型和参数在编译期才确定。模板函数本身不能直接当综合顶层——这一点会在 4.4 讲清楚。

## 3. 本讲源码地图

本讲聚焦 `utils` 库 L1 下的四个文件，它们恰好覆盖四个最小模块：

| 文件 | 作用 | 对应模块 |
| --- | --- | --- |
| `utils/L1/include/xf_utils_hw/types.hpp` | 类型基础设施：重导出定宽整数、强制 `AP_INT_MAX_W=4096`、引入 `ap_int.h` 与 `hls_stream.h` | ap_int 类型 |
| `utils/L1/include/xf_utils_hw/common.hpp` | 库内共享逻辑：编译期模板元工具（`PowerOf2/GCD/LCM`）、`XF_UTILS_HW_STATIC_ASSERT` 等宏 | end-flag 约定（断言）、ap_int |
| `utils/L1/include/xf_utils_hw/stream_dup.hpp` | 「复制流」内核：把一条输入流复制成 N 条，每条都带 end flag。本讲的主例子 | hls::stream 读写、end-flag 约定 |
| `utils/L1/tests/stream_dup/test.cpp` | stream_dup 的测试台：同时定义 `extern "C" dut0/dut1`（DUT）与 `test_dut0/test_dut1`（testbench） | extern C DUT 封装 |

辅助文件（用于综合实践与对比）：

- `utils/L1/include/xf_utils_hw/stream_split.hpp`：「拆分流」内核，综合实践的目标。
- `utils/L1/include/xf_utils_hw/enums.hpp`：`LSBSideT/MSBSideT` 等空标签结构体，用于靠参数类型选择重载。
- `utils/L1/tests/stream_split/split_lsb/test.cpp`：stream_split 的 LSB 测试台，与 stream_dup 风格做对比。

## 4. 核心概念与源码讲解

### 4.1 hls::stream：FPGA 上的数据队列

#### 4.1.1 概念说明

软件里传递一批数据，最自然的是放进数组 `T a[N]`，然后用 `a[i]` 随机访问。但在 FPGA 上，数组会被综合成 RAM，随机访问意味着要在某个周期去「寻址读 RAM」，很难做到每个周期都喂给流水线一个新数据。

`hls::stream<T>` 是 Vitis HLS 提供的**先进先出队列（FIFO）抽象**，专门解决「把数据一拍一拍喂给流水线」这件事：

- 写端 `push` 进去，读端按写入顺序 `pop` 出来。
- **每个元素只能读一次**：一旦 `read()` 走，就没了。这强制你用「顺序、单次」的方式消费数据——而这正是硬件流水线最喜欢的访问模式。
- 在硬件里它通常映射成一个深度有限的 FIFO（或直接展平成一组寄存器 / 一根线），把生产者和消费者的时序解耦。

常用接口（均为 `hls::stream` 的成员）：

| 方法 | 行为 |
| --- | --- |
| `s.read()` | **阻塞读**：取队首并弹出；csim 中若队列为空会报错抛出 |
| `s.write(v)` | **阻塞写**：把 `v` 压入队尾；队列满时阻塞 |
| `s.read_nb(v)` | **非阻塞读**：成功返回 `true` 并把值写到 `v`；队列为空返回 `false` |
| `s.write_nb(v)` | **非阻塞写**：成功返回 `true`；队列满返回 `false` |
| `s.empty()` / `s.full()` | 查询状态 |

> 直觉一句话：`hls::stream` = 只能「从头取一次、往尾塞一次」的单向传送带。它逼着你写出**流式**代码，从而能被综合成每个周期吞一拍、吐一拍的硬件。

#### 4.1.2 核心流程

一个典型的「生产者—stream—消费者」数据通路：

```
生产者循环:  每拍算出一个值 → istrm.write(v); e_istrm.write(false);
                                   │ (FIFO)
消费者循环:  while (!end) { v = istrm.read(); 处理 v; }
```

若消费者的处理循环被综合成 **II=1**（每个周期接受一个新输入，见 u2-l3），那么稳态吞吐就是每周期 1 个元素。用数学表达，设时钟周期为 \(T_{\text{clk}}\)、初始化间隔为 II，则吞吐为：

\[
\text{throughput} = \frac{1}{\text{II} \cdot T_{\text{clk}}}\quad(\text{beat/秒})
\]

II=1 时即「每拍一个」，这是流式内核追求的目标。

#### 4.1.3 源码精读

**① stream 的引入与声明。** `types.hpp` 在文件末尾引入了 HLS 的两个核心头：

[utils/L1/include/xf_utils_hw/types.hpp:81-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/types.hpp#L81-L82) —— `#include "ap_int.h"` 与 `#include "hls_stream.h"`，`hls::stream` 就来自后者。任何包含 `types.hpp`（或直接包含 `stream_dup.hpp`，因为它间接包含 `types.hpp`）的文件都能用 `hls::stream`。

在测试台里，stream 的声明和数组写法很像，只是元素类型换成 `hls::stream<T>`：

[utils/L1/tests/stream_dup/test.cpp:76-79](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L76-L79) —— 声明了一组输入数据流 `istrm[NUM_ISTRM]`、一组输入 end 流 `e_istrm[NUM_ISTRM]`、以及输出流二维数组 `ostrms[NUM_DSTRM][NUM_COPY]`。注意 `hls::stream` 可以做成数组，每个元素都是独立的 FIFO。

**② 写入：阻塞 `write`。** 测试台作为生产者，往流里喂数据：

[utils/L1/tests/stream_dup/test.cpp:81-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L81-L87) —— `istrm[i].write(testdata[i][j])` 写数据，`e_istrm[i].write(0)` 写 end flag，循环结束后再 `e_istrm[i].write(1)` 写终止符。csim 里 `write` 是阻塞的，但默认 FIFO 深度足够，这里不会满。

**③ 读取：阻塞 `read`。** 在内核侧（DUT 调用的 `streamDup` 内部）：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:92-97](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L92-L97) —— 先 `e_istrm.read()` 读 end flag，循环体内 `tmp = istrm.read()` 读数据。这些 `read()` 都是阻塞读：在 csim 里若队列为空会直接报错。

**④ 非阻塞 `read_nb`。** 在校验结果时，测试台想「能读就读、读不到就计数一次错误」，于是用非阻塞版本：

[utils/L1/tests/stream_dup/test.cpp:104-113](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L104-L113) —— `ostrms[i][k].read_nb(outdata)` 返回是否读到，读到则把值写到 `outdata`，没读到（队列空）则 `nerr++`。`read_nb` 适合在 testbench 里做「有多少读多少」的柔性消费，但**不建议**在可综合内核里依赖它的返回值做控制流（综合后的空满判断是另一套机制）。

#### 4.1.4 代码实践

**目标**：用肉眼追踪一条数据在 stream 里的生命周期，建立「写一次只能读一次」的直觉。

**步骤**：

1. 打开 [utils/L1/tests/stream_dup/test.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp)。
2. 在 `test_dut0()` 里定位生产者写入（L81-87）与消费者读取（L91-93 调用 `dut0`，真正的读发生在 `streamDup` 内部）。
3. 注意校验段（L102-113）用的是 `read_nb`，把同一个 `ostrms[i][k]` 里写入的 16 份副本逐一读出来比对。

**需要观察的现象**：

- `dut0` 调用返回后，输入流 `istrm[i]` 已经被 `streamDup` 读空——所以 L116-124 才能直接用 `istrm[i].read_nb()` 读出**未被复制**的那几路（`i` 从 `NUM_DSTRM` 到 `NUM_ISTRM`，它们没进过 `dut0`，仍由原测试台持有）。

**预期结果**：能讲清楚「为什么 `dut0` 处理过的前 4 路 `istrm[0..3]` 已经空了，而 `istrm[4..7]` 还满着」。如果你本地有 Vitis 环境，可在 `utils/L1/tests/stream_dup` 下 `make run TARGET=csim` 看到 `PASS: no error found.`（与 u2-l2 一致）。若无环境，本任务为源码阅读型，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `hls::stream` 换成普通数组 `T a[N]`，为什么不适合做流式内核的接口？

> **答案**：数组会被综合成 RAM，访问需要寻址，且默认允许随机访问；HLS 难以把它推断成「每拍进一个、出一个」的流水。`hls::stream` 强制单次顺序读写，天然映射成 FIFO/线，能稳定达成 II=1。

**练习 2**：csim 中对一个空流调用 `s.read()` 会发生什么？怎么避免？

> **答案**：会触发「读空流」的错误并终止仿真。避免方式有两种：要么像库内核那样用 end-flag 保证只在有数据时读，要么用非阻塞的 `s.read_nb(v)` 先试探。

---

### 4.2 end-flag 流约定：流没有「长度」字段

#### 4.2.1 概念说明

`hls::stream` 有一个软件工程师不习惯的特性：**它不携带长度信息**。你拿到一条 `hls::stream<T> istrm`，并不知道里面有多少个元素、何时结束。

这带来一个致命问题：消费者的 `while` 循环什么时候停？

- 在 csim 里，盲目 `read()` 会读到空流报错。
- 在真实硬件里，FIFO 空了再读会读到未定义的垃圾数据，循环永远不会自然结束。

Vitis `utils` 库的统一约定是：**给每条数据流配一条伴生的 `hls::stream<bool>` end-flag 流**（习惯上叫 `e_istrm` / `e_ostrm`，`e` 即 end）。用一个布尔位标记「数据是否结束」，从而让消费者知道何时停止。

这里有一个**关键设计差异**值得记住——end-flag 的结构跟着数据拓扑走：

- `stream_dup` 是「一进 N 出」，且每路输出独立结束，所以它给**每一路输出**都配了一个 end 流，声明为**数组** `hls::stream<bool> e_ostrms[_NStrm]`：
  [utils/L1/include/xf_utils_hw/stream_dup.hpp:46-50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L46-L50)。
- `stream_split` 虽然也是「一进 N 出」，但所有输出**同生同灭**（一次拆分产生 N 个同步的输出），所以共用**一个** end 流 `hls::stream<bool>& e_ostrm`：
  [utils/L1/include/xf_utils_hw/stream_split.hpp:50-55](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_split.hpp#L50-L55)。

> 直觉一句话：流没有 `size()`，于是用一条并行的布尔流当「信号兵」，告诉消费者「下一个还要不要读」。信号兵的个数取决于输出是不是各自独立结束。

#### 4.2.2 核心流程

`stream_dup` 的 `streamDup` 采用一种**前瞻式（look-ahead）**消费 end flag 的写法。设输入数据共 N 拍，则：

1. 进入函数，先 `e = e_istrm.read()` 读第一个 flag；
2. `while (!e)`：只要还没到结束，进入循环体；
3. 循环体**开头**再 `e = e_istrm.read()` 读「下一个」flag（前瞻），随后 `tmp = istrm.read()` 读一个数据，处理后写回各输出；
4. 当某次读到的 flag 为 `true`，`while (!e)` 退出，最后向所有输出 end 流写 `true` 终止符。

这条规律可以量化为一个不变式（invariant）：**对 N 个输入数据 beat，内核恰好读走 N 个数据 + N+1 个 end flag**。相应地，生产者必须写入 N 个 `false` + 1 个 `true`（共 N+1 个 flag）。把 flag 流相对数据流的「领先一拍」关系画出来：

```
数据流   :         d0      d1      d2   ...   d(N-1)
flag流   :  f0(=0) f1(=0)  f2(=0)  ...         fN(=1)
            ↑       ↑       ↑                    ↑
          入口读   循环开头读，决定是否再吃一个数据    终止
```

#### 4.2.3 源码精读

**① 前瞻式 end-flag 消费。** 这是 `streamDup` 的核心循环：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:87-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L87-L108) —— L92 读首个 flag；L93 `while (!e)`；L96 在循环开头读下一个 flag；L97 才读数据；L98-102 用 `#pragma HLS unroll` 把同一个 `tmp` 复制到 N 路输出，并向每路写一个 `false` 的 end flag；L104-107 循环结束后向每路写 `true` 终止符。计数一下：循环每轮读 1 个数据 + 1 个 flag，加上入口那 1 个 flag，正好 N 个数据 + N+1 个 flag。

**② 生产者必须满足不变式。** 看 testbench 怎么喂数据：

[utils/L1/tests/stream_dup/test.cpp:81-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L81-L87) —— 对每个数据先 `istrm[i].write(testdata[i][j])` 再 `e_istrm[i].write(0)`，循环外补一个 `e_istrm[i].write(1)`。即「N 个数据 → N 个 `0` + 1 个 `1`」，与上面的不变式严丝合缝。

**③ 校验端也按 end-flag 停止。** testbench 读输出时，先读 `LEN_STRM` 个 `false`，再期望读到 1 个 `true`：

[utils/L1/tests/stream_dup/test.cpp:128-145](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L128-L145) —— 内层循环读 `LEN_STRM` 个 flag 并断言它们都是 `false`（L135-136 `else if (e) nerr++`），紧接着再读一个并断言它是 `true`（L138-143 `else if (!e) nerr++`）。这就是用 end flag 来「校验结束信号正确」。

**④ 编译期断言保护约定。** 第二个重载用静态断言保证「被复制的路数不能超过输入路数」：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:117](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L117) —— `XF_UTILS_HW_STATIC_ASSERT(_NDStrm <= _NIStrm, "...")`。这个宏定义在 `common.hpp`：

[utils/L1/include/xf_utils_hw/common.hpp:216-220](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/common.hpp#L216-L220) —— C++11 下展开成 `static_assert((b), m)`，在编译期就拦下错误的模板参数，而不是等到仿真跑飞。

#### 4.2.4 代码实践

**目标**：亲手验证「漏掉终止符会让消费者无法正常停止」。

**步骤**：

1. 复制一份 `utils/L1/tests/stream_dup` 到本地实验目录（**不要改动源码**）。
2. 在副本的 `test.cpp` 的 `test_dut0()` 里，把 L86 的 `e_istrm[i].write(1);` 注释掉。
3. 重新 `make run TARGET=csim`。

**需要观察的现象**：

- 没有 `true` 终止符后，`streamDup` 内部 L96 的 `e_istrm.read()` 会一直读到 `false`，循环不会按预期结束；当输入流被读空后继续 `istrm.read()`，csim 会抛出「读空流」错误。

**预期结果**：csim 报错或返回 `FAIL`（具体错误信息取决于 Vitis 版本）。这反向证明了 end-flag 的必要性。**待本地验证**（若无环境，则作为源码推理练习：用 4.2.2 的不变式推导「N 个数据配 N 个 false、缺最后一个 true」时，内核会读空数据流后卡在循环里）。

#### 4.2.5 小练习与答案

**练习 1**：`stream_dup` 的 `e_ostrms` 是「每路一个」的数组，而 `stream_split` 只用一个 `e_ostrm`，为什么？

> **答案**：`stream_dup` 把同一份数据复制到 N 路输出，每路输出可能被下游各自独立消费、各自结束，所以每路需要独立 end 流；`stream_split` 每拍同时产出 N 路同步结果，N 路同生同灭，一个共享 end 流即可表达「这一组都结束了」。end-flag 的拓扑与数据拓扑一致。

**练习 2**：对 10 个输入数据，`streamDup`（第一个重载）会从 `e_istrm` 读走多少个 flag？

> **答案**：11 个（10 个 `false` + 1 个 `true`）。数据流读 10 个。这是 4.2.2 不变式的直接应用：N 个数据 ⇒ N+1 个 flag。

---

### 4.3 ap_int：任意位宽的硬件整数

#### 4.3.1 概念说明

软件里的整数只有 8/16/32/64 位几种「标准」宽度。FPGA 却完全不受这个限制——你想要一个 20 位、512 位甚至 4096 位的数，硬件都能用恰好那么多触发器/查找表实现，不多不少。

Vitis HLS 的 `ap_int<N>`（有符号）与 `ap_uint<N>`（无符号）模板类就是用来表达**任意位宽整数**的：

- `ap_uint<512> x;` —— 一个 512 位无符号数。
- `x.range(hi, lo)` —— 取从 `hi` 到 `lo` 的连续位段（闭区间），返回一个 `ap_uint` 引用，可读可写。

为什么要用任意位宽？因为**单拍能搬运的数据量（datawidth）直接决定带宽**。同样 250 MHz 时钟下：

- 每拍 64 位 → 理论带宽 \(64 \times 250\text{M} / 8 = 2\text{ GB/s}\)；
- 每拍 512 位 → \(512 \times 250\text{M} / 8 = 16\text{ GB/s}\)，是 64 位的 8 倍。

所以库内核大量用 `ap_uint<大宽度>` 把多路数据「打包」进一拍，靠 `.range()` 切出各路。带宽与时钟、位宽的关系：

\[
\text{带宽} = \text{datawidth} \times f_{\text{clk}}
\]

> 直觉一句话：`ap_int<N>` 让你用一个「正好 N 位」的硬件数，想多宽就多宽；`.range()` 是它的「切片刀」，用来在一个超宽 beat 里切出若干窄通道。

#### 4.3.2 核心流程

把一个宽为 \(W_{\text{in}}\) 的整数拆成 \(K\) 段、每段 \(W_{\text{out}}\) 位（要求 \(W_{\text{out}} \cdot K \le W_{\text{in}}\)），第 \(i\) 段（从最低位起，LSB 优先）的取值为：

\[
\text{chunk}_i = \left\lfloor \frac{V}{2^{i \cdot W_{\text{out}}}} \right\rfloor \bmod 2^{W_{\text{out}}}
\]

用 `ap_uint` 写就是 `V.range((i+1)*Wout - 1, i*Wout)`。这正是 `stream_split` 的拆分逻辑。

使用 `ap_int` 有一个全局前置条件：必须在包含 `ap_int.h` **之前**用宏 `AP_INT_MAX_W` 告诉工具链「我最多用多宽」，否则默认上限可能不够。`utils` 库在 `types.hpp` 里统一把它强制设到 4096。

#### 4.3.3 源码精读

**① 强制最大位宽并引入头文件。**

[utils/L1/include/xf_utils_hw/types.hpp:75-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/types.hpp#L75-L82) —— L78-79 先 `#undef` 再 `#define AP_INT_MAX_W 4096`，L81 才 `#include "ap_int.h"`。顺序不能反：宏必须在 `ap_int.h` 被看到前生效。L75-77 还会在用户自定义了更小的值时给出 `#warning`。

**② 用 `.range()` 切片拆分。** `stream_split` 的 LSB 实现里，把一个宽整数切成 K 段：

[utils/L1/include/xf_utils_hw/stream_split.hpp:117-121](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_split.hpp#L117-L121) —— `ap_uint<_WOut> d = data.range((i + 1) * _WOut - 1, i * _WOut);` 取出第 i 段，再 `ostrms[i].write(d)`。这就是 4.3.2 公式的直接代码化。文件头部 L96-108 的注释给了具体数值例子（`_WIn=20, _WOut=4, _NStrm=4`，输入 `0x82356`）。

**③ ap_uint 的运算用法。** `common.hpp` 里一组 `countOnes` 重载展示 `ap_uint` 的位运算（统计二进制中 1 的个数，经典 popcount）：

[utils/L1/include/xf_utils_hw/common.hpp:155-164](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/common.hpp#L155-L164) —— 用 `ap_uint<16>` 配合掩码与移位逐级归并，全部 `#pragma HLS inline`。这说明 `ap_uint` 支持和普通整数一样的位运算（`&`、`>>`、`+`），且能被内联进调用方。

#### 4.3.4 代码实践

**目标**：用纸笔（或写一小段纯 C++ 程序）验证 `.range()` 的拆分结果，建立位宽直觉。

**步骤**：

1. 读 [utils/L1/include/xf_utils_hw/stream_split.hpp:96-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_split.hpp#L96-L108) 的注释例子：`_WIn=20, _WOut=4, _NStrm=4`，输入 `0x82356`。
2. 用 4.3.2 的公式手算 4 段：把 `0x82356` 写成 20 位二进制，每 4 位切一段，从低位取。

**需要观察的现象**：

- `0x82356` = 二进制 `1000 0010 0011 0101 0110`（20 位）。LSB 起每 4 位一段：`0110`(=6)、`0101`(=5)、`0011`(=3)、`0010`(=2)，最高 4 位 `1000`(=8) 因 `WOut*NStrm=16 < 20` 被丢弃。所以 `ostrms[0..3] = 6,5,3,2`。

**预期结果**：你手算的四段是 `6,5,3,2`，与注释里「lsb 列」一致。**待本地验证**（若装了 Vitis，可写 5 行 `ap_uint<20>` 程序打印 `.range()` 验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `types.hpp` 要在 `#include "ap_int.h"` 之前定义 `AP_INT_MAX_W`？

> **答案**：`ap_int.h` 根据这个宏生成支持的最大位宽模板特化（默认值通常较小，如 1024）。若在包含之后才定义，宏不会被 `ap_int.h` 看到，用到超宽类型（如 `ap_uint<4096>`）时会编译失败。

**练习 2**：一个 `ap_uint<512>` 的流，每拍传输 512 位，比 `ap_uint<64>` 快多少（相同 II、相同时钟）？

> **答案**：理论上快 8 倍，因为每拍搬动的位数是 8 倍。这正是「加宽 datawidth」换带宽的常用手段，代价是占更多布线与寄存器资源。

---

### 4.4 extern "C" DUT 封装：把模板内核包成综合入口

#### 4.4.1 概念说明

库里的算法内核大多是**模板函数**，比如：

```cpp
template <typename _TIn, int _NStrm>
void streamDup(hls::stream<_TIn>& istrm, ... );
```

模板的 `_TIn` 和 `_NStrm` 要到使用时才确定。但 HLS 综合需要一个**完全确定**的顶层函数：固定的实参类型、固定的模板参数、固定的符号名。而且这个符号名还要能被主机端 XRT/OpenCL 通过名字找到（见 u4 单元）。所以直接拿模板函数当顶层是不可行的。

Vitis 库的统一做法是写一个 **DUT 封装**：

1. 是一个**普通（非模板）函数**，所有模板参数被「钉死」为具体值；
2. 加 `extern "C"`，关闭 C++ 的 name mangling（名字粉碎），保证编译后的符号就是函数名本身，便于 XRT 按名查找；
3. 函数体里调用真正的模板内核，把工作转交出去。

> 直觉一句话：DUT 是模板内核的「实例化壳」——钉死参数、压平签名、加 `extern "C"` 拿到一个稳定名字，让综合器和主机都能认得它。

#### 4.4.2 核心流程

一个 L1 用例的 `test.cpp` 同时承担两个角色，靠 `__SYNTHESIS__` 宏切换：

```
test.cpp
├── extern "C" void dut0(...)        ← DUT：综合器看到的部分（顶层 = dut0）
│       └── 调用模板内核 streamDup<TYPE, NUM_COPY>(...)
└── #ifndef __SYNTHESIS__            ← testbench：仅 csim 编译，综合时整体消失
        int test_dut0() { 喂数据、调 dut0、比对 }
        int main(...) { 按 argv[1] 选 test }
```

- 综合时（`csynth` 及以后）：HLS 预定义 `__SYNTHESIS__`，`#ifndef` 段被剔除，只剩下 `dut0`，它就是 `description.json` 里 `topfunction` 指定的顶层。
- 仿真时（`csim`）：`__SYNTHESIS__` 未定义，`main` 被编译进来，`main` 调 `test_dut0`，`test_dut0` 调 `dut0`，于是同一个 `dut0` 在软件里被跑一遍。

#### 4.4.3 源码精读

**① DUT 封装的标准写法。** stream_dup 测试台里有两个 DUT，都是 `extern "C"`：

[utils/L1/tests/stream_dup/test.cpp:33-48](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L33-L48) —— `dut0` 把模板参数钉成 `<TYPE, NUM_COPY>`（`TYPE=uint32_t`，`NUM_COPY=16`，见 L26、L31），签名里的 `hls::stream<TYPE> ostrms[NUM_COPY]` 用数组维度固化了 `_NStrm`，函数体仅一行，转发给 `streamDup`。`dut1` 同理钉死四个模板参数并多传一个 `choose` 数组。

**② `extern "C"` 的作用。** 它告诉编译器对这个函数用 C 链接规则，不做 C++ 的参数型签名编码。结果：符号表里这个函数就叫 `dut0`，而不是被 mangle 成类似 `_Z4dut0RN3hls6streamIjEE...` 的乱码。L2/L3 阶段，主机程序就是用 `"dut0"` 这个名字去 xclbin 里找内核的。

**③ topfunction 与 DUT 名字一一对应。** `description.json` 里：

[utils/L1/tests/stream_dup/description.json:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L14) —— `"topfunction": "dut0"`。综合时 HLS 以 `dut0` 为顶层，把它（以及它调用的 `streamDup` 实例化）一起综合成一个硬件模块。改 DUT 名字必须同步改这里，否则找不到顶层。

**④ `__SYNTHESIS__` 切换 testbench。**

[utils/L1/tests/stream_dup/test.cpp:50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L50) —— `#ifndef __SYNTHESIS__`，到 L285 才 `#endif`。中间所有 `test_dut0/test_dut1/main` 只在 csim 存在。`main` 在 L261，按 `argv[1][0]` 分派（L264、L271），csim 固定传 `"0"` 跑 `test_dut0`（见 u2-l2）。

**⑤ 风格对比：不是所有 DUT 都显式 `extern "C"`。** stream_split 的 LSB 测试台用了不带 `extern "C"` 的普通函数做内核壳：

[utils/L1/tests/stream_split/split_lsb/test.cpp:29-35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_split/split_lsb/test.cpp#L29-L35) —— `void test_core_split_lsb(...)` 同样钉死模板参数、转发给 `streamSplit`，并用 `LSBSideT()` 这个空结构体实例选择 LSB 重载（标签类型定义在 [utils/L1/include/xf_utils_hw/enums.hpp:49-55](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/enums.hpp#L49-L55)）。它在 L1 阶段当 DUT 没问题；但**上板给主机调用时，规范做法仍是 `extern "C"`**（stream_dup 的 `dut0` 是更值得模仿的模板）。

#### 4.4.4 代码实践

**目标**：读懂现有 DUT，并写出一个最小 `extern "C"` DUT 的签名与函数体（不写 testbench）。

**步骤**：

1. 对照 [utils/L1/tests/stream_dup/test.cpp:33-38](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L33-L38)，逐行说明 `dut0` 做了哪三件事：(a) 钉死模板参数 `<TYPE, NUM_COPY>`；(b) 用 `extern "C"` 固化符号名；(c) 转发给 `streamDup`。
2. 写一个「把输入流复制成 4 路」的 DUT 片段（**示例代码**）：

```cpp
// 示例代码：模仿 dut0 写法，复制成 4 路
typedef uint32_t MY_T;
#define MY_COPY 4

extern "C" void dup4(hls::stream<MY_T>& istrm,
                     hls::stream<bool>& e_istrm,
                     hls::stream<MY_T> ostrms[MY_COPY],
                     hls::stream<bool> e_ostrms[MY_COPY]) {
    xf::common::utils_hw::streamDup<MY_T, MY_COPY>(istrm, e_istrm, ostrms, e_ostrms);
}
```

**需要观察的现象**：

- 这段代码与 `dut0` 的唯一差别是 `NUM_COPY` 从 16 改成 4；签名结构、`extern "C"`、转发方式完全一致。

**预期结果**：能说出「若把这个 `dup4` 放进一个新用例，`description.json` 的 `topfunction` 应填 `dup4`」。完整可运行版本见第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么 DUT 必须是非模板函数？直接 `template<class T> void dut(...)` 当顶层不行吗？

> **答案**：综合顶层必须有确定的接口签名（确定的位宽、确定的流数量），模板的参数未定，综合器无法据此生成硬件。DUT 的职责正是把模板「实例化」成唯一确定的函数。

**练习 2**：去掉 `dut0` 的 `extern "C"`，L1 的 `csim/csynth` 还能跑吗？为什么仍推荐加上？

> **答案**：L1 阶段（HLS 内）通常还能跑，因为 HLS 自己知道函数名。但到了 L2/L3，主机端 XRT 要按**未 mangle 的符号名**在 xclbin 里找内核；没有 `extern "C"`，C++ 编译器会改写名字，主机就找不到了。所以从一开始就用 `extern "C"` 是好习惯。

---

## 5. 综合实践：为 stream_split 写一个完整的 extern "C" DUT 用例

本任务把四个最小模块（`hls::stream` 读写、end-flag 约定、`ap_int`、`extern "C"` DUT）串起来。

**目标**：仿照 `stream_dup` 的用例结构，为 `stream_split` 写一个最小的 L1 用例，包含 `extern "C"` DUT + testbench，并解释每条流为什么带 end flag。

**输入约定**：用 LSB 拆分。输入流位宽 `WIn = 64`，每路输出 `WOut = 16`，输出路数 `NStrm = 4`（正好 `16 × 4 = 64`，无丢弃）。

### 5.1 参考实现（示例代码）

下面是一个完整的 `test.cpp`（**示例代码**，参照 `stream_dup` 与 `split_lsb` 风格手写，非仓库已有文件）：

```cpp
// 示例代码：stream_split 的最小 extern "C" DUT 用例
#include <iostream>
#include "xf_utils_hw/stream_split.hpp"   // 间接包含 types.hpp → ap_int.h / hls_stream.h

#define WIN  16                                // 每路输出 16 位
#define NOUT 4                                 // 输出 4 路
#define WIN_IN (WIN * NOUT)                    // 输入 64 位
#define N 5                                    // 喂 5 个数据 beat

// ---- DUT：钉死模板参数，extern "C" 固化符号名 ----
extern "C" void split_dut(hls::stream<ap_uint<WIN_IN> >& istrm,
                          hls::stream<bool>& e_istrm,
                          hls::stream<ap_uint<WIN> > ostrms[NOUT],
                          hls::stream<bool>& e_ostrm) {
    // 用 LSBSideT() 这个空标签实例选择「从低位起拆」的重载
    xf::common::utils_hw::streamSplit<WIN_IN, WIN, NOUT>(
        istrm, e_istrm, ostrms, e_ostrm,
        xf::common::utils_hw::LSBSideT());
}

// ---- testbench：仅 csim 编译 ----
#ifndef __SYNTHESIS__

int main() {
    hls::stream<ap_uint<WIN_IN> > istrm;
    hls::stream<bool> e_istrm;
    hls::stream<ap_uint<WIN> > ostrms[NOUT];
    hls::stream<bool> e_ostrm;

    // 生产数据：把 4 个 16 位数打包进一个 64 位 beat
    for (int d = 1; d <= N; ++d) {
        ap_uint<WIN_IN> packed = 0;
        for (int k = 0; k < NOUT; ++k) {
            packed.range((k + 1) * WIN - 1, k * WIN) = (ap_uint<WIN>)(d * 10 + k);
        }
        istrm.write(packed);
        e_istrm.write(false);                  // 每个 beat 配一个 false
    }
    e_istrm.write(true);                       // 终止符（N 个 false + 1 个 true）

    split_dut(istrm, e_istrm, ostrms, e_ostrm);

    // 校验：按 stream_split 的「单共享 end 流」约定读
    int nerr = 0, beat = 0;
    bool last = e_ostrm.read();
    while (!last) {
        last = e_ostrm.read();
        for (int k = 0; k < NOUT; ++k) {
            ap_uint<WIN> got = ostrms[k].read();
            ap_uint<WIN> exp = (ap_uint<WIN>)((beat + 1) * 10 + k);
            if (got != exp) { nerr++; std::cout << "mismatch k=" << k << "\n"; }
        }
        ++beat;
    }
    std::cout << (nerr ? "\nFAIL\n" : "\nPASS\n");
    return nerr;
}

#endif
```

### 5.2 操作步骤

1. 在 `utils/L1/tests/` 下**新建一个实验目录**（如 `my_split_dut/`），把上面的 `test.cpp` 放进去——**不要修改源码、不要写进仓库已有目录**。
2. 从 `utils/L1/tests/stream_dup/` 复制 `Makefile`、`hls_config.tmpl`、`run_hls.tcl`，并把 `description.json` 里的 `topfunction` 从 `dut0` 改成 `split_dut`（参考 [utils/L1/tests/stream_dup/description.json:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L14) 的写法）。
3. `make run TARGET=csim`。

### 5.3 需要观察的现象与预期结果

- DUT 是 `extern "C" void split_dut`，模板参数 `<64, 16, 4>` 被钉死，与 `description.json` 的 `topfunction: split_dut` 对应（模块 4.4）。
- 输入 64 位 beat 被 `.range()` 切成 4 路 16 位（模块 4.3）；LSB 一侧是第 0 路。
- testbench 按「N 个 false + 1 个 true」喂数据（模块 4.2 的不变式）；注意 `stream_split` 只用**一个共享** `e_ostrm`，校验时按「先读一个 flag 进 while、每轮再读一个」的前瞻式节奏消费（与 `stream_dup` 同款节奏，见 [utils/L1/include/xf_utils_hw/stream_split.hpp:110-124](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_split.hpp#L110-L124)）。
- 预期 csim 输出 `PASS`。**待本地验证**（依赖 Vitis 工具链是否就绪，见 u2-l1）。

### 5.4 解释「每个流为何要带 end flag」

- **输入端**：`split_dut` 不知道 `istrm` 里有几个 beat，必须靠 `e_istrm` 的终止符停循环；漏掉它，[stream_split.hpp:111](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_split.hpp#L111) 的 `while (!last)` 会一直读到数据流被读空、继续 `istrm.read()` 而报错。
- **输出端**：下游消费者同样不知道 `ostrms[k]` 何时结束，必须靠 `e_ostrm` 告知。由于 4 路输出每拍同步产生、同生同灭，**一个共享 end 流**就足以表达「这一组 4 路都结束了」——这正是 `stream_split` 选 `hls::stream<bool>& e_ostrm`（单流）而非 `e_ostrms[NOUT]`（数组）的原因，也是它和 `stream_dup`（每路独立结束，故用数组）的关键区别（模块 4.2）。

## 6. 本讲小结

- **`hls::stream` 是单向 FIFO**：元素只能顺序、单次读写，强制流式访问，天然映射成 II=1 的硬件流水；掌握 `read/write`（阻塞）与 `read_nb/write_nb`（非阻塞）的区别。
- **end-flag 流是流的「信号兵」**：`hls::stream` 不带长度信息，必须用伴生的 `hls::stream<bool>` 标记结束；`stream_dup` 用前瞻式消费，对 N 个数据读 N+1 个 flag，**拓扑跟着数据走**（`stream_dup` 每路一个 end 数组，`stream_split` 共享一个）。
- **`ap_int<N>` 表达任意位宽**：`types.hpp` 在引入 `ap_int.h` 前把 `AP_INT_MAX_W` 设到 4096；`.range(hi, lo)` 切片是「宽 beat 切多路窄通道」的核心手段，位宽（datawidth）直接放大带宽。
- **DUT 是模板内核的实例化壳**：`extern "C"` 非模板函数钉死参数、固化符号名，名字与 `description.json` 的 `topfunction` 对应；`__SYNTHESIS__` 宏让 `test.cpp` 同时充当 DUT（综合）与 testbench（仿真）。
- **三个文件的协作**：`types.hpp` 提供类型地基，`common.hpp` 提供断言与模板元工具，`stream_dup.hpp` + `test.cpp` 示范完整约定。

## 7. 下一步学习建议

- 本讲只读了 `streamDup` 的实现骨架，刻意没展开 `#pragma HLS pipeline/unroll` 对硬件的深层影响。下一讲 **u3-l2「HLS pragma 如何映射硬件」** 会以 `stream_dup.hpp` 的 `II=1` 与 `unroll` 为例，讲透 II 对吞吐、unroll 对并行度与资源的定量影响。
- 想先横向认识 `utils` 全家桶的读者，可继续 **u3-l3「utils 流式原语目录」**，浏览 `axi_to_stream / stream_combine / multiplexer / uram_array` 等同伴原语。
- 若你更关心「主机怎么调用内核」，可以先跳到 **u4 单元**——本讲建立的「`extern "C"` DUT 名字 = 主机按名查找的内核符号」正是 u4 的起点。
- 推荐继续精读的源码：[utils/L1/include/xf_utils_hw/stream_split.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_split.hpp)（对比 LSB/MSB 两个重载）与 [utils/L1/include/xf_utils_hw/enums.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/enums.hpp)（看「标签类型选重载」这一惯用法）。
