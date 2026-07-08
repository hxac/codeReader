# 二次开发与系统扩展

## 1. 本讲目标

本讲是整本学习手册的收官篇。前面二十几讲我们一直在「读懂」这套 FPGA-FOC 电流环——它的每一条数据通路、每一个定点技巧、每一种时序约定。本讲的目标是反过来：**学会「改」和「加」**。

学完后你应该能够：

1. 说清楚 `foc_top` 对外暴露的可移植接口边界——哪些信号是「契约」，改什么会让蓝色核心崩溃，改什么只是在它外面「套壳」。
2. 把现在这个**电流环（扭矩环）**当成最内环，在它之上叠加**速度环**、**位置环**，构成经典的级联（串级）控制，并知道每多一环要新增哪些逻辑、能复用哪些已有模块。
3. 在更换角度传感器 / ADC 芯片型号时，知道只需重写粉色外设控制器，并满足 `foc_top` 的脉冲握手时序即可，蓝色核心一行不动。
4. 评估「纯 FPGA」与「MCU+FPGA 协同」两种二次开发路线，以及多通道电机扩展的可行性。

本讲的实践任务（见第 5 节）是**给电流环外加一个速度环**——这是从「让电机来回正反转」走向「让电机按指定转速稳态运行」的关键一步。

## 2. 前置知识

本讲默认你已经读完了 u2-l1（`foc_top` 全景）和 u3-l3（UART 监视器与用户逻辑）。开始前请回忆这几个关键结论：

- **电流环 = 扭矩环**：在 `id_aim≈0` 的前提下，`iq` 直接代表电磁扭矩，`iq>0` 一个方向、`iq<0` 反方向。示例程序里电机来回正反转，就是因为 `iq_aim` 在 ±200 间周期切换（见 u3-l3）。
- **统一节拍**：控制频率 = `clk/2048`（36.864MHz 下约 18kHz，周期约 55.6µs）。`en_idq` 每个控制周期产生一个高电平脉冲，它是整条流水线对齐外部世界的「心跳」。
- **i_en / o_en 脉冲握手**：模块之间用单周期高电平脉冲传递「数据有效」，这是全库统一的接口约定。
- **三色分区**：粉色＝硬件相关外设（`i2c_register_read`、`adc_ad7928`），蓝色＝硬件无关 FOC 核心算法，黄色＝用户自定义逻辑（`fpga_top` 里那段 `cnt` 计数器）。

此外补充一个本讲要用、但前面没专门强调的控制理论术语：

- **级联控制（cascade control）**：把多个控制器串成嵌套的环。最内环跑得最快、最外环跑得最慢；外环的**输出**就是内环的**目标值（aim）」。在本项目里，电流环是内环，它的目标值 `iq_aim` 就是「外环的输出」。这正是本讲所有扩展的切入点。

## 3. 本讲源码地图

本讲主要围绕三个文件展开，它们共同划定了「什么能改、什么不能改」的边界：

| 文件 | 在本讲中的作用 |
| :--- | :--- |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | 蓝色核心的**对外接口**就在它的端口表里。我们要逐个端口判断：哪些是「契约」、哪些是「扩展点」。 |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | 黄色用户逻辑的所在地。第 136–154 行那段让 `iq_aim` 来回切换的代码，就是本讲所有扩展要「替换掉」的对象。 |
| [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) | 作者关于「纯 FPGA / MCU+FPGA」「多路扩展」「可移植性」的原话，是我们讨论扩展方向的依据。 |

另外，实践任务会复用一个老朋友——[RTL/foc/pi_controller.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v)，它将被「第三次例化」充当速度环的 PI 控制器。

## 4. 核心概念与源码讲解

本讲按「先认清接口边界 → 再往外叠环 → 再换外设 → 最后谈协同与多路」四步展开。

### 4.1 电流环的抽象边界：foc_top 的可移植接口

#### 4.1.1 概念说明

要做任何二次开发，第一件事都是搞清楚「我手里的这个模块，对外承诺了什么」。`foc_top` 是一个**完整的电流环 + SVPWM**，它把自己封装成一个「黑盒」：你给它角度、给它电流采样、给它目标电流，它吐出三相 PWM。它的端口表就是这张「契约」。

关键认知：**蓝色核心（`foc_top` 及其下属 8 个文件）是「固定功能、一般不需要改动」的**——这是 README 在代码文件表里反复标注的原话（见 [README.md:L439-L446](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L439-L446)）。所有的二次开发，原则上都应在 `foc_top` **之外**进行，而不是去改它内部。

#### 4.1.2 核心流程

把 `foc_top` 的端口按「方向 + 角色」分成四组，就能一眼看出扩展点在哪：

```
              ┌──────────────────────── foc_top (电流环黑盒) ────────────────────────┐
              │                                                                        │
  角度 ──▶   phi[11:0]          (input,  机械角度，由粉色 I2C 提供)                  │
              │                                                                        │
  采样握手 ─ sn_adc (output) ──▶ 命令 ADC 采样                                       │
              en_adc (input)  ◀── ADC 报「结果好了」                                  │
              adc_a/b/c (input) ◀ 三相 ADC 原始值                                     │
              │                                                                        │
  PWM  ──▶  pwm_en, pwm_a/b/c (output, 6 个 MOS 管的驱动)                            │
              │                                                                        │
  监测 ──── en_idq (output) ◀── 每控制周期一拍，指示 id/iq 已更新                     │
              id, iq (output, signed 16bit)  ◀── 实际电流（供观测 / 供外环反馈）       │
              │                                                                        │
  ┌─扩展点─┤ id_aim, iq_aim (input, signed 16bit) ◀── 目标电流（外环/用户逻辑注入） │  ← 关键！
  │        │  Kp, Ki (input, 31bit)           ◀── PI 增益（运行时可调）              │
  │        │  init_done (output)              ◀── 标定结束 = 全链路 rstn             │
  │        └────────────────────────────────────────────────────────────────────────┘
  │
  └─▶ 这一组就是「外环 / 用户逻辑」的接入位置：你只需改变 id_aim/iq_aim 的来源，
      就能从「扭矩控制」升级到「速度控制」「位置控制」，核心算法一行不改。
```

四组端口的角色：

| 组别 | 信号 | 角色 | 改动风险 |
| :--- | :--- | :--- | :--- |
| 角度输入 | `phi` | 转子机械角度，电角度换算的原料 | 改来源（换传感器）安全，改位宽需谨慎 |
| 采样握手 | `sn_adc`/`en_adc`/`adc_a/b/c` | 「同步读入 3 通道」抽象 | 换 ADC 时必须严格满足此时序契约 |
| PWM 输出 | `pwm_en`/`pwm_a/b/c` | 6 管驱动 | 一般不动，除非换驱动板拓扑 |
| **控制目标 / 反馈** | `id_aim`/`iq_aim`/`Kp`/`Ki`/`id`/`iq`/`en_idq`/`init_done` | **本讲主战场** | 这里正是「套壳」的地方 |

最关键的一条结论：**`iq_aim` / `id_aim` 是 `input` 端口**——也就是说，目标电流是「从外面喂进来」的。示例程序里喂它的是一个 24 位计数器的最高位（让它在 ±200 间切换）；本讲要做的，就是把「喂它的人」从一个计数器换成一个速度环。这就是全部扩展的实质。

#### 4.1.3 源码精读

先看 `foc_top` 端口表里「控制目标」这一节，确认它们确实是 `input`：

[RTL/foc/foc_top.v:L40-L44](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L40-L44) —— `id_aim`、`iq_aim` 被声明为 `input wire signed [15:0]`，注释明确写着「目标电流值，可正可负」。注意 `iq_aim` 的注释「若正代表逆时针，则负代表顺时针」——这告诉我们 `iq_aim` 的符号直接决定转向，速度环输出的 `iq_aim` 天然带方向。

再看这个目标是在哪里被「消费」的——电流环 PI 控制器：

[RTL/foc/foc_top.v:L180-L190](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L180-L190) —— `u_iq_pi` 把 `iq_aim` 接到 `i_aim`、`iq` 接到 `i_real`、`en_idq` 接到 `i_en`。也就是说，**每个 `en_idq` 脉冲，电流环 PI 就「采样」一次 `iq_aim` 作为本周期目标**。这给外环指明了节拍：外环只要在 `en_idq` 节拍上更新 `iq_aim`，电流环就会忠实地跟随。

然后转到 `fpga_top`，看示例程序是怎么「喂」目标的——这正是本讲要替换的黄色用户逻辑：

[RTL/fpga_top.v:L136-L154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L136-L154) —— 一个 24 位自增计数器 `cnt`，按 `cnt[23]` 让 `iq_aim` 在 +200/-200 间切换，`id_aim` 恒 0。`cnt[23]` 每 2²³ 个时钟翻转一次，在 36.864MHz 下约每 0.23 秒换一次向。**本讲后续所有「级联控制」的改造，本质都是把第 146–154 行这个 `always` 块换掉**，让 `iq_aim` 来自一个速度 PI，而不是来自一个计数器。

最后，确认「反馈量」是否齐全。速度环需要转速反馈，而转速可以由机械角度 `phi` 差分得到——`phi` 在顶层已经是一根现成的 wire：

[RTL/fpga_top.v:L34-L46](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L34-L46) —— `phi`、`en_idq`、`id`、`iq`、`id_aim`、`iq_aim` 全都在顶层 `wire` 出来了。这意味着外环所需的一切（节拍 `en_idq`、角度 `phi`、可选的电流反馈 `iq`）在顶层都已经可用，**外环可以完全在 `fpga_top` 这一层实现，根本不用碰 `foc_top` 内部**。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「端口即契约」的直觉，判断每个信号改动的风险。
2. **操作步骤**：打开 [RTL/foc/foc_top.v:L18-L45](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L18-L45)，把端口逐个填入 4.1.2 那张表的某一组。
3. **需要观察的现象**：你会发现没有任何一个端口是「为了外环而专门留」的——但 `iq_aim`/`id_aim` 作为 input，加上 `phi`/`en_idq`/`iq` 作为可观测 output，天然就构成了一个完美的「外环接入面」。
4. **预期结果**：你能用一句话回答「为什么本项目不用改任何蓝色代码就能加速度环？」——因为目标电流是输入端口，节拍和反馈量都是输出端口。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `iq_aim` 从 16 位有符号改成 32 位有符号，会破坏什么？
  - **答**：会破坏 `pi_controller` 对 `i_aim` 的位宽约定（它是 `signed [15:0]`，见 [pi_controller.v:L15](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L15)），进而破坏整条定点链。所以端口位宽属于「契约」，不能随便改。
- **练习 2**：`en_idq` 是 output，`iq_aim` 是 input，两者方向相反却配合工作，这说明了什么设计思想？
  - **答**：说明 `foc_top` 把「我什么时候更新了 id/iq」通过 `en_idq` 告诉外部，外部再把「下一步要多少电流」通过 `iq_aim` 喂回来——这是一种**以脉冲节拍对齐的、解耦的 producer/consumer 接口**，外部逻辑无需知道内部流水线有多深。

### 4.2 级联控制：在 iq_aim 上叠加速度环与位置环

#### 4.2.1 概念说明

电流环控制的是**扭矩**（`iq`）。但实际应用里，我们更常想要的是**「让电机以指定转速 n 转/分运行」**（速度控制），或者**「让电机转到指定角度」**（位置控制，如机械臂关节）。这就需要在电流环外面再套环：

- **速度环**：目标是转速 `ω_aim`，反馈是实测转速 `ω_real`，输出是 `iq_aim`（喂给内环电流环）。
- **位置环**：目标是角度 `θ_aim`，反馈是实测角度 `θ_real`，输出是 `ω_aim`（喂给中环速度环）。

于是构成三层嵌套：**位置环 → 速度环 → 电流环**。越内越快、越外越慢；每一环的输出都是下一环的目标。这是伺服控制的经典结构，本项目已经做好了最内环，剩下两环是我们自己加。

转速反馈从哪来？不用加新传感器——**对 `phi` 做时间差分就能得到机械转速**。这是「有感 FOC」的红利：既然已经有一个 12 位磁编码器在持续读角度，转速就是角度的导数。

#### 4.2.2 核心流程

**(a) 转速估计（差分法）**

每个 `en_idq` 脉冲锁存一次 `phi`，相邻两次的差值就是「一个控制周期内转子转过的机械角度」，它正比于转速：

\[
\omega_{\text{mech}} \;=\; \frac{\Delta\varphi}{T_{\text{ctrl}}}\cdot\frac{2\pi}{4096}\quad [\text{rad/s}],\qquad T_{\text{ctrl}}=\frac{2048}{f_{\text{clk}}}
\]

工程上更常用转/分（RPM）：

\[
n\;=\;\Delta\varphi\;\cdot\;\frac{f_{\text{ctrl}}}{4096}\;\cdot\;60,\qquad f_{\text{ctrl}}=\frac{f_{\text{clk}}}{2048}
\]

把默认参数代进去（`f_clk=36.864MHz`，`f_ctrl=18kHz`）：`n ≈ Δφ × 263.7`。也就是说，若某个控制周期内 `Δφ=11`，对应转速约 2900 RPM——和 README FAQ 里「3000r/min、极对数 7」的电机量级吻合。

⚠️ **回绕修正（wraparound）**：`phi` 是 12 位无符号，范围 0~4095。当转子正向转过 0/4095 边界（如从 4080 转到 10），直接相减会得到 −4070 这样的大负数。必须修正：若 `Δφ > +2048` 就减 4096，若 `Δφ < −2048` 就加 4096，把它折叠回 ±2048 内的真实小位移。

**(b) 速度 PI（直接复用 pi_controller）**

转速估出来后，速度环就是又一个 PI：

\[
\texttt{iq\_aim} \;=\; K_{p,\omega}\cdot(\omega_{\text{aim}}-\omega_{\text{real}}) \;+\; K_{i,\omega}\cdot\sum(\omega_{\text{aim}}-\omega_{\text{real}})
\]

这个公式和电流环 PI **完全同构**——所以我们可以直接把 `pi_controller.v` **第三次例化**，把它的 `i_aim` 接 `ω_aim`、`i_real` 接 `ω_real`、`i_en` 接 `en_idq`、`o_value` 接 `iq_aim`。这就是模块化设计最甜的果实。

**(c) 节拍与延迟**

电流环 PI 在 `en_idq` 那一拍采样 `iq_aim`；而速度 PI 也用 `en_idq` 当 `i_en`，其输出要经过 5 级流水线（见 [pi_controller.v:L62-L75](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L62-L75)）才稳定。所以第 K 个控制周期的 `iq_aim`，实际上是基于第 K−1 周期的转速误差算出来的——这是数字级联控制器天然的**一个采样周期延迟**，对慢得多的速度环完全无害，无需消除。

**(d) 整体数据流**

```
   θ_aim ──▶ [位置PI] ──ω_aim──▶ [速度PI] ──iq_aim──▶ ┌────────────┐
                        ▲                            ┌─▶│  foc_top   │──▶ PWM ──▶ 电机
                        │ ω_real                     │  │ (电流环)   │
                   ┌────┴─────┐    iq                │  └────────────┘
                   │ 转速估计 │ ◀───────────────────┘        │
                   │ dφ/dt    │                               │ phi
                   └────▲─────┘                               │
                        │ phi, en_idq  ◀───────────────────────┘
```

#### 4.2.3 源码精读

**复用点 1：pi_controller 的端口正好够用。** 看 [RTL/foc/pi_controller.v:L9-L19](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L9-L19) —— `i_aim`/`i_real`/`i_en`/`o_value` 全是通用接口，没有任何「电流专属」的耦合。把它例化成速度 PI 时，端口连接方式与 `foc_top` 里 [L180-L190 的 u_iq_pi](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L180-L190) 几乎一模一样，只是 `i_aim` 从 `iq_aim` 换成 `omega_aim`、`i_real` 从 `iq` 换成 `omega_real`、`o_value` 从 `vq` 换成 `iq_aim`。

**复用点 2：节拍 en_idq 现成。** [RTL/fpga_top.v:L42](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L42) 已把 `en_idq` 声明为顶层 wire，且 [RTL/fpga_top.v:L126](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L126) 把它从 `foc_top` 引出。它就是速度环的天然时钟节拍。

**复用点 3：phi 现成。** [RTL/fpga_top.v:L34](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L34) 的 `phi` 来自 AS5600 的持续读取（[L60-L73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L60-L73) 的 `i2c_register_read`，`start=1'b1` 一直在读）。我们只需在每个 `en_idq` 对它采样即可，无需新增任何传感器。

**替换点：把计数器换成速度 PI。** 对照 [RTL/fpga_top.v:L146-L154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L146-L154) 那段让 `iq_aim` 来回切换的 `always`，把它整体替换成「速度 PI 的输出驱动 `iq_aim`」即可。`iq_aim` 仍是一个 `reg signed [15:0]`，只是驱动来源变了。

下面是一段**示例代码（非项目原有）**，演示转速估计器的写法（仅供设计参考，未经综合验证）：

```verilog
// ===== 示例代码（非仓库原有）：基于 φ 差分的机械转速估计 =====
// 功能：每个 en_idq 节拍，输出本控制周期内转子的机械角增量（正比于转速）。
// 注意：本段为讲解用伪 RTL，需读者自行综合与时序验证。
reg         [11:0] phi_q1;            // 上一控制周期锁存的机械角度
reg  signed [12:0] dphi_raw;          // 原始差分（带符号）
reg  signed [15:0] omega_real;        // 估计转速，单位：counts/控制周期，可正可负

always @ (posedge clk or negedge rstn) begin
    if(~rstn) begin
        phi_q1 <= 12'd0;  dphi_raw <= 13'sd0;  omega_real <= 16'sd0;
    end else if(en_idq) begin
        dphi_raw <= $signed({1'b0, phi}) - $signed({1'b0, phi_q1});  // φ(当前) − φ(上一周期)
        // 回绕修正（折叠到 ±2048）：
        //   若 dphi_raw >  +2048 → omega_real = dphi_raw − 4096
        //   若 dphi_raw <  −2048 → omega_real = dphi_raw + 4096
        //   否则                 → omega_real = dphi_raw
        phi_q1 <= phi;                // 为下一周期保存本次角度
    end
end
```

> 上面把回绕修正写成注释是为了突出主流程；实际实现里可以写一个 `function`，或在 `en_idq` 的下一拍用组合逻辑对 `dphi_raw` 做条件加减。

#### 4.2.4 代码实践（设计型）

1. **实践目标**：写出速度环在 `fpga_top` 中的例化草图，体会「复用 + 替换」。
2. **操作步骤**：
   - 在 `fpga_top.v` 里新增 `wire signed [15:0] omega_aim;`（目标转速，先给个常数，如 `16'sd10`，对应约 2600 RPM）。
   - 加入上面的转速估计器，产出 `omega_real`。
   - 例化第三个 `pi_controller`（命名 `u_speed_pi`）：`i_en=en_idq`、`i_aim=omega_aim`、`i_real=omega_real`、`o_value` 接到一个新 wire `iq_aim_from_speed`。
   - 把 [L146-L154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L146-L154) 的 `iq_aim` 驱动改成 `assign iq_aim = iq_aim_from_speed;`（注意 `iq_aim` 需相应从 `reg` 改回 `wire`，或用 `always` 把速度 PI 输出转拍寄存）。
3. **需要观察的现象**：通电后电机应加速到并稳定在 `omega_aim` 对应的转速（空载时 `iq` 趋近 0），而不是像示例那样来回正反转。
4. **预期结果 / 待本地验证**：转速稳态误差应被速度 PI 的积分项消除；若电机抖动或失步，通常是 `Kp,ω`/`Ki,ω` 过大，需调小（速度环增益应远小于电流环增益）。**具体整定值待本地验证。**

#### 4.2.5 小练习与答案

- **练习 1**：为什么转速估计必须在 `en_idq` 节拍做，而不是每个 `clk` 都做？
  - **答**：因为 `phi` 的有效刷新节奏与控制周期同频；每个 `clk` 都做差分会把同一次角度读数重复相减得到 0，或引入亚周期噪声。锁在 `en_idq` 上才能保证 `Δt` 恒为 `T_ctrl`，`Δφ` 才线性对应转速。
- **练习 2**：若要再加一层位置环，复用点和新增点分别是什么？
  - **答**：复用 `pi_controller`（第四次例化）做位置 PI，`i_real` 接 `phi`（或它的缩放）、`o_value` 接 `omega_aim`；新增的只是目标位置 `theta_aim` 的来源（如 UART 命令或一个目标计数器）。`foc_top`、转速估计、速度 PI 全部不动。
- **练习 3**：速度环增益 `Ki,ω` 为什么通常要比电流环的 `Ki` 小一两个数量级？
  - **答**：因为外环带宽远低于内环。内环（电流）要在几百微秒内建立扭矩，外环（速度）受转子惯量限制、响应以毫秒计。增益过大会让外环比内环还快，导致失稳；这正是级联控制「外环必慢于内环」的原则。

### 4.3 更换传感器与 ADC：只重写粉色外设

#### 4.3.1 概念说明

本项目的蓝色 FOC 核心**完全不认识具体的传感器型号**——它只认两个抽象：一个 12 位机械角度 `phi`，一组「同步读入 3 通道」的 ADC 握手时序。所以当你想把 AS5600 换成别的磁编码器（如 AS5048A），或把 AD7928 换成别的 ADC（甚至 3 颗并行 ADC），**只需要重写粉色部分的控制器**，让它仍然产出符合契约的 `phi` 和 `sn_adc`/`en_adc`/`adc_a/b/c` 即可。这是作者反复强调的可移植性设计。

#### 4.3.2 核心流程

**换角度传感器**（`phi` 来源）：

```
  新传感器 ──[新控制器, 如 SPI/ABI/Resolver 解码]──▶ phi[11:0]  ──▶ foc_top
   (任意总线)                                          (契约:12位无符号)
```

契约只有一个：**持续产出 12 位无符号的机械角度 `phi`**，0 对应 0°、4095 对应几乎 360°。无论底层是 I2C、SPI、正交编码（ABI）还是旋变（Resolver），只要最后给出这个 12 位数即可。`i2c_register_read` 只是众多实现中的一种。

**换 ADC**（电流采样来源），契约是 5 个信号的时序：

```
  foc_top.sn_adc (1拍高) ──▶ [新ADC控制器] 开始采 3 相
                                          ... 串行或并行皆可 ...
  foc_top.en_adc (1拍高) ◀── [新ADC控制器] 同步提交 3 通道结果
  foc_top.adc_a/b/c       ◀── 必须在 en_adc 同一拍就绪
```

只要满足「`sn_adc` 命令开始 → 一段时间后 `en_adc` 脉冲 + 三相结果同时就绪」，蓝色核心就感知不到差别——这正是 README FAQ 里作者的原话。

**位宽适配**：若新传感器 >12 位（如 14 位），取高 12 位（低位截断）；若 <12 位（如 10 位），在低位补 0（低位填充）。README 在技术特点里明确写了这条规则。

#### 4.3.3 源码精读

**角度契约侧**：[RTL/i2c_register_read.v:L9-L22](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L9-L22) —— 它用 `SLAVE_ADDR`、`REGISTER_ADDR`、`CLK_DIV` 三个 parameter 适配具体芯片，对外只吐 `regout[15:0]`（其中低 12 位是角度）。换芯片时，要么改 parameter（同是 I2C 磁编码器），要么新写一个 `spi_angle_read.v` 替换整个模块——`fpga_top` 里 [L60-L73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L60-L73) 的例化保持输出接 `phi` 即可。

**位宽规则**：[README.md:L285-L287](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L285-L287) —— 「对于>12bit的传感器，需要进行低位截断。对于<12bit的传感器，需要进行低位填充。」

**ADC 契约侧**：[README.md:L566-L569](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L566-L569) —— 作者解释为何把 `sn_adc`/`en_adc`/`adc_a/b/c` 设计成「同步读入 3 通道」抽象：「这样的同步读入接口是 ADC 的一种高度抽象……如果用户用了其它 ADC 型号，只需按照这个时序的抽象来具象地编写 ADC 控制器即可，而 foc_top.v 并不关心拟用的是 1 个串行的 ADC 还是 3 个并行的 ADC，反正你都要给我同步提交。」并提醒用户必须自行核算 `hold_detect` 的延时 + ADC 采样耗时 < 采样窗口长度。

**fpga_top 侧的接法不变**：[RTL/fpga_top.v:L78-L100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100) 是 AD7928 的例化，换 ADC 时这 23 行整体替换为新控制器例化，但下方 [L118-L121](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L118-L121) 接到 `foc_top` 的 `.en_adc`/`.adc_a`/`.adc_b`/`.adc_c` 那几行一字不改。

#### 4.3.4 代码实践（设计型 + 阅读型）

1. **实践目标**：把「换型号」这件事量化成一张改动清单。
2. **操作步骤**：假设要把 ADC 换成「3 颗各自独立的并行 12 位 ADC（每相一颗，同时采样）」。请列出：
   - 新控制器需要对 `foc_top` 暴露哪些信号？（答：`sn_adc` 输入触发、`en_adc` 输出脉冲、`adc_a/b/c` 三路 12 位）
   - 相比 AD7928 方案，采样窗口约束是变松还是变紧？为什么？（提示：并行 ADC 不再需要串行采 3 次，`sn_adc→en_adc` 的耗时会短得多。）
3. **需要观察的现象 / 预期结果**：并行方案下 `sn_adc→en_adc` 的时钟周期数大幅下降，因此 `MAX_AMP` 可以调得更大（采样窗口可以更短仍能满足约束），电机可达最大力矩提升。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：换一个 SPI 接口的磁编码器，`foc_top` 需要改吗？
  - **答**：不需要。只需新写 `spi_angle_read.v`，在 `fpga_top` 里替换掉 `i2c_register_read` 的例化，输出仍接 `phi`。
- **练习 2**：14 位角度传感器怎么接到 12 位的 `phi` 上？
  - **答**：取高 12 位（`sensor[13:2]`），即「低位截断」。这会让角度分辨率从 14 位降回 12 位，但 `foc_top` 内部本就按 12 位角度运作（电角度 ψ 也是 12 位），无副作用。

### 4.4 MCU+FPGA 协同与多通道电机扩展

#### 4.4.1 概念说明

除了「在 FPGA 内部继续加逻辑」，还有两条更宏观的扩展路线，都是 README 明确点出的：

1. **MCU+FPGA 协同**：让 FPGA 专心做它最擅长的——确定性极高的实时电流环（`foc_top`），把需要灵活编程、复杂状态机、网络/文件系统的高层控制（速度环、位置环、轨迹规划、用户交互）交给 MCU。两者之间用 SPI/UART/I2C 传几个数（主要是 `iq_aim`/`omega_aim` 和反馈 `id`/`iq`）。
2. **多通道电机**：FPGA 天生并行，可以同时跑 N 个 `foc_top`，每个拖一台电机，互不干扰——这是相对 MCU「顺序执行」的巨大优势。

README 开篇就把这两点列为选 FPGA 的动机。

#### 4.4.2 核心流程

**(a) MCU+FPGA 的分工**

```
   ┌──────── MCU (如 STM32 / Arduino) ────────┐         ┌──────── FPGA ────────────────┐
   │  速度环 / 位置环 / 轨迹规划 / 通信上位机   │         │                              │
   │  运行你熟悉的 C/Arduino 代码，算出 iq_aim  │  SPI/   │  fpga_top                    │
   │  或 omega_aim                              │ ───────▶│   └─ foc_top (电流环, 硬实时)│
   │  接收 id/iq 反馈做监控                     │ ◀───────│      └─ PWM → 电机           │
   └───────────────────────────────────────────┘  UART   │                              │
                                                          └──────────────────────────────┘
```

此时 `fpga_top` 里第 4.2 节的「用户逻辑」就不再是 FPGA 内部的速度 PI，而是一个**通信从机**（如 SPI slave），把 MCU 发来的 `iq_aim` 写进一个寄存器，再接到 `foc_top.iq_aim`。蓝色核心依旧一行不改。

**(b) 多通道电机**

```
   fpga_top
     ├─ foc_top #(.POLE_PAIR(7))  u_motor0   ──▶ pwm0_a/b/c ──▶ 电机0
     │    ├─ i2c_register_read / spi_angle0  ── phi0
     │    └─ adc_ad7928 / parallel_adc0      ── adc0_a/b/c
     ├─ foc_top #(.POLE_PAIR(14)) u_motor1   ──▶ pwm1_a/b/c ──▶ 电机1
     │    ├─ ...                              ── phi1
     │    └─ ...                              ── adc1_a/b/c
     └─ ... (按需扩展)
```

每一路都是独立的一套 `foc_top` + 角度控制器 + ADC 控制器。FPGA 的并行硬件让 N 路电流环**真正同时**运行，而不是 MCU 那样分时切片——这正是「多路反馈协同」比 MCU 方案更稳的根本原因。

#### 4.4.3 源码精读

**MCU+FPGA 路线的依据**：[README.md:L274](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L274) —— 「借助本库，你可以进一步使用 **纯FPGA** 或 **MCU+FPGA** 的方式实现更复杂的电机应用。」英文版同义表述见 [README.md:L14](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L14)。

**多路扩展的依据**：[README.md:L272](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L272) —— 「使用 FPGA 实现的 FOC 可以获得更好的实时性，并且更方便进行**多路扩展**和**多路反馈协同**。」

**可移植性的总纲**：[README.md:L453-L460](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L453-L460) —— 四色分区里，粉色（外设）是硬件相关、可替换的；蓝色（FOC 核心）是硬件无关、复用的；黄色（用户逻辑）是自由发挥区。除 `altpll` 外全是纯 RTL，可跨厂商（Xilinx/Lattice）移植。这条总纲同时支撑了「换外设」「换 FPGA 厂商」「多路复制」三种扩展。

**多路复制时的注意点**：[RTL/fpga_top.v:L105-L132](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L105-L132) 这段 `foc_top` 例化是「复制单元」。多路扩展时为每路复制一份，并注意：① 每路的 `POLE_PAIR`/`MAX_AMP` 等 parameter 可各自不同（电机型号不同）；② 三相 PWM 引脚、SPI/I2C 引脚必须各自独立（不同物理引脚）；③ 若多路共用一颗 AD7928（它是 8 通道），可在 `adc_ad7928` 里把 `CH_CNT` 调大、用不同通道分配给不同电机，但这会让多路采样串行化、需重新核算采样窗口约束。

#### 4.4.4 代码实践（设计型）

1. **实践目标**：为 MCU+FPGA 方案设计 `fpga_top` 侧的最小改动。
2. **操作步骤**：
   - 在 `fpga_top` 中新增一个简易 SPI 从机寄存器（可新写 `spi_slave_reg.v`，或复用现成 IP），它接收 MCU 发来的 16 位 `iq_aim` 命令。
   - 把 [RTL/fpga_top.v:L146-L154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L146-L154) 的计数器逻辑替换为：`iq_aim` 由 SPI 从机寄存器的输出驱动。
   - 把 `iq`（[L44](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L44)）通过同一 SPI 从机回传给 MCU 做监控（或继续用现有的 `uart_monitor`）。
3. **需要观察的现象**：MCU 用 C 代码写一个速度环，每若干毫秒通过 SPI 下发新的 `iq_aim`，FPGA 电流环忠实跟随；电机转速受 MCU 速度环控制。
4. **预期结果 / 待本地验证**：稳态转速跟随 MCU 给定的 `omega_aim`。SPI 通信帧率与速度环带宽的匹配需实测整定。**具体参数待本地验证。**

#### 4.4.5 小练习与答案

- **练习 1**：MCU+FPGA 方案里，速度环放在 MCU 还是 FPGA？为什么这是合理的分工？
  - **答**：放 MCU。因为速度环慢（毫秒级）、但逻辑复杂（轨迹规划、状态机、通信），MCU 的 C 代码更好写好维护；而电流环快（55µs）、对确定性要求极高，FPGA 纯硬件流水线最稳。把「快而简单」留给 FPGA、「慢而复杂」留给 MCU，是经典分工。
- **练习 2**：用一颗 8 通道的 AD7928 同时带两台电机的电流采样，最大风险是什么？
  - **答**：串行化导致采样窗口约束变紧。两台电机各需 3 相 = 6 次采样，全在同一个采样窗口里串行完成，`sn_adc→en_adc` 耗时翻倍，可能挤爆采样窗口。更稳妥的做法是两台电机各配独立的采样通路（或两颗 ADC）。

## 5. 综合实践

**任务：给本电流环外加一个速度环，让电机按指定转速稳态运行。**

这是本讲所有内容的汇总。请按以下步骤完成一个完整的设计方案（画出模块框图 + 指出复用与新增）。

### 5.1 设计目标

把示例程序「`iq_aim` 在 ±200 间来回切换」改为「`iq_aim` 由速度环产出，使电机稳定在目标转速 `omega_aim`」。完成后面向串口绘图器应看到 `iq` 在稳态时趋近 0（空载无需扭矩维持转速），而转速维持恒定。

### 5.2 模块框图（请在笔记上画出）

```
                         ┌─────────────── fpga_top (黄色用户逻辑层) ───────────────────┐
                         │                                                              │
   omega_aim(常数/UART)─▶│ ┌────────────┐  iq_aim(reg)                                 │
                         │ │ 速度 PI     │───────────────────────┐                      │
                         │ │ u_speed_pi  │                       │                      │
                         │ │ (复用       │ ◀────omega_real──────┐ │                      │
                         │ │ pi_controller)                     │ │                      │
                         │ └────▲───────┘     ┌──────────────┐  │ │                      │
                         │      │             │ 转速估计器    │  │ │                      │
                         │      │             │ (新增, 差分)  │  │ │                      │
                         │      │             └────▲─────────┘  │ │                      │
                         │      │ omega_aim        │ en_idq,phi  │ │                      │
                         │      └──────────────────┼────────────┘ │                      │
                         │                         │              │                      │
                         │           ┌─────────────┼──────────────┼───────────┐          │
                         │           │             │              │           │          │
                         │           ▼             ▼              ▼           │          │
                         │   ┌─────────────────────────────────────────────┐  │          │
                         │   │ foc_top  (蓝色电流环, 一行不改)              │◀─┘          │
                         │   │   iq_aim, id_aim, Kp, Ki ──▶ 内部 PI ──▶ PWM │             │
                         │   │   phi ◀── i2c_register_read (复用)          │             │
                         │   │   en_adc/adc_a/b/c ◀── adc_ad7928 (复用)    │             │
                         │   │   en_idq, id, iq ──▶ (供观测与外环反馈)      │             │
                         │   └─────────────────────────────────────────────┘             │
                         │           │                                                   │
                         │           ▼ pwm_a/b/c/en                                      │
                         │           电机                                                 │
                         └──────────────────────────────────────────────────────────────┘
                                       ▲ uart_monitor 仍打印 id/id_aim/iq/iq_aim (可改成打印转速)
```

### 5.3 复用了哪些现有模块（一行不改或仅改例化）

| 复用对象 | 作用 | 是否改动 |
| :--- | :--- | :--- |
| `foc_top`（含其下 8 个蓝色文件） | 电流环 / 扭矩环，最内环 | **不改**，仅作为被控对象 |
| `i2c_register_read` + AS5600 | 提供 `phi`，速度环反馈的源头 | **不改** |
| `adc_ad7928` + AD7928 | 提供三相电流，电流环反馈 | **不改** |
| `pi_controller.v` | 充当**速度 PI**（第三次例化） | **不改**，只是又例化一份 |
| `uart_monitor` | 监视电流跟随曲线 | 可选改监视量为转速 |

### 5.4 需要新增哪些逻辑

| 新增对象 | 功能要点 |
| :--- | :--- |
| 转速估计器 `speed_estimator` | 在 `en_idq` 节拍锁存 `phi`、做带符号差分、修正 0/4095 回绕，输出 `omega_real` |
| 目标转速源 `omega_aim` | 先用常数（如 `16'sd10`），后续可接 UART 命令或 MCU |
| 速度 PI 例化 `u_speed_pi` | `i_en=en_idq`、`i_aim=omega_aim`、`i_real=omega_real`、`o_value→iq_aim` |
| 顶层连线调整 | 把 [fpga_top.v:L146-L154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L146-L154) 的计数器 `always` 删除，`iq_aim` 改由速度 PI 驱动 |

### 5.5 关键设计决策与验证清单

1. **节拍对齐**：转速估计与速度 PI 都用 `en_idq` 触发，保证 `Δt=T_ctrl` 恒定。
2. **回绕修正**：必须处理 `phi` 跨越 0/4095 边界，否则转子每转过一圈速度估计都会出现一个巨大尖峰。
3. **增益量级**：速度 PI 的 `Kp,ω`/`Ki,ω` 应远小于电流环的 `Kp`/`Ki`（小一两个数量级起步），外环带宽必须低于内环。
4. **延迟认知**：第 K 周期的 `iq_aim` 基于第 K−1 周期的转速误差（速度 PI 的 5 级流水线 + 一个采样延迟），这是正常的、无需消除。
5. **初始化保护**：转速估计器与速度 PI 的复位建议接 `init_done`（与 `foc_top` 内部子模块一致），保证标定 Φ 期间不输出错误的 `iq_aim`。
6. **验证手段**：先在 `omega_aim=0` 下确认电机能静止 hold（速度 PI 会输出小 `iq_aim` 抵消外加扰动）；再给非零 `omega_aim` 确认稳态转速。**由于本项目无电机 Verilog 模型（见 u4-l3），速度环闭环只能上板实测，无法整体仿真，转速与增益的具体数值待本地验证。**

## 6. 本讲小结

- **`foc_top` 的端口表就是契约**：`id_aim`/`iq_aim` 是 `input`、`phi`/`en_idq`/`id`/`iq` 是 `output`，这套「目标进、反馈出、脉冲节拍对齐」的接口天然构成了外环接入面，二次开发原则上只在 `foc_top` 之外「套壳」，蓝色核心一行不改。
- **电流环是最内环**：在 `iq_aim` 上叠加速度环（再叠位置环）即得级联控制；转速可由 `phi` 在 `en_idq` 节拍上差分得到，需做 0/4095 回绕修正。
- **PI 控制器是复用明星**：`pi_controller.v` 通用且无电流耦合，速度环/位置环只需把它再例化一两次，端口连接方式与电流环里的 `u_iq_pi` 几乎相同。
- **换传感器/ADC 只动粉色外设**：角度侧只需持续产出 12 位 `phi`；ADC 侧只需满足 `sn_adc→en_adc+三相结果同步就绪` 的 5 信号契约，蓝色核心不关心总线与型号。
- **两条宏观路线**：MCU+FPGA 协同（FPGA 跑硬实时电流环、MCU 跑慢而复杂的速度/位置/轨迹环）与多通道电机扩展（复制 `foc_top` 例化、FPGA 天然并行），均被 README 列为选 FPGA 的核心理由。
- **可移植性总纲**：除 `altpll` 外全库纯 RTL，粉色可替换、蓝色可复用、黄色可自由发挥——这条总纲同时支撑了换外设、换 FPGA 厂商、多路复制三种扩展。

## 7. 下一步学习建议

到这里，整本手册的 19 篇讲义已覆盖了从「项目是什么」到「如何二次开发」的完整链条。建议你按以下方向继续：

1. **动手做综合实践**：先实现 5.3/5.4 的速度环，再尝试加位置环让电机转到指定角度并 hold 住。这是把本手册从「读懂」变成「会做」的分水岭。
2. **重读蓝色核心，但带着「能否复用」的新视角**：回看 [pi_controller.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v)、[cartesian2polar.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v)，思考它们能否被你自己的其它控制项目（如 SMPS、伺服、平衡车）直接复用。
3. **补电机与控制理论**：手册刻意回避了电机本体模型。若要理解「为什么速度环增益要这样整定」「弱磁控制（`id_aim<0`）何时用」，建议读 README 参考资料 [6]~[9]（稚晖、上官致远的 FOC 知乎专栏、STM32 电机控制讲座）。
4. **跨平台移植实练**：把工程移植到一块 Xilinx 板（用 Clocking Wizard 替换 `altpll`，见 u4-l2），体会「纯 RTL + 一处 PLL 替换」的可移植承诺。
5. **回归 FAQ**：[README.md 的 FAQ](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L524) 浓缩了社区真实疑问（电流重构系数、采样窗口、串行采样误差），是检验你是否真正读通的试金石。

祝你在自己的电机项目里，把这套电流环用得得心应手。
