# UART 监视器与用户逻辑

## 1. 本讲目标

本讲聚焦在 FPGA-FOC 工程里两段「不属于 FOC 算法本身、但让整个示例跑起来、看得见」的代码：

- `uart_monitor.v`：一个 115200,8,n,1 的 UART **发送器**，把 4 个 16 位有符号数转成十进制文本，通过串口一行一行打印出来，用于在 PC 上观察电流环的跟随曲线。
- `fpga_top.v` 里的**用户逻辑**：一个 24 位自增计数器 `cnt`，让目标电流 `iq_aim` 在 +200 / −200 之间周期性切换，演示电机扭矩「一会顺时针、一会逆时针」。

学完本讲你应该能够：

1. 算出给定时钟和 `CLK_DIV` 下的 UART 波特率，并解释 8n1 帧的位结构。
2. 看懂 `uart_monitor` 内部 `IDLE/SELECT/WAIT/PARSING/SENDING` 五状态机如何把 4 个数值「逐值转字符串、逐字节发出去」。
3. 读懂 `itoa`（整数转字符串）如何用「反复除以 10 取余数」的方式，把 16 位有符号数变成右对齐、带符号、去前导零的 6 字符十进制串。
4. 理解顶层 `cnt[23]` 如何决定 `iq_aim` 的换向周期，并能动手改成别的换向方式或别的被监视变量。

## 2. 前置知识

在进入源码前，先用三段大白话把背景补齐。

**（1）UART 串口到底是什么。** UART 是异步串行通信：只用一根 TX 线（发送）和一根 RX 线（接收），没有时钟线。收发双方事先约好一个「位速率」（波特率，比如 115200 bit/s），发送方把每个字节拆成一串 0/1 电平，按这个速率一位一位往外推；接收方按同样的速率逐位采样拼回字节。最常用的 8n1 格式规定：线路空闲时为高电平；每个字节前面加 1 位起始位（低电平），随后 8 位数据（**最低位先发**），最后 1 位停止位（高电平）。所以一个字节在物理线路上占 \(1+8+1=10\) 个位周期。

**（2）有符号数与十进制文本是两回事。** 在 Verilog 里 `wire signed [15:0] iq` 是一个 16 位补码整数，例如 `16'hFF38` 表示十进制的 −200。但串口助手里你想看到的是字符 `'-'`、`'2'`、`'0'`、`'0'` 这 4 个 ASCII 字节（`0x2D 0x32 0x30 0x30`）。把补码整数变成人能读的十进制字符的过程叫 **itoa**（integer to ASCII），本讲的 `uart_monitor` 自己用硬件实现了它。

**（3）本库统一的脉冲握手约定。** 回顾 [u2-l1](u2-l1-foc-top-overview.md)：模块之间用 `i_en`/`o_en` 这类「单时钟周期高电平脉冲」表示「数据有效/节拍到了」。`foc_top` 每个控制周期（\(=clk/2048\)，约 18kHz / 55.6µs）会在 `en_idq` 上打一个脉冲，表示「`id`/`iq` 又更新了一次」。`uart_monitor` 就把这个脉冲当作「该打印一行了」的触发信号。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [RTL/uart_monitor.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v) | 黄色区域「用户逻辑」里的串口监视器（可综合，纯 Verilog） | 波特率、8n1 帧、itoa、五状态机 |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | 工程顶层 | 例化 `uart_monitor` 的连线；`cnt` 计数器与 `iq_aim` 换向 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | 蓝色 FOC 核心 | 只看它**对外暴露**了哪些信号（决定我们能监视什么） |

> 提示：`uart_monitor` 属于「黄色 = 用户自定义逻辑」，和 FOC 核心算法完全解耦——就算你把整个 FOC 换成别的算法，只要仍给出 `en_idq` 脉冲和几个 16 位有符号数，这个监视器原样可用。

---

## 4. 核心概念与源码讲解

### 4.1 uart_monitor 模块总览：职责、端口与触发约定

#### 4.1.1 概念说明

`uart_monitor` 是一个「数值 → 串口文本」的桥。它接受 4 个 16 位有符号输入 `i_val0..i_val3`，每当 `i_en` 上来了一个脉冲，就把这 4 个数依次转成十进制字符串，用空格隔开放在一行里，通过 `o_uart_tx` 发出去，行尾换行。它不接收任何数据（没有 RX），是一个**纯发送**监视器。

#### 4.1.2 核心流程

整体可以看成三层叠加：

```
i_en 脉冲 ──▶ [上层 FSM] ──▶ 依次取出 4 个值
                              │
                              ▼
                       [itoa] 把当前值转成 6 字符十进制串
                              │
                              ▼
                       [TX 引擎] 把 8 个字节(6字符+空格+分隔符)逐字节
                                 按 8n1 / 115200 发到 o_uart_tx
```

每次 `i_en` 触发后，FSM 把 4 个值轮一遍，每个值产生 8 个字节，共 \(4 \times 8 = 32\) 字节，构成一行输出，例如：

```
   12     0  -157   200
```

（依次是 `id`、`id_aim`、`iq`、`iq_aim`，行尾换行。）

#### 4.1.3 源码精读

模块端口与波特率参数见 [RTL/uart_monitor.v:L9-L20](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L9-L20)，其中 `CLK_DIV` 的注释给出波特率公式：

> 波特率 \(= f_{clk} / CLK\_DIV\)。若 \(f_{clk}=36.864\,\text{MHz}\)，`CLK_DIV=320`，则波特率 \(=36.864\text{M}/320=115200\)。

注意代码里 `parameter CLK_DIV = 217` 是默认值（对应约 25MHz 时钟），实际使用时被 `fpga_top` 覆盖成 320，见 [RTL/fpga_top.v:L159-L161](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L159-L161)。`i_en` 接到 `foc_top` 的 `en_idq`，4 个数值分别接 `id / id_aim / iq / iq_aim`：

- [RTL/fpga_top.v:L164-L168](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L164-L168)：`i_en(en_idq)`、`i_val0(id)`、`i_val1(id_aim)`、`i_val2(iq)`、`i_val3(iq_aim)`。

也就是说：**每个控制周期 `en_idq` 一脉冲，就触发打印一行当前的 d/q 轴实际电流与目标电流。**

#### 4.1.4 代码实践

**实践目标**：在真实源码里确认「谁触发打印、打印哪 4 个量」。

**操作步骤**：

1. 打开 `RTL/fpga_top.v`，定位 `u_uart_monitor` 的例化（约 159 行起）。
2. 找到 `.i_en(...)`、`.i_val0..3(...)` 各自连到的 wire。
3. 顺着这些 wire 往上找它们的来源（`en_idq` 来自 `u_foc_top`，`id/iq` 也来自 `u_foc_top`，`id_aim/iq_aim` 来自本文件的用户逻辑）。

**需要观察的现象 / 预期结果**：你会得到一条完整的「数据通路」：`foc_top` 算出 `id/iq` 并发 `en_idq` 脉冲 → `uart_monitor` 把它们连同 `id_aim/iq_aim` 一起发到 PC。这印证了「监视器与 FOC 核心之间唯一的耦合就是 `en_idq` 脉冲 + 4 个 16 位数」。

#### 4.1.5 小练习与答案

**Q1**：如果把时钟改成 50MHz，仍想要 115200 波特率，`CLK_DIV` 该填多少？
**A1**：\(CLK\_DIV = f_{clk}/\text{波特率} = 50\,000\,000 / 115200 \approx 434\)，填 434（误差约 0.1%，UART 容忍）。

**Q2**：`uart_monitor` 能用来接收 PC 发来的命令吗？
**A2**：不能。它只有 `o_uart_tx` 输出，没有 RX 通路。要接收命令需要另写一个 UART 接收器（见第 5 节综合实践）。

---

### 4.2 UART 物理发送层：CLK_DIV 波特率与 8n1 帧

#### 4.2.1 概念说明

这一层负责「把一个字节变成串行电平」。它要解决两个问题：(a) 每一位电平持续多久（由 `CLK_DIV` 分频决定）；(b) 起始位、8 位数据、停止位的排布（8n1 帧）。

#### 4.2.2 核心流程

```
tx_en=1, tx_data=字节  ──▶  装载移位寄存器 tx_shift，tx_cnt=12
                              │
                              ▼  每经过 CLK_DIV 个 clk，tx_cnt 减 1
                         按 tx_cnt 索引从 tx_shift 取一位送到 o_uart_tx
                              │
                              ▼  tx_cnt 减到 0，tx_rdy 拉高，表示「发完一个字节」
```

位周期（每个电平位持续几个时钟）为：

\[
T_{\text{bit}} = CLK\_DIV \quad \text{（个 clk 周期）}
\]

对应波特率：

\[
\text{baud} = \frac{f_{clk}}{CLK\_DIV}
\]

#### 4.2.3 源码精读

TX 引擎的寄存器与就绪信号见 [RTL/uart_monitor.v:L170-L174](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L170-L174)，其中 `tx_rdy = (tx_cnt==0)` 用来告诉上层 FSM「我空闲，可以喂下一个字节」。核心发送逻辑在 [RTL/uart_monitor.v:L176-L199](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L176-L199)：

- `ccnt` 是位内计数器，从 0 数到 `CLK_DIV-1`，数满就把 `tx_cnt` 减 1（[L192-L197](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L192-L197)）。
- 当 `tx_cnt==0`（空闲）且 `tx_en` 有效时，装载一帧（[L186-L189](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L186-L189)）：

```verilog
tx_cnt  <= 4'd12;
tx_shift <= {2'b10, tx_data[0], tx_data[1], tx_data[2], tx_data[3],
             tx_data[4], tx_data[5], tx_data[6], tx_data[7], 2'b11};
```

这里的 `{2'b10, 8位数据(LSB在前), 2'b11}` 是一个非常巧的写法。`tx_shift` 按 `tx_cnt` 从 12 递减到 1 逐位输出，排布如下：

| `tx_cnt` | 取值 | 含义 |
|---|---|---|
| 12 | 1 | 线路空闲（前导高电平） |
| 11 | 0 | **起始位** |
| 10..3 | `tx_data[0..7]` | 8 位数据，**最低位先发** |
| 2 | 1 | 停止位 |
| 1 | 1 | 帧间空闲（额外的高电平） |

所以每个字节占 12 个位周期（比标准 10 多出首尾各一位空闲，等价于在标准 8n1 帧前后多留了空闲时间，对任何标准 UART 接收方完全兼容）。`tx_data` 在拼装时写成 `tx_data[0], tx_data[1], ...` 而不是 `tx_data[7:0]`，正是为了把 LSB 放在高索引位、让 `tx_cnt` 递减时「先输出 LSB」，符合 UART「低位先发」约定。

#### 4.2.4 代码实践

**实践目标**：理解波特率与每帧耗时的关系。

**操作步骤**：

1. 在 `fpga_top.v` 里把 `CLK_DIV` 从 320 临时改成 640（即 [L160](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L160)）。
2. 重新综合烧录后，把 PC 串口助手的波特率改成 57600（\(=36.864\text{M}/640\)）再打开。

**需要观察的现象 / 预期结果**：只有在「FPGA 实际波特率」与「PC 设定波特率」一致时才能看到正常文本；二者不匹配时（比如忘了改 PC 端）会看到乱码。这验证了 `baud = f_clk / CLK_DIV`。**待本地验证**（本仓库未提供该工程的仿真 testbench，需在真实板卡或自行编写 testbench 时观察）。

#### 4.2.5 小练习与答案

**Q1**：为什么作者把数据位拼成 `tx_data[0], tx_data[1], ..., tx_data[7]` 而不是直接 `tx_data`？
**A1**：因为 `tx_cnt` 是**递减**索引。把 LSB（`tx_data[0]`）放在最高索引位（`tx_shift[10]`，对应较大的 `tx_cnt=10` 先输出），才能保证「低位先发」，符合 UART 规范。

**Q2**：一个字节实际占 12 个位周期，相比标准 10 位周期，吞吐损失多少？
**A2**：有效吞吐 \(= 10/12 \approx 83.3\%\)。在 115200 下，每秒最多发 \(115200/12 = 9600\) 字节。

---

### 4.3 itoa：16 位有符号数转十进制字符串

#### 4.3.1 概念说明

itoa（integer to ASCII）解决「补码 → 人能读的十进制字符」问题。难点有两个：(a) 处理负号；(b) 去掉前导零（把 `  -00200` 显示成 `  -200`）。`uart_monitor` 用一个 ~8 拍的小状态机，靠「反复除以 10 取余数」逐位求出十进制数字。

#### 4.3.2 核心流程

```
拍0: 锁存 sign = i_val[15], abs = |i_val|
拍1..7:
   rem  = abs % 10      // 当前个位
   abs  = abs / 10      // 剩余高位
   把 rem 变成 ASCII ('0'+rem)，压入 str[0]，原内容右移
   若 abs 已为 0（没有更高位了）：剩余高位填空格或负号
输出: itoa_str[0..5] 是 6 字符、右对齐、去前导零的十进制串
```

6 个字符刚好够装下 16 位有符号数的极限情况：`-32768`（5 位数字 + 1 个负号 = 6 字符）。

#### 4.3.3 源码精读

itoa 相关寄存器见 [RTL/uart_monitor.v:L122-L127](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L122-L127)，其中：

```verilog
wire[15:0] itoa_rem_w = (itoa_abs % 16'd10);   // 当前个位（组合逻辑求余）
reg  [ 3:0] itoa_rem;
```

主逻辑在 [RTL/uart_monitor.v:L130-L167](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L130-L167)。

**符号与绝对值**（[L142-L146](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L142-L146)）：在 `itoa_cnt==0` 且收到 `itoa_en` 时，锁存符号位和绝对值：

```verilog
itoa_sign <= itoa_val[15];
itoa_abs  <= itoa_val[15] ? $unsigned(-itoa_val) : $unsigned(itoa_val);
```

**逐位提取**（[L148-L164](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L148-L164)）：之后每拍做 `itoa_abs <= itoa_abs / 10`（扔掉刚刚处理过的个位），并把这个个位 `itoa_rem` 转成 ASCII 写进 `itoa_str[0]`，同时整个缓冲右移一位：

```verilog
itoa_str[5] <= itoa_str[4];   // 整体右移
...
itoa_str[1] <= itoa_str[0];
if(itoa_cnt>3'd2 && itoa_zero) begin                  // 高位已无有效数字
    itoa_str[0] <= itoa_sign ? 8'h2D : 8'h20;          // 填 '-'(0x2D) 或 空格(0x20)
    itoa_sign <= 1'b0;                                  // 负号只插一次
end else begin
    itoa_str[0] <= {4'h3, itoa_rem};                   // '0'+rem = 0x30+rem
end
```

要点：

- `{4'h3, itoa_rem}` 就是 `0x30 + rem`，即 ASCII `'0'..'9'`。
- `itoa_zero = (itoa_abs==0)` 表示「更高位已经全是 0」，此时不再写 `'0'`，而是写空格（正数）或一次性写一个 `'-'`（负数），实现**去前导零 + 负号**。
- `itoa_oen <= (itoa_cnt==7)`（[L166](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L166)）在第 7 拍拉高一个脉冲，告诉上层 FSM「字符串准备好了」。

#### 4.3.4 代码实践

**实践目标**：手算验证 itoa 的输出。

**操作步骤**：对下面 3 个输入，按上面的算法手动模拟 6 字符输出（左补空格、右对齐）。

1. `i_val = +200`
2. `i_val = -123`
3. `i_val = 0`

**预期结果**：

| 输入 | `itoa_str[0..5]`（每格一个 ASCII） | 显示效果 |
|---|---|---|
| `+200` | `' ',' ',' ','2','0','0'` | `   200` |
| `-123` | `' ',' ','-','1','2','3'` | `  -123` |
| `0` | `' ',' ',' ',' ',' ','0'` | `     0` |

（负号紧贴最高有效数字左侧，其余高位为空格。）

#### 4.3.5 小练习与答案

**Q1**：为什么缓冲区是 6 字符？5 字符行不行？
**A1**：16 位有符号数范围是 \(-32768 \sim +32767\)，最坏情况 `-32768` 需要 5 位数字 + 1 个负号 = 6 字符。5 字符装不下。

**Q2**：如果把 `itoa_sign <= 1'b0;` 这一行删掉，负数会显示成什么样？
**A2**：负号会被重复插入到每一个去前导零的高位，例如 `-123` 可能显示成 `---123`（每个高位都插一个 `-`），因为 `itoa_sign` 没被清零、一直为真。

---

### 4.4 上层 FSM：四值循环发送状态机

#### 4.4.1 概念说明

itoa 一次只能转一个数，TX 引擎一次只能发一个字节。要把 4 个数全发出去，需要一个「调度员」：依次选第 0..3 个值 → 让 itoa 转换 → 让 TX 把这 8 个字节逐个发出去 → 换下一个值。这就是 `uart_monitor` 顶层那个 5 状态状态机干的活。

#### 4.4.2 核心流程

```
IDLE    ──i_en脉冲──▶ SELECT(选下一个值, vcnt++)
SELECT  ──▶ WAIT(给 itoa_en 一拍) ──▶ PARSING(等 itoa_oen)
PARSING ──itoa_oen──▶ SENDING
SENDING ──逐字节发 8 个字符──▶ cnt==7 时回到 SELECT
                             4 个值全发完(vcnt 轮回到 0) ──▶ IDLE
```

每个值产出 8 个字节：`itoa_str[0..5]`（6 字符）+ 空格（`0x20`，固定）+ 行内分隔符 `eov`。`eov` 对前 3 个值是空格，对第 4 个值（`i_val3` = `iq_aim`）是换行 `\n`（`0x0A`），见 [RTL/uart_monitor.v:L46-L53](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L46-L53)。于是 PC 端每收完一行自动换行。

#### 4.4.3 源码精读

状态定义见 [RTL/uart_monitor.v:L24-L28](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L24-L28)。组合逻辑把「SENDING 状态下当前要发的字节」喂给 TX 引擎（[L55-L62](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L55-L62)）：

```verilog
if(stat==SENDING) begin
    tx_en   = 1'b1;
    tx_data = s_str[cnt];   // cnt: 0..7 指向当前字节
end
```

主状态机在 [RTL/uart_monitor.v:L64-L119](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L64-L119)，要点：

- **IDLE 等触发**（[L75-L76](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L75-L76)）：`IDLE: if(i_en) stat <= SELECT;`。注意只有在 IDLE 才会响应 `i_en`，发送过程中进来的 `i_en` 脉冲会被丢弃（见下文「欠采样」）。
- **SELECT 选值**（[L77-L107](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L77-L107)）：按 `vcnt`（0..3）把 `i_val0..3` 之一锁进 `itoa_val` 并拉一拍 `itoa_en`；前 3 个值 `eov <= 8'h20`（空格），第 4 个值 `eov <= 8'h0A`（换行，[L96-L101](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L96-L101)）；`vcnt` 到 4 后清零回 IDLE（[L102-L106](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L102-L106)）。
- **PARSING 等 itoa**（[L110-L111](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L110-L111)）：`if(itoa_oen) stat <= SENDING;`——握手。
- **SENDING 逐字节**（[L112-L117](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/uart_monitor.v#L112-L117)）：每当 TX 引擎 `tx_rdy`（发完一个字节），`cnt` 加 1；`cnt==7` 时这一值的 8 字节发完，回 SELECT 取下一个值。

**一个重要后果——欠采样**：一帧 32 字节、每字节 12 个位周期，所以一帧耗时

\[
T_{\text{frame}} = 32 \times 12 \times T_{\text{bit}} = 384 \times \frac{CLK\_DIV}{f_{clk}} = \frac{384\times 320}{36.864\text{M}} \approx 3.33\,\text{ms}
\]

而 `en_idq` 每 \(\approx 55.6\,\mu\text{s}\) 就来一个脉冲。也就是说，发送一帧期间会错过约 \(3.33\text{ms}/55.6\mu\text{s} \approx 60\) 个脉冲，FSM 回到 IDLE 后要等「下一个」`en_idq` 才再次触发。所以串口看到的曲线**采样率约 \(1/3.4\text{ms} \approx 300\) 帧/秒**，远低于控制频率 18kHz。对缓慢换向的扭矩轨迹而言足够了。

#### 4.4.4 代码实践

**实践目标**：算出一帧耗时与有效采样率，理解为什么监视器会「跳过」很多控制周期。

**操作步骤**：

1. 按 \(T_{\text{frame}} = 32 \times 12 \times CLK\_DIV / f_{clk}\) 代入 `CLK_DIV=320`、`f_clk=36.864MHz`，算出一帧时间。
2. 用控制周期 \(T_{ctrl}=2048/f_{clk}\approx 55.6\mu\text{s}\) 求出每帧错过了多少个 `en_idq` 脉冲。

**预期结果**：\(T_{\text{frame}}\approx 3.33\,\text{ms}\)，每帧错过约 60 个脉冲，实际采样率约 300 帧/秒。

#### 4.4.5 小练习与答案

**Q1**：`eov` 这个信号是干什么用的？为什么第 4 个值要单独用 `0x0A`？
**A1**：`eov` 是「每个数值后面的分隔符」。前 3 个值后用空格，第 4 个值（一行的最后一个）后用 `\n`（0x0A）换行，让 PC 端每收到一组 4 个数就自动另起一行。

**Q2**：如果想让一行只打印 2 个数（比如只看 `iq` 和 `iq_aim`），需要改哪里？
**A2**：把 `vcnt` 的上限从 3 改成 1（让 FSM 在 `vcnt==1` 发完后就回到 IDLE），并把换行符 `0x0A` 改到第 2 个值（`vcnt==1`）上。同时 `fpga_top` 里把 `i_val0/i_val1` 接成你想看的两个量。

---

### 4.5 fpga_top 用户逻辑：扭矩顺逆换向演示

#### 4.5.1 概念说明

FOC 电流环本身只是「让 `id`/`iq` 跟随 `id_aim`/`iq_aim`」。**目标电流从哪来**是「上一级」的事（速度环、位置环，或本讲这种演示逻辑）。`fpga_top` 为了让示例能直观演示，自己造了一个最简单的「目标发生器」：让 `iq_aim` 在正负之间周期性跳变，于是电机扭矩就会一会正、一会负，肉眼看到电机来回摆。

回顾：q 轴电流 `iq` 代表电磁扭矩（正负对应转向），d 轴电流 `id` 在不弱磁时压到 0。所以「控扭矩」=「控 `iq_aim`」，`id_aim` 恒 0。

#### 4.5.2 核心流程

```
24 位自增计数器 cnt：每个 clk 加 1，自然在 0 ~ 2^24-1 间循环
cnt[23]（最高位）：前 2^23 拍为 0，后 2^23 拍为 1，周期性方波
   ├─ cnt[23]=0  ─▶  iq_aim = -200  （一个方向的扭矩）
   └─ cnt[23]=1  ─▶  iq_aim = +200  （反方向扭矩）
id_aim 恒 = 0
```

换向周期：

\[
T_{\text{half}} = \frac{2^{23}}{f_{clk}} = \frac{8\,388\,608}{36.864\text{M}} \approx 0.228\,\text{s}
\]

即 `iq_aim` 约 0.23 秒切换一次方向，完整一个「正→负→正」周期约 0.45 秒。

#### 4.5.3 源码精读

24 位自增计数器见 [RTL/fpga_top.v:L136-L141](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L136-L141)：

```verilog
reg [23:0] cnt;
always @ (posedge clk or negedge rstn)
    if(~rstn) cnt <= 24'd0;
    else      cnt <= cnt + 24'd1;
```

`id_aim` 恒 0 与 `iq_aim` 换向见 [RTL/fpga_top.v:L144-L154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L144-L154)：

```verilog
assign id_aim = $signed(16'd0);          // id_aim 恒为 0
always @ (posedge clk or negedge rstn)
    if(~rstn) iq_aim <= $signed(16'd0);
    else
        if(cnt[23]) iq_aim <=  $signed(16'd200);
        else        iq_aim <= -$signed(16'd200);
```

> ⚠️ **一个小提醒：要信代码，不要信注释。** [L151](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L151) 与 [L153](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L153) 行尾注释写的是「令 id_aim = +200 / -200」，但代码赋值的是 `iq_aim`（`id_aim` 在上一行已被恒定为 0）。这是作者复制注释时留下的小笔误，**以代码为准**。读源码时遇到「注释和代码打架」要优先相信代码，这也是一种常见的好习惯。

#### 4.5.4 代码实践

**实践目标**：通过改一行代码，直观改变电机换向频率。

**操作步骤**：

1. 把 [L150](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L150) 的 `if(cnt[23])` 改成 `if(cnt[22])`。
2. 重新综合烧录，观察电机摆动节奏。

**需要观察的现象 / 预期结果**：换向周期减半（约 0.11 秒切一次方向），电机来回摆动明显加快。这印证了「用 `cnt` 的不同位可得到不同周期」：位号每减 1，周期减半。

**待本地验证**（需在真实板卡运行）。

#### 4.5.5 小练习与答案

**Q1**：为什么用 `cnt[23]` 而不是 `cnt[0]`？
**A1**：`cnt[0]` 每个 clk 都翻转，`iq_aim` 会在 MHz 量级疯狂跳变，电流环根本跟不上，电机会发抖甚至失步。`cnt[23]` 给出约 0.23 秒的半周期，足够 PI 跟随，演示效果好。

**Q2**：把目标电流从 ±200 改成 ±400，意味着什么？
**A2**：`iq_aim` 加倍意味着目标扭矩加倍（前提是没超过 `MAX_AMP` 限定的最大电流/电压）。电机会摆得更有力，但若超出 PI 能力或 SVPWM 限幅，`iq` 将跟不上 `iq_aim`（可在串口曲线上看到跟随误差变大）。

---

## 5. 综合实践

本节的两个小任务都是「设计/扩展型」练习——仓库里**没有现成代码**，需要你基于本讲理解去设计。下面的方案是示例参考，标注为「示例方案」。

### 任务一：把 `iq_aim` 的换向改成受外部控制（按键或串口命令）

**目标**：不再用 `cnt[23]` 自动换向，而是由外部决定方向——按下按键切换一次方向，或收到串口命令切换方向。

**示例方案 A（按键控制）——需要新增/修改：**

1. **新增端口**：在 `fpga_top` 的端口列表里加一个 `input wire key`（接板卡按键）。
2. **新增按键消抖 + 边沿检测逻辑**（示例代码，需自行验证）：

   ```verilog
   // 示例代码：按下按键（下降沿）时翻转方向标志 direction
   reg [19:0] debounce_cnt;
   reg        key_r1, key_r2;
   reg        direction;       // 0=正, 1=反
   always @ (posedge clk or negedge rstn)
       if(~rstn) begin
           key_r1 <= 1'b1; key_r2 <= 1'b1; direction <= 1'b0; debounce_cnt <= 0;
       end else begin
           key_r1 <= key_r2;              // 两级打拍同步
           key_r2 <= key;
           // 检测下降沿并切换方向
           if(key_r1 && ~key_r2) direction <= ~direction;
       end
   always @ (posedge clk or negedge rstn)
       if(~rstn) iq_aim <= 16'sd0;
       else      iq_aim <= direction ? 16'sd200 : -16'sd200;
   ```

3. **删除** [L150](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L150) 那段基于 `cnt[23]` 的换向逻辑（`cnt` 若不再使用也可一并删除）。
4. **约束**：按键需接板卡上拉、做消抖（上面用同步+边沿检测近似；实际可用更长的计数器消抖）。**待本地验证**。

**示例方案 B（串口命令控制）——需要新增/修改：**

1. **新增一个 UART 接收器模块**（仓库目前没有 RX 模块，需自行编写或引入），例如 `uart_receiver.v`，输出 `rx_en` 脉冲 + `rx_data[7:0]`。
2. 在 `fpga_top` 里加端口 `input wire uart_rx`，例化接收器。
3. 用一个简单状态机解析命令字节：例如收到 `'+'`（`0x2B`）置 `direction=1`，收到 `'-'`（`0x2D`）置 `direction=0`，再用 `direction` 决定 `iq_aim` 正负。
4. 同样删除原 `cnt[23]` 换向逻辑。

> 这两种方案都**只动 `fpga_top`（黄色用户逻辑）**，蓝色 FOC 核心 `foc_top` 一行都不用改——这正是本工程「核心算法 / 用户逻辑」分层带来的好处。

### 任务二：让 `uart_monitor` 监视别的变量（例如 ψ、Vrρ）

**关键约束（先想清楚再看答案）**：`uart_monitor` 的 4 个输入是普通 16 位有符号 wire，只要把别的 wire 接上去就能换被监视的量。但**能不能接得上，取决于那个信号在不在 `foc_top` 的端口上**。

查 [RTL/foc/foc_top.v:L18-L45](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L18-L45) 的端口列表可知：

- `id`、`iq`、`phi` **是端口**，可以直接接 `uart_monitor`。
- **ψ（`psi`）是 `foc_top` 内部 `reg`**（见 [RTL/foc/foc_top.v:L50](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L50)），**Vrρ（`vr_rho`）也是内部 `wire`**（见 [RTL/foc/foc_top.v:L60](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L60)）。它们**没有对外暴露**，在 `fpga_top` 里根本够不着。

所以分两种情况：

- **监视已是端口的量**（比如换成打印 `phi` 机械角度）：只需在 `fpga_top` 里把 `u_uart_monitor` 的 `.i_val0(phi)` 这样接上即可（注意 `phi` 是 12 位无符号，要 `\{4'h0, phi\}` 零扩展成 16 位有符号再接，否则位宽不匹配）。
- **监视内部量（ψ、Vrρ）**：需要先给 `foc_top` **新增输出端口**，例如加 `output wire [11:0] psi_out` 和 `output wire [11:0] vr_rho_out`，在 `foc_top` 内部 `assign psi_out = psi; assign vr_rho_out = vr_rho;`，然后在 `fpga_top` 里接到 `uart_monitor`。这属于「为调试而扩展核心模块的对外接口」，会改动蓝色区域，做完调试后通常再撤掉。

**操作步骤（以打印 ψ 为例）**：

1. 在 `foc_top` 端口列表加 `output wire [11:0] psi_out`，并 `assign psi_out = psi;`。
2. 在 `fpga_top` 的 `u_foc_top` 例化里加 `.psi_out(psi_wire)`，并声明 `wire [11:0] psi_wire;`。
3. 把 `u_uart_monitor` 的 `.i_val0({4'h0, psi_wire})`（其它三个保持 `id_aim/iq/iq_aim` 或按需调整）。
4. 综合后用串口助手观察 ψ 随时间的变化曲线（电机转动时应呈锯齿状 0~4095 循环）。

**需要观察的现象 / 预期结果**：ψ 是 12 位电角度，电机匀速转动时 ψ 应在 0~4095 之间周期性扫描（每转一个电周期扫一遍，极对数越多机械转一圈扫的遍数越多）。**待本地验证**。

---

## 6. 本讲小结

- `uart_monitor` 是一个 115200,8,n,1 的**纯发送**监视器，把 4 个 16 位有符号数转十进制文本逐行打印，与 FOC 核心的唯一耦合是 `en_idq` 脉冲 + 4 个数据 wire。
- 波特率 \(= f_{clk}/CLK\_DIV\)；`fpga_top` 里 `CLK_DIV=320`、\(f_{clk}=36.864\text{MHz}\) ⇒ 115200。每个字节占 12 个位周期（标准 8n1 帧前后各多一位空闲）。
- **itoa** 用「反复 `%10` 取个位、`/10` 去个位」的方法逐位求出十进制字符，配 `itoa_zero` 实现「右对齐 + 去前导零 + 负号」，6 字符缓冲恰好覆盖 `-32768`。
- 顶层 **IDLE/SELECT/WAIT/PARSING/SENDING** 五状态机调度「逐值转换、逐字节发送」；一帧 32 字节约 3.33ms，导致实际采样率约 300 帧/秒（欠采样）。
- `fpga_top` 用户逻辑用一个 24 位计数器 `cnt`，按 `cnt[23]` 让 `iq_aim` 在 ±200 间约每 0.23 秒切换一次，演示扭矩顺逆换向；`id_aim` 恒 0。
- 这两段代码都属黄色「用户逻辑」，换算法、换被监视量、改换向方式都基本不动蓝色 FOC 核心——分层带来的可改性。

## 7. 下一步学习建议

- 想把「监视」升级成「在线调参」？可以仿照本讲的「串口接收器」思路，写一个能接收 `Kp/Ki` 的 UART RX 模块，把 [fpga_top.v:L114-L115](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L114-L115) 里写死的 `Kp=300000/Ki=30000` 改成运行时可改，体会「电流环调参」的实时性。
- 想把「演示换向」升级成「真正控制转速」？下一步就是给 `iq_aim` 外加一个**速度环**：用 `phi` 的差分估算转速，由转速误差生成 `iq_aim`——这正是 [u4-l4 二次开发与系统扩展](u4-l4-extension-and-development.md) 的主题。
- 想更系统地理解全链路的定点与饱和约定（`$signed`、`{4'h3, rem}` 这类写法的同伴）？继续阅读 [u4-l1 定点数运算与饱和保护](u4-l1-fixed-point-and-saturation.md)。
- 推荐配合阅读：`RTL/uart_monitor.v` 全文（仅 200 行，是练手「写一个完整外设控制器」的好范本），以及 `RTL/fpga_top.v` 把四个子模块拼成数据通路的连线方式。
