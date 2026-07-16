# FIR 滤波器、系数存储与滑动平均

## 1. 本讲目标

本讲是 `fix` 区域「DSP 滤波器」专题的收尾。学完后你应当能够：

- 看懂 Open Logic FIR 实体的**命名约定**，并能根据名字反推它的「采样率变换方式 / 抽头计算方式 / 通道组织方式」。
- 理解**串行抽头计算（serial taps）**的含义：一个乘法器每拍算一个抽头，N 个抽头要 N 拍才能算出一个输出样本，以及它与 FIR 全并行实现的资源/吞吐取舍。
- 区分 `olo_fix_fir_dec_ser_chpar`（通道并行）与 `olo_fix_fir_dec_ser_chtdm`（通道时分复用 TDM）在**端口、存储与乘法器数量**上的差异。
- 掌握 `olo_fix_coef_storage` 如何用「实数初始化 + ROM/RAM 双口」统一管理滤波器系数，并支持运行期更新。
- 理解 `olo_fix_mov_avg` 滑动平均如何用「加新减旧」把每拍 O(N) 的累加降到 O(1)，并用移位/乘法做增益校准。
- 能跑通仓库自带的 FIR 协仿真，并对比 FIR 低通与 mov_avg 的滤波效果。

## 2. 前置知识

本讲默认你已经学过：

- **u8-l1 / u8-l2 / u8-l3**：定点格式三元组 `(S,I,F)`、`en_cl_fix` 的 `cl_fix_mult_fmt` / `cl_fix_add` / `cl_fix_resize` 等函数，以及 Open Logic 的字符串泛型模式。
- **u8-l4 / u8-l5**：Python 代码生成与「Python 黄金模型 + `.fix` 文件 + HDL 逐拍比对」的位真协仿真流程（`pre_config` 先生成、再仿真）。
- **u9-l1**：`olo_fix_madd`（乘累加）、`olo_fix_mult`、`olo_fix_resize` 的用法，以及「运算→截断→饱和」三段式。
- **u2-l3**：`olo_base_ram_sdp`（简单双端口 RAM）与 RBW/WBR 读写行为。
- **u1-l5 / u3-l3**：两进程法、AXI-S 握手，以及 TDM（时分复用）约定与 `Last` 信号。

补充两个本讲要用到的 DSP 直觉：

1. **FIR（有限脉冲响应）滤波器**。它的输出是输入的一段历史样本与一组系数（抽头）的点积：

\[
y[n] = \sum_{k=0}^{N-1} h[k]\, x[n-k]
\]

其中 \(N\) 是抽头数，\(h[k]\) 是系数，\(x[n-k]\) 是延迟了 \(k\) 拍的输入。FIR 的关键性质是**线性相位**（系数对称时）和**绝对稳定**（无反馈）。

2. **抽取（decimation）**。先做 FIR 低通滤波（防止高频混叠），再每 \(R\) 个样本只保留一个，使采样率降为原来的 \(1/R\)。Open Logic 的 FIR 实体把「滤波 + 丢弃」合在一起：只有相位对齐的那一拍才真正算输出，其余输入只更新延迟线。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd) | 多通道**并行**、串行抽头的抽取 FIR。每通道一个乘法器。 |
| [src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd) | 多通道 **TDM**、串行抽头的抽取 FIR。所有通道共享一个乘法器。 |
| [src/fix/vhdl/olo_fix_coef_storage.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_coef_storage.vhd) | 定点系数存储，ROM 或 RAM，带「数据口 + 配置口」双读口。 |
| [src/fix/vhdl/olo_fix_mov_avg.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mov_avg.vhd) | 滑动平均滤波器，用「加新减旧」+ 增益校准实现。 |
| [src/fix/python/olo_fix/olo_fix_fir_dec.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_fir_dec.py) | FIR 的 Python 位真模型（两实体共用）。 |
| [test/fix/olo_fix_fir_dec_ser_chtdm/cosim.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_fir_dec_ser_chtdm/cosim.py) | 用 `scipy.signal.firwin` 设计低通 FIR、生成协仿真文件、可画图。 |

## 4. 核心概念与源码讲解

### 4.1 FIR 命名约定：三个正交维度

#### 4.1.1 概念说明

Open Logic 的 FIR 实体名字很长，例如 `olo_fix_fir_dec_ser_chpar`，但它其实是把**三个互相正交的设计维度**拼在一起：

```
olo_fix_fir _ <速率变换> _ <抽头计算方式> _ <通道组织>
```

| 维度 | 取值 | 含义 |
| :--- | :--- | :--- |
| 速率变换 | `dec` | 抽取（decimation），输出采样率 = 输入 / Ratio |
| 速率变换 | `int` | 插值（interpolation），输出采样率 = 输入 × Ratio（命名预留） |
| 抽头计算 | `ser` | 串行：一个乘法器每拍算一个抽头，N 拍算完一个输出 |
| 抽头计算 | `par` | 全并行：每个抽头一个乘法器，一拍算完（命名预留） |
| 抽头计算 | `semi` | 半并行：多乘法器分批算（命名预留） |
| 通道组织 | `chtdm` | 通道时分复用：多路样本在同一根线上轮流出现 |
| 通道组织 | `chpar` | 通道并行：多路样本在同一拍拼接成一根宽线 |

> **重要说明（以源码为准）**：截至当前 HEAD（`ecca8af`），仓库里**实际实现**的只有两个 FIR 实体——`olo_fix_fir_dec_ser_chpar` 与 `olo_fix_fir_dec_ser_chtdm`，即「抽取 + 串行抽头」的两种通道组织。`int`（插值）、`par`/`semi`（全并行/半并行）属于命名约定里预留的设计位置，本讲不会假装它们存在。看名字时请始终回到「三个维度」的拆解。

为什么要这样命名？因为这三个维度是**独立**的选择：你可以单独决定要不要变采样率、用多少个乘法器、通道怎么排布。把维度写进名字，工程师一眼就能估出资源与吞吐。

#### 4.1.2 核心流程：串行抽头计算的代价

「串行（ser）」是这两个实体的共同灵魂。一个 N 抽头 FIR 本质上要做 N 次乘加：

\[
y[n] = \underbrace{h[0]x[n] + h[1]x[n-1] + \dots + h[N-1]x[n-N+1]}_{N\ \text{次乘累加}}
\]

全并行实现会摆 N 个乘法器，一拍出结果（贵但快）；**串行实现只用 1 个乘法器**，分 N 拍依次算完这 N 次乘加，把结果累加到一个寄存器里（便宜但慢）。代价是吞吐：每产出一个输出样本要花 N 拍。对于通道并行的 `chpar`，N 个通道在这 N 拍里**同时**算（每通道一个乘法器），所以一组输出耗时 N 拍；对于 TDM 的 `chtdm`，N 个通道**轮流**用同一个乘法器，所以一组输出耗时 \(N \times \text{Channels}\) 拍。

由此得到一个必须记住的**带宽上限**（实体不产生反压，输入快了就出错，需在外部用 `olo_base_rate_limit` 限速）：

\[
f_{\text{in}} \le \frac{f_{\text{clk}} \cdot \text{Ratio}}{\text{Taps}} \quad(\text{chpar}), \qquad
f_{\text{in}} \le \frac{f_{\text{clk}} \cdot \text{Ratio}}{\text{Taps} \times \text{Channels}} \quad(\text{chtdm})
\]

#### 4.1.3 源码精读：命名与公共骨架

两个实体的泛型几乎完全一致，区别只在通道处理。先看 `chpar` 的实体声明头部（端口宽度直接体现「通道并行」）：

[olo_fix_fir_dec_ser_chpar.vhd:80-86](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L80-L86) —— 输入 `In_Data` 宽度是 `width(InFmt_g) * Channels_g`，即所有通道拼成一根宽线（通道 0 在低位）；输出同理。

而 `chtdm` 的端口是窄线 + `Last`：

[olo_fix_fir_dec_ser_chtdm.vhd:79-86](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L79-L86) —— `In_Data` 宽度就是 `width(InFmt_g)`（单路），靠 `In_Last` 标记 TDM 帧边界，输出带 `Out_Last`。

两者共享同一套定点格式推导（以 `chpar` 为例）：

[olo_fix_fir_dec_ser_chpar.vhd:97-109](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L97-L109) —— 关键三行：

- `MultFmt_c = cl_fix_mult_fmt(InFmt_c, CoefFmt_c)`：乘法用全精度，不丢位。
- `AccuFmt_c = (1, OutFmt_c.I + GuardBits_g, MultFmt_c.F)`：累加器在输出格式之上保留 `GuardBits_g` 个整数保护位、多 1 个符号位，给「N 个乘积之和」留增长空间。
- `AccuStage_c = 4 + MultRegs_g`：乘法结果要经过固定 4 级地址/读 RAM 流水线 + `MultRegs_g` 级乘法器寄存，才到达累加拍。

#### 4.1.4 代码实践

1. **实践目标**：不看正文，仅凭实体名反推它的三维度，再去源码验证。
2. **操作步骤**：
   - 打开 `src/fix/vhdl/` 目录，列出所有 `olo_fix_fir_*` 文件。
   - 对每个名字，按 `<速率>_<抽头>_<通道>` 拆分，写出三个维度的取值。
   - 打开实体，看它的 `Channels_g` 约束与端口宽度，验证你的拆解。
3. **观察现象**：当前仓库只有 `dec_ser_chpar` 与 `dec_ser_chtdm` 两个文件；`chpar` 要求 `Channels_g >= 1`，`chtdm` 要求 `Channels_g >= 2`。
4. **预期结果**：你能正确说出「chpar = 通道并行、chtdm = 通道时分复用、ser = 串行抽头、dec = 抽取」。`chtdm` 的 `Channels_g >= 2` 断言见 [olo_fix_fir_dec_ser_chtdm.vhd:188-190](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L188-L190)（单通道请改用 `chpar` 且 `Channels_g=1`）。

#### 4.1.5 小练习与答案

**练习 1**：若要实现「采样率不变、单通道、用尽量少的乘法器」的 FIR，应选哪个实体？如何配置？
**答案**：选 `olo_fix_fir_dec_ser_chpar`，设 `Channels_g=1`、`MaxRatio_g=1`（ratio=1 即不抽取）。`chpar` 允许单通道，而 `chtdm` 强制 `Channels_g >= 2`。

**练习 2**：`AccuFmt_c` 为什么要比 `OutFmt_c` 多 `GuardBits_g` 个整数位？
**答案**：N 个乘积累加可能超出单个输出的范围。保护位让中间和在累加期间不溢出，最后由 `olo_fix_resize` 统一收敛（舍入+饱和）回 `OutFmt`。保护位不够会导致累加器溢出，输出错误。

---

### 4.2 串行抽头 chpar：通道并行 FIR

#### 4.2.1 概念说明

`olo_fix_fir_dec_ser_chpar` 处理「通道并行」场景：所有通道的**同一时刻**样本在同一拍一起到达，拼成一根宽线（通道 0 在最低位）。它给**每个通道分配一个独立的乘法器和累加器**，但这 `Channels` 个乘法器**共用同一套系数、同一条抽头地址**——它们只在「读哪段历史数据」上不同。所以系数存储只有一份，与通道数无关。

「串行」体现在：所有通道在第 0 拍一起算抽头 0，第 1 拍一起算抽头 1，…… 第 N-1 拍一起算抽头 N-1，N 拍后所有通道同时产出一组结果。

#### 4.2.2 核心流程

每收到一组输入样本（所有通道同拍到达）：

1. **写数据 RAM**：把这一拍所有通道拼接的宽字写入延迟线 RAM，写地址 `TapWrAddr` 自增。
2. **抽取相位计数**：`DecCnt` 每个「有效输入」递减；减到 0 时这一拍触发一次计算，并把 `DecCnt` 重装为 `Cfg_Ratio`。
3. **启动计算**：记下当前写地址为 `Tap0Addr`（抽头 0 对应的最新样本位置），把 `TapCnt` 装为「抽头数−1」，开始串行累加。
4. **串行累加（N 拍）**：每拍 `TapCnt` 递减，读地址 = `Tap0Addr − TapCnt`（实现 \(x[n-k]\) 的回看），系数地址 = `TapCnt`；每通道一个乘法器算 `data × coef`，累加进各自的累加器。
5. **收敛输出**：最后一个抽头算完后，每个通道用 `olo_fix_resize` 把全精度累加值舍入+饱和到 `OutFmt`，所有通道同拍输出。

读 RAM 有固定延迟，所以 `CalcOn`、`First`、`Last` 三个流水控制向量随数据一起下传，保证「第 0 个抽头清零累加器、最后一个抽头捕获结果」的时序对齐。

#### 4.2.3 源码精读

**每通道一组数据通路（generate 循环）**——这是 chpar 与 chtdm 最核心的区别：

[olo_fix_fir_dec_ser_chpar.vhd:394-437](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L394-L437) —— `g_channels` 循环为每个通道例化一个 `olo_fix_mult`（输入是本通道的历史数据 `MultInTap(i)` + 共享系数 `MultInCoef`）和一个 `olo_fix_resize`。所有乘法器读同一个系数、同一个抽头地址，只是数据切片不同。

**每通道一个累加器**：

[olo_fix_fir_dec_ser_chpar.vhd:275-288](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L275-L288) —— `Accu` 是数组 `AccuVec_t(0 to Channels_g-1)`。`First` 拍清零、其余拍把 `MultOut_Data(i)` 累加进去；`AccuValid` 在最后一个抽头（`Last(AccuStage_c)`）置位。

**宽字数据 RAM（一个字容纳所有通道）**：

[olo_fix_fir_dec_ser_chpar.vhd:373-391](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L373-L391) —— `olo_base_ram_sdp` 深度 `DataMemDepth_c`、宽度 `InWidth_c * Channels_g`，读地址就是 `TapRdAddr_2`（不需要通道位，因为所有通道在同一字里）。

**共享系数存储**：

[olo_fix_fir_dec_ser_chpar.vhd:347-371](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L347-L371) —— 一个 `olo_fix_coef_storage` 实例，所有通道共用。

**启动零填充（ReplaceZero）保证位真**：复位后数据 RAM 里可能有残留，未写过的位置会被替换成 0，与 Python 模型（延迟线初始化为 0）对齐：

[olo_fix_fir_dec_ser_chpar.vhd:249-271](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L249-L271) —— 当 `ReplaceZero='1'` 且读地址超过已写范围时，喂给乘法器的是 0 而非 RAM 残留。

#### 4.2.4 代码实践

1. **实践目标**：实例化一个最简单的 chpar 低通 FIR（固定系数 ROM）。
2. **操作步骤**：参考文档示例 [olo_fix_fir_dec_ser_chpar.vhd:17-21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L17-L21)（描述）与 `doc/fix/olo_fix_fir_dec_ser_chpar.md` 的 Example Instantiation，写一个 `Channels_g=1`、`MaxRatio_g=1`、`MaxTaps_g=3`、`CoefInit_g="0.25, 0.5, 0.25"`（标准 3 抽头低通）的例化，端口只接 `Clk/Rst/In_Valid/In_Data/Out_Valid/Out_Data`。
3. **观察现象**：因为 `MaxRatio_g=1`（ratio=1，不抽取）、`RuntimeCfg_g` 默认 false，`Cfg_Ratio/Cfg_Taps` 可悬空；所有 `Coef_*` 端口在 ROM 模式也可悬空。
4. **预期结果**：综合/仿真时无需接额外控制端口即可工作；输出是输入的 3 点加权平均。

#### 4.2.5 小练习与答案

**练习 1**：chpar 的数据 RAM 宽度为什么是 `InWidth × Channels`，而深度却**不带**通道因子？
**答案**：通道并行意味着所有通道同一时刻的样本在**同一拍**写入，所以把它们拼成一个宽字存一行，深度只随延迟线长度（`MaxTaps+MaxRatio`）增长。宽度换深度：通道越多，RAM 字越宽但行数不变。

**练习 2**：如果把 `MaxTaps_g` 设为 1（单抽头），会发生什么？
**答案**：会被断言拦截。见 [olo_fix_fir_dec_ser_chpar.vhd:184-186](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L184-L186)（`MaxTaps_g >= 2`）。单抽头 FIR 不是一个有意义的高通/低通滤波器，故不支持。

---

### 4.3 串行抽头 chtdm：TDM 通道 FIR

#### 4.3.1 概念说明

`olo_fix_fir_dec_ser_chtdm` 处理「TDM」场景：多个通道的样本**轮流**出现在同一根窄数据线上（通道 0、1、…、N-1、0、1、…），用 `In_Last` 标记一帧的最后一个通道。它与 chpar 的最大区别是：**所有通道共享同一个乘法器和同一个累加器**。计算顺序是「先算通道 0 的所有抽头，再算通道 1 的所有抽头，……」，所以一组输出要花 `Taps × Channels` 拍。

资源极省（1 个乘法器，无论多少通道），代价是吞吐随通道数下降。

#### 4.3.2 核心流程

1. **输入分通道写 RAM**：用 `ChannelNr` 区分当前样本属于哪个通道；数据 RAM 地址 = `ChannelNr & TapAddr`（通道在高位选区、低位选抽头），每个通道有独立的延迟线区域。
2. **一帧到齐后启动**：当一帧的最后一个通道（`ChannelNr = Channels-1`）到达且抽取相位 `DecCnt=0` 时，启动一次完整计算。
3. **通道轮流累加**：`CalcChnl` 从 0 递增到 `Channels-1`；每个通道内 `TapCnt` 从「抽头数−1」减到 0，逐拍读 `data × coef` 累加。一个通道算完（`Last`）就把累加值送 resize 输出，然后清零累加器、切到下一个通道。
4. **输出按 TDM 顺序**：结果按通道 0、1、… 顺序出现在 `Out_Data` 上，`Out_Last` 在最后一个通道拉高，重建 TDM 帧。

#### 4.3.3 源码精读

**通道在数据 RAM 地址的高位**：

[olo_fix_fir_dec_ser_chtdm.vhd:484-504](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L484-L504) —— `DataRamWrAddr = ChannelNr(1) & TapWrAddr`、`DataRamRdAddr = CalcChnl_2 & TapRdAddr_2`；RAM 深度 `DataMemDepth_c * Channels_g`、宽度仅 `width(InFmt)`。这与 chpar「宽字浅 RAM」恰好相反。

**单一乘法器 + 单一 resize**：

[olo_fix_fir_dec_ser_chtdm.vhd:416-454](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L416-L454) —— 整个实体只有**一个** `olo_fix_mult` 和**一个** `olo_fix_resize`，所有通道轮流复用。累加器 `Accu` 是单个标量（不是数组）。

**通道轮转控制**：

[olo_fix_fir_dec_ser_chtdm.vhd:245-273](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L245-L273) —— `TapCnt` 减到 0 时，若还没到最后一个通道就 `CalcChnl_1 + 1` 并重装 `TapCnt` 开始下一通道；到最后一个通道则 `CalcOn(1):='0'` 结束本轮。

**TDM 帧边界检查（仅仿真）**：

[olo_fix_fir_dec_ser_chtdm.vhd:394-400](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L394-L400) —— `In_Last` 没有功能作用，仅在仿真里检查它是否恰好在最后一个通道拉高，否则报错（见 `Last Handling` 约定）。

#### 4.3.4 代码实践

1. **实践目标**：从资源角度对比 chpar 与 chtdm。
2. **操作步骤**：阅读 [olo_fix_fir_dec_ser_chtdm.vhd:11-14](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L11-L14) 的描述（「single multiplier computes filter taps serially … one after the other for the next channel」），再对比 chpar 的 [olo_fix_fir_dec_ser_chpar.vhd:11-14](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd#L11-L14)（「one multiplier per channel」）。
3. **观察现象**：在「4 通道、16 抽头」配置下，chpar 产出一组结果要 16 拍、用 4 个乘法器；chtdm 产出一组结果要 64 拍、用 1 个乘法器。
4. **预期结果**：你能用一句话说清取舍——「chpar 用乘法器数量换吞吐，chtdm 用时间换面积」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 chtdm 的数据 RAM 地址要把 `ChannelNr` 放在**高位**、`TapAddr` 放低位？
**答案**：每个通道需要一条独立的延迟线（连续的抽头历史）。把通道放高位、抽头放低位，相当于把 RAM 切成 `Channels` 个连续小段，每段是一条通道的延迟线；访问时高位选通道、低位选该通道内的历史样本。

**练习 2**：同一份系数，chtdm 的系数存储大小与通道数有关吗？
**答案**：无关。所有通道共享同一套系数，所以系数 RAM/ROM 深度只由 `MaxTaps_g` 决定（`CoefMemDepth_c = 2^log2ceil(MaxTaps_g)`），见 [olo_fix_fir_dec_ser_chtdm.vhd:112](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chtdm.vhd#L112)。

---

### 4.4 系数存储 olo_fix_coef_storage

#### 4.4.1 概念说明

`olo_fix_coef_storage` 是一个「为 DSP 数据通路量身定做」的系数存储器。你当然可以直接用 `olo_base_ram_sdp` 存系数，但这个实体解决了三个 DSP 场景的痛点：

1. **实数初始化**：`Init_g` 直接写 `"0.3, 0.55, 0.2"` 这样的实数串，实体内部量化到 `Fmt_g`。你可以从 Python/MATLAB 复制系数，无需手动先量化成二进制。
2. **ROM/RAM 双模式**：固定系数用 ROM（综合成块 ROM/查找表），运行期可变系数用 RAM。
3. **双读口分工**：`Coef` 口只读、供数据通路每拍取系数；`Cfg` 口可写、供软件在运行期更新系数（可选回读校验）。两个口的读延迟都可配。

#### 4.4.2 核心流程

- **初始化**：elaboration 时 `initData` 把 `Init_g` 字符串解析成实数数组，逐个 `cl_fix_from_real` 量化成定点，缺失项补 0。
- **ROM 模式**：用一个 `shared variable` 存数组，加 `rom_style`/`romstyle`/`syn_romstyle` 三套厂商属性控制实现方式；`Cfg` 写口被忽略，`Cfg_RdData` 恒为 0。
- **RAM 模式**：同样用 `shared variable`，但根据 `RamBehavior_g`（RBW/WBR）在同一个时钟进程里调整「先读后写 / 先写后读」的语句顺序，从而控制同址同周期读写的返回值。
- **读延迟流水线**：`CoefPipe`/`RdPipe` 按 `RdLatency_g` 深度打拍，并用 `shreg_extract = suppress` 防止被抽成移位寄存器、稳定推断成块 RAM。

#### 4.4.3 源码精读

**实数初始化（量化）**：

[olo_fix_coef_storage.vhd:79-90](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_coef_storage.vhd#L79-L90) —— `fromString(Init_g)` 把逗号分隔字符串变成 `RealArray_t`，再 `cl_fix_from_real(..., Fmt_c)` 量化；超出 `Init_g` 长度的项保持初值 0。

**双口（Cfg 配置口 + Coef 数据口）**：

[olo_fix_coef_storage.vhd:50-66](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_coef_storage.vhd#L50-L66) —— `Cfg_*` 端口（地址/写使能/写数据/读使能/读数据/读有效）与 `Coef_*` 端口（地址/读使能/读数据/读有效）分离。

**ROM 实现 + 三套厂商属性**：

[olo_fix_coef_storage.vhd:114-152](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_coef_storage.vhd#L114-L152) —— `shared variable Rom_v` 配 `rom_style`（Vivado）/`romstyle`（Quartus）/`syn_romstyle`（其他）三属性，一份代码跨厂商控制 ROM 实现。

**RAM 的 RBW/WBR 由语句顺序决定**：

[olo_fix_coef_storage.vhd:198-236](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_coef_storage.vhd#L198-L236) —— WBR 分支先写后读（返回新值），RBW 分支先读后写（返回旧值）。这正是 u2-l3 讲过的「读时写歧义」用 `shared variable` 语句顺序来实现的套路。

#### 4.4.4 代码实践

1. **实践目标**：用 RAM 模式实现「运行期可更新系数」。
2. **操作步骤**：阅读 [olo_fix_coef_storage.vhd:105-110](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_coef_storage.vhd#L105-L110) 的 `StorageType_g`/`RamBehavior_g` 合法值断言；再在 FIR 里设 `CoefStorageType_g => "RAM"`、`CoefRamReadback_g => true`，通过 `Coef_Addr/Coef_WrEna/Coef_WrData` 在运行期改写系数。
3. **观察现象**：写系数时若同一地址同一拍恰好被 `Coef` 口读，RBW 返回旧值、WBR 返回新值。
4. **预期结果**：改写后滤波器响应当即变化；`Coef_RdData`（回读口）能读回刚写的值用于校验。注意：并非所有 FPGA 都允许 RAM 初始化，SRAM 型 FPGA 通常允许（参见 `doc/fix/olo_fix_coef_storage.md` 的 Initialization 一节）。

#### 4.4.5 小练习与答案

**练习 1**：为什么系数用 `shared variable` 而不是 `constant` 或普通 `signal`？
**答案**：注释 [olo_fix_coef_storage.vhd:115](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_coef_storage.vhd#L115) 说明——综合属性无法干净地加到 `constant` 上；用 `shared variable` 既能被初始化、又能挂 `rom_style` 等属性，还能在 RAM 模式下被改写。

**练习 2**：RAM 模式下，`Cfg` 口和 `Coef` 口同时读同一地址会冲突吗？
**答案**：不会。两个口各自读自己的 `RdPipe`/`CoefPipe`，本质是一个真双端口 RAM 的两个读口（写只在 `Cfg` 口）。`RamReadback_g=true` 时 `Cfg` 口才有读功能，此时综合成真双端口 RAM，要注意目标器件对真双口 RAM 的限制（见 doc 注释）。

---

### 4.5 滑动平均 olo_fix_mov_avg

#### 4.5.1 概念说明

`olo_fix_mov_avg` 实现滑动平均：每个输出是最近 \(N\) 个输入的均值。

\[
y[n] = \frac{1}{N}\sum_{k=0}^{N-1} x[n-k]
\]

它其实是一个**系数全为 \(1/N\) 的特殊 FIR**。但与 4.2/4.3 的「串行抽头」FIR 不同，mov_avg 用了一个 O(1) 的巧办法——不必每拍重新加 N 个数，而是维护一个滑动和，**每拍加新样本、减最老样本**：

\[
S[n] = S[n-1] + x[n] - x[n-N]
\]

这样无论 N 多大，每拍只做 1 加 1 减。`olo_base_delay` 负责把输入延迟 \(N\) 拍得到 \(x[n-N]\)。

#### 4.5.2 核心流程

1. **延迟线**：`olo_base_delay`（深度 `Taps_g`）输出 \(x[n-N]\)。
2. **差分**：`Diff = x[n] − x[n-N]`（`cl_fix_sub`）。
3. **滑动和**：`Sum = Sum + Diff`（`cl_fix_add`）。求和格式 `MovSumFmt` 比输入多 `AddBits = ceil(log2(N))` 个整数位，容纳 N 项之和的位增长。
4. **增益校准**（`GainCorrType_g`）：
   - `"NONE"`：不校准，输出就是滑动和（增益为 \(N\)）。
   - `"SHIFT"`：右移 `AddBits` 位，把增益校正到 \((0.5, 1.0]\)；仅当 N 为 2 的幂时精确。
   - `"EXACT"`：N 为 2 的幂时退化为 SHIFT（无乘法器）；否则 SHIFT 之后再乘一个细校系数 \(G_c = 2^{\text{AddBits}}/N\)，做到增益恰为 1.0。

#### 4.5.3 源码精读

**位增长与增益系数推导**：

[olo_fix_mov_avg.vhd:72-84](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mov_avg.vhd#L72-L84) —— `AddBits_c = log2ceil(Taps_g)`；`MovSumFmt_c` 的整数位 = `In.I + AddBits`；细校系数 `Gc_c = 2.0**AddBits / Taps_g`，量化成 `GainCorrCoefFmt_c`（必须是 `(0,1,x)` 格式）。

**加新减旧的差分实现**：

[olo_fix_mov_avg.vhd:118-130](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mov_avg.vhd#L118-L130) —— Stage 1 算 `Diff_1 = Data_0 − Del_Data`（`Del_Data` 来自延迟线），Stage 2 算 `Sum_2 = Diff_1 + Sum_2`。

**延迟线例化**：

[olo_fix_mov_avg.vhd:153-168](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mov_avg.vhd#L153-L168) —— `olo_base_delay` 深度 `Taps_g`，提供 \(x[n-N]\)。

**三种增益校准分支（互斥 generate）**：

[olo_fix_mov_avg.vhd:171-247](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mov_avg.vhd#L171-L247) —— `g_none` 直接 resize；`g_shift` 先 `cl_fix_shift(…, -AddBits)` 再 resize（N 为 2 的幂时也走这里，精确）；`g_gain_corr` 在非 2 的幂 + EXACT 时启用，shift 后接一个 `olo_fix_mult` 乘 `GcFix_c`。

#### 4.5.4 代码实践

1. **实践目标**：观察「加新减旧」与「直接卷积」结果一致。
2. **操作步骤**：仓库自带的协仿真脚本 [test/fix/olo_fix_mov_avg/cosim.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mov_avg/cosim.py) 在画图模式下（`cosim_mode=False`）会同时画「定点 mov_avg 输出」与「`np.convolve` 理想滑动平均」并给出误差曲线。在装有 `numpy/matplotlib` 与 `en_cl_fix`（见 `.gitmodules` 的 `3rdParty/en_cl_fix`）的环境里运行 `python test/fix/olo_fix_mov_avg/cosim.py`。
3. **观察现象**：理想曲线与定点曲线几乎重合，误差曲线上有量化噪声（量级为 `OutFmt` 的分辨率）。
4. **预期结果**：误差在个位 LSB 量级，验证「加新减旧」与「N 点卷积」数学等价。**待本地验证**（依赖 Python 环境是否就绪）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 mov_avg 不像 FIR 那样需要 `olo_fix_coef_storage`？
**答案**：它的「系数」恒为 \(1/N\)，固化在「右移 AddBits 位 + 细校乘法」里，没有可变系数集合，所以不需要系数存储。

**练习 2**：`GainCorrType_g="EXACT"` 且 `Taps_g=8` 时，实体里会有乘法器吗？
**答案**：不会。8 是 2 的幂，`g_shift` 的生成条件 `isPower2(Taps_g) and EXACT` 成立（[olo_fix_mov_avg.vhd:194](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mov_avg.vhd#L194)），右移即精确校准，省掉乘法器。乘法器只在「EXACT + 非 2 的幂」时才出现。

---

## 5. 综合实践：用 chpar 实现低通 FIR 并对比 mov_avg

本任务把本讲四块内容串起来：设计一个真正的低通 FIR（系数由 `coef_storage` 提供）、跑位真协仿真验证带外衰减，再与 `mov_avg` 这种「粗糙低通」对比。

### 背景与现成工具

仓库已经把这件事做了一遍，你可以直接复用：

- 设计脚本 [test/fix/olo_fix_fir_dec_ser_chtdm/cosim.py:46-49](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_fir_dec_ser_chtdm/cosim.py#L46-L49) 用 `scipy.signal.firwin(Taps_g, cutoff)` 设计低通 FIR（截止频率 `cutoff = 1/Ratio`，归一化到 Nyquist=1.0），系数量化到 `CoefFmt_g` 后写入 `Coef.fix`，TB 用它初始化 `olo_fix_coef_storage`（ROM 模式）。
- 同一脚本 [cosim.py:75-77](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_fir_dec_ser_chtdm/cosim.py#L75-L77) 还构造了一路 `sps.chirp` 扫频信号（频率从 0 扫到 5 kHz）作为「带内 + 带外」混合激励，能直观看出高频被衰减。
- 位真模型 [olo_fix_fir_dec.py:87-109](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_fir_dec.py#L87-L109) 用 `scipy.signal.lfilter` 算参考输出，再按 `Ratio` 抽取、resize，作为 TB 的期望值。

### 步骤

1. **跑 chpar 的位真测试**（最稳的路径，依赖 VUnit + GHDL）：

   ```bash
   cd sim
   python run.py --ghdl -v -k olo_fix_fir_dec_ser_chpar 2>&1 | tee fir_chpar.log
   ```

   `run.py` 会先调用 `pre_config`（即 `cosim.cosim`）生成 `.fix` 文件，再编译仿真。关注默认配置 `ch4`、`ratio1` 等用例是否全部 pass（见 [sim/test_configs/olo_fix.py:755-786](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py#L755-L786)）。

2. **（可选）画图看低通效果**：在就绪的 Python 环境运行 `python test/fix/olo_fix_fir_dec_ser_chtdm/cosim.py`（它 `__main__` 里 `cosim_mode=False`，会画各通道输入/输出）。重点看 ch2 的扫频信号：高于截止频率的部分被衰减。

3. **对比 mov_avg**：运行 `python test/fix/olo_fix_mov_avg/cosim.py`，对比 mov_avg 对阶跃/扫频的响应。

### 需要观察的现象

- FIR（firwin 设计）在通带内近似平坦、阻带有明显衰减，过渡带较窄。
- mov_avg 的「频率响应」是 sinc 形（第一旁瓣仅衰减约 13 dB、且有零点），过渡带很宽、选择性差——它便宜（无系数 RAM、O(1) 每拍），但滤波质量远不如专门设计的 FIR。
- 两者都通过各自的协仿真（Python 黄金模型逐拍比对），说明 HDL 与数学模型位真一致。

### 预期结果

- `run.py` 报告 chpar 全部用例 pass（`pass` 计数 > 0、无 `fail`）。
- 你能用一句话总结选型：**需要精确频率选择性 → 用 fir_dec_ser_chpar/chtdm（系数由 coef_storage 提供，可 ROM 可 RAM）；只需要简单平滑且资源极省 → 用 mov_avg**。
- 步骤 2、3 的画图结果**待本地验证**（依赖 matplotlib/scipy 与 `en_cl_fix` 是否在 Python 路径中）。

> 若本地没有 GHDL/VUnit，也可做「源码阅读型实践」：跟踪 `cosim.py` 写出 `Coef.fix` → TB 用 `fixFileReadString` 读回当 `CoefInit` → `olo_fix_coef_storage` ROM 模式初始化 → FIR 计算，画出这条「系数从 Python 到 HDL」的数据流，并解释为何这样能保证位真。

## 6. 本讲小结

- Open Logic 的 FIR 实体名编码**三个正交维度**：速率变换（`dec`/`int`）× 抽头计算（`ser`/`par`/`semi`）× 通道组织（`chtdm`/`chpar`）；当前仓库实现的是 `dec_ser_chpar` 与 `dec_ser_chtdm`。
- 「串行（ser）」= 一个乘法器每拍算一个抽头，N 抽头要 N 拍；实体不产生反压，输入速率受 \(f_{\text{in}} \le f_{\text{clk}}\cdot\text{Ratio}/\text{Taps}\)（chtdm 再除以通道数）约束，需用 `olo_base_rate_limit` 限速。
- `chpar`（通道并行）每通道一个乘法器 + 一个累加器，数据 RAM 是「宽字浅」、N 拍出一组结果；`chtdm`（TDM）共享一个乘法器 + 一个累加器，数据 RAM 是「窄字深、通道在地址高位」、\(N\times\text{Channels}\) 拍出一组结果。系数存储与通道数无关。
- 两实体共享格式推导：`MultFmt` 全精度、`AccuFmt` 在 `OutFmt` 之上留 `GuardBits_g` 个整数保护位，最后由 `olo_fix_resize` 收敛；启动时用 `ReplaceZero` 把未写 RAM 位置零，保证从首个样本起与 Python 模型位真。
- `olo_fix_coef_storage` 用实数初始化（`Init_g` 自动量化）、ROM/RAM 双模式、`Coef`（数据口）+`Cfg`（配置口）双读口，RAM 的 RBW/WBR 由 `shared variable` 的读写语句顺序实现，是所有 DSP 系数存储的统一积木。
- `olo_fix_mov_avg` 用「加新减旧」把 O(N) 累加降到 O(1)，位增长 `log2ceil(N)`；增益校准 `NONE/SHIFT/EXACT` 三档，N 为 2 的幂时 EXACT 退化成纯移位、无需乘法器。

## 7. 下一步学习建议

- **回到第 10 单元（u10）工程化**：本讲的协仿真依赖 VUnit `pre_config` + `olo_fix_sim_stimuli/checker`，建议接着读 u10-l1（VUnit 测试台与验证组件）与 u10-l2（`sim/run.py` 与 `test_configs`），搞清「先 codegen/cosim、再仿真」的时序是如何被强制的。
- **对比 CIC（u9-l3）**：CIC 也是抽取滤波器，但只用加减法、无需乘法器，常作多级抽取的第一级，FIR 接在其后做通带补偿。把本讲的 FIR 与 u9-l3 的 CIC 串成一条「CIC 粗抽取 → FIR 精整形」的多级抽取链来思考。
- **继续读源码**：精读 [olo_fix_fir_dec_ser_chpar.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_fir_dec_ser_chpar.vhd) 的 `p_comb` 全过程，画出 Stage 0→AccuStage_c 的数据与控制（`CalcOn/First/Last`）流水时序图；再看 `olo_fix_fir_dec.py` 如何用 `lfilter` + 抽取复现这条链，做到软硬位真。
