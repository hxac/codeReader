# OFDM 解码流水线总览

## 1. 本讲目标

上一篇讲义（u1-l4）我们把 `dot11.v` 的**对外接口与时序**看了一遍：进来的什么样（32 位 I/Q 样本＋strobe）、出去的什么样（字节＋FCS），以及 100 MHz 时钟与 20 MSPS 采样的 5:1 关系。但接口只是「外壳」，本讲要打开这个外壳，回答一个最关键的问题：

> **一个 I/Q 样本从 `sample_in` 进来，到底经过了哪些模块、走了什么顺序，才变成 `byte_out` 出去？**

学完本讲，你应该能够：

1. 背出 802.11 OFDM 解码的 **8 步概念流水线**（检测→频偏→FFT→信道估计→解调→解交织→卷积解码→解扰）。
2. 在 `dot11.v` 中**定位**每一步对应的子模块实例（`power_trigger` / `sync_short` / `sync_long` / `equalizer` / `ofdm_decoder`），画出一张「数据流地图」。
3. 看懂 `ofdm_decoder.v` 内部又是一条**子流水线**（解调→解交织→Viterbi→解扰→成字节）。
4. 理解模块之间统一的 **「数据＋strobe」握手风格**，以及 `dot11.v` 为什么要把 `phase` 和 `rot_lut` 两块资源**共享**给多个模块。

本讲是后续第 2、3 单元（逐模块精读）的**导航图**，不展开任何一个模块的算法细节——只把全局拼图画清楚。

---

## 2. 前置知识

### 2.1 什么是 OFDM，为什么要「解码流水线」

802.11a/g/n 把要发送的数据切成很多路低速比特流，分别调制到很多个**正交的子载波**（subcarrier）上，再用 IFFT 合并成一路时域信号发出去——这就是 **OFDM**（Orthogonal Frequency Division Multiplexing，正交频分复用）。接收端要还原数据，就必须**反过来**走一遍：把时域信号变回频域（FFT）、估计每个子载波在路上被「揉」成什么样（信道估计）、把星座点判回比特（解调）、再解开发送端做过的比特交织和扰码。

因为每一步都依赖上一步的结果，这些步骤天然构成一条**单向流水线**。OpenOFDM 把这条流水线**用硬件实现**：每个步骤对应一个 Verilog 模块，模块之间像工厂流水线一样接力。

### 2.2 strobe 握手：硬件流水线怎么「递东西」

硬件模块之间没有现成的「函数调用」，数据是在时钟节拍下流动的。OpenOFDM 全项目统一用一种最简单的握手约定：

- 每个模块都有一对 `xxx_strobe`（或 `_stb`）信号伴随数据。
- **只有当 `strobe` 为高的那一拍，数据才被认为是有效的**；strobe 为低时，数据线上的值是垃圾，下游应当忽略。

这种「数据＋strobe」风格贯穿全项目——记住这一点，后面看任何模块的端口都不会迷路。上一篇 u1-l4 已经在 `sample_in` / `sample_in_strobe` 上见过它了。

### 2.3 两个会被多个模块共享的资源

后面会看到，`dot11.v` 里有两块资源是被**多个模块轮流使用**的，这是 FPGA 设计中常见的「省资源」手法，初学时容易困惑，先打个预防针：

- **`phase` 模块**（算复数相位）：`sync_short`（粗频偏）和 `equalizer`（细频偏）**分时**共用一个实例。
- **`rot_lut`**（旋转因子查找表，本质是一块双口 RAM）：`sync_long` 和 `equalizer` 各用**一个端口**，同时共享。

这两点在 4.4 节会结合源码讲清楚。

---

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| `docs/source/overview.rst` | 给出 8 步解码流水线的**概念定义**，是本讲的主线权威来源 |
| `verilog/dot11.v` | 顶层模块，**实例化**了流水线上的所有子模块，是本讲的精读对象 |
| `verilog/ofdm_decoder.v` | 顶层里「解码」这一大步的内部**子流水线**（解调→…→成字节） |
| `verilog/common_params.v` | 顶层状态机的状态码定义（`S_*`），用于理解控制顺序 |
| `verilog/sync_long.v` | 用来确认「FFT」这一步实际落在哪个模块里（关键细节） |

> 提示：本讲只读上面这些文件的**结构与实例化关系**，不展开任何单个算法模块。每个模块的算法细节会在第 2、3 单元单独成篇。

---

## 4. 核心概念与源码讲解

### 4.1 八步解码流水线：从概念到实现

#### 4.1.1 概念说明

OpenOFDM 的官方文档 `overview.rst` 一开篇就列出了这条 8 步流水线。这是理解整个项目的**总纲**，无论后面看哪个模块，都要先问一句：「它属于 8 步里的哪一步？」

[docs/source/overview.rst:L4-L14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L4-L14) — 文档明确列出解码流水线的 8 个步骤：包检测、中心频偏校正、FFT、信道增益估计、解调、解交织、卷积解码、解扰。

这 8 步可以分成三大组，对应 Wi-Fi 接收的三个层次：

| 组 | 步骤 | 解决的问题 |
| --- | --- | --- |
| **前端同步** | ① 包检测 ② 频偏校正 | 「包来了吗？什么时候开始？时钟/频率对齐了吗？」 |
| **频域还原** | ③ FFT ④ 信道估计 | 把时域信号变回频域，并补偿每个子载波受到的畸变 |
| **比特还原** | ⑤ 解调 ⑥ 解交织 ⑦ 卷积解码 ⑧ 解扰 | 把星座点判回比特，再解开发送端做过的交织与扰码 |

#### 4.1.2 核心流程

发送端做的事和接收端**严格镜像**——发送端「扰乱」一次，接收端就「还原」一次。所以这条流水线本质上是对 802.11 发射机的**逆操作**：

```
接收时域样本
  │
  │ ① 包检测：发现能量突变，判定「有包」
  │ ② 中心频偏校正：用前导序列估计并抵消载波频偏
  ▼
时域样本（已对齐、已校正频偏）
  │
  │ ③ FFT：时域 → 频域，得到每个子载波的复数值
  │ ④ 信道估计：用训练序列算出每个子载波的复增益，做除法均衡
  ▼
频域复数（已均衡的星座点）
  │
  │ ⑤ 解调：星座点 → 比特
  │ ⑥ 解交织：按规则把比特位置还原
  │ ⑦ 卷积解码（Viterbi）：把冗余比特还原成原始信息比特
  │ ⑧ 解扰：用 LFSR 抵消发送端的加扰
  ▼
原始数据比特 → 组装成字节 → FCS 校验
```

#### 4.1.3 源码精读

这条概念流水线在硬件里**并不是 8 个一一对应的模块**——这是初学者最容易踩的坑。一个关键例子：第 ③ 步「FFT」在 `dot11.v` 里**找不到名为 `fft` 的顶层实例**。我们去 `sync_long.v` 里找，才会发现 FFT 其实藏在「长训练序列同步」这个模块内部：

[verilog/sync_long.v:L185-L199](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L185-L199) — `xfft_v7_1 dft_inst`：FFT 这一步其实由 Xilinx coregen IP `xfft_v7_1` 承担，且它被实例化在 `sync_long` **内部**（实例名 `dft_inst`，`fwd_inv=1` 表示正向 FFT），而不是一个独立的顶层模块。

> ⚠️ 这个映射关系是本讲最重要的「意外」之一：**概念步骤 ≠ 模块一一对应**。第 ③ 步 FFT 落在 `sync_long` 里；而第 ② 步频偏校正则由 `phase` + `rotate` 两个原语在多个模块里**重复出现**。第 4.2 节的表格会给出完整对照。

#### 4.1.4 代码实践

**实践目标**：把「概念 8 步」和「实际模块」对应起来，体会它们不是一一对应的。

**操作步骤**：

1. 打开 [docs/source/overview.rst:L4-L14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L4-L14)，抄下 8 步的名字。
2. 在仓库里用编辑器搜索 `xfft_v7_1`、`rotate`、`demodulate`、`viterbi` 等关键字，看它们各自被实例化在哪个文件。
3. 重点确认：FFT（第 ③ 步）出现在 `sync_long.v` 而非 `equalizer.v`。

**需要观察的现象**：你会发现第 ③、④ 两步的代码并不像概念那样「分家」——FFT 在 `sync_long`，信道估计在 `equalizer`；而频偏校正（第 ② 步）的 `rotate`/`phase` 原语在 `sync_long` 和 `equalizer` 里**各出现一次**。

**预期结果**：在脑子里（或纸上）建立一个印象——「8 步是逻辑顺序，模块是物理划分，两者错位是正常的」。

#### 4.1.5 小练习与答案

**练习 1**：8 步流水线里，哪一步把信号从「时域」变到「频域」？哪一步把它从「频域复数」变回「比特」？

> **答案**：时域→频域是第 ③ 步 **FFT**；频域复数→比特的「分界点」是第 ⑤ 步 **解调**（解调之前都是复数/星座点，解调之后都是比特）。

**练习 2**：为什么说接收端的 8 步是发送端的「镜像」？

> **答案**：因为发送端为了抗干扰做了扰码、卷积编码、交织、IFFT、上变频等操作；接收端必须按**相反顺序**逐一抵消，才能还原原始数据。例如发送端最后做扰码，接收端就最先（在比特层面）做解扰。

---

### 4.2 dot11.v 顶层例化：全局数据流地图

#### 4.2.1 概念说明

`dot11.v` 是整条流水线的「总装车间」。它本身**几乎不写算法**，主要做两件事：

1. **数据平面（data plane）**：把各个算法模块**按顺序实例化**并用 wire 串起来，让样本从 `sample_in` 一路流到 `byte_out`。
2. **控制平面（control plane）**：用一个状态机（`case(state)`）在合适的时机给各模块发 `enable` / `reset`，决定「现在轮到谁工作」。

本节只看数据平面——也就是「模块怎么连线」。控制平面（状态机）留到第 4 单元（u4-l1）专门讲。

#### 4.2.2 核心流程：从 sample_in 到 byte_out

下面这张「模块级数据流图」是本讲最核心的产出，建议手抄一遍：

```
                 ┌─────────────────────────────────────────────┐
sample_in ──────▶│ power_trigger.v   (包检测：能量门限)         │── power_trigger(触发)
 (32b I/Q)       └─────────────────────────────────────────────┘
      │
      ├──────────────────────────────────────────────┐
      ▼                                              ▼
 ┌──────────────────────────┐         ┌────────────────────────────────────┐
 │ sync_short.v (短训练同步) │─phase──▶│  phase.v ◀── 分时复用（sync_short/   │
 │ → short_preamble_detected│         │            equalizer 共用一个实例）  │
 │ → phase_offset(粗频偏)   │         └────────────────────────────────────┘
 └──────────────────────────┘
      │ phase_offset
      ▼
 ┌──────────────────────────────────────────────────────────┐
 │ sync_long.v (长训练同步)                                  │
 │   内含: rotate(频偏校正) → xfft_v7_1(FFT, dft_inst)       │── long_preamble_detected
 │   输出: sync_long_out (频域、已对齐的样本)                │
 └──────────────────────────────────────────────────────────┘
      │  sync_long_out (32b)
      ▼
 ┌──────────────────────────────────────────────────────────┐
 │ equalizer.v (信道估计 + 均衡)                             │
 │   内含: rotate(细相位跟踪) + divider(逐子载波相除)        │── equalizer_out
 └──────────────────────────────────────────────────────────┘
      │  equalizer_out (32b)  ──delayT(6拍)──▶ eq_out_*_delayed
      ▼
 ┌──────────────────────────────────────────────────────────┐
 │ ofdm_decoder.v  (见 4.3 节子流水线)                       │── byte_out + byte_out_strobe
 └──────────────────────────────────────────────────────────┘
      │  byte_out (8b)
      ▼
 ┌──────────────────────────────────────────────────────────┐
 │ crc32.v (FCS 校验) ──▶ fcs_ok                              │
 └──────────────────────────────────────────────────────────┘

共享资源:
  rot_lut (双口 RAM): A 口→sync_long.rotate,  B 口→equalizer.rotate
  phase  (分时复用):  state==S_SYNC_SHORT ? sync_short : equalizer
```

#### 4.2.3 源码精读

我们在 `dot11.v` 里逐个定位这些实例化，把上图和真实代码对上号。

**① 包检测 `power_trigger`**

[verilog/dot11.v:L257-L270](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L257-L270) — `power_trigger power_trigger_inst`：直接拿 `sample_in` 做能量门限检测，输出 `trigger`。它是流水线的「门铃」，没触发就不会往下走。

**② 短训练同步 `sync_short`**

[verilog/dot11.v:L272-L293](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L272-L293) — `sync_short sync_short_inst`：用短训练序列（STS）做延迟自相关，输出 `short_preamble_detected` 和 `phase_offset`（粗频偏）。注意它的 `reset` 和 `enable` 都由顶层状态机控制。

**③ 长训练同步 + FFT `sync_long`**

[verilog/dot11.v:L295-L319](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L295-L319) — `sync_long sync_long_inst`：输入 `sample_in` 与 `phase_offset`，输出 `sync_long_out`（**已经是频域、已对齐**的样本）和 `long_preamble_detected`。正如 4.1.3 所述，它内部藏着 FFT（见 `sync_long.v:L185`）。

**④ 信道均衡 `equalizer`**

[verilog/dot11.v:L321-L344](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L321-L344) — `equalizer equalizer_inst`：**输入是 `sync_long_out`**（注意这条连线，它把 sync_long 和 equalizer 串起来），输出 `equalizer_out`。这一步做信道估计与逐子载波均衡。

**⑤ 延时对齐 `delayT`**

[verilog/dot11.v:L347-L353](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L347-L353) — `delayT #(.DATA_WIDTH(33), .DELAY(6)) eq_delay_inst`：把 `equalizer_out` 连同它的 strobe **整体延迟 6 拍**。这是为了补偿某种流水线节拍，让 strobe 和数据在送进 `ofdm_decoder` 时仍然对齐。`{equalizer_out_strobe, equalizer_out}` 共 33 位一起搬移。

**⑥ 解码子流水线 `ofdm_decoder`**

[verilog/dot11.v:L356-L382](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L356-L382) — `ofdm_decoder ofdm_decoder_inst`：输入是延时后的 `{ofdm_in_i, ofdm_in_q}`，输出 `byte_out`。它内部还有一条 5 级子流水线，见 4.3 节。

**⑦ CRC（HT-SIG 与 FCS）**

[verilog/dot11.v:L384-L392](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L384-L392) — `ht_sig_crc crc_inst`：对 HT-SIG 字段做 CRC-8。

[verilog/dot11.v:L394-L400](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L394-L400) — `crc32 fcs_inst`：对 `byte_out` 做 CRC-32（FCS），得到整包校验结果 `fcs_ok`。这是流水线的**终点**。

> 小结对照表（概念 8 步 → 实际模块）：

| 8 步 | 实际承担模块 | 在 dot11.v 的实例 | 关键输出 |
| --- | --- | --- | --- |
| ① 包检测 | `power_trigger.v` + `sync_short.v` | `power_trigger_inst` / `sync_short_inst` | `power_trigger` / `short_preamble_detected` |
| ② 频偏校正 | `phase.v` + `rotate.v`（在多个模块内） | `phase_inst`；`sync_long`/`equalizer` 内各一个 `rotate` | `phase_offset`；校正后的样本 |
| ③ FFT | `xfft_v7_1`（**在 sync_long 内**） | 经由 `sync_long_inst` | `sync_long_out`（频域） |
| ④ 信道估计 | `equalizer.v` + `divider.v` | `equalizer_inst` | `equalizer_out` |
| ⑤ 解调 | `demodulate.v` | 经由 `ofdm_decoder_inst` | `demod_out[5:0]` |
| ⑥ 解交织 | `deinterleave.v` | 经由 `ofdm_decoder_inst` | `deinterleave_out[1:0]` |
| ⑦ 卷积解码 | `viterbi_v7_0`（Xilinx IP） | 经由 `ofdm_decoder_inst` | `conv_decoder_out` |
| ⑧ 解扰 | `descramble.v` + `bits_to_bytes.v` | 经由 `ofdm_decoder_inst` | `descramble_out` → `byte_out[7:0]` |

#### 4.2.4 代码实践

**实践目标**：亲手把 `dot11.v` 的实例化「翻译」成一张模块级框图，作为后续学习的导航图。

**操作步骤**：

1. 打开 `verilog/dot11.v`，定位 4.2.3 列出的 7 处实例化（行号已给出）。
2. 对每个实例，记下三件事：**模块名 → 源文件名 → 输入连的是谁、输出连给谁**。例如 `equalizer_inst` 的 `sample_in` 连的是 `sync_long_out`，输出 `equalizer_out` 先经 `eq_delay_inst` 再进 `ofdm_decoder_inst`。
3. 用纸或画图工具（draw.io / Excalidraw）画出 4.2.2 那样的框图，在每个方框上标出**对应的源文件名**，在每条连线上标出**关键 strobe 信号**（如 `sample_in_strobe`、`sync_long_out_strobe`、`equalizer_out_strobe`、`byte_out_strobe`）。
4. 用红笔标出两个「意外」：FFT 在 `sync_long` 内部；`phase` 与 `rot_lut` 是共享的。

**需要观察的现象**：画完会发现，除了 `power_trigger`，**几乎所有模块都靠 strobe 串联**，且样本数据「逐级变窄」——从 32 位复数样本，到 6 位解调比特，到 2 位解交织比特，到 1 位卷积/解扰比特，最后又汇成 8 位字节。这条「位宽收窄再汇合」的形状正是解码流水线的典型特征。

**预期结果**：得到一张可长期保存的导航图。后续读到任何一个模块时，都能在这张图上找到它的位置和上下游。**待本地验证**：框图属于手工产出，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：`equalizer_inst` 的输入 `sample_in` 连接的是哪个信号？这说明 equalizer 处理的是时域还是频域数据？

> **答案**：连的是 `sync_long_out`（见 [dot11.v:L325-L327](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L325-L327)）。因为 `sync_long` 内部已经做了 FFT，所以 `equalizer` 拿到的是**频域**样本，这正是它能「逐子载波」做信道估计的前提。

**练习 2**：`eq_delay_inst`（delayT）为什么要把 `{strobe, data}` 共 33 位一起延迟？

> **答案**：因为 strobe 和 data 必须**同步**移动。如果只延迟 data 而不延迟 strobe，下游看到的「有效拍」就会和真正的数据错位，整条流水线立刻乱套。把两者打包成一个 33 位向量一起过延时线，是保证握手不破坏的简洁做法。

---

### 4.3 ofdm_decoder.v：解码子流水线

#### 4.3.1 概念说明

`ofdm_decoder.v` 是顶层 `dot11.v` 里「解码」这一大步的内部实现。它自己也是一条**子流水线**，把 8 步里的后 4 步（解调→解交织→卷积解码→解扰）外加「成字节」全部封装在一起。可以这样理解层次关系：

```
dot11.v（顶层流水线：5 个大模块）
        │
        └── ofdm_decoder.v（其中「解码」大模块 = 5 级子流水线）
                ├── demodulate      （解调）
                ├── deinterleave    （解交织）
                ├── viterbi_v7_0    （卷积解码，Xilinx IP）
                ├── descramble      （解扰）
                └── bits_to_bytes   （串并转换：比特 → 字节）
```

为什么要再封装一层？因为这 5 级在控制上高度耦合（都要按 `rate` 配置、都要在合适时机 flush），单独拎到顶层会让 `dot11.v` 变得臃肿。封装成 `ofdm_decoder` 后，顶层只需喂 `(样本, rate, num_bits_to_decode, do_descramble)`，它就吐 `(byte_out, byte_out_strobe)`。

#### 4.3.2 核心流程

子流水线的数据流（注意位宽的逐级变化）：

```
sample_in[31:0] (均衡后的频域复数: 高16 I, 低16 Q)
   │
   │ ⑤ demodulate      : 复数 → demod_out[5:0]        (每个子载波最多 6 bit, 对应 64-QAM)
   ▼
demod_out[5:0]
   │
   │ ⑥ deinterleave    : 比特重排 → deinterleave_out[1:0]  (每次吐 2 bit, 配合 1/2 卷积码)
   ▼
deinterleave_out[1:0]
   │
   │ ⑦ viterbi_v7_0    : 软判决卷积解码 → conv_decoder_out (每次吐 1 bit)
   ▼
conv_decoder_out (1 bit)
   │
   │ ⑧ descramble      : LFSR 解扰 → descramble_out (1 bit)
   ▼
descramble_out (1 bit)
   │
   │    bits_to_bytes  : 每 8 bit 组 1 byte → byte_out[7:0]
   ▼
byte_out[7:0] + byte_out_strobe
```

#### 4.3.3 源码精读

我们在 `ofdm_decoder.v` 中逐级定位这 5 个实例：

[verilog/ofdm_decoder.v:L54-L65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L65) — `demodulate demod_inst`：根据 `rate` 选择星座（BPSK/QPSK/16-QAM/64-QAM），把 `cons_i/cons_q` 判成 `bits[5:0]`。

[verilog/ofdm_decoder.v:L67-L79](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L67-L79) — `deinterleave deinterleave_inst`：用查找表把交织后的比特位置还原，输出 2 bit 一组，并给出 `erase`（擦除指示，用于卷积码的 puncturing 还原）。

[verilog/ofdm_decoder.v:L81-L90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L81-L90) — `viterbi_v7_0 viterbi_inst`：Xilinx Viterbi IP，做卷积码的最大似然解码。注意它吃的是 3 bit **软判决**（`conv_in0/conv_in1`），不是硬比特。

[verilog/ofdm_decoder.v:L93-L103](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L93-L103) — `descramble decramble_inst`（实例名拼写为 `decramble`，模块是 `descramble`）：用接收前 7 bit 初始化 LFSR，抵消发送端的加扰。

[verilog/ofdm_decoder.v:L106-L116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L106-L116) — `bits_to_bytes byte_inst`：把 1 bit 流每 8 个组装成 1 byte，每组成一个就拉高一次 `byte_out_strobe`。

这里有一个值得注意的控制细节，它解释了「SIGNAL/HT-SIG 字段」与「数据」的区别：

[verilog/ofdm_decoder.v:L126-L128](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L126-L128) — `skip_bit <= 9`：复位后默认跳过解扰输出的前 9 个比特。这是因为 802.11 数据帧开头有一个 16 bit 的 service 字段（前 9 bit 在此处被跳过），不属于有效载荷。这个细节会在 u3-l6 详讲。

#### 4.3.4 代码实践

**实践目标**：在 `ofdm_decoder.v` 中跟踪一次「数据宽度」的变化，体会流水线如何把复数样本「压缩」成字节。

**操作步骤**：

1. 在 [ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) 中找出每一级的输出位宽：`demod_out[5:0]`、`deinterleave_out[1:0]`、`conv_decoder_out`、`descramble_out`、`byte_out[7:0]`。
2. 列一张表：级别 → 输出位宽 → 对应 8 步里的哪一步。
3. 思考：为什么 `demod_out` 是 6 位、而 `deinterleave_out` 只有 2 位？（提示：解调按子载波吐比特，64-QAM 一个子载波 6 bit；解交织/卷积码处理时每次处理 2 bit。）

**需要观察的现象**：位宽从 32（复数样本）→ 6（解调比特）→ 2（解交织比特对）→ 1（卷积/解扰比特）→ 8（字节），呈现「先收窄、后汇合」的形状。

**预期结果**：得到一张位宽变化表，加深对「解码 = 把模拟/复数信息逐步还原成数字比特」的理解。**待本地验证**：属源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`ofdm_decoder` 内部 5 个实例，分别对应 8 步流水线的哪几步？哪一步是用 Xilinx IP 实现的？

> **答案**：`demodulate`=⑤解调，`deinterleave`=⑥解交织，`viterbi_v7_0`=⑦卷积解码（Xilinx IP），`descramble`=⑧解扰，`bits_to_bytes` 是⑧之后的「成字节」辅助步骤（不在概念 8 步内）。用 Xilinx IP 实现的是 **Viterbi 卷积解码**。

**练习 2**：`ofdm_decoder_inst` 的 `rate` 输入来自哪里？为什么解码需要知道 rate？

> **答案**：`rate` 接的是 `pkt_rate`（见 [dot11.v:L366](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L366)）。因为不同 rate 对应不同调制方式（BPSK/QPSK/16-QAM/64-QAM）和不同交织参数，解调和解交织都必须按 rate 配置才能正确还原比特。

---

### 4.4 模块间的握手风格与资源共享

#### 4.4.1 概念说明

前面三节我们看了「有哪些模块、怎么串联」。本节回答两个收尾问题：

1. **模块间到底靠什么传递数据？** —— 统一的「数据＋strobe」握手（已在 2.2 节铺垫）。
2. **为什么 `dot11.v` 里有两块资源（`phase`、`rot_lut`）是被多个模块共享的？** —— FPGA 资源宝贵，能复用就复用。

理解这两点，才算真正读懂了顶层的「连线意图」，而不是只看到一堆 wire。

#### 4.4.2 核心流程

**握手风格**：每个模块都有一对 `input_strobe` / `output_strobe`（或 `_stb`）。上游在数据有效的那一拍拉高 strobe，下游只在 strobe 高时采样数据。整个流水线没有任何「应答（ack）」回线——是**单向、无反压**的流水。这意味着如果上游偶尔漏一拍，数据就丢了；所以各模块内部的节拍设计必须匹配（这也是 `delayT` 用来做节拍对齐的原因）。

**资源共享**：

- `phase` 模块（算复数相位）只实例化了**一个**，被 `sync_short` 和 `equalizer` **分时**使用。顶层用一个多路选择器，按当前状态 `state == S_SYNC_SHORT` 决定把谁的输入送进去。
- `rot_lut`（旋转因子表）是一块**双口 RAM**，两个端口分别服务于 `sync_long` 和 `equalizer`，两者可**同时**访问各自的端口。

#### 4.4.3 源码精读

**phase 的分时复用**：

[verilog/dot11.v:L118-L160](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L118-L160) — 注释明确写着 "Shared phase module for sync_short and equalizer"。

关键的多路选择在 [dot11.v:L133-L138](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L133-L138)：

```verilog
wire[31:0] phase_in_i = state == S_SYNC_SHORT?
    sync_short_phase_in_i: eq_phase_in_i;
wire[31:0] phase_in_q = state == S_SYNC_SHORT?
    sync_short_phase_in_q: eq_phase_in_q;
wire phase_in_stb = state == S_SYNC_SHORT?
    sync_short_phase_in_stb: eq_phase_in_stb;
```

含义：当前处于 `S_SYNC_SHORT` 状态时，把 `sync_short` 的 I/Q 喂给 `phase`；否则（均衡阶段）把 `equalizer` 的 I/Q 喂进去。输出则同时回连给两者（[dot11.v:L143-L146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L143-L146)）。因为两个使用者**从不在同一时刻工作**（一个在短同步阶段，一个在均衡阶段），所以分时复用是安全的。

**rot_lut 的双口共享**：

[verilog/dot11.v:L96-L113](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L96-L113) — 注释 "Shared rotation LUT for sync_long and equalizer"。`rot_lut_inst` 用 `addra/douta`（A 口）接 `sync_long`，用 `addrb/doutb`（B 口）接 `equalizer`。双口 RAM 天然支持两个独立地址同时读，所以这两者甚至可以**并发**使用，无需分时。

> 顺带一提，状态机的状态码定义在 [common_params.v:L27-L41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L27-L41)，例如 `S_SYNC_SHORT=1`、`S_DECODE_SIGNAL=3`、`S_DECODE_DATA=11`、`S_DECODE_DONE=14`。状态机本身的精读留到 u4-l1，本节只需知道「`state` 这个信号被用来做 phase 的多路选择」即可。

#### 4.4.4 代码实践

**实践目标**：亲手验证「phase 是被两个模块共享的同一个实例」，体会分时复用的设计意图。

**操作步骤**：

1. 在 [dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 中用搜索功能数一下 `phase` 关键字出现了几次，确认 `phase_inst` **只有一个实例**。
2. 阅读 L121-L146，画出「两个生产者（sync_short、equalizer）→ 一个多路选择器 → phase_inst → 两个消费者」的连线图。
3. 对照状态码（`common_params.v`）确认：`S_SYNC_SHORT` 和 equalizer 工作的阶段（`S_DECODE_SIGNAL` 起的各状态）在时间上**不重叠**，所以分时复用不会冲突。

**需要观察的现象**：`phase` 的输入/输出 wire 各有「sync_short 版」和「eq 版」两组，但模块实例只有一个。

**预期结果**：理解「分时复用」= 用一个多路选择器 + 状态判断，让一个硬件单元在不同时段服务不同模块。**待本地验证**：属源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：`phase` 用「分时复用」，`rot_lut` 用「双口共享」。为什么前者不能也用双口、后者不能也用分时？

> **答案**：`phase` 是一个**有时序状态的计算模块**（内部有流水线寄存器），不是单纯存储，无法像 RAM 那样开两个独立端口，只能靠多路选择器分时喂入。`rot_lut` 是**纯查表存储**（双口 RAM），天然支持两地址并发读，所以用双口更高效，不必分时。设计者是根据资源本身的性质选择共享方式的。

**练习 2**：整条流水线有没有「反压（back-pressure / ack）」机制？如果某一拍上游 strobe 有效但下游没准备好，会发生什么？

> **答案**：没有反压机制。OpenOFDM 的流水线是**单向无应答**的，下游不向上游回 ack。这意味着各模块的吞吐必须由设计保证匹配（必要时用 `delayT` 做节拍对齐）。若节拍失配，数据可能被覆盖丢失——这也是为什么定点缩放和各级 strobe 节拍在整个项目里如此关键（u6-l1 会专题讨论）。

---

## 5. 综合实践

**综合任务：制作一份「OpenOFDM 解码流水线导航手册」**

把本讲的所有产出整合成一份可长期查阅的文档（Markdown 或纸笔均可），要求包含：

1. **8 步概念流水线表**：步骤号、名称、解决什么问题（取自 4.1）。
2. **概念→模块对照表**：每一步对应的实际模块名、源文件名、顶层实例名、关键输出信号（取自 4.2.3 的小结表，**自己核对行号**）。
3. **两张框图**：
   - 顶层 `dot11.v` 的 `sample_in → byte_out` 模块级框图（4.2.2），标注源文件名和 strobe 信号；
   - `ofdm_decoder.v` 的子流水线框图（4.3.2），标注每级位宽。
4. **两个「意外」备忘**：FFT 在 `sync_long` 内部；`phase` 与 `rot_lut` 被共享（4.4）。
5. **位宽变化链**：32（样本）→ 6（解调）→ 2（解交织）→ 1（卷积/解扰）→ 8（字节）。

**验证方法**：随机挑一个模块（例如 `demodulate`），问自己三个问题——它在 8 步里是第几步？它的上游和下游分别是谁？它的输入/输出 strobe 叫什么？如果三个都能从你的手册里查到，说明导航图建成了。

> 这份手册就是你在第 2、3 单元逐模块精读时的「地图」。每读一个模块，就在图上把它「点亮」，标上你新学到的算法要点。

---

## 6. 本讲小结

- OpenOFDM 的解码遵循 **8 步概念流水线**：包检测→频偏校正→FFT→信道估计→解调→解交织→卷积解码→解扰，是 802.11 发射机的严格逆操作。
- **概念步骤 ≠ 模块一一对应**：最典型的例子是 FFT（第 ③ 步）藏在 `sync_long.v` 内部（`xfft_v7_1 dft_inst`），而不是独立模块；频偏校正（第 ② 步）的 `phase`/`rotate` 原语在多个模块里重复出现。
- 顶层 `dot11.v` 的数据流是：`sample_in → power_trigger → sync_short → sync_long(含FFT) → equalizer → delayT → ofdm_decoder → byte_out`，最后由 `crc32` 做 FCS 校验。
- `ofdm_decoder.v` 内部是一条 **5 级子流水线**：`demodulate → deinterleave → viterbi → descramble → bits_to_bytes`，数据位宽呈「32→6→2→1→8」的收窄再汇合形状。
- 全项目统一 **「数据＋strobe」单向无反压** 握手；`dot11.v` 通过 **`phase` 分时复用** 和 **`rot_lut` 双口共享** 来节省 FPGA 资源。
- 本讲建立的是**导航图**：每个模块的算法细节（怎么检测、怎么均衡、怎么解调）留待第 2、3 单元逐篇精读，控制平面（状态机）留待第 4 单元。

---

## 7. 下一步学习建议

有了这张全局地图，接下来可以从数据流的**最前端**开始逐模块深入。建议按数据流顺序学习第 2 单元「前端检测与同步」：

- **u2-l1 包检测 power_trigger.v**：从流水线第一站 `power_trigger_inst` 开始，看能量门限如何「敲响门铃」。
- **u2-l2 短训练序列同步 sync_short.v**：理解 `short_preamble_detected` 和 `phase_offset` 是怎么算出来的。
- **u2-l4 长训练同步 sync_long.v**：这是本讲反复提到的「藏了 FFT」的模块，读懂它就能把第 ②③ 步彻底打通。

如果你更关心「字节是怎么还原的」，也可以先跳到第 3 单元（u3-l3 解调、u3-l5 ofdm_decoder 子流水线），但建议至少先读 u2-l1，理解 strobe 流水线是怎么启动的。

> 阅读源码时，随时回到本讲的「概念→模块对照表」和两张框图定位，避免在细节里迷路。
