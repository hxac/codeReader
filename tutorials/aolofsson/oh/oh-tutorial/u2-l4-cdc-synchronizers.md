# 跨时钟域同步原语

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「亚稳态（metastability）」是什么，以及为什么跨时钟域（CDC, Clock Domain Crossing）的信号不能直接连。
- 区分**电平同步**（`oh_dsync`）、**复位同步**（`oh_rsync`）和**脉冲同步**（`oh_pulse2pulse`）三件事各自解决的问题。
- 读懂边沿检测家族 `oh_edge2pulse` / `oh_rise2pulse` / `oh_fall2pulse`，并能用「同步 + 边沿检测」组合把慢时钟域的脉冲安全地搬进快时钟域。
- 看懂 OH! 仓库里 `gpio`、`elink` 等真实模块是怎样使用这些原语的。

本讲承接 [u2-l2 时序原语：触发器家族](u2-l2-sequential-flops.md)——你需要已经理解 D 触发器、异步复位、非阻塞赋值。本讲把单个触发器升级成「一串触发器」，用来对付芯片里最难缠的一类问题：信号从一个时钟域跳到另一个时钟域。

## 2. 前置知识

### 2.1 什么是时钟域

一块芯片里通常不止一个时钟。比如 CPU 跑 500 MHz，而一个 SPI 外设只有 10 MHz，一条高速链路（elink）又有自己的恢复时钟。**同一个时钟节拍下一起动作的触发器集合，叫一个「时钟域（clock domain）」**。

一个信号从域 A（`clkin`）的触发器输出，直接连到域 B（`clkout`）的触发器输入，就构成了一次「跨时钟域」。问题是：`clkout` 的采样沿可能出现在 A 信号正在翻转的那一瞬间。

### 2.2 亚稳态：触发器的「犹豫」

回顾 [u2-l2](u2-l2-sequential-flops.md) 讲过的建立/保持时间（setup/hold）。如果一个触发器在采样窗口内输入还在变，它就**违反了建立/保持时间**，输出不会干净地停在 0 或 1，而是可能：

1. 停在一个半生不熟的中间电平（亚稳态电压）；
2. 经过一段**不确定的时间** \(t_r\)（resolution time）后才随机塌缩到 0 或 1。

这段不确定的塌缩时间就是灾难的根源：下一个 `clkout` 沿可能在它还没塌缩好时就又采了一次，于是这个错误值会像传染病一样被后续逻辑扇出，整块芯片的行为就不可预测了。

> 直觉比喻：你让一个人在旋转的硬币还在空中旋转时喊出「正面还是反面」。他可能盯半天才能喊出来——这段时间里你再催他，他就会乱喊。

### 2.3 同步器：用时间换可靠性

工业界标准做法是**串一串触发器**——「同步器（synchronizer）」。它的魔法不是消除亚稳态，而是**给亚稳态留出足够的塌缩时间**，让概率降到几乎不可能。两级触发器同步器的平均无故障时间（MTBF）大致满足：

\[
\text{MTBF} \;\propto\; \frac{e^{\,t_r/\tau}}{f_{\text{clk}} \cdot f_{\text{data}} \cdot T_0}
\]

其中 \(t_r\) 是第一级留给亚稳态的塌缩时间，\(\tau\)、\(T_0\) 是工艺常数。多一级触发器，\(t_r\) 就多一个时钟周期，MTBF 随 \(e^{t_r/\tau}\) **指数级**变好。所以「两级」是底线，「三级」用于高频或长寿命场景。OH! 的默认值就是两级（`SYNCPIPE=2`）。

### 2.4 电平、脉冲，谁更难搬

跨时钟域要搬的东西分两类，难度天差地别：

- **电平信号（level）**：持续时间长（比如一个持续有效的「忙」标志）。只要用同步器打两拍，输出迟早会稳定成正确值——顶多延迟几个周期。
- **脉冲信号（pulse）**：只在一个时钟周期内有效。如果源时钟比目标时钟**快**，目标可能根本采不到这个脉冲（脉冲在两个采样沿之间冒了一下又消失了）。

所以搬脉冲要更小心，本讲会分别给出「慢→快」和「通用」两套办法。

## 3. 本讲源码地图

本讲涉及的关键文件全部在 `stdlib/rtl/` 下，都是几十行的小模块：

| 文件 | 作用 | 属于哪个最小模块 |
|------|------|------------------|
| `oh_dsync.v` | N 级打拍**电平**同步器 | 同步器 |
| `oh_rsync.v` | **复位**信号同步器（异步置位、同步释放） | 同步器 |
| `oh_edge2pulse.v` | 任一边沿 → 单周期脉冲（同域） | 边沿检测 |
| `oh_rise2pulse.v` | 上升沿 → 单周期脉冲（同域） | 边沿检测 |
| `oh_fall2pulse.v` | 下降沿 → 单周期脉冲（同域） | 边沿检测 |
| `oh_pulse2pulse.v` | 跨时钟域**脉冲**搬运（通用） | 脉冲转换 |

另外会引用两个「真实使用现场」作为佐证：`gpio/hdl/gpio.v`（同步 GPIO 输入再检测边沿）和 `elink/hdl/erx_clocks.v`（同步复位与 PLL 锁定信号）。

> 重要区分：`oh_edge2pulse` / `oh_rise2pulse` / `oh_fall2pulse` **本身不是同步器**——它们只在自己的时钟域里干活。它们常被接在同步器**之后**，把已经稳定的电平翻沿转成单周期脉冲。别把这个顺序搞反。

## 4. 核心概念与源码讲解

### 4.1 同步器：oh_dsync 与 oh_rsync

#### 4.1.1 概念说明

`oh_dsync` 解决最朴素的需求：**把一个电平信号从别的时钟域安全地搬进本域**。它就是一串依次相接的 D 触发器（本讲称之为「打拍」），第一级「吃下」亚稳态，第二级及以后「等它塌缩完」再输出。

`oh_rsync` 解决一个更具体但同样致命的问题：**复位信号的同步**。注意它的注释写得很精炼——「async assert, sync deassert」（异步生效、同步释放）。原因见 4.1.5 的练习：复位必须立刻抓住整个芯片（异步生效），但释放必须对齐时钟（同步释放），否则释放沿本身又是一个会引发亚稳态的冒险。

#### 4.1.2 核心流程

`oh_dsync` 的行为（默认 `TARGET="DEFAULT"`、`SYNCPIPE=2`）：

```
din ──► [DFF] ──► [DFF] ──► dout
        第1级     第2级
        (吃亚稳态) (输出稳定值)

每个 clk 上升沿：整条移位寄存器向左移一位，din 进入第1级
异步 nreset=0：整条清零
```

`oh_rsync` 的行为：

```
nrst_in ──(async)──► [DFF] ──► [DFF] ──► nrst_out
                    第1级     第2级

nrst_in=0  : 立刻（异步）把整条清零 → nrst_out 立即为 0
nrst_in=1  : 每个 clk 向左移入一个 '1'，SYNCPIPE 拍后 nrst_out 才变 1
```

#### 4.1.3 源码精读

先看 `oh_dsync` 的可综合实现（默认分支）：

[stdlib/rtl/oh_dsync.v:L20-L31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dsync.v#L20-L31) —— 这段是同步器的全部内核：声明一根 `SYNCPIPE+1` 位宽的移位寄存器 `sync_pipe`，异步低复位，时钟上升沿把 `din` 移进最低位，并把 `dout` 选在 `sync_pipe[SYNCPIPE-1]` 上（即第二级，默认两级同步）。

注意三个细节：

1. `reg [SYNCPIPE:0] sync_pipe;` 声明的是 `SYNCPIPE+1` 位（例如 `SYNCPIPE=2` 时是 `[2:0]`，3 位）。最低位是第 1 级，第 `SYNCPIPE-1` 位是第 2 级（即 `dout`），最高位 `sync_pipe[SYNCPIPE]` 留给 `DELAY` 选项——见下条。
2. 移位语句 `sync_pipe <= {sync_pipe[SYNCPIPE-1:0], din}` 是标准的「拼接左移」写法，等价于 `sync_pipe = sync_pipe << 1 | din`，符合 [u2-l2](u2-l2-sequential-flops.md) 讲过的非阻塞赋值约定。
3. `assign dout = (DELAY & sync_pipe[SYNCPIPE]) | (~DELAY & sync_pipe[SYNCPIPE-1]);` 让 `DELAY` 这个参数在非 0 时多取一级（第 3 级）输出，等效于「故意多等一拍」。`DELAY` 的注释写的是 `random delay`，本意是给仿真 testbench 注入随机延迟、模拟最坏情况下的亚稳态传播——它是一个**仿真可调**的旋钮，不是可综合的随机源。

完整的模块/参数/端口声明在这里：

[stdlib/rtl/oh_dsync.v:L8-L18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dsync.v#L8-L18) —— 端口只有 `clk / nreset / din / dout`，参数有 `SYNCPIPE`（级数）、`DELAY`（额外一拍/仿真随机延迟）、`TARGET`（soft/hard 切换，见下）。

和 [u2-l2](u2-l2-sequential-flops.md) 一致，`oh_dsync` 也走 soft/hard 双实现：`TARGET=="DEFAULT"` 走上面那段可综合 RTL，否则例化硬核 `asic_dsync`：

[stdlib/rtl/oh_dsync.v:L32-L40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dsync.v#L32-L40) —— ASIC 流程会把这段替换成工艺库里的专用同步器单元（通常带更高增益、更快塌缩），soft/hard 的动机详见 [u1-l4](u1-l4-coding-style.md)。

再看 `oh_rsync`。它的结构与 `oh_dsync` 几乎镜像，但有两个关键差别：

[stdlib/rtl/oh_rsync.v:L18-L28](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_rsync.v#L18-L28) —— 复位同步器的内核。差别一：敏感列表是 `posedge clk or negedge nrst_in`，于是 `nrst_in` 一拉低就**立刻**把 `sync_pipe` 清零（异步生效）；差别二：正常分支移入的是常数 `1'b1` 而不是 `din`——`{sync_pipe[SYNCPIPE-2:0], 1'b1}`。于是 `nrst_in` 一旦释放，需要整整 `SYNCPIPE` 个时钟，第一个 `1` 才能移到 `nrst_out`，从而保证**释放是同步对齐的**。

端口和声明：

[stdlib/rtl/oh_rsync.v:L8-L16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_rsync.v#L8-L16) —— 输入 `clk / nrst_in`，输出 `nrst_out`；参数 `SYNCPIPE=2`、`TARGET="DEFAULT"`。

**真实使用现场 1：elink 的复位与锁相环锁定**。高速链路接收侧 `erx_clocks.v` 用了两处 `oh_rsync` 和一处 `oh_dsync`：

[elink/hdl/erx_clocks.v:L123-L130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L123-L130) —— `rsync_io` / `rsync_core` 分别把复位同步到 IO 时钟域和核心时钟域（这两个域频率/相位不同，复位必须各自同步释放）。

[elink/hdl/erx_clocks.v:L202-L204](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L202-L204) —— 注释明写 `two clock synchronizer for lock signal`，把 PLL 的 `locked` 标志同步进 `sys_clk` 域再用。这正是电平同步的典型用法。

**真实使用现场 2：异步 FIFO 的复位**也走 `oh_rsync`：[stdlib/rtl/oh_fifo_cdc.v:L46-L48](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_cdc.v#L46-L48)（跨时钟域 FIFO 会在 [u3-l2](u3-l2-fifo-design.md) 详讲）。

#### 4.1.4 代码实践

**实践目标**：亲眼看见两级同步器「延迟但可靠」的特性，并对比 `DELAY` 参数的效果。

**操作步骤**（这是一个源码阅读 + 自建小型 testbench 的实践，因为仓库没有为这几个原语提供专用 `dut_*.v`）：

1. 在任意工作目录新建下面的测试平台（**示例代码**，非仓库原有文件）：

   ```verilog
   `timescale 1ns/1ps
   module tb_dsync;
     reg  clk = 0, nreset = 0, din = 0;
     wire d0, d1;   // d0: DELAY=0(2级); d1: DELAY=1(3级)
     always #5 clk = ~clk;            // 100MHz

     oh_dsync #(.SYNCPIPE(2), .DELAY(0)) u0 (.clk(clk), .nreset(nreset), .din(din), .dout(d0));
     oh_dsync #(.SYNCPIPE(2), .DELAY(1)) u1 (.clk(clk), .nreset(nreset), .din(din), .dout(d1));

     initial begin
       $dumpfile("dsync.vcd"); $dumpvars(0, tb_dsync);
       #20 nreset = 1;
       #12 din = 1;     // 在非沿时刻翻转，制造近似亚稳态窗口
       #10 din = 0;
       #50 $finish;
     end
   endmodule
   ```

2. 直接用 iverilog 编译这两个原语（绕开仓库 `libs.cmd` 的历史路径问题，见 [u1-l3](u1-l3-simulation-setup.md) 的排错结论）：

   ```bash
   iverilog -g2005 -o dsync.vvp tb_dsync.v $OH_HOME/stdlib/rtl/oh_dsync.v
   vvp dsync.vvp
   gtkwave dsync.vcd        # 用波形验证
   ```

**需要观察的现象**：`din` 翻转后，`d0` 在约 2 个 `clk` 周期后跟随；`d1` 比 `d0` 再晚 1 个周期（因为多取了一级）。

**预期结果**：`din` 的每一个变化，`d0` 滞后 2 拍、`d1` 滞后 3 拍出现；二者最终电平一致。若你的工具对 `DELAY` 的位宽解释不同（`DELAY` 是整数而非布尔），把 `1` 改成 `1'b1` 或具体非零值再核对——这一处工具行为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `oh_dsync` 默认是两级（`SYNCPIPE=2`）而不是一级？

**参考答案**：一级触发器虽然能「挡一下」，但亚稳态值会直接被下一级组合逻辑采样，几乎没给塌缩留时间，MTBF 改善有限。两级把第一级隔离出来，让它拥有整整一个时钟周期 \(t_r\) 去塌缩，第二级才采样到稳定值——MTBF 随 \(e^{t_r/\tau}\) 指数提升。所以两级是工程底线。

**练习 2**：`oh_rsync` 为什么「异步生效、同步释放」，而不是反过来「同步生效、异步释放」？

**参考答案**：复位必须**立刻**抓住全芯片，不能等时钟——否则未复位的部分会跑飞，所以「生效」要异步。但「释放」如果也是异步、且落在某个触发器的建立/保持窗口里，那个触发器就会亚稳态，相当于在全芯片同时撒播亚稳态。把释放对齐到时钟（经过 `SYNCPIPE` 拍），保证所有触发器在同一拍干净地脱离复位。

---

### 4.2 边沿检测：oh_edge2pulse / oh_rise2pulse / oh_fall2pulse

#### 4.2.1 概念说明

很多时候我们关心的不是「信号是 0 还是 1」，而是「信号**刚刚**从 0 变成了 1」。比如 GPIO 引脚上检测一个按键按下（上升沿）、或者检测同步过来的电平**第一次**变高。**边沿检测电路**把这种「翻沿事件」转换成一个**单时钟周期**的脉冲，方便后续逻辑当「事件」处理。

这三个模块都是**同一个时钟域内**的纯边沿检测器，不是同步器。它们的套路完全一样：先把输入打一拍存成「上一拍的值」，再用组合逻辑比较「当前值」和「上一拍的值」。

#### 4.2.2 核心流程

```
in ──┬──────────────────────────┐
     │                          │
     ▼                          ▼
  [DFF: in_reg]              (当前 in)
     │                          │
     └──── 比较(异或/与) ◄───────┘
              │
              ▼
            out（仅翻沿那 1 拍为 1）
```

三种「比较」对应三种边沿：

| 模块 | 组合逻辑 | 触发条件 |
|------|----------|----------|
| `oh_edge2pulse` | `in_reg ^ in` | 上升沿**或**下降沿（任意翻沿） |
| `oh_rise2pulse` | `in & ~in_reg` | 仅上升沿（现在 1、上一拍 0） |
| `oh_fall2pulse` | `~in & in_reg` | 仅下降沿（现在 0、上一拍 1） |

#### 4.2.3 源码精读

`oh_edge2pulse` 是三者里最通用的（任意边沿）：

[stdlib/rtl/oh_edge2pulse.v:L19-L25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_edge2pulse.v#L19-L25) —— `in_reg` 存「上一拍的 `in`」，`out = in_reg ^ in`。异或的特性是「两输入不同则 1」：只要 `in` 这一拍和上一拍不一样（无论 0→1 还是 1→0），`out` 就在这一拍为 1，下一拍又归 0——正好是一个单周期脉冲。

`oh_rise2pulse` 只把组合逻辑换成「与」：

[stdlib/rtl/oh_rise2pulse.v:L18-L24](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_rise2pulse.v#L18-L24) —— `out = in & ~in_reg`。只有当**当前 `in` 为 1 且上一拍 `in_reg` 为 0** 时才成立，即上升沿。

`oh_fall2pulse` 把操作数反过来：

[stdlib/rtl/oh_fall2pulse.v:L19-L25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fall2pulse.v#L19-L25) —— `out = ~in & in_reg`，下降沿脉冲。

三个模块都用 `parameter N` 参数化位宽，可对一根总线逐位做边沿检测（每位独立）。时序部分与 [u2-l2](u2-l2-sequential-flops.md) 的 `oh_dffq` 完全同构：`posedge clk or negedge nreset`、异步低复位、非阻塞赋值。

> 为什么边沿检测必须在**目标时钟域**里做、并且接在同步器**之后**？因为边沿检测器本身就是触发器，它的 `in_reg` 必须由目标时钟采样；如果直接喂一个未同步的异域信号，`in_reg` 就会亚稳态，比较出来的「脉冲」也就不可信。正确顺序永远是：**先 `oh_dsync` 进域，再 `oh_rise2pulse` 取沿**。

**真实使用现场：GPIO 输入**。`gpio.v` 正是这套范式的教科书例子——先用 `oh_dsync` 把外部引脚同步进来，再存一拍准备做边沿/中断检测：

[gpio/hdl/gpio.v:L127-L133](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L127-L133) —— 注意例化写成 `oh_dsync oh_dsync[N-1:0] (...)`，这是 Verilog 的**数组例化（instance array）**：一次性为 `N` 位 `gpio_in` 各生成一个独立的 `oh_dsync`，输出汇成 `gpio_in_sync[N-1:0]`。紧接着 `data_old <= gpio_in_sync` 把同步后的值再存一拍，下游就能用 `gpio_in_sync ^ data_old` 之类的式子还原出上升/下降沿事件（这部分逻辑会在 [u6-l2 GPIO 模块全解析](u6-l2-gpio-module.md) 详讲）。

#### 4.2.4 代码实践

**实践目标**：用一个 testbench 同时实例化三个边沿检测器，观察它们对同一段输入的不同响应。

**操作步骤**：

```verilog
`timescale 1ns/1ps
module tb_edge;
  reg clk = 0, nreset = 0, in = 0;
  wire e, r, f;
  always #5 clk = ~clk;
  oh_edge2pulse uE (.clk(clk),.nreset(nreset),.in(in),.out(e));
  oh_rise2pulse uR (.clk(clk),.nreset(nreset),.in(in),.out(r));
  oh_fall2pulse uF (.clk(clk),.nreset(nreset),.in(in),.out(f));
  initial begin
    $dumpfile("edge.vcd"); $dumpvars(0, tb_edge);
    #20 nreset = 1;
    #12 in = 1;   // 上升沿
    #40 in = 0;   // 下降沿
    #40 $finish;
  end
endmodule
```

编译：`iverilog -g2005 -o edge.vvp tb_edge.v $OH_HOME/stdlib/rtl/oh_edge2pulse.v $OH_HOME/stdlib/rtl/oh_rise2pulse.v $OH_HOME/stdlib/rtl/oh_fall2pulse.v && vvp edge.vvp`。

**需要观察的现象**：`in` 上升那拍，`e` 和 `r` 同时为 1（`f` 为 0）；`in` 下降那拍，`e` 和 `f` 同时为 1（`r` 为 0）；其它拍三者皆 0。

**预期结果**：`e = r | f` 在所有时刻成立，验证「任意沿 = 上升沿 ∪ 下降沿」。每个脉冲都恰为 1 个 `clk` 周期宽。若观察到复位期间 `e` 出现毛刺，那是 `in_reg` 复位值与 `in` 初值不同导致的合法翻沿——可把 `in` 在复位释放后再驱动以避免。

#### 4.2.5 小练习与答案

**练习 1**：只看表达式 `in_reg ^ in`，它能区分上升沿和下降沿吗？

**参考答案**：不能。异或只反映「变了没有」，不反映方向。要区分方向必须用 `in & ~in_reg`（上升）或 `~in & in_reg`（下降）。这也正是 `oh_rise2pulse` / `oh_fall2pulse` 单独存在的理由。

**练习 2**：如何把 `oh_rise2pulse` 一行改成 `oh_fall2pulse`？

**参考答案**：把输出赋值从 `out = in & ~in_reg;` 改成 `out = ~in & in_reg;`——也就是把「当前值」和「上一拍值」的取反对象对调。对比 [oh_rise2pulse.v:L24](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_rise2pulse.v#L24) 与 [oh_fall2pulse.v:L25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fall2pulse.v#L25) 即可印证。

---

### 4.3 脉冲转换：oh_pulse2pulse

#### 4.3.1 概念说明

4.2 的边沿检测只能处理**同域**信号。但真正的难题是：**把源时钟域的一个单周期脉冲，搬到目标时钟域，还是输出一个单周期脉冲**——而且两个时钟频率任意、相位无关。

如果直接用 `oh_dsync` 同步一个脉冲，源时钟快、目标时钟慢时，脉冲会在两次采样之间冒一下就消失，目标域**根本看不见**。`oh_pulse2pulse` 用一个经典套路解决这个问题：**先把脉冲「展」成一个会长期保持的电平（toggle），同步这个电平，再在目标域用边沿检测还原成脉冲**。

#### 4.3.2 核心流程

`oh_pulse2pulse` 的三段式数据流：

```
(clkin 域)                              (clkout 域)
din 脉冲 ──► 翻转 toggle_reg ──► toggle 电平
                                          │
                           ┌──────────────┘
                           ▼
                    oh_dsync 同步 ──► toggle_sync 电平
                                          │
                           ┌──────────────┘
                           ▼
                  异或还原 ──► dout 单周期脉冲
```

1. **脉冲 → 电平**：每来一个 `din` 脉冲，就把 `toggle_reg` 翻一次（0→1→0→1…）。于是「脉冲次数」被编码成「电平翻转次数」，而电平是长期保持的，不会被采样错过。
2. **电平同步**：把 `toggle` 用 `oh_dsync` 同步进 `clkout` 域，得到 `toggle_sync`。
3. **电平 → 脉冲**：在 `clkout` 域里，比较 `toggle_sync` 的当前值和上一拍（`pulse_reg`），不同则说明刚翻过一次沿——输出一个单周期脉冲。

第 3 步用的 `dout = pulse_reg ^ toggle_sync`，本质就是 4.2 讲的「任意边沿检测」（异或），只是写法内联了。

#### 4.3.3 源码精读

先看模块顶部和那条**很关键的警告注释**：

[stdlib/rtl/oh_pulse2pulse.v:L1-L20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_pulse2pulse.v#L1-L20) —— 第 3 行注释用三个感叹号强调：`"din" pulse width must be 2x greater than clkout width`（`din` 脉冲宽度必须大于 `clkout` 周期的两倍）。原因见 4.3.5 练习 1：这是个**开环**转换器（没有应答反馈），源端连续两次翻转若被目标端漏采样，两次脉冲都会丢；2 倍是作者给的保守约束。端口分属两个域：`clkin/nrstin/din` 是源域，`clkout/nrstout/dout` 是目标域。

第 1 段：脉冲转翻转电平。

[stdlib/rtl/oh_pulse2pulse.v:L28-L34](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_pulse2pulse.v#L28-L34) —— `toggle = din ? ~toggle_reg : toggle_reg`：有脉冲就翻转、没有就保持；`toggle_reg` 在 `clkin` 上升沿更新。注意这里复位用的是 `~nrstin`（按位取反，1 位信号上等价于 `!nrstin`），与 [u2-l2](u2-l2-sequential-flops.md) 讲的低有效复位约定一致。

第 2 段：把 `toggle` 同步到 `clkout` 域。

[stdlib/rtl/oh_pulse2pulse.v:L37-L42](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_pulse2pulse.v#L37-L42) —— 例化 `oh_dsync` 把 `toggle` 同步成 `toggle_sync`，时钟用 `clkout`、复位用 `nrstout`。

> **源码阅读现场·一个真实的参数名不匹配**：这段例化写的是 `oh_dsync #(.TYPE(TYPE), .SYN(SYN))`，但对照 [oh_dsync.v:L8-L18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dsync.v#L8-L18) 可以看到，`oh_dsync` 的参数其实是 `SYNCPIPE / DELAY / TARGET`，**根本没有 `TYPE` 和 `SYN` 这两个名字**。也就是说这两个按名传递（named parameter binding）并不会绑到任何东西上，`oh_dsync` 实际跑在默认值（`SYNCPIPE=2`、`TARGET="DEFAULT"`）。功能上「歪打正着」还能工作，但作者显然想传递的 `TYPE/SYN` 并没有生效——这是一个值得记下的源码 bug。不同仿真器对「未知命名参数」的处理（告警还是忽略）**待本地验证**。

第 3 段：电平还原成脉冲。

[stdlib/rtl/oh_pulse2pulse.v:L45-L51](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_pulse2pulse.v#L45-L51) —— `pulse_reg` 把 `toggle_sync` 在 `clkout` 域再打一拍，`dout = pulse_reg ^ toggle_sync` 就是上一拍和当前的异或——和 `oh_edge2pulse` 完全同构的任意边沿检测。每检测到一次 `toggle_sync` 的翻沿，`dout` 就吐一个 `clkout` 单周期脉冲，正好对应源端的一次 `din` 脉冲。

#### 4.3.4 代码实践

**实践目标**：把一个**快时钟域**的单周期脉冲送进**慢时钟域**，验证「直接同步会丢、用 `oh_pulse2pulse` 不丢」。

**操作步骤**（**示例代码**）：

```verilog
`timescale 1ns/1ps
module tb_p2p;
  reg clkin = 0, clkout = 0, nrstin = 0, nrstout = 0, din = 0;
  wire dout;
  // clkin 200MHz(快), clkout 50MHz(慢) —— 慢域采不到快域单周期脉冲
  always #2.5 clkin  = ~clkin;
  always #10  clkout = ~clkout;

  oh_pulse2pulse u (.nrstin(nrstin), .din(din), .clkin(clkin),
                     .nrstout(nrstout), .clkout(clkout), .dout(dout));
  initial begin
    $dumpfile("p2p.vcd"); $dumpvars(0, tb_p2p);
    #25 nrstin = 1; nrstout = 1;
    @(posedge clkin); din = 1;   // 快域单周期脉冲
    @(posedge clkin); din = 0;
    #200;
    // 再连发两个，间隔大于 2 倍 clkout 周期，验证不丢
    @(posedge clkin); din = 1; @(posedge clkin); din = 0;
    #80;
    @(posedge clkin); din = 1; @(posedge clkin); din = 0;
    #200 $finish;
  end
endmodule
```

编译：`iverilog -g2005 -o p2p.vvp tb_p2p.v $OH_HOME/stdlib/rtl/oh_pulse2pulse.v $OH_HOME/stdlib/rtl/oh_dsync.v && vvp p2p.vvp`。

**需要观察的现象**：每一次 `din` 脉冲（哪怕只占快域 1 拍），过若干 `clkout` 周期后，`dout` 都会准确出现一个**单 `clkout` 周期**的脉冲。

**预期结果**：发 3 个 `din` 脉冲，`dout` 出现 3 个一一对应的单周期脉冲；中间有 `oh_dsync` 带来的几个 `clkout` 周期延迟。如果想看「会丢」的对照，可另起一个 `oh_dsync` 直接同步 `din`，观察它的输出多半根本不动——因为快域 1 拍脉冲宽度（2.5ns）远小于慢域采样间隔（20ns）。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 3 行注释要求 `din` 脉冲宽度 ≥ 2 倍 `clkout` 周期？

**参考答案**：因为这个转换器是**开环**的——目标域没有任何「我收到了」的应答反馈给源域。如果源端在目标域还没来得及采样到当前 `toggle` 电平之前，就又发了一个脉冲，`toggle_reg` 会连续翻两次（例如 0→1→0），电平回到原值，目标域看就像「什么都没发生」，这一对脉冲就都丢了。「2 倍宽度」是作者给的经验性保守约束，确保每次翻转的电平至少被 `clkout` 采到一次。需要绝对不丢时，工程上会加一条应答通路（闭环握手），但 `oh_pulse2pulse` 没有实现。

**练习 2**：`dout = pulse_reg ^ toggle_sync` 这一行，和本讲哪个模块的核心表达式是一样的？

**参考答案**：和 [oh_edge2pulse](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_edge2pulse.v#L25) 的 `out = in_reg ^ in` 完全同构——都是「上一拍 ^ 当前值」的任意边沿检测。`oh_pulse2pulse` 实际上就是「脉冲→电平（toggle）」+「`oh_dsync` 同步」+「内联的 `oh_edge2pulse`」三件套。

## 5. 综合实践

把第 4.1、4.2 两节串起来，完成本讲的核心任务：**用 `oh_dsync` + `oh_rise2pulse` 把一个慢时钟域的单周期脉冲安全地同步到快时钟域，并输出快域的单周期脉冲。**

**为什么这个组合对「慢→快」安全？** 慢域的 1 个脉冲宽度，等于好几个快域周期（比如慢 100 MHz、快 400 MHz，慢域 1 拍 = 快域 4 拍）。所以快域的同步器一定能采到这个宽电平；再用 `oh_rise2pulse` 取它的上升沿，就还原成一个快域单周期脉冲。这正是 `gpio.v` 同步引脚再检测事件的同一套思路（见 [gpio/hdl/gpio.v:L127-L133](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L127-L133)）。

**操作步骤**（**示例代码**）：

```verilog
`timescale 1ns/1ps
module tb_cdc_slow2fast;
  reg  slow_clk = 0, fast_clk = 0, nreset = 0, pulse_in = 0;
  wire level_sync, pulse_out;
  // 慢 100MHz(周期10ns)，快 400MHz(周期2.5ns) —— 慢域脉冲对快域足够宽
  always #5    slow_clk = ~slow_clk;
  always #1.25 fast_clk = ~fast_clk;

  // 第1步：电平同步（2级）进快域
  oh_dsync #(.SYNCPIPE(2)) u_sync (
     .clk(fast_clk), .nreset(nreset), .din(pulse_in), .dout(level_sync));
  // 第2步：在快域检测上升沿 → 单周期脉冲
  oh_rise2pulse #(.N(1)) u_edge (
     .clk(fast_clk), .nreset(nreset), .in(level_sync), .out(pulse_out));

  // 计数 pulse_out 拉高的次数
  integer hits = 0;
  always @(posedge fast_clk) if (nreset && pulse_out) hits = hits + 1;

  initial begin
    $dumpfile("cdc.vcd"); $dumpvars(0, tb_cdc_slow2fast);
    #20 nreset = 1;
    // 在慢域产生 3 个彼此分开的单周期脉冲
    @(posedge slow_clk); pulse_in = 1; @(posedge slow_clk); pulse_in = 0;  // #1
    #50;
    @(posedge slow_clk); pulse_in = 1; @(posedge slow_clk); pulse_in = 0;  // #2
    #50;
    @(posedge slow_clk); pulse_in = 1; @(posedge slow_clk); pulse_in = 0;  // #3
    #50;
    $display("CDC_TEST: observed %0d rising-edge pulses in fast domain", hits);
    $finish;
  end
endmodule
```

编译运行：

```bash
iverilog -g2005 -o cdc.vvp tb_cdc_slow2fast.v \
    $OH_HOME/stdlib/rtl/oh_dsync.v \
    $OH_HOME/stdlib/rtl/oh_rise2pulse.v
vvp cdc.vvp
gtkwave cdc.vcd
```

**需要观察的现象**：
1. `pulse_in` 每次拉高（持续一个 `slow_clk` 周期 ≈ 4 个 `fast_clk` 周期），`level_sync` 在 2 个 `fast_clk` 周期延迟后跟随变高。
2. `pulse_out` 在 `level_sync` 的上升沿那**一拍**为高，下一拍立即归 0——是干净的单 `fast_clk` 周期脉冲。
3. 3 次 `pulse_in` 对应 3 次 `pulse_out`，打印 `CDC_TEST: observed 3 rising-edge pulses in fast domain`。

**预期结果**：`hits == 3`，且波形里每个 `pulse_out` 都恰好 1 个 `fast_clk` 周期宽。

**进阶思考（不要求实现）**：如果把 `fast_clk` 改成比 `slow_clk` 还慢（即变成「快→慢」），这个组合还能保证不丢脉冲吗？为什么？此时该改用哪个模块？（提示：见 4.3 的 `oh_pulse2pulse`。）把你的结论和 4.3.5 练习 1 的答案对照。

## 6. 本讲小结

- **跨时钟域的根敌人是亚稳态**：采样窗口内输入在翻，触发器输出会在中间电平悬停一段不确定时间，再随机塌缩。同步器不消除亚稳态，而是用「多级触发器」给它留塌缩时间，把 MTBF 提升到可忽略。
- **`oh_dsync` 是电平同步器**：默认两级（`SYNCPIPE=2`），把异域电平可靠搬进本域；`DELAY` 选项可多取一级，也用于仿真注入随机延迟。
- **`oh_rsync` 是复位同步器**，遵循「异步生效、同步释放」——生效要立刻抓住全芯片，释放要对齐时钟避免新的亚稳态。
- **边沿检测三兄弟（`oh_edge2pulse` / `oh_rise2pulse` / `oh_fall2pulse`）是同域电路**，套路都是「打一拍再比较」：异或检测任意沿、`in & ~in_reg` 检测上升沿、`~in & in_reg` 检测下降沿。
- **正确顺序是「先同步、再取沿」**：边沿检测器本身是触发器，必须在目标域、且喂已同步的信号，否则 `in_reg` 会亚稳态。`gpio.v` 的 `oh_dsync` → 存拍 → 边沿/中断就是这个范式。
- **`oh_pulse2pulse` 解决「脉冲跨域」**：脉冲→翻转电平→`oh_dsync` 同步→异或还原脉冲。它是开环的，源端脉冲间隔需 ≥ 2 倍目标周期，否则会丢；其源码里 `oh_dsync` 的例化存在参数名（`TYPE/SYN`）与声明（`SYNCPIPE/DELAY/TARGET`）不匹配的真实瑕疵，功能靠默认值侥幸工作。

## 7. 下一步学习建议

- **下一讲 [u3-l1 存储原语](u3-l1-memory-primitives.md)** 进入数据通路与存储组件，会用到这里和 [u2-l2](u2-l2-sequential-flops.md) 的时序原语搭建双口 RAM 与寄存器堆。
- **强烈推荐紧接着读 [u3-l2 FIFO 设计](u3-l2-fifo-design.md)**：异步 FIFO（`oh_fifo_async` / `oh_fifo_cdc`）是跨时钟域数据传输的终极方案，它用格雷码指针 + 本讲这类同步器实现「不丢不重」的批量数据搬运，并直接用到 `oh_fifo_cdc.v` 里的 `oh_rsync`。读完那一讲你会真正看清同步器在系统里的位置。
- 想立刻看同步器的工业用法，可直接跳读 [gpio/hdl/gpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v) 与 [elink/hdl/erx_clocks.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v)，对照本讲的源码解读印证。
