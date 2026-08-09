# DPRAM_CONT：环形缓冲地址控制器

## 1. 本讲目标

上一篇（u3-l1）我们把 `DATA_BUFFER` 当作「黑盒拼接」来看：它把一个控制器 `DPRAM_CONT` 和一个存储原语 `SDPRAM_SINGLECLK` 绑在一起，自己不做任何逻辑。本讲我们就打开这个黑盒，专门拆解控制器 `DPRAM_CONT`。

学完本讲，你应当能够：

- 说清 `DPRAM_CONT` 如何用 **LRCK 上升沿检测** 产生一个单周期脉冲，并在这一拍里更新写地址。
- 画出 `ADDR_PTR / WADDR_REG / RADDR_REG` 三个地址寄存器在一个 LRCK 周期内的递推关系，并解释「读地址领先写地址 1」的原因。
- 解释复位期间那段看似奇怪的「`WADDR = RADDR`、`RADDR` 自增、`WEN` 恒为 1」的时序到底在做什么——它是为了**预填充 RAM**。
- 说清 `REN_O = ~(WEN_REG & (WADDR_REG == RADDR_REG))` 这一行的作用，以及它为什么是**读写碰撞保护**。

本讲只讲「地址与使能怎么产生」，存储原语如何真正读写数据已在 u3-l1 讲过，奇偶系数地址派生则留到 u4。

## 2. 前置知识

在进入源码前，先建立两个直觉。本讲默认你已经读过 u2-l2（时钟模型）和 u3-l1（DATA_BUFFER 封装）。

### 2.1 为什么 FIR 需要一个「环形缓冲」

FIR 滤波器的本质是一个卷积：

\[
y[n] = \sum_{k=0}^{N-1} h[k] \cdot x[n-k]
\]

每算一个输出 \(y[n]\)，都要用到当前和过去共 \(N\) 个输入样点 \(x[n], x[n-1], \dots, x[n-N+1]\)。也就是说，硬件必须**记住一段历史输入**。

最省资源的做法不是把历史样点搬来搬去，而是在一块 RAM 里**原地不动**，只用一个不断推进的「头指针」标记最新样点写在哪儿、再用一个「读指针」依次把历史样点读出来做乘加。这块 RAM 加上这两个指针，就是一个**环形缓冲（ring buffer）**，也叫**抽头延迟线（tapped delay line）**。

`DPRAM_CONT` 做的全部工作就是：**维护这两个指针，以及对应的读写使能信号**。它不碰数据本身——数据直接连到存储原语（见 u3-l1）。

### 2.2 单时钟域下的「每样点动作一次」

回顾 u2-l2 的关键结论：整个设计只有 `MCLK_I` 一个时钟，`BCK`/`LRCK` 不进时钟敏感列表，而是被当作**数据信号**。要让电路「每收到一个新样点就动作一次」，常用的技巧是**边沿检测**：把 `LRCK` 延迟一拍得到 `LRCK_REG`，再用

\[
\text{rise} = \text{LRCK\_I} \;\&\; \sim \text{LRCK\_REG}
\]

得到一个只在一个 `MCLK` 周期内为 1 的脉冲。这个脉冲就是「新样点到来了」的事件。

> 术语提示：**非阻塞赋值 `<=`** 的 RHS（等号右边）取的都是本拍**之前**的旧值，结果在拍末才更新。这一点在本讲的地址递推里会反复用到，请牢记。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [01_DPRAM_CONT/DPRAM_CONT.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v) | 被测设计（DUT）：环形缓冲地址控制器，产生 `WEN/WADDR/REN/RADDR` |
| [01_DPRAM_CONT/DPRAM_CONT_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT_TB.v) | 测试激励：产生 `MCLK/LRCK` 与一个复位脉冲，观察控制器输出 |
| [01_DPRAM_CONT/Questa/DPRAM_CONT.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/Questa/DPRAM_CONT.bat) | 仿真批处理：`vlib/vlog/vsim` 编译并启动仿真 |
| [01_DPRAM_CONT/Questa/run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/Questa/run.do) | 波形脚本：`add wave` 添加待观察信号并 `run -all` |

控制器对外的端口（[DPRAM_CONT.v:49-63](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L49-L63)）非常简洁：

```verilog
module DPRAM_CONT #(
    parameter ADDR_WIDTH = 8 // Default: 8bits (255 - 0)
)(
    input  wire MCLK_I,
    input  wire LRCK_I,
    input  wire NRST_I, // Active Low.
    output wire WEN_O,
    output wire [ADDR_WIDTH-1:0] WADDR_O,
    output wire REN_O,
    output wire [ADDR_WIDTH-1:0] RADDR_O
);
```

注意几个要点：

- 只有时钟、LRCK、复位三路输入，**没有数据端口**——再次印证「它只管地址与使能」。
- 唯一的参数 `ADDR_WIDTH`（默认 8）决定存储深度为 \(2^8 = 256\)。在顶层它由 `WADDR_WIDTH` 传入（u2-l1 已说明系数 ROM 深度为数据 RAM 的 2 倍）。
- `BCK_I` 在本控制器里**完全没用到**（端口里都没有），它在封装层 `DATA_BUFFER` 也是悬空的（见 u3-l1）。

内部寄存器声明在 [DPRAM_CONT.v:65-71](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L65-L71)，先把名字记住，后面逐个用到：

| 寄存器 | 含义 |
| --- | --- |
| `LRCK_REG` | `LRCK_I` 延迟一拍，用于上升沿检测 |
| `WEN_REG` | 写使能（组合后输出为 `WEN_O`） |
| `WADDR_REG` | 写地址 |
| `RADDR_REG` | 读地址 |
| `ADDR_PTR` | 环形缓冲的「头指针」，每来一个样点加 1 |

---

## 4. 核心概念与源码讲解

控制器的全部时序逻辑集中在一个 `always` 块里（[DPRAM_CONT.v:74-99](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L74-L99)），外加一段组合输出赋值（[DPRAM_CONT.v:101-113](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L101-L113)）。我们把它拆成三个最小模块来讲。

### 4.1 上升沿检测：把 LRCK 电平变成单周期写脉冲

#### 4.1.1 概念说明

控制器要在「每个输入样点到来时」写一次 RAM。但 `LRCK_I` 是一个**电平信号**——它在半个采样周期内都保持高电平（数百个 `MCLK`），不能直接拿去当写使能，否则会连续写几百拍。

我们需要一个**单周期脉冲**：只在 `LRCK_I` 从 0 跳到 1 的那一拍为 1，其余拍都为 0。这就是上升沿检测，也是 u2-l2 提到的「把电平压成事件」的具体落地。

#### 4.1.2 核心流程

```
每个 posedge MCLK_I：
    LRCK_REG <= LRCK_I            # 把当前 LRCK 存起来（延迟一拍）
    rise = LRCK_I & ~LRCK_REG     # 当前为高、上一拍为低 ⇒ 上升沿
```

时序示意（`^` 表示该拍 rise=1）：

```
MCLK     : _|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_
LRCK_I   : 0 0 0 1 1 1 1 1 1 1 1 ...        # 电平，长时间为高
LRCK_REG : 0 0 0 0 1 1 1 1 1 1 1 ...        # 延迟一拍
rise     : 0 0 0 1 0 0 0 0 0 0 0 ...   ^
```

`rise` 只在跳变那一拍为 1，完美适合做「写一次」的触发。

#### 4.1.3 源码精读

延迟一拍的寄存（[DPRAM_CONT.v:76](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L76)）：

```verilog
LRCK_REG <= LRCK_I;
```

上升沿检测在两处被使用。第一处是作为分支条件（[DPRAM_CONT.v:88](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L88)）：

```verilog
if ((LRCK_I & ~LRCK_REG) == 1'b1) begin
    ...  // 上升沿那一拍：更新头指针与写地址
```

第二处是直接赋给写使能寄存器（[DPRAM_CONT.v:97](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L97)）：

```verilog
WEN_REG <= LRCK_I & ~LRCK_REG;   // 写使能 = 上升沿脉冲
```

这两处合起来表达的意思是：**只在 LRCK 上升沿那一拍写一次 RAM**。`WEN_REG` 随后被组合输出为 `WEN_O`（见 4.2.3）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，配合仿真波形理解。

1. **实践目标**：在波形上确认 `LRCK_I & ~LRCK_REG` 只在一个 `MCLK` 周期内为 1。
2. **操作步骤**：
   - 用 Questa 打开 `01_DPRAM_CONT/Questa/DPRAM_CONT.bat` 跑仿真（流程见 u1-l3，本目录的脚本结构与顶层一致）。
   - 在波形窗口把 `u1/LRCK_I`、`u1/LRCK_REG` 两个信号拖出来（`run.do` 默认没加 `LRCK_REG`，可手动 `add wave`）。
   - 用光标对齐到一次 `LRCK_I` 的 0→1 跳变。
3. **需要观察的现象**：`LRCK_REG` 比 `LRCK_I` 晚一个 `MCLK`；两者异或只在跳变那一拍为 1。
4. **预期结果**：脉冲宽度恰为 1 个 `MCLK` 周期（TB 中 `MCLK` 周期为 2 ns，见 4.3.4）。
5. **待本地验证**：精确的跳变时刻取决于仿真器光标定位，请以本地波形为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `LRCK_REG <= LRCK_I;` 这一行删掉，电路会怎样？

> **参考答案**：`LRCK_REG` 会一直保持初值 0，于是 `~LRCK_REG` 恒为 1，`LRCK_I & ~LRCK_REG` 退化为 `LRCK_I` 本身。这样写使能就变成了一个长达数百拍的电平，RAM 会在整个 LRCK 高电平期间被反复写同一个地址——环形缓冲失效。

**练习 2**：为什么用 `&`（位与）而不是 `&&`（逻辑与）来写 `LRCK_I & ~LRCK_REG`？

> **参考答案**：这里操作的是 1 比特信号，`&` 与 `&&` 结果相同；但位与更明确地表达「按位运算、结果仍是 1 比特」，且不引入逻辑运算的隐式归约语义，符合硬件描述的惯用风格。

---

### 4.2 环形地址递推：ADDR_PTR / WADDR / RADDR

#### 4.2.1 概念说明

边沿脉冲告诉我们「何时写」，而地址递推告诉我们「写在哪儿、从哪儿读」。三个寄存器分工如下：

- `ADDR_PTR`：**头指针**。每来一个新样点（每个 LRCK 周期）加 1，标记「最新样点要写入的槽位」。它本质上就是环形缓冲里那个慢慢转的写游标。
- `WADDR_REG`：**写地址**。写使能有效的那一拍，它等于当前的 `ADDR_PTR`，告诉 RAM 把新样点存到哪个槽。
- `RADDR_REG`：**读地址**。它在每个 `MCLK` 都自增，依次把缓冲里的历史样点读出来送给乘法器做卷积。

一个关键不变式（源码注释里也写明了）：**写使能有效时，读地址领先写地址 1**，即 `RADDR == WADDR + 1`。这样读取扫描从「最新样点的下一个槽」开始，绕一圈把全部历史样点读出来。

#### 4.2.2 核心流程

把一个 LRCK 周期内发生的事拆成「上升沿那一拍」和「其余拍」两段：

```
posedge MCLK_I（正常工作，NRST_I==1）:

  if (LRCK 上升沿) {              # 一个样点到来，只此一拍
      ADDR_PTR  <= ADDR_PTR + 1;  # 头指针推进到下一个槽（新槽）
      WADDR_REG <= ADDR_PTR;      # 写地址 = 旧头指针（写当前槽）
      RADDR_REG <= ADDR_PTR + 1;  # 读地址 = 旧头指针+1（领先 1）
  } else {                        # 样点内的其余 MCLK
      RADDR_REG <= RADDR_REG + 1; # 只有读地址继续自增，扫描历史
  }
  WEN_REG <= (LRCK 上升沿 ? 1 : 0);
```

注意非阻塞赋值的语义：上升沿那一拍，三句赋值的右边都取**旧** `ADDR_PTR`，所以拍末结果是 `WADDR = 旧ptr`、`RADDR = 旧ptr+1`、`ADDR_PTR = 旧ptr+1`。三者满足 `RADDR == WADDR + 1`。

随后进入「其余拍」，`WADDR` 不再变化（本样点只写一次），`RADDR` 每 `MCLK` 加 1，不断扫描缓冲。地址在 \(0 \sim 2^{\text{ADDR\_WIDTH}}-1\) 之间自然回绕（8 位即 0~255），形成环形。

> 为什么是 256 深的缓冲？以 FIR512 配置为例，`WADDR_WIDTH=8`（见 u2-l1），缓冲存 256 个历史样点；每个输入样点周期内读地址把 256 个样点全部扫一遍供乘加使用。这与系数 ROM 的 2 倍深度（512）共同支撑 2 倍过采样的多相卷积，系数侧细节见 u4。

#### 4.2.3 源码精读

头指针声明（[DPRAM_CONT.v:71](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L71)）：

```verilog
reg [ADDR_WIDTH-1:0] ADDR_PTR  = {ADDR_WIDTH{1'b0}};
```

正常工作下的地址递推（[DPRAM_CONT.v:86-98](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L86-L98)）：

```verilog
end else begin
    /* Normal Operation */
    if ((LRCK_I & ~LRCK_REG) == 1'b1) begin
        /* When Write Enable */
        ADDR_PTR  <= ADDR_PTR + 1'b1;  // Update the Head of Address.
        WADDR_REG <= ADDR_PTR;         // Update Write Address.
        RADDR_REG <= ADDR_PTR + 1'b1;  // Update Read Address.
    end else begin
        RADDR_REG <= RADDR_REG + 1'b1; // Update Only Read Address.
    end
    WEN_REG   <= LRCK_I & ~LRCK_REG;
end
```

输出赋值与读写碰撞保护（[DPRAM_CONT.v:101-113](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L101-L113)）：

```verilog
// Assertion #0: REN_O must be 1'b1
// psl assert always (REN_O == 1'b1) @ (posedge MCLK_I);
assign REN_O   = ~(WEN_REG & (WADDR_REG == RADDR_REG));

// Assertion #1: WEN_O must be 1'b0 when WADDR_O equals to RADDR_O.
// psl assert always ((WADDR_O == RADDR_O) -> (WEN_O == 1'b0)) @ (posedge MCLK_I);
assign WEN_O   = WEN_REG;

// Assertion #2: RADDR_O must be equals to (WADDR_O + 1'b1) if WEN_REG_P is 1'b1
// psl assert always ((WEN_O == 1'b1) -> (RADDR_O == WADDR_O + 1'b1)) @ (posedge MCLK_I);
assign WADDR_O = WADDR_REG;
assign RADDR_O = RADDR_REG;
```

这里要重点解释 `REN_O` 这一行。先看表达式：

\[
\text{REN\_O} = \sim\,(\text{WEN\_REG} \;\&\; (\text{WADDR\_REG} == \text{RADDR\_REG}))
\]

把它读成：「**除非**正在写 **且** 写地址等于读地址，否则读使能一直为 1」。也就是说，`REN_O` 正常情况下恒为 1（读始终允许），只有在「写使能有效的同时，写地址和读地址撞在一起」这一种情况下才被拉低。

为什么要拉低？在简单双口 RAM 中，对**同一个地址同时读和写**会产生「读优先 / 写优先」的不确定结果（写优先时读到旧值，读优先时读到正在写入的新值）。为了避免下游乘法器读到半新半旧的数据，控制器在这一拍**暂停一次读**，等下一拍写完再读。这就是「读写碰撞保护」。

在正常工作下，由于上升沿那一拍 `RADDR == WADDR + 1`，两地址永远不会相等，所以 `REN_O` 实际上恒为 1（注释里的 Assertion #0 正是想固化这个不变式）。碰撞保护主要在复位预填充阶段（4.3）和理论上的边界情形中起作用——它是一道安全网。

> 旁注：上面三段 `// psl assert` 是**被注释掉的 PSL 断言**，它们用可读的方式记录了三条不变式（REN 恒为 1、WADDR==RADDR 时 WEN 必为 0、WEN 为 1 时 RADDR==WADDR+1）。断言本身的可信度与覆盖率解读放在 u6-l3 详讲，本讲只需把它们当作「设计意图的注释」。

#### 4.2.4 代码实践

这是本讲的主实践任务（对应大纲里的 `practice_task`）。

1. **实践目标**：用 `DPRAM_CONT_TB` 跑仿真，观察一次 LRCK 上升沿前后 `WADDR_O / RADDR_O / WEN_O` 的变化，并亲手验证 `REN_O = ~(WEN & WADDR==RADDR)`。
2. **操作步骤**：
   - 在 Questa 中运行 [01_DPRAM_CONT/Questa/DPRAM_CONT.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/Questa/DPRAM_CONT.bat)。该脚本用 `vlog -cover bcs ../*.v` 编译本目录两个 `.v`，再用 `vsim ... -do "do run.do"` 启动仿真。
   - [run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/Questa/run.do) 已经把 `MCLK_I/LRCK_I/NRST_I/WEN_O/WADDR_O/REN_O/RADDR_O` 七个信号加入了波形。
   - 找到**复位释放之后**（见 4.3，TB 中复位在约 5501 ns 结束）的第一次 `LRCK_I` 上升沿，把光标分别放在它的前一拍、当拍、后一拍。
3. **需要观察的现象**：
   - `WEN_O` 只在上升沿那一拍为 1，其余拍为 0。
   - 上升沿当拍末：`WADDR_O` 取某个值 `A`，`RADDR_O` 取 `A+1`。
   - 之后若干拍 `WADDR_O` 保持 `A` 不变，`RADDR_O` 每拍加 1（`A+1, A+2, A+3, ...`），直到下一次上升沿。
   - `REN_O` 全程为 1（因为正常工作时 `WADDR != RADDR`）。
4. **预期结果**：三条不变式在波形上成立——`WEN` 为单拍脉冲、写时 `RADDR==WADDR+1`、`REN` 恒 1。
5. **解释 `REN_O`**：把某一拍的 `WEN_O`、`WADDR_O`、`RADDR_O` 代入 `~(WEN_O & (WADDR_O==RADDR_O))`，应得到与波形 `REN_O` 完全一致的值。由于正常工作下 `WADDR_O != RADDR_O`，括号内为 0，取反后 `REN_O==1`，验证「碰撞保护在不碰撞时不触发」。
6. **待本地验证**：具体地址数值（`A` 取几）取决于你定位的那次上升沿是复位后的第几个样点，请以本地波形读数为准。

#### 4.2.5 小练习与答案

**练习 1**：在上升沿那一拍，为什么 `WADDR_REG <= ADDR_PTR` 写入的是「旧」`ADDR_PTR`，而 `ADDR_PTR` 同时变成了「旧值+1」？

> **参考答案**：因为 Verilog 非阻塞赋值 `<=` 的右边统一使用本拍**开始前**的寄存器值，所有左边在拍末才同时更新。所以三条赋值的 RHS 都读到同一个「旧 ADDR_PTR」，拍末 `ADDR_PTR` 更新为旧值+1，而 `WADDR_REG` 拿到的是更新前的旧值。这正是我们想要的：把新样点写到当前头位置，然后把头指针推进一格。

**练习 2**：如果要让读地址「落后」写地址 1（即 `RADDR == WADDR - 1`），需要改哪一行？这种改动会带来什么风险？

> **参考答案**：把 `RADDR_REG <= ADDR_PTR + 1'b1` 改成 `RADDR_REG <= ADDR_PTR - 1'b1` 即可让读地址落后写地址 1。风险在于：读取扫描会从「最新写入样点的前一个槽」开始，卷积的样点顺序会错位，滤波结果出错；并且 `RADDR == WADDR` 不再被天然避开，碰撞保护会更频繁地触发。这只是一个理解性的假设，请勿在真实工程里这样改。

---

### 4.3 复位预填充时序：上电把 RAM 填满

#### 4.3.1 概念说明

一个刚上电的 RAM 里全是随机垃圾值。如果环形缓冲带着垃圾启动，那么滤波器输出的前若干个样点就是垃圾卷积结果，要等所有历史样点都被真实数据「冲刷」一遍之后才会稳定。

为了**上电即稳定**，`DPRAM_CONT` 在复位期间（`NRST_I == 0`）不进入正常环形工作，而是执行一段**预填充（pre-fill）**：配合存储原语的 `RAM_INIT_FILE`（见 u3-l1，初值来自 `BUFFER_INIT.hex`），它会在复位窗口里把每个地址都「写一遍」，确保环形缓冲启动时已经装满了有意义的初始数据，而不是垃圾。

#### 4.3.2 核心流程

```
posedge MCLK_I（复位期间，NRST_I==0）:
    WADDR_REG <= RADDR_REG;          # 写地址 = 当前读地址
    RADDR_REG <= RADDR_REG + 1;      # 读地址自增（带动 WADDR 一起推进）
    WEN_REG   <= 1;                  # 全程写使能
```

每一拍：写地址等于当前读地址，然后读地址加 1。下一拍写地址又跟上新的读地址……于是写地址依次取 `0,1,2,3,...`，每个地址都被写一次。地址在 \(0 \sim 2^{\text{ADDR\_WIDTH}}-1\) 内回绕，只要复位持续足够多个 `MCLK`，整个 RAM 就被扫了一遍。

复位释放后，`ADDR_PTR` 从 0 开始正常递增，环形缓冲带着预填充的数据进入工作。测试激励正是用一段固定宽度的复位脉冲来保证这一扫过程覆盖整个缓冲。

#### 4.3.3 源码精读

复位分支（[DPRAM_CONT.v:79-85](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L79-L85)）：

```verilog
if (NRST_I == 1'b0) begin
    /* Reset Opearation.
       Update Address for Initializing RAM Data.*/
    WADDR_REG <= RADDR_REG;
    RADDR_REG <= RADDR_REG + 1'b1;
    
    WEN_REG   <= 1'b1; // Enable WEN_O while Reset.
end
```

注意这段和正常工作的两点不同：

1. **`WADDR_REG <= RADDR_REG`**：写地址不再来自头指针，而是直接等于读地址，让两者「齐头并进」地扫过所有地址。
2. **`WEN_REG <= 1'b1`**：写使能在整个复位窗口恒为 1，确保每个地址都被写。

这里 `REN_O` 的碰撞保护就真正发挥作用了：复位期间 `WADDR_REG == RADDR_REG`（因为 `WADDR` 直接取 `RADDR`），又因为 `WEN_REG==1`，于是 `REN_O = ~(1 & 1) = 0`——读使能在复位期间被拉低。这是合理的：复位阶段是在初始化 RAM，根本不该往外读数据。

测试激励里的复位脉冲（[DPRAM_CONT_TB.v:86-89](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT_TB.v#L86-L89)）：

```verilog
always begin
    #4989 NRST_I <= 1'b0;
    #512  NRST_I <= 1'b1;
end
```

它让 `NRST_I` 在 `#4989 ns` 拉低、保持 `512 ns` 后再拉高。注意这是 `always` 块，会**周期性重复**触发复位，方便你在一次仿真里多次观察预填充过程。`512 ns` 对应 `MCLK` 周期（2 ns）的 256 倍，恰好够把 256 深的缓冲完整扫一遍。

#### 4.3.4 代码实践

1. **实践目标**：在波形上观察复位窗口，确认控制器在该窗口内「逐地址写入、读使能拉低」。
2. **操作步骤**：
   - 沿用 4.2.4 的仿真。先看清 TB 的时钟：[DPRAM_CONT_TB.v:75-77](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT_TB.v#L75-L77) 用 `#1 MCLK_I <= ~MCLK_I;` 产生周期 2 ns 的 `MCLK`；[DPRAM_CONT_TB.v:79-84](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT_TB.v#L79-L84) 用 9 位计数器 `MCLK_REG` 分频，`LRCK_I = MCLK_REG[8]`（=MCLK/512）。
   - 把光标移到 `NRST_I` 第一次拉低处（约 4989 ns），放大观察其后 512 ns。
3. **需要观察的现象**：
   - `WEN_O` 在整个复位窗口保持 1。
   - `WADDR_O` 与 `RADDR_O` 每拍都变化，且 `WADDR_O == RADDR_O`（同拍同值，因为 `WADDR` 直接取 `RADDR`），`RADDR_O` 每拍加 1。
   - `REN_O` 在复位窗口内为 0（碰撞保护触发）。
   - 复位拉高后，`WEN_O` 变回单拍脉冲，`REN_O` 回到 1。
4. **预期结果**：复位窗口宽度约 512 ns（256 个 `MCLK`），刚好覆盖整个 256 深缓冲的预填充。
5. **待本地验证**：由于 `MCLK_REG` 与 `NRST_I` 分属不同 `always`、初始相位由仿真器调度决定，具体的地址初值与拉低起止时刻请以本地波形为准。

#### 4.3.5 小练习与答案

**练习 1**：复位期间 `WADDR_REG <= RADDR_REG` 和 `RADDR_REG <= RADDR_REG + 1` 同时执行，`WADDR_REG` 拿到的是更新前还是更新后的 `RADDR_REG`？

> **参考答案**：拿到的是**更新前**的值。非阻塞赋值的右边都取本拍开始前的旧值，所以 `WADDR_REG` 拿到旧 `RADDR`，而 `RADDR_REG` 在拍末才变成旧值+1。于是每一拍 `WADDR` 锁定的是「上一个 `RADDR`」，两者在波形上表现为 `WADDR` 紧跟 `RADDR` 的前一拍取值——效果仍是逐地址扫过整个缓冲。

**练习 2**：如果把复位脉冲从 `#512` 缩短到 `#100`（50 个 MCLK），预填充还能完整覆盖 256 深的缓冲吗？会有什么后果？

> **参考答案**：不能。50 个 `MCLK` 只能写 50 个地址，缓冲里会有 206 个地址仍是 `RAM_INIT_FILE` 之外的随机值。复位释放后这些位置还残留垃圾，滤波器输出的前若干样点仍不可靠，预填充「上电即稳定」的目的部分失效。所以复位窗口宽度必须 ≥ 缓冲深度对应的 `MCLK` 数（本例为 256）。

---

## 5. 综合实践

把本讲三个模块串起来，做一个**「地址控制器行为速写」**的小任务。

**任务**：在 `DPRAM_CONT_TB` 的仿真波形上，画一张覆盖「复位预填充 → 复位释放 → 第一次正常 LRCK 上升沿 → 之后两三个 MCLK」的时序草图，纵轴列出 8 个信号，按下面顺序标注每一阶段的关键取值：

1. `NRST_I`：复位窗口为 0，其余为 1。
2. `LRCK_I`：标出第一次上升沿位置。
3. `LRCK_REG`：比 `LRCK_I` 晚一拍。
4. `WEN_O`：复位窗口恒 1；正常时为上升沿单拍脉冲。
5. `WADDR_O`：复位窗口逐拍变化；正常时在上升沿跳到新值后保持。
6. `RADDR_O`：复位窗口与 `WADDR_O` 同值且逐拍加 1；正常时每拍加 1。
7. `REN_O`：复位窗口为 0；正常时恒 1。
8. 用一个箭头标出 `RADDR_O == WADDR_O + 1` 成立的那一拍（即 `WEN_O` 为 1 的那一拍）。

**检查清单**：

- [ ] 复位窗口里 `WEN_O=1`、`REN_O=0`、`WADDR_O` 与 `RADDR_O` 同步扫地址。
- [ ] 复位释放后 `REN_O` 立即恢复 1。
- [ ] 正常上升沿那一拍 `WEN_O=1` 且 `RADDR_O == WADDR_O + 1`。
- [ ] 其余拍 `WEN_O=0`，`RADDR_O` 每拍加 1，`WADDR_O` 不变。

完成这张草图后，你就把「边沿检测 → 地址递推 → 复位预填充 → 碰撞保护」四件事在时序上对齐了一遍，这正是 `DPRAM_CONT` 的全部职责。

## 6. 本讲小结

- `DPRAM_CONT` 是 `DATA_BUFFER` 内的**地址与使能控制器**，不碰数据本身，只输出 `WEN_O / WADDR_O / REN_O / RADDR_O`。
- 它用 `LRCK_I & ~LRCK_REG` 做**上升沿检测**，把长时间为高的 LRCK 电平压成单周期脉冲，作为「新样点到来了」的事件。
- 环形地址由三个寄存器维护：头指针 `ADDR_PTR` 每样点加 1，`WADDR` 在写拍取 `ADDR_PTR`，`RADDR` 始终领先 `WADDR` 一拍并在样点内每 `MCLK` 自增扫描历史。
- 正常工作时 `RADDR == WADDR + 1`，两地址永不相等，这是被 PSL 注释断言固化的不变式。
- `REN_O = ~(WEN & (WADDR==RADDR))` 是**读写碰撞保护**：正常时恒为 1，只在读写同址时拉低；它在复位预填充阶段真正生效（`REN_O=0`）。
- 复位期间执行**预填充**：`WADDR` 跟随 `RADDR` 逐地址扫描、`WEN` 恒为 1，配合 `RAM_INIT_FILE` 让环形缓冲上电即装满有意义数据。

## 7. 下一步学习建议

- 想看控制器产生的这些地址/使能是如何驱动真正的存储读写的，回到 [02_DATA_BUFFER/SDPRAM_SINGLECLK.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v) 精读 **u3-l3**（SDPRAM 原语），重点看 `WENABLE_I/WADDR_I/RENABLE_I/RADDR_I` 如何映射到写口与 1 级/2 级读寄存器。
- 数据通路上的地址搞清楚后，进入 **u4-l1 / u4-l2**（FIR_COEF 与 SPROM_CONT），那里有「另一半地址故事」——系数 ROM 地址如何在奇偶抽头间切换并派生出 2 倍过采样时钟 `LRCKx2`。
- 对本讲提到的三条 PSL 断言与覆盖率感兴趣，可先跳读 **u6-l3**，再回头对照 [DPRAM_CONT.v:101-113](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L101-L113) 的注释理解它们如何把「设计意图」变成可验证的时序不变式。
