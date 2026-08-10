# 顶层 FIR_x2 模块：端口、参数与实例化图谱

## 1. 本讲目标

本讲是进入 FIR_x2 内部实现的第一站。读完本讲，你应当能够：

- 说清 `FIR_x2.v` 顶层模块对外暴露了哪些端口、声明了哪些参数，以及 `RADDR_WIDTH`、`MULT_WIDTH` 等 `localparam` 是怎样从基本参数**派生**出来的。
- 画出 `DATA_BUFFER → FIR_COEF → MULT → ADD` 四大子模块的实例化图谱，知道谁实例化谁、各自负责什么。
- 沿内部 wire 追踪一个 PCM 样点从 `DATA_I` 到 `DATA_O` 的完整通路，并能标注出每一级的位宽。
- 理解顶层最后一段流水线寄存器和输出赋值的作用。

本讲只看「顶层骨架」。四大子模块各自的内部细节（控制器、存储原语、乘法累加时序、饱和舍入）分别在 u3、u4、u5 各讲展开，本讲不深入。

## 2. 前置知识

在阅读本讲前，建议你已经具备以下概念（不熟悉也没关系，下面会用大白话解释）：

- **模块（module）与实例化（instantiation）**：Verilog 里一个 `module` 是一块电路。把一个模块「放进」另一个模块里使用，叫**实例化**。被放进去的叫子模块，放它的叫父模块。`FIR_x2` 就是把四个子模块拼起来的父模块。
- **参数（parameter）与本地参数（localparam）**：`parameter` 是调用方可以修改的「配置项」；`localparam` 是模块内部自己算出来、调用方不能改的「派生值」。
- **单时钟域（single clock domain）**：整个设计只用一个时钟 `MCLK_I`，所有寄存器都在 `MCLK_I` 上升沿更新。`BCK_I`、`LRCK_I` 在内部被当作「数据/控制信号」而非独立时钟（这一点 u2-l2 会详述）。
- **FIR 卷积直觉**：FIR 滤波就是把一串输入样点与一串系数两两相乘再求和。2 倍过采样意味着每输入 1 个样点，要输出 2 个样点，因此系数数量是输入缓冲深度的 2 倍（多相分解，细节见 u4）。

一个贯穿全讲的关键认知：**顶层 `FIR_x2.v` 几乎是纯结构化的（structural）**——它本身不带算法逻辑，只做「连线 + 实例化」，唯一的 `always` 块是最后一级输出流水线。理解了它的「端口—参数—实例—连线」四件事，就理解了整个滤波器的骨架。

## 3. 本讲源码地图

本讲只围绕一个核心文件，并借用一个测试激励和一层封装模块来佐证：

| 文件 | 作用 |
|------|------|
| [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) | **本讲主角**。顶层模块，定义端口/参数、实例化四大子模块、做最终输出流水线。 |
| [07_FIR_x2/FIR_x2_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v) | 顶层测试激励。给出了参数的真实取值，可用来核对本讲对参数的讲解。 |
| [02_DATA_BUFFER/DATA_BUFFER.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v) | 子模块 `DATA_BUFFER` 的实现，用来印证「封装 = 控制器 + 存储原语」这一层级关系。 |

> 说明：本讲引用的子模块端口（`FIR_COEF`、`MULT`、`ADD`）来自各自目录下的源文件，但只看它们的**端口声明**以核对连线，不展开内部实现。

## 4. 核心概念与源码讲解

### 4.1 端口与参数定义

#### 4.1.1 概念说明

顶层模块对外要回答两个问题：

1. **我需要什么、我能给什么？** —— 由**端口（port）**回答。`FIR_x2` 的输入是标准 I2S 风格的音频信号（`MCLK_I/BCK_I/LRCK_I/NRST_I/DATA_I`），输出是 2 倍过采样后的音频信号（`BCKx2_O/LRCKx2_O/DATA_O`）。
2. **这套电路有多大、用什么系数？** —— 由**参数（parameter）**回答。比如数据位宽、系数字宽、缓冲深度、系数文件名。

此外，有些数值不是用户直接给的，而是从基本参数**算出来的**，这类用 `localparam` 表达，例如「乘积位宽 = 数据位宽 + 系数字宽」。把派生关系写清楚，改动一处就能让整个设计自适应，这正是参数化设计的威力。

#### 4.1.2 核心流程

端口与参数的组织流程可以概括为：

```text
用户可配置参数 (parameter)
        │
        ├── 直接决定端口位宽（DATA_WIDTH → DATA_I/DATA_O 位宽）
        │
        └── 派生出 localparam
                ├── RADDR_WIDTH = WADDR_WIDTH + 1   （系数 ROM 地址比数据 RAM 多 1 位）
                ├── MULT_WIDTH  = DATA_WIDTH + COEF_WIDTH （乘积/累加位宽）
                ├── OUTPUT_REG  = "TRUE"            （存储原语输出寄存器使能）
                └── BUFF_INIT   = "BUFFER_INIT.hex" （数据 RAM 初值文件）
```

四个基本参数之间的数量关系（这是本小节最重要的结论）：

- **数据缓冲深度** \( = 2^{\text{WADDR\_WIDTH}} \)。
- **系数 ROM 深度** \( = 2^{\text{RADDR\_WIDTH}} = 2^{\text{WADDR\_WIDTH}+1} = 2 \times 2^{\text{WADDR\_WIDTH}} \)，**恰好是数据缓冲的 2 倍**。
- 为什么是 2 倍？因为 2 倍过采样把一组 \(N\) 抽头系数按**多相（polyphase）**拆成奇、偶两组各 \(N/2\) 个；输入缓冲只需容纳其中一组（\(N/2\) 个样点），而系数 ROM 要装下全部 \(N\) 个系数，所以系数总数是输入缓冲深度的两倍。多相的生成机制在 u4 详述，这里只需记住「系数地址位宽 = 数据地址位宽 + 1」。
- **乘积位宽**：两个有符号数相乘，乘积位宽等于两者位宽之和，即 \(\text{MULT\_WIDTH} = \text{DATA\_WIDTH} + \text{COEF\_WIDTH}\)。

#### 4.1.3 源码精读

**参数声明**——这一段定义了五个可配置项，并默认按「32 位 PCM / 16 位系数 / 256 深缓冲」配置：

[07_FIR_x2/FIR_x2.v:52-59](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L52-L59) —— 声明 `DATA_WIDTH/COEF_WIDTH/WADDR_WIDTH/COEF_INIT/DATAO_WIDTH` 五个参数。

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `DATA_WIDTH` | 32 | PCM 输入/输出数据位宽 |
| `COEF_WIDTH` | 16 | 滤波器系数字宽 |
| `WADDR_WIDTH` | 8 | 输入数据 RAM 写地址位宽（深度 \(2^8=256\)） |
| `COEF_INIT` | `"FIR512.hex"` | 系数 ROM 初始化文件名 |
| `DATAO_WIDTH` | 32 | 最终输出数据位宽 |

**派生参数（localparam）**——注释明确警告「改动它们可能引入 bug」，说明这些是设计内部精算好的：

[07_FIR_x2/FIR_x2.v:74-79](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L74-L79) —— 由 `WADDR_WIDTH+1` 得到 `RADDR_WIDTH`，由 `DATA_WIDTH+COEF_WIDTH` 得到 `MULT_WIDTH`。

| localparam | 表达式 | 默认值 | 含义 |
|------------|--------|--------|------|
| `RADDR_WIDTH` | `WADDR_WIDTH + 1` | 9 | 系数 ROM 读地址位宽（深度 512） |
| `MULT_WIDTH` | `DATA_WIDTH + COEF_WIDTH` | 48 | 乘积与累加位宽 |
| `OUTPUT_REG` | `"TRUE"` | — | 传给存储原语，开启输出寄存器 |
| `BUFF_INIT` | `"BUFFER_INIT.hex"` | — | 数据 RAM 初始化文件名 |

**端口声明**——输入是 I2S 风格信号，输出是 2 倍频信号；注意 `DATA_I`/`DATA_O` 都用 `signed` 声明为有符号数：

[07_FIR_x2/FIR_x2.v:60-72](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L60-L72) —— 定义 5 个输入（MCLK/BCK/LRCK/NRST/DATA）和 3 个输出（BCKx2/LRCKx2/DATA）。

最后留意文件首尾的 [`` `default_nettype none ``(第 50 行)](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L50) 与 [`` `default_nettypes wire ``(第 181 行)](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L181)：它强制所有连线必须显式声明（禁止「漏写 wire 时编译器自动补一根」），这是减少笔误的好习惯，也解释了为什么本模块要把所有内部 wire 一一列在 [第 81–98 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L81-L98)。

#### 4.1.4 代码实践

**实践目标**：亲手把「基本参数 → 派生宽度 → 存储深度」的换算走一遍，验证你理解了派生关系。

**操作步骤**：

1. 打开 `07_FIR_x2/FIR_x2_TB.v`，找到顶层实例化（下方链接），记下测试台给定的参数值。
2. 用这些值填下面的表格（手算）。

参考实例化处：[07_FIR_x2/FIR_x2_TB.v:56-70](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L56-L70) —— 测试台实例化 `FIR_x2`，给定 `DATA_WIDTH=32, COEF_WIDTH=16, WADDR_WIDTH=8, COEF_INIT="FIR512_x2_48000.hex"`。

| 待求量 | 表达式 | 你的答案 |
|--------|--------|----------|
| `RADDR_WIDTH` | `WADDR_WIDTH + 1` | ？ |
| `MULT_WIDTH` | `DATA_WIDTH + COEF_WIDTH` | ？ |
| 数据 RAM 深度 | \(2^{\text{WADDR\_WIDTH}}\) | ？ |
| 系数 ROM 深度 | \(2^{\text{RADDR\_WIDTH}}\) | ？ |

**需要观察的现象 / 预期结果**：`RADDR_WIDTH=9`、`MULT_WIDTH=48`、数据 RAM 深度 256、系数 ROM 深度 512。系数 ROM 深度 512 正好对应系数文件名里的 `FIR512`，也对应「滤波器长度 = MCLK/fs」（见 README Notes 第 2 条）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `WADDR_WIDTH` 改成 9，`RADDR_WIDTH` 和系数 ROM 深度分别变成多少？系数文件名里的数字应当是多少？
**答**：`RADDR_WIDTH = 10`，系数 ROM 深度 \(2^{10}=1024\)，对应 `FIR1024`。

**练习 2**：为什么 `MULT_WIDTH = DATA_WIDTH + COEF_WIDTH` 而不是两者之积或更大？
**答**：两个位宽为 \(a\)、\(b\) 的有符号定点数相乘，乘积位宽恰为 \(a+b\)，这是定点乘法的标准结论，无需更大。

---

### 4.2 子模块实例化图谱

#### 4.2.1 概念说明

顶层 `FIR_x2` 把整个滤波器拆成四级，沿音频信号流动方向依次为：

1. **DATA_BUFFER（数据缓冲）**：把输入 PCM 样点存进一个**环形缓冲**（双口 RAM），按地址读出历史样点。
2. **FIR_COEF（系数 ROM）**：输出当前需要的滤波器系数；**同时**产生 2 倍过采样时钟 `LRCKx2`/`BCKx2`。
3. **MULT（乘法器）**：把数据与系数做**有符号乘法**，并带流水线寄存器。
4. **ADD（累加器）**：把一个个乘积累加成卷积和，每个过采样周期结束时输出一个结果并复位。

需要特别理解的一点是**层级关系**：`DATA_BUFFER` 和 `FIR_COEF` 本身也是「封装模块」——它们内部各自再实例化一个**控制器**（产生读写地址）和一个**存储原语**（真正存数据）。以 `DATA_BUFFER` 为例：

[02_DATA_BUFFER/DATA_BUFFER.v:76-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L76-L102) —— `DATA_BUFFER` 内部实例化控制器 `DPRAM_CONT`（产生 WEN/WADDR/REN/RADDR）和存储原语 `SDPRAM_SINGLECLK`（真正读写）。

因此完整的实例化层次是三层：**顶层 → 封装模块 → 控制器 + 存储原语**。控制器与原语的设计在 u3、u4 详述。

#### 4.2.2 核心流程

四级模块的数据/时钟流动如下（这是本讲要建立的「图谱」，4.3 会补上位宽与具体 wire 名）：

```text
                   ┌────────────┐
DATA_I ───────────▶│ DATA_BUFFER│──▶ RDATA ──┐
                   └────────────┘            │
                                              ├──▶ MULT ──▶ MULT_DATA ──▶ ADD ──▶ ADD_DATA ──▶ 输出
MCLK/BCK/LRCK ───▶┌────────────┐             │
                   │ FIR_COEF   │── COEF ────┘
                   └────────────┘
                          │
                          └── LRCKx2/BCKx2 ──▶ MULT ──▶ ADD ──▶ 顶层流水线 ──▶ LRCKx2_O/BCKx2_O
```

要点：

- **数据通路**：`DATA_I → DATA_BUFFER → RDATA` 与 `COEF` 汇入 `MULT`，乘积 `MULT_DATA` 进 `ADD` 累加成 `ADD_DATA`，最后送输出级。
- **时钟通路**：2 倍过采样时钟**不是**用 PLL 生成的，而是由 `FIR_COEF`（更准确地说是其内部的 `SPROM_CONT`）从 `LRCK_I` **派生**出来的，然后逐级**延迟寄存**穿过 `MULT`、`ADD` 和顶层流水线，保证时钟边沿与对应数据在同一时刻到达输出。这条「时钟跟随数据一起打拍」的设计是本项目的精髓，u4/u5 会专门讲。

#### 4.2.3 源码精读

四个实例化集中在 [第 101–165 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L101-L165)。逐一说明参数映射：

**① DATA_BUFFER（输入 PCM 缓冲）**——把顶层基本参数透传下去，初值文件用 `BUFF_INIT`：

[07_FIR_x2/FIR_x2.v:101-115](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L101-L115) —— 实例 `u_DATA_BUFFER`，地址位宽取 `WADDR_WIDTH`，数据位宽取 `DATA_WIDTH`，读出 `RDATA`。

**② FIR_COEF（系数 ROM + 2× 时钟源）**——注意两个细节：一是地址位宽取**派生的** `RADDR_WIDTH`；二是它把自己的 `DATA_WIDTH` 形参接到了顶层的 `COEF_WIDTH`（参数名相同、语义不同，见下文练习）；三是它的复位端接的是 `DUMMY_NRST`（恒为 1，见 4.3）：

[07_FIR_x2/FIR_x2.v:117-132](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L117-L132) —— 实例 `u_FIR_COEF`，输出系数 `COEF` 与过采样时钟 `LRCKx2_COEF/BCKx2_COEF`。

**③ MULT（有符号乘法 + 流水线）**——只传 `DATA_WIDTH` 和 `COEF_WIDTH`。注意 `MULT` 模块其实还有第三个参数 `ROM_ADDR_WIDTH`（默认 9），**顶层并未覆盖它**，因此走默认值：

[07_FIR_x2/FIR_x2.v:134-149](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L134-L149) —— 实例 `u_MULT`，输入 `RDATA/COEF` 与上游时钟，输出乘积 `MULT_DATA` 与延迟后的时钟。

**④ ADD（累加积分 + 周期复位）**——累加位宽取 `MULT_WIDTH`，地址位宽取 `WADDR_WIDTH`（用于判断滤波器规模，进而决定 BCKx2 的生成方式）：

[07_FIR_x2/FIR_x2.v:151-165](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L151-L165) —— 实例 `u_ADD`，输出累加和 `ADD_DATA` 与延迟后的时钟 `LRCKx2O_wire/BCKx2O_wire`。

实例图谱汇总：

| 实例 | 模块 | 职责 | 关键参数映射 |
|------|------|------|--------------|
| `u_DATA_BUFFER` | DATA_BUFFER | 输入 PCM 环形缓冲 | `ADDR_WIDTH←WADDR_WIDTH` |
| `u_FIR_COEF` | FIR_COEF | 系数 ROM + 生成 2× 时钟 | `ADDR_WIDTH←RADDR_WIDTH`、`DATA_WIDTH←COEF_WIDTH` |
| `u_MULT` | MULT | 有符号乘法 + 流水线 | `DATA_WIDTH←DATA_WIDTH`、`COEF_WIDTH←COEF_WIDTH` |
| `u_ADD` | ADD | 累加积分 + 周期复位 | `MULT_WIDTH←MULT_WIDTH`、`RAM_ADDR_WIDTH←WADDR_WIDTH` |

#### 4.2.4 代码实践

**实践目标**：通过对照实例化代码，发现两处「不显眼但重要」的参数细节，锻炼读图能力。

**操作步骤**：

1. 打开 [FIR_COEF.v 的端口声明](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L51-L69)，确认它内部那个形参确实叫 `DATA_WIDTH`。
2. 回到顶层 [第 119–124 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L119-L124)，确认 `.DATA_WIDTH(COEF_WIDTH)` 这一行——形参名是 `DATA_WIDTH`，实参却是顶层的 `COEF_WIDTH`。
3. 打开 [MULT.v 的参数声明](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/05_MULT/MULT.v#L51-L56)，数一下它有几个参数；再回到顶层 [第 136–139 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L136-L139)，数一下顶层覆盖了几个。

**需要观察的现象 / 预期结果**：

- 第 2 步：`FIR_COEF` 的「系数字宽」形参虽然也叫 `DATA_WIDTH`，但接的是顶层的 `COEF_WIDTH(16)`，**不是**顶层的 `DATA_WIDTH(32)`。这是参数名重用导致的陷阱——只看名字会被骗，必须看连接的实参。
- 第 3 步：`MULT` 声明了 3 个参数（`DATA_WIDTH/COEF_WIDTH/ROM_ADDR_WIDTH`），顶层只覆盖前 2 个，`ROM_ADDR_WIDTH` 走默认值 9。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FIR_COEF` 的地址位宽用 `RADDR_WIDTH`，而 `DATA_BUFFER` 的地址位宽用 `WADDR_WIDTH`？
**答**：系数 ROM 要装下全部抽头（深度 \(2^{\text{RADDR\_WIDTH}}\)），数据 RAM 只需装一个多相分支的样点（深度 \(2^{\text{WADDR\_WIDTH}}\)），前者是后者的 2 倍，所以地址位宽多 1 位。

**练习 2**：顶层给 `u_MULT` 没有传 `ROM_ADDR_WIDTH`，这会带来什么后果？如果默认值与实际不符会怎样？
**答**：`ROM_ADDR_WIDTH` 取模块默认值 9。它与 `WADDR_WIDTH` 在小规模滤波器下共同决定 BCKx2 的生成分支；若默认值与实际滤波器规模不符，可能导致 `BCKx2` 输出异常。这正是顶层对 `BCKx2_O` 做条件选择的原因（见 4.3）。

---

### 4.3 内部连线与输出赋值

#### 4.3.1 概念说明

端口和实例确定后，剩下的事就是**用内部 wire 把四个实例串起来**，再做最后的输出处理。本小节要建立两个视图：

- **数据连线视图**：哪根 wire 把哪个实例的输出送到哪个实例的输入，位宽是多少。
- **输出赋值视图**：顶层唯一的 `always` 块做最后一级流水线（含饱和/舍入），再用 `assign` 把结果送到输出端口。

此外有一个容易忽略却很关键的设计选择：`FIR_COEF` 的复位端被**永久接高**（`DUMMY_NRST=1'b1`，即永远不复位），而数据通路上的 `DATA_BUFFER/MULT/ADD` 都接真实 `NRST_I`。原因是系数 ROM 内容静态不变、无需复位清零，而数据通路必须在上电时清除残留样点。

#### 4.3.2 核心流程

内部 wire 的拓扑（含位宽）：

```text
数据通路（位宽）：
DATA_I[32] ─▶ DATA_BUFFER ─▶ RDATA[32] ─────────────┐
                                                      ▼
MCLK/BCK/LRCK ─▶ FIR_COEF ─▶ COEF[16] ─────────────▶ MULT ─▶ MULT_DATA[48] ─▶ ADD ─▶ ADD_DATA[48]
                                                      ▲                                 │
                                                      │                                 ▼
                                                   (乘积)                         [饱和/舍入 48→32]
                                                                                         │
                                                                                         ▼
                                                                                    DATA_O[32]

时钟通路（被逐级延迟寄存）：
FIR_COEF ── LRCKx2_COEF/BCKx2_COEF ──▶ MULT ── LRCKx2_MULT/BCKx2_MULT ──▶ ADD ── LRCKx2O_wire/BCKx2O_wire
                                                                                       │
                                                                                       ▼
                                                                          顶层 always（再打 1 拍）
                                                                                       │
                                                                                       ▼
                                                                            BCKx2O_REG/LRCKx2O_REG
                                                                                       │
                                                                                       ▼
                                                                              BCKx2_O / LRCKx2_O
```

两个要点：

- **数据位宽演化**：32 位数据 × 16 位系数 = 48 位乘积，累加后仍是 48 位（累加器不扩位，靠系数量化保证不溢出，详见 u5/u6），最后由饱和舍入级截回 32 位。
- **时钟随数据打拍**：过采样时钟从 `FIR_COEF` 出发，每经过一级模块就多延迟若干拍，目的是让时钟边沿与对应数据「同步抵达」下一级；顶层 `always` 再补一拍，使最终的 `DATA_O` 与 `LRCKx2_O` 对齐。

#### 4.3.3 源码精读

**内部 wire 声明**——所有连线都集中在此，建议把它当成「接线表」来读：

[07_FIR_x2/FIR_x2.v:81-98](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L81-L98) —— 声明 `RDATA[32]/COEF[16]/MULT_DATA[48]/ADD_DATA[48]` 等数据线，以及 `LRCKx2_COEF/BCKx2_COEF/LRCKx2_MULT/BCKx2_MULT` 等时钟线。

其中 [第 92 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L92) 的 `assign DUMMY_NRST = 1'b1;` 把系数 ROM 的复位永久接高（即不复位），并在 [第 128 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L128) 把它接到 `u_FIR_COEF` 的 `NRST_I`。

**最终流水线寄存器（顶层唯一的 always 块）**——把过采样时钟再打一拍，并对累加结果做饱和/舍入：

[07_FIR_x2/FIR_x2.v:167-172](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L167-L172) —— `BCKx2O_REG/LRCKx2O_REG` 继续延迟时钟；`DATAO_REG` 用一个三元表达式做饱和判断后取 `DATAO_WIDTH` 位。

第 171 行的饱和/舍入逻辑（默认 `MULT_WIDTH=48`、`DATAO_WIDTH=32`）可这样理解：

- 正常支路：取 `ADD_DATA[45:14]` 共 32 位（高位 `[47:46]` 是保护位，低位 `[13:0]` 被舍去），相当于把 48 位累加结果**右移 14 位**后取低 32 位。
- 饱和支路：当保护位 `ADD_DATA[46]` 与新符号位 `ADD_DATA[45]` 不一致时，说明溢出，输出被钳位到 32 位的最大/最小值。

> 这一行的逐位推导、溢出方向、以及与系数量化（`fir_gen.py` 的 `MAX_TOTAL` 检查）的关系，是 **u5-l3「输出饱和与定点舍入」** 的主题，本讲只需知道「这里做了饱和+截位」即可。

**输出赋值**——把寄存器送到端口，其中 `BCKx2_O` 有一条有意思的分支：

[07_FIR_x2/FIR_x2.v:174-177](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L174-L177) —— `LRCKx2_O/DATA_O` 直接来自寄存器；`BCKx2_O` 在 `WADDR_WIDTH >= 7` 时取流水线结果，否则直接取 `MCLK_I`。

`BCKx2_O = (WADDR_WIDTH >= 7) ? BCKx2O_REG : MCLK_I;` 这一行表明：当滤波器规模较大（`WADDR_WIDTH >= 7`，即数据缓冲深度 ≥128）时，`BCKx2` 走完整的派生+延迟通路；规模较小时则直接用 `MCLK_I` 充当 2 倍位时钟。其深层原因涉及 `SPROM_CONT`/`ADD` 内部对 BCKx2 的条件生成（见 u4-l2、u5-l2），本讲只需记住这一选择的存在。

#### 4.3.4 代码实践

**实践目标**：完成本讲的 headline 任务——追踪一个 PCM 样点从 `DATA_I` 到 `DATA_O` 的完整通路，画出标注位宽的数据通路图。

**操作步骤**：

1. 在 [FIR_x2.v 第 81–98 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L81-L98) 找到所有内部 wire，记录每根的位宽。
2. 顺着四个实例（[101–165 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L101-L165)），把每根 wire「谁驱动、谁接收」填入下表。
3. 用箭头画出从 `DATA_I` 到 `DATA_O` 的链路，并在每段标注位宽。

| 内部 wire | 位宽 | 驱动者（来自哪个实例/端口） | 接收者（去往哪个实例/端口） |
|-----------|------|------------------------------|------------------------------|
| `RDATA` | ? | `u_DATA_BUFFER.RDATA_O` | `u_MULT.DATA_I` |
| `COEF` | ? | ？ | ？ |
| `MULT_DATA` | ? | ？ | ？ |
| `ADD_DATA` | ? | ？ | 顶层 always（饱和/舍入） |

**需要观察的现象 / 预期结果**：补全后应得到一条 `DATA_I[32] → RDATA[32] →(与 COEF[16] 相乘)→ MULT_DATA[48] → ADD_DATA[48] →(饱和舍入)→ DATAO_REG[32] → DATA_O[32]` 的链路；同时系数支路为 `FIR_COEF → COEF[16] → u_MULT.COEF_I`。对照 4.3.2 的示意图核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DUMMY_NRST` 恒为 1？如果让 `FIR_COEF` 也跟随真实 `NRST_I` 复位会怎样？
**答**：系数 ROM 内容是静态的，复位与否不影响系数正确性；让系数通路参与复位反而会在复位期间扰乱系数读出时序。数据通路则必须复位以清除残留样点，所以两者复位策略不同。

**练习 2**：累加器 `ADD_REG` 用的是 `MULT_WIDTH`（48）位，而卷积最多累加 256 个乘积，为什么不需要更宽的累加器？
**答**：设计依赖系数生成阶段（`fir_gen.py` 的 `MAX_TOTAL` 溢出检查）保证「奇/偶抽头系数之和」不超过 16 位有符号范围，从而保证累加和不会超出 48 位。这正是 u6-l1 会讲的内容。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一张**完整的顶层架构图**。要求：

1. **画出三层实例化层次**：顶层 `FIR_x2` → 四个实例（`DATA_BUFFER/FIR_COEF/MULT/ADD`）→ 其中 `DATA_BUFFER` 与 `FIR_COEF` 再向下各含一个控制器 + 一个存储原语（参考 [DATA_BUFFER.v 第 76–102 行](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L76-L102)）。
2. **在数据通路上标注位宽**：`DATA_I[32] → RDATA[32] →（×COEF[16]）→ MULT_DATA[48] → ADD_DATA[48] → DATAO_REG[32] → DATA_O[32]`。
3. **单独画出时钟通路**：`FIR_COEF` 产生的 `LRCKx2/BCKx2` 如何逐级延迟穿过 `MULT → ADD → 顶层 always → 输出端口`。
4. **在图上标出两处特别设计**：`FIR_COEF` 复位端接 `DUMMY_NRST`（恒 1）；`BCKx2_O` 在 `WADDR_WIDTH >= 7` 时走派生通路、否则取 `MCLK_I`。

完成后，你应当能用这张图向别人解释「一个输入样点在 `FIR_x2` 内部依次经过了哪些模块、每级位宽如何变化、过采样时钟从哪来又如何与数据对齐」。

> 如果你能在 Questa 中跑通 u1-l3 的顶层仿真（[FIR_x2.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat)），可以在波形里对照验证：`u_MULT` 的 `DATA_I`/`COEF_I` 与 `DATA_O` 是否符合同一拍相乘、下一拍出结果的流水线关系。如暂无仿真环境，本实践可作为「源码阅读型实践」完成——只画图、对照源码核对即可。

## 6. 本讲小结

- `FIR_x2.v` 是一个**近乎纯结构化**的顶层模块：端口 + 参数 + 四个实例 + 一段输出流水线，几乎没有算法逻辑。
- 端口是 I2S 风格输入与 2 倍频输出；五个 `parameter`（`DATA_WIDTH/COEF_WIDTH/WADDR_WIDTH/COEF_INIT/DATAO_WIDTH`）可配置，`RADDR_WIDTH`/`MULT_WIDTH` 等由 `localparam` 派生。
- 关键派生关系：`RADDR_WIDTH = WADDR_WIDTH + 1`（系数 ROM 深度是数据 RAM 的 2 倍，源于 2× 过采样的多相分解）；`MULT_WIDTH = DATA_WIDTH + COEF_WIDTH`。
- 四个实例沿信号流串联：`DATA_BUFFER`（缓冲）→ `FIR_COEF`（系数 + 2× 时钟源）→ `MULT`（乘法）→ `ADD`（累加）。
- 过采样时钟由 `FIR_COEF` 派生，并随数据**逐级延迟寄存**穿过 `MULT/ADD/顶层`，保证数据与时钟同步抵达输出。
- 顶层唯一的 `always` 做最后一级流水线（含饱和/舍入）；`BCKx2_O` 依 `WADDR_WIDTH >= 7` 选择派生通路或直接取 `MCLK_I`。

## 7. 下一步学习建议

本讲建立了顶层骨架与数据通路全景。接下来建议**沿信号流逐级下钻**：

- 想搞清「输入样点如何被存成环形缓冲、地址如何产生」→ 学习 **u3（输入数据通路：环形缓冲与双口 RAM）**，先看 `u3-l1 DATA_BUFFER` 与 `u3-l2 DPRAM_CONT`。
- 想搞清「系数如何按奇偶多相寻址、2× 时钟如何从 LRCK 派生」→ 学习 **u4（系数通路与 2 倍过采样时钟生成）**，重点看 `u4-l2 SPROM_CONT`。
- 想搞清「乘法流水线、累加复位、最终饱和舍入」→ 学习 **u5（运算通路：乘法、累加与输出饱和）**，其中 `u5-l3` 会逐位拆解本讲第 171 行的饱和表达式。
- 想搞清「系数量化如何保证累加不溢出」→ 学习 **u6-l1（FIR 系数生成：firwin 与多相奇偶抽头分解）**。
