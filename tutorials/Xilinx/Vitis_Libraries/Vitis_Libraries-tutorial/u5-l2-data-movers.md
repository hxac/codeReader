# 数据搬运器与 DDR↔AIE 桥接

## 1. 本讲目标

在上一篇（u5-l1）里，我们看清了 v++ 三段流水线如何把 C++ 内核组装成可上板的 xclbin，也提到 `system.cfg` 用 `sc=`（AXI Stream 连接）把 PL 内核与 AIE 图接在一起。但我们一直把"DDR 里的数据是怎么进到 AIE 图的、AIE 算完又是怎么写回 DDR 的"当成黑盒。本讲就打开这个黑盒。

学完本讲，读者应该能够：

- 说出 mm2s（memory-map to stream）/ s2mm（stream to memory-map）这两类 PL 内核在 PL↔AIE 系统中的"桥接"角色，以及它们为什么几乎出现在每一个 AIE 示例里。
- 读懂 dsp 库自带的 mm2s/s2mm 源码：DDR↔BRAM↔多路 AXI Stream 的拆分/合并逻辑。
- 掌握 data_mover 库提供的通用搬运器：`pl_data_mover` 的描述符驱动突发读写，以及 `pl_4d_data_mover` 的"带 URAM 缓存的 4D 分块搬运"。
- 认识 URAM 缓存为何能解耦 DDR 访存延迟与 AIE 流式消费，以及 `bi_dm` 双向搬运器如何用一套资源同时跑读/写两个方向。

本讲承接 u3-l3（utils 流式原语）和 u5-l1（v++ 构建流程），是后续 u6（dsp 库 AIE 示例）、u12（性能与存储分区）的前置。

## 2. 前置知识

本讲用到几个概念，先用大白话过一遍，已经熟悉的读者可以跳过。

- **AXI 三种接口**。AMD/Xilinx 芯片里模块之间最常用的接线标准是 AXI（Advanced eXtensible Interface）。本讲涉及它的三种变体：
  - **AXI4（memory-mapped，简称 M_AXI / maxi）**：带地址的总线，像访问内存一样按地址突发读写 DDR/HBM。本文里"MAXI 端口""主端口"都指它。
  - **AXI4-Stream（简称 axis）**：不带地址、只有数据的手拉手流水线接口，一对 valid/ready 握手，谁生产谁消费一目了然。PL 与 AIE 之间的物理连线（PLIO）就跑这种协议。
  - **AXI4-Lite（s_axilite）**：极窄的寄存器配置接口，主机用它给内核"点火"（写启动寄存器、传参地址）。
- **mm2s / s2mm**。这是 AMD 文档里的习惯叫法：mm2s = memory-map **to** stream（从带地址的 DDR 读出来，转成无地址的流）；s2mm = stream **to** memory-map（反过来，把流收下来写回 DDR）。它们本质上就是"协议转换器 + 数据搬运工"。
- **burst（突发）**。一次只读写一个字太浪费总线带宽。AXI 允许"给一个起始地址 + 一个长度，连续传一批"，这一批就叫一次突发（burst）。突发越长，开销摊薄越充分，但单次突发不能跨 4KB 边界（AXI 协议规定）。
- **URAM**。Versal/部分 Alveo 器件里的一种专用大容量片上存储（单块 288Kb 级别），比 BRAM 容量大、密度高，适合当片上缓存。本讲把 URAM 当作"片上一块大 SRAM"来理解即可。
- **PLIO / GMIO**。PLIO 是 PL 侧连到 AIE 阵列的物理流端口（在 vss 例子里固定 128-bit @ 312.5MHz）；GMIO 是另一种由 AIE 直接访问 DDR 的方式。本讲的搬运都走"PL 内核经 PLIO 喂 AIE"的路线。
- **描述符（descriptor）**。让一个搬运器能适应任意访问形状，与其改源码重综合，不如把"从哪儿读、读多大、步长多少"打包成一小段数据喂给它，让它自己解析。这段数据就叫描述符。本讲会看到两种描述符：简单的 9 字版本和复杂的 24 字版本。

> 一句话直觉：AIE 阵列只会"吃流、吐流"，它不认 DDR 地址；而 DDR 又只能按地址访问。mm2s/s2mm 就是夹在中间、把"带地址的大水缸（DDR）"和"无地址的水管（AXI Stream）"互相转换的水泵。

## 3. 本讲源码地图

本讲涉及的关键文件分两组：dsp 库里"专用"的 mm2s/s2mm（更贴近某个算法的搬运），和 data_mover 库里"通用"的搬运器（可被任何库复用）。

| 文件 | 作用 |
| --- | --- |
| [dsp/L1/tests/hw/mm2s/mm2s.h](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h) | mm2s 内核的类型与编译期常量（总线宽度、流数、各维尺寸）。 |
| [dsp/L1/tests/hw/mm2s/mm2s.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp) | mm2s 内核实现：DDR→BRAM→多路 AXI Stream。 |
| [dsp/L1/tests/hw/s2mm/s2mm.h](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.h) | s2mm 内核的类型与常量，结构与 mm2s.h 对称。 |
| [dsp/L1/tests/hw/s2mm/s2mm.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp) | s2mm 内核实现：多路 AXI Stream→BRAM→DDR，是 mm2s 的逆过程。 |
| [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp) | 主机程序，能看到 mm2s/s2mm 的内核名、buffer 对象与启动序列。 |
| [data_mover/L1/include/xf_data_mover/pl_data_mover.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp) | 通用搬运器：9 字描述符 + 突发读写，DDR↔AXI Stream 直连，无片上缓存。 |
| [data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp) | 带 URAM 缓存的 4D 分块搬运器：24 字 tiling 描述符，解耦 DDR 与 stream。 |
| [data_mover/L1/include/xf_data_mover/dm_4d_uram.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/dm_4d_uram.hpp) | 4D 搬运器的内部实现：pattern 解析、URAM 访问、地址生成。 |
| [data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp) | 双向搬运器 bi_dm：一套 URAM 同时承载读、写两个方向。 |
| [data_mover/L1/include/xf_data_mover/load_master_to_stream.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/load_master_to_stream.hpp) | 最朴素的线性搬运 `loadMasterToStream`，便于对比理解。 |

## 4. 核心概念与源码讲解

### 4.1 mm2s/s2mm：DDR↔AXI Stream 的桥接内核

#### 4.1.1 概念说明

任何一个 PL+AIE 混合系统都面临同一个物理事实：**AIE 阵列只会消费和产生 AXI Stream，它不会自己去 DDR 按地址取数**。而主机放进加速卡的数据、要取回的结果，都躺在按地址寻址的 DDR（或 HBM）里。这中间的鸿沟，必须由 PL 侧的搬运内核填上：

- **mm2s（memory-map to stream）**：从 DDR 按地址把一整块数据读进来，拆成（或拼成）一条或多条 AXI Stream，喂给下游的 AIE 图或 PL 内核。
- **s2mm（stream to memory-map）**：把 AIE/PL 吐出来的流收集起来，按地址写回 DDR，供主机读取。

dsp 库的 vss_fft_ifft_1d 示例就是典型的"夹心"结构：

```
DDR --(mm2s)--> AXI Stream --(AIE FFT 图)--> AXI Stream --(s2mm)--> DDR
```

mm2s 在最前面灌数据，s2mm 在最后面收数据，AIE 图夹在中间只管算。这种"前后各一个搬运内核 + 中间计算"的布局，是几乎所有 AIE 示例的标配。

dsp 的 mm2s/s2mm 还多干了一件事：**一路 DDR 数据要被拆成 `NSTREAM` 条并行的流**（对应 AIE 图的 SSR 并行度）。这一点和 utils 的 `streamSplit`（u3-l3）思路一致——但这里是在搬运的"同时"完成拆分，而不是先搬再拆。

#### 4.1.2 核心流程

mm2s 的数据流（以 `NSTREAM_INT > 1` 为例）：

```
DDR(mem[])
   │  m_axi 突发读
   ▼
load_buffer()：把连续地址的数据"交错"填进 buff[NITER][NSTREAM_INT][...]
   │  （第 0 个元素给流 0，第 1 个给流 1，……绕回）
   ▼
transmit()：从 buff 里每次取 samplesPerRead 个，拼成一个宽 beat，写进各路 sig_o[]
   │  axis 输出
   ▼
NSTREAM_INT 条 AXI Stream → AIE 图
```

注意中间有一层片上 BRAM 缓冲 `buff`：先把 DDR 数据按"交错"顺序放好，再按"每路流"顺序读出去。这个重排就是 mm2s 相对"傻瓜搬运"的价值——它在搬运的同时完成了**串行 DDR 布局 ↔ 并行多流布局**的转换。

s2mm 是它的镜像：先把多路流交错收进 `buff`，再把 `buff` 顺序写回 DDR。

#### 4.1.3 源码精读

先看 mm2s 的类型与常量定义，理解"宽度从哪来"。总线宽度固定 128-bit：

[mm2s.h:76-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h#L76-L82) —— 定义 PLIO 总线宽度 `NBITS=128`（即 AIE 侧 PLIO 在 312.5MHz 下的位宽），`TT_DATA` 是一个 128-bit 的 AXI 字（恰能装两个 cint32 样本），`TT_STREAM` 就是承载它的 `hls::stream`。

```cpp
static constexpr unsigned NBITS = 128; // Size of PLIO bus on PL side @ 312.5 MHz
typedef ap_uint<NBITS> TT_DATA;        // Equals two 'cint32' samples
typedef hls::stream<TT_DATA> TT_STREAM;
```

[mm2s.h:81-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h#L81-L82) 还定义了 `samplesPerRead = NBITS / DATAWIDTH`（一次读能塞几个样本）和 `TT_SAMPLE`（单个样本的位宽）。这正是 u3-l3 讲过的"宽 beat 切多路窄通道"——一个 128-bit beat 装几个 `DATAWIDTH` 宽的样本。

实际输出多少条流由 `NSTREAM_INT` 决定，它还能被 `API_IO` 翻倍（AIE1 的双流特性）：

[mm2s.h:90](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h#L90) —— `NSTREAM_INT = (API_IO==1) ? NSTREAM*2 : NSTREAM`。这个 `NSTREAM_INT` 就是 mm2s 对外的 axis 端口数，必须和 AIE 图里 SSR 流数严格对齐。

接下来看顶层 wrapper，这是真正综合成硬件、被主机调用的函数：

[mm2s.cpp:143-166](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L143-L166) —— `mm2s_wrapper` 用四条 `interface` 指令把端口钉死成三种 AXI 协议，再用 `DATAFLOW` 把"装载"和"发射"两段并发起来：

```cpp
void mm2s_wrapper(TT_DATA mem[NITER][memSizeAct], TT_STREAM sig_o[NSTREAM_INT]) {
#pragma HLS interface m_axi port = mem bundle = gmem offset = slave depth = memSizeAct * NITER
#pragma HLS interface axis port = sig_o
#pragma HLS interface s_axilite port = mem bundle = control
#pragma HLS interface s_axilite port = return bundle = control
#pragma HLS DATAFLOW
    TT_SAMPLE buff[NITER][NSTREAM_INT][(samplesPerRead * memSizeAct) / NSTREAM_INT];
#pragma HLS array_partition variable = buff dim = 1
#pragma HLS array_partition variable = buff dim = 2
#pragma HLS bind_storage variable = buff type = RAM_T2P impl = bram
    ...
    if (NSTREAM_INT != 1) {
        load_buffer(mem, buff);
        transmit(buff, sig_o);
    } else {
        mm2s_str1(mem, sig_o);
    }
}
```

读这段要抓住三点：

1. **`mem` 是 `m_axi`（带地址，连 DDR）**，`offset=slave` 表示起始地址由 s_axilite 寄存器传入（主机控制从哪读）；`sig_o` 是 `axis`（无地址，连 AIE）。mm2s 的"协议转换"本质就体现在这两个端口的协议不同。
2. **`buff` 是片上 BRAM 中转**，被 `array_partition` 在前两维完全展开（`dim=1` 对应 NITER、`dim=2` 对应 NSTREAM_INT），让各路流可以并行访问；`RAM_T2P`（双口）+ `dependence intra false` 是为了维持内层循环 II=1（回顾 u3-l2 的 dataflow/pipeline/unroll）。
3. **`DATAFLOW` 让 `load_buffer` 和 `transmit` 流水起来**：一边从 DDR 往 buff 灌，一边从 buff 往外发，两段在时间上重叠，端到端吞吐由较慢的一段决定。

`load_buffer` 是"按交错顺序填 buff"的关键，其内层循环 II=1：

[mm2s.cpp:30-57](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L30-L57) —— 以 `samplesPerRead==2` 为例，每读一个 128-bit 字（含 val1、val0 两个样本），按当前位置 `ss` 把它们分发给相邻的两路流；当 `ss` 走到末尾时回绕到 `dd+1`（下一拍）。这就是"连续 DDR 字 → 交错多流"的重排逻辑：

```cpp
(val1, val0) = mem[n][samp];
if (ss == NSTREAM_INT - 1) {
    buff[n][NSTREAM_INT - 1][dd] = val0;
    buff[n][0][dd + 1] = val1;     // 回绕到下一拍
} else {
    buff[n][ss][dd] = val0;
    buff[n][ss + 1][dd] = val1;
}
```

`transmit` 则反过来把 buff 里同拍的多路样本拼成宽 beat 写出：

[mm2s.cpp:107-138](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L107-L138) —— 内层 `STREAM1` 循环遍历 `NSTREAM_INT` 路流，每路读出 `samplesPerRead` 个样本拼成 `(val1,val0)` 或 `(val3,val2,val1,val0)`，`sig_o[ss].write(...)` 写到对应流。

s2mm 是完全对称的镜像过程。顶层 wrapper 同样是 m_axi + axis + s_axilite + DATAFLOW：

[s2mm.cpp:120-145](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp#L120-L145) —— `s2mm_wrapper` 先 `capture_streams` 把多路流交错收进 `buff`，再 `read_buffer` 把 `buff` 顺序写回 `mem`（DDR）：

```cpp
if (NSTREAM_INT != 1) {
    capture_streams(buff, sig_i);
    read_buffer(mem, buff);
} else {
    s2mm_str1(mem, sig_i);
}
```

[s2mm.cpp:26-58](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp#L26-L58) —— `capture_streams` 从每路 `sig_i[ss].read()` 取一个宽 beat，拆回 val0/val1（或四样本）填进 `buff[ll][ss][addr+...]`，与 mm2s 的 `load_buffer` 一一对应。

最后看主机侧怎么"点名"这两个内核——这是 4.1 的实践依据：

[host.cpp:131-135](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L131-L135) —— 主机用"函数名:{实例名}"的格式取出两个 PL 搬运内核，实例名 `mm2s`/`s2mm` 必须和 `system.cfg` 里 `nk=`（内核实例化）声明的名字一致（回顾 u5-l1）：

```cpp
auto mm2s = xrt::kernel(my_device, xclbin_uuid, "mm2s_wrapper:{mm2s}");
auto s2mm = xrt::kernel(my_device, xclbin_uuid, "s2mm_wrapper:{s2mm}");
```

#### 4.1.4 代码实践

**实践目标**：把 mm2s/s2mm 在 vss 系统里的"名字—连接—主机控制"三件事串起来，理解它们为何是 AIE 示例的标配。

**操作步骤**：

1. 打开 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp)，定位 [L131](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L131) 与 [L134](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L134)，记录两个内核的"函数名 + 实例名"。
2. 打开同目录的 `system.cfg`（u5-l1 讲过它的 `nk/sp/sc` 三件套），找到把 `mm2s` 的 axis 输出连到 AIE 图输入、把 AIE 图输出连到 `s2mm` 的 axis 输入的 `sc=`（stream connection）行。
3. 回到 host.cpp，阅读 [L137-L141](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L137-L141)（建 run）、[L190-L194](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L190-L194)（set_arg 绑 DDR buffer）、[L196-L206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L196-L206)（start/wait）。
4. 画出数据通路：`mm2s_bo(DDR) → mm2s_wrapper → [axis] → AIE 图 → [axis] → s2mm_wrapper → s2mm_bo(DDR)`。

**需要观察的现象**：主机对 mm2s/s2mm 只做了"建 bo → set_arg → start → wait"这一套（与 u4-l2 讲的原生 XRT 调用链完全一致），没有任何"流"层面的操作——流的接驳是在 `system.cfg` 里静态连好的，主机只点火。

**预期结果**：你能用一句话说清——mm2s/s2mm 是主机用普通 XRT kernel API 驱动的 PL 内核，它们对外的 axis 端口在链接期被 `system.cfg` 焊到 AIE 图上，主机对此无感。若手头没有 Versal 板卡与 hw_emu 环境，连接关系以 `system.cfg` 的静态分析为准，运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把 mm2s 的 `NSTREAM_INT` 从 4 改成 8（即 SSR 翻倍），它对外的 axis 端口数和 `system.cfg` 里的流连接会发生什么变化？

> **答案**：axis 输出端口数从 4 变 8（`sig_o[8]`），`system.cfg` 里必须有 8 条 `sc=` 把这 8 路分别接到 AIE 图的 8 个输入端口；同时 AIE 图那侧的 SSR 也要相应改成 8，否则端口数不匹配，v++ -l 链接会报错。这正说明 mm2s 的流数必须与 AIE 图的 SSR 严格对齐。

**练习 2**：mm2s 在 `NSTREAM_INT==1` 时走 `mm2s_str1` 这条简化路径（[mm2s.cpp:90-101](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L90-L101)），不再用 `buff` 中转。为什么单流时可以省掉 buff？

> **答案**：单流时 DDR 的连续布局和流的顺序布局本来就一致（没有"1 拆 N"的交错需求），直接 `sig_o[0].write(mem[n][samp])` 边读边写即可，省掉一层 BRAM 中转反而更省资源、延迟更低。`buff` 的存在价值就是"多路交错重排"，单路时无事可做。

### 4.2 pl_data_mover：描述符驱动的通用搬运器

#### 4.2.1 概念说明

dsp 的 mm2s/s2mm 是为某个算法"量体裁衣"的——访问形状（POINT_SIZE、NITER、NSTREAM）在编译期就钉死了，改形状要改宏、重综合。但很多场景下，访问形状要到运行时才知道（比如把张量的不同切片轮番喂给 AIE），或者一个搬运器要服务多种形状。这时就需要一个**通用搬运器**：访问形状不写死在代码里，而是用一段"描述符"在运行时告诉它。

data_mover 库的 `pl_data_mover.hpp` 提供的就是这种通用搬运器。它的核心思想有两点：

1. **用 `hls::burst_maxi` 做手动突发读写**，而不是依赖 HLS 自动生成的简单访存。手动突发可以精确控制 outstanding（在途请求数）和 burst length，把 DDR/HBM 带宽榨干。
2. **用"4D 描述符"描述任意访问形状**。一个描述符就是一段 9 个 64-bit 字的配置，等价于一个 4 层嵌套循环；搬运器解析它、据此发出一串突发请求。

这一节的 `read4D`/`write4D` 与 4.1 的 mm2s 形成对照：mm2s 把形状编进代码，`read4D` 把形状编进描述符。注意它**没有片上缓存**——DDR 读出来直接进 stream，stream 收下来直接写 DDR，是"直连"搬运。

#### 4.2.2 核心流程

`read4D` 内部是一个 dataflow，两个 stage：

```
descriptor_buffer(DDR 里的描述符表)
   │
   ▼
cmdParser：解析 9 字描述符 → 发出 (offset, burst, end) 三元组流
   │
   ▼
manualBurstRead：按这些三元组向 data(DDR) 发突发读请求 → 把读到的数据包成 axis 写到 w_data 流
   │
   ▼
AXI Stream → 下游 AIE/PL
```

`write4D` 对称：从 `w_data` 流读数据，`cmdParser` 给出写地址，`manualBurstWrite` 发突发写。

**9 字描述符的含义**（这是理解本节的关键）。一个描述符由 9 个 64-bit 字 `cfg[0..8]` 组成：

| 字段 | 含义 |
| --- | --- |
| `offset = cfg[0]` | 起始地址（字地址） |
| `i1, d1 = cfg[1], cfg[2]` | 第 1（最内）维：步长、长度 |
| `i2, d2 = cfg[3], cfg[4]` | 第 2 维：步长、长度 |
| `i3, d3 = cfg[5], cfg[6]` | 第 3 维：步长、长度 |
| `i4, d4 = cfg[7], cfg[8]` | 第 4（最外）维：步长、长度 |

它等价于四层嵌套循环：

```
for w in [0, d4):  s4 = offset + w*i4
  for z in [0, d3):  s3 = s4 + z*i3
    for y in [0, d2):  s2 = s3 + y*i2
      for x in [0, d1) step x_inc:  地址 = s2 + x*i1   ← 内层连续时合成一次长突发
```

当最内维步长 `i1==1`（地址连续）时，搬运器把最多 `BURSTLEN` 个连续地址合并成一次突发；否则每步单独一个 1-beat 突发。通过调整四个 `i/d`，同一个搬运器就能描述连续块、跨步行、二维 tile、四维子立方体等任意访问模式。

> 关键直觉：描述符把"访存形状"从 RTL 里抽出来了。改形状 = 改 DDR 里的描述符表，不用重综合。

#### 4.2.3 源码精读

顶层 `read4D` 极简，只是一个 dataflow 外壳，把 `cmdParser` 和 `manualBurstRead` 串起来：

[pl_data_mover.hpp:320-337](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L320-L337) —— `read4D` 用三条内部流（`r_offset`/`r_burst`/`e_r`，深度都绑成 `OUTSTANDING`）把"解析"和"搬运"两段解耦：

```cpp
template <int WDATA, int LATENCY, int OUTSTANDING, int BURSTLEN>
void read4D(hls::burst_maxi<ap_uint<64> >& descriptor_buffer,
            hls::burst_maxi<ap_uint<WDATA> >& data,
            hls::stream<ap_axiu<WDATA, 0, 0, 0> >& w_data) {
#pragma HLS dataflow
    ...
    details::cmdParser<BURSTLEN>(descriptor_buffer, r_offset, r_burst, e_r);
    details::manualBurstRead<WDATA, LATENCY, OUTSTANDING, BURSTLEN>(data, r_offset, r_burst, e_r, w_data);
}
```

四个模板参数 `WDATA/LATENCY/OUTSTANDING/BURSTLEN` 控制位宽与时序：`WDATA` 是数据端口位宽，`LATENCY`/`OUTSTANDING`/`BURSTLEN` 必须与 maxi 端口 pragma 设置一致（见文件头注释 L37-54 的约束：OUTSTANDING < 512、BURSTLEN ≤ 64、为避免 4KB 跨界拆包导致的死锁，HLS 侧在途请求要小于等于 `OUTSTANDING/2`）。

`cmdParser` 就是"把 9 字描述符翻译成一串突发请求"的地方：

[pl_data_mover.hpp:228-297](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L228-L297) —— 先读描述符表的第一个字拿到描述符个数 `cmd_nums`，然后循环处理每个描述符；每个描述符读 9 个字，展开成上面的四层嵌套循环，逐个地址计算并发出 `(offset, burst)`：

```cpp
for (int cmd_idx = 0; cmd_idx < cmd_nums; cmd_idx++) {
    descriptor.read_request(cmd_buf_ptr, 9);
    for (int i = 0; i < 9; i++) cfg[i] = descriptor.read();
    cmd_buf_ptr += 9;
    ap_uint<64>& offset = cfg[0];
    ap_uint<64>& i1 = cfg[1]; ap_uint<64>& d1 = cfg[2];
    ...
    for (ap_uint<64> w = 0; w < d4; w++) {
        ap_uint<64> s4 = offset + w * i4;
        for (ap_uint<64> z = 0; z < d3; z++) { ... // s3, s2
            for (ap_uint<64> x = 0; x < d1; x += x_inc) {
#pragma HLS pipeline II = 1
                ap_uint<64> s1 = s2 + x * i1;
                ap_uint<10> burst = (i1 == 1) ? min(BURSTLEN, d1-x) : 1;
                r_offset.write(s1); r_burst.write(burst); e_r.write(false);
            }
        }
    }
}
e_r.write(true);  // 收尾标志
```

注意两个细节：其一，最内维 `i1==1` 时 `x_inc = BURSTLEN`（一次跳一整个突发），否则 `x_inc = 1`——这是"连续段合并成长突发、跨行段拆成单 beat"的策略；其二，结束时写一个 `e_r=true` 作为终止标志，下游 `manualBurstRead` 据此知道请求已发完。

`manualBurstRead` 是"按请求做真正的突发读"。它用 `check`（一个 `LATENCY` 位的移位寄存器）来记每个请求的"成熟时间"，避免读回来对不上号：

[pl_data_mover.hpp:177-215](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L177-L215) —— 主循环里：只要还有 outstanding 配额且没到末尾，就 `data.read_request(offset, burst)` 发读请求、`check[0]=1` 记一笔；同时按延迟拍数 `check_l` 判断某个请求"成熟"了就从 `data.read()` 取数包成 axis 写出。

```cpp
if (req_left != 0 && !last) {            // 还有配额：发新请求
    data.read_request(tmp_offset, tmp_burst);
    check[0] = 1; req_left--;
    burst_record[rec_tail++] = tmp_burst;
}
if (req_ready != 0 || read_left != 0) {  // 有成熟请求：取数
    tmp_data.data = data.read();
    tmp_data.keep = -1; tmp_data.last = 0;
    w_data.write(tmp_data);
}
if (check_l) { req_ready++; }            // 某请求成熟
```

[pl_data_mover.hpp:80](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L80) 的 `req_left = OUTSTANDING / 2` 正是文件头注释强调的"对半留余量防死锁"。`manualBurstWrite`（[L71-143](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L71-L143)）结构对称，多一步 `data.write_response()` 等 AXI 写响应。

#### 4.2.4 代码实践

**实践目标**：理解 `pl_data_mover` 的模板参数与描述符如何共同控制"位宽 + 访问形状"。

**操作步骤**：

1. 打开 [pl_data_mover.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp)，阅读 `read4D` 的模板声明 [L320-L326](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L320-L326) 与文件头设计注释 [L37-L54](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L37-L54)。
2. 在 [cmdParser L260-296](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L260-L296) 里，把 `cfg[0..8]` 九个字与"offset + 4 组 (步长,长度)"对应起来，确认四层嵌套循环的地址计算。
3. 构造一个假想的描述符：从地址 0 开始，连续读 64 个字（一个长突发），写成 9 个字。即 `offset=0, i1=1, d1=64, i2=0,d2=1, i3=0,d3=1, i4=0,d4=1`（外三维退化，只剩最内维）。

**需要观察的现象**：当你让 `i1=1`，`cmdParser` 走 `x_inc=BURSTLEN` 的分支，64 个地址会被合并成尽可能少的、长度上限 `BURSTLEN` 的突发；如果让 `i1=2`（每隔一个字读一个），则每个地址都是 `burst=1` 的单 beat 突发，总请求数暴涨、带宽利用率骤降。

**预期结果**：你能写出一句话——`WDATA` 决定位宽，`OUTSTANDING`/`BURSTLEN` 决定带宽利用率，9 字描述符的四组 `(步长,长度)` 决定访问形状；连续访问合并长突发、跨行访问拆成单 beat。具体跑 csim 验证描述符解析可参考 data_mover/L2/tests 下用例，运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：文件头注释强调 `req_left = OUTSTANDING / 2`（[L80](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L80)）"对半留余量防死锁"。结合 [L43-L54](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_data_mover.hpp#L43-L54) 的注释，解释为什么不能把 `OUTSTANDING` 用满。

> **答案**：AXI burst_maxi 端口会把跨 4KB 边界的请求自动拆成子请求，实际在途子请求数可能达到 HLS 侧发出请求数的 2 倍。如果 HLS 侧发满 `OUTSTANDING` 个请求，硬件侧子请求可能超过端口的 outstanding 上限，导致总线反压；而 `manualBurstRead` 又在等读回来的数据才能继续，双方互等就死锁了。留一半余量（`/2`）保证硬件侧子请求不超限。

**练习 2**：`read4D` 与 4.1 的 `mm2s_wrapper` 都把 DDR 数据变成 AXI Stream，它们在"访问形状"的处理上有什么本质区别？

> **答案**：`mm2s_wrapper` 的访问形状（POINT_SIZE、NITER、NSTREAM、交错方式）在**编译期**由宏钉死，改形状要重综合；`read4D` 的访问形状由运行时读入的**描述符**决定，同一个综合好的内核靠换描述符就能搬不同形状。前者性能极致但死板，后者灵活但单次开销略高。

### 4.3 带有 URAM 缓存的 4D 搬运器（pl_4d_data_mover）

#### 4.3.1 概念说明

4.2 的 `pl_data_mover` 是"直连"搬运——DDR 读出来马上进流，流收下来马上写 DDR。这有个隐患：**DDR 访存延迟高且不均匀**，而 AIE 那侧的消费又要求流持续不断（一旦流断流，AIE 的计算单元就空转）。如果某次 DDR 突发卡了一下，延迟会直接传导到 AIE。

解决办法是经典的**加缓存**：在 DDR 与 stream 之间塞一块片上 URAM 当缓冲。DDR 那头按自己的节奏 prefetch（预取）一大块进 URAM，AIE/stream 这头再从 URAM 里按流的节奏平稳读出。两头的速率被 URAM 解耦，DDR 的抖动被吸收。

data_mover 库的 `pl_4d_data_mover.hpp` 提供的就是这种"带 URAM 缓存的 4D 搬运器"。相比 4.2，它有两点升级：

1. **片上 URAM 缓存**：`CACHE_DEPTH` 模板参数定义缓存深度；DDR 侧（AXIM）和流侧（AXIS）各自独立地访问同一块 URAM，由独立的控制器驱动。
2. **更丰富的 24 字 tiling 描述符**：除了"从哪读、读多大"，还能描述 tile（瓦片）大小、stride（步长）、wrap（回绕）等，专门为"把一个大张量切成小块轮番喂给 AIE"这种深度学习/图像常见的访问模式设计。

> 关键直觉：`pl_data_mover` 是"水管"（直连），`pl_4d_data_mover` 是"带蓄水池的水管"（有 URAM 缓存）。蓄水池让两头的水流可以各按各的节拍。

#### 4.3.2 核心流程

`ddr_to_stream`（读方向顶层）拆成两步：先 `load_cfg` 把 DDR 里的配置（pattern + program memory）读进片上 RAM，再 `read_4D` 做真正的搬运：

```
cfg_port(DDR 里的配置)
   │  load_cfg：拆出 pattern_m2s / pattern_s2s / pm_m2s / pm_s2s 四块片上 RAM
   ▼
read_4D (dataflow)：
  ┌─ dm_ctrl_axim  ──► axim_to_uram ──►  往 URAM 写  (DDR → URAM)
  └─ dm_ctrl_axis  ──► uram_to_axis ──►  从 URAM 读  (URAM → axis)
                                            │
                                            ▼
                                        AXI Stream → AIE
```

注意 dataflow 里有**两个独立的控制器**：`dm_ctrl_axim` 驱动 DDR↔URAM 这一段（管 prefetch），`dm_ctrl_axis` 驱动 URAM↔stream 这一段（管平稳输出）。它们之间用 `i_sync_strm`/`o_sync_strm` 这对握手流同步（比如等某块 prefetch 完了才允许 AXIS 段去读它）。这正是"两头解耦"的实现机制。

24 字 tiling 描述符的含义（每个 pattern 24 个 32-bit 字，由 `parse_pattern` 解析）：

| 字段组（各 4 字） | 含义 |
| --- | --- |
| `buff_dim[4]` | URAM 缓存里 buffer 的四维尺寸 |
| `offset[4]` | 在 4D 立方体里的起始偏移 |
| `tiling[4]` | 每一维的 tile 大小（一次搬多少） |
| `dim_id[4]` | 每一维的遍历顺序标识 |
| `stride[4]` | 每一维的步长 |
| `wrap[4]` | 每一维的回绕长度（实现循环边界回绕） |

这比 4.2 的 9 字描述符表达力强得多：它能描述"把一个 `[H,W,C,N]` 张量，按 `[th,tw,c,n]` 的 tile 大小、以指定步长滑窗、必要时在边界回绕"这种复杂的分块搬运——这正是 AIE 上做卷积/矩阵分块时最需要的访问模式。

#### 4.3.3 源码精读

顶层 `ddr_to_stream` 很薄：声明四块片上 RAM（pattern 和 program memory 各两份，分别给 AXIM 段和 AXIS 段），`load_cfg` 灌配置，`read_4D` 干活：

[pl_4d_data_mover.hpp:222-241](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp#L222-L241) —— 四块片上 RAM 都绑成 BRAM（`RAM_2P`），然后调内部 `read_4D`：

```cpp
template <int WDATA, int CACHE_DEPTH, int LATENCY, int BURST_LEN, int OUTSTANDING>
void ddr_to_stream(hls::burst_maxi<ap_uint<WDATA>> cfg_port,
                   hls::burst_maxi<ap_uint<WDATA>> data_port,
                   hls::stream<ap_axiu<8+8,0,0,0>>& i_sync_strm,
                   hls::stream<ap_axiu<8+8,0,0,0>>& o_sync_strm,
                   hls::stream<ap_axiu<WDATA,0,0,0>>& o_axis_strm) {
    ap_uint<32> pattern_m2s[1024]; ap_uint<32> pattern_s2s[1024];
    ap_uint<32> pm_m2s[1024];      ap_uint<32> pm_s2s[1024];
#pragma HLS bind_storage variable = pattern_m2s type = RAM_2P impl = bram
    ...
    details::load_cfg<WDATA>(cfg_port, pattern_m2s, pattern_s2s, pm_m2s, pm_s2s);
    details::read_4D<WDATA, CACHE_DEPTH, LATENCY, BURST_LEN, OUTSTANDING>(
        data_port, pattern_m2s, pattern_s2s, pm_m2s, pm_s2s, i_sync_strm, o_sync_strm, o_axis_strm);
}
```

五个模板参数：`WDATA`（位宽）、`CACHE_DEPTH`（URAM 缓存深度）、`LATENCY`/`BURST_LEN`/`OUTSTANDING`（maxi 时序，须与端口 pragma 一致）。注意多了 `i_sync_strm`/`o_sync_strm` 这对握手流——它用于多个搬运器之间的协同（比如读搬运器和写搬运器交替工作）。

`read_4D` 内部的 dataflow 是本节的灵魂——**两段、两控制器、共享 URAM**：

[pl_4d_data_mover.hpp:123-153](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp#L123-L153) —— AXIM 段（DDR→URAM）由 `dm_ctrl_axim` + `axim_to_uram` 构成，AXIS 段（URAM→stream）由 `dm_ctrl_axis` + `uram_to_axis` 构成，两段经 `uram_waddr_strm`/`uram_wdata_strm` 共享同一片 URAM：

```cpp
void read_4D(...) {
#pragma HLS dataflow
    ...
    dm_ctrl_axim(pm_0, ctrl0_ack_strm, ctrl_sync_s2m_strm, i_sync_strm, ctrl0_pattern_strm, ctrl_sync_m2s_strm, o_sync_strm);
    axim_to_uram<...>(maxi_port, pattern_0, ctrl0_pattern_strm, ctrl0_ack_strm, uram_waddr_strm, uram_wdata_strm);
    ...
    dm_ctrl_axis(pm_1, ctrl1_ack_strm, ctrl_sync_m2s_strm, ctrl1_pattern_strm, ctrl_sync_s2m_strm);
    uram_to_axis<WDATA, CACHE_DEPTH>(uram_waddr_strm, uram_wdata_strm, pattern_1, ctrl1_pattern_strm, ctrl1_ack_strm, o_axis_strm);
}
```

`dm_ctrl_axim`/`dm_ctrl_axis` 是"程序存储器（pm）驱动的小 ALU/控制器"——它读 pm 里的指令序列，按节奏向 `axim_to_uram`/`uram_to_axis` 发 pattern_id（启用哪个 pattern），并通过 `ctrl_sync_*` 流与对方段握手。这就是"用片上程序存储器控制访存节奏"，比纯硬件状态机灵活得多。

URAM 缓存的访问原语在 `dm_4d_uram.hpp`。`axim_to_uram` 把 DDR 数据搬进 URAM：

[dm_4d_uram.hpp:850-883](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/dm_4d_uram.hpp#L850-L883) —— `axim_to_uram` 内部又是 dataflow：`parse_pattern` 解析 24 字 pattern → `axim_read_addr_gen` 生成 DDR 读地址与突发长度 → `axim_burst_read` 真正突发读 → 输出 `waddr_strm`/`wdata_strm`（URAM 的写地址与写数据）：

```cpp
void axim_to_uram(...) {
#pragma HLS dataflow
    parse_pattern(pattern_buf, pattern_id, tile_4d_strm, bias_4d_strm, dim_4d_strm, parse_e_strm);
    axim_read_addr_gen<BURST_LEN>(..., raddr_strm, rlen_strm, gen_e_strm);
    axim_burst_read<W,D,LATENCY,OUTSTANDING>(maxi_port, raddr_strm, rlen_strm, ...,
                                             waddr_strm, wdata_strm);
    axim_br_loopback<LATENCY, OUTSTANDING>(br_info_strm, br_fb_info_strm);
}
```

`parse_pattern` 负责把 24 字 pattern 拆成六组四元组，是理解 24 字描述符的入口：

[dm_4d_uram.hpp:50-96](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/dm_4d_uram.hpp#L50-L96) —— 每个 pattern 读 24 个字，前 4 个是 `buff_dim`、接着依次是 `offset`、`tiling`、`dim_id`、`stride`、`wrap`（各 4 个字），全部用 `complete` 分区以便并行访问：

```cpp
ap_uint<32> buff_dim[4]; ap_uint<32> offset[4]; ap_uint<32> tiling[4];
ap_uint<32> dim_id[4];   ap_uint<32> stride[4]; ap_uint<32> wrap[4];
#pragma HLS array_partition variable = buff_dim complete
...
LOAD_PATTERN_CFG_LOOP:
for (ap_uint<5> i = 0; i < 24; i++) {
#pragma HLS pipeline II = 1
    ap_uint<32> load_cfg = pattern_buf[cmd_s++];
    if (i < 4)       buff_dim[i(1,0)] = load_cfg;
    else if (i < 8)  offset[i(1,0)]   = load_cfg;
    else if (i < 12) tiling[i(1,0)]   = load_cfg;
    else if (i < 16) dim_id[i(1,0)]   = load_cfg;
    else if (i < 20) stride[i(1,0)]   = load_cfg;
    else             wrap[i(1,0)]     = load_cfg;
}
```

`uram_to_axis`（[dm_4d_uram.hpp:798-831](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/dm_4d_uram.hpp#L798-L831)）是镜像：`parse_pattern` → `read_uram_addr_gen`（生成 URAM 读地址）→ `uram_access`（共享的 URAM 读写仲裁）→ `write_to_axis`（包成 axis 输出）。写方向 `stream_to_ddr`/`write_4D`（[pl_4d_data_mover.hpp:173-203](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp#L173-L203)、[258-277](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp#L258-L277)）结构对称，只是把 `axim_to_uram`/`uram_to_axis` 换成 `uram_to_axim`/`axis_to_uram`。

#### 4.3.4 代码实践

**实践目标**：用模板参数和 24 字 pattern 解释 `pl_4d_data_mover` 如何控制"位宽、缓存深度、访存节奏"。

**操作步骤**：

1. 打开 [pl_4d_data_mover.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp)，对照 `ddr_to_stream` 的声明 [L222-L227](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/pl_4d_data_mover.hpp#L222-L227)，列出五个模板参数各自的职责。
2. 打开 [dm_4d_uram.hpp 的 parse_pattern](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/dm_4d_uram.hpp#L50-L96)，把 24 个字与 `buff_dim/offset/tiling/dim_id/stride/wrap` 六组对应。
3. 浏览 [data_mover/L2/tests/mm2s_4d_and_s2mm_4d/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L2/tests/mm2s_4d_and_s2mm_4d/README.md)（若存在），看 L2 是如何把 `ddr_to_stream`/`stream_to_ddr` 包成 mm2s_4d/s2mm_4d 内核并短接测试的。

**需要观察的现象**：`read_4D` 内部有两段 dataflow（AXIM 段 + AXIS 段），它们各自独立地消费自己的 pattern 与 program memory，只通过 URAM 和几个同步流耦合。这说明 URAM 是真正的"解耦点"——DDR prefetch 和 stream 输出可以各跑各的节拍。

**预期结果**：你能说清——`WDATA` 控位宽、`CACHE_DEPTH` 控 URAM 缓存大小（决定能吸收多大的 DDR 抖动）、`LATENCY/BURST_LEN/OUTSTANDING` 控 DDR 突发效率；24 字 pattern 的 `tiling`/`stride`/`wrap` 控访问形状。L2 的具体打包与上板运行待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`pl_4d_data_mover` 的 `read_4D` 为什么需要 `dm_ctrl_axim` 和 `dm_ctrl_axis` 两个独立控制器，而不是像 `pl_data_mover` 那样只有一个 `cmdParser`？

> **答案**：因为有 URAM 缓存后，DDR↔URAM 和 URAM↔stream 是两个节奏不同的过程（DDR 延迟大、要长突发 prefetch；stream 要平稳持续输出）。两个独立控制器各管一段，通过同步流握手，才能让 URAM 真正起到"解耦两头速率"的作用。`pl_data_mover` 是直连无缓存，一头直通另一头，一个解析器够了。

**练习 2**：若把 `CACHE_DEPTH` 设得很小（比如只能放一个 tile），URAM 缓存还能起到"吸收 DDR 抖动"的作用吗？

> **答案**：基本不能。缓存太小意味着 DDR prefetch 一小段就被迫停下、等 AXIS 段读走才能继续 prefetch，两头重新强耦合，DDR 任何抖动都会立刻传导到 stream。`CACHE_DEPTH` 必须大到能容纳至少一个完整 tile（最好能预取下一块），才能让两头真正解耦。这就是 `buff_dim` 要和 `tiling` 协调配置的原因。

### 4.4 URAM 缓存与 bi_dm 双向搬运

#### 4.4.1 概念说明

到目前为止我们讲的搬运器都是"单向"的：`read4D`/`ddr_to_stream` 只读不写，`write4D`/`stream_to_ddr` 只写不读。但很多 AIE 应用是"边读边写"的：喂进去一块输入、吐出来一块输出，两个方向同时发生。如果用两个单向搬运器，就要实例化两套 URAM 缓存、两组控制器，资源翻倍。

`bi_pl_4d_data_mover.hpp` 的 `bi_data_mover`（bi = bidirectional）解决这个问题：**一套 URAM 缓存 + 四个控制器，同时承载读、写两个方向**。它的典型用法是把 AIE 图包成一个"环"：输入流进 bi_dm → 缓存 → 喂给 AIE；AIE 吐出 → 缓存 → 写回 DDR，全在一个内核里完成。

bi_dm 的复杂度来自"双向共享资源时的协同"：四个方向（axis→uram、uram→axis、axim→uram、uram→axim）要访问同一片 URAM，谁先谁后、何时同步，靠一组握手流（`ctrl_sync_s2s/s2m/m2m/m2s`）协调。为了和软件仿真对齐，它在 csim 下用 `std::thread` 把四个控制器分别跑成线程（`RUN_IN_THREAD`），综合时才退化成纯硬件 dataflow。

#### 4.4.2 核心流程

`bi_data_mover` 顶层同样先 `bi_load_cfg` 把配置读进 8 块片上 RAM（4 个 pattern + 4 个 program memory，分别给四个方向的控制器），再调内部 `bi_dm`：

```
cfg_port(DDR 里的配置)
   │  bi_load_cfg：拆出 4×pattern + 4×pm（共 8 块片上 RAM）
   ▼
bi_dm (dataflow)：
  四个控制器 + 四个方向原语，共享 bi_uram_access：
   axis_to_uram  (流 → URAM)   ┐
   uram_to_axim  (URAM → DDR)  ├─ 四路经 bi_uram_access 仲裁访问同一片 URAM
   uram_to_axis  (URAM → 流)   │
   axim_to_uram  (DDR → URAM)  ┘
  ctrl_sync_* 流在四者之间做节奏同步
```

四个方向的含义：

- `axis_to_uram`：从输入流收数据进 URAM（AIE 回来的或外部来的）。
- `uram_to_axim`：把 URAM 数据写回 DDR（输出落盘）。
- `uram_to_axis`：把 URAM 数据发到输出流（喂给 AIE）。
- `axim_to_uram`：从 DDR 预取数据进 URAM（输入搬入）。

`bi_uram_access` 是四向共享的 URAM 仲裁器，把四个方向的地址/数据流复用到同一片物理 URAM。

#### 4.4.3 源码精读

顶层 `bi_data_mover` 把八个配置数组声明为 BRAM，`bi_load_cfg` 一次灌入，再交给内部 `bi_dm`：

[bi_pl_4d_data_mover.hpp:360-388](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp#L360-L388) —— 顶层声明 `pattern_0..3` 与 `pm_0..3` 共八块片上 RAM，分别对应 `axis_to_uram`/`uram_to_axim`/`uram_to_axis`/`axim_to_uram` 四个方向：

```cpp
template <int WDATA, int CACHE_DEPTH, int LATENCY, int BURST_LEN, int OUTSTANDING>
void bi_data_mover(hls::burst_maxi<ap_uint<WDATA>> cfg_port,
                   hls::burst_maxi<ap_uint<WDATA>> i_maxi_port,
                   hls::burst_maxi<ap_uint<WDATA>> o_maxi_port,
                   hls::stream<ap_axiu<WDATA,0,0,0>>& i_axis_strm,
                   hls::stream<ap_axiu<WDATA,0,0,0>>& o_axis_strm) {
    ap_uint<32> pattern_0[1024]; ... ap_uint<32> pm_3[1024];   // 8 块
#pragma HLS bind_storage variable = pattern_0 type = RAM_2P impl = bram
    ...
    bi_details::bi_load_cfg<WDATA>(cfg_port, pattern_0, ..., pm_3);
    bi_details::bi_dm<WDATA, CACHE_DEPTH, LATENCY, BURST_LEN, OUTSTANDING>(
        i_axis_strm, o_axis_strm, i_maxi_port, o_maxi_port, pattern_0, ..., pm_3);
}
```

注意端口：两个 maxi（`i_maxi_port` 读方向、`o_maxi_port` 写方向）+ 两个 axis（`i_axis_strm` 流入、`o_axis_strm` 流出）。一个内核同时具备读 DDR、写 DDR、流入、流出四种端口，这就是"双向"的含义。

`bi_dm` 在综合分支（`__SYNTHESIS__`）下是 5 段 dataflow，中间用 `bi_uram_access` 共享 URAM：

[bi_pl_4d_data_mover.hpp:324-342](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp#L324-L342) —— 四个 `bi_dm_ctrl` + 四个方向原语 + 一个 `bi_uram_access` 仲裁器：

```cpp
bi_details::bi_dm_ctrl(pm_0, c0_ack_strm, ctrl_sync_m2s_strm, ctrl_sync_s2s_strm, c0_pattern_strm);
axis_to_uram<WDATA, CACHE_DEPTH>(i_axis_strm, pattern_0, c0_pattern_strm, s2u_waddr_strm, s2u_wdata_strm, c0_ack_strm);

bi_details::bi_dm_ctrl(pm_1, c1_ack_strm, ctrl_sync_m2m_strm, ctrl_sync_m2s_strm, c1_pattern_strm);
uram_to_axim<...>(o_maxi_port, pattern_1, c1_pattern_strm, u2m_raddr_strm, u2m_rdata_strm, c1_ack_strm);

bi_details::bi_dm_ctrl(pm_2, c2_ack_strm, ctrl_sync_s2s_strm, ctrl_sync_s2m_strm, c2_pattern_strm);
uram_to_axis<WDATA, CACHE_DEPTH>(o_axis_strm, pattern_2, c2_pattern_strm, u2s_raddr_strm, u2s_rdata_strm, c2_ack_strm);

bi_details::bi_dm_ctrl(pm_3, c3_ack_strm, ctrl_sync_s2m_strm, ctrl_sync_m2m_strm, c3_pattern_strm);
axim_to_uram<...>(i_maxi_port, pattern_3, c3_pattern_strm, m2u_waddr_strm, m2u_wdata_strm, c3_ack_strm);

bi_uram_access<WDATA, CACHE_DEPTH>(s2u_waddr_strm, s2u_wdata_strm, m2u_waddr_strm, m2u_wdata_strm,
                                    u2s_raddr_strm, u2s_rdata_strm, u2m_raddr_strm, u2m_rdata_strm);
```

四个控制器之间通过 `ctrl_sync_s2s/s2m/m2m/m2s` 四条同步流彼此握手（s2s = stream-to-stream，s2m = stream-to-maxim，m2m = maxim-to-maxim，m2s = maxim-to-stream），协调四个方向对 URAM 的分时复用。

最后看 bi_dm 的 csim 分支——它揭示了这些控制器在仿真时如何被验证：

[bi_pl_4d_data_mover.hpp:266-306](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp#L266-L306) —— 在非综合模式下，用 `RUN_IN_THREAD` 把每个 `bi_alu`（控制器）和 `bi_sync_dm`（同步器）各起一个 `std::thread` 并发跑，最后 `JOIN_THREADS` 收口。这是因为 csim 是纯软件、没有真硬件并发，用线程模拟四控制器的并行行为，使仿真结果与综合后的 dataflow 行为一致：

```cpp
#ifndef __SYNTHESIS__
    std::cout << "## Test in CSIM_THREADS ##" << std::endl;
    ...
    RUN_IN_THREAD(bi_details::bi_alu, STD_REF(pm_0), STD_REF(c0_ack_strm), ...);
    RUN_IN_THREAD(bi_details::bi_sync_dm, STD_REF(c0_sync_i_intra_strm), STD_REF(ctrl_sync_m2s_strm), ...);
    ...
    JOIN_THREADS();
#else
    ...  // 综合分支：纯硬件 dataflow
#endif
```

这套 `RUN_IN_THREAD`/`JOIN_THREADS` 宏（[L93-L105](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp#L93-L105)）在综合时被替换成普通函数调用，是 data_mover 库"同一份代码兼顾 csim 与综合"的常用手法。

#### 4.4.4 代码实践

**实践目标**：理解 bi_dm 如何用一套 URAM 承载双向数据流，以及它与"两个单向搬运器"相比的资源取舍。

**操作步骤**：

1. 打开 [bi_pl_4d_data_mover.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp)，看 `bi_data_mover` 的端口 [L360-L365](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp#L360-L365)：两个 maxi + 两个 axis。
2. 浏览 [data_mover/L2/tests/bi_dm_s2mm_mm2s_s2s/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L2/tests/bi_dm_s2mm_mm2s_s2s/README.md)。该测试把 mm2s 与 s2mm 短接（不插 AIE 图），用三个 CASE 验证 bi_dm 的三种场景：`TEST_AXIS_TO_DDR`（流→DDR）、`TEST_DDR_TO_AXIS`（DDR→流）、`TEST_AXIS_CACHE_AXIS`（流→缓存→流）。
3. 对照这三种 CASE，在 `bi_dm` 的四向 dataflow [L324-L342](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp#L324-L342) 里指出每种 CASE 分别激活了哪几个方向。

**需要观察的现象**：bi_dm 的四个方向并非每次都要全开——`TEST_AXIS_CACHE_AXIS` 只用流进流出、不碰 DDR（`i_maxi_port`/`o_maxi_port` 空闲），相当于把 URAM 当成"AIE 之间的中间缓存"。这说明 bi_dm 是个可按需启用方向的"瑞士军刀"。

**预期结果**：你能说清 bi_dm 的资源模型——一套 URAM + 四控制器，相比"两个单向 pl_4d_data_mover"省了一半 URAM，代价是四个方向要经同步流分时复用 URAM、控制更复杂。短接测试的运行（`make run TARGET=hw_emu CASE=...`）待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：bi_dm 在 csim 下用 `std::thread` 跑四个控制器（[L266-L306](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L1/include/xf_data_mover/bi_pl_4d_data_mover.hpp#L266-L306)），综合时却退化成 dataflow。为什么要做这种"双面"实现？

> **答案**：dataflow 在综合后是真硬件并发（每段是独立流水），但 csim 是纯软件单线程执行，无法真正并发；若直接顺序调用四个控制器，握手流会立刻死锁（A 等 B、B 等 A）。用线程在 csim 下模拟并发，能让仿真行为贴近硬件真并发，从而在综合前验证逻辑正确性。`__SYNTHESIS__` 宏切换两套实现，是 HLS 库兼顾"可仿真"与"可综合"的标准做法。

**练习 2**：什么场景下应该选 bi_dm 而不是"一个 mm2s + 一个 s2mm"（两个单向搬运器）？

> **答案**：当读、写两个方向共享同一批数据（比如 AIE 把输入变换后原地写回、或输入输出 tile 有重叠）且对片上存储紧张时，bi_dm 共享 URAM 更省资源；此外 bi_dm 的四向同步能精细协调"先 prefetch 哪块、何时让 AIE 读、何时收结果"。反之，若读、写完全独立、数据无重叠，或想各自调优时序，用两个独立的单向搬运器更简单清晰。

## 5. 综合实践

**任务**：为 vss_fft_ifft_1d 这个 AIE 系统画一张完整的"数据搬运总图"，并据此判断它用的是哪一代搬运器、为什么没用到 data_mover 库的通用搬运器。

**步骤**：

1. 打开 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp)，把 mm2s/s2mm 的内核名、bo 创建、start/wait 序列标注出来（参考 [L131-L135](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L131-L135)、[L147-L160](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L147-L160)、[L196-L206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L196-L206)）。
2. 打开 [dsp/L1/tests/hw/mm2s/mm2s.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp) 与 [s2mm.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp)，在图上标出"DDR↔BRAM↔多路 axis"的三段。
3. 画出端到端通路：`主机 DDR buffer → mm2s_wrapper(DDR→多路 axis) → AIE FFT 图 → s2mm_wrapper(多路 axis→DDR) → 主机取回`。
4. 回答两个判断题：
   - vss 用的是本讲 4.1～4.4 中的哪一代搬运器？（提示：访问形状编译期钉死、有 BRAM 中转但不是 URAM、单方向。）
   - 如果要把 vss 改成"运行时换 POINT_SIZE"，应该换成 4.2/4.3/4.4 中的哪一个？为什么？

**预期产出**：一张含"主机/DDR/PL 内核/AIE 图"四层、标注了协议（m_axi/axis/s_axilite）和数据流向的图，以及两条判断结论。

**参考结论**（先自己想再对照）：

- vss 用的是 **4.1 的 dsp 专用 mm2s/s2mm**：形状（POINT_SIZE/NITER/NSTREAM）编译期钉死，中转用 BRAM（`bind_storage ... impl = bram`），单方向。
- 要运行时换 POINT_SIZE，应换成 **4.2 的 `pl_data_mover`**（9 字描述符够用、无缓存更轻）或 **4.3 的 `pl_4d_data_mover`**（若还要吸收 DDR 抖动、保证 AIE 不空转）。若输入输出想共用一套缓存，则选 **4.4 的 bi_dm**。

## 6. 本讲小结

- **mm2s/s2mm 是 PL↔AIE 系统的必备桥接**：AIE 只认 AXI Stream、DDR 只能按地址访问，二者的协议鸿沟由这两类 PL 内核填平。vss 例子里 mm2s 在前灌数据、s2mm 在后收数据，AIE 图夹在中间只管算。
- **dsp 的 mm2s/s2mm 在搬运的同时做"多路交错重排"**：经一层片上 BRAM（`buff`），把连续 DDR 布局转成 `NSTREAM_INT` 路并行流（或反向），访问形状在编译期钉死。
- **`pl_data_mover` 用 9 字描述符 + 手动突发做通用直连搬运**：访问形状不写死在代码里，运行时换描述符即可；`OUTSTANDING/2` 的余量是为防 4KB 跨界拆包导致的死锁。
- **`pl_4d_data_mover` 加了 URAM 缓存和 24 字 tiling 描述符**：片上 URAM 把 DDR 抖动与 AIE 流式消费解耦，双控制器（AXIM 段 + AXIS 段）经同步流握手；24 字 pattern 支持 tile/stride/wrap，专为张量分块搬运设计。
- **`bi_data_mover` 用一套 URAM 承载读/写双向**：四个控制器 + 四向 URAM 仲裁，相比两个单向搬运器省一半缓存；csim 下用 `std::thread` 模拟并发、综合时退化成 dataflow。
- **三档搬运器是"灵活度 vs 资源/延迟"的阶梯**：4.1 专用最省最死板、4.2 通用直连、4.3 通用带缓存、4.4 通用双向带缓存。选型看"形状是否运行时可变、是否需要解耦 DDR 抖动、读写是否共享数据"。

## 7. 下一步学习建议

- **接着学 AIE 端的对应物**：本讲只讲了 PL 侧搬运。AIE 图那侧怎么"接住"这些流，见 u6-l3（vss 端到端示例）与 u13-l1（ADF 图、window/stream 与 PL↔AIE 边界），届时会把 mm2s/s2mm 的 axis 端口与 AIE 图的输入输出端口在 `system.cfg` 的 `sc=` 行里焊在一起。
- **深入性能与存储分区**：搬运器的实际带宽取决于 DDR/HBM bank 怎么分（u4-l3 的 group_id）、`OUTSTANDING`/`BURSTLEN` 怎么配（本讲 4.2），以及 URAM 缓存深度（本讲 4.3）。系统级调优见 u12-l1（dataflow/SSR/II 调优）与 u12-l2（URAM、HBM/DDR 分区）。
- **自己跑一个搬运器**：data_mover 库的 [L2/tests](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/L2/tests) 下有现成的短接测试（`mm2s_and_s2mm`、`bi_dm_s2mm_mm2s_s2s`），按各自 README 用 `make run TARGET=hw_emu` 跑起来，是把本讲概念落地最快的路径（运行结果待本地验证）。
- **跨库组合的工程视角**：data_mover 库被 dsp/solver 依赖（见 dependency.json，回顾 u1-l2、u15-l1）。学完本讲后，可在 dsp 库的 AIE 示例里反向搜索 `pl_data_mover`/`pl_4d_data_mover` 的 `#include`，观察真实工程如何选档搬运器。
