# 开方与流水线时序

## 1. 本讲目标

本讲承接 [u3-l2](u3-l2-magnitude-square-and-sum.md)。上一讲我们把 FFT 的复数输出算成了「幅度平方」`re²+im²`，并存进了 ram2。本讲要把这个平方值**开根号**，还原成真正的幅度 `|X| = √(re²+im²)`，再写进 ram3，供后续 UART 上传给 PC。

学完本讲你应当掌握：

1. `Root_square`（文件 `Radical.v`）如何作为一层薄壳封装 Xilinx CORDIC 开方 IP，以及它的端口与位宽（20 位输入 → 11 位输出）。
2. 为什么开方不是「零延迟」——CORDIC 是一条 **8 拍流水线**，`sclr`（同步复位）与 `rdy`（输出有效）两个握手信号如何配合这条流水线。
3. TOP.v 里 `square_state` ~ `square_state5` 这五个状态如何把「等流水线出结果 → 写 ram3 → 复位核 → 换下一个地址」编排成一个逐点处理的循环，以及 `sqr_rdy` 在哪一拍被拉高。

## 2. 前置知识

### 2.1 为什么要开方

回顾幅度公式。对 FFT 的某一个频点，输出是一个复数 \(X = \text{re} + j\,\text{im} \)，它的幅度（模长）是：

\[
|X| = \sqrt{\text{re}^2 + \text{im}^2}
\]

上一讲我们已经用 `Square`（乘法器）和 `Sum`（加法器）算出了根号里的 `re²+im²`，存进 ram2。本讲只差最后一步：**开根号**。开方之后得到的才是真正物理意义上的「频谱幅度」，可以直接画出来给人看。

> 为什么不直接发 `re²+im²` 给 PC？因为平方后的数值在视觉上会被「抬亮」——大信号显得更大、小信号被压得更小，谱形失真。开方能让幅度谱更接近人耳/人眼对能量的线性感知。

### 2.2 什么是流水线延迟

组合逻辑（上一讲的平方、加法）是「给输入立刻出结果」，延迟只是几级逻辑门的翻转时间。但**开方**这种复杂运算，如果做成组合逻辑，关键路径会非常长，跑不到 200 MHz。所以 FPGA 厂商把它做成**流水线（pipeline）**：把运算拆成很多小级，每级在一个时钟周期内干一点活，数据像流水一样一级一级往下流。

代价是**延迟（latency）**：今天送进去的输入，要等好几个周期后才从末端冒出来。TOP.v 头部注释明确写了这个数：

> The aquare root module is not a 0 latency module, so it requires eight clock cycles.
> （开方模块不是零延迟模块，它需要 8 个时钟周期。）

这意味着我们不能像对待加法器那样「输入一变就去读输出」——必须**等够 8 拍**，输出才有效。本讲的核心难点，就是状态机如何耐心地等这 8 拍、并逐点重复这个过程。

### 2.3 三个关键握手信号

- `sclr`（synchronous clear，同步复位）：高电平有效时把 CORDIC 流水线清空/复位。在 TOP 里它是一个 `reg`，由状态机主动驱动。
- `rdy`（ready）：CORDIC 输出信号，**当它为 1 时表示 `x_out` 上是一个有效的开方结果**。在 TOP 里它接到 `sqr_rdy` 这根线。
- `clk`：开方核的时钟，在 TOP 里接的是 200 MHz 的 `clk`（系统主时钟）。

一个朴素的直觉：**「拉低 sclr → 喂输入 → 数 8 拍 → rdy 变 1 → 读输出」**。下面的源码正是围绕这个直觉展开的，只是还多了「处理完一个点之后，怎么干净地切换到下一个点」的工程细节。

## 3. 本讲源码地图

| 文件 | 模块名 | 在本讲中的角色 |
|---|---|---|
| `verilog files/Radical.v` | `Root_square` | 开方核的封装壳，把 Xilinx CORDIC IP `Root` 改个名接出来。本讲的主角。 |
| `verilog files/TOP.v` | `TOP` | 在其中例化 `Root_square`，并用 `square_state`~`square_state5` 五个状态驱动它逐点处理。 |

> 命名陷阱（复习 [u1-l2](u1-l2-repo-structure-and-reproduction.md)）：文件叫 `Radical.v`，里面的模块却叫 `Root_square`，例化名又是 `root_square`。读代码一律认 `module` 关键字后面的名字。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Root_square 封装壳**——它到底封装了什么、端口怎么接。
2. **CORDIC 8 拍流水线与 sclr/rdy 握手**——位宽、延迟、握手语义。
3. **square_state 五状态循环**——TOP 如何把单次开方编排成「逐点处理 1024 个频点」的循环。

---

### 4.1 Root_square：开方 IP 的薄封装

#### 4.1.1 概念说明

Xilinx Vivado 提供一个现成的 CORDIC IP，能做开方、三角函数、双曲函数等。这个 IP 在生成时是一个「黑盒」——你只能通过一组固定端口跟它对话，看不见内部 RTL。`Root_square` 就是给这个黑盒套了一层壳，作用只有一个：**把 IP 的端口名原样转接出来，方便顶层连线**。它自己**不做任何运算**。

#### 4.1.2 核心流程

```
        +-----------------------------------+
x_in -->|  Root_square (= CORDIC 开方 IP)   |--> x_out
clk ---->|  sclr 复位 / rdy 输出有效         |
sclr -->|                                   |--> rdy
        +-----------------------------------+
```

输入是一个 20 位无符号数（`re²+im²` 恒非负），输出是 11 位开方结果。

#### 4.1.3 源码精读

整个模块只有端口声明加一个 IP 例化，非常短：

[verilog files/Radical.v:6-13](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Radical.v#L6-L13) 声明端口：20 位输入 `x_in`、11 位输出 `x_out`、输出 `rdy`、输入 `clk` 与 `sclr`。

```verilog
module Root_square(x_in, x_out, rdy, clk, sclr);
 input [19 : 0] x_in;
 output [10 : 0] x_out;
 output rdy;
 input clk;
 input sclr;
```

[verilog files/Radical.v:16-22](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Radical.v#L16-L22) 例化名叫 `Root` 的 Xilinx IP，端口一一对应、原样接出。

```verilog
Root your_instance_name (
  .x_in(x_in),    // 输入 20 位
  .x_out(x_out),  // 输出 11 位
  .rdy(rdy),
  .clk(clk),
  .sclr(sclr)
);
```

**位宽背后的数学**：上一讲 ram2 里存的是 `re²+im²`，最大约 \(2 \times 512^2 = 524288 = 2^{19}\)，恰好装得进 20 位（\(2^{20}=1048576\)），所以输入取 20 位刚好够用。开方把数值压回原量级：

\[
\sqrt{524288} \approx 724 < 1024 = 2^{10}
\]

结果最大约 724，理论 10 位就够；IP 给 11 位是为了能完整表达 \(\sqrt{2^{20}}=2^{10}=1024\) 这个上界。后面会看到 TOP 只取低 10 位写进 ram3。

#### 4.1.4 代码实践

**实践目标**：确认封装壳「零运算、纯转接」的判断。

**操作步骤**：

1. 打开 `verilog files/Radical.v`，对照 [verilog files/Radical.v:16-22](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Radical.v#L16-L22)。
2. 数一数：模块体内除了端口声明和这一处例化，有没有任何 `assign`、`always`、算术运算符？

**需要观察的现象**：除了例化语句，模块里没有任何逻辑——没有 `assign`、没有 `always`。

**预期结果**：确认 `Root_square` 是纯封装壳，所有真实运算都在 Xilinx IP `Root` 内部（二进制黑盒，源码不可见）。

#### 4.1.5 小练习与答案

**练习 1**：为什么输入是 20 位、输出却是 11 位，不是同为 20 位？
**答案**：开方使数值量级减半。20 位输入的最大值约 \(2^{19}\)，开方后约 \(2^{9.5}\)，10~11 位足够；位宽由数学关系决定，不是任选。

**练习 2**：文件名叫 `Radical.v`，但 TOP 里例化的是 `Root_square`。如果你用文件名去搜模块，会发生什么？
**答案**：会搜不到。必须以文件内部 `module Root_square` 这个声明名为准——这是本仓库反复出现的「文件名≠模块名」陷阱。

---

### 4.2 CORDIC 8 拍流水线与 sclr/rdy 握手

#### 4.2.1 概念说明

CORDIC（Coordinate Rotation DIgital Computer）是一种只用移位和加法做超越运算的经典算法。Xilinx 把它做成了多级流水线 IP。本工程里它有两个我们要关注的特性：

1. **8 拍延迟**：从输入有效到 `rdy` 拉高，中间相隔约 8 个 `clk` 周期。
2. **需要主动复位（sclr）来重新「装填」**：TOP 头部注释直言「`sqr_rdy` 拉高后必须手动复位」（*This has to be manually reseted*）。换句话说，处理完一个点后，状态机要给 CORDIC 打一个 `sclr` 脉冲，把 `rdy` 清掉、把流水线冲干净，才能开始算下一个点。

#### 4.2.2 核心流程

一次开方的握手时序（朴素模型）：

```
sclr:  1(复位) ─┐                （下一次复位由状态机在处理完后打出）
              └─0─────────────────────────
x_in:        ──────[ ram2[n] ]──────────  (输入保持稳定)
clk 周期:      0  1  2  3  4  5  6  7  8
rdy:   0 0 0  0  0  0  0  0  0  1  ← 第 8 拍拉高
x_out:                       ──[ √ram2[n] ]──  (rdy=1 时有效)
```

要点：

- 在 `sclr` 拉低后，输入 `x_in` 必须保持稳定整整 8 拍，否则流水线里会混入错误数据。
- 第 8 拍 `rdy` 拉高、`x_out` 有效——**这一拍就是状态机去「捞结果」的时机**。
- 想算下一个点，必须先打 `sclr` 脉冲复位，否则 `rdy` 会一直挂着、状态机会误判。

> 关于 `rdy` 究竟是「电平」还是「单拍脉冲」：这取决于 CORDIC IP 的内部配置（黑盒，源码不可见，**待确认**）。但 TOP 的处理方式（每点之间打一个 `sclr` 脉冲）对两种情况都成立——这就是下一节五个状态存在的根本原因。

#### 4.2.3 源码精读

先看 TOP 里相关的连线声明。[verilog files/TOP.v:69-73](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L69-L73)：

```verilog
reg sclr;                 // 开方核的复位引脚，由状态机驱动
wire sqr_rdy;             // 开方核输出有效标志（接 IP 的 rdy）
reg [10:0] cnt_s;         // 同时作 ram2 读地址 与 ram3 写地址 的计数器
wire [10:0] square_out;   // 开方核输出（11 位）
```

再看开方核的例化，[verilog files/TOP.v:184-188](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L184-L188)：

```verilog
Root_square root_square(
    .x_in(out_fft[19:0]),   // 输入 = ram2 输出的低 20 位
    .x_out(square_out),     // 输出 = 11 位开方结果
    .rdy(sqr_rdy),          // HIGH when data is outputed
    .clk(clk),              // 200 MHz 系统时钟
    .sclr(sclr));
```

**关键接线 1：输入从哪来。** `out_fft` 是 ram2（例化名 `ram_fft_20bit`）的输出，见 [verilog files/TOP.v:137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142)。ram2 是**组合读**（复习 [u2-l2](u2-l2-sram-storage-and-three-rams.md)：`assign data_out = mem[addr_r]`），读地址是 `cnt_s`。所以：

```
cnt_s（读地址）──► ram2 ──组合读──► out_fft[19:0] ──► Root_square.x_in
```

`cnt_s` 一变，输入立刻跟着变。这正是为什么状态机必须在开方期间**保持 `cnt_s` 不变**整整 8 拍。

**关键接线 2：输出到哪去。** `square_out[9:0]` 写进 ram3（例化名 `ram_fft_10bit`），见 [verilog files/TOP.v:145-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145-L150)。注意三个细节：

- ram3 的**写地址也是 `cnt_s`**——和 ram2 的读地址是同一个计数器。这造就了「读 ram2[n] → 开方 → 写 ram3[n]」的一一对应。
- 写进 ram3 的是 `square_out[9:0]`，**只取低 10 位**，丢掉第 10 位。前面算过最大值约 724 < 1024，低 10 位够用。
- ram3 是**同步写**（`posedge clk` 时 `if(we3) mem[addr] <= data_in`），所以必须在 `we3=1` 且 `square_out` 有效的那一拍，结果才会被锁存。

**关键接线 3：输入截断。** ram2 的 `out_fft` 是 21 位（`wire [20:0] out_fft`，见 [verilog files/TOP.v:64](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L64)），而开方核只接 `out_fft[19:0]`，丢掉最高位（bit 20，值 \(2^{20}\)）。正常幅度下 `re²+im²` 不会超过 \(2^{19}\)，这一位恒为 0，截断不丢信息。

#### 4.2.4 代码实践

**实践目标**：亲手追踪「输入组合读、输出同步写」这条数据通路。

**操作步骤**：

1. 在 [verilog files/TOP.v:137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142) 找到 ram2 例化，确认它的 `.addr_r(cnt_s)` 和 `.data_out(out_fft)`。
2. 在 [verilog files/TOP.v:145-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145-L150) 找到 ram3 例化，确认它的 `.addr(cnt_s)` 和 `.data_in(square_out[9:0])`。
3. 把两段连起来，在纸上画出：`cnt_s → ram2(读) → out_fft → Root_square → square_out → ram3(写,cnt_s)`。

**需要观察的现象**：ram2 的读地址和 ram3 的写地址是**同一个 `cnt_s`**。

**预期结果**：你会得到一个闭环——同一个计数器既驱动「读哪个平方值」，又驱动「开方结果写到哪里」，保证两点之间不串位。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ram2 用组合读、ram3 用同步写，对开方核的时序很关键？
**答案**：组合读意味着 `cnt_s` 一稳定，输入立刻稳定，可保证开方 8 拍内输入不变；同步写意味着只有在 `we3=1` 的那个上升沿、且 `square_out` 有效时，结果才被锁存——这让我们能精确控制「在 rdy 拉高那一拍把结果收进 ram3」。

**练习 2**：如果 `re²+im²` 真的超过了 \(2^{20}\)，会发生什么？
**答案**：最高位会被 `out_fft[19:0]` 截掉，开方核拿到一个被「折半」的输入，结果偏小。正常幅度下不会触发，但这是位宽截断固有的潜在风险。

---

### 4.3 square_state 五状态循环：把流水线编排成逐点处理

#### 4.3.1 概念说明

开方核一次只能算一个点，而 ram2 里存着上千个频点。TOP 的做法是：用一个**五状态小循环**，每循环一遍处理一个点——等开方核出结果（`square_state`）→ 捕获并写 ram3（`square_state2/3`）→ 复位核、地址加一（`square_state4/5`）→ 回到起点算下一个点。

这五个状态是本讲真正的主角。它们存在的全部理由，就是伺候好那条「不能被打扰的 8 拍流水线」。

#### 4.3.2 核心流程

先看进入循环前的准备。开方循环是从 `fft_write_state`（FFT 卸载阶段）结束时切入的，[verilog files/TOP.v:346-354](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L346-L354)：

```verilog
fft_write_state: begin
    start_fft<=1'b0;
    if(index_out==10'b1111111110) begin   // FFT 卸载到倒数第二个点
        we2<=1'b0;        // 关闭 ram2 写
        we3<=1'b1;        // 打开 ram3 写
        state<=square_state;
        sclr<=1'b0;       // 释放开方核复位 → 流水线开始算第 0 点
        cnt_s<=11'b00000000000;  // 从地址 0 开始
    end
end
```

进入 `square_state` 时，初始条件是：`sclr=0`（核已放行）、`we3=1`（ram3 可写）、`cnt_s=0`。

五个状态的参数定义在 [verilog files/TOP.v:220-224](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L220-L224)，注释在 [verilog files/TOP.v:355-356](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L355-L356)：

```
square_state   ── 等 sqr_rdy 拉高（等够 8 拍）；期间 ram3[cnt_s] 持续被写
   │ rdy==1
   ▼
square_state2  ── 过渡拍（让 rdy 那拍的数据稳定落盘）
   ▼
square_state3  ── 打 sclr=1 复位核、we3=0 停止写 ram3
   ▼
square_state4  ── cnt_s + 1（切换到下一个频点）
   ▼
square_state5  ── sclr=0 重新放行、we3=1 重新可写 → 回 square_state
```

每循环一遍处理 ram2 的一个点；当 `cnt_s` 计到 1023 时，循环结束，转去 `send_state` 开始上传。

#### 4.3.3 源码精读

逐状态精读（注意所有赋值都是 `<=` 非阻塞，本拍计算、**下一拍才生效**）。

**square_state** —— [verilog files/TOP.v:357-370](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L370)：

```verilog
square_state: begin
    if(cnt_s<11'b1111111111) begin          // cnt_s < 1023：还有点要处理
        if(sqr_rdy==1'b1) state<=square_state2;   // 等到开方结果有效 → 进入捕获
    end
    else begin                              // cnt_s == 1023：处理完毕
        state<=send_state;
        res_serial<=1'b0;
        sel<=1'b1;        // 把 ram3 读地址切到 cnt（供上传使用）
        we3<=1'b0;
        sclr<=1'b1;       // 复位开方核，进入待机
    end
end
```

它做两件事：① 在 `sqr_rdy` 拉高前原地等待（这段时间正是 CORDIC 的 8 拍流水线在跑）；② `sqr_rdy` 一拉高就转去 `square_state2`。当所有点处理完（`cnt_s==1023`），收尾切到上传状态。

**square_state2** —— [verilog files/TOP.v:372-374](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L372-L374)，纯过渡拍，仅跳转：

```verilog
square_state2: begin state<=square_state3; end
```

**square_state3** —— [verilog files/TOP.v:376-380](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L376-L380)，打复位脉冲、关写使能：

```verilog
square_state3: begin
    sclr<=1'b1;     // 复位开方核（清掉 rdy、冲刷流水线）
    we3<=1'b0;      // 停止写 ram3，防止换地址时写串
    state<=square_state4;
end
```

**square_state4** —— [verilog files/TOP.v:382-385](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L382-L385)，地址递增：

```verilog
square_state4: begin
    cnt_s<=cnt_s+1;   // 切到下一个频点
    state<=square_state5;
end
```

**square_state5** —— [verilog files/TOP.v:387-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L387-L391)，重新放行、回到起点：

```verilog
square_state5: begin
    sclr<=1'b0;     // 释放复位，让核开始算下一个点
    we3<=1'b1;      // 重新打开 ram3 写
    state<=square_state;
end
```

**为什么需要 state2/state3/state4/state5 这么多拍，而不能合并？** 因为 `cnt_s` 同时是 ram2 的读地址。如果在拉高 `sclr`（复位核）的同一拍就去 `cnt_s+1`，输入会立刻变成下一个点的值，而流水线还没清干净，容易把上一个点的残留和新输入混在一起。这四个状态本质上是在**排好一个严格的先后顺序**：先确认结果落盘 → 复位核 → 再换地址 → 再放行。这是用「多个状态 + 非阻塞赋值」换取时序确定性，是 FPGA 状态机的典型写法。

**循环处理了多少个点？** 注意 `square_state` 的判据是 `cnt_s < 1023`（`11'b1111111111` = 1023）。`cnt_s` 从 0 开始，每轮在 `square_state4` 加一。所以循环依次处理 `cnt_s = 0, 1, 2, …, 1022`，共 **1023 个点**；当 `cnt_s` 计到 1023 时，判据为假，直接转 `send_state`。也就是说，ram2 中第 **1023** 号频点并未被开方写入 ram3——这是阅读时值得留意的一处细节（可能是配合 FFT 频谱对称性的取舍，也可能是一处 off-by-one，**待结合上位机波形确认**）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：画出「ram2 读出 → 开方 → 写 ram3」的**逐拍时序**，标注 `sqr_rdy` 何时拉高。这是本讲的核心实践。

**操作步骤**：

1. 重读 [verilog files/TOP.v:357-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L391) 这五个状态，记住所有 `<=` 都是「下一拍生效」。
2. 以「进入 `square_state` 处理第 0 个点」为第 0 拍（记作 S0），逐拍推演 `state / sclr / we3 / cnt_s / sqr_rdy` 的值，以及每个上升沿 ram3 写进了什么。
3. 标出 `sqr_rdy` 在第几拍拉高、有效结果在哪一拍被写进 ram3。

**参考答案（逐拍时序表）**：设进入 `square_state` 处理第 0 点为 S0，CORDIC 取注释所述 8 拍延迟（`sqr_rdy` 在 S8 拉高）：

| 拍 | state | sclr | we3 | cnt_s | sqr_rdy | 本拍末上升沿 ram3 写入 | 下一 state |
|----|-------|------|-----|-------|---------|------------------------|-----------|
| S0 | square_state | 0 | 1 | 0 | 0 | ram3[0] ≤ （无效值） | square_state |
| S1–S7 | square_state | 0 | 1 | 0 | 0 | …（流水线计算中） | square_state |
| **S8** | **square_state** | 0 | 1 | 0 | **1** | **ram3[0] ≤ √ram2[0] ✓ 有效** | square_state2 |
| S9 | square_state2 | 0 | 1 | 0 | — | ram3[0] ≤ √ram2[0]（重复，无害） | square_state3 |
| S10 | square_state3 | 0 | 1 | 0 | — | ram3[0] ≤ √ram2[0]（末次，随后 we3→0） | square_state4 |
| S11 | square_state4 | 1 | 0 | 0 | — | 不写（we3=0） | square_state5 |
| S12 | square_state5 | 1 | 0 | 1 | — | 不写（we3=0） | square_state |
| S13 | square_state | 0 | 1 | 1 | 0 | ram3[1] ≤ （开始算第 1 点） | square_state |

**需要观察的现象与预期结果**：

- `sqr_rdy` 在 **S8**（进入 `square_state` 后约第 8 拍）拉高——这正是 TOP 注释所说的「8 个时钟周期」。
- 有效结果在 **S8 那个上升沿**被锁进 `ram3[0]`，因为此时 `we3=1`、`cnt_s=0`、`square_out` 有效三者同时成立。
- 之后 S9–S12 共 **4 拍**完成「复位核 + 地址递增 + 重新放行」，S13 开始算第 1 点。
- 因此**每处理一个点约 13 个 200 MHz 时钟周期**（8 拍流水线 + 1 拍捕获 + 4 拍握手），处理 1023 个点约 \(1023 \times 13 \approx 13300\) 拍，约 66 µs（`待本地验证`：精确的 `rdy` 拍数取决于 CORDIC IP 的内部配置）。

> 提示：表里 S1–S7 期间 `we3=1`，ram3[0] 其实每拍都在被写，只是 `square_out` 还无效；直到 S8 才写进正确值。这并不出错，因为地址一直是 0、最后写入的有效值覆盖了前面的无效值。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `square_state3` 里的 `sclr<=1'b1` 删掉（即不在点与点之间复位核），会发生什么？
**答案**：`sqr_rdy` 不会被清掉，状态机回到 `square_state` 后会立刻看到 `sqr_rdy==1`，于是一路狂奔切换地址，根本不给 CORDIC 8 拍计算时间——ram3 里会写满错误数据。这正是注释强调「`sqr_rdy` 必须手动复位」的原因。

**练习 2**：为什么 `cnt_s` 的递增放在 `square_state4`，而不是更早的 `square_state2`？
**答案**：因为 `cnt_s` 是 ram2 的组合读地址。必须等开方结果已经稳定落盘、且 `we3` 已关断（`square_state3` 做的事）之后，才能改 `cnt_s`，否则会在结果尚未写好时就改变输入，导致写串。顺序被刻意排成「先锁结果 → 再换地址」。

**练习 3**：循环结束时 `sel<=1'b1`（见 `square_state` 的 else 分支），结合 [verilog files/MUX.v:9](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/MUX.v#L9) 的 `out = sel ? a : b` 与 [verilog files/TOP.v:190-193](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L190-L193) 的 `mux_ram3`，说明 ram3 的读地址在上传阶段切到了谁。
**答案**：`mux_ram3` 的 `a=cnt`、`b=cnt_s`，`sel=1` 时 `ADR_r = a = cnt`。上传阶段 ram3 改由 `cnt`（在上传状态机里逐个递增）来读，逐点把幅度谱送出去。开方循环用 `cnt_s` 写、上传用 `cnt` 读，两者通过这个 MUX 分时复用 ram3。

---

## 5. 综合实践

**任务：把信号链最后一段「补全」，并估算开方这一段的吞吐瓶颈。**

1. **补全数据流**：结合本讲和 [u3-l1](u3-l1-fft-core-wrapper-and-handshake.md)、[u3-l2](u3-l2-magnitude-square-and-sum.md)，在纸上完整画出从 FFT 输出到 ram3 的链路，标注每一级的位宽与延迟性质：
   - `xk_re / xk_im`（各 10 位，FFT 输出）
   - → `Square × 2`（10×10→20，组合）
   - → `Sum`（20+20→21，组合）→ ram2（21 位，同步写/组合读）
   - → `Root_square`（20→11，**8 拍流水线**）→ ram3（10 位，同步写）。
2. **回答**：在这条链路里，哪一级是「组合零延迟」，哪一级是「流水线有延迟」？这种混合设计为什么可行？
3. **估算瓶颈**：用本讲得到的「每点约 13 拍」结论，估算开方 1023 个点占用的总时钟周期数，并与 FFT 核本身的变换时间相比（FFT 变换时间**待确认**，可先标注），判断开方是否是整条 DSP 链的吞吐瓶颈。
4. **延伸思考（可选）**：如果要让开方「不停顿」地流式处理（即不要每个点都打 `sclr` 停 4 拍），你会怎么改？提示——查阅 Xilinx CORDIC IP 是否支持 `nd`（new data）连续输入模式，以及连续输入时 `rdy` 的行为（**待确认**，需阅读 IP 数据手册）。

**预期产出**：一张带位宽与延迟标注的完整链路图，以及一段不超过 100 字的瓶颈判断。

## 6. 本讲小结

- `Root_square`（`Radical.v`）是 Xilinx CORDIC 开方 IP `Root` 的**纯封装壳**，自身零运算，只做 20 位输入 → 11 位输出的端口转接。
- 开方是**8 拍流水线**，不是零延迟；`sclr`（复位）与 `rdy`（`sqr_rdy`，输出有效）是它的两个握手信号。
- TOP 用 `square_state`~`square_state5` **五个状态**把单次开方编排成逐点循环：等 `sqr_rdy`（8 拍）→ 写 ram3 → 打 `sclr` 复位 → `cnt_s+1` → 重新放行。
- `cnt_s` **同时**是 ram2 的读地址和 ram3 的写地址，造就「读 ram2[n] → 开方 → 写 ram3[n]」的一一对应。
- 有效结果在 `sqr_rdy` 拉高那一拍的上升沿被锁进 ram3；每点约 13 拍，整段约 66 µs（`待本地验证`）。
- 循环判据 `cnt_s < 1023` 意味着只处理 0..1022 共 1023 个点，ram2 第 1023 号频点未被开方——一处值得留意的细节。

## 7. 下一步学习建议

- 本讲完成了 DSP 信号链的最后一环。下一篇 [u3-l4](u3-l4-dsp-chain-end-to-end.md) 会把 FFT、平方求和、开方三段**串成一条完整的幅度谱计算链**，建议紧接着读，把本讲放进端到端的时序里再过一遍。
- 想理解 ram3 里的幅度谱是如何被打包上传的，跳到 [u5-l3](u5-l3-tx-packing-subfsm-and-leds.md) 看 `send_state` 与 `state3` 打包子状态机；本讲末尾提到的 `sel`/`mux_ram3` 切换正是那里的入口。
- 对 CORDIC IP 内部如何用移位加法做开方感兴趣，可课外阅读 Xilinx 的 *CORDIC LogiCORE IP Product Guide*（PG105）——本工程把它的精确配置（流水线级数、`rdy` 模式）作为黑盒处理，相关细节在源码里**待确认**。
