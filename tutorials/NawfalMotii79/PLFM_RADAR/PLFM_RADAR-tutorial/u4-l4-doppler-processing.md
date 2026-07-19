# Doppler 处理与 FFT 引擎

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚什么是「慢时间 FFT」，为什么对一串 chirp 的回波做 FFT 就能得到目标速度。
2. 解释 AERIS-10 为什么用「双 16 点 FFT」而不是「单个 32 点 FFT」，并能把这套 staggered-PRI（长短 PRI 交替）的设计与发射波形的 long/short chirp 对应起来。
3. 读懂 `doppler_processor.v`（实际模块名 `doppler_processor_optimized`）的状态机，知道数据是怎么按「行写列读」组织进 BRAM、再逐个距离门送进 FFT 的。
4. 厘清 `xfft_16` 与 `fft_engine` 的关系，看懂一个 16 点 FFT 在硬件里是怎么跑出来的。
5. 追踪 `frame_complete` 信号如何跨模块传到下游的 CFAR，触发一整张 Range-Doppler 图的检测。

## 2. 前置知识

本讲是接收信号处理链的倒数第二步。在进入 Doppler 之前，请确认你已经理解以下概念（它们在前置讲义里都已建立）：

- **脉冲压缩与距离像**（u4-l2）：匹配滤波把每个 chirp 的回波压成一条「距离像（range profile）」，横轴是距离门。本讲的输入就是这条距离像。
- **距离抽取与 MTI**（u4-l3）：1024 个原始距离门被压成 64 个；MTI 做了静止杂波对消。Doppler 处理器吃的就是 MTI 之后、每个 chirp 64 个距离门的 I/Q 数据。
- **跨时钟域 `new_chirp_frame`**（u3-l2）：发射端每开始一帧扫描会发一个单拍脉冲，经 toggle-CDC 送到 100MHz 处理域。这个脉冲在本讲里是「一帧开始」的同步信号。
- **复数 I/Q 数据**：DDC（u4-l1）把实信号混频成复基带，保留了相位。Doppler 信息就藏在逐 chirp 的相位变化里，必须有 I/Q 才算得准。

> 名词速查：
> - **PRI**（Pulse Repetition Interval）：相邻两个脉冲之间的时间间隔，即一个 chirp 周期。
> - **PRF**（Pulse Repetition Frequency）：PRI 的倒数，即每秒发多少个脉冲。
> - **快时间（fast time）**：一个 chirp 内部沿距离方向的采样时间。
> - **慢时间（slow time）**：chirp 与 chirp 之间的时间轴，也就是「第几个 chirp」。Doppler 就在慢时间轴上。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `9_Firmware/9_2_FPGA/` 下：

| 文件 | 实际模块名 | 作用 |
| --- | --- | --- |
| `doppler_processor.v` | `doppler_processor_optimized` | Doppler 处理器主体：组织数据矩阵、加窗、调度双 16 点 FFT、输出打包后的 Doppler 结果 |
| `xfft_16.v` | `xfft_16` | 16 点 FFT 的 AXI-Stream 封装层，把 `fft_engine` 包成上层好用的接口 |
| `fft_engine.v` | `fft_engine` | 可参数化的 radix-2 DIT FFT/IFFT 运算核，真正的蝶形运算在这里 |
| `fft_twiddle_16.mem` | —— | 16 点 FFT 的旋转因子（twiddle factor）ROM，只有 4 个表项 |

下游衔接（不在本讲精读范围，但会引用其连接点）：

| 文件 | 作用 |
| --- | --- |
| `radar_receiver_final.v` | 例化 `doppler_processor_optimized`，把 `frame_complete` 改名 `doppler_frame_done` |
| `radar_system_top.v` | 把 `doppler_frame_done` 接到 `cfar_inst.frame_complete`，并对 `doppler_bin` 做 DC notch |
| `tb/tb_doppler_cosim.v` | Doppler 协同仿真 testbench，用真实数据做 exact-match 比对 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**慢时间 FFT**（Doppler 测速的原理与数据组织）、**双 16 点子帧架构**（为什么不是 32 点）、**FFT 引擎**（xfft_16 与 fft_engine）、**帧完成信号**（frame_complete 如何驱动 CFAR）。

### 4.1 慢时间 FFT：从一摞距离像到速度谱

#### 4.1.1 概念说明

脉冲压缩之后，每个 chirp 给你一条距离像（64 个距离门）。如果连续发 M 个相同的 chirp 打向同一片空域，你会拿到 M 条距离像。把这 M 条距离像按「第几个 chirp」堆起来，就得到一个二维矩阵：

- 每一**行**是一个距离门、沿「慢时间（chirp 编号）」排列的时间序列。
- 每一**列**是一个 chirp、沿「快时间（距离门）」排列的距离像。

对一个静止目标，它在每条距离像里都出现在同一个距离门、且幅度相位基本不变——这一行的慢时间序列是直流。但对一个运动目标，它在逐个 chirp 之间会有微小的距离变化，反映到回波上就是**相位的缓慢旋转**。沿慢时间轴对每一行做 FFT，这种相位旋转就会变成一个明确的频率峰——这就是 **Doppler 频率**。

物理上，径向速度 \(v_r\) 与 Doppler 频率 \(f_d\) 的关系是：

\[ f_d = \frac{2 v_r}{\lambda} \]

其中 \(\lambda\) 是波长（10.5 GHz 对应约 2.86 cm），系数 2 来源于「雷达波往返」的往返路径。把上面的式子反过来，就能从测到的 \(f_d\) 算出目标速度：

\[ v_r = \frac{f_d \, \lambda}{2} \]

慢时间 FFT 有两个关键指标：

- **最大不模糊速度**：受 Nyquist 限制，\(|v| < v_{\max}\)，其中

  \[ v_{\max} = \frac{\lambda \cdot \mathrm{PRF}}{4} \quad\text{（基带复采样，每周期}\lambda/2\text{往返）} \]

  超过这个速度的目标会「折叠」到错误的 bin，这就是速度模糊。提高 PRF 能扩大不模糊速度范围，但会缩短最大不模糊距离——这就是雷达里经典的「距离/速度模糊权衡」。

- **速度分辨率**：N 点 FFT 把一个 PRI 区间切成 N 份，所以

  \[ \Delta v = \frac{\lambda \cdot \mathrm{PRF}}{2 N} \]

  N 越大，速度看得越细。AERIS-10 的 N=16，所以每个子帧给 16 个 Doppler 门。

> 一句话总结：**快时间 FFT（匹配滤波）给距离，慢时间 FFT（Doppler）给速度**。两者叠在一起就是一张「Range-Doppler 图」，横轴距离门、纵轴速度门、每格的亮度是能量。后面 CFAR 就是在这张图上找亮点。

#### 4.1.2 核心流程

要让慢时间 FFT 真正跑起来，硬件要解决一个看似简单、实则棘手的问题：**数据是按 chirp 顺序到达的（按行），但 FFT 要沿 chirp 方向算（按列）**。这需要一次「矩阵转置」。

`doppler_processor_optimized` 用「双口 BRAM + 地址反排」实现这个转置，流程是：

1. **写入阶段（按行）**：每个 chirp 的 64 个距离门样本，按地址 `(chirp_index, range_bin)` 顺序写进 BRAM。
2. **转置读取（按列）**：当一个帧的 32 个 chirp 全部写完，改用地址 `(doppler_index, range_bin)` 读出——固定一个 `range_bin`，连续扫过 16 个 chirp，就拿到该距离门的整条慢时间序列。
3. **加窗 + 送 FFT**：对这 16 个样本做 Hamming 加窗，喂进 `xfft_16`。
4. **输出**：FFT 吐出 16 个 Doppler bin，打包后送下游。

地址反排的精髓就藏在两条地址公式里（见 4.1.3）。

#### 4.1.3 源码精读

先看模块的参数与端口——注意**文件名是 `doppler_processor.v`，但模块名是 `doppler_processor_optimized`**（这是本项目常见的「文件名 ≠ 模块名」情况）：

[doppler_processor.v:35-71](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L35-L71) —— 模块声明。关键参数：`DOPPLER_FFT_SIZE=16`（每子帧 FFT 点数）、`RANGE_BINS=64`、`CHIRPS_PER_FRAME=32`（一帧总 chirp 数）、`CHIRPS_PER_SUBFRAME=16`。注意输出 `doppler_bin[4:0]` 是 5 位、`range_bin[5:0]` 是 6 位，它们的打包含义在 4.2 讲。

数据矩阵的存储用两块 BRAM（实部 I、虚部 Q 各一块），深度就是整个数据矩阵的大小：

[doppler_processor.v:111-113](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L111-L113) —— `MEM_DEPTH = RANGE_BINS * CHIRPS_PER_FRAME = 64 * 32 = 2048`。`(* ram_style = "block" *)` 显式要求综合工具把它映射成 Block RAM，而不是分布式查找表。

**转置的核心就在这两行地址计算**：

[doppler_processor.v:153-154](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L153-L154) ——

```verilog
assign mem_write_addr = (write_chirp_index * RANGE_BINS) + write_range_bin;
assign mem_read_addr  = (read_doppler_index  * RANGE_BINS) + read_range_bin;
```

仔细看：写地址用「chirp 在高位、range 在低位」，读地址用「doppler（即 chirp）在高位、range 在低位」，**两者的维度排布其实相同**。但关键在于写时 `write_chirp_index` 沿 chirp 方向慢变、读时固定 `read_range_bin` 让 `read_doppler_index` 连续扫过 16 个值——于是对同一个 `read_range_bin`，连续 16 个读地址是 `0*R+rb, 1*R+rb, ..., 15*R+rb`，正好把这一列（同一个距离门、跨 16 个 chirp）的样本一气读出。这就是「按行写、按列读」的矩阵转置。

实际写入在 Block 2 里完成，数据来自上游 32 位的 `range_data`（低 16 位是 I、高 16 位是 Q）：

[doppler_processor.v:384-407](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L384-L407) —— `S_IDLE`/`S_ACCUMULATE` 状态下，`mem_we<=1`、`mem_waddr_r<=mem_write_addr`、`mem_wdata_i<=range_data[15:0]`、`mem_wdata_q<=range_data[31:16]`。注意写 BRAM 的 `always` 块特意**不带异步复位**（见 L206-213），这是为了让综合工具放心地把它推断成真正的 Block RAM（带复位反而会变成寄存器堆）。

> 关于 `range_data` 的来源：它来自 `radar_receiver_final.v` 里 MTI 的输出，见 [radar_receiver_final.v:427-430](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L427-L430)，把 `{mti_range_q, mti_range_i}` 拼成 32 位、`mti_range_valid` 作为 `data_valid`。这就是 u4-l3 里 MTI 与 Doppler 的衔接点。

#### 4.1.4 代码实践

**实践目标**：亲手验证「按行写、按列读」的转置，确认你能从地址公式算出哪个样本被读出。

**操作步骤**：

1. 设 `RANGE_BINS=64`。假设上游按 chirp 0,1,2,…,31 的顺序写入，每个 chirp 写 64 个距离门。
2. 在纸上写下 chirp 0 的距离门 0 写入了哪个地址（用 `mem_write_addr` 公式）。
3. 现在处理器要算距离门 `read_range_bin=5` 的 Doppler。读地址用 `read_doppler_index` 从 0 扫到 15，写出连续 16 个读地址。
4. 对照写地址表，确认这 16 个读地址命中的正是「chirp 0..15 的距离门 5」——也就是同一列。

**需要观察的现象**：连续 16 个读地址之间相差 `RANGE_BINS=64`，而不是 1。这说明读取是在跨 chirp 跳跃，正是慢时间方向。

**预期结果**：

- chirp 0 / range 0 → 写地址 `0`；chirp 1 / range 0 → 写地址 `64`。
- 读 `range=5`、`doppler=0..15` → 读地址 `5, 69, 133, …`（每次 +64），正好命中 chirp 0..15 的 range 5。

> 本实践为「源码阅读 + 手算」型，不依赖运行环境，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果 `RANGE_BINS` 改成 128，`MEM_DEPTH` 变成多少？地址总线上需要几位？

> **答**：`MEM_DEPTH = 128 * 32 = 40196`。当前地址线是 11 位（最大 2047），改 128 后需要 ≥12 位（覆盖到 4095），代码里 `mem_write_addr`/`mem_read_addr` 的位宽 `[10:0]` 也要相应加宽。

**练习 2**：为什么 Doppler 处理器必须收到 I/Q 两路，而不能只用一路实信号？

> **答**：Doppler 频率可正可负（目标靠近 vs 远离）。实信号的频谱关于零频共轭对称，无法区分 \(+f_d\) 与 \(-f_d\)；复 I/Q 保留了相位，频谱不再对称，从而能判断目标运动方向。代码用两块 BRAM `doppler_i_mem`/`doppler_q_mem` 分别存 I 与 Q（L112-113）正是为此。

### 4.2 双 16 点子帧架构与 staggered PRI

#### 4.2.1 概念说明

这是本讲最关键、也是最容易让人困惑的设计：**明明一帧有 32 个 chirp，为什么不直接做一个 32 点 FFT，反而要拆成「两个 16 点 FFT」？**

答案藏在发射波形里。AERIS-10 的 PLFM 波形是 **staggered PRI**（交错 PRI）：一帧 32 个 chirp 里，前 16 个是「长 PRI chirp」、后 16 个是「短 PRI chirp」（对应发射机 `plfm_chirp_controller` 里的 long/short chirp，详见 u5-l1）。也就是说，这 32 个样本**不是均匀采样**的——前 16 个间隔大、后 16 个间隔小。

而离散傅里叶变换（DFT/FFT）有一个前提：**输入必须是均匀采样的**。对一组非均匀间隔的样本直接做 32 点 FFT，数学上就是错的——频谱会泄漏、峰位会偏，得不到可信的 Doppler。所以源码注释里直白地写了：

> Rather than a single 32-point FFT over the non-uniformly sampled frame (which is signal-processing invalid), this module processes each sub-frame independently.

正确做法是：把均匀的 16 个长 PRI chirp 单独做一个 16 点 FFT、把均匀的 16 个短 PRI chirp 单独做一个 16 点 FFT。每个子帧内部采样是均匀的，FFT 才合法。

那为什么费这么大劲要长短两种 PRI？为了**解速度模糊**。回忆 4.1：单个 PRF 下，超过 \(v_{\max}\) 的目标会折叠。长短 PRI 对应两套不同的 \(v_{\max}\)，同一个真实速度在两套 FFT 里会落到**不同的 Doppler bin**。比较两个 bin 的位置（类似中国剩余定理），就能反推出真实速度——这就是 staggered-PRF 解模糊。源码注释也点明了这一点：

> This architecture enables downstream staggered-PRF ambiguity resolution: the same target velocity maps to DIFFERENT Doppler bins at different PRIs.

#### 4.2.2 核心流程

两个子帧的处理顺序由状态机 `S_OUTPUT` 统一调度，对每个距离门执行「先长后短」两次 FFT：

```
对每一个 range_bin (0..63):
    current_sub_frame = 0（长 PRI 子帧，chirps 0..15）
        → S_PRE_READ → S_LOAD_FFT（送 16 样本）→ S_FFT_WAIT（收 16 个 bin）
    current_sub_frame = 1（短 PRI 子帧，chirps 16..31）
        → S_PRE_READ → S_LOAD_FFT → S_FFT_WAIT
    两个子帧都做完 → 进入下一个 range_bin
全部 64 个 range_bin 做完 → 回 S_IDLE，frame_complete 拉起
```

每个子帧的输出是 16 个 bin，两个子帧共 32 个，但它们共享同一个 5 位的 `doppler_bin` 端口，打包方式是：

\[ \text{doppler\_bin}[4:0] = \{\text{sub\_frame},\ \text{bin}[3:0]\} \]

- `sub_frame=0`：长 PRI 的 bin 0..15，对应打包值 0..15。
- `sub_frame=1`：短 PRI 的 bin 0..15，对应打包值 16..31。

这样一个 5 位端口就能同时表达「属于哪个子帧」和「子帧内的 bin 编号」。

#### 4.2.3 源码精读

文件头部的注释把整个架构讲得非常清楚，是本讲最重要的「说明书」：

[doppler_processor.v:3-33](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L3-L33) —— 重点看 L8-L12（为什么不能 32 点）、L14-L19（双子帧 + bin 打包）、L21-L23（staggered 解模糊）。

**累积阶段**：状态机在 `S_ACCUMULATE` 里数够 32 个 chirp，置 `frame_buffer_full`，然后跳到处理阶段：

[doppler_processor.v:262-283](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L262-L283) —— 当 `write_chirp_index` 数到 `CHIRPS_PER_FRAME-1=31`（L271），置 `frame_buffer_full<=1`，跳 `S_PRE_READ`，并把 `current_sub_frame<=0`（L279），从长 PRI 子帧开始处理。

**装载 FFT**：`S_LOAD_FFT` 把当前子帧的 16 个样本逐个喂给 `xfft_16`，并拉高 `fft_input_last` 标记最后一个：

[doppler_processor.v:292-310](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L292-L310) —— 计数器从 0 数到 `CHIRPS_PER_SUBFRAME+1=17`（含 2 个 BRAM 预充拍），到 17 时置 `fft_input_last<=1` 并转 `S_FFT_WAIT`。

**收集输出并打包**：`S_FFT_WAIT` 里每收到一个 `fft_output_valid`，就把它打包成 `{Q, I}` 的 32 位输出，并把 bin 编号按 `{sub_frame, bin[3:0]}` 写进 `doppler_bin`：

[doppler_processor.v:312-332](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L312-L332) —— 关键三行（L314-L318）：

```verilog
doppler_output <= {fft_output_q[15:0], fft_output_i[15:0]};
doppler_bin   <= {current_sub_frame, fft_sample_counter[3:0]};
range_bin     <= read_range_bin;
sub_frame     <= current_sub_frame;
```

**子帧/距离门轮转**：`S_OUTPUT` 决定下一步去哪——刚做完长 PRI 就切到短 PRI；两个子帧都做完就推进到下一个距离门；64 个距离门全做完就回 IDLE：

[doppler_processor.v:334-354](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L334-L354) —— `current_sub_frame==0` 时切到 1 并回 `S_PRE_READ`（L335-L339）；`current_sub_frame==1` 时若 `read_range_bin` 还没到 63 就继续、否则回 `S_IDLE` 并清 `frame_buffer_full`（L342-L352）。

#### 4.2.4 代码实践

**实践目标**：把「为什么双 16 而不是单 32」这条设计决策讲给别人听，并验证 bin 打包。

**操作步骤**：

1. 打开 `doppler_processor.v` 文件头注释（L3-L33），找到说明「单 32 点 FFT 非法」的那一句，抄下来。
2. 设某目标在长 PRI 子帧的 bin 4 出现、在短 PRI 子帧的 bin 9 出现。用打包公式 `{sub_frame, bin[3:0]}` 算出它在 `doppler_bin[4:0]` 上分别显示成哪两个十进制值。
3. 打开下游 `radar_system_top.v` 的 DC notch 注释，确认它对这两套 bin 编号的解读是否一致。

**需要观察的现象**：同一个目标在两个子帧里给出不同的 bin 值，这正是因为 PRI 不同——也是解模糊的依据。

**预期结果**：

- 长 PRI bin 4 → `{0, 0100}` = `00_0100` = 4。
- 短 PRI bin 9 → `{1, 1001}` = `1_1001` = 25。
- DC notch 的注释（见 4.4.3）确认子帧 0 的 DC 在 bin 0、子帧 1 的 DC 在 bin 16，与打包公式一致。

> 本实践为「源码阅读 + 手算」型，无需「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果未来把发射波形改成「32 个 chirp 全是均匀长 PRI」，Doppler 处理需要怎么改？

> **答**：既然采样变均匀了，理论上可以做单个 32 点 FFT（速度分辨率翻倍）。但要相应改 `DOPPLER_FFT_SIZE=32`、`CHIRPS_PER_SUBFRAME=32`、把 `xfft_16` 换成 32 点 FFT、并去掉 `sub_frame` 打包逻辑。代价是失去 staggered 解模糊能力。这就是「双 16」与「单 32」的取舍本质。

**练习 2**：`CHIRPS_PER_FRAME` 能不能设成 30（比如 15+15）而不改代码其它地方？

> **答**：不能随便改。模块注释明确要求 `CHIRPS_PER_FRAME` 必须是 32（每子帧 16）。`xfft_16` 是固定 16 点的（`N=16` 硬编码在 [xfft_16.v:38-39](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/xfft_16.v#L38-L39)），子帧点数必须等于 16。改 15 会破坏加窗表（16 项 Hamming）和 FFT 点数。

### 4.3 FFT 引擎：xfft_16 与 fft_engine

#### 4.3.1 概念说明

`doppler_processor` 自己不做蝶形运算，它只负责「调度」——把 16 个样本喂给一个叫 `xfft_16` 的模块，再把它吐出的 16 个 bin 收回来。真正算 FFT 的是 `xfft_16` 背后的 `fft_engine`。这三层是「调度器 → 接口适配 → 运算核」的关系：

- **`doppler_processor_optimized`**：调度。组织数据、加窗、数样本、打包输出。
- **`xfft_16`**：接口适配。把 `fft_engine` 包成 AXI-Stream 风格的接口（`tvalid`/`tlast`/`tready`），让上层不用关心 FFT 内部时序。
- **`fft_engine`**：运算核。可参数化的 radix-2 DIT（Decimation In Time）FFT/IFFT。

**什么是 radix-2 DIT FFT？** 直接算 N 点 DFT 要 \(O(N^2)\) 次复数乘法；radix-2 FFT 利用对称性把它降到 \(O(N \log_2 N)\)。对 N=16，就是 \(\log_2 16 = 4\) 个「级（stage）」，每级做 N/2 = 8 个「蝶形（butterfly）」。DIT 的特点是输入要按**位反转（bit-reverse）顺序**排列、输出是自然顺序。

**蝶形运算**：每个蝶形吃两个复数 \(a, b\)，乘上一个旋转因子 \(W\)，吐出：

\[
A = a + b \cdot W, \qquad B = a - b \cdot W
\]

旋转因子 \(W_N^k = e^{-j 2\pi k / N} = \cos(2\pi k/N) - j\sin(2\pi k/N)\)。N=16 时只有少数几个不同的 \(k\)，而且 cos/sin 有大量对称性，所以不必存全部 16 个复数。

**四分之一波长（quarter-wave）ROM**：利用 cos 的对称性，只存一个象限的值（N/4 = 4 个），靠符号变换和地址映射拼出全部 sin/cos。所以 `fft_twiddle_16.mem` 只有 4 行（见 4.3.3）。

#### 4.3.2 核心流程

`xfft_16` 内部是一个简单 FSM，把 AXI-Stream 流转成对 `fft_engine` 的「整存整取」调用：

```
S_IDLE  : 收到 config（forward/inverse），转 S_FEED
S_FEED  : 攒满 16 个输入样本到 in_buf，置 fft_start，转 S_WAIT
S_WAIT  : 把 in_buf 喂给 fft_engine（逐拍 din_valid），同时收 fft_engine 的输出存进 out_buf
          收到 fft_done 后转 S_OUTPUT
S_OUTPUT: 把 out_buf 里的 16 个结果按 AXI-Stream 吐给上层
```

`fft_engine` 内部则是经典的「单蝶形迭代」结构：

```
ST_LOAD    : 收 N 个样本，按位反转地址写入 BRAM
ST_BF_READ : 呈现两个蝶形操作数地址 + 寄存旋转因子索引
ST_BF_TW   : BRAM 数据有效，捕获操作数；查旋转因子 ROM，捕获 cos/sin
ST_BF_MULT2: DSP 做复数乘法，结果进 PREG
ST_BF_WRITE: 移位 + 加减 + 写回 BRAM
（以上 4 拍为一个蝶形；做完 N/2 个蝶形后 stage++，共 LOG2N 级）
ST_OUTPUT  : 顺序输出 N 个结果（IFFT 时额外做 1/N 缩放）
```

「READ → TW → MULT2 → WRITE」这个 4 拍流水是 `fft_engine` 注释里反复强调的设计，目的是把 BRAM 读、ROM 查表、DSP 乘法、写回各拆到一拍，避免长组合路径。

#### 4.3.3 源码精读

**xfft_16**：先看它如何封装接口、如何例化 `fft_engine`。

[xfft_16.v:3-12](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/xfft_16.v#L3-L12) —— 数据格式 `{Q[15:0], I[15:0]}`，`config tdata[0]` 的 `1=正向 FFT、0=逆向 FFT`。

[xfft_16.v:84-104](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/xfft_16.v#L84-L104) —— 例化 `fft_engine`，参数 `N=16, LOG2N=4, DATA_W=16, INTERNAL_W=32, TWIDDLE_W=16`，旋转因子文件 `"fft_twiddle_16.mem"`。注意上层 `doppler_processor` 调用时 `s_axis_config_tdata` 传的是 `8'h01`（[doppler_processor.v:518](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L518)），即正向 FFT。

> `xfft_16` 把 `s_axis_config_tdata[0]` 的含义**反相**后存进 `inverse_reg`（[xfft_16.v:171](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/xfft_16.v#L171) `inverse_reg <= ~s_axis_config_tdata[0]`）。所以上层传 `1` 表示「正向」、内部 `inverse_reg=0`，是一致的。

**fft_engine**：先看整体架构注释，再看蝶形地址与旋转因子这两个最巧妙的地方。

[fft_engine.v:3-29](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L3-L29) —— 整体说明：radix-2 DIT、单蝶形迭代、四分之一波长 ROM、4 拍蝶形流水。

[fft_engine.v:114-122](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L114-L122) —— `bit_reverse` 函数，DIT 要求输入按位反转排列。`ST_LOAD` 状态写入时就用 `bit_reverse(load_count)` 作地址（[fft_engine.v:275](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L275)），这样输出就是自然顺序，省掉输出端再做一次位反转。

[fft_engine.v:148-161](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L148-L161) —— 蝶形地址与旋转因子索引的组合计算。注意 `bf_tw_idx = idx_val << (LOG2N - 1 - stage)` 用**桶形移位**代替乘法求旋转因子步长——因为步长永远是 2 的幂，移位就够了，省掉一个乘法器。

[fft_engine.v:169-193](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L169-L193) —— 四分之一波长旋转因子查表。只读 `cos_rom`，靠 \(k=0\)、\(k=N/4\)、\(k<N/4\)、\(k>N/4\) 四种分支拼出 cos 与 sin 的正负号。例如 \(k > N/4\) 时 `cos = -cos_rom[N/2 - k]`，利用了 cos 在第二象限为负的对称性。

[fft_engine.v:233-240](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L233-L240) —— 蝶形的加减：`bf_sum = rd_a + (bf_prod >>> (TWIDDLE_W-1))`、`bf_dif = rd_a - (bf_prod >>> ...)`。注释强调这里的「移位」是纯走线（bit-select），后面接一个 32 位进位链加法器，约 3ns，没有额外逻辑层。

[fft_engine.v:693-705](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L693-L705) —— 蝶形复数乘法的核心（`ST_BF_MULT2` 状态）：

```verilog
bf_prod_re <= rd_b_re * rd_tw_cos + rd_b_im * rd_tw_sin;
bf_prod_im <= rd_b_im * rd_tw_cos - rd_b_re * rd_tw_sin;
```

这正是复数乘法 \((b_{re}+j b_{im})(\cos - j\sin)\) 的实虚部展开，`inverse` 分支只是翻转了交叉项的符号（对应 IFFT 用 \(+j\sin\)）。

**旋转因子 ROM**：只有 4 行，是「四分之一波长」最直观的证据：

[fft_twiddle_16.mem:1-9](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_twiddle_16.mem#L1-L9) —— `7FFF`（cos 0° = 1.0）、`7641`（cos 22.5°）、`5A82`（cos 45°）、`30FB`（cos 67.5°），都是 Q15 定点。第 5 个值 cos 90° = 0 不需要存（查表逻辑里 \(k=N/4\) 分支直接给 0）。

**加窗**：Hamming 窗是在 `doppler_processor` 里、在喂进 `xfft_16` **之前**乘上去的（不是在 FFT 内部）。窗系数存在一张 16 项表里：

[doppler_processor.v:76-106](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L76-L106) —— 16 点 Hamming 窗（Q15），公式 \(w[n] = 0.54 - 0.46\cos(2\pi n/15)\)，对称（`w[n]=w[15-n]`）。加窗的目的是压低有限截断造成的频谱泄漏，让 Doppler 峰更「干净」。`WINDOW_TYPE=0` 选 Hamming、`=1` 选矩形（全 `0x7FFF`）。乘法在 `S_LOAD_FFT` 的数据通路里完成（[doppler_processor.v:424-426](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L424-L426) 等），结果右移 15 位回到 Q15。

#### 4.3.4 代码实践

**实践目标**：用 iverilog 跑一遍 Doppler 协同仿真，亲眼看 RTL 输出与 Python 黄金参考对齐。

**操作步骤**：

1. 进入 FPGA 目录（路径含 `9_Firmware/9_2_FPGA/`）。
2. 用 testbench 头部给的编译命令编译（`-DSIMULATION` 让 `fft_engine`/`xfft_16` 走行为级模型而非 Xilinx XPM）：

   ```bash
   iverilog -g2001 -DSIMULATION \
     -o tb/tb_doppler_cosim.vvp \
     tb/tb_doppler_cosim.v doppler_processor.v xfft_16.v fft_engine.v
   vvp tb/tb_doppler_cosim.vvp
   ```

3. 改用运动目标场景再跑一次：

   ```bash
   iverilog -g2001 -DSIMULATION -DSCENARIO_MOVING \
     -o tb/tb_doppler_cosim_moving.vvp \
     tb/tb_doppler_cosim.v doppler_processor.v xfft_16.v fft_engine.v
   vvp tb/tb_doppler_cosim_moving.vvp
   ```

**需要观察的现象**：

- 静止场景：能量集中在 DC bin（子帧 0 的 bin 0、子帧 1 的 bin 16）。
- 运动场景：能量峰移到某个非零 bin，且在长/短两个子帧里落到**不同**的 bin 编号。

**预期结果**：testbench 会把 RTL 输出写成 CSV 并与 Python 黄金参考做 exact-match 比对，打印 PASS/FAIL 与输入/输出样本计数（应为 2048 进、2048 出）。具体数值与运行环境、输入 hex 有关，**待本地验证**。

> 若本机没装 iverilog，可退化为「源码阅读型实践」：打开 `tb/tb_doppler_cosim.v` 的头部注释（[tb_doppler_cosim.v:6-32](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_doppler_cosim.v#L6-L32)），确认它声明了「2048 进、2048 出、三套场景」的验证目标，并解释为什么运动场景能验证 staggered 解模糊。

#### 4.3.5 小练习与答案

**练习 1**：`fft_engine` 默认参数是 `N=1024`，但 `xfft_16` 把它实例化成 `N=16`。这种「大默认、小实例化」的设计有什么好处？

> **答**：`fft_engine` 是可参数化的通用核，默认值（1024）只是占位，真正的点数由上层实例化时覆盖（[xfft_16.v:85-87](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/xfft_16.v#L85-L87) 传 `N(16)`）。这样同一个核既能做 Doppler 的 16 点 FFT，也能复用给匹配滤波的 1024 点 FFT（见 u4-l2 的 `fft_engine` 引用），提高复用率。

**练习 2**：为什么 `fft_engine` 在 `ST_LOAD` 写入时就用位反转地址，而不是输出时再做位反转？

> **答**：DIT FFT 的输入需位反转、输出为自然序。在写入时即按位反转地址存放，运算全程在 BRAM 里用自然序地址流转，输出时就能直接按 `0,1,2,…,N-1` 顺序读出（[fft_engine.v:302-304](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fft_engine.v#L302-L304)），省掉输出端一次位反转遍历，时序更简单。

### 4.4 帧完成信号 frame_complete 与下游联动

#### 4.4.1 概念说明

Doppler 处理器不是孤立干活的——它每产出一个结果，下游的 DC notch 和 CFAR 都要同步知道。其中最重要的一个信号是 `frame_complete`：它告诉整个系统「一整张 Range-Doppler 图已经全部算完、可以开始检测了」。

为什么 CFAR 需要这个信号？因为 **CA-CFAR 是一种「在邻域上算平均」的检测器**——要给当前单元估一个本地噪声门限，它得看周围一圈训练单元的值。如果一帧的 Range-Doppler 数据还没攒齐就开始检测，CFAR 在图像边缘会读到不完整的邻域，门限估错、虚警率就乱套。`frame_complete` 就是 CFAR「等一帧凑齐再开工」的同步令牌。

同时，`doppler_bin` 的打包格式 `{sub_frame, bin[3:0]}` 在下游被 DC notch 用来精准清除零多普勒杂波——这一步夹在 Doppler 与 CFAR 之间，也依赖 `frame_complete` 建立的「一帧完整数据」语义。

#### 4.4.2 核心流程

信号在三个模块间的传递链：

```
doppler_processor_optimized.frame_complete          (doppler_processor.v)
        │  改名
        ▼
radar_receiver_final.doppler_frame_done             (radar_receiver_final.v:453)
        │  引到顶层
        ▼
radar_system_top.rx_frame_complete                  (radar_system_top.v:554)
        │  喂给 CFAR
        ▼
cfar_ca.frame_complete                              (radar_system_top.v:629, cfar_inst)
```

注意 `frame_complete` 在源码里的定义（`doppler_processor.v` 末尾）：

```verilog
assign frame_complete = (state == S_IDLE && frame_buffer_full == 0);
```

也就是说，它**不是处理完成那一刻的脉冲，而是「已经回到空闲、且缓存已清空、准备好接收下一帧」这个持续电平**。这种「电平型完成」让下游只要看到它为高，就知道当前数据集完整且稳定。

#### 4.4.3 源码精读

`frame_complete` 的产生：

[doppler_processor.v:530-534](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L530-L534) ——

```verilog
assign processing_active = (state != S_IDLE);
assign frame_complete    = (state == S_IDLE && frame_buffer_full == 0);
```

`processing_active` 与 `frame_complete` 互补：要么在处理（非 IDLE）、要么已完成且就绪（IDLE 且缓存空）。

在接收机顶层被改名引出：

[radar_receiver_final.v:452-454](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L452-L454) —— 例化时 `.frame_complete(doppler_frame_done)`、`.processing_active(doppler_processing)`、`.status()`（状态口悬空不接）。注意这里**没有连接 `sub_frame` 输出口**——模块注释说过这个口是「超集」，不接也能工作（[doppler_processor.v:25-28](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L25-L28)），下游靠 `doppler_bin` 的高位（`sub_frame`）就能区分两个子帧。

顶层把信号接到 CFAR，并在中间插入 DC notch：

[radar_system_top.v:620-629](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L620-L629) —— `cfar_inst` 例化，`.doppler_data(notched_doppler_data)`、`.frame_complete(rx_frame_complete)`。注意喂给 CFAR 的数据是 `notched_doppler_data`（经过 DC notch 的），不是原始 Doppler 输出。

DC notch 怎么用 `doppler_bin` 的打包格式：

[radar_system_top.v:578-601](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L578-L601) —— 注释清楚写了打包规则：子帧 0 的 DC 在 bin 0、子帧 1 的 DC 在 bin 16（L583-L585）。`bin_within_sf = dop_bin_unsigned[3:0]`（L592）取低 4 位得到「子帧内的 bin」，然后 `dc_notch_active`（L593-L595）判断这个 bin 是否落在 `±host_dc_notch_width` 范围内——**这一判断对两个子帧同时生效**，因为低 4 位对子帧 0 和子帧 1 是共享的。`notch_width=1` 清掉 bin {0, 16}、`=2` 清掉 {0,1,15,16,17,31}（L586-L587）。

> 为什么要在 Doppler 之后、CFAR 之前做 DC notch？因为零多普勒（静止地物）能量往往远强于运动目标，若不清除，CFAR 的本地平均会被它拉高，导致小目标漏检。DC notch 与 u4-l3 的 MTI 互补：MTI 在时域（Doppler FFT 之前）做一次粗滤，DC notch 在频域（Doppler FFT 之后）再补一刀。

#### 4.4.4 代码实践

**实践目标**：写一段判定脚本，在仿真或实采数据上检查「一帧的 Doppler 数据是否完整就绪」。

**操作步骤**：

1. 追踪 `frame_complete` 从产生到消费的全链路（三个文件、三处改名）。
2. 用伪代码写一个上位机/仿真检查器：在收到每个 `doppler_valid` 时累计 `(range_bin, doppler_bin)`，直到看到 `frame_complete` 拉高，此时验证：
   - 是否恰好收到 \(64 \times 32 = 2048\) 个有效样本；
   - `doppler_bin` 是否覆盖了 0..31 全部 32 个值（每个 range_bin 各一遍）。

**需要观察的现象**：`frame_complete` 拉高前，样本计数应正好达到 2048；少于 2048 就拉高说明丢包或上游时序异常。

**预期结果**：

```text
# 伪代码（示例代码，非项目原有）
count = 0
seen_bins = set()
on each (doppler_valid pulse):
    count += 1
    seen_bins.add(doppler_bin)
on frame_complete rising:
    assert count == 2048, f"frame incomplete: {count}"
    # 每个 range_bin 都应有 32 个不同 doppler_bin
    print("PASS" if count == 2048 else "FAIL")
```

具体计数值依赖实际仿真或硬件，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`frame_complete` 是电平（持续高）还是单拍脉冲？这对下游设计有什么影响？

> **答**：是电平——只要 `state==S_IDLE && frame_buffer_full==0` 就持续为高（[doppler_processor.v:534](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L534)）。这意味着下游若想要「单拍触发」必须自己做一个上升沿检测；若直接用电平，则在 `frame_complete` 为高的整段时间内都视作「数据就绪」。

**练习 2**：如果 DC notch 的 `host_dc_notch_width` 设为 0，会发生什么？

> **答**：`dc_notch_active` 恒为假（[radar_system_top.v:593](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L593) 的第一项 `host_dc_notch_width != 0` 为假），`notched_doppler_data` 直接等于原始 `rx_doppler_output`（L598），即完全透传、不清除任何 DC bin。这是「关闭 DC notch」的兼容档。

## 5. 综合实践

把本讲的四个最小模块串成一个端到端的小任务：**画一张 Doppler 处理的「数据生命周期图」并标注每一处的关键信号与行号**。

任务要求：

1. 从上游 MTI 送来一个 32 位 `{Q, I}` 样本开始（[radar_receiver_final.v:427-430](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L427-L430)），画出它经历的全部阶段：
   - 按 `(chirp, range)` 写入 BRAM（4.1 的转置）；
   - 攒够 32 chirp 触发处理（4.2 的 `frame_buffer_full`）；
   - 按 `(doppler_index, range)` 读出、Hamming 加窗、送 `xfft_16`（4.3）；
   - 经 `fft_engine` 蝶形运算得 16 个 bin；
   - 打包成 `doppler_bin={sub_frame, bin[3:0]}` 输出；
   - 经 DC notch 清零频（4.4）；
   - 连同 `frame_complete` 一起送进 CFAR。
2. 在图上至少标注：`MEM_DEPTH`、`mem_write_addr`/`mem_read_addr` 两条地址公式、`current_sub_frame` 的 0→1 切换点、`fft_input_last`、`frame_complete` 的产生式。
3. 写一段话回答本讲的核心问题：**为什么是双 16 点而不是单 32 点 FFT？** 要求同时给出「数学理由」（非均匀采样）和「工程收益」（staggered 解模糊）。

完成这张图后，你应当能不看源码、向别人讲清楚一个目标回波从「距离像里的一个像素」变成「Range-Doppler 图里一个被检测的亮点」的完整旅程。

## 6. 本讲小结

- **慢时间 FFT 测速**：把多个 chirp 的距离像堆成矩阵、沿 chirp 方向（慢时间）做 FFT，相位旋转变成 Doppler 频率峰，\(f_d = 2v_r/\lambda\)；硬件靠「按行写、按列读」的 BRAM 地址反排实现矩阵转置。
- **双 16 点而非单 32 点**：一帧的 32 个 chirp 是 long/short 交替的非均匀采样，单 32 点 FFT 数学非法；拆成两个 16 点子帧（各自均匀）才合法，同时获得 staggered-PRI 解速度模糊的能力。
- **三层 FFT 架构**：`doppler_processor`（调度+加窗）→ `xfft_16`（AXI-Stream 接口封装）→ `fft_engine`（radix-2 DIT 蝶形核，四分之一波长旋转因子 ROM，4 拍蝶形流水）。
- **bin 打包**：`doppler_bin[4:0] = {sub_frame, bin[3:0]}`，一个 5 位端口同时承载「子帧」与「子帧内 bin」，子帧 0 占 0..15、子帧 1 占 16..31。
- **帧完成联动**：`frame_complete`（电平，`state==S_IDLE && !frame_buffer_full`）经 `doppler_frame_done`→`rx_frame_complete` 传到 `cfar_inst`，告诉 CFAR 一整张 Range-Doppler 图已就绪；中间的 DC notch 用同样的 bin 打包格式精准清除零多普勒杂波。
- **文件名 ≠ 模块名**：`doppler_processor.v` 里的模块叫 `doppler_processor_optimized`，读源码与例化时都要留意。

## 7. 下一步学习建议

- **继续沿流水线向下**：下一讲 u4-l5 讲 **CFAR 目标检测**。Doppler 处理器输出的 `{range_bin, doppler_bin, magnitude}` 正是 CFAR 的输入，建议对照本讲的打包格式与 `frame_complete` 时序去读 `cfar_ca.v`。
- **回头看发射端**：本讲的 long/short chirp 来自 u5-l1 的 `plfm_chirp_controller`。学完 u5-l1 后再回来，你会更清楚 staggered PRI 是怎么从发射端一路决定到 Doppler 处理架构的。
- **验证体系**：如果想从测试角度巩固本讲，直接读 `tb/tb_doppler_cosim.v` 与 u11-l1（FPGA 回归与协同仿真），看「真实数据 exact-match 比对」如何保证这套双 16 点 FFT 的数值正确性。
- **形式化角度**：u14-l1 会用 SymbiYosys 对 `doppler_processor` 做形式化验证（`formal/fv_doppler_processor.sby`），证明它的状态机性质恒成立——这是仿真之外的另一层信心来源。
