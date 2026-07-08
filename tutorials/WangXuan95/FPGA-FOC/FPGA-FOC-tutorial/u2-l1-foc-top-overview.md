# foc_top.v 全景与控制环路

## 1. 本讲目标

本讲是「FOC 核心数据流」单元的第一讲。读完本讲，你应该能够：

- 看懂 `foc_top.v` 的端口分组，知道哪些信号进、哪些信号出、它们各自的物理含义。
- 顺着信号流，说清楚电流矢量在三大坐标系（定子直角 αβ、转子直角 dq、极坐标）之间是如何一步步转换的。
- 掌握全库统一的 `i_en`/`o_en` 单周期脉冲握手约定，并能数清楚从 `en_adc` 到 `en_idq` 这条握手链经过了多少级流水线节拍。
- 理解「控制频率 = `clk`/2048 = 采样率 = PID 更新率 = SVPWM 占空比更新率」这个统一节拍为什么成立。

本讲只俯瞰全景、建立「数据流地图」，不展开 Clark/Park/SVPWM 等子模块的内部细节——那些是后续讲义（u2-l3 ~ u2-l8）的任务。

## 2. 前置知识

在进入本讲前，建议你先建立以下直觉（这些在 u1 系列讲义里已经讲过，这里只做一句话回顾）：

- **FOC 电流环**：直接控制 q 轴电流（决定扭矩），把 d 轴电流压到 0，从而高效驱动无刷/永磁同步电机。用 FPGA 实现是为了获得确定性、低延迟、可多路扩展的实时性。
- **foc_top 的定位**：它是「蓝色 FOC 固定算法」子树的根模块，自身几乎不含算法逻辑，主要靠 `wire` 连线 + 两个 `always` 块 + 一组模块例化，把 8 个硬件无关的子模块串成一条流水线。
- **坐标系直觉**（电机控制的核心）：
  - **三相 abc**：电机物理上三根线，看到的是三个交变电流。
  - **定子直角 αβ**：把三相「降维」成两路正交电流，仍然随时间正弦变化。
  - **转子直角 dq**：再旋转一个电角度 ψ，让电流「跟着转子转」，于是稳态下变成近似常数——这才是 PI 能控制的对象。
  - **极坐标 (ρ, θ)**：用一个幅值 + 一个角度表示同一个矢量，方便 SVPWM 直接生成 PWM。
- **节拍/握手**：模块之间不靠 valid/ready 双向握手，而是用一个「只持续一个时钟周期的高电平脉冲」表示「我这一拍算完了，数据有效，请下一级取用」。这是本项目最重要的工程约定之一。

> 对初学者：如果你对 Clark/Park 变换只是听过名字、不清楚公式，没关系。本讲只需要你记住「三相 → αβ → dq → 极坐标」这个顺序和它解决什么问题，公式细节后续讲义会逐个推导。

## 3. 本讲源码地图

本讲只围绕一个文件展开：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | FOC 核心算法的顶层，例化 8 个子模块 | 端口、内部坐标变量、脉冲握手链、统一节拍、初始化与反 Park |

为了数清握手节拍，本讲还会**引用**（但不深入）三个被 `foc_top` 例化的子模块，仅用于确认它们的流水线深度：

- `clark_tr.v`（Clark 变换，3 级流水线）
- `park_tr.v`（Park 变换，2 级流水线）
- `pi_controller.v`（PI 控制器，5 级流水线）

它们的内部原理分别在 u2-l3、u2-l4、u2-l5 讲解。

## 4. 核心概念与源码讲解

### 4.1 foc_top 模块：端口契约与三大坐标系的数据流

#### 4.1.1 概念说明

`foc_top` 是一个「**算法顶层**（IP's top）」：它本身不实现某个变换公式，而是把「角度换算」「电流重构」「Clark」「Park」「PI×2」「直角转极坐标」「反 Park」「SVPWM」「采样时机检测」这 9 段功能，用 `wire` 连线和模块例化串成一条完整的数据通路。

可以把 `foc_top` 理解成一个**管道**：原料（机械角度 φ + 三相 ADC 原始值）从一端灌进去，产物（三相 PWM）从另一端流出来，中间经历多次「坐标系变换」。学习这条管道的关键，是先认清**有哪些坐标系**、**它们之间的转换顺序**，以及**每段管道用哪个子模块实现**。

#### 4.1.2 核心流程

电流矢量从输入到输出，依次穿过三大坐标系：

```text
   三相 abc          定子直角 αβ          转子直角 dq
 (adc_a/b/c)  ──电流重构──> (ia/ib/ic) ──Clark──> (iα/iβ) ──Park──> (id/iq)
                                                                       │
                                                                       │  PI 控制
                                                                       ▼
 三相 PWM    <──SVPWM──  定子极坐标        <──反 Park──  转子极坐标   (vd/vq)
(pwm_a/b/c)             (Vsρ,Vsθ)                      (Vrρ,Vrθ)  ←─cartesian2polar
```

也就是说：

1. **采样与重构**：三相 ADC 原始值 → 三相电流 `ia/ib/ic`（仍在 abc 坐标系）。
2. **定子直角 αβ**：`ia/ib/ic` 经 Clark 变换得到 `iα/iβ`（定子直角坐标系）。
3. **转子直角 dq**：`iα/iβ` 经 Park 变换（旋转电角度 ψ）得到 `id/iq`（转子直角坐标系）——这是 PI 能控制的「直流化」电流。
4. **PI 控制出电压**：`id/iq` 与目标 `id_aim/iq_aim` 比较，PI 算出转子坐标系电压 `vd/vq`。
5. **转子极坐标**：`vd/vq` 经 `cartesian2polar` 得到幅值+角度 `(Vrρ, Vrθ)`。
6. **定子极坐标（反 Park）**：把转子极坐标旋转回定子极坐标 `(Vsρ, Vsθ)`：\(V_{s\rho}=V_{r\rho}\)，\(V_{s\theta}=V_{r\theta}+\psi\)。
7. **SVPWM**：`(Vsρ, Vsθ)` 经七段式 SVPWM 生成三相 PWM。

#### 4.1.3 源码精读

模块的端口声明集中体现了上面的「管道」边界。先看端口分组（[RTL/foc/foc_top.v:L11-L45](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L11-L45)）：

- **时钟/复位**：`rstn`、`clk`。
- **PI 参数**：`Kp`、`Ki`（31bit，运行时可调）。
- **角度输入**：`phi[11:0]`，机械角度 φ（0~4095 对应 0~360°）。
- **ADC 握手与结果**：`sn_adc`（输出，通知 ADC「可以采样」）、`en_adc`（输入，ADC 转换完成）、`adc_a/b/c[11:0]`。
- **PWM 输出**：`pwm_en`、`pwm_a`、`pwm_b`、`pwm_c`。
- **dq 电流监测**：`en_idq`、`id`、`iq`（16bit 有符号）。
- **dq 电流目标**：`id_aim`、`iq_aim`（16bit 有符号）。
- **初始化完成**：`init_done`。

内部把「三大坐标系」的中间量都声明成了 `wire`/`reg`，注释里直接标了坐标含义（[RTL/foc/foc_top.v:L50-L64](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L50-L64)）：

```verilog
reg  signed [15:0] ia, ib, ic;        // 三相电流 (abc 坐标系)
wire signed [15:0] ialpha, ibeta;     // iα/iβ (定子直角 αβ)
wire signed [15:0] vd, vq;            // vd/vq (转子直角 dq，PI 输出)
wire        [11:0] vr_rho, vr_theta;  // (Vrρ, Vrθ) 转子极坐标
reg         [11:0] vs_rho, vs_theta;  // (Vsρ, Vsθ) 定子极坐标
```

注意位宽约定：**电流类信号是 16bit 有符号**（`signed [15:0]`），**极坐标的角度/幅值是 12bit 无符号**（`[11:0]`，0~4095 对应 0~360°）。这种「电流走有符号、角度走无符号标度」的约定贯穿全库。

#### 4.1.4 代码实践

**目标**：建立端口与坐标系的对应关系。

**步骤**：

1. 打开 [RTL/foc/foc_top.v:L11-L45](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L11-L45)，把所有端口分成 7 组（时钟/复位、PI 参数、角度、ADC、PWM、监测、目标、初始化）。
2. 对照 [RTL/foc/foc_top.v:L50-L64](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L50-L64) 的内部变量，画一张表，标注每个变量属于哪个坐标系。

**预期结果**：你会得到一张「端口/变量 → 坐标系」对照表，例如 `phi`→机械角度、`ia/ib/ic`→abc、`ialpha/ibeta`→αβ、`id/iq`→dq、`vr_rho/vr_theta`→转子极坐标、`vs_rho/vs_theta`→定子极坐标。这张表就是后续阅读所有子模块的「坐标系地图」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vd/vq` 是 `wire`，而 `vs_rho/vs_theta` 是 `reg`？

> **答案**：`vd/vq` 由子模块（`pi_controller`）的输出端口驱动，在 `foc_top` 里只是连线，所以是 `wire`；`vs_rho/vs_theta` 由 `foc_top` 自己的 `always` 块（反 Park）用 `<=` 赋值，所以是 `reg`。在 Verilog 里 `wire`/`reg` 的选择取决于**驱动方式**（`assign`/例化 vs `always`），与最终综合成连线还是触发器无关。

**练习 2**：`id/iq` 是 16bit 有符号，`vr_rho` 是 12bit 无符号，这反映了什么？

> **答案**：电流/电压分量（直角坐标）可正可负，用有符号；极坐标的幅值 ρ 是非负的、角度 θ 是周期标度（0~4095↔0~360°），用无符号 12bit 表达更紧凑，也方便直接喂给查表型 SVPWM。

---

### 4.2 i_en / o_en 脉冲握手协议

#### 4.2.1 概念说明

`foc_top` 把 9 段功能串成流水线，但 FPGA 流水线的每一级都要花好几个时钟周期（Clark 要 3 拍、Park 要 2 拍、PI 要 5 拍）。下游模块怎么知道上游「这一拍的数据算好了」？

本项目没有用复杂的 valid/ready 双向握手，而是用一个**极简约定**：**「单周期高电平脉冲」=「数据有效节拍」**。每个模块都有一个输入使能 `i_en` 和输出使能 `o_en`：

- `i_en` 上出现 1 个时钟周期的高电平，表示「输入数据已就绪，请在本模块启动一次计算」。
- 计算完成、输出寄存器更新后，`o_en` 上也产生 1 个时钟周期的高电平，表示「输出已就绪，请下一级取用」。

于是整条链上的脉冲就像接力棒一样一级级传下去，每传一级就把数据往后推一段。

#### 4.2.2 核心流程

电流采样这一段（从 ADC 完成到算出 dq）的握手链是：

```text
en_adc ──(电流重构,1拍)──> en_iabc ──(Clark,3拍)──> en_ialphabeta ──(Park,2拍)──> en_idq
```

- `en_adc`：外部 ADC（`adc_ad7928`）转换完成后产生，表示 `adc_a/b/c` 有效。
- `en_iabc`：`foc_top` 自己的 `always` 块把 `en_adc` 打一拍（顺便完成电流重构），表示 `ia/ib/ic` 有效。
- `en_ialphabeta`：`clark_tr` 的 `o_en`，表示 `iα/iβ` 有效。
- `en_idq`：`park_tr` 的 `o_en`，表示 `id/iq` 有效（同时也是 `foc_top` 对外的监测输出）。

再往后，`en_idq` 又作为两个 PI 控制器的 `i_en` 继续传递（PI 的 `o_en` 在 `foc_top` 里被悬空，因为 `vd/vq` 是组合直连、不需要再握手给下游的 `cartesian2polar`）。

#### 4.2.3 源码精读

握手信号的声明（[RTL/foc/foc_top.v:L52-L55](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L52-L55)）：

```verilog
reg   en_iabc;          // 三相电流有效脉冲
wire  en_ialphabeta;    // αβ 有效脉冲（clark 的 o_en）
// en_idq 在 park 实例的 .o_en 上，见 L147
```

电流重构 `always` 块里，`en_iabc <= en_adc` 就是把脉冲打一拍（[RTL/foc/foc_top.v:L99-L109](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L99-L109)）：

```verilog
en_iabc <= en_adc;
if(en_adc) begin
    ia <= $signed( {4'b0, adc_b} + {4'b0, adc_c} - {3'b0, adc_a, 1'b0} );  // Ia = ADCb+ADCc-2*ADCa
    ...
end
```

Clark 实例把 `en_iabc` 接到 `i_en`，把 `o_en` 接出成 `en_ialphabeta`（[RTL/foc/foc_top.v:L119-L129](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L119-L129)）：

```verilog
clark_tr u_clark_tr (
    .i_en  ( en_iabc       ),
    ...
    .o_en  ( en_ialphabeta ),
    ...
);
```

Park 实例同理，`en_ialphabeta → en_idq`（[RTL/foc/foc_top.v:L140-L150](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L140-L150)）。PI 实例再把 `en_idq` 当 `i_en`（[RTL/foc/foc_top.v:L160-L170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L160-L170)），只是 `o_en` 端口悬空：

```verilog
pi_controller u_id_pi (
    .i_en    ( en_idq ),
    ...
    .o_en    (        ),   // 悬空：vd 直连 cartesian2polar，不需要再握手
    .o_value ( vd     )
);
```

#### 4.2.4 代码实践（本讲核心实践）

**目标**：标出 `en_adc → en_iabc → en_ialphabeta → en_idq` 这条握手链，并数清楚从电流采样到 dq 算完，一共经过了多少级（多少个时钟周期）流水线节拍。

**步骤**：

1. 在 [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) 里定位 4 个使能信号的出现位置：`en_adc`（端口 L29，使用 L103）、`en_iabc`（L52，L103 赋值、L122 使用）、`en_ialphabeta`（L55，clark 的 o_en）、`en_idq`（park 的 o_en，L147）。
2. 逐级确认每个子模块的流水线深度（去对应文件数 `en_s*/o_en` 的打拍级数）：
   - 电流重构：`en_iabc <= en_adc`，**1 级**。
   - `clark_tr.v`：`i_en → en_s1 → en_s2 → o_en`，**3 级**。
   - `park_tr.v`：`i_en → en_s1 → o_en`，**2 级**。
3. 把延迟累加起来，画一张「脉冲时序」草图。

**需要观察的现象**：假设 `en_adc` 在第 T 个时钟周期为高，那么：

| 周期 | 事件 |
|------|------|
| T | `en_adc` 高（ADC 结果就绪） |
| T+1 | `en_iabc` 高（`ia/ib/ic` 更新） |
| T+4 | `en_ialphabeta` 高（`iα/iβ` 更新，经 Clark 3 级） |
| T+6 | `en_idq` 高（`id/iq` 更新，经 Park 2 级） |

**预期结果（答案）**：从 `en_adc` 到 `en_idq`，**一共 6 个时钟周期**（电流重构 1 级 + Clark 3 级 + Park 2 级）。

**继续延伸到 PWM**（定性，不计精确数）：`en_idq` 之后再经过 PI 5 级 → `cartesian2polar`（迭代式，约 30 个时钟周期，详见 u2-l6）→ 反 Park 1 级 → SVPWM。把这段加起来也只有几十个时钟周期，远小于一个 PWM 周期（2048 个时钟周期），所以「算完一次控制」绝不会跨周期，这正是单速率设计可行的原因（见 4.3）。

> 说明：cartesian2polar 是迭代式而非短流水线，其精确延迟在 u2-l6 讲解；SVPWM 的占空比更新时机在 u2-l7 讲解。本讲只需建立「全链路总延迟远小于 2048 拍」的直觉。

#### 4.2.5 小练习与答案

**练习 1**：如果要把 `id/iq` 的监测改用「每收到一个 `en_idq` 就打印一次」（这正是 `uart_monitor` 的做法），这个触发信号从哪里取？

> **答案**：直接用 `en_idq`（`foc_top` 的输出端口）。`fpga_top.v` 里正是把 `en_idq` 接到 `uart_monitor` 的 `i_en`，每来一个脉冲就启动一次 UART 发送。

**练习 2**：为什么 PI 的 `o_en` 在 `foc_top` 里被悬空？

> **答案**：因为下游 `cartesian2polar` 的 `i_en` 被常接 `1'b1`（始终使能、持续迭代），它不靠脉冲触发，而是每个 ~30 拍自主产出一组 `(Vrρ, Vrθ)`。所以 PI 算完的 `vd/vq` 不需要再握手给下游，`o_en` 自然悬空。

---

### 4.3 统一节拍：控制频率 = clk / 2048

#### 4.3.1 概念说明

整个 FOC 有四个「频率」概念，乍看不同，其实在 `foc_top` 里**被强制统一**成一个值：

- **采样率**：多久采一次三相电流。
- **PID 更新率**：多久算一次 PI。
- **SVPWM 占空比更新率**：多久更新一次 PWM 占空比。
- **控制频率**：以上三者的统称。

它们都等于 \(f_{ctrl} = f_{clk}/2048\)。例如 `clk = 36.864MHz` 时，\(f_{ctrl} = 36.864\text{MHz}/2048 = 18\text{kHz}\)，即每 55.6µs 完成一次完整控制。

为什么是 2048？因为 SVPWM 用一个 0~2047 的计数器定义一个 PWM 周期，**一个 PWM 周期内只做一次采样 + 一次完整运算**，下一周期再套用新的占空比。这样采样、计算、调制三者天然锁在同一节拍上，避免了「采样时刻碰上 PWM 翻转」之类的不确定性。

#### 4.3.2 核心流程

一个控制周期（2048 个时钟）内发生的事：

```text
  PWM 周期 (cnt: 0 → 2047, 共 2048 个 clk)
  ├─ 某时刻三相 PWM 同时为低（下桥臂全导通）= 采样窗口
  ├─ hold_detect 检测到该窗口，延时 SAMPLE_DELAY 拍后拉高 sn_adc
  ├─ ADC 采样三相电流 → 回送 en_adc + adc_a/b/c
  ├─ 流水线计算: en_iabc → en_ialphabeta → en_idq → vd/vq → (Vrρ,Vrθ) → (Vsρ,Vsθ)
  └─ 本周期末(cnt≈2041~2047) SVPWM 锁存新的 (Vsρ,Vsθ) → 下一周期生效新占空比
```

关键点：**「算」和「调」错开一个周期**——本周期采的电流，算出的新电压，在下一个 PWM 周期才反映到占空比上。这是离散控制系统的标准做法，延迟一个周期（55.6µs）对电机这种慢动态对象完全可以接受。

#### 4.3.3 源码精读

统一节拍的「总定义」写在端口注释里（[RTL/foc/foc_top.v:L21](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L21)）：

```verilog
input wire clk,  // 控制频率 = 时钟频率 / 2048。
                 // (控制频率 = 采样率 = PID 控制频率 = SVPWM 占空比更新率)
```

「2048」这个数来自 SVPWM 的周期计数器（[RTL/foc/svpwm.v:L10](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L10) 注释「PWM 频率 = clk/2048」）。SVPWM 实例把 `vs_rho/vs_theta` 接进去（[RTL/foc/foc_top.v:L246-L256](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L246-L256)）。

而「采样窗口 + 延时」由 `hold_detect` 负责：它监测三相 PWM 是否同时为低，延时 `SAMPLE_DELAY` 拍后产生 `sn_adc`（[RTL/foc/foc_top.v:L264-L271](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L264-L271)）：

```verilog
hold_detect #(.SAMPLE_DELAY(SAMPLE_DELAY)) u_adc_sn_ctrl (
    .in  ( ~pwm_a & ~pwm_b & ~pwm_c ),  // 三相同时为低时 =1
    .out ( sn_adc )                      // 保持 SAMPLE_DELAY 拍后产生采样脉冲
);
```

这样 `sn_adc` 一定落在采样窗口内，保证了「采到的电流能反映真实相电流」。

#### 4.3.4 代码实践

**目标**：算清楚一个控制周期到底有多长，并验证全链路延迟远小于它。

**步骤**：

1. 取 `clk = 36.864MHz`，计算 \(T_{ctrl} = 2048 / 36.864\text{MHz} \approx 55.6\,\mu s\)。
2. 由 4.2.4 已知 `en_adc → en_idq` 耗时 6 拍 ≈ \(6/36.864\text{MHz} \approx 0.16\,\mu s\)。
3. 估算全链路（到 PWM）约几十拍 ≈ 1µs 量级。

**预期结果**：\(0.16\,\mu s \ll 55.6\,\mu s\)，整条运算链只用掉一个控制周期的百分之几，剩余时间完全空闲。这说明设计留了极大裕量，也解释了为什么不必担心流水线延迟会「卡住」下一个采样。

> 待本地验证：如果你跑仿真，可以观察 `en_idq` 脉冲间距是否恰好是 2048 个 `clk`（受仿真激励驱动方式影响，此结论以真实硬件节拍为准）。

#### 4.3.5 小练习与答案

**练习 1**：若把 `clk` 提高到 73.728MHz（翻倍），控制频率变成多少？采样率呢？

> **答案**：控制频率 \(= 73.728\text{MHz}/2048 = 36\text{kHz}\)，采样率随之翻倍到 36kHz——因为「控制频率 = 采样率」被绑死。但注意 `clk` 不能任意提高，受 AD7928 的 SPI 时序约束（`clk` 须 ≤40MHz，见 u1-l3）。

**练习 2**：为什么采样要等三相 PWM 「同时为低」？

> **答案**：本项目用「下桥臂电阻采样法」，只有当下桥臂 MOS 管导通（PWM=0）时，相电流才流过采样电阻、才能被 ADC 测到。三相同时为低的窗口，就是三相都能采到电流的公共窗口。

---

### 4.4 初始化与反 Park：闭环的起点

#### 4.4.1 概念说明

`foc_top` 里有一个特殊的 `always` 块，同时承担两件事：

1. **初始化（标定初始机械角度 Φ）**：上电后先不知道「电角度 0 对应哪个机械角度」。于是强行输出一个最大幅值、角度为 0 的电压矢量，把转子「拽」到电角度 ψ=0 的位置，然后记下此时传感器读到的机械角度作为 Φ。之后就能用 \(\psi = N\cdot(\varphi - \Phi)\) 换算电角度。
2. **反 Park 变换**：初始化结束后，持续地把转子极坐标 `(Vrρ, Vrθ)` 旋转成定子极坐标 `(Vsρ, Vsθ)` 送给 SVPWM。

这两个功能共用一个状态变量 `init_done`：标定期间 `init_done=0`，标定结束置 1，之后才真正进入闭环 FOC。

#### 4.4.2 核心流程

```text
  上电(rstn=0) → init_done=0, init_cnt=0
        │
        ▼  init_cnt 从 0 数到 INIT_CYCLES (~0.45s @36.864MHz)
   强制输出 Vsρ=4095(最大), Vsθ=0  →  转子被拽到 ψ=0
        │  init_cnt==INIT_CYCLES 时
        ▼  记录 init_phi = phi (即 Φ)，init_done<=1
   进入闭环：Vsρ <= Vrρ ;  Vsθ <= Vrθ + ψ   (反 Park)
```

反 Park 的数学依据：转子极坐标系是定子极坐标系**旋转了电角度 ψ** 得到的，所以幅值不变、角度叠加：

\[
V_{s\rho} = V_{r\rho}, \qquad V_{s\theta} = V_{r\theta} + \psi
\]

#### 4.4.3 源码精读

这个「初始化 + 反 Park」`always` 块（[RTL/foc/foc_top.v:L218-L237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237)）：

```verilog
if(init_cnt<=INIT_CYCLES) begin            // 初始化未完成
    vs_rho  <= 12'd4095;                   //   强制 Vsρ 取最大
    vs_theta<= 12'd0;                      //   强制 Vsθ = 0
    init_cnt<= init_cnt + 1;
    if(init_cnt==INIT_CYCLES) begin         //   即将完成
        init_phi <= phi;                    //   记录 Φ
        init_done<= 1'b1;
    end
end else begin                             // 初始化完成 → 反 Park
    vs_rho  <= vr_rho;                     //   Vsρ = Vrρ
    vs_theta<= vr_theta + psi;             //   Vsθ = Vrθ + ψ
end
```

注意 `init_done` 还被当作**所有子模块的复位**（例化时 `.rstn(init_done)`，见 [L120](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L120)、[L141](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L141) 等）：标定期间所有变换/PI 模块都处于复位态，标定结束才同步「解复位」开始工作，避免在转子还在被「拽」的时候 PI 就乱算。

而电角度 ψ 的换算在另一个独立的 `always` 块里（[RTL/foc/foc_top.v:L76-L88](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L76-L88)）：

```verilog
psi <= {4'h0, POLE_PAIR} * (phi - init_phi);   // ψ = N*(φ-Φ)   (未装反时)
```

#### 4.4.4 代码实践

**目标**：理解初始化时长由谁决定、反 Park 在数据流中的位置。

**步骤**：

1. 在 [RTL/foc/foc_top.v:L13](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L13) 找到 `INIT_CYCLES = 16777216`，计算初始化时间：\(16777216 / 36.864\text{MHz} \approx 0.45\text{s}\)。
2. 在 [L234-L235](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L234-L235) 确认反 Park 的两行赋值，对照数据流图（4.1.2）的第 6 步。
3. 思考：为什么反 Park 之后幅值还是 `vr_rho`，角度却要加 `psi`？

**预期结果**：你会确认反 Park 是「极坐标→极坐标」的纯旋转（幅值不变、角度加 ψ），它把 PI 算出的「转子视角电压」翻译成 SVPWM 需要的「定子视角电压」。如果改 `INIT_CYCLES`，初始化时长随之线性变化（详见 u4-l2 的调参）。

#### 4.4.5 小练习与答案

**练习 1**：初始化期间为什么要强制 `Vsρ=4095, Vsθ=0`？

> **答案**：这是在施加一个「幅值最大、方向为电角度 0」的电压矢量，产生一个把转子拉向 ψ=0 的最大扭矩。等转子稳定在 ψ=0 后，记录的机械角度才是真正的初始角 Φ。

**练习 2**：`init_done` 既是输出端口、又当子模块复位，这样安全吗？

> **答案**：安全且刻意为之。标定期间 `init_done=0` 让所有算法模块保持复位（输出 0），避免它们在转子还在被「拽」时产生干扰输出；标定完成 `init_done` 跳 1，所有模块同拍解复位、协调启动。这是一种用单一信号同时表达「状态」和「控制」的简洁写法。

---

## 5. 综合实践

**任务**：为 `foc_top` 画一张完整的「数据流 + 握手 + 节拍」三合一总图。

要求：

1. 把以下 9 段功能按顺序排成一条从左到右的流水线：电流重构、Clark、Park、PI(id)、PI(iq)、cartesian2polar、反 Park、SVPWM，以及旁路的「角度换算 ψ」和「hold_detect」。
2. 在每段之间标注它使用的使能脉冲（`en_adc`、`en_iabc`、`en_ialphabeta`、`en_idq`）。
3. 用括号标出每段的流水线深度（1/3/2/5/… 拍）。
4. 在图的最外层画一个大框，标注「一个控制周期 = 2048 个 clk ≈ 55.6µs」，并指出整条链的总延迟（约几十拍）远小于这个周期。
5. 用不同颜色/记号区分三大坐标系区段：**abc 区**（电流重构）、**αβ 区**（Clark）、**dq 区**（Park~PI）、**极坐标区**（cartesian2polar~SVPWM）。

**验收标准**：

- 从图上能一眼看出「三相 ADC → en_adc → … → en_idq → vd/vq → PWM」的完整路径。
- 能在图上数出 `en_adc → en_idq = 6 拍`。
- 能解释为什么所有模块都用 `init_done` 当复位、为什么 `cartesian2polar` 的 `i_en` 接 `1'b1`。

这张图将是你阅读 u2-l2 ~ u2-l8（逐模块精读）时的「导航地图」，建议保存。

## 6. 本讲小结

- `foc_top` 是 FOC 算法顶层，自身不含公式，靠连线 + 例化把 9 段功能串成流水线；端口分 7 组（时钟/复位、PI 参数、角度、ADC、PWM、监测、目标、初始化）。
- 电流矢量依次穿过 **abc → αβ（定子直角）→ dq（转子直角）→ 转子极坐标 → 定子极坐标**，最后由 SVPWM 生成三相 PWM。
- 全库统一用 **`i_en`/`o_en` 单周期高电平脉冲** 做模块间握手：`en_adc → en_iabc → en_ialphabeta → en_idq`，从电流采样到算出 dq 共 **6 个时钟周期**（电流重构 1 级 + Clark 3 级 + Park 2 级）。
- **控制频率 = `clk`/2048 = 采样率 = PID 更新率 = SVPWM 占空比更新率**；一个控制周期 ≈ 55.6µs，整条运算链只占其百分之几，裕量充足。
- 初始化阶段用最大电压矢量把转子拽到 ψ=0 标定 Φ，标定完 `init_done` 置 1 同时解复位所有子模块并启动反 Park（\(V_{s\rho}=V_{r\rho},\,V_{s\theta}=V_{r\theta}+\psi\)）。

## 7. 下一步学习建议

本讲建立了 `foc_top` 的全景地图，接下来按数据流顺序逐模块深入：

- **u2-l2 角度换算与电流重构**：精读 `ψ = N·(φ−Φ)` 和 `Ia = ADCb+ADCc−2·ADCa` 的两个 `always` 块。
- **u2-l3 Clark 变换**：精读 `clark_tr.v` 的 3 级流水线和 √3 移位近似。
- **u2-l4 Park 变换与 sincos**：精读 `park_tr.v` 与 `sincos.v` 的查表。
- **u2-l5 PI 控制器**：精读 `pi_controller.v` 的 5 级流水线和饱和保护。
- **u2-l6 cartesian2polar 与反 Park**：精读迭代式直角转极坐标。
- **u2-l7 SVPWM**：精读七段式调制与马鞍波 duty。
- **u2-l8 hold_detect**：精读采样窗口检测。

建议顺序阅读，因为每一篇都承接前一篇的下游数据流。
