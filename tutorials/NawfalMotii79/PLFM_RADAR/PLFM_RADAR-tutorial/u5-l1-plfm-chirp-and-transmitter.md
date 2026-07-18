# PLFM Chirp 生成与发射机

## 1. 本讲目标

本讲聚焦雷达的**发射链入口**：AERIS-10 是怎么把一段「线性调频脉冲（PLFM chirp）」变成 DAC 上的模拟波形、又怎么把它和混频器、ADAR1000 相移器、扫描节拍协同起来的。

读完本讲，你应当能够：

- 说清楚一段 PLFM chirp 为什么被拆成「长 chirp + 短 chirp」两段，以及状态机如何把它们串成一个完整帧。
- 根据 `T1_SAMPLES=3600`、采样率 `120 MHz` 算出长 chirp 的真实持续时长，并把状态机的状态序列一步步写出来。
- 看懂 8 位 chirp 数据如何经 `dac_interface` 送到 DAC，为什么 DAC 时钟要用 ODDR 原语转发。
- 解释发射机 `radar_transmitter` 如何做跨时钟域（CDC）、边沿检测、SPI 电平转换穿通，以及**当前版本里 ADAR1000 的 load 引脚是直接拉地、靠 `adar_tr` 位与混频器使能来分时控制**这一关键事实。
- 把 `chirp_counter / elevation_counter / azimuth_counter` 三个计数器对应到一次完整的电子扫描节拍。

本讲承接 u3-l1（FPGA 顶层）和 u4 系列的接收链认知：发射链与接收链镜像对称，理解了发射端的 chirp 节拍，回过头看 u4-l4 的「双 16 点 Doppler 子帧」就会明白它的 32 个 chirp 是怎么来的。

## 2. 前置知识

在进入源码前，先用三段话补齐直觉。

**线性调频（LFM/chirp）与脉冲压缩。** 雷达想「看得远」就需要大能量（长脉冲），想「分得清」就需要高距离分辨率（短脉冲），二者天然矛盾。PLFM 的解法是：发射一个频率随时间线性扫动的长脉冲（chirp），接收端用匹配滤波把它「压」成一个窄峰（脉冲压缩）。这样能量与分辨率解耦——距离分辨率只由带宽 \(B\) 决定：

\[
\Delta R = \frac{c}{2B}
\]

脉压增益近似为时间-带宽积 \(T\cdot B\)。本讲的发射端只负责「把 chirp 波形按时序喂给 DAC」，真正做压缩的是 u4-l2 的匹配滤波。

**长/短 chirp 与 staggered PRI。** AERIS-10 的一帧并不是 32 个完全相同的 chirp，而是 **16 个长 chirp + 16 个短 chirp** 交替的节拍。两种 chirp 的「脉冲重复周期（PRI）」略有不同（长、短各自带不同长度的监听时间），这叫 staggered PRI。它的好处在 u4-l4 讲过：两段各自均匀采样的子帧可以用来解 Doppler 速度模糊。本讲要回答的，是这个长/短交替节拍在硬件上由谁、以什么状态机产生。

**相控阵扫描节拍。** 电子波束扫描靠 ADAR1000 相移器设置递进相位实现（u1-l1）。要把整个空域扫一遍，雷达需要三层嵌套计数：**chirp（同一波位的相干积累）→ elevation（仰角步进）→ azimuth（方位步进）**。本讲的 `plfm_chirp_controller` 内部跑 chirp 层，外部由 STM32 给出 `new_chirp / new_elevation / new_azimuth` 三个脉冲节拍来推进。

> 名词速查：PRI（Pulse Repetition Interval，脉冲重复周期，即两次发射之间的间隔）；PRF（Pulse Repetition Frequency，=1/PRI）；chirp（线性调频脉冲）；TR switch（收发切换开关，由 `adar_tr` 位与 RF 开关控制）；ODDR（Xilinx 7 系列的输出双数据率寄存器原语，用于时钟转发与 IOB 打包）。

## 3. 本讲源码地图

本讲涉及的关键文件如下。注意这里有一个容易踩坑的**文件名与模块名不一致**问题，先列清楚：

| 文件 | 文件内的 module | 作用 |
|---|---|---|
| `9_Firmware/9_2_FPGA/plfm_chirp_controller.v` | `plfm_chirp_controller_enhanced` | chirp 状态机核心：产生长/短 chirp 数据、`rf_switch_ctrl`、混频器与 ADAR1000 使能、`chirp/elevation/azimuth` 计数器 |
| `9_Firmware/9_2_FPGA/dac_interface_single.v` | `dac_interface_enhanced` | 把 8 位 chirp 数据送到 DAC，并用 ODDR 转发 DAC 时钟 |
| `9_Firmware/9_2_FPGA/radar_transmitter.v` | `radar_transmitter` | 发射机顶层：做 CDC、边沿检测、SPI 电平转换穿通，例化上面两个模块 |
| `9_Firmware/9_2_FPGA/long_chirp_lut.mem` | （数据文件，3600 行 × 8 位） | 长 chirp 的实际波形样本，被控制器 `$readmemh` 装进 BRAM |
| `9_Firmware/9_2_FPGA/short_chirp_i.mem` / `short_chirp_q.mem` | （数据文件，50 行 × 16 位） | **短 chirp 的匹配滤波参考**（16 位复 I/Q），由 u4-l2 的 `chirp_memory_loader_param` 加载，**不是发射端用的** |

⚠️ 特别提醒：**`short_chirp_i.mem` 不在本讲的发射通路上**。发射端真正用的短 chirp 是源码里硬编码的 8 位 `short_chirp_lut`（见 4.1.3）。`short_chirp_i.mem` 是同一个短 chirp 波形的高精度（16 位、复 I/Q）参考副本，供接收端脉冲压缩用。把它列在这里，是为了让你看清「发射波形」与「接收参考」是同一物理波形的两种表示——这是理解 u4-l2 脉冲压缩的前提。

另外，`plfm_chirp_controller.v` 顶部声明的 `F_START=30MHz / F_END=10MHz / FS=120MHz` 三个参数是**设计意图说明**，仅出现在声明处、不参与任何逻辑运算（[plfm_chirp_controller.v:36-38](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L36-L38)）。RTL 里真正逐样本的波形来自 `long_chirp_lut.mem`，而不是用这几个频率参数现场合成。读源码时不要被它们误导。

## 4. 核心概念与源码讲解

### 4.1 PLFM Chirp 波形与状态机

#### 4.1.1 概念说明

发射端的根本任务只有一个：**按节拍，把 chirp 波形样本一个不差地喂给 DAC，并在「发」与「收」之间精确分时切换 RF 通路。**

为此 `plfm_chirp_controller_enhanced` 用一个 7 状态的有限状态机（FSM）来组织时间。每一拍要回答三个问题：

1. **现在在发还是收？**——决定 `tx_mixer_en / rx_mixer_en` 与 `rf_switch_ctrl` 的电平。
2. **现在该输出什么数据？**——长 chirp 读 BRAM，短 chirp 读内联 LUT，其余时间输出中点 `8'd128`（DAC 的零输入）。
3. **当前是第几个 chirp？**——用 `chirp_counter` 决定是否进入下一段（guard、短 chirp、done）。

为什么需要 GUARD（保护时间）？因为长、短两段 chirp 的 PRI 不同，状态机在切换波形形状前需要一个隔离期，避免上一段监听窗口的尾音污染下一段，也给 ADAR1000 与 RF 开关留出切换裕量。

#### 4.1.2 核心流程

把状态机画成节拍，一个完整帧（一次 `new_chirp` 触发）的轨迹是：

```
IDLE
  └─[new_chirp & mixers_enable]─▶ LONG_CHIRP ─▶ LONG_LISTEN ─┐
        ┌───────────────────────────────────────────────────┘
        │  (LONG_CHIRP/LONG_LISTEN 重复 16 次，chirp_counter 0..15)
        └─▶ GUARD_TIME ─▶ SHORT_CHIRP ─▶ SHORT_LISTEN ─┐
        ┌────────────────────────────────────────────────┘
        │  (SHORT_CHIRP/SHORT_LISTEN 重复 16 次，chirp_counter 16..31)
        └─▶ DONE ─▶ IDLE
```

也就是说：**16 个长 chirp + guard + 16 个短 chirp = 一帧 32 个 chirp**。这正好对应 u4-l4 里 Doppler 的「双 16 点子帧」——长 chirp 子帧与短 chirp 子帧各做一次 16 点 FFT。

每个状态的持续时间由对应的样本计数参数决定（采样率 `FS = 120 MHz`，采样周期 \(T_s = 1/120\,\text{MHz} \approx 8.333\,\text{ns}\)）：

| 状态 | 计数参数 | 样本数 | 时长 |
|---|---|---|---|
| LONG_CHIRP | `T1_SAMPLES` | 3600 | \(3600 \times 8.333\,\text{ns} = 30.0\,\mu\text{s}\) |
| LONG_LISTEN | `T1_RADAR_LISTENING` | 16440 | \(137.0\,\mu\text{s}\) |
| GUARD_TIME | `GUARD_SAMPLES` | 21048 | \(175.4\,\mu\text{s}\) |
| SHORT_CHIRP | `T2_SAMPLES` | 60 | \(0.5\,\mu\text{s}\) |
| SHORT_LISTEN | `T2_RADAR_LISTENING` | 20940 | \(174.5\,\mu\text{s}\) |

参数声明见 [plfm_chirp_controller.v:40-45](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L40-L45)，注释里已标好换算后的微秒数，可对照验算。

由此还能推出两类有用的物理量：

- **长 chirp 的 PRI** = 发射 + 监听 = \(3600 + 16440 = 20040\) 样本 \(= 167.0\,\mu\text{s}\)。
- **最大非模糊距离** \(R_{\max} = c\cdot\text{PRI}/2 = 3\times10^8 \times 167\times10^{-6}/2 \approx 25.0\,\text{km}\)（与 Extended 版 20 km 量级一致，留了余量）。
- **chirp 带宽**（按设计意图参数）\(B = |F_{\text{start}}-F_{\text{end}}| = |30-10|\,\text{MHz} = 20\,\text{MHz}\)，距离分辨率 \(\Delta R = c/2B = 7.5\,\text{m}\)。

#### 4.1.3 源码精读

**(a) 状态编码与下一状态逻辑。** 7 个状态用 3 位编码，集中声明在 [plfm_chirp_controller.v:52-59](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L52-L59)。下一状态逻辑是一个纯组合 `case`，体现了 4.1.2 那张转移图，最关键的两处判据是「长段是否满 16」和「短段是否满 32」：

```verilog
// 长监听结束：满 16 个长 chirp 就进 guard，否则继续长 chirp
LONG_LISTEN: begin
    if (sample_counter == T1_RADAR_LISTENING-1) begin
        if (chirp_counter == (CHIRP_MAX/2)-1)   // == 15
            next_state = GUARD_TIME;
        else
            next_state = LONG_CHIRP;
    end else next_state = LONG_LISTEN;
end
// 短监听结束：满 32 个 chirp 就 done，否则继续短 chirp
SHORT_LISTEN: begin
    if (sample_counter == T2_RADAR_LISTENING-1) begin
        if (chirp_counter == CHIRP_MAX-1)        // == 31
            next_state = DONE;
        else
            next_state = SHORT_CHIRP;
    end else next_state = SHORT_LISTEN;
end
```

完整逻辑见 [plfm_chirp_controller.v:177-237](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L177-L237)。注意 `CHIRP_MAX=32`、`CHIRP_MAX/2-1=15` 这两个魔数决定了「16 长 + 16 短」的对称结构（参数声明见 [plfm_chirp_controller.v:48-50](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L48-L50)）。

**(b) 时序核心：唯一一个 `clk_120m` 的状态寄存器。** 状态机跑在 `clk_120m`（DAC 域）上（[plfm_chirp_controller.v:168-174](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L168-L174)）。同一个 `clk_120m` 时序块里还驱动 `sample_counter`、`chirp_counter` 以及所有数据/控制输出（[plfm_chirp_controller.v:239-340](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L239-L340)）。这里有一段重要的历史注释：`chirp_counter` **只由这个 120M 块驱动**，曾经还存在一个 100M 的冗余驱动，会造成「多驱动同一寄存器」（综合失败、仿真竞争），已被删除（[plfm_chirp_controller.v:128-132](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L128-L132)）。读源码遇到「某个寄存器为何只在一处写」时，往往就是这种多驱动 bug 的遗迹。

**(c) 长 chirp 波形来自 BRAM，短 chirp 是内联 LUT。** 长 chirp 用 `(* ram_style = "block" *)` 综合成块 RAM，并通过 `$readmemh` 从 `long_chirp_lut.mem` 装载 3600 个 8 位样本（[plfm_chirp_controller.v:71](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L71)、[plfm_chirp_controller.v:106-108](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L106-L108)）。读取采用**同步读**（无异步复位），这是为了让综合器推断出真正的 BRAM（[plfm_chirp_controller.v:111-113](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L111-L113)）；这一改动对应提交记录里的「chirp BRAM migration（B2）」。短 chirp 只有 60 个样本、太小不值得开 BRAM，于是硬编码在源码里（[plfm_chirp_controller.v:116-126](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L116-L126)）。

发 chirp 时把读出的样本送上 `chirp_data` 并拉高 `chirp_valid`，同时切 RF 开关与 ADAR1000 TR 位（[plfm_chirp_controller.v:281-292](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L281-L292) 为长 chirp 段、[plfm_chirp_controller.v:304-315](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L304-L315) 为短 chirp 段）。不发的时候 `chirp_data` 保持中点 `8'd128`（DAC 的「零」）。

**(d) 帧起始脉冲。** `new_chirp_frame` 在「IDLE 即将跳到 LONG_CHIRP」的那一拍拉高，用来告诉下游（接收链）一帧的相干积累开始了（[plfm_chirp_controller.v:81](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L81)）。

**(e) 混频器分时使能。** 收发严格互斥：发 chirp 时 `tx_mixer_en` 高、`rx_mixer_en` 低；监听时反过来。两者都受 `mixers_enable`（STM32 主使能）门控（[plfm_chirp_controller.v:86-89](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L86-L89)）。`mixers_enable` 一旦为 0，整个时序块进入「全部输出归零」的安全分支（[plfm_chirp_controller.v:331-339](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L331-L339)）。

#### 4.1.4 代码实践

这是本讲的主实践任务（纸笔 + 源码阅读型，**待本地验证**指的是若你想用仿真波形核对，需要自行跑 testbench）。

**实践目标：** 算出长 chirp 的真实时长，并徒手写出一个完整 chirp 周期的状态序列与 `chirp_counter` 变化。

**操作步骤：**

1. 打开 [plfm_chirp_controller.v:40-45](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L40-L45)，记下 `T1_SAMPLES=3600`、`FS=120MHz`。
2. 计算长 chirp 时长：\(T = T1\_SAMPLES / FS = 3600 / 120\times10^6 = 30\,\mu\text{s}\)。
3. 对照下一状态逻辑 [plfm_chirp_controller.v:177-237](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L177-L237)，把下表填满（这里给出前两行作示例，其余自己推）：

| 步 | 当前状态 | 事件 | chirp_counter（事件后） | 下一状态 |
|---|---|---|---|---|
| 1 | IDLE | 收到 `new_chirp`，`mixers_enable=1` | 0 | LONG_CHIRP |
| 2 | LONG_CHIRP | `sample_counter` 数到 3599 | 0 | LONG_LISTEN |
| 3 | LONG_LISTEN | 数到 16399，cc≠15 → cc++ | 1 | LONG_CHIRP |
| … | … | … | … | … |
| 33 | SHORT_LISTEN | 数到 20939，cc==31 | 31 | DONE |
| 34 | DONE | 无条件 | 31 | IDLE |

**需要观察的现象：** 你应当发现长段会经历恰好 16 次「LONG_CHIRP→LONG_LISTEN」循环（`chirp_counter` 从 0 走到 15），第 16 次监听结束时 `chirp_counter==15==(CHIRP_MAX/2-1)` 才跳进 GUARD_TIME；短段同理循环 16 次（cc 16→31），第 16 次结束时 `cc==31==CHIRP_MAX-1` 才跳 DONE。

**预期结果：** 一帧 = 16 长 + guard + 16 短 = 32 个 chirp，与 u4-l4 的双 16 点 Doppler 子帧完全对齐。把完整状态序列写成一行就是：

```
IDLE → (LONG_CHIRP→LONG_LISTEN)×16 → GUARD_TIME → (SHORT_CHIRP→SHORT_LISTEN)×16 → DONE → IDLE
```

4. **（选做）估算一帧总时长**：\(16\times20040 + 21048 + 16\times21000 = 677688\) 样本 \(\approx 5.65\,\text{ms}\)。这是一个波位（beam position）的驻留时间。

> 本任务不依赖硬件。若想用波形核对，可在 `9_Firmware/9_2_FPGA/tb/` 下找现成 testbench 用 iverilog 跑，观察 `current_state` 与 `chirp_counter` 的跳变（本地运行方式见 u1-l4）。

#### 4.1.5 小练习与答案

**练习 1：** 如果想把「长 + 短」改成「长 + 长」（即去掉短 chirp、全用长 chirp），状态机里最少要改哪几个判断？

**参考答案：** 把 SHORT 段改成与 LONG 段相同即可。但更本质的是：去掉短段后 `CHIRP_MAX/2-1` 这个「长段结束」判据就应改成 `CHIRP_MAX-1`，并且 GUARD→SHORT 的跳转目标改成回到 LONG_CHIRP。代价是失去 staggered PRI 的 Doppler 解模糊能力（见 u4-l4）。

**练习 2：** 为什么 `long_chirp_lut` 要用 `(* ram_style = "block" *)` 且用同步读，而 `short_chirp_lut` 用普通 `reg` 数组即可？

**参考答案：** 长 chirp 有 3600 个 8 位样本，规模大，用 BRAM 可以省下大量触发器并提高时序余量；BRAM 要求同步读、且不能有异步复位，所以读口写成「无 reset 的 `posedge` 寄存」([plfm_chirp_controller.v:111-113](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L111-L113))。短 chirp 只有 60 个样本，用分布式 RAM/触发器更省事，硬编码也便于维护。

### 4.2 DAC 接口与时钟转发

#### 4.2.1 概念说明

DAC（主板上是一片 AD9708，见 u2-l1）需要两路输入：**数据** 和 **采样时钟**。数据是 8 位宽（与 chirp LUT 的位宽一致），采样时钟就是 `clk_120m`。这里有两个工程要点：

1. **数据要有「使能」语义。** 只有 `chirp_valid` 为高时才是真实波形，其余时间 DAC 应输入中点 `8'd128`（相当于模拟零电平），避免把监听期的随机电平当成信号发出去。
2. **时钟和数据必须对齐、且抖动要小。** DAC 时钟直接决定输出模拟信号的质量，所以不能随便用 fabric 布线把 `clk_120m` 引到引脚——要用 Xilinx 7 系列的 **ODDR 原语** 把时钟「寄存」到 IOB（I/O Block）里输出，让时钟边沿和数据脚位处在同一个 bank、走相近的延迟。

#### 4.2.2 核心流程

DAC 接口的数据通路极简：

```
chirp_data[7:0] ──┐
                  ├─▶ dac_data_reg ──▶ ODDR×8 ──▶ dac_data[7:0] 引脚
chirp_valid ──────┘        (valid=0 时回中点 128)

clk_120m ──▶ ODDR(D1=1,D2=0) ──▶ dac_clk 引脚   (时钟转发，与数据同 IOB)
```

- `dac_data_reg` 在每个 `clk_120m` 上升沿更新：`chirp_valid` 高时载入 `chirp_data`，否则保持 `8'd128`（[dac_interface_single.v:16-24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/dac_interface_single.v#L16-L24)）。
- 综合时（`` `ifndef SIMULATION ``）用 ODDR 转发时钟：`D1=1, D2=0` 在 `OPPOSITE_EDGE` 模式下产生一个与 `clk_120m` 上升沿对齐的时钟副本（[dac_interface_single.v:33-45](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/dac_interface_single.v#L33-L45)）。
- 8 位数据各自走一个 ODDR（`D1=D2=同值`，等效 SDR，但被打进 IOB），消除 fabric 到引脚的布线抖动（[dac_interface_single.v:52-69](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/dac_interface_single.v#L52-L69)）。
- 仿真时（`` `ifdef SIMULATION ``）跳过 ODDR，用行为级 `assign`，方便 iverilog 跑（[dac_interface_single.v:71-77](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/dac_interface_single.v#L71-L77)）。`dac_sleep` 恒为 0（DAC 始终唤醒，[dac_interface_single.v:79](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/dac_interface_single.v#L79)）。

#### 4.2.3 源码精读

整个模块（`dac_interface_enhanced`，文件名却叫 `dac_interface_single.v`）的核心就是上面三段，见 [dac_interface_single.v:1-81](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/dac_interface_single.v#L1-L81)。注意它**没有**任何状态机，只是「寄存 + ODDR 打包」的纯组合输出级。复杂的节拍全在 4.1 的控制器里，本模块只负责把数据干净地送上引脚——这是好的模块划分：时序逻辑与 IO 物理分离。

`radar_transmitter` 里例化它的方式（[radar_transmitter.v:240-248](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L240-L248)）只是把 `chirp_data/chirp_valid` 接到输入、`dac_data/dac_clk/dac_sleep` 接到顶层引脚。

#### 4.2.4 代码实践

**实践目标（源码阅读型）：** 把 `chirp_valid` 与 `dac_data` 的时序关系在脑中画清楚。

**操作步骤：**

1. 读 [dac_interface_single.v:16-24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/dac_interface_single.v#L16-L24)。
2. 回到控制器 [plfm_chirp_controller.v:281-292](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L281-L292)，看 LONG_CHIRP 段里 `chirp_valid` 何时被拉高、`chirp_data` 何时等于 `long_chirp_rd_data`。
3. 回答：在 LONG_LISTEN 状态下，`chirp_valid` 是什么电平？`dac_data` 最终稳定在哪个值？

**预期结果：** LONG_LISTEN 下 `chirp_valid=0`，于是 `dac_data_reg` 保持 `8'd128`，DAC 输出零电平——这正是「发完就静默、转入接收监听」的物理体现。这个「valid 门控 + 中点回零」的设计保证了监听期 DAC 不会泄漏任何波形能量。

> 待本地验证：若用仿真，可在 testbench 里把 `dac_data` 与 `current_state` 一起 dump 成 VCD，观察 LONG_CHIRP 段 `dac_data` 跟随 `long_chirp_lut.mem`、其余段稳定在 `0x80`(=128)。

#### 4.2.5 小练习与答案

**练习：** 为什么 ODDR 的数据脚用 `D1=D2=dac_data_reg[i]`（两个输入相同），而不是真正用 DDR 的双沿送不同数据？

**参考答案：** 因为本设计是单数据率（SDR）——每个 `clk_120m` 周期送一个 8 位样本。用 ODDR 只是为了**把输出寄存器打进 IOB**（获得近零偏斜、固定延迟），并不是要用 DDR 传两倍数据。`D1=D2` 让 ODDR 在两沿都输出同一个值，等效 SDR，但物理位置在 IOB，这是 Xilinx 推荐的「源同步输出」做法。

### 4.3 发射机顶层与 ADAR1000 / 混频器协同

#### 4.3.1 概念说明

`radar_transmitter` 是发射链的「接线盒」。它的职责不是算波形，而是解决三类工程问题：

1. **跨时钟域（CDC）。** STM32 的触发信号（`new_chirp / new_elevation / new_azimuth / mixers_enable`）是异步 GPIO，需要安全地搬进 FPGA 时钟域。其中 `new_chirp` 是单周期脉冲，要用 toggle-CDC（u3-l2 讲过原理，这里看应用）。
2. **电平转换穿通。** STM32（3.3 V Bank）和 ADAR1000（1.8 V Bank）的 SPI 总线通过 FPGA 的 I/O bank 做电压翻译，FPGA 内部只是把信号「穿过去」。
3. **把控制器与 DAC 接口连起来。** 把 CDC 后的脉冲接到 `plfm_chirp_controller_enhanced`，把控制器产出的 `chirp_data/chirp_valid` 接到 `dac_interface_enhanced`。

⚠️ **关于 ADAR1000 的 load 信号——一个必须澄清的事实。** 本讲主题里提到「驱动 ADAR1000 load 信号」，但**在当前 HEAD 的实现里，8 条 `adar_tx_load_*` / `adar_rx_load_*` 信号全部被硬连线拉低（`1'b0`）**（[plfm_chirp_controller.v:92-99](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L92-L99)），源码注释写得很直白：`ADAR1000 pull to ground tx and rx load pins if not used`。也就是说，当前版本**并未在发射时序里主动驱动 load 引脚**。发射期真正被驱动的是：

- `adar_tr_1..4`（TR 切换位）：发 chirp 时置 `4'b1111`（[plfm_chirp_controller.v:283](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L283)、[plfm_chirp_controller.v:306](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L306)）；
- `tx_mixer_en / rx_mixer_en`（混频器分时使能）；
- `rf_switch_ctrl`（RF 开关切到发或收）。

ADAR1000 的相位权值装载是由 STM32 那侧通过 SPI 直接写寄存器完成的（见 u7-l3 的 `ADAR1000_Manager`），FPGA 这条 load 通路目前是「留位但接地」的预留接口。读源码时一定要以代码为准，不要被「load 信号」这个说法误导成「FPGA 在发射时主动 load 相位」。

#### 4.3.2 核心流程

`radar_transmitter` 内部的数据/控制流如下：

```
STM32 GPIO (异步)
   │
   ├─ stm32_new_chirp ─▶ cdc_single_bit(2级) ─▶ edge_detector ─▶ new_chirp_pulse (clk_100m)
   │                                                      │
   │                                                      ▼  toggle-CDC (100m→120m)
   │                                                 new_chirp_pulse_120m ──┐
   │                                                                      │
   ├─ stm32_mixers_enable ─▶ cdc_single_bit(3级, →120m) ─▶ mixers_enable_120m ┤
   │                                                                      │
   ├─ stm32_new_elevation ─▶ cdc(2级)+edge ─▶ new_elevation_pulse ──┐        │
   ├─ stm32_new_azimuth  ─▶ cdc(2级)+edge ─▶ new_azimuth_pulse  ──┐  │        │
   │                                                              │  │        │
   │   plfm_chirp_controller_enhanced (clk_120m FSM) ◀────────────┴──┴────────┘
   │            │ chirp_data/chirp_valid
   │            ▼
   │   dac_interface_enhanced ─▶ dac_data/dac_clk/dac_sleep
   │
   └─ SPI 3.3V ⇄ 1.8V 电平转换穿通（FPGA bank 做电压翻译）
```

- 三个 STM32 脉冲输入先各自经 2 级 `cdc_single_bit` 同步到 `clk_100m`，再做边沿检测，避免亚稳态导致 XOR 边沿检测出毛刺（注释解释了为什么不能省同步器，[radar_transmitter.v:155-181](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L155-L181)、[radar_transmitter.v:185-204](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L185-L204)）。
- `new_chirp_pulse` 是 100M 域的 1 拍脉冲，要送进 120M 的 FSM 启动它，所以再走一次 **toggle-CDC**：源域翻转电平 → 3 级同步 → 目的域 XOR 还原脉冲（[radar_transmitter.v:114-144](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L114-L144)）。这正是 u3-l2 讲的「单拍脉冲必须用 toggle-CDC」的真实用法。
- `mixers_enable` 是电平信号，用 3 级 `cdc_single_bit` 同步到 120M 即可（[radar_transmitter.v:147-153](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L147-L153)）。
- SPI 穿通是纯组合 `assign`，把 3.3 V 侧的 sclk/mosi/cs 直通到 1.8 V 侧、miso 反向直通（[radar_transmitter.v:83-93](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L83-L93)）。

#### 4.3.3 源码精读

- 顶层端口与 SPI 穿通：[radar_transmitter.v:21-93](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L21-L93)。注意 SPI 的 mosi/sclk/cs 都是「3v3 ↔ 1v8」成对出现，FPGA bank 做电平翻译，内部仅穿通。
- toggle-CDC 把 chirp 脉冲搬到 120M：[radar_transmitter.v:114-144](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L114-L144)。还原脉冲用的是 `chirp_toggle_120m ^ chirp_toggle_120m_prev`（XOR 边沿检测）。
- 例化控制器（把 CDC 后的信号接进去）：[radar_transmitter.v:207-237](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L207-L237)。注意端口名 `.new_chirp(new_chirp_pulse_120m)`、`.mixers_enable(mixers_enable_120m)`——接的是 CDC 之后的版本，不是原始 GPIO。
- 顶层 `radar_system_top` 把它例化为 `tx_inst`，端口对接见 [radar_system_top.v:435-496](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L435-L496)，其中 DAC 域用 `sys_reset_120m_n`、边沿检测/CDC 用 `sys_reset_n`（100M），复位域各归各位（呼应 u3-l2 的「每域各一套同步器」）。

#### 4.3.4 代码实践

**实践目标（调用链追踪型）：** 追踪一个 `new_chirp` 脉冲从 STM32 GPIO 一路走到 FSM 启动的完整 CDC 链，回答「这个脉冲穿过了几个时钟域、各用了什么同步方式」。

**操作步骤：**

1. 从顶层输入 `stm32_new_chirp`（[radar_system_top.v:452](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L452)）出发。
2. 进入 `radar_transmitter` 后第一站：`cdc_single_bit #(.STAGES(2))` → `stm32_new_chirp_sync`（[radar_transmitter.v:159-165](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L159-L165)）。
3. 第二站：`edge_detector_enhanced` 产出 `new_chirp_pulse`（clk_100m 域 1 拍脉冲，[radar_transmitter.v:185-190](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L185-L190)）。
4. 第三站：toggle-CDC 翻转 → 3 级同步 → XOR 还原，得到 `new_chirp_pulse_120m`（[radar_transmitter.v:118-144](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L118-L144)）。
5. 终点：接到控制器的 `.new_chirp(...)`（[radar_transmitter.v:211](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L211)），在 120M FSM 里触发 IDLE→LONG_CHIRP。

**需要观察/回答的现象：**

- 这个脉冲穿过了 **异步 GPIO → clk_100m → clk_120m** 三个域。
- 100M 段用了 2 级电平同步 + 边沿检测；100M→120M 段用了 toggle-CDC（因为目标是把单拍脉冲安全搬域）。
- **追问：** 为什么 `mixers_enable` 不用 toggle-CDC、只用 3 级电平同步？因为它是**电平**信号（长时间稳定），不是单拍脉冲，电平同步器就够，toggle-CDC 反而多余（见 u3-l2 的选型原则）。

**预期结果：** 你能画出一条「GPIO → 2级sync → edge → toggle → 3级sync → XOR → FSM」的链路图，并能解释每一级存在的理由。这是雷达这种多时钟域系统里最典型、也最值得反复练习的读图题。

> 待本地验证：跑跨层契约测试（u11-l3）时，这条 CDC 链是「Python↔Verilog↔C」三层验证的对象之一。

#### 4.3.5 小练习与答案

**练习 1：** 如果把 `new_chirp` 的 toggle-CDC 换成普通 2 级电平同步器，会发生什么？

**参考答案：** `new_chirp_pulse` 在 100M 域只有 1 拍宽（10 ns），120M 采样周期约 8.33 ns——虽然 120M 更快，但「电平同步」语义上只保证稳定信号搬移，不保证脉冲语义；更危险的是如果时序略有偏移或后续还要再降速，单拍脉冲可能被采样到 0 次或 2 次，导致丢帧或重帧。toggle-CDC 把「脉冲」编码成「电平翻转」，对采样次数不敏感，还原时再用边沿检测恢复成 1 拍脉冲，这才是脉冲跨域的标准做法。

**练习 2：** 发射期 ADAR1000 的相位权值是谁装载的？FPGA 的 load 引脚为何接地？

**参考答案：** 当前实现里相位权值由 STM32 通过 SPI 直接写 ADAR1000 寄存器装载（u7-l3 的 `ADAR1000_Manager`）。FPGA 的 `adar_tx/rx_load_*` 是预留引脚，本设计未使用，故硬拉低避免悬空。FPGA 在发射时实际控制的是 TR 切换位（`adar_tr_*`）和 RF 开关/混频器使能，而非相位装载。

## 5. 综合实践

**任务：为一帧发射「画一张完整时序甘特图」。**

把本讲三个模块串起来，画出**一个完整 chirp 帧**（从 IDLE 到回到 IDLE）期间，下列信号随时间的电平/取值变化（用纸笔画出时间轴，单位用 µs）：

- `current_state`（标注 IDLE / LONG_CHIRP / LONG_LISTEN / GUARD_TIME / SHORT_CHIRP / SHORT_LISTEN / DONE 各段时长）
- `chirp_valid`（高电平区间）
- `dac_data`（在 valid 高时是「长/短 chirp 波形」，valid 低时是 `0x80`）
- `tx_mixer_en` 与 `rx_mixer_en`（互补）
- `rf_switch_ctrl`（发=1，收=0）
- `chirp_counter`（0→15 阶梯，guard 期间跳到 16，再 16→31）

**要求：**

1. 标出长段的 16 次循环和短段的 16 次循环。
2. 在图上标出 `new_chirp_frame` 脉冲的位置（帧起始）。
3. 写出该帧的总时长（约 5.65 ms）。
4. 用一句话解释：为什么这张图里 `tx_mixer_en` 和 `rf_switch_ctrl` 的高电平区间完全重合？答案要回到源码——因为二者都由「`current_state == LONG_CHIRP || SHORT_CHIRP`」派生（[plfm_chirp_controller.v:86-87](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L86-L87) 与 [plfm_chirp_controller.v:282](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/plfm_chirp_controller.v#L282)）。

**进阶（可选）：** 把这张时序图与 u4-l4 的 Doppler「双 16 点子帧」对照——你能指出长段 16 chirp 对应 Doppler 子帧 0、短段 16 chirp 对应子帧 1 吗？这就把发射节拍与接收处理闭环了。

## 6. 本讲小结

- 发射链入口是 `radar_transmitter`（接线盒）→ `plfm_chirp_controller_enhanced`（状态机核心）→ `dac_interface_enhanced`（DAC 输出级）三级，外加 SPI 电平转换穿通。
- chirp 状态机用 7 个状态产生 **16 长 + guard + 16 短 = 32 chirp/帧** 的节拍，这是 u4-l4 双 16 点 Doppler 子帧的来源。
- 长 chirp 时长 = `T1_SAMPLES / FS = 3600 / 120 MHz = 30 µs`；长段 PRI ≈ 167 µs，对应最大非模糊距离 ≈ 25 km。
- 长 chirp 波形来自 `long_chirp_lut.mem`（3600×8 位 BRAM，同步读），短 chirp 是内联 8 位 LUT；`short_chirp_i/q.mem` 是接收端匹配滤波的高精度参考，不在发射通路上。
- DAC 接口用 ODDR 原语转发时钟与打包数据到 IOB，`chirp_valid` 做使能、低有效时回中点 `0x80`。
- `new_chirp` 脉冲经「2 级电平同步 + 边沿检测 + toggle-CDC」从异步 GPIO 安全搬进 120M FSM；`mixers_enable` 电平信号只用 3 级电平同步。
- ⚠️ 当前 HEAD 里 ADAR1000 的 `adar_tx/rx_load_*` **全部硬拉低**，发射期真正驱动的是 `adar_tr_*` TR 位 + RF 开关 + 混频器使能；相位装载由 STM32 经 SPI 完成。

## 7. 下一步学习建议

- **顺接接收端：** 读 u4-l2（匹配滤波）与 u4-l4（Doppler），把本讲的「16 长 + 16 短」节拍与接收端的「双 16 点子帧 FFT」对照，看清收发节拍的对称关系。
- **扫描节拍全貌：** 继续 u5-l2（雷达模式控制器），看 `elevation_counter / azimuth_counter`（31 仰角 × 50 方位）如何被组织成完整扫描，以及运行时配置如何覆盖编译期参数。
- **ADAR1000 与 PA 偏置：** 转 u7-l3（ADAR1000 波束赋形与 Idq 校准），看 STM32 那侧如何经 SPI 装载相位、并用 DAC5578/ADS7830 闭环校准 PA 的 Idq。
- **CDC 深入：** 若想彻底搞懂本讲反复出现的 toggle-CDC，回头精读 u3-l2 的同步器原理与 `cdc_modules.v`。
- **测试视角：** 本讲的 CDC 链与 BRAM 装载都是 u11-l1（FPGA 回归）与 u11-l3（跨层契约测试）的覆盖对象，学完测试篇可以回来验证你对时序的理解。
