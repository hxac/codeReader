# PL 包路由器仿真与 testbench

## 1. 本讲目标

上一讲（u6-l1）我们读懂了 PL 上唯一的 HLS 内核 `dma_pkt_router`：它读取带包头的 128 位 AXI-Stream，按 `instance_id` 把乱序图像重排进 DDR。本讲回答下一个自然的问题——**这个内核写对了吗？怎么在不上板的情况下验证它？**

本讲围绕 PL 包路由器的仿真 testbench 展开，学完后你应当能够：

1. 说清 testbench 如何把 aiesimulator 产出的 PLIO trace CSV 重新拼装成 `hls::stream<ap_axiu<128>>` 喂给内核；
2. 理解 Vitis HLS 的 csim（C 仿真）/ cosim（协同仿真）流程，以及 `set_part`、`create_clock` 如何把仿真锚定到 VCK190 器件与 312.5 MHz 时钟；
3. 追踪 Makefile 里 `plsim_router` 目标的依赖链——为何要先跑 aiesim、产物落在哪些目录、最终 `output_img.csv` 与主机 `writeImg()` 输出为何格式一致。

## 2. 前置知识

在进入源码前，先用三段话补齐概念直觉。

**为什么要单独仿真一个 PL 内核。** 整个反投影设计是「ARM + AIE + PL」三域协同的，上板验证代价极高（要走完构建、打包、烧板，见 u7-l3）。而 `dma_pkt_router` 的逻辑是**纯局部、可隔离**的：它的输入是 AIE 末端 `pktmerge<32>` 吐出的 128 位流，输出是 DDR 里一段连续图像。只要我们能「录下」AIE 的输出流当激励，就能脱离整张图单独验证这个内核的重排逻辑。这就是 testbench 的核心思想——**用 aiesim 录制的 CSV 当回放激励，把 PL 内核当成被测单元（DUT）**。

**aiesim 的 PLIO trace 是什么。** 当 AIE 图里有一个**输出** PLIO 端口（数据从 AIE 流向 PL），`aiesimulator` 会把流过该端口的每一拍数据落盘成一个 CSV 文件，文件名形如 `aie_to_plio_switch_<图实例>_<plio实例>.csv`。本项目有 7 个输出 PLIO（`plio_pkt_rtr_out_0_0` … `_0_6`，对应 7 个 PL 内核实例），所以 aiesim 会产出 7 个这样的 CSV。testbench 逐个读它们，等价于逐个驱动 7 个 PL 内核实例。

**csim 与 cosim 的差别。** Vitis HLS 仿真分两档：**csim**（C Simulation）只编译并运行 C++ 源码，不综合、不涉及时序，最快，用来验证**算法功能**正确；**cosim**（Co-Simulation）会先把内核综合成 RTL，再挂载 testbench 做RTL级仿真，能验证**时序/握手**是否正确，但慢得多。本设计的 testbench 主要面向 csim（快速验证重排逻辑），TCL 脚本里通过一个变量切换是否进一步跑 cosim。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `design/pl/tb/dma_pkt_router_tb.cpp` | testbench 主体：读 aiesim CSV → 构造 `ap_axiu<128>` 流 → 调用内核 → 把 DDR 结果写成 `output_img.csv`。 |
| `design/pl/tb/run_dma_pkt_router_tb.tcl` | 驱动 Vitis HLS 的 Tcl 脚本：建工程、设器件/时钟、跑 csim（可选 cosim）。 |
| `design/pl/pkt_router_config.cfg` | HLS 编译配置（顶层函数、综合文件、时钟），供 `v++ --mode hls` 读取。 |
| `design/pl/dma_pkt_router.cpp` | 被测内核本身（u6-l1 已详解，本讲引用其签名与循环结构）。 |
| `Makefile` | `plsim_router` 目标与 `aiesim` 目标，串起依赖链。 |
| `design/host/sar_backproject.cpp` | 主机 `writeImg()`，用来与 testbench 输出格式对账。 |

## 4. 核心概念与源码讲解

### 4.1 仿真动机与 testbench 全局骨架

#### 4.1.1 概念说明

testbench 的设计哲学写在源码注释里的一句关键话：「**It helps to imagine this testbench from the AI Engine perspective**」（从 AIE 的视角来想象这个 testbench）。意思是：testbench 扮演的是**AIE 那一侧**——它产生 AIE 本会产生的流数据；而被测的 `dma_pkt_router` 则扮演 PL 那一侧。两者之间用一根 `hls::stream` 连起来，等价于一根 128 位 AXI4-Stream。

之所以这样安排，是因为真实硬件里 7 个 PL 内核实例**共享同一个 DDR 图像 buffer**，各自按 `instance_id` 写不重叠的区段（见 u6-l1 的 `ddr_offset = instance_id * SAMPLES_PER_KERN`）。testbench 用一个 `for` 循环跑 7 次、每次喂一个 PLIO 的 CSV，正好复现「7 个实例依次写同一块 DDR」的场景——因此 testbench 的循环变量名叫 `pl_kern`（PL 内核实例）。

#### 4.1.2 核心流程

整个 testbench 的 `main()` 可以概括为四步：

```text
1. 估算总 beat 数 AXIS128_SAMPLES，声明流 pl_stream_in，分配并清零 DDR 缓冲 ddr_mem。
2. for pl_kern = 0 .. AIE_SWITCHES-1:          # 7 次，对应 7 个 PL 实例
       打开 aie_to_plio_switch_0_<pl_kern>.csv
       逐行解析 CSV，把每行拼成一拍 ap_axiu<128>，write 进 pl_stream_in
       调用 dma_pkt_router(pl_stream_in, ddr_mem)   # 内核把这段流重排进 DDR
3. 把 ddr_mem 按 cfloat 视图写成 output_img.csv（每 RC_SAMPLES 个换行）。
4. 打印原始 DDR 内容供肉眼检查。
```

注意第 2 步里，**流是先填满一整个 PLIO 的数据，再一次性喂给内核**；内核内部会用 `IMG_KERNEL_LOOP` 恰好消费掉这 32 个包（每个包 = 1 拍包头 + 数据拍），消费完流恰好空，下一轮 `pl_kern` 再重新填充。所以流对象虽然跨轮复用，但不会残留数据。

#### 4.1.3 源码精读

先看常量、流与 DDR 缓冲的声明：

[design/pl/tb/dma_pkt_router_tb.cpp:17-26](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L17-L26) —— 计算 `AXIS128_SAMPLES` 并声明流、分配 DDR。注释解释了公式的两部分：除以 2 是因为 128 位总线一拍装 2 个 64 位 cfloat；加 `IMG_SOLVERS` 是因为每个图像重建内核会额外产生 1 拍 128 位包头。

代入默认宏（PULSES=602、RC_SAMPLES=512、IMG_SOLVERS=7×32=224）：

\[
\text{AXIS128\_SAMPLES} = \frac{602 \times 512}{2} + 224 = 154112 + 224 = 154336
\]

这个值在 testbench 里**只用于注释说明、并不直接驱动循环**（循环靠读 CSV 到 EOF 终止）。它是一把「理论标尺」，让读者知道整张图应当产出多少拍 128 位数据。

> 自检：每个重建核产出 1 拍包头 + \(1376/2=688\) 拍数据 = 689 拍；×32 核 = 22048 拍/PL实例；×7 实例 = 154336 拍，与上式吻合。

DDR 缓冲按 `ap_uint<64>` 分配 `PULSES*RC_SAMPLES` 个元素（即 308224 个 64 位字 ≈ 2.35 MiB），并清零——这就是图像最终落脚的整块内存，7 轮 `pl_kern` 都往这里写。

接着是「7 个 PL 实例」的外层循环与内核调用：

[design/pl/tb/dma_pkt_router_tb.cpp:28-33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L28-L33) —— 外层 `pl_kern` 循环与 CSV 路径。注释点明两个细节：每次迭代等价于「再调用一个 PL 内核实例」；路径里的 `0` 是 `bpGraph` 图实例号（假设全图只实例化一次，所以固定 0），`pl_kern` 才是会变的 PLIO/PL 实例号。

最后看内核调用与「7 实例共享同一 DDR」的复现：

[design/pl/tb/dma_pkt_router_tb.cpp:108-111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L108-L111) —— 把填好的流连同**同一个** `ddr_mem` 指针传给内核。注意 testbench 不传基地址偏移，偏移完全由内核内部按 `instance_id` 算（见 [design/pl/dma_pkt_router.cpp:62-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L62-L63)），所以 7 次调用写进的是同一块 buffer 的不重叠区段——这正是真实硬件里 7 个 PL 实例并行写同一 DDR buffer 的单线程等价复现。

#### 4.1.4 代码实践（源码阅读型）

**目标**：理解 testbench 如何用单线程循环等价复现「7 个并行 PL 实例」。

**步骤**：

1. 打开 [design/pl/tb/dma_pkt_router_tb.cpp:29](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L29)，确认外层循环上界是 `AIE_SWITCHES`（=7）。
2. 打开被测内核 [design/pl/dma_pkt_router.cpp:31](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L31)，确认内层 `IMG_KERNEL_LOOP` 上界是 `IMG_SOLVERS_PER_SWITCH`（=32）。
3. 推算：7（外）× 32（内）= 224 次「读包头 → 写一段 DDR」，覆盖全部 224 个重建核的输出。

**需要观察的现象**：外层循环次数（7）= PL 内核实例数；内层循环次数（32）= 每实例处理的重建核数；二者乘积 = 全图重建核总数。

**预期结果**：你能用一句话说清「testbench 的两层循环（C++ 外层 + 内核内层）合起来正好遍历全部 224 个重建核的输出包」，且每次写 DDR 的偏移互不重叠。

#### 4.1.5 小练习与答案

**练习 1**：testbench 里 `AXIS128_SAMPLES` 既然不驱动循环，它存在的意义是什么？

**参考答案**：它是一个**说明性常量**，给读者一把「整张图理论应产出多少拍 128 位数据」的标尺（154336 拍），便于人工核对 aiesim CSV 的行数是否合理。循环本身靠读到 CSV 文件尾（EOF）终止。

**练习 2**：为什么 testbench 把 `ddr_mem` 分配成 `ap_uint<64>*` 而不是 `ap_uint<128>*`？

**参考答案**：因为图像的基本单元是 64 位 cfloat（32 位实部 + 32 位虚部）。内核按 64 位字写 DDR（`ddr_mem[ddr_offset+idx]`），testbench 也按 64 位分配与清零；128 位只是 AXI-Stream **总线**的宽度（一拍装 2 个 cfloat），并非存储单元宽度。

---

### 4.2 最小模块一：aiesim CSV → ap_axiu<128> 流解析

#### 4.2.1 概念说明

这是 testbench 最核心、也最琐碎的一段：把 aiesim 录下的 **CSV 文本**重新变成硬件能消费的 **128 位 AXI-Stream 拍（beat）**。难点在于 aiesim 的 PLIO trace 是人读友好的文本（带表头、带 `DATA:` 标记、十六进制/`-1` 混用的 TKEEP），而 `ap_axiu<128,0,0,0>` 是位精确的硬件类型——testbench 必须做一次**反序列化**（文本 → 位向量），并正确填充 `data`、`last`（TLAST）、`keep`（TKEEP）三路副信号。

理解这段的关键是搞清「CSV 一行 = AXI-Stream 一拍」的对应关系，以及四个 32 位数据字 `D0~D3` 如何拼成 128 位。

#### 4.2.2 核心流程

CSV 解析的步骤如下：

```text
打开 CSV → 读并丢弃表头行
while 读取一行:
    if 该行不以 "DATA:" 开头: 跳过          # 过滤非数据行
    按逗号切分，trim 每个字段 → tokens[]
    D0,D1,D2,D3 = stoul(tokens[1..4])       # 四个 32 位字
    tlast  = stoi(tokens[5])                # 0 或 1
    tkeep  = 解析 tokens[6]（支持 -1 / 0x.. / 十进制 / 空）
    data128.range(31,0)=D0; (63,32)=D1; (95,64)=D2; (127,96)=D3
    pkt.data=data128; pkt.last=tlast; pkt.keep=tkeep
    pl_stream_in.write(pkt)                 # 入流
```

注意字段下标从 `tokens[1]` 开始——因为 `tokens[0]` 是 `"DATA:"` 这个操作标记本身（每行以 `DATA:` 打头）。

#### 4.2.3 源码精读

先看「丢表头 + 过滤非 DATA 行」的骨架：

[design/pl/tb/dma_pkt_router_tb.cpp:40-49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L40-L49) —— 先 `getline` 丢弃第一行表头，再用 `line.rfind("DATA:", 0) == 0` 只保留以 `DATA:` 起始的行。`rfind(..., 0)` 是 C++ 里「判断字符串是否以某子串开头」的常用惯用法（等价于 `starts_with`，兼容旧标准）。

接着是按逗号切分并 trim：

[design/pl/tb/dma_pkt_router_tb.cpp:53-68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L53-L68) —— 用 `stringstream` + `getline(.., ',')` 切分，并对每个字段做前后空白 trim；`D0~D3` 用 `std::stoul` 转 32 位无符号。这里 `tokens[1]~tokens[4]` 是四个 32 位数据字——它们合起来正是 u6-l1 里内核读取的那 128 位：`D0` 低 32 位是包交换头（含 `pkt_id`/`pkt_type`），但要注意**首拍**的 `D1`（次 32 位）才是 `instance_id`，由内核在包头解析阶段提取。

然后是两路副信号 TLAST 与 TKEEP 的解析：

[design/pl/tb/dma_pkt_router_tb.cpp:70-86](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L70-L86) —— `TLAST` 直接 `stoi`；`TKEEP` 要兼容 aiesim 的多种写法：`-1` 表示「全部字节有效」（转成 128 位总线的 `0xFFFF`）、`0x..` 按十六进制解析、纯数字按十进制、空串视作全有效。这一段值得细读，因为 TKEEP 的多形态处理是 testbench 里最容易踩坑的地方。

最后把四个字拼成 128 位、装配 `ap_axiu` 并入流：

[design/pl/tb/dma_pkt_router_tb.cpp:91-105](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L91-L105) —— 用 `data128.range(31,0) / (63,32) / (95,64) / (127,96)` 按小端顺序填入 `D0~D3`，再赋给 `pkt.data`、`pkt.last`、`pkt.keep`，`write` 进流。这一拍就是内核 `pl_stream_in.read()` 拿到的那一拍——文本到硬件的转换在此完成。

> 术语小释：`ap_axiu<W,0,0,0>` 是 Vitis HLS 对 AXI4-Stream 数据通路的建模模板，`W=128` 是数据位宽，三个 `0` 分别是 TID/TDEST/TUSER 的位宽（本设计都没用，故为 0）。它除了 `.data` 外还带 `.last`（包尾标志 TLAST）和 `.keep`（字节使能 TKEEP）等副信号。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证「CSV 一行的 4 个数据字 = AXI-Stream 一拍的 128 位」，并理解首拍包头字段。

**步骤**：

1. 在 [dma_pkt_router_tb.cpp:64-68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L64-L68) 确认四个数据字来自 `tokens[1..4]`。
2. 在 [dma_pkt_router_tb.cpp:92-96](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L92-L96) 确认它们被填进 128 位的低/次/次高/高 32 位。
3. 对照内核 [dma_pkt_router.cpp:51-56](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L51-L56)：内核从首拍的 `range(31,0)` 取包头、从 `range(63,32)` 取 `instance_id`——正好对应 testbench 填的 `D0` 和 `D1`。

**需要观察的现象**：testbench 写入的位序与内核读取的位序**完全一致**（都是 `D0`=低 32 位、`D1`=次 32 位）。

**预期结果**：你能说清「testbench 的 `D0/D1` 经 128 位拼装后，被内核原样读作包头/`instance_id`」，从而确认 testbench 没有把字节序拼反。

**待本地验证**：若你有 aiesim 产出的真实 CSV，可数一下单个 `aie_to_plio_switch_0_0.csv` 的 `DATA:` 行数是否 = 22048（= 32 包 × 689 拍/包）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tokens` 的数据字从下标 `1` 而不是 `0` 取？

**参考答案**：因为每行以 `DATA:` 打头，按逗号切分后 `tokens[0]` 就是 `"DATA:"` 这个操作标记；真正的数据从 `tokens[1]` 开始。

**练习 2**：TKEEP 写成 `-1` 时，testbench 为什么要转成 `0xFFFF`？

**参考答案**：128 位的 AXI4-Stream 总线有 16 个字节，TKEEP 用 16 位（`0xFFFF`）表示「全部 16 字节都有效」。aiesim 用 `-1` 作为「全有效」的简写，testbench 需把它翻译成位宽正确的 `0xFFFF` 才能如实还原硬件副信号。

---

### 4.3 调用内核、写出 output_img.csv 并与主机 writeImg() 对账

> 本节是 4.1 的延伸，专门讲「内核跑完之后如何落盘」，并完成规格里要求的核心对账：testbench 的 `output_img.csv` 与主机 `writeImg()` 在**格式上完全一致**。它不是独立的最小模块，但理解它才能做对本讲的综合实践。

内核被调用 7 次后，`ddr_mem` 里就躺着重排好的完整图像（308224 个 cfloat）。testbench 接下来把它写成 CSV。关键两步：把 `ap_uint<64>*` 重新看成 `float*`，再用与主机**一模一样**的格式串写出。

[design/pl/tb/dma_pkt_router_tb.cpp:113-129](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L113-L129) —— 打开输出文件、`reinterpret_cast<float*>` 把 DDR 视作浮点数组，然后按 `%.12f%+.12fi` 格式写每个 cfloat，每 `RC_SAMPLES` 个换一行。

现在对照主机侧的 `writeImg()`：

[design/host/sar_backproject.cpp:135-152](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L135-L152) —— 主机把 `m_img_arrays[0]`（cfloat 数组）写成同样的 CSV。

把两者逐点对账，得到下表——**它们在格式上完全一致**：

| 对账项 | testbench (`dma_pkt_router_tb.cpp`) | 主机 (`sar_backproject.cpp` `writeImg`) |
| --- | --- | --- |
| 格式串 | `%.12f%+.12fi` | `%.12f%+.12fi` |
| 首元素 | 不带前导逗号单独写 | 不带前导逗号单独写 |
| 换行规则 | `i%RC_SAMPLES==0` 时 `\n` | `i%RC_SAMPLES==0` 时 `\n` |
| 元素总数 | `PULSES*RC_SAMPLES` | `PULSES*RC_SAMPLES` |
| 数据来源 | `ddr_mem` 的 `float*` 视图（`[i*2]`=实, `[i*2+1]`=虚） | `m_img_arrays[0][i].real/.imag`（cfloat） |

**为什么故意保持一致？** 这样 PL 独立仿真产出的 `output_img.csv` 就能与上板运行主机产出的图像 CSV **逐行逐字段比对**——若两者数值一致，就说明 `dma_pkt_router` 的重排逻辑在 PL 侧正确还原了 AIE 的输出；若不一致，则定位到 PL 重排出错。这是脱离整板上板、单独给 PL 内核「打分」的关键手段。

> 细节：格式串里的 `%+` 强制虚部带符号（如 `0.123-0.456i`），所以复数写出来总是 `a+bi` 或 `a-bi` 的紧凑形式，没有空格。两端都依赖这个约定，比对时才不会被空格/符号差异干扰。

---

### 4.4 最小模块二：csim/cosim 流程与 set_part/create_clock

#### 4.4.1 概念说明

`run_dma_pkt_router_tb.tcl` 是驱动整个 HLS 仿真实验的 Tcl 脚本，用 `vitis-run --tcl` 启动。它做四件事：建工程并挂源码与 testbench、指定顶层函数、设置**目标器件与时钟**、按需跑 csim/cosim/综合。其中「器件 + 时钟」是把仿真锚定到真实硬件（VCK190、312.5 MHz）的关键——`pkt_router_config.cfg` 则是同一套配置的文本化身，供 `v++ --mode hls` 命令行编译时读取。

#### 4.4.2 核心流程

```text
open_project dma_pkt_router_testbench
  add_files dma_pkt_router.cpp        # 被测设计
  add_files -tb dma_pkt_router_tb.cpp # testbench
  set_top dma_pkt_router              # 顶层函数
open_solution solution1 -flow_target vitis
  set_part   xcvc1902-vsva2197-2MP-e-S   # VCK190 的 Versal 器件型号
  create_clock -period 312.5MHz         # 目标时钟
  set hls_exec 2                        # 选择执行档位
  csim_design                           # C 仿真（必定运行）
  根据 hls_exec: csynth_design / +cosim_design / +export_design
exit
```

`hls_exec` 是档位开关：`1`=只综合；`2`=综合+协同仿真；`3`=再导出 IP；其他=只综合。注意 **csim 总会跑**（它在 `if` 之前），因为 testbench 的首要目的是功能验证。

#### 4.4.3 源码精读

工程与顶层设定：

[design/pl/tb/run_dma_pkt_router_tb.tcl:4-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/run_dma_pkt_router_tb.tcl#L4-L16) —— `add_files` 挂设计源、`add_files -tb` 挂 testbench、`set_top dma_pkt_router` 指明顶层（与 [dma_pkt_router.h:15-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.h#L15-L16) 的签名一致）。`$script_dir` 用 `[file dirname [info script]]` 取本 Tcl 所在目录，保证从任意 cwd 调用都能找到源码。

器件、时钟与档位：

[design/pl/tb/run_dma_pkt_router_tb.tcl:21-30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/run_dma_pkt_router_tb.tcl#L21-L30) —— 三个关键设定。`set_part {xcvc1902-vsva2197-2MP-e-S}` 是 VCK190 板上 Versal Premium 器件型号（与 Makefile 的 PLATFORM 对应）；`create_clock -period "312.5MHz"` 是内核目标时钟；`hls_exec=2` 表示跑综合 + cosim。

> 时钟取 312.5 MHz 并非随意——它正是 [design/pl/pkt_router_config.cfg:2](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/pkt_router_config.cfg#L2) 与 system.cfg 里 `default_freqhz=312500000`（见 u7-l1）的一致约定：PL 内核、AIE→PL PLIO、system 时钟三者对齐到同一频率，跨域流才能正确握手。

档位分支：

[design/pl/tb/run_dma_pkt_router_tb.tcl:35-53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/run_dma_pkt_router_tb.tcl#L35-L53) —— `hls_exec==1` 仅 `csynth_design`（综合，看资源/时序报告）；`==2` 加 `cosim_design`（RTL 协同仿真，验证握手）；`==3` 再 `export_design -format ip_catalog`（导出成可复用的 IP）；`else` 默认只综合。

最后看 `pkt_router_config.cfg` 这个「配置文本化身」：

[design/pl/pkt_router_config.cfg:1-7](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/pkt_router_config.cfg#L1-L7) —— 它把 Tcl 里的关键设定（时钟、顶层、综合源、testbench 文件、产物格式 `xo`）以 `.cfg` 形式给出，供 Makefile 里 `v++ -c --mode hls --config pkt_router_config.cfg`（见 [Makefile:220-222](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L220-L222)）命令行编译用。即：**仿真走 Tcl，实编译走 cfg**，两者共享同一套时钟/顶层约定。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解「Tcl 仿真配置」与「cfg 实编译配置」如何共享器件/时钟/顶层约定。

**步骤**：

1. 读 [run_dma_pkt_router_tb.tcl:24-25](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/run_dma_pkt_router_tb.tcl#L24-L25)，记下器件 `xcvc1902-...` 与时钟 `312.5MHz`。
2. 读 [pkt_router_config.cfg:2-6](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/pkt_router_config.cfg#L2-L6)，确认时钟与顶层函数一致。
3. 读 [Makefile:220-222](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L220-L222)，确认 cfg 被 `v++ --mode hls` 消费。

**需要观察的现象**：仿真（Tcl）与实编译（cfg）用的是**同一个时钟频率与同一个顶层函数名**。

**预期结果**：你能解释「为什么仿真验证通过的设计，实编译后行为应当一致」——因为两者锚定到同一器件、同一时钟、同一顶层。

#### 4.4.5 小练习与答案

**练习 1**：把 `hls_exec` 从 2 改成 1，testbench 还会跑吗？

**参考答案**：**会**。`csim_design`（运行 testbench 的 C 仿真）写在 `if` 之前，**与 `hls_exec` 无关、总会执行**。`hls_exec` 只控制 csim **之后**是否继续做综合（1）、综合+cosim（2）、综合+cosim+导出（3）。改成 1 只是少跑 cosim。

**练习 2**：为什么 Tcl 里要用 `set_part` 显式指定 `xcvc1902-...`，而不是随便一个 Versal 型号？

**参考答案**：因为综合与 cosim 要针对**真实目标器件**的资源（LUT/寄酱/BRAM/URAM）和布线结构评估时序是否收敛。VCK190 板卡用的是 `xcvc1902-vsva2197-2MP-e-S` 这一具体型号，指定它才能让资源/时序报告对上板有参考价值。

---

### 4.5 最小模块三：plsim_router 目标的依赖链

#### 4.5.1 概念说明

`make plsim_router` 是把前面所有片段串成一个命令的「一键仿真」入口。它的核心职责有三：**(a) 保证激励存在**——若 aiesim 的 PLIO trace CSV 还没生成，就先跑 aiesim；**(b) 准备输出目录**；**(c) 调 `vitis-run --tcl` 跑 testbench**。理解这条依赖链，就理解了「PL 仿真为什么离不开 aiesim」——testbench 的输入不是凭空造的，而是 AIE 仿真录下来的真实输出。

#### 4.5.2 核心流程

```text
make plsim_router:
  1. 若 build/hw/aiesim/aiesimulator_output/aie_to_plio_switch_0_0.csv 不存在:
         make aiesim   # 先跑 AIE 仿真产出 7 个 PLIO trace CSV
  2. mkdir -p build/hw/plsim/plsimulator_output
  3. cd build/hw/plsim
  4. vitis-run --tcl design/pl/tb/run_dma_pkt_router_tb.tcl | tee plsim_router.log

而 make aiesim 本身依赖 libadf.a（AIE 图编译产物）:
  design/aie/* + common.h --v++ --mode aie--> libadf.a
  libadf.a --aiesimulator--> aiesimulator_output/*.csv (含 aie_to_plio_switch_0_*.csv)
```

完整依赖链：`design/aie 源码 + common.h → libadf.a → aiesim → aie_to_plio_switch_0_*.csv → testbench(csim) → output_img.csv`。

#### 4.5.3 源码精读

先看 `plsim_router` 目标本身：

[Makefile:127-138](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L127-L138) —— 三段式逻辑。第 129-131 行是关键的「激励守卫」：检查 `aie_to_plio_switch_0_0.csv` 是否存在，不存在则 `$(MAKE) aiesim` 先生成它。第 132-133 行建输出目录、`cd` 到 `build/hw/plsim`（`PLSIM_BUILD_DIR`）。第 134 行用 `vitis-run --tcl` 跑 Tcl 脚本，并用 `tee` 同时落日志。

> 这里有个**重要的相对路径推论**：`vitis-run` 在 `build/hw/plsim` 下创建工程 `dma_pkt_router_testbench`，csim 的实际工作目录在 `build/hw/plsim/dma_pkt_router_testbench/solution1/csim/build`。testbench 里读 CSV 的相对路径是 `../../../../../aiesim/aiesimulator_output/...`（5 个 `..` 回到 `build/hw`，再进 `aiesim`），写输出的路径是 `../../../../plsimulator_output/...`（4 个 `..` 回到 `build/hw/plsim`，再进 `plsimulator_output`）。这两条路径与 Makefile 的 `AIESIM_BUILD_DIR`、`PLSIM_BUILD_DIR` 完全对得上。注意 testbench 源码第 114 行注释把输出路径的 cwd 写成 `design/pl/tb/...`（源码树布局）是**过时注释**，与第 31 行注释（`build/hw/plsim/...`）以及 Makefile 实际行为不符——以 Makefile 与第 31 行为准。

接着看「激励守卫」依赖的 `aiesim` 目标：

[Makefile:148-156](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L148-L156) —— `aiesim` 依赖 `libadf.a`，在 `build/hw/aiesim` 下跑 `aiesimulator`，产出 `aiesimulator_output/` 目录（里面就有 testbench 要读的 `aie_to_plio_switch_0_*.csv`）。注意它还带 `--input-dir ${PLSIM_BUILD_DIR}/plsimulator_output`，这是给 GMIO 输入端口喂数据用的（与 PLIO 输出 trace 是两个方向）。

最后看最底层的 `libadf.a` 编译：

[Makefile:230-244](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L230-L244) —— `design/aie/*` 与 `common.h` 经 `v++ --mode aie` 编译成 `libadf.a`（AIE 图库）。这是整条依赖链的源头：没有它就没有 aiesim，没有 aiesim 就没有 PLIO CSV，没有 CSV 就没法跑 PL 包路由器仿真。

#### 4.5.4 代码实践（源码阅读型，对应本讲综合实践的子任务）

**目标**：说清 `plsim_router` 为何在 CSV 缺失时先跑 aiesim，以及输出格式如何与主机对账。

**步骤**：

1. 读 [Makefile:128-134](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L128-L134)，定位「if CSV 不存在则 `$(MAKE) aiesim`」这一行。
2. 读 [Makefile:148](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L148)，确认 `aiesim` 依赖 `libadf.a`。
3. 回到 4.3 的对账表，确认 testbench 的 `output_img.csv` 与主机 `writeImg()` 格式一致。

**需要观察的现象**：`plsim_router` 不直接依赖 `libadf.a`，而是**运行时检查 CSV 文件是否存在**来决定是否触发 aiesim——这是一种「按产物存在性」的惰性依赖。

**预期结果**：你能解释「testbench 的输入必须由 aiesim 先产出，所以 Makefile 用文件存在性守卫来保证激励就绪」，并说清输出格式与主机端的一致性。

**待本地验证**：在有 Vitis 工具链的环境里执行 `make plsim_router`，观察：首次运行会先触发 `make aiesim`（能看到 aiesimulator 输出），随后 `vitis-run` 跑 csim，最终在 `build/hw/plsim/plsimulator_output/` 下生成 `output_img.csv`。

#### 4.5.5 小练习与答案

**练习 1**：如果你只改了 `dma_pkt_router.cpp`（PL 内核）而没动 AIE 源码，`make plsim_router` 会重新跑 aiesim 吗？

**参考答案**：**取决于 PLIO trace CSV 是否还在**。`plsim_router` 的守卫只看 `aie_to_plio_switch_0_0.csv` 文件**是否存在**，不看时间戳、也不看 AIE 源码是否变过。只要该 CSV 还在（哪怕是旧的），就不会重跑 aiesim——这在只改 PL 内核时正是想要的行为（激励复用）。但若你删了 `build/` 或手动删了 CSV，它就会重新跑 aiesim。

**练习 2**：testbench 里读 CSV 用 5 个 `..`、写输出用 4 个 `..`，为什么不一样？

**参考答案**：因为 csim 工作目录在 `build/hw/plsim/dma_pkt_router_testbench/solution1/csim/build`，要到达 aiesim 输出（`build/hw/aiesim`）需要先回到 `build/hw`（5 个 `..`），而到达 plsim 的输出目录（`build/hw/plsim/plsimulator_output`）只需回到 `build/hw/plsim`（4 个 `..`）。两个目标目录在目录树里的深度不同，故 `..` 个数不同。

---

## 5. 综合实践

**任务**：追踪 `make plsim_router` 的完整依赖链，并解释其输出与主机 `writeImg()` 为何可比对。

**操作步骤**：

1. **依赖链追踪**：从 [Makefile:128](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L128) 出发，画出有向依赖图：
   - `plsim_router` →（CSV 缺失时）`aiesim` → `libadf.a` → `design/aie/*` + `common.h`。
   - `plsim_router` → `vitis-run --tcl run_dma_pkt_router_tb.tcl` → csim 读 CSV → 写 `output_img.csv`。
2. **回答「为何先 aiesim」**：testbench 的输入 `aie_to_plio_switch_0_*.csv` 不是凭空造的，而是 AIE 仿真录下来的真实输出流；没有 aiesim 就没有激励，所以 Makefile 用「文件存在性守卫」保证激励就绪后再跑 PL 仿真。
3. **格式对账**：把 [dma_pkt_router_tb.cpp:123-129](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/tb/dma_pkt_router_tb.cpp#L123-L129) 与 [sar_backproject.cpp:144-150](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L144-L150) 并排比较，确认两者格式串（`%.12f%+.12fi`）、首元素处理、换行规则（每 `RC_SAMPLES` 个换行）、元素总数（`PULSES*RC_SAMPLES`）**完全一致**。

**预期结果**：

- 你能画出 `design/aie → libadf.a → aiesim → PLIO CSV → csim → output_img.csv` 这条完整链路；
- 你能解释「PL 仿真依赖 aiesim 先产出激励」的根因（testbench 回放的是 AIE 录制的流）；
- 你能说明 `output_img.csv` 与主机图像 CSV 在格式上逐字段一致，因此可脱离上板、单独给 `dma_pkt_router` 的重排正确性打分。

**待本地验证**：在配好 Vitis + Versal 工具链的机器上跑 `make plsim_router`，确认产物路径与日志顺序与上述分析吻合。

## 6. 本讲小结

- testbench 的设计哲学是「**站在 AIE 视角**」：用 aiesim 录制的 PLIO trace CSV 当回放激励，把 `dma_pkt_router` 当被测单元，用一根 `hls::stream<ap_axiu<128>>` 连接两者。
- **CSV → 流解析**：每行 `DATA:` 切成 `D0~D3` 四个 32 位字拼成 128 位 `data`，再配 `TLAST`/`TKEEP` 副信号；TKEEP 需兼容 `-1`/`0x..`/十进制/空串四种写法。
- testbench 用 7 次 `pl_kern` 循环 × 内核内 32 次 `IMG_KERNEL_LOOP`，复现「224 个重建核的输出经 7 个 PL 实例写进同一块 DDR」的场景，偏移由内核按 `instance_id` 自算。
- **csim/cosim**：Tcl 脚本里 `csim_design` 总会跑（功能验证），`hls_exec` 只控制之后是否综合/cosim/导出；器件锚定 VCK190（`xcvc1902-...`）、时钟锚定 312.5 MHz，与 `pkt_router_config.cfg` 及 system.cfg 共享同一约定。
- **依赖链**：`make plsim_router` 用「CSV 文件存在性守卫」在激励缺失时先跑 `make aiesim`，而 aiesim 依赖 `libadf.a`（AIE 图编译产物），链路为 `design/aie → libadf.a → aiesim → PLIO CSV → csim → output_img.csv`。
- **输出对账**：testbench 的 `output_img.csv` 与主机 `writeImg()` 在格式串、换行、元素总数上**完全一致**，因此可逐字段比对、单独验证 PL 重排正确性。
- 阅读彩蛋：testbench 第 114 行关于输出路径 cwd 的注释已过时，应以第 31 行注释与 Makefile 实际行为（`build/hw/plsim/...`）为准。

## 7. 下一步学习建议

- **横向**：回头对照 u6-l1，确认 testbench 喂入的包头字段（`pkt_id`/`pkt_type`/`instance_id`）与内核解析逻辑（[dma_pkt_router.cpp:51-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L51-L63)）严丝合缝——这是 testbench 能正确驱动内核的前提。
- **向后（系统集成）**：进入 u7-l1「系统集成：system.cfg、XSA 链接与打包」，看 `dma_pkt_router.xo`（由 [Makefile:200-227](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L200-L227) 用同一份 `pkt_router_config.cfg` 编译而成）如何与 `libadf.a` 链接成 XSA，理解「仿真用 Tcl、实编译用 cfg」如何收口到同一器件/时钟。
- **向后（仿真全貌）**：进入 u8-l1「AIE 与 PL 仿真流程」，把本讲的 PL 仿真与 aiesim/aiesim_profile/aiesim_xpe 串联，看清 sw_emu/hw_emu/hw 三种 TARGET 下仿真手段的差异。
