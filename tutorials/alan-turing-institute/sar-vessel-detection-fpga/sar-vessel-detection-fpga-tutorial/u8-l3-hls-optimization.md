# u8-l3 HLS 优化指令

## 1. 本讲目标

本讲是第八单元「HLS 后处理解码内核」的第三篇，承接 u8-l1（内核接口与数据打包）和 u8-l2（解码算法实现）。前面两讲回答了「内核**做什么**、数据怎么进出」，本讲回答「**怎么让它在 FPGA 上跑得快、又不把资源撑爆**」——也就是源码里那些 `#pragma HLS ...` 优化指令。

学完本讲，你应当能够：

- 理解 `#pragma HLS UNROLL` 如何把循环展开成并行副本，以及它**复制的是计算（吃 LUT/DSP）而非存储（不吃 BRAM）**这一关键区别；
- 掌握 `#pragma HLS PIPELINE II=1` 与 `#pragma HLS ARRAY_PARTITION ... complete` 为什么必须**配合使用**（流水需要多端口并行读写，而分区正是为此提供端口）；
- 理解用**局部写指针 `tb`** 隔离跨迭代标量依赖、帮助 HLS 高层综合分析依赖、从而保住 `II=1` 的技巧；
- 能够在 [decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp) 中逐条找出并分类所有 `UNROLL` / `PIPELINE` / `ARRAY_PARTITION` 指令，说出每条作用在哪个循环或数组上。

本讲对应的最小模块有三个：**UNROLL 并行化**、**PIPELINE / ARRAY_PARTITION 配合**、**局部写指针与依赖**。

## 2. 前置知识

在进入源码前，先用三段话建立 HLS（High-Level Synthesis，高层综合）最核心的几个直觉概念。它们是读懂本讲每个 pragma 的前提。

**(1) FPGA 没有「指令」，只有「电路」。** CPU 跑一段 `for` 循环是「一条指令一条指令地取指、译码、执行」，每次迭代占用若干时钟周期。HLS 则把 C++ 的 `for` 循环翻译成**真正的硬件电路**：循环体里的每一次加法、乘法、`expf` 都变成芯片上的一个运算器（加法器、乘法器、浮点核）。所以 FPGA 上的「快」不是靠高频，而是靠「同一时刻有很多运算器在并行干活」。

**(2) 衡量「快」的两个指标：Latency 与 II。**

- **Latency（延迟）**：算完一个循环（或一次调用）需要的总时钟周期数。越少越快。
- **Initiation Interval，II（启动间隔）**：在**流水化**的循环里，每隔多少个周期能够**接收一个新的输入**。`II=1` 意味着每个周期都能喂进新数据，是流水线的理想状态。若循环体里有跨迭代的依赖（下一轮要用到上一轮算出的值），HLS 不得不把 II 拉大（例如 `II=11`），吞吐就掉到 1/11。

可以用流水工厂打比方：Latency 是「一件产品从进厂到出厂的总工时」，II 是「每隔多久能从进料口投一件新产品」。理想流水线 II=1，就像每秒钟都能投一件新品；而 Latency 则是单件的总耗时。

**(3) FPGA 的四类核心资源：LUT、FF、BRAM、DSP（外加 URAM）。** 这和 u5-l1 讲的 KV260 资源表是同一套词。

| 资源 | 全称 | 在本内核里对应什么 |
|------|------|-------------------|
| **LUT** | Look-Up Table（查找表） | 通用逻辑：比较器、地址计算、状态机、整数运算 |
| **FF** | Flip-Flop（触发器） | 寄存器：保存中间状态、流水线寄存器 |
| **BRAM** | Block RAM（块存储） | 片上存储：数组（如 `local_boxes_x[2048]`） |
| **DSP** | DSP Slice（数字信号处理单元） | 高效乘加、浮点运算核心（`expf`/`fmul`/`fadd`） |
| **URAM** | Ultra RAM（超大块存储） | 更大容量的片上存储（本内核未用） |

本讲最重要的一个直觉：**`UNROLL` 复制运算器，所以主要吃 LUT 和 DSP；它几乎不吃 BRAM**（BRAM 的用量由数组大小决定，与循环是否展开无关）。这一点会在 4.1 反复验证。

## 3. 本讲源码地图

本讲只聚焦一个文件，但会引用一份综合报告作为「真实资源数字」的佐证。

| 文件 | 作用 |
|------|------|
| [platform/post_processing/decode_krnl/decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp) | HLS 解码内核的**全部源码**，本讲所有 pragma 都在这里。接口（`INTERFACE`）部分已在 u8-l1 讲过，解码算法在 u8-l2 讲过，本讲只讲优化指令。 |
| [platform/post_processing/decode_krnl/decode_kernel.h](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.h) | 内核函数原型声明。 |
| [platform/post_processing/decode_krnl/hls_config.cfg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/hls_config.cfg) | HLS 综合配置：目标器件 `part=xck26-sfvc784-2LV-c`、`clock=5`（即 200 MHz）、顶层函数 `syn.top=decode_kernel`。 |
| [platform/post_processing/reports/hls_compile.rpt](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/reports/hls_compile.rpt) | 仓库自带的 HLS 综合报告，含资源列（BRAM/DSP/FF/LUT/URAM）、II、Latency。**注意**：该报告标题为 `yolov8_decode_kernel`，是更早一版内核（接口含 `LAYER_LOOP`/`num_layers`/`batch_index`，与当前 `decode_kernel.cpp` 不同）的综合结果。其绝对数字不能直接套到当前文件，但目标器件一致、资源**量级与列结构**对理解优化权衡仍有参考价值。本讲引用它时会明确标注。 |

## 4. 核心概念与源码讲解

### 4.1 UNROLL 并行化（复制计算，吃 LUT/DSP 而非 BRAM）

#### 4.1.1 概念说明

`#pragma HLS UNROLL` 的作用是把一个 `for` 循环**展开**：与其让一份循环体跑 N 次，不如把循环体**复制 N 份**，让它们在同一时刻并行执行。

- 写 `#pragma HLS UNROLL`（不带参数）= **完全展开（full unroll）**：循环体复制「迭代次数」份，全部并行。
- 写 `#pragma HLS UNROLL factor=k` = 只展开成 k 份（部分展开）。

收益是**消除循环控制开销 + 并行执行**；代价是**复制循环体里的每一个运算器**。这里要建立的最重要的直觉是：

> **UNROLL 复制的是「计算电路」，主要消耗 LUT 与 DSP；它不复制「存储」，所以基本不吃 BRAM。**

为什么？因为循环体里的 `expf`、乘法、比较被复制了；但循环里读写的大数组（如 `input_data`、`local_boxes_x[2048]`）仍然是同一块存储，并不会因为循环展开而翻倍。BRAM 的用量由「你声明了多少个数组、每个多大」决定，与循环展不展开无关。

#### 4.1.2 核心流程

本内核里被 `UNROLL` 的循环可以分成两类：

**第一类：类别循环（很小，3 次）。** `CLASS_CHECK_LOOP`（遍历 3 个类别 `NUM_CLASSES=3`）完全展开，变成 3 个并行比较器，一眼就能判断「这个 anchor 是否至少有一个类别过阈值」。3 个比较器几乎不占资源，纯属「顺手消除循环开销」。

**第二类：距离 softmax 子循环（16 次，且被 4 个分支嵌套）。** 这是资源消耗的大头。回顾 u8-l2：每个 anchor 要对**左/上/右/下 4 条边**各做一次 **16-bin softmax**（DFL 距离分布解码）。源码用两层循环实现——外层 `DIST_BRANCH_LOOP`（4 次）、内层 `LOGIT_LOAD`/`MAX_FIND`/`EXPS_LOOP`/`WEIGHTED_MEAN`（各 16 次）——并且**两层都被完全展开**。

完全展开后的并行浮点算子数量（上界，未考虑 HLS 内部复用）：

\[
\text{并行 } \mathrm{expf} \text{ 核数} \le \underbrace{4}_{\text{DIST\_BRANCH}} \times \underbrace{16}_{\text{EXPS\_LOOP}} = 64
\]

每个 `expf` 浮点核要消耗 DSP 与 LUT。这正是本内核资源偏紧的根源。把 4.1.3 引用的真实报告读一遍就会发现：**LUT 是占比最高的资源，BRAM 反而极少**——这与「UNROLL 吃计算、不吃存储」完全吻合。

#### 4.1.3 源码精读

**类别检查循环**（3 次，完全展开，几乎零成本）：

[decode_kernel.cpp:L116-L121](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L116-L121) —— `CLASS_CHECK_LOOP` 完全展开成 3 个并行比较器，判断该 anchor 是否值得继续算 softmax。这里的 `#pragma HLS UNROLL` 没有参数，表示完全展开。

**距离分支外循环**（4 次，完全展开，复制内层整套 softmax 4 份）：

[decode_kernel.cpp:L130-L132](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L130-L132) —— `DIST_BRANCH_LOOP` 完全展开。展开后，左/上/右/下四条边的 softmax 在同一时刻并行计算。

**距离分支内循环**（16 次，完全展开，是浮点算子复制的主战场）：

[decode_kernel.cpp:L145-L147](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L145-L147) —— `LOGIT_LOAD` 完全展开，并行读取 16 个距离 bin 的 int8 logit。

[decode_kernel.cpp:L174-L180](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L174-L180) —— `EXPS_LOOP` 完全展开，**16 个 `expf` 并行计算**。配合外层 4 个分支，这是 DSP/LUT 的最大消耗点。

`MAX_FIND`（[L167-L171](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L167-L171)）和 `WEIGHTED_MEAN`（[L184-L189](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L184-L189)）同理完全展开，分别并行求最大值和加权均值。

**真实资源佐证（注意版本差异）。** 仓库自带的 [hls_compile.rpt:L22](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/reports/hls_compile.rpt#L22) 给出顶层模块 `yolov8_decode_kernel` 的资源估算（这是更早一版内核，仅作量级参考）：

```
|+ yolov8_decode_kernel | Timing | -0.49 | ... | BRAM 8 (2%) | DSP 201 (16%) | FF 37655 (16%) | LUT 54125 (46%) | URAM - |
```

读数：**LUT 46% 一家独大，DSP 16% 次之，BRAM 只有 2%（8 块）**。这正是「大量 `UNROLL` 复制浮点算子」的典型指纹——计算资源（LUT/DSP）被吃满，存储资源（BRAM）几乎没动。另外 `Timing Slack = -0.49` 为负，说明在 `clock=5`（200 MHz）下时序没收敛，过度的并行展开会让组合逻辑路径变深、拖累时序，这也是 UNROLL 的隐性代价之一。

#### 4.1.4 代码实践

> **实践目标**：把内核里所有 `#pragma HLS UNROLL` 全部找出来，分类标注它们各自作用在哪个循环、展开几份，并据此论证「把 `DIST_BRANCH_LOOP` 完全 UNROLL」对 LUT 与 BRAM 的影响。

**操作步骤**：

1. 打开 [decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp)。
2. 用编辑器搜索 `UNROLL`，应当找到 **6 处**未注释的 `#pragma HLS UNROLL`（另有 1 处 `factor=4` 在 L77 是注释掉的旧代码）。
3. 按下表逐条核对（行号、所在循环、trip count）：

   | pragma 位置 | 所在循环 | 迭代次数 | 展开后并行副本数 |
   |-------------|----------|----------|------------------|
   | L118 | `CLASS_CHECK_LOOP` | 3 | 3 |
   | L132 | `DIST_BRANCH_LOOP` | 4 | 4 |
   | L147 | `LOGIT_LOAD` | 16 | 16 |
   | L169 | `MAX_FIND` | 15 | 15 |
   | L176 | `EXPS_LOOP` | 16 | 16 |
   | L186 | `WEIGHTED_MEAN` | 16 | 16 |

4. 回答讨论题：**把 `DIST_BRANCH_LOOP` 完全 UNROLL 后，对 LUT 和 BRAM 各有什么影响？**

**需要观察的现象 / 预期结论**：

- 对 **LUT（与 DSP）**：显著增加。因为 `DIST_BRANCH_LOOP` 内部嵌着 `LOGIT_LOAD`/`EXPS_LOOP`/`WEIGHTED_MEAN`，它们各自又完全展开 16 份。外层再展 4 份，意味着整套 16-bin softmax 机器被**复制 4 份并行**，浮点算子（尤其是 `expf`，[hls_compile.rpt](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/reports/hls_compile.rpt) 显示每个 `fexp` 占 7 个 DSP）数量翻倍式增长 → LUT/DSP 上升。
- 对 **BRAM**：几乎无影响。BRAM 由数组大小决定（`local_boxes_*[2048]` 等），与「距离分支展不展开」无关。报告里 BRAM 仅 2%，正好印证这一点。
- **权衡**：如果不展开 `DIST_BRANCH_LOOP` 而改用 `PIPELINE`，能省下约 3/4 的浮点算子（LUT/DSP 下降），但四条边要**串行**算，Latency 变大。本设计选择「全展开换吞吐、接受 LUT 46% 的高占用」，是因为解码是 u7-l3 profiling 里识别出的主机开销主体，值得用面积换速度。

> 注：以上「完全 UNROLL」的描述对应当前源码的实际状态（`DIST_BRANCH_LOOP` 在 L132 **已经**是完全展开）。若你想亲手验证资源数字的变化，需要在本机用 Vitis HLS 2023.1 跑综合，比较「展开 vs. 改为 `PIPELINE`」两份报告的 LUT/BRAM 列——这一步**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：源码 L77 有一行被注释掉的 `// #pragma HLS UNROLL factor=4`。它和 L118 当前生效的 `#pragma HLS UNROLL`（无参数）在语义上有什么区别？

**参考答案**：`factor=4` 是**部分展开**，把循环体复制 4 份、剩余迭代仍按循环跑（需要迭代次数是 4 的倍数才最高效）；而无参数的 `#pragma HLS UNROLL` 是**完全展开**，把循环体复制「迭代次数」份（这里 `NUM_CLASSES=3`，即复制 3 份、零循环开销）。当前代码类别数只有 3，完全展开成本极低，所以选了无参数完全展开。

**练习 2**：假设把 `EXPS_LOOP`（L176）的 `UNROLL` 去掉、改回普通串行循环，预期 DSP 和 Latency 各如何变化？

**参考答案**：DSP 会大幅下降（不再需要并行实例化 16 个 `expf` 核，只需 1 个复用），但该循环的 Latency 会从「几乎 1 拍出 16 个结果」变成「16 拍串行」，单 anchor 的解码总延迟随之上升。这是 HLS 里最典型的「面积（资源）换时间（延迟）」取舍。

---

### 4.2 PIPELINE 与 ARRAY_PARTITION 的配合

#### 4.2.1 概念说明

**`#pragma HLS PIPELINE II=1`** 把循环改造成硬件流水线，目标是**每个时钟周期都能接收一个新输入**（II=1）。它不像 UNROLL 那样复制循环体，而是让循环体的不同阶段（如「读→算→写」）像工厂流水线一样重叠执行。

**`#pragma HLS ARRAY_PARTITION variable=x complete`** 把一个数组**完全拆分**成若干独立寄存器（而不是一块整 RAM）。一个 3 元素数组完全分区后，等价于 3 个独立寄存器，于是**同一时刻可以从 3 个不同的下标并行读/写**——相当于给数组开了多个独立的访问端口。

两者为什么要配合？因为 **PIPELINE 想要每拍都访问数组，而一块普通 BRAM 只有 1～2 个读写端口，会成为流水的瓶颈**。这时先用 `ARRAY_PARTITION` 把数组拆成多端口，才能支撑流水线每拍的并行访问。一句话总结：

> **分区是为了给流水线「开端口」；没有分区，流水线的 II 会被存储端口数卡住。**

#### 4.2.2 核心流程

本内核只有两个 `PIPELINE II=1`，夹着一个小数组 `cls_logits[3]`，构成一个完整的「分区 + 预取 + 流水消费」小生态：

1. **分区**：`cls_logits[NUM_CLASSES]`（3 元素）被 `ARRAY_PARTITION complete` 拆成 3 个独立寄存器；
2. **预取（流水写入）**：`PREFETCH_LOOP` 以 `II=1` 从 `input_data` 把 3 个类别 logit 读进 `cls_logits`；
3. **并行检查（4.1 讲的 UNROLL）**：`CLASS_CHECK_LOOP` 完全展开，**同一拍**读出 `cls_logits` 的 3 个元素做比较——这要求 3 个端口并行读，正是分区提供的；
4. **流水发射**：`CLASS_EMIT_LOOP` 以 `II=1` 逐个类别判断是否输出框，从 `cls_logits` 读、向 `local_boxes_*` 写。

如果省略第 1 步的分区，第 3 步的并行读会退化成「从一个单端口 RAM 分 3 拍读」，`CLASS_CHECK_LOOP` 就没法真正并行，流水的 II 也会被拖大。

#### 4.2.3 源码精读

**分区声明**（把 3 元素小数组拆成 3 个独立寄存器）：

[decode_kernel.cpp:L96-L98](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L96-L98) —— `int8_t cls_logits[NUM_CLASSES];` 紧跟 `#pragma HLS ARRAY_PARTITION variable=cls_logits complete dim=1`。`complete` 表示按第 1 维完全拆分，3 个元素变成 3 个独立寄存器。

**预取循环**（流水读 AXI）：

[decode_kernel.cpp:L99-L101](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L99-L101) —— `PREFETCH_LOOP` 带 `#pragma HLS PIPELINE II=1`，每拍从 64 位打包输入里按字节取出一个类别 logit 写入 `cls_logits`。循环体里有一次 `input_data[word_idx]` 的 AXI 读（u8-l1 讲过的 64 位打包读取），流水化后多次读请求可重叠发出。

**发射循环**（流水写局部缓冲，并与 4.3 的写指针 `tb` 配合）：

[decode_kernel.cpp:L224-L226](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L224-L226) —— `CLASS_EMIT_LOOP` 带 `#pragma HLS PIPELINE II=1`，从 `cls_logits[m]` 读 logit、条件性地把框写进 `local_boxes_*[tb]`。注意它读的是 `cls_logits`（已分区的片上寄存器），不再碰 AXI，所以这条流水线的瓶颈只在写指针依赖上（见 4.3）。

#### 4.2.4 代码实践

> **实践目标**：通过「思考实验 + 本地综合」理解分区与流水的依存关系。

**操作步骤**：

1. 在 [decode_kernel.cpp:L98](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L98) 注释掉 `#pragma HLS ARRAY_PARTITION variable=cls_logits complete dim=1`。
2. （本地有 Vitis HLS 2023.1 时）按 [hls_config.cfg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/hls_config.cfg) 跑一次综合，打开报告找 `CLASS_CHECK_LOOP` 和 `CLASS_EMIT_LOOP` 的 II 列。

**需要观察的现象 / 预期结果**：

- 预期 `CLASS_CHECK_LOOP` 的并行读退化：`cls_logits` 不再是 3 个独立寄存器，而是一块 RAM，HLS 只能用有限端口读，3 个类别无法在同一拍全部读出，原本靠 `UNROLL` 得到的并行度被存储端口卡住。
- 预期相关流水的 II 变大（>1）。报告 [hls_compile.rpt:L26](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/reports/hls_compile.rpt#L26) 显示该版本里 `CLASS_CHECK_LOOP` 的 II 为 11（注意是另一版本，仅说明「II 会被拖大」这一现象确实出现过）。
- 若无法本地综合，这一步标注为**待本地验证**；但结论（分区支撑并行端口、去掉则 II 退化）可从 HLS 原理直接推出。

#### 4.2.5 小练习与答案

**练习 1**：`ARRAY_PARTITION` 有 `complete`、`cyclic`、`block` 三种模式。本内核为什么对 `cls_logits` 选 `complete`？

**参考答案**：`cls_logits` 只有 3 个元素，且 `CLASS_CHECK_LOOP` 要**同时**读全部 3 个元素做并行比较。`complete` 把它拆成 3 个独立寄存器、提供 3 个并行读端口，正好满足「一拍读 3 个」的需求；元素这么少，完全分区的寄存器成本可以忽略。若数组很大（如 `local_boxes_x[2048]`），`complete` 会爆资源，那时才考虑 `cyclic`/`block`。

**练习 2**：本内核的 `PIPELINE II=1` 为什么只加在 `PREFETCH_LOOP` 和 `CLASS_EMIT_LOOP`，而不加在最外层 `ANCHOR_LOOP`？

**参考答案**：`ANCHOR_LOOP` 内部已经塞进了被 `UNROLL` 的大量并行计算（4.1 的 64 个 `expf`）和条件分支（`if (!found) continue`），体量巨大。若强行在最外层加 `PIPELINE II=1`，HLS 要在一个拍内完成整块距离 softmax，资源与时序都无法承受。源码 L71 确实有一行被注释的 `// #pragma HLS PIPELINE II=1`（`ANCHOR_LOOP`），说明作者试过、最终放弃了对外层流水化。当前策略是：外层串行，内层小循环分别用 `UNROLL` 或 `PIPELINE` 优化。

---

### 4.3 局部写指针 `tb` 与依赖分析

#### 4.3.1 概念说明

这是三块里最微妙、也最体现「HLS 编程思维」的一块。

在软件里，`total_boxes++` 这种「累加/移动写指针」再普通不过。但在 HLS 里，如果一个**跨迭代使用的标量**（scalar，这里 `total_boxes` 在 `ANCHOR_LOOP` 的每次迭代都被读写）在某个**被流水的内层循环**里被「读—改—写」，HLS 会看到一条**跨迭代依赖（loop-carried dependency）**：第 m 次迭代写 `total_boxes`，第 m+1 次又要读它。为了让结果正确，流水线不得不等上一轮写完才能读，于是 **II 被迫从 1 拉大**，吞吐骤降。

解决办法是一个经典 HLS 惯用法：**把跨循环边界的标量复制成一份「局部写指针」**，让流水循环体只跟这个局部的、依赖关系简单清晰的变量打交道，循环结束后**一次性提交**回原变量。源码里的 `tb`（temporary box index）就是它。

> **直觉**：HLS 喜欢依赖关系「短而清晰、尽量局限在单个循环体内」的变量。用一个只在循环内活动的局部副本来承载写指针，HLS 就能放心地把它流水化到 II=1。

#### 4.3.2 核心流程

`CLASS_EMIT_LOOP` 的写指针管理模式分三步：

1. **进入循环前，把全局写指针拷贝到局部变量**：`int tb = total_boxes;`
2. **流水循环内，只用 `tb` 做读—改—写**：`local_boxes_x[tb] = ...; tb++;`——`tb` 是循环内的局部累加，依赖清晰，HLS 能流水化；
3. **循环结束后，一次性把 `tb` 提交回 `total_boxes`**：`total_boxes = tb;`

对照被注释掉的**旧版本**（直接在循环体里 `local_boxes_x[total_boxes] = ...; total_boxes++;`），就能看出这次重构的目的：把写指针从「跨迭代全局标量」改造成「循环内局部变量」，正是为了帮 HLS 分析依赖、保住吞吐。

此外，内核在写回主机前还有两个与写指针相关的小处理：

- **奇数框丢弃**：`if (total_boxes & 1) total_boxes--;`——若最终框数为奇数，丢掉最后一个，凑成偶数。这是一种对齐/打包权衡（偶数利于后续 AXI 突发或两两打包），代价是极端情况下少输出一个框。
- **总数回写**：`out_box_num = total_boxes;`——把最终框数通过标量端口告诉主机（u8-l1 讲过 `out_box_num` 走 `s_axilite`）。

#### 4.3.3 源码精读

**局部写指针的引入与提交**（核心三行 + 注释说明）：

[decode_kernel.cpp:L221-L244](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L221-L244) —— L222 `int tb = total_boxes;` 取局部副本；L224-L241 的 `CLASS_EMIT_LOOP` 用 `tb` 作为写指针并 `tb++`；L244 `total_boxes = tb;` 每个 anchor 结束提交一次。L221 的注释 `// Use a local write index so HLS can reason about dependencies` 直白写出了这么做的理由。

**被注释掉的旧实现**（反面教材，直接读写全局指针）：

[decode_kernel.cpp:L246-L266](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L246-L266) —— 旧版 `CLASS_EMIT_LOOP` 直接 `local_boxes_x[total_boxes] = ...; total_boxes++;`。把它和当前生效版本对比，就是把「跨迭代全局标量」改造成「循环内局部副本」的重构现场。

**奇数框丢弃与总数回写**：

[decode_kernel.cpp:L302-L303](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L302-L303) —— `if (total_boxes & 1) total_boxes--;` 凑偶数。

[decode_kernel.cpp:L335](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L335) —— `out_box_num = total_boxes;` 把框数回写给主机（配合 L305-L333 的六个 `WRITE_BACK_*` 循环把 `local_*` 拷到 `out_*`）。

#### 4.3.4 代码实践

> **实践目标**：亲手对比「局部写指针」与「直接写全局指针」两种写法在 HLS 报告里的 II 差异，体会依赖分析的威力。

**操作步骤**：

1. 备份当前 [decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp)（本实践只读、不改源码仓库；如要实验请在自己的副本上操作）。
2. 在副本里，把生效的 `tb` 版本（L222/L233-L239/L244）注释掉，启用 L246-L266 的旧版（用 `total_boxes` 直接读写）。
3. 用 Vitis HLS 2023.1，按 [hls_config.cfg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/hls_config.cfg) 综合两份代码。
4. 打开两份报告，定位 `CLASS_EMIT_LOOP` 的 **II** 列和 **Latency** 列做对比。

**需要观察的现象 / 预期结果**：

- 旧版（直接读写 `total_boxes`）预期 II > 1，因为 HLS 检测到 `total_boxes` 的跨迭代读—改—写依赖，不敢每拍都发起新迭代；Latency 也相应变大。
- 新版（局部 `tb`）预期更接近 `II=1`，因为依赖被局限在循环内的局部变量上，HLS 能放心流水化。
- 若无法本地综合，此对比**待本地验证**；但「局部副本隔离依赖、利于流水」是 HLS 通用原理，结论可靠。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `int tb = total_boxes;` 和 `total_boxes = tb;` 都删掉，直接在 `CLASS_EMIT_LOOP` 里用 `total_boxes` 作为写指针并 `total_boxes++`，HLS 报告最可能在哪里报警？

**参考答案**：在 `CLASS_EMIT_LOOP` 的 II 上。HLS 会报告该循环存在 **loop-carried dependency**（`total_boxes` 的读依赖于上一迭代的写），从而无法达到 `II=1`，实际 II 会被拉大、吞吐下降。这正是源码注释里「让 HLS 能分析依赖」要规避的问题。

**练习 2**：`tb` 在 `CLASS_EMIT_LOOP` 内部也是一个「每次迭代都可能 +1」的累加器，它难道就没有跨迭代依赖吗？为什么用 `tb` 就可以、用 `total_boxes` 就不行？

**参考答案**：`tb` 确实也有「m 次迭代写、m+1 次读」的依赖，但关键是**它的生命周期完全局限在单个 `CLASS_EMIT_LOOP` 内**（进循环前赋初值、出循环就提交回 `total_boxes`）。HLS 对这种「循环内局部、初值在循环前确定」的简单递推有成熟的流水化手段（可当作一个可预测的累加/计数器来调度）。而 `total_boxes` 是**跨外层 `ANCHOR_LOOP` 迭代**存活的全局标量，生命周期长、依赖链跨循环边界，HLS 更难高效调度。所以问题不在「有没有依赖」，而在「依赖是否清晰、是否局限在单个可流水循环体内」。

---

## 5. 综合实践

把三块知识串起来，做一次「**pragma 审计 + 优化方案设计**」。

**任务**：假设你是这个 HLS 内核的维护者，团队反馈「综合后 LUT 占用 46% 偏高，希望压一压」。请完成：

1. **审计**：通读 [decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp)，列出全部未注释的 HLS pragma，按下表分类填好（本讲已给出大部分答案，请自行补全「预期作用」一栏）：

   | 类型 | pragma | 行号 | 作用对象 | 预期作用 |
   |------|--------|------|----------|----------|
   | INTERFACE | m_axi / s_axilite | L38-L50 | 端口（u8-l1 已讲） | … |
   | ARRAY_PARTITION | complete | L98 | `cls_logits` | … |
   | PIPELINE | II=1 | L101, L226 | `PREFETCH_LOOP`/`CLASS_EMIT_LOOP` | … |
   | UNROLL | full | L118/L132/L147/L169/L176/L186 | 6 个循环 | … |

2. **定位 LUT 大头**：结合 4.1 的分析指出，LUT 主要被哪一组循环的展开吃掉（答案：`DIST_BRANCH_LOOP` × 内层 16-bin softmax 的浮点算子复制）。

3. **提出两个互相权衡的优化方向**，并说明各自代价：
   - 方向 A（省资源）：把 `DIST_BRANCH_LOOP`（L132）从 `UNROLL` 改为 `PIPELINE II=1`。预期 LUT/DSP 显著下降，但四条边串行、Latency 上升。
   - 方向 B（保吞吐、动存储）：检查 `local_boxes_*` 等 `[MAX_BOXES=2048]` 数组是否可以改用更窄位宽或 `cyclic` 分区以省 BRAM——但注意本内核瓶颈是 LUT 不是 BRAM，所以这条收益有限。

4. **诚实记录**：在你没本地跑综合的前提下，哪些结论是「从原理和源码可直接推出」，哪些是「待本地验证」（资源绝对数字、II 变化幅度）。把这两类分清楚写进你的审计报告。

## 6. 本讲小结

- `#pragma HLS UNROLL` 把循环体复制成并行副本，**复制的是计算电路，主要吃 LUT/DSP，几乎不吃 BRAM**；本内核 6 处 `UNROLL` 中，`DIST_BRANCH_LOOP`（4）× 内层 16-bin softmax（`EXPS_LOOP` 等）是浮点算子复制的最大来源。
- 仓库自带 [hls_compile.rpt](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/reports/hls_compile.rpt) 显示顶层 **LUT 46%、DSP 16%、BRAM 仅 2%**，正是「UNROLL 吃计算不吃存储」的真实指纹（报告为另一版本内核，仅作量级参考）。
- `PIPELINE II=1` 与 `ARRAY_PARTITION complete` 必须**配合**：分区给数组开多端口，流水线才能每拍并行读写；`cls_logits` 被完全分区，正是为了支撑 `CLASS_CHECK_LOOP` 的并行读和后续流水发射。
- 局部写指针 `tb`（[L221-L244](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L221-L244)）是经典 HLS 惯用法：把跨循环边界的全局标量复制成循环内局部副本，隔离跨迭代依赖，帮助 HLS 把 `CLASS_EMIT_LOOP` 流水化到更高吞吐；旧版直接读写 `total_boxes` 的代码被注释保留作对照。
- 写回阶段还有两个写指针相关处理：`if (total_boxes & 1) total_boxes--;`（[L302-L303](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L302-L303)）凑偶数对齐、`out_box_num = total_boxes;`（[L335](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L335)）把框数回写主机。
- 读 HLS 报告时要分清**原理可直接推出**的结论与**需本地综合验证**的绝对数字（资源量、II 值），不要把另一版本内核的报告数字误当作当前文件的事实。

## 7. 下一步学习建议

本讲把「内核源码」层面的优化指令讲完了。建议接下来：

- **u8-l4 测试激励与 OpenCL 宿主程序**：看 [test_bench.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp) 如何构造 64 位打包输入逐层喂给本内核做 C/RTL 仿真，以及宿主程序如何用 `cl::Event` profiling 测量 write/kernel/read 三段耗时——这正好能验证本讲「待本地验证」的那些 II/资源结论。
- **u8-l5 HLS 综合配置与平台打包**：看 [hls_config.cfg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/hls_config.cfg) 的 `part`/`clock`/`syn.top` 如何决定本讲引用的资源百分比分母、以及内核如何被打包进 xclbin 上板。
- **若想亲手验证**：在本机装 Vitis/Vivado HLS 2023.1，按本讲 4.1.4 / 4.2.4 / 4.3.4 的步骤分别改一条 pragma 重跑综合，对比报告里的 LUT/BRAM/DSP 与 II 列，把「待本地验证」逐条变成实测数据。
