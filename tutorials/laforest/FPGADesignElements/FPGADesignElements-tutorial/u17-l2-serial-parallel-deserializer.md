# 串并转换与差分反串行化

## 1. 本讲目标

上一讲（u17-l1）我们把构建块拼成了「能跑循环的计算引擎」。本讲换个方向，看另一类同样靠拼装完成、却服务于**高速 I/O** 的引擎：把数据在「少而宽」（并行）与「多而窄」（串行）两种形态之间来回转换。

串行/并行转换无处不在：一条 LVDS 链路用 1 根线每秒传几 Gbit，FPGA 内部却想按 1 个字（多 bit）逐步处理——中间必须有「串→并」；反过来，把一个宽字打到一条高速串行线上，则需要「并→串」。本讲会讲两种实现路线：一种**纯软**（用本书通用构建块搭出来，跨任何 FPGA）、一种**纯硬**（直接用 Xilinx Series-7 片上 SERDES 硬核）。

学完本讲后，你应该能够：

1. 说清 `Parallel_Serial` 与 `Pipeline_Serial_Parallel` 如何把一个 `Register_Pipeline` 当作**移位寄存器**使用（回顾 u6-l2：`pipe_in` 喂 LSB 端、`pipe_out` 读 MSB 端、移位方向 LSB→MSB、Load overrides shift），并配合一个 `Counter_Binary` 数移了多少次，从而实现并→串与串→并。
2. 解释 `Pipeline_Serial_Parallel` 为什么比简单版多出 ready/valid 双向握手、它的 `counter_load_value` 在「输出与输入同拍握手」时为何要减 1，从而支持**不间断的连续串行流**。
3. 画出 `Deserializer_Differential_1toN` 的硬件流水线（`IBUFDS_DIFF_OUT` → `IDELAYE2` → `ISERDESE2`，正负各一路），说清它做**位对齐**（调延迟抽头）与**字对齐**（bitslip）两件事，并解释为什么 `ISERDESE2` 的并行输出要**反向接线**（`Q1→parallel[N-1] … Qn→parallel[0]`）。
4. 手画一张 1:4 反串行化的比特重排时序图，标出串行 bit 与并行字位的对应关系。
5. 说清 `IDELAYCTRL_Instance` 的作用：它校准 `IDELAYE2` 的延迟线，使抽头延迟在 PVT 下保持稳定已知；从而解释**为什么差分反串行化之前必须先有它**——没有校准，位对齐训练就没有可信的时间基准。

## 2. 前置知识

本讲是「复合流水线引擎」单元第二篇，承接以下基础（均已在前序讲义建立）：

- **u6-l2（寄存器流水线）**：本讲最重要的前置。`Register_Pipeline` 有两种用法：`WORD_WIDTH` 设成数据宽度 + `PIPE_DEPTH` 设成级数 → **时延流水线**（整字前移）；`WORD_WIDTH` 设成 1 + `PIPE_DEPTH` 设成字宽 → **移位寄存器**（逐位移入/移出）。本讲的两个软转换器正是后者与前者的两种参数化。务必记住三条性质：移位方向 **LSB→MSB**、`pipe_in` 喂最低级（stage 0）、`pipe_out` 读最高级（stage `PIPE_DEPTH-1`）、**Load overrides shift**（[Register_Pipeline.v:10-12](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L10-L12)）。
- **u8-l2（计数器与 clog2）**：知道 `Counter_Binary` 是「加法器 + 寄存器」，有 `run`/`load`/`clear`/`up_down`，`load` 优先于 `run`。本讲用它做「还剩几次移位」的下计数器。同时回忆 clog2 的位数陷阱：要能表示到 N（而非 N−1）需 `clog2(N)+1` 位——本讲 `Pipeline_Serial_Parallel` 的 `COUNT_WIDTH` 正是这则陷阱的实例。
- **u9-l1 / u9-l2（ready/valid 握手）**：知道 `handshake_complete = ready && valid`，影响接口的内部动作只在握手完成拍发生。`Pipeline_Serial_Parallel` 在串行输入侧与并行输出侧各有一套握手，是弹性的；`Parallel_Serial` 只有并行输入侧一个握手，串行输出是裸线。
- **u17-l1（引擎组装）**：上一讲我们看到复杂引擎「零自写时序、全部实例化构建块」的组装式风格。本讲两个软转换器继续这条线（自写逻辑只有几个组合 `always @(*)`）；而硬反串行化则展示了「组装」的另一极——直接调用芯片厂商的 I/O 硬核原语。
- **u4-l2（Core/Instance/Adapter/Shim 与约束）**：`Deserializer_Differential_1toN` 与 `IDELAYCTRL_Instance` 都触碰 Xilinx 专用 I/O 原语，属于 **Adapter 层**（适配器件与物理接口）；`IODELAY_GROUP` 这类 `(* *)` 属性必须写在源码里、随声明走，正是 u4-l2「源码内约束」原则的体现。

> 一个贯穿全讲的直觉：**串并转换有「软」「硬」两条路，选哪条取决于速度。** 软转换器（`Register_Pipeline` + `Counter_Binary`）完全可综合、跨厂商、能塞进任意 ready/valid 弹性流水线，但每个时钟周期只能搬 1 个串行元素，速度上限是 FPGA 普通逻辑的几百 MHz。一旦链路跑到 Gbit/s，普通触发器根本采不稳亚纳秒级的串行数据——这时候必须改用片上专用硬件：**SERDES**（串并转换硬核）+ **IDELAY**（可编程延迟）+ **IDELAYCTRL**（延迟校准）。本讲先讲软的、再讲硬的，最后讲让硬的能成立的那个校准器。

## 3. 本讲源码地图

本讲涉及的关键文件（均在仓库根目录）：

| 文件 | 作用 | 是否厂商相关 |
| --- | --- | --- |
| [Parallel_Serial.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v) | **并→串（软）**：吃一个宽字，逐位从 MSB 起串行送出；并行侧一个 ready/valid 握手 | 否（可综合） |
| [Pipeline_Serial_Parallel.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v) | **串→并（软）**：吃多个串行字，拼成一个更宽的并行字；串行侧与并行侧各一套 ready/valid 握手，可塞入弹性流水线 | 否（可综合） |
| [Deserializer_Differential_1toN.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v) | **差分反串行化（硬）**：1:N 比率的 Xilinx Series-7 高速差分接收前端，正负两路各自 IDELAY + ISERDESE2 | 是（Series-7） |
| [IDELAYCTRL_Instance.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v) | **IDELAY2 校准器**：每个 I/O Bank 一个，给 IDELAYE2 延迟线提供稳定校准 | 是（Series-7） |

软硬两路的对照：

| | 软转换器（`Parallel_Serial` / `Pipeline_Serial_Parallel`） | 硬反串行化（`Deserializer_Differential_1toN`） |
| --- | --- | --- |
| 实现材料 | 通用构建块（`Register_Pipeline` + `Counter_Binary`） | Xilinx I/O 硬核原语（`IBUFDS_DIFF_OUT`/`IDELAYE2`/`ISERDESE2`） |
| 可移植性 | 跨厂商可综合 | Series-7 专用（架构思路可借鉴到别家） |
| 速度 | 受普通逻辑限制（每周期 1 个串行元素） | Gbit/s 级（片上 SERDES 硬件） |
| 接口形态 | ready/valid 弹性（可塞入本书流水线） | 原始时钟域 + 训练控制（位/字对齐由外部模块完成） |
| 「N」的来源 | 参数 `WORD_COUNT_IN` / `WORD_WIDTH` | 硬件原生支持的 `DATA_WIDTH`（2–8, 10, 14） |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1** 串并转换（两个软转换器）、**4.2** 差分反串行化（硬 SERDES 前端）、**4.3** IDELAYCTRL（让硬前端的延迟可信的校准器）。

### 4.1 串并转换：把移位寄存器参数化

#### 4.1.1 概念说明

串并转换本质是「在时间维度上排开的 bit/字」与「在空间维度上排开的 bit/字」之间的互换。一个移位寄存器天然就是这种互换的载体——而 u6-l2 已经告诉我们，`Register_Pipeline` 只要换两组参数就能当移位寄存器用：

- **并→串**：`WORD_WIDTH=1`、`PIPE_DEPTH=WORD_WIDTH`。先通过 `parallel_in` 把整个字**并行载入**（每级装 1 bit），再让数据逐级向 MSB 端移位，最末级 `pipe_out` 就一字一字地吐出 bit。
- **串→并**：`WORD_WIDTH=字宽`、`PIPE_DEPTH=字数`。让串行字逐个从 `pipe_in` 移入，移满后整条流水线的 `parallel_out` 就是一个拼好的宽字。

`Parallel_Serial` 与 `Pipeline_Serial_Parallel` 就是在这套移位骨架上，各加一个 `Counter_Binary` 来数「还剩几次移位」，再用一段组合逻辑把移位动作绑到「握手完成」上。两者区别只在接口的弹性程度：

- `Parallel_Serial` 只在并行输入侧有 ready/valid 握手；串行输出 `serial_out` 是一根**裸线**（`output wire`），外部按需采。适合「我给你一个字，你按固定节拍逐位读走」。
- `Pipeline_Serial_Parallel` 在**串行输入侧**与**并行输出侧**各有一套 ready/valid 握手，所以它能嵌在弹性的 ready/valid 数据流里，做到「输入断了不丢、输出堵了不挤」。它是对更简单的 [Serial to Parallel Converter](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L16) 的推广（注释 [Pipeline_Serial_Parallel.v:16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L16) 明说）。

> **位序约定**：两个转换器都约定 **MSB first**（先送/先收最高位）。这并非任意选择，而是由 `Register_Pipeline`「`pipe_out` 读 MSB 端、移位方向 LSB→MSB」的内部几何直接决定的——下面 4.1.3 会看到这条线是如何从源码里读出来的。

#### 4.1.2 核心流程

**并→串（`Parallel_Serial`）的流程**：

```text
  parallel_in_valid ──▶ handshake_done = parallel_in_valid && parallel_in_ready
                              │ (完成并行载入)
                              ▼
   [Counter_Binary] ◀── load COUNT_BITS(=WORD_WIDTH-1)      ── 从 WORD_WIDTH-1 开始下数
   [Register_Pipeline] ◀── parallel_load (整字载入)          ── pipe_out 立刻可见 MSB
                              │
   每拍 count != 0：         │
     shifter_run=1 ──▶ 移一位(LSB→MSB) ──▶ serial_out 出下一个 bit；count--
                              │
   count == 0：               │
     停移位，serial_out 保持最后一位(LSB)；parallel_in_ready 重新拉高(可再次载入)
```

为什么计数器初值是 `WORD_WIDTH-1` 而不是 `WORD_WIDTH`？因为**载入那一拍 MSB 就已经出现在 `serial_out` 上了**（`pipe_out` = 最高级 = 字的 MSB，[Parallel_Serial.v:55-58](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L55-L58)），所以只需要再移 `WORD_WIDTH-1` 次就能把剩下 `WORD_WIDTH-1` 位依次送出，共 `WORD_WIDTH` 位。

**串→并（`Pipeline_Serial_Parallel`）的流程**（弹性、双侧握手）：

```text
  serial_in_ready = (count != 0) OR output_handshake_done    // 还有空位，或正好腾出一个位
  parallel_out_valid = (count == 0)                          // 移满了，宽字就绪

  每次 serial 输入握手(count != 0)：
     counter_run=1 ──▶ 移一个字进来 ──▶ count--

  count == 0：
     parallel_out_valid 拉高，等并行侧读走

  并行输出握手完成(output_handshake_done)：
     counter_load ← 重新装数；shifter 同时移位(把旧字顶出去)
        └─ 若本拍「正好也有串行输入握手」：装 WORD_COUNT_IN - 1 (本拍已移入 1 个)
        └─ 否则                          ：装 WORD_COUNT_IN
```

最后那条 `counter_load_value` 的「同拍减 1」是支持**连续不间断串行流**的关键（[Pipeline_Serial_Parallel.v:135](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L135)）：当一个宽字刚被读走的同一拍，正好又有一个串行字要进来，那么这一拍就已经消耗掉一次移位，剩余次数应当比「从零数起」少 1，否则会多出一个空拍、打断连续流。

#### 4.1.3 源码精读

**(a) `Parallel_Serial`：把 `Register_Pipeline` 当 1 位移位寄存器**

[Parallel_Serial.v:92-110](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L92-L110) 实例化移位寄存器——注意三组关键参数：

```verilog
Register_Pipeline #(
    .WORD_WIDTH (1),              // 每级 1 bit
    .PIPE_DEPTH (WORD_WIDTH),     // 级数 = 字宽
    .RESET_VALUES (WORD_ZERO)
) shift_register (
    ...
    .parallel_load (shifter_load),
    .parallel_in   (parallel_in), // 整字并行载入：parallel_in[i] → 第 i 级
    .pipe_in       (1'b0),        // 移位时低位补 0
    .pipe_out      (serial_out)   // 末级(MSB 端)输出
);
```

对照 u6-l2 的几何：`parallel_in[i]` 载入第 `i` 级，`pipe_out` 是最高级 `WORD_WIDTH-1`。载入后 `serial_out = parallel_in[WORD_WIDTH-1]` = MSB，与 [Parallel_Serial.v:4](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L4) 声明的「MSB first」一致。`serial_out` 是 `output wire`（[Parallel_Serial.v:41](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L41)），因为它直接来自子实例的 `pipe_out`，不在本模块内赋值——这正是 u2-l1 的 reg/wire 约定（来自实例 → wire）。

[Parallel_Serial.v:64-85](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L64-L85) 是下计数器：`INITIAL_COUNT=0`、`up_down=1`（下数）、`load_count = COUNT_BITS`，而 `COUNT_BITS = WORD_WIDTH - 1`（[Parallel_Serial.v:49](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L49)）。

控制逻辑只有一段组合块 [Parallel_Serial.v:120-129](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L120-L129)：

```verilog
parallel_in_ready = (count == COUNT_ZERO) && (clock_enable == 1'b1);   // 只在移完时才肯再吃一个字
handshake_done    = (parallel_in_valid == 1'b1) && (parallel_in_ready == 1'b1);
counter_run       = (count != COUNT_ZERO) && (clock_enable == 1'b1);
counter_load      = (handshake_done == 1'b1);                          // 握手完成 → 装载
shifter_run       = (counter_run == 1'b1) || (counter_load == 1'b1);   // 载入那一拍也要"移"(实际是 load)
shifter_load      = (counter_load == 1'b1);                            // 且这一拍是 load 而非 shift
```

注意 `shifter_run = counter_run || counter_load`：载入那一拍 `Register_Pipeline` 的 `clock_enable` 也得为 1，否则 `parallel_load` 不会生效——这是 u6-l2「Load overrides shift，但二者都受 `clock_enable` 门控」的体现。

**(b) `Pipeline_Serial_Parallel`：把 `Register_Pipeline` 当字移位寄存器 + 双侧握手**

[Pipeline_Serial_Parallel.v:94-112](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L94-L112) 实例化移位寄存器，这次参数换成「整字移位」：

```verilog
Register_Pipeline #(
    .WORD_WIDTH (WORD_WIDTH_IN),     // 每级 = 一个输入字的宽度
    .PIPE_DEPTH (WORD_COUNT_IN),     // 级数 = 输入字数
    .RESET_VALUES (WORD_ZERO_OUT)
) shift_register (
    ...
    .parallel_load (1'b0),           // 纯移位寄存器,从不并行载入
    .parallel_in   (WORD_ZERO_OUT),
    .parallel_out  (parallel_out),   // 整条流水线 = 拼好的宽字
    .pipe_in       (serial_in),      // 串行字从 LSB 端移入
    .pipe_out      ()                // 末级溢出丢弃
);
```

字序与位序同理：`serial_in`（**最高位字 first**，[Pipeline_Serial_Parallel.v:4](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L4)）从 stage 0 移入，移满 `WORD_COUNT_IN` 次后，最先进入的字到达最高级 stage `WORD_COUNT_IN-1`——即宽字的 MSB 字位置，正合「MSB word first」。

计数器 [Pipeline_Serial_Parallel.v:67-88](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L67-L88) 的位宽有个值得注意的细节：

```verilog
localparam COUNT_WIDTH = clog2(WORD_COUNT_IN) + 1;   // 要能表示到 N,不是 N-1
...
.INITIAL_COUNT (WORD_COUNT_IN [COUNT_WIDTH-1:0])      // 清零后从 WORD_COUNT_IN 开始下数
```

这正是 u8-l2 的位数陷阱：计数初值是 `WORD_COUNT_IN` 本身（不是 `WORD_COUNT_IN-1`），要能装下这个值就需要 `clog2(N)+1` 位。`clear` 后 `Counter_Binary` 把 `INITIAL_COUNT` 装入，于是 `count = WORD_COUNT_IN`，准备接收 `WORD_COUNT_IN` 个字。

控制逻辑 [Pipeline_Serial_Parallel.v:126-136](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L126-L136) 把两套握手事件（[Pipeline_Serial_Parallel.v:121-124](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L121-L124) 先算好）汇成移位/装载命令：

```verilog
parallel_out_valid =  (count == COUNT_ZERO)                                    && (clock_enable);
serial_in_ready    = ((count != COUNT_ZERO) || (output_handshake_done == 1'b1)) && (clock_enable);
counter_run        =  (count != COUNT_ZERO) && (input_handshake_done == 1'b1)  && (clock_enable);
counter_load       = (output_handshake_done == 1'b1);
shifter_run        = (counter_run == 1'b1) || (counter_load == 1'b1);
counter_load_value = ((output_handshake_done) && (input_handshake_done)) ? WORD_COUNT_IN - 1 : WORD_COUNT_IN;
```

最后一行（[Pipeline_Serial_Parallel.v:135](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L135)）就是 4.1.2 说的「同拍减 1」机关。`parallel_out` 是 `output wire`（来自子实例），而 `parallel_out_valid`、`serial_in_ready` 是 `output reg`（本模块组合逻辑赋值，[Pipeline_Serial_Parallel.v:34](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L34)、[Pipeline_Serial_Parallel.v:37](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L37)）——再次印证 u2-l1 的类型约定。

#### 4.1.4 代码实践：手算 `Parallel_Serial` 的逐位输出

**实践目标**：给定一个 4 位字，手工推演 `serial_out` 随时钟逐位的取值，验证「MSB first」与「计数器初值 = WORD_WIDTH−1」。

**操作步骤**：

1. 设 `WORD_WIDTH = 4`，则 `COUNT_BITS = 3`、`COUNT_WIDTH = clog2(4) = 2`（[Parallel_Serial.v:47-49](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L47-L49)）。
2. 载入 `parallel_in = 4'b1011`（MSB=1，LSB=1）。
3. 假设 `clock_enable` 恒为 1。`parallel_in_valid` 在 T0 拉高，T0 完成握手（`count==0` 故 `parallel_in_ready==1`）→ 该拍 `counter_load=1`、`shifter_load=1`：计数器装入 3，移位寄存器装入 `1011`。
4. 逐拍推演（边沿对齐以「该拍可见的 `serial_out`」为准）：

   | 拍 | `count` | `serial_out`（=末级=MSB 端） | 说明 |
   | --- | --- | --- | --- |
   | T1 | 3 | **1** | 载入后立即可见 MSB（`parallel_in[3]`） |
   | T2 | 2 | **0** | 移一位：末级←原次级（`parallel_in[2]`） |
   | T3 | 1 | **1** | 再移一位（`parallel_in[1]`） |
   | T4 | 0 | **1** | 再移一位（`parallel_in[0]`=LSB）；`count==0` 停移位，`parallel_in_ready` 重新拉高 |

5. 把 `serial_out` 的序列读出来：`1, 0, 1, 1`——正是 `1011` 的 **MSB first** 串行化。

**需要观察的现象**：`count` 从 3 递减到 0 共下数 3 次（移位 3 次），加上载入那拍自带 1 位，恰好输出 4 位；`count==0` 后 `serial_out` 停在最后一位（LSB=1）不动，等下一次载入。

**预期结果**：串行序列为 `1→0→1→1`。逐拍的精确边沿对齐（载入与首次移位的相对时序）待本地仿真确认，但「4 位、MSB first、末位保持」这三点由源码逻辑确定。

#### 4.1.5 小练习与答案

**练习 1**：`Parallel_Serial` 里 `pipe_in` 接的是 `1'b0`（[Parallel_Serial.v:108](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Parallel_Serial.v#L108)）。随着 bit 不断移出，移位寄存器低位会被 0 填满。这会不会导致下一字载入时出错？

> **答案**：不会。载入是通过 `parallel_load`（`shifter_load`）**整体并行载入**——`Register_Pipeline` 的「Load overrides shift」（[Register_Pipeline.v:10-12](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L10-L12)）保证载入时每一级都直接取 `parallel_in[i]`，与之前移位残留的 0 无关。`pipe_in=1'b0` 只在「移位但不载入」时起作用，补进来的 0 在下次载入时被整体覆盖。

**练习 2**：`Pipeline_Serial_Parallel` 若把 `counter_load_value` 那行的 `WORD_COUNT_IN - 1` 改成 `WORD_COUNT_IN`（即永远不减 1），连续串行流会出什么问题？

> **答案**：当一个宽字在 T 拍被读走、且 T 拍恰好又移入一个新字时，计数器会被装成 `WORD_COUNT_IN` 而非 `WORD_COUNT_IN-1`。这意味着它会以为「还要再移 `WORD_COUNT_IN` 次」，但实际本拍已经移了 1 次，于是会多收一个字、把整条流往后推一拍，在「不间断连续流」场景下会出现一个空拍或错位。这个「同拍减 1」正是为消除该空拍而设（[Pipeline_Serial_Parallel.v:58-61](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Serial_Parallel.v#L58-L61) 的注释也说明了这一点）。

**练习 3**：同样是「串→并」，`Pipeline_Serial_Parallel` 与一个普通「并入并出」的 `Register_Pipeline`（纯时延）相比，多花了什么？为什么值得？

> **答案**：多了：①一个 `Counter_Binary` 数剩余移位次数；②一套组合控制逻辑，把移位绑到「握手完成」、并在满/空之间切换 `serial_in_ready`/`parallel_out_valid`。值得，因为这让串→并成为一个**弹性接口模块**：它能在串行输入断流时保持已收数据不丢、在并行输出反压时停止接收，从而安全地塞进任意 ready/valid 流水线——纯时延流水线没有这种自我节流能力。

---

### 4.2 差分反串行化：1:N 硬件 SERDES 前端

#### 4.2.1 概念说明

`Deserializer_Differential_1toN` 是 Xilinx Series-7 专用模块（[Deserializer_Differential_1toN.v:17-18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L17-L18)），用片上 SERDES 硬核把一路高速差分串行流（如 LVDS）反串行化成并行字。它和 4.1 的软转换器解决同一类问题，但工作在 Gbit/s 量级——这个速度下普通触发器已无法可靠采样，必须靠专用硬件。

它有三个鲜明特征：

1. **差分 + 双极性**：用 `IBUFDS_DIFF_OUT` 把差分输入拆成正（`datain_p`）、负（`datain_n`）两路各自缓冲，然后**正负各走一条独立的延迟+SERDES 链**（[Deserializer_Differential_1toN.v:89-97](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L89-L97)）。所以输出是 `datain_p_parallel` 与 `datain_n_parallel` 两组并行字。
2. **两种训练，都交给外部**：高速接口开机时必须「对齐」——既要**位对齐**（把采样时刻调到数据眼图正中，靠调 IDELAY 抽头实现），又要**字对齐**（找到正确的字边界，靠 bitslip 实现）。本模块只提供这两件训练的**硬件钩子**（抽头增减/装载、bitslip 脉冲），训练算法由外部模块完成（[Deserializer_Differential_1toN.v:6-9](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L6-L9)）。
3. **1:N 比率**：`N` 就是 `DATA_WIDTH`。例如 DDR、`clk_serial` 300 MHz、`clk_parallel` 100 MHz ⇒ `DATA_WIDTH = 6`、比率 1:6（[Deserializer_Differential_1toN.v:11-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L11-L13)）。DDR 在每个串行时钟周期采 2 bit（用 `CLK` 与 `CLKB=~CLK` 两个边沿），故 300 MHz×2 = 600 Mbit/s，正好等于 100 MHz×6 bit。

> **两个时钟域**：串行数据在 `clk_serial`（高速 I/O 时钟）域；所有控制与并行输出在 `clk_parallel`（低速、分频后的时钟）域（[Deserializer_Differential_1toN.v:32-33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L32-L33)）。SERDES 硬件天然跨在这两个域之间——它用 `CLK`/`CLKB` 把 bit 一位位搬进来，每攒够 N 位就在 `CLKDIV` 边沿一并吐出并行字。

#### 4.2.2 核心流程

整条接收链（正路；负路完全对称）：

```text
datain_p ─┐                                                                         datain_p_parallel[N-1:0]
          ├─ IBUFDS_DIFF_OUT ─→ datain_p_buffered ─→ IDELAYE2 ─→ datain_p_delayed ─→ ISERDESE2 ──────────────▶
datain_n ─┘                    ─→ datain_n_buffered ─→ IDELAYE2 ─→ datain_n_delayed ─→ ISERDESE2 ──┐ datain_n_parallel[N-1:0]
                                                                                                    │
   IDELAYE2:  位对齐训练(调抽头)   ◀── tap_*_load/incdec_p (外部)                                     │ (Q 反向接线 → 并行字)
   ISERDESE2: 字对齐训练(bitslip)  ◀── datain_*_bitslip  (外部)                                       │
              CLK=clk_serial, CLKB=~clk_serial, CLKDIV=clk_parallel ─────────────────────────────────┘
```

四级各自的责任：

- **`IBUFDS_DIFF_OUT`**（[Deserializer_Differential_1toN.v:98-110](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L98-L110)）：差分输入缓冲器，**带差分输出**——同时给出正端 `O` 与负端 `OB`，使正、负两路能各自独立延迟与反串行化。
- **`IDELAYE2`**（正/负各一）：可编程抽头延迟线，`IDELAY_TYPE = "VAR_LOAD"`，5 bit 抽头（0–31，`TAP_COUNTER_WIDTH=5`）。外部模块通过 `CE`/`INC`（增减）、`LD`+`CNTVALUEIN`（装载）来调抽头值，把采样时刻移到数据眼中央——这就是**位对齐**。当前抽头值从 `CNTVALUEOUT`（`tap_*_current`）读回。
- **`ISERDESE2`**（正/负各一）：真正的 1:N 反串行化硬核。`INTERFACE_TYPE="NETWORKING"`、`DATA_RATE`（SDR/DDR）、`DATA_WIDTH`（硬件原生支持 2–8, 10, 14）。它用 `CLK`（+DDR 时的 `CLKB`）高速采串行 bit，用 `CLKDIV` 吐并行字；`BITSLIP` 每拉一次就把输出字挪一位——这就是**字对齐**。
- **`IDELAYCTRL`**（**不在本模块内**，由 4.3 的 `IDELAYCTRL_Instance` 提供）：校准同一 I/O Bank 内所有 IDELAYE2 的延迟线。没有它，上面的位对齐训练就建立在「未知延迟」上，毫无意义。

#### 4.2.3 源码精读

**(a) 输入延迟 IDELAYE2（正路）**

[Deserializer_Differential_1toN.v:125-152](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L125-L152) 实例化正路延迟线。几个要点：

```verilog
(* IODELAY_GROUP = IODELAY_GROUP *)   // 必须与同 Bank 的 IDELAYCTRL 同组
IDELAYE2 #(
    .DELAY_SRC           ("IDATAIN"),       // 延迟来自 I/O 的数据
    .IDELAY_TYPE         ("VAR_LOAD"),      // 抽头可由外部装载/增减(位对齐训练用)
    .IDELAY_VALUE        (0),
    .REFCLK_FREQUENCY    (IODELAY_REFCLK_FREQUENCY),  // 必须与 IDELAYCTRL 的参考时钟频率一致
    ...
) input_data_delay_p (
    .CNTVALUEOUT (tap_p_current),     // 回读当前抽头
    .DATAOUT     (datain_p_delayed),  // 延迟后的数据 → 送 ISERDESE2
    .CE          (incdec_p_enable),   // 允许增/减
    .CNTVALUEIN  (tap_p_load_value),  // 装载的新抽头值
    .INC         (incdec_p),          // 1=增,0=减
    .LD          (tap_p_load),        // 装载抽头
    .IDATAIN     (datain_p_buffered), // 来自差分缓冲的正端
    .REGRST      (reset_parallel)
);
```

注意 [Deserializer_Differential_1toN.v:125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L125) 与 [Deserializer_Differential_1toN.v:158](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L158) 的 `(* IODELAY_GROUP = ... *)` 属性——它把这两个 IDELAYE2 与本 Bank 内唯一的 `IDELAYCTRL`（4.3）绑成一组，是 u4-l2「属性随声明走、必须写在源码里」的典型例证。负路延迟线在 [Deserializer_Differential_1toN.v:160-185](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L160-L185)，完全对称。

**(b) 反串行化 ISERDESE2 与「反向接线」**

[Deserializer_Differential_1toN.v:201-267](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L201-L267) 实例化正路 SERDES。本模块**最关键的一处设计**是并行输出的接线——`ISERDESE2` 的 `Q1..Q8` 输出是**按位反向**的，所以要把它们逆序接到并行字上（以示例 `DATA_WIDTH=6` 为例，[Deserializer_Differential_1toN.v:229-234](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L229-L234)）：

```verilog
// *** BIT-REVERSED ORDER! ***
.Q1 (datain_p_parallel[5]),     // Q1 → 最高位
.Q2 (datain_p_parallel[4]),
.Q3 (datain_p_parallel[3]),
.Q4 (datain_p_parallel[2]),
.Q5 (datain_p_parallel[1]),
.Q6 (datain_p_parallel[0]),     // Q6 → 最低位
.Q7 (),                         // 未用
.Q8 (),
```

源码注释明确解释了这么做的原因，并强调了一条工程纪律——**改 `DATA_WIDTH` 必须手工重接这里的线**（[Deserializer_Differential_1toN.v:192-197](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L192-L197)，亦见头部 [Deserializer_Differential_1toN.v:27-30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L27-L30)）。作者宁可手接、也不愿搞一套「聪明的自动重连」——因为那会生成一堆「未用线」告警，且 `DATA_WIDTH` 在一个设计里基本不会变，自动化得不偿失。这是 u4-l1「模块即设计意图」的另一种体现：把不常变的接线写死、写得醒目。

时钟接线（[Deserializer_Differential_1toN.v:251-253](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L251-L253)）也很能说明 SERDES 的工作方式：

```verilog
.CLK    (clk_serial),       // 高速时钟(采 bit)
.CLKB   (~clk_serial),      // 高速反相时钟(DDR 第二个边沿)
.CLKDIV (clk_parallel),     // 分频时钟(出并行字)
```

字对齐训练钩子是 `BITSLIP`（[Deserializer_Differential_1toN.v:245](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L245)）：外部每发一个 `datain_p_bitslip` 脉冲，输出字就整体挪一位，直到对齐到正确的字边界。负路 SERDES 在 [Deserializer_Differential_1toN.v:271-337](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L271-L337)，结构相同、`Q` 反向接线在 [Deserializer_Differential_1toN.v:299-304](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L299-L304)。

#### 4.2.4 代码实践：画 1:4 反串行化的比特重排时序图

> 这是本讲的核心实践。我们以 **SDR、1:4**（最简形态）为例手画时序，把「串行 bit 流」与「并行字位」的对应关系，连同 `ISERDESE2` 的反向接线，一次讲清。

**实践目标**：给定一条 SDR 串行流，画出 4 个串行 bit 周期（= 1 个并行周期）内 bit 的到达顺序、它们落到 `Q1..Q4` 的位置、以及经反向接线后 `datain_p_parallel[3:0]` 的最终取值。

**前置设定**：

- `DATA_RATE = "SDR"`、`DATA_WIDTH = 4`，故 `clk_serial` 是 `clk_parallel` 的 4 倍（每个串行边沿采 1 bit，4 个边沿攒成 1 个 4-bit 字）。
- 反向接线规则（由 [Deserializer_Differential_1toN.v:229-234](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L229-L234) 的 1:6 规则缩到 1:4）：`Q1→parallel[3]`、`Q2→parallel[2]`、`Q3→parallel[1]`、`Q4→parallel[0]`，`Q5..Q8` 不接。

**第 1 步：画时钟与串行流。** 设串行流按时间顺序到达的 bit 为 `s0, s1, s2, s3, s4, s5, s6, s7, …`（`s0` 最先到）。

```text
clk_serial:   ┌─┐_┌─┐_┌─┐_┌─┐_┌─┐_┌─┐_┌─┐_┌─┐_   (每周期采 1 bit,上升沿)
serial in:      s0  s1  s2  s3  s4  s5  s6  s7 ...
                ├── 并行周期 #0 ──┤├── 并行周期 #1 ──┤
clk_parallel: ┌─────────────┐___┌─────────────┐
                (CLKDIV 边沿在此出并行字)
```

**第 2 步：标注 `Q1..Q4` 与反向接线。** 在每个 `CLKDIV`（`clk_parallel`）边沿，`ISERDESE2` 把攒满的 4 bit 经 `Q1..Q4` 输出。由于硬件输出是「按位反向」的，源码用反向接线补偿——接线表如下：

| `ISERDESE2` 输出 | 接到 `datain_p_parallel` 的位 | 字中位置 |
| --- | --- | --- |
| `Q1` | `[3]` | 最高位 |
| `Q2` | `[2]` | |
| `Q3` | `[1]` | |
| `Q4` | `[0]` | 最低位 |

**第 3 步：说明重排的必要性。** `ISERDESE2` 的移位方向决定了 `Q1..Q4` 的物理顺序与期望的逻辑字序相反，因此若**正向接**（`Q1→[0]`）会得到一个比特位全反的字；源码的反向接线（`Q1→[3]`）正是把这层反转再翻回来，恢复逻辑位序。最终逻辑位与串行到达顺序的精确对应，需在实验室用已知训练码型（配合 bitslip）确认——这是为什么 [Deserializer_Differential_1toN.v:27-30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L27-L30) 要求改 `DATA_WIDTH` 后手工重接并实测。

**需要观察的现象**：每 4 个 `clk_serial` 周期产出 1 个 4-bit 并行字；`datain_p_bitslip` 每发一次脉冲，下一个并行字的 4 bit 整体在流里挪一位（字边界移动）。

**预期结果（待本地验证）**：连续两拍 `clk_parallel` 应分别输出由 `{s0,s1,s2,s3}` 与 `{s4,s5,s6,s7}` 组成的两个 4-bit 字（具体哪一位落 `[3]`/`[0]` 以反向接线 + 实测训练为准）。改用 DDR 时，`clk_serial` 只需 2 倍 `clk_parallel`（每周期 `CLK`+`CLKB` 两个边沿各采 1 bit），同样攒出 4 bit/并行周期。

#### 4.2.5 小练习与答案

**练习 1**：为什么本模块坚持用 `IBUFDS_DIFF_OUT`（**带差分输出**的缓冲），而不是普通的 `IBUFDS`（只一个单端输出）？

> **答案**：因为本模块要**同时**反串行化正极性（`datain_p`）和负极性（`datain_n`）两路（输出 `datain_p_parallel` 和 `datain_n_parallel` 两组并行字）。`IBUFDS_DIFF_OUT` 同时给出正端 `O` 与负端 `OB`，让两路各自接独立的 `IDELAYE2`+`ISERDESE2`；普通 `IBUFDS` 只给一个单端输出，无法分出负路。

**练习 2**：[Deserializer_Differential_1toN.v:27-30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L27-L30) 要求「改 `DATA_WIDTH` 必须手工重接 `Q` 输出」。如果改成了却忘了重接（比如仍是 1:6 的接法），综合会报错吗？运行会怎样？

> **答案**：综合多半**不会报错**，只会警告「某些 `datain_p_parallel` 位未被驱动」或「某些 `Q` 悬空」。但运行时并行字会错位/错序——高位读不到、低位读成 0 或重复。这正是作者宁可要「吵闹的、需要手改的接线」也不要「静默的自动重连」的原因：手改是显式的、可复核的；自动重连一旦算错会静默出错（呼应 u1-l2「参数默认 0 = 让失败吵闹」的设计哲学）。

**练习 3**：位对齐（调 IDELAY 抽头）和字对齐（bitslip）各解决什么问题？它们能互相替代吗？

> **答案**：**位对齐**解决「在每一个 bit 的时间窗内，采样时刻是否落在数据眼中央」——靠 `IDELAYE2` 把采样点在时间轴上微调（亚纳秒级，每抽头约 78ps）。**字对齐**解决「相邻 bit 被正确分组到正确的字边界」——靠 `BITSLIP` 整字挪位。两者解决不同维度的问题（一个是「每个 bit 采得准不准」，一个是「bit 之间怎么分组」），不能互相替代：位没对齐时采到的 bit 本身就是错的，字对齐再多也没用；位对齐了但字边界错，则每个字都会是两个相邻字的错位拼接。所以训练通常**先位对齐、再字对齐**。

---

### 4.3 IDELAYCTRL：让延迟线可信的校准器

#### 4.3.1 概念说明

`IDELAYE2` 的抽头延迟是一段**模拟延迟线**，其每个抽头的实际延迟量会随工艺（P）、电压（V）、温度（T）漂移——未经校准时，你根本不知道「调 5 个抽头」到底延迟了多少皮秒。位对齐训练（4.2）要把采样时刻精确移到数据眼中央，这只有在「抽头延迟量稳定且已知」时才有意义。

`IDELAYCTRL` 就是干这件事的：它拿一根**参考时钟**（`REFCLK`，允许频率见 UG471，例如 190–210 或 290–310 MHz），持续把同一 I/O Bank 内所有 `IDELAYE2`（与 `ODELAY2`）的延迟线**校准**到一个稳定、已知的标准。校准好之后，每个抽头代表一个确定的延迟量（Series-7 名义上约 78 ps/抽头），PVT 漂移被持续补偿。

关于它有三条使用纪律（写在 [IDELAYCTRL_Instance.v:13-19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L13-L19)）：

1. **每个 I/O Bank 一个 `IDELAYCTRL`**，且被它控制的 `IDELAYE2` 必须在同一 `IODELAY_GROUP` 里。本模块的 `IODELAY_GROUP` 参数会被当作 `(* *)` 属性套到 `IDELAYCTRL` 实例上（[IDELAYCTRL_Instance.v:34](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L34)），与 4.2 里 IDELAYE2 上的同名属性配对——这是 u4-l2「属性随声明走、必须写在源码里」原则的又一次落地（属性不在外部约束文件里，否则漏写一行就静默失去校准、MTBF 崩塌）。
2. **参考时钟必须稳**。`ready` 一旦掉下，说明校准丢失（多半是 `reference_clock` 抖动/ glitch），必须复位并重新训练整个接口（[IDELAYCTRL_Instance.v:16-17](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L16-L17)）。
3. **`IODELAY_REFCLK_FREQUENCY` 必须处处一致**：`IDELAYCTRL` 的 `reference_clock` 频率，与每个 `IDELAYE2` 的 `REFCLK_FREQUENCY` 参数（4.2 里 [Deserializer_Differential_1toN.v:135](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L135)）必须报同一个值，否则二者对「一个抽头是多少延迟」的理解不一致。

> **为什么差分反串行化之前必须有它？** 把 4.2 与 4.3 串起来看就清楚了：`Deserializer_Differential_1toN` 靠 `IDELAYE2` 做位对齐；`IDELAYE2` 的抽头延迟只有在被 `IDELAYCTRL` 持续校准时才稳定可信。没有 `IDELAYCTRL`（或它没 `ready`），调抽头就是「在一个会漂移、未知刻度的延迟线上盲调」——位对齐训练没有任何确定含义，高速接口根本无从稳定工作。所以 `IDELAYCTRL_Instance` 是 `Deserializer_Differential_1toN` 能成立的前置条件，二者通过共享的 `IODELAY_GROUP` 与相同的 `REFCLK_FREQUENCY` 绑定。

#### 4.3.2 核心流程

```text
                reference_clock (REFCLK, 允许频段见 UG471)
                        │
                        ▼
   (* IODELAY_GROUP = ... *)
   ┌─────────────────────────────┐
   │         IDELAYCTRL          │   持续校准同 Bank 同组的 IDELAYE2/ODELAY2 延迟线
   │                             │
   │  REFCLK ◀── reference_clock │
   │  RST   ◀── reset            │
   │  RDY   ──▶ ready            │──▶ ready=1: 抽头延迟已校准,可信任
   └─────────────────────────────┘           ready=0: 校准丢失(参考时钟 glitch),需复位重训
```

`IDELAYCTRL` 本身**没有数据通路**——它不碰任何数据信号，只输出一个状态位 `ready`，其作用是「在后台维持延迟线刻度的准确性」。所以它常常被画在数据流之外，却是整个高速接收链能否工作的前提。

#### 4.3.3 源码精读

`IDELAYCTRL_Instance.v` 整个就是一个**薄包装**（[IDELAYCTRL_Instance.v:23-43](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L23-L43)）：

```verilog
module IDELAYCTRL_Instance #(
    parameter IODELAY_GROUP = ""    // 必须与 IDELAY2 块匹配
)(
    input  wire reference_clock,
    input  wire reset,
    output wire ready
);

    (* IODELAY_GROUP = IODELAY_GROUP *)   // 把参数转成原语属性
    IDELAYCTRL idelay2_control (
        .RDY    (ready),            // 校准就绪
        .REFCLK (reference_clock),  // 参考时钟
        .RST    (reset)             // 高有效复位
    );
endmodule
```

注释 [IDELAYCTRL_Instance.v:9-11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L9-L11) 点明了这个包装的唯一价值：**把 Verilog 属性（`(* IODELAY_GROUP = ... *)`）转成模块参数**。这样上层设计在实例化时只要传一个普通参数 `IODELAY_GROUP`，而不必在实例处写属性——组合进大设计时更干净（这是 u4-l2「Instance/Adapter 层缩放与连线」的一个小范例）。

#### 4.3.4 代码实践：核验 IDELAYCTRL 与反串行化的绑定关系

> 本实践对应任务的后半句：**说明为何差分反串行化前需要 `IDELAYCTRL_Instance`**。它是一个跨文件的源码阅读型任务。

**实践目标**：通过阅读两个文件，确认 `IDELAYCTRL_Instance` 与 `Deserializer_Differential_1toN` 之间「同组、同频」的两条绑定，并据此解释「为什么必须有它」。

**操作步骤**：

1. 在 [IDELAYCTRL_Instance.v:34](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L34) 找到 `(* IODELAY_GROUP = IODELAY_GROUP *)`；再在 [Deserializer_Differential_1toN.v:125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L125) 与 [Deserializer_Differential_1toN.v:158](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L158) 找到两个 IDELAYE2 上同样的属性。**结论**：三处必须报同一个 `IODELAY_GROUP` 字符串，校准才能作用到这两条延迟线上。
2. 在 [IDELAYCTRL_Instance.v:19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L19) 看到「参考时钟频率范围见 UG471」；在 [Deserializer_Differential_1toN.v:135](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L135) 与 [Deserializer_Differential_1toN.v:168](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L168) 看到 `REFCLK_FREQUENCY (IODELAY_REFCLK_FREQUENCY)`。**结论**：`IDELAYCTRL` 的参考时钟频率与 IDELAYE2 的 `REFCLK_FREQUENCY` 必须一致。
3. 据此回答「为什么反串行化前需要它」：`Deserializer_Differential_1toN` 用 IDELAYE2 做位对齐；IDELAYE2 抽头刻度由 `IDELAYCTRL` 校准；没有它（或 `ready=0`），抽头延迟不可信，位对齐无意义 → 故 `IDELAYCTRL_Instance` 必须先就位且 `ready`，反串行化才能稳定工作。

**需要观察的现象**：`IODELAY_GROUP` 在两个文件里成对出现（一个 IDELAYCTRL + 两个 IDELAYE2，共三处属性）；`REFCLK_FREQUENCY` 同样成对出现。

**预期结果**：列出三处 `IODELAY_GROUP` 与两处 `REFCLK_FREQUENCY` 的行号，确认它们在设计实例化时必须取一致值——这正是 IDELAYCTRL 能否「管住」这些延迟线的结构保证。

#### 4.3.5 小练习与答案

**练习 1**：`IDELAYCTRL_Instance` 的 `ready` 输出在系统里通常该怎么用？

> **答案**：`ready` 是「延迟线已校准、可信任」的指示。设计里通常用它（连同复位完成等其它就绪信号）门控高速接收链的「开始训练/开始接收」——只有 `ready=1` 才允许外部模块去做位对齐（调抽头）与字对齐（bitslip），并最终放开数据通路。一旦 `ready` 掉下（参考时钟 glitch 等），应复位并重新训练整个接口（[IDELAYCTRL_Instance.v:16-17](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L16-L17)）。

**练习 2**：`IDELAYCTRL_Instance` 把 `(* IODELAY_GROUP = ... *)` 属性「转成参数」（[IDELAYCTRL_Instance.v:9-11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L9-L11)）。如果不转、直接在上层实例化 `IDELAYCTRL` 原语并手写属性，会有什么问题？

> **答案**：功能上没区别，但工程上更易出错：属性散落在各实例处，容易漏写或拼错组名，且无法被参数化复用。包装成参数后，`IODELAY_GROUP` 像普通参数一样被上层统一传入、在模块内统一施加属性，减少手写属性带来的不一致风险——这正是「包装原语」的标准收益。

**练习 3**：一个设计里用了 3 个不同 I/O Bank 的 IDELAYE2。需要几个 `IDELAYCTRL_Instance`？它们的 `IODELAY_GROUP` 该怎么设？

> **答案**：需要 **3 个** `IDELAYCTRL_Instance`，每个 I/O Bank 一个（[IDELAYCTRL_Instance.v:15](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/IDELAYCTRL_Instance.v#L15)）。可以给三个组取不同的 `IODELAY_GROUP` 名字（每组内的 IDELAYE2 与对应 IDELAYCTRL 同名），也可以都取同名——关键是**每个 Bank 内**的 IDELAYE2 与本 Bank 的 IDELAYCTRL 同组。三个 IDELAYCTRL 可共用同一根参考时钟（同频即可），各自给自己的 Bank 提供校准。

---

## 5. 综合实践

设计一个 **LVDS ADC/摄像头数据接收前端的参数规划与模块选型**，把本讲三块内容（软串并转换、硬反串行化、IDELAYCTRL）串起来。

**场景**：一颗外部 ADC 通过 1 对 LVDS 差分线，以 600 Mbit/s 的线速率向 FPGA 持续送采样数据；FPGA 内部希望以 100 MHz 的并行时钟、按字处理这些数据。

**任务**：

1. **算比率与位宽**：线速率 600 Mbit/s、`clk_parallel` 100 MHz ⇒ 每个 `clk_parallel` 周期要收 6 bit，即 `DATA_WIDTH = 6`、比率 1:6。若选 SDR，`clk_serial` 要多快？若选 DDR 呢？（提示：参照 [Deserializer_Differential_1toN.v:11-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L11-L13) 的范例。）
2. **决定软还是硬**：能否用 4.1 的 `Pipeline_Serial_Parallel` 来收这 600 Mbit/s 的 LVDS 流？为什么？（提示：普通逻辑触发器的建立/保持时间能否采稳亚纳秒级串行数据？软转换器每个周期只能搬 1 个串行元素。）
3. **画出接收链**：用 `Deserializer_Differential_1toN` + `IDELAYCTRL_Instance` 画出从 LVDS 引脚到 6-bit 并行字的接收链，标出 `clk_serial`/`clk_parallel`、`IODELAY_GROUP`、`REFCLK_FREQUENCY` 三处必须一致的绑定。
4. **解释训练顺序**：上电后应当先做什么、再做什么？（提示：先等 `IDELAYCTRL` 的 `ready` → 再做位对齐（调抽头）→ 再做字对齐（bitslip）→ 才放开数据通路。）
5. **软转换器的用武之地**：如果这 6-bit 并行字进入 FPGA 后，还要被「打散成 6 个独立的 1-bit 串行流」送给某个低速外设，应该用 4.1 的哪个模块？给出参数取值。

**参考要点**：

1. SDR 需 `clk_serial = 600 MHz`（每个上升沿 1 bit × 6 = 6 bit/并行周期）；DDR 需 `clk_serial = 300 MHz`（`CLK`+`CLKB` 两边沿各 1 bit，每周期 2 bit × 3 周期 = 6 bit）。实际多用 DDR，`clk_serial` 频率更低、更易实现——这正是源码范例（[Deserializer_Differential_1toN.v:11-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L11-L13)）取 DDR/300 MHz/100 MHz/6 的原因。
2. **不能**用软转换器。600 Mbit/s 对应每位仅约 1.67 ns，普通 FPGA 逻辑触发器无法在这么窄的窗内可靠建立/保持采样；软转换器还受「每周期搬 1 个串行元素」限制，需 ~600 MHz 的逻辑时钟，远超普通布线能力。必须用片上 SERDES 硬核。
3. `datain_p/datain_n` → `IBUFDS_DIFF_OUT` → `IDELAYE2`（+`IDELAYCTRL_Instance` 经 `IODELAY_GROUP` 校准）→ `ISERDESE2`（`CLK=clk_serial`、`CLKB=~clk_serial`、`CLKDIV=clk_parallel`）→ `datain_p_parallel[5:0]`（反向接线）。三处绑定：`IODELAY_GROUP` 同名（IDELAYCTRL + 两 IDELAYE2）、`REFCLK_FREQUENCY` 一致、`DATA_WIDTH` 决定反向接线行数。
4. 先等 `IDELAYCTRL` 的 `ready=1`（延迟线已校准）→ 位对齐（外部调 `tap_*` 把采样移到眼中央）→ 字对齐（发 `datain_*_bitslip` 找字边界）→ 放开后续处理。`ready` 掉下则重来。
5. 用 `Parallel_Serial`（并→串）：`WORD_WIDTH = 6`，把每个 6-bit 并行字按 MSB first 逐位打到低速外设；若要 6 路独立 1-bit 流而非时分复用的一根线，则用 6 个 `WORD_WIDTH=1` 的实例或直接拆线。具体拍数/速率待本地仿真与外设时序确认。

## 6. 本讲小结

- **串并转换有软硬两条路**。软路线（`Parallel_Serial`、`Pipeline_Serial_Parallel`）用 `Register_Pipeline` 当移位寄存器 + `Counter_Binary` 数移位次数，跨厂商可综合、带 ready/valid 弹性接口，但每周期只搬 1 个串行元素、速度有限；硬路线（`Deserializer_Differential_1toN`）直接用 Xilinx Series-7 的 SERDES/IDELAY 硬核，工作在 Gbit/s 量级。
- **软转换器的核心是「移位寄存器的两种参数化」**（u6-l2）：`Parallel_Serial` 取 `WORD_WIDTH=1`、`PIPE_DEPTH=WORD_WIDTH`（逐位移出，MSB first，计数器初值 `WORD_WIDTH-1` 因为载入那拍 MSB 已可见）；`Pipeline_Serial_Parallel` 取 `WORD_WIDTH=字宽`、`PIPE_DEPTH=字数`（逐字移入拼成宽字，MSB word first，`COUNT_WIDTH=clog2(N)+1` 是 u8-l2 位数陷阱的实例）。后者靠 `counter_load_value` 的「同拍减 1」支持不间断连续流。
- **硬反串行化是一条四级流水线**：`IBUFDS_DIFF_OUT`（差分拆正负两路）→ `IDELAYE2`（可编程抽头延迟，做位对齐）→ `ISERDESE2`（1:N 反串行化硬核，`CLK`/`CLKB`/`CLKDIV` 跨双时钟域）→ 并行字；正负两路对称。两种训练（位对齐调抽头、字对齐 bitslip）都由外部模块完成。
- **`ISERDESE2` 输出按位反向，必须反向接线**（`Q1→parallel[N-1] … Qn→parallel[0]`，[Deserializer_Differential_1toN.v:229-234](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Deserializer_Differential_1toN.v#L229-L234)），且改 `DATA_WIDTH` 必须手工重接——作者宁可显式手改也不要静默的自动重连。
- **`IDELAYCTRL_Instance` 是硬前端的可信前提**：它用参考时钟持续校准同 I/O Bank、同 `IODELAY_GROUP` 的 `IDELAYE2` 延迟线，使抽头延迟在 PVT 下稳定已知。没有它（或 `ready=0`），位对齐训练建立在未知延迟上、毫无意义——这就是「差分反串行化之前必须先有 IDELAYCTRL」的根本原因。
- 贯穿全讲的设计纪律：u4-l2「属性随声明走」（`IODELAY_GROUP`、`(* *)` 必须写在源码里）、u4-l1/u17-l1「用构建块/原语组装、自写逻辑极少」（软转换器只有组合 `always @(*)`）、u1-l2「让失败吵闹」（手接 `Q` 线、参数默认 0）。

## 7. 下一步学习建议

- 想亲手验证软转换器的逐位/逐字时序，进入 **u18-l2（仿真、测试台与综合验证）**：用 `Simulation_Clock` 与 `Synthesis_Harness` 给 `Parallel_Serial`/`Pipeline_Serial_Parallel` 配上 ready/valid 测试台，喂一个已知字、观察 `serial_out` 的 MSB-first 序列与 `parallel_out` 的拼装过程；也可仿照 `tests/` 用 cocotb 写自检。
- 想深入理解两个软转换器所依赖的移位骨架，回看 **u6-l2（寄存器流水线）**：本讲所有「`pipe_in` 喂 LSB、`pipe_out` 读 MSB、Load overrides shift」的几何都建立在那里。
- 想理解「为什么这些 `(* *)` 属性必须写在源码里」，回看 **u4-l2（Core/Instance/Adapter/Shim 与约束）**：`IODELAY_GROUP` 与 `ASYNC_REG`/`PRESERVE` 同属「随声明走、不可外置」的源码内约束一族。
- 若要在真实 Series-7 板上跑 `Deserializer_Differential_1toN`，需对照 **AMD/Xilinx《7 Series FPGAs SelectIO Resources User Guide (UG471)》**（源码多次引用）确认 `DATA_WIDTH` 合法取值（2–8, 10, 14）、`REFCLK_FREQUENCY` 允许频段（190–210 / 290–310 MHz）与 IDELAY 抽头的延迟刻度，并编写外部位/字对齐训练模块——这部分超出本书（厂商原语）范围，是 Adapter 层的工作。
