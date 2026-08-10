# FIR_COEF：滤波器系数 ROM 封装

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 FIR_COEF 这个「封装模块」在做什么：把控制器 SPROM_CONT 与存储原语 SPROM 拼成一个对外输出滤波器系数的黑盒。
- 解释为什么过采样时钟 `LRCKx2_O` / `BCKx2_O` 不是直接从控制器引出，而是要再过 `p1` / `p2` 两级寄存器——为了让时钟「跟着数据一起打拍」，与系数的读出延迟严格对齐。
- 看懂 `generate` 块如何用一个 `OUTPUT_REG` 参数**同时**决定 ROM 的读延迟和时钟取哪一级，使两条流水线始终同步。

## 2. 前置知识

承接前面几讲，你需要先记住这些已经建立的认知（本讲不再重复细节）：

- **三层结构**：FIR_x2 的存储类模块分为「存储原语（SPROM/SDPRAM）→ 控制器（SPROM_CONT/DPRAM_CONT）→ 封装模块（FIR_COEF/DATA_BUFFER）」。封装模块本身不含数据逻辑，只把控制器和原语连起来（见 u1-l2、u3-l1）。
- **SPROM 是同步读 ROM**：内部有 `RDATAO_REG_1P` / `RDATAO_REG_2P` 两级读寄存器，由 `OUTPUT_REG` 选择输出哪一级，读延迟为 1 拍或 2 拍（见 u3-l3 的同款设计）。
- **过采样时钟是「派生 + 打拍」出来的**：`BCKx2` / `LRCKx2` 不是外部 PLL 给的，而是芯片内部从系数地址派生，并随数据逐级寄存以保持对齐（见 u2-l1、u2-l2）。
- **SPROM_CONT 产生地址与过采样时钟**：它输出系数地址 `CADDR_O`，以及 `LRCKx_O` / `BCKx_O`。其中地址与 `LRCKx_O` 在同一个 MCLK 上升沿更新、时间上对齐（内部细节留给 u4-l2）。

一句话直觉：**系数从 ROM 里读出来要花时间（1 或 2 拍），而标记「该出下一个过采样样点」的时钟信号也要花同样的时间一起走到出口，两边才能在输出端对上。FIR_COEF 的全部精妙就在于这个「对齐」。**

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的部分 |
|---|---|---|
| [04_FIR_COEF/FIR_COEF.v:51-126](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L51-L126) | **主角**：系数 ROM 封装模块 | 端口、参数、两个子模块实例、时钟流水线 `always` 块、`generate` 选择 |
| [03_SPROM_CONT/SPROM_CONT.v:90-99](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L90-L99) | 被封装的控制器 | 它输出 `CADDR_O` / `LRCKx_O` / `BCKx_O` 的方式（说明「地址与时钟同沿」） |
| [04_FIR_COEF/SPROM.v:69-82](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L69-L82) | 被封装的 ROM 原语 | 两级读寄存器 + `generate`（决定系数读延迟 1/2 拍） |
| [07_FIR_x2/FIR_x2.v:119-132](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L119-L132) | 顶层 | 看 FIR_COEF 在系统里被怎样实例化（参数取值、复位接法） |

## 4. 核心概念与源码讲解

### 4.1 系数 ROM 封装：控制器 + 存储原语的黑盒拼接

#### 4.1.1 概念说明

FIR 卷积需要两路输入相乘再累加：一路是**历史 PCM 样点**（由 DATA_BUFFER 提供，u3 讲过），另一路就是**滤波器系数**。系数是固定的，存在 ROM 里；本讲的主角 FIR_COEF 就是「系数那一侧」的封装模块。

它的职责只有两件：

1. **算出当前该读哪个系数的地址**——交给控制器 SPROM_CONT；
2. **按地址把系数读出来**——交给 ROM 原语 SPROM。

这和 DATA_BUFFER（u3-l1）是同一个套路：封装模块自己不碰数据，只负责把「管地址的控制器」和「管存取的原语」用几根线连起来。这样做的好处是原语被压在最底层，将来要换厂商的 Block RAM/ROM 时，只动原语这一层，上面的封装和控制器都不用改。

#### 4.1.2 核心流程

把 FIR_COEF 当黑盒看，信号流如下：

```
        MCLK_I, BCK_I, LRCK_I, NRST_I
                  │
        ┌─────────┴─────────────┐
        │      FIR_COEF         │
        │                       │
        │  ┌───────────────┐    │   CADDR（系数地址）
        │  │  SPROM_CONT   │────┼──────────────┐
        │  │  (控制器)     │────┼── LRCKx_O ──┐ │
        │  └───────────────┘    │  BCKx_O ──┐ │ │
        │                       │           │ │ │
        │  ┌───────────────┐    │           │ │ │
        │  │    SPROM      │<───┼───────────┘ │ │   （地址送进 ROM）
        │  │  (ROM 原语)   │────┼── COEF_O    │ │
        │  └───────────────┘    │             │ │
        │                       │  时钟打拍对齐（见 4.2）
        │  COEF_O  ──────────────────────────────►  系数输出
        │  LRCKx_O ──[p1]──[p2]──────────────► LRCKx2_O 过采样时钟
        │  BCKx_O  ──[p1]──[p2]──────────────► BCKx2_O
        └───────────────────────┘
```

- 控制器给出**地址**与**原始过采样时钟**（`LRCKx_O` / `BCKx_O`）。
- ROM 用地址读出**系数** `COEF_O`。
- 原始时钟再过两级寄存器（`p1` / `p2`），变成对外的 `LRCKx2_O` / `BCKx2_O`（这是 4.2 的重点）。

#### 4.1.3 源码精读

先看端口与参数 [FIR_COEF.v:51-69](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L51-L69)：FIR_COEF 对外接收时钟/复位/控制输入，输出系数 `COEF_O` 与两个过采样时钟。

参数里有个**容易踩坑的命名**：这里的 `DATA_WIDTH` 实际指的是**系数位宽**，而不是 PCM 数据位宽。看顶层怎么实例化就明白了 [FIR_x2.v:119-124](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L119-L124)：

```verilog
FIR_COEF #(
    .DATA_WIDTH(COEF_WIDTH),   // 16：把顶层的系数位宽 COEF_WIDTH 接到 FIR_COEF 的 DATA_WIDTH
    .ADDR_WIDTH(RADDR_WIDTH),  // 9 ：ROM 地址宽度 = WADDR_WIDTH+1
    .OUTPUT_REG(OUTPUT_REG),   // "TRUE"
    .RAM_INIT_FILE(COEF_INIT)  // "FIR512.hex"
) u_FIR_COEF ( ... );
```

> 注意：`ADDR_WIDTH=9` 意味着 ROM 有 \(2^9=512\) 个系数，正好对应「MCLK/fs = 512 抽头」的 FIR512（见 u1-l1）。地址比数据 RAM 多 1 位，是因为 2 倍过采样把每对奇偶抽头展开成两个系数地址（多相分解，u4-l2 详讲）。

封装本体只有两个实例。**控制器实例** [FIR_COEF.v:82-92](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L82-L92) 把 `LRCK_I` / `BCK_I` / `MCLK_I` / `NRST_I` 喂给 SPROM_CONT，取回系数地址 `CADDR` 与原始过采样时钟 `LRCKx_O` / `BCKx_O`：

```verilog
SPROM_CONT #(.ROM_ADDR_WIDTH(ADDR_WIDTH)) u_SPROM_CONT(
    .MCLK_I(MCLK_I), .BCK_I(BCK_I), .LRCK_I(LRCK_I), .NRST_I(NRST_I),
    .CADDR_O(CADDR),   // 系数读地址
    .LRCKx_O(LRCKx_O), // 原始过采样 LRCK
    .BCKx_O(BCKx_O)    // 原始过采样 BCK
);
```

**ROM 实例** [FIR_COEF.v:95-104](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L95-L104) 用上面的地址读系数：

```verilog
SPROM #(.DATA_WIDTH(DATA_WIDTH), .ADDR_WIDTH(ADDR_WIDTH),
        .OUTPUT_REG(OUTPUT_REG), .ROM_INIT_FILE(RAM_INIT_FILE)) u_SPROM(
    .CLK_I(MCLK_I),    // ROM 与控制器共用同一个 MCLK
    .RADDR_I(CADDR),   // 控制器给的地址
    .RDATA_O(COEF_O)   // 读出的系数直通对外（中间不打拍，打拍在 ROM 内部完成）
);
```

可以看到：**系数这条数据通路在 FIR_COEF 内部是一根直通线**（`CADDR → SPROM → COEF_O`），FIR_COEF 不给数据加任何寄存器，所有读延迟都在 SPROM 内部那两级寄存器里（见 4.2）。FIR_COEF 唯一主动加的逻辑，是给时钟加的寄存器。

> 小知识：顶层把 FIR_COEF 的复位接到了恒高的 `DUMMY_NRST`（[FIR_x2.v:92](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L92) 与 [FIR_x2.v:128](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L128)），即「系数通路永不复位」。这与数据通路接真实复位形成对比，是 u2-l1 已经提过的设计取舍。

#### 4.1.4 代码实践

**实践目标**：确认 FIR_COEF 是「无数据逻辑的纯封装」，并厘清 `DATA_WIDTH` 命名陷阱。

**操作步骤**：

1. 打开 [FIR_COEF.v:71-126](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L71-L126)，确认从 `CADDR` 到 `COEF_O` 之间没有任何由 FIR_COEF 自己写的 `always` / `assign` 改动数据，只有 SPROM 的实例连线。
2. 对照 [FIR_x2.v:119-124](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L119-L124)，在纸上写下 FIR_COEF 四个参数的真实取值（`DATA_WIDTH=16`、`ADDR_WIDTH=9`、`OUTPUT_REG="TRUE"`、`RAM_INIT_FILE="FIR512.hex"`）。
3. 数一下 FIR_COEF 对外有几个输出、分别来自哪个子模块。

**需要观察的现象 / 预期结果**：

- `COEF_O` 来源 = SPROM（原语）；`LRCKx2_O` / `BCKx2_O` 来源 = SPROM_CONT（控制器）+ FIR_COEF 自己的打拍逻辑。
- 你会发现「数据走原语、时钟走控制器 + 打拍」的清晰分工。
- 待本地验证：若你能打开综合工具的原理图，应看到 FIR_COEF 内部除两个子模块外，只剩 4 个时钟寄存器（`p1` / `p2` 各一对）。

#### 4.1.5 小练习与答案

**Q1**：为什么把控制器和原语拆成两个子模块，而不是直接写在一个 FIR_COEF 里？

> **答**：为了让原语（SPROM）隔离在最底层，便于跨厂商替换。换 Vivado 的 Block ROM 时，只改 SPROM 这一层，FIR_COEF 和 SPROM_CONT 都不用动。这是全项目存储类模块一致的分层约定（见 u3-l1）。

**Q2**：FIR_COEF 的 `DATA_WIDTH` 和顶层 FIR_x2 的 `DATA_WIDTH` 是同一个意思吗？

> **答**：不是，是个命名陷阱。顶层里 `DATA_WIDTH=32` 指 PCM 数据位宽；FIR_COEF 里 `DATA_WIDTH` 实际是系数位宽（顶层用 `.DATA_WIDTH(COEF_WIDTH)` 把 16 传进来）。读源码时要看实例化处的端口映射，别被同名参数误导。

---

### 4.2 过采样时钟流水线对齐：让时钟跟着数据打拍

#### 4.2.1 概念说明

这是本讲的核心，也是最容易看漏的一处设计。

下游的乘法器（MULT）和累加器（ADD）需要一个信号告诉它们「现在该处理一个过采样样点了」——这个信号就是 `LRCKx2_O`（它的每次翻转对应一个 2 倍过采样样点）。问题是：**这个「样点节拍」必须和「对应的系数」同时到达下游**，否则累加器会在错误的时刻复位/输出。

而系数从 ROM 读出来是有延迟的：地址先进 ROM，系数要 1 或 2 拍后才出现在 `COEF_O`。如果 `LRCKx2_O` 直接取控制器刚算出来的 `LRCKx_O`，它就会比系数**早到**，对不齐。

解决办法很朴素也很关键：**给 `LRCKx_O` 也串上和 ROM 一样多的寄存器**，让它和系数一起「排队走」相同的拍数，这样两者在出口处重新对齐。这就是源码注释 `Add LRCKx_O Output Register` 的真正含义——加寄存器不是为了去抖动，而是**为了等数据**。

#### 4.2.2 核心流程

先把「对齐」量化。设 ROM 的读延迟为 \(L\)（`OUTPUT_REG="TRUE"` 时 \(L=2\)，`"FALSE"` 时 \(L=1\)）。要让系数与时钟在输出端对齐，时钟流水线必须恰好打 \(N\) 拍，且满足：

\[
N = L
\]

具体到 `OUTPUT_REG="TRUE"`（顶层固定取值），两条路径的拍数对照如下（「第 k 拍」= 地址与标记在控制器输出端就绪之后，又经过 k 个 MCLK 上升沿）：

| 拍数 | 系数路径（`CADDR → COEF_O`） | 时钟路径（`LRCKx_O → LRCKx2_O`） |
|---|---|---|
| 第 0 拍 | `CADDR = 地址 X`（控制器同时把标记 M 送到 `LRCKx_O`） | `LRCKx_O = 标记 M` |
| 第 1 拍 | SPROM: `RDATAO_REG_1P = coef(X)` | `LRCKx2_p1 = M` |
| 第 2 拍 | SPROM: `RDATAO_REG_2P = coef(X) → COEF_O` | `LRCKx2_p2 = M → LRCKx2_O` |

**第 2 拍**那一行就是结论：`coef(X)` 与标记 M 同时出现在 FIR_COEF 的两个输出上，对齐达成。

为什么「地址和标记在第 0 拍同时就绪」？因为 SPROM_CONT 把地址 `CADDR_O` 与 `LRCKx_O` 放在**同一个** `always @(posedge MCLK_I)` 里、用同一组非阻塞赋值更新（[SPROM_CONT.v:90-94](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L90-L94)、[SPROM_CONT.v:96-99](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L96-L99)），它们在同一拍一起变。这是整个对齐成立的前提。

#### 4.2.3 源码精读

FIR_COEF 为时钟准备的 4 个寄存器与打拍逻辑在 [FIR_COEF.v:71-78](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L71-L78) 声明、[FIR_COEF.v:107-114](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L107-L114) 赋值：

```verilog
reg LRCKx2_p1 = 1'b1;
reg LRCKx2_p2 = 1'b1;
...
always @ (posedge MCLK_I) begin
    LRCKx2_p1 <= LRCKx_O;        // 第 1 级：把控制器的 LRCKx_O 延迟 1 拍
    LRCKx2_p2 <= LRCKx2_p1;      // 第 2 级：再延迟 1 拍
    ...
end
```

可见从 `LRCKx_O` 到 `LRCKx2_p2` 经过了 **p1、p2 两级寄存器**。

再看系数那一侧，SPROM 的两级读寄存器 [SPROM.v:69-82](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L69-L82)：

```verilog
always @ (posedge CLK_I) begin
    RDATAO_REG_1P <= ROM[RADDR_I];   // 第 1 级：地址 → 系数，延迟 1 拍
    RDATAO_REG_2P <= RDATAO_REG_1P;  // 第 2 级：再延迟 1 拍
end
// OUTPUT_REG == "TRUE" 时：assign RDATA_O = RDATAO_REG_2P;  → 系数延迟 2 拍
```

两侧都是 **2 级**——这就是「为何要与 SPROM 的 2 级读延迟对齐」的答案：时钟路径的 2 级（`p1→p2`）专门用来匹配系数路径的 2 级（`1P→2P`），保证 `COEF_O` 与 `LRCKx2_O` 同拍变化。等式 \(N=L=2\) 在这里落地。

> 这个「时钟随数据打拍」的思想会一路延伸：`LRCKx2` 出了 FIR_COEF 之后，在 MULT、ADD 里还会继续被寄存（见 [FIR_x2.v:143-148](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L143-L148)），每一次寄存都对应数据通路的一级流水线，时钟始终陪数据走完最后一级（见 u5）。

#### 4.2.4 代码实践（本讲指定实践）

**实践目标**：亲手追踪 `LRCKx_O → LRCKx2_O` 的寄存器级数，并解释为何要和 SPROM 的 2 级读延迟对齐。

**操作步骤**：

1. 在 [FIR_COEF.v:107-109](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L107-L109) 数寄存器：`LRCKx_O → LRCKx2_p1`（第 1 级）`→ LRCKx2_p2`（第 2 级）。
2. 结合 [FIR_COEF.v:118](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L118)（`OUTPUT_REG="TRUE"` 时 `assign LRCKx2_O = LRCKx2_p2;`），确认对外输出取的是第 2 级。
3. 在 [SPROM.v:70-73](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L70-L73) 数系数路径的寄存器：`ROM[RADDR_I] → RDATAO_REG_1P`（第 1 级）`→ RDATAO_REG_2P`（第 2 级）。
4.（可选，待本地验证）用 [FIR_COEF_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF_TB.v#L35-L97) 跑 Questa 仿真，把 `COEF_O` 与 `LRCKx2_O` 加到波形，观察每当 `COEF_O` 更新为一个新系数时，`LRCKx2_O` 是否恰好同步翻转。

**需要观察的现象 / 预期结果**：

- `LRCKx_O` 到 `LRCKx2_O` 共 **2 级**寄存器（p1、p2）。
- 必须对齐 SPROM 的 2 级读延迟，是因为：控制器在给出地址 `CADDR` 的同一拍给出 `LRCKx_O`，系数却要 2 拍后才从 `COEF_O` 出来；若 `LRCKx2_O` 只打 1 拍（或 0 拍），它就会比系数早到 1（或 2）拍，下游 ADD 会在系数还没齐时就复位累加器、送出错误的卷积和。
- 结论一句话：**时钟打拍的级数 = ROM 读延迟的级数**，二者锁定才能在输出端对齐。

#### 4.2.5 小练习与答案

**Q1**：如果把 SPROM 的 `OUTPUT_REG` 改成 `"FALSE"`（1 级读），却忘了改 FIR_COEF，`LRCKx2_O` 仍取 `p2`，会怎样？

> **答**：系数路径变成 1 拍（`COEF_O` 取 `RDATAO_REG_1P`），但时钟仍延迟 2 拍，于是 `LRCKx2_O` 会比系数**晚到 1 拍**，下游节拍错位、卷积结果错。正因如此，FIR_COEF 用同一个 `OUTPUT_REG` 同时控制两边（见 4.3）。

**Q2**：为什么不干脆把 `LRCKx2_O` 直接接到控制器的 `LRCKx_O`，省掉两级寄存器？

> **答**：那样时钟会比系数早 2 拍到，无法对齐。这两级寄存器不是「可选的去毛刺」，而是**必需的等待**，本质是在给系数读延迟做时间补偿。

---

### 4.3 generate 时钟选择：用一个参数锁住两条流水线

#### 4.3.1 概念说明

4.2 留下一个问题：怎么保证「时钟打拍级数」永远跟着「ROM 读延迟」一起变？答案是 `generate`：FIR_COEF 与 SPROM 读**同一个 `OUTPUT_REG` 参数**，编译时一起切换。

- `OUTPUT_REG="TRUE"` → ROM 走 2 级（`2P`），FIR_COEF 的时钟取 `p2`（2 级）。
- `OUTPUT_REG="FALSE"` → ROM 走 1 级（`1P`），FIR_COEF 的时钟取 `p1`（1 级）。

两边用同一个开关，等式 \(N=L\) 就被「焊死」，不会因为改一边忘改另一边而出错。

#### 4.3.2 核心流程

`BCKx2` 这一路还多一层判断：是否真的从控制器派生。整理成伪代码：

```
if (OUTPUT_REG == "TRUE"):        # 系数 2 级读
    LRCKx2_O = LRCKx2_p2          # 时钟也 2 级
    BCKx2_O  = (ADDR_WIDTH>=8) ? BCKx2_p2 : MCLK_I
else:                             # 系数 1 级读
    LRCKx2_O = LRCKx2_p1          # 时钟也 1 级
    BCKx2_O  = (ADDR_WIDTH>=8) ? BCKx2_p1 : MCLK_I
```

`LRCKx2` 的逻辑很纯粹：取 `p2` 还是 `p1`，完全跟着 `OUTPUT_REG`。

`BCKx2` 多了一个 `ADDR_WIDTH >= 8` 的阈值：只有当地址够宽（ROM 至少 256 项、对应至少 512 抽头）时，才用控制器派生出来的 `BCKx` 走流水线；地址不够宽时，`BCKx_O` 这条派生路径不可用，于是 `BCKx2_p1` 被强制写 0、输出直接旁路到 `MCLK_I` 兜底。至于为什么是「派生地址位」够不够的问题，属于 SPROM_CONT 的内部细节，留到 u4-l2 讲；本讲只需记住这条**结构上的分支**。

#### 4.3.3 源码精读

`generate` 块在 [FIR_COEF.v:116-124](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L116-L124)：

```verilog
generate
    if (OUTPUT_REG == "TRUE") begin : gen_regtrue
        assign LRCKx2_O = LRCKx2_p2;                              // 系数 2 级 → 时钟取第 2 级
        assign BCKx2_O  = (ADDR_WIDTH >= 8) ? BCKx2_p2 : MCLK_I;
    end else begin : gen_regfalse
        assign LRCKx2_O = LRCKx2_p1;                              // 系数 1 级 → 时钟取第 1 级
        assign BCKx2_O  = (ADDR_WIDTH >= 8) ? BCKx2_p1 : MCLK_I;
    end
endgenerate
```

而 `BCKx2_p1` 的「是否派生」在打拍处就已决定 [FIR_COEF.v:112](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L112)：

```verilog
BCKx2_p1 <= (ADDR_WIDTH >= 8) ? BCKx_O : 1'b0;   // 地址不够宽时，把 p1 钳成 0，禁用派生路径
```

对照顶层的真实配置：`OUTPUT_REG="TRUE"`、`ADDR_WIDTH=9`（≥8），所以本设计走的是 `gen_regtrue` 分支，`LRCKx2_O=LRCKx2_p2`、`BCKx2_O=BCKx2_p2`，两条过采样时钟都是「派生 + 2 级打拍」。

> ⚠️ **大小写陷阱**：字符串比较 `OUTPUT_REG == "TRUE"` 是**精确匹配**。若误写成小写 `"true"`，会落入 `else`（`gen_regfalse`）分支；SPROM 内部 [SPROM.v:77](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L77) 的同款比较也会判错，系数走 `1P`、时钟走 `p1`，两边虽然恰好又「自洽」地都变成 1 级——但那已不是设计的 2 级流水线，且与下游 MULT/ADD 预期的延迟拍数不再匹配。所以参数必须写大写 `"TRUE"`（与 u3-l3 的提醒一致）。

#### 4.3.4 代码实践

**实践目标**：体会「同一参数控制两条流水线」的安全性，并识破大小写陷阱。

**操作步骤**：

1. 在 [FIR_COEF.v:116-124](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L116-L124) 与 [SPROM.v:76-82](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L76-L82) 两处 `generate` 旁各画一个箭头，标注「都受 `OUTPUT_REG` 控制」，体会它们是**同一个开关的两面**。
2.（纸面推演，待本地验证）假设把顶层 [FIR_x2.v:78](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L78) 的 `OUTPUT_REG` 改成 `"FALSE"`：预测 `COEF_O` 取 `RDATAO_REG_1P`（1 拍）、`LRCKx2_O` 取 `LRCKx2_p1`（1 拍），两边仍同步；但整个滤波器流水线总深度变浅 1 拍，下游 MULT/ADD 的对齐预期也要相应调整——这正是注释 `Changing these parameters may cause bugs`（[FIR_x2.v:75](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L75)）警告的内容。

**需要观察的现象 / 预期结果**：

- 改 `OUTPUT_REG` 会**同时**改变系数读延迟和时钟打拍级数，二者始终相等。
- 单看 FIR_COEF 内部仍自洽；真正的风险在于与上下游流水线深度的耦合，所以项目把 `OUTPUT_REG` 固定为 `"TRUE"`，不建议随意改。

#### 4.3.5 小练习与答案

**Q1**：`generate` 里的 `if (OUTPUT_REG == "TRUE")` 是在什么时候、怎么「执行」的？

> **答**：`generate` 是**编译期/综合期**的条件展开，不是运行时判断。综合器根据 `OUTPUT_REG` 的值只保留一个分支（`gen_regtrue` 或 `gen_regfalse`），另一个分支的硬件根本不存在，所以它没有运行时开销。

**Q2**：`ADDR_WIDTH=9` 时，`BCKx2_O` 走哪条路？为什么？

> **答**：9 ≥ 8，走派生路：`BCKx2_O = BCKx2_p2`（`OUTPUT_REG="TRUE"`），即控制器派生的 `BCKx_O` 经 2 级打拍后输出。若 `ADDR_WIDTH<8`，则 `BCKx2_O = MCLK_I` 兜底，因为地址位不够、无法从地址派生出 `BCKx`（详见 u4-l2）。

**Q3**：为什么 `BCKx2` 需要兜底到 `MCLK_I`，而 `LRCKx2` 不需要？

> **答**：`LRCKx`（过采样 LRCK）由地址**最高位**派生（[SPROM_CONT.v:92](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L92)），任何地址宽度都有最高位，总能派生；而 `BCKx` 由地址中某个**靠中间的位**（`CADDR_REG[ROM_ADDR_WIDTH-7]`）派生，地址不够宽时这个位不存在/无意义，只能兜底。这是两者的派生来源不同导致的（细节见 u4-l2）。

---

## 5. 综合实践

把本讲三块内容串起来，做一个「对齐验证」小任务。

**任务**：用 FIR_COEF 单模块的 testbench（[FIR_COEF_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF_TB.v#L35-L97)）跑一次 Questa 仿真，验证「系数与时钟在输出端对齐」这一核心结论。

**步骤**：

1. 按 u1-l3 学过的仿真流程编译 `04_FIR_COEF/` 下的 `FIR_COEF.v` / `SPROM_CONT.v` / `SPROM.v` 与 `FIR_COEF_TB.v`（testbench 已自带 MCLK 分频与复位，参数 `DATA_WIDTH=16`、`ADDR_WIDTH=9`、`OUTPUT_REG="TRUE"`，见 [FIR_COEF_TB.v:38-41](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF_TB.v#L38-L41)）。
2. 把这些信号加进波形：`u_FIR_COEF/CADDR`、`u_FIR_COEF/COEF_O`、`u_FIR_COEF/LRCKx_O`、`u_FIR_COEF/LRCKx2_p1`、`u_FIR_COEF/LRCKx2_p2`、`LRCKx2_O`。
3. 找一个 `CADDR` 发生跳变的时刻作为「第 0 拍」，逐拍（每个 MCLK 上升沿）记录：`COEF_O` 何时变成该地址对应的系数？`LRCKx2_p1`、`LRCKx2_p2`（即 `LRCKx2_O`）何时跟着翻？
4. 量出：`COEF_O` 相对 `CADDR` 延迟几拍？`LRCKx2_O` 相对 `LRCKx_O` 延迟几拍？

**预期结果**：两条都是 **2 拍**，且 `COEF_O` 更新的同一拍 `LRCKx2_O` 也翻转——这就是 4.2 那张对齐表的波形版证明。

**如果无法本地运行**：明确标注「待本地验证」，并按源码静态推导出上述 2 拍的结论（4.2 已给出推导）。静态推导本身就是合格的源码阅读型实践。

## 6. 本讲小结

- FIR_COEF 是**系数侧的封装模块**：把控制器 SPROM_CONT（管地址）与原语 SPROM（管存取）拼成一个输出系数 `COEF_O` 的黑盒，自身不含数据逻辑——和 DATA_BUFFER 同一套分层套路。
- 它的精髓在**过采样时钟的流水线对齐**：`LRCKx_O` / `BCKx_O` 不是直通，而是再过 `p1` / `p2` 两级寄存器，专门用来等系数从 ROM 读出来。
- 对齐的数学约束很简单：**时钟打拍级数 \(N\) = ROM 读延迟 \(L\)**；`OUTPUT_REG="TRUE"` 时 \(N=L=2\)，于是 `COEF_O` 与 `LRCKx2_O` 在输出端同拍变化。
- 一个 `OUTPUT_REG` 参数**同时**控制 ROM 读延迟（SPROM 内 `generate`）和时钟取哪一级（FIR_COEF 内 `generate`），把两条流水线焊在一起，改一边不会漏改另一边。
- `BCKx2` 多一个 `ADDR_WIDTH>=8` 阈值：地址够宽才走「派生 + 打拍」，否则兜底到 `MCLK_I`；`LRCKx2` 因由地址最高位派生而无需兜底。
- 「时钟随数据打拍」会延续到下游：`LRCKx2` 出 FIR_COEF 后在 MULT、ADD 继续被寄存，始终陪数据走完流水线（u5 详讲）。

## 7. 下一步学习建议

- **u4-l2 SPROM_CONT**：本讲把 SPROM_CONT 当黑盒，只用到「它同时输出地址与 `LRCKx_O`」。下一讲进到它内部，讲清楚系数地址如何在奇偶抽头间切换（多相分解），以及 `LRCKx_O` / `BCKx_O` 到底从地址的哪一位派生、为什么 `BCKx` 需要 `ADDR_WIDTH>=8` 阈值。
- **u4-l3 SPROM 原语**：若你想把 4.2 里 ROM 读延迟的细节（`$readmemh` 加载系数、两级寄存器）看得更透，可对照 u4-l3，它把 SPROM 与 SDPRAM 做了对比。
- **u5 运算通路**：带着本讲建立的「`LRCKx2` 是对齐后的样点节拍」这一定义，去看 MULT 如何把它再寄存一级、ADD 如何在它的下降沿复位累加器，体会对齐如何贯穿整条流水线。
