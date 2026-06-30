# 测试方法学：随机激励与自检 testbench

## 1. 本讲目标

本讲是「专家层」的第一讲，主题不再是某个具体 RTL 模块，而是**怎么写 testbench（测试平台）去验证模块**。读完本讲你应该能够：

- 说清「随机激励」为什么比固定激励更能暴露 bug，并掌握仓库里两种产生随机数的手段（`$urandom` 系统函数与 `c_rand` RTL 模块）。
- 理解为什么 testbench 里要故意注入「异步时钟」和「抖动」，以及仓库是怎么用一行 `always @(*)` 给时钟加随机抖动的。
- 学会写「自校验」testbench：不靠人眼盯波形，而是让 testbench 自己用黄金模型去比对 DUT（被测模块）输出，发现不一致就报错。

本讲的全部手法都直接取自仓库里真实存在的 testbench（`main_tb.sv`、`fifo_single_clock_ram_tb.sv`、`cdc_strobe_tb.sv`、`delayed_event_tb.sv`），不引入任何外部框架。

## 2. 前置知识

在进入本讲前，请确认你已经掌握（这些都在前面的讲义里讲过）：

- **testbench 是什么**：它是不可综合的「测试仪器」，可以大胆使用 `initial`、`#延时`、`forever`、`$display` 等仿真专用结构，目的是给被测模块施加时钟、复位和激励（见 u1-l3）。
- **`timescale` 编译指令**：`1ns / 1ps` 表示时间单位是 1 ns、精度是 1 ps，它决定了 `#2.5` 到底是多长物理时间（见 u1-l3）。
- **手写时钟套路**：`initial begin #0 clk=0; forever #2.5 clk=~clk; end` 产生周期为 5 ns（即 200 MHz）的方波（见 u1-l3、u2-l1）。
- **`clk_divider` 与 `edge_detect`**：本讲的实践任务以 `edge_detect` 为被测模块，它是「用一级延迟寄存器比较得到上升/下降沿脉冲」的电路（见 u2-l2）。

本讲会新引入的概念：随机激励、伪随机数发生器（PRNG）、线性同余发生器（LCG）、种子（SEED）与可复现性、异步时钟与抖动注入、黄金模型、自校验、连续比对与状态锁存。

一个贯穿全讲的核心理念：**好的 testbench 要让 bug 自己跳出来报错，而不是依赖你盯着波形用眼睛找。** 随机激励负责「把 bug 激发出来」，自校验负责「把 bug 报出来」，异步时钟负责「把真实世界里才会出现的危险时序也测到」。三者合起来，才是一个能长期回归、能抓到回归 bug 的 testbench。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `main_tb.sv` | 仓库根目录的 testbench 模板。演示手写时钟、带随机抖动的异步时钟 `clk33`、周期性复位、以及用 `c_rand` 产生随机数。注意它例化的 `module_under_test` 是占位符，需替换为你自己的模块才能编译。 |
| `fifo_single_clock_ram_tb.sv` | **自校验 + 随机激励**的范例。把自写的 FIFO 和厂商 FIFO 并排跑同一份随机读写请求，连续比对两者输出，不一致就把 `success` 标志拉低。 |
| `cdc_strobe_tb.sv` | **跨时钟域**自校验范例。在两个时钟域各放一个计数器，统计源域发出多少脉冲、目的域收到多少脉冲，用计数差衡量丢脉冲。 |
| `delayed_event_tb.sv` | **事件驱动激励 + 可复现随机**的范例。用 `sim_clk_gen` 产生带抖动时钟，用 `$urandom(1)` 固定种子，用 `repeat(@posedge)` 同步地施加复位与激励。 |
| `sim_clk_gen.sv` | 参数化仿真时钟发生器，能同时输出理想时钟 `clk` 和带抖动时钟 `clkd`（u1-l3 已介绍，本讲重点看它的抖动注入）。 |
| `Advanced Synthesis Cookbook/random/c_rand.v` | 可综合的伪随机数发生器（LCG），是仓库里 `_tb.sv` 共用的随机源。**注意：仓库里只有这一份 `c_rand`，根目录的 testbench 都假设你把它拷到编译路径里。** |
| `edge_detect.sv` | 本讲实践任务的被测模块（DUT）。 |

> 提示：仓库里还有一份「开箱即用、自包含」的 testbench 模板 `example_projects/testbench_template_tb/main_tb.sv`，它不依赖占位模块，比根目录的 `main_tb.sv` 更适合作为新 testbench 的起点（u1-l3 推荐过）。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**随机激励 → 异步时钟注入 → 自校验**。它们正好对应一个成熟 testbench 的三件事：拿什么喂给 DUT、在什么样的危险环境里喂、怎么判断 DUT 的回答对不对。

### 4.1 随机激励（Random Stimulus）

#### 4.1.1 概念说明

「激励」就是 testbench 施加给 DUT 输入端的信号序列。最朴素的写法是**定向激励**（directed stimulus）：手写「先写 0，再写 1，再写 2……」。它直观、好理解，但有两个致命弱点：

1. **只能测到你想到的情况**。你没想到的边界（同时读写且 FIFO 满、复位正好打在事务中间……）就测不到。
2. **写起来又长又脆**。每加一个场景就要手写一大段 `#延时` 序列，改一个参数就要重写。

**随机激励**（random stimulus）的思路反过来：让 testbench 用随机数驱动输入，跑成千上万拍，让各种罕见组合「自己撞出来」。这比人脑穷举能覆盖更多角落。但随机带来一个新问题——**如果这次跑出了 bug，下次还能复现吗？** 所以随机激励必须配合「固定种子」，让同一段随机序列可以一键重放。这就叫**可复现性**（repeatability）。

仓库里提供两种产生随机数的方式，要分清：

| 方式 | 是什么 | 能否综合 | 适用场景 |
| --- | --- | --- | --- |
| `$urandom` / `$urandom_range(l,r)` | SystemVerilog 内建系统函数，每拍调用返回一个新随机数 | **否**，仅仿真 | testbench 内部产生激励、延时、地址 |
| `c_rand` 模块 | 一个 RTL 例化的伪随机数发生器 | **是** | 需要把「随机」作为总线信号同时喂给 DUT 和别的模块，或要上板验证 |

一句话区别：`$urandom` 是「写在 always 块里、每拍现取」的函数；`c_rand` 是「例化成一个模块、输出一根随机总线」的器件。前者轻便，后者可被多个模块共享、还能综合进真实电路。

#### 4.1.2 核心流程

**`$urandom` 的可复现用法**：在 `initial` 里最先调用一次 `$urandom(SEED)`，之后每次 `$urandom` / `$urandom_range` 都从这条确定的序列里取值。换不同的 `SEED` 就换一组随机场景，固定 `SEED` 就能精确复现某次失败。

```text
initial 开始
  ├─ $urandom(SEED)          // 种下种子，决定整条随机序列
  └─ 之后每个 $urandom_range(...) 都按确定顺序取值
```

**`c_rand` 的工作原理**是一个**线性同余发生器**（Linear Congruential Generator, LCG）。它用一个递推式不断更新内部状态，再取状态的高位当作随机数输出：

\[
\text{state}_{n+1} = (a \cdot \text{state}_n + c) \bmod 2^{32}
\]

仓库里取的常数是 \(a=\text{0x343FD}\)、\(c=\text{0x269EC3}\)（这正是 C 标准库 `rand()` 用的经典常数）。输出取状态的高 15 位：

\[
\text{out} = (\text{state} \gg 16)\ \&\ \text{0x7FFF}
\]

LCG 不是密码学安全的随机，但它**确定、简单、可综合**，对功能验证完全够用。「线性」意味着序列会周期性循环，但周期长达 \(2^{32}\)，验证里基本碰不到循环。

#### 4.1.3 源码精读

先看 `c_rand` 模块本身。它的核心就是上面那个 LCG 递推，外加一个 `reseed` 端口允许临时换种子：

[Advanced Synthesis Cookbook/random/c_rand.v:L34-L46](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/Advanced%20Synthesis%20Cookbook/random/c_rand.v#L34-L46) —— `state` 寄存器每个时钟沿用 `state*343fd+269EC3` 更新；`reseed` 有效时改用外部 `seed_val` 重新播种；输出取 `state>>16` 的低 15 位。

仓库里所有 `_tb.sv` 都通过例化它来获得一根 16 位的随机总线 `RandomNumber1`。下面是 FIFO testbench 的用法，它把随机总线的不同位段当成「写请求」「读请求」「写数据」三路独立随机源：

[fifo_single_clock_ram_tb.sv:L74-L81](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L74-L81) —— 例化 `c_rand`，把派生时钟 `DerivedClocks` 当种子，输出 16 位随机数。

[fifo_single_clock_ram_tb.sv:L162-L166](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L162-L166) —— 用「`RandomNumber1` 的第 10、9 位同时为 1」当写请求、第 8、7 位同时为 1 当读请求，数据直接用整个随机数。`&RandomNumber1[10:9]` 是仓库常见的「缩减与」写法，等价于「这两位都是 1 才为真」，于是写/读请求各自以约 25% 的概率随机发生。

> 这个写法揭示了一个关键技巧：**一根随机总线可以「切片」成多路独立的随机源**，不需要例化多个 `c_rand`。不同位段之间相关性很弱，对功能验证足够。

再看「可复现随机」的标准写法，出自 `delayed_event_tb.sv`。它在最开始固定种子，并用 `$timeformat` 让之后所有 `$display` 的时间戳统一带单位：

[delayed_event_tb.sv:L17-L24](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event_tb.sv#L17-L24) —— 注释明确写出「种子是故意手动设的，为了在多次仿真间实现可复现性」，`$urandom(1)` 即把种子固定为 1。

`$urandom_range` 则用来产生**有范围的随机整数**，常见用途是「随机延迟几拍」：

[delayed_event_tb.sv:L122-L133](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event_tb.sv#L122-L133) —— `start` 选通信号每次复位后都「随机延迟 0~20 拍」才拉高，专门测试 DUT 对「不可预测到达时刻」的容忍度。`repeat( $urandom_range(0, 20) ) @(posedge clk200);` 是仓库的标志性写法：**把 `repeat` 和 `@(posedge clk)` 组合起来，实现「同步地等若干拍」**，比 `#延时` 更稳健，因为它和时钟沿对齐。

最后，仓库还用 `` `ifdef `` 在编译期切换激励模式，同一份 testbench 既能做「随机读写」也能做「扫空/扫满」：

[fifo_single_clock_ram_tb.sv:L93-L103](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L93-L103) —— 注释掉/打开 `TEST_SWEEP`、`TEST_FWFT` 等宏，就能在「随机测试」和「扫描测试」「标准模式」和「FWFT 模式」之间切换，不必复制多份 testbench。

#### 4.1.4 代码实践

**实践目标**：亲手感受「固定种子 = 可复现」。

**操作步骤**：

1. 新建一个最小 testbench `rand_demo_tb.sv`（**示例代码**，不是仓库原有文件），内容如下：

   ```systemverilog
   `timescale 1ns / 1ps
   module rand_demo_tb();
     initial begin
       $urandom(1);                       // 固定种子
       $display("seed=1 => %0d %0d %0d",
                $urandom_range(0,99), $urandom_range(0,99), $urandom_range(0,99));
       $finish;
     end
   endmodule
   ```

2. 用 iverilog 编译运行（参考 `scripts/iverilog_compile.bat` 的命令格式）：
   `iverilog -g2012 -o rand.vvp rand_demo_tb.sv && vvp rand.vvp`
3. 把种子从 `1` 改成 `42`，再跑一次。

**需要观察的现象**：同一颗种子两次运行打印的三个数**完全一致**；换种子后变成另一组数。

**预期结果**：种子相同时输出逐位相同（可复现）；种子不同时输出不同。具体数值取决于仿真器实现，**待本地验证**。

> 不要假装已经跑过：本实践的目的就是让你亲自确认「可复现」这件事，请真的运行并记录打印值。

#### 4.1.5 小练习与答案

**练习 1**：为什么仓库在 `delayed_event_tb.sv` 里要写 `$urandom(1)` 而不是直接用 `$urandom`？

**参考答案**：不传种子时，`$urandom` 用仿真器自带的随机种子（通常是系统时间），每次运行序列都不同，发现 bug 后无法复现。固定成 `1` 后整条序列确定，失败用例可以一键重放。

**练习 2**：`&RandomNumber1[10:9]` 和 `RandomNumber1[10] & RandomNumber1[9]` 等价吗？为什么这里能当「概率约 25% 的随机请求」用？

**参考答案**：等价。`&bus[10:9]` 是对这两位做缩减与，即两位都为 1 才为真。两位各自约 50% 为 1、近似独立，同时为 1 的概率约 25%，所以每拍约有 1/4 的概率产生一次请求，天然形成随机稀疏的读写流量。

### 4.2 异步时钟注入（Asynchronous Clock Injection）

#### 4.2.1 概念说明

很多 bug 只在「两个时钟不同源、相位关系不断漂移」时才暴露——典型的就是时钟域跨越（CDC）里的亚稳态、丢脉冲、采样错位（见 u3-l1、u3-l2）。如果你的 testbench 只用一个理想时钟，这些 bug **永远测不出来**，上板才爆。

所以成熟的 testbench 会故意做两件「危险」的事：

1. **造第二个时钟**，且它的频率和主时钟不成整数倍关系（仓库里常用 `clk200` 配 `clk33`），让两个时钟的上升沿不断相对滑动。
2. **给时钟加抖动**（jitter）：让时钟周期在每拍都随机微调一点点，模拟真实晶振和 PLL 的不完美。抖动会让「采样窗口」时宽时窄，专门压榨建立/保持时间的余量。

除了时钟，**复位**也要被「折磨」。真实系统里复位可能随机到来、可能打在事务中途。仓库用一个 `forever` 循环周期性地拉高复位，让 DUT 反复经历「正常工作→被复位→重新工作」，测试它的恢复能力。

注意一个层次区别：`sim_clk_gen`（u1-l3）是封装好的、参数化的抖动时钟发生器；而根目录 `main_tb.sv` 里有一段**就地手写**的抖动注入，只有一行，是理解原理最好的入口。本讲两者都讲。

#### 4.2.2 核心流程

**就地手写抖动时钟**的思路是「一个理想时钟 + 一根带随机延迟的影子线」：

```text
clk33a：理想慢时钟，initial/forever 按固定半周期翻转
clk33 ：clk33a 的「延迟影子」，每拍延迟一个 0..MAX 的随机量
```

关键就一行：在 `always @(*)` 里用**内嵌延迟** `#(随机延时)` 把理想时钟搬过去：

```systemverilog
always @(*) begin
  clk33 = #($urandom_range(0, 2000)*1ps) clk33a;   // 每次变化都随机延迟 0..2000 ps
end
```

每次 `clk33a` 翻转，`always @(*)` 被触发，`clk33` 在 0\~2000 ps 的随机延迟后才跟着翻转。于是 `clk33` 相对 `clk33a` 不断抖动，相当于一个真实的、带相位噪声的外部时钟。

**周期性复位**的思路是「`forever` 循环 + 固定模式」：

```text
initial 开始
  #10.2 rst = 1     // 短脉冲复位
  #5    rst = 0
  forever begin
    #9985 rst = ~rst // 每隔约 9985 ns 再翻转一次（构造随机相位的复位沿）
    #5    rst = ~rst
  end
```

这种 `#9985` 这种「不成整数倍」的间隔，配合主时钟 5 ns 周期，会让复位沿落在主时钟周期的各种不同相位上，等效于随机复位。

#### 4.2.3 源码精读

先看根目录 `main_tb.sv` 里手写的抖动注入，这是理解原理最直接的一段：

[main_tb.sv:L25-L36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L25-L36) —— 先用 `clk33a` 产生理想慢时钟（半周期 7 ns），再用 `always @(*)` + 内嵌随机延迟 `#($urandom_range(0,2000)*10ps)` 得到带抖动的 `clk33`。注释明确称它为「外部设备的异步时钟」。`cdc_strobe_tb.sv` 里几乎是同一段，只是把单位从 `10ps` 改成 `1ps`：

[cdc_strobe_tb.sv:L29-L33](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L29-L33) —— 同样的「理想时钟 + `always @(*)` 随机延迟影子」三件套，抖动幅度 0\~2000 ps。`cdc_strobe` 正是跨时钟域脉冲模块，必须用这种抖动时钟才能测出丢脉冲。

再看封装好的 `sim_clk_gen` 是怎么用同一个套路产出 `clkd` 的：

[sim_clk_gen.sv:L67-L69](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv#L67-L69) —— `clkd` 就是理想 `clk` 的随机延迟影子，最大延迟由参数 `DISTORT`（单位 ps）控制。这行和上面手写版本质完全一样，只是把抖动幅度参数化了。

`delayed_event_tb.sv` 用 `sim_clk_gen` 同时造了两个不同抖动幅度的时钟，分别驱动主逻辑和「外部设备」：

[delayed_event_tb.sv:L26-L36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event_tb.sv#L26-L36) —— 主时钟 `clk200` 用 `DISTORT=10`（轻微抖动）。

[delayed_event_tb.sv:L63-L72](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event_tb.sv#L63-L72) —— 「外部设备」时钟用 `DISTORT=1000`（剧烈抖动），刻意制造恶劣的跨域环境。

最后看周期性复位，根目录 `main_tb.sv` 和 FIFO tb 用的是同一套 `forever` 模式：

[main_tb.sv:L38-L48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L38-L48) —— 先来一个短的「上电复位」（`#10.2 rst=1; #5 rst=0`），随后 `forever` 每 `#9985` 翻转一次，把复位沿散布到主时钟的各种相位上，模拟随机到来的复位。

> 注意区分两种复位：`rst`（周期性、反复触发，测恢复能力）和 `rst_once`（只发生一次的「上电复位」，给计数器一个确定起点）。仓库几乎所有 tb 都同时保留这两根，见 [main_tb.sv:L53-L61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L53-L61)。

#### 4.2.4 代码实践

**实践目标**：直观看到抖动幅度对时钟边沿的影响。

**操作步骤**：

1. 复制 `sim_clk_gen.sv` 的例化片段到一个新 tb 里，分别例化两路：`DISTORT=10` 和 `DISTORT=1000`，把 `clk` 和 `clkd` 都 dump 到波形（参考 `scripts/iverilog_compile.bat` 注释里的 `$dumpfile`/`$dumpvars` 写法）。
2. 编译运行后在 GTKWave 里把两路 `clkd` 叠在理想 `clk` 上对比。

**需要观察的现象**：`DISTORT=10` 时抖动几乎肉眼不可见，边沿几乎贴着理想时钟；`DISTORT=1000` 时边沿明显左右「晃动」最多 1 ns。

**预期结果**：抖动幅度随 `DISTORT` 线性增大；边沿最坏延迟不超过 `DISTORT` 皮秒。具体波形**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么抖动时钟的 `always @(*)` 块里用的是「内嵌延迟 `#=...`」而不是「在敏感列表里加延时」？

**参考答案**：内嵌延迟（intra-assignment delay）`clk33 = #d expr` 的语义是「先等 `d`，再把此刻 `expr` 的值赋给 `clk33`」，正好实现「理想时钟翻转后，延迟一段随机时间再反映到影子时钟」。如果改成阻塞延时再赋值，会丢失对 `clk33a` 每次跳变的跟踪，得不到正确的抖动波形。

**练习 2**：`clk33a` 半周期是 7 ns，`clk200` 半周期是 2.5 ns，两者的周期比约是多少？为什么作者特意挑这种不成整数比的组合？

**参考答案**：`clk33a` 周期 14 ns、`clk200` 周期 5 ns，比值 14/5=2.8，不是整数比。这样两个时钟的上升沿相对位置会不断漂移、几乎不重复，能在长时间仿真里遍历各种相位关系，正是测 CDC 模块所需要的「最坏采样窗口」。

### 4.3 自校验（Self-Checking）

#### 4.3.1 概念说明

随机激励能让 bug 涌现，但跑出来成千上万拍的波形，靠人眼根本看不过来。**自校验**（self-checking）就是让 testbench 自己判断对错：在 DUT 旁边维护一个「黄金模型」（golden model / reference），每个时钟沿把 DUT 的输出和黄金模型的输出做**连续比对**，一旦不一致就立刻报错。

仓库里用了两种自校验套路，对应两类问题：

- **「复制比对」式**（duplicate-and-compare）：当你不确定自写模块和某个已知正确的实现（比如厂商 IP）哪个对，就让它们**吃同样的激励、比同样的输出**。`fifo_single_clock_ram_tb.sv` 把自写 FIFO 和 Altera SCFIFO 并排跑就是这种。
- **「计数守恒」式**：当输出难以逐拍比对（比如跨时钟域脉冲会被合法地延迟或丢弃），就统计两边各自发生了多少次事件，用**总计数**衡量有没有丢东西。`cdc_strobe_tb.sv` 在两个域各放一个计数器就是这种。

报错的手段是 SystemVerilog 的几个系统任务：

- `$error("...")`：打印一条错误并标记仿真失败（ severity 高于 `$display`）。
- `$display` / `$realtime` / `$timeformat`：打印带时间戳的诊断信息，方便定位失败时刻。
- 一个 `success` 寄存器：用 `always_ff` 在检测到不一致时**锁存**为 0，仿真结束时看它是 0 还是 1 就知道整轮有没有出错——这比每拍都 `$error` 刷屏更干净。

> 黄金模型可以是一个独立的参考实现，也可以是「同一个 DUT 的另一种配置」。FIFO tb 用的就是后者：同一段 RTL，一个配 FWFT、一个配普通模式，比对它们在等价条件下是否一致。

#### 4.3.2 核心流程

「复制比对」式自校验的骨架：

```text
激励源（随机） ─┬─> DUT(待测) ─────────────┐
                └─> 参考实现(已知正确) ──────┤
                                            v
                                   连续比对 od_dut == od_ref ?
                                            │ 不等
                                            v
                                   success <= 0  (锁存一次即永久标记)
```

「计数守恒」式自校验的骨架：

```text
源域：每发一个脉冲，cnt_src++
   │ (跨域)
   v
目的域：每收一个脉冲，cnt_dst++

仿真结束时比较 cnt_src 与 cnt_dst（允许已知延迟）
```

两者的共同点是：**把「正确性判断」从人眼转移到电路逻辑**，让 testbench 变成一个可以放进回归脚本、每次提交自动跑、自动报红绿灯的「测试用例」。

#### 4.3.3 源码精读

先看 FIFO testbench 的「复制比对」自校验。它把自写 FIFO（`FF1`）和厂商 FIFO（`FF2`）的输出、满标志、空标志三路都做连续比对：

[fifo_single_clock_ram_tb.sv:L289-L303](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L289-L303) —— `outputs_equal`、`empty_equal`、`full_equal` 三根 `assign` 线持续比对两路 FIFO 的对应输出；`outputs_equal` 还对 FWFT 模式下厂商 FIFO 的一拍额外缓冲做了显式豁免（注释说「跳过轻微的不连续」）。

然后用一个 `success` 寄存器锁存任何一次不一致：

[fifo_single_clock_ram_tb.sv:L305-L314](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L305-L314) —— `success` 初值 1，只要 `~outputs_equal` 出现一拍就**永久**拉到 0（没有再置 1 的路径）。仿真结束时只看这根线就能判定整轮随机测试有没有失配。这正是「锁存式报错」的典范写法。

为了让两路 FIFO 吃到**完全相同**的随机激励，作者把 `RandomNumber1` 的位段同时接到两个 FIFO 的请求和数据上（见 [fifo_single_clock_ram_tb.sv:L162-L166](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L162-L166) 与 L267-L277），确保任何差异都来自 FIFO 实现本身而非输入不同。

再看 `cdc_strobe_tb.sv` 的「计数守恒」自校验。它无法逐拍比对（跨域脉冲会被合法延迟），于是分别在两个时钟域统计脉冲数：

[cdc_strobe_tb.sv:L129-L141](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L129-L141) —— `strb1_cntr` 在 `clk200` 域每收到一个源脉冲 `strb1` 就加 1；`strb2_cntr` 在抖动的 `clk33` 域每收到一个目的脉冲 `strb2` 就加 1。跑完一轮看两个计数器的差，就知道 `cdc_strobe` 丢了多少脉冲——而丢脉冲正是这个模块在输入过密时的预期失败模式（见 u3-l2）。

`delayed_event_tb.sv` 则示范了**带时间戳的诊断打印**，配合 `$timeformat`：

[delayed_event_tb.sv:L111-L119](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event_tb.sv#L111-L119) —— 每次复位后用 `$display("[T=%0t] ...", $realtime, ...)` 打印当时的时间和关键信号，`%0t` 配合前面的 `$timeformat(-9,3," ns")` 会输出形如 `T=105.000 ns` 的人类可读时间戳，失败时定位极快。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：为 `edge_detect` 写一个**自校验 testbench**——用随机输入制造大量跳变，让 testbench 自动用黄金模型比对 `rising`/`falling`/`both` 三路输出，发现不一致即 `$error`。

> 为什么新建而不是改仓库已有的 `edge_detect_tb.sv`？因为那份 testbench 已经**过时**：它例化的时钟分频器叫 `ClkDivider`（旧名，现仓库是 `clk_divider`）、又给 `edge_detect` 接了 `.nrst` 端口，而当前的 `edge_detect` 复位端口是异步的 `.anrst`（见 [edge_detect.sv:L44-L45](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L44-L45)）。所以它对当前代码已无法直接编译。本实践写一份全新的、能跑的自校验版本。

**第一步：理解 DUT 和黄金模型的关系。**

`edge_detect`（`REGISTER_OUTPUTS=0`，即默认组合输出）的核心是：把输入打一拍得到 `in_d`，然后

- `rising  = in & ~in_d`
- `falling = ~in & in_d`
- `both    = rising | falling`

见 [edge_detect.sv:L54-L70](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L54-L70)。黄金模型只要在 testbench 里也维护一个「输入的上一拍 `in_prev`」，用同样三条公式算出期望值即可。

**第二步：写 testbench（示例代码，新建文件 `edge_detect_selfcheck_tb.sv`）。**

```systemverilog
`timescale 1ns / 1ps
module edge_detect_selfcheck_tb();

  // ---- 时钟：手写 200 MHz ----
  logic clk;
  initial begin
    #0 clk = 1'b0;
    forever #2.5 clk = ~clk;
  end

  // ---- 可复现随机 + 统一时间格式 ----
  initial begin
    $urandom(1);                       // 固定种子，失败可复现
    $timeformat(-9, 3, " ns");
  end

  // ---- 异步复位（高有效 rst -> 低有效 anrst），一次性上电复位 ----
  logic rst, anrst;
  initial begin
    #0  rst = 1'b1;     // 上电即复位
    #20 rst = 1'b0;     // 20 ns 后释放
  end
  assign anrst = ~rst;  // edge_detect 用低有效异步复位 anrst

  // ---- 随机激励：每拍以约 50% 概率翻转输入 ----
  logic in_sig = 1'b0;
  always_ff @(posedge clk)
    in_sig <= ($urandom_range(0,1)) ? ~in_sig : in_sig;

  // ---- DUT ----
  logic rising, falling, both;
  edge_detect #(.WIDTH(1), .REGISTER_OUTPUTS(1'b0)) dut (
    .clk(clk), .anrst(anrst),
    .in(in_sig),
    .rising(rising), .falling(falling), .both(both)
  );

  // ---- 黄金模型：同样的“输入打一拍 + 三条公式” ----
  logic in_prev = 1'b0;
  always_ff @(posedge clk or negedge anrst) begin
    if (~anrst) in_prev <= 1'b0;
    else        in_prev <= in_sig;
  end
  logic g_rising, g_falling, g_both;
  assign g_rising  = anrst & ( in_sig & ~in_prev);
  assign g_falling = anrst & (~in_sig &  in_prev);
  assign g_both    = anrst & (g_rising | g_falling);

  // ---- 自校验：连续比对，失配即 $error ----
  logic success = 1'b1;
  always_ff @(posedge clk) begin
    if (anrst) begin                    // 复位释放后才检查，避开初值 X
      if (rising  !== g_rising) begin
        success <= 1'b0;
        $error("[T=%0t] rising  mismatch: dut=%b gold=%b", $realtime, rising,  g_rising);
      end
      if (falling !== g_falling) begin
        success <= 1'b0;
        $error("[T=%0t] falling mismatch: dut=%b gold=%b", $realtime, falling, g_falling);
      end
      if (both    !== g_both) begin
        success <= 1'b0;
        $error("[T=%0t] both    mismatch: dut=%b gold=%b", $realtime, both,    g_both);
      end
    end
  end

  // ---- 跑一段时间后收尾 ----
  initial begin
    #5000;
    $display("=== TEST %0s ===", success ? "PASSED" : "FAILED");
    $finish;
  end
endmodule
```

**操作步骤**：

1. 把上面这段保存为 `edge_detect_selfcheck_tb.sv`（和 `edge_detect.sv` 放同一目录）。
2. 参考 `scripts/iverilog_compile.bat` 的命令格式编译运行：
   `iverilog -g2012 -o sim.vvp edge_detect_selfcheck_tb.sv edge_detect.sv && vvp sim.vvp`
   （`-g2012` 打开 SystemVerilog-2012 支持，这是 `$urandom` 等必需的，见 u1-l3。）
3. 把 `$urandom(1)` 的种子换成几个不同值（如 `7`、`42`），各跑一轮，看是否都 PASSED。

**需要观察的现象**：

- 控制台不应出现任何 `mismatch` 行；末尾打印 `=== TEST PASSED ===`。
- 把波形 dump 出来，应能看到每当 `in_sig` 由 0→1，`rising` 在同一拍（组合模式）拉高 1 拍；由 1→0 时 `falling` 拉高；任一跳变 `both` 都拉高。

**预期结果**：因为黄金模型和 DUT 用的是**完全等价**的逻辑，所有种子下都应 PASSED。`success` 全程保持 1。

**故意制造一次失败以验证检查器有效**：把黄金模型里 `g_rising` 的公式从 `in & ~in_prev` 故意改成 `in & in_prev`，重新跑。现在应看到大量 `rising mismatch` 报错、末尾打印 `FAILED`。这证明你的自校验逻辑确实在起作用——**一个从不报错的检查器可能根本没在检查**，所以这一步很重要。

> 说明：以上为示例代码，未在本环境实际运行；具体打印数值与是否需调整 `#5000` 时长，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么自校验比对块要加 `if (anrst)` 这个门控，而不是从头到尾都比？

**参考答案**：复位期间 DUT 的内部 `in_d` 和黄金模型的 `in_prev` 都还在建立，输出可能含 `X`；此时比对会刷出大量假错误。用 `if (anrst)` 把检查限制在「复位已释放」之后，避开初值未定义阶段，只检查真正的工作区间。

**练习 2**：FIFO testbench 用 `success <= 1'b0` 锁存，而不是每拍都 `$error`，这样做有什么好处？

**参考答案**：锁存式只需一个触发器记录「是否曾经出错」，仿真结束看一眼即可，输出干净；而每拍 `$error` 会在持续失配时刷屏、淹没真正有用的信息。两者可以结合：用 `success` 做总判定，再用少量带时间戳的 `$error`/`$display` 记录**第一次**失配用于定位。

## 5. 综合实践

把本讲三个模块（随机激励 + 异步时钟注入 + 自校验）串成一个**升级版** `edge_detect` 自校验 testbench，要求同时满足：

1. **随机激励**：输入 `in_sig` 用 `$urandom_range` 随机翻转，并固定 `$urandom(42)` 种子保证可复现。
2. **异步时钟注入**：主时钟用 `sim_clk_gen`（`DISTORT=50`）产生带轻微抖动的 `clk`，并增加一根**周期性复位** `rst`（仿照 [main_tb.sv:L44-L48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L44-L48) 的 `forever` 写法），让 DUT 在仿真中途被反复复位。
3. **自校验**：沿用 4.3.4 的黄金模型与 `success` 锁存，但要把比对门控改成「复位释放后」(`if (anrst)`)，并在每次复位释放时 `$display` 一条带时间戳的提示。

**验收标准**：多种种子下仿真末尾都打印 `PASSED`；周期性复位期间 `success` 不被误触发（因为门控跳过了复位期）。如果在这一步出现了真实的 mismatch，恭喜——你刚刚用「随机 + 异步 + 自检」三件套抓出了一个边界 bug，请用固定种子把它复现并定位。

> 这个综合实践把 `edge_detect` 当成「替身」：真正的价值在于这套**模板**。把你以后写的任何时序模块套进同一个 testbench 骨架（换 DUT、换黄金模型），就能立刻得到一个能长期回归、能自动报红的验证用例。

## 6. 本讲小结

- **随机激励**用「不可预测的输入」覆盖人脑想不到的角落；仓库提供两种随机源：仿真专用的 `$urandom`/`$urandom_range` 和可综合的 `c_rand` LCG 模块，前者每拍现取、后者例化成一根可共享的随机总线。
- **可复现性**靠固定种子实现：`$urandom(SEED)` 让同一段随机序列可一键重放，是「发现 bug 后能定位」的前提。
- **异步时钟注入**故意造第二个不同频时钟并加抖动（`always @(*) clk = #(随机延迟) clk_ideal` 或封装的 `sim_clk_gen`），专门测出只在跨时钟域才暴露的 bug。
- **周期性复位**用 `forever` 把复位沿散布到各种相位，测试 DUT 的恢复能力；与一次性 `rst_once` 配合使用。
- **自校验**把「对错判断」交给电路：用黄金模型连续比对 DUT 输出，失配即 `$error`；FIFO tb 用「复制比对」，cdc_strobe tb 用「计数守恒」，二者都把结果锁存进一根 `success` 标志。
- 一个从不报错的自校验器未必有效，**故意改坏黄金模型**确认它能报错，是验证「检查器本身」的必要步骤。

## 7. 下一步学习建议

- **承接本讲的实战**：u7-l2「时序约束与收敛」会讲 `set_false_path` 等约束——本讲里 `cdc_strobe_tb` 用的抖动时钟之所以能合法地丢/不丢脉冲，正是靠这些约束在真实综合时豁免跨域路径，建议接着读。
- **深入 CDC**：本讲的 `cdc_strobe_tb` 是 u3-l2「跨时钟域单周期脉冲」的配套 testbench，如果你还没读过 `cdc_strobe.sv` 本身，现在正好带着「计数守恒」的视角回去看它的丢脉冲速率限制。
- **把模板用起来**：选一个你感兴趣的模块（如 `moving_average`、`adder_tree`），用本讲的「随机 + 自检」骨架为它写一个回归 testbench，作为本单元 u7-l4「综合实战」的预热。
- **进阶阅读**：仓库 `Advanced Synthesis Cookbook/` 下有大量 `_tb.sv`（如 `storage/ready_skid_tb.sv`、`crc/crc32c_tb.sv`），它们用到了本讲全部三件套的更复杂变体，是进阶练习的宝库。
