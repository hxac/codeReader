# HLS 内核接口与数据打包

## 1. 本讲目标

本讲是第八单元「HLS 后处理解码内核」的第一篇，对应端到端流水线（见 [u1-l3](u1-l3-end-to-end-pipeline.md)）最末端的「HLS 后处理」阶段。

DPU 把 YOLOv8 的卷积运算加速完之后，输出的是一张张「原始特征图」（一坨 int8 数字），还不能直接当检测框用。需要一段**解码（decode）**程序，把这些数字翻译成「框的中心、宽高、类别、置信度」。在 [u6-l3](u6-l3-postprocess-optimization.md) 里我们看到，这段解码在 C++ 软件侧已经做了 27× 优化，但仍是主机开销的大头。本单元把这段解码搬上 FPGA 的 PL（可编程逻辑），写成一个 HLS 内核，用硬件并行进一步加速。

本讲**只讲内核的「门面」**——它对外的函数签名、数据怎么打包进出、和主机怎么连线。**不**讲解码算法本身（softmax、距离还原等留到 [u8-l2](u8-l2-decode-algorithm.md)），**也**不讲综合优化指令（留到 [u8-l3](u8-l3-hls-optimization.md)）。

学完本讲你应该能：

1. 说清楚为什么内核入口用 `ap_uint<64>*` 而不是 `int8_t*`，以及这种「8 个 int8 打包进一个 64 位字」如何提升 AXI 带宽利用率。
2. 看懂 `#pragma HLS INTERFACE` 里 `m_axi`（搬大数据）和 `s_axilite`（传小标量、控制握手）的分工。
3. 解释 `bundle=gmem0` / `bundle=gmem1` 把读口和写口分成两条独立 AXI 通道的意义，以及 `MAX_BOXES`、`OUTPUT_DIM`、`NUM_CLASSES` 等常量为何是「可调旋钮」。

## 2. 前置知识

本讲面向已经读完第五单元硬件平台、第六单元推理框架的读者。需要建立几个 HLS / AXI 的基础概念：

- **HLS（High-Level Synthesis，高层次综合）**：用 C/C++ 写算法，由工具（这里是 Vitis HLS 2023.1）自动综合成 FPGA 的 RTL 电路，再走实现、布线变成比特流。我们写的是 C++，但心里要想着「它会变成硬件」。
- **AXI 总线**：在 [u5-l1](u5-l1-kv260-dpu-architecture.md) 里讲过，KV260 的 PS（ARM CPU）和 PL（FPGA）通过 AXI 总线互联、共享 DDR。HLS 内核要读写数据，就是通过 AXI 去 DDR 里取/存。
- **AXI 的两类「瘦胖」接口**：
  - **AXI4（全称 AXI4 Memory-Mapped，HLS 里叫 `m_axi`）**：胖通道，支持**突发（burst）**批量传输，专门用来搬大块内存（特征图、输出数组）。
  - **AXI-Lite（HLS 里叫 `s_axilite`）**：瘦通道，每次传一个 32 位标量，用来传「控制参数」（如图层尺寸、stride）和启动/完成握手。
- **`ap_int.h` 与任意精度整数**：HLS 提供的模板类型 `ap_uint<N>` 表示「N 位无符号整数」，`ap_int<N>` 是有符号版。它们综合成恰好 N 位的硬件寄存器/连线，`ap_uint<64>` 就是一个 64 位的硬件字。
- **DPU 输出是 int8 特征图**：DPUCZDX8G 是 int8 定点加速器（见 [u4-l1](u4-l1-quantization-ptq.md)、[u6-l1](u6-l1-patch-overview.md)），所以解码内核吃进来的就是一串 int8。
- **YOLOv8 的多尺度头**：[u6-l3](u6-l3-postprocess-optimization.md) 提过本项目用 P2/P3/P4/P5 四个检测头（stride 分别为 4/8/16/32），每个头输出一张特征图，所以解码要处理 4 张图。

如果上面某条陌生，建议先回看对应讲义；本讲会反复用到「AXI 突发」「int8 特征图」「4 个 stride 的层」这几个概念。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `platform/post_processing/decode_krnl/decode_kernel.cpp` | HLS 内核主体：顶层函数签名、所有 `#pragma HLS INTERFACE`、可配置常量、解码主循环。本讲核心。 |
| `platform/post_processing/decode_krnl/decode_kernel.h` | 内核函数原型声明，供主机程序和测试台 `#include`。 |
| `platform/post_processing/decode_host/decode.cpp` | OpenCL 主机程序，揭示内核接口在运行时怎么被调用（如何分配 buffer、如何 `setArg`）。用来反证 `m_axi`/`s_axilite` 的设计意图。 |
| `platform/post_processing/decode_krnl/test_bench.cpp` | C++ 测试台，揭示 `ap_uint<64>` 打包数据是怎么构造的。 |
| `platform/post_processing/decode_krnl/hls_config.cfg` | HLS 综合配置：目标器件、时钟、顶层函数名。用来确认这是面向 KV260 的内核。 |

整个内核其实只有**一个顶层函数 `decode_kernel`**，本讲就是把这个函数的「签名 + pragma」逐行讲透。

## 4. 核心概念与源码讲解

### 4.1 `ap_uint<64>` 数据打包

#### 4.1.1 概念说明

DPU 吐出来的特征图是一串 int8（每个值 1 字节）。最朴素的内核写法是把入口声明成 `int8_t* input_data`，然后一个一个字节去读。

问题在于 AXI 总线的传输粒度。KV260 上连接 DDR 和 PL 的 AXI 数据通道宽度是 64 位（8 字节）。如果内核一次只读 1 个 int8，实际硬件却得动用整个 64 位通道、搬来 8 字节，其中 7 字节被浪费——带宽利用率只有 1/8。这就像用一辆 8 座面包车每次只送 1 个人。

解决办法是**打包（packing）**：在主机侧就把连续 8 个 int8 塞进一个 64 位字里，内核入口声明成 `ap_uint<64>*`，一次 AXI 读取同时拿到 8 个 int8。带宽利用率直接拉满到 8/8。这是 HLS 内核里最常见的「喂饱总线」技巧。

#### 4.1.2 核心流程

打包带来的一个直接后果是：内核内部不能再用「第 `idx` 个 int8」直接索引内存，而要先把逻辑下标 `idx` 拆成「第几个 64 位字」和「字内第几个字节」两步：

1. **字下标**：`word_idx = idx >> 3`（即除以 8，因为每个字装 8 个字节）。
2. **字节下标**：`byte_idx = idx & 0x7`（即对 8 取余，0~7）。
3. **取字**：`word = input_data[word_idx]`——这是一次 64 位 AXI 读，一次拿到 8 个候选 int8。
4. **取字节**：把 64 位字右移 `byte_idx * 8` 位，再强转 `int8_t`，就取出想要的那一个字节。

用位运算写出索引映射：

\[
\text{word\_idx} = \left\lfloor \frac{\text{idx}}{8} \right\rfloor, \qquad
\text{byte\_idx} = \text{idx} \bmod 8
\]

注意 `>> 3` 和 `& 0x7` 在硬件里分别是「移位」和「截位」，几乎零成本，比整数除法/取余电路便宜得多——这也是为什么用 8（2 的幂）来打包。

#### 4.1.3 源码精读

内核入口签名里，`input_data` 就是这个打包指针：

[decode_kernel.cpp:23-34](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L23-L34) —— 顶层函数签名，第一个参数 `const ap_uint<64> *input_data` 是「装满各层输出的整块大内存」，注释说明它存放 DPU 各检测头输出的特征图。

文件顶部 include 了提供 `ap_uint` 的头文件：

[decode_kernel.cpp:3-5](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L3-L5) —— `<ap_int.h>` 提供 `ap_uint<N>` 任意精度整数类型；`<hls_stream.h>`、`<ap_fixed.h>` 为后续算法预留。

打包后「按字节提取」的位运算在内核里出现两次。第一次是在「预取类别 logit」时：

[decode_kernel.cpp:104-110](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L104-L110) —— 先算 `word_idx = idx >> 3`、`byte_idx = idx & 0x7`，再 `ap_uint<64> word = input_data[word_idx]` 读整字，最后 `(int8_t)(word >> (byte_idx * 8))` 取出目标字节。这就是「面包车一次装 8 人、再挑出要的那位」的硬件实现。

测试台 `test_bench.cpp` 完整演示了主机侧怎么把 int8 打包成 `ap_uint<64>`：

[test_bench.cpp:33-47](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L33-L47) —— 先算 `PACKED_SIZE = (total_size + 7) / 8`（向上取整的字数），再逐字 `word.range(b*8+7, b*8) = (ap_uint<8>)val` 把 8 个 int8 塞进一个 64 位字。`.range(高位, 低位)` 是 `ap_uint` 的位域赋值语法。

主机程序 `decode.cpp` 里则用 `int8_t*` 视角往同一块 buffer 写数据——这是关键的「类型双关」：

[decode.cpp:193](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L193) 与 [decode.cpp:239-241](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L239-L241) —— 主机把 buffer 当 `int8_t*` 逐字节填值；而内核侧同一片内存被当 `ap_uint<64>*` 按 8 字节一字读取。两边能对上，是因为 AXI 只负责搬字节，不在乎 C++ 类型怎么解释——只要总字节数一致（`depth × 8` 等于主机分配的字节数）即可。

#### 4.1.4 代码实践

> **实践目标**：亲手验证打包前后的「下标映射」和带宽收益。

1. 打开 `decode_kernel.cpp`，定位到 [L104-L110](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L104-L110)。
2. 写一个最小 C++ 片段（普通 PC 即可，不需要 FPGA）模拟这段逻辑：

   ```cpp
   // 示例代码：验证 ap_uint<64> 打包的字节提取（在普通 C++ 上用 uint64_t 模拟）
   #include <cstdint>
   #include <iostream>
   int main() {
       int8_t raw[16] = {0,1,2,3,4,5,6,7, 10,11,12,13,14,15,16,17};
       uint64_t* packed = reinterpret_cast<uint64_t*>(raw); // 2 个 64 位字
       for (int idx = 0; idx < 16; ++idx) {
           int word_idx = idx >> 3;          // 第几个字
           int byte_idx = idx & 0x7;         // 字内第几字节
           int8_t v = (int8_t)(packed[word_idx] >> (byte_idx * 8));
           std::cout << (int)v << " ";       // 应输出 0 1 2 ... 17
       }
   }
   ```

3. **需要观察的现象**：输出应是 `0 1 2 3 4 5 6 7 10 11 12 13 14 15 16 17`，与原始 `raw` 逐项一致，证明 `idx → (word_idx, byte_idx)` 映射正确。
4. **预期结果**：逐字节读和打包后按字读取到同一份数据。进而体会：朴素 `int8_t*` 每取一个值触发一次「1 字节有效」的访问；打包后每取一个字，后续 7 个 `idx` 可复用同一字（或至少同一次突发），有效负载提升至 8 倍。
5. 运行环境为普通 g++，**待本地验证**（无 FPGA 依赖）。

#### 4.1.5 小练习与答案

**练习 1**：如果把打包宽度从 8 字节（`ap_uint<64>`）改成 4 字节（`ap_uint<32>`），索引代码 `word_idx`、`byte_idx` 该怎么改？带宽利用率会变好还是变差？

> **答案**：`word_idx = idx >> 2`、`byte_idx = idx & 0x3`。带宽利用率降为 4/8（假设 AXI 物理通道仍是 64 位），反而变差。`ap_uint<64>` 是为了「恰好填满」KV260 的 64 位 AXI 数据通道。

**练习 2**：为什么用 `idx >> 3` 和 `idx & 0x7`，而不是 `idx / 8` 和 `idx % 8`？

> **答案**：移位 `>>` 和截位 `&` 在硬件里是几乎免费的位操作；而通用整数除法/取余电路面积大、延迟高。2 的幂打包让索引映射退化成位运算，是 HLS 的常规优化。

---

### 4.2 INTERFACE pragma：`m_axi` 与 `s_axilite` 的分工

#### 4.2.1 概念说明

HLS 把 C++ 函数综合成硬件时，每个函数参数都会变成一个「端口」。`#pragma HLS INTERFACE` 就是用来**指定每个端口走哪种 AXI 协议**。本内核把端口分成两类，对应 AXI 的「胖通道」和「瘦通道」：

- **`m_axi`（AXI4 Memory-Mapped）**：给大数组用。支持突发传输，连到 DDR，用来搬特征图（`input_data`，上百万字节）和输出框数组。配套的 `offset=slave` 表示「这块内存的基地址由主机通过一个 AXI-Lite 寄存器下发」——也就是主机运行时把 buffer 指针传进来。
- **`s_axilite`（AXI-Lite）**：给小标量用。每个参数占一个 32 位寄存器，主机逐个写值。本内核用它传 `layer_size`、`layer_stride`、`out_box_num`，以及 `return`（函数返回，承担启动/完成握手）。

这种「大数据走 `m_axi`、控制走 `s_axilite`」的分工，正是 Vitis 加速器模型的标准范式：主机通过 AXI-Lite 写好参数和 buffer 指针、敲一下「启动」寄存器，内核通过 `m_axi` 突发搬数据干活，干完通过「完成」寄存器回报。

#### 4.2.2 核心流程

一次内核调用的端到端时序（对照主机 [decode.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp)）：

1. **主机 `setArg`**：把 `m_axi` buffer 的指针、`s_axilite` 标量值写进内核的控制寄存器组。
2. **主机 `enqueueMigrateMemObjects`**：把 `input_data` 从主机内存搬进设备 DDR（触发一次 host→device 的 DMA）。
3. **主机 `enqueueTask`**：写「启动」寄存器，内核开始跑。
4. **内核执行**：通过 `m_axi`（`offset=slave` 给的基地址）从 DDR 突发读特征图、算完把框数组通过 `m_axi` 突发写回 DDR；`out_box_num` 这个标量写进它的 `s_axilite` 寄存器。
5. **内核完成**：通过 `return` 对应的握手寄存器通知主机。
6. **主机 `enqueueMigrateMemObjects`**：把输出 buffer 从设备 DDR 搬回主机。

```
主机                           AXI-Lite (控制/标量)              内核
 │ setArg(layer_size, 200)  ────────────────────────────────► │ s_axilite 寄存器
 │ setArg(<buffer 指针>)    ────────────────────────────────► │ m_axi offset=slave
 │ enqueueMigrate(input)    ──► DDR  (host→device)
 │ enqueueTask (启动)       ────────────────────────────────► │ 开始执行
 │                              ◄──── m_axi 突发读 input ───── │
 │                              ──── m_axi 突发写 output ────► │ (DDR)
 │                          ◄──────── 完成 (return) ───────── │
 │ enqueueMigrate(output)   ◄── DDR  (device→host)
```

#### 4.2.3 源码精读

所有 INTERFACE pragma 集中在函数体开头：

[decode_kernel.cpp:37-50](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L37-L50) —— 这 14 行是本讲的「主舞台」。逐条拆解：

- 第 38 行（有效）：`#pragma HLS INTERFACE m_axi port=input_data offset=slave bundle=gmem0 depth=335000` —— 输入特征图走 `m_axi`，基地址由主机下发（`offset=slave`），归入 bundle `gmem0`，深度声明 335000 个 64 位字。
- 第 37 行（被注释）：`depth=2680000` 是打包前的旧版本，按 int8 字节计数；打包后字数缩为 1/8，故改成 335000。（`depth` 的含义见 4.3。）
- 第 40-45 行：6 个输出数组（`out_boxes_x/y/w/h`、`out_cls`、`out_score`）全部 `m_axi offset=slave bundle=gmem1 depth=2048` —— 输出走 `m_axi`，归入 bundle `gmem1`。
- 第 47-49 行：`layer_size`、`layer_stride`、`out_box_num` 用 `s_axilite` —— 小标量走瘦通道。
- 第 50 行：`return` 用 `s_axilite` —— 这是 Vitis 内核的**强制要求**，`return` 端口承担 `ap_ctrl_chain`/`ap_ctrl_hs` 的启动-完成握手，没有它主机就无法知道内核何时跑完。

主机侧 `decode.cpp` 的调用方式正好印证这套设计。buffer 指针通过 `setArg` 下发（对应 `m_axi offset=slave`）：

[decode.cpp:179-190](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L179-L190) —— 依次 `setArg` 把 input buffer、6 个输出 buffer 传进内核；其中 `layer_size`、`layer_stride` 的 `setArg` 被记住下标（`layer_size_arg_index`），因为每层都要改值。

而标量参数逐层重设（对应 `s_axilite`）：

[decode.cpp:244-247](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L244-L247) —— 每层循环里用 `setArg(layer_size_arg_index, feat_layer_size)` 改写这两个 `s_axilite` 标量寄存器，证明它们确实是「主机可逐次写入的控制寄存器」，而非大数组。

> 关于 `offset=slave` 的细节：它告诉 HLS「这个 `m_axi` 端口的基地址偏移来自一个 AXI-Lite 从寄存器」。运行时主机把 device buffer 的地址写进这个寄存器，内核据此发起对 DDR 的突发访问。这正是主机 `cl::Buffer` + `setArg(buffer)` 能把不同 buffer 喂给同一内核的原因。

#### 4.2.4 代码实践

> **实践目标**：把 9 个 INTERFACE pragma 分类成表格，理解每个端口的「角色」。

1. 打开 [decode_kernel.cpp:37-50](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L37-L50)。
2. 自制一张分类表（答案见本节末「参考答案」），每行填：**参数名 / pragma 类型（m_axi 或 s_axilite）/ bundle / 方向（读或写）**。判断方向时回到函数签名看 `const`（读）还是普通数组（写）。
3. 在 [decode.cpp:179-190](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L179-L190) 核对：主机对每个 `m_axi` 端口都 `setArg` 了一个 `cl::Buffer`，对每个 `s_axilite` 端口都 `setArg` 了一个标量值——类型一一对应。
4. **需要观察的现象**：`m_axi` 端口都配 `cl::Buffer`（buffer 指针），`s_axilite` 端口都配普通 int/float 标量；两者泾渭分明。
5. **预期结果**：9 个端口中，7 个 `m_axi`（1 个读 + 6 个写）、4 个 `s_axilite`（3 个标量 + `return`）。

> **参考答案（分类表）**
>
> | 参数 | 类型 | bundle | 方向 | 说明 |
> |---|---|---|---|---|
> | `input_data` | m_axi | gmem0 | 读（const） | 输入特征图，大块 |
> | `out_boxes_x` | m_axi | gmem1 | 写 | 输出框中心 x |
> | `out_boxes_y` | m_axi | gmem1 | 写 | 输出框中心 y |
> | `out_boxes_w` | m_axi | gmem1 | 写 | 输出框宽 |
> | `out_boxes_h` | m_axi | gmem1 | 写 | 输出框高 |
> | `out_cls` | m_axi | gmem1 | 写 | 输出类别 |
> | `out_score` | m_axi | gmem1 | 写 | 输出置信度 |
> | `layer_size` | s_axilite | — | 读标量 | 每层网格尺寸 |
> | `layer_stride` | s_axilite | — | 读标量 | 每层步长 |
> | `out_box_num` | s_axilite | — | 写标量 | 实际写出框数 |
> | `return` | s_axilite | — | 控制 | 启动/完成握手 |

#### 4.2.5 小练习与答案

**练习 1**：如果把 `input_data` 的 pragma 改成 `s_axilite`，综合还能过吗？为什么不行？

> **答案**：综合可能报错或生成无法工作的设计。`s_axilite` 每次只传一个 32 位标量，无法承载「上百万字节的数组基地址 + 突发传输」语义；大数组必须用 `m_axi` 才能连到 DDR 并支持突发。

**练习 2**：`return` 端口为什么必须显式写成 `s_axilite`？删掉会怎样？

> **答案**：Vitis 加速器内核靠 `return`（即 `ap_ctrl_hs`/`ap_ctrl_chain`）做启动-完成握手，主机 `enqueueTask` 写「启动」、轮询「完成」。在 Vitis flow 下，HLS 通常会自动补上；但显式写出是清晰且安全的做法，确保控制寄存器归入同一个 AXI-Lite 从接口，供主机统一访问。

---

### 4.3 bundle 划分与可配置常量

#### 4.3.1 概念说明

**bundle（束）** 是 `m_axi` pragma 的一个关键字段：**bundle 名相同的 `m_axi` 端口会被合并成同一个物理 AXI 主端口**，共享一条通往 DDR 的通道（占用一个 HP/HPC 接口）；bundle 名不同则各占一条独立通道。

本内核只用了两个 bundle：

- `gmem0`：只有 `input_data`，独占一条读通道。
- `gmem1`：6 个输出数组共享一条写通道。

这种「读一条、写一条」的划分有两个好处：一是**读写可以并行**——内核通过 `gmem0` 读下一批输入的同时，可通过 `gmem1` 写上一批输出，两条物理通道互不阻塞；二是**省端口资源**——KV260 上 PL 到 DDR 的 HP 端口数量有限（u5-l1 提过 BRAM/端口资源紧张），把 6 个小输出合并到一个端口，比开 6 个端口省得多。代价是 6 个输出彼此串行复用 `gmem1`，但它们每个只有 2048 个元素（见下），且只在最后统一写回（[L305-L333](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L305-L333)），串行开销可忽略。

**可配置常量**是顶部一组 `#define`，把「会随设计/模型变化」的尺寸抽出来，方便改一处而全局生效。

#### 4.3.2 核心流程

bundle 与端口数量的关系：

\[
\text{物理 AXI 主端口数} = \text{不同 bundle 名的个数}
\]

本内核有 `gmem0`、`gmem1` 两个 bundle → 2 个物理 AXI 主端口（1 读 + 1 写）。

可配置常量约束了内核各处的数组尺寸与循环边界：

| 常量 | 值 | 含义 |
|---|---|---|
| `MAX_BOXES` | 2048 | NMS 前每层最多保留多少个框；所有输出数组和本地缓冲都按它定长 |
| `DIST_BINS` | 16 | 距离 softmax 每个「分支」的 bin 数（DFL 的 16 bin） |
| `NUM_CLASSES` | 3 | 类别数（非船/船/渔船，见 [u2-l3](u2-l3-label-conversion.md)） |
| `OUTPUT_DIM` | 67 | 每个 anchor 的输出通道数 = 4×16 + 3 = 64 + 3 |

`OUTPUT_DIM=67` 的来历正是 [u6-l3](u6-l3-postprocess-optimization.md) 讲的：每个 anchor 先有 4 个距离分支 × 16 bin = 64 个距离 logit，再加 `NUM_CLASSES=3` 个类别 logit，合计 67。这条等式在测试台和主机里都自洽：`output_dim = num_classes + 64 = 3 + 64 = 67`（[test_bench.cpp:11-12](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L11-L12)、[decode.cpp:156-157](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L156-L157)）。

`depth` 字段则是给 HLS 的「容量声明」：它告诉工具这个 `m_axi` 端口**最多会被访问多少个元素**，主要用于 C/RTL 协同仿真时自动生成测试适配器（testbench adapter）。`depth` 不影响最终 RTL 的功能，但若小于实际访问量，协同仿真会越界。本内核 `input_data` 的 `depth=335000`，6 个输出的 `depth=2048`（= `MAX_BOXES`）。

#### 4.3.3 源码精读

可配置常量集中定义在文件顶部：

[decode_kernel.cpp:12-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L12-L18) —— 注释写着「Configurable limits (tune for your design)」，明确这组 `#define` 是可调旋钮；`MAX_BOXES`、`DIST_BINS`、`NUM_CLASSES`、`OUTPUT_DIM` 四项。注意第 13 行 `MAX_INPUT 2680000` 被注释掉了，它正是 `depth` 的「字节数版本」。

`MAX_BOXES` 决定了输出数组和片上本地缓冲的尺寸（注意输出数组的 `depth=2048` 与之相等）：

[decode_kernel.cpp:27-33](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L27-L33) —— 6 个输出数组都声明为 `... [MAX_BOXES]`，即 2048 个元素。

[decode_kernel.cpp:60-65](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L60-L65) —— 片上本地缓冲 `local_boxes_x` 等也按 `MAX_BOXES` 定长，综合成 BRAM/URAM。

`NUM_CLASSES` 与 `OUTPUT_DIM` 的用法体现在「类别 logit 从第 64 个通道开始」：

[decode_kernel.cpp:96-98](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L96-L98) —— `int idx_base = n * OUTPUT_DIM + 64` 表示第 `n` 个 anchor 的前 64 个通道是距离 logit（4×16），从第 64 号起才是 3 个类别 logit。这正是 `OUTPUT_DIM=67` 的语义落点。

bundle 划分直接出现在 pragma 里（已在 4.2.3 引用 [L37-L50](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L37-L50)）：`input_data` 独占 `gmem0`，6 个输出共享 `gmem1`。

最后用综合配置确认这是面向 KV260 的内核：

[hls_config.cfg:1-7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/hls_config.cfg#L1-L7) —— `part=xck26-sfvc784-2LV-c` 是 KV260 SOM 的器件型号；`clock=5` 即 5ns（200MHz）目标时钟；`syn.top=decode_kernel` 指定本函数为综合顶层。换言之，上面所有接口都会被综合成这个器件上的真实端口。

#### 4.3.4 代码实践

> **实践目标**：算清 `depth=335000` 的来历，理解 depth 与可配置常量的耦合。

1. 打开 [decode_kernel.cpp:38](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L38) 与 [decode.cpp:14](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L14)、[decode.cpp:160-161](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L160-L161)。
2. 计算最大层的字节数：四个层 `feat_layer_sizes = {200,100,50,25}`，`output_dim = 67`。最大层是 200×200：
   \[
   200 \times 200 \times 67 = 2{,}680{,}000 \text{ 字节}
   \]
   这正是主机常量 `MAX_FEATURE_SIZE = 2680000`（注释「max size for 200x200 layer with output_dim=67」）。
3. 把字节数换算成 64 位字数（即 `ap_uint<64>` 元素数）：
   \[
   \frac{2{,}680{,}000}{8} = 335{,}000 \text{ 字}
   \]
   这就是 `depth=335000`。
4. 反算总字节数验证一致性：
   \[
   335{,}000 \times 8 = 2{,}680{,}000 \text{ 字节} \approx 2.68 \text{ MB（十进制）}\approx 2.55 \text{ MiB（二进制）}
   \]
   它等于主机 buffer 的分配大小 `MAX_FEATURE_SIZE * sizeof(int8_t)`（[decode.cpp:75](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L75)），主机与内核容量自洽。
5. **需要观察的现象**：`depth` 不是随便填的数，而是「最大单层特征图的字节数 ÷ 8」；注释掉的 `depth=2680000` 正是除以 8 之前的字节数版本。
6. **预期结果**：能讲清「200×200 是 stride=4 的 P2 头对应 800×800 输入（800/4=200，呼应 [u6-l3](u6-l3-postprocess-optimization.md) 的 P2 头与 [u1-l3](u1-l3-end-to-end-pipeline.md) 的 imgsz=800），它的特征图最大，故 depth 按它取」。

> **为什么取最大层？** 内核对每一层都被调用一次（[decode.cpp:221](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L221) 循环），输入 buffer 被复用。`depth` 是端口级的「最大访问量声明」，必须覆盖最大那一层，否则协同仿真会越界。

#### 4.3.5 小练习与答案

**练习 1**：本内核实际占用几个物理 AXI 主端口？如果想让 6 个输出各自独占一条通道，该怎么改？代价是什么？

> **答案**：当前 2 个（`gmem0` 读 + `gmem1` 写）。若给每个输出起独立 bundle 名（如 `gmem1`…`gmem6`），则变成 7 个主端口，读写可更并行；代价是占用更多 PL→DDR 的 HP 接口与地址发生器资源（u5-l1 指出 KV260 资源紧张，尤其 BRAM/端口），对本内核这些小输出而言收益甚微、不划算。

**练习 2**：如果把 `NUM_CLASSES` 改成 5（假设换一个 5 类模型），需要同步修改哪些地方？

> **答案**：至少改：(a) 内核顶部 `#define NUM_CLASSES 5` 与 `#define OUTPUT_DIM (5+64)=69`；(b) `depth` 要重算（最大层字节数 = 200×200×69，再 ÷8）；(c) 主机/测试台的 `num_classes`、`output_dim` 与 buffer 大小。这正是把这组尺寸抽成 `#define` 的意义——但也提醒接口常量是跨主机/内核/测试台**三方一致**的契约（呼应全链路的「一致性暗线」）。

**练习 3**：`MAX_BOXES=2048` 同时被输出数组、片上本地缓冲、`depth=2048` 三处使用。把它调大（如 4096）会同时影响哪两类硬件资源？

> **答案**：片上本地缓冲 `local_boxes_*`（[L60-L65](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L60-L65)）会吃更多 BRAM/URAM；输出数组变大则增加 host↔device 搬运量。这是「容量 vs 资源/带宽」的权衡，需结合 u5-l1 的资源余量（BRAM 仅剩约 35 块）谨慎调。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「接口审计」：

**任务**：假设你要把这个解码内核从「处理 4 层 YOLOv8 头」改造成「只处理 3 层（去掉 P2 头，即去掉 stride=4 的 200×200 层）」，请基于本讲所学，列出对**接口与打包**层面的全部改动，并说明每条改动的依据。

提示步骤：

1. **可配置常量**：`NUM_CLASSES`、`DIST_BINS`、`OUTPUT_DIM` 是否要改？（答：类别与距离结构没变，三者都不用改。）
2. **`depth`**：去掉 200×200 这一层后，最大层变成 100×100（stride=8）。重算 `depth = (100×100×67)/8 = 83750`。把 [decode_kernel.cpp:38](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L38) 的 `depth=335000` 改成 `83750`，主机 `MAX_FEATURE_SIZE`（[decode.cpp:14](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L14)）也随之缩小。
3. **bundle / pragma 类型**：`gmem0`/`gmem1` 划分、`m_axi`/`s_axilite` 分工是否要改？（答：完全不用，端口语义与层数无关。）
4. **主机调用**：`feat_layer_sizes`/`feat_layer_strides`（[decode.cpp:160-161](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L160-L161)）从 4 元素数组删到 3 元素，`num_layers=3`。

**交付物**：一张「改动清单」表，列出文件、行号、旧值、新值、依据。完成后你应能体会到：**接口 pragma（bundle、m_axi/s_axilite）是稳定的架构决策，而 `depth` 与常量是随模型/层数变化的可调参数**——这正是本讲把它们分成 4.2 与 4.3 两个模块的原因。

> 注：本实践为源码阅读与设计推演型任务，不依赖 FPGA；实际改完后需在 Vitis HLS 2023.1 里重新综合与协同仿真验证，**待本地验证**。

## 6. 本讲小结

- 内核入口用 `ap_uint<64>* input_data` 把 8 个 int8 打包进一个 64 位字，恰好填满 KV260 的 64 位 AXI 数据通道，带宽利用率从 1/8 提升到 8/8；内核内用 `idx>>3`、`idx&0x7` 做字/字节索引。
- `#pragma HLS INTERFACE` 把端口分成两类：大数组走 `m_axi offset=slave`（基地址由主机下发、支持突发）、小标量与握手走 `s_axilite`，这是 Vitis 加速器的标准范式。
- bundle `gmem0`（独占读输入）与 `gmem1`（6 个输出共享写）构成「1 读 + 1 写」两条独立物理 AXI 通道，读写可并行又省 HP 端口。
- `MAX_BOXES`、`DIST_BINS`、`NUM_CLASSES`、`OUTPUT_DIM` 是可配置常量，其中 `OUTPUT_DIM=67=4×16+3` 体现了「4 距离分支×16 bin + 3 类别」的 anchor 结构。
- `depth=335000 = 200×200×67÷8`，等于最大层（stride=4 的 P2 头）打包后的字数，是端口级容量声明，必须覆盖最大层。
- 主机（`decode.cpp`）用 `int8_t*` 写、内核用 `ap_uint<64>*` 读同一片内存，靠 AXI「只搬字节」实现跨边界类型双关——前提是 `depth×8` 与主机分配字节数一致。

## 7. 下一步学习建议

本讲只搭好了内核的「门面」。接下来：

- **[u8-l2 解码算法实现](u8-l2-decode-algorithm.md)**：进入 `ANCHOR_LOOP` 主循环，讲清楚 4.1 里那句 `(int8_t)(word >> (byte_idx*8))` 取出来的 int8 logit 后续怎么用——整型置信阈值剔除、4 个距离分支的 16-bin softmax 加权、anchor 中心 + 距离还原出 `cx/cy/w/h`。
- **[u8-l3 HLS 优化指令](u8-l3-hls-optimization.md)**：精读 `#pragma HLS UNROLL/PIPELINE/ARRAY_PARTITION`，看本讲提到的 `PREFETCH_LOOP`（[L99-L111](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L99-L111)）等循环如何被流水化、`cls_logits` 如何被 `complete` 划分进片上 RAM。
- **[u8-l4 测试激励与 OpenCL 宿主程序](u8-l4-testbench-host.md)**：深入 `test_bench.cpp` 与 `decode.cpp`，把本讲用到的主机调用细节（`setArg`、`enqueueMigrateMemObjects`、profiling 三段计时）讲透。

建议在进入 u8-l2 前，先回头确认本讲的「端口分类表」（4.2.4 参考答案）你已经能默写——后续两篇都会假设你清楚「输入走 gmem0、输出走 gmem1、标量走 s_axilite」这张底图。
