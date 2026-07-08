# SPI ADC 读取 adc_ad7928.v

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `adc_ad7928.v` 在整个 FOC 系统里的位置：它是粉色「硬件相关逻辑」中负责**三相相电流采样**的 ADC 控制器，把电机驱动板放大后的模拟电流翻译成 12bit 数字值交给蓝色 FOC 核心。
- 看懂 `CH_CNT` 与 `CH0..CH7` 一组参数如何让一个通用 AD7928 控制器自由适配「单次采几个通道、各对应 AD7928 哪个物理通道」。
- 读懂 `cnt`/`idx` 双计数器驱动的 SPI 状态机：如何在 `sn_adc` 脉冲到来后串行地完成多个通道的 SPI 采样，再用 `o_en_adc` 脉冲**同步提交**全部结果。
- 理解作者为何把 `sn_adc`/`en_adc`/`adc_a/b/c` 设计成「同步提交」的抽象接口，以及这种抽象对换用其它 ADC（甚至并行 ADC）带来的可移植性收益。
- 算清一个硬约束：`SAMPLE_DELAY + (sn_adc 到 en_adc 的时间差)` 必须小于采样窗口长度，并解释它的物理来源。

## 2. 前置知识

本讲承接 **u2-l8（hold_detect.v）**，请先回忆两个关键结论：

- **采样窗口**：相电流采样电阻接在下桥臂，只有三相下桥臂同时导通（`pwm_a=pwm_b=pwm_c=0`）时相电流才可测。这段公共低电平期称为采样窗口，其最短长度为 \(T_{win}=1024-2\cdot MAX_{AMP}\) 个 `clk` 周期。
- **sn_adc 脉冲**：`hold_detect.v` 在检测到三相全低并延时 `SAMPLE_DELAY` 个 `clk` 后，在 `sn_adc` 上输出一拍高电平脉冲，意为「现在 MOS 管电流已稳定，ADC 可以采样了」。

本讲要回答的下一个问题是：**sn_adc 脉冲发出之后，到底是谁、用什么节奏把三相电流采回来？** 答案就是 `adc_ad7928.v`。它通过 SPI 总线与 AD7928 芯片通信，最终在 `en_adc` 上回送一拍脉冲，并把三相 ADC 原始值同步呈现在 `adc_a`/`adc_b`/`adc_c` 上。

> 名词速查
> - **AD7928**：Analog Devices 的 8 通道、1MSPS、12bit 逐次逼近型 ADC，SPI 接口，**片内只有一个采样保持器 (T/H)**，因此任意时刻只能采一个通道。
> - **SPI**：四线串行总线（SS/SCK/MOSI/MISO），主机拉低 SS 开始一帧，靠 SCK 边沿在 MOSI 上串行写出、在 MISO 上串行读入。
> - **sn_adc / en_adc**：本库统一的「启动 / 完成」单拍脉冲握手约定，与 u2 系列中 Clark/Park/PI 模块用的 `i_en/o_en` 是同一套思想。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| :--- | :--- | :--- |
| [RTL/adc_ad7928.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v) | AD7928 的通用 SPI 控制器（粉色，硬件相关） | 参数化通道、`cnt`/`idx` 状态机、同步提交 |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | 工程顶层 | 例化 `adc_ad7928`、把它与 `hold_detect`、`foc_top` 连成数据通路 |
| RTL/foc/hold_detect.v | （上一讲）产生 `sn_adc` | 提供 `sn_adc` 的来源与采样窗口概念 |
| RTL/foc/foc_top.v | （蓝色核心）消费 `en_adc`+三相 ADC 值 | 用 `ia=ADCb+ADCc-2·ADCa` 做电流重构（见 u2-l2） |

---

## 4. 核心概念与源码讲解

本讲按「**接口 → 参数 → 状态机 → 同步提交与例化**」四块推进，前两块对应 `adc_ad7928` 模块的外表，后两块对应它的内部实现与在 `fpga_top` 中的接线。

### 4.1 模块定位、接口抽象与端口定义

#### 4.1.1 概念说明

`adc_ad7928` 是一个**通用的 AD7928 读控制器**：它对外只暴露两类接口——一组 SPI 引脚（接芯片）和一组「用户逻辑接口」（接 FOC 系统）。它**不知道 FOC 为何物**，只认一个简单的约定：

- 输入 `i_sn_adc`：来一拍高电平脉冲 ⇒ 「开始一次（多通道）采样」。
- 输出 `o_en_adc`：采样全部结束 ⇒ 回一拍高电平脉冲，**同时**在 `o_adc_value0..7` 上把结果同步呈出。

这个「**一个启动脉冲换来一个完成脉冲，且完成脉冲同拍交付全部数据**」的约定，是整个 FOC 与 ADC 之间的唯一契约，也是本模块最重要的设计思想（详见 4.4）。

#### 4.1.2 核心流程

```text
           i_sn_adc 脉冲                         o_en_adc 脉冲
                │  (启动)                              │  (完成+同步提交)
                ▼                                      ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  idle ──► 串行采样 通道N ──► 通道N-1 ──► … ──► 通道0 ──► idle │
  └─────────────────────────────────────────────────────────────┘
   (SS/SCK/MOSI 驱动 AD7928)        (MISO 收数据，存入 ch_value[])
```

收到 `i_sn_adc` 后，模块按 `idx` 从大到小依次对配置好的通道做 SPI 转换，每完成一个就存入对应槽位 `ch_value[idx]`；当最后一个通道（`idx==0`）完成时，置一拍 `o_en_adc`，此时全部 `o_adc_value*` 同时有效。

#### 4.1.3 源码精读

端口定义见 [RTL/adc_ad7928.v:22-39](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L22-L39)。其中：

- `spi_ss`/`spi_sck`/`spi_mosi` 为 `output reg`，`spi_miso` 为 `input wire`，这 4 根线直接连到 AD7928 芯片。
- [RTL/adc_ad7928.v:30-31](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L30-L31) 是与 FOC 的握手：`i_sn_adc` 进、`o_en_adc` 出，两者都是单拍脉冲。
- [RTL/adc_ad7928.v:32-39](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L32-L39) 给出最多 8 路 12bit 结果 `o_adc_value0..7`，注释明确：**只有 `o_en_adc` 那一拍它们才保证有效**。

结果寄存器与输出的连接很简单——`ch_value[]` 是 8 个 12bit 寄存器，直接 `assign` 到对应端口，见 [RTL/adc_ad7928.v:63-70](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L63-L70)。

#### 4.1.4 代码实践

**目标**：从端口层面确认「同步提交」的时序含义。

**步骤**（源码阅读型实践）：

1. 打开 [RTL/adc_ad7928.v:31](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L31) 与 [RTL/adc_ad7928.v:138-144](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L138-L144)。
2. 回答：`o_en_adc` 被置 1 的那一拍，`ch_value[idx]` 是否已经写入了本次最后一个通道的值？换句话说，`o_en_adc` 与最后一个 `ch_value[]` 的写入是同一拍发生吗？
3. 结合 `foc_top` 一侧（[RTL/fpga_top.v:118](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L118)）思考：FOC 在 `en_adc` 脉冲那一拍读 `adc_a/b/c`，会不会读到「尚未更新」的旧值？

**预期结果**：`o_en_adc<=nfirst` 与 `ch_value[idx]<=data_in_latch` 写在**同一个 always 块的同一拍**（[L141-L143](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L141-L143)），二者在下一个时钟沿同时生效。因此 `en_adc` 脉冲出现的那一拍，所有 `o_adc_value*` 已是本次结果——这正是「同步提交」的时序保证，FOC 可以放心在 `en_adc` 拍采样。

#### 4.1.5 小练习与答案

**Q1**：如果把 `o_en_adc` 改成电平信号（转换期间一直为 1，结束才回 0），会对 FOC 控制环造成什么麻烦？

> **参考答案**：FOC 流水线里多处使用「单拍脉冲 = 数据有效」的握手（Clark/Park/PI 都是）。电平型 `en_adc` 会让下游在每个高电平拍都误以为有新数据，导致同一组电流被重复处理；脉冲约定则保证「一拍启动↔一拍完成↔一拍消费」的一一对应。

---

### 4.2 通道参数化：CH_CNT 与 CH0..CH7 的自由配置

#### 4.2.1 概念说明

AD7928 物理上有 8 个输入通道（VIN0..VIN7），但一次 FOC 只需要 3 路相电流。作者没有把「采 3 路」写死，而是用一组参数让你自由决定：**单次启动采几个通道、每个逻辑槽位对应 AD7928 的哪个物理通道**。这让同一个模块既能用于 FOC（3 通道），也能用于将来需要更多通道的场合。

#### 4.2.2 核心流程

参数定义见 [RTL/adc_ad7928.v:11-20](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L11-L20)：

```verilog
parameter [2:0] CH_CNT = 3'd7,  // 单次转换用 CH_CNT+1 个通道
parameter [2:0] CH0   = 3'd0,   // CH0 槽位 -> AD7928 物理通道号
parameter [2:0] CH1   = 3'd1,
...
parameter [2:0] CH7   = 3'd7
```

- `CH_CNT`：决定一次 `i_sn_adc` 触发后采几个通道（`CH_CNT+1` 个）。示例取 2 ⇒ 采 CH0/CH1/CH2 共 3 个。
- `CH0..CH7`：把「逻辑槽位编号」映射到「AD7928 物理通道号」。示例里 `CH0=1, CH1=2, CH2=3`，即 A 相电流接到 AD7928 的物理通道 1、B 相接 2、C 相接 3。

为了在硬件里查表方便，代码把这 8 个参数打包成一个数组，见 [RTL/adc_ad7928.v:44-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L44-L52)：

```verilog
wire [2:0] channels [0:7];
assign channels[0] = CH0;  // 之后用 channels[addr] 就能取到物理通道号
...
```

通道数越多，`sn_adc` 到 `en_adc` 的延时越长（注释见 [L12](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L12)），这是 4.4 节要算的硬约束之一。

#### 4.2.3 源码精读

- 参数与含义：[RTL/adc_ad7928.v:11-20](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L11-L20)（注释里给出了 `CH_CNT=0/2/7` 三种典型取值）。
- 数组化查表：[RTL/adc_ad7928.v:44-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L44-L52)，使得状态机里只需一个变量 `addr` 就能索引到当前要采的物理通道。

#### 4.2.4 代码实践

**目标**：体会参数化带来的可移植性。

**步骤**：

1. 假设你换了一块电机驱动板，硬件上把三相电流分别接到了 AD7928 的物理通道 4/5/6，且只需采这 3 路。请写出新的例化参数。
2. 再假设某个应用需要同时采 3 路相电流 + 1 路母线电压（共 4 路），母线电压在物理通道 0。写出参数。
3. 对照 [RTL/fpga_top.v:78-82](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L82) 检查你的写法是否与现有风格一致。

**预期结果**：

- 场景 1：`.CH_CNT(3'd2), .CH0(3'd4), .CH1(3'd5), .CH2(3'd6)`。
- 场景 2：`.CH_CNT(3'd3), .CH0(3'd4), .CH1(3'd5), .CH2(3'd6), .CH3(3'd0)`，并在 `fpga_top` 里把 `o_adc_value3` 接到一个新增的 `adc_value_vbus` 上。

注意：增加通道数会拉长 `sn_adc→en_adc`，需重新核算 4.4 节的时序约束。

#### 4.2.5 小练习与答案

**Q1**：`CH_CNT` 的注释说「用的通道越多，从 sn_adc 到 en_adc 的时间差越长」。结合状态机，每多采一个通道，大约多花多少个 `clk`？

> **参考答案**：多一个通道 = 多一轮完整的 SPI 转换。一轮转换遍历 `cnt=0..38` 共 39 个 `clk` 周期（见 4.3），所以每多一个通道约多花 39 个 `clk`（约 1.06µs @ 36.864MHz）。

---

### 4.3 SPI 时序状态机：cnt 与 idx 的协作

#### 4.3.1 概念说明

这是本模块最核心的部分。模块用两个计数器协作：

- `cnt [7:0]`：**帧内节拍计数器**，编码一次单通道 SPI 转换的全过程（拉低 SS → 装载控制字 → 产生 16 个 SCK 周期串行收发 → 收尾）。
- `idx [2:0]`：**通道轮次计数器**，从 `CH_CNT` 递减到 0，决定当前在采第几个逻辑通道、结果存进哪个 `ch_value[idx]`。

辅助寄存器（见 [RTL/adc_ad7928.v:54-61](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L54-L61)）：`addr`（当前物理通道号）、`wshift`（MOSI 移位寄存器，装控制字）、`data_in_latch`（MISO 移位寄存器，收结果）、`sck_pre`（SCK 的「预置值」，真正的 `spi_sck` 比它晚一拍）。

#### 4.3.2 核心流程

**SCK 的产生**：`spi_sck <= sck_pre`（[L72-L76](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L72-L76)），而 `sck_pre` 在 SPI 活跃区每个 `clk` 翻转一次，所以 SCK 的周期 = 2 个 `clk`：

\[
f_{SCK} = \frac{f_{clk}}{2}
\]

本例 `clk=36.864MHz` ⇒ \(f_{SCK}=18.432\,\text{MHz}\)，恰好满足 AD7928「SCK 不超过 20MHz」的要求——这也是 README 里强调主时钟不能超过 40MHz 的根本原因（[README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) 「时钟配置」一节）。

**单通道转换的 `cnt` 节拍**（`WAIT_CNT=6`，见 [L42](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L42)）：

| `cnt` 取值 | 分支 | 作用 |
| :--- | :--- | :--- |
| `0` | [L86-L94](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L86-L94) | 空闲/触发：SS 拉高；若 `idx!=0` 则 `idx--` 续采；若 `idx==0 && i_sn_adc` 则 `idx<=CH_CNT` 启动新轮 |
| `1` | [L95-L98](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L95-L98) | 计算 `addr`（带 −1 预判，见下） |
| `2` | [L99-L102](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L99-L102) | 把控制字装入 `wshift` |
| `3,4,5` | [L103-L105](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L103-L105) | 等待（建立时间） |
| `6..37` | [L106-L111](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L106-L111) | **SPI 活跃区**：SS=0，`sck_pre` 翻转，MOSI 移出 `wshift`，MISO 移入 `data_in_latch`（32 个 `clk` = 16 个 SCK 周期） |
| `38` | [L112-L116](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L112-L116) | 收尾：`cnt<=0`，回到起点 |

可见一次单通道转换 = 39 个 `clk` 周期。

**控制字组帧**：[RTL/adc_ad7928.v:101](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L101) 把 12bit 控制字拼好：

```verilog
wshift <= {1'b1, 1'b0, 1'b0, channels[addr], 2'b11, 1'b0, 1'b0, 2'b11};
```

其中 `channels[addr]` 这 3bit 是**参数化的物理通道号**，其余是 AD7928 控制寄存器的固定配置位（编码方式、软件模式、不自动排序等，具体位含义见 AD7928 数据手册）。

**`addr` 的 −1 预判**：注意 [L97](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L97) 写的是 `addr <= (idx==0) ? CH_CNT : idx-1`，用的是 `idx-1` 而非 `idx`。这是因为 SPI ADC（如 AD7928）在收到控制字后，本次 DOUT 上回读的往往是**上一帧所选通道**的转换结果（一帧流水线延迟）。作者用 `idx-1` 的「提前一拍选通道」，正好让结果在 `idx` 这一槽位上「对号入座」，最终使 `o_adc_value0/1/2` 与 `fpga_top` 里标注的 A/B/C 相对应。

> 说明：上述「一帧流水线延迟」是 AD7928 这类串行 ADC 的典型行为，是解释 `idx-1` 写法的合理依据；精确的通道对应关系建议结合数据手册与本模块仿真（见 4.3.4）最终确认。

**MISO 接收**：在 SPI 活跃区内，当 `spi_sck` 为高时把 `spi_miso` 串行移入 `data_in_latch`，见 [RTL/adc_ad7928.v:135-137](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L135-L137)。

#### 4.3.3 源码精读

- 主状态机全貌：[RTL/adc_ad7928.v:78-117](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L78-L117)。`cnt==0` 处的 [L86-L94](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L86-L94) 是状态机的「调度中枢」：续采（`idx--`）或响应 `i_sn_adc` 启动新轮（`idx<=CH_CNT`）。
- SPI 活跃区与移位：[RTL/adc_ad7928.v:106-111](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L106-L111)，`{spi_mosi,wshift} <= {wshift,1'b1}` 是「带 1 填充的右移」，MSB 先发。
- 接收与提交：[RTL/adc_ad7928.v:120-145](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L120-L145)。

#### 4.3.4 代码实践

**目标**：用仿真量出「一次 `i_sn_adc` 到 `o_en_adc`」到底花了多少个 `clk`（FAQ 里作者说「具体数字我忘了，你可以仿真确定一下」）。

**步骤**：

1. 仿照 `SIM/` 下已有 testbench 的写法（参考 u1-l4），写一个最小的 `tb_adc.v`：产生 `clk`/`rstn`，给 `i_sn_adc` 一拍脉冲，例化 `adc_ad7928` 时用 `.CH_CNT(3'd2), .CH0(3'd1), .CH1(3'd2), .CH2(3'd3)`（与 `fpga_top` 一致），用 `$dumpvars` 把 `cnt/idx/addr/spi_ss/spi_sck/o_en_adc` 都 dump 出来。
2. 因为没有真实的 AD7928 器件模型，可把 `spi_miso` 直接接到地或一个固定模式，重点观察**控制信号时序**而非转换精度。
3. 用 gtkwave 测量：从 `i_sn_adc` 上升沿到 `o_en_adc` 上升沿之间有多少个 `clk`；再测一次单通道转换（相邻两次 SS 拉低之间）是多少个 `clk`。

**预期结果（待本地验证）**：

- 单通道转换 ≈ 39 个 `clk`；
- 3 通道一轮 ≈ 117 个 `clk`（即 \(3\times 39\)），在 36.864MHz 下约 3.17µs。

如果你懒得搭仿真，也可纯靠数 `cnt` 的取值范围推得上述结论（见 4.3.2 的表格）。

#### 4.3.5 小练习与答案

**Q1**：为什么 `spi_sck` 不是直接 `assign` 出来，而是用 `sck_pre` 打一拍（[L72-L76](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L72-L76)）？

> **参考答案**：寄存器输出 `spi_sck` 可以保证 SCK 相对 `clk` 有干净的建立/保持时间，避免组合逻辑产生的毛刺，使 SPI 时序更稳健；同时让 SCK 与 MOSI/MISO 的移位动作（都基于 `sck_pre`）严格对齐。

**Q2**：`WAIT_CNT=6` 里的「等待」段（`cnt=3,4,5`）起什么作用？

> **参考答案**：在拉低 SS、装好控制字之后、正式发起 SCK 之前，留出几拍建立时间，给 AD7928 足够的 SS 有效→SCK 起始间隔（datasheet 要求的 t1/t2 等时序参数），保证器件正确识别帧起始。

---

### 4.4 同步提交、o_en_adc 脉冲与可移植性抽象

#### 4.4.1 概念说明

本节回答两个问题：(1) 串行采样的多通道结果如何「看起来像同时给出」；(2) 为什么这种抽象让 `foc_top` 不在乎你用的是 1 片串行 ADC 还是 3 片并行 ADC。

AD7928 片内只有一个采样保持器，本质上是**轮流**采 A、B、C 相的，并非真正同步。但作者让控制器在全部通道采完后再统一提交，对 `foc_top` 隐藏了「轮流」这一事实。FAQ 里作者专门解释了这样做为何可行、以及为何如此设计接口。

#### 4.4.2 核心流程

**同步提交**（[RTL/adc_ad7928.v:138-144](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L138-L144)）：

```verilog
end else if(cnt==WAIT_CNT+8'd32) begin
    if(idx == 3'd0) begin
        nfirst  <= 1'b1;
        o_en_adc <= nfirst;   // 仅在最后一通道完成时发一拍脉冲
    end
    ch_value[idx] <= data_in_latch;  // 本通道结果落盘
end
```

- 每个通道采完（`cnt==38`）都把结果写进自己的 `ch_value[idx]`；
- **只有** `idx==0`（最后一个通道）完成时才置一拍 `o_en_adc`；
- `nfirst` 用来屏蔽上电后第一轮（`idx` 从 7 倒数到 0 的预热过程）的误触发：首轮 `nfirst` 仍为 0，故 `o_en_adc<=0`；此后 `nfirst=1`，每轮结束都会发出有效的完成脉冲。

**可移植性抽象**：从 `foc_top` 视角看，ADC 控制器只需满足三条时序契约：

1. 收到 `sn_adc` 脉冲 ⇒ 开始工作；
2. 工作结束 ⇒ 在 `en_adc` 上发一拍脉冲；
3. `en_adc` 那一拍，`adc_a/adc_b/adc_c` 上同步出现有效结果。

只要满足这三条，`foc_top` 根本不关心你是用 1 片 AD7928 串行采 3 次，还是用 3 片并行 ADC 同时采。换 ADC 型号时，只需重写粉色控制器、维持这套握手时序即可，蓝色 FOC 核心算法一行都不用动。

#### 4.4.3 源码精读

- `nfirst`/`o_en_adc`/`ch_value` 的复位与提交逻辑：[RTL/adc_ad7928.v:120-145](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L120-L145)。注意 [L134](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L134) 每拍默认把 `o_en_adc<=0`，只在 `idx==0 && cnt==38` 时覆写为 1，从而形成天然的单拍脉冲。
- FAQ 对「单 T/H 为何够用」「为何设计同步接口」的官方解释：[README.md FAQ「关于 ADC 采样时机」](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md)。

#### 4.4.4 代码实践（本讲主实践）

**目标**：结合 FAQ，解释「单 T/H 的 AD7928 为何能用于三相采样」，并算清那条采样窗口的硬约束。

**任务 A：解释单 T/H 够用**

请阅读 [README.md FAQ](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) 「关于 ADC 采样时机」的两条问答，用自己的话写出：既然 A/B/C 是轮流采的，为什么误差可忽略？

> **参考答案要点**：三相电流以控制周期（约 55µs）为尺度变化。例如 3000r/min、极对数=7 的电机，相电流约 350Hz（周期 ≈2.86ms）。而三轮串行采样总共只占采样窗口内的几微秒（约 3.17µs），相对电流变化周期短了三个数量级，故不同步带来的误差可忽略。

**任务 B：算清时序约束**

约束的来源：从「三相下桥臂开始同时导通」到「ADC 真正把电流采下来」，电流必须仍处于下桥臂导通期（采样窗口）内，否则相电流通路被切断、采样无意义。这段时间由两部分组成：

\[
T_{total} = SAMPLE\_DELAY + T_{sn\_adc \rightarrow en\_adc}
\]

要求：

\[
T_{total} < T_{win}, \qquad T_{win} = 1024 - 2\cdot MAX_{AMP}
\]

代入 `fpga_top` 的实际参数（`SAMPLE_DELAY=120`、`MAX_AMP=384`，见 [RTL/fpga_top.v:109-110](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L109-L110)）：

\[
\begin{aligned}
T_{win} &= 1024 - 2\times 384 = 256\;\text{个 clk} \\
T_{sn\_adc \rightarrow en\_adc} &\approx 3 \times 39 = 117\;\text{个 clk} \\
T_{total} &= 120 + 117 = 237\;\text{个 clk}
\end{aligned}
\]

比较：\(237 < 256\)，留有 19 个 `clk`（约 0.5µs）的余量，约束成立 ✅。

**操作步骤**：

1. 量出 \(T_{sn\_adc \rightarrow en\_adc}\) 的精确值（用 4.3.4 的仿真，或数 `cnt`）。
2. 代入上式，验证是否 \(< 256\)。
3. 思考：如果把 `MAX_AMP` 调大到 480（窗口 = \(1024-960=64\) 个 clk），\(120+117=237 > 64\)，约束被破坏——这就是 README 里强调「`MAX_AMP` 不能太大」的定量原因。

**预期结果**：默认参数下 \(237<256\) 成立；增大 `MAX_AMP` 或 `SAMPLE_DELAY`、或增加采样通道数都会挤压余量，极端情况下采样窗口关门前 ADC 还没采完，导致电流采样错乱。

#### 4.4.5 小练习与答案

**Q1**：如果换用 3 片**并行** ADC（同时采、同时出结果），`adc_ad7928.v` 这个模块还要不要？`foc_top` 要不要改？

> **参考答案**：`adc_ad7928.v` 要被替换成一个新的「并行 ADC 控制器」，但**接口契约不变**——它仍要在收到 `sn_adc` 后、采完后用一拍 `en_adc` 同步提交 `adc_a/b/c`。只要满足这三条，`foc_top` 完全不用改。这正是同步提交抽象的价值：把「硬件相关」隔离在粉色模块内，蓝色核心对 ADC 拓扑无感。

**Q2**：为什么 `nfirst` 的初值要参与 `o_en_adc` 的生成，而不是简单地「每轮都发脉冲」？

> **参考答案**：上电后 `idx` 从 7 倒数到 0 的第一遍是「预热」，此时各 `ch_value[]` 还没有有效数据，若发 `o_en_adc` 会让 FOC 采到垃圾值。用 `nfirst` 屏蔽首轮，保证只有真正完成过一轮采样后才向 FOC 报告「数据就绪」。

---

### 4.5 fpga_top 中的例化与三通道接线

#### 4.5.1 概念说明

最后把镜头拉回顶层 `fpga_top.v`，看 `adc_ad7928` 是怎么被实例化、怎么和 `hold_detect`（产 `sn_adc`）、`foc_top`（消费 `en_adc`+三相值）连成一个闭环的。这一节属于 `fpga_top` 模块。

#### 4.5.2 核心流程

数据通路（粉色控制器在中间，左连握手、右连核心）：

```text
   foc_top.hold_detect ──sn_adc──► adc_ad7928 ──(SPI)──► AD7928 芯片
                                      │
                      (en_adc 脉冲 + adc_a/b/c 同步)
                                      ▼
                                   foc_top (电流重构 ia=ADCb+ADCc-2·ADCa)
```

#### 4.5.3 源码精读

例化代码在 [RTL/fpga_top.v:78-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100)：

- 参数：`.CH_CNT(3'd2), .CH0(3'd1), .CH1(3'd2), .CH2(3'd3)`（[L79-L82](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L79-L82)），注释写明 A/B/C 相分别接 AD7928 物理通道 1/2/3。
- SPI 四线直连芯片（[L86-L89](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L86-L89)）。
- 握手：`.i_sn_adc(sn_adc)`（[L90](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L90)）接来自 `foc_top` 的 `sn_adc`；`.o_en_adc(en_adc)`（[L91](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L91)）回送给 `foc_top`。
- 三相结果：`.o_adc_value0(adc_value_a), .o_adc_value1(adc_value_b), .o_adc_value2(adc_value_c)`（[L92-L94](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L92-L94)），其余 5 路悬空（[L95-L99](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L95-L99)）。

注意 `sn_adc`/`en_adc` 是 `fpga_top` 内的 `wire`（[L36-L37](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L36-L37)），它既是 `foc_top` 的输出/输入、又是 `adc_ad7928` 的输入/输出——顶层用 `wire` 把两个模块的握手对缝连起来，形成闭环。这些 ADC 原始值随后进入 `foc_top`，经 u2-l2 的 `ia=ADCb+ADCc-2·ADCa` 重构出实际相电流。

#### 4.5.4 代码实践

**目标**：把「角度→采样→电流」这条链在顶层连线上走一遍。

**步骤**（源码阅读型实践）：

1. 在 [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) 中找到 `sn_adc` 的所有驱动点与消费点：它由谁产生（提示：`foc_top` 内部的 `hold_detect`，见 u2-l8）、被谁消费（`adc_ad7928` 的 `i_sn_adc`）。
2. 同样追踪 `en_adc`：由 `adc_ad7928` 产生，被 `foc_top` 消费。
3. 追踪三相值 `adc_value_a/b/c`：从 `adc_ad7928` 的输出端口，到 `foc_top` 的 `adc_a/b/c` 输入端口。
4. 画出这条闭环的信号流图，标注每段信号名与方向。

**预期结果**：你能得到 4.5.2 那样的闭环框图，并确认 `adc_ad7928` 是闭环中唯一的「数字↔模拟」边界（再往外就是 AD7928 芯片与电机驱动板等橙色硬件）。

#### 4.5.5 小练习与答案

**Q1**：`adc_value_a/b/c` 被声明为 `wire [11:0]`（[L38-L40](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L38-L40)），但它们既被 `adc_ad7928` 当输出、又被 `foc_top` 当输入。这会冲突吗？

> **参考答案**：不会。`adc_value_a/b/c` 作为 `wire` 只有一个驱动源——`adc_ad7928` 的 `output` 端口（连续赋值驱动）；`foc_top` 把它们接在自己的 `input` 端口上只是「读」。一个 `wire` 只能被一个 `output/assign` 驱动，但可以被任意多个 `input` 读取，所以这是合法的单向数据流。

---

## 5. 综合实践

把本讲所有要点串成一个任务：**为一颗「每轮采样耗时更短」的虚拟 ADC 重新核算采样窗口约束**。

背景：假设你要把 AD7928 换成一颗并行 ADC，它收 `sn_adc` 后只需 20 个 `clk` 就能同时给出三相结果（即 \(T_{sn\_adc \rightarrow en\_adc}=20\)），但接口契约与 `adc_ad7928` 完全一致（同样的 `sn_adc`/`en_adc`/`adc_a/b/c`）。

请完成：

1. **接口层**：参照 [RTL/fpga_top.v:78-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100) 写出新控制器 `adc_parallel.v` 的模块端口声明（端口名与时序契约不变）。
2. **参数层**：新 ADC 只有 3 个固定通道，不再需要 `CH_CNT/CH0..7`，说明可以删掉哪些 parameter。
3. **时序层**：重新计算约束 \(SAMPLE\_DELAY + 20 < 1024 - 2\cdot MAX_{AMP}\)。在 `SAMPLE_DELAY=120` 下，求允许的 `MAX_AMP` 最大值（取整数），并与原 AD7928 方案的 384 对比——这说明并行 ADC 能允许更大的 `MAX_AMP`（更大力矩），因为采样窗口可以更短。
4. **移植层**：说明这次更换中，`foc_top.v` 与 `fpga_top.v` 各需要改什么、不改什么，体会「同步提交抽象」带来的隔离价值。

> 参考答案要点：(1) 端口完全照抄 `adc_ad7928` 的用户逻辑接口部分；(2) 可删除 `CH_CNT/CH0..CH7`；(3) \(120+20=140 < 1024-2M \Rightarrow M < 442\)，即 `MAX_AMP` 最大可取 441，明显大于 384；(4) `foc_top.v` 一行不改，`fpga_top.v` 只需把 `adc_ad7928` 的例化换成 `adc_parallel`（参数也相应精简），闭环接线（`sn_adc`/`en_adc`/`adc_value_*`）原样保留。

## 6. 本讲小结

- `adc_ad7928.v` 是粉色「硬件相关」的通用 AD7928 控制器，靠 `sn_adc` 脉冲启动、`en_adc` 脉冲完成，是数字 FOC 与模拟电流之间的唯一边界。
- `CH_CNT` 与 `CH0..CH7` 一组参数把「采几路、各路对应哪个物理通道」完全参数化，使同一模块能适配不同应用。
- `cnt`（帧内节拍）/`idx`（通道轮次）双计数器驱动 SPI：每通道约 39 个 `clk`，SCK = `clk/2` ≤ 20MHz；`addr=idx-1` 的预判用于补偿串行 ADC 的读回流水线。
- 多通道结果是**串行采集、同步提交**：只有最后一通道（`idx==0`）完成时才发 `o_en_adc` 一拍脉冲，同拍交付全部 `o_adc_value*`。
- 这套「一启动脉冲↔一完成脉冲↔同步数据」的抽象，让 `foc_top` 对 ADC 拓扑无感——换并行 ADC 也只需维持时序契约，蓝色核心不动。
- 硬约束：\(SAMPLE\_DELAY + T_{sn\_adc\rightarrow en\_adc} < 1024 - 2\cdot MAX_{AMP}\)；默认参数下 \(120+117=237<256\) 成立，余量约 19 个 `clk`。

## 7. 下一步学习建议

- 至此粉色「传感器控制器」三件套（I2C 角度读取 u3-l1、SPI ADC 本讲、UART 监视 u3-l3）已讲完两件。下一讲 **u3-l3** 将精读 `uart_monitor.v`，看它如何把 `id/iq/id_aim/iq_aim` 转成串口字符，并解读 `fpga_top` 里的用户逻辑（`iq_aim` 在 ±200 间切换）。
- 若想深入「采样窗口」与 `MAX_AMP` 的定量关系，回头重读 **u2-l7（svpwm）** 的末段时序与 **u2-l8（hold_detect）**，把窗口公式 \(1024-2\cdot MAX_{AMP}\) 与本讲的 ADC 耗时闭环理解透。
- 进阶读者可尝试为 `adc_ad7928.v` 写一个 testbench（如 4.3.4），用仿真精确量出 `sn_adc→en_adc` 的拍数，验证本讲的 39/117 估算。
