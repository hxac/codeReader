# CFAR 目标检测

## 1. 本讲目标

经过 u4-l4，我们已经把每个距离门上的多个 chirp 做了慢时间 FFT，得到一张 **Range-Doppler 图**（64 距离门 × 32 Doppler bin）。这张图里既有真实目标，也有噪声和各种杂波。本讲要解决的最后一个 DSP 问题是：**在这张图上自动、稳定地判出“哪些点是目标”**。

读完本讲后，你应该能够：

1. 说清楚 **CFAR（恒虚警率，Constant False Alarm Rate）** 相比“固定门限”解决了什么问题，为什么雷达必须用自适应门限。
2. 理解 `cfar_ca` 模块的两阶段状态机：先缓存整帧幅度，再逐 Doppler 列滑动窗口计算局部噪声。
3. 区分 **CA / GO / SO** 三种单元平均 CFAR 模式各自的适用场景与代码实现差异。
4. 解释门限公式 \( T = (\alpha \cdot \text{noise\_sum}) \gg 4 \) 里 `alpha`、Q4.4 定点、训练单元数的含义。
5. 读懂 `enable=0` 时 CFAR 如何回退为简单门限，保证对旧版本上位机的向后兼容。
6. 读懂顶层 `radar_system_top.v` 里 **DC notch** 如何在 CFAR 之前清零零多普勒杂波，以及它与 MTI 的互补关系。

## 2. 前置知识

在进入源码前，先用三段直觉把概念建立起来。

**为什么不能直接用一个固定门限？** 雷达接收机底噪会随温度、电磁环境、距离段变化；地表杂波（地物、海浪、雨雪）在不同方向强度天差地别。如果门限写死，环境一变要么漏检（门限太高）、要么虚警爆炸（门限太低）。CFAR 的核心思想是：**门限跟着本地噪声走**——在待判单元（CUT，Cell Under Test）周围取一圈“训练单元”估算当前噪声电平，再乘一个系数当门限。这样无论噪声起伏，虚警概率都能近似恒定。

**滑动窗口的结构。** 围绕 CUT，CFAR 取的是：

- **保护单元（guard cells）**：紧贴 CUT 两侧的若干单元，**不参与**噪声估计。因为真实目标会“漏”到相邻 bin（能量扩散），如果把它当噪声会抬高门限、漏检自己。
- **训练单元（training cells）**：保护单元再往外的一圈，**参与**噪声估计。工程上常分“前导窗（leading）”和“滞后窗（lagging）”两段，分别估算 CUT 两侧的噪声。

经典 CA-CFAR（Cell-Averaging）的门限为：

\[
T_{\text{CUT}} = \alpha \cdot \frac{1}{2N}\sum_{i \in \text{train}} |X_i|
\]

其中 \(N\) 是单侧训练单元数。CUT 的幅度大于 \(T_{\text{CUT}}\) 即判为目标。

**为什么还要 DC notch？** 静止或慢速物体（地面、建筑）的回波几乎全部落在零多普勒（DC）bin。虽然 u4-l3 讲的 MTI 在时域已经差分滤掉了大部分静止杂波，但 MTI 是 **pre-Doppler**（在 FFT 之前），残留的直流泄漏和强地杂波在 FFT 之后仍会高度集中在 DC bin 附近，形成巨大尖峰。这个尖峰会污染 CFAR 的噪声估计、制造大量虚警。所以本系统在 **FFT 之后、CFAR 之前** 加了一道 DC notch，把 DC 附近的 bin 直接置零。MTI 与 DC notch 是互补关系，不是重复。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `9_Firmware/9_2_FPGA/cfar_ca.v` | CFAR 检测器主体：幅度缓存、滑动窗口、CA/GO/SO 模式选择、门限计算、检测输出。本讲的绝对核心。 |
| `9_Firmware/9_2_FPGA/radar_system_top.v` | 顶层：例化 `cfar_inst`、实现 DC notch 组合逻辑、声明并译码所有 `host_cfar_*` / `host_dc_notch_width` 配置寄存器。 |
| `9_Firmware/9_2_FPGA/tb/tb_cfar_ca.v` | CFAR 专项测试台，14 项测试，输出 `[PASS]/[FAIL]`，是理解期望行为的最佳参考。 |
| `9_Firmware/9_3_GUI/radar_protocol.py` | Python 侧 `Opcode` 枚举（`CFAR_GUARD=0x21` … `DC_NOTCH_WIDTH=0x27`），与 Verilog case 表构成跨层契约（详见 u6-l2）。 |

## 4. 核心概念与源码讲解

### 4.1 CFAR 检测原理与两阶段状态机

#### 4.1.1 概念说明

`cfar_ca` 模块的核心设计是一个 **两阶段流水**：

- **Phase 1（BUFFER）**：Doppler 处理器在 `doppler_valid` 拉高时逐个吐出 Range-Doppler 样本。CFAR 一边把每个样本的幅度 \(|I|+|Q|\) 按地址 `{range_bin, doppler_bin}` 写进 BRAM，一边（在 CFAR 关闭时）做简单门限直通。
- **Phase 2（CFAR）**：当 Doppler 处理器发出 `frame_complete`（一整张 Range-Doppler 图就绪）后，CFAR 才开始逐 Doppler 列地做滑动窗口自适应检测。

为什么要分两阶段？因为滑动窗口需要 CUT 两侧的数据，**必须等整列（一个 Doppler bin 下全部 64 个距离门）都到齐才能算**。Phase 1 只管“收齐并缓存”，Phase 2 才管“算门限、判目标”。

#### 4.1.2 核心流程

模块整体 FSM 如下（状态编码见源码注释）：

```
ST_IDLE            复位后等待第一个 doppler_valid
   │  捕获本帧配置 (guard/train/alpha/mode/enable)，写第一个样本
   ▼
ST_BUFFER          边收边存，写满 2048 单元；若 enable=0，逐样本简单门限直通
   │  收到 frame_complete：
   │    enable=1 → 进入 Phase 2
   │    enable=0 → 跳到 ST_DONE（本帧只用简单门限）
   ▼
ST_COL_LOAD        把当前 Doppler 列的 64 个幅度从 BRAM 读进列缓冲 col_buf
   ▼
ST_CFAR_INIT       为 CUT=0 算初始滞后窗和
   ▼
ST_CFAR_THR → ST_CFAR_MUL → ST_CFAR_CMP   每个 CUT 三拍：锁存噪声→乘 alpha→比较+滑窗
   │  cut_idx 走完 64 个距离门
   ▼
ST_COL_NEXT        切到下一个 Doppler 列（共 32 列）
   ▼
ST_DONE            回到 ST_IDLE，等下一帧
```

关键设计点：

- **每帧开始时一次性锁存配置**（`r_guard`、`r_train` 等），保证一帧处理期间参数稳定，不会中途被主机改值撕裂。
- **CFAR 关闭时（`enable=0`）零成本回退为简单门限**：Phase 1 在 `ST_BUFFER` 里直接对每个进来的样本和 `r_simple_thr` 比较，并直接跳过整个 Phase 2。这是为了与“没有 CFAR 的旧固件/上位机”保持二进制兼容。

#### 4.1.3 源码精读

模块端口与参数。注意 `cfg_*` 输入就是顶层 `host_cfar_*` 寄存器直连过来的：

[cfar_ca.v:L62-L100](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L62-L100) —— 声明参数（`NUM_RANGE_BINS=64`、`NUM_DOPPLER_BINS=32`、`MAG_WIDTH=17`）与全部端口；其中 `cfg_guard_cells` / `cfg_train_cells` / `cfg_alpha` / `cfg_cfar_mode` / `cfg_cfar_enable` 五个就是主机可配置的 CFAR 参数，`frame_complete` 是 Doppler 处理器送来的“整帧就绪”信号。

FSM 状态定义与 `cfar_busy`：

[cfar_ca.v:L116-L127](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L116-L127) —— 9 个状态，注意 `ST_CFAR_THR/MUL/CMP` 三个状态编号不连续（4/8/5），这是为方便阅读而刻意留的；`cfar_busy = (state != ST_IDLE)` 把“是否在处理”暴露给状态回读。

幅度计算（\(|I|+|Q|\)，L1 范数近似）：

[cfar_ca.v:L132-L136](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L132-L136) —— 用补码取负把有符号 16 位 I/Q 转成绝对值，再相加得到 17 位幅度。比起 \(\sqrt{I^2+Q^2}\)，\(L_1\) 范数无需乘法/开方，资源极省，是 FPGA 幅度检测的常规手法。

`ST_BUFFER`：边收边存，并在 CFAR 关闭时做简单门限直通：

[cfar_ca.v:L343-L373](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L343-L373) —— 重点看第 363–372 行的 `frame_complete` 分支：`r_enable` 为真才进 `ST_COL_LOAD`（启动 Phase 2），否则直接 `ST_DONE`。这就是“兼容回退”的实现位置。

`ST_DONE` 收尾：

[cfar_ca.v:L526-L533](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L526-L533) —— 回到 `ST_IDLE` 等下一帧；仿真模式下用 `$display` 打印本帧总检测数。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认两阶段边界与配置锁存时机。

**操作步骤**：

1. 打开 `cfar_ca.v`，定位 `ST_IDLE`（第 307 行起）。
2. 找到“捕获配置”的 5 行赋值（`r_guard <= cfg_guard_cells` 等），确认它们只在 `doppler_valid` 首次到来时执行一次。
3. 定位 `ST_BUFFER` 里的 `frame_complete` 分支，确认 `enable=0` 时跳过 Phase 2。

**需要观察的现象**：配置寄存器 `r_*` 在一帧内**不会**再被 `cfg_*` 立即覆盖；只有进入下一帧 `ST_IDLE`/`ST_BUFFER` 时才可能更新。

**预期结果**：你能用一句话说出“主机在帧中间改 CFAR 参数，最早在下一帧才生效”。

#### 4.1.5 小练习与答案

**练习 1**：如果主机在 Phase 2 正在跑的时候修改了 `host_cfar_alpha`，本帧检测会不会立刻用新值？

> **答**：不会。Phase 2 用的是帧起始锁存的 `r_alpha`，新值要等下一帧 `ST_IDLE` 才被锁进 `r_alpha`。

**练习 2**：`cfar_busy` 在哪些状态下为高？它对状态回读有什么用？

> **答**：除 `ST_IDLE` 外所有状态都为高。上位机可通过状态包判断“当前是否正在处理一帧”，避免在处理中改参数（详见 u10-l1 自测试与状态回读）。

---

### 4.2 CA / GO / SO 三种检测模式

#### 4.2.1 概念说明

CA-CFAR 用 CUT 两侧**全部**训练单元估噪声，门限最稳，但在“多个目标相邻”或“杂波边缘”（一边是空旷一边是地物）时表现差。于是衍生出两种变体：

- **GO-CFAR（Greatest-Of）**：取前导窗、滞后窗中**平均更大**的一侧。适用于杂波边缘——能压低噪声低的一侧被误判的可能，**抗虚警强**，但两个目标相邻时容易互相抬门限、漏检。
- **SO-CFAR（Smallest-Of）**：取**平均更小**的一侧。适用于多目标邻接场景——避免邻近目标抬高门限，**抗漏检强**，但噪声估计不稳、虚警略高。

`cfar_ca` 用 `cfg_cfar_mode`（2 位）在三者间切换，`2'b11` 保留并回退到 CA。

#### 4.2.2 核心流程

三种模式都建立在“前导窗和 `leading_sum` / 滞后窗和 `lagging_sum`”之上。区别只在于最终 `noise_sum_comb` 怎么由两侧汇总：

```
CA : noise = leading_sum + lagging_sum              # 两窗相加
GO : noise = max(leading_avg, lagging_avg)           # 取平均更大的一侧的“和”
SO : noise = min(leading_avg, lagging_avg)           # 取平均更小的一侧的“和”
```

**为什么 GO/SO 要比“平均”而不是直接比“和”？** 因为 CUT 在列首/列尾时，一侧的训练单元可能不完整（单元数少），直接比和会偏向单元多的一侧。代码用交叉相乘 `leading_sum * lagging_count` vs `lagging_sum * leading_count` 来比平均，避免除法：

\[
\text{leading\_avg} > \text{lagging\_avg}
\;\Longleftrightarrow\;
\frac{\text{leading\_sum}}{\text{leading\_count}} > \frac{\text{lagging\_sum}}{\text{lagging\_count}}
\;\Longleftrightarrow\;
\text{leading\_sum} \cdot \text{lagging\_count} > \text{lagging\_sum} \cdot \text{leading\_count}
\]

#### 4.2.3 源码精读

模式选择核心——`noise_sum_comb` 组合逻辑：

[cfar_ca.v:L224-L260](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L224-L260) —— `case(r_mode)`：
- `2'b00, 2'b11`（CA）：`leading_sum + lagging_sum`；
- `2'b01`（GO）：交叉相乘比平均，取更大一侧的和；
- `2'b10`（SO）：交叉相乘比平均，取更小一侧的和；
- 边界保护：当某侧 `count==0`（CUT 在列首/尾）时退化成只用另一侧。

滑动窗口的增量更新（避免每个 CUT 都从头求和）：

[cfar_ca.v:L188-L222](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L188-L222) —— 当 CUT 从 \(k\) 推进到 \(k+1\) 时，计算出“进入窗的新单元”和“掉出窗的旧单元”的下标与合法性，得到 `lead_delta` / `lag_delta` 两个净增量；更新时只需 `leading_sum <= leading_sum + lead_delta`，O(1) 而非 O(N)。这是 CFAR 能在 100 MHz 实时跑完的关键。

初始窗口和（CUT=0 时前导窗为空，只算滞后窗）：

[cfar_ca.v:L416-L429](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L416-L429) —— `ST_CFAR_INIT` 每拍累加一个训练单元到 `lagging_sum`，为后续滑窗提供起点。

> 备注：GO/SO 模式下 `noise_sum` 只含一侧训练单元（数量为 \(N\)），而 CA 模式含两侧（\(2N\)）。这意味着同一个 `alpha` 在两种模式下的统计含义不同。源码注释建议主机“按训练单元数预先补偿 alpha”（见 [cfar_ca.v:L35-L47](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L35-L47)）。

#### 4.2.4 代码实践

**实践目标**：通过 `tb_cfar_ca.v` 的 T6/T7 测试用例理解 GO 与 SO 的行为差异。

**操作步骤**：

1. 打开 `tb/tb_cfar_ca.v`，找到 T6（GO-CFAR 不对称噪声）与 T7（SO-CFAR 更敏感）两段。
2. 阅读它们各自构造的输入幅度分布与断言（期望的检测数 / 检测位置）。
3. 若本地装了 iverilog，可尝试编译该测试台（具体命令与 `run_regression.sh` 的调用方式见 u11-l1，精确标志位待本地验证）。

**需要观察的现象**：SO 模式因取更小的一侧、门限更低，在同样输入下检测数应不少于 CA；GO 模式在杂波边缘更“保守”。

**预期结果**：你能口述“多目标邻接选 SO、杂波边缘选 GO、一般场景选 CA”这条经验法则。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GO/SO 用交叉相乘而不是直接相除比平均？

> **答**：FPGA 里除法代价高且难以在单拍内完成；交叉相乘把“比平均”转成一次乘法比较，资源省、可单拍完成。

**练习 2**：CUT 位于距离门 0（列首）时，前导窗的 `leading_count` 是多少？此时 CA 模式的 `noise_sum` 退化成什么？

> **答**：`leading_count=0`（前面没有训练单元）。CA 退化成 `noise_sum = lagging_sum`，噪声估计样本变少、门限偏低，边缘虚警率会上升——源码注释把这视为可接受（边缘 bin 通常是杂波）。

---

### 4.3 自适应门限计算（alpha 与 Q4.4 定点）

#### 4.3.1 概念说明

得到 `noise_sum` 后，门限就是乘以系数 `alpha`。工程上的关键在于定点表示：

- `alpha` 用 **Q4.4 定点**（4 位整数 + 4 位小数，共 8 位）。例如 `0x30 = 48`，对应十进制 \(48/16 = 3.0\)。
- 乘法在 DSP48 里做：`noise_product = r_alpha * noise_sum_reg`（31 位结果）。
- 再右移 `ALPHA_FRAC_BITS = 4` 把小数对齐回去：\[ T = (\text{noise\_product}) \gg 4 \]
- 若结果超过 17 位幅度位宽则饱和到全 1，防止溢出回绕。

#### 4.3.2 核心流程

为把关键路径切短，门限计算被拆成 **三拍流水**（对应 `ST_CFAR_THR → ST_CFAR_MUL → ST_CFAR_CMP`）：

```
ST_CFAR_THR : noise_sum_reg <= noise_sum_comb      # 锁存噪声（断开 cross-multiply→DSP 长路径）
ST_CFAR_MUL : noise_product <= r_alpha * noise_sum_reg   # DSP 乘法
ST_CFAR_CMP : T = (noise_product >> 4) 饱和到 17 位
              比较 col_buf[cut_idx] > T → detect_flag
              更新滑窗，cut_idx++，回到 ST_CFAR_THR
```

三拍流水是经典面积/速度权衡：每个 CUT 花费 3 个时钟，但每条组合路径都很短，能跑高频。

alpha 的统计含义。源码注释给了一个换算例子：希望虚警概率 \(P_{\text{fa}}=10^{-4}\)、单侧训练 \(T=8\)（共 16 单元）时，经典统计系数 \(\alpha_{\text{stat}}\approx 4.88\)。由于本设计 `noise_sum` 是**总和**而非平均，主机需预先除以单元数：

\[
\alpha_{\text{fpga}} = \frac{\alpha_{\text{stat}}}{2N}
\quad\Rightarrow\quad
0.305 \;\to\; \text{Q4.4}\approx 0\text{x}05
\]

固件默认 `alpha=0x30`（3.0）偏保守，且 CFAR 默认关闭（见 4.4），实际部署应由上位机按场景重设。

#### 4.3.3 源码精读

关键位宽与 Q4.4：

[cfar_ca.v:L109-L111](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L109-L111) —— `SUM_WIDTH=23`（最多 64 个 17 位幅度相加）、`PROD_WIDTH=31`、`ALPHA_FRAC_BITS=4`。

DSP 乘法拍：

[cfar_ca.v:L452-L457](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L452-L457) —— `noise_product <= r_alpha * noise_sum_reg;`，干净的“寄存器输入 → DSP”路径。

比较与饱和拍：

[cfar_ca.v:L462-L507](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cfar_ca.v#L462-L507) —— 第 467–470 行做饱和判定（高位非零则置全 1）；第 488 行 `col_buf[cut_idx] > thr_val` 决定 `detect_flag` 并累加 `detect_count`；第 495–503 行在 `cut_idx < 63` 时应用 4.2 节的滑窗增量并回到 `ST_CFAR_THR`。

#### 4.3.4 代码实践

**实践目标**：手算一个门限，验证你对 Q4.4 的理解。

**操作步骤**：

1. 设 `r_alpha = 0x05`（即 0.3125），某 CUT 的 `noise_sum_reg = 1600`（十进制）。
2. 计算 `noise_product = 5 × 1600 = 8000`，再 `>> 4` 得 `T = 500`。
3. 若该 CUT 幅度为 700，则 700 > 500，判为检测；若为 400，则不判。

**需要观察的现象**：把 `alpha` 从 `0x05` 调到 `0x30`（3.0），同一 `noise_sum` 下门限放大约 9.6 倍，检测数应大幅下降。

**预期结果**：你能解释“调大 alpha → 更保守（少虚警多漏检），调小 alpha → 更敏感（多虚警少漏检）”。

#### 4.3.5 小练习与答案

**练习 1**：`0x30` 在 Q4.4 下等于多少？为什么用它做默认值是“保守”的？

> **答**：\(0x30 = 48\)，\(48/16 = 3.0\)。对 CA 模式（默认训练 16 单元）它对应的统计系数约 \(3.0\times 16 = 48\)，远高于 \(P_{\text{fa}}=10^{-4}\) 所需的 4.88，门限偏高、虚警极少但漏检偏多，故称保守；且默认 CFAR 关闭，不影响默认行为。

**练习 2**：为什么 `ST_CFAR_CMP` 里要判饱和？不判会怎样？

> **答**：`noise_product` 是 31 位，截到 17 位幅度时若高位非零说明溢出；不饱和会让回绕后的门限变成一个小值，导致本该“过门限即检测”的强噪声段反而漏判。饱和到全 1 保证“噪声极大时门限也极大”。

---

### 4.4 DC notch：在 CFAR 前清除零多普勒杂波

#### 4.4.1 概念说明

DC notch 是顶层的组合逻辑（不在 `cfar_ca.v` 内部），位于 **Doppler FFT 之后、CFAR 之前**。它的职责：当主机开启（`host_dc_notch_width != 0`）时，把零多普勒（DC）附近若干 bin 的 I/Q 直接置零，再喂给 `cfar_inst`。

为什么要在两个 16 点子帧里**都**清零？回忆 u4-l4：一帧 32 chirp 拆成 **双 16 点子帧**（long/short 交替的 staggered PRI），每个子帧各有自己的 DC bin。`doppler_bin[4:0]` 的打包格式是 `{sub_frame, bin[3:0]}`：

- 子帧 0：bin 0–15，DC 在 bin 0；
- 子帧 1：bin 16–31，DC 在 bin 16。

所以一次“清 DC”必须同时命中 bin 0 和 bin 16，否则两个子帧之一会残留强地杂波。

#### 4.4.2 核心流程

DC notch 用一个简单条件决定当前样本是否落在 notch 区：

```
bin_within_sf = doppler_bin[3:0]            # 当前 bin 在自己子帧内的位置 (0..15)
dc_notch_active = (width != 0) 且
                 (bin_within_sf < width  或
                  bin_within_sf > (15 - width + 1))   # 靠近 DC 或靠近子帧末端(wrap)

notched_doppler_data = dc_notch_active ? 0 : rx_doppler_output
```

子帧末端为什么也要清？因为 16 点 FFT 把负频率折叠在 bin 15（子帧 0）/bin 31（子帧 1）——它们在频谱上紧贴 DC 的另一侧，属于“DC 附近的 wrap 区”。于是按宽度展开：

| `host_dc_notch_width` | 清零的 bin（两个子帧并集） |
| --- | --- |
| 0（关） | 无（直通） |
| 1 | {0, 16} |
| 2 | {0, 1, 15, 16, 17, 31} |
| … | 子帧 0 清 `[0,width-1]` 与 `[16-width,15]`，子帧 1 同理平移 16 |

#### 4.4.3 源码精读

顶层 DC notch 实现（纯组合）：

[radar_system_top.v:L578-L601](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L578-L601) —— 注释详细说明了打包格式与各宽度下被清零的 bin 集合；第 590–595 行算 `dc_notch_active`；第 598–601 行把命中的样本 I/Q 置零后改名成 `notched_doppler_data/_valid/_bin/_range_bin` 一组信号。

CFAR 吃的是 notch 之后的信号：

[radar_system_top.v:L620-L651](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L620-L651) —— `cfar_inst` 的 `.doppler_data(notched_doppler_data)`、`.frame_complete(rx_frame_complete)`，证明 CFAR 在 notch 之后；同时把五个 `host_cfar_*` 配置寄存器接进 `cfg_*`。

DC notch 的配置寄存器声明与默认值：

[radar_system_top.v:L269-L271](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L269-L271) —— `host_mti_enable` 与 `host_dc_notch_width[2:0]`。

复位默认值（开机即“全关”，向后兼容）：

[radar_system_top.v:L934-L936](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L934-L936) —— `host_mti_enable<=0`、`host_dc_notch_width<=0`。

opcode 译码（0x26 开 MTI、0x27 设 notch 宽度）：

[radar_system_top.v:L984-L986](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L984-L986) —— 与 Python 侧 `MTI_ENABLE=0x26`、`DC_NOTCH_WIDTH=0x27` 一一对应（见 [radar_protocol.py:L85-L91](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L85-L91)），这是跨层硬契约。

#### 4.4.4 代码实践

**实践目标**：在顶层验证 DC notch 的清零集合，并算出“开 width=2”会牺牲多少有效 bin。

**操作步骤**：

1. 在 `radar_system_top.v` 第 590–595 行附近，逐字读懂 `bin_within_sf < width` 与 `bin_within_sf > 15 - width + 1` 两个条件。
2. 代入 `width=2`：列出每个子帧被清零的下标（子帧 0：0,1,15；子帧 1：16,17,31）。
3. 全图共 64×32=2048 个 Range-Doppler 单元，问：width=2 时每个 Doppler 列有几个 bin 被永久清零？这对探测慢速目标意味着什么？

**需要观察的现象**：宽度越大，DC 附近被清的 bin 越多，零多普勒杂波抑制越彻底，但低速目标（行人、慢速无人机）也会被一并清掉。

**预期结果**：你能得出“width=2 时每列清 6 个 Doppler bin（占 32 的约 19%）”，并据此向用户解释“不要在需要低速探测的场景把 notch 开太大”。

**若无法本地验证**：精确的 wrap 边界行为（尤其 `15 - width + 1` 在 `width=7` 时是否如预期）建议在 `tb_fullchain_mti_cfar_realdata.v` 中用真实数据核对，结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`host_dc_notch_width` 是几位？最大能开到几？开到最大时每列清几个 bin？

> **答**：3 位，最大 7。每列被清 `[0,6]`（7 个）加 `[15-7+1=9..15]`（7 个）共 14 个 bin，接近半个 Doppler 列——几乎一定会误伤低速目标，故大宽度只在强地杂波场景使用。

**练习 2**：DC notch 与 MTI（u4-l3）都去杂波，二者是否重复？

> **答**：不重复。MTI 是 pre-Doppler 的时域二脉冲对消，滤的是“完全静止”的强分量；DC notch 是 post-Doppler 的频域置零，清的是 FFT 后残留在 DC 附近的泄漏与慢杂波。前者先大幅衰减，后者再补刀残余，互补而非冗余。

## 5. 综合实践

把本讲三个最小模块（CA/GO/SO 模式、门限计算、DC notch）串成一个端到端配置任务。

**任务**：你拿到了一块雷达板，需要在不同场景下配置 CFAR。请完成下表，并解释每条选择的依据：

| 场景 | guard | train | alpha(十六进制) | mode | enable | dc_notch_width | 理由 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 开阔海面、稀疏目标 | ? | ? | ? | CA | ? | ? | ? |
| 城市边缘（强地杂波） | ? | ? | ? | GO | ? | ? | ? |
| 多架无人机编队（目标密集） | ? | ? | ? | SO | ? | ? | ? |

**参考答题思路**（数值需结合实际雷达标定，以下为方向性建议，待本地验证）：

1. **开阔海面**：杂波均匀 → `mode=CA`；中等 `train=8`、`guard=2`；`alpha` 按目标 \(P_{\text{fa}}\) 调到约 `0x05`–`0x08`；`enable=1`；海面慢杂波弱时 `dc_notch_width=1` 即可。
2. **城市边缘**：杂波边缘陡 → `mode=GO` 抑虚警；`dc_notch_width` 开到 2–3 清强地物；`alpha` 略大。
3. **无人机编队**：目标邻接 → `mode=SO` 防互相抬门限；`guard` 适当加大避免目标能量扩散互扰；`dc_notch_width` 不宜过大（无人机有慢速运动，会落入低速 bin）。

**进阶（源码阅读型）**：对照 `tb/tb_cfar_ca.v` 的 T1–T14 测试计划，逐条写出它对应本讲哪个机制（如 T1 验证兼容回退、T6 验证 GO、T9 验证 guard 作用、T13 验证 `detect_count` 跨帧累加）。这能帮你把“概念—源码—测试断言”三者对齐。

## 6. 本讲小结

- CFAR 用 **CUT 周围的训练单元**估算本地噪声，让门限随噪声自适应，从而在变化环境中保持近似恒定的虚警率——这是固定门限做不到的。
- `cfar_ca` 是 **两阶段 FSM**：Phase 1 缓存整帧幅度到 BRAM，Phase 2 在 `frame_complete` 后逐 Doppler 列做滑窗检测；配置在帧起始一次性锁存。
- **CA** 取两侧之和（通用）、**GO** 取平均更大侧（抗杂波边缘虚警）、**SO** 取平均更小侧（抗多目标漏检）；GO/SO 用交叉相乘比平均，避开除法。
- 门限 \( T = (\alpha \cdot \text{noise\_sum}) \gg 4 \)，`alpha` 为 Q4.4 定点（默认 `0x30`=3.0，偏保守），经 THR→MUL→CMP 三拍流水计算。
- **`enable=0` 自动回退为简单门限**，Phase 1 直通、跳过 Phase 2，保证对旧上位机的二进制兼容。
- **DC notch** 在 FFT 之后、CFAR 之前，按 `host_dc_notch_width` 同时清零两个 16 点子帧的 DC/wrap 区 bin；与 pre-Doppler 的 MTI 互补。

## 7. 下一步学习建议

本讲是 **FPGA 接收信号处理链** 的最后一讲（DDC → 匹配滤波 → 距离抽取/MTI → Doppler → DC notch → CFAR），至此你已经走完了从 ADC 原始采样到“目标检测点”的整条数字链路。接下来建议：

1. **横向对照发射链**：读 u5-l1（PLFM chirp 与发射机），理解发射的长/短 chirp 是如何与本讲 Doppler 的双 16 点子帧（staggered PRI）严格对偶的——收发链路是对称镜像。
2. **看检测结果如何被搬出 FPGA**：读 u6-l1（USB 数据接口），关注 11 字节数据包里 `detection` 字节（bit0）正是本讲 `detect_flag` 的去向，bit7 的 `frame_start` 与本讲帧概念对应。
3. **理解参数如何被上位机设置**：读 u6-l2（主机命令协议），把本讲的 opcode `0x21`–`0x27` 与 Python `Opcode` 枚举、`build_command` 串起来。
4. **验证体系**：读 u11-l1（FPGA 回归与 cosim），看 `tb_cfar_ca.v` 与 `tb_fullchain_mti_cfar_realdata.v` 如何用真实数据做 exact-match 黄金比对，确保 CFAR 行为不被回归破坏。
