# 基础模块：消抖与公用逻辑

> 所属单元：Unit 5 · projf-explore Verilog 库基础模块
> 依赖讲义：u5-l1（库总览与 SystemVerilog 风格）、u5-l2（时钟与跨时钟域同步器）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚机械按键为什么会「抖动」，以及为什么 FPGA 必须做消抖（debounce）。
2. 逐行讲明白 `debounce.sv` 这 38 行代码：两拍同步器、积分式计数器、`out` 翻转、`ondn`/`onup` 单拍脉冲各自的作用。
3. 用「连续 disagree 满 2^N 拍才确认」这一条规则，推出稳定时间与计数器位宽、时钟频率的关系式。
4. 在真实工程（Hello Arty 定时器、FPGA Pong）里找到 `debounce` 的例化点，并解释每个输出端口被如何使用。
5. 说出 projf `essential` 区「小而专、可复用」的公用模块设计范式，并以 `async_reset.sv` 为第二个例子。

## 2. 前置知识

本讲假设你已经读过 **u5-l1**（projf 库的七大分区、SystemVerilog 子集、厂商中立思想）和 **u5-l2**（跨时钟域、亚稳态、两级触发器同步器）。这里只补充两个本讲特有的概念：

- **按键（机械开关）**：FPGA 开发板上的按钮、拨码开关是物理金属触点。按下或松开的瞬间，触点不会立刻稳定接触，而是在几毫秒内反复「接通—断开」很多次，这种现象叫**抖动（bounce）**。
- **积分式消抖（integrator debounce）**：消抖有好多做法（检测到边沿后等固定时间再采样、移位寄存器多数表决等）。projf 用的是一种「积分」思路——只要输入和当前已确认的状态不一致，就累加计数器；一旦计数器计满，才认定这是一次真正的状态变化。本讲的 4.2 节会把它讲透。

> 一句话直觉：**消抖模块就像一个「很有耐心」的判官**——你（按键）反复变来变去它都不信，只有当你连续保持同一个态度超过 2.6 毫秒，它才点头承认「好吧，这次是真的」。

## 3. 本讲源码地图

本讲涉及的关键文件，都在 `ThreePart/projf-explore/` 下：

| 文件 | 行数 | 作用 |
|---|---|---|
| [`lib/essential/debounce.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv) | 38 | **本讲主角**：按键消抖模块，库的权威实现 |
| [`lib/essential/README.md`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/README.md) | — | essential 区说明，列出全部两个模块 |
| [`lib/essential/xc7/async_reset.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/xc7/async_reset.sv) | 27 | essential 区的另一个模块：异步复位同步器（Xilinx 7 系专用） |
| [`hello/hello-arty/K/top.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/hello/hello-arty/K/top.sv) | 77 | 真实用法：Arty 定时器，用 `onup` 喂状态机 |
| [`graphics/pong/sim/top_pong.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/top_pong.sv) | 265 | 真实用法：Pong 游戏，把 `debounce` 跑在**像素时钟**上 |
| [`hello/hello-arty/K/debounce.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/hello/hello-arty/K/debounce.sv) | 37 | 库版本的「裁剪副本」：20 位计数器（≈10 ms）、去掉 `ondn` |

注意：`lib/essential/` 目录下一共只有**两个模块**——`debounce`（本讲主角）和 `xc7/` 下的 `async_reset`（4.3 节讲）。README 里写的「Vivado test benches in the xc7 directory」在本 HEAD 实际并不存在（xc7/ 下只有 `async_reset.sv`），所以本讲的 testbench 是我们自己写的示例代码。

---

## 4. 核心概念与源码讲解

### 4.1 debounce 模块：抖动问题与外部接口

#### 4.1.1 概念说明

开发板上的按键是一个机械开关。理想情况下，按下应该在 0 时刻立刻、干净地从 0 变到 1：

```
理想按键:  ____|‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾   （一次干净的跳变）
```

但真实金属触点在接触瞬间会弹跳，示波器上看到的更像是：

```
真实按键:  ____|‾|_|‾‾|_|‾|‾‾‾‾‾‾‾‾‾‾   （前沿抖动 1~10 ms，之后才稳定）
```

如果 FPGA 直接把这个原始信号送给一个「按一次加一分」的计分逻辑，那么一次按压可能被识别成几十次按压——分数瞬间暴涨。**消抖模块的作用，就是把这段「抖动」滤掉，只在信号真正稳定后才输出一次干净的状态变化。**

`debounce` 模块对外提供四个信号：一个去抖后的**电平** `out`，以及两个**单拍（one tick）脉冲** `ondn`（按下瞬间）和 `onup`（松开瞬间）。下游逻辑想用电平就接 `out`，想只对「按下的那一刻」反应就接 `ondn`。

#### 4.1.2 核心流程

从外部看，一次完整的「按下→松开」会这样流动：

```
原始 in:   ____|‾|_|‾‾|_|‾|‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾|_|‾|____
              ↑ 抖动                 ↑ 稳定按下         ↑ 抖动      ↑ 稳定松开

out:       ____________________________|‾‾‾‾‾‾‾‾‾‾‾‾‾‾|___________
                                      ↑                ↑
                                  ondn 一拍          onup 一拍
                              （稳定满 2.6ms 后）   （稳定满 2.6ms 后）
```

要点：

1. 抖动期间 `out` **纹丝不动**，`ondn`/`onup` **不产生**脉冲。
2. 只有 `in` 连续保持新电平超过稳定阈值（≈2.6 ms）后，`out` 才翻转。
3. 翻转的那一拍，对应的脉冲（`ondn` 或 `onup`）恰好高一个时钟周期。

#### 4.1.3 源码精读

先看模块的端口声明——这是它的「外部合同」：

[debounce.sv:8-14](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L8-L14) 声明了一个时钟输入 `clk`、一个待消抖的信号输入 `in`，以及三个输出：去抖电平 `out`、按下脉冲 `ondn`、松开脉冲 `onup`。注意端口用了 SystemVerilog 的 `wire logic` / `logic` 写法（详见 u5-l1）：`clk`/`in` 是 wire 输入，三个输出都是 `logic`（可被 `always_ff`/`always_comb` 驱动）。

文件开头还有两行值得认识的「纪律性」代码：

[debounce.sv:5-6](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L5-L6) —— `` `default_nettype none`` 关闭「隐式线网」，强制所有信号必须显式声明，能挡住大量拼写错误；`` `timescale 1ns / 1ps`` 设定仿真时间单位和精度。这是 projf 全库统一的工程纪律。

模块内部的实现细节（同步器、计数器、翻转逻辑）留给 4.2 节逐行精读。这里只先建立外部印象。

#### 4.1.4 代码实践：在真实工程里找 debounce

这是一个**源码阅读型实践**，目标是确认「库里的模块确实被这么用」。

1. **实践目标**：看清 `debounce` 的三个输出（`out`/`ondn`/`onup`）在真实设计里分别被如何取舍。
2. **操作步骤**：
   - 打开 [hello/hello-arty/K/top.sv:22-25](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/hello/hello-arty/K/top.sv#L22-L25)：这里为例化的三个按键各自接了哪个输出？`out` 端口连到了什么（注意 `()` 空连接的含义）？
   - 打开 [graphics/pong/sim/top_pong.sv:81-87](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/top_pong.sv#L81-L87)：开火键 `deb_fire` 用了哪个脉冲？上下键 `deb_up`/`deb_dn` 又分别用了 `out` 还是脉冲？为什么开火用脉冲、移动用电平？
3. **需要观察的现象**：你会发现「按下一次只触发一次」的逻辑（开火、确认）接的是 `ondn`/`onup` 脉冲；而「按住持续生效」的逻辑（球拍持续上移）接的是 `out` 电平。
4. **预期结果**：Pong 里 `deb_up`/`deb_dn` 接的是 `out`（`sig_up`/`sig_dn`），因为球拍要在按住时每帧移动；`deb_fire` 接的是 `onup`（`sig_fire`），因为开火只需按一次。
5. **一个进阶观察**：Pong 把 `debounce` 的 `.clk` 接成了 `clk_pix`（像素时钟），而 Hello Arty 接的是板载 100 MHz 时钟。**同一个模块，喂的时钟不同，实际稳定时间就不同**——像素时钟约 25 MHz（待本地验证），2^18 拍约 10.5 ms；100 MHz 时则约 2.6 ms。这正是 4.2 节要推导的公式。

#### 4.1.5 小练习与答案

**练习 1**：如果某个设计只想知道「按键被按下了」，应该接 `out`、`ondn` 还是 `onup`？
**答**：接 `ondn`。它是在确认「真正按下」那一拍高一个周期，天然适合做「按一次做一件事」的触发，下游不用自己再做边沿检测。

**练习 2**：端口声明里 `output logic out` 和 `output wire logic` 有什么区别？为什么 `out` 不写成 `wire`？
**答**：`out` 要在 `always_ff` 里被赋值（见 4.2），所以必须是 `logic`（可被过程块驱动）而不是 `wire`（只能用 `assign` 连续驱动）。projf 用 `logic` 统一类型，详见 u5-l1。

---

### 4.2 同步+计数消抖：两拍同步器与积分式计数器

#### 4.2.1 概念说明

`debounce` 内部做了两件事，每件都对应一个经典的 FPGA 设计问题：

**第一件事：跨时钟域同步（承接 u5-l2）。**
按键信号来自 FPGA 外部的物理世界，与 `clk` 完全异步。异步信号撞上时钟沿会引发**亚稳态**（metastability）——触发器输出停留在半电压值，要花不可预期的时间才能随机稳定到 0 或 1，而且不同负载可能看到不同值。解决办法是「打两拍」：把异步信号串两级触发器，给第一级足够长的恢复时间，使第二级采样到稳定值。这正是 u5-l2 讲过的两级同步器，这里它被复用进来吸收亚稳态。

**第二件事：积分式计数消抖。**
同步后的信号 `sync_1` 仍带着抖动。模块的判官逻辑是：拿 `sync_1` 跟「已经确认的输出 `out`」比——
- 若两者**相同**（`idle=1`）：说明输入与现状一致，没有要确认的事，计数器清零、原地待命；
- 若两者**不同**（`idle=0`）：说明输入在「叫板」，开始累加计数器；只有当计数器**连续计满** 2^18 拍都没被打断（即输入连续 2.6 ms 不肯回到旧电平），才认定这是真的变化，把 `out` 翻转。

这里的关键词是「**连续**」。任何一个抖动让 `sync_1` 短暂回到与 `out` 相同，`idle` 立刻变 1，计数器归零——所以连续短抖动永远凑不满 2^18 拍，会被统统忽略。这比「检测到边沿就等固定时间」的方案更鲁棒，因为它对任意长度的抖动列车都有效。

#### 4.2.2 核心流程

把内部机制画成数据流：

```
        ┌──────────── 两拍同步器 (抗亚稳态) ────────────┐
   in ──┤ FF sync_0 ──┬── FF sync_1 ──┐                 │
        └─────────────┘               │                 │
                                      ▼                 │
   out ──────────────────────► (==) ──┤ idle  ──────────┤
                                      │                 │
                         ┌────────────┘                 │
                         ▼                              │
              idle=1? ──► cnt <= 0        (一致：待命)   │
              idle=0? ──► cnt <= cnt+1    (叫板：积分)   │
                         │                              │
                  cnt 全 1? ── max ──┐                   │
                                   ▼                    │
                        out <= ~out  (翻转！)            │
                                   ▼                    │
              ondn = ~idle & max & ~out  (按下一拍)      │
              onup = ~idle & max &  out  (松开一拍)      │
```

计数器位宽 N 与稳定时间的关系（核心公式）：

\[
T_{\text{stable}} \approx \frac{2^{N}}{f_{\text{clk}}}
\]

库版本 `N = 18`、`f_{\text{clk}} = 100\,\text{MHz}`：

\[
T_{\text{stable}} \approx \frac{2^{18}}{10^{8}} = \frac{262\,144}{10^{8}}\,\text{s} \approx 2.62\,\text{ms}
\]

源码注释 [debounce.sv:21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L21) 写的正是 `2^18 = 2.6 ms counter at 100 MHz`。把 N 换成 20（Hello Arty/K 的副本）就是 ≈10.5 ms；把时钟换成 25 MHz（Pong 像素时钟）同样位宽下就是 ≈10.5 ms。**改稳定时间有两条独立途径：改计数器位宽，或换时钟。**

#### 4.2.3 源码精读

**第一段：两拍同步器。**

[debounce.sv:16-19](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L16-L19) 用两个 `always_ff` 把异步的 `in` 依次打两拍得到 `sync_1`。这就是 u5-l2 讲过的两级触发器同步器，吸收跨时钟域亚稳态。后续所有判断都用 `sync_1`，而不再碰原始 `in`。

**第二段：纯组合的「判官」逻辑。**

[debounce.sv:21-28](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L21-L28) 集中定义了四个组合信号，是全模块的「大脑」：

- `logic [17:0] cnt;` —— 18 位计数器（注释说明 ≈2.6 ms @100 MHz）。
- `idle = (out == sync_1);` —— 输入是否与已确认输出**一致**。一致则「空闲」，否则「叫板」。
- `max = &cnt;` —— 归约与（reduction AND），当 `cnt` 所有位都为 1（计满）时为真。`&cnt` 是「判断是否到上限」的简洁写法。
- `ondn = ~idle & max & ~out;` —— 「正在叫板 + 刚计满 + 当前是松开态」 ⇒ 这一拍确认**按下**。
- `onup = ~idle & max & out;` —— 「正在叫板 + 刚计满 + 当前是按下态」 ⇒ 这一拍确认**松开**。

注意 `ondn`/`onup` 为什么天然是单拍脉冲：它们要求 `max` 为真，而 `max` 为真的那一拍一旦结束（`out` 在时序块里翻转后 `idle` 变 1、计数器清零），`~idle & max` 立刻不成立，脉冲自动消失。

**第三段：时序的计数与翻转。**

[debounce.sv:30-37](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L30-L37) 是唯一的时序块，规则极简：

- `if (idle) cnt <= 0;` —— 一致就清零计数器（待命 / 抹掉短抖动）。
- `else` —— 不一致就 `cnt <= cnt + 1;`（积分）；并且 `if (max) out <= ~out;`（计满则翻转 `out`）。

为什么用「翻转 `out <= ~out`」而不是「直接 `out <= sync_1`」？因为本模块的设计前提是：**只有当输入连续与 `out` 相反满 2^N 拍，才确认变化**。既然已经确认要变，翻一下 `out` 正好让它追上新的 `sync_1`。这是一种很省逻辑的写法——不需要再比较「新值到底是 0 还是 1」。

把三段串起来，一次确认按下的完整时序（设 `out` 初值 0，按键按下 `in=1`）：

| 拍 | sync_1 | idle | cnt 走向 | out | ondn |
|---|---|---|---|---|---|
| 起始 | 0 | 1（0==0） | 保持 0 | 0 | 0 |
| 按下后 2 拍 | 1 | 0（0≠1） | 开始 +1 | 0 | 0 |
| 连续 2^18 拍末 | 1 | 0 | 到达全 1（max=1） | 0 | **1（单拍）** |
| 下一拍 | 1 | 1（1==1） | 清零 | **1（翻转）** | 0 |

松开过程对称，触发 `onup`。

#### 4.2.4 代码实践：为 debounce 写 testbench（本讲主实践）

> **说明**：本仓库 `lib/essential/` 下**没有**提供 `debounce` 的 testbench（README 所称的 xc7 测试台在本 HEAD 不存在）。下面是为本讲编写的**示例代码**，可保存为 `tb_debounce.sv` 与 `debounce.sv` 一起仿真。

1. **实践目标**：用仿真亲眼验证两件事——(a) 短抖动不会让 `out` 翻转、不会产生 `ondn`/`onup`；(b) 只有输入连续稳定超过 ≈2.6 ms 后，`out` 才翻转并在那一拍产生单拍脉冲。

2. **操作步骤**：把下面这段示例 testbench 保存为 `tb_debounce.sv`，与库里的 `debounce.sv` 放同一目录，用 Icarus Verilog / Verilator / Vivado 仿真：

   ```systemverilog
   // 示例代码：debounce.sv 的测试激励（本仓库未提供，本讲义编写）
   `timescale 1ns / 1ps
   `default_nettype none

   module tb_debounce;
       logic clk = 0;
       logic in;
       wire  out, ondn, onup;

       // 1) 100 MHz 时钟，周期 10 ns
       always #5 clk = ~clk;

       // 2) 例化待测模块
       debounce dut (.clk, .in, .out, .ondn, .onup);

       // 3) 捕获并打印每一次单拍脉冲（带时间戳，单位 ns）
       always @(posedge clk) begin
           if (ondn) $display("[%0t ns] ONDN  -> 按下被确认, out=%b", $time, out);
           if (onup) $display("[%0t ns] ONUP  -> 松开被确认, out=%b", $time, out);
       end

       // 4) 主激励：模拟「带抖动的按下」和「带抖动的松开」
       initial begin
           in = 0;                       // 空闲为低
           #20_000;                      // 先稳定 20 us

           // —— 带抖动的按下：每次抖动只有几 us，远小于 2.6 ms，会被吃掉 ——
           in = 1;  #5_000;              // 抖高 5 us
           in = 0;  #4_000;              // 抖低 4 us（回到 out，计数器清零）
           in = 1;  #3_000;              // 抖高 3 us
           in = 0;  #2_000;              // 抖低 2 us（再次清零）
           in = 1;                       // 最终稳定按下
           #5_000_000;                   // 等 5 ms（>2.6 ms）让 out 翻转

           // —— 带抖动的松开 ——
           in = 0;  #4_000;
           in = 1;  #3_000;
           in = 0;                       // 最终稳定松开
           #5_000_000;

           $finish;
       end
   endmodule
   ```

3. **需要观察的现象**：
   - 在前 2.6 ms 的抖动阶段，**不会有任何** `ONDN` 打印——说明短抖动被滤掉了。
   - 在「最终稳定按下」之后约 2.62 ms 处，打印一行 `ONDN -> 按下被确认, out=1`；`out` 在这之后保持 1。
   - 在「最终稳定松开」之后约 2.62 ms 处，打印一行 `ONUP -> 松开被确认, out=0`；`out` 回到 0。

4. **预期结果（关键时间验证）**：用打印的时间戳做减法——`ONDN` 的时刻减去「最后一次 `in=1` 稳定」的时刻，应该约等于 2.62 ms（262 144 拍 × 10 ns）。**它不是从第一次抖动算起**，而是从最后一次稳定算起，这正是「连续计满才确认」的直接证据。

5. **若嫌仿真太慢**：可临时把 [debounce.sv:21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L21) 的 `[17:0]` 改小（如 `[7:0]`），稳定时间降到 2.56 us，方便快速观察；验证完务必改回。该改动只用于仿真观察，**不要提交到源码**。

> 仿真命令示例（待本地验证，取决于你装的工具）：
> - Icarus：`iverilog -g2012 -o tb tb_debounce.sv debounce.sv && vvp tb`
> - Verilator：`verilator --binary --timing -Wno-WIDTH -Wno-UNOPTFLAT tb_debounce.sv debounce.sv`

#### 4.2.5 小练习与答案

**练习 1**：把计数器位宽从 18 改成 20，稳定时间变成多少？这对消抖效果有什么影响？
**答**：由 \(T \approx 2^{N}/f\)，N=20、100 MHz 时 ≈10.5 ms，约变为原来的 4 倍。消抖更彻底（能容忍更长的抖动列车），代价是对「快速连按」的响应变慢——人手连按间隔若小于 10 ms 会被吞掉。Hello Arty/K 的副本正是用了 20 位（≈10 ms），见 [hello/hello-arty/K/debounce.sv:20](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/hello/hello-arty/K/debounce.sv#L20)。

**练习 2**：`max = &cnt;` 这句中 `&cnt` 叫什么运算？为什么它能判断「计满」？
**答**：归约与（reduction AND），把向量所有位 AND 起来得到 1 bit。计数器从 0 不断 +1，只有当它到达 `18'h3FFFF`（所有位都为 1）时 `&cnt` 才为 1，所以它天然就是「是否到达 2^18−1 上限」的判据，比写 `cnt == 18'h3FFFF` 更简洁。

**练习 3**：为什么 `ondn`/`onup` 不需要额外寄存器就能保证是单拍脉冲？
**答**：因为它们的条件含 `max`，而 `max` 只在计数器计满的那一拍为真；同一拍的时序块会翻转 `out`，下一拍 `out == sync_1` 使 `idle=1`、计数器清零，于是 `~idle & max` 立刻不成立，脉冲自然只高一拍。

---

### 4.3 公用逻辑设计范式：async_reset 与可复用思想

#### 4.3.1 概念说明

`essential` 区放的是「不好归类、但几乎每个工程都用得上」的小工具。除了主角 `debounce`，本 HEAD 下只有另一个模块：`async_reset`（异步复位同步器）。它的职责是把外部「粗暴」的异步复位信号，整理成一个对 FPGA 内部时序安全的复位。

这里要先理解 FPGA 复位的一条重要纪律——**异步断言、同步释放（async assert, sync deassert）**：

- **异步断言**：复位信号一旦有效，立刻（不等时钟）把电路拉进复位态，确保哪怕时钟没起来也能复位。
- **同步释放**：复位撤销时，必须对齐到时钟沿再撤，否则撤销瞬间恰好在时钟沿附近，又会让受复位控制的触发器进入亚稳态。

`async_reset` 正是干这件事：输入 `rst_in` 一拉高，`rst_out` 立刻（异步）有效；`rst_in` 撤销后，`rst_out` 再随时钟**缓缓（同步）**撤销。它本质上又是一个「同步器」（承接 u5-l2 的同步思想），只是同步的对象是复位而非普通数据。

更重要的是，这个模块还示范了 projf `essential` 区的**公用模块设计范式**：

1. **单一职责**：`debounce` 只消抖，`async_reset` 只同步复位，互不越界。
2. **放对位置**：纯逻辑的 `debounce.sv` 放分区根目录（厂商中立）；用到 Xilinx 专用属性 `ASYNC_REG` 的 `async_reset.sv` 放进 `xc7/` 子目录——这正对应 u5-l1 讲过的「厂商中立放根、厂商相关分子目录」纪律。
3. **小而精**：两个模块都在 30~40 行内，一眼能读完，容易移植、容易验证。

#### 4.3.2 核心流程

`async_reset` 用一个 2 位移位寄存器 `rst_shf`，在复位撤销时把「1」逐拍移出去：

```
rst_in=1 (异步有效) ──► {rst_out, rst_shf} = 3'b111   （立刻全置位）
                            │
rst_in=0 后，每来一个时钟沿 ──►  {rst_out, rst_shf} <= {rst_shf, 1'b0}
                            │
                            └─► 两个 1 被逐拍移出，rst_out 再过两拍才变 0
                                 （= 同步释放，给受复位触发器留足恢复时间）
```

初始（`initial`）就让 `rst_out=1`、`rst_shf=2'b11`——即**上电直接进入复位态**，等时钟稳定后再由移位逻辑把复位释放掉。这对「时钟由 PLL 产生、需要时间锁定」的场景尤其重要（呼应 u5-l2「等时钟 lock 再用」的纪律）。

#### 4.3.3 源码精读

[async_reset.sv:8-12](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/xc7/async_reset.sv#L8-L12) 声明端口：一个时钟 `clk`、异步复位输入 `rst_in`、整理后的复位输出 `rst_out`。

[async_reset.sv:14-17](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/xc7/async_reset.sv#L14-L17) 关键两行：`(* ASYNC_REG = "TRUE" *)` 是 **Xilinx 专用综合属性**，告诉综合器把 `rst_shf` 这两级触发器放进同一个 SLICE、紧紧挨着布局，并禁止优化掉它们——这是让同步器真正可靠的物理保障（也正因如此该文件被放进 `xc7/` 厂商子目录）。两条 `initial` 让上电即处于复位。

[async_reset.sv:19-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/xc7/async_reset.sv#L19-L24) 是核心时序块，敏感列表写成 `posedge clk or posedge rst_in`——既有时钟沿又有复位沿，所以是「异步复位」写法：

- `if (rst_in) {rst_out, rst_shf} <= 3'b111;` —— 复位有效，立刻（异步）把输出和移位寄存器全置 1。
- `else {rst_out, rst_shf} <= {rst_shf, 1'b0};` —— 复位撤销后，每个时钟沿把 `rst_shf` 整体左移、低位补 0。从 `3'b111` 出发，经过两拍后 `rst_out` 才采样到 0——这就是「同步释放」的两拍延迟。

代码里两行 `/* verilator lint_off SYNCASYNCNET */` 是给 Verilator 静音的：lint 工具看到一个网络既被异步复位又被时钟驱动会告警，而这里是有意为之，所以临时关掉该检查再打开。这也是 projf 全库「兼容多工具链」纪律的体现（u5-l1）。

#### 4.3.4 代码实践：阅读 async_reset 并画时序

1. **实践目标**：理解「异步断言、同步释放」，并确认它本质是复位版的同步器。
2. **操作步骤**：
   - 读 [async_reset.sv:19-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/xc7/async_reset.sv#L19-L24)，设 `rst_in` 在某时刻拉高、若干拍后撤销。
   - 在纸上画出 `rst_in`、`rst_shf[1:0]`、`rst_out` 三个波形，标出「拉高瞬间 `rst_out` 立刻变 1」和「撤销后 `rst_out` 还要等 2 个时钟沿才变 0」。
3. **需要观察的现象**：`rst_out` 的**拉高**与 `rst_in` 几乎同时（异步、不等时钟），但 `rst_out` 的**拉低**严格发生在 `rst_in` 撤销之后的某个时钟沿（同步）。
4. **预期结果**：从 `rst_in` 撤销到 `rst_out` 撤销，最多差 2 个时钟周期；这 2 拍就是给下游受复位触发器逃离亚稳态的恢复窗口。
5. **若无法确认**：标注「待本地验证」，但时序关系可由代码逻辑直接推出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `async_reset.sv` 放在 `xc7/` 子目录，而 `debounce.sv` 放在 `essential/` 根目录？
**答**：因为 `async_reset` 用了 Xilinx 专用的 `ASYNC_REG` 综合属性（以及对应的布局约束），只在 Xilinx 7 系上有意义；而 `debounce` 是纯 RTL 逻辑，不依赖任何厂商原语，所有 FPGA 平台都能用。这对应 u5-l1 的「厂商中立放根目录、厂商相关分子目录」纪律。

**练习 2**：把 `async_reset` 和 4.2 的两拍同步器对比，它们的共同点是什么？
**答**：都是「用多级触发器给一个异步来源争取恢复时间，避免下游亚稳态」。`debounce` 同步的是异步数据信号（按键），`async_reset` 同步的是异步复位；两者都靠「串两级触发器」降低亚稳态传播风险（承接 u5-l2）。

---

## 5. 综合实践：给 Hello Arty 定时器换一个「更慢」的消抖

把本讲知识串起来的小任务：projf 的 Hello Arty K（[hello/hello-arty/K/top.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/hello/hello-arty/K/top.sv)）用了一个**本地副本** `debounce.sv`（[hello/hello-arty/K/debounce.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/hello/hello-arty/K/debounce.sv)），它和库版本有两处不同：

1. 计数器是 20 位（`logic [19:0] cnt;`，注释 `2^20 = 10 ms`），稳定时间约 10 ms。
2. 只导出了 `out` 和 `onup`，**去掉了** `ondn`。

请完成：

1. **对照阅读**：把 [hello/hello-arty/K/debounce.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/hello/hello-arty/K/debounce.sv) 与库版本 [lib/essential/debounce.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv) 逐行对比，列出所有差异（位宽、端口、组合逻辑行）。
2. **解释取舍**：为什么定时器工程要单独维护一个副本，而不是直接用库版本？（提示：位宽即稳定时间，是工程特定的取舍；去掉 `ondn` 是因为它只接 `onup`。）这反映了「库版本并未参数化」的现实——要改稳定时间，目前只能复制一份改位宽。
3. **动手改（仿真验证）**：把副本的位宽从 20 改回 18，用你在 4.2.4 写的 testbench（注意改一下例化的模块来源）验证 `onup` 的稳定时间从 ≈10 ms 降到 ≈2.6 ms。**只在自己的仿真沙盒里改，不要改源码仓库。**
4. **延伸思考**：如果你来改进这个库，会不会把计数器位宽做成 `parameter`（例如 `parameter int CNT_W = 18`）？这样做的好处和代价各是什么？（好处：一处实例化即可调稳定时间，不必复制模块；代价：要在端口表里加参数，且要确保 `cnt` 位宽和 `max = &cnt` 仍正确。）

> 这个综合实践把「抖动概念 → 积分式计数器 → 位宽与稳定时间公式 → 真实工程的取舍 → 参数化设计」整条链路串了起来，做完你就真正吃透了 projf 的消抖模块。

## 6. 本讲小结

- 按键是机械开关，按下/松开瞬间有 1~10 ms 的抖动，FPGA 必须消抖，否则一次按压会被识别成几十次。
- `debounce.sv` 内部两步走：先用**两拍同步器**（承接 u5-l2）吸收跨时钟域亚稳态，再用**积分式计数器**过滤抖动。
- 判官规则只有一条：输入连续与已确认输出 `out` **相反**满 \(2^{N}\) 拍才翻转 `out`；任何短抖动让输入回到与 `out` 相同，计数器立刻清零。
- 稳定时间 \(T \approx 2^{N}/f_{\text{clk}}\)：库版本 N=18 @100 MHz ≈ 2.6 ms；改位宽或换时钟（如 Pong 用像素时钟）都会改变实际稳定时间。
- `out` 给电平、`ondn`/`onup` 给单拍脉冲；脉冲天然单拍，因为条件里含只在计满那一拍为真的 `max`。
- `essential` 区另一模块 `async_reset`（`xc7/` 子目录）示范「异步断言、同步释放」复位纪律，与 projf「单一职责、厂商中立放根、厂商相关分子目录、小而精」的公用模块设计范式。

## 7. 下一步学习建议

- **横向联系**：本讲的 `debounce` 和 `async_reset` 都是「同步器」家族成员，回头重读 u5-l2 的 `xd.sv` 跨时钟域同步器，体会三种同步器（数据 CDC、脉冲 CDC、复位同步）的共性与差异。
- **走向应用**：带上本讲的 `debounce`，进入 Unit 6 的显示与图形世界——推荐先读 [graphics/pong/sim/top_pong.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/top_pong.sv)，看 `debounce` 如何与显示时序、碰撞检测一起拼出一个完整的 Pong 游戏（对应 u6-l5）。
- **深入数学模块**：若对 `essential` 区的「计数器」意犹未尽，可接着读 u7 数学运算单元，那里有更复杂的计数器/状态机（除法、开方、LFSR）。
- **阅读建议源码**：projf 官方博文 [Hello Arty Part 3](https://projectf.io/posts/hello-arty-3/) 与 [FPGA Pong](https://projectf.io/posts/fpga-pong/)（见 [essential/README.md:15](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/README.md#L15)）给出了 `debounce` 的完整上板教学，强烈推荐配套阅读。
