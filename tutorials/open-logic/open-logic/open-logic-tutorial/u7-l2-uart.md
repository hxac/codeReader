# UART（olo_intf_uart）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 UART 一帧由哪些位组成，以及 `olo_intf_uart` 用哪些泛型来配置波特率、数据位、停止位与校验位。
- 理解「2 倍波特率过采样」是如何用 `olo_base_strobe_gen` 实现的，以及它为何能让接收端在每一位的中心采样、并具备毛刺过滤能力。
- 读懂发送（TX）与接收（RX）两条有限状态机（FSM）的流转，讲清起始位、数据位、校验位、停止位是如何被逐位移出 / 移入的。
- 明白 TX 侧是完整的 AXI-S Valid/Ready 握手（支持反压），而 RX 侧**只有 Valid、没有 Ready**（消费者必须立刻接收）这一关键差异，并理解 `Uart_Rx` 为何要在内部先过一遍同步器。

---

## 2. 前置知识

在进入本讲前，建议你已经具备以下认知（来自前置讲义）：

- **AXI-S 握手**：数据传递用 `Valid`/`Ready` 握手，二者同时为高才完成一次传输；带 `Ready` 的通路支持反压（back-pressure），见 u1-l5、u2-l2。
- **两进程法（two-process method）**：组合进程 `p_comb` 只算下一拍状态 `r_next`，时序进程 `p_seq` 只打拍并复位，所有寄存器收进一个 `record`，复位写在 `p_seq` 末尾的覆盖里，见 u2-l2。
- **同步器（`olo_intf_sync`）**：把外部异步单比特串过多级触发器以降低亚稳态风险，见 u7-l1。
- **`olo_base_strobe_gen`**：按设定频率产生单周期脉冲（strobe），可工作在小数高精度模式（`FractionalMode_g`），并有 `In_Sync` 输入用来在事件发生时把脉冲相位重置对齐，见 u5-l1。

### 2.1 通俗理解 UART 协议

UART（通用异步收发传输）是一种**异步、串行、单比特**的通信协议。「异步」指收发双方**没有共享时钟**，靠事先约定好的「波特率（Baud Rate）」各自计时。

一根 UART 线在空闲时保持高电平（idle = 1）。发送一个字节时，线路电平随时间变化如下（以 8 数据位、无校验、1 停止位为例）：

```text
空闲(1) | 起始位(0) | D0 D1 D2 D3 D4 D5 D6 D7 | 停止位(1) | 空闲(1)
```

- **起始位（Start）**：1 个低电平比特，标志一帧开始（下降沿）。
- **数据位（Data）**：5～9 位，`olo_intf_uart` 支持 7～9 位，**低位先发（LSB first）**。
- **校验位（Parity，可选）**：none / even / odd。
- **停止位（Stop）**：1 / 1.5 / 2 个高电平比特，标志一帧结束。

每一位的持续时间由波特率决定：

\[
T_{\text{bit}} = \frac{1}{\text{BaudRate}}
\]

例如 115200 波特率下，\(T_{\text{bit}} \approx 8.68\,\mu s\)。由于没有共享时钟，接收端必须自己产生一个同频率的「采样节拍」，并且**尽量在每一位的正中间采样**，以获得最大裕量、容忍双方波特率的微小偏差——这正是本讲要讲的「2 倍过采样」的动机。

> 注：官方文档明确说 UART 协议是常识，不再赘述，并给出了[外部参考资料](https://ece353.engr.wisc.edu/serial-interfaces/uart-basics/)。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/intf/vhdl/olo_intf_uart.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd) | UART 实体本体，包含 TX/RX 两条 FSM 与所有配置逻辑。 |
| [doc/intf/olo_intf_uart.md](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_uart.md) | 官方使用文档，列出泛型、端口与约束。 |
| [test/intf/olo_intf_uart/olo_intf_uart_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_uart/olo_intf_uart_tb.vhd) | VUnit 测试台，用 `uart_master`/`uart_slave` 验证组件（VC）测试收发。 |
| [sim/test_configs/olo_intf.py](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py) | 为 UART 测试台按泛型组合注册多个测试用例。 |

实体内部还复用了两个已学过的积木（不在 intf 目录）：

- `olo_base_strobe_gen`（base 区，u5-l1）：产生 2 倍波特率的采样节拍。
- `olo_intf_sync`（intf 区，u7-l1）：对 `Uart_Rx` 做同步。

---

## 4. 核心概念与源码讲解

### 4.1 波特率与帧格式

#### 4.1.1 概念说明

`olo_intf_uart` 是一个「简单 UART」——它把 UART 协议里所有可配置项都做成了**编译期泛型**，运行时不可改。配置分两类：

- **波特率与时钟**：`ClkFreq_g`（系统时钟频率，单位 Hz）、`BaudRate_g`（波特率，默认 115200）。
- **帧格式**：`DataBits_g`（7～9，默认 8）、`StopBits_g`（`"1"`/`"1.5"`/`"2"`，默认 `"1"`）、`Parity_g`（`"none"`/`"even"`/`"odd"`，默认 `"none"`）。

注意 `StopBits_g` 与 `Parity_g` 是**字符串泛型**——这是 Open Logic 的惯用模式（见 u8-l1 的字符串泛型模式）：用字符串承载枚举语义，既能在 VHDL 里用 `compareNoCase` 比较，也方便跨语言实例化时传参。

由于字符串泛型无法在实体端口处直接声明取值范围，合法性改由 **elaboration 阶段的 `assert` 断言**来把关。

#### 4.1.2 核心流程

整帧包含的「数据类比特数」（不含停止位）由一个编译期函数 `transmitBits` 算出：

\[
\text{transmitBits} = \underbrace{1}_{\text{起始位}} + \text{DataBits\_g} + \begin{cases}1 & \text{Parity\_g} \ne \text{none}\\ 0 & \text{otherwise}\end{cases}
\]

**关键设计：2 倍波特率过采样。** 内部并不直接按波特率产生节拍，而是用 `olo_base_strobe_gen` 产生 **2 倍波特率**的 strobe：

\[
f_{\text{strobe}} = 2 \times \text{BaudRate\_g}
\]

于是每个比特持续时间内会出现恰好 2 个 strobe。这样做有两个好处：

1. **中心采样**：接收端可以在每位的中点采样（见 4.3），获得最大时钟偏差裕量。
2. **毛刺过滤**：起始位检测后，若在线路「应当为低」的窗口内发现线路已回高，可判定为毛刺并回退（见 4.3）。

为保证过采样精度，strobe 生成器开启小数模式（`FractionalMode_g => true`），即使 `ClkFreq/BaudRate` 不是整数也能均匀分摊节拍。同时存在一条硬约束：**波特率不得超过时钟的 1/10**（即每个比特至少跨 20 个时钟周期），否则采样精度无法保证。

#### 4.1.3 源码精读

泛型声明——`ClkFreq_g` 无默认值（必填），其余都有默认值，体现了「可选即有默认」的规范（u1-l5）：

[olo_intf_uart.vhd:34-40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L34-L40) —— 五个泛型，`StopBits_g`/`Parity_g` 为字符串。

`transmitBits` 函数累加起始位与（可选）校验位，返回一帧中需要在 `Data_s` 状态移出的总比特数（停止位另算）：

[olo_intf_uart.vhd:99-109](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L99-L109) —— 编译期计算一帧的「数据类比特数」。

`stopStrobeCount` 把停止位翻译成 strobe 计数（注意是 2 倍过采样，所以 1 比特 = 2 个 strobe）：`"1"`→2、`"1.5"`→3、`"2"`→4：

[olo_intf_uart.vhd:88-97](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L88-L97) —— 停止位换算成 2 倍过采样下的 strobe 个数。

`parityBit` 函数：把所有数据位异或起来得到 even 校验，再按 `Parity_g` 决定是否取反（odd = 非 even）。`none` 时返回 `'0'`（实际不会用到）：

[olo_intf_uart.vhd:69-86](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L69-L86) —— 校验位计算，用 `compareNoCase` 做大小写不敏感的字符串比较。

四条断言把不可在端口处约束的字符串/数值合法性补齐：波特率大于 0、停止位取值合法、校验取值合法，以及那条关键的 **`BaudRate_g <= ClkFreq_g / 10.0`**：

[olo_intf_uart.vhd:145-159](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L145-L159) —— 四条 elaborate 断言，违反时报告 `errorMessage`。

2 倍过采样的实现——两个 `olo_base_strobe_gen` 都把目标频率设为 `BaudRate_g*2.0` 并开启小数模式：

[olo_intf_uart.vhd:343-367](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L343-L367) —— TX 与 RX 各用一个 strobe 生成器，频率为波特率的 2 倍。

#### 4.1.4 代码实践

**实践目标**：直观感受 2 倍过采样节拍与帧长度的关系。

**操作步骤**：

1. 打开 `olo_intf_uart.vhd`，定位到 `stopStrobeCount`（第 88 行）与 `transmitBits`（第 99 行）。
2. 假设配置为 `DataBits_g => 8`、`Parity_g => "none"`、`StopBits_g => "1"`，手算：
   - `transmitBits` = ?
   - `stopStrobeCount` = ?
   - 一整帧（含停止位）共占多少个 strobe？多少个比特时间？
3. 改 `Parity_g => "even"` 再算一遍。

**需要观察的现象 / 预期结果**：

- 无校验时：`transmitBits = 1 + 8 = 9`，`stopStrobeCount = 2`；整帧 = \(9 + 1 = 10\) 比特 = 20 个 strobe（\(10 \times 2\)）。
- 有校验时：`transmitBits = 1 + 8 + 1 = 10`，整帧 = 11 比特 = 22 个 strobe。
- 结论：每帧的 strobe 数恒为「比特数 × 2」，验证了 2 倍过采样的设计。

> 待本地验证：你可以在仿真里对 `TxStrobe` 计数，确认发送一帧正好产生上述数量的脉冲。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaudRate_g` 必须不超过 `ClkFreq_g/10`，而不是 `ClkFreq_g/2`？

**答案**：UART 是异步协议，接收端靠本地时钟重建采样节拍。若波特率接近时钟频率，每个比特只跨极少时钟周期，2 倍过采样不足以在中点稳定采样，也无法过滤毛刺；限制为 1/10 保证每个比特至少 ~20 个时钟周期，留出足够的相位裕量。

**练习 2**：`StopBits_g` 为 `"1.5"` 时，`stopStrobeCount` 返回 3。3 个 strobe 对应几个比特时间？

**答案**：在 2 倍过采样下 2 个 strobe = 1 比特，故 3 个 strobe = 1.5 比特，即 1.5 个停止位。

---

### 4.2 发送通路（TX）

#### 4.2.1 概念说明

TX 通路负责把一个并行字节（`Tx_Data`）按 UART 帧格式**串行移出**到 `Uart_Tx` 引脚。它是一个 4 状态的 FSM：`Reset_s → Idle_s → Data_s → Stop_s → Idle_s`。TX 侧提供完整的 AXI-S Valid/Ready 握手：用户给 `Tx_Valid`/`Tx_Data`，模块回 `Tx_Ready`，握手成功即表示数据已被收下排队发送。

#### 4.2.2 核心流程

```text
Reset_s:  复位后第一拍，把 Tx_Ready 置 1（允许接收用户数据）
   ↓
Idle_s:   等待 Tx_Valid&Tx_Ready 握手
          握手成功 → 锁存数据到移位寄存器，拉 TxSync 同步节拍，Tx_Ready=0（忙）
          ↓
Data_s:   每个 strobe 维持一位电平；每 2 个 strobe 移出 1 位（起始位→数据→校验）
          移完 transmitBits 位 → Stop_s
          ↓
Stop_s:   Uart_Tx 保持高（停止位），持续 stopStrobeCount 个 strobe
          → 回 Idle_s，Tx_Ready=1，可发下一帧
```

**移位寄存器的拼装顺序很关键**。握手套接数据时执行：

```vhdl
v.TxShiftReg := parityBit(Tx_Data) & Tx_Data & '0';
```

从最低位到最高位依次是：`'0'`（起始位）、`Tx_Data`（数据，低位在前）、`parityBit`（校验位）。由于发送时总是输出 `TxShiftReg(0)` 并向右移位，**最低位先发**，恰好符合 UART 的 LSB-first 约定。

**节拍同步**：握手时拉高 `TxSync` 一个时钟，`olo_base_strobe_gen` 检测到 `In_Sync` 上升沿会把相位清零重对齐，使后续 strobe 的边沿与比特边界对齐。由于 strobe 生成器需要约 2 个时钟周期才完成同步，FSM 用 `TxSync`/`TxSyncLast` 两拍窗口**忽略这段时间内的 strobe**，避免相位未稳就开始计数。

#### 4.2.3 源码精读

握手与数据锁存（注意移位寄存器拼装与 `Tx_Ready` 拉低）：

[olo_intf_uart.vhd:182-192](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L182-L192) —— `Idle_s`：握手成功后锁存数据、同步节拍、置忙。

`Data_s`：输出最低位；忽略同步窗口内的 strobe；每 2 个 strobe 右移一位；计满 `transmitBits*2-1` 后进入停止位：

[olo_intf_uart.vhd:194-209](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L194-L209) —— 逐位移出，`r.TxCount mod 2 = 1` 时移位。

`Stop_s`：`Uart_Tx` 维持高，计满 `stopStrobeCount` 个 strobe 后回 `Idle_s` 并重新置 `Tx_Ready=1`：

[olo_intf_uart.vhd:211-221](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L211-L221) —— 停止位持续时长由 `stopStrobeCount` 决定。

复位行为：`p_seq` 末尾把 `StateTx` 强制为 `Reset_s`、`Tx_Ready` 清 0；于是复位释放后第一拍 `Reset_s` 把 `Tx_Ready` 置 1。这正是测试台 `ResetValues` 用例检查的两拍行为：

[olo_intf_uart.vhd:329-336](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L329-L336) —— 复位只清状态，不清数据通路。

#### 4.2.4 代码实践

**实践目标**：验证 TX 逐位移出的电平序列与 UART 帧一致。

**操作步骤**：

1. 运行现有测试台中的发送用例（无需自己写代码）：

   ```bash
   cd sim
   python3 run.py --ghdl -v -p olo_intf_uart_tb.TxSingle
   ```

   （具体命令行请以本地 `sim/run.py` 的参数为准；`-p` 用 test pattern 选取单个用例，**待本地验证**所用仿真器是否已安装。）

2. 阅读测试台 `TxSingle` 用例：它用 AXI-S 主机 VC 把 `0x7A` 推给 DUT，再用 VUnit `uart_slave` VC 把 `Uart_Tx` 上的串行帧读回并比对：

   [olo_intf_uart_tb.vhd:144-148](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_uart/olo_intf_uart_tb.vhd#L144-L148) —— `TxSingle`：发送单字节并校验串行帧。

3. 手画 `0x7A`（`0111_1010`）在 `Parity_g => "odd"`、`StopBits_g => "1"` 时的 `Uart_Tx` 波形（含起始位 0、8 个数据位 LSB 先、odd 校验位、停止位 1）。

**需要观察的现象 / 预期结果**：`TxSingle` 用例通过；手画波形应与仿真里 `Uart_Tx` 的电平逐位吻合。

#### 4.2.5 小练习与答案

**练习 1**：为何 `Data_s` 里用 `r.TxCount mod 2 = 1` 作为移位条件，而不是每个 strobe 都移位？

**答案**：strobe 频率是波特率的 2 倍，每个比特内会出现 2 个 strobe。每个比特电平必须稳定维持满 1 个比特时间（= 2 个 strobe），所以只在第 2 个 strobe（`mod 2 = 1`）才移到下一位。

**练习 2**：握手后立刻把 `Tx_Ready` 拉成 `'0'` 有什么作用？

**答案**：表示模块正在发送、暂不能再接收新数据，构成反压，防止用户在当前帧发完前覆盖移位寄存器；直到 `Stop_s` 结束才重新置 `'1'`。

---

### 4.3 接收通路（RX）

#### 4.3.1 概念说明

RX 通路负责从 `Uart_Rx` 引脚**逐位采样、拼回并行字节**。它是一个 5 状态的 FSM：`Idle_s → Start_s → Data_s → (Parity_s) → Stop_s → Idle_s`。与 TX 不同，RX 侧**没有 `Ready`**——每收到完整一帧，立即在 `Rx_Valid` 拉一个时钟周期，消费者必须当下取走。

由于 `Uart_Rx` 来自外部异步世界，它先被 `olo_intf_sync` 同步到 `Clk` 域（同步后内部信号为 `UartRxInt`），再做协议解析。

#### 4.3.2 核心流程

```text
Idle_s:   空闲，等待下降沿。检测到 UartRxInt='0' → 拉一次 RxSync（对齐采样节拍），进 Start_s
   ↓
Start_s:  用 2 倍过采样数 strobe；这 1.5 个比特时间后正好落到第 1 个数据位的中点
          期间若发现线路已回高 → 判为毛刺，回 Idle_s
          数到边界 → 进 Data_s
   ↓
Data_s:   每隔 1 个 strobe（中心点）采样 1 位，左移进移位寄存器
          采满 DataBits_g 位 → 进 Parity_s（有校验）或 Stop_s（无校验）
   ↓
Parity_s: 在校验位中心比对 expected/actual，不匹配则置 Rx_ParityError
   ↓
Stop_s:   检测到停止位（高）即可，仅需半比特即离开 → Rx_Valid 拉一拍，回 Idle_s
```

**中心采样是怎么做到的？** 起始位的下降沿发生在「起始位的起点」。`Start_s` 状态消耗 3 个 strobe（= 1.5 个比特）才进入 `Data_s`，于是 `Data_s` 的第 0 个 strobe 恰好落在**第一个数据位的正中央**；此后每 2 个 strobe 推进 1 位，采样点始终落在后续每一数据位的中点。这就是 2 倍过采样的价值——天然实现中点采样，最大化对波特率偏差的容忍度。

**毛刺过滤**：在 `Start_s` 计数期间，若在某 strobe 发现 `UartRxInt` 已经是高（说明刚才那个下降沿是个短毛刺，并非真正的起始位），FSM 立刻回到 `Idle_s`，不会误触发一帧接收。

**停止位的处理**：RX 不严格校验停止位电平，只等半比特就结束并置 `Rx_Valid`。这样即使发送方停止位偏短或波特率有偏差，也能正常完成接收。

#### 4.3.3 源码精读

`Idle_s`：检测到起始位下降沿，拉 `RxSync` 对齐节拍：

[olo_intf_uart.vhd:237-244](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L237-L244) —— 起始位检测，触发节拍同步。

`Start_s`：数到边界进 `Data_s`；中途线路回高则判毛刺回退：

[olo_intf_uart.vhd:246-260](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L246-L260) —— 1.5 比特延迟 + 毛刺过滤。

`Data_s`：在 `r.RxCount mod 2 = 0`（中心点）时把 `UartRxInt` 移入寄存器高位；采满后按是否有校验分支：

[olo_intf_uart.vhd:262-279](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L262-L279) —— 中心采样与左移拼装。

`Parity_s`：在校验位中心比对，不匹配置 `Rx_ParityError`：

[olo_intf_uart.vhd:281-293](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L281-L293) —— 校验位检查。

`Stop_s`：半比特即结束并拉 `Rx_Valid`（注释明确「停止位不检查」）：

[olo_intf_uart.vhd:295-300](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L295-L300) —— 仅需半比特即完成接收。

`Uart_Rx` 的内部同步——用 `olo_intf_sync` 把外部信号过同步器，注意 `RstLevel_g => '1'`（UART 空闲电平为高，复位后保持高）：

[olo_intf_uart.vhd:369-378](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L369-L378) —— RX 线内部同步。

#### 4.3.4 代码实践

**实践目标**：理解校验错误检测与毛刺过滤两类边界用例。

**操作步骤**：

1. 阅读测试台 `ParityError` 用例：它故意把校验位翻转后发给 DUT，期望 `Rx_ParityError`（映射成 AXI-S 的 `tuser`）为 `'1'`，且数据仍正常送出；再发一个正确帧，`tuser` 应为 `'0'`：

   [olo_intf_uart_tb.vhd:195-216](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_uart/olo_intf_uart_tb.vhd#L195-L216) —— 注入校验错误并检查 `tuser`。

2. 阅读 `RxSpike` 用例：通过 `Uart_RxPull` 制造一个 0.2 比特时间的短负脉冲毛刺，随后再发正常数据，验证接收仍正确（毛刺被过滤）：

   [olo_intf_uart_tb.vhd:218-231](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_uart/olo_intf_uart_tb.vhd#L218-L231) —— 毛刺注入测试。

3. 运行：`python3 run.py --ghdl -p olo_intf_uart_tb.ParityError`（**待本地验证**）。

**需要观察的现象 / 预期结果**：`ParityError` 通过（坏帧 `tuser=1`、好帧 `tuser=0`）；`RxSpike` 通过（毛刺未触发误接收，后续数据正常）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Start_s` 要消耗 1.5 个比特时间才进入 `Data_s`，而不是 1 个比特？

**答案**：起始位下降沿在起始位**起点**。多等 0.5 个比特，使 `Data_s` 的首个采样点落在第一个数据位的**正中央**；之后每 2 个 strobe（=1 比特）采一位，所有数据位都在中点被采样，裕量最大。

**练习 2**：`Rx_ParityError` 拉起后，`Rx_Data` 还会输出吗？

**答案**：会。校验错误只置标志位，不丢弃数据——数据位已正确接收并移入寄存器，模块仍照常在 `Stop_s` 输出 `Rx_Data` 与 `Rx_Valid`，由消费者根据 `Rx_ParityError` 决定是否丢弃该帧。

---

### 4.4 AXI-S 数据接口

#### 4.4.1 概念说明

`olo_intf_uart` 的用户侧数据接口有意设计成 **AXI-S 风格**，让 UART 能像普通流式数据源/汇一样被串接到 Open Logic 的数据通路里。但它对收发两侧采用了**非对称的握手**：

| 方向 | 握手信号 | 是否支持反压 |
| :--- | :--- | :--- |
| TX（用户→模块） | `Tx_Valid` / `Tx_Ready` | 是，模块忙时拉低 `Tx_Ready` |
| RX（模块→用户） | 仅 `Rx_Valid` | **否**，无 `Rx_Ready`，消费者必须立刻收 |

RX 侧不设 `Ready` 的原因是：UART 一旦开始接收就无法让发送方停下来（异步、无流控），所以模块内部也不缓冲多帧，收完一帧就立即输出；消费者若来不及处理就会丢数据。文档明确写出这一点：*There is no Ready signal — hence the consumer must be able to accept data immediately.*

#### 4.4.2 核心流程

- **TX**：用户在 `Tx_Valid` 上给数据，等 `Tx_Ready` 为高即完成一次握手，模块内部排队发送；发送期间 `Tx_Ready` 为低（反压）。
- **RX**：每收完一帧，`Rx_Valid` 拉高**一个时钟周期**，此时 `Rx_Data` 上是本帧数据、`Rx_ParityError` 指示校验状态；消费者必须在这个周期取走。

此外，物理引脚 `Uart_Tx`/`Uart_Rx` 是单比特：`Uart_Tx` 由 FSM 直接驱动；`Uart_Rx` 在内部先经同步器（`UartRxInt`）再被 FSM 使用。

#### 4.4.3 源码精读

端口声明清晰呈现了非对称握手——TX 有 `Tx_Ready`，RX 只有 `Rx_Valid`：

[olo_intf_uart.vhd:45-52](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L45-L52) —— TX 三件套含 `Ready`，RX 仅有 `Valid`/`Data`/`ParityError`。

`Rx_Valid` 是组合进程里的默认值 `'0'`，仅在 `Stop_s` 被置 `'1'` 一拍——典型的「单周期脉冲」式 AXI-S Valid：

[olo_intf_uart.vhd:232-233](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L232-L233) 与 [olo_intf_uart.vhd:295-300](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L295-L300) —— `Rx_Valid` 默认 0，仅 `Stop_s` 拉一拍。

输出赋值（注意 `Rx_Data` 直接来自移位寄存器 `r.RxShiftReg`，复位不清零）：

[olo_intf_uart.vhd:316-320](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd#L316-L320) —— 输出映射。

测试台里 RX 侧用 AXI-S **slave** VC 接收，并把 `Rx_ParityError` 接到 `tuser(0)`，正是利用了「无 Ready、单拍 Valid」的特性：

[olo_intf_uart_tb.vhd:307-316](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_uart/olo_intf_uart_tb.vhd#L307-L316) —— RX slave VC，`tuser` 承载校验错误。

#### 4.4.4 代码实践

**实践目标**：体会「RX 无反压」带来的设计约束。

**操作步骤**：

1. 假设你要把 `olo_intf_uart` 的 RX 接到一个处理较慢、带 AXI-S `Ready` 的下游模块。下游某段时间 `Ready=0`，而此时 UART 仍在按波特率逐帧到达。
2. 思考：会发生什么？该如何补救？
3. （阅读型）打开 `olo_base_fifo_sync`（u2-l4）或 `olo_base_flowctrl_handler`（u5-l4）的文档，确认它们能否作为「RX 之后的小缓冲」吸收短时反压。

**需要观察的现象 / 预期结果**：由于 RX 无 `Ready`，下游来不及收的那一帧会被**直接丢弃**（`Rx_Valid` 是单拍脉冲，不等待）。补救方法是在 `olo_intf_uart` 的 RX 输出后立即接一个深度足够的同步 FIFO（如 `olo_base_fifo_sync`），把「无反压的 UART」适配成「带反压的 AXI-S 流」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 TX 侧可以提供 `Ready` 反压，RX 侧却不提供？

**答案**：TX 由本地时钟驱动，模块可以随时暂停从用户处取数（拉低 `Tx_Ready`），数据仍在用户手里不会丢；RX 则受制于远端发送方的波特率，模块无法让对方停下，所以无法反压，只能要求消费者立即接收，或由用户自行加 FIFO 缓冲。

**练习 2**：`Uart_Rx` 的默认值为何是 `'1'`？内部同步器的 `RstLevel_g` 为何也设 `'1'`？

**答案**：UART 线空闲电平为高。`Uart_Rx` 默认 `'1'`（未连接时视为空闲，不会误触发起始位）；同步器 `RstLevel_g => '1'` 保证复位期间内部信号也是高，避免复位一释放就误判出一个假的起始位下降沿。

---

## 5. 综合实践：UART 回环（loopback）

把本讲四个模块串起来的最佳方式，是搭一个**回环**：把 `olo_intf_uart` 的 `Uart_Tx` 直接连到自己的 `Uart_Rx`，发送一串字符，验证接收端逐字节还原一致。

### 5.1 设计目标

实例化一个 `olo_intf_uart`，配置常用参数（如 `ClkFreq_g => 100.0e6`、`BaudRate_g => 115200.0`、`DataBits_g => 8`、`Parity_g => "even"`），把 `Uart_Tx` 接到 `Uart_Rx` 形成回环，依次发送字节串 `"Hi"`（`0x48`、`0x69`），在 RX 侧收集并比对。

### 5.2 操作步骤

1. **新建一个最小测试台**（示例代码，非项目原有文件）`olo_intf_uart_loopback_tb.vhd`，骨架如下：

   ```vhdl
   -- 示例代码：仅供学习，非仓库原有文件
   constant ClkFreq_c : real := 100.0e6;
   signal Uart_Loop : std_logic;  -- 回环连线

   i_dut : entity olo.olo_intf_uart
       generic map (
           ClkFreq_g  => ClkFreq_c,
           BaudRate_g => 115200.0,
           DataBits_g => 8,
           Parity_g   => "even"      -- TX/RX 必须一致
       )
       port map (
           Clk   => Clk,  Rst  => Rst,
           -- 用户 TX 侧（AXI-S master 驱动）
           Tx_Valid => Tx_Valid, Tx_Ready => Tx_Ready, Tx_Data => Tx_Data,
           -- 用户 RX 侧（无 Ready，立即采样）
           Rx_Valid => Rx_Valid, Rx_Data => Rx_Data, Rx_ParityError => Rx_ParityError,
           -- 回环
           Uart_Tx => Uart_Loop, Uart_Rx => Uart_Loop
       );
   ```

2. 用一个简单进程当 TX 源：握手套接发送 `0x48`，等 `Tx_Ready` 重新拉高后再发 `0x69`。

3. 用另一个进程当 RX 汇：每当 `Rx_Valid='1'`，把 `Rx_Data` 存入数组，并检查 `Rx_ParityError='0'`。

4. 发完后比对 RX 收集数组是否等于 `[0x48, 0x69]`。

### 5.3 需要观察的现象 / 预期结果

- `Uart_Loop` 上应依次出现两帧完整 UART 波形（起始位 0 → 8 数据位 LSB 先 → even 校验位 → 停止位 1）。
- RX 侧应在每帧停止位的半比特后各拉一次 `Rx_Valid`。
- 收集到的两字节正好是 `0x48`、`0x69`，`Rx_ParityError` 全程为 `'0'`。
- 由于回环 TX 与 RX 共用同一 `Clk`、同一波特率泛型，且校验设置一致，应当无校验错误。

> 待本地验证：本实践需自行编写并编译测试台。若想先跑现成用例，可直接运行 `olo_intf_uart_tb`（见 4.2.4 / 4.3.4），它用 VUnit 的 `uart_master`/`uart_slave` VC 分别独立测试收发，已覆盖等价的检查。

### 5.4 进阶思考

- 把 `Parity_g` 在 TX 侧故意改成与 RX 侧不一致会怎样？（提示：`Rx_ParityError` 会持续拉高。）
- 把 RX 输出接到 `olo_base_fifo_sync` 之后再处理，能否解决「RX 无反压」的问题？

---

## 6. 本讲小结

- `olo_intf_uart` 把 UART 的波特率与帧格式（数据位/停止位/校验）全部做成了**编译期泛型**，字符串枚举（`StopBits_g`/`Parity_g`）的合法性由 `assert` 断言把关。
- 核心设计是 **2 倍波特率过采样**：两个 `olo_base_strobe_gen`（小数模式）产生 \(2\times\) 波特率的节拍，使每个比特跨 2 个 strobe。
- 这带来两大好处——**接收端在每一位中心采样**（`Start_s` 先消耗 1.5 比特对齐到中点）与**起始位毛刺过滤**。
- TX 是 4 状态 FSM，移位寄存器按 `parityBit & Tx_Data & '0'` 拼装、LSB 先发；TX 侧提供完整 **Valid/Ready 反压**。
- RX 是 5 状态 FSM，`Uart_Rx` 先经 `olo_intf_sync` 同步（`RstLevel_g=>'1'`），RX 侧**只有 `Rx_Valid`、没有 `Ready`**，消费者必须立即接收，可后接 FIFO 补反压。
- 校验由收发两侧各自的 `parityBit` 函数独立计算/核对，不匹配时拉 `Rx_ParityError`（但不丢数据）。

---

## 7. 下一步学习建议

- **SPI**：继续 intf 区，学习 `olo_intf_spi_master`/`olo_intf_spi_slave`（u7-l3），对比 UART 的「异步无时钟」与 SPI 的「同步带时钟、主从全双工」。
- **I2C**：学习 `olo_intf_i2c_master`（u7-l4），理解多主仲裁与时钟拉伸等更复杂的总线机制。
- **流控补救**：若你想在工程里给 UART RX 加缓冲，回顾 `olo_base_fifo_sync`（u2-l4）与 `olo_base_flowctrl_handler`（u5-l4）。
- **节拍生成器深入**：本讲把 `olo_base_strobe_gen` 当黑盒用了，其小数分频与 `In_Sync` 相位对齐的细节见 u5-l1。
