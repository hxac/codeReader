# SPROM：单口 ROM 原语

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚**单口 ROM（Single-Port ROM）**的只读结构与同步读时序，以及它为什么没有写口、没有读使能。
- 看懂 `$readmemh` 如何把 `.hex` 系数文件灌入 ROM 数组，并能解释为什么这一步在 SPROM 里是**无条件执行**的。
- 把 SPROM 与 u3-l3 学过的 SDPRAM_SINGLECLK 放在一起做**端口级对比**，理解「只读 ROM」与「简单双口 RAM」在结构、使能、初始化上的差异。
- 理解 SPROM 内部两级读寄存器（1P / 2P）如何与下游乘法流水线的拍数对齐（承接 u4-l1 的「时钟随数据打拍」思想）。

## 2. 前置知识

本讲是 u4 单元（系数通路）的最后一讲，默认你已经具备以下认知（来自前置讲义）：

- **三层结构套路**（u1-l2、u3-l1、u4-l1）：FIR_x2 的存储侧遵循「存储原语 → 控制器 → 封装模块」的分层。SPROM 就是最底层的**存储原语**，它只管「按地址吐系数」，不关心系数怎么用。
- **FIR_COEF 封装**（u4-l1）：SPROM 被 FIR_COEF 当作黑盒实例化，对外输出 `COEF_O`。控制器 SPROM_CONT（u4-l2）给出读地址 `CADDR`，SPROM 据此读出系数。
- **同步读与输出寄存器**（u3-l3）：读地址在时钟上升沿被采样，数据要等若干拍后才出现在输出口；`OUTPUT_REG` 参数用 `generate` 在 1 级（1P）和 2 级（2P）读寄存器之间二选一。
- **两个文件别混淆**（u1-l3）：`.hex` 经 `$readmemh` 初始化存储（决定滤波器系数），`.txt` 经 `$fscanf` 喂输入信号（决定测试 PCM）。本讲的 `.hex` 属于前者。

如果你对「同步读延迟几拍」「`generate` 怎么选寄存器」还不熟，建议先回看 u3-l3，那里用 SDPRAM 把同一套机制讲过一遍。本讲的重点是**对比**：同样是存储原语，ROM 比 RAM 「少」了什么，又为什么必须少。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用 |
| --- | --- | --- |
| `04_FIR_COEF/SPROM.v` | 单口 ROM 原语（DUT） | 本讲的主角，逐段精读 |
| `04_FIR_COEF/Questa/FIR512_x2_48000.hex` | 512 个 16 位有符号系数的初始化文件 | 配合 `$readmemh` 解读其格式 |
| `02_DATA_BUFFER/SDPRAM_SINGLECLK.v` | 简单双口 RAM 原语（对照组） | 与 SPROM 做端口级对比 |
| `04_FIR_COEF/FIR_COEF.v` | 系数侧封装模块 | 看 SPROM 是如何被实例化、参数如何下传的 |

> 命名小陷阱（u4-l1 已提过）：在 SPROM / FIR_COEF 的语境里，参数名 `DATA_WIDTH` 实际指**系数位宽**（默认 16），而不是音频数据位宽（32）。读源码时不要被名字误导。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**单口 ROM 只读时序**、**`$readmemh` 系数加载**、**与 SDPRAM 的对比**。

### 4.1 单口 ROM 只读时序

#### 4.1.1 概念说明

**ROM（Read-Only Memory，只读存储器）** 用来存放「上电后就不再变化」的内容。在 FIR_x2 里，ROM 存的是**滤波器系数**——这些系数由 Python 工具（u6-l1）一次性算好，烧进硬件后在整个工作期间一个比特都不改。既然永远不会写，ROM 就**根本没有写口**。

「单口（Single-Port）」强调的是：整个存储器**只有一个地址端口**。每个时钟周期，SPROM 只能根据 `RADDR_I` 读出**一个**单元。这与 u3-l3 的 SDPRAM 不同——SDPRAM 是「简单双口」，有一个写口、一个读口，两套地址，可以边写边读。

为什么系数侧用单口 ROM 就够了？因为系数只读不写，且每个 MCLK 只需要给乘法器喂一个系数，一个读口完全够用。用更简单的结构，意味着综合后更容易映射到 FPGA 里专用的 ROM 资源（或把 Block RAM 的写口直接接地），也更容易跨厂商替换。

#### 4.1.2 核心流程

SPROM 的读时序是一条**固定的两级流水线**：

```text
RADDR_I ──(posedge CLK_I)──► ROM[RADDR_I] ──► RDATAO_REG_1P ──(posedge)──► RDATAO_REG_2P
                                         1 拍后可见 (1P)              2 拍后可见 (2P)
                                                    └──────────── RDATA_O 二选一接出
```

- 第 \(N\) 个上升沿：把 `ROM[RADDR_I]` 锁进 `RDATAO_REG_1P`；同时把上一拍的 `1P` 推进到 `RDATAO_REG_2P`。
- 读延迟（从给出地址到数据稳定）：
  - `OUTPUT_REG="FALSE"`：取 1P，延迟 **1 个 MCLK**；
  - `OUTPUT_REG="TRUE"`：取 2P，延迟 **2 个 MCLK**。

注意：SPROM 的读 `always` 块**没有任何使能条件**，每个上升沿都无条件地推进这两级寄存器。这是因为系数地址在每个 MCLK 都是有效的、永远在扫描（详见 u4-l2 的多相寻址），不需要像数据缓冲那样在「写读撞址」时把读关掉。这正是 4.3 节要重点对比的差异之一。

读延迟 \(L\) 决定了 FIR_COEF 必须把过采样时钟 `LRCKx2` 也打同样多的拍数 \(N=L\)，才能让「系数」和「节拍」在输出端同拍到达（u4-l1 的对齐约束）。顶层固定 `OUTPUT_REG="TRUE"`，所以 \(L=N=2\)。

#### 4.1.3 源码精读

模块的端口极简——只有时钟、读地址、读数据三样，没有任何写相关端口：

[04_FIR_COEF/SPROM.v:41-54](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L41-L54) —— 定义 `SPROM` 模块、4 个参数（`DATA_WIDTH=16`、`ADDR_WIDTH=8`、`OUTPUT_REG="TRUE"`、`ROM_INIT_FILE`）与端口；注意输入只有 `CLK_I`、`RADDR_I`，输出只有 `RDATA_O`。

存储深度由地址位宽派生，与 SDPRAM 用的是同一个公式：

[04_FIR_COEF/SPROM.v:57](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L57) —— `localparam MEMORY_DEPTH = 2**ADDR_WIDTH;`，当 `ADDR_WIDTH=9` 时深度为 512，正好对应 `FIR512` 这个名字（512 抽头）。

核心的同步读流水线——两级寄存器**始终**维护，每个上升沿都推进：

[04_FIR_COEF/SPROM.v:70-73](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L70-L73) —— `RDATAO_REG_1P <= ROM[RADDR_I];` 把当前地址的系数锁入第一级，`RDATAO_REG_2P <= RDATAO_REG_1P;` 把第一级推到第二级；注意这里**没有 `if` 使能**，与 SDPRAM 的读块形成对照。

最后用 `generate` 在编译期决定输出从哪一级接出：

[04_FIR_COEF/SPROM.v:76-82](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L76-L82) —— `OUTPUT_REG=="TRUE"` 时 `RDATA_O = RDATAO_REG_2P`（2 拍延迟），否则 `RDATA_O = RDATAO_REG_1P`（1 拍延迟）。**字符串必须精确写成大写 `"TRUE"`**，写成小写会落入 1 级路径（u3-l3 已踩过这个坑）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「给出地址后，系数分别在 1 拍、2 拍后出现」。

**操作步骤（源码阅读型，无需综合）**：

1. 打开 [04_FIR_COEF/SPROM.v:60-73](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L60-L73)，在纸上画 4 列表格：`周期`、`RADDR_I`、`RDATAO_REG_1P`、`RDATAO_REG_2P`。
2. 假设第 0 周期上升沿到来前，`RADDR_I` 已稳定为地址 `A`，且 ROM[A]=`0001`、ROM[A+1]=`FFFF`。
3. 按第 70–73 行的赋值，逐拍填写：第 0 拍后 `1P` 变成什么？第 1 拍后 `2P` 变成什么？

**需要观察的现象**：

- 第 0 个上升沿后，`RDATAO_REG_1P` 才变成 `ROM[A]`——数据**滞后地址 1 拍**。
- 第 1 个上升沿后，`RDATAO_REG_2P` 才变成 `ROM[A]`——再滞后 1 拍，共 **2 拍**。

**预期结果**：

| 上升沿编号 | RADDR_I | RDATAO_REG_1P | RDATAO_REG_2P |
| --- | --- | --- | --- |
| 第 0 拍后 | A | `0001` (=ROM[A]) | 旧值 |
| 第 1 拍后 | A+1 | `FFFF` (=ROM[A+1]) | `0001` (=ROM[A]) |

可见 `OUTPUT_REG="TRUE"` 时，`RDATA_O` 在地址给出后 **2 个 MCLK** 才反映该地址的系数——这正是 FIR_COEF 要把 `LRCKx2_O` 也打 2 拍的原因。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `OUTPUT_REG` 设成 `"true"`（小写），`RDATA_O` 会接哪一级？读延迟变成几拍？

> **答案**：字符串比较 `"true" == "TRUE"` 为假，落入 `else` 分支（`gen_reg1p`），`RDATA_O = RDATAO_REG_1P`，读延迟从 2 拍变成 **1 拍**。这会破坏与下游乘法的拍数对齐，是个隐蔽 bug。

**练习 2**：SPROM 的读 `always` 块为什么不需要类似 SDPRAM 的 `RENABLE_I`？请用「地址是否永远有效」来解释。

> **答案**：系数地址由 SPROM_CONT 在每个 MCLK 持续产生且始终有效（永远在多相扫描），不存在「需要保持旧数据」的时刻，因此读寄存器每拍无条件推进即可，省掉读使能。

### 4.2 `$readmemh` 系数加载

#### 4.2.1 概念说明

ROM 里的系数从哪儿来？答案是：**在仿真/上电初始化阶段，由 `$readmemh` 这个 Verilog 系统任务从文件里灌进去**。`$readmemh(file, mem)` 会读取 `file` 中的十六进制数，从 `mem` 的下标 0 开始依次填入。

在 SPROM 里，这步写在 `initial` 块中：

```verilog
initial begin
    $readmemh(ROM_INIT_FILE, ROM);
end
```

文件名通过参数 `ROM_INIT_FILE` 传入，FIR_COEF 实例化时把它指到 `FIR512_x2_48000.hex`（顶层默认）。于是 ROM 在仿真开始（时刻 0）就被装满了 512 个系数，后续电路读到的就是一套完整的设计好的滤波器。

需要特别强调：`$readmemh` 是一种**初始化描述**。在仿真器里它直接填数组；在 FPGA 综合工具（Quartus / Vivado / Gowin 等）里，综合器会把 `$readmemh` 理解为「给 Block ROM 规定上电初值」，从而生成一个带初始化文件的硬件 ROM。也正因如此，**不同厂商对初始化文件的格式/扩展名要求不同**（Questa/Quartus 用 `.hex`，Vivado 用 `.data`），这正是 u6-l2 要展开的话题。

#### 4.2.2 核心流程

`$readmemh` 的填充规则：

- 文件中**每个数占一行**（也支持 `@地址` 跳转语法，但本项目的 `.hex` 没用到）。
- 从 `ROM[0]` 开始，按文件顺序依次写入 `ROM[1]`、`ROM[2]`……直到文件结束或填满 `MEMORY_DEPTH`。
- 每个数的位宽应与 `DATA_WIDTH` 一致；本项目系数是 16 位，故每行是 **4 个十六进制字符**。

以 `FIR512_x2_48000.hex` 的前几行为例（每行一个 16 位有符号系数）：

```text
0000   // ROM[0]  =  0
0000   // ROM[1]  =  0
...
0001   // ROM[k]  = +1
0000
FFFF   // ROM[k+2]= -1   （16 位二补码：FFFF = -1）
0000
0001   // = +1
0001   // = +1
FFFF   // = -1
```

这里的 `FFFF` 是关键证据：它说明系数是 **16 位有符号数（two's complement）**。16 位二补码的取值范围是：

\[
\mathrm{value} = \begin{cases} h & 0 \le h \le 7FFF \quad (\text{即 } 0 \sim +32767) \\ h - 65536 & 8000 \le h \le FFFF \quad (\text{即 } -32768 \sim -1) \end{cases}
\]

所以 `FFFF` 解码为 \(65535 - 65536 = -1\)，`0001` 解码为 \(+1\)。开头大量 `0000` 对应低通 FIR 在边缘处接近 0 的小系数，越往中间系数绝对值越大（这是窗函数设计 FIR 的典型形状，详见 u6-l1）。

#### 4.2.3 源码精读

SPROM 的初始化是无条件的——只要模块存在，就一定会加载文件：

[04_FIR_COEF/SPROM.v:64-67](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L64-L67) —— `initial $readmemh(ROM_INIT_FILE, ROM);`，直接把 `ROM_INIT_FILE` 的内容灌入 `ROM` 数组，没有 `if` 保护。这与 SDPRAM「可选初始化」的写法形成对照（见 4.3）。

ROM 数组本身的声明：

[04_FIR_COEF/SPROM.v:60-62](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L60-L62) —— 声明 `ROM[MEMORY_DEPTH-1:0]`（默认 256 深，FIR_COEF 实例化时改为 512）与两级读寄存器，初值都给 `0`，确保未被文件覆盖的单元至少是确定的 0。

文件名是怎么传进来的？看 FIR_COEF 对 SPROM 的实例化：

[04_FIR_COEF/FIR_COEF.v:95-104](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L95-L104) —— FIR_COEF 把自己的 `RAM_INIT_FILE`（虽叫 RAM，实际是系数文件名）下传给 SPROM 的 `ROM_INIT_FILE`，并把控制器给出的读地址 `CADDR` 接到 `RADDR_I`，读出的 `RDATA_O` 直接作为封装的 `COEF_O`。

#### 4.2.4 代码实践

**实践目标**：把 `FIR512_x2_48000.hex` 的若干行翻译成十进制有符号数，亲手验证「每个十六进制项 = 一个 16 位有符号系数」。

**操作步骤**：

1. 打开 [04_FIR_COEF/Questa/FIR512_x2_48000.hex:1-30](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/Questa/FIR512_x2_48000.hex#L1-L30)。
2. 逐行读前 30 行，按下表把每行的 4 位十六进制转成十进制有符号整数（用 4.2.2 的公式；`FFFF→-1`、`0001→+1`、`0000→0`）。
3. 结合 SPROM 的 [第 64–67 行 `$readmemh`](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L64-L67)，确认：第 1 行（`0000`）进 `ROM[0]`，第 2 行进 `ROM[1]`……即**文件第 i 行 = `ROM[i-1]` 的初值**。

**需要观察的现象**：

- 前约 20 行几乎全是 `0000`（低通 FIR 边缘系数很小）。
- 中间出现 `0001` / `FFFF` 交替——即 ±1 量级的小波动。
- 整个文件每行都是恰好 4 个十六进制字符，对应 16 位。

**预期结果**：你能用一句话说清「`.hex` 的第 i 行就是 SPROM 在地址 `i-1` 处读到的 16 位有符号系数」。**待本地验证**：若要确认文件总行数与 `MEMORY_DEPTH` 是否完全匹配，可在本地用编辑器查看该文件总行数（应为 512 行，对应 `ADDR_WIDTH=9`、深度 \(2^9=512\)）。

#### 4.2.5 小练习与答案

**练习 1**：假如把 `FIR512_x2_48000.hex` 的某一行从 `FFFF` 改成 `7FFF`，该地址读出的系数从多少变成多少？

> **答案**：从 \(-1\) 变成 \(+32767\)（16 位有符号最大值）。这会严重改变滤波器响应，说明 `.hex` 文件**就是**滤波器特性本身。

**练习 2**：为什么 SPROM 的 `$readmemh` 不像 SDPRAM 那样用 `if (ROM_INIT_FILE != "")` 包起来？

> **答案**：ROM 必须有系数才能工作，初始化是**强制的**，因此 SPROM 无条件加载；而 SDPRAM 存的是输入数据缓冲，允许「静音初始化」（文件名留空、RAM 全 0），所以加了空字符串保护（见 4.3 对比）。

### 4.3 与 SDPRAM 的对比

#### 4.3.1 概念说明

SPROM 和 SDPRAM_SINGLECLK 是 FIR_x2 里**一对平行的底层存储原语**：前者服务系数通路（04_FIR_COEF），后者服务数据通路（02_DATA_BUFFER）。两者长得很像——都用 `generate` 选 1P/2P 读寄存器、都用 `2**ADDR_WIDTH` 算深度、都被刻意隔离在最底层以便跨厂商替换 Block RAM。

但它们解决的是**两个本质不同的问题**：

- **SPROM**：存「永不改变的系数」→ **只读**，没有写口。
- **SDPRAM**：存「不断更新的输入 PCM 样点」→ **可读可写**，且写口和读口分开（简单双口），支持环形缓冲的「边写边读」。

理解这对差异，是看懂「为什么系数侧和数据侧要用两种不同原语」的关键。

#### 4.3.2 核心流程

从「端口结构」上看，SDPRAM 有完整的写通道和受使能控制的读通道，而 SPROM 把这些全部砍掉：

```text
SDPRAM_SINGLECLK（双口，可读写）         SPROM（单口，只读）
  CLK_I  ─────────────────────────────►  CLK_I
  WADDR_I / WENABLE_I / WDATA_I  ──┐      （无写口）
  RADDR_I / RENABLE_I            ──┤    RADDR_I ──────────►
  RDATA_O ◄── (2P/1P 经 generate)   RDATA_O ◄── (2P/1P 经 generate)
```

三个最关键的差异：

1. **写口有无**：SDPRAM 有 `always` 块在 `WENABLE_I==1` 时写 `RAM[WADDR_I]`；SPROM 没有这个块，物理上写不出来。
2. **读使能有无**：SDPRAM 的读块用 `if (RENABLE_I==1'b1)` 门控，撞址/复位时可关读以保护刚写入的数据（u3-l2）；SPROM 每拍无条件读，因为系数地址永远有效。
3. **初始化是否可选**：SDPRAM 用 `if (RAM_INIT_FILE != "")` 保护 `$readmemh`，允许全 0 静音初值；SPROM 无条件加载，因为没系数就没滤波器。

#### 4.3.3 源码精读

先看 SDPRAM 的写口（SPROM 完全没有对应物）：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:81-85](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L81-L85) —— SDPRAM 的写 `always` 块：`WENABLE_I==1` 时把 `WDATA_I` 写入 `RAM[WADDR_I]`。SPROM 没有这段。

再看读口的使能差异（最值得对照的一处）：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:88-93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L88-L93) —— SDPRAM 的读块被 `if (RENABLE_I==1'b1)` 包住；对照 [SPROM.v:70-73](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L70-L73) 的无条件读——SPROM 没有 `RENABLE_I` 这个端口，也根本不做门控。

初始化的可选 vs 强制：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:74-78](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L74-L78) —— SDPRAM 用 `if (RAM_INIT_FILE != "")` 保护 `$readmemh`，对照 [SPROM.v:64-67](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L64-L67) 的无条件加载。

两者的 `generate` 输出选择则**几乎一模一样**，这是它们共享的设计骨架：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:96-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L96-L102) —— SDPRAM 的 `generate`，与 [SPROM.v:76-82](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L76-L82) 结构完全一致，只是寄存器名（`RDATA_REG_*` vs `RDATAO_REG_*`）不同。

#### 4.3.4 代码实践

**实践目标**：亲手整理出 SPROM 与 SDPRAM 的端口差异表，把「只读 vs 读写」具象化。

**操作步骤**：

1. 同时打开 [SPROM.v:41-54](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L41-L54) 与 [SDPRAM_SINGLECLK.v:52-62](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L52-L62) 的端口列表。
2. 逐项比对，填写下表（左列已给出维度，右侧自行补全）：

| 维度 | SPROM | SDPRAM_SINGLECLK |
| --- | --- | --- |
| 时钟 | `CLK_I`（单时钟） | `CLK_I`（单时钟） |
| 写口端口 | ？ | `WADDR_I` / `WENABLE_I` / `WDATA_I` |
| 读口端口 | `RADDR_I` | `RADDR_I` / `RENABLE_I` |
| 存储体 | `ROM`（只读） | `RAM`（可写） |
| 读是否门控 | ？（无，每拍读） | 是（`RENABLE_I`） |
| `$readmemh` 保护 | 无（强制加载） | ？（`if != ""`） |
| 默认 `DATA_WIDTH` | 16（系数） | 8（数据） |
| 用途 | 滤波器系数 | 输入 PCM 环形缓冲 |

3. 答完后核对：把表格里标 `？` 的两处补成「无写口」「无条件读」「有 `if` 保护」。

**需要观察的现象**：SDPRAM 的端口列表明显比 SPROM 多出一大块（写通道 + 读使能），这正是「双口 RAM」比「单口 ROM」贵的地方。

**预期结果**：你能口头复述——「SPROM = 砍掉写口、砍掉读使能、初始化强制的 SDPRAM」，并解释为什么系数侧不需要这些。

#### 4.3.5 小练习与答案

**练习 1**：如果硬要用 SDPRAM 来代替 SPROM 存系数，需要怎么接它的写口？这样做有什么坏处？

> **答案**：把 `WENABLE_I` 永久接 `0`（或写口全悬空），`WDATA_I`/`WADDR_I` 接任意值，只用读口。坏处：浪费了一个写口资源、端口更复杂、综合后未必能映射到专用 ROM（可能被当成 RAM 实现），违背「用最简单结构」的原则。

**练习 2**：SPROM 和 SDPRAM 的「读延迟可配置（1P/2P）」机制完全相同。为什么 FIR_x2 顶层把它们都固定成 `OUTPUT_REG="TRUE"`（2 拍）？

> **答案**：为了让系数读出（SPROM，2 拍）与数据读出（SDPRAM，2 拍）的流水线延迟相等，从而在下游乘法器（u5-l1）处同拍对齐相乘；同时让 FIR_COEF 派生的过采样时钟也打 2 拍（u4-l1）。整条通路统一成 2 拍节拍。

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出「系数从文件到乘法器输入」的完整时序链。

要求：

1. 从 [FIR512_x2_48000.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/Questa/FIR512_x2_48000.hex#L1-L30) 任选一行（例如第 23 行的 `FFFF`），指出它最终被装进 `ROM[?]` 的哪个单元。
2. 追踪：控制器 SPROM_CONT 给出读地址 `CADDR` → SPROM 用它索引 `ROM` → 经过 `RDATAO_REG_1P`、`RDATAO_REG_2P` → 从 `RDATA_O`（即 FIR_COEF 的 `COEF_O`）输出。
3. 标注：从 `RADDR_I` 给定到 `COEF_O` 稳定，一共经过几个 MCLK（`OUTPUT_REG="TRUE"` 时）。
4. 在同一张图上标注：这条 2 拍延迟如何与 FIR_COEF 里 `LRCKx2_O` 的 2 级打拍（`p1`/`p2`）对齐（参考 u4-l1）。
5. 最后用一句话总结：相比 SDPRAM，SPROM 省掉了哪些端口、为什么能省。

**预期产出**：一张标注了「文件行 → ROM 下标 → 1P → 2P → COEF_O（2 拍）」的数据流草图，以及一句「SPROM 是砍掉写口与读使能、强制初始化的只读原语」的总结。

## 6. 本讲小结

- SPROM 是 FIR_x2 最底层的**单口只读存储原语**，端口只有 `CLK_I`/`RADDR_I`/`RDATA_O`，没有写口、没有读使能——因为系数只读且地址永远有效。
- 读路径是固定的**两级流水线**（`RDATAO_REG_1P` → `RDATAO_REG_2P`），由 `OUTPUT_REG` 经 `generate` 选 1 拍或 2 拍延迟；顶层固定 `"TRUE"` 即 2 拍。
- 系数靠 `initial $readmemh(ROM_INIT_FILE, ROM)` 在时刻 0 灌入，**无条件加载**；文件每行一个 4 位十六进制 = 16 位有符号系数（`FFFF`=-1 印证了有符号）。
- 与 SDPRAM 的核心差异：SPROM 无写口、读无使能、初始化强制；SDPRAM 有写口、读受 `RENABLE_I` 门控、初始化可选（`if != ""`）。
- 二者共享同一套 `generate` 选级骨架，且顶层都固定 2 拍读延迟，目的是让系数与数据在下游乘法器同拍对齐。
- 两个原语都被隔离在最底层，便于跨厂商替换为各自的 Block ROM/RAM IP——这是 u6-l4 移植主题的基础。

## 7. 下一步学习建议

- **进入运算通路（u5）**：系数从 SPROM 读出后，下一步就是和数据相乘。建议接着读 [u5-l1 MULT：有符号乘法器与流水线寄存]，看 `COEF_O` 与 `RDATA` 如何在乘法器里相遇——本讲的「2 拍读延迟」将直接对应乘法流水线的对齐拍数。
- **追到系数源头（u6）**：如果想搞懂 `FIR512_x2_48000.hex` 这套系数本身是怎么算出来的，去看 [u6-l1 FIR 系数生成：firwin 与多相奇偶抽头分解]（Python `firwin` + 多相分解 + 定点量化）和 [u6-l2 十进制转十六进制：dec2hex.awk 与存储文件格式]（`.hex` 文件的生成与厂商差异）。
- **横向巩固原语层**：可重读 u3-l3 的 SDPRAM 讲义，与本讲的对比表互相对照，确保你能脱口而出这对原语的全部差异。
