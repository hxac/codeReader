# SPI ADC 读取 adc_ad7928.v

## 1. 本讲目标

上一讲（[u3-l1](u3-l1-i2c-angle-read.md)）我们打开了 FOC 的第一个输入黑盒——转子机械角度 φ 的来源（AS5600 + I2C）。本讲打开第二个输入黑盒：**三相电流的来源**。

FOC 电流环要算 id/iq，就必须知道实时的 ia/ib/ic；而 ia/ib/ic 又来自 ADC 对电机驱动板三相采样电阻的测量。本仓库用的是 **AD7928**，一颗 8 通道、12 位、最高 1MSPS 的 SPI 接口 ADC。本讲精读它的控制器 [RTL/adc_ad7928.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v)，学完后你应当掌握：

- 看懂用 `CH_CNT` 与 `CH0..CH7` 两层 parameter 自由配置「单次转换用几个通道、每个逻辑通道对应 AD7928 哪个物理通道」。
- 看懂 `cnt`/`idx` 状态机如何驱动 SPI 的 `ss/sck/mosi/miso`，把多个通道**串行**地逐个采样，并把结果拼装到 8 个寄存器里。
- 理解 `i_sn_adc`（开始）→ 串行采样 → `o_en_adc`（同步提交）这条抽象接口，以及为何它让 `foc_top` 不关心你到底用的是 1 颗串行 ADC 还是 3 颗并行 ADC。
- 会算「采样窗口」这条硬约束：`SAMPLE_DELAY + (sn_adc→en_adc 的时间差)` 必须小于三相下桥臂同时导通的窗口长度。

> 承接：本讲与 [u2-l8 hold_detect](u2-l8-hold-detect.md) 是一对搭档——`hold_detect` 决定「**何时**采样」（在采样窗口内延迟若干拍后发 `sn_adc`），本讲的 `adc_ad7928` 决定「**怎么**采样」（收到 `sn_adc` 后串行读 3 路、同步提交）。

## 2. 前置知识

### 2.1 SPI 是什么

SPI（Serial Peripheral Interface，串行外设接口）是一种主从式、全双工的串行总线，常用 4 根线：

| 信号 | 方向（主→从） | 作用 |
| :-- | :-- | :-- |
| `SCK` | 主→从 | 串行时钟，主机产生，每一个边沿传送 1 个比特 |
| `SS`（也叫 CS） | 主→从 | 片选，拉低表示「现在跟这颗从机说话」 |
| `MOSI` | 主→从 | 主出从入，主机发给从机的数据 |
| `MISO` | 从→主 | 主入从出，从机回给主机的数据 |

一次 SPI 传输通常是把一个「控制字」从 MOSI 移位送给从机，**同时**把从机的「转换结果」从 MISO 移位读回来——收发是同时进行的（这是 SPI 全双工的特性）。

### 2.2 AD7928 的两个关键特点

读 [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) 的 FAQ（关于 ADC 采样时机）会看到两个对理解代码至关重要的事实：

1. **AD7928 只有一个采样保持器（T/H）**，意味着它一次只能采一个通道。要采三相电流（A/B/C 三路），必须**串行**地切换通道、连采 3 次，而不是 3 路同时采。
2. **它的 SPI 时钟不能超过 20MHz**。[README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) 指出本模块用「二分频」产生 `spi_sck`（即 `spi_sck` 频率 = `clk`/2），所以主时钟 `clk` 必须 ≤ 40MHz——这正是顶层选 36.864MHz 的原因之一。

### 2.3 为什么串行采样也能用于 FOC

FAQ 给出的核心论证：三相电流是正弦变化的，但其变化周期是**毫秒级**的（例如 3000r/min、极对数=7 的电机，相电流约 350Hz，周期约 2.8ms）；而采样窗口只有**几微秒**，3 次串行采样的总耗时也在微秒级。在微秒尺度内，正弦电流几乎不变，因此「串行采的 3 个值」可以近似看作「同一时刻的三相电流」。再加上本模块在 3 次采样结束后才**同步提交**结果（一次性给 foc_top），foc_top 拿到的就是一个干净的三相快照。

## 3. 本讲源码地图

| 文件 | 角色 |
| :-- | :-- |
| [RTL/adc_ad7928.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v) | **本讲主角**。通用 AD7928 SPI 控制器：收到 `i_sn_adc` 脉冲后串行采样配置好的若干通道，最后同步提交结果并产生 `o_en_adc` 脉冲。属「粉色」硬件相关逻辑。 |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | 顶层。例化 `adc_ad7928`，把 `CH_CNT=2`、`CH0/1/2=1/2/3` 接好，并用 `sn_adc`/`en_adc` 与 `foc_top` 握手。 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | 蓝色 FOC 核心。定义了 `sn_adc`（输出，命令采样）与 `en_adc`（输入，结果有效）这对抽象接口，并用 `hold_detect` 产生 `sn_adc`。 |
| [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) | FAQ 里关于「单 T/H 如何采三相」「采样窗口」的两段问答是本讲实践任务的依据。 |

## 4. 核心概念与源码讲解

### 4.1 模块定位与可配置通道参数

#### 4.1.1 概念说明

`adc_ad7928` 是一颗**通用**的 AD7928 控制器，所谓「通用」体现在：它不写死「采 3 路、固定第 1/2/3 通道」，而是用两层 parameter 把这件事完全交给使用者配置：

- `CH_CNT`：单次转换使用多少个通道（用的是 `CH_CNT+1` 个）。
- `CH0..CH7`：每个**逻辑通道编号**对应 AD7928 的哪个**物理通道**。

这样做的好处是：换电机、换接线时，只需改 parameter，模块内部状态机一行都不用动。

#### 4.1.2 核心流程

逻辑通道与物理通道的映射在模块内用一个 8 元素数组 `channels[0:7]` 承载，`channels[i]` 就是 `CHi` 的值。每次要采「逻辑通道 i」时，就把 `channels[i]` 拼进发给 AD7928 的控制字里。状态机按 `idx` 从 `CH_CNT` 递减到 0，依次采样逻辑通道 `CH_CNT, CH_CNT-1, …, 0`。

#### 4.1.3 源码精读

parameter 定义见 [RTL/adc_ad7928.v:11-20](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L11-L20)，注释里清楚说明了 `CH_CNT` 的取值含义（`CH_CNT=0` 只用 CH0，`CH_CNT=2` 用 CH0/CH1/CH2，依此类推）：

```verilog
parameter [2:0] CH_CNT = 3'd7,  // 单次 ADC 转换使用的通道数为 CH_CNT+1
parameter [2:0] CH0 = 3'd0,     // CH0 对应 AD7928 的哪个通道
...
parameter [2:0] CH7 = 3'd7
```

把 parameter 装进可索引数组，见 [RTL/adc_ad7928.v:44-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L44-L52)：

```verilog
wire [2:0] channels [0:7];
assign channels[0] = CH0;
...
assign channels[7] = CH7;
```

用户逻辑侧的端口（与 SPI 物理端口分离）见 [RTL/adc_ad7928.v:30-39](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L30-L39)：`i_sn_adc` 是「开始采样」脉冲输入，`o_en_adc` 是「结果有效」脉冲输出，`o_adc_value0..7` 是 8 路结果。

顶层的实际接法见 [RTL/fpga_top.v:78-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100)：

```verilog
adc_ad7928 #(
    .CH_CNT ( 3'd2 ),   // 只用 CH0,CH1,CH2 这三个通道
    .CH0    ( 3'd1 ),   // CH0 对应 AD7928 物理通道1（A 相）
    .CH1    ( 3'd2 ),   // CH1 对应物理通道2（B 相）
    .CH2    ( 3'd3 )    // CH2 对应物理通道3（C 相）
) u_adc_ad7928 ( ... );
```

即：逻辑通道 0/1/2 分别绑定 A/B/C 三相，对应 AD7928 的物理通道 1/2/3；其余 5 路结果端口悬空（`.o_adc_value3()` 等），不予理会。

#### 4.1.4 代码实践

1. **实践目标**：验证 parameter 语义，搞清「逻辑通道」与「物理通道」的对应关系。
2. **操作步骤**：
   - 打开 [RTL/adc_ad7928.v:11-20](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L11-L20)，确认 `CH_CNT` 与「通道数 = CH_CNT+1」的关系。
   - 打开 [RTL/fpga_top.v:78-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100)，对照注释确认 A/B/C 分别接到了物理 1/2/3。
3. **需要观察的现象**：在注释里看到「硬件上 A 相电流连接到 AD7928 的通道1」等说明。
4. **预期结果**：`CH_CNT=2` ⇒ 用 3 个通道；`o_adc_value0/1/2` ⇒ `adc_value_a/b/c` ⇒ A/B/C 相。
5. 假设你把 B、C 两相在驱动板上接反了，**应只改 fpga_top.v 的哪两个 parameter** 就能纠正，而无需改 `adc_ad7928.v`？（答：把 `.CH1` 与 `.CH2` 的值对调即可。）

#### 4.1.5 小练习与答案

- **练习 1**：若想同时监视母线电压（接到 AD7928 物理通道 5），最少要改哪些地方？
  - **答**：把 fpga_top 里 `CH_CNT` 改成 `3'd3`，新增 `.CH3(3'd5)`，并把 `.o_adc_value3( )` 接到一根新 wire 上供用户逻辑使用。`adc_ad7928.v` 不用改。代价是采样时间变长一拍（多一个通道）。
- **练习 2**：为什么 `CH_CNT` 是 3 位宽？
  - **答**：AD7928 最多 8 个通道（0~7），`CH_CNT` 取值 0~7 恰好指定「用到第几个通道为止」，3 位足够。

---

### 4.2 SPI 串行采样的状态机

#### 4.2.1 概念说明

收到 `i_sn_adc` 脉冲后，模块要**串行**地完成多次 SPI 传输（每个通道一次）。整个调度由两个计数器配合完成：

- `cnt`（8 位）：一次 SPI 传输内部的节拍（从拉低 SS、装载控制字、翻转 SCK 移位、到结束）。
- `idx`（3 位）：当前在采第几个逻辑通道，从 `CH_CNT` 递减到 0。

每完成一次 SPI 传输，`idx` 减 1，进入下一个通道；`idx` 减到 0 并完成最后一次传输后，回到空闲，等待下一个 `i_sn_adc`。

#### 4.2.2 核心流程

一次 SPI 传输（即 `cnt` 从 0 扫到 38 再回 0）分为若干阶段，**共 39 个时钟周期**：

| `cnt` 取值 | 阶段 | 做什么 |
| :-- | :-- | :-- |
| 0 | 触发/转移 | 若 `idx≠0`：继续下一通道（`idx--`）；若 `idx==0` 且 `i_sn_adc` 有效：开启新一轮（`idx<=CH_CNT`）。总线保持空闲（`ss/sck/mosi=111`）。 |
| 1 | 选通道 | 计算本次要采的物理通道号 `addr`。 |
| 2 | 装控制字 | 把含 `channels[addr]` 的 12 位控制字装入移位寄存器 `wshift`。 |
| 3,4,5 | 建立等待 | 保持总线空闲，给 SS 以建立时间（`WAIT_CNT=6`）。 |
| 6 ~ 37 | SPI 活跃 | `spi_ss<=0`（选中芯片），`sck_pre` 每拍翻转（产生 16 个 SCK 边沿＝16 位传输）；同时 MOSI 移位送出控制字、MISO 移位读回结果。 |
| 38 | 结束 | 收尾，`cnt<=0`，准备下一个通道。 |

> 关于 `spi_sck` 的产生：[RTL/adc_ad7928.v:72-76](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L72-L76) 里 `spi_sck <= sck_pre`，而活跃期 `sck_pre <= ~sck_pre` 每拍翻转一次，所以 `spi_sck` 的周期 = 2 个 `clk`，即 SPI 时钟 = `clk`/2 = 36.864MHz/2 ≈ 18.4MHz < 20MHz，满足 AD7928 要求。

控制字的拼装见 [RTL/adc_ad7928.v:99-102](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L99-L102)：

```verilog
wshift <= {1'b1, 1'b0, 1'b0, channels[addr], 2'b11, 1'b0, 1'b0, 2'b11};
```

其中 `channels[addr]` 这 3 位是送给 AD7928 的**通道选择字段**，其余是 AD7928 控制寄存器的固定配置位（具体每一位的物理含义需对照 AD7928 数据手册，此处不臆断）。

#### 4.2.3 源码精读

主状态机见 [RTL/adc_ad7928.v:78-117](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L78-L117)。关键分支：

- 触发新一轮采样的入口（`cnt==0 && idx==0 && i_sn_adc`）在 [RTL/adc_ad7928.v:86-94](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L86-L94)：把 `idx` 重新装为 `CH_CNT`，开启第一拍。
- 通道号计算在 [RTL/adc_ad7928.v:95-98](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L95-L98)：`addr <= (idx == 3'd0) ? CH_CNT : idx - 3'd1;`（这个相对 `idx` 的「错位 1 拍」会在 4.3 解释原因）。
- SPI 活跃期（SCK 翻转 + MOSI 移位）在 [RTL/adc_ad7928.v:106-111](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L106-L111)，其中 `{spi_mosi, wshift} <= {wshift, 1'b1};` 是「左移送出 MSB、低位补 1」的标准移位写法。

MISO 移位读回在第二个 always 块的 [RTL/adc_ad7928.v:135-137](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L135-L137)：在 SCK 高电平期间把 `spi_miso` 串行塞进 `data_in_latch`。

#### 4.2.4 代码实践

1. **实践目标**：数清楚一次 SPI 传输占多少拍、整轮采多少拍，为后面算时序约束做准备。
2. **操作步骤**：
   - 在 [RTL/adc_ad7928.v:78-117](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L78-L117) 里逐段标注 `cnt` 的区间：`0`→`1`→`2`→`3..5`→`6..37`→`38`。
   - 数一下 `cnt` 从 0 到 38 共经历多少个值（=39）。
3. **需要观察的现象**：每采一个通道经历一次完整的 `cnt` 0→38 扫描。
4. **预期结果**：单通道 = 39 拍；`CH_CNT=2`（3 通道）整轮 ≈ \(3 \times 39 = 117\) 个 `clk`，即 `sn_adc` 到 `en_adc` 的时间差约为 117 拍（≈ \(117 / 36.864{\rm MHz} \approx 3.17\mu s\)）。
5. **待本地验证**：上述拍数是据代码人工推得；建议用 iverilog 仿真（给 `i_sn_adc` 一个脉冲，看 `o_en_adc` 在第几拍出现）来确认精确值——FAQ 里作者也说「具体数字我忘了，你可以仿真确定一下」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 SCK 活跃期（`cnt` 6~37）恰好是 32 拍？
  - **答**：32 拍里 `sck_pre` 每拍翻转一次，产生 16 个完整 SCK 周期 = 16 个比特的传输，正好对应 AD7928 的 16 位 SPI 帧。
- **练习 2**：如果把 `WAIT_CNT` 调大，会怎样？
  - **答**：`cnt` 的空闲建立期变长，单次传输拍数增加，`sn_adc→en_adc` 延迟变大，采样窗口的预算更紧张。一般无需改动。

---

### 4.3 结果同步提交与首拍抑制（nfirst）

#### 4.3.1 概念说明

这是本模块最巧妙的两点，也是初学者最容易看不懂的地方：

1. **同步提交**：3 个通道**不是**各采完就各上报，而是全部采完后，在同一拍把 3 个结果一起送上 `o_adc_value0/1/2`，并同时拉高一个周期的 `o_en_adc`。这样 `foc_top` 看到的是一个「三相快照」，而不是 3 个错开的值。
2. **首拍抑制（`nfirst`）**：模块上电后第一次跑完整轮时，`o_en_adc` **不**拉高；从第二次起才正常拉高。

为什么要抑制第一次？因为像 AD7928 这类 SPI ADC 通常存在**转换流水线延迟**——某次 SPI 传输从 MISO 读回的，其实是「上一次传输所寻址通道」的转换结果（本次写入的控制字要等下一次传输才生效）。代码里两处设计正是为了消化这一点：

- `addr` 故意取 `channels[idx-1]`（相对 `idx` 错位），使得存入 `ch_value[idx]` 的结果恰好对应逻辑通道 `idx`；
- `nfirst` 把上电后第一轮（流水线尚未对齐、结果无效）的结果吞掉，不上报。

> 说明：以上「流水线延迟」是对代码自洽性（`addr` 错位 + `nfirst`）的最合理解释；精确的延迟拍数与位序请以 AD7928 数据手册和仿真为准（待确认）。

#### 4.3.2 核心流程

第二个 always 块（[RTL/adc_ad7928.v:120-145](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L120-L145)）负责结果收集与提交：

```
每拍默认 o_en_adc<=0
若处于 SPI 活跃期(cnt∈[8,38)) 且 SCK 为高：
    把 miso 串行移入 data_in_latch
否则若 cnt==38（一次传输结束）：
    ch_value[idx] <= data_in_latch        // 存到对应通道寄存器
    若 idx==0（本轮最后一个通道）：
        nfirst <= 1                        // 标记「已经不是第一次了」
        o_en_adc <= nfirst                 // 用旧值决定是否上报
```

关键在 `o_en_adc <= nfirst` 用的是 `nfirst` 的**旧值**（非阻塞赋值右边读旧值）：第一轮 `nfirst` 旧值为 0 ⇒ `o_en_adc=0`（抑制）；同时 `nfirst` 被置 1。此后每轮 `nfirst` 旧值为 1 ⇒ `o_en_adc=1`（正常上报）。

`addr` 与 `idx` 的错位关系（承接 4.2）：传输 `idx` 时寻址的是 `channels[idx-1]`（`idx==0` 时回绕到 `channels[CH_CNT]`），但读回的结果存入 `ch_value[idx]`。结合「读回的是上一次寻址通道的结果」，可推出 `ch_value[idx]` 最终正好持有 `channels[idx]` 的数据——与端口命名一致。

#### 4.3.3 源码精读

结果收集与提交逻辑见 [RTL/adc_ad7928.v:133-144](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L133-L144)：

```verilog
o_en_adc <= 1'b0;                                   // 默认不脉冲
if(cnt>=WAIT_CNT+8'd2 && cnt<WAIT_CNT+8'd32) begin
    if(spi_sck)
        data_in_latch <= {data_in_latch[10:0], spi_miso};   // 移位读回
end else if(cnt==WAIT_CNT+8'd32) begin
    if(idx == 3'd0) begin
        nfirst  <= 1'b1;
        o_en_adc <= nfirst;                         // 用旧值：首轮抑制
    end
    ch_value[idx] <= data_in_latch;                 // 存结果
end
```

8 个结果寄存器到端口的连续赋值见 [RTL/adc_ad7928.v:63-70](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L63-L70)：`o_adc_valuei = ch_value[i]`。所以只要 `o_en_adc` 脉冲一拉高，`foc_top` 就能在 `adc_value_a/b/c` 上同时拿到 3 个有效结果。

#### 4.3.4 代码实践

1. **实践目标**：理解 `nfirst` 如何保证「上电后第一次不上报脏数据」。
2. **操作步骤**：
   - 在 [RTL/adc_ad7928.v:138-144](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v#L138-L144) 处标注：第一轮 `idx==0` 时 `nfirst`（旧）=0 ⇒ `o_en_adc=0`；同时 `nfirst<=1`。
   - 跟踪第二轮：`idx==0` 时 `nfirst`（旧）=1 ⇒ `o_en_adc=1`，脉冲正常发出。
3. **需要观察的现象**：`o_en_adc` 的**第一个**脉冲出现在第二轮结束时，而不是第一轮。
4. **预期结果**：上电后 `foc_top` 的 `en_adc` 不会因脏数据误触发；待 `init_done` 结束、流水线对齐后，每个控制周期稳定收到一个 `en_adc` 脉冲。
5. **待本地验证**：`nfirst` 仅在 `idx==0` 时才可能置位；若上电后迟迟没有 `i_sn_adc`（即 `hold_detect` 未产生脉冲），`nfirst` 会一直为 0——这是符合预期的，因为初始化阶段本就不该采样。

#### 4.3.5 小练习与答案

- **练习 1**：如果删掉 `nfirst` 机制（直接 `o_en_adc <= 1'b1`），首轮会出什么问题？
  - **答**：上电后第一轮各通道寄存器里可能装的是「未对齐流水线」读回的错位/无效值，`foc_top` 会基于错误的三相电流做 Clark/Park/PI，导致初始化尾部出现一次错误电流冲击。
- **练习 2**：`o_en_adc <= nfirst` 为什么不能写成 `o_en_adc <= 1'b1` 配合一个单独的「已就绪」标志？
  - **答**：可以等价改写，但作者用「`nfirst` 旧值」一个信号同时承担「标记是否首次」和「决定本轮是否上报」两职，是最省寄存器的写法。

---

### 4.4 foc_top 的 ADC 抽象接口与可移植性

#### 4.4.1 概念说明

回头看 [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) 对 ADC 的依赖，只有 5 个信号：

| 信号 | 方向（相对 foc_top） | 含义 |
| :-- | :-- | :-- |
| `sn_adc` | 输出 | 「该采样了」的单拍脉冲 |
| `en_adc` | 输入 | 「结果有效」的单拍脉冲 |
| `adc_a/b/c` | 输入 | 12 位三相 ADC 原始值 |

这是一个**高度抽象的「同步读入 3 通道」接口**：`foc_top` 完全不关心你背后用的是 1 颗 AD7928 串行 ADC，还是 3 颗并行 ADC，还是别的什么型号——只要你满足「收到 `sn_adc` 后，在合理时间内回一个 `en_adc` 脉冲，并在此时把 3 个结果同步送上 `adc_a/b/c`」即可。FAQ 把这一点讲得很明白：这种抽象是为了通用性、简约性和可移植性。

#### 4.4.2 核心流程

闭环数据通路（与 [u2-l8](u2-l8-hold-detect.md) 的图景对接）：

```
svpwm 输出 pwm_a/b/c
        │
        ▼
hold_detect 检测三相同时为低（采样窗口），延迟 SAMPLE_DELAY 拍
        │  sn_adc 脉冲
        ▼
adc_ad7928 串行采 3 路，同步提交
        │  en_adc 脉冲 + adc_a/b/c
        ▼
foc_top 电流重构 ia/ib/ic → Clark → Park → PI → … → svpwm
```

注意 `sn_adc` 与 `en_adc` 在顶层是同一根 wire 被 `foc_top` 与 `adc_ad7928` 共享：`foc_top`（经 `hold_detect`）驱动 `sn_adc`，`adc_ad7928` 驱动 `en_adc`，方向互补，构成一次问答握手。

#### 4.4.3 源码精读

`foc_top` 对 ADC 接口的声明见 [RTL/foc/foc_top.v:28-30](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L28-L30)（`sn_adc` 输出、`en_adc` 与 `adc_a/b/c` 输入）。`sn_adc` 由 `hold_detect` 产生，见 [RTL/foc/foc_top.v:264-271](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L264-L271)，其输入是三相 PWM 的「全低」组合 `~pwm_a & ~pwm_b & ~pwm_c`（[第 269 行](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L269)）。

顶层把这 5 个信号在 `adc_ad7928` 与 `foc_top` 之间对接，见 [RTL/fpga_top.v:90-94](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L90-L94)：

```verilog
.i_sn_adc      ( sn_adc         ),  // foc_top 发出 → adc_ad7928 接收
.o_en_adc      ( en_adc         ),  // adc_ad7928 发出 → foc_top 接收
.o_adc_value0  ( adc_value_a    ),  // ┐
.o_adc_value1  ( adc_value_b    ),  // ├─ 三相结果同步送 foc_top
.o_adc_value2  ( adc_value_c    ),  // ┘
```

而 `foc_top` 一侧的接线见 [RTL/fpga_top.v:117-121](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L117-L121)：`.sn_adc(sn_adc)`、`.en_adc(en_adc)`、`.adc_a(adc_value_a)` 等。

#### 4.4.4 代码实践

1. **实践目标**：体会「抽象接口」带来的可移植性，为日后换 ADC 型号打基础。
2. **操作步骤**：
   - 在 [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) 中搜索 `sn_adc`、`en_adc`，确认它们同时出现在 `u_adc_ad7928` 与 `u_foc_top` 两个例化里，方向相反。
   - 假设要把 AD7928 换成 3 颗并行的独立 ADC（每相一颗，结果同时就绪）。思考：新控制器需要向外提供哪几个信号才能无缝替换 `adc_ad7928`？
3. **需要观察的现象**：`sn_adc`/`en_adc` 是一对「命令—完成」的握手，三相结果是 3 根并行的 12 位线。
4. **预期结果**：新控制器只要暴露 `i_sn_adc`（输入）、`o_en_adc`（输出）、`o_adc_value0/1/2`（输出）这 5 个信号，并满足时序约定，就能直接替换，**蓝色 foc_top 一行都不用改**。
5. 若新 ADC 是 3 颗并行、结果几乎瞬时就绪，则 `sn_adc→en_adc` 的延迟会大幅缩短，采样窗口预算更宽松——这正是该抽象的价值。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `foc_top` 不直接例化 `adc_ad7928`，而是把它放在顶层、只留 5 个抽象信号？
  - **答**：解耦。蓝色 FOC 核心是硬件无关的「固定算法」，不应绑定任何具体 ADC 型号；把 ADC 控制器放在顶层（粉色硬件相关区），换型号时只改顶层例化，核心不动。
- **练习 2**：`adc_a/b/c` 是 12 位无符号原始值，`foc_top` 内部却用 16 位有符号电流。这之间是怎么衔接的？
  - **答**：在 [RTL/foc/foc_top.v:99-109](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L99-L109) 用 `Ia = ADCb + ADCc - 2*ADCa` 等公式重构出有符号电流（详见 [u2-l2](u2-l2-angle-and-current-recon.md)），这一步同时完成了「去偏置」和「无符号→有符号」。

## 5. 综合实践：验算采样窗口的时序硬约束

本实践把 4.2 的拍数计算与 [u2-l8](u2-l8-hold-detect.md) 的采样窗口公式串起来，算清 FAQ 反复强调的那条不等式。这也是本讲规格里要求的核心实践。

### 5.1 背景与 FAQ 问答

采样电阻在下桥臂，只有三相 PWM **同时为低**（三个下桥臂同时导通）时相电流才可测，这段公共低电平期就是**采样窗口**。FAQ 的问答（[README.md 关于 ADC 采样时机](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md)）给出两条要点：

- AD7928 只有一个 T/H，所以采样窗口内要**串行做 3 次采样**；`hold_detect` 在窗口开始时延时 `SAMPLE_DELAY` 后发 `sn_adc`，`adc_ad7928` 收到后串行采完，再**同步提交**并产生 `o_en_adc`。
- 用户必须自己算好：`hold_detect` 的延时 + ADC 采三通道（即 `sn_adc` 脉冲到 `en_adc` 脉冲的时间差）**必须小于采样窗口的长度**。

### 5.2 任务

结合 FAQ，说明 AD7928 只有一个采样保持器为何仍能用于 FOC 三相电流采样；并证明在默认参数下：

\[
{\rm SAMPLE\_DELAY} + T_{sn\_adc \to en\_adc} < T_{window}
\]

### 5.3 第一问：单 T/H 为何够用

- 因为 3 次串行采样都发生在**同一个采样窗口**（几微秒）内，而相电流以控制周期（几十微秒）为尺度变化，在几微秒内几乎不变，故 3 次串行值可近似看作「同一时刻」的三相电流。
- 又因为 `adc_ad7928` 在 3 次采样结束后才**同步提交**（`o_en_adc` 拉高时 3 个结果同时就位），`foc_top` 拿到的是一个对齐的三相快照，而非 3 个错开的值。这两点合起来，让单 T/H 的 AD7928 足以胜任 FOC 三相采样。

### 5.4 第二问：约束验算

1. **采样窗口长度 \(T_{window}\)**：由 [u2-l7/u2-l8](u2-l8-hold-detect.md) 已知，中心对齐 PWM 下三相同时为低的窗口长度为

\[
T_{window} = 2 \times (512 - {\rm MAX\_AMP}) \quad (\text{个 clk 周期})
\]

默认 `MAX_AMP=9'd384`，代入：

\[
T_{window} = 2 \times (512 - 384) = 2 \times 128 = 256
\]

2. **\(T_{sn\_adc \to en\_adc}\)**：由 4.2，单通道占 39 拍，`CH_CNT=2` 即 3 通道：

\[
T_{sn\_adc \to en\_adc} \approx 3 \times 39 = 117
\]

3. **SAMPLE_DELAY**：默认 `9'd120`。

4. **合计**：

\[
{\rm SAMPLE\_DELAY} + T_{sn\_adc \to en\_adc} = 120 + 117 = 237
\]

5. **比较**：

\[
237 < 256 \quad \checkmark
\]

余量约 19 拍（≈ \(19/36.864{\rm MHz} \approx 0.52\mu s\)）。换成时间：窗口 \(256/36.864{\rm MHz} \approx 6.94\mu s\)，链路耗时 \(237/36.864{\rm MHz} \approx 6.43\mu s\)。

### 5.5 约束的来源（要点）

- `MAX_AMP` 越大 ⇒ 力矩越大，但 \(T_{window}=2(512-{\rm MAX\_AMP})\) 越小；到 `MAX_AMP=511` 时窗口≈0，无法采样。这是「最大力矩 vs 可采样性」的根本折中。
- `SAMPLE_DELAY` 是等 MOS 管电流稳定的延时；它吃掉的预算直接从窗口里扣。
- 通道数（`CH_CNT+1`）越多 ⇒ \(T_{sn\_adc \to en\_adc}\) 越长，窗口预算越紧。
- 换言之：**增大 `MAX_AMP`、增大 `SAMPLE_DELAY`、增多采样通道，三件事都会让 237 这个数往 256 逼近，一旦越过就会采到错误电流。**

### 5.6 思考延伸

- 若把 `MAX_AMP` 提到 `9'd450`，窗口 = \(2\times(512-450)=124\) 拍，而链路仍需 237 拍 ⇒ **237 > 124，约束被破坏**，此时必须减小 `SAMPLE_DELAY` 或减通道数，或接受更小的 `MAX_AMP`。
- 若换用并行 ADC（3 颗同时采），\(T_{sn\_adc \to en\_adc}\) 可降到个位数拍，窗口约束瞬间宽裕——这正是抽象接口（4.4）带来的工程红利。

> 本节数据中的 117 拍为据代码人工推得，建议用 iverilog 仿真 `adc_ad7928`（独立给定 `i_sn_adc` 与一个假的 `spi_miso` 激励，量 `o_en_adc` 的延迟）来最终确认。

## 6. 本讲小结

- `adc_ad7928` 是通用 AD7928 SPI 控制器，用 `CH_CNT`+`CH0..CH7` 两层 parameter 自由配置「采几路、各对应哪个物理通道」，换接线只改 parameter。
- 调度核心是 `cnt`（单次 SPI 传输节拍，0→38 共 39 拍）× `idx`（通道计数，`CH_CNT`→0）的双重计数器；`spi_sck` 由 `clk` 二分频得到（≈18.4MHz < 20MHz）。
- 结果采用**同步提交**：3 路采完后在同一拍送上 `o_adc_value*` 并拉高 `o_en_adc`；`nfirst` 抑制上电后首轮脏数据，`addr` 相对 `idx` 的错位消化 AD7928 的转换流水线延迟。
- `foc_top` 对 ADC 的依赖只有 `sn_adc`/`en_adc`/`adc_a/b/c` 五个信号的「同步读入 3 通道」抽象，使蓝色核心与具体 ADC 型号彻底解耦——换并行 ADC 也只需满足此时序。
- 一条硬约束贯穿全链：\({\rm SAMPLE\_DELAY} + T_{sn\_adc \to en\_adc} < T_{window} = 2(512-{\rm MAX\_AMP})\)；默认参数下 \(120+117=237<256\) 刚好成立。

## 7. 下一步学习建议

- 至此，「粉色」传感器外设（[u3-l1](u3-l1-i2c-angle-read.md) 的 I2C 角度读取 + 本讲的 SPI ADC 读取）已全部讲完。下一篇 [u3-l3 UART 监视器与用户逻辑](u3-l3-uart-monitor-and-user-logic.md) 将讲「黄色」用户逻辑：如何用 UART 把 id/iq 打印出来看跟随曲线，以及如何改写用户行为实现不同的电机应用。
- 若想加深对本讲时序的理解，建议自己写一个 `adc_ad7928` 的 testbench（仿照 [SIM/tb_svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v) 的范式），量出 `i_sn_adc` 到 `o_en_adc` 的精确拍数，验证本讲的 117 拍估算——这也是 [u4-l3 仿真方法论](u4-l3-simulation-methodology.md) 会系统讲解的内容。
- 想了解参数整定与跨平台移植（比如换晶振频率、换极对数后这些参数怎么调），可预习 [u4-l2 参数整定与跨平台移植](u4-l2-parameter-tuning-and-porting.md)。
