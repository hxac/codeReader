# 参数整定与跨平台移植

## 1. 本讲目标

本讲是专家层的「调参与移植」专题。读者学完后应该能够：

- 说清 `foc_top` 的 5 个关键 parameter（`INIT_CYCLES` / `ANGLE_INV` / `POLE_PAIR` / `MAX_AMP` / `SAMPLE_DELAY`）各自的物理含义、取值范围和调参策略；
- 理解 `Kp` / `Ki` 为什么是 31bit **输入端口**（而非 parameter），因而可在运行时调整，以及 `id_aim` 一般恒为 0 的原因（不弱磁）；
- 看懂除 `altpll` 外全库都是纯 RTL，能把工程从 Altera Cyclone IV 移植到 Xilinx / Lattice，核心只在于替换 PLL IP 核并保证主时钟 ≈ 36.864MHz（且 < 40MHz）；
- 独立完成一次完整的「换板 + 换电机」参数重算与移植方案。

本讲承接 u2-l1（foc_top 全景）、u2-l8（采样窗口与 hold_detect）。如果你还不清楚 `MAX_AMP` 与采样窗口的关系，建议先回顾 u2-l8。

## 2. 前置知识

- **parameter（参数） vs input port（输入端口）**：在 Verilog 中，`parameter` 是**编译期/综合期**常量，写死在硬件里，上电后不可改；`input wire` 端口是**运行期**信号，可由其它逻辑（寄存器、MCU、外部拨码开关）在每个时钟驱动。本讲的 5 个量是 parameter，而 `Kp`/`Ki` 是端口——这一区分是本讲的灵魂。
- **电角度与机械角度**：\( \psi = N\cdot(\varphi-\Phi) \)，其中 \(N\) 是极对数（`POLE_PAIR`），\(\varphi\) 是机械角度，\(\Phi\) 是初始化时标定的初始机械角度。详见 u2-l2。
- **采样窗口**：相电流采样电阻在下桥臂，只有三相下桥臂同时导通（`pwm_a=pwm_b=pwm_c=0`）时相电流才可测。这段公共低电平期称为采样窗口，其最小长度 \(T_{\text{window,min}} = 1024 - 2\cdot\text{MAX\_AMP}\) 个 clk 周期（u2-l8 已推导）。
- **主时钟 clk 的统一节拍**：控制频率 = 采样率 = PID 更新率 = SVPWM 占空比更新率 = \(f_{\text{clk}}/2048\)。默认 \(f_{\text{clk}}=36.864\text{MHz}\)，故约 18kHz。
- **PLL（锁相环）**：用晶振输入产生一个稳定的新频率时钟。Altera 叫 `altpll`，Xilinx 叫 Clocking Wizard，Lattice 有自己的 IP，功能等价。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| :-- | :-- | :-- |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | FOC+SVPWM 算法顶层 | 5 个 parameter 的定义与用法、`Kp`/`Ki` 端口、初始化计时逻辑 |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | 工程顶层 | 参数取值、`altpll` 原语与移植要点、`id_aim=0`、各外设 `CLK_DIV` |
| [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) | 项目说明 | 官方调参表与时钟配置说明 |

## 4. 核心概念与源码讲解

本讲按「先调参、再讲 PI 增益、最后讲移植」的顺序，拆成三个最小模块。

### 4.1 foc_top 的五大 parameter

#### 4.1.1 概念说明

`foc_top` 模块用 5 个 parameter 把「与具体电机、具体接法、具体节奏」相关的可变量全部抽离到模块头部。这样做的目的是：**核心算法一行不改，只改这 5 个数，就能适配不同的电机和安装方式**。这正是本库「蓝色核心固定、外层可改」分层设计的体现。

这 5 个 parameter 可分为三类：

- **与电机本身有关**：`POLE_PAIR`（极对数）、`ANGLE_INV`（传感器方向）。
- **与时序节奏有关**：`INIT_CYCLES`（初始化时长）、`SAMPLE_DELAY`（采样延时）。
- **与驱动能力/采样可行性折中有关**：`MAX_AMP`（SVPWM 最大振幅）。

#### 4.1.2 核心流程

调参时遵循下面这张「取值—含义—联动」表：

| parameter | 取值范围 | 物理含义 | 调大/调小的影响 |
| :-- | :-- | :-- | :-- |
| `INIT_CYCLES` | 1~4294967294 | 初始化占多少个 clk 周期 | 初始化时间 \(t = \text{INIT\_CYCLES}/f_{\text{clk}}\)；太短则转子来不及回归电角度=0 |
| `ANGLE_INV` | 0 或 1 | 传感器是否装反 | 装反置 1，把电角度取反 |
| `POLE_PAIR` | 1~255 | 电机极对数 \(N\) | 决定电角度换算 \(\psi=N(\varphi-\Phi)\) |
| `MAX_AMP` | 1~511 | SVPWM 最大振幅 | 力矩越大；但采样窗口 \(1024-2\cdot\text{MAX\_AMP}\) 越短 |
| `SAMPLE_DELAY` | 0~511 | 采样延时（clk 周期数） | 等 MOS 管电流稳定的延时；过长会吃掉采样窗口 |

其中 `MAX_AMP` 与 `SAMPLE_DELAY` 存在硬联动约束（来自 u2-l8）：

\[ \text{SAMPLE\_DELAY} + T_{\text{sn\_adc}\to\text{en\_adc}} < T_{\text{window,min}} = 1024 - 2\cdot\text{MAX\_AMP} \]

也就是说，`MAX_AMP` 调大会同时「减小右边、显得左边更挤」，二者必须一起权衡。

#### 4.1.3 源码精读

5 个 parameter 集中定义在模块头部：

[foc_top.v:L11-L18](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L11-L18) —— 这里集中声明了 5 个 parameter，每个都带详细中文注释说明取值范围与含义。

**`INIT_CYCLES` 与初始化计时**：模块用一个 32bit 计数器 `init_cnt` 从 0 数到 `INIT_CYCLES`，到达时锁存当前机械角度作为 \(\Phi\) 并置 `init_done=1`：

[foc_top.v:L218-L237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237) —— 这是「初始化标定 + 反 Park」的合并 always 块。当 `init_cnt<=INIT_CYCLES` 时令 `vs_rho=4095, vs_theta=0`（用最大电压矢量把转子拽到电角度 0）；当 `init_cnt==INIT_CYCLES` 时锁存 `init_phi<=phi` 并置 `init_done=1`；之后才进入反 Park。

注意上限是 4294967294（\(2^{32}-2\)）而不是 \(2^{32}-1\)，因为 `init_cnt` 是 `reg [31:0]`（[foc_top.v:L47](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L47)），若 `INIT_CYCLES` 取满 \(2^{32}-1\)，则 `init_cnt+1` 会回绕到 0，导致 `init_cnt<=INIT_CYCLES` 恒成立、`init_done` 永远拉不起来。

**`ANGLE_INV`**：用 `generate-if` 在综合期选择正/负方向：

[foc_top.v:L76-L88](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L76-L88) —— `ANGLE_INV=1` 时编译 `psi <= POLE_PAIR*(init_phi-phi)`（取反），否则编译 `psi <= POLE_PAIR*(phi-init_phi)`。两套电路只有一套会被综合出来。

**`POLE_PAIR`**：直接作为电角度换算的乘数出现在上面两行（`{4'h0, POLE_PAIR} * (...)`），8bit 宽，故范围 1~255。

**`MAX_AMP`**：作为 SVPWM 的振幅输入 `v_amp` 传入：

[foc_top.v:L246-L256](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L246-L256) —— 例化 svpwm 时 `.v_amp(MAX_AMP)`，把最大振幅交给调制器，决定马鞍波占空比的极值。

**`SAMPLE_DELAY`**：作为延时长度传入 hold_detect：

[foc_top.v:L264-L271](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L264-L271) —— 例化 hold_detect 时 `.SAMPLE_DELAY(SAMPLE_DELAY)`，决定从「三相下桥臂全导通」到发出 `sn_adc` 脉冲之间倒数多少个 clk 周期。

这 5 个值在 fpga_top 里给出的默认取值（见 [fpga_top.v:L105-L110](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L105-L110)）为：`INIT_CYCLES=16777216`、`ANGLE_INV=0`、`POLE_PAIR=7`、`MAX_AMP=384`、`SAMPLE_DELAY=120`。其中 `INIT_CYCLES=16777216` 恰为 \(2^{24}\)，配合 36.864MHz 给出约 0.45 秒的初始化时间（注释 L106 明算 `16777216/36864000=0.45`）。官方调参表见 [README.md:L377-L385](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L377-L385)。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `INIT_CYCLES` 与初始化时间的换算关系，并体会它「依赖 clk 频率」这一点。

**操作步骤**（源码阅读 + 计算）：

1. 打开 [foc_top.v:L218-L237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237)，确认初始化耗时就是 `init_cnt` 从 0 数到 `INIT_CYCLES` 的周期数。
2. 用公式 \(t_{\text{init}} = \text{INIT\_CYCLES}/f_{\text{clk}}\) 计算默认配置：\(16777216 / 36864000 \approx 0.455\text{s}\)（项目注释四舍五入为 0.45s）。
3. 假设你希望初始化时间精确为 0.3 秒，反推：\(\text{INIT\_CYCLES} = 0.3 \times 36864000 = 11059200\)。
4. 思考：若把主时钟 clk 从 36.864MHz 改成 18MHz，`INIT_CYCLES=16777216` 还能给出 0.45s 吗？（答：不能，会变成 \(16777216/18000000\approx 0.93\text{s}\)，需同步改小 `INIT_CYCLES`。）

**需要观察的现象 / 预期结果**：`INIT_CYCLES` 与 clk 频率**成反比耦合**——同一个值在不同 clk 下代表不同的物理时间。这正是「换板后要重算 `INIT_CYCLES`」的根因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `INIT_CYCLES` 的上限是 4294967294 而不是 4294967295？

**参考答案**：`init_cnt` 是 32bit 无符号（最大 4294967295）。若 `INIT_CYCLES=4294967295`，当 `init_cnt` 数到该值后 `+1` 会回绕到 0，使 `init_cnt<=INIT_CYCLES` 恒为真、永远进不了「初始化完成」分支。所以最多用到 \(2^{32}-2\)，留 1 给 `+1` 不溢出。

**练习 2**：把 `MAX_AMP` 从 384 调到 450，默认 `SAMPLE_DELAY=120` 是否仍安全？（设 \(T_{\text{sn\_adc}\to\text{en\_adc}}\approx 117\) clk，见 u3-l2。）

**参考答案**：\(T_{\text{window,min}} = 1024 - 2\times 450 = 124\) clk。约束要求 \(120+117=237 < 124\)，**不成立**，会丢失采样。要让 450 可用，需把 `SAMPLE_DELAY` 压到 \(124-117=7\) 以内，几乎无裕量，工程上不可取。可见默认 384（窗口 256，裕量 19）是经过权衡的。

### 4.2 Kp / Ki：运行时可调的 PI 增益

#### 4.2.1 概念说明

`Kp` 和 `Ki` 是电流环 PI 控制器的两个增益（详见 u2-l5）。与前 5 个 parameter 最关键的区别是：**它们是 `input wire` 端口，不是 parameter**。这意味着它们可以在 FPGA 上电运行期间被动态修改，而不必重新综合。

为什么这样设计？因为 PI 参数属于「需要现场整定的量」——同一台电机、同一块驱动板，负载不同、温度不同时，最佳 Kp/Ki 也不同。把它做成端口，就可以由 MCU、拨码开关、串口命令等外部逻辑实时喂入；当然，如果你不需要动态调整，也可以像 fpga_top 那样直接喂一个常数。

它们的位宽是 31bit 无符号（`[30:0]`），是大整数，代表的其实是「带定点小数分辨率」的增益（u2-l5 已说明：最终输出取 `value[31:16]`，使 Kp/Ki 等效于乘了 \(1/2^{16}\) 的分数）。

#### 4.2.2 核心流程

PI 控制器的工作流（u2-l5 详述，这里只回顾与端口有关的节拍）：

1. 误差 \(e = \text{i\_aim} - \text{i\_real}\)；
2. 比例项 \(K_p\cdot e\)，积分项 \(\Sigma e\) 经 \(K_i\) 缩放；
3. 输出 \(v = K_p\cdot e + K_i\cdot\Sigma e\)，取高 16 位作为 vd/vq。

关键点：`Kp`/`Ki` 作为**组合输入**直接进入乘法器，模块内部不会锁存它们。因此你在外部改了 `Kp`/`Ki`，下一个控制周期（约 55µs）立刻生效。

#### 4.2.3 源码精读

端口声明在模块头部，注意没有 `signed` 关键字（无符号 31bit）：

[foc_top.v:L23-L24](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L23-L24) —— `input wire [30:0] Kp` 和 `input wire [30:0] Ki`，是端口而非 parameter。

`foc_top` 把同一对 `Kp`/`Ki` 分别喂给 d 轴和 q 轴两个 PI 控制器：

[foc_top.v:L160-L190](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L160-L190) —— `u_id_pi` 与 `u_iq_pi` 共用 `.i_Kp(Kp)`、`.i_Ki(Ki)`，即两轴共用同一组增益。

fpga_top 里给的是固定常数（当然你也可以改成寄存器驱动）：

[fpga_top.v:L114-L115](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L114-L115) —— `.Kp(31'd300000)`、`.Ki(31'd30000)`，典型取 \(K_p\) 比 \(K_i\) 大约一个数量级。

与之配套的 `id_aim` 在示例中恒为 0：

[fpga_top.v:L144](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L144) —— `assign id_aim = $signed(16'd0);`，即不进行弱磁控制（field weakening）。foc_top.v 端口注释 [L41](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L41) 也写明「在不使用弱磁控制的情况下一般设为0」。这是因为本库只实现电流环（扭矩控制），d 轴电流对永磁电机而言只会产生不期望的去磁/发热，故压到 0。

#### 4.2.4 代码实践

**实践目标**：把 `Kp`/`Ki` 从「固定常数」改造成「运行时可改的寄存器」，体会端口 vs parameter 的差别。

**操作步骤**（源码修改型，属示例代码，请勿提交到原仓库）：

1. 在 fpga_top.v 中新增两个寄存器，例如：
   ```verilog
   // 示例代码：把 Kp/Ki 改为运行时可改的寄存器
   reg [30:0] Kp_reg = 31'd300000;
   reg [30:0] Ki_reg = 31'd30000;
   ```
2. 把 foc_top 例化处的 `.Kp(31'd300000)` 改为 `.Kp(Kp_reg)`，`Ki` 同理。
3. 再写一段逻辑（例如根据某个按键或 UART 接收字节）在运行时改写 `Kp_reg`/`Ki_reg`。

**需要观察的现象 / 预期结果**：由于 `Kp`/`Ki` 是端口，上述改动**不需要重新综合 foc_top 核心**，只需重新综合 fpga_top。这印证了「PI 增益是运行期量」的设计意图。**待本地验证**：在真实硬件上用串口监视 iq 跟随曲线，应能看到改写 `Kp_reg` 后响应速度（超调/上升时间）随之变化。

#### 4.2.5 小练习与答案

**练习 1**：为什么作者把 `Kp`/`Ki` 做成端口而不是 parameter？

**参考答案**：PI 增益需要根据具体电机和负载现场整定，且可能要在运行中调整（如速度变化、温漂补偿）。做成端口即可由外部逻辑动态驱动而不必重综合；若做成 parameter，每次调参都要重新综合烧录，迭代成本高。

**练习 2**：示例里 `id_aim` 恒为 0，如果想让电机进入弱磁区（高速区），应如何改？

**参考答案**：弱磁控制需要在高速时给 `id_aim` 注入负值以抵消永磁体磁链、扩展调速范围。可把 `assign id_aim = $signed(16'd0);` 改成由一段用户逻辑根据当前转速生成负的 `id_aim`（转速越高、`id_aim` 越负）。注意这超出了本库「仅电流环」的范围，需自行增加速度估计。

### 4.3 跨平台移植：替换 altpll

#### 4.3.1 概念说明

本库号称「平台无关」，但有一个例外：fpga_top.v 里的 `altpll` 原语是 **Altera Cyclone IV 专属**。移植到 Xilinx / Lattice 时，唯一必须替换的就是这一处——用一个等效的 PLL IP 核（Xilinx 的 Clocking Wizard、Lattice 的对应 IP）代替它，产生同样约 36.864MHz 的主时钟即可。foc_top 及其下所有蓝色核心模块都是纯 RTL，与 FPGA 厂商无关。

这里有两个硬约束：

1. **主时钟必须 < 40MHz**。因为 adc_ad7928.v 用 clk 二分频产生 SPI 时钟 `spi_sck`，而 AD7928 芯片要求 SPI 时钟 ≤ 20MHz（见 [README.md:L358](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L358)）。
2. **主时钟最好仍是 36.864MHz**。这是为了凑 SVPWM 频率 = \(36.864\text{MHz}/2048 = 18\text{kHz}\) 这个整数；同时 I2C 的 `CLK_DIV=10`、UART 的 `CLK_DIV=320` 也都是按 36.864MHz 算好的（SCL≈922kHz≤1MHz，波特率=115200）。换 clk 频率会牵一发动全身。

#### 4.3.2 核心流程

移植到新平台的步骤：

1. **替换 PLL**：删掉 `altpll` 例化与 `defparam`，换成目标平台的时钟 IP（输入=该板晶振频率，输出≈36.864MHz）。保留 `locked` 信号接到 `rstn`（PLL 未锁相时保持复位）。
2. **核对引脚约束**：clk 输入改为新板晶振引脚；其余 IO（I2C/SPI/PWM/UART）按新板重新分配。
3. **（一般无需）重算时序参数**：只要主时钟仍是 36.864MHz，`INIT_CYCLES`、各 `CLK_DIV`、`Kp`/`Ki` 全部沿用原值。只有当你刻意改了主时钟频率，才需要按下表重算。

clk 频率变更后的联动重算清单：

| 受影响量 | 公式 | 默认值（36.864MHz） |
| :-- | :-- | :-- |
| SVPWM/控制频率 | \(f_{\text{clk}}/2048\) | 18kHz |
| 初始化时间 | \(\text{INIT\_CYCLES}/f_{\text{clk}}\) | 0.45s |
| I2C SCL | \(f_{\text{clk}}/(4\cdot\text{CLK\_DIV}_{\text{i2c}})\) | 922kHz |
| UART 波特率 | \(f_{\text{clk}}/\text{CLK\_DIV}_{\text{uart}}\) | 115200 |
| SPI sck | \(f_{\text{clk}}/2\) | 18.4MHz（须 ≤20MHz） |

#### 4.3.3 源码精读

fpga_top.v 里 PLL 段是全库唯一不可移植的部分：

[fpga_top.v:L50-L55](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L50-L55) —— 注释明确写「该模块仅适用于 Altera Cyclone IV FPGA，对于其他厂家或系列的 FPGA，请使用各自相同效果的 IP 核/原语（例如 Xilinx 的 clock wizard）代替」。

具体的倍分频关系藏在 `defparam` 里（[fpga_top.v:L54](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L54)）：`clk0_multiply_by=73`、`clk0_divide_by=99`、`inclk0_input_frequency=20000`（即 50MHz 晶振，周期 20ns）。于是输出 \(= 50\text{MHz}\times 73/99 \approx 36.8687\text{MHz}\)（项目注释统一称作 36.864MHz）。

注意一个移植时容易忽略的细节：`rstn` 不是外部按键复位，而是直接取自 PLL 的 `locked` 输出：

[fpga_top.v:L53](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L53) —— altpll 例化里 `.locked(rstn)`。换成 Xilinx Clocking Wizard 后，同样要把其 `locked` 输出接到 `rstn`，保证「PLL 未锁相→全局复位；锁相后→释放复位」的语义不变。

官方对「除 altpll 外全是纯 RTL」的说明见 [README.md:L460](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L460)，时钟配置说明见 [README.md:L350-L358](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L350-L358)。

#### 4.3.4 代码实践

**实践目标**：把 altpll 替换为 Xilinx Clocking Wizard 的概念性写法（示例代码，待在 Vivado 中具象化）。

**操作步骤**：

1. 在 Vivado 中用 IP Catalog 生成一个 Clocking Wizard（`clk_wiz`），设置 `clk_in1` = 50MHz（或你板子的晶振），`clk_out1` = 36.864MHz，勾选 `locked` 输出。
2. 用下面的示例代码替换 fpga_top.v 中 L52–L54 的 altpll 段：
   ```verilog
   // 示例代码：Xilinx Clocking Wizard 替代 altpll
   clk_wiz u_clk_wiz (
       .clk_in1  ( clk_50m ),  // 输入晶振
       .clk_out1 ( clk     ),  // 输出 36.864MHz 主时钟
       .locked   ( rstn    )   // 锁相成功=1，直接当 rstn
   );
   ```
3. 删掉对应的 `defparam`（Xilinx IP 不用 Altera 的 defparam 语法）。

**需要观察的现象 / 预期结果**：替换后，fpga_top 的对外行为与原 Altera 版本完全一致——`rstn` 仍在锁相后拉高，下游 foc_top 收到 36.864MHz 的 clk，所有 parameter 与 `CLK_DIV` 都不用改。**待本地验证**：在 Vivado 综合后查看时序报告，确认 `clk` 频率约为 36.864MHz。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接把主时钟设成 100MHz 以提高运算速度？

**参考答案**：受 SPI 时钟约束。adc_ad7928.v 用 clk 二分频产生 `spi_sck`，AD7928 要求 `spi_sck` ≤ 20MHz，故 clk ≤ 40MHz。100MHz 会让 `spi_sck`=50MHz 远超上限，ADC 无法正常工作。这也是为什么主时钟「可以取小于 40MHz 的任意值」但不能更高。

**练习 2**：移植到新板后，发现 I2C 通信失败（AS5600 无响应），但 foc_top 算法本身没问题。最可能的原因是什么？

**参考答案**：新板的主时钟 clk 偏离了 36.864MHz，但 I2C 的 `CLK_DIV` 仍是 10。若 clk 偏高使 SCL 超过 1MHz（AS5600 的上限），AS5600 就不响应。解决：按 \(f_{\text{SCL}}=f_{\text{clk}}/(4\cdot\text{CLK\_DIV})\) 重新算 `CLK_DIV`，把 SCL 压回 ≤1MHz；或修正 PLL 让 clk 回到 36.864MHz。

## 5. 综合实践

**任务背景**：把示例从「Altera Cyclone IV + 50MHz 晶振 + 极对数 7 的电机」移植到「某 Xilinx 开发板（晶振 100MHz）+ 极对数 14 的电机」，并保持初始化时间约 0.45 秒。

**要求**：

1. 列出所有需要修改的 parameter 与配置项；
2. 计算新的 `INIT_CYCLES`；
3. 说明 `MAX_AMP` 调大对采样窗口的影响。

**参考方案**：

**(1) 需要修改的项**：

| 项 | 原值 | 新值 | 原因 |
| :-- | :-- | :-- | :-- |
| PLL/IP 核 | `altpll`（Altera） | Xilinx Clocking Wizard | 跨厂商，必须替换 |
| PLL 输入频率 | 50MHz | 100MHz | 新板晶振不同 |
| PLL 输出频率 | ≈36.864MHz | **保持 ≈36.864MHz** | 让下游全部参数不动 |
| `POLE_PAIR` | 8'd7 | **8'd14** | 新电机极对数不同 |
| `ANGLE_INV` | 0 | **待现场验证**（0 或 1） | 取决于新电机上传感器装向 |
| 引脚约束 | Cyclone IV 引脚 | Xilinx 引脚 | 芯片不同 |

`Kp`/`Ki`、`MAX_AMP`、`SAMPLE_DELAY`、各 `CLK_DIV`、`INIT_CYCLES`（见下）原则上沿用——前提是 PLL 输出仍是 36.864MHz。

**(2) 计算新的 INIT_CYCLES**：

由于我们让主时钟仍是 36.864MHz，初始化时间的公式 \(t=\text{INIT\_CYCLES}/f_{\text{clk}}\) 中分母不变，故 `INIT_CYCLES` **无需改变**，沿用 `16777216` 即得 ≈0.455s≈0.45s。

若想精确凑 0.45s：\(\text{INIT\_CYCLES}=0.45\times 36864000 = 16588800\)。

> 注意：晶振从 50MHz 变 100MHz **不会**自动改变 `INIT_CYCLES` 的物理时间，因为我们用 PLL 把输出钉死在 36.864MHz。真正会触发重算的，是「你刻意改了主时钟频率」这种情况。例如若某原因 clk 只能取 18MHz，则要 \(0.45\times 18000000=8100000\) 才能维持 0.45s。

**(3) MAX_AMP 调大对采样窗口的影响**：

采样窗口最小长度 \(T_{\text{window,min}} = 1024 - 2\cdot\text{MAX\_AMP}\)（clk 周期）。`MAX_AMP` 调大：

- **好处**：SVPWM 振幅更大，电机可达最大力矩更大（占空比范围更宽）。
- **代价**：采样窗口线性缩短。例如 384→256 clk（约 6.9µs），450→124 clk，511→2 clk（近乎归零）。
- **硬约束**：必须满足 \(\text{SAMPLE\_DELAY} + T_{\text{sn\_adc}\to\text{en\_adc}} < T_{\text{window}}\)。默认 \(120+117=237\)，反推 `MAX_AMP` 上限约 \((1024-237)/2\approx 393\)。所以默认 384 已接近安全上限，调大空间很小；若一定要更大 `MAX_AMP`，必须同步减小 `SAMPLE_DELAY`（但会牺牲电流稳定时间）或换更快的 ADC 缩短 \(T_{\text{sn\_adc}\to\text{en\_adc}}\)。

## 6. 本讲小结

- `foc_top` 用 5 个 parameter 把可变量抽离：`INIT_CYCLES`（初始化时长，依赖 clk）、`ANGLE_INV`（传感器方向）、`POLE_PAIR`（极对数）、`MAX_AMP`（力矩/采样窗口折中）、`SAMPLE_DELAY`（采样延时）。
- `Kp`/`Ki` 是 31bit **输入端口**而非 parameter，可在运行时由外部逻辑动态调整；`id_aim` 示例中恒为 0，表示不做弱磁。
- `MAX_AMP` 与 `SAMPLE_DELAY` 存在硬联动：\(\text{SAMPLE\_DELAY}+T_{\text{sn\_adc}\to\text{en\_adc}} < 1024-2\cdot\text{MAX\_AMP}\)，默认 384 已近上限。
- 全库除 `altpll` 外均为纯 RTL；跨厂商移植只需替换 PLL IP（如 Xilinx Clocking Wizard），并把 `locked` 接到 `rstn`。
- 主时钟必须 < 40MHz（SPI 二分频约束），最好仍是 36.864MHz（凑 18kHz SVPWM，且让各 `CLK_DIV` 不必重算）。
- 换晶振只要 PLL 输出仍钉死 36.864MHz，则 `INIT_CYCLES` 与所有 `CLK_DIV` 都不用改；只有刻意改 clk 频率时才需联动重算。

## 7. 下一步学习建议

- 想深入理解 `MAX_AMP`/`SAMPLE_DELAY` 与采样窗口的推导，请回顾 **u2-l8（hold_detect）** 和 **u3-l2（adc_ad7928）**，并自己仿真测出 \(T_{\text{sn\_adc}\to\text{en\_adc}}\) 的精确值。
- 想验证 PI 增益整定效果，可阅读 **u2-l5（pi_controller）**，并参照 **u4-l3（仿真方法论）** 为 pi_controller 单独写一个 testbench，用 Analog 波形观察动态响应。
- 若要在本电流环上叠加速度环/位置环做级联控制，进入 **u4-l4（二次开发与系统扩展）**，那里讨论如何用 \(\varphi\) 的差分估转速、生成 `iq_aim`。
- 移植实战建议先在 iverilog 仿真层验证 foc_top 子模块行为（见 **u1-l4**），再上真实 Xilinx/Lattice 硬件。
