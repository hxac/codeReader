# FPGA 移植与开发板示例工程

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚为什么仿真用的 `SPROM` / `SDPRAM_SINGLECLK` 在上真机时最好被各厂商官方 IP（Block RAM/ROM）替换，以及替换时必须守住的关键约束。
- 根据自己的音频系统（MCLK、BCK、采样率）推导出正确的滤波器长度、`WADDR_WIDTH`、`RADDR_WIDTH`，并判断过采样时钟 `BCKx2` 走哪条派生路径。
- 看懂 `10_Example` 下 7 款开发板参考工程的组织方式，知道每个压缩包扩展名（`.qar` / `.zip` / `.xpr.zip` / `.gar`）对应哪家工具链与哪个版本。
- 独立完成「把 FIR_x2 从仿真搬到某一块开发板」的移植检查清单。

本讲是整本手册的「落地篇」。前面 u2~u5 把数据通路逐级拆透，u6-l1~u6-l2 解决了系数怎么来、怎么变成 ROM 文件；这一讲回答最后一个问题：**这套在 Questa 里跑通的 Verilog，怎么变成一块 FPGA 上真正发声的电路。**

## 2. 前置知识

本讲默认你已经掌握以下概念（前序讲义已建立）：

- **三层结构**：FIR_x2 的设计是「存储原语 → 控制器 → 封装 → 顶层」分层实例化的（u2-l1、u3-l1）。`SPROM` 和 `SDPRAM_SINGLECLK` 被刻意压在最底层，**目的就是方便跨厂商替换**（u3-l3、u4-l3）。
- **2 拍读延迟对齐契约**：顶层固定 `OUTPUT_REG = "TRUE"`，使数据 RAM 和系数 ROM 的读延迟都是 2 个 MCLK，与下游乘法流水线对齐（u3-l3、u4-l3、u5-l1）。**移植时这一拍数不能变。**
- **FIR 长度耦合**：滤波器抽头数 \(L = f_{\text{MCLK}}/f_s\)，默认 512，对应系数文件名 `FIR512`（u1-l1）。
- **过采样时钟派生**：`LRCKx2` / `BCKx2` 不是外部 PLL 给的，而是 `SPROM_CONT` 用系数地址的某些位在片内派生的，且有 `ROM_ADDR_WIDTH>=8` / `>=7` 两道阈值，且以标准 32 位立体声 I2S 时钟比例为前提（u2-l2、u4-l1、u4-l2）。
- **系数文件格式**：仿真用 `.hex`（`$readmemh`），Vivado 硬件流程改用 `.data`，两者**内容格式完全相同**，只差扩展名（u6-l2）。

几个本讲用到的新术语，先建立直觉：

- **综合（synthesis）**：把 Verilog 描述翻译成 FPGA 上的真实门电路/硬核的过程。仿真能跑 ≠ 能综合，更不等于综合得好。
- **Block RAM / Block ROM（BRAM）**：FPGA 芯片内部专门的存储硬块，比用查找表（LUT）拼出来的存储更省资源、更快。各厂商都提供图形化 IP 生成器来例化它。
- **存储 IP**：厂商官方提供的、可配置深度/位宽/寄存级的 RAM/ROM 宏，例如 Vivado 的 Block Memory Generator、Quartus 的 IP Catalog 里的 RAM: 2-PORT 等。
- **行为级推断**：直接写 `reg [..] mem [..]` + `$readmemh`，让综合器自己去识别成 BRAM。本仓库的 `SPROM` / `SDPRAM_SINGLECLK` 就是这种写法。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `README.md` | 「Real Machine」真机步骤、Notes 三条移植要点、Verified Devices 七款验证板、Examples 七个参考工程清单。 |
| `07_FIR_x2/FIR_x2.v` | 顶层：5 个 parameter、派生 localparam、四个子模块实例、最后一级饱和流水线、`BCKx2_O` 的 `WADDR_WIDTH>=7` 阈值。 |
| `07_FIR_x2/Questa/FIR_x2.bat` | 仿真编译清单——它列出的正是移植时需要加入工程的 9 个 RTL 文件。 |
| `04_FIR_COEF/SPROM.v` | 待替换的系数 ROM 原语（单口、只读、2 级读寄存）。 |
| `02_DATA_BUFFER/SDPRAM_SINGLECLK.v` | 待替换的数据 RAM 原语（简单双口、可写、2 级读寄存）。 |
| `03_SPROM_CONT/SPROM_CONT.v` | 地址宽度阈值（`ROM_ADDR_WIDTH>=8` / `>=7`）所在，决定 `BCKx2` 是否能正常派生。 |
| `10_Example/01~07_*` | 7 个厂商开发板参考工程（压缩包），每包对应一块板 + 一个工具版本。 |

## 4. 核心概念与源码讲解

### 4.1 厂商存储 IP 替换

#### 4.1.1 概念说明

`SPROM` 和 `SDPRAM_SINGLECLK` 是用纯 Verilog 行为级写成的存储原语：声明一个 `reg` 数组、用 `$readmemh` 装初值、再用 `always` 块做同步读。这种写法**在 Questa 仿真里完美工作**，综合时大多数工具也能把它「推断（infer）」成 Block RAM。

但 README 明确建议：**真机请优先用各厂商官方 IP。**

> Notes
> 1. Single-Port ROM (SPROM.v) & Simple Dual-Port RAM (SDPRAM_SINGLECLK.v) are provided from AUDIY_Verilog_IP but it is recommended to use each-vendor official IP.

——见 [README.md:41-44](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L41-L44)，此处说明这两个原语来自 AUDIY 的通用 IP 库，但推荐换成厂商原生 IP。

为什么「能用」还要「换」？三个理由：

1. **推断不稳定**：行为级 BRAM 推断依赖综合器的版本与启发式策略。两级输出寄存器（`RDATAO_REG_1P` / `RDATAO_REG_2P`）能不能全部收进 BRAM 内部的硬件寄存器，不同工具、不同器件结果不一；推断失败就会溢出到逻辑单元（LE/ALUT/LUT），浪费资源还拖慢时序。厂商 IP 则显式勾选「使用硬件输出寄存」，行为可预期。
2. **时序与资源更优**：BRAM 硬块自带的内嵌寄存器能跑更高频率，对音频 MCLK（几十 MHz）绰绰有余，但用 IP 能确保寄存器落在硬块里而不是 fabric 里。
3. **初始化文件对接顺畅**：厂商 IP 有原生的存储器初始化格式与 GUI（如 Vivado 的 `.coe` / `.mem`、Quartus 的 `.mif` / `.hex`），与各自流程契合。

#### 4.1.2 核心流程

替换的**黄金法则是「接口等价」**：不动控制器和封装层，只把最底层的两个原语换成厂商 IP，且保持端口名、位宽、深度、**读延迟拍数**完全一致。

替换流程：

```
1. 锁定两个替换目标
     - SPROM            → 厂商「单口 ROM」（系数，只读）
     - SDPRAM_SINGLECLK → 厂商「简单双口 RAM」（数据，可写可读）

2. 在厂商 IP GUI 里按下表配置（接口等价）
     | 项                 | SPROM 对应        | SDPRAM 对应          |
     |-------------------|------------------|---------------------|
     | 类型              | Single-Port ROM  | Simple Dual-Port RAM|
     | 数据位宽           | COEF_WIDTH=16     | DATA_WIDTH=32        |
     | 深度              | 2^ADDR_WIDTH     | 2^ADDR_WIDTH         |
     | 读使能             | 不用（ROM 常读）   | RENABLE_I（撞址保护）|
     | 输出寄存级         | 2 级              | 2 级                 |
     | 初始化文件         | 系数 .data/.hex   | 可选（默认静音全 0）  |

3. 保持读延迟 = 2 拍不变（关键！见 4.1.3）

4. 封装层 DATA_BUFFER / FIR_COEF 的连线一根都不用改
     （它们只引用端口名 WEN/WADDR/REN/RADDR/RDATA 等）
```

第 3 步是最容易踩坑的地方，下面用源码说明为什么必须是 2 拍。

#### 4.1.3 源码精读

**为什么读延迟必须是 2 拍**——顶层把 `OUTPUT_REG` 写死为 `"TRUE"`：

[07_FIR_x2/FIR_x2.v:74-79](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L74-L79) 定义了不可改的 localparam，其中 `OUTPUT_REG = "TRUE"`，并把它透传给 `DATA_BUFFER` 与 `FIR_COEF`：

```verilog
localparam RADDR_WIDTH = WADDR_WIDTH + 1;
localparam MULT_WIDTH  = DATA_WIDTH + COEF_WIDTH;
localparam OUTPUT_REG  = "TRUE";          // 固定 2 级读寄存
localparam BUFF_INIT   = "BUFFER_INIT.hex";
```

[07_FIR_x2/FIR_x2.v:103-115](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L103-L115) 把 `OUTPUT_REG(OUTPUT_REG)` 传给 `DATA_BUFFER`；同理 [07_FIR_x2/FIR_x2.v:119-132](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L119-L132) 传给 `FIR_COEF`。这意味着数据通路与系数通路**都**带 2 拍读延迟，且过采样时钟也相应打 2 拍——这就是贯穿 u3~u5 的「时钟随数据打拍」对齐契约。

在原语内部，2 拍是由两级寄存器 + `generate` 实现的。以系数 ROM 为例：

[04_FIR_COEF/SPROM.v:70-82](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L70-L82) ——`RDATAO_REG_1P` 是第 1 拍、`RDATAO_REG_2P` 是第 2 拍，`OUTPUT_REG=="TRUE"` 时输出取第 2 拍：

```verilog
always @ (posedge CLK_I) begin
    RDATAO_REG_1P <= ROM[RADDR_I];
    RDATAO_REG_2P <= RDATAO_REG_1P;     // 第 2 级
end
generate
    if (OUTPUT_REG == "TRUE") begin : gen_reg2p
        assign RDATA_O = RDATAO_REG_2P; // 2 拍延迟
    end else ...
```

数据 RAM 的结构完全对称，见 [02_DATA_BUFFER/SDPRAM_SINGLECLK.v:88-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L88-L102)。

> **移植铁律**：在厂商 IP GUI 里，输出寄存器（Output Register / Preregister）一定要勾到**第 2 级 / "Preregister Output to"** 这档，使总读延迟 = 2 个 MCLK。若只勾 1 级或 0 级，数据会比系数（或反之）早到一拍，下游乘法对不齐，整个滤波输出就是错的——而且**仿真可能依然对**（因为仿真里你替换的是同一个原语），错误只在真机综合后才暴露。这是移植最隐蔽的坑。

另外一个差异点：`SDPRAM` 的读口带 `RENABLE_I`，在读写撞址时拉低以保护写（u3-l2）；`SPROM` 是 ROM，**没有读使能、没有写口**，每个 MCLK 都在读。换厂商 IP 时，单口 ROM 同样不需要读使能引脚，简单双口 RAM 则要保留读使能。

最后看初始化：ROM 强制加载、RAM 可选加载。

[04_FIR_COEF/SPROM.v:64-67](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L64-L67) 用 `initial $readmemh(ROM_INIT_FILE, ROM)` **无条件**加载系数；而 [02_DATA_BUFFER/SDPRAM_SINGLECLK.v:74-78](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L74-L78) 则用 `if (RAM_INIT_FILE != "")` **可选**加载——硬件上 RAM 内容会被真实音频数据即时覆盖，所以 RAM 初始化对真机意义不大，ROM 初始化（系数）则是必须的。

#### 4.1.4 代码实践

**实践目标**：为系数 ROM `SPROM` 编写一份「厂商 IP 参数映射表」，确保替换后接口与延迟等价。

**操作步骤**（源码阅读 + 设计型实践，无需运行综合工具）：

1. 重读 [04_FIR_COEF/SPROM.v:41-54](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L41-L54) 的端口与参数。
2. 仿照下表，把 `SPROM` 的每个 Verilog 参数映射到你所用厂商 IP GUI 里的对应选项（以 Vivado Block Memory Generator 为例，若你用其他厂商请填对应项）：

| `SPROM` 参数 / 端口 | 值（默认） | 厂商 IP GUI 对应设置 |
|---|---|---|
| `DATA_WIDTH` | 16 | Width of Port A = 16 |
| `ADDR_WIDTH` | 8（默认）/ 9（顶层实际） | Depth = 2^ADDR_WIDTH |
| `OUTPUT_REG` | `"TRUE"` | Preregister Output: **2 stages**（总延迟 2 拍）|
| `ROM_INIT_FILE` | 系数文件 | COE/MEM 文件 |
| `CLK_I` | MCLK_I | ClkA |
| `RADDR_I` | 地址 | Addr A |
| `RDATA_O` | 系数输出 | Dout A |
| （无写口） | — | Memory Type = **Single-Port ROM** |

3. 自检：把 IP 的「读延迟」一栏写明 **2 clock cycles**。

**需要观察的现象**：映射表里没有任何一行把输出寄存设成 0 级或 1 级。

**预期结果**：替换后，`FIR_COEF` 的 `.RDATA_O(COEF)` 连线照旧，封装层代码零改动，且系数在 MCLK 上升沿后第 2 拍出现在 `COEF` 上。

#### 4.1.5 小练习与答案

**练习 1**：为什么替换 `SDPRAM_SINGLECLK` 时不能顺手把输出寄存从 2 级改成 1 级以求「更快」？

**参考答案**：因为顶层的 `MULT`、`ADD` 以及过采样时钟 `LRCKx2` 的打拍级数都建立在「数据 = 2 拍延迟」之上（u5-l1 的对齐契约）。把 RAM 改成 1 级会让数据比 `LRCKx2` 早 1 拍到达 `ADD`，累加复位节拍错位，卷积和全错。改延迟必须同时改整条流水线，不能只动一个原语。

**练习 2**：`SPROM` 有 `WENABLE_I` / `WDATA_I` 端口吗？这对你选 IP 类型有什么提示？

**参考答案**：没有。`SPROM` 是只读 ROM（见其端口列表 [04_FIR_COEF/SPROM.v:48-53](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L48-L53)）。这提示你在 IP GUI 里应选 **Single-Port ROM**，而不是 RAM；选成 RAM 会多出无用的写口，浪费资源且容易接错。

### 4.2 时钟与滤波器长度参数调整

#### 4.2.1 概念说明

移植不只是「换存储」，还要按**你的音频系统**调参数。README 的真机步骤第二条就是改参数：

> 2. Change parameters depending on your audio data settings (ex. MCLK frequency, BCK frequency).

——见 [README.md:35-39](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L35-L39)。

核心耦合关系只有一条（u1-l1 已点明，本讲给出可计算的形式）：**FIR 滤波器的总抽头数等于 MCLK 频率除以采样频率**。

> Notes
> 2. FIR filter length must be equals to (MCLK_I frequency)/(Sampling frequency)

——见 [README.md:41-44](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L41-L44)。

为什么？因为本设计是「单 MCLK 时钟域」+「每 MCLK 做一次乘加」的结构（u2-l2、u4-l2）。一个输入样点持续 \(f_{\text{MCLK}}/f_s\) 个 MCLK 周期；2 倍过采样把一个 LRCK 周期切成两个过采样样点，每个过采样样点占用一半的 MCLK 周期，而每个样点的卷积需要逐拍乘加——所以抽头数恰好被 MCLK 与采样率之比锁死。

#### 4.2.2 核心流程

设 \(L\) 为 FIR 总抽头数，\(W = \text{WADDR\_WIDTH}\)，\(R = \text{RADDR\_WIDTH}\)，则：

\[
L = \frac{f_{\text{MCLK}}}{f_s}
\]

\[ L \text{ 必须为偶数（多相奇偶分解要求两相抽头数相等，见 u6-l1）} \]

\[
W = \log_2\!\left(\frac{L}{2}\right) = \log_2 L - 1
\]

\[
R = W + 1 = \log_2 L
\]

直观解释：2 倍过采样把 \(L\) 个系数分成奇/偶两相，每相 \(L/2\) 个系数；数据延迟线只需存 \(L/2\) 个输入样点（因为上采样插入的零不占存储，u4-l2），所以数据 RAM 深度 \(2^W = L/2\)，系数 ROM 深度 \(2^R = L\)。

移植调参流程：

```
1. 确定你的音频系统：fs（采样率）与 f_MCLK（主时钟）
2. 算 L = f_MCLK / fs；要求 L 为偶数，且是 2 的幂（否则 W 不是整数）
3. 算 WADDR_WIDTH = log2(L) - 1，RADDR_WIDTH = WADDR_WIDTH + 1
4. 用 11_fir_gen 重新生成 L 个抽头的系数（u6-l1），并用 dec2hex.awk
   转成定宽十六进制存储文件（u6-l2）
5. 把 COEF_INIT 指向新生成的系数文件名
6. 检查地址宽度是否仍满足过采样时钟派生阈值（见 4.2.3 的坑）
```

第 6 步是一个常被忽略的硬约束，下面用源码说明。

#### 4.2.3 源码精读

**顶层的可调参数**就这 5 个（[07_FIR_x2/FIR_x2.v:52-59](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L52-L59)）：

```verilog
module FIR_x2 #(
    parameter DATA_WIDTH  = 32,            // PCM 位宽
    parameter COEF_WIDTH  = 16,            // 系数位宽
    parameter WADDR_WIDTH = 8,             // 数据 RAM 地址宽度 → 深度 256
    parameter COEF_INIT   = "FIR512.hex",  // 系数文件名
    parameter DATAO_WIDTH = 32             // 输出位宽
)
```

其中 `WADDR_WIDTH` 和 `COEF_INIT` 是移植时最常动的两个。`RADDR_WIDTH`、`MULT_WIDTH` 由 localparam 自动派生（[07_FIR_x2/FIR_x2.v:76-77](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L76-L77)），**不用手动改**。

**过采样时钟派生阈值（移植必须复核）**：`BCKx2` 的生成依赖系数地址宽度，当宽度不够时会退化。在控制器里有两道阈值：

[03_SPROM_CONT/SPROM_CONT.v:93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L93) ——选位阈值：

```verilog
BCKx_REG <= (ROM_ADDR_WIDTH >= 8) ? CADDR_REG[ROM_ADDR_WIDTH-7] : 1'b0;
```

[03_SPROM_CONT/SPROM_CONT.v:99](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L99) ——派生/兜底阈值：

```verilog
assign BCKx_O = (ROM_ADDR_WIDTH >= 7) ? BCKx_REG : MCLK_I;
```

顶层在最终输出处还有一道（[07_FIR_x2/FIR_x2.v:175](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L175)）：

```verilog
assign BCKx2_O = (WADDR_WIDTH >= 7) ? BCKx2O_REG : MCLK_I;
```

注意 `ROM_ADDR_WIDTH` 就是 `RADDR_WIDTH = WADDR_WIDTH + 1`。把这些阈值换算到 `WADDR_WIDTH`：

| `WADDR_WIDTH` | `RADDR_WIDTH` | 对应抽头数 \(L\) | `BCKx2` 路径 |
|---|---|---|---|
| 8（默认） | 9 | 512 | 正常派生（满足全部阈值）|
| 7 | 8 | 256 | 派生，但 `BCKx_REG` 选位恰在临界 |
| ≤ 6 | ≤ 7 | ≤ 128 | **退化**：顶层 `BCKx2_O` 兜底到 `MCLK_I` |

> **移植警示**：`LRCKx2` 由地址最高位派生，任何位宽都能生成（u4-l1）；但 `BCKx2` 是按「标准 32 位立体声 I2S（BCK=MCLK/8、LRCK=MCLK/512）」的比例选位的（u4-l2）。如果你的滤波器长度大幅偏离 512（即 `WADDR_WIDTH < 7`），`BCKx2` 会落到 `MCLK_I` 兜底分支，不再是真正的「2×BCK」。所以**缩小滤波器不是无脑改 `WADDR_WIDTH` 就行**，必须同时确认下游 DAC 接受的 `BCKx2` 来源。默认的 512 抽头（44.1/48 kHz → 88.2/96 kHz）是经过验证的安全配置，所有 `10_Example` 工程都用它。

#### 4.2.4 代码实践

**实践目标**：给定一组音频参数，推导出移植所需的全部规模参数，并预测 `BCKx2` 的路径。

**操作步骤**（纯计算 + 源码核对）：

1. 假设你的系统是 \(f_{\text{MCLK}} = 22.5792\,\text{MHz}\)、\(f_s = 44.1\,\text{kHz}\)（典型 44.1k 音频）。
2. 计算：
   - \(L = 22{,}579{,}200 / 44{,}100 = 512\)
   - \(W = \log_2(512) - 1 = 8\)
   - \(R = 9\)
3. 核对 [07_FIR_x2/FIR_x2.v:56](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L56) 默认 `WADDR_WIDTH = 8`，完全匹配，**无需改参数**，可直接用 `FIR512` 系数。
4. 再算一个非默认情形：\(f_{\text{MCLK}} = 6.144\,\text{MHz}\)、\(f_s = 48\,\text{kHz}\)：
   - \(L = 6{,}144{,}000 / 48{,}000 = 128\)，\(W = \log_2(128)-1 = 6\)，\(R = 7\)
   - 查上表：\(W = 6 < 7\) → `BCKx2_O` 兜底到 `MCLK_I`。

**需要观察的现象**：第一种参数下，所有阈值都满足、参数与默认一致；第二种参数下，`BCKx2` 退化。

**预期结果**：第一种可直接移植；第二种虽能算出 `WADDR_WIDTH=6`，但必须额外处理 `BCKx2`（要么换 DAC、要么改设计），不能照搬。这印证了 README 把 44.1/48 kHz → 88.2/96 kHz 作为唯一示例是有原因的。

#### 4.2.5 小练习与答案

**练习 1**：若 \(f_{\text{MCLK}} = 24.576\,\text{MHz}\)、\(f_s = 48\,\text{kHz}\)，求 \(L\)、\(W\)、\(R\)，并判断 `COEF_INIT` 应指向什么名字的系数文件。

**参考答案**：\(L = 24{,}576{,}000/48{,}000 = 512\)；\(W = \log_2 512 - 1 = 8\)；\(R = 9\)。与默认完全一致，`COEF_INIT` 可沿用 `FIR512.hex`（Vivado 下为 `.data`）。这正是 `08_hex/FIR512_x2_48000.hex` 文件名里「512」与「48000」的由来。

**练习 2**：为什么 `WADDR_WIDTH` 必须是整数？若某系统的 \(L = 384\)，能直接用本设计吗？

**参考答案**：`WADDR_WIDTH` 是地址位宽，必须是整数，因此要求 \(L\) 是 2 的整数次幂。\(L = 384 = 256 \times 1.5\)，\(\log_2(384/2) \approx 7.58\) 不是整数，**不能直接用**。需要就近取 \(L = 256\) 或 \(512\)（相应调整 MCLK 或 fs），或重新设计地址生成逻辑——本设计的地址派生假设 \(L\) 是 2 的幂。

### 4.3 开发板示例工程

#### 4.3.1 概念说明

`10_Example` 提供 7 个**现成的真机参考工程**，覆盖 Altera/Intel、Efinix、AMD（Xilinx）、Gowin 四家厂商。它们各自是一个完整的「FPGA 工程」（包含引脚约束、IP 配置、时钟、I2S 接口等），把 FIR_x2 包成了能直接编译下载的实现。

这些工程的价值在于：**厂商存储 IP 已经替你替换好、参数已经替你设好、存储文件格式已经替你转好**。最快的「移植」其实是直接打开对应工程。

#### 4.3.2 核心流程

把 7 个工程按厂商/工具分组（信息全部来自 [README.md:46-63](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L46-L63)）：

| # | 目录 | 器件 | 开发板 | 工具链 / 版本 | 压缩包格式 |
|---|------|------|--------|--------------|-----------|
| 1 | `01_EK-10CL025U256` | Altera Cyclone 10 LP | Intel EK-10CL025U256 评估卡 | Quartus Prime Lite **v24.1** | `.qar` |
| 2 | `02_DE0-Nano` | Altera Cyclone IV E | Terasic DE0-Nano | Quartus Prime Lite v24.1 | `.qar` |
| 3 | `03_DE10-Lite` | Altera MAX 10 | Terasic DE10-Lite | Quartus Prime Lite v24.1 | `.qar` |
| 4 | `04_T20F256DevKit` | Efinix Trion T20 | Trion T20 BGA256 开发套件 | Efinity **2025.1.110.4.9** | `.zip` |
| 5 | `05_Cmod-A7` | AMD Artix-7 | Digilent Cmod A7 | Vivado **2025.1** | `.xpr.zip` |
| 6 | `06_Cmod-S7` | AMD Spartan-7 | Digilent Cmod S7 | Vivado 2025.1 | `.xpr.zip` |
| 7 | `07_TangPrimer20K` | Gowin Arora GW2A | Sipeed Tang Primer 20K + Dock | Gowin FPGA Designer **v1.9.11.01 Education** | `.gar` |

要点：

- **同一厂商、不同器件**：Quartus 三款（Cyclone 10 LP / Cyclone IV E / MAX 10）证明设计在 Intel 不同工艺间可移植；Vivado 两款（Artix-7 / Spartan-7）同理。
- **压缩包扩展名 = 工具原生工程归档**：`.qar`（Quartus Archive）、`.zip`（Efinity 工程）、`.xpr.zip`（Vivado 工程，`.xpr` 是 Vivado 工程文件）、`.gar`（Gowin Archive）。用对应工具「Restore / Open / Import」即可还原。
- **统一功能**：README 写明 [README.md:56](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L56) 「These sample projects oversample 44.1/48kHz PCM to 88.2/96kHz PCM」——7 个工程功能完全相同，只是平台不同。

#### 4.3.3 源码精读

「真机步骤」本身就是移植 SOP（[README.md:35-39](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L35-L39)）：

```
1. Add all modules (except *_tb.v) and memory initialization file into your project.
2. Change parameters depending on your audio data settings.
3. Synthesize, place & route to your FPGA.
4. Confirm actual operation.
```

第 1 步「加哪些模块」由仿真编译清单给出答案。看 [07_FIR_x2/Questa/FIR_x2.bat:5-14](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L5-L14)，它 `vlog` 的就是全部设计文件（最后一个是 testbench，移植时剔除）：

```bash
vlog +cover=bcs ../FIR_x2.v
vlog ../../01_DPRAM_CONT/DPRAM_CONT.v
vlog ../../02_DATA_BUFFER/SDPRAM_SINGLECLK.v
vlog ../../02_DATA_BUFFER/DATA_BUFFER.v
vlog ../../03_SPROM_CONT/SPROM_CONT.v
vlog ../../04_FIR_COEF/SPROM.v
vlog ../../04_FIR_COEF/FIR_COEF.v
vlog ../../05_MULT/MULT.v
vlog ../../06_ADD/ADD.v
vlog ../FIR_x2_TB.v          # ← testbench，移植时不要加
```

即移植需要 **9 个 RTL 文件**：`FIR_x2`、`DPRAM_CONT`、`SDPRAM_SINGLECLK`、`DATA_BUFFER`、`SPROM_CONT`、`SPROM`、`FIR_COEF`、`MULT`、`ADD`，外加 **2 个存储初始化文件**（系数 `COEF_INIT`、可选的数据 `BUFFER_INIT`）。

最后别忘了 `.hex` → `.data` 的转换。README Note 3（[README.md:44](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L44)）：

> 3. When you use in vivado, memory file(.hex) should be changed to data file(.data).

而 `11_fir_gen/README.md` 第 3 节也规定生成脚本的输出**必须用 `.data` 扩展名**（[11_fir_gen/README.md:37-38](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/README.md#L37-L38)）。注意内容完全相同，只是扩展名约定不同（详见 u6-l2）：Vivado 工程的 `COEF_INIT` 应指向 `.data` 文件。这正好解释了为什么 AMD 两款板（Cmod-A7 / Cmod-S7，都用 Vivado）的工程里用的是 `.data`。

#### 4.3.4 代码实践

**实践目标**：选定一款开发板工程，整理出完整的移植检查清单。

**操作步骤**：

1. 从上表中选定 AMD Cmod-A7（`05_Cmod-A7`，Vivado 2025.1，Artix-7）作为目标——它涉及 `.hex→.data` 转换，最具教学价值。
2. 解压 `10_Example/05_Cmod-A7/Oversample_x2_Cmod-A7.xpr.zip`（待本地验证：若你没有该板/工具，可只读工程内的文件列表）。
3. 在解压后的工程里逐一确认下表（**待本地验证**项需打开工程才能看到）：

| 检查项 | 预期内容 |
|--------|---------|
| 工具链 | Vivado 2025.1 |
| 器件 | xc7a35tcpg236-1（Artix-7）|
| RTL 文件数 | 9（见 4.3.3 清单）|
| `SPROM` 是否替换 | 替换为 Vivado Block Memory（单口 ROM）|
| `SDPRAM` 是否替换 | 替换为 Vivado Block Memory（简单双口 RAM）|
| 输出寄存级 | 各 2 级（接口等价）|
| 系数文件 | `FIR512_x2_48000.data`（注意是 `.data`）|
| `COEF_INIT` 指向 | 上述 `.data` 文件 |
| `WADDR_WIDTH` | 8（512 抽头）|
| 引脚约束 | MCLK/BCK/LRCK/DATA 对应 Cmod-A7 的 Pmod/IO（待本地验证）|

**需要观察的现象**：工程里不应再出现纯行为级 `reg [..] ROM [..]` 的 `SPROM.v`/`SDPRAM.v`，而是 Vivado IP 例化；系数文件扩展名为 `.data`。

**预期结果**：直接综合实现即可生成比特流，下载后把 44.1/48 kHz PCM 升频到 88.2/96 kHz。

#### 4.3.5 小练习与答案

**练习 1**：把 7 个压缩包扩展名（`.qar` / `.zip` / `.xpr.zip` / `.gar`）与工具一一对应，并说明为何同是 AMD 的两款板用相同扩展名。

**参考答案**：`.qar`→Quartus（01/02/03，Intel）；`.zip`→Efinity（04，Efinix）；`.xpr.zip`→Vivado（05/06，AMD）；`.gar`→Gowin（07，Gowin）。AMD 两款（Artix-7、Spartan-7）都用 Vivado，故都是 `.xpr.zip`——同一工具链天然跨同厂商不同系列器件。

**练习 2**：为什么把工程从 Cmod-A7（Artix-7）搬到 Cmod-S7（Spartan-7）几乎不费力？

**参考答案**：两者都用 Vivado、同一套 IP、同一份 `.data` 系数、同一套参数，差别仅在器件型号与引脚约束。只需在 Vivado 里改目标器件、重新分配引脚即可——这正体现了「存储原语隔离 + 厂商 IP」分层带来的可移植性。

## 5. 综合实践

**任务**：把 FIR_x2 从「Questa 仿真」完整移植到 **AMD Cmod-A7**（Vivado 2025.1），产出一份可交付的移植清单与流程图。

要求覆盖三大模块的全部要点：

1. **存储 IP 替换**：列出要替换的两个原语、各自对应 Vivado Block Memory 的类型与配置（位宽、深度、输出寄存级 = 2）。
2. **参数调整**：确认 \(f_{\text{MCLK}}/f_s = 512\)、`WADDR_WIDTH = 8`、`RADDR_WIDTH = 9`、`BCKx2` 走正常派生路径。
3. **文件与工程**：列出 9 个 RTL 文件、把 `COEF_INIT` 改指 `.data`、说明为何用 `.data` 而非 `.hex`。
4. **工具版本**：Vivado 2025.1、器件 Artix-7。

参考流程图（伪流程）：

```
解压 05_Cmod-A7 工程
        │
        ▼
核对 9 个 RTL 已加入 ────────► (缺则按 FIR_x2.bat 清单补)
        │
        ▼
确认 SPROM/SDPRAM 已是 Vivado IP ──► (否则用 GUI 重建，输出寄存=2 级)
        │
        ▼
确认系数文件 = *.data ──────────► (是 .hex 则用 dec2hex.awk 重生成并改名)
        │
        ▼
核对 WADDR_WIDTH=8 / COEF_INIT 指向 .data
        │
        ▼
Synthesize → Place & Route → Generate Bitstream → 下载
        │
        ▼
喂 44.1/48k PCM，示波器/逻辑分析仪量 LRCKx2_O ≈ 2× LRCK_I
```

> 完成后，对照 `10_Example/05_Cmod-A7` 里的官方工程做 diff——你的清单应当与官方实现一致；若你的器件不是 Artix-7，请把「器件/引脚」一栏换成自己的，其余不变。若无法在本地运行 Vivado，标注「待本地验证」即可。

## 6. 本讲小结

- 移植的第一原则是**接口等价**：用厂商 Block RAM/ROM IP 替换 `SPROM` / `SDPRAM_SINGLECLK`，端口、位宽、深度、**读延迟（2 拍）**保持一致，封装层与控制器零改动。
- 输出寄存级必须锁在 **2 级**，这是贯穿全设计的「时钟随数据打拍」对齐契约；改它就要改整条流水线，是最隐蔽的移植坑。
- 滤波器规模由音频系统锁死：\(L = f_{\text{MCLK}}/f_s\)，`WADDR_WIDTH = log2(L)-1`，`RADDR_WIDTH = WADDR_WIDTH+1`；默认 512 抽头对应 44.1/48 kHz。
- `BCKx2` 的派生有 `WADDR_WIDTH>=7` 等阈值，且以标准 32 位立体声 I2S 比例为前提；滤波器长度大幅缩水时 `BCKx2` 会退化到 `MCLK_I`，缩小规模需谨慎。
- 移植需带入 **9 个 RTL 文件 + 系数初始化文件**，文件清单由 `FIR_x2.bat` 给出；Vivado 下系数文件要从 `.hex` 改为 `.data`（内容不变）。
- `10_Example` 的 7 个工程覆盖四家厂商，扩展名（`.qar`/`.zip`/`.xpr.zip`/`.gar`）对应工具；最快的移植是直接打开同厂商工程再改器件与引脚。

## 7. 下一步学习建议

- **若你要自己换板子**：挑一个与 `10_Example` 同厂商的工程（例如你有 Cyclone IV 就从 `02_DE0-Nano` 出发），只改器件型号和引脚约束，跑通后再尝试换厂商。
- **若你要换采样率/滤波器**：回到 [11_fir_gen/fir_gen.py](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/fir_gen.py)（u6-l1）重新生成系数，注意保持抽头数为 2 的整数次幂且为偶数，并重新核算 `WADDR_WIDTH` 与 `BCKx2` 路径。
- **若你要深入时钟派生**：重读 u4-l2 的 `SPROM_CONT`，理解 `BCKx2` 的位选派生与阈值，再看一遍本讲 4.2.3，你就明白为什么默认 512 抽头是「安全区」。
- **延伸阅读**：本项目已宣告停止维护、即将归档（见 [README.md:5-12](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L5-L12)），源码稳定，适合作为静态教材；官方提到将有后继项目扩展 FIR_x2 的功能，可保持关注。
