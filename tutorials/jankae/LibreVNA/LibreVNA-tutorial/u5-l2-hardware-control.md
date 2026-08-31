# 射频硬件控制：时钟、PLL 与硬件门面

## 1. 本讲目标

上一讲（u5-l1）我们走完了固件从复位到事件主循环的启动链路，知道 `App_Init` 会调用 `HW::Init()` 完成硬件初始化。本讲把放大镜对准这一次调用，学完后你应该能够：

1. 解释 `HW` 命名空间（Hardware 门面）提供了哪些高层 API，以及一个抽象的测量意图（频率、功率）如何被分解成一串对 Si5351C、MAX2871、FPGA 的寄存器写操作。
2. 读懂 Si5351C 时钟树驱动与 MAX2871 PLL 驱动的配置流程，包括小数分频的数学与「影子寄存器 + 批量提交」的设计。
3. 理解 `HW_HAL` 这一层对 MCU 引脚、SPI/I2C 总线句柄和 Si5351 通道号的封装作用，以及「一条 SPI 总线如何被 FPGA 与两颗 PLL 分时复用」。
4. 独立跟踪一条完整的硬件动作调用链（如「把激励设到 3 GHz、-10 dBm」），并输出带寄存器含义的时序清单。

## 2. 前置知识

### 2.1 门面（Facade）与「影子寄存器」

固件要驱动的射频芯片（Si5351C、MAX2871）都是「写寄存器型」器件：你往一串寄存器里写正确的值，它就输出你想要的频率和功率。围绕这一点有两个工程惯例：

- **门面模式**：上层代码不想关心「先写哪个芯片、再等哪个锁定」，于是有一个中间层把常见意图封装成 `SetFrequency()`、`GetAmplitudeSettings()` 这样的高层函数。LibreVNA 中这个角色由 `HW` 命名空间（Hardware.cpp）承担。
- **影子寄存器（register shadow）**：驱动类在 RAM 里维护一份寄存器镜像（如 `MAX2871::regs[6]`）。`SetFrequency()` 之类的函数**只改 RAM 不碰总线**，等你显式调用 `Update()` 时才一次性按芯片要求的顺序把 6 个寄存器全部写出。好处是：可以在不占用 SPI 总线的时间点（例如扫描规划阶段）慢慢算好寄存器值，之后一次性提交。

### 2.2 PLL 与小数分频

锁相环（PLL）用鉴相器（PFD）比较参考频率 \( f_{PFD} \) 与压控振荡器（VCO）经分频后的反馈信号，调节 VCO 直到两者一致。此时：

\[ f_{VCO} = \left(N + \frac{F}{M}\right) \times f_{PFD} \]

其中 \(N\) 是整数分频比，\(F/M\) 是小数部分（由 Σ-Δ 调制器实现）。MAX2871 的频率规划就是把目标频率反解成 \(N, F, M\) 和输出分频 \(2^{div}\)。Si5351C 内部也是两级结构：先由 PLL 把晶振倍频到 600–900 MHz，再由多路合成器（Multisynth）分频到目标频率，分频比同样写成「整数 + 分数 \(b/c\)」。

### 2.3 cdbm 与 0.25 dB 步进衰减器

固件内部功率单位是 **cdbm**（centi-dBM，1 dBm = 100 cdbm），例如 `-1000` 表示 -10 dBm。激励幅度由两级器件共同决定：源芯片的输出档位（粗调，几档固定功率）+ 数字步进衰减器（细调，0.25 dB/步、共 127 步 ≈ 31.75 dB）。`HW::GetAmplitudeSettings()` 的职责就是把 cdbm 拆成「档位 + 衰减值」。

### 2.4 与前面讲义的衔接

- u1-l1 的 RF 框图：激励源 Si5351C（<25 MHz）/MAX2871（≥25 MHz）、两级下变频。本讲你会看到这些频率关系在代码里的对应常量（`DefaultIF1 = 62 MHz`、`DefaultIF2 = 250 kHz` 等）。
- u5-l1 的启动链路：`HW::Init()` 是 `App_Init` 十五步中的一步；本讲展开它的内部。
- u4-l1 的通信层：本讲提到的 `Protocol::DeviceStatus`、`Protocol::DeviceConfig` 等结构体最终都经 `Communication::Send()` 上报给 PC。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [Software/VNA_embedded/Application/Hardware.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp) | `HW` 门面的实现：初始化时序、幅度策略、模式切换、参考源管理、设备配置存取 |
| [Software/VNA_embedded/Application/Hardware.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp) | `HW` 命名空间的 API 与全部硬件频率常量（带 `static_assert` 自检） |
| [Software/VNA_embedded/Application/HW_HAL.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.cpp) | 全局硬件对象实例化：`Si5351`、`Source`、`LO1`、`flash` 各挂在哪条总线、哪些引脚 |
| [Software/VNA_embedded/Application/HW_HAL.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.hpp) | `HWHAL` 命名空间声明 + `SiChannel` 通道号到物理时钟的映射表 |
| [Software/VNA_embedded/Application/Drivers/Si5351C.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp) | Si5351C 时钟树驱动（I2C）：PLL/输出分频计算与寄存器打包 |
| [Software/VNA_embedded/Application/Drivers/max2871.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp) | MAX2871 PLL 驱动（SPI）：频率规划、影子寄存器、VCO 映射、温度读取 |
| Software/VNA_embedded/Application/Generator.cpp | 甲方示例：信号源模式的硬件编排（本讲综合实践的主线） |
| Software/VNA_embedded/Application/VNA.cpp | 甲方示例：VNA 扫描如何把 PLL 寄存器「预推送」给 FPGA |
| Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp | FPGA 驱动中的 `SetMode`（SPI 路由）、`WriteSweepConfig`、`OverwriteHardware` |

> 说明：本讲的四个核心文件是 Hardware.cpp、HW_HAL.cpp、Si5351C.cpp、max2871.cpp；Generator.cpp / VNA.cpp / FPGA.cpp 只节选与硬件编排直接相关的片段，深入的走读留给 u5-l4 与 u6 系列。

## 4. 核心概念与源码讲解

### 4.1 Hardware 门面：HW 命名空间

#### 4.1.1 概念说明

`HW` 是固件里所有射频硬件的「总管家」。它把两类职责收拢到一处：

1. **初始化与模式管理**：`HW::Init()` 按正确顺序唤醒时钟树、FPGA、两颗 PLL；`HW::SetMode()`/`HW::SetIdle()` 负责测量模式（VNA/SA/Generator/Manual/Idle）之间的硬件状态迁移。
2. **共享策略**：任何模式都需要的「公共计算」，例如把 cdbm 拆成档位+衰减的 `GetAmplitudeSettings()`、参考源（内/外接晶振）切换的 `Ref::` 子命名空间、设备配置（IF1、ADC 采样率等）的 flash 存取。

有一个容易误解的地方要先澄清：**`HW` 并不是纯粹的 gatekeeper**。模式层代码（Generator.cpp、VNA.cpp）会直接调用 `Si5351.SetCLK(...)`、`Source.SetFrequency(...)` 这样的驱动函数。换句话说，LibreVNA 的分层是「驱动层（芯片类）→ 策略层（HW 提供公共算法与初始化）→ 编排层（各模式自己写脚本）」，而不是「一切经过 HW 转发」。这个取舍减少了层层包装，代价是你读调用链时要多走一层。

#### 4.1.2 核心流程

`HW::Init()` 的编排顺序（为什么是这个顺序，比顺序本身更重要）：

```text
LoadDeviceConfig()                # 从 flash 读回 IF1/ADC 配置，后面算频率要用
Si5351.Init()                     # I2C 唤醒时钟芯片，先关全部输出（安全态）
Si5351.SetPLL(A, 800MHz, XTAL)    # PLL A：恒定 800 MHz，供各路参考
Si5351.SetPLL(B, LO2×13, XTAL)    # PLL B：二本振的 13 倍频（802.75 MHz）
配置 CLK3/CLK5 = 100MHz → 两颗 MAX2871 的参考
配置 CLK7 = 16MHz  → FPGA 主时钟
配置 CLK1/2/4 = 61.75MHz → 三路二本振
ResetPLL(B) + WaitForLock          # 复位对齐相位（见源码注释中的 issue #280）
FPGA::Init()                       # 此刻 FPGA 时钟已存在，才能初始化
FPGA::WriteRegister(ADCPrescaler / PhaseIncrement / SettlingTime)
配置 Source MAX2871（BuildVCOMap 自学习 VCO 边界）
配置 LO1  MAX2871（初始频率 = 1GHz + IF1）
FPGA::WriteMAX2871Default(Source.GetRegisters())  # 影子寄存器交给 FPGA
Ref::update()                      # 参考源最终裁决
```

要点：**时钟树必须先于 FPGA、先于 PLL**——FPGA 需要 16 MHz 时钟才能响应 SPI，MAX2871 需要 100 MHz 参考才能锁相。这就是「编排顺序体现硬件依赖」的活教材。

幅度策略的算法（`GetAmplitudeSettings`）可以概括为：

1. 若 `applyCorrections`，先叠加 `Cal::SourceCorrection(freq)`（设备级幅度校准，见 u5-l3）。
2. 按频率选波段：\( f < 25\,\text{MHz} \) 用 Si5351 低段（档位 = 驱动强度 mA2/mA8），否则用 MAX2871 高段（档位 = 芯片功率 n4dBm/p5dBm）。
3. 档位只有「低」「高」两档，剩余功率全部交给衰减器：\( \text{attval} = \lfloor -\Delta \, /\, 25 \rfloor \)（25 cdbm = 0.25 dB/步）。
4. 衰减值溢出 [0,127] 区间则置 `unlevel = true`——这正是 `HW::SetOutputUnlevel()` 要上报给 PC 的「未校准输出」标志。

#### 4.1.3 源码精读

**(a) 硬件常量全部集中在头文件，并用 `static_assert` 自检**

[Hardware.hpp:L29-L54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L29-L54) 定义了本讲所有频率关系的「单一事实来源」：TCXO 26 MHz、PLL A 恒定 800 MHz、FPGA 时钟 16 MHz、一级中频 62 MHz、二级中频 250 kHz、波段切换点 25 MHz、PLL 参考 100 MHz 等。其中：

- `DefaultADCprescaler = FPGA::Clockrate / DefaultADCSamplerate`（[Hardware.hpp:L51-L54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L51-L54)）后跟 `static_assert`，保证 800 kHz ADC 采样率与 DFT 相位增量能被整数除法精确表示——编译期就拦住「频率组合不可实现」的错误。
- `DefaultLO2 = DefaultIF1 - DefaultIF2`（61.75 MHz）、`SI5351CPLLAlignedFrequency = DefaultLO2 × 13`（802.75 MHz）：二本振由 PLL B 整数倍频再分频得到，**保证三路 LO2 相位对齐**。

**(b) `HW::Init()` 的时钟树编排**

[Hardware.cpp:L111-L153](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L111-L153) 依次配置 Si5351 的两个 PLL 与八路输出中的六路，并对 PLL B 做复位+等锁。注意 [Hardware.cpp:L136-L139](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L136-L139) 的注释：二本振频率只在初始化时设定一次，之后改变频率时改的是 PLL B 而不是输出分频器，否则三路输出会间歇性相位翻转（issue #280）——这是「时钟树不是随便配的」的绝佳例子。

**(c) 两颗 MAX2871 的初始化与 VCO 自学习**

[Hardware.cpp:L172-L208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L172-L208)：先 `FPGA::Enable(SourceChip)` + `FPGA::SetMode(SourcePLL)` 把 SPI 路由到源 PLL，`Source.Init(100MHz, ...)` 后调用 `Source.BuildVCOMap()`（详见 4.2.3），再以 1 GHz 起步；随后同样流程配置 LO1，起始频率为 `1000000000 + IF1`（**LO1 = 激励 + IF1**，与本振混频后得到 62 MHz 一中频）。最后 [Hardware.cpp:L208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L208) 把源 PLL 的影子寄存器整体推给 FPGA（`WriteMAX2871Default`），供扫描期间 FPGA 自主驱动。

**(d) 幅度策略 `GetAmplitudeSettings`**

[Hardware.cpp:L272-L316](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L272-L316)。低段两档功率的物理含义在 [Hardware.hpp:L66-L70](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L66-L70)：`LowBandMinPower = -1350`（Si5351 驱动 mA2 时端口近似输出 -13.5 dBm）、`HighBandMaxPower = -160`（MAX2871 +5 dBm 档时端口近似 -1.6 dBm）等。衰减步进 25 cdbm 见 [Hardware.cpp:L304](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L304) 的 `attval = -cdbm / 25`。

**(e) `SetOutputUnlevel` 与状态上报**

[Hardware.cpp:L336-L338](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L336-L338) 只有一行：把标志存进静态变量 `unlevel`。真正消费它的是 [Hardware.cpp:L340-L372](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L340-L372) 的 `getDeviceStatus()`：读两颗 PLL 温度、读 FPGA 统计的 ADC min/max 判断过载，连同 `unlevel`、锁定状态、参考源状态一起填进 `Protocol::DeviceStatus` 发给 PC（GUI 状态栏上的温度/UNL 指示就来自这里）。**这就是「高层 API 分解为底层操作」的反向链路：底层状态聚合成高层标志**。

**(f) 参考源管理 `Ref::`**

[Hardware.cpp:L386-L429](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L386-L429)：`Ref::update()` 按需切换内部 26 MHz TCXO 与外部 10 MHz 参考——切换的本质是重配 Si5351 两个 PLL 的参考源（`PLLSource::XTAL` ↔ `CLKIN`），并等待重新锁定。自动模式下 `Ref::available()` 直接查 Si5351 的 CLKIN 检测位（[Si5351C.cpp:L326-L334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp#L326-L334)）。

#### 4.1.4 代码实践

**实践：核对 `HW::Init()` 与 RF 框图的对应关系（源码阅读型，无需硬件）**

1. **实践目标**：验证你从 u1-l1 学到的射频架构（激励源、两本振、FPGA 时钟）能逐路对应到 `HW::Init()` 中的 Si5351 配置语句。
2. **操作步骤**：
   - 打开 [Hardware.cpp:L111-L153](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L111-L153)；
   - 对照 [HW_HAL.hpp:L20-L31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.hpp#L20-L31) 的 `SiChannel` 映射，为每条 `SetCLK`/`SetPLL` 语句填写一张表：通道号 | 物理用途 | 频率 | 来源 PLL | 驱动强度；
   - 用 [Hardware.hpp:L35-L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L35-L47) 的常量验算 PLL B 频率是否等于 \( (62\,\text{MHz} - 250\,\text{kHz}) \times 13 \)。
3. **需要观察的现象**：你会得到一张 8 行左右的时钟分配表，其中 CLK0（LowbandSource）在本段未被配置——思考它何时才被配置（答案：各模式按需配置，见 Generator.cpp/VNA.cpp）。
4. **预期结果**：PLL A 800 MHz 派生出 100 MHz（两路 PLL 参考）+ 16 MHz（FPGA）+ 低段激励；PLL B 802.75 MHz 派生出三路 61.75 MHz 二本振。若你的表格能完整复现两级下变频所需的全部本振，即通过。
5. 本实践为纯源码阅读，结论可直接从代码得出，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`HW::SetMode()`（[Hardware.cpp:L217-L240](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L217-L240)）在切换到非 Idle 模式时会完整重跑一遍 `HW::Init()`。为什么「VNA → SA」这种切换宁可重新初始化全部硬件，也不做增量迁移？

**答案**：模式间硬件状态差异巨大（不同本振规划、不同射频开关、不同 FPGA 配置），增量迁移需要维护一张「当前状态 vs 目标状态」的差异表，容易漏。完整重跑 `Init()` 的代价只是几百毫秒的锁定等待（PLL 重锁、VCO 映射重建约需遍历 3–6 GHz 每 10 MHz 一点），换来的是**从已知安全态出发的确定性**。这是嵌入式里典型的「重初始化优于状态机」取舍。

**练习 2**：`GetAmplitudeSettings(-4000, 30 MHz)`（-40 dBm，高段）会返回什么？是否 unlevel？

**答案**：高段路径，`cdbm=-4000 > HighBandMinPower=-1060` → 高档 `p5dbm`，`cdbm = -4000 - (-160) = -3840`；`attval = 3840/25 = 153 > 127` → 被钳到 127（≈31.75 dB），`unlevel = true`。也就是说 -40 dBm 超出了 -1.6 dBm 档位 + 31.75 dB 衰减的组合能力（约 -33.35 dBm），设备只能给出 -33.35 dBm 并亮 UNL。这也解释了 [Hardware.hpp:L84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L84) 中 `limits_cdbm_min = -4000` 但实际仍可能 unlevel 的边界行为。

**练习 3**：`HW::TimedOut()`（[Hardware.cpp:L318-L334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L318-L334)）监测的是什么信号？为什么它对一个「硬件门面」来说是必要功能？

**答案**：它检查 `lastISR`——FPGA 采样完成中断的最近触发时间戳（在 [Hardware.cpp:L73-L76](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L73-L76) 的 `FPGA_Interrupt` 里刷新）。若活动测量模式超过 1 秒没有收到 FPGA 中断，说明硬件链路（PLL 失锁、FPGA 配置丢失等）出了问题，上层可以据此复位。门面既然负责把硬件带起来，也就天然负责「看门」。

### 4.2 时钟与 PLL 驱动：Si5351C 与 MAX2871

#### 4.2.1 概念说明

两颗合成器芯片分工如下（对应 u1-l1 框图）：

| 芯片 | 总线 | 角色 | 输出 |
|---|---|---|---|
| Si5351C | I2C（`hi2c2`） | 时钟树 + 低段激励 + 二本振 | CLK0 低段激励；CLK1/2/4 三路 61.75 MHz 二本振；CLK3/5 两路 100 MHz PLL 参考；CLK6 参考输出；CLK7 FPGA 16 MHz |
| MAX2871 ×2 | SPI（`hspi1`，经路由） | 高段激励（Source）、一本振（LO1） | 23.5 MHz–6 GHz |

两个驱动的**公共设计**值得先记住：

1. **影子寄存器**：`Si5351C` 把配置拆成 `PLLConfig`/`ClkConfig` 结构体再打包成字节序列；`MAX2871` 直接维护 `uint32_t regs[6]`。`SetFrequency` 类函数只算不写。
2. **显式提交**：`MAX2871::Update()`/`UpdateFrequency()` 按 datasheet 要求的顺序（先 R5，再 R4→R0）写出；Si5351 则在每次 `SetCLK`/`SetPLL` 内部立即写 I2C（它的寄存器没有「一次性提交」的时序约束）。
3. **读回能力**：Si5351 靠读状态寄存器判断锁定；MAX2871 靠 MUX 引脚（锁存检测）和 SPI 读回寄存器 6 完成 VCO 自学习与温度测量。

#### 4.2.2 核心流程

**Si5351C 输出频率的数学**：PLL 倍频到 \( f_{PLL} \) 后，每路 Multisynth 分频比为 \( a + b/c \)（\(a\) 为整数部分，\(b/c\) 为最接近的真分数）：

\[ f_{out} = \frac{f_{PLL}}{a + b/c} \qquad a \in [24, 351],\; c \le 2^{20}-1 \]

驱动用 `FindOptimalDivider` 把 \( b/c \) 选成对剩余频率的最佳有理逼近，再按 AN619 的公式换算成硬件参数：

\[ P_1 = 128a + \left\lfloor \frac{128b}{c} \right\rfloor - 512, \qquad P_2 = 128b - c\left\lfloor \frac{128b}{c} \right\rfloor, \qquad P_3 = c \]

CLK6/7 是例外：只有**偶数整数分频**（6–254），这也是为什么 10 MHz 参考输出（800/80）与 16 MHz FPGA 时钟（800/50）恰好落在可用区间，且 [Hardware.hpp:L56-L63](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L56-L63) 用 `static_assert` 强制了「偶数、6–254」这两个条件。

**MAX2871 输出频率的数学**：

\[ f_{out} = \frac{(N + F/M) \times f_{PFD}}{2^{div}} \]

频率规划（`SetFrequency`）四步：① 查表选输出分频 `div`，把 \( f_{VCO} \) 拉进 3.0–6.1 GHz 的 VCO 区间；② 若已有 VCO 映射则手动选 VCO（绕过芯片自动切换、加速锁定）；③ \( N = f_{VCO}/f_{PFD} \)，余数 \( f_{rem} \) 用有理逼近拆成 \( F/M \)；④ 把 `div`、`N`、`F`、`M`、VCO 号按 datasheet 的位域写进影子寄存器 R0/R1/R3/R4。

#### 4.2.3 源码精读

**(a) Si5351C::SetCLK —— 分频规划与 R 分频器**

[Si5351C.cpp:L78-L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp#L78-L121)：先按 `clknum > 5` 分流到整数分频支路（CLK6/7），否则用 `while` 循环倍增 `RDiv`（后置 R 分频器，1–128）把频率压进 Multisynth 的合法窗口（≥500 kHz 且分频比 <2048），最后 `FindOptimalDivider` 求 P1/P2/P3。

**(b) Si5351C::FindOptimalDivider —— 有理逼近**

[Si5351C.cpp:L349-L376](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp#L349-L376)。非精确模式下直接取 \( c = 2^{20}-1 \)、\( b = f_{rem} \cdot c / f \)（一次乘除，够用）；`exact` 模式才调用 `Algorithm::BestRationalApproximation` 花力气找最优分数。随后三行就是上面 P1/P2/P3 公式的逐字实现。注意 [VNA.cpp:L213](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L213) 调 `SetPLL` 时传的就是默认 `exactFrequency=false`。

**(c) Si5351C::WriteClkConfig —— 寄存器打包**

[Si5351C.cpp:L226-L292](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp#L226-L292)：把 `ClkConfig` 结构体拆成 CLKxControl 控制字节（电源、PLL 选择 A/B、反相、驱动强度）和 8 字节的 Multisynth 参数块（P1/P2/P3 + RDiv），经 `WriteRegisterRange` 一次 I2C 写入。驱动强度到控制位的关系见 [Si5351C.cpp:L250-L262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp#L250-L262)：mA2=00、mA4=01、mA6=10、mA8=11——**低段激励的「功率粗调」其实就是这 2 个 bit**。

**(d) Si5351C 的 I2C 底座**

[Si5351C.cpp:L294-L324](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp#L294-L324)：`WriteRegister`/`ReadRegister` 直接包 STM32 HAL 的 `HAL_I2C_Mem_Read/Write`（100 ms 超时），`SetBits`/`ClearBits` 实现「读-改-写」。驱动与 MCU 之间的全部耦合就是构造时传入的 `I2C_HandleTypeDef*`。

**(e) MAX2871::Init —— 安全态与上电时序**

[max2871.cpp:L15-L69](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L15-L69)：清零影子寄存器、关闭芯片与 RF 输出、配参考分频（决定 \( f_{PFD} \)）、再按 datasheet 注释逐位设置环路滤波极性、数字锁存检测、fundamental 反馈等固定项，最后**按 R5→R4→R3→R2→R1→R0 的顺序写两遍**（中间 20 ms 延时）——这是上电初始化的硬性时序要求。

**(f) MAX2871::SetFrequency —— 频率规划主体**

[max2871.cpp:L135-L231](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L135-L231)。对照上一节的四步：输出分频查表在 [L146-L167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L146-L167)；手动 VCO 选择在 [L170-L181](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L170-L181)；\( N \) 与小数部分的求解在 [L182-L225](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L182-L225)，其中 [L205-L208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L205-L208) 处理「M 至少为 2」的硬件约束；位域写入在 [L220-L225](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L220-L225)（R4 的 bit20-22 是 div，R0 的 bit15-29 是 N、bit3-14 是 F，R1 的 bit3-14 是 M）。**整段函数没有任何 SPI 传输**——再次印证影子寄存器设计。

**(g) MAX2871::Write 与 Update —— 提交时序**

[max2871.cpp:L318-L330](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L318-L330)：一次写 = 4 字节 SPI（高 29 位数据 + 低 3 位寄存器号）+ LE 引脚脉冲锁存。[L302-L316](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L302-L316) 的 `Update()` 按 R5→R0 全量写出；`UpdateFrequency()` 只写频率相关的 R4/R3/R1/R0（跳过 R2/R5，减少切换点时的寄存器流量）。

**(h) MAX2871::BuildVCOMap —— 出厂自学习**

[max2871.cpp:L349-L405](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L349-L405)：从 3 GHz 到 6.1 GHz 每 10 MHz 设一次频率，把 MUX 引脚切到锁存检测、等锁定（100 ms 超时），再切到 SPI 读回读出芯片实际选择的 VCO 号，记录「每个 VCO 覆盖的最高频率」。此后 [L402-L403](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L402-L403) 关闭芯片的 VAS 自动切换、改为固件查表手动指定 VCO——**用启动时几十毫秒的自学习，换取每次变频时更短的锁定时间**（扫描速度的直接优化）。

#### 4.2.4 代码实践

**实践：手算 3 GHz 的 MAX2871 寄存器值（纸面推演 + 代码核对）**

1. **实践目标**：不运行任何代码，仅用纸笔和源码，推出 `Source.SetFrequency(3000000000)` 之后 `regs[]` 中与频率相关的位域值。
2. **操作步骤**：
   - 参考频率：`HW::Init` 中 `Source.Init(HW::PLLRef=100 MHz, doubler=false, r=1, div2=false)`（[Hardware.cpp:L176](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L176)），由 `SetReference`（[max2871.cpp:L233-L300](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L233-L300)）算出 \( f_{PFD} = 100\,\text{MHz} \)；
   - 3 GHz ≥ 3 GHz，查 [max2871.cpp:L146-L167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L146-L167) 的分频表：所有 `f < ...` 条件都不满足 → `div = 0`，\( f_{VCO} = 3\,\text{GHz} \)；
   - \( N = 3\times10^9 / 10^8 = 30 \)，\( f_{rem} = 0 \) → 小数部分为 0，`approx.denom` 被抬到 2（[max2871.cpp:L205-L208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L205-L208)）；
   - 写出结果：R0 的 N 域 = 30、F 域 = 0；R1 的 M 域 = 2；R4 的 div 域 = 0；R3 的 VCO 域由 VCO 映射表决定（`compare = f_vco/100000 = 30000`，取第一个 `VCOmax[vco] >= 30000` 的 vco）。
3. **需要观察的现象**：核对验算式 \( f_{out} = (30 + 0/2) \times 100\,\text{MHz} / 2^0 = 3.000000\,\text{GHz} \)，无频率偏差告警。
4. **预期结果**：3 GHz 恰好是 100 MHz 的整数倍，是这台机器上「最干净」的频率之一；再顺手算 2.999 GHz 验证小数路径：\( N=29 \)、\( f_{rem}=90\,\text{MHz} \)、\( F/M \approx 9/10 \)。
5. 本实践为纸面推演，公式与位域均直接来自源码，无需本地验证；若想真正跑起来，需要在 STM32CubeIDE 中加打印（见 4.3.4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么二本振用「PLL B 整数倍频 13 再分频」而不直接让每路输出做小数分频？

**答案**：三路二本振（端口 1、端口 2、参考）必须**同相**，否则接收机之间的相位差会直接污染 S 参数测量。Si5351 的三路 Multisynth 若各自独立做小数分频，相位关系不确定；共用同一个 PLL B、且分频比为整数（802.75/13 = 61.75）时三路严格同相。[Hardware.cpp:L136-L148](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L136-L148) 的注释与 `ResetPLL(B)`（宣称「重新对齐各时钟相位」）正对应这个约束。

**练习 2**：`MAX2871::Update()` 与 `UpdateFrequency()` 的差别是什么？各用在什么场合？

**答案**：`Update()` 写 R5→R0 全部六个寄存器，用于初始化或参考/环路参数变化后的全量同步；`UpdateFrequency()` 只写 R4/R3/R1/R0 四个频率相关寄存器，用于单纯的变频场合。`HW::Init` 用前者（[Hardware.cpp:L186](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L186)），Generator 模式连续改频率时用后者思路（[Generator.cpp:L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L47) 实际调的是 `Update()`，因为每次 Setup 都可能改功率档位）。

**练习 3**：`Si5351C::Locked()` 与 `MAX2871::Locked()` 的实现方式有何不同？

**答案**：Si5351C 读自身状态寄存器的 lock 位（[Si5351C.cpp:L167-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.cpp#L167-L177)，走 I2C）；MAX2871 的 `Locked()` 读 LD 引脚的 GPIO 输入寄存器（[max2871.cpp:L94-L96](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L94-L96)）——但注意 [HW_HAL.cpp:L4-L5](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.cpp#L4-L5) 实例化时 LD 传的是 `nullptr`，所以固件运行期实际上不查 MAX2871 的锁定，而是靠 FPGA 监测失锁中断（[Hardware.cpp:L361-L363](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L361-L363) 读 `FPGA::GetStatus()` 的 `LO1Unlock`/`SourceUnlock` 位）。这也是「驱动能力」与「板上实际接线」分离的一个例子。

### 4.3 HW_HAL 与板级引脚

#### 4.3.1 概念说明

`HW_HAL` 是「板级支持包」（BSP）的最薄形态：**把「哪条总线、哪个引脚、哪个通道」这类纯硬件事实集中到一个 7 行的 .cpp 和一个 33 行的 .hpp 里**，让 Hardware.cpp 和各模式代码可以用 `Si5351`、`Source`、`LO1` 这些有意义的名字编程，而不必到处出现 `hi2c2`、`GPIOA` 这样的裸句柄。它封装了三样东西：

1. **全局对象实例化**：Si5351C 挂 I2C2、26 MHz 晶振；两颗 MAX2871 挂 SPI1、LE 复用 FPGA 的 CS 引脚、MUX 读回接 PA6；Flash 挂 SPI1 加独立 CS。
2. **`SiChannel` 通道映射表**：把「CLK3 = 源 PLL 参考」这样的电路板事实写成枚举，模式代码里 `SiChannel::LowbandSource` 一眼可读。
3. **隐含的总线复用规则**：一条 SPI1 要轮流服务 FPGA、源 PLL、LO1 PLL、Flash，谁在听由 `FPGA::SetMode` 摆弄的 AUX1/AUX2/CS 三根线决定。

#### 4.3.2 核心流程

SPI 总线路由的状态机（[FPGA.cpp:L356-L382](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L356-L382)）：

```text
Mode::FPGA:     CS=高, AUX1=低, AUX2=低  → SPI 目标是 FPGA（42.5 MHz 快速档）
Mode::SourcePLL: CS=低, AUX1=高, AUX2=低 → SPI 直通到 Source MAX2871（10.625 MHz）
Mode::LOPLL:     CS=低, AUX1=低, AUX2=高 → SPI 直通到 LO1 MAX2871（10.625 MHz）
```

细节非常值得品：**切到 PLL 模式时 CS 拉低**——此时 FPGA 片选失效，而 MAX2871 的 LE（锁存使能）恰恰接在这根 CS 线上（[HW_HAL.cpp:L4-L5](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.cpp#L4-L5) 把 `FPGA_CS_Pin` 同时当作 LE 传入），于是 `MAX2871::Write()` 里 LE 的脉冲（[max2871.cpp:L327-L329](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L327-L329)）就能把移位寄存器内容锁进目标芯片。SPI 波特率也随模式切换（MAX2871 限 20 MHz，FPGA 可以跑更快）——**一根物理总线、三种逻辑设备，靠 GPIO 摆位 + 速率切换实现分时复用**。

另一块「引脚级」硬件控制是射频开关与衰减器。它们不接 MCU，而是由 FPGA 驱动，MCU 只写 FPGA 的硬件覆盖寄存器：[FPGA.cpp:L397-L410](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L397-L410) 的 `FPGA::OverwriteHardware(attenuation, filter, lowband, port1, port2)` 把衰减值（bit8-14）、低通滤波器档位（bit6-7，M947/M1880/M3500/直通，见 [FPGA.hpp:L79-L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp#L79-L85)）、波段与端口开关打包成 16 位写进 FPGA；各射频器件的使能位则定义在 `FPGA::Periphery` 枚举（[FPGA.hpp:L54-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp#L54-L68)）。

#### 4.3.3 源码精读

**(a) 七行的板级定义**

[HW_HAL.cpp:L3-L7](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.cpp#L3-L7)。逐参数读 `Source` 的构造：`hspi1`（总线）、`LE = FPGA_CS_GPIO_Port/Pin`（锁存使能复用 FPGA 片选）、`RF_EN/LD/CE = nullptr`（未接，相应方法直接返回）、`MUX = GPIOA, PIN_6`（读回引脚）。对照 [max2871.hpp:L7-L22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.hpp#L7-L22) 的构造函数参数表，就能在脑中画出「STM32 —MAX2871」的全部连线。

**(b) 通道映射表**

[HW_HAL.hpp:L20-L31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.hpp#L20-L31)。注意 CLK0–5 是可小数分频的 Multisynth（接 PLL A 或 B），CLK6–7 只有整数分频——映射表里 `ReferenceOut=6`、`FPGA=7` 恰好用满这两个特殊通道，频率也正好是偶数分频可达（800/80、800/50）。**电路设计与芯片资源的匹配在这里看得一清二楚**。

**(c) 模式层如何消费这些封装**

[Generator.cpp:L11-L65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L11-L65) 是最短的完整消费者：`using namespace HWHAL;` 之后直接用 `Si5351`、`SiChannel::LowbandSource` 编程。4.1.1 说的「HW 不是唯一入口」在这里得到印证——但它调用的**策略**（`HW::GetAmplitudeSettings`、`HW::SetMode`、`HW::SetOutputUnlevel`）与**驱动**（`Si5351.SetCLK`、`Source.SetFrequency`）分工清晰。

#### 4.3.4 代码实践

**实践：绘制板级连接表（源码阅读型，无需硬件）**

1. **实践目标**：产出一张「MCU 外设 → 目标芯片/引脚」的连接表，作为你后续读任何驱动代码的「地图」。
2. **操作步骤**：
   - 从 [HW_HAL.cpp:L3-L7](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.cpp#L3-L7) 提取四行实例化语句，填入表格：对象 | 总线 | 控制引脚 | 备注；
   - 从 [HW_HAL.hpp:L20-L31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.hpp#L20-L31) 提取 Si5351 的 8 个通道用途；
   - 用 `grep -n "AUX" Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp`（或阅读 [FPGA.cpp:L356-L382](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L356-L382)）补全 SPI 路由的三个模式。
3. **需要观察的现象**：表格中 SPI1 一行会出现四个「共享者」（FPGA、Source、LO1、Flash），I2C2 只有一个（Si5351）。
4. **预期结果**：约 12 行的连接表。附加思考题：为什么 Si5351 独享 I2C 而 MAX2871 必须与 FPGA 分时复用 SPI？（提示：MAX2871 需要 ≥10.625 MHz 的时钟速率与 FPGA 直通路径，且 LE 复用省了一根引脚；I2C 带地址天然支持总线共享。）
5. 本实践为纯文档整理，无需本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`FPGA::SetMode(FPGA::Mode::FPGA)` 为什么要同时把 SPI 波特率调到 42.5 MHz，而 PLL 模式降到 10.625 MHz？

**答案**：MAX2871 的 SPI 时序上限是 20 MHz（源码注释明确写了），FPGA 则可以承受更高时钟。切模式时同步改 `SPI_CR1` 的分频（[FPGA.cpp:L364-L373](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L364-L373)），让每种目标都跑在各自最高安全速率上——扫描时要把几百个点的 PLL 寄存器表推进 FPGA，速率直接决定配置耗时。

**练习 2**：`HW_HAL.hpp` 里的 `SiChannel` 用 `enum`（无 class）且数值手工指定，为什么不用 `enum class SiChannel`？

**答案**：因为 Si5351C 驱动的接口签名就是 `SetCLK(uint8_t clknum, ...)`——芯片本来就把通道当作 0–7 的裸编号。用普通 `enum` 可以隐式转成 `uint8_t` 直接传入；若用 `enum class` 则每处调用都要 `static_cast`。这是「封装优雅性」与「驱动接口贴近硬件」之间的务实折中（作为对比，`Si5351C::DriveStrength`、`MAX2871::Power` 这些取值集合就用 `enum class` 强类型）。

**练习 3**：衰减器和低通滤波器为什么挂在 FPGA 而不直接接 MCU GPIO？

**答案**：两个原因。① 扫描期间每个点都可能要切衰减值/滤波器，VNA 扫描由 FPGA 自主推进（见 4.1.3(c)），MCU 逐点介入会拖慢扫描、也需要 halt；把开关挂到 FPGA 后，`FPGA::WriteSweepConfig` 可以把每个点的衰减值随 PLL 寄存器一起预推送（[VNA.cpp:L289-L291](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L289-L291) 的 `attenuator` 参数）。② 引脚预算：MCU 的 GPIO 数量有限，射频开关数量多，由 FPGA 用并行寄存器位统一驱动更省（`Periphery` 枚举里 14 个使能位就是 FPGA 侧的一个状态寄存器）。

## 5. 综合实践

**任务：把激励设到「3 GHz、-10 dBm」——完整时序清单（本讲核心实践）**

规格书要求「在 Hardware.cpp 中找到实现，列出它调用的每个底层驱动函数及寄存器含义，输出为时序清单」。做这个任务时的第一个发现就是一个知识点：**「设激励」的实现入口不在 Hardware.cpp**——`HW` 只提供幅度策略（`GetAmplitudeSettings`）与状态上报（`SetOutputUnlevel`），真正的编排在模式层。信号源模式下最短的完整链路是 `Generator::Setup`：

1. **实践目标**：沿 [Generator.cpp:L11-L65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L11-L65) 逐步跟踪 `Generator::Setup({frequency=3e9, cdbm_level=-1000, activePort=1})`，产出一份「步骤 | 函数（文件:行） | 硬件动作/寄存器含义」的时序清单。
2. **操作步骤与参考答案**（建议先自己填，再对照）：

| # | 函数调用（位置） | 硬件动作 / 寄存器含义 |
|---|---|---|
| 1 | `Si5351.Disable(Port1LO2/Port2LO2/RefLO2)`（Generator.cpp:L13-L15） | 写 Si5351 OutputEnableControl 置位 CLK1/2/4——信号源模式不需要接收链，先关三路二本振省电 |
| 2 | `HW::SetMode(Mode::Generator)`（Generator.cpp:L17 → Hardware.cpp:L217-L240） | 内部 `HW::Init()` 重跑整棵时钟树 + `SetIdle()` 关全部射频器件：从已知态出发 |
| 3 | `Cal::FrequencyCorrectionToDevice(3e9)`（Generator.cpp:L29） | 用设备级频率校准修正 TCXO 误差（详见 u5-l3），得到实际写入芯片的频率 |
| 4 | `HW::GetAmplitudeSettings(-1000, 3e9, …)`（Generator.cpp:L30 → Hardware.cpp:L272-L316） | 纸面计算：高段（≥25 MHz）；-1000 > -1060 → 高档 `p5dbm`；残余 cdbm = -1000-(-160) = -840；`attval = 840/25 = 33`；`unlevel=false`。**无硬件访问** |
| 5 | `Si5351.Disable(LowbandSource)`（Generator.cpp:L42） | I2C 关 CLK0：低段激励源退场 |
| 6 | `FPGA::Enable(SourceChip)` + `FPGA::SetMode(SourcePLL)`（Generator.cpp:L43-L44） | 写 FPGA 外设寄存器开源 PLL 供电；AUX1=高/CS=低 + SPI 降速 10.625 MHz——SPI 此后直达 Source |
| 7 | `Source.SetPowerOutA(p5dbm)`（Generator.cpp:L45 → max2871.cpp:L98-L105） | 改 R4 bit3-5 = 0b11（+5 dBm 档）、bit5 置输出使能，只改 RAM |
| 8 | `Source.SetFrequency(3e9)`（Generator.cpp:L46 → max2871.cpp:L135-L231） | 频率规划（4.2.4 已手算）：R0←N=30,F=0；R1←M=2；R4←div=0；R3←查 VCO 表，只改 RAM |
| 9 | `Source.Update()`（Generator.cpp:L47 → max2871.cpp:L302-L309） | 按 R5→R0 六次 `Write()`：每次 4 字节 SPI + LE 脉冲（LE 即 FPGA CS 线）——**唯一的总线提交点** |
| 10 | 低通滤波选择（Generator.cpp:L49-L57） | 3 GHz 落在 [1.8, 3.5) GHz → `lp = M3500`：纯软件分支，值随后写入 FPGA |
| 11 | `FPGA::OverwriteHardware(33, M3500, lowband=false, port1=true, port2=false)`（Generator.cpp:L60） | 写 FPGA 硬件覆盖寄存器：衰减 33×0.25=8.25 dB（bit8-14）、3.5 GHz 低通（bit6-7）、高段路径、端口 1 开 |
| 12 | `FPGA::SetMode(FPGA)`（Generator.cpp:L48） | SPI 切回 FPGA 目标、恢复 42.5 MHz |
| 13 | `HW::SetOutputUnlevel(false)`（Generator.cpp:L61 → Hardware.cpp:L336-L338） | 清 UNL 标志，等下次 `getDeviceStatus` 随 DeviceStatus 包上报 PC |
| 14 | `FPGA::Enable(Amplifier/SourceRF/PortSwitch)`（Generator.cpp:L62-L64） | 打开激励放大器、源射频路径、端口开关——信号到达端口 1 |

3. **需要观察的现象**：核对功率预算：端口近似输出 ≈ 高档端口功率(-1.6 dBm) − 衰减(8.25 dB) ≈ -9.85 dBm ≈ 目标 -10 dBm（未计入 Cal 修正；计入后第 4 步的 cdbm 会先加上校准量再走同一算法）。
4. **预期结果**：一份 14 行时序清单 + 一段「RAM 影子何时变总线写」的结论：只有第 6/9/11/14 类步骤真正占用 I2C/SPI/FPGA 总线，其余全是计算。这正是 LibreVNA 硬件控制代码的核心节奏——**先算后写、批量提交、开关放最后**（射频器件最后上电，避免中间态把杂散辐射出去）。
5. 若有硬件与 STM32CubeIDE 环境，可在第 9 步前于 `MAX2871::Write` 加 `LOG_DEBUG` 打印 `reg/val` 验证清单（日志模块 "MAX2871"，需把 [max2871.cpp:L7](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/max2871.cpp#L7) 的 `LOG_LEVEL` 调到 DEBUG 后重新编译）；无硬件时本实践为纯走读，标注「待本地验证」的部分仅指波形观察，逻辑链本身已由源码核实。

## 6. 本讲小结

- **`HW` 是策略层而非纯门面**：初始化时序（时钟树→FPGA→PLL）、幅度拆分（`GetAmplitudeSettings`：波段→档位→0.25 dB 衰减步进）、参考源切换与状态聚合（`unlevel`、温度、ADC 过载）都在这里；但「何时设什么频率」由模式层（Generator/VNA/SA）编排，它们直接调用驱动对象。
- **两颗合成器、两种总线、一个公共范式**：Si5351C（I2C）管时钟树+低段激励+三路同相二本振；MAX2871（SPI，×2）管高段激励与一本振。驱动都用「影子寄存器 + 显式提交」（`SetFrequency` 只算不写，`Update()` 才按 datasheet 顺序写总线）。
- **频率规划就是解方程**：Si5351 是 \( f_{PLL}/(a+b/c) \) 的有理逼近（P1/P2/P3 打包）；MAX2871 是 \( (N+F/M)f_{PFD}/2^{div} \) 加 VCO 手动选择——`BuildVCOMap` 用启动期自学习换扫描期快锁定。
- **HW_HAL 是 7 行的板级真相**：总线句柄、LE/MUX 引脚复用、`SiChannel` 通道映射全部集中于此；一条 SPI1 被 FPGA/两颗 PLL/Flash 分时复用，靠 `FPGA::SetMode` 的 AUX1/AUX2/CS 摆位加波特率切换完成路由。
- **射频开关与衰减器归 FPGA 管**：MCU 写 `FPGA::OverwriteHardware`/`Periphery` 位，扫描时每点的衰减值随 PLL 寄存器一起预推送，FPGA 自主推进、无需 MCU 逐点介入。
- **安全的编排节奏**：先算后写、影子寄存器批量提交、模式切换宁可全量重初始化（`SetMode`→`Init`）、射频器件最后上电（`SetIdle` 全关起步）。

## 7. 下一步学习建议

本讲结束后，你已经掌握「一条硬件指令如何变成寄存器写」。接下来两条路：

1. **u5-l3（设备级校准与 Flash 存储）**：本讲多次出现的 `Cal::SourceCorrection`、`Cal::FrequencyCorrectionToDevice` 到底存了什么、怎么写进 flash——这决定了 `GetAmplitudeSettings` 第 1 步修正的来源。
2. **u5-l4（设备端三大模式）**：把本讲的「单点设置」升级为「整条扫描的硬件编排」，重点看 [VNA.cpp:L224-L293](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L224-L293) 的逐点循环如何配合 `FPGA::WriteSweepConfig` 把影子寄存器变成 FPGA 侧的扫描表，以及低段点为何必须 halt（Si5351 走 I2C、只能由 MCU 逐点配置）。
3. 若对 FPGA 侧「谁在驱动 MAX2871」感兴趣，可预习 u6-l5：扫描期间 FPGA 用本讲推送的寄存器表直接产生 PLL 写时序（`FPGA/VNA/MAX2871.vhd`）。

建议顺带精读的源码：`Drivers/Si5351C.hpp`（寄存器地址枚举 `Reg` 与 AN619 的对应）、`Drivers/FPGA/FPGA.hpp`（`Periphery`/`Interrupt`/`Samples` 枚举——下一讲的 vocabulary）。
