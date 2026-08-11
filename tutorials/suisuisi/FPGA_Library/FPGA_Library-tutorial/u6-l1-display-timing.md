# 显示时序与 VGA/720p 信号生成

## 1. 本讲目标

本讲聚焦 `ThreePart/projf-explore/lib/display/` 下的三个显示时序生成模块：`display_480p`、`display_720p`、`display_24x18`。学完后你应当能够：

- 说清一幅画面在时间上是怎么「扫」出来的——**行（line）**、**场（frame）**、**有效区（active）**、**消隐期（blanking）** 这四件事的几何与时间关系；
- 把「**Active / Front Porch / Sync / Back Porch**」四个区间与同步**极性（polarity）** 对上号，并用「像素时钟 ÷ 一帧总像素数 = 帧率」这个公式算出刷新率；
- 看懂 `display_480p.sv` 的源码：它用一个**带符号坐标系**把有效区摆到 `0..H_RES-1`、把消隐期摆到负坐标，再用四个 `always_ff` 块分别生成同步、控制、坐标、延迟输出；
- 理解三个模块为什么**代码骨架完全相同、只是参数不同**，从而能举一反三地自己改出 720p、1080p 或自定义分辨率；
- 自己写一个最小 testbench，对 `display_480p`（或更小的 `display_24x18`）跑仿真，在波形里验证「一行内 sync 与 active 的时间关系」。

承接 [u5-l2](u5-l2-clock-domain-crossing.md)（时钟生成与跨时钟域，本讲的**像素时钟**就来自那里的 PLL）和 [u5-l3](u5-l3-memory-rom-ram-bram.md)（存储器与 BRAM，本讲的坐标输出是后续帧缓冲寻址的基础），本讲进入 projf 七大分区中的 **display 分区**，是整个 Unit 6 FPGA 图形与显示系统的第一块基石。

## 2. 前置知识

### 2.1 显示器为什么需要「时序」

CPU 给显示器发一个像素值，显示器怎么知道这个像素该落在屏幕的哪个位置？答案出乎意料地复古：**今天的数字显示器（HDMI/DP）依然在模仿 20 世纪阴极射线管（CRT）显示器的扫描方式**。

CRT 内部有一支电子枪，它从屏幕**左上角**开始，**从左到右**逐像素点亮一行，到达右端后迅速**回扫**到下一行左端（这个回扫的空档叫**水平消隐**），如此逐行向下；扫完最后一行后，再迅速**回到左上角**开始下一帧（这个空档叫**垂直消隐**）。整个过程像读一本书：从左到右读一行，换行，读完一页翻页。

电子枪在「回扫」时必须**关掉**（不能在屏幕上画出一条回扫的白线），所以每一行、每一帧里都有一段「不发数据」的空白时间——这就是**消隐期（blanking）**。即便今天的液晶/OLED 屏根本没有电子枪，这套带消隐期的时序约定仍然被 HDMI/DVI/VGA 接口完整保留，因为整个视频产业链都建立在它之上。

> 一句话记忆：**画面不是「一次性贴上去」的，而是「逐像素扫出来」的；扫描需要回扫时间，于是每行每帧都有一段不发像素的消隐期。**

### 2.2 一行的四个区间

把「一行」在时间上展开（横轴是像素时钟周期，每个周期发一个像素），一行由四个区间拼接而成：

| 区间 | 英文 | 含义 | 本仓库参数名 |
| --- | --- | --- | --- |
| 有效区 | Active | 真正显示像素的区域 | `H_RES` |
| 前沿 | Front Porch | 有效区结束→同步脉冲开始之间的缓冲 | `H_FP` |
| 同步脉冲 | Sync | 触发「回扫」的脉冲 | `H_SYNC` |
| 后沿 | Back Porch | 同步脉冲结束→下一行有效区开始之间的缓冲 | `H_BP` |

顺序是：**Active → Front Porch → Sync → Back Porch →（下一行）Active**。前沿、同步、后沿合起来就是**水平消隐期**（这段时间不发有效像素）。

垂直方向（一帧）完全同理，只是把「像素」换成「行」：`V_RES` 行有效区 + `V_FP` + `V_SYNC` + `V_BP` 行消隐期。

### 2.3 同步极性（Polarity）

同步脉冲可以是**高有效**（脉冲期间为高电平，平时低），也可以是**低有效**（脉冲期间为低电平，平时高）。到底用哪种，由**极性参数**决定：

- 本仓库里 `H_POL=0` 表示负极性（脉冲为低），`H_POL=1` 表示正极性（脉冲为高）。垂直方向 `V_POL` 同理。
- 历史/标准上的约定：VGA（640×480）的行场同步都是**负**极性；720p/1080p 的行场同步都是**正**极性。本仓库的参数默认值正好对应这个约定。

### 2.4 像素时钟与帧率

显示时序本质是一个**固定频率的像素时钟（pixel clock）** 驱动的计数器。每个时钟周期发一个像素（含消隐期内的「空像素」）。一帧要发的总像素数为：

\[
\text{Total} = \underbrace{(H\_RES + H\_FP + H\_SYNC + H\_BP)}_{\text{一行总像素 } H\_total} \times \underbrace{(V\_RES + V\_FP + V\_SYNC + V\_BP)}_{\text{一帧总行数 } V\_total}
\]

于是**帧率（刷新率）** 为：

\[
f_{frame} = \frac{f_{pix}}{H\_total \times V\_total}
\]

比如标准 VGA 640×480@60Hz：\(H\_total = 640+16+96+48 = 800\)，\(V\_total = 480+10+2+33 = 525\)，像素时钟取 25.175 MHz，则：

\[
f_{frame} = \frac{25\,175\,000}{800 \times 525} = \frac{25\,175\,000}{420\,000} \approx 59.94 \text{ Hz}
\]

（标准 VGA 的「60 Hz」实际是 59.94 Hz，这就是为什么有时被写成 59.9 Hz。）后面我们会用本仓库的 `display_480p` 参数把这道题再算一遍。

### 2.5 三个 SystemVerilog 关键字回顾

projf 库只用一个很小的 SystemVerilog 子集（见 [u5-l1](u5-l1-verilog-library-overview.md)），本讲反复用到：

- `logic`：统一数据类型，端口和内部寄存器都用它；
- `always_ff @(posedge clk_pix)`：**时序逻辑**，每个像素时钟沿更新一次，综合成触发器（带来一拍延迟）；
- `signed`：本讲最关键的关键字——坐标被声明为 `logic signed`，于是负数有了意义（用来表示消隐期），下文详述。

> 永久链接的固定前缀为：
> `https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/`

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `lib/display/display_480p.sv` | 93 | 640×480@60Hz（标准 VGA）时序生成，负极性同步 |
| `lib/display/display_720p.sv` | 93 | 1280×720@60Hz（720p）时序生成，正极性同步 |
| `lib/display/display_24x18.sv` | 96 | 24×18 超小测试分辨率，专供 testbench 快速仿真 |
| `lib/display/README.md` | 33 | display 分区索引，列出全部模块清单 |

辅助参考（用于实践与真实用法对照）：

| 文件 | 作用 |
| --- | --- |
| `lib/display/xc7/display_480p_tb.sv` | 仓库自带的 480p testbench（XC7/Vivado，含真实 MMCM 像素时钟） |
| `lib/display/xc7/display_720p_tb.sv` | 仓库自带的 720p testbench |
| `lib/clock/xc7/clock_480p.sv` | 480p 像素时钟生成（25.2 MHz，承接 u5-l2 的 MMCM） |
| `lib/display/display_1080p.sv` | 1080p 时序模块（结构与本讲三个模块完全相同，可作延伸阅读） |

## 4. 核心概念与源码讲解

### 4.1 显示时序参数：光栅扫描与四个区间

#### 4.1.1 概念说明

这一节没有代码，只有「约定」。因为显示时序的参数（多少像素有效、多少像素消隐、同步多宽、什么极性）并不是哪个工程师拍脑袋定的，而是**工业标准**（VESA、CTA-861 等）规定好的——显示器只认这套参数，你发错了，画面就会偏移、抖动甚至黑屏。

所以读显示时序模块的第一步，是先建立一张「参数表」的直觉：每个分辨率都对应一组固定的 `RES / FP / SYNC / BP / POL`，外加一个对应的像素时钟频率。本仓库的 `display_480p` 注释就直说了它是「传统 VGA 时序」：

[ThreePart/projf-explore/lib/display/display_480p.sv:1-3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L1-L3)
> 模块头注释标明这是 Project F 的 640×480p60 显示模块，MIT 许可。

> ⚠️ **一个容易混淆的点**：本讲规格里把 `display_480p` 描述成「800×600」。实际源码里它是 **640×480@60Hz**（标准 VGA）。这里的「800」很可能指的是**一行含消隐的总像素数**（640 + 16 + 96 + 48 = 800），而不是有效分辨率。**本讲一律以源码实际参数为准**——有效分辨率是 640×480。

#### 4.1.2 核心流程

把一行的四个区间画成时间轴（横轴 = 像素时钟周期）：

```
一行 (H_total = H_RES + H_FP + H_SYNC + H_BP 个像素时钟)
├──── Active (H_RES) ────┤├ FP ├├── Sync (H_SYNC) ──┤├── BP ──┤
   ↑发有效像素↑              ↑回扫脉冲↑
   |<--       有效区        -->|<--     水平消隐期            -->|
```

模块在每个像素时钟周期做四件事：

1. 维护两个计数器 `x`（列位置）、`y`（行位置），每个时钟 `x+1`，到行末则 `x` 归零、`y+1`，到帧末则 `y` 归零；
2. 根据 `x` 是否落在 `[HS_STA, HS_END)` 区间内，结合极性 `H_POL`，生成 `hsync`；
3. 根据 `y` 是否落在有效区，结合 `x` 是否落在有效区，生成 `de`（数据有效）、`frame`（帧起始）、`line`（行起始）；
4. 把 `x/y` 延迟一拍输出为 `sx/sy`，让坐标与上述控制信号对齐。

#### 4.1.3 源码精读：参数就是一张时序表

四个区间参数 + 两个极性参数，全部集中在模块的 `parameter` 列表里。先看 `display_480p`：

[ThreePart/projf-explore/lib/display/display_480p.sv:8-20](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L8-L20)
> 480p 的全部时序参数：`H_RES=640 / V_RES=480`（有效区），`H_FP=16 / H_SYNC=96 / H_BP=48`（水平三段），`V_FP=10 / V_SYNC=2 / V_BP=33`（垂直三段），`H_POL=0 / V_POL=0`（行场同步均为**负极性**，这正是 VGA 的约定）。`CORDW=16` 是坐标位宽。

对照标准 VGA 640×480@60Hz 的 VESA 参数表，这组值一字不差。同样地看 720p：

[ThreePart/projf-explore/lib/display/display_720p.sv:8-20](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_720p.sv#L8-L20)
> 720p 参数：`H_RES=1280 / V_RES=720`，`H_FP=110 / H_SYNC=40 / H_BP=220`，`V_FP=5 / V_SYNC=5 / V_BP=20`，`H_POL=1 / V_POL=1`（行场同步均为**正极性**，这是 720p/1080p 的约定）。

把两者的关键数字并排放：

| 参数 | 480p（VGA） | 720p（HD） | 含义 |
| --- | --- | --- | --- |
| `H_RES` × `V_RES` | 640 × 480 | 1280 × 720 | 有效分辨率 |
| `H_FP` / `H_SYNC` / `H_BP` | 16 / 96 / 48 | 110 / 40 / 220 | 水平 前沿/同步/后沿 |
| `V_FP` / `V_SYNC` / `V_BP` | 10 / 2 / 33 | 5 / 5 / 20 | 垂直 前沿/同步/后沿 |
| `H_total`（含消隐一行） | 800 | 1650 | = H_RES+H_FP+H_SYNC+H_BP |
| `V_total`（含消隐一帧） | 525 | 750 | = V_RES+V_FP+V_SYNC+V_BP |
| `H_POL` / `V_POL` | 0 / 0（负） | 1 / 1（正） | 同步极性 |
| 像素时钟 | 25.175 MHz | 74.25 MHz | 由 PLL 生成 |

#### 4.1.4 代码实践：用参数算帧率

**实践目标**：用 4.1.3 的参数表，手算 480p 与 720p 的帧率，验证它们都约等于 60 Hz。

**操作步骤**：

1. 对 480p：\(H\_total = 640+16+96+48 = 800\)，\(V\_total = 480+10+2+33 = 525\)，像素时钟取本仓库 `clock_480p` 生成的 25.2 MHz（见 4.2.3）。
2. 对 720p：\(H\_total = 1280+110+40+220 = 1650\)，\(V\_total = 720+5+5+20 = 750\)，像素时钟取标准 74.25 MHz。
3. 代入 \(f_{frame} = f_{pix} / (H\_total \times V\_total)\)。

**预期结果**：

- 480p：\(25\,200\,000 / (800 \times 525) = 25\,200\,000 / 420\,000 = 60.0\) Hz。（本仓库 PLL 取 25.2 MHz，正好算出整 60 Hz；若用标准 25.175 MHz 则为 59.94 Hz。）
- 720p：\(74\,250\,000 / (1650 \times 750) = 74\,250\,000 / 1\,237\,500 = 60.0\) Hz。

两个分辨率帧率都精确等于 60 Hz，这正是这些参数被标准化选中的原因——它们能让像素时钟取一个「干净的」频率时整除出 60 Hz。

> 「待本地验证」的部分：如果你换一块板子、改了像素时钟频率，帧率也会变；上板后可用示波器量 `hsync` 频率（应等于 \(f_{pix}/H\_total\)，480p 约 31.5 kHz）来核对。

#### 4.1.5 小练习与答案

**练习 1**：`display_24x18` 的注释说自己「24x18 (432) active pixels, 35x30 (1050) pixels inc. blanking」。请用它的参数验证这两个数字。

参考答案：参数为 `H_RES=24, H_FP=3, H_SYNC=4, H_BP=4`，故 \(H\_total = 24+3+4+4 = 35\)；`V_RES=18, V_FP=3, V_SYNC=2, V_BP=7`，故 \(V\_total = 18+3+2+7 = 30\)。有效像素 \(24 \times 18 = 432\)，含消隐总像素 \(35 \times 30 = 1050\)，与注释完全吻合。

**练习 2**：如果一块老式 CRT 显示器只支持**负极性**同步，而你想输出 720p，能直接用 `display_720p` 的默认参数吗？为什么？

参考答案：不能直接用。`display_720p` 默认 `H_POL=1, V_POL=1`（正极性），而老 CRT 要负极性。好在极性是参数，例化时传 `H_POL=0, V_POL=0` 即可（但同步极性通常和具体分辨率绑定，乱改可能导致显示器拒绝同步——标准本身才是稳妥的）。

### 4.2 display_480p：带符号坐标系与共享模块骨架

#### 4.2.1 概念说明

参数表只是「配置」，真正干活的是模块体。projf 的设计有一个非常聪明的点——**它把屏幕坐标定义成带符号数（signed），让有效区正好落在 `0 .. H_RES-1`，把消隐期摆到负坐标**。

这样做的好处巨大：下游所有绘图模块想知道「当前像素在不在有效区」，只要判断 `sx >= 0`（或直接用模块输出的 `de` 信号）；想知道「当前像素在第几列」，直接读 `sx` 就是 0 起始的列号，无需再减去消隐宽度。负坐标天然地「藏」起了消隐期。

更重要的是，`display_480p`、`display_720p`、`display_24x18`（以及未在本讲精读的 `display_1080p`）**共享完全相同的模块体**，区别只是 `parameter` 默认值。理解了 480p，就理解了全部。

#### 4.2.2 核心流程

先看带符号坐标是怎么从参数推出来的。对水平方向（480p 为例）：

```
坐标轴 (x, 带符号):
  H_STA          HS_STA      HS_END      HA_STA        HA_END
   -160           -144         -48         0             639
    |── FP (16) ──|── Sync(96) ─|── BP(48) ─|── Active(640) ──|
    ↑                                ↑                          ↑
  行起始(回扫区)                   有效区起点                  行末,回卷到 H_STA
```

关键派生关系（全部是 `localparam signed`，编译期常量）：

- `H_STA = -(H_FP + H_SYNC + H_BP)`：一行的起点（最负的坐标），消隐从这里开始；
- `HS_STA = H_STA + H_FP`：同步脉冲起点（前沿走完）；
- `HS_END = HS_STA + H_SYNC`：同步脉冲终点；
- `HA_STA = 0`：有效区起点（固定为 0）；
- `HA_END = H_RES - 1`：有效区终点。

垂直方向完全对称。模块主体用四个 `always_ff` 块实现四件事，下面逐块精读。

#### 4.2.3 源码精读

**(a) 派生常量**：水平与垂直各五个 `localparam`，把「起点/同步起止/有效起止」从原始参数算出来。

[ThreePart/projf-explore/lib/display/display_480p.sv:32-44](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L32-L44)
> 水平 5 个 + 垂直 5 个 `localparam signed`。注意 `HA_STA=0`、`VA_STA=0`——这就是「有效区从坐标 0 开始」的契约。480p 的 `H_STA = -(16+96+48) = -160`，`HS_STA = -144`，`HS_END = -48`。

**(b) 同步信号生成**：根据当前 `x/y` 是否落在同步窗口内，结合极性生成 `hsync/vsync`。

[ThreePart/projf-explore/lib/display/display_480p.sv:48-56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L48-L56)
> 极性用一个三元运算符搞定：`H_POL ? (在窗口内) : ~(在窗口内)`。480p 的 `H_POL=0`，所以 `hsync = ~(在窗口内)`——在同步窗口外为高、窗口内为低，即**负极性**（脉冲是低电平）。复位时给空闲电平（`H_POL=0` 时复位为 1，即空闲高）。

**(c) 控制信号生成**：`de`（数据有效）、`frame`（帧起始）、`line`（行起始）。

[ThreePart/projf-explore/lib/display/display_480p.sv:58-68](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L58-L68)
> `de = (y>=0 && x>=0)`：只有当坐标进入有效区（行场都不为负）才拉高，下游据此决定发像素还是发黑。`frame` 在每帧第一个像素（`y==V_STA && x==H_STA`，即左上角回扫起点）高一拍，常用于动画/双缓冲的「换帧」触发。`line` 在每行起点（`x==H_STA`）高一拍，常用于 linebuffer 的「换行」加载。

**(d) 坐标计数器**：核心扫描逻辑——`x` 每拍 +1，到行末回卷并让 `y` 前进。

[ThreePart/projf-explore/lib/display/display_480p.sv:70-82](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L70-L82)
> `x == HA_END`（有效区最后一列，480p 是 639）时，`x` 回卷到 `H_STA`（-160），并判断是否也到了最后一行（`y == VA_END`，即 479），是则 `y` 回卷到 `V_STA`，否则 `y+1`。否则 `x <= x+1`。复位时 `x/y` 都回到各自的 `H_STA/V_STA`（最负值）。

这里值得停下来想一下：回卷条件是 `x == HA_END`（639），那 639 之后岂不直接跳回 -160，FP/Sync/BP 期间的坐标从哪来？答案在于：`H_STA = -(H_FP+H_SYNC+H_BP) = -160`，而从 -160 走到 639 正好是 \(160 + 640 = 800\) 拍，等于一整行 `H_total`。所以「回卷到 -160 再 +1 走到 639」本身就**经过了**那段负坐标区间（FP/Sync/BP 就藏在 -160..-1 里）。换言之，回卷到 `H_STA` 后，计数器要走过 160 拍负坐标（消隐）才再次到达有效区——这正是消隐期。理解了这一点，整个模块就通透了。

**(e) 延迟输出 sx/sy**：

[ThreePart/projf-explore/lib/display/display_480p.sv:84-92](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L84-L92)
> `sx <= x; sy <= y` 把坐标打一拍再输出。**为什么要打这一拍？** 因为上面的 `hsync/vsync/de/frame/line` 全部是 `always_ff` 寄存器输出，它们描述的是「上一拍的 x/y」；如果 `sx/sy` 直接用组合 `assign sx = x`，坐标会比控制信号快一拍，于是「de 高电平那拍，sx 却指向下一个像素」——错位。把 `sx/sy` 也寄存一拍，就保证「`de`、`hsync`、`sx`、`sy` 在同一拍描述同一个像素」。

**(f) 端口总览**：把模块看作一个黑盒，它吃像素时钟，吐出行场同步 + 坐标 + 控制脉冲。

[ThreePart/projf-explore/lib/display/display_480p.sv:21-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_480p.sv#L21-L30)
> 输入只有 `clk_pix`（像素时钟）和 `rst_pix`（像素时钟域复位）；输出八条线：`hsync/vsync`（同步）、`de`（数据有效）、`frame/line`（帧/行起始脉冲）、`sx/sy`（带符号坐标，16 位）。下游绘图模块只消费这些信号就能知道「现在该给屏幕哪个像素上什么颜色」。

**像素时钟从哪来**：模块只认 `clk_pix`，它本身不生成时钟。480p 的 25.2 MHz 像素时钟由 `clock_480p` 用 MMCM 从 100 MHz 板载时钟生成（承接 u5-l2 的 PLL 内容）：

[ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv:8-23](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L8-L23)
> 头注释写明「用 100 MHz 输入生成 25.2 MHz（640×480 60Hz）」。`MULT_MASTER=31.5, DIV_MASTER=5` 把 VCO 推到 \(100 \times 31.5 / 5 = 630\) MHz，再 `DIV_1X=25` 分频得 \(630/25 = 25.2\) MHz。`clk_pix_locked` 用来告诉显示模块「时钟已稳定」。

#### 4.2.4 代码实践：画出一行内 sync 与 active 的时间关系

**实践目标**：读源码，把 480p 一行（800 个像素时钟）内 `hsync`、`de`、坐标 `sx` 的时间关系画出来——这是本讲规格点名的核心任务。

**操作步骤**：

1. 取 480p 参数，按 4.2.2 的派生关系，填出下表的「坐标范围」一列（以 `sx` 为准，因为它是对齐后的输出坐标）：

| 区间 | 坐标范围 `sx` | 宽度（像素时钟） | `hsync`（负极性） | `de` |
| --- | --- | --- | --- | --- |
| 前沿 FP | -160 .. -145 | 16 | 1（高，空闲） | 0 |
| 同步 Sync | -144 .. -49 | 96 | **0（低，脉冲）** | 0 |
| 后沿 BP | -48 .. -1 | 48 | 1（高，空闲） | 0 |
| 有效 Active | 0 .. 639 | 640 | 1（高，空闲） | **1** |

2. 据此画出时间轴（横轴 = 像素时钟周期，纵轴 = 信号电平）：

```
sx:   -160 ... -145 |-144 ... -49 | -48 ... -1 | 0 ............ 639 |(回卷)-160...
           FP             Sync           BP            Active
hsync: ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾____________________________‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾
                       (负极性: 同步窗口内为低)
de:    ___________________________________________‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾
                                                    (仅有效区为高)
```

3. 验证三个关键性质：
   - 一行内 `hsync` 的低电平（脉冲）宽度恰为 `H_SYNC=96` 个时钟；
   - `de` 的高电平宽度恰为 `H_RES=640` 个时钟；
   - `hsync` 脉冲结束后还要经过 `H_BP=48` 个时钟的后沿，`de` 才升高（进入有效区）。

**预期结果**：上表与时间轴就是答案。注意「坐标范围」按 `sx`（已对齐）描述；若用内部 `x` 描述则需整体前移一拍，但区间宽度不变。

> 「待本地验证」：上板或仿真时，用示波器/波形窗量 `hsync` 的低电平宽度，应等于 `96 / f_pix = 96 / 25.2MHz ≈ 3.81 µs`。

#### 4.2.5 小练习与答案

**练习 1**：`de` 信号的定义是 `(y >= VA_STA && x >= HA_STA)`，即 `y>=0 && x>=0`。为什么用 `>=` 而不是 `==`？为什么 `sx/sy` 的位宽要设成 `signed`？

参考答案：`de` 用 `>=` 是因为有效区是一个**区间**（640×480 个点），不是一个点，`>=0` 判断的是「当前坐标是否进入了有效区这个象限」。`sx/sy` 必须是 `signed`，因为消隐期坐标是负数（如 -160），若声明成无符号，负数会被解释成巨大的正数，所有区间判断都会错。

**练习 2**：如果把 `display_480p` 例化时把 `H_RES` 改成 800、`V_RES` 改成 600，但保持 `H_FP/H_SYNC/H_BP/V_FP/V_SYNC/V_BP` 不变，会发生什么？这是一个「正确的 800×600」配置吗？

参考答案：不是正确配置。800×600@60Hz 有自己的一套标准参数（`H_FP=40, H_SYNC=128, H_BP=88, V_FP=1, V_SYNC=4, V_BP=23`，像素时钟 40 MHz）。只改 `H_RES/V_RES` 而沿用 480p 的消隐参数，得到的不是任何标准分辨率，显示器很可能无法同步。这印证了 4.1 说的：**时序参数是一组绑定的标准值，不能零散乱改**。

### 4.3 display_720p 与 display_24x18：换参数换分辨率

#### 4.3.1 概念说明

如果说 4.2 讲清了「骨架」，那么 4.3 要传达的就是本讲最重要的工程思想之一：**这套时序生成逻辑是分辨率无关的**。要支持一个新分辨率，不需要写新代码，只需要换一组参数——这正是 projf 把三个（其实四个，含 1080p）模块写成「同构不同参」的原因。

`display_24x18` 则是这条原理的一个巧妙应用：既然逻辑分辨率无关，那完全可以造一个**极小**的「假分辨率」专门给 testbench 用——一帧只有 1050 个像素，仿真几百纳秒就能跑完一整帧，而 480p 要 42 万拍、720p 要 123 万拍。这大幅缩短了图形相关模块的仿真时间。

#### 4.3.2 核心流程

三个模块的执行流程**逐字相同**（四个 `always_ff` 块一字不差），区别只有两处：

1. `parameter` 默认值不同（分辨率、消隐、极性）；
2. 因此 `localparam` 派生出的坐标区间宽度不同。

也就是说，对 720p 和 24x18，4.2.2 那张坐标轴图完全适用，只是把数字换成各自的参数：

| 量 | 480p | 720p | 24x18 |
| --- | --- | --- | --- |
| `H_STA`（行起点） | -160 | -370 | -11 |
| `HS_STA`（同步起点） | -144 | -260 | -8 |
| `HS_END`（同步终点） | -48 | -220 | -4 |
| `HA_STA` / `HA_END` | 0 / 639 | 0 / 1279 | 0 / 23 |
| `H_total` | 800 | 1650 | 35 |
| 极性 `H_POL/V_POL` | 负 | 正 | 正 |

（720p 的 `H_STA = -(110+40+220) = -370`，`HS_STA = -370+110 = -260`，`HS_END = -260+40 = -220`，读者可自行验证 24x18。）

#### 4.3.3 源码精读

**720p 的参数与骨架**：

[ThreePart/projf-explore/lib/display/display_720p.sv:32-44](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_720p.sv#L32-L44)
> 720p 的 `localparam` 派生公式与 480p **逐字相同**（`H_STA = 0 - H_FP - H_SYNC - H_BP` 等），只是代入的参数不同，于是算出更大的负坐标区间和更宽的有效区。

把 720p 的 `always_ff` 四块与 480p 对照——你会发现它们从第 49 行到第 92 行**完全一致**。这就是「同构不同参」的直观体现：同一份代码，编译期常量不同，行为就适配了新分辨率。

**24x18：为 testbench 而生的小分辨率**：

[ThreePart/projf-explore/lib/display/display_24x18.sv:5-6](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_24x18.sv#L5-L6)
> 头注释直说：「为 testbench 设计的更小显示器；24×18 (432) 个有效像素，含消隐 35×30 (1050) 个像素」。

[ThreePart/projf-explore/lib/display/display_24x18.sv:11-22](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/display_24x18.sv#L11-L22)
> 参数全部缩小到个位数（`H_RES=24, H_SYNC=4` 等），但极性取正（`H_POL=1, V_POL=1`）。模块体与 480p/720p 仍完全相同。

**README 的索引视角**：display 分区把「信号生成」「缓冲」「编码」三类模块分开列。

[ThreePart/projf-explore/lib/display/README.md:7-16](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/README.md#L7-L16)
> README 把本讲三个模块归入「Display Signal Generation」，并列出了 linebuffer（缓冲）和 TMDS/DVI（编码）两类后续模块——它们分别属于 [u6-l4](u6-l4-framebuffer-sprites.md) 和 [u6-l2](u6-l2-dvi-hdmi-tmds.md) 的内容。

#### 4.3.4 代码实践：参数对照与「同构」验证

**实践目标**：亲手验证三个模块「代码同构、参数不同」，并据参数预测各自的同步脉冲极性。

**操作步骤**：

1. 打开 `display_480p.sv`、`display_720p.sv`、`display_24x18.sv` 三个文件，**只比较第 49–92 行**（四个 `always_ff`）。
2. 确认它们逐字相同（你可以用 `diff` 工具：在本仓库根目录执行 `diff <(sed -n '49,92p' ThreePart/projf-explore/lib/display/display_480p.sv) <(sed -n '49,92p' ThreePart/projf-explore/lib/display/display_720p.sv)`，预期无输出）。
3. 再据各自的 `H_POL`，预测 `hsync` 在同步窗口内是高还是低。

**预期结果**：

- 三个文件的 `always_ff` 主体**完全相同**，差异仅在第 8–20 行的 `parameter` 默认值与第 32–44 行派生出的 `localparam` 数值。
- 极性预测：480p `H_POL=0` → 同步窗口内 `hsync` 为**低**；720p 与 24x18 `H_POL=1` → 同步窗口内 `hsync` 为**高**。

> 「待本地验证」：`diff` 命令在你的本地环境应返回空（无差异）。这强有力地证明了「同构不同参」。

#### 4.3.5 小练习与答案

**练习 1**：如果要新增一个 1080p（1920×1080@60Hz）模块，你需要写多少行新代码？参考仓库已有的 `display_1080p.sv`。

参考答案：理论上 0 行新逻辑——只需复制 `display_720p.sv`，把 `parameter` 改成 1080p 的标准值（`H_RES=1920, V_RES=1080, H_FP=88, H_SYNC=44, H_BP=148, V_FP=4, V_SYNC=5, V_BP=36, H_POL=1, V_POL=1`），模块体一字不改。仓库里的 `display_1080p.sv` 正是这样做的。

**练习 2**：`display_24x18` 一帧有多少个像素时钟？相比 480p，用它做 testbench 能快多少倍？

参考答案：24x18 一帧 \(35 \times 30 = 1050\) 个时钟；480p 一帧 \(800 \times 525 = 420\,000\) 个时钟。\(420\,000 / 1050 = 400\)，所以仿真一帧快约 **400 倍**。这就是 projf 专门造这个小分辨率模块的原因。

## 5. 综合实践：仿真 display_480p，验证一行内的时序

本讲的综合实践把第 4 节串起来：写一个最小 testbench，例化 `display_480p`，用一个简单方波当像素时钟（**不走 MMCM，从而可用 Icarus/Verilator 等任意仿真器**），跑过「一行多一点」的时间，观察 `sx`、`hsync`、`de` 的变化，亲手验证 4.2.4 那张时序图。

> 本实践分两条路线：
> - **路线 A（便携，推荐先做）**：下面给出的最小 testbench 是**示例代码**，用普通时钟直接驱动 `display_480p`，任何仿真器都能跑。
> - **路线 B（仓库自带，Vivado 专用）**：仓库的 `lib/display/xc7/display_480p_tb.sv` 用了真实 MMCM 像素时钟，只能在 Vivado 仿真器跑，见 5.6。

### 5.1 实践目标

- 验证 `sx` 在一行内从 `H_STA(-160)` 递增到 `HA_END(639)` 再回卷；
- 验证 `hsync` 在同步窗口 `[-144, -48)` 内为低（负极性），其余为高；
- 验证 `de` 只在 `sx >= 0` 时为高，且高电平持续 640 拍。

### 5.2 示例 testbench

把以下内容存为 `tb_display_480p.sv`，与原 `display_480p.sv` 放同一目录。**这是示例代码（非仓库原有文件）**，刻意避开了 MMCM，方便用任意仿真器运行：

```systemverilog
// 示例代码：display_480p 最小 testbench（不依赖 MMCM，便携）
`default_nettype none
`timescale 1ns / 1ps

module tb_display_480p();
    localparam CORDW = 16;
    localparam CLK_PERIOD = 40;   // 40ns 周期 == 25 MHz（近似 480p 像素时钟）

    logic clk_pix;
    logic rst_pix;
    logic signed [CORDW-1:0] sx, sy;
    logic hsync, vsync, de, frame, line;

    display_480p #(.CORDW(CORDW)) dut (
        .clk_pix, .rst_pix,
        .sx, .sy, .hsync, .vsync, .de, .frame, .line
    );

    // 简单方波像素时钟（不走 MMCM）
    initial clk_pix = 0;
    always #(CLK_PERIOD/2) clk_pix = ~clk_pix;

    integer i;
    initial begin
        // 复位
        rst_pix = 1;
        #(CLK_PERIOD*2);
        @(negedge clk_pix); rst_pix = 0;

        // 监视：每拍打印坐标与关键信号
        $display("t(ns)    sx     sy  hsync de");
        for (i = 0; i < 900; i = i + 1) begin   // 跑过一整行(800拍)再多一点
            @(posedge clk_pix);
            // 只在「进入同步窗口」「离开同步窗口」「进入有效区」三类关键时刻打印
            if (sx == -160 || sx == -144 || sx == -48 || sx == 0 || sx == 639)
                $display("%0t  %4d   %4d    %b    %b", $time, sx, sy, hsync, de);
        end
        $finish;
    end
endmodule
```

### 5.3 操作步骤

1. 准备文件：`tb_display_480p.sv`（上面这段）与原 `ThreePart/projf-explore/lib/display/display_480p.sv` 放进同一目录。
2. 选择仿真器（任一即可）：
   - **Icarus Verilog**：`iverilog -g2012 -o sim.vvp tb_display_480p.sv display_480p.sv && vvp sim.vvp`
   - **Verilator**：`verilator --binary -Wall --timing -sv tb_display_480p.sv display_480p.sv`
   - **Vivado 仿真器**：两个文件加进工程，设 `tb_display_480p` 为 top，Run Simulation。
3. 观察终端打印的 5 个关键时刻。

### 5.4 需要观察的现象与预期结果

按 `sx` 递增顺序，预期看到：

| `sx` | 含义 | `hsync`（负极性）预期 | `de` 预期 |
| --- | --- | --- | --- |
| -160 | 行起点（前沿开始） | 1（高，空闲） | 0 |
| -144 | 同步窗口开始 | **0（低，脉冲开始）** | 0 |
| -48 | 同步窗口结束 | **1（高，脉冲结束）** | 0 |
| 0 | 有效区开始 | 1 | **1（升高）** |
| 639 | 有效区最后一列 | 1 | **1** |

此外：

- 从 `sx=-144` 到 `sx=-48`，`hsync` 应连续 96 拍为低；
- 从 `sx=0` 到 `sx=639`，`de` 应连续 640 拍为高；
- `sx=639` 的下一拍，`sx` 应回卷到 -160（行回卷）。

> 若你的仿真器对 `logic signed` 打印格式有差异（`sx` 可能打印成大正数），那是 `%4d` 把负数当无符号显示的问题，可改用 `%0d` 或在监视前做 `$signed(sx)`——**结果以实际仿真器为准，待本地验证**。

### 5.5 用 display_24x18 加速仿真（可选）

把上面 testbench 里的 `display_480p` 换成 `display_24x18`，并把 `for` 循环上界从 900 改成 40，就能在一帧（35 拍/行 × 30 行 = 1050 拍）内完整观察一帧（含垂直同步 `vsync` 与 `frame` 脉冲）。这是 projf 提供 24x18 的本意——快速验证垂直时序而不必等几十万拍。

### 5.6 路线 B：仓库自带 testbench（Vivado 专用）

仓库在 `lib/display/xc7/` 下提供了完整的、带真实 MMCM 像素时钟的 testbench：

[ThreePart/projf-explore/lib/display/xc7/display_480p_tb.sv:35-45](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/display_480p_tb.sv#L35-L45)
> 例化 `display_480p`，时钟来自 `clock_480p`（MMCM），复位由 `!clk_pix_locked` 自动维持到时钟锁定。

[ThreePart/projf-explore/lib/display/xc7/display_480p_tb.sv:50-56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/display_480p_tb.sv#L50-L56)
> 100 MHz 主时钟（`CLK_PERIOD=10ns`），仿真跑 18 ms 后 `$finish`——因为一帧约 16.7 ms（\(1/59.94\) Hz），18 ms 刚好能覆盖完整一帧。

**操作**：在 Vivado 中把 `clock_480p.sv`、`display_480p.sv`、`display_480p_tb.sv` 加进工程（Xilinx 原语 `MMCME2_BASE`/`BUFG` 需要 Vivado），设 `display_480p_tb` 为 top，Run Simulation，在波形窗里观察完整的行场同步与一帧 16.7 ms 的周期。**注意：这个 testbench 因含 MMCM 原语，无法在 Icarus/Verilator 下运行。**

## 6. 本讲小结

- 显示器沿袭 CRT 的**光栅扫描**：逐像素从左到右扫一行，回扫换行，扫完一帧再回帧；回扫期间不发数据，形成**消隐期**。
- 一行 = **Active + Front Porch + Sync + Back Porch**，一帧同理（把像素换成行）；这些参数是 VESA/CTA **工业标准**，不能零散乱改。
- 帧率公式：\(f_{frame} = f_{pix} / (H\_total \times V\_total)\)；480p（25.2 MHz, 800×525）与 720p（74.25 MHz, 1650×750）都精确整除出 60 Hz。
- projf 的聪明设计：用**带符号坐标**，把有效区摆到 `sx/sy >= 0`，消隐期藏进负坐标，下游只需 `sx>=0` 或 `de` 即可判断有效区。
- `display_480p/720p/24x18`（及 1080p）**模块体逐字相同**，只差 `parameter` 默认值——「同构不同参」，换参数即可换分辨率。
- 模块四个 `always_ff` 分别管：同步生成（含极性三元运算）、控制信号（`de/frame/line`）、坐标计数（带符号回卷）、`sx/sy` 延迟对齐；像素时钟由 `clock_480p` 等 PLL 模块（承接 u5-l2）提供。

## 7. 下一步学习建议

- **纵向（显示链）**：本讲只生成了同步与坐标，还没把像素「送」出板子。下一讲 [u6-l2 DVI/HDMI 输出与 TMDS 编码](u6-l2-dvi-hdmi-tmds.md) 讲解如何用 `tmds_encoder_dvi.sv` 与差分原语把 `sx/sy/de` 配合的像素数据编码成 HDMI/DVI 信号。
- **纵向（图形）**：有了坐标 `sx/sy`，就能「按坐标画图」——[u6-l3 绘图原语：线与几何形状](u6-l3-graphics-primitives.md) 讲 Bresenham 画线、矩形、圆，全部建立在 `display_*` 输出的坐标之上。
- **纵向（帧缓冲）**：[u6-l4 帧缓冲与硬件精灵](u6-l4-framebuffer-sprites.md) 用 `de/frame/line` 配合 [u5-l3 的 `bram_sdp`](u5-l3-memory-rom-ram-bram.md) 实现帧缓冲与 linebuffer，是把本讲信号与存储结合的关键一讲。
- **延伸阅读**：projf 博客 [Video Timings: VGA, SVGA, 720p, 1080p](https://projectf.io/posts/video-timings-vga-720p-1080p/)（各种分辨率的完整时序参数表）与 [FPGA Graphics](https://projectf.io/posts/fpga-graphics/)（如何用本讲模块驱动第一幅画面）。
