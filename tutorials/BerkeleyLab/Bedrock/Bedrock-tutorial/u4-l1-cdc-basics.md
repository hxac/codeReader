# 时钟域跨越（CDC）基础

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚什么是「时钟域」、跨时钟域为什么会产生**亚稳态**，以及为什么「两级同步器」是处理单比特跨域的标准手段。
- 读懂 Bedrock 里的三个 CDC 积木：`reg_tech_cdc`（工艺相关的同步器原子）、`flag_xdomain`（单比特标志跨域）、`data_xdomain`（多位数据跨域），并理解它们是如何一层层组合起来的。
- 理解 `async_to_sync_reset_shift` 这类**复位同步器**「异步复位、同步释放」的原理。
- 结合 `localbus/README.md`，解释为什么把 localbus 的**写侧**整体搬到另一个时钟域很简单，而**读侧**很难——并由此理解下一讲（u4-l2 / u4-l3）要讲的存储网关与 jit_rad 为什么存在。

本讲是第 4 单元「时钟域跨越、片上互联与以太网」的第一讲，承接 u2-l2 的 localbus，为 u4-l2/u4-l3 的片上互联网关打基础。

## 2. 前置知识

在进入源码前，先用最直白的话把几个概念讲清楚。

### 2.1 时钟域（clock domain）

一个 FPGA 内部经常有多个不同频率/相位的时钟：比如 125 MHz 的以太网时钟、250 MHz 的 DSP 处理时钟、几十 MHz 的慢速控制时钟。所有由**同一个时钟沿**驱动的触发器（flip-flop），属于同一个**时钟域**。

当一个时钟域里的信号要送给另一个时钟域的触发器去采样时，就发生了**时钟域跨越（Clock Domain Crossing，CDC）**。

### 2.2 亚稳态（metastability）与两级同步器

触发器采样时，要求输入数据在时钟沿前后有一段**稳定时间**（建立时间 setup / 保持时间 hold）。如果输入恰好在时钟沿附近发生变化（这正是两个异步时钟域里必然会出现的情况），触发器输出就会停留在 0 和 1 之间的一个**非法电平**上一段时间，然后随机地「跌落」到 0 或 1。这种现象叫**亚稳态**。

问题不在于跌落到 0 还是 1，而在于：

1. 这个非法电平可能持续**很久**（远超一个时钟周期）；
2. 如果它被下一级电路当作输入，下游不同分支可能对「同一个信号」分别解释成 0 和 1，造成**多位数据撕裂**或**控制逻辑跑飞**。

经典解法是**两级（或多级）同步器**：把跨域信号串着打两拍。第一拍以较高概率「吸收」亚稳态，经过一整个时钟周期的恢复时间后，第二拍采到稳定值的概率极大提高。两级同步器不能消除亚稳态，但能把其平均无故障时间（MTBF）推到几千年以上。一个常引用的近似关系：

\[
\text{MTBF} \;\approx\; \frac{\exp(T_{\text{res}}/\tau)}{f_{\text{clk1}}\, f_{\text{clk2}}\, T_0}
\]

其中 \(T_{\text{res}}\) 是留给亚稳态恢复的时间、\(\tau\) 与 \(T_0\) 是工艺相关常数、\(f_{\text{clk1}}, f_{\text{clk2}}\) 是两域的时钟频率。串的级数越多、时钟越慢，\(T_{\text{res}}\) 越大，MTBF 指数级上升。

### 2.3 单比特 vs 多位：为什么不能直接「每位都打两拍」

- **单比特控制信号**（如一个「事件发生」脉冲）：直接进同步器即可。常用的是 **toggle + 边沿检测** 手法（见 `flag_xdomain`）。
- **多位数据总线**：**绝不能**简单地对每一位分别打两拍！因为各比特的同步器跌落时刻不同，目的域可能在某一拍采到「一半新值、一半旧值」，数据就撕裂了。正确做法是：在源域先把数据**稳定锁存**好，然后用一个跨域的**控制信号**告诉目的域「现在可以放心采样了」。这就是 `data_xdomain` 的核心思路（见后文）。

### 2.4 localbus 与跨域动机（承接 u2-l2）

localbus 是 Bedrock 贯穿全局的轻量总线（24 位地址、32 位数据、strobe、读写信号），刻意**没有握手、没有等待状态**。它的所有时序在综合时就定死了。当 localbus 主机（通常在 `lb_clk` 域）需要访问另一个时钟域（比如 `clk1x`）里的寄存器时，就需要把总线信号安全地搬过时钟域边界。这正是本讲三个积木最典型的应用场景。

> 提示：如果你对 localbus 的信号集还陌生，建议先回顾 u2-l2。本讲依赖那讲的「写侧/读侧」「strobe/读写」等概念。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `dsp/reg_tech_cdc.v` | 工艺相关的**两级同步器原子**，带 `ASYNC_REG` 与 `magic_cdc` 属性 | CDC 的最小构件，其余模块都靠它 |
| `dsp/flag_xdomain.v` | **单比特标志**跨域：toggle + 同步 + 边沿检测 | 处理「事件脉冲」跨域 |
| `dsp/data_xdomain.v` | **多位数据**跨域：门控锁存 + 跨域 gate + 受限采样 | 处理总线/数据跨域，localbus 跨域靠它 |
| `dsp/async_to_sync_reset_shift.v` | **复位同步器**：异步复位、同步释放 | 复位跨域 |
| `dsp/data_xdomain_tb.v` | `data_xdomain` 的自校验测试台（往返跨域） | 代码实践依据 |
| `localbus/README.md` | 解释为什么 localbus **写侧**跨域容易、读侧难 | 应用场景与综合实践依据 |

> 注：`reg_tech_cdc` 是原子，`flag_xdomain` 内部例化它，`data_xdomain` 内部又例化 `flag_xdomain`。三者是层层嵌套关系，理解顺序建议**自底向上**：先 `reg_tech_cdc` → 再 `flag_xdomain` → 再 `data_xdomain`。

## 4. 核心概念与源码讲解

### 4.1 reg_tech_cdc：工艺相关的 CDC 同步器原子

#### 4.1.1 概念说明

`reg_tech_cdc` 是 Bedrock 里**最小的**、可复用的同步器单元。它做且只做一件事：把一个输入比特 `I` 用时钟 `C` **打两拍**后输出 `O`，并打上工艺相关的综合属性。

它的名字透露了设计意图：

- `reg` = 用寄存器（触发器）实现；
- `tech` = technology，工艺/厂家相关；
- `cdc` = clock domain crossing。

「工艺相关」体现在两处综合属性上：`ASYNC_REG` 告诉 Xilinx Vivado「这两个寄存器是异步跨域链上的，别给它们塞进普通时序约束去优化」，`magic_cdc` 是 Bedrock 自定义属性，供本单元第 6 单元会讲到的形式化 CDC 检查工具 `cdc_snitch`（u6-l1）识别「这里是**有意为之**的跨域点」。换句话说，这个模块同时是**功能构件**和**给工具看的标记**。

> 源码注释里有句很诚实的话：「我不会为这个模块写测试台——我假设如果它坏了，所有用它的模块都会一起坏。」这正说明它是一个被无限信任的、无处不在的底层原子。

#### 4.1.2 核心流程

```
输入 I (异步)
   │
   ▼
 [r1]  ← posedge C 采样（第一拍：吸收亚稳态，ASYNC_REG=TRUE）
   │
   ▼
 [r2]  ← posedge C 采样（第二拍：大概率已稳定）
   │
   ▼
 输出 O
```

- `POST_STAGES` 参数（默认 1）控制输出取哪一拍：`=1` 取 `r2`（两级同步器，标准用法）；`=0` 取 `r1`（只用一级，留给特殊场合，见 4.3）。
- 两个寄存器都被标注 `ASYNC_REG = "TRUE"`，保证综合器把它们放在同一个 slice 里、禁止普通时序优化破坏这条跨域链。

#### 4.1.3 源码精读

模块声明与参数（[dsp/reg_tech_cdc.v:6-11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_tech_cdc.v#L6-L11)）：

- `POST_STAGES=1` 是默认两级同步器。

带属性的两级寄存器（[dsp/reg_tech_cdc.v:18-23](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_tech_cdc.v#L18-L23)）：

```verilog
(* ASYNC_REG = "TRUE" *) (* magic_cdc *) reg r1=0;
(* ASYNC_REG = "TRUE" *)            reg r2=0;
always @(posedge C) begin
    r1 <= I;
    r2 <= r1;
end
assign O = (POST_STAGES==0) ? r1 : r2;
```

这段说明：`r1` 既带 `ASYNC_REG` 又带 `magic_cdc`，是跨域链的「入口拍」，也是 cdc_snitch 要追踪的锚点。`r2` 只带 `ASYNC_REG`，是「恢复拍」。输出由 `POST_STAGES` 选择。

#### 4.1.4 代码实践

这是一个**源码阅读型**实践（该模块本身没有独立测试台）：

1. **目标**：确认「谁在用 `reg_tech_cdc`、分别用了几级」。
2. **步骤**：在仓库根目录运行 `grep -rn "reg_tech_cdc" dsp/ | head -40`，统计它被直接例化的位置；重点看本讲的 `flag_xdomain.v` 和 `data_xdomain.v` 两处。
3. **观察**：`flag_xdomain` 用默认 `POST_STAGES=1`（两级），而 `data_xdomain` 对数据位用了 `POST_STAGES=0`（一级）。先记住这个差异，4.3 会解释原因。
4. **预期结果**：你会看到除了本讲三个模块，`biquad` 等需要把系数从慢域搬到快域的地方也会用到它——凡是「单比特或已被门控稳定的多位信号」跨域，都走这个原子。

#### 4.1.5 小练习与答案

- **Q1**：为什么 `r1` 和 `r2` 都要加 `ASYNC_REG`？
  **答**：`ASYNC_REG` 告诉综合器这两个寄存器构成异步采样链，应放在物理相邻的同一个 slice 内、不被普通 setup/hold 约束当普通数据路径优化，从而保证「第一拍吸收亚稳态、第二拍恢复」的时序意图不被破坏。
- **Q2**：把 `POST_STAGES` 从 1 改成 0，对单比特跨域的 MTBF 有什么影响？
  **答**：`POST_STAGES=0` 只剩 `r1` 一级，没有恢复拍，亚稳态直接传给下游，MTBF 会急剧下降（指数级）。所以单比特控制信号跨域**应保持默认的两级**；`POST_STAGES=0` 只在输入已被保证稳定时（如 `data_xdomain` 的数据位）才用。

---

### 4.2 flag_xdomain：单比特标志跨域

#### 4.2.1 概念说明

很多场合需要把一个**短脉冲**（「事件发生了一次」）从一个时钟域送到另一个时钟域。难点在于：如果直接把这个脉冲送进同步器，目的域可能恰好没在那个脉冲的高电平期间采样到，脉冲就「漏掉」了。

`flag_xdomain` 用经典的 **toggle + 边沿检测** 手法解决：源域不传「电平」，而传「翻转」（每次事件把一个 toggle 位取反）；toggle 的翻转天然是格雷码式（每次只变 1 位），跨域安全；目的域同步后，用「本拍 XOR 上拍」检测出翻转，从而还原出一个目的域时钟宽度的脉冲。

`data_xdomain` 里负责「跨域 gate」的那个子模块 `foo`，正是它。

#### 4.2.2 核心流程

```
clk1 域：flagin_clk1 ──(每个高电平)──▶ flagtoggle_clk1 取反   // Step1
                                              │
                                  reg_tech_cdc(两级)          // Step2：跨域
                                              ▼
clk2 域：sync1_clk2 ──打一拍──▶ sync2_clk2
         flagout_clk2 = sync1_clk2 XOR sync2_clk2              // Step3：边沿检测
```

注意约束：源域的 `flagin_clk1` **不能比目的域采样速率太快**地持续拉高。理想情况是每次事件是一个「单拍脉冲」；如果连着两拍都高，第二次就被吞掉了。源码特意在仿真里加了对这种滥用的告警（Step4）。

#### 4.2.3 源码精读

Step 1 —— toggle 翻转（[dsp/flag_xdomain.v:10-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v#L10-L12)）：

```verilog
reg flagtoggle_clk1=0;
always @(posedge clk1) if (flagin_clk1)
    flagtoggle_clk1 <= ~flagtoggle_clk1;
```

源域每次收到 `flagin_clk1`，就把 toggle 位翻转一次。这一步把「事件计数」编码成「奇偶翻转」，跨域时不丢事件。

Step 2 —— 跨域（[dsp/flag_xdomain.v:14-16](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v#L14-L16)）：

```verilog
wire sync1_clk2;
reg_tech_cdc flagtoggle_cdc(.I(flagtoggle_clk1), .C(clk2), .O(sync1_clk2));
```

用 4.1 的原子（默认两级）把 toggle 同步到 `clk2`。这里用两级是必须的——因为 toggle 的翻转沿相对 `clk2` 是异步的。

Step 3 —— 边沿检测（[dsp/flag_xdomain.v:19-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v#L19-L21)）：

```verilog
reg sync2_clk2=0;
always @(posedge clk2) sync2_clk2 <= sync1_clk2;
assign flagout_clk2 = sync2_clk2 ^ sync1_clk2;
```

`sync2_clk2` 是 `sync1_clk2` 的上一拍。两者 XOR：当 `sync1_clk2` 在本拍发生了翻转时，XOR 为 1，于是 `flagout_clk2` 在目的域产生一个时钟宽度的脉冲——这就是「事件搬过来了」。

Step 4 —— 仿真期的滥用告警（[dsp/flag_xdomain.v:23-36](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v#L23-L36)）：注释里点名批评「太多人把 `data_xdomain` 的 `.gate_in(1'b1)` 直接恒接 1 而不理解原理」。若 `flagin_clk1` 连续两拍都高，仿真会打印 `XXXX Warning: flag_xdomain module abuse ...`。这提醒你：本模块假设输入是**稀疏的事件/脉冲**，不是连续电平。

#### 4.2.4 代码实践

**源码阅读 + 推理型**实践：

1. **目标**：亲手验证「toggle + XOR」确实把脉冲搬到了另一时钟域，且不依赖两边频率成整数比。
2. **步骤**：打开 `data_xdomain_tb.v`（4.3 会精读），看它的 `clk1` 周期是 `8ns`、`clk2` 周期是 `9.44ns`——两者**互为异步且无整数倍关系**。这正是 `flag_xdomain` 的用武之地。
3. **观察**：在测试台里，`clk1` 域以 `cnt[2:0]==1`（每 8 拍 1 拍高）产生 `gate_in1`；它经过 `one2two` 实例内部的 `flag_xdomain`（实例名 `foo`）变成 `clk2` 域的 `gate_out1`。
4. **预期结果**：尽管两时钟异步，`gate_out1` 仍能稳定地、不丢事件地在 `clk2` 域还原出脉冲。
5. **待本地验证**：以上行为需用 `make -C dsp data_xdomain_check` 实跑确认（见 4.3 的实践）。

#### 4.2.5 小练习与答案

- **Q1**：为什么用「toggle」而不是直接传 `flagin_clk1` 的电平？
  **答**：因为脉冲可能窄到目的域采样不到。toggle 是**电平翻转**（持续到下一次事件），目的域一定能采到它的状态变化，再靠 XOR 把「变化」还原成「脉冲」，从而不漏事件。
- **Q2**：如果源域 `flagin_clk1` 连续两拍都为 1，会发生什么？
  **答**：toggle 只翻转一次，第二个脉冲被吞掉（两事件合并成一事件）。这正是源码 Step4 仿真告警要抓的滥用场景。正确用法是保证事件之间有足够间隔（通常一次事件一个单拍脉冲）。

---

### 4.3 data_xdomain：多位数据跨域

#### 4.3.1 概念说明

`data_xdomain` 处理本讲最重要的场景：把**一束多位数据**（如 localbus 的地址+数据，49 位）从一个时钟域搬到另一个时钟域。

回顾 2.3：多位数据**不能**逐位打两拍。`data_xdomain` 的办法是：

1. 源域用一个 `gate_in` 信号表示「当前 `data_in` 是稳定可用的、且这是一次新写入」；
2. 源域在 `gate_in` 有效时把 `data_in` **锁存**进 `data_latch`，并保持住（直到下一次 `gate_in`）；
3. 把 `gate_in` 当作单比特事件，用 `flag_xdomain`（4.2）安全跨到目的域，得到 `gate_x`；
4. 目的域在 `gate_x` 有效时，放心地采样那束**早已稳定**的数据。

关键洞见：数据之所以安全，不是因为数据位各自做了多强的同步，而是因为**控制它采样的 gate 已经被正确同步过了**，而此时数据早已稳定。源码第一行注释点出的约束——「`clk_out` 必须比 `gate_in` 的速率快两倍以上」——就是为了保证：在 `gate_in` 的下一次翻转到来之前，目的域有足够时间完成同步并采样稳定数据。

#### 4.3.2 核心流程

```
clk_in 域：
  data_in ──(gate_in 有效时)──▶ data_latch (锁存，稳定保持)
  gate_in ──▶ flag_xdomain ────────────────────────┐
                                                    │ 跨域
clk_out 域：                                        ▼
  data_latch ──(逐位 reg_tech_cdc, POST_STAGES=0)──▶ data_pipe
  gate_x ──(有效时)──▶ 把 data_pipe 锁进 data_out_r
  gate_out_r <= gate_x   (输出 gate_out 延迟一拍，与 data 对齐)
```

注意一个微妙之处：数据位的 `reg_tech_cdc` 用了 `POST_STAGES=0`（**单级**），而 gate 用的 `flag_xdomain` 内部是**两级**。这是因为：

- **gate** 的边沿相对 `clk_out` 是真异步的，必须两级同步器吸收亚稳态；
- **数据**在 `gate_x` 变有效时**早已稳定**（被源域锁存住并保持），单级采样即可，多一级反而白白增加延迟、降低吞吐（源码注释明说：直接用 `data_latch`「Vivado 能接受，但仿真显示会明显降低吞吐」）。

换句话说：**亚稳态防护集中在 gate 上，数据靠 gate 的「资格认证」得到安全**——这是本模块最值得记住的设计取舍。

#### 4.3.3 源码精读

模块端口与约束注释（[dsp/data_xdomain.v:1-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v#L1-L12)）：注意第一行那条「`clk_out` 必须比 `gate_in` 速率快两倍以上」的全局前提。端口里 `size`（默认 16）参数化数据宽度。

源域锁存（[dsp/data_xdomain.v:14-15](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v#L14-L15)）：

```verilog
reg [size-1:0] data_latch=0;
always @(posedge clk_in) if (gate_in) data_latch <= data_in;
```

`gate_in` 有效时把数据抓进 `data_latch` 并**保持**——这就是「让数据稳定」的那一步。

gate 跨域（[dsp/data_xdomain.v:17-20](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v#L17-L20)）：直接例化 4.2 的 `flag_xdomain`（实例名 `foo`），把 `gate_in` 安全搬到 `clk_out` 域得到 `gate_x`。

数据跨域的两套实现（[dsp/data_xdomain.v:22-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v#L22-L32)）：

```verilog
`ifdef HAPPY_VIVADO
assign data_pipe = data_latch;                 // Vivado 接受直连
`else
reg_tech_cdc #(.POST_STAGES(0)) rtc[size-1:0] (.C(clk_out),
    .I(data_latch), .O(data_pipe));            // 逐位单级采样
`endif
```

这里用一个**数组实例** `rtc[size-1:0]` 给 `data_latch` 的每一位各生成一个 `reg_tech_cdc`，且 `POST_STAGES=0`（只一级 `r1`）。注释说：Vivado 会把这种「直接用 `data_latch`」的写法报成 `CDC-4 Critical`（见 UG906），所以才在仿真路径上用 `reg_tech_cdc` 显式表达跨域意图。

受限采样（[dsp/data_xdomain.v:34-46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v#L34-L46)）：

```verilog
reg [size-1:0] data_out_r=0;
reg gate_out_r=0;
always @(posedge clk_out) begin
    if (gate_x) data_out_r <= data_pipe;   // gate 合格时才采数据
    gate_out_r <= gate_x;                   // gate_out 与 data 同拍对齐
end
assign data_out = data_out_r;
assign gate_out = gate_out_r;
```

`gate_out_r` 比 `gate_x` 晚一拍，正好与 `data_out_r` 对齐，告诉下游「现在 `data_out` 是一份有效的新数据」。

#### 4.3.4 代码实践

这是本讲的核心**可运行实践**。

1. **目标**：亲手跑通 `data_xdomain` 的自校验测试台，理解它的「往返跨域」校验思路。
2. **操作步骤**：在仓库根目录执行
   ```bash
   make -C dsp data_xdomain_check
   ```
   （该测试台在 `dsp/rules.mk` 的 `TEST_BENCH` 列表里，且不在 `NO_CHECK` 中，对应 `%_check` 模式规则会自动编译并仿真。）
3. **观察现象**：仿真会在若干 `clk1` 周期后打印一行 `After <N> clk1 cycles, # of errors: <k>`，并最终输出 `PASS`。
4. **预期结果**：`# of errors` 应为 `0`，结尾打印 `PASS`。
5. **理解校验逻辑**：测试台（[dsp/data_xdomain_tb.v:44-53](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain_tb.v#L44-L53)）把 `data_xdomain` 串了两次——`one2two`（clk1→clk2）再 `two2one`（clk2→clk1）——形成一个**往返**。校验逻辑（[dsp/data_xdomain_tb.v:64-83](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain_tb.v#L64-L83)）在 `clk1` 域把每次 `gate_in1` 时的 `data_in` 存进数组，等数据往返回来经 `gate_out2` 到达，逐一比对是否相等；不等则 `fail`。由于 `clk1`(8ns) 与 `clk2`(9.44ns) 互为异步，能 PASS 说明跨域**没有撕裂数据**。
6. **可选**：用 `make -C dsp data_xdomain_tb VFLAGS_data_xdomain_tb=-DXXX` 验证 u2-l1 讲过的「按目标定制参数」钩子是否生效（这里仅作演示，加一个未使用的宏不应改变行为）。
7. **看波形**：`data_xdomain.gtkw` 预设了信号树，包含 `one2two.clk_in/clk_out/data_in/data_out/gate_in/gate_out` 以及内部的 `foo.flagtoggle_clk1`、`foo.sync1_clk2`，可用 `make -C dsp data_xdomain.vcd && gtkwave data_xdomain.vcd &` 观察 toggle 跨域全过程。
8. **若工具缺失**：如果没装 iverilog/vvp，则无法实跑，需标注「待本地验证」。

#### 4.3.5 小练习与答案

- **Q1**：为什么数据位用 `POST_STAGES=0`（一级），而 gate 用两级？
  **答**：gate 的翻转沿相对 `clk_out` 是真异步的，需要两级同步器吸收亚稳态；而数据在被 gate 认证采样时**早已被源域锁存并稳定保持**，单级采样已足够，多一级只会徒增延迟、降低吞吐（源码注释有据）。
- **Q2**：若把 `gate_in` 恒接 `1'b1`（源码 Step4 点名的滥用），系统会怎样？
  **答**：源域每个 `clk_in` 沿都翻转 toggle，远超 `clk_out` 能分辨的速率，`flag_xdomain` 会大量丢事件、`data_latch` 也每拍都在变，目的域采到的数据与「源域某一拍」根本对不上号。仿真期会打印 `flag_xdomain module abuse` 警告。
- **Q3**：`data_latch` 是电平敏感的「保持」寄存器，为什么它跨域是安全的？
  **答**：因为它的内容只在 `gate_in` 有效时才改变，且改变后保持到下一次 `gate_in`；由于 `clk_out` 比 `gate_in` 速率快两倍以上，在 `gate_x` 认证采样的那个时刻，`data_latch` 一定处于稳定值。安全性来自「稳定窗口足够长 + gate 做资格认证」，而不是来自数据线本身的同步强度。

---

### 4.4 async_to_sync_reset_shift：复位同步器

#### 4.4.1 概念说明

前三个模块处理「数据/控制信号」跨域。还有一类常见的跨域是**复位信号**。

好的复位设计要求**异步复位、同步释放**：

- **异步复位**：复位信号一来，立刻（不等时钟沿）把所有触发器置成复位态，确保整个芯片在同一瞬间进入已知状态；
- **同步释放**：复位信号撤销时，要让「撤销」动作对齐到时钟沿，否则同一个复位释放沿在不同触发器上到达时间不同，部分触发器先退出复位、部分还在复位，状态机就跑飞了。

`async_to_sync_reset_shift` 就是干这个的：它把一个异步输入复位，转换成一个「异步assert、同步deassert、且保证最小脉宽」的复位输出。

#### 4.4.2 核心流程

```
Pinput(异步) ── 有效 ──▶ shift 全部置成 OUTPUT_POLARITY ──▶ Poutput 立即assert
                 │
                 └── 无效后，每个 clk 沿把 shift 移位、最低位补反相 ──▶ 经过 LENGTH 拍后 Poutput 才deassert(同步)
```

- 输入有效时（`Pinput == INPUT_POLARITY`），整条 `shift` 寄存器**异步**并行置位 → 输出 `Poutput = OUTPUT_POLARITY`（复位有效）立即生效；
- 输入无效后，每个时钟沿移位、低位补反相值，要等 `LENGTH` 拍之后复位才「同步地」释放；
- 顺带把复位**最小脉宽**撑到 `LENGTH` 个时钟周期，避免输入毛刺造成亚微秒复位。

#### 4.4.3 源码精读

参数与移位逻辑（[dsp/async_to_sync_reset_shift.v:1-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/async_to_sync_reset_shift.v#L1-L18)）：

```verilog
parameter LENGTH =8;
parameter INPUT_POLARITY = 1'b1;
parameter OUTPUT_POLARITY= 1'b1;
reg [LENGTH-1:0] shift=0;
always @(Pinput or (clk)) begin
    if ( Pinput == INPUT_POLARITY ) begin
        shift <= {LENGTH{OUTPUT_POLARITY}};          // 异步置位
    end else if (clk) begin
        shift <= {shift[LENGTH-2:0], ~OUTPUT_POLARITY}; // 同步移位释放
    end
end
assign Poutput=shift[LENGTH-1];
```

- 灵敏表 `always @(Pinput or (clk))` + `if (Pinput==...)` 实现**异步 assert**（输入一变就生效，不等时钟）；
- `else if (clk)` 配合移位实现**同步 deassert**（释放对齐到时钟沿，并延迟 `LENGTH` 拍）；
- `INPUT_POLARITY` / `OUTPUT_POLARITY` 让输入/输出都支持高有效或低有效，适配不同板子上复位按键的极性。

> 提示：Bedrock 多数 CDC 用 `data_xdomain/flag_xdomain`；复位跨域则常用这类「移位同步器」。它和 `reg_tech_cdc` 是两类不同用途的同步结构。

#### 4.4.4 代码实践

**源码阅读型**实践：

1. **目标**：理解参数对复位脉宽的影响。
2. **步骤**：阅读上面这段代码，回答：若 `LENGTH=8`、`INPUT_POLARITY=1`、`OUTPUT_POLARITY=1`，一次持续 1 拍的 `Pinput` 高电平，`Poutput` 会维持高（复位有效）至少多少个 `clk` 周期？
3. **观察/推理**：`Pinput` 高时 `shift` 全置 1（输出 1）；`Pinput` 撤销后，每个 `clk` 沿 `shift` 右移并低位补 0，要 8 拍后 `shift[7]` 才变 0。所以复位有效至少持续约 8 个 `clk` 周期。
4. **预期结果**：脉宽被撑到约 `LENGTH` 个时钟周期，远长于输入毛刺，且释放边沿同步于 `clk`。
5. **待本地验证**：本模块无独立测试台，可自行写一个最小 testbench 用 iverilog 验证上述推理。

#### 4.4.5 小练习与答案

- **Q1**：为什么复位要「同步释放」？
  **答**：若释放沿异步到达，不同触发器退出复位的时刻可能差一个时钟周期，导致状态机各寄存器不同步地「启动」，出现非法状态。同步释放保证所有触发器在同一个时钟沿一起退出复位。
- **Q2**：`LENGTH` 设得过大或过小各有什么影响？
  **答**：过小 → 复位最小脉宽不足，可能滤不掉输入毛刺、且给亚稳态恢复时间短；过大 → 复位释放延迟变长，上电/重配后电路「醒」得更慢。典型取 3~8。

---

## 5. 综合实践：把 localbus 写侧整体跨域

本实践把 4.1~4.3 串起来，对应本讲规格里指定的综合任务。

### 5.1 背景：为什么写侧容易、读侧难

`localbus/README.md` 在 [localbus/README.md:28-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/README.md#L28-L42) 有一段关键论述：把 localbus 的**写侧**搬到另一个时钟域很容易，吞吐不受影响、多出来的延迟和抖动相比软件侧的延迟可忽略；但它没说读侧也能这么搬——因为读侧本质上做不到。原因在于：

- **写侧**是单向「主机 → 总线」：主机在 `lb_clk` 域给出 `{lb_addr, lb_data}` 并拉 `lb_write`，目的域只要在 `lb_write` 有效时把这束稳定的地址+数据收下、写进自己的寄存器即可。这是一次**没有回程**的跨域，正是 `data_xdomain` 的天然场景：`gate_in = lb_write`，`data_in = {lb_addr, lb_data}`。延迟一两个周期无关紧要。
- **读侧**是**往返**「主机发地址 → 目的域返回数据 → 主机还要知道数据何时有效」。这要求一次**请求-响应握手**，而 localbus 刻意没有握手、没有等待状态。读数据从另一时钟域回来时，主机域无法判断它何时有效——于是不能简单跨域。

> 这正是后续 u4-l2（存储网关 `mem_gateway`，用**固定延迟**绕开握手）和 u4-l3（`jit_rad`，用 UDP 包到来前的预警把数据**预快照**进 dpram 再让 localbus 安全读）要解决的问题。本讲理解了「写侧易、读侧难」，就理解了那两讲存在的根本动机。

### 5.2 真实代码：cmoc/cryomodule.v 的 lb_to_1x

README 给出的写侧跨域例子并非杜撰，`cmoc/cryomodule.v` 里就有几乎逐字一致的实例。`lb_to_1x`（[cmoc/cryomodule.v:116-118](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L116-L118)）和 `lb_to_2x`（[cmoc/cryomodule.v:112-114](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L112-L114)）各把一组 localbus 写信号从 `lb_clk` 搬到 `clk1x` / `clk2x`：

```verilog
data_xdomain #(.size(32+17)) lb_to_1x(
    .clk_in(lb_clk), .gate_in(lb_write), .data_in({lb_addr,lb_data}),
    .clk_out(clk1x), .gate_out(clk1x_write), .data_out({clk1x_addr,clk1x_data}));
```

对照 4.3：

- `size = 32+17 = 49` 位 = 17 位地址 + 32 位写数据；
- `gate_in = lb_write`：每个 localbus 写周期就是一个跨域事件；
- `data_in = {lb_addr, lb_data}`：把地址和数据**拼成一束**一起搬，避免地址和数据分别跨域产生对不齐；
- 输出端解拼成 `{clk1x_addr, clk1x_data}` 加 `clk1x_write`，在 `clk1x` 域还原出一次完整的写操作。

### 5.3 你的任务

1. **画连接图**：在纸上画出「把一组 localbus 写信号从 `lb_clk` 搬到 `clk1x`」的方框图，包含：`lb_clk` 域的 `lb_addr/lb_data/lb_write` → `data_xdomain` 模块（标出内部 `data_latch`、`flag_xdomain foo`、逐位 `reg_tech_cdc`、`gate_x` 采样）→ `clk1x` 域的 `clk1x_addr/clk1x_data/clk1x_write`。
2. **标注速率约束**：在图边写上「`clk1x` 必须比 `lb_write` 的速率快两倍以上」这条前提，并解释为什么 localbus 写速率通常远低于 `clk1x`，所以这条几乎总是满足。
3. **解释写侧易/读侧难**：用自己的话写 3~5 句，说明为何同样的 `data_xdomain` 能搞定写侧，却搞不定读侧（提示：读侧需要回程数据 + 有效性指示，缺握手）。
4. **跑测试**：完成 4.3.4 的 `make -C dsp data_xdomain_check`，确认 `PASS`，作为你这张连接图「真能工作」的证据。
5. **预期结果**：一张清晰的跨域连接图 + 一段对「写易读难」的正确解释 + `data_xdomain_check` 的 `PASS` 输出。
6. **待本地验证**：若缺工具无法实跑，至少完成画图与解释部分，并把 `data_xdomain_check` 标注为「待本地验证」。

## 6. 本讲小结

- **CDC 的本质**是处理异步时钟域之间信号传递的**亚稳态**问题；单比特用同步器，多位数据要靠「稳定锁存 + 跨域 gate 资格认证」。
- **`reg_tech_cdc`** 是 Bedrock 的同步器**原子**：带 `ASYNC_REG` 与 `magic_cdc` 属性的两级（可配一级）寄存器，是其余 CDC 模块和 `cdc_snitch`（u6-l1）检查的共同锚点。
- **`flag_xdomain`** 用 **toggle + 两级同步 + XOR 边沿检测**把单比特事件脉冲安全搬到另一时钟域，假设输入是稀疏脉冲（连发会被吞，仿真会告警）。
- **`data_xdomain`** 把多位数据跨域：源域在 `gate_in` 有效时锁存数据并保持，用 `flag_xdomain` 把 gate 跨过去，目的域在认证后的 gate 有效时采样早已稳定的数据；数据位只取一级同步，亚稳态防护集中在 gate 上。
- **`async_to_sync_reset_shift`** 是复位同步器：**异步复位、同步释放**，并把复位最小脉宽撑到 `LENGTH` 拍。
- **localbus 写侧跨域**用 `data_xdomain` 一行就能搞定（`cmoc/cryomodule.v` 的 `lb_to_1x` 即实例）；**读侧因缺乏握手而困难**，这正是 u4-l2（固定延迟网关）与 u4-l3（jit_rad 预快照）要解决的核心问题。

## 7. 下一步学习建议

- **紧接着学 u4-l2（存储网关 `mem_gateway`）**：看它如何用**综合时确定的固定延迟**让 localbus 读在不引入握手的前提下也能跨域/被网络访问，这是对「读侧难」的第一个工程回答。
- **再学 u4-l3（jit_rad）**：看它如何用 UDP 包到来前的预警时间，把另一时钟域的数据**原子快照**进 dpram 供 localbus 安全读回，并理解为何 `passthrough=1` 会被 u6-l1 的 `cdc_snitch` 判违规。
- **回头深读 `reg_tech_cdc` 的 `magic_cdc` 属性**：为 u6-l1 的形式化 CDC 验证做准备——那时你会看到本讲这些「有意为之」的跨域点是如何被工具自动识别和放行的。
- **延伸阅读**：`localbus/README.md`、`cmoc/cryomodule.v`、以及 `dsp/data_xdomain_tb.v` 这条「往返自校验」测试台，是理解本讲最值得反复看的三个文件。
