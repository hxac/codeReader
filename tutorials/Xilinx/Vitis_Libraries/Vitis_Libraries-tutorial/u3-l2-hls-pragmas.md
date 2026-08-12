# HLS pragma 如何映射硬件

## 1. 本讲目标

上一讲（u3-l1）我们建立了内核之间「数据怎么流」的约定——`hls::stream`、end-flag 流、`ap_int` 宽类型与 `extern "C"` DUT 封装。本讲把镜头从「数据流」转向「控制流与硬件映射」：**同样一段 C++ 循环，为什么有的能每个时钟周期吐一个结果、有的却要十拍才能算一个？答案就在 `#pragma HLS ...` 这几行指令里。**

学完本讲你应该能够：

- 说出 `#pragma HLS pipeline II=1` 如何决定内核**吞吐率**，以及把 II 改大对吞吐的影响；
- 说出 `#pragma HLS unroll` 如何决定循环的**并行度与资源占用**，以及它为什么离不开 `array_partition`；
- 说出 `#pragma HLS dataflow` 如何在**函数/任务级**把多个内核串成流水线；
- 读懂 `XF_UTILS_HW_STATIC_ASSERT` 这类**编译期约束**在模板内核里扮演的「护栏」角色。

本讲全部结论都基于 `utils` 库的真实头件，不引入假设代码。

## 2. 前置知识

在进入 pragma 之前，先建立三个直觉。它们是后面所有讨论的基础。

**直觉一：HLS 编译器把 C++ 翻译成 RTL。** 高层综合（High-Level Synthesis）不是「解释执行」C++，而是把每个函数、每个循环编译成真实的寄存器与组合逻辑。一段 `for` 循环可能被编译成「一个运算单元反复使用」（串行、省资源、慢），也可能被编译成「多个运算单元同时跑」（并行、费资源、快）。pragma 就是你给编译器的「调度指令」。

**直觉二：吞吐、延迟、面积是三件事。**

- **吞吐率（throughput）**：单位时间能处理多少个数据，单位通常是「个/周期」。流式内核最看重这个。
- **延迟（latency）**：从输入到输出要经过多少个时钟周期。控制类应用（如电机）看重这个。
- **面积（area）**：消耗多少 LUT / FF / BRAM / DSP / URAM。决定内核能不能装进芯片。

这三者常常互相矛盾，pragma 就是你调节这三者平衡的旋钮。

**直觉三：II 是 HLS 里最重要的一个数字。** II（Initiation Interval，启动间隔）= 连续两次「新的输入进入流水线」之间相隔的时钟周期数。II=1 意味着每个周期都能喂一个新数据——这是流式内核追求的极致。II=2 意味着每两个周期才能喂一个，吞吐直接减半。本讲的核心就是围绕 II、unroll、dataflow 这三把旋钮展开。

## 3. 本讲源码地图

本讲涉及的关键文件，全部来自 `utils` 库的 L1 头件：

| 文件 | 作用 |
| --- | --- |
| `utils/L1/include/xf_utils_hw/stream_dup.hpp` | 流复制内核 `streamDup`，是 `pipeline II=1` + `unroll` 的最小范本，也是本讲的实践对象。 |
| `utils/L1/include/xf_utils_hw/common.hpp` | 公共逻辑，定义 `XF_UTILS_HW_STATIC_ASSERT` 宏，是「静态断言约束」模块的核心。 |
| `utils/L1/include/xf_utils_hw/stream_combine.hpp` | 多流合并内核 `streamCombine`，演示 `unroll` 与 `array_partition` 的配合，以及嵌套展开。 |
| `utils/L1/include/xf_utils_hw/stream_discard.hpp` | 流丢弃内核 `streamDiscard`，用 `dataflow` 把多条丢弃任务并起来跑，是 `dataflow` 的最小范本。 |
| `utils/L1/include/xf_utils_hw/axi_to_stream.hpp` | AXI→stream 转换内核 `axiToStream`，**四件套齐用**：`DATAFLOW` + `STATIC_ASSERT` + `STREAM/bind_storage` + 内部的 `pipeline/unroll`，是综合实践的解剖对象。 |

> 说明：本讲为了讲清 `dataflow`，额外引用了 `stream_discard.hpp` 与 `axi_to_stream.hpp`（在任务规格列出的三个文件之外）。这是因为 `stream_dup.hpp` / `stream_combine.hpp` 里没有 `dataflow` 用法，必须引入真实范例，避免编造。

## 4. 核心概念与源码讲解

按四个最小模块拆分：`pipeline II=1` → `unroll` → `dataflow` → `静态断言约束`。

### 4.1 pipeline II=1（循环级流水，决定吞吐）

#### 4.1.1 概念说明

一个 `for` 循环默认会被综合成「顺序执行」：第一次迭代算完（比如花 5 拍），再开始第二次迭代。如果循环跑 N 次，总周期数 ≈ N × 5。

`#pragma HLS pipeline II=N` 告诉编译器：把循环体做成一条**流水线**——像工厂流水线一样，不必等一个零件完全走完所有工序，下一个零件就能进第一道工序。N 是相邻两次「进料」之间的周期间隔，即 II。

- II=1 是流式内核的黄金目标：每个周期都能吃进一个新数据、吐出一个结果，吞吐 = 1 个/周期。
- II 越大，进料越稀疏，吞吐越低。

#### 4.1.2 核心流程

设循环体单次迭代的流水线深度为 \(D\)（即一个数据从进到出经历的级数），循环次数为 \(N\)，启动间隔为 \(II\)。

未流水（顺序）时，总周期数约为：

\[
T_{\text{顺序}} \approx N \times D
\]

流水后，第一个数据花 \(D\) 拍走完整条线，之后每 \(II\) 拍产出一个，总周期数约为：

\[
T_{\text{流水}} \approx D + II \times (N - 1) \approx II \times N \quad (\text{当 } N \gg D)
\]

吞吐率为：

\[
\text{throughput} = \frac{1}{II} \quad \text{（个/周期）}
\]

关键推论（直接对应本讲的实践任务）：

- II 从 1 改成 2 → 吞吐减半，处理 N 个数据的总周期数约翻倍；
- II 并不是想设多少就能设多少，若循环体里存在**资源冲突**（如同一块 RAM 一周期只能读一次）或**数据依赖**（下次迭代要用上次的结果），编译器会被迫把 II 拉大，这叫「II 违例（II violation）」。

#### 4.1.3 源码精读

`streamDup` 的第一份重载是 `pipeline II=1` 的最小范本：一个 `while` 循环不断从输入流读数据，复制成 `_NStrm` 份写出。

```cpp
bool e = e_istrm.read();
while (!e) {
#pragma HLS pipeline II = 1
    _TIn tmp;
    e = e_istrm.read();
    tmp = istrm.read();
    for (int i = 0; i < _NStrm; i++) {
#pragma HLS unroll
        ostrms[i].write(tmp);
        e_ostrms[i].write(0);
    }
}
```

[utils/L1/include/xf_utils_hw/stream_dup.hpp:92-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L92-L108) —— 这段就是整个复制逻辑。注意 `#pragma HLS pipeline II = 1` 加在 `while` 上，意味着这个外层循环被流水化，目标是每周期处理一个 `tmp`。

观察两个细节：

1. pragma 写在循环**正上方**，作用于紧随其后的循环（这是 HLS 的作用域规则）。
2. 第二份重载用的是大写 `#pragma HLS PIPELINE II = 1`，[utils/L1/include/xf_utils_hw/stream_dup.hpp:122](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L122)。HLS 的 pragma **不区分大小写**，`pipeline` 与 `PIPELINE` 完全等价，全库混用。

#### 4.1.4 代码实践

> 本实践直接采用任务规格指定的练习，并在此基础上给出可观察的预期。

1. **实践目标**：亲眼看 II 翻倍对吞吐与资源的影响。
2. **操作步骤**：
   - 备份 `utils/L1/include/xf_utils_hw/stream_dup.hpp`（本练习会临时改动源文件，完成后请还原）。
   - 把 [stream_dup.hpp:94](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L94) 的 `#pragma HLS pipeline II = 1` 改成 `#pragma HLS pipeline II = 2`。
   - 在 `utils/L1/tests/stream_dup` 下执行 `make run TARGET=csynth`（这一档的用法与上一讲 u2-l3 一致，csynth 是首个产出硬件报告的阶段）。
   - 还原源文件。
3. **需要观察的现象**：打开生成的综合报告（顶层函数名为 `dut0`，参见 [description.json:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L14)，故报告文件为 `test.prj/solution1/.../dut0_csynth.rpt`），在 **Loop** 表里看该循环的 `II` 与 `Latency` 列，在 **Utilization** 表里看 LUT/FF/BRAM/DSP 估计。
4. **预期结果**：
   - **Loop II**：II=1 时理想达成 1；改成 2 后应达成 2（或接近）。
   - **Loop Latency**：与 trip count 的关系近似满足 \(T \approx II \times N\)，II 翻倍 → 处理相同数据量的总周期数约翻倍，即**吞吐减半**。
   - **资源**：II 变化主要改延迟与吞吐，对 `streamDup` 这种纯复制内核，LUT/FF 变化不大，DSP 预计仍为 0（无算术）。
   - 具体数值**待本地验证**（取决于工具版本、平台 part 与 `dut0` 实例化的 `NUM_COPY` 等模板参数）。
5. **一句话解释**：II 是进料间隔，II 从 1 变 2 意味着每两个周期才喂一个数据，所以吞吐减半——这正是 pragma 翻译成硬件吞吐的直接体现。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `#pragma HLS pipeline II = 1` 整行删掉，`streamDup` 的外层 `while` 默认会怎样被综合？

**参考答案**：删除后该循环默认不流水、不展开，会被综合成顺序执行——每个数据要等 `e_istrm.read()`、`istrm.read()` 以及内部 `unroll` 后的若干写操作全部完成，下一拍才能读下一个数据，吞吐从「≈1 个/周期」暴跌到「≈1 个/每迭代周期」，但资源最省。

**练习 2**：某内核设定 `II=1`，但综合报告显示**达成 II=3**，最可能的两类原因是什么？

**参考答案**：① 资源端口受限（如某个数组挂在单端口 RAM 上、一周期只能读一次，迭代要访问多次）；② 存在跨迭代的**数据依赖**（本次计算要用上一次的结果，不得不等）。解决办法通常是 `array_partition` 拆分存储、或重构算法以消除递推。

---

### 4.2 unroll 展开（循环级并行，决定并行度与面积）

#### 4.2.1 概念说明

`#pragma HLS unroll` 把一个 `for` 循环「复制」成多份，让多个迭代**同一周期内并行执行**。这是与 `pipeline` 互补的另一条并行化路线：

- `pipeline`：把**一个**迭代拆成多级流水线，靠时间重叠换吞吐；
- `unroll`：把**多个**迭代并排放在一起，靠空间并行换吞吐，代价是面积。

不带参数的 `unroll` 表示**完全展开**（fully unroll），即把循环彻底摊平——要求循环次数在编译期已知且不太大。带参数 `unroll factor=F` 表示每次展开 F 份。

#### 4.2.2 核心流程

设循环次数为 \(N\)、展开因子为 \(F\)：

- 展开后循环变成 \(\lceil N/F \rceil\) 个「组」，每组 \(F\) 个迭代并行；
- 该循环体耗时约为原来的 \(1/F\)（前提是没有访存瓶颈）；
- 占用的运算资源约为原来的 \(F\) 倍。

关键陷阱：unroll 之后，循环体里若访问数组，多个并行迭代会**同时**访问数组。HLS 默认把数组综合成 RAM，而一块 RAM 一周期只有 1~2 个端口——这会卡住并行，迫使 II 变大。解决办法是配套使用 `#pragma HLS array_partition`，把一块 RAM 拆成多块，每个并行迭代访问独立的块。

#### 4.2.3 源码精读

回到 `streamDup` 的内层循环——它把一个 `tmp` 同时写到 `_NStrm` 条输出流：

```cpp
for (int i = 0; i < _NStrm; i++) {
#pragma HLS unroll
    ostrms[i].write(tmp);
    e_ostrms[i].write(0);
}
```

[utils/L1/include/xf_utils_hw/stream_dup.hpp:98-102](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L98-L102) —— 这里 `unroll` 把 `_NStrm` 次写操作完全展开成 `_NStrm` 个并行的 `write`，从而在一个周期内同时驱动 `_NStrm` 条输出流。注意它能干净展开、不被访存卡住，是因为 `ostrms` 是**流数组**（每条流是独立 FIFO，天然各自有端口），而不是一块共享 RAM。

更典型的「unroll + array_partition」配合出现在 `streamCombine` 里：

```cpp
bool b[_NStrm][_NStrm];
#pragma HLS array_partition variable = b complete dim = 1
// ...
ap_uint<_WIn> tmp[_NStrm][_NStrm];
#pragma HLS array_partition variable = tmp complete dim = 1
// ...
while (!e) {
#pragma HLS pipeline II = 1
    for (int i = 0; i < _NStrm; i++) {
#pragma HLS unroll
        tmp[0][i] = istrms[i].read();
        b[0][i] = bb[i];
    }
```

[utils/L1/include/xf_utils_hw/stream_combine.hpp:168-181](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_combine.hpp#L168-L181) —— 这里 `b` 和 `tmp` 是普通 C 数组，会被综合成 RAM。为了让内层 `unroll` 循环对 `dim=1`（第一维）的 \(i\) 并行访问，必须先用 `array_partition ... complete dim=1` 把第一维**完全拆分**成 \(\_NStrm\) 个独立寄存器。没有这两行 partition，`unroll` 会被 RAM 端口卡死、II 上升。

对比要点：

| 手段 | 作用对象 | 效果 | 代价 |
| --- | --- | --- | --- |
| `unroll` | 循环 | 多迭代并行 | 运算资源 ×F |
| `array_partition` | 数组 | 拆成多块，解除端口瓶颈 | RAM→寄存器，FF/LUT 上升 |

`streamCombine` 里还有**嵌套展开**的例子：外层 `for k` 与内层 `for j` 都带 `unroll`（[stream_combine.hpp:183-207](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_combine.hpp#L183-L207)），把 \(O(\_NStrm^2)\) 量级的列重排逻辑全部摊在一个周期内完成——典型的「用面积换吞吐」。

#### 4.2.4 代码实践

1. **实践目标**：体会 unroll 必须配 array_partition，否则会被端口卡住。
2. **操作步骤**（源码阅读型，无需综合也能理解）：
   - 打开 [stream_combine.hpp:168-181](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_combine.hpp#L168-L181)。
   - 设想把 `array_partition` 那两行**删掉**，再分析 `tmp[0][i]` 在 `unroll` 后会发生什么。
3. **需要观察的现象**：`tmp[_NStrm][_NStrm]` 若不拆分，被综合成一块 RAM；展开后的 `_NStrm` 个 `tmp[0][i]` 写入要在同一周期打到这块 RAM 的不同地址，但单端口 RAM 一周期只接受一次写。
4. **预期结果**：HLS 为了满足端口约束，会强行把 II 拉大（II ≥ _NStrm），循环不再是每周期一次，性能崩塌；恢复 `array_partition` 后各 `tmp[0][i]` 落在独立寄存器上，II 才能回到 1。
5. **结论**：**unroll 是「下订单」，array_partition 是「修配套道路」**，两者通常成对出现。具体 II 数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`streamDup` 内层 `unroll` 写流数组时，为什么**不需要** `array_partition`？

**参考答案**：因为 `ostrms[i]` 是 `hls::stream` 数组，每条流在硬件里是独立的 FIFO，各自有独立读写端口，不存在「多端口争抢一块 RAM」的问题，所以展开后天然可并行。

**练习 2**：完全展开（`unroll` 不带 factor）对循环次数有什么编译期要求？

**参考答案**：完全展开要求循环边界在**编译期已知且常量**，否则编译器无法确定要复制几份；此外展开因子过大（如上万次）会爆炸式占用资源，故完全展开只适合小循环，大循环应使用 `unroll factor=F` 部分展开。

---

### 4.3 dataflow 任务级流水（函数级流水，决定端到端吞吐）

#### 4.3.1 概念说明

`pipeline` 与 `unroll` 作用在**循环级**，而 `dataflow` 作用在**函数/任务级**。它把一个函数内的若干**子函数**通过 `hls::stream` 串联起来，让它们作为独立的 stage **同时运行**——前一个 stage 算出一批数据写进流，后一个 stage 立刻从流里读走，形成「任务级流水线」。

这是 AIE 与 PL 加速库里把「读数据→计算→写数据」三大块并起来的核心手段：PL 搬运器（mm2s）→ 计算内核 → 搬运器（s2mm）三段同时跑，端到端吞吐由最慢的一段决定，而总延迟≈各段延迟之和。

#### 4.3.2 核心流程

设有 \(S\) 个 stage 串联，第 \(k\) 个 stage 的单数据延迟为 \(L_k\)、吞吐为 \(r_k\)：

- **端到端吞吐** \(= \min_k r_k\)（木桶效应，由最慢 stage 决定）；
- **首数据端到端延迟** \(\approx \sum_{k=1}^{S} L_k\)；
- **稳态吞吐**与延迟**不**叠加——稳态下每个周期整条线产出 \(\min_k r_k\) 个结果。

`dataflow` 的纪律（所有 Vitis 库都遵守）：

1. stage 之间**只能用 `hls::stream`（或 `hls::stream of packets`）传递**，不能用普通变量/指针；
2. 同一个流只能「一个 stage 写、一个 stage 读」，**不能**多读多写交叉；
3. 读到的数据要**顺序**消费（FIFO 语义）。

违反纪律会让编译器无法证明安全性，从而拒绝生成 dataflow 或退回顺序执行。

#### 4.3.3 源码精读

最小、最干净的 `dataflow` 范本是 `streamDiscard` 的第一份重载：它把 `_NStrm` 条流的丢弃任务并起来跑。

```cpp
template <typename _TIn, int _NStrm>
void streamDiscard(hls::stream<_TIn> istrms[_NStrm], hls::stream<bool> e_istrms[_NStrm]) {
#pragma HLS dataflow
    for (int i = 0; i < _NStrm; ++i) {
#pragma HLS unroll
        streamDiscard<_TIn>(istrms[i], e_istrms[i]);
    }
}
```

[utils/L1/include/xf_utils_hw/stream_discard.hpp:83-90](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_discard.hpp#L83-L90) —— 这里 `dataflow` 作用于外层函数体，`unroll` 把循环展开成 `_NStrm` 次对**单流版** `streamDiscard<_TIn>(istrms[i], ...)` 的调用；于是 `_NStrm` 个子函数成为 `_NStrm` 个并行 stage，同时消费各自输入流。每条输入流是独立的 FIFO，满足「一写一读」纪律。

更工程化的范例是 `axiToStream`，它展示了 dataflow 经典的「读 → 拆」两段式：

```cpp
template <int _BurstLen, int _WAxi, typename _TStrm>
void axiToStream(ap_uint<_WAxi>* rbuf, const int num, hls::stream<_TStrm>& ostrm, hls::stream<bool>& e_ostrm) {
    XF_UTILS_HW_STATIC_ASSERT(_WAxi % sizeof(_TStrm) == 0, "AXI port width is not multiple of stream element width.");
    XF_UTILS_HW_STATIC_ASSERT((_WAxi == 8) || ... || (_WAxi == 1024),
                              "AXI port width must be power of 2 and between 8 to 1024.");

#pragma HLS DATAFLOW
    static const int fifo_depth = _BurstLen * 2;
    // ...
    hls::stream<ap_uint<_WAxi> > vec_strm;
#pragma HLS bind_storage variable = vec_strm type = FIFO impl = LUTRAM
#pragma HLS STREAM variable = vec_strm depth = fifo_depth

    details::read_to_vec<_WAxi, _BurstLen>(rbuf, num, scal_vec, vec_strm);
    details::split_vec<_WAxi, _TStrm, scal_vec>(vec_strm, num, 0, ostrm, e_ostrm);
}
```

[utils/L1/include/xf_utils_hw/axi_to_stream.hpp:410-430](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L410-L430) —— 这段是 dataflow 的教科书式结构：

1. 在两个子任务之间声明一条 `hls::stream`（`vec_strm`）作为「传送带」；
2. `#pragma HLS STREAM variable = vec_strm depth = fifo_depth` 显式设定传送带的 FIFO 深度；
3. `#pragma HLS bind_storage ... impl = LUTRAM` 指定这条 FIFO 用 LUTRAM 实现（而不是 BRAM），因为深度浅、要求低延迟；
4. `read_to_vec`（从 AXI 缓冲读宽 beat）和 `split_vec`（把宽 beat 切成窄元素）两个子任务**通过 `vec_strm` 串联**，在 `DATAFLOW` 作用下并行流水——前者持续往 FIFO 写，后者持续从 FIFO 读。

观察：`dataflow`（[stream_discard.hpp:85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_discard.hpp#L85)）与 `DATAFLOW`（[axi_to_stream.hpp:417](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L417)）大小写不同但等价，再次印证 pragma 不分大小写。

#### 4.3.4 代码实践

1. **实践目标**：看清 dataflow 三要素（子任务 + 传送带 FIFO + 纪律）。
2. **操作步骤**（源码阅读型）：
   - 打开 [axi_to_stream.hpp:417-430](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L417-L430)。
   - 在纸上画两个方框 `read_to_vec` 和 `split_vec`，中间用 `vec_strm` 连接。
   - 标注：`read_to_vec` 的输出是 `vec_strm`，`split_vec` 的输入也是 `vec_strm`——验证「一写一读」。
3. **需要观察的现象**：若把 `vec_strm` 改成普通指针传参，HLS 报告里 `DATAFLOW` 会报警或失效。
4. **预期结果**：dataflow 要求 stage 间只能用 stream，用普通变量/指针会破坏单向 FIFO 语义，编译器无法证明安全，从而**不生成任务级流水**，端到端退化为顺序执行。此现象**待本地验证**。
5. **延伸**：这条 `vec_strm` 的深度为何是 `_BurstLen * 2`？它对应 AXI 突发长度，保证读段能连续灌数据而不被消费段反压卡停——这是「最慢 stage 决定吞吐」的工程体现。

#### 4.3.5 小练习与答案

**练习 1**：dataflow 与 pipeline 都叫「流水」，它们的区别是什么？

**参考答案**：`pipeline` 作用在**单个循环/函数内部**，把循环体拆成多级；`dataflow` 作用在**多个子函数之间**，让它们作为独立 stage 通过 stream 串联。前者是「函数内的时间重叠」，后者是「函数间的空间并行」。

**练习 2**：若一个 dataflow 区域里有 3 个 stage，吞吐分别是 1、0.5、2 个/周期，端到端稳态吞吐是多少？

**参考答案**：\(\min(1, 0.5, 2) = 0.5\) 个/周期，由最慢的第二段决定（木桶效应）。要提升整体吞吐，必须优化最慢的那一段。

---

### 4.4 静态断言约束（编译期护栏，防模板误用）

#### 4.4.1 概念说明

前面三个 pragma 都是在「调性能」，而 `STATIC_ASSERT` 是在「防出错」。

`utils` 库的内核大多是**模板函数**，模板参数（如 `_NStrm`、`_WAxi`）由调用方传入。如果调用方传了非法值（比如「复制输出比输入还多」「AXI 位宽不是 2 的幂」），等综合到一半才报错就太晚了——csynth 一跑几十分钟。静态断言把这些约束**前移到编译期**：一编译就失败，错误信息直接告诉你哪个参数不合法。

#### 4.4.2 核心流程

静态断言的工作时机：

\[
\text{模板实例化} \;\xrightarrow{\text{STATIC_ASSERT}}\; \begin{cases} \text{条件为真} & \Rightarrow \text{编译继续（无任何运行时代价）} \\ \text{条件为假} & \Rightarrow \text{编译立即失败，打印诊断信息} \end{cases}
\]

它**不产生任何硬件**（零面积、零延迟），纯粹是编译期的布尔判断。在 C++11 及以上，它直接映射到语言内置的 `static_assert`。

#### 4.4.3 源码精读

先看宏定义。`common.hpp` 在文件末尾给出了两套断言：

```cpp
#ifndef __SYNTHESIS__
// for assert function.
#include <cassert>
#define XF_UTILS_HW_ASSERT(b) assert((b))
#else
#define XF_UTILS_HW_ASSERT(b) ((void)0)
#endif

#if __cplusplus >= 201103L
#define XF_UTILS_HW_STATIC_ASSERT(b, m) static_assert((b), m)
#else
#define XF_UTILS_HW_STATIC_ASSERT(b, m) XF_UTILS_HW_ASSERT((b) && (m))
#endif
```

[utils/L1/include/xf_utils_hw/common.hpp:208-220](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/common.hpp#L208-L220) —— 三个要点：

1. `XF_UTILS_HW_STATIC_ASSERT(b, m)` 在 C++11（`__cplusplus >= 201103L`）下展开为标准 `static_assert(b, m)`——编译期检查，csim 与 csynth 都生效。
2. 它接受两个参数：条件 `b` 与诊断字符串 `m`，失败时编译器把 `m` 打在错误里。
3. 与运行期 `XF_UTILS_HW_ASSERT` 区分：后者在综合模式下（`__SYNTHESIS__` 已定义）被替换成 `((void)0)`（空操作），因为 `assert` 依赖的运行期库在硬件里不存在；而 STATIC_ASSERT 始终是编译期的，不受 `__SYNTHESIS__` 影响。

再看真实用法。`streamDup` 第二份重载一进门就检查参数合法性：

```cpp
XF_UTILS_HW_STATIC_ASSERT(_NDStrm <= _NIStrm, "stream_dup cannot have more duplicated output than input.");
```

[utils/L1/include/xf_utils_hw/stream_dup.hpp:117](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L117) —— 含义：要复制的输出流数 `_NDStrm` 不能大于输入流数 `_NIStrm`。若有人误写 `streamDup<T, 2, 5, ...>`（输入 2 条却想复制出 5 路），编译立刻失败，错误信息直指「不能有比输入更多的复制输出」，而不是在运行时数组越界。

`axiToStream` 更密集，一进门两条断言守护 AXI 位宽：

```cpp
XF_UTILS_HW_STATIC_ASSERT(_WAxi % sizeof(_TStrm) == 0, "AXI port width is not multiple of stream element width.");
XF_UTILS_HW_STATIC_ASSERT((_WAxi == 8) || (_WAxi == 16) || ... || (_WAxi == 1024),
                          "AXI port width must be power of 2 and between 8 to 1024.");
```

[utils/L1/include/xf_utils_hw/axi_to_stream.hpp:412-415](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L412-L415) —— 第一条确保 AXI 宽 beat 能被整除成若干个流元素（否则 `split_vec` 切不动）；第二条确保 AXI 位宽是 8~1024 之间 2 的幂（这是 AXI 协议与物理通道的硬约束）。两条都是编译期常量比较，零运行时代价。

#### 4.4.4 代码实践

1. **实践目标**：触发一次 STATIC_ASSERT 失败，看清它的报错形态。
2. **操作步骤**（纯编译实验，几秒就出结果，不必跑 csynth）：
   - 在一个临时 `.cpp` 里 `#include "xf_utils_hw/stream_dup.hpp"`，写一行实例化：`xf::common::utils_hw::streamDup<int, 5, 2, 1>(...)`（即 `_NIStrm=2`、`_NDStrm=5`，故意违反 `_NDStrm <= _NIStrm`）。
   - 用 `g++ -std=c++14 -c` 编译（include 路径指向 `utils/L1/include`，注意还需 `ap_int.h` 所在的 Vitis HLS 头，本地若没有完整 HLS 环境可只读源码推导）。
3. **需要观察的现象**：编译在 [stream_dup.hpp:117](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L117) 处终止，错误信息里出现字符串 `"stream_dup cannot have more duplicated output than input."`。
4. **预期结果**：静态断言把「参数非法」从「综合几十分钟后崩溃」前移成「编译几秒钟即明确报错」。若本地无 HLS 头件，则**待本地验证**，但仍可从源码静态推断出该断言必然触发。
5. **一句话总结**：STATIC_ASSERT 是模板内核的「前置安检」，让你在写 DUT 的那一刻就知道参数对不对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `XF_UTILS_HW_STATIC_ASSERT` 用 `static_assert` 而不是 `assert`？

**参考答案**：因为被检查的是模板参数（编译期常量），`static_assert` 在**编译期**求值、零运行时代价、在综合模式下依然有效；而 `assert` 是运行期检查，且在综合模式下会被宏替换成空操作（硬件里没有标准库），无法守护模板参数。

**练习 2**：[axi_to_stream.hpp:412](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L412) 的断言 `_WAxi % sizeof(_TStrm) == 0` 守护的是哪条后续逻辑？

**参考答案**：守护 [axi_to_stream.hpp:420](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L420) 处的 `scal_vec = _WAxi / (8 * size0)` 与随后的 `split_vec`——只有 AXI 宽 beat 能被流元素位宽整除时，才能把一个宽 beat 干净地切成整数个流元素。

---

## 5. 综合实践

**任务：解剖 `axiToStream`——一个函数里集齐本讲四个模块。**

`axiToStream` 是本讲唯一「四件套齐用」的内核，请按下列步骤把它讲清楚：

1. **定位**：打开 [utils/L1/include/xf_utils_hw/axi_to_stream.hpp:410-430](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L410-L430)。
2. **静态断言**：找出函数开头两条 `XF_UTILS_HW_STATIC_ASSERT`（L412-415），逐条说明它们守护的参数与后续逻辑。
3. **dataflow**：标记 `#pragma HLS DATAFLOW`（L417），指出它把哪两个子任务（`read_to_vec`、`split_vec`）串成任务级流水，连接它们的「传送带」是哪条流（`vec_strm`）。
4. **stream 配套**：找到 `#pragma HLS STREAM ... depth`（L425）与 `#pragma HLS bind_storage ... impl=LUTRAM`（L424），说明它们如何配置这条传送带的深度与硬件实现。
5. **pipeline/unroll**：跳进 `details::read_to_vec` 与 `details::split_vec`（同一文件内），找出它们内部循环上的 `pipeline II=1` 与 `unroll`，说明这是「任务级 dataflow 内部还有循环级流水」的**两级并行**结构。
6. **画出整体**：用一张图把「AXI 缓冲 → read_to_vec → vec_strm(FIFO) → split_vec → ostrm」串起来，在每段上标注它用到的本讲 pragma/约束。

**预期产物**：一张标注了 STATIC_ASSERT、DATAFLOW、STREAM、pipeline、unroll 各自落点的数据流图，以及一句话总结——「静态断言管安全、dataflow 管任务并行、pipeline 管循环吞吐、unroll 管循环并行，四者各司其职又层层嵌套」。各 pragma 的精确效果**待本地验证**（需在真实 Vitis HLS 环境跑 csynth 看报告）。

## 6. 本讲小结

- `#pragma HLS pipeline II=N` 作用在**循环级**，II 是进料间隔，吞吐 \(=1/II\)；II=1 是流式内核的黄金目标，II 翻倍则吞吐减半。
- `#pragma HLS unroll` 把循环展开成多份并行，用面积换吞吐；它通常需要 `#pragma HLS array_partition` 配套，否则会被 RAM 端口卡住、迫使 II 上升。
- `#pragma HLS dataflow` 作用在**函数/任务级**，把多个子函数通过 `hls::stream` 串成流水线，端到端吞吐由最慢 stage 决定；要求 stage 间只用 stream、一写一读、顺序消费。
- `XF_UTILS_HW_STATIC_ASSERT`（展开为 `static_assert`）是模板内核的**编译期护栏**，零运行时代价，把参数误用前移到编译瞬间。
- HLS pragma **不区分大小写**（`pipeline`/`PIPELINE`、`dataflow`/`DATAFLOW` 等价），全库混用，阅读时不必纠结。
- 四者的层次关系：**dataflow（任务级）⊃ pipeline（循环级）⊃ unroll（迭代级）**，外加 STATIC_ASSERT 作为贯穿编译期的安全网——`axiToStream` 是同时看见这四层的最佳样本。

## 7. 下一步学习建议

- **横向应用**：下一讲 u3-l3「utils 流式原语目录」会系统过一遍 `axi_to_stream`/`stream_to_axi`/`stream_combine`/`multiplexer`/`stream_split` 等原语，届时你会看到本讲的 pragma 在更多真实内核里反复出现，建议带着「这个内核靠什么达到 II=1」的视角去读。
- **纵向加深**：进入第 6 单元（DSP 库）后，关注 `mixed_radix_fft` 等 AIE 内核如何用 **SSR（streaming split-radix）** 把 unroll 的思想放大到数据通路上——SSR 是「unroll + 宽 datawidth」的系统级版本。
- **动手巩固**：第 14 单元 u14-l2「从零编写自己的 L1 内核」会让你亲手写一个模板化流式内核，本讲的「pipeline + unroll + STATIC_ASSERT」三件套就是你写内核时要主动加上的标配。
- **报告解读**：若想更熟读综合报告，可回看 u2-l3 关于 II/latency/资源三字段的讲解，并用本讲的 `axiToStream` 跑一次 csynth，对照报告验证各 pragma 的实际效果。
