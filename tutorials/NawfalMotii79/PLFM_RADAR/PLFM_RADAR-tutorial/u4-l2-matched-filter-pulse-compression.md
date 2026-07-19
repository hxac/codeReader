# 匹配滤波与脉冲压缩

## 1. 本讲目标

上一讲（u4-l1）我们讲完了数字下变频 DDC：它把 400 MSPS 的实 ADC 数据变成 100 MSPS 的复 I/Q 基带信号。但基带信号本身还「看不清」目标——发射出去的是一个长达几十微秒的 chirp（线性调频脉冲），回波也是一个长脉冲，多个距离上的回波会彼此重叠。

本讲解决一个核心问题：**如何把一个长 chirp 回波「压」成一个又窄又尖的峰，从而精确分辨不同距离上的目标。** 这就是脉冲压缩（Pulse Compression），它在 FPGA 里靠匹配滤波（Matched Filter）实现。

学完本讲你应该能：

1. 说清脉冲压缩为什么能兼顾「探测远」和「看得清」，以及匹配滤波在频域里到底做了什么运算。
2. 看懂 AERIS-10 是如何把参考 chirp 存进 BRAM、如何用时延缓冲对齐参考与回波、又为什么要把长 chirp 拆成「多段」做频域卷积。
3. 独立算出 `LATENCY=3187` 在 100 MHz 下对应多少微秒，并能解释这个延时的物理含义。

---

## 2. 前置知识

本讲假设你已经读过 u4-l1（DDC）并理解以下概念。我们先用最朴素的语言把它们再过一遍。

### 2.1 为什么要脉冲压缩

雷达探测距离由发射能量决定：脉冲越长（持续时间 \(T\)），能量越大，看得越远。但距离分辨率由脉冲宽度决定：

\[
\Delta R = \frac{c \cdot \tau}{2}
\]

其中 \(c\) 是光速，\(\tau\) 是脉冲宽度。脉冲越窄，分辨率越高。这就矛盾了：**要看得远就要长脉冲，要看得清就要窄脉冲。**

chirp（线性调频，LFM）破解了这个矛盾：让频率在一个长脉冲内线性扫过一段带宽 \(B\)。这样脉冲既长（能量足）又「宽频带」（信息量大）。匹配滤波把这段长 chirp 压缩回一个窄峰，压缩后的有效脉宽近似为：

\[
\tau_{\text{eff}} \approx \frac{1}{B}
\]

于是分辨率变成：

\[
\Delta R = \frac{c}{2B}
\]

**只由带宽 \(B\) 决定，与脉冲长度无关。** 这就是脉冲压缩的本质收益。压缩比（脉压增益）约等于时间-带宽积 \(T \cdot B\)。

### 2.2 匹配滤波是什么

「匹配滤波器」是使输出信噪比最大的一种滤波器，它的冲激响应是已知信号 \(s(t)\) 的时间反转取共轭：

\[
h(t) = s^{*}(T - t)
\]

在频域里这件事变得特别简单。设回波 \(x(t)\) 的傅里叶变换是 \(X(k)\)，参考 chirp \(s(t)\) 的是 \(S(k)\)，那么匹配滤波输出：

\[
Y(k) = X(k) \cdot S^{*}(k)
\]

也就是：**回波做 FFT、参考做 FFT、两者相乘时参考取共轭、再做 IFFT 回时域。** 取共轭这一步对应「相关」（correlation），这正是匹配滤波在数学上的真身。本讲讲到的 `matched_filter_processing_chain` 就是干这四件事。

### 2.3 为什么要在频域做

直接时域卷积 \(x * h\) 需要约 \(N \times L\) 次乘法（\(N\) 信号长，\(L\) 滤波器长）；而 FFT + 频域相乘 + IFFT 只要 \(O(N \log N)\)。当参考 chirp 很长（几千点）时，频域实现快得多，这就是 FPGA 用 FFT 引擎的原因。

### 2.4 循环卷积与分块卷积

一个 \(N\)-点 FFT 算出来的是**循环卷积**，而我们要的是**线性卷积**。当信号和滤波器都很长、超过单个 FFT 长度时，必须把信号切成一块一块分别做 FFT 卷积再拼接——这叫**分块卷积**。本讲的「多段（multi-segment）」正是这个意思，用的是其中的 overlap-save（重叠保留）法。

> 名词速查：BRAM（块 RAM，FPGA 片内存储）、FFT（快速傅里叶变换）、IFFT（逆 FFT）、I/Q（同相/正交两路基带）、\(S^{*}\)（共轭）。

---

## 3. 本讲源码地图

本讲涉及四个 Verilog 文件，它们在接收链 `radar_receiver_final.v` 里被串成一条「参考 chirp 存取 → 时延对齐 → 频域匹配滤波」的流水线：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `chirp_memory_loader_param.v` | 把参考 chirp 从 `.mem` 文件加载进 BRAM，按段号/地址输出参考 I/Q | 模块 4.1：参考 chirp 存储 |
| `latency_buffer.v` | BRAM 实现的参数化延时线，把参考延迟 `LATENCY` 个时钟周期 | 模块 4.2：时延对齐 |
| `matched_filter_multi_segment.v` | 匹配滤波的顶层控制器：攒数据、分段、驱动 FFT 链、做 overlap-save | 模块 4.3：频域匹配滤波（控制） |
| `matched_filter_processing_chain.v` | 真正做 FFT→共轭乘→IFFT 的运算核（仿真用行为级 FFT，综合用 IP） | 模块 4.3：频域匹配滤波（运算） |

三个模块在 `radar_receiver_final.v` 里的连接顺序是：

```
.mem 文件 ─▶ chirp_memory_loader_param ─▶ latency_buffer(延迟3187) ─▶ matched_filter_multi_segment ─▶ 距离像(range profile)
                  ▲                                                        │
                  └──────── segment_request / mem_request / addr ◀─────────┘
```

匹配滤波器一边收「回波」（来自 DDC+增益控制的 `ddc_i/ddc_q`），一边按需向存储器要「参考」，两者在 FFT 链入口对齐后做频域卷积，输出距离像。

---

## 4. 核心概念与源码讲解

### 4.1 参考 chirp 存储：chirp_memory_loader_param

#### 4.1.1 概念说明

匹配滤波需要一个「参考波形」\(s(t)\)——理论上它应该等于发射出去的那个 chirp。AERIS-10 的做法是：**离线把理想 chirp 算好，存成一堆 `.mem` 十六进制文件，上电时由 FPGA 读进 BRAM。** 这样滤波系数固定、可复现，且不占用运行时算力。

为什么长 chirp 要存成多个文件？因为单个 chirp 有几千个采样点，而匹配滤波的 FFT 块只有 1024 点，所以参考也按 1024 一段来切分存储（`seg0`–`seg3`），与处理时分段一一对应。

#### 4.1.2 核心流程

1. 上电 `initial` 块用 `$readmemh` 把 4 段长 chirp I/Q 装进 `long_chirp_i/q[0:4095]`，每段 1024。
2. 短 chirp 只有 50 个样，装进 `short_chirp_i/q[0:1023]` 的前 50 项，其余补零。
3. 运行时由 `segment_select`（段号）+ `sample_addr`（段内地址）拼成 12 位地址，从 BRAM 同步读出参考 `ref_i/ref_q`。
4. `mem_request` 触发一次读取，`mem_ready` 在下一拍拉高表示数据就绪。

#### 4.1.3 源码精读

文件用参数声明所有 `.mem` 文件名，方便不同板卡换波形：

[chirp_memory_loader_param.v:2-13](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/chirp_memory_loader_param.v#L2-L13) —— 用 `parameter` 列出 4 段长 chirp 与 1 段短 chirp 的 I/Q 文件名，默认指向 `long_chirp_seg0_i.mem` 等。

存储体声明为 BRAM（`ram_style = "block"`），长 chirp 共 4096 项（4×1024），短 chirp 1024 项：

[chirp_memory_loader_param.v:27-30](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/chirp_memory_loader_param.v#L27-L30) —— 声明两块 4096 深、16 位宽的长 chirp BRAM 和两块 1024 深的短 chirp BRAM，`(* ram_style = "block" *)` 引导综合器使用 Block RAM。

加载逻辑把 4 段分别填到地址 `0-1023`、`1024-2047`、`2048-3071`、`3072-4095`：

[chirp_memory_loader_param.v:44-69](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/chirp_memory_loader_param.v#L44-L69) —— 四次 `$readmemh` 依次把 `SEG0..SEG3` 装进连续地址段；这正是「分段存储」的体现，每段 1024 点。

短 chirp 只读 50 个有效样，其余显式补零（注释说这样做是为了避免 iverilog 的「字数不足」告警）：

[chirp_memory_loader_param.v:74-84](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/chirp_memory_loader_param.v#L74-L84) —— `$readmemh(..., 0, 49)` 只装前 50 项，`for` 循环把 50–1023 补零。

运行时寻址用拼接：`{segment_select, sample_addr}`，段号占高 2 位，段内地址占低 10 位：

[chirp_memory_loader_param.v:108](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/chirp_memory_loader_param.v#L108) —— `wire [11:0] long_addr = {segment_select, sample_addr};` 组合逻辑拼出 12 位地址，按 `use_long_chirp` 选择长/短 chirp 读出。

> 工程细节：读 BRAM 的 `always` 块用**同步复位**（`posedge clk`），而非异步复位。注释里写明原因——Xilinx BRAM 的输出寄存器不支持异步复位，强行写会触发综合告警（`Synth 8-3391`），后续 `latency_buffer.v` 也遵守同一约定。

#### 4.1.4 代码实践

**实践目标**：确认长 chirp 参考的「4 段 × 1024」结构是真实存在的，而不是代码注释的夸大。

**操作步骤**：

1. 进入 `9_Firmware/9_2_FPGA/` 目录。
2. 用 `wc -l long_chirp_seg0_i.mem long_chirp_seg1_i.mem long_chirp_seg2_i.mem long_chirp_seg3_i.mem short_chirp_i.mem` 统计每个文件的行数。
3. 打开任一 `long_chirp_seg*_i.mem`，确认每行是一个 4 位十六进制数（16 位 Q15 定点）。

**需要观察的现象**：四个长 chirp I 文件**各 1024 行**，共 4096 行；`short_chirp_i.mem` 只有 **50 行**（对应代码里「前 50 项有效、其余补零」）。

**预期结果**：长 chirp = 4 × 1024 = 4096 样点容量；短 chirp = 50 样点。这与你刚读到的 `$readmemh(..., 0, 1023)` 与 `$readmemh(..., 0, 49)` 完全吻合。

> 待本地验证：若你手头没有完整克隆仓库，可只在 GitHub 网页上点开这些 `.mem` 文件查看行数。

#### 4.1.5 小练习与答案

**练习 1**：`segment_select` 是 2 位、`sample_addr` 是 10 位，为什么长 chirp 地址是 12 位？最多能寻址多少样点？
**答案**：拼接成 `{segment_select[1:0], sample_addr[9:0]}` 共 12 位，可寻址 \(2^{12}=4096\) 样点，正好覆盖 4 段 × 1024。

**练习 2**：短 chirp 只有 50 个样，为什么要分配 1024 项的数组？
**答案**：因为匹配滤波的 FFT 块固定是 1024 点，短 chirp 也要凑满一个 1024 点的块才能送进 FFT，所以多余的位置补零。

---

### 4.2 时延对齐：latency_buffer

#### 4.2.1 概念说明

匹配滤波要把「回波」和「参考」**逐样本地、同时**喂进 FFT 链。但这两条路到达匹配滤波器的时间不一样：

- **回波**：ADC → DDC（NCO/CIC/FIR）→ 增益控制，经过一长串流水线寄存器后才到达 `ddc_i/ddc_q`。
- **参考**：从 BRAM 读出来几乎立刻可用。

如果直接把「此刻」读到的参考和「此刻」的回波配对，两者会错开几百上千拍——回波对应的是几微秒前发射的 chirp，参考却是当下正在读的那段。**结果就是相关峰对不上，距离像完全错位。**

解决办法：给参考加一条延时线，把它**往后推固定拍数**，推到正好和回波对齐。`latency_buffer` 就是这条延时线。

#### 4.2.2 核心流程

延时线本质是一个**环形缓冲（FIFO）**：

1. 每来一个 `valid_in` 样本，写入 `write_ptr` 指向的 BRAM 单元，`write_ptr++`（到 4095 回绕）。
2. 用 `delay_counter` 数到 `LATENCY` 个样本后，置 `buffer_has_data`，表示「缓冲已灌满、可以开始读了」。
3. 此后读指针恒等于 `read_ptr = (write_ptr - LATENCY) mod 4096`，即**读出恰好 `LATENCY` 拍之前写入的那个样本**。
4. BRAM 读出再寄存一拍（`data_out_reg`），并用一个 `valid_out_pipe` 把有效信号也顺延一拍对齐。

#### 4.2.3 源码精读

模块参数化，默认 `DATA_WIDTH=32`（16 位 I + 16 位 Q 打包）、`LATENCY=3187`：

[latency_buffer.v:6-16](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/latency_buffer.v#L6-L16) —— 模块声明与默认参数。文件头注释明确写：「由 `latency_buffer_2159` 改名为 `latency_buffer`，因为模块名与实际的 `LATENCY=3187` 参数不一致。」**这是一个重要信号：老名字里的 2159 已经过时，真实延时是 3187。**

写侧 + 灌满计数：

[latency_buffer.v:66-84](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/latency_buffer.v#L66-L84) —— 每写入一样本 `write_ptr` 自增并在 4095 回绕；`delay_counter` 累加到 `LATENCY-1` 时把 `buffer_has_data` 拉高，表示延时线已「充满」，之后读侧才开始输出。

读侧的核心算式 `read_ptr = (write_ptr - LATENCY) mod 4096`：

[latency_buffer.v:86-102](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/latency_buffer.v#L86-L102) —— 当缓冲有数据且仍在写入时，按 `write_ptr` 是否 ≥ `LATENCY` 分别处理回绕，算出读指针并拉高 `valid_out_reg`。这一段就是「读出 `LATENCY` 拍前的样本」的数学实现。

> 注意：调用方在 [radar_receiver_final.v:301-311](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L301-L311) 实例化它，**注释写的是「2159 cycle delay」，但 `.LATENCY(3187)` 才是真实值**。这印证了模块头注释说的「名字与参数不一致」。读源码时要以**参数实例化**为准，而不是上方注释——注释会随调参而滞后。

BRAM 读出寄存 + 有效信号再顺延一拍：

[latency_buffer.v:106-127](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/latency_buffer.v#L106-L127) —— `data_out_reg <= bram[read_ptr]` 把读出寄存一拍（Xilinx BRAM 物理上就是寄存输出，强写异步读会退化成 LUTRAM、浪费约 704 个 LUT）；`valid_out_pipe` 跟着顺延一拍，保证 `valid_out` 与 `data_out` 严格对齐。

#### 4.2.4 代码实践（本讲指定计算题）

**实践目标**：算出 `LATENCY=3187` 在 100 MHz 时钟下对应多少微秒，并理解它代表什么。

**计算**：

- 100 MHz 时钟周期 \(T_{\text{clk}} = 1 / 10^{8}\,\text{s} = 10\,\text{ns}\)。
- 总延时 \(= 3187 \times 10\,\text{ns} = 31{,}870\,\text{ns} = 31.87\,\mu\text{s}\)。

**结论**：参考 chirp 被整体延迟 **约 31.87 微秒**后才送进匹配滤波器。

**物理含义**：这段时间大致等于回波从 ADC 走到匹配滤波入口所累积的流水线深度（DDC 的 CIC/FIR 各级 + 增益控制寄存器 + 攒满一个 1024 点处理块所需的填充时间 + BRAM 读延迟）。只有把参考也推迟同样的时间，参考的第 \(n\) 个样才能和回波的第 \(n\) 个样在 FFT 链入口「会合」。

**需要观察的现象**（源码阅读型）：在仿真波形里，给 `data_in` 喂一个有明显特征的样本（例如一个尖峰），数一下它从 `data_in` 出现到 `data_out` 出现之间相隔多少个 `clk` 上升沿。

**预期结果**：间隔恰好 = `LATENCY`（3187）+ BRAM 读寄存 1 拍 + `valid_out_pipe` 1 拍，即数据值滞后约 3189 拍出现，而 `valid_out` 与数据同步对齐。

> 待本地验证：精确的「+2 拍」是否计入 `LATENCY` 的语义，取决于上游对齐要求；若你跑 iverilog 仿真，可在 testbench 里打印 `$time` 自行核对。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `LATENCY` 改成 100，环形缓冲深度仍是 4096，功能还正常吗？为什么默认要 3187？
**答案**：只要 `LATENCY ≤ 4096`，环形缓冲逻辑都正常；3187 是为了精确补偿本系统回波通路的具体流水线延迟，不是随便取的——改小会导致参考与回波错位、距离像偏移。

**练习 2**：为什么 `bram` 写和读要分成两个 `always` 块，且都不带异步复位？
**答案**：Xilinx BRAM 不支持异步复位；把存储写/读单独放进 `posedge clk` 块、复位只作用于控制寄存器，才能让综合器推断出真正的 Block RAM（避免 `Synth 8-3391` 告警和资源浪费）。

---

### 4.3 频域匹配滤波：matched_filter_multi_segment + matched_filter_processing_chain

#### 4.3.1 概念说明

有了对齐好的「回波」和「参考」，剩下就是做匹配滤波。前面说过频域实现最快：

\[
Y(k) = X(k) \cdot S^{*}(k) \quad\Longleftrightarrow\quad y = \text{IFFT}\big(\text{FFT}(x) \cdot \text{conj}(\text{FFT}(s))\big)
\]

但这里有个工程难点：长 chirp 大约 3000 样，而单个 FFT 块只有 1024 点。一次 1024 点 FFT 算出来的是循环卷积，直接用会把首尾卷绕、产生伪峰。**必须分块做，这就是「multi-segment（多段）」的全部含义。**

本系统用 **overlap-save（重叠保留）** 分块卷积：

- 把输入切成一块块 1024 样，相邻块之间**重叠 128 样**。
- 每块只产生 896 个「干净」输出（= 1024 − 128），块首 128 个是循环卷绕污染区，丢弃。
- 下一块把这 128 个重叠样重新放回块首，再接 896 个新样，继续。

长 chirp 大约要 4 块（`LONG_SEGMENTS = 4`），短 chirp 50 样补零成一块就够（`SHORT_SEGMENTS = 1`）。两个文件分工：

- `matched_filter_multi_segment.v`：**控制**——攒数据、切段、求参考、overlap-save 的搬运、驱动 FFT 链。
- `matched_filter_processing_chain.v`：**运算**——真正跑 FFT→共轭乘→IFFT。

#### 4.3.2 核心流程

`matched_filter_multi_segment` 的状态机（节选）：

```
ST_IDLE          等待 mc_new_chirp 脉冲，决定本次是长/短 chirp、共几段
   │
ST_COLLECT_DATA  攒 1024 个回波样进 input_buffer（长 chirp 段首含 128 重叠）
   │  (短 chirp 攒满 50 即转 ST_ZERO_PAD 补零到 1024)
   │  (长 chirp 攒满 1024 转 ST_WAIT_REF；若已到 LONG_CHIRP_SAMPLES 转补零)
ST_ZERO_PAD      不足 1024 的余量补零
   │
ST_WAIT_REF      拉高 mem_request，等 chirp_memory_loader 给出本段参考
   │
ST_PROCESSING    逐点把回波+参考喂进 FFT 链；同时缓存末尾 128 样到 overlap_cache
   │
ST_WAIT_FFT      等 FFT 链把 1024 点距离像全部吐完并回到 idle
   │
ST_OUTPUT        锁存本段输出 pc_i/pc_q
   │
ST_NEXT_SEGMENT  段号++，长 chirp 转 ST_OVERLAP_COPY，短 chirp 直接结束
   │
ST_OVERLAP_COPY  把 overlap_cache 的 128 样写回 buffer[0..127]，回到 ST_COLLECT_DATA
```

`matched_filter_processing_chain` 内部状态机（仿真路径）：

```
IDLE → FWD_FFT(攒1024+位反转) → FWD_BUTTERFLY(回波FFT) →
REF_BITREV → REF_BUTTERFLY(参考FFT) → MULTIPLY(共轭乘) →
INV_BITREV → INV_BUTTERFLY(IFFT+1/N缩放) → OUTPUT(流式吐1024点) → DONE → IDLE
```

#### 4.3.3 源码精读

先看控制器参数，这是「多段」的根源：

[matched_filter_multi_segment.v:42-53](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L42-L53) —— 关键参数：`BUFFER_SIZE=1024`（FFT 块）、`LONG_CHIRP_SAMPLES=3000`、`SHORT_CHIRP_SAMPLES=50`（注释「0.5µs @ 100MHz」）、`OVERLAP_SAMPLES=128`、`SEGMENT_ADVANCE=1024-128=896`、`LONG_SEGMENTS=4`、`SHORT_SEGMENTS=1`。注释里给出分段算式：\(\lceil(3072-128)/896\rceil = 4\)。

短 chirp 长度换算：50 样 / 100 MHz = 0.5 µs，与注释一致；长 chirp 3000 样 / 100 MHz = 30 µs。

状态编码：

[matched_filter_multi_segment.v:79-87](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L79-L87) —— 9 个状态：IDLE/COLLECT_DATA/ZERO_PAD/WAIT_REF/PROCESSING/WAIT_FFT/OUTPUT/NEXT_SEGMENT/OVERLAP_COPY。

`ST_IDLE` 根据 `use_long_chirp` 决定本次要处理几段：

[matched_filter_multi_segment.v:207-218](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L207-L218) —— 收到 `chirp_start_pulse` 后，`total_segments <= use_long_chirp ? LONG_SEGMENTS : SHORT_SEGMENTS`，即长 chirp 跑 4 段、短 chirp 跑 1 段。

`ST_COLLECT_DATA` 里 overlap-save 的关键注释与判断：

[matched_filter_multi_segment.v:264-292](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L264-L292) —— 注释说明：段 0 写满 1024 个新样；段 1+ 因 `write_ptr` 从 `OVERLAP_SAMPLES(128)` 起步，只需再收 896 个新样即可填满 1024。`buffer_write_ptr >= BUFFER_SIZE` 时转 `ST_WAIT_REF` 并请求本段参考；`chirp_samples_collected >= LONG_CHIRP_SAMPLES` 时标记 `chirp_complete` 并对不足部分补零。

在 `ST_PROCESSING` 里，一边把回波喂进 FFT 链，一边缓存段尾 128 样供下一段重叠：

[matched_filter_multi_segment.v:348-351](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L348-L351) —— 当 `buffer_read_ptr >= SEGMENT_ADVANCE` 时，把当前样存进 `overlap_cache`（共 128 项），这正是下一段要写回块首的重叠数据。

`ST_OVERLAP_COPY` 把缓存写回块首，完成 overlap-save 闭环：

[matched_filter_multi_segment.v:454-478](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L454-L478) —— 每拍写一个 `overlap_cache` 样到 `buffer[overlap_copy_count]`，写满 128 后把 `buffer_write_ptr` 设为 `OVERLAP_SAMPLES`，回到 `ST_COLLECT_DATA` 继续收 896 个新样。

控制器例化运算核 `matched_filter_processing_chain`，把回波 `fft_input_i/q` 和（对齐后的）参考 `long_chirp_real/imag` 一起送进去，取回距离像：

[matched_filter_multi_segment.v:491-516](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L491-L516) —— 例化 `m_f_p_c`，输入 `adc_data_i/q = fft_input_i/q`、参考 `long/short_chirp_real/imag`，输出 `range_profile_i/q/valid`。

再看运算核。文件头注释一语道破它做的事：

[matched_filter_processing_chain.v:6-10](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_processing_chain.v#L6-L10) —— 「Implements: FFT(signal) → FFT(reference) → Conjugate multiply → IFFT」，正是本讲 §2.2 的频域匹配滤波公式。

共轭乘的实现——注意它取的是参考的共轭：

[matched_filter_processing_chain.v:355-388](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_processing_chain.v#L355-L388) —— 注释给出复数乘法公式 \((a+jb)(c-jd) = (ac+bd) + j(bc-ad)\)，其中 `c+jd` 是参考 FFT 结果，取共轭即 `c-jd`；实部 `ac+bd`、虚部 `bc-ad`，结果饱和到 16 位。这一步对应 \(X(k)\cdot S^{*}(k)\)。

IFFT 后做 \(1/N\) 缩放（右移 \(\log_2 1024 = 10\) 位）：

[matched_filter_processing_chain.v:440-459](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_processing_chain.v#L440-L459) —— `scaled_re = work_re[i] >>> ADDR_BITS`（`ADDR_BITS=10`），完成 IFFT 必需的 \(1/N\) 归一化，再饱和到 16 位存入 `ifft_out`。

> 重要的工程结构：这个文件用 `` `ifdef SIMULATION `` 提供了两套实现。
> [matched_filter_processing_chain.v:530-553](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_processing_chain.v#L530-L553) 的综合路径用 `fft_engine`（复用 3 次：回波 FFT、参考 FFT、乘积 IFFT）+ `frequency_matched_filter`（4 级流水共轭乘）替代仿真用的行为级 FFT。这样做的好处是：iverilog 仿真不依赖 Xilinx 付费 IP 也能跑通整条链（详见 u11-l1 的 FPGA 回归与 cosim），而上 Vivado 综合时换成真正的 IP 核。这也是为什么前面看到那么多 BRAM 端口与 `*_primed` 标志——它们处理 BRAM 读出 1 拍延迟。

#### 4.3.4 代码实践（本讲指定问题）

**实践目标**：解释 `matched_filter_multi_segment` 为什么是「多段」的，把概念与代码参数对上。

**操作步骤**：

1. 读 [matched_filter_multi_segment.v:42-53](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_multi_segment.v#L42-L53)，记下 `LONG_CHIRP_SAMPLES`、`BUFFER_SIZE`、`OVERLAP_SAMPLES`、`SEGMENT_ADVANCE`、`LONG_SEGMENTS` 的值。
2. 用 overlap-save 公式自己算一遍：长 chirp 约 3000 样，块长 1024、重叠 128、步进 896，需要几块？
3. 对照 `chirp_memory_loader_param` 的 4 个分段 `.mem` 文件，确认「分段存储」与「分段处理」是一一对应的。

**分析与预期结果**：

- **为什么多段**：长 chirp（约 3000 样）远长于 FFT 块长 1024，单块 FFT 只能给循环卷积、首尾卷绕出错。必须分块用 overlap-save 做线性卷积：每块 1024 样含 128 个与上块的重叠，贡献 896 个干净输出。块数 \(=\lceil(3000-128)/896\rceil = \lceil 3.2\rceil = 4\)，与 `LONG_SEGMENTS=4` 一致；这也是参考 chirp 被切成 `seg0..seg3` 四个 `.mem` 文件的原因——**存储分段是为处理分段服务的**。
- **短 chirp 为什么一段**：短 chirp 只有 50 样，补零到一个 1024 块就够，所以 `SHORT_SEGMENTS=1`，对应 `short_chirp_i.mem` 单文件。
- **结论**：「multi-segment」不是性能优化，而是**线性卷积对 FFT 块长的数学约束**在工程上的直接体现。

> 待本地验证：可在 `9_Firmware/9_2_FPGA/` 下跑 `./run_regression.sh --quick`（见 u11-l1），观察 `tb_fullchain_realdata` 这类全链 testbench 是否能 exact-match 通过，从而间接验证分段 overlap-save 的正确性。

#### 4.3.5 小练习与答案

**练习 1**：如果要让长 chirp 支持到 6000 样（其他参数不变），`LONG_SEGMENTS` 应该改成多少？
**答案**：\(\lceil(6000-128)/896\rceil = \lceil 6.56\rceil = 7\)，且参考存储也要相应扩到 7 段 `.mem`、`long_chirp_i/q` 深度要 ≥ 7168。

**练习 2**：共轭乘那一步如果把参考的共轭写成「不取共轭」（直接 \((a+jb)(c+jd)\）），输出会变成什么？
**答案**：那就变成普通卷积而非相关，匹配滤波峰不会出现在正确距离上、甚至可能出现在镜像位置；取共轭对应「时间反转」，是把卷积变成相关的关键。

**练习 3**：为什么 IFFT 之后必须右移 10 位（\(1/N\)）？
**答案**：正变换 FFT 通常不带 \(1/N\)，逆变换 IFFT 的定义里含 \(1/N\)；若不在 IFFT 后除以 \(N=1024\)（右移 10），输出幅度会被放大 1024 倍而溢出 16 位。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，追踪「一个参考样本」从 `.mem` 文件一路走到距离像，并把延时对齐算清楚。

请按顺序完成并记录：

1. **存储**：打开 `9_Firmware/9_2_FPGA/long_chirp_seg0_i.mem`，挑出第 0 行的十六进制值，说明它会被 `chirp_memory_loader_param` 装到 `long_chirp_i[0]`，并在 `segment_select=0, sample_addr=0` 时经 [chirp_memory_loader_param.v:108](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/chirp_memory_loader_param.v#L108) 的 `{segment_select, sample_addr}` 寻址读出为 `ref_i`。

2. **延时**：这个 `ref_i` 接着进入 [radar_receiver_final.v:301-311](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L301-L311) 的 `latency_buffer`。写出它会被延迟多少拍（3187）、换算成多少微秒（**31.87 µs**），并解释这个数字代表回波通路的总流水线延迟。

3. **对齐**：延迟后的 `delayed_ref_i/q` 被接到 `matched_filter_multi_segment` 的 `long_chirp_real/imag` 输入（见 [radar_receiver_final.v:314-317](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L314-L317)）。说明此时它与同时到达的回波 `ddc_i/ddc_q` 在 `matched_filter_processing_chain` 入口逐样对齐。

4. **压缩**：两者在 [matched_filter_processing_chain.v:355-388](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/matched_filter_processing_chain.v#L355-L388) 做共轭乘、再 IFFT，得到距离像 `range_profile_i/q`。用一句话说明：为什么这条流水线的最终输出能让一个 30 µs 的长 chirp 变成能分辨 \( \Delta R = c/2B \) 的窄峰。

5. **多段**：最后回答本讲核心问题——长 chirp 为什么要分 4 段、短 chirp 为什么只 1 段（用 896/128 的 overlap-save 步进算给出来）。

**交付物**：一张包含「样本值 → 地址 → 延时拍数 → 延时微秒 → 输出含义」的小表格，加一段 200 字以内的追踪说明。

> 待本地验证：第 1 步的精确十六进制值需打开实际 `.mem` 文件查看；若仅看 GitHub 网页版，可直接读首行。

---

## 6. 本讲小结

- **脉冲压缩**用匹配滤波把长 chirp 压成窄峰，让雷达既看得远（长脉冲=大能量）又看得清（分辨率 \(\Delta R = c/2B\) 只由带宽决定），脉压增益约 \(T \cdot B\)。
- **频域匹配滤波**的四步运算是 `FFT(x) → FFT(s) → 共轭乘 X(k)·S*(k) → IFFT`，比时域卷积快得多。
- **参考存储**（`chirp_memory_loader_param`）把理想 chirp 离线存成 4 段 ×1024 的 `.mem`，运行时按 `{segment_select, sample_addr}` 从 BRAM 同步读出；短 chirp 50 样补零成一块。
- **时延对齐**（`latency_buffer`）是环形缓冲延时线，把参考推迟 `LATENCY=3187` 拍（100 MHz 下 = **31.87 µs**）以匹配回波通路延迟；模块名残留的「2159」已过时，真实值看参数实例化。
- **多段（overlap-save）**是因为长 chirp（~3000 样）超过 1024 点 FFT 块：每块 1024 含 128 重叠、贡献 896 干净输出，共 4 段（`LONG_SEGMENTS=4`）；短 chirp 一段（`SHORT_SEGMENTS=1`）。存储分段与处理分段一一对应。
- **仿真/综合双实现**：`matched_filter_processing_chain` 用 `` `ifdef SIMULATION `` 在行为级 FFT（iverilog 可跑）与 `fft_engine`/`frequency_matched_filter` IP（Vivado 综合）间切换，这是全链 cosim 能脱离付费 IP 跑通的关键。

---

## 7. 下一步学习建议

- **下一讲 u4-l3（距离抽取与 MTI）**：距离像是 1024 个距离门，后续 `range_bin_decimator` 会用峰值检测把它压成 64 个门，`mti_canceller` 做二脉冲对消滤掉静止杂波。建议带着「距离像的每个样本对应一个距离门」的认知去读。
- **横向回顾 u4-l1（DDC）**：本讲的「回波通路延迟 3187 拍」正是由 DDC 的 CIC/FIR 各级 + 增益控制累积出来的，回看 DDC 能帮你理解为什么延时是这么大一个数。
- **深入发射端 u5-l1**：本讲处理的「参考 chirp」来自 `.mem`，而真正发射出去的 chirp 由 `plfm_chirp_controller` + `dac_interface_single` 从 `long_chirp_lut.mem`（3600 样）生成；对比「参考」与「发射」两套波形数据，能完整理解 chirp 的发/收对称性。
- **验证角度 u11-l1**：`run_regression.sh` 的「真实数据 exact-match cosim」会拿真实回波 hex 与本讲匹配滤波的输出做逐位比对，是检验脉压正确性的最高标准，学完本讲后再去看 cosim 会非常顺。
